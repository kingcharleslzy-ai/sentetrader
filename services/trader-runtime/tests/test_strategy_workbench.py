from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
from trader_runtime.main import create_app
from trader_runtime.replay import ReplayProvider
from trader_runtime.strategy_rules import (
    Condition,
    Operand,
    StrategyPlan,
    _condition,
    evaluate_rules,
    indicator_values,
    rule_position_intent,
)
from trader_runtime.trading_costs import FeeSchedule, FeeSettings, review_trades, trade_fee


def group(op="gt", value=823.5):
    return {"join": "all", "sequence": False, "conditions": [{"label": "价格条件", "left": {"indicator": "close", "timeframe": 60}, "operator": op, "right": {"indicator": "constant", "value": value}, "withinBars": 1}]}


def plan():
    return StrategyPlan(name="可保存的双向策略", description="价格高于阈值开多、平空；低于阈值开空、平多。只在已收盘分钟线上判断。", timeframeSeconds=60,
                        direction="both", maxPosition=1, maxLoss=50000, stopLossTicks=50, takeProfitTicks=100, cooldownBars=0,
                        rules={"entryLong": group(), "exitLong": group("lt"), "entryShort": group("lt"), "exitShort": group()})


def bars(values, timeframe=60):
    anchor = int(datetime.fromisoformat("2026-09-03T10:00:00+00:00").timestamp() * 1e9)
    return [{"datetime": anchor + i * timeframe * 1_000_000_000, "open": v, "high": v + 1, "low": v - 1, "close": v, "volume": 100} for i, v in enumerate(values)]


def test_fee_rates_fifo_partial_fills_and_no_double_charge():
    schedule = FeeSchedule(source="custom", default={"mode": "per_lot", "open": 2, "closeToday": 4, "closeYesterday": 3}, overrides=[{"symbol": "SHFE.au", "mode": "notional", "open": 0.5, "closeToday": 1, "closeYesterday": 0.5}]).model_dump()
    assert trade_fee(schedule, symbol=SYMBOL, price=800, volume=2, multiplier=1000, offset="OPEN") == 80
    rb = "SHFE.rb2701"
    fills = [{"tradeId": "a", "symbol": rb, "direction": "SELL", "offset": "OPEN", "price": 3200, "volume": 2, "tradeDateTime": 1, "tradingDay": "2026-09-03"},
             {"tradeId": "b", "symbol": rb, "direction": "BUY", "offset": "CLOSE", "price": 3190, "volume": 1, "tradeDateTime": 2, "tradingDay": "2026-09-04"},
             {"tradeId": "c", "symbol": rb, "direction": "BUY", "offset": "CLOSE", "price": 3210, "volume": 1, "tradeDateTime": 3, "tradingDay": "2026-09-04"}]
    result = review_trades(fills + fills, schedule, 10)
    assert result["estimatedCommission"] == 10
    assert [t["net"] for t in result["trades"]] == [95, -105]
    assert result["estimatedNet"] == -10
    assert result["unmatched"] == []


def test_fee_settings_isolate_users_and_reject_invalid_rates(tmp_path):
    settings = FeeSettings(tmp_path)
    value = FeeSchedule(source="custom").model_dump()
    settings.save("one", value)
    assert settings.get("one")["source"] == "custom"
    assert settings.get("two")["source"] == "account"
    with pytest.raises(ValueError):
        FeeSchedule(default={"open": float("nan")})


def test_rules_exclude_forming_bar_and_keep_long_exits_separate():
    p = plan()
    data = {60: {"bars": bars([824] * 40 + [1])}}
    result = evaluate_rules(p, data)
    assert result["rules"]["entryLong"]
    assert not result["rules"]["entryShort"]
    action = rule_position_intent(result["rules"], "long", 0, 1, 1)
    assert action["action"] == "close_short"
    p.direction = "long"; p.rules.entryShort = None
    result = evaluate_rules(p, {60: {"bars": bars([822] * 40 + [900])}})
    assert rule_position_intent(result["rules"], "long", 1, 0, 1)["action"] == "close_long"
    assert rule_position_intent(result["rules"], "long", 0, 0, 1)["action"] == "hold"


