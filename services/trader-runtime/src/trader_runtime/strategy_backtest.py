"""规则回测器 (策略积木·第一期 B, 2026-09-25): the user's own rule strategy over history.

Pure computation over read-only K-lines. It never places, protects or records an order and never
writes the database: the judgement is ``strategy_engine.step`` bar by bar — the same
``strategy_rules`` evaluation and ``strategy_engine.decide`` the live monitor runs — and this
module only does the bookkeeping around it (fills, fees, equity, statistics).

Live parity
-----------
* Every decision sees the windows the live monitor sees: the last ``LIVE_WINDOW`` completed bars of
  each series (the sidecar serves 256 bars and the still-forming one is never a signal input), and
  the 86400 series up to the decision bar's trading day (only today's open is read). Indicators are
  therefore computed exactly like live, window by window; the first ``LIVE_WINDOW - 1`` bars only
  warm the window up.
* 按收盘确认、下一根开盘成交: an open decided on bar k fills at bar k+1's open; a rule / time exit,
  an 加仓 and a 分批止盈 partial close fill at bar k+1's open; a protective stop / take / trailing hit
  inside a bar fills at ``Decision.fillPrice`` (the level, or the open when the bar gapped through
  it). One bar touching both stop and take counts as the stop (the engine's conservative reading).
* 盘中触价 (an entry group's ``trigger``): the open decided on bar k is armed at
  ``Decision.entryTrigger`` and fills inside bar k+1 only when that bar reaches it — at the level, or
  at the open when the bar opened past it (``strategy_engine.trigger_fill``); otherwise it lapses and
  counts for nothing (no daily open, no cooldown, no segment), like live where the attempt only
  exists once the price got there. The bar it filled in is then checked for the stop like any held
  bar (its low / high may have come before the entry: the conservative reading). 双向触价 (OCO,
  ``Decision.alternate``): both levels are armed; when bar k+1 reaches both, the one nearer its open is
  taken as reached first.
* The live execution gates that sit outside the engine are applied the same way: cooldownBars
  (bars since the last executed open / rule or time exit), maxOrdersPerDay (opens per trading day),
  maxLoss (the day's closed net), perTradeRisk sizing and the structural-stop sanity check, and one
  attempt per direction segment — an 加仓 skips the cooldown and the segment / structure checks, like
  the live monitor. Account-level risk rules do not exist on the simulation account.

Speed without a second judgement: each window handed to the engine is a ``SeriesWindow`` — the same
bars, plus where they sit in the whole series — so closes, true ranges, trading-day labels and the
indicators that only read a bar and the bars before it are computed once per series and sliced, and
a trading day's daily window (with its anchors) is built once. Recursive indicators (EMA, ATR, RSI…)
still run window by window. tests/test_backtest_template_parity.py pins trade-by-trade equality.

策略体检 (2026-09-26): the same replay counts, per entry condition, the judged bars it held on
(``strategy_health``); when the plan never opens the result carries ``headline`` and ``advice``
(drop one condition / relax the crosses), counted in entry bars, never chosen by profit.

Off the Runtime (2026-09-25)
----------------------------
The replay is pure Python CPU work. In a thread it still holds the Runtime's GIL: the event loop
waits one switch interval (5 ms) for it at every I/O step, so while a backtest ran every user's
requests stalled — up to 2.2 s in production. ``run_in_worker`` runs ``run_backtest`` in a spawned
worker process instead; only the history read (I/O, cached) stays in the Runtime's thread.
``BacktestQueue`` bounds the work: by default at most 2 backtests at once, 1 per user, first come first
served, 60 s in the queue at most (``AI_TRADER_BACKTEST_*``, see ``backtest_settings``). The live monitor's judgement never goes through either.
"""
from __future__ import annotations

import asyncio
import math
import multiprocessing
import os
import threading
import time
from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .backtest_settings import load_backtest_settings
from .instruments import money_unit, symbol_currency
from .strategy_engine import (
    Decision,
    MarketContext,
    PositionState,
    flat_after_close,
    open_position,
    partial_after_close,
    step,
    trigger_fill,
)
from .strategy_health import EntryHealth, health_report
from .strategy_rules import (
    SeriesFacts,
    SeriesWindow,
    StrategyPlan,
    StrategyRuleError,
    plan_timeframes,
    session_timeframes,
)
from .strategy_spread import join_legs, joined_window_start, spread_price
from .trading_calendar import NS, bar_trading_day, daily_bar_day, session_position
from .trading_costs import trade_fee

# Completed bars per series a live evaluation reads (KLINE_DATA_LENGTH 256 minus the forming bar);
# the 86400 context the live monitor passes is the whole 256-bar daily workbench.
LIVE_WINDOW = 255
DAILY_WINDOW = 256
HISTORY_MAX_BARS = 10_000
# Most recent bars a backtest reads by default, by execution period (TqSdk serves ≤ 10 000 per request).
DEFAULT_BARS = {60: 10_000, 180: 10_000, 300: 10_000, 900: 10_000, 3600: 5_000, 86400: 1_500}
EQUITY_POINTS = 300
TRADES_KEPT = 2_000
DEFAULT_BALANCE = 1_000_000.0
EXIT_LABELS = {"rule": "规则出场", "stop_loss": "止损", "take_profit": "止盈", "trailing_stop": "追踪止损",
               "breakeven": "保本止损", "close_before_end": "收盘前平仓", "max_hold": "最长持仓",
               "time_take_profit": "分段止盈", "scale_out": "分批止盈", "end_of_data": "回测结束平仓",
               "spread_stop": "价差止损", "spread_take": "价差止盈", "drawdown_stop": "回撤停机"}
ASSUMPTIONS = (
    "按收盘确认、下一根开盘成交：信号在K线收盘后判断，开仓与规则/时间出场（含分段止盈）在下一根K线开盘价成交。",
    "加仓与分批止盈同样按收盘确认、下一根K线开盘价成交：加仓并进同一笔持仓，盈亏按成交量加权均价计算；"
    "分批止盈的每一档平一部分，剩余持仓保留原止损止盈。",
    "价差策略的两腿行情按同一根K线时间对齐（一腿缺失的那根不判定）；开仓与出场各腿按自己下一根K线开盘价成交，"
    "手续费与滑点按各腿合约分别计算；价差止损/止盈按收盘价差判断（实盘按两腿最新行情每秒判断，会比回测更早触发）。",
    "收盘前平仓按实盘的钟点平：平仓时点落在一根K线中间（如 15 分钟、小时、日线）或K线有缺口时，用那一分钟的"
    "1 分钟K线开盘价成交；没有那段分钟K线时，按当天最接近的K线收盘价估算并在结果里注明，从不拖到下一个交易日开盘。",
    "止损、止盈、追踪止损、保本按K线最高/最低价判断是否触及，按止损/止盈价成交；跳空越过时按开盘价成交；"
    "同一根K线同时碰到止损和止盈时按止损算。",
    "判定与上线运行完全相同：同一套规则求值与策略引擎，每根K线读取与实盘相同的最近 255 根已收盘K线。",
    "冷却、每日开仓次数、当日亏损上限、单笔预算、“每段方向只开一次”与复位条件按实盘同样的口径执行。",
    "按资金比例定手数用同一份预算核心：按当时净值×比例、扣掉已持与已挂手数后除以每手保证金（按当前保证金估算，"
    "历史逐日保证金不可得），并受账户可用资金与总手数上限约束；比例缩小不自动减持。",
    "回撤停机按每根K线收盘的扣费净值（含浮动盈亏）从本轮高点判断；触发后不再开仓，持仓按下一根K线开盘价平仓。"
    "实盘为触发即撤销未成交开仓单并按对手价平仓，会比回测更早离场。",
    "只按K线回放，不含盘中逐笔成交、排队与部分成交；历史表现不代表未来。",
)


class BacktestDataError(ValueError):
    """The history cannot support a backtest; the message says what is missing."""


@dataclass(frozen=True)
class BacktestSetup:
    plan: StrategyPlan                       # executable, concrete clocks
    symbol: str
    series: dict[int, list[dict[str, Any]]]  # every series in plan_timeframes, completed bars, oldest first
    price_tick: float
    volume_multiple: float
    fee_schedule: dict[str, Any]
    initial_balance: float = DEFAULT_BALANCE
    slippage_ticks: int = 0
    schedule: dict[str, Any] | None = None
    trading_days: int | None = None          # evaluate only the last N trading days (None = all after warm-up)
    # 价差 (series holds the joined spread bars): each leg's own bars per period (its true next open for
    # fills), each joined bar's index into each leg's list (the live-sized window slices), the legs' own
    # ticks and multipliers. None for single-contract plans.
    spread_raw: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]] | None = None
    spread_indexes: dict[int, tuple[list[int], list[int]]] | None = None
    leg_ticks: tuple[float, float] | None = None
    leg_multiples: tuple[float, float] | None = None
    # 按资金比例定手数 (2026-10-09): the per-lot margin the sizing budget divides by — today's
    # snapshot (no daily history of margins exists), reported as such in the result.
    margin_per_lot: float | None = None


