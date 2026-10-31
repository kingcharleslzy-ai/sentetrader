"""策略积木·第一期 A (2026-09-25): the building blocks on the live chain.

ATR stop re-anchored on the fill, the trailing stop tightening the armed protection (persisted,
never loosened, and still triggering the protective close), 收盘前平仓 once per position and
trading day under each 执行方式 (also across a restart), 最长持仓 from the live position state,
ATR sizing inside the intent, per-direction daily counts and daily anchors in a replay.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
from test_simulation_orders import USER, Gateway, NormalProvider, application
from test_strategy_intents import account as intent_account
from test_strategy_intents import build
from test_strategy_monitor import STRATEGY_ID, ProposalStub
from trader_runtime.data_providers import fake_market_workbench
from trader_runtime.replay import ReplayProvider
from trader_runtime.strategy_engine import MarketContext, decide
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_protection import live_position_state
from trader_runtime.strategy_rules import StrategyPlan, evaluate_plan
from trader_runtime.strategy_workbench import definition_for

CST = timezone(timedelta(hours=8))
SHFE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
        "night": [["21:00:00", "26:30:00"]]}
NEVER = [{"label": "永不", "left": {"indicator": "close", "timeframe": 300}, "operator": "lt",
          "right": {"indicator": "constant", "value": 0}}]
ALWAYS = [{"label": "总是", "left": {"indicator": "close", "timeframe": 300}, "operator": "gt",
           "right": {"indicator": "constant", "value": 0}}]
DIGEST = "c" * 64


def _plan(**fields: Any) -> StrategyPlan:
    return StrategyPlan.model_validate({
        "name": "积木实盘", "description": "积木实盘测试", "direction": "long", "timeframeSeconds": 300,
        "maxPosition": 1, "stopLossTicks": 2, "takeProfitTicks": None, "cooldownBars": 0,
        "rules": {"entryLong": {"conditions": ALWAYS}, "exitLong": {"conditions": NEVER}}, **fields})


def _save(store, plan: StrategyPlan, *, status: str = "monitoring") -> dict[str, Any]:
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    saved = store.save_strategy_draft(strategy_id=STRATEGY_ID, user_id=USER, source_session_id="session-blocks",
                                      name="积木实盘", symbol="SHFE.au2612", version=1, status=status,
                                      definition=definition_for(plan), definition_digest=DIGEST)
    store.save_strategy_validation_report(report_id="report-blocks", strategy_id=STRATEGY_ID, user_id=USER,
                                          strategy_digest=DIGEST, status="passed", checks=[], evidence={})
    return saved


def _open_intent(store, bar_time: str, levels: dict[str, float] | None = None) -> dict[str, Any]:
    payload = {"intentId": "intent-blocks", "strategyId": STRATEGY_ID,
               "signalSnapshot": {"signal": "buy", "barTime": bar_time,
                                  **({"protectionLevels": levels} if levels else {})},
               "positionIntent": {"action": "open_long", "requestedVolume": 1, "reason": "条件已满足"},
               "riskAdjustedIntent": {"status": "allowed", "action": "open_long", "volume": 1, "reason": "条件已满足"}}
    store.save_strategy_intent_snapshot(intent_id="intent-blocks", strategy_id=STRATEGY_ID, user_id=USER,
                                        strategy_digest=DIGEST, signal_bar_time=bar_time, context_digest="d" * 64,
                                        payload=payload)
    return payload


class Provider(NormalProvider):
    def __init__(self) -> None:
        super().__init__()
        self.day = "2026-08-25"
        self.activity: dict[str, Any] = {"dataStatus": "normal", "isCached": False, "ageSeconds": 0,
                                         "ordersInitialized": True, "orders": [], "trades": []}
        self.position_items = []

    def account_activity(self) -> dict[str, Any]:
        return self.activity

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return {**super().account_summary(turn_id), "tradingDay": self.day}


async def _filled_entry(tmp_path, plan: StrategyPlan, *, fill_time: int = 1, levels: dict[str, float] | None = None):
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "blocks.sqlite3", gateway, provider)
    _save(app.state.store, plan)
    monitor = StrategyMonitorService(store=app.state.store, proposals=ProposalStub(app.state.store, _open_intent(
        app.state.store, "1767225600000000000", levels)), autonomy=app.state.autonomy)
    result = await monitor.run_once()
    assert result[0]["status"] == "submitted", result
    await _fill(app, provider, gateway, fill_time)
    return app, provider, gateway


async def _fill(app, provider, gateway, fill_time: int) -> None:
    request = dict(gateway.request)
    provider.activity["trades"] = [{"tradeId": "entry-fill", "clientOrderId": request["clientOrderId"],
        "symbol": request["symbol"], "direction": "BUY", "offset": "OPEN", "volume": 1, "price": 822.0,
        "tradeDateTime": fill_time, "tradingDay": provider.day}]
    provider.activity["orders"] = [{**request, "status": "FINISHED", "volumeLeft": 0}]
    provider.position_items = [{"symbol": request["symbol"], "direction": "long", "volume": 1, "todayVolume": 1,
                                "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()


def _stop(app) -> Any:
    return app.state.store.connection.execute("SELECT * FROM ai_stop_orders").fetchone()


async def test_atr_stop_becomes_a_fixed_stop_from_the_fill_price(tmp_path):
    # The signal bar's ATR × 2 = 4.0 travels with the order; the stop is set from the 822 fill.
    app, _, _ = await _filled_entry(tmp_path, _plan(atrStop={"multiple": 2}), levels={"stopDistance": 4.0})
    stops = app.state.autonomy.list_stops(user_id=USER)
    assert len(stops) == 1 and stops[0]["triggerPrice"] == 818.0
    app.state.store.close()


async def test_atr_stop_card_confirmed_after_the_next_bar_still_orders_with_the_frozen_distance(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from test_simulation_orders import quote_now
    from test_strategy_signal_action import ORIGIN, CardProposals, _login
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "card.sqlite3", gateway, provider)
    store = app.state.store
    _save(store, _plan(atrStop={"multiple": 2}))
    session = app.state.broker.create_session("local", "native", USER)
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action="confirm", session_id=session.session_id)
    # The card's signal bar is long gone and carries a frozen distance of 3.0 (the current fake
    # series would give 2 × ATR 2 = 4): confirming later still orders, with the frozen distance.
    intent = _open_intent(store, "1767225600000000000", {"stopDistance": 3.0})
    monitor = StrategyMonitorService(store=store, proposals=CardProposals(app, intent), autonomy=app.state.autonomy)
    assert (await monitor.run_once())[0]["status"] == "proposed"
    card = next(event["payload"] for _, event in store.list_events(session.session_id)
                if event.get("name") == "proposal.trade")
    assert card["strategySource"]["protectionLevels"] == {"stopDistance": 3.0}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": ORIGIN}) as client:
        await _login(client)
        provider.quote_datetime = quote_now()
        confirmed = await client.post(f"/api/proposals/{card['proposalId']}/confirm", json={
            "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": "atr-card-1"})
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 1
    await _fill(app, provider, gateway, 1)
    assert app.state.autonomy.list_stops(user_id=USER)[0]["triggerPrice"] == 819.0   # 822 − 3.0
    store.close()


async def test_go_live_is_refused_without_a_session_schedule_for_close_before_end(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from trader_runtime.main import create_app
    source = HistoricalSource()
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    app = create_app(tmp_path / "golive.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", data_provider=provider,
                     simulation_gateway=provider, _replay_provider=provider, local_token="golive")
    plan = StrategyPlan.model_validate({"name": "收盘前平", "description": "收盘前平仓", "direction": "long",
        "timeframeSeconds": 60, "closeBeforeEndMinutes": 5, "rules": {"entryLong": {"conditions": [
            {**ALWAYS[0], "left": {"indicator": "close", "timeframe": 60}}]}}})
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer golive"}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
        library = (await client.post("/api/strategy-plans", json={"plan": plan.model_dump()})).json()["strategyId"]
        refused = await client.post(f"/api/strategy-plans/{library}/validate", json={"symbol": SYMBOL})
        assert refused.status_code == 422 and "拿不到这个合约的交易时段，无法保证收盘前平仓" in refused.text
        for chart in source.data["workbenches"].values():
            chart["quote"]["tradingSchedule"] = SHFE
        await provider.command("state")
        validated = await client.post(f"/api/strategy-plans/{library}/validate", json={"symbol": SYMBOL})
        assert validated.status_code == 200, validated.text
        instance = validated.json()["strategyId"]
        for chart in source.data["workbenches"].values():
            chart["quote"].pop("tradingSchedule")
        await provider.command("state")
        started = await client.post(f"/api/strategies/{instance}/monitor/start", json={})
        assert started.status_code == 409 and "无法保证收盘前平仓" in started.text
        assert app.state.store.get_strategy_draft(instance, user_id=USER)["status"] == "ready"
    app.state.store.close()


async def test_missing_schedule_with_a_position_warns_once_per_trading_day(tmp_path):
    from test_strategy_signal_action import Voice
    provider, gateway = Provider(), Gateway()
    provider.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 1, "todayVolume": 1,
                                "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    app = application(tmp_path / "warn.sqlite3", gateway, provider)
    store = app.state.store
    _save(store, _plan(closeBeforeEndMinutes=5))
    session = app.state.broker.create_session("local", "native", USER)
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action="auto", session_id=session.session_id)
    hold = {"intentId": "intent-hold", "strategyId": STRATEGY_ID, "engine": {"schedule": None},
            "signalSnapshot": {"signal": "wait", "barTime": "1"},
            "positionIntent": {"action": "hold", "requestedVolume": 0, "reason": "继续等待"},
            "riskAdjustedIntent": {"status": "no_action", "action": "hold", "volume": 0, "reason": "继续等待"}}
    monitor = StrategyMonitorService(store=store, proposals=ProposalStub(store, hold), autonomy=app.state.autonomy)
    voice = Voice()
    monitor.voice_lookup = lambda: voice
    for _ in range(3):
        await monitor.run_once()
    notices = [event["payload"]["text"] for _, event in store.list_events(session.session_id)
               if event.get("name") == "message.completed"]
    assert len(notices) == 1 and "今天可能无法按时收盘前平仓，请手动处理" in notices[0]
    assert [kind for kind, _ in voice.said] == ["strategy-warning"]
    provider.day = "2026-08-26"   # the next trading day warns again
    await monitor.run_once()
    assert len(voice.said) == 2
    provider.position_items = []  # flat: nothing to warn about
    provider.day = "2026-08-27"
    await monitor.run_once()
    assert len(voice.said) == 2
    store.close()


async def test_trailing_stop_only_tightens_survives_restart_and_triggers(tmp_path):
    app, provider, gateway = await _filled_entry(tmp_path, _plan(trailingStop={"mode": "ticks", "value": 4}))
    assert float(_stop(app)["trigger_price"]) == 821.0          # stopLossTicks 2 × 0.5 below the fill
    provider.last = 830
    await app.state.autonomy._check_stop(_stop(app))
    assert float(_stop(app)["trigger_price"]) == 828.0          # 830 − 4 ticks
    provider.last = 829
    await app.state.autonomy._check_stop(_stop(app))
    assert float(_stop(app)["trigger_price"]) == 828.0          # never loosens
    assert gateway.submit_count == 1
    app.state.store.close()
    app = application(tmp_path / "blocks.sqlite3", gateway, provider)
    app.state.autonomy.sync_strategy_protection()
    row = _stop(app)
    assert float(row["trigger_price"]) == 828.0 and '"trailExtreme": "830' in row["strategy_metadata_json"]
    provider.last = 827.5
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == 2 and gateway.request["offset"] == "CLOSETODAY"
    assert "止损触发：828" in app.state.store.connection.execute(
        "SELECT payload_json FROM proposals ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    app.state.store.close()


async def test_live_state_counts_bars_held_for_max_hold(tmp_path):
    bars = fake_market_workbench("SHFE.au2612", 300)["bars"]
    app, provider, _ = await _filled_entry(tmp_path, _plan(maxHoldBars=5), fill_time=int(bars[70]["datetime"]))
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    state = live_position_state(app.state.store, provider, strategy=strategy, symbol="SHFE.au2612",
                                account=provider.account_summary(""))
    assert (state.entry_price, state.bars_held, state.stop_price) == (822.0, 9, 821.0)  # bars 70…78 completed
    evaluation = evaluate_plan(provider, strategy["definition"], "SHFE.au2612")
    held = decide(StrategyPlan.model_validate(strategy["definition"]["plan"]), evaluation,
                  state.__class__(**{**state.__dict__, "direction": "long", "volume": 1}),
                  MarketContext(price_tick=0.5, volume_multiple=1000, equity=None))
    assert (held.action, held.exitReason) == ("close_long", "max_hold") and held.exitKey.startswith("max_hold:")
    app.state.store.close()


# -- 收盘前平仓 under each 执行方式 -----------------------------------------------------------------------------

NOW = datetime(2026, 9, 2, 14, 56, tzinfo=CST)   # a Wednesday, four minutes before the close


class SessionProvider(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.quote_datetime = NOW.strftime("%Y-%m-%d %H:%M:%S.%f")
        self.bar_count = 40
        self.position_items = [{"symbol": "SHFE.au2612", "direction": "long", "volume": 1, "todayVolume": 1,
                                "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        workbench = fake_market_workbench(symbol, duration_seconds)
        workbench["bars"] = workbench["bars"][:self.bar_count]
        workbench["quote"]["tradingSchedule"] = SHFE
        return workbench


def _own_lot(store) -> None:
    """The strategy's own held lot: an armed strategy protection row — the 2026-10-08 口径 under
    which 自动下单 / 出卡确认 judge (the account's long beside it stays the user's)."""
    now = datetime.now(UTC).isoformat()
    store.connection.execute("PRAGMA foreign_keys = OFF")   # the authorization / session rows are not the point here
    store.connection.execute(
        "INSERT INTO ai_stop_orders (id,user_id,authorization_id,session_id,symbol,position_direction,position_bucket,"
        "volume,trigger_price,price_intent,status,strategy_metadata_json,remaining_volume,created_at,updated_at)"
        " VALUES ('own-lot',?,'auth','s','SHFE.au2612','long','today',1,'821','aggressive','ARMED',?,1,?,?)",
        (USER, json.dumps({"strategyId": STRATEGY_ID, "entryPrice": "822", "entryVolume": 1,
                           "entryTradingDay": "2026-08-25"}), now, now))
    store.connection.commit()


def _session_app(path, provider, gateway, signal_action: str):
    app = application(path, gateway, provider)
    app.state.simulation_orders.trading_clock = lambda: NOW
    store = app.state.store
    if store.connection.execute("SELECT 1 FROM strategy_drafts").fetchone() is None:
        _save(store, _plan(closeBeforeEndMinutes=5))
        session = app.state.broker.create_session("local", "native", USER)
        store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action=signal_action,
                                         session_id=session.session_id)
        _own_lot(store)
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    return app, monitor


def _count(store, name: str) -> int:
    return sum(1 for session in store.connection.execute("SELECT id FROM sessions").fetchall()
               for _, event in store.list_events(session[0]) if event.get("name") == name)


@pytest.mark.parametrize("signal_action", ["alert", "confirm", "auto"])
async def test_close_before_end_fires_once_per_execution_mode_and_after_restart(tmp_path, signal_action):
    provider, gateway = SessionProvider(), Gateway()
    path = tmp_path / f"close-{signal_action}.sqlite3"
    app, monitor = _session_app(path, provider, gateway, signal_action)
    first = await monitor.run_once()
    assert first[0]["status"] == {"alert": "alerted", "confirm": "proposed", "auto": "submitted"}[signal_action], first
    attempts = app.state.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
    assert [item["action"] for item in attempts] == ["close_long"]
    if signal_action == "auto":
        assert gateway.submit_count == 1 and gateway.request["offset"] == "CLOSETODAY"
        # The close was cancelled and the position is still there: the time exit is not repeated.
        gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=1)
    provider.bar_count += 1  # a new completed bar, still before the bell
    again = await monitor.run_once()
    assert again[0]["status"] == "waiting", again
    app.state.store.close()
    app, monitor = _session_app(path, provider, gateway, signal_action)
    provider.bar_count += 1
    restarted = await monitor.run_once()
    assert restarted[0]["status"] == "waiting", restarted
    assert len(app.state.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)) == 1
    assert gateway.submit_count == (1 if signal_action == "auto" else 0)
    if signal_action == "confirm":
        assert _count(app.state.store, "proposal.trade") == 1
    app.state.store.close()


# -- sizing, counts, replay --------------------------------------------------------------------------------------

def test_atr_sizing_sets_the_intent_volume_and_refuses_on_a_stale_account():
    plan = _plan(maxPosition=3, sizing={"mode": "atr_risk", "riskPercent": 1, "atrMultiple": 2})
    strategy = {"strategyId": "s", "symbol": "SHFE.rb2610", "definitionDigest": "a" * 64,
                "definition": definition_for(plan)}
    evaluation = {"signal": "wait", "evaluatedBarTime": "1", "dataStatus": "normal",
                  "rules": {"entryLong": True, "exitLong": False, "entryShort": False, "exitShort": False},
                  "engine": {"barClose": 1, "tradingDay": "2026-09-23", "close": 3500.0, "atr": {"14": 50.0}}}
    sized = build(strategy=strategy, evaluation=evaluation, account={**intent_account(), "equity": 150_000})
    assert sized["positionIntent"]["requestedVolume"] == 1            # 1500 / (10 × 2 × 50) = 1.5 → 1
    assert "ATR" in sized["positionIntent"]["reason"]
    stale = build(strategy=strategy, evaluation=evaluation, account={**intent_account(age=60), "equity": 150_000})
    assert stale["positionIntent"]["action"] == "hold" and "不新鲜" in stale["positionIntent"]["reason"]
    small = build(strategy=strategy, evaluation=evaluation, account={**intent_account(), "equity": 50_000})
    assert small["positionIntent"]["action"] == "hold" and "不足 1 手" in small["positionIntent"]["reason"]


def test_opens_are_counted_per_direction_and_trading_day(tmp_path):
    provider, gateway = Provider(), Gateway()
    app = application(tmp_path / "counts.sqlite3", gateway, provider)
    store = app.state.store
    _save(store, _plan())
    _open_intent(store, "1")
    for index, (action, day, status) in enumerate([("open_long", "2026-09-23", "SUBMITTED"),
                                                   ("open_long", "2026-09-23", "SKIPPED"),
                                                   ("open_short", "2026-09-23", "PARTIAL"),
                                                   ("open_long", "2026-09-22", "SUBMITTED")]):
        store.reserve_strategy_execution_attempt(attempt_id=f"a{index}", strategy_id=STRATEGY_ID, user_id=USER,
            intent_id="intent-blocks", signal_bar_time=str(index), trading_day=day, action=action, volume=1)
        store.finish_strategy_execution_attempt(attempt_id=f"a{index}", status=status)
    assert store.count_strategy_opens_by_direction(strategy_id=STRATEGY_ID, user_id=USER,
                                                   trading_date="2026-09-23") == {"long": 1, "short": 1}
    store.close()


async def test_replay_provider_serves_daily_bars_for_anchors():
    source = HistoricalSource()
    # The replay subscribes the 86400 series too; give it the days before the replayed one.
    daily = source.data["workbenches"]["86400"]["bars"]
    for index, bar in enumerate(daily):
        bar["datetime"] -= (len(daily) - index) * 86400 * 1_000_000_000 * 2
        bar["tradingDay"] = datetime.fromtimestamp(bar["datetime"] / 1e9, CST).date().isoformat()
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    plan = StrategyPlan.model_validate({
        "name": "回放锚点", "description": "收盘高于昨收", "direction": "long", "timeframeSeconds": 60,
        "rules": {"entryLong": {"conditions": [{"label": "高于昨收", "left": {"indicator": "close", "timeframe": 60},
                                                "operator": "gt", "right": {"indicator": "prev_close", "timeframe": 60}}]}}})
    evaluation = evaluate_plan(provider, {"plan": plan.model_dump()}, SYMBOL)
    daily = provider.market_workbench(SYMBOL, 86400)["bars"]
    days = {bar["tradingDay"]: bar for bar in daily}
    minute = provider.market_workbench(SYMBOL, 60)["bars"][-2]
    previous = days[max(day for day in days if day < minute["tradingDay"])]
    detail = evaluation["conditions"][0]["detail"]
    assert detail.endswith(f"{previous['close']:.4f}")
    assert evaluation["rules"]["entryLong"] is (minute["close"] > previous["close"])


def test_rules_are_judged_again_only_when_their_closed_bars_change(monkeypatch):
    """规则只在新 K 线收盘时重算 (所有者 2026-10-22): monitoring judges every second, but the rules read closed bars
    only — between two closes the evaluation is reused. The forming bar moving, or the forming daily bar's
    high and close, change nothing (only its open is read, by 今开); a new closed bar, a new day's open or another
    plan is judged afresh, and the reused result equals a fresh one."""
    import copy

    from trader_runtime import strategy_rules
    plan = _plan(rules={"entryLong": {"conditions": [{"label": "站上今开", "left": {"indicator": "close", "timeframe": 300},
                                                      "operator": "gt", "right": {"indicator": "today_open", "timeframe": 300}}]},
                        "exitLong": {"conditions": NEVER}})
    benches = {300: copy.deepcopy(fake_market_workbench("SHFE.au2612", 300)),
               86400: copy.deepcopy(fake_market_workbench("SHFE.au2612", 86400))}

    class Bench:
        def market_workbench(self, symbol, duration):
            return benches[duration]

    judged: list[int] = []
    original = strategy_rules.evaluate_rules
    monkeypatch.setattr(strategy_rules, "evaluate_rules", lambda *args, **kwargs: (judged.append(1), original(*args, **kwargs))[1])
    strategy_rules._EVALUATIONS.clear()
    definition = {"plan": plan.model_dump(mode="json")}

    def evaluate():
        return evaluate_plan(Bench(), definition, "SHFE.au2612", require_fresh=False)

    def fresh():
        strategy_rules._EVALUATIONS.clear()
        return evaluate()

    first = evaluate()
    assert evaluate() == first and len(judged) == 1
    benches[300]["bars"][-1] = {**benches[300]["bars"][-1], "close": 1.0, "high": 99999.0}  # the forming bar moves
    benches[86400]["bars"][-1] = {**benches[86400]["bars"][-1], "high": 99999.0, "close": 1.0}
    assert evaluate() == first and len(judged) == 1
    benches[86400]["bars"][-1] = {**benches[86400]["bars"][-1], "open": 1.0}  # 今开 is read: judged again
    moved = evaluate()
    assert len(judged) == 2 and moved == fresh()
    closed = dict(benches[300]["bars"][-1])
    benches[300]["bars"].append({**closed, "datetime": int(closed["datetime"]) + 300 * 10**9})  # a bar closed
    judged.clear()
    evaluate()
    assert len(judged) == 1
    evaluate()
    assert len(judged) == 1
    definition = {"plan": {**plan.model_dump(mode="json"), "cooldownBars": 2}}  # another plan
    evaluate()
    assert len(judged) == 2
