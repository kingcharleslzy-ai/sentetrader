"""收盘前/风险度提醒的“一次”账本：每个用户 × 账户 × 交易日 × kind 一行（kind 是
market_close_day / market_close_night / risk_ratio），与交割提醒的账本同一套幂等写法。"""
from __future__ import annotations

import sqlite3

from .base import StoreBase, utc_now


class ReminderNoticeStore(StoreBase):

    def record_reminder_notice(self, *, user_id: str, account_id: str, trade_day: str,
                               kind: str, text: str, connection: sqlite3.Connection | None = None,
                               symbols: str = "") -> bool:
        """True 当且仅当这一行是第一次写入（这次提醒还没发过）；已存在时不改任何东西，
        重启、多窗口与一天里的反复读取都不会重发。`connection` 在调用方的事务里写这一行
        （#521）：去重行与随提醒落库的消息同进同退，投递步骤失败下一轮重试也不会重发。
        `symbols`（今日复盘 2026-10-06）记下这条提醒说到的合约（逗号分隔；空＝整个账户，风险度
        就是），复盘据此判定同一品种的单子是不是“提醒后”下的。"""
        if connection is not None:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO reminder_notices
                (user_id, account_id, trade_day, kind, text, created_at, symbols)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, account_id, trade_day, kind, text, utc_now(), symbols))
            return cursor.rowcount == 1
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO reminder_notices
                (user_id, account_id, trade_day, kind, text, created_at, symbols)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, account_id, trade_day, kind, text, utc_now(), symbols))
            return cursor.rowcount == 1
