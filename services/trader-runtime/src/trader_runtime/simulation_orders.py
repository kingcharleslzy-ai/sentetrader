from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from . import fast_json
from .exchange_calendar import calendar_trading_days
from .order_backfill import fill_time
from .order_boundaries import (
    SimulationOrderError,
    finite_positive,
    price_on_tick,
    quote_is_fresh,
)
from .risk import (
    InstrumentSpecProvider,
    RiskPolicyProvider,
    calculate_trade_risk,
    ceil_to_tick,
    decimal_value,
    floor_to_tick,
)
from .sidecar_http import SidecarHttpError
from .store import RuntimeStore, utc_now
from .trading_calendar import trading_day_at
from .trading_clock import TradingClock
from .trading_costs import effective_fee_rate

log = logging.getLogger("uvicorn.error.orders")

CLOSE_TODAY_EXCHANGES = frozenset({"SHFE", "INE"})
OPEN_SIDES = frozenset({"buy_open", "sell_open"})
CLOSE_SIDES = frozenset({"buy_close", "sell_close"})
ANOMALOUS_REMOTE_STATUSES = frozenset({"IDENTITY_MISMATCH", "REPORT_INVALID"})


def _paper_settlement_pending(error: BaseException) -> bool:
    """本站模拟引擎在等交易所公布结算价时的确定性拒收（409
    paper_settlement_pending，2026-10-08 结算改版）：命令没有进账本，委托是
    如实的“未发送”，不是“结果未知”，也不是网关中断。"""
    return (isinstance(error, SidecarHttpError) and error.status == 409
            and str(error.payload.get("error") or "") == "paper_settlement_pending")


def order_is_active(order: dict[str, Any]) -> bool:
    """Still working or not yet settled: the exchange holds it (ALIVE), the submit
    has not been confirmed, or a submit/cancel outcome is unknown."""
    if order.get("submitPhase") in {"PREPARED", "CALL_STARTED", "UNKNOWN"} or order.get("cancelPhase") == "UNKNOWN":
        return True
    report = order.get("report") or {}
    return (report.get("order") or {}).get("status") == "ALIVE"


def order_is_working(order: dict[str, Any]) -> bool:
    """The one judgment for "这单还挂在场上" (2026-10-04 #518): the submit was confirmed, the
    exchange still holds it (ALIVE), and no cancel has taken effect (confirmed or unknown).
    Every 还在挂 decision (cancel gate, watch, relay, spread cancel) reads the phases here, never
    the Chinese 投影 (a cancel may be in flight while it still reads "部分成交")."""
    return (
        order.get("submitPhase") == "CONFIRMED"
        and order.get("remoteStatus") == "ALIVE"
        and order.get("cancelPhase") not in {"CONFIRMED", "UNKNOWN"}
    )


# How a confirmed batch's child orders roll up. Approval is not execution: an order
# that is still PREPARED was never sent, and only a real gateway ambiguity is unknown.
# Every value `order_display_status` can return is listed here (a guard test enumerates them).
# "已发送" is the one claim that must never be a default: a status outside these sets
# falls into "结果未知", so a new display status can never silently become a send receipt.
# 部分成交后已结束 (#932): some lots filled, the rest is off the book (cancelled, expired with the
# trading day, or ended by an error frame); "部分成交" alone is an order still working.
PARTIAL_ENDED = "部分成交后已结束"
NOT_SENT_STATUSES = frozenset({"待发送", "已放弃"})
SENT_STATUSES = frozenset({"已受理", "部分成交", "部分成交后已结束", "已成交", "已拒绝", "已撤销"})
UNKNOWN_STATUSES = frozenset({"结果未知", "身份异常", "回报异常"})


def closable_buckets(position: dict[str, Any]) -> tuple[int, int]:
    """今昨仓各自的实际可平量：持仓量先扣冻结，不小于 0。"""
    today = max(0, int(position.get("todayVolume") or 0) - int(position.get("frozenToday") or 0))
    historical = max(0, int(position.get("historicalVolume") or 0) - int(position.get("frozenHistorical") or 0))
    return today, historical


def close_allocation(position: dict[str, Any], *, volume: int, preference: str,
                     bucket: str | None) -> dict[str, int]:
    """上期所/能源中心一笔平仓在今昨两侧的手数分配，估算、AI 平仓候选和限量批量平仓共用
    这一份（#480）：`bucket` 显式给 today/historical 时只分那一侧，否则按用户偏好
    （设置 → 交易 → 平今平昨）跨侧分配。超出可平量的部分不分进来；是否裁剪或拒绝由调用方决定，
    发单前仍按最新持仓重新核对。"""
    today, historical = closable_buckets(position)
    if bucket in {"today", "historical"}:
        order = [(bucket, today if bucket == "today" else historical)]
    elif preference == "today":
        order = [("today", today), ("historical", historical)]
    elif preference == "historical":
        order = [("historical", historical), ("today", today)]
    else:
        # auto：昨仓够就整笔平昨，今仓够就整笔平今，跨两侧时今先昨后。
        order = [("historical", historical), ("today", today)] if volume <= historical \
            else [("today", today), ("historical", historical)]
    parts = {"today": 0, "historical": 0}
    remaining = volume
    for name, closable in order:
        used = min(remaining, closable)
        parts[name] = used
        remaining -= used
    return parts


def close_yesterday_first_split(position: dict[str, Any], *, volume: int) -> dict[str, int]:
    """非上期所/能源中心一笔平仓在今昨两侧的手数拆分（#711）：先昨后今——昨仓可平量
    （扣 `frozenHistorical`）先取平昨，剩余从今仓（扣 `frozenToday`）取平今，与本站
    模拟盘实扣 `paper_book._fee_rate_commission` 同一口径（sidecar 是另一服务，不跨服务
    import，测试里写明对照）。今昨字段缺失（provider 不分桶）时拿不到拆分，退回总持仓
    扣冻结、整笔按昨仓。超出可平量的部分不拆进来。"""
    if "todayVolume" not in position and "historicalVolume" not in position:
        closable = max(0, int(position.get("volume") or 0)
                       - int(position.get("frozenToday") or 0)
                       - int(position.get("frozenHistorical") or 0))
        return {"historical": min(volume, closable), "today": 0}
    today, historical = closable_buckets(position)
    used_historical = min(volume, historical)
    return {"historical": used_historical, "today": min(volume - used_historical, today)}


