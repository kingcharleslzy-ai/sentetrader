from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol


class CloudAgentRunner(Protocol):
    """Narrow future interface for an OS-isolated per-tenant Agent runner."""

    def create_adapter(
        self, *, user_id: str, workspace_root: Path, state_root: Path, **dependencies: Any,
    ) -> Any: ...


class TenantExecutionBoundary(Protocol):
    """Authorizes immutable inputs before any strategy code can execute."""

    def authorize(
        self, *, strategy_path: str, strategy_digest: str,
        data_path: str, data_digest: str,
    ) -> None: ...


class LocalExecutionBoundary:
    """Local desktop users may execute code in their selected engineering project."""

    def authorize(
        self, *, strategy_path: str, strategy_digest: str,
        data_path: str, data_digest: str,
    ) -> None:
        del strategy_path, strategy_digest, data_path, data_digest


class TrustedTemplateExecutionBoundary:
    """Cloud fallback: only byte-identical bundled templates may be executed."""

    def __init__(self, template_root: Path) -> None:
        self.template_root = template_root.resolve()
        self._digests = {
            relative: hashlib.sha256((self.template_root / relative).read_bytes()).hexdigest()
            for relative in (
                "workspace/strategies/rb_trend.py",
                "workspace/backtests/fixtures/rb_monthly_v1.csv",
            )
        }

    def authorize(
        self, *, strategy_path: str, strategy_digest: str,
        data_path: str, data_digest: str,
    ) -> None:
        if (
            self._digests.get(strategy_path) != strategy_digest
            or self._digests.get(data_path) != data_digest
        ):
            raise RuntimeError("isolated_runner_required")
