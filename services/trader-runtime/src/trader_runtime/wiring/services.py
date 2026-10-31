"""Build the stateful services: store, broker, one service set per trading account."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..account_ledger import AccountLedger
from ..autonomy import AutonomousSimulationService
from ..backtest_coordinator import BacktestCoordinator
from ..backtest_settings import load_backtest_settings
from ..backtests import BacktestService
from ..broker import EventBroker
from ..daily_budget import DailyCallBudget
from ..data_providers import (
    FakeReadOnlyDataProvider,
    ReadOnlyDataProvider,
    SidecarReadOnlyDataProvider,
)
from ..exchange_calendar import calendar_trading_days
from ..foreign_market import ForeignMarketService, build_sources
from ..instance_lease import LEASE_SECONDS
from ..last_good import LastGood
from ..news_analyst import NewsAnalyst
from ..news_screen import NewsScreener
from ..news_search import NewsSearchService
from ..news_translate import NewsTranslator
from ..order_backfill import backfill_order_fills
from ..proposals import ProposalService
from ..release_manager import StrategyReleaseManager
from ..research_brief import ResearchBriefService
from ..research_data import ResearchDataService
from ..research_debate import ResearchDebateService
from ..research_desk import ResearchDeskService
from ..research_live import LiveSources, ResearchLiveService
from ..risk import InstrumentSpecProvider, RiskPolicyProvider
from ..simulation_orders import SimulationGateway, SimulationOrderService
from ..store import RuntimeStore
from ..strategy_monitor import StrategyMonitorService
from ..testing.fake_gateway import ENV_FLAG as FAKE_GATEWAY_FLAG
from ..testing.fake_gateway import FakeSimulationGateway
from ..testing.fake_research_live import ENV_FLAG as FAKE_RESEARCH_LIVE_FLAG
from ..testing.fake_research_live import (
    FakeForeignSource,
    FakeLiveSources,
    FakeNewsModel,
    FakeResearchData,
    FakeWebSearch,
)
from ..trading_costs import FeeSettings
from ..web_search import WebSearchClient
from .accounts import (
    AccountRegistry,
    AccountServices,
    AccountSpec,
    build_account_provider,
    load_account_specs,
)
from .config import REPO_ROOT, RuntimeSettings, _migrate_legacy_database

log = logging.getLogger("uvicorn.error.account_assembly")

# The account ledger's idle bounds (the wake-ups only make a pass run sooner).
# Module constants, not env knobs: tests monkeypatch them (like voice_realtime's
# bounds) so a ledger pass never waits real seconds, without a deployed knob.
LEDGER_TICK_SECONDS = 30.0
LEDGER_MIN_INTERVAL = 5.0


@dataclass
class RuntimeServices:
    store: RuntimeStore
    recovered_runs: list[dict[str, str]]
    broker: EventBroker
    releases: StrategyReleaseManager
    backtest_coordinator: BacktestCoordinator
    backtests: BacktestService
    accounts: AccountRegistry
    policies: RiskPolicyProvider
    instruments: InstrumentSpecProvider
    proposals: ProposalService
    research: ResearchDataService
    web_search: WebSearchClient
    news_search: NewsSearchService
    research_brief: ResearchBriefService
    research_desk: ResearchDeskService
    research_live: ResearchLiveService
    foreign_market: ForeignMarketService
    # 多 AI 研讨 (2026-10-11): debates over the desk's facts, written back through the desk.
    research_debate: ResearchDebateService
    # 525-A: whether incomplete runs were already recovered by this assembly —
    # at construction, or by the lease holder when a standby skipped it.
    runs_recovered: bool = False


def build_account_services(
    spec: AccountSpec, *, store: RuntimeStore, broker: EventBroker,
    policies: RiskPolicyProvider, instruments: InstrumentSpecProvider,
    auth_mode: str, simulation_gateway: SimulationGateway | None,
    simulation_trading: bool, fake_gateway: bool = False,
    replay_provider: Any | None = None, injected: ReadOnlyDataProvider | None = None,
    is_default: bool = False, fee_settings: Any | None = None,
) -> AccountServices:
    """One account's order/autonomy wiring — the same construction the static
    boot loop and the on-demand pool account assembly share."""
    provider = build_account_provider(spec, injected=injected)
    resolved_gateway = simulation_gateway if is_default else None
    if simulation_trading:
        # The sidecar-backed kinds trade through their own provider's order
        # gateway: the owner's TqKq sidecar account and a paper account's
        # site-hosted matching engine.
        if (spec.provider_kind not in ("tqkq-sidecar", "paper")
                or not isinstance(provider, SidecarReadOnlyDataProvider)):
            raise RuntimeError("simulation_trading_requires_tqkq_sidecar")
        # 快期模拟停用交易（所有者 2026-10-30，#1205）：配置了本站模拟盘（paper_accounts.
        # PAPER_ENGINE_URL_ENV）时快期账户只供行情与历史，没有下单网关，委托与撤单回
        # simulation_gateway_disabled；没有本站模拟盘的部署照旧。
        retired = spec.provider_kind == "tqkq-sidecar" and bool(os.getenv("AI_TRADER_PAPER_ENGINE_URL", "").strip())
        resolved_gateway = None if retired else provider
    if fake_gateway:
        # Browser tests only: the gateway double may stand in solely for the
        # local, token-authenticated, fake-data runtime, never beside a real one.
        if (not is_default or auth_mode != "local-token"
                or not isinstance(provider, FakeReadOnlyDataProvider)
                or replay_provider is not None or resolved_gateway is not None):
            raise RuntimeError("fake_simulation_gateway_requires_local_fake_runtime")
        resolved_gateway = FakeSimulationGateway()
    simulation_orders = SimulationOrderService(
        store=store, data_provider=provider, gateway=resolved_gateway,
        policies=policies, instruments=instruments,
        trading_clock=replay_provider.clock if replay_provider is not None else None,
        historical_price_limits_unavailable=replay_provider is not None,
        trading_account_id=spec.id,
        fee_settings=fee_settings,
    )
    autonomy = AutonomousSimulationService(
        store=store,
        broker=broker,
        simulation_orders=simulation_orders,
        data_provider=provider,
        allow_cloud_owner=auth_mode == "owner-password",
        trading_account_id=spec.id,
    )
    return AccountServices(
        account_id=spec.id, spec=spec, provider=provider,
        simulation_orders=simulation_orders, autonomy=autonomy,
    )


def attach_account_loops(
    services_for_account: AccountServices, *, store: RuntimeStore, proposals: ProposalService,
) -> None:
    """The monitor and ledger loops every account gets once the registry and the
    shared proposal service exist."""
    services_for_account.strategy_monitor = StrategyMonitorService(
        store=store, proposals=proposals, autonomy=services_for_account.autonomy,
        trading_account_id=services_for_account.account_id,
    )
    services_for_account.account_ledger = AccountLedger(
        store=store, provider=services_for_account.provider,
        # Bounds only (the wake-ups make it run sooner); the constants above say why.
        tick_seconds=LEDGER_TICK_SECONDS,
        min_interval=LEDGER_MIN_INTERVAL,
        trading_account_id=services_for_account.account_id,
    )


def wire_orders_on_change(services_for_account: AccountServices, *, store: RuntimeStore, runtime_feed: Any) -> None:
    """Order links change between provider snapshots; push the activity right
    away. The static boot loop and every on-demand assembler (pool and paper
    accounts, through `assemble_account`) share this wiring, so no account's
    fills wait for the next snapshot."""
    def orders_changed() -> None:
        # A link that just reached FINISHED may never get a fill report (the
        # sidecar only serves today's); synthesize its fill for the review now.
        backfill_order_fills(
            store, services_for_account.provider, trading_account_id=services_for_account.account_id,
        )
        runtime_feed.orders_changed()

    services_for_account.simulation_orders.on_change = orders_changed


def recover_interrupted_execution(services_for_account: AccountServices) -> None:
    """Restart recovery for one account, once per assembly: submit/cancel calls
    and stop triggers a previous holder left mid-flight become UNKNOWN, its AI
    execution leases expire. Only the side that runs the account's loops calls
    this — the lease holder (`lease_acquired`), an on-demand assembler allowed
    to run loops (`start_account_loops`), a replay app over its own database.
    Never at construction: building another account's services, or a
    blue-green standby assembling read-only, must not touch these rows."""
    if services_for_account.recovered:
        return
    services_for_account.simulation_orders.recover_interrupted_calls()
    services_for_account.autonomy.recover_interrupted_execution()
    services_for_account.recovered = True


async def start_account_loops(services_for_account: AccountServices) -> None:
    """Bring one on-demand account's loops up the way `lease_acquired` does at
    boot: recover what a previous holder left, book the account once (this also
    backfills the day's fills after a cold re-assembly), then the loops that can
    act on it."""
    recover_interrupted_execution(services_for_account)
    if services_for_account.account_ledger is not None:
        await services_for_account.account_ledger.run_once()
        await services_for_account.account_ledger.start()
    await services_for_account.autonomy.start()
    if services_for_account.strategy_monitor is not None:
        await services_for_account.strategy_monitor.start()


async def stop_account_loops(services_for_account: AccountServices) -> None:
    """`lease_lost`'s order: what can place an order stops first. Each stop is
    a no-op for a loop that never started."""
    if services_for_account.strategy_monitor is not None:
        await services_for_account.strategy_monitor.stop()
    await services_for_account.autonomy.stop()
    if services_for_account.account_ledger is not None:
        await services_for_account.account_ledger.stop()


def _wire_account(
    services_for_account: AccountServices, *, store: RuntimeStore, runtime_feed: Any,
    publish_status: Callable[[], Coroutine[Any, Any, None]],
) -> Callable[[], None]:
    """The subscriptions the static boot wires per provider in the lifespan:
    snapshots feed the runtime feed and this account's ledger; a provider
    status change re-publishes runtime.status to the account's sessions;
    order-link changes push the activity right away. Returns the undo."""
    loop = asyncio.get_running_loop()
    ledger = services_for_account.account_ledger

    def snapshots_arrived() -> None:
        runtime_feed.snapshot_arrived()
        if ledger is not None:
            ledger.snapshot_arrived()

    subscribe_snapshot = getattr(services_for_account.provider, "subscribe_snapshot", None)
    unsubscribe_snapshot: Callable[[], None] | None = None
    if subscribe_snapshot is not None:
        unsubscribe_snapshot = subscribe_snapshot(
            lambda: loop.call_soon_threadsafe(snapshots_arrived) if not loop.is_closed() else None
        )
    status_tasks: set[asyncio.Task[None]] = set()

    def schedule_status(_: dict[str, object]) -> None:
        def schedule() -> None:
            task = asyncio.create_task(publish_status())
            status_tasks.add(task)
            task.add_done_callback(status_tasks.discard)
        if not loop.is_closed():
            loop.call_soon_threadsafe(schedule)

    unsubscribe_status = services_for_account.provider.subscribe_status(schedule_status)
    wire_orders_on_change(services_for_account, store=store, runtime_feed=runtime_feed)

    def unwire() -> None:
        services_for_account.simulation_orders.on_change = None
        if unsubscribe_snapshot is not None:
            unsubscribe_snapshot()
        unsubscribe_status()

    return unwire


async def dismantle_account(services_for_account: AccountServices, unwire: Callable[[], None]) -> None:
    """Undo `assemble_account` (already out of the registry): loops, then the
    subscriptions, then this process's provider."""
    await stop_account_loops(services_for_account)
    unwire()
    await services_for_account.provider.stop()


async def assemble_account(
    services_for_account: AccountServices, *, registry: AccountRegistry, store: RuntimeStore,
    proposals: ProposalService, runtime_feed: Any, publish_status: Callable[[], Coroutine[Any, Any, None]],
    run_loops: bool,
) -> Callable[[], None]:
    """Register one on-demand account (pool or paper) and bring it up: loops
    attached, subscriptions wired, provider started and — on the side allowed to
    run them — the loops started. Returns the undo `dismantle_account` takes.

    All or nothing: a failure or a cancellation part-way takes the account back
    out of the registry and dismantles what was brought up before re-raising,
    so the next `ensure` assembles afresh instead of returning a half-built
    service set. The dismantle is shielded — a second cancellation cannot leave
    a started provider or loop without an owner."""
    attach_account_loops(services_for_account, store=store, proposals=proposals)
    registry.add(services_for_account)
    unwire: Callable[[], None] = lambda: None  # noqa: E731
    try:
        unwire = _wire_account(services_for_account, store=store, runtime_feed=runtime_feed,
                               publish_status=publish_status)
        await services_for_account.provider.start()
        if run_loops:
            await start_account_loops(services_for_account)
    except BaseException:
        registry.remove(services_for_account.account_id)
        try:
            await asyncio.shield(dismantle_account(services_for_account, unwire))
        except Exception:
            # The assembly's own error is the one the caller needs.
            log.exception("account_assembly_cleanup_failed account=%s", services_for_account.account_id)
        raise
    return unwire


def build_services(
    settings: RuntimeSettings, *, data_provider: ReadOnlyDataProvider | None,
    simulation_gateway: SimulationGateway | None, replay_provider: Any | None,
    execution_owner_user_id: str,
    account_providers: Mapping[str, ReadOnlyDataProvider] | None = None,
) -> RuntimeServices:
    if settings.migrate_legacy_database:
        _migrate_legacy_database(REPO_ROOT / ".runtime" / "ai-trader.sqlite3", settings.database_path)
    specs = load_account_specs(
        settings, execution_owner_user_id=execution_owner_user_id, replay_provider=replay_provider,
    )
    store = RuntimeStore(settings.database_path)
    for spec in specs:
        if spec.rename_from:
            # The one-time fingerprint rename runs before the new id is ensured,
            # so the conflict check inside sees a free id, and before anything
            # writes under this boot's ids.
            store.rename_trading_account(old_id=spec.rename_from, new_id=spec.id)
    for spec in specs:
        store.ensure_trading_account(
            spec.id,
            execution_owner_user_id=spec.execution_owner_user_id or execution_owner_user_id,
            provider_kind=spec.provider_kind,
            display_name=spec.display_name,
            sidecar_url=spec.sidecar_url,
            sidecar_token_file=spec.sidecar_token_file,
            grants=spec.grants,
        )
    store.default_trading_account_id = specs[0].id
    if specs[0].grants is None:
        # The 2026-09-23 isolation migration: deployments that never declared the
        # default account's grants used to auto-grant every cloud user operator
        # on it at boot and at registration. The mechanisms are gone; this boots
        # the leftovers out. A declared account skips it — its grant set is
        # rewritten from the file in the ensure call above anyway.
        store.purge_default_account_auto_grants(account_id=specs[0].id)
    store.disable_trading_accounts_except([spec.id for spec in specs])
    # 525-A: during a blue-green deploy the serving slot still holds the lease
    # with a fresh heartbeat while this process constructs. Its runs are
    # executing there, not abandoned — recovery waits for the lease
    # (`recover_incomplete_agent_runs`), the same holder-only rule the account
    # recovery follows.
    stale_before = (datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS)).isoformat()
    live_holder = store.live_lease_holder(stale_before=stale_before)
    runs_recovered = live_holder is None
    recovered_runs = store.recover_incomplete_runs() if runs_recovered else []
    broker = EventBroker(store)
    releases = StrategyReleaseManager(
        engineering_root=settings.engineering_root, state_root=settings.state_root, store=store
    )
    backtest_config = load_backtest_settings()
    backtest_coordinator = BacktestCoordinator(
        global_limit=backtest_config.global_limit,
        per_user_limit=backtest_config.user_limit,
        global_queue_limit=backtest_config.global_queue_limit,
        per_user_queue_limit=backtest_config.user_queue_limit,
        timeout_seconds=backtest_config.timeout_seconds,
    )
    backtests = BacktestService(
        store=store, releases=releases, engineering_root=settings.engineering_root, state_root=settings.state_root,
        coordinator=backtest_coordinator,
    )
    policies = RiskPolicyProvider(store, default_allowed_symbols=(
        (replay_provider.snapshot["symbol"].split(".")[-1].upper(),) if replay_provider is not None else None
    ))
    instruments = InstrumentSpecProvider()
    injected_providers = dict(account_providers or {})
    simulation_trading = os.getenv("AI_TRADER_SIMULATION_TRADING") == "1" and replay_provider is None
    fake_gateway = os.getenv(FAKE_GATEWAY_FLAG) == "1"
    assembled: dict[str, AccountServices] = {}
    for index, spec in enumerate(specs):
        is_default = index == 0
        injected = injected_providers.get(spec.id)
        if injected is None and is_default:
            injected = data_provider
        assembled[spec.id] = build_account_services(
            spec, store=store, broker=broker, policies=policies, instruments=instruments,
            auth_mode=settings.auth_mode, simulation_gateway=simulation_gateway,
            simulation_trading=simulation_trading, fake_gateway=fake_gateway,
            replay_provider=replay_provider, injected=injected, is_default=is_default,
            fee_settings=FeeSettings(settings.state_root),
        )
    accounts = AccountRegistry(assembled[specs[0].id], {
        account_id: services for account_id, services in assembled.items() if account_id != specs[0].id
    })
    proposals = ProposalService(
        store, broker, policies=policies, instruments=instruments,
        data_provider=accounts.default.provider, simulation_orders=accounts.default.simulation_orders,
        accounts=accounts, fee_settings=FeeSettings(settings.state_root),
    )
    for account in accounts.all():
        attach_account_loops(account, store=store, proposals=proposals)
    fake_live = os.getenv(FAKE_RESEARCH_LIVE_FLAG) == "1"
    research = (FakeResearchData if fake_live else ResearchDataService)(connection=store.connection, lock=store.lock)
    # One exchange calendar for every trading-day judgment (#976 step 6): research reads the 快期 calendar too.
    research.sources.calendar = lambda: calendar_trading_days(accounts.default.provider)
    web_search = WebSearchClient(budget=DailyCallBudget("news-search", store.connection, store.lock),
                                 daily_limit=int(os.getenv("AI_TRADER_NEWS_SEARCH_DAILY_CALLS", "600")))
    research_brief = ResearchBriefService(research=research, web_search=web_search, instrument_catalog=accounts.default.provider.instrument_catalog)
    live_sources = LiveSources()
    screener = NewsScreener(connection=store.connection, lock=store.lock)
    analyst = NewsAnalyst(connection=store.connection, lock=store.lock, search=web_search)
    translator = NewsTranslator(connection=store.connection, lock=store.lock)
    fake_model: FakeNewsModel | None = None
    if fake_live:
        # Browser tests only: recorded 快讯/日历/沪金研究数据 samples stand in for AKShare on the fake-data runtime,
        # and a recorded fake model screens them, writes the 今日新闻要点 and the desk's 今日判断 (no key, no network).
        if not isinstance(accounts.default.provider, FakeReadOnlyDataProvider) or settings.auth_mode != "local-token":
            raise RuntimeError("fake_research_live_requires_local_fake_runtime")
        live_sources = FakeLiveSources()
        fake_model = FakeNewsModel()
        analyst.search = None  # recorded browser-test events never fetch external pages
        screener.generator = analyst.generator = translator.generator = lambda: fake_model
    # 外盘: the one foreign-quote entry (board, research, AI). Seconds-long quotes keep their last good value in
    # memory only; the 快期 source is bound to the shared market in the lifespan.
    foreign_market = ForeignMarketService(LastGood(), [FakeForeignSource()] if fake_live else build_sources())
    research_live = ResearchLiveService(connection=store.connection, lock=store.lock, state_root=settings.state_root,
                                        instrument_catalog=accounts.default.provider.instrument_catalog,
                                        sources=live_sources, screener=screener, analyst=analyst, translator=translator,
                                        foreign=foreign_market)
    research_brief.live = research_live  # 相关报道 go through the same product screening
    research_desk = ResearchDeskService(research=research, news=lambda symbol: research_brief.brief(symbol, "news"),
                                        provider=lambda: accounts.default.provider, extra=research_live)
    research_brief.desk = research_desk
    research_desk.fixed_generator = fake_model
    analyst.price_for = research_desk.price_line  # the analyst reads the desk's latest price fact when it has one
    # The analysts fill gaps through the same search client and daily budget (a recorded fake on the fake-data runtime).
    research_debate = ResearchDebateService(desk=research_desk, connection=store.connection, lock=store.lock,
                                            events=research_live.product_events, event_products=research_live.event_products,
                                            search=FakeWebSearch() if fake_live else web_search)
    return RuntimeServices(
        store=store, recovered_runs=recovered_runs, runs_recovered=runs_recovered, broker=broker,
        releases=releases, research=research,
        web_search=web_search, news_search=NewsSearchService(research_desk, research_live, web_search),
        research_brief=research_brief, research_desk=research_desk, research_live=research_live,
        foreign_market=foreign_market,
        research_debate=research_debate,
        backtest_coordinator=backtest_coordinator, backtests=backtests, accounts=accounts,
        policies=policies, instruments=instruments, proposals=proposals,
    )


async def recover_incomplete_agent_runs(services: RuntimeServices, *, own_runs: set[str] | frozenset[str] = frozenset()) -> None:
    """525-A: the run recovery a standby skipped at construction because another
    slot held a live lease — run once this process acquires the lease, once per
    assembly (the same rule `recover_interrupted_execution` follows), with the
    same restart notice a boot that recovers at construction writes."""
    if services.runs_recovered:
        return
    # Runs this process accepted while it served as standby are its own, not leftovers.
    for recovered in services.store.recover_incomplete_runs(exclude=own_runs):
        await services.broker.publish(recovered["sessionId"], kind="error", name="error", payload={
            "code": "runtime_restarted", "message": "Runtime 重启，未完成任务已安全终止。",
            "runId": recovered["runId"], "scope": "task",
        })
    services.runs_recovered = True
