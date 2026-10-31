"""4.1 多账户数据层：默认账户、12 张目标表的账户列与三张复合主键、旧行回填。

单账户部署行为不变；这里只验证数据层已经能按账户承载行，以及旧库升级后
每行都归到 `tqkq-sim:default`。
"""
from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.auth import hash_owner_password
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.store import RuntimeStore

DEFAULT_ACCOUNT = "tqkq-sim:default"
OTHER_ACCOUNT = "tqkq-sim:other"
ACCOUNT_COLUMN_TABLES = (
    "account_trades", "account_daily", "ai_execution_controls", "tqkq_sim_order_links",
    "ai_execution_authorizations", "ai_stop_orders", "strategy_drafts", "strategy_monitor_states",
    "strategy_execution_attempts", "strategy_fill_events", "sessions", "proposals",
)
ORIGIN = "https://qilinshuzhi.com"
PASSWORD = "correct-demo-password"
PASSWORD_HASH = hash_owner_password(PASSWORD, salt=b"0123456789abcdef")
SESSION_SECRET = "owner-session-secret-for-tests-more-than-32-bytes"
NOW = "2026-09-20T08:00:00+00:00"


def scoped_owner_id(username: str) -> str:
    return f"owner-{sha256(username.encode()).hexdigest()}"


def table_columns(connection: sqlite3.Connection, table: str) -> list[tuple[str, int]]:
    return [(row[1], int(row[5])) for row in connection.execute(f"PRAGMA table_info({table})")]


def primary_key_order(connection: sqlite3.Connection, table: str) -> list[str]:
    columns = table_columns(connection, table)
    keyed = sorted(((pk, name) for name, pk in columns if pk), key=lambda item: item[0])
    return [name for _, name in keyed]


def object_sql(connection: sqlite3.Connection, kind: str, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
    ).fetchone()
    return str(row[0]) if row else None


