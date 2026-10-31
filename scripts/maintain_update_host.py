#!/usr/bin/env python3
"""Bounded update/Runtime host retention. Default is a read-only plan; --apply mutates."""

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

UPDATE_ROOT = Path("/var/www/ai-trader-updates")
RUNTIME_ROOT = Path("/opt/ai-trader")
GIB = 1024 ** 3
# The loopback registry on the Runtime host (deploy/vps/docker-compose.registry.yml). Its full-sha
# aliases on the production daemon are managed together with the short compose names (#668).
# --storage separately manages the registry graph and its official offline garbage collection.
MANAGED_REGISTRY = "127.0.0.1:5000"
# The NAS build host's push side of the same registry (deploy-release.sh nas-registry mode): every
# release leaves 127.0.0.1:15000/<repo>:<full-sha> for both images there, plus its build cache.
# --nas keeps those to a bounded set (#668); --storage runs only on the production host.
NAS_PUSH_PREFIX = "127.0.0.1:15000"
NAS_CACHE_LIMIT = "25GB"
REGISTRY_CONTAINER = "ai-trader-registry-registry-1"
REGISTRY_WORKER = "ai-trader-registry-maintenance"
REGISTRY_IMAGE = "registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
REGISTRY_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
REGISTRY_TYPES = (REGISTRY_ACCEPT, "application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")


def file_identity(path: Path) -> list:
    st = path.lstat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def backup_idle(path: Path, opened: set) -> bool:
    base = path
    for suffix in ("-wal", "-shm", "-journal"):
        if path.name.endswith(suffix):
            base = path.with_name(path.name[:-len(suffix)])
            break
    siblings = [base, *(base.with_name(base.name + suffix) for suffix in ("-wal", "-shm", "-journal"))]
    return not any(p == f or f in p.parents for p in siblings for f in opened) and not any(
        p.exists() and p.stat().st_size for p in siblings if p.name.endswith(("-wal", "-journal")))


def retained_backups_unchanged(plan: dict) -> None:
    for proof in plan["retained"].values():
        path = Path(proof["path"])
        if path.is_symlink() or file_identity(path) != proof["identity"] or not backup_idle(path, set()):
            raise ValueError("verified retained backup changed; refusing cleanup")


def verified_database(path: Path, family: str) -> dict:
    """Read the standalone backup, never copy a live main file without its WAL."""
    if path.is_symlink() or any(path.with_name(path.name + suffix).exists()
                                and path.with_name(path.name + suffix).stat().st_size
                                for suffix in ("-wal", "-journal")):
        raise ValueError("backup is linked or has pending transactions")
    required = (("sessions", "events", "proposals") if family == "runtime"
                else ("paper_accounts", "paper_orders", "paper_trades"))
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as database:
        if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("backup integrity_check failed")
        counts = {name: database.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                  for name in required}
        # Reject another valid but empty SQLite file masquerading as a backup.
        if not any(counts.values()):
            raise ValueError("backup contains no business rows")
        return {"path": str(path), "integrity": "ok", "counts": counts}


