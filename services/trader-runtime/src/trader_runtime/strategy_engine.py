"""Strategy engine: the one bar-close judgement shared by live monitoring and backtests.

纯函数接口契约（回测器与实盘共用）
====================================

All functions here are pure: the same arguments always give the same result. They never read
the wall clock, the database, a provider or the network; every time they need arrives as an
argument (``MarketContext.now`` or the decision bar's own close).

Judgement only happens on completed bars ("按收盘确认"). The one intra-bar act is 盘中触价 (an entry
group's ``trigger``): the bar-close decision arms a level, and the open happens inside the next bar only
if the price reaches it (live: the monitor's poll of the latest price; a backtest: that bar's range).
One position per strategy instance, built from multiple fills: an initial open is only proposed while
flat, and while the position is held the same decision may scale out (分批止盈, a partial close) or add
(加仓, a same-direction increase only in profit — never averaging down). A close and an open never happen
on the same decision (no same-bar reversal), and an add is only proposed when that bar decided no exit;
the priority is rule exit > time exit > 分批平仓 > 加仓.

Inputs
------
``plan``             A validated ``StrategyPlan`` (``check_executable`` passed, concrete clocks).
``bars_by_timeframe`` ``{seconds: [bar, ...]}`` of **completed** K-lines, oldest first, each bar
                     ``{"datetime": ns, "open", "high", "low", "close", "volume", "tradingDay"?}``.
                     The last bar of ``plan.timeframeSeconds`` is the decision bar; slower series may
                     run ahead in time (only bars finished by the decision time are read). The 86400
                     series may include the current trading day's bar: only its open is read (今开).
                     Daily anchors need the 86400 series; give ~N+2 trading days of it.
``state``            ``PositionState`` — the strategy's own position before this bar.
``market``           ``MarketContext`` — price tick, contract multiplier, account equity (None when
                     unknown or stale: ATR sizing then refuses to open) and the decision time.
``schedule``         ``Quote.trading_time`` as ``{"day": [[start, end], ...], "night": [...]}`` (CST).

Outputs: ``Decision``
---------------------
``action``        hold / open_long / open_short / close_long / close_short
``volume``        lots to open or close (0 for hold)
``reason``        Chinese explanation (why it acts, or why an entry signal was not taken)
``exitReason``    rule / stop_loss / take_profit / trailing_stop / breakeven / close_before_end /
                  spread_stop / spread_take / max_hold / time_take_profit / scale_out
``exitKey``       once-only identity of a time exit or 分批止盈 tier (close_before_end / spread_stop /
                  spread_take / max_hold / time_take_profit / scale_out); the caller puts it into
                  ``PositionState.fired_exits`` once acted on, so it never fires again
``fillPrice``     only from ``step`` for a protective hit inside the bar: the stop / take price, or
                  the bar's open when the bar gapped through it
``entryStopDistance`` for opens with ``atrStop``: signal-bar ATR × multiple (price units)
``entryAtr``      for opens: the signal bar's ATR used by the ATR chandelier trail
``stopPrice`` / ``takeProfitPrice`` for opens: the levels from the decision bar's close (a preview —
                  after the fill use ``open_position``); for a held position: the levels after this
                  bar (trailing already applied)
``trailExtreme``  highest (long) / lowest (short) price since entry after this bar
``alignmentId``   for opens of a direction-segment entry: the segment's identity (one attempt per
                  segment — live the store remembers it, a backtest keeps the used ids)
``exitAtr``       for opens with 保本 / 分段止盈 in ATR: the signal bar's ATR14, frozen with the entry
``entryTrigger``  for opens of an entry group with 盘中触价: ``{"kind": stop|limit, "price"}`` — the level (on
                  the tick grid, rounded so it is never easier to reach) the open waits for inside the next
                  execution bar; the preview stop / take are measured from it. Not reached → no open
``alternate``     双向触价 (OCO): both entry groups hold and both carry a trigger — the decision is the side
                  whose level is nearer the reference price (live: the one the latest price reached), and
                  ``alternate`` the other side's full open decision. Whichever level is reached first
                  fills; the other lapses with it
``add``          加仓: this ``open_long`` / ``open_short`` increases an existing same-direction position
                  (lots = ``volume``); the fill merges into it — a volume-weighted average price, the
                  first open's time and position key kept, the stop tightened to the latest fill when
                  ``pyramiding.moveStop``
``state``         ``step`` only: the position state after this bar (extremes, stop, bars held)

Second-batch blocks (策略积木·第三期 A, 2026-09-25)
-----------------------------------------------------
* 保本 (``plan.breakeven``): once the best price since entry is the trigger in profit, the stop moves
  to entry ± offset ticks. Like the trail it only tightens (the tighter of the two stands); live it
  runs tick by tick in ``autonomy._trail_stop``, a backtest applies it from each bar's extreme in
  ``step`` after the protective check. A hit is ``exitReason`` breakeven.
* 分段止盈 (``plan.timeTakeProfit``): judged at the bar close in ``decide`` like 最长持仓 — the tier
  with the largest ``afterBars`` ≤ bars held, the close's profit against its target; once per position.
* 复位 (``EntryGroup.rearm``): after an executed open (``PositionState.last_open_long/short`` = its
  signal bar; live the latest dispatched open attempt) a new entry in that direction waits until the
  rearm conditions held on a later bar (``engine.rearm``: the latest such bar in the live window).

Typical backtest loop (fills at the next bar's open)::

    state = PositionState()
    for each completed execution bar k:
        decision = step(plan, windows_up_to_k, state, MarketContext(tick, multiplier, equity), schedule=...)
        if decision.action.startswith("close"):   # fill at decision.fillPrice or next open
            state = flat_after_close(state, decision.exitKey)
        elif decision.action.startswith("open"):  # fill at bar k+1 open
            state = open_position(plan, decision, fill_price=open_k1, fill_time=time_k1,
                                  price_tick=tick, previous=state)
        else:
            state = decision.state or state

Live monitoring calls ``decide`` with the provider's evaluation (``strategy_rules.evaluate_plan``)
and a ``PositionState`` rebuilt from the account's positions and the strategy's armed protection;
the real fills and protective orders stay on the existing Runtime chain.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal

from .risk import ceil_to_tick, floor_to_tick
from .strategy_rules import (
    EXIT_ATR_PERIOD,
    Breakeven,
    SeriesWindow,
    StrategyPlan,
    TrailingStop,
    describe_condition,
    evaluate_completed,
    rearm_conditions,
    rule_position_intent,
    trigger_of,
    uses_exit_atr,
)
from .trading_calendar import NS, is_night, minutes_of_day, session_position

Side = Literal["long", "short"]


@dataclass(frozen=True)
class PositionState:
    direction: Literal["flat", "long", "short"] = "flat"
    volume: int = 0
    entry_price: float | None = None
    entry_time: int | None = None          # ns of the entry fill
    highest: float | None = None           # highest price since entry
    lowest: float | None = None            # lowest price since entry
    bars_held: int = 0                     # completed execution bars held, the decision bar included
    bars_pending: bool = False              # full history since entry has not arrived yet
    stop_price: float | None = None
    take_profit_price: float | None = None
    entry_atr: float | None = None         # signal-bar ATR for the chandelier trail
    trailing_active: bool = False          # the trail has moved the stop at least once
    entries_long: int = 0                  # opens on `trading_day`, per direction
    entries_short: int = 0
    trading_day: str | None = None         # the day the entry counters belong to (None = current)
    fired_exits: frozenset[str] = field(default_factory=frozenset)
    position_key: str | None = None        # stable identity of this position (protection id / entry time)
    breakeven_active: bool = False         # 保本 has moved the stop at least once
    exit_atr: float | None = None          # signal-bar ATR14 for 保本 / 分段止盈 / 分批止盈 in ATR
    last_open_long: int | None = None      # 复位: signal-bar ns of the latest executed open, per direction
    last_open_short: int | None = None
    adds: int = 0                          # 加仓 fills since the initial open
    initial_volume: int = 0                # the initial open's lots (the default add size)
    peak_volume: int = 0                   # largest volume held (initial open + adds)
    last_entry_price: float | None = None  # the latest fill's price (加仓 measures profit from it)
    scaled_out: bool = False               # 分批止盈 has closed part of this position (no adds after)
    stop_distance: float | None = None     # the initial stop's distance from its fill (structural 加仓 re-anchors)


@dataclass(frozen=True)
class MarketContext:
    price_tick: float
    volume_multiple: float
    equity: float | None = None
    now: int | None = None                 # decision time ns; None = the decision bar's close
    price: float | None = None             # live latest price (双向触价 picks the side it reached); None at a bar close
    # 按资金比例定手数 (2026-10-09): strategy_funds.compute_budget's payload — the shared core's
    # verdict on how many lots this entry may still open (live: fresh account/market facts; a
    # backtest: the same core off its own book). None when the plan does not size off equity.
    funds: dict[str, Any] | None = None


@dataclass(frozen=True)
class Decision:
    action: str
    volume: int
    reason: str
    exitReason: str | None = None
    exitKey: str | None = None
    fillPrice: float | None = None
    entryStopDistance: float | None = None
    entryAtr: float | None = None
    stopPrice: float | None = None
    takeProfitPrice: float | None = None
    trailExtreme: float | None = None
    structure: dict[str, Any] | None = None
    tradingDay: str | None = None
    barTime: str | None = None
    blocked: tuple[str, ...] = ()
    alignmentId: str | None = None
    exitAtr: float | None = None
    entryTrigger: dict[str, Any] | None = None
    alternate: Decision | None = None
    add: bool = False                      # 加仓: this open increases an existing same-direction position
    state: PositionState | None = None
    position: dict[str, Any] = field(default_factory=dict)   # rule_position_intent shape, for intents

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["blocked"] = list(self.blocked)
        if self.state is not None:
            result["state"] = {**asdict(self.state), "fired_exits": sorted(self.state.fired_exits)}
        return result


# -- price levels (Decimal, rounded away from the position like the live protection) ----------------

def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_stop(price: Any, tick: Any, *, long: bool) -> Decimal:
    """A stop rounds away from the position: down for a long, up for a short."""
    price, tick = _decimal(price), _decimal(tick)
    return floor_to_tick(price, tick) if long else ceil_to_tick(price, tick)


def stop_from_distance(entry: Any, distance: Any, tick: Any, *, long: bool) -> Decimal:
    entry, distance = _decimal(entry), _decimal(distance)
    return round_stop(entry - distance if long else entry + distance, tick, long=long)


def trigger_level(level: Any, tick: Any, *, long: bool, kind: str) -> Decimal:
    """盘中触价 on the tick grid, never easier to reach than asked: a buy stop / sell limit rounds up,
    a sell stop / buy limit rounds down."""
    level, tick = _decimal(level), _decimal(tick)
    return ceil_to_tick(level, tick) if long == (kind == "stop") else floor_to_tick(level, tick)


def trigger_fill(entry_trigger: dict[str, Any], bar: dict[str, Any], *, long: bool) -> float | None:
    """Where an armed 盘中触价 fills inside `bar`: at its level, or at the open when the bar opened past it;
    None when the bar never reached it. A stop waits for the price to come to it from below (long) /
    above (short), a limit from above / below."""
    level, opening = float(entry_trigger["price"]), float(bar["open"])
    high, low = float(bar["high"]), float(bar["low"])
    if long == (entry_trigger["kind"] == "stop"):       # buy stop / sell limit: the price rises to it
        return max(opening, level) if high >= level else None
    return min(opening, level) if low <= level else None


def trigger_reached(entry_trigger: dict[str, Any], price: Any, *, long: bool) -> bool:
    """Live: whether the latest price has reached an armed 盘中触价."""
    level, current = _decimal(entry_trigger["price"]), _decimal(price)
    return current >= level if long == (entry_trigger["kind"] == "stop") else current <= level


def entry_levels(plan: StrategyPlan, side: Side, entry_price: Any, price_tick: Any, *,
                 stop_distance: Any = None, structure: dict[str, Any] | None = None) -> tuple[Decimal | None, Decimal | None]:
    """Initial (stop, take profit) for an entry at `entry_price`, as the live fill arms them:
    ATR stop → entry ∓ stop_distance (signal ATR × multiple); structural stop → one tick outside
    the structure; else stopLossTicks. None for the stop when the ATR distance is unavailable."""
    if plan.spread is not None:
        # 价差: no per-contract stop / take is armed (the spread has no tick path); spread.stopLoss /
        # takeProfit are judged on the close spread in _time_exit's slot.
        return None, None
    long = side == "long"
    entry, tick = _decimal(entry_price), _decimal(price_tick)
    stop: Decimal | None
    if plan.atrStop is not None:
        stop = None if stop_distance is None else stop_from_distance(entry, stop_distance, tick, long=long)
    elif structure is not None:
        stop = (round_stop(_decimal(structure["low"]), tick, long=True) - tick if long
                else round_stop(_decimal(structure["high"]), tick, long=False) + tick)
    else:
        stop = stop_from_distance(entry, tick * plan.stopLossTicks, tick, long=long)
    take = None
    if plan.takeProfitTicks is not None:
        raw = entry + tick * plan.takeProfitTicks if long else entry - tick * plan.takeProfitTicks
        take = round_stop(raw, tick, long=not long)
    return stop, take


def trail_stop(trailing: TrailingStop | dict[str, Any], *, long: bool, entry_price: Any, extreme: Any,
               price_tick: Any, current_stop: Any = None, entry_atr: Any = None) -> Decimal | None:
    """The trailing stop for a position whose best price since entry is `extreme`. Only tightens:
    never below (long) / above (short) `current_stop`. Before the profit reaches `activateAfter`
    (same unit as `value`) the current stop is kept. The ATR chandelier uses the signal-bar ATR."""
    config = trailing if isinstance(trailing, TrailingStop) else TrailingStop.model_validate(trailing)
    entry, best, tick = _decimal(entry_price), _decimal(extreme), _decimal(price_tick)
    current = None if current_stop is None else _decimal(current_stop)
    value, activate = Decimal(str(config.value)), Decimal(str(config.activateAfter))
    if config.mode == "percent":
        unit_distance, unit_activation = best * value / 100, entry * activate / 100
    elif config.mode == "ticks":
        unit_distance, unit_activation = tick * value, tick * activate
    else:
        if entry_atr is None:
            return current
        atr = _decimal(entry_atr)
        unit_distance, unit_activation = atr * value, atr * activate
    profit = best - entry if long else entry - best
    if profit < unit_activation:
        return current
    candidate = round_stop(best - unit_distance if long else best + unit_distance, tick, long=long)
    if current is None:
        return candidate
    return max(current, candidate) if long else min(current, candidate)


def breakeven_stop(breakeven: Breakeven | dict[str, Any], *, long: bool, entry_price: Any, extreme: Any,
                   price_tick: Any, current_stop: Any = None, exit_atr: Any = None) -> Decimal | None:
    """保本: once the best price since entry is `triggerTicks` ticks (or `triggerAtr` × the signal-bar
    ATR14) in profit, the stop goes to the entry price ± `offsetTicks` ticks (the profit side). Only
    tightens: never below (long) / above (short) `current_stop`; before the trigger it is kept."""
    config = breakeven if isinstance(breakeven, Breakeven) else Breakeven.model_validate(breakeven)
    entry, best, tick = _decimal(entry_price), _decimal(extreme), _decimal(price_tick)
    current = None if current_stop is None else _decimal(current_stop)
    if config.triggerTicks is not None:
        trigger = tick * config.triggerTicks
    elif exit_atr is not None:
        trigger = Decimal(str(config.triggerAtr)) * _decimal(exit_atr)
    else:
        return current
    if (best - entry if long else entry - best) < trigger:
        return current
    offset = tick * config.offsetTicks
    candidate = round_stop(entry + offset if long else entry - offset, tick, long=long)
    if current is None:
        return candidate
    return max(current, candidate) if long else min(current, candidate)


def atr_position_size(plan: StrategyPlan, atr: float | None, market: MarketContext) -> tuple[int, str | None]:
    """floor(equity × riskPercent% ÷ (multiplier × atrMultiple × ATR)), capped by maxPosition.
    (0, reason) when it cannot open."""
    sizing = plan.sizing
    if sizing is None or sizing.mode != "atr_risk":
        return plan.maxPosition, None
    if market.equity is None or market.equity <= 0:
        return 0, "账户权益不可用或不新鲜，按 ATR 定手数时不开仓。"
    if atr is None or atr <= 0 or market.volume_multiple <= 0:
        return 0, f"ATR{sizing.atrPeriod} 数据不足，按 ATR 定手数时不开仓。"
    risk = Decimal(str(market.equity)) * Decimal(str(sizing.riskPercent)) / 100
    per_lot = Decimal(str(market.volume_multiple)) * Decimal(str(sizing.atrMultiple)) * Decimal(str(atr))
    lots = int(risk // per_lot)
    if lots <= 0:
        return 0, (f"按 ATR 定手数不足 1 手（权益 {market.equity:g} × {sizing.riskPercent:g}% ÷ "
                   f"每手风险 {float(per_lot):g}），不开仓。")
    return min(lots, plan.maxPosition), None


def equity_position_size(plan: StrategyPlan, market: MarketContext) -> tuple[int, str | None]:
    """按资金比例 (2026-10-09): the shared funds core's lots for this entry. The budget already
    deducts the strategy's held lots and unfilled open orders, so this is the lots a fresh entry
    may still open — maxPosition does not apply (its old default of 1 must not silently cap the
    new mode), and a shrinking budget never reduces what is already held."""
    sizing = plan.sizing
    if sizing is None or sizing.mode != "equity_percent":
        return plan.maxPosition, None
    funds = market.funds
    if not isinstance(funds, dict):
        return 0, "资金数据不可用（权益、每手保证金或手续费缺失），按资金比例定手数时不开仓。"
    reason = funds.get("unavailableReason")
    if reason:
        return 0, str(reason)
    return int(funds.get("lots") or 0), None


def bars_held(bars: list[dict[str, Any]], entry_time: int, timeframe_seconds: int) -> int:
    """Completed bars that ended after the entry fill: the fill bar counts once it completes."""
    if isinstance(bars, SeriesWindow) and bars.facts.ordered:   # a backtest window: count by bisection
        start, end = 0, bars.start + len(bars)
        return end - bisect_right(bars.facts.times(), int(entry_time) - timeframe_seconds * NS, start, end)
    return sum(1 for bar in bars if int(bar["datetime"]) + timeframe_seconds * NS > int(entry_time))


# -- the bar-close decision ------------------------------------------------------------------------------

def _entry_window_block(plan: StrategyPlan, engine: dict[str, Any]) -> str | None:
    """开仓时段 / 夜盘 gates, shared by entries and 加仓. Exits are never gated."""
    bar_close = engine.get("barClose")
    schedule = engine.get("schedule")
    if bar_close is not None and plan.entryWindows:
        minute = minutes_of_day(int(bar_close))

        def inside(start: str, end: str) -> bool:
            first = int(start[:2]) * 60 + int(start[3:])
            last = int(end[:2]) * 60 + int(end[3:])
            return first <= minute <= last if first <= last else minute >= first or minute <= last
        if not any(inside(w.start, w.end) for w in plan.entryWindows):
            return "不在允许开仓的时段，本次不开仓；出场照常。"
    if bar_close is not None and not plan.allowNightEntries and is_night(int(bar_close), schedule):
        return "夜盘不开新仓；出场照常。"
    return None


def _entry_block(plan: StrategyPlan, side: Side, engine: dict[str, Any], state: PositionState) -> str | None:
    """Plan-level entry gates. Exits are never gated."""
    gate = _entry_window_block(plan, engine)
    if gate is not None:
        return gate
    if plan.maxEntriesPerDirection is not None:
        current_day = engine.get("tradingDay")
        same_day = state.trading_day is None or current_day is None or state.trading_day == current_day
        count = (state.entries_long if side == "long" else state.entries_short) if same_day else 0
        if count >= plan.maxEntriesPerDirection:
            return f"本交易日{'开多' if side == 'long' else '开空'}已达 {plan.maxEntriesPerDirection} 次，不再开仓。"
    name = "entryLong" if side == "long" else "entryShort"
    rearm = rearm_conditions(getattr(plan.rules, name))
    latch = state.last_open_long if side == "long" else state.last_open_short
    bar_time = engine.get("barTime")
    if rearm and latch is not None and bar_time is not None and latch < int(bar_time):
        # 复位: after an executed open this direction waits until the rearm conditions hold on a later bar.
        hit = (engine.get("rearm") or {}).get(name)
        if hit is None or hit <= latch:
            words = "开多" if side == "long" else "开空"
            return f"上次{words}后复位条件「{'且'.join(describe_condition(c) for c in rearm)}」还没成立，本次不{words}。"
    return None


def _time_take_profit(plan: StrategyPlan, state: PositionState, side: Side, engine: dict[str, Any],
                      price_tick: float, key_base: str) -> tuple[str, str, str] | None:
    """分段止盈: the tier with the largest afterBars ≤ bars held; exit when the decision bar's close is
    at least its profit from the entry. Once per position."""
    tiers = [target for target in plan.timeTakeProfit if target.afterBars <= state.bars_held]
    close = engine.get("close")
    if not tiers or close is None or state.entry_price is None:
        return None
    tier = max(tiers, key=lambda target: target.afterBars)
    if tier.profitTicks is not None:
        if price_tick <= 0:
            return None
        target = _decimal(price_tick) * tier.profitTicks
        words = f"{tier.profitTicks} 跳"
    elif state.exit_atr is not None:
        target = Decimal(str(tier.profitAtr)) * _decimal(state.exit_atr)
        words = f"{tier.profitAtr:g}×ATR{EXIT_ATR_PERIOD}"
    else:
        return None
    profit = _decimal(close) - _decimal(state.entry_price)
    if side == "short":
        profit = -profit
    key = f"time_take_profit:{key_base}"
    if profit < target or key in state.fired_exits:
        return None
    return ("time_take_profit", key,
            f"已持仓 {state.bars_held} 根K线，浮盈达到这一档的 {words}（满 {tier.afterBars} 根起），分段止盈平仓。")


def _spread_exit(spread, state: PositionState, side: Side, engine: dict[str, Any],
                 key_base: str) -> tuple[str, str, str] | None:
    """价差止损/止盈 (第一期): judged on the decision bar's close spread against the entry spread —
    a synthesized spread bar has no intrabar path to trigger off. Stop before take when a close is
    beyond both (the conservative reading). Once per position."""
    close, entry = engine.get("close"), state.entry_price
    if close is None or entry is None:
        return None
    profit = _decimal(close) - _decimal(entry)
    if side == "short":
        profit = -profit
    for kind, target in (("spread_stop", spread.stopLoss), ("spread_take", spread.takeProfit)):
        if target is None:
            continue
        key, amount = f"{kind}:{key_base}", Decimal(str(target))
        if key in state.fired_exits:
            continue
        if kind == "spread_stop" and profit <= -amount:
            return kind, key, f"收盘价差较开仓价差浮亏 {float(-profit):g} 点（止损 {target:g} 点），平掉价差持仓。"
        if kind == "spread_take" and profit >= amount:
            return kind, key, f"收盘价差较开仓价差浮盈 {float(profit):g} 点（止盈 {target:g} 点），平掉价差持仓。"
    return None


def _time_exit(plan: StrategyPlan, state: PositionState, side: Side, engine: dict[str, Any],
               now: int | None, price_tick: float = 0.0) -> tuple[str, str, str] | None:
    """(exitReason, exitKey, reason) of a due time exit that has not fired for this position."""
    key_base = state.position_key or f"{side}:{state.entry_time or 'position'}"
    if plan.closeBeforeEndMinutes is not None and now is not None:
        position = session_position(int(now), engine.get("schedule"))
        if position.in_session:
            session_scope = plan.closeBeforeEndScope == "session"
            left = position.minutes_to_session_end if session_scope else position.minutes_to_day_end
            if left is not None and left <= plan.closeBeforeEndMinutes:
                key = (f"close_before_end:{position.trading_day}:"
                       f"{position.session_end_ns if session_scope else 'day'}:{key_base}")
                if key not in state.fired_exits:
                    scope = "本交易时段" if session_scope else "当日收盘"
                    return "close_before_end", key, f"距{scope}不足 {plan.closeBeforeEndMinutes} 分钟，平掉持仓。"
    if plan.spread is not None:
        hit = _spread_exit(plan.spread, state, side, engine, key_base)
        if hit is not None:
            return hit
    # Even with older history still pending, the visible completed bars give a
    # safe lower bound: reaching the hold limit cannot be undone by more bars.
    if plan.maxHoldBars is not None and state.bars_held >= plan.maxHoldBars:
        key = f"max_hold:{key_base}"
        if key not in state.fired_exits:
            return "max_hold", key, f"已持仓 {state.bars_held} 根K线（上限 {plan.maxHoldBars} 根），平掉持仓。"
    if plan.timeTakeProfit and not state.bars_pending:
        return _time_take_profit(plan, state, side, engine, price_tick, key_base)
    return None


def _scale_out_exit(plan: StrategyPlan, state: PositionState, side: Side, engine: dict[str, Any],
                    price_tick: float, key_base: str) -> tuple[str, str, str, int] | None:
    """分批止盈: (exitReason, exitKey, reason, lots) of the first tier (near → far) this position has
    not yet fired, when the decision bar's close reaches its profit against the average entry price.
    The tier closes ``percent``% of the peak volume keeping at least one lot; with one lot left the
    tier is consumed (lots 0, the caller still records the key) without an order."""
    if not plan.scaleOut or state.entry_price is None:
        return None
    close = engine.get("close")
    if close is None:
        return None
    for index, tier in enumerate(plan.scaleOut):
        key = f"scale_out:{index}:{key_base}"
        if key in state.fired_exits:
            continue
        if tier.profitTicks is not None:
            if price_tick <= 0:
                return None
            target, words = _decimal(price_tick) * tier.profitTicks, f"{tier.profitTicks} 跳"
        elif state.exit_atr is not None:
            target = Decimal(str(tier.profitAtr)) * _decimal(state.exit_atr)
            words = f"{tier.profitAtr:g}×ATR{EXIT_ATR_PERIOD}"
        else:
            return None
        profit = _decimal(close) - _decimal(state.entry_price)
        if side == "short":
            profit = -profit
        if profit < target:
            return None
        lots = min(max(1, state.peak_volume * tier.percent // 100), state.volume - 1)
        reason = (f"浮盈达第 {index + 1} 档目标（{words}），分批止盈平 {lots} 手，剩余持仓保留原止损止盈。"
                  if lots > 0 else
                  f"浮盈达第 {index + 1} 档目标（{words}），只剩 1 手，这一档不再平，等止损止盈或规则出场。")
        return "scale_out", key, reason, lots
    return None


def _pyramid_add(plan: StrategyPlan, state: PositionState, side: Side, engine: dict[str, Any],
                 market: MarketContext, rules: dict[str, bool]) -> tuple[int, str] | None:
    """加仓: (lots, reason) of a same-direction add in profit, or None. Judged only when this bar
    decided no exit; never averages down, never after a scale-out, capped by maxAdds and maxPosition.
    The entry-window / night gates apply; the per-direction daily count, rearm and 盘中触价 do not."""
    pyramid = plan.pyramiding
    if (pyramid is None or state.last_entry_price is None or state.scaled_out
            or state.adds >= pyramid.maxAdds or _entry_window_block(plan, engine) is not None):
        return None
    lots = pyramid.volume if pyramid.volume is not None else (state.initial_volume or 1)
    if plan.sizing is not None and plan.sizing.mode == "equity_percent":
        # 按资金比例：加仓与首次开仓共用同一份预算——本次还能开的手数来自同一核心（已扣
        # 本策略持仓与挂单），再受用户填的 maxLots（不填不限）约束；预算数据缺失或不足
        # 1 手就不加仓，发送前还会按当时预算再核对。旧固定 / ATR 行为不变。
        funds = market.funds if isinstance(market.funds, dict) else None
        if funds is None or funds.get("unavailableReason") or int(funds.get("lots") or 0) <= 0:
            return None
        lots = min(lots, int(funds["lots"]))
        if plan.sizing.maxLots is not None:
            lots = min(lots, plan.sizing.maxLots - state.volume)
    else:
        lots = min(lots, plan.maxPosition - state.volume)
    if lots <= 0:
        return None
    close, last = engine.get("close"), _decimal(state.last_entry_price)
    if close is None:
        return None
    close_at, long = _decimal(close), side == "long"
    if pyramid.trigger == "signal":
        if not rules.get("entryLong" if long else "entryShort") or not (close_at > last if long else close_at < last):
            return None
        words = f"收盘 {close_at:g} 优于上次成交 {state.last_entry_price:g}，同向信号再成立"
    else:
        gain = close_at - last if long else last - close_at
        if pyramid.trigger == "profit_atr":
            atr = (engine.get("atr") or {}).get(str(pyramid.atrPeriod))
            if atr is None or atr <= 0:
                return None
            unit = f"{pyramid.step:g} 倍 ATR{pyramid.atrPeriod}"
        elif market.price_tick > 0:
            unit = f"{pyramid.step:g} 跳"
        else:
            return None
        if gain < Decimal(str(pyramid.step)) * (_decimal(atr) if pyramid.trigger == "profit_atr" else _decimal(market.price_tick)):
            return None
        words = f"收盘 {close_at:g} 比上次成交 {state.last_entry_price:g} {'高出' if long else '低出'} {unit}"
    return lots, f"加仓第 {state.adds + 1} 次：{words}，加 {lots} 手。"


def decide(plan: StrategyPlan, evaluation: dict[str, Any], state: PositionState, market: MarketContext) -> Decision:
    """The bar-close judgement from an evaluation (``strategy_rules.evaluate_rules`` /
    ``evaluate_completed``): rule exits first, then due time exits, then gated and sized entries.
    Protective stop / take hits are not judged here — live they run tick by tick on the armed
    protection, in a backtest ``step`` checks them against the bar's range first."""
    engine = evaluation.get("engine") or {}
    rules = dict(evaluation["rules"])
    long = state.volume if state.direction == "long" else 0
    short = state.volume if state.direction == "short" else 0
    now = market.now if market.now is not None else engine.get("barClose")
    exit_reason = exit_key = reason = None
    blocked: list[str] = []
    maximum = plan.maxPosition
    atr = engine.get("atr") or {}
    alternate: Decision | None = None
    add_volume = 0
    partial_volume: int | None = None
    if long or short:
        side: Side = "long" if long else "short"
        name = "exitLong" if long else "exitShort"
        if rules[name]:
            exit_reason = "rule"
        else:
            timed = _time_exit(plan, state, side, engine, now, market.price_tick)
            if timed is not None:
                exit_reason, exit_key, reason = timed
                rules[name] = True
        if exit_reason is None:
            # 分批平仓 then 加仓: each only judged when this bar decided no exit at all.
            key_base = state.position_key or f"{side}:{state.entry_time or 'position'}"
            scale = _scale_out_exit(plan, state, side, engine, market.price_tick, key_base)
            if scale is not None:
                tier_reason, exit_key, reason, partial_volume = scale
                if partial_volume > 0:
                    exit_reason, rules[name] = tier_reason, True
                # else: the last lot skips this tier — the key is still returned so the caller
                # records the tier as fired without an order.
            else:
                added = _pyramid_add(plan, state, side, engine, market, rules)
                if added is not None:
                    add_volume, reason = added
    else:
        for entry_side, name in (("long", "entryLong"), ("short", "entryShort")):
            if rules[name]:
                gate = _entry_block(plan, entry_side, engine, state)  # type: ignore[arg-type]
                if gate is not None:
                    rules[name] = False
                    blocked.append(gate)
        dual = _dual_trigger(plan, evaluation, rules, market)
        if dual is not None:
            primary, other = dual
            alternate = decide(plan, {**evaluation, "rules": {**rules, primary: False}}, state, replace(market, price=None))
            if not alternate.action.startswith("open"):
                alternate = None
            rules[other] = False
        wants = (plan.direction in {"long", "both"} and rules["entryLong"]) != (
            plan.direction in {"short", "both"} and rules["entryShort"])
        equity_sizing = plan.sizing is not None and plan.sizing.mode == "equity_percent"
        if wants and plan.sizing is not None and plan.sizing.mode == "atr_risk":
            maximum, sizing_block = atr_position_size(plan, atr.get(str(plan.sizing.atrPeriod)), market)
            if sizing_block is not None:
                rules["entryLong"] = rules["entryShort"] = False
                blocked.append(sizing_block)
        elif wants and equity_sizing:
            # 按资金比例：预算已扣本策略持仓与挂单，这里就是本次还能开的手数；比例缩小不减已有持仓。
            maximum, sizing_block = equity_position_size(plan, market)
            if sizing_block is not None:
                rules["entryLong"] = rules["entryShort"] = False
                blocked.append(sizing_block)
        if plan.pyramiding is not None and not equity_sizing:
            # 加仓的首笔开仓给加仓留空间：按每次加仓手数（缺省 1 手）× 最多次数预留，加满才到最多手数。
            maximum = max(1, min(maximum, plan.maxPosition - (plan.pyramiding.volume or 1) * plan.pyramiding.maxAdds))
    position = rule_position_intent(rules, plan.direction, long, short, maximum)
    if add_volume > 0:
        # 加仓: the intent opens `add_volume` more lots on top of the held same-direction position.
        position = {"action": "open_long" if long else "open_short", "requestedVolume": add_volume,
                    "currentLong": long, "currentShort": short,
                    "targetLong": long + add_volume if long else 0,
                    "targetShort": short + add_volume if short else 0, "reason": str(reason)}
    elif partial_volume:
        # 分批止盈: only this tier's lots close; the rest keeps its protection and exits.
        position["requestedVolume"] = partial_volume
        position["targetLong" if long else "targetShort"] = (long or short) - partial_volume
    action = str(position["action"])
    if reason is not None and (action.startswith("close") or exit_key is not None):
        position["reason"] = reason
    elif action == "hold" and evaluation.get("dataPending"):
        position["reason"] = str(evaluation["dataPending"])   # 数据未就绪：本次不判定规则
    elif action == "hold" and blocked:
        position["reason"] = blocked[0]
    elif action.startswith("open") and not add_volume and plan.sizing is not None and plan.sizing.mode == "atr_risk":
        position["reason"] = f"条件已满足；按 ATR 风险定为 {position['requestedVolume']} 手"
    elif action.startswith("open") and not add_volume and plan.sizing is not None and plan.sizing.mode == "equity_percent":
        position["reason"] = f"条件已满足；按资金比例定为 {position['requestedVolume']} 手"
    decision: dict[str, Any] = {}
    if action.startswith("open"):
        side = "long" if action == "open_long" else "short"
        adding = add_volume > 0   # an add: no 盘中触价 / alignment, the position's frozen exit ATR
        structure = (evaluation.get("structures") or {}).get("entryLong" if side == "long" else "entryShort")
        stop_atr = atr.get(str(plan.atrStop.period)) if plan.atrStop else None
        trail_atr = (atr.get(str(plan.trailingStop.atrPeriod))
                     if plan.trailingStop and plan.trailingStop.mode == "atr_chandelier" else None)
        distance = plan.atrStop.multiple * stop_atr if plan.atrStop is not None and stop_atr is not None else None
        close = engine.get("close")
        trigger = trigger_of(getattr(plan.rules, "entryLong" if side == "long" else "entryShort"))
        entry_trigger = None
        if trigger is not None and not adding:   # 加仓不挂盘中触价
            level = (evaluation.get("triggers") or {}).get("entryLong" if side == "long" else "entryShort")
            if level is not None and market.price_tick > 0:
                entry_trigger = {"kind": trigger.kind, "price": float(trigger_level(level, market.price_tick,
                                                                                    long=side == "long", kind=trigger.kind))}
                close = entry_trigger["price"]      # the preview stop / take from where the open would fill
        stop = take = None
        if close is not None and market.price_tick > 0:
            stop, take = entry_levels(plan, side, close, market.price_tick, stop_distance=distance, structure=structure)
        alignment = (evaluation.get("entryAlignments") or {}).get("entryLong" if side == "long" else "entryShort")
        exit_atr = atr.get(str(EXIT_ATR_PERIOD)) if uses_exit_atr(plan) else None
        if adding:
            exit_atr = state.exit_atr   # the position froze its ATRs at the initial open
        decision = {"entryStopDistance": distance, "entryTrigger": entry_trigger,
                    "entryAtr": trail_atr, "structure": structure, "exitAtr": exit_atr,
                    "alignmentId": alignment.get("id") if isinstance(alignment, dict) and not adding else None,
                    "stopPrice": float(stop) if stop is not None else None,
                    "takeProfitPrice": float(take) if take is not None else None}
        missing = ("触价价位数据不足，本根不挂触价单。" if trigger is not None and entry_trigger is None
                   else f"ATR{plan.atrStop.period} 数据不足，无法确定 ATR 止损，不开仓。" if plan.atrStop is not None and stop_atr is None
                   else f"ATR{EXIT_ATR_PERIOD} 数据不足，无法确定保本 / 分段止盈的距离，不开仓。"
                   if uses_exit_atr(plan) and exit_atr is None else None)
        if missing is not None:
            position = rule_position_intent({**rules, "entryLong": False, "entryShort": False},
                                            plan.direction, long, short, maximum)
            position["reason"] = missing
            action, decision = "hold", {}
            blocked.append(position["reason"])
    elif long or short:
        decision = {"stopPrice": state.stop_price, "takeProfitPrice": state.take_profit_price,
                    "trailExtreme": state.highest if long else state.lowest}
    return Decision(action=action, volume=int(position["requestedVolume"]), reason=str(position["reason"]),
                    exitReason=exit_reason if action.startswith("close") else None,
                    exitKey=exit_key,   # set for an acted-on close or 分批止盈's one-lot skip (a hold)
                    tradingDay=engine.get("tradingDay"), barTime=engine.get("barTime") or evaluation.get("evaluatedBarTime"),
                    blocked=tuple(blocked), position=position,
                    add=bool(add_volume) and action.startswith("open"),
                    alternate=alternate if action.startswith("open") else None, **decision)


