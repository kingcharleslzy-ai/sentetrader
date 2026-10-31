"""4.4a 账户目录、会话切换账户、状态快照带账户、默认账户指纹改名与 grants 声明。

T1 目录只列有角色的账户并报告本请求账户；T2 会话切换后 feed/agent 回合跟随新账户；
T3 accounts 文件 `renameFrom` 一次性、幂等、事务内全表改名；T4 声明的 grants 对账为
恰好 owner + 声明集合；T5 状态快照（GET /api/runtime/status 与 SSE runtime.status）带
account 块；T6 无 accounts 文件的部署零变化；T7 SSE 一连上即推瞬态 runtime.status，
已有会话重连也能拿到账户，且不进 Last-Event-ID 重放。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from test_account_registry import (
    BETA_ACCOUNT,
    DEFAULT_ACCOUNT,
    TOKEN,
    local_client,
    write_accounts_file,
)
from test_account_roles import roles_app
from test_simulation_orders import USER, Gateway
from test_stream_revocation import ORIGIN, Reader, serving
from test_trading_accounts import ACCOUNT_COLUMN_TABLES, NOW
from trader_runtime.main import create_app
from trader_runtime.store import RuntimeStore
from trader_runtime.store.trading_accounts import scoped_owner_user_id

OTHER_OWNER = scoped_owner_user_id("someone-else")
FINGERPRINT_ACCOUNT = "tqkq-sim:0123456789ab"
GHOST_ACCOUNT = "tqkq-sim:ghost"
REMOVED_ACCOUNT = "tqkq-sim:alpha-removed"  # sorts before 乙：残留授权会被 min(grants) 选中


def account_rows(store: RuntimeStore, table: str) -> list[tuple[str, ...]]:
    return [tuple(row) for row in store.connection.execute(
        f"SELECT trading_account_id FROM {table}"
    ).fetchall()]


def grant_rows(store: RuntimeStore, account_id: str) -> list[tuple[str, str]]:
    return sorted((row[0], row[1]) for row in store.connection.execute(
        "SELECT user_id, role FROM account_grants WHERE account_id = ?", (account_id,),
    ).fetchall())


async def test_t1_account_directory_lists_granted_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """甲（默认）owner、乙 operator：两项、字段齐全、current 随请求头；角色变化与撤权即时可见。"""
    app, _, _ = roles_app(tmp_path, monkeypatch, "directory.sqlite3")
    try:
        store = app.state.store
        client = await local_client(app)
        try:
            listed = await client.get("/api/accounts")
            assert listed.status_code == 200, listed.text
            payload = listed.json()
            assert [item["id"] for item in payload["accounts"]] == [DEFAULT_ACCOUNT, BETA_ACCOUNT]
            alpha, beta = payload["accounts"]
            assert alpha == {
                "id": DEFAULT_ACCOUNT, "displayName": "账户甲", "role": "owner",
                "environment": "simulation", "providerKind": "fake",
                "executionOwner": True, "isDefault": True, "status": "active", "currency": "CNY",
            }
            assert beta["role"] == "operator" and beta["isDefault"] is False
            assert beta["executionOwner"] is False and beta["displayName"] == "账户乙"
            assert payload["current"] == DEFAULT_ACCOUNT
            headed = await client.get("/api/accounts", headers={"X-AI-Trader-Account": BETA_ACCOUNT})
            assert headed.json()["current"] == BETA_ACCOUNT
            # 角色换成 viewer：目录反映新角色。
            store.grant_account(account_id=BETA_ACCOUNT, user_id=USER, role="viewer", granted_by=OTHER_OWNER)
            demoted = await client.get("/api/accounts")
            beta_now = next(item for item in demoted.json()["accounts"] if item["id"] == BETA_ACCOUNT)
            assert beta_now["role"] == "viewer"
            # 撤掉乙的 grant：只剩甲（默认在前）。
            store.connection.execute(
                "DELETE FROM account_grants WHERE account_id = ? AND user_id = ?", (BETA_ACCOUNT, USER),
            )
            store.connection.commit()
            revoked = await client.get("/api/accounts")
            assert [item["id"] for item in revoked.json()["accounts"]] == [DEFAULT_ACCOUNT]
        finally:
            await client.aclose()
    finally:
        app.state.store.close()


async def test_t2_session_switches_its_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = roles_app(tmp_path, monkeypatch, "switch.sqlite3")
    try:
        store = app.state.store
        runtime_feed = app.state.runtime_feed
        client = await local_client(app)
        try:
            created = await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})
            session_id = created.json()["sessionId"]
            assert store.get_session(session_id)["tradingAccountId"] == DEFAULT_ACCOUNT
            runtime_feed.set_focus(session_id, symbol="SHFE.au2612", duration=60)
            switched = await client.put(
                f"/api/sessions/{session_id}/account", json={"accountId": BETA_ACCOUNT})
            assert switched.status_code == 200, switched.text
            assert {key: value for key, value in switched.json().items() if key != "statusAt"} == {
                "sessionId": session_id, "accountId": BETA_ACCOUNT, "accountGeneration": 1}
            # 会话行、feed 会话状态与下一回合的工具集来源都指向乙。
            assert store.get_session(session_id)["tradingAccountId"] == BETA_ACCOUNT
            assert runtime_feed._sessions[session_id].account_id == BETA_ACCOUNT
            assert app.state.accounts.get(store.get_session(session_id)["tradingAccountId"]) \
                is app.state.accounts.get(BETA_ACCOUNT)
            # 切换后向该会话发布的 runtime.status 带新账户。
            status_events = [event for _, event in store.list_events(session_id)
                             if event["name"] == "runtime.status"]
            assert status_events and status_events[-1]["payload"]["account"]["id"] == BETA_ACCOUNT
            assert status_events[-1]["payload"]["account"]["role"] == "operator"
            # 重复 PUT 同一账户幂等 200；未授权账户 403；已 disable（未装配）的账户 403。
            again = await client.put(
                f"/api/sessions/{session_id}/account", json={"accountId": BETA_ACCOUNT})
            assert again.status_code == 200, again.text
            # #932 R3: the switch (and its repeat) answers with the timestamp of the runtime.status it published, the
            # mark a window waits for before it trusts the account — the repeat publishes a newer one.
            statuses = [event for _, event in store.list_events(session_id) if event["name"] == "runtime.status"]
            assert switched.json()["statusAt"] in {event["timestamp"] for event in statuses}
            assert again.json()["statusAt"] == statuses[-1]["timestamp"] > switched.json()["statusAt"]
            assert statuses[-1]["payload"]["account"]["id"] == BETA_ACCOUNT
            denied = await client.put(
                f"/api/sessions/{session_id}/account", json={"accountId": "tqkq-sim:nope"})
            assert denied.status_code == 403 and denied.json()["detail"] == "account_not_granted"
            store.ensure_trading_account(
                GHOST_ACCOUNT, execution_owner_user_id=OTHER_OWNER, provider_kind="fake",
                display_name="幽灵账户", sidecar_url=None,
            )
            store.grant_account(account_id=GHOST_ACCOUNT, user_id=USER, role="operator", granted_by=OTHER_OWNER)
            store.disable_trading_accounts_except([DEFAULT_ACCOUNT, BETA_ACCOUNT])
            disabled = await client.put(
                f"/api/sessions/{session_id}/account", json={"accountId": GHOST_ACCOUNT})
            assert disabled.status_code == 403 and disabled.json()["detail"] == "account_not_granted"
            missing = await client.put(
                "/api/sessions/no-such-session/account", json={"accountId": BETA_ACCOUNT})
            assert missing.status_code == 404
        finally:
            await client.aclose()
        # SSE：切换后的下一拍 feed.account 读乙账户的 provider（_SessionState 已刷新）。
        saved_logging = {
            name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                   list(logging.getLogger(name).handlers))
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
        }
        try:
            async with serving(app) as base, httpx.AsyncClient(
                base_url=base, headers={"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}"},
                trust_env=False,
            ) as stream_client:
                reader = Reader(stream_client, session_id)
                task = asyncio.create_task(reader.run())
                try:
                    await asyncio.wait_for(reader.opened.wait(), 2)
                    await asyncio.sleep(0.1)
                    await runtime_feed.tick()
                    await asyncio.sleep(0.2)
                    account = next(
                        item for item in reader.messages
                        if item.get("name") == "feed.account"
                    )
                    assert account["payload"]["account"]["accountName"] == "账户乙"
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
        app.state.store.close()


async def test_a_switch_decided_before_a_newer_one_landed_is_refused_not_written_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#932 follow-up (#1000 / #1003 Codex): a switch names the account revision it was decided on — the session row's
    own counter, so another Runtime slot on the same database (blue-green) sees it too. An older PUT reaching a Runtime
    after a newer one landed — left in flight across a reconnect or a slot switch — is 409 account_switch_superseded
    and changes nothing; it is refused before its target is even resolved. Every accepted switch that names a revision
    advances it, a repeat of the same account too; a PUT without the field (an older client) applies as before."""
    app, _, _ = roles_app(tmp_path, monkeypatch, "generation.sqlite3")
    try:
        store = app.state.store
        other_slot = RuntimeStore(tmp_path / "generation.sqlite3")   # the other blue-green slot's connection
        client = await local_client(app)
        try:
            session_id = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
            statuses = lambda: [event for _, event in store.list_events(session_id) if event["name"] == "runtime.status"]  # noqa: E731
            put = lambda body: client.put(f"/api/sessions/{session_id}/account", json=body)  # noqa: E731
            assert store.get_session(session_id)["accountRevision"] == 0
            # Every status carries the revision, the agent's bootstrap one too (#1003 Codex 4309945263).
            assert statuses() and all(event["payload"]["accountGeneration"] == 0 for event in statuses())
            # Both decided on revision 0: the newer lands on the other slot first …
            assert other_slot.switch_session_account(session_id, BETA_ACCOUNT, user_id=USER, expected=0) == (True, 1, BETA_ACCOUNT, DEFAULT_ACCOUNT)
            # … so the older one is refused here, by the shared row — and before it resolves its target: an account
            # the user holds no grant on would be 403 if it were resolved.
            older = await put({"accountId": DEFAULT_ACCOUNT, "expectedGeneration": 0})
            assert older.status_code == 409
            assert older.json()["detail"] == {"code": "account_switch_superseded", "accountGeneration": 1, "accountId": BETA_ACCOUNT}
            stale_ungranted = await put({"accountId": "tqkq-sim:nope", "expectedGeneration": 0})
            assert stale_ungranted.status_code == 409
            assert store.get_session(session_id)["tradingAccountId"] == BETA_ACCOUNT
            # The final check is the conditional update itself: a slot that read revision 0 before still cannot write.
            assert store.switch_session_account(session_id, DEFAULT_ACCOUNT, user_id=USER, expected=0) == (False, 1, BETA_ACCOUNT, BETA_ACCOUNT)
            # Decided on the current revision: applies and advances it; a repeat of the same account advances it too.
            back = await put({"accountId": DEFAULT_ACCOUNT, "expectedGeneration": 1})
            assert back.status_code == 200 and back.json()["accountGeneration"] == 2
            assert statuses()[-1]["payload"]["accountGeneration"] == 2
            same = await put({"accountId": DEFAULT_ACCOUNT, "expectedGeneration": 2})
            assert same.status_code == 200 and same.json()["accountGeneration"] == 3
            assert (await put({"accountId": BETA_ACCOUNT, "expectedGeneration": 2})).status_code == 409   # superseded by the repeat
            # The answer carries the revision this switch committed, even when the other slot moves the row while the
            # status is being published (Codex r4309759904).
            publish = app.state.broker.publish

            async def publish_then_other_slot_moves(*args: Any, **kwargs: Any) -> Any:
                event = await publish(*args, **kwargs)
                other_slot.switch_session_account(session_id, BETA_ACCOUNT, user_id=USER, expected=4)
                return event
            monkeypatch.setattr(app.state.broker, "publish", publish_then_other_slot_moves)
            raced = await put({"accountId": DEFAULT_ACCOUNT, "expectedGeneration": 3})
            assert raced.status_code == 200 and raced.json()["accountGeneration"] == 4
            assert store.get_session(session_id)["accountRevision"] == 5
            monkeypatch.setattr(app.state.broker, "publish", publish)
            assert (await put({"accountId": DEFAULT_ACCOUNT, "expectedGeneration": 5})).json()["accountGeneration"] == 6
            # An older client names no revision: the switch applies as before; a real move still advances the revision
            # (the voice turns' A→B→A check reads it), a repeat does not.
            legacy = await put({"accountId": BETA_ACCOUNT})
            assert legacy.status_code == 200 and store.get_session(session_id)["tradingAccountId"] == BETA_ACCOUNT
            assert legacy.json()["accountGeneration"] == 7
            assert (await put({"accountId": BETA_ACCOUNT})).json()["accountGeneration"] == 7
        finally:
            await client.aclose()
            other_slot.close()
    finally:
        app.state.store.close()