def backup_retention(root: Path, now: float, protected_shas=()) -> dict:
    """Only established source families are managed; restore sets are never flattened."""
    families = {"runtime": [], "paper": [], "paper-foreign": [], "release-env": []}
    backups = root / "backups"
    known = set()
    if backups.exists():
        for directory in backups.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            if re.fullmatch(r"\d+\.\d+\.\d+", directory.name):
                for path in directory.glob("before-*.sqlite3"):
                    if re.fullmatch(r"before-[a-f0-9]{7,40}\.sqlite3", path.name):
                        families["runtime"].append(path)
            elif re.fullmatch(r"paper-\d{8}", directory.name):
                for path in directory.glob("*.sqlite3"):
                    family = "paper-foreign" if path.name.startswith("paper-foreign-") else "paper"
                    if re.fullmatch(r"paper-(?:foreign-)?(?:before|after)-[a-z0-9-]+\.sqlite3", path.name):
                        families[family].append(path)
    families["release-env"] = [p for p in root.glob(".env.production.before-*")
                               if re.fullmatch(r"\.env\.production\.before-[a-f0-9]{7,40}", p.name)]
    result = {"retained": {}, "candidates": [], "rejected": [], "unmanaged": [], "protected": [], "identities": {}}
    opened = open_release_paths(roots=(backups, root))
    for family, paths in families.items():
        known.update(paths)
        ranked = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
        for index, path in enumerate(ranked):
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("not a regular backup")
                identity = file_identity(path)
                if family == "release-env":
                    # Validate syntax only; never serialize or print credential values.
                    lines = [line for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
                    if not lines or any(not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line) for line in lines):
                        raise ValueError("invalid environment backup")
                    proof = {"path": str(path), "syntax": "ok", "keys": len(lines)}
                else:
                    proof = verified_database(path, "runtime" if family == "runtime" else "paper")
                if file_identity(path) != identity:
                    raise ValueError("backup changed during verification")
                proof["identity"] = identity
                result["retained"][family] = proof
                # Failed newer snapshots remain as evidence. Only older siblings can go.
                for old in ranked[index + 1:]:
                    if family == "release-env" and any(sha.startswith(old.name.rsplit("-", 1)[1]) for sha in protected_shas):
                        result["protected"].append(str(old))
                        continue
                    siblings = [old, *(old.with_name(old.name + suffix) for suffix in ("-wal", "-shm", "-journal"))]
                    if old.is_symlink() or now - old.stat().st_mtime < 3600 or any(
                            p == f or f in p.parents for p in siblings for f in opened):
                        continue
                    if any(p.exists() and p.stat().st_size for p in siblings[1:] if p.name.endswith(("-wal", "-journal"))):
                        continue
                    for sibling in siblings:
                        if sibling.exists() and not sibling.is_symlink():
                            result["candidates"].append(str(sibling))
                            result["identities"][str(sibling)] = file_identity(sibling)
                break
            except (ValueError, sqlite3.Error, OSError):
                result["rejected"].append(str(path))
    if backups.exists():
        result["unmanaged"] = sorted(str(p) for p in backups.rglob("*") if p.is_file()
                                      and p not in known and not any(str(p) == str(k) + suffix
                                      for k in known for suffix in ("-wal", "-shm", "-journal")))
    return result


def registry_request(path: str, *, port=5000, method="GET") -> tuple:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v2/{path}", method=method,
                                     headers={"Accept": ", ".join(REGISTRY_TYPES)})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
        if response.headers.get("Link"):
            raise ValueError("registry pagination is unknown; refusing cleanup")
        return (json.loads(body) if body else {}, dict(response.headers))