def test_cross_window_is_an_event_not_just_current_relative_position():
    c = Condition(label="MA交叉", left={"indicator": "close", "timeframe": 60}, operator="cross_up", right={"indicator": "constant", "value": 10}, withinBars=1)
    sample = bars([9] * 10 + [11, 12])
    assert _condition(c, sample)["status"] == "waiting"
    c.withinBars = 2
    assert _condition(c, sample)["status"] == "passed"


def test_chart_kd_formula_and_ambiguities_fail_closed():
    sample = bars([10, 12, 9, 13])
    k = d = 50
    expected = []
    for i, b in enumerate(sample):
        low, high = min(x["low"] for x in sample[:i+1]), max(x["high"] for x in sample[:i+1])
        k = (2 * k + (b["close"] - low) / (high - low) * 100) / 3
        d = (2 * d + k) / 3
        expected.append(d)
    assert indicator_values(sample, Operand(indicator="kd_d")) == pytest.approx(expected)
    p = plan(); p.questions = ["同时指同一根还是最近三根？"]
    with pytest.raises(ValueError, match="澄清"):
        p.check_executable()


class CycleSource(HistoricalSource):
    """A deterministic exchange fixture, never included in product runtime."""
    def __init__(self):
        super().__init__()
        self.price = 824
        self.data["account"].update(todayNetPnl=0, todayPnl=0, closeProfit=0, floatProfit=0, positionProfit=0, commission=0, positionCount=0)
        self.data["workbenches"]["60"]["bars"] = bars([824] * 180)
        self.data["clock"] = datetime.fromtimestamp((self.data["workbenches"]["60"]["bars"][-1]["datetime"] / 1e9), tz=datetime.fromisoformat("2026-09-04T00:00:00+00:00").tzinfo).isoformat()
        self._refresh()

    def _refresh(self):
        self.data["market"].update(price=self.price, lastPrice=self.price, bidPrice1=self.price-.02, askPrice1=self.price+.02, quoteDatetime=self.data["clock"])
        self.data["account"]["positionCount"] = len(self.data["positions"])

    def _request(self, method, path, payload=None, **kwargs):
        action = path.rsplit("/", 1)[-1]
        if action == "submit":
            result = super()._request(method, path, payload, **kwargs)
            self.data["orders"].append(copy.deepcopy(self.reports[payload["clientOrderId"]]["order"]))
            return result
        if action == "cancel":
            report = self.reports[payload["clientOrderId"]]
            report["order"]["status"] = "FINISHED"
            next(o for o in self.data["orders"] if o["clientOrderId"] == payload["clientOrderId"])["status"] = "FINISHED"
            return copy.deepcopy(report)
        if action == "advance":
            self.data["clock"] = (datetime.fromisoformat(self.data["clock"]) + timedelta(minutes=1)).isoformat()
            for order in self.data["orders"]:
                if order["status"] != "ALIVE":
                    continue
                order.update(status="FINISHED", volumeLeft=0)
                self.reports[order["clientOrderId"]]["order"].update(status="FINISHED", volumeLeft=0)
                price = self.price
                fill = {"tradeId": f"fill-{len(self.data['trades'])}", "clientOrderId": order["clientOrderId"], "symbol": SYMBOL,
                        "direction": order["direction"], "offset": order["offset"], "price": price, "volume": order["volume"],
                        "tradeDateTime": int(datetime.fromisoformat(self.data["clock"]).timestamp()*1e9), "tradingDay": self.data["tradingDay"]}
                self.data["trades"].append(fill)
                if order["offset"] == "OPEN":
                    self.data["positions"] = [{"symbol": SYMBOL, "direction": "long" if order["direction"] == "BUY" else "short", "volume": 1,
                        "todayVolume": 1, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0, "avgPrice": price, "floatingPnl": 0}]
                else:
                    position = self.data["positions"][0]
                    pnl = (price-position["avgPrice"])*1000*(1 if position["direction"]=="long" else -1)
                    self.data["account"]["equity"] += pnl
                    self.data["account"]["todayNetPnl"] += pnl
                    self.data["account"]["todayPnl"] += pnl
                    self.data["positions"] = []
            sample = self.data["workbenches"]["60"]["bars"]
            sample[-1].update(open=self.price, high=self.price+1, low=self.price-1, close=self.price)
            sample.append({**sample[-1], "datetime": sample[-1]["datetime"]+60_000_000_000})
            self._refresh()
            return copy.deepcopy(self.data)
        self._refresh()
        return super()._request(method, path, payload, **kwargs)


