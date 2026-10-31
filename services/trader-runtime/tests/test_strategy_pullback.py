from __future__ import annotations

import json

import pytest

from test_strategy_intents import account, build, evaluation, strategy
from test_strategy_workbench import bars, group
from trader_runtime.strategy_rules import StrategyPlan, evaluate_rules, resolve_chart_timeframe
from trader_runtime.strategy_workbench import definition_for


def pullback_plan():
    return StrategyPlan(name="回调测试", description="顺势回调", direction="long", timeframeSeconds=60,
        maxPosition=3, maxLoss=1000, takeProfitTicks=None, perTradeRisk=300, pullback={},
        rules={"entryLong": group("cross_up", 320), "exitLong": group("lt", 300)})


def sample():
    data = bars([100 + i for i in range(220)] + [319, 321, 322])
    data[220]["low"] = 306
    return data


def evaluate(p, data):
    return evaluate_rules(p, {60: {"bars": data}})


def test_first_touch_records_structure_and_consumes_cross_window_once():
    p, data = pullback_plan(), sample()
    p.rules.entryLong.conditions[0].withinBars = 3
    result = evaluate(p, data)
    assert result["rules"]["entryLong"]
    assert result["structures"]["entryLong"]["low"] == 306
    assert result["structures"]["entryLong"]["high"] == 322
    # The forming bar must not alter the fixed signal structure.
    data[-1].update(low=1, high=9999, close=1)
    assert evaluate(p, data) == result
    data[-1].update(low=321, high=323, close=322)
    data.append({**data[-1], "datetime": data[-1]["datetime"] + 60_000_000_000})
    assert not evaluate(p, data)["rules"]["entryLong"]


@pytest.mark.parametrize("invalid", ["guard", "timeout"])
def test_invalid_setups_do_not_trade(invalid):
    p, data = pullback_plan(), sample()
    if invalid == "guard":
        data[220]["close"] = 270
    elif invalid == "timeout":
        p.pullback.maxBars = 1
    assert not evaluate(p, data)["rules"]["entryLong"]


def test_cross_window_can_precede_touch_but_still_expires():
    p, data = pullback_plan(), sample()[:-1]
    p.rules.entryLong.conditions[0].right.value = 317
    p.rules.entryLong.conditions[0].withinBars = 3
    assert evaluate(p, data)["rules"]["entryLong"]
    p.rules.entryLong.conditions[0].withinBars = 2
    assert not evaluate(p, data)["rules"]["entryLong"]


def test_short_setup_mirrors_long_and_chart_binding_retains_structure():
    p = pullback_plan()
    p.direction = "short"
    p.rules.entryShort = p.rules.entryLong
    p.rules.entryLong = None
    c = p.rules.entryShort.conditions[0]
    c.operator, c.right.value = "cross_down", 680
    p.rules.exitLong = None
    data = sample()
    for b in data:
        b.update(open=1000-b["open"], close=1000-b["close"], high=1000-b["low"], low=1000-b["high"])
    assert evaluate(p, data)["structures"]["entryShort"]["high"] == 694
    p.timeframeSeconds = c.left.timeframe = c.right.timeframe = 0
    resolved = resolve_chart_timeframe(p, 300)
    assert resolved.pullback == p.pullback
    assert resolved.timeframeSeconds == resolved.rules.entryShort.conditions[0].left.timeframe == 300


@pytest.mark.parametrize("low,expected", [(3491, 3), (3486, 2), (3469, 0), (3502, 0)])
def test_structural_budget_sizes_or_skips_without_moving_stop(low, expected):
    s = strategy(3)
    s["definition"] = definition_for(pullback_plan())
    e = {**evaluation(), "rules": {"entryLong": True, "entryShort": False, "exitLong": False, "exitShort": False},
         "structures": {"entryLong": {"low": low, "high": 3505}}}
    result = build(strategy=s, evaluation=e)["riskAdjustedIntent"]
    assert result["volume"] == expected
    assert result["stopPrice"] == low - 1
    assert result["takeProfitPrice"] is None
    if expected:
        assert result["riskCheck"]["estimatedLoss"] <= 300
    e["rules"].update(entryLong=False, exitLong=True)
    a = account(); a["todayNetPnl"] = -1000
    result = build(strategy=s, evaluation=e, account=a,
                   positions=[{"symbol":s["symbol"], "direction":"long", "volume":2}])["riskAdjustedIntent"]
    assert result["status"] == "reduce_only" and result["volume"] == 2


