"""策略实验室 (2026-09-30): one comparison — up to 12 strategy variants on one contract.

A lab row holds what was asked (``request_json``), the resolved variants (``variants_json``, each
with its executable plan), the per-variant results as they arrive (``results_json``, also the
run-server progress), the server-recomputed ranking (``ranking_json``) and the variant the user
kept (``chosen_json``). ``source`` says where the results were computed: the user's browser
(Pyodide) or the cloud fallback. Rows are per user; another user's lab is a 404, never a 403.
"""
from __future__ import annotations

import json
from typing import Any

from .base import StoreBase, utc_now


def _row_to_lab(row: Any) -> dict[str, Any]:
    lab = dict(row)
    lab["userId"] = lab.pop("user_id")
    lab["createdAt"], lab["updatedAt"] = lab.pop("created_at"), lab.pop("updated_at")
    lab["request"] = json.loads(lab.pop("request_json") or "{}")
    lab["variants"] = json.loads(lab.pop("variants_json") or "[]")
    lab["results"] = json.loads(lab.pop("results_json") or "null")
    lab["ranking"] = json.loads(lab.pop("ranking_json") or "null")
    lab["chosen"] = json.loads(lab.pop("chosen_json") or "null")
    lab["skipped"] = json.loads(lab.pop("skipped_json") or "null")
    return lab


class StrategyLabStore(StoreBase):
    def save_lab(self, *, lab_id: str, user_id: str, symbol: str, request: dict[str, Any],
                 variants: list[dict[str, Any]], skipped: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_labs (id, user_id, symbol, status, request_json, variants_json,
                   skipped_json, created_at, updated_at) VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
                (lab_id, user_id, symbol, json.dumps(request, ensure_ascii=False),
                 json.dumps(variants, ensure_ascii=False), json.dumps(skipped or [], ensure_ascii=False),
                 now, now),
            )
        return self.get_lab(lab_id, user_id=user_id)

    def get_lab(self, lab_id: str, *, user_id: str) -> dict[str, Any]:
        """The lab, only when it is this user's (another user's lab reads as missing)."""
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM strategy_labs WHERE id = ? AND user_id = ?", (lab_id, user_id)
            ).fetchone()
        if row is None:
            raise KeyError(lab_id)
        return _row_to_lab(row)

    def list_labs(self, *, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM strategy_labs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [_row_to_lab(row) for row in rows]

    def latest_lab_for_strategy(self, *, user_id: str, strategy_id: str) -> dict[str, Any] | None:
        """The newest 比一比 lab of this strategy — filtered before it is limited, so other labs the
        user ran since cannot push an older strategy's comparison out of a recent-N window."""
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM strategy_labs WHERE user_id = ? AND json_extract(request_json, '$.strategyId') = ? "
                "ORDER BY created_at DESC LIMIT 1", (user_id, strategy_id),
            ).fetchone()
        return _row_to_lab(row) if row is not None else None

    def update_lab(self, lab_id: str, *, user_id: str, status: str | None = None,
                   results: list[dict[str, Any]] | None = None,
                   ranking: dict[str, Any] | None = None, chosen: dict[str, Any] | None = None,
                   source: str | None = None, reset_source: bool = False,
                   request: dict[str, Any] | None = None) -> dict[str, Any]:
        """Patch the mutable columns; ``None`` leaves that column alone. ``reset_source`` clears
        the source (a fresh run starts over); pass ``results=[]`` / ``ranking={}`` to clear those.
        ``request`` replaces what was asked (the cloud fallback records why it ran there)."""
        assignments: list[str] = []
        values: list[Any] = []
        if request is not None:
            assignments.append("request_json = ?")
            values.append(json.dumps(request, ensure_ascii=False))
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if results is not None:
            assignments.append("results_json = ?")
            values.append(json.dumps(results, ensure_ascii=False))
        if ranking is not None:
            assignments.append("ranking_json = ?")
            values.append(json.dumps(ranking, ensure_ascii=False))
        if chosen is not None:
            assignments.append("chosen_json = ?")
            values.append(json.dumps(chosen, ensure_ascii=False))
        if reset_source:
            assignments.append("source = NULL")
        elif source is not None:
            assignments.append("source = ?")
            values.append(source)
        assignments.append("updated_at = ?")
        values.extend([utc_now(), lab_id, user_id])
        with self.lock, self.connection:
            cursor = self.connection.execute(
                f"UPDATE strategy_labs SET {', '.join(assignments)} WHERE id = ? AND user_id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise KeyError(lab_id)
        return self.get_lab(lab_id, user_id=user_id)
