"""侧栏重设计第三期 (2026-09-25): 执行方式 — how a monitored instance handles its signal.

alert 只提醒 (one message, no card, no order), confirm 出卡确认 (one card per leg, once per completed
bar; confirming it is the ordinary confirm and the fill reads as the strategy's), auto 自动下单 (the
unchanged path; with AI 自主执行 off it is handled as confirm). Plus the start/stop routes, the
"today" figures of the monitor payload, the automatic-pause notice and the object-level AI focus.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from test_simulation_orders import USER, Gateway, NormalProvider, application, quote_now
from test_strategy_monitor import (
    STRATEGY_ID,
    AutonomyStub,
    DataStub,
    ProposalStub,
    _definition,
    _intent,
    _save_strategy,
)
from trader_runtime.message_context import decode_instruction, encode_instruction
from trader_runtime.store import RuntimeStore
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_workbench import order_sources

ORIGIN = "http://127.0.0.1:5173"
TRADING_DAY = "2026-08-25"  # the fake account's trading day (fake_account_summary)


class CardProposals(ProposalStub):
    """The monitor's proposal service: the saved intent, and the app's real card creation."""

    def __init__(self, app, intent: dict[str, Any]) -> None:
        super().__init__(app.state.store, intent)
        self.real = app.state.proposals

    def create_trade(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.real.create_trade(*args, **kwargs)


class Voice:
    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []

    def user_calls(self, user_id: str) -> int:
        return 1

    def announce(self, kind: str, text: str, *, user_id: str, **_: object) -> None:
        self.said.append((kind, text))


def _setup(tmp_path: Path, *, signal_action: str, autonomy: bool = True):
    provider = NormalProvider()
    provider.position_items = []
    app = application(tmp_path / f"signal-{signal_action}.sqlite3", Gateway(), provider)
    store = app.state.store
    _save_strategy(store)
    session = app.state.broker.create_session("local", "native", USER)
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action=signal_action,
                                     session_id=session.session_id)
    intent = _intent(store)
    stub = AutonomyStub(store, DataStub(trading_day=TRADING_DAY))
    stub.ai_autonomy = autonomy
    monitor = StrategyMonitorService(store=store, proposals=CardProposals(app, intent),  # type: ignore[arg-type]
                                     autonomy=stub)  # type: ignore[arg-type]
    voice = Voice()
    monitor.voice_lookup = lambda: voice
    return SimpleNamespace(app=app, store=store, provider=provider, session=session.session_id,
                           monitor=monitor, autonomy=stub, voice=voice)


def _events(store: RuntimeStore, session_id: str, name: str) -> list[dict[str, Any]]:
    return [event["payload"] for _, event in store.list_events(session_id) if event.get("name") == name]


async def _login(client: AsyncClient) -> None:
    assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200


@pytest.mark.asyncio
async def test_confirm_issues_one_card_per_bar_and_counts_nothing_until_confirmed(tmp_path: Path) -> None:
    env = _setup(tmp_path, signal_action="confirm")

    first = await env.monitor.run_once()
    again = await env.monitor.run_once()

    assert first[0]["status"] == "proposed"
    assert again[0]["status"] in {"waiting", "duplicate"}
    cards = _events(env.store, env.session, "proposal.trade")
    assert len(cards) == 1  # the same completed bar never brings a second card
    card = cards[0]
    assert (card["side"], card["volume"], card["status"]) == ("buy_open", 2, "approval_required")
    assert card["strategySource"]["strategyId"] == STRATEGY_ID
    assert card["reason"].startswith("已验证模拟策略：")
    assert env.autonomy.calls == []  # nothing was sent
    notices = [item["text"] for item in _events(env.store, env.session, "message.completed")]
    assert any("确认卡" in text and "确认后才下单" in text for text in notices)
    assert env.voice.said and env.voice.said[0][0] == "strategy-card"
    attempt = env.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)[0]
    assert attempt["status"] == "SKIPPED" and attempt["cardProposalIds"] == [card["proposalId"]]
    # An unconfirmed card is not an open toward maxOrdersPerDay.
    assert env.store.count_strategy_attempts_today(strategy_id=STRATEGY_ID, user_id=USER, trading_date=TRADING_DAY) == 0
    env.store.close()


@pytest.mark.asyncio
async def test_confirming_the_strategy_card_attributes_the_fill_to_the_strategy(tmp_path: Path) -> None:
    env = _setup(tmp_path, signal_action="confirm")
    await env.monitor.run_once()
    card = _events(env.store, env.session, "proposal.trade")[0]
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        env.provider.quote_datetime = quote_now()
        confirmed = await client.post(f"/api/proposals/{card['proposalId']}/confirm", json={
            "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": "strategy-card-1"})
    assert confirmed.status_code == 200, confirmed.text
    client_order_id = confirmed.json()["clientOrderId"]
    assert order_sources(env.store, USER)[client_order_id] == {"source": "strategy", "strategyName": "已验证模拟策略"}
    attempt = env.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)[0]
    assert attempt["status"] == "SUBMITTED" and attempt["clientOrderId"] == client_order_id
    # Confirmed and sent: now it is one of today's opens.
    assert env.store.count_strategy_attempts_today(strategy_id=STRATEGY_ID, user_id=USER, trading_date=TRADING_DAY) == 1
    env.store.close()


@pytest.mark.asyncio
async def test_alert_only_sends_one_message(tmp_path: Path) -> None:
    env = _setup(tmp_path, signal_action="alert")

    first = await env.monitor.run_once()
    await env.monitor.run_once()

    assert first[0]["status"] == "alerted"
    notices = [item["text"] for item in _events(env.store, env.session, "message.completed")]
    assert len(notices) == 1 and notices[0].endswith("开多条件满足（只提醒，未下单）")
    assert _events(env.store, env.session, "proposal.trade") == []
    assert env.autonomy.calls == []
    # Recorded (so a restart does not remind again), never as an open.
    attempts = env.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
    assert [(item["status"], item["signalAction"]) for item in attempts] == [("SKIPPED", "alert")]
    assert env.store.count_strategy_attempts_today(strategy_id=STRATEGY_ID, user_id=USER, trading_date=TRADING_DAY) == 0
    assert env.voice.said == [("strategy-signal", notices[0])]
    env.store.close()


@pytest.mark.asyncio
async def test_auto_executes_as_before(tmp_path: Path) -> None:
    env = _setup(tmp_path, signal_action="auto")
    result = await env.monitor.run_once()
    assert result[0]["status"] == "submitted"
    assert len(env.autonomy.calls) == 1
    assert _events(env.store, env.session, "proposal.trade") == []
    # 执行回报卡 (2026-10-04): the automatic order leaves a record in the instance's conversation —
    # the same session its confirmation cards go to — with the read-back state, on the notice's turn.
    cards = _events(env.store, env.session, "card.execution")
    assert len(cards) == 1
    card = cards[0]
    assert card["source"] == "strategy" and card["sourceLabel"] == "策略 · 已验证模拟策略"
    assert (card["status"], card["statusLabel"]) == ("submitted", "已报")
    assert card["clientOrderId"] == "strategy-order-1" and card["cardId"] == "execution-strategy-order-1"
    notice = [item for item in _events(env.store, env.session, "message.completed") if item["turnId"] == card["turnId"]]
    assert len(notice) == 1 and "自动下单 1 笔委托" in notice[0]["text"]
    assert not [kind for kind, _ in env.voice.said if kind == "strategy-execution"]  # fills have their own voice
    env.store.close()


@pytest.mark.asyncio
async def test_auto_order_with_an_unknown_outcome_shows_unknown_never_success(tmp_path: Path) -> None:
    env = _setup(tmp_path, signal_action="auto")
    env.autonomy.outcomes = ["结果未知"]
    result = await env.monitor.run_once()
    assert result[0]["status"] == "unknown"
    card = _events(env.store, env.session, "card.execution")[0]
    assert (card["status"], card["statusLabel"]) == ("unknown", "结果未知")
    assert "不会重发" in card["detail"]
    env.store.close()


@pytest.mark.asyncio
async def test_auto_with_ai_autonomy_off_is_handled_as_confirm(tmp_path: Path) -> None:
    """User decision 2026-09-25: AI 自主执行 off no longer means 只监控不下单 — the signal still
    reaches the user as a card, which they confirm."""
    env = _setup(tmp_path, signal_action="auto", autonomy=False)
    result = await env.monitor.run_once()
    assert result[0]["status"] == "proposed"
    assert env.autonomy.calls == []
    assert len(_events(env.store, env.session, "proposal.trade")) == 1
    assert env.monitor.signal_mode(env.store.get_strategy_draft(STRATEGY_ID, user_id=USER)) == "confirm"
    env.store.close()


@pytest.mark.asyncio
async def test_start_route_takes_signal_action_and_refuses_auto_without_autonomy(tmp_path: Path) -> None:
    app = application(tmp_path / "start.sqlite3", Gateway())
    store = app.state.store
    _save_strategy(store, status="ready")
    store.save_strategy_validation_report(report_id="validation-start", strategy_id=STRATEGY_ID, user_id=USER,
                                          strategy_digest="a" * 64, status="passed", checks=[], evidence={})
    other = app.state.broker.create_session("local", "native", "someone-else")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        session = (await client.post("/api/sessions", json={"mode": "local"})).json()["sessionId"]
        store.update_preferences(USER, {"trade": {"aiAutonomy": False}})
        refused = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/start", json={"signalAction": "auto"})
        foreign = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/start",
                                    json={"signalAction": "confirm", "sessionId": other.session_id})
        started = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/start",
                                    json={"signalAction": "confirm", "sessionId": session})
        switched = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/start", json={"signalAction": "alert"})
        legacy = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/start", json={})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ai_autonomy_disabled"
    assert foreign.status_code == 404
    assert started.status_code == 200 and started.json()["signalAction"] == "confirm"
    assert started.json()["status"] == "monitoring" and started.json()["signalSessionId"] == session
    # Calling start while monitoring changes the 执行方式; the body-less call keeps it.
    assert switched.json()["signalAction"] == "alert" and switched.json()["signalSessionId"] == session
    assert legacy.status_code == 200 and legacy.json()["signalAction"] == "alert"
    store.close()


@pytest.mark.asyncio
async def test_stop_returns_to_ready_keeps_positions_and_ends_signals(tmp_path: Path) -> None:
    env = _setup(tmp_path, signal_action="confirm")
    env.store.save_strategy_validation_report(report_id="validation-stop", strategy_id=STRATEGY_ID, user_id=USER,
                                              strategy_digest="a" * 64, status="passed", checks=[], evidence={})
    env.provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 1, "todayVolume": 1,
                                    "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        stopped = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/stop", json={})
    assert stopped.status_code == 200
    body = stopped.json()
    assert body["status"] == "ready" and body["positionsKept"] is True and "不会平掉" in body["notice"]
    assert await env.monitor.run_once() == []  # no longer judged: no reminder, no card
    assert _events(env.store, env.session, "proposal.trade") == []
    assert env.provider.positions()[0]["volume"] == 1  # nothing closed
    env.store.close()


def test_migration_defaults_existing_rows_to_auto_and_runs_twice(tmp_path: Path) -> None:
    path = tmp_path / "migrate.sqlite3"
    store = RuntimeStore(path)
    _save_strategy(store)
    # A database from before the columns: drop them the way an older build would have left it.
    store.connection.execute("ALTER TABLE strategy_drafts DROP COLUMN signal_session_id")
    store.connection.execute("ALTER TABLE strategy_drafts DROP COLUMN signal_action")
    store.connection.execute("ALTER TABLE strategy_execution_attempts DROP COLUMN card_proposal_ids")
    store.connection.commit()
    store.close()
    for _ in range(2):
        store = RuntimeStore(path)
        row = store.get_strategy_draft(STRATEGY_ID, user_id=USER)
        assert row["signalAction"] == "auto" and row["signalSessionId"] is None
        store.close()


@pytest.mark.asyncio
async def test_monitor_payload_blanks_today_figures_from_another_trading_day(tmp_path: Path) -> None:
    app = application(tmp_path / "today.sqlite3", Gateway())
    store = app.state.store
    _save_strategy(store)
    store.save_strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER, trading_date="2026-08-24",
                                      high_watermark_equity="10000", last_today_pnl="187", consecutive_losses=2,
                                      last_trade_id=None, last_executed_bar=None, pause_reason=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        stale = (await client.get(f"/api/strategies/{STRATEGY_ID}/monitor")).json()
        store.save_strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER, trading_date=TRADING_DAY,
                                          high_watermark_equity="10000", last_today_pnl="-35", consecutive_losses=1,
                                          last_trade_id=None, last_executed_bar=None, pause_reason=None)
        fresh = (await client.get(f"/api/strategies/{STRATEGY_ID}/monitor")).json()
    assert stale["tradingDay"] == TRADING_DAY
    assert stale["today"] is None and stale["opensToday"] is None
    assert stale["monitorState"]["lastTodayPnl"] is None and stale["monitorState"]["consecutiveLosses"] == 0
    assert fresh["today"] == {"tradingDate": TRADING_DAY, "pnl": "-35", "consecutiveLosses": 1}
    assert fresh["opensToday"] == 0
    store.close()


@pytest.mark.asyncio
async def test_automatic_pause_posts_a_message_card_once(tmp_path: Path) -> None:
    """Drawdown over the budget pauses a (schema 1) instance: one message in its conversation."""
    env = _setup(tmp_path, signal_action="auto")
    env.autonomy.data_provider.equity = 10_000
    await env.monitor.run_once()  # high watermark 10000, one submitted open
    env.autonomy.data_provider.equity = 8_000
    result = await env.monitor.run_once()
    assert result[0]["status"] == "paused"
    texts = [item["text"] for item in _events(env.store, env.session, "message.completed")]
    paused = [text for text in texts if "已自动暂停" in text]
    assert len(paused) == 1 and "已有持仓不受影响" in paused[0]
    assert ("strategy-paused", paused[0]) in env.voice.said
    env.store.close()


def test_focus_reaches_the_agent_instruction() -> None:
    from trader_runtime.message_context import FuturesWorkspaceContext
    context = FuturesWorkspaceContext.model_validate({
        "view": "trade", "symbol": "SHFE.rb2701", "durationSeconds": 60, "quoteDatetime": "2026-09-25 10:00:00",
        "capturedAt": "2026-09-25T02:00:00Z", "ageSeconds": 1, "dataStatus": "normal",
        "focus": {"kind": "strategy_instance", "id": STRATEGY_ID, "label": "双均线 · 螺纹2701"},
    })
    decoded = decode_instruction(encode_instruction("为什么没开仓", context))
    assert decoded.focus == {"kind": "strategy_instance", "id": STRATEGY_ID, "label": "双均线 · 螺纹2701"}
    assert "get_strategy_monitor_status" in decoded.model_text and "本轮不得直接下单" in decoded.model_text
    assert decoded.user_text == "为什么没开仓"


@pytest.mark.asyncio
async def test_focus_label_rides_on_the_user_message(tmp_path: Path) -> None:
    app = application(tmp_path / "focus.sqlite3", Gateway())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        session = (await client.post("/api/sessions", json={"mode": "local"})).json()["sessionId"]
        sent = await client.post(f"/api/sessions/{session}/messages", json={"text": "这笔持仓的风险", "workspaceContext": {
            "view": "trade", "symbol": "SHFE.au2612", "durationSeconds": 60, "quoteDatetime": "2026-09-25 10:00:00",
            "capturedAt": "2026-09-25T02:00:00Z", "ageSeconds": 1, "dataStatus": "normal",
            "focus": {"kind": "position", "id": "SHFE.au2612:long", "label": "黄金2612 多 2 手"}}})
        assert sent.status_code == 202
        import asyncio
        for _ in range(50):
            users = [item for item in _events(app.state.store, session, "message.completed")
                     if item.get("role") == "user" and item.get("text") == "这笔持仓的风险"]
            if users:
                break
            await asyncio.sleep(0.05)
    assert users[0]["focus"] == {"kind": "position", "label": "黄金2612 多 2 手"}
    app.state.store.close()


def test_ui_commands_cover_the_launch_dialog_signal_action_stop_confirm_and_ai_menus() -> None:
    """AI operability: every new surface has a command and a snapshot field."""
    from pydantic import ValidationError

    from trader_runtime.ui_control import UiChanges, UiState, changes_match
    for change in ({"dialog": {"name": "strategy-launch", "open": True}},
                   {"strategyLaunch": {"symbol": "SHFE.rb2701", "signalAction": "confirm"}},
                   {"disclosure": {"name": "strategy-signal-action", "open": True, "id": STRATEGY_ID}},
                   {"disclosure": {"name": "strategy-stop-confirm", "open": True, "id": STRATEGY_ID}},
                   {"disclosure": {"name": "ai-menu", "open": True, "id": f"instance:{STRATEGY_ID}"}}):
        UiChanges.model_validate(change)
    with pytest.raises(ValidationError):
        UiChanges.model_validate({"strategyLaunch": {}})
    state = {"strategyInstances": [{"strategyId": STRATEGY_ID, "signalAction": "alert", "effectiveSignalAction": "alert"}],
             "strategyLaunch": {"open": True, "symbol": "SHFE.rb2701", "signalAction": "confirm"}}
    assert changes_match(state, {"strategyLaunch": {"symbol": "SHFE.rb2701", "signalAction": "confirm"}})
    assert set(UiState.model_fields) >= {"strategyLaunch", "strategyInstances"}


# -- 审查修正 (2026-09-25): strategy protection on confirmed cards, durable dedupe ---------------


def _intent_at(store: RuntimeStore, bar: str, action: str = "open_long") -> dict[str, Any]:
    """Another completed bar's intent for the same strategy (hold = the signal went away)."""
    intent_id = f"intent-{bar}-{action}"
    status = "allowed" if action != "hold" else "no_action"
    payload = {
        "intentId": intent_id, "strategyId": STRATEGY_ID,
        "signalSnapshot": {"signal": "buy" if action != "hold" else "wait", "barTime": bar},
        "positionIntent": {"action": action, "requestedVolume": 2 if action != "hold" else 0, "reason": "已完成K线产生买入信号。"},
        "riskAdjustedIntent": {"status": status, "action": action, "volume": 2 if action != "hold" else 0,
                               "reason": "已完成K线产生买入信号。"},
    }
    store.save_strategy_intent_snapshot(intent_id=intent_id, strategy_id=STRATEGY_ID, user_id=USER,
                                        strategy_digest="a" * 64, signal_bar_time=bar,
                                        context_digest=f"{bar}-{action}".ljust(64, "c")[:64], payload=payload)
    return payload


