"""策略模板库 (策略积木·第二期 A, 2026-09-25) and the backtest fee reference.

Every template validates as an ordinary StrategyPlan at load (an invalid one stops the library), a
preview backtests each of them on the fake history, out-of-range parameters are refused with a
Chinese reason, an instantiated draft passes the ordinary validation, the routes and tools sit in
the capability catalog, and 账户默认 backtests estimate fees from the daily exchange standard (openctp).
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from conftest import per_lot_entry, seed_exchange_fees
from test_strategy_backtest import DOUBLE_MA, DOUBLE_MA_CLOSES, cond, gapless, run, series
from trader_runtime import strategy_templates
from trader_runtime.main import create_app
from trader_runtime.strategy_rules import StrategyPlan

HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer tpl-token"}
SHFE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
        "night": [["21:00:00", "23:00:00"]]}
FIRST_BATCH = {"double_ma", "donchian_turtle", "boll_channel", "supertrend", "multi_timeframe", "atr_rsi",
               "aberration", "dual_thrust", "fairy_four_price", "sky_garden"}
# 策略积木·第三期 B (2026-09-25): built from the second batch of blocks (枢轴、当日最高/最低、复位、连续 N 根、上升/下降).
SECOND_BATCH = {"r_breaker", "rsi_reversal", "pivot_reversal", "squeeze", "king_keltner"}
# 第三批积木 (2026-09-27): Z-Score 均值回归 (TqSdk z_score.py) and 连涨连跌 (TradingView Consecutive Up/Down).
THIRD_BATCH = {"z_score", "consecutive_updown"}
# 第四批积木 (2026-09-27): 恒温器 (Pruitt & Hill, 条件组合 + CMI), NR7 窄幅突破 (Crabel), 威廉 %R 确认反转.
FOURTH_BATCH = {"thermostat", "nr7_breakout", "williams_r"}
# 第五批模板 (2026-09-28): 海龟交易法（含加仓）——第六批加仓积木补齐原版海龟的金字塔加仓.
FIFTH_BATCH = {"turtle_pyramid"}
# 价差策略第一期 (2026-09-29): 跨期价差回归（布林）——第一个价差模板，参数是两个具体合约.
SIXTH_BATCH = {"spread_boll_reversion"}
LIBRARY = FIRST_BATCH | SECOND_BATCH | THIRD_BATCH | FOURTH_BATCH | FIFTH_BATCH | SIXTH_BATCH


def app_for(tmp_path, name="tpl"):
    return create_app(tmp_path / f"{name}.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="tpl-token")


def continuous_fake_schedule(monkeypatch):
    """The fake bars run continuously, so give preview tests matching synthetic sessions."""
    from trader_runtime.data_providers import FakeReadOnlyDataProvider

    original = FakeReadOnlyDataProvider.market_history

    def history(self, symbol, duration_seconds, length):
        return {**original(self, symbol, duration_seconds, length),
                "tradingSchedule": {"day": [["00:00:00", "23:59:00"]], "night": []}}

    monkeypatch.setattr(FakeReadOnlyDataProvider, "market_history", history)


def test_the_first_batch_validates_as_ordinary_plans():
    library = strategy_templates.templates()
    assert set(library) == LIBRARY
    assert {item.category for item in library.values()} == set(strategy_templates.CATEGORIES)
    for template in library.values():
        plan, values = strategy_templates.build_plan(template)
        assert isinstance(plan, StrategyPlan) and plan.timeframeSeconds == template.recommendedTimeframe
        assert values == template.defaults() and template.source.url.startswith("https://")
        assert not strategy_templates.PLACEHOLDER.search(json.dumps(plan.model_dump(), ensure_ascii=False))
    # 保护止损按 ATR（随品种波动缩放）：趋势 / 通道 3×ATR14，日内 2×ATR14，Aberration 日线 4×ATR20；
    # 自带止损的模板保持原样（唐奇安 2×ATR20、布林吊灯 5.2×ATR30、菲阿里与空中花园按规则止损）。
    stops = {key: (plan.atrStop.multiple, plan.atrStop.period) if plan.atrStop else None
             for key, plan in ((key, strategy_templates.build_plan(item)[0]) for key, item in library.items())}
    assert stops == {"double_ma": (3, 14), "supertrend": (3, 14), "multi_timeframe": (3, 14), "atr_rsi": (3, 14),
                     "aberration": (4, 20), "dual_thrust": (2, 14), "donchian_turtle": (2, 20),
                     "boll_channel": (5.2, 30), "fairy_four_price": None, "sky_garden": None,
                     # 第二批：原版没有止损的另设保护止损；R-Breaker / 枢轴反转的价格止损按原版换算成 ATR。
                     "rsi_reversal": (3, 14), "squeeze": (3, 14), "king_keltner": (3, 14),
                     "r_breaker": (20, 14), "pivot_reversal": (0.5, 14),
                     # 第三批：原版都没有价格止损，另设保护止损。
                     "z_score": (3, 14), "consecutive_updown": (3, 14),
                     # 第四批：恒温器按原版 3×ATR10 保护止损；NR7、威廉 %R 原版没有价格止损，另设保护止损。
                     "thermostat": (3, 10), "nr7_breakout": (3, 14), "williams_r": (2.5, 14),
                     # 第五批：海龟（含加仓）沿用唐奇安海龟的 2×ATR20 止损。
                     "turtle_pyramid": (2, 20),
                     # 价差模板：止损按收盘价差的点数，不用 ATR（价差策略第一期）。
                     "spread_boll_reversion": None}
    # Z-Score：|Z| ≥ 1.8 反向开仓 = 收盘越过布林(14, 1.8)；Z 回到 0.4 以内或偏到 2.5 离场（任一），持仓满 8 根离场。
    z_score = strategy_templates.build_plan(library["z_score"], {"window": 20, "entryZ": 2, "exitZ": 0.5, "stopZ": 3})[0]
    entry_long, exits = z_score.rules.entryLong.conditions[0], z_score.rules.exitLong
    assert (entry_long.operator, entry_long.right.indicator, entry_long.right.period, entry_long.right.k) == ("lt", "boll_down", 20, 2)
    assert exits.join == "any" and [(c.operator, c.right.indicator, c.right.k) for c in exits.conditions] == [
        ("gte", "boll_down", 0.5), ("lt", "boll_down", 3)]
    assert [(c.operator, c.right.indicator, c.right.k) for c in z_score.rules.exitShort.conditions] == [
        ("lte", "boll_up", 0.5), ("gt", "boll_up", 3)]
    assert z_score.maxHoldBars == 8 and z_score.timeframeSeconds == 86400
    with pytest.raises(strategy_templates.TemplateError, match="止损 Z 值须大于开仓 Z 值"):
        strategy_templates.build_plan(library["z_score"], {"stopZ": 1.5})
    # 连涨连跌：收盘较上一根上升连续 N 根开多，反向连续 M 根平多（不反手）。
    streak = strategy_templates.build_plan(library["consecutive_updown"], {"upBars": 4, "downBars": 2})[0]
    long_entry, long_exit = streak.rules.entryLong.conditions[0], streak.rules.exitLong.conditions[0]
    assert (long_entry.operator, long_entry.right.value, long_entry.consecutive) == ("rising", 1, 4)
    assert (long_exit.operator, long_exit.consecutive) == ("falling", 2)
    assert streak.rules.entryShort.conditions[0].consecutive == 2 and streak.rules.exitShort.conditions[0].consecutive == 4
    # 恒温器：CMI 分界同时改八处，短线幅度在做空与平多一侧取负；两种模式各是一个分支，按“任一”合并。
    thermostat = strategy_templates.build_plan(library["thermostat"], {"switchLevel": 25, "swingAtr": 0.6, "bandPeriod": 40})[0]
    for key in ("entryLong", "exitLong", "entryShort", "exitShort"):
        group = getattr(thermostat.rules, key)
        assert group.join == "any" and not group.conditions and len(group.branches) == 2
        assert [(b.conditions[0].operator, b.conditions[0].right.value) for b in group.branches] == [("gte", 25), ("lt", 25)]
    assert [getattr(thermostat.rules, key).branches[1].conditions[1].right.offset.factor
            for key in ("entryLong", "exitLong", "entryShort", "exitShort")] == [0.6, -0.6, -0.6, 0.6]
    assert thermostat.rules.exitLong.branches[0].conditions[1].right.period == 40 and thermostat.timeframeSeconds == 86400
    # NR7：比较天数改日线振幅的区间最低；威廉 %R：超卖 / 超买线两边对称，等待根数改 withinBars。
    nr = strategy_templates.build_plan(library["nr7_breakout"], {"nrDays": 4})[0]
    assert nr.rules.entryLong.conditions[0].right.lowest == nr.rules.entryShort.conditions[0].right.lowest == 4
    assert nr.rules.entryLong.conditions[0].left.timeframe == 86400 and nr.closeBeforeEndMinutes == 5
    wr = strategy_templates.build_plan(library["williams_r"], {"extreme": 10, "lookback": 3})[0]
    assert [(c.right.value, c.withinBars) for c in (wr.rules.entryLong.conditions[0], wr.rules.entryShort.conditions[0],
            wr.rules.exitLong.conditions[0], wr.rules.exitShort.conditions[0])] == [(-90, 3), (-10, 3), (-10, 3), (-90, 3)]
    assert library["rsi_reversal"].param("stopAtr").label == "保护止损（ATR 倍数）"
    assert library["r_breaker"].param("stopAtr").label == "止损（ATR 倍数）"
    # 复位：RSI 迟滞反转开多后须 RSI 回到 35 以上才再开多（LEAN 35 / 65），离场线参数同时改两边。
    rsi = strategy_templates.build_plan(library["rsi_reversal"], {"rearmLow": 40, "exitLevel": 55})[0]
    assert rsi.rules.entryLong.rearm[0].right.value == 40 and rsi.rules.entryShort.rearm[0].right.value == 65
    assert rsi.rules.exitLong.conditions[0].right.value == rsi.rules.exitShort.conditions[0].right.value == 55
    with pytest.raises(strategy_templates.TemplateError, match="多头复位线须高于超卖线"):
        strategy_templates.build_plan(library["rsi_reversal"], {"rearmLow": 25})
    # R-Breaker：突破 R3 / S3 开仓，“曾过 R2 且跌破 R1”平多（不反手），收盘前 10 分钟平仓。
    breaker = strategy_templates.build_plan(library["r_breaker"])[0]
    assert breaker.rules.entryLong.conditions[0].right.value == 3 and breaker.rules.entryShort.conditions[0].right.value == -3
    assert [(c.left.indicator, c.right.value) for c in breaker.rules.exitLong.conditions] == [("session_high", 2), ("close", 1)]
    assert breaker.closeBeforeEndMinutes == 10
    # 挤压释放：此前连续 N 根挤压（withinBars 2 让连续段在上一根结束）+ 本根释放 + 动量方向。
    squeeze = strategy_templates.build_plan(library["squeeze"], {"minBars": 8, "momentum": 12})[0]
    held = squeeze.rules.entryLong.conditions[0]
    assert (held.consecutive, held.withinBars, held.operator) == (8, 2, "lt")
    assert squeeze.rules.entryShort.conditions[2].operator == "falling" and squeeze.rules.entryShort.conditions[2].right.value == 12
    assert library["dual_thrust"].param("stopAtr").label == "保护止损（ATR 倍数）"
    assert library["double_ma"].param("stopLossTicks") is None
    tighter = strategy_templates.build_plan(library["double_ma"], {"stopAtr": 1.5})[0]
    assert tighter.atrStop.multiple == 1.5
    dual = library["dual_thrust"]
    plan, _ = strategy_templates.build_plan(dual, {"n": 3, "k1": 0.35})
    entry, exit_long = plan.rules.entryLong.conditions[0], plan.rules.exitLong.conditions[0]
    assert (entry.right.offset.factor, entry.right.offset.period) == (0.35, 3)
    assert exit_long.right.offset.factor == -0.2 and entry.label == "收盘突破今开 + 0.35×3日振幅"
    assert plan.maxEntriesPerDirection == 1 and plan.closeBeforeEndMinutes == 5
    rsi = strategy_templates.build_plan(library["multi_timeframe"], {"rsiSignal": 25})[0]
    assert rsi.rules.entryLong.conditions[1].right.value == 75 and rsi.rules.entryShort.conditions[1].right.value == 25


def test_an_invalid_template_stops_the_library(tmp_path, monkeypatch):
    broken = json.loads((strategy_templates.TEMPLATE_DIR / "double_ma.json").read_text(encoding="utf-8"))
    broken["plan"]["rules"]["entryLong"]["conditions"][0]["right"]["timeframe"] = 300   # two clocks in one condition
    (tmp_path / "double_ma.json").write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(strategy_templates, "TEMPLATE_DIR", tmp_path)
    strategy_templates.templates.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="double_ma.json"):
            strategy_templates.templates()
    finally:
        monkeypatch.undo()
        strategy_templates.templates.cache_clear()
    assert len(strategy_templates.templates()) == len(LIBRARY)


def test_parameters_are_checked_with_chinese_reasons():
    library = strategy_templates.templates()
    cases = [(library["dual_thrust"], {"k1": 5}, "向上突破幅度（K1）须在 0.05–2×振幅 之间"),
             (library["dual_thrust"], {"k1": 0.33}, "按 0.05 递增"),
             (library["double_ma"], {"fast": 30, "slow": 20}, "快线周期须小于慢线周期"),
             (library["double_ma"], {"nope": 1}, "没有参数 nope"),
             (library["boll_channel"], {"bollN": "18"}, "须是数字"),
             (library["double_ma"], {"fast": None}, "须是数字"),
             (library["double_ma"], {"maxLoss": 50}, "当日亏损上限须在 100–1000000元 之间")]
    for template, params, reason in cases:
        with pytest.raises(strategy_templates.TemplateError, match=reason):
            strategy_templates.build_plan(template, params)
    # 当日亏损上限 (10-08 用户决定 A): optional, not set by any template; filled in only when the user gives it.
    assert all(t.param("maxLoss") is None or (t.param("maxLoss").optional and t.param("maxLoss").default is None)
               for t in library.values())
    assert all(strategy_templates.build_plan(t)[0].maxLoss is None for t in library.values())
    capped, values = strategy_templates.build_plan(library["double_ma"], {"maxLoss": 3000})
    assert capped.maxLoss == 3000 and values["maxLoss"] == 3000
    for empty in (None, "", " "):
        assert strategy_templates.build_plan(library["double_ma"], {"maxLoss": empty})[0].maxLoss is None
    # A faster execution period is fine; one slower than the 1 小时 filter is refused.
    assert strategy_templates.build_plan(library["boll_channel"], timeframe=300)[0].timeframeSeconds == 300
    with pytest.raises(strategy_templates.TemplateError, match="执行周期不能慢于条件周期"):
        strategy_templates.build_plan(library["boll_channel"], timeframe=86400)


def _preview_days(template_id: str) -> int | None:
    """The depth each template needs so it trades on the fake history (kept from the single-test
    loop this table came from)."""
    return None if (strategy_templates.templates()[template_id].recommendedTimeframe == 86400
                    or template_id in SECOND_BATCH | THIRD_BATCH | FOURTH_BATCH) else 3


@pytest.mark.asyncio
@pytest.mark.parametrize("template_id", sorted(LIBRARY))
async def test_every_template_previews_a_backtest_on_the_fake_history(tmp_path, monkeypatch, template_id):
    """One case per template. A preview is a real rule backtest over the fake history — compute,
    not a wait — so each template is its own case (the slowest ones run ~2 s of bar-by-bar rule
    judgement that the trades assertions need; they cannot be shortened without changing what is
    asserted) and the library previews in parallel under xdist."""
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, f"tpl-{template_id}")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            days = _preview_days(template_id)
            response = await client.post(f"/api/strategy-templates/{template_id}/preview",
                                         json={"symbol": "SHFE.au2612", **({"tradingDays": days} if days else {})})
            assert response.status_code == 200, (template_id, response.text)
            body = response.json()
            assert body["templateId"] == template_id and body["backtest"]["kind"] == "rule"
            assert body["backtest"]["templateId"] == template_id and body["backtest"]["coverage"]["bars"] > 0
            assert body["backtest"]["feeEstimate"]["source"] == "none"      # no reference in the test Runtime
            if template_id in SECOND_BATCH | THIRD_BATCH:
                assert body["backtest"]["summary"]["trades"] > 0, template_id


@pytest.mark.asyncio
async def test_routes_list_preview_every_template_and_refuse_bad_parameters(tmp_path, monkeypatch):
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            listing = (await client.get("/api/strategy-templates")).json()
            assert [item["id"] for item in listing["categories"]] == list(strategy_templates.CATEGORIES)
            assert {item["id"] for item in listing["items"]} == LIBRARY
            first = listing["items"][0]
            assert first["categoryLabel"] and first["scenario"] and first["source"]["label"] and first["params"][0]["targets"]
            detail = (await client.get("/api/strategy-templates/dual_thrust")).json()
            StrategyPlan.model_validate(detail["plan"]).check_executable()
            assert detail["defaults"]["k1"] == 0.2
            assert (await client.get("/api/strategy-templates/nope")).status_code == 404
            # Every template previews (one backtest each) in the parametrized case above.
            changed = await client.post("/api/strategy-templates/dual_thrust/preview",
                                        json={"symbol": "SHFE.au2612", "tradingDays": 3, "params": {"k1": 0.5, "n": 3}})
            assert changed.status_code == 200 and changed.json()["params"]["k1"] == 0.5
            assert changed.json()["plan"]["rules"]["entryLong"]["conditions"][0]["right"]["offset"]["period"] == 3
            # 价差模板的两条腿是合约代码参数：客户端把它们和数字参数一起发来（2026-09-29 线上 422 回归）。
            legs = await client.post("/api/strategy-templates/spread_boll_reversion/preview", json={
                "symbol": "SHFE.rb2701", "timeframe": 3600,
                "params": {"near": "SHFE.rb2701", "far": "SHFE.rb2705", "bollN": 20, "bollK": 2, "spreadStop": 60}})
            assert legs.status_code == 200, legs.text
            assert [leg["symbol"] for leg in legs.json()["plan"]["spread"]["legs"]] == ["SHFE.rb2701", "SHFE.rb2705"]
            bad_leg = await client.post("/api/strategy-templates/spread_boll_reversion/preview", json={
                "symbol": "SHFE.rb2701", "params": {"near": "SHFE.rb2701", "far": "螺纹"}})
            assert bad_leg.status_code == 422
            refused = await client.post("/api/strategy-templates/dual_thrust/preview",
                                        json={"symbol": "SHFE.au2612", "params": {"k1": 9}})
            assert refused.status_code == 422 and "向上突破幅度（K1）须在" in refused.json()["detail"]
            slow = await client.post("/api/strategy-templates/boll_channel/preview",
                                     json={"symbol": "SHFE.au2612", "timeframe": 86400})
            assert slow.status_code == 422 and "执行周期" in slow.json()["detail"]
            assert app.state.store.connection.execute("SELECT COUNT(*) FROM strategy_drafts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_instantiated_drafts_are_ordinary_strategies_that_validate(tmp_path, monkeypatch):
    from trader_runtime.data_providers import FakeReadOnlyDataProvider
    original = FakeReadOnlyDataProvider.market_workbench

    def with_schedule(self, symbol, duration_seconds):
        workbench = original(self, symbol, duration_seconds)
        workbench["quote"]["tradingSchedule"] = SHFE        # 收盘前平仓 needs the contract's sessions to go live
        return workbench
    monkeypatch.setattr(FakeReadOnlyDataProvider, "market_workbench", with_schedule)
    app = app_for(tmp_path, "inst")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            body = {"params": {"k1": 0.3}, "chartSymbol": "SHFE.rb2701"}
            created = await client.post("/api/strategy-templates/dual_thrust/instantiate", json=body)
            assert created.status_code == 201, created.text
            item = created.json()
            source = item["definition"]["templateSource"]
            assert source == {"templateId": "dual_thrust", "templateName": "Dual Thrust",
                              "params": {**strategy_templates.templates()["dual_thrust"].defaults(), "k1": 0.3},
                              "defaults": False}
            assert item["symbol"] == "*" and item["status"] == "draft" and item["definition"]["instrumentScope"]["mode"] == "any"
            # 当日亏损上限 is not the template's to choose (10-08 用户决定 A): unset unless the params give it.
            assert item["definition"]["plan"]["maxLoss"] is None and item["definition"]["riskLimits"]["maxLoss"] is None
            # 模板名 · 合约中文名 · 周期: the catalog knows 螺纹钢2610, so rb2701 reads 螺纹钢2701.
            assert item["name"] == "Dual Thrust · 螺纹钢2701 · 1分钟"
            again = (await client.post("/api/strategy-templates/dual_thrust/instantiate", json=body)).json()
            assert again["strategyId"] == item["strategyId"]                   # same definition: the same draft
            # Other parameters, same default name: a new strategy with a sequence number.
            second = (await client.post("/api/strategy-templates/dual_thrust/instantiate",
                                        json={**body, "params": {"k1": 0.4}})).json()
            third = (await client.post("/api/strategy-templates/dual_thrust/instantiate",
                                       json={**body, "params": {"k1": 0.5}})).json()
            assert (second["name"], third["name"]) == ("Dual Thrust · 螺纹钢2701 · 1分钟 (2)", "Dual Thrust · 螺纹钢2701 · 1分钟 (3)")
            assert second["version"] == third["version"] == 1
            exact = (await client.post("/api/strategy-templates/dual_thrust/instantiate",
                                       json={"params": {"k1": 0.6, "maxLoss": 3000}, "chartSymbol": "SHFE.au2612", "timeframe": 300})).json()
            assert exact["name"] == "Dual Thrust · 沪金2612 · 5分钟" and exact["definition"]["plan"]["timeframeSeconds"] == 300
            assert exact["definition"]["plan"]["maxLoss"] == 3000
            plain = (await client.post("/api/strategy-templates/aberration/instantiate", json={})).json()
            assert plain["name"] == "Aberration · 日线"                         # no contract given
            library = (await client.get("/api/strategy-plans")).json()["items"]
            assert item["strategyId"] in [entry["strategyId"] for entry in library] and len(library) == 5
            # 说人话：详情的一句话总结随列表返回，来自模板的用模板的大白话。
            summaries = {entry["strategyId"]: entry["planSummary"] for entry in library}
            assert summaries[item["strategyId"]] == strategy_templates.templates()["dual_thrust"].plainSummary
            validated = await client.post(f"/api/strategy-plans/{item['strategyId']}/validate", json={"symbol": "SHFE.au2612"})
            assert validated.status_code == 200 and validated.json()["status"] == "passed", validated.text
            # 恢复模板默认: the defaults as the next version of that strategy, under its own name.
            restored = (await client.post("/api/strategy-templates/dual_thrust/instantiate",
                                          json={"baseStrategyId": item["strategyId"]})).json()
            assert restored["name"] == item["name"] and restored["version"] == item["version"] + 1
            assert restored["definition"]["templateSource"]["defaults"]
            wrong = await client.post("/api/strategy-templates/double_ma/instantiate", json={"baseStrategyId": item["strategyId"]})
            assert wrong.status_code == 422
            # A requested name is used as given; taken, it gets a number too.
            scoped = await client.post("/api/strategy-templates/double_ma/instantiate",
                                       json={"symbol": "SHFE.rb", "name": "我的双均线"})
            assert scoped.status_code == 201 and scoped.json()["definition"]["instrumentScope"] == {"mode": "selected", "symbols": ["SHFE.rb"]}
            assert scoped.json()["name"] == "我的双均线"
            renamed = (await client.post("/api/strategy-templates/supertrend/instantiate", json={"name": "我的双均线"})).json()
            assert renamed["name"] == "我的双均线 (2)"
            bad = await client.post("/api/strategy-templates/double_ma/instantiate", json={"params": {"fast": 1}})
            assert bad.status_code == 422 and "短均线（快线周期）须在" in bad.json()["detail"]


@pytest.mark.asyncio
async def test_catalog_and_tools_cover_the_template_library(tmp_path, monkeypatch):
    continuous_fake_schedule(monkeypatch)
    from test_ai_annotations import with_tools
    app = app_for(tmp_path, "tools")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            with_tools(app)
            catalog = {item["operation"]: item for item in (await client.get("/api/agent/capabilities")).json()["operations"]}
            for name in ("GET /api/strategy-templates", "GET /api/strategy-templates/{template_id}",
                         "POST /api/strategy-templates/{template_id}/preview", "tool:list_strategy_templates",
                         "tool:preview_strategy_template"):
                assert catalog[name]["readOnly"], name
            for name in ("POST /api/strategy-templates/{template_id}/instantiate", "tool:create_strategy_from_template"):
                assert not catalog[name]["readOnly"], name
            listed = (await client.post("/api/agent/call", json={"operation": "tool:list_strategy_templates",
                                                                   "body": {"category": "intraday_range"}})).json()
            assert listed["ok"] and {item["id"] for item in listed["data"]["data"]["items"]} == {
                "dual_thrust", "fairy_four_price", "sky_garden", "r_breaker", "nr7_breakout"}
            reversal = (await client.post("/api/agent/call", json={"operation": "tool:list_strategy_templates",
                                                                     "body": {"category": "reversal"}})).json()
            assert [item["id"] for item in reversal["data"]["data"]["items"]] == ["rsi_reversal", "williams_r", "pivot_reversal", "z_score"]
            preview =(await client.post("/api/agent/call", json={"operation": "tool:preview_strategy_template",
                "body": {"templateId": "fairy_four_price", "symbol": "SHFE.au2612", "tradingDays": 3}})).json()
            assert preview["ok"] and preview["data"]["data"]["backtest"]["summary"] is not None
            refused = (await client.post("/api/agent/call", json={"operation": "tool:preview_strategy_template",
                "body": {"templateId": "dual_thrust", "symbol": "SHFE.au2612", "params": {"k2": 3}}})).json()
            assert not refused["ok"] and "向下突破幅度（K2）须在" in refused["data"]["warning"]
            created = (await client.post("/api/agent/call", json={"operation": "tool:create_strategy_from_template",
                "body": {"templateId": "supertrend", "params": {"multiplier": 2.5}}})).json()
            assert created["ok"] and created["data"]["data"]["templateSource"]["params"]["multiplier"] == 2.5
            assert app.state.store.get_strategy_draft(created["data"]["data"]["strategyId"], user_id="local-user")["status"] == "draft"


def test_template_command_validates_and_is_judged_by_the_reported_state():
    from pydantic import ValidationError

    from trader_runtime.ui_control import UiChanges, changes_match
    # 2026-10-08: the gallery and parameter page became the 策略编辑卡's 从模板; only restore is left.
    assert UiChanges.model_validate({"strategyTemplate": {"restore": True}}).strategyTemplate.restore is True
    for bad in ({}, {"run": True}, {"templateId": "dual_thrust", "run": True}, {"open": True}, {"other": 1}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"strategyTemplate": bad})
    state = {"open": False, "detail": {"defaults": True}}
    assert changes_match({"strategyTemplate": state}, {"strategyTemplate": {"restore": True}})
    assert not changes_match({"strategyTemplate": {**state, "detail": {"defaults": False}}},
                             {"strategyTemplate": {"restore": True}})
    assert changes_match({}, {"strategyTemplate": {"restore": True}})       # an older client reports nothing



# -- 回测默认费率：不自定义时按交易所标准（openctp 缓存） --------------------------------------------


def test_exchange_standard_backtest_charges_notional_per_lot_and_the_extra():
    notional = {"source": "exchange", "default": {"mode": "notional", "open": 1.0, "closeYesterday": 1.0,
                "closeToday": 2.0, "openPerLotExtra": 0.0, "closeYesterdayPerLotExtra": 0.0,
                "closeTodayPerLotExtra": 0.0}, "overrides": []}
    bars = series(gapless(DOUBLE_MA_CLOSES))
    result = run(DOUBLE_MA, bars, fees=notional)
    for trade in result["trades"]:      # same-day round trips: 开仓万分之1 + 平今万分之2 of the notional (乘数 10)
        expected = round(trade["openPrice"] * 10 / 10000, 2) + round(trade["closePrice"] * 10 * 2 / 10000, 2)
        assert trade["fees"] == pytest.approx(expected)
    assert result["feeEstimate"]["source"] == "exchange" and not result["feeEstimate"]["warning"]
    assert "交易所标准估算" in result["feeEstimate"]["text"]
    per_lot = {"source": "exchange", "default": {"mode": "per_lot", "open": 5, "closeYesterday": 5,
               "closeToday": 10}, "overrides": []}
    lots = run(DOUBLE_MA, bars, fees=per_lot)
    assert [trade["fees"] for trade in lots["trades"]] == [15, 15]                 # 开仓 5 + 平今 10
    # 同栏“金额 + 每手”并存（openctp 三栏口径不一还原出的形状）：perLotExtra 按手叠加。
    mixed = {**notional, "default": {**notional["default"], "openPerLotExtra": 1.0, "closeTodayPerLotExtra": 1.0}}
    extra = run(DOUBLE_MA, bars, fees=mixed)
    for plain, both in zip(result["trades"], extra["trades"]):
        assert both["fees"] == pytest.approx(plain["fees"] + 2)                    # 开 +1、平今 +1


def fees_app(root, name="fees"):
    root.mkdir(parents=True, exist_ok=True)
    return create_app(root / f"{name}.sqlite3", runtime_state_root=root, agent_mode="fake", local_token="tpl-token")


@pytest.mark.asyncio
async def test_account_default_backtests_use_exchange_standard_and_custom_stays(tmp_path):
    seed_exchange_fees(tmp_path, {"SHFE.au2612": per_lot_entry("SHFE", "au2612", "au", 1000, 5, 5, 10)})
    app = fees_app(tmp_path)
    body = {"plan": DOUBLE_MA.model_dump(), "symbol": "SHFE.au2612"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            estimated = (await client.post("/api/strategy-backtests", json=body)).json()
            assert estimated["feeEstimate"]["source"] == "exchange" and estimated["feeSource"] == "exchange"
            # 开仓 5 + 平今 10 inside a trading day, 开仓 5 + 平昨 5 across one.
            assert {trade["fees"] for trade in estimated["trades"]} <= {10, 15} and estimated["summary"]["fees"] > 0
            assert "au2612" in estimated["feeEstimate"]["text"] and "2026-10-08" in estimated["feeEstimate"]["text"]

            # 自定义费率原样生效（整套替换，不查交易所标准）。
            custom = {"source": "custom", "default": {"mode": "per_lot", "open": 1, "closeToday": 1, "closeYesterday": 1},
                      "overrides": []}
            assert (await client.post("/api/trading-costs", json=custom)).status_code == 200
            charged = (await client.post("/api/strategy-backtests", json=body)).json()
            assert charged["feeEstimate"]["source"] == "custom"
            assert charged["summary"]["fees"] == 2 * charged["summary"]["trades"]

    # 拿不到交易所标准（没有缓存、测试也不联网）：整次不扣并醒目标注。
    empty = tmp_path / "no-fees"
    bare = fees_app(empty, "bare")
    async with bare.router.lifespan_context(bare):
        async with AsyncClient(transport=ASGITransport(bare), base_url="http://runtime", headers=HEADERS) as client:
            missing = (await client.post("/api/strategy-backtests", json=body)).json()
            assert missing["feeEstimate"]["warning"] and missing["summary"]["fees"] == 0
            assert "未扣手续费" in missing["feeEstimate"]["text"]
    bare.state.store.close()
    app.state.store.close()


@pytest.mark.asyncio
async def test_spread_backtests_charge_each_legs_own_exchange_standard(tmp_path):
    """#506 的多腿口径保留：价差回测各腿读自己合约的交易所标准（B 不再静默套 A 的费率）；
    一腿拿不到时整次不扣并写明缺哪个合约；自定义 override 的正向对照不查缓存。"""
    SPREAD_A, SPREAD_B = "SHFE.rb2701", "SHFE.au2705"
    spread = StrategyPlan.model_validate({
        "name": "价差费率", "description": "两腿各查各的交易所标准", "direction": "long", "timeframeSeconds": 300,
        "maxPosition": 1, "stopLossTicks": 20, "cooldownBars": 0,
        "rules": {"entryLong": {"conditions": [cond("总是", "close", "gt", {"indicator": "constant", "value": -10_000})]},
                  "exitLong": {"conditions": [cond("从不", "close", "lt", {"indicator": "constant", "value": -10_000})]}},
        "spread": {"kind": "difference", "legs": [{"symbol": SPREAD_A}, {"symbol": SPREAD_B}]}})
    body = {"plan": spread.model_dump(), "symbol": SPREAD_A}
    both = tmp_path / "both"
    seed_exchange_fees(both, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 2, 2, 2),
                              "SHFE.au2705": per_lot_entry("SHFE", "au2705", "au", 1000, 20, 20, 20)})
    app = fees_app(both, "spread-fees")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            result = (await client.post("/api/strategy-backtests", json=body)).json()
            [trade] = result["trades"]
            assert [leg["fees"] for leg in trade["legs"]] == [4.0, 40.0]     # A 开2+平2；B 开20+平20
            assert trade["fees"] == 44.0 and result["summary"]["fees"] == 44.0
            assert result["feeEstimate"]["source"] == "exchange" and not result["feeEstimate"]["warning"]
            assert "rb2701" in result["feeEstimate"]["text"] and "au2705" in result["feeEstimate"]["text"]

            # 正向对照：自定义费表给 B 腿 override，不查缓存，两腿仍各按各的费率。
            custom = {"source": "custom", "default": {"mode": "per_lot", "open": 2, "closeToday": 2, "closeYesterday": 2},
                      "overrides": [{"symbol": SPREAD_B, "mode": "per_lot", "open": 20, "closeToday": 20,
                                     "closeYesterday": 20}]}
            assert (await client.post("/api/trading-costs", json=custom)).status_code == 200
            charged = (await client.post("/api/strategy-backtests", json=body)).json()
            assert charged["feeEstimate"]["source"] == "custom"
            [trade] = charged["trades"]
            assert [leg["fees"] for leg in trade["legs"]] == [4.0, 40.0]
    app.state.store.close()

    # B 腿的品种不在缓存里：不用 A 腿兜底，整次不计费并醒目写明缺哪个合约。
    only_a = tmp_path / "only-a"
    seed_exchange_fees(only_a, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 2, 2, 2)})
    missing_app = fees_app(only_a, "spread-missing")
    async with missing_app.router.lifespan_context(missing_app):
        async with AsyncClient(transport=ASGITransport(missing_app), base_url="http://runtime", headers=HEADERS) as client:
            missing = (await client.post("/api/strategy-backtests", json=body)).json()
            assert missing["summary"]["fees"] == 0 and missing["feeEstimate"]["warning"]
            assert "au2705" in missing["feeEstimate"]["text"] and "未扣手续费" in missing["feeEstimate"]["text"]
    missing_app.state.store.close()


@pytest.mark.asyncio
async def test_the_ai_saves_strategies_into_my_strategies_and_can_delete_an_old_draft(tmp_path, monkeypatch):
    """#932 F5 (10-19 夜盘): the AI's create_strategy_draft saved an old-format draft (schemaVersion 1) that 我的策略
    never lists and DELETE /api/strategy-plans refused — the user could neither see nor remove it. It now saves the
    StrategyPlan through the editor's own route (POST /api/strategy-plans): listed, editable (a running instance's id
    as base saves its library strategy's next version), deletable; an old draft not monitoring is deleted too."""
    continuous_fake_schedule(monkeypatch)
    from test_ai_annotations import with_tools
    app = app_for(tmp_path, "tools")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            with_tools(app)
            catalog = {item["operation"]: item for item in (await client.get("/api/agent/capabilities")).json()["operations"]}
            assert not catalog["tool:create_strategy_draft"]["readOnly"]
            plan = (await client.get("/api/strategy-templates/double_ma")).json()["plan"]
            plan = {**plan, "name": "沪银双均线2-3"}
            saved = (await client.post("/api/agent/call", json={"operation": "tool:create_strategy_draft", "body": {
                "plan": plan, "instrumentScope": {"mode": "selected", "symbols": ["SHFE.au2612"]}}})).json()
            assert saved["ok"], saved
            strategy_id = saved["data"]["data"]["strategyId"]
            library = (await client.get("/api/strategy-plans")).json()["items"]
            assert [item["name"] for item in library if item["strategyId"] == strategy_id] == ["沪银双均线2-3"]
            bad = (await client.post("/api/agent/call", json={"operation": "tool:create_strategy_draft",
                                                                "body": {"plan": {"name": "缺规则"}}})).json()
            assert not bad["ok"] and bad["data"]["warning"]  # the route's reason, for the next call to fix
            instance = (await client.post(f"/api/strategy-plans/{strategy_id}/validate",
                                          json={"symbol": "SHFE.au2612"})).json()["strategyId"]
            assert instance != strategy_id
            revised = (await client.post("/api/agent/call", json={"operation": "tool:create_strategy_draft", "body": {
                "plan": {**plan, "description": "改一下"}, "baseStrategyId": instance}})).json()
            assert revised["ok"], revised
            assert revised["data"]["data"]["name"] == "沪银双均线2-3" and revised["data"]["data"]["version"] == 2
            # A revision that does not say its scope keeps the base's (#1034 review), not 全部品种.
            stored = app.state.store.get_strategy_draft(revised["data"]["data"]["strategyId"], user_id="local-user")
            assert stored["definition"]["instrumentScope"] == {"mode": "selected", "symbols": ["SHFE.au2612"]}
            # An old-format draft (what the tool saved before): deleted (archived) through the same route.
            store = app.state.store
            session = app.state.broker.create_session("local", "native", "local-user")
            old = store.save_strategy_draft(strategy_id="legacy-draft-1", user_id="local-user",
                                            source_session_id=session.session_id, name="旧草稿", symbol="SHFE.ag2612",
                                            version=1, status="validating", definition={"schemaVersion": 1},
                                            definition_digest="b" * 64)
            deleted = await client.delete(f"/api/strategy-plans/{old['strategyId']}")
            assert deleted.status_code == 200, deleted.text
            assert store.get_strategy_draft(old["strategyId"], user_id="local-user")["status"] == "archived"
            refused = await client.delete(f"/api/strategy-plans/{instance}")
            assert refused.status_code == 409 and "策略库版本" in refused.json()["detail"]
