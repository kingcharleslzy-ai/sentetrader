"""M1a 多人接入：按需装配、快期密码托管、闲置回收与容量排队。

池的 HTTP 边界（PoolClient）用假客户端替身；凭据盒、resolve 触发装配、
presence 回收全部走真实代码。绑定走没配本站模拟盘时快期登录用的 bind_tqkq_account
（绑定/解绑接口随 #1205 快期模拟停用交易下线）。
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

import trader_runtime.account_pool as account_pool_module
from test_stream_revocation import ORIGIN
from trader_runtime.account_ledger import AccountLedger
from trader_runtime.account_pool import PoolCapacityFull, PoolStartRefused
from trader_runtime.credential_box import CredentialBox, CredentialKeyError
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.routes.account import bind_tqkq_account

TOKEN = "pool-runtime-test-token-with-at-least-32-chars"
SIDECAR_TOKEN = "sidecar-test-token-with-at-least-32-characters"
POOL_URL = "http://127.0.0.1:19010"
USERNAME = "kq-test-user"
PASSWORD = "kq-test-pass"


def bind_account_id(username: str = USERNAME) -> str:
    import hashlib

    return f"tqkq-sim:{hashlib.sha256(username.encode()).hexdigest()[:12]}"


class NamedFakeProvider(FakeReadOnlyDataProvider):
    """A fake provider whose account card names the account it serves."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def account_summary(self, turn_id: str) -> dict[str, object]:
        return {**super().account_summary(turn_id), "accountName": self.name}


class FakePoolClient:
    """The pool control-plane double: records every call, never leaves memory.
    `entries` answers from what start/stop left running, and a start of an
    already-running account returns its existing child — the real pool's
    idempotent takeover, so blue-green and self-heal paths test real semantics."""

    def __init__(self) -> None:
        self.starts: list[tuple[str, str, str]] = []
        self.stops: list[str] = []
        self.verifies: list[tuple[str, str]] = []
        self.refuse: str | None = None
        self.capacity = False
        self.running: dict[str, int] = {}
        self.spawns: list[str] = []
        # Per spawn, like the real pool: childId (listed) and the child's own token.
        self.children: dict[str, tuple[str, str]] = {}
        self.market_only_starts: list[bool] = []

    def _answer(self, account_id: str) -> dict[str, object]:
        child_id, token = self.children[account_id]
        return {"port": self.running[account_id], "childId": child_id, "token": token,
                "startedAt": "2026-09-22T00:00:00Z"}

    async def start(self, account_id: str, username: str, password: str, *,
                    market_only: bool = False) -> dict[str, object]:
        self.starts.append((account_id, username, password))
        self.market_only_starts.append(market_only)
        if account_id in self.running:
            return self._answer(account_id)  # idempotent takeover
        if self.capacity:
            raise PoolCapacityFull("capacity_full")
        if self.refuse is not None:
            raise PoolStartRefused(self.refuse)
        self.spawn(account_id, 19100 + len(self.spawns))
        return self._answer(account_id)

    def spawn(self, account_id: str, port: int) -> None:
        """A new child for the account on `port` (also what a crash-and-restart looks like)."""
        index = len(self.spawns)
        self.spawns.append(account_id)
        self.running[account_id] = port
        self.children[account_id] = (f"child-{index:04d}", f"child-token-{index:04d}-" + "x" * 32)

    async def stop(self, account_id: str) -> dict[str, object]:
        self.stops.append(account_id)
        self.running.pop(account_id, None)
        return {"accountId": account_id, "stopped": True}

    async def verify(self, username: str, password: str) -> dict[str, object]:
        self.verifies.append((username, password))
        return {"ok": True, "kqUsername": username}

    async def entries(self) -> dict[str, object]:
        accounts = [
            {"accountId": account_id, "port": port, "status": "running",
             "childId": self.children[account_id][0]}
            for account_id, port in sorted(self.running.items())
        ]
        return {"accounts": accounts, "capacity": {"max": 8, "running": len(accounts)}}


