import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("maintenance", Path(__file__).with_name("maintain_update_host.py"))
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


def test_retention_preserves_current_patch_bases_and_recent_publications(tmp_path):
    root = tmp_path
    directory = root / "releases"
    directory.mkdir()
    names = [f"1.0.{i}-{i:016x}" for i in range(7)]
    for i, name in enumerate(names):
        path = directory / name
        path.mkdir()
        os.utime(path, (i * 100, i * 100))
    now = 10 * 86400
    os.utime(directory / names[2], (now - 5, now - 5))
    (root / "latest.json").write_text(json.dumps({"platforms": {"mac": {
        "url": f"https://example/releases/{names[1]}/app",
        "patches": [{"baseRelease": names[0]}],
    }}}))
    keep, candidates = maintenance.retained_releases(root, 3, now)
    assert {names[0], names[1], names[2], names[5], names[6]} <= keep
    assert {p.name for p in candidates} == {names[3], names[4]}


def test_missing_current_release_stops_cleanup(tmp_path):
    (tmp_path / "releases").mkdir()
    (tmp_path / "latest.json").write_text(json.dumps({"platforms": {"mac": {"url": "https://example/releases/missing/app"}}}))
    with pytest.raises(ValueError, match="refusing cleanup"):
        maintenance.retained_releases(tmp_path, 3, 1_000_000)


def test_running_worker_and_deploy_process_are_busy(tmp_path):
    process = tmp_path / "42"
    process.mkdir()
    (process / "comm").write_text("Runner.Worker\n")
    assert maintenance.busy_processes(tmp_path)
    (process / "comm").write_text("sh\n")
    (process / "cmdline").write_bytes(b"sh\0/opt/ai-trader/deploy-123abcd.sh\0")
    assert maintenance.busy_processes(tmp_path)
    (process / "cmdline").write_bytes(b"sh\0-c\0echo deploy-123abcd.sh\0")
    assert not maintenance.busy_processes(tmp_path)


def test_container_rollback_and_recent_images_are_protected(monkeypatch):
    from datetime import datetime, timezone
    now = 1_000_000
    def command(*args):
        if args == ("docker", "ps", "-aq"):
            return "running\nstopped"
        if args[1:3] == ("inspect", "--format"):
            assert args[-2:] == ("running", "stopped")
            return "image0\nimage1"
        if args[1:3] == ("image", "ls"):
            # The registry repositories carry full 40-hex aliases; the bare names short tags.
            digits = "{:040x}" if args[3].startswith("127.0.0.1:5000/") else "{:07x}"
            return "\n".join(f"{args[3]}:" + digits.format(i) for i in range(9)) + "\nother:latest"
        return json.dumps([{
            "Created": datetime.fromtimestamp(now - 5 if int(tag.split(":")[-1], 16) == 5
                else int(tag.split(":")[-1], 16) * 100, timezone.utc).isoformat(),
            "Id": f"image{int(tag.split(':')[-1], 16)}",
        } for tag in args[3:]])
    monkeypatch.setattr(maintenance, "command", command)
    # Since #668 the two managed registry repositories are enumerated too, and a stale image is
    # only reclaimable when every managed alias naming it goes — the short tag AND the
    # 127.0.0.1:5000 full-sha alias of the same stale identity are selected together.
    assert set(maintenance.stale_image_tags({"ai-trader-runtime:0000002"}, now)) == {
        f"ai-trader-{repo}:{i:07x}" for repo in ("runtime", "sidecar") for i in (3, 4, 6)
    } | {
        f"127.0.0.1:5000/ai-trader-{repo}:{i:040x}" for repo in ("runtime", "sidecar") for i in (3, 4, 6)
    }


def test_registry_aliases_of_stale_images_are_selected_by_identity(monkeypatch):
    from datetime import datetime, timezone
    now = 1_000_000
    reg = "127.0.0.1:5000"
    # Production's real residue: older images carry both a short tag and a registry full-sha
    # alias (or, after earlier sweeps, only the alias); every release leaves both naming the same
    # image. Images 0-1 are stale (>1h, not current, not running — except a stopped container on
    # runtime 0), 2-4 are the newest three plus a fresh candidate.
    store = {}
    for repo in ("runtime", "sidecar"):
        for i in range(5):
            identity, created = f"image-{repo}-{i}", (now - 5 if i == 4 else now - (5 - i) * 3600)
            if i == 0:
                store[f"{reg}/ai-trader-{repo}:{i:040x}"] = (identity, created)
            else:
                store[f"ai-trader-{repo}:{i:07x}"] = (identity, created)
                store[f"{reg}/ai-trader-{repo}:{i:040x}"] = (identity, created)
    enumerated = []

    def command(*args):
        if args == ("docker", "ps", "-aq"):
            return "stopped"
        if args[1:3] == ("inspect", "--format"):
            return "image-runtime-0"  # the stopped container runs (rolled back to) runtime 0
        if args[1:3] == ("image", "ls"):
            enumerated.append(args[3])
            # An unknown short-hex tag under a registry repo (a test push, a manual tag) sits
            # beside the managed 40-hex aliases and must never be enumerated as managed.
            extra = f"{args[3]}:abcdef1\n" if args[3].startswith(reg + "/") else ""
            return "\n".join(tag for tag in store if tag.startswith(args[3] + ":")) + "\n" \
                + "unmanaged-repo:whatever\n" + f"{args[3]}:latest\n" + extra
        return json.dumps([{"Created": datetime.fromtimestamp(store[tag][1], timezone.utc).isoformat(),
                            "Id": store[tag][0]} for tag in args[3:]])

    monkeypatch.setattr(maintenance, "command", command)
    removable = set(maintenance.stale_image_tags((), now))
    # Stale identities lose ALL their managed aliases together (both kinds of image 1), the
    # alias-only stale identity is reclaimed too (sidecar 0), and the identity a stopped container
    # runs keeps every alias (runtime 0) — protection follows the image, not the tag spelling.
    assert removable == {
        "ai-trader-runtime:0000001", f"{reg}/ai-trader-runtime:{1:040x}",
        "ai-trader-sidecar:0000001", f"{reg}/ai-trader-sidecar:{1:040x}",
        f"{reg}/ai-trader-sidecar:{0:040x}",
    }, removable
    assert not any(tag.endswith(":abcdef1") for tag in removable)
    # Only the four managed repositories are ever enumerated; unknown repos and non-sha tags
    # never reach the selector.
    assert sorted(enumerated) == sorted([
        "ai-trader-runtime", "ai-trader-sidecar",
        f"{reg}/ai-trader-runtime", f"{reg}/ai-trader-sidecar"]), enumerated


def runtime_fixture(root):
    names = [f"{i:040x}" for i in range(7)]
    for i, name in enumerate(names):
        release = root / "releases" / name
        manifest = release / "deploy/vps/images.yml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(f"services:\n  runtime:\n    image: ai-trader-runtime:{i:07x}\n")
        os.utime(manifest, (100 + i, 100 + i))
        os.utime(release, (100 + i, 100 + i))
    (root / "current").symlink_to(root / "releases" / names[2])
    return names


def test_runtime_retains_current_rollback_container_and_open_cwd(tmp_path, monkeypatch):
    names = runtime_fixture(tmp_path)
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **kwargs: {
        tmp_path / "releases" / names[1] / "work"
    })
    kept, candidates, tags = maintenance.runtime_releases(tmp_path, 3, 1_000_000, [
        tmp_path / "releases" / names[0] / "deploy/vps/images.yml"
    ])
    assert kept == {names[i] for i in (0, 1, 2, 4, 5, 6)}
    assert candidates == [tmp_path / "releases" / names[3]]
    assert "ai-trader-runtime:0000000" in tags
    (tmp_path / "releases" / names[2] / "deploy/vps/images.yml").write_text("services: {}")
    with pytest.raises(ValueError, match="retained Runtime image manifest is invalid"):
        maintenance.runtime_releases(tmp_path, 3, 1_000_000)


def test_invalid_rollback_manifest_stops_cleanup(tmp_path, monkeypatch):
    names = runtime_fixture(tmp_path)
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **kwargs: set())
    (tmp_path / "releases" / names[6] / "deploy/vps/images.yml").write_text("services: {}")
    with pytest.raises(ValueError, match="retained Runtime image manifest"):
        maintenance.runtime_releases(tmp_path, 3, 1_000_000)


def test_failed_build_without_manifest_is_removed_not_kept(tmp_path, monkeypatch):
    # A build that failed never wrote images.yml; it must not take a kept slot or stop every sweep.
    names = runtime_fixture(tmp_path)
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **kwargs: set())
    (tmp_path / "releases" / names[6] / "deploy/vps/images.yml").unlink()
    kept, candidates, _ = maintenance.runtime_releases(tmp_path, 3, 1_000_000)
    assert kept == {names[i] for i in (2, 3, 4, 5)}
    assert {p.name for p in candidates} == {names[i] for i in (0, 1, 6)}
    # Within the upload hour it is still a candidate being built, and the current release never goes.
    os.utime(tmp_path / "releases" / names[6], (1_000_000 - 60, 1_000_000 - 60))
    assert names[6] in maintenance.runtime_releases(tmp_path, 3, 1_000_000)[0]
    (tmp_path / "releases" / names[2] / "deploy/vps/images.yml").unlink()
    with pytest.raises(ValueError, match="retained Runtime image manifest missing"):
        maintenance.runtime_releases(tmp_path, 3, 1_000_000)


def test_open_download_and_working_directory_are_detected(tmp_path):
    proc, root = tmp_path / "proc", tmp_path / "releases"
    (proc / "12/fd").mkdir(parents=True)
    (proc / "12/fd/7").symlink_to(root / "version/app.tar")
    (proc / "12/cwd").symlink_to(root / "version")
    assert maintenance.open_release_paths(proc, (root,)) == {root / "version", root / "version/app.tar"}


def test_runtime_without_update_site_cleanup_and_second_run(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    names = runtime_fixture(root)
    monkeypatch.setattr(maintenance, "RUNTIME_ROOT", root)
    monkeypatch.setattr(maintenance, "UPDATE_ROOT", tmp_path / "absent-update-site")
    monkeypatch.setattr(maintenance, "busy_processes", lambda: False)
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **kwargs: set())
    monkeypatch.setattr(maintenance.time, "time", lambda: 1_000_000)
    for name in ("agent-expired", "agent-recent", "unmanaged"):
        path = root / "tmp" / name
        path.mkdir(parents=True)
        os.utime(path, (999_999 if name == "agent-recent" else 1,)*2)
    for name in ("backups", "secrets", "volumes"):
        (root / name).mkdir()
        (root / name / "important").write_text("preserve")
    calls = []
    monkeypatch.setattr(maintenance, "command", lambda *args: calls.append(args) or "")
    monkeypatch.setattr(maintenance, "stale_image_tags", lambda *args: [])
    plan = maintenance.maintain(apply=False, docker=True, journal=True)
    assert len(plan["releaseCandidates"]) == 4
    assert all(Path(p).exists() for p in plan["releaseCandidates"])
    assert not any("prune" in call for call in calls)
    applied = maintenance.maintain(apply=True, docker=True, journal=True)
    assert set(applied["removed"]) == set(plan["releaseCandidates"])
    second = maintenance.maintain(apply=True, docker=True, journal=True)
    assert second["removed"] == second["releaseCandidates"] == []
    assert (root / "current").resolve().name == names[2]
    for name in ("backups", "secrets", "volumes"):
        assert (root / name / "important").read_text() == "preserve"
    assert (root / "tmp/agent-recent").is_dir() and (root / "tmp/unmanaged").is_dir()
    assert ("docker", "buildx", "prune", "--force", "--filter", "until=24h", "--max-used-space", "2GB") in calls


def test_busy_marker_and_missing_rollback_image_prevent_any_deletion(tmp_path, monkeypatch):
    runtime_fixture(tmp_path)
    monkeypatch.setattr(maintenance, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(maintenance, "UPDATE_ROOT", tmp_path / "absent-update-site")
    monkeypatch.setattr(maintenance, "busy_processes", lambda: False)
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **kwargs: set())
    monkeypatch.setattr(maintenance.time, "time", lambda: 1_000_000)
    monkeypatch.setattr(maintenance, "command", lambda *args: "")
    marker = tmp_path / ".deploying"
    marker.mkdir()
    os.utime(marker, (1_000_000 - 60, 1_000_000 - 60))
    assert maintenance.maintain(apply=True, docker=True, journal=True)["status"] == "deferred_busy"
    # A marker left for hours fails the unit visibly instead of deferring every hour.
    os.utime(marker, (1_000_000 - 3 * 3600, 1_000_000 - 3 * 3600))
    assert maintenance.maintain(apply=True, docker=True, journal=True)["status"] == "stale_deploy_marker"
    marker.rmdir()
    def missing(*args):
        raise ValueError("rollback image missing")
    monkeypatch.setattr(maintenance, "stale_image_tags", missing)
    with pytest.raises(ValueError, match="rollback image missing"):
        maintenance.maintain(apply=True, docker=True, journal=True)
    assert len(list((tmp_path / "releases").iterdir())) == 7


def nas_store(now):
    # The NAS's real residue shape: per release sha both 127.0.0.1:15000 full-sha refs, built in
    # release order (sha i built i hours after the first, the last one ten minutes ago), beside
    # unmanaged images that share the daemon.
    from datetime import datetime, timezone
    nas = "127.0.0.1:15000"
    store = {}
    for i in range(8):
        built = now - 600 if i == 7 else now - (10 - i) * 3600
        for repo in ("runtime", "sidecar"):
            store[f"{nas}/ai-trader-{repo}:{i:040x}"] = (f"id-{repo}-{i}",
                datetime.fromtimestamp(built, timezone.utc).isoformat().replace("+00:00", "Z"))
    return store


def nas_command(store, enumerated, calls=None, used="id-rsshub"):
    def command(*args):
        if calls is not None:
            calls.append(args)
        if args == ("docker", "ps", "-aq"):
            return "rsshub"
        if args[1:3] == ("inspect", "--format"):
            return used
        if args[1:3] == ("image", "ls"):
            enumerated.append(args[3])
            # Unmanaged neighbours under the managed repository names never reach the selector.
            return "\n".join(tag for tag in store if tag.startswith(args[3] + ":")) \
                + f"\n{args[3]}:latest\n{args[3]}:abcdef1\npython:3.12-slim\ndiygod/rsshub:2026-10-06\n"
        if args[1:3] == ("image", "inspect"):
            # An optional third field is the image's last-tag time (docker's Metadata.LastTagTime).
            return json.dumps([{"Id": store[tag][0], "Created": store[tag][1],
                                **({"Metadata": {"LastTagTime": store[tag][2]}} if len(store[tag]) > 2 else {})}
                               for tag in args[3:]])
        return ""
    return command