async def test_saved_strategy_runs_both_directions_in_isolated_replay_and_reviews_fees(tmp_path):
    source = CycleSource()
    app = create_app(tmp_path/"live.sqlite3", runtime_state_root=tmp_path, data_provider=source, agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            saved = await client.post("/api/strategy-plans", json={"plan": plan().model_dump()})
            assert saved.status_code == 201, saved.text
            identifier = saved.json()["strategyId"]
            duplicate = await client.post("/api/strategy-plans", json={"plan": plan().model_dump()})
            assert duplicate.json()["strategyId"] == identifier
            fees = FeeSchedule(source="custom", default={"open": 2, "closeToday": 3, "closeYesterday": 4}).model_dump()
            assert (await client.post("/api/trading-costs", json=fees)).status_code == 200
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1000000})
            assert created.status_code == 201, created.text
            prefix = created.json()["runtimePath"]
            await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action":"step"})
            validation = await client.post(prefix+f"/api/strategy-plans/{identifier}/validate", json={})
            assert validation.status_code == 200, validation.text
            identifier = validation.json()["strategyId"]
            assert (await client.post(prefix+f"/api/strategies/{identifier}/monitor/start", json={})).status_code == 200
            for price in [824, 822, 822, 822, 822, 824, 824, 824, 824]:
                source.price = price
                result = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action":"step"})
                assert result.status_code == 200, result.text
            assert [(o["direction"], o["offset"]) for o in source.submits[:4]] == [("BUY","OPEN"),("SELL","CLOSETODAY"),("SELL","OPEN"),("BUY","CLOSETODAY")]
            review = (await client.get(prefix+f"/api/trade-review?symbol={SYMBOL}")).json()
            assert [t["direction"] for t in review["trades"]] == ["long","short"]
            assert [t["fees"] for t in review["trades"]] == [5,5]
            assert review["account"]["commission"] == 10
            assert app.state.store.get_strategy_draft(saved.json()["strategyId"], user_id="local-user")["status"] == "draft"
            assert app.state.simulation_orders.list(user_id="local-user") == []
            await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action":"pause_entries"})
            # Fill an already submitted entry, then still allow its rule exit.
            source.price = 822
            for _ in range(2):
                await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action":"step"})
            assert source.submits[-1]["offset"] == "CLOSETODAY"
            assert not source.data["positions"]
            finish = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action":"finish"})
            assert finish.json()["ended"] is True
            assert (await client.post(prefix+f"/api/strategies/{identifier}/monitor/start", json={})).status_code == 409
            assert (await client.get(prefix+"/api/account/summary")).json()["marketSessionStatus"] == "closed"
            assert (tmp_path/"replays"/IDENTIFIER/"review.json").is_file()


async def test_custom_fees_frozen_and_account_not_double_adjusted():
    source = CycleSource(); provider = ReplayProvider(source, IDENTIFIER)
    provider.fee_schedule = FeeSchedule(source="custom", default={"open":2}).model_dump()
    source.data["trades"] = [{"tradeId":"a", "symbol":SYMBOL,"direction":"BUY","offset":"OPEN","volume":1,"price":824,"tradeDateTime":1,"tradingDay":"2026-09-04"}]
    source.data["account"].update(commission=10, equity=999990, todayNetPnl=-10, todayPnl=-10)
    for _ in range(3):
        await provider.command("state")
        assert provider.account_summary("")["equity"] == 999998
        assert provider.account_summary("")["commission"] == 2


