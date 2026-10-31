"""真实用户监测：`POST /api/telemetry/rum` 收匿名分钟汇总，`GET /api/admin/rum` 给主账号看分位数。

What a deployment observes: a signed-in page's batch is stored under an anonymous hash (never the
user name), merged per minute with the same session's next batch, summarized as p50/p95 per client
and over time; a member cannot read the summary; anything outside the schema (an unknown metric —
the only place a URL or a message could hide — a wrong bucket total, an oversized body) is refused
whole; the deployment switch and sampling answer 200 `enabled: false` so the page stops quietly;
rows older than 14 days are swept.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime import rum
from trader_runtime.auth import hash_owner_password
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app

ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"
FRIEND_PASSWORD = "a-solid-password-123"
HEADERS = {"origin": ORIGIN, "content-type": "text/plain"}


def minute_now(offset: int = 0) -> int:
    return int(time.time()) // 60 * 60 - offset


def batch(session: str = "page-session-0000000001", *, client: str = "web", minutes=None) -> dict:
    return {"v": 1, "session": session, "client": client, "network": "wifi", "minutes": minutes or [{
        "t": minute_now(),
        "metrics": {
            "lcp": {"n": 1, "sum": 1200, "buckets": {"11": 1}},
            "price_latency": {"n": 4, "sum": 400, "buckets": {"3": 2, "4": 2}},
            "sse_reconnect": {"n": 1},
            "error.type": {"n": 2},
            "visible": {"n": 1, "sum": 60000},
        },
    }]}


@pytest.fixture
def rum_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-zhipu-fixture")
    monkeypatch.delenv("AI_TRADER_RUM_ENABLED", raising=False)
    monkeypatch.delenv("AI_TRADER_RUM_SAMPLE_RATE", raising=False)
    application = create_app(
        tmp_path / "rum.sqlite3",
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
        registration_enabled=True,
        data_mode="tqkq-sidecar",
        data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek",
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    yield application
    application.state.rum_store.close()
    application.state.store.close()


@pytest.fixture
async def friend_and_owner(rum_app):
    transport = ASGITransport(app=rum_app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as friend,
        AsyncClient(transport=transport, base_url="http://test") as owner,
    ):
        assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD},
                                 headers={"origin": ORIGIN})).status_code == 200
        code = (await owner.post("/api/auth/owner/invite-codes", json={"daysValid": 14},
                                 headers={"origin": ORIGIN})).json()["inviteCode"]
        assert (await friend.post("/api/auth/owner/register", json={
            "username": "friend", "password": FRIEND_PASSWORD, "inviteCode": code},
            headers={"origin": ORIGIN})).status_code == 200
        yield friend, owner


async def post(client: AsyncClient, payload: dict | bytes):
    content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return await client.post("/api/telemetry/rum", content=content, headers=HEADERS)


async def test_a_batch_is_stored_anonymously_and_summarized_for_the_owner(rum_app, friend_and_owner):
    friend, owner = friend_and_owner
    config = (await friend.get("/api/telemetry/rum", headers={"origin": ORIGIN})).json()
    assert config["enabled"] is True and config["bucketBounds"] == list(rum.BUCKET_BOUNDS)
    answer = await post(friend, batch())
    assert answer.status_code == 200
    assert answer.json()["accepted"] is True and abs(answer.json()["serverNow"] - time.time() * 1000) < 5000
    # The same page's next batch for the same minute merges into the same rows.
    assert (await post(friend, batch())).status_code == 200
    assert (await post(owner, batch("owner-desktop-session-01", client="desktop"))).status_code == 200

    rows = rum_app.state.rum_store.connection.execute(
        "SELECT DISTINCT user_hash, session FROM rum_minutes").fetchall()
    stored = json.dumps(rows)
    assert len(rows) == 2 and "friend" not in stored and "charles" not in stored
    assert "page-session-0000000001" not in stored

    summary = (await owner.get("/api/admin/rum", params={"range": "1h"}, headers={"origin": ORIGIN})).json()
    assert summary["status"] == "ok" and summary["enabled"] is True
    assert summary["totals"]["sessions"] == 2 and summary["totals"]["users"] == 2
    assert summary["totals"]["byClient"]["web"] == {"sessions": 1, "users": 1}
    lcp = summary["metrics"]["lcp"]
    assert lcp["n"] == 3 and 1000 < lcp["p50"] <= 1500 and lcp["unit"] == "ms"
    assert set(lcp["byClient"]) == {"web", "desktop"} and lcp["byClient"]["web"]["n"] == 2
    assert summary["metrics"]["price_latency"]["p95"] <= 100
    assert len(lcp["series"]) == 1 and lcp["series"][0]["n"] == 3
    assert summary["counters"]["sseReconnect"]["total"] == 3
    assert summary["counters"]["errors"] == {
        "total": 6, "byCategory": {"type": 6}, "byClient": {"web": 4, "desktop": 2},
        "series": [{"t": summary["counters"]["errors"]["series"][0]["t"], "n": 6}]}
    # The friend's two same-minute posts merge into one row, bounded again as a whole (one minute of
    # visible time at most); the owner's desktop page adds its own.
    assert summary["totals"]["visibleMinutes"] == 2.0


async def test_the_summary_is_owner_only_and_the_beacon_needs_a_login(rum_app, friend_and_owner):
    friend, _owner = friend_and_owner
    assert (await friend.get("/api/admin/rum", headers={"origin": ORIGIN})).status_code == 404
    async with AsyncClient(transport=ASGITransport(app=rum_app), base_url="http://test") as anonymous:
        assert (await post(anonymous, batch())).status_code == 401
        assert (await anonymous.get("/api/admin/rum")).status_code == 401


@pytest.mark.parametrize("change, code", [
    (lambda b: b["minutes"][0]["metrics"].update({"url": {"n": 1}}), "rum_metric_unknown"),
    (lambda b: b["minutes"][0]["metrics"]["lcp"].update({"n": 2}), "rum_buckets_invalid"),
    (lambda b: b["minutes"][0].update({"t": minute_now() + 1}), "rum_minute_invalid"),
    (lambda b: b["minutes"][0].update({"t": minute_now(3 * 3600)}), "rum_minute_out_of_range"),
    (lambda b: b.update({"session": "short"}), "rum_session_invalid"),
    (lambda b: b.update({"client": "bot"}), "rum_client_invalid"),
    (lambda b: b.update({"v": 2}), "rum_version_unsupported"),
])
async def test_anything_outside_the_schema_is_refused_whole(rum_app, friend_and_owner, change, code):
    friend, _owner = friend_and_owner
    payload = batch()
    change(payload)
    answer = await post(friend, payload)
    assert answer.status_code == 422 and answer.json()["detail"] == code
    assert rum_app.state.rum_store.connection.execute("SELECT COUNT(*) FROM rum_minutes").fetchone()[0] == 0


async def test_an_oversized_body_or_foreign_origin_is_refused(rum_app, friend_and_owner):
    friend, _owner = friend_and_owner
    assert (await post(friend, b"x" * (rum.MAX_BODY_BYTES + 1))).status_code == 413
    foreign = await friend.post("/api/telemetry/rum", content=json.dumps(batch()).encode(),
                                headers={"origin": "https://evil.example", "content-type": "text/plain"})
    assert foreign.status_code == 403


async def test_switched_off_or_unsampled_pages_are_told_to_stop_with_a_200(rum_app, friend_and_owner, monkeypatch):
    friend, owner = friend_and_owner
    monkeypatch.setenv("AI_TRADER_RUM_ENABLED", "0")
    answer = await post(friend, batch())
    assert answer.status_code == 200 and answer.json()["enabled"] is False and answer.json()["accepted"] is False
    assert (await owner.get("/api/admin/rum", headers={"origin": ORIGIN})).json()["enabled"] is False
    monkeypatch.setenv("AI_TRADER_RUM_ENABLED", "1")
    monkeypatch.setenv("AI_TRADER_RUM_SAMPLE_RATE", "0")
    answer = await post(friend, batch())
    assert answer.status_code == 200 and answer.json() == {**answer.json(), "enabled": False, "accepted": False}
    assert rum_app.state.rum_store.connection.execute("SELECT COUNT(*) FROM rum_minutes").fetchone()[0] == 0


async def test_one_user_is_rate_limited(rum_app, friend_and_owner):
    friend, _owner = friend_and_owner
    statuses = [(await post(friend, batch())).status_code for _ in range(rum.RATE_LIMIT_PER_MINUTE + 1)]
    assert statuses[:-1] == [200] * rum.RATE_LIMIT_PER_MINUTE and statuses[-1] == 429


async def test_every_attempt_spends_the_budget_even_when_nothing_is_stored(rum_app, friend_and_owner, monkeypatch):
    """#832: a malformed body, a switched-off collection and an unsampled page all count against the
    identity's budget, and none of them opens the telemetry file."""
    friend, owner = friend_and_owner
    limit = rum.RATE_LIMIT_PER_MINUTE
    bad = [(await post(friend, b"{not json")).status_code for _ in range(limit + 1)]
    assert bad[:-1] == [422] * limit and bad[-1] == 429
    assert (await post(friend, batch())).status_code == 429  # a valid batch has no budget left either
    monkeypatch.setenv("AI_TRADER_RUM_ENABLED", "0")
    off = [(await post(owner, batch())).status_code for _ in range(limit + 1)]
    assert off[:-1] == [200] * limit and off[-1] == 429
    assert rum_app.state.rum_store._connection is None  # nothing was stored, no file opened for them


