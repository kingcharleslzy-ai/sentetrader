from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime
from typing import Any

from .indicators import cross_signal as _cross
from .indicators import ema as _ema

ENGINE_SPEC = {
    "engine": "ai-trader-completed-bar-replay",
    "revision": 1,
    "decisionPoint": "completed_bar_close",
    "fillPoint": "next_bar_open",
    "sameBarExitPriority": "stop_before_take_profit",
    "liveTrading": "disabled",
}


def engine_digest() -> str:
    return hashlib.sha256(json.dumps(
        ENGINE_SPEC, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def _finite(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("strategy_validation_non_finite_bar")
    return result


def _normalize_bars(raw_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    previous_timestamp = 0
    for raw in raw_bars:
        timestamp = int(_finite(raw.get("datetime")))
        opening = _finite(raw.get("open"))
        high = _finite(raw.get("high"))
        low = _finite(raw.get("low"))
        close = _finite(raw.get("close"))
        volume = _finite(raw.get("volume"))
        if (
            timestamp <= previous_timestamp
            or min(opening, high, low, close) <= 0
            or low > min(opening, close)
            or high < max(opening, close)
            or volume < 0
        ):
            raise ValueError("strategy_validation_invalid_bar")
        bars.append({
            "datetime": timestamp,
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "tradingDay": str(raw.get("tradingDay") or ""),
        })
        previous_timestamp = timestamp
    return bars



def _signal_series(
    bars: list[dict[str, float | int]], definition: dict[str, Any],
) -> list[str]:
    parameters = definition["parameters"]
    closes = [float(bar["close"]) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    volumes = [float(bar["volume"]) for bar in bars]
    count = len(bars)
    output = ["wait"] * count
    component_signals = {
        name: ["wait"] * count
        for name in ("ma_cross", "breakout", "kd_cross", "macd_cross")
    }

    close_prefix = [0.0]
    volume_prefix = [0.0]
    for close, volume in zip(closes, volumes, strict=True):
        close_prefix.append(close_prefix[-1] + close)
        volume_prefix.append(volume_prefix[-1] + volume)
    fast = int(parameters["fastPeriod"])
    slow = int(parameters["slowPeriod"])
    fast_values: list[float | None] = [None] * count
    slow_values: list[float | None] = [None] * count
    for index in range(count):
        if index + 1 >= fast:
            fast_values[index] = (
                close_prefix[index + 1] - close_prefix[index + 1 - fast]
            ) / fast
        if index + 1 >= slow:
            slow_values[index] = (
                close_prefix[index + 1] - close_prefix[index + 1 - slow]
            ) / slow
        if (
            index > 0
            and fast_values[index] is not None
            and slow_values[index] is not None
            and fast_values[index - 1] is not None
            and slow_values[index - 1] is not None
        ):
            component_signals["ma_cross"][index] = _cross(
                fast_values[index], slow_values[index],
                fast_values[index - 1], slow_values[index - 1],
            )

    breakout = int(parameters["breakoutPeriod"])
    for index in range(breakout, count):
        reference_high = max(highs[index - breakout:index])
        reference_low = min(lows[index - breakout:index])
        component_signals["breakout"][index] = (
            "buy" if closes[index] > reference_high
            else "sell" if closes[index] < reference_low
            else "wait"
        )

    kd_period = int(parameters["kdPeriod"])
    k_values: list[float | None] = [None] * count
    d_values: list[float | None] = [None] * count
    for index in range(kd_period - 1, count):
        low = min(lows[index - kd_period + 1:index + 1])
        high = max(highs[index - kd_period + 1:index + 1])
        k_values[index] = 50.0 if high == low else (closes[index] - low) / (high - low) * 100
        recent = [
            value for value in k_values[max(0, index - 2):index + 1]
            if value is not None
        ]
        d_values[index] = sum(recent) / len(recent)
        if (
            index > 0 and k_values[index - 1] is not None
            and d_values[index - 1] is not None
        ):
            component_signals["kd_cross"][index] = _cross(
                k_values[index], d_values[index],
                k_values[index - 1], d_values[index - 1],
            )

    macd_fast = _ema(closes, int(parameters["macdFast"]))
    macd_slow = _ema(closes, int(parameters["macdSlow"]))
    dif = [left - right for left, right in zip(macd_fast, macd_slow, strict=True)]
    dea = _ema(dif, int(parameters["macdSignal"]))
    for index in range(1, count):
        component_signals["macd_cross"][index] = _cross(
            dif[index], dea[index], dif[index - 1], dea[index - 1]
        )

    template = str(definition["template"])
    direction = str(definition["direction"])
    minimum_volume_ratio = float(parameters["minimumVolumeRatio"])
    for index in range(20, count):
        volume_average = (volume_prefix[index] - volume_prefix[index - 20]) / 20
        if volume_average <= 0 or volumes[index] / volume_average < minimum_volume_ratio:
            continue
        selected = (
            [component_signals[template][index]]
            if template != "combined"
            else [component_signals[name][index] for name in (
                "ma_cross", "breakout", "kd_cross", "macd_cross"
            )]
        )
        signal = (
            selected[0] if template != "combined"
            else "buy" if all(item == "buy" for item in selected)
            else "sell" if all(item == "sell" for item in selected)
            else "wait"
        )
        if not (
            (signal == "buy" and direction == "short")
            or (signal == "sell" and direction == "long")
        ):
            output[index] = signal
    return output


def _signal(
    bars: list[dict[str, float | int]], index: int, definition: dict[str, Any],
) -> str:
    return _signal_series(bars[:index + 1], definition)[-1]


def _warmup(definition: dict[str, Any]) -> int:
    parameters = definition["parameters"]
    return max(
        int(parameters["slowPeriod"]) + 2,
        int(parameters["breakoutPeriod"]) + 2,
        int(parameters["kdPeriod"]) + 5,
        int(parameters["macdSlow"]) + int(parameters["macdSignal"]) + 2,
        22,
    )


def _date_key(bar: dict[str, Any]) -> str:
    trading_day = str(bar.get("tradingDay") or "")
    try:
        datetime.strptime(trading_day, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("strategy_trading_day_missing") from error
    return trading_day


def _segment(
    *, bars: list[dict[str, float | int]], definition: dict[str, Any],
    start_timestamp: int, end_timestamp: int | None, price_tick: float,
    multiplier: float, slippage_ticks: float, commission_ticks: float,
    initial_balance: float,
) -> dict[str, Any]:
    limits = definition["riskLimits"]
    volume = int(limits["maxPosition"])
    max_orders = int(limits["maxOrdersPerDay"])
    stop_distance = int(limits["stopLossTicks"]) * price_tick
    take_distance = int(limits["takeProfitTicks"]) * price_tick
    cooldown_bars = int(limits["cooldownBars"])
    slippage = slippage_ticks * price_tick
    commission = commission_ticks * price_tick * multiplier * volume
    warmup = _warmup(definition)
    signals = _signal_series(bars, definition)

    cash = initial_balance
    position = 0
    entry_price = 0.0
    entry_commission = 0.0
    entry_timestamp = 0
    cooldown_until = -1
    orders_by_day: dict[str, int] = {}
    trades: list[dict[str, Any]] = []
    equity_curve = [initial_balance]
    signal_count = 0
    stop_count = 0
    take_count = 0
    daily_limit_blocks = 0
    order_count = 0

    def allowed_entry(bar: dict[str, Any]) -> bool:
        nonlocal daily_limit_blocks, order_count
        key = _date_key(bar)
        if orders_by_day.get(key, 0) >= max_orders:
            daily_limit_blocks += 1
            return False
        orders_by_day[key] = orders_by_day.get(key, 0) + 1
        order_count += 1
        return True

    def close(price: float, timestamp: int, reason: str) -> None:
        nonlocal cash, position, entry_price, entry_commission, cooldown_until, order_count
        order_count += 1
        exit_price = price - slippage if position > 0 else price + slippage
        gross = (exit_price - entry_price) * position * multiplier
        cash += gross - commission
        net = gross - entry_commission - commission
        trades.append({
            "entryTimestamp": entry_timestamp,
            "exitTimestamp": timestamp,
            "direction": "long" if position > 0 else "short",
            "netPnl": round(net, 6),
            "reason": reason,
        })
        position = 0
        entry_price = 0.0
        entry_commission = 0.0

    for index in range(warmup + 1, len(bars)):
        bar = bars[index]
        timestamp = int(bar["datetime"])
        if timestamp < start_timestamp:
            continue
        if end_timestamp is not None and timestamp >= end_timestamp:
            break
        signal = signals[index - 1]
        if signal in {"buy", "sell"}:
            signal_count += 1
        desired = volume if signal == "buy" else -volume if signal == "sell" else position
        if position and desired and (position > 0) != (desired > 0):
            close(float(bar["open"]), timestamp, "opposite_signal")
            cooldown_until = index + cooldown_bars
        if position == 0 and desired and index > cooldown_until and allowed_entry(bar):
            entry_price = float(bar["open"]) + slippage if desired > 0 else float(bar["open"]) - slippage
            entry_commission = commission
            cash -= commission
            position = desired
            entry_timestamp = timestamp

        if position:
            if position > 0:
                stop = entry_price - stop_distance
                take = entry_price + take_distance
                stop_hit = float(bar["low"]) <= stop
                take_hit = float(bar["high"]) >= take
            else:
                stop = entry_price + stop_distance
                take = entry_price - take_distance
                stop_hit = float(bar["high"]) >= stop
                take_hit = float(bar["low"]) <= take
            if stop_hit:
                close(stop, timestamp, "stop_loss")
                stop_count += 1
                cooldown_until = index + cooldown_bars
            elif take_hit:
                close(take, timestamp, "take_profit")
                take_count += 1
                cooldown_until = index + cooldown_bars

        marked = cash
        if position:
            marked += (float(bar["close"]) - entry_price) * position * multiplier
        equity_curve.append(marked)

    if position:
        last = next(
            (bar for bar in reversed(bars) if end_timestamp is None or int(bar["datetime"]) < end_timestamp),
            None,
        )
        if last is not None:
            close(float(last["close"]), int(last["datetime"]), "segment_end")
            equity_curve.append(cash)

    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    wins = sum(1 for trade in trades if trade["netPnl"] > 0)
    gross_profit = sum(max(0.0, float(trade["netPnl"])) for trade in trades)
    gross_loss = -sum(min(0.0, float(trade["netPnl"])) for trade in trades)
    return {
        "returnPercent": round((cash / initial_balance - 1) * 100, 6),
        "maxDrawdownPercent": round(max_drawdown, 6),
        "signalCount": signal_count,
        "tradeCount": len(trades),
        "winRatePercent": round(wins / len(trades) * 100, 4) if trades else 0.0,
        "profitFactor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
        "commission": round(commission * order_count, 6),
        "stopLossCount": stop_count,
        "takeProfitCount": take_count,
        "dailyLimitBlocks": daily_limit_blocks,
        "endingEquity": round(cash, 6),
        "trades": trades,  # validate_strategy_history pops it for the 运气检验; not kept in the evidence
    }


LUCK_MIN_TRADES = 10
LUCK_SAMPLES = 1000
LUCK_SEED = 42  # fixed: the same history and strategy always give the same answer


def _max_drawdown_percent(pnls: list[float], initial_balance: float) -> float:
    equity = peak = initial_balance
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100)
    return worst


def _percentile(values: list[float], share: float) -> float:
    """Linear interpolation between closest ranks (numpy's default)."""
    ordered = sorted(values)
    at = share * (len(ordered) - 1)
    low = math.floor(at)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def luck_check(pnls: list[float], initial_balance: float) -> dict[str, Any]:
    """运气检验 (2026-10-25), adapted from HKUDS/Vibe-Trading (MIT, 41f333b6) `agent/backtest/validation.py`:
    a bootstrap of the per-trade net P&L (is the average trade distinguishable from 0 at 95%?) and the trade-order
    shuffle's drawdown distribution (how deep the same trades could have drawn down in a worse order). Standard
    library only. Not ported: the shuffle's Sharpe p-value — reordering keeps the trades' mean and spread, so it sits
    near 0.5 for any strategy (10-25: 0.43 with an edge, 0.50 without)."""
    count = len(pnls)
    if count < LUCK_MIN_TRADES:
        return {"tradeCount": count, "status": "insufficient"}
    rng = random.Random(LUCK_SEED)
    means = [sum(rng.choices(pnls, k=count)) / count for _ in range(LUCK_SAMPLES)]
    drawdowns = []
    shuffled = list(pnls)
    for _ in range(LUCK_SAMPLES):
        rng.shuffle(shuffled)
        drawdowns.append(_max_drawdown_percent(shuffled, initial_balance))
    low, high = _percentile(means, 0.025), _percentile(means, 0.975)
    return {
        "tradeCount": count, "status": "edge" if low > 0 else "unclear",
        "meanNetPnl": round(sum(pnls) / count, 6), "meanLow95": round(low, 6), "meanHigh95": round(high, 6),
        "probabilityPositive": round(sum(1 for mean in means if mean > 0) / LUCK_SAMPLES, 4),
        "maxDrawdownPercent": round(_max_drawdown_percent(pnls, initial_balance), 6),
        "shuffledDrawdownP95Percent": round(_percentile(drawdowns, 0.95), 6),
        "samples": LUCK_SAMPLES, "seed": LUCK_SEED,
    }


def _luck_row(luck: dict[str, Any]) -> dict[str, Any]:
    """The check row: ✓ when the average trade's 95% interval is above 0, “!” otherwise. It informs, it does not gate:
    `passed` below and the 上线 gate (proposals.complete_strategy_validation) read only the other checks."""
    if luck["status"] == "insufficient":
        detail = f"成交 {luck['tradeCount']} 笔，不足 {LUCK_MIN_TRADES} 笔，判断不了是不是运气"
    else:
        verdict = "区间在 0 以上" if luck["status"] == "edge" else "区间跨过 0，分不清是不是运气"
        detail = (f"{luck['tradeCount']} 笔每笔平均净盈亏 {luck['meanNetPnl']:+,.0f}，95% 区间 "
                  f"{luck['meanLow95']:+,.0f} ~ {luck['meanHigh95']:+,.0f}，{verdict}；同样这些成交打乱顺序 {LUCK_SAMPLES} 次，"
                  f"95% 的情形最大回撤不超过 {luck['shuffledDrawdownP95Percent']:.2f}%（实际 {luck['maxDrawdownPercent']:.2f}%）")
    return {"id": "luck", "label": "运气检验", "status": "passed" if luck["status"] == "edge" else "waiting", "detail": detail}


def validate_strategy_history(
    *, definition: dict[str, Any], raw_bars: list[dict[str, Any]],
    split_timestamp: int, price_tick: float, contract_multiplier: float,
    data_source: str, data_digest: str, slippage_ticks: float = 1.0,
    commission_ticks_per_side: float = 0.5, initial_balance: float = 1_000_000,
) -> dict[str, Any]:
    bars = _normalize_bars(raw_bars)
    if price_tick <= 0 or contract_multiplier <= 0:
        raise ValueError("strategy_validation_contract_metadata_invalid")
    warmup = _warmup(definition)
    split_index = next(
        (index for index, bar in enumerate(bars) if int(bar["datetime"]) >= split_timestamp),
        -1,
    )
    if split_index < warmup + 10 or len(bars) - split_index < 10:
        raise ValueError("strategy_validation_split_insufficient")
    common = {
        "bars": bars,
        "definition": definition,
        "price_tick": price_tick,
        "multiplier": contract_multiplier,
        "slippage_ticks": slippage_ticks,
        "commission_ticks": commission_ticks_per_side,
        "initial_balance": initial_balance,
    }
    in_sample = _segment(
        **common, start_timestamp=int(bars[0]["datetime"]), end_timestamp=split_timestamp,
    )
    out_of_sample = _segment(
        **common, start_timestamp=split_timestamp, end_timestamp=None,
    )
    genuine_history = data_source.startswith("tqsdk:")
    checks = [
        {"id": "definition_integrity", "label": "策略定义完整", "status": "passed",
         "detail": "策略指纹、合约、周期、信号和风险参数均已固化"},
        {"id": "completed_bars_only", "label": "只使用已完成K线", "status": "passed",
         "detail": "信号读取上一根已完成K线，并在下一根开盘成交"},
        {"id": "lookahead_bias", "label": "未来数据偏差检查", "status": "passed",
         "detail": "回放引擎不读取成交点之后的K线，止盈止损同K触发时按止损优先"},
        {"id": "out_of_sample", "label": "样本外验证",
         "status": "passed" if genuine_history and out_of_sample["returnPercent"] > 0 else "failed",
         "detail": f"样本外 {out_of_sample['tradeCount']} 笔，扣成本收益 {out_of_sample['returnPercent']:.2f}%"},
        {"id": "trading_costs", "label": "交易成本", "status": "passed",
         "detail": f"每次成交计 {slippage_ticks:g} tick 滑点和 {commission_ticks_per_side:g} tick 单边费用"},
        {"id": "risk_protections", "label": "风险保护器",
         "status": "passed" if out_of_sample["maxDrawdownPercent"] <= 20 else "failed",
         "detail": f"样本外最大回撤 {out_of_sample['maxDrawdownPercent']:.2f}%，止损 {out_of_sample['stopLossCount']} 次"},
        {"id": "minimum_signals", "label": "有效信号数量",
         "status": "passed" if in_sample["tradeCount"] >= 3 and out_of_sample["tradeCount"] >= 2 else "failed",
         "detail": f"样本内 {in_sample['tradeCount']} 笔，样本外 {out_of_sample['tradeCount']} 笔"},
        {"id": "official_history", "label": "历史数据来源",
         "status": "passed" if genuine_history else "failed",
         "detail": "官方 TqSdk 历史数据" if genuine_history else "非正式历史数据，只能用于测试"},
    ]
    passed = all(check["status"] == "passed" for check in checks)
    # The luck row comes after the gate: it informs, it never fails a strategy that passes today's checks.
    luck = luck_check([float(trade["netPnl"]) for trade in in_sample.pop("trades") + out_of_sample.pop("trades")],
                      initial_balance)
    checks.append(_luck_row(luck))
    return {
        "status": "passed" if passed else "failed",
        "checks": checks,
        "evidence": {
            "engine": ENGINE_SPEC,
            "engineDigest": engine_digest(),
            "dataSource": data_source,
            "dataDigest": data_digest,
            "barCount": len(bars),
            "splitTimestamp": split_timestamp,
            "priceTick": price_tick,
            "contractMultiplier": contract_multiplier,
            "slippageTicks": slippage_ticks,
            "commissionTicksPerSide": commission_ticks_per_side,
            "inSample": in_sample,
            "outOfSample": out_of_sample,
            "luck": luck,
            "liveTrading": "disabled",
        },
    }
