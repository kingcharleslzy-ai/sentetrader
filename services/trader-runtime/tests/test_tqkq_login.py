"""M1b 多人接入：快期账号直接登录、首次邀请码、主账号防重绑与频率限制。

OwnerAuth 的会话发放、防重绑、频率限制与回落限制全部走真实代码；池的
HTTP 控制面（verify/start/stop）复用 test_account_pool 的假客户端替身。
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

import trader_runtime.account_pool as account_pool_module
from test_account_pool import POOL_URL, SIDECAR_TOKEN, FakePoolClient, NamedFakeProvider, bind_account_id
from test_owner_auth import ORIGIN, PASSWORD, PASSWORD_HASH, SESSION_SECRET
from test_paper_accounts import (
    ENGINE_URL,
    FakePaperClient,
    FakePaperEngine,
    _bind_engine,
    _bind_poller,
    close_paper_app,
)
from trader_runtime.auth import TQKQ_REGISTER_URL
from trader_runtime.main import create_app
from trader_runtime.wiring.accounts import resolve_account

KQ_USER = "kq-m1b-user"
KQ_PASSWORD = "kq-m1b-password"
DEFAULT_ACCOUNT = "tqkq-sim:default"


def scoped_owner_id(username: str) -> str:
    """The session-subject id a cloud username resolves to (same as login)."""
    return f"owner-{hashlib.sha256(username.encode()).hexdigest()}"


def tqkq_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str, *,
    static_file: Path | None = None, invite: str = "1", pool: bool = True,
):
    """owner-password 模式 + 池的测试应用：真实凭据盒与登录语义，假池客户端。"""
    key_file = tmp_path / f"{database}.key"
    key_file.write_bytes(base64.b64encode(os.urandom(32)))
    monkeypatch.setenv("AI_TRADER_CREDENTIAL_KEY_FILE", str(key_file))
    monkeypatch.setenv("AI_TRADER_TQKQ_LOGIN_INVITE", invite)
    if pool:
        monkeypatch.setenv("AI_TRADER_TQKQ_POOL_URL", POOL_URL)
        monkeypatch.setenv("AI_TRADER_READONLY_SIDECAR_TOKEN", SIDECAR_TOKEN)
    else:
        monkeypatch.delenv("AI_TRADER_TQKQ_POOL_URL", raising=False)
    monkeypatch.delenv("AI_TRADER_STATIC_TQKQ_USERNAME", raising=False)
    if static_file is not None:
        monkeypatch.setenv("AI_TRADER_STATIC_TQKQ_USERNAME_FILE", str(static_file))
    else:
        monkeypatch.delenv("AI_TRADER_STATIC_TQKQ_USERNAME_FILE", raising=False)
    application = create_app(
        tmp_path / database,
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
    )
    fake_client = FakePoolClient()
    if application.state.account_pool is not None:
        application.state.account_pool.client = fake_client
    real_build = account_pool_module.build_account_services

    def recording_build(spec, **kwargs):
        # 本文件有同一 monkeypatch 建两个应用（跨部署升级）的用例：这层包装会
        # 叠两层，剥掉里层已经给的 injected 再统一换成假 provider。
        kwargs.pop("injected", None)
        return real_build(spec, injected=NamedFakeProvider(spec.id), **kwargs)

    monkeypatch.setattr(account_pool_module, "build_account_services", recording_build)
    return application, fake_client


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://runtime.example.test",
        headers={"Origin": ORIGIN},
    )


async def tqkq_login(client: httpx.AsyncClient, *, username=KQ_USER, password=KQ_PASSWORD, invite_code=""):
    return await client.post(
        "/api/auth/tqkq", json={"username": username, "password": password, "inviteCode": invite_code})


async def owner_minted_invite(application) -> str:
    """The owner mints a fresh invite code on its own client."""
    async with client_for(application) as owner:
        login = await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})
        assert login.status_code == 200
        minted = await owner.post("/api/auth/owner/invite-codes")
        assert minted.status_code == 200
        return minted.json()["inviteCode"]


async def login_first_user(client: httpx.AsyncClient, application) -> str:
    code = await owner_minted_invite(application)
    response = await tqkq_login(client, invite_code=code)
    assert response.status_code == 200
    return code


async def test_first_login_consumes_invite_binds_and_issues_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client = tqkq_app(tmp_path, monkeypatch, "invite.sqlite3")
    try:
        store = application.state.store
        user_id = scoped_owner_id(KQ_USER)
        account_id = bind_account_id(KQ_USER)
        async with client_for(application) as client:
            # 无邀请码（或缺席）不能创建用户，验证口令也不发生。
            missing = await tqkq_login(client)
            assert missing.status_code == 403
            assert missing.json()["detail"] == "invite_code_invalid"
            assert fake_client.verifies == []
            assert store.get_cloud_user(KQ_USER) is None

            code = await owner_minted_invite(application)
            wrong = await tqkq_login(client, invite_code="not-the-code")
            assert wrong.status_code == 403 and wrong.json()["detail"] == "invite_code_invalid"
            assert store.get_cloud_user(KQ_USER) is None

            logged_in = await tqkq_login(client, invite_code=code)
            # 首次登录如实标出：路由据此把新用户的默认账户放到本站模拟（2026-10-04）。
            assert logged_in.json()["firstLogin"] is True
            assert logged_in.status_code == 200
            payload = logged_in.json()
            assert payload["status"] == "logged_in" and payload["role"] == "user"
            assert payload["username"] == KQ_USER and payload["userId"] == user_id
            assert payload["accountId"] == account_id and payload["assembly"] == "connected"

            # 用户记录：随机不可用口令哈希、tqkq-login 来源、只授予自己的池账户。
            record = store.get_cloud_user(KQ_USER)
            assert record is not None and record["invitedBy"] == "tqkq-login"
            assert [(g["account_id"], g["role"]) for g in store.list_account_grants(user_id=user_id)] == [
                (account_id, "owner")]
            # 口令只以密文托管。
            credentials = store.load_tqkq_credentials(user_id=user_id)
            assert credentials is not None and KQ_PASSWORD not in repr(credentials)

            # 会话 cookie 生效：目录里只有自己的池账户（connected）。
            directory = (await client.get("/api/accounts")).json()
            assert [item["id"] for item in directory["accounts"]] == [account_id]
            assert directory["current"] == account_id
            pool_item = directory["accounts"][0]
            assert pool_item["providerKind"] == "tqkq-pool" and pool_item["poolStatus"] == "connected"
            assert pool_item["executionOwner"] is True

            # 邀请码一次性：同一个码换一个新账号不再可用。
            reused = await tqkq_login(client, username="kq-other-user", invite_code=code)
            assert reused.status_code == 403 and reused.json()["detail"] == "invite_code_invalid"
            assert store.get_cloud_user("kq-other-user") is None
    finally:
        application.state.store.close()


async def test_first_login_with_an_unlimited_invite_admits_a_second_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不限次数邀请码：快期首次登录路径上两个不同账号都能用同一个码。"""
    application, _fake_client = tqkq_app(tmp_path, monkeypatch, "invite-unlimited.sqlite3")
    try:
        store = application.state.store
        async with client_for(application) as owner:
            login = await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})
            assert login.status_code == 200
            minted = await owner.post("/api/auth/owner/invite-codes",
                                      json={"code": "open-door", "uses": "unlimited"})
            assert minted.status_code == 200
        async with client_for(application) as client:
            first = await tqkq_login(client, invite_code="open-door")
            assert first.status_code == 200
            second = await tqkq_login(client, username="kq-unlimited-mate", invite_code="open-door")
            assert second.status_code == 200
            assert store.get_cloud_user("kq-unlimited-mate") is not None
            codes = store.list_invite_codes()
            row = next(item for item in codes if item["code"] == "open-door")
            assert row["useCount"] == 2 and row["status"] == "active"
            assert sorted(use["username"] for use in row["usedBy"]) == [
                KQ_USER, "kq-unlimited-mate"]
    finally:
        application.state.store.close()


