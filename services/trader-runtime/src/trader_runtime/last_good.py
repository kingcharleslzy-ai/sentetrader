"""上次好值（2026-10-12 数据获取审查的统一规则）：外部源失败时保留上次成功取到的值并标 stale，
不把失败或异常当成"没有数据"缓存，也不让失败覆盖好值。

* ``LastGood``：按键保存上次好值，并管每个键下次何时再取——成功按各自 TTL，失败只按短的重试间隔
  推迟下一次，从不碰已存的好值。有数据库连接时好值存进 ``research_live_cache``（重启后读回、两个
  实例共用），没有就只在内存里；本进程还没取过的键按存下的取值时间加 TTL 算到期（重启或另一槽位
  不会把刚取到的值立刻重取；带 ``partial`` 的好值按重试间隔算）。冷启动没有好值时失败如实返回不可用，且只缓存一个重试间隔。
  “空”算不算失败由各数据源自己定义（调用方在 ``succeeded`` 之前判），空结果不覆盖好值。
* ``write_json_atomic`` / ``read_json``：state_root 下的文件版（交易日历、交易所手续费表、备用源合约
  目录）。临时文件名带 uuid：两个容器挂同一个 state_root 时 pid 可能相同，固定名或按 pid 取名会互相
  覆盖半截文件。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TABLE = "research_live_cache"


@dataclass(frozen=True)
class Kept:
    """A last good value; `error` is set while the latest attempt after it failed (the value is stale)."""

    value: Any
    saved_at: float  # wall-clock time it was fetched
    error: str | None = None

    @property
    def stale(self) -> bool:
        return self.error is not None

    @property
    def fetched_at(self) -> str:
        return datetime.fromtimestamp(self.saved_at, UTC).isoformat(timespec="seconds")

    def mark(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A dict payload as served: unchanged while fresh, else status stale with the failure and its fetch time
        (the payload's own fetchedAt when it carries one)."""
        if not self.stale:
            return payload
        return {**payload, "status": "stale", "stale": True, "error": self.error,
                "fetchedAt": payload.get("fetchedAt") or self.fetched_at}


@dataclass
class LastGood:
    connection: sqlite3.Connection | None = None
    lock: Any = None
    prefix: str = ""
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    _memory: dict[str, tuple[float, Any]] = field(default_factory=dict, repr=False)
    _due: dict[str, float] = field(default_factory=dict, repr=False)
    _errors: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.connection is not None:
            with self._locked(), self.connection:
                self.connection.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (key TEXT PRIMARY KEY,"
                                        " fetched_at REAL NOT NULL, payload_json TEXT NOT NULL)")

    def _locked(self) -> Any:
        return self.lock if self.lock is not None else nullcontext()

    def saved(self, key: str) -> tuple[float, Any] | None:
        """(fetch time, value) of the stored good value. Read from the database every time when there is one:
        the other instance may have refreshed it."""
        if self.connection is None:
            return self._memory.get(key)
        with self._locked():
            row = self.connection.execute(f"SELECT fetched_at, payload_json FROM {TABLE} WHERE key = ?",
                                          (self.prefix + key,)).fetchone()
        if row is None:
            return None
        try:
            return float(row[0]), json.loads(row[1])
        except ValueError:
            return None

    def saved_at(self, key: str) -> float | None:
        if self.connection is None:
            entry = self._memory.get(key)
            return entry[0] if entry else None
        with self._locked():
            row = self.connection.execute(f"SELECT fetched_at FROM {TABLE} WHERE key = ?", (self.prefix + key,)).fetchone()
        return float(row[0]) if row else None

    def get(self, key: str) -> Kept | None:
        saved = self.saved(key)
        return None if saved is None else Kept(saved[1], saved[0], self._errors.get(key))

    def value(self, key: str) -> Any:
        saved = self.saved(key)
        return None if saved is None else saved[1]

    def put(self, key: str, value: Any) -> None:
        """Store a good value (no schedule change)."""
        at = self.wall()
        if self.connection is None:
            self._memory[key] = (at, value)
            return
        with self._locked(), self.connection:
            self.connection.execute(
                f"INSERT INTO {TABLE} (key, fetched_at, payload_json) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, payload_json = excluded.payload_json",
                (self.prefix + key, at, json.dumps(value, ensure_ascii=False, default=str)))

    def due(self, key: str, ttl: float, retry: float | None = None) -> bool:
        """Whether a fetch may start: its TTL / retry interval has passed. A key this process has not tried yet
        is scheduled from the stored good value's fetch time plus `ttl` (#788): a restart or the other
        blue-green slot reuses a value fetched moments ago for the rest of its TTL instead of fetching again.
        A stored value marked ``partial`` (some of its sources failed) was kept for `retry` only, so it is
        scheduled from its fetch time plus `retry` when one is given (#932)."""
        if key not in self._due:
            saved = self.saved(key)
            if saved is None:
                return True
            saved_at, value = saved
            span = retry if retry is not None and isinstance(value, dict) and value.get("partial") else ttl
            remaining = saved_at + span - self.wall()
            if remaining <= 0:
                return True
            self._due[key] = self.clock() + min(remaining, span)
        return self._due[key] <= self.clock()

    def backing_off(self, key: str) -> bool:
        """Whether ``key`` is inside a failure's retry interval (no fetch may start yet)."""
        return key in self._due and self._due[key] > self.clock()

    def error(self, key: str) -> str | None:
        return self._errors.get(key)

    def succeeded(self, key: str, value: Any, ttl: float) -> Kept:
        self.put(key, value)
        self._due[key] = self.clock() + ttl
        self._errors.pop(key, None)
        return Kept(value, self.wall())

    def forget(self, key: str) -> None:
        """Drop ``key`` entirely (its value and schedule): a memory-only reader's keys nobody reads any more."""
        self._memory.pop(key, None)
        self._due.pop(key, None)
        self._errors.pop(key, None)
        if self.connection is not None:
            with self._locked(), self.connection:
                self.connection.execute(f"DELETE FROM {TABLE} WHERE key = ?", (self.prefix + key,))

    def failed(self, key: str, error: str, retry: float) -> Kept | None:
        """A failed or empty attempt: the next one waits `retry`, the good value stays and now reads stale."""
        self._due[key] = self.clock() + retry
        self._errors[key] = error
        return self.get(key)


def read_json(path: Path | None) -> Any:
    """The saved file's JSON, None when missing or unreadable."""
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write through a uniquely named temporary file and rename over the target (OSError propagates)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
