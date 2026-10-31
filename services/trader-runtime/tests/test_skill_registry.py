"""The skill registry's contract (2026-09-24): the SKILL.md tree under
`agent_skills/` is the single source of truth, and every consumer — the tool
hiding, the capability catalog, the HTTP write gates, the typed/voice routers,
the admin switches and the progressive-disclosure surface — derives from it.

The tests mirror the acceptance checklist:
1. every SKILL.md meets the Agent Skills spec and the product's own rules;
2. every built-in AI tool and every catalog write route belongs to exactly
   one skill (shared surfaces carry a written reason);
3. switching a skill off hides its tools, routes and directory entry for
   everyone — the owner included — and restoring brings everything back;
4. the legacy `features` configs migrate to skill lists with identical
   availability, once and idempotently;
5. the availability truth table (global switch × tier × owner/member);
6. members never see the owner's surfaces;
7. progressive disclosure scopes the enum, refuses escapes and survives
   compaction;
8. the pending-card confirm/cancel priority stays skill-independent.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from fixtures.fake_responses_server import FakeResponsesServer
from trader_runtime.agent_api import CARD_REJECT_ROUTES, CHAT_OPERATIONS, HUMAN_ONLY_ROUTES, PRODUCT_PATHS
from trader_runtime.auth import hash_owner_password
from trader_runtime.main import create_app
from trader_runtime.plans import FEATURE_TO_SKILLS, FEATURES, plan_from_config
from trader_runtime.skill_registry import (
    AI_FEATURE_SKILLS,
    BODY_MAX_LINES,
    COMPATIBILITY_MAX,
    DESCRIPTION_MAX,
    SKILL_NAME_PATTERN,
    _parse_frontmatter,
    all_skills,
    availability_map,
    hidden_tool_names,
    migrate_config_features,
    parent_block_reason,
    route_owner_map,
    skill_block_reason,
    skill_by_id,
    tool_owner_map,
    unavailable_line,
)
from trader_runtime.store import RuntimeStore
from trader_runtime.store.trading_accounts import scoped_owner_user_id
from trader_runtime.strategy_editor import RETIRED_GUIDE_ROUTES
from trader_runtime.trader_tools import CARD_ACTION_TOOLS, TOOL_ARGUMENT_MODELS
from trader_runtime.voice_realtime import ROUTED_ACTION_SKILLS, route_utterance

ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"
FRIEND_PASSWORD = "a-solid-password-123"
DEEPSEEK_KEY = "sk-fixture-deepseek-not-real"
ZHIPU_KEY = "sk-fixture-zhipu-not-real"
REPO_ROOT = Path(__file__).resolve().parents[3]
# The help catalog data file (named by path: a help-text edit selects this file).
HELP_CATALOG = REPO_ROOT / "services/trader-runtime/src/trader_runtime/help/catalog.json"


def owner_headers() -> dict[str, str]:
    return {"origin": ORIGIN}


def friend_user_id() -> str:
    return scoped_owner_user_id("friend")


class SkillFlowServer(FakeResponsesServer):
    """The progressive-disclosure scenario: activate once, then a re-activate
    (dedup) plus two escaping resource reads, then a plain answer."""

    def _scenario(self, text, outputs, body):
        if "技能加载流程" in text:
            if not outputs:
                return self._tool_stream([("activate_skill", {"skillId": "order-proposal"})])
            if len(outputs) == 1:
                return self._tool_stream([
                    ("activate_skill", {"skillId": "order-proposal"}),
                    ("read_skill_resource", {"skillId": "order-proposal", "path": "../SKILL.md"}),
                    ("read_skill_resource", {"skillId": "order-proposal", "path": "/etc/passwd"}),
                ])
            return self._text_stream("手册与资源已核对。")
        return super()._scenario(text, outputs, body)


@pytest.fixture
def responses_server() -> SkillFlowServer:
    return SkillFlowServer(expected_keys={"deepseek-flash": DEEPSEEK_KEY, "glm-5.3-flash": ZHIPU_KEY})


@pytest.fixture
async def skill_app(tmp_path: Path, responses_server: SkillFlowServer, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZHIPU_API_KEY", ZHIPU_KEY)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    application = create_app(
        tmp_path / "skills.sqlite3",
        agent_mode="native",
        agent_provider="deepseek",
        deepseek_api_key=DEEPSEEK_KEY,
        native_transport=responses_server.transport(),
        engineering_root=tmp_path / "ai-trader-project",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
        registration_enabled=True,
    )
    yield application
    await application.state.runs.shutdown()
    await application.state.adapter.shutdown()
    await application.state.broker.shutdown()
    application.state.store.close()


async def _owner_client(app) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    login = await client.post(
        "/api/auth/owner", json={"username": "charles", "password": PASSWORD}, headers=owner_headers(),
    )
    assert login.status_code == 200
    return client


@pytest.fixture
async def member_and_owner(skill_app):
    """A registered basic-tier member (granted an account) and the owner, each
    on their own client."""
    owner = await _owner_client(skill_app)
    code = (await owner.post(
        "/api/auth/owner/invite-codes", json={"daysValid": 14}, headers=owner_headers(),
    )).json()["inviteCode"]
    friend = AsyncClient(transport=ASGITransport(app=skill_app), base_url="http://test")
    registered = await friend.post(
        "/api/auth/owner/register",
        json={"username": "friend", "password": FRIEND_PASSWORD, "inviteCode": code},
        headers=owner_headers(),
    )
    assert registered.status_code == 200
    skill_app.state.store.grant_account(
        account_id=skill_app.state.store.default_trading_account_id,
        user_id=friend_user_id(), role="operator",
        granted_by=scoped_owner_user_id("charles"),
    )
    try:
        yield friend, owner
    finally:
        await friend.aclose()
        await owner.aclose()


async def _create_session(client: AsyncClient) -> str:
    response = await client.post(
        "/api/sessions", json={"mode": "local", "agentId": "native"}, headers=owner_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()["sessionId"]


async def _wait_for_run(app, run_id: str) -> dict:
    for _ in range(400):
        run = app.state.store.get_run(run_id)
        if run["status"] in {"completed", "failed", "cancelled"}:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not finish")


def _assistant_answers(app, session_id: str) -> list[str]:
    return [
        event["payload"]["text"] for _, event in app.state.store.list_events(session_id)
        if event["name"] == "message.completed" and event["payload"].get("role") == "assistant"
    ]


def _directory(client_response_json: dict) -> dict[str, dict]:
    return {item["id"]: item for item in client_response_json["items"]}


async def _catalog_operations(client: AsyncClient) -> set[str]:
    catalog = await client.get("/api/agent/capabilities")
    assert catalog.status_code == 200, catalog.text
    return {item["operation"] for item in catalog.json()["operations"]}


def _skill_catalog_ops(skill, catalog_surface: set[str]) -> set[str]:
    """The catalog operations a skill owns that the catalog surface actually
    lists. The two disclosure meta-tools (`app_capabilities`/`app_call`) are
    the catalog's own access mechanism and never appear as `tool:` items;
    their hiding is pinned through `hidden_tool_names` instead."""
    return {f"tool:{tool}" for tool in skill.tools if f"tool:{tool}" in catalog_surface} | {
        route for route in skill.routes if route in catalog_surface
    }


# -- 1. the SKILL.md files meet the spec --------------------------------------


def test_every_skill_file_meets_the_spec():
    skills = all_skills()
    assert len(skills) >= 20, "the registry should carry the product's skill set"
    for skill in skills:
        raw = (skill.directory / "SKILL.md").read_text(encoding="utf-8")
        fields = _parse_frontmatter(raw)
        assert fields is not None, f"{skill.skill_id}: frontmatter must parse"
        # Spec: the name equals the directory and uses the restricted charset.
        assert fields.get("name") == skill.skill_id
        assert SKILL_NAME_PATTERN.match(skill.skill_id)
        assert 1 <= len(skill.description) <= DESCRIPTION_MAX
        compatibility = str(fields.get("compatibility") or "")
        assert len(compatibility) <= COMPATIBILITY_MAX
        body_lines = skill.body.splitlines()
        assert len(body_lines) <= BODY_MAX_LINES
        headings = [line.strip() for line in body_lines if line.startswith("## ")]
        for required in ("何时用", "何时不用", "步骤", "安全边界"):
            assert any(head.startswith(f"## {required}") for head in headings), (
                f"{skill.skill_id}: missing ## {required}")
        # The routing corpus: ≥3 typical phrases and ≥2 counter-examples.
        assert len(skill.phrases) >= 3, f"{skill.skill_id}: 典型说法 needs ≥3 bullets"
        assert len(skill.counter_examples) >= 2, f"{skill.skill_id}: 反例 needs ≥2 bullets"
        # Referenced files exist, stay in the directory and are readable resources.
        for reference in skill.references:
            path = skill.directory / reference
            assert path.is_file(), f"{skill.skill_id}: {reference} missing"
            assert path.stat().st_size <= 128 * 1024
        # Product extensions ride in metadata with `ai-trader-` keys, string values.
        for key in fields.get("metadata") or {}:
            assert str(key).startswith("ai-trader-"), f"{skill.skill_id}: metadata key {key}"


def test_route_claims_carry_their_verb():
    for skill in all_skills():
        for route in skill.routes:
            method, _, path = route.partition(" ")
            assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"}, route
            assert path.startswith("/api/"), route


# -- 2. ownership audit --------------------------------------------------------


def test_every_built_in_tool_belongs_to_exactly_one_skill():
    claims = [(tool, skill.skill_id) for skill in all_skills() for tool in skill.tools]
    names = [tool for tool, _ in claims]
    assert len(names) == len(set(names)), "a tool claimed twice"
    # The static argument-model registry is every built-in tool there is; the
    # two disclosure tools are built by the native loop itself and stay
    # available to everyone, so no skill claims them. The card's own 取消
    # (reject_proposal, #756) is the card priority: no skill claims it either.
    assert set(TOOL_ARGUMENT_MODELS) - CARD_ACTION_TOOLS == set(tool_owner_map())
    assert not ({"activate_skill", "read_skill_resource"} & set(tool_owner_map()))
    assert not (CARD_ACTION_TOOLS & set(tool_owner_map()))


# Write routes inside the catalog surface that deliberately belong to no
# skill. Every entry carries its reason; the test asserts the set matches the
# routes exactly, so a reason can neither hide a new gap nor go stale.
# (开户/绑定/解绑 left this table in 526-A3: they are human-only now, outside
# the catalog entirely — see HUMAN_ONLY_ROUTES in agent_api.)
UNOWNED_WRITE_ROUTES = {
    # Safety shutoffs are never gated: anyone can always stop (the
    # pre-registry rule — reads and exits stay open).
    "POST /api/ai-execution/stop",
    "POST /api/ai-execution/stops",
    "POST /api/ai-execution/stops/{stop_id}/cancel",
    # 图上拖线改价 (2026-10-24): the user's own stop / take moved in place — protective like the two above.
    "POST /api/ai-execution/stops/{stop_id}/reprice",
    # 保本提醒 (2026-10-06): the user's own answer to a protection question on their position —
    # protective like the stops above, so never tier-gated.
    "POST /api/ai-execution/breakeven/{notice_id}/move",
    "POST /api/ai-execution/breakeven/{notice_id}/decline",
    # 提醒中心 (#932): marking the user's own reminders read writes no business state, and every tier
    # receives reminders — never tier-gated.
    "POST /api/reminders/read",
    # The strategy library's save/design/validate/delete surface was never
    # feature-gated (only monitor start/pause and backtests are); claiming it
    # for a tier-gated skill would change who may save a strategy.
    "POST /api/strategy-plans/design-jobs",
    "DELETE /api/strategy-plans/design-jobs/{identifier}",
    "POST /api/strategy-plans/design",
    "POST /api/strategy-plans",
    "DELETE /api/strategy-plans/{identifier}",
    "POST /api/strategy-plans/{identifier}/scope",
    "POST /api/strategy-plans/chart-period",
    "POST /api/strategy-plans/{identifier}/validate",
    # 从模板创建 saves an ordinary library strategy exactly like POST /api/strategy-plans
    # (same save path), so it stays as open as that route.
    "POST /api/strategy-templates/{template_id}/instantiate",
    # 策略体检's 按这个改 saves the advised change as the next library version through the
    # same save path as POST /api/strategy-plans (改一版), so it stays as open as that route.
    "POST /api/strategy-plans/{identifier}/apply-advice",
    # The auto-explanation records (server-generated summaries of fills,
    # failed executions and protection changes) write no business state.
    "POST /api/app/strategy-explanations",
    # Per-account fee schedules: a settings surface open to every tier, exactly
    # as before the registry.
    "POST /api/trading-costs",
    # 调研汇总's refresh re-reads shared public facts and writes no business state; the
    # judgment it may regenerate is gated inside by the reader's web-research tier.
    "POST /api/research/desk/refresh",
    # The skill switches themselves are the restore surface: unowned so that
    # disabling admin-console can never lock the owner out of re-enabling it.
    "PUT /api/admin/skills",
    "PUT /api/admin/skills/{skill_id}",
}


async def test_every_catalog_write_route_belongs_to_one_skill_or_a_reason(skill_app):
    owned = route_owner_map()
    app_routes = {
        f"{method} {route.path}": route for route in skill_app.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods or [])
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    unowned = set()
    for key, route in app_routes.items():
        path = route.path
        if (key in CHAT_OPERATIONS or key in HUMAN_ONLY_ROUTES or key in RETIRED_GUIDE_ROUTES or path.startswith("/api/auth")
                or key.split(" ", 1)[0] == "GET"):
            continue
        if any(path == prefix or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
               for prefix in PRODUCT_PATHS):
            if owned.get(key) is None:
                unowned.add(key)
    assert unowned == UNOWNED_WRITE_ROUTES
    # No skill claims a route the app does not have.
    assert set(owned) <= set(app_routes)
    # The human-only exclusions are real app routes (the user's own UI calls
    # them); the catalog is what leaves them out, not the router.
    assert HUMAN_ONLY_ROUTES <= set(app_routes)


async def test_card_confirmation_stays_outside_the_skill_system(skill_app):
    """The pending card's confirm/reject/adjust-risk are the user's own
    actions: claimed by no skill, so no skill switch can touch the card
    priority (checklist 8, HTTP side). Confirm and adjust-risk stay outside the
    agent catalog entirely; reject — the card's 取消 — is listed (#756) so any
    AI can do what the click does, still owned by no skill."""
    owned = route_owner_map()
    card_routes = {f"POST /api/proposals/{{proposal_id}}/{action}"
                   for action in ("confirm", "reject", "adjust-risk")}
    app_paths = {f"{method} {route.path}": route.path for route in skill_app.routes
                 if isinstance(route, APIRoute)
                 for method in sorted(route.methods or [])}
    assert card_routes <= set(app_paths)
    assert not (card_routes & set(owned))
    catalog = set(skill_app.state.app_capabilities.routes())
    assert card_routes & catalog == CARD_REJECT_ROUTES == {"POST /api/proposals/{proposal_id}/reject"}
    for path in {app_paths[route] for route in card_routes}:
        assert not any(path == prefix or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
                       for prefix in PRODUCT_PATHS)


# -- 3. switching every skill off and back on ----------------------------------


@pytest.mark.parametrize("skill_id", [skill.skill_id for skill in all_skills()])
async def test_global_disable_hides_the_skill_everywhere_and_restore_brings_it_back(
    skill_app, skill_id,
):
    skill = skill_by_id(skill_id)
    owner = await _owner_client(skill_app)
    try:
        surface = await _catalog_operations(owner)
        expected = _skill_catalog_ops(skill, surface)

        before = _directory((await owner.get("/api/agent/skills")).json())
        assert before[skill_id]["available"] is True

        switched = await owner.put(
            f"/api/admin/skills/{skill_id}", json={"enabled": False}, headers=owner_headers(),
        )
        assert switched.status_code == 200
        # The global switch binds everyone — the owner included.
        after = _directory((await owner.get("/api/agent/skills")).json())
        assert after[skill_id]["available"] is False
        assert after[skill_id]["unavailableReason"] == "disabled"
        # AI never sees the skill's tools, at the source the native loop and
        # the routed subsets filter with.
        assert set(skill.tools) <= hidden_tool_names(
            skill_app.state.store, scoped_owner_user_id("charles"))
        hidden = await _catalog_operations(owner)
        assert not (expected & hidden), f"{skill_id} operations survived the switch-off"

        restored = await owner.put(
            f"/api/admin/skills/{skill_id}", json={"enabled": True}, headers=owner_headers(),
        )
        assert restored.status_code == 200
        recovered = _directory((await owner.get("/api/agent/skills")).json())
        assert recovered[skill_id]["available"] is True
        assert expected <= await _catalog_operations(owner)
    finally:
        await owner.aclose()


# One representative write route per skill that owns one. The owner's switch
# governs the AI (2026-09-27): an AI-feature skill (AI_FEATURE_SKILLS) switched
# off refuses its write for everyone; any other skill switched off is refused
# only to the AI (through /api/agent/call) and the user's own request passes the
# gate. The gate reads the registry's route
# table, so the refusal cannot drift from the claims — and restoring must let
# the request through the gate again. What the body then gets (422 validation,
# 404/409 business refusals, 200) is the route's own business; only the skill
# refusal (403) is gone. Briefs refresh is gated in its handler (the gentle
# `status: disabled` tier contract) and still refuses 403 when switched off;
# replay control has its own test below. Invite codes are the one owned write
# route with no skill gate at all: the owner-session check IS their gate
# (HANDLER_GATED_WRITE_ROUTES carries the reason).
WRITE_ROUTE_CASES = [
    ("admin-console", "PUT", "/api/admin/users/nobody", {"plan": "basic"}),
    ("ai-execution", "POST", "/api/ai-execution/limits", {}),
    ("app-catalog", "POST", "/api/agent/call", {}),
    ("batch-close", "POST", "/api/simulation/close-batch-proposals", {}),
    ("market-briefs", "POST", "/api/app/briefs/refresh", {"symbol": "SHFE.rb2610"}),
    ("market-replay", "POST", "/api/replays", {}),
    ("order-ops", "POST", "/api/simulation/orders/does-not-exist/cancel", None),
    ("order-proposal", "POST", "/api/simulation/proposals", {}),
    ("preferences", "PUT", "/api/preferences", {}),
    ("strategy-backtests", "POST", "/api/backtests", {}),
    ("strategy-management", "POST", "/api/strategies/does-not-exist/monitor/start", None),
    ("target-position", "POST", "/api/simulation/target-position/proposals", {}),
    ("ui-control", "POST", "/api/app/ui/commands", {}),
    ("voice-call", "POST", "/api/voice/realtime/announce", {}),
]


@pytest.mark.parametrize("skill_id,method,path,body", WRITE_ROUTE_CASES)
async def test_a_disabled_skill_refuses_one_of_its_write_routes_at_the_app_gate(
    skill_app, skill_id, method, path, body,
):
    owner = await _owner_client(skill_app)
    try:
        call = owner.post if method == "POST" else owner.put
        await owner.put(f"/api/admin/skills/{skill_id}", json={"enabled": False}, headers=owner_headers())
        direct = await call(path, json=body, headers=owner_headers())
        if skill_id in AI_FEATURE_SKILLS:
            assert direct.status_code == 403, direct.text
            assert direct.json()["detail"] == "skill_disabled"
        else:
            # The user's own screens keep a skill the switch only took away from the AI.
            assert not (direct.status_code == 403 and direct.json().get("detail") == "skill_disabled"), direct.text
        await owner.put(f"/api/admin/skills/{skill_id}", json={"enabled": True}, headers=owner_headers())
        recovered = await call(path, json=body, headers=owner_headers())
        assert recovered.status_code != 403, recovered.text
    finally:
        await owner.aclose()


async def test_a_switched_off_skill_leaves_the_user_and_refuses_the_ai(skill_app):
    """Owner decision 2026-09-27: switching off 下单建议 must not block the user's own order ticket."""
    owner = await _owner_client(skill_app)
    try:
        await owner.put("/api/admin/skills/order-proposal", json={"enabled": False}, headers=owner_headers())
        skills = {item["id"]: item for item in (await owner.get("/api/agent/skills", headers=owner_headers())).json()["items"]}
        assert skills["order-proposal"]["unavailableReason"] == "disabled" and skills["order-proposal"]["aiFeature"] is False
        assert skills["voice-call"]["aiFeature"] is True
        direct = await owner.post("/api/simulation/proposals", json={}, headers=owner_headers())
        assert direct.status_code != 403, direct.text
        via_ai = await owner.post("/api/agent/call", json={"operation": "POST /api/simulation/proposals", "body": {}},
                                  headers=owner_headers())
        assert "skill_disabled" in via_ai.text or via_ai.status_code == 404, via_ai.text
        catalog = (await owner.get("/api/agent/capabilities", headers=owner_headers())).json()
        assert all(item["operation"] != "POST /api/simulation/proposals" for item in catalog["operations"])
        assert "propose_order" in hidden_tool_names(skill_app.state.store, scoped_owner_user_id("charles"))
    finally:
        await owner.aclose()


