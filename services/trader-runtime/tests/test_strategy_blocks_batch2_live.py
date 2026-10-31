"""策略积木·第三期 A (2026-09-25): the second batch of blocks on the live chain.

保本 tightening the armed stop tick by tick (only tighter, persisted across a restart, the tighter of it
and the trail standing, still triggering the protective close); 分段止盈 closing once per position under
each 执行方式 and after a restart; 复位 latched by the strategy's own dispatched opens, so a restart
neither forgets nor repeats it.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from test_simulation_orders import USER, Gateway, application
from test_strategy_blocks_live import NOW, SessionProvider, _open_intent, _plan, _save, _stop
from test_strategy_monitor import STRATEGY_ID, ProposalStub
from trader_runtime.data_providers import fake_market_workbench
from trader_runtime.strategy_engine import MarketContext, decide
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_protection import live_position_state
from trader_runtime.strategy_rules import StrategyPlan, evaluate_plan
from trader_runtime.strategy_workbench import definition_for

ALWAYS = [{"label": "总是", "left": {"indicator": "close", "timeframe": 300}, "operator": "gt",
           "right": {"indicator": "constant", "value": 0}}]
NEVER = [{"label": "永不", "left": {"indicator": "close", "timeframe": 300}, "operator": "lt",
          "right": {"indicator": "constant", "value": 0}}]


def _app(path, provider, gateway):
    app = application(path, gateway, provider)
    app.state.simulation_orders.trading_clock = lambda: NOW
    return app


async def _entry(path, plan, *, price: float, fill_time: int, levels: dict[str, float] | None = None,
                 provider: SessionProvider | None = None):
    """An auto open through the monitor, filled at `price`: the strategy's armed protection exists."""
    provider, gateway = provider or SessionProvider(), Gateway()
    provider.position_items = []
    app = _app(path, provider, gateway)
    _save(app.state.store, plan)
    monitor = StrategyMonitorService(store=app.state.store, proposals=ProposalStub(app.state.store, _open_intent(
        app.state.store, "1767225600000000000", levels)), autonomy=app.state.autonomy)
    assert (await monitor.run_once())[0]["status"] == "submitted"
    request = dict(gateway.request)
    provider.activity["trades"] = [{"tradeId": "entry-fill", "clientOrderId": request["clientOrderId"],
        "symbol": request["symbol"], "direction": "BUY", "offset": "OPEN", "volume": 1, "price": price,
        "tradeDateTime": fill_time, "tradingDay": provider.day}]
    provider.activity["orders"] = [{**request, "status": "FINISHED", "volumeLeft": 0}]
    provider.position_items = [{"symbol": request["symbol"], "direction": "long", "volume": 1, "todayVolume": 1,
                                "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0}]
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    return app, provider, gateway


# -- 当日最高 / 最低 while the day's earlier bars are still being read: 数据未就绪 --------------------------------

class LateSessionProvider(SessionProvider):
    """1-minute bars late in a 555-bar trading day (the live window starts inside the day); the history
    read answers only once `ready`."""

    def __init__(self) -> None:
        super().__init__()
        from test_strategy_blocks_batch2 import AU_SCHEDULE, au_minutes
        self.minutes, self.schedule = au_minutes(3), AU_SCHEDULE
        self.end = 2 * 555 + 400                                      # the 400th bar of the third day is forming
        self.ready, self.reads = False, 0

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        workbench = fake_market_workbench(symbol, duration_seconds)
        workbench["bars"] = self.minutes[self.end - 255:self.end + 1]
        workbench["quote"]["tradingSchedule"] = self.schedule
        return workbench

    def market_history_nowait(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        self.reads += 1
        if not self.ready:
            raise RuntimeError("market_series_not_ready")
        return {"bars": self.minutes[:self.end + 1][-length:]}


@pytest.mark.asyncio
async def test_time_take_profit_live_uses_restart_safe_history_and_waits_for_incomplete_history(tmp_path):
    bars = fake_market_workbench("SHFE.au2612", 300)["bars"]
    start = int(bars[0]["datetime"])
    # Session gaps make elapsed wall time differ from the actual number of completed bars.
    complete = [{**bars[0], "datetime": start + i * 300 * 1_000_000_000 +
                 (60 * 60 * 1_000_000_000 if i >= 300 else 0)} for i in range(502)]
    complete.append({**complete[-1], "datetime": int(complete[-1]["datetime"]) + 300 * 1_000_000_000})

    class HistoryProvider(SessionProvider):
        ready = False

        def market_workbench(self, symbol, duration_seconds):
            result = fake_market_workbench(symbol, duration_seconds)
            result["bars"] = complete[-256:]
            return result

        def market_history_nowait(self, symbol, duration_seconds, length):
            if not self.ready:
                raise RuntimeError("market_series_not_ready")
            return {"bars": complete[-length:]}

    provider = HistoryProvider()
    plan = _plan(timeTakeProfit=[{"afterBars": 500, "profitTicks": 1}])
    app, _, _ = await _entry(tmp_path / "history.sqlite3", plan, price=700, fill_time=start, provider=provider)
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    waiting = live_position_state(app.state.store, provider, strategy=strategy, symbol="SHFE.au2612",
                                  account=provider.account_summary(""))
    assert waiting.bars_pending and waiting.bars_held == 255
    evaluation = {"rules": {name: False for name in ("entryLong", "entryShort", "exitLong", "exitShort")},
                  "engine": {"close": 701}}
    market = MarketContext(price_tick=1, volume_multiple=10)
    assert decide(plan, evaluation, replace(waiting, direction="long", volume=1), market).action == "hold"
    provider.ready = True
    restored = live_position_state(app.state.store, provider, strategy=strategy, symbol="SHFE.au2612",
                                   account=provider.account_summary(""))
    assert not restored.bars_pending and restored.bars_held == 502
    decision = decide(plan, evaluation, replace(restored, direction="long", volume=1), market)
    assert decision.action == "close_long" and decision.exitReason == "time_take_profit"
    app.state.store.close()


@pytest.mark.parametrize("lag,max_hold,expected", [
    (1, None, "time_take_profit"), (4, None, "time_take_profit"),
    (400, None, None), (400, 200, "max_hold"),
])
async def test_live_monitor_stitches_stale_history_and_preserves_known_hold_limit(tmp_path, lag, max_hold, expected):
    bars = fake_market_workbench("SHFE.au2612", 300)["bars"]
    start = int(bars[0]["datetime"])
    complete = [{**bars[0], "datetime": start + i * 300 * 1_000_000_000 +
                 (60 * 60 * 1_000_000_000 if i >= 300 else 0)} for i in range(503)]
    complete.append({**complete[-1], "datetime": int(complete[-1]["datetime"]) + 300 * 1_000_000_000})

    class CachedProvider(SessionProvider):
        def market_workbench(self, symbol, duration_seconds):
            result = fake_market_workbench(symbol, duration_seconds)
            result["bars"] = complete[-256:]
            return result

        def market_history_nowait(self, symbol, duration_seconds, length):
            return {"bars": complete[:-lag]}

    provider = CachedProvider()
    plan = _plan(timeTakeProfit=[{"afterBars": 500, "profitTicks": 1}], maxHoldBars=max_hold)
    app, _, gateway = await _entry(tmp_path / f"stale-{lag}-{max_hold}.sqlite3", plan,
                                   price=700, fill_time=start, provider=provider)
    try:
        monitor = StrategyMonitorService(store=app.state.store, proposals=app.state.proposals,
                                         autonomy=app.state.autonomy)
        result = await monitor.run_once()
        snapshot = json.loads(app.state.store.connection.execute(
            "SELECT payload_json FROM strategy_intent_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        if expected is None:
            assert snapshot["signalSnapshot"]["dataPending"]
            assert snapshot["positionIntent"]["action"] == "hold"
            assert gateway.submit_count == 1
        else:
            assert result[0]["status"] == "submitted", result
            assert snapshot["signalSnapshot"]["exitReason"] == expected
            assert gateway.submit_count == 2
    finally:
        app.state.store.close()


def _session_plan(**fields: Any) -> StrategyPlan:
    below_high = [{"label": "低于当日最高", "left": {"indicator": "close", "timeframe": 60}, "operator": "lt",
                   "right": {"indicator": "session_high", "timeframe": 60}}]
    return StrategyPlan.model_validate({
        "name": "当日高点回落", "description": "低于当日最高就平多并开空", "direction": "both", "timeframeSeconds": 60,
        "maxPosition": 1, "stopLossTicks": 200, "takeProfitTicks": None, "cooldownBars": 0, "maxOrdersPerDay": 20,
        "rules": {"entryLong": {"conditions": NEVER_60}, "exitLong": {"conditions": below_high},
                  "entryShort": {"conditions": below_high}, "exitShort": {"conditions": NEVER_60}}, **fields})


NEVER_60 = [{"label": "永不", "left": {"indicator": "close", "timeframe": 60}, "operator": "lt",
             "right": {"indicator": "constant", "value": 0}}]


async def test_session_rules_wait_while_the_days_earlier_bars_are_not_read(tmp_path):
    provider = LateSessionProvider()
    app, provider, gateway = await _entry(tmp_path / "pending.sqlite3", _session_plan(), price=500.0, fill_time=1,
                                          provider=provider)
    store = app.state.store
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    for _ in range(2):                         # holding a long: neither the rule exit nor the reversal fires
        result = await monitor.run_once()
        assert result[0]["status"] == "waiting" and "还没读到" in result[0]["detail"], result
    snapshot = json.loads(store.connection.execute(
        "SELECT payload_json FROM strategy_intent_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert snapshot["signalSnapshot"]["dataPending"] and snapshot["positionIntent"]["action"] == "hold"
    assert [item["action"] for item in store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)] \
        == ["open_long"] and gateway.submit_count == 1
    provider.ready = True                       # the read arrived: the same bar is judged normally
    result = await monitor.run_once()
    assert result[0]["status"] == "submitted", result
    assert [item["action"] for item in store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)] \
        == ["close_long", "open_long"] and gateway.request["offset"] == "CLOSETODAY"
    store.close()


async def test_time_exits_still_run_while_session_data_is_pending(tmp_path):
    provider = LateSessionProvider()
    app, provider, gateway = await _entry(tmp_path / "pending-hold.sqlite3", _session_plan(maxHoldBars=5), price=500.0,
                                          fill_time=1, provider=provider)
    monitor = StrategyMonitorService(store=app.state.store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    result = await monitor.run_once()
    assert result[0]["status"] == "submitted" and gateway.submit_count == 2, result   # 最长持仓 closes anyway
    intent = json.loads(app.state.store.connection.execute(
        "SELECT payload_json FROM strategy_intent_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert intent["signalSnapshot"]["exitReason"] == "max_hold" and intent["signalSnapshot"]["dataPending"]
    app.state.store.close()


def test_strategies_without_session_extremes_never_read_the_history():
    provider = LateSessionProvider()
    plan = _session_plan()
    plain = StrategyPlan.model_validate({**plan.model_dump(), "rules": {
        "entryLong": {"conditions": NEVER_60}, "exitLong": {"conditions": NEVER_60}}, "direction": "long"})
    evaluation = evaluate_plan(provider, definition_for(plain), "SHFE.au2612")
    assert provider.reads == 0 and "dataPending" not in evaluation
    pending = evaluate_plan(provider, definition_for(plan), "SHFE.au2612")
    assert provider.reads == 1 and pending["dataPending"] and not any(pending["rules"].values())
    provider.ready = True
    judged = evaluate_plan(provider, definition_for(plan), "SHFE.au2612")
    assert "dataPending" not in judged and judged["rules"]["exitLong"] and judged["rules"]["entryShort"]


def test_session_history_never_waits_for_the_history_download():
    """当日最高 / 最低 in the live monitor: a cache miss starts one background read and answers at once."""
    import threading

    from trader_runtime.data_providers import SidecarReadOnlyDataProvider
    provider = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:1", token="x" * 32, symbols=("SHFE.au2612",))
    release, calls = threading.Event(), []

    def query(path, *, unavailable_code, timeout):
        calls.append(path)
        release.wait(5)
        return {"schemaVersion": 1, "bars": [{"datetime": i} for i in range(3_000)]}
    provider._query_sidecar = query
    for _ in range(3):
        with pytest.raises(RuntimeError, match="market_series_not_ready"):
            provider.market_history_nowait("SHFE.au2612", 60, 1_500)
    release.set()
    for _ in range(50):
        if provider._history_cache.get("SHFE.au2612", 60, 1_500):
            break
        threading.Event().wait(0.05)
    assert len(provider.market_history_nowait("SHFE.au2612", 60, 1_500)["bars"]) == 1_500 and len(calls) == 1


async def test_breakeven_tightens_the_armed_stop_survives_restart_and_stands_with_the_trail(tmp_path):
    # Fill 822, stop 821 (2 ticks of 0.5). 保本: 1 × the frozen ATR14 2.0 in profit → stop to 822 + 1 tick.
    # A 4.0 trail runs as well; whichever is tighter stands.
    plan = _plan(breakeven={"triggerAtr": 1, "offsetTicks": 1}, trailingStop={"mode": "ticks", "value": 8})
    path = tmp_path / "breakeven.sqlite3"
    app, provider, gateway = await _entry(path, plan, price=822.0, fill_time=1, levels={"exitAtr": 2.0})
    metadata = json.loads(_stop(app)["strategy_metadata_json"])
    assert metadata["breakeven"] == {"triggerTicks": None, "triggerAtr": 1.0, "offsetTicks": 1}
    assert metadata["exitAtr"] == "2.0" and float(_stop(app)["trigger_price"]) == 821.0
    for last, expected in ((823, 821.0), (824, 822.5), (823, 822.5)):   # below the trigger, at it, never loosened
        provider.last = last
        await app.state.autonomy._check_stop(_stop(app))
        assert float(_stop(app)["trigger_price"]) == expected, last
    metadata = json.loads(_stop(app)["strategy_metadata_json"])
    assert metadata["breakevenActive"] is True and "trailActive" not in metadata
    app.state.store.close()
    app = _app(path, provider, gateway)
    app.state.autonomy.sync_strategy_protection()
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    state = live_position_state(app.state.store, provider, strategy=strategy, symbol="SHFE.au2612",
                                account=provider.account_summary(""))
    assert (state.stop_price, state.breakeven_active, state.exit_atr) == (822.5, True, 2.0)
    provider.last = 828                                   # the trail's 824 is now tighter than 822.5
    await app.state.autonomy._check_stop(_stop(app))
    assert float(_stop(app)["trigger_price"]) == 824.0
    assert json.loads(_stop(app)["strategy_metadata_json"])["trailActive"] is True
    provider.last = 823.5
    await app.state.autonomy._check_stop(_stop(app))
    assert gateway.submit_count == 2 and gateway.request["offset"] == "CLOSETODAY"
    app.state.store.close()


async def test_breakeven_in_ticks_without_an_atr(tmp_path):
    app, provider, gateway = await _entry(tmp_path / "ticks.sqlite3", _plan(breakeven={"triggerTicks": 6}),
                                          price=822.0, fill_time=1)
    provider.last = 824.5                                  # 5 ticks: not yet
    await app.state.autonomy._check_stop(_stop(app))
    assert float(_stop(app)["trigger_price"]) == 821.0
    provider.last = 825
    await app.state.autonomy._check_stop(_stop(app))
    assert float(_stop(app)["trigger_price"]) == 822.0 and gateway.submit_count == 1
    provider.last = 822
    await app.state.autonomy._check_stop(_stop(app))
    assert gateway.submit_count == 2
    app.state.store.close()


@pytest.mark.parametrize("signal_action", ["alert", "confirm", "auto"])
async def test_time_take_profit_closes_once_per_position_under_each_execution_mode(tmp_path, signal_action):
    bars = fake_market_workbench("SHFE.au2612", 300)["bars"]
    plan = _plan(timeTakeProfit=[{"afterBars": 50, "profitTicks": 0}, {"afterBars": 3, "profitTicks": 10}])
    path = tmp_path / f"tiers-{signal_action}.sqlite3"
    # Filled far below the market at bar 30: 8 bars held by the last completed bar 38, well in profit.
    app, provider, gateway = await _entry(path, plan, price=700.0, fill_time=int(bars[30]["datetime"]))
    store = app.state.store
    session = app.state.broker.create_session("local", "native", USER)
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action=signal_action, session_id=session.session_id)
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    first = await monitor.run_once()
    assert first[0]["status"] == {"alert": "alerted", "confirm": "proposed", "auto": "submitted"}[signal_action], first
    attempts = store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
    assert [item["action"] for item in attempts] == ["close_long", "open_long"]
    intent = store.connection.execute("SELECT payload_json FROM strategy_intent_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
    snapshot = json.loads(intent[0])["signalSnapshot"]
    assert snapshot["exitReason"] == "time_take_profit" and snapshot["exitKey"].startswith("time_take_profit:")
    if signal_action == "auto":
        assert gateway.submit_count == 2 and gateway.request["offset"] == "CLOSETODAY"
        gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=1)   # not filled
    provider.bar_count += 1
    again = await monitor.run_once()
    assert again[0]["status"] == "waiting", again
    store.close()
    app = _app(path, provider, gateway)
    monitor = StrategyMonitorService(store=app.state.store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    provider.bar_count += 1
    assert (await monitor.run_once())[0]["status"] == "waiting"
    closes = [item for item in app.state.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)
              if item["action"] == "close_long"]
    assert len(closes) == 1 and gateway.submit_count == (2 if signal_action == "auto" else 1)
    app.state.store.close()


async def test_switching_the_signal_action_mid_round_refuses_the_auto_send(tmp_path):
    """#528：监控轮开始后（策略行已按 auto 读走）用户把执行方式改成 confirm——发送前再读
    一次口径：这单不发，尝试 FAILED 写明 strategy_signal_not_auto，也没有新委托出去。"""
    app, _provider, gateway = await _entry(
        tmp_path / "signal-switch.sqlite3", _plan(), price=700, fill_time=1)
    store = app.state.store

    class SwitchingProposals(ProposalStub):
        """The user's change lands mid-round: after the strategy row was read as auto,
        before the send (the preview is the round's own seam)."""

        switched = False

        def preview_strategy_intent(self, **_: Any) -> dict[str, Any]:
            if not SwitchingProposals.switched:
                SwitchingProposals.switched = True
                store.set_strategy_signal_action(STRATEGY_ID, user_id=USER,
                                                 signal_action="confirm", session_id=None)
            return self.intent

    close = _open_intent(store, "1767225600000000001")
    close["positionIntent"] = {**close["positionIntent"], "action": "close_long"}
    close["riskAdjustedIntent"] = {**close["riskAdjustedIntent"], "action": "close_long"}
    monitor = StrategyMonitorService(store=store, proposals=SwitchingProposals(store, close),
                                     autonomy=app.state.autonomy)
    result = await monitor.run_once()
    assert result[0]["status"] == "failed" and result[0]["detail"] == "strategy_signal_not_auto", result
    assert gateway.submit_count == 1  # 只有进场那一单；这单没有发出
    attempt = store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)[0]
    assert attempt["status"] == "FAILED" and attempt["detail"] == "strategy_signal_not_auto"
    store.close()


async def test_rearm_latch_comes_from_dispatched_opens_and_survives_restart(tmp_path):
    provider, gateway = SessionProvider(), Gateway()
    provider.position_items = []
    path = tmp_path / "rearm.sqlite3"
    app = _app(path, provider, gateway)
    store = app.state.store
    waiting_plan = _plan(rules={"entryLong": {"conditions": ALWAYS, "rearm": NEVER}, "exitLong": {"conditions": NEVER}})
    _save(store, waiting_plan)
    _open_intent(store, "1")
    bars = fake_market_workbench("SHFE.au2612", 300)["bars"][:provider.bar_count]
    latch = int(bars[20]["datetime"])
    for index, (bar, status) in enumerate([(bars[10], "SUBMITTED"), (bars[20], "PARTIAL"), (bars[30], "FAILED"),
                                          (bars[32], "SKIPPED")]):
        store.reserve_strategy_execution_attempt(attempt_id=f"a{index}", strategy_id=STRATEGY_ID, user_id=USER,
            intent_id="intent-blocks", signal_bar_time=str(bar["datetime"]), trading_day="2026-09-02",
            action="open_long", volume=1)
        store.finish_strategy_execution_attempt(attempt_id=f"a{index}", status=status)
    assert store.strategy_last_open_bars(strategy_id=STRATEGY_ID, user_id=USER) == {"long": latch, "short": None}
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    result = await monitor.run_once()
    assert result[0]["status"] == "waiting" and "复位条件「收盘价 < 0」还没成立" in result[0]["detail"], result
    assert gateway.submit_count == 0
    store.close()
    app = _app(path, provider, gateway)                      # a restart reads the same latch
    strategy = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    state = live_position_state(app.state.store, provider, strategy=strategy, symbol="SHFE.au2612",
                                account=provider.account_summary(""))
    assert state.last_open_long == latch
    monitor = StrategyMonitorService(store=app.state.store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    assert "复位" in (await monitor.run_once())[0]["detail"] and gateway.submit_count == 0
    app.state.store.close()


async def test_rearm_that_held_after_the_latch_lets_the_direction_open_again(tmp_path):
    provider, gateway = SessionProvider(), Gateway()
    provider.position_items = []
    app = _app(tmp_path / "rearmed.sqlite3", provider, gateway)
    store = app.state.store
    _save(store, _plan(rules={"entryLong": {"conditions": ALWAYS, "rearm": ALWAYS}, "exitLong": {"conditions": NEVER}}))
    _open_intent(store, "1")
    bars = fake_market_workbench("SHFE.au2612", 300)["bars"]
    store.reserve_strategy_execution_attempt(attempt_id="a0", strategy_id=STRATEGY_ID, user_id=USER,
        intent_id="intent-blocks", signal_bar_time=str(bars[20]["datetime"]), trading_day="2026-09-02",
        action="open_long", volume=1)
    store.finish_strategy_execution_attempt(attempt_id="a0", status="SUBMITTED")
    monitor = StrategyMonitorService(store=store, proposals=app.state.proposals, autonomy=app.state.autonomy)
    result = await monitor.run_once()
    assert result[0]["status"] == "submitted" and gateway.submit_count == 1, result
    store.close()