def registry_retention(protected_tags, now, *, port=5000) -> dict:
    """Build the complete manifest graph, including untagged OCI children and unknown roots."""
    result = {"retained": [], "candidates": [], "unknown": []}
    active = container_images()
    if protected_tags:
        active.update(item["Id"] for item in json.loads(command("docker", "image", "inspect", *sorted(protected_tags))))
    repositories, _ = registry_request("_catalog?n=1000", port=port)
    for repo in repositories.get("repositories", []):
        if repo not in ("ai-trader-runtime", "ai-trader-sidecar"):
            result["unknown"].append(repo)
            continue
        repository = RUNTIME_ROOT / "registry/data/docker/registry/v2/repositories" / repo
        response, _ = registry_request(f"{repo}/tags/list?n=1000", port=port)
        nodes, tagged = {}, {}

        def read_manifest(reference):
            manifest, headers = registry_request(f"{repo}/manifests/{reference}", port=port)
            digest = headers.get("Docker-Content-Digest")
            if not digest or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise ValueError("registry manifest digest missing")
            if digest not in nodes:
                if manifest.get("mediaType") not in REGISTRY_TYPES:
                    raise ValueError("unknown registry media type; refusing cleanup")
                nodes[digest] = {"manifest": manifest, "children": set(), "created": 0, "active": digest in active}
                node = nodes[digest]
                for child in manifest.get("manifests", []):
                    child_digest = read_manifest(child["digest"])
                    node["children"].add(child_digest)
                if "config" in manifest:
                    config_digest = manifest["config"]["digest"]
                    config, _ = registry_request(f"{repo}/blobs/{config_digest}", port=port)
                    node["active"] = node["active"] or config_digest in active
                    if config.get("created"):
                        node["created"] = created_at(config["created"])
            return digest

        for tag in response.get("tags") or []:
            tagged.setdefault(read_manifest(tag), []).append(tag)
        # Only read link identities; never modify Distribution's storage layout. An untagged
        # manifest not reachable from known tagged roots is independent material and stays.
        revisions = repository / "_manifests/revisions/sha256"
        if not revisions.is_dir():
            raise ValueError("registry revision inventory unavailable; refusing cleanup")
        for link in revisions.glob("*/link"):
            digest = link.read_text().strip()
            read_manifest(digest)

        def reachable(digest, visiting=None):
            visiting = set() if visiting is None else visiting
            if digest in visiting:
                raise ValueError("cyclic registry manifest graph")
            reached = {digest}
            for child in nodes[digest]["children"]:
                reached.update(reachable(child, visiting | {digest}))
            return reached

        children = set().union(*(node["children"] for node in nodes.values())) if nodes else set()
        marker = RUNTIME_ROOT / "maintenance/registry-gc-in-progress"
        pending = json.loads(marker.read_text()).get("remaining", []) if marker.exists() else []
        pending_roots = {item["digest"] for item in pending if item["repository"] == repo} & set(nodes)
        unknown_roots = set(nodes) - set(tagged) - children - pending_roots
        kept = set().union(*(reachable(d) for d in unknown_roots)) if unknown_roots else set()
        for digest, node in nodes.items():
            if node["active"]:
                kept.update(reachable(digest))
        result["unknown"].extend(f"{repo}@{d}" for d in sorted(unknown_roots))
        rows = []
        for digest, tags in tagged.items():
            reached = reachable(digest)
            created = max(nodes[d]["created"] for d in reached)
            protected = digest in kept or any(nodes[d]["active"] for d in reached)
            for tag in tags:
                link = repository / "_manifests/tags" / tag / "current/link"
                if not re.fullmatch(r"[a-f0-9]{40}", tag):
                    protected = True
                    result["unknown"].append(f"{repo}:{tag}")
                if not link.is_file() or now - max(created, link.stat().st_mtime) < 3600:
                    protected = True
            if any(nodes[d]["manifest"].get("mediaType") not in REGISTRY_TYPES for d in reached):
                protected = True
            rows.append({"repository": repo, "digest": digest, "tags": tags,
                         "protected": protected, "created": created})
        newest = sorted(rows, key=lambda row: row["created"], reverse=True)[:3]
        for row in rows:
            if row["protected"] or row in newest:
                kept.update(reachable(row["digest"]))
        deleting = set().union(*(reachable(row["digest"]) for row in rows if row["digest"] not in kept)) if rows else set()
        for digest in pending_roots:
            deleting.update(reachable(digest))
        deleting -= kept
        # A deleted parent can release a child only after every protected root's reachability
        # has been subtracted. Default GC then preserves every other untagged manifest.
        for digest in sorted(nodes, key=lambda d: len(reachable(d)), reverse=True):
            item = {"repository": repo, "digest": digest, "tags": tagged.get(digest, [])}
            result["candidates" if digest in deleting else "retained"].append(item)
    return result


def registry_uploads_active(now: float) -> bool:
    uploads = RUNTIME_ROOT / "registry/data/docker/registry/v2/repositories"
    return any(now - path.stat().st_mtime < 3600 for directory in uploads.glob("*/_uploads/*")
               if directory.is_dir() for path in (directory, *directory.rglob("*")))


def registry_ready(container: str) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            command("docker", "exec", container, "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:5000/v2/")
            return
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def write_registry_state(marker: Path, state: dict) -> None:
    temporary = marker.with_suffix(".tmp")
    with temporary.open("w") as record:
        json.dump(state, record)
        record.flush()
        os.fsync(record.fileno())
    temporary.replace(marker)


def remove_registry_worker(expected: dict) -> None:
    existing = command("docker", "ps", "-aq", "--filter", f"name=^/{REGISTRY_WORKER}$")
    if not existing:
        return
    worker = json.loads(command("docker", "inspect", existing))[0]
    owned = ((worker["Config"].get("Labels") or {}).get("ai-trader.maintenance-token") == expected.get("token")
             and expected.get("token") and worker["Image"] == expected["image"]
             and (str(RUNTIME_ROOT / "registry/data"), "/var/lib/registry") in {
                 (mount["Source"], mount["Destination"]) for mount in worker["Mounts"]})
    if not owned:
        raise ValueError("unowned maintenance container preserved; manual inspection required")
    command("docker", "rm", "-f", worker["Id"])


