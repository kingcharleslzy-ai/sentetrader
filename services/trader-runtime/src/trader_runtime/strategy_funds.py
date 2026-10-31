"""按资金比例定手数与回撤停机 (所有者 2026-10-09)：同一份计算核心供实盘意图、发送前核对、
确认卡、回测与前端只读预览使用。预算 = 权益 × 比例，先扣本策略持仓与未成交开仓挂单占用的
保证金，再除以每手保证金，向下取整；比例下降不自动减持。回撤只按本策略归属净值计算（起始
基准 + 本轮已实现/未实现盈亏 − 本策略手续费），他人、手动与入出金的影响不计入；归属不明的
手动平仓让净值不可用时如实标 unavailable，不当零损益。"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .instruments import money_unit, symbol_currency
from .trading_costs import effective_fee_schedule, trade_fee

# The drawdown curve keeps the same sampling cap as the backtest's equity curve.
CURVE_POINTS = 300
CURVE_MIN_INTERVAL_NS = 60 * 1_000_000_000     # one point per minute at most while running


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _current_fact(value: dict[str, Any]) -> bool:
    age = _number(value.get("ageSeconds"))
    return bool(value.get("dataStatus") in {"normal", "simulated"}
                and value.get("isCached") is not True
                and age is not None and age <= 15)


def _format(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def compute_budget(
    *, equity: float | None, equity_percent: float, margin_per_lot: float | None,
    used_lots: int, pending_lots: int, available: float | None,
    fee_per_lot: float | None, max_lots: int | None, unit: str = "元",
) -> dict[str, Any]:
    """The one sizing core (live, cards, backtest). `used_lots` = the strategy's held lots,
    `pending_lots` = its unfilled open-order lots; both occupy the budget. Returns the funds
    payload: `lots` still openable this entry and `unavailableReason` when it cannot open
    (missing/stale inputs, an insufficient budget or the user's maxLots cap). Never negative,
    never rounds up, never opens without every input. `unit` is the account's money unit in the reason
    (美元 for a foreign contract's USD sub-account)."""
    occupied = max(0, int(used_lots)) + max(0, int(pending_lots))
    payload: dict[str, Any] = {
        "equity": _format(equity),
        "equityPercent": float(equity_percent),
        "usedLots": max(0, int(used_lots)),
        "pendingLots": max(0, int(pending_lots)),
        "usedMargin": None,
        "pendingMargin": None,
        "remainingBudget": None,
        "marginPerLot": _format(margin_per_lot),
        "maxLots": int(max_lots) if max_lots is not None else None,
        "feePerLot": _format(fee_per_lot),
        "lots": 0,
        "unavailableReason": None,
    }
    if equity is None or equity <= 0:
        payload["unavailableReason"] = "账户权益不可用或不新鲜，按资金比例定手数时不开仓。"
        return payload
    if margin_per_lot is None or margin_per_lot <= 0:
        payload["unavailableReason"] = "每手保证金不可用，按资金比例定手数时不开仓。"
        return payload
    if available is None:
        payload["unavailableReason"] = "账户可用资金不可用，按资金比例定手数时不开仓。"
        return payload
    if fee_per_lot is None or fee_per_lot < 0:
        payload["unavailableReason"] = "开仓手续费不可用，按资金比例定手数时不开仓。"
        return payload
    used_margin = round(payload["usedLots"] * margin_per_lot, 2)
    pending_margin = round(payload["pendingLots"] * margin_per_lot, 2)
    budget = equity * float(equity_percent) / 100
    remaining = max(0.0, budget - used_margin - pending_margin)
    lots = int(remaining // margin_per_lot)
    # 账户可用资金（已扣账户级冻结）约束整笔开仓的保证金与手续费。
    affordable = int(available // (margin_per_lot + fee_per_lot)) if margin_per_lot + fee_per_lot > 0 else 0
    lots = min(lots, max(0, affordable))
    if max_lots is not None:
        lots = min(lots, max(0, int(max_lots) - occupied))
    payload.update({
        "usedMargin": used_margin,
        "pendingMargin": pending_margin,
        "remainingBudget": round(remaining, 2),
        "lots": lots,
    })
    if lots <= 0:
        if max_lots is not None and occupied >= int(max_lots):
            reason = f"已达本策略总手数上限 {max_lots} 手（持仓 {payload['usedLots']} + 挂单 {payload['pendingLots']}），这次不开。"
        elif remaining < margin_per_lot:
            reason = f"剩余额度 {remaining:.2f} {unit} ÷ 每手保证金 {margin_per_lot:.2f} {unit}，不足 1 手，这次不开。"
        else:
            reason = f"账户可用资金 {available:.2f} {unit}不够保证金加手续费的 1 手，这次不开。"
        payload["unavailableReason"] = reason
    return payload


def _held_lots(store: Any, user_id: str, strategy_id: str, symbol: str,
               positions: list[dict[str, Any]] | None) -> int:
    """The strategy's own held lots: its armed protection rows' remaining volume per direction,
    capped by what the account actually holds (the same read held_position uses)."""
    with store.lock:
        rows = store.connection.execute(
            """SELECT position_direction, SUM(remaining_volume) AS volume FROM ai_stop_orders
               WHERE user_id = ? AND trading_account_id = ? AND lower(symbol) = lower(?)
               AND remaining_volume > 0
               AND json_extract(strategy_metadata_json, '$.strategyId') = ?
               GROUP BY position_direction""",
            (user_id, _account_of(store, strategy_id), symbol, strategy_id)).fetchall()
    held = 0
    for row in rows:
        direction = str(row["position_direction"])
        account = sum(max(0, int(item.get("volume") or 0)) for item in positions or []
                      if isinstance(item, dict) and str(item.get("symbol") or "").casefold() == symbol.casefold()
                      and item.get("direction") == direction)
        held += min(int(row["volume"] or 0), account)
    return held


def _account_of(store: Any, strategy_id: str) -> str:
    row = store.connection.execute(
        "SELECT trading_account_id FROM strategy_drafts WHERE id = ?", (strategy_id,)).fetchone()
    return str(row["trading_account_id"]) if row is not None else store.default_trading_account_id


def pending_open_lots(store: Any, user_id: str, strategy_id: str,
                      exclude_attempt_id: str | None = None,
                      gateway: Any = None) -> int:
    """The strategy's unfilled open-order lots: attempts of open actions not yet terminal. A
    UNKNOWN order counts as fully occupied until its original order has been queried and its
    fills observed — never bypassed by a fresh open. `exclude_attempt_id` is the order being
    sent right now: its own reservation is not double-counted against itself — every other
    reservation still occupies the budget.

    终态占用 (2026-10-10 #694)：`gateway`（模拟网关，与在途扫描同一身份来源）在场时，原单已
    可信终态（FINISHED）的**已撤余量**释放——只释放成交量之外被撤掉的部分；已成交但成交流
    还没归属的数量继续占用（成交会变成持仓，不能提前放额度再开）。ALIVE、UNKNOWN、读取失败、
    没拿到委托号的 RESERVED 一律保守占用，不因查询失败放松。"""
    with store.lock:
        rows = store.connection.execute(
            """SELECT client_order_id, volume FROM strategy_execution_attempts
               WHERE user_id = ? AND strategy_id = ? AND action IN ('open_long', 'open_short')
               AND status IN ('RESERVED', 'SUBMITTED', 'PARTIAL', 'UNKNOWN')
               AND id IS NOT ?""",
            (user_id, strategy_id, exclude_attempt_id)).fetchall()
    pending = 0
    for row in rows:
        volume = int(row["volume"] or 0)
        if volume <= 0:
            continue
        filled = 0
        cancelled = 0
        for identifier in filter(None, str(row["client_order_id"] or "").split(",")):
            observed = store.connection.execute(
                """SELECT payload_json FROM strategy_fill_events
                   WHERE user_id = ? AND client_order_id = ?""",
                (user_id, identifier)).fetchall()
            filled += sum(int(json.loads(item[0])["trade"]["volume"]) for item in observed)
            if gateway is None:
                continue
            try:
                order = gateway.get(identifier, user_id=user_id)
            except Exception:  # noqa: BLE001 — 读不回来的单不知道终态，保守占用
                continue
            if (order.get("submitPhase") in {"PREPARED", "ABANDONED"}
                    or order.get("remoteStatus") != "FINISHED"):
                continue
            request = order.get("request") or {}
            remote_volume = int(request.get("volume") or volume)
            remote_filled = int(order.get("filledVolume") or 0)
            cancelled += max(0, remote_volume - remote_filled)
        pending += max(0, volume - filled - cancelled)
    return pending


def _open_fee_per_lot(fee_settings: Any, user_id: str, symbol: str, price: float | None,
                      multiplier: float | None, market: dict[str, Any]) -> tuple[float | None, str | None]:
    """The per-lot open fee from the same source order_estimate uses: the user's schedule, else
    the exchange standard, else the market source's own per-lot commission. (fee, source)."""
    schedule = fee_settings.get(user_id) if fee_settings is not None else None
    state_root = Path(fee_settings.root).parent if fee_settings is not None else None
    effective = effective_fee_schedule(schedule, symbol, root=state_root) if schedule is not None else None
    if effective is not None and price and multiplier and multiplier > 0:
        return trade_fee(effective, symbol=symbol, price=price, volume=1,
                         multiplier=multiplier, offset="OPEN"), "rate"
    per_lot = _number(market.get("commissionPerLot"))
    if per_lot is not None and per_lot >= 0:
        return per_lot, "market"
    return None, None


def live_budget(store: Any, provider: Any, *, user_id: str, strategy_id: str, symbol: str,
                account: dict[str, Any] | None = None, market: dict[str, Any] | None = None,
                positions: list[dict[str, Any]] | None = None,
                fee_settings: Any = None, gateway: Any = None,
                equity_percent: float, max_lots: int | None = None,
                exclude_attempt_id: str | None = None) -> dict[str, Any]:
    """The funds payload off live facts. Fresh account/market is a precondition of opening: a
    stale or cached read reports the reason instead of a number (order_estimate may show stale
    figures; this path may not), and a failed positions read leaves the strategy's occupation
    unknown — never zero, so it opens nothing."""
    read_failed = False
    try:
        account = account if account is not None else (provider.account_summary("") or {})
        market = market if market is not None else (provider.market_snapshot("", symbol) or {})
    except RuntimeError:
        account, market, read_failed = {}, {}, True
    if positions is None:
        try:
            positions = list(provider.positions() or [])
        except RuntimeError:
            positions, read_failed = None, True
    fresh = not read_failed and _current_fact(account) and _current_fact(market)
    equity = _number(account.get("equity")) if fresh else None
    available = _number(account.get("available")) if fresh else None
    margin_per_lot = _number(market.get("marginPerLot")) if fresh else None
    if margin_per_lot is not None and margin_per_lot <= 0:
        margin_per_lot = None
    fee_per_lot, _ = _open_fee_per_lot(fee_settings, user_id, symbol,
                                       _number(market.get("price")) if fresh else None,
                                       _number(market.get("volumeMultiple")) if fresh else None, market) if fresh else (None, None)
    payload = compute_budget(
        equity=equity, equity_percent=equity_percent, margin_per_lot=margin_per_lot,
        used_lots=_held_lots(store, user_id, strategy_id, symbol, positions),
        pending_lots=pending_open_lots(store, user_id, strategy_id,
                                       exclude_attempt_id=exclude_attempt_id, gateway=gateway),
        available=available, fee_per_lot=fee_per_lot, max_lots=max_lots, unit=money_unit(symbol_currency(symbol)))
    if not fresh:
        if read_failed:
            payload["unavailableReason"] = "账户、行情或持仓读取失败，无法确认本策略占用，按资金比例定手数时不开仓。"
        elif payload["unavailableReason"] is None:
            payload["unavailableReason"] = "行情或账户数据不新鲜，按资金比例定手数时不开仓。"
    payload["feeSource"] = "estimated"
    return payload


# -- 回撤停机：本策略净值 --------------------------------------------------------------------------

def round_identity_digest(definition: dict[str, Any]) -> str:
    """回撤风险轮的身份摘要 (2026-10-10 #692)：切图表周期（strategy_chart_period.rebind_items，
    文档合同 "without resetting risk state"）只改写由 chartPlan 派生的执行周期部分（plan、
    chartTimeframeSeconds 与顶层 timeframeSeconds），不换风险轮——起点/峰值/费用口径/停机锁都
    保留。身份只对去掉这些派生字段后的定义计算；真正改了规则（chartPlan 或显式 plan）才会变。
    旧行存的是完整定义摘要，比较处同时接受完整摘要与该摘要。"""
    normalized = ({key: value for key, value in definition.items()
                   if key not in {"plan", "chartTimeframeSeconds", "timeframeSeconds"}}
                  if isinstance(definition.get("chartPlan"), dict) else definition)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def strategy_round_facts(
    store: Any, *, user_id: str, strategy_id: str, symbol: str, account_id: str,
    since_ns: int, latest_price: float | None, multiplier: float | None,
    fee_settings: Any = None,
) -> dict[str, Any]:
    """This round's strategy-only net PnL facts since `since_ns`: attributed realized gross PnL,
    fees (the trade's own commission when the source carries one, else the formal rate as an
    estimate — never an account-commission delta) and the held rows' unrealized PnL at
    `latest_price`. `unattributed` is set when a manual close consumed this strategy's rows
    without an attributable result: the net value is then unknowable, not zero."""
    realized = Decimal(0)
    fees = Decimal(0)
    fee_estimated = False
    fee_basis_missing = False
    unattributed = None
    with store.lock:
        events = store.connection.execute(
            """SELECT * FROM strategy_fill_events WHERE user_id = ? AND trading_account_id = ?
               AND trade_time >= ? AND json_extract(payload_json, '$.trade.symbol') = ?
               ORDER BY trade_time, event_id""",
            (user_id, account_id, since_ns, symbol)).fetchall()
        rows = store.connection.execute(
            """SELECT * FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?
               AND symbol = ? AND remaining_volume > 0
               AND json_extract(strategy_metadata_json, '$.strategyId') = ?""",
            (user_id, account_id, symbol, strategy_id)).fetchall()
    schedule = fee_settings.get(user_id) if fee_settings is not None else None
    state_root = Path(fee_settings.root).parent if fee_settings is not None else None
    effective = effective_fee_schedule(schedule, symbol, root=state_root) if schedule is not None else None
    for row in events:
        payload = json.loads(row["payload_json"])
        trade = payload.get("trade") or {}
        pnl = (payload.get("realizedGrossPnl") or {}).get(strategy_id)
        if pnl is not None:
            realized += Decimal(str(pnl))
        marked = payload.get("unattributedStrategyIds") or []
        if strategy_id in marked:
            unattributed = "本策略持仓被归属不明的手动平仓消耗，无法确定本策略净值；已暂停新开仓。"
        # Fees: only this strategy's own fills (its entries and its attributed closes).
        entry_fill = payload.get("strategyEntry") == strategy_id
        if pnl is not None or entry_fill:
            price, volume = _number(trade.get("price")), int(trade.get("volume") or 0)
            commission = _number(trade.get("commission"))
            if commission is not None and commission >= 0:
                fees += Decimal(str(commission))
            elif (effective is not None and price is not None and price > 0
                  and multiplier and multiplier > 0):
                fee_estimated = True
                if trade.get("offset") == "OPEN":
                    fees += Decimal(str(trade_fee(effective, symbol=symbol, price=price,
                                                  volume=volume, multiplier=multiplier,
                                                  offset="OPEN")))
                else:
                    # 平今/平昨按这笔平仓花掉的保护行真实分配（CLOSE 不等于所有交易所的平昨：
                    # 只有 SHFE/INE 用 CLOSETODAY 区分，其他交易所按开仓交易日区分）。
                    split = (payload.get("strategyCloseLots") or {}).get(strategy_id) or {}
                    today_lots = int(split.get("closeToday") or 0)
                    yesterday_lots = int(split.get("closeYesterday") or 0)
                    if today_lots + yesterday_lots <= 0:
                        # 旧事件没有分配事实：按成交自己的 offset 归属兜底（CLOSETODAY＝平今）。
                        today_lots, yesterday_lots = (
                            volume if trade.get("offset") == "CLOSETODAY" else 0,
                            0 if trade.get("offset") == "CLOSETODAY" else volume)
                    for lots, today in ((today_lots, True), (yesterday_lots, False)):
                        if lots > 0:
                            fees += Decimal(str(trade_fee(effective, symbol=symbol, price=price,
                                                          volume=lots, multiplier=multiplier,
                                                          offset="CLOSE", today=today)))
            else:
                # 无实际值也无估算依据：这一笔的费用当作不可知，净值不可用，不能按 0 继续。
                fee_estimated = fee_basis_missing = True
    unrealized = Decimal(0)
    if latest_price is not None and latest_price > 0:
        for row in rows:
            metadata = json.loads(row["strategy_metadata_json"])
            entry = Decimal(str(metadata["entryPrice"]))
            volume = int(row["remaining_volume"])
            gross = (Decimal(str(latest_price)) - entry) * volume * Decimal(str(metadata["contractMultiplier"]))
            unrealized += gross if row["position_direction"] == "long" else -gross
    held_lots = sum(int(row["remaining_volume"]) for row in rows)
    return {
        "realized": realized,
        "fees": fees,
        "feeSource": ("unavailable" if fee_basis_missing
                      else "estimated" if fee_estimated else "actual"),
        "unrealized": unrealized,
        "heldLots": held_lots,
        "unattributed": unattributed,
    }


def drawdown_percent(peak: float, current: float) -> float:
    """(peak − current) / peak as a percentage; 0 without a positive peak."""
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - current) / peak * 100)


def sample_curve(points: list[dict[str, Any]], limit: int = CURVE_POINTS) -> list[dict[str, Any]]:
    """Keep the curve within `limit` points by dropping every second older point (the same cap
    the backtest's equity curve uses)."""
    if len(points) <= limit:
        return points
    keep = points[len(points) - limit:]
    half = [point for index, point in enumerate(keep) if index % 2 == 0 or index >= len(keep) - limit // 2]
    return sample_curve(half, limit) if len(half) > limit else half


def curve_append(curve: list[dict[str, Any]], *, time_ns: int, value: float) -> list[dict[str, Any]]:
    """One net-value point, at most one per minute while running (the monitor polls faster)."""
    if curve and int(time_ns) - int(curve[-1]["time"]) < CURVE_MIN_INTERVAL_NS:
        return curve
    return sample_curve([*curve, {"time": int(time_ns), "equity": round(value, 2)}])


def round_started_ns(started_at: str) -> int:
    return int(datetime.fromisoformat(str(started_at)).timestamp() * 1_000_000_000) if started_at else 0


