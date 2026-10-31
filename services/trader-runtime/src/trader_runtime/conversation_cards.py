"""Structured conversation cards the Runtime produces itself (2026-10-04).

Four card events join the proposal cards, each built here from facts the Runtime already holds —
never from the model's words:

- `card.order_question`: an order that needs one more detail — the order planner's question (the typed fast
  path or propose_order), its quick answers whole orders (`no_card` = a forced order round answered in
  prose twice).
- `card.research_judgment`: the research desk's judgment of a product for today, attached to an
  opinion question (built in native_agent from ResearchDeskService.report).
- `card.reminder`: price alert, risk ratio, market close, delivery, protection fired, unfilled
  order — one shape, published with the reminder's message under the same turnId.
- `card.execution`: what the AI (自主执行) or a strategy (自动下单) sent, read back from the account;
  `ExecutionCardTracker` republishes it under the same cardId as the order's state changes — a stop
  card the same way, from the stop's own row (armed → triggered / partially filled / closed /
  cancelled / unknown), so live and after-refresh read the same.

Card actions only ask the AI (the prompt goes out as the user's own message) or open a panel through
an existing UI command; no card action places or cancels an order.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .contracts import EVENT_MODELS
from .instruments import PAPER_FOREIGN_PREFIX, is_foreign_paper
from .simulation_orders import PARTIAL_ENDED

log = logging.getLogger("uvicorn.error.cards")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def ask_action(label: str, prompt: str) -> dict[str, Any]:
    """A button that sends `prompt` as the user's next message (the normal conversation)."""
    return {"kind": "ask", "label": label[:40], "prompt": prompt[:200]}


def ui_action(label: str, changes: dict[str, Any]) -> dict[str, Any]:
    """A button that applies an existing UI command in the window showing the card."""
    return {"kind": "ui", "label": label[:40], "changes": changes}


def event_kind(name: str) -> str:
    """The envelope kind a card event is published with (the contract's own table)."""
    definition = EVENT_MODELS.get(name)
    return definition[0] if definition else "artifact"


async def publish_notice(broker: Any, session_id: str, *, turn_id: str, text: str,
                         card: tuple[str, dict[str, Any]] | None = None, at: str | None = None) -> None:
    """One assistant message and (optionally) its card under the same turn — the message first, so
    voice and older clients keep reading the text; a client with the card shows the card instead."""
    await broker.publish(session_id, kind="message", name="message.completed", payload={
        "turnId": turn_id, "role": "assistant", "agentId": "native", "text": text, "at": at or now_iso()})
    if card is not None:
        await broker.publish(session_id, kind=event_kind(card[0]), name=card[0], payload={**card[1], "turnId": turn_id})


def stage_notice(broker: Any, connection: Any, session_id: str, *, turn_id: str, text: str,
                 card: tuple[str, dict[str, Any]] | None = None, at: str | None = None) -> None:
    """`publish_notice`'s synchronous twin for callers inside a store transaction (#521): the
    message and the card land in the outbox on the same connection, so they commit with the
    caller's dedup row or roll back with it. A later flush (the caller's or the broker loop's)
    delivers them; nothing is lost to a crash between commit and delivery."""
    broker.stage(session_id, kind="message", name="message.completed", connection=connection, payload={
        "turnId": turn_id, "role": "assistant", "agentId": "native", "text": text, "at": at or now_iso()})
    if card is not None:
        broker.stage(session_id, kind=event_kind(card[0]), name=card[0], connection=connection,
                     payload={**card[1], "turnId": turn_id})


