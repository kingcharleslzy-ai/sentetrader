"""Push account, market, activity and AI-execution snapshots over SSE.

The desktop used to poll six endpoints every 1–3 seconds per open window.
The feed publishes the same payloads as transient events to every session
with an attached SSE reader, only when the payload changed, throttled per
kind. Clients keep polling only as a fallback while the stream is down.

2026-10-12 报价总表推送与“只推看得见的”: the board (`feed.board` / `feed.board-patch`) is read once
per second from the shared market for every session that asked for it (PUT .../feed/view), diffed
once against the previous read and fanned out; each distinct row filter's frame is built and encoded
once. A session whose page is hidden gets no chart market and no board until it is visible again,
then one full frame of each; account, activity, execution and every event outside this loop
(orders, alerts, reminders, signals, AI results) keep flowing.

2026-10-13 扩容①: what many sessions receive identically is built once — a market patch once per source
revision, an account / activity / execution frame once per tick and read key (its digest, validation and
encoding) — and a focus may carry `since` (the last closed bar the client holds, #816): the first chart
frame then carries only the bars after it, with the window proof the HTTP read gives (`barsAfter`,
`barsWindow`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from . import fast_json
from .broker import EventBroker
from .shared_market import workbench_since

log = logging.getLogger("uvicorn.error.agent")

FEED_EVENTS = {
    "feed.account": 1.0,    # at most once per second
    "feed.market": 1.0,
    "feed.activity": 1.0,
    "feed.execution": 1.0,
}
# With a snapshot source these are re-read only after the provider refreshed its snapshot.
SNAPSHOT_GATED = {"feed.account", "feed.market", "feed.activity"}
# The channel a client tracks separately per feed event; a failed read is reported
# on "feed.status" for that channel alone, never by silence on all of them.
FEED_CHANNELS = {
    "feed.account": "account", "feed.market": "market",
    "feed.activity": "activity", "feed.execution": "execution",
}
DEFAULT_TICK_SECONDS = 0.5
# An unchanged payload is still re-sent this often so ageSeconds/capturedAt stay current on the client.
REFRESH_SECONDS = 5.0
VOLATILE_KEYS = {"ageSeconds", "capturedAt"}
# First-frame `since` answers kept per market packet (Codex review #850: bounded).
SINCE_ANSWERS_PER_PACKET = 16
# Channels whose unchanged refresh may be a feed.touch (the market has its own patches).
TOUCHABLE = {"feed.account", "feed.activity", "feed.execution"}
MARKET_DURATIONS = {60, 180, 300, 900, 1800, 3600, 86400, 604800}
# How long a session → user lookup (a store read) is reused. Every account snapshot wakes a tick,
# and each tick and market watcher re-checks every streaming session: uncached, 100 site-hosted
# paper accounts made that ~10,000 SQLite reads a second, the Runtime's top cost (2026-10-03
# load test). A session's user never changes; its end arrives in memory first (release →
# subscribed_session_ids, forget_session), so the store read only catches expiry, within a second.
SESSION_USER_TTL_SECONDS = 1.0
# 报价总表 push cadence: the shared board cache lives one second, so one read a second is every
# change the Runtime can see; the HTTP board read and the push share that one cache.
BOARD_TICK_SECONDS = 1.0
# Board meta that moves on every read without a business change (the sidecar's own revision marker
# included): carried in every patch, never a reason for a new board revision.
BOARD_VOLATILE = {"capturedAt", "ageSeconds", "revision"}
REASON_CODE = re.compile(r"^[a-z0-9_]+$")


class FeedReadError(Exception):
    """A channel's read failed. The client is told so on that channel instead of
    being left holding the last successful payload while other channels tick on."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class FeedFocus:
    symbol: str | None = None
    duration: int = 60
    # The client applies feed.market-patch; otherwise every change is a full feed.market.
    market_patches: bool = False
    # The client applies feed.touch: an unchanged frame's refresh sends its capture stamps only.
    feed_touch: bool = False
    # The last closed bar the client holds for this chart (#816): the focus's first chart frame
    # carries only the bars after it (see shared_market.workbench_since). None = the whole window.
    # Spent by that first frame (#850): every later full frame of the focus is the whole window.
    since: int | None = None


