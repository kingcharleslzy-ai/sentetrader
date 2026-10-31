from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = Path(os.environ.get("AI_TRADER_DESKTOP_ARTIFACT_DIR") or os.environ.get("CARGO_TARGET_DIR", ROOT / "apps/desktop/src-tauri/target"))
MACOS_DESKTOP = (
    TARGET / "release/bundle/macos/AI Trader.app/Contents/MacOS"
    / "ai-trader-desktop"
)
WINDOWS_DESKTOP = TARGET / "release/ai-trader-desktop.exe"


def desktop_executable() -> Path:
    override = os.environ.get("AI_TRADER_DESKTOP_EXECUTABLE")
    if override:
        return Path(override).resolve()
    return WINDOWS_DESKTOP if os.name == "nt" else MACOS_DESKTOP


def child_names() -> set[str]:
    # The cloud-only desktop spawns no local services; any of these under the app is a regression.
    suffix = ".exe" if os.name == "nt" else ""
    return {
        f"ai-trader-runtime{suffix}",
        f"ai-trader-tqkq-sidecar{suffix}",
    }


# pid -> (parent pid, name, creation time). Windows keeps a dead parent's PID in
# ParentProcessId and reuses PIDs, so an unrelated user process (e.g. WPS started by a
# long-gone process whose PID our desktop now has) looks like our child unless the
# child was created after the parent. POSIX reparents orphans, so creation is 0 there.
Snapshot = dict[int, tuple[int, str, int]]


def process_snapshot() -> Snapshot:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId,Name,"
                "@{n='Created';e={if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }}} | "
                "ConvertTo-Json -Compress",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        return {
            int(row["ProcessId"]): (
                int(row["ParentProcessId"]), str(row["Name"]), int(row.get("Created") or 0)
            )
            for row in rows
        }
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,comm="],
        check=True,
        capture_output=True,
        text=True,
    )
    snapshot: Snapshot = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 3:
            snapshot[int(parts[0])] = (int(parts[1]), parts[2], 0)
    return snapshot


def descendants(root_pid: int, snapshot: Snapshot) -> dict[int, int]:
    """Processes in root_pid's tree as pid -> creation time. A child older than its
    recorded parent belongs to an earlier holder of that PID and is not ours."""
    if root_pid not in snapshot:
        return {}
    found: dict[int, int] = {}
    pending = [(root_pid, snapshot[root_pid][2])]
    while pending:
        parent, parent_created = pending.pop()
        for pid, (ppid, _, created) in snapshot.items():
            if ppid == parent and pid != root_pid and pid not in found and created >= parent_created:
                found[pid] = created
                pending.append((pid, created))
    return found


def still_running(children: dict[int, int], snapshot: Snapshot) -> set[int]:
    """Children that are still the same process; a reused PID is a different process."""
    return {pid for pid, created in children.items() if pid in snapshot and snapshot[pid][2] == created}


def competing_macos_instances(desktop: Path) -> set[int]:
    if os.name == "nt":
        return set()
    expected = desktop.resolve()
    return {
        pid
        for pid, (_, command, _) in process_snapshot().items()
        if Path(command).name == desktop.name
        and Path(command).resolve() != expected
    }


def preexisting_windows_desktops() -> set[int]:
    if os.name != "nt":
        return set()
    return {
        pid
        for pid, (_, name, _) in process_snapshot().items()
        if name.lower() == "ai-trader-desktop.exe"
    }


def clear_preexisting_windows_desktops() -> set[int]:
    # A tray-hidden desktop app holds the single-instance lock and makes every
    # launched smoke instance exit before spawning children.
    stale = preexisting_windows_desktops()
    for pid in stale:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
    return stale


