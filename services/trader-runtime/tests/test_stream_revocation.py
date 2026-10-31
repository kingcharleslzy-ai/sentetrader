"""Exercise revocation over real HTTP/SSE sockets, including idle readers.

2026-10-13 扩容① (owner-approved): a stream re-checks its caller once per second (RECHECK_SECONDS), not
before every event — a revocation ends the stream within that second, and events published inside that
second may still reach it."""
import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from hashlib import sha256

import httpx
import pytest
import uvicorn

from trader_runtime import auth as auth_module
from trader_runtime.auth import OWNER_COOKIE_NAME, hash_owner_password
from trader_runtime.broker import RECHECK_SECONDS
from trader_runtime.main import create_app

ORIGIN = "http://127.0.0.1:5173"
# The promise is "within RECHECK_SECONDS"; the slack covers the socket round trip on a loaded runner.
CLOSE_WITHIN = RECHECK_SECONDS + 0.5


@asynccontextmanager
async def serving(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0)); sock.listen(); sock.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 4)
        sock.close()
        app.state.store.close()


class Reader:
    def __init__(self, client, session):
        self.client, self.session = client, session
        self.messages = []
        self.opened, self.closed = asyncio.Event(), asyncio.Event()

    async def run(self):
        try:
            async with self.client.stream("GET", "/api/events", params={"sessionId": self.session}) as response:
                assert response.status_code == 200
                self.opened.set()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        self.messages.append(json.loads(line[6:]))
        finally:
            self.closed.set()

    def received(self, text):
        return any(item.get("payload", {}).get("text") == text for item in self.messages)

    async def closes_within(self, started, limit=CLOSE_WITHIN):
        await asyncio.wait_for(self.closed.wait(), limit + 1)
        elapsed = time.monotonic() - started
        assert elapsed <= limit, f"stream outlived its revocation by {elapsed:.2f}s"


async def publish(app, session, text):
    await app.state.broker.publish(session, kind="message", name="message.completed",
        payload={"turnId": "revocation-test", "role": "assistant", "agentId": "native", "text": text})


@pytest.mark.parametrize("busy", [False, True])
async def test_owner_logout_ends_original_stream_without_deleting_chat_or_relogin_revival(tmp_path, busy):
    app = create_app(tmp_path / "owner.sqlite3", agent_mode="fake", data_mode="fake", auth_mode="owner-password",
        owner_username="test-owner", owner_password_hash=hash_owner_password("isolated-demo-password"),
        owner_session_secret="isolated-owner-session-secret-more-than-32-bytes", cors_origins={ORIGIN})
    async with serving(app) as base, httpx.AsyncClient(base_url=base, headers={"Origin": ORIGIN}, trust_env=False) as client:
        login = {"username": "test-owner", "password": "isolated-demo-password"}
        assert (await client.post("/api/auth/owner", json=login)).status_code == 200
        created = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
        assert created.status_code == 201
        session = created.json()["sessionId"]
        reader = Reader(client, session)
        task = asyncio.create_task(reader.run())
        try:
            await asyncio.wait_for(reader.opened.wait(), 2)
            started = time.monotonic()
            assert (await client.post("/api/auth/owner/logout")).status_code == 200
            if busy:
                for index in range(20):  # a busy stream: events keep coming after the logout
                    await publish(app, session, f"after-logout-{index}")
                    await asyncio.sleep(.02)
            await reader.closes_within(started)
            await task
            assert (await client.post("/api/auth/owner", json=login)).status_code == 200
            assert (await client.get(f"/api/sessions/{session}")).status_code == 200
            await publish(app, session, "after-relogin")
            assert not reader.received("after-relogin")
            # New authorized reads can resume the same persisted chat.
            new = Reader(client, session)
            new_task = asyncio.create_task(new.run())
            try:
                async with asyncio.timeout(2):
                    while not new.received("after-relogin"):
                        await asyncio.sleep(.01)
            finally:
                new_task.cancel(); await asyncio.gather(new_task, return_exceptions=True)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("revocation", ["expiry", "delete"])
