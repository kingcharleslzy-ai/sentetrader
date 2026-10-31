"""真实用户监测（RUM，所有者 2026-10-13）：浏览器/桌面/手机端按分钟聚合的匿名技术指标。

What is collected is technical only — page vitals (LCP/INP/CLS/FCP/TTFB), the price's end-to-end
latency (the sidecar's capture time → the browser's paint, corrected by the server clock this
module hands out), the K-line chart's first paint, the session stream's drops and reconnects, how
many page errors of which kind (never their text), visible time and JS heap. No message, symbol,
order, account or URL is accepted: the payload schema below has no field that could carry one.

The client sends one small batch a minute (`POST /api/telemetry/rum`, each metric a count, a sum and
a histogram over the shared bucket bounds — no raw points). Rows are kept per minute × anonymous
user hash × page session × client kind × network type, in their own SQLite file next to the Runtime
database (so the telemetry never contends with the business tables' writer lock), for
RETENTION_DAYS: the receiver itself sweeps older rows at most once an hour. The owner reads the
summary (`GET /api/admin/rum`, 后台「用户体验」 and the capability catalog).

Switched off for the whole deployment with AI_TRADER_RUM_ENABLED=0; AI_TRADER_RUM_SAMPLE_RATE
(0–1, default 1) keeps that share of page sessions. A switched-off or unsampled session is told so
in a 200 answer (`enabled: false`) and the client stops — never a 404, which a browser logs as a
console error.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RETENTION_DAYS = 14
PRUNE_INTERVAL_SECONDS = 3600
MAX_BODY_BYTES = 16 * 1024
MAX_MINUTES_PER_BATCH = 10
MAX_METRICS_PER_MINUTE = 32
# A batch may carry minutes queued while the tab was offline, never older than this.
MAX_MINUTE_AGE_SECONDS = 2 * 3600
# Per anonymous user: one batch a minute per open page, a few pages, a hide/close flush each.
RATE_LIMIT_PER_MINUTE = 20

# Upper bounds of the histogram buckets (v1); bucket i holds (bound[i-1], bound[i]], the last one
# everything above the final bound. The client (apps/client/src/rum.ts) carries the same list.
BUCKET_BOUNDS: tuple[float, ...] = (
    10, 25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 13000, 20000,
    30000, 60000,
)
BUCKETS = len(BUCKET_BOUNDS) + 1

# Distribution metrics and their unit (CLS arrives ×1000, the heap in MB).
HISTOGRAM_METRICS: dict[str, str] = {
    "lcp": "ms", "inp": "ms", "fcp": "ms", "ttfb": "ms", "cls": "‰",
    "price_latency": "ms", "kline_first_paint": "ms", "sse_down": "ms", "memory": "MB",
}
ERROR_CATEGORIES = ("chunk", "network", "type", "reference", "range", "syntax", "security", "rejection", "other")
# Counters: n is the count; `visible` also sums the visible milliseconds of the minute.
COUNTER_METRICS = frozenset({"sse_reconnect", "visible", *(f"error.{name}" for name in ERROR_CATEGORIES)})
CLIENTS = ("web", "desktop", "mobile")
NETWORKS = ("wifi", "ethernet", "cellular", "4g", "3g", "2g", "slow-2g", "unknown")
RANGES = {"1h": (3600, 60), "24h": (86400, 600), "7d": (7 * 86400, 3600)}
SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")
MAX_COUNT = 100_000
# One sample can be at most a day (ms) and one minute holds at most a minute of visible time, so a
# forged sum can neither push a stored total toward infinity nor skew the dashboard by orders of magnitude.
MAX_SAMPLE_VALUE = 86_400_000
MAX_VISIBLE_MS_PER_MINUTE = 60_000


class RumRejected(ValueError):
    """The batch does not match the schema; nothing of it is stored."""


def enabled_from_env() -> bool:
    return os.getenv("AI_TRADER_RUM_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}


def sample_rate_from_env() -> float:
    try:
        rate = float(os.getenv("AI_TRADER_RUM_SAMPLE_RATE", "1"))
    except ValueError:
        return 1.0
    return min(1.0, max(0.0, rate)) if math.isfinite(rate) else 1.0


def bucket_index(value: float) -> int:
    for index, bound in enumerate(BUCKET_BOUNDS):
        if value <= bound:
            return index
    return len(BUCKET_BOUNDS)


def quantile(buckets: list[int], q: float) -> float | None:
    """The q-quantile of a bucket histogram, interpolated linearly inside its bucket; the open last
    bucket answers its lower bound (the figure reads "at least")."""
    total = sum(buckets)
    if total <= 0:
        return None
    rank = q * total
    seen = 0
    for index, count in enumerate(buckets):
        if count <= 0:
            continue
        if seen + count >= rank:
            if index >= len(BUCKET_BOUNDS):
                return float(BUCKET_BOUNDS[-1])
            low = 0.0 if index == 0 else float(BUCKET_BOUNDS[index - 1])
            high = float(BUCKET_BOUNDS[index])
            return round(low + (high - low) * max(0.0, rank - seen) / count, 1)
        seen += count
    return float(BUCKET_BOUNDS[-1])


def quantile_is_open(buckets: list[int], q: float) -> bool:
    """True when the q-quantile falls in the open last bucket: `quantile` then only knows "at least
    the last bound", and the figure must be shown as such (≥ 60 s), not as a measured 60 s."""
    total = sum(buckets)
    if total <= 0:
        return False
    rank = q * total
    seen = 0
    for index, count in enumerate(buckets):
        if count <= 0:
            continue
        if seen + count >= rank:
            return index >= len(BUCKET_BOUNDS)
        seen += count
    return True