async def test_the_feed_follows_what_the_switch_replaced_and_a_vanished_session_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1003 Codex 4309822248: "did the account change" comes from the row this write replaced, not a read before the
    awaits — another slot moving the session meanwhile still gets the feed told. 4309822263: a session deleted between
    the checks is 404 session_not_found, not 500."""
    app, _, _ = roles_app(tmp_path, monkeypatch, "replaced.sqlite3")
    try:
        store = app.state.store
        other_slot = RuntimeStore(tmp_path / "replaced.sqlite3")
        client = await local_client(app)
        try:
            session_id = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
            app.state.runtime_feed.set_focus(session_id, symbol="SHFE.au2612", duration=60)
            ctx = app.state.app_capabilities.ctx
            resolve = ctx.account_context

            async def resolve_while_the_other_slot_moves(*args: Any, **kwargs: Any) -> Any:
                other_slot.set_session_trading_account(session_id, BETA_ACCOUNT, user_id=USER)
                return await resolve(*args, **kwargs)
            # The session reads DEFAULT when the request arrives; the other slot moves it to BETA meanwhile; this
            # older-client switch then writes DEFAULT back — a real change the feed must hear about.
            monkeypatch.setattr(ctx, "account_context", resolve_while_the_other_slot_moves)
            app.state.runtime_feed.session_account_changed(session_id, BETA_ACCOUNT)
            switched = await client.put(f"/api/sessions/{session_id}/account", json={"accountId": DEFAULT_ACCOUNT})
            assert switched.status_code == 200
            assert app.state.runtime_feed._sessions[session_id].account_id == DEFAULT_ACCOUNT
            monkeypatch.setattr(ctx, "account_context", resolve)

            def vanished(*args: Any, **kwargs: Any) -> tuple[int, str]:
                raise KeyError(session_id)
            monkeypatch.setattr(store, "session_account_state", vanished)
            gone = await client.put(f"/api/sessions/{session_id}/account", json={"accountId": BETA_ACCOUNT, "expectedGeneration": 2})
            assert gone.status_code == 404 and gone.json()["detail"] == "session_not_found"
        finally:
            await client.aclose()
            other_slot.close()
    finally:
        app.state.store.close()


async def test_a_status_built_before_a_switch_landed_is_not_published_with_the_new_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1003 Codex 4310015757: the broker stamps a runtime.status with the session's revision only while the row is
    still on the account the status names — a status built for A, published after the session moved to B, is stale and
    not sent (never "A with B's revision"); B's own status goes out with B's revision."""
    app, _, _ = roles_app(tmp_path, monkeypatch, "stale-status.sqlite3")
    try:
        store, broker = app.state.store, app.state.broker
        client = await local_client(app)
        try:
            session_id = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
            ctx = app.state.app_capabilities.ctx
            statuses = lambda: [event for _, event in store.list_events(session_id) if event["name"] == "runtime.status"]  # noqa: E731
            built_for_a = {key: value for key, value in ctx.status_for_session(session_id).items() if key != "accountGeneration"}
            assert built_for_a["account"]["id"] == DEFAULT_ACCOUNT
            store.set_session_trading_account(session_id, BETA_ACCOUNT, user_id=USER)   # another slot's switch lands
            before = len(statuses())
            await broker.publish(session_id, kind="result", name="runtime.status", payload=built_for_a)
            assert len(statuses()) == before                                          # the stale A status is not sent
            built_for_b = {key: value for key, value in ctx.status_for_session(session_id).items() if key != "accountGeneration"}
            await broker.publish(session_id, kind="result", name="runtime.status", payload=built_for_b)
            last = statuses()[-1]["payload"]
            assert last["account"]["id"] == BETA_ACCOUNT and last["accountGeneration"] == 1   # the window stays on B
        finally:
            await client.aclose()
    finally:
        app.state.store.close()