# The one projection from an order's local phases and its remote report to the status users
# see; the order list and the AI activity feed both use it (#483).
def order_display_status(
    *, remote_status: str | None, submit_phase: str, cancel_phase: str, volume: int,
    order: Any,
) -> str:
    if remote_status == "IDENTITY_MISMATCH":
        return "身份异常"
    if remote_status == "REPORT_INVALID":
        return "回报异常"
    if submit_phase == "ABANDONED":
        return "已放弃"
    if submit_phase == "PREPARED":
        return "待发送"
    if submit_phase == "UNKNOWN" or cancel_phase == "UNKNOWN":
        return "结果未知"
    if not isinstance(order, dict):
        return "结果未知"
    filled = max(0, volume - int(order.get("volumeLeft", volume)))
    if str(order.get("status")) == "ALIVE":
        return "部分成交" if filled else "已受理"
    if str(order.get("status")) == "FINISHED":
        if filled >= volume:
            return "已成交"
        if filled:
            # Ended after a partial fill (#932): cancelled, expired with the trading day, or an
            # error frame — the order is off the book, so never the still-working "部分成交"; the
            # lots that filled (`filledVolume`) are a fact an error flag cannot erase (#518).
            return "部分成交后已结束"  # PARTIAL_ENDED, spelled out for the status guard test
        return "已拒绝" if bool(order.get("isError")) else "已撤销"
    return "结果未知"


def close_batch_execution(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """What actually happened to a batch, counted from its persisted child orders.
    The parent's own `candidate_recorded` only ever meant "the user confirmed"."""
    statuses = [str(order.get("displayStatus") or "结果未知") for order in orders]
    not_sent = sum(1 for status in statuses if status in NOT_SENT_STATUSES)
    sent = sum(1 for status in statuses if status in SENT_STATUSES)
    unknown = len(statuses) - not_sent - sent
    parts = [
        f"{count} 笔{label}"
        for count, label in ((sent, "已发送"), (not_sent, "未发送"), (unknown, "结果未知"))
        if count
    ]
    return {
        "total": len(statuses), "sent": sent, "notSent": not_sent, "unknown": unknown,
        "filled": sum(1 for status in statuses if status == "已成交"),
        "text": "已确认，" + "、".join(parts) if parts else "已确认，没有子委托",
    }


def order_needs_reconciliation(order: dict[str, Any]) -> bool:
    if order.get("remoteStatus") in ANOMALOUS_REMOTE_STATUSES:
        return True
    if order.get("submitPhase") in {"ABANDONED", "PREPARED"}:
        return False
    if order.get("submitPhase") == "UNKNOWN" or order.get("cancelPhase") == "UNKNOWN":
        return True
    report = order.get("report") or {}
    return (report.get("order") or {}).get("status") not in {"ALIVE", "FINISHED"}


class SimulationGateway(Protocol):
    async def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]: ...
    async def query_order(self, client_order_id: str) -> dict[str, Any]: ...
    async def cancel_order_once(
        self, client_order_id: str, command_id: str
    ) -> dict[str, Any]: ...