def _dual_trigger(plan: StrategyPlan, evaluation: dict[str, Any], rules: dict[str, Any],
                  market: MarketContext) -> tuple[str, str] | None:
    """双向触价 (OCO): (the entry group to decide, the other one) when both hold and both carry a trigger
    with a known level — the one the live price reached, else the one nearer the decision bar's close
    (a tie goes long; the close, not the moving price, so the unreached choice stays put within the bar)."""
    long_trigger, short_trigger = trigger_of(plan.rules.entryLong), trigger_of(plan.rules.entryShort)
    if (plan.direction != "both" or not rules["entryLong"] or not rules["entryShort"]
            or long_trigger is None or short_trigger is None or market.price_tick <= 0):
        return None
    levels = evaluation.get("triggers") or {}
    reference = (evaluation.get("engine") or {}).get("close")
    if levels.get("entryLong") is None or levels.get("entryShort") is None or reference is None:
        return None
    high = trigger_level(levels["entryLong"], market.price_tick, long=True, kind=long_trigger.kind)
    low = trigger_level(levels["entryShort"], market.price_tick, long=False, kind=short_trigger.kind)
    up: dict[str, Any] = {"kind": long_trigger.kind, "price": high}
    down: dict[str, Any] = {"kind": short_trigger.kind, "price": low}
    if market.price is not None and trigger_reached(down, market.price, long=False) and not trigger_reached(up, market.price, long=True):
        return "entryShort", "entryLong"
    if market.price is not None and trigger_reached(up, market.price, long=True):
        return "entryLong", "entryShort"
    nearer_short = abs(_decimal(reference) - low) < abs(_decimal(reference) - high)
    return ("entryShort", "entryLong") if nearer_short else ("entryLong", "entryShort")


