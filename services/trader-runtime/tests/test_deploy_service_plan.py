"""Which sidecar-namespace containers a release recreates (deploy/vps/service_plan.py).

2026-10-12 (#793): extra_hosts was added to tqkq-sidecar, but deploy-release.sh recreated the
sidecar only when its source changed, so the running sidecar kept the old definition and the
Runtime (which shares its /etc/hosts) never saw the entry. The plan now compares each running
container's compose config hash with this release's, the image pinned to the running one. These
tests drive it through a fake docker that hashes service definitions the way compose does: the
whole definition including image and the resolved network namespace.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("service_plan", REPO_ROOT / "deploy" / "vps" / "service_plan.py")
assert SPEC and SPEC.loader
plan = importlib.util.module_from_spec(SPEC)
sys.modules["service_plan"] = plan  # dataclasses resolve their module by name
SPEC.loader.exec_module(plan)

PROJECT = "ai-trader-production"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml", "-f", "images.yml"]
RUNTIME_ENV = {"AI_TRADER_DEBATE_JUDGE_MODEL": "deepseek-flash", "AI_TRADER_READONLY_SIDECAR_URL": "http://127.0.0.1:19001"}


def release(tag: str) -> dict[str, dict]:
    """The services one release's compose files resolve to (the parts these tests vary)."""
    slot = {"image": f"ai-trader-runtime:{tag}", "network_mode": "service:tqkq-sidecar", "environment": dict(RUNTIME_ENV)}
    return {
        "tqkq-sidecar": {"image": f"ai-trader-sidecar:{tag}", "ports": ["127.0.0.1:18000:8000", "127.0.0.1:18001:8001"],
                         "environment": {"AI_TRADER_TQKQ_SYMBOLS": "SHFE.au2612"}},
        "tqkq-pool": {"image": f"ai-trader-sidecar:{tag}", "network_mode": "service:tqkq-sidecar",
                      "environment": {"AI_TRADER_TQKQ_POOL_MAX": "8"}},
        "paper-engine": {"image": f"ai-trader-sidecar:{tag}", "network_mode": "service:tqkq-sidecar"},
        "trader-runtime-blue": copy.deepcopy(slot),
        "trader-runtime-green": copy.deepcopy(slot),
        "rsshub-tunnel": {"image": "alpine/socat@sha256:82ad", "network_mode": "host"},
    }


def compose_hash(definition: dict) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


class FakeDocker:
    """`docker ps/inspect` over containers created from `deployed`, `compose config` over `target`."""

    def __init__(self, deployed: dict[str, dict], target: dict[str, dict]) -> None:
        self.target = target
        self.containers: dict[str, dict] = {}
        owner_id = "c-tqkq-sidecar"
        for service in ("tqkq-sidecar", "tqkq-pool"):
            definition = copy.deepcopy(deployed[service])
            if definition.get("network_mode") == "service:tqkq-sidecar":
                definition["network_mode"] = f"container:{owner_id}"  # compose resolves before hashing
            self.containers[service] = {"Id": f"c-{service}", "Image": definition["image"],
                                        "Labels": {plan.HASH_LABEL: compose_hash(definition)}}

    def __call__(self, argv: list[str], stdin: str | None) -> str:
        if argv[:2] == ["docker", "ps"]:
            service = argv[-1].rsplit("=", 1)[1]
            return self.containers[service]["Id"] + "\n" if service in self.containers else ""
        if argv[:2] == ["docker", "inspect"]:
            found = next(c for c in self.containers.values() if c["Id"] == argv[-1])
            return json.dumps({"Image": found["Image"], "Labels": found["Labels"]})
        assert argv[: len(COMPOSE)] == COMPOSE
        rest = argv[len(COMPOSE):]
        if rest == ["config", "--services"]:
            return "\n".join(self.target) + "\n"
        assert rest[:2] == ["-f", "-"] and rest[2:4] == ["config", "--hash"] and stdin
        service = rest[4]
        definition = copy.deepcopy(self.target[service])
        for line in stdin.splitlines()[2:]:
            key, value = line.strip().split(": ", 1)
            definition[key] = json.loads(value)
        return f"{service} {compose_hash(definition)}\n"


def run_plan(deployed: dict[str, dict], target: dict[str, dict], *, source_changed: bool = False) -> dict[str, tuple[str, str]]:
    configured, running, wanted = plan.gather(COMPOSE, PROJECT, FakeDocker(deployed, target))
    steps = plan.decide(configured, running, wanted, source_changed, idle="green")
    result = {step.service: (step.action, step.reason) for step in steps if step.service not in ("trader-runtime-green",)}
    result["netns-recreate"] = plan.netns_recreate(steps)
    return result


