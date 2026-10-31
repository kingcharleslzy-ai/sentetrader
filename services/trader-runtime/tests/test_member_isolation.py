"""2026-09-23 多用户数据隔离：成员不得触达主账号（默认）交易账户。

覆盖五件事：自动授权机制删除后首登与两次重启的状态、启动迁移对历史
自动授权行的清理（幂等、声明式 grants 不受影响）、成员以自己账户 /
主账号账户 / 不带账户三种方式请求各账户表面的读写边界、事件流对失权
会话的拒绝、无绑定账户成员的 tqkq_account_required 业务状态，以及
owner 行为不变。登录、池与会话全部走真实代码，只有池控制面是替身。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import pytest

from test_owner_auth import ORIGIN, PASSWORD
from test_strategy_workbench import plan
from test_stream_revocation import Reader, serving
from test_tqkq_login import (
    DEFAULT_ACCOUNT,
    KQ_USER,
    client_for,
    owner_minted_invite,
    scoped_owner_id,
    tqkq_app,
    tqkq_login,
)
from trader_runtime.auth import hash_owner_password
from trader_runtime.store import RuntimeStore

OWNER = scoped_owner_id("charles")
MEMBER = scoped_owner_id(KQ_USER)
MEMBER_ACCOUNT = f"tqkq-sim:{_[:12]}" if (_ := __import__("hashlib").sha256(KQ_USER.encode()).hexdigest()) else ""


def grant_rows(store: RuntimeStore, account_id: str) -> list[tuple[str, str]]:
    return sorted((row[0], row[1]) for row in store.connection.execute(
        "SELECT user_id, role FROM account_grants WHERE account_id = ?", (account_id,),
    ).fetchall())


async def member_client(application) -> httpx.AsyncClient:
    """A logged-in member (own TqKq account bound); caller closes it."""
    client = client_for(application)
    code = await owner_minted_invite(application)
    login = await tqkq_login(client, invite_code=code)
    assert login.status_code == 200, login.text
    return client


async def owner_client(application) -> httpx.AsyncClient:
    client = client_for(application)
    login = await client.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})
    assert login.status_code == 200, login.text
    return client


async def test_first_login_and_restarts_leave_default_account_to_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) 成员首登 + 两次重启：默认账户始终只有 owner 行。"""
    application, _ = tqkq_app(tmp_path, monkeypatch, "iso-a.sqlite3")
    try:
        client = await member_client(application)
        try:
            # 首登即绑定自己的池账户：授权只在那里。
            assert grant_rows(application.state.store, MEMBER_ACCOUNT) == [(MEMBER, "owner")]
            assert grant_rows(application.state.store, DEFAULT_ACCOUNT) == [(OWNER, "owner")]
        finally:
            await client.aclose()
    finally:
        application.state.store.close()
    for _ in range(2):
        application, _ = tqkq_app(tmp_path, monkeypatch, "iso-a.sqlite3")
        try:
            # 自动授权机制（auto_operator_for_cloud_users）已删除：重启不再回填。
            assert grant_rows(application.state.store, DEFAULT_ACCOUNT) == [(OWNER, "owner")]
            assert grant_rows(application.state.store, MEMBER_ACCOUNT) == [(MEMBER, "owner")]
        finally:
            application.state.store.close()