@dataclass(frozen=True)
class BoardWant:
    """The board rows one window shows (2026-10-12): everything, or the rows of some exchanges,
    products ("au" or "SHFE.au") and contracts. `key` is the client's own name for the set; every
    board frame echoes it so the client knows the push covers what it asked for."""
    everything: bool = False
    exchanges: frozenset[str] = frozenset()
    products: frozenset[str] = frozenset()
    symbols: frozenset[str] = frozenset()
    key: str = ""

    @classmethod
    def of(cls, *, everything: bool = False, exchanges=(), products=(), symbols=(), key: str = "") -> BoardWant:
        return cls(everything=everything, exchanges=frozenset(str(item).upper() for item in exchanges),
                   products=frozenset(str(item).lower() for item in products),
                   symbols=frozenset(str(item).upper() for item in symbols), key=key)

    def admits(self, row: dict[str, Any]) -> bool:
        if self.everything:
            return True
        exchange = str(row.get("exchangeId") or "").upper()
        product = str(row.get("productId") or "").lower()
        return (exchange in self.exchanges or product in self.products
                or f"{exchange.lower()}.{product}" in self.products
                or str(row.get("symbol") or "").upper() in self.symbols)


@dataclass
class _BoardFrame:
    """One board read the push holds: rows by symbol in board order, and what changed since the
    previous revision (`changes` None = rows added, removed or reordered: full frames only)."""
    id: str
    previous: str | None
    meta: dict[str, Any]
    rows: dict[str, dict[str, Any]]
    changes: dict[str, dict[str, Any]] | None
    meta_changes: dict[str, Any]
    raw: Any = None


@dataclass
class _Frame:
    """One tick's read for one key, shared by every session it goes to: its digest, and its
    validation + encoding and feed.touch stamps once the first session needs them."""
    payload: dict[str, Any]
    digest: str
    prepared: Any = None
    stamps: list[list[Any]] | None = None


@dataclass
class _Sent:
    checked: float      # monotonic of the last read (throttle)
    published: float    # monotonic of the last event actually sent
    digest: str


@dataclass
class _SessionState:
    user_id: str
    account_id: str | None = None  # the trading account the session acts on
    focus: FeedFocus = field(default_factory=FeedFocus)
    last_sent: dict[str, _Sent] = field(default_factory=dict)
    read_version: dict[str, int] = field(default_factory=dict)  # name -> snapshot version of the last read
    stream_generation: int = -1  # broker reader generation the state was built for
    # PUT .../feed/view (2026-10-12): a hidden page gets no chart market or board; `board` is the
    # rows it shows (None = no board on screen). Older clients never send it: visible, no board.
    visible: bool = True
    board: BoardWant | None = None
    board_held: str | None = None      # the board revision this session's client holds in full
    board_generation: int = -1
    board_published: float = 0.0
    board_error: str | None = None     # the unavailability last reported on the board channel
    view_epoch: int = 0


