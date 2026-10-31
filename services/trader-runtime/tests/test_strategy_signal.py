"""explain_strategy_signal — the read-only strategy-signal read: the open/hold/partial conclusions,
the numbered chart annotations it reuses from annotate_chart, the replay clock, the member and tier
boundaries, and the monitoring alert's one-per-(action, bar) debounce with its notify preference.
Read-only means read-only: these tests also assert no proposal, intent snapshot or execution attempt
row ever appears from an explanation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from test_replay import IDENTIFIER, HistoricalSource
from test_strategy_monitor import (
    BAR_TIME,
    USER,
    AutonomyStub,
    DataStub,
    ProposalStub,
    _save_strategy,
)
from test_strategy_workbench import group
from test_user_plans import (  # noqa: F401  # pytest fixtures / helpers
    friend_and_owner,
    friend_user_id,
    owner_headers,
    plan_app,
)
from trader_runtime.broker import EventBroker
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.proposals import ProposalConflict, ProposalService
from trader_runtime.replay import ReplayProvider
from trader_runtime.skill_registry import hidden_tool_names
from trader_runtime.store import RuntimeStore
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_rules import StrategyPlan
from trader_runtime.strategy_workbench import definition_for

# The fake workbench serves 80 rising bars; evaluate_rules drops the forming last one, so the
# evaluated bar is index 78 (close 822.3) — 1_788_192_000 + 78 * 60 seconds.
SYMBOL = "SHFE.rb2610"  # no fake position on it, so an entry intent is an open
AU = "SHFE.au2612"
EVALUATED_BAR = "1788196680000000000"
BAR_TIME_2 = "1767225900000000000"  # one 300-second bar after monitor's BAR_TIME
TOKEN = "signal-test-token"
ORIGIN = "http://127.0.0.1:5173"


def signal_plan(
    *, entry_value: float = 820.0, direction: str = "long", max_position: int = 2,
    entry_long: dict[str, Any] | None = None,
) -> StrategyPlan:
    return StrategyPlan(
        name="信号解读测试", description="价格高于阈值开多；只用已完成K线判断。",
        direction=direction, maxPosition=max_position, maxLoss=50000,
        stopLossTicks=20, takeProfitTicks=40, cooldownBars=0,
        rules={
            "entryLong": entry_long or group("gt", entry_value),
            "exitLong": group("lt", 100.0),
            "entryShort": None if direction == "long" else group("lt", 100.0),
            "exitShort": None if direction == "long" else group("gt", 90000.0),
        },
    )


def partial_entry_group() -> dict[str, Any]:
    """Two close-price conditions in one all-joined group: one satisfied, one still waiting."""
    def condition(op: str, value: float, label: str) -> dict[str, Any]:
        return {"label": label, "left": {"indicator": "close", "timeframe": 60}, "operator": op,
                "right": {"indicator": "constant", "value": value}, "withinBars": 1}
    return {"join": "all", "sequence": False, "conditions": [
        condition("gt", 820.0, "站上阈值"), condition("gt", 900.0, "继续走高"),
    ]}


def digest_of(definition: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


def _save(
    store: RuntimeStore, *, plan_obj: StrategyPlan, strategy_id: str, name: str,
    user: str = USER, symbol: str = SYMBOL, status: str = "monitoring",
) -> dict[str, Any]:
    definition = definition_for(plan_obj)
    return store.save_strategy_draft(
        strategy_id=strategy_id, user_id=user, source_session_id="session-signal-tests",
        name=name, symbol=symbol, version=1, status=status,
        definition=definition, definition_digest=digest_of(definition),
    )


def _signal_service(store: RuntimeStore, provider: FakeReadOnlyDataProvider) -> ProposalService:
    return ProposalService(store, EventBroker(store), data_provider=provider)


def assert_read_only(store: RuntimeStore) -> None:
    for table in ("proposals", "strategy_intent_snapshots", "strategy_execution_attempts"):
        assert store.connection.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_open_signal_explains_and_annotates_without_side_effects(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-open.sqlite3")
    proposals = _signal_service(store, FakeReadOnlyDataProvider())
    _save(store, plan_obj=signal_plan(), strategy_id="strategy-signal-open", name="信号开多测试")

    item = proposals.explain_strategy_signal(
        user_id=USER, turn_id="turn-signal-open", symbol=SYMBOL)

    assert item["action"] == "open_long" and item["conclusion"] == "开多"
    assert item["symbol"] == SYMBOL and item["status"] == "monitoring"
    assert item["validated"] is False and "未通过验证" in item["summary"]
    assert item["referencePrice"] == 823.5  # the fake snapshot's price, priceTick 0.5
    assert item["stopPrice"] == 823.5 - 20 * 0.5
    assert item["takeProfitPrice"] == 823.5 + 40 * 0.5
    assert item["volume"] == 2  # maxPosition
    assert item["evaluatedBarTime"] == EVALUATED_BAR
    assert item["executionMode"] == "simulation_only" and item["liveTrading"] == "disabled"
    annotations = item["annotations"]
    ids = [a["id"] for a in annotations]
    assert ids[0] == "signal-entry" and {"signal-stop", "signal-take"} <= set(ids)
    assert len([i for i in ids if i.startswith("signal-cond-")]) == 1
    assert len(annotations) <= 6
    entry = annotations[0]
    assert entry["kind"] == "point" and entry["role"] == "buy"
    assert entry["price"] == 823.5 and entry["time"] == int(EVALUATED_BAR) // 1_000_000_000
    lines = {(a["id"], a["kind"]) for a in annotations}
    assert ("signal-stop", "hline") in lines and ("signal-take", "hline") in lines
    assert all(len(a["label"]) <= 8 and a["label"][0] in "①②③④⑤⑥" for a in annotations)
    assert_read_only(store)
    store.close()


def test_hold_signal_explains_waiting_conditions(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-hold.sqlite3")
    proposals = _signal_service(store, FakeReadOnlyDataProvider())
    _save(store, plan_obj=signal_plan(entry_value=900.0),
          strategy_id="strategy-signal-hold", name="信号观望测试")

    item = proposals.explain_strategy_signal(
        user_id=USER, turn_id="turn-signal-hold", symbol=SYMBOL)

    assert item["action"] == "hold" and item["conclusion"] == "观望"
    assert item["annotations"] == []
    assert "条件尚未同时满足" in item["summary"]
    waiting = [c for c in item["conditions"] if c["status"] == "waiting"]
    assert waiting and "900.0000" in waiting[0]["detail"]  # the gap, in the condition's own words
    assert_read_only(store)
    store.close()


def test_partially_satisfied_group_shows_each_conditions_gap(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-partial.sqlite3")
    proposals = _signal_service(store, FakeReadOnlyDataProvider())
    _save(store, plan_obj=signal_plan(entry_long=partial_entry_group()),
          strategy_id="strategy-signal-partial", name="信号部分满足测试")

    item = proposals.explain_strategy_signal(
        user_id=USER, turn_id="turn-signal-partial", symbol=SYMBOL)

    # One leg satisfied does not open: the group is all-joined, so the whole entry still waits,
    # and each condition carries its own gap in its detail.
    assert item["action"] == "hold"
    details = {c["detail"]: c["status"] for c in item["conditions"]
               if " 分钟 · " in c.get("detail", "")}
    assert details["1 分钟 · 822.3000 / 820.0000"] == "passed"
    assert details["1 分钟 · 822.3000 / 900.0000"] == "waiting"
    store.close()


def test_stale_market_refuses_with_the_freshness_code(tmp_path: Path) -> None:
    class StaleProvider(FakeReadOnlyDataProvider):
        def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
            return {**super().market_workbench(symbol, duration_seconds), "ageSeconds": 999}

    store = RuntimeStore(tmp_path / "signal-stale.sqlite3")
    proposals = _signal_service(store, StaleProvider())
    _save(store, plan_obj=signal_plan(), strategy_id="strategy-signal-stale", name="信号不新鲜测试")

    with pytest.raises(ProposalConflict) as raised:
        proposals.explain_strategy_signal(user_id=USER, turn_id="t", symbol=SYMBOL)
    assert raised.value.code == "strategy_market_not_fresh"
    store.close()


def test_validation_report_clears_the_unvalidated_flag(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-validated.sqlite3")
    proposals = _signal_service(store, FakeReadOnlyDataProvider())
    saved = _save(store, plan_obj=signal_plan(),
                  strategy_id="strategy-signal-validated", name="信号已验证测试")
    store.save_strategy_validation_report(
        report_id="report-signal-validated", strategy_id=saved["strategyId"],
        user_id=USER, strategy_digest=saved["definitionDigest"],
        status="passed", checks=[], evidence={},
    )

    item = proposals.explain_strategy_signal(
        user_id=USER, turn_id="t", strategy_id=saved["strategyId"])

    assert item["validated"] is True and "未通过验证" not in item["summary"]
    store.close()


def test_candidate_selection_prefers_monitoring_and_asks_when_many(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-candidates.sqlite3")
    proposals = _signal_service(store, FakeReadOnlyDataProvider())
    _save(store, plan_obj=signal_plan(), strategy_id="strategy-signal-draft",
          name="草稿策略", status="draft")

    # No monitoring strategy: the most recently edited candidate is explained.
    item = proposals.explain_strategy_signal(user_id=USER, turn_id="t", symbol=SYMBOL)
    assert item["strategyId"] == "strategy-signal-draft"

    _save(store, plan_obj=signal_plan(), strategy_id="strategy-signal-live-a", name="监控A")
    _save(store, plan_obj=signal_plan(), strategy_id="strategy-signal-live-b", name="监控B")
    many = proposals.explain_strategy_signal(user_id=USER, turn_id="t", symbol=SYMBOL)
    assert many["requiresSelection"] is True
    assert [c["strategyId"] for c in many["candidates"]] == [
        "strategy-signal-live-b", "strategy-signal-live-a"]

    chosen = proposals.explain_strategy_signal(
        user_id=USER, turn_id="t", strategy_id="strategy-signal-live-a", symbol=SYMBOL)
    assert chosen["strategyId"] == "strategy-signal-live-a" and chosen["action"] == "open_long"
    assert_read_only(store)
    store.close()


def test_members_only_read_their_own_strategies(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-members.sqlite3")
    proposals = _signal_service(store, FakeReadOnlyDataProvider())
    _save(store, plan_obj=signal_plan(), strategy_id="strategy-signal-owner", name="所有者策略")

    with pytest.raises(KeyError):
        proposals.explain_strategy_signal(
            user_id="friend-user", turn_id="t", strategy_id="strategy-signal-owner", symbol=SYMBOL)
    with pytest.raises(ProposalConflict) as raised:
        proposals.explain_strategy_signal(user_id="friend-user", turn_id="t", symbol=SYMBOL)
    assert raised.value.code == "strategy_signal_no_candidate"
    store.close()


@pytest.mark.asyncio
async def test_replay_clock_evaluates_the_replay_bars(tmp_path: Path) -> None:
    source = HistoricalSource()
    bars = source.data["workbenches"]["60"]["bars"]
    bars[-2]["close"] = 830.0  # the last completed bar the replay evaluates ([-1] is forming)
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")

    store = RuntimeStore(tmp_path / "signal-replay.sqlite3")
    _save(store, plan_obj=signal_plan(entry_value=825.0),
          strategy_id="strategy-signal-replay", name="回放策略", symbol=AU)

    live = _signal_service(store, FakeReadOnlyDataProvider()).explain_strategy_signal(
        user_id=USER, turn_id="t-live", symbol=AU)
    assert live["action"] == "hold"  # the fake live bars close at 822.3 < 825

    replayed = ProposalService(store, EventBroker(store), data_provider=provider).explain_strategy_signal(
        user_id=USER, turn_id="t-replay", symbol=AU)
    assert replayed["action"] == "open_long" and replayed["conclusion"] == "开多"
    assert replayed["evaluatedBarTime"] == str(bars[-2]["datetime"])
    assert_read_only(store)
    store.close()


@pytest.fixture
async def app_client(tmp_path: Path):
    # agent_mode="native": /api/agent/call's tool: dispatch reaches the real
    # trader-tools set (a fake-mode adapter has none); no model key is needed
    # because these tests never start a turn.
    app = create_app(tmp_path / "signal-api.sqlite3", local_token=TOKEN,
                     agent_mode="native", cors_origins={ORIGIN})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Origin": ORIGIN}) as client:
            login = await client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
            assert login.status_code == 200
            yield app, client


@pytest.mark.asyncio
async def test_signal_route_validates_and_serves(app_client) -> None:
    app, client = app_client
    empty = await client.get("/api/strategies/signal", params={"symbol": SYMBOL})
    assert empty.status_code == 409
    assert empty.json()["detail"]["code"] == "strategy_signal_no_candidate"

    _save(app.state.store, plan_obj=signal_plan(),
          strategy_id="strategy-signal-route", name="信号路由测试")
    served = await client.get("/api/strategies/signal", params={"symbol": SYMBOL})
    assert served.status_code == 200
    item = served.json()
    assert item["action"] == "open_long" and item["conclusion"] == "开多"
    assert item["validated"] is False and "未通过验证" in item["summary"]

    invalid = await client.get("/api/strategies/signal", params={"chartTimeframeSeconds": 45})
    assert invalid.status_code == 422 and invalid.json()["detail"] == "chart_timeframe_invalid"
    assert_read_only(app.state.store)


@pytest.mark.asyncio
async def test_external_tool_explains_without_drawing(app_client) -> None:
    """tool:explain_strategy_signal — the same implementation external agents reach through
    /api/agent/call. No bound window (CLI-style call): the explanation stands, the drawing is
    honestly reported as skipped."""
    app, client = app_client
    _save(app.state.store, plan_obj=signal_plan(),
          strategy_id="strategy-signal-tool", name="信号工具测试")

    called = await client.post("/api/agent/call", json={
        "operation": "tool:explain_strategy_signal", "body": {"symbol": SYMBOL}})
    assert called.status_code == 200, called.text
    body = called.json()
    assert body["ok"] is True
    result = body["data"]
    assert result["tool"] == "explain_strategy_signal" and result["readOnly"] is True
    assert result["data"]["action"] == "open_long"
    assert result["data"]["annotationsDrawn"] is False
    assert "没有在线窗口" in result["warning"]
    assert_read_only(app.state.store)


# -- the monitoring alert ------------------------------------------------------


def mutable_intent() -> dict[str, Any]:
    """The preview payload the alert path reads: positionIntent drives the edge detection,
    evaluatedBarTime + riskAdjustedIntent prices drive the annotations."""
    return {
        "intentId": "intent-signal-alert",
        "strategyId": "strategy-monitor-test",
        "evaluatedBarTime": BAR_TIME,
        "signalSnapshot": {"signal": "buy", "barTime": BAR_TIME},
        "positionIntent": {"action": "open_long", "requestedVolume": 2,
                           "reason": "已完成K线产生买入信号。"},
        "riskAdjustedIntent": {"status": "allowed", "action": "open_long", "volume": 2,
                               "stopPrice": 813.5, "takeProfitPrice": 843.5,
                               "reason": "风险规则允许 2 手。"},
    }


def save_intent_row(store: RuntimeStore, intent: dict[str, Any]) -> None:
    """The audit row the monitor's execution attempt references (its FK); the alert
    itself needs none of it, but a run that reaches submission must not fail on it."""
    store.save_strategy_intent_snapshot(
        intent_id=intent["intentId"], strategy_id="strategy-monitor-test", user_id=USER,
        strategy_digest="a" * 64, signal_bar_time=str(intent["signalSnapshot"]["barTime"]),
        context_digest="b" * 64, payload=intent,
    )


def point_intent(intent: dict[str, Any], action: str, bar: str) -> None:
    opening = action in {"open_long", "open_short"}
    intent["evaluatedBarTime"] = bar
    intent["signalSnapshot"]["barTime"] = bar
    intent["positionIntent"] = {
        "action": action, "requestedVolume": 2 if opening else 0,
        "reason": "已完成K线产生买入信号。" if opening else "继续等待。",
    }
    intent["riskAdjustedIntent"] = {
        "status": "allowed" if opening else "no_action", "action": action,
        "volume": 2 if opening else 0,
        "stopPrice": 813.5 if opening else None, "takeProfitPrice": 843.5 if opening else None,
        "reason": "风险规则允许 2 手。" if opening else "没有可执行动作。",
    }


@pytest.mark.asyncio
async def test_monitor_alert_fires_once_per_action_and_bar(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-alert.sqlite3")
    _save_strategy(store)
    intent = mutable_intent()
    save_intent_row(store, intent)
    fired: list[dict[str, Any]] = []

    async def capture(user_id: str, payload: dict[str, Any]) -> None:
        fired.append(payload)

    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=AutonomyStub(store, DataStub()),  # type: ignore[arg-type]
    )
    monitor.signal_alert_sink = capture

    point_intent(intent, "open_long", BAR_TIME)
    await monitor.run_once()
    assert len(fired) == 1
    payload = fired[0]
    assert payload["strategyId"] == "strategy-monitor-test"
    assert payload["symbol"] == "SHFE.au2612"
    assert payload["action"] == "open_long" and payload["actionLabel"] == "开多"
    assert payload["volume"] == 2 and payload["validated"] is True
    assert payload["barTime"] == int(BAR_TIME) // 1_000_000_000
    assert payload["evaluatedBarTime"] == BAR_TIME
    assert "参考价 823.5" in payload["spoken"] and "已在图上标注" in payload["spoken"]
    assert "signal-entry" in {a["id"] for a in payload["annotations"]}
    assert payload["executionMode"] == "simulation_only"

    await monitor.run_once()  # same action, same bar: the alerted pair suppresses it
    point_intent(intent, "hold", BAR_TIME)  # 观望: not an alert action at all
    await monitor.run_once()
    point_intent(intent, "open_long", BAR_TIME)  # flip back within the same bar: still quiet
    await monitor.run_once()
    assert len(fired) == 1

    point_intent(intent, "hold", BAR_TIME_2)
    await monitor.run_once()
    point_intent(intent, "open_long", BAR_TIME_2)  # a new bar re-arms the edge
    await monitor.run_once()
    assert len(fired) == 2
    assert fired[1]["barTime"] == int(BAR_TIME_2) // 1_000_000_000
    store.close()


@pytest.mark.asyncio
async def test_restart_neither_repeats_the_alert_nor_the_order(tmp_path: Path) -> None:
    """重启 / 切槽：新进程的监控从数据库读回上次判到与提醒过的 (动作, K 线)，同一根不再提醒；
    下单仍由执行预约去重，同一根不重复下单。信号真正变化后照常提醒。"""
    store = RuntimeStore(tmp_path / "signal-restart.sqlite3")
    _save_strategy(store)
    intent = mutable_intent()
    save_intent_row(store, intent)
    fired: list[dict[str, Any]] = []

    async def capture(user_id: str, payload: dict[str, Any]) -> None:
        fired.append(payload)

    def boot() -> tuple[StrategyMonitorService, AutonomyStub]:
        autonomy = AutonomyStub(store, DataStub())
        monitor = StrategyMonitorService(
            store=store, proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
            autonomy=autonomy,  # type: ignore[arg-type]
        )
        monitor.signal_alert_sink = capture
        return monitor, autonomy

    point_intent(intent, "open_long", BAR_TIME)
    first, first_autonomy = boot()
    assert (await first.run_once())[0]["status"] == "submitted"
    assert len(fired) == 1 and len(first_autonomy.calls) == 1

    second, second_autonomy = boot()  # 同一数据库，新进程
    assert (await second.run_once())[0]["status"] in {"duplicate", "blocked"}  # 冷却或预约去重，都不下单
    assert len(fired) == 1 and second_autonomy.calls == []
    assert len(store.list_strategy_execution_attempts(strategy_id="strategy-monitor-test", user_id=USER)) == 1

    point_intent(intent, "hold", BAR_TIME_2)
    await second.run_once()
    third, _ = boot()  # 观望之后重启，再回到开多：是一次真正的信号变化
    point_intent(intent, "open_long", BAR_TIME_2)
    await third.run_once()
    assert len(fired) == 2 and fired[1]["barTime"] == int(BAR_TIME_2) // 1_000_000_000
    store.close()


@pytest.mark.asyncio
async def test_a_second_monitor_reading_stale_marks_does_not_announce_again(tmp_path: Path) -> None:
    """#783: the marks are claimed with a conditional write — a monitor that read them before another
    process moved them (blue-green overlap) loses the claim and stays quiet."""
    store = RuntimeStore(tmp_path / "signal-claim.sqlite3")
    _save_strategy(store)
    intent = mutable_intent()
    save_intent_row(store, intent)
    fired: list[dict[str, Any]] = []

    async def capture(user_id: str, payload: dict[str, Any]) -> None:
        fired.append(payload)

    monitor = StrategyMonitorService(
        store=store, proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=AutonomyStub(store, DataStub()),  # type: ignore[arg-type]
    )
    monitor.signal_alert_sink = capture
    real_marks = store.strategy_signal_marks
    point_intent(intent, "open_long", BAR_TIME)
    store.strategy_signal_marks = lambda **_: (None, None)  # type: ignore[method-assign]  # both read before either wrote
    await monitor.run_once()
    await monitor.run_once()  # the second still reads "nothing marked yet"
    assert len(fired) == 1
    store.strategy_signal_marks = real_marks  # type: ignore[method-assign]
    assert real_marks(strategy_id="strategy-monitor-test", user_id=USER)[1] == ("open_long", BAR_TIME)
    store.close()


@pytest.mark.asyncio
async def test_monitor_alert_honours_the_notify_preference(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-notify.sqlite3")
    _save_strategy(store)
    intent = mutable_intent()
    save_intent_row(store, intent)
    autonomy = AutonomyStub(store, DataStub())
    session = autonomy.broker.create_session("local", "native", USER)
    autonomy.broker.subscribers[session.session_id] = 1  # one attached SSE reader
    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=autonomy,  # type: ignore[arg-type]
    )

    point_intent(intent, "open_long", BAR_TIME)
    await monitor.run_once()
    # #932: the signal's own message is a 策略 reminder too (feed.reminder), under the same switch.
    def named(name: str) -> list:
        return [event for _, event in autonomy.broker.transient_events[session.session_id] if event["name"] == name]
    events = named("feed.strategy-signal")
    assert len(events) == 1 and events[0]["payload"]["actionLabel"] == "开多"
    reminders_before = len(named("feed.reminder"))
    assert all(event["payload"]["kind"] == "strategy" for event in named("feed.reminder"))

    store.update_preferences(USER, {"notify": {"strategySignalAlerts": False}})
    point_intent(intent, "hold", BAR_TIME_2)
    await monitor.run_once()
    point_intent(intent, "open_long", BAR_TIME_2)  # the edge fires, the sink declines to push
    await monitor.run_once()
    assert len(named("feed.strategy-signal")) == 1 and len(named("feed.reminder")) == reminders_before
    store.close()


@pytest.mark.asyncio
async def test_monitor_alert_only_for_monitoring_strategies(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "signal-draft.sqlite3")
    _save_strategy(store, status="draft")
    intent = mutable_intent()
    fired: list[dict[str, Any]] = []

    async def capture(user_id: str, payload: dict[str, Any]) -> None:
        fired.append(payload)

    monitor = StrategyMonitorService(
        store=store,
        proposals=ProposalStub(store, intent),  # type: ignore[arg-type]
        autonomy=AutonomyStub(store, DataStub()),  # type: ignore[arg-type]
    )
    monitor.signal_alert_sink = capture

    point_intent(intent, "open_long", BAR_TIME)
    assert await monitor.run_once() == []
    assert fired == []
    store.close()


# -- tier boundary -------------------------------------------------------------


@pytest.mark.asyncio
async def test_strategies_tier_off_refuses_the_read_and_hides_the_tool(
    plan_app, friend_and_owner,  # noqa: F811  # fixtures
) -> None:
    friend, owner = friend_and_owner
    store = plan_app.state.store
    assert "explain_strategy_signal" not in hidden_tool_names(store, friend_user_id())

    switch = await owner.put("/api/admin/plans/basic",
                             json={"features": {"strategies": False}}, headers=owner_headers())
    assert switch.status_code == 200

    refused = await friend.get("/api/strategies/signal", params={"symbol": SYMBOL},
                               headers=owner_headers())
    assert refused.status_code == 403 and refused.json()["detail"] == "feature_not_in_plan"
    # The same hidden-set bridge.tools consults when listing (and accepting) tool: operations.
    assert "explain_strategy_signal" in hidden_tool_names(store, friend_user_id())