def recover_registry() -> None:
    marker = RUNTIME_ROOT / "maintenance/registry-gc-in-progress"
    if marker.exists():
        expected = json.loads(marker.read_text())
        original = json.loads(command("docker", "inspect", REGISTRY_CONTAINER))[0]
        if original["Id"] != expected["container"] or original["Image"] != expected["image"]:
            raise ValueError("registry changed during maintenance; manual recovery required")
        # Both normal finally and systemd ExecStopPost use this exact recovery path.
        foreign_worker = False
        try:
            remove_registry_worker(expected)
        except ValueError:
            foreign_worker = True
        if not original["State"]["Running"]:
            command("docker", "start", REGISTRY_CONTAINER)
        registry_ready(REGISTRY_CONTAINER)
        if foreign_worker:
            raise ValueError("unowned maintenance container preserved; manual inspection required")
        if not expected.get("gcNeeded"):
            marker.unlink()


def collect_registry(plan: dict, protected_tags, now: float) -> dict:
    marker = RUNTIME_ROOT / "maintenance/registry-gc-in-progress"
    pending = json.loads(marker.read_text()) if marker.exists() else None
    if not plan["candidates"] and not pending:
        return {"status": "unchanged", "removed": []}
    if registry_uploads_active(now):
        return {"status": "deferred_upload", "removed": []}
    meta = json.loads(command("docker", "inspect", REGISTRY_CONTAINER))[0]
    expected_image_id = command("docker", "image", "inspect", "--format", "{{.Id}}", REGISTRY_IMAGE)
    if not meta["State"]["Running"] or meta["Image"] != expected_image_id:
        raise ValueError("unexpected registry state/image; refusing cleanup")
    mounts = {(m["Source"], m["Destination"]) for m in meta["Mounts"]}
    if (str(RUNTIME_ROOT / "registry/data"), "/var/lib/registry") not in mounts:
        raise ValueError("unexpected registry data mount")
    if command("docker", "ps", "-aq", "--filter", f"name=^/{REGISTRY_WORKER}$"):
        raise ValueError("maintenance container name already occupied; refusing cleanup")
    remaining = [*plan["candidates"], *((pending or {}).get("remaining", []))]
    remaining = list({(p["repository"], p["digest"]): p for p in remaining}.values())
    state = {"container": meta["Id"], "image": expected_image_id,
             "gcNeeded": bool(pending and pending.get("gcNeeded")), "remaining": remaining, "token": uuid.uuid4().hex}
    if pending and (pending["container"] != meta["Id"] or pending["image"] != meta["Image"]):
        raise ValueError("pending GC belongs to another registry")
    write_registry_state(marker, state)
    removed = []
    try:
        command("docker", "stop", "--time", "30", REGISTRY_CONTAINER)
        if registry_uploads_active(time.time()):
            return {"status": "deferred_upload", "removed": []}
        # Original push/pull port is offline. Only our private loopback API can write now.
        command("docker", "run", "-d", "--name", REGISTRY_WORKER,
                "--label", "ai-trader.maintenance-token=" + state["token"],
                "--publish", "127.0.0.1:15001:5000", "--volumes-from", REGISTRY_CONTAINER,
                "--env", "REGISTRY_STORAGE_DELETE_ENABLED=true", REGISTRY_IMAGE)
        registry_ready(REGISTRY_WORKER)
        # Re-enumerate after freezing writers. New/unknown aliases fail closed per digest.
        frozen = registry_retention(protected_tags, now, port=15001)
        approved = {(p["repository"], p["digest"]) for p in remaining}
        for item in frozen["candidates"]:
            key = (item["repository"], item["digest"])
            if key in approved:
                # Persist before DELETE: a disconnect after a successful response is retryable.
                state["gcNeeded"] = True
                write_registry_state(marker, state)
                registry_request(f'{key[0]}/manifests/{key[1]}', port=15001, method="DELETE")
                removed.append(item)
                state["remaining"] = [p for p in state["remaining"] if (p["repository"], p["digest"]) != key]
                write_registry_state(marker, state)
        remove_registry_worker(state)
        if state["gcNeeded"]:
            # Official mark/sweep preserves shared layers and unknown/untagged manifests.
            command("docker", "run", "--rm", "--network", "none", "--name", REGISTRY_WORKER,
                    "--label", "ai-trader.maintenance-token=" + state["token"],
                    "--volumes-from", REGISTRY_CONTAINER, REGISTRY_IMAGE,
                    "garbage-collect", "/etc/docker/registry/config.yml")
        state["gcNeeded"] = False
        state["remaining"] = []
        write_registry_state(marker, state)
        return {"status": "ok", "removed": removed}
    finally:
        recover_registry()