def _rebuilt(env) -> StrategyMonitorService:
    """A fresh monitor on the same database: what a Runtime restart (a blue-green switch) is."""
    monitor = StrategyMonitorService(store=env.store, proposals=CardProposals(env.app, env.monitor.proposals.intent),  # type: ignore[arg-type]
                                     autonomy=env.autonomy)  # type: ignore[arg-type]
    monitor.voice_lookup = lambda: env.voice
    return monitor


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["confirm", "alert"])
async def test_the_same_signal_is_not_offered_again_after_a_restart_but_is_once_it_changed(tmp_path: Path, mode: str) -> None:
    env = _setup(tmp_path, signal_action=mode)
    assert (await env.monitor.run_once())[0]["status"] in {"proposed", "alerted"}

    def offers() -> int:
        return len(_events(env.store, env.session, "proposal.trade")) if mode == "confirm" else len(
            [item for item in _events(env.store, env.session, "message.completed") if "只提醒" in item["text"]])
    # Restart; the signal is still open_long on the next completed bar: nothing new.
    restarted = _rebuilt(env)
    restarted.proposals.intent = _intent_at(env.store, "1767225900000000000")
    env.provider.quote_datetime = quote_now()
    again = await restarted.run_once()
    assert again[0]["status"] == "waiting" and offers() == 1
    # The signal goes away (hold) and comes back after another restart: offered once more.
    restarted.proposals.intent = _intent_at(env.store, "1767226200000000000", "hold")
    await restarted.run_once()
    back = _rebuilt(env)
    back.proposals.intent = _intent_at(env.store, "1767226500000000000")
    env.provider.quote_datetime = quote_now()
    assert (await back.run_once())[0]["status"] in {"proposed", "alerted"}
    assert offers() == 2
    # A reminder is recorded, but it is never an open and never uses up the alignment segment.
    attempts = env.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
    assert {attempt["signalAction"] for attempt in attempts} == {mode}
    assert env.store.count_strategy_attempts_today(strategy_id=STRATEGY_ID, user_id=USER, trading_date=TRADING_DAY) == 0
    env.store.close()


