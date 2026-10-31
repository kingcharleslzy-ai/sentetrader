"""Serialize shared desktop outputs until consumers have a verified private copy."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys


def tree_digest(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            result[str(path.relative_to(root))] = "link:" + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result[str(path.relative_to(root))] = digest.hexdigest()
    return result


def snapshot_bundle(target, destination):
    source = target / "release/bundle"
    before = tree_digest(source)
    if not before:
        raise RuntimeError("desktop_bundle_missing")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        copied = destination / "release/bundle"
        shutil.copytree(source, copied, symlinks=True)
        if tree_digest(copied) != before or tree_digest(source) != before:
            raise RuntimeError("desktop_candidate_copy_mismatch")
        (destination / "checksums.json").write_text(json.dumps(before, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(destination)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a build command is required")
    target = Path(os.environ.get("CARGO_TARGET_DIR", Path.home() / ".cache/ai-trader/cargo-target")).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if args.snapshot and (args.snapshot.resolve() == target or target in args.snapshot.resolve().parents):
        parser.error("candidate must be outside the shared Cargo target")
    with (target / ".ai-trader-build.lock").open("a+") as lock:
        print("等待桌面构建目录锁…", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = None
        def stop(signum, frame):
            if child is not None:
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass
            raise SystemExit(128 + signum)
        previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            child = subprocess.Popen(command, env={**os.environ, "CARGO_TARGET_DIR": str(target),
                "AI_TRADER_BUILD_LOCK_FD": str(lock.fileno())}, pass_fds=(lock.fileno(),), start_new_session=True)
            status = child.wait()
            if status == 0 and args.snapshot:
                snapshot_bundle(target, args.snapshot.resolve())
                print("已核对独立制品：" + str(args.snapshot.resolve()), flush=True)
            return status if status >= 0 else 128 - status
        finally:
            if child is not None:
                # A failed shell/node process may leave its compiler behind.
                # Terminate its process group before releasing the output lock.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
