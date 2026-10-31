"""本站模拟撮合引擎的行情账号接力（2026-10-12）：Runtime 一侧。

租约持有者把公共行情源清单（`market_sources`，同一份顺序）下发给引擎：账户池
账号带 Runtime 账户库里的快期登录，主 sidecar 账号只标 local；停用或缺凭据的
跳过。内容没变、引擎也没重启就不重发；顺序一变、引擎重启（指纹不符）就重发；
备用槽不发。口令不进日志；管理员清单接口显示引擎当前用哪个账号。
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import trader_runtime.paper_accounts as paper_accounts_module
from test_market_source_relay import FIRST, SECOND, relay  # noqa: F401 (fixture)
from trader_runtime.account_pool import PoolStartRefused
from trader_runtime.paper_accounts import PaperEngineClient, PaperEngineError, PaperMarketSourceSync

SECRET_FIRST, SECRET_THIRD = "kq-secret-first-9a7", "kq-secret-third-4c2"
ACCOUNTS = {
    "tqkq-sim:first": {"status": "active", "provider_kind": "tqkq-pool"},
    "tqkq-sim:default": {"status": "active", "provider_kind": "tqkq-sidecar"},
    "tqkq-sim:third": {"status": "active", "provider_kind": "tqkq-pool"},
    "tqkq-sim:paused": {"status": "disabled", "provider_kind": "tqkq-pool"},
    "tqkq-sim:nologin": {"status": "active", "provider_kind": "tqkq-pool"},
    "tqkq-sim:sidecar2": {"status": "active", "provider_kind": "tqkq-sidecar"},
}
LOGINS = {"tqkq-sim:first": ("kq-first", SECRET_FIRST), "tqkq-sim:third": ("kq-third", SECRET_THIRD)}


class FakePool:
    def market_credentials(self, account_id: str):
        if account_id not in LOGINS:
            raise PoolStartRefused("credentials_missing")
        username, password = LOGINS[account_id]
        return username, lambda: password


class FakeEngine:
    """The engine's two routes: health carries the revision of the list it holds."""

    def __init__(self) -> None:
        self.revision = "boot-0"
        self.pushes: list[list[dict[str, Any]]] = []

    async def health(self) -> dict[str, Any]:
        return {"marketSource": {"revision": self.revision, "current": "tqkq-sim:default",
                                 "accounts": []}}

    async def set_market_sources(self, sources: list[dict[str, Any]]) -> dict[str, Any]:
        self.pushes.append(sources)
        self.revision = f"rev-{len(self.pushes)}"
        return {"revision": self.revision}

    def restart(self) -> None:
        self.revision = "boot-1"


def make_sync(order: list[str], engine: FakeEngine, *, leader: list[bool] | None = None,
              seeded: bool = True) -> PaperMarketSourceSync:
    store = SimpleNamespace(get_trading_account=lambda account_id: (
        {"id": account_id, **ACCOUNTS[account_id]} if account_id in ACCOUNTS else None),
        market_sources_seeded=lambda: seeded)
    shared = SimpleNamespace(ordered_sources=lambda: list(order))
    lead = leader if leader is not None else [True]
    return PaperMarketSourceSync(client=lambda: engine, store=store, shared_market=lambda: shared,
                                 account_pool=lambda: FakePool(), may_run=lambda: lead[0],
                                 local_account_id=lambda: "tqkq-sim:default")


async def test_the_engine_gets_the_public_market_order_with_pool_logins(caplog):
    caplog.set_level(logging.INFO, logger=paper_accounts_module.log.name)
    engine = FakeEngine()
    order = ["tqkq-sim:first", "tqkq-sim:paused", "tqkq-sim:default", "tqkq-sim:nologin",
             "tqkq-sim:missing", "tqkq-sim:sidecar2", "tqkq-sim:third"]
    sync = make_sync(order, engine)
    assert await sync.sync_once() is True
    assert engine.pushes == [[
        {"accountId": "tqkq-sim:first", "username": "kq-first", "password": SECRET_FIRST},
        {"accountId": "tqkq-sim:default", "local": True},
        {"accountId": "tqkq-sim:third", "username": "kq-third", "password": SECRET_THIRD},
    ]]
    assert "tqkq-sim:nologin" in caplog.text and "credentials_missing" in caplog.text
    # A secondary static sidecar's login is not mounted in the engine: never sent as local.
    assert "tqkq-sim:sidecar2" in caplog.text and "not_engine_mounted" in caplog.text
    for secret in (SECRET_FIRST, SECRET_THIRD):
        assert secret not in caplog.text


