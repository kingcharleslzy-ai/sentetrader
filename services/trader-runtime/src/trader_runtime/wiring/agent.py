"""Choose the agent adapter (native model loop, fake, replay, or the per-tenant cloud router)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import __version__
from ..backtests import BacktestService
from ..execution_boundaries import TrustedTemplateExecutionBoundary
from ..fake_agent import FakeAgentAdapter
from ..native_agent import NativeAgentAdapter, build_model_endpoints
from ..release_manager import StrategyReleaseManager
from ..runs import RunManager
from ..tenant_adapters import CloudWorkspaceManager, SessionAgentRouter, UserBacktestRouter
from .config import RuntimeSettings
from .services import RuntimeServices


@dataclass
class AgentWiring:
    native_endpoints: dict[str, Any]
    workspace_manager: CloudWorkspaceManager | None
    backtest_router: UserBacktestRouter | None
    adapter: Any
    runs: RunManager


def build_agent(
    settings: RuntimeSettings, services: RuntimeServices, *, native_transport: Any | None,
    deepseek_api_key: str | None, replay_provider: Any | None,
) -> AgentWiring:
    store, broker = services.store, services.broker
    default_account = services.accounts.default
    provider = default_account.provider
    proposals, autonomy = services.proposals, default_account.autonomy
    simulation_orders = default_account.simulation_orders
    backtests, backtest_coordinator = services.backtests, services.backtest_coordinator
    # One endpoint per catalog model whose provider key is present; the
    # account's preference picks from this pool per turn.
    native_endpoints = build_model_endpoints(
        default_provider=settings.agent_provider, deepseek_api_key=deepseek_api_key,
    )
    workspace_manager: CloudWorkspaceManager | None = None
    backtest_router: UserBacktestRouter | None = None
    if settings.auth_mode == "cloud-jwt":
        workspace_manager = CloudWorkspaceManager(template_root=settings.engineering_root, state_root=settings.state_root)

        def user_backtest_factory(user_id: str) -> BacktestService:
            user_workspace = workspace_manager.prepare(user_id)
            namespace = CloudWorkspaceManager.key(user_id)
            user_state_root = settings.state_root / "cloud-artifacts" / namespace
            user_releases = StrategyReleaseManager(
                engineering_root=user_workspace, state_root=user_state_root, store=store
            )
            return BacktestService(
                store=store, releases=user_releases,
                engineering_root=user_workspace, state_root=user_state_root,
                execution_boundary=TrustedTemplateExecutionBoundary(settings.engineering_root),
                coordinator=backtest_coordinator,
            )

        backtest_router = UserBacktestRouter(store=store, service_factory=user_backtest_factory)

        def cloud_status(mode: str, connected: bool) -> dict[str, Any]:
            payload = {
                "mode": mode,
                "adapter": "native-agent" if settings.agent_mode == "native" else "fake-agent",
                "runtimeConnected": True,
                "agentConnected": connected,
                "connected": connected,
                "version": __version__,
                **provider.runtime_status(),
            }
            if settings.agent_mode == "fake":
                payload.update({"agentNotice": "云端 AI 尚未配置隔离执行环境。"})
            return payload

        if settings.agent_mode == "native":
            def adapter_factory(user_id: str):
                user_workspace = workspace_manager.prepare(user_id)
                return NativeAgentAdapter(
                    store=store, broker=broker, workspace_root=user_workspace,
                    endpoints=native_endpoints, provider=settings.agent_provider,
                    data_provider=provider, backtests=backtest_router.for_user(user_id),
                    proposals=proposals, accounts=services.accounts,
                    research_brief=services.research_brief,
                    research_live=services.research_live,
                    http_transport=native_transport,
                )
        elif settings.agent_mode == "fake":
            def adapter_factory(user_id: str):
                return FakeAgentAdapter(
                    broker, proposals, provider, backtest_router.for_user(user_id),
                    status_overrides={"agentNotice": "云端 AI 尚未配置隔离执行环境。"},
                )
        else:
            raise ValueError("invalid_agent_mode")
        adapter: Any = SessionAgentRouter(
            store=store,
            adapter_factory=adapter_factory,
            adapter_id="native-agent" if settings.agent_mode == "native" else "fake-agent",
            runtime_status_factory=cloud_status,
        )
    elif settings.agent_mode == "native":
        adapter = NativeAgentAdapter(
            store=store, broker=broker, workspace_root=settings.engineering_root,
            endpoints=native_endpoints, provider=settings.agent_provider,
            data_provider=provider, backtests=backtests, proposals=proposals, autonomy=autonomy,
            accounts=services.accounts,
            research_brief=services.research_brief,
            research_live=services.research_live,
            status_overrides={"simulationTrading": simulation_orders.enabled},
            http_transport=native_transport,
        )
    elif settings.agent_mode == "fake":
        from ..replay import ReplayAdapter
        adapter_class = ReplayAdapter if replay_provider is not None else FakeAgentAdapter
        adapter = adapter_class(
            broker,
            proposals,
            provider,
            backtests,
            status_overrides={"simulationTrading": simulation_orders.enabled},
        )
    else:
        raise ValueError("invalid_agent_mode")
    runs = RunManager(store, broker, adapter)
    return AgentWiring(native_endpoints=native_endpoints, workspace_manager=workspace_manager,
                       backtest_router=backtest_router, adapter=adapter, runs=runs)
