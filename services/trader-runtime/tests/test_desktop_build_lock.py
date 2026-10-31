"""Real entrypoints, controlled compiler boundary; no signing or publishing."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not NODE, reason="shared POSIX desktop target")


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("build boundary not reached")
        time.sleep(.02)


@pytest.fixture
def builds(tmp_path):
    tools = tmp_path / "tools"; tools.mkdir()
    npm = tools / "npm"
    npm.write_text(f"#!{sys.executable}\n" + '''import os,sys,time
from pathlib import Path
root=Path(os.environ["PROBE_ROOT"]); name=os.environ["PROBE_NAME"]
if sys.argv[-1]=="prepare:sidecars":
    (root/(name+".entered")).touch()
    while not (root/(name+".release")).exists(): time.sleep(.02)
    if os.environ.get("PROBE_FAIL"): sys.exit(7)
else:
    bundle=Path(os.environ["CARGO_TARGET_DIR"])/"release/bundle/macos"
    bundle.mkdir(parents=True,exist_ok=True)
    (bundle/"candidate.txt").write_text(name)
''')
    npm.chmod(0o755)
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "CARGO_TARGET_DIR": str(tmp_path / "cargo"), "PROBE_ROOT": str(tmp_path),
        "AI_TRADER_BUILD_PYTHON": sys.executable}
    env.pop("AI_TRADER_BUILD_LOCK_FD", None)
    processes = []
    def start(name, *, fail=False, publisher=False):
        output = (tmp_path / (name + ".log")).open("w")
        process = subprocess.Popen([NODE, str(ROOT / "scripts" / ("publish_desktop_update.mjs" if publisher else "build_desktop_app.mjs"))],
            cwd=ROOT, env={**env, "PROBE_NAME": name, "PROBE_FAIL": "1" if fail else "",
                "AI_TRADER_DESKTOP_ARTIFACT_DIR": str(tmp_path / (name + "-candidate"))}, stdout=output, stderr=output)
        processes.append((process, output))
        return process
    yield start, env
    for process, output in processes:
        if process.poll() is None: process.terminate()
        process.wait(timeout=5)
        output.close()


def test_app_builds_wait_and_keep_verified_independent_candidates(tmp_path, builds):
    start, _ = builds
    first = start("first")
    wait_for(lambda: (tmp_path / "first.entered").exists())
    second = start("second")
    time.sleep(.3)
    assert not (tmp_path / "second.entered").exists()
    (tmp_path / "first.release").touch()
    assert first.wait(timeout=5) == 0
    wait_for(lambda: (tmp_path / "second.entered").exists())
    (tmp_path / "second.release").touch()
    assert second.wait(timeout=5) == 0
    for name in ("first", "second"):
        candidate = tmp_path / (name + "-candidate")
        assert (candidate / "release/bundle/macos/candidate.txt").read_text() == name
        assert "macos/candidate.txt" in json.loads((candidate / "checksums.json").read_text())


@pytest.mark.parametrize("termination", ["failure", "signal"])
def test_failed_build_releases_lock_without_exposing_candidate(tmp_path, builds, termination):
    start, _ = builds
    first = start("first", fail=termination == "failure")
    wait_for(lambda: (tmp_path / "first.entered").exists())
    second = start("second")
    if termination == "failure":
        (tmp_path / "first.release").touch()
    else:
        first.terminate()
    assert first.wait(timeout=5) != 0
    wait_for(lambda: (tmp_path / "second.entered").exists())
    (tmp_path / "second.release").touch()
    assert second.wait(timeout=5) == 0
    assert not (tmp_path / "first-candidate").exists()


def test_publisher_waits_for_same_lock_before_preflight_or_credentials(tmp_path, builds):
    start, _ = builds
    first = start("first")
    wait_for(lambda: (tmp_path / "first.entered").exists())
    publisher = start("publisher", publisher=True)
    time.sleep(.3)
    assert publisher.poll() is None
    assert "desktop_update_windows_artifacts_required" not in (tmp_path / "publisher.log").read_text()
    (tmp_path / "first.release").touch()
    assert first.wait(timeout=5) == 0
    assert publisher.wait(timeout=5) != 0
    assert "desktop_update_windows_artifacts_required" in (tmp_path / "publisher.log").read_text()


def test_lock_is_held_during_candidate_copy_and_checksum(tmp_path, builds):
    start, env = builds
    source = tmp_path / "cargo/release/bundle"; source.mkdir(parents=True)
    (source / "result").write_text("first")
    code = '''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import desktop_build_lock as build
root=Path(sys.argv[2]); original=build.shutil.copytree
def paused(*args,**kwargs):
    (root/"copy.entered").touch()
    while not (root/"copy.release").exists(): time.sleep(.02)
    return original(*args,**kwargs)
build.shutil.copytree=paused
sys.argv=["build", "--snapshot",str(root/"copied"),"--",sys.executable,"-c","pass"]
raise SystemExit(build.main())
'''
    holder = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "scripts"), str(tmp_path)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: (tmp_path / "copy.entered").exists())
        second = start("second")
        time.sleep(.3)
        assert not (tmp_path / "second.entered").exists()
        (tmp_path / "copy.release").touch()
        assert holder.wait(timeout=5) == 0
        wait_for(lambda: (tmp_path / "second.entered").exists())
        (tmp_path / "second.release").touch()
        assert second.wait(timeout=5) == 0
        assert (tmp_path / "copied/release/bundle/result").read_text() == "first"
        assert (tmp_path / "copied/checksums.json").exists()
    finally:
        if holder.poll() is None: holder.terminate()
        holder.wait(timeout=5)