def command(*args: str) -> str:
    return subprocess.check_output(args, universal_newlines=True, stderr=subprocess.DEVNULL).strip()


def busy_processes(proc: Path = Path("/proc")) -> bool:
    # Check exact executable names; do not match this script or a shell command string.
    names = {"Runner.Worker", "rustc", "cargo", "buildkit-runc", "pyinstaller", "makensis"}
    for entry in proc.glob("[0-9]*/comm"):
        try:
            if entry.read_text().strip() in names:
                return True
            arguments = (entry.parent / "cmdline").read_bytes().split(b"\0")
            if any(re.fullmatch(r"deploy-(?:release|[a-f0-9]{7,40})\.sh", Path(os.fsdecode(arg)).name)
                   for arg in arguments if arg):
                return True
            if arguments and Path(os.fsdecode(arguments[0])).name in {"docker", "docker-buildx"}:
                if b"build" in arguments:
                    return True
        except (FileNotFoundError, PermissionError):
            continue
    return False


def open_release_paths(proc: Path = Path("/proc"), roots=None) -> set:
    paths = set()
    roots = roots or (UPDATE_ROOT / "releases", RUNTIME_ROOT / "releases", RUNTIME_ROOT / "tmp")
    for entry in [*proc.glob("[0-9]*/fd/*"), *proc.glob("[0-9]*/cwd")]:
        try:
            target = Path(os.readlink(entry))
            if any(root == target or root in target.parents for root in roots):
                paths.add(target)
        except (OSError, ValueError):
            continue
    return paths


def retained_releases(root: Path, keep: int, now: float) -> tuple:
    directory = root / "releases"
    releases = [p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()
                and re.fullmatch(r"\d+\.\d+\.\d+-[a-f0-9]{16}", p.name)]
    # File mtime is publication time, independent of lexical version ordering.
    releases.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    protected = {p.name for p in releases[:keep]}
    manifest = json.loads((root / "latest.json").read_text())
    for platform in manifest["platforms"].values():
        match = re.search(r"/releases/([^/]+)/", platform["url"])
        if not match or not (directory / match.group(1)).is_dir():
            raise ValueError("current update artifact is missing; refusing cleanup")
        protected.add(match.group(1))
        # Keep bases explicitly referenced by a published differential update.
        for patch in platform.get("patches", []):
            if patch.get("baseRelease"):
                protected.add(patch["baseRelease"])
    # Grace period covers clients that checked before a new publication.
    protected.update(p.name for p in releases if now - p.stat().st_mtime < 48 * 3600)
    return protected, [p for p in releases if p.name not in protected]


def runtime_releases(root: Path, keep: int, now: float, container_paths=()) -> tuple:
    directory = root / "releases"
    current = (root / "current").resolve(strict=True)
    if current.parent != directory.resolve() or not re.fullmatch(r"[a-f0-9]{40}", current.name):
        raise ValueError("invalid current Runtime release; refusing cleanup")
    releases = [p for p in directory.iterdir() if not p.is_symlink() and p.is_dir()
                and re.fullmatch(r"[a-f0-9]{40}", p.name)]
    releases.sort(key=lambda p: (p / "deploy/vps/images.yml").stat().st_mtime
                  if (p / "deploy/vps/images.yml").is_file() else p.stat().st_mtime, reverse=True)
    referenced = {p.name for p in releases if any(p == f or p in f.parents for f in container_paths)}
    # A deploy writes images.yml right after both images build, before anything starts from the
    # release. An older directory without it (not current, no container) is a failed build: it
    # neither counts toward the kept releases nor blocks the sweep; it is removed like any other.
    failed = {p.name for p in releases if p.name != current.name and p.name not in referenced
              and not (p / "deploy/vps/images.yml").exists() and now - p.stat().st_mtime >= 3600}
    ranked = [p for p in releases if p.name not in failed]
    protected = {current.name, *(p.name for p in ranked[:keep])} | referenced
    # A just-uploaded candidate may not have an images manifest yet.
    protected.update(p.name for p in releases if now - p.stat().st_mtime < 3600)
    opened = open_release_paths(roots=(directory,))
    protected.update(p.name for p in releases if any(f == p or p in f.parents for f in opened))
    tags = set()
    for p in releases:
        if p.name not in protected:
            continue
        manifest = p / "deploy/vps/images.yml"
        if not manifest.exists():
            if p != current and now - p.stat().st_mtime < 3600:
                continue
            raise ValueError("retained Runtime image manifest missing; refusing cleanup")
        release_tags = re.findall(r"^\s*image:\s*(ai-trader-(?:runtime|sidecar):[a-f0-9]{7,40})\s*$",
                                  manifest.read_text(), re.MULTILINE)
        if not any(tag.startswith("ai-trader-runtime:") for tag in release_tags):
            raise ValueError("retained Runtime image manifest is invalid; refusing cleanup")
        tags.update(release_tags)
    if not any(tag.startswith("ai-trader-runtime:") for tag in tags):
        raise ValueError("Runtime image manifest is invalid; refusing cleanup")
    return protected, [p for p in releases if p.name not in protected], tags


