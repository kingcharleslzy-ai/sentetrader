from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from .broker import EventBroker
from .card_protection import (
    RemainingProtectionInvalid,
    bucket_protection_levels,
    bucket_stop_motion,
    common_protection_price,
    cost_on_tick,
    held_protection_buckets,
)
from .conversation_cards import (
    ask_action,
    execution_finished,
    order_figure,
    price_text,
    reminder_card,
    track_reminder_order,
    ui_action,
)
from .data_providers import provider_read_snapshot
from .exchange_calendar import calendar_trading_days
from .order_boundaries import SimulationOrderError, price_on_tick, quote_is_fresh
from .reminders import ReminderSpec, deliver_via_store, order_state
from .simulation_orders import (
    CLOSE_TODAY_EXCHANGES,
    PARTIAL_ENDED,
    SimulationOrderService,
    order_needs_reconciliation,
)
from .store import RuntimeStore, utc_now
from .strategy_protection import (
    account_protection_day,
    breakeven_at_r,
    fill_protection_bucket,
    require_current_activity,
    sync_protection,
)
from .trading_calendar import trading_day_at
from .trading_clock import fill_trading_day
from .web_push import PushMessage, deep_link, emergency_message

if TYPE_CHECKING:  # strategy_monitor imports autonomy; the back-reference avoids a cycle
    from .strategy_monitor import StrategyMonitorService

log = logging.getLogger("uvicorn.error.autonomy")

AUTONOMY_PERMISSIONS = frozenset({"open", "cancel", "close", "stop"})
UNLIMITED_SIMULATION_LIMIT = 2_147_483_647
UNLIMITED_SIMULATION_EXPIRES_AT = "9999-12-31T23:59:59+00:00"
# 保护扫描对同一笔平仓单的重试节奏：在途单的对账，以及等结算价拒收后的重派 (#750)。
STOP_RETRY_SECONDS = 2.0


def undispatched_reason(order: dict[str, Any]) -> str:
    """保护行上“平仓单没派发出去”的原因：引擎在等交易所结算价时写具体原因 (#750)，
    持仓面板与 AI 据此说“在等结算价”；其余确定未派发统一为 order_not_dispatched。"""
    if order.get("lastReadError") == "paper_settlement_pending":
        return "paper_settlement_pending"
    return "order_not_dispatched"


def _retry_due(updated_at: Any) -> bool:
    try:
        moment = datetime.fromisoformat(str(updated_at))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return (datetime.now(UTC) - moment).total_seconds() >= STOP_RETRY_SECONDS
    except (TypeError, ValueError):
        return True


class AutonomyError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        # str() stays the code (logs, tests and every existing code-path read it); the
        # optional Chinese sentence rides on .message for the surfaces a user reads.
        super().__init__(code)
        self.code = code
        self.message = message


def direct_protection_bucket(row: Any, *, today: str,
                             trading_days: Sequence[str] | Callable[[], Sequence[str]] | None = None) -> str:
    """Existing direct-stop overnight rule, shared by read-only previews and the write Owner.

    `today` is the account's own trading day (its summary's ``tradingDay``, read beside the positions
    it is judged against — `_direct_stop_day`), and the row's day is the account day it was armed on
    (``account_trading_day``). The today/historical split of a position moves when the account rolls,
    not at the calendar's 18:00 turn: the 本站模拟 book keeps yesterday's split until the exchange's
    settlement price arrives, so a calendar-based roll would look for a historical position that does
    not exist yet and cancel a stop on a still-held position (#790 review). Rows armed before the
    column existed fall back to the calendar day of their creation time; ``trading_days`` may be a
    reader, called only for such a row (a stamped row needs no calendar read)."""
    bucket = str(row["position_bucket"] or "all")
    if (bucket == "today" and str(row["symbol"]).split(".", 1)[0] in CLOSE_TODAY_EXCHANGES
            and _armed_day(row, trading_days) < today):
        return "historical"
    return bucket


def _armed_day(row: Any, trading_days: Sequence[str] | Callable[[], Sequence[str]] | None) -> str:
    try:
        day = row["account_trading_day"]
    except (KeyError, IndexError):
        day = None
    if day:
        return str(day)
    return trading_day_at(str(row["created_at"]), trading_days() if callable(trading_days) else trading_days)


def protection_price_text(value: Decimal) -> str:
    """A protection price as humans write it: no trailing .0, thousands separated."""
    text = format(value.normalize(), "f")
    if "." in text:
        head, tail = text.split(".")
        return f"{int(head):,}.{tail}"
    return f"{int(text):,}"


def manual_trailing(provider: Any, symbol: str, trailing: dict[str, Any] | None, *, long: bool) -> dict[str, Any]:
    """手动止损的追踪方式（2026-10-25 第 4 批）as the armed stop carries it — the strategy's own `trailing` config
    (tightened by `_trail_stop` with the strategy's trail_stop), plus for the ATR chandelier the ATR of the chosen
    period's last completed bars, frozen now like a strategy's signal-bar ATR. Empty without a trailing choice."""
    if not trailing:
        return {}
    config = {key: trailing[key] for key in ("mode", "value", "activateAfter", "atrPeriod") if key in trailing}
    if config["mode"] == "bar_extreme":
        config["atrTimeframe"] = int(trailing.get("atrTimeframe") or 300)
        level = bar_extreme(provider, symbol, config["atrTimeframe"], int(config["value"]), long=long)
        if level is None:
            raise AutonomyError("trailing_bars_unavailable", "这个周期收完的 K 线还不够这么多根，换个周期或少几根")
        # #1207: the price already broke the last N bars' low (long) / high (short) — the first tightening would
        # set the stop past the market and close the position at once.
        latest = Decimal(str(provider.market_snapshot("", symbol).get("price")))
        if (level >= latest) if long else (level <= latest):
            raise AutonomyError("trailing_bars_breached",
                                f"现价已{'跌破' if long else '涨过'}前 {int(config['value'])} 根 K 线的"
                                f"{'低点' if long else '高点'} {protection_price_text(level)}，这样挂会马上止损；多取几根或换个周期")
        return {"trailing": config}
    if config["mode"] != "atr_chandelier":
        return {"trailing": config}
    from .strategy_rules import Operand, indicator_values
    timeframe = int(trailing.get("atrTimeframe") or 300)
    try:
        bars = (provider.market_workbench(symbol, timeframe).get("bars") or [])[:-1]
        values = indicator_values(bars, Operand(indicator="atr", period=int(config.get("atrPeriod") or 14), timeframe=timeframe))
        atr = Decimal(str(values[-1])) if values and values[-1] is not None else None
    except (KeyError, ValueError, RuntimeError, InvalidOperation, IndexError):
        atr = None
    if atr is None or not atr.is_finite() or atr <= 0:
        raise AutonomyError("trailing_atr_unavailable", "这个周期的 K 线还不够算 ATR，换按跳数或百分比追踪")
    return {"trailing": config, "entryAtr": str(atr)}


def bar_extreme(provider: Any, symbol: str, timeframe: int, count: int, *, long: bool) -> Decimal | None:
    """前 N 根 K 线追踪 (2026-10-25): the lowest low (long) / highest high (short) of the last `count` completed bars
    of `timeframe` seconds; None while fewer have completed or the series cannot be read."""
    try:
        bars = (provider.market_workbench(symbol, timeframe).get("bars") or [])[:-1][-count:]
        levels = [Decimal(str(bar["low" if long else "high"])) for bar in bars]
    except (KeyError, ValueError, RuntimeError, InvalidOperation, TypeError):
        return None
    if len(levels) < count or not all(level.is_finite() and level > 0 for level in levels):
        return None
    return min(levels) if long else max(levels)


# -- 直接保护的持仓锚点 (#528)---------------------------------------------------------------------
# A direct stop (持仓面板 / 工具直接挂，无 strategy_metadata) guards the position it was armed on:
# anchor_volume = what that bucket held then; closed_since = the close fills since (every close
# counts against the original first — 所有者决定 2026-10-06, the exchange only reports totals);
# fill_watermark_json = {"day": newest trading day seen, "ids": that day's "clientOrderId:tradeId"}.
# Opens never add to a stop. A fill of an older trading day than the watermark that it has not
# listed is treated as already seen: uncertainty never cancels a protection.
# A direct stop this snapshot could not check against its anchor carries
# last_error = DIRECT_STOP_BLOCKED + reason (activity_unavailable / activity_stale /
# positions_unavailable / fill_untrusted / convergence_failed) and does not fire this round.
DIRECT_STOP_BLOCKED = "direct_stop_blocked:"
_TRADE_OFFSETS = frozenset({"OPEN", "CLOSE", "CLOSETODAY", "CLOSEYESTERDAY"})
_BUCKET_CLOSE_OFFSETS = {
    "today": frozenset({"CLOSETODAY"}),
    "historical": frozenset({"CLOSE", "CLOSEYESTERDAY"}),
    "all": frozenset({"CLOSE", "CLOSETODAY", "CLOSEYESTERDAY"}),
}


def _bucket_held(positions: list[Any], symbol: str, direction: str, bucket: str) -> int:
    """该方向该桶实际持有的手数 —— 不扣冻结（用户自己挂的限价平仓单冻结了仓位，仓位仍在）。"""
    held = 0
    for position in positions:
        if (str(position.get("symbol", "")).casefold() != symbol.casefold()
                or str(position.get("direction", "")) != direction):
            continue
        key = {"all": "volume", "today": "todayVolume", "historical": "historicalVolume"}[bucket]
        held += max(0, int(position.get(key) or 0))
    return held