class RuntimeFeed:
    """One loop for the whole Runtime; per-session focus decides which market to push."""

    def __init__(
        self, *, broker: EventBroker, session_user: Callable[[str], str | None],
        account_activity: Callable[[str | None], Awaitable[dict[str, Any] | None]],
        account_overview: Callable[[str | None, str, dict[str, Any]], Awaitable[dict[str, Any] | None]],
        market_workbench: Callable[[str | None, str, int], Awaitable[dict[str, Any] | None]],
        activity: Callable[[str | None, str, str, dict[str, Any]], Awaitable[dict[str, Any] | None]],
        execution: Callable[[str | None, str], Awaitable[dict[str, Any] | None]],
        session_account: Callable[[str], str | None] | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        snapshot_gated: bool = False,
        shared_market: Callable[[], Any] | None = None,
    ) -> None:
        self.broker = broker
        self._shared_market = shared_market
        self._market_tasks: dict[str, asyncio.Task[None]] = {}
        self._retiring_market_tasks: set[asyncio.Task[None]] = set()
        if shared_market is not None:
            broker.on_subscribers_changed = self._restart_market
        self._lookup_session_user = session_user
        self._session_users: dict[str, tuple[float, str | None]] = {}
        self._session_account = session_account
        self._account_activity = account_activity
        self._account_overview = account_overview
        self._market_workbench = market_workbench
        self._activity = activity
        self._execution = execution
        self.tick_seconds = tick_seconds
        # Without a snapshot source (fake provider) the time throttle alone applies.
        self._snapshot_gated = snapshot_gated
        self._snapshot_version = 0
        self._round_shared: dict[Any, Any] | None = None
        self._sessions: dict[str, _SessionState] = {}
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._board_task: asyncio.Task[None] | None = None
        self._board_wake = asyncio.Event()
        self._board_current: _BoardFrame | None = None

    # -- focus ---------------------------------------------------------------

    def set_focus(self, session_id: str, *, symbol: str | None, duration: int, market_patches: bool = False,
                  feed_touch: bool = False, since: int | None = None) -> FeedFocus:
        if duration not in MARKET_DURATIONS:
            raise ValueError("invalid_market_duration")
        state = self._state(session_id)
        if state is None:
            raise KeyError(session_id)
        state.focus = FeedFocus(symbol=symbol, duration=duration, market_patches=market_patches, feed_touch=feed_touch,
                                since=since)
        # A new focus is served on the next tick regardless of throttle or snapshot gate.
        for name in ("feed.market", "feed.activity"):
            state.last_sent.pop(name, None)
            state.read_version.pop(name, None)
        self._restart_market(session_id)
        self._wake.set()
        return state.focus

    def focus(self, session_id: str) -> FeedFocus | None:
        state = self._sessions.get(session_id)
        return state.focus if state else None

    def set_view(self, session_id: str, *, visible: bool, board: BoardWant | None) -> _SessionState:
        """What the session's window shows right now (2026-10-12): whether the page is visible and
        which board rows are on screen. Every call starts the board over with one full frame (the
        client's resync); a page coming back gets one full chart frame too."""
        state = self._state(session_id)
        if state is None:
            raise KeyError(session_id)
        shown = visible and not state.visible
        hidden = state.visible and not visible
        state.visible = visible
        state.board = board
        state.board_held = None
        state.board_error = None
        state.view_epoch += 1
        if shown or hidden:
            for name in ("feed.market",):
                state.last_sent.pop(name, None)
                state.read_version.pop(name, None)
            self._restart_market(session_id)
        self._board_wake.set()
        self._wake.set()
        return state

    def forget_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._session_users.pop(session_id, None)
        self._cancel_market(session_id)

    def session_account_changed(self, session_id: str, account_id: str) -> None:
        """A session switched its trading account (4.4a): the feed state keeps its
        focus but follows the new account from the next tick on — the per-event
        throttles and snapshot gates are dropped so the first tick after the
        switch re-reads everything from the new account's provider. A session
        with no state yet (nobody streaming) reads the store on its next tick."""
        state = self._sessions.get(session_id)
        if state is not None and state.account_id != account_id:
            state.account_id = account_id
            self._restart_market(session_id)
            for name in FEED_EVENTS:
                state.last_sent.pop(name, None)
                state.read_version.pop(name, None)
        self._wake.set()

    def _session_user(self, session_id: str) -> str | None:
        now = monotonic()
        cached = self._session_users.get(session_id)
        if cached is not None and now - cached[0] < SESSION_USER_TTL_SECONDS:
            return cached[1]
        user_id = self._lookup_session_user(session_id)
        self._session_users[session_id] = (now, user_id)
        return user_id

    def _state(self, session_id: str) -> _SessionState | None:
        state = self._sessions.get(session_id)
        if state is None:
            user_id = self._session_user(session_id)
            if user_id is None:
                return None
            account_id = self._session_account(session_id) if self._session_account is not None else None
            state = _SessionState(user_id=user_id, account_id=account_id)
            self._sessions[session_id] = state
        return state

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.create_task(self._run())
        if self._board_task is None or self._board_task.done():
            self._board_task = asyncio.create_task(self._run_board())

    async def stop(self) -> None:
        self._closing = True
        self._wake.set()
        self._board_wake.set()
        for task in (self._task, self._board_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._task = self._board_task = None
        for session_id in list(self._market_tasks):
            self._cancel_market(session_id)
        await asyncio.gather(*self._retiring_market_tasks, return_exceptions=True)

    def nudge(self) -> None:
        """Run a tick now (a UI command was queued)."""
        self._wake.set()

    def snapshot_arrived(self) -> None:
        """The provider refreshed its snapshot, or an order link changed: gated reads are due."""
        self._snapshot_version += 1
        self._wake.set()

    @property
    def snapshot_version(self) -> int:
        """Bumped by every provider snapshot and order-link change (read-only for the wiring)."""
        return self._snapshot_version

    @property
    def round_shared(self) -> dict[Any, Any] | None:
        """Scratch the wiring may share between the reads of one tick (#862: the activity base of an
        account), None outside a tick. It is dropped when the tick ends, so nothing a round built
        outlives it — a reader who left holds no account's book in memory."""
        return self._round_shared

    @contextmanager
    def _round(self) -> Iterator[dict[Any, Any]]:
        previous, self._round_shared = self._round_shared, {}
        try:
            yield self._round_shared
        finally:
            self._round_shared = previous

    def orders_changed(self) -> None:
        """Thread-safe variant for the order service: bump from any thread, wake on the loop."""
        self._snapshot_version += 1
        loop = self._loop
        if loop is None:
            self._wake.set()
        elif not loop.is_closed():
            loop.call_soon_threadsafe(self._wake.set)

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self.tick()
            except Exception:
                log.exception("runtime_feed_tick_failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass
            self._wake.clear()

    def _cancel_market(self, session_id: str) -> None:
        clear_market = getattr(self.broker, "clear_market", None)
        if clear_market is not None:
            clear_market(session_id)
        task = self._market_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
            self._retiring_market_tasks.add(task)
            task.add_done_callback(self._retiring_market_tasks.discard)

    def _restart_market(self, session_id: str) -> None:
        # A reader attached or left: judge the session against the store again.
        self._session_users.pop(session_id, None)
        self._cancel_market(session_id)
        self._sync_market(session_id)

    def _sync_market(self, session_id: str) -> None:
        if self._shared_market is None:
            return
        active = not self._closing and session_id in self.broker.subscribed_session_ids()
        state = self._state(session_id) if active else None
        if (state is None or not state.focus.symbol or not state.visible
                or self._session_user(session_id) != state.user_id):
            self._cancel_market(session_id)
            return
        task = self._market_tasks.get(session_id)
        if task is None or task.done():
            self._market_tasks[session_id] = asyncio.create_task(self._watch_market(session_id, state))

    async def _watch_market(self, session_id: str, state: _SessionState) -> None:
        # This reader never waits for private account snapshots or the polling tick.
        focus = state.focus
        while not self._closing:
            if (self._sessions.get(session_id) is not state or state.focus is not focus or not state.visible
                    or self._session_user(session_id) != state.user_id
                    or session_id not in self.broker.subscribed_session_ids()):
                return
            service = self._shared_market() if self._shared_market is not None else None
            channel = None
            try:
                if service is None:
                    raise FeedReadError("market_unavailable")
                channel = await service.acquire_market(focus.symbol, focus.duration)
                previous = None
                sent_id = None  # the packet this session's client holds in full, for patches
                while not self._closing:
                    if (self._sessions.get(session_id) is not state or state.focus is not focus or not state.visible
                            or self._session_user(session_id) != state.user_id
                            or session_id not in self.broker.subscribed_session_ids()):
                        return
                    changed = channel.changed
                    packet = channel.packet
                    generation = self.broker.stream_generation(session_id)
                    marker = (packet["id"] if packet is not None else None, generation, channel.error is not None)
                    if marker != previous:
                        if channel.error is not None:
                            sent_id = None
                            await self.broker.publish_transient(
                                session_id, kind="result", name="feed.status",
                                payload={"channel": "market", "status": "unavailable", "reason": "market_unavailable"},
                            )
                        elif (packet is not None and focus.market_patches and sent_id is not None
                              and previous is not None and previous[1] == generation
                              and packet.get("previous") == sent_id and packet.get("patch")):
                            # Only what changed since the frame the client holds (~1 KB, not ~40 KB).
                            # Every session on this channel gets the same patch: built, validated and
                            # encoded once per source revision (2026-10-13).
                            patch = packet.get("member_patch")
                            if patch is None:
                                workbench = packet["value"]["workbench"]
                                changes = {**packet["patch"].get("workbench", {}),
                                           **{key: workbench[key] for key in ("capturedAt", "ageSeconds") if key in workbench}}
                                payload = {"revision": packet["id"], "base": packet["previous"],
                                           "durationSeconds": focus.duration, "changes": changes}
                                prepare_frame = getattr(self.broker, "prepare_frame", None)
                                patch = prepare_frame("feed.market-patch", payload) if prepare_frame is not None else payload
                                packet["member_patch"] = patch
                            if isinstance(patch, dict):
                                await self.broker.publish_transient(
                                    session_id, kind="result", name="feed.market-patch", payload=patch)
                            else:
                                await self.broker.publish_transient(
                                    session_id, kind="result", name="feed.market-patch",
                                    payload=patch.payload, prepared_market=patch)
                            sent_id = packet["id"]
                        elif packet is not None:
                            prepare = getattr(self.broker, "prepare_market", None)
                            if prepare is not None:
                                if "member_frame" not in packet:
                                    packet["member_frame"] = prepare({**packet["value"]["workbench"], "liveTrading": "disabled",
                                                                      "revision": packet["id"]})
                                frame = packet["member_frame"]
                                if focus.since is not None and sent_id is None and previous is None:
                                    # The focus's first frame: only the bars after the client's last
                                    # closed one, one encoding per packet and `since` (#816).
                                    frame = self._since_frame(packet, frame, focus.since, prepare)
                                await self.broker.publish_transient(
                                    session_id, kind="result", name="feed.market",
                                    payload=frame.payload, prepared_market=frame,
                                )
                            else:
                                await self.broker.publish_transient(
                                    session_id, kind="result", name="feed.market",
                                    payload={**packet["value"]["workbench"], "liveTrading": "disabled", "revision": packet["id"]},
                                )
                            sent_id = packet["id"]
                            # The hint answered the focus's first frame (#850): a watcher restarted later
                            # (a reattached reader, a recovered channel) sends the whole window — the
                            # reader then may not hold that prefix.
                            focus.since = None
                        previous = marker
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                if (self._sessions.get(session_id) is not state or state.focus is not focus
                        or self._session_user(session_id) != state.user_id
                        or session_id not in self.broker.subscribed_session_ids()):
                    return
                await self.broker.publish_transient(
                    session_id, kind="result", name="feed.status",
                    payload={"channel": "market", "status": "unavailable", "reason": "market_unavailable"},
                )
                await asyncio.sleep(0.5)
            finally:
                if channel is not None and service is not None:
                    service.release_market(channel)

    @staticmethod
    def _since_frame(packet: dict[str, Any], frame: Any, since: int, prepare: Callable[[dict[str, Any]], Any]) -> Any:
        answers = packet.setdefault("member_since", {})
        if since in answers:
            return answers[since]
        answer = workbench_since(frame.payload, since)
        # A `since` the window does not hold answers the whole window: that is the shared frame (not cached).
        if answer is frame.payload:
            return frame
        prepared = prepare(answer)
        # Readers of one chart hold nearly the same last bar; a few distinct `since` per packet are
        # the norm, so the cache is capped and a burst of distinct values is answered uncached.
        if len(answers) < SINCE_ANSWERS_PER_PACKET:
            answers[since] = prepared
        return prepared

    # -- 报价总表 push (2026-10-12) ------------------------------------------

    async def _run_board(self) -> None:
        while not self._closing:
            try:
                await self.board_round()
            except Exception:
                log.exception("runtime_feed_board_round_failed")
            try:
                await asyncio.wait_for(self._board_wake.wait(), timeout=BOARD_TICK_SECONDS)
            except TimeoutError:
                pass
            self._board_wake.clear()

    def _board_audience(self) -> list[tuple[str, _SessionState]]:
        audience = []
        for session_id in self.broker.subscribed_session_ids():
            state = self._sessions.get(session_id)
            if (state is not None and state.board is not None and state.visible
                    and self._session_user(session_id) == state.user_id):
                audience.append((session_id, state))
        return audience

    def _advance_board(self, raw: dict[str, Any]) -> _BoardFrame:
        """The board as one revision chain shared by every session: a read with the same business
        content keeps the revision (only its stamps move); a changed read gets a new revision and
        its changes against the previous one, computed here once for everyone."""
        current = self._board_current
        if current is not None and current.raw is raw:
            return current  # the shared one-second cache answered with the same read
        rows: dict[str, dict[str, Any]] = {}
        for row in raw.get("rows", []):
            if isinstance(row, dict) and row.get("symbol"):
                rows[str(row["symbol"])] = row
        meta = {key: value for key, value in raw.items() if key != "rows"}
        if current is None:
            frame = _BoardFrame(secrets.token_hex(8), None, meta, rows, None, {}, raw)
            self._board_current = frame
            return frame
        meta_changes = {key: meta.get(key) for key in set(meta) | set(current.meta)
                        if key not in BOARD_VOLATILE and meta.get(key) != current.meta.get(key)}
        changes: dict[str, dict[str, Any]] | None = None
        if list(rows) == list(current.rows):
            changes = {}
            for symbol, row in rows.items():
                old = current.rows[symbol]
                if row != old:
                    changes[symbol] = {key: row.get(key) for key in set(row) | set(old) if row.get(key) != old.get(key)}
        if changes == {} and not meta_changes:
            current.meta, current.raw = meta, raw  # same content: only the stamps are new
            return current
        frame = _BoardFrame(secrets.token_hex(8), current.id, meta, rows, changes, meta_changes, raw)
        self._board_current = frame
        return frame

    def _board_payload(self, kind: str, frame: _BoardFrame, want: BoardWant) -> tuple[str, dict[str, Any]]:
        stamps = {key: frame.meta[key] for key in BOARD_VOLATILE if key in frame.meta}
        if kind == "full":
            return "feed.board", {**frame.meta, "feedRevision": frame.id, "key": want.key,
                                  "rows": [row for row in frame.rows.values() if want.admits(row)]}
        if kind == "patch":
            rows = {symbol: change for symbol, change in (frame.changes or {}).items() if want.admits(frame.rows[symbol])}
            return "feed.board-patch", {"feedRevision": frame.id, "base": frame.previous, "key": want.key,
                                        "meta": {**frame.meta_changes, **stamps}, "rows": rows}
        # refresh: nothing changed for a while; the stamps keep the client's age honest
        return "feed.board-patch", {"feedRevision": frame.id, "base": frame.id, "key": want.key,
                                    "meta": stamps, "rows": {}}

    async def board_round(self) -> int:
        """One board read for every session showing the board; returns events sent."""
        audience = self._board_audience()
        if not audience:
            return 0
        service = self._shared_market() if self._shared_market is not None else None
        error: str | None = None
        raw: Any = None
        if service is None:
            error = "market_board_unavailable"
        else:
            try:
                raw = await service.board()
            except Exception as failure:  # noqa: BLE001 - every failure is reported on the channel
                detail = getattr(failure, "detail", None)
                error = detail if isinstance(detail, str) and REASON_CODE.fullmatch(detail) else "market_board_unavailable"
            if error is None and not isinstance(raw, dict):
                error = "market_board_unavailable"
        sent = 0
        if error is not None:
            self._board_current = None
            for session_id, state in audience:
                generation = self.broker.stream_generation(session_id)
                if state.board_error == error and generation == state.board_generation:
                    continue
                await self.broker.publish_transient(
                    session_id, kind="result", name="feed.status",
                    payload={"channel": "board", "status": "unavailable", "reason": error})
                state.board_error, state.board_held, state.board_generation = error, None, generation
                sent += 1
            return sent
        frame = self._advance_board(raw)
        now = monotonic()
        built: dict[tuple[Any, ...], tuple[str, dict[str, Any], Any]] = {}
        prepare = getattr(self.broker, "prepare_frame", None)
        for session_id, state in audience:
            want = state.board
            if want is None or self._sessions.get(session_id) is not state:
                continue
            generation = self.broker.stream_generation(session_id)
            if generation != state.board_generation:
                state.board_held, state.board_generation = None, generation  # a new reader starts empty
            if state.board_held == frame.id:
                if now - state.board_published < REFRESH_SECONDS:
                    continue
                kind = "refresh"
            elif state.board_held is not None and state.board_held == frame.previous and frame.changes is not None:
                kind = "patch"
            else:
                kind = "full"
            group = (kind, want.everything, want.exchanges, want.products, want.symbols, want.key)
            if group not in built:
                # Built and encoded once per distinct row filter, whatever the number of windows.
                name, payload = self._board_payload(kind, frame, want)
                built[group] = (name, payload, prepare(name, payload) if prepare is not None else None)
            name, payload, prepared = built[group]
            epoch = state.view_epoch
            if prepared is not None:
                await self.broker.publish_transient(session_id, kind="result", name=name, payload=payload,
                                                    prepared_market=prepared)
            else:
                await self.broker.publish_transient(session_id, kind="result", name=name, payload=payload)
            sent += 1
            if state.view_epoch == epoch:
                # A view change during the send asked for a full frame again; it keeps that request.
                state.board_held, state.board_published, state.board_error = frame.id, now, None
        return sent

    # -- one tick ------------------------------------------------------------

    async def tick(self) -> int:
        """Publish changed snapshots to every attached session; returns events sent."""
        with self._round():
            return await self._tick_round()

    async def _tick_round(self) -> int:
        sent = 0
        for session_id in set(self.broker.subscribed_session_ids()) | set(self._market_tasks):
            self._sync_market(session_id)
        subscribed = set(self.broker.subscribed_session_ids())
        for session_id in list(self._sessions):
            if session_id not in subscribed:
                # Keep focus across a short reconnect; drop it once the session is gone.
                if self._session_user(session_id) is None:
                    self._sessions.pop(session_id, None)
                    self._session_users.pop(session_id, None)
        # Keys mix channel names with the account (possibly None) and user.
        cache: dict[tuple[str | None, ...], tuple[dict[str, Any] | None, str | None]] = {}
        frames: dict[tuple[str | None, ...], _Frame] = {}
        for session_id in subscribed:
            state = self._state(session_id)
            if state is None:
                continue
            generation = self.broker.stream_generation(session_id)
            if generation != state.stream_generation:
                # A (re)attached reader starts empty: resend everything, changed or not.
                state.last_sent.clear()
                state.read_version.clear()
                state.stream_generation = generation
            user = state.user_id
            account = state.account_id
            symbol, duration = state.focus.symbol, state.focus.duration
            plan: list[tuple[str, tuple[str | None, ...]]] = [
                ("feed.account", ("account", account, user)),
                ("feed.execution", ("execution", account, user)),
            ]
            if symbol:
                if self._shared_market is None and state.visible:
                    plan.append(("feed.market", ("market", account, symbol, str(duration))))
                plan.append(("feed.activity", ("activity", account, user, symbol)))
            for name, key in plan:
                if not self._due(state, name):
                    continue
                if key not in cache:
                    try:
                        cache[key] = (await self._load(name, account, user, symbol, duration, cache), None)
                    except FeedReadError as error:
                        cache[key] = (None, error.reason)
                    except Exception:
                        cache[key] = (None, "read_unavailable")
                if self._sessions.get(session_id) is not state or state.account_id != account:
                    # The read awaited: meanwhile the session may have switched account or been
                    # rebuilt. Publishing now would stamp another account's numbers as this
                    # session's fresh frame — drop this round; the next tick reads as it stands
                    # (#515 F-e).
                    break
                state.read_version[name] = self._snapshot_version
                payload, reason = cache[key]
                if payload is None:
                    # A channel that cannot be read says so on its own channel. Staying
                    # silent left the client showing the last good value as if current.
                    if reason is not None:
                        sent += await self._publish(
                            session_id, state, name,
                            {"channel": FEED_CHANNELS[name], "status": "unavailable", "reason": reason},
                            digest=f"unavailable:{reason}", event_name="feed.status",
                        )
                    continue
                frame = frames.get(key)
                if frame is None or frame.payload is not payload:
                    frame = frames[key] = _Frame(payload, _digest(payload))
                sent += await self._publish(session_id, state, name, payload, digest=frame.digest, shared=frame)
        return sent

    async def _publish(
        self, session_id: str, state: _SessionState, name: str,
        payload: dict[str, Any], *, digest: str | None = None, event_name: str | None = None,
        shared: _Frame | None = None,
    ) -> int:
        """One send slot per channel, whether the send is the snapshot or its
        unavailability, so any transition between the two is a digest change."""
        digest = _digest(payload) if digest is None else digest
        previous = state.last_sent.get(name)
        account = state.account_id  # the session's account at read time; re-checked after the await
        now = monotonic()
        if previous is not None and previous.digest == digest and now - previous.published < REFRESH_SECONDS:
            previous.checked = now
            return 0
        if (previous is not None and previous.digest == digest and event_name is None
                and state.focus.feed_touch and name in TOUCHABLE):
            # Unchanged business content: only the capture stamps moved (a 13 KB activity frame
            # every 5 s became ~100 bytes, 2026-09-28).
            if shared is None:
                stamps = _stamps(payload)
            else:
                if shared.stamps is None:
                    shared.stamps = _stamps(payload)
                stamps = shared.stamps
            await self.broker.publish_transient(
                session_id, kind="result", name="feed.touch", payload={"name": name, "stamps": stamps})
            if state.account_id == account:
                previous.checked = previous.published = now
            return 1
        prepare = getattr(self.broker, "prepare_frame", None) if shared is not None and event_name is None else None
        if prepare is not None and shared is not None and shared.prepared is None:
            try:
                shared.prepared = prepare(name, payload)  # validated and encoded once for every session
            except TypeError:
                # Not encodable (a Decimal slipped in): sent unprepared, so the stream drops this one
                # frame as before instead of the whole tick failing for every session.
                prepare = None
        if prepare is not None and shared is not None and shared.prepared is not None:
            await self.broker.publish_transient(
                session_id, kind="result", name=name, payload=payload, prepared_market=shared.prepared)
        else:
            await self.broker.publish_transient(
                session_id, kind="result", name=event_name or name, payload=payload)
        if state.account_id == account:
            # The frame went out for the account read at entry; only then may the session's
            # last_sent say it is the current one (#515 F-e).
            state.last_sent[name] = _Sent(checked=now, published=now, digest=digest)
        return 1

    async def _load(
        self, name: str, account: str | None, user: str, symbol: str | None, duration: int,
        cache: dict[tuple[str | None, ...], tuple[dict[str, Any] | None, str | None]],
    ) -> dict[str, Any] | None:
        if name == "feed.execution":
            return await self._execution(account, user)
        if name == "feed.market" and symbol:
            return await self._market_workbench(account, symbol, duration)
        if name in {"feed.account", "feed.activity"}:
            # The account activity read is the expensive one: once per tick for everyone
            # on the same account, and its failure is cached too so both channels
            # report the same reason once.
            key = ("activity_raw", account)
            if key not in cache:
                try:
                    cache[key] = (await self._account_activity(account), None)
                except FeedReadError as error:
                    cache[key] = (None, error.reason)
                except Exception:
                    cache[key] = (None, "read_unavailable")
            raw, reason = cache[key]
            if raw is None:
                if reason is not None:
                    raise FeedReadError(reason)
                return None
            if name == "feed.account":
                return await self._account_overview(account, user, raw)
            if symbol:
                return await self._activity(account, user, symbol, raw)
        return None

    def _due(self, state: _SessionState, name: str) -> bool:
        previous = state.last_sent.get(name)
        if previous is None:
            return True
        if monotonic() - previous.checked < FEED_EVENTS[name]:
            return False
        if (
            self._snapshot_gated and name in SNAPSHOT_GATED
            and monotonic() - previous.published < REFRESH_SECONDS
        ):
            # The gate skips reads the provider cannot have changed, but it must not
            # hold a channel silent forever: a provider that stopped refreshing is
            # itself the staleness to report, so re-judge at the refresh cadence.
            return state.read_version.get(name) != self._snapshot_version
        return True

    async def publish_ui_nudge(self, user_id: str, reason: str) -> None:
        """Tell every attached session of a user to pulse now (a UI command or capture waits)."""
        for session_id in self.broker.subscribed_session_ids():
            state = self._state(session_id)
            if state is None or state.user_id != user_id:
                continue
            await self.broker.publish_transient(session_id, kind="result", name="feed.ui", payload={"reason": reason})


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_volatile(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _stamps(value: Any, path: tuple[Any, ...] = ()) -> list[list[Any]]:
    """[[path, value], ...] of every volatile key in a frame: what feed.touch carries."""
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in (
            [[[*path, key], child]] if key in VOLATILE_KEYS else _stamps(child, (*path, key)))]
    if isinstance(value, list):
        return [item for index, child in enumerate(value) for item in _stamps(child, (*path, index))]
    return []


def _digest(payload: dict[str, Any]) -> str:
    """Timestamps and ages tick every read; only business content counts as a change."""
    return fast_json.dumps(_strip_volatile(payload), sort_keys=True, default=str)
