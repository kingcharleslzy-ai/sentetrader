"""策略一句话总结 (说人话, 2026-09-25).

``describe_plan`` turns any ``StrategyPlan`` into one plain sentence — period, when it opens, the
stop, how it exits, how long it holds and how many lots — by fixed rules, never a model. Every
building block has words here; when the short side is the long side reversed (operators flipped,
upper and lower lines swapped) it says “反过来就开空” instead of repeating it, and conditions both
sides share are said once. ``strategy_summary`` is what 详情 shows above the five sections: a
template-built strategy (not changed beyond its parameters) uses the template's ``plainSummary``,
everything else ``describe_plan``. It only describes; it never changes a plan or a judgement.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from .instruments import money_unit, symbol_currency
from .strategy_rules import (
    EXIT_ATR_PERIOD,
    OI_QUADRANTS,
    PIVOT_LEVELS,
    Condition,
    OperandBase,
    RuleGroup,
    StrategyPlan,
    rearm_conditions,
)
from .strategy_spread import spread_words

PERIODS = {60: "1分钟", 180: "3分钟", 300: "5分钟", 900: "15分钟", 3600: "1小时", 86400: "日线"}
OPERATOR_WORDS = {"gt": "高于", "gte": "不低于", "lt": "低于", "lte": "不高于", "cross_up": "上穿", "cross_down": "下穿"}
# The short side of a long rule: operators flip, upper / lower lines swap, signed levels change sign.
MIRROR_OPERATOR = {"gt": "lt", "lt": "gt", "gte": "lte", "lte": "gte", "cross_up": "cross_down",
                   "cross_down": "cross_up", "rising": "falling", "falling": "rising"}
_PAIRS = (("donchian_high", "donchian_low"), ("boll_up", "boll_down"), ("keltner_up", "keltner_down"),
          ("prev_high", "prev_low"), ("opening_range_high", "opening_range_low"), ("session_high", "session_low"), ("high", "low"),
          ("upper_shadow", "lower_shadow"))
MIRROR_INDICATOR = {**dict(_PAIRS), **{low: high for high, low in _PAIRS}}
OSCILLATORS = frozenset({"rsi", "kd_k", "kd_d", "kd_j", "mfi"})        # around 50: the mirror of v is 100 − v
SIGNED = frozenset({"cci", "gap_percent", "macd_dif", "macd_dea", "macd_hist", "oi_change", "roc", "trix",
                    "bias", "body"})                                   # −v
# 威廉 %R runs −100~0 around −50: the mirror of v is −100 − v.
MAX_LISTED = 3
# A space where Latin letters / digits meet Chinese (“RSI14 高于 70”), except in “K线”.
_SPACING = re.compile(r"(?<=[A-Za-z0-9%])(?<!K)(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9+])(?!K线)")


def _n(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _spaced(text: str) -> str:
    return _SPACING.sub(" ", text)


def operand_words(operand: OperandBase) -> str:
    n, indicator = operand.period, operand.indicator
    words = {
        "constant": _n(operand.value), "close": "收盘价", "ma": f"{n} 根均线", "ema": f"{n} 根指数均线",
        "macd_dif": "MACD 快线", "macd_dea": "MACD 慢线", "macd_hist": "MACD 柱", "kd_k": "KD 的 K 值",
        "kd_d": "KD 的 D 值", "volume": "成交量", "volume_ratio": "放量倍数（比前 20 根均量）",
        "atr": f"平均波动（ATR{n}）", "donchian_high": f"前 {n} 根最高价", "donchian_low": f"前 {n} 根最低价",
        "boll_up": "布林上轨", "boll_mid": "布林中轨", "boll_down": "布林下轨",
        "keltner_up": "肯特纳上轨", "keltner_mid": "肯特纳中轨", "keltner_down": "肯特纳下轨",
        "rsi": f"RSI{n}", "supertrend": "超级趋势线", "prev_high": "昨日最高价", "prev_low": "昨日最低价",
        "prev_close": "昨日收盘价", "today_open": "今天开盘价", "range_nday": f"前 {n} 日振幅",
        "gap_percent": "开盘跳空幅度", "opening_range_high": "开盘第一根K线的最高价" if n == 1 else f"开盘前 {n} 根最高价",
        "opening_range_low": "开盘第一根K线的最低价" if n == 1 else f"开盘前 {n} 根最低价", "cci": f"CCI{n}", "adx": f"趋势强度 ADX{n}",
        "plus_di": f"+DI{n}", "minus_di": f"−DI{n}", "sar": "抛物线 SAR", "session_high": "当日最高价",
        "session_low": "当日最低价", "oi_change": f"持仓量较 {n} 根前的变化", "oi_quadrant": "持仓四象限",
        "roc": f"{n} 根涨跌幅", "kd_j": "KDJ 的 J 值", "trix": f"TRIX{n}", "high": "最高价", "low": "最低价",
        "open": "开盘价", "williams_r": f"威廉指标 %R{n}", "bias": f"{n} 根乖离率", "mfi": f"资金流量 MFI{n}",
        "bbi": "多空指数 BBI", "cmi": f"{n} 根震荡指数 CMI", "bar_range": "K线振幅", "body": "K线实体（收盘减开盘）",
        "upper_shadow": "上影线", "lower_shadow": "下影线", "vwap": "分时均价",
        "pivot": "昨日" + PIVOT_LEVELS.get(int(operand.value), "枢轴") if float(operand.value).is_integer() else "昨日枢轴",
    }
    text = words.get(indicator, indicator)
    if operand.average:
        text += f"的 {operand.average} 根均值"
    if operand.highest or operand.lowest:
        text = f"最近 {operand.highest or operand.lowest} 根{text}的{'最高' if operand.highest else '最低'}值"
    offset = getattr(operand, "offset", None)
    if offset is not None:
        term = f"前 {offset.period} 日振幅" if offset.indicator == "range_nday" else f"平均波动（ATR{offset.period}）"
        text += f"{'加' if offset.factor > 0 else '减'} {_n(abs(offset.factor))} 倍{term}"
    if operand.barsAgo:
        text = ("上一根K线的" if operand.barsAgo == 1 else f"{operand.barsAgo} 根前的") + text
    return text


def condition_words(condition: Condition, execution: int = 0) -> str:
    left = operand_words(condition.left)
    operator = condition.operator
    if operator in {"rising", "falling"}:
        body = f"{left}比 {_n(condition.right.value)} 根前{'高' if operator == 'rising' else '低'}"
    elif operator == "eq":
        body = f"{left}为“{OI_QUADRANTS.get(int(condition.right.value), _n(condition.right.value))}”"
    else:
        right = operand_words(condition.right)
        if condition.left.indicator in {"gap_percent", "roc", "bias"} and condition.right.indicator == "constant":
            right += "%"
        body = left + OPERATOR_WORDS[operator] + right
    if condition.consecutive > 1:
        body = f"连续 {condition.consecutive} 根{body}"
    if condition.minCount > 1:
        body += f"（最近 {condition.withinBars} 根里至少 {condition.minCount} 根）"
    elif condition.withinBars > 1:
        body += f"（最近 {condition.withinBars} 根内出现即可）"
    clock = condition.left.timeframe
    if clock and execution and clock != execution:
        body = f"{PERIODS.get(clock, '')}图上{body}"
    return body


def _listing(conditions: list[Condition], join: str, execution: int, sequence: bool = False) -> str:
    texts = [condition_words(item, execution) for item in conditions[:MAX_LISTED]]
    if sequence:
        text = "先" + "、再".join(texts)
    else:
        text = ("且" if join == "all" else "或").join(texts)
    if len(conditions) > MAX_LISTED:
        text += f"等 {len(conditions)} 个条件"
    return text


def _key(condition: Condition) -> dict[str, Any]:
    return condition.model_dump(exclude={"label"})


def _mirror(condition: Condition) -> dict[str, Any] | None:
    """The long rule's short counterpart, or None when it has no unambiguous reverse."""
    operator = MIRROR_OPERATOR.get(condition.operator)
    if operator is None:
        return None
    data = _key(condition)
    data["operator"] = operator
    for side in ("left", "right"):
        operand = data[side]
        operand["indicator"] = MIRROR_INDICATOR.get(operand["indicator"], operand["indicator"])
        operand["highest"], operand["lowest"] = operand.get("lowest"), operand.get("highest")
        if operand["indicator"] == "pivot":
            operand["value"] = -operand["value"] or 0.0
        if operand.get("offset"):
            operand["offset"]["factor"] = -operand["offset"]["factor"]
    right = data["right"]
    if right["indicator"] == "constant" and operator not in {"rising", "falling"}:
        left = condition.left.indicator
        if left in OSCILLATORS:
            right["value"] = 100 - right["value"]
        elif left == "williams_r":
            right["value"] = -100 - right["value"]
        elif left in SIGNED:
            right["value"] = -right["value"] or 0.0
        else:
            return None                          # a price level: its reverse is unknown
    return data