def test_the_account_revision_column_migrates_in_and_an_older_build_still_reads_the_row(tmp_path: Path) -> None:
    """#932 follow-up: a database from before the revision gets `account_revision` (default 0) on open; the row stays
    readable and writable by a build that knows nothing of it (rollback)."""
    import sqlite3
    path = tmp_path / "old-sessions.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mode TEXT NOT NULL, agent_id TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default');
        INSERT INTO sessions VALUES ('s-old', 'u', 'local', 'native', 'active', '2026-10-18', '2026-10-18', '2099-01-01',
            'tqkq-sim:default');
    """)
    old.commit()
    old.close()
    store = RuntimeStore(path)
    try:
        assert store.get_session("s-old")["accountRevision"] == 0
        assert store.switch_session_account("s-old", "tqkq-sim:b", user_id="u", expected=0) == (True, 1, "tqkq-sim:b", "tqkq-sim:default")
    finally:
        store.close()
    # The previous build's own statements: named-column insert and select, an update without the column.
    previous = sqlite3.connect(path)
    previous.row_factory = sqlite3.Row
    previous.execute("""INSERT INTO sessions (id, user_id, mode, agent_id, status, created_at, updated_at, expires_at,
        trading_account_id) VALUES ('s-new', 'u', 'local', 'native', 'active', 'x', 'x', '2099-01-01', 'tqkq-sim:default')""")
    previous.execute("UPDATE sessions SET trading_account_id = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                     ("tqkq-sim:default", "y", "s-old", "u"))
    previous.commit()
    rows = {row["id"]: dict(row) for row in previous.execute("SELECT * FROM sessions")}
    previous.close()
    assert rows["s-old"]["trading_account_id"] == "tqkq-sim:default" and rows["s-new"]["account_revision"] == 0
    reopened = RuntimeStore(path)
    try:
        assert reopened.get_session("s-new")["accountRevision"] == 0 and reopened.get_session("s-old")["accountRevision"] == 1
    finally:
        reopened.close()


async def test_implicit_default_skips_grants_this_boot_cannot_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """526-B1：隐式默认只从本进程可用的授权里选——残留在已移除账户上的 grant 不会把
    无请求头的账户请求变成 403，健康的授权照常接上。"""
    app, _, _ = roles_app(tmp_path, monkeypatch, "leftover-grant.sqlite3")
    try:
        store = app.state.store
        # local-user 只剩乙的授权，外加一条 id 排在乙前面、账户已从 accounts 文件
        # 移除（注册表里没有）的残留授权——记录还在库里，看起来仍是 active。
        store.connection.execute(
            "DELETE FROM account_grants WHERE account_id = ? AND user_id = ?",
            (DEFAULT_ACCOUNT, USER),
        )
        store.connection.commit()
        store.ensure_trading_account(
            REMOVED_ACCOUNT, execution_owner_user_id=OTHER_OWNER, provider_kind="tqkq-sidecar",
            display_name="已移除的静态账户", sidecar_url=None,
        )
        store.grant_account(account_id=REMOVED_ACCOUNT, user_id=USER, role="operator",
                            granted_by=OTHER_OWNER)
        client = await local_client(app)
        try:
            listed = await client.get("/api/accounts")
            assert listed.status_code == 200, listed.text
            assert listed.json()["current"] == BETA_ACCOUNT
        finally:
            await client.aclose()
    finally:
        app.state.store.close()


async def test_directory_fallback_keeps_the_requested_header_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """526-B2：目录的 503 回退（账户装配中/排队）保留请求头里的目标账户，
    不把 current 显示成默认账户。"""
    app, _, _ = roles_app(tmp_path, monkeypatch, "fallback.sqlite3")
    try:
        async def assembling(request, user_id, **kwargs):
            raise HTTPException(status_code=503, detail="account_assembly_required")
        monkeypatch.setattr(app.state.ctx, "account_context", assembling)
        client = await local_client(app)
        try:
            listed = await client.get("/api/accounts", headers={"X-AI-Trader-Account": BETA_ACCOUNT})
            assert listed.status_code == 200, listed.text
            assert listed.json()["current"] == BETA_ACCOUNT
        finally:
            await client.aclose()
    finally:
        app.state.store.close()


# 526-D / #526: account-scoped tables that are not in ACCOUNT_COLUMN_TABLES must still be renamed whole.
RENAMED_TABLES_OUTSIDE_THE_COLUMN_LIST = (
    ("trade_notes", "trading_account_id"), ("target_position_plans", "trading_account_id"),
    ("reminder_notices", "account_id"),
    ("strategy_drawdown_states", "trading_account_id"), ("daily_reviews", "account_id"),
    ("review_observation_verdicts", "account_id"), ("user_default_accounts", "account_id"),
)


def _seed_default_account_rows(app) -> dict[str, int]:
    """在每个账户列表面写至少一行旧 id 数据；返回每表行数。"""
    store = app.state.store
    session = app.state.broker.create_session("local", "native", USER)
    store.create(
        proposal_id="proposal-rename", session_id=session.session_id, proposal_type="trade",
        status="rejected", payload={"symbol": "SHFE.au2612"}, user_id=USER, version=1,
        trading_account_id=DEFAULT_ACCOUNT,
    )
    store.save_strategy_draft(
        strategy_id="strategy-rename", user_id=USER, source_session_id=session.session_id,
        name="改名前策略", symbol="SHFE.au2612", version=1, status="ready",
        definition={"template": "ma_cross"}, definition_digest="digest-rename",
        trading_account_id=DEFAULT_ACCOUNT,
    )
    store.upsert_account_daily("2026-09-18", {"equity": 200000.0})
    connection = store.connection
    connection.executescript(f"""
        INSERT INTO account_trades (trading_account_id, trade_id, symbol, direction, offset,
            price, volume, trade_date_time, trading_day, captured_at)
        VALUES ('{DEFAULT_ACCOUNT}', 'trade-rename', 'SHFE.au2612', 'BUY', 'OPEN', 612.0, 1, 1000, '2026-09-18', '{NOW}');
        INSERT INTO strategy_monitor_states (strategy_id, user_id, trading_date, updated_at, trading_account_id)
        VALUES ('strategy-rename', '{USER}', '2026-09-18', '{NOW}', '{DEFAULT_ACCOUNT}');
        INSERT INTO strategy_intent_snapshots (id, strategy_id, user_id, strategy_digest,
            signal_bar_time, context_digest, payload_json, created_at)
        VALUES ('intent-rename', 'strategy-rename', '{USER}', 'digest-rename', '1', 'ctx-rename', '{{}}', '{NOW}');
        INSERT INTO strategy_execution_attempts (id, strategy_id, user_id, intent_id,
            signal_bar_time, trading_day, action, volume, status, created_at, updated_at, trading_account_id)
        VALUES ('attempt-rename', 'strategy-rename', '{USER}', 'intent-rename', '1', '2026-09-18',
            'open_long', 1, 'SKIPPED', '{NOW}', '{NOW}', '{DEFAULT_ACCOUNT}');
        INSERT INTO strategy_fill_events (user_id, event_id, trade_time, trading_day,
            client_order_id, payload_json, trading_account_id)
        VALUES ('{USER}', 'fill-rename', 1000, '2026-09-18', 'cli-rename', '{{}}', '{DEFAULT_ACCOUNT}');
        INSERT INTO tqkq_sim_order_links (id, user_id, proposal_id, client_order_id, request_json,
            submit_phase, cancel_phase, created_at, updated_at, trading_account_id)
        VALUES ('link-rename', '{USER}', 'proposal-rename', 'cli-rename', '{{}}', 'PREPARED', 'NONE',
            '{NOW}', '{NOW}', '{DEFAULT_ACCOUNT}');
        INSERT INTO ai_execution_controls (user_id, trading_account_id, stop_status, updated_at)
        VALUES ('{USER}', '{DEFAULT_ACCOUNT}', 'STOPPED', '{NOW}');
        INSERT INTO ai_execution_authorizations (id, user_id, session_id, status, symbols_json,
            permissions_json, max_order_volume, max_orders, expires_at, created_at, updated_at, trading_account_id)
        VALUES ('auth-rename', '{USER}', '{session.session_id}', 'STOPPED', '[]', '[]', 1, 1, '{NOW}', '{NOW}', '{NOW}',
            '{DEFAULT_ACCOUNT}');
        INSERT INTO ai_stop_orders (id, user_id, authorization_id, session_id, symbol,
            position_direction, position_bucket, volume, trigger_price, price_intent, status,
            created_at, updated_at, trading_account_id)
        VALUES ('stop-rename', '{USER}', 'auth-rename', '{session.session_id}', 'SHFE.au2612',
            'long', 'today', 1, '800', 'passive', 'CANCELLED', '{NOW}', '{NOW}', '{DEFAULT_ACCOUNT}');
        INSERT INTO trade_notes (trading_account_id, round_id, text, via, user_id, updated_at)
        VALUES ('{DEFAULT_ACCOUNT}', 'round-rename', '改名前的交易笔记', 'manual', '{USER}', '{NOW}');
        INSERT INTO target_position_plans (id, user_id, trading_account_id, symbol, target_net,
            leg_index, leg_json, current_proposal_id, current_client_order_ids, status,
            created_at, updated_at)
        VALUES ('plan-rename', '{USER}', '{DEFAULT_ACCOUNT}', 'SHFE.au2612', 2, 0, '{{}}', NULL,
            '[]', 'active', '{NOW}', '{NOW}');
        INSERT INTO reminder_notices (user_id, account_id, trade_day, kind, text, created_at)
        VALUES ('{USER}', '{DEFAULT_ACCOUNT}', '2026-09-18', 'risk_ratio', '风险度提醒', '{NOW}');
        INSERT INTO strategy_drawdown_states (strategy_id, user_id, trading_account_id, status, limit_percent,
            round_started_at, updated_at)
        VALUES ('strategy-rename', '{USER}', '{DEFAULT_ACCOUNT}', 'running', 5.0, '{NOW}', '{NOW}');
        INSERT INTO daily_reviews (user_id, account_id, trading_day, payload_json, generated_at)
        VALUES ('{USER}', '{DEFAULT_ACCOUNT}', '2026-09-18', '{{}}', '{NOW}');
        INSERT INTO review_observation_verdicts (user_id, key, verdict, text, account_id, trading_day, created_at)
        VALUES ('{USER}', 'obs-rename', 'remembered', '改名前的习惯', '{DEFAULT_ACCOUNT}', '2026-09-18', '{NOW}');
        INSERT OR REPLACE INTO user_default_accounts (user_id, account_id, updated_at)
        VALUES ('{USER}', '{DEFAULT_ACCOUNT}', '{NOW}');
    """)
    connection.commit()
    return {table: len(account_rows(store, table)) for table in ACCOUNT_COLUMN_TABLES} | {
        table: len(rows) for table, rows in (
            ("trade_notes", connection.execute("SELECT trading_account_id FROM trade_notes").fetchall()),
            ("target_position_plans", connection.execute(
                "SELECT trading_account_id FROM target_position_plans").fetchall()),
            ("reminder_notices", connection.execute(
                "SELECT account_id FROM reminder_notices").fetchall()),
            *[(table, connection.execute(f"SELECT {column} FROM {table}").fetchall())
              for table, column in RENAMED_TABLES_OUTSIDE_THE_COLUMN_LIST[3:]],
        )
    }


def test_t3_rename_default_account_to_fingerprint_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无文件启动写满 12 张表 → 带 renameFrom 的文件重启：一个事务全表改名，幂等可重启。"""
    database = tmp_path / "rename.sqlite3"
    app = create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        seeded = _seed_default_account_rows(app)
        assert all(count >= 1 for count in seeded.values()), seeded
        seeded["trading_accounts"] = len(app.state.store.connection.execute(
            "SELECT id FROM trading_accounts").fetchall())
        seeded["account_grants"] = len(app.state.store.connection.execute(
            "SELECT account_id FROM account_grants").fetchall())
    finally:
        app.state.store.close()
    accounts_file = write_accounts_file(tmp_path / "accounts.json", [{
        "id": FINGERPRINT_ACCOUNT, "displayName": "甲账户", "dataMode": "fake",
        "renameFrom": DEFAULT_ACCOUNT,
    }])
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(accounts_file))
    renamed = create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        store = renamed.state.store
        registry = renamed.state.accounts
        # 12 张业务表 + 登记表 + grants：旧 id 为 0，新 id 计数等于原计数。
        for table, column in (
            *[(table, "trading_account_id") for table in ACCOUNT_COLUMN_TABLES],
            # 526-D / #526：不在 ACCOUNT_COLUMN_TABLES 里的账户隔离表也必须整体改名。
            *RENAMED_TABLES_OUTSIDE_THE_COLUMN_LIST,
            ("trading_accounts", "id"), ("account_grants", "account_id"),
        ):
            rows = [tuple(row) for row in store.connection.execute(
                f"SELECT {column} FROM {table}").fetchall()]
            assert DEFAULT_ACCOUNT not in rows, table
            assert len(rows) == seeded[table], (table, len(rows), seeded[table])
        assert registry.default.account_id == FINGERPRINT_ACCOUNT
        assert store.default_trading_account_id == FINGERPRINT_ACCOUNT
        assert store.get_trading_account(FINGERPRINT_ACCOUNT)["display_name"] == "甲账户"
        assert store.get_trading_account(FINGERPRINT_ACCOUNT)["execution_owner_user_id"] == USER
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # 新写入不再落回旧 id。
        session = renamed.state.broker.create_session("local", "native", USER)
        assert store.get_session(session.session_id)["tradingAccountId"] == FINGERPRINT_ACCOUNT
        store.create(
            proposal_id="proposal-after-rename", session_id=session.session_id,
            proposal_type="trade", status="rejected", payload={}, user_id=USER, version=1,
            trading_account_id=None,
        )
        assert [tuple(row) for row in store.connection.execute(
            "SELECT trading_account_id FROM proposals WHERE id = 'proposal-after-rename'").fetchall()] \
            == [(FINGERPRINT_ACCOUNT,)]
        assert store.account_role(account_id=FINGERPRINT_ACCOUNT, user_id=USER) == "owner"
        # 改名完成、尚未新增写入时的行数：第二次启动的幂等基线。
        expected = {
            **{table: len(account_rows(store, table)) for table in ACCOUNT_COLUMN_TABLES},
            "trade_notes": len(store.connection.execute(
                "SELECT trading_account_id FROM trade_notes").fetchall()),
            "target_position_plans": len(store.connection.execute(
                "SELECT trading_account_id FROM target_position_plans").fetchall()),
            "reminder_notices": len(store.connection.execute(
                "SELECT account_id FROM reminder_notices").fetchall()),
            **{table: len(store.connection.execute(f"SELECT {column} FROM {table}").fetchall())
               for table, column in RENAMED_TABLES_OUTSIDE_THE_COLUMN_LIST[3:]},
            "trading_accounts": len(store.connection.execute(
                "SELECT id FROM trading_accounts").fetchall()),
            "account_grants": len(store.connection.execute(
                "SELECT account_id FROM account_grants").fetchall()),
        }
    finally:
        renamed.state.store.close()
    # 第二次同文件启动：幂等，计数不变。
    again = create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        store = again.state.store
        for table, column in (
            *[(table, "trading_account_id") for table in ACCOUNT_COLUMN_TABLES],
            # 526-D / #526：不在 ACCOUNT_COLUMN_TABLES 里的账户隔离表也必须整体改名。
            *RENAMED_TABLES_OUTSIDE_THE_COLUMN_LIST,
            ("trading_accounts", "id"), ("account_grants", "account_id"),
        ):
            rows = [tuple(row) for row in store.connection.execute(
                f"SELECT {column} FROM {table}").fetchall()]
            assert len(rows) == expected[table], table
    finally:
        again.state.store.close()