def _slipped(price: float, ticks: int, tick: float, *, buy: bool) -> float:
    if not ticks:
        return price
    return float(Decimal(str(price)) + Decimal(str(tick)) * ticks * (1 if buy else -1))


def _sample(points: list[dict[str, Any]], limit: int = EQUITY_POINTS) -> list[dict[str, Any]]:
    if len(points) <= limit:
        return points
    stride = math.ceil(len(points) / (limit - 1))
    sampled = points[::stride]
    return sampled if sampled[-1] is points[-1] else [*sampled, points[-1]]


def run_backtest(setup: BacktestSetup, *, trace: Callable[[int, PositionState, Decision], None] | None = None) -> dict[str, Any]:
    """The report only: its ``trades`` list is cut to the last ``TRADES_KEPT`` for display."""
    return run_backtest_with_trades(setup, trace=trace)[0]


def run_backtest_with_trades(setup: BacktestSetup, *, trace: Callable[[int, PositionState, Decision], None] | None = None,
                             ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replay the plan over `setup.series`: (report, every closed trade). The report keeps only the
    last ``TRADES_KEPT`` trades for display; a caller whose statistics must not depend on that cut (the
    lab's 样本内/样本外 split) reads the complete list. `trace(k, state, decision)` (tests) sees, per
    decision bar, the position state the engine judged with and its decision."""
    plan = setup.plan
    timeframe = plan.timeframeSeconds
    base = setup.series.get(timeframe) or []
    if len(base) < 2:
        raise BacktestDataError("执行周期的历史K线不足，无法回测")
    tick, multiple = float(setup.price_tick), float(setup.volume_multiple)
    if tick <= 0 or multiple <= 0:
        raise BacktestDataError("拿不到合约的最小变动价位或合约乘数，无法回测")
    spread = plan.spread
    leg_ticks = leg_multiples = None
    raw_legs: tuple[list[dict[str, Any]], list[dict[str, Any]]] = ([], [])
    leg_times: tuple[list[int], list[int]] = ([], [])
    if spread is not None:
        if (setup.spread_raw is None or setup.spread_indexes is None
                or setup.leg_ticks is None or setup.leg_multiples is None):
            raise BacktestDataError("价差回测缺少两腿行情，无法回测")
        leg_ticks, leg_multiples = setup.leg_ticks, setup.leg_multiples
        raw_legs = setup.spread_raw[timeframe]
        leg_times = ([int(bar["datetime"]) for bar in raw_legs[0]], [int(bar["datetime"]) for bar in raw_legs[1]])
        if any(value <= 0 for value in (*leg_ticks, *leg_multiples)):
            raise BacktestDataError("拿不到价差两腿的最小变动价位或合约乘数，无法回测")
        weight_a, weight_b = spread.legs[0].weight, spread.legs[1].weight

        def leg_fill(index: int, after: int) -> tuple[float, int, str] | None:
            """(price, time, trading day) of this leg's first bar strictly after `after`."""
            times, bars = leg_times[index], raw_legs[index]
            at = bisect_right(times, after)
            if at >= len(times):
                return None
            bar = bars[at]
            return float(bar["open"]), times[at], bar_trading_day(bar, None)

        def fill_or_last(index: int, after: int) -> tuple[float, int, str]:
            found = leg_fill(index, after)
            if found is not None:
                return found
            bar = raw_legs[index][-1]           # a leg's history ends: close at its last bar
            return float(bar["close"]), int(bar["datetime"]), bar_trading_day(bar, None)
    needed = plan_timeframes(plan)
    daily = setup.series.get(86400) if 86400 in needed and timeframe != 86400 else None
    daily_days = [daily_bar_day(bar) for bar in daily] if daily else []
    daily_facts = SeriesFacts(daily) if daily is not None else None
    others = {tf: (SeriesFacts(setup.series.get(tf) or []), [int(b["datetime"]) + tf * NS for b in setup.series.get(tf) or []])
              for tf in needed if tf not in {timeframe, 86400}}
    # Per-bar facts of each series are computed once; every window below is a slice that knows its place.
    base_facts = SeriesFacts(base)
    # 当日最高 / 最低 read a day that began before the window from the whole series (live: the history read).
    extended = {tf: (base_facts if tf == timeframe else others[tf][0]).bars
                for tf in session_timeframes(plan) if tf == timeframe or tf in others} or None
    known_days = sorted(set(daily_days)) if daily_days else None
    days = [bar_trading_day(bar, known_days) for bar in base]
    execution_days = sorted(set(days))

    warm = LIVE_WINDOW - 1 if len(base) > LIVE_WINDOW + 20 else 0
    start = warm
    requested = setup.trading_days
    if requested is not None:
        unique = sorted(set(days[warm:]))
        first = unique[-requested] if requested <= len(unique) else unique[0]
        start = max(warm, next(i for i, day in enumerate(days) if day >= first))
    if start >= len(base):
        raise BacktestDataError("历史K线只够预热指标，没有可回测的区间")
    # 收盘前平仓 closes by the clock live. A time inside an execution bar is priced from the 1-minute
    # series (loaded for it by load_series); without that minute, the nearest same-day close.
    minute_bars = setup.series.get(60) if plan.closeBeforeEndMinutes is not None and timeframe > 60 else None
    minute_times = [int(item["datetime"]) for item in minute_bars or []]
    estimated_closes = 0

    def minute_fill(at: int, end: int, fallback: tuple[float, int]) -> tuple[float, int]:
        """The open of the first minute bar at or after `at` and before `end`, else `fallback`."""
        nonlocal estimated_closes
        index = bisect_left(minute_times, at)
        if minute_bars and index < len(minute_times) and minute_times[index] < end:
            return float(minute_bars[index]["open"]), minute_times[index]
        estimated_closes += 1
        return fallback

    def close_target(timestamp: int) -> tuple[int, int] | None:
        minutes = plan.closeBeforeEndMinutes
        if minutes is None:
            return None
        position = session_position(timestamp, setup.schedule, execution_days)
        if not position.in_session:
            return None
        end = position.session_end_ns if plan.closeBeforeEndScope == "session" else position.day_end_ns
        if end is None:
            return None
        return end - minutes * 60 * NS, end

    slip = int(setup.slippage_ticks)
    balance = float(setup.initial_balance)
    realized = 0.0
    state = PositionState()
    # (decision, volume, decision bar index) — two of them for 双向触价, whichever level is reached first
    pending_open: list[tuple[Decision, int, int]] | None = None
    pending_close: tuple[Decision, PositionState, int] | None = None
    last_executed: int | None = None
    opens_by_day: dict[str, int] = {}
    net_by_day: dict[str, float] = {}
    used_segments: set[str] = set()
    daily_window: SeriesWindow | None = None
    daily_end = -1
    trades: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None
    equity_points: list[dict[str, Any]] = []
    spread_points: list[dict[str, Any]] = []
    skipped = blocked_opens = lapsed_triggers = 0
    lapsed_opens = 0                           # 价差: an open whose leg never filled (history ended)
    equity_sizing = plan.sizing is not None and plan.sizing.mode == "equity_percent"
    drawdown_stopped = False                   # 回撤停机: triggered — no more opens this round
    drawdown_close = False                     # the trigger's close, filled at the next bar's open
    drawdown_fired: dict[str, Any] | None = None
    drawdown_blocked_opens = 0
    peak = balance
    max_drawdown = max_drawdown_percent = 0.0
    # the aggregates as they stood before the newest point: a replaced last point leaves the
    # curve, and with it the peak / drawdown it set (#520-B) — 回撤按替换后的曲线算.
    before_last = (balance, 0.0, 0.0)

    def mark_equity(value: float, timestamp: int, *, replace_last: bool = False) -> None:
        """One equity point and the peak / drawdown aggregates over it (#505): the per-bar
        mark-to-market and the final end-of-data liquidation both go through here, so the last
        close's fees and slippage count in the curve and in 最大回撤, on the same basis as the
        final equity (a liquidation smaller than an earlier drawdown never overrides it). A
        replacement first takes the replaced point's peak / drawdown back out (#520-B): the
        aggregates always describe the curve as it stands."""
        nonlocal peak, max_drawdown, max_drawdown_percent, before_last
        if replace_last:
            peak, max_drawdown, max_drawdown_percent = before_last
        else:
            before_last = (peak, max_drawdown, max_drawdown_percent)
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > max_drawdown:
            max_drawdown, max_drawdown_percent = drawdown, (drawdown / peak * 100 if peak > 0 else 0.0)
        point = {"time": timestamp, "equity": round(value, 2)}
        if replace_last:
            equity_points[-1] = point
        else:
            equity_points.append(point)
    # 策略体检: per entry condition, how often it held on the judged bars (strategy_health).
    health = EntryHealth(plan)
    observe = health if health else None

    def fee(price: float, volume: int, offset: str, same_day: bool) -> float:
        return trade_fee(setup.fee_schedule, symbol=setup.symbol, price=price, volume=volume,
                         multiplier=multiple, offset=offset, today=same_day)

    def close_trade(price: float, index: int, reason: str, bars: int, *, lots: int | None = None,
                    adds: int = 0, at: int | None = None) -> None:
        """Close `lots` of the open position (the whole position when None) as one trade row: the
        row's open price is the position's weighted average at this close, the open fee is the FIFO
        share of the lots closed, and each fill bucket's close fee is 平今 / 平昨 by its own trading
        day. A partial close (分批止盈) leaves the rest of the position open. The open fee already
        entered `realized` at its fill (#520-A); only the close leg lands in it here — the row itself
        still shows the whole trade's fees and net."""
        nonlocal realized, open_trade
        assert open_trade is not None
        long = open_trade["direction"] == "long"
        price = _slipped(price, slip, tick, buy=not long)
        remaining = open_trade["volume"]
        lots = remaining if lots is None else min(lots, remaining)
        day = days[index]
        fills: list[list[Any]] = open_trade["fills"]
        open_fee = close_fee = 0.0
        left = lots
        while left > 0:                      # the closed lots leave their fill buckets, oldest first
            bucket = fills[0]
            take = min(left, bucket[1])
            share = take / bucket[1] if bucket[1] else 1.0
            open_fee += bucket[2] * share
            close_fee += fee(price, take, "CLOSE", day == bucket[3])
            bucket[1] -= take
            bucket[2] -= bucket[2] * share
            left -= take
            if bucket[1] == 0:
                fills.pop(0)
        gross = (price - open_trade["openPrice"]) * multiple * lots * (1 if long else -1)
        fees = open_fee + close_fee
        net = gross - fees
        # The open fee's share joined `realized` when its fill happened (#520-A): cash-basis, the
        # close only books its own leg now.
        realized += gross - close_fee
        net_by_day[day] = net_by_day.get(day, 0.0) + gross - close_fee
        trades.append({"id": len(trades) + 1, "direction": open_trade["direction"], "volume": lots,
                       "openTime": open_trade["openTime"], "openPrice": open_trade["openPrice"], "adds": adds,
                       "partial": lots < remaining,
                       "closeTime": int(base[index]["datetime"]) if at is None else at, "closePrice": price,
                       "exitReason": reason, "exitLabel": EXIT_LABELS[reason], "barsHeld": bars,
                       "gross": round(gross, 2), "fees": round(fees, 2), "net": round(net, 2)})
        open_trade["volume"] = remaining - lots
        if open_trade["volume"] == 0:
            open_trade = None

    if spread is not None:
        def close_spread_trade(fills: list[tuple[float, int, str]], *, reason: str, bars: int, day: str) -> None:
            """Close the whole spread position as one trade row: each leg closes at its own fill,
            gross and fees per leg (its own multiplier), the row's prices the spread values."""
            nonlocal realized, open_trade
            assert open_trade is not None and spread is not None and leg_ticks is not None and leg_multiples is not None
            trade = open_trade
            rows, closes = [], []
            gross = fees = close_fees = 0.0
            for index, row in enumerate(trade["legs"]):
                price, when, close_day = fills[index]
                price = _slipped(price, slip, leg_ticks[index], buy=row["direction"] == "sell")
                volume = row["volume"]
                leg_gross = (price - row["openPrice"]) * volume * leg_multiples[index] * (1 if row["direction"] == "buy" else -1)
                leg_close_fee = trade_fee(setup.fee_schedule, symbol=row["symbol"], price=price, volume=volume,
                                          multiplier=leg_multiples[index], offset="CLOSE", today=close_day == row["openDay"])
                leg_fees = (trade_fee(setup.fee_schedule, symbol=row["symbol"], price=row["openPrice"], volume=volume,
                                      multiplier=leg_multiples[index], offset="OPEN", today=True) + leg_close_fee)
                gross, fees, close_fees = gross + leg_gross, fees + leg_fees, close_fees + leg_close_fee
                closes.append(price)
                rows.append({"symbol": row["symbol"], "direction": row["direction"], "volume": volume,
                             "openPrice": row["openPrice"], "closePrice": price,
                             "openTime": row["openTime"], "closeTime": when,
                             "gross": round(leg_gross, 2), "fees": round(leg_fees, 2)})
            net = gross - fees
            # The open legs' fees were booked at their fills (#520-A); only the close legs land here.
            realized += gross - close_fees
            net_by_day[day] = net_by_day.get(day, 0.0) + gross - close_fees
            trades.append({"id": len(trades) + 1, "direction": trade["direction"], "volume": trade["volume"],
                           "openTime": trade["openTime"], "openPrice": trade["openPrice"], "adds": 0, "partial": False,
                           "closeTime": max(fill[1] for fill in fills),
                           "closePrice": spread_price(spread.kind, closes[0], closes[1], weight_a, weight_b),
                           "exitReason": reason, "exitLabel": EXIT_LABELS[reason], "barsHeld": bars,
                           "gross": round(gross, 2), "fees": round(fees, 2), "net": round(net, 2), "legs": rows})
            open_trade = None

    def open_floating(index: int) -> float:
        """The held position's floating P&L at `index`'s close. The maxLoss gate reads the day's
        result like the live monitor reads todayNetPnl: fees paid plus the open position's move."""
        if open_trade is None:
            return 0.0
        bar = base[index]
        if spread is not None and leg_multiples is not None:
            total = 0.0
            for i, row in enumerate(open_trade["legs"]):
                close = float(bar["legA" if i == 0 else "legB"]["close"])
                total += (close - row["openPrice"]) * row["volume"] * leg_multiples[i] * (
                    1 if row["direction"] == "buy" else -1)
            return total
        direction = 1 if open_trade["direction"] == "long" else -1
        return (float(bar["close"]) - open_trade["openPrice"]) * multiple * open_trade["volume"] * direction

    for k in range(start, len(base)):
        bar = base[k]
        if open_trade is not None and plan.closeBeforeEndMinutes is not None and k > start:
            previous_close = int(base[k - 1]["datetime"]) + timeframe * NS
            previous_target = close_target(previous_close)
            if previous_target is not None and previous_close <= previous_target[0] < int(bar["datetime"]):
                # The close time falls in a gap between two bars: that minute, else the last close before it.
                price, at = minute_fill(previous_target[0], previous_target[1],
                                        (float(base[k - 1]["close"]), previous_close - timeframe * NS))
                close_trade(price, k - 1, "close_before_end", state.bars_held, adds=state.adds, at=at)
                state = flat_after_close(state)
        if spread is not None and pending_close is not None and open_trade is not None:
            closing, held, decided = pending_close
            after = int(base[decided]["datetime"])
            close_spread_trade([fill_or_last(0, after), fill_or_last(1, after)],
                               reason=closing.exitReason or "rule", bars=held.bars_held, day=days[k])
        elif pending_close is not None and open_trade is not None:
            closing, held, _ = pending_close
            close_trade(float(bar["open"]), k, closing.exitReason or "rule", held.bars_held,
                        lots=closing.volume, adds=held.adds)
        pending_close = None
        if drawdown_close:
            # 回撤停机: the previous bar breached the line — the held position fills at this
            # bar's open (live fires at once, at the opponent's price) and stays closed.
            drawdown_close = False
            if open_trade is not None:
                close_trade(float(bar["open"]), k, "drawdown_stop", state.bars_held, adds=state.adds)
                state = flat_after_close(state)
        if drawdown_stopped and pending_open is not None:
            pending_open = None    # 回撤停机: this round's unfilled opens are cancelled, like live
        if pending_open is not None and spread is not None:
            # 价差: each leg fills at its own next bar's open (one unit = both legs, no trigger, no add).
            opening, volume, decided = pending_open[0]
            pending_open = None
            after = int(base[decided]["datetime"])
            assert leg_ticks is not None
            fill_a, fill_b = leg_fill(0, after), leg_fill(1, after)
            if fill_a is None or fill_b is None:
                lapsed_opens += 1
            else:
                long = opening.action == "open_long"
                (price_a, time_a, day_a), (price_b, time_b, day_b) = fill_a, fill_b
                price_a = _slipped(price_a, slip, leg_ticks[0], buy=long)
                price_b = _slipped(price_b, slip, leg_ticks[1], buy=not long)
                last_executed = decided
                day = opening.tradingDay or days[decided]
                opens_by_day[day] = opens_by_day.get(day, 0) + 1
                fill = spread_price(spread.kind, price_a, price_b, weight_a, weight_b)
                state = open_position(plan, opening, fill_price=fill, fill_time=time_a,
                                      price_tick=leg_ticks[0], previous=state, volume=volume)
                open_trade = {"direction": "long" if long else "short", "volume": volume,
                              "openTime": time_a, "openPrice": fill,
                              "legs": [{"symbol": spread.legs[0].symbol, "direction": "buy" if long else "sell",
                                        "volume": spread.legs[0].lots * volume, "openPrice": price_a,
                                        "openTime": time_a, "openDay": day_a},
                                       {"symbol": spread.legs[1].symbol, "direction": "sell" if long else "buy",
                                        "volume": spread.legs[1].lots * volume, "openPrice": price_b,
                                        "openTime": time_b, "openDay": day_b}]}
                assert leg_multiples is not None
                open_fees = sum(trade_fee(setup.fee_schedule, symbol=leg["symbol"], price=leg["openPrice"],
                                          volume=leg["volume"], multiplier=leg_multiples[i], offset="OPEN", today=True)
                                for i, leg in enumerate(open_trade["legs"]))
                realized -= open_fees   # both legs' open fees are paid at their fills (#520-A)
                net_by_day[days[k]] = net_by_day.get(days[k], 0.0) - open_fees
        elif pending_open is not None:
            reached = []
            for opening, volume, decided in pending_open:
                long = opening.action == "open_long"
                price_at = float(bar["open"]) if opening.entryTrigger is None else trigger_fill(opening.entryTrigger, bar, long=long)
                if price_at is not None:
                    reached.append((abs(price_at - float(bar["open"])), opening, volume, decided, price_at))
            pending_open = None
            if not reached:
                lapsed_triggers += 1
            else:
                _, opening, volume, decided, fill = min(reached, key=lambda item: item[0])
                long = opening.action == "open_long"
                # Counted once it fills (a lapsed 盘中触价 never became an attempt).
                last_executed = decided
                day = opening.tradingDay or days[decided]
                opens_by_day[day] = opens_by_day.get(day, 0) + 1
                if opening.alignmentId:
                    used_segments.add(opening.alignmentId)
                price = _slipped(fill, slip, tick, buy=long)
                state = open_position(plan, opening, fill_price=price, fill_time=int(bar["datetime"]),
                                      price_tick=tick, previous=state, volume=volume)
                open_fee = fee(price, volume, "OPEN", True)
                # The open fee is paid at the fill, not at the close (#520-A): it enters the day's
                # result and the equity marks from this bar on, like the live account's balance.
                realized -= open_fee
                net_by_day[days[k]] = net_by_day.get(days[k], 0.0) - open_fee
                if open_trade is not None and opening.add and open_trade["direction"] == ("long" if long else "short"):
                    # 加仓: another fill of the same position — a volume-weighted average price.
                    total = open_trade["volume"] + volume
                    open_trade["openPrice"] = round((open_trade["openPrice"] * open_trade["volume"]
                                                     + price * volume) / total, 6)
                    open_trade["volume"] = total
                    open_trade["fills"].append([price, volume, open_fee, days[k]])
                else:
                    open_trade = {"direction": "long" if long else "short", "volume": volume,
                                  "openTime": int(bar["datetime"]), "openPrice": price,
                                  "fills": [[price, volume, open_fee, days[k]]]}
        # The windows the live monitor would read at this bar's close.
        cutoff = int(bar["datetime"]) + timeframe * NS
        if open_trade is not None and plan.closeBeforeEndMinutes is not None:
            target = close_target(int(bar["datetime"]))
            # The close time falls inside this bar: live closes at that minute, so the backtest does too —
            # before this bar's own rule exit, which would come later than the clock.
            if target is not None and int(bar["datetime"]) < target[0] < cutoff:
                price, at = minute_fill(target[0], target[1], (float(bar["close"]), int(bar["datetime"])))
                close_trade(price, k, "close_before_end", state.bars_held, adds=state.adds, at=at)
                state = flat_after_close(state)
        # The windows the live monitor would read at this bar's close. 价差: the join of each leg's own
        # last LIVE_WINDOW bars (a leg's bars missing in the other still count toward its window).
        windows: dict[int, list[dict[str, Any]]] = {}
        if spread is not None:
            assert setup.spread_indexes is not None
            ja, jb = setup.spread_indexes[timeframe]
            windows[timeframe] = SeriesWindow(base_facts, joined_window_start(ja, jb, k, LIVE_WINDOW), k + 1)
        else:
            windows[timeframe] = SeriesWindow(base_facts, max(0, k + 1 - LIVE_WINDOW), k + 1)
        for tf, (facts, ends) in others.items():
            end = bisect_right(ends, cutoff)
            from_here = max(0, end - LIVE_WINDOW)
            if spread is not None and setup.spread_indexes is not None and end > 0:
                ja_other, jb_other = setup.spread_indexes[tf]
                from_here = joined_window_start(ja_other, jb_other, end - 1, LIVE_WINDOW)
            windows[tf] = SeriesWindow(facts, from_here, end)
        if daily_facts is not None:
            end = bisect_right(daily_days, days[k])
            if daily_window is None or daily_end != end:   # one window object per trading day keeps its anchors
                daily_window, daily_end = SeriesWindow(daily_facts, max(0, end - DAILY_WINDOW), end), end
            windows[86400] = daily_window
        equity = balance + realized
        funds: dict[str, Any] | None = None
        if equity_sizing:
            # 按资金比例: the same compute_budget core — off this bar's mark (cash + floating),
            # the held and pending lots occupied. The account's affordable leg also carries the
            # occupation (live `available` is net of frozen margin; the backtest deducts the same
            # occupied margin off its own book).
            from .strategy_funds import compute_budget
            floating = open_floating(k)
            used_lots = int(open_trade["volume"]) if open_trade is not None else 0
            pending_lots = 0
            for _, volume, _ in pending_open or []:
                pending_lots += int(volume)
            mark = balance + realized + floating
            occupied_margin = (used_lots + pending_lots) * float(setup.margin_per_lot or 0)
            funds = compute_budget(
                equity=mark,
                equity_percent=float(plan.sizing.equityPercent),  # type: ignore[union-attr, arg-type]
                margin_per_lot=setup.margin_per_lot,
                used_lots=used_lots,
                pending_lots=pending_lots,
                available=max(0.0, mark - occupied_margin),
                fee_per_lot=fee(float(bar["close"]), 1, "OPEN", True),
                max_lots=(int(plan.sizing.maxLots)  # type: ignore[union-attr]
                          if plan.sizing.maxLots is not None else None),  # type: ignore[union-attr]
                unit=money_unit(symbol_currency(setup.symbol)))
        market = MarketContext(price_tick=tick, volume_multiple=multiple, equity=equity, funds=funds)
        decision: Decision | None
        try:
            decision = step(plan, windows, state, market, daily=daily_window, schedule=setup.schedule,
                            extended=extended, observe=observe)
        except StrategyRuleError:
            skipped += 1   # not enough bars for an indicator yet: live would not judge either
            decision = None
        if decision is not None:
            if trace is not None:
                trace(k, decision.state if decision.state is not None and decision.fillPrice is None else state, decision)
            action = decision.action
            if action.startswith("close") and open_trade is not None:
                reason = decision.exitReason or "rule"
                held = decision.state or state
                if decision.fillPrice is not None:
                    close_trade(float(decision.fillPrice), k, reason, held.bars_held,
                                lots=decision.volume, adds=held.adds)
                else:
                    target = close_target(cutoff) if reason == "close_before_end" else None
                    next_open = int(base[k + 1]["datetime"]) if k + 1 < len(base) else None
                    if target is not None and (next_open is None or next_open >= target[1]):
                        # The next bar opens after the close (next session or day): fill at this clock
                        # minute instead of carrying the position over.
                        price, at = minute_fill(cutoff, target[1], (float(bar["close"]), int(bar["datetime"])))
                        close_trade(price, k, reason, held.bars_held, lots=decision.volume,
                                    adds=held.adds, at=at)
                    else:
                        pending_close = (decision, held, k)
                        last_executed = k
                # 分批止盈 closes only its tier's lots; any other close takes the whole position.
                state = (partial_after_close(state, decision.volume, decision.exitKey)
                         if decision.volume < held.volume else flat_after_close(state, decision.exitKey))
            elif action.startswith("open") and k + 1 < len(base):
                if drawdown_stopped:
                    # 回撤停机: this round opens nothing more (the engine's sizing may still say
                    # lots; the persistent fence is the round's, not the budget's).
                    drawdown_blocked_opens += 1
                else:
                    armed = []
                    floating = open_floating(k)
                    for candidate in (decision, decision.alternate):
                        if candidate is None:
                            continue
                        volume, gate = _execution_gate(setup, candidate, bar, k, base, last_executed, opens_by_day,
                                                       net_by_day, used_segments, floating=floating)
                        if gate is None:
                            armed.append((candidate, volume, k))
                    if armed:
                        pending_open = armed
                    else:
                        blocked_opens += 1
            elif not action.startswith(("open", "close")):
                state = decision.state or state
        unrealized = open_floating(k)
        equity = balance + realized + unrealized
        mark_equity(equity, int(bar["datetime"]))
        if (plan.maxDrawdownPercent is not None and not drawdown_stopped
                and peak > 0 and (peak - equity) / peak * 100 >= float(plan.maxDrawdownPercent)):
            # 回撤停机: the fee-paid net value (floating included) fell past the line off this
            # round's peak — the same read the live monitor judges each poll.
            drawdown_stopped = drawdown_close = True
            drawdown_fired = {"triggeredAt": int(bar["datetime"]), "limitPercent": float(plan.maxDrawdownPercent),
                              "drawdownPercent": round((peak - equity) / peak * 100, 2),
                              "peakEquity": round(peak, 2), "equity": round(equity, 2)}
        if spread is not None:                    # 价差小图: the spread's own close, same cadence as equity
            spread_points.append({"time": int(bar["datetime"]), "value": round(float(bar["close"]), 6)})

    last = len(base) - 1
    if open_trade is not None and spread is not None:
        close_spread_trade([(float(base[last]["legA"]["close"]), int(base[last]["datetime"]), days[last]),
                            (float(base[last]["legB"]["close"]), int(base[last]["datetime"]), days[last])],
                           reason="end_of_data", bars=(state.bars_held if state.direction != "flat" else 0),
                           day=days[last])
        mark_equity(balance + realized, int(base[last]["datetime"]), replace_last=True)
    elif open_trade is not None:
        close_trade(float(base[last]["close"]), last,
                    "drawdown_stop" if drawdown_close else "end_of_data",
                    (state.bars_held if state.direction != "flat" else 0), adds=state.adds)
        mark_equity(balance + realized, int(base[last]["datetime"]), replace_last=True)
    report = _report(setup, trades, equity_points, base, days, start, warm, requested,
                     max_drawdown, max_drawdown_percent, skipped, blocked_opens)
    if plan.closeBeforeEndMinutes is not None and not setup.schedule:
        report["notes"].append("拿不到这个合约的交易时段，回测里收盘前平仓不会触发（实盘同样无法按时平，会提醒你手动处理）。")
    if lapsed_triggers:
        report["notes"].append(f"{lapsed_triggers} 次盘中触价单在下一根K线内没有触及价位，已作废（不计开仓次数）。")
    if lapsed_opens:
        report["notes"].append(f"{lapsed_opens} 次价差开仓因一腿历史K线先行结束没有成交（不计开仓次数）。")
    if estimated_closes:
        report["notes"].append(f"{estimated_closes} 笔收盘前平仓缺少那段 1 分钟K线（分钟线只取得到最近约 29 个交易日），"
                               "按当天最接近的K线收盘价估算。")
    if equity_sizing and setup.margin_per_lot is not None:
        report["notes"].append(f"按资金比例定手数按当前每手保证金 {setup.margin_per_lot:g} 元估算"
                               "（历史逐日保证金不可得），预算随净值逐根K线重算。")
    if drawdown_fired is not None:
        report["notes"].append(
            f"回撤停机已触发：扣费净值从高点 {drawdown_fired['peakEquity']:g} 回撤 "
            f"{drawdown_fired['drawdownPercent']:g}%（线 {drawdown_fired['limitPercent']:g}%），此后不再开仓"
            + (f"，另有 {drawdown_blocked_opens} 次信号被拦下，" if drawdown_blocked_opens else "，")
            + "持仓按下一根K线开盘价平仓；实盘为触发即撤单并按对手价平仓，会更早离场。")
        report["drawdownStop"] = {**drawdown_fired, "blockedOpens": drawdown_blocked_opens}
    result = {**report, **health_report(plan, health, trading_days=report["coverage"]["tradingDays"],
                                        trades=report["summary"]["trades"])}
    if spread is not None and leg_ticks is not None and leg_multiples is not None:
        result["spread"] = {"kind": spread.kind, "legs": [
            {"symbol": leg.symbol, "lots": leg.lots, "weight": leg.weight,
             "priceTick": leg_ticks[index], "volumeMultiple": leg_multiples[index]}
            for index, leg in enumerate(spread.legs)]}
        result["spreadCurve"] = _sample(spread_points)
    return result, trades


def _execution_gate(setup: BacktestSetup, decision: Decision, bar: dict[str, Any], k: int, base: list[dict[str, Any]],
                    last_executed: int | None, opens_by_day: dict[str, int], net_by_day: dict[str, float],
                    used_segments: set[str], *, floating: float = 0.0) -> tuple[int, str | None]:
    """The live monitor's execution checks around an engine open (strategy_monitor / strategy_intents).
    The day's loss reads like the live todayNetPnl (#520-A): realized fees and P&L so far, plus the
    held position's floating move at this bar's close."""
    plan = setup.plan
    volume = decision.volume
    day = decision.tradingDay or ""
    if plan.maxLoss is not None and net_by_day.get(day, 0.0) + floating <= -float(plan.maxLoss):
        return 0, "已达到本次亏损上限"
    if decision.alignmentId and decision.alignmentId in used_segments:
        return 0, "本段方向已尝试开仓"
    if opens_by_day.get(day, 0) >= plan.maxOrdersPerDay:
        return 0, "已达到策略当日委托上限"
    if not decision.add and last_executed is not None:
        gap = (int(bar["datetime"]) - int(base[last_executed]["datetime"])) / (plan.timeframeSeconds * NS)
        if gap <= plan.cooldownBars:
            return 0, "策略仍在冷却周期"
    # The reference the budget and structure are checked from: where the open would fill (盘中触价: its level).
    close = float(decision.entryTrigger["price"]) if decision.entryTrigger else float(bar["close"])
    stop = decision.stopPrice
    long = decision.action == "open_long"
    if decision.structure is not None and (stop is None or stop <= 0 or (stop >= close if long else stop <= close)):
        return 0, "结构止损无效"
    if plan.perTradeRisk is not None and stop is not None:
        per_lot = abs(Decimal(str(close)) - Decimal(str(stop))) * Decimal(str(setup.volume_multiple))
        affordable = int(Decimal(str(plan.perTradeRisk)) // per_lot) if per_lot > 0 else 0
        volume = min(volume, affordable)
        if volume <= 0:
            return 0, "单笔预算不足一手"
    return volume, None


def _report(setup: BacktestSetup, trades: list[dict[str, Any]], equity_points: list[dict[str, Any]],
            base: list[dict[str, Any]], days: list[str], start: int, warm: int, requested: int | None,
            max_drawdown: float, max_drawdown_percent: float, skipped: int, blocked_opens: int) -> dict[str, Any]:
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [t["net"] for t in trades if t["net"] <= 0]
    net = round(sum(t["net"] for t in trades), 2)
    average_win = sum(wins) / len(wins) if wins else None
    average_loss = abs(sum(losses) / len(losses)) if losses else None
    payoff = (round(average_win / average_loss, 2) if average_win is not None and average_loss
              else None)
    reasons: dict[str, dict[str, Any]] = {}
    for trade in trades:
        entry = reasons.setdefault(trade["exitReason"], {"reason": trade["exitReason"], "label": trade["exitLabel"],
                                                         "count": 0, "net": 0.0})
        entry["count"] += 1
        entry["net"] = round(entry["net"] + trade["net"], 2)
    covered = sorted(set(days[start:]))
    coverage = {"firstBarTime": int(base[start]["datetime"]), "lastBarTime": int(base[-1]["datetime"]),
                "bars": len(base) - start, "warmupBars": warm, "tradingDays": len(covered),
                "firstTradingDay": covered[0] if covered else None, "lastTradingDay": covered[-1] if covered else None,
                "requestedTradingDays": requested, "limited": requested is not None and requested > len(covered)}
    notes = []
    if coverage["limited"]:
        notes.append(f"数据只覆盖 {len(covered)} 个交易日（要求 {requested} 个）：已用全部可取到的历史。")
    if skipped:
        notes.append(f"{skipped} 根K线的指标所需历史不足，这些K线不判定（实盘同样不判定）。")
    return {
        "summary": {"netProfit": net, "returnPercent": round(net / setup.initial_balance * 100, 2) if setup.initial_balance else None,
                    "maxDrawdown": round(max_drawdown, 2), "maxDrawdownPercent": round(max_drawdown_percent, 2),
                    "winRate": round(len(wins) / len(trades) * 100, 1) if trades else None,
                    "payoffRatio": payoff, "trades": len(trades),
                    "averageBarsHeld": round(sum(t["barsHeld"] for t in trades) / len(trades), 1) if trades else None,
                    "fees": round(sum(t["fees"] for t in trades), 2), "blockedOpens": blocked_opens},
        "exitReasons": sorted(reasons.values(), key=lambda item: -item["count"]),
        "equityCurve": _sample(equity_points),
        "trades": trades[-TRADES_KEPT:], "tradesTruncated": len(trades) > TRADES_KEPT,
        "coverage": coverage, "notes": notes, "assumptions": list(ASSUMPTIONS),
        "feeEstimate": fee_estimate(setup.fee_schedule),
        "initialBalance": setup.initial_balance, "slippageTicks": setup.slippage_ticks,
        "priceTick": setup.price_tick, "volumeMultiple": setup.volume_multiple,
    }


def _reference_figures(rate: dict[str, Any], reference: dict[str, Any]) -> tuple[str, str]:
    """One rate as (figures, origin): 开仓 / 平昨 / 平今 on the rate's own basis（交易所标准里
    同一栏“按金额＋每手”并存时用＋每手标出）。"""
    unit = "元/手" if rate["mode"] == "per_lot" else "万分之"

    def figure(key: str) -> str:
        extra = rate.get(f"{key}PerLotExtra") or 0
        suffix = f"＋每手{extra:g}" if rate["mode"] == "notional" and extra else ""
        return f"{rate[key]:g}{suffix}"

    figures = (f"开仓 {figure('open')}、平昨 {figure('closeYesterday')}、"
               f"平今 {figure('closeToday')} {unit}")
    origin = "、".join(str(item) for item in (reference.get("contract"), reference.get("asOf")) if item)
    return figures, origin


def fee_estimate(schedule: dict[str, Any]) -> dict[str, Any]:
    """How the fees in this result were counted (shown on the result card): the user's own rates,
    the exchange standard (未自定义费率时的默认，openctp 汇总), or none at all. A
    spread's schedule carries one rate per leg (#506) and is shown leg by leg; when a leg had no
    standard the whole backtest is uncounted and the missing contracts are named."""
    source = schedule.get("source")
    if source == "custom":
        return {"source": "custom", "warning": False, "text": "手续费按“设置 → 交易”里的自定义费率计算。"}
    if source == "foreign":
        per_lot = schedule["default"]["open"]
        return {"source": "foreign", "warning": False, "rate": schedule["default"],
                "text": f"外盘手续费按每手每边 {per_lot:g} 美元计算，与外盘撮合同一口径。"}
    if source in {"exchange", "reference"}:
        legs = schedule.get("legs")
        if legs:
            parts = []
            for leg in legs:
                figures, origin = _reference_figures(leg["rate"], leg.get("reference") or {})
                parts.append(f"{leg['symbol']} {figures}{f'（{origin}）' if origin else ''}")
            return {"source": source, "warning": False, "legs": legs,
                    "text": f"手续费按交易所标准估算（openctp 汇总），两腿各按自己合约计费：{'；'.join(parts)}。可在“设置 → 交易”里自定义。"}
        figures, origin = _reference_figures(schedule["default"], schedule.get("reference") or {})
        return {"source": source, "warning": False, "rate": schedule["default"],
                "reference": schedule.get("reference") or {},
                "text": f"手续费按交易所标准估算（openctp 汇总{f'，{origin}' if origin else ''}）：{figures}。可在“设置 → 交易”里自定义。"}
    missing = "、".join(str(item) for item in schedule.get("missingReference") or [])
    whose = f" {missing} " if missing else "这个合约"
    if schedule.get("foreign"):
        return {"source": "none", "warning": True, "text": f"未扣手续费，结果偏乐观：拿不到{whose}的外盘每手手续费（MT5 合约资料暂缺）。"}
    return {"source": "none", "warning": True,
            "text": f"未扣手续费，结果偏乐观：拿不到{whose}的交易所标准手续费。可在“设置 → 交易”里自定义费率。"}


# -- reading the history through the provider (read-only) ---------------------------------------------------

def load_series(provider, plan: StrategyPlan, symbol: str, trading_days: int | None) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    """Every series the plan reads, through `provider.market_history` (read-only). The execution
    period takes its default depth (all 10 000 bars when a trading-day range is asked for); slower
    series and the daily anchors are sized to cover it. Intraday series drop their still-forming
    last bar; the daily series keeps today's bar (only its open is ever read)."""
    reader = getattr(provider, "market_history", None)
    if not callable(reader):
        raise RuntimeError("strategy_backtest_history_unavailable")
    timeframe = plan.timeframeSeconds
    first = reader(symbol, timeframe, HISTORY_MAX_BARS if trading_days else DEFAULT_BARS[timeframe])
    base = list(first.get("bars") or [])[:-1]
    if len(base) < 2:
        raise BacktestDataError("执行周期的历史K线不足，无法回测")
    series: dict[int, list[dict[str, Any]]] = {timeframe: base}
    span = int(base[-1]["datetime"]) - int(base[0]["datetime"])
    wanted = plan_timeframes(plan) - {timeframe}
    if plan.closeBeforeEndMinutes is not None and timeframe > 60:
        wanted |= {60}   # 收盘前平仓 inside a bar is priced at that minute
    for other in sorted(wanted):
        if other == 86400:
            day_count = len({bar.get("tradingDay") or bar["datetime"] // (86400 * NS) for bar in base})
            length = min(HISTORY_MAX_BARS, day_count + DAILY_WINDOW + 2)
            series[86400] = list(reader(symbol, 86400, length).get("bars") or [])
        else:
            length = min(HISTORY_MAX_BARS, span // (other * NS) + LIVE_WINDOW + 2)
            series[other] = list(reader(symbol, other, length).get("bars") or [])[:-1]
    return series, first


def load_spread_series(provider, plan: StrategyPlan, trading_days: int | None) -> tuple[
        dict[int, list[dict[str, Any]]], dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]],
        dict[int, tuple[list[int], list[int]]], tuple[float, float], tuple[float, float], dict[str, Any]]:
    """``load_series`` for a spread plan: both legs' own bars per period (the legs' true next opens for
    fills), the joined spread series and each joined bar's index into each leg (the live-sized windows),
    and the legs' own ticks and multipliers. The returned response is the A leg's."""
    reader = getattr(provider, "market_history", None)
    if not callable(reader):
        raise RuntimeError("strategy_backtest_history_unavailable")
    spread = plan.spread
    assert spread is not None
    timeframe = plan.timeframeSeconds
    legs = []
    for leg in spread.legs:
        first = reader(leg.symbol, timeframe, HISTORY_MAX_BARS if trading_days else DEFAULT_BARS[timeframe])
        bars = list(first.get("bars") or [])[:-1]
        if len(bars) < 2:
            raise BacktestDataError("执行周期的历史K线不足，无法回测")
        legs.append((bars, first))
    joined, ja, jb = join_legs(spread, legs[0][0], legs[1][0])
    if len(joined) < 2:
        raise BacktestDataError("两条腿同周期的共同K线不足，无法回测")
    series: dict[int, list[dict[str, Any]]] = {timeframe: joined}
    spread_raw: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {timeframe: (legs[0][0], legs[1][0])}
    spread_indexes: dict[int, tuple[list[int], list[int]]] = {timeframe: (ja, jb)}
    span = int(joined[-1]["datetime"]) - int(joined[0]["datetime"])
    for other in sorted(plan_timeframes(plan) - {timeframe}):   # spread bans daily anchors: no 86400 series
        length = min(HISTORY_MAX_BARS, span // (other * NS) + LIVE_WINDOW + 2)
        own = [list(reader(leg.symbol, other, length).get("bars") or [])[:-1] for leg in spread.legs]
        bars, ja_other, jb_other = join_legs(spread, own[0], own[1])
        series[other], spread_raw[other], spread_indexes[other] = bars, (own[0], own[1]), (ja_other, jb_other)
    ticks, multiples = [], []
    for index, leg in enumerate(spread.legs):
        tick, multiple = legs[index][1].get("priceTick"), legs[index][1].get("volumeMultiple")
        if not tick or not multiple:
            snapshot = provider.market_snapshot("backtest", leg.symbol)
            tick, multiple = snapshot.get("priceTick"), snapshot.get("volumeMultiple")
        ticks.append(float(tick or 0))
        multiples.append(float(multiple or 0))
    return series, spread_raw, spread_indexes, (ticks[0], ticks[1]), (multiples[0], multiples[1]), legs[0][1]


def plan_for_backtest(raw_plan: dict[str, Any], timeframe: int | None) -> StrategyPlan:
    """A saved or submitted plan made executable: follow-chart clocks bound to `timeframe`."""
    from .strategy_rules import resolve_chart_timeframe, uses_chart_timeframe
    plan = StrategyPlan.model_validate(raw_plan)
    plan.check_executable()
    if uses_chart_timeframe(plan):
        if timeframe is None:
            raise StrategyRuleError("chart_timeframe_required", "这条策略跟随图表周期，请指定回测周期")
        plan = resolve_chart_timeframe(plan, timeframe)
    return plan


HISTORY_ERRORS = {
    "history_session_failed": "历史行情会话暂时出错，已自动重建，请稍后重试；实盘行情不受影响。",
    "market_history_timeout": "历史行情读取超时，请稍后重试；实盘行情不受影响。",
    "market_series_not_ready": "历史K线还在下载，请稍后再回测一次。",
    "sidecar_market_history_unavailable": "历史行情服务暂时不可用，请稍后重试；实盘行情不受影响。",
    "tqkq_credentials_missing": "历史行情会话缺少快期认证，拿不到历史K线。",
    "tqkq_authentication_failed": "历史行情会话的快期认证失败，拿不到历史K线。",
    "tqkq_connection_timeout": "历史行情会话连接超时，请稍后重试；实盘行情不受影响。",
    "tqkq_daily_maintenance": "快期每日 19:00–19:30 例行维护，期间拿不到历史K线，结束后再回测。",
    "strategy_backtest_history_unavailable": "当前数据源拿不到历史K线，无法回测。",
    "market_series_requires_sidecar": "当前数据源拿不到历史K线，无法回测。",
    "backtest_worker_failed": "回测进程意外退出，请重试一次。",
}


def history_error_message(code: str) -> str:
    """A history-read failure as the sentence the 回测 card shows."""
    return HISTORY_ERRORS.get(code, f"历史行情暂时不可用（{code or '未知原因'}），请稍后重试；实盘行情不受影响。")


def account_equity(provider) -> float:
    """初始资金默认取账户权益；读不到时 100 万。"""
    try:
        equity = float(provider.account_summary("backtest").get("equity"))
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        return DEFAULT_BALANCE
    return equity if math.isfinite(equity) and equity > 0 else DEFAULT_BALANCE


def equity_sized(plan: StrategyPlan) -> bool:
    return plan.sizing is not None and plan.sizing.mode == "equity_percent"


def margin_per_lot_for(provider, symbol: str, first: dict[str, Any]) -> float:
    """按资金比例回测 (2026-10-09): the budget divides by the per-lot margin, and only today's
    snapshot carries one — no daily margin history exists. Missing margin refuses the mode
    instead of silently sizing off a guessed number. Shared by the rule backtest and the lab."""
    margin_per_lot = first.get("marginPerLot")
    if margin_per_lot is None:
        try:
            margin_per_lot = provider.market_snapshot("backtest", symbol).get("marginPerLot")
        except (KeyError, RuntimeError):
            margin_per_lot = None
    try:
        margin_per_lot = float(margin_per_lot) if margin_per_lot is not None else None
    except (TypeError, ValueError):
        margin_per_lot = None
    if margin_per_lot is None or margin_per_lot <= 0:
        raise StrategyRuleError(
            "backtest_margin_unavailable",
            "按资金比例定手数需要每手保证金，当前拿不到这个合约的保证金，无法回测；可先改用固定手数或 ATR 定手数。")
    return margin_per_lot


def prepare_setup(provider, *, plan: StrategyPlan, symbol: str, fee_schedule: dict[str, Any], trading_days: int | None,
                  slippage_ticks: int, initial_balance: float | None) -> BacktestSetup:
    """Read the history (read-only) into the setup ``run_backtest`` replays — what the server's worker
    computes and, serialised by ``setup_inputs``, what the browser engine computes. Blocking."""
    spread_raw = spread_indexes = leg_ticks = leg_multiples = None
    tick: float | None
    multiple: float | None
    if plan.spread is not None:
        series, spread_raw, spread_indexes, leg_ticks, leg_multiples, first = load_spread_series(provider, plan, trading_days)
        symbol = plan.spread.legs[0].symbol          # the execution copy's symbol; A drives the schedule
        tick, multiple = leg_ticks[0], leg_multiples[0]
    else:
        series, first = load_series(provider, plan, symbol, trading_days)
        tick, multiple = first.get("priceTick"), first.get("volumeMultiple")
        if not tick or not multiple:
            snapshot = provider.market_snapshot("backtest", symbol)
            tick, multiple = snapshot.get("priceTick"), snapshot.get("volumeMultiple")
    margin_per_lot = (margin_per_lot_for(provider, symbol, {} if plan.spread else first)
                      if equity_sized(plan) else None)
    return BacktestSetup(plan=plan, symbol=symbol, series=series,
                         price_tick=float(tick or 0), volume_multiple=float(multiple or 0), fee_schedule=fee_schedule,
                         initial_balance=float(initial_balance) if initial_balance else account_equity(provider),
                         slippage_ticks=slippage_ticks, schedule=first.get("tradingSchedule"),
                         trading_days=trading_days, spread_raw=spread_raw, spread_indexes=spread_indexes,
                         leg_ticks=leg_ticks, leg_multiples=leg_multiples, margin_per_lot=margin_per_lot)


# -- the setup as plain JSON: the browser engine (本机优先, 2026-10-12) ------------------------------------------

def setup_inputs(setup: BacktestSetup) -> dict[str, Any]:
    """The setup as a JSON-ready dict (``POST /api/strategy-backtests/inputs``): everything
    ``run_backtest`` reads, nothing else. ``setup_from_inputs`` is its exact inverse, so the browser's
    Pyodide run and the server's native run replay the same setup."""
    pairs = lambda table: ({str(key): list(value) for key, value in table.items()}   # noqa: E731
                           if table is not None else None)
    return {"plan": setup.plan.model_dump(), "symbol": setup.symbol,
            "series": {str(timeframe): bars for timeframe, bars in setup.series.items()},
            "priceTick": setup.price_tick, "volumeMultiple": setup.volume_multiple,
            "feeSchedule": setup.fee_schedule, "initialBalance": setup.initial_balance,
            "slippageTicks": setup.slippage_ticks, "schedule": setup.schedule, "tradingDays": setup.trading_days,
            "spreadRaw": pairs(setup.spread_raw), "spreadIndexes": pairs(setup.spread_indexes),
            "legTicks": list(setup.leg_ticks) if setup.leg_ticks is not None else None,
            "legMultiples": list(setup.leg_multiples) if setup.leg_multiples is not None else None,
            "marginPerLot": setup.margin_per_lot}


def setup_from_inputs(inputs: dict[str, Any]) -> BacktestSetup:
    """``setup_inputs`` read back (int timeframes, tuples where the setup keeps tuples)."""
    def pairs(table: dict[str, Any] | None) -> dict[int, tuple[Any, Any]] | None:
        return {int(key): (value[0], value[1]) for key, value in table.items()} if table is not None else None
    leg_ticks, leg_multiples = inputs.get("legTicks"), inputs.get("legMultiples")
    return BacktestSetup(
        plan=StrategyPlan.model_validate(inputs["plan"]), symbol=str(inputs["symbol"]),
        series={int(timeframe): bars for timeframe, bars in (inputs.get("series") or {}).items()},
        price_tick=float(inputs["priceTick"]), volume_multiple=float(inputs["volumeMultiple"]),
        fee_schedule=inputs["feeSchedule"], initial_balance=float(inputs["initialBalance"]),
        slippage_ticks=int(inputs["slippageTicks"]), schedule=inputs.get("schedule"),
        trading_days=inputs.get("tradingDays"),
        spread_raw=pairs(inputs.get("spreadRaw")), spread_indexes=pairs(inputs.get("spreadIndexes")),
        leg_ticks=(float(leg_ticks[0]), float(leg_ticks[1])) if leg_ticks is not None else None,
        leg_multiples=(float(leg_multiples[0]), float(leg_multiples[1])) if leg_multiples is not None else None,
        margin_per_lot=inputs.get("marginPerLot"))


def execute(provider, *, plan: StrategyPlan, symbol: str, fee_schedule: dict[str, Any], trading_days: int | None,
            slippage_ticks: int, initial_balance: float | None,
            digest: Callable[[dict[str, Any]], str] | None = None) -> dict[str, Any]:
    """Read the history (read-only) and run the backtest. Blocking: call it off the event loop.
    ``digest`` (the server's inputs digest) marks the result with the digest of the setup it ran, so
    a browser result over the same inputs can be checked against it."""
    started = time.monotonic()
    setup = prepare_setup(provider, plan=plan, symbol=symbol, fee_schedule=fee_schedule, trading_days=trading_days,
                          slippage_ticks=slippage_ticks, initial_balance=initial_balance)
    loaded = time.monotonic()
    result = run_in_worker(setup)
    return {**result, **result_identity(setup, fee_schedule),
            **({"inputDigest": digest(setup_inputs(setup))} if digest is not None else {}),
            "timings": {"loadMs": round((loaded - started) * 1000), "runMs": round((time.monotonic() - loaded) * 1000)}}


def result_identity(setup: BacktestSetup, fee_schedule: dict[str, Any]) -> dict[str, Any]:
    """The fields ``execute`` adds around ``run_backtest``'s report (both engines' results get them)."""
    return {"symbol": setup.symbol, "timeframeSeconds": setup.plan.timeframeSeconds, "planName": setup.plan.name,
            "feeSource": fee_schedule.get("source", "account")}


def card_payload(result: dict[str, Any], *, turn_id: str, task_id: str) -> dict[str, Any]:
    """The conversation card: the same result with the trade list trimmed to the latest 200."""
    trades = result.get("trades") or []
    return {**result, "turnId": turn_id, "taskId": task_id, "trades": trades[-200:],
            "tradesTotal": result["summary"]["trades"]}


# -- running it off the Runtime: worker processes and the queue in front of them ------------------------------

# The limits (global 2 / per user 1 / 60 s wait / worker count) come from ``backtest_settings``
# (AI_TRADER_BACKTEST_*), read when the queue and the worker pool are built.
QUEUE_TIMEOUT_MESSAGE = "回测排队中，稍后再试"

_workers: ProcessPoolExecutor | None = None
_workers_lock = threading.Lock()


def _exit_with_parent() -> None:
    """Worker initializer: end with the Runtime. A worker holds its own end of the task queue, so a
    Runtime that exits without shutting the pool down (killed, the desktop watchdog's os._exit)
    would otherwise leave it running."""
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch() -> None:
        parent.join()
        os._exit(0)
    threading.Thread(target=watch, name="backtest-worker-parent", daemon=True).start()


def _worker_pool() -> ProcessPoolExecutor:
    """``AI_TRADER_BACKTEST_WORKERS`` spawned worker processes (default: the global limit), started
    on first use and kept (the queue never asks for more at once). Spawned, not forked: the Runtime
    is multi-threaded."""
    global _workers
    with _workers_lock:
        if _workers is None:
            _workers = ProcessPoolExecutor(load_backtest_settings().workers, mp_context=multiprocessing.get_context("spawn"),
                                           initializer=_exit_with_parent)
        return _workers


def run_in_worker(setup: BacktestSetup) -> dict[str, Any]:
    """``run_backtest(setup)`` in a worker process (blocking: call it off the event loop). The same
    function on the same setup, so the result is the one a direct call gives. A worker that died
    (killed, out of memory) fails this backtest only; the next one starts a fresh pool."""
    global _workers
    pool = _worker_pool()
    try:
        return pool.submit(run_backtest, setup).result()
    except BrokenProcessPool as error:
        with _workers_lock:
            if _workers is pool:
                _workers = None
        pool.shutdown(wait=False, cancel_futures=True)
        raise RuntimeError("backtest_worker_failed") from error


async def run_callable_in_worker(fn: Callable[..., Any], *args: Any) -> Any:
    """``fn(*args)`` awaited in a worker process of the same pool — ``run_in_worker`` for callers on
    the event loop (the cloud lab fallback), with the same broken-pool handling: a worker that died
    fails this one job only; the next submission starts a fresh pool."""
    global _workers
    pool = _worker_pool()
    try:
        return await asyncio.wrap_future(pool.submit(fn, *args))
    except BrokenProcessPool as error:
        with _workers_lock:
            if _workers is pool:
                _workers = None
        pool.shutdown(wait=False, cancel_futures=True)
        raise RuntimeError("backtest_worker_failed") from error


class BacktestQueueTimeout(RuntimeError):
    """Waited the queue's ``wait_seconds`` without a free slot."""


@dataclass(eq=False)
class QueueTicket:
    user: str
    request_id: str | None
    label: dict[str, Any]
    granted: asyncio.Future[None]
    queued_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    state: str = "queued"          # queued → running
    work: asyncio.Task[Any] | None = None


class BacktestQueue:
    """先进先出的回测队列 (event-loop only): a request starts when fewer than ``global_limit`` backtests
    run and its user runs fewer than ``per_user_limit``; otherwise it waits in arrival order — a
    user's second request never holds up other users behind it. After ``wait_seconds`` in the queue
    it gives up with BacktestQueueTimeout. ``items`` is what 排队中（前面还有 N 个）shows."""

    def __init__(self, *, global_limit: int | None = None, per_user_limit: int | None = None,
                 wait_seconds: float | None = None) -> None:
        configured = load_backtest_settings()
        self.global_limit = configured.global_limit if global_limit is None else global_limit
        self.per_user_limit = configured.user_limit if per_user_limit is None else per_user_limit
        self.wait_seconds = configured.queue_wait_seconds if wait_seconds is None else wait_seconds
        self._waiting: list[QueueTicket] = []
        self._running: list[QueueTicket] = []
        self._closed = False

    def _dispatch(self) -> None:
        if self._closed:
            return
        for ticket in list(self._waiting):
            if len(self._running) >= self.global_limit:
                return
            if sum(1 for item in self._running if item.user == ticket.user) >= self.per_user_limit:
                continue
            self._waiting.remove(ticket)
            ticket.state = "running"
            self._running.append(ticket)
            if not ticket.granted.done():
                ticket.granted.set_result(None)

    def _release(self, ticket: QueueTicket) -> None:
        if ticket in self._waiting:
            self._waiting.remove(ticket)
        if ticket in self._running:
            self._running.remove(ticket)
        self._dispatch()

    def _work_done(self, ticket: QueueTicket, work: asyncio.Task[Any]) -> None:
        # A disconnected caller no longer observes the result, including errors.
        if not work.cancelled():
            work.exception()
        self._release(ticket)

    async def shutdown(self) -> None:
        self._closed = True
        for ticket in self._waiting:
            ticket.granted.cancel()
        work = [ticket.work for ticket in self._running if ticket.work is not None]
        if work:
            await asyncio.gather(*work, return_exceptions=True)

    @asynccontextmanager
    async def slot(self, user: str, *, request_id: str | None = None,
                   label: dict[str, Any] | None = None) -> AsyncIterator[QueueTicket]:
        if self._closed:
            raise RuntimeError("backtest_queue_closed")
        ticket = QueueTicket(user, request_id, dict(label or {}), asyncio.get_running_loop().create_future())
        self._waiting.append(ticket)
        self._dispatch()
        try:
            if ticket.state != "running":
                try:
                    await asyncio.wait_for(asyncio.shield(ticket.granted), self.wait_seconds)
                except TimeoutError:
                    if ticket.state != "running":
                        raise BacktestQueueTimeout(QUEUE_TIMEOUT_MESSAGE) from None
            yield ticket
        finally:
            if ticket.work is not None and not ticket.work.done():
                ticket.work.add_done_callback(lambda work: self._work_done(ticket, work))
            else:
                self._release(ticket)

    def ahead(self, ticket: QueueTicket) -> int:
        """Backtests before this one: those running now and those queued earlier."""
        return len(self._running) + self._waiting.index(ticket) if ticket in self._waiting else 0

    def items(self, user: str, request_id: str | None = None) -> list[dict[str, Any]]:
        """This user's backtests in the queue or running (nobody else's)."""
        return [{"requestId": ticket.request_id, "state": ticket.state, "ahead": self.ahead(ticket),
                 "queuedAt": ticket.queued_at, **ticket.label}
                for ticket in (*self._running, *self._waiting)
                if ticket.user == user and (request_id is None or ticket.request_id == request_id)]

    def limits(self) -> dict[str, Any]:
        return {"global": self.global_limit, "perUser": self.per_user_limit, "waitSeconds": self.wait_seconds,
                "running": len(self._running), "queued": len(self._waiting)}
