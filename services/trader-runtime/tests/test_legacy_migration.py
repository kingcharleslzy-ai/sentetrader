from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trader_runtime.main import _migrate_legacy_database
from trader_runtime.store import RuntimeStore

TOKEN = "phase13-review-token"
ORIGIN = "http://127.0.0.1:5173"


def _create_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mode TEXT NOT NULL,
          agent_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, expires_at TEXT NOT NULL, codex_thread_id TEXT);
        CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
          session_id TEXT NOT NULL, schema_version INTEGER NOT NULL, kind TEXT NOT NULL,
          name TEXT NOT NULL, timestamp TEXT NOT NULL, payload_json TEXT NOT NULL,
          FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);
        CREATE TABLE outbox_events (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
          event_json TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT);
        CREATE TABLE agent_runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_id TEXT NOT NULL,
          status TEXT NOT NULL, instruction TEXT NOT NULL, result_json TEXT, error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);
        CREATE TABLE artifacts (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT,
          artifact_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
          FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE SET NULL);
        CREATE TABLE proposals (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
          proposal_type TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          user_id TEXT NOT NULL DEFAULT 'local-user', version INTEGER NOT NULL DEFAULT 1,
          approval_id TEXT, approval_expires_at TEXT, approval_used_at TEXT);
        CREATE TABLE proposal_audit (id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
          action TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE idempotency_keys (user_id TEXT NOT NULL, key TEXT NOT NULL,
          action TEXT NOT NULL, proposal_id TEXT NOT NULL, response_json TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(user_id, key));
        CREATE TABLE codex_approvals (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
          user_id TEXT NOT NULL, run_id TEXT, request_id_json TEXT NOT NULL,
          approval_type TEXT NOT NULL, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
          item_id TEXT NOT NULL, status TEXT NOT NULL, decision TEXT, payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL, resolved_at TEXT);
    """)
    now = "2026-08-25T12:00:00+00:00"
    connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       ("legacy-session", "local-user", "local", "codex", "active",
                        now, now, "2099-01-01T00:00:00+00:00", "legacy-thread"))
    connection.execute("INSERT INTO events VALUES (NULL, ?, ?, 1, 'message', 'message.completed', ?, ?)",
                       ("legacy-event", "legacy-session", now,
                        '{"turnId":"legacy-turn","role":"assistant","text":"保留的对话"}'))
    connection.execute("INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       ("legacy-run", "legacy-session", "local-user", "completed", "历史任务",
                        '{"ok":true}', None, now, now))
    connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)",
                       ("legacy-artifact", "legacy-session", "legacy-run", "codex.diff",
                        '{"changes":[]}', now))
    connection.execute(
        """INSERT INTO proposals
        (id, session_id, proposal_type, status, payload_json, created_at, updated_at,
         user_id, version, approval_id, approval_expires_at, approval_used_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("legacy-proposal", "legacy-session", "trade", "rejected",
         '{"proposalId":"legacy-proposal","status":"rejected"}', now, now,
         "local-user", 1, "legacy-approval", now, now),
    )
    connection.execute("INSERT INTO proposal_audit VALUES (?, ?, ?, ?, ?)",
                       ("legacy-audit", "legacy-proposal", "rejected", '{"ok":true}', now))
    connection.execute("INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?, ?)",
                       ("local-user", "legacy-idempotency", "reject", "legacy-proposal",
                        '{"status":"rejected"}', now))
    connection.execute(
        "INSERT INTO codex_approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-codex-approval", "legacy-session", "local-user", "legacy-run", '"request"',
         "command", "legacy-thread", "legacy-turn", "legacy-item", "resolved", "accept",
         '{}', now, now, now),
    )
    connection.commit()
    connection.close()


def test_legacy_default_database_migrates_atomically_without_data_loss(tmp_path: Path) -> None:
    source = tmp_path / "legacy" / "ai-trader.sqlite3"
    source.parent.mkdir()
    _create_legacy_database(source)
    before = source.read_bytes()
    destination = tmp_path / "Application Support" / "AI Trader" / "ai-trader.sqlite3"
    assert _migrate_legacy_database(source, destination) is True
    assert source.read_bytes() == before
    store = RuntimeStore(destination)
    assert store.get_session("legacy-session")["codexThreadId"] == "legacy-thread"
    assert store.list_events("legacy-session")[0][1]["payload"]["text"] == "保留的对话"
    assert store.get_run("legacy-run")["instruction"] == "历史任务"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM artifacts WHERE id = 'legacy-artifact'"
    ).fetchone()[0] == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM proposals WHERE id = 'legacy-proposal'"
    ).fetchone()[0] == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM proposal_audit WHERE id = 'legacy-audit'"
    ).fetchone()[0] == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE key = 'legacy-idempotency'"
    ).fetchone()[0] == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM codex_approvals WHERE id = 'legacy-codex-approval'"
    ).fetchone()[0] == 1
    marker = store.connection.execute(
        "SELECT source_retained FROM ai_trader_runtime_migrations WHERE migration_id = ?",
        ("phase-1.2-default-database",),
    ).fetchone()
    assert marker[0] == 1
    assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.close()