@pytest.mark.parametrize("low,stale", [(821.8, False), (822.8, False), (822.8, True)])
async def test_structure_is_frozen_across_fill_and_restart_without_take_profit(tmp_path, monkeypatch, low, stale):
    from test_simulation_orders import USER, application
    from test_strategy_monitor import STRATEGY_ID
    from test_strategy_protection import entry_fill, setup
    app, provider, gateway, monitor = setup(tmp_path)
    provider.price_tick = 0.02
    # The 822.90 entry risks 120 at 822.78, but 1120 at 821.78.
    provider.bid, provider.ask, provider.last = 822.88, 822.9, 822.9
    s = app.state.store.get_strategy_draft(STRATEGY_ID, user_id=USER)
    # The fixture uses gold with a 1000 multiplier. Exercise the real dispatch
    # and fill protection path, with a deterministic structure snapshot.
    s["definition"]["riskLimits"].update(takeProfitTicks=None, perTradeRisk=300)
    s["definition"]["plan"] = pullback_plan().model_dump()
    app.state.store.connection.execute("UPDATE strategy_drafts SET definition_json=?", (json.dumps(s["definition"]),))
    app.state.store.connection.commit()
    monkeypatch.setattr("trader_runtime.strategy_rules.evaluate_plan", lambda *args: {
        "rules": {"entryLong": True}, "structures": {"entryLong": {"low": low, "high": 824}},
        "evaluatedBarTime": "different-bar" if stale else monitor.proposals.intent["signalSnapshot"]["barTime"]})
    result = await monitor.run_once()
    if low == 821.8 or stale:
        expected = "strategy_pullback_signal_expired" if stale else "strategy_trade_budget_exceeded"
        assert result[0]["status"] == "failed" and expected in result[0]["detail"]
        assert gateway.submit_count == 0
        app.state.store.close()
        return
    assert result[0]["status"] == "submitted", result
    request = dict(gateway.request)
    entry_fill(provider, request, price=822.9)
    provider.position_items = [{"symbol":request["symbol"], "direction":"long", "volume":1,
                               "todayVolume":1, "historicalVolume":0, "frozenToday":0, "frozenHistorical":0}]
    provider.activity["orders"] = [{**request, "status":"FINISHED", "volumeLeft":0}]
    gateway.query_report = gateway.report(request, status="FINISHED", volume_left=0)
    await app.state.simulation_orders.query(request["clientOrderId"], user_id=USER)
    app.state.autonomy.sync_strategy_protection()
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    assert stop["triggerPrice"] == 822.78
    assert stop["takeProfitPrice"] is None
    app.state.store.close()
    app = application(tmp_path / "protection.sqlite3", gateway, provider)
    app.state.autonomy.sync_strategy_protection()
    assert app.state.autonomy.list_stops(user_id=USER)[0]["triggerPrice"] == 822.78
    app.state.store.close()


async def test_deepseek_design_accepts_nullable_structural_plan_and_still_rejects_bad_risk():
    import asyncio
    from types import SimpleNamespace

    from pydantic import ValidationError

    from trader_runtime.strategy_designer import design_plan
    raw = pullback_plan().model_dump()
    async def complete_structured(**kwargs):
        def provider_accepts(value):
            if isinstance(value,dict):
                if any("$ref" in branch for branch in value.get("anyOf",[])):
                    raise RuntimeError("model_request_failed")  # DeepSeek: anyOf branch missing `type`
                for child in value.values():provider_accepts(child)
            elif isinstance(value,list):
                for child in value:provider_accepts(child)
        provider_accepts(kwargs["json_schema"])
        return SimpleNamespace(value=kwargs["schema"].model_validate(raw), tool_calls=[])
    adapter = SimpleNamespace(provider="deepseek", model="deepseek-flash", _design_lock=asyncio.Lock(),
        agent_authenticated=True, complete_structured=complete_structured)
    result = await design_plan(adapter,"已确认的回调",{})
    assert result["pullback"]["trendPeriod"] == 200
    assert result["takeProfitTicks"] is None and result["perTradeRisk"] == 300
    raw["perTradeRisk"] = -300
    with pytest.raises(ValidationError):
        await design_plan(adapter,"已确认的回调",{})