def _symbol_fills(activity: dict[str, Any], symbol: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(event id, trading day, trade) of one contract's fills; ValueError when any is untrusted."""
    fills = []
    for trade in activity["trades"]:
        if not isinstance(trade, dict) or str(trade.get("symbol", "")).casefold() != symbol.casefold():
            continue
        trade_id, order_id, volume = trade.get("tradeId"), trade.get("clientOrderId"), trade.get("volume")
        if (not isinstance(trade_id, str) or not trade_id or not isinstance(order_id, str)
                or isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0
                or trade.get("direction") not in {"BUY", "SELL"} or trade.get("offset") not in _TRADE_OFFSETS):
            raise ValueError("direct_stop_fill_untrusted")
        fills.append((f"{order_id}:{trade_id}", fill_trading_day(trade), trade))
    return fills


def _fill_watermark(fills: list[tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
    day = max((trade_day for _, trade_day, _ in fills), default=None)
    return {"day": day, "ids": sorted(event for event, trade_day, _ in fills if trade_day == day)}


def _closes_since(fills: list[tuple[str, str, dict[str, Any]]], watermark: dict[str, Any], *,
                  direction: str, bucket: str) -> tuple[int, dict[str, Any]]:
    """Close volume of this direction and bucket past the watermark, and the advanced watermark."""
    day = watermark.get("day")
    seen = set(watermark.get("ids") or [])
    closing = "SELL" if direction == "long" else "BUY"
    closed = 0
    for event, trade_day, trade in fills:
        if day is not None and (trade_day < day or (trade_day == day and event in seen)):
            continue
        if trade["direction"] == closing and trade["offset"] in _BUCKET_CLOSE_OFFSETS[bucket]:
            closed += int(trade["volume"])
    newest = max([trade_day for _, trade_day, _ in fills] + ([day] if day else []), default=None)
    ids = (seen if newest == day else set()) | {event for event, trade_day, _ in fills if trade_day == newest}
    return closed, {"day": newest, "ids": sorted(ids)}


StopAction = Literal["open", "close", "stop", "exit"]

# Leases carry the four execution permissions; stop fencing only distinguishes
# open / close / stop. A stray permission keeps the pre-existing close semantics.
PERMISSION_STOP_ACTIONS: dict[str, StopAction] = {
    "open": "open", "close": "close", "stop": "stop",
}


def stop_blocks(stop_status: str | None, action: StopAction) -> str | None:
    """The error code a stop status imposes on an action, or None when allowed.

    EMERGENCY_STOP blocks everything; STOPPED blocks every AI-initiated action
    except protective exits; HALT_OPENS blocks only new opens."""
    if stop_status == "EMERGENCY_STOP":
        return "ai_execution_stopped"
    if stop_status == "STOPPED":
        return None if action == "exit" else "ai_execution_stopped"
    if stop_status == "HALT_OPENS":
        return "ai_new_opens_halted" if action == "open" else None
    return None


def _side_stop_action(side: str) -> StopAction:
    return "open" if side.endswith("_open") else "close"


def _iso_after(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _json_list(value: str) -> list[str]:
    parsed = json.loads(value)
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


class AutonomousSimulationService:
    """Runtime-managed access to the one-shot TqKq simulation gateway."""

    # Cancel notices collected during one `_clamp_direct_stops` scan, merged per position on flush.
    _cancel_queue: list[tuple[sqlite3.Row, int]] | None = None

    def __init__(
        self,
        *,
        store: RuntimeStore,
        broker: EventBroker,
        simulation_orders: SimulationOrderService,
        data_provider: Any,
        poll_interval_seconds: float = 0.5,
        allow_cloud_owner: bool = False,
        trading_account_id: str | None = None,
    ) -> None:
        self.allow_cloud_owner = allow_cloud_owner
        self.store = store
        self.account_id = trading_account_id or store.default_trading_account_id
        self.broker = broker
        self.simulation_orders = simulation_orders
        self.data_provider = data_provider
        self.poll_interval_seconds = max(0.1, poll_interval_seconds)
        # 本账户的策略监控 (2026-10-10 #692)：attach_account_loops 装配后回填，HTTP 与内置工具
        # 的启动/状态共用它的人口；只读装配（未跑监控循环）保持 None。
        self.strategy_monitor: StrategyMonitorService | None = None
        # A live voice call for trigger notices (wired in main; None = nobody to speak to).
        self.voice_lookup: Callable[[], Any] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._action_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._bar_levels: dict[tuple[str, int, int, bool], tuple[float, Decimal | None]] = {}
        self._stopping = False

    def recover_interrupted_execution(self) -> None:
        """Restart recovery for this account only. Called by the side that runs
        this account's loops (`recover_interrupted_execution` in wiring), never
        at construction: assembling another account, or a standby slot building
        this one read-only, must not expire a running holder's leases."""
        now = utc_now()
        with self.store.lock, self.store.connection:
            # A restart invalidates the old session lease. The next simulation
            # action may establish a fresh runtime-managed lease automatically;
            # no user confirmation is required for simulation.
            self.store.connection.execute(
                """UPDATE ai_execution_authorizations SET status = 'EXPIRED', updated_at = ?
                WHERE status = 'ACTIVE' AND trading_account_id = ?""",
                (now, self.account_id),
            )
            # A process may have stopped after reserving the one-shot close but before
            # observing the result. Never create a second close order on recovery.
            self.store.connection.execute(
                """UPDATE ai_stop_orders SET status = 'UNKNOWN',
                last_error = COALESCE(last_error, 'runtime_restarted_during_trigger'),
                updated_at = ? WHERE status = 'TRIGGERING' AND close_client_order_id IS NOT NULL
                AND trading_account_id = ?""",
                (now, self.account_id),
            )
            # 528-C: reserved the trigger but the close identity was never written — the close
            # was never sent (an orphaned PREPARED order is never auto-dispatched), so the stop
            # re-arms instead of living as an unqueryable, uncancelable UNKNOWN forever.
            self.store.connection.execute(
                """UPDATE ai_stop_orders SET status = 'ARMED',
                last_error = COALESCE(last_error, 'runtime_restarted_before_dispatch'),
                updated_at = ? WHERE status = 'TRIGGERING' AND close_client_order_id IS NULL
                AND trading_account_id = ?""",
                (now, self.account_id),
            )

    @property
    def enabled(self) -> bool:
        return self.simulation_orders.enabled

    def supports_mode(self, mode: str) -> bool:
        # A personal owner Runtime has the same bounded simulation gateway as
        # the local app. This does not authorize multi-tenant cloud execution.
        return mode == "local" or (mode == "cloud" and self.allow_cloud_owner)

    async def start(self) -> None:
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._stopping = False
        self._monitor_task = asyncio.create_task(
            self._monitor_stops(), name="ai-trader-stop-monitor"
        )

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _authorization(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "authorizationId": row["id"],
            "userId": row["user_id"],
            "sessionId": row["session_id"],
            "status": row["status"],
            "allowedSymbols": _json_list(row["symbols_json"]),
            "permissions": _json_list(row["permissions_json"]),
            "maxOrderVolume": int(row["max_order_volume"]),
            "maxOrders": int(row["max_orders"]),
            "usedOrders": int(row["used_orders"]),
            "remainingOrders": max(0, int(row["max_orders"]) - int(row["used_orders"])),
            "expiresAt": row["expires_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            # 'ai_autonomy_off': stopped by switching AI 自主执行 off, not by the user's stop.
            "stopReason": row["stop_reason"],
            "environment": "本站模拟" if self.data_provider.mode == "paper" else "TqKq simulation",
            "liveTrading": "disabled",
        }

    def current(self, *, user_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                """UPDATE ai_execution_authorizations SET status = 'EXPIRED', updated_at = ?
                WHERE user_id = ? AND status = 'ACTIVE' AND expires_at <= ?
                AND trading_account_id = ?""",
                (now, user_id, now, self.account_id),
            )
            # Wall-clock timestamps can tie (notably on Windows); insertion order
            # determines which explicit activation/stop is authoritative.
            row = self.store.connection.execute(
                """SELECT * FROM ai_execution_authorizations WHERE user_id = ?
                AND trading_account_id = ?
                ORDER BY rowid DESC LIMIT 1""",
                (user_id, self.account_id),
            ).fetchone()
        return self._authorization(row) if row is not None else None

    def _lift_account_stop(self, connection: sqlite3.Connection, now: str) -> None:
        connection.execute("""UPDATE ai_execution_controls SET stop_status = NULL,
            generation = generation + 1, updated_at = ?
            WHERE trading_account_id = ? AND stop_status IS NOT NULL""", (now, self.account_id))
        # Resume grants no other user's lease: their next permitted action must establish it again.
        connection.execute("""UPDATE ai_execution_authorizations SET status = 'EXPIRED', updated_at = ?
            WHERE trading_account_id = ? AND status IN ('STOPPED', 'EMERGENCY_STOP')
            AND stop_reason IS NULL""", (now, self.account_id))

    def lift_stop(self, *, user_id: str) -> None:
        """Only the execution owner explicitly lifts an account stop, without creating a lease."""
        self._require_execution_owner(user_id)
        with self.store.transaction() as connection:
            self._lift_account_stop(connection, utc_now())
        self._wake.set()

    def controls(self, *, user_id: str) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT * FROM ai_execution_controls WHERE user_id = ? AND trading_account_id = ?",
            (user_id, self.account_id),
        ).fetchone()
        latest = self.store.connection.execute("""SELECT * FROM ai_execution_authorizations
            WHERE user_id = ? AND trading_account_id = ? ORDER BY rowid DESC LIMIT 1""",
            (user_id, self.account_id)).fetchone()
        current = self._authorization(latest) if latest else None
        # Stop belongs to the account; limits and leases still belong to their user.
        # Keeping every actor's row also preserves the stop across grant changes and restart.
        fences = self.store.connection.execute(
            "SELECT stop_status, generation FROM ai_execution_controls WHERE trading_account_id = ?",
            (self.account_id,),
        ).fetchall()
        strength = {None: 0, "HALT_OPENS": 1, "STOPPED": 2, "EMERGENCY_STOP": 3}
        stopped = max((item["stop_status"] for item in fences), key=lambda value: strength[value], default=None)
        return {
            # The switch-off lease (stopReason ai_autonomy_off) is not the user's stop: only an
            # explicit stop / emergency stop (the controls row, or a legacy stopped lease) is.
            "stopStatus": stopped or ((current or {}).get("status") if (current or {}).get("status") in {"STOPPED", "EMERGENCY_STOP"} and not self._autonomy_off(current) else None),
            "generation": sum(item["generation"] for item in fences),
            "revision": row["limits_revision"] if row else 0,
            "maxOrderVolume": row["max_order_volume"] if row and row["limits_revision"] else (current or {}).get("maxOrderVolume"),
            "maxOrders": row["max_orders"] if row and row["limits_revision"] else (current or {}).get("maxOrders"),
        }

    def update_limits(self, *, user_id: str, revision: int,
                      max_order_volume: int | None, max_orders: int | None) -> dict[str, Any]:
        self._require_account_write(user_id)
        if any(value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                     or value < 1 or value > UNLIMITED_SIMULATION_LIMIT)
               for value in (max_order_volume, max_orders)):
            raise AutonomyError("invalid_simulation_limits")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ai_execution_controls (user_id, trading_account_id, updated_at) VALUES (?, ?, ?)",
                (user_id, self.account_id, utc_now()))
            changed = connection.execute("""UPDATE ai_execution_controls
                SET max_order_volume = ?, max_orders = ?, limits_revision = limits_revision + 1, updated_at = ?
                WHERE user_id = ? AND trading_account_id = ? AND limits_revision = ?""",
                (max_order_volume, max_orders, utc_now(), user_id, self.account_id, revision))
            if changed.rowcount != 1:
                raise AutonomyError("simulation_limits_conflict")
            connection.execute("""UPDATE ai_execution_authorizations
                SET max_order_volume = ?, max_orders = ?, updated_at = ?
                WHERE id = (SELECT id FROM ai_execution_authorizations
                            WHERE user_id = ? AND trading_account_id = ?
                            ORDER BY rowid DESC LIMIT 1)""",
                (max_order_volume or UNLIMITED_SIMULATION_LIMIT, max_orders or UNLIMITED_SIMULATION_LIMIT,
                 utc_now(), user_id, self.account_id))
        return self.controls(user_id=user_id)

    def _require_not_stopped(self, user_id: str, generation: int | None = None, *, action: StopAction) -> None:
        control = self.controls(user_id=user_id)
        code = stop_blocks(control["stopStatus"], action)
        if code is None and (generation is None or generation == control["generation"]):
            return
        raise AutonomyError(code or "ai_execution_stopped")

    def autonomy_enabled(self, user_id: str) -> bool:
        """The account's 设置 → 交易 → AI 自主执行 switch (trade.aiAutonomy, default on)."""
        try:
            return bool(self.store.user_preferences(user_id).trade.aiAutonomy)
        except Exception:  # an unreadable preference document must not silently disable execution
            log.exception("ai_autonomy_preference_unreadable user=%s", user_id)
            return True

    def _require_autonomy_enabled(self, user_id: str) -> None:
        """Every path where the AI sends an order by itself: its execution tool, the lease it needs and
        strategy monitoring. With the switch off only proposal cards remain, which the user confirms."""
        if not self.autonomy_enabled(user_id):
            raise AutonomyError("ai_autonomy_disabled")

    def _require_execution_owner(self, user_id: str) -> None:
        """4.3: one account, one execution owner. Every path where the AI acts on the
        account's behalf — its execution tool, its leases, its protective stops,
        strategy monitoring — belongs to that owner; any other user's AI may only
        produce proposal cards the owner confirms."""
        if not self.store.is_execution_owner(account_id=self.account_id, user_id=user_id):
            raise AutonomyError("not_execution_owner")

    def _require_account_write(self, user_id: str, *, account_id: str | None = None) -> None:
        """4.3: a viewer holds a read-only grant; the paths a user drives by hand
        (confirming cards, cancelling, resuming, limits) stop here, before the
        account is touched."""
        role = self.store.account_role(account_id=account_id or self.account_id, user_id=user_id)
        if role == "viewer":
            raise AutonomyError("account_read_only")
        if role is None:
            raise AutonomyError("account_not_granted")

    @staticmethod
    def _autonomy_off(authorization: dict[str, Any] | None) -> bool:
        """A lease stopped only because AI 自主执行 was switched off (not a user stop)."""
        return bool(authorization and authorization["status"] == "STOPPED"
                    and authorization.get("stopReason") == "ai_autonomy_off")

    def apply_autonomy_preference(self, user_id: str, *, enabled: bool) -> int:
        """Called after 设置 → 交易 → AI 自主执行 changes. Turning it off ends the running lease at once
        instead of leaving it ACTIVE for `get_ai_execution_status` to report. Turning it on creates
        nothing: the next AI action establishes its own lease. Returns how many leases were stopped."""
        if enabled:
            return 0
        now = utc_now()
        with self.store.lock, self.store.connection:
            cursor = self.store.connection.execute(
                """UPDATE ai_execution_authorizations SET status = 'STOPPED', stop_reason = 'ai_autonomy_off',
                updated_at = ? WHERE user_id = ? AND status = 'ACTIVE'""",
                (now, user_id),
            )
        stopped = int(cursor.rowcount or 0)
        # The audit trail for this: proposal_audit rows are foreign-keyed to a proposal, so the record
        # is the lease's own STOPPED row plus this line in the Runtime log.
        log.info("ai_autonomy_disabled user=%s stopped_leases=%d at=%s", user_id, stopped, now)
        self._wake.set()
        return stopped

    def activate(
        self,
        *,
        user_id: str,
        session_id: str,
        symbols: list[str],
        permissions: list[str],
        max_order_volume: int | None,
        max_orders: int | None,
        duration_minutes: int | None,
        automatic: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise AutonomyError("simulation_gateway_disabled")
        if not automatic:
            # Only an explicit activation (the start route, the AI's own resume)
            # is the execution owner's to make; automatic leases the user's own
            # confirmed actions create keep flowing for operators too.
            self._require_execution_owner(user_id)
        try:
            session = self.store.get_session(session_id, user_id=user_id)
        except KeyError as error:
            raise AutonomyError("session_not_found") from error
        if not self.supports_mode(session["mode"]):
            raise AutonomyError("local_simulation_only")
        requested_symbols = list(dict.fromkeys(
            str(item).strip() for item in symbols if str(item).strip()
        ))
        normalized_permissions = sorted({str(item).strip().lower() for item in permissions})
        if not requested_symbols or len(requested_symbols) > 20:
            raise AutonomyError("invalid_allowed_symbols")
        if any("." not in symbol or len(symbol) > 80 for symbol in requested_symbols):
            raise AutonomyError("actual_delivery_symbols_required")
        if not normalized_permissions or not set(normalized_permissions) <= AUTONOMY_PERMISSIONS:
            raise AutonomyError("invalid_permissions")
        if max_order_volume is not None and max_order_volume < 1:
            raise AutonomyError("invalid_max_order_volume")
        if max_orders is not None and max_orders < 1:
            raise AutonomyError("invalid_max_orders")
        if duration_minutes is not None and duration_minutes < 5:
            raise AutonomyError("invalid_duration")
        stored_max_order_volume = max_order_volume or UNLIMITED_SIMULATION_LIMIT
        stored_max_orders = max_orders or UNLIMITED_SIMULATION_LIMIT
        expires_at = (
            _iso_after(duration_minutes)
            if duration_minutes is not None
            else UNLIMITED_SIMULATION_EXPIRES_AT
        )
        normalized_symbols: list[str] = []
        for symbol in requested_symbols:
            try:
                market = self.data_provider.market_snapshot("", symbol)
            except (KeyError, RuntimeError) as error:
                raise AutonomyError("symbol_not_available") from error
            canonical_symbol = str(market.get("symbol", "")).strip()
            if canonical_symbol.casefold() != symbol.casefold():
                raise AutonomyError("symbol_identity_mismatch")
            policy = self.simulation_orders.policies.get(
                user_id=user_id, symbol=canonical_symbol
            )
            policy_symbol = canonical_symbol.split(".", 1)[-1].upper()
            if policy.allowed_symbols is not None and policy_symbol not in policy.allowed_symbols:
                raise AutonomyError("symbol_outside_risk_policy")
            if canonical_symbol not in normalized_symbols:
                normalized_symbols.append(canonical_symbol)
        authorization_id, now = str(uuid4()), utc_now()
        with self.store.transaction() as connection:
            control = connection.execute(
                "SELECT * FROM ai_execution_controls WHERE user_id = ? AND trading_account_id = ?",
                (user_id, self.account_id),
            ).fetchone()
            # HALT_OPENS still lets the AI act on anything but new opens, so an
            # automatic lease for a close or a protective stop stays available.
            if automatic and self.controls(user_id=user_id)["stopStatus"] in {"STOPPED", "EMERGENCY_STOP"}:
                raise AutonomyError("ai_execution_stopped")
            if automatic and control is not None and control["limits_revision"]:
                stored_max_order_volume = control["max_order_volume"] or UNLIMITED_SIMULATION_LIMIT
                stored_max_orders = control["max_orders"] or UNLIMITED_SIMULATION_LIMIT
            previous = connection.execute("""SELECT used_orders FROM ai_execution_authorizations
                WHERE user_id = ? AND trading_account_id = ? ORDER BY rowid DESC LIMIT 1""",
                (user_id, self.account_id)).fetchone()
            used_orders = previous["used_orders"] if previous else 0
            if not automatic:
                self._lift_account_stop(connection, now)
                connection.execute("""INSERT INTO ai_execution_controls
                    (user_id, max_order_volume, max_orders, limits_revision, generation, trading_account_id, updated_at)
                    VALUES (?, ?, ?, 1, 1, ?, ?)
                    ON CONFLICT(user_id, trading_account_id) DO UPDATE SET stop_status = NULL, generation = generation + 1,
                    max_order_volume = excluded.max_order_volume, max_orders = excluded.max_orders,
                    limits_revision = limits_revision + 1, updated_at = excluded.updated_at""",
                    (user_id, max_order_volume, max_orders, self.account_id, now))
            connection.execute(
                """UPDATE ai_execution_authorizations SET status = 'STOPPED', updated_at = ?
                WHERE user_id = ? AND status = 'ACTIVE' AND trading_account_id = ?""",
                (now, user_id, self.account_id),
            )
            connection.execute(
                """INSERT INTO ai_execution_authorizations
                (id, user_id, session_id, status, symbols_json, permissions_json,
                 max_order_volume, max_orders, used_orders, expires_at, created_at, updated_at,
                 trading_account_id)
                VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    authorization_id,
                    user_id,
                    session_id,
                    json.dumps(normalized_symbols, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(normalized_permissions, ensure_ascii=False, separators=(",", ":")),
                    stored_max_order_volume,
                    stored_max_orders,
                    used_orders,
                    expires_at,
                    now,
                    now,
                    self.account_id,
                ),
            )
        self._wake.set()
        result = self.current(user_id=user_id)
        if result is None:
            raise AutonomyError("authorization_not_created")
        return result

    def ensure_runtime_access(
        self, *, user_id: str, session_id: str, symbol: str,
        permission: str, volume: int | None = None, ai_initiated: bool = False,
    ) -> dict[str, Any]:
        """Create the default unlimited simulation lease without asking the user.

        `ai_initiated` marks the calls the AI makes on its own behalf (its execution tool, its stop
        tool, strategy monitoring): those are refused when the account turned AI 自主执行 off, and
        belong to the account's execution owner alone. What the user drives — a confirmed proposal
        card, the end of a replay, the client's own requests — keeps the default and is never gated."""
        if ai_initiated:
            self._require_execution_owner(user_id)
            self._require_autonomy_enabled(user_id)
        self._require_not_stopped(
            user_id, action=PERMISSION_STOP_ACTIONS.get(permission, "close")
        )
        current = self.current(user_id=user_id)
        if self._autonomy_off(current):
            # Stopped by the switch, not by the user: the next action that may run (the user's own,
            # or the AI's once the switch is back on) starts a fresh lease.
            current = None
        if current is not None and current["status"] in {"STOPPED", "EMERGENCY_STOP"}:
            raise AutonomyError("ai_execution_stopped")
        if current is not None and current["status"] == "EXPIRED":
            if current["expiresAt"] <= utc_now():
                if ai_initiated:
                    raise AutonomyError("ai_execution_expired")
                # The user's own action (a stop on their position, #1206) is never gated by a timed AI lease
                # that ran out: it starts the default unlimited lease like a cold account.
                current = None
        if current is not None and current["status"] == "EXPIRED":
            self.store.get_session(session_id, user_id=user_id)
            with self.store.lock, self.store.connection:
                self.store.connection.execute("UPDATE ai_execution_authorizations SET status = 'ACTIVE', updated_at = ? WHERE id = ? AND status = 'EXPIRED'", (utc_now(), current["authorizationId"]))
            current = self.current(user_id=user_id)
        if current is not None and current["status"] == "ACTIVE":
            same_session = current["sessionId"] == session_id
            symbol_allowed = any(
                allowed.casefold() == symbol.casefold()
                for allowed in current["allowedSymbols"]
            )
            permission_allowed = permission in current["permissions"]
            volume_allowed = (
                volume is None
                or int(current["maxOrderVolume"]) == UNLIMITED_SIMULATION_LIMIT
                or volume <= int(current["maxOrderVolume"])
            )
            if same_session and symbol_allowed and permission_allowed and volume_allowed:
                return current
            persistent_settings = current["expiresAt"] == UNLIMITED_SIMULATION_EXPIRES_AT
            if not persistent_settings or not permission_allowed or not volume_allowed:
                raise AutonomyError("configured_simulation_limit_reached")
            symbols = list(dict.fromkeys([*current["allowedSymbols"], symbol]))
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    """UPDATE ai_execution_authorizations
                    SET session_id = ?, symbols_json = ?, updated_at = ? WHERE id = ?""",
                    (
                        session_id,
                        json.dumps(symbols, ensure_ascii=False, separators=(",", ":")),
                        utc_now(),
                        current["authorizationId"],
                    ),
                )
            refreshed = self.current(user_id=user_id)
            if refreshed is None:
                raise AutonomyError("simulation_lease_not_updated")
            return refreshed
        else:
            symbols = [symbol]
        configured_volume = None
        configured_orders = None
        if current is not None and current["expiresAt"] == UNLIMITED_SIMULATION_EXPIRES_AT:
            if int(current["maxOrderVolume"]) != UNLIMITED_SIMULATION_LIMIT:
                configured_volume = int(current["maxOrderVolume"])
            if int(current["maxOrders"]) != UNLIMITED_SIMULATION_LIMIT:
                configured_orders = int(current["maxOrders"])
        return self.activate(
            user_id=user_id,
            session_id=session_id,
            symbols=symbols,
            permissions=sorted(AUTONOMY_PERMISSIONS),
            max_order_volume=configured_volume,
            max_orders=configured_orders,
            duration_minutes=None,
            automatic=True,
        )

    def resume_runtime_access(
        self, *, user_id: str, session_id: str, symbol: str, ai_initiated: bool = False,
    ) -> dict[str, Any]:
        """Explicitly resume simulation execution after a normal or emergency stop."""
        if ai_initiated:
            self._require_execution_owner(user_id)
            self._require_autonomy_enabled(user_id)
        current = self.current(user_id=user_id)
        if not self.controls(user_id=user_id)["stopStatus"] and (current is None or current["status"] not in {"STOPPED", "EMERGENCY_STOP"}):
            return self.ensure_runtime_access(
                user_id=user_id,
                session_id=session_id,
                symbol=symbol,
                permission="open",
                ai_initiated=ai_initiated,
            )
        if current is None:
            control = self.controls(user_id=user_id)
            return self.activate(user_id=user_id, session_id=session_id, symbols=[symbol],
                                 permissions=sorted(AUTONOMY_PERMISSIONS), max_order_volume=control["maxOrderVolume"],
                                 max_orders=control["maxOrders"], duration_minutes=None)
        symbols = list(dict.fromkeys([*current["allowedSymbols"], symbol]))
        configured_volume = (
            None
            if int(current["maxOrderVolume"]) == UNLIMITED_SIMULATION_LIMIT
            else int(current["maxOrderVolume"])
        )
        configured_orders = (
            None
            if int(current["maxOrders"]) == UNLIMITED_SIMULATION_LIMIT
            else int(current["maxOrders"])
        )
        return self.activate(
            user_id=user_id,
            session_id=session_id,
            symbols=symbols,
            permissions=sorted(AUTONOMY_PERMISSIONS),
            max_order_volume=configured_volume,
            max_orders=configured_orders,
            duration_minutes=None,
        )

    async def deactivate(
        self, *, user_id: str, emergency: bool = False,
        scope: Literal["all", "opens"] = "all",
        parent_account_id: str | None = None,
    ) -> dict[str, Any]:
        if emergency and scope != "all":
            raise AutonomyError("invalid_stop_scope")
        if parent_account_id is not None:
            # A CNY paper stop includes its own USD ledger, even when the actor only has
            # the CNY grant. This narrow composite operation never grants USD writes/resume.
            parent = self.store.get_trading_account(parent_account_id)
            child = self.store.get_trading_account(self.account_id)
            if (not parent_account_id.startswith("paper:")
                    or self.account_id != f"paperusd:{parent_account_id.removeprefix('paper:')}"
                    or parent is None or child is None
                    or parent["execution_owner_user_id"] != child["execution_owner_user_id"]):
                raise AutonomyError("account_not_granted")
        self._require_account_write(user_id, account_id=parent_account_id)
        status = "EMERGENCY_STOP" if emergency else ("STOPPED" if scope == "all" else "HALT_OPENS")
        now = utc_now()
        if scope == "opens":
            # 只停新开仓: refuse new opens only. The lease keeps running, live
            # orders stay untouched and armed protective stops remain armed, so
            # closes and protective exits continue until the user resumes.
            # The fence only strengthens: after a stop or an emergency stop only
            # an explicit resume may lower it, so this scope must never overwrite
            # a stronger fence. Re-clicking it on HALT_OPENS stays idempotent.
            with self.store.transaction() as connection:
                if self.controls(user_id=user_id)["stopStatus"] in {"STOPPED", "EMERGENCY_STOP"}:
                    raise AutonomyError("ai_execution_stopped")
                connection.execute("""INSERT INTO ai_execution_controls (user_id, stop_status, generation, trading_account_id, updated_at)
                    VALUES (?, ?, 1, ?, ?) ON CONFLICT(user_id, trading_account_id) DO UPDATE SET stop_status = excluded.stop_status,
                    generation = generation + 1, updated_at = excluded.updated_at
                    WHERE ai_execution_controls.stop_status IS NULL
                    OR ai_execution_controls.stop_status = 'HALT_OPENS'""",
                    (user_id, status, self.account_id, now))
            self._wake.set()
            return {
                "authorization": self.current(user_id=user_id),
                "emergency": False,
                "scope": "opens",
                "cancelledOrders": [],
                "liveTrading": "disabled",
            }
        if not emergency and self.controls(user_id=user_id)["stopStatus"] == "EMERGENCY_STOP":
            # A plain stop never downgrades an emergency stop: the fence, the
            # stopped lease and the voided protective stops stay as they are
            # until the user resumes, so re-clicking stop stays harmless.
            return {
                "authorization": self.current(user_id=user_id),
                "emergency": False,
                "scope": "all",
                "cancelledOrders": [],
                "liveTrading": "disabled",
            }
        # Persist the execution fence before any network cancellation. This path
        # intentionally does not wait for a submission that already owns _action_lock.
        with self.store.transaction() as connection:
            # emergency=True only strengthens; a plain stop never overwrites an
            # emergency fence, so the conflict update refuses that downgrade.
            connection.execute("""INSERT INTO ai_execution_controls (user_id, stop_status, generation, trading_account_id, updated_at)
                VALUES (?, ?, 1, ?, ?) ON CONFLICT(user_id, trading_account_id) DO UPDATE SET stop_status = excluded.stop_status,
                generation = generation + 1, updated_at = excluded.updated_at
                WHERE ai_execution_controls.stop_status IS NULL
                OR ai_execution_controls.stop_status <> 'EMERGENCY_STOP'""",
                (user_id, status, self.account_id, now))
            connection.execute(
                """UPDATE ai_execution_authorizations SET status = ?, stop_reason = NULL, updated_at = ?
                WHERE trading_account_id = ? AND status <> 'EMERGENCY_STOP'
                AND rowid IN (SELECT MAX(rowid) FROM ai_execution_authorizations
                              WHERE trading_account_id = ? GROUP BY user_id)""",
                (status, now, self.account_id, self.account_id),
            )
            if emergency:
                for order in self.simulation_orders.list_on_account():
                    if order["remoteStatus"] == "FINISHED" or order["submitPhase"] == "ABANDONED":
                        continue
                    try:
                        proposal = self.store.get(str(order["proposalId"]))
                    except KeyError:
                        continue
                    if proposal["payload"].get("automationSource") in {"ai_autonomous", "deterministic_stop"}:
                        self.simulation_orders.request_cancel(
                            str(order["clientOrderId"]), user_id=proposal["userId"], connection=connection)
                for stop in connection.execute("""SELECT id, strategy_metadata_json FROM ai_stop_orders
                    WHERE trading_account_id = ? AND strategy_metadata_json IS NOT NULL""",
                    (self.account_id,)).fetchall():
                    metadata = json.loads(stop["strategy_metadata_json"])
                    metadata["disarmed"] = True
                    connection.execute("UPDATE ai_stop_orders SET strategy_metadata_json = ? WHERE id = ?",
                                       (json.dumps(metadata), stop["id"]))
                connection.execute(
                    """UPDATE ai_stop_orders SET status = 'CANCELLED', updated_at = ?,
                    last_error = 'emergency_stop'
                    WHERE trading_account_id = ? AND status = 'ARMED'""",
                    (now, self.account_id),
                )
        self._wake.set()
        if emergency:
            await self._announce_emergency(user_id)
        cancelled_orders: list[dict[str, Any]] = []
        if emergency:
            for order in self.simulation_orders.list_on_account():
                if order["displayStatus"] not in {"已受理", "部分成交"}:
                    continue
                try:
                    proposal = self.store.get(str(order["proposalId"]))
                except KeyError:
                    continue
                if proposal["payload"].get("automationSource") not in {
                    "ai_autonomous", "deterministic_stop",
                }:
                    continue
                try:
                    cancelled_orders.append(
                        await self.simulation_orders.cancel(
                            str(order["clientOrderId"]), user_id=proposal["userId"]
                        )
                    )
                except SimulationOrderError:
                    # The fence is already active. A racing terminal order only
                    # needs its existing identity to remain queryable.
                    continue
        return {
            "authorization": self.current(user_id=user_id),
            "emergency": emergency,
            "scope": "all",
            "cancelledOrders": cancelled_orders,
            "liveTrading": "disabled",
        }

    def _require_active(
        self, *, user_id: str, permission: str, symbol: str | None = None,
        volume: int | None = None,
    ) -> dict[str, Any]:
        self._require_not_stopped(
            user_id, action=PERMISSION_STOP_ACTIONS.get(permission, "close")
        )
        authorization = self.current(user_id=user_id)
        if authorization is None or authorization["status"] != "ACTIVE":
            raise AutonomyError("ai_execution_not_active")
        if permission not in authorization["permissions"]:
            raise AutonomyError("action_not_authorized")
        if symbol is not None and not any(
            allowed.casefold() == symbol.casefold()
            for allowed in authorization["allowedSymbols"]
        ):
            raise AutonomyError("symbol_not_authorized")
        maximum_volume = int(authorization["maxOrderVolume"])
        if volume is not None and (
            volume <= 0
            or (
                maximum_volume != UNLIMITED_SIMULATION_LIMIT
                and volume > maximum_volume
            )
        ):
            raise AutonomyError("volume_not_authorized")
        return authorization

    def _reserve_order(self, *, authorization_id: str) -> None:
        with self.store.lock, self.store.connection:
            cursor = self.store.connection.execute(
                """UPDATE ai_execution_authorizations
                SET used_orders = used_orders + 1, updated_at = ?
                WHERE id = ? AND status = 'ACTIVE' AND expires_at > ?
                  AND used_orders < max_orders""",
                (utc_now(), authorization_id, utc_now()),
            )
        if cursor.rowcount != 1:
            raise AutonomyError("authorization_order_limit_reached")

    def _release_order(self, *, authorization_id: str) -> None:
        with self.store.lock, self.store.connection:
            self.store.connection.execute(
                """UPDATE ai_execution_authorizations
                SET used_orders = used_orders - 1, updated_at = ?
                WHERE id = ? AND used_orders > 0""",
                (utc_now(), authorization_id),
            )

    def _require_authorization_id_active(self, authorization_id: str) -> None:
        row = self.store.connection.execute(
            """SELECT status, expires_at FROM ai_execution_authorizations
            WHERE id = ?""",
            (authorization_id,),
        ).fetchone()
        if row is None or row["status"] != "ACTIVE" or row["expires_at"] <= utc_now():
            raise AutonomyError("ai_execution_stopped")

    def _emergency_active(self, user_id: str) -> bool:
        """The emergency fence is read from both records: the controls row and the
        latest lease. Either one holding EMERGENCY_STOP keeps the fence up, so a
        stop status written later can never silently disarm it."""
        if self.controls(user_id=user_id)["stopStatus"] == "EMERGENCY_STOP":
            return True
        latest = self.store.connection.execute(
            """SELECT status FROM ai_execution_authorizations
            WHERE user_id = ? AND trading_account_id = ? ORDER BY rowid DESC LIMIT 1""",
            (user_id, self.account_id),
        ).fetchone()
        return latest is not None and latest["status"] == "EMERGENCY_STOP"

    def _require_exit_allowed(self, user_id: str) -> None:
        """A protective exit answers only to an emergency stop: lease state, budget
        and volume caps never fence it (owner decision 2026-09-20)."""
        if self._emergency_active(user_id):
            raise AutonomyError("ai_execution_stopped")

    async def execute_trade(
        self,
        *,
        user_id: str,
        session_id: str,
        symbol: str,
        side: Literal["buy_open", "sell_open", "buy_close", "sell_close"],
        volume: int,
        price_intent: Literal["passive", "aggressive", "last"],
        reason: str,
        position_bucket: Literal["today", "historical", "all"] | None = None,
        before_dispatch: Callable[[str, str], None] | None = None,
        strategy_id: str | None = None,
        strategy_bar_time: str | None = None,
        ai_initiated: bool = False,
        strategy_levels: dict[str, Any] | None = None,
        strategy_add: bool = False,
        strategy_spread: bool = False,
        strategy_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if ai_initiated:
            self._require_execution_owner(user_id)
            self._require_autonomy_enabled(user_id)
        else:
            self._require_account_write(user_id)
        generation = self.controls(user_id=user_id)["generation"]
        self._require_not_stopped(user_id, generation, action=_side_stop_action(side))
        original_guard = before_dispatch
        def guard(proposal_id: str, client_order_id: str) -> None:
            self._require_not_stopped(user_id, generation, action=_side_stop_action(side))
            if original_guard:
                original_guard(proposal_id, client_order_id)
        timeframes = ()
        if strategy_id is not None and side.endswith("_open") and not strategy_add:
            from .strategy_rules import StrategyPlan, plan_timeframes, requires_entry_recheck
            strategy = self.store.get_strategy_draft(strategy_id, user_id=user_id)
            if requires_entry_recheck(strategy["definition"]):
                timeframes = plan_timeframes(StrategyPlan.model_validate(strategy["definition"]["plan"]))
        # Refresh every input used by the sending-time recheck before acquiring
        # the action lock, so slow Sidecar reads cannot hold up an emergency stop.
        async with provider_read_snapshot(self.data_provider, symbols=(symbol,), timeframes=timeframes, refresh=True):
            self._require_not_stopped(user_id, generation, action=_side_stop_action(side))
            return await self.execute_trade_snapshot(user_id=user_id, session_id=session_id, symbol=symbol, side=side, volume=volume, price_intent=price_intent, reason=reason, position_bucket=position_bucket, before_dispatch=guard, strategy_id=strategy_id, strategy_bar_time=strategy_bar_time, generation=generation, ai_initiated=ai_initiated, strategy_levels=strategy_levels, strategy_add=strategy_add, strategy_spread=strategy_spread, strategy_attempt_id=strategy_attempt_id)

    async def execute_trade_snapshot(
        self,
        *,
        user_id: str,
        session_id: str,
        symbol: str,
        side: Literal["buy_open", "sell_open", "buy_close", "sell_close"],
        volume: int,
        price_intent: Literal["passive", "aggressive", "last"],
        reason: str,
        position_bucket: Literal["today", "historical", "all"] | None = None,
        before_dispatch: Callable[[str, str], None] | None = None,
        strategy_id: str | None = None,
        strategy_bar_time: str | None = None,
        generation: int | None = None,
        ai_initiated: bool = False,
        strategy_levels: dict[str, Any] | None = None,
        strategy_add: bool = False,
        strategy_spread: bool = False,
        strategy_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if generation is None:
            generation = self.controls(user_id=user_id)["generation"]
        async with self._action_lock:
            self._require_not_stopped(user_id, generation, action=_side_stop_action(side))
            requested_symbol = symbol.strip()
            permission = "open" if side.endswith("_open") else "close"
            self.ensure_runtime_access(
                user_id=user_id, session_id=session_id, permission=permission,
                symbol=requested_symbol, volume=volume, ai_initiated=ai_initiated,
            )
            authorization = self._require_active(
                user_id=user_id, permission=permission, symbol=requested_symbol, volume=volume
            )
            if authorization["sessionId"] != session_id:
                raise AutonomyError("authorization_session_mismatch")
            protection = None
            if strategy_id is not None:
                if side.endswith("_open") and not strategy_spread:
                    # The AI arms the strategy's stop on its own lease: that lease must allow stops.
                    # 价差 legs carry no stop of their own (the per-second 价差止损/止盈 is the
                    # protection), so a spread leg skips this requirement.
                    self._require_active(user_id=user_id, permission="stop", symbol=requested_symbol, volume=volume)
                protection = self._strategy_protection(
                    user_id=user_id, session_id=session_id, strategy_id=strategy_id, symbol=requested_symbol,
                    side=side, volume=volume, position_bucket=position_bucket,
                    authorization=authorization, strategy_bar_time=strategy_bar_time,
                    strategy_levels=strategy_levels, strategy_add=strategy_add,
                    require_auto_signal=ai_initiated,
                    strategy_spread=strategy_spread, strategy_attempt_id=strategy_attempt_id)
            symbol = next(
                allowed for allowed in authorization["allowedSymbols"]
                if allowed.casefold() == requested_symbol.casefold()
            )
            return await self._create_and_dispatch(
                user_id=user_id,
                session_id=session_id,
                authorization_id=str(authorization["authorizationId"]),
                symbol=symbol,
                side=side,
                volume=volume,
                price_intent=price_intent,
                position_bucket=position_bucket,
                reason=reason,
                source="ai_autonomous",
                before_dispatch=before_dispatch,
                protection=protection,
            )

    def _strategy_protection(
        self, *, user_id: str, session_id: str, strategy_id: str, symbol: str, side: str, volume: int,
        position_bucket: str | None, authorization: dict[str, Any], strategy_bar_time: str | None,
        strategy_levels: dict[str, Any] | None = None, strategy_add: bool = False,
        strategy_spread: bool = False, require_auto_signal: bool = False,
        strategy_attempt_id: str | None = None,
    ) -> dict[str, Any] | None:
        """The protection a strategy's order carries to its fill (the strategy's stop and take
        profit, armed by sync_protection once the entry fills). The same checks for the monitor's
        own orders (auto) and a confirmed strategy card (出卡确认); None for a close. An 加仓
        (``strategy_add``) joins this strategy's own protected position instead of a fresh one:
        its row carries ``addIndex`` and the position's original stop rule re-anchored. A spread
        leg (``strategy_spread``) arms nothing — the per-second 价差止损/止盈 is the protection —
        but the working-position fence still applies, per leg symbol."""
        self.sync_strategy_protection(user_id=user_id, symbol=symbol)
        strategy = self.store.get_strategy_draft(strategy_id, user_id=user_id)
        if require_auto_signal and str(strategy.get("signalAction") or "auto") != "auto":
            # 528-B: 监控轮开始后用户把执行方式改成了 confirm/alert——发送前再读一次，
            # 按新口径不发这单（出卡确认走用户自己的确认，不复核这里）。
            raise AutonomyError("strategy_signal_not_auto")
        if strategy_spread:
            spec = (strategy["definition"].get("plan") or {}).get("spread") or {}
            if strategy["status"] != "monitoring":
                raise AutonomyError("strategy_not_monitoring")
            if not any(str(leg.get("symbol") or "").casefold() == symbol.casefold()
                       for leg in spec.get("legs") or []):
                raise AutonomyError("strategy_leg_not_in_spread")
            for stop in self.store.connection.execute(
                """SELECT * FROM ai_stop_orders
                WHERE user_id = ? AND symbol = ? AND trading_account_id = ?
                AND strategy_metadata_json IS NOT NULL""",
                (user_id, symbol, self.account_id),
            ).fetchall():
                if stop["remaining_volume"] <= 0:
                    continue
                if side.endswith("_open") and stop["status"] in {"ARMED", "TRIGGERING", "SUBMITTED", "UNKNOWN", "PARTIAL"}:
                    raise AutonomyError("strategy_position_already_protected")
                if stop["status"] in {"TRIGGERING", "SUBMITTED", "UNKNOWN"} and stop["position_bucket"] == position_bucket:
                    raise AutonomyError("strategy_protection_close_pending")
            return None
        if strategy["status"] != "monitoring" or strategy["symbol"].casefold() != symbol.casefold():
            raise AutonomyError("strategy_not_monitoring")
        held: list[tuple[Any, dict[str, Any]]] = []
        for stop in self.store.connection.execute(
            """SELECT * FROM ai_stop_orders
            WHERE user_id = ? AND symbol = ? AND trading_account_id = ?
            AND strategy_metadata_json IS NOT NULL""",
            (user_id, strategy["symbol"], self.account_id),
        ).fetchall():
            if stop["remaining_volume"] <= 0:
                continue
            metadata = json.loads(stop["strategy_metadata_json"])
            own = metadata.get("strategyId") == strategy_id
            if own:
                held.append((stop, metadata))
            # 策略只管自己的仓 (2026-10-09): only this strategy's own protected position blocks a
            # fresh open; a manual lot's stop or another strategy's row on the same contract does
            # not (10-09 night: a manual 沪金 stop refused the strategy's entry). Its fills and closes
            # stay apart by attribution (#652 / #654).
            if own and side.endswith("_open") and stop["status"] in {"ARMED", "TRIGGERING", "SUBMITTED", "UNKNOWN", "PARTIAL"}:
                if strategy_add:
                    continue       # 加仓: this strategy's own position is what it joins
                raise AutonomyError("strategy_position_already_protected")
            if stop["status"] in {"TRIGGERING", "SUBMITTED", "UNKNOWN"} and stop["position_bucket"] == position_bucket:
                raise AutonomyError("strategy_protection_close_pending")
        if not side.endswith("_open"):
            return None
        sizing = (strategy["definition"].get("plan") or {}).get("sizing") or {}
        if sizing.get("mode") == "equity_percent" and sizing.get("equityPercent") is not None:
            # 按资金比例 (2026-10-09): the sending-time recheck on fresh facts — the same shared
            # core the intent used. A budget that shrank between the preview and the send refuses
            # this order (both the monitor's own and a confirmed card's); the retry path re-reads
            # its original order, never opens around this fence.
            from .strategy_funds import live_budget
            budget = live_budget(
                self.store, self.data_provider, user_id=user_id, strategy_id=strategy_id,
                symbol=symbol, account=None, market=None,
                fee_settings=getattr(self.simulation_orders, "fee_settings", None),
                gateway=self.simulation_orders,
                equity_percent=float(sizing["equityPercent"]),
                max_lots=(int(sizing["maxLots"]) if sizing.get("maxLots") is not None else None),
                exclude_attempt_id=strategy_attempt_id)
            if int(budget.get("lots") or 0) < volume:
                raise AutonomyError("strategy_funds_budget_exceeded",
                                    budget.get("unavailableReason")
                                    or f"资金比例预算只剩 {budget.get('lots') or 0} 手，本次 {volume} 手不发。")
        # 回撤停机 (2026-10-09): the persistent per-strategy fence — a triggered or unattributable
        # round refuses new opens on every path (the monitor's own orders and confirmed cards
        # alike). A row from another definition or an edited-away limit is a different round, not
        # this one's fence; the monitor re-baselines it on its next poll. 身份用风险轮摘要 (#692):
        # 切图表周期是同一轮，围栏不因周期重绑而失守；旧行存的完整摘要同样有效。
        if (strategy["definition"].get("plan") or {}).get("maxDrawdownPercent") is not None:
            from .strategy_funds import round_identity_digest
            dd = self.store.strategy_drawdown_state(strategy_id=strategy_id, user_id=user_id)
            if (dd is not None
                    and str(dd["strategyDigest"]) in {
                        str(strategy["definitionDigest"]),
                        round_identity_digest(strategy["definition"])}
                    and str(dd["status"]) in {"liquidating", "stopped", "unavailable"}):
                raise AutonomyError(
                    "strategy_drawdown_locked",
                    str(dd["reason"] or "本策略回撤停机处理中，暂不开新仓。"))
        limits = strategy["definition"]["riskLimits"]
        protection: dict[str, Any] = {
            "strategyId": strategy_id, "stopLossTicks": int(limits["stopLossTicks"]),
            "takeProfitTicks": limits["takeProfitTicks"],
            "perTradeRisk": limits.get("perTradeRisk"),
            "timeframeSeconds": int(strategy["definition"]["timeframeSeconds"]),
            "authorizationId": authorization["authorizationId"], "sessionId": session_id}
        plan = strategy["definition"].get("plan") or {}
        pyramid = plan.get("pyramiding") or None
        from .strategy_rules import evaluate_plan, requires_entry_recheck, uses_structural_stop
        if strategy_add:
            if pyramid is None:
                raise AutonomyError("strategy_add_not_enabled")
            wanted = "long" if side == "buy_open" else "short"
            if not held or {str(row["position_direction"]) for row, _ in held} != {wanted}:
                raise AutonomyError("strategy_position_already_protected")
            add_index = max(int(meta.get("addIndex") or 0) for _, meta in held) + 1
            if add_index > int(pyramid["maxAdds"]):
                raise AutonomyError("strategy_add_limit_reached")
            protection["addIndex"] = add_index
            protection["moveStop"] = bool(pyramid["moveStop"])
            if uses_structural_stop(strategy["definition"]):
                # The original stop was structural: re-anchor its initial distance on this fill
                # (the engine's open_position), recorded with the initial open's row.
                row, meta = next(((r, m) for r, m in held if not int(m.get("addIndex") or 0)), held[0])
                distance = meta.get("initialStopDistance")
                if distance is None:
                    distance = str(abs(Decimal(str(meta["entryPrice"])) - Decimal(str(row["trigger_price"]))))
                protection["stopDistance"] = distance
        if plan.get("trailingStop"):
            # 追踪止损: carried to the armed stop, which tightens it on the latest price.
            protection["trailing"] = plan["trailingStop"]
        if plan.get("breakeven"):
            # 保本: carried to the armed stop too; the same tightening moves it to the entry price.
            protection["breakeven"] = plan["breakeven"]
        # ATR 止损 / 吊灯追踪 / 保本与分段止盈的 ATR14: the signal bar's ATR, frozen in the intent and
        # carried with the order or the card; a later confirmation still uses it (no signal-bar recheck).
        from .strategy_rules import uses_exit_atr
        levels = strategy_levels or {}
        for key, needed in (("stopDistance", bool(plan.get("atrStop"))),
                            ("entryAtr", (plan.get("trailingStop") or {}).get("mode") == "atr_chandelier"),
                            ("exitAtr", uses_exit_atr(plan))):
            if not needed:
                continue
            try:
                value = Decimal(str(levels[key]))
            except (KeyError, InvalidOperation):
                raise AutonomyError("strategy_atr_unavailable") from None
            if not value.is_finite() or value <= 0:
                raise AutonomyError("strategy_atr_unavailable")
            protection[key] = str(value)
        if requires_entry_recheck(strategy["definition"]) and not strategy_add:
            evaluated = evaluate_plan(self.data_provider, strategy["definition"], symbol)
            key = "entryLong" if side == "buy_open" else "entryShort"
            structure = evaluated.get("structures", {}).get(key)
            if (not evaluated["rules"][key] or (uses_structural_stop(strategy["definition"]) and not structure)
                    or evaluated["evaluatedBarTime"] != strategy_bar_time):
                raise AutonomyError("strategy_pullback_signal_expired")
            if structure:
                protection["structure"] = structure
            alignment = evaluated.get("entryAlignments", {}).get(key)
            if alignment:
                if self.store.strategy_alignment_used(strategy_id=strategy_id, user_id=user_id,
                        alignment_id=alignment["id"], dispatched_only=True):
                    raise AutonomyError("strategy_alignment_already_used")
                protection["alignmentId"] = alignment["id"]
        return protection

    @staticmethod
    def _priced_protection(
        protection: dict[str, Any], payload: dict[str, Any], *, side: str, volume: int,
    ) -> tuple[dict[str, Any], float]:
        """Validate a strategy protection against the order's own price, tick and multiplier:
        the stop price it implies, the structural stop's side, the per-trade budget. Returns the
        protection as stored on the order link (with priceTick / contractMultiplier) and the stop."""
        tick = Decimal(str(payload["priceTick"]))
        if protection["stopLossTicks"] <= 0 or (protection["takeProfitTicks"] is not None and protection["takeProfitTicks"] <= 0):
            raise AutonomyError("invalid_strategy_protection")
        price = Decimal(str(payload["limitPrice"]))
        distance = tick * protection["stopLossTicks"]
        stop_price = float(price - distance if side == "buy_open" else price + distance)
        protection = dict(protection)
        if protection.get("structure"):
            from .risk import ceil_to_tick, floor_to_tick
            structure = protection["structure"]
            stop = (floor_to_tick(Decimal(str(structure["low"])), tick) - tick if side == "buy_open"
                    else ceil_to_tick(Decimal(str(structure["high"])), tick) + tick)
            if stop <= 0 or (stop >= price if side == "buy_open" else stop <= price):
                raise AutonomyError("invalid_strategy_protection")
            stop_price = float(stop)
            protection["fixedStopPrice"] = str(stop)
        if protection.get("stopDistance") is not None:
            # ATR 止损 checked here against the order price; the fill re-anchors it on its own price.
            from .strategy_engine import stop_from_distance
            distance = Decimal(str(protection["stopDistance"]))
            stop = stop_from_distance(price, distance, tick, long=side == "buy_open")
            if distance <= 0 or stop <= 0:
                raise AutonomyError("invalid_strategy_protection")
            stop_price = float(stop)
        if protection.get("perTradeRisk") is not None:
            loss = abs(price - Decimal(str(stop_price))) * Decimal(str(payload["contractMultiplier"])) * volume
            if loss > Decimal(str(protection["perTradeRisk"])):
                raise AutonomyError("strategy_trade_budget_exceeded")
        return {**protection, "priceTick": str(tick), "contractMultiplier": str(payload["contractMultiplier"])}, stop_price

    async def strategy_card_protection(
        self, *, user_id: str, session_id: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """出卡确认 (2026-09-25): the protection a confirmed strategy card's order carries, built
        exactly as for the monitor's own order of the same signal, before the card's order is
        prepared and dispatched. The user confirming the card is what arms it, so the lease is the
        user-driven one (like arming their own stop); a close card needs none."""
        source = payload.get("strategySource") or {}
        side = str(payload.get("side") or "")
        if not source.get("strategyId") or not side.endswith("_open"):
            return None
        symbol, volume = str(payload["symbol"]), int(payload["volume"])
        strategy_add = bool(source.get("add"))
        timeframes: tuple[int, ...] = ()
        from .strategy_rules import StrategyPlan, plan_timeframes, requires_entry_recheck
        strategy = self.store.get_strategy_draft(str(source["strategyId"]), user_id=user_id)
        if requires_entry_recheck(strategy["definition"]) and not strategy_add:
            timeframes = tuple(plan_timeframes(StrategyPlan.model_validate(strategy["definition"]["plan"])))
        async with provider_read_snapshot(self.data_provider, symbols=(symbol,), timeframes=timeframes, refresh=True):
            async with self._action_lock:
                self._require_account_write(user_id)
                # Only the user's own stop fences their card: 停止 / 急停 refuse it, 只停新开仓 refuses
                # the open. AI 自主执行 being off does not — this protection is the user's.
                code = stop_blocks(self.controls(user_id=user_id)["stopStatus"], "open")
                if code is not None:
                    raise AutonomyError(code)
                authorization = self.current(user_id=user_id)
                if authorization is None or not (authorization["status"] == "ACTIVE" or self._autonomy_off(authorization)):
                    # No lease yet (or an expired one): the user-driven default lease, as for their own stop.
                    self.ensure_runtime_access(user_id=user_id, session_id=session_id, permission="stop",
                                               symbol=symbol, volume=None)
                    authorization = self.current(user_id=user_id)
                if authorization is None:
                    raise AutonomyError("ai_execution_not_active")
                # The lease is only the protective stop's reference and emergency fence: an armed
                # stop triggers on the emergency fence alone (_stop_authorization / _require_exit_allowed).
                protection = self._strategy_protection(
                    user_id=user_id, session_id=session_id, strategy_id=str(source["strategyId"]), symbol=symbol,
                    side=side, volume=volume, position_bucket=payload.get("positionBucket"),
                    authorization=authorization, strategy_bar_time=source.get("barTime"),
                    strategy_levels=source.get("protectionLevels"), strategy_add=strategy_add,
                    strategy_attempt_id=(str(source["attemptId"]) if source.get("attemptId") else None))
        if protection is None:
            return None
        return self._priced_protection(protection, payload, side=side, volume=volume)[0]

    async def manual_card_protection(
        self, *, user_id: str, session_id: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """下单顺带保护 (2026-10-02): the protection a confirmed manual open card carries to its
        fill — the user's own stop / take prices, already validated against the card when it was
        made. Confirming the card is what arms them, so the lease is the user-driven one (as for
        their own stop); only the account's stop fences (停止 / 急停 / 只停新开仓) refuse it."""
        symbol = str(payload["symbol"])
        async with self._action_lock:
            self._require_account_write(user_id)
            code = stop_blocks(self.controls(user_id=user_id)["stopStatus"], "open")
            if code is not None:
                raise AutonomyError(code)
            authorization = self.current(user_id=user_id)
            if authorization is None or not (authorization["status"] == "ACTIVE" or self._autonomy_off(authorization)):
                # No lease yet (or an expired one): the user-driven default lease, as for their own stop.
                self.ensure_runtime_access(user_id=user_id, session_id=session_id, permission="stop",
                                           symbol=symbol, volume=None)
                authorization = self.current(user_id=user_id)
            if authorization is None:
                raise AutonomyError("ai_execution_not_active")
        carried = payload.get("protection") or {}
        protection = {str(key): str(carried[key]) for key in ("stopLossPrice", "takeProfitPrice") if carried.get(key) is not None}
        if not protection:
            return None
        return {
            "source": "manual",
            "fixedStopPrice": protection.get("stopLossPrice"),
            "fixedTakeProfitPrice": protection.get("takeProfitPrice"),
            "priceTick": str(payload["priceTick"]),
            "contractMultiplier": str(payload["contractMultiplier"]),
            "authorizationId": authorization["authorizationId"],
            "sessionId": session_id,
            # 追踪止损 (2026-10-25): the strategy's trailing on the armed stop (ATR frozen at confirmation); 浮盈 1R
            # 后移到成本 becomes its breakeven once the fill price is known (strategy_protection).
            **manual_trailing(self.data_provider, symbol, carried.get("trailing"), long=str(payload["side"]).startswith("buy")),
            **({"breakevenAtR": True} if carried.get("breakevenAtR") else {}),
        }

    async def _announce_stop_trigger(
        self, *, row: sqlite3.Row, session_id: str, kind_word: str,
        trigger_price: Any = None, latest: Any = None, order_status: str = "结果未知",
        limit_price: Any = None, client_order_id: str | None = None,
    ) -> None:
        """One notice for one fired protection; never raises (the close order already went out).
        The price it names is the close order's actual dispatched limit (#750: a retry after a
        settlement-pending refusal is re-priced, so the trigger-time quote is not it)."""
        try:
            user_id = str(row["user_id"])
            name = self._contract_name(str(row["symbol"]))
            direction = "多" if row["position_direction"] == "long" else "空"
            intent = {"aggressive": "对手价", "passive": "挂单价", "last": "最新价"}.get(
                str(row["price_intent"]), "对手价")
            text = (f"{name} {direction}单 {int(row['volume'])} 手{kind_word}触发，"
                    f"已按{intent}发出平仓单"
                    + (f"（委托价 {price_text(limit_price)}）" if limit_price not in (None, "") else ""))
            target = self._notice_target(user_id, session_id)
            # The line plus its reminder card under one turn (2026-10-04): trigger price, latest price and
            # the close order's read-back state (结果未知 stays 结果未知); the 平仓单 figure then follows that
            # order to its end under the same cardId, and so does the 提醒中心 row's state (#932). Delivered
            # through the one entry: 止损止盈触发's switch, once per fired close order (a re-armed partial
            # that fires again is a new order), the desktop notification, the call and the push.
            turn_id = f"protection-{row['id']}"
            at = datetime.now(UTC).isoformat()
            symbol = str(row["symbol"])
            card = reminder_card(
                turn_id=turn_id, kind="protection", title=f"{kind_word}触发", at=at, symbol=symbol,
                summary=text,
                figures=[("触发价", price_text(trigger_price) if trigger_price not in (None, "") else None),
                         ("最新价", price_text(latest) if latest is not None else None),
                         ("委托价", price_text(limit_price) if limit_price not in (None, "") else None),
                         order_figure("平仓单", order_status),
                         ("手数", f"{int(row['volume'])} 手")],
                actions=[ask_action("问 AI 怎么办", f"{name} {kind_word}触发了，接下来怎么办？"),
                         ui_action("打开持仓", {"panel": "positions"})])
            delivery = await deliver_via_store(self.store, ReminderSpec(
                kind="stop_triggered", user_id=user_id, account_id=self.account_id,
                dedupe_key=f"{row['id']}:{client_order_id or uuid4().hex}", title=f"{kind_word}触发 · {name}",
                body=text, symbol=symbol, name=name, at=at, message=text, turn_id=turn_id,
                cards=[("card.reminder", card)], session_id=target or None, auto_landing=False,
                state=order_state(order_status, volume=int(row["volume"]), price=limit_price, label="平仓单"),
                speech=text, voice_kind=turn_id,
                push=PushMessage(kind="stop_triggered", title=f"{kind_word}触发 · {name}", body=text,
                                 short=f"{name}的{kind_word}触发了", tag=f"protection-{row['id']}",
                                 url=deep_link("card", str(card["cardId"])))),
                broker=self.broker, voice=self.voice_lookup() if callable(self.voice_lookup) else None)
            if (delivery.landed and delivery.session_id and client_order_id
                    and not execution_finished(order_status)):
                track_reminder_order(self.broker, delivery.session_id, card, client_order_id=client_order_id,
                                     figure="平仓单", simulation_orders=self.simulation_orders, user_id=user_id)
        except Exception as error:
            log.debug("stop trigger notice not delivered: %s", error)

    async def _create_and_dispatch(
        self,
        *,
        user_id: str,
        session_id: str,
        authorization_id: str,
        symbol: str,
        side: str,
        volume: int,
        price_intent: str,
        position_bucket: str | None,
        reason: str,
        source: str,
        before_dispatch: Callable[[str, str], None] | None = None,
        protection: dict[str, Any] | None = None,
        protective: bool = False,
    ) -> dict[str, Any]:
        proposal_id, turn_id = str(uuid4()), str(uuid4())
        payload = {
            "turnId": turn_id,
            "proposalId": proposal_id,
            "version": 1,
            **self.simulation_orders.build_candidate(
                user_id=user_id,
                symbol=symbol,
                side=side,
                volume=volume,
                price_intent=price_intent,
                position_bucket=position_bucket,
                reason=reason,
            ),
            "updatedAt": utc_now(),
            "environment": "simulation",
            "status": "draft",
            "authorizationMode": "delegated_session",
            "authorizationId": authorization_id,
            "automationSource": source,
        }
        if protection is not None:
            protection, payload["stopPrice"] = self._priced_protection(protection, payload, side=side, volume=volume)
            payload["riskCheck"] = self.simulation_orders.risk_check(payload, user_id)
        if payload.get("riskCheck", {}).get("passed") is not True:
            raise AutonomyError("risk_check_failed")
        self.store.create(
            proposal_id=proposal_id,
            session_id=session_id,
            proposal_type="trade",
            status="draft",
            payload=payload,
            user_id=user_id,
            version=1,
            # 归属 (#528): 单子落在本执行负责人自己的账户台账，不再默认账户兜底。
            trading_account_id=self.account_id,
        )
        try:
            with self.store.transaction() as connection:
                order = self.simulation_orders.prepare(
                    proposal_id=proposal_id,
                    user_id=user_id,
                    payload=payload,
                    connection=connection,
                )
                if protection is not None:
                    connection.execute("UPDATE tqkq_sim_order_links SET protection_json = ? WHERE client_order_id = ?",
                                       (json.dumps(protection), order["clientOrderId"]))
                payload.update(
                    {
                        "clientOrderId": order["clientOrderId"],
                        "status": "candidate_recorded",
                    }
                )
                audit_id = self.store.audit(
                    proposal_id,
                    "delegated-execute",
                    {
                        "authorizationId": authorization_id,
                        "clientOrderId": order["clientOrderId"],
                        "source": source,
                    },
                    connection=connection,
                )
                payload["auditId"] = audit_id
                self.store.update(
                    proposal_id,
                    status="candidate_recorded",
                    payload=payload,
                    version=1,
                    connection=connection,
                )
        except Exception:
            current = self.store.get(proposal_id)
            if current["status"] == "draft":
                failed = dict(current["payload"])
                failed.update({"status": "rejected", "failure": "deterministic_validation_failed"})
                self.store.update(
                    proposal_id, status="rejected", payload=failed, version=1
                )
            raise
        if before_dispatch is not None:
            before_dispatch(proposal_id, str(payload["clientOrderId"]))
        return await self._dispatch_prepared_order(
            user_id=user_id, authorization_id=authorization_id,
            client_order_id=str(payload["clientOrderId"]), protective=protective,
        )

    async def retry_trade(self, *, user_id: str, client_order_id: str) -> dict[str, Any]:
        self._require_account_write(user_id)
        async with provider_read_snapshot(self.data_provider, symbols=(self.simulation_orders.get(client_order_id, user_id=user_id)["request"]["symbol"],), refresh=True):
            return await self.retry_trade_snapshot(user_id=user_id, client_order_id=client_order_id)

    async def retry_trade_snapshot(self, *, user_id: str, client_order_id: str) -> dict[str, Any]:
        """Resume the original explicitly unsent order, never an unknown submission."""
        async with self._action_lock:
            order = self.simulation_orders.get(client_order_id, user_id=user_id)
            payload = self.store.get(str(order["proposalId"]))["payload"]
            if payload.get("automationSource") != "ai_autonomous":
                raise AutonomyError("retry_source_mismatch")
            permission = "open" if order["request"]["offset"] == "OPEN" else "close"
            authorization = self._require_active(
                user_id=user_id, permission=permission,
                symbol=order["request"]["symbol"], volume=int(order["request"]["volume"]),
            )
            row = self.store.connection.execute("SELECT protection_json FROM tqkq_sim_order_links WHERE client_order_id = ?",
                                                (client_order_id,)).fetchone()
            if row and row["protection_json"] and order["submitPhase"] == "PREPARED":
                self._require_active(user_id=user_id, permission="stop", symbol=order["request"]["symbol"], volume=int(order["request"]["volume"]))
                protection = json.loads(row["protection_json"])
                strategy = self.store.get_strategy_draft(protection["strategyId"], user_id=user_id)
                if strategy["status"] != "monitoring":
                    raise AutonomyError("strategy_not_monitoring")
                protection.update({"authorizationId": authorization["authorizationId"], "sessionId": authorization["sessionId"]})
                with self.store.lock, self.store.connection:
                    self.store.connection.execute("UPDATE tqkq_sim_order_links SET protection_json = ? WHERE client_order_id = ?",
                                                  (json.dumps(protection), client_order_id))
            return await self._dispatch_prepared_order(
                user_id=user_id, authorization_id=authorization["authorizationId"],
                client_order_id=client_order_id,
            )

    async def _dispatch_prepared_order(
        self, *, user_id: str, authorization_id: str, client_order_id: str,
        protective: bool = False,
    ) -> dict[str, Any]:
        order = self.simulation_orders.get(client_order_id, user_id=user_id)
        if order["submitPhase"] != "PREPARED":
            raise SimulationOrderError("order_not_prepared")
        proposal_id = str(order["proposalId"])
        self.store.audit(proposal_id, "delegated-dispatch", {
            "authorizationId": authorization_id, "clientOrderId": client_order_id,
        })
        reserved = False
        try:
            if protective:
                # A protective exit is not budgeted against the lease and does not
                # require it to be ACTIVE; only an emergency stop fences it.
                self._require_exit_allowed(user_id)
            else:
                self._reserve_order(authorization_id=authorization_id)
                reserved = True
                # No await occurs between this durable fence check and dispatch
                # entering CALL_STARTED, so a completed emergency stop wins the race.
                self._require_authorization_id_active(authorization_id)
            dispatched = await self.simulation_orders.dispatch(
                client_order_id, user_id=user_id
            )
            try:
                if protective:
                    self._require_exit_allowed(user_id)
                else:
                    self._require_authorization_id_active(authorization_id)
            except AutonomyError:
                if dispatched["displayStatus"] in {"已受理", "部分成交"}:
                    try:
                        await self.simulation_orders.cancel(
                            client_order_id, user_id=user_id
                        )
                    except SimulationOrderError:
                        pass
                raise
        except Exception:
            if reserved:
                current_order = self.simulation_orders.get(
                    client_order_id, user_id=user_id
                )
                if current_order["submitPhase"] == "PREPARED":
                    self._release_order(authorization_id=authorization_id)
            raise
        if reserved and dispatched["submitPhase"] == "PREPARED":
            self._release_order(authorization_id=authorization_id)
        return {
            "authorizationId": authorization_id,
            "proposalId": proposal_id,
            "clientOrderId": client_order_id,
            "simulationOrder": dispatched,
            "liveTrading": "disabled",
        }

    async def query_order(self, *, user_id: str, client_order_id: str) -> dict[str, Any]:
        return await self.simulation_orders.query(client_order_id, user_id=user_id)

    async def cancel_order(self, *, user_id: str, client_order_id: str) -> dict[str, Any]:
        self.simulation_orders.get(client_order_id, user_id=user_id)
        result = await self.simulation_orders.cancel(client_order_id, user_id=user_id)
        self.store.audit(
            str(result["proposalId"]),
            "delegated-cancel",
            {"clientOrderId": client_order_id, "displayStatus": result["displayStatus"]},
        )
        return result

    @staticmethod
    def _stop_public(row: sqlite3.Row) -> dict[str, Any]:
        metadata = json.loads(row["strategy_metadata_json"]) if row["strategy_metadata_json"] else {}
        # 追踪止损 / 保本 (2026-10-25): how the stop moves, whichever column carries it; the trigger above is where it is now.
        moving = metadata or (json.loads(row["trailing_json"]) if row["trailing_json"] else {})
        return {
            "trailing": ({key: moving["trailing"][key] for key in ("mode", "value", "activateAfter", "atrTimeframe")
                          if key in moving["trailing"]}
                         if moving.get("trailing") else None),
            "breakeven": bool(moving.get("breakeven")),
            "stopId": row["id"],
            "authorizationId": row["authorization_id"],
            "sessionId": row["session_id"],
            "symbol": row["symbol"],
            "positionDirection": row["position_direction"],
            "positionBucket": row["position_bucket"],
            "volume": int(row["volume"]),
            # An empty trigger_price is the take-profit-only stop (no stop level on this row).
            "triggerPrice": float(Decimal(row["trigger_price"])) if row["trigger_price"] else None,
            "takeProfitPrice": float(row["take_profit_price"]) if row["take_profit_price"] else None,
            "remainingVolume": row["remaining_volume"],
            "strategyId": metadata.get("strategyId"),
            # "strategy" = a strategy's own rule armed it; "manual" = the user set it (order
            # card or 持仓面板), directly or through the AI stop tool.
            "source": "strategy" if metadata.get("strategyId") else "manual",
            "priceIntent": row["price_intent"],
            "status": row["status"],
            "closeProposalId": row["close_proposal_id"],
            "closeClientOrderId": row["close_client_order_id"],
            "lastError": row["last_error"],
            "triggeredAt": row["triggered_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "liveTrading": "disabled",
        }

    def list_stops(self, *, user_id: str) -> list[dict[str, Any]]:
        rows = self.store.connection.execute(
            """SELECT * FROM ai_stop_orders
            WHERE user_id = ? AND trading_account_id = ? ORDER BY created_at DESC""",
            (user_id, self.account_id),
        ).fetchall()
        return [self._stop_public(row) for row in rows]

    async def create_stop(
        self,
        *,
        user_id: str,
        session_id: str,
        symbol: str,
        position_direction: Literal["long", "short"],
        position_bucket: Literal["today", "historical", "all"],
        volume: int,
        trigger_price: float | None = None,
        take_profit_price: float | None = None,
        price_intent: Literal["passive", "aggressive", "last"] = "aggressive",
        ai_initiated: bool = False,
        trailing: dict[str, Any] | None = None,
        breakeven_after_r: bool = False,
    ) -> dict[str, Any]:
        # The account rides along so a direct stop records the position and fills it was armed on (#528).
        async with self._account_snapshot(symbol):
            return await self.create_stop_snapshot(user_id=user_id, session_id=session_id, symbol=symbol, position_direction=position_direction, position_bucket=position_bucket, volume=volume, trigger_price=trigger_price, take_profit_price=take_profit_price, price_intent=price_intent, ai_initiated=ai_initiated, trailing=trailing, breakeven_after_r=breakeven_after_r)

    async def create_stop_snapshot(
        self,
        *,
        user_id: str,
        session_id: str,
        symbol: str,
        position_direction: Literal["long", "short"],
        position_bucket: Literal["today", "historical", "all"],
        volume: int,
        trigger_price: float | None = None,
        take_profit_price: float | None = None,
        price_intent: Literal["passive", "aggressive", "last"] = "aggressive",
        ai_initiated: bool = False,
        trailing: dict[str, Any] | None = None,
        breakeven_after_r: bool = False,
    ) -> dict[str, Any]:
        async with self._action_lock:
            if ai_initiated:
                self._require_execution_owner(user_id)
                self._require_autonomy_enabled(user_id)
            else:
                self._require_account_write(user_id)
            # A protective stop is sized by the position it guards: arming it is not
            # fenced by the lease's per-order volume cap (owner decision 2026-09-20).
            self.ensure_runtime_access(
                user_id=user_id,
                session_id=session_id,
                permission="stop",
                symbol=symbol.strip(),
                volume=None,
                ai_initiated=ai_initiated,
            )
            return self._create_stop_locked(
                user_id=user_id,
                session_id=session_id,
                symbol=symbol,
                position_direction=position_direction,
                position_bucket=position_bucket,
                volume=volume,
                trigger_price=trigger_price,
                take_profit_price=take_profit_price,
                price_intent=price_intent,
                trailing=trailing,
                breakeven_after_r=breakeven_after_r,
            )

    def _create_stop_locked(
        self,
        *,
        user_id: str,
        session_id: str,
        symbol: str,
        position_direction: Literal["long", "short"],
        position_bucket: Literal["today", "historical", "all"],
        volume: int,
        trigger_price: float | None = None,
        take_profit_price: float | None = None,
        price_intent: Literal["passive", "aggressive", "last"] = "aggressive",
        trailing: dict[str, Any] | None = None,
        breakeven_after_r: bool = False,
    ) -> dict[str, Any]:
        requested_symbol = symbol.strip()
        authorization = self._require_active(
            user_id=user_id, permission="stop", symbol=requested_symbol, volume=None
        )
        if authorization["sessionId"] != session_id:
            raise AutonomyError("authorization_session_mismatch")
        symbol = next(
            allowed for allowed in authorization["allowedSymbols"]
            if allowed.casefold() == requested_symbol.casefold()
        )
        trigger, take = self._stop_levels(symbol, position_direction, trigger_price, take_profit_price)
        exchange = symbol.split(".", 1)[0]
        if exchange in CLOSE_TODAY_EXCHANGES and position_bucket not in {"today", "historical"}:
            raise AutonomyError("close_bucket_required")
        if exchange not in CLOSE_TODAY_EXCHANGES and position_bucket != "all":
            raise AutonomyError("close_bucket_not_supported")
        close_side = "sell_close" if position_direction == "long" else "buy_close"
        # Reuse the same current-position and frozen-volume checks used by a real close.
        self.simulation_orders.build_candidate(
            user_id=user_id,
            symbol=symbol,
            side=close_side,
            volume=volume,
            price_intent=price_intent,
            position_bucket=position_bucket,
            reason="验证保护性止损可平数量",
        )
        fills, positions = self._account_cut(symbol)
        anchor_volume, watermark = self._direct_stop_anchor(fills, positions, symbol, position_direction, position_bucket)
        account_day = self._direct_stop_day()
        trailing_state = self._direct_trailing(symbol, position_direction, positions, trigger, trailing, breakeven_after_r)
        stop_id = str(uuid4())
        with self.store.lock, self.store.connection:
            self._insert_direct_stop(
                self.store.connection, stop_id=stop_id, user_id=user_id,
                authorization_id=authorization["authorizationId"], session_id=session_id, symbol=symbol,
                position_direction=position_direction, position_bucket=position_bucket, volume=volume,
                trigger=trigger, take=take, price_intent=price_intent,
                anchor_volume=anchor_volume, watermark=watermark, account_day=account_day,
                trailing_json=json.dumps(trailing_state) if trailing_state else None)
        self._wake.set()
        row = self.store.connection.execute(
            "SELECT * FROM ai_stop_orders WHERE id = ?", (stop_id,)
        ).fetchone()
        if row is None:
            raise AutonomyError("stop_not_created")
        return self._stop_public(row)

    def _direct_trailing(self, symbol: str, direction: str, positions: list[Any], trigger: Decimal | None,
                         trailing: dict[str, Any] | None, breakeven_after_r: bool) -> dict[str, Any]:
        """追踪止损 / 浮盈 1R 后移到成本 on the user's own stop (2026-10-25): the same state a strategy's armed stop
        keeps (`_trail_stop`), anchored on the position's open average; the best price so far starts at the better
        of that and the latest price, so a position already in profit trails from where it is now."""
        if not trailing and not breakeven_after_r:
            return {}
        if trigger is None:
            raise AutonomyError("trailing_needs_stop", "追踪止损和浮盈 1R 后移到成本都要先填止损价")
        market = self.data_provider.market_snapshot("", symbol)
        tick, latest = Decimal(str(market.get("priceTick"))), Decimal(str(market.get("price")))
        long = direction == "long"
        position = next((item for item in positions if str(item.get("symbol") or "").casefold() == symbol.casefold()
                         and str(item.get("direction") or "") == direction), None)
        entry = cost_on_tick((position or {}).get("openPrice"), tick, long=long)
        if entry is None:
            raise AutonomyError("activity_not_current", "读不到持仓均价，稍后再设追踪止损")
        state: dict[str, Any] = {"entryPrice": str(entry), "priceTick": str(tick),
                                 "trailExtreme": str(max(entry, latest) if long else min(entry, latest)),
                                 **manual_trailing(self.data_provider, symbol, trailing, long=long)}
        if breakeven_after_r:
            state["breakeven"] = breakeven_at_r(entry, trigger, tick)
        return state

    def _stop_levels(
        self, symbol: str, position_direction: str, trigger_price: Any, take_profit_price: Any,
    ) -> tuple[Decimal | None, Decimal | None]:
        """A stop's levels checked against the contract's fresh quote: on the tick grid, above 0 and on
        the right side of the market (多仓 止损<现价<止盈, 空仓相反). Shared by every direct stop —
        the 持仓面板 / AI tool's own and the close card's 剩余仓位 replacement (第四批)."""
        if trigger_price is None and take_profit_price is None:
            raise AutonomyError("stop_price_required", "止损价和止盈价至少填一个")
        try:
            trigger = None if trigger_price is None else Decimal(str(trigger_price))
            take = None if take_profit_price is None else Decimal(str(take_profit_price))
        except InvalidOperation as error:
            raise AutonomyError("invalid_stop_price") from error
        market = self.data_provider.market_snapshot("", symbol)
        quote_is_fresh(market, allow_fixture=False, now=self.simulation_orders.trading_now())
        tick = Decimal(str(market.get("priceTick")))
        latest = Decimal(str(market.get("price")))
        for level, word in ((trigger, "止损"), (take, "止盈")):
            if (
                level is not None
                and (
                    not level.is_finite()
                    or level <= 0
                    or not tick.is_finite()
                    or tick <= 0
                    or not price_on_tick(level, tick)
                )
            ):
                raise AutonomyError(
                    "invalid_stop_price", f"{word}价要大于 0 并落在最小变动价位上")
        # 方向: 多仓 止损<现价<止盈, 空仓相反 — 反了会立刻触发或永远不触发。
        if position_direction == "long":
            if trigger is not None and trigger >= latest:
                raise AutonomyError(
                    "long_stop_must_be_below_market", f"止损要低于现价 {protection_price_text(latest)}")
            if take is not None and take <= latest:
                raise AutonomyError(
                    "long_take_profit_must_be_above_market", f"止盈要高于现价 {protection_price_text(latest)}")
        else:
            if trigger is not None and trigger <= latest:
                raise AutonomyError(
                    "short_stop_must_be_above_market", f"止损要高于现价 {protection_price_text(latest)}")
            if take is not None and take >= latest:
                raise AutonomyError(
                    "short_take_profit_must_be_below_market", f"止盈要低于现价 {protection_price_text(latest)}")
        return trigger, take

    def _insert_direct_stop(
        self, db: Any, *, stop_id: str, user_id: str, authorization_id: str, session_id: str, symbol: str,
        position_direction: str, position_bucket: str, volume: int, trigger: Decimal | None,
        take: Decimal | None, price_intent: str, anchor_volume: int | None, watermark: str | None,
        account_day: str, trailing_json: str | None = None,
    ) -> None:
        """One ARMED direct stop row (#528 anchor included) on the caller's connection, stamped with
        the account trading day it is armed on (the overnight roll compares account days)."""
        now = utc_now()
        db.execute(
            """INSERT INTO ai_stop_orders
            (id, user_id, authorization_id, session_id, symbol, position_direction,
             position_bucket, volume, trigger_price, take_profit_price, price_intent,
             status, close_proposal_id, close_client_order_id, last_error, triggered_at,
             created_at, updated_at, trading_account_id, remaining_volume,
             anchor_volume, closed_since, fill_watermark_json, account_trading_day, trailing_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ARMED', NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stop_id, user_id, authorization_id, session_id, symbol, position_direction, position_bucket,
                volume,
                # trigger_price is NOT NULL: an empty string marks the take-profit-only stop.
                format(trigger, "f") if trigger is not None else "",
                format(take, "f") if take is not None else None,
                price_intent, now, now, self.account_id, volume, anchor_volume,
                None if anchor_volume is None else 0, watermark, account_day, trailing_json,
            ),
        )

    def _account_snapshot(self, symbol: str) -> Any:
        """The read a user-set protection is written against (#528/#754): ONE combined sidecar account
        read (positions, fills, trading day) plus the contract's quote, re-fetched now — never a fresh
        fill report paired with the poll cache's positions from before an add."""
        return provider_read_snapshot(self.data_provider, symbols=(symbol,), refresh=True, account=True)

    def _account_cut(self, symbol: str) -> tuple[list[tuple[str, str, dict[str, Any]]], list[Any]]:
        """This contract's fills and the account's positions, both from `_account_snapshot`'s one read.
        Unreadable, stale or untrusted: refused (`activity_not_current`), never a guess."""
        try:
            activity = self.data_provider.account_activity()
            require_current_activity(activity)
            return _symbol_fills(activity, symbol), list(self.data_provider.positions())
        except Exception as error:
            raise AutonomyError("activity_not_current", "账户数据还没跟上，稍后再试") from error

    @staticmethod
    def _direct_stop_anchor(fills: list[tuple[str, str, dict[str, Any]]], positions: list[Any], symbol: str,
                            direction: str, bucket: str) -> tuple[int, str]:
        """The position a new direct stop guards (#528): that bucket's held volume and the fills already
        in the account, from the same `_account_cut`."""
        return _bucket_held(positions, symbol, direction, bucket), json.dumps(_fill_watermark(fills))

    def cancel_stop(self, *, user_id: str, stop_id: str) -> dict[str, Any]:
        # The owner may always remove a pending side effect, even after the
        # delegated execution window expires. Creating one still needs an active
        # authorization.
        self._require_account_write(user_id)
        with self.store.lock, self.store.connection:
            cursor = self.store.connection.execute(
                """UPDATE ai_stop_orders SET status = 'CANCELLED', updated_at = ?
                WHERE id = ? AND user_id = ? AND trading_account_id = ? AND status = 'ARMED'""",
                (utc_now(), stop_id, user_id, self.account_id),
            )
            row = self.store.connection.execute(
                "SELECT * FROM ai_stop_orders WHERE id = ? AND user_id = ? AND trading_account_id = ?",
                (stop_id, user_id, self.account_id),
            ).fetchone()
        if row is None:
            raise KeyError(stop_id)
        if cursor.rowcount != 1:
            raise AutonomyError("stop_not_cancelable")
        self._wake.set()
        return self._stop_public(row)

    # -- 第四批 (2026-10-06): 合并卡第二步 与 保本提醒的“移到成本” -------------------------------------
    # Both act on the position's own protection: the direct stops (持仓面板 / 工具直接挂) and the stops an
    # open card carried to its fill (source manual). A strategy's protection is the strategy's and is
    # never touched here. Both are the user's own action (a confirmed card / a click or a spoken 移):
    # the account write check applies, AI 自主执行 does not.

    _IN_FLIGHT_STOP_STATUSES = frozenset({"TRIGGERING", "SUBMITTED", "UNKNOWN"})

    def manual_protection_rows(self, *, user_id: str, symbol: str, direction: str) -> list[sqlite3.Row]:
        """The position's user-set protection rows (newest first), in-flight ones included."""
        return self.store.connection.execute(
            """SELECT * FROM ai_stop_orders
            WHERE user_id = ? AND trading_account_id = ? AND symbol = ? COLLATE NOCASE
            AND position_direction = ? AND remaining_volume > 0
            AND status IN ('ARMED', 'PARTIAL', 'TRIGGERING', 'SUBMITTED', 'UNKNOWN')
            AND (strategy_metadata_json IS NULL OR json_extract(strategy_metadata_json, '$.strategyId') IS NULL)
            ORDER BY created_at DESC, id DESC""",
            (user_id, self.account_id, symbol, direction),
        ).fetchall()

    def manual_protection_snapshot_rows(self, *, user_id: str, symbol: str, direction: str) -> list[dict[str, Any]]:
        """Read-only bucket mapping; confirmation still performs the formal locked convergence."""
        rows = self.manual_protection_rows(user_id=user_id, symbol=symbol, direction=direction)
        today = self._direct_stop_day()
        try:
            day = (account_protection_day(self.data_provider.account_summary(""))
                   if any(row["strategy_metadata_json"] for row in rows) else None)
            snapshot = []
            for row in rows:
                if row["strategy_metadata_json"]:
                    assert day is not None
                    bucket = fill_protection_bucket(row, json.loads(row["strategy_metadata_json"]), day=day)
                else:
                    bucket = direct_protection_bucket(row, today=today, trading_days=self._trading_days)
                snapshot.append({**dict(row), "position_bucket": bucket})
            return snapshot
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise RemainingProtectionInvalid(
                "remaining_protection_ambiguous", "账户交易日或原保护的成交来源尚不能确认，无法确定原价归属；请稍后重试") from error

    def strategy_protection_note(self, *, user_id: str, symbol: str, direction: str) -> str | None:
        """“策略「X」的保护不受影响” when the position also carries a strategy's own protection (it stays as
        it is — the user then has two stops), else None."""
        rows = self.store.connection.execute(
            """SELECT DISTINCT json_extract(strategy_metadata_json, '$.strategyId') AS strategy_id FROM ai_stop_orders
            WHERE user_id = ? AND trading_account_id = ? AND symbol = ? COLLATE NOCASE AND position_direction = ?
            AND remaining_volume > 0 AND status IN ('ARMED', 'PARTIAL', 'TRIGGERING', 'SUBMITTED', 'UNKNOWN')
            AND json_extract(strategy_metadata_json, '$.strategyId') IS NOT NULL""",
            (user_id, self.account_id, symbol, direction)).fetchall()
        names = []
        for row in rows:
            try:
                names.append(str(self.store.get_strategy_draft(str(row["strategy_id"]), user_id=user_id)["name"]))
            except KeyError:
                names.append("策略")
        if not names:
            return None
        return "、".join(f"策略「{name}」" for name in names) + "的保护不受影响，这笔持仓会同时有两套止损"

    def _sync_manual_protection(self, rows: list[sqlite3.Row], *, user_id: str, symbol: str) -> None:
        """Use the existing fill Owner; returned per-symbol failures also fence user actions."""
        if any(row["strategy_metadata_json"] for row in rows):
            for row in rows:
                if not row["strategy_metadata_json"]:
                    continue
                metadata = json.loads(row["strategy_metadata_json"])
                if metadata.get("disarmed") or not self.store.connection.execute(
                    """SELECT 1 FROM tqkq_sim_order_links WHERE client_order_id = ?
                    AND user_id = ? AND trading_account_id = ? AND protection_json IS NOT NULL""",
                    (metadata.get("entryClientOrderId"), user_id, self.account_id),
                ).fetchone():
                    raise AutonomyError("protection_target_gone", "原保护的成交来源已失效，本次修改未执行；请在持仓面板重设")
            try:  # entry-fill rows follow their fills first: the sync itself proves the report current
                blocked = self.sync_strategy_protection(user_id=user_id, symbol=symbol)
                if any(owner == user_id and item.casefold() == symbol.casefold() for owner, item in blocked):
                    raise RuntimeError("protection_sync_failed")
            except Exception as error:
                raise AutonomyError("activity_not_current", "账户成交还没跟上，稍后再点") from error

    async def replace_card_protection(
        self, *, user_id: str, session_id: str, symbol: str, direction: Literal["long", "short"],
        plan: dict[str, Any], close_order_ids: list[str], filled: int,
        finish: Callable[[Any, dict[str, Any]], bool],
    ) -> dict[str, Any] | None:
        """合并卡第二步: after the card's close finished with `filled` lots, the remaining position's
        user-set protection is REPLACED (所有者决定 2026-10-06): the old rows are cancelled
        (last_error 'replaced_by_card', no 持仓已平 notice) and one direct stop per held bucket is armed
        at the card's levels for what the account actually holds now, with the #528 anchor taken after
        the close. Everything is checked first — the account's report shows the close's fills, the
        position is still there, nothing is mid-trigger, the levels sit on the right side of a fresh
        quote, the volume is closable — so a failed replacement does not cancel or replace still-valid rows; the existing fill Owner
        can retire/shrink rows from completed fills before these checks. The
        cancel, the inserts and the step's own final write (`finish`) share one transaction: a second
        sweep or a restart can never replace twice. `card_protection_not_ready` = try again later (the
        report has not caught up); any other AutonomyError is the step's failure, its message the
        user's sentence. Returns None when the step had already ended."""
        async with self._account_snapshot(symbol):
            async with self._action_lock:
                self._require_account_write(user_id)
                try:
                    fills, positions = self._account_cut(symbol)
                except AutonomyError as error:
                    raise AutonomyError("card_protection_not_ready") from error
                wanted = set(close_order_ids)
                seen = sum(int(trade["volume"]) for _, _, trade in fills if trade["clientOrderId"] in wanted)
                if seen < filled:
                    raise AutonomyError("card_protection_not_ready")  # the fill report has not caught up
                buckets = held_protection_buckets(positions, symbol, direction)
                if not buckets:
                    raise AutonomyError("remaining_position_gone", "剩余仓位已经没有了")
                rows = self.manual_protection_rows(user_id=user_id, symbol=symbol, direction=direction)
                if any(row["status"] in self._IN_FLIGHT_STOP_STATUSES for row in rows):
                    raise AutonomyError("protection_in_flight", "原来的止损正在触发平仓")
                stop_plan = plan.get("stopLoss") or {"action": "keep"}
                take_plan = plan.get("takeProfit") or {"action": "keep"}
                self._sync_manual_protection(rows, user_id=user_id, symbol=symbol)
                for row in rows:
                    if row["strategy_metadata_json"] or row["status"] not in {"ARMED", "PARTIAL"}:
                        continue
                    if ((stop_plan.get("action") == "keep" or take_plan.get("action") == "keep")
                            and (row["anchor_volume"] is None or row["fill_watermark_json"] is None)):
                        raise AutonomyError("remaining_protection_ambiguous",
                                            "旧保护缺少持仓锚点，无法确定原价对应的手数；请明确设置或撤掉该项，或先在持仓面板整理保护单")
                    if await self._verify_direct_stop(row) in self._CLAMP_UNVERIFIED:
                        raise AutonomyError("card_protection_not_ready")
                # Legitimate fill convergence may retire/shrink old rows. A rejected replacement
                # leaves every still-valid row as the existing Owner reconciled it, never re-arms it.
                rows = self.manual_protection_rows(user_id=user_id, symbol=symbol, direction=direction)
                if any(row["status"] in self._IN_FLIGHT_STOP_STATUSES for row in rows):
                    raise AutonomyError("protection_in_flight", "原来的止损正在触发平仓")
                live = [row for row in rows if row["status"] in {"ARMED", "PARTIAL"}]
                try:
                    original = bucket_protection_levels(
                        buckets, live, keep={key for key, spec in (("stopLoss", stop_plan), ("takeProfit", take_plan))
                                           if spec.get("action") == "keep"})
                    # A kept stop keeps how it moves (追踪 / 保本), bucket by bucket and from either source (#1193);
                    # a new stop level starts fixed.
                    motions = bucket_stop_motion(buckets, live) if stop_plan.get("action") == "keep" else {}
                except RemainingProtectionInvalid as error:
                    raise AutonomyError(error.code, error.message) from error
                applied: list[dict[str, Any]] = []
                anchors: list[tuple[str, int, int | None, str | None, Decimal | None, Decimal | None]] = []
                authorization: dict[str, Any] | None = None

                def requested(spec: dict[str, Any], old: str | None) -> Decimal | None:
                    value = spec.get("price") if spec.get("action") == "set" else (
                        None if spec.get("action") == "remove" else old)
                    return Decimal(str(value)) if value is not None else None

                for previous in original:
                    bucket, held = previous["positionBucket"], previous["volume"]
                    new_stop = requested(stop_plan, previous["stopLoss"])
                    new_take = requested(take_plan, previous["takeProfit"])
                    if new_stop is None and new_take is None:
                        applied.append({**previous, "stopLoss": None, "takeProfit": None,
                                        "previousStopLoss": previous["stopLoss"], "previousTakeProfit": previous["takeProfit"]})
                        continue
                    try:
                        trigger, take = self._stop_levels(symbol, direction, new_stop, new_take)
                    except SimulationOrderError as error:
                        raise AutonomyError("quote_not_current", "行情不新鲜，没法核对新价位") from error
                    except AutonomyError as error:
                        if error.code.endswith("_stop_must_be_below_market") or error.code.endswith("_stop_must_be_above_market"):
                            raise AutonomyError("remaining_stop_crossed",
                                                f"现价已穿过 {protection_price_text(Decimal(str(new_stop)))}") from error
                        if "take_profit_must_be" in error.code:
                            raise AutonomyError("remaining_take_crossed",
                                                f"现价已越过止盈 {protection_price_text(Decimal(str(new_take)))}") from error
                        raise
                    new_stop, new_take = trigger, take
                    if authorization is None:
                        self.ensure_runtime_access(user_id=user_id, session_id=session_id, permission="stop",
                                                   symbol=symbol, volume=None)
                        authorization = self._require_active(user_id=user_id, permission="stop", symbol=symbol, volume=None)
                    close_side = "sell_close" if direction == "long" else "buy_close"
                    try:
                        self.simulation_orders.build_candidate(
                            user_id=user_id, symbol=symbol, side=close_side, volume=held,
                            price_intent="aggressive", position_bucket=bucket, reason="验证剩余仓位保护可平数量")
                    except (SimulationOrderError, RuntimeError, KeyError) as error:
                        raise AutonomyError("remaining_volume_not_closable",
                                            "剩余仓位有挂着的平仓单占着，或持仓读不到") from error
                    anchor_volume, watermark = self._direct_stop_anchor(fills, positions, symbol, direction, bucket)
                    anchors.append((bucket, held, anchor_volume, watermark, new_stop, new_take))
                    applied.append({**previous, "stopLoss": format(new_stop, "f") if new_stop is not None else None,
                                    "takeProfit": format(new_take, "f") if new_take is not None else None,
                                    "previousStopLoss": previous["stopLoss"], "previousTakeProfit": previous["takeProfit"]})
                result = {
                    "remainingVolume": sum(held for _, held in buckets),
                    "stopLoss": common_protection_price(applied, "stopLoss"),
                    "takeProfit": common_protection_price(applied, "takeProfit"),
                    "previousStopLoss": common_protection_price(applied, "previousStopLoss"),
                    "previousTakeProfit": common_protection_price(applied, "previousTakeProfit"),
                    "perBucketProtection": applied,
                    "replacedStopIds": [str(row["id"]) for row in live],
                    "stopIds": [],
                    "appliedAt": utc_now(),
                }
                stop_ids = [str(uuid4()) for _ in anchors]
                account_day = self._direct_stop_day()
                result["stopIds"] = stop_ids
                with self.store.transaction() as db:
                    if not finish(db, result):
                        return None
                    now = utc_now()
                    for row in live:
                        cursor = db.execute(
                            """UPDATE ai_stop_orders SET status = 'CANCELLED', remaining_volume = 0,
                            last_error = 'replaced_by_card', updated_at = ? WHERE id = ? AND status IN ('ARMED', 'PARTIAL')""",
                            (now, row["id"]))
                        if cursor.rowcount != 1:
                            raise AutonomyError("protection_changed", "原来的止损刚刚变了")
                    for stop_id, (bucket, held, anchor_volume, watermark, new_stop, new_take) in zip(stop_ids, anchors):
                        assert authorization is not None
                        self._insert_direct_stop(
                            db, stop_id=stop_id, user_id=user_id, authorization_id=authorization["authorizationId"],
                            session_id=session_id, symbol=symbol, position_direction=direction, position_bucket=bucket,
                            volume=held, trigger=new_stop, take=new_take, price_intent="aggressive",
                            anchor_volume=anchor_volume, watermark=watermark, account_day=account_day,
                            trailing_json=motions.get(bucket))
        self._wake.set()
        return result

    # _clamp_direct_stop / _verify_direct_stop reasons that mean "cannot prove anything this
    # second" (data unreadable or untrusted) — the answer stays pending and changes nothing.
    _CLAMP_UNVERIFIED = frozenset({"activity_unavailable", "activity_stale", "positions_unavailable",
                                    "fill_untrusted", "convergence_failed"})

    def _manual_stop_rows(self, stop_ids: list[str], *, user_id: str, symbol: str, direction: str) -> list[sqlite3.Row]:
        """The user's own (not a strategy's) stops among `stop_ids` on this account, contract and side."""
        marks = ",".join("?" for _ in stop_ids)
        return self.store.connection.execute(
            f"""SELECT * FROM ai_stop_orders WHERE id IN ({marks})
            AND user_id = ? AND trading_account_id = ? AND symbol = ? COLLATE NOCASE
            AND position_direction = ?
            AND (strategy_metadata_json IS NULL
                 OR json_extract(strategy_metadata_json, '$.strategyId') IS NULL)""",
            (*stop_ids, user_id, self.account_id, symbol, direction)).fetchall()

    async def _verified_manual_stops(
        self, stop_ids: list[str], *, user_id: str, symbol: str, direction: str,
    ) -> list[sqlite3.Row]:
        """Inside the action lock: every one of the user's stops `stop_ids` is still ARMED, not mid-trigger, and
        proven to guard the position it was armed on (#528: an entry-fill row through the fill sync, a direct row
        through its anchor and `_verify_direct_stop`; a row without an anchor is refused, never rebased onto
        whatever the account holds now). Returns the rows re-read after that proof. Shared by 移到成本 and the
        chart's dragged stop / take (图上拖线改价)."""
        rows = self._manual_stop_rows(stop_ids, user_id=user_id, symbol=symbol, direction=direction)
        if len(rows) != len(set(stop_ids)):
            raise AutonomyError("protection_target_gone",
                                "原来那笔止损已经撤了或被替换，这张卡没移；现在的止损要在持仓面板重设")
        self._sync_manual_protection(rows, user_id=user_id, symbol=symbol)
        rows = self._manual_stop_rows(stop_ids, user_id=user_id, symbol=symbol, direction=direction)
        if any(row["status"] in self._IN_FLIGHT_STOP_STATUSES for row in rows):
            raise AutonomyError("protection_in_flight", "止损正在触发平仓")
        verified: list[sqlite3.Row] = []
        for stop_id in stop_ids:
            row = next((item for item in rows if str(item["id"]) == stop_id), None)
            if row is None or row["status"] != "ARMED":
                raise AutonomyError("protection_target_gone",
                                    "原来那笔止损已经撤了或被替换，这张卡没移；现在的止损要在持仓面板重设")
            if not row["strategy_metadata_json"]:
                if row["anchor_volume"] is None or row["fill_watermark_json"] is None:
                    # Pre-#528 row (or armed while the report was unreadable): it carries no
                    # proof of the position it was armed on, and anchoring it now would bind
                    # it to whatever position happens to be held — refuse instead.
                    raise AutonomyError("protection_anchor_missing",
                                        "这笔旧止损缺少持仓锚点，核对不了还是不是原来那笔，没移；请在持仓面板重设")
                if await self._verify_direct_stop(row) in self._CLAMP_UNVERIFIED:
                    raise AutonomyError("activity_not_current", "账户数据还没跟上，稍后再点")
                row = self.store.connection.execute(
                    "SELECT * FROM ai_stop_orders WHERE id = ?", (stop_id,)).fetchone()
                if row is None or row["status"] != "ARMED":
                    raise AutonomyError("protection_target_gone",
                                        "原来那笔止损已经撤了或被替换，这张卡没移；现在的止损要在持仓面板重设")
            verified.append(row)
        return verified

    async def reprice_stop(
        self, *, user_id: str, stop_id: str, level: Literal["stop", "take"], previous: str, price: float,
    ) -> dict[str, Any]:
        """图上拖线改价 (2026-10-24): one of the user's own stops gets a new 止损 or 止盈 level, in place — the row
        keeps its position, bucket and volume, so there is no moment with no protection or two. The row must still
        guard its position (`_verified_manual_stops`, as 移到成本), still carry `previous` (the level the user
        dragged from: a stop re-priced or re-triggered meanwhile is refused, never overwritten), and the new level
        passes the same fresh-quote / tick / side checks as a new stop (`_stop_levels`). A strategy's protection is
        not the user's to move here."""
        row = self.store.connection.execute(
            "SELECT symbol, position_direction FROM ai_stop_orders WHERE id = ? AND user_id = ? AND trading_account_id = ?",
            (stop_id, user_id, self.account_id)).fetchone()
        if row is None:
            raise AutonomyError("protection_target_gone", "这笔保护已经撤了或被替换，没改")
        symbol, direction = str(row["symbol"]), str(row["position_direction"])
        column = "trigger_price" if level == "stop" else "take_profit_price"
        async with self._account_snapshot(symbol):
            async with self._action_lock:
                self._require_account_write(user_id)
                try:
                    current = (await self._verified_manual_stops(
                        [stop_id], user_id=user_id, symbol=symbol, direction=direction))[0]
                except AutonomyError as error:
                    if error.code == "protection_target_gone" and self._manual_stop_rows(
                            [stop_id], user_id=user_id, symbol=symbol, direction=direction) == []:
                        raise AutonomyError("protection_not_manual", "策略挂的保护在策略里改，图上不能拖") from error
                    raise
                if not current[column] or Decimal(str(current[column])) != Decimal(previous):
                    raise AutonomyError("protection_target_changed", "这笔保护刚被改过价位，没改；请看图上现在的线")
                trigger, take = self._stop_levels(symbol, direction, price if level == "stop" else None,
                                                  price if level == "take" else None)
                new = trigger if level == "stop" else take
                with self.store.transaction() as db:
                    cursor = db.execute(
                        f"UPDATE ai_stop_orders SET {column} = ?, changed_by = 'user', updated_at = ? WHERE id = ? AND status = 'ARMED' AND {column} = ?",
                        (format(new, "f"), utc_now(), stop_id, current[column]))
                    if cursor.rowcount != 1:
                        raise AutonomyError("protection_changed", "保护刚刚变了")
                updated = self.store.connection.execute("SELECT * FROM ai_stop_orders WHERE id = ?", (stop_id,)).fetchone()
        self._wake.set()
        return self._stop_public(updated)

    async def move_stops_to_cost(
        self, *, user_id: str, symbol: str, direction: Literal["long", "short"], cost: Decimal,
        stop_ids: list[str], triggers: list[str],
        finish: Callable[[Any, dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        """保本提醒 · 移到成本: the stops the question was asked about (`stop_ids`, each with the
        trigger it carried then) move to the cost — never a stop that appeared since (#528: a same
        symbol+direction position reopened later is a different target, whatever its cost). Inside
        the action lock every bound row is first proven to still guard its original position: an
        entry-fill row through the fill sync (a report that is not current proves nothing), a direct
        row through its #528 anchor and `_verify_direct_stop` — a row without an anchor is refused,
        never rebased onto whatever the account holds now. A row gone, replaced, re-triggered or
        re-priced fails the card, and the cost the card showed is re-derived from the position's
        current average: it is the question's basis, not a target. Everything else is unchanged —
        止盈不动, the fresh quote / tick / profit-side checks, and the moves and `finish` (the
        notice's own settle) in one transaction. Returns None when the notice had already been
        answered. The fills, the trading day and the position's average all come from ONE combined
        account read (account=True, #528/#754): the poll cache's average from before an add is never
        checked against the card's cost."""
        async with self._account_snapshot(symbol):
            async with self._action_lock:
                self._require_account_write(user_id)
                rows = await self._verified_manual_stops(stop_ids, user_id=user_id, symbol=symbol, direction=direction)
                long = direction == "long"
                for stop_id, asked in zip(stop_ids, triggers):
                    row = next(item for item in rows if str(item["id"]) == stop_id)
                    if str(row["trigger_price"]) != asked:
                        raise AutonomyError("protection_target_changed",
                                            "这笔止损刚被改过价位，这张卡没移；要移请重新确认")

                def bound() -> list[sqlite3.Row]:
                    return self._manual_stop_rows(stop_ids, user_id=user_id, symbol=symbol, direction=direction)

                try:
                    market = self.data_provider.market_snapshot("", symbol)
                    quote_is_fresh(market, allow_fixture=False, now=self.simulation_orders.trading_now())
                    tick = Decimal(str(market.get("priceTick")))
                    latest = Decimal(str(market.get("price")))
                except (SimulationOrderError, InvalidOperation, KeyError, RuntimeError) as error:
                    raise AutonomyError("quote_not_current", "行情不新鲜，先没移") from error
                _, held = self._account_cut(symbol)
                position = next((item for item in held
                                 if str(item.get("symbol") or "").casefold() == symbol.casefold()
                                 and str(item.get("direction") or "") == direction), None)
                if position is None:
                    raise AutonomyError("protection_target_gone",
                                        "这笔持仓已经没有了，这张卡没移；现在的止损要在持仓面板重设")
                basis = cost_on_tick(position.get("openPrice"), tick, long=long)
                if basis is None:
                    raise AutonomyError("activity_not_current", "读不到持仓均价，稍后再点")
                if basis != cost:
                    raise AutonomyError("cost_basis_changed",
                                        f"持仓成本已经变了（现在 {protection_price_text(basis)}），"
                                        "这张卡的价格依据过期，没移；要移请在持仓面板重设")
                if not price_on_tick(cost, tick):
                    raise AutonomyError("invalid_stop_price", "成本价不在最小变动价位上")
                if (cost >= latest) if long else (cost <= latest):
                    raise AutonomyError("cost_crossed", f"现价 {protection_price_text(latest)} 已回到成本 "
                                                        f"{protection_price_text(cost)} {'以下' if long else '以上'}，先没移")
                rows = bound()  # the verification above may have rolled or shrunk rows: re-read, then re-check
                for stop_id, asked in zip(stop_ids, triggers):
                    row = next((item for item in rows if str(item["id"]) == stop_id), None)
                    if row is None or row["status"] != "ARMED" or str(row["trigger_price"]) != asked:
                        raise AutonomyError("protection_changed", "止损刚刚变了")
                worse = [row for row in rows
                         if (Decimal(row["trigger_price"]) < cost if long else Decimal(row["trigger_price"]) > cost)]
                if not worse:
                    raise AutonomyError("no_stop_to_move", "止损已经在成本或更好的位置")
                result = {"moved": len(worse), "stopLoss": format(cost, "f"),
                          "previous": sorted({format(Decimal(row["trigger_price"]), "f") for row in worse}),
                          "stopIds": [str(row["id"]) for row in worse], "appliedAt": utc_now()}
                with self.store.transaction() as db:
                    if finish is not None and not finish(db, result):
                        return None
                    now = utc_now()
                    for row in worse:
                        cursor = db.execute(
                            """UPDATE ai_stop_orders SET trigger_price = ?, changed_by = 'user', updated_at = ?
                            WHERE id = ? AND status = 'ARMED' AND trigger_price = ?""",
                            (format(cost, "f"), now, row["id"], row["trigger_price"]))
                        if cursor.rowcount != 1:
                            raise AutonomyError("protection_changed", "止损刚刚变了")
        self._wake.set()
        return result

    async def _monitor_stops(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A provider outage leaves triggers armed; no inferred price or retrying
                # order is produced. The next normal snapshot re-evaluates the condition.
                with self.store.lock, self.store.connection:
                    self.store.connection.execute("""UPDATE ai_stop_orders SET last_error = ?
                        WHERE strategy_metadata_json IS NOT NULL AND status = 'ARMED'
                        AND trading_account_id = ?""", (str(error)[:160], self.account_id))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                continue

    async def run_once(self, *, force_reconcile: bool = False) -> None:
        await self.simulation_orders.reconcile_cancellations()
        symbols = [r[0] for r in self.store.connection.execute(
            """SELECT DISTINCT symbol FROM ai_stop_orders
            WHERE status IN ('ARMED', 'SUBMITTED', 'UNKNOWN', 'PARTIAL') AND trading_account_id = ?""",
            (self.account_id,))]
        if not symbols and not self.store.connection.execute(
                """SELECT 1 FROM tqkq_sim_order_links
                WHERE protection_json IS NOT NULL AND trading_account_id = ? LIMIT 1""",
                (self.account_id,)).fetchone():
            return
        async with provider_read_snapshot(self.data_provider, symbols=symbols):
            await self._run_stops_snapshot(force_reconcile=force_reconcile)

    async def _run_stops_snapshot(self, *, force_reconcile: bool) -> None:
        # 先让直接保护跟上当前持仓 (#528)，后面的扫描读到的就是收拢后的行。
        await self._clamp_direct_stops()
        blocked = self.sync_strategy_protection()
        rows = self.store.connection.execute(
            """SELECT * FROM ai_stop_orders
            WHERE status IN ('ARMED', 'SUBMITTED', 'UNKNOWN') AND trading_account_id = ?
            ORDER BY created_at""",
            (self.account_id,),
        ).fetchall()
        for row in rows:
            if row["status"] == "ARMED":
                if (row["user_id"], row["symbol"]) in blocked:
                    continue
                await self._check_stop(row)
            elif row["close_client_order_id"] and (force_reconcile or self._reconcile_due(row)):
                await self._reconcile_stop(row, refresh=not force_reconcile)
        if force_reconcile:
            self.sync_strategy_protection()

    @staticmethod
    def _reconcile_due(row: sqlite3.Row) -> bool:
        return _retry_due(row["updated_at"])

    async def _clamp_direct_stops(self) -> None:
        """直接保护跟住它挂上时的那笔持仓 (#528-A): 原仓平完就撤（之后同向新开的仓不归它管）、
        减仓就缩、隔夜今仓滚昨仓；平仓单已结束的部分成交保护重新武装 (#528-A')。只动直接保护
        （无 strategy_metadata 的行）；带来源的保护仍由成交同步负责。活动读数不可信就整轮跳过，
        某合约成交形状不可信就跳过该行 —— 绝不在不确定时撤保护；同时把“本快照未核对”的原因
        记到行上（触发入口据此本轮不触发，见 _verify_direct_stop）。"""
        rows = self.store.connection.execute(
            """SELECT * FROM ai_stop_orders
            WHERE trading_account_id = ? AND strategy_metadata_json IS NULL
            AND status IN ('ARMED', 'PARTIAL') ORDER BY created_at""",
            (self.account_id,),
        ).fetchall()
        if not rows:
            return
        inputs = self._direct_stop_inputs()
        self._cancel_queue = []
        try:
            for row in rows:
                if isinstance(inputs, str):
                    self._record_direct_stop_verdict(str(row["id"]), inputs)
                    continue
                await self._clamp_and_record(row, inputs)
        finally:
            queue, self._cancel_queue = self._cancel_queue, None
            await self._flush_cancel_notices(queue or [])

    def _direct_stop_inputs(self) -> tuple[dict[str, Any], list[Any], str] | str:
        """(activity, positions, trading day) of this snapshot, or why a direct stop cannot be
        checked against it."""
        try:
            activity = self.data_provider.account_activity()
        except Exception:
            return "activity_unavailable"
        try:
            require_current_activity(activity)
        except Exception:
            return "activity_stale"
        try:
            positions = self.data_provider.positions()
        except Exception:
            return "positions_unavailable"
        return activity, positions, self._direct_stop_day()

    def _direct_stop_day(self) -> str:
        """The account's own trading day — the day its positions' today/historical split belongs to
        (the summary comes from the same sidecar read as the positions; the 本站模拟 book rolls only
        once settlement completes). An account that does not report one falls back to the calendar
        day on the Owner clock, the rule rows armed without a stamped day use too."""
        try:
            day = str(self.data_provider.account_summary("").get("tradingDay") or "")
            datetime.strptime(day, "%Y-%m-%d")
            return day
        except Exception:  # noqa: BLE001 - no account day: the calendar rule stands in
            now = self.simulation_orders.trading_now() or datetime.now(UTC)
            return trading_day_at(now, self._trading_days())

    def _trading_days(self) -> list[str]:
        """The exchange calendar's known trading days (shared, cached source); [] when unreadable."""
        return calendar_trading_days(self.data_provider)

    async def _clamp_and_record(
        self, row: sqlite3.Row, inputs: tuple[dict[str, Any], list[Any], str],
    ) -> str | None:
        activity, positions, today = inputs
        try:
            reason = await self._clamp_direct_stop(row, positions=positions, activity=activity, today=today)
        except Exception as error:  # 一行读不懂不拖累其它行
            log.debug("direct stop clamp %s failed: %s", row["id"], error)
            reason = "convergence_failed"
        self._record_direct_stop_verdict(str(row["id"]), reason)
        return reason

    def _record_direct_stop_verdict(self, stop_id: str, reason: str | None) -> None:
        """未核对的原因留在仍挂着的保护上（持仓面板可见）；核对通过就清掉本类原因。"""
        with self.store.lock, self.store.connection:
            if reason:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET last_error = ?
                    WHERE id = ? AND status = 'ARMED' AND last_error IS NOT ?""",
                    (DIRECT_STOP_BLOCKED + reason, stop_id, DIRECT_STOP_BLOCKED + reason))
            else:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET last_error = NULL
                    WHERE id = ? AND last_error LIKE ?""",
                    (stop_id, DIRECT_STOP_BLOCKED + "%"))

    async def _verify_direct_stop(self, row: sqlite3.Row) -> str | None:
        """触发前（动作锁内、同一快照）把这笔直接保护再对一次原仓 (#528 P1-1 残留): 成交读不到、
        不新鲜、读不懂或收敛失败时返回原因 —— 本轮不 claim、不派发，保护原样保留，下一轮可信数据
        到了再判断撤销或缩量。None = 本快照已核对（行可能刚被撤或缩，调用方重读）。"""
        inputs = self._direct_stop_inputs()
        if isinstance(inputs, str):
            self._record_direct_stop_verdict(str(row["id"]), inputs)
            return inputs
        return await self._clamp_and_record(row, inputs)

    async def _clamp_direct_stop(
        self, row: sqlite3.Row, *, positions: list[Any], activity: dict[str, Any], today: str,
    ) -> str | None:
        symbol = str(row["symbol"])
        direction = str(row["position_direction"])
        bucket = str(row["position_bucket"] or "all")
        try:
            fills = _symbol_fills(activity, symbol)
        except ValueError:
            return "fill_untrusted"  # 这个合约的成交读不懂：这一行这一轮什么都不改
        if row["status"] == "PARTIAL":
            # 部分成交的直接保护：平仓单结束后清掉旧单身份，回 ARMED（与策略保护 _roll_positions
            # 的续接同一判断），剩余量随后向原仓余量与当前持仓收敛。它自己的平仓成交和外部平仓
            # 一样计入 closed_since —— 只计一次（按成交 id），有效量取最小值而不是相减，不会重复扣。
            linked = self.store.connection.execute(
                "SELECT last_remote_status FROM tqkq_sim_order_links WHERE client_order_id = ?",
                (str(row["close_client_order_id"] or ""),)).fetchone()
            if linked is None or str(linked["last_remote_status"]) != "FINISHED":
                return  # 平仓单还挂着或结果未知：等它结束，什么都不猜
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET status = 'ARMED',
                    close_proposal_id = NULL, close_client_order_id = NULL, updated_at = ?
                    WHERE id = ?""",
                    (utc_now(), row["id"]))
            row = self.store.connection.execute(
                "SELECT * FROM ai_stop_orders WHERE id = ?", (row["id"],)).fetchone()
            assert row is not None
        # 隔夜滚桶：挂在更早账户交易日的今仓保护，账户换日（本站模拟盘结算完成）后要平的已经是昨仓。
        current_bucket = direct_protection_bucket(row, today=today, trading_days=self._trading_days)
        if current_bucket != bucket:
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    "UPDATE ai_stop_orders SET position_bucket = 'historical', updated_at = ? WHERE id = ?",
                    (utc_now(), row["id"]))
            bucket = current_bucket
        held = _bucket_held(positions, symbol, direction, bucket)
        remaining = int(row["remaining_volume"])
        cap = remaining
        if row["anchor_volume"] is None or row["fill_watermark_json"] is None:
            # 锚点出现前挂的旧行（或挂单时读不到成交）：以此刻为起点，等同此刻新挂一笔 ——
            # anchor 取该桶当前持有量而不是它与剩余量的较小值：只保护了部分仓量时，先平掉的
            # 那部分按“先算原仓”扣，取较小值会把仍在的原仓误判成已平。本轮不据此撤或缩。
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET anchor_volume = ?, closed_since = 0,
                    fill_watermark_json = ? WHERE id = ?""",
                    (held, json.dumps(_fill_watermark(fills)), row["id"]))
        else:
            watermark = json.loads(str(row["fill_watermark_json"]))
            closed, advanced = _closes_since(fills, watermark, direction=direction, bucket=bucket)
            total = int(row["closed_since"] or 0) + closed
            if closed or advanced != watermark:
                with self.store.lock, self.store.connection:
                    self.store.connection.execute(
                        "UPDATE ai_stop_orders SET closed_since = ?, fill_watermark_json = ? WHERE id = ?",
                        (total, json.dumps(advanced), row["id"]))
            left = int(row["anchor_volume"]) - total
            if left <= 0:
                # 原仓已平完：即使现在同方向又有仓，也不是这笔保护挂上时的那笔 —— 撤。
                with self.store.lock, self.store.connection:
                    self.store.connection.execute(
                        """UPDATE ai_stop_orders SET status = 'CANCELLED', remaining_volume = 0,
                        last_error = 'anchor_position_closed', updated_at = ? WHERE id = ?""",
                        (utc_now(), row["id"]))
                await self._announce_stop_cancelled(
                    row, reopened=_bucket_held(positions, symbol, direction, "all"))
                return
            cap = left
        if held <= 0:
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET status = 'CANCELLED', remaining_volume = 0,
                    last_error = 'position_closed', updated_at = ? WHERE id = ?""",
                    (utc_now(), row["id"]))
            await self._announce_stop_cancelled(row)
            return
        target = min(remaining, cap, held)
        if target < remaining:
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET volume = ?, remaining_volume = ?, updated_at = ?
                    WHERE id = ?""",
                    (target, target, utc_now(), row["id"]))
            return  # 减仓只跟随，不打扰
        # 原仓余量与当前持仓都不小于剩余量：保护原样（等触发）。

    async def _announce_emergency(self, user_id: str) -> None:
        """急停 always reaches the account's owners (#932: no switch) — the 提醒中心 row, the desktop
        notification, the line on a live call and the push (except a device whose page is in front: it shows
        the stop itself). Never raises: the stop itself already happened."""
        try:
            owners = self.store.account_owner_ids(self.account_id) or [user_id]
        except Exception:  # noqa: BLE001
            owners = [user_id]
        message = emergency_message()
        stop_id = uuid4().hex
        for owner in owners:
            try:
                await deliver_via_store(self.store, ReminderSpec(
                    kind="emergency_stop", user_id=owner, account_id=self.account_id, dedupe_key=stop_id,
                    title="急停", body=message.body, target={"type": "chat"}, speech=message.body,
                    voice_kind="emergency-stop", push=message),
                    broker=self.broker, voice=self.voice_lookup() if callable(self.voice_lookup) else None)
            except Exception as error:  # noqa: BLE001
                log.debug("emergency notice not delivered: %s", error)

    def _notice_target(self, user_id: str, session_id: str) -> str:
        """The human chat a protection notice goes to: the named session when a person reads
        it, else the most recent one. 触发改写后的行会话是 deterministic-stop 内部会话，没人
        看 (#528) —— 通知只落到人的会话（native/codex）。"""
        target = str(session_id or "")
        try:
            session = self.broker.get_session(target, user_id) if target else None
            if target and session is not None and str(getattr(session, "agent_id", "")) in {"native", "codex"}:
                return target
        except Exception:
            pass
        recent = self.store.recent_chat_session(
            user_id=user_id, mode=None, trading_account_id=self.account_id)
        return str(recent["sessionId"]) if recent else ""

    def _contract_name(self, symbol: str) -> str:
        """螺纹2701-style contract name for a notice (the shared catalog entry, never the bare code)."""
        from .strategy_workbench import contract_label
        try:
            return contract_label(self.data_provider, symbol)
        except Exception:  # noqa: BLE001 - a notice never fails on a missing name
            return symbol

    async def _announce_stop_cancelled(self, row: sqlite3.Row, *, reopened: int = 0) -> None:
        """直接保护自动撤销：纯撤销通知在一次扫描内按 用户+合约+方向 合并成一句；“又开了新仓、要重新挂吗”
        的提议卡带着各自保护的价位，逐条发，不合并。扫描外（单行校验）的撤销立即发。"""
        if self._cancel_queue is not None:
            self._cancel_queue.append((row, reopened))
        else:
            await self._publish_stop_cancelled([(row, reopened)])

    async def _flush_cancel_notices(self, queue: list[tuple[sqlite3.Row, int]]) -> None:
        groups: dict[tuple[str, str, str], list[tuple[sqlite3.Row, int]]] = {}
        for row, reopened in queue:
            if reopened > 0:
                await self._publish_stop_cancelled([(row, reopened)])
                continue
            key = (str(row["user_id"]), str(row["symbol"]), str(row["position_direction"]))
            groups.setdefault(key, []).append((row, reopened))
        for items in groups.values():
            await self._publish_stop_cancelled(items)

    async def _publish_stop_cancelled(self, items: list[tuple[sqlite3.Row, int]]) -> None:
        """一句话到挂保护的那个会话，经统一提醒入口（#932：保护单撤销的开关、一条只发一次、桌面通知、电话在线
        才读出来、锁屏推送）。原仓平完而同方向现在又有仓（`reopened` 手）时，说清新仓没有保护，并给“重新挂”——
        按钮只以用户名义问 AI（按原价位重新挂，走既有挂保护入口与校验）和打开持仓面板的保护表单，不自动挂。
        重新挂的提议只来自单条保护（items 只有一条）；纯撤销时 items 是同一持仓的几条保护，只发一句。"""
        try:
            row, reopened = items[0]
            user_id = str(row["user_id"])
            symbol = str(row["symbol"])
            name = self._contract_name(symbol)
            direction = "多" if row["position_direction"] == "long" else "空"
            turn_id = f"protection-cancel-{row['id']}"
            target = self._notice_target(user_id, str(row["session_id"] or ""))
            at = datetime.now(UTC).isoformat()
            stop = price_text(row["trigger_price"]) if row["trigger_price"] else None
            take = price_text(row["take_profit_price"]) if row["take_profit_price"] else None
            levels = "、".join(part for part in (stop and f"止损 {stop}", take and f"止盈 {take}") if part)
            cards: list[tuple[str, dict[str, Any]]] = []
            if reopened > 0:
                kinds = "止损止盈" if stop and take else "止损" if stop else "止盈"
                volume = int(row["volume"])
                text = (f"{name} 原来那 {volume} 手{direction}单已平，{kinds}已撤；"
                        f"现在这 {reopened} 手{direction}单没有保护，要按原价位重新挂吗？")
                cards = [("card.reminder", reminder_card(
                    turn_id=turn_id, kind="protection", title="原仓已平，保护已撤", at=at, symbol=symbol,
                    summary=text,
                    figures=[("原止损", stop), ("原止盈", take),
                             ("原手数", f"{volume} 手"), ("现持仓", f"{reopened} 手", "warning")],
                    actions=[ask_action("重新挂", f"按原价位给我现在这 {reopened} 手{name}（{symbol}）"
                                                 f"{direction}单重新挂保护：{levels}"),
                             ui_action("打开持仓", {"panel": "positions"})]))]
                body = text
            else:
                text = f"{name} {direction}单持仓已平，保护自动撤销"
                body = f"原仓已平完，跟着的{levels or '保护'}已撤"
            ids = sorted(str(item[0]["id"]) for item in items)
            await deliver_via_store(self.store, ReminderSpec(
                kind="protection_cancelled", user_id=user_id, account_id=self.account_id,
                dedupe_key=",".join(ids) + (":reopened" if reopened > 0 else ""), title="保护单撤销",
                body=body, symbol=symbol, name=name, at=at, message=text, turn_id=turn_id, cards=cards,
                session_id=target or None, auto_landing=False, speech=text, voice_kind=turn_id,
                push=PushMessage(kind="protection_cancelled", title=f"保护已撤 · {name}", body=text,
                                 short=f"{name}的保护已撤", tag=turn_id,
                                 url=deep_link("card", f"reminder-{turn_id}") if cards else deep_link("chat"))),
                broker=self.broker, voice=self.voice_lookup() if callable(self.voice_lookup) else None)
        except Exception as error:
            log.debug("stop cancel notice not delivered: %s", error)

    async def _check_stop(self, row: sqlite3.Row) -> None:
        async with provider_read_snapshot(self.data_provider, symbols=(row["symbol"],)):
            return await self._check_stop_snapshot(row=row)

    async def _check_stop_snapshot(self, row: sqlite3.Row) -> None:
        async with self._action_lock:
            await self._check_stop_locked(row)

    def sync_strategy_protection(self, *, user_id=None, symbol=None):
        return sync_protection(
            self.store, self.data_provider, user_id=user_id, symbol_filter=symbol,
            trading_account_id=self.account_id,
        )

    async def _strategy_exit_ready(self, row: sqlite3.Row) -> bool:
        activity = self.data_provider.account_activity()
        require_current_activity(activity)
        metadata = json.loads(row["strategy_metadata_json"])
        def blocked(reason: str) -> bool:
            with self.store.lock, self.store.connection:
                self.store.connection.execute("UPDATE ai_stop_orders SET last_error = ? WHERE id = ?",
                                              (reason, row["id"]))
            return False
        def conflicts(order: dict[str, Any]) -> bool:
            if order.get("offset") == "OPEN":
                return False
            close_direction = "SELL" if row["position_direction"] == "long" else "BUY"
            if order.get("direction") and order["direction"] != close_direction:
                return False
            if row["symbol"].split(".")[0] in CLOSE_TODAY_EXCHANGES:
                if order.get("offset") in {"CLOSE", "CLOSETODAY"}:
                    return order["offset"] == ("CLOSETODAY" if row["position_bucket"] == "today" else "CLOSE")
            return True
        for order in activity["orders"]:
            if order.get("symbol") != row["symbol"] or order.get("status") != "ALIVE":
                continue
            if order["clientOrderId"] == metadata["entryClientOrderId"]:
                await self.simulation_orders.cancel(str(order["clientOrderId"]), user_id=str(row["user_id"]))
                return blocked("等待原开仓委托撤销剩余数量后保护性平仓。")
            if conflicts(order):
                return blocked("同方向、同持仓类别仍有平仓委托，等待其结果后继续保护。")
        for order in self.simulation_orders.list(user_id=str(row["user_id"])):
            if order["request"]["symbol"] == row["symbol"] and order_needs_reconciliation(order):
                await self.simulation_orders.query(str(order["clientOrderId"]), user_id=str(row["user_id"]))
                return blocked("同合约委托结果未知，正在查询原委托后继续保护。")
            if (order["request"]["symbol"] == row["symbol"] and order["request"]["offset"] != "OPEN"
                    and order["displayStatus"] in {"已受理", "部分成交"} and order.get("remoteStatus") != "FINISHED"
                    and conflicts(order["request"])):
                return blocked("同方向、同持仓类别仍有平仓委托，等待其结果后继续保护。")
        with self.store.lock, self.store.connection:
            self.store.connection.execute("UPDATE ai_stop_orders SET last_error = NULL WHERE id = ?", (row["id"],))
        return True

    def _stop_authorization(self, row: sqlite3.Row) -> dict[str, Any]:
        # An armed stop is already durably authorized: only an emergency stop may
        # block its exit. Lease state (expired, stopped), the order budget and the
        # per-order volume cap never fence a protective exit (owner decision
        # 2026-09-20); the deterministic position checks at trigger time remain.
        if self._emergency_active(str(row["user_id"])):
            raise AutonomyError("ai_execution_stopped")
        # Its event/proposal context must not depend on a chat or monitor session
        # surviving retention cleanup. A rebuilt session stays on this service's
        # own account — never the store default.
        session = self.broker.get_session(str(row["session_id"]), str(row["user_id"]))
        if session is None or session.agent_id != "deterministic-stop":
            session = self.broker.create_session(
                "local", "deterministic-stop", str(row["user_id"]), trading_account_id=self.account_id)
        return {"authorizationId": row["authorization_id"], "sessionId": session.session_id}

    @staticmethod
    def _stop_order_status(order: dict[str, Any]) -> str:
        if order["submitPhase"] == "PREPARED":
            return "ARMED"
        display = order["displayStatus"]
        if display == "已成交":
            return "EXECUTED"
        if display == PARTIAL_ENDED:
            return "PARTIAL"
        if display in {"已撤销", "已拒绝", "已放弃"}:
            return "FAILED"
        if display in {"已受理", "部分成交"}:
            return "SUBMITTED"
        return "UNKNOWN"

    def _trail_stop(self, row: sqlite3.Row, latest: Decimal, trigger: Decimal) -> Decimal:
        """追踪止损 / 保本: move the armed stop toward the latest fresh price — only tighter, never
        looser; with both, the tighter one stands — and persist the best price since entry with it,
        so a restart resumes from the same stop. The trigger, the emergency fence and the protective
        order below are unchanged."""
        # A strategy's (and an order card's) stop keeps its state in strategy_metadata_json; the user's own direct
        # stop in trailing_json (2026-10-25), so its 原仓核对 still keys on strategy_metadata_json being empty.
        column = "strategy_metadata_json" if row["strategy_metadata_json"] else "trailing_json"
        metadata = json.loads(row[column])
        from .strategy_engine import breakeven_stop, trail_stop
        long = row["position_direction"] == "long"
        previous = Decimal(str(metadata.get("trailExtreme") or metadata["entryPrice"]))
        extreme = max(previous, latest) if long else min(previous, latest)
        stop = trigger
        if (metadata.get("trailing") or {}).get("mode") == "bar_extreme":
            tightened = self._bar_stop(str(row["symbol"]), metadata["trailing"], long=long,
                                       tick=metadata["priceTick"], current=stop)
            if tightened != stop:
                stop, metadata["trailActive"] = tightened, True
        elif metadata.get("trailing"):
            tightened = trail_stop(metadata["trailing"], long=long, entry_price=metadata["entryPrice"], extreme=extreme,
                                   price_tick=metadata["priceTick"], current_stop=stop,
                                   entry_atr=metadata.get("entryAtr"))
            if tightened is not None and tightened != stop:
                stop, metadata["trailActive"] = tightened, True
        if metadata.get("breakeven"):
            moved = breakeven_stop(metadata["breakeven"], long=long, entry_price=metadata["entryPrice"], extreme=extreme,
                                   price_tick=metadata["priceTick"], current_stop=stop,
                                   exit_atr=metadata.get("exitAtr"))
            if moved is not None and moved != stop:
                stop, metadata["breakevenActive"] = moved, True
        if extreme == previous and stop == trigger:
            return trigger
        metadata["trailExtreme"] = str(extreme)
        with self.store.lock, self.store.connection:
            cursor = self.store.connection.execute(
                f"""UPDATE ai_stop_orders SET trigger_price = ?, {column} = ?, changed_by = 'trailing', updated_at = ?
                WHERE id = ? AND status = 'ARMED' AND trigger_price = ?""",
                (str(stop), json.dumps(metadata), utc_now(), row["id"], row["trigger_price"]),
            )
        return stop if cursor.rowcount == 1 else trigger

    def _bar_stop(self, symbol: str, trailing: dict[str, Any], *, long: bool, tick: Any, current: Decimal) -> Decimal:
        """前 N 根 K 线追踪 (2026-10-25, the user's own stops only): the stop follows the last N completed bars' low
        (long) / high (short), only tighter. The level is re-read at most every 30 s (or once a bar, if shorter),
        not on every tick; an unreadable series keeps the stop where it is."""
        from .strategy_engine import round_stop
        timeframe, count = int(trailing.get("atrTimeframe") or 300), int(trailing["value"])
        key, now = (symbol, timeframe, count, long), time.monotonic()
        cached = self._bar_levels.get(key)
        if cached is None or now - cached[0] >= min(30, timeframe):
            cached = self._bar_levels[key] = (now, bar_extreme(self.data_provider, symbol, timeframe, count, long=long))
        if cached[1] is None:
            return current
        candidate = round_stop(cached[1], tick, long=long)
        return max(current, candidate) if long else min(current, candidate)

    def _protection_sync_failed(self, row: sqlite3.Row) -> bool:
        """动作锁内的成交同步失败围栏 (2026-10-10 #692)：普通保护触发与回撤停机出口共用——
        (user, symbol) 的迟到成交归属没落定（sync 失败）时零派发，不清错误，不把旧仓证据当
        新仓授权；行保持 ARMED 等下一轮同步恢复。"""
        if not row["strategy_metadata_json"]:
            return False
        blocked = self.sync_strategy_protection()
        return (str(row["user_id"]), str(row["symbol"])) in blocked

    async def _check_stop_locked(self, row: sqlite3.Row) -> None:
        if self._protection_sync_failed(row):
            return
        # Re-read after acquiring the lock: a queued tick may predate cancellation.
        row = self.store.connection.execute(
            "SELECT * FROM ai_stop_orders WHERE id = ?", (row["id"],)
        ).fetchone()
        if row is None or row["status"] != "ARMED":
            return
        if row["strategy_metadata_json"] and json.loads(row["strategy_metadata_json"]).get("disarmed"):
            return
        if not row["strategy_metadata_json"]:
            # 直接保护：本快照没核对过原仓就不触发 —— 不能用“现在又有持仓”代替原仓证明 (#528)。
            if await self._verify_direct_stop(row):
                return
            row = self.store.connection.execute(
                "SELECT * FROM ai_stop_orders WHERE id = ?", (row["id"],)
            ).fetchone()
            if row is None or row["status"] != "ARMED":
                return
        try:
            market = self.data_provider.market_snapshot("", str(row["symbol"]))
            quote_is_fresh(market, allow_fixture=False, now=self.simulation_orders.trading_now())
            latest = Decimal(str(market.get("price")))
            if not latest.is_finite() or latest <= 0:
                return
            # An empty trigger_price is a take-profit-only row: no stop level to compare.
            trigger = Decimal(str(row["trigger_price"])) if row["trigger_price"] else None
            protection = json.loads(row["strategy_metadata_json"] or row["trailing_json"] or "{}")
            if trigger is not None and (protection.get("trailing") or protection.get("breakeven")):
                trigger = self._trail_stop(row, latest, trigger)
            long = row["position_direction"] == "long"
            triggered = trigger is not None and (latest <= trigger if long else latest >= trigger)
            take = Decimal(str(row["take_profit_price"])) if row["take_profit_price"] else None
            take_triggered = take is not None and (latest >= take if long else latest <= take)
        except (KeyError, RuntimeError, InvalidOperation, SimulationOrderError, TypeError):
            return
        if not triggered and not take_triggered:
            return
        await self._fire_protective_close(
            row, reason=(f"保护性{'止损' if triggered else '止盈'}触发："
                         f"{trigger if triggered else row['take_profit_price']}"),
            kind_word="止损" if triggered else "止盈",
            trigger_price=trigger if triggered else row["take_profit_price"], latest=latest)

    def _settlement_retry_waiting(self, row: sqlite3.Row) -> bool:
        """The row's close was refused while the engine waits for the settlement price, and
        the last refusal is younger than the retry cadence: skip this quote (#750). The engine
        settles on a quote or its own query, so the next due scan simply tries again."""
        try:
            order = self.simulation_orders.get(str(row["close_client_order_id"]), user_id=str(row["user_id"]))
        except (KeyError, SimulationOrderError):
            return False
        return (order["submitPhase"] == "PREPARED" and order.get("lastReadError") == "paper_settlement_pending"
                and not _retry_due(order["updatedAt"]))

    def _refresh_undispatched_close(self, row: sqlite3.Row, client_order_id: str) -> bool:
        """保护平仓重派前的重新定价 (#750)，在 TRIGGERING 认领与执行许可之后、派发之前。
        只动确定未派发的单（PREPARED）；已受理、CALL_STARTED、UNKNOWN 不在这里改价或再发，
        交给原单回读（派发入口对非 PREPARED 直接拒绝）。价格按行上的价格意图取当前行情，
        并重验持仓桶与可平量（`reprice_protective_close`，同一 clientOrderId、同一事务）。
        今昨桶或手数已变：原请求身份不成立，放弃旧单并清掉行上的旧单身份（同一事务，中断后
        行按 528-C 回 ARMED 重建），返回 False 让调用方按新身份重建。"""
        user_id = str(row["user_id"])
        if self.simulation_orders.get(client_order_id, user_id=user_id)["submitPhase"] != "PREPARED":
            return True
        outcome = self.simulation_orders.reprice_protective_close(
            client_order_id, user_id=user_id, price_intent=str(row["price_intent"]),
            volume=int(row["volume"]), position_bucket=str(row["position_bucket"]),
        )
        if outcome != "replan":
            return True
        with self.store.transaction() as connection:
            order = self.simulation_orders.get(client_order_id, user_id=user_id, connection=connection)
            cursor = connection.execute(
                """UPDATE tqkq_sim_order_links SET submit_phase = 'ABANDONED', updated_at = ?
                WHERE client_order_id = ? AND user_id = ? AND submit_phase = 'PREPARED'""",
                (utc_now(), client_order_id, user_id))
            if cursor.rowcount != 1:
                raise SimulationOrderError("order_not_prepared")
            self.store.audit(str(order["proposalId"]), "protective-close-replan", {
                "stopId": row["id"], "previousClientOrderId": client_order_id,
                "volume": int(row["volume"]), "positionBucket": row["position_bucket"],
            }, connection=connection)
            connection.execute(
                """UPDATE ai_stop_orders SET close_proposal_id = NULL, close_client_order_id = NULL,
                updated_at = ? WHERE id = ? AND status = 'TRIGGERING'""", (utc_now(), row["id"]))
        return False

    async def _fire_protective_close(
        self, row: sqlite3.Row, *, reason: str, kind_word: str,
        trigger_price: Any = None, latest: Any = None,
    ) -> str:
        """The single guarded firing segment shared by every protective close (止损/止盈命中、
        回撤停机): the fill-sync failure fence and in-lock re-read for strategy rows (#692:
        both trigger paths converge here, not only the stop hit), exit-readiness, the
        emergency-only fence (`_stop_authorization`), the TRIGGERING handoff, dispatch
        (prepared or fresh) with close-identity recording, read-back with the direct-row
        remaining_volume reset, the ARMED-because-undispatched skip (#528) and the trigger
        notice. Returns the outcome: "fired" (close went out), "blocked" (a fence held it;
        the row stays ARMED with the reason on it) or "skipped" (the row was already in
        flight / undispatched retry).

        A close the engine refused while it waits for the settlement price is retried at the
        stop scan's retry cadence, not on every quote; every retry of a definitely-undispatched
        close re-prices it on the current quote by the row's own price intent first (#750)."""
        if row["close_client_order_id"] and self._settlement_retry_waiting(row):
            return "skipped"
        if row["strategy_metadata_json"]:
            if self._protection_sync_failed(row):
                return "blocked"
            # 同步通过后在锁内重读：触发判断到这里的行可能已被排队的事件改掉。
            row = self.store.connection.execute(
                "SELECT * FROM ai_stop_orders WHERE id = ?", (row["id"],)
            ).fetchone()
            if row is None or row["status"] != "ARMED":
                return "skipped"
        if row["strategy_metadata_json"] and not await self._strategy_exit_ready(row):
            return "blocked"
        # The notice goes to the session the protection was armed from; the row's session is
        # replaced by the deterministic-stop agent session in the TRIGGERING update below.
        original_session_id = str(row["session_id"])
        try:
            authorization = self._stop_authorization(row)
        except (AutonomyError, KeyError) as error:
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    "UPDATE ai_stop_orders SET last_error = ?, updated_at = ? WHERE id = ? AND status = 'ARMED'",
                    (str(error)[:160], utc_now(), row["id"]),
                )
            return "blocked"
        now = utc_now()
        with self.store.lock, self.store.connection:
            cursor = self.store.connection.execute(
                """UPDATE ai_stop_orders SET status = 'TRIGGERING', triggered_at = ?, updated_at = ?,
                authorization_id = ?, session_id = ?
                WHERE id = ? AND status = 'ARMED'""",
                (now, now, authorization["authorizationId"], authorization["sessionId"], row["id"]),
            )
        if cursor.rowcount != 1:
            return "skipped"
        close_side = "sell_close" if row["position_direction"] == "long" else "buy_close"
        try:
            def record_close_identity(proposal_id: str, client_order_id: str) -> None:
                with self.store.lock, self.store.connection:
                    self.store.connection.execute(
                        """UPDATE ai_stop_orders SET close_proposal_id = ?,
                        close_client_order_id = ?, updated_at = ?
                        WHERE id = ? AND status = 'TRIGGERING'""",
                        (proposal_id, client_order_id, utc_now(), row["id"]),
                    )

            close_id = str(row["close_client_order_id"] or "")
            if close_id and not self._refresh_undispatched_close(row, close_id):
                close_id = ""  # 今昨桶或手数变了：旧单已放弃，按新身份重建
            if close_id:
                result = await self._dispatch_prepared_order(
                    user_id=str(row["user_id"]),
                    authorization_id=authorization["authorizationId"],
                    client_order_id=close_id,
                    protective=True,
                )
            else:
                result = await self._create_and_dispatch(
                    user_id=str(row["user_id"]),
                    session_id=authorization["sessionId"],
                    authorization_id=authorization["authorizationId"],
                    symbol=str(row["symbol"]), side=close_side, volume=int(row["volume"]),
                    price_intent=str(row["price_intent"]), position_bucket=str(row["position_bucket"]),
                    reason=reason,
                    source="deterministic_stop", before_dispatch=record_close_identity,
                    protective=True,
                )
            order = result["simulationOrder"]
            status = self._stop_order_status(order)
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET status = ?, close_proposal_id = ?,
                    close_client_order_id = ?, last_error = ?, updated_at = ?,
                    remaining_volume = CASE WHEN ? = 'EXECUTED' AND strategy_metadata_json IS NULL
                        THEN 0 ELSE remaining_volume END
                    WHERE id = ?""",
                    (
                        status,
                        result["proposalId"],
                        result["clientOrderId"],
                        # 未派发的提交（挡在网关之前）不算触发过 (#528): 行回 ARMED、原因留在
                        # 行上，下一轮按当时行情重新定价后再派发 (#750)；只有真正发出去的单才
                        # 清掉原因并通知。
                        undispatched_reason(order) if status == "ARMED" else None,
                        utc_now(),
                        status,
                        row["id"],
                    ),
                )
            if status == "ARMED":
                return "skipped"  # 单没发出去：不喊“已发出平仓单”，等下一轮重试同一笔单
            # 触发通知 (2026-10-02): the session the order was armed from gets one line saying
            # the protection fired and the close went out; a live call says it too. A failure
            # here never touches the close order that already went out.
            await self._announce_stop_trigger(
                row=row, session_id=original_session_id, kind_word=kind_word,
                trigger_price=trigger_price, latest=latest,
                order_status=str(order.get("displayStatus") or "结果未知"),
                limit_price=(order.get("request") or {}).get("limitPrice"),
                client_order_id=str(result["clientOrderId"] or "") or None)
            return "fired"
        except Exception as error:
            code = str(error)
            linked = self.store.connection.execute(
                "SELECT close_client_order_id FROM ai_stop_orders WHERE id = ?", (row["id"],)
            ).fetchone()
            status = "ARMED" if row["strategy_metadata_json"] else "FAILED"
            if linked and linked["close_client_order_id"]:
                try:
                    order = self.simulation_orders.get(str(linked["close_client_order_id"]), user_id=str(row["user_id"]))
                except (KeyError, SimulationOrderError):
                    status = "UNKNOWN"
                else:
                    status = self._stop_order_status(order)
                    if status == "ARMED" and self._emergency_active(str(row["user_id"])):
                        status = "CANCELLED"
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET status = ?, last_error = ?,
                    updated_at = ?,
                    remaining_volume = CASE WHEN ? = 'EXECUTED' AND strategy_metadata_json IS NULL
                        THEN 0 ELSE remaining_volume END
                    WHERE id = ? AND status = 'TRIGGERING'""",
                    (status, code[:160], utc_now(), status, row["id"]),
                )
            return "blocked"

    async def trigger_drawdown_exits(
        self, *, user_id: str, strategy_id: str, reason_text: str,
    ) -> dict[str, Any]:
        """回撤停机触发 (2026-10-09): fire this strategy's armed protection rows as protective
        closes through the same guarded execution a stop hit takes — exit-readiness (its live
        orders reconciled, its entry order cancelled first), the emergency-only fence
        (`_stop_authorization`), the TRIGGERING handoff, dispatch and read-back — with the
        drawdown as the recorded reason. A row already in flight is left to its own
        reconciliation; an emergency stop fences the dispatch and keeps the row waiting."""
        async with self._action_lock:
            return await self._trigger_drawdown_exits_locked(
                user_id=user_id, strategy_id=strategy_id, reason_text=reason_text)

    async def _trigger_drawdown_exits_locked(
        self, *, user_id: str, strategy_id: str, reason_text: str,
    ) -> dict[str, Any]:
        rows = self.store.connection.execute(
            """SELECT * FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?
            AND remaining_volume > 0 AND status = 'ARMED'
            AND json_extract(strategy_metadata_json, '$.strategyId') = ?""",
            (user_id, self.account_id, strategy_id)).fetchall()
        fired = blocked = 0
        for row in rows:
            if json.loads(row["strategy_metadata_json"]).get("disarmed"):
                continue
            # 与止损/止盈命中共用同一段受护栏的执行（`_fire_protective_close`）：一行失败
            # 或被围栏拦下只计入 blocked，从不影响其余行。
            outcome = await self._fire_protective_close(
                row, reason=f"回撤停机触发：{reason_text}",
                kind_word="回撤停机", trigger_price=None, latest=None)
            if outcome == "fired":
                fired += 1
            elif outcome == "blocked":
                blocked += 1
        return {"fired": fired, "blocked": blocked}

    async def _reconcile_stop(self, row: sqlite3.Row, *, refresh: bool = True) -> None:
        async with self._action_lock:
            try:
                order = self.simulation_orders.get(
                    str(row["close_client_order_id"]), user_id=str(row["user_id"])
                )
                if refresh and order["submitPhase"] not in {"PREPARED", "ABANDONED"}:
                    order = await self.simulation_orders.query(
                        str(row["close_client_order_id"]), user_id=str(row["user_id"])
                    )
            except (KeyError, SimulationOrderError):
                return
            remote = str(order.get("remoteStatus") or "UNKNOWN")
            if order["submitPhase"] == "PREPARED":
                stopped = self._emergency_active(str(row["user_id"]))
                status = "CANCELLED" if stopped else "ARMED"
                error = undispatched_reason(order)
            else:
                # 528-E2: 与首次分类同一张表 —— "回报异常"/"已放弃"不再被兜底分支落成 SUBMITTED。
                status = self._stop_order_status(order)
                error = {"PARTIAL": "stop_close_partially_filled",
                         "FAILED": f"stop_close_{remote.lower()}",
                         "UNKNOWN": "stop_close_result_unknown"}.get(status)
            with self.store.lock, self.store.connection:
                self.store.connection.execute(
                    """UPDATE ai_stop_orders SET status = ?, last_error = ?, updated_at = ?,
                    remaining_volume = CASE WHEN ? = 'EXECUTED' AND strategy_metadata_json IS NULL
                        THEN 0 ELSE remaining_volume END
                    WHERE id = ? AND status IN ('SUBMITTED', 'UNKNOWN')""",
                    (status, error, utc_now(), status, row["id"]),
                )
