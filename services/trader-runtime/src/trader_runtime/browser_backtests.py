"""规则回测本机优先 (2026-10-12): the server's half of a rule backtest the user's browser computes.

The browser asks ``POST /api/strategy-backtests/inputs`` for everything one backtest reads — the
setup ``prepare_setup`` builds, serialised by ``setup_inputs`` (K-lines, contract figures, fees,
session schedule, initial balance, slippage) — runs ``strategy_lab_engine.run_rule`` over it in
Pyodide with the engine sources of ``GET /api/strategy-engine/bundle`` and posts the report back
(``POST /api/strategy-backtests/browser-result``). The server keeps what it handed out as a
ticket with its own id (``ticketId``, never the inputs digest — two requests over the same inputs
are two tickets): the result is wrapped and stored with the server's own record of the plan, contract
and options, never with anything the browser could have changed, and a result for a ticket the
server no longer holds (restart, expiry) is refused so the browser falls back to the server queue.
Handing a ticket's report in again (a retry after a lost reply) returns the first outcome, never a
second stored result.

A browser number is never an authoritative one: saving a strategy reruns its backtest on the
server, and ``BrowserChecks`` compares that rerun with the browser's result over the same inputs
(the same inputs digest) — a mismatch is logged and marked on the server's result
(``browserCheck``), the server's numbers stand.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from asyncio import Future
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

log = logging.getLogger("uvicorn.error.browser-backtests")

TICKET_SECONDS = 15 * 60       # how long handed-out inputs wait for their result
TICKETS_KEPT = 64              # all users together; the oldest goes first
RETRY_SECONDS = 120            # how long an answered ticket keeps its outcome for a retried hand-in
CHECKS_KEPT = 64               # browser summaries kept for the server's cross-check

# Why a backtest the browser could have computed ran on the server instead (the client reports
# one of these with the server request; the stored result and the UI snapshot carry it).
FALLBACK_REASONS = frozenset({
    "unsupported",      # no WebAssembly / module worker in this browser
    "device",           # a phone, a tablet or fewer than 4 cores
    "warming",          # the engine was still loading; this run went to the server, the next is local
    "init_failed",      # loading Pyodide or the engine sources failed
    "init_timeout",     # loading took longer than the limit
    "memory",           # the worker ran out of memory
    "crashed",          # the worker died
    "engine_error",     # the engine raised (the server's run gives the user its own answer, a data refusal included)
    "hidden",           # the page was hidden (a hidden tab computes 17–100× slower)
    "slow",             # a run took longer than the limit
    "inputs_failed",    # the inputs could not be fetched
    "inputs_expired",   # the server no longer held the inputs when the result arrived
    "rerun",            # the server's authoritative rerun (after saving a strategy)
})


# The result fields the server sets around a report: a browser's report never supplies them.
SERVER_FIELDS = frozenset({
    "backtestId", "kind", "createdAt", "verdict", "params", "strategyId", "strategyName", "version", "templateId",
    "definition", "planSummary", "symbol", "timeframeSeconds", "planName", "feeSource", "timings", "source",
    "fallbackReason", "inputDigest", "engineDigest", "browserCheck",
})


def browser_backtests_enabled() -> bool:
    """回滚开关: ``AI_TRADER_BROWSER_BACKTESTS=0`` sends every interface backtest back to the server queue
    (the inputs route answers 503, a browser comparison is computed by the server) without a client release."""
    return os.environ.get("AI_TRADER_BROWSER_BACKTESTS", "1").strip() != "0"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True,
                      default=str).encode()


def inputs_digest(inputs: dict[str, Any]) -> str:
    """First 16 hex of sha256 over the inputs' canonical JSON: the same inputs, the same digest."""
    return hashlib.sha256(canonical_json(inputs)).hexdigest()[:16]


@dataclass
class Ticket:
    user: str
    digest: str
    plan: dict[str, Any]
    options: dict[str, Any]
    symbol: str
    identity: dict[str, Any]
    persist: bool
    preview: bool
    fee_source: str
    load_ms: int
    id: str = field(default_factory=lambda: uuid4().hex)
    issued: float = field(default_factory=time.monotonic)
    # The first hand-in's outcome — the reply text (event loop only): a repeat of the same ticket
    # awaits and returns it. ``answered`` is when it succeeded.
    outcome: Future[str] | None = field(default=None, repr=False)
    answered: float | None = None


class BrowserTickets:
    """The inputs handed out, per (user, ticket id); thread-safe, bounded. A ticket stays after its
    result came back (until it expires) so that a retried hand-in finds the first outcome."""

    def __init__(self, *, seconds: float = TICKET_SECONDS, kept: int = TICKETS_KEPT) -> None:
        self.seconds, self.kept = seconds, kept
        self._items: OrderedDict[tuple[str, str], Ticket] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self) -> None:
        now = time.monotonic()
        for key in [key for key, ticket in self._items.items()
                    if now - ticket.issued > self.seconds
                    or (ticket.answered is not None and now - ticket.answered > RETRY_SECONDS)]:
            del self._items[key]
        while len(self._items) > self.kept:
            self._items.popitem(last=False)

    def issue(self, ticket: Ticket) -> None:
        with self._lock:
            self._items[(ticket.user, ticket.id)] = ticket
            self._expire()

    @staticmethod
    def settled(ticket: Ticket):
        """Done-callback of a ticket's outcome: a success keeps it for retries (briefly), a failure
        clears it so the hand-in may be tried again."""
        def callback(done: Future[str]) -> None:
            if done.cancelled() or done.exception() is not None:
                ticket.outcome = None
            else:
                ticket.answered = time.monotonic()
        return callback

    def get(self, user: str, ticket_id: str) -> Ticket | None:
        with self._lock:
            self._expire()
            return self._items.get((user, ticket_id))


def summary_key(summary: dict[str, Any] | None) -> str:
    return canonical_json(summary or {}).decode()


class BrowserChecks:
    """The summaries of the latest browser results by (user, inputs digest), for the server's rerun
    over the same inputs to be compared with."""

    def __init__(self, *, kept: int = CHECKS_KEPT) -> None:
        self.kept = kept
        self._items: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def remember(self, user: str, digest: str, summary: dict[str, Any]) -> None:
        with self._lock:
            self._items.pop((user, digest), None)
            self._items[(user, digest)] = summary
            while len(self._items) > self.kept:
                self._items.popitem(last=False)

    def check(self, user: str, result: dict[str, Any]) -> dict[str, Any] | None:
        """``{match, browserSummary?}`` when the browser computed these inputs before, else None. A
        mismatch is an engine divergence (the same inputs gave different numbers): logged."""
        digest = result.get("inputDigest")
        if not digest:
            return None
        with self._lock:
            browser = self._items.get((user, str(digest)))
        if browser is None:
            return None
        if summary_key(browser) == summary_key(result.get("summary")):
            return {"match": True}
        log.warning("browser backtest differs from the server's over the same inputs %s: browser %s, server %s",
                    digest, browser, result.get("summary"))
        return {"match": False, "browserSummary": browser}
