"""用户画线 rows: per user × contract — a drawing follows the user across devices, so it lives
server-side and knows neither the trading account nor the chart period (画线第二版: its `style`
may name the period it was drawn on, for 在所有周期显示 off)."""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .base import StoreBase, utc_now


def drawing_view(row: Any) -> dict[str, Any]:
    """One drawing in the shape the routes, the tools and the client read."""
    def read(column: str) -> dict[str, Any]:
        try:
            value = json.loads(row[column])
        except (TypeError, ValueError, IndexError, KeyError):
            return {}
        return value if isinstance(value, dict) else {}
    return {"id": row["id"], "symbol": row["symbol"], "kind": row["kind"],
            "points": read("points"), "label": row["label"], "style": read("style"),
            "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


class ChartDrawingStore(StoreBase):
    """Create / list / update / delete, every row scoped to its user. The per-contract cap is
    checked and written inside one locked transaction, like the alerts'."""

    def create_chart_drawing(self, *, user_id: str, symbol: str, kind: str,
                             points: dict[str, Any], label: str,
                             max_per_symbol: int, style: dict[str, Any] | None = None) -> dict[str, Any] | str:
        """Insert one drawing. Returns the row view, or the Chinese refusal reason (the
        per-user-and-contract cap) — the check and the insert share one locked transaction."""
        drawing_id, now = uuid4().hex, utc_now()
        with self.lock, self.connection:
            held = self.connection.execute(
                "SELECT COUNT(*) FROM chart_drawings WHERE user_id = ? AND symbol = ?",
                (user_id, symbol)).fetchone()[0]
            if held >= max_per_symbol:
                return f"这个合约图上已有 {held} 条线，达到上限 {max_per_symbol} 条；先删掉一条再画"
            self.connection.execute(
                """INSERT INTO chart_drawings (id, user_id, symbol, kind, points, label, style, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (drawing_id, user_id, symbol, kind, json.dumps(points, ensure_ascii=False), label,
                 json.dumps(style or {}, ensure_ascii=False), now, now))
        row = self.connection.execute("SELECT * FROM chart_drawings WHERE id = ?", (drawing_id,)).fetchone()
        return drawing_view(row)

    def list_chart_drawings(self, *, user_id: str, symbol: str | None = None) -> list[dict[str, Any]]:
        """The user's drawings, drawing order (oldest first) so the chart stacks them as drawn;
        one contract's, or every contract's grouped by symbol when `symbol` is None."""
        with self.lock:
            if symbol is None:
                rows = self.connection.execute(
                    "SELECT * FROM chart_drawings WHERE user_id = ? ORDER BY symbol, created_at, id",
                    (user_id,)).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM chart_drawings WHERE user_id = ? AND symbol = ? ORDER BY created_at, id",
                    (user_id, symbol)).fetchall()
        return [drawing_view(row) for row in rows]

    def get_chart_drawing(self, *, user_id: str, drawing_id: str) -> dict[str, Any] | None:
        """One of the user's drawings, or None (the caller's 404)."""
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM chart_drawings WHERE id = ? AND user_id = ?", (drawing_id, user_id)).fetchone()
        return None if row is None else drawing_view(row)

    def update_chart_drawing(self, *, user_id: str, drawing_id: str,
                             points: dict[str, Any] | None = None,
                             label: str | None = None,
                             style: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Points, label and/or style (the whole checked style — the route merged the patch);
        None (the caller's 404) when it is not the user's drawing."""
        with self.lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM chart_drawings WHERE id = ? AND user_id = ?", (drawing_id, user_id)).fetchone()
            if row is None:
                return None
            self.connection.execute(
                """UPDATE chart_drawings SET points = ?, label = ?, style = ?, updated_at = ? WHERE id = ?""",
                (json.dumps(points, ensure_ascii=False) if points is not None else row["points"],
                 row["label"] if label is None else label,
                 json.dumps(style, ensure_ascii=False) if style is not None else row["style"],
                 utc_now(), drawing_id))
        updated = self.connection.execute("SELECT * FROM chart_drawings WHERE id = ?", (drawing_id,)).fetchone()
        return drawing_view(updated)

    def delete_chart_drawing(self, *, user_id: str, drawing_id: str) -> dict[str, Any] | None:
        """Delete one; None when it is not the user's drawing (a stranger's row is a 404)."""
        with self.lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM chart_drawings WHERE id = ? AND user_id = ?", (drawing_id, user_id)).fetchone()
            if row is None:
                return None
            self.connection.execute("DELETE FROM chart_drawings WHERE id = ?", (drawing_id,))
            # A line's 碰到就提醒 goes with the line (改版 2026-10-31).
            self.connection.execute(
                "UPDATE price_alerts SET status = 'cancelled' WHERE user_id = ? AND drawing_id = ? AND status = 'active'",
                (user_id, drawing_id))
        return drawing_view(row)

    def clear_chart_drawings(self, *, user_id: str, symbol: str) -> int:
        """Delete every drawing of the user's on one contract (and the alerts watching them); returns how many went."""
        with self.lock, self.connection:
            self.connection.execute(
                """UPDATE price_alerts SET status = 'cancelled' WHERE user_id = ? AND status = 'active' AND drawing_id IN
                (SELECT id FROM chart_drawings WHERE user_id = ? AND symbol = ?)""", (user_id, user_id, symbol))
            cursor = self.connection.execute(
                "DELETE FROM chart_drawings WHERE user_id = ? AND symbol = ?", (user_id, symbol))
        return cursor.rowcount
