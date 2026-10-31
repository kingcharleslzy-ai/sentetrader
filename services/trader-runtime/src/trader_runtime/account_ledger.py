"""Background account ledger: fills and daily settlement, with no client online.

The push feed used to be the only writer of account_trades and account_daily,
and it only reads for sessions with an attached SSE reader — with nobody
watching, strategy fills stayed in the sidecar's current-day data and were lost
overnight. This loop owns those writes now; the feed went back to pushing only.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from time import monotonic
from typing import Any

from .data_providers import ReadOnlyDataProvider, provider_read_snapshot
from .instruments import foreign_display_name
from .order_backfill import backfill_order_fills
from .store import RuntimeStore
from .trading_costs import contract_multiplier, persist_account_fills

log = logging.getLogger("uvicorn.error.agent")

# The provider's own read failures. Anything else is a bug and surfaces through
# the loop's exception log instead of being mistaken for a dead sidecar.
LEDGER_READ_ERRORS = (RuntimeError, OSError, ValueError, KeyError, TypeError, sqlite3.Error)


class AccountLedger:
    """Book the account on the Runtime's own loop, whether or not a client is attached.

    `run_once` is awaited once at startup and then every `tick_seconds`, sooner
    when the sidecar produced a fresh snapshot (`snapshot_arrived`) or an order
    link changed (`wake`, thread-safe)."""

    def __init__(self, *, store: RuntimeStore, provider: ReadOnlyDataProvider,
                 tick_seconds: float = 30.0, min_interval: float = 5.0,
                 trading_account_id: str | None = None) -> None:
        self.store = store
        self.account_id = trading_account_id or store.default_trading_account_id
        self.provider = provider
        self.tick_seconds = tick_seconds
        self.min_interval = min_interval
        self._trade_keys: set[tuple[str, str | None]] = set()
        # 锁屏推送 · 成交与撤单 (2026-10-06): the fills and cancelled orders already seen (None until
        # the first read after a start, which only seeds them).
        self._push_seen: dict[str, set[str]] | None = None
        # #932 R2: a reminder whose transaction rolled back stays pending — out of `_push_seen`, so the next pass
        # finds it again (a fill past the age rule too) — from its first failure (ns) for at most the fill / cancel
        # window.
        self._pending: dict[tuple[str, str], int] = {}
        self.clock = time.time_ns   # the reminders' wall clock (ns); tests pass a fake one
        self._last_failure: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._closing = False

    async def run_once(self) -> dict[str, int]:
        """One accounting pass: current-day fills, order-link backfill, daily settlement."""
        try:
            result = await self._ledger_once()
        except LEDGER_READ_ERRORS as error:
            detail = str(error.args[0]).strip() if error.args and isinstance(error.args[0], str) else type(error).__name__
            if detail != self._last_failure:
                # A reconnecting provider fails every pass; one line per condition, not per tick.
                log.info("account_ledger_read_failed detail=%s", detail)
            self._last_failure = detail
            return {"fills": 0, "backfilled": 0, "daily": 0}
        self._last_failure = None
        return result

    async def _ledger_once(self) -> dict[str, int]:
        fills = 0
        async with provider_read_snapshot(self.provider):
            activity = self.provider.account_activity()
        await self._push_activity(activity)
        # The sidecar only reports the current trading day; keep every fill so the
        # trade review can pair across days. Written only when the fill set changes —
        # keyed on (tradeId, tradingDay) (#514-6): a fill first reported without its
        # trading day is written again once the day arrives (the upsert fills the column).
        trade_keys = {(str(t.get("tradeId") or ""), t.get("tradingDay")) for t in activity.get("trades") or []}
        if trade_keys != self._trade_keys:
            # persist_account_fills itself refuses fixture data; the count stays honest.
            if activity.get("dataStatus") != "simulated":
                fills = len(persist_account_fills(
                    self.store, activity, self._multiplier_for, trading_account_id=self.account_id,
                ))
            self._trade_keys.clear()
            self._trade_keys.update(trade_keys)
        backfilled = backfill_order_fills(self.store, self.provider, trading_account_id=self.account_id)
        account = self.provider.account_summary("ledger")
        daily = 0
        trading_day = account.get("tradingDay")
        if account.get("dataStatus") == "normal" and isinstance(trading_day, str) and trading_day:
            self.store.upsert_account_daily(trading_day, account, trading_account_id=self.account_id)
            daily = 1
        return {"fills": fills, "backfilled": backfilled, "daily": daily}

    async def _push_activity(self, activity: dict) -> None:
        """成交与撤单's one detector (2026-10-06; #932 the only one — the client and the voice call no longer
        judge fills themselves): this loop reads every account's activity whether or not a client is
        attached, so a fill or a cancel seen here for the first time becomes one reminder per account owner
        through the one entry — the 提醒中心 row, feed.reminder (the desktop notification), the line on a
        live call, the push; 成交与撤单's switches over every channel. The first read after a start still
        tells what happened since the reminder claims began (their claims keep it once across a restart or
        a slot switch). A fill or cancel counts as seen once its reminder was recorded, already told or
        switched off; one whose transaction failed is read again on the next pass (#932 R2) within its
        window. Never breaks the accounting pass."""
        try:
            from .web_push import CANCEL_RECENT_SECONDS, FILL_RECENT_SECONDS, activity_events
            now = self.clock()
            since = self._since_ns() if self._push_seen is None else None
            self._expire_pending(now)
            retrying = {key for (name, key) in self._pending if name == "trades"}
            self._push_seen, fills, cancels = activity_events(self._push_seen, activity, now_ns=now, since_ns=since,
                                                              retrying=retrying)
            if not (fills or cancels):
                return
            trade_ids = {trade_id for fill in fills for trade_id in fill["tradeIds"]}
            order_ids = {str(cancel["orderId"]) for cancel in cancels}
            try:
                failed_trades, failed_orders = await self._remind_activity(fills, cancels)
            except Exception:  # noqa: BLE001 — nothing of this read is known told: all of it stays pending
                log.debug("account_ledger_remind_failed", exc_info=True)
                failed_trades, failed_orders = trade_ids, order_ids
            self._keep_pending("trades", trade_ids, failed_trades, now, FILL_RECENT_SECONDS)
            self._keep_pending("cancelled", order_ids, failed_orders, now, CANCEL_RECENT_SECONDS)
        except Exception:  # noqa: BLE001
            log.debug("account_ledger_push_failed", exc_info=True)

    def _expire_pending(self, now: int) -> None:
        """Give up what failed longer ago than its window, whether or not this read still shows it."""
        from .web_push import CANCEL_RECENT_SECONDS, FILL_RECENT_SECONDS
        for (name, key), first in list(self._pending.items()):
            window = FILL_RECENT_SECONDS if name == "trades" else CANCEL_RECENT_SECONDS
            if now - first > window * 1_000_000_000:
                del self._pending[(name, key)]
                if self._push_seen is not None:
                    self._push_seen[name].add(key)
                log.info("account_ledger_reminder_given_up kind=%s", name)

    def _keep_pending(self, name: str, attempted: set[str], failed: set[str], now: int, window: int) -> None:
        """Un-see what failed (bounded by `window` seconds from its first failure); forget what went through."""
        seen = self._push_seen
        if seen is None:
            return
        for key in attempted - failed:
            self._pending.pop((name, key), None)
        for key in failed:
            first = self._pending.setdefault((name, key), now)
            if now - first <= window * 1_000_000_000:
                seen[name].discard(key)
            else:
                self._pending.pop((name, key), None)
                log.info("account_ledger_reminder_given_up kind=%s", name)

    def _since_ns(self) -> int | None:
        from .reminders import ns_of
        try:
            epoch = self.store.reminders_epoch()
            return ns_of(epoch) if epoch else None
        except (AttributeError, ValueError, sqlite3.Error):
            return None

    async def _name(self, symbol: str) -> str:
        hub = getattr(self.store, "push_hub", None)
        if hub is not None:
            try:
                return str(await hub.instrument_name(symbol))
            except Exception:  # noqa: BLE001
                pass
        return foreign_display_name(symbol) or symbol.split(".", 1)[-1]

    async def _remind_activity(self, fills: list[dict], cancels: list[dict]) -> tuple[set[str], set[str]]:
        """One reminder per owner and event; returns the trade ids and order ids whose delivery failed (read
        again next pass), never the ones already told or switched off."""
        from .account_reminders import account_name
        from .conversation_cards import SIDE_LABELS, price_text
        from .reminders import ReminderSpec, deliver_via_store
        from .strategy_workbench import source_label
        from .web_push import cancel_detail, cancel_message, fill_message
        owners = self.store.account_owner_ids(self.account_id)
        sources = {user_id: self._order_sources(user_id) for user_id in owners} if fills else {}
        failed_trades: set[str] = set()
        failed_orders: set[str] = set()

        def titled(title: str, user_id: str) -> str:
            # Two accounts' 成交 / 撤单 rows read apart in one 提醒中心 (#932 P2), like 风险度 and 收盘提醒.
            account = account_name(self.store, user_id, self.account_id)
            return f"{title} · {account}" if account else title

        async def sent(spec: ReminderSpec) -> bool:
            """False only when nothing was recorded and it may be tried again (#932 R2)."""
            try:
                delivery = await deliver_via_store(self.store, spec)
            except Exception:  # noqa: BLE001
                log.debug("account_ledger_reminder_failed kind=%s", spec.kind, exc_info=True)
                return False
            return delivery.outcome != "failed"

        for fill in fills:
            name = await self._name(fill["symbol"])
            side = SIDE_LABELS.get((str(fill["direction"] or "").lower(), str(fill["offset"] or "").upper()), "成交")
            order_id = str(fill["orderId"])

            def content(batch: list[dict], origin: str | None, name: str = name, side: str = side,
                        order_id: str = order_id, fill: dict = fill) -> dict[str, Any]:
                """What a 成交 reminder says — record, feed, call and push alike — for exactly these fills."""
                volume, price = sum(int(trade["volume"]) for trade in batch), batch[-1]["price"]
                body = f"{side} {volume} 手 @ {price_text(price)}" + (f" · {origin}" if origin else "")
                return {"dedupe_key": f"{self.account_id}:{order_id}:{','.join(sorted(t['id'] for t in batch))}",
                        "body": body, "speech": f"{name} {body}，已成交。",
                        "push": fill_message(name=name, direction=fill["direction"], offset=fill["offset"],
                                             volume=volume, price=price, order_id=order_id)}

            for user_id in owners:
                # 稳定身份 (#932 P2): each fill is told once per user — a restart or the other slot regrouping the
                # same fills into another set (t1 then t1+t2) tells only the ones no reminder told yet. This read
                # only spares a transaction; the claim inside it decides what is told (#932 R1).
                told = self.store.told_fill_trades(user_id=user_id, account_id=self.account_id, order_id=order_id)
                trades = [trade for trade in fill.get("trades") or [] if trade["id"] not in told]
                if not trades:
                    continue
                # 成交归属 (#932): a strategy's or the AI's own order says so on its fill line — the
                # 执行卡 lands in the strategy's own conversation, this line wherever the user is.
                origin = source_label(sources[user_id].get(order_id))

                def claim(connection: Any, user_id: str = user_id, trades: list[dict] = trades,
                          origin: str | None = origin, order_id: str = order_id, content: Any = content,
                          ) -> dict[str, Any] | None:
                    # The fills this transaction claimed — not the ones read before it — make the reminder (#932 R1).
                    claimed = set(self.store.claim_fill_trades(
                        connection, user_id=user_id, account_id=self.account_id, order_id=order_id,
                        trade_ids=[trade["id"] for trade in trades]))
                    batch = [trade for trade in trades if trade["id"] in claimed]
                    return content(batch, origin) if batch else None

                if not await sent(ReminderSpec(
                        kind="fill", user_id=user_id, account_id=self.account_id,
                        title=titled(f"成交 · {name}", user_id), symbol=fill["symbol"], name=name,
                        target={"type": "orders", "symbol": fill["symbol"]}, voice_kind="fill", voice_key=order_id,
                        claim=claim, **content(trades, origin))):
                    failed_trades.update(fill["tradeIds"])
        for cancel in cancels:
            name = await self._name(cancel["symbol"])
            side = SIDE_LABELS.get((str(cancel["direction"] or "").lower(), str(cancel["offset"] or "").upper()), "委托")
            total = int(cancel.get("total") or cancel["volume"])
            body = f"{side} {max(total, cancel['volume'])} 手，" + cancel_detail(
                left=cancel["volume"], total=total, reason=str(cancel.get("reason") or ""))
            for user_id in owners:
                if not await sent(ReminderSpec(
                        kind="cancel", user_id=user_id, account_id=self.account_id,
                        dedupe_key=f"{self.account_id}:{cancel['orderId']}", title=titled(f"已撤单 · {name}", user_id),
                        body=body, symbol=cancel["symbol"], name=name, target={"type": "orders", "symbol": cancel["symbol"]},
                        speech=f"{name} {body}。", voice_kind="cancel", voice_key=cancel["orderId"],
                        push=cancel_message(name=name, direction=cancel["direction"], offset=cancel["offset"],
                                            volume=cancel["volume"], reason=cancel["reason"],
                                            order_id=cancel["orderId"], total=total))):
                    failed_orders.add(str(cancel["orderId"]))
        return failed_trades, failed_orders

    def _order_sources(self, user_id: str) -> dict[str, dict]:
        from .strategy_workbench import order_sources
        try:
            return order_sources(self.store, user_id)
        except (sqlite3.Error, ValueError, TypeError):
            return {}

    def _multiplier_for(self, symbol: str) -> float | None:
        return contract_multiplier(self.provider, symbol)

    # -- lifecycle (the RuntimeFeed pattern) ---------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closing = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def wake(self) -> None:
        """Thread-safe wake for services that run on worker threads."""
        loop = self._loop
        if loop is None:
            self._wake.set()
        elif not loop.is_closed():
            loop.call_soon_threadsafe(self._wake.set)

    def snapshot_arrived(self) -> None:
        """The provider refreshed its snapshot (loop thread only): an accounting pass is due."""
        self._wake.set()

    async def _run(self) -> None:
        while not self._closing:
            started = monotonic()
            try:
                await self.run_once()
            except Exception:
                log.exception("account_ledger_run_failed")
            # A burst of wakes (a snapshot plus order changes) still means at most
            # one pass per min_interval.
            remaining = self.min_interval - (monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