def test_t3_rename_edge_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # renameFrom 指向不存在的账户：正常启动（改名无操作），账户照常装配。
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(
        tmp_path / "missing-old.json",
        [{"id": FINGERPRINT_ACCOUNT, "displayName": "甲", "dataMode": "fake",
          "renameFrom": "tqkq-sim:never-existed"}],
    )))
    app = create_app(tmp_path / "missing-old.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        assert [services.account_id for services in app.state.accounts.all()] == [FINGERPRINT_ACCOUNT]
    finally:
        app.state.store.close()
    # 旧 id 与新 id 同时存在：拒绝启动。
    database = tmp_path / "conflict.sqlite3"
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(
        tmp_path / "two.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "甲", "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "displayName": "乙", "dataMode": "fake"},
        ],
    )))
    app = create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    app.state.store.close()
    write_accounts_file(tmp_path / "two.json", [
        {"id": BETA_ACCOUNT, "displayName": "乙", "dataMode": "fake", "renameFrom": DEFAULT_ACCOUNT},
    ])
    with pytest.raises(RuntimeError) as raised:
        create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    assert "account_rename_conflict" in str(raised.value)
    # 校验：renameFrom 等于自身 id、renameFrom 与其它条目 id 重复、grants 两角色重叠、
    # grants 含执行负责人，都拒绝启动。
    for payload in (
        [{"id": FINGERPRINT_ACCOUNT, "dataMode": "fake", "renameFrom": FINGERPRINT_ACCOUNT}],
        [
            {"id": FINGERPRINT_ACCOUNT, "dataMode": "fake", "renameFrom": BETA_ACCOUNT},
            {"id": BETA_ACCOUNT, "dataMode": "fake"},
        ],
        [
            {"id": BETA_ACCOUNT, "dataMode": "fake", "executionOwnerUsername": "someone-else",
             "grants": {"operator": ["u1"], "viewer": ["u1"]}},
        ],
        [
            {"id": BETA_ACCOUNT, "dataMode": "fake", "executionOwnerUsername": "someone-else",
             "grants": {"operator": ["someone-else"]}},
        ],
    ):
        monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(
            tmp_path / "broken.json", payload)))
        with pytest.raises(RuntimeError) as raised:
            create_app(tmp_path / "broken.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake")
        assert "accounts_file_invalid" in str(raised.value)