async def test_the_skill_switches_survive_disabling_admin_console(skill_app):
    """The restore surface itself: PUT /api/admin/skills belongs to no skill
    (UNOWNED_WRITE_ROUTES above), so disabling admin-console refuses its admin
    routes yet can never lock the owner out of re-enabling it."""
    owner = await _owner_client(skill_app)
    try:
        await owner.put("/api/admin/skills/admin-console", json={"enabled": False}, headers=owner_headers())
        # The AI loses the skill's write routes — the gate fires before the
        # handler's user lookup, so even a missing username gets the refusal.
        refused = await owner.post("/api/agent/call", json={
            "operation": "PUT /api/admin/users/{username}", "path": {"username": "nobody"},
            "body": {"plan": "basic"}}, headers=owner_headers())
        assert refused.status_code == 403 or refused.json().get("status") == 403, refused.text
        assert "skill_disabled" in refused.text or "能力不存在" in refused.text, refused.text
        # The restore entry point stays open.
        restored = await owner.put(
            "/api/admin/skills/admin-console", json={"enabled": True}, headers=owner_headers())
        assert restored.status_code == 200
    finally:
        await owner.aclose()


class _StubReplayContext:
    """A mounted replay context for the control route: the handler resolves
    the replay before its own gate, and the fake-mode app cannot create a real
    one (no configured sidecar)."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.playing = True
        self.ended = False
        self.sidecar_closed = False  # The stub has no sidecar closure verdict.
        self.ending = False
        self.error = None
        self.speed = 1

        class _Provider:
            entries_paused = False
            snapshot: dict = {}

        self.provider = _Provider()

    def require_open(self, user_id: str) -> None:
        assert user_id == self.user_id

    async def stop(self) -> None:
        self.ended = True

    async def call_tool(self, user_id, call, authorize=None):
        return await call()

    def public(self) -> dict:
        return {"replayId": "stub-replay", "playing": self.playing}


async def test_replay_control_gates_only_its_non_close_actions(skill_app):
    """The control route is the one owned write route the app-level gate
    leaves to its handler: closing a replay is a stop-like action that stays
    open even with the skill off; every other action follows the skill."""
    router = skill_app.state.replay_router
    owner = await _owner_client(skill_app)
    try:
        router.contexts["stubreplay"] = _StubReplayContext(scoped_owner_user_id("charles"))
        await owner.put("/api/admin/skills/replay-control", json={"enabled": False}, headers=owner_headers())
        # Switched off, the AI loses it; the user's own replay controls keep working.
        refused = await owner.post("/api/agent/call", json={
            "operation": "POST /api/replays/{identifier}/control", "path": {"identifier": "stubreplay"},
            "body": {"action": "pause"}}, headers=owner_headers())
        assert "skill_disabled" in refused.text or refused.status_code == 404, refused.text
        paused = await owner.post(
            "/api/replays/stubreplay/control", json={"action": "pause"}, headers=owner_headers())
        assert paused.status_code == 200, paused.text
        assert paused.json()["playing"] is False

        await owner.put("/api/admin/skills/replay-control", json={"enabled": False}, headers=owner_headers())
        closing = await owner.post(
            "/api/replays/stubreplay/control", json={"action": "close"}, headers=owner_headers())
        assert closing.status_code == 200, closing.text
        assert "stubreplay" not in router.contexts
    finally:
        await owner.aclose()


# -- 4. the legacy features → skills migration ---------------------------------


def test_migration_converts_every_legacy_shape_to_the_same_availability():
    shapes = {
        # The pre-registry seeds: every switch on.
        "all-on-list": ["ai_execution", "web_research", "market_briefs", "strategies",
                        "backtests", "replays", "voice"],
        # A tier the owner had edited: some switches off.
        "owner-edited": ["market_briefs", "strategies", "voice"],
        # The dict shape `Plan.config()`'s predecessor also wrote.
        "dict-shape": {"ai_execution": False, "web_research": True, "market_briefs": True,
                       "strategies": True, "backtests": False, "replays": True, "voice": True},
        # A production row missing unknown legacy keys keeps them dropped.
        "unknown-keys": ["ai_execution", "not-a-feature", "voice"],
    }
    for name, features in shapes.items():
        config = {"models": None, "dailyTokens": None, "voiceSecondsPerDay": None,
                  "features": features}
        enabled = ({str(key) for key, on in features.items() if on}
                   if isinstance(features, dict) else {str(key) for key in features})
        expected_skills = {
            skill_id for key in enabled for skill_id in FEATURE_TO_SKILLS.get(key, frozenset())
        }
        migrated = migrate_config_features(config)
        assert migrated is not None, name
        assert set(migrated["skills"]) == expected_skills, name
        assert "features" not in migrated
        # Availability is exactly the pre-migration one, per legacy switch.
        plan = plan_from_config(name, name, migrated)
        for feature in FEATURES:
            assert plan.has_skill_in_group(feature.key) == (feature.key in enabled), name
        # Idempotent: a second pass is a no-op.
        assert migrate_config_features(config) is None, name

    # A row that predates features entirely (nothing set): everything on.
    all_gated = {skill.skill_id for skill in all_skills() if skill.tier_gated}
    blank = {"models": None, "dailyTokens": None, "voiceSecondsPerDay": None}
    migrated = migrate_config_features(blank)
    assert set(migrated["skills"]) == all_gated
    assert migrate_config_features(blank) is None
    # A row that already carries skills is untouched.
    modern = {"models": None, "dailyTokens": None, "voiceSecondsPerDay": None,
              "skills": ["voice-call"]}
    assert migrate_config_features(modern) is None


def test_store_startup_migrates_tier_rows_once(tmp_path: Path):
    database = tmp_path / "legacy.sqlite3"
    store = RuntimeStore(database)
    legacy_configs = {
        "basic": ["market_briefs", "strategies", "backtests", "replays", "voice"],
        "pro": ["ai_execution", "web_research", "market_briefs", "strategies",
                "backtests", "replays", "voice"],
        "test": {"ai_execution": True, "web_research": False, "market_briefs": True,
                 "strategies": True, "backtests": True, "replays": False, "voice": True},
    }
    expected = {}
    for tier, features in legacy_configs.items():
        enabled = ({str(key) for key, on in features.items() if on}
                   if isinstance(features, dict) else set(features))
        expected[tier] = sorted({
            skill_id for key in enabled for skill_id in FEATURE_TO_SKILLS.get(key, frozenset())
        })
        assert store.update_plan_tier(tier, config={
            "models": None, "dailyTokens": None, "voiceSecondsPerDay": None, "features": features,
        }) is not None
    store.close()

    # Startup rewrites the rows; per tier the open skills — and therefore the
    # derived legacy feature view — are exactly the pre-migration ones.
    reopened = RuntimeStore(database)
    try:
        for tier, skills in expected.items():
            config = reopened.get_plan_tier(tier)["config"]
            assert config["skills"] == skills, tier
            assert "features" not in config, tier
            plan = plan_from_config(tier, tier, config)
            enabled = set(legacy_configs[tier]) if isinstance(legacy_configs[tier], list) else {
                key for key, on in legacy_configs[tier].items() if on}
            for feature in FEATURES:
                assert plan.has_skill_in_group(feature.key) == (feature.key in enabled), tier
        snapshot = {tier["name"]: tier["config"] for tier in reopened.list_plan_tiers()}
    finally:
        reopened.close()

    # The second startup is a no-op (idempotent), byte-identical rows.
    again = RuntimeStore(database)
    try:
        assert {tier["name"]: tier["config"] for tier in again.list_plan_tiers()} == snapshot
    finally:
        again.close()


# -- 5. the availability truth table -------------------------------------------


async def test_availability_truth_table(skill_app, member_and_owner):
    friend, owner = member_and_owner
    store = skill_app.state.store
    member, owner_id = friend_user_id(), scoped_owner_user_id("charles")

    gated = skill_by_id("voice-call")
    owner_only = skill_by_id("admin-console")
    plain = skill_by_id("order-proposal")

    # basic opens voice-call; a tier-gated skill is the member's when the tier
    # has it, always the owner's.
    assert store.get_plan_tier("basic")["config"]["skills"].count("voice-call") == 1
    assert skill_block_reason(store, member, gated) is None
    assert skill_block_reason(store, owner_id, gated) is None
    # The tier drops it: the member is refused, the owner is not.
    dropped = await owner.put(
        "/api/admin/plans/basic", json={"skills": {"voice-call": False}}, headers=owner_headers(),
    )
    assert dropped.status_code == 200
    assert skill_block_reason(store, member, gated) == "not_in_plan"
    assert skill_block_reason(store, owner_id, gated) is None
    # The global switch outranks everything and binds the owner too.
    await owner.put("/api/admin/skills/voice-call", json={"enabled": False}, headers=owner_headers())
    assert skill_block_reason(store, member, gated) == "disabled"
    assert skill_block_reason(store, owner_id, gated) == "disabled"
    await owner.put("/api/admin/plans/basic", json={"skills": {"voice-call": True}}, headers=owner_headers())
    await owner.put("/api/admin/skills/voice-call", json={"enabled": True}, headers=owner_headers())

    # owner-only: the member is refused for that reason, the owner is not —
    # and the global switch still binds the owner.
    assert skill_block_reason(store, member, owner_only) == "owner_only"
    assert skill_block_reason(store, owner_id, owner_only) is None
    await owner.put("/api/admin/skills/admin-console", json={"enabled": False}, headers=owner_headers())
    assert skill_block_reason(store, member, owner_only) == "disabled"
    assert skill_block_reason(store, owner_id, owner_only) == "disabled"
    await owner.put("/api/admin/skills/admin-console", json={"enabled": True}, headers=owner_headers())

    # A non-gated skill is everyone's; only the global switch can refuse it.
    assert skill_block_reason(store, member, plain) is None
    assert skill_block_reason(store, owner_id, plain) is None


# -- 5b. the parent/child hierarchy (2026-09-30) -------------------------------


async def test_children_follow_their_parent(skill_app, member_and_owner):
    """An unavailable parent makes its children unavailable, for the global
    switch and the tier matrix alike (both `skill_block_reason` and the
    whole-registry `availability_map` agree)."""
    _, owner = member_and_owner
    store = skill_app.state.store
    member, owner_id = friend_user_id(), scoped_owner_user_id("charles")
    child = skill_by_id("strategy-backtests")
    await owner.put("/api/admin/skills/strategy-management",
                    json={"enabled": False}, headers=owner_headers())
    assert skill_block_reason(store, owner_id, child) == "disabled"
    assert availability_map(store, owner_id)["strategy-backtests"] == "disabled"
    await owner.put("/api/admin/skills/strategy-management",
                    json={"enabled": True}, headers=owner_headers())
    assert skill_block_reason(store, owner_id, child) is None

    dropped = await owner.put(
        "/api/admin/plans/basic", json={"skills": {"market-replay": False}}, headers=owner_headers())
    assert dropped.status_code == 200
    assert skill_block_reason(store, member, skill_by_id("market-replay")) == "not_in_plan"
    assert availability_map(store, member)["replay-control"] == "not_in_plan"
    await owner.put("/api/admin/plans/basic",
                    json={"skills": {"market-replay": True}}, headers=owner_headers())
    assert availability_map(store, member)["replay-control"] is None


async def test_inherited_blocks_name_the_parent_in_the_refusal_line(skill_app, member_and_owner):
    """设置拆页 (2026-09-30 review): a child blocked only through its parent says so
    with the parent's name; a child blocked in its own right keeps its own line."""
    _, owner = member_and_owner
    store = skill_app.state.store
    member, owner_id = friend_user_id(), scoped_owner_user_id("charles")
    child, parent = skill_by_id("strategy-backtests"), skill_by_id("strategy-management")

    # Parent switched off: the child's line says it follows the parent.
    await owner.put("/api/admin/skills/strategy-management", json={"enabled": False}, headers=owner_headers())
    reason = skill_block_reason(store, owner_id, child)
    assert reason == "disabled" and parent_block_reason(store, owner_id, child) == "disabled"
    line = unavailable_line(child, reason, parent_block_reason(store, owner_id, child))
    assert line == f"「{child.label}」随「{parent.label}」关闭"

    # The child's own switch produces the plain line even with the parent off.
    await owner.put("/api/admin/skills/strategy-management", json={"enabled": True}, headers=owner_headers())
    await owner.put("/api/admin/skills/strategy-backtests", json={"enabled": False}, headers=owner_headers())
    reason = skill_block_reason(store, owner_id, child)
    assert reason == "disabled" and parent_block_reason(store, owner_id, child) is None
    assert unavailable_line(child, reason) == f"「{child.label}」已停用"
    await owner.put("/api/admin/skills/strategy-backtests", json={"enabled": True}, headers=owner_headers())

    # A tier-withheld parent: the member's line names the parent's plan gap.
    # (First make sure the child itself is open on basic, whatever the seed.)
    await owner.put("/api/admin/plans/basic", json={"skills": {"strategy-backtests": True}}, headers=owner_headers())
    dropped = await owner.put(
        "/api/admin/plans/basic", json={"skills": {"strategy-management": False}}, headers=owner_headers())
    assert dropped.status_code == 200
    reason = skill_block_reason(store, member, child)
    inherited = parent_block_reason(store, member, child)
    assert reason == "not_in_plan" and inherited == "not_in_plan"
    assert unavailable_line(child, reason, inherited) == f"当前档位未开通「{parent.label}」"
    await owner.put("/api/admin/plans/basic",
                    json={"skills": {"strategy-management": True}}, headers=owner_headers())