def _fill(order: dict[str, Any], trade_id: str) -> dict[str, Any]:
    request = order["request"]
    return {"tradeId": trade_id, "clientOrderId": order["clientOrderId"], "symbol": request["symbol"],
            "direction": request["direction"], "offset": request["offset"], "price": float(request["limitPrice"]),
            "volume": int(request["volume"]), "tradeDateTime": 1788196800000000000, "tradingDay": TRADING_DAY}


@pytest.mark.asyncio
async def test_a_confirmed_strategy_card_is_protected_like_the_monitors_own_order(tmp_path: Path) -> None:
    """The strategy's stop / take profit follow the confirmed card's fill exactly as they follow the
    auto order of the same signal; an ordinary card carries no protection."""
    import json

    from trader_runtime.strategy_protection import sync_protection
    env = _setup(tmp_path, signal_action="confirm")
    autonomy = env.app.state.autonomy
    await env.monitor.run_once()
    card = _events(env.store, env.session, "proposal.trade")[0]
    # The monitor's own order for the same signal (what auto sends), for comparison — auto only
    # sends while the strategy is on 自动下单 (#528-B); the card above was already issued.
    monitor_session = env.app.state.broker.create_session("local", "strategy-monitor", USER)
    env.store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action="auto", session_id=None)
    env.provider.quote_datetime = quote_now()
    auto = await autonomy.execute_trade(user_id=USER, session_id=monitor_session.session_id, symbol="SHFE.au2612",
                                        side="buy_open", volume=2, price_intent="aggressive", reason="策略 auto",
                                        strategy_id=STRATEGY_ID, ai_initiated=True)
    plain = env.app.state.proposals.create_trade(env.session, "turn-plain", symbol="SHFE.au2612", side="buy_open",
                                                 volume=1, price_intent="aggressive", reason="手动卡")
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        results = []
        for item, key in ((card, "protect-card"), (plain, "protect-plain")):
            env.provider.quote_datetime = quote_now()
            response = await client.post(f"/api/proposals/{item['proposalId']}/confirm", json={
                "approvalId": item["approvalId"], "proposalVersion": item["version"], "idempotencyKey": key})
            assert response.status_code == 200, response.text
            results.append(response.json())

    def protection(client_order_id: str) -> dict[str, Any] | None:
        row = env.store.connection.execute("SELECT protection_json FROM tqkq_sim_order_links WHERE client_order_id = ?",
                                           (client_order_id,)).fetchone()
        return json.loads(row["protection_json"]) if row["protection_json"] else None
    auto_protection, card_protection = protection(auto["clientOrderId"]), protection(results[0]["clientOrderId"])
    assert protection(results[1]["clientOrderId"]) is None
    assert auto_protection is not None and card_protection is not None
    for key in ("strategyId", "stopLossTicks", "takeProfitTicks", "perTradeRisk", "timeframeSeconds", "priceTick", "contractMultiplier"):
        assert card_protection[key] == auto_protection[key], key
    # Both fill at the same price: the strategy arms the same stop and take profit for each.
    orders = [env.app.state.simulation_orders.get(identifier, user_id=USER)
              for identifier in (auto["clientOrderId"], results[0]["clientOrderId"], results[1]["clientOrderId"])]
    fills = [_fill(order, f"trade-{index}") for index, order in enumerate(orders)]
    base_activity = env.provider.account_activity()
    env.provider.account_activity = lambda: {**base_activity, "dataStatus": "normal", "isCached": False, "ageSeconds": 0,
                                             "ordersInitialized": True, "trades": fills,
                                             "orders": [{"clientOrderId": order["clientOrderId"], "status": "FINISHED",
                                                         "volume": order["request"]["volume"], "volumeLeft": 0} for order in orders]}
    assert sync_protection(env.store, env.provider, trading_account_id=env.store.default_trading_account_id) == {}
    stops = env.store.connection.execute(
        """SELECT trigger_price, take_profit_price, strategy_metadata_json FROM ai_stop_orders
        WHERE strategy_metadata_json IS NOT NULL ORDER BY created_at""").fetchall()
    by_entry = {json.loads(row["strategy_metadata_json"])["entryClientOrderId"]: row for row in stops}
    assert set(by_entry) == {auto["clientOrderId"], results[0]["clientOrderId"]}  # the ordinary card arms nothing
    auto_stop, card_stop = by_entry[auto["clientOrderId"]], by_entry[results[0]["clientOrderId"]]
    assert (card_stop["trigger_price"], card_stop["take_profit_price"]) == (auto_stop["trigger_price"], auto_stop["take_profit_price"])
    entry = float(orders[0]["request"]["limitPrice"])
    assert float(card_stop["trigger_price"]) == entry - 2 * 0.5 and float(card_stop["take_profit_price"]) == entry + 4 * 0.5
    env.store.close()


