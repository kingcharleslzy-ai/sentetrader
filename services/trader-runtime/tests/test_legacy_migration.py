from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trader_runtime.store import RuntimeStore


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