def test_nas_retention_keeps_current_rollback_newest_three_and_fresh_builds(monkeypatch):
    now = 2_000_000
    store, enumerated = nas_store(now), []
    monkeypatch.setattr(maintenance, "command", nas_command(store, enumerated))
    current, rollback = f"{1:040x}", f"{0:040x}"
    removable = set(maintenance.stale_nas_tags({current, rollback}, keep=3, now=now))
    # Kept: current (1) and rollback (0) though old, the newest three shas (5, 6, 7 — 7 also
    # inside the in-progress hour); removed: both refs of every other release sha (2, 3, 4).
    assert removable == {f"127.0.0.1:15000/ai-trader-{repo}:{i:040x}"
                         for repo in ("runtime", "sidecar") for i in (2, 3, 4)}, removable
    # Only the two managed push repositories are enumerated; base images, rsshub and non-sha
    # tags under the managed names are never candidates.
    assert sorted(enumerated) == ["127.0.0.1:15000/ai-trader-runtime", "127.0.0.1:15000/ai-trader-sidecar"]
    assert not any(tag.endswith((":latest", ":abcdef1")) for tag in removable)


def iso(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def test_nas_retention_ranks_by_tag_time_not_image_created(monkeypatch):
    # Sha 2 is a fully cached rebuild: its images keep an 8-hour-old Created but were tagged two
    # hours ago (outside the fresh hour), so by release time it outranks shas 3-6.
    now = 2_000_000
    store = nas_store(now)
    for repo in ("runtime", "sidecar"):
        tag = f"127.0.0.1:15000/ai-trader-{repo}:{2:040x}"
        store[tag] = (*store[tag], iso(now - 7200))
    monkeypatch.setattr(maintenance, "command", nas_command(store, []))
    # Keep the newest 2 plus protected sha 0: sha 7 (newest) and sha 2 (re-tagged).
    removable = {tag.rsplit(":", 1)[1] for tag in maintenance.stale_nas_tags({f"{0:040x}"}, keep=2, now=now)}
    assert removable == {f"{i:040x}" for i in (1, 3, 4, 5, 6)}, removable


def test_nas_retention_keeps_whole_sha_when_a_container_uses_one_image(monkeypatch):
    now = 2_000_000
    monkeypatch.setattr(maintenance, "command", nas_command(nas_store(now), [], used="id-sidecar-3"))
    removable = {tag for tag in maintenance.stale_nas_tags({f"{0:040x}"}, keep=1, now=now)}
    # Sha 3 is old, but its sidecar is in use: the runtime ref of the same release stays too.
    assert not any(tag.endswith(f":{3:040x}") for tag in removable), removable
    assert any(tag.endswith(f":{2:040x}") for tag in removable)


def test_nas_retention_incomplete_sha_does_not_take_a_keep_place(monkeypatch):
    now = 2_000_000
    store = nas_store(now)
    # Sha 6 only got its runtime image pushed (an hour+ ago): it is not a release, so the newest
    # `keep` complete shas are 7 and 5, and the leftover runtime ref is itself reclaimable.
    del store[f"127.0.0.1:15000/ai-trader-sidecar:{6:040x}"]
    monkeypatch.setattr(maintenance, "command", nas_command(store, []))
    removable = set(maintenance.stale_nas_tags({f"{0:040x}"}, keep=2, now=now))
    assert f"127.0.0.1:15000/ai-trader-runtime:{6:040x}" in removable
    assert not any(tag.endswith(f":{5:040x}") for tag in removable)
    assert not any(tag.endswith(f":{7:040x}") for tag in removable)


def test_nas_retention_refuses_without_full_protected_shas(monkeypatch):
    monkeypatch.setattr(maintenance, "command", nas_command(nas_store(2_000_000), []))
    for protected in (set(), {"abc1234"}, {"<current>"}):
        with pytest.raises(ValueError):
            maintenance.stale_nas_tags(protected, keep=3, now=2_000_000)


def test_nas_plan_deletes_nothing_and_apply_removes_only_candidates(monkeypatch):
    now = 2_000_000
    store = nas_store(now)
    calls, removed = [], []
    monkeypatch.setattr(maintenance, "command", nas_command(store, [], calls))
    monkeypatch.setattr(maintenance, "busy_processes", lambda: False)
    monkeypatch.setattr(maintenance.time, "time", lambda: now)
    monkeypatch.setattr(maintenance.subprocess, "run", lambda args, **_: removed.append(args))
    protected = {f"{1:040x}", f"{0:040x}"}
    plan = maintenance.maintain_nas(apply=False, protected_shas=protected)
    assert plan["mode"] == "plan" and len(plan["imageCandidates"]) == 6
    assert removed == [] and plan["removed"] == []
    assert not any(args[1:3] in (("buildx", "prune"), ("image", "rm")) for args in calls), calls
    applied = maintenance.maintain_nas(apply=True, protected_shas=protected)
    assert sorted(applied["removed"]) == sorted(plan["imageCandidates"])
    assert removed == [["docker", "image", "rm", tag] for tag in applied["removed"]]
    assert ("docker", "buildx", "prune", "--force", "--max-used-space", "25GB") in calls
    # A busy host (a runner job or a build elsewhere on the NAS) defers without deleting.
    removed.clear()
    monkeypatch.setattr(maintenance, "busy_processes", lambda: True)
    assert maintenance.maintain_nas(apply=True, protected_shas=protected)["status"] == "deferred_busy"
    assert removed == []


def formal_deploy_run(tmp_path, mode, flags=(), env_updates=None):
    import subprocess
    import sys
    scripts = tmp_path / "bin"
    scripts.mkdir(parents=True)
    marker = tmp_path / "deploying"
    for name, body in {
        "node": "#!/bin/sh\necho 1.6.11\n",
        "git": '#!/bin/sh\necho \'{"version":"1.6.11"}\'\n',
        "ssh": f'''#!{sys.executable}
import json, os, pathlib, signal, sys
with open(os.environ["TEST_SSH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
marker = pathlib.Path(os.environ["TEST_MARKER"])
hostlog = pathlib.Path(os.environ["TEST_HOSTLOG"])
command = sys.argv[-1]
if os.environ["TEST_MODE"] == "jump-fails" and "-J" in sys.argv:
    sys.exit(255)
elif command == "true":
    sys.exit({{"nas-unreachable": 255, "nas-unreachable-1": 1}}.get(os.environ["TEST_MODE"], 0))
elif "flock -n" in command:
    marker.mkdir()
elif "rmdir" in command:
    marker.rmdir()
elif "systemd-run" in command:
    # The deploy phase: the host script now runs under a transient unit whose output the outer
    # script reads back from a host-side log, so the mock plays the unit (exit code) and writes
    # the log the later "sudo cat" fetch returns.
    mode = os.environ["TEST_MODE"]
    if mode in ("disconnect", "deploy-disconnect"):
        # The unit had already written stage output when the caller died: the log fetch below
        # still succeeds, and the outer script must classify on the 255 alone, not the partial
        # log content.
        hostlog.write_text("up -d trader-runtime-green: started\\n")
        sys.exit(255)
    if mode == "signal":
        os.kill(os.getppid(), signal.SIGTERM)
        sys.exit(255)
    if mode == "host-signal":
        hostlog.write_text("DEPLOY_ABORTED_BY_SIGNAL: interrupting the switch, restoring /opt/ai-trader/releases/<prev>\\n"
                           "ROLLBACK_OK: restored <prev>\\n")
        sys.exit(143)
    if mode == "host-signal-after-cutover":
        hostlog.write_text("DEPLOY_ABORTED_BY_SIGNAL_AFTER_CUTOVER: current is already <sha>; the switch stands, no rollback ran\\n")
        sys.exit(143)
    if mode == "rollback-failed":
        # The host script's rollback itself failed: the log must surface that, and the outer
        # script must not label it "rolled back" — nor auto-release the guard.
        hostlog.write_text("NEW_SLOT_START_FAILED\\nROLLBACK_FAILED: restoring /opt/ai-trader/releases/<prev> did not complete\\n")
        sys.exit(1)
    if mode == "remote-failure":
        hostlog.write_text("NEW_SLOT_HEALTHZ_MISMATCH\\nROLLBACK_OK: restored <prev>\\n")
        sys.exit(1)
    if mode == "start-failed":
        sys.exit(1)  # The unit never started; no log is ever written.
    if mode == "deploy-ok-no-marker":
        hostlog.write_text("service plan: recreating tqkq-sidecar trader-runtime-green\\n")
        sys.exit(0)
    hostlog.write_text("DEPLOY_OK\\n")
    sys.exit(0)
elif command.startswith("sudo cat "):
    if not hostlog.exists():
        sys.exit(1)
    sys.stdout.write(hostlog.read_text())
    if os.environ["TEST_MODE"] == "cat-fails-after-ok":
        # The read-back connection died after emitting the complete-looking output: no marker
        # in it may be trusted because the read itself failed.
        sys.exit(255)
elif "docker image ls" in command:
    # The #667 existence listing: the full-sha refs are present in this harness, so the digest
    # inspect follows and answers.
    print("sha256:mock-listed")
elif "docker image inspect" in command:
    if "--format" in command:
        # The nas-registry identity checks (#667): digest reads answer on both ends, and the
        # cached short tag / full-sha ref image-ID reads answer the same identity.
        if "{{.Id}}" in command:
            print("sha256:cached-identity")
        else:
            print("repo@sha256:test")
    elif os.environ["TEST_MODE"] in ("route", "jump-fails"):
        sys.exit(1)
elif "docker push" in command:
    pathlib.Path(os.environ["TEST_DOCKER_STATE"]).write_text("pushed")
elif command.startswith("sudo docker pull"):
    # The registry holds the full-sha tags only once the NAS pushed them (same run or a rerun).
    sys.exit(0 if pathlib.Path(os.environ.get("TEST_DOCKER_STATE", "/nonexistent")).exists() else 1)
elif "readlink -f" in command:
    previous = os.environ["TEST_MODE"] in ("remote-failure", "rollback-failed", "host-signal", "host-signal-after-cutover",
                                           "signal", "disconnect", "deploy-disconnect", "start-failed", "deploy-ok-no-marker",
                                           "deploy-ok", "cat-fails-after-ok")
    print("/opt/ai-trader/releases/" + ("b" if previous else "a") * 40)
''',
    }.items():
        path = scripts / name
        path.write_text(body)
        path.chmod(0o755)
    deploy = Path(__file__).resolve().parents[1] / "deploy/vps/deploy-release.sh"
    ssh_log = tmp_path / "ssh.jsonl"
    result = subprocess.run(["sh", str(deploy), "a" * 40, *flags], env={**os.environ, "NAS_JUMP": "mac-mini-cc", "NAS_HOST": "nas-ci",
        "PATH": f"{scripts}:{os.environ['PATH']}", "TEST_MODE": mode,
        "TEST_MARKER": str(marker), "TEST_SSH_LOG": str(ssh_log), "TEST_HOSTLOG": str(tmp_path / "host.log"),
        "TEST_DOCKER_STATE": str(tmp_path / "docker-state"), **(env_updates or {})}, capture_output=True, text=True, timeout=10, check=False)
    return result, marker, [json.loads(line) for line in ssh_log.read_text().splitlines()]


@pytest.mark.parametrize("mode", ["nas-unreachable", "nas-unreachable-1", "disconnect", "signal", "already-current", "remote-failure", "rollback-failed", "host-signal", "host-signal-after-cutover", "deploy-ok-no-marker", "start-failed", "cat-fails-after-ok"])
def test_formal_deploy_preserves_guard_on_uncertain_exit_and_releases_on_noop(tmp_path, mode):
    result, marker, calls = formal_deploy_run(tmp_path, mode)
    if mode.startswith("nas-unreachable"):
        assert calls == [["-J", "mac-mini-cc", "nas-ci", "true"]]
        assert result.returncode == 1 and "NAS_CONNECTION_FAILED" in result.stderr
        assert not marker.exists() and "DEPLOY_GUARD_RETAINED" not in result.stderr
    elif mode == "already-current":
        assert result.returncode == 0, result.stderr
        assert not marker.exists()
    elif mode == "remote-failure":
        # The host log shows the ROLLBACK_OK marker, so — and only so — the outer script may say
        # "rolled back"; the guard is released because the remote run provably finished.
        assert result.returncode == 1 and "DEPLOY_FAILED" in result.stderr, result.stderr
        assert "rolled back to" in result.stderr
        assert "ROLLBACK_FAILED" not in result.stderr and "interrupted by signal" not in result.stderr
        assert not marker.exists() and "DEPLOY_GUARD_RETAINED" not in result.stderr
    elif mode == "rollback-failed":
        # The remote run finished (exit 1) with an incomplete restore: named for what the host
        # reported — never "rolled back" — and the guard stays for a real read-back.
        assert result.returncode == 1, result.stderr
        assert "DEPLOY_FAILED_ROLLBACK_FAILED" in result.stderr
        assert "rolled back to" not in result.stderr
        assert marker.is_dir() and "DEPLOY_GUARD_RETAINED" in result.stderr
    elif mode == "host-signal":
        # The unit exited 143 after its own rollback; the read-back log proves the remote work
        # ended, so the guard is released even though the local exit code is a signal code.
        assert result.returncode == 143, result.stderr
        assert "interrupted by signal" in result.stderr and "rollback" in result.stderr
        assert not marker.exists() and "DEPLOY_GUARD_RETAINED" not in result.stderr
    elif mode == "host-signal-after-cutover":
        # A signal after the committed cutover: the switch stands and no rollback ran — reported
        # as neither success nor "rolled back", non-zero to block any outer chain; the terminal
        # marker in the log proves the unit ended, so the guard is released.
        assert result.returncode == 143, result.stderr
        assert "DEPLOY_FAILED_AFTER_CUTOVER" in result.stderr and "aaaaaaa is serving" in result.stderr
        assert "rolled back" not in result.stderr and "DEPLOY_OK" not in result.stderr
        assert not marker.exists() and "DEPLOY_GUARD_RETAINED" not in result.stderr
    elif mode == "deploy-ok-no-marker":
        # Exit 0 without DEPLOY_OK in the log is not a success: the marker stays for inspection.
        assert result.returncode == 1, result.stderr
        assert "DEPLOY_RESULT_UNREAD" in result.stderr and "systemctl show" in result.stderr
        assert marker.is_dir() and "DEPLOY_GUARD_RETAINED" in result.stderr
    elif mode == "start-failed":
        # The unit exited without ever writing an outcome (never started or died before the
        # switch): no "rolled back" claim, and with no terminal marker the guard stays too —
        # retained-by-default until a real read-back.
        assert result.returncode == 1, result.stderr
        assert "DEPLOY_RESULT_UNREAD" in result.stderr and "rolled back" not in result.stderr
        assert marker.is_dir() and "DEPLOY_GUARD_RETAINED" in result.stderr
    elif mode == "disconnect":
        # 255 stays unknown even though the log fetch returned stage output: the unit may still
        # be running, and nothing is killed or cleared automatically.
        assert result.returncode == 255, result.stderr
        assert "DEPLOY_UNKNOWN" in result.stderr and "systemctl status" in result.stderr
        assert "up -d trader-runtime-green" in result.stdout  # the partial log was still printed
        assert marker.is_dir() and "DEPLOY_GUARD_RETAINED" in result.stderr
    elif mode == "cat-fails-after-ok":
        # Red-proof regression: the deploy unit succeeded, but the read-back connection died
        # right after emitting the full DEPLOY_OK line. A non-zero read status voids every
        # marker — no success, no maintenance install, no guard release.
        assert result.returncode == 1, result.stderr
        assert "DEPLOY_RESULT_UNREAD" in result.stderr and "read-back ssh itself failed" in result.stderr
        assert "systemctl show" in result.stderr
        assert not any("maintain_update_host.py" in argv[-1] and "--nas" not in argv[-1] for argv in calls), calls
        assert marker.is_dir() and "DEPLOY_GUARD_RETAINED" in result.stderr
    else:
        assert result.returncode != 0
        assert marker.is_dir()
        assert "DEPLOY_GUARD_RETAINED" in result.stderr


def test_formal_deploy_runs_host_script_under_systemd_run(tmp_path):
    import re
    # Two invocations: the unit and log identities must be unique per run, never reattaching to
    # an older unit or overwriting its evidence log.
    units = []
    for sub in (tmp_path, tmp_path / "second"):
        result, marker, calls = formal_deploy_run(sub, "deploy-ok")
        assert result.returncode == 0, result.stderr
        assert not marker.exists()
        deploy_calls = [argv[-1] for argv in calls if "systemd-run" in argv[-1]]
        assert len(deploy_calls) == 1
        command = deploy_calls[0]
        # The connection can die without stopping the deploy: no --pipe/--pty ties the unit's
        # stdio to the ssh session.
        assert "--wait" in command and "--pipe" not in command and "--pty" not in command
        unit = re.search(r"--unit=(\S+)", command).group(1)
        assert re.fullmatch(r"ai-trader-deploy-a{40}-\d{8}T\d{6}-\d+", unit), unit
        units.append(unit)
        invocation = unit[len("ai-trader-deploy-" + "a" * 40 + "-"):]
        # Output appends to a per-invocation host log (never truncates an earlier evidence log).
        assert f"--property=StandardOutput=append:/opt/ai-trader/deploy-{'a' * 40}-{invocation}.log" in command
        assert f"--property=StandardError=append:/opt/ai-trader/deploy-{'a' * 40}-{invocation}.log" in command
        assert "--property=WorkingDirectory=/opt/ai-trader" in command
        # The unit's output file is root-only: the log carries deploy output, not a world-readable one.
        assert "--property=UMask=0077" in command
        # No stop timeout may SIGKILL the unit mid-rollback (Ubuntu noble systemd: infinity
        # disables the stop timeout; verified on a real user transient unit).
        assert "--property=TimeoutStopSec=infinity" in command
        assert command.endswith("/bin/sh /opt/ai-trader/deploy-aaaaaaa.sh")
        # The result is classified from the log the outer script reads back, not the pipe.
        fetch = [argv for argv in calls if argv[-1].startswith("sudo cat /opt/ai-trader/deploy-")]
        assert len(fetch) == 1 and "DEPLOY_OK" in result.stdout
    assert len(set(units)) == 2


# --- nas-registry image identity across runs (#667) ---------------------------------------------
# The stateful stand-in models the docker stores on the NAS, the loopback registry and the host
# across invocations: pushes fill the NAS and the registry, pulls populate the host's full-sha
# refs with the registry's content, tags alias the host entries, and the digest/identity reads
# answer from the same state — so a second run of the real outer script sees exactly what the
# first one left behind. Nothing here executes remote strings.

REGISTRY_STUB = '''#!__PYTHON__
import json, os, pathlib, sys
with open(os.environ["TEST_SSH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
mode = os.environ["TEST_MODE"]
state = json.loads(pathlib.Path(os.environ["TEST_REGISTRY_STATE"]).read_text())
sha, short = "a" * 40, "a" * 7
nas, reg = "127.0.0.1:15000", "127.0.0.1:5000"
argv = sys.argv[1:]
is_nas = bool(argv) and argv[0] == "-J"
command = argv[-1]
marker = pathlib.Path(os.environ["TEST_MARKER"])
hostlog = pathlib.Path(os.environ["TEST_HOSTLOG"])
def save():
    pathlib.Path(os.environ["TEST_REGISTRY_STATE"]).write_text(json.dumps(state))
if command == "true":
    sys.exit(0)
if "flock -n" in command:
    if marker.exists():
        sys.exit(1)
    marker.mkdir(); sys.exit(0)
if "rmdir" in command:
    if marker.exists():
        marker.rmdir()
    sys.exit(0)
if "docker image ls" in command:
    # The existence listing: prints the image id for an existing ref, empty output rc 0 when the
    # ref is absent, and dies 255 whenever the read itself fails (nas-read-fails mode).
    if mode == "nas-read-fails" and (is_nas or command.split("reference=", 1)[1].startswith(nas)):
        sys.exit(255)
    ref = command.split("reference=", 1)[1]
    entry = state["nas" if is_nas else "host"].get(ref)
    print(entry["id"] if entry else "")
    sys.exit(0)
if "docker image inspect" in command and "--format" in command:
    ref = command.split()[-1]
    if "{.Id}" in command:
        entry = state["nas" if is_nas else "host"].get(ref)
    else:
        if mode == "nas-read-fails" and (is_nas or ref.startswith(nas)):
            sys.exit(255)
        entry = state["nas" if is_nas else "host"].get(ref)
    if entry is None:
        sys.exit(1)
    print(f"{ref.rsplit(':', 1)[0]}@{entry['digest']}" if "RepoDigests" in command else entry["id"])
    sys.exit(0)
if f"sudo docker image inspect ai-trader-runtime:{short} ai-trader-sidecar:{short}" in command:
    ok = f"ai-trader-runtime:{short}" in state["host"] and f"ai-trader-sidecar:{short}" in state["host"]
    sys.exit(0 if ok else 1)
if "docker push" in command:
    # poison-push simulates a corrupt landing: the NAS keeps what it built, but the registry
    # (and so every host pull) serves a runtime that is NOT what the NAS holds.
    poison = mode == "poison-push"
    for repo in ("runtime", "sidecar"):
        state["nas"][f"{nas}/ai-trader-{repo}:{sha}"] = {"id": f"id-nas-{repo}", "digest": f"sha256:ok-{repo}"}
        digest = "sha256:bad-runtime" if poison and repo == "runtime" else f"sha256:ok-{repo}"
        state["registry"][f"{reg}/ai-trader-{repo}:{sha}"] = digest
    save(); sys.exit(0)
if command.startswith("sudo docker pull"):
    ok = True
    for seg in command.split(" && "):
        ref = seg.split()[-1]
        digest = state["registry"].get(ref)
        if digest is None:
            ok = False; break
        state["host"][ref] = {"id": f"id-{digest}", "digest": digest}
    save(); sys.exit(0 if ok else 1)
if command.startswith("sudo docker tag"):
    for seg in command.split(" && "):
        _, _, _, ref, target = seg.split()
        state["host"][target] = dict(state["host"][ref])
    save(); sys.exit(0)
if "readlink -f" in command:
    if mode == "current-unreadable":
        sys.exit(255)
    print("/opt/ai-trader/releases/" + ("b" if mode != "already" else "a") * 40); sys.exit(0)
if "maintain_update_host.py --nas" in command:
    sys.exit(1 if mode == "retention-fails" else 0)
if "systemd-run" in command:
    state["deploys"] = state.get("deploys", 0) + 1
    hostlog.write_text("DEPLOY_OK\\n")
    save(); sys.exit(0)
if command.startswith("sudo cat "):
    if hostlog.exists():
        sys.stdout.write(hostlog.read_text())
    sys.exit(0)
sys.exit(0)
'''


def registry_deploy_run(tmp_path, mode, seed):
    import subprocess
    import sys
    scripts = tmp_path / "bin"
    scripts.mkdir(parents=True, exist_ok=True)
    for name, body in {
        "node": "#!/bin/sh\necho 1.6.11\n",
        "git": '#!/bin/sh\necho \'{"version":"1.6.11"}\'\n',
        "ssh": REGISTRY_STUB.replace("__PYTHON__", sys.executable),
    }.items():
        path = scripts / name
        path.write_text(body)
        path.chmod(0o755)
    state_path = tmp_path / "registry-state.json"
    state_path.write_text(json.dumps(seed))
    # The image-store state carries across invocations; the call log is per run.
    ssh_log = tmp_path / "ssh.jsonl"
    ssh_log.unlink(missing_ok=True)
    result = subprocess.run(
        ["sh", str(Path(__file__).resolve().parents[1] / "deploy/vps/deploy-release.sh"), "a" * 40],
        env={**os.environ, "PATH": f"{scripts}:{os.environ['PATH']}", "NAS_JUMP": "mac-mini-cc", "NAS_HOST": "nas-ci",
             "TEST_MODE": mode, "TEST_MARKER": str(tmp_path / "deploying"), "TEST_SSH_LOG": str(ssh_log),
             "TEST_HOSTLOG": str(tmp_path / "host.log"), "TEST_REGISTRY_STATE": str(state_path)},
        capture_output=True, text=True, timeout=10, check=False)
    calls = [json.loads(line)[-1] for line in ssh_log.read_text().splitlines()]
    return result, json.loads(state_path.read_text()), calls


def empty_seed():
    # Nothing built yet: the first run's NAS build/push populates the stores per its mode.
    return {"nas": {}, "registry": {}, "host": {}, "deploys": 0}


def verified_cache_seed(*, short_id=None):
    # A complete, verified cache: full-sha refs on the host and (unless short_id overrides) short
    # tags naming the very same images, digests agreeing with the NAS.
    sha = "a" * 40
    nas, reg = "127.0.0.1:15000", "127.0.0.1:5000"
    host = {}
    for repo in ("runtime", "sidecar"):
        host[f"{reg}/ai-trader-{repo}:{sha}"] = {"id": f"id-ok-{repo}", "digest": f"sha256:ok-{repo}"}
        host[f"ai-trader-{repo}:{sha[:7]}"] = {"id": short_id or f"id-ok-{repo}", "digest": f"sha256:ok-{repo}"}
    return {"nas": {f"{nas}/ai-trader-{repo}:{sha}": {"id": f"id-nas-{repo}", "digest": f"sha256:ok-{repo}"}
                    for repo in ("runtime", "sidecar")},
            "registry": {f"{reg}/ai-trader-{repo}:{sha}": f"sha256:ok-{repo}" for repo in ("runtime", "sidecar")},
            "host": host, "deploys": 0}


def test_formal_deploy_digest_mismatch_leaves_no_short_tags_and_retry_cannot_bypass(tmp_path):
    # Red proof of the #667 defect's fix: the digest gate must run BEFORE any new short tag is
    # written. A corrupt registry landing fails the first run with only full-sha refs on the host,
    # and an unchanged retry rebuilds and re-verifies (never reuses its own leftovers) and still
    # refuses as long as the corruption stands.
    result, state, calls = registry_deploy_run(tmp_path / "first", "poison-push", empty_seed())
    assert result.returncode == 1 and "IMAGE_DIGEST_MISMATCH" in result.stderr, result.stderr
    assert "ai-trader-runtime:aaaaaaa" not in state["host"], "no short tag may exist after a failed verification"
    assert state["deploys"] == 0 and not (tmp_path / "first/deploying").exists()
    verified = sum("RepoDigests" in call for call in calls)
    assert verified == 2, calls  # the mismatching repo's digests were read on both ends first
    result, state, calls = registry_deploy_run(tmp_path / "first", "poison-push", state)
    assert result.returncode == 1 and "IMAGE_DIGEST_MISMATCH" in result.stderr, result.stderr
    assert "ai-trader-runtime:aaaaaaa" not in state["host"] and state["deploys"] == 0
    # The retry really rebuilt and re-verified instead of taking a cache shortcut (no short tags
    # existed): the NAS build/push ran again and the digests were read on both ends once more.
    assert sum("docker push" in call for call in calls) == 1, calls
    assert sum("RepoDigests" in call for call in calls) == 2, calls


def test_formal_deploy_retry_rebuilds_pushes_and_verifies(tmp_path):
    # The corrupt landing was transient: a plain formal retry (no registry cleanup, no manual
    # recovery step) rebuilds on the NAS, re-pushes, re-pulls, verifies both digests and only
    # then writes the short tags and deploys — the healed chain through the official flow.
    result, state, _ = registry_deploy_run(tmp_path, "poison-push", empty_seed())
    assert result.returncode == 1 and "IMAGE_DIGEST_MISMATCH" in result.stderr
    result, state, calls = registry_deploy_run(tmp_path, "retry-ok", state)
    assert result.returncode == 0, result.stderr
    assert "DEPLOY_OK" in result.stdout and state["deploys"] == 1
    assert any("docker push" in call for call in calls), calls
    # The short tags now name exactly the verified full-sha images.
    sha, reg = "a" * 40, "127.0.0.1:5000"
    for repo in ("runtime", "sidecar"):
        assert state["host"][f"ai-trader-{repo}:aaaaaaa"]["id"] == state["host"][f"{reg}/ai-trader-{repo}:{sha}"]["id"]


def test_formal_deploy_verified_cache_reverifies_identity_on_every_reuse(tmp_path):
    # A legitimate cache is still verified on every run (digests on both ends plus short-tag
    # identity), and a verified reuse neither builds nor pulls — the tags really are the cache.
    result, state, calls = registry_deploy_run(tmp_path / "one", "cache-ok", verified_cache_seed())
    assert result.returncode == 0 and "DEPLOY_OK" in result.stdout and state["deploys"] == 1, result.stderr
    assert not any("docker pull" in call or "docker push" in call for call in calls), calls
    assert sum("RepoDigests" in call for call in calls) == 4, calls  # two repos × two ends
    assert sum("{{.Id}}" in call for call in calls) == 4, calls  # short and full ref per repo
    # A reused-cache rerun still reclaims the NAS (#668): retention is not tied to the build branch.
    assert any("maintain_update_host.py --nas" in call for call in calls), calls
    result, state, calls = registry_deploy_run(tmp_path / "one", "cache-ok", state)
    assert result.returncode == 0 and state["deploys"] == 2, result.stderr
    assert sum("RepoDigests" in call for call in calls) == 4 and sum("{{.Id}}" in call for call in calls) == 4, calls
    assert not any("docker pull" in call for call in calls), calls


def test_formal_deploy_cache_short_tag_naming_another_image_is_reverified_and_retagged(tmp_path):
    # The short tags exist but name a different image than the full-sha refs (the residue of an
    # old failed flow): reuse is refused and the formal build path rebuilds on the NAS, re-pushes,
    # re-verifies and rewrites the short tags onto the verified images before deploying.
    result, state, calls = registry_deploy_run(tmp_path, "cache-wrong-id",
                                               verified_cache_seed(short_id="id-unrelated"))
    assert result.returncode == 0 and "DEPLOY_OK" in result.stdout and state["deploys"] == 1, result.stderr
    assert "did not verify; rebuilding and re-verifying" in result.stderr, result.stderr
    assert any("docker push" in call for call in calls) and any("docker pull" in call for call in calls), calls
    sha, reg = "a" * 40, "127.0.0.1:5000"
    for repo in ("runtime", "sidecar"):
        assert state["host"][f"ai-trader-{repo}:aaaaaaa"]["id"] == state["host"][f"{reg}/ai-trader-{repo}:{sha}"]["id"]


def test_formal_deploy_cache_with_missing_nas_fullref_rebuilds(tmp_path):
    # The short tags still exist but the NAS no longer holds the full-sha ref (its image store was
    # pruned): the existence listing succeeds empty, so the cache is simply not verifiable — NOT a
    # hard stop — and the formal entry rebuilds, pushes, pulls and verifies back to a deploy.
    seed = verified_cache_seed()
    sha, nas = "a" * 40, "127.0.0.1:15000"
    for repo in ("runtime", "sidecar"):
        del seed["nas"][f"{nas}/ai-trader-{repo}:{sha}"]
    result, state, calls = registry_deploy_run(tmp_path, "nas-ref-missing", seed)
    assert result.returncode == 0 and "DEPLOY_OK" in result.stdout and state["deploys"] == 1, result.stderr
    assert "IMAGE_DIGEST_READ_FAILED" not in result.stderr and "IMAGE_DIGEST_MISMATCH" not in result.stderr, result.stderr
    assert "did not verify; rebuilding and re-verifying" in result.stderr, result.stderr
    assert any("docker push" in call for call in calls), calls
    reg = "127.0.0.1:5000"
    for repo in ("runtime", "sidecar"):
        assert state["host"][f"ai-trader-{repo}:aaaaaaa"]["id"] == state["host"][f"{reg}/ai-trader-{repo}:{sha}"]["id"]


def test_formal_deploy_cache_with_missing_host_fullref_rebuilds(tmp_path):
    # Same residue with the host side missing its full-sha refs (e.g. an interrupted pull):
    # also not verifiable, never a hard stop, and the rebuild restores the refs before deploying.
    seed = verified_cache_seed()
    sha, reg = "a" * 40, "127.0.0.1:5000"
    for repo in ("runtime", "sidecar"):
        del seed["host"][f"{reg}/ai-trader-{repo}:{sha}"]
    result, state, calls = registry_deploy_run(tmp_path, "host-ref-missing", seed)
    assert result.returncode == 0 and "DEPLOY_OK" in result.stdout and state["deploys"] == 1, result.stderr
    assert "IMAGE_DIGEST_READ_FAILED" not in result.stderr and "IMAGE_DIGEST_MISMATCH" not in result.stderr, result.stderr
    assert "did not verify; rebuilding and re-verifying" in result.stderr, result.stderr
    assert any("docker push" in call for call in calls), calls
    for repo in ("runtime", "sidecar"):
        assert state["host"][f"ai-trader-{repo}:aaaaaaa"]["id"] == state["host"][f"{reg}/ai-trader-{repo}:{sha}"]["id"]


def test_formal_deploy_digest_read_ssh_failure_is_explicit_not_an_empty_digest(tmp_path):
    # A digest read that dies with ssh 255 must be named for what it is and stop the release —
    # it may not be swallowed into an empty digest inside the || list (the old behavior), and
    # the release stops right there instead of building on an unverifiable cache.
    result, state, calls = registry_deploy_run(tmp_path, "nas-read-fails", verified_cache_seed())
    assert result.returncode == 1, result.stderr
    assert "IMAGE_DIGEST_READ_FAILED" in result.stderr and "IMAGE_DIGEST_MISMATCH" not in result.stderr, result.stderr
    assert state["deploys"] == 0 and not (tmp_path / "deploying").exists()
    assert not any("docker push" in call or "docker pull" in call for call in calls), calls


@pytest.mark.parametrize("mode", ["retry-ok", "retention-fails", "current-unreadable"])
def test_formal_deploy_runs_nas_retention_after_verified_push_without_blocking_release(tmp_path, mode):
    # #668: after the images verified on the host, the NAS runs this release's own maintenance
    # owner protecting the new sha and the host's current release (the rollback after the
    # switch). Its failure, or an unreadable current release (then it is not run at all, since
    # protection would be unknown), never stops the release.
    result, state, calls = registry_deploy_run(tmp_path, mode, empty_seed())
    if mode == "current-unreadable":
        # The same readlink also decides the deploy's previous release; with it unreadable the
        # release stops later as before — the point here is that NAS retention never ran blind.
        assert "NAS_RETENTION_SKIPPED" in result.stderr, result.stderr
        assert not any("maintain_update_host.py --nas" in call for call in calls), calls
        return
    assert result.returncode == 0 and "DEPLOY_OK" in result.stdout and state["deploys"] == 1, result.stderr
    retention = [call for call in calls if "maintain_update_host.py --nas" in call]
    assert retention == [f"python3 /home/lizhengyi/ai-trader-builds/{'a' * 40}/scripts/maintain_update_host.py "
                         f"--nas --apply --protect {'a' * 40} --protect {'b' * 40}"], calls
    order = [i for i, call in enumerate(calls) if "RepoDigests" in call or "maintain_update_host.py --nas" in call]
    assert "maintain_update_host.py --nas" in calls[order[-1]], "retention runs only after verification"
    assert ("NAS_RETENTION_FAILED" in result.stderr) == (mode == "retention-fails"), result.stderr


@pytest.mark.parametrize("flags,env_updates,jump,builder", [
    ([], {}, "mac-mini-cc", "nas-ci"),
    (["--nas-jump", "other-jump"], {}, "other-jump", "nas-ci"),
    (["--nas-jump", "none"], {}, None, "nas-ci"),
    ([], {"NAS_JUMP": ""}, None, "nas-ci"),
    (["--build-host", "nas-ci"], {}, None, "nas-ci"),
    (["--build-host", "other-builder"], {}, None, "other-builder"),
    (["--build-on-host"], {"NAS_HOST": "tencent-ai-trader"}, None, "tencent-ai-trader"),
])
def test_formal_deploy_ssh_routes_follow_mode_not_alias(tmp_path, flags, env_updates, jump, builder):
    result, marker, calls = formal_deploy_run(tmp_path, "route", flags, env_updates)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    nas_mode = not flags or flags[0] == "--nas-jump"
    for argv in calls:
        target = argv[-2]
        prefix = ["-J", jump, builder] if nas_mode and jump and target == builder else [target]
        assert argv[:-1] == prefix
    builder_commands = [argv[-1] for argv in calls if argv[-2] == builder]
    assert any("mkdir" in command for command in builder_commands)
    assert any("tar -x" in command for command in builder_commands)
    assert any("docker build" in command for command in builder_commands)
    if nas_mode:
        assert calls[0][-1] == "true"
        assert sum("--format" in command for command in builder_commands) == 2
        assert any("tail -n +6" in command for command in builder_commands)
    elif flags[0] == "--build-host":
        assert any("docker save" in command for command in builder_commands)


def test_remote_save_same_nas_alias_does_not_depend_on_jump(tmp_path):
    result, marker, calls = formal_deploy_run(tmp_path, "jump-fails", ["--build-host", "nas-ci"])
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert all("-J" not in argv for argv in calls)


# --- the generated host-side deploy script (#715) ----------------------------------------------
# The outer ssh mock above proves the local guard handling, not the host script's own control
# flow. These tests generate the real host script through deploy-release.sh --dry-run and run it
# end to end inside an isolated fixture root: docker/nginx/curl/sleep are recording stubs that
# can deliver a signal to the host shell at a chosen stage. Only absolute path constants
# (/opt/ai-trader, /etc/nginx, the docker volume) are rewritten into the fixture; the trap,
# rollback and cutover logic under test is the generated text verbatim. The docker stub records
# which release each started service runs (from the first -f path) into $TEST_STATE, and the
# curl stub answers a slot's /healthz from that record — so assertions are on the stack the
# fixture believes it is running, not on log strings alone.

HEALTHZ_JSON = ('{"status": "ok", "version": "1.6.11", "commit": "%s", "adapter": "native-agent",'
                ' "mode": "native", "liveTrading": "disabled", "authMode": "owner-password"}')


def host_deploy_script(tmp_path):
    import subprocess
    scripts = tmp_path / "gen-bin"
    scripts.mkdir()
    for name, body in {
        "node": "#!/bin/sh\necho 1.6.11\n",
        "git": '#!/bin/sh\necho \'{"version":"1.6.11"}\'\n',
        # The dry run reads its service plan from the host; here there is no host to reach.
        "ssh": "#!/bin/sh\nexit 255\n",
    }.items():
        path = scripts / name
        path.write_text(body)
        path.chmod(0o755)
    deploy = Path(__file__).resolve().parents[1] / "deploy/vps/deploy-release.sh"
    result = subprocess.run(["sh", str(deploy), "a" * 40, "--dry-run"],
        env={**os.environ, "PATH": f"{scripts}:{os.environ['PATH']}"}, capture_output=True, text=True, timeout=30, check=True)
    lines = result.stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if "write /opt/ai-trader/deploy-aaaaaaa.sh:" in line)
    end = next(i for i, line in enumerate(lines) if "systemd-run --wait --unit=" in line)
    return "\n".join(lines[start + 1:end]) + "\n"