def _mirrors(longs: list[Condition], shorts: list[Condition]) -> bool:
    return bool(longs) and len(longs) == len(shorts) and all(
        _mirror(a) == _key(b) for a, b in zip(longs, shorts, strict=True))


def _plain_group(group: RuleGroup | None) -> bool:
    return (group is not None and group.join == "all" and not group.sequence and not group.alignmentConditions
            and not group.branches and getattr(group, "trigger", None) is None)


def _group_text(group: RuleGroup, execution: int, long: bool | None = None) -> str:
    parts = [_listing(group.conditions, group.join, execution, group.sequence)] if group.conditions else []
    parts += [f"（{_listing(branch.conditions, branch.join, execution)}）" if len(branch.conditions) > 1
              else _listing(branch.conditions, branch.join, execution) for branch in group.branches]
    text = ("且" if group.join == "all" else "或").join(parts)
    if group.alignmentConditions:
        text = f"在{_listing(group.alignmentConditions, 'all', execution)}的前提下，{text}"
    trigger = getattr(group, "trigger", None)
    if trigger is not None and long is not None:
        rises = long == (trigger.kind == "stop")
        text += f"时，盘中价格{'涨到' if rises else '跌到'}{operand_words(trigger.price)}"
    return text


def _pair(long: RuleGroup | None, short: RuleGroup | None, execution: int, *, entry: bool) -> str:
    """Both sides of a rule pair in one clause: shared conditions once, a mirrored side as “反过来”."""
    long_verb, short_verb = ("就开多", "就开空") if entry else ("时平仓", "时平仓")
    if long is None or short is None:
        side = long or short
        if side is None:
            return ""
        if entry:
            return _group_text(side, execution, long is not None) + (long_verb if long else short_verb)
        return ("多单在" if long else "空单在") + _group_text(side, execution) + "时平仓"
    prefix = ""
    longs, shorts = long.conditions, short.conditions
    long_trigger, short_trigger = getattr(long, "trigger", None), getattr(short, "trigger", None)
    if (entry and long_trigger is not None and short_trigger is not None
            and _plain_group(long.model_copy(update={"trigger": None}))
            and _plain_group(short.model_copy(update={"trigger": None}))
            and [_key(item) for item in longs] == [_key(item) for item in shorts]):
        # 盘中触价 on the same setup both ways (NR7): the setup once, then each side's level.
        words = [f"{'涨到' if rises else '跌到'}{operand_words(trigger.price)}就{verb}" for trigger, rises, verb in (
            (long_trigger, long_trigger.kind == "stop", "开多"), (short_trigger, short_trigger.kind != "stop", "开空"))]
        return f"{_listing(longs, 'all', execution)}时，盘中价格" + "、".join(words)
    if _plain_group(long) and _plain_group(short):
        shared = [item for item in longs if any(_key(item) == _key(other) for other in shorts)]
        rest_long = [item for item in longs if item not in shared]
        rest_short = [item for item in shorts if not any(_key(item) == _key(other) for other in shared)]
        if shared and rest_long and rest_short:
            prefix, longs, shorts = f"在{_listing(shared, 'all', execution)}时，", rest_long, rest_short
        if _mirrors(longs, shorts):
            text = _listing(longs, "all", execution)
            return prefix + (f"{text}就开多，反过来就开空" if entry else f"多单在{text}时平仓，空单反过来")
    if prefix:
        long_text, short_text = _listing(longs, "all", execution), _listing(shorts, "all", execution)
    else:
        long_text, short_text = _group_text(long, execution, True), _group_text(short, execution, False)
    if entry:
        return f"{prefix}{long_text}就开多，{short_text}就开空"
    return f"{prefix}多单在{long_text}时平仓，空单在{short_text}时平仓"


