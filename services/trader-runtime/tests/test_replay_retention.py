from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from test_replay import IDENTIFIER, SYMBOL, HistoricalSource, build_slot
from trader_runtime import replay_retention
from trader_runtime.replay_retention import CLOSED_NAME, SAVED_TABLES, closed_record

HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"}
BODY = {"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000}


def contents(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
async def closed(tmp_path):
    source = HistoricalSource()
    app = build_slot(tmp_path / "live.sqlite3", source)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            assert (await client.post("/api/replays", json=BODY)).status_code == 201
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})).status_code == 200
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})).status_code == 200
        root = tmp_path / "replays" / IDENTIFIER
        proof = json.loads((root / CLOSED_NAME).read_text())
        yield app, root, datetime.fromisoformat(proof["closedAt"])


@pytest.mark.parametrize("age,removed", [(timedelta(days=30) - timedelta(microseconds=1), False),
                                         (timedelta(days=30), True), (timedelta(days=31), True)])
async def test_http_close_retains_thirty_days_then_deletes_idempotently(closed, age, removed):
    app, root, ended = closed
    router = app.state.replay_router
    before = contents(root)
    report = await router.cleanup_expired(now=ended + age, dry_run=True)
    assert report[IDENTIFIER] == ("expired" if removed else "retained_30_days")
    assert before == contents(root)
    assert (await router.cleanup_expired(now=ended + age))[IDENTIFIER] == ("removed" if removed else "retained_30_days")
    assert root.exists() is not removed
    assert await router.cleanup_expired(now=ended + age) == ({} if removed else {IDENTIFIER: "retained_30_days"})


async def test_existing_lifecycle_invokes_retention_at_startup(closed, monkeypatch):
    app, root, ended = closed

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return ended + timedelta(days=30)

    monkeypatch.setattr(replay_retention, "datetime", Clock)
    restarted = build_slot(root.parent.parent / "next-slot.sqlite3", HistoricalSource())
    async with restarted.router.lifespan_context(restarted):
        assert not root.exists()
    assert not root.exists()


@pytest.mark.parametrize("saved", ["review.json", "unknown-file", "retained_chat", "user_turn", *SAVED_TABLES])
async def test_saved_content_and_unknown_files_are_preserved(closed, saved):
    app, root, ended = closed
    if saved in {"review.json", "unknown-file"}:
        (root / saved).write_text("keep this")
    elif saved in {"retained_chat", "user_turn"}:
        with closing(sqlite3.connect(root / "runtime.sqlite3")) as connection:
            connection.execute("INSERT INTO sessions (id,user_id,mode,agent_id,status,created_at,updated_at,expires_at) "
                               "VALUES ('saved','local-user','local','native','active',?,?,?)",
                               (ended.isoformat(), ended.isoformat(),
                                "9999-12-31T00:00:00+00:00" if saved == "retained_chat" else ended.isoformat()))
            if saved == "user_turn":
                connection.execute("INSERT INTO events (id,session_id,schema_version,kind,name,timestamp,payload_json) "
                                   "VALUES ('message','saved',1,'message','message.completed',?,?)",
                                   (ended.isoformat(), '{"role":"user","text":"keep this"}'))
            connection.commit()
    else:
        # Add a representative saved record using a minimal replacement table;
        # the retention owner reads only, and never imports/migrates this child DB.
        with closing(sqlite3.connect(root / "runtime.sqlite3")) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(f'DROP TABLE "{saved}"')
            connection.execute(f'CREATE TABLE "{saved}" (saved TEXT)')
            connection.execute(f'INSERT INTO "{saved}" VALUES (?)', ("keep this",))
            connection.commit()
    before = contents(root)
    report = await app.state.replay_router.cleanup_expired(now=ended + timedelta(days=90))
    assert report[IDENTIFIER] in {"review", "unknown_files", "saved_content"}
    assert before == contents(root)