async def test_cloud_revocation_closes_only_affected_user_stream(tmp_path, revocation, monkeypatch):
    # The signed exp is whole seconds, so a real 1 s token lived anywhere from 0.1 s to 1 s and under
    # CI load could expire before its stream opened (verify-mac 10-05, 10-08). The auth module's clock
    # is held still instead, and the exp check itself still decides.
    clock = [time.time()]
    monkeypatch.setattr(auth_module, "time", type("Clock", (), {"time": staticmethod(lambda: clock[0]),
                                                                "monotonic": staticmethod(time.monotonic)}))
    app = create_app(tmp_path / "cloud.sqlite3", agent_mode="fake", data_mode="fake", auth_mode="cloud-jwt",
        cloud_jwt_secret="isolated-cloud-session-secret-more-than-32-bytes", cors_origins={ORIGIN})
    async with serving(app) as base, httpx.AsyncClient(base_url=base, headers={"Origin": ORIGIN}, trust_env=False) as a, \
            httpx.AsyncClient(base_url=base, headers={"Origin": ORIGIN}, trust_env=False) as b:
        sessions = []
        # Warm the app (the first request carries startup) so the short-lived expiry token
        # below is spent on the auth check, not on the app booting.
        await a.get("/api/runtime/status")
        for client, subject in [(a, "alice"), (b, "bob")]:
            token = app.state.auth.issue_for_test(tenant_id="acceptance", subject=subject,
                expires_in=1.0 if client is a and revocation == "expiry" else 3600)
            assert (await client.post("/api/auth/cloud", headers={"Authorization": "Bearer " + token})).status_code == 200
            created = await client.post("/api/sessions", json={"mode": "cloud", "agentId": "native"})
            assert created.status_code == 201
            sessions.append(created.json()["sessionId"])
        ra, rb = Reader(a, sessions[0]), Reader(b, sessions[1])
        tasks = [asyncio.create_task(r.run()) for r in (ra, rb)]
        try:
            await asyncio.wait_for(asyncio.gather(ra.opened.wait(), rb.opened.wait()), 2)
            started = time.monotonic()
            if revocation == "delete":
                assert (await a.delete(f"/api/sessions/{sessions[0]}")).status_code == 200
            else:
                # Let the signed token expire while the reader is idle: the 401 below is its exp at work.
                clock[0] += 2
                assert (await a.get("/api/runtime/status")).status_code == 401
                await publish(app, sessions[0], "after-expiry-A")
            await ra.closes_within(started)
            await tasks[0]
            await publish(app, sessions[1], "still-authorized-B")
            async with asyncio.timeout(2):
                while not rb.received("still-authorized-B"):
                    await asyncio.sleep(.01)
            assert not rb.closed.is_set()
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("change", ["revoked", "disabled", "expired"])
async def test_owner_stream_sees_store_side_revocation_within_a_second(tmp_path, change):
    """The stream re-reads the store once a second (one query since 2026-10-03; once per second
    instead of per event since 2026-10-13): a token revoked, a user disabled or a session expired
    by another writer — the other blue-green slot, an operator — ends that stream within the
    second, with nothing in this process's memory to notice it first. Others keep streaming."""
    app = create_app(tmp_path / "owner-store.sqlite3", agent_mode="fake", data_mode="fake",
        auth_mode="owner-password", owner_username="test-owner",
        owner_password_hash=hash_owner_password("isolated-demo-password"),
        owner_session_secret="isolated-owner-session-secret-more-than-32-bytes", cors_origins={ORIGIN},
        registration_enabled=True)
    async with serving(app) as base, \
            httpx.AsyncClient(base_url=base, headers={"Origin": ORIGIN}, trust_env=False) as owner, \
            httpx.AsyncClient(base_url=base, headers={"Origin": ORIGIN}, trust_env=False) as friend:
        login = {"username": "test-owner", "password": "isolated-demo-password"}
        assert (await owner.post("/api/auth/owner", json=login)).status_code == 200
        code = (await owner.post("/api/auth/owner/invite-codes")).json()["inviteCode"]
        registered = await friend.post("/api/auth/owner/register", json={
            "username": "friend", "password": "a-solid-password-123", "inviteCode": code})
        assert registered.status_code == 200
        app.state.store.grant_account(account_id=app.state.accounts.default.account_id,
                                      user_id=registered.json()["userId"], role="operator", granted_by="test-owner")
        sessions = []
        for client in (owner, friend):
            created = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
            assert created.status_code == 201
            sessions.append(created.json()["sessionId"])
        readers = [Reader(owner, sessions[0]), Reader(friend, sessions[1])]
        tasks = [asyncio.create_task(reader.run()) for reader in readers]
        try:
            await asyncio.wait_for(asyncio.gather(*(reader.opened.wait() for reader in readers)), 2)
            await publish(app, sessions[1], "before-change")
            async with asyncio.timeout(2):
                while not readers[1].received("before-change"):
                    await asyncio.sleep(.01)
            store = app.state.store
            started = time.monotonic()
            if change == "revoked":
                token = friend.cookies.get(OWNER_COOKIE_NAME)
                assert token
                now = int(time.time())
                store.revoke_owner_session(sha256(token.encode()).hexdigest(), now + 3600, now)
            elif change == "disabled":
                with store.lock, store.connection:
                    store.connection.execute("UPDATE cloud_users SET disabled = 1 WHERE username = 'friend'")
            else:
                with store.lock, store.connection:
                    store.connection.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'"
                                             " WHERE id = ?", (sessions[1],))
            await publish(app, sessions[1], "after-change")
            await readers[1].closes_within(started)
            await publish(app, sessions[0], "owner-still-live")
            async with asyncio.timeout(2):
                while not readers[0].received("owner-still-live"):
                    await asyncio.sleep(.01)
            assert not readers[0].closed.is_set()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
