"""交割月提醒的“一天一次”账本：每个用户 × 合约 × 交易日一行，与账户无关
（提醒跟着持仓合约走，不跟哪个账户）。"""
from __future__ import annotations

import sqlite3

from .base import StoreBase, utc_now


class DeliveryNoticeStore(StoreBase):

    def record_delivery_notice(self, *, user_id: str, symbol: str, trade_day: str,
                               kind: str, level: str, text: str,
                               connection: sqlite3.Connection | None = None) -> bool:
        """True 当且仅当这一行是今天第一次写入（提醒还没发过）；已存在时不改任何东西，
        重启、多窗口与一天里的反复读取都不会重发。`connection` 在调用方的事务里写这一行
        （#521）：去重行与随提醒落库的消息同进同退，投递步骤失败下一轮重试也不会重发。"""
        if connection is not None:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO delivery_notices
                (user_id, symbol, trade_day, kind, level, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, symbol, trade_day, kind, level, text, utc_now()))
            return cursor.rowcount == 1
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO delivery_notices
                (user_id, symbol, trade_day, kind, level, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, symbol, trade_day, kind, level, text, utc_now()))
            return cursor.rowcount == 1