async def test_relogin_without_invite_reseals_changed_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client = tqkq_app(tmp_path, monkeypatch, "relogin.sqlite3")
    try:
        store = application.state.store
        user_id = scoped_owner_id(KQ_USER)
        async with client_for(application) as client:
            await login_first_user(client, application)
            first = store.load_tqkq_credentials(user_id=user_id)
            assert first is not None

            # 改了快期密码后再登录：不再需要邀请码，新口令重新密封。
            relogin = await tqkq_login(client, password="kq-m1b-rotated")
            assert relogin.status_code == 200
            assert relogin.json()["firstLogin"] is False  # 老用户：已选的默认账户不动
            assert relogin.json()["assembly"] == "connected"
            assert (KQ_USER, "kq-m1b-rotated") in fake_client.verifies
            second = store.load_tqkq_credentials(user_id=user_id)
            assert second is not None
            assert second["ciphertext"] != first["ciphertext"]
            assert second["account_id"] == first["account_id"]
    finally:
        application.state.store.close()


async def test_static_main_account_name_is_refused_with_owner_login_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    static_file = tmp_path / "static-username.txt"
    static_file.write_text("  Owner-Static \n", encoding="utf-8")
    application, fake_client = tqkq_app(
        tmp_path, monkeypatch, "static.sqlite3", static_file=static_file)
    try:
        async with client_for(application) as client:
            for username in ("owner-static", "OWNER-STATIC"):
                refused = await tqkq_login(client, username=username)
                assert refused.status_code == 403
                assert refused.json()["detail"] == {"code": "use_owner_login"}
            # 与 Runtime owner 同名（不分大小写）：按管理员密码校验，不走池。
            wrong = await tqkq_login(client, username="Charles")
            assert wrong.status_code == 401 and wrong.json()["detail"] == "invalid_owner_credentials"
            # 管理员在同一个登录框里输入自己的账号密码，直接以管理员登录。
            owner = await tqkq_login(client, username="charles", password=PASSWORD)
            assert owner.status_code == 200
            assert owner.json()["status"] == "authenticated" and owner.json()["role"] == "owner"
            assert "bindings" in owner.json()
            # 以上都没有走池验证。
            assert fake_client.verifies == []
    finally:
        application.state.store.close()


