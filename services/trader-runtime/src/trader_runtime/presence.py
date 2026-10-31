"""在线判定：谁还在用这个 Runtime；按需装配的账户在所有人离线后回收。

touch() 由四处入口调用（UI 心跳、SSE 订阅、发消息、语音通话开始），
闲置回收扫描在 account_pool 的后台任务里每 60 s 读一次。
"""
from __future__ import annotations

import os
import time

IDLE_SECONDS_ENV = "AI_TRADER_ACCOUNT_IDLE_SECONDS"
DEFAULT_IDLE_SECONDS = 1800.0
SCAN_INTERVAL_SECONDS = 60.0


def idle_seconds_from_env() -> float:
    try:
        return max(1.0, float(os.getenv(IDLE_SECONDS_ENV, str(DEFAULT_IDLE_SECONDS))))
    except ValueError:
        return DEFAULT_IDLE_SECONDS


class PresenceTracker:
    """user_id → 最近一次心跳的单调时钟；纯内存，Runtime 重启即全员重新上线。"""

    def __init__(self) -> None:
        self._last_seen: dict[str, float] = {}
        # M2: the wall-clock twin of the same heartbeat, for the owner's
        # "最近在线" column (monotonic clocks cannot cross a restart).
        self._last_seen_at: dict[str, float] = {}

    def touch(self, user_id: str) -> None:
        if user_id:
            self._last_seen[user_id] = time.monotonic()
            self._last_seen_at[user_id] = time.time()

    def idle_seconds(self, user_id: str, *, now: float | None = None) -> float | None:
        """距上次心跳的秒数；None = 本次启动以来从未出现（按从未在线处理）。"""
        seen = self._last_seen.get(user_id)
        if seen is None:
            return None
        return (now if now is not None else time.monotonic()) - seen

    def last_seen_epoch(self, user_id: str) -> float | None:
        """上次心跳的墙钟时间；None = 本次启动以来从未出现。"""
        return self._last_seen_at.get(user_id)
