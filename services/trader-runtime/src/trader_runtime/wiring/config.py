"""Resolve the runtime's configuration from arguments and environment, fail closed on unsafe combinations."""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..auth import allowed_origins
from ..broker import utc_now
from ..data_providers import FakeReadOnlyDataProvider, ReadOnlyDataProvider
from ..simulation_orders import SimulationGateway

REPO_ROOT = Path(__file__).resolve().parents[5]


@dataclass(frozen=True)
class RuntimeSettings:
    engineering_root: Path
    data_mode: str
    auth_mode: str
    agent_mode: str
    agent_provider: str
    origins: set[str]
    state_root: Path
    database_path: Path
    migrate_legacy_database: bool


def _state_root(explicit_path: Path | None = None, database_path: Path | None = None) -> Path:
    if explicit_path is not None:
        return explicit_path.resolve()
    configured = os.getenv("AI_TRADER_STATE_ROOT")
    if configured:
        path = Path(configured)
        return (path if path.is_absolute() else REPO_ROOT / path).resolve()
    if database_path is not None:
        return database_path.resolve().parent
    return (Path.home() / "Library" / "Application Support" / "AI Trader").resolve()


def _database_path(explicit_path: Path | None = None, state_root: Path | None = None) -> Path:
    if explicit_path is not None:
        return explicit_path
    configured = os.getenv("AI_TRADER_DB_PATH")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else REPO_ROOT / path
    return (state_root or _state_root()) / "ai-trader.sqlite3"