async def test_failed_tqkq_logins_are_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client = tqkq_app(tmp_path, monkeypatch, "ratelimit.sqlite3", invite="0")
    try:
        async def refuse_verify(username, password):
            fake_client.verifies.append((username, password))
            return {"ok": False, "reason": "auth_failed"}

        application.state.account_pool.client.verify = refuse_verify  # type: ignore[method-assign]
        async with client_for(application) as client:
            for _ in range(5):
                failed = await tqkq_login(client)
                assert failed.status_code == 401
                assert failed.json()["detail"] == {
                    "code": "tqkq_login_failed", "reason": "auth_failed", "canRegister": False}
            # 第 6 次按用户名锁住：即便这次口令正确也先吃 429。
            locked = await tqkq_login(client)
            assert locked.status_code == 429
    finally:
        application.state.store.close()


async def test_without_pool_tqkq_login_is_unavailable_and_status_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client = tqkq_app(tmp_path, monkeypatch, "nopool.sqlite3", pool=False)
    try:
        async with client_for(application) as client:
            anonymous = (await client.get("/api/auth/owner/status")).json()
            assert anonymous["tqkqLogin"] == {
                "enabled": False,
                "inviteRequired": True,
                "registerUrl": TQKQ_REGISTER_URL,
            }
            # 一个登录框（2026-10-04）：没有快期池时，未知账号名按“新本站账号”回答，由客户端问过再注册。
            unknown = await tqkq_login(client)
            assert unknown.status_code == 404
            assert unknown.json()["detail"] == {"code": "account_not_found", "canRegister": False}
    finally:
        application.state.store.close()


