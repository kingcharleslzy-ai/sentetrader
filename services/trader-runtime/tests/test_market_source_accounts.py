"""行情专用快期账号（2026-10-12）：主人在后台「行情源」输入快期账号与口令加成第 N 个行情源。

口令经池校验（与快期登录同一校验）、按绑定的同一路径加密托管，账户挂在专用系统归属
`market-source:<账户号>` 下：不进任何人的账户目录、不能被会话使用，只给公共行情接力与
本站模拟撮合引擎的行情接力用；追加到清单末尾立即生效。移除＝出清单、停用、删授权与口令、
停子进程。口令不出现在任何日志、响应与错误里；能力目录不列这两条（人工操作）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from test_account_pool import bind_account_id
from test_paper_accounts import close_paper_app
from test_tqkq_login import (
    DEFAULT_ACCOUNT,
    PASSWORD,
    client_for,
    owner_minted_invite,
    tqkq_login,
    tqkq_paper_app,
)
from trader_runtime.routes.account import store_tqkq_pool_account
from trader_runtime.store.trading_accounts import market_source_owner_id

NEW_USER = "kq-market-4th"
SECRET = "kq-market-secret-7f3e91"
MEMBER_A, MEMBER_B = "kq-member-a", "kq-member-b"
ROUTE = "/api/admin/market-sources/tqkq"


@pytest.fixture
async def market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A paper deployment with the pool and three sources: two members' pool accounts
    (each with its own sealed login) and the resident main account."""
    first, second = bind_account_id(MEMBER_A), bind_account_id(MEMBER_B)
    monkeypatch.setenv("AI_TRADER_SHARED_MARKET_ACCOUNT_IDS", f"{first},{DEFAULT_ACCOUNT},{second}")
    application, fake_client, _engine = tqkq_paper_app(tmp_path, monkeypatch, "market-sources.sqlite3")
    store, pool = application.state.store, application.state.account_pool
    for username, owner in ((MEMBER_A, "owner-member-a"), (MEMBER_B, "owner-member-b")):
        store_tqkq_pool_account(store, pool, owner_user_id=owner, username=username, password=f"{username}-pw")
    owner = client_for(application)
    assert (await owner.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
    try:
        yield application, fake_client, owner, [first, DEFAULT_ACCOUNT, second]
    finally:
        await owner.aclose()
        await close_paper_app(application)


def engine_ids(application) -> list[str]:
    return [item["accountId"] for item in application.state.paper_accounts.market_sync.payload()]


async def test_adding_verifies_seals_appends_and_both_relays_see_four_sources(market, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    application, fake_client, owner, sources = market
    store = application.state.store
    added = await owner.post(ROUTE, json={"username": NEW_USER, "password": SECRET})
    assert added.status_code == 200, added.text
    account_id = bind_account_id(NEW_USER)
    body = added.json()
    assert body["accountId"] == account_id
    assert [item["accountId"] for item in body["items"]] == [*sources, account_id]
    assert body["items"][-1]["marketOnly"] is True and body["items"][-1]["displayName"] == NEW_USER
    assert not any(item["marketOnly"] for item in body["items"][:-1])
    # 同一条池校验（快期登录的口令校验），不装配交易服务、不起子进程。
    assert fake_client.verifies[-1] == (NEW_USER, SECRET)
    assert fake_client.starts == []
    # 账户挂在专用系统归属下，口令只以密文存在、能打开回原文。
    owner_id = market_source_owner_id(account_id)
    record = store.get_trading_account(account_id)
    assert record["execution_owner_user_id"] == owner_id
    assert record["provider_kind"] == "tqkq-pool" and record["status"] == "active"
    assert store.list_account_grantees(account_id=account_id) == [owner_id]
    username, opener = application.state.account_pool.market_credentials(account_id)
    assert (username, opener()) == (NEW_USER, SECRET)
    # 立即生效：公共行情接力与模拟引擎的行情接力都看到 4 个源，新号在末尾。
    assert application.state.shared_market.ordered_sources() == [*sources, account_id]
    # (The test app's main account is no TqKq sidecar, so the engine list carries the pool ones.)
    payload = application.state.paper_accounts.market_sync.payload()
    assert [item["accountId"] for item in payload] == [sources[0], sources[2], account_id]
    assert payload[-1] == {"accountId": account_id, "username": NEW_USER, "password": SECRET}
    listed = (await owner.get("/api/admin/market-sources")).json()
    assert [item["accountId"] for item in listed["items"]] == [*sources, account_id]
    # 后台“交易账户”里如实标“行情专用”，不显示内部编号。
    accounts = (await owner.get("/api/admin/accounts")).json()["items"]
    row = next(item for item in accounts if item["id"] == account_id)
    assert row["executionOwner"] == "行情专用"
    assert SECRET not in caplog.text and SECRET not in added.text and SECRET not in str(listed)


async def test_the_same_tqkq_login_still_logs_in_to_the_site_simulation_only(market) -> None:
    """同一快期账号的人用登录框登录：照常只开本站模拟，看不到也用不了那条行情账户。"""
    application, _fake_client, owner, _sources = market
    assert (await owner.post(ROUTE, json={"username": NEW_USER, "password": SECRET})).status_code == 200
    account_id = bind_account_id(NEW_USER)
    async with client_for(application) as person:
        code = await owner_minted_invite(application)
        logged_in = await tqkq_login(person, username=NEW_USER, password=SECRET, invite_code=code)
        assert logged_in.status_code == 200, logged_in.text
        assert logged_in.json()["accountId"].startswith("paper:")
        directory = (await person.get("/api/accounts")).json()
        assert account_id not in [item["id"] for item in directory["accounts"]]
        refused = await person.get("/api/account/summary", headers={"X-AI-Trader-Account": account_id})
        assert refused.status_code == 403
    # 行情账户原样：仍归系统、仍在清单里。
    record = application.state.store.get_trading_account(account_id)
    assert record["execution_owner_user_id"] == market_source_owner_id(account_id)
    assert account_id in application.state.shared_market.ordered_sources()


async def test_a_wrong_password_leaves_no_trace(market, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    application, fake_client, owner, sources = market

    async def refuse(username: str, password: str) -> dict[str, object]:
        fake_client.verifies.append((username, password))
        return {"ok": False, "reason": "auth_failed"}

    fake_client.verify = refuse
    refused = await owner.post(ROUTE, json={"username": NEW_USER, "password": SECRET})
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "tqkq_bind_failed", "reason": "auth_failed"}
    account_id = bind_account_id(NEW_USER)
    store = application.state.store
    assert store.get_trading_account(account_id) is None
    assert store.load_tqkq_credentials(user_id=market_source_owner_id(account_id)) is None
    assert store.list_market_sources() == sources
    assert SECRET not in refused.text and SECRET not in caplog.text


async def test_duplicates_members_accounts_and_a_full_pool_are_refused_before_verifying(market) -> None:
    application, fake_client, owner, sources = market
    assert (await owner.post(ROUTE, json={"username": NEW_USER, "password": SECRET})).status_code == 200
    verified = len(fake_client.verifies)
    again = await owner.post(ROUTE, json={"username": NEW_USER, "password": SECRET})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "market_source_duplicate"
    # 某位成员自己的交易账户：走下方“已有账户”选择，不用口令再托管一份。
    application.state.store.set_market_sources(sources)
    application.state.shared_market.reload_sources()
    taken = await owner.post(ROUTE, json={"username": MEMBER_A, "password": "whatever"})
    assert taken.json()["detail"]["code"] == "market_source_duplicate"
    application.state.store.set_market_sources([DEFAULT_ACCOUNT])
    application.state.shared_market.reload_sources()
    in_use = await owner.post(ROUTE, json={"username": MEMBER_A, "password": "whatever"})
    assert in_use.status_code == 409 and in_use.json()["detail"]["code"] == "market_source_account_in_use"
    # 号池上限不够：明确报上限与占用，不校验、不写入。
    application.state.store.set_market_sources(sources)
    application.state.shared_market.reload_sources()
    real_entries = fake_client.entries

    async def full() -> dict[str, object]:
        listing = await real_entries()
        return {**listing, "capacity": {"max": 2, "running": 0}}

    fake_client.entries = full
    crowded = await owner.post(ROUTE, json={"username": "kq-market-5th", "password": SECRET})
    assert crowded.status_code == 409
    assert crowded.json()["detail"] == {"code": "market_source_pool_full", "max": 2, "running": 0, "poolSources": 2}
    assert len(fake_client.verifies) == verified
    assert application.state.store.get_trading_account(bind_account_id("kq-market-5th")) is None
    # 格式不对的输入只回代码，不回显输入。
    bad = await owner.post(ROUTE, json={"username": "kq", "password": SECRET})
    assert bad.status_code == 422 and SECRET not in bad.text
    long_secret = "x" * 401
    too_long = await owner.post(ROUTE, json={"username": NEW_USER + "x", "password": long_secret})
    assert too_long.status_code == 422 and long_secret not in too_long.text


async def test_removing_takes_it_out_disables_it_and_stops_its_child(market) -> None:
    application, fake_client, owner, sources = market
    store, pool = application.state.store, application.state.account_pool
    assert (await owner.post(ROUTE, json={"username": NEW_USER, "password": SECRET})).status_code == 200
    account_id = bind_account_id(NEW_USER)
    port = await pool.market_source(account_id)  # the relay started its child
    assert fake_client.running[account_id] == port
    # 行情专用（10-13）：它的子进程只开数据会话（marketOnly），成员账号当行情源时照常起交易子进程。
    assert fake_client.market_only_starts[-1] is True
    await pool.market_source(sources[0])
    assert fake_client.market_only_starts[-1] is False
    removed = await owner.delete(f"/api/admin/market-sources/{account_id}")
    assert removed.status_code == 200, removed.text
    assert [item["accountId"] for item in removed.json()["items"]] == sources
    assert application.state.shared_market.ordered_sources() == sources
    assert engine_ids(application) == [sources[0], sources[2]]
    assert store.get_trading_account(account_id)["status"] == "disabled"
    assert store.list_account_grantees(account_id=account_id) == []
    assert store.load_tqkq_credentials(user_id=market_source_owner_id(account_id)) is None
    assert account_id in fake_client.stops and account_id not in fake_client.running
    # 只移除行情专用账号；成员的账户走“停用”（PUT 清单），不删它的绑定。
    member = await owner.delete(f"/api/admin/market-sources/{sources[0]}")
    assert member.status_code == 409 and member.json()["detail"]["code"] == "market_source_not_market_only"
    assert (await owner.delete("/api/admin/market-sources/tqkq-sim:missing")).status_code == 404
    # 移除后可以再加回来：同一条账户重新启用、重新托管口令。
    again = await owner.post(ROUTE, json={"username": NEW_USER, "password": SECRET})
    assert again.status_code == 200 and store.get_trading_account(account_id)["status"] == "active"


async def test_only_the_owner_reaches_them_and_the_agent_catalog_leaves_them_out(market) -> None:
    application, _fake_client, owner, _sources = market
    async with client_for(application) as person:
        code = await owner_minted_invite(application)
        assert (await tqkq_login(person, username="kq-plain-member", invite_code=code)).status_code == 200
        # 与 PUT 清单同口径：admin-console 技能网关对非主账号 403。
        assert (await person.post(ROUTE, json={"username": NEW_USER, "password": SECRET})).status_code == 403
        assert (await person.delete(f"/api/admin/market-sources/{DEFAULT_ACCOUNT}")).status_code == 403
    async with client_for(application) as anonymous:
        assert (await anonymous.post(ROUTE, json={"username": NEW_USER, "password": SECRET})).status_code in {401, 404}
    assert application.state.store.get_trading_account(bind_account_id(NEW_USER)) is None
    manifest = (await owner.get("/api/agent/capabilities")).json()
    operations = {item["operation"] for item in manifest["operations"]}
    assert {"GET /api/admin/market-sources", "PUT /api/admin/market-sources"} <= operations
    assert not ({"POST /api/admin/market-sources/tqkq",
                 "DELETE /api/admin/market-sources/{account_id}"} & operations)


async def test_a_reorder_cannot_drop_a_market_only_account(market) -> None:
    """整份 PUT 只能调序：漏掉行情专用账号会 409，必须走“移除”（会停用并删口令）。"""
    application, _fake_client, owner, sources = market
    assert (await owner.post(ROUTE, json={"username": NEW_USER, "password": SECRET})).status_code == 200
    account_id = bind_account_id(NEW_USER)
    members = [sources[0], sources[2]]  # (the test app's main account is no TqKq sidecar: PUT refuses it)
    dropped = await owner.put("/api/admin/market-sources", json={"accountIds": members})
    assert dropped.status_code == 409
    assert dropped.json()["detail"] == {"code": "market_source_remove_required", "accountId": account_id}
    reordered = await owner.put("/api/admin/market-sources", json={"accountIds": [account_id, *members]})
    assert reordered.status_code == 200, reordered.text
    assert application.state.store.list_market_sources() == [account_id, *members]
    # 成员账户照旧可以用 PUT 停用（从清单去掉）。
    assert (await owner.put("/api/admin/market-sources", json={"accountIds": [account_id]})).status_code == 200
