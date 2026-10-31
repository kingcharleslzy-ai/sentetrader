from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from .risk import (
    InstrumentSpecProvider,
    RiskPolicyProvider,
    calculate_trade_risk,
    ceil_to_tick,
    decimal_value,
    floor_to_tick,
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _same_symbol(left: Any, right: Any) -> bool:
    return str(left or "").casefold() == str(right or "").casefold()


def _positions_for_symbol(
    positions: list[dict[str, Any]], symbol: str,
) -> tuple[int, int]:
    long_volume = 0
    short_volume = 0
    for position in positions:
        if not isinstance(position, dict) or not _same_symbol(position.get("symbol"), symbol):
            continue
        volume = max(0, int(position.get("volume") or 0))
        if position.get("direction") == "long":
            long_volume += volume
        elif position.get("direction") == "short":
            short_volume += volume
    return long_volume, short_volume


OwnPosition = tuple[str | None, int]


def _own_position_volumes(own_position: OwnPosition | None) -> tuple[int, int] | None:
    """`own_position` — the strategy's own held lots as (direction, volume), the held_position read
    (its armed protection, capped by the account). None keeps the account-wide read below."""
    if own_position is None:
        return None
    direction, volume = own_position
    volume = max(0, int(volume))
    if direction == "long":
        return volume, 0
    if direction == "short":
        return 0, volume
    return 0, 0


def _position_intent(
    *, signal: str, symbol: str, maximum: int, positions: list[dict[str, Any]],
    own_position: OwnPosition | None = None,
) -> dict[str, Any]:
    own = _own_position_volumes(own_position)
    if own is not None:
        # 只管自己开的仓 (2026-10-08): 自动下单 / 出卡确认 judge off the strategy's own lots, so a
        # manual or AI position on the same contract is neither closed by it nor suppresses an open.
        long_volume, short_volume = own
    else:
        long_volume, short_volume = _positions_for_symbol(positions, symbol)
    action = "hold"
    requested = 0
    target_long, target_short = long_volume, short_volume
    reason = "当前没有新的开平仓动作。"
    if signal == "buy":
        if short_volume > 0:
            action, requested, target_short = "close_short", short_volume, 0
            reason = "买入信号出现，先平已有空仓；本次不同时反手开多。"
        elif long_volume > maximum:
            action, requested, target_long = "reduce_long", long_volume - maximum, maximum
            reason = "当前多仓超过策略上限，只生成减仓意图。"
        elif long_volume < maximum:
            action, requested, target_long = "open_long", maximum - long_volume, maximum
            reason = "买入信号出现，目标多仓不超过策略上限。"
    elif signal == "sell":
        if long_volume > 0:
            action, requested, target_long = "close_long", long_volume, 0
            reason = "卖出信号出现，先平已有多仓；本次不同时反手开空。"
        elif short_volume > maximum:
            action, requested, target_short = "reduce_short", short_volume - maximum, maximum
            reason = "当前空仓超过策略上限，只生成减仓意图。"
        elif short_volume < maximum:
            action, requested, target_short = "open_short", maximum - short_volume, maximum
            reason = "卖出信号出现，目标空仓不超过策略上限。"
    return {
        "action": action,
        "requestedVolume": requested,
        "currentLong": long_volume,
        "currentShort": short_volume,
        "targetLong": target_long,
        "targetShort": target_short,
        "reason": reason,
    }


def _current_fact(value: dict[str, Any], *, require_initialized: bool = False) -> bool:
    age = _number(value.get("ageSeconds"))
    current = (
        value.get("dataStatus") in {"normal", "simulated"}
        and value.get("isCached") is not True
        and age is not None
        and age <= 15
    )
    if require_initialized:
        current = current and value.get("ordersInitialized") is True
    return current


def _prices(
    *, action: str, market_price: Decimal, tick: Decimal,
    stop_ticks: int, take_ticks: int | None,
) -> tuple[Decimal | None, Decimal | None]:
    if action == "open_long":
        return (
            floor_to_tick(market_price - tick * stop_ticks, tick),
            ceil_to_tick(market_price + tick * take_ticks, tick) if take_ticks is not None else None,
        )
    if action == "open_short":
        return (
            ceil_to_tick(market_price + tick * stop_ticks, tick),
            floor_to_tick(market_price - tick * take_ticks, tick) if take_ticks is not None else None,
        )
    return None, None


def _context_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def build_strategy_intent(
    *, strategy: dict[str, Any], evaluation: dict[str, Any],
    market: dict[str, Any], account: dict[str, Any],
    positions: list[dict[str, Any]], activity: dict[str, Any],
    user_id: str, policies: RiskPolicyProvider,
    instruments: InstrumentSpecProvider,
    now: datetime | None = None,
    engine_state: Any = None,
    decision_time: datetime | None = None,
    own_position: OwnPosition | None = None,
    funds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`engine_state` (strategy_engine.PositionState) carries the strategy's own position facts
    (entry, bars held, today's opens, time exits already acted on); `decision_time` is the trading
    clock the time exits (收盘前平仓) are judged on — the replay clock in a replay. `own_position`
    (direction, volume) is the strategy's own held lots (held_position's read): given, it replaces
    the account-wide volumes in both the legacy intent and schemaVersion 2's PositionState — the
    自动下单 / 出卡确认 口径. A spread plan always reads its two legs from the account, whatever
    `own_position` says. `funds` (strategy_funds.compute_budget's payload) feeds 按资金比例定手数:
    the shared core's verdict on the lots this entry may still open."""
    definition = strategy["definition"]
    position = _position_intent(
        signal=str(evaluation["signal"]),
        symbol=strategy["symbol"],
        maximum=int(definition["riskLimits"]["maxPosition"]),
        positions=positions,
        own_position=own_position,
    )
    market_current = _current_fact(market)
    account_current = _current_fact(account)
    activity_current = _current_fact(activity, require_initialized=True)
    market_price_value = _number(market.get("price"))
    tick_value = _number(market.get("priceTick"))
    multiplier_value = _number(market.get("volumeMultiple"))
    decision = None
    if definition.get("schemaVersion") == 2:
        # One judgement for live and backtest: strategy_engine.decide (rule exits, time exits,
        # entry gates, ATR sizing) over the same evaluation.
        from .strategy_engine import MarketContext, PositionState, decide
        from .strategy_rules import StrategyPlan
        plan_model = StrategyPlan.model_validate(definition["plan"])
        if plan_model.spread is not None:
            # 价差: the position is the two legs combined (做多价差 = A 多 + B 空), rebuilt from the
            # account; the entry price is the entry spread of the legs' average prices.
            from .strategy_spread import spread_position
            held = spread_position(plan_model.spread, positions)
            state = replace(engine_state or PositionState(),
                            direction=held["direction"] or "flat", volume=held["volume"],
                            entry_price=held["entry_price"])
        else:
            own = _own_position_volumes(own_position)
            long, short = own if own is not None else _positions_for_symbol(positions, strategy["symbol"])
            state = replace(engine_state or PositionState(),
                            direction="long" if long else "short" if short else "flat", volume=long or short)
        if state.bars_pending and state.direction != "flat":
            evaluation["dataPending"] = "持仓起点以来的历史K线未就绪，暂不判断按持仓根数退出。"
        equity = _number(account.get("equity")) if account_current else None
        decision = decide(plan_model, evaluation, state, MarketContext(
            price_tick=tick_value or 0.0, volume_multiple=multiplier_value or 0.0, equity=equity,
            now=int(decision_time.timestamp() * 1_000_000_000) if decision_time is not None else None,
            # 双向触价 picks the side the latest (current) price reached.
            price=market_price_value if market_current and market_price_value else None,
            # 按资金比例定手数: the shared funds core (None unless the plan sizes off equity).
            funds=funds if plan_model.sizing is not None and plan_model.sizing.mode == "equity_percent" else None))
        position = decision.position
    action = str(position["action"])
    requested_volume = int(position["requestedVolume"])
    metadata_current = (
        market_price_value is not None and market_price_value > 0
        and tick_value is not None and tick_value > 0
        and multiplier_value is not None and multiplier_value > 0
    )
    checks: list[dict[str, Any]] = [
        {"id": "market_current", "label": "行情可用", "passed": market_current},
        {"id": "account_current", "label": "账户可用", "passed": account_current},
        {"id": "activity_current", "label": "委托状态已初始化", "passed": activity_current},
        {"id": "contract_metadata", "label": "合约参数完整", "passed": metadata_current},
    ]
    stop_price: Decimal | None = None
    take_price: Decimal | None = None
    adjusted_volume = requested_volume
    risk_check: dict[str, Any] | None = None
    status = "no_action"
    reason = str(position["reason"])
    risk_reducing = action in {"close_short", "close_long", "reduce_long", "reduce_short"}
    if risk_reducing:
        status = "reduce_only"
        checks.append({
            "id": "risk_reducing", "label": "仅降低当前持仓风险", "passed": True,
        })
    elif action in {"open_long", "open_short"} and definition.get("plan", {}).get("spread") is not None:
        # 价差 (第二期): the two legs dispatch together at the opponent's price — no per-contract
        # stop or deterministic risk math applies (a unit's lots are fixed by the spread definition,
        # not risk-sized), and the spread's own 止损/止盈 is judged every second while held.
        status = "allowed"
        reason = "价差两腿执行：一份价差的两腿手数写在价差定义里，止损止盈按价差每秒判断。"
        checks.append({"id": "spread_legs", "label": "价差两腿执行", "passed": True})
    elif action in {"open_long", "open_short"}:
        status = "blocked"
        if market_current and account_current and activity_current and metadata_current:
            market_price = decimal_value(market_price_value)
            tick = decimal_value(tick_value)
            stop_price, take_price = _prices(
                action=action,
                market_price=market_price,
                tick=tick,
                stop_ticks=int(definition["riskLimits"]["stopLossTicks"]),
                take_ticks=definition["riskLimits"]["takeProfitTicks"],
            )
            structure = None
            structural_ok = True
            from .strategy_rules import uses_structural_stop
            if decision is not None and decision.entryStopDistance is not None:
                # ATR 止损: the signal bar's ATR × multiple from the reference price; the fill
                # re-anchors it on the actual fill price.
                from .strategy_engine import stop_from_distance
                stop_price = stop_from_distance(market_price, decision.entryStopDistance, tick,
                                                long=action == "open_long")
            if uses_structural_stop(definition):
                structure = evaluation.get("structures", {}).get("entryLong" if action == "open_long" else "entryShort")
                structural_ok = bool(structure)
                if structure:
                    stop_price = (floor_to_tick(decimal_value(structure["low"]), tick) - tick if action == "open_long"
                                  else ceil_to_tick(decimal_value(structure["high"]), tick) + tick)
                    structural_ok = stop_price > 0 and (stop_price < market_price if action == "open_long" else stop_price > market_price)
                checks.append({"id": "structural_stop", "label": "结构止损有效", "passed": structural_ok})
            today_pnl_value = account.get("todayNetPnl", account.get("todayPnl"))
            today_pnl = Decimal(str(today_pnl_value)) if _number(today_pnl_value) is not None else None
            side = "buy_open" if action == "open_long" else "sell_open"
            risk_payload = {
                "symbol": strategy["symbol"],
                "side": side,
                "volume": requested_volume,
                "limitPrice": market_price,
                "stopPrice": stop_price,
                "contractMultiplier": multiplier_value,
                "priceTick": tick_value,
            }
            risk_check = calculate_trade_risk(
                risk_payload,
                user_id=user_id,
                policies=policies,
                instruments=instruments,
                today_pnl=today_pnl,
                now=now,
            )
            deterministic_checks = dict(risk_check.get("checks") or {})
            if not deterministic_checks.get("perTradeLoss") and requested_volume > 0:
                per_contract_loss = float(risk_check["estimatedLoss"]) / requested_volume
                affordable = int(float(risk_check["limit"]) // per_contract_loss) if per_contract_loss > 0 else 0
                if affordable > 0 and all(
                    deterministic_checks.get(name) for name in (
                        "symbolAllowed", "dailyLoss", "openingTime"
                    )
                ):
                    adjusted_volume = min(requested_volume, affordable)
                    risk_payload["volume"] = adjusted_volume
                    risk_check = calculate_trade_risk(
                        risk_payload,
                        user_id=user_id,
                        policies=policies,
                        instruments=instruments,
                        today_pnl=today_pnl,
                        now=now,
                    )
            budget = definition["riskLimits"].get("perTradeRisk")
            if budget is not None:
                per_lot = abs(market_price - stop_price) * decimal_value(multiplier_value)
                affordable = int(decimal_value(budget) // per_lot) if per_lot > 0 else 0
                adjusted_volume = min(adjusted_volume, affordable)
                checks.append({"id": "strategy_trade_budget", "label": "单笔止损预算", "passed": adjusted_volume > 0})
                if adjusted_volume > 0:
                    risk_payload["volume"] = adjusted_volume
                    risk_check = calculate_trade_risk(risk_payload, user_id=user_id, policies=policies,
                                                     instruments=instruments, today_pnl=today_pnl, now=now)
            checks.append({
                "id": "deterministic_risk",
                "label": "确定性风险规则",
                "passed": bool(risk_check["passed"]),
            })
            if risk_check["passed"] and adjusted_volume > 0 and structural_ok:
                status = "allowed"
                reason = (
                    f"风险规则允许 {adjusted_volume} 手；这里只生成意图，尚未提交模拟委托。"
                )
            else:
                reason = ("结构止损或单笔预算不足，跳过本次开仓。" if not structural_ok or budget is not None and adjusted_volume == 0
                          else str(risk_check["reason"]))
                adjusted_volume = 0
        else:
            adjusted_volume = 0
            reason = "行情、账户、委托状态或合约参数不完整，停止产生开仓意图。"
    maximum_loss = definition["riskLimits"].get("maxLoss")
    if action in {"open_long", "open_short"} and maximum_loss is not None:
        net = _number(account.get("todayNetPnl", account.get("todayPnl")))
        loss_ok = net is not None and net > -float(maximum_loss)
        checks.append({"id": "strategy_loss_limit", "label": "策略亏损上限", "passed": loss_ok})
        if not loss_ok:
            status, adjusted_volume, reason = "blocked", 0, "已达到本次亏损上限，继续监控平仓。"
    risk_adjusted = {
        "status": status,
        "action": action,
        "volume": adjusted_volume if status in {"allowed", "reduce_only"} else 0,
        "stopPrice": float(stop_price) if stop_price is not None else None,
        "takeProfitPrice": float(take_price) if take_price is not None else None,
        "checks": checks,
        "riskCheck": risk_check,
        "reason": reason,
        "executionEligible": False,
        "liveTrading": "disabled",
    }
    signal_snapshot = {
        "signal": evaluation["signal"],
        "barTime": evaluation["evaluatedBarTime"],
        "barState": "completed",
        "dataStatus": evaluation["dataStatus"],
        "definitionDigest": strategy["definitionDigest"],
    }
    if evaluation.get("dataPending"):
        # 数据未就绪 (当日最高 / 最低): the monitor treats this judgement as waiting, not as a signal.
        signal_snapshot["dataPending"] = evaluation["dataPending"]
    if action in {"open_long", "open_short"}:
        alignment = evaluation.get("entryAlignments", {}).get("entryLong" if action == "open_long" else "entryShort")
        if alignment:
            signal_snapshot["alignmentId"] = alignment["id"]
    if decision is not None and action in {"open_long", "open_short"} and decision.entryTrigger:
        # 盘中触价: the open waits inside the next bar for this level; `reached` is the latest price's
        # verdict, re-read on every monitor poll of that bar.
        from .strategy_engine import trigger_reached
        signal_snapshot["entryTrigger"] = {**decision.entryTrigger, "reached": bool(
            market_current and market_price_value is not None and market_price_value > 0
            and trigger_reached(decision.entryTrigger, market_price_value, long=action == "open_long"))}
        if decision.alternate is not None and decision.alternate.entryTrigger:
            # 双向触价: the other side's level, armed with it (shown in the waiting line).
            signal_snapshot["entryTrigger"]["other"] = {**decision.alternate.entryTrigger,
                                                        "action": decision.alternate.action}
    if decision is not None and action in {"open_long", "open_short"}:
        # ATR 止损 / 吊灯追踪 / 保本与分段止盈的 ATR14: the signal bar's ATR, frozen here and carried
        # with the order or card.
        levels = {key: value for key, value in (("stopDistance", decision.entryStopDistance),
                                                 ("entryAtr", decision.entryAtr),
                                                 ("exitAtr", decision.exitAtr)) if value is not None}
        if levels:
            signal_snapshot["protectionLevels"] = levels
    if decision is not None and decision.exitKey:
        # A time exit (收盘前平仓 / 最长持仓) or a 分批止盈 tier fires once per position: the attempt
        # carrying this key is what later reads as "already acted on", across restarts.
        signal_snapshot["exitKey"] = decision.exitKey
        signal_snapshot["exitReason"] = decision.exitReason
    if decision is not None and decision.add and action in {"open_long", "open_short"}:
        # 加仓: this open increases the held same-direction position — not a fresh entry, so the
        # per-direction daily count and the 复位 latch skip it (store reads this flag).
        signal_snapshot["add"] = True
    context = {
        "strategyDigest": strategy["definitionDigest"],
        "signalSnapshot": signal_snapshot,
        "market": {
            "capturedAt": market.get("capturedAt"),
            "price": market.get("price"),
            "current": market_current,
        },
        "account": {
            "capturedAt": account.get("capturedAt"),
            "todayNetPnl": account.get("todayNetPnl"),
            "current": account_current,
        },
        "positions": sorted([
            {
                "symbol": item.get("symbol"),
                "direction": item.get("direction"),
                "volume": item.get("volume"),
            }
            for item in positions if isinstance(item, dict)
        ], key=lambda item: (str(item["symbol"]), str(item["direction"]))),
        "activity": {
            "capturedAt": activity.get("capturedAt"),
            "current": activity_current,
            "ordersInitialized": activity.get("ordersInitialized"),
        },
    }
    return {
        "contextDigest": _context_digest(context),
        "signalSnapshot": signal_snapshot,
        "positionIntent": position,
        "riskAdjustedIntent": risk_adjusted,
    }


# The one-word Chinese read of a position intent, shared by the signal explanation and the
# monitoring alert so the chart, the spoken line and the reply all say the same word.
SIGNAL_ACTION_LABELS = {
    "hold": "观望",
    "open_long": "开多",
    "open_short": "开空",
    "close_long": "平多",
    "close_short": "平空",
    "reduce_long": "减多",
    "reduce_short": "减空",
}


def signal_action_label(intent: dict[str, Any], action: str) -> str:
    """The one-word read of an intent's action: the plain label, except an 加仓 open reads 加多 / 加空
    and a 分批止盈 close reads 分批平多 / 分批平空 — their own acts, not a fresh open or a full close."""
    snapshot = intent.get("signalSnapshot") or {}
    if snapshot.get("add") and action in {"open_long", "open_short"}:
        return "加多" if action == "open_long" else "加空"
    if snapshot.get("exitReason") == "scale_out" and action in {"close_long", "close_short"}:
        return "分批平多" if action == "close_long" else "分批平空"
    return SIGNAL_ACTION_LABELS.get(action, action)

_ANNOTATION_NUMBERING = "①②③④⑤⑥"


def _bar_seconds(value: Any) -> int | None:
    """An evaluated bar / hit time in nanoseconds (tqsdk style) → epoch seconds for the annotation."""
    try:
        return int(int(value) // 1_000_000_000)
    except (TypeError, ValueError):
        return None


def signal_annotation_items(
    *, action: str, evaluation: dict[str, Any], intent: dict[str, Any],
    market: dict[str, Any],
) -> list[dict[str, Any]]:
    """The chart annotations of one strategy-signal read: the entry/close point on the evaluated bar,
    the strategy's stop/take lines for opens, and the last satisfied condition bars (v2 rules only —
    legacy evaluations carry no hit times). Same ChartAnnotationItem shape `annotate_chart` draws,
    numbered labels the reply explains in order; a hold draws nothing."""
    action = str(action)
    if action == "hold":
        return []
    items: list[dict[str, Any]] = []
    bar_seconds = _bar_seconds((evaluation or {}).get("evaluatedBarTime"))
    price = _number((market or {}).get("price"))
    if bar_seconds is not None and price is not None and price > 0:
        items.append({
            "id": "signal-entry", "kind": "point", "pane": "main",
            "role": "close" if action.startswith(("close_", "reduce_")) else ("sell" if action == "open_short" else "buy"),
            "price": price, "time": bar_seconds,
            "label": (signal_action_label(intent, action) if intent
                      else SIGNAL_ACTION_LABELS.get(action, action)),
        })
    if action in {"open_long", "open_short"}:
        adjusted = (intent or {}).get("riskAdjustedIntent") or {}
        for key, annotation_id, label in (
            ("stopPrice", "signal-stop", "止损"),
            ("takeProfitPrice", "signal-take", "止盈"),
        ):
            line_price = _number(adjusted.get(key))
            if line_price is not None and line_price > 0:
                items.append({"id": annotation_id, "kind": "hline", "pane": "main",
                              "price": line_price, "label": label})
    satisfied = [
        condition for condition in (evaluation or {}).get("conditions") or []
        if isinstance(condition, dict) and condition.get("status") == "passed"
    ]
    for condition in satisfied[:2]:
        hits = [hit for hit in condition.get("hits") or [] if hit]
        seconds = _bar_seconds(hits[-1]) if hits else None
        if seconds is None:
            continue
        items.append({
            "id": f"signal-cond-{len(items)}", "kind": "vline", "pane": "main",
            "time": seconds,
            "label": str(condition.get("label") or "").strip()[:7] or "条件",
        })
    for index, item in enumerate(items):
        # ①②③ prefix + text, within the annotation label cap (8).
        item["label"] = f"{_ANNOTATION_NUMBERING[index]}{item['label']}"[:8]
    return items[:6]