class SimulationOrderService:
    """One durable link per approved proposal; TqKq remains the order fact source."""

    def __init__(
        self,
        *,
        store: RuntimeStore,
        data_provider: Any,
        gateway: SimulationGateway | None = None,
        policies: RiskPolicyProvider | None = None,
        instruments: InstrumentSpecProvider | None = None,
        trading_clock: TradingClock | None = None,
        historical_price_limits_unavailable: bool = False,
        trading_account_id: str | None = None,
        fee_settings: Any | None = None,
    ) -> None:
        self.store = store
        self.account_id = trading_account_id or store.default_trading_account_id
        self.data_provider = data_provider
        self.gateway = gateway
        # 本站模拟盘按交易所标准/自定义费率扣费（2026-10-08）：paper 账户提交时把生效
        # 费率随请求带给撮合引擎；快期账户不用（实扣由快期决定）。
        self.fee_settings = fee_settings
        self.policies = policies or RiskPolicyProvider(store)
        self.instruments = instruments or InstrumentSpecProvider()
        self.trading_clock = trading_clock
        self.historical_price_limits_unavailable = historical_price_limits_unavailable
        # Called after an order link changes state (prepared, sent, cancelled, reported)
        # so the runtime feed pushes the activity without waiting for a provider snapshot.
        self.on_change: Callable[[], None] | None = None

    def recover_interrupted_calls(self) -> None:
        """Restart recovery for this account: a submit or cancel call a previous
        holder started but never saw answered is UNKNOWN now — the next query
        settles it, nothing is resent. Called by the side that runs this
        account's loops (`recover_interrupted_execution`), never at construction."""
        now = utc_now()
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                "UPDATE tqkq_sim_order_links SET submit_phase = 'UNKNOWN', updated_at = ? "
                "WHERE submit_phase = 'CALL_STARTED' AND trading_account_id = ?",
                (now, self.account_id),
            )
            self.store.connection.execute(
                "UPDATE tqkq_sim_order_links SET cancel_phase = 'UNKNOWN', updated_at = ? "
                "WHERE cancel_phase = 'CALL_STARTED' AND trading_account_id = ?",
                (now, self.account_id),
            )

    @property
    def enabled(self) -> bool:
        return self.gateway is not None

    def trading_now(self) -> datetime | None:
        return self.trading_clock() if self.trading_clock is not None else None

    def _account_pnl(self) -> Decimal | None:
        try:
            account = self.data_provider.account_summary("")
            accepted = {"normal"} | ({"simulated"} if self.gateway is None else set())
            if account.get("dataStatus") not in accepted or bool(
                account.get("isCached")
            ):
                return None
            value = account.get("todayNetPnl", account.get("todayPnl"))
            return Decimal(str(value)) if value is not None else None
        except (KeyError, RuntimeError, TypeError, ValueError):
            return None

    def risk_check(self, payload: dict[str, Any], user_id: str) -> dict[str, Any]:
        """Deterministic pre-trade risk for a proposal payload against the account's current day."""
        return calculate_trade_risk(
            payload,
            user_id=user_id,
            policies=self.policies,
            instruments=self.instruments,
            today_pnl=self._account_pnl(),
            now=self.trading_now(),
        )

    @staticmethod
    def _direction(side: str) -> str:
        if side in {"buy_open", "buy_close"}:
            return "BUY"
        if side in {"sell_open", "sell_close"}:
            return "SELL"
        raise SimulationOrderError("invalid_order_side")

    def _close_position(
        self,
        *,
        symbol: str,
        side: str,
        volume: int,
        position_bucket: str | None,
        allow_fixture: bool,
    ) -> tuple[str, int]:
        if side not in CLOSE_SIDES:
            raise SimulationOrderError("close_side_required")
        account = self.data_provider.account_summary("")
        accepted = {"normal"} | ({"simulated"} if allow_fixture else set())
        if account.get("dataStatus") not in accepted:
            raise SimulationOrderError("account_not_current")
        exchange = symbol.split(".", 1)[0].upper()
        if exchange in CLOSE_TODAY_EXCHANGES:
            if position_bucket not in {"today", "historical"}:
                raise SimulationOrderError("close_bucket_required")
            offset = "CLOSETODAY" if position_bucket == "today" else "CLOSE"
        else:
            if position_bucket not in {None, "all"}:
                raise SimulationOrderError("close_bucket_not_supported")
            offset = "CLOSE"
        position_direction = "short" if side == "buy_close" else "long"
        position = next(
            (
                item
                for item in self.data_provider.positions()
                if str(item.get("symbol", "")).upper() == symbol.upper()
                and item.get("direction") == position_direction
            ),
            None,
        )
        if position is None:
            raise SimulationOrderError("close_position_not_found")
        if exchange in CLOSE_TODAY_EXCHANGES:
            today, historical = closable_buckets(position)
            available = today if offset == "CLOSETODAY" else historical
        else:
            available = max(0, int(position.get("volume") or 0) - int(
                position.get("frozenToday") or 0
            ) - int(position.get("frozenHistorical") or 0))
        if volume > available:
            raise SimulationOrderError("close_volume_unavailable")
        return offset, available

    @staticmethod
    def _close_risk(multiplier: Decimal, tick: Decimal) -> dict[str, Any]:
        return {
            "passed": True,
            "limit": 0.0,
            "estimatedLoss": 0.0,
            "reason": "仅减少现有持仓；发送前会再次核对持仓和冻结量。",
            "contractMultiplier": float(multiplier),
            "priceTick": format(tick, "f"),
            "policyVersion": 0,
            "checks": {
                "positionAvailable": True,
                "reduceOnly": True,
                "quoteFresh": True,
            },
        }

    def build_candidate(
        self,
        *,
        user_id: str,
        symbol: str,
        side: str,
        volume: int,
        price_intent: str,
        reason: str,
        limit_price: float | None = None,
        position_bucket: str | None = None,
    ) -> dict[str, Any]:
        if side not in OPEN_SIDES | CLOSE_SIDES:
            raise SimulationOrderError("invalid_order_side")
        if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
            raise SimulationOrderError("invalid_order_volume")
        if price_intent not in {"passive", "aggressive", "last"}:
            raise SimulationOrderError("invalid_price_intent")
        market = self.data_provider.market_snapshot("", symbol.strip())
        quote_is_fresh(market, allow_fixture=self.gateway is None, now=self.trading_now())
        tick = finite_positive(market.get("priceTick"), "price_tick_missing")
        multiplier = finite_positive(
            market.get("volumeMultiple"), "volume_multiple_missing"
        )
        if limit_price is None:
            resolved_limit_price = self._intent_price(market, side=side, price_intent=price_intent)
        else:
            resolved_limit_price = finite_positive(
                limit_price, "invalid_limit_price"
            )
        self._validate_price(resolved_limit_price, tick, market)
        offset, available = "OPEN", None
        if side in CLOSE_SIDES:
            offset, available = self._close_position(
                symbol=str(market["symbol"]),
                side=side,
                volume=volume,
                position_bucket=position_bucket,
                allow_fixture=self.gateway is None,
            )
        payload = {
            "symbol": str(market["symbol"]),
            "executionEligibleAtCreation": self.enabled,
            "name": str(market["name"]),
            "side": side,
            "offset": offset,
            "volume": volume,
            "limitPrice": float(resolved_limit_price),
            "contractMultiplier": float(multiplier),
            "priceTick": format(tick, "f"),
            "priceIntent": price_intent,
            "reason": reason.strip(),
            "quoteDatetime": str(market["quoteDatetime"]),
            "marketSnapshot": {
                key: market.get(key)
                for key in (
                    "price",
                    "bidPrice1",
                    "askPrice1",
                    "priceTick",
                    "volumeMultiple",
                    "upperLimit",
                    "lowerLimit",
                    "quoteDatetime",
                    "capturedAt",
                )
            },
        }
        if offset == "OPEN":
            policy = self.policies.get(user_id=user_id, symbol=str(market["symbol"]))
            if policy.max_loss_per_trade is not None:
                distance = policy.max_loss_per_trade / (multiplier * Decimal(volume))
                stop_price = (
                    ceil_to_tick(resolved_limit_price - distance, tick)
                    if side == "buy_open"
                    else floor_to_tick(resolved_limit_price + distance, tick)
                )
                payload["stopPrice"] = float(stop_price if stop_price > 0 else tick)
            payload["riskCheck"] = self.risk_check(payload, user_id)
        else:
            payload.update(
                {
                    "positionBucket": position_bucket or "all",
                    "availableVolumeAtCreation": available,
                    "riskCheck": self._close_risk(multiplier, tick),
                }
            )
        return payload

    @classmethod
    def _intent_price(cls, market: dict[str, Any], *, side: str, price_intent: str) -> Decimal:
        """The limit price a price intent means on this quote (对手价 / 挂单价 / 最新价)."""
        price_field = {
            ("BUY", "passive"): "bidPrice1",
            ("BUY", "aggressive"): "askPrice1",
            ("SELL", "passive"): "askPrice1",
            ("SELL", "aggressive"): "bidPrice1",
        }.get((cls._direction(side), price_intent), "price")
        return finite_positive(market.get(price_field), "limit_price_unavailable")

    def reprice_protective_close(
        self, client_order_id: str, *, user_id: str, price_intent: str, volume: int,
        position_bucket: str | None,
    ) -> str:
        """保护平仓重派前按原价格意图重新定价 (#750)。只用于确定未派发（PREPARED：从未发出，或
        引擎/网关确定拒收、命令没进账本，如 paper_settlement_pending、NOT_DISPATCHED）的保护
        平仓单；用户自己定价的单不在此列。同一 clientOrderId 改写限价：两种网关对确定未派发的
        提交都不留命令记录，原身份仍可用；万一判断错（原单其实已被接收），网关按原身份去重
        回读到旧价，记成身份异常而不是多发一笔平仓。

        重验：新鲜行情、合约身份、价格网格与涨跌停、当前持仓与今昨桶可平量。返回
        "repriced"（限价已改写；委托请求与价格决策审计同一事务）、"unchanged"（价格没变，什么都不写）、
        "replan"（今昨桶或手数已变，原请求身份不再成立：调用方放弃这笔、按新身份重建）。"""
        if self.gateway is None:
            raise SimulationOrderError("simulation_gateway_disabled")
        if price_intent not in {"passive", "aggressive", "last"}:
            raise SimulationOrderError("invalid_price_intent")
        with self.store.transaction() as connection:
            current = self.get(client_order_id, user_id=user_id, connection=connection)
            if current["submitPhase"] != "PREPARED":
                raise SimulationOrderError("order_not_prepared")
            record = self.store.get(str(current["proposalId"]), connection=connection)
            payload = dict(record["payload"])
            side = str(payload.get("side") or "")
            if payload.get("automationSource") != "deterministic_stop" or side not in CLOSE_SIDES:
                raise SimulationOrderError("reprice_not_protective")
            request = dict(current["request"])
            market = self.data_provider.market_snapshot("", str(request["symbol"]))
            quote_is_fresh(market, allow_fixture=False, now=self.trading_now())
            if str(market.get("symbol", "")).casefold() != str(request["symbol"]).casefold():
                raise SimulationOrderError("symbol_identity_mismatch")
            tick = finite_positive(market.get("priceTick"), "price_tick_missing")
            offset, _ = self._close_position(
                symbol=str(request["symbol"]), side=side, volume=volume,
                position_bucket=position_bucket, allow_fixture=False,
            )
            if offset != request["offset"] or volume != int(request["volume"]):
                return "replan"
            price = self._intent_price(market, side=side, price_intent=price_intent)
            self._validate_price(price, tick, market)
            previous = decimal_value(request["limitPrice"])
            if previous == price:
                return "unchanged"
            request["limitPrice"] = float(price)
            now = utc_now()
            cursor = connection.execute(
                """UPDATE tqkq_sim_order_links SET request_json = ?, updated_at = ?
                WHERE client_order_id = ? AND user_id = ? AND submit_phase = 'PREPARED'""",
                (json.dumps(request, ensure_ascii=False, separators=(",", ":")), now,
                 client_order_id, user_id),
            )
            if cursor.rowcount != 1:
                raise SimulationOrderError("order_not_prepared")
            # 提案是已记录的授权，不可改（terminal_proposal_immutable）；价格决策与它依据的
            # 行情记在同一事务的审计里，委托请求是发出价格的唯一事实。
            self.store.audit(str(current["proposalId"]), "protective-reprice", {
                "clientOrderId": client_order_id, "priceIntent": price_intent,
                "previousLimitPrice": float(previous), "limitPrice": float(price),
                "quoteDatetime": str(market["quoteDatetime"]),
                "marketSnapshot": {key: market.get(key) for key in (
                    "price", "bidPrice1", "askPrice1", "upperLimit", "lowerLimit", "quoteDatetime",
                )},
                "reason": current.get("lastReadError") or "order_not_dispatched",
            }, connection=connection)
        self._notify_change()
        return "repriced"

    def _validate_price(self, price: Decimal, tick: Decimal, market: dict[str, Any]) -> None:
        if not price_on_tick(price, tick):
            raise SimulationOrderError("limit_price_not_on_tick")
        # TqBacktest does not expose historical exchange price bands. This flag
        # is set only by the isolated replay runtime, never by a request/quote.
        if self.historical_price_limits_unavailable:
            return
        lower = finite_positive(market.get("lowerLimit"), "lower_limit_missing")
        upper = finite_positive(market.get("upperLimit"), "upper_limit_missing")
        if lower > upper or not lower <= price <= upper:
            raise SimulationOrderError("limit_price_outside_limits")

    def prepare(
        self,
        *,
        proposal_id: str,
        user_id: str,
        payload: dict[str, Any],
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        if self.gateway is None:
            raise SimulationOrderError("simulation_gateway_disabled")
        if payload.get("executionEligibleAtCreation") is not True:
            raise SimulationOrderError("proposal_not_execution_eligible")
        market = self.data_provider.market_snapshot("", str(payload["symbol"]))
        quote_is_fresh(market, allow_fixture=False, now=self.trading_now())
        tick = finite_positive(market.get("priceTick"), "price_tick_missing")
        current_multiplier = finite_positive(
            market.get("volumeMultiple"), "volume_multiple_missing"
        )
        price = finite_positive(payload.get("limitPrice"), "invalid_limit_price")
        self._validate_price(price, tick, market)
        checked = dict(payload)
        checked["priceTick"] = format(tick, "f")
        checked["contractMultiplier"] = float(current_multiplier)
        offset = str(payload.get("offset") or "OPEN")
        if offset == "OPEN":
            risk = self.risk_check(checked, user_id)
        else:
            expected_offset, _ = self._close_position(
                symbol=str(market["symbol"]),
                side=str(payload.get("side")),
                volume=int(payload.get("volume") or 0),
                position_bucket=payload.get("positionBucket"),
                allow_fixture=False,
            )
            if expected_offset != offset:
                raise SimulationOrderError("close_offset_mismatch")
            risk = self._close_risk(current_multiplier, tick)
        if not risk["passed"]:
            raise SimulationOrderError("risk_check_failed")
        payload.update({
            "quoteDatetime": str(market["quoteDatetime"]),
            "priceTick": format(tick, "f"),
            "contractMultiplier": float(current_multiplier),
            "riskCheck": risk,
            "marketSnapshot": {
                key: market.get(key) for key in (
                    "price", "bidPrice1", "askPrice1", "priceTick", "volumeMultiple",
                    "upperLimit", "lowerLimit", "quoteDatetime", "capturedAt",
                )
            },
        })
        client_order_id = f"AIT{uuid4().hex}"
        request = {
            "commandId": f"submit:{client_order_id}",
            "clientOrderId": client_order_id,
            "symbol": str(market["symbol"]),
            "direction": self._direction(str(payload["side"])),
            "offset": offset,
            "volume": int(payload["volume"]),
            "priceType": "LIMIT",
            "limitPrice": float(price),
        }
        if getattr(self.data_provider, "mode", "") == "paper" and self.fee_settings is not None:
            # 本站模拟盘的扣费费率随单走（撮合内核按它计佣金；拿不到就不带，退回行情源口径）。
            fee_rate = effective_fee_rate(
                self.fee_settings.get(user_id), str(market["symbol"]),
                root=Path(self.fee_settings.root).parent)
            if fee_rate is not None:
                request["feeRate"] = fee_rate
        now, link_id = utc_now(), str(uuid4())
        connection.execute(
            """INSERT INTO tqkq_sim_order_links
            (id, user_id, proposal_id, client_order_id, request_json, submit_phase,
             cancel_phase, last_remote_status, last_report_json, last_synced_at,
             created_at, updated_at, trading_account_id)
            VALUES (?, ?, ?, ?, ?, 'PREPARED', 'NONE', NULL, NULL, NULL, ?, ?, ?)""",
            (
                link_id,
                user_id,
                proposal_id,
                client_order_id,
                json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
                self.account_id,
            ),
        )
        self._notify_change()
        return self.get(client_order_id, user_id=user_id, connection=connection)

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        # orjson (fast_json, 2026-10-13): every activity frame and order read decodes these. Both
        # columns hold only what this service wrote, the validated request and the gateway's report
        # (volumes, prices, nanosecond times within 64 bits, ids as strings), so no integer is beyond
        # 64 bits, the one input orjson reads differently; NaN tokens fall back to the stdlib.
        request = fast_json.loads(row["request_json"])
        report = (
            fast_json.loads(row["last_report_json"]) if row["last_report_json"] else None
        )
        return {
            "id": row["id"],
            "proposalId": row["proposal_id"],
            "clientOrderId": row["client_order_id"],
            "accountId": row["trading_account_id"],
            "request": request,
            "submitPhase": row["submit_phase"],
            "cancelPhase": row["cancel_phase"],
            "cancelRequestedAt": row["cancel_requested_at"],
            "remoteStatus": row["last_remote_status"],
            "report": report,
            "filledVolume": (
                max(
                    0,
                    int(request["volume"])
                    - int(report["order"].get("volumeLeft", request["volume"])),
                )
                if report and isinstance(report.get("order"), dict)
                else 0
            ),
            "displayStatus": self._display_status(row, request, report),
            "lastSyncedAt": row["last_synced_at"],
            "lastReadError": row["last_read_error"],
            "factRevision": row["fact_revision"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "accountLabel": "本站模拟账户" if self.data_provider.mode == "paper" else "快期模拟账户",
            "liveTrading": "disabled",
            "automaticStopLoss": False,
        }

    @staticmethod
    def _display_status(
        row: sqlite3.Row, request: dict[str, Any], report: dict[str, Any] | None
    ) -> str:
        return order_display_status(
            remote_status=row["last_remote_status"], submit_phase=row["submit_phase"],
            cancel_phase=row["cancel_phase"], volume=int(request["volume"]),
            order=report.get("order") if report else None,
        )

    def get(
        self,
        client_order_id: str,
        *,
        user_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        row = (
            (connection or self.store.connection)
            .execute(
                """SELECT * FROM tqkq_sim_order_links
                WHERE client_order_id = ? AND user_id = ? AND trading_account_id = ?""",
                (client_order_id, user_id, self.account_id),
            )
            .fetchone()
        )
        if row is None:
            raise KeyError(client_order_id)
        return self._public(row)

    def list(self, *, user_id: str) -> list[dict[str, Any]]:
        rows = self.store.connection.execute(
            """SELECT * FROM tqkq_sim_order_links
            WHERE user_id = ? AND trading_account_id = ? ORDER BY updated_at DESC""",
            (user_id, self.account_id),
        ).fetchall()
        return [self._public(row) for row in rows]

    def list_on_account(self) -> list[dict[str, Any]]:
        """Every order on this trading account, across the users it is granted to (#699).
        An internal risk read (同一快照的在途核验) through the same normalized projection as
        `list` — never exposed through a public interface, which stays user-scoped."""
        rows = self.store.connection.execute(
            """SELECT * FROM tqkq_sim_order_links
            WHERE trading_account_id = ? ORDER BY updated_at DESC""",
            (self.account_id,),
        ).fetchall()
        return [self._public(row) for row in rows]

    def _identity_matches(
        self, request: dict[str, Any], report: dict[str, Any]
    ) -> bool:
        order = report.get("order")
        if not isinstance(order, dict):
            return False
        try:
            return all(
                (
                    order.get("clientOrderId") == request["clientOrderId"],
                    order.get("symbol") == request["symbol"],
                    order.get("direction") == request["direction"],
                    order.get("offset") == request["offset"],
                    order.get("priceType") == "LIMIT",
                    int(order.get("volume", -1)) == int(request["volume"]),
                    decimal_value(order.get("limitPrice")) == decimal_value(request["limitPrice"]),
                )
            )
        except (InvalidOperation, TypeError, ValueError):
            return False

    def _notify_change(self) -> None:
        # Inside a store transaction the change waits for its commit: firing now
        # would let a reader (the order backfill) commit the caller's transaction
        # halfway. Outside one it fires immediately.
        self.store.after_commit(self._fire_change)

    def _fire_change(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _save_report(
        self, client_order_id: str, report: dict[str, Any], *, user_id: str
    ) -> dict[str, Any]:
        try:
            return self._save_report_locked(client_order_id, report, user_id=user_id)
        finally:
            self._notify_change()

    def _save_report_locked(
        self, client_order_id: str, report: dict[str, Any], *, user_id: str
    ) -> dict[str, Any]:
        # Read/merge/write under one database boundary. Request completion order
        # is not exchange fact order; cumulative fills and terminal facts persist.
        # A report without the order may mean its trading day ended: the exchange calendar
        # (a sidecar read on a cache miss) is fetched here, never inside the transaction.
        trading_days = (calendar_trading_days(self.data_provider)
                        if report.get("ordersInitialized") is True and report.get("found") is not True else [])
        with self.store.transaction() as connection:
            current = self.get(client_order_id, user_id=user_id, connection=connection)
            if report.get("ordersInitialized") is not True:
                return current
            if (report.get("commandStatus") == "NOT_DISPATCHED" and report.get("found") is not True
                    and current["submitPhase"] in {"CALL_STARTED", "UNKNOWN"}):
                # The query of a submit whose call timed out (or was dropped by a gateway
                # rebuild): the gateway knows it never sent it. Back to 未发送 with the reason —
                # the same definite "not sent" as a direct NOT_DISPATCHED answer (#647), not a
                # NOT_FOUND left as 结果未知 forever.
                connection.execute("""UPDATE tqkq_sim_order_links SET submit_phase = 'PREPARED',
                    last_read_error = ?, updated_at = ?
                    WHERE client_order_id = ? AND user_id = ? AND submit_phase IN ('CALL_STARTED', 'UNKNOWN')""",
                    (str(report.get("errorCode") or "gateway_not_dispatched"), utc_now(), client_order_id, user_id))
                return self.get(client_order_id, user_id=user_id, connection=connection)
            found = report.get("found") is True and isinstance(report.get("order"), dict)
            order = report.get("order") if found else None
            remote_status = str(order.get("status") or "UNKNOWN") if order else "NOT_FOUND"
            anomaly = None
            if found and not self._identity_matches(current["request"], report):
                anomaly = "IDENTITY_MISMATCH"
            elif found:
                try:
                    left = Decimal(str(order["volumeLeft"]))
                    volume = Decimal(str(order["volume"]))
                    if (not left.is_finite() or not volume.is_finite()
                            or left != left.to_integral_value() or volume != volume.to_integral_value()
                            or not 0 <= left <= volume or remote_status not in {"ALIVE", "FINISHED"}
                            or (order.get("isError") and left < volume)):
                        anomaly = "REPORT_INVALID"
                except (KeyError, InvalidOperation, TypeError, ValueError):
                    anomaly = "REPORT_INVALID"
                if anomaly is None:
                    order = {**order, "volumeLeft": int(left), "volume": int(volume)}
                    report = {**report, "order": order}
            previous = (current["report"] or {}).get("order")
            if (isinstance(previous, dict) and previous.get("status") == "FINISHED" and remote_status == "FINISHED"
                    and bool(previous.get("isError")) != bool(order.get("isError"))):
                anomaly = anomaly or "REPORT_INVALID"
            if anomaly:
                connection.execute("""UPDATE tqkq_sim_order_links SET
                    submit_phase = CASE WHEN submit_phase = 'CALL_STARTED' THEN 'UNKNOWN' ELSE submit_phase END,
                    cancel_phase = CASE WHEN cancel_phase = 'CALL_STARTED' THEN 'UNKNOWN' ELSE cancel_phase END,
                    last_remote_status = ?,
                    last_read_error = ?, last_error_report_json = ?, updated_at = ?
                    WHERE client_order_id = ? AND user_id = ?""",
                    (anomaly, anomaly, json.dumps(report, ensure_ascii=False), utc_now(), client_order_id, user_id))
                return self.get(client_order_id, user_id=user_id, connection=connection)
            if current["remoteStatus"] in ANOMALOUS_REMOTE_STATUSES:
                return current  # A normal report cannot silently dismiss an identity/quantity alarm.
            if not found and isinstance(previous, dict) and previous.get("status") == "ALIVE" \
                    and self._trading_day_ended(current, trading_days):
                # A limit order lives for one trading day: after that day's settlement the broker no
                # longer lists it. The unfilled rest expired; fills are the last report's or the
                # recorded trades', whichever is more.
                filled = max(int(previous["volume"]) - int(previous["volumeLeft"]), self._recorded_fills(connection, client_order_id))
                order = {**previous, "volumeLeft": max(0, int(previous["volume"]) - filled), "status": "FINISHED",
                         "isOnline": False, "isDead": True, "expired": True, "lastMsg": "交易日结束，未成交部分已失效"}
                report = {**report, "found": True, "order": order}
                found, remote_status = True, "FINISHED"
            if isinstance(previous, dict):
                regresses = (not found or int(order["volumeLeft"]) > int(previous.get("volumeLeft", current["request"]["volume"]))
                             or (previous.get("status") == "FINISHED" and remote_status != "FINISHED"))
                if regresses:
                    connection.execute("""UPDATE tqkq_sim_order_links SET last_read_error = 'stale_order_report',
                        last_error_report_json = ? WHERE client_order_id = ? AND user_id = ?""",
                        (json.dumps(report, ensure_ascii=False), client_order_id, user_id))
                    return self.get(client_order_id, user_id=user_id, connection=connection)
            cancel_phase = current["cancelPhase"]
            if cancel_phase in {"CALL_STARTED", "UNKNOWN"}:
                cancel_phase = "CONFIRMED" if remote_status == "FINISHED" else "UNKNOWN"
            now = utc_now()
            connection.execute("""UPDATE tqkq_sim_order_links SET submit_phase = ?, cancel_phase = ?,
                last_remote_status = ?, last_report_json = ?, last_synced_at = ?, updated_at = ?, last_read_error = NULL
                WHERE client_order_id = ? AND user_id = ?""",
                ("CONFIRMED" if found else "UNKNOWN", cancel_phase, remote_status,
                 json.dumps(report, ensure_ascii=False, separators=(",", ":")), now, now, client_order_id, user_id))
            return self.get(client_order_id, user_id=user_id, connection=connection)

    def _trading_day_ended(self, link: dict[str, Any], trading_days: list[str]) -> bool:
        """The trading day this limit order was entered in is over (2026-10-12): a limit order lives
        for that one day, so it is anchored on the order's own insert time (the broker's, else when
        it was placed — `order_backfill.fill_time`), not on the last report, which a read after the
        18:00 turn would date to the next day. Both sides go through the Runtime's one trading-day
        rule with the exchange calendar, so the first day after a holiday ends a pre-holiday order
        and a Friday-night order stays alive into Monday.

        Without the calendar (unreadable, or a provider that has none — a replay) the weekend rule
        stands in, as it did before the calendar was wired (#790 review): skipping expiry instead
        would leave a GFD order the broker no longer lists `stale_order_report` forever and block
        what follows it. The rule only applies to an order the broker has stopped listing, so a
        holiday it cannot see can at worst date the end of such an order early."""
        entered = fill_time(link)
        if entered is None:
            return False
        now = self.trading_now() or datetime.now(UTC)
        return trading_day_at(now, trading_days) > trading_day_at(entered, trading_days)

    def _recorded_fills(self, connection: sqlite3.Connection, client_order_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(volume), 0) FROM account_trades WHERE trading_account_id = ? AND client_order_id = ?",
            (self.account_id, client_order_id)).fetchone()
        return int(row[0] or 0)

    def _read_failed(self, client_order_id: str, *, user_id: str, phase: str = "submit") -> None:
        column = "cancel_phase" if phase == "cancel" else "submit_phase"
        with self.store.lock, self.store.connection:
            self.store.connection.execute(f"""UPDATE tqkq_sim_order_links
                SET {column} = CASE WHEN {column} = 'CALL_STARTED' THEN 'UNKNOWN' ELSE {column} END,
                last_read_error = 'order_transport_unavailable'
                WHERE client_order_id = ? AND user_id = ?""", (client_order_id, user_id))
        self._notify_change()

    async def dispatch(self, client_order_id: str, *, user_id: str) -> dict[str, Any]:
        if self.gateway is None:
            raise SimulationOrderError("simulation_gateway_disabled")
        started = time.monotonic()
        current = self.get(client_order_id, user_id=user_id)
        market = self.data_provider.market_snapshot("", current["request"]["symbol"])
        quote_is_fresh(market, allow_fixture=False, now=self.trading_now())
        if current["submitPhase"] == "PREPARED":
            if str(market.get("symbol", "")).casefold() != current["request"]["symbol"].casefold():
                raise SimulationOrderError("symbol_identity_mismatch")
            tick = finite_positive(market.get("priceTick"), "price_tick_missing")
            multiplier = finite_positive(market.get("volumeMultiple"), "volume_multiple_missing")
            link = self.store.connection.execute(
                "SELECT protection_json FROM tqkq_sim_order_links WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
            if link["protection_json"]:
                protection = json.loads(link["protection_json"])
                if tick != Decimal(protection["priceTick"]) or multiplier != Decimal(protection["contractMultiplier"]):
                    raise SimulationOrderError("strategy_metadata_changed")
            self._validate_price(
                finite_positive(current["request"]["limitPrice"], "invalid_limit_price"), tick, market,
            )
            payload = dict(self.store.get(current["proposalId"])["payload"])
            payload.update({"priceTick": format(tick, "f"), "contractMultiplier": float(multiplier)})
            if current["request"]["offset"] == "OPEN":
                if not self.risk_check(payload, user_id)["passed"]:
                    raise SimulationOrderError("risk_check_failed")
            else:
                expected_offset, _ = self._close_position(
                    symbol=current["request"]["symbol"],
                    side=str(payload.get("side")),
                    volume=int(current["request"]["volume"]),
                    position_bucket=payload.get("positionBucket"),
                    allow_fixture=False,
                )
                if expected_offset != current["request"]["offset"]:
                    raise SimulationOrderError("close_offset_mismatch")
        now = utc_now()
        with self.store.lock, self.store.connection:
            cursor = self.store.connection.execute(
                """UPDATE tqkq_sim_order_links SET submit_phase = 'CALL_STARTED', updated_at = ?
                WHERE client_order_id = ? AND user_id = ? AND submit_phase = 'PREPARED'""",
                (now, client_order_id, user_id),
            )
        if cursor.rowcount != 1:
            self.get(client_order_id, user_id=user_id)
            raise SimulationOrderError("order_not_prepared")
        from .operations import owned_operation
        # Keep the existing no-await fence through CALL_STARTED. Only then
        # transfer the bounded send and durable result to the operation owner.
        return await owned_operation(
            lambda: self._submit_and_record(client_order_id, user_id, current["request"], started=started), committed=True)

    async def _submit_and_record(self, client_order_id, user_id, request, *, started=None):
        sent = time.monotonic()
        try:
            report = await self.gateway.submit_order_once(request)
            _log_order_timing(client_order_id, report, started=started, sent=sent)
        except SidecarHttpError as error:
            if _paper_settlement_pending(error):
                # 引擎确定性地拒收（等交易所公布结算价，命令没有进账本）：回到
                # 未发送并保留原因——不是“结果未知”，也不是网关中断；用户稍后
                # 在委托栏点“继续”即可重发（2026-10-08 结算改版）。
                return self._return_unsent(client_order_id, user_id=user_id, reason="paper_settlement_pending")
            if error.status == 401:
                # 网关在鉴权处就拒了（号池子进程已换人，旧 token 不认）：命令没进任何
                # 账户，确定未发出（2026-10-12 端口复用审查）。
                return self._return_unsent(client_order_id, user_id=user_id, reason="sidecar_auth_rejected")
            self._read_failed(client_order_id, user_id=user_id)
            return self.get(client_order_id, user_id=user_id)
        except (Exception, asyncio.CancelledError) as error:
            self._read_failed(client_order_id, user_id=user_id)
            if isinstance(error, asyncio.CancelledError):
                raise
            return self.get(client_order_id, user_id=user_id)
        if report.get("commandStatus") == "NOT_DISPATCHED":
            # 这次没派发的原因是网关自己的（errorCode），不再沿用上一次的“等结算价”等旧原因 (#750)。
            return self._return_unsent(client_order_id, user_id=user_id,
                                       reason=str(report.get("errorCode") or "") or None)
        return self._save_report(client_order_id, report, user_id=user_id)

    def _return_unsent(self, client_order_id: str, *, user_id: str, reason: str | None) -> dict[str, Any]:
        """The gateway answered that this submit was never sent: back to 未发送 (PREPARED) with
        the reason kept, so 继续 sends it again after the usual price checks — never 结果未知.
        The reason replaces any earlier one (a gateway answer without a code clears it) — an old
        “等结算价” must not outlive the attempt that hit it (#750)."""
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                """UPDATE tqkq_sim_order_links SET submit_phase = 'PREPARED',
                last_read_error = ?, updated_at = ?
                WHERE client_order_id = ? AND user_id = ? AND submit_phase = 'CALL_STARTED'""",
                (reason, utc_now(), client_order_id, user_id))
        self._notify_change()
        return self.get(client_order_id, user_id=user_id)

    async def query(self, client_order_id: str, *, user_id: str) -> dict[str, Any]:
        if self.gateway is None:
            raise SimulationOrderError("simulation_gateway_disabled")
        current = self.get(client_order_id, user_id=user_id)
        if current["submitPhase"] in {"PREPARED", "ABANDONED"}:
            raise SimulationOrderError("order_not_dispatched")
        try:
            report = await self.gateway.query_order(client_order_id)
        except Exception:
            self._read_failed(client_order_id, user_id=user_id)
            return self.get(client_order_id, user_id=user_id)
        current = self._save_report(client_order_id, report, user_id=user_id)
        return await self._cancel_if_requested(current, user_id=user_id)

    def request_cancel(self, client_order_id: str, *, user_id: str, connection) -> None:
        # Persist intent separately from an attempted cancellation. An UNKNOWN
        # submission may only be queried until its original identity is found.
        connection.execute("""UPDATE tqkq_sim_order_links
            SET cancel_requested_at = COALESCE(cancel_requested_at, ?)
            WHERE client_order_id = ? AND user_id = ?""",
            (utc_now(), client_order_id, user_id))
        self._notify_change()

    async def _cancel_if_requested(self, order: dict[str, Any], *, user_id: str) -> dict[str, Any]:
        if (order["cancelRequestedAt"] and order["remoteStatus"] != "FINISHED"
                and order["displayStatus"] in {"已受理", "部分成交"}):
            return await self.cancel(order["clientOrderId"], user_id=user_id)
        return order

    async def reconcile_cancellations(self) -> None:
        rows = self.store.connection.execute("""SELECT client_order_id, user_id
            FROM tqkq_sim_order_links WHERE cancel_requested_at IS NOT NULL
            AND submit_phase NOT IN ('PREPARED', 'ABANDONED')
            AND COALESCE(last_remote_status, '') != 'FINISHED'
            AND trading_account_id = ?
            ORDER BY updated_at""", (self.account_id,)).fetchall()
        for row in rows:
            await self.query(row["client_order_id"], user_id=row["user_id"])

    async def refresh_resting(self) -> None:
        """Query every order of this account last reported resting (ALIVE): one
        that filled or was cancelled while nobody watched stops counting as
        working once the idle scan has asked."""
        if self.gateway is None:
            return
        with self.store.lock:
            rows = self.store.connection.execute(
                """SELECT client_order_id, user_id FROM tqkq_sim_order_links
                WHERE trading_account_id = ? AND submit_phase = 'CONFIRMED'
                AND last_remote_status = 'ALIVE' ORDER BY updated_at""",
                (self.account_id,)).fetchall()
        for row in rows:
            await self.query(row["client_order_id"], user_id=row["user_id"])

    async def cancel(self, client_order_id: str, *, user_id: str) -> dict[str, Any]:
        if self.gateway is None:
            raise SimulationOrderError("simulation_gateway_disabled")
        current = self.get(client_order_id, user_id=user_id)
        # 还在挂才可撤（#518）: 按阶段判断，已结束的单（含部分成交后结束）直接回不可撤，
        # 不向网关发起撤，也不制造“结果未知”。
        if not order_is_working(current):
            raise SimulationOrderError("order_not_cancelable")
        with self.store.lock, self.store.connection:
            cursor = self.store.connection.execute(
                """UPDATE tqkq_sim_order_links SET cancel_phase = 'CALL_STARTED', updated_at = ?
                WHERE client_order_id = ? AND user_id = ? AND cancel_phase = 'NONE'""",
                (utc_now(), client_order_id, user_id),
            )
        if cursor.rowcount != 1:
            return self.get(client_order_id, user_id=user_id)
        from .operations import owned_operation
        return await owned_operation(
            lambda: self._cancel_and_record(client_order_id, user_id), committed=True)

    async def _cancel_and_record(self, client_order_id, user_id):
        try:
            report = await self.gateway.cancel_order_once(
                client_order_id, f"cancel:{client_order_id}"
            )
        except (Exception, asyncio.CancelledError) as error:
            self._read_failed(client_order_id, user_id=user_id, phase="cancel")
            if isinstance(error, asyncio.CancelledError):
                raise
            return self.get(client_order_id, user_id=user_id)
        if report.get("commandStatus") == "NOT_DISPATCHED":
            with self.store.lock, self.store.connection:
                self.store.connection.execute("UPDATE tqkq_sim_order_links SET cancel_phase = 'NONE', updated_at = ? WHERE client_order_id = ? AND user_id = ? AND cancel_phase = 'CALL_STARTED'", (utc_now(), client_order_id, user_id))
            return self.get(client_order_id, user_id=user_id)
        return self._save_report(client_order_id, report, user_id=user_id)

    def note_dispatch_error(self, client_order_id: str, *, user_id: str, reason: str) -> None:
        """A send that was refused before the gateway call. The order stays PREPARED —
        it was not sent — and the reason is kept so the user sees why, instead of a
        blank "not sent" or, worse, "result unknown". A later report clears it."""
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                """UPDATE tqkq_sim_order_links SET last_read_error = ?, updated_at = ?
                WHERE client_order_id = ? AND user_id = ? AND submit_phase = 'PREPARED'""",
                (reason, utc_now(), client_order_id, user_id),
            )
        self._notify_change()

    def abandon(self, client_order_id: str, *, user_id: str) -> dict[str, Any]:
        with self.store.transaction() as connection:
            self.get(client_order_id, user_id=user_id, connection=connection)
            role = self.store.account_role(account_id=self.account_id, user_id=user_id)
            if role not in {"owner", "operator"}:
                raise SimulationOrderError("account_read_only" if role == "viewer" else "account_not_granted")
            cursor = self.store.connection.execute(
                """UPDATE tqkq_sim_order_links SET submit_phase = 'ABANDONED', updated_at = ?
                WHERE client_order_id = ? AND user_id = ? AND trading_account_id = ? AND submit_phase = 'PREPARED'""",
                (utc_now(), client_order_id, user_id, self.account_id),
            )
        if cursor.rowcount != 1:
            raise SimulationOrderError("order_not_abandonable")
        self._notify_change()
        return self.get(client_order_id, user_id=user_id)

    async def recover_once(
        self, *, user_id: str, attempts: int = 180, interval: float = 1.0,
        run_round: Callable[[Callable[[], Awaitable[bool]]], Awaitable[bool]] | None = None,
    ) -> None:
        if self.gateway is None:
            return

        async def recover():
            return await self._recover_pending(user_id=user_id)

        for attempt in range(attempts):
            completed = await run_round(recover) if run_round is not None else await recover()
            if completed:
                return
            # A replay may advance or close between polls. Never retain its
            # operation lock while waiting for the remote history to initialize.
            if attempt + 1 < attempts:
                await asyncio.sleep(interval)

    async def _recover_pending(self, *, user_id: str) -> bool:
        pending = [
            item
            for item in self.list(user_id=user_id)
            if item["submitPhase"] in {"UNKNOWN", "CONFIRMED"}
            and item["displayStatus"] not in {"已成交", PARTIAL_ENDED, "已撤销", "已拒绝", "身份异常", "回报异常"}
        ]
        if not pending:
            return True
        try:
            first = await self.gateway.query_order(pending[0]["clientOrderId"])
        except Exception:
            return False
        if first.get("ordersInitialized") is not True:
            return False
        current = self._save_report(
            pending[0]["clientOrderId"], first, user_id=user_id
        )
        await self._cancel_if_requested(current, user_id=user_id)
        for item in pending[1:]:
            try:
                await self.query(item["clientOrderId"], user_id=user_id)
            except Exception:
                continue
        return True


def _log_order_timing(client_order_id: str, report: dict[str, Any], *, started: float | None, sent: float) -> None:
    """One line per submitted order: where its time went (2026-09-27, 下单全程打点).
    checks = quote/price/risk checks and the CALL_STARTED write before sending; gateway = the
    round trip to the account's sidecar; queued / exchange = the sidecar's own share (waiting
    for its TqSdk actor, then insert until the exchange acknowledged). The fill time itself is
    the trade's exchange timestamp (account_trades.trade_date_time)."""
    try:
        done = time.monotonic()
        timing = report.get("timing") if isinstance(report.get("timing"), dict) else {}
        order = report.get("order") if isinstance(report.get("order"), dict) else {}
        log.info("order_timing client_order_id=%s checks_ms=%s gateway_ms=%d sidecar_queued_ms=%s "
                 "sidecar_exchange_ms=%s status=%s",
                 client_order_id, round((sent - started) * 1000) if started is not None else "-",
                 round((done - sent) * 1000), timing.get("queuedMs", "-"), timing.get("exchangeMs", "-"),
                 order.get("status") or report.get("commandStatus") or report.get("errorCode") or "-")
    except Exception:  # Timing must never affect an order.
        pass