def all_table_sql(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def _create_pre_41_database(path: Path, *, cloud_users: tuple[str, ...] = ()) -> sqlite3.Connection:
    """Build the exact schema current main (pre-4.1) produces, with one row in each target table."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE account_trades (
            trade_id TEXT PRIMARY KEY,
            client_order_id TEXT NOT NULL DEFAULT '',
            exchange_trade_id TEXT NOT NULL DEFAULT '',
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            offset TEXT NOT NULL,
            price REAL,
            volume INTEGER NOT NULL,
            trade_date_time INTEGER NOT NULL,
            trading_day TEXT,
            volume_multiple REAL,
            captured_at TEXT NOT NULL
        );
        ALTER TABLE account_trades ADD COLUMN source TEXT NOT NULL DEFAULT 'activity';
        ALTER TABLE account_trades ADD COLUMN approximate INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX idx_account_trades_symbol_time ON account_trades(symbol, trade_date_time);
        CREATE TABLE account_daily (
            trading_day TEXT PRIMARY KEY,
            equity REAL,
            static_balance REAL,
            close_profit REAL,
            position_profit REAL,
            commission REAL,
            today_net_pnl REAL,
            captured_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        ALTER TABLE sessions ADD COLUMN codex_thread_id TEXT;
        ALTER TABLE sessions ADD COLUMN codex_tool_protocol_version INTEGER NOT NULL DEFAULT 0;
        CREATE TABLE proposals (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            proposal_type TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        ALTER TABLE proposals ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local-user';
        ALTER TABLE proposals ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE proposals ADD COLUMN approval_id TEXT;
        ALTER TABLE proposals ADD COLUMN approval_expires_at TEXT;
        ALTER TABLE proposals ADD COLUMN approval_used_at TEXT;
        CREATE TABLE ai_execution_controls (
            user_id TEXT PRIMARY KEY,
            stop_status TEXT,
            generation INTEGER NOT NULL DEFAULT 0,
            limits_revision INTEGER NOT NULL DEFAULT 0,
            max_order_volume INTEGER,
            max_orders INTEGER,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE ai_execution_authorizations (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('ACTIVE', 'STOPPED', 'EXPIRED', 'EMERGENCY_STOP')
            ),
            symbols_json TEXT NOT NULL,
            permissions_json TEXT NOT NULL,
            max_order_volume INTEGER NOT NULL CHECK (max_order_volume > 0),
            max_orders INTEGER NOT NULL CHECK (max_orders > 0),
            used_orders INTEGER NOT NULL DEFAULT 0 CHECK (used_orders >= 0),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_ai_execution_authorizations_active
            ON ai_execution_authorizations(user_id)
            WHERE status = 'ACTIVE';
        CREATE TABLE cloud_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL,
            invited_by TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO account_trades (trade_id, client_order_id, symbol, direction, offset, price,"
        " volume, trade_date_time, trading_day, volume_multiple, captured_at)"
        " VALUES ('legacy-1', 'cli-1', 'SHFE.au2612', 'BUY', 'OPEN', 612.0, 1, 1000, '2026-09-18', 1000.0, ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO account_trades (trade_id, client_order_id, symbol, direction, offset, price,"
        " volume, trade_date_time, trading_day, captured_at)"
        " VALUES ('legacy-2', 'cli-2', 'SHFE.au2612', 'SELL', 'CLOSE', 614.0, 1, 2000, '2026-09-18', ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO account_daily (trading_day, equity, static_balance, close_profit,"
        " position_profit, commission, today_net_pnl, captured_at)"
        " VALUES ('2026-09-18', 200000.0, 200000.0, 120.0, 0.0, 12.4, 107.6, ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO sessions (id, user_id, mode, agent_id, status, created_at, updated_at, expires_at)"
        " VALUES ('legacy-session', 'local-user', 'chat', 'native', 'active', ?, ?, ?)",
        (NOW, NOW, NOW),
    )
    connection.execute(
        "INSERT INTO proposals (id, session_id, proposal_type, status, payload_json, created_at, updated_at)"
        " VALUES ('legacy-proposal', 'legacy-session', 'trade', 'rejected', '{}', ?, ?)",
        (NOW, NOW),
    )
    connection.execute(
        "INSERT INTO ai_execution_controls (user_id, stop_status, generation, updated_at)"
        " VALUES ('local-user', 'HALT_OPENS', 3, ?)",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO ai_execution_authorizations (id, user_id, session_id, status, symbols_json,"
        " permissions_json, max_order_volume, max_orders, expires_at, created_at, updated_at)"
        " VALUES ('legacy-auth', 'local-user', 'legacy-session', 'ACTIVE', '[]', '{}', 10, 10, ?, ?, ?)",
        (NOW, NOW, NOW),
    )
    for username in cloud_users:
        connection.execute(
            "INSERT INTO cloud_users (username, password_hash, role, created_at, invited_by)"
            " VALUES (?, ?, 'user', ?, NULL)",
            (username, PASSWORD_HASH, NOW),
        )
    connection.commit()
    return connection


def _trade(trade_id: str, when: int = 3000) -> dict[str, object]:
    return {
        "tradeId": trade_id, "clientOrderId": f"cli-{trade_id}", "exchangeTradeId": "",
        "symbol": "SHFE.au2612", "direction": "BUY", "offset": "OPEN", "price": 615.0,
        "volume": 2, "tradeDateTime": when, "tradingDay": "2026-09-20",
    }


def test_fresh_database_carries_trading_account_columns(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "fresh.sqlite3")
    try:
        for table in ACCOUNT_COLUMN_TABLES:
            assert "trading_account_id" in {name for name, _ in table_columns(store.connection, table)}, table
        assert primary_key_order(store.connection, "account_trades") == ["trading_account_id", "trade_id"]
        assert primary_key_order(store.connection, "account_daily") == ["trading_account_id", "trading_day"]
        assert primary_key_order(store.connection, "ai_execution_controls") == ["user_id", "trading_account_id"]
        # #453: 一笔成交的唯一性是（用户、账户、事件）——新库直接按三列主键建。
        assert primary_key_order(store.connection, "strategy_fill_events") == [
            "user_id", "trading_account_id", "event_id"]
        assert object_sql(store.connection, "table", "trading_accounts") is not None
        assert object_sql(store.connection, "table", "account_grants") is not None
        assert object_sql(store.connection, "index", "idx_account_grants_user") is not None
        active = object_sql(store.connection, "index", "idx_ai_execution_authorizations_active")
        assert active is not None and "trading_account_id" in active
    finally:
        store.close()


def test_local_token_app_creates_default_account_for_local_user(tmp_path: Path) -> None:
    application = create_app(
        tmp_path / "local.sqlite3", local_token="token", agent_mode="fake",
        data_mode="fake", data_provider=FakeReadOnlyDataProvider(), agent_provider="deepseek",
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    try:
        store = application.state.store
        account = store.get_trading_account(DEFAULT_ACCOUNT)
        assert account is not None
        assert account["environment"] == "simulation"
        assert account["status"] == "active"
        assert account["execution_owner_user_id"] == "local-user"
        assert account["provider_kind"] == "fake"
        assert account["display_name"] == "假数据模拟账户"
        grants = store.list_account_grants(user_id="local-user")
        assert [(g["account_id"], g["role"]) for g in grants] == [(DEFAULT_ACCOUNT, "owner")]
    finally:
        application.state.store.close()


def test_owner_password_app_makes_owner_the_execution_owner(tmp_path: Path) -> None:
    application = create_app(
        tmp_path / "owner.sqlite3",
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
        data_mode="tqkq-sidecar",
        data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek",
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    try:
        store = application.state.store
        account = store.get_trading_account(DEFAULT_ACCOUNT)
        assert account is not None
        assert account["execution_owner_user_id"] == scoped_owner_id("charles")
        assert account["provider_kind"] == "tqkq-sidecar"
        assert account["display_name"] == "TqKq 模拟账户"
        assert [(g["account_id"], g["role"]) for g in store.list_account_grants(user_id=scoped_owner_id("charles"))] == [
            (DEFAULT_ACCOUNT, "owner")
        ]
    finally:
        application.state.store.close()


def test_legacy_database_backfills_every_row_to_the_default_account(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = _create_pre_41_database(path)
    connection.close()
    store = RuntimeStore(path)
    try:
        for table in ACCOUNT_COLUMN_TABLES:
            assert "trading_account_id" in {name for name, _ in table_columns(store.connection, table)}, table
        assert primary_key_order(store.connection, "account_trades") == ["trading_account_id", "trade_id"]
        assert primary_key_order(store.connection, "account_daily") == ["trading_account_id", "trading_day"]
        assert primary_key_order(store.connection, "ai_execution_controls") == ["user_id", "trading_account_id"]
        for table in ("account_trades", "account_daily", "sessions", "proposals",
                      "ai_execution_controls", "ai_execution_authorizations"):
            rows = store.connection.execute(f"SELECT trading_account_id FROM {table}").fetchall()
            assert rows and {row[0] for row in rows} == {DEFAULT_ACCOUNT}, table
        assert [row[0] for row in store.connection.execute(
            "SELECT trade_id FROM account_trades WHERE trade_id IN ('legacy-1', 'legacy-2') ORDER BY trade_id"
        ).fetchall()] == ["legacy-1", "legacy-2"]
        # Composite key: the same fill id under another account coexists with the backfilled one.
        assert store.upsert_account_trades([_trade("legacy-1")], trading_account_id=OTHER_ACCOUNT) == 1
        rows = store.connection.execute(
            "SELECT trading_account_id FROM account_trades WHERE trade_id = 'legacy-1' ORDER BY trading_account_id"
        ).fetchall()
        assert [row[0] for row in rows] == sorted([DEFAULT_ACCOUNT, OTHER_ACCOUNT])
        assert [t["tradeId"] for t in store.list_account_trades()] == ["legacy-1", "legacy-2"]
        assert [t["tradeId"] for t in store.list_account_trades(trading_account_id=OTHER_ACCOUNT)] == ["legacy-1"]
        # The legacy day row is updated in place under the default account, not duplicated.
        store.upsert_account_daily("2026-09-18", {"equity": 200120.0})
        daily = [tuple(row) for row in store.connection.execute(
            "SELECT trading_account_id, equity FROM account_daily"
        ).fetchall()]
        assert daily == [(DEFAULT_ACCOUNT, 200120.0)]
        assert object_sql(store.connection, "index", "idx_account_trades_symbol_time") is not None
        active = object_sql(store.connection, "index", "idx_ai_execution_authorizations_active")
        assert active is not None and "trading_account_id" in active
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        legacy_leftovers = store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%_legacy'"
        ).fetchall()
        assert legacy_leftovers == []
        snapshot = all_table_sql(store.connection)
    finally:
        store.close()
    # Reopening an already-migrated database changes nothing.
    store_reopened = RuntimeStore(path)
    try:
        assert all_table_sql(store_reopened.connection) == snapshot
        accounts_by_table = {
            "account_trades": {DEFAULT_ACCOUNT, OTHER_ACCOUNT},
            "account_daily": {DEFAULT_ACCOUNT},
            "ai_execution_authorizations": {DEFAULT_ACCOUNT},
        }
        for table, expected in accounts_by_table.items():
            rows = store_reopened.connection.execute(
                f"SELECT DISTINCT trading_account_id FROM {table}"
            ).fetchall()
            assert {row[0] for row in rows} == expected, table
    finally:
        store_reopened.close()


def test_strategy_fill_events_old_key_rebuild_keeps_history_and_admits_the_second_account(
        tmp_path: Path) -> None:
    """#453：修复前的线上库形状（账户列已在、两列主键）重启即换三列主键——历史行连同
    结算状态原样保留；同一 event_id 从此能在第二个账户下各自存在；重开不再改表。"""
    path = tmp_path / "fill-key.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE strategy_fill_events (
            user_id TEXT NOT NULL, event_id TEXT NOT NULL, trade_time INTEGER NOT NULL,
            trading_day TEXT NOT NULL, client_order_id TEXT NOT NULL,
            payload_json TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0, settled_day TEXT,
            trading_account_id TEXT NOT NULL DEFAULT '{DEFAULT_ACCOUNT}',
            PRIMARY KEY(user_id, event_id)
        );
        """
    )
    connection.execute(
        "INSERT INTO strategy_fill_events (user_id, event_id, trade_time, trading_day, client_order_id,"
        " payload_json, settled, settled_day, trading_account_id)"
        " VALUES ('local-user', 'ev-1', 1000, '2026-09-18', 'cli-1', '{\"v\": 1}', 1, '2026-09-19', ?)",
        (DEFAULT_ACCOUNT,),
    )
    connection.commit()
    connection.close()
    store = RuntimeStore(path)
    try:
        assert primary_key_order(store.connection, "strategy_fill_events") == [
            "user_id", "trading_account_id", "event_id"]
        assert [tuple(row) for row in store.connection.execute(
            "SELECT user_id, event_id, payload_json, settled, settled_day, trading_account_id"
            " FROM strategy_fill_events"
        )] == [("local-user", "ev-1", '{"v": 1}', 1, "2026-09-19", DEFAULT_ACCOUNT)]
        # 同一 event_id 落到第二个账户：修复前的两列主键在这是 IntegrityError（#453 的病灶）。
        store.connection.execute(
            "INSERT INTO strategy_fill_events (user_id, event_id, trade_time, trading_day, client_order_id,"
            " payload_json, settled, settled_day, trading_account_id)"
            " VALUES ('local-user', 'ev-1', 1000, '2026-09-18', 'cli-1', '{\"v\": 1}', 0, NULL, ?)",
            (OTHER_ACCOUNT,),
        )
        store.connection.commit()
        assert store.connection.execute(
            "SELECT COUNT(*) FROM strategy_fill_events WHERE event_id = 'ev-1'").fetchone()[0] == 2
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        snapshot = all_table_sql(store.connection)
    finally:
        store.close()
    store_reopened = RuntimeStore(path)
    try:
        assert all_table_sql(store_reopened.connection) == snapshot
    finally:
        store_reopened.close()