def test_startup_migration_purges_auto_grant_rows_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) 迁移删三种自动签名行、保留 owner 与他人签名行；幂等；声明式不受影响。"""
    database = "iso-b.sqlite3"
    store = RuntimeStore(tmp_path / database)
    try:
        store.ensure_trading_account(
            DEFAULT_ACCOUNT, execution_owner_user_id=OWNER, provider_kind="fake",
            display_name="假数据模拟账户", sidecar_url=None,
        )
        member2, member3 = scoped_owner_id("second"), scoped_owner_id("third")
        # 历史残留的三种来源：启动循环（执行负责人签名）、注册默认（system）、
        # 邀请码注册（invite-code）；watcher 是别的手工签名，必须保留。
        store.grant_account(account_id=DEFAULT_ACCOUNT, user_id=MEMBER, role="operator", granted_by=OWNER)
        store.grant_account(account_id=DEFAULT_ACCOUNT, user_id=member2, role="operator", granted_by="system")
        store.grant_account(account_id=DEFAULT_ACCOUNT, user_id=member3, role="viewer", granted_by="invite-code")
        store.grant_account(account_id=DEFAULT_ACCOUNT, user_id="watcher", role="viewer", granted_by="admin-script")
    finally:
        store.close()

    def boot() -> RuntimeStore:
        application, _ = tqkq_app(tmp_path, monkeypatch, database, pool=False)
        return application.state.store

    monkeypatch.delenv("AI_TRADER_ACCOUNTS_FILE", raising=False)
    first = boot()
    try:
        assert grant_rows(first, DEFAULT_ACCOUNT) == sorted([
            (OWNER, "owner"), ("watcher", "viewer"),
        ])
        # 幂等：行已删净，再跑删除零行。
        assert first.purge_default_account_auto_grants() == 0
    finally:
        first.close()
    second = boot()
    try:
        assert grant_rows(second, DEFAULT_ACCOUNT) == sorted([
            (OWNER, "owner"), ("watcher", "viewer"),
        ])
    finally:
        second.close()

    # 声明式 grants 的账户不跑迁移：文件声明的集合（含 operator 成员）原样保留。
    accounts_file = tmp_path / "iso-b-accounts.json"
    accounts_file.write_text(
        '{"accounts": [{"id": "' + DEFAULT_ACCOUNT + '", "displayName": "甲", "dataMode": "fake",'
        ' "grants": {"operator": ["u1"]}}]}', encoding="utf-8")
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(accounts_file))
    declared = boot()
    try:
        assert grant_rows(declared, DEFAULT_ACCOUNT) == sorted([
            (OWNER, "owner"), (scoped_owner_id("u1"), "operator"),
        ])
    finally:
        declared.close()


async def test_member_surfaces_stay_on_their_own_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) 成员以自己账户 id、主账号账户 id、不带账户 id 三种方式请求。"""
    application, _ = tqkq_app(tmp_path, monkeypatch, "iso-c.sqlite3")
    owner, member = await owner_client(application), await member_client(application)
    try:
        # 不带账户 id：解析到成员自己的池账户（默认账户 = 主账号，不可见）。
        summary = await member.get("/api/account/summary")
        assert summary.status_code == 200, summary.text
        assert summary.json()["accountName"] == MEMBER_ACCOUNT
        assert (await member.get("/api/positions")).status_code == 200
        assert (await member.get("/api/simulation/orders")).status_code == 200
        assert (await member.get("/api/app/activity")).status_code == 200
        # 会话与策略草稿落到成员自己的账户，而不是默认账户。
        session = await member.post("/api/sessions?chat=true", json={"mode": "local", "agentId": "native"})
        assert session.status_code == 201, session.text
        session_id = session.json()["sessionId"]
        detail = await member.get(f"/api/sessions/{session_id}")
        assert detail.json()["tradingAccountId"] == MEMBER_ACCOUNT
        saved = await member.post("/api/strategy-plans", json={"plan": plan().model_dump()})
        assert saved.status_code == 201, saved.text
        draft = application.state.store.get_strategy_draft(
            saved.json()["strategyId"], user_id=MEMBER)
        assert draft["tradingAccountId"] == MEMBER_ACCOUNT

        # 显式点名主账号（默认账户）：账户维度的读与写全部拒绝，不回落。
        # /api/strategies 是纯 user 过滤的列表（不带账户维度），不在其中；
        # strategy-plans 换一份不同 digest 的计划体，避开同定义幂等短路。
        for path, method in (
            ("/api/account/summary", "get"), ("/api/positions", "get"),
            ("/api/simulation/orders", "get"), ("/api/app/activity", "get"),
            ("/api/sessions?chat=true", "post"),
            ("/api/strategy-plans", "post"),
        ):
            if "sessions" in path:
                body = {"mode": "local", "agentId": "native"}
            elif "strategy-plans" in path:
                headed_plan = plan().model_dump()
                headed_plan["name"] = "点名主账号的策略"
                body = {"plan": headed_plan}
            else:
                body = None
            response = await member.request(
                method, path, headers={"x-ai-trader-account": DEFAULT_ACCOUNT}, json=body)
            assert response.status_code == 403, (path, response.status_code, response.text)
            assert response.json()["detail"] == "account_not_granted", path

        # owner 同一批表面不受影响：不带账户 id 解析默认账户。
        owner_summary = await owner.get("/api/account/summary")
        assert owner_summary.status_code == 200
        assert owner_summary.json()["accountName"] != MEMBER_ACCOUNT
        owner_session = await owner.post("/api/sessions?chat=true",
                                         json={"mode": "local", "agentId": "native"})
        assert owner_session.status_code == 201
        owner_detail = await owner.get(f"/api/sessions/{owner_session.json()['sessionId']}")
        assert owner_detail.json()["tradingAccountId"] == DEFAULT_ACCOUNT
    finally:
        await owner.aclose()
        await member.aclose()
        application.state.store.close()