async def test_an_unsampled_page_also_spends_the_budget(rum_app, friend_and_owner, monkeypatch):
    friend, _owner = friend_and_owner
    monkeypatch.setenv("AI_TRADER_RUM_SAMPLE_RATE", "0")
    limit = rum.RATE_LIMIT_PER_MINUTE
    unsampled = [(await post(friend, batch())).status_code for _ in range(limit + 1)]
    assert unsampled[:-1] == [200] * limit and unsampled[-1] == 429
    assert rum_app.state.rum_store._connection is None


async def test_the_store_is_closed_when_the_app_shuts_down(rum_app):
    store = rum_app.state.rum_store
    async with rum_app.router.lifespan_context(rum_app):
        store.record("u", {"session": "s" * 16, "client": "web", "network": "wifi",
                           "rows": [(minute_now(), "sse_reconnect", 1, 0.0, None)]})
        assert store._connection is not None
    assert store._connection is None


def test_rows_older_than_the_retention_are_swept(tmp_path: Path):
    now = [1_800_000_000.0]
    store = rum.RumStore(tmp_path / "rum.sqlite3", clock=lambda: now[0])
    old = int(now[0]) // 60 * 60 - (rum.RETENTION_DAYS + 1) * 86400
    fresh = int(now[0]) // 60 * 60
    for minute in (old, fresh):
        store.record("u", {"session": "s" * 16, "client": "web", "network": "wifi",
                           "rows": [(minute, "sse_reconnect", 1, 1.0, None)]})
    assert store.connection.execute("SELECT minute FROM rum_minutes").fetchall() == [(fresh,)]
    store.close()