async def test_cloud_users_get_no_auto_grants_and_registration_grants_none(tmp_path: Path) -> None:
    """2026-09-23 isolation: neither boot nor registration grants the default
    account to cloud members; only the execution owner holds it until someone
    grants explicitly."""
    path = tmp_path / "cloud.sqlite3"
    connection = _create_pre_41_database(path, cloud_users=("charles", "alice", "bob"))
    connection.close()
    application = create_app(
        path,
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
        data_mode="tqkq-sidecar",
        data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek",
        registration_enabled=True,
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    try:
        store = application.state.store
        assert [(g["account_id"], g["role"]) for g in store.list_account_grants(user_id=scoped_owner_id("charles"))] == [
            (DEFAULT_ACCOUNT, "owner")
        ]
        for username in ("alice", "bob"):
            assert [(g["account_id"], g["role"]) for g in store.list_account_grants(user_id=scoped_owner_id(username))] == []
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/auth/owner", json={"username": "charles", "password": PASSWORD},
                headers={"origin": ORIGIN},
            )
            created = await client.post("/api/auth/owner/invite-codes", headers={"origin": ORIGIN})
            assert created.status_code == 200
            invite_code = created.json()["inviteCode"]
            registered = await client.post(
                "/api/auth/owner/register",
                json={"username": "dave", "password": PASSWORD, "inviteCode": invite_code},
                headers={"origin": ORIGIN},
            )
            assert registered.status_code == 200
        assert [(g["account_id"], g["role"]) for g in store.list_account_grants(user_id=scoped_owner_id("dave"))] == []
    finally:
        application.state.store.close()
    # A second boot keeps the default account owner-only.
    again = create_app(
        path,
        agent_mode="fake",
        auth_mode="owner-password",
        owner_username="charles",
        owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET,
        cors_origins={ORIGIN},
        data_mode="tqkq-sidecar",
        data_provider=FakeReadOnlyDataProvider(),
        agent_provider="deepseek",
        registration_enabled=True,
        deepseek_api_key="sk-test-deepseek-key-000001",
    )
    try:
        store = again.state.store
        rows = store.connection.execute(
            "SELECT user_id, role FROM account_grants WHERE account_id = ? ORDER BY user_id",
            (DEFAULT_ACCOUNT,),
        ).fetchall()
        assert sorted((row[0], row[1]) for row in rows) == [
            (scoped_owner_id("charles"), "owner"),
        ]
    finally:
        again.state.store.close()


def test_grant_account_validates_roles_and_lists_grants(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "grants.sqlite3")
    try:
        store.ensure_default_trading_account(
            execution_owner_user_id="local-user", provider_kind="fake",
            display_name="假数据模拟账户", sidecar_url=None,
        )
        with pytest.raises(ValueError):
            store.grant_account(
                account_id=DEFAULT_ACCOUNT, user_id="local-user", role="admin", granted_by="local-user",
            )
        store.grant_account(
            account_id=DEFAULT_ACCOUNT, user_id="owner-x", role="viewer", granted_by="local-user",
        )
        store.grant_account(
            account_id=DEFAULT_ACCOUNT, user_id="owner-x", role="operator", granted_by="local-user",
        )
        assert [(g["account_id"], g["role"]) for g in store.list_account_grants(user_id="owner-x")] == [
            (DEFAULT_ACCOUNT, "operator")
        ]
        assert [a["id"] for a in store.list_trading_accounts()] == [DEFAULT_ACCOUNT]
    finally:
        store.close()