async def test_uncheckpointed_wal_is_preserved_without_touching_it(closed):
    app, root, ended = closed
    with closing(sqlite3.connect(root / "runtime.sqlite3")) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE pending_saved_data (value TEXT)")
        connection.commit()
        before = contents(root)
        report = await app.state.replay_router.cleanup_expired(now=ended + timedelta(days=90))
        assert report[IDENTIFIER] == "uncheckpointed_database"
        assert contents(root) == before


async def test_replay_directory_symlink_cannot_escape_the_state_root(closed):
    app, root, ended = closed
    outside = root.parent.parent / "outside"
    root.rename(outside)
    root.symlink_to(outside, target_is_directory=True)
    before = contents(outside)
    report = await app.state.replay_router.cleanup_expired(now=ended + timedelta(days=90))
    assert report[IDENTIFIER] == "unknown_path" and contents(outside) == before


@pytest.mark.parametrize("damage", ["missing_proof", "bad_json", "wrong_id", "naive_date", "future_date",
                                    "bad_db", "unknown_table", "symlink", "hardlink", "recoverable"])
async def test_uncertain_identity_format_or_path_never_deletes(closed, damage, tmp_path):
    app, root, ended = closed
    now = ended + timedelta(days=60)
    if damage == "missing_proof":
        (root / CLOSED_NAME).unlink()
    elif damage == "bad_json":
        (root / CLOSED_NAME).write_text("{")
    elif damage in {"wrong_id", "naive_date", "future_date"}:
        proof = json.loads((root / CLOSED_NAME).read_text())
        proof.update({"wrong_id": {"replayId": "b" * 32},
                      "naive_date": {"closedAt": ended.replace(tzinfo=None).isoformat()},
                      "future_date": {"closedAt": (now + timedelta(days=1)).isoformat()}}[damage])
        (root / CLOSED_NAME).write_text(json.dumps(proof))
    elif damage == "bad_db":
        (root / "runtime.sqlite3").write_bytes(b"broken")
    elif damage == "unknown_table":
        with closing(sqlite3.connect(root / "runtime.sqlite3")) as connection:
            connection.execute("CREATE TABLE unknown_saved_data (value TEXT)")
    elif damage in {"symlink", "hardlink"}:
        outside = tmp_path / "outside"
        outside.write_text("preserve")
        if damage == "symlink":
            (root / "exchange-fees.json").unlink(missing_ok=True)
            (root / "exchange-fees.json").symlink_to(outside)
        else:
            os.link(outside, root / "exchange-fees.json")
    elif damage == "recoverable":
        (root / "replay.json").write_text("{}")
    report = await app.state.replay_router.cleanup_expired(now=now)
    assert report[IDENTIFIER] != "removed" and root.is_dir()


async def test_other_process_using_the_directory_blocks_deletion(closed):
    app, root, ended = closed
    process = subprocess.Popen([sys.executable, "-c", """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
fcntl.flock(fd, fcntl.LOCK_SH)
print('locked', flush=True)
sys.stdin.read(1)
os.close(fd)
""", str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        report = await app.state.replay_router.cleanup_expired(now=ended + timedelta(days=31))
        assert report[IDENTIFIER] == "unconfirmed_or_in_use" and root.is_dir()
    finally:
        process.communicate("x", timeout=2)
    assert process.returncode == 0
    assert (await app.state.replay_router.cleanup_expired(now=ended + timedelta(days=31)))[IDENTIFIER] == "removed"


@pytest.mark.parametrize("action", ["pause", "detach", "close_failure", "invalid_close"])
async def test_pause_deploy_handoff_and_failed_close_have_no_end_proof(tmp_path, action):
    source = HistoricalSource()
    app = build_slot(tmp_path / "live.sqlite3", source)
    root = tmp_path / "replays" / IDENTIFIER
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            assert (await client.post("/api/replays", json=BODY)).status_code == 201
            if action == "pause":
                assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "pause"})).status_code == 200
            elif action in {"close_failure", "invalid_close"}:
                source.failure = action == "close_failure"
                if action == "invalid_close":
                    request = source._request

                    def incomplete_reply(method, path, *args, **kwargs):
                        return {} if path.endswith("/close") else request(method, path, *args, **kwargs)

                    source._request = incomplete_reply
                assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})).status_code == 200
            assert not (root / CLOSED_NAME).exists()
            assert (await app.state.replay_router.cleanup_expired(now=datetime.now(UTC) + timedelta(days=90)))[IDENTIFIER] != "removed"
    assert root.is_dir() and not (root / CLOSED_NAME).exists()


