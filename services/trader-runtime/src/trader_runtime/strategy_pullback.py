"""Replay the bounded setup from closed bars, without changing entry stop levels."""
from __future__ import annotations

from .strategy_rules import Operand, StrategyRuleError, _condition, indicator_values


def evaluate_pullback(plan, bars, context=None):
    setup = plan.pullback
    # Include a preceding trend value and the complete possible setup history.
    if len(bars) < setup.trendPeriod + setup.maxBars + 1:
        raise StrategyRuleError("strategy_bars_insufficient", "回调策略需要趋势均线及完整回调窗口的已收盘K线")
    trend, touch, guard = [indicator_values(bars, Operand(indicator="ma", period=p))
                           for p in (setup.trendPeriod, setup.touchPeriod, setup.guardPeriod)]
    entries, structures, details = {}, {}, []
    for name, sign in (("entryLong", 1), ("entryShort", -1)):
        group = getattr(plan.rules, name)
        entries[name] = False
        if group is None:
            continue
        start = None
        low = high = None
        state = "等待首次触及回调均线"
        for i in range(setup.trendPeriod, len(bars)):
            bar, previous = bars[i], bars[i - 1]
            close = float(bar["close"])
            valid = sign * (close - trend[i]) > 0 and sign * (trend[i] - trend[i - 1]) > 0
            valid = valid and sign * (close - guard[i]) >= 0
            if not valid:
                start = None
                state = "趋势或保护均线条件不满足"
                continue
            if start is not None and i - start >= setup.maxBars:
                start = None
                state = "回调已超时，等待新的触及"
            # A fresh approach from outside prevents resetting a stale setup
            # merely because price remains on the wrong side of the touch MA.
            field = "low" if sign == 1 else "high"
            touched = sign * (float(previous[field]) - touch[i - 1]) > 0 and sign * (float(bar[field]) - touch[i]) <= 0
            if start is None and touched:
                start, low, high = i, float(bar["low"]), float(bar["high"])
            if start is None:
                continue
            low, high = min(low, float(bar["low"])), max(high, float(bar["high"]))
            state = f"回调第 {i - start + 1} / {setup.maxBars} 根，等待入场条件"
            try:
                checks = [_condition(c, bars[:i + 1], context) for c in group.conditions]
            except StrategyRuleError as error:
                if error.code != "strategy_bars_insufficient" or i == len(bars) - 1:
                    raise
                continue
            start_time = int(bars[start]["datetime"])
            # The user's event window is relative to the decision bar. The
            # touch starts structural level tracking, not a second event clock.
            passed = all(bool(c["hits"]) for c in checks)
            if not passed:
                continue
            if i == len(bars) - 1:
                entries[name] = True
                structures[name] = {"startTime": str(start_time), "low": low, "high": high,
                                    "signalTime": str(bar["datetime"])}
            # One signal consumes this setup, even when risk/position gates
            # skip the order. Later bars cannot reuse the same cross window.
            start = None
            state = "回调入场条件已满足，本次机会已使用"
        details.append({"group": name, "label": "顺势回调结构", "status": "passed" if entries[name] else "waiting",
                        "detail": state, "hits": [int(bars[-1]["datetime"])] if entries[name] else [],
                        "time": int(bars[-1]["datetime"]) if entries[name] else None})
    return entries, structures, details