def test_a_new_image_tag_with_the_same_source_and_definition_keeps_the_namespace() -> None:
    result = run_plan(release("aaaaaaa"), release("bbbbbbb"))
    assert result["tqkq-sidecar"][0] == "keep"
    assert result["tqkq-pool"][0] == "keep"
    assert result["netns-recreate"] == []


def test_a_runtime_only_change_leaves_the_sidecar_and_pool_running() -> None:
    target = release("bbbbbbb")
    for slot in ("trader-runtime-blue", "trader-runtime-green"):
        target[slot]["environment"]["AI_TRADER_DEBATE_JUDGE_MODEL"] = ""
    result = run_plan(release("aaaaaaa"), target)
    assert result["tqkq-sidecar"][0] == "keep" and result["tqkq-pool"][0] == "keep"
    assert result["trader-runtime-blue"][0] == "remove"
    assert result["netns-recreate"] == []


def test_a_shared_change_recreates_the_sidecar_with_its_members_and_the_serving_slot() -> None:
    # The Runtime's green port lives in both the slot and the sidecar that publishes it.
    target = release("bbbbbbb")
    target["tqkq-sidecar"]["ports"][1] = "127.0.0.1:18011:8001"
    target["trader-runtime-green"]["environment"]["AI_TRADER_RUNTIME_GREEN_PORT"] = "18011"
    result = run_plan(release("aaaaaaa"), target)
    assert result["tqkq-sidecar"][0] == "recreate"
    assert result["tqkq-sidecar"][1].startswith("definition changed")
    assert result["tqkq-pool"] == ("recreate", "joins the recreated tqkq-sidecar network namespace")
    assert result["netns-recreate"] == ["tqkq-sidecar", "tqkq-pool", "trader-runtime-green"]


def test_a_sidecar_only_definition_change_is_no_longer_skipped() -> None:
    # The #793 shape: extra_hosts on the sidecar, the sidecar source unchanged.
    target = release("bbbbbbb")
    target["tqkq-sidecar"]["extra_hosts"] = ["ai-runtime.qilinshuzhi.com:host-gateway"]
    result = run_plan(release("aaaaaaa"), target)
    assert result["tqkq-sidecar"][0] == "recreate"
    # Only the sidecar's definition changed; the members follow because the namespace is replaced.
    assert result["netns-recreate"] == ["tqkq-sidecar", "tqkq-pool", "trader-runtime-green"]


def test_a_member_only_change_recreates_that_member_alone() -> None:
    target = release("bbbbbbb")
    target["tqkq-pool"]["environment"]["AI_TRADER_TQKQ_POOL_MAX"] = "12"
    result = run_plan(release("aaaaaaa"), target)
    assert result["tqkq-sidecar"][0] == "keep"
    assert result["tqkq-pool"][0] == "recreate"
    assert result["netns-recreate"] == ["tqkq-pool"]


def test_a_sidecar_source_change_recreates_the_namespace_on_the_new_image() -> None:
    result = run_plan(release("aaaaaaa"), release("bbbbbbb"), source_changed=True)
    assert result["tqkq-sidecar"] == ("recreate", "sidecar source changed (new image)")
    assert result["netns-recreate"] == ["tqkq-sidecar", "tqkq-pool", "trader-runtime-green"]


def test_a_member_enabled_by_this_release_is_created() -> None:
    docker = FakeDocker(release("aaaaaaa"), release("bbbbbbb"))
    del docker.containers["tqkq-pool"]
    configured, running, wanted = plan.gather(COMPOSE, PROJECT, docker)
    steps = plan.decide(configured, running, wanted, False, idle="green")
    assert plan.netns_recreate(steps) == ["tqkq-pool"]
    assert next(step for step in steps if step.service == "tqkq-pool").action == "create"


@pytest.mark.parametrize("ignored", ["tests/test_main.py", "src/pkg/__pycache__/main.cpython-312.pyc"])
def test_the_source_digest_follows_shipped_files_only(tmp_path: Path, ignored: str) -> None:
    source = tmp_path / plan.SIDECAR_SOURCE
    (source / "src/pkg").mkdir(parents=True)
    (source / "src/pkg/main.py").write_text("A = 1\n")
    before = plan.source_digest(str(tmp_path))
    (source / ignored).parent.mkdir(parents=True, exist_ok=True)
    (source / ignored).write_text("changed\n")
    assert plan.source_digest(str(tmp_path)) == before
    (source / "src/pkg/main.py").write_text("A = 2\n")
    assert plan.source_digest(str(tmp_path)) != before
    assert plan.source_digest(str(tmp_path / "missing")) is None