def container_images() -> set:
    # Stopped containers also count: they can be an intentional rollback candidate.
    containers = command("docker", "ps", "-aq").splitlines()
    return set(command("docker", "inspect", "--format", "{{.Image}}", *containers).splitlines()) if containers else set()


def managed_rows(repository: str, length: str, tagged: bool = False) -> list:
    # (created, tag, image id) for the sha tags of one managed repository; anything else under it
    # (latest, a manual tag, a shorter hex where full shas are expected) is never returned.
    # `tagged` swaps the first field for the image's last-tag time when it is later than Created: a
    # release whose build was fully cached keeps an old Created but is tagged again at release time.
    tags = command("docker", "image", "ls", repository, "--format", "{{.Repository}}:{{.Tag}}").splitlines()
    tags = sorted({tag for tag in tags if re.fullmatch(re.escape(repository) + r":[a-f0-9]" + length, tag)})
    if not tags:
        return []
    metadata = json.loads(command("docker", "image", "inspect", *tags))
    rows = []
    for tag, meta in zip(tags, metadata, strict=True):
        created = meta["Created"]
        last_tag = (meta.get("Metadata") or {}).get("LastTagTime") or ""
        if tagged and last_tag and created_at(last_tag) > created_at(created):
            created = last_tag
        rows.append((created, tag, meta["Id"]))
    return rows


def created_at(created: str) -> float:
    # Docker writes nanosecond fractions, which fromisoformat accepts only up to microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", created.replace("Z", "+00:00"))
    return datetime.fromisoformat(text).timestamp()


def stale_image_tags(protected_tags=(), now=None) -> list:
    now = time.time() if now is None else now
    active = container_images()
    if protected_tags:
        # Missing rollback images fail closed instead of silently dropping protection.
        active.update(item["Id"] for item in json.loads(command("docker", "image", "inspect", *sorted(protected_tags))))
    # Managed aliases on the production daemon (#668): the two short compose names plus the two
    # full-sha registry repositories the NAS flow pulls under. Every release leaves both kinds
    # naming the same image, so an old image only becomes reclaimable when all its managed
    # aliases go; removing just one alias used to pin the image forever. Only these four
    # repositories are ever enumerated, and the registry repositories only ever receive full
    # 40-hex release aliases — anything shorter there (a test push, a manual tag) is unknown and
    # untouched, like unknown repositories and non-sha tags under the bare names.
    repositories = ("ai-trader-runtime", "ai-trader-sidecar",
                    f"{MANAGED_REGISTRY}/ai-trader-runtime", f"{MANAGED_REGISTRY}/ai-trader-sidecar")
    rows = [row for repository in repositories for row in managed_rows(
        repository, r"{40}" if repository.startswith(MANAGED_REGISTRY + "/") else r"{7,40}")]
    # Protection is per image identity across every managed alias: current and rollback images,
    # running and stopped containers, anything an hour old or younger, and the newest three
    # distinct images of each service keep every alias that names them.
    rows.sort(reverse=True)
    retained = set(active)
    newest = {}
    for _, tag, identifier in rows:
        service = tag.rsplit(":", 1)[0].rsplit("/", 1)[-1]
        identities = newest.setdefault(service, [])
        if identifier not in identities:
            identities.append(identifier)
    for identities in newest.values():
        retained.update(identities[:3])
    retained.update(identifier for created, _, identifier in rows if now - created_at(created) < 3600)
    return [tag for _, tag, identifier in rows if identifier not in retained]