@pytest.mark.parametrize("reply", [None, [], {"accountType": "wrong"},
                                   {"accountType": "TqSim", "replayId": "b" * 32, "status": "closed"},
                                   {"accountType": "TqSim", "replayId": IDENTIFIER, "status": "error"}])
async def test_invalid_probe_never_becomes_close_proof(tmp_path, reply):
    source = HistoricalSource()
    app = build_slot(tmp_path / "live.sqlite3", source)
    router = app.state.replay_router
    router._write_metadata(IDENTIFIER, "local-user", BODY, {})

    async def invalid(*args, **kwargs):
        return reply

    source._request_async = invalid
    assert await router._adoptable_exists() is True
    root = tmp_path / "replays" / IDENTIFIER
    assert (root / "replay.json").exists() and not (root / CLOSED_NAME).exists()
    app.state.store.close()


@pytest.mark.parametrize("status", [404, 422, 503])
async def test_http_error_without_a_missing_replay_verdict_keeps_adoption(tmp_path, status):
    from trader_runtime.sidecar_http import SidecarHttpError
    source = HistoricalSource()
    app = build_slot(tmp_path / "live.sqlite3", source)
    router = app.state.replay_router
    router._write_metadata(IDENTIFIER, "local-user", BODY, {})

    async def unavailable(*args, **kwargs):
        raise SidecarHttpError("sidecar_http_error", status=status, payload={"error": "unavailable"})

    source._request_async = unavailable
    assert await router._adoptable_exists() is True
    assert (tmp_path / "replays" / IDENTIFIER / "replay.json").exists()
    assert not (tmp_path / "replays" / IDENTIFIER / CLOSED_NAME).exists()
    app.state.store.close()


async def test_generic_refusal_revokes_adoption_without_proof_and_allows_a_new_replay(tmp_path):
    source = HistoricalSource()
    app = build_slot(tmp_path / "live.sqlite3", source)
    root = tmp_path / "replays" / IDENTIFIER
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            assert (await client.post("/api/replays", json=BODY)).status_code == 201
            await app.state.replay_router.close()  # deploy-style detach, then the actor disappears
            source.gone = True
            assert (await client.get(f"/api/replays/{IDENTIFIER}")).status_code == 404
            assert not (root / "replay.json").exists() and not (root / CLOSED_NAME).exists()
            source.gone = False
            assert (await client.post("/api/replays", json=BODY)).status_code == 201
            assert not (root / CLOSED_NAME).exists()


async def test_explicit_closed_verdict_starts_the_clock_now_and_retries_do_not_reset_it(tmp_path):
    source = HistoricalSource()
    app = build_slot(tmp_path / "live.sqlite3", source)
    router = app.state.replay_router
    router._write_metadata(IDENTIFIER, "local-user", BODY, {})
    async def confirmed(*args, **kwargs):
        return {"replayId": IDENTIFIER, "accountType": "TqSim", "status": "closed"}

    source._request_async = confirmed
    before = datetime.now(UTC)
    assert await router._adoptable_exists() is False
    root = tmp_path / "replays" / IDENTIFIER
    proof = (root / CLOSED_NAME).read_text()
    assert datetime.fromisoformat(json.loads(proof)["closedAt"]) >= before
    closed_record(root, IDENTIFIER, now=before + timedelta(days=90))
    assert (root / CLOSED_NAME).read_text() == proof
    app.state.store.close()