def test_multitimeframe_rules_never_use_forming_daily_candle():
    p = plan()
    raw = p.model_dump()
    raw['rules']['entryLong']['conditions'].append({'label':'日线趋势','left':{'indicator':'ma','timeframe':86400,'period':5},'operator':'gt','right':{'indicator':'ma','timeframe':86400,'period':10},'withinBars':1})
    p = StrategyPlan.model_validate(raw)
    daily = bars(list(range(40)) + [-10000],86400)
    shift = 40 * 86400 * 1_000_000_000
    daily = [{**b,'datetime':b['datetime']-shift} for b in daily]
    result = evaluate_rules(p,{60:{'bars':bars([824]*40+[1])},86400:{'bars':daily}})
    assert result['rules']['entryLong'] is True
    daily[-1].update(close=10000,high=10001,low=9999)
    assert evaluate_rules(p,{60:{'bars':bars([824]*40+[1])},86400:{'bars':daily}})['rules'] == result['rules']


def test_ordered_crosses_require_real_chronological_order():
    p = plan().model_dump()
    first={'label':'先上穿10','left':{'indicator':'close','timeframe':60},'operator':'cross_up','right':{'indicator':'constant','value':10},'withinBars':3}
    second={**first,'label':'后上穿20','right':{'indicator':'constant','value':20}}
    p['rules']['entryLong']={'join':'all','sequence':True,'conditions':[first,second]}
    data={60:{'bars':bars([9]*12+[11,21,22,0])}}
    assert evaluate_rules(StrategyPlan.model_validate(p),data)['rules']['entryLong']
    p['rules']['entryLong']['conditions']=[second,first]
    assert not evaluate_rules(StrategyPlan.model_validate(p),data)['rules']['entryLong']


def test_fee_allocation_preserves_cents_when_position_closes_in_parts():
    schedule = FeeSchedule(source='custom',default={'open':0.0033,'closeToday':0,'closeYesterday':0}).model_dump()
    fills=[{'tradeId':'open','symbol':SYMBOL,'direction':'BUY','offset':'OPEN','price':800,'volume':3,'tradeDateTime':1}]
    fills += [{'tradeId':f'close-{i}','symbol':SYMBOL,'direction':'SELL','offset':'CLOSETODAY','price':800,'volume':1,'tradeDateTime':i+2} for i in range(3)]
    fills = [{**fill, 'tradingDay': '2026-09-04'} for fill in fills]
    result=review_trades(fills,schedule,1000)
    assert result['estimatedCommission'] == .01
    assert sum(t['fees'] for t in result['trades']) == .01
    assert result['estimatedNet'] == -.01


def test_strategy_loss_limit_blocks_entries_but_never_its_exits():
    from test_strategy_intents import account, build, evaluation, strategy
    item=strategy(); item['definition'].update(schemaVersion=2,direction='both',plan=plan().model_dump())
    item['definition']['riskLimits']['maxLoss']=1000
    signal=evaluation(); signal.update(rules={'entryLong':True,'exitLong':True,'entryShort':False,'exitShort':False})
    snapshot=account(); snapshot['todayNetPnl']=-1000
    intent=build(strategy=item,evaluation=signal,account=snapshot)
    assert intent['riskAdjustedIntent']['status']=='blocked'
    intent=build(strategy=item,evaluation=signal,account=snapshot,positions=[{'symbol':item['symbol'],'direction':'long','volume':1}])
    assert intent['riskAdjustedIntent']['action']=='close_long'
    assert intent['riskAdjustedIntent']['status']=='reduce_only'


async def test_finish_cancels_pending_order_through_shared_order_service(tmp_path):
    from trader_runtime.replay import ReplayContext
    source = CycleSource()
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command('state')
    child = create_app(tmp_path/'replay.sqlite3',runtime_state_root=tmp_path,agent_mode='fake',data_provider=provider,simulation_gateway=provider,_replay_provider=provider)
    try:
        session=child.state.broker.create_session('local','native','local-user')
        order=await child.state.autonomy.execute_trade(user_id='local-user',session_id=session.session_id,symbol=SYMBOL,side='buy_open',volume=1,price_intent='passive',reason='pending order cancellation acceptance')
        await provider.command('state')
        await child.state.simulation_orders.query(order['clientOrderId'],user_id='local-user')
        context=ReplayContext('local-user',child,provider)
        await context.finish()
        assert source.reports[order['clientOrderId']]['order']['status']=='FINISHED'
        await provider.command('state')
        await context.finish()
        assert context.ended and not source.data['positions']
        assert len(source.submits)==1
    finally:
        child.state.store.close()


