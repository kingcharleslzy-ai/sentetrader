"""策略实验室共享引擎 (2026-09-30): the rule backtest split 样本内 / 样本外.

This file is also shipped to the browser (``GET /api/strategy-engine/bundle``) and runs there under
Pyodide against the very same sources, so it may import only the standard library, pydantic and the
rule-backtest modules bundled with it — never anything else of the Runtime.

防过拟合: the compared interval is split by time into the first ``SPLIT`` 样本内 (what picks the
winner) and the rest 样本外 (what checks it); a trade that straddles the split counts in neither
side, so out-of-sample prices cannot move the in-sample numbers. ``rank`` marks the best by its
in-sample net and flags decay (out-of-sample average net over in-sample) and overfitting (the
winner loses out of sample).
The browser's numbers are display-only: the server re-ranks whatever it is sent and re-runs the
chosen variant's authoritative backtest before saving it as 我的策略.

``run_rule`` is the browser's entry for a single rule backtest (规则回测本机优先, 2026-10-12): the
same ``run_backtest`` over the setup the server serialised.
"""
from __future__ import annotations

from typing import Any

from .strategy_backtest import BacktestSetup, _sample, run_backtest, run_backtest_with_trades, setup_from_inputs
from .strategy_rules import StrategyPlan, StrategyRuleError

SPLIT = 0.7           # 前 70% 样本内（用来挑），后 30% 样本外（用来检验）
MIN_IS_TRADES = 5     # 样本内至少这么多笔才有资格参与排名
CURVE_POINTS = 60     # 手机版回测结果页的净值曲线（2026-10-06）：每组最多这么多点