async def test_feed_refuses_a_session_that_lost_its_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c-事件流) 失去授权的会话：feed 报 account_not_granted，不推默认账户数据。"""
    application, _ = tqkq_app(tmp_path, monkeypatch, "iso-feed.sqlite3")
    try:
        member = await member_client(application)
        session = await member.post("/api/sessions?chat=true",
                                    json={"mode": "local", "agentId": "native"})
        assert session.status_code == 201
        session_id = session.json()["sessionId"]
        # 模拟历史中毒会话：把成员会话直接改绑到默认账户（生产中来自被删的
        # 自动授权期），SSE 下一拍必须拒绝，而不是把主账号持仓推给成员。
        with application.state.store.lock, application.state.store.connection:
            application.state.store.connection.execute(
                "UPDATE sessions SET trading_account_id = ? WHERE id = ?",
                (DEFAULT_ACCOUNT, session_id),
            )
        member_cookies = dict(member.cookies)
        await member.aclose()
        saved_logging = {
            name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                   list(logging.getLogger(name).handlers))
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
        }
        try:
            async with serving(application) as base, httpx.AsyncClient(
                base_url=base, headers={"Origin": ORIGIN}, cookies=member_cookies,
                trust_env=False,
            ) as stream_client:
                reader = Reader(stream_client, session_id)
                task = asyncio.create_task(reader.run())
                try:
                    await asyncio.wait_for(reader.opened.wait(), 2)
                    await asyncio.sleep(0.1)
                    await application.state.runtime_feed.tick()
                    await asyncio.sleep(0.2)
                    statuses = [item for item in reader.messages if item.get("name") == "feed.status"]
                    assert statuses, reader.messages
                    reasons = {item["payload"].get("reason") for item in statuses}
                    assert "account_not_granted" in reasons, reasons
                    assert not [item for item in reader.messages if item.get("name") == "feed.account"], \
                        reader.messages
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            for name, (level, propagate, handlers) in saved_logging.items():
                logger = logging.getLogger(name)
                logger.setLevel(level)
                logger.propagate = propagate
                logger.handlers = handlers
    finally:
        application.state.store.close()


async def test_unbound_member_gets_account_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) 无绑定账户的成员：账户表面得到 tqkq_account_required 业务状态。"""
    application, _ = tqkq_app(tmp_path, monkeypatch, "iso-d.sqlite3")
    try:
        # 注册不再授默认账户：直建一个 role=user 云用户并登录。
        application.state.store.create_cloud_user(
            username="orphan", password_hash=hash_owner_password("orphan-password-1"),
            invited_by="invite-code",
        )
        client = client_for(application)
        try:
            login = await client.post("/api/auth/owner",
                                      json={"username": "orphan", "password": "orphan-password-1"})
            assert login.status_code == 200, login.text
            assert login.json()["role"] == "user"
            for path in ("/api/account/summary", "/api/positions", "/api/app/activity",
                         "/api/simulation/orders"):
                response = await client.get(path)
                assert response.status_code == 403, (path, response.text)
                assert response.json()["detail"] == "tqkq_account_required", path
            session = await client.post("/api/sessions?chat=true",
                                        json={"mode": "local", "agentId": "native"})
            assert session.status_code == 403
            assert session.json()["detail"] == "tqkq_account_required"
            # 用户自有的策略列表照常（按 user 过滤，与账户无关）。
            strategies = await client.get("/api/strategies")
            assert strategies.status_code == 200
        finally:
            await client.aclose()
    finally:
        application.state.store.close()