# -- 复审 (2026-09-25): AI 自主执行 off is not the user's stop -------------------------------------


async def _switched_off(tmp_path: Path):
    """The real chain's setup: a monitored instance set to 自动下单, the user's AI lease ACTIVE, then
    设置 → 交易 → AI 自主执行 switched off through PUT /api/preferences (which stops that lease)."""
    from test_strategy_protection import Provider
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "switch-off.sqlite3", gateway, provider)
    store = app.state.store
    _save_strategy(store)
    session = app.state.broker.create_session("local", "native", USER).session_id
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action="auto", session_id=session)
    intent = _intent(store)
    intent["riskAdjustedIntent"]["volume"] = 1
    autonomy = app.state.autonomy
    autonomy.ensure_runtime_access(user_id=USER, session_id=session, symbol="SHFE.au2612", permission="open")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        assert (await client.put("/api/preferences", json={"trade": {"aiAutonomy": False}})).status_code == 200
    lease = autonomy.current(user_id=USER)
    assert (lease["status"], lease["stopReason"]) == ("STOPPED", "ai_autonomy_off")
    assert autonomy.controls(user_id=USER)["stopStatus"] is None  # the switch is not the user's stop
    monitor = StrategyMonitorService(store=store, proposals=CardProposals(app, intent), autonomy=autonomy)  # type: ignore[arg-type]
    return SimpleNamespace(app=app, store=store, provider=provider, gateway=gateway, session=session,
                           monitor=monitor, autonomy=autonomy)