def test_a_forged_sum_is_bounded_so_totals_stay_finite(tmp_path: Path):
    minute = minute_now()
    body = json.dumps({"v": 1, "session": "s" * 16, "client": "web", "network": "wifi", "minutes": [{
        "t": minute, "metrics": {
            "visible": {"n": 1, "sum": 1e308},
            "lcp": {"n": 1, "sum": -5, "buckets": {"3": 1}},
            "price_latency": {"n": 2, "sum": 1e300, "buckets": {"3": 2}},
            "sse_reconnect": {"n": 1, "sum": 1e308},
        }}]}).encode()
    parsed = rum.parse_batch(body, now=time.time())
    sums = {metric: total for _minute, metric, _n, total, _buckets in parsed["rows"]}
    assert sums == {"visible": rum.MAX_VISIBLE_MS_PER_MINUTE, "lcp": 0.0,
                    "price_latency": 2.0 * rum.MAX_SAMPLE_VALUE, "sse_reconnect": 0.0}
    store = rum.RumStore(tmp_path / "rum.sqlite3")
    for _ in range(3):
        store.record("u", parsed)
    summary = store.summary("1h")
    json.dumps(summary, allow_nan=False)  # a non-finite figure would be refused here
    # Three posts for the same minute and session: the merged row is clamped as a whole, not per post.
    assert summary["totals"]["visibleMinutes"] == 1.0
    stored = dict(store.connection.execute("SELECT metric, total FROM rum_minutes").fetchall())
    assert stored["price_latency"] == 6.0 * rum.MAX_SAMPLE_VALUE  # clamped by its merged count (3 x 2)
    store.close()