@pytest.mark.asyncio
async def test_delete_checks_all_contracts_and_preserves_history_and_ownership(tmp_path):
    from trader_runtime.strategy_scope import execution_strategy
    app = create_app(tmp_path/"delete.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="test-delete-token")
    headers = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-delete-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            item = (await client.post("/api/strategy-plans", json={"plan": plan().model_dump()})).json()
            store = app.state.store
            kwargs = dict(store=store, broker=app.state.broker, user="local-user")
            execution = execution_strategy(item, "SHFE.rb2610", **kwargs)
            with store.connection:
                store.connection.execute("UPDATE strategy_drafts SET status='monitoring' WHERE id=?", (execution["strategyId"],))
            blocked = await client.delete(f"/api/strategy-plans/{item['strategyId']}")
            assert blocked.status_code == 409 and "SHFE.rb2610" in blocked.json()["detail"]
            with store.connection:
                store.connection.execute("UPDATE strategy_drafts SET status='paused' WHERE id=?", (execution["strategyId"],))
            store.save_strategy_intent_snapshot(intent_id="pending-intent", strategy_id=execution["strategyId"], user_id="local-user", strategy_digest=execution["definitionDigest"], signal_bar_time="1", context_digest="context", payload={})
            store.reserve_strategy_execution_attempt(attempt_id="pending", strategy_id=execution["strategyId"], user_id="local-user", intent_id="pending-intent", signal_bar_time="1", trading_day="2026-09-06", action="open_long", volume=1)
            assert (await client.delete(f"/api/strategy-plans/{item['strategyId']}")).status_code == 409
            store.finish_strategy_execution_attempt(attempt_id="pending", status="NOT_DISPATCHED", detail="尚未发送")
            now = "2026-09-06T00:00:00+00:00"
            with store.connection:
                store.connection.execute("INSERT INTO ai_execution_authorizations (id,user_id,session_id,status,symbols_json,permissions_json,max_order_volume,max_orders,expires_at,created_at,updated_at) VALUES ('auth','local-user','test','STOPPED','[]','[]',1,1,?,?,?)", (now,now,now))
                store.connection.execute("INSERT INTO ai_stop_orders (id,user_id,authorization_id,session_id,symbol,position_direction,position_bucket,volume,trigger_price,price_intent,status,strategy_metadata_json,remaining_volume,created_at,updated_at) VALUES ('stop','local-user','auth','test','SHFE.rb2610','long','today',1,'1','passive','ARMED',?,1,?,?)", ('{"strategyId":"'+execution["strategyId"]+'"}',now,now))
            assert (await client.delete(f"/api/strategy-plans/{item['strategyId']}")).status_code == 409
            with store.connection:
                store.connection.execute("UPDATE ai_stop_orders SET remaining_volume=0, status='CANCELLED' WHERE id='stop'")
            assert (await client.delete(f"/api/strategy-plans/{item['strategyId']}")).status_code == 200
            assert (await client.delete(f"/api/strategy-plans/{item['strategyId']}")).status_code == 200
            assert not (await client.get("/api/strategy-plans")).json()["items"]
            assert store.get_strategy_draft(execution["strategyId"], user_id="local-user")["status"] == "archived"
            assert store.connection.execute("SELECT COUNT(*) FROM strategy_execution_attempts").fetchone()[0] == 1
            assert (await client.post(f"/api/strategy-plans/{item['strategyId']}/validate", json={"symbol": "SHFE.au2612"})).status_code == 422
            with store.connection:
                store.connection.execute("UPDATE strategy_drafts SET user_id='other-user' WHERE id=?", (item["strategyId"],))
            assert (await client.delete(f"/api/strategy-plans/{item['strategyId']}")).status_code == 404