def host_deploy_fixture(tmp_path, *, legacy=False, sidecar_changed=False, overlays=False,
                        migration_layout=False, new_overlays=False):
    root = tmp_path / "host"
    prev, new = "b" * 40, "a" * 40
    base = root / "opt"
    previous_compose = base / "releases" / prev / "deploy/vps/docker-compose.yml"
    previous_compose.parent.mkdir(parents=True)
    # A "  trader-runtime:" service line marks the pre-blue-green layout the rollback restores whole.
    previous_compose.write_text("services:\n  trader-runtime:\n    image: ai-trader-runtime:old\n" if legacy
        else "services:\n  trader-runtime-blue:\n    image: ai-trader-runtime:old\n")
    # The previous release's images.yml names every service it started; overlays enabled only
    # for this deploy (new_overlays) are absent from it.
    previous_images = "services:\n  trader-runtime-blue:\n    image: ai-trader-runtime:old\n  tqkq-sidecar:\n    image: ai-trader-sidecar:old\n"
    if overlays and not new_overlays:
        previous_images += "  tqkq-pool:\n    image: ai-trader-sidecar:old\n  paper-engine:\n    image: ai-trader-sidecar:old\n"
    (previous_compose.parent / "images.yml").write_text(previous_images)
    if sidecar_changed:
        for release, content in ((prev, "old\n"), (new, "new\n")):
            sidecar = base / "releases" / release / "services/tqkq-sidecar"
            sidecar.mkdir(parents=True)
            (sidecar / "sidecar.py").write_text(content)
    (base / "releases" / new / "deploy/vps").mkdir(parents=True, exist_ok=True)
    # The release tree ships the service plan the host script runs (it drives the docker stub).
    shutil.copy(Path(__file__).resolve().parents[1] / "deploy/vps/service_plan.py",
                base / "releases" / new / "deploy/vps/service_plan.py")
    for release in (prev, new):
        # The rollback reads the previous release's version from its own package.json.
        (base / "releases" / release / "package.json").write_text('{"version": "1.6.11"}\n')
    if legacy or migration_layout:
        (base / "releases" / new / "deploy/vps/nginx-ai-trader-runtime.conf").write_text("server{listen 443;}\n")
    (base / "backups").mkdir()
    (base / "secrets").mkdir()
    (base / "secrets/voice-realtime-key").write_text("key")
    env_text = f"AI_TRADER_BUILD_SHA={prev}\n"
    if overlays:
        # The optional stack: tqkq-pool lives in the sidecar netns, paper-engine joins it.
        env_text += "AI_TRADER_TQKQ_POOL=1\nAI_TRADER_PAPER_ENGINE=1\n"
    (base / ".env.production").write_text(env_text)
    (base / "current").symlink_to(base / "releases" / prev)
    nginx_root = root / "etc-nginx"
    (nginx_root / "conf.d").mkdir(parents=True)
    # Pre-migration the runtime was proxied straight from the conf.d file; the migration backs it
    # up before overwriting, so the legacy fixture needs that original template too.
    (nginx_root / "conf.d/ai-trader-runtime.conf").write_text(
        "location / { proxy_pass http://127.0.0.1:18000; }\n" if legacy or migration_layout
        else "include /etc/nginx/ai-trader-runtime-upstream.conf;\n")
    if not legacy and not migration_layout:
        (nginx_root / "ai-trader-runtime-upstream.conf").write_text("upstream ai_trader_runtime { server 127.0.0.1:18000; }\n")
    (nginx_root / "nginx.conf").write_text("worker_processes 1;\nworker_shutdown_timeout 8s;\n")
    volumes = root / "var-lib-docker/volumes/ai-trader-production_ai-trader-cloud-data/_data"
    volumes.mkdir(parents=True)
    import sqlite3
    connection = sqlite3.connect(volumes / "ai-trader.sqlite3")
    connection.execute("create table sessions (id integer)")
    connection.execute("insert into sessions values (1)")
    connection.commit()
    connection.close()
    return root, prev, new