def test_every_skill_has_a_summary_and_one_level_of_parent():
    """The admin matrix shows each skill's one-line summary, and hierarchy is
    exactly one level: a parent must exist and carry no parent itself."""
    for skill in all_skills():
        assert skill.summary.strip(), f"{skill.skill_id}: summary is required"
        if skill.parent:
            parent = skill_by_id(skill.parent)
            assert parent is not None, f"{skill.skill_id}: unknown parent {skill.parent}"
            assert not parent.parent, f"{skill.parent}: a parent may not have a parent"


# -- 6. member isolation --------------------------------------------------------


async def test_members_never_see_the_owner_surfaces(skill_app, member_and_owner):
    friend, owner = member_and_owner

    directory = _directory((await friend.get("/api/agent/skills")).json())
    assert directory["admin-console"]["available"] is False
    assert directory["admin-console"]["unavailableReason"] == "owner_only"
    assert directory["ai-execution"]["unavailableReason"] == "not_in_plan"
    assert directory["web-research"]["unavailableReason"] == "not_in_plan"
    assert directory["order-proposal"]["available"] is True
    owner_directory = _directory((await owner.get("/api/agent/skills")).json())
    assert all(item["available"] for item in owner_directory.values())

    # The admin skill switches are undiscoverable and unusable for members.
    assert (await friend.get("/api/admin/skills")).status_code == 404
    flipped = await friend.put(
        "/api/admin/skills/voice-call", json={"enabled": False}, headers=owner_headers(),
    )
    assert flipped.status_code == 404
    # Activation of an owner-only skill refuses the member.
    activate = await friend.get("/api/agent/skills/admin-console")
    assert activate.status_code == 403

    # A global disable the owner makes reaches the owner's own directory.
    await owner.put("/api/admin/skills/voice-call", json={"enabled": False}, headers=owner_headers())
    owner_directory = _directory((await owner.get("/api/agent/skills")).json())
    assert owner_directory["voice-call"]["available"] is False
    assert owner_directory["voice-call"]["unavailableReason"] == "disabled"


