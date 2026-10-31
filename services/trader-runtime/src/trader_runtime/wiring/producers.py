"""The background producers and who runs them (2026-10-12).

A producer is a background loop that reads external sources, calls a model, or writes shared state
other processes read: the flash poll with its screener, analyst and translator, the desk's 08:45
pre-generation, the debates' settlement, the price/close/risk/movement/delivery reminders and their 30-day retention, the 06:00 memory
consolidation, the 15:15 review and the exchange-fee refresher. Blue-green slots share one state database, so every producer runs only on the
instance-lease holder: `start_producers` runs when the lease lands here (main.py `lease_acquired`)
and `stop_producers` when it moves away or the process stops (`lease_lost`), before the lease is
released. A stop is a barrier: work a producer already started — the desk's pre-generation model calls,
a flash/calendar/LPR round or the fee fetch in its worker thread — is cancelled or waited for (threads
for at most `STOP_WAIT_SECONDS`, then logged) before the lease moves. Each one's own database guard
(conditional updates, push claims) stays as the second line of protection for the handoff instant.

Each start also catches up on what is due now, so a slot switch never loses the day: the research
desk's 08:45 pre-generation, the 06:00 memory consolidation and the 15:15 review run within a minute once a
holder starts after their time if the day's run has not happened (each checks the shared database
for that).

Not producers, so they run on every process: the push feed, the debates' heartbeat (it keeps this
process's own running debates alive) and the unfilled-order watch (it follows the confirmed orders from their rows — a restart or the
other slot picks them up, #932 — and the cards confirmed through this process; its reminders are once
by their claims).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..exchange_fees import start_refresher, stop_refresher
from ..research_live import STOP_WAIT_SECONDS

log = logging.getLogger("uvicorn.error.lifespan")


def _producers(application: Any, services: Any) -> list[tuple[str, Callable[[], Any], Callable[[], Any]]]:
    """(name, start, stop) for every producer this app has, in start order; stopped in reverse."""
    state = application.state
    root = state.ctx.settings.state_root
    found: list[tuple[str, Callable[[], Any], Callable[[], Any]]] = [
        # 交易所标准手续费: the daily openctp fetch into the shared state root (readers only read that file)
        ("exchange_fees", lambda: start_refresher(root),
         lambda: asyncio.to_thread(stop_refresher, root, STOP_WAIT_SECONDS)),
        # 快讯每分钟一巡 (and its screener, analyst, translator); 日历 6 h、LPR 每日随同一循环
        ("research_live", services.research_live.start, services.research_live.stop),
        # 08:45 pre-generation of the day's judgments
        ("research_desk", services.research_desk.start, services.research_desk.stop_loop),
        ("news_search", services.news_search.start, services.news_search.close),
        # 多 AI 研讨: settle the debates five trading days on
        ("research_debate", services.research_debate.start_settle, services.research_debate.stop_settle),
    ]
    for name, stop in (
        ("price_alerts", "stop"),             # 到价提醒
        ("account_reminder_loop", "stop"),    # 收盘前 / 风险度 without a client open
        ("movement_watch", "stop"),           # 盘中异动
        ("memory_consolidator", "close"),     # 06:00 私人记忆每日整理
        ("daily_reviews", "stop"),            # 15:15 今日复盘
        ("reminder_retention", "stop"),       # 提醒记录与旧去重表 30 天清理 (#932)
    ):
        component = getattr(state, name, None)
        if component is not None:
            found.append((name, component.start, getattr(component, stop)))
    return found


async def _call(action: Callable[[], Any]) -> None:
    result = action()
    if isinstance(result, Awaitable):
        await result


async def start_producers(application: Any, services: Any) -> None:
    for name, start, _ in _producers(application, services):
        await _call(start)
        log.debug("lease_producer_started name=%s", name)


async def stop_producers(application: Any, services: Any) -> None:
    """Every producer is stopped even when one fails; the failures are raised together so the lease
    keeps retrying the stop instead of handing over with a producer still running."""
    failures: list[Exception] = []
    for name, _, stop in reversed(_producers(application, services)):
        try:
            await _call(stop)
        except Exception as error:  # noqa: BLE001 — collected and raised below
            log.warning("lease_producer_stop_failed name=%s", name, exc_info=True)
            failures.append(error)
    if failures:
        raise ExceptionGroup("lease producers did not all stop", failures)