def host_deploy_stubs():
    return {
        # Signals are delivered with kill "$PPID": every stub runs as a direct child of the host
        # shell, so the trap under test fires exactly where the real docker/nginx/sleep would be.
        # up/rm also maintain $TEST_STATE/svc-<service> = the release whose files the command ran
        # with (the first -f path), so the curl stub and the tests can see which image each
        # service really ended up on instead of trusting the script's own log lines.
        "docker": '''#!/bin/sh
printf 'docker %s\\n' "$*" >> "$TEST_LOG"
last=; for a in "$@"; do last=$a; done
case "$*" in
  "ps -aq"*) [ -n "${TEST_LEGACY_ID:-}" ] && echo "$TEST_LEGACY_ID"; exit 0 ;;
  "image inspect"*) exit 0 ;;
  # The service plan's reads: a container per running service (the stack state), each stamped
  # with its compose config hash h-<service>.
  "ps -q"*)
    svc=${last#label=com.docker.compose.service=}
    [ -f "$TEST_STATE/svc-$svc" ] && echo "id-$svc"
    exit 0 ;;
  "inspect --format"*)
    printf '{"Image": "ai-trader-sidecar:old", "Labels": {"com.docker.compose.config-hash": "h-%s"}}\\n' "${last#id-}"
    exit 0 ;;
  # The rollback's paper checkpoint gate (one-off --checkpoint-db on the paper volume).
  "run --rm"*"--checkpoint-db"*)
    [ -z "${TEST_PAPER_CHECKPOINT_FAIL:-}" ] || exit 1
    exit 0 ;;
esac
[ "$1" = compose ] || exit 0
case "$*" in
  # This release's definitions: unchanged (h-<service>) unless TEST_DEFINITION_CHANGED names it.
  *" config --services"*)
    printf 'tqkq-sidecar\\ntrader-runtime-blue\\ntrader-runtime-green\\nrsshub-tunnel\\n'
    case "$*" in *docker-compose.accounts.yml*) echo tqkq-sidecar-2 ;; esac
    case "$*" in *docker-compose.pool.yml*) echo tqkq-pool ;; esac
    case "$*" in *docker-compose.paper.yml*) echo paper-engine ;; esac
    exit 0 ;;
  *" config --hash "*)
    if [ "$last" = "${TEST_DEFINITION_CHANGED:-}" ]; then echo "$last h-changed"; else echo "$last h-$last"; fi
    exit 0 ;;
esac
if [ -n "${TEST_RESIGNAL:-}" ]; then
  case "$*" in *"up -d"*"trader-runtime-blue"*) kill -"${TEST_SIGNAL:-TERM}" "$PPID" ;; esac
fi
if [ -n "${TEST_FAIL_ROLLBACK:-}" ]; then
  case "$*" in *"up -d"*"trader-runtime-blue"*) exit 1 ;; esac
fi
if [ -n "${TEST_SIGNAL_UP_SVC:-}" ]; then
  case "$*" in *"up -d"*"${TEST_SIGNAL_UP_SVC}"*) kill -"${TEST_SIGNAL:-TERM}" "$PPID" ;; esac
fi
if [ -n "${TEST_FAIL_ROLLBACK_SIDE:-}" ]; then
  # Only the restore from the previous release fails (its -f paths precede "up" on the command
  # line, so the three markers are matched independently); the deploy-time recreate still runs.
  case "$*" in *"up -d"*) case "$*" in *"tqkq-sidecar"*) case "$*" in *"$TEST_PREV_SHA"*)
    exit 1 ;; esac ;; esac ;; esac
fi
if [ -n "${TEST_FAIL_SIDECAR:-}" ]; then
  case "$*" in *"up -d"*"tqkq-sidecar"*) exit 1 ;; esac
fi
if [ -n "${TEST_FAIL_ROLLBACK_IDLE:-}" ]; then
  case "$*" in *"rm"*"trader-runtime-green"*) exit 1 ;; esac
fi
if [ -n "${TEST_FAIL_CONSUMER_RM:-}" ]; then
  # The rollback's consumer removal is the only rm naming both slots at once (idle first).
  case "$*" in
    *"rm -sf"*"trader-runtime-blue"*"trader-runtime-green"*|*"rm -sf"*"trader-runtime-green"*"trader-runtime-blue"*) exit 1 ;;
  esac
fi
case "$*" in
  *" exec -T "*)
    # member_gate's in-container health check: the service is the first non-flag word after
    # exec. Its /healthz payload is simulated from the fixture stack state plus knobs, and the
    # generated python assertion runs for real against that payload with urllib patched to it —
    # so a wrong service, an unavailable member or a broken generated check all fail here.
    svc=; take=0
    for a in "$@"; do
      if [ "$take" = 1 ]; then
        case "$a" in -*) continue ;; esac
        svc=$a; break
      fi
      [ "$a" = exec ] && take=1
    done
    [ -n "$svc" ] || exit 1
    [ -f "$TEST_STATE/svc-$svc" ] || exit 1
    [ "${TEST_MEMBER_FAIL_SVC:-}" = "$svc" ] && exit 1
    code=; prevarg=
    for a in "$@"; do [ "$prevarg" = -c ] && code=$a; prevarg=$a; done
    [ -n "$code" ] || exit 1
    TEST_STUB_SVC="$svc" python3 - "$code" <<'PY' || exit 1
import io, json, os, sys
code, svc = sys.argv[1], os.environ["TEST_STUB_SVC"]
wrong = bool(os.environ.get("TEST_MEMBER_WRONG_SERVICE"))
if svc == "tqkq-pool":
    payload = {"status": "ok", "pool": False} if wrong else {"status": "ok", "pool": True}
else:
    service = {"paper-engine": "ai-trader-paper-engine"}.get(svc, "ai-trader-tqkq-readonly-sidecar")
    payload = {"service": "ai-trader-unexpected-service" if wrong else service,
               "status": os.environ.get("TEST_MEMBER_DATASTATUS", "normal"),
               "dataStatus": os.environ.get("TEST_MEMBER_DATASTATUS", "normal"),
               "liveTrading": "disabled"}
captured = {}
class Req:
    def __init__(self, url): self.url, self.headers = url, {}
    def add_header(self, key, value): self.headers[key] = value
def urlopen(req, timeout=None):
    captured.update(url=req.url, headers=req.headers, timeout=timeout)
    return io.BytesIO(json.dumps(payload).encode())
import urllib.request
urllib.request.Request, urllib.request.urlopen = Req, urlopen
exec(compile(code, "<member-gate>", "exec"), {"__name__": "__main__"})
auth = captured.get("headers", {}).get("Authorization", "")
token = open(os.environ["AI_TRADER_READONLY_SIDECAR_TOKEN_FILE"]).read().strip()
with open(os.environ["TEST_LOG"], "a") as log:
    log.write("member-auth %s bearer=%s url=%s\\n" % (svc, auth == "Bearer " + token, captured["url"]))
PY
    exit 0
    ;;
esac
case "$*" in
  *"rm -sf"*)
    for a in "$@"; do
      case "$a" in
        trader-runtime-blue|trader-runtime-green|tqkq-sidecar|tqkq-sidecar-2|tqkq-pool|paper-engine)
          rm -f "$TEST_STATE/svc-$a" 2>/dev/null || true ;;
      esac
    done
    exit 0
    ;;
esac
expect=0; sha=; services=; envfile=
for a in "$@"; do
  if [ "$expect" = 1 ]; then
    expect=0
    case "$a" in */releases/*/*) sha=${a#*/releases/}; sha=${sha%%/*} ;; esac
    continue
  fi
  if [ "$expect" = 2 ]; then
    expect=0; envfile=$a; continue
  fi
  case "$a" in
    -f) expect=1 ;;
    --env-file) expect=2 ;;
    trader-runtime-blue|trader-runtime-green|tqkq-sidecar|tqkq-sidecar-2|tqkq-pool|paper-engine) services="$services $a" ;;
  esac
done
whole=1
case "$*" in *--no-deps*) whole=0 ;; esac
if [ -z "$services" ] && [ "$whole" = 1 ]; then
  # A whole-project up (first migration, legacy rollback) recreates the slots and the sidecar,
  # plus the overlay members the env file enables.
  services=" trader-runtime-blue trader-runtime-green tqkq-sidecar"
  if [ -n "$envfile" ] && grep -q '^AI_TRADER_TQKQ_POOL=1$' "$envfile" 2>/dev/null; then
    services="$services tqkq-pool"
  fi
  if [ -n "$envfile" ] && grep -q '^AI_TRADER_PAPER_ENGINE=1$' "$envfile" 2>/dev/null; then
    services="$services paper-engine"
  fi
fi
[ -n "$services" ] || exit 0
# Every netns member joins the sidecar's namespace: its container must exist first. A whole-
# project up (or an up naming tqkq-sidecar itself) creates the namespace, so it is exempt —
# which is exactly why the rollback must restore the sidecar in its own earlier compose call.
case "$services" in
  *"tqkq-sidecar"*) ;;
  *)
    for s in $services; do
      case "$s" in
        trader-runtime-blue|trader-runtime-green|tqkq-sidecar-2|tqkq-pool|paper-engine)
          [ -f "$TEST_STATE/svc-tqkq-sidecar" ] || { echo "no sidecar netns for $s" >&2; exit 1; } ;;
      esac
    done
    ;;
esac
for s in $services; do printf '%s\\n' "$sha" > "$TEST_STATE/svc-$s"; done
exit 0
''',
        "nginx": '''#!/bin/sh
printf 'nginx %s\\n' "$*" >> "$TEST_LOG"
if [ "$1" = -s ] && [ "$2" = reload ] && [ "${TEST_STAGE:-}" = nginx-reload ]; then
  kill -"${TEST_SIGNAL:-TERM}" "$PPID"
fi
exit 0
''',
        # /healthz answers from the recorded stack state: the port maps to its slot service, and
        # the reported commit is the release that service was last brought up with — nothing
        # listening (removed slot) fails like a real refused connection.
        "curl": '''#!/bin/sh
printf 'curl %s\\n' "$*" >> "$TEST_LOG"
url=
for a in "$@"; do case "$a" in http://*) url=$a ;; esac; done
slot=blue
case "$url" in *18001*) slot=green ;; esac
if [ -n "${TEST_GATE_FAIL:-}" ] && [ "$slot" = green ]; then exit 1; fi
if [ -n "${TEST_GATE_ONCE:-}" ] && [ "$slot" = green ]; then
  n=$(cat "$TEST_CURL_COUNT" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$TEST_CURL_COUNT"
  [ "$n" -gt 0 ] || exit 1
fi
state="$TEST_STATE/svc-trader-runtime-$slot"
[ -f "$state" ] || exit 1
sha=$(cat "$state")
if [ -n "${TEST_ROLLBACK_HEALTH_FAIL:-}" ] && [ "$slot" = blue ] && [ "$sha" = "$TEST_PREV_SHA" ]; then exit 1; fi
printf '{"status": "ok", "version": "1.6.11", "commit": "%s", "adapter": "native-agent", "mode": "native", "liveTrading": "disabled", "authMode": "owner-password"}\\n' "$sha"
''',
        "sleep": '''#!/bin/sh
printf 'sleep %s\\n' "$*" >> "$TEST_LOG"
if [ "$1" = 10 ] && [ "${TEST_STAGE:-}" = drain ]; then kill -"${TEST_SIGNAL:-TERM}" "$PPID"; fi
if [ "$1" = 5 ] && [ "${TEST_STAGE:-}" = gate-sleep ]; then kill -"${TEST_SIGNAL:-TERM}" "$PPID"; fi
exit 0
''',
        # The gate's deadline is real clock time; each date call advances a fake clock 30 s so a
        # failing gate gives up after three instant polls instead of a real 60 s.
        "date": '''#!/bin/sh
case "$1" in +%s) ;; *) exec /bin/date "$@" ;; esac
n=$(cat "$TEST_DATE_COUNT" 2>/dev/null || echo 0)
echo $((n + 1)) > "$TEST_DATE_COUNT"
echo $((1757000000 + 30 * n))
''',
        # In-place edits differ between BSD and GNU sed: the production host runs GNU sed, the
        # dev Mac runs BSD, and GNU rejects `-i ''` outright (rc 2, file untouched — the red
        # proof from the NAS). Probe the real binary's own behavior instead of assuming the dev
        # platform: GNU sed answers --version, BSD sed errors on it.
        "sed": '''#!/bin/sh
if [ "$1" = "-i" ]; then
  shift
  if /usr/bin/sed --version >/dev/null 2>&1; then exec /usr/bin/sed -i "$@"; fi
  exec /usr/bin/sed -i '' "$@"
fi
exec /usr/bin/sed "$@"
''',
        "mv": '''#!/bin/sh
printf 'mv %s\\n' "$*" >> "$TEST_LOG"
if [ "$1" = "-Tf" ]; then
  shift; rm -f "$2"; /bin/mv "$@"; rc=$?
  # A signal right after the atomic cutover commits must keep the switch (env and current stay
  # on the new release together) instead of rolling the env file back under a new current.
  if [ "$rc" = 0 ] && [ "${TEST_STAGE:-}" = cutover ]; then kill -"${TEST_SIGNAL:-TERM}" "$PPID"; fi
  exit "$rc"
fi
exec /bin/mv "$@"
''',
        "sha256sum": '''#!/bin/sh
if command -v shasum >/dev/null 2>&1; then
  [ $# -gt 0 ] && exec shasum -a 256 "$@"
  exec shasum -a 256
fi
exec /usr/bin/sha256sum "$@"
''',
    }