def _protective_hit(state: PositionState, bar: dict[str, Any]) -> tuple[str, float] | None:
    """Stop before take when one bar touches both (the conservative reading)."""
    long = state.direction == "long"
    opening, high, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
    stop, take = state.stop_price, state.take_profit_price
    kind = "trailing_stop" if state.trailing_active else "breakeven" if state.breakeven_active else "stop_loss"
    if stop is not None:
        if long and low <= stop:
            return kind, min(opening, stop)
        if not long and high >= stop:
            return kind, max(opening, stop)
    if take is not None:
        if long and high >= take:
            return "take_profit", max(opening, take)
        if not long and low <= take:
            return "take_profit", min(opening, take)
    return None


def step(plan: StrategyPlan, bars_by_timeframe: dict[int, list[dict[str, Any]]], state: PositionState,
         market: MarketContext, *, daily: list[dict[str, Any]] | None = None,
         schedule: dict[str, Any] | None = None,
         extended: dict[int, list[dict[str, Any]]] | None = None,
         observe: Callable[[dict[str, Any]], None] | None = None) -> Decision:
    """One completed execution bar of a backtest: the armed stop / take against this bar's range,
    then the trail from this bar's extreme, then ``decide``. Deterministic; see the module contract.
    `observe` (the backtester's 体检) sees the rule evaluation of this bar; it changes nothing."""
    evaluation = evaluate_completed(plan, bars_by_timeframe, daily=daily, schedule=schedule, extended=extended)
    if observe is not None:
        observe(evaluation)
    base = bars_by_timeframe[plan.timeframeSeconds]
    bar = base[-1]
    engine = evaluation["engine"]
    if state.direction == "flat" or state.volume <= 0:
        return decide(plan, evaluation, state, market)
    long = state.direction == "long"
    hit = _protective_hit(state, bar)
    if hit is not None:
        exit_reason, price = hit
        text = {"stop_loss": "触及止损", "trailing_stop": "触及追踪止损", "breakeven": "触及保本止损",
                "take_profit": "触及止盈"}[exit_reason]
        return Decision(action="close_long" if long else "close_short", volume=state.volume,
                        reason=f"{text} {price:g}，平掉持仓。", exitReason=exit_reason, fillPrice=price,
                        stopPrice=state.stop_price, takeProfitPrice=state.take_profit_price,
                        trailExtreme=state.highest if long else state.lowest,
                        tradingDay=engine["tradingDay"], barTime=engine["barTime"], state=state)
    highest = max(x for x in (state.highest, float(bar["high"])) if x is not None)
    lowest = min(x for x in (state.lowest, float(bar["low"])) if x is not None)
    stop = state.stop_price
    trailing_active = state.trailing_active
    if plan.trailingStop is not None and state.entry_price is not None:
        trailed = trail_stop(plan.trailingStop, long=long, entry_price=state.entry_price,
                             extreme=highest if long else lowest, price_tick=market.price_tick,
                             current_stop=stop, entry_atr=state.entry_atr)
        if trailed is not None and (stop is None or float(trailed) != stop):
            stop, trailing_active = float(trailed), True
    breakeven_active = state.breakeven_active
    if plan.breakeven is not None and state.entry_price is not None:
        # 保本 and the trail both only tighten, so the stronger of the two stands.
        moved = breakeven_stop(plan.breakeven, long=long, entry_price=state.entry_price,
                               extreme=highest if long else lowest, price_tick=market.price_tick,
                               current_stop=stop, exit_atr=state.exit_atr)
        if moved is not None and (stop is None or float(moved) != stop):
            stop, breakeven_active = float(moved), True
    held = bars_held(base, state.entry_time, plan.timeframeSeconds) if state.entry_time is not None else state.bars_held
    current = replace(state, highest=highest, lowest=lowest, stop_price=stop, trailing_active=trailing_active,
                      breakeven_active=breakeven_active, bars_held=held)
    decision = decide(plan, evaluation, current, market)
    if decision.exitKey is not None and not decision.action.startswith("close"):
        # 分批止盈's one-lot skip: the tier is consumed (recorded) without an order.
        current = replace(current, fired_exits=current.fired_exits | {decision.exitKey}, scaled_out=True)
    return replace(decision, state=current)