async def test_t4_declared_grants_reconcile_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """声明的 grants 对账为恰好 owner + 声明集合；未声明的非默认账户只有 owner；
    默认账户的注册自动授权只在未声明时生效。"""
    accounts_file = tmp_path / "grants.json"
    database = tmp_path / "grants.sqlite3"
    beta_entry: dict[str, Any] = {
        "id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake",
        "executionOwnerUsername": "someone-else",
    }
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(accounts_file, [
        {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
        dict(beta_entry),
    ])))
    app = create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        store = app.state.store
        # 未声明的非默认账户：只有执行负责人。
        assert grant_rows(store, BETA_ACCOUNT) == [(OTHER_OWNER, "owner")]
        # 手工多授一条 u3 operator、甲上多授一条观察者：无声明时不动。
        store.grant_account(account_id=BETA_ACCOUNT, user_id=scoped_owner_user_id("u3"),
                            role="operator", granted_by=OTHER_OWNER)
        store.grant_account(account_id=DEFAULT_ACCOUNT, user_id="watcher",
                            role="viewer", granted_by=USER)
    finally:
        app.state.store.close()
    # 乙声明 grants：u1 operator、u2 viewer；u3 多余行被删，角色以声明为准。
    beta_entry["grants"] = {"operator": ["u1"], "viewer": ["u2"]}
    write_accounts_file(accounts_file, [
        {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
        dict(beta_entry),
    ])
    declared = create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        store = declared.state.store
        assert grant_rows(store, BETA_ACCOUNT) == sorted([
            (OTHER_OWNER, "owner"),
            (scoped_owner_user_id("u1"), "operator"),
            (scoped_owner_user_id("u2"), "viewer"),
        ])
        # 甲未声明：不做对账；2026-09-23 隔离迁移会清掉 granted_by=执行负责人
        # 的非 owner 行（生产中只有已删除的自动授权机制会写这种行），watcher 一并消失。
        assert grant_rows(store, DEFAULT_ACCOUNT) == [(USER, "owner")]
        # 角色不同则更新：把 u1 的行预先改成 viewer 后重启，回到声明的 operator。
        store.grant_account(account_id=BETA_ACCOUNT, user_id=scoped_owner_user_id("u1"),
                            role="viewer", granted_by=OTHER_OWNER)
    finally:
        declared.state.store.close()
    reconciled = create_app(database, local_token=TOKEN, agent_mode="fake", data_mode="fake")
    try:
        assert grant_rows(reconciled.state.store, BETA_ACCOUNT) == sorted([
            (OTHER_OWNER, "owner"),
            (scoped_owner_user_id("u1"), "operator"),
            (scoped_owner_user_id("u2"), "viewer"),
        ])
    finally:
        reconciled.state.store.close()
    # 注册不授权（2026-09-23 隔离修复）：无论默认账户是否声明 grants，注册
    # 都不再自动给新成员授 operator；成员只能经声明文件或手工授权获得账户。
    store = RuntimeStore(tmp_path / "registration.sqlite3")
    try:
        store.ensure_default_trading_account(
            execution_owner_user_id=USER, provider_kind="fake",
            display_name="假数据模拟账户", sidecar_url=None,
        )
        store.create_cloud_user(username="dave", password_hash="x", invited_by=None)
        assert grant_rows(store, DEFAULT_ACCOUNT) == [(USER, "owner")]
    finally:
        store.close()
    store = RuntimeStore(tmp_path / "registration-managed.sqlite3")
    try:
        store.ensure_trading_account(
            DEFAULT_ACCOUNT, execution_owner_user_id=USER, provider_kind="fake",
            display_name="假数据模拟账户", sidecar_url=None,
            grants=((scoped_owner_user_id("u1"), "operator"),),
        )
        store.create_cloud_user(username="dave", password_hash="x", invited_by=None)
        assert grant_rows(store, DEFAULT_ACCOUNT) == sorted([
            (USER, "owner"), (scoped_owner_user_id("u1"), "operator"),
        ])
    finally:
        store.close()


async def test_t5_status_snapshots_carry_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = roles_app(tmp_path, monkeypatch, "status.sqlite3")
    try:
        client = await local_client(app)
        try:
            status = await client.get("/api/runtime/status")
            assert status.status_code == 200, status.text
            account = status.json()["account"]
            assert account == {
                "id": DEFAULT_ACCOUNT, "displayName": "账户甲", "role": "owner",
                "environment": "simulation", "executionOwner": True, "currency": "CNY",
            }
            headed = await client.get(
                "/api/runtime/status", headers={"X-AI-Trader-Account": BETA_ACCOUNT})
            assert headed.json()["account"]["id"] == BETA_ACCOUNT
            assert headed.json()["account"]["executionOwner"] is False
        finally:
            await client.aclose()
    finally:
        app.state.store.close()
    # 原生 Agent：GET /api/sessions/{id} 触发 refresh_session，发布的 runtime.status
    # 按该会话绑定的账户组块（bootstrap 走同一条组块路径）。
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(
        tmp_path / "native-accounts.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake",
             "executionOwnerUsername": "someone-else"},
        ],
    )))
    native_app = create_app(
        tmp_path / "native-status.sqlite3", local_token=TOKEN, agent_mode="native",
        agent_provider="deepseek", data_mode="fake",
    )
    try:
        native_app.state.store.grant_account(
            account_id=BETA_ACCOUNT, user_id=USER, role="operator", granted_by=OTHER_OWNER)
        beta_session = native_app.state.broker.create_session(
            "local", "native", USER, trading_account_id=BETA_ACCOUNT).session_id
        client = await local_client(native_app)
        try:
            resumed = await client.get(f"/api/sessions/{beta_session}")
            assert resumed.status_code == 200, resumed.text
        finally:
            await client.aclose()
        statuses = [event for _, event in native_app.state.store.list_events(beta_session)
                    if event["name"] == "runtime.status"]
        assert statuses and statuses[-1]["payload"]["account"] == {
            "id": BETA_ACCOUNT, "displayName": "账户乙", "role": "operator",
            "environment": "simulation", "executionOwner": False, "currency": "CNY",
        }
    finally:
        native_app.state.store.close()