def run_host_deploy(tmp_path, *, legacy=False, sidecar_changed=False, overlays=False,
                    migration_layout=False, stage="", signal_name="TERM", fail_rollback=False,
                    fail_rollback_idle=False, fail_rollback_side=False, fail_sidecar=False,
                    rollback_health_fail=False, member_fail_svc="", member_datastatus="normal",
                    member_wrong_service=False, signal_up_svc="", gate_fail=False,
                    gate_once=False, legacy_id=None, resignal=False, same_sha=False,
                    fail_consumer_rm=False, definition_changed="", new_overlays=False,
                    paper_checkpoint_fail=False):
    import subprocess
    root, prev, new = host_deploy_fixture(tmp_path, legacy=legacy, sidecar_changed=sidecar_changed,
        overlays=overlays, migration_layout=migration_layout, new_overlays=new_overlays)
    script = host_deploy_script(tmp_path)
    if same_sha:
        # A rerun of an already-current commit: previous, current and the env file all name the
        # target SHA from the start, so a standing current -> release must not read as this
        # run's cutover when a signal lands early.
        prev = new
        (root / "opt/.env.production").write_text(f"AI_TRADER_BUILD_SHA={new}\n")
        (root / "opt/current").unlink()
        (root / "opt/current").symlink_to(root / "opt/releases" / new)
    # The running stack before the deploy: the serving slot, the sidecar, and the optional
    # members all on the previous release; the idle slot is not running.
    state = root / "state"
    state.mkdir()
    running = ["trader-runtime-blue", "tqkq-sidecar"] + (["tqkq-pool", "paper-engine"] if overlays and not new_overlays else [])
    for svc in running:
        (state / f"svc-{svc}").write_text(prev + "\n")
    # The sidecar-token secret the member health checks read inside their containers.
    (root / "opt/secrets/tqkq_sidecar_token").write_text("t" * 40 + "\n")
    script = script.replace("previous=<current>", f"previous={root}/opt/releases/{prev}")
    script = (script
        .replace("/var/lib/docker/volumes", f"{root}/var-lib-docker/volumes")
        .replace("/etc/nginx", f"{root}/etc-nginx")
        .replace("/opt/ai-trader", f"{root}/opt"))
    (root / "deploy-host.sh").write_text(script)
    stubs = root / "bin"
    stubs.mkdir()
    for name, body in host_deploy_stubs().items():
        path = stubs / name
        path.write_text(body)
        path.chmod(0o755)
    env = {**os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "TEST_LOG": str(root / "calls.log"),
        "TEST_DATE_COUNT": str(root / "date.count"),
        "TEST_CURL_COUNT": str(root / "curl.count"),
        "TEST_STATE": str(state), "TEST_PREV_SHA": prev,
        "AI_TRADER_READONLY_SIDECAR_TOKEN_FILE": str(root / "opt/secrets/tqkq_sidecar_token"),
        "TEST_MEMBER_DATASTATUS": member_datastatus,
        "TEST_STAGE": stage, "TEST_SIGNAL": signal_name}
    if fail_rollback:
        env["TEST_FAIL_ROLLBACK"] = "1"
    if fail_rollback_idle:
        env["TEST_FAIL_ROLLBACK_IDLE"] = "1"
    if fail_rollback_side:
        env["TEST_FAIL_ROLLBACK_SIDE"] = "1"
    if fail_sidecar:
        env["TEST_FAIL_SIDECAR"] = "1"
    if rollback_health_fail:
        env["TEST_ROLLBACK_HEALTH_FAIL"] = "1"
    if member_fail_svc:
        env["TEST_MEMBER_FAIL_SVC"] = member_fail_svc
    if member_wrong_service:
        env["TEST_MEMBER_WRONG_SERVICE"] = "1"
    if signal_up_svc:
        env["TEST_SIGNAL_UP_SVC"] = signal_up_svc
    if gate_fail:
        env["TEST_GATE_FAIL"] = "1"
    if gate_once:
        env["TEST_GATE_ONCE"] = "1"
    if legacy_id:
        env["TEST_LEGACY_ID"] = legacy_id
    if resignal:
        env["TEST_RESIGNAL"] = "1"
    if fail_consumer_rm:
        env["TEST_FAIL_CONSUMER_RM"] = "1"
    if definition_changed:
        env["TEST_DEFINITION_CHANGED"] = definition_changed
    if paper_checkpoint_fail:
        env["TEST_PAPER_CHECKPOINT_FAIL"] = "1"
    result = subprocess.run(["/bin/sh", str(root / "deploy-host.sh")], env=env,
        capture_output=True, text=True, timeout=60, check=False)
    calls = (root / "calls.log").read_text().splitlines() if (root / "calls.log").exists() else []
    return result, root, prev, new, calls


