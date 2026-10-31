"""统一提醒的投递入口与提醒记录 (#932, 2026-10-15).

When each kind of reminder is due stays its own module's judgment (收盘前, 风险度, 保本, 盘中异动, 到价,
交割, 止损止盈触发, 保护撤销, 挂单未成交, 目标持仓, 策略, 成交/撤单, 复盘, 开盘前判断, 研讨完成, AI 简讯,
急停). Delivering one is this module's, in one order for all of them:

1. the kind's one switch (`reminder_kinds`) — off means no channel at all: no message, no desktop
   notification, no spoken line, no push, no 提醒中心 row. 急停 has no switch. A message that carries the
   business itself (a strategy's or a 目标持仓's confirmation card, an automatic order's execution card) is
   `essential`: it still lands in its conversation with the switch off, only the reminder is gone;
2. the claim — one `reminders` row per user × kind × dedupe key — committed in ONE store transaction with
   the conversation message and its card (staged on the outbox, `conversation_cards.stage_notice`) and
   with the caller's own business claim (`legacy`: a 到价提醒 turning triggered, a 保本 question row, a
   复盘's pushed_at, or the older ledger row a slot still running the previous build reads; or `claim`, a
   per-item claim whose result decides what the reminder says — 成交 counts only the fills this
   transaction claimed). A crash or a failed write rolls all of them back and the result says `failed`:
   the caller keeps the event pending for its next round; a second process, a restart or the other
   blue-green slot finds the claim and stays quiet (`claimed`);
3. the landing: a 交易与账户 kind goes to the conversation the caller names (the stop's own, the strategy's,
   the confirming one) or, when it names none, `reminder_target_session` (online on this account first);
   an 信息 kind (开盘前判断, 研讨完成, AI 简讯) is never written into a conversation;
4. after the commit: the outbox flush, one `feed.reminder` to each of the user's subscribed windows (the
   reminder id, `type` new, the day's unread count — the client re-reads 提醒中心 and shows the desktop
   notification), the spoken line on a live call (throttled per kind AND object, so two strategies'
   signals do not swallow each other) and the lock-screen push (`PushHub.notify`, the only push exit).

A reminder's state follows its card afterwards (平仓单 已成交, 保本 已移): `update_state` rewrites the row
and sends `feed.reminder` type update (no desktop notification). The 提醒中心 reads
GET /api/reminders and marks read through POST /api/reminders/read; rows live
AI_TRADER_REMINDER_RETAIN_DAYS days (`ReminderRetention`, a lease producer)."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .conversation_cards import event_kind, reminder_target_session
from .reminder_kinds import KINDS, canonical, reminder_enabled, voice_allowed
from .trading_calendar import NS, trading_day_of

log = logging.getLogger("uvicorn.error.reminders")

RETAIN_DAYS_ENV = "AI_TRADER_REMINDER_RETAIN_DAYS"
DEFAULT_RETAIN_DAYS = 30
RETENTION_SWEEP_SECONDS = 3600.0
PAGE_LIMIT = 50

# The exchange calendar the reminders' trading day is cut on (sorted ISO days); refreshed from the shared
# market at most every CALENDAR_REFRESH_SECONDS by the app-level paths, empty = the weekday rule.
_calendar: tuple[float, list[str]] = (0.0, [])
CALENDAR_REFRESH_SECONDS = 600.0


def retain_days() -> int:
    try:
        return max(1, int(os.getenv(RETAIN_DAYS_ENV, str(DEFAULT_RETAIN_DAYS))))
    except ValueError:
        return DEFAULT_RETAIN_DAYS


async def refresh_calendar(app: Any) -> list[str]:
    global _calendar
    if time.monotonic() - _calendar[0] < CALENDAR_REFRESH_SECONDS and _calendar[1]:
        return _calendar[1]
    shared = getattr(app.state, "shared_market", None)
    days: list[str] = []
    if shared is not None:
        try:
            days = list(await shared.trading_days())
        except Exception:  # noqa: BLE001 — the weekday rule stands in
            days = []
    _calendar = (time.monotonic(), days or _calendar[1])
    return _calendar[1]


def current_trading_day(now_ns: int | None = None) -> str:
    """The trading day a moment belongs to (≥18:00 → the next one, holidays snap forward): the one cut
    the account, the review and the delivery reminders already use."""
    return trading_day_of(time.time_ns() if now_ns is None else now_ns, _calendar[1] or None)


@dataclass
class ReminderSpec:
    """One reminder to deliver. `message` is the conversation text (None = no conversation landing; an
    info kind never lands); `cards` are staged under the same turn after it. `session_id` names the
    landing; without it a trade kind lands through `reminder_target_session`. `speech` is the spoken line
    (None = not spoken); `voice_kind` / `voice_key` name it for the call (the throttle is per both)."""
    kind: str
    user_id: str
    dedupe_key: str
    title: str
    body: str
    account_id: str = ""
    symbol: str = ""
    name: str = ""
    trading_day: str | None = None
    at: str | None = None
    message: str | None = None
    cards: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    turn_id: str | None = None
    session_id: str | None = None
    auto_landing: bool = True
    target: dict[str, Any] | None = None
    state: Any = None
    actions: list[dict[str, Any]] | None = None
    push: Any = None
    speech: str | None = None
    voice_kind: str | None = None
    voice_key: str | None = None
    essential: bool = False
    legacy: Callable[[Any], bool] | None = None
    # A per-item claim in the same transaction (#932 R1): the content fields (title, body, dedupe_key, speech,
    # voice_key, push) built from exactly what it claimed — the record and every channel then say only that;
    # None = it claimed nothing, nothing goes out.
    claim: Callable[[Any], dict[str, Any] | None] | None = None
    replay_id: str | None = None
    feed_extra: dict[str, Any] = field(default_factory=dict)


DeliveryOutcome = Literal["delivered", "off", "claimed", "failed"]


@dataclass
class Delivery:
    """What became of a reminder: `delivered` (recorded, channels sent), `off` (its switch is off — an essential
    card may still have landed), `claimed` (another holder or an earlier round already told it) or `failed` (the
    transaction rolled back: nothing was recorded, the caller may try again next round). Channel failures after
    the commit stay `delivered` — they are never turned into a second claim."""
    reminder: dict[str, Any] | None
    session_id: str | None
    landed: bool
    outcome: DeliveryOutcome = "delivered"


def _card_id(cards: list[tuple[str, dict[str, Any]]]) -> str | None:
    for _, payload in cards:
        value = payload.get("cardId") or payload.get("proposalId")
        if value:
            return str(value)
    return None


async def publish_feed(store: Any, broker: Any, user_id: str, payload: dict[str, Any]) -> None:
    """One transient feed.reminder to each window of this user attached to this process."""
    if broker is None:
        return
    for session_id in broker.subscribed_session_ids():
        try:
            if str(store.get_session(session_id).get("userId") or "") != user_id:
                continue
            await broker.publish_transient(session_id, kind="result", name="feed.reminder", payload=payload)
        except Exception:  # noqa: BLE001 — a closed window never stops the others
            continue


def _account_name(store: Any, account_id: str) -> str:
    if not account_id:
        return ""
    try:
        return str((store.get_trading_account(account_id) or {}).get("display_name") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def deliver_reminder(store: Any, broker: Any, spec: ReminderSpec, *, voice: Any = None, push: Any = None,
                           preferences_store: Any = None) -> Delivery:
    """The one delivery entry (module docstring). Never raises: a reminder never breaks what it rides on."""
    kind = canonical(spec.kind)
    try:
        preferences = (preferences_store or store).user_preferences(spec.user_id).model_dump()
    except Exception:  # noqa: BLE001 — unreadable preferences: the defaults decide
        preferences = {}
    at = spec.at or datetime.now(UTC).isoformat()
    turn_id = spec.turn_id or f"reminder-{kind}-{spec.dedupe_key}"
    session_id = spec.session_id
    if session_id is None and spec.message is not None and spec.auto_landing and KINDS[kind].group != "info":
        target = reminder_target_session(broker, store, spec.user_id, account_id=spec.account_id or None)
        session_id = str(target["sessionId"]) if target else None
    if KINDS[kind].group == "info":
        session_id = None
    message = {"turnId": turn_id, "role": "assistant", "agentId": "native", "text": spec.message, "at": at}
    if not reminder_enabled(preferences, kind):
        if spec.essential and session_id and spec.message is not None and broker is not None:
            try:  # the business card still lands; only the reminder (record and channels) is off
                await broker.publish(session_id, kind="message", name="message.completed", payload=message)
                for name, payload in spec.cards:
                    await broker.publish(session_id, kind=event_kind(name), name=name, payload={**payload, "turnId": turn_id})
                return Delivery(None, session_id, True, "off")
            except Exception:  # noqa: BLE001
                log.warning("reminder_essential_not_published kind=%s", kind, exc_info=True)
        return Delivery(None, session_id, False, "off")
    try:
        trading_day = spec.trading_day or current_trading_day(ns_of(at))
    except ValueError:
        trading_day = current_trading_day()
    card_id = _card_id(spec.cards)
    landed = bool(session_id and spec.message is not None)
    target = spec.target or {"type": "chat"}
    if landed and target.get("type") == "chat":
        target = {**target, "sessionId": session_id, **({"cardId": card_id} if card_id else {})}
    if spec.symbol and "symbol" not in target:
        target = {**target, "symbol": spec.symbol}
    try:
        with store.transaction() as connection:
            if spec.legacy is not None and not spec.legacy(connection):
                return Delivery(None, session_id, False, "claimed")
            if spec.claim is not None:
                shaped = spec.claim(connection)
                if not shaped:
                    return Delivery(None, session_id, False, "claimed")
                spec = replace(spec, **shaped)
            reminder = store.claim_reminder(
                connection, user_id=spec.user_id, kind=kind, dedupe_key=spec.dedupe_key, trading_day=trading_day,
                title=spec.title, body=spec.body, account_id=spec.account_id, symbol=spec.symbol, name=spec.name,
                session_id=session_id if landed else None, message_id=turn_id if landed else None, card_id=card_id,
                target=target, state=spec.state, actions=spec.actions, created_at=at)
            if reminder is None:
                return Delivery(None, session_id, False, "claimed")
            if landed and broker is not None:
                broker.stage(str(session_id), kind="message", name="message.completed", connection=connection,
                             payload=message)
                for name, payload in spec.cards:
                    broker.stage(str(session_id), kind=event_kind(name), name=name, connection=connection,
                                 payload={**payload, "turnId": turn_id})
    except Exception as error:  # noqa: BLE001 — rolled back together; the caller's next round retries
        log.warning("reminder_not_recorded kind=%s error=%s", kind, type(error).__name__)
        log.debug("reminder_not_recorded detail", exc_info=True)
        return Delivery(None, session_id, False, "failed")
    if landed and broker is not None:
        try:
            await broker.flush()
        except Exception as error:  # noqa: BLE001 — the staged rows are durable; the outbox loop retries them
            log.debug("reminder message not flushed: %s", error)
    try:
        unread = store.unread_reminder_count(spec.user_id, trading_day=current_trading_day())
    except Exception:  # noqa: BLE001
        unread = None
    feed: dict[str, Any] = {"id": reminder["id"], "type": "new", "kind": kind, "title": spec.title,
                            "text": spec.body, "at": at, **spec.feed_extra}
    if unread is not None:
        feed["unread"] = unread
    if spec.account_id:
        feed["accountId"] = spec.account_id
        name = _account_name(store, spec.account_id)
        if name:
            feed["accountName"] = name
    await publish_feed(store, broker, spec.user_id, feed)
    if voice is not None and spec.speech and voice_allowed(preferences, kind):
        try:
            if voice.user_calls(spec.user_id) > 0:
                voice.announce(spec.voice_kind or kind, spec.speech, user_id=spec.user_id, replay_id=spec.replay_id,
                               key=spec.voice_key or spec.dedupe_key)
        except Exception:  # noqa: BLE001
            log.debug("reminder voice failed", exc_info=True)
    if push is not None and spec.push is not None:
        try:
            push.notify(spec.user_id, spec.push, account_id=spec.account_id or None)
        except Exception:  # noqa: BLE001
            log.debug("reminder push failed", exc_info=True)
    return Delivery(reminder, session_id if landed else None, landed)


def _replay_voice(app: Any) -> Any:
    """A replay child's calls hang on the main app's voice service (the same rule account_reminders spoke by before #932)."""
    replay = getattr(app.state, "replay_context", None)
    if replay is None:
        return getattr(app.state, "voice_realtime", None)
    parent = getattr(replay, "parent", None)
    return getattr(parent.state, "voice_realtime", None) if parent is not None else None


def _preferences_store(app: Any) -> Any:
    replay = getattr(app.state, "replay_context", None)
    parent = getattr(replay, "parent", None) if replay is not None else None
    return parent.state.store if parent is not None else app.state.store


async def deliver(app: Any, spec: ReminderSpec, *, voice: Any = None) -> Delivery:
    """`deliver_reminder` for an app: its store and broker, the voice service (a replay child speaks on the
    main app's calls bound to this replay), the push hub (none in a replay child) and the preferences
    document (a replay child reads the main app's)."""
    await refresh_calendar(app)
    replay = getattr(app.state, "replay_context", None)
    if replay is not None and spec.replay_id is None:
        try:
            spec.replay_id = str(replay.provider.identifier)
        except Exception:  # noqa: BLE001
            spec.replay_id = None
    return await deliver_reminder(app.state.store, app.state.broker, spec, voice=voice or _replay_voice(app),
                                  push=getattr(app.state, "push", None), preferences_store=_preferences_store(app))


async def deliver_via_store(store: Any, spec: ReminderSpec, *, broker: Any = None, voice: Any = None) -> Delivery:
    """For the account loops that hold only the store (autonomy, strategy monitor, ledger): the app's own
    entry when main bound one (`store.reminder_sink`), else the store-level delivery with what they have."""
    sink = getattr(store, "reminder_sink", None)
    if sink is not None:
        result: Delivery = await sink(spec, voice=voice)
        return result
    return await deliver_reminder(store, broker, spec, voice=voice, push=getattr(store, "push_hub", None))


def bind_store(app: Any) -> None:
    """Bind the app's delivery entry to its store (main runtime and every replay child)."""
    async def sink(spec: ReminderSpec, *, voice: Any = None) -> Delivery:
        return await deliver(app, spec, voice=voice)
    app.state.store.reminder_sink = sink


async def update_state(store: Any, broker: Any, *, card_id: str, state: Any = None, actions: Any = None,
                       clear_actions: bool = False) -> int:
    """A reminder's state (and its actions) follow its card: rewrite the row, tell the user's windows
    (feed.reminder type update — no desktop notification). Returns how many rows changed."""
    kwargs: dict[str, Any] = {"state": state}
    if clear_actions or actions is not None:
        kwargs["actions"] = actions
    try:
        changed = store.update_reminder_state(card_id=card_id, **kwargs)
    except Exception:  # noqa: BLE001
        log.debug("reminder state not updated", exc_info=True)
        return 0
    for item in changed:
        user_id = item.pop("userId")
        payload = {"id": item["id"], "type": "update", "kind": item["kind"], "title": item["title"],
                   "text": item["body"], "at": datetime.now(UTC).isoformat()}
        try:
            payload["unread"] = store.unread_reminder_count(user_id, trading_day=current_trading_day())
        except Exception:  # noqa: BLE001
            pass
        await publish_feed(store, broker, user_id, payload)
    return len(changed)


def order_state(display_status: str, *, filled: int | None = None, volume: int | None = None,
                price: Any = None, label: str = "") -> dict[str, str]:
    """A reminder's state line for the order it reports (平仓单 / 挂单), from its read-back."""
    from .conversation_cards import execution_status, price_text
    from .simulation_orders import PARTIAL_ENDED
    status, text = execution_status(display_status)
    tone = {"filled": "ok", "submitted": "wait", "partial": "wait", "rejected": "warn",
            "unfilled": "muted"}.get(status, "warn")
    detail = text
    if display_status == PARTIAL_ENDED:
        tone, detail = "warn", f"部分成交 {filled or 0} 手后已结束"
    elif status == "filled":
        detail = f"已成交{(' ' + price_text(price)) if price not in (None, '') else ''}{f' ×{volume}' if volume else ''}"
    elif status in {"submitted", "partial"} and volume:
        detail = f"仍在排队 {filled or 0}/{volume}"
    return {"text": f"{label} {detail}".strip(), "tone": tone}


# -- the 提醒中心 interface ----------------------------------------------------------------------------

class ReminderTarget(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["chat", "orders", "research", "news", "review"]
    cardId: str | None = None
    sessionId: str | None = None
    symbol: str | None = None
    debateId: str | None = None
    tradingDay: str | None = None
    # 今日作战表 (2026-10-25): the 调研 panel's page a research row opens; absent = 品种调研.
    page: Literal["sheet"] | None = None


class ReminderItem(BaseModel):
    id: str
    kind: str = Field(description="类别：stop_triggered / protection_cancelled / order_unfilled / target_plan / strategy / "
                                  "fill / cancel / price_alert / market_close / risk_ratio / breakeven / delivery / "
                                  "market_movement / daily_review / morning_desk / research_debate / brief / emergency_stop")
    tradingDay: str
    createdAt: str
    accountId: str | None = Field(default=None, description="这条提醒属于的交易账户（成交、撤单、挂单未成交、复盘等）；点开委托或复盘前先切到它，不用当前账户代替；行情、到价等用户级提醒为 null")
    symbol: str | None = None
    name: str | None = None
    title: str
    body: str
    state: dict[str, Any] | str | None = Field(default=None, description="结果或进度，如 {text:'平仓单 已成交 771.5 ×1', tone:'ok'}")
    target: ReminderTarget
    read: bool
    actions: list[dict[str, Any]] | None = Field(default=None, description="待决的次级按钮（保本：移到成本 / 不用）")


class ReminderPage(BaseModel):
    items: list[ReminderItem]
    unread: int = Field(description="当前交易日的未读条数")
    tradingDay: str = Field(description="当前交易日（18:00 后算下一交易日）")
    nextCursor: str | None = Field(default=None, description="下一页的 before；没有更多为 null")
    moreCommand: bool = Field(default=True, description="这个 Runtime 认得界面命令 reminderCenter {more: true} 与快照的 "
                              "moreToday / moreAsked / moreDone / moreError（#932 R5）；之前的 Runtime 不回这个字段")


class ReminderRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] | None = Field(default=None, max_length=500, description="要标为已读的提醒 id")
    all: bool = Field(default=False, description="true：本人全部提醒标为已读")


class ReminderUnread(BaseModel):
    unread: int


def register_reminders(app: Any, current_user: Any) -> None:
    from .guest import guest_request

    async def owner(request: Request) -> str:
        if guest_request(request):
            raise HTTPException(403, "guest_forbidden")
        return str(await current_user(request))

    @app.get("/api/reminders", response_model=ReminderPage)
    async def list_reminders(request: Request,
                             scope: Literal["today", "earlier"] = Query("today", description="today＝当前交易日；earlier＝之前各交易日（保留 30 天）"),
                             before: str | None = Query(None, max_length=120, description="上一页返回的 nextCursor"),
                             unread: bool = Query(False, description="true 只列未读"),
                             limit: int = Query(PAGE_LIMIT, ge=1, le=200)):
        """提醒中心：本人的提醒记录，新的在前（止损止盈触发、保护撤销、挂单未成交、策略、成交撤单、到价、收盘前、
        风险度、保本、交割、盘中异动、复盘、开盘前判断、研讨完成、AI 简讯、急停）。每条带类别、合约、一句话、
        状态（如平仓单已成交）、点开去哪儿（target）与是否已读；保本待决时带“移到成本 / 不用”。"""
        user = await owner(request)
        await refresh_calendar(app)
        day = current_trading_day()
        items, cursor = await asyncio.to_thread(app.state.store.reminders_page, user, trading_day=day, scope=scope,
                                                unread_only=unread, before=before, limit=limit)
        return {"items": items, "unread": app.state.store.unread_reminder_count(user, trading_day=day),
                "tradingDay": day, "nextCursor": cursor, "moreCommand": True}

    @app.post("/api/reminders/read", response_model=ReminderUnread)
    async def read_reminders(request: Request, body: ReminderRead):
        """把提醒标为已读：{ids:[...]} 指定几条，或 {all:true} 全部；返回当前交易日剩余未读数。桌面与手机共用同一份已读。"""
        user = await owner(request)
        if not body.all and not body.ids:
            raise HTTPException(422, "ids_or_all_required")
        app.state.store.mark_reminders_read(user, None if body.all else list(body.ids or []))
        await refresh_calendar(app)
        return {"unread": app.state.store.unread_reminder_count(user, trading_day=current_trading_day())}


class ReminderRetention:
    """每日清理 (a lease producer): reminders and the older ledgers past AI_TRADER_REMINDER_RETAIN_DAYS."""

    def __init__(self, app: Any, *, interval: float = RETENTION_SWEEP_SECONDS) -> None:
        self.app = app
        self.interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def run_once(self) -> dict[str, int]:
        before = (datetime.now(UTC) - timedelta(days=retain_days())).isoformat()
        counts = self.app.state.store.purge_reminders(before=before)
        if any(counts.values()):
            log.info("reminder_retention %s", " ".join(f"{key}={value}" for key, value in counts.items() if value))
        return counts

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:  # noqa: BLE001 — one failed sweep never ends the loop
                log.warning("reminder_retention_failed", exc_info=True)
            await asyncio.sleep(self.interval)


def ns_of(at: str) -> int:
    moment = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * NS)