def _stop_words(plan: StrategyPlan) -> list[str]:
    words = []
    if plan.spread is not None:
        # 价差: the protective stop / take live on the spread series (spread points, judged on the
        # close spread); the plan's stopLossTicks / takeProfitTicks do not apply to a spread.
        if plan.spread.stopLoss is not None:
            words.append(f"价差浮亏 {_n(plan.spread.stopLoss)} 点止损")
        if plan.spread.takeProfit is not None:
            words.append(f"价差浮盈 {_n(plan.spread.takeProfit)} 点止盈")
        return words
    if plan.atrStop:
        words.append(f"止损约 {_n(plan.atrStop.multiple)} 倍平均波动（ATR{plan.atrStop.period}）")
    elif plan.stopLookbackBars:
        words.append(f"止损放在最近 {plan.stopLookbackBars} 根K线的最低 / 最高价外一跳")
    elif plan.pullback:
        words.append("止损放在回调低点 / 高点外一跳")
    else:
        # 5000 跳 is the schema's ceiling: the model's way of saying "no fixed stop" (10-08 线上：用户选了不加固定止损).
        words.append("不设固定止损，只按平仓条件出场（留 5000 跳极远兜底）" if plan.stopLossTicks >= 5000
                     else f"止损 {plan.stopLossTicks} 跳")
    if plan.takeProfitTicks is not None:
        words.append(f"止盈 {plan.takeProfitTicks} 跳")
    trailing = plan.trailingStop
    if trailing:
        unit = {"percent": "%", "ticks": " 跳", "atr_chandelier": f" 倍平均波动（ATR{trailing.atrPeriod}）"}[trailing.mode]
        text = f"从持仓后的最高 / 最低价回撤 {_n(trailing.value)}{unit}就离场"
        if trailing.activateAfter:
            text = f"赚到 {_n(trailing.activateAfter)}{unit}后，" + text
        words.append(text)
    if plan.breakeven:
        trigger = (f"{plan.breakeven.triggerTicks} 跳" if plan.breakeven.triggerTicks is not None
                   else f"{_n(plan.breakeven.triggerAtr or 0)} 倍平均波动（ATR{EXIT_ATR_PERIOD}）")
        place = f"开仓价盈利一侧 {plan.breakeven.offsetTicks} 跳" if plan.breakeven.offsetTicks else "开仓价"
        words.append(f"赚到 {trigger}后止损挪到{place}")
    if plan.timeTakeProfit:
        tiers = "、".join(
            f"持仓满 {tier.afterBars} 根后赚到 "
            + (f"{tier.profitTicks} 跳" if tier.profitTicks is not None else f"{_n(tier.profitAtr or 0)} 倍平均波动")
            for tier in sorted(plan.timeTakeProfit, key=lambda item: item.afterBars))
        words.append(f"{tiers}就止盈")
    if plan.scaleOut:
        words.append("、".join(
            f"赚 {tier.profitTicks} 跳先平 {tier.percent}%" if tier.profitTicks is not None
            else f"赚 {_n(tier.profitAtr or 0)} 倍平均波动先平 {tier.percent}%" for tier in plan.scaleOut))
    if plan.maxDrawdownPercent is not None:
        words.append(f"本轮净值从高点回撤 {_n(plan.maxDrawdownPercent)}% 就停机"
                     "（撤销未成交开仓单、平掉本策略持仓，重置回撤后才能再开）")
    return words