def host_state(root, service):
    # Which release the fixture's stack state says the service is actually running (None: down).
    path = root / "state" / f"svc-{service}"
    return path.read_text().strip() if path.exists() else None


def assert_old_release_still_serving(result, root, prev, calls):
    output = result.stdout + result.stderr
    assert "DEPLOY_OK" not in output
    assert os.readlink(root / "opt/current").endswith(prev), "current must keep pointing at the previous release"
    assert f"AI_TRADER_BUILD_SHA={prev}" in (root / "opt/.env.production").read_text()
    # The restored old slot is never stopped afterwards and no cutover happens.
    assert not any("rm -sf" in call and "trader-runtime-blue" in call for call in calls), calls
    assert not any(call.startswith("mv ") for call in calls), calls
    assert sum("up -d" in call and "trader-runtime-blue" in call for call in calls) == 1, calls


def test_host_deploy_script_switches_slots_and_current_on_success(tmp_path):
    result, root, prev, new, calls = run_host_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "DEPLOY_OK" in result.stdout
    assert os.readlink(root / "opt/current").endswith(new)
    assert "18001" in (root / "etc-nginx/ai-trader-runtime-upstream.conf").read_text()
    assert f"AI_TRADER_BUILD_SHA={new}" in (root / "opt/.env.production").read_text()
    assert any("up -d" in call and "trader-runtime-green" in call for call in calls)
    assert any("rm -sf" in call and "trader-runtime-blue" in call for call in calls)


@pytest.mark.parametrize("stage", ["nginx-reload", "drain"])
@pytest.mark.parametrize("signal_name,exit_code", [("HUP", 129), ("INT", 130), ("TERM", 143)])
def test_host_deploy_script_signal_rolls_back_once_and_exits_nonzero(tmp_path, stage, signal_name, exit_code):
    result, root, prev, new, calls = run_host_deploy(tmp_path, stage=stage, signal_name=signal_name)
    assert result.returncode == exit_code, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL" in result.stderr
    # The completed restore says so explicitly — the outer script trusts that marker alone.
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert_old_release_still_serving(result, root, prev, calls)
    include = root / "etc-nginx/ai-trader-runtime-upstream.conf"
    assert "18000" in include.read_text(), "the nginx upstream include must be back on the old slot"


def test_host_deploy_script_signal_during_gate_polling_rolls_back(tmp_path):
    result, root, prev, new, calls = run_host_deploy(tmp_path, stage="gate-sleep", signal_name="TERM", gate_once=True)
    assert result.returncode == 143, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL" in result.stderr
    assert_old_release_still_serving(result, root, prev, calls)


def test_host_deploy_script_ignores_repeated_signal_during_rollback(tmp_path):
    # A second signal arrives while the rollback itself is restoring the old slot: it must be
    # ignored (the old flow re-entered rollback and deleted the restored slot) and the rollback
    # still completes exactly once.
    result, root, prev, new, calls = run_host_deploy(tmp_path, stage="drain", signal_name="TERM", resignal=True)
    assert result.returncode == 143, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL" in result.stderr
    assert_old_release_still_serving(result, root, prev, calls)
    assert any("rm -sf" in call and "trader-runtime-green" in call for call in calls), "rollback still removed the idle slot"


def test_host_deploy_script_reports_failed_rollback_as_failure(tmp_path):
    result, root, prev, new, calls = run_host_deploy(tmp_path, stage="nginx-reload", signal_name="TERM", fail_rollback=True)
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)


def test_host_deploy_script_rollback_counts_idle_slot_removal(tmp_path):
    # The old rollback ignored an idle-slot removal failure (|| true): a leftover container on
    # the new image still counted as "restored". Every restore step must now report into
    # ROLLBACK_FAILED, and the success marker must stay absent.
    result, root, prev, new, calls = run_host_deploy(tmp_path, stage="nginx-reload", signal_name="TERM",
        fail_rollback_idle=True)
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)


def test_host_deploy_script_healthz_mismatch_rolls_back_and_exits_nonzero(tmp_path):
    result, root, prev, new, calls = run_host_deploy(tmp_path, gate_fail=True)
    assert result.returncode == 1, result.stderr
    assert "NEW_SLOT_HEALTHZ_MISMATCH" in result.stderr
    assert "ROLLBACK_FAILED" not in result.stderr
    assert "ROLLBACK_OK" in result.stderr
    assert_old_release_still_serving(result, root, prev, calls)


def test_host_deploy_script_sidecar_failure_is_deploy_partial_not_rollback(tmp_path):
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, fail_sidecar=True)
    assert result.returncode == 4, result.stderr
    assert "DEPLOY_PARTIAL" in result.stderr and "SIDECAR_UPDATE_FAILED" in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    # The Runtime switch stands: current points at the new release, the old slot was stopped.
    assert os.readlink(root / "opt/current").endswith(new)
    assert any("rm -sf" in call and "trader-runtime-blue" in call for call in calls)


def test_host_deploy_script_legacy_rollback_restores_whole_stack(tmp_path):
    # First-migration layout: a legacy trader-runtime container and no upstream include. A signal
    # at the migration reload must run the legacy rollback (force-recreate of the previous
    # release's compose files) and still exit non-zero without touching current.
    result, root, prev, new, calls = run_host_deploy(tmp_path, legacy=True, stage="nginx-reload",
        signal_name="TERM", legacy_id="legacy-container-id")
    assert result.returncode == 143, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL" in result.stderr
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)
    assert any("rm -f legacy-container-id" in call for call in calls)
    assert any("up -d" in call and "--force-recreate" in call and prev in call for call in calls), calls
    # The whole-stack legacy restore is held to the member health proof too, not just the slot.
    auth = {c.split()[1] for c in calls if c.startswith("member-auth ")}
    assert "tqkq-sidecar" in auth, calls
    assert not any(call.startswith("mv ") for call in calls), calls


def test_host_deploy_script_plain_failure_with_failed_rollback_exits_nonzero(tmp_path):
    # The non-signal failure sites run `{ echo ...; rollback; exit 1; }`: with rollback itself
    # failing, the script must still exit non-zero and report ROLLBACK_FAILED, never DEPLOY_OK.
    result, root, prev, new, calls = run_host_deploy(tmp_path, gate_fail=True, fail_rollback=True)
    assert result.returncode == 1, result.stderr
    assert "NEW_SLOT_HEALTHZ_MISMATCH" in result.stderr
    assert "ROLLBACK_FAILED" in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)


@pytest.mark.parametrize("signal_name,exit_code", [("HUP", 129), ("TERM", 143)])
def test_host_deploy_script_signal_after_cutover_keeps_the_switch(tmp_path, signal_name, exit_code):
    # The narrow boundary after the atomic current switch — the mv stub signals right after the
    # mv commits, before the stage assignment runs: current and the env file are both on the new
    # release, so a rollback here would split them. The switch stands, the exit stays non-zero
    # (the signal code) so no outer chain continues, and neither DEPLOY_OK nor a rollback is
    # claimed.
    result, root, prev, new, calls = run_host_deploy(tmp_path, stage="cutover", signal_name=signal_name)
    assert result.returncode == exit_code, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL_AFTER_CUTOVER" in result.stderr
    assert "ROLLBACK_OK" not in result.stderr and "DEPLOY_OK" not in result.stdout + result.stderr
    assert os.readlink(root / "opt/current").endswith(new)
    assert f"AI_TRADER_BUILD_SHA={new}" in (root / "opt/.env.production").read_text()
    assert "18001" in (root / "etc-nginx/ai-trader-runtime-upstream.conf").read_text()
    # The rollback never ran: the old slot was not brought back up again.
    assert not any("up -d" in call and "trader-runtime-blue" in call for call in calls), calls


def test_host_deploy_script_signal_after_cutover_with_sidecar_failure_is_partial(tmp_path):
    # Same boundary with a failed sidecar-side refresh: exit 4 and DEPLOY_PARTIAL, exactly as the
    # normal end of the script would classify it — never a rollback of the committed switch.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, fail_sidecar=True,
        stage="cutover")
    assert result.returncode == 4, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL_AFTER_CUTOVER" in result.stderr and "DEPLOY_PARTIAL" in result.stderr
    assert "ROLLBACK_OK" not in result.stderr
    assert os.readlink(root / "opt/current").endswith(new)
    assert f"AI_TRADER_BUILD_SHA={new}" in (root / "opt/.env.production").read_text()


def test_host_deploy_script_same_sha_rerun_early_signal_rolls_back(tmp_path):
    # A rerun of an already-current commit starts with current -> release standing; an early
    # signal there must not read that as this run's cutover. The stage evidence gates the
    # decision, so the interrupt runs the normal rollback and leaves everything consistent.
    result, root, prev, new, calls = run_host_deploy(tmp_path, same_sha=True, stage="nginx-reload",
        signal_name="TERM")
    assert result.returncode == 143, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL" in result.stderr and "AFTER_CUTOVER" not in result.stderr
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    # current/env stay on the (same) target SHA and the upstream include is back on the old slot.
    assert os.readlink(root / "opt/current").endswith(new)
    assert f"AI_TRADER_BUILD_SHA={new}" in (root / "opt/.env.production").read_text()
    assert "18000" in (root / "etc-nginx/ai-trader-runtime-upstream.conf").read_text()


# --- full-stack rollback of the sidecar netns and the paper engine (r4239955095) -----------------
# Red proof: a deploy that had already switched tqkq-sidecar (and with it the shared netns) and
# paper-engine to the new image was interrupted; the old rollback restored only the Runtime slot
# and still printed ROLLBACK_OK — the site kept serving the new sidecar/paper unnoticed. The
# assertions below are on the fixture's stack state (which release each service actually runs),
# not on the script's own log lines.


def test_host_deploy_script_mid_sidecar_signal_restores_whole_netns(tmp_path):
    # A signal lands while the sidecar stack is being force-recreated: everything that was
    # already switched must come back from the previous release's own files, or nothing may be
    # called rolled back.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, overlays=True,
        signal_up_svc="tqkq-sidecar", signal_name="TERM")
    assert result.returncode == 143, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL" in result.stderr and "AFTER_CUTOVER" not in result.stderr
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)
    assert f"AI_TRADER_BUILD_SHA={prev}" in (root / "opt/.env.production").read_text()
    # The whole interrupted netns runs the previous release again: sidecar, pool, the old
    # serving slot and the paper engine — its old container died with the netns recreate even
    # though this deploy never reached the paper step, so it is removed and restored too.
    assert host_state(root, "tqkq-sidecar") == prev
    assert host_state(root, "tqkq-pool") == prev
    assert host_state(root, "trader-runtime-blue") == prev
    assert host_state(root, "paper-engine") == prev
    assert host_state(root, "trader-runtime-green") is None, "the interrupted new slot must be removed"
    # The rebuild runs from the previous release's files as strictly ordered single-purpose
    # compose calls. Removal is two-phase: the namespace's consumers first — both slots (the
    # switch already moved serving to the new slot) and every enabled member, the paper engine
    # included — then the netns owner (tqkq-sidecar) alone; docker rm -f of the owner succeeds
    # even with consumers still attached and the consumers keep their old namespace, so the
    # owner goes last and the rebuild (owner → members → old serving slot → paper) starts only
    # after both.
    rm_consumers = next(i for i, c in enumerate(calls) if "rm -sf" in c and new in c
                        and "trader-runtime-green" in c and "trader-runtime-blue" in c
                        and "tqkq-pool" in c and "paper-engine" in c and "tqkq-sidecar" not in c)
    rm_owner = next(i for i, c in enumerate(calls) if "rm -sf" in c and new in c
                    and "tqkq-sidecar" in c and "trader-runtime" not in c)
    ups = [i for i, c in enumerate(calls) if "up -d" in c and prev in c and "--force-recreate" in c]
    i_side = next(i for i in ups if "tqkq-sidecar" in calls[i]
                  and "trader-runtime-blue" not in calls[i] and "tqkq-pool" not in calls[i]
                  and "paper-engine" not in calls[i])
    i_pool = next(i for i in ups if "tqkq-pool" in calls[i] and "tqkq-sidecar" not in calls[i])
    i_slot = next(i for i in ups if "trader-runtime-blue" in calls[i] and "tqkq-sidecar" not in calls[i])
    i_paper = next(i for i in ups if "paper-engine" in calls[i] and "tqkq-sidecar" not in calls[i])
    assert rm_consumers < rm_owner < i_side < i_pool < i_slot < i_paper, calls
    # Every enabled member proved itself over its own /healthz with a Bearer header that came
    # from the token file, not from the command line.
    auth = {c.split()[1]: c.split()[2] for c in calls if c.startswith("member-auth ")}
    assert auth == {"tqkq-sidecar": "bearer=True", "tqkq-pool": "bearer=True",
                    "paper-engine": "bearer=True"}, calls
    assert any(c.startswith("member-auth tqkq-sidecar bearer=True url=http://127.0.0.1:19001/healthz")
               for c in calls), calls
    assert "18000" in (root / "etc-nginx/ai-trader-runtime-upstream.conf").read_text()