async def test_owner_status_reports_tqkq_login_for_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _ = tqkq_app(tmp_path, monkeypatch, "status.sqlite3")
    try:
        async with client_for(application) as client:
            anonymous = (await client.get("/api/auth/owner/status")).json()
            assert anonymous["authenticated"] is False
            assert anonymous["tqkqLogin"]["enabled"] is True
            assert anonymous["tqkqLogin"]["inviteRequired"] is True
            assert anonymous["tqkqLogin"]["registerUrl"] == TQKQ_REGISTER_URL
    finally:
        application.state.store.close()


async def test_runtime_status_stays_readable_while_assembly_is_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, fake_client = tqkq_app(tmp_path, monkeypatch, "queued.sqlite3")
    try:
        fake_client.capacity = True
        async with client_for(application) as client:
            await login_first_user(client, application)
            logged_in = await tqkq_login(client)
            assert logged_in.status_code == 200
            assert logged_in.json()["assembly"] == "queued"

            status = await client.get("/api/runtime/status")
            assert status.status_code == 200
            payload = status.json()
            assert payload["account"]["id"] == bind_account_id(KQ_USER)
            assert payload["account"]["poolStatus"] == "queued"
            assert payload["simulationTrading"] is False

            # 容量恢复后同一会话直接读到账户。
            fake_client.capacity = False
            recovered = await client.get("/api/account/summary")
            assert recovered.status_code == 200
    finally:
        application.state.store.close()


def tqkq_paper_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: str):
    """tqkq_app 再叠上本站模拟盘（假引擎）：“新账号只开 paper” 的部署形态。"""
    monkeypatch.setenv("AI_TRADER_PAPER_ENGINE_URL", ENGINE_URL)
    application, fake_client = tqkq_app(tmp_path, monkeypatch, database)
    engine = FakePaperEngine()
    _bind_engine(monkeypatch, engine)
    paper = application.state.paper_accounts
    assert paper is not None
    paper.client = FakePaperClient(engine)
    _bind_poller(paper, engine)
    return application, fake_client, engine