async def test_t6_single_account_deployment_is_unchanged(tmp_path: Path) -> None:
    gateway = Gateway()
    app = create_app(
        tmp_path / "single.sqlite3", local_token=TOKEN, agent_mode="fake",
        data_mode="fake", simulation_gateway=gateway,
    )
    try:
        client = await local_client(app)
        try:
            listed = await client.get("/api/accounts")
            assert listed.status_code == 200, listed.text
            payload = listed.json()
            assert payload["accounts"] == [{
                "id": DEFAULT_ACCOUNT, "displayName": "假数据模拟账户", "role": "owner",
                "environment": "simulation", "providerKind": "fake",
                "executionOwner": True, "isDefault": True, "status": "active", "currency": "CNY",
            }]
            assert payload["current"] == DEFAULT_ACCOUNT
            status = await client.get("/api/runtime/status")
            assert status.json()["account"]["id"] == DEFAULT_ACCOUNT
            assert status.json()["account"]["role"] == "owner"
        finally:
            await client.aclose()
        assert app.state.store.default_trading_account_id == DEFAULT_ACCOUNT
    finally:
        app.state.store.close()


async def test_t7_sse_connect_pushes_transient_runtime_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有会话直接订阅 /api/events（不经 POST /api/sessions，无 bootstrap）：一连上
    就收到瞬态 runtime.status 且带会话账户；不入 outbox，Last-Event-ID 重放里没有它。"""
    app, _, _ = roles_app(tmp_path, monkeypatch, "reconnect.sqlite3")
    try:
        store = app.state.store
        session_id = app.state.broker.create_session("local", "native", USER).session_id
        assert store.get_session(session_id)["tradingAccountId"] == DEFAULT_ACCOUNT

        async def until(predicate, timeout: float = 2.0) -> None:
            async with asyncio.timeout(timeout):
                while not predicate():
                    await asyncio.sleep(0.02)

        # serving() 重排进程级 uvicorn 日志配置，退出时原样还原，保护后续 caplog 测试。
        saved_logging = {
            name: (logging.getLogger(name).level, logging.getLogger(name).propagate,
                   list(logging.getLogger(name).handlers))
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
        }
        try:
            async with serving(app) as base, httpx.AsyncClient(
                base_url=base, headers={"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}"},
                trust_env=False,
            ) as stream_client:
                reader = Reader(stream_client, session_id)
                task = asyncio.create_task(reader.run())
                try:
                    await asyncio.wait_for(reader.opened.wait(), 2)
                    await until(lambda: any(
                        item.get("name") == "runtime.status" for item in reader.messages))
                    status = next(item for item in reader.messages
                                  if item.get("name") == "runtime.status")
                    assert status["payload"]["account"]["id"] == DEFAULT_ACCOUNT
                    assert status["payload"]["account"]["role"] == "owner"
                    # 瞬态未持久化：重放序列里没有它。
                    assert [event for _, event in store.list_events(session_id)
                            if event["name"] == "runtime.status"] == []
                    # 从 0 重放的第二个读者：持久事件照常到达，runtime.status 只有本次
                    # 连接触发的那一条瞬态（若被误持久化，这里会是两条）。
                    await app.state.broker.publish(
                        session_id, kind="message", name="message.completed",
                        payload={"turnId": "t7", "role": "assistant", "agentId": "native",
                                 "text": "replay-me"})
                    async with httpx.AsyncClient(
                        base_url=base, trust_env=False,
                        headers={"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}",
                                 "Last-Event-ID": "0"},
                    ) as replay_client:
                        replay = Reader(replay_client, session_id)
                        replay_task = asyncio.create_task(replay.run())
                        try:
                            await asyncio.wait_for(replay.opened.wait(), 2)
                            await until(lambda: any(
                                item.get("payload", {}).get("text") == "replay-me"
                                for item in replay.messages))
                            assert [item.get("payload", {}).get("text")
                                    for item in replay.messages
                                    if item.get("name") == "message.completed"] == ["replay-me"]
                            statuses = [item for item in replay.messages
                                        if item.get("name") == "runtime.status"]
                            assert len(statuses) == 1
                            assert statuses[0]["payload"]["account"]["id"] == DEFAULT_ACCOUNT
                        finally:
                            replay_task.cancel()
                            await asyncio.gather(replay_task, return_exceptions=True)
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
        app.state.store.close()