def _migrate_legacy_database(source: Path, destination: Path) -> bool:
    """Atomically copy/merge Phase 1.2 state while retaining the source database."""
    if not source.is_file() or source.resolve() == destination.resolve():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.migration")
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
        source_check = source_connection.execute("PRAGMA integrity_check").fetchone()
        if source_check is None or source_check[0] != "ok":
            raise RuntimeError("legacy_database_integrity_failed")
        if destination.exists():
            current = sqlite3.connect(f"file:{destination.resolve()}?mode=ro", uri=True)
            try:
                marker_exists = current.execute(
                    """SELECT 1 FROM sqlite_master WHERE type = 'table'
                    AND name = 'ai_trader_runtime_migrations'"""
                ).fetchone()
                if marker_exists and current.execute(
                    "SELECT 1 FROM ai_trader_runtime_migrations WHERE migration_id = ?",
                    ("phase-1.2-default-database",),
                ).fetchone():
                    return False
                current_check = current.execute("PRAGMA integrity_check").fetchone()
                if current_check is None or current_check[0] != "ok":
                    raise RuntimeError("current_database_integrity_failed")
                destination_connection = sqlite3.connect(temporary)
                current.backup(destination_connection)
            finally:
                current.close()
        else:
            destination_connection = sqlite3.connect(temporary)
            source_connection.backup(destination_connection)
        if destination.exists():
            destination_connection.execute("PRAGMA foreign_keys = OFF")
            destination_connection.execute("ATTACH DATABASE ? AS legacy", (str(source.resolve()),))
            merge_order = (
                "sessions", "events", "outbox_events", "agent_runs", "artifacts",
                "proposals", "proposal_audit", "idempotency_keys", "codex_approvals",
            )
            for table in merge_order:
                source_table = destination_connection.execute(
                    "SELECT 1 FROM legacy.sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                target_table = destination_connection.execute(
                    "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                if source_table is None or target_table is None:
                    continue
                source_columns = {
                    row[1] for row in destination_connection.execute(
                        f"PRAGMA legacy.table_info('{table}')"
                    ).fetchall()
                }
                target_columns = [
                    row[1] for row in destination_connection.execute(
                        f"PRAGMA main.table_info('{table}')"
                    ).fetchall()
                ]
                columns = [
                    column for column in target_columns
                    if column in source_columns and not (table == "events" and column == "sequence")
                ]
                quoted = ", ".join(f'"{column}"' for column in columns)
                destination_connection.execute(
                    f"INSERT OR IGNORE INTO main.\"{table}\" ({quoted}) "
                    f"SELECT {quoted} FROM legacy.\"{table}\""
                )
            destination_connection.commit()
            destination_connection.execute("DETACH DATABASE legacy")
        destination_connection.execute(
            """CREATE TABLE IF NOT EXISTS ai_trader_runtime_migrations (
            migration_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
            migrated_at TEXT NOT NULL, source_retained INTEGER NOT NULL CHECK(source_retained = 1)
            )"""
        )
        destination_connection.execute(
            "INSERT OR REPLACE INTO ai_trader_runtime_migrations VALUES (?, ?, ?, 1)",
            ("phase-1.2-default-database", str(source.resolve()), utc_now()),
        )
        destination_connection.commit()
        destination_connection.execute("PRAGMA foreign_keys = ON")
        destination_check = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if destination_check is None or destination_check[0] != "ok":
            raise RuntimeError("migrated_database_integrity_failed")
        destination_connection.close()
        destination_connection = None
        os.replace(temporary, destination)
        return True
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        if temporary.exists():
            temporary.unlink()

def resolve_settings(
    *, database_path: Path | None, engineering_root: Path | None, runtime_state_root: Path | None,
    data_mode: str | None, data_provider: ReadOnlyDataProvider | None, auth_mode: str | None,
    agent_mode: str | None, agent_provider: str | None, cors_origins: set[str] | None,
    simulation_gateway: SimulationGateway | None,
) -> RuntimeSettings:
    """Every fail-closed check that used to open create_app, in the same order."""
    configured_root = engineering_root or Path(os.getenv("AI_TRADER_ENGINEERING_ROOT", REPO_ROOT))
    if not configured_root.is_absolute():
        configured_root = REPO_ROOT / configured_root
    configured_root = configured_root.resolve()
    resolved_data_mode = data_mode if data_mode else os.getenv("AI_TRADER_DATA_MODE", "fake")
    resolved_auth_mode = auth_mode if auth_mode else os.getenv("AI_TRADER_AUTH_MODE", "local-token")
    resolved_agent_mode = agent_mode if agent_mode else os.getenv("AI_TRADER_AGENT_MODE", "native")
    if resolved_agent_mode == "codex":
        # The Codex App Server adapter was removed; the value stays accepted for
        # one release so an old environment file still starts the native loop.
        logging.getLogger("uvicorn.error.agent").warning(
            "AI_TRADER_AGENT_MODE=codex is no longer supported; using native")
        resolved_agent_mode = "native"
    if resolved_agent_mode not in {"fake", "native"}:
        raise ValueError("invalid_agent_mode")
    # Refused here, in resolve, so a mistyped mode never reaches build_services —
    # which writes to the shared database (reconciliation, recovery) before
    # wiring/auth.py would reject it (#525-B).
    if resolved_auth_mode not in {"cloud-jwt", "owner-password", "local-token"}:
        raise ValueError("invalid_auth_mode")
    if resolved_data_mode not in {"fake", "tqkq-readonly", "tqkq-sidecar"}:
        raise ValueError("invalid_data_mode")
    resolved_agent_provider = agent_provider or os.getenv("AI_TRADER_AGENT_PROVIDER", "deepseek")
    if resolved_agent_provider not in {"deepseek", "moonshot"}:
        raise RuntimeError("agent_provider_invalid")
    if resolved_auth_mode == "cloud-jwt" and (
        (data_provider is not None and not isinstance(data_provider, FakeReadOnlyDataProvider))
        or (data_provider is None and resolved_data_mode != "fake")
    ):
        # Account-backed cloud Providers must eventually be resolved by a
        # UserDataProviderRouter across REST, Agent tools, status and lifecycle.
        # A single injected/global Provider is never safe for multiple subjects.
        raise RuntimeError("cloud_account_provider_router_required")
    if resolved_auth_mode == "cloud-jwt" and simulation_gateway is not None:
        raise RuntimeError("cloud_simulation_execution_forbidden")
    remote_auth = resolved_auth_mode in {"cloud-jwt", "owner-password"}
    resolved_origins = cors_origins or allowed_origins(
        include_local_defaults=not remote_auth
    )
    if remote_auth and not resolved_origins:
        raise RuntimeError("cloud_allowed_origins_required")
    if data_provider is None and resolved_data_mode in {"tqkq-readonly", "tqkq-sidecar"}:
        if resolved_data_mode == "tqkq-readonly" and os.getenv("AI_TRADER_RELOAD_ENABLED") == "1":
            raise RuntimeError("tqkq_readonly_reload_forbidden")
        if os.getenv("AI_TRADER_TQKQ_USERNAME") or os.getenv("AI_TRADER_TQKQ_PASSWORD"):
            raise RuntimeError("tqkq_credentials_require_isolated_sidecar")
    state_root = _state_root(runtime_state_root, database_path)
    resolved_database_path = _database_path(database_path, state_root)
    return RuntimeSettings(
        engineering_root=configured_root, data_mode=resolved_data_mode, auth_mode=resolved_auth_mode,
        agent_mode=resolved_agent_mode, agent_provider=resolved_agent_provider, origins=resolved_origins,
        state_root=state_root, database_path=resolved_database_path,
        migrate_legacy_database=database_path is None and not os.getenv("AI_TRADER_DB_PATH"),
    )
