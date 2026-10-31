"""规则回测结果 (策略页改版第八批, 2026-10-08): the recent rule-backtest results, kept in SQLite.

Before this batch the results lived only in the Runtime's memory (5 per user) and a restart lost
them. A row is one result: who ran it, the library strategy it belongs to (``strategy_id`` NULL for
an unsaved plan or a template preview), its summary (the strategy cards read only that) and the
whole payload, its trade list trimmed to the newest ``TRADES_PERSISTED`` (the full list goes back to
the caller that ran it). Each owner keeps its newest ``KEEP`` rows; older ones are deleted on save.
Rows are per user: another user's result reads as missing.
"""
from __future__ import annotations

import json
from typing import Any

from .base import StoreBase

KEEP = 5                  # newest results kept per (user, strategy) — and per user for unsaved plans
TRADES_PERSISTED = 300    # trade rows kept in a stored payload (the summary covers every trade)


def _payload(row: Any) -> dict[str, Any]:
    return json.loads(row["payload_json"])


class StrategyBacktestStore(StoreBase):
    def save_backtest_result(self, *, user_id: str, result: dict[str, Any], keep: int = KEEP) -> None:
        """Store one result (``result["backtestId"]`` is the row id) and trim the owner's older rows."""
        strategy_id = result.get("strategyId")
        trades = result.get("trades") or []
        stored = {**result, "trades": trades[-TRADES_PERSISTED:],
                  "tradesTruncated": bool(result.get("tradesTruncated")) or len(trades) > TRADES_PERSISTED}
        text = json.dumps(stored, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)
        summary = json.dumps({**(result.get("summary") or {}), "coverage": result.get("coverage"),
                              "createdAt": result.get("createdAt"), "symbol": result.get("symbol"),
                              "timeframeSeconds": result.get("timeframeSeconds")}, ensure_ascii=False, default=str)
        owner = "strategy_id = ?" if strategy_id else "strategy_id IS NULL"
        owner_args: tuple[Any, ...] = (user_id, strategy_id) if strategy_id else (user_id,)
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO strategy_backtest_results
                   (id, user_id, strategy_id, created_at, summary_json, payload_json) VALUES (?, ?, ?, ?, ?, ?)""",
                (str(result["backtestId"]), user_id, strategy_id, str(result.get("createdAt") or ""), summary, text))
            self.connection.execute(
                f"""DELETE FROM strategy_backtest_results WHERE user_id = ? AND {owner} AND id NOT IN (
                    SELECT id FROM strategy_backtest_results WHERE user_id = ? AND {owner}
                    ORDER BY created_at DESC, rowid DESC LIMIT ?)""",
                (*owner_args, *owner_args, keep))

    def list_backtest_results(self, *, user_id: str, strategy_id: str | None = None,
                              limit: int = KEEP) -> list[dict[str, Any]]:
        """The user's newest results (one strategy's when ``strategy_id`` is given), newest first."""
        where, args = ("user_id = ? AND strategy_id = ?", (user_id, strategy_id)) if strategy_id \
            else ("user_id = ?", (user_id,))
        with self.lock:
            rows = self.connection.execute(
                f"SELECT payload_json FROM strategy_backtest_results WHERE {where}"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
        return [_payload(row) for row in rows]

    def get_backtest_result(self, backtest_id: str, *, user_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload_json FROM strategy_backtest_results WHERE id = ? AND user_id = ?",
                (backtest_id, user_id)).fetchone()
        if row is None:
            raise KeyError(backtest_id)
        return _payload(row)

    def update_backtest_result(self, backtest_id: str, *, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Merge top-level fields into a stored payload (the verdict sentence once it is written)."""
        with self.lock, self.connection:
            row = self.connection.execute(
                "SELECT payload_json FROM strategy_backtest_results WHERE id = ? AND user_id = ?",
                (backtest_id, user_id)).fetchone()
            if row is None:
                raise KeyError(backtest_id)
            payload = {**_payload(row), **changes}
            self.connection.execute(
                "UPDATE strategy_backtest_results SET payload_json = ? WHERE id = ? AND user_id = ?",
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str),
                 backtest_id, user_id))
        return payload

    def latest_backtest_summaries(self, *, user_id: str) -> dict[str, dict[str, Any]]:
        """strategyId → its newest result's summary (策略卡的“最近回测”), one query for the whole list."""
        with self.lock:
            rows = self.connection.execute(
                """SELECT r.strategy_id, r.id, r.summary_json FROM strategy_backtest_results r
                   JOIN (SELECT strategy_id, MAX(created_at) AS newest FROM strategy_backtest_results
                         WHERE user_id = ? AND strategy_id IS NOT NULL GROUP BY strategy_id) latest
                     ON latest.strategy_id = r.strategy_id AND latest.newest = r.created_at
                   WHERE r.user_id = ?""", (user_id, user_id)).fetchall()
        return {row["strategy_id"]: {**json.loads(row["summary_json"]), "backtestId": row["id"]} for row in rows}