def stale_nas_tags(protected_shas, keep=3, now=None) -> list:
    """NAS build host: full-sha push refs outside the retained release set (#668).

    The unit is the release sha (both images of one release go together): the shas the caller
    protects (production current and rollback), the newest `keep` complete shas (both images
    present) by release time — the image's last-tag time, so a fully cached rebuild of an old
    image still counts as a fresh release — and any sha tagged within the last hour (a release in
    progress elsewhere) keep both refs, as does every sha one of whose images a container uses.
    A sha with only one image never takes one of the `keep` places. Only the two 127.0.0.1:15000
    repositories and their 40-hex tags are managed; base images, other repositories and other tags
    are never enumerated.
    """
    now = time.time() if now is None else now
    protected = set(protected_shas)
    if not protected or any(not re.fullmatch(r"[a-f0-9]{40}", sha) for sha in protected):
        raise ValueError("NAS retention needs the full production current/rollback shas; refusing cleanup")
    active = container_images()
    repos = ("ai-trader-runtime", "ai-trader-sidecar")
    rows = [row for repo in repos for row in managed_rows(f"{NAS_PUSH_PREFIX}/{repo}", r"{40}", tagged=True)]
    built, present, in_use = {}, {}, set()
    for created, tag, identifier in rows:
        repository, sha = tag.rsplit(":", 1)
        built[sha] = max(built.get(sha, 0.0), created_at(created))
        present.setdefault(sha, set()).add(repository.rsplit("/", 1)[-1])
        if identifier in active:
            in_use.add(sha)
    complete = [sha for sha in built if present[sha] == set(repos)]
    retained = protected | in_use | set(sorted(complete, key=built.get, reverse=True)[:keep])
    retained.update(sha for sha, at in built.items() if now - at < 3600)
    return [tag for _, tag, _ in sorted(rows, reverse=True) if tag.rsplit(":", 1)[1] not in retained]


def maintain_nas(*, apply: bool, protected_shas, keep: int = 3) -> dict:
    # Runs on the NAS build host itself (deploy-release.sh calls it after each verified push).
    before = shutil.disk_usage("/")
    result = {"mode": "apply" if apply else "plan", "freeBefore": before.free, "removed": [],
              "protectedShas": sorted(protected_shas), "keep": keep}
    if busy_processes():
        return {**result, "status": "deferred_busy"}
    images = stale_nas_tags(protected_shas, keep)
    result["imageCandidates"] = images
    # A bounded cache keeps the next release's incremental build; only the excess is pruned.
    result["buildCachePolicy"] = f"keep at most {NAS_CACHE_LIMIT}"
    if apply:
        for tag in images:
            if busy_processes():
                return {**result, "status": "deferred_busy"}
            # By tag, never forced: an image still named elsewhere or used by a container stays.
            subprocess.run(["docker", "image", "rm", tag], check=True, stdout=subprocess.DEVNULL)
            result["removed"].append(tag)
        if not busy_processes():
            command("docker", "buildx", "prune", "--force", "--max-used-space", NAS_CACHE_LIMIT)
    after = shutil.disk_usage("/")
    result.update(freeAfter=after.free, status="low_space" if after.free < 8 * GIB else "ok")
    return result