def test_repeated_posts_for_one_minute_stay_within_what_the_minute_can_hold(tmp_path: Path):
    """#832: a merged same-minute row is capped again - visible time at most a minute, a histogram sum
    at most one day per sample."""
    minute = minute_now()
    store = rum.RumStore(tmp_path / "rum.sqlite3")
    top = json.dumps([0] * (rum.BUCKETS - 1) + [1])
    for _ in range(5):
        store.record("u", {"session": "s" * 16, "client": "web", "network": "wifi", "rows": [
            (minute, "visible", 1, 45_000.0, None),
            (minute, "lcp", 1, float(rum.MAX_SAMPLE_VALUE), top)]})
    totals = dict(store.connection.execute("SELECT metric, total FROM rum_minutes").fetchall())
    assert totals["visible"] == rum.MAX_VISIBLE_MS_PER_MINUTE
    assert totals["lcp"] == 5.0 * rum.MAX_SAMPLE_VALUE  # five samples, each at most the sample bound
    store.close()


def test_retention_is_swept_without_new_beacons(tmp_path: Path):
    now = [1_800_000_000.0]
    old = int(now[0]) // 60 * 60 - (rum.RETENTION_DAYS + 1) * 86400
    first = rum.RumStore(tmp_path / "rum.sqlite3", clock=lambda: now[0])
    first.connection.execute(
        "INSERT INTO rum_minutes (minute, session, metric, user_hash, client, network, n, total, buckets)"
        " VALUES (?, 's', 'visible', 'u', 'web', 'wifi', 1, 1.0, NULL)", (old,))
    first.connection.commit()
    first.close()
    # A fresh process (collection may be off, nobody visits): the boot sweep alone removes the old row.
    second = rum.RumStore(tmp_path / "rum.sqlite3", clock=lambda: now[0])
    assert second.sweep_existing() == 1
    assert second.connection.execute("SELECT COUNT(*) FROM rum_minutes").fetchone()[0] == 0
    second.close()
    # A store that never saw a beacon opens no file.
    untouched = rum.RumStore(tmp_path / "never.sqlite3")
    assert untouched.sweep_existing() == 0 and not (tmp_path / "never.sqlite3").exists()


def test_quantiles_interpolate_inside_the_bucket():
    counts = [0] * rum.BUCKETS
    counts[rum.bucket_index(120)] = 10      # (100, 150]
    assert rum.quantile(counts, 0.5) == 125.0
    counts = [0] * rum.BUCKETS
    counts[-1] = 1                          # above the last bound: "at least"
    assert rum.quantile(counts, 0.95) == rum.BUCKET_BOUNDS[-1]
    assert rum.quantile([0] * rum.BUCKETS, 0.5) is None


def test_a_quantile_in_the_open_last_bucket_is_reported_as_a_lower_bound(tmp_path: Path):
    """#832: 60 000 ms from the open bucket reads "at least 60 s", not a measured 60 s."""
    minute = minute_now()
    store = rum.RumStore(tmp_path / "rum.sqlite3")
    open_bucket = json.dumps([0] * (rum.BUCKETS - 1) + [4])
    at = rum.bucket_index(120)
    closed = json.dumps([0] * at + [4] + [0] * (rum.BUCKETS - at - 1))
    store.record("u", {"session": "s" * 16, "client": "web", "network": "wifi", "rows": [
        (minute, "sse_down", 4, 4 * 90_000.0, open_bucket), (minute, "price_latency", 4, 480.0, closed)]})
    metrics = store.summary("1h")["metrics"]
    assert metrics["sse_down"]["p95"] == rum.BUCKET_BOUNDS[-1]
    assert metrics["sse_down"]["atLeast"] == ["p50", "p75", "p95"]
    assert metrics["sse_down"]["series"][0]["atLeast"] == ["p50", "p95"]
    assert metrics["price_latency"]["atLeast"] == [] and metrics["price_latency"]["series"][0]["atLeast"] == []
    store.close()