# -- 7. progressive disclosure ---------------------------------------------------


async def test_disclosure_enum_and_catalog_follow_availability(
    skill_app, member_and_owner, responses_server: SkillFlowServer,
):
    friend, owner = member_and_owner
    for client, hidden in ((friend, {"ai-execution", "web-research", "admin-console"}),
                           (owner, set())):
        session_id = await _create_session(client)
        run = await client.post(
            f"/api/sessions/{session_id}/messages", json={"text": "你还有什么能力"},
            headers=owner_headers(),
        )
        assert run.status_code == 202
        assert (await _wait_for_run(skill_app, run.json()["runId"]))["status"] == "completed"
        request = responses_server.requests[-1]
        enum_tool = next(tool for tool in request["tools"] if tool.get("name") == "activate_skill")
        enum = set(enum_tool["parameters"]["properties"]["skillId"]["enum"])
        assert all(skill.skill_id in enum for skill in all_skills() if skill.skill_id not in hidden)
        assert not (enum & hidden)
        instructions = request["instructions"]
        assert "可用技能目录" in instructions
        for skill_id in hidden:
            assert f"- {skill_id}:" not in instructions
        assert "- order-proposal:" in instructions


async def test_activation_dedupes_and_resource_reads_stay_in_the_directory(
    skill_app, member_and_owner, responses_server: SkillFlowServer,
):
    friend, _ = member_and_owner
    session_id = await _create_session(friend)
    run = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "技能加载流程"}, headers=owner_headers(),
    )
    assert (await _wait_for_run(skill_app, run.json()["runId"]))["status"] == "completed"

    skill_requests = [request for request in responses_server.requests
                      if any(isinstance(item, dict) and item.get("type") == "function_call"
                             and item.get("name") in {"activate_skill", "read_skill_resource"}
                             for item in request.get("input") or [])]
    # Later requests replay earlier turns' calls in `input`; `call_id` dedupes
    # to the calls actually made, in order.
    seen_calls: list[tuple[str, str]] = []
    for request in skill_requests:
        for item in request["input"]:
            if (isinstance(item, dict) and item.get("type") == "function_call"
                    and item.get("name") in {"activate_skill", "read_skill_resource"}
                    and (item.get("call_id"), item["name"]) not in seen_calls):
                seen_calls.append((item["call_id"], item["name"]))
    # First activation, the dedup re-activation and the two escaping reads.
    assert [name for _, name in seen_calls] == [
        "activate_skill", "activate_skill", "read_skill_resource", "read_skill_resource"]
    # Tier 2: the manual arrives as the spec's <skill_content> block in the instructions of the next
    # request (2026-10-14: mounted, not repeated in the tool output)…
    outputs = [item for request in skill_requests for item in request["input"]
               if isinstance(item, dict) and item.get("type") == "function_call_output"]
    assert any("已挂上" in str(item.get("output")) for item in outputs)
    assert all('<skill_content name="order-proposal">' in request["instructions"] for request in skill_requests)
    # …the second activation of the same skill dedupes…
    assert any("已加载过" in str(item.get("output")) for item in outputs)
    # …and both escapes (`..` and an absolute path) are refused.
    refusals = [item for item in outputs if "skill_resource_not_found" in str(item.get("output"))]
    assert len(refusals) == 2

    # The activated manual rides the instructions of every later request while the skill stays mounted (used within
    # SKILL_MOUNT_IDLE_TURNS turns), exactly once — including after history compaction.
    follow_up = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "再核对一遍手册"}, headers=owner_headers(),
    )
    assert (await _wait_for_run(skill_app, follow_up.json()["runId"]))["status"] == "completed"
    last = responses_server.requests[-1]
    assert last["instructions"].count('<skill_content name="order-proposal">') == 1
    adapter = skill_app.state.adapter
    await adapter._compact_history(session_id, next(iter(adapter.endpoints.values())))
    after_compaction = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "压缩后再核对"}, headers=owner_headers(),
    )
    assert (await _wait_for_run(skill_app, after_compaction.json()["runId"]))["status"] == "completed"
    assert responses_server.requests[-1]["instructions"].count(
        '<skill_content name="order-proposal">') == 1