def definition_currency(definition: dict[str, Any], symbol: str | None = None) -> str:
    """The currency a strategy's money (单笔预算、当天亏损上限) is in: its contract's (`symbol`, else the first of
    its instrument scope) — a foreign strategy's budget is dollars (外盘三期), a domestic one's yuan."""
    scope = definition.get("instrumentScope") or {}
    symbols = [symbol] if symbol else [*(scope.get("symbols") or []), definition.get("symbol")]
    return symbol_currency(next((value for value in symbols if value), ""))


def _plan_parts(plan: StrategyPlan, *, market: str | None = None, currency: str = "CNY") -> dict[str, Any]:
    """The pieces both the one sentence and the four rule rows are made of."""
    execution = plan.timeframeSeconds
    spread = plan.spread
    period = PERIODS.get(execution)
    if spread is not None:
        head = f"在{market or ''}{spread_words(spread)}价差的{period or '当前图表周期'}图上"
    else:
        head = f"在{market or ''}{period}图上" if period else f"在{market or ''}当前图表周期上"
    rules = plan.rules
    long = rules.entryLong if plan.direction != "short" else None
    short = rules.entryShort if plan.direction != "long" else None
    entry = [_pair(long, short, execution, entry=True) or "还没有开仓条件"]
    if plan.direction == "long":
        entry.append("只做多")
    elif plan.direction == "short":
        entry.append("只做空")
    if plan.pullback:
        entry.append(f"顺着 {plan.pullback.trendPeriod} 根均线的方向，等价格回调到 {plan.pullback.touchPeriod} 根均线附近再入场")
    if plan.entryWindows:
        entry.append("只在 " + "、".join(f"{window.start}–{window.end}" for window in plan.entryWindows) + " 开仓")
    if not plan.allowNightEntries:
        entry.append("夜盘不开新仓")
    if plan.maxEntriesPerDirection:
        entry.append(f"每个方向每天最多开 {plan.maxEntriesPerDirection} 次")
    for group, side in ((long, "多"), (short, "空")):
        rearm = rearm_conditions(group)
        if rearm:
            entry.append(f"开{side}后要等{_listing(rearm, 'all', execution)}才再开{side}")
    if plan.pyramiding:
        pyramid = plan.pyramiding
        distance = (None if pyramid.trigger == "signal" else
                    f"{_n(pyramid.step)} 跳" if pyramid.trigger == "profit_ticks" else
                    f"{_n(pyramid.step)} 倍平均波动（ATR{pyramid.atrPeriod}）")
        add_text = (f"收盘比上次成交价多赚 {distance}就顺着加仓" if distance
                    else "同向信号再成立且收盘更优就顺势加仓")
        entry.append(f"做对了不一次做完：{add_text}，最多 {pyramid.maxAdds} 次"
                     + ("，止损跟着最近一次成交收紧" if pyramid.moveStop else ""))
    exits = _pair(rules.exitLong if plan.direction != "short" else None,
                  rules.exitShort if plan.direction != "long" else None, execution, entry=False)
    timing = []
    if plan.closeBeforeEndMinutes:
        where = "每个交易时段" if plan.closeBeforeEndScope == "session" else ""
        timing.append(f"{where}收盘前 {plan.closeBeforeEndMinutes} 分钟全部平仓")
    if plan.maxHoldBars:
        timing.append(f"持仓满 {plan.maxHoldBars} 根K线就平仓")
    if not plan.closeBeforeEndMinutes:
        timing.append("持仓可以过夜")
    if plan.sizing and plan.sizing.mode == "equity_percent":
        size = (f"手数按资金比例算：拿权益的 {_n(float(plan.sizing.equityPercent or 0))}% 做本策略的保证金预算，"
                "扣掉已持有和已挂单的手数后按每手保证金向下取整，开仓前再核对一遍")
        if plan.sizing.maxLots is not None:
            size += f"，总手数（持仓＋挂单）最多 {_n(plan.sizing.maxLots)} 手"
    elif plan.sizing and plan.sizing.mode == "atr_risk":
        size = (f"手数按最近波动自动算（每笔按权益的 {_n(plan.sizing.riskPercent or 0)}% 定风险），"
                f"最多 {plan.maxPosition} 手")
    elif spread is not None:
        leg_a, leg_b = spread.legs
        size = (f"每次最多 {plan.maxPosition} 份价差，1 份 = 买{leg_a.symbol} {leg_a.lots} 手 + "
                f"卖{leg_b.symbol} {leg_b.lots} 手（做空价差反过来）")
    else:
        size = f"每次 {plan.maxPosition} 手"
    if plan.pyramiding:
        pyramid = plan.pyramiding
        reserve = max(1, plan.maxPosition - (pyramid.volume or 1) * pyramid.maxAdds)
        size += (f"；首笔先开 {_n(reserve)} 手给加仓留空间（最大持仓 {_n(plan.maxPosition)} − 每次加仓手数 × "
                 f"最多 {_n(pyramid.maxAdds)} 次，最少 1 手；最大持仓 4、最多加 3 次时首笔 1 手、每次加 1 手），"
                 "做对了逐步加满")
    if plan.perTradeRisk is not None:
        size += f"，单笔预算 {_n(plan.perTradeRisk)} {money_unit(currency)}"
    return {"head": head, "entry": entry, "stops": _stop_words(plan), "exits": exits, "timing": timing, "size": size}