def open_position(plan: StrategyPlan, decision: Decision, *, fill_price: float, fill_time: int,
                  price_tick: float, previous: PositionState | None = None, volume: int | None = None) -> PositionState:
    """The position after an open fill, with the protection the live chain would arm: the stop
    from the fill price (ATR distance / structure / ticks), the take profit, and the counters.
    An add (``decision.add``) merges into `previous` instead: a volume-weighted average price, the
    first open's time / key / counters kept, and — with ``pyramiding.moveStop`` — the stop the
    tighter of the current one and the one this fill's original stop rule gives (a structural
    initial stop re-anchors at its initial distance; the add bar carries no structure), the take
    profit recomputed from this fill."""
    side: Side = "long" if decision.action == "open_long" else "short"
    long = side == "long"
    stop, take = entry_levels(plan, side, fill_price, price_tick, stop_distance=decision.entryStopDistance,
                              structure=decision.structure)
    base = previous or PositionState()
    if decision.add and base.direction == side and base.volume > 0:
        lots = volume if volume is not None else decision.volume
        stop_price = float(stop) if stop is not None else None
        take_price = float(take) if take is not None else None
        if plan.atrStop is None and base.stop_distance is not None:
            stop_price = float(stop_from_distance(fill_price, base.stop_distance, price_tick, long=long))
        moves = plan.pyramiding is not None and plan.pyramiding.moveStop
        if moves and stop_price is not None and base.stop_price is not None:
            stop_price = max(base.stop_price, stop_price) if long else min(base.stop_price, stop_price)
        elif not moves or stop_price is None:
            stop_price = base.stop_price
        take_price = take_price if moves else base.take_profit_price
        total = base.volume + lots
        average = ((base.entry_price or 0.0) * base.volume + fill_price * lots) / total
        return replace(base, direction=side, volume=total, entry_price=average,
                       highest=max(x for x in (base.highest, fill_price) if x is not None),
                       lowest=min(x for x in (base.lowest, fill_price) if x is not None),
                       stop_price=stop_price, take_profit_price=take_price,
                       adds=base.adds + 1, peak_volume=max(base.peak_volume, total),
                       last_entry_price=fill_price)
    same_day = base.trading_day is None or base.trading_day == decision.tradingDay
    entries_long = (base.entries_long if same_day else 0) + (side == "long")
    entries_short = (base.entries_short if same_day else 0) + (side == "short")
    signal = int(decision.barTime) if decision.barTime is not None and decision.barTime.isdigit() else None
    lots = volume if volume is not None else decision.volume
    return PositionState(direction=side, volume=lots,
                         entry_price=fill_price, entry_time=fill_time, highest=fill_price, lowest=fill_price,
                         stop_price=float(stop) if stop is not None else None,
                         take_profit_price=float(take) if take is not None else None,
                         entry_atr=decision.entryAtr, entries_long=entries_long, entries_short=entries_short,
                         trading_day=decision.tradingDay, fired_exits=base.fired_exits,
                         position_key=f"{side}:{fill_time}", exit_atr=decision.exitAtr,
                         last_open_long=signal if side == "long" else base.last_open_long,
                         last_open_short=signal if side == "short" else base.last_open_short,
                         initial_volume=lots, peak_volume=lots, last_entry_price=fill_price,
                         stop_distance=abs(fill_price - float(stop)) if stop is not None else None)


def flat_after_close(state: PositionState, exit_key: str | None = None) -> PositionState:
    """The state once a position is closed: flat, keeping what outlives a position — today's entry
    counters, the time exits already acted on and the 复位 latches."""
    return PositionState(entries_long=state.entries_long, entries_short=state.entries_short,
                         trading_day=state.trading_day,
                         fired_exits=state.fired_exits | ({exit_key} if exit_key else frozenset()),
                         last_open_long=state.last_open_long, last_open_short=state.last_open_short)


def partial_after_close(state: PositionState, lots: int, exit_key: str | None = None) -> PositionState:
    """The state after 分批止盈's partial close: `lots` fewer held and no more adds for this
    position; everything else — the average price, the first open's time / key / counters, the
    stop and take — is the same position's and stays."""
    return replace(state, volume=state.volume - lots, scaled_out=True,
                   fired_exits=state.fired_exits | ({exit_key} if exit_key else frozenset()))