async def test_first_login_on_paper_deployment_opens_only_the_site_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置了本站模拟盘时，快期首次登录（owner 2026-10-05）：口令仍经池验证，
    但不再自动建 tqkq 账户、不装配 sidecar——目录里只有本站模拟且为默认；
    同一快期账号再登录仍走同一条路。"""
    application, fake_client, _engine = tqkq_paper_app(tmp_path, monkeypatch, "paper-first.sqlite3")
    try:
        store = application.state.store
        user_id = scoped_owner_id(KQ_USER)
        async with client_for(application) as client:
            code = await owner_minted_invite(application)
            logged_in = await tqkq_login(client, invite_code=code)
            assert logged_in.status_code == 200
            payload = logged_in.json()
            assert payload["firstLogin"] is True
            assert payload["accountId"].startswith("paper:")
            assert payload["assembly"] == "connected"

            directory = (await client.get("/api/accounts")).json()
            assert [item["id"] for item in directory["accounts"]] == [payload["accountId"]]
            assert directory["current"] == payload["accountId"]
            assert directory["accounts"][0]["providerKind"] == "paper"

            # 登录身份靠池验证（verifies 有记录），但不建 tqkq 账户：无行、无授予、
            # 无托管凭据、无 sidecar 装配。
            assert (KQ_USER, KQ_PASSWORD) in fake_client.verifies
            assert store.get_trading_account(bind_account_id(KQ_USER)) is None
            assert all(grant["account_id"] != bind_account_id(KQ_USER)
                       for grant in store.list_account_grants(user_id=user_id))
            assert store.load_tqkq_credentials(user_id=user_id) is None
            assert fake_client.starts == [] and fake_client.spawns == []

            # 同一快期账号第二次登录：仍能登录，仍只有本站模拟。
            relogin = await tqkq_login(client)
            assert relogin.status_code == 200
            assert relogin.json()["firstLogin"] is False
            assert relogin.json()["accountId"] == payload["accountId"]
            directory = (await client.get("/api/accounts")).json()
            assert [item["id"] for item in directory["accounts"]] == [payload["accountId"]]
            assert store.get_trading_account(bind_account_id(KQ_USER)) is None
            assert fake_client.starts == []
    finally:
        await close_paper_app(application)


async def test_old_user_with_a_tqkq_account_lands_on_paper_without_a_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快期模拟停用交易 (#1205)：已有快期账户的老用户在配置了本站模拟盘的部署里再登录——口令照样经池
    核验身份，但不再密封口令、不重新绑定或启用（已停用的）快期账户、不装配；落在自己的本站模拟上
    并把它设为默认。"""
    monkeypatch.delenv("AI_TRADER_PAPER_ENGINE_URL", raising=False)
    database = "paper-old.sqlite3"
    application, _ = tqkq_app(tmp_path, monkeypatch, database)
    store = application.state.store
    user_id = scoped_owner_id(KQ_USER)
    account_id = bind_account_id(KQ_USER)
    try:
        async with client_for(application) as client:
            await login_first_user(client, application)
        assert store.get_trading_account(account_id) is not None
        first_seal = store.load_tqkq_credentials(user_id=user_id)
        assert first_seal is not None
    finally:
        store.close()

    # 同一数据库换到配置了本站模拟盘的部署（假引擎）；快期账户已被停用（启动时的整套解绑）。
    monkeypatch.setenv("AI_TRADER_PAPER_ENGINE_URL", ENGINE_URL)
    engine = FakePaperEngine()
    _bind_engine(monkeypatch, engine)
    upgraded, fake_client = tqkq_app(tmp_path, monkeypatch, database)
    paper = upgraded.state.paper_accounts
    assert paper is not None
    paper.client = FakePaperClient(engine)
    _bind_poller(paper, engine)
    try:
        upgraded_store = upgraded.state.store
        upgraded_store.disable_trading_account(account_id)
        async with client_for(upgraded) as client:
            relogin = await tqkq_login(client, password="kq-m1b-rotated")
            assert relogin.status_code == 200
            payload = relogin.json()
            assert payload["firstLogin"] is False
            paper_id = str(upgraded_store.find_user_paper_account(user_id=user_id)["id"])
            assert payload["accountId"] == paper_id and payload["assembly"] == "connected"
            assert (KQ_USER, "kq-m1b-rotated") in fake_client.verifies
            assert fake_client.starts == [] and fake_client.spawns == []
            assert upgraded_store.get_trading_account(account_id)["status"] == "disabled"
            assert upgraded_store.load_tqkq_credentials(user_id=user_id)["ciphertext"] == first_seal["ciphertext"]
            assert upgraded_store.get_user_default_account(user_id=user_id) == paper_id
            assert (await client.get("/api/accounts")).json()["current"] == paper_id
    finally:
        await close_paper_app(upgraded)


def test_resolve_account_fallback_only_for_owner_or_local_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _ = tqkq_app(tmp_path, monkeypatch, "fallback.sqlite3")
    try:
        registry = application.state.accounts
        store = application.state.store
        # 未绑定的 role=user（无任何授予）必须 403，不得停放回默认账户。
        with pytest.raises(HTTPException) as refused:
            resolve_account(
                registry, store, request=None,
                user_id=scoped_owner_id(KQ_USER), idp_fallback=False)
        assert refused.value.status_code == 403
        assert refused.value.detail == "tqkq_account_required"
        # owner（或 local-token 模式）无授予仍回落默认账户 —— 原行为不变。
        owner = resolve_account(
            registry, store, request=None,
            user_id=scoped_owner_id("charles"), idp_fallback=True)
        assert owner.account.account_id == DEFAULT_ACCOUNT
    finally:
        application.state.store.close()
