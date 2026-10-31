"""FastAPI entrypoint: `create_app` assembles the runtime, `app` is what uvicorn serves.

Configuration resolution, service construction, agent selection, auth, the push
feed and the lifespan live under `wiring/`; the HTTP routes under `routes/`.
Historical replays get their own child app through `create_replay_app`.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .account_pool import POOL_URL_ENV, AccountPoolManager, PoolClient
from .broker import utc_now
from .credential_box import CredentialBox
from .data_providers import ReadOnlyDataProvider
from .instance_lease import InstanceLease
from .lifecycle import SessionLifecycleManager
from .paper_accounts import (
    PAPER_ENGINE_URL_ENV,
    PAPER_FOREIGN_ENGINE_URL_ENV,
    PaperAccountManager,
    PaperEngineClient,
    PaperMarketSourceSync,
)
from .presence import PresenceTracker
from .routes import register_all
from .secret_values import secret_value
from .simulation_orders import SimulationGateway
from .skill_registry import write_skill_gate
from .wiring.accounts import ACCOUNT_HEADER, idp_fallback_for_auth
from .wiring.agent import build_agent
from .wiring.auth import AuthService, build_auth, current_user_resolver, default_execution_owner_user_id
from .wiring.config import REPO_ROOT, RuntimeSettings, _migrate_legacy_database, resolve_settings
from .wiring.context import RuntimeContext
from .wiring.feed import build_feed
from .wiring.http_middleware import DisconnectGuard, OriginAndClientCompat
from .wiring.lifespan import build_lifespan
from .wiring.producers import start_producers, stop_producers
from .wiring.services import build_services, recover_incomplete_agent_runs, recover_interrupted_execution

__all__ = ["REPO_ROOT", "_migrate_legacy_database", "app", "create_app", "create_replay_app"]

log = logging.getLogger("uvicorn.error.client")


def create_app(
    database_path: Path | None = None, *, local_token: str | None = None,
    agent_mode: Literal["fake", "native"] | None = None,
    native_transport: Any | None = None,
    engineering_root: Path | None = None,
    runtime_state_root: Path | None = None,
    data_mode: Literal["fake", "tqkq-readonly", "tqkq-sidecar"] | None = None,
    data_provider: ReadOnlyDataProvider | None = None,
    auth_mode: Literal["local-token", "cloud-jwt", "owner-password"] | None = None,
    cloud_jwt_secret: str | None = None,
    owner_username: str | None = None,
    owner_password_hash: str | None = None,
    owner_session_secret: str | None = None,
    cors_origins: set[str] | None = None,
    runtime_instance_id: str | None = None,
    simulation_gateway: SimulationGateway | None = None,
    agent_provider: Literal["deepseek", "moonshot"] | None = None,
    agent_credential_configured: bool = False,
    registration_enabled: bool | None = None,
    deepseek_api_key: str | None = None,
    account_providers: Mapping[str, ReadOnlyDataProvider] | None = None,
    _replay_provider: Any | None = None,
    _replay_auth: Any | None = None,
) -> FastAPI:
    settings = resolve_settings(
        database_path=database_path, engineering_root=engineering_root, runtime_state_root=runtime_state_root,
        data_mode=data_mode, data_provider=data_provider, auth_mode=auth_mode, agent_mode=agent_mode,
        agent_provider=agent_provider, cors_origins=cors_origins, simulation_gateway=simulation_gateway,
    )
    return _build_runtime(
        settings, local_token=local_token, native_transport=native_transport, data_provider=data_provider,
        cloud_jwt_secret=cloud_jwt_secret, owner_username=owner_username, owner_password_hash=owner_password_hash,
        owner_session_secret=owner_session_secret, runtime_instance_id=runtime_instance_id,
        simulation_gateway=simulation_gateway, agent_credential_configured=agent_credential_configured,
        registration_enabled=registration_enabled, deepseek_api_key=deepseek_api_key,
        account_providers=account_providers,
        replay_provider=_replay_provider, replay_auth=_replay_auth,
    )


def create_replay_app(parent: RuntimeContext, root: Path, replay_provider: Any) -> FastAPI:
    """A historical replay runs as its own app: fake data mode fed by the replay
    provider, which is also its simulation gateway; the parent's auth and agent
    settings; local-token auth so the parent router owns the identity check."""
    settings = resolve_settings(
        database_path=root / "runtime.sqlite3", engineering_root=parent.settings.engineering_root,
        runtime_state_root=root, data_mode="fake", data_provider=replay_provider, auth_mode="local-token",
        agent_mode=parent.settings.agent_mode, agent_provider=parent.settings.agent_provider,
        cors_origins=parent.settings.origins, simulation_gateway=replay_provider,
    )
    app = _build_runtime(
        settings, local_token=None, native_transport=parent.native_transport, data_provider=replay_provider,
        cloud_jwt_secret=None, owner_username=None, owner_password_hash=None, owner_session_secret=None,
        runtime_instance_id=None, simulation_gateway=replay_provider,
        agent_credential_configured=parent.agent_credential_configured, registration_enabled=None,
        deepseek_api_key=None, replay_provider=replay_provider, replay_auth=parent.auth,
    )
    # A replay app is single-tenant by construction: its own database serves the one
    # user the parent's router already checked owns this replay. It inherits the
    # parent's owner-password auth, which would otherwise mark it multi-tenant and
    # describe the replay account with role null (the client then drops every
    # runtime.status event and the replay never loads its chart, 2026-09-24).
    app.state.store.idp_fallback = True
    return app


def _build_runtime(
    settings: RuntimeSettings, *, local_token: str | None, native_transport: Any | None,
    data_provider: ReadOnlyDataProvider | None, cloud_jwt_secret: str | None, owner_username: str | None,
    owner_password_hash: str | None, owner_session_secret: str | None, runtime_instance_id: str | None,
    simulation_gateway: SimulationGateway | None, agent_credential_configured: bool,
    registration_enabled: bool | None, deepseek_api_key: str | None, replay_provider: Any | None,
    replay_auth: AuthService | None, account_providers: Mapping[str, ReadOnlyDataProvider] | None = None,
) -> FastAPI:
    services = build_services(settings, data_provider=data_provider, simulation_gateway=simulation_gateway,
                              replay_provider=replay_provider,
                              execution_owner_user_id=default_execution_owner_user_id(settings, owner_username),
                              account_providers=account_providers)
    agent = build_agent(settings, services, native_transport=native_transport, deepseek_api_key=deepseek_api_key,
                        replay_provider=replay_provider)
    lifecycle = SessionLifecycleManager(
        store=services.store,
        broker=services.broker,
        runs=agent.runs,
        adapter=agent.adapter,
    )
    auth = build_auth(
        settings, services.store, local_token=local_token, cloud_jwt_secret=cloud_jwt_secret,
        owner_username=owner_username, owner_password_hash=owner_password_hash,
        owner_session_secret=owner_session_secret, registration_enabled=registration_enabled, replay_auth=replay_auth,
    )
    # The deployment-level IdP fallback verdict (single-tenant modes only) for
    # the surfaces that describe an account without a request: the runtime.status
    # account block and the accounts directory. The request path keeps its own,
    # login-role-refined verdict in RuntimeContext.
    services.store.idp_fallback = idp_fallback_for_auth(auth)
    store, broker = services.store, services.broker
    for recovered in services.recovered_runs:
        store.append_event(recovered["sessionId"], {
            "schemaVersion": 1, "id": str(uuid4()), "sessionId": recovered["sessionId"],
            "kind": "error", "name": "error", "timestamp": utc_now(),
            "payload": {"code": "runtime_restarted", "message": "Runtime 重启，未完成任务已安全终止。",
                        "runId": recovered["runId"], "scope": "task"},
        })
    # The desktop shell passes its instanceId; every other process gets its own.
    if replay_provider is None:
        runtime_instance_id = runtime_instance_id or uuid4().hex
    ctx = RuntimeContext(
        settings=settings, services=services, agent=agent, auth=auth, lifecycle=lifecycle,
        current_user=current_user_resolver(auth), runtime_instance_id=runtime_instance_id,
        agent_credential_configured=agent_credential_configured, native_transport=native_transport,
        replay_provider=replay_provider,
    )
    ctx.runtime_feed = build_feed(ctx)
    if replay_provider is None:
        # M1a: the on-demand TqKq access pool. No URL configured → no pool, and
        # every account keeps the static single-boot behaviour. A URL without a
        # readable credential key would mean binding is requested but passwords
        # cannot be sealed — that combination refuses to start.
        pool_url = os.getenv(POOL_URL_ENV, "").strip()
        if pool_url:
            credential_box = CredentialBox.from_env()
            if credential_box is None:
                raise RuntimeError("tqkq_pool_requires_credential_key")
            ctx.account_pool = AccountPoolManager(
                client=PoolClient(pool_url, secret_value("AI_TRADER_READONLY_SIDECAR_TOKEN", required=True)),
                registry=services.accounts, store=store, credential_box=credential_box,
                services=services, settings=settings,
                runtime_feed=ctx.runtime_feed, status_for_session=ctx.status_for_session,
            )
        # 本站模拟账户 (2026-10-03): no engine URL configured → no paper
        # feature, and every user keeps the behaviour they had.
        paper_url = os.getenv(PAPER_ENGINE_URL_ENV, "").strip()
        if paper_url:
            # 外盘本站模拟（外盘二期，2026-10-19）：外盘撮合引擎的地址，配了才有美元子账户。
            foreign_paper_url = os.getenv(PAPER_FOREIGN_ENGINE_URL_ENV, "").strip()
            ctx.paper_accounts = PaperAccountManager(
                client=PaperEngineClient(
                    paper_url, secret_value("AI_TRADER_READONLY_SIDECAR_TOKEN", required=True)),
                registry=services.accounts, store=store, services=services, settings=settings,
                runtime_feed=ctx.runtime_feed, status_for_session=ctx.status_for_session,
                # 外盘引擎是同进程的第二个实例：开户请求短超时，它没起来时不拖住启动与注册。
                foreign_client=PaperEngineClient(
                    foreign_paper_url, secret_value("AI_TRADER_READONLY_SIDECAR_TOKEN", required=True), timeout=3.0)
                if foreign_paper_url else None,
            )
            # 行情账号接力 (2026-10-12): the engine's matching quotes relay across the
            # public market's ordered source accounts; the lease holder hands it the list.
            paper = ctx.paper_accounts
            if paper.foreign_client is not None:
                async def foreign_account(user_id: str) -> str | None:
                    """外盘二期：这个用户的美元子账户，装配好再交出去；没有或装配不上为 None。"""
                    record = store.find_user_paper_account(user_id=user_id)
                    foreign_id = paper.foreign_account_of(str(record["id"])) if record is not None else None
                    if foreign_id is None:
                        return None
                    try:
                        await paper.ensure(foreign_id, reason="foreign_order")
                    except Exception:
                        log.exception("paper_foreign_assemble_failed account=%s", foreign_id)
                        return None
                    return foreign_id

                services.accounts.foreign_account = foreign_account
                services.accounts.foreign_account_of = paper.foreign_account_of
            ctx.paper_accounts.market_sync = PaperMarketSourceSync(
                client=lambda: paper.client, store=store,
                shared_market=lambda: (getattr(ctx.application.state, "shared_market", None)
                                       if ctx.application is not None else None),
                account_pool=lambda: ctx.account_pool,
                may_run=lambda: paper.may_run_loops(),
                local_account_id=lambda: services.accounts.default.account_id,
            )
    if replay_provider is not None:
        # A replay app runs no lease over its own database: it recovers its
        # account's interrupted calls at construction, as every boot used to.
        for services_for_account in services.accounts.all():
            recover_interrupted_execution(services_for_account)
    else:
        assert runtime_instance_id is not None  # generated above for every non-replay app
        accounts = services.accounts

        async def lease_acquired() -> None:
            # The lease holder owns restart recovery: the previous holder's
            # incomplete AI runs (a standby skipped them while that holder was
            # live), then each account's interrupted calls, triggers and leases —
            # once per assembly, before any loop runs. A standby never writes
            # them; it may be serving the holder's accounts.
            runs = getattr(ctx.application.state, "runs", None) if ctx is not None and ctx.application is not None else None
            await recover_incomplete_agent_runs(services, own_runs=set(getattr(runs, "accepted", ())))
            for services_for_account in accounts.all():
                recover_interrupted_execution(services_for_account)
            # Startup books each account once — fills, the order-link backfill and
            # the daily settlement — without waiting for the first tick.
            for services_for_account in accounts.all():
                if services_for_account.account_ledger is not None:
                    await services_for_account.account_ledger.run_once()
            for services_for_account in accounts.all():
                if services_for_account.account_ledger is not None:
                    await services_for_account.account_ledger.start()
            for services_for_account in accounts.all():
                if services_for_account.autonomy is not None:
                    await services_for_account.autonomy.start()
            for services_for_account in accounts.all():
                if services_for_account.strategy_monitor is not None:
                    await services_for_account.strategy_monitor.start()
            # The background producers (news, research, reminders, reviews) belong to the holder too:
            # one owner per shared state database, never both blue-green slots (wiring/producers.py).
            if ctx is not None and ctx.application is not None:
                await start_producers(ctx.application, services)

        async def lease_lost() -> None:
            # The producers first (they place no order), then what can place an order, then the bookkeeping.
            failures = []
            if ctx is not None and ctx.application is not None:
                try:
                    await stop_producers(ctx.application, services)
                except Exception as error:
                    failures.append(error)
            for services_for_account in accounts.all():
                if services_for_account.strategy_monitor is not None:
                    try:
                        await services_for_account.strategy_monitor.stop()
                    except Exception as error:
                        failures.append(error)
            for services_for_account in accounts.all():
                if services_for_account.autonomy is not None:
                    try:
                        await services_for_account.autonomy.stop()
                    except Exception as error:
                        failures.append(error)
            for services_for_account in accounts.all():
                if services_for_account.account_ledger is not None:
                    try:
                        await services_for_account.account_ledger.stop()
                    except Exception as error:
                        failures.append(error)
            if failures:
                raise ExceptionGroup("instance lease loops did not all stop", failures)

        ctx.instance_lease = InstanceLease(
            store=services.store, instance_id=runtime_instance_id,
            build_sha=os.getenv("AI_TRADER_BUILD_SHA"),
            on_acquired=lease_acquired, on_lost=lease_lost,
        )
        if ctx.account_pool is not None or ctx.paper_accounts is not None:
            # A blue-green standby slot that takes a member's request over
            # assembles the account read-only: the lease holder already runs
            # its ordering/bookkeeping loops, and `lease_acquired` starts
            # them for every registry account once the lease lands here.
            # No lease (single process, tests) keeps assembling as before.
            lease = ctx.instance_lease
            if ctx.account_pool is not None:
                ctx.account_pool.may_run_loops = lambda: lease is None or lease.role != "standby"
            if ctx.paper_accounts is not None:
                ctx.paper_accounts.may_run_loops = lambda: lease is None or lease.role != "standby"
    # Order links change between provider snapshots; push the activity right away.
    lifespan = build_lifespan(ctx)

    application = FastAPI(
        title="Sente Runtime", version=__version__,
        description=(
            "TqKq 模拟盘默认不设平台手数或次数上限，也不逐单请求授权；每次动作仍重新"
            "校验行情、账户、持仓和可配置风险规则。实盘始终关闭。"
        ),
        lifespan=lifespan,
        # The registry-derived skill gate for every owned write route; see
        # write_skill_gate — reads and unowned routes pass straight through.
        dependencies=[Depends(write_skill_gate)],
    )
    # add_middleware builds innermost-first: CORS sits just above the router,
    # the origin gate and client-compat answer wrap it, and the disconnect guard
    # is the outermost user layer so a vanished client ends the request quietly
    # instead of surfacing a framework-only ExceptionGroup as a 500.
    application.add_middleware(
        CORSMiddleware, allow_origins=sorted(settings.origins), allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "X-AI-Trader-Client", ACCOUNT_HEADER],
        expose_headers=["X-AI-Trader-Client-Compat", "X-AI-Trader-Runtime", "X-AI-Trader-Min-Client"],
    )
    application.state.client_builds_seen = set()
    application.add_middleware(
        OriginAndClientCompat, origins=settings.origins,
        builds_seen=application.state.client_builds_seen, logger=log,
    )
    application.add_middleware(DisconnectGuard)

    application.state.broker = broker
    application.state.store = store
    application.state.proposals = services.proposals
    application.state.adapter = agent.adapter
    application.state.runs = agent.runs
    application.state.lifecycle = lifecycle
    application.state.accounts = services.accounts
    application.state.account_pool = ctx.account_pool
    application.state.paper_accounts = ctx.paper_accounts
    # Presence is tracked even without a pool: harmless, and every touch point
    # can assume the tracker exists.
    application.state.presence = (ctx.account_pool.presence
                                  if ctx.account_pool is not None else PresenceTracker())
    if ctx.paper_accounts is not None:
        ctx.paper_accounts.presence = application.state.presence  # one heartbeat for both managers
    application.state.data_provider = services.accounts.default.provider
    application.state.runtime_feed = ctx.runtime_feed
    application.state.account_ledger = services.accounts.default.account_ledger
    application.state.instance_lease = ctx.instance_lease
    application.state.simulation_orders = services.accounts.default.simulation_orders
    application.state.research = services.research
    # Browser tests (the fake research flag): the recorded fake model also designs strategies and
    # writes the 回测 verdict sentence (strategy_workbench.fixed_verdict_model); None otherwise.
    application.state.fixed_strategy_model = services.research_desk.fixed_generator
    application.state.research_live = services.research_live
    application.state.autonomy = services.accounts.default.autonomy
    application.state.strategy_monitor = services.accounts.default.strategy_monitor
    application.state.backtest_service = services.backtests
    application.state.backtest_router = agent.backtest_router
    application.state.backtest_coordinator = services.backtest_coordinator
    application.state.cloud_workspace_manager = agent.workspace_manager
    application.state.runtime_state_root = settings.state_root
    application.state.auth = auth
    application.state.agent_mode = settings.agent_mode

    ctx.application = application
    # The account-resolution context for late-registered surfaces that need it
    # (the strategy explanations service and its tests).
    application.state.ctx = ctx

    def create_replay_child(root: Path, child_provider: Any, user_id: str, *, configure: bool = True) -> FastAPI:
        child = create_replay_app(ctx, root, child_provider)
        # A replay is the user's own isolated TqSim account: they own it. Without the grant the
        # hand-driven paths that check the account role (挂/撤保护, 开仓卡顺带止损止盈) refuse every
        # cloud user with account_not_granted; only the local-token user had one by default.
        child_account = child.state.accounts.default.account_id
        if child.state.store.account_role(account_id=child_account, user_id=user_id) is None:
            child.state.store.grant_account(account_id=child_account, user_id=user_id, role="owner",
                                            granted_by="replay")
        if configure:
            from .replay import copy_replay_configuration
            copy_replay_configuration(store, child, user_id, child_provider.snapshot["symbol"])
        return child

    ctx.create_replay_child = create_replay_child
    register_all(application, ctx)
    from .order_watch import OrderWatch
    application.state.order_watch = OrderWatch(application)
    from .price_alerts import PriceAlertMonitor
    application.state.price_alerts = PriceAlertMonitor(application)
    from .voice_realtime import register_voice_realtime
    register_voice_realtime(application, ctx)

    # 主动提醒 (2026-09): a monitoring strategy's signal changed — the preference gates it here (one
    # place for main and replay runtimes alike), then every attached session of the strategy's owner
    # gets one transient event (the client draws the annotations and shows the light notice) and an
    # active voice call hears the one spoken line.
    async def _strategy_signal_alert(user_id: str, payload: dict) -> None:
        if not services.store.user_preferences(user_id).notify.strategySignalAlerts:
            return
        for session_id in services.broker.subscribed_session_ids():
            try:
                if str(services.store.get_session(session_id).get("userId") or "") != user_id:
                    continue
            except KeyError:
                continue
            await services.broker.publish_transient(
                session_id, kind="result", name="feed.strategy-signal", payload=payload
            )
        voice = getattr(application.state, "voice_realtime", None)
        if voice is not None:
            # Named by the strategy (#932): another strategy's signal within the minute is still heard.
            voice.announce("strategy-signal", str(payload.get("spoken") or ""), user_id=user_id,
                           key=str(payload.get("strategyId") or "") or None)

    for account in services.accounts.all():
        if account.strategy_monitor is not None:
            account.strategy_monitor.signal_alert_sink = _strategy_signal_alert
            # 执行方式 reminders, strategy cards and automatic pauses are spoken on a live call.
            account.strategy_monitor.voice_lookup = lambda: getattr(application.state, "voice_realtime", None)
        if account.autonomy is not None:
            # 保护触发 (止损/止盈) is spoken on a live call too.
            account.autonomy.voice_lookup = lambda: getattr(application.state, "voice_realtime", None)
    from .user_preferences import register_user_preferences
    register_user_preferences(application, ctx.current_user)
    from .guest import register as register_guest
    register_guest(application, ctx)
    from .agent_api import register_agent_api
    register_agent_api(application, ctx, settings.state_root,
                       "tauri://localhost" if "tauri://localhost" in settings.origins else sorted(settings.origins)[0])
    if replay_provider is None:
        # 锁屏推送 (2026-10-06): the main runtime's push hub (no key file → "not configured", every other
        # feature unchanged), the 收盘前/风险度 loop that no longer needs a client open, and 盘中异动.
        from .account_reminders import AccountReminderLoop
        from .movement_alerts import MovementWatch
        from .web_push import PRIVATE_KEY_SECRET, PushHub, load_vapid
        try:
            vapid_text = secret_value(PRIVATE_KEY_SECRET)
        except RuntimeError as error:
            log.warning("webpush_key_unreadable reason=%s", error)
            vapid_text = ""
        hub = PushHub(application, load_vapid(vapid_text))
        application.state.push = hub
        store.push_hub = hub
        services.research_desk.on_pregenerated = hub.morning_desk
        application.state.account_reminder_loop = AccountReminderLoop(application)
        application.state.movement_watch = MovementWatch(application)
        from .reminders import ReminderRetention
        application.state.reminder_retention = ReminderRetention(application)
    # 统一提醒 (#932): the 提醒中心 routes and the one delivery entry bound to this app's store (the account
    # loops reach it through the store). A replay child binds its own: its reminders stay in its store and
    # conversations, its spoken lines go to the main app's calls bound to that replay.
    from .reminders import bind_store, register_reminders
    register_reminders(application, ctx.current_user)
    bind_store(application)
    return application


app = create_app()
