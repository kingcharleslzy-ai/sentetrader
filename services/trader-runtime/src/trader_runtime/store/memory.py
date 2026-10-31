"""Per-user memory facts and audited mutations, including guarded consolidation and undo."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .base import StoreBase, utc_now

_MEMORY_COLUMNS = ("id", "user_id", "layer", "text", "source", "evidence_json", "tags_json",
                   "status", "created_at", "updated_at", "invalid_at", "invalid_reason",
                   "revision", "state_revision")
_MEMORY_SELECT = ("SELECT " + ", ".join("m." + column for column in _MEMORY_COLUMNS)
                  + ", c.id AS last_change_id, c.after_json AS last_after_json FROM user_memories m"
                  " LEFT JOIN user_memory_changes c ON c.rowid = (SELECT MAX(h.rowid)"
                  " FROM user_memory_changes h WHERE h.user_id = m.user_id AND h.memory_id = m.id)")


_CHINA = ZoneInfo("Asia/Shanghai")  # the consolidation day key is a Beijing date


class MemoryConflict(Exception):
    """The observed target or undo's causal state no longer belongs to this operation."""


def _memory_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "userId": row["user_id"], "layer": row["layer"], "text": row["text"],
        "source": row["source"], "evidence": json.loads(row["evidence_json"]),
        "tags": json.loads(row["tags_json"]), "status": row["status"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "invalidAt": row["invalid_at"], "invalidReason": row["invalid_reason"],
        "revision": row["revision"] or "legacy:" + row["updated_at"],
        "stateRevision": row["state_revision"],
    }