def _merge(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    a, b = json.loads(left), json.loads(right)
    return json.dumps([int(x) + int(y) for x, y in zip(a, b, strict=True)], separators=(",", ":"))


class _HistogramSum:
    """SQLite aggregate: the element-wise sum of the rows' bucket lists."""

    def __init__(self) -> None:
        self.total: list[int] | None = None

    def step(self, value: str | None) -> None:
        if value is None:
            return
        counts = json.loads(value)
        self.total = counts if self.total is None else [x + y for x, y in zip(self.total, counts, strict=True)]

    def finalize(self) -> str | None:
        return None if self.total is None else json.dumps(self.total, separators=(",", ":"))


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RumRejected(f"rum_{field}_invalid")
    return float(value)


def _bounded_sum(name: str, total: float, n: int) -> float:
    """The row's sum clamped into what the metric can honestly be (a client with a stepped clock may
    report a small negative or an overlong visible interval; the batch is kept, the figure bounded)."""
    if name in HISTOGRAM_METRICS:
        return min(max(total, 0.0), float(MAX_SAMPLE_VALUE) * n)
    if name == "visible":
        return min(max(total, 0.0), float(MAX_VISIBLE_MS_PER_MINUTE))
    return 0.0  # the other counters carry a count only


def _count(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        raise RumRejected(f"rum_{field}_invalid")
    return value


def parse_batch(body: bytes, *, now: float) -> dict[str, Any]:
    """Validate one client batch into {session, client, network, rows: [(minute, metric, n, sum, buckets)]}."""
    if len(body) > MAX_BODY_BYTES:
        raise RumRejected("rum_too_large")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise RumRejected("rum_not_json") from None
    if not isinstance(data, dict) or data.get("v") != SCHEMA_VERSION:
        raise RumRejected("rum_version_unsupported")
    session = data.get("session")
    if not isinstance(session, str) or not SESSION_PATTERN.fullmatch(session):
        raise RumRejected("rum_session_invalid")
    client = data.get("client")
    if client not in CLIENTS:
        raise RumRejected("rum_client_invalid")
    network = data.get("network", "unknown")
    if network not in NETWORKS:
        network = "unknown"
    minutes = data.get("minutes")
    if not isinstance(minutes, list) or not 1 <= len(minutes) <= MAX_MINUTES_PER_BATCH:
        raise RumRejected("rum_minutes_invalid")
    rows: list[tuple[int, str, int, float, str | None]] = []
    for entry in minutes:
        if not isinstance(entry, dict):
            raise RumRejected("rum_minutes_invalid")
        minute = entry.get("t")
        if isinstance(minute, bool) or not isinstance(minute, int) or minute % 60:
            raise RumRejected("rum_minute_invalid")
        if not now - MAX_MINUTE_AGE_SECONDS <= minute <= now + 300:
            raise RumRejected("rum_minute_out_of_range")
        metrics = entry.get("metrics")
        if not isinstance(metrics, dict) or len(metrics) > MAX_METRICS_PER_MINUTE:
            raise RumRejected("rum_metrics_invalid")
        for name, value in metrics.items():
            if name not in HISTOGRAM_METRICS and name not in COUNTER_METRICS:
                raise RumRejected("rum_metric_unknown")
            if not isinstance(value, dict):
                raise RumRejected("rum_metrics_invalid")
            n = _count(value.get("n"), field="n")
            total = _number(value.get("sum", 0), field="sum")
            buckets: str | None = None
            if name in HISTOGRAM_METRICS:
                raw = value.get("buckets")
                if not isinstance(raw, dict):
                    raise RumRejected("rum_buckets_invalid")
                counts = [0] * BUCKETS
                for key, count in raw.items():
                    if not isinstance(key, str) or not key.isdigit() or int(key) >= BUCKETS:
                        raise RumRejected("rum_buckets_invalid")
                    counts[int(key)] += _count(count, field="buckets")
                if sum(counts) != n:
                    raise RumRejected("rum_buckets_invalid")
                buckets = json.dumps(counts, separators=(",", ":"))
            total = _bounded_sum(name, total, n)
            if n == 0 and total == 0:
                continue
            rows.append((minute, name, n, total, buckets))
    return {"session": session, "client": client, "network": network, "rows": rows}


class RumStore:
    """The minute rows, in their own small SQLite file; every call is short and serialized. The file
    is opened on first use, so an app that never receives a beacon (tests, a replay) touches no disk."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.lock = threading.Lock()
        self._open_lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._salt = b""
        self._pruned_at = 0.0

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            with self._open_lock:
                if self._connection is None:
                    self._connection = self._open()
        return self._connection

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
        connection.create_function("rum_merge", 2, _merge, deterministic=True)
        connection.create_aggregate("rum_hist_sum", 1, _HistogramSum)  # type: ignore[arg-type]
        with connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS rum_minutes ("
                " minute INTEGER NOT NULL, session TEXT NOT NULL, metric TEXT NOT NULL,"
                " user_hash TEXT NOT NULL, client TEXT NOT NULL, network TEXT NOT NULL,"
                " n INTEGER NOT NULL, total REAL NOT NULL, buckets TEXT,"
                " PRIMARY KEY (minute, session, metric, user_hash, client, network)) WITHOUT ROWID")
            connection.execute("CREATE INDEX IF NOT EXISTS rum_minutes_metric ON rum_minutes (metric, minute)")
            connection.execute("CREATE TABLE IF NOT EXISTS rum_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = connection.execute("SELECT value FROM rum_meta WHERE key = 'salt'").fetchone()
            if row is None:
                salt = secrets.token_hex(32)
                connection.execute("INSERT INTO rum_meta (key, value) VALUES ('salt', ?)", (salt,))
            else:
                salt = str(row[0])
        self._salt = bytes.fromhex(salt)
        return connection

    def close(self) -> None:
        with self.lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def user_hash(self, user_id: str) -> str:
        self.connection  # noqa: B018 — the salt is read when the file opens
        return hmac.new(self._salt, user_id.encode(), hashlib.sha256).hexdigest()[:16]

    @staticmethod
    def session_key(session: str) -> str:
        return hashlib.sha256(session.encode()).hexdigest()[:16]

    def record(self, user_id: str, batch: dict[str, Any]) -> int:
        user = self.user_hash(user_id)
        session = self.session_key(batch["session"])
        with self.lock, self.connection:
            self.connection.executemany(
                "INSERT INTO rum_minutes (minute, session, metric, user_hash, client, network, n, total, buckets)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (minute, session, metric, user_hash, client, network) DO UPDATE SET"
                " n = n + excluded.n,"
                # The merged row is bounded again as a whole (`_bounded_sum` only clamps one batch):
                # repeated posts for one minute cannot add up past what that minute can honestly hold.
                " total = CASE WHEN metric = 'visible' THEN MIN(total + excluded.total, ?)"
                " WHEN buckets IS NOT NULL THEN MIN(total + excluded.total, ? * (n + excluded.n))"
                " ELSE total + excluded.total END,"
                " buckets = rum_merge(buckets, excluded.buckets)",
                [(minute, session, metric, user, batch["client"], batch["network"], n, total, buckets,
                  float(MAX_VISIBLE_MS_PER_MINUTE), float(MAX_SAMPLE_VALUE))
                 for minute, metric, n, total, buckets in batch["rows"]])
        self.prune_if_due()
        return len(batch["rows"])

    def prune_if_due(self, *, force: bool = False) -> int:
        now = self.clock()
        if not force and now - self._pruned_at < PRUNE_INTERVAL_SECONDS:
            return 0
        self._pruned_at = now
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "DELETE FROM rum_minutes WHERE minute < ?", (int(now) - RETENTION_DAYS * 86400,))
        return cursor.rowcount

    def sweep_existing(self) -> int:
        """Retention does not wait for traffic: a store whose file already exists is swept at boot and
        every hour (also while collection is off), a store that never saw a beacon opens no file."""
        if self._connection is None and not self.path.exists():
            return 0
        return self.prune_if_due(force=True)

    async def sweep_forever(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.sweep_existing)
            except Exception:  # noqa: BLE001 — retention is best effort, the next hour retries
                log.warning("rum_sweep_failed", exc_info=True)
            await asyncio.sleep(PRUNE_INTERVAL_SECONDS)

    def summary(self, range_key: str) -> dict[str, Any]:
        span, slot = RANGES[range_key]
        now = int(self.clock())
        start = (now - span) // slot * slot
        self.prune_if_due()
        with self.lock:
            grouped = self.connection.execute(
                "SELECT minute / ? * ? AS slot, metric, client, SUM(n), SUM(total), rum_hist_sum(buckets)"
                " FROM rum_minutes WHERE minute >= ? GROUP BY slot, metric, client ORDER BY slot",
                (slot, slot, start)).fetchall()
            people = self.connection.execute(
                "SELECT client, network, COUNT(DISTINCT session), COUNT(DISTINCT user_hash)"
                " FROM rum_minutes WHERE minute >= ? GROUP BY client, network", (start,)).fetchall()
            overall = self.connection.execute(
                "SELECT COUNT(DISTINCT session), COUNT(DISTINCT user_hash) FROM rum_minutes WHERE minute >= ?",
                (start,)).fetchone()
            by_client_people = self.connection.execute(
                "SELECT client, COUNT(DISTINCT session), COUNT(DISTINCT user_hash) FROM rum_minutes"
                " WHERE minute >= ? GROUP BY client", (start,)).fetchall()
        return _shape_summary(range_key, slot, start, now, grouped, people, overall, by_client_people)


QUANTILES = (("p50", 0.5), ("p75", 0.75), ("p95", 0.95))


def _at_least(buckets: list[int], names: tuple[str, ...]) -> list[str]:
    """The listed quantiles that landed in the open last bucket (their value is a lower bound)."""
    return [name for name, q in QUANTILES if name in names and quantile_is_open(buckets, q)]


def _figures(buckets: list[int] | None, n: int) -> dict[str, Any]:
    if not buckets:
        return {"n": n, "p50": None, "p75": None, "p95": None, "atLeast": []}
    return {"n": n, **{name: quantile(buckets, q) for name, q in QUANTILES},
            "atLeast": _at_least(buckets, ("p50", "p75", "p95"))}


def _add(target: list[int] | None, counts: list[int]) -> list[int]:
    return list(counts) if target is None else [x + y for x, y in zip(target, counts, strict=True)]


def _shape_summary(range_key: str, slot: int, start: int, now: int, grouped: list[tuple],
                   people: list[tuple], overall: tuple, by_client_people: list[tuple]) -> dict[str, Any]:
    # metric → {"all": hist, "n", "byClient": {client: [hist, n]}, "series": {slot: [hist, n]}}
    hist: dict[str, dict[str, Any]] = {}
    counters: dict[str, dict[str, Any]] = {}
    for slot_start, metric, client, n, total, buckets in grouped:
        n, total = int(n or 0), float(total or 0)
        if metric in HISTOGRAM_METRICS:
            counts = json.loads(buckets) if buckets else [0] * BUCKETS
            entry = hist.setdefault(metric, {"all": None, "n": 0, "byClient": {}, "series": {}})
            entry["all"] = _add(entry["all"], counts)
            entry["n"] += n
            by_client = entry["byClient"].setdefault(client, [None, 0])
            by_client[0] = _add(by_client[0], counts)
            by_client[1] += n
            point = entry["series"].setdefault(slot_start, [None, 0])
            point[0] = _add(point[0], counts)
            point[1] += n
        else:
            entry = counters.setdefault(metric, {"n": 0, "sum": 0.0, "byClient": {}, "series": {}})
            entry["n"] += n
            entry["sum"] += total
            entry["byClient"][client] = entry["byClient"].get(client, 0) + n
            entry["series"][slot_start] = entry["series"].get(slot_start, 0) + n
    metrics = {
        metric: {
            "unit": HISTOGRAM_METRICS[metric], **_figures(entry["all"], entry["n"]),
            "byClient": {client: _figures(counts, n) for client, (counts, n) in sorted(entry["byClient"].items())},
            "series": [{"t": slot_start, "n": n, "p50": quantile(counts, 0.5), "p95": quantile(counts, 0.95),
                        "atLeast": _at_least(counts, ("p50", "p95"))}
                       for slot_start, (counts, n) in sorted(entry["series"].items())],
        }
        for metric, entry in sorted(hist.items())
    }
    errors = {name.removeprefix("error."): entry for name, entry in counters.items() if name.startswith("error.")}
    error_series: dict[int, int] = {}
    error_clients: dict[str, int] = {}
    for entry in errors.values():
        for slot_start, n in entry["series"].items():
            error_series[slot_start] = error_series.get(slot_start, 0) + n
        for client, n in entry["byClient"].items():
            error_clients[client] = error_clients.get(client, 0) + n
    reconnects = counters.get("sse_reconnect", {"n": 0, "byClient": {}, "series": {}})
    visible = counters.get("visible", {"sum": 0.0})
    by_network: dict[str, dict[str, int]] = {}
    for client, network, sessions, _users in people:
        bucket = by_network.setdefault(network, {"sessions": 0})
        bucket["sessions"] += int(sessions)
    return {
        "status": "ok",
        "range": range_key,
        "slotSeconds": slot,
        "from": start,
        "to": now,
        "retentionDays": RETENTION_DAYS,
        "totals": {
            "sessions": int(overall[0] or 0),
            "users": int(overall[1] or 0),
            "visibleMinutes": round(float(visible.get("sum", 0.0)) / 60000, 1),
            "byClient": {client: {"sessions": int(sessions), "users": int(users)}
                         for client, sessions, users in by_client_people},
            "byNetwork": dict(sorted(by_network.items())),
        },
        "metrics": metrics,
        "counters": {
            "sseReconnect": {"total": int(reconnects["n"]), "byClient": dict(reconnects["byClient"]),
                             "series": [{"t": t, "n": n} for t, n in sorted(reconnects["series"].items())]},
            "errors": {"total": sum(entry["n"] for entry in errors.values()),
                       "byCategory": {name: int(entry["n"]) for name, entry in sorted(errors.items())},
                       "byClient": error_clients,
                       "series": [{"t": t, "n": n} for t, n in sorted(error_series.items())]},
        },
    }


class RumLimiter:
    """A fixed one-minute window per identity, counting every attempt (the body is not read first);
    the table is swept as windows expire."""

    def __init__(self, limit: int = RATE_LIMIT_PER_MINUTE, *, clock: Callable[[], float] = time.time) -> None:
        self.limit, self.clock = limit, clock
        self.windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> bool:
        now = self.clock()
        if len(self.windows) > 5000:
            self.windows = {k: v for k, v in self.windows.items() if v[0] > now}
        until, used = self.windows.get(key, (now + 60, 0))
        if until <= now:
            until, used = now + 60, 0
        if used >= self.limit:
            return False
        self.windows[key] = (until, used + 1)
        return True


def sampled(session: str, rate: float) -> bool:
    """A page session is kept or dropped as a whole, the same way on every batch."""
    if rate >= 1:
        return True
    return int(hashlib.sha256(session.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


def register(application: Any, ctx: Any) -> None:
    """`GET/POST /api/telemetry/rum` — any signed-in user or guest. Not in the capability catalog:
    it is the page's own technical beacon, not something a user or an agent operates."""
    from .guest import guest_request

    # Next to the Runtime database (the data volume in the cloud, the app folder on the desktop).
    store = RumStore(ctx.settings.database_path.parent / "rum.sqlite3")
    application.state.rum_store = store
    limiter = RumLimiter()

    async def identity(request: Request) -> str:
        guests = getattr(application.state, "guests", None)
        if guests is not None and guest_request(request):
            return str(guests.require(request, meter=False).id)
        return str(await ctx.current_user(request))

    def config() -> dict[str, Any]:
        return {"enabled": enabled_from_env(), "sampleRate": sample_rate_from_env(),
                "serverNow": int(time.time() * 1000), "v": SCHEMA_VERSION, "bucketBounds": list(BUCKET_BOUNDS)}

    @application.get("/api/telemetry/rum", include_in_schema=False)
    async def rum_config(request: Request) -> dict[str, Any]:
        await identity(request)
        return config()

    @application.post("/api/telemetry/rum", include_in_schema=False)
    async def rum_ingest(request: Request) -> dict[str, Any]:
        # The same origin rule as the guest bootstrap: a browser's write names its page.
        origin = request.headers.get("origin")
        if origin and origin not in ctx.settings.origins:
            raise HTTPException(403, "origin_not_allowed")
        user_id = await identity(request)
        # Every authenticated attempt spends the identity's budget — before the body is read and before
        # any early answer (collection off, unsampled, malformed) — so none of those is a free path.
        # Keyed on the identity itself: no telemetry file is opened for a request that is not stored.
        if not limiter.allow(user_id):
            raise HTTPException(429, "rum_rate_limited")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise HTTPException(413, "rum_too_large")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                raise HTTPException(413, "rum_too_large")
        answer = config()
        if not answer["enabled"]:
            return {**answer, "accepted": False}
        try:
            batch = parse_batch(body, now=time.time())
        except RumRejected as error:
            raise HTTPException(422, str(error)) from None
        if not sampled(batch["session"], answer["sampleRate"]):
            return {**answer, "enabled": False, "accepted": False}
        await asyncio.to_thread(store.record, user_id, batch)
        return {**answer, "accepted": True}