def describe_plan(plan: StrategyPlan, *, market: str | None = None, currency: str = "CNY") -> str:
    """One plain sentence for the whole plan (e.g. “在螺纹1分钟图上，…，收盘前 5 分钟全部平仓。”)."""
    parts = _plan_parts(plan, market=market, currency=currency)
    clauses = ["，".join(parts["entry"]), "，".join(parts["stops"])]
    if parts["exits"]:
        clauses.append(parts["exits"])
    clauses.append("，".join(parts["timing"]))
    clauses.append(parts["size"])
    return _spaced(f"{parts['head']}，" + "；".join(clauses) + "。")


# -- 规则四行 (策略页改版第八批, 2026-10-08) ----------------------------------------------------------------
#
# The result page writes a strategy as four plain rows — 开仓 / 平仓 / 止损 / 风险 — with every
# professional name moved out of the text into a “?” beside the plain word (ATR → 平均波动, the
# parameter letter K1 → “2 成振幅”). A part is {text} or {text, term, help}: `text` is what the row
# says, `term` the professional name and `help` one sentence on what it is.

TERMS: tuple[tuple[re.Pattern[str], str, str, str], ...] = tuple((re.compile(pattern), plain, term, help_text) for pattern, plain, term, help_text in (
    (r"平均波动（ATR(\d+)）", "平均波动", "ATR{0}", "ATR（平均真实波幅）：最近 {0} 根K线平均每根波动多大。用它的倍数定止损，波动大的品种止损自动放宽。"),
    (r"(\d+) 根指数均线", "{0} 根均线（近的权重大）", "EMA{0}", "EMA（指数移动平均）：越近的K线权重越大的均线，比普通均线反应快。"),
    (r"RSI(\d+)", "涨跌强弱", "RSI{0}", "RSI（相对强弱指标）：0–100，最近 {0} 根里涨得越多越高；70 以上算涨过头，30 以下算跌过头。"),
    (r"CCI(\d+)", "偏离程度", "CCI{0}", "CCI（顺势指标）：价格偏离近 {0} 根平均水平有多远，100 以上算偏高，−100 以下算偏低。"),
    (r"趋势强度 ADX(\d+)", "趋势强度", "ADX{0}", "ADX（平均趋向指数）：只看趋势强不强、不看方向，25 以上一般算有趋势。"),
    (r"\+DI(\d+)", "上涨力量", "+DI{0}", "+DI：近 {0} 根里向上推的力量。"),
    (r"−DI(\d+)", "下跌力量", "−DI{0}", "−DI：近 {0} 根里向下压的力量。"),
    (r"MACD 快线", "快慢均线之差", "MACD DIF", "MACD 的 DIF：短期均线减长期均线，正的说明短期走得比长期强。"),
    (r"MACD 慢线", "快慢差的均线", "MACD DEA", "MACD 的 DEA：DIF 的均线，DIF 上穿它常被看作转强。"),
    (r"MACD 柱", "快慢差的变化", "MACD 柱", "MACD 柱：DIF 减 DEA，由负转正说明在转强。"),
    (r"KDJ 的 J 值", "超买超卖 J 值", "KDJ J", "KDJ（随机指标）的 J 线：比 K、D 更灵敏，常超过 100 或低于 0。"),
    (r"KD 的 K 值", "超买超卖 K 值", "KD K", "KD（随机指标）：0–100，看收盘价在近期高低区间里的位置；80 以上算高位，20 以下算低位。"),
    (r"KD 的 D 值", "超买超卖 D 值", "KD D", "KD 的 D 线：K 值的均线，比 K 慢一些。"),
    (r"布林(上轨|中轨|下轨)", "通道{0}", "布林带 BOLL", "布林带：均线上下各加几倍标准差画出的通道，价格出了通道说明走得比平时远。"),
    (r"肯特纳(上轨|中轨|下轨)", "波动通道{0}", "肯特纳通道", "肯特纳通道：均线上下各加几倍平均波动画出的通道。"),
    (r"超级趋势线", "趋势线", "SuperTrend", "SuperTrend（超级趋势）：按平均波动画在价格下方或上方的跟随线，被价格穿过就算翻转。"),
    (r"抛物线 SAR", "跟随止损线", "SAR", "SAR（抛物线转向）：跟着价格一路收紧的点，被穿过就算转向。"),
    (r"TRIX(\d+)", "三重平滑涨跌", "TRIX{0}", "TRIX：把价格平滑三次后看涨跌，过滤掉小波动。"),
    (r"威廉指标 %R(\d+)", "高低位置", "Williams %R{0}", "威廉 %R：−100 到 0，越接近 0 说明收盘越靠近近 {0} 根的最高价。"),
    (r"资金流量 MFI(\d+)", "资金流向", "MFI{0}", "MFI（资金流量指标）：把成交量算进去的强弱指标，0–100。"),
    (r"多空指数 BBI", "多空均线", "BBI", "BBI（多空指数）：3、6、12、24 根四条均线的平均。"),
    (r"(\d+) 根震荡指数 CMI", "{0} 根震荡程度", "CMI{0}", "CMI（震荡指数）：近 {0} 根走得有多直，越高越像趋势，越低越像来回震荡。"),
    (r"分时均价", "分时均价", "VWAP", "VWAP：当天按成交量加权的平均价。"),
    (r"(\d+) 根乖离率", "{0} 根偏离均线", "BIAS{0}", "乖离率（BIAS）：价格离 {0} 根均线有百分之几。"),
    (r"前 (\d+) 日振幅", "前 {0} 日振幅", "Range（Dual Thrust）", "振幅：前 {0} 天里“最高价 − 最低收盘价”与“最高收盘价 − 最低价”中较大的一个，用来量一天能走多远。"),
))
# “0.2 倍前 5 日振幅” → “2 成前 5 日振幅”: a fraction of a range reads as 成 (tenths).
_TENTHS = re.compile(r"(?<![\d.])0\.(\d) 倍(?=前 \d+ 日振幅)")


