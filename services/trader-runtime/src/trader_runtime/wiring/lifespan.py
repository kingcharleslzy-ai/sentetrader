"""Start and stop the background services around the FastAPI app, in dependency order."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from ..exchange_fees import start_refresher, stop_refresher
from ..tqkq_retirement import retire_tqkq_trading
from .context import RuntimeContext

log = logging.getLogger("uvicorn.error.lifespan")


def build_lifespan(ctx: RuntimeContext):
    """The context's application field is filled in by create_app before startup."""
    services, agent = ctx.services, ctx.agent
    store, broker, adapter = services.store, services.broker, agent.adapter
    accounts = services.accounts.all()
    providers = [services_for_account.provider for services_for_account in accounts]
    ledgers = [services_for_account.account_ledger for services_for_account in accounts]
    autonomies = [services_for_account.autonomy for services_for_account in accounts]
    monitors = [services_for_account.strategy_monitor for services_for_account in accounts]
    lifecycle, runs = ctx.lifecycle, agent.runs
    status_for_session = ctx.status_for_session

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime_feed = ctx.runtime_feed
        instance_lease = ctx.instance_lease
        loop = asyncio.get_running_loop()
        status_tasks: set[asyncio.Task[None]] = set()
        unsubscribe_snapshots: list[Callable[[], None]] = []
        unsubscribe_providers: list[Callable[[], None]] = []
        rum_sweep: asyncio.Task[None] | None = None

        async def close_quietly(step: str, action: Callable[[], Any]) -> None:
            """One shutdown step, however startup went (#525-C3): a component that
            never started — or a close that itself fails — must not strand the
            resources after it. The failure is logged; the sequence continues."""
            try:
                result = action()
                if isinstance(result, Awaitable):
                    await result
            except Exception:  # noqa: BLE001 — best-effort teardown
                log.warning("lifespan_close_failed step=%s", step, exc_info=True)

        async def publish_provider_status() -> None:
            for session_id in broker.subscribed_session_ids():
                try:
                    payload = status_for_session(session_id)
                except KeyError:
                    continue
                await broker.publish(
                    session_id, kind="result", name="runtime.status", payload=payload,
                )

        def schedule_provider_status(_: dict[str, object]) -> None:
            def schedule() -> None:
                task = asyncio.create_task(publish_provider_status())
                status_tasks.add(task)
                task.add_done_callback(status_tasks.discard)
            if not loop.is_closed():
                loop.call_soon_threadsafe(schedule)

        def snapshots_arrived() -> None:
            runtime_feed.snapshot_arrived()
            for ledger in ledgers:
                if ledger is not None:
                    ledger.snapshot_arrived()

        def wire_snapshots(provider) -> Callable[[], None]:
            subscribe_snapshot = getattr(provider, "subscribe_snapshot", None)
            return subscribe_snapshot(
                lambda: loop.call_soon_threadsafe(snapshots_arrived) if not loop.is_closed() else None
            ) if subscribe_snapshot is not None else (lambda: None)

        try:
            await broker.start()
            shared_market = getattr(application.state, "shared_market", None)
            if shared_market is not None:
                # 新浪备用 on/off changes what runtime.status says: republish it to every reader.
                shared_market.backup_listeners.append(lambda: schedule_provider_status({}))
                # 外盘's 快期 source reads the 快期 foreign main contracts through the shared quote channel.
                services.foreign_market.bind(loop, shared_market.quotes)
                shared_market.foreign_chart = services.foreign_market.chart  # 外盘图表：分钟到小时线由 MT5 画
                shared_market.foreign_quote = services.foreign_market.chart_quote  # 日线/周线报价取 MT5 主力月份
            unsubscribe_snapshots = [wire_snapshots(provider) for provider in providers]
            unsubscribe_providers = [provider.subscribe_status(schedule_provider_status) for provider in providers]
            for provider in providers:
                await provider.start()
            if ctx.replay_provider is None:
                # Replay windows keep their fast local polling; no feed for the child app.
                await runtime_feed.start()
                # The ordering/bookkeeping loops — the ledger, the stop monitor, the
                # strategy monitor — and the background producers (news, research,
                # reminders, reviews: wiring/producers.py) run only on the lease holder;
                # a second process on the same state database stands by and keeps
                # serving interfaces, reading what the holder writes.
                if instance_lease is not None:
                    await instance_lease.start()
                # 快期模拟停用交易 (#1205): members' 快期 trading accounts move to 本站模拟
                # (idempotent; a no-op without the paper engine) before the pool adopts children.
                try:
                    await retire_tqkq_trading(ctx, market_sources=(
                        shared_market.ordered_sources() if shared_market is not None else store.list_market_sources()))
                except Exception:  # noqa: BLE001 — never blocks the boot; the next boot retries
                    log.exception("tqkq_retire_step_failed")
                # M1a: idle reclamation of pool-assembled accounts, plus boot-time
                # adoption of children left running by an exited blue-green slot.
                if ctx.account_pool is not None:
                    await ctx.account_pool.start_background()
                # 本站模拟账户: boot-time assembly of the busy paper accounts —
                # monitoring strategies, armed stops, in-flight order links; the
                # idle ones assemble on their owner's first request.
                if ctx.paper_accounts is not None:
                    await ctx.paper_accounts.start_background()
            await publish_provider_status()
            await lifecycle.start()
            # Follows the confirmed orders (their rows, #932) and this process's cards: every process.
            application.state.order_watch.start()
            if instance_lease is None:
                # 到价提醒 in a replay child (2026-10-03): it watches the replay market on the replay
                # clock; the stop below is unconditional. The main app's crossing loop is a lease
                # producer like the research, reminder and review loops (wiring/producers.py): the
                # lease holder started them above, and only the holder runs them.
                application.state.price_alerts.start()
            if ctx.replay_provider is None:
                # The heartbeat of the debates this process runs (started by its readers): every process.
                services.research_debate.start_beat()
            # 交易所标准手续费：后台线程抓 openctp（启动即一次，当天没成功每 30 分钟重试）；
            # 下单估算 / paper 扣费 / 回测的读路径只读缓存，不联网等待。The main app's refresher
            # is a lease producer (it writes the shared state root); a replay child keeps its own.
            if instance_lease is None:
                start_refresher(ctx.settings.state_root)
            # 真实用户监测 retention does not wait for new beacons (boot sweep, then hourly).
            rum_store = getattr(application.state, "rum_store", None)
            if rum_store is not None:
                rum_sweep = asyncio.create_task(rum_store.sweep_forever())
            yield
        finally:
            # #525-C3: this runs for a failed startup too — a component that started
            # before the failure must not stay running — and every step is attempted
            # whatever the previous one did. Only this app's root: a replay child's
            # shutdown must not stop the main app's refresher.
            await close_quietly("exchange_fees", lambda: stop_refresher(ctx.settings.state_root))
            if rum_sweep is not None:
                rum_sweep.cancel()
                await asyncio.gather(rum_sweep, return_exceptions=True)
            rum_telemetry = getattr(application.state, "rum_store", None)
            if rum_telemetry is not None:
                await close_quietly("rum_store", rum_telemetry.close)
            await close_quietly("guests", application.state.guests.close)
            # M1a: pool accounts go first — their loops and subscriptions are this
            # runtime's own wiring. The children stay: a blue-green sibling slot
            # may still be serving them (2026-09-24).
            if ctx.account_pool is not None:
                await close_quietly("account_pool", ctx.account_pool.shutdown)
            # Paper accounts detach the same way: this runtime's wiring goes, the
            # engine-side state stays and the next boot or request re-assembles.
            if ctx.paper_accounts is not None:
                await close_quietly("paper_accounts", ctx.paper_accounts.shutdown)
            await close_quietly("runtime_feed", runtime_feed.stop)
            if instance_lease is not None:
                # Stops the loops first and only then releases the lease; the stops
                # below still run before the store closes as the idempotent fallback.
                await close_quietly("instance_lease", instance_lease.stop)
            for ledger in ledgers:
                if ledger is not None:
                    await close_quietly("account_ledger", ledger.stop)
            for unsubscribe_snapshot in unsubscribe_snapshots:
                await close_quietly("snapshot_unsubscribe", unsubscribe_snapshot)
            await close_quietly("market_briefs", application.state.market_briefs.close)
            await close_quietly("strategy_explanations", application.state.strategy_explanations.close)
            if ctx.replay_router is not None:
                await close_quietly("replay_router", ctx.replay_router.close)
            for unsubscribe_provider in unsubscribe_providers:
                await close_quietly("status_unsubscribe", unsubscribe_provider)
            for monitor in monitors:
                if monitor is not None:
                    await close_quietly("strategy_monitor", monitor.stop)
            for autonomy in autonomies:
                if autonomy is not None:
                    await close_quietly("autonomy", autonomy.stop)
            if status_tasks:
                await asyncio.gather(*status_tasks, return_exceptions=True)
            await close_quietly("price_alerts", application.state.price_alerts.stop)
            for name in ("account_reminder_loop", "movement_watch", "daily_reviews", "reminder_retention", "push"):
                component = getattr(application.state, name, None)
                if component is not None:
                    await close_quietly(name, component.stop if hasattr(component, "stop") else component.close)
            await close_quietly("order_watch", application.state.order_watch.stop)
            await close_quietly("lifecycle", lifecycle.stop)
            await close_quietly("ui_workspace", application.state.ui_workspace.close)
            await close_quietly("runs", runs.shutdown)
            await close_quietly("strategy_backtest_queue", application.state.strategy_backtest_queue.shutdown)
            # Judgments use the model adapter; stop them before it shuts down.
            await close_quietly("research_debate", services.research_debate.close)
            # The brief's news searches first: the desk reads news through it (#932).
            await close_quietly("research_brief", services.research_brief.close)
            await close_quietly("research_desk", services.research_desk.close)
            await close_quietly("research_live", services.research_live.close)
            await close_quietly("foreign_market", services.foreign_market.close)
            await close_quietly("memory_consolidator", application.state.memory_consolidator.close)
            shutdown_adapter = getattr(adapter, "shutdown", None)
            if shutdown_adapter is not None:
                await close_quietly("adapter", shutdown_adapter)
            # Tool/design owners finish before closing their shared HTTP pools.
            for provider in reversed(providers):
                await close_quietly("provider", provider.stop)
            await close_quietly("broker", broker.shutdown)
            await close_quietly("research", services.research.close)
            await close_quietly("store", store.close)
    return lifespan