async def test_resource_endpoint_refuses_escapes_over_http(skill_app, member_and_owner):
    friend, _ = member_and_owner
    for path in ("../SKILL.md", "/etc/passwd", "../../agent_skills/order-proposal/SKILL.md"):
        response = await friend.get(
            "/api/agent/skills/order-proposal/resource", params={"path": path},
        )
        assert response.status_code == 404, path
        assert response.json()["detail"] == "skill_resource_not_found"
    inside = await friend.get(
        "/api/agent/skills/order-proposal/resource", params={"path": "references/card-flow.md"},
    )
    assert inside.status_code == 200
    assert inside.json()["path"] == "references/card-flow.md"
    assert "下单卡流程" in inside.json()["content"]


# -- 8. the unified refusal line and the card priority ---------------------------


async def test_typed_strategy_signal_hit_answers_with_the_unified_line(
    skill_app, member_and_owner,
):
    friend, owner = member_and_owner
    session_id = await _create_session(friend)

    # The tier withholds the skill: the deterministic hit never reaches a
    # model turn; the user gets the one unified line.
    dropped = await owner.put(
        "/api/admin/plans/basic", json={"skills": {"strategy-signals": False}}, headers=owner_headers(),
    )
    assert dropped.status_code == 200
    run = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "按我的策略，现在该怎么做？"},
        headers=owner_headers(),
    )
    assert run.status_code == 202
    assert (await _wait_for_run(skill_app, run.json()["runId"]))["status"] == "completed"
    assert _assistant_answers(skill_app, session_id)[-1] == "当前档位未开通「策略信号解读」"

    # The owner's global switch produces the other unified line (and binds
    # the owner as much as the member).
    await owner.put("/api/admin/plans/basic", json={"skills": {"strategy-signals": True}}, headers=owner_headers())
    await owner.put("/api/admin/skills/strategy-signals", json={"enabled": False}, headers=owner_headers())
    run = await friend.post(
        f"/api/sessions/{session_id}/messages", json={"text": "策略怎么看"}, headers=owner_headers(),
    )
    assert (await _wait_for_run(skill_app, run.json()["runId"]))["status"] == "completed"
    assert _assistant_answers(skill_app, session_id)[-1] == "「策略信号解读」已停用"


