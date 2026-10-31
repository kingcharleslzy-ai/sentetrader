"""我的流程 (2026-10-06): the user's named routines and the 流程卡 drafts the AI makes of them.

Every method is scoped by user_id; another user's id is simply "没有这个流程". A routine is written
only by `save_routine_draft` (the user's 保存 on a 流程卡) or `update_routine` (the user's own edit);
the limits are checked inside the same transaction as the write."""
from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from .base import StoreBase, utc_now


class RoutineLimitError(ValueError):
    """The user already has the maximum number of routines."""


class RoutineNameTaken(ValueError):
    """Another routine of this user already has the name."""


class RoutineDraftUsed(ValueError):
    """The draft was saved already (a double click, a second window)."""


def _routine(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "steps": json.loads(row["steps_json"]),
            "createdAt": row["created_at"], "updatedAt": row["updated_at"], "lastRunAt": row["last_run_at"]}


def _draft(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "routineId": row["routine_id"], "name": row["name"], "steps": json.loads(row["steps_json"]),
            "sessionId": row["session_id"], "turnId": row["turn_id"], "refusal": row["refusal"],
            "createdAt": row["created_at"], "savedAt": row["saved_at"]}


class RoutineStore(StoreBase):

    def list_routines(self, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM user_routines WHERE user_id = ? ORDER BY created_at, id", (user_id,)).fetchall()
        return [_routine(row) for row in rows]

    def get_routine(self, user_id: str, routine_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM user_routines WHERE user_id = ? AND id = ?", (user_id, routine_id)).fetchone()
        return _routine(row) if row is not None else None

    def routine_names(self, user_id: str) -> dict[str, str]:
        """name → id, for the exact-name trigger (read on every message, so one narrow query)."""
        with self.lock:
            return {row["name"]: row["id"] for row in self.connection.execute(
                "SELECT id, name FROM user_routines WHERE user_id = ?", (user_id,))}

    def _write_routine(self, connection: sqlite3.Connection, user_id: str, *, routine_id: str | None, name: str,
                       steps: list[dict[str, str]], limit: int) -> str:
        now = utc_now()
        encoded = json.dumps(steps, ensure_ascii=False)
        clash = connection.execute("SELECT id FROM user_routines WHERE user_id = ? AND name = ?", (user_id, name)).fetchone()
        if clash is not None and clash["id"] != routine_id:
            raise RoutineNameTaken(name)
        if routine_id is None:
            count = connection.execute("SELECT COUNT(*) FROM user_routines WHERE user_id = ?", (user_id,)).fetchone()[0]
            if count >= limit:
                raise RoutineLimitError(str(limit))
            routine_id = "rt-" + uuid4().hex[:12]
            connection.execute(
                "INSERT INTO user_routines (user_id, id, name, steps_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, routine_id, name, encoded, now, now))
            return routine_id
        changed = connection.execute(
            "UPDATE user_routines SET name = ?, steps_json = ?, updated_at = ? WHERE user_id = ? AND id = ?",
            (name, encoded, now, user_id, routine_id)).rowcount
        if changed != 1:
            raise KeyError(routine_id)
        return routine_id

    def update_routine(self, user_id: str, routine_id: str, *, name: str, steps: list[dict[str, str]]) -> dict[str, Any]:
        """The user's own rename / step edit (KeyError when there is no such routine)."""
        with self.lock, self.connection:
            self._write_routine(self.connection, user_id, routine_id=routine_id, name=name, steps=steps, limit=0)
        routine = self.get_routine(user_id, routine_id)
        assert routine is not None
        return routine

    def delete_routine(self, user_id: str, routine_id: str) -> bool:
        with self.lock, self.connection:
            return self.connection.execute(
                "DELETE FROM user_routines WHERE user_id = ? AND id = ?", (user_id, routine_id)).rowcount == 1

    def mark_routine_run(self, user_id: str, routine_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("UPDATE user_routines SET last_run_at = ? WHERE user_id = ? AND id = ?",
                                    (utc_now(), user_id, routine_id))

    # -- 流程卡 drafts --------------------------------------------------------

    def insert_routine_draft(self, user_id: str, *, routine_id: str | None, name: str, steps: list[dict[str, str]],
                             session_id: str | None, turn_id: str | None, refusal: str | None) -> dict[str, Any]:
        draft_id = "rd-" + uuid4().hex[:12]
        now = utc_now()
        with self.lock, self.connection:
            # A draft is only worth keeping while its card can still be saved: a week is plenty.
            self.connection.execute("DELETE FROM routine_drafts WHERE user_id = ? AND created_at < ?",
                                    (user_id, _days_before(now, 7)))
            self.connection.execute(
                "INSERT INTO routine_drafts (user_id, id, routine_id, name, steps_json, session_id, turn_id, refusal, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, draft_id, routine_id, name, json.dumps(steps, ensure_ascii=False), session_id, turn_id,
                 refusal, now))
        draft = self.get_routine_draft(user_id, draft_id)
        assert draft is not None
        return draft

    def get_routine_draft(self, user_id: str, draft_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM routine_drafts WHERE user_id = ? AND id = ?", (user_id, draft_id)).fetchone()
        return _draft(row) if row is not None else None

    def session_routine_drafts(self, user_id: str, session_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """The 流程卡 this conversation showed, newest first (the model's note of what became of them)."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM routine_drafts WHERE user_id = ? AND session_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, session_id, limit)).fetchall()
        return [_draft(row) for row in rows]

    def save_routine_draft(self, user_id: str, draft_id: str, *, limit: int) -> dict[str, Any]:
        """Write the draft as a routine (new, or over its `routine_id`) and mark it saved, in one
        transaction: a second 保存 of the same card raises RoutineDraftUsed and writes nothing."""
        with self.lock, self.connection:
            row = self.connection.execute(
                "SELECT * FROM routine_drafts WHERE user_id = ? AND id = ?", (user_id, draft_id)).fetchone()
            if row is None:
                raise KeyError(draft_id)
            if row["saved_at"] is not None:
                raise RoutineDraftUsed(draft_id)
            routine_id = self._write_routine(self.connection, user_id, routine_id=row["routine_id"], name=row["name"],
                                             steps=json.loads(row["steps_json"]), limit=limit)
            self.connection.execute("UPDATE routine_drafts SET saved_at = ? WHERE user_id = ? AND id = ?",
                                    (utc_now(), user_id, draft_id))
        routine = self.get_routine(user_id, routine_id)
        assert routine is not None
        return routine


def _days_before(stamp: str, days: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(stamp) - timedelta(days=days)).isoformat()
