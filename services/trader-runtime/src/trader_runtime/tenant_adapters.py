from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .data_providers import ReadOnlyDataProvider, StatusSubscriber, Unsubscribe
from .store import RuntimeStore


class UserDataProviderRouter(Protocol):
    """Future tenant boundary for account, market, status and provider lifecycle.

    A path-scoped workspace is not an account-data isolation boundary. Cloud
    runtimes must keep account-backed providers disabled until one router owns
    every provider consumer and lifecycle callback for the authenticated user.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def subscribe_status(self, subscriber: StatusSubscriber) -> Unsubscribe: ...

    def for_user(self, user_id: str) -> ReadOnlyDataProvider: ...

    def for_session(self, session_id: str, *, user_id: str) -> ReadOnlyDataProvider: ...

    def runtime_status_for_user(self, user_id: str) -> dict[str, Any]: ...


class CloudWorkspaceManager:
    """Creates per-user paths; these paths are not an OS security boundary."""

    def __init__(self, *, template_root: Path, state_root: Path) -> None:
        self.template_root = template_root.resolve()
        self.root = state_root.resolve() / "cloud-users"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(user_id: str) -> str:
        return hashlib.sha256(user_id.encode()).hexdigest()

    @staticmethod
    def _make_editable(root: Path) -> None:
        root.chmod(0o755)
        for item in root.rglob("*"):
            if item.is_symlink():
                raise RuntimeError("cloud_workspace_symlink_forbidden")
            item.chmod(0o755 if item.is_dir() else 0o644)

    def prepare(self, user_id: str) -> Path:
        user_root = self.root / self.key(user_id)
        project = user_root / "project"
        if not project.exists():
            temporary = Path(tempfile.mkdtemp(prefix="cloud-workspace-", dir=self.root))
            try:
                source_workspace = self.template_root / "workspace"
                if not source_workspace.is_dir():
                    raise RuntimeError("cloud_workspace_template_missing")
                shutil.copytree(source_workspace, temporary / "workspace", symlinks=True)
                self._make_editable(temporary / "workspace")
                (temporary / "README.md").write_text(
                    "# AI Trader Cloud Workspace\n\n隔离的策略、测试与回测工程区；不含账户凭据或交易网关。\n",
                    encoding="utf-8",
                )
                user_root.mkdir(parents=True, exist_ok=True)
                temporary.rename(project)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        return project


class UserBacktestRouter:
    """Returns the same user-scoped service to REST, Fake Agent and future runners."""

    def __init__(
        self, *, store: RuntimeStore, service_factory: Callable[[str], Any],
    ) -> None:
        self.store = store
        self.service_factory = service_factory
        self.services: dict[str, Any] = {}

    def for_user(self, user_id: str) -> Any:
        service = self.services.get(user_id)
        if service is None:
            service = self.service_factory(user_id)
            self.services[user_id] = service
        return service

    def for_session(self, session_id: str, *, user_id: str) -> Any:
        session = self.store.get_session(session_id)
        if str(session["userId"]) != user_id:
            raise KeyError(session_id)
        return self.for_user(user_id)


class SessionAgentRouter:
    """Routes every cloud subject to a distinct Agent adapter and process lifecycle."""

    def __init__(
        self, *, store: RuntimeStore, adapter_factory: Callable[[str], Any],
        adapter_id: str, runtime_status_factory: Callable[[str, bool], dict[str, Any]],
    ) -> None:
        self.store = store
        self.adapter_factory = adapter_factory
        self.adapter_id = adapter_id
        self.runtime_status_factory = runtime_status_factory
        self.adapters: dict[str, Any] = {}
        self.app_capabilities = None
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def tasks(self) -> dict[str, dict[str, Any]]:
        combined: dict[str, dict[str, Any]] = {}
        for adapter in self.adapters.values():
            combined.update(getattr(adapter, "tasks", {}))
        return combined

    def adapter_for_user(self, user_id: str) -> Any:
        adapter = self.adapters.get(user_id)
        if adapter is None:
            adapter = self.adapter_factory(user_id)
            if hasattr(adapter, "usage_user"):
                adapter.usage_user = user_id
            tools = getattr(adapter, "trader_tools", None)
            if tools is not None:
                tools.app_capabilities = self.app_capabilities
            self.adapters[user_id] = adapter
        return adapter

    def bind_app_capabilities(self, bridge) -> None:
        self.app_capabilities = bridge
        for adapter in self.adapters.values():
            tools = getattr(adapter, "trader_tools", None)
            if tools is not None:
                tools.app_capabilities = bridge

    def _user_for_session(self, session_id: str) -> str:
        return str(self.store.get_session(session_id)["userId"])

    async def bootstrap(self, session_id: str) -> None:
        user_id = self._user_for_session(session_id)
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            await self.adapter_for_user(user_id).bootstrap(session_id)

    async def handle_message(self, session_id: str, text: str, run_id: str, *, user_id: str | None = None, model_slug: str | None = None) -> None:
        await self.adapter_for_user(self._user_for_session(session_id)).handle_message(
            session_id, text, run_id, user_id=user_id, model_slug=model_slug
        )

    async def refresh_session(self, session_id: str) -> None:
        adapter = self.adapter_for_user(self._user_for_session(session_id))
        refresh = getattr(adapter, "refresh_session", None)
        if refresh is not None:
            await refresh(session_id)

    async def cancel_run(self, run_id: str) -> None:
        adapter = self.adapter_for_user(self.store.run_user_id(run_id))
        cancel = getattr(adapter, "cancel_run", None)
        if cancel is not None:
            await cancel(run_id)

    async def release_session(self, session_id: str) -> None:
        user_id = self._user_for_session(session_id)
        adapter = self.adapter_for_user(user_id)
        release = getattr(adapter, "release_session", None)
        if release is not None:
            await release(session_id)
        remaining = [
            session for session in self.store.active_sessions()
            if session["userId"] == user_id and session["sessionId"] != session_id
        ]
        if not remaining:
            shutdown = getattr(adapter, "shutdown", None)
            if shutdown is not None:
                await shutdown()
            self.adapters.pop(user_id, None)
            self._locks.pop(user_id, None)

    async def shutdown(self) -> None:
        adapters = list(self.adapters.values())
        self.adapters.clear()
        self._locks.clear()
        await asyncio.gather(*[
            adapter.shutdown() for adapter in adapters if hasattr(adapter, "shutdown")
        ], return_exceptions=True)

    def runtime_status_for_user(self, user_id: str, mode: str) -> dict[str, Any]:
        adapter = self.adapters.get(user_id)
        connected = bool(
            adapter is not None and adapter.runtime_status(mode).get("agentConnected")
        )
        return self.runtime_status_factory(mode, connected)

    def runtime_status_for_session(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        return self.runtime_status_for_user(str(session["userId"]), str(session["mode"]))

    def runtime_status(self, mode: str) -> dict[str, Any]:
        # Health checks without an authenticated identity never aggregate tenant state.
        return self.runtime_status_factory(mode, False)