def _same_legacy_snapshot(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Equality only checks audit continuity; the audit row identity, never text/time,
    # determines the legacy causal token (including same-clock A -> B -> A).
    keys = ("id", "userId", "layer", "text", "source", "evidence", "tags", "status",
            "createdAt", "updatedAt", "invalidAt", "invalidReason")
    return all(left.get(key) == right.get(key) for key in keys)


class MemoryStore(StoreBase):
    """MemoryStore is the sole write/undo boundary; caller-owned transactions stay open."""

    @contextmanager
    def _memory_write(self, connection: sqlite3.Connection | None = None):
        if connection is not None:
            yield connection
        elif self.connection.owner_thread == threading.get_ident():
            yield self.connection
        else:
            with self.transaction() as owned:
                yield owned

    def _memory_item(self, row: Any) -> dict[str, Any]:
        item = _memory_row(row)
        last = json.loads(row["last_after_json"]) if row["last_after_json"] else None
        matches = last is not None and _same_legacy_snapshot(last, item)
        if matches and last is not None and row["revision"] and last.get("revision") == row["revision"] and last.get("stateRevision") == row["state_revision"]:
            return item
        # A rollback Runtime can change facts/audit without advancing the new columns.
        # Bind the observed version to both facts and the latest causal audit identity.
        observed = json.dumps([row["revision"], row["last_change_id"], item],
                              ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        item["revision"] = "observed:" + hashlib.sha256(observed.encode()).hexdigest()
        if matches:
            item["stateRevision"] = "legacy-change:" + row["last_change_id"]
        else:
            item["stateRevision"] = "untracked:" + item["revision"]
        return item

    @staticmethod
    def _get_memory_row(memory_id: str, user_id: str, connection: sqlite3.Connection):
        return connection.execute(_MEMORY_SELECT + " WHERE m.id = ? AND m.user_id = ?",
                                  (memory_id, user_id)).fetchone()

    def _write_change(self, user_id: str, memory_id: str, op: str, actor: str,
                      before: dict[str, Any] | None, after: dict[str, Any] | None, *,
                      connection: sqlite3.Connection | None = None, undo_of: str | None = None) -> dict[str, Any]:
        change = {"id": uuid4().hex, "memoryId": memory_id, "op": op, "actor": actor, "at": utc_now(),
                  "before": before, "after": after, "undoOf": undo_of}
        target = connection if connection is not None else self.connection
        target.execute(
            "INSERT INTO user_memory_changes (id, user_id, memory_id, op, before_json, after_json, actor, at, undo_of)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (change["id"], user_id, memory_id, op,
             json.dumps(before, ensure_ascii=False) if before is not None else None,
             json.dumps(after, ensure_ascii=False) if after is not None else None,
             actor, change["at"], undo_of))
        return change

    def insert_memory(self, user_id: str, layer: str, text: str, source: str,
                      evidence: dict[str, Any], tags: list[str], *, actor: str | None = None,
                      connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self._memory_write(connection) as target:
            memory_id, at, revision = uuid4().hex, utc_now(), uuid4().hex
            target.execute(
                "INSERT INTO user_memories (" + ", ".join(_MEMORY_COLUMNS) + ")"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL, ?, ?)",
                (memory_id, user_id, layer, text, source, json.dumps(evidence, ensure_ascii=False),
                 json.dumps(tags, ensure_ascii=False), at, at, revision, revision))
            row = self._get_memory_row(memory_id, user_id, target)
            after = _memory_row(row) if actor is not None else self._memory_item(row)
            if actor is not None:
                self._write_change(user_id, memory_id, "add", actor, None, after, connection=target)
            return after

    def get_memory(self, memory_id: str, *, user_id: str,
                   connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        target = connection if connection is not None else self.connection
        row = self._get_memory_row(memory_id, user_id, target)
        return self._memory_item(row) if row is not None else None

    def _mutate_memory(self, memory_id: str, user_id: str, assignments: dict[str, Any], *,
                       op: str, actor: str | None, expected_revision: str | None = None,
                       state_revision: str | None = None, undo_of: str | None = None,
                       connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        with self._memory_write(connection) as target:
            row = self._get_memory_row(memory_id, user_id, target)
            if row is None:
                if expected_revision is not None:
                    raise MemoryConflict("目标记忆已更新")
                return None
            before = self._memory_item(row)
            if expected_revision is not None and (before["revision"] != expected_revision
                    or before["status"] != "active" or before["source"] != "user"):
                raise MemoryConflict("目标记忆已更新")
            revision = uuid4().hex
            values = {**assignments, "updated_at": utc_now(), "revision": revision,
                      "state_revision": state_revision or revision}
            cursor = target.execute(
                "UPDATE user_memories SET " + ", ".join(key + " = ?" for key in values)
                + " WHERE id = ? AND user_id = ? AND revision = ?",
                [*values.values(), memory_id, user_id, row["revision"]])
            if not cursor.rowcount:
                raise MemoryConflict("目标记忆已更新")
            row = self._get_memory_row(memory_id, user_id, target)
            after = _memory_row(row) if actor is not None else self._memory_item(row)
            if actor is not None:
                self._write_change(user_id, memory_id, op, actor, before, after,
                                   connection=target, undo_of=undo_of)
            return after

    def update_memory_text(self, memory_id: str, *, user_id: str, text: str,
                           actor: str | None = None, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        return self._mutate_memory(memory_id, user_id, {"text": text}, op="update", actor=actor,
                                   connection=connection)

    def set_memory_status(self, memory_id: str, *, user_id: str, status: str, reason: str | None,
                          actor: str | None = None, expected_revision: str | None = None,
                          connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        return self._mutate_memory(memory_id, user_id,
                                   {"status": status, "invalid_at": utc_now() if status == "invalid" else None,
                                    "invalid_reason": reason if status == "invalid" else None},
                                   op="invalidate" if status == "invalid" else "restore", actor=actor,
                                   expected_revision=expected_revision, connection=connection)

    def list_memories(self, user_id: str, *, include_invalid: bool = False) -> list[dict[str, Any]]:
        query = _MEMORY_SELECT + " WHERE m.user_id = ?"
        if not include_invalid:
            query += " AND m.status = 'active'"
        query += " ORDER BY m.updated_at DESC, m.id"
        return [self._memory_item(row) for row in self.connection.execute(query, (user_id,))]

    @staticmethod
    def _change_item(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "memoryId": row["memory_id"], "op": row["op"], "actor": row["actor"],
                "at": row["at"], "before": json.loads(row["before_json"]) if row["before_json"] else None,
                "after": json.loads(row["after_json"]) if row["after_json"] else None, "undoOf": row["undo_of"]}

    def get_memory_change(self, change_id: str, *, user_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM user_memory_changes WHERE id = ? AND user_id = ?", (change_id, user_id)).fetchone()
        return self._change_item(row) if row is not None else None

    def list_memory_changes(self, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM user_memory_changes WHERE user_id = ? ORDER BY at DESC, rowid DESC LIMIT ?",
            (user_id, limit)).fetchall()
        return [self._change_item(row) for row in rows]

    def update_memory_text_and_evidence(self, memory_id: str, *, user_id: str, text: str,
                                        evidence: dict[str, Any], tags: list[str] | None = None,
                                        actor: str | None = None, expected_revision: str | None = None,
                                        connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        assignments = {"text": text, "evidence_json": json.dumps(evidence, ensure_ascii=False)}
        if tags is not None:
            assignments["tags_json"] = json.dumps(tags, ensure_ascii=False)
        return self._mutate_memory(memory_id, user_id, assignments, op="update", actor=actor,
                                   expected_revision=expected_revision, connection=connection)

    def undo_memory_change(self, change_id: str, *, user_id: str, actor: str) -> dict[str, Any]:
        with self._memory_write() as target:
            change = self.get_memory_change(change_id, user_id=user_id)
            current = self.get_memory(change["memoryId"], user_id=user_id) if change else None
            if change is None or current is None:
                raise KeyError(change_id)
            previous_undo = target.execute(
                "SELECT after_json FROM user_memory_changes WHERE user_id = ? AND undo_of = ? ORDER BY rowid DESC LIMIT 1",
                (user_id, change_id)).fetchone()
            if previous_undo:
                result = json.loads(previous_undo["after_json"])
                if result["revision"] == current["revision"]:
                    return current  # same request, same committed fact, zero second write/audit
                raise MemoryConflict("这条变动已撤销，之后的修改不能被旧撤销覆盖，请刷新记忆")
            expected = change["after"] or {}
            expected_state = expected.get("stateRevision") or expected.get("revision") or "legacy-change:" + change_id
            if current["stateRevision"] != expected_state:
                raise MemoryConflict("这条记忆已有后续修改，请先撤销后续变动或刷新记忆")
            before = change["before"]
            if before is None:
                state = "removed:" + change_id
                assignments: dict[str, Any] = {"status": "invalid", "invalid_at": utc_now(), "invalid_reason": "撤销添加"}
                operation = "invalidate"
            else:
                state = before.get("stateRevision") or before.get("revision")
                if not state:
                    # The immediate preceding audited state must be exactly this old before.
                    prior = target.execute(
                        "SELECT id, after_json FROM user_memory_changes WHERE user_id = ? AND memory_id = ?"
                        " AND rowid < (SELECT rowid FROM user_memory_changes WHERE id = ?) ORDER BY rowid DESC LIMIT 1",
                        (user_id, current["id"], change_id)).fetchone()
                    if not prior or not prior["after_json"] or not _same_legacy_snapshot(json.loads(prior["after_json"]), before):
                        raise MemoryConflict("旧变动缺少完整版本依据，不能安全撤销，请刷新记忆")
                    prior_after = json.loads(prior["after_json"])
                    state = prior_after.get("stateRevision") or prior_after.get("revision") or "legacy-change:" + prior["id"]
                assignments = {"text": before["text"], "evidence_json": json.dumps(before["evidence"], ensure_ascii=False),
                               "tags_json": json.dumps(before["tags"], ensure_ascii=False), "status": before["status"],
                               "invalid_at": utc_now() if before["status"] == "invalid" else None,
                               "invalid_reason": "撤销恢复" if before["status"] == "invalid" else None}
                operation = "update" if change["op"] == "update" else ("restore" if before["status"] == "active" else "invalidate")
            result = self._mutate_memory(current["id"], user_id, assignments, op=operation, actor=actor,
                                         state_revision=state, undo_of=change_id, connection=target)
            assert result is not None
            return result

    def claim_memory_run(self, user_id: str, day: str, *, window_start: str, force: bool = False,
                         stale_after_seconds: int = 1800, max_attempts: int = 3) -> dict[str, str] | None:
        """认领 (user_id, day) 的一次整理，返回本次 attempt 令牌与要覆盖的材料起点；拿不到返回 None。

        新的一天直接认领；error 与卡住超过 stale_after_seconds 的 running 可重试，同一天合计最多
        max_attempts 次（#524-5）；done 与新鲜 running 排他。force（手动“现在整理一次”）总能接手。
        每次认领换新令牌，并把这个用户更早几天还挂着的 running 一并作废：旧 attempt 的采纳和收尾
        都按令牌判定，接手之后旧 owner 再写不进来。接手 error/running 时材料起点取已有窗口与本次
        窗口中更早的那个，重试不丢失败那次的材料；重开已完成的一天只用本次窗口。"""
        attempt, now = uuid4().hex, utc_now()
        # BEGIN IMMEDIATE: two processes reading "no row" cannot both insert (first claim wins).
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status, at, attempts, window_start FROM user_memory_runs WHERE user_id = ? AND day = ?",
                (user_id, day)).fetchone()
            if row is not None and not force:
                if row["status"] == "done" or int(row["attempts"] or 0) >= max_attempts:
                    return None   # 已完成，或已重试够次数：留给第二天带着这段窗口再整理
                if row["status"] == "running":
                    try:
                        age = (datetime.now(UTC) - datetime.fromisoformat(str(row["at"]))).total_seconds()
                    except ValueError:
                        return None
                    if age < stale_after_seconds:
                        return None
            # 更早几天没收尾的 attempt 作废（它的材料窗口已由调用方带进本次），不与本次同时采纳。
            connection.execute("UPDATE user_memory_runs SET status = 'error', at = ?"
                               " WHERE user_id = ? AND day < ? AND status = 'running'", (now, user_id, day))
            if row is None:
                connection.execute(
                    "INSERT INTO user_memory_runs (user_id, day, status, detail_json, at, attempt, attempts, window_start)"
                    " VALUES (?, ?, 'running', NULL, ?, ?, 1, ?)", (user_id, day, now, attempt, window_start))
                return {"attempt": attempt, "windowStart": window_start}
            start = window_start if row["status"] == "done" or not row["window_start"] \
                else min(window_start, row["window_start"], key=datetime.fromisoformat)
            connection.execute(
                "UPDATE user_memory_runs SET status = 'running', detail_json = NULL, at = ?, attempt = ?,"
                " attempts = attempts + 1, window_start = ? WHERE user_id = ? AND day = ?",
                (now, attempt, start, user_id, day))
            return {"attempt": attempt, "windowStart": start}

    def record_memory_run(self, user_id: str, day: str, status: str, detail: dict[str, Any] | None, *,
                          attempt: str) -> bool:
        """写回这次 attempt 的结果；认领已被别人接手（令牌不同）时不覆盖，返回 False。"""
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE user_memory_runs SET status = ?, detail_json = ?, at = ? WHERE user_id = ? AND day = ? AND attempt = ?",
                (status, json.dumps(detail, ensure_ascii=False) if detail is not None else None, utc_now(),
                 user_id, day, attempt))
            return bool(cursor.rowcount)

    def memory_run_current(self, user_id: str, day: str, attempt: str, *,
                           connection: sqlite3.Connection | None = None) -> bool:
        """这次 attempt 仍是当天唯一在跑的认领：没被取消、没记失败、没被别人接手。"""
        target = connection if connection is not None else self.connection
        row = target.execute("SELECT status, attempt FROM user_memory_runs WHERE user_id = ? AND day = ?",
                             (user_id, day)).fetchone()
        return row is not None and row["status"] == "running" and row["attempt"] == attempt

    def memory_retry_windows(self, since_iso: str) -> dict[str, str]:
        """每个用户最近一次整理若是 error（或卡住的 running），返回它没整理完的材料起点（不早于
        since_iso）：失败那天的材料在下一次整理里补上，而不是滑出 24 小时窗口就丢了。"""
        since_day = datetime.fromisoformat(since_iso).astimezone(_CHINA).date().isoformat()
        latest: dict[str, Any] = {}
        for row in self.connection.execute(
                "SELECT user_id, day, status, window_start FROM user_memory_runs WHERE day >= ? ORDER BY day",
                (since_day,)):
            latest[row["user_id"]] = row
        return {user_id: max(row["window_start"] or since_iso, since_iso, key=datetime.fromisoformat)
                for user_id, row in latest.items() if row["status"] in ("error", "running")}

    def user_texts_between(self, user_id: str, start_iso: str, end_iso: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """窗口 [start, end) 内该用户自己的原话（3b 提炼的输入），最新的在前。role=user 在 SQL
        里过滤（#524-8）：assistant 消息再多也挤不掉窗口里的用户原话。每条带来源身份 key 与
        时间 at：采纳前按 key 复核它仍在（会话被删就不再采纳），与删除决定比先后。"""
        rows = self.connection.execute(
            """SELECT events.id AS id, events.timestamp AS at,
                      json_extract(events.payload_json, '$.text') AS text FROM events
            JOIN sessions ON sessions.id = events.session_id
            WHERE sessions.user_id = ? AND events.name = 'message.completed'
              AND json_extract(events.payload_json, '$.role') = 'user'
              AND events.timestamp >= ? AND events.timestamp < ?
            ORDER BY events.sequence DESC LIMIT ?""",
            (user_id, start_iso, end_iso, limit),
        ).fetchall()
        return [{"key": "event:" + row["id"], "text": row["text"], "at": row["at"]}
                for row in rows if isinstance(row["text"], str)]

    def users_with_texts_between(self, start_iso: str, end_iso: str) -> list[str]:
        rows = self.connection.execute(
            """SELECT DISTINCT sessions.user_id FROM events
            JOIN sessions ON sessions.id = events.session_id
            WHERE events.name = 'message.completed' AND events.timestamp >= ? AND events.timestamp < ?""",
            (start_iso, end_iso),
        ).fetchall()
        return [row["user_id"] for row in rows]

    def notes_between(self, user_id: str, start_iso: str, end_iso: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """窗口内该用户写过的交易补充（trade_notes），只作提炼材料。从最新的往回取（#524-9：
        一天写很多条时，最新的纠正不能被挤掉），返回时翻回时间正序。key 是哪个账户哪个回合，
        at 是这一版补充的写入时间：改过或删掉的补充，采纳前复核时不再算数。"""
        rows = self.connection.execute(
            "SELECT trading_account_id, round_id, text, updated_at FROM trade_notes"
            " WHERE user_id = ? AND updated_at >= ? AND updated_at < ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, start_iso, end_iso, limit),
        ).fetchall()
        return [{"key": "note:" + json.dumps([row["trading_account_id"], row["round_id"]], ensure_ascii=False),
                 "text": row["text"], "at": row["updated_at"]} for row in reversed(rows)]

    def memory_materials_live(self, user_id: str, materials: list[dict[str, Any]], *,
                              connection: sqlite3.Connection | None = None) -> set[str]:
        """采纳前复核（#524-3）：返回仍然存在且还是读取时那一版的材料 key。原话所在的会话被删、
        补充被改或删、归属变了的材料都不在其中。"""
        target = connection if connection is not None else self.connection
        live: set[str] = set()
        for material in materials:
            key = str(material.get("key") or "")
            if key.startswith("event:"):
                row = target.execute(
                    "SELECT 1 FROM events JOIN sessions ON sessions.id = events.session_id"
                    " WHERE events.id = ? AND sessions.user_id = ?", (key[len("event:"):], user_id)).fetchone()
                if row is not None:
                    live.add(key)
            elif key.startswith("note:"):
                account_id, round_id = json.loads(key[len("note:"):])
                row = target.execute(
                    "SELECT text, updated_at FROM trade_notes WHERE trading_account_id = ? AND round_id = ? AND user_id = ?",
                    (account_id, round_id, user_id)).fetchone()
                if row is not None and row["text"] == material.get("text") and row["updated_at"] == material.get("at"):
                    live.add(key)
        return live

    def users_with_notes_between(self, start_iso: str, end_iso: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT user_id FROM trade_notes WHERE updated_at >= ? AND updated_at < ?",
            (start_iso, end_iso),
        ).fetchall()
        return [row["user_id"] for row in rows]

    def recent_user_texts(self, user_id: str, *, days: int = 7, limit: int = 200) -> list[str]:
        """The user's own spoken words across their sessions (name='message.completed' with
        payload.role='user'): what the via=chat 原话核对 compares a quote against. The role
        filter lives in SQL (#524-8), so the LIMIT counts user words only."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.connection.execute(
            """SELECT json_extract(events.payload_json, '$.text') AS text FROM events
            JOIN sessions ON sessions.id = events.session_id
            WHERE sessions.user_id = ? AND events.name = 'message.completed'
              AND json_extract(events.payload_json, '$.role') = 'user'
              AND events.timestamp >= ?
            ORDER BY events.sequence DESC LIMIT ?""",
            (user_id, cutoff, limit),
        ).fetchall()
        return [row["text"] for row in rows if isinstance(row["text"], str)]
