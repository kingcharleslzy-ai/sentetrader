"""Administrator-only MT5 outage transitions, driven by the existing reminder producer.

No separate probe: refresh the normal foreign board and consume its actual MT5 source sample.
State is process-local and resets on lease acquisition/restart. Closed sessions cannot start an
outage; an already alerted outage stays pending until a real successful read, including at close.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .trading_calendar import NS, cme_session
from .web_push import PushMessage, admin_push_user_id

log = logging.getLogger("uvicorn.error.mt5_alerts")
OUTAGE_SECONDS = 600
SAMPLE_MAX_AGE_SECONDS = 120


class Mt5BridgeAlerts:
    def __init__(self, app: Any, *, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.app, self.clock, self.now = app, clock, now
        self.reset()

    def reset(self) -> None:
        self.failed_since: float | None = None
        self.alerted = False

    def _foreign(self) -> Any:
        ctx = getattr(self.app.state, "ctx", None)
        return ctx.services.foreign_market if ctx is not None else None

    def log_started(self) -> None:
        foreign = self._foreign()
        if foreign is not None and any(source.name == "mt5" for source in foreign.sources):
            log.info("mt5_bridge_alert_monitor_started threshold_seconds=%s producer=account_reminder_loop", OUTAGE_SECONDS)

    async def check(self) -> None:
        foreign = self._foreign()
        hub = getattr(self.app.state, "push", None)
        user_id = admin_push_user_id(self.app)
        if foreign is None or hub is None or not hub.configured or user_id is None:
            return
        if not any(source.name == "mt5" for source in foreign.sources):
            return
        # Collector coalesces this read with UI/AI reads; it runs even with no connected clients.
        await foreign.board()
        now, tick = self.now(), self.clock()
        trading = cme_session(int(now.timestamp() * NS))["status"] == "trading"
        sample = foreign.mt5_health
        if sample is None or not 0 <= (now - sample[0]).total_seconds() <= SAMPLE_MAX_AGE_SECONDS:
            # A stale cached board cannot prove continuous failure or recovery.
            if not self.alerted:
                self.failed_since = None
            return
        if sample[1]:
            if self.alerted and self.failed_since is not None:
                minutes = int((tick - self.failed_since) // 60)
                self._notify(hub, user_id, "外盘行情已恢复", f"MT5 行情桥已恢复：中断 {minutes} 分钟（4090）")
            self.reset()
            return
        if not trading:
            if not self.alerted:
                self.failed_since = None
            return
        if self.failed_since is None:
            self.failed_since = tick
        if not self.alerted and tick - self.failed_since >= OUTAGE_SECONDS:
            minutes = int((tick - self.failed_since) // 60)
            self._notify(hub, user_id, "外盘行情中断", f"MT5 行情桥已 {minutes} 分钟取不到行情（4090）")
            self.alerted = True

    @staticmethod
    def _notify(hub: Any, user_id: str, title: str, body: str) -> None:
        hub.notify(user_id, PushMessage(kind="admin_alert", title=title, body=body, short=body,
                                        tag="admin-mt5-bridge", url="admin?section=servers"))
        log.info("mt5_bridge_alert_transition title=%s", title)