def test_account_trades_gains_columns_added_after_the_table_existed(tmp_path: Path) -> None:
    """A database made before `volume_multiple` joined the CREATE never got the column, so every
    /api/trade-review read raised "no such column" (2026-09-19). Opening the store must add it."""
    path = tmp_path / "old-account-trades.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE account_trades (
            trade_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL DEFAULT '',
            exchange_trade_id TEXT NOT NULL DEFAULT '', symbol TEXT NOT NULL,
            direction TEXT NOT NULL, offset TEXT NOT NULL, price REAL,
            volume INTEGER NOT NULL, trade_date_time INTEGER NOT NULL,
            trading_day TEXT, captured_at TEXT NOT NULL)"""
    )
    connection.execute(
        "INSERT INTO account_trades VALUES ('t1', 'o1', 'x1', 'SHFE.au2612', 'BUY', 'OPEN',"
        " 820.0, 1, 1788192000000000000, '2026-09-19', '2026-09-19T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    store = RuntimeStore(path)
    assert "volume_multiple" in {row["name"] for row in store.connection.execute("PRAGMA table_info(account_trades)")}
    rows = store.list_account_trades()
    assert [row["tradeId"] for row in rows] == ["t1"]
    assert rows[0]["volumeMultiple"] is None
    store.close()


def test_legacy_migration_merges_with_an_already_created_database(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    _create_legacy_database(source)
    destination = tmp_path / "new-state" / "ai-trader.sqlite3"
    current = RuntimeStore(destination)
    current.create_session(user_id="local-user", mode="local", agent_id="codex")
    current_session_count = len(current.active_sessions())
    current.close()
    assert _migrate_legacy_database(source, destination) is True
    merged = RuntimeStore(destination)
    assert len(merged.active_sessions()) == current_session_count + 1
    assert merged.get_session("legacy-session")["codexThreadId"] == "legacy-thread"
    assert _migrate_legacy_database(source, destination) is False
    merged.close()


def test_add_column_tolerates_a_concurrent_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """525-C1：两个进程同时迁移旧库时，PRAGMA 读列到 ALTER 之间另一进程可能已把
    列加上——撞上的 duplicate column 说明目标状态已达成，不该让启动失败。"""
    store = RuntimeStore(tmp_path / "add-column-race.sqlite3")
    try:
        # 正常路径：列已存在，PRAGMA 跳过，不执行 ALTER。
        store._add_column("account_trades", "volume_multiple REAL")
        # 竞态形态：PRAGMA 读不到列（另一进程刚加完），ALTER 撞上 duplicate。
        monkeypatch.setattr(store, "_columns", lambda table: set())
        store._add_column("account_trades", "volume_multiple REAL")
        columns = {row["name"] for row in store.connection.execute(
            "PRAGMA table_info(account_trades)")}
        assert "volume_multiple" in columns
    finally:
        store.close()


def test_short_release_ids_rewrite_backtest_references_too(tmp_path: Path) -> None:
    """525-C2：旧开发库的短 release ID 改写为 canonical 时，backtest_runs 里的
    引用一起改写——否则升级时外键在提交处失败，启动直接中断。"""
    path = tmp_path / "short-release.sqlite3"
    digest = "a" * 64
    store = RuntimeStore(path)
    store.connection.execute(
        """INSERT INTO strategy_releases (id, artifact_digest, source_ref, manifest_json, created_at)
        VALUES ('dev-short-1', ?, 'draft-1', '{"releaseId": "dev-short-1"}',
        '2026-09-01T00:00:00+00:00')""", (digest,))
    store.connection.execute(
        """INSERT INTO backtest_runs (id, session_id, user_id, status, artifact_digest,
        strategy_release_id, data_digest, parameters_json, manifest_json, artifact_path,
        created_at, updated_at)
        VALUES ('run-1', 'session-1', 'local-user', 'completed', ?, 'dev-short-1', ?, '{}', '{}',
        'runs/run-1.parquet', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')""",
        ("b" * 64, "c" * 64))
    store.connection.commit()
    store.close()

    reopened = RuntimeStore(path)  # 修复前：提交时外键失败，这里打不开
    try:
        canonical = f"sha256-{digest}"
        assert [row[0] for row in reopened.connection.execute(
            "SELECT id FROM strategy_releases")] == [canonical]
        assert [row[0] for row in reopened.connection.execute(
            "SELECT strategy_release_id FROM backtest_runs")] == [canonical]
        manifest = json.loads(reopened.connection.execute(
            "SELECT manifest_json FROM strategy_releases").fetchone()[0])
        assert manifest["releaseId"] == canonical
        assert reopened.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()