def test_host_deploy_script_signal_after_paper_restores_paper_and_netns(tmp_path):
    # The deploy got through both the sidecar refresh and the paper engine before the signal:
    # both were switched, so both must be restored; ROLLBACK_OK only after the restored serving
    # slot reports the previous release over /healthz.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, overlays=True,
        signal_up_svc="rsshub-tunnel", signal_name="TERM")
    assert result.returncode == 143, result.stderr
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)
    assert host_state(root, "tqkq-sidecar") == prev
    assert host_state(root, "tqkq-pool") == prev
    assert host_state(root, "paper-engine") == prev, "paper was switched and must be restored"
    assert host_state(root, "trader-runtime-blue") == prev
    assert host_state(root, "trader-runtime-green") is None
    assert any("up -d" in c and prev in c and "paper-engine" in c for c in calls), calls
    assert_paper_gate_before_old_engine(calls, prev, new)


PAPER_GATE = ("docker run --rm --network none -v ai-trader-production_ai-trader-paper-data:/data "
              "ai-trader-sidecar:{short} python -m tqkq_readonly_sidecar.paper --checkpoint-db "
              "/data/paper.sqlite3")


def assert_paper_gate_before_old_engine(calls, prev, new):
    # #527/#510: the previous paper engine starts only after this release's image merged and
    # verified the checkpoints on the stopped volume (an older engine reads state_json).
    gate = calls.index(PAPER_GATE.format(short=new[:7]))
    rm_paper = max(i for i, c in enumerate(calls) if "rm -sf" in c and "paper-engine" in c)
    up_paper = next(i for i, c in enumerate(calls) if "up -d" in c and prev in c and "paper-engine" in c)
    assert rm_paper < gate < up_paper, calls


@pytest.mark.parametrize("sidecar_changed", [True, False], ids=["netns", "paper-only"])
def test_host_deploy_script_paper_checkpoint_gate_failure_keeps_old_engine_down(tmp_path, sidecar_changed):
    # The gate failing (checkpoints not caught up, or the new engine still holds the lock) must
    # not start the old engine on a stale snapshot: the restore stops there and is ROLLBACK_FAILED.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=sidecar_changed,
        overlays=True, signal_up_svc="rsshub-tunnel", signal_name="TERM", paper_checkpoint_fail=True)
    assert result.returncode != 0
    assert "ROLLBACK_PAPER_CHECKPOINT_FAILED" in result.stderr
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    assert PAPER_GATE.format(short=new[:7]) in calls
    assert host_state(root, "paper-engine") is None, "the old engine must not start"
    assert not any("up -d" in c and prev in c and "paper-engine" in c for c in calls), calls
    assert host_state(root, "trader-runtime-blue") == prev


def test_host_deploy_script_paper_only_signal_keeps_sidecar_untouched(tmp_path):
    # Only the paper engine was switched (sidecar source unchanged): rollback keeps the sidecar
    # stack and its subscriptions exactly as they are and restores the paper engine plus the old
    # serving slot.
    result, root, prev, new, calls = run_host_deploy(tmp_path, overlays=True,
        signal_up_svc="rsshub-tunnel", signal_name="TERM")
    assert result.returncode == 143, result.stderr
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert host_state(root, "paper-engine") == prev
    assert host_state(root, "trader-runtime-blue") == prev
    assert host_state(root, "trader-runtime-green") is None
    assert host_state(root, "tqkq-sidecar") == prev
    # The sidecar is never recreated anywhere in this deploy — sessions survive it.
    assert not any("up -d" in c and "tqkq-sidecar" in c for c in calls), calls
    assert any("up -d" in c and prev in c and "paper-engine" in c and "--force-recreate" in c
               for c in calls), calls
    assert_paper_gate_before_old_engine(calls, prev, new)


@pytest.mark.parametrize("changed,recreated", [
    # #793: extra_hosts on the sidecar with its source unchanged — the old source-only rule kept
    # the old sidecar. The whole netns follows it, the new serving slot included.
    ("tqkq-sidecar", ("tqkq-sidecar", "tqkq-pool", "trader-runtime-green")),
    # A member's own definition: that member alone; the sidecar keeps its sessions.
    ("tqkq-pool", ("tqkq-pool",)),
])
def test_host_deploy_script_recreates_containers_whose_definition_changed(tmp_path, changed, recreated):
    result, root, prev, new, calls = run_host_deploy(tmp_path, overlays=True, definition_changed=changed)
    assert result.returncode == 0, result.stderr
    assert "DEPLOY_OK" in result.stdout
    assert "definition changed" in result.stdout
    for svc in recreated:
        assert host_state(root, svc) == new, svc
    if "tqkq-sidecar" not in recreated:
        assert host_state(root, "tqkq-sidecar") == prev
        assert not any("up -d" in c and "tqkq-sidecar " in c + " " for c in calls), calls


def test_host_deploy_script_rollback_removes_members_this_deploy_enabled(tmp_path):
    # The pool and paper overlays were enabled just before this deploy: the plan creates them,
    # and an interrupted switch must remove them again — the previous release never ran them and
    # has no image for them, so trying to restore them would fail the whole rollback.
    result, root, prev, new, calls = run_host_deploy(tmp_path, overlays=True, new_overlays=True,
        signal_up_svc="rsshub-tunnel", signal_name="TERM")
    assert "tqkq-pool" in result.stdout and "not running" in result.stdout
    assert result.returncode == 143, result.stderr
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert host_state(root, "tqkq-sidecar") == prev
    assert host_state(root, "trader-runtime-blue") == prev
    assert host_state(root, "tqkq-pool") is None
    assert host_state(root, "paper-engine") is None


def test_host_deploy_script_side_restore_failure_is_rollback_failed(tmp_path):
    # Restoring the sidecar stack itself fails: the marker must say ROLLBACK_FAILED and the exit
    # must stay non-zero — never a ROLLBACK_OK over a still-new sidecar.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, overlays=True,
        signal_up_svc="tqkq-sidecar", fail_rollback_side=True, signal_name="TERM")
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)
    # The interrupted switch was removed but its restore never came back: the sidecar is down,
    # and that incomplete state is exactly what ROLLBACK_FAILED must report instead of covering.
    assert host_state(root, "tqkq-sidecar") is None
    assert any("rm -sf" in c and "tqkq-sidecar" in c for c in calls), calls


def test_host_deploy_script_consumer_rm_failure_keeps_owner_in_place(tmp_path):
    # The consumer removal itself fails (an external Docker failure, injected as a real exit 1):
    # the rollback stops right there — the netns owner is neither removed nor rebuilt around a
    # half-removed consumer set, and the outcome stays ROLLBACK_FAILED for the outer guard.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, overlays=True,
        signal_up_svc="tqkq-sidecar", fail_consumer_rm=True, signal_name="TERM")
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)
    # The consumer removal ran and failed; the owner rm and the whole rebuild never happened.
    assert any("rm -sf" in c and "trader-runtime-blue" in c and "trader-runtime-green" in c
               and "tqkq-pool" in c and "paper-engine" in c for c in calls), calls
    assert not any("rm -sf" in c and "tqkq-sidecar" in c for c in calls), calls
    assert not any("up -d" in c and prev in c for c in calls), calls


def test_host_deploy_script_rollback_healthz_mismatch_is_rollback_failed(tmp_path):
    # Services were restored, but the restored serving slot never reports the previous release
    # over /healthz: ROLLBACK_OK is earned by that check alone, so the deploy says
    # ROLLBACK_FAILED instead.
    result, root, prev, new, calls = run_host_deploy(tmp_path, stage="drain",
        rollback_health_fail=True, signal_name="TERM")
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_HEALTHZ_MISMATCH" in result.stderr
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr
    assert os.readlink(root / "opt/current").endswith(prev)
    # The containers really did come back; it is the healthz proof that failed.
    assert host_state(root, "trader-runtime-blue") == prev


def test_host_deploy_script_full_refresh_with_overlays_succeeds(tmp_path):
    # The positive path with every member switched: sidecar netns plus paper engine all end up
    # on the new release and the deploy reports DEPLOY_OK.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, overlays=True)
    assert result.returncode == 0, result.stderr
    assert "DEPLOY_OK" in result.stdout
    assert os.readlink(root / "opt/current").endswith(new)
    for svc in ("trader-runtime-green", "tqkq-sidecar", "tqkq-pool", "paper-engine"):
        assert host_state(root, svc) == new, svc
    assert host_state(root, "trader-runtime-blue") is None


# --- round-6 regressions: migration without legacy, member health semantics --------------------


def test_host_deploy_script_migration_without_legacy_restores_netns(tmp_path):
    # needs_migration=1 with legacy_previous=0: a blue-green previous release whose upstream
    # include is missing still forces the full-stack recreate, which switches the whole sidecar
    # netns — so an interruption must restore every member (paper included), not run the
    # lightweight slot-only rollback, and put the pre-migration nginx template back.
    result, root, prev, new, calls = run_host_deploy(tmp_path, overlays=True, migration_layout=True,
        stage="nginx-reload", signal_name="TERM")
    assert result.returncode == 143, result.stderr
    assert "DEPLOY_ABORTED_BY_SIGNAL" in result.stderr and "AFTER_CUTOVER" not in result.stderr
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    # The migration really ran: one whole-project force-recreate from the new release's files
    # (both slots and every member switched), so the rollback removes the namespace consumers
    # first — both slots included — and only then the netns owner, before rebuilding.
    assert any("up -d" in c and "--force-recreate" in c and new in c and "--no-deps" not in c
               and "tqkq-sidecar" not in c for c in calls), calls
    rm_consumers = next(i for i, c in enumerate(calls) if "rm -sf" in c and new in c
                        and "trader-runtime-green" in c and "trader-runtime-blue" in c
                        and "tqkq-pool" in c and "paper-engine" in c and "tqkq-sidecar" not in c)
    rm_owner = next(i for i, c in enumerate(calls) if "rm -sf" in c and new in c
                    and "tqkq-sidecar" in c and "trader-runtime" not in c)
    assert rm_consumers < rm_owner, calls
    assert host_state(root, "tqkq-sidecar") == prev
    assert host_state(root, "tqkq-pool") == prev
    assert host_state(root, "paper-engine") == prev
    assert host_state(root, "trader-runtime-blue") == prev
    assert host_state(root, "trader-runtime-green") is None
    assert os.readlink(root / "opt/current").endswith(prev)
    assert f"AI_TRADER_BUILD_SHA={prev}" in (root / "opt/.env.production").read_text()
    # The pre-migration conf.d template (direct 18000 proxy) is back, and the include file
    # this deploy created is removed: no include existed before the migration, so none may
    # remain after the rollback.
    assert (root / "etc-nginx/conf.d/ai-trader-runtime.conf").read_text() \
        == "location / { proxy_pass http://127.0.0.1:18000; }\n"
    assert not (root / "etc-nginx/ai-trader-runtime-upstream.conf").exists(), calls
    auth = {c.split()[1] for c in calls if c.startswith("member-auth ")}
    assert {"tqkq-sidecar", "tqkq-pool", "paper-engine"} <= auth, calls


def test_host_deploy_script_unhealthy_member_is_rollback_failed(tmp_path):
    # The netns containers came back, but tqkq-sidecar's own /healthz never answers: the
    # restore may not be called complete — ROLLBACK_MEMBER_UNHEALTHY and ROLLBACK_FAILED,
    # never ROLLBACK_OK.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, overlays=True,
        signal_up_svc="tqkq-sidecar", member_fail_svc="tqkq-sidecar", signal_name="TERM")
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_MEMBER_UNHEALTHY: tqkq-sidecar" in result.stderr
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr
    assert "DEPLOY_OK" not in result.stdout + result.stderr
    assert host_state(root, "trader-runtime-blue") == prev


def test_host_deploy_script_member_wrong_service_is_rollback_failed(tmp_path):
    # A member answering /healthz with the wrong service identity never passes as restored —
    # the generated check asserts the real service ids, not just HTTP 200.
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, overlays=True,
        signal_up_svc="tqkq-sidecar", member_wrong_service=True, signal_name="TERM")
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_MEMBER_UNHEALTHY: tqkq-sidecar" in result.stderr
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr


def test_host_deploy_script_paper_unavailable_is_rollback_failed(tmp_path):
    # Paper reports dataStatus=unavailable after the restore: HTTP up is not business
    # availability — the restore cannot be called complete.
    result, root, prev, new, calls = run_host_deploy(tmp_path, overlays=True,
        signal_up_svc="rsshub-tunnel", member_datastatus="unavailable", signal_name="TERM")
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_MEMBER_UNHEALTHY: paper-engine" in result.stderr
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr
    assert host_state(root, "paper-engine") == prev


def test_host_deploy_script_paper_stale_data_is_restored(tmp_path):
    # Negative control: dataStatus=stale is freshness, not unavailability — a restored member
    # serving slightly stale quotes is restored; the gate must not force 'normal'.
    result, root, prev, new, calls = run_host_deploy(tmp_path, overlays=True,
        signal_up_svc="rsshub-tunnel", member_datastatus="stale", signal_name="TERM")
    assert result.returncode == 143, result.stderr
    assert "ROLLBACK_OK" in result.stderr and "ROLLBACK_FAILED" not in result.stderr
    assert any(c.startswith("member-auth paper-engine bearer=True") for c in calls), calls


def test_host_deploy_script_sidecar_connecting_is_rollback_failed(tmp_path):
    # dataStatus=connecting means the member has not proved live data yet: HTTP up is not
    # business availability, so the restore may not be called complete (same for unavailable).
    result, root, prev, new, calls = run_host_deploy(tmp_path, sidecar_changed=True, overlays=True,
        signal_up_svc="tqkq-sidecar", member_datastatus="connecting", signal_name="TERM")
    assert result.returncode not in (0, 143)
    assert "ROLLBACK_MEMBER_UNHEALTHY: tqkq-sidecar" in result.stderr
    assert "ROLLBACK_FAILED" in result.stderr and "ROLLBACK_OK" not in result.stderr


def backup_fixture(path, family="runtime"):
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        names = ("sessions", "events", "proposals") if family == "runtime" else ("paper_accounts", "paper_orders", "paper_trades")
        for name in names:
            database.execute(f'CREATE TABLE "{name}" (value INTEGER)')
            database.execute(f'INSERT INTO "{name}" VALUES (1)')


