from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from types import TracebackType
from typing import Any, Literal
from uuid import uuid4

from .state_changes import install as install_state_changes

log = logging.getLogger("uvicorn.error.agent")

# 4.1: every account-scoped row carries this id until real accounts are declared (4.4).
# The Runtime process never reads TqKq credentials, so no account fingerprint exists yet.
DEFAULT_TRADING_ACCOUNT_ID = "tqkq-sim:default"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def utc_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _serialized(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class GuardedCursor(sqlite3.Cursor):
    """No driver call may overlap another call on the store's connection.

    Lock consumption too: SQLite's serialized mode does not protect CPython's
    statement cache, and execute() has only stepped to the first row.
    """

    @property
    def _lock(self) -> Any:
        return self.connection._lock  # type: ignore[attr-defined]

    execute = _serialized(sqlite3.Cursor.execute)
    executemany = _serialized(sqlite3.Cursor.executemany)
    @_serialized
    def executescript(self, *args: Any, **kwargs: Any) -> Any:
        self.connection._check_owned_commit()  # type: ignore[attr-defined]
        return super().executescript(*args, **kwargs)
    fetchone = _serialized(sqlite3.Cursor.fetchone)
    fetchmany = _serialized(sqlite3.Cursor.fetchmany)
    fetchall = _serialized(sqlite3.Cursor.fetchall)
    __next__ = _serialized(sqlite3.Cursor.__next__)
    close = _serialized(sqlite3.Cursor.close)


class GuardedConnection(sqlite3.Connection):
    """Serialize shared driver access and preserve StoreBase transaction ownership.

    A `with connection:` block exits through the C-level commit/rollback, which
    does not call the Python overrides, so the guard lives in `__exit__` too.
    `owner_thread` names the thread whose `transaction()` is open; while it is
    set, only that transaction may commit or roll back.
    """

    owner_thread: int | None = None
    owner_committing: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    def __enter__(self) -> GuardedConnection:
        self._lock.acquire()
        return self

    def _check_owned_commit(self) -> None:
        if self.owner_thread is not None and not self.owner_committing:
            raise RuntimeError("commit_inside_owned_transaction")

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> Literal[False]:
        try:
            if self.owner_thread is not None and not self.owner_committing:
                if exc_type is None:
                    raise RuntimeError("commit_inside_owned_transaction")
                # Let the original exception carry on; the owning transaction rolls back.
                return False
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self._lock.release()

    @_serialized
    def commit(self) -> None:
        self._check_owned_commit()
        super().commit()

    @_serialized
    def rollback(self) -> None:
        self._check_owned_commit()
        super().rollback()

    @_serialized
    def cursor(self, factory: type[sqlite3.Cursor] = GuardedCursor) -> Any:
        return super().cursor(factory)

    @_serialized
    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self.cursor().execute(*args, **kwargs)

    @_serialized
    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        return self.cursor().executemany(*args, **kwargs)

    @_serialized
    def executescript(self, *args: Any, **kwargs: Any) -> Any:
        return self.cursor().executescript(*args, **kwargs)

    close = _serialized(sqlite3.Connection.close)
    backup = _serialized(sqlite3.Connection.backup)


class StoreBase:
    """Connection, lock, transaction boundary and every schema migration; the domain mixins build on it."""

    # Composed into `RuntimeStore` by PlanTierStore; the migration seeds the rows.
    seed_plan_tiers: Callable[[], None]
    migrate_plan_configs_to_skills: Callable[[], int]
    migrate_plan_brief_products: Callable[[], int]
    migrate_legacy_reminders: Callable[[], int]

    def __init__(self, database_path: Path, *, max_events_per_session: int = 500) -> None:
        self.database_path = database_path
        self.max_events_per_session = max_events_per_session
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # CPython #118172: shared cached statements can return another cursor's
        # rows. Cursor destruction also resets statements outside Python method
        # locks, so keep statements cursor-local as well as serializing driver
        # calls. The shared RLock still owns transaction/rollback isolation.
        self.connection = sqlite3.connect(database_path, check_same_thread=False, cached_statements=0,
                                          factory=GuardedConnection)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.lock = self.connection._lock
        self._after_commit: list[Callable[[], None]] | None = None
        # 4.4a: a deployment's default account id. `tqkq-sim:default` unless the
        # accounts file renamed it onto a fingerprint id; written rows and the IdP
        # fallback follow it.
        self.default_trading_account_id = DEFAULT_TRADING_ACCOUNT_ID
        # Deployment fact recorded by create_app from the auth mode: in the
        # single-tenant modes (local token, cloud-jwt) a user with no grant rows
        # runs on the shared default account — the verdict resolve_account and
        # user_may_use_account apply. Strict default so a directly-constructed
        # store behaves like owner-password, where a member without grants owns
        # no account at all (2026-09-23 isolation).
        self.idp_fallback = False
        # 锁屏推送 (2026-10-06): the main runtime's PushHub; the account loops (ledger, strategy monitor,
        # autonomy) reach it through the store they already share. None in a replay child and in tests.
        self.push_hub: Any = None
        # 统一提醒 (#932): the app's delivery entry (reminders.deliver bound to the app that owns this store),
        # for the account loops that only hold the store. None in bare tests: they deliver on the store alone.
        self.reminder_sink: Any = None
        self._migrate()

    @contextmanager
    def transaction(self) -> Iterator[GuardedConnection]:
        callbacks: list[Callable[[], None]] = []
        with self.lock:
            if self.connection.owner_thread is not None:
                raise RuntimeError("nested_transaction")
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.owner_thread = threading.get_ident()
            self._after_commit = callbacks
            try:
                yield self.connection
                self.connection.owner_committing = True
                try:
                    self.connection.commit()
                finally:
                    self.connection.owner_committing = False
            except Exception:
                self.connection.owner_committing = True
                try:
                    self.connection.rollback()
                finally:
                    self.connection.owner_committing = False
                raise
            finally:
                # Clear the ownership before the lock goes: another thread taking
                # it must never see a stale owner.
                self.connection.owner_thread = None
                self._after_commit = None
        # After the commit and outside the lock: a notification failure must not
        # turn a committed business operation into an error.
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.exception("after_commit_callback_failed")

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Run `callback` once the caller's open transaction commits; a rollback
        drops it. Judged by thread identity so a notification from a thread that
        does not own the transaction fires immediately instead of riding — and
        dying — with someone else's transaction."""
        if self._after_commit is not None and self.connection.owner_thread == threading.get_ident():
            self._after_commit.append(callback)
        else:
            callback()

    def revoke_owner_session(self, token_hash: str, expires_at: int, now: int) -> None:
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM owner_session_revocations WHERE expires_at <= ?", (now,))
            self.connection.execute("INSERT OR REPLACE INTO owner_session_revocations VALUES (?, ?)", (token_hash, expires_at))

    def owner_session_revoked(self, token_hash: str) -> bool:
        with self.lock:
            return self.connection.execute("SELECT 1 FROM owner_session_revocations WHERE token_hash = ?", (token_hash,)).fetchone() is not None

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}

    def _add_column(self, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in self._columns(table):
            try:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
            except sqlite3.OperationalError as error:
                # Two processes migrating the same old database can race: the
                # other one added this column between the PRAGMA read above and
                # the ALTER. The duplicate-column outcome is already the goal
                # (#525-C1); anything else is a real failure.
                if "duplicate column" not in str(error).lower():
                    raise

    def _migrate(self) -> None:
        with self.lock, self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner_session_revocations (
                    token_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
                );
                -- 修改密码（2026-10-21）：管理员在设置里改过的登录密码（单行）；有它就不用部署配置里的
                -- AI_TRADER_OWNER_PASSWORD_HASH，换了密码所有旧登录随之失效（会话带密码指纹）。
                CREATE TABLE IF NOT EXISTS owner_password (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    password_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cloud_users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL,
                    invited_by TEXT
                );
                CREATE TABLE IF NOT EXISTS invite_codes (
                    code_hash TEXT PRIMARY KEY,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_by TEXT,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS plan_tiers (
                    name TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    config TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                -- The skill registry's global switches (2026-09-24): absent row = enabled.
                CREATE TABLE IF NOT EXISTS skill_settings (
                    skill_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_model_prefs (
                    user_id TEXT PRIMARY KEY,
                    model_slug TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    preferences_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_token_usage (
                    user_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, day)
                );
                -- One row per AI turn (2026-10-14 token 计量): what the turn's model requests cost,
                -- split into input / output / provider cache hits, so a change to the prompt or
                -- the tool set can be measured per turn. Rows older than 30 days are pruned on insert.
                CREATE TABLE IF NOT EXISTS agent_turn_usage (
                    run_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    route TEXT NOT NULL,
                    origin TEXT,
                    model TEXT,
                    status TEXT NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    first_text_ms INTEGER,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_agent_turn_usage_user_day ON agent_turn_usage(user_id, day);
                CREATE INDEX IF NOT EXISTS idx_agent_turn_usage_day ON agent_turn_usage(day);
                CREATE TABLE IF NOT EXISTS user_voice_usage (
                    user_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    seconds INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, day)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default'
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_events_session_sequence
                    ON events(session_id, sequence);
                CREATE TABLE IF NOT EXISTS user_memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    layer TEXT NOT NULL CHECK(layer IN ('profile','rule','lesson')),
                    text TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('user','stats','ai')),
                    evidence_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','invalid')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    invalid_at TEXT,
                    invalid_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_user_memories_user_status_layer
                    ON user_memories(user_id, status, layer);
                CREATE TABLE IF NOT EXISTS user_memory_changes (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    op TEXT NOT NULL CHECK(op IN ('add','update','invalidate','restore')),
                    before_json TEXT,
                    after_json TEXT,
                    actor TEXT NOT NULL CHECK(actor IN ('user','ai','consolidation')),
                    at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_memory_changes_user_at
                    ON user_memory_changes(user_id, at);
                CREATE INDEX IF NOT EXISTS idx_user_memory_changes_user_memory
                    ON user_memory_changes(user_id, memory_id);
                CREATE TABLE IF NOT EXISTS user_memory_runs (
                    user_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT,
                    at TEXT NOT NULL,
                    PRIMARY KEY (user_id, day)
                );
                CREATE TABLE IF NOT EXISTS outbox_events (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON outbox_events(delivered_at, created_at);
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_runs_session_created
                    ON agent_runs(session_id, created_at);
                CREATE TABLE IF NOT EXISTS account_trades (
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    trade_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL DEFAULT '',
                    exchange_trade_id TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    offset TEXT NOT NULL,
                    price REAL,
                    volume INTEGER NOT NULL,
                    trade_date_time INTEGER NOT NULL,
                    trading_day TEXT,
                    volume_multiple REAL,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'activity',
                    approximate INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (trading_account_id, trade_id)
                );
                CREATE INDEX IF NOT EXISTS idx_account_trades_symbol_time
                    ON account_trades(symbol, trade_date_time);
                CREATE TABLE IF NOT EXISTS trade_notes (
                    trading_account_id TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    via TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    volume_at_write INTEGER,
                    PRIMARY KEY (trading_account_id, round_id)
                );
                CREATE TABLE IF NOT EXISTS account_daily (
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    trading_day TEXT NOT NULL,
                    equity REAL,
                    static_balance REAL,
                    close_profit REAL,
                    position_profit REAL,
                    commission REAL,
                    today_net_pnl REAL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY (trading_account_id, trading_day)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    artifact_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    proposal_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default'
                );
                CREATE TABLE IF NOT EXISTS proposal_audit (
                    id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(proposal_id) REFERENCES proposals(id)
                );
                CREATE TABLE IF NOT EXISTS target_position_plans (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    symbol TEXT NOT NULL,
                    target_net INTEGER NOT NULL,
                    leg_index INTEGER NOT NULL DEFAULT 0,
                    leg_json TEXT NOT NULL,
                    current_proposal_id TEXT,
                    current_client_order_ids TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    user_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, key)
                );
                CREATE TABLE IF NOT EXISTS codex_approvals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    run_id TEXT,
                    request_id_json TEXT NOT NULL,
                    approval_type TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_codex_approvals_session_created
                    ON codex_approvals(session_id, created_at);
                CREATE TABLE IF NOT EXISTS strategy_releases (
                    id TEXT PRIMARY KEY,
                    artifact_digest TEXT NOT NULL UNIQUE,
                    source_ref TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_drafts (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    source_session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version > 0),
                    status TEXT NOT NULL CHECK (
                        status IN ('draft', 'validating', 'ready', 'monitoring', 'paused', 'archived')
                    ),
                    definition_json TEXT NOT NULL,
                    definition_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    UNIQUE(user_id, name, version)
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_drafts_user_updated
                    ON strategy_drafts(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS strategy_validation_reports (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    strategy_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('incomplete', 'passed', 'failed')
                    ),
                    checks_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_validation_strategy_created
                    ON strategy_validation_reports(strategy_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS strategy_intent_snapshots (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    strategy_digest TEXT NOT NULL,
                    signal_bar_time TEXT NOT NULL,
                    context_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, strategy_id, context_digest),
                    FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_intents_strategy_created
                    ON strategy_intent_snapshots(strategy_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS strategy_drawdown_states (
                    strategy_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    status TEXT NOT NULL CHECK (
                        status IN ('running', 'liquidating', 'stopped', 'unavailable')
                    ),
                    limit_percent REAL NOT NULL,
                    initial_equity TEXT,
                    baseline_pnl TEXT,
                    peak_equity TEXT,
                    current_equity TEXT,
                    strategy_digest TEXT,
                    round_started_at TEXT NOT NULL,
                    triggered_at TEXT,
                    reason TEXT,
                    fee_source TEXT,
                    curve_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id)
                );
                CREATE TABLE IF NOT EXISTS strategy_monitor_states (
                    strategy_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    high_watermark_equity TEXT,
                    last_today_pnl TEXT,
                    consecutive_losses INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_losses >= 0),
                    last_trade_id TEXT,
                    last_executed_bar TEXT,
                    pause_reason TEXT,
                    last_error TEXT,
                    last_error_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
                    next_retry_at TEXT,
                    updated_at TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id)
                );
                CREATE TABLE IF NOT EXISTS strategy_execution_attempts (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL,
                    signal_bar_time TEXT NOT NULL,
                    trading_day TEXT NOT NULL,
                    action TEXT NOT NULL,
                    volume INTEGER NOT NULL CHECK (volume >= 0),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'RESERVED', 'SUBMITTED', 'PARTIAL', 'NOT_DISPATCHED',
                            'REJECTED', 'CANCELLED', 'SKIPPED', 'BLOCKED', 'UNKNOWN', 'FAILED'
                        )
                    ),
                    client_order_id TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    UNIQUE(user_id, strategy_id, signal_bar_time),
                    FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id),
                    FOREIGN KEY(intent_id) REFERENCES strategy_intent_snapshots(id)
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_attempts_strategy_created
                    ON strategy_execution_attempts(strategy_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    artifact_digest TEXT NOT NULL,
                    strategy_release_id TEXT NOT NULL,
                    data_digest TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(strategy_release_id) REFERENCES strategy_releases(id)
                );
                CREATE INDEX IF NOT EXISTS idx_backtest_runs_user_created
                    ON backtest_runs(user_id, created_at);
                CREATE TABLE IF NOT EXISTS risk_policy_versions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    rules_json TEXT NOT NULL,
                    rules_digest TEXT NOT NULL,
                    source_proposal_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, version),
                    UNIQUE(source_proposal_id)
                );
                CREATE TABLE IF NOT EXISTS active_risk_policies (
                    user_id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(policy_id) REFERENCES risk_policy_versions(id)
                );
                CREATE TABLE IF NOT EXISTS tqkq_sim_order_links (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL UNIQUE,
                    client_order_id TEXT NOT NULL UNIQUE,
                    request_json TEXT NOT NULL,
                    submit_phase TEXT NOT NULL CHECK (
                        submit_phase IN ('PREPARED', 'CALL_STARTED', 'UNKNOWN', 'CONFIRMED', 'ABANDONED')
                    ),
                    cancel_phase TEXT NOT NULL CHECK (
                        cancel_phase IN ('NONE', 'CALL_STARTED', 'UNKNOWN', 'CONFIRMED')
                    ),
                    last_remote_status TEXT,
                    last_report_json TEXT,
                    last_synced_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    FOREIGN KEY(proposal_id) REFERENCES proposals(id)
                );
                CREATE INDEX IF NOT EXISTS idx_tqkq_sim_order_links_user_updated
                    ON tqkq_sim_order_links(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS ai_execution_controls (
                    user_id TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    stop_status TEXT,
                    generation INTEGER NOT NULL DEFAULT 0,
                    limits_revision INTEGER NOT NULL DEFAULT 0,
                    max_order_volume INTEGER,
                    max_orders INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, trading_account_id)
                );
                CREATE TABLE IF NOT EXISTS ai_execution_authorizations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('ACTIVE', 'STOPPED', 'EXPIRED', 'EMERGENCY_STOP')
                    ),
                    symbols_json TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    max_order_volume INTEGER NOT NULL CHECK (max_order_volume > 0),
                    max_orders INTEGER NOT NULL CHECK (max_orders > 0),
                    used_orders INTEGER NOT NULL DEFAULT 0 CHECK (used_orders >= 0),
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default'
                );
                CREATE INDEX IF NOT EXISTS idx_ai_execution_authorizations_user_updated
                    ON ai_execution_authorizations(user_id, updated_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_execution_authorizations_active
                    ON ai_execution_authorizations(user_id, trading_account_id)
                    WHERE status = 'ACTIVE';
                CREATE TABLE IF NOT EXISTS ai_stop_orders (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    authorization_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    position_direction TEXT NOT NULL CHECK (
                        position_direction IN ('long', 'short')
                    ),
                    position_bucket TEXT NOT NULL CHECK (
                        position_bucket IN ('today', 'historical', 'all')
                    ),
                    volume INTEGER NOT NULL CHECK (volume > 0),
                    trigger_price TEXT NOT NULL,
                    price_intent TEXT NOT NULL CHECK (
                        price_intent IN ('passive', 'aggressive', 'last')
                    ),
                    status TEXT NOT NULL CHECK (
                        status IN ('ARMED', 'TRIGGERING', 'SUBMITTED', 'PARTIAL', 'EXECUTED', 'UNKNOWN', 'CANCELLED', 'FAILED')
                    ),
                    close_proposal_id TEXT,
                    close_client_order_id TEXT,
                    last_error TEXT,
                    triggered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                    FOREIGN KEY(authorization_id) REFERENCES ai_execution_authorizations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_ai_stop_orders_pending
                    ON ai_stop_orders(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_ai_stop_orders_user_updated
                    ON ai_stop_orders(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS runtime_lease (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    instance_id TEXT NOT NULL,
                    host TEXT,
                    pid INTEGER,
                    build_sha TEXT,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trading_accounts (
                    id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL CHECK (environment IN ('simulation', 'live')),
                    provider_kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    sidecar_url TEXT,
                    sidecar_token_file TEXT,
                    execution_owner_user_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_grants (
                    account_id TEXT NOT NULL REFERENCES trading_accounts(id),
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('owner', 'operator', 'viewer')),
                    granted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_account_grants_user
                    ON account_grants(user_id);
                CREATE TABLE IF NOT EXISTS tqkq_credentials (
                    user_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    kq_username TEXT NOT NULL,
                    ciphertext BLOB NOT NULL,
                    nonce BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_default_accounts (
                    user_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                -- 本站模拟账户 (2026-10-03): the one paper account id each user holds,
                -- claimed before the engine is asked to create it (see claim_paper_account).
                CREATE TABLE IF NOT EXISTS paper_account_claims (
                    user_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                -- 策略实验室 (2026-09-30): one comparison of up to 12 variants on one contract.
                -- results/ranking are written as they arrive (run-server progress); source says
                -- whether the browser (Pyodide) or the cloud fallback computed them.
                CREATE TABLE IF NOT EXISTS strategy_labs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ready', 'running', 'done', 'error')),
                    request_json TEXT NOT NULL,
                    variants_json TEXT NOT NULL,
                    results_json TEXT,
                    ranking_json TEXT,
                    chosen_json TEXT,
                    source TEXT CHECK (source IN ('browser', 'server') OR source IS NULL),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_labs_user_created
                    ON strategy_labs(user_id, created_at DESC);
                -- 规则回测结果 (策略页改版第八批, 2026-10-08): the recent results per user and
                -- library strategy (strategy_id NULL = an unsaved plan or a template preview),
                -- kept so a restart loses nothing; store/backtests.py trims them to the newest few.
                CREATE TABLE IF NOT EXISTS strategy_backtest_results (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    strategy_id TEXT,
                    created_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_backtest_results_owner
                    ON strategy_backtest_results(user_id, strategy_id, created_at DESC);
                -- 到价提醒 (2026-10-02): per user (quotes are account-independent), one row per
                -- alert; `price` is Decimal text, `status` active/triggered/cancelled.
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL CHECK (direction IN ('above', 'below')),
                    price TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'triggered', 'cancelled')),
                    created_via TEXT NOT NULL DEFAULT 'ui' CHECK (created_via IN ('ui', 'chat', 'voice')),
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    triggered_at TEXT,
                    trigger_price TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_price_alerts_user_status
                    ON price_alerts(user_id, status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_price_alerts_active
                    ON price_alerts(status, symbol);
                -- 公共行情多账号接力 (2026-10-04): the ordered public market source list the
                -- relay allocates kline channels over; one row per source account, `position`
                -- decides the fill order. The seeded marker keeps the env bootstrap a
                -- once-only event — after it (or an admin PUT) the table is authoritative.
                CREATE TABLE IF NOT EXISTS market_sources (
                    account_id TEXT PRIMARY KEY,
                    position INTEGER NOT NULL,
                    added_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_sources_seeded (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    seeded_at TEXT NOT NULL
                );
                -- 快捷技能默认置顶退场 (2026-10-05): the marker row (id = 1) says the
                -- one-time clear of the old default pins already ran — a user who re-pins
                -- the same two skills is never cleared again.
                CREATE TABLE IF NOT EXISTS quick_skills_default_cleared (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cleared_at TEXT NOT NULL
                );
                -- 用户画线 (2026-10-02): per user × contract (not per account or period), one row
                -- per drawn line; `points` is the JSON coordinates the client wrote, `label` ≤12 chars
                -- (a 文字 drawing's words ≤60). 画线第二版 (2026-10-09): the kind is checked by the
                -- route (twenty kinds), `style` is the JSON style (colour, width, dash, extend, lock…).
                CREATE TABLE IF NOT EXISTS chart_drawings (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    points TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    style TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chart_drawings_user_symbol
                    ON chart_drawings(user_id, symbol, created_at);
                -- 打法本 (2026-10-25): the user's own setups — entry / exit / stop rules in words, optionally
                -- linked to one of their strategies. proposal_playbooks tags the card an order came from, so
                -- the trade review's round trips carry the setup they were opened under.
                CREATE TABLE IF NOT EXISTS playbooks (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entry_rules TEXT NOT NULL DEFAULT '',
                    exit_rules TEXT NOT NULL DEFAULT '',
                    stop_rules TEXT NOT NULL DEFAULT '',
                    strategy_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_playbooks_user ON playbooks(user_id, created_at);
                CREATE TABLE IF NOT EXISTS proposal_playbooks (
                    proposal_id TEXT PRIMARY KEY,
                    playbook_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                -- A round trip tagged afterwards in the review (2026-10-25, 第 2 批后半): it overrides the card's
                -- tag; keyed like trade_notes and moved with them when a backfilled fill is replaced.
                CREATE TABLE IF NOT EXISTS round_playbooks (
                    trading_account_id TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    playbook_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trading_account_id, round_id)
                );
                -- 交割月提醒 (2026-10-02): one row per user × contract × trading day. The
                -- INSERT OR IGNORE rowcount says whether today's reminder still has to go out;
                -- restarts and repeated positions reads never resend it.
                CREATE TABLE IF NOT EXISTS delivery_notices (
                    user_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trade_day TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('delivery_month', 'expiry')),
                    level TEXT NOT NULL CHECK (level IN ('warning', 'danger')),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, symbol, trade_day)
                );
                -- 收盘前与风险度提醒 (2026-10-03): one row per user × account × trading day ×
                -- kind (market_close_day / market_close_night / risk_ratio). Same INSERT OR
                -- IGNORE ledger as delivery notices — one reminder per key, restarts never resend.
                CREATE TABLE IF NOT EXISTS reminder_notices (
                    user_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    trade_day TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('market_close_day', 'market_close_night', 'risk_ratio')),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, account_id, trade_day, kind)
                );
                -- 锁屏推送 (2026-10-06): one Web Push subscription per user × endpoint (a device
                -- re-subscribing replaces its row); 404/410 from the push service deletes it.
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_ok_at TEXT,
                    last_error TEXT,
                    PRIMARY KEY (user_id, endpoint)
                );
                CREATE INDEX IF NOT EXISTS idx_push_subscriptions_device
                    ON push_subscriptions(user_id, device_id);
                -- The "once" ledger of events that have no dedup row of their own (盘中异动
                -- market:{symbol}:{5-minute bucket}:{direction}, 开盘前判断 morning:{research day}).
                CREATE TABLE IF NOT EXISTS push_notices (
                    user_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                );
                -- 今日复盘 (2026-10-06): one review per user × account × trading day, regenerated in
                -- place; `pushed_at` is the once-only mark of the 15:15 delivery (message, card, push).
                CREATE TABLE IF NOT EXISTS daily_reviews (
                    user_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    trading_day TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    pushed_at TEXT,
                    PRIMARY KEY (user_id, account_id, trading_day)
                );
                -- The user's answer to a review's 习惯 observation (记住 / 不对), keyed by the observation's
                -- normalized text: an answered observation is never offered again.
                CREATE TABLE IF NOT EXISTS review_observation_verdicts (
                    user_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK (verdict IN ('remembered', 'dismissed')),
                    text TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    trading_day TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                );
                -- 合并卡第二步 (2026-10-06): a confirmed close card's 剩余仓位改止损, written in the
                -- confirmation's transaction and finished once (store/protection_steps.py).
                CREATE TABLE IF NOT EXISTS card_protection_steps (
                    proposal_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('long', 'short')),
                    plan_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('awaiting_close', 'done', 'failed', 'skipped')),
                    result_json TEXT,
                    ready_since TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_card_protection_steps_status
                    ON card_protection_steps(status);
                -- 保本提醒 (2026-10-06): one question per user × account × trading day × contract ×
                -- direction; the card's 移到成本 / 不用 settle it once.
                CREATE TABLE IF NOT EXISTS breakeven_notices (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL,
                    trade_day TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('long', 'short')),
                    session_id TEXT NOT NULL,
                    card_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'moved', 'declined', 'failed')),
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, trading_account_id, trade_day, symbol, direction)
                );
                -- 统一提醒 (#932, 2026-10-15): one row per reminder that went out — the once-only claim
                -- (user × kind × dedupe_key, committed with the conversation message it announces) and
                -- the 提醒中心 record (store/reminders.py).
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    account_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    trading_day TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    session_id TEXT,
                    message_id TEXT,
                    card_id TEXT,
                    target_json TEXT NOT NULL DEFAULT '{"type":"chat"}',
                    state_json TEXT,
                    actions_json TEXT,
                    read_at TEXT,
                    dedupe_key TEXT NOT NULL,
                    UNIQUE (user_id, kind, dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_user_day
                    ON reminders(user_id, trading_day, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reminders_created ON reminders(created_at);
                -- 成交提醒的稳定身份 (#932 P2, 2026-10-17): each fill told once per user, account and order,
                -- claimed in the reminder's own transaction — a restart or the other slot regrouping the
                -- same fills into another set never tells them again (account_ledger.py).
                CREATE TABLE IF NOT EXISTS reminder_fill_claims (
                    user_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, account_id, order_id, trade_id)
                );
                CREATE INDEX IF NOT EXISTS idx_reminder_fill_claims_created ON reminder_fill_claims(created_at);
                -- 挂单未成交看护 (#932): the orders the watch follows, so a restart or the other
                -- blue-green slot picks them up again (order_watch.py); the reminders' own claims keep
                -- each 15 s / 60 s reminder once.
                CREATE TABLE IF NOT EXISTS unfilled_watch_entries (
                    client_order_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminder_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                -- 我的流程 (2026-10-06): a user's named routines (at most 5, at most 10 steps each);
                -- steps_json is [{kind, text}]. Only the user's own 保存 (a 流程卡 or the settings page)
                -- writes here — the AI only drafts.
                CREATE TABLE IF NOT EXISTS user_routines (
                    user_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    PRIMARY KEY (user_id, id),
                    UNIQUE (user_id, name)
                );
                -- The 流程卡 the AI drafted (new or a change to `routine_id`): what 保存 writes, the card it
                -- republishes, and a refusal reason when the draft may not be saved at all.
                CREATE TABLE IF NOT EXISTS routine_drafts (
                    user_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    routine_id TEXT,
                    name TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    refusal TEXT,
                    created_at TEXT NOT NULL,
                    saved_at TEXT,
                    PRIMARY KEY (user_id, id)
                );
                """
            )
            # The contract multiple each fill was worth: only in the CREATE above, so a database made
            # before it never had the column and every /api/trade-review read raised "no such column:
            # volume_multiple" (2026-09-19, the mac runner's long-lived e2e database).
            # #524: new writes get a fresh token; old facts/audit snapshots remain untouched.
            self._add_column("user_memories", "revision TEXT NOT NULL DEFAULT ''")
            self._add_column("user_memories", "state_revision TEXT NOT NULL DEFAULT ''")
            self._add_column("user_memory_changes", "undo_of TEXT")
            # #524 run state: each claim gets an attempt token (adoption and the final status are
            # conditioned on it), a bounded retry count and the material window it must cover.
            self._add_column("user_memory_runs", "attempt TEXT")
            self._add_column("user_memory_runs", "attempts INTEGER NOT NULL DEFAULT 0")
            self._add_column("user_memory_runs", "window_start TEXT")
            self._add_column("account_trades", "volume_multiple REAL")
            # Fills synthesized from finished order links (no fill report from the sidecar) are marked so
            # the review can say the price is the limit price, not a confirmed fill.
            self._add_column("account_trades", "source TEXT NOT NULL DEFAULT 'activity'")
            self._add_column("account_trades", "approximate INTEGER NOT NULL DEFAULT 0")
            # 补充写入时那个回合的手数（#514-2）：迟到的更早开仓会让同一个回合 ID 的
            # 手数变掉，读回时比对得上才知道补充说的还是原来那几手。
            self._add_column("trade_notes", "volume_at_write INTEGER")
            # 今日复盘 (2026-10-06) tags a fill 提醒后 by product: a 收盘前 reminder keeps the contracts it
            # named (comma-separated; empty = the whole account, as 风险度 is), a 盘中异动 claim its text.
            self._add_column("reminder_notices", "symbols TEXT NOT NULL DEFAULT ''")
            self._add_column("push_notices", "text TEXT")
            # 碰到这条线就提醒 (改版 2026-10-31): the user's drawing an alert watches; deleting the drawing cancels it.
            self._add_column("price_alerts", "drawing_id TEXT")
            self._add_column("sessions", "codex_thread_id TEXT")
            self._add_column("sessions", "codex_tool_protocol_version INTEGER NOT NULL DEFAULT 0")
            # #932 follow-up: how many times the session's account was switched — the shared, cross-slot token a switch
            # names (expectedGeneration). DEFAULT 0: an older build ignores it and still reads and writes the row.
            self._add_column("sessions", "account_revision INTEGER NOT NULL DEFAULT 0")
            self._add_column("agent_runs", "model_slug TEXT")
            # M2 tiers: every pre-existing cloud user stays on `basic` (the tier a
            # TqKq direct login starts on); `disabled` is the owner's kill switch.
            self._add_column("cloud_users", "plan TEXT NOT NULL DEFAULT 'basic'")
            self._add_column("cloud_users", "disabled INTEGER NOT NULL DEFAULT 0")
            # A tier-carrying invite (2026-09-23): NULL = the code predates tiers,
            # the first login then lands on the default tier.
            self._add_column("invite_codes", "plan TEXT")
            # 邀请码四能力 (2026-10-01): the code text itself (rows made before this
            # keep only the hash, so their code reads back null), the unlimited-use
            # and never-expiring flags, the use counter, the disable stamp, and a
            # per-use ledger. `expires_at` stays NOT NULL — a never-expiring code
            # stores its creation time there and reads judge by `never_expires`.
            self._add_column("invite_codes", "code TEXT")
            self._add_column("invite_codes", "unlimited INTEGER NOT NULL DEFAULT 0")
            self._add_column("invite_codes", "use_count INTEGER NOT NULL DEFAULT 0")
            self._add_column("invite_codes", "never_expires INTEGER NOT NULL DEFAULT 0")
            self._add_column("invite_codes", "disabled_at TEXT")
            invite_uses_existed = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'invite_code_uses'"
            ).fetchone() is not None
            self.connection.execute("""CREATE TABLE IF NOT EXISTS invite_code_uses (
                code_hash TEXT NOT NULL, username TEXT NOT NULL, used_at TEXT NOT NULL
            )""")
            if not invite_uses_existed:
                # A legacy one-shot code that was consumed counts as exactly one use
                # by its recorded user; runs after the first are no-ops.
                self.connection.execute(
                    "UPDATE invite_codes SET use_count = 1 WHERE used_by IS NOT NULL"
                )
                self.connection.execute(
                    "INSERT INTO invite_code_uses (code_hash, username, used_at)"
                    " SELECT code_hash, used_by, COALESCE(used_at, created_at)"
                    " FROM invite_codes WHERE used_by IS NOT NULL"
                )
            self._add_column("proposals", "user_id TEXT NOT NULL DEFAULT 'local-user'")
            self._add_column("proposals", "version INTEGER NOT NULL DEFAULT 1")
            self._add_column("proposals", "approval_id TEXT")
            self._add_column("proposals", "approval_expires_at TEXT")
            self._add_column("proposals", "approval_used_at TEXT")
            self._add_column("tqkq_sim_order_links", "protection_json TEXT")
            # 策略实验室: the groups the parameter grid produced that could not be built (skipInvalid),
            # kept with the lab so 界面 can say how many were skipped. Only in the CREATE above for
            # new databases; older ones gain it here.
            self._add_column("strategy_labs", "skipped_json TEXT")
            self._add_column("tqkq_sim_order_links", "cancel_requested_at TEXT")
            self._add_column("tqkq_sim_order_links", "last_read_error TEXT")
            self._add_column("tqkq_sim_order_links", "last_error_report_json TEXT")
            self._add_column("tqkq_sim_order_links", "fact_revision INTEGER NOT NULL DEFAULT 0")
            self.connection.execute("""CREATE TRIGGER IF NOT EXISTS tqkq_order_fact_revision
                AFTER UPDATE OF submit_phase, cancel_phase, last_remote_status, last_report_json, cancel_requested_at
                ON tqkq_sim_order_links BEGIN
                UPDATE tqkq_sim_order_links SET fact_revision = OLD.fact_revision + 1 WHERE id = NEW.id;
                END""")
            self._add_column("ai_stop_orders", "take_profit_price TEXT")
            self._add_column("ai_stop_orders", "strategy_metadata_json TEXT")
            self._add_column("ai_stop_orders", "remaining_volume INTEGER")
            # 直接保护的持仓锚点 (#528): 挂单时该桶持有手数、此后已平手数、成交水位
            # ({"day", "ids"})。旧行为空，第一次收敛时以当时持仓与成交为起点。
            self._add_column("ai_stop_orders", "anchor_volume INTEGER")
            self._add_column("ai_stop_orders", "closed_since INTEGER")
            self._add_column("ai_stop_orders", "fill_watermark_json TEXT")
            # 直接保护挂上时的账户交易日 (#790 review)：隔夜滚桶按账户换日判断。旧行为空，按创建时间的日历日。
            self._add_column("ai_stop_orders", "account_trading_day TEXT")
            # 手动持仓的追踪止损 (2026-10-25)：直接保护的追踪方式与它的状态（entryPrice、priceTick、trailExtreme…），
            # 和策略行的 strategy_metadata_json 同形；单列一列，直接保护仍靠 strategy_metadata_json 为空来走原仓核对。
            self._add_column("ai_stop_orders", "trailing_json TEXT")
            # 改动留痕 (2026-10-30): who moved a level last (user / strategy / trailing), read by its trigger.
            self._add_column("ai_stop_orders", "changed_by TEXT")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS strategy_fill_events (
                user_id TEXT NOT NULL, event_id TEXT NOT NULL, trade_time INTEGER NOT NULL,
                trading_day TEXT NOT NULL, client_order_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0, settled_day TEXT,
                trading_account_id TEXT NOT NULL DEFAULT 'tqkq-sim:default',
                PRIMARY KEY(user_id, trading_account_id, event_id)
            )""")
            strategy_table = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'strategy_drafts'"
            ).fetchone()
            if strategy_table is not None and "backtest_required" in str(strategy_table["sql"]):
                self.connection.commit()
                self._backup_legacy_strategy_database()
                self._migrate_legacy_strategy_schema()
            self._migrate_strategy_attempt_schema()
            self._migrate_voice_prefs()
            self._migrate_quick_skills_default()
            self._migrate_chart_drawings_schema()
            # 4.1 multi-account data layer (after the legacy strategy rebuilds, which recreate
            # strategy tables from their own DDL and would drop the column): every row gets its trading account. The
            # nine tables whose primary key already identifies one row only gain the
            # column (the DEFAULT backfills old rows); the three keyed by a single
            # business column are rebuilt below with a composite key.
            trading_account_migration_ran = False
            for table in ("tqkq_sim_order_links", "ai_execution_authorizations", "ai_stop_orders",
                          "strategy_drafts", "strategy_monitor_states", "strategy_execution_attempts",
                          "strategy_fill_events", "sessions", "proposals"):
                if "trading_account_id" not in self._columns(table):
                    self._add_column(
                        table, f"trading_account_id TEXT NOT NULL DEFAULT '{DEFAULT_TRADING_ACCOUNT_ID}'"
                    )
                    trading_account_migration_ran = True
            trading_account_migration_ran = self._migrate_trading_account_keys() or trading_account_migration_ran
            active_index = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_ai_execution_authorizations_active'"
            ).fetchone()
            if active_index is not None and "trading_account_id" not in str(active_index["sql"]):
                self.connection.execute("DROP INDEX idx_ai_execution_authorizations_active")
                self.connection.execute(
                    """CREATE UNIQUE INDEX idx_ai_execution_authorizations_active
                    ON ai_execution_authorizations(user_id, trading_account_id)
                    WHERE status = 'ACTIVE'"""
                )
                trading_account_migration_ran = True
            if trading_account_migration_ran:
                violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    self.connection.rollback()
                    raise RuntimeError("trading_account_migration_foreign_key_failed")
            self._migrate_strategy_fill_events_key()
            self._add_column("strategy_monitor_states", "last_error TEXT")
            self._add_column("strategy_monitor_states", "last_error_at TEXT")
            self._add_column(
                "strategy_monitor_states",
                "consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0)",
            )
            self._add_column("strategy_monitor_states", "next_retry_at TEXT")
            # 侧栏重设计第三期 (2026-09-25): how a monitored instance handles its signal —
            # alert 只提醒 / confirm 出卡确认 / auto 自动下单 — and the conversation that hears it.
            # Existing rows keep today's behaviour (auto); adding the columns again is a no-op.
            self._add_column(
                "strategy_drafts",
                "signal_action TEXT NOT NULL DEFAULT 'auto' CHECK (signal_action IN ('alert', 'confirm', 'auto'))",
            )
            self._add_column("strategy_drafts", "signal_session_id TEXT")
            # A confirm-mode attempt's cards (comma-separated proposal ids): the attempt stays
            # SKIPPED until a card is confirmed, then carries that order like an auto attempt.
            self._add_column("strategy_execution_attempts", "card_proposal_ids TEXT")
            # Which 执行方式 produced an attempt (alert / confirm; NULL = auto and every older row),
            # and when the instance's signal last moved away from what it offered: together the
            # durable "this signal was already reminded / carded" record that survives a restart.
            self._add_column("strategy_execution_attempts", "signal_action TEXT")
            # 价差第二期: whether the spread executor's background follow is still running
            # ('following') or reached its outcome ('done'). SUBMITTED is written both when the legs
            # dispatch and when they finish filling, so this — not the status — is what the restart
            # sweep reads to tell an in-flight attempt from a completed spread trade.
            self._add_column("strategy_execution_attempts", "follow_state TEXT")
            # The attempt's structured conclusion (filled / filled_chased / rolled_back / leftover /
            # unknown) written when the follow settles: the card's 执行结论 line reads this, on the
            # first push and on every idempotent replay alike.
            self._add_column("strategy_execution_attempts", "outcome TEXT")
            self._add_column("strategy_monitor_states", "signal_reset_at TEXT")
            # 主动提醒去重 (2026-10-12): last judged / last announced [action, bar] as JSON, so a
            # restart or a blue-green switch never announces the same bar's signal twice.
            self._add_column("strategy_monitor_states", "signal_seen TEXT")
            self._add_column("strategy_monitor_states", "signal_alerted TEXT")
            # Why a lease stopped: 'ai_autonomy_off' when 设置 → 交易 → AI 自主执行 was switched off —
            # not a stop the user gave, so it never fences the user's own confirmed actions.
            self._add_column("ai_execution_authorizations", "stop_reason TEXT")
            # 回答卡片化 (2026-10-14): how many cards the turn showed and whether its answer text stated
            # figures — numeric_text = 1 with card_count = 0 is a turn that still answered data in prose.
            self._add_column("agent_turn_usage", "card_count INTEGER")
            self._add_column("agent_turn_usage", "numeric_text INTEGER")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_execution_legs (
                    id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    leg_index INTEGER NOT NULL CHECK (leg_index >= 0),
                    position_bucket TEXT,
                    volume INTEGER NOT NULL CHECK (volume > 0),
                    client_order_id TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'RESERVED', 'SUBMITTED', 'PARTIAL', 'NOT_DISPATCHED',
                            'REJECTED', 'CANCELLED', 'UNKNOWN', 'FAILED'
                        )
                    ),
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(attempt_id, leg_index),
                    FOREIGN KEY(attempt_id) REFERENCES strategy_execution_attempts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_execution_legs_attempt
                    ON strategy_execution_legs(attempt_id, leg_index);
                """
            )
            self.connection.executescript(
                """
                DROP TRIGGER IF EXISTS proposals_terminal_immutable;
                CREATE TRIGGER proposals_terminal_immutable
                BEFORE UPDATE ON proposals
                WHEN OLD.status IN ('candidate_recorded', 'risk_rules_applied', 'rejected', 'cancelled')
                BEGIN
                    SELECT RAISE(ABORT, 'terminal_proposal_immutable');
                END;
                """
            )
            deployments_kept = self._drop_retired_deployment_table()
            # Early Phase 1.3 development builds used a shortened content ID. Upgrade
            # those local rows in place so persisted backtest snapshots keep working.
            rows = self.connection.execute(
                "SELECT * FROM strategy_releases"
            ).fetchall()
            if any(row["id"] != f"sha256-{row['artifact_digest']}" for row in rows):
                self.connection.execute("PRAGMA defer_foreign_keys = ON")
            for row in rows:
                canonical_id = f"sha256-{row['artifact_digest']}"
                if row["id"] == canonical_id:
                    continue
                manifest = json.loads(row["manifest_json"])
                manifest["releaseId"] = canonical_id
                self.connection.execute(
                    "UPDATE strategy_releases SET id = ?, manifest_json = ? WHERE id = ?",
                    (canonical_id, json.dumps(manifest, ensure_ascii=False), row["id"]),
                )
                if deployments_kept:
                    self.connection.execute(
                        "UPDATE strategy_deployments SET release_id = ? WHERE release_id = ?",
                        (canonical_id, row["id"]),
                    )
                # Backtest rows point at the release too, and the foreign key
                # settles at this transaction's commit — without the rewrite the
                # whole upgrade fails on the old short ids (#525-C2).
                self.connection.execute(
                    "UPDATE backtest_runs SET strategy_release_id = ? WHERE strategy_release_id = ?",
                    (canonical_id, row["id"]),
                )
            # Paper accounts provisioned before claims existed: each user's claim is
            # the earliest active paper account they own — the one the lookup used
            # to return. A duplicate a race left behind keeps its row and history
            # and is just never handed out again. Existing claims are left alone.
            self.connection.execute(
                """INSERT OR IGNORE INTO paper_account_claims (user_id, account_id, created_at)
                SELECT execution_owner_user_id, id, created_at FROM trading_accounts
                WHERE provider_kind = 'paper' AND status = 'active'
                ORDER BY created_at, id"""
            )
            # M2 tiers in the database (2026-09-23): the three built-in tiers
            # exist only to populate an empty table; after that the rows are the
            # owner's data and outlive any restart unchanged.
            self.seed_plan_tiers()
            # The skill registry (2026-09-24): rows still carrying the legacy
            # `features` list become `skills` lists, once (the rewrite is a
            # no-op for already-migrated rows, so every startup runs it safely).
            self.migrate_plan_configs_to_skills()
            # AI 简报订阅上限 (2026-10-05): basic/pro rows predating `briefProducts`
            # gain the built-in default; the owner's own value is never touched.
            self.migrate_plan_brief_products()
            # 统一提醒 (#932): the reminders already sent through the older ledgers become 提醒中心 rows
            # (read, so no badge lights up for them) — once per database.
            self.migrate_legacy_reminders()
            # 改动留痕 (2026-10-30): last, so a table a migration above rebuilt gets its triggers back.
            install_state_changes(self.connection)

    def _backup_legacy_strategy_database(self) -> Path:
        backup_path = self.database_path.with_name(
            f"{self.database_path.stem}.pre-strategy-migration-"
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}.sqlite3"
        )
        backup = sqlite3.connect(backup_path)
        try:
            self.connection.backup(backup)
        finally:
            backup.close()
        return backup_path

    def _drop_retired_deployment_table(self) -> bool:
        """The strategy deployment-candidate chain was removed (2026-09-25).

        Its table is dropped only when empty; a table that still holds rows is kept
        untouched for inspection. Returns whether the table still exists."""
        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'strategy_deployments'"
        ).fetchone()
        if exists is None:
            return False
        count = self.connection.execute("SELECT COUNT(*) FROM strategy_deployments").fetchone()[0]
        if count:
            log.warning("strategy_deployments kept: %s rows from the retired deployment chain", count)
            return True
        self.connection.execute("DROP TABLE strategy_deployments")
        return False

    def _migrate_voice_prefs(self) -> None:
        """1.6.6: the chosen voice (`user_voice_prefs`) became `user_preferences.voice.voice`."""
        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_voice_prefs'").fetchone()
        if exists is None:
            return
        for row in self.connection.execute("SELECT user_id, voice FROM user_voice_prefs").fetchall():
            self.connection.execute(
                "INSERT OR IGNORE INTO user_preferences (user_id, version, preferences_json, updated_at)"
                " VALUES (?, 1, ?, ?)",
                (row["user_id"], json.dumps({"voice": {"voice": row["voice"]}}), utc_now()))
        self.connection.execute("DROP TABLE user_voice_prefs")

    def _migrate_quick_skills_default(self) -> None:
        """快捷技能默认不置顶（owner 2026-10-05）：一次性把 ui.quickSkills 恰好等于
        旧默认 ["strategy-signals", "strategy-guided-design"]（顺序也相同）的行改成
        []; 用户改过的（顺序不同、多一个少一个）不动。quick_skills_default_cleared
        的标记行保证只跑一次——迁移后用户自己重新钉上旧默认，重启也不会再被清掉。"""
        done = self.connection.execute(
            "SELECT 1 FROM quick_skills_default_cleared WHERE id = 1").fetchone()
        if done is not None:
            return
        old_default = ["strategy-signals", "strategy-guided-design"]
        for row in self.connection.execute(
                "SELECT user_id, preferences_json FROM user_preferences").fetchall():
            try:
                document = json.loads(row["preferences_json"])
            except (TypeError, ValueError):
                continue
            ui = document.get("ui") if isinstance(document, dict) else None
            if not isinstance(ui, dict) or ui.get("quickSkills") != old_default:
                continue
            ui["quickSkills"] = []
            self.connection.execute(
                "UPDATE user_preferences SET version = version + 1, preferences_json = ?,"
                " updated_at = ? WHERE user_id = ?",
                (json.dumps(document, ensure_ascii=False), utc_now(), row["user_id"]))
        self.connection.execute(
            "INSERT INTO quick_skills_default_cleared (id, cleared_at) VALUES (1, ?)",
            (utc_now(),))

    def _migrate_legacy_strategy_schema(self) -> None:
        """Replace the retired draft status without discarding strategy evidence."""
        self.connection.executescript(
            """
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS strategy_validation_reports_migration_backup;
            DROP TABLE IF EXISTS strategy_intent_snapshots_migration_backup;
            DROP TABLE IF EXISTS strategy_monitor_states_migration_backup;
            DROP TABLE IF EXISTS strategy_execution_attempts_migration_backup;
            CREATE TABLE strategy_validation_reports_migration_backup AS
                SELECT * FROM strategy_validation_reports;
            CREATE TABLE strategy_intent_snapshots_migration_backup AS
                SELECT * FROM strategy_intent_snapshots;
            CREATE TABLE strategy_monitor_states_migration_backup AS
                SELECT * FROM strategy_monitor_states;
            CREATE TABLE strategy_execution_attempts_migration_backup AS
                SELECT * FROM strategy_execution_attempts;
            DROP TABLE strategy_execution_attempts;
            DROP TABLE strategy_monitor_states;
            DROP TABLE strategy_validation_reports;
            DROP TABLE strategy_intent_snapshots;
            ALTER TABLE strategy_drafts RENAME TO strategy_drafts_legacy;
            CREATE TABLE strategy_drafts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                source_session_id TEXT NOT NULL,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version > 0),
                status TEXT NOT NULL CHECK (
                    status IN ('draft', 'validating', 'ready', 'monitoring', 'paused', 'archived')
                ),
                definition_json TEXT NOT NULL,
                definition_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, name, version)
            );
            INSERT INTO strategy_drafts
            SELECT id, user_id, source_session_id, name, symbol, version,
                   CASE WHEN status = 'backtest_required' THEN 'draft' ELSE status END,
                   definition_json, definition_digest, created_at, updated_at
            FROM strategy_drafts_legacy;
            DROP TABLE strategy_drafts_legacy;
            CREATE INDEX idx_strategy_drafts_user_updated
                ON strategy_drafts(user_id, updated_at DESC);
            CREATE TABLE strategy_validation_reports (
                id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                strategy_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('incomplete', 'passed', 'failed')),
                checks_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id)
            );
            INSERT INTO strategy_validation_reports SELECT *
                FROM strategy_validation_reports_migration_backup;
            CREATE INDEX idx_strategy_validation_strategy_created
                ON strategy_validation_reports(strategy_id, created_at DESC);
            CREATE TABLE strategy_intent_snapshots (
                id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                strategy_digest TEXT NOT NULL,
                signal_bar_time TEXT NOT NULL,
                context_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, strategy_id, context_digest),
                FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id)
            );
            INSERT INTO strategy_intent_snapshots SELECT *
                FROM strategy_intent_snapshots_migration_backup;
            CREATE INDEX idx_strategy_intents_strategy_created
                ON strategy_intent_snapshots(strategy_id, created_at DESC);
            CREATE TABLE strategy_monitor_states (
                strategy_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                high_watermark_equity TEXT,
                last_today_pnl TEXT,
                consecutive_losses INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_losses >= 0),
                last_trade_id TEXT,
                last_executed_bar TEXT,
                pause_reason TEXT,
                last_error TEXT,
                last_error_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
                next_retry_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id)
            );
            INSERT INTO strategy_monitor_states (
                strategy_id, user_id, trading_date, high_watermark_equity,
                last_today_pnl, consecutive_losses, last_trade_id,
                last_executed_bar, pause_reason, updated_at
            ) SELECT strategy_id, user_id, trading_date, high_watermark_equity,
                     last_today_pnl, consecutive_losses, last_trade_id,
                     last_executed_bar, pause_reason, updated_at
              FROM strategy_monitor_states_migration_backup;
            CREATE TABLE strategy_execution_attempts (
                id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                signal_bar_time TEXT NOT NULL,
                trading_day TEXT NOT NULL,
                action TEXT NOT NULL,
                volume INTEGER NOT NULL CHECK (volume >= 0),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'RESERVED', 'SUBMITTED', 'PARTIAL', 'NOT_DISPATCHED',
                        'REJECTED', 'CANCELLED', 'SKIPPED', 'BLOCKED', 'UNKNOWN', 'FAILED'
                    )
                ),
                client_order_id TEXT,
                detail TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, strategy_id, signal_bar_time),
                FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id),
                FOREIGN KEY(intent_id) REFERENCES strategy_intent_snapshots(id)
            );
            INSERT INTO strategy_execution_attempts (
                id, strategy_id, user_id, intent_id, signal_bar_time, trading_day,
                action, volume, status, client_order_id, detail, created_at, updated_at
            ) SELECT id, strategy_id, user_id, intent_id, signal_bar_time,
                     substr(created_at, 1, 10), action, volume, status,
                     client_order_id, detail, created_at, updated_at
              FROM strategy_execution_attempts_migration_backup;
            CREATE INDEX idx_strategy_attempts_strategy_created
                ON strategy_execution_attempts(strategy_id, created_at DESC);
            DROP TABLE strategy_validation_reports_migration_backup;
            DROP TABLE strategy_intent_snapshots_migration_backup;
            DROP TABLE strategy_monitor_states_migration_backup;
            DROP TABLE strategy_execution_attempts_migration_backup;
            """
        )
        violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            self.connection.rollback()
            raise RuntimeError("legacy_strategy_migration_foreign_key_failed")
        self.connection.commit()

    def _migrate_chart_drawings_schema(self) -> None:
        """画线第二版 (2026-10-09): the first version's table limited `kind` to three values with a
        CHECK and had no `style`. Rebuild it once — every row kept as it was (its points, label and
        times), `style` '{}' meaning the kind's original look — so old lines read and draw the same."""
        table = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chart_drawings'").fetchone()
        if table is None or "CHECK" not in str(table["sql"] or ""):
            return
        self.connection.executescript(
            """
            BEGIN IMMEDIATE;
            ALTER TABLE chart_drawings RENAME TO chart_drawings_legacy;
            DROP INDEX IF EXISTS idx_chart_drawings_user_symbol;
            CREATE TABLE chart_drawings (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                kind TEXT NOT NULL,
                points TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                style TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO chart_drawings (id, user_id, symbol, kind, points, label, style, created_at, updated_at)
            SELECT id, user_id, symbol, kind, points, label, '{}', created_at, updated_at FROM chart_drawings_legacy;
            DROP TABLE chart_drawings_legacy;
            CREATE INDEX IF NOT EXISTS idx_chart_drawings_user_symbol ON chart_drawings(user_id, symbol, created_at);
            COMMIT;
            """
        )

    def _migrate_strategy_fill_events_key(self) -> None:
        """#453: a fill's uniqueness is (user, trading account, event) — the same
        user's two accounts can hold gateway-identical fills (clientOrderId +
        tradeId), so a key without the account made the second account's sync
        die on IntegrityError with its close consumption rolled back. Rebuild
        the pre-fix table (account column present, two-column key), keeping
        every history row and its settlement state; the dedup SELECT already
        filtered by account, so no row can collide in the rebuild."""
        rows = sorted(self.connection.execute("PRAGMA table_info(strategy_fill_events)").fetchall(),
                      key=lambda row: int(row["pk"]))
        if "trading_account_id" in {str(row["name"]) for row in rows if int(row["pk"]) > 0}:
            return
        self.connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            ALTER TABLE strategy_fill_events RENAME TO strategy_fill_events_legacy;
            CREATE TABLE strategy_fill_events (
                user_id TEXT NOT NULL, event_id TEXT NOT NULL, trade_time INTEGER NOT NULL,
                trading_day TEXT NOT NULL, client_order_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0, settled_day TEXT,
                trading_account_id TEXT NOT NULL DEFAULT '{DEFAULT_TRADING_ACCOUNT_ID}',
                PRIMARY KEY(user_id, trading_account_id, event_id)
            );
            INSERT INTO strategy_fill_events (
                user_id, event_id, trade_time, trading_day, client_order_id,
                payload_json, settled, settled_day, trading_account_id)
            SELECT user_id, event_id, trade_time, trading_day, client_order_id,
                payload_json, settled, settled_day, trading_account_id
            FROM strategy_fill_events_legacy;
            DROP TABLE strategy_fill_events_legacy;
            COMMIT;
            """
        )

    def _migrate_trading_account_keys(self) -> bool:
        """4.1: rebuild the tables whose key was one business column so the account
        becomes part of the primary key, backfilling old rows to the default account."""
        migrated = False
        if "trading_account_id" not in self._columns("account_trades"):
            migrated = True
            self.connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                ALTER TABLE account_trades RENAME TO account_trades_legacy;
                CREATE TABLE account_trades (
                    trading_account_id TEXT NOT NULL DEFAULT '{DEFAULT_TRADING_ACCOUNT_ID}',
                    trade_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL DEFAULT '',
                    exchange_trade_id TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    offset TEXT NOT NULL,
                    price REAL,
                    volume INTEGER NOT NULL,
                    trade_date_time INTEGER NOT NULL,
                    trading_day TEXT,
                    volume_multiple REAL,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'activity',
                    approximate INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (trading_account_id, trade_id)
                );
                INSERT INTO account_trades (
                    trading_account_id, trade_id, client_order_id, exchange_trade_id, symbol,
                    direction, offset, price, volume, trade_date_time, trading_day,
                    volume_multiple, captured_at, source, approximate
                ) SELECT '{DEFAULT_TRADING_ACCOUNT_ID}', trade_id, client_order_id, exchange_trade_id,
                         symbol, direction, offset, price, volume, trade_date_time, trading_day,
                         volume_multiple, captured_at, source, approximate
                  FROM account_trades_legacy;
                DROP TABLE account_trades_legacy;
                CREATE INDEX idx_account_trades_symbol_time
                    ON account_trades(symbol, trade_date_time);
                COMMIT;
                """
            )
        if "trading_account_id" not in self._columns("account_daily"):
            migrated = True
            self.connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                ALTER TABLE account_daily RENAME TO account_daily_legacy;
                CREATE TABLE account_daily (
                    trading_account_id TEXT NOT NULL DEFAULT '{DEFAULT_TRADING_ACCOUNT_ID}',
                    trading_day TEXT NOT NULL,
                    equity REAL,
                    static_balance REAL,
                    close_profit REAL,
                    position_profit REAL,
                    commission REAL,
                    today_net_pnl REAL,
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY (trading_account_id, trading_day)
                );
                INSERT INTO account_daily (
                    trading_account_id, trading_day, equity, static_balance, close_profit,
                    position_profit, commission, today_net_pnl, captured_at
                ) SELECT '{DEFAULT_TRADING_ACCOUNT_ID}', trading_day, equity, static_balance,
                         close_profit, position_profit, commission, today_net_pnl, captured_at
                  FROM account_daily_legacy;
                DROP TABLE account_daily_legacy;
                COMMIT;
                """
            )
        if "trading_account_id" not in self._columns("ai_execution_controls"):
            migrated = True
            self.connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                ALTER TABLE ai_execution_controls RENAME TO ai_execution_controls_legacy;
                CREATE TABLE ai_execution_controls (
                    user_id TEXT NOT NULL,
                    trading_account_id TEXT NOT NULL DEFAULT '{DEFAULT_TRADING_ACCOUNT_ID}',
                    stop_status TEXT,
                    generation INTEGER NOT NULL DEFAULT 0,
                    limits_revision INTEGER NOT NULL DEFAULT 0,
                    max_order_volume INTEGER,
                    max_orders INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, trading_account_id)
                );
                INSERT INTO ai_execution_controls (
                    user_id, trading_account_id, stop_status, generation, limits_revision,
                    max_order_volume, max_orders, updated_at
                ) SELECT user_id, '{DEFAULT_TRADING_ACCOUNT_ID}', stop_status, generation,
                         limits_revision, max_order_volume, max_orders, updated_at
                  FROM ai_execution_controls_legacy;
                DROP TABLE ai_execution_controls_legacy;
                COMMIT;
                """
            )
        return migrated

    def _migrate_strategy_attempt_schema(self) -> None:
        table = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'strategy_execution_attempts'"
        ).fetchone()
        if table is None:
            return
        sql = str(table["sql"] or "")
        columns = self._columns("strategy_execution_attempts")
        if "NOT_DISPATCHED" in sql and "trading_day" in columns:
            return
        trading_day = "trading_day" if "trading_day" in columns else "substr(created_at, 1, 10)"
        self.connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            ALTER TABLE strategy_execution_attempts RENAME TO strategy_execution_attempts_legacy;
            CREATE TABLE strategy_execution_attempts (
                id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                intent_id TEXT NOT NULL,
                signal_bar_time TEXT NOT NULL,
                trading_day TEXT NOT NULL,
                action TEXT NOT NULL,
                volume INTEGER NOT NULL CHECK (volume >= 0),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'RESERVED', 'SUBMITTED', 'PARTIAL', 'NOT_DISPATCHED',
                        'REJECTED', 'CANCELLED', 'SKIPPED', 'BLOCKED', 'UNKNOWN', 'FAILED'
                    )
                ),
                client_order_id TEXT,
                detail TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, strategy_id, signal_bar_time),
                FOREIGN KEY(strategy_id) REFERENCES strategy_drafts(id),
                FOREIGN KEY(intent_id) REFERENCES strategy_intent_snapshots(id)
            );
            INSERT INTO strategy_execution_attempts (
                id, strategy_id, user_id, intent_id, signal_bar_time, trading_day,
                action, volume, status, client_order_id, detail, created_at, updated_at
            ) SELECT id, strategy_id, user_id, intent_id, signal_bar_time, {trading_day},
                     action, volume, status, client_order_id, detail, created_at, updated_at
              FROM strategy_execution_attempts_legacy;
            DROP TABLE strategy_execution_attempts_legacy;
            CREATE INDEX idx_strategy_attempts_strategy_created
                ON strategy_execution_attempts(strategy_id, created_at DESC);
            COMMIT;
            """
        )

    def close(self) -> None:
        self.connection.close()