def settle_without_children(root_pid: int, seconds: float = 8.0) -> dict[int, int]:
    """The window must come up without spawning a local Runtime or sidecar."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        snapshot = process_snapshot()
        found = descendants(root_pid, snapshot)
        names = {Path(snapshot[pid][1]).name for pid in found if pid in snapshot}
        if names & child_names():
            raise RuntimeError(f"desktop_spawned_local_services:{sorted(names & child_names())}")
        time.sleep(0.5)
    return descendants(root_pid, process_snapshot())


def wait_for_frontend_ready(ready_file: Path, seconds: float = 30.0) -> float:
    """The embedded frontend must run: the shell writes this file when React reports ready.
    A bundle whose frontend never executes (wrong asset base, broken script) fails here."""
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        if ready_file.is_file():
            build = ready_file.read_text().strip()
            expected = os.environ.get("AI_TRADER_BUILD_SHA")
            if build:
                if expected and build != expected:
                    raise RuntimeError(f"desktop_frontend_build_mismatch:{expected}:{build}")
                return round(time.monotonic() - started, 1)
        time.sleep(0.5)
    raise RuntimeError("desktop_frontend_never_ready")


def wait_for_exit(process: subprocess.Popen[bytes], timeout: float) -> int:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("desktop_second_instance_did_not_exit") from error


def terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    desktop = desktop_executable()
    if not desktop.is_file():
        raise RuntimeError("desktop_bundle_missing")
    competing = competing_macos_instances(desktop)
    if competing and os.environ.get("AI_TRADER_DESKTOP_SMOKE_STRICT") == "1":
        # The release run must actually exercise the bundle; another instance of the same bundle id
        # would swallow the launch through the single-instance lock and fake a failure.
        raise RuntimeError(f"desktop_smoke_competing_instance:{sorted(competing)}")
    if competing:
        print(json.dumps({
            "status": "skipped",
            "reason": "preexistingDesktopInstance",
            "singleInstance": "notRetested",
            "liveTrading": "disabled",
        }))
        return 0
    stale = clear_preexisting_windows_desktops()
    if stale:
        print(json.dumps({
            "status": "cleaned",
            "reason": "preexistingDesktopInstance",
            "terminatedPids": sorted(stale),
        }))
        time.sleep(2)
    environment = dict(os.environ)
    ready_file = Path(os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or "/tmp") / f"ai-trader-desktop-ready-{os.getpid()}"
    ready_file.unlink(missing_ok=True)
    environment["AI_TRADER_DESKTOP_READY_FILE"] = str(ready_file)
    ready_seconds = 0.0
    first: subprocess.Popen[bytes] | None = None
    second: subprocess.Popen[bytes] | None = None
    children: dict[int, int] = {}
    try:
        first = subprocess.Popen(
            [str(desktop)], cwd=ROOT, env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        children = settle_without_children(first.pid)
        ready_seconds = wait_for_frontend_ready(ready_file)
        second = subprocess.Popen(
            [str(desktop)], cwd=ROOT, env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if wait_for_exit(second, 15) != 0:
            raise RuntimeError("desktop_second_instance_failed")
        if first.poll() is not None:
            raise RuntimeError("desktop_primary_instance_exited")
        terminate(first)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not still_running(children, process_snapshot()):
                break
            time.sleep(0.2)
        else:
            snapshot = process_snapshot()
            remaining = {
                pid: {"parentPid": snapshot[pid][0], "name": snapshot[pid][1]}
                for pid in sorted(still_running(children, snapshot))
            }
            raise RuntimeError(f"desktop_child_process_leak:{json.dumps(remaining, sort_keys=True)}")
        print(json.dumps({
            "status": "ok", "singleInstance": True, "localServicesSpawned": False, "frontendReadySeconds": ready_seconds,
            "childCleanup": True, "liveTrading": "disabled",
        }))
        return 0
    finally:
        ready_file.unlink(missing_ok=True)
        terminate(second)
        terminate(first)
        for pid in still_running(children, process_snapshot()):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                # Already gone or access denied (WinError 5); cleanup must not mask the result.
                pass


if __name__ == "__main__":
    raise SystemExit(main())