def reminder_target_session(broker: Any, store: Any, user_id: str, *,
                            account_id: str | None = None) -> dict[str, Any] | None:
    """The conversation a reminder's persistent message and card land in (owner decision,
    2026-10-08): the one the user is looking at now, not merely the newest one. For an
    account-bound reminder: a currently subscribed (online) session of this user bound to that
    account — the most recently active when several qualify —, else the most recently active
    session bound to it, else the user's most recently active chat at all. Without an account
    (a price or movement alert has none) the account tier vanishes: an online session of this
    user, else the most recent one. Exactly one target — the reminder still lands once, never
    one copy per session. A failure here returns None, which only drops the persistent landing,
    exactly as the old single read failing did. The USD ledger (paperusd:x) is one account with its
    RMB ledger (paper:x) on screen (两本账第三批, 2026-10-22): its reminders land in the conversations on
    paper:x, which is where the user sees its positions."""
    try:
        sessions = store.chat_sessions_by_recency(user_id=user_id, mode=None)
        online = set(broker.subscribed_session_ids())
        accounts = {account_id} if account_id else set()
        if account_id and is_foreign_paper(account_id):
            accounts.add("paper:" + account_id.removeprefix(PAPER_FOREIGN_PREFIX))
        on_account = [session for session in sessions if str(session.get("accountId") or "") in accounts]
        group = on_account or sessions
        target = next((session for session in group if session["sessionId"] in online), None)
        return target or (group[0] if group else None)
    except Exception:  # noqa: BLE001 — never break the reminder this only picks a landing for
        return None


