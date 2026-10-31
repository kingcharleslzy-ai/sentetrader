"""使用说明 (2026-09-25 两层重做): the plain layer stays plain and short, old ids keep working,
and the help API / UI command return the redesigned structure."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from trader_runtime import app_help
from trader_runtime.app_help import HELP, HELP_ALIASES, HELP_GROUPS, HelpCatalogError, load_catalog, resolve_topic
from trader_runtime.main import create_app
from trader_runtime.ui_control import UiChanges, _surface_matches

# The help text itself (2026-10-12: data, not code). Named by path so a text-only edit selects this file.
CATALOG = Path(__file__).resolve().parents[1] / "src/trader_runtime/help/catalog.json"
HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer help-token"}
# Ids the client, older clients, UI commands and agents already use.
OLD_IDS = ("settings", "preferences", "ai-model", "admin", "connection", "replay", "chart", "depth", "briefs",
           "activity", "research", "agent", "ui-commands", "voice-realtime", "tour", "strategy", "strategy-glossary",
           "skills", "workspace", "appearance")
# The plain layer names no routes, code spans or snake_case identifiers.
TECHNICAL = re.compile(r"/api/|`|\b[a-z][a-z0-9]*_[a-z0-9_]+\b")


def plain_layer(item):
    yield item["summary"]
    yield from item["steps"]
    for entry in item["faq"]:
        yield entry["q"]
        yield entry["a"]
    for term in item.get("terms", []):
        yield from (term["term"], term["plain"], term["when"])


def test_every_topic_has_a_short_plain_layer_and_a_detail():
    groups = {group["id"] for group in HELP_GROUPS}
    for topic, item in HELP.items():
        assert item["group"] in groups, topic
        assert item["title"] and item["detail"], topic
        assert 0 < len(item["summary"]) <= 60, (topic, len(item["summary"]))
        assert 3 <= len(item["steps"]) <= 5 and all(0 < len(step) <= 40 for step in item["steps"]), topic
        assert 2 <= len(item["faq"]) <= 4 and all(entry["q"] and 0 < len(entry["a"]) <= 80 for entry in item["faq"]), topic
        for text in plain_layer(item):
            assert not TECHNICAL.search(text), (topic, text)
    # Topics are listed group by group, and every group has at least one topic.
    order = [group["id"] for group in HELP_GROUPS]
    assert [item["group"] for item in HELP.values()] == sorted((item["group"] for item in HELP.values()), key=order.index)
    assert {item["group"] for item in HELP.values()} == groups


def test_the_catalog_file_is_the_readable_source_with_generated_parts_as_placeholders(tmp_path):
    assert app_help.CATALOG == CATALOG
    text = CATALOG.read_text(encoding="utf-8")
    raw = json.loads(text)
    # Kept as written by json.dumps(..., ensure_ascii=False, indent=2): Chinese stays readable in diffs.
    assert text == json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    assert list(raw["topics"]) == list(HELP) and raw["aliases"] == HELP_ALIASES and raw["groups"] == HELP_GROUPS
    # The skill list and the review tag rules are generated at load, never frozen into the file.
    assert {"generated": "skills"} in raw["topics"]["skills"]["detail"]
    assert any(isinstance(line, dict) and "{review_tags}" in line.get("template", "")
               for line in raw["topics"]["daily-review"]["detail"])
    assert len(HELP["skills"]["detail"]) > len(raw["topics"]["skills"]["detail"])

    def broken(mutate):
        data = json.loads(text)
        mutate(data)
        path = tmp_path / "catalog.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(HelpCatalogError) as caught:
            load_catalog(path)
        return str(caught.value)

    assert "tour.summary" in broken(lambda data: data["topics"]["tour"].pop("summary"))
    assert "unknown group" in broken(lambda data: data["topics"]["tour"].update(group="nope"))
    assert "unknown field" in broken(lambda data: data["topics"]["tour"].update(stpes=[]))
    assert "duplicate id" in broken(lambda data: data["groups"].append(dict(data["groups"][0])))
    assert "unknown generated" in broken(lambda data: data["topics"]["skills"]["detail"].append({"generated": "nope"}))
    assert "aliases.workspace" in broken(lambda data: data["aliases"].update(workspace="nope"))
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(text.replace('"tour": {', '"tour": {"title": "重复", ', 1), encoding="utf-8")
    with pytest.raises(HelpCatalogError, match="duplicate key 'title'"):
        load_catalog(duplicate)


def test_old_ids_still_open_a_topic():
    for old in OLD_IDS:
        assert resolve_topic(old) in HELP, old
    assert resolve_topic("workspace") == "tour" and resolve_topic("nope") is None
    assert set(HELP_ALIASES.values()) <= set(HELP)
    # The default course stays where agents read it: topics.tour.tour.
    assert len(HELP["tour"]["tour"]) == 7


def test_a_manual_search_ranks_by_relevance_before_the_limit(monkeypatch):
    """手续费 (2026-10-25): in catalog order, 设置 · 高级交易设置 · 费率 fell behind topics that only mention
    fees in their detail and dropped past the 16th match once a new topic did too."""
    order = [match["topic"] for match in app_help.help_search("手续费")["matches"]]
    assert order.index("settings") < min(order.index(key) for key in ("mobile", "research", "strategy-backtest"))

    def topic(title, summary="", detail=()):
        return {"title": title, "summary": summary, "steps": [], "faq": [], "detail": list(detail)}
    monkeypatch.setattr(app_help, "HELP", {"once": topic("a", detail=["词"]), "twice": topic("b", detail=["词", "词"]),
                                           "summary": topic("c", "词"), "titled": topic("词", detail=["词"])})
    assert [match["topic"] for match in app_help.help_search("词")["matches"]] == ["summary", "titled", "twice", "once"]


def test_every_ui_changes_field_is_documented_in_the_ui_commands_topic():
    text = "".join(app_help.topic_text("ui-commands"))
    for field in UiChanges.model_fields:
        assert field in text, field


async def test_help_api_returns_groups_aliases_and_topics_in_reading_order(tmp_path):
    app = create_app(tmp_path / "help.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="help-token")
    async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
        full = (await client.get("/api/app/help")).json()
        assert [group["id"] for group in full["groups"]][-1] == "developer" and full["groups"][-1]["collapsed"]
        assert full["aliases"]["workspace"] == "tour" and list(full["topics"]) == list(HELP)
        order = full["topics"]["order"]
        assert list(order)[:6] == ["title", "group", "summary", "steps", "faq", "detail"]
        # paragraphs = summary → 怎么用 → 常见问题 → detail, for agents and clients before the redesign.
        assert order["paragraphs"][0] == order["summary"] and order["paragraphs"][1].startswith("怎么用：1. ")
        assert order["paragraphs"][-len(order["detail"]):] == order["detail"]
        # The 价格阶梯 topic merged into 下单 (2026-10-07): its old id opens the 下单 topic.
        assert "depth" not in full["topics"] and full["aliases"]["depth"] == "order"
        assert full["topics"]["strategy-glossary"]["terms"][0]["when"]
        one = (await client.get("/api/app/help", params={"topic": "workspace"})).json()
        assert list(one["topics"]) == ["tour"] and one["topics"]["tour"]["tour"]
        assert (await client.get("/api/app/help", params={"topic": "nope"})).status_code == 404
        guide = (await client.get("/api/agent/capabilities")).json()["guide"]
        assert guide[0] == HELP["agent"]["summary"] and any("交易候选卡" in line for line in guide)


def test_help_command_validates_and_is_judged_by_the_card_state():
    change = UiChanges.model_validate({"help": {"topic": "workspace", "query": "止损", "detail": True, "faq": {"index": 1, "open": True}}})
    value = change.model_dump(exclude_unset=True)["help"]
    card = {"open": True, "topic": "tour", "group": "start", "query": "止损", "matches": ["tour"],
            "detailOpen": True, "faqOpen": [1], "developerOpen": False}
    assert _surface_matches({"help": card}, "help", value)
    assert not _surface_matches({"help": {**card, "faqOpen": []}}, "help", value)
    assert not _surface_matches({"help": {**card, "open": False}}, "help", value)
    assert _surface_matches({}, "help", value)   # a client before the two-layer card
    # 重试 after a failed read (2026-10-11): applied once a new read started; not when nothing can be read.
    retry = UiChanges.model_validate({"help": {"retry": True}}).model_dump(exclude_unset=True)["help"]
    assert _surface_matches({"help": {**card, "loadStatus": "loading", "loadError": None, "canRetry": False}}, "help", retry)
    assert not _surface_matches({"help": {**card, "loadStatus": "offline"}}, "help", retry)
    assert not _surface_matches({"help": {**card, "open": False, "loadStatus": "loading"}}, "help", retry)
    for bad in ({"topic": "nope"}, {"group": "nope"}, {}, {"faq": {"index": -1, "open": True}}, {"retry": False}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"help": bad})
    # The old dialog command keeps working.
    assert UiChanges.model_validate({"dialog": {"name": "help", "open": True, "topic": "strategy"}}).dialog


def test_every_tour_step_points_at_a_target_the_client_renders():
    """AI 审计 2026-10-13：引导第 4 步指向已删掉的 rail:chat，那一步高亮落空。每一步的 target 都得是客户端
    真实渲染的 data-agent-target / data-testid / data-region 值。"""
    client = Path(__file__).resolve().parents[3] / "apps/client/src"
    if not client.is_dir():
        pytest.skip("client sources are not in this checkout")
    rendered: set[str] = set()
    for source in client.rglob("*.tsx"):
        if ".test." in source.name:
            continue
        rendered.update(re.findall(r'data-(?:agent-target|testid|region)="([^"]+)"', source.read_text(encoding="utf-8")))
    steps = [step for topic in HELP.values() for step in topic.get("tour") or []]
    assert steps
    assert [step["target"] for step in steps if step["target"] not in rendered] == []