async def _confirm(app, card: dict[str, Any], key: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        app.state.data_provider.quote_datetime = quote_now()
        return await client.post(f"/api/proposals/{card['proposalId']}/confirm", json={
            "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": key})


@pytest.mark.asyncio
async def test_with_ai_autonomy_off_an_auto_instances_card_is_sent_protected_and_its_stop_triggers(tmp_path: Path) -> None:
    """The main scenario of the new design, end to end: switch off → the auto instance cards →
    the user confirms → the order is sent and carries the strategy protection → the fill arms the
    stop → the price touches it → the protective close is sent."""
    from test_strategy_protection import entry_fill
    env = await _switched_off(tmp_path)
    assert (await env.monitor.run_once())[0]["status"] == "proposed"
    card = _events(env.store, env.session, "proposal.trade")[0]
    confirmed = await _confirm(env.app, card, "switch-off-card")
    assert confirmed.status_code == 200, confirmed.text
    assert env.gateway.submit_count == 1
    entry = dict(env.gateway.request)
    link = env.store.connection.execute("SELECT protection_json FROM tqkq_sim_order_links WHERE client_order_id = ?",
                                        (entry["clientOrderId"],)).fetchone()
    assert link["protection_json"] is not None
    # The entry fills at 822: the strategy's stop (2 ticks) and take profit (4 ticks) are armed.
    entry_fill(env.provider, entry, volume=1, price=822.0, time=int(datetime.now(UTC).timestamp() * 1e9))
    env.provider.position_items = [{"symbol": entry["symbol"], "direction": "long", "volume": 1, "todayVolume": 1,
                                    "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    env.provider.activity["orders"] = [{**entry, "status": "FINISHED", "volumeLeft": 0}]
    env.gateway.query_report = env.gateway.report(entry, status="FINISHED", volume_left=0)
    await env.app.state.simulation_orders.query(entry["clientOrderId"], user_id=USER)
    env.autonomy.sync_strategy_protection()
    stops = env.autonomy.list_stops(user_id=USER)
    assert [(stop["status"], stop["triggerPrice"], stop["takeProfitPrice"]) for stop in stops] == [("ARMED", 821.0, 824.0)]
    # The price touches the stop while AI 自主执行 is still off: the protective close goes out.
    env.provider.last = 820.5
    await env.autonomy.run_once()
    assert env.gateway.submit_count == 2
    assert (env.gateway.request["direction"], env.gateway.request["offset"], env.gateway.request["volume"]) == ("SELL", "CLOSETODAY", 1)
    assert env.autonomy.list_stops(user_id=USER)[0]["status"] in {"SUBMITTED", "EXECUTED"}
    env.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("emergency", [False, True])
async def test_the_users_own_stop_refuses_a_strategy_open_card_with_a_reason_and_sends_nothing(tmp_path: Path, emergency: bool) -> None:
    """停止 / 急停 by the user (not the switch) still fence the card: refused, a clear reason on the
    card and in the answer, no order."""
    env = await _switched_off(tmp_path)
    assert (await env.monitor.run_once())[0]["status"] == "proposed"
    card = _events(env.store, env.session, "proposal.trade")[0]
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        assert (await client.post("/api/ai-execution/stop", json={"emergency": emergency})).status_code == 200
    assert env.autonomy.controls(user_id=USER)["stopStatus"] == ("EMERGENCY_STOP" if emergency else "STOPPED")
    refused = await _confirm(env.app, card, f"stopped-{emergency}")
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "ai_execution_stopped" and "没有发单" in detail["message"]
    assert env.gateway.submit_count == 0
    record = env.store.get(card["proposalId"])
    assert record["status"] == "approval_required" and record["payload"]["confirmBlocked"]["reason"] == detail["message"]
    republished = _events(env.store, env.session, "proposal.trade")[-1]
    assert republished["proposalId"] == card["proposalId"] and republished["confirmBlocked"]["code"] == "ai_execution_stopped"
    env.store.close()


@pytest.mark.asyncio
async def test_an_ordinary_card_is_unaffected_by_the_switch(tmp_path: Path) -> None:
    env = await _switched_off(tmp_path)
    plain = env.app.state.proposals.create_trade(env.session, "turn-plain-off", symbol="SHFE.au2612", side="buy_open",
                                                 volume=1, price_intent="aggressive", reason="手动卡")
    confirmed = await _confirm(env.app, plain, "plain-off")
    assert confirmed.status_code == 200, confirmed.text
    assert env.gateway.submit_count == 1
    assert env.store.connection.execute("SELECT protection_json FROM tqkq_sim_order_links WHERE client_order_id = ?",
                                        (confirmed.json()["clientOrderId"],)).fetchone()["protection_json"] is None
    env.store.close()


# -- 策略只管自己开的仓 (所有者 2026-10-08) --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_confirmed_cards_fill_is_the_strategys_own_so_the_next_same_signal_holds(tmp_path: Path) -> None:
    """确认成交的策略卡带 strategyId 保护行：算作策略自己的仓。第二个同向信号到达上限就不再开仓，
    手动那手不算它的；只提醒口径（账户持仓）才会当成超上限去减仓。"""
    from test_strategy_protection import entry_fill
    from trader_runtime.risk import InstrumentSpecProvider, RiskPolicyProvider
    from trader_runtime.strategy_intents import build_strategy_intent
    env = await _switched_off(tmp_path)
    env.monitor.proposals.intent["riskAdjustedIntent"]["volume"] = 2   # 上限 2 手，一次开满
    assert (await env.monitor.run_once())[0]["status"] == "proposed"
    card = _events(env.store, env.session, "proposal.trade")[0]
    confirmed = await _confirm(env.app, card, "own-position-card")
    assert confirmed.status_code == 200, confirmed.text
    entry = dict(env.gateway.request)
    entry_fill(env.provider, entry, volume=2, price=822.0)
    # 账户 3 手 = 策略的 2 手 + 用户手动 1 手。
    env.provider.position_items = [{"symbol": entry["symbol"], "direction": "long", "volume": 3,
                                    "todayVolume": 3, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    env.provider.activity["orders"] = [{**entry, "status": "FINISHED", "volumeLeft": 0}]
    env.gateway.query_report = env.gateway.report(entry, status="FINISHED", volume_left=0)
    await env.app.state.simulation_orders.query(entry["clientOrderId"], user_id=USER)
    env.autonomy.sync_strategy_protection()

    strategy = env.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    held = env.monitor.held_position(strategy)
    assert held == {"direction": "long", "volume": 2, "legs": [(entry["symbol"], "long", 2)]}
    assert env.monitor._own_position(strategy, plan=None) == ("long", 2)

    market = {"symbol": entry["symbol"], "price": 823.0, "priceTick": 0.5, "volumeMultiple": 1000.0,
              "capturedAt": "2026-08-25T09:00:00+08:00", "ageSeconds": 0, "dataStatus": "normal"}
    facts = {"market": market, "account": {"todayNetPnl": 0.0, "ageSeconds": 0, "dataStatus": "normal", "isCached": False},
             "activity": {"ageSeconds": 0, "dataStatus": "normal", "isCached": False, "ordersInitialized": True}}
    positions = [{"symbol": entry["symbol"], "direction": "long", "volume": 3}]
    second = build_strategy_intent(
        strategy=strategy, evaluation={"signal": "buy", "evaluatedBarTime": "1767225900000000000",
                                       "dataStatus": "normal"},
        positions=positions, user_id=USER, policies=RiskPolicyProvider(),
        instruments=InstrumentSpecProvider(), own_position=("long", 2), **facts)
    assert second["positionIntent"]["action"] == "hold"          # 自己已在上限，不再开仓
    account_read = build_strategy_intent(
        strategy=strategy, evaluation={"signal": "buy", "evaluatedBarTime": "1767225900000000000",
                                       "dataStatus": "normal"},
        positions=positions, user_id=USER, policies=RiskPolicyProvider(),
        instruments=InstrumentSpecProvider(), **facts)
    assert account_read["positionIntent"]["action"] == "reduce_long"   # 只提醒口径：账户 3 手 > 上限 2
    env.store.close()


def _as_reminder(store: RuntimeStore) -> None:
    """提醒页的提醒 (第 6 批): the same strategy with `plan.reminderOnly` (an old-format definition, so the plan is not run)."""
    with store.lock, store.connection:
        store.connection.execute("UPDATE strategy_drafts SET definition_json = ? WHERE id = ?",
                                 (json.dumps({**_definition(), "plan": {"reminderOnly": True}}), STRATEGY_ID))


@pytest.mark.asyncio
async def test_a_reminder_only_strategy_only_reminds_whatever_its_row_says(tmp_path: Path) -> None:
    env = _setup(tmp_path, signal_action="auto")
    _as_reminder(env.store)

    first = await env.monitor.run_once()

    assert first[0]["status"] == "alerted"
    assert _events(env.store, env.session, "proposal.trade") == [] and env.autonomy.calls == []
    env.store.close()


@pytest.mark.asyncio
async def test_starting_a_reminder_refuses_cards_and_orders_and_sets_alert(tmp_path: Path) -> None:
    app = application(tmp_path / "reminder-start.sqlite3", Gateway())
    store = app.state.store
    _save_strategy(store, status="ready")
    _as_reminder(store)
    store.save_strategy_validation_report(report_id="validation-reminder", strategy_id=STRATEGY_ID, user_id=USER,
                                          strategy_digest="a" * 64, status="passed", checks=[], evidence={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        refused = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/start", json={"signalAction": "confirm"})
        started = await client.post(f"/api/strategies/{STRATEGY_ID}/monitor/start", json={})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "strategy_reminder_only", refused.text
    assert started.status_code == 200 and started.json()["signalAction"] == "alert" and started.json()["status"] == "monitoring"
    store.close()