def _term_parts(text: str) -> list[dict[str, str]]:
    """`text` split into plain parts and termed parts ({text, term, help}), professional names moved
    out of the visible words."""
    text = _TENTHS.sub(lambda match: f"{match.group(1)} 成", text)
    parts: list[dict[str, str]] = []
    rest = text
    while rest:
        found = None
        for pattern, plain, term, help_text in TERMS:
            match = pattern.search(rest)
            if match and (found is None or match.start() < found[0].start()):
                found = (match, plain, term, help_text)
        if found is None:
            parts.append({"text": rest})
            break
        match, plain, term, help_text = found
        if match.start():
            parts.append({"text": rest[:match.start()]})
        groups = match.groups()
        parts.append({"text": plain.format(*groups), "term": term.format(*groups), "help": help_text.format(*groups)})
        rest = rest[match.end():]
    return [{**part, "text": _spaced(part["text"])} for part in parts if part["text"]]


def plain_rows(plan: StrategyPlan, *, currency: str = "CNY") -> list[dict[str, Any]]:
    """开仓 / 平仓 / 止损 / 风险, each row's text split into parts (see TERMS)."""
    parts = _plan_parts(plan, currency=currency)
    exits = parts["exits"] or "不按规则平仓，只靠止损、止盈和收盘前平仓"
    risk = [parts["size"], f"每天最多开 {plan.maxOrdersPerDay} 次"]
    if plan.maxLoss is not None:
        risk.append(f"当天亏满 {_n(plan.maxLoss)} {money_unit(currency)}就停止开新仓")
    if plan.cooldownBars:
        risk.append(f"平仓后等 {plan.cooldownBars} 根K线再开")
    rows = (("entry", "开仓", "，".join(parts["entry"])),
            ("exit", "平仓", "，".join([exits, *parts["timing"]])),
            ("stop", "止损", "，".join(parts["stops"]) or "不设止损"),
            ("risk", "风险", "，".join(risk)))
    return [{"key": key, "label": label, "parts": _term_parts(text + "。")} for key, label, text in rows]


def strategy_summary(definition: dict[str, Any], *, market: str | None = None, symbol: str | None = None) -> str | None:
    """详情's sentence: the template's plainSummary for a strategy built from a template (and not
    changed beyond its parameters), otherwise describe_plan; None when there is no valid plan."""
    try:
        plan = StrategyPlan.model_validate(definition.get("plan") or {})
    except (ValidationError, ValueError):
        return None
    source = definition.get("templateSource") or {}
    if source.get("templateId") and not source.get("modified"):
        from .strategy_templates import templates
        template = templates().get(str(source["templateId"]))
        if template is not None:
            return template.plainSummary
    return describe_plan(plan, market=market, currency=definition_currency(definition, symbol))