def _segment(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """One side of the split: counts, win rate, net, average net and the drawdown of that
    segment's own cumulative net (≥ 0)."""
    wins = sum(1 for trade in trades if trade["net"] > 0)
    net = round(sum(trade["net"] for trade in trades), 2)
    cumulative = peak = drawdown = 0.0
    for trade in trades:
        cumulative += float(trade["net"])
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return {"trades": len(trades), "wins": wins,
            "winRate": round(wins / len(trades) * 100, 1) if trades else None,
            "net": net, "avgNet": round(net / len(trades), 2) if trades else None,
            "maxDrawdown": round(max(drawdown, 0.0), 2)}


def split_metrics(coverage: dict[str, Any], trades: list[dict[str, Any]],
                  ) -> tuple[int, dict[str, Any], dict[str, Any], int]:
    """(split time ns, in-sample metrics, out-of-sample metrics, straddling count) over EVERY closed
    trade — never the display-trimmed ``result["trades"]``, whose last-2000 cut would let later
    out-of-sample trades push in-sample ones out of the statistics. The split sits
    at firstBarTime + SPLIT × (lastBarTime − firstBarTime); a trade counts in-sample only when it
    closed before that moment and out-of-sample only when it opened at or after it. The trade
    straddling the split belongs to neither side — out-of-sample prices can never move the
    in-sample numbers (or the winner they pick)."""
    first, last = int(coverage["firstBarTime"]), int(coverage["lastBarTime"])
    split_time = int(first + SPLIT * (last - first))
    straddling = sum(1 for trade in trades
                     if int(trade["openTime"]) < split_time <= int(trade["closeTime"]))
    return (split_time,
            _segment([trade for trade in trades if int(trade["closeTime"]) < split_time]),
            _segment([trade for trade in trades if int(trade["openTime"]) >= split_time]),
            straddling)


def run_variant(inputs: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    """One variant over the lab's shared ``inputs`` (the browser and the server build the same
    dict). Data that cannot support a backtest comes back as ``{variantId, label, error}``
    instead of raising — one starved variant never hides the others."""
    identifier = str(variant.get("id"))
    label = str(variant.get("label") or identifier)
    try:
        setup = BacktestSetup(
            plan=StrategyPlan.model_validate(variant["plan"]),
            symbol=str(inputs["symbol"]),
            series={int(timeframe): bars for timeframe, bars in (inputs.get("series") or {}).items()},
            price_tick=float(inputs["priceTick"]),
            volume_multiple=float(inputs["volumeMultiple"]),
            fee_schedule=inputs["feeSchedule"],
            initial_balance=float(inputs["initialBalance"]),
            slippage_ticks=int(inputs["slippageTicks"]),
            schedule=inputs.get("schedule"),
            trading_days=inputs.get("tradingDays"),
            margin_per_lot=inputs.get("marginPerLot"),
        )
        result, trades = run_backtest_with_trades(setup)
    except (StrategyRuleError, ValueError, KeyError, TypeError) as error:
        return {"variantId": identifier, "label": label, "error": str(error) or error.__class__.__name__}
    split_time, in_sample, out_sample, straddling = split_metrics(result["coverage"], trades)
    return {"variantId": identifier, "label": label, "summary": result["summary"],
            "coverage": result["coverage"], "inSample": in_sample, "outSample": out_sample,
            "splitTime": split_time, "tradesUsed": len(trades),
            # The equity curve thinned to CURVE_POINTS, drawn with the split on 回测结果页.
            "equityCurve": _sample(result.get("equityCurve") or [], CURVE_POINTS),
            "straddling": straddling,
            # How fees were counted (the same for every variant: one fee schedule per lab) —
            # "未扣手续费，结果偏乐观" must reach the comparison table like it reaches a backtest.
            "feeEstimate": result.get("feeEstimate")}


def run_rule(inputs: dict[str, Any]) -> dict[str, Any]:
    """规则回测本机优先 (2026-10-12): one rule backtest over ``setup_inputs`` (``POST
    /api/strategy-backtests/inputs``) — ``run_backtest`` itself, in the browser's Pyodide or here. The
    server wraps the report (identity, verdict facts, storage) when the browser posts it back."""
    return run_backtest(setup_from_inputs(inputs))


def rank(results: list[dict[str, Any]], tried: int | None = None) -> dict[str, Any]:
    """The comparison table's judgement, recomputed server-side from whatever results arrive:
    ``tried`` is how many variants this comparison asked for (``len(results)`` only when the caller
    does not know better — a variant that arrives without a result is the sender's loss, not a
    smaller comparison); only variants without an error and with at least ``MIN_IS_TRADES``
    in-sample trades qualify, ranked by in-sample net. The best one is flagged ``best`` — only when it
    made money in sample: when no qualified variant did, ``best`` is None and ``noProfit`` is true
    (nothing is recommended, 09-30 owner decision). The best one is
    ``overfit`` when its out-of-sample net is ≤ 0 or its out-of-sample rank among the qualified is
    past the first three. ``decay`` is out-of-sample avgNet over in-sample avgNet (None when the
    in-sample average is ≤ 0 or nothing traded out of sample). Both sides are computed over every
    closed trade, so the backtest report's display cut (last 2000 trades) never reaches the ranking."""
    qualified = [item for item in results if not item.get("error")
                 and (item.get("inSample") or {}).get("trades", 0) >= MIN_IS_TRADES]
    by_in_sample = sorted(qualified, key=lambda item: item["inSample"]["net"], reverse=True)
    by_out_sample = sorted(qualified, key=lambda item: item["outSample"]["net"], reverse=True)
    is_rank = {item["variantId"]: place for place, item in enumerate(by_in_sample, 1)}
    oos_rank = {item["variantId"]: place for place, item in enumerate(by_out_sample, 1)}
    best = by_in_sample[0]["variantId"] if by_in_sample and by_in_sample[0]["inSample"]["net"] > 0 else None
    no_profit = bool(by_in_sample) and best is None
    items = []
    for item in results:
        identifier = item["variantId"]
        entry: dict[str, Any] = {"variantId": identifier, "isRank": is_rank.get(identifier),
                                 "oosRank": oos_rank.get(identifier), "decay": None, "flags": []}
        if item.get("error"):
            items.append(entry)
            continue
        in_sample, out_sample = item["inSample"], item["outSample"]
        out_net = float(out_sample["net"] or 0)
        if identifier == best:
            entry["flags"].append("best")
        if in_sample["trades"] < MIN_IS_TRADES:
            entry["flags"].append("too_few")
        if identifier == best and (out_net <= 0 or (oos_rank.get(identifier) or 0) > 3):
            entry["flags"].append("overfit")
        if out_net < 0:
            entry["flags"].append("negative_oos")
        average = float(in_sample["avgNet"] or 0)
        if average > 0 and out_sample["trades"]:
            entry["decay"] = round(float(out_sample["avgNet"] or 0) / average, 2)
        items.append(entry)
    return {"tried": tried if tried is not None else len(results), "best": best, "noProfit": no_profit, "items": items}