def pool_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str):
    """An app with the pool wired: real key file and manager, fake HTTP client."""
    key_file = tmp_path / f"{database}.key"
    key_file.write_bytes(base64.b64encode(os.urandom(32)))
    monkeypatch.setenv("AI_TRADER_CREDENTIAL_KEY_FILE", str(key_file))
    monkeypatch.setenv("AI_TRADER_TQKQ_POOL_URL", POOL_URL)
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_TOKEN", SIDECAR_TOKEN)
    application = create_app(tmp_path / database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    fake_client = FakePoolClient()
    manager = application.state.account_pool
    assert manager is not None
    manager.client = fake_client
    specs: list = []
    real_build = account_pool_module.build_account_services

    def recording_build(spec, **kwargs):
        specs.append(spec)
        return real_build(spec, injected=NamedFakeProvider(spec.id), **kwargs)

    monkeypatch.setattr(account_pool_module, "build_account_services", recording_build)
    return application, fake_client, specs


async def local_client(application) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}"},
    )
    response = await client.post("/api/auth/local")
    assert response.status_code == 200
    return client


async def bind(application, client, *, username=USERNAME, password=PASSWORD):
    """What a TqKq login binds for the local user on a deployment without the paper engine."""
    return await bind_tqkq_account(application.state.store, application.state.account_pool, user_id="local-user",
                                   username=username, password=password, kq_username=username)


def test_credential_box_roundtrip_and_wrong_aad():
    box = CredentialBox(base64.b64decode(base64.b64encode(os.urandom(32)).decode()))
    ciphertext, nonce = box.seal(user_id="user-a", password="s3cret-密码")
    assert box.open(user_id="user-a", ciphertext=ciphertext, nonce=nonce) == "s3cret-密码"
    # 同一密文换一个 user_id（AAD）打不开；库里拖走的密文不属于别人。
    with pytest.raises(CredentialKeyError):
        box.open(user_id="user-b", ciphertext=ciphertext, nonce=nonce)
    with pytest.raises(CredentialKeyError):
        CredentialBox(b"short")


def test_pool_url_without_key_file_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_TRADER_TQKQ_POOL_URL", POOL_URL)
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_TOKEN", SIDECAR_TOKEN)
    monkeypatch.delenv("AI_TRADER_CREDENTIAL_KEY_FILE", raising=False)
    with pytest.raises(RuntimeError, match="tqkq_pool_requires_credential_key"):
        create_app(tmp_path / "nokey.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake")


async def test_bind_assembles_switches_default_and_keeps_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client, specs = pool_app(tmp_path, monkeypatch, "bind.sqlite3")
    try:
        store = application.state.store
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            bound = await bind(application, client)
            assert bound["accountId"] == account_id
            # 口令经内存传给池客户端，不落任何日志字段。
            assert fake_client.starts == [(account_id, USERNAME, PASSWORD)]
            assert specs[-1].sidecar_url == "http://127.0.0.1:19100"
            # 密码只以密文入册。
            credentials = store.load_tqkq_credentials(user_id="local-user")
            assert credentials is not None and credentials["account_id"] == account_id
            assert PASSWORD not in repr(credentials) and bytes(credentials["ciphertext"]) != PASSWORD.encode()
            # 目录两项：默认账户 + 新绑账户（connected）。
            directory = (await client.get("/api/accounts")).json()
            assert [item["id"] for item in directory["accounts"]] == ["tqkq-sim:default", account_id]
            pool_item = directory["accounts"][1]
            assert pool_item["providerKind"] == "tqkq-pool" and pool_item["poolStatus"] == "connected"
            assert pool_item["role"] == "owner" and pool_item["executionOwner"] is True
            # 新会话默认落在自己绑的账户上。
            created = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
            assert created.status_code == 201
            assert store.get_session(created.json()["sessionId"])["tradingAccountId"] == account_id
            summary = (await client.get("/api/account/summary")).json()
            assert summary["accountName"] == account_id
            # 主账号不受影响：显式头部仍读默认账户（假数据默认卡，不带池账户名）。
            default_summary = await client.get(
                "/api/account/summary", headers={"X-AI-Trader-Account": "tqkq-sim:default"})
            assert default_summary.status_code == 200
            assert default_summary.json()["accountName"] == "假数据模拟账户"
        finally:
            await client.aclose()
    finally:
        application.state.store.close()