def test_pending_card_outranks_every_routed_action():
    """The voice router maps a waiting card's 确认/取消 ahead of the 策略编辑卡, tours
    and replay control, and those routed actions never include confirm or
    cancel — so no skill switch can demote the card (checklist 8, voice side)."""
    assert route_utterance("确认", pending=True) == "confirm"
    assert route_utterance("取消", pending=True) == "cancel"
    assert route_utterance("确认", pending=True, editor_open=True) == "confirm"
    assert route_utterance("算了", pending=True, tour_active=True) == "cancel"
    assert route_utterance("确认", pending=False) != "confirm"
    assert not (set(ROUTED_ACTION_SKILLS) & {"confirm", "cancel"})
    assert not (set(ROUTED_ACTION_SKILLS.values()) & {"order-proposal"})


# -- docs stay current (AGENTS.md: no stale descriptions) -----------------------


def test_the_documented_skill_tables_list_the_registry():
    external = (REPO_ROOT / "skills" / "ai-trader" / "SKILL.md").read_text(encoding="utf-8")
    capability = (REPO_ROOT / "docs" / "FUTURES_AGENT_CAPABILITY_V1.md").read_text(encoding="utf-8")
    from trader_runtime.app_help import HELP
    help_text = "\n".join(HELP["skills"]["detail"])
    # The skills chapter is generated from this registry at load, never a frozen copy in the help data.
    assert {"generated": "skills"} in json.loads(HELP_CATALOG.read_text(encoding="utf-8"))["topics"]["skills"]["detail"]
    for skill in all_skills():
        for document, text in (("skills/ai-trader", external),
                               ("docs/FUTURES_AGENT_CAPABILITY_V1", capability),
                               ("app_help", help_text)):
            assert skill.skill_id in text, f"{document} lacks {skill.skill_id}"
