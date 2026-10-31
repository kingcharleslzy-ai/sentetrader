"""Which containers of the sidecar network namespace a release recreates, and why (deploy-release.sh).

tqkq-sidecar owns the network namespace the Runtime slots, tqkq-sidecar-2, tqkq-pool and the paper
engine join, and those containers keep their TqKq sessions across a deploy, so a release recreates
them only when something they are built from changed:

- the image: the sidecar source (services/tqkq-sidecar without tests) differs from the previous
  release's. Every release builds a new tag, so the tag itself says nothing; equal source reuses
  the running container's image as it is (no rebuild, no recreate for the image alone).
- the definition: anything else compose would create the container from — compose files, the
  overlays enabled in .env.production, interpolated variables, secrets, ports, extra_hosts. Judged
  by compose itself: the running container carries the `com.docker.compose.config-hash` compose
  stamped on it, and `docker compose config --hash` recomputes it for this release with the image
  pinned to the one the container runs and `network_mode: service:tqkq-sidecar` resolved to the
  running sidecar's container, exactly as compose resolves it before hashing. Equal hashes mean
  compose would create the same container. (2026-10-12: #793 added extra_hosts to tqkq-sidecar;
  the old source-only rule kept the old sidecar and the Runtime never saw the new hosts entry.)

Recreating tqkq-sidecar replaces the namespace, so every member running in it — the serving
Runtime slot included — is recreated with it. A member whose own definition changed is recreated
alone. The Runtime slots always roll blue-green, the paper engine always takes the release image,
and the RSSHub / MT5 relays converge through compose; those lines are reported, not decided here.

    python3 service_plan.py --project P --previous DIR --release DIR --idle blue|green -- docker compose ... -f ...

prints one line per service (`service  action  reason`) and last `netns-recreate: <services>` — the
list deploy-release.sh passes to `up --force-recreate`, empty when the namespace stays.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

OWNER = "tqkq-sidecar"
MEMBERS = ("tqkq-sidecar-2", "tqkq-pool")
SIDECAR_SOURCE = "services/tqkq-sidecar"
HASH_LABEL = "com.docker.compose.config-hash"


@dataclass(frozen=True)
class Running:
    id: str
    image: str
    config_hash: str


@dataclass(frozen=True)
class Step:
    service: str
    action: str
    reason: str


def source_digest(root: str) -> str | None:
    """Digest of the sidecar source under a release tree (tests and bytecode excluded); None when absent."""
    top = os.path.join(root, SIDECAR_SOURCE)
    if not os.path.isdir(top):
        return None
    digest = hashlib.sha256()
    for directory, subdirs, files in os.walk(top):
        subdirs[:] = sorted(name for name in subdirs if name not in ("tests", "__pycache__"))
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            path = os.path.join(directory, name)
            digest.update(os.path.relpath(path, root).encode() + b"\0")
            with open(path, "rb") as handle:
                digest.update(hashlib.sha256(handle.read()).digest())
    return digest.hexdigest()


def pinned_override(service: str, running: Running, owner_id: str | None) -> str:
    """The compose override that makes `config --hash` describe the running image and namespace."""
    lines = ["services:", f"  {service}:", f"    image: {json.dumps(running.image)}"]
    if service != OWNER and owner_id:
        lines.append(f"    network_mode: {json.dumps('container:' + owner_id)}")
    return "\n".join(lines) + "\n"


def decide(configured: list[str], running: dict[str, Running], wanted: dict[str, str],
           source_changed: bool, idle: str) -> list[Step]:
    """The plan from gathered facts. `wanted[s]` is this release's hash for a running service s,
    computed with its running image and namespace (see pinned_override)."""
    active = "blue" if idle == "green" else "green"

    def own_change(service: str) -> str | None:
        current = running.get(service)
        if current is None:
            return "not running"
        if wanted.get(service) != current.config_hash:
            before, after = current.config_hash[:8] or "none", (wanted.get(service) or "?")[:8]
            return f"definition changed (compose config hash {before} → {after})"
        return None

    steps = [Step(f"trader-runtime-{idle}", "start", "blue-green: the idle slot starts on the release image"),
             Step(f"trader-runtime-{active}", "remove", "blue-green: the old slot stops after the cutover")]
    owner_reasons = [reason for reason in (
        "sidecar source changed (new image)" if source_changed else None, own_change(OWNER)) if reason]
    owner_recreated = bool(owner_reasons)
    steps.append(Step(OWNER, ("create" if OWNER not in running else "recreate") if owner_recreated else "keep",
                      "; ".join(owner_reasons) or "same image source and definition"))
    for member in MEMBERS:
        if member not in configured:
            continue
        if owner_recreated:
            steps.append(Step(member, "recreate", f"joins the recreated {OWNER} network namespace"))
            continue
        reason = own_change(member)
        steps.append(Step(member, ("create" if member not in running else "recreate") if reason else "keep",
                          reason or "same image source and definition"))
    if owner_recreated:
        steps.append(Step(f"trader-runtime-{idle}", "recreate",
                          f"the new serving slot joins the recreated {OWNER} network namespace"))
    if "paper-engine" in configured:
        steps.append(Step("paper-engine", "recreate", "always: takes the release image, joins the namespace last"))
    for relay in ("rsshub-tunnel", "mt5-tunnel"):
        if relay in configured:
            steps.append(Step(relay, "converge", "compose recreates it only when its definition changed"))
    return steps


def netns_recreate(steps: list[Step]) -> list[str]:
    """The services deploy-release.sh force-recreates in the sidecar namespace step."""
    group = {OWNER, *MEMBERS}
    names = []
    for step in steps:
        in_group = step.service in group or (step.action == "recreate" and step.service.startswith("trader-runtime-"))
        if in_group and step.action in ("create", "recreate") and step.service not in names:
            names.append(step.service)
    return names


Command = Callable[[list[str], str | None], str]


def _run(argv: list[str], stdin: str | None = None) -> str:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, check=True).stdout


def gather(compose: list[str], project: str, run: Command = _run) -> tuple[list[str], dict[str, Running], dict[str, str]]:
    """Configured services, running containers and this release's pinned hashes, from docker itself."""
    configured = run([*compose, "config", "--services"], None).split()
    running: dict[str, Running] = {}
    for service in (OWNER, *MEMBERS):
        ids = run(["docker", "ps", "-q", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}",
                   "--filter", f"label=com.docker.compose.service={service}"], None).split()
        if not ids:
            continue
        info = json.loads(run(["docker", "inspect", "--format", "{{json .Config}}", ids[0]], None))
        running[service] = Running(ids[0], info.get("Image") or "", (info.get("Labels") or {}).get(HASH_LABEL, ""))
    owner_id = running[OWNER].id if OWNER in running else None
    wanted: dict[str, str] = {}
    for service, current in running.items():
        if service not in configured:
            continue
        out = run([*compose, "-f", "-", "config", "--hash", service], pinned_override(service, current, owner_id))
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == service:
                wanted[service] = parts[1]
    return configured, running, wanted


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print("usage: service_plan.py --project P --previous DIR --release DIR --idle blue|green -- <compose command>",
              file=sys.stderr)
        return 2
    split = argv.index("--")
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--idle", choices=("blue", "green"), required=True)
    args = parser.parse_args(argv[:split])
    compose = argv[split + 1:]
    previous, release = source_digest(args.previous), source_digest(args.release)
    # Unknown previous source (no tree to compare): the image counts as unchanged, as before.
    source_changed = previous is not None and previous != release
    configured, running, wanted = gather(compose, args.project)
    steps = decide(configured, running, wanted, source_changed, args.idle)
    width = max(len(step.service) for step in steps)
    for step in steps:
        print(f"  {step.service.ljust(width)}  {step.action.ljust(8)}  {step.reason}")
    print("netns-recreate: " + " ".join(netns_recreate(steps)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
