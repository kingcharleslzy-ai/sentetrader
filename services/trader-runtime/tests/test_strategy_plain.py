"""说人话 (2026-09-25): templates carry a plain sentence, level, holding and per-parameter help (the
load check requires them), ``describe_plan`` says any plan in one sentence without a model, and the
策略术语表 help topic explains the blocks for the help card and the AI's help reads."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime import strategy_templates
from trader_runtime.app_help import HELP
from trader_runtime.main import create_app
from trader_runtime.strategy_plain import describe_plan, strategy_summary
from trader_runtime.strategy_rules import StrategyPlan

HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer plain-token"}
# The 策略术语表 text lives in the help catalog data file (named by path: a help-text edit selects this file).
HELP_CATALOG = Path(__file__).resolve().parents[1] / "src/trader_runtime/help/catalog.json"
PERIOD_WORDS = {60: "在 1 分钟图上", 300: "在 5 分钟图上", 900: "在 15 分钟图上", 86400: "在日线图上",
                # 价差模板：头是两条腿的价差，不是单一合约。
                3600: "在 SHFE.rb2701 − SHFE.rb2705 价差的 1 小时图上"}
BEGINNER = {"double_ma", "supertrend", "donchian_turtle", "aberration", "king_keltner", "fairy_four_price", "consecutive_updown"}
INTRADAY = {"dual_thrust", "fairy_four_price", "sky_garden", "r_breaker", "nr7_breakout"}


def cond(left: dict, operator: str, right: dict, **extra) -> dict:
    return {"label": "条件", "left": left, "operator": operator, "right": right, **extra}


def test_every_template_says_itself_in_plain_words():
    library = strategy_templates.templates()
    assert {key for key, item in library.items() if item.level == "新手"} == BEGINNER
    assert {key for key, item in library.items() if item.holding == "日内"} == INTRADAY
    for template in library.values():
        summary = strategy_templates.summary(template)
        assert summary["plainSummary"] == template.plainSummary and summary["level"] in {"新手", "进阶"}
        assert not strategy_templates.JARGON.search(template.plainSummary.replace("K线", ""))
        for param in summary["params"]:
            assert set(param["help"]) == {"what", "up", "down"} and all(param["help"].values()), (template.id, param["key"])
    # Plain labels keep the professional name in brackets.
    assert library["dual_thrust"].param("k1").label == "向上突破幅度（K1）"


@pytest.mark.parametrize("change, reason", [
    (lambda raw: raw.update(holding="会隔夜"), "holding"),
    (lambda raw: raw.update(plainSummary="价格突破今开 + K1×Range 就追"), "plainSummary"),
    (lambda raw: raw.pop("level"), "level"),
    (lambda raw: raw["params"][0].pop("help"), "help"),
])
def test_the_load_check_requires_the_plain_fields(tmp_path, monkeypatch, change, reason):
    raw = json.loads((strategy_templates.TEMPLATE_DIR / "dual_thrust.json").read_text(encoding="utf-8"))
    change(raw)
    (tmp_path / "dual_thrust.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(strategy_templates, "TEMPLATE_DIR", tmp_path)
    strategy_templates.templates.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=reason):
            strategy_templates.templates()
    finally:
        monkeypatch.undo()
        strategy_templates.templates.cache_clear()
    assert len(strategy_templates.templates()) == 22


def test_describe_plan_covers_every_template():
    for key, template in strategy_templates.templates().items():
        plan, _ = strategy_templates.build_plan(template)
        text = describe_plan(plan)
        assert text.startswith(PERIOD_WORDS[plan.timeframeSeconds]) and text.endswith("。"), key
        assert "就开多" in text and ("反过来就开空" in text or "就开空" in text), key
        assert "止损" in text and ("每次" in text or "手数按最近波动自动算" in text), key
        assert ("收盘前" in text) == (key in INTRADAY) and ("持仓可以过夜" in text) == (key not in INTRADAY), key
        assert "{" not in text and "  " not in text, key
    dual = describe_plan(strategy_templates.build_plan(strategy_templates.templates()["dual_thrust"])[0], market="螺纹")
    assert dual == ("在螺纹 1 分钟图上，收盘价高于今天开盘价加 0.2 倍前 5 日振幅就开多，反过来就开空，每个方向每天最多开 1 次；"
                    "止损约 2 倍平均波动（ATR14）；多单在收盘价低于今天开盘价减 0.2 倍前 5 日振幅时平仓，空单反过来；"
                    "收盘前 5 分钟全部平仓；每次 1 手。")
    # Unequal K1 / K2 are not a mirror: both sides are said.
    lopsided = describe_plan(strategy_templates.build_plan(strategy_templates.templates()["dual_thrust"], {"k2": 0.3})[0])
    assert "减 0.3 倍前 5 日振幅就开空" in lopsided and "反过来就开空" not in lopsided
    # Conditions both sides share are said once (ATR-RSI's volatility gate).
    atr_rsi = describe_plan(strategy_templates.build_plan(strategy_templates.templates()["atr_rsi"])[0])
    assert atr_rsi.count("平均波动（ATR22）高于") == 1 and "RSI5 高于 66 就开多，反过来就开空" in atr_rsi


def test_describe_plan_handwritten_combinations():
    five = {"indicator": "close", "timeframe": 300}
    long_only = StrategyPlan.model_validate({
        "name": "手写", "description": "手写", "direction": "long", "timeframeSeconds": 300, "maxPosition": 3,
        "stopLossTicks": 30, "takeProfitTicks": None,
        "trailingStop": {"mode": "percent", "value": 0.5, "activateAfter": 1},
        "breakeven": {"triggerTicks": 20, "offsetTicks": 2},
        "timeTakeProfit": [{"afterBars": 10, "profitTicks": 40}],
        "entryWindows": [{"start": "09:00", "end": "11:30"}], "allowNightEntries": False, "maxHoldBars": 30,
        "sizing": {"mode": "atr_risk", "riskPercent": 0.5},
        "rules": {"entryLong": {"conditions": [
            cond({"indicator": "ma", "period": 5, "timeframe": 300}, "cross_up", {"indicator": "ma", "period": 20, "timeframe": 300}),
            cond({"indicator": "cci", "timeframe": 300}, "gt", {"indicator": "constant", "value": 100, "timeframe": 300}, consecutive=3)],
            "rearm": [cond({"indicator": "rsi", "timeframe": 300}, "lt", {"indicator": "constant", "value": 50, "timeframe": 300})]},
            "exitLong": {"conditions": [cond({"indicator": "oi_change", "timeframe": 300}, "falling", {"indicator": "constant", "value": 3})]}}})
    text = describe_plan(long_only, market="螺纹")
    for piece in ("在螺纹 5 分钟图上", "5 根均线上穿 20 根均线且连续 3 根 CCI20 高于 100 就开多", "只做多", "只在 09:00–11:30 开仓",
                  "夜盘不开新仓", "开多后要等 RSI14 低于 50 才再开多", "止损 30 跳", "赚到 1% 后，从持仓后的最高 / 最低价回撤 0.5% 就离场",
                  "赚到 20 跳后止损挪到开仓价盈利一侧 2 跳", "持仓满 10 根后赚到 40 跳就止盈", "多单在持仓量较 1 根前的变化比 3 根前低时平仓",
                  "持仓满 30 根K线就平仓", "持仓可以过夜", "手数按最近波动自动算（每笔按权益的 0.5% 定风险），最多 3 手"):
        assert piece in text, piece
    assert "开空" not in text

    any_join = StrategyPlan.model_validate({
        "name": "任一", "description": "任一", "direction": "both", "timeframeSeconds": 0, "stopLossTicks": 15,
        "takeProfitTicks": 60, "closeBeforeEndMinutes": 3, "closeBeforeEndScope": "session",
        "rules": {"entryLong": {"join": "any", "conditions": [
            cond({"indicator": "close"}, "gt", {"indicator": "pivot", "value": 1}),
            cond({"indicator": "oi_quadrant"}, "eq", {"indicator": "constant", "value": 1})]},
            "entryShort": {"conditions": [cond({"indicator": "close"}, "lt", {"indicator": "pivot", "value": -1})]}}})
    text = describe_plan(any_join)
    assert text.startswith("在当前图表周期上，收盘价高于昨日枢轴 R1（反转卖出价）或持仓四象限为“多头增仓”就开多，"
                           "收盘价低于昨日枢轴 S1（反转买入价）就开空")
    assert "止损 15 跳，止盈 60 跳" in text and "每个交易时段收盘前 3 分钟全部平仓" in text and "持仓可以过夜" not in text

    aligned = StrategyPlan.model_validate({
        "name": "对齐", "description": "对齐", "direction": "short", "timeframeSeconds": 300, "stopLookbackBars": 5,
        "perTradeRisk": 800, "takeProfitTicks": None,
        "rules": {"entryShort": {"alignmentConditions": [cond(five, "lt", {"indicator": "ema", "period": 60, "timeframe": 300})],
                                 "conditions": [cond(five, "lt", {"indicator": "donchian_low", "timeframe": 300})]}}})
    text = describe_plan(aligned)
    assert "在收盘价低于 60 根指数均线的前提下，收盘价低于前 20 根最低价就开空" in text and "只做空" in text
    assert "止损放在最近 5 根K线的最低 / 最高价外一跳" in text and "每次 1 手，单笔预算 800 元" in text


def test_a_foreign_strategy_states_its_budget_in_dollars():
    """外盘盘点（2026-10-21）：外盘策略挂在美元子账户上，单笔预算与当天亏损上限按美元记；白话说明以前一律写“元”。"""
    from trader_runtime.strategy_plain import plain_rows
    five = {"indicator": "close", "timeframe": 300}
    plan = StrategyPlan.model_validate({
        "name": "外盘", "description": "外盘", "direction": "short", "timeframeSeconds": 300, "stopLookbackBars": 5,
        "perTradeRisk": 300, "maxLoss": 900, "takeProfitTicks": None,
        "rules": {"entryShort": {"conditions": [cond(five, "lt", {"indicator": "donchian_low", "timeframe": 300})]}}})
    gold = {"plan": plan.model_dump(), "instrumentScope": {"mode": "selected", "symbols": ["CME.GCE"]}}
    assert "单笔预算 300 美元" in strategy_summary(gold)
    assert "单笔预算 300 美元" in strategy_summary({"plan": plan.model_dump()}, symbol="KQD.m@COMEX.GC")
    risk = next(row for row in plain_rows(plan, currency="USD") if row["key"] == "risk")
    assert "当天亏满 900 美元就停止开新仓" in "".join(part["text"] for part in risk["parts"])
    rebar = {"plan": plan.model_dump(), "instrumentScope": {"mode": "selected", "symbols": ["SHFE.rb2701"]}}
    assert "单笔预算 300 元" in strategy_summary(rebar)


def test_strategy_summary_prefers_the_template_sentence():
    template = strategy_templates.templates()["supertrend"]
    plan, values = strategy_templates.build_plan(template)
    definition = {"plan": plan.model_dump(), "templateSource": strategy_templates.template_source(template, values)}
    assert strategy_summary(definition) == template.plainSummary
    modified = {**definition, "templateSource": {**definition["templateSource"], "modified": True}}
    assert strategy_summary(modified) == describe_plan(plan)
    assert strategy_summary({"plan": plan.model_dump()}) == describe_plan(plan)
    assert strategy_summary({"plan": {"name": "坏"}}) is None


async def test_the_glossary_is_a_help_topic(tmp_path):
    glossary = HELP["strategy-glossary"]
    assert json.loads(HELP_CATALOG.read_text(encoding="utf-8"))["topics"]["strategy-glossary"] == glossary
    text = "".join(glossary["detail"])
    assert glossary["title"] == "策略术语表"
    for term in ("ATR", "布林带", "肯特纳", "唐奇安", "超级趋势", "RSI", "枢轴", "持仓四象限", "追踪止损", "吊灯", "保本",
                 "分段止盈", "复位", "连续 N 根", "开仓时段", "按 ATR 定手数", "新手", "日内"):
        assert term in text, term
    app = create_app(tmp_path / "plain.sqlite3", runtime_state_root=tmp_path, agent_mode="fake", local_token="plain-token")
    async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
        topics = (await client.get("/api/app/help")).json()["topics"]
        assert topics["strategy-glossary"]["title"] == "策略术语表"
        listed = (await client.get("/api/strategy-templates")).json()["items"]
        assert all(item["plainSummary"] and item["level"] and item["holding"] and all(p["help"]["what"] for p in item["params"])
                   for item in listed)


def test_a_designed_plan_with_one_side_only_is_that_side_and_a_ceiling_stop_reads_as_none():
    """保护：用户答“只做多头”，模型却留 direction=both 只写开多——按写出的规则定方向，回测不再报
    “缺少开空条件”；5000 跳（上限）止损按“不设固定止损”说人话（10-08 线上一句话建策略）。"""
    import json as _json

    from trader_runtime.strategy_designer import _finalize_plan
    plan = strategy_templates.build_plan(strategy_templates.templates()["double_ma"])[0].model_dump()
    raw = {**plan, "direction": "both", "stopLossTicks": 5000, "atrStop": None, "stopLookbackBars": None, "pullback": None,
           "rules": {**plan["rules"], "entryShort": None, "exitShort": None}}
    finished = _finalize_plan(_json.dumps(raw, ensure_ascii=False), "突破前高做多", {})
    assert finished["direction"] == "long"
    StrategyPlan.model_validate(finished).check_executable()
    assert "不设固定止损" in describe_plan(StrategyPlan.model_validate(finished))
    # A direction the user set stays as set.
    assert _finalize_plan(_json.dumps(raw, ensure_ascii=False), "x", {"direction": "both"})["direction"] == "both"


def test_a_daily_loss_cap_is_only_the_users_own():
    """保护 2026-10-08 用户决定 A：用户没说当日亏损上限，模型写的 maxLoss 不保存（线上一句话建策略
    模型自己写了 5000，页面说“当天亏满 5000 元就停止开新仓”）；原话或澄清回答里说了才保留，
    constraints 给的值（含 null 清空）为准，改一版沿用原草稿的值。"""
    import json as _json

    from trader_runtime.strategy_designer import _finalize_plan
    plan = strategy_templates.build_plan(strategy_templates.templates()["double_ma"])[0].model_dump()
    raw = _json.dumps({**plan, "maxLoss": 5000}, ensure_ascii=False)
    unasked = _finalize_plan(raw, "均线金叉做多，死叉平仓", {})
    assert unasked["maxLoss"] is None
    from trader_runtime.strategy_plain import plain_rows

    def risk(item: dict) -> str:
        return "".join(part["text"] for row in plain_rows(StrategyPlan.model_validate(item)) if row["key"] == "risk"
                       for part in row["parts"])
    assert "亏满" not in risk(unasked)
    for words in ("均线金叉做多，一天最多亏 2000 就停", "金叉开多，当日亏损到 2000 元不再开仓", "金叉开多，亏满2000停手"):
        assert _finalize_plan(_json.dumps({**plan, "maxLoss": 2000}, ensure_ascii=False), words, {})["maxLoss"] == 2000
    answered = {"clarificationAnswers": [{"question": "一天亏多少就停止开新仓？", "answer": "2000 元"}]}
    assert _finalize_plan(_json.dumps({**plan, "maxLoss": 2000}, ensure_ascii=False), "金叉开多", answered)["maxLoss"] == 2000
    assert _finalize_plan(raw, "金叉开多", {"maxLoss": 3000})["maxLoss"] == 3000
    assert _finalize_plan(raw, "金叉开多，最多亏 5000", {"maxLoss": None})["maxLoss"] is None
    assert _finalize_plan(raw, "金叉开多，止损改成 30 跳", {"previousPlan": {**plan, "maxLoss": 1500}})["maxLoss"] == 1500
    assert _finalize_plan(raw, "金叉开多，止损改成 30 跳", {"previousPlan": {**plan, "maxLoss": None}})["maxLoss"] is None
    assert "当天亏满 2000 元就停止开新仓" in risk({**plan, "maxLoss": 2000})
