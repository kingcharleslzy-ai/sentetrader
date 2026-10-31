from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request

from . import fast_json
from .contracts import SCHEMA_VERSION, validate_event
from .store import RuntimeStore

log = logging.getLogger("uvicorn.error.broker")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def session_is_live(record: dict[str, Any]) -> bool:
    """An active, unexpired session row — what `get_session` serves."""
    return bool(record["status"] == "active" and record["expiresAt"] > utc_now())


@dataclass(frozen=True)
class PreparedMarket:
    payload: dict[str, Any]
    encoded: str


# Events whose shared payload may arrive pre-encoded (prepare_frame): the market and its patches keep
# their own one-slot store; the board frames and the account / activity / execution frames ride the
# ordinary transient ring carrying their encoding (2026-10-13: one encoding per frame, not per session).
PREPARED_EVENTS = {"feed.market", "feed.market-patch", "feed.board", "feed.board-patch",
                   "feed.account", "feed.activity", "feed.execution"}
MARKET_SLOT_EVENTS = {"feed.market", "feed.market-patch"}
# 扩容① (2026-10-13, owner-approved): a stream re-checks its caller's authorization and its connection
# once per this many seconds — not before every event — so a revoked login, a logout or an expired
# session ends the stream within this delay. The persisted-event table is re-read on the same cadence
# when nothing was delivered in this process (another process on the same database may deliver).
RECHECK_SECONDS = 1.0


class _PreparedEvent(dict[str, Any]):
    """A transient event whose payload was encoded once for every session (prepare_frame)."""
    prepared: PreparedMarket | None = None


@dataclass(frozen=True)
class SessionStream:
    session_id: str
    mode: str
    agent_id: str
    user_id: str
    trading_account_id: str | None = None