def test_backup_families_fallback_and_unknown_restore_sets(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **_: set())
    old = tmp_path / "backups/1.8.0/before-0000001.sqlite3"
    good = tmp_path / "backups/1.8.1/before-0000002.sqlite3"
    bad = tmp_path / "backups/1.8.1/before-0000003.sqlite3"
    for at, path in enumerate((old, good, bad), 1):
        backup_fixture(path)
        os.utime(path, (at, at))
    bad.write_bytes(b"corrupt")
    os.utime(bad, (3, 3))
    domestic = tmp_path / "backups/paper-20261020/paper-before-0000001.sqlite3"
    foreign = domestic.with_name("paper-foreign-before-0000001.sqlite3")
    for path in (domestic, foreign):
        backup_fixture(path, "paper")
    unknown = tmp_path / "backups/migrate-1234567/ai-trader.sqlite3"
    backup_fixture(unknown)
    env = tmp_path / ".env.production.before-0000001"
    env.write_text("KEY=do-not-report-this-value\n")
    plan = maintenance.backup_retention(tmp_path, 10000)
    assert plan["retained"]["runtime"]["path"] == str(good)
    assert set(plan["retained"]) == {"runtime", "paper", "paper-foreign", "release-env"}
    assert plan["candidates"] == [str(old)]
    assert plan["rejected"] == [str(bad)]
    assert plan["unmanaged"] == [str(unknown)]
    assert "do-not-report" not in json.dumps(plan)


def test_backup_wal_open_recent_and_symlink_are_never_deleted(tmp_path, monkeypatch):
    old = tmp_path / "backups/1.8.0/before-0000001.sqlite3"
    good = old.with_name("before-0000002.sqlite3")
    for path in (old, good):
        backup_fixture(path)
    os.utime(old, (1, 1))
    os.utime(good, (2, 2))
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **_: {old})
    assert maintenance.backup_retention(tmp_path, 10000)["candidates"] == []
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **_: set())
    wal = old.with_name(old.name + "-wal")
    wal.write_bytes(b"pending")
    assert maintenance.backup_retention(tmp_path, 10000)["candidates"] == []
    wal.unlink()
    assert maintenance.backup_retention(tmp_path, 100)["candidates"] == []
    old.unlink()
    old.symlink_to(good)
    assert maintenance.backup_retention(tmp_path, 10000)["candidates"] == []


def registry_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(maintenance, "container_images", lambda: {"active-config"})
    monkeypatch.setattr(maintenance, "command", lambda *_: json.dumps([{"Id": "rollback-config"}]))
    manifests, configs, aliases = {}, {}, {}
    for i in range(7):
        tag, digest = f"{i:040x}", "sha256:" + f"{i:064x}"
        config = "active-config" if i == 0 else "rollback-config" if i == 1 else f"config-{i}"
        aliases[tag] = digest
        manifests[digest] = {"mediaType": maintenance.REGISTRY_ACCEPT, "config": {"digest": config}}
        revision = tmp_path / "registry/data/docker/registry/v2/repositories/ai-trader-runtime/_manifests/revisions/sha256" / digest.split(":")[1] / "link"
        revision.parent.mkdir(parents=True)
        revision.write_text(digest)
        configs[config] = {"created": f"2020-01-0{i + 1}T00:00:00Z"}
        link = tmp_path / "registry/data/docker/registry/v2/repositories/ai-trader-runtime/_manifests/tags" / tag / "current/link"
        link.parent.mkdir(parents=True)
        link.write_text(digest)
        os.utime(link, (1, 1))
    # Unknown tag aliases an otherwise stale digest: DELETE must preserve the whole identity.
    aliases["manual"] = aliases[f"{2:040x}"]
    def request(path, **_):
        if path.startswith("_catalog"):
            return {"repositories": ["ai-trader-runtime", "unknown"]}, {}
        if "tags/list" in path:
            return {"tags": list(aliases)}, {}
        if "/manifests/" in path:
            ref = path.rsplit("/", 1)[1]
            digest = aliases.get(ref, ref)
            return manifests[digest], {"Docker-Content-Digest": digest}
        return configs[path.rsplit("/", 1)[1]], {}
    monkeypatch.setattr(maintenance, "registry_request", request)
    return aliases, manifests, configs


def test_registry_shared_digest_containers_rollback_and_unknown_tags(tmp_path, monkeypatch):
    registry_fixture(tmp_path, monkeypatch)
    plan = maintenance.registry_retention({"ai-trader-runtime:rollback"}, 2_000_000_000)
    assert [row["tags"] for row in plan["candidates"]] == [[f"{3:040x}"]]
    assert len(plan["retained"]) == 6
    assert "unknown" in plan["unknown"] and "ai-trader-runtime:manual" in plan["unknown"]


def test_registry_no_candidates_does_not_touch_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(maintenance, "command", lambda *_: pytest.fail("no registry restart for an empty plan"))
    assert maintenance.collect_registry({"candidates": []}, set(), 10000)["status"] == "unchanged"


def test_registry_recovery_checks_identity_and_does_not_start_unknown_container(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "RUNTIME_ROOT", tmp_path)
    marker = tmp_path / "maintenance/registry-gc-in-progress"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"container": "old", "image": "image", "gcNeeded": True}))
    calls = []
    monkeypatch.setattr(maintenance, "command", lambda *args: calls.append(args) or json.dumps([{"Id": "replaced", "Image": "image"}]))
    with pytest.raises(ValueError, match="changed during"):
        maintenance.recover_registry()
    assert len(calls) == 1 and marker.exists()


def test_registry_oci_index_shared_child_and_unknown_root(tmp_path, monkeypatch):
    aliases, manifests, _ = registry_fixture(tmp_path, monkeypatch)
    root = aliases[f"{3:040x}"]
    shared = aliases[f"{2:040x}"]
    leaf = "sha256:" + f"{9:064x}"
    manifests[leaf] = manifests[root]
    manifests[root] = {"mediaType": "application/vnd.oci.image.index.v1+json",
                       "manifests": [{"digest": shared}, {"digest": leaf}]}
    revision = tmp_path / "registry/data/docker/registry/v2/repositories/ai-trader-runtime/_manifests/revisions/sha256" / leaf.split(":")[1] / "link"
    revision.parent.mkdir(parents=True)
    revision.write_text(leaf)
    plan = maintenance.registry_retention({"rollback"}, 2_000_000_000)
    assert {p["digest"] for p in plan["candidates"]} == {root, leaf}
    assert shared in {p["digest"] for p in plan["retained"]}
    # An independent untagged parent can also reference the otherwise disposable leaf.
    unknown = "sha256:" + f"{10:064x}"
    manifests[unknown] = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [{"digest": leaf}]}
    extra = revision.parent.parent / unknown.split(":")[1] / "link"
    extra.parent.mkdir()
    extra.write_text(unknown)
    assert {p["digest"] for p in maintenance.registry_retention({"rollback"}, 2_000_000_000)["candidates"]} == {root}


def test_registry_container_index_id_and_unknown_media_type(tmp_path, monkeypatch):
    aliases, manifests, _ = registry_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(maintenance, "container_images", lambda: {aliases[f"{0:040x}"]})
    plan = maintenance.registry_retention({"rollback"}, 2_000_000_000)
    assert aliases[f"{0:040x}"] in {p["digest"] for p in plan["retained"]}
    manifests[aliases[f"{3:040x}"]]["mediaType"] = "application/unknown"
    with pytest.raises(ValueError, match="unknown registry media"):
        maintenance.registry_retention({"rollback"}, 2_000_000_000)


def test_unowned_worker_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "RUNTIME_ROOT", tmp_path)
    calls = []
    worker = {"Id": "foreign-id", "Image": "expected-image", "Config": {"Labels": {}}, "Mounts": []}
    def command(*args):
        calls.append(args)
        return "foreign-id" if "ps" in args else json.dumps([worker])
    monkeypatch.setattr(maintenance, "command", command)
    with pytest.raises(ValueError, match="unowned"):
        maintenance.remove_registry_worker({"token": "ours", "image": "expected-image"})
    assert all("rm" not in args for args in calls)


def test_keeper_changed_new_wal_and_open_handle_block_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **_: set())
    old = tmp_path / "backups/1.8.1/before-0000001.sqlite3"
    good = old.with_name("before-0000002.sqlite3")
    for at, path in enumerate((old, good), 1):
        backup_fixture(path)
        os.utime(path, (at, at))
    plan = maintenance.backup_retention(tmp_path, 10000)
    maintenance.retained_backups_unchanged(plan)
    assert not maintenance.backup_idle(old, {old})
    old.with_name(old.name + "-wal").write_bytes(b"new pending WAL")
    assert not maintenance.backup_idle(old, set())
    good.write_bytes(b"replaced keeper")
    with pytest.raises(ValueError, match="retained backup changed"):
        maintenance.retained_backups_unchanged(plan)


def test_foreign_after_stays_in_foreign_family_and_rollback_env_is_protected(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "open_release_paths", lambda **_: set())
    path = tmp_path / "backups/paper-20261020/paper-foreign-after-0000001.sqlite3"
    backup_fixture(path, "paper")
    for i in (1, 2):
        env = tmp_path / f".env.production.before-{i:07x}"
        env.write_text("KEY=value\n")
        os.utime(env, (i, i))
    plan = maintenance.backup_retention(tmp_path, 10000, ["0000001" + "0" * 33])
    assert set(plan["retained"]) == {"paper-foreign", "release-env"}
    assert not plan["candidates"]
    assert plan["protected"] == [str(tmp_path / ".env.production.before-0000001")]


@pytest.mark.parametrize("image_id", ["sha256:configuration-id", maintenance.REGISTRY_IMAGE.split("@", 1)[1]])
def test_registry_resolves_the_pinned_image_before_validating_the_original(tmp_path, monkeypatch, image_id):
    monkeypatch.setattr(maintenance, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(maintenance, "registry_uploads_active", lambda _: False)
    calls = []
    def command(*args):
        calls.append(args)
        if args == ("docker", "inspect", maintenance.REGISTRY_CONTAINER):
            return json.dumps([{"Id": "original", "Image": image_id, "State": {"Running": True},
                                "Mounts": [{"Source": str(tmp_path / "registry/data"), "Destination": "/var/lib/registry"}]}])
        if args[:3] == ("docker", "image", "inspect"):
            return image_id
        if "ps" in args:
            return "occupied-worker"
        pytest.fail("registry must remain running during validation")
    monkeypatch.setattr(maintenance, "command", command)
    # Reaching the occupied-name guard proves the valid pinned container was accepted, even
    # when its Docker image ID differs from the registry's manifest/index digest.
    with pytest.raises(ValueError, match="already occupied"):
        maintenance.collect_registry({"candidates": [{"digest": "candidate"}]}, set(), 10000)
    assert ("docker", "image", "inspect", "--format", "{{.Id}}", maintenance.REGISTRY_IMAGE) in calls


@pytest.mark.parametrize("entrypoint", ["deploy-release.sh", "migrate-to-host.sh"])
@pytest.mark.parametrize("case", ["current", "old-timer", "missing-candidate", "missing-main", "fetch-failed", "read-failed"])
def test_formal_entrypoints_refuse_timer_regression_before_remote_changes(tmp_path, entrypoint, case):
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.PIPE).strip()

    timer = "deploy/vps/ai-trader-maintenance.timer"
    for name in ("deploy/vps/deploy-release.sh", "deploy/vps/migrate-to-host.sh", timer,
                 "deploy/vps/docker-compose.yml", "deploy/vps/nginx-ai-trader-runtime.conf", "package.json"):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "Fixture")
    git("add", ".")
    git("commit", "-qm", "current policy")
    current = git("rev-parse", "HEAD")
    git("checkout", "-qb", "candidate", current)
    if case == "old-timer":
        (repo / timer).write_text("[Timer]\nOnUnitActiveSec=1h\nPersistent=true\n")
    elif case == "missing-candidate":
        (repo / timer).unlink()
    # Candidate HEAD equals the requested SHA, while main is independently authoritative.
    git("add", "-A")
    git("commit", "-qm", "isolated candidate", "--allow-empty")
    candidate = git("rev-parse", "HEAD")
    git("branch", "-f", "main", candidate)
    git("checkout", "-q", "main")
    if case == "missing-main":
        (repo / timer).unlink()
    else:
        shutil.copyfile(source / timer, repo / timer)
    git("add", "-A")
    git("commit", "-qm", "newer main policy", "--allow-empty")
    git("checkout", "-q", "candidate")
    git("remote", "add", "origin", str(tmp_path / "unreachable") if case == "fetch-failed" else str(repo))
    # A stale cached origin/main points at the candidate; only a real fetch reveals the newer rule.
    git("update-ref", "refs/remotes/origin/main", candidate)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "ssh.jsonl"
    ssh = bin_dir / "ssh"
    ssh.write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ["TEST_SSH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "source-fixture" and "bash -s" in sys.argv[-1]:
    print("current=/opt/ai-trader/releases/" + os.environ["TEST_CANDIDATE"])
    print("machine=source-machine\\nslots=blue-green\\nupstream=18000\\nenv=present")
    print("services=trader-runtime-blue trader-runtime-green tqkq-sidecar\\nvol=cloud")
elif sys.argv[1] == "target-fixture" and sys.argv[-1].startswith("cat /etc/machine-id"):
    print("target-machine")
else:
    sys.exit(73)  # Any attempted mutation is a test failure, even though this is a harmless stub.
''')
    ssh.chmod(0o755)
    if case == "read-failed":
        real_git = shutil.which("git")
        wrapper = bin_dir / "git"
        wrapper.write_text(f'#!/bin/sh\ncase "$*" in *"cat-file -e origin/main:"*) exit 128 ;; esac\nexec "{real_git}" "$@"\n')
        wrapper.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "TEST_SSH_LOG": str(calls), "TEST_CANDIDATE": candidate}
    args = ["bash", str(repo / "deploy/vps" / entrypoint)]
    if entrypoint == "deploy-release.sh":
        args += [candidate, "--host", "target-fixture", "--build-on-host"]
    else:
        args += ["target-fixture", "--from", "source-fixture", "--phase", "images"]
    if case == "current":
        if entrypoint == "deploy-release.sh":
            args.append("--dry-run")
        # Repeat the same official dry-run: refreshing authority remains safe and idempotent.
        runs = 2
    else:
        if entrypoint == "migrate-to-host.sh":
            args.append("--apply")
        runs = 1
    for _ in range(runs):
        result = subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True, timeout=10)
        if case == "current":
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0
            expected = "MAINTENANCE_TIMER_MAIN_UNREADABLE" if case == "fetch-failed" else "MAINTENANCE_TIMER_MISMATCH"
            assert expected in result.stderr, result.stdout + result.stderr
    logged = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    # Deploy performs no ssh before the guard; migration only reads the two machine/layout facts.
    if case == "current" and entrypoint == "deploy-release.sh":
        # Existing dry-run probes a temporary service-plan directory; the stub refuses the write.
        assert logged and all("/tmp/agent-plan-" in call[-1] for call in logged), logged
    else:
        assert len(logged) == (2 * runs if entrypoint == "migrate-to-host.sh" else 0), logged
        assert not any(any(word in arg for word in ("mkdir", "install", " stop", "systemd-run")) for call in logged for arg in call)
    if case != "fetch-failed":
        assert git("rev-parse", "origin/main") == git("rev-parse", "main")