def maintain(*, apply: bool, docker: bool, journal: bool, storage: bool = False) -> dict:
    root = RUNTIME_ROOT if docker else UPDATE_ROOT
    before = shutil.disk_usage(root)
    result = {"mode": "apply" if apply else "plan", "freeBefore": before.free, "removed": []}
    def busy():
        return busy_processes() or (UPDATE_ROOT / ".publishing").exists() or (RUNTIME_ROOT / ".deploying").exists()
    marker = RUNTIME_ROOT / ".deploying"
    if marker.exists() and time.time() - marker.stat().st_mtime > 2 * 3600 and not busy_processes():
        # No deploy runs this long: a leftover marker must fail the unit instead of deferring forever.
        return {**result, "status": "stale_deploy_marker",
                "detail": f"inspect and remove {marker} once no deploy is running"}
    if busy():
        return {**result, "status": "deferred_busy"}
    if storage and apply:
        recover_registry()
    protected, candidates, protected_images = set(), [], set()
    if UPDATE_ROOT.exists():
        protected, candidates = retained_releases(UPDATE_ROOT, 3, time.time())
    if docker:
        containers = command("docker", "ps", "-aq").splitlines()
        references = command("docker", "inspect", "--format",
                             '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
                             *containers).splitlines() if containers else []
        paths = [Path(p) for line in references for p in line.split(",") if p.startswith("/")]
        runtime_keep, runtime_candidates, protected_images = runtime_releases(RUNTIME_ROOT, 3, time.time(), paths)
        result["runtimeRetained"] = sorted(runtime_keep)
        candidates.extend(runtime_candidates)
        temporary = RUNTIME_ROOT / "tmp"
        if temporary.exists():
            candidates.extend(p for p in temporary.iterdir() if p.is_dir() and not p.is_symlink()
                              and re.fullmatch(r"agent-[a-z0-9][a-z0-9-]{0,80}", p.name)
                              and time.time() - p.stat().st_mtime > 72 * 3600)
    opened = open_release_paths()
    candidates = [p for p in candidates if not any((f == p or p in f.parents) for f in opened)]
    result["retained"] = sorted(protected)
    result["releaseCandidates"] = [str(p) for p in candidates]
    images = stale_image_tags(protected_images) if docker else []
    result["imageCandidates"] = images
    result["buildCachePolicy"] = "unused for 24h; retain 2GB" if docker else None
    if storage:
        result["backups"] = backup_retention(RUNTIME_ROOT, time.time(), runtime_keep)
        result["registry"] = registry_retention(protected_images, time.time())
        if registry_uploads_active(time.time()):
            return {**result, "status": "deferred_upload"}
    if apply:
        if storage:
            # Plan and mutation both run under main's shared deployment lock. Deleting snapshots
            # happens only after a retained sibling's full integrity/schema/count proof succeeds.
            for name in result["backups"]["candidates"]:
                if busy():
                    return {**result, "status": "deferred_busy"}
                path = Path(name)
                retained_backups_unchanged(result["backups"])
                if not backup_idle(path, open_release_paths(roots=(RUNTIME_ROOT,))):
                    continue
                if path.is_symlink() or file_identity(path) != result["backups"]["identities"][name]:
                    raise ValueError("backup changed after planning; refusing cleanup")
                path.unlink()
                result["removed"].append(name)
            result["registryResult"] = collect_registry(result["registry"], protected_images, time.time())
        for path in candidates:
            if busy():
                return {**result, "status": "deferred_busy"}
            # Recheck open download descriptors immediately before deletion.
            if any((f == path or path in f.parents) for f in open_release_paths()):
                continue
            shutil.rmtree(path)
            result["removed"].append(str(path))
        for tag in images:
            if busy():
                return {**result, "status": "deferred_busy"}
            # Docker itself rejects images still needed by containers. Never force. Unbinding a
            # managed daemon alias only reclaims the daemon's image store; the registry's own
            # ./data keeps its manifests and blobs until a separate registry-side GC decision.
            subprocess.run(["docker", "image", "rm", tag], check=True, stdout=subprocess.DEVNULL)
        if docker and not busy():
            command("docker", "buildx", "prune", "--force", "--filter", "until=24h", "--max-used-space", "2GB")
        if journal and not busy():
            command("journalctl", "--vacuum-size=512M")
    after = shutil.disk_usage(root)
    result.update(freeAfter=after.free, status="low_space" if after.free < 8 * GIB else "ok")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--docker", action="store_true", help="Runtime/build host: releases, images and build cache")
    parser.add_argument("--journal", action="store_true")
    parser.add_argument("--nas", action="store_true",
                        help="NAS build host: 127.0.0.1:15000 release refs and build cache only")
    parser.add_argument("--protect", action="append", default=[], metavar="SHA",
                        help="with --nas: a full release sha to keep (production current and rollback)")
    parser.add_argument("--keep", type=int, default=3, help="with --nas: newest distinct release shas to keep")
    parser.add_argument("--storage", action="store_true", help="with --docker: verified backup families and registry GC")
    parser.add_argument("--recover-registry", action="store_true", help="systemd failure recovery for an interrupted GC")
    args = parser.parse_args()
    if args.storage and not args.docker:
        parser.error("--storage requires --docker")
    if args.nas and (args.docker or args.journal):
        parser.error("--nas runs alone")
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    with open("/run/lock/ai-trader-update-maintenance.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "deferred_busy"}))
            return 0
        if args.recover_registry:
            recover_registry()
            return 0
        if args.nas:
            result = maintain_nas(apply=args.apply, protected_shas=args.protect, keep=args.keep)
        else:
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
            result = maintain(apply=args.apply, docker=args.docker, journal=args.journal, storage=args.storage)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["status"] in ("low_space", "stale_deploy_marker") else 0


if __name__ == "__main__":
    raise SystemExit(main())