class EventBroker:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store
        self.conditions: dict[str, asyncio.Condition] = {}
        self.subscribers: dict[str, int] = {}
        self.stream_generations: dict[str, int] = {}
        self.transient_events: dict[str, deque[tuple[int, dict[str, Any]]]] = {}
        # The newest sequence evicted from a session's full transient ring: a reader whose
        # cursor is below it has silently missed whole frames and must be handed a full
        # resend (a generation bump), not refreshed timestamps on stale data (#515 F-f).
        self.transient_evicted: dict[str, int] = {}
        self.market_frames: dict[str, PreparedMarket] = {}
        # Bumped whenever this process delivers a persisted event to the session (flush): a stream
        # reads the events table only when its mark moved, or every RECHECK_SECONDS as a fallback.
        self.persisted_marks: dict[str, int] = {}
        self.recheck_seconds = RECHECK_SECONDS
        self.latest_market: dict[str, tuple[int, dict[str, Any]]] = {}
        self.on_subscribers_changed: Callable[[str], None] | None = None
        self.transient_sequences: dict[str, int] = {}
        self._flush_lock = asyncio.Lock()
        self._outbox_task: asyncio.Task[None] | None = None
        self._closing = False
        self._released_sessions: set[str] = set()
        # The execution-card tracker (conversation_cards.ExecutionCardTracker), set by the app that
        # runs the order watch; producers of card.execution hand it their card. None = no tracking.
        self.execution_cards: Any = None
        # 新浪备用 (2026-10-07): how a runtime.status reads to the user while the shared
        # market serves backup quotes (SharedMarketService.present_status). Every
        # runtime.status a client receives — staged or transient — passes here; None = unchanged.
        self.present_runtime_status: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def create_session(
        self, mode: str, agent_id: str, user_id: str, trading_account_id: str | None = None,
    ) -> SessionStream:
        record = self.store.create_session(
            user_id=user_id, mode=mode, agent_id=agent_id, trading_account_id=trading_account_id,
        )
        self._released_sessions.discard(record["sessionId"])
        self.conditions[record["sessionId"]] = asyncio.Condition()
        return SessionStream(record["sessionId"], mode, agent_id, user_id, record["tradingAccountId"])

    def get_session(self, session_id: str, user_id: str | None = None) -> SessionStream | None:
        try:
            record = self.store.get_session(session_id, user_id=user_id)
        except KeyError:
            return None
        if not session_is_live(record):
            return None
        self.conditions.setdefault(session_id, asyncio.Condition())
        return SessionStream(
            session_id, record["mode"], record["agentId"], record["userId"], record["tradingAccountId"],
        )

    def resume_session(self, session_id: str, user_id: str) -> SessionStream:
        record = self.store.resume_session(session_id, user_id=user_id)
        self.conditions.setdefault(session_id, asyncio.Condition())
        return SessionStream(
            session_id, record["mode"], record["agentId"], record["userId"], record["tradingAccountId"],
        )

    def stage(
        self, session_id: str, *, kind: str, name: str, payload: dict[str, Any],
        connection: Any | None = None,
    ) -> dict[str, Any]:
        stale = False
        if name == "runtime.status":
            stamped = self._with_account_revision(session_id, payload)
            stale = stamped is None
            payload = payload if stamped is None else stamped
        if name == "runtime.status" and self.present_runtime_status is not None:
            payload = self.present_runtime_status(payload)
        event = {"schemaVersion": SCHEMA_VERSION, "id": str(uuid4()), "sessionId": session_id,
                 "kind": kind, "name": name, "timestamp": utc_now(), "payload": payload}
        validate_event(event)
        if not stale:   # a stale status is dropped here: the caller still gets the event it built, nothing is sent
            self.store.stage_outbox_event(session_id, event, connection=connection)
        return event

    def _with_account_revision(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Every runtime.status of a session carries its account revision (#932 follow-up) — the agent's bootstrap
        status and the stream's first one included — so a window knows from its first status that this Runtime
        orders switches and names the revision in each. The revision and the account are read together from the
        session row and stamped only when the row is still on the account the status names: a status built before
        another switch landed is stale and None — not published (that switch publishes its own), never "the old
        account with the new revision" (Codex r4310015757). A status that already carries its pair (built from one
        row read) or names no account goes out as it is; so does one for a session this store does not hold (a replay
        child)."""
        account = payload.get("account")
        account_id = account.get("id") if isinstance(account, dict) else None
        if "accountGeneration" in payload or not account_id:
            return payload
        try:
            revision, row_account = self.store.session_account_state(session_id)
        except Exception:  # noqa: BLE001 — no session row here: the status goes out as it was
            return payload
        if row_account != account_id:
            log.debug("runtime_status_stale session=%s status_account=%s session_account=%s",
                      session_id, account_id, row_account)
            return None
        return {**payload, "accountGeneration": revision}

    async def _notify(self, session_id: str) -> None:
        condition = self.conditions.get(session_id)
        if condition is None:
            return
        async with condition:
            condition.notify_all()

    async def flush(self) -> set[str]:
        async with self._flush_lock:
            sessions = self.store.deliver_outbox()
            for session_id in sessions:
                if self.subscribers.get(session_id, 0) > 0:  # a stream that starts later reads the table first
                    self.persisted_marks[session_id] = self.persisted_marks.get(session_id, 0) + 1
        for session_id in sessions:
            await self._notify(session_id)
        return sessions

    async def publish(self, session_id: str, *, kind: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.stage(session_id, kind=kind, name=name, payload=payload)
        await self.flush()
        return event

    def prepare_market(self, payload: dict[str, Any]) -> PreparedMarket:
        # A source revision is immutable and shared across sessions. Validate and
        # encode its large bars array once; each stream still owns its envelope.
        return self.prepare_frame("feed.market", payload)

    def prepare_frame(self, name: str, payload: dict[str, Any]) -> PreparedMarket:
        """One shared payload (a market revision, a board frame or patch) validated and encoded once
        for every session it goes to (2026-10-12: the board fans one frame out to many windows)."""
        if name not in PREPARED_EVENTS:
            raise ValueError("invalid_prepared_market")
        validate_event({"schemaVersion": SCHEMA_VERSION, "id": "market", "sessionId": "market",
                        "kind": "result", "name": name, "timestamp": utc_now(), "payload": payload})
        return PreparedMarket(payload, fast_json.dumps(payload))

    async def publish_transient(
        self, session_id: str, *, kind: str, name: str, payload: dict[str, Any],
        prepared_market: PreparedMarket | None = None,
    ) -> dict[str, Any]:
        if name == "runtime.status":
            stamped = self._with_account_revision(session_id, payload)
            if stamped is None:   # stale (another switch landed since it was built): not sent
                return {"schemaVersion": SCHEMA_VERSION, "id": str(uuid4()), "sessionId": session_id,
                        "kind": kind, "name": name, "timestamp": utc_now(), "payload": payload}
            payload = stamped
        if name == "runtime.status" and self.present_runtime_status is not None:
            payload = self.present_runtime_status(payload)  # the reader's first status, as stage() does
        event = {"schemaVersion": SCHEMA_VERSION, "id": str(uuid4()), "sessionId": session_id,
                 "kind": kind, "name": name, "timestamp": utc_now(), "payload": payload}
        if prepared_market is None:
            validate_event(event)
        elif name not in PREPARED_EVENTS or kind != "result" or prepared_market.payload is not payload:
            raise ValueError("invalid_prepared_market")
        elif name not in MARKET_SLOT_EVENTS:
            event = _PreparedEvent(event)
            event.prepared = prepared_market
        if self.subscribers.get(session_id, 0) > 0:
            sequence = self.transient_sequences.get(session_id, 0) + 1
            self.transient_sequences[session_id] = sequence
            if name in MARKET_SLOT_EVENTS or (name == "feed.status" and payload.get("channel") == "market"):
                # One market slot: a slow reader gets the latest only. A patch whose base it
                # never received makes the client ask for a full frame (feed focus) again.
                self.latest_market[session_id] = (sequence, event)
                if name == "feed.market":
                    self.market_frames[session_id] = prepared_market or self.prepare_market(payload)
                elif prepared_market is not None:
                    self.market_frames[session_id] = prepared_market
                else:
                    self.market_frames.pop(session_id, None)
            else:
                queue = self.transient_events.setdefault(session_id, deque(maxlen=256))
                if len(queue) == queue.maxlen:
                    # A frame falls off the ring for every reader still behind it. The record
                    # keeps growing while readers lag; clearing it on release/disconnect.
                    self.transient_evicted[session_id] = queue[0][0]
                queue.append((sequence, event))
            await self._notify(session_id)
        return event

    def clear_market(self, session_id: str) -> None:
        self.latest_market.pop(session_id, None)
        self.market_frames.pop(session_id, None)

    def _transients_after(self, session_id: str, cursor: int) -> list[tuple[int, dict[str, Any]]]:
        # The ring is in sequence order: walk back from the newest only as far as the cursor (a caught-up
        # reader looks at one item, not the whole 256-item ring, on every wake — 2026-10-13).
        items: list[tuple[int, dict[str, Any]]] = []
        for item in reversed(self.transient_events.get(session_id, ())):
            if item[0] <= cursor:
                break
            items.append(item)
        items.reverse()
        market = self.latest_market.get(session_id)
        if market is not None and market[0] > cursor:
            items.append(market)
            items.sort(key=lambda item: item[0])
        return items

    def stream_generation(self, session_id: str) -> int:
        """Counts SSE readers ever attached to the session; a new reader starts with no feed state."""
        return self.stream_generations.get(session_id, 0)

    def subscribed_session_ids(self) -> tuple[str, ...]:
        """Return only sessions with a currently attached SSE reader."""
        return tuple(
            session_id for session_id, count in self.subscribers.items()
            if count > 0 and session_id not in self._released_sessions
        )

    async def start(self) -> None:
        await self.flush()
        if self._outbox_task is None or self._outbox_task.done():
            self._outbox_task = asyncio.create_task(self._outbox_loop())

    async def _outbox_loop(self) -> None:
        while not self._closing:
            try:
                await self.flush()
            except Exception:
                # A later iteration or process restart retries every undelivered row.
                pass
            await asyncio.sleep(0.25)

    async def shutdown(self) -> None:
        """Stops the outbox and every open reader: a reader woken after the store closes must end its
        stream, not read the closed database (#530: a closed replay's SSE readers raised
        sqlite3.ProgrammingError)."""
        self._closing = True
        for condition in list(self.conditions.values()):
            async with condition:
                condition.notify_all()
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            await asyncio.gather(self._outbox_task, return_exceptions=True)
            self._outbox_task = None

    async def release_session(self, session_id: str) -> None:
        condition = self.conditions.get(session_id)
        if self.subscribers.get(session_id, 0) > 0:
            self._released_sessions.add(session_id)
            if self.on_subscribers_changed is not None:
                self.on_subscribers_changed(session_id)
            if condition is not None:
                async with condition:
                    condition.notify_all()
            return
        self.conditions.pop(session_id, None)
        self.clear_market(session_id)
        self.transient_events.pop(session_id, None)
        self.transient_sequences.pop(session_id, None)
        self.transient_evicted.pop(session_id, None)
        self.persisted_marks.pop(session_id, None)
        self.subscribers.pop(session_id, None)
        self._released_sessions.discard(session_id)

    async def stream(self, session_id: str, request: Request, last_sequence: int = 0,
                     *, authorize: Callable[[], Awaitable[None]],
                     on_subscribed: Callable[[], Awaitable[Any]] | None = None) -> AsyncIterator[str]:
        condition = self.conditions.setdefault(session_id, asyncio.Condition())
        self.subscribers[session_id] = self.subscribers.get(session_id, 0) + 1
        self.stream_generations[session_id] = self.stream_generations.get(session_id, 0) + 1
        if self.on_subscribers_changed is not None:
            self.on_subscribers_changed(session_id)
        cursor = last_sequence
        transient_cursor = self.transient_sequences.get(session_id, 0)
        # The newest sequence when this reader last got a resend: a resend answers every frame dropped
        # up to it, so only a frame dropped after it needs another one (#515).
        resent_through: int | None = None

        def lagging_transients() -> list[tuple[int, dict[str, Any]]]:
            # This reader fell behind the ring's start: frames it never saw were dropped, so
            # it gets a generation bump — the feed then clears its last_sent and resends
            # every channel in full — instead of touch frames that would dress old values
            # up as fresh (#515 F-f). One bump answers everything dropped so far; if the ring
            # then drops a frame published after that bump (the resend itself, when the reader
            # stays slow), the reader has missed it too and is due another (#515: once per
            # lagging episode left it on old values, refreshed by touches, for good).
            nonlocal resent_through
            evicted = self.transient_evicted.get(session_id, 0)
            if transient_cursor < evicted and (resent_through is None or evicted > resent_through):
                resent_through = self.transient_sequences.get(session_id, 0)
                self.stream_generations[session_id] = self.stream_generations.get(session_id, 0) + 1
            return self._transients_after(session_id, transient_cursor)

        # 扩容① (2026-10-13): the caller's authorization (a store read and a token check) and the
        # connection are re-checked once per `recheck_seconds`, not before every event; the waits
        # below end when the next check is due, so a revocation ends the stream within that delay.
        recheck = self.recheck_seconds
        checked_at: float | None = None
        listed_at: float | None = None
        listed_mark: int | None = None

        async def still_allowed() -> bool:
            nonlocal checked_at
            if self._closing:
                return False
            if checked_at is not None and monotonic() - checked_at < recheck:
                return True
            if session_id in self._released_sessions or await request.is_disconnected():
                return False
            await authorize()
            checked_at = monotonic()
            return True

        def persisted() -> list[tuple[int, dict[str, Any]]]:
            # The events table is read only when this process delivered something to the session
            # since the last read, and otherwise once per `recheck` (a second process sharing the
            # database delivers without touching this one's marks).
            nonlocal listed_at, listed_mark
            if self._closing:
                return []  # the store may already be closed; still_allowed() ends the stream
            mark = self.persisted_marks.get(session_id, 0)
            now = monotonic()
            if listed_mark == mark and listed_at is not None and now - listed_at < recheck:
                return []
            listed_at, listed_mark = now, mark
            return self.store.list_events(session_id, after_sequence=cursor)

        try:
            if on_subscribed is not None:
                # Runs with this reader already counted, so a transient published
                # here passes the subscribers>0 guard and lands in the first batch.
                await on_subscribed()
            while session_id not in self._released_sessions:
                if not await still_allowed():  # also ends every reader once the broker shuts down
                    return
                batch = persisted()
                transient = lagging_transients()
                if not batch and not transient:
                    # Wake on a publish, or when the next check is due.
                    due = recheck - (monotonic() - (checked_at or 0.0))
                    async with condition:
                        try:
                            await asyncio.wait_for(condition.wait(), timeout=min(recheck, max(0.01, due)))
                        except TimeoutError:
                            pass
                    batch = persisted()
                    transient = lagging_transients()
                if session_id in self._released_sessions or not await still_allowed():
                    return
                if not batch and not transient:
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                # Persisted history first, current transients last: a (re)connecting
                # reader replays the backlog — e.g. a replay's startup "connecting"
                # statuses — and must end on the live snapshot, not on the oldest
                # stored state (2026-09-24: the replay chart never loaded).
                for sequence, event in batch:
                    if not await still_allowed():
                        return
                    cursor = sequence
                    yield f"id: {sequence}\ndata: {fast_json.dumps(event)}\n\n"
                # Re-read after a slow persisted replay so market is the latest full snapshot.
                transient = lagging_transients()
                for transient_sequence, event in transient:
                    if not await still_allowed():
                        return
                    if event["name"] in MARKET_SLOT_EVENTS or (event["name"] == "feed.status" and event["payload"].get("channel") == "market"):
                        latest = self.latest_market.get(session_id)
                        if latest is None:
                            continue
                        _, event = latest
                    transient_cursor = transient_sequence
                    prepared = (self.market_frames.get(session_id) if event["name"] in MARKET_SLOT_EVENTS
                                else getattr(event, "prepared", None))
                    try:
                        if prepared is not None and prepared.payload is event["payload"]:
                            envelope = {key: value for key, value in event.items() if key != "payload"}
                            data = fast_json.dumps(envelope)[:-1] + ',"payload":' + prepared.encoded + '}'
                        else:
                            data = fast_json.dumps(event)
                    except (TypeError, ValueError) as error:
                        # 一条序列化不了的瞬时事件绝不剪断整条流（到价提醒的 Decimal 进过
                        # lastPrice 的教训）：丢弃这一条、保住其余事件与心跳。
                        log.warning("dropped unserializable transient %s: %s", event["name"], error)
                        continue
                    yield f"event: transient\ndata: {data}\n\n"
        except HTTPException as error:
            if error.status_code not in {401, 403, 404}:
                raise
            # Headers are already sent. End this stream, without deleting chat
            # history or revoking independently authorized background protection.
        finally:
            remaining = self.subscribers.get(session_id, 1) - 1
            if remaining <= 0:
                self.subscribers.pop(session_id, None)
                self.clear_market(session_id)
                self.transient_events.pop(session_id, None)
                self.transient_sequences.pop(session_id, None)
                self.transient_evicted.pop(session_id, None)
                self.persisted_marks.pop(session_id, None)
                if self.conditions.get(session_id) is condition:
                    self.conditions.pop(session_id, None)
                self._released_sessions.discard(session_id)
            else:
                self.subscribers[session_id] = remaining
            if self.on_subscribers_changed is not None:
                self.on_subscribers_changed(session_id)