async def test_the_list_is_pushed_again_only_on_a_change_or_an_engine_restart():
    engine = FakeEngine()
    order = ["tqkq-sim:first", "tqkq-sim:default", "tqkq-sim:third"]
    leader = [True]
    sync = make_sync(order, engine, leader=leader)
    assert await sync.sync_once() is True
    assert await sync.sync_once() is False  # same list, engine still holds it
    order[:] = ["tqkq-sim:third", "tqkq-sim:first", "tqkq-sim:default"]  # admin reordered
    assert await sync.sync_once() is True
    assert [item["accountId"] for item in engine.pushes[-1]] == order
    engine.restart()  # the engine lost the list with its memory
    assert await sync.sync_once() is True
    assert await sync.sync_once() is False
    leader[0] = False  # a blue-green standby slot never pushes
    order.reverse()
    assert await sync.sync_once() is False
    assert len(engine.pushes) == 3


async def test_an_engine_without_the_route_is_left_alone():
    class ReplayEngine(FakeEngine):
        async def set_market_sources(self, sources):
            raise PaperEngineError("paper_market_sources_unsupported")

    engine = ReplayEngine()
    sync = make_sync(["tqkq-sim:default"], engine)
    assert await sync.sync_once() is False
    assert sync.last_error == "paper_market_sources_unsupported"
    assert await sync.sync_once() is False  # not retried every tick


async def test_the_client_speaks_the_engine_contract(monkeypatch):
    seen: list[tuple[str, str, Any, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body, request.headers.get("Authorization", "")))
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"marketSource": {"revision": "r1"}})
        return httpx.Response(200, json={"revision": "r2"})

    real_client = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(paper_accounts_module.httpx, "AsyncClient", patched)
    client = PaperEngineClient("http://paper-engine.test", "t" * 40)
    assert (await client.health())["marketSource"]["revision"] == "r1"
    sources = [{"accountId": "tqkq-sim:default", "local": True}]
    assert (await client.set_market_sources(sources))["revision"] == "r2"
    assert seen[1] == ("POST", "/v1/market-sources", {"sources": sources}, "Bearer " + "t" * 40)


async def test_the_owner_sees_which_account_the_engine_uses_and_a_reorder_reaches_it(relay):  # noqa: F811
    engine = FakeEngine()
    pokes: list[bool] = []
    sync = make_sync([FIRST, SECOND], engine)
    sync.poke = lambda: pokes.append(True)  # type: ignore[method-assign]
    ctx = relay.service.ctx
    ctx.paper_accounts = SimpleNamespace(market_sync=sync)
    try:
        listed = (await relay.owner.get("/api/admin/market-sources")).json()
        assert listed["paperEngine"]["current"] == "tqkq-sim:default"
        assert "password" not in json.dumps(listed)
        assert (await relay.owner.put("/api/admin/market-sources",
                                      json={"accountIds": [SECOND, FIRST]})).status_code == 200
        assert pokes == [True]
    finally:
        ctx.paper_accounts = None


@pytest.mark.parametrize("raised", [PaperEngineError("paper_engine_unavailable")])
async def test_an_unreachable_engine_shows_as_an_error(raised):
    class Down(FakeEngine):
        async def health(self):
            raise raised

    sync = make_sync([], Down())
    assert await sync.engine_status() == {"error": "paper_engine_unavailable"}
    assert await sync.sync_once() is False and sync.last_error == "paper_engine_unavailable"


async def test_an_unconfigured_list_pushes_nothing_but_a_cleared_list_pushes_empty():
    engine = FakeEngine()
    never = make_sync([], engine, seeded=False)
    assert await never.sync_once() is False and engine.pushes == []  # engine keeps its mounted account
    cleared = make_sync([], engine, seeded=True)
    assert await cleared.sync_once() is True and engine.pushes == [[]]  # the owner stopped every source


async def test_a_hung_engine_does_not_hold_the_source_list(monkeypatch):
    import asyncio

    class Hung(FakeEngine):
        async def health(self):
            await asyncio.sleep(30)

    monkeypatch.setattr(paper_accounts_module, "ENGINE_STATUS_TIMEOUT_SECONDS", 0.05)
    assert await make_sync([], Hung()).engine_status() == {"error": "paper_engine_timeout"}