def price_text(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else f"{number:g}"


def reminder_card(*, turn_id: str, kind: str, title: str, summary: str, at: str,
                  figures: list[tuple[Any, ...]] | None = None,
                  symbol: str | None = None, actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A `card.reminder` payload. `figures` are (label, value[, tone]); empty values are dropped."""
    shown = []
    for figure in figures or []:
        label, value = figure[0], figure[1]
        tone = figure[2] if len(figure) > 2 else None
        if value is None or str(value).strip() in {"", "None"}:
            continue
        shown.append({"label": str(label)[:20], "value": str(value)[:60], **({"tone": tone} if tone else {})})
    return {"turnId": turn_id, "cardId": f"reminder-{turn_id}", "kind": kind, "title": title[:40],
            "summary": summary[:300], "figures": shown[:8], "symbol": symbol, "at": at,
            "actions": (actions or [])[:4]}


# -- execution cards -------------------------------------------------------------------------------

SIDE_LABELS = {("buy", "OPEN"): "买开", ("sell", "OPEN"): "卖开", ("buy", "CLOSE"): "买平",
               ("sell", "CLOSE"): "卖平", ("buy", "CLOSETODAY"): "买平今", ("sell", "CLOSETODAY"): "卖平今"}
# The order's own read-back state (simulation_orders.order_display_status) → the card's status.
# Everything else — 结果未知 / 身份异常 / 回报异常 / unreadable — is unknown, never success.
_STATUS = {
    "已受理": ("submitted", "已报"),
    "部分成交": ("partial", "部分成交"),
    # Ended after a partial fill (#932): the card stays "partial" (some lots did fill) but says it
    # is over, and its follow ends (`execution_finished`).
    PARTIAL_ENDED: ("partial", PARTIAL_ENDED),
    "已成交": ("filled", "已成交"),
    "已撤销": ("unfilled", "未成交（已撤）"),
    "待发送": ("unfilled", "未发出"),
    "已放弃": ("unfilled", "未发出"),
    "已拒绝": ("rejected", "已拒绝"),
}
TERMINAL_STATUSES = frozenset({"filled", "unfilled", "rejected", "failed"})
UNKNOWN_DETAIL = "回报暂时读不到，结果以账户回读为准；只回读原委托，不会重发。"


def execution_status(display_status: str) -> tuple[str, str]:
    return _STATUS.get(display_status, ("unknown", "结果未知"))


def execution_finished(display_status: str) -> bool:
    """The order this card follows will not change any more: a terminal card status, or a
    partial fill whose rest has ended."""
    return display_status == PARTIAL_ENDED or execution_status(display_status)[0] in TERMINAL_STATUSES


def order_figure(label: str, display_status: str) -> tuple[str, str, str | None]:
    """A reminder card's figure for an order it reports (保护触发的“平仓单”): the read-back label and
    its tone — filled positive, rejected danger, anything but 已报 a warning."""
    status, text = execution_status(display_status)
    tone = ("positive" if status == "filled" else "danger" if status == "rejected"
            else None if status == "submitted" else "warning")
    return (label, text, tone)


def _status_detail(display_status: str, filled: int | None) -> str | None:
    status = execution_status(display_status)[0]
    if status == "unknown":
        return UNKNOWN_DETAIL
    if display_status == PARTIAL_ENDED:
        return f"已成交 {filled or 0} 手，其余已撤或已失效，不再挂单。"
    if status == "partial":
        return f"已成交 {filled or 0} 手，其余仍在挂单。"
    return None


def execution_card(*, turn_id: str, source: str, source_label: str, order: dict[str, Any],
                   reason: str = "", name: str | None = None) -> dict[str, Any]:
    """A `card.execution` payload from a simulation order view (the account's read-back)."""
    request = order.get("request") or {}
    client_order_id = str(order.get("clientOrderId") or "")
    direction = str(request.get("direction") or "").lower()
    offset = str(request.get("offset") or "OPEN").upper()
    display = str(order.get("displayStatus") or "结果未知")
    status, label = execution_status(display)
    filled = int(order["filledVolume"]) if order.get("filledVolume") is not None else None
    symbol = str(request.get("symbol") or "")
    return {
        "turnId": turn_id, "cardId": f"execution-{client_order_id or turn_id}",
        "source": source, "sourceLabel": source_label[:80] or ("AI 自主" if source == "ai" else "策略"),
        "orderKind": "order", "symbol": symbol or "?", "name": str(name or request.get("name") or symbol or "?"),
        "sideLabel": SIDE_LABELS.get((direction, offset), f"{direction} {offset}".strip() or "委托"),
        "volume": int(request.get("volume") or 0),
        "price": price_text(request["limitPrice"]) if request.get("limitPrice") is not None else None,
        "reason": reason[:300], "status": status, "statusLabel": label, "filledVolume": filled,
        "clientOrderId": client_order_id or None, "detail": _status_detail(display, filled), "updatedAt": now_iso(),
    }


def stop_execution_card(*, turn_id: str, stop: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """A `card.execution` payload for a protective stop the AI armed (no order goes out until it fires)."""
    direction = "多" if stop.get("positionDirection") == "long" else "空"
    parts = [f"止损 {price_text(stop['triggerPrice'])}" if stop.get("triggerPrice") else "",
             f"止盈 {price_text(stop['takeProfitPrice'])}" if stop.get("takeProfitPrice") else ""]
    armed = str(stop.get("status") or "ARMED").upper() == "ARMED"
    symbol = str(stop.get("symbol") or "?")
    return {
        "turnId": turn_id, "cardId": f"execution-stop-{stop.get('stopId') or turn_id}",
        "source": "ai", "sourceLabel": "AI 自主", "orderKind": "stop", "symbol": symbol,
        "name": str(name or symbol), "sideLabel": f"保护{direction}单",
        "volume": int(stop.get("volume") or 0), "price": " / ".join(part for part in parts if part) or None,
        "reason": "", "status": "armed" if armed else "failed", "statusLabel": "已挂保护" if armed else "未挂上",
        "filledVolume": None, "clientOrderId": None,
        "detail": "触发后按设定价格方式发平仓单；持仓平掉或撤销后保护失效。" if armed else None,
        "updatedAt": now_iso(),
    }


STOP_CARD_STATUSES = {
    # ai_stop_orders.status → the stop card's own (status, label). TRIGGERING/SUBMITTED both read
    # "已触发" — the close order is out; what it became next is its own row's business.
    "ARMED": ("armed", "已挂保护"),
    "TRIGGERING": ("submitted", "已触发"),
    "SUBMITTED": ("submitted", "已触发"),
    "PARTIAL": ("partial", "部分成交"),
    "EXECUTED": ("filled", "已平"),
    "FAILED": ("failed", "平仓未成"),
    "CANCELLED": ("unfilled", "已撤销"),
    "UNKNOWN": ("unknown", "结果未知"),
}
STOP_CANCEL_DETAILS = {
    "anchor_position_closed": "持仓已平，保护自动撤销。",
    "position_closed": "持仓已平，保护自动撤销。",
    "emergency_stop": "急停，保护已撤销。",
    "replaced_by_card": "改挂了新的保护，这一条已撤销。",
}
STOP_STATE_DETAILS = {
    "submitted": "已触发，平仓单已发出；结果以委托栏为准。",
    "partial": "平仓单部分成交；结束后剩余手数自动重新挂保护。",
    "filled": "保护触发，平仓已完成。",
    "failed": "触发后平仓单未成交；持仓已无保护，请在委托里核对。",
}



def protection_state(stop: dict[str, Any]) -> dict[str, Any]:
    """一条保护单（`_stop_public` 形状）对读者的当前状态（#794）：只有 ARMED 且还有剩余手数的
    才是仍挂着、会按价触发的有效保护（effective）；已撤销 / 已触发 / 已平 / 未成 / 结果未知的
    都不是，statusLabel 与卡片同一份映射，detail 说明为什么不在了。AI 工具与对话建议都按它判。"""
    status = str(stop.get("status") or "UNKNOWN").upper()
    card_status, label = STOP_CARD_STATUSES.get(status, ("unknown", "结果未知"))
    remaining = stop.get("remainingVolume")
    effective = status == "ARMED" and (remaining is None or int(remaining) > 0)
    detail = STOP_CANCEL_DETAILS.get(str(stop.get("lastError") or ""),
                                     "保护已撤销。" if card_status == "unfilled" else None)
    detail = detail or STOP_STATE_DETAILS.get(card_status, UNKNOWN_DETAIL if card_status == "unknown" else None)
    if status == "ARMED" and not effective:
        label, detail = "已失效", "剩余手数为 0，这条保护不再生效。"
    return {"effective": effective, "statusLabel": label, "statusDetail": None if effective else detail}

def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def stop_card_update(base: dict[str, Any], row: Any) -> dict[str, Any]:
    """A stop card's payload rebuilt from its ai_stop_orders row (2026-10-08): same cardId and
    turn as the card the AI's arm turn published, with the row's current state — armed volume
    shrinks with the position, a trigger / cancellation / unknown outcome shows as it is."""
    status = str(_row_value(row, "status") or "UNKNOWN").upper()
    card_status, label = STOP_CARD_STATUSES.get(status, ("unknown", "结果未知"))
    trigger, take = _row_value(row, "trigger_price"), _row_value(row, "take_profit_price")
    parts = [f"止损 {price_text(trigger)}" if trigger else "",
             f"止盈 {price_text(take)}" if take else ""]
    detail = STOP_CANCEL_DETAILS.get(str(_row_value(row, "last_error") or ""),
                                     "保护已撤销。" if card_status == "unfilled" else None)
    detail = detail or STOP_STATE_DETAILS.get(card_status, UNKNOWN_DETAIL if card_status == "unknown" else None)
    if card_status == "armed":
        detail = base.get("detail")  # the arm-time line still says what the protection does
    return {**base, "volume": int(_row_value(row, "volume") or 0),
            "price": " / ".join(part for part in parts if part) or None,
            "status": card_status, "statusLabel": label, "filledVolume": None,
            "detail": detail, "updatedAt": now_iso()}


@dataclass
class _Tracked:
    session_id: str
    payload: dict[str, Any]
    simulation_orders: Any
    user_id: str
    registered: float
    stop_id: str | None = None  # orderKind "stop": the read-back is the stop's own row
    # A reminder card (保护触发) reporting one order in one figure: the order it follows and that
    # figure's label; it republishes as card.reminder under its own cardId (#932).
    order_id: str | None = None
    figure: str | None = None


class ExecutionCardTracker:
    """Follows each execution card's order until it reads a terminal state (or the cap) and
    republishes the card under the same cardId when the read-back changes. It only queries: an
    order whose outcome is unknown is never re-sent (TQKQ_SIM_THIN_V1). The order watch's loop
    ticks it; producers reach it as `broker.execution_cards`. A stop card is followed the same
    way from its ai_stop_orders row (cancel, trigger, fill, shrink), which needs the store."""

    POLL_SECONDS = 2.0
    CAP_SECONDS = 1800.0

    def __init__(self, broker: Any, store: Any = None) -> None:
        self.broker = broker
        self._store = store
        self._cards: dict[str, _Tracked] = {}

    def track(self, session_id: str, payload: dict[str, Any], *, simulation_orders: Any, user_id: str) -> None:
        if (not session_id or simulation_orders is None or not payload.get("clientOrderId")
                or payload.get("status") in TERMINAL_STATUSES or payload.get("statusLabel") == PARTIAL_ENDED):
            return
        self._cards[str(payload["cardId"])] = _Tracked(session_id, dict(payload), simulation_orders, user_id,
                                                       time.monotonic())

    def track_stop(self, session_id: str, payload: dict[str, Any], *, stop_id: str, user_id: str) -> None:
        """A stop card joins the follow from its arm turn; the row is the read-back (no order to
        query until it fires, and the close order's outcome lands on the row itself)."""
        if not session_id or not stop_id or self._store is None or payload.get("status") in TERMINAL_STATUSES:
            return
        self._cards[str(payload["cardId"])] = _Tracked(session_id, dict(payload), None, user_id,
                                                       time.monotonic(), stop_id=stop_id)

    def track_reminder_order(self, session_id: str, payload: dict[str, Any], *, client_order_id: str,
                             figure: str, simulation_orders: Any, user_id: str) -> None:
        """A reminder card whose `figure` is an order's read-back follows that order the way an
        execution card does: same query, republished as card.reminder when the figure changes,
        dropped once terminal (#932). The caller hands over only a card whose order is still open."""
        if (not session_id or not client_order_id or simulation_orders is None
                or not any(item.get("label") == figure for item in payload.get("figures") or [])):
            return
        self._cards[str(payload["cardId"])] = _Tracked(session_id, dict(payload), simulation_orders, user_id,
                                                       time.monotonic(), order_id=client_order_id, figure=figure)

    def tracking(self) -> list[str]:
        return list(self._cards)

    def _stop_row(self, entry: _Tracked) -> Any | None:
        try:
            return self._store.connection.execute(
                "SELECT * FROM ai_stop_orders WHERE id = ?", (entry.stop_id,)).fetchone()
        except Exception as error:  # unreadable now: the next tick decides, nothing is guessed
            log.debug("stop card %s read failed: %s", entry.stop_id, error)
            return None

    async def tick(self) -> None:
        for card_id in list(self._cards):
            entry = self._cards.get(card_id)
            if entry is None:
                continue
            age = time.monotonic() - entry.registered
            if age > self.CAP_SECONDS:
                self._cards.pop(card_id, None)
                continue
            if age < self.POLL_SECONDS:
                continue  # the producer's own publish goes out before the first re-read
            if entry.stop_id is not None:
                await self._tick_stop(card_id, entry)
                continue
            if entry.figure is not None:
                await self._tick_reminder(card_id, entry)
                continue
            try:
                view = await entry.simulation_orders.query(str(entry.payload["clientOrderId"]), user_id=entry.user_id)
            except Exception as error:  # unreadable now: the next tick decides, nothing is guessed
                log.debug("execution card %s read failed: %s", card_id, error)
                continue
            display = str(view.get("displayStatus") or "结果未知")
            status, label = execution_status(display)
            filled = int(view["filledVolume"]) if view.get("filledVolume") is not None else None
            if (status, label, filled) == (entry.payload.get("status"), entry.payload.get("statusLabel"),
                                           entry.payload.get("filledVolume")):
                if execution_finished(display):
                    self._cards.pop(card_id, None)
                continue
            entry.payload = {**entry.payload, "status": status, "statusLabel": label, "filledVolume": filled,
                             "detail": _status_detail(display, filled), "updatedAt": now_iso()}
            await self._republish(card_id, entry)
            if execution_finished(display):
                self._cards.pop(card_id, None)

    async def _tick_stop(self, card_id: str, entry: _Tracked) -> None:
        row = self._stop_row(entry)
        if row is None:
            return  # the row never left: the next tick decides (only the cap ends the follow)
        payload = stop_card_update(entry.payload, row)
        unchanged = all(payload.get(key) == entry.payload.get(key)
                        for key in ("status", "statusLabel", "volume", "price", "detail"))
        if unchanged:
            return
        entry.payload = payload
        await self._republish(card_id, entry)
        if payload["status"] in TERMINAL_STATUSES:
            self._cards.pop(card_id, None)

    async def _tick_reminder(self, card_id: str, entry: _Tracked) -> None:
        try:
            view = await entry.simulation_orders.query(str(entry.order_id), user_id=entry.user_id)
        except Exception as error:  # unreadable now: the next tick decides, nothing is guessed
            log.debug("reminder card %s read failed: %s", card_id, error)
            return
        display = str(view.get("displayStatus") or "结果未知")
        label, value, tone = order_figure(str(entry.figure), display)
        figures = [{"label": label, "value": value, **({"tone": tone} if tone else {})}
                   if item.get("label") == label else item for item in entry.payload.get("figures") or []]
        if figures != entry.payload.get("figures"):
            entry.payload = {**entry.payload, "figures": figures}
            await self._republish(card_id, entry, name="card.reminder")
            # The 提醒中心 row of this card follows the same read-back (#932: 平仓单 已成交 771.5 ×1).
            from .reminders import order_state, update_state
            request = view.get("request") or {}
            order = (view.get("report") or {}).get("order") or {}
            price = order.get("tradePrice") or order.get("averagePrice") or request.get("limitPrice")
            await update_state(self._store, self.broker, card_id=card_id, state=order_state(
                display, filled=view.get("filledVolume"), volume=int(request.get("volume") or 0) or None,
                price=price, label=str(entry.figure)))
        if execution_finished(display):
            self._cards.pop(card_id, None)

    async def _republish(self, card_id: str, entry: _Tracked, *, name: str = "card.execution") -> None:
        try:
            await self.broker.publish(entry.session_id, kind="artifact", name=name, payload=entry.payload)
        except Exception as error:
            log.debug("execution card %s not republished: %s", card_id, error)


def track_execution_card(broker: Any, session_id: str, payload: dict[str, Any], *,
                         simulation_orders: Any, user_id: str) -> None:
    """Hand a just-built execution card to the tracker when this Runtime has one (replay children
    and bare test brokers have none: their card shows the first read-back only)."""
    tracker = getattr(broker, "execution_cards", None)
    if tracker is not None:
        tracker.track(session_id, payload, simulation_orders=simulation_orders, user_id=user_id)


def track_reminder_order(broker: Any, session_id: str, payload: dict[str, Any], *, client_order_id: str,
                         figure: str, simulation_orders: Any, user_id: str) -> None:
    """`track_execution_card`'s reminder twin: a 保护触发 card's 平仓单 figure follows its order."""
    tracker = getattr(broker, "execution_cards", None)
    if tracker is not None:
        tracker.track_reminder_order(session_id, payload, client_order_id=client_order_id, figure=figure,
                                     simulation_orders=simulation_orders, user_id=user_id)


def track_execution_stop(broker: Any, session_id: str, payload: dict[str, Any], *,
                         stop_id: str, user_id: str) -> None:
    """`track_execution_card`'s stop twin: the arm turn's card joins the follow by stopId."""
    tracker = getattr(broker, "execution_cards", None)
    if tracker is not None:
        tracker.track_stop(session_id, payload, stop_id=stop_id, user_id=user_id)