async def test_release_then_request_reassembles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client, _ = pool_app(tmp_path, monkeypatch, "release.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
            assert await manager.release(account_id, reason="idle") is True
            assert fake_client.stops == [account_id]
            assert application.state.accounts.get(account_id) is None
            # 主账号永远不回收。
            assert await manager.release("tqkq-sim:default", reason="idle") is False
            # 下一次请求就是重新上线的信号：自动重装配。
            summary = await client.get("/api/account/summary")
            assert summary.status_code == 200 and summary.json()["accountName"] == account_id
            assert len(fake_client.starts) == 2
        finally:
            await client.aclose()
    finally:
        application.state.store.close()


async def test_capacity_full_queues_with_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client, _ = pool_app(tmp_path, monkeypatch, "capacity.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
            await manager.release(account_id, reason="idle")
            fake_client.capacity = True
            summary = await client.get("/api/account/summary")
            assert summary.status_code == 503
            assert summary.json()["detail"] == "runtime_capacity_full"
            # 目录仍列出该账户，状态 queued：已绑定、等容量。
            pool_item = next(
                item for item in (await client.get("/api/accounts")).json()["accounts"]
                if item["id"] == account_id)
            assert pool_item["poolStatus"] == "queued"
            fake_client.capacity = False
            assert (await client.get("/api/account/summary")).status_code == 200
        finally:
            await client.aclose()
    finally:
        application.state.store.close()


async def test_start_refusal_maps_to_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client, _ = pool_app(tmp_path, monkeypatch, "refused.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
            await manager.release(account_id, reason="idle")
            fake_client.refuse = "auth_failed"
            summary = await client.get("/api/account/summary")
            assert summary.status_code == 502
            assert summary.json()["detail"] == {"code": "account_start_failed", "reason": "auth_failed"}
        finally:
            await client.aclose()
    finally:
        application.state.store.close()


async def test_idle_scan_releases_only_pool_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client, _ = pool_app(tmp_path, monkeypatch, "idle.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
            manager.idle_seconds = 5.0
            await manager._scan_idle()  # bind 刚 touch 过：都在线，不回收。
            assert application.state.accounts.get(account_id) is not None
            manager.idle_seconds = 0.01
            import asyncio

            await asyncio.sleep(0.02)
            await manager._scan_idle()
            assert application.state.accounts.get(account_id) is None
            assert fake_client.stops == [account_id]
            # 主账号始终装配。
            assert application.state.accounts.get("tqkq-sim:default") is not None
        finally:
            await client.aclose()
    finally:
        application.state.store.close()


def bluegreen_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str,
) -> tuple:
    """Two Runtime instances (old slot A, new slot B) over one state database
    and one shared fake pool — the blue-green overlap window."""
    key_file = tmp_path / f"{database}.key"
    key_file.write_bytes(base64.b64encode(os.urandom(32)))
    monkeypatch.setenv("AI_TRADER_CREDENTIAL_KEY_FILE", str(key_file))
    monkeypatch.setenv("AI_TRADER_TQKQ_POOL_URL", POOL_URL)
    monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_TOKEN", SIDECAR_TOKEN)
    slot_a = create_app(tmp_path / database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    slot_b = create_app(tmp_path / database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    shared_pool = FakePoolClient()
    slot_a.state.account_pool.client = shared_pool
    slot_b.state.account_pool.client = shared_pool
    real_build = account_pool_module.build_account_services

    def recording_build(spec, **kwargs):
        return real_build(spec, injected=NamedFakeProvider(spec.id), **kwargs)

    monkeypatch.setattr(account_pool_module, "build_account_services", recording_build)
    return slot_a, slot_b, shared_pool


async def test_bluegreen_sibling_exit_keeps_member_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """蓝绿换槽：旧槽 A 装配成员后新槽 B 接管同一子连接（不重启）；A 退出只
    分离自己的装配、不停池子里的子连接，B 上的请求保持 200。子连接随后被
    外部停掉（旧缺陷的行为/池子重启），B 的下一次请求自动重新装配，而不是
    持续 503。"""
    slot_a, slot_b, shared_pool = bluegreen_pair(tmp_path, monkeypatch, "bluegreen.sqlite3")
    try:
        account_id = bind_account_id()
        client_a = await local_client(slot_a)
        client_b = await local_client(slot_b)
        try:
            await bind(slot_a, client_a)
            assert shared_pool.spawns == [account_id]
            # B 接管：同一子连接，不起第二个进程。
            summary = await client_b.get("/api/account/summary")
            assert summary.status_code == 200 and summary.json()["accountName"] == account_id
            assert shared_pool.spawns == [account_id] and len(shared_pool.starts) == 2
            # A（旧槽）退出：只分离，不停子连接。
            await slot_a.state.account_pool.shutdown()
            assert shared_pool.stops == [] and account_id in shared_pool.running
            assert slot_a.state.accounts.get(account_id) is None
            summary = await client_b.get("/api/account/summary")
            assert summary.status_code == 200 and summary.json()["accountName"] == account_id
            # 子连接从 B 脚下被停掉：下一次请求自愈，重新装配。
            shared_pool.running.pop(account_id)
            summary = await client_b.get("/api/account/summary")
            assert summary.status_code == 200 and summary.json()["accountName"] == account_id
            assert shared_pool.spawns == [account_id, account_id]
            assert account_id in shared_pool.running
        finally:
            await client_a.aclose()
            await client_b.aclose()
    finally:
        slot_a.state.store.close()
        slot_b.state.store.close()


async def test_assembly_binds_to_the_childs_own_token_and_heals_on_port_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口复用串号（2026-10-12 审查）：装配用子进程自己的 token，不用共享 token。A 的子进程
    崩溃、另一个账户的子进程拿到同一端口：A 的旧装配不算“还连着”（childId 对不上），请求
    时摘掉并在 A 的新子进程上重装；A 的装配从不指向 B 的子进程或 B 的 token。"""
    application, fake_client, specs = pool_app(tmp_path, monkeypatch, "reuse.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
            first_child, first_token = fake_client.children[account_id]
            old_port = fake_client.running[account_id]
            assert specs[-1].sidecar_token == first_token != SIDECAR_TOKEN
            assert first_token not in repr(specs[-1])  # never in a log line
            # A crashes; another account's child is started on the very same port.
            fake_client.running.pop(account_id)
            fake_client.spawn("tqkq-sim:otheraccount", old_port)
            other_token = fake_client.children["tqkq-sim:otheraccount"][1]
            assert await manager.child_lost(account_id) is True
            summary = await client.get("/api/account/summary")
            assert summary.status_code == 200 and summary.json()["accountName"] == account_id
            new_child, new_token = fake_client.children[account_id]
            assert new_child != first_child and specs[-1].sidecar_token == new_token
            assert new_token not in {first_token, other_token}
            assert specs[-1].sidecar_url == f"http://127.0.0.1:{fake_client.running[account_id]}"
            assert manager._children[account_id] == (fake_client.running[account_id], new_child)
            # A restarted on its own old port (a sibling slot's start): same port, new child —
            # still lost for this wiring, never taken as "the same child".
            port = fake_client.running[account_id]
            fake_client.spawn(account_id, port)
            assert await manager.child_lost(account_id) is True
        finally:
            await client.aclose()
    finally:
        await application.state.account_pool.shutdown()
        application.state.store.close()


async def test_idle_scan_detaches_and_reassembles_a_replaced_child_without_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台循环不等请求：扫描发现子进程已换人，摘掉旧装配；用户仍在线就在新子进程上
    重装（不算用户上线，不刷新在线时间），用户都不在就只摘不装。"""
    application, fake_client, specs = pool_app(tmp_path, monkeypatch, "scanheal.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
            manager.idle_seconds = 3600.0
            fake_client.spawn(account_id, fake_client.running[account_id])  # crashed and restarted
            seen_before = dict(manager.presence._last_seen)
            await manager._scan_idle()
            services = application.state.accounts.get(account_id)
            assert services is not None
            assert specs[-1].sidecar_token == fake_client.children[account_id][1]
            assert manager.presence._last_seen == seen_before
            assert await manager.child_lost(account_id) is False
            # Everyone gone: the replaced child's wiring is dropped, nothing reassembled.
            fake_client.spawn(account_id, fake_client.running[account_id])
            manager.idle_seconds = 0.0
            assemblies = len(specs)
            await manager._heal_lost_children()
            assert application.state.accounts.get(account_id) is None and len(specs) == assemblies
        finally:
            await client.aclose()
    finally:
        await application.state.account_pool.shutdown()
        application.state.store.close()


async def test_a_pool_without_child_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧池不发子进程 token 时拒绝装配（fail-closed），不退回所有子进程都认的共享 token。"""
    application, fake_client, specs = pool_app(tmp_path, monkeypatch, "notoken.sqlite3")
    try:
        client = await local_client(application)
        try:
            real_start = fake_client.start

            async def legacy_start(account_id, username, password):
                answer = await real_start(account_id, username, password)
                return {key: value for key, value in answer.items() if key != "token"}

            fake_client.start = legacy_start  # type: ignore[method-assign]
            with pytest.raises(HTTPException):  # the binding login answers 409 tqkq_bind_failed
                await bind(application, client)
            account_id = bind_account_id()
            assert application.state.accounts.get(account_id) is None and specs == []
            summary = await client.get("/api/account/summary", headers={"X-AI-Trader-Account": account_id})
            assert summary.status_code == 502
            assert summary.json()["detail"] == {"code": "account_start_failed", "reason": "pool_protocol_error"}
        finally:
            await client.aclose()
    finally:
        await application.state.account_pool.shutdown()
        application.state.store.close()


async def test_boot_adopts_orphans_and_reclaims_when_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧槽退出留下的、新槽尚未接管的子连接：新进程启动时收养（不装配），
    启动后的完整空闲窗口内保留（旧槽可能还在服务、用户心跳可能未到新槽），
    超过 idle_seconds 仍无人出现才回收停掉。"""
    slot_a, slot_b, shared_pool = bluegreen_pair(tmp_path, monkeypatch, "orphan.sqlite3")
    try:
        account_id = bind_account_id()
        client_a = await local_client(slot_a)
        try:
            await bind(slot_a, client_a)
            await slot_a.state.account_pool.shutdown()  # 旧槽退出，子连接留在池里
            manager_b = slot_b.state.account_pool
            await manager_b.start_background()
            # 收养但不装配：用户没来，新槽不主动接线。
            assert manager_b._adopted == {account_id}
            assert slot_b.state.accounts.get(account_id) is None
            # 启动后立即扫描：从未见过的用户从进程启动起算空闲，不回收。
            manager_b.idle_seconds = 50.0
            await manager_b._scan_idle()
            assert shared_pool.stops == [] and manager_b._adopted == {account_id}
            # 推过空闲线后回收。
            manager_b._started -= manager_b.idle_seconds + 1.0
            await manager_b._scan_idle()
            assert manager_b._adopted == set()
            assert shared_pool.stops == [account_id]
            assert account_id not in shared_pool.running
        finally:
            await client_a.aclose()
    finally:
        slot_a.state.store.close()
        slot_b.state.store.close()


async def test_adopted_orphan_is_taken_over_by_user_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收养的孤儿在用户到来时由 ensure 正常接管，转入常规空闲回收，不再
    被孤儿分支停掉。"""
    slot_a, slot_b, shared_pool = bluegreen_pair(tmp_path, monkeypatch, "takeover.sqlite3")
    try:
        account_id = bind_account_id()
        client_a = await local_client(slot_a)
        client_b = await local_client(slot_b)
        try:
            await bind(slot_a, client_a)
            await slot_a.state.account_pool.shutdown()
            manager_b = slot_b.state.account_pool
            await manager_b.start_background()
            assert manager_b._adopted == {account_id}
            summary = await client_b.get("/api/account/summary")
            assert summary.status_code == 200 and summary.json()["accountName"] == account_id
            assert manager_b._adopted == set()
            assert shared_pool.spawns == [account_id]  # 接管现有子连接，不重启
            manager_b.idle_seconds = 5.0
            await manager_b._scan_idle()
            # 刚 touch 过：常规回收在线判定保护它，孤儿分支也不再持有。
            assert shared_pool.stops == []
        finally:
            await client_a.aclose()
            await client_b.aclose()
    finally:
        slot_a.state.store.close()
        slot_b.state.store.close()


async def test_release_stops_adopted_orphan_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """释放（快期交易停用 #1205 的整套解绑走它）一个未被本进程装配的收养孤儿：池里的子连接
    也要停掉，不能因为“没装配”而漏停。"""
    slot_a, slot_b, shared_pool = bluegreen_pair(tmp_path, monkeypatch, "unbindorphan.sqlite3")
    try:
        account_id = bind_account_id()
        client_a = await local_client(slot_a)
        client_b = await local_client(slot_b)
        try:
            await bind(slot_a, client_a)
            await slot_a.state.account_pool.shutdown()
            manager_b = slot_b.state.account_pool
            await manager_b.start_background()
            assert manager_b._adopted == {account_id}
            assert await manager_b.release(account_id, reason="tqkq_retired") is True
            assert shared_pool.stops == [account_id]
            assert account_id not in shared_pool.running
            assert manager_b._adopted == set()
        finally:
            await client_a.aclose()
            await client_b.aclose()
    finally:
        slot_a.state.store.close()
        slot_b.state.store.close()


async def test_standby_slot_assembles_without_running_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """备用槽不得双跑池账户的下单/记账循环：standby 槽接管成员请求只装配
    只读服务（leader 还在跑循环）；旧槽退出、租约落到备用槽后，
    lease_acquired 遍历含池账户，循环才补启动。"""
    from trader_runtime.strategy_monitor import StrategyMonitorService

    monitor_starts: list[str] = []
    real_monitor_start = StrategyMonitorService.start

    async def recording_monitor_start(self: StrategyMonitorService) -> None:
        monitor_starts.append(self.account_id)
        await real_monitor_start(self)

    monkeypatch.setattr(StrategyMonitorService, "start", recording_monitor_start)
    slot_a, slot_b, shared_pool = bluegreen_pair(tmp_path, monkeypatch, "standby.sqlite3")
    account_id = bind_account_id()
    lifespan_a = slot_a.router.lifespan_context(slot_a)
    lifespan_b = None
    client_a = client_b = None
    try:
        await lifespan_a.__aenter__()
        assert slot_a.state.instance_lease.role == "leader"
        client_a = await local_client(slot_a)
        await bind(slot_a, client_a)
        # leader 槽照常装配并启动循环。
        services_a = slot_a.state.accounts.get(account_id)
        assert services_a is not None
        assert services_a.account_ledger._task is not None
        assert services_a.autonomy._monitor_task is not None
        assert monitor_starts.count(account_id) == 1

        lifespan_b = slot_b.router.lifespan_context(slot_b)
        await lifespan_b.__aenter__()
        assert slot_b.state.instance_lease.role == "standby"
        client_b = await local_client(slot_b)
        summary = await client_b.get("/api/account/summary")
        assert summary.status_code == 200 and summary.json()["accountName"] == account_id
        assert shared_pool.spawns == [account_id]  # 接管同一子连接
        services_b = slot_b.state.accounts.get(account_id)
        assert services_b is not None
        assert services_b.account_ledger._task is None
        assert services_b.autonomy._monitor_task is None
        assert monitor_starts.count(account_id) == 1  # 循环仍只有 leader 在跑

        # 旧槽退出（释放租约）；备用槽一个心跳内拿到租约，循环补启动。
        await lifespan_a.__aexit__(None, None, None)
        lifespan_a = None
        await slot_b.state.instance_lease._tick()
        assert slot_b.state.instance_lease.role == "leader"
        assert services_b.account_ledger._task is not None
        assert services_b.autonomy._monitor_task is not None
        assert monitor_starts.count(account_id) == 2
    finally:
        if lifespan_a is not None:
            await lifespan_a.__aexit__(None, None, None)
        if lifespan_b is not None:
            await lifespan_b.__aexit__(None, None, None)
        if client_a is not None:
            await client_a.aclose()
        if client_b is not None:
            await client_b.aclose()
        slot_a.state.store.close()
        slot_b.state.store.close()


async def test_never_seen_users_idle_from_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已装配账户的授权用户在本进程从未心跳（例如主账号在后台触发装配）：
    空闲从进程启动起算，idle_seconds 内不被第一轮扫描回收。"""
    from trader_runtime.presence import PresenceTracker

    application, fake_client, _ = pool_app(tmp_path, monkeypatch, "neverseen.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
            assert application.state.accounts.get(account_id) is not None
            manager.idle_seconds = 50.0
            # 假装本进程从没见过任何用户（装配后、扫描前清掉全部心跳）。
            manager.presence = PresenceTracker()
            await manager._scan_idle()
            assert application.state.accounts.get(account_id) is not None
            assert fake_client.stops == []
            # 超过进程启动后的完整空闲窗口才回收。
            manager._started -= manager.idle_seconds + 1.0
            await manager._scan_idle()
            assert application.state.accounts.get(account_id) is None
            assert fake_client.stops == [account_id]
        finally:
            await client.aclose()
    finally:
        application.state.store.close()


async def test_pool_assembly_pushes_order_changes_like_static_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#479: an on-demand pool account gets the static boot's order-change wiring
    — an order link change pushes the activity and backfills fills at once —
    exactly one per assembly, and none left behind after detach."""
    application, _fake_client, _ = pool_app(tmp_path, monkeypatch, "orders-wiring.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
        finally:
            await client.aclose()
        pushed: list[str] = []
        monkeypatch.setattr(application.state.runtime_feed, "orders_changed", lambda: pushed.append("orders"))
        backfilled: list[str | None] = []
        monkeypatch.setattr(
            "trader_runtime.wiring.services.backfill_order_fills",
            lambda store, provider, trading_account_id=None: backfilled.append(trading_account_id) or 0,
        )
        services = application.state.accounts.get(account_id)
        assert services is not None
        services.simulation_orders._fire_change()
        assert pushed == ["orders"] and backfilled == [account_id]
        await manager.detach(account_id, reason="test")
        assert services.simulation_orders.on_change is None
        again = await manager.ensure(account_id, reason="test")
        again.simulation_orders._fire_change()
        assert pushed == ["orders", "orders"] and backfilled == [account_id, account_id]
    finally:
        application.state.store.close()


async def test_cancelled_pool_assembly_leaves_nothing_and_the_retry_starts_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#482: a caller cancelled while the assembly waits on the first ledger read
    leaves no half-built services registered and no state stuck at connecting;
    the next ensure assembles afresh with every loop running. The pool child
    stays (a blue-green sibling may share it)."""
    application, fake_client, _ = pool_app(tmp_path, monkeypatch, "cancelled.sqlite3")
    try:
        manager = application.state.account_pool
        client = await local_client(application)
        try:
            account_id = bind_account_id()
            await bind(application, client)
        finally:
            await client.aclose()
        await manager.detach(account_id, reason="test")
        entered = asyncio.Event()
        real_run_once = AccountLedger.run_once
        calls: list[AccountLedger] = []

        async def first_read_hangs(self: AccountLedger) -> dict[str, int]:
            calls.append(self)
            if len(calls) == 1:
                entered.set()
                await asyncio.Event().wait()
            return await real_run_once(self)

        monkeypatch.setattr(AccountLedger, "run_once", first_read_hangs)
        assembling = asyncio.create_task(manager.ensure(account_id, reason="test"))
        await asyncio.wait_for(entered.wait(), 5.0)
        half = application.state.accounts.get(account_id)
        assert half is not None and manager.account_state(account_id) == "connecting"
        assembling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await assembling
        assert application.state.accounts.get(account_id) is None
        assert manager.account_state(account_id) == "offline"
        assert half.simulation_orders.on_change is None
        assert fake_client.stops == []
        services = await manager.ensure(account_id, reason="retry")
        assert services is not half
        assert manager.account_state(account_id) == "connected"
        assert services.account_ledger is not None and services.account_ledger._task is not None
        assert services.autonomy._monitor_task is not None
        assert len(calls) == 2  # the retry booked the account afresh
        await manager.shutdown()
    finally:
        application.state.store.close()
