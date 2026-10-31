"""The unfilled-order watchdog (09-21): a confirmed simulation limit order that sits on the book
gets looked after, whoever confirmed it — the voice, a click on the card or an external agent.

A resting limit order is the common outcome on the thin night-session contracts (the 09-21 call:
棉纱2611 买开 1 手 22035 sat ALIVE for minutes with no feedback but the position screen). The watch
polls each confirmed order every POLL_SECONDS and reminds twice — at 15 s and again at 60 s — in the
conversation the card was confirmed in, and through the voice when a call is online. "追单" / "撤单"
are the voice's two commands on the watched order (voice_realtime); both keep the TQKQ_SIM_THIN_V1
contract: this module only ever QUERIES, a cancel goes through the normal cancel route once, and an
order whose outcome reads 结果未知 is queried on (never reminded, never re-sent) until it settles or the
30-minute cap; a quarantined report (身份异常 / 回报异常) ends the watch (#932 F3).

The same loop carries out a confirmed 合并卡's second step (第四批, card_protection) and is the 目标持仓 relay (09-22): a plan whose current leg's orders all filled moves on.
A crossing-zero 目标持仓 / 反手 plan's opening leg is authorized by the user's first-leg confirmation (反手确认一次,
2026-10-24): the relay makes its card and confirms it through the one confirm service, so every send-time check runs
again. A 一句多单 relay's next leg is still a confirmation card. A plan that stalled 30 minutes without progress is
abandoned with one honest line. UNKNOWN or a partial fill never advances a plan.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .conversation_cards import ExecutionCardTracker, ask_action, reminder_card, ui_action
from .order_intent import from_trade_order as intent_from_trade_order
from .order_planner import CANDIDATE_ISSUE_LABELS, Cards, outcome_line
from .simulation_orders import PARTIAL_ENDED, order_is_working

log = logging.getLogger("uvicorn.error.orderwatch")

POLL_SECONDS = 2.0
FIRST_REMIND_SECONDS = 15.0
SECOND_REMIND_SECONDS = 60.0
# No reminders after the second one, and no polling past this: a wedged gateway must not be
# queried for the rest of the night (the order stays readable from the positions/orders screens).
WATCH_HARD_CAP_SECONDS = 1800.0
# A 目标持仓 plan that stays awaiting a confirmation or a fill this long is abandoned (its leg's
# orders, UNKNOWN included, stop mattering after this).
PLAN_STALL_SECONDS = 1800.0

# One order's side and offset, short (买开 / 卖平今 / 卖平昨) and spoken (买入开仓 / 卖出平今仓): the same reading
# as the client's `orderLegLabel` — 上期所 / 能源中心 split 平今 (CLOSETODAY) from 平昨 (CLOSE), elsewhere CLOSE is
# just 平 (#932 F3: a 平今 order used to be shown and spoken as "SELL").
_OFFSET_WORDS = {"OPEN": ("开", "开仓"), "CLOSETODAY": ("平今", "平今仓"), "CLOSE": ("平", "平仓")}
_YESTERDAY_EXCHANGES = frozenset({"SHFE", "INE"})


def _side_words(direction: str, offset: str, symbol: str) -> tuple[str, str] | None:
    side = {"buy": ("买", "买入"), "sell": ("卖", "卖出")}.get(str(direction).lower())
    words = _OFFSET_WORDS.get(str(offset).upper())
    if side is None or words is None:
        return None
    if str(offset).upper() == "CLOSE" and str(symbol).split(".")[0].upper() in _YESTERDAY_EXCHANGES:
        words = ("平昨", "平昨仓")
    return side[0] + words[0], side[1] + words[1]


# The watch ends when the order reads any of these: filled, ended after a partial fill, cancelled,
# refused, abandoned, never sent, or a quarantined report (身份异常 / 回报异常: simulation_orders never lets
# a later normal report dismiss them, so reading on would settle nothing). 结果未知 is not an end (#932 F3):
# a cancel read back mid-flight is UNKNOWN for a moment, so the watch keeps querying it — only ever
# queried, never re-sent (TQKQ_SIM_THIN_V1) — until it settles or WATCH_HARD_CAP_SECONDS.
TERMINAL_STATUSES = frozenset({"已成交", PARTIAL_ENDED, "已撤销", "已拒绝", "已放弃", "待发送", "身份异常", "回报异常"})
# What a plan's leg reads when it cannot complete (anything terminal that is not 已成交).
_PLAN_FAILURE_WORDS = {"已撤销": "已撤销", "已拒绝": "已拒绝", "身份异常": "回报身份异常", "回报异常": "回报异常",
                       "已放弃": "已放弃", "待发送": "没有发出"}
# The unknown family (close_batch_execution's UNKNOWN_STATUSES): the outcome is simply not known.
_PLAN_UNKNOWN_STATUSES = frozenset({"结果未知", "身份异常", "回报异常"})


def _price_text(price: Any) -> str:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return str(price)
    return str(int(value)) if value == int(value) else f"{value:g}"


@dataclass
class WatchedOrder:
    client_order_id: str
    account_id: str
    user_id: str
    session_id: str
    symbol: str
    name: str
    direction: str  # buy / sell
    offset: str  # OPEN / CLOSE
    volume: int
    limit_price: Any
    confirmed_at: float
    reminded_first: bool = False
    reminded_second: bool = False
    # What the watch last read back (2026-10-08): a changed displayStatus / filledVolume is pushed
    # to the confirming session as one transient feed.order, so a confirmed card's rows follow the
    # order live — the same fact a refresh would read, never a re-send of the order.
    display_status: str = ""
    filled_volume: int = 0
    # A close batch's children join for the fact push only: the unfilled reminders stay the
    # single-order path's own (09-21 — a batch would say it N times over).
    remind: bool = True

    @property
    def side_word(self) -> str:
        words = _side_words(self.direction, self.offset, self.symbol)
        return words[0] if words else str(self.direction)

    @property
    def side_phrase(self) -> str:
        words = _side_words(self.direction, self.offset, self.symbol)
        return words[1] if words else f"{self.direction} {self.offset}"

    @property
    def label(self) -> str:
        price = f" {_price_text(self.limit_price)}" if self.limit_price is not None else ""
        return f"{self.name or self.symbol} {self.side_word} {self.volume} 手{price}".strip()


class OrderWatch:
    """One background loop for every confirmed-but-unfilled simulation order of this boot."""

    def __init__(self, app) -> None:
        self.app = app
        self._orders: dict[str, WatchedOrder] = {}
        self._task: asyncio.Task | None = None
        # 执行回报卡 (2026-10-04): the same loop re-reads each execution card's order and republishes
        # the card when its state changes; producers find the tracker on the broker.
        # 合并卡 (第四批): the same loop carries out a confirmed close card's second step once its
        # close has finished (card_protection).
        from .card_protection import CardProtectionRelay
        self.card_protection = CardProtectionRelay(app)
        broker = getattr(app.state, "broker", None)
        self.execution_cards = (ExecutionCardTracker(broker, store=getattr(app.state, "store", None))
                                if broker is not None else None)
        if broker is not None:
            broker.execution_cards = self.execution_cards

    # -- registration -------------------------------------------------------

    def register(self, *, user_id: str, account_id: str, session_id: str, order: dict[str, Any],
                 name: str = "", remind: bool = True) -> None:
        """Start watching one confirmed order (`order` is its simulation-order view). Registering the
        same client order again restarts its clock — the watch holds one entry per order."""
        request = order.get("request") or {}
        client_order_id = str(order.get("clientOrderId") or "")
        if not client_order_id:
            return
        self._orders[client_order_id] = WatchedOrder(
            client_order_id=client_order_id, account_id=str(account_id), user_id=user_id,
            session_id=str(session_id or ""), symbol=str(request.get("symbol") or ""),
            name=str(name or request.get("name") or ""), direction=str(request.get("direction") or ""),
            offset=str(request.get("offset") or "OPEN"), volume=int(request.get("volume") or 1),
            limit_price=request.get("limitPrice"), confirmed_at=time.monotonic(),
            display_status=str(order.get("displayStatus") or ""),
            filled_volume=int(order.get("filledVolume") or 0), remind=remind)
        log.info("order watch: %s %s registered", client_order_id, self._orders[client_order_id].label)
        self._save(self._orders[client_order_id], confirmed_at=datetime.now(UTC).isoformat())

    def _save(self, watched: WatchedOrder, *, confirmed_at: str) -> None:
        """落库 (#932): a restart or the other blue-green slot picks the order up again from this row."""
        store = getattr(self.app.state, "store", None)
        if store is None:
            return
        try:
            store.save_order_watch({
                "clientOrderId": watched.client_order_id, "userId": watched.user_id, "accountId": watched.account_id,
                "sessionId": watched.session_id, "symbol": watched.symbol, "name": watched.name,
                "direction": watched.direction, "offset": watched.offset, "volume": watched.volume,
                "limitPrice": watched.limit_price, "remind": watched.remind,
                "displayStatus": watched.display_status, "filledVolume": watched.filled_volume,
                "confirmedAt": confirmed_at})
        except Exception as error:  # noqa: BLE001 — this process still watches it from memory
            log.debug("order watch %s not saved: %s", watched.client_order_id, error)

    def forget(self, client_order_id: str) -> None:
        self._orders.pop(str(client_order_id), None)
        store = getattr(self.app.state, "store", None)
        if store is not None:
            try:
                store.delete_order_watch(str(client_order_id))
            except Exception as error:  # noqa: BLE001 — the row expires with the watch's cap
                log.debug("order watch %s not deleted: %s", client_order_id, error)

    def restore(self) -> int:
        """The watched orders confirmed in the last WATCH_HARD_CAP_SECONDS, from their rows (a restart, the
        other slot's confirms): each keeps its own clock (the time since its confirmation), and the 15 s /
        60 s reminders it already had are recognised by their claims, never sent twice."""
        store = getattr(self.app.state, "store", None)
        if store is None:
            return 0
        now = datetime.now(UTC)
        since = (now - timedelta(seconds=WATCH_HARD_CAP_SECONDS)).isoformat()
        restored = 0
        try:
            entries = store.unfilled_watch_entries(since=since)
        except Exception as error:  # noqa: BLE001
            log.debug("order watch rows unreadable: %s", error)
            return 0
        for entry in entries:
            client_order_id = str(entry.get("clientOrderId") or "")
            if not client_order_id or client_order_id in self._orders:
                continue
            try:
                age = (now - datetime.fromisoformat(str(entry["confirmedAt"]))).total_seconds()
                self._orders[client_order_id] = WatchedOrder(
                    client_order_id=client_order_id, account_id=str(entry["accountId"]), user_id=str(entry["userId"]),
                    session_id=str(entry.get("sessionId") or ""), symbol=str(entry.get("symbol") or ""),
                    name=str(entry.get("name") or ""), direction=str(entry.get("direction") or ""),
                    offset=str(entry.get("offset") or "OPEN"), volume=int(entry.get("volume") or 1),
                    limit_price=entry.get("limitPrice"), confirmed_at=time.monotonic() - max(0.0, age),
                    display_status=str(entry.get("displayStatus") or ""),
                    filled_volume=int(entry.get("filledVolume") or 0), remind=bool(entry.get("remind", True)))
                restored += 1
            except (KeyError, TypeError, ValueError) as error:
                log.debug("order watch row %s unusable: %s", client_order_id, error)
        if restored:
            log.info("order watch: %d order(s) restored", restored)
        return restored

    def read_on(self, client_order_id: str) -> None:
        """A cancel on the watched order came back 结果未知 (the voice's 撤单 / 追单): keep reading it until it
        settles instead of dropping it (#932 F3) — not a target for another 撤单 / 追单 meanwhile, since a
        second cancel on an unknown outcome is what TQKQ_SIM_THIN_V1 forbids."""
        watched = self._orders.get(str(client_order_id))
        if watched is not None:
            watched.display_status = "结果未知"
            # Saved too, with its original confirmation time: a restored watch must not offer it as a target again.
            age = max(0.0, time.monotonic() - watched.confirmed_at)
            self._save(watched, confirmed_at=(datetime.now(UTC) - timedelta(seconds=age)).isoformat())

    async def settle(self, client_order_id: str, view: dict[str, Any] | None) -> None:
        """A cancel the voice sent on an order came back with `view`: the order's 挂单未成交 rows follow that read-back
        (#932 T020: 已撤 instead of 仍在排队), then 结果未知 is read on (`read_on`) and anything else ends the watch."""
        status = str((view or {}).get("displayStatus") or "")
        watched = self._orders.get(str(client_order_id))
        if watched is not None and view and status and status != "结果未知":
            watched.display_status = status
            watched.filled_volume = int(view.get("filledVolume") or watched.filled_volume)
            await self._follow_reminders(watched, view)
        (self.read_on if status == "结果未知" else self.forget)(client_order_id)

    def target_for(self, user_id: str) -> dict[str, Any] | None:
        """The order "追单" / "撤单" act on: the user's most recently confirmed watched order whose outcome is
        known (one read 结果未知 is only queried until it settles)."""
        orders = [order for order in self._orders.values()
                  if order.user_id == user_id and order.display_status != "结果未知"]
        if not orders:
            return None
        order = max(orders, key=lambda item: item.confirmed_at)
        return {"clientOrderId": order.client_order_id, "accountId": order.account_id, "symbol": order.symbol,
                "name": order.name, "label": order.label, "sideWord": order.side_word,
                "sidePhrase": order.side_phrase, "volume": order.volume}

    # -- loop ---------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.restore()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            for client_order_id in list(self._orders):
                try:
                    await self._tick(client_order_id)
                except Exception as error:  # one unreadable order never stops the others
                    log.debug("order watch tick %s failed: %s", client_order_id, error)
            try:
                await self._sweep_target_plans()
            except Exception as error:
                log.debug("target plan sweep failed: %s", error)
            try:
                await self.card_protection.advance_steps()
            except Exception as error:
                log.debug("card protection sweep failed: %s", error)
            if self.execution_cards is not None:
                try:
                    await self.execution_cards.tick()
                except Exception as error:
                    log.debug("execution card tick failed: %s", error)

    def _services(self, account_id: str):
        registry = getattr(self.app.state, "accounts", None)
        services = registry.get(account_id) if registry is not None else None
        simulation_orders = getattr(services, "simulation_orders", None) if services is not None else None
        return services, simulation_orders

    async def _tick(self, client_order_id: str) -> None:
        watched = self._orders.get(client_order_id)
        if watched is None:
            return
        if time.monotonic() - watched.confirmed_at > WATCH_HARD_CAP_SECONDS:
            self.forget(client_order_id)
            return
        services, simulation_orders = self._services(watched.account_id)
        if simulation_orders is None or not simulation_orders.enabled:
            self.forget(client_order_id)
            return
        view = await simulation_orders.query(client_order_id, user_id=watched.user_id)
        status = str(view.get("displayStatus") or "结果未知")
        filled = int(view.get("filledVolume") or 0)
        if status != watched.display_status or filled != watched.filled_volume:
            watched.display_status, watched.filled_volume = status, filled
            await self._publish_order_fact(watched, view)
            await self._follow_reminders(watched, view)
        if status in TERMINAL_STATUSES:
            self.forget(client_order_id)
            log.info("order watch: %s ended with %s", client_order_id, status)
            return
        if status == "结果未知":
            return  # not known to be unfilled: queried again next round, never reminded as 挂着没成交
        report_order = (view.get("report") or {}).get("order") or {}
        if int(report_order.get("volumeLeft", watched.volume)) <= 0:
            self.forget(client_order_id)
            return
        waited = time.monotonic() - watched.confirmed_at
        if not watched.remind:
            return  # a batch child: followed for its fact only, never reminded
        if waited >= FIRST_REMIND_SECONDS and not watched.reminded_first:
            watched.reminded_first = True
            await self._remind(watched, waited, late=False, services=services)
        elif waited >= SECOND_REMIND_SECONDS and not watched.reminded_second:
            watched.reminded_second = True
            await self._remind(watched, waited, late=True, services=services)

    async def _publish_order_fact(self, watched: WatchedOrder, view: dict[str, Any]) -> None:
        """One transient feed.order to the session the card was confirmed in: the read-back the
        watch just saw (受理 → 部分成交 / 成交 / 撤销 / 拒绝 / 结果未知), as the same order view the
        list endpoint serves. Pushed only — the watch still never sends anything itself."""
        if not watched.session_id:
            return
        try:
            await self.app.state.broker.publish_transient(
                watched.session_id, kind="result", name="feed.order",
                payload={**view, "accountId": watched.account_id})
        except Exception as error:
            log.debug("order fact %s not published: %s", watched.client_order_id, error)

    @staticmethod
    def _reminder_turn(watched: WatchedOrder, late: bool) -> str:
        return f"orderwatch-{watched.client_order_id}-{'late' if late else 'first'}"

    async def _follow_reminders(self, watched: WatchedOrder, view: dict[str, Any]) -> None:
        """The 挂单未成交 rows of this order follow its read-back (#932: 仍在排队 0/1 → 已成交 / 已撤)."""
        from .reminders import order_state, update_state
        state = order_state(watched.display_status, filled=watched.filled_volume, volume=watched.volume,
                            price=watched.limit_price)
        for late in (False, True):
            await update_state(self.app.state.store, self.app.state.broker,
                               card_id=f"reminder-{self._reminder_turn(watched, late)}", state=state)

    async def _remind(self, watched: WatchedOrder, waited: float, *, late: bool, services=None) -> None:
        """One unfilled reminder through the one entry (#932): 挂单未成交's switch over every channel, once per
        order and step (15 s / 60 s) whatever the process or restart, in the conversation the card was
        confirmed in, the call that is online now (its line named by the order, so a second order's reminder
        is not swallowed), the desktop notification and the push."""
        from .reminders import ReminderSpec, deliver, order_state
        from .web_push import PushMessage, deep_link
        prices = ""
        bid = ask = None
        services = services or self._services(watched.account_id)[0]
        if services is not None:
            try:
                market = services.provider.market_snapshot(f"orderwatch-{watched.client_order_id}", watched.symbol)
            except Exception:
                market = {}
            bid, ask = market.get("bidPrice1"), market.get("askPrice1")
            if bid is not None and ask is not None:
                prices = f"，现在买一 {_price_text(bid)} / 卖一 {_price_text(ask)}"
        text = (f"{watched.label} 挂了 {int(waited)} 秒没成交{prices}。"
                f"说“追单”按对手价改单，说“撤单”撤掉，不说就继续等")
        # The text plus the reminder card (same turn; the card's actions ask the AI or open the orders tab,
        # never an order).
        turn_id = self._reminder_turn(watched, late)
        at = datetime.now(UTC).isoformat()
        summary = f"{watched.label} 挂了 {int(waited)} 秒没成交"
        card = reminder_card(
            turn_id=turn_id, kind="order_unfilled", title="挂单未成交", at=at, symbol=watched.symbol,
            summary=summary,
            figures=[("挂单价", _price_text(watched.limit_price) if watched.limit_price is not None else None),
                     ("已挂", f"{int(waited)} 秒", "warning"),
                     ("买一", _price_text(bid) if bid is not None else None),
                     ("卖一", _price_text(ask) if ask is not None else None)],
            actions=[ask_action("问 AI 怎么办", f"{watched.label} 挂了 {int(waited)} 秒没成交，现在怎么办？"),
                     ui_action("打开委托", {"positionsTab": "orders"})])
        name = watched.name or watched.symbol
        await deliver(self.app, ReminderSpec(
            kind="order_unfilled", user_id=watched.user_id, account_id=watched.account_id,
            dedupe_key=f"{watched.client_order_id}:{'late' if late else 'first'}", title="挂单未成交",
            body=summary, symbol=watched.symbol, name=watched.name, at=at, message=text, turn_id=turn_id,
            cards=[("card.reminder", card)], session_id=watched.session_id or None, auto_landing=False,
            target={"type": "orders", "cardId": card["cardId"], **({"sessionId": watched.session_id}
                                                                   if watched.session_id else {})},
            state=order_state(watched.display_status or "已受理", filled=watched.filled_volume,
                              volume=watched.volume, price=watched.limit_price),
            speech=text, voice_kind="order_unfilled_late" if late else "order_unfilled",
            voice_key=watched.client_order_id,
            push=PushMessage(kind="order_unfilled", title=f"挂单未成交 · {name}", body=summary,
                             short=f"{name}有挂单没成交", tag=f"orderwatch-{watched.client_order_id}",
                             url=deep_link("mobilePage", "positions"))))

    # -- 目标持仓 relay (09-22) ------------------------------------------------
    # A plan's confirmed card links back to its row; the loop above then watches the leg's orders
    # and advances the plan when they fill. A 目标持仓 / 反手 plan's next leg is confirmed by the relay on the
    # strength of the first leg's confirmation (once, idempotency-keyed per plan and leg); a 一句多单 plan's next
    # leg is another confirmation card. The relay never re-sends anything.

    async def link_confirmed_plan(self, result: dict[str, Any], *, user_id: str, proposal_id: str) -> None:
        """Called from the confirm route: when the confirmed card is a 目标持仓 plan's current leg,
        the plan moves to awaiting_fill and its orders (every child of a batch) join the watch."""
        store = getattr(self.app.state, "store", None)
        if store is None:
            return
        plan_row = store.plan_by_proposal(proposal_id, user_id=user_id)
        if plan_row is None:
            return
        orders: list[dict[str, Any]] = []
        if isinstance(result.get("simulationOrder"), dict):
            orders.append(result["simulationOrder"])
        elif isinstance(result.get("simulationOrders"), list):
            orders.extend(item for item in result["simulationOrders"] if isinstance(item, dict))
        client_ids: list[str] = []
        for order in orders:
            client_id = str(order.get("clientOrderId") or "")
            if not client_id:
                continue
            client_ids.append(client_id)
            self.register(user_id=user_id, account_id=str(plan_row["trading_account_id"]),
                          session_id=str(plan_row["leg"].get("sessionId") or ""), order=order,
                          name=str(order.get("name") or ""))
        if not client_ids:
            # A gateway-less confirm leaves no order to watch; the stall sweep ends the plan.
            return
        store.update_target_plan(plan_row["id"], status="awaiting_fill",
                                 current_client_order_ids=client_ids)
        log.info("target plan %s leg %s awaiting fill: %s", plan_row["id"],
                 plan_row["leg_index"] + 1, ",".join(client_ids))

    async def _sweep_target_plans(self) -> None:
        """Both relay clocks in one pass: advance plans whose current leg filled, abandon the ones
        that stalled past PLAN_STALL_SECONDS."""
        store = getattr(self.app.state, "store", None)
        if store is None:
            return
        for plan_row in store.awaiting_fill_plans():
            try:
                await self._advance_target_plan(plan_row)
            except Exception as error:  # one broken plan never stops the sweep
                log.debug("target plan %s advance failed: %s", plan_row["id"], error)
        cutoff = (datetime.now(UTC) - timedelta(seconds=PLAN_STALL_SECONDS)).isoformat()
        for plan_row in store.stale_awaiting_plans(older_than_iso=cutoff):
            store.update_target_plan(plan_row["id"], status="abandoned")
            await self._plan_notice(plan_row, (
                f"{plan_row['symbol']} 的目标持仓计划 30 分钟没有进展，已自动取消；要继续就重新发起。"))

    async def _advance_target_plan(self, plan_row: dict[str, Any]) -> None:
        """The current leg's orders, one fact each: all filled → next leg's card or done; any
        order still working (还在挂, whatever its Chinese projection says) → wait, the stall
        sweep ends it; UNKNOWN → wait; only ended non-fills → abandoned (#518)."""
        store = self.app.state.store
        _, simulation_orders = self._services(str(plan_row["trading_account_id"]))
        if simulation_orders is None or not simulation_orders.enabled:
            return
        views: list[dict[str, Any]] = []
        for client_id in plan_row["currentClientOrderIds"]:
            try:
                views.append(await simulation_orders.query(
                    str(client_id), user_id=str(plan_row["user_id"])))
            except Exception:
                return  # unreadable now: the next sweep decides, nothing is guessed
        statuses = [str(view.get("displayStatus") or "结果未知") for view in views]
        if not statuses:
            return  # nothing to read: the stall sweep decides
        if any(status in _PLAN_UNKNOWN_STATUSES for status in statuses):
            return  # UNKNOWN never advances a plan (TQKQ_SIM_THIN_V1); the stall sweep ends it
        if any(order_is_working(view) for view in views):
            return  # 还有单挂在场上（含部分成交未结束）: 继续等，30 分钟停滞清扫收尾
        if not all(status == "已成交" for status in statuses):
            failures = []
            for view, status in zip(views, statuses):
                if status == "已成交":
                    continue
                filled = int(view.get("filledVolume") or 0)
                # 部分成交后结束说清成交了多少手；其余结束形态沿用各自的词。
                failures.append(
                    f"部分成交 {filled} 手后已结束" if filled else _PLAN_FAILURE_WORDS.get(status, status))
            ended = store.update_target_plan(plan_row["id"], status="abandoned",
                                             expect=("awaiting_fill", int(plan_row["leg_index"])))
            if ended is not None:  # a plan superseded mid-query is not told about again
                await self._plan_notice(plan_row, (
                    f"{plan_row['symbol']} 目标持仓的第 {plan_row['leg_index'] + 1} 段{failures[0]}，"
                    f"目标持仓没完成；请先在委托里确认原单状态。"))
            return
        leg_index = int(plan_row["leg_index"])
        plan = plan_row["leg"].get("plan") or {}
        legs = plan.get("legs") or []
        if leg_index >= len(legs) - 1:
            ended = store.update_target_plan(plan_row["id"], status="done",
                                             expect=("awaiting_fill", leg_index))
            if ended is not None:
                await self._plan_notice(plan_row, f"{plan.get('summary') or plan_row['symbol']}，目标持仓完成。")
            return
        # The next leg (a reverse plan's opening): status first, then the card — a crash between
        # the two leaves a plan awaiting a card it no longer has, which the stall sweep ends.
        # The write is conditional (#518): a plan superseded (or ended) while these order queries
        # were in flight must not be revived into awaiting_confirm to issue a second card.
        # A crash after this write and before the next leg is confirmed leaves the plan awaiting_confirm, which this
        # sweep never advances again: nothing is sent twice, and the stall sweep ends it.
        advanced = store.update_target_plan(
            plan_row["id"], status="awaiting_confirm", leg_index=leg_index + 1,
            current_proposal_id=None, current_client_order_ids=[],
            expect=("awaiting_fill", leg_index))
        if advanced is None:
            return
        await self._issue_next_leg_card({**plan_row, "leg_index": leg_index + 1},
                                        authorized_by=str(plan_row.get("current_proposal_id") or ""))

    async def _issue_next_leg_card(self, plan_row: dict[str, Any], *, authorized_by: str = "") -> None:
        """The relay's one creative act: the plan's next leg as a normal card in the plan's own conversation,
        announced by voice when a call is online. A 目标持仓 / 反手 plan's leg is confirmed here on the first leg's
        confirmation (`authorized_by`, that card's id); a 一句多单 plan's leg waits for the user's own confirm."""
        store = self.app.state.store
        proposals = getattr(self.app.state, "proposals", None)
        leg_index = int(plan_row["leg_index"])
        plan = plan_row["leg"].get("plan") or {}
        legs = plan.get("legs") or []
        if proposals is None or not legs or leg_index >= len(legs):
            store.update_target_plan(plan_row["id"], status="abandoned")
            return
        leg = legs[leg_index]
        session_id = str(plan_row["leg"].get("sessionId") or "")
        # 账户核对 (#518): 会话已切到别的账户时，第 2 步的卡不能发进另一个账户的台账 ——
        # 计划取消，一句话说清；用户在新账户上想继续就重新发起目标持仓。
        try:
            session = self.app.state.broker.get_session(session_id) if session_id else None
        except Exception:
            session = None
        session_account = str(getattr(session, "trading_account_id", "") or "") \
            if session is not None else ""
        # A plan on an account the caller named (accountExplicit, HTTP 账户头 / CLI --account) never followed the
        # conversation's account: its next leg stays on the plan's own account.
        explicit = bool(plan_row["leg"].get("accountExplicit"))
        if not explicit and session_account and session_account != str(plan_row["trading_account_id"]):
            store.update_target_plan(plan_row["id"], status="abandoned",
                                     expect=("awaiting_confirm", leg_index))
            await self._plan_notice(plan_row, (
                f"{plan_row['symbol']} 目标持仓的第 {leg_index + 1} 步未出卡：会话账户已切换，计划已取消。"))
            return
        turn_id = f"targetplan-{uuid4().hex[:12]}"
        # Only a plan whose first card promised it (`confirmOnce`, #1140): one stored before 反手确认一次 keeps its
        # own second card, as its first card said.
        authorized = bool(authorized_by) and bool(plan_row["leg"].get("confirmOnce")) and not plan.get("sentence")
        note = (f"这是目标持仓的第 {leg_index + 1} 步 / 共 {len(legs)} 步；第 1 步确认时已一并确认，前一段全部成交后自动发出。"
                if authorized else
                f"这是目标持仓的第 {leg_index + 1} 步 / 共 {len(legs)} 步；前一段已成交，这段确认后才会发单。")
        annotation = {**plan, "planId": plan_row["id"], "step": leg_index + 1, "totalSteps": len(legs), "note": note,
                      **({"authorizedByProposalId": authorized_by} if authorized else {})}
        # The next leg goes through the one order planner like every other card (ORDER_INTENT_V2 第 1 步): an
        # opening leg passes the same readiness gate as a typed order (行情/K线/合约/账户/委托最新、日内开仓限额),
        # priced by the plan's own mode (#941) or the per-side default.
        tools = self._account_tools(str(plan_row["user_id"]), str(plan_row["trading_account_id"]), session_id)
        problem: str | None = None
        proposal: dict[str, Any] | None = None
        event = "proposal.trade"
        if tools is None:
            problem = "这个账户的出卡环境暂时不可用"
        else:
            intent = intent_from_trade_order(
                symbol=str(plan_row["symbol"]), side=str(leg["side"]), volume=int(leg["volume"]),
                # 一句两单的接力（ORDER_INTENT_V2 第 2 步）: each leg keeps the price the user said for it.
                price_intent=leg.get("priceIntent", plan.get("priceIntent")), limit_price=leg.get("limitPrice"),
                reason=str(plan_row["leg"].get("reason") or "目标持仓"))
            try:
                outcome = await tools.plan_order(
                    intent, mode=str(getattr(session, "mode", "") or "local"), session_id=session_id,
                    turn_id=turn_id, user_id=str(plan_row["user_id"]), target_position=annotation,
                    account_id=str(plan_row["trading_account_id"]) if explicit else None)
            except Exception as error:  # noqa: BLE001 — any failure ends the plan with one honest line
                outcome = None
                problem = str(error) or type(error).__name__
            if isinstance(outcome, Cards):
                event, proposal = outcome.cards[0].event, outcome.cards[0].proposal
            elif outcome is not None:
                problem = outcome_line(outcome)
        if proposal is None:
            store.update_target_plan(plan_row["id"], status="abandoned",
                                     expect=("awaiting_confirm", leg_index))
            await self._plan_notice(plan_row, (
                f"{plan_row['symbol']} 目标持仓的第 {leg_index + 1} 段没能生成确认卡（{problem}），计划已取消。"))
            return
        linked = store.update_target_plan(plan_row["id"], current_proposal_id=str(proposal["proposalId"]),
                                          expect=("awaiting_confirm", leg_index))
        if linked is None:
            return  # superseded while the card was being created: the card dies with it, no notice
        side_word = {"buy_open": "买开", "sell_open": "卖开",
                     "buy_close": "买平", "sell_close": "卖平"}.get(
            str(proposal.get("side") or leg["side"]), str(leg["side"]))
        price = f" {_price_text(proposal['limitPrice'])}" if proposal.get("limitPrice") is not None else ""
        label = f"{proposal.get('name') or plan_row['symbol']} {side_word} {proposal.get('volume', leg['volume'])} 手{price}".strip()
        if authorized:
            await self._send_authorized_leg(plan_row, proposal, event=event, turn_id=turn_id, label=label,
                                            authorized_by=authorized_by)
            return
        # The notice and the card share the card's own turn: a notice under a fresh turn id left the
        # card attached to a turn no client had, and the second leg's confirmation never showed.
        await self._plan_notice(plan_row, (
            f"{plan_row['symbol']} 目标持仓的第 {leg_index} 段已成交。第 {leg_index + 1} 步的确认卡来了：{label}；"
            f"确认后才会发单。"), card=(event, proposal), turn_id=turn_id)

    async def _send_authorized_leg(self, plan_row: dict[str, Any], proposal: dict[str, Any], *, event: str,
                                   turn_id: str, label: str, authorized_by: str) -> None:
        """反手确认一次 (2026-10-24): the next leg's card confirmed through the one confirm service — freshness, tick,
        price limit and position are checked again at send time — authorized by the first leg's confirmation
        (audited on the card). The idempotency key is the plan's leg and the card's approval is single-use, so a
        second attempt never sends again. A refusal ends the plan with one line; the card is voided, never left
        for anyone to confirm."""
        store = self.app.state.store
        proposals = self.app.state.proposals
        leg_index = int(plan_row["leg_index"])
        user_id = str(plan_row["user_id"])
        proposal_id = str(proposal["proposalId"])
        store.audit(proposal_id, "target-plan-authorized", {
            "planId": plan_row["id"], "step": leg_index + 1, "authorizedByProposalId": authorized_by})
        try:
            result = await proposals.confirm(
                proposal_id, user_id=user_id, approval_id=str(proposal["approvalId"]),
                proposal_version=int(proposal["version"]),
                idempotency_key=f"targetplan:{plan_row['id']}:leg{leg_index + 1}")
        except Exception as error:  # noqa: BLE001 — any refusal ends the plan with one honest line
            code = str(getattr(error, "code", None) or error or type(error).__name__)
            problem = getattr(error, "message", None) or CANDIDATE_ISSUE_LABELS.get(code, code)
            try:
                proposals.supersede_stale_card(proposal_id, user_id=user_id)
            except Exception:  # noqa: BLE001 — the card was never shown; its approval window still ends it
                pass
            ended = store.update_target_plan(plan_row["id"], status="abandoned",
                                             expect=("awaiting_confirm", leg_index))
            if ended is not None:
                await self._plan_notice(plan_row, (
                    f"{plan_row['symbol']} 第 {leg_index} 段已成交，第 {leg_index + 1} 段（{label}）没有发出：{problem}。"
                    f"计划已取消，请先在委托里确认状态。"))
            return
        await self.link_confirmed_plan(result, user_id=user_id, proposal_id=proposal_id)
        try:
            card = (event, dict(store.get(proposal_id)["payload"]))
        except KeyError:
            card = (event, proposal)
        await self._plan_notice(plan_row, (
            f"{plan_row['symbol']} 第 {leg_index} 段已成交，第 {leg_index + 1} 段已按你第 1 步的确认自动发出：{label}。"),
            card=card, turn_id=turn_id)

    def _account_tools(self, user_id: str, account_id: str, session_id: str) -> Any | None:
        """The plan account's card tools (`trader_tools.account_card_tools`, the trading AI's own set for that
        account); None when the account is not assembled in this process."""
        from .trader_tools import account_card_tools
        services, _ = self._services(account_id)
        if services is None:
            return None
        bridge = getattr(self.app.state, "app_capabilities", None)
        adapter: Any = getattr(self.app.state, "adapter", None)

        def status(mode: str) -> dict[str, Any]:
            ctx = getattr(bridge, "ctx", None)
            if ctx is not None and session_id:
                return dict(ctx.status_for_session(session_id))
            return dict(adapter.runtime_status(mode))
        try:
            return account_card_tools(self.app, user_id, services, status)
        except Exception:
            return None

    async def _plan_notice(self, plan_row: dict[str, Any], text: str,
                           *, card: tuple[str, dict[str, Any]] | None = None, turn_id: str | None = None) -> None:
        """One line in the plan's conversation (and the card, when this notice carries one) through the one
        reminder entry (#932: 目标持仓's switch — the same one as 挂单未成交 —, the 提醒中心 row, the desktop
        notification, the call that is online now, the push). A notice carrying the next leg's confirmation
        card is essential: the card lands even with the switch off. Each notice is already once (the plan's
        conditional updates), so its turn is its key. A notice carrying a card is published under that card's
        turn (`turn_id`), so both land on one turn."""
        from .reminders import ReminderSpec, deliver
        from .web_push import PushMessage, deep_link
        session_id = str(plan_row["leg"].get("sessionId") or "")
        turn = turn_id or str((card[1] if card else {}).get("turnId") or "") or f"targetplan-{uuid4().hex[:12]}"
        symbol = str(plan_row.get("symbol") or "")
        await deliver(self.app, ReminderSpec(
            kind="target_plan", user_id=str(plan_row["user_id"]), account_id=str(plan_row.get("trading_account_id") or ""),
            dedupe_key=turn, title="目标持仓", body=text, symbol=symbol, at=datetime.now(UTC).isoformat(),
            message=text, turn_id=turn, cards=[card] if card is not None else [], session_id=session_id or None,
            auto_landing=False, essential=card is not None, speech=text, voice_kind="target_plan",
            voice_key=str(plan_row.get("id") or ""),
            push=PushMessage(kind="target_plan", title="目标持仓", body=text, short="目标持仓有新进展",
                             tag=f"targetplan-{plan_row.get('id')}", url=deep_link("chat"))))
