"""改动留痕 (owner 2026-10-30, #932): a trading object the user can change keeps every change — what changed, the
value before and after, when, and who (where the writer says) — in one shared append-only table, so “what was it
at that moment” can always be answered (交易验尸 reads a stop's level at the close from here).

The database writes the rows itself (triggers), so none of the many places that rewrite these tables can skip
it. They are rebuilt on every startup: a migration that rebuilds a table drops its triggers, and a changed spec
replaces the old one. A new table whose rows are changed in place gets a spec here (AGENTS.md)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    user_id TEXT,
    trading_account_id TEXT,
    change TEXT NOT NULL CHECK (change IN ('created', 'updated', 'deleted')),
    before_json TEXT,
    after_json TEXT,
    actor TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_changes_object ON state_changes(object_type, object_id, seq);
CREATE INDEX IF NOT EXISTS idx_state_changes_user ON state_changes(user_id, at);
"""
# The same shape utc_now() writes (ISO 8601, UTC offset), so times compare with the tables' own.
NOW = "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"


@dataclass(frozen=True)
class Spec:
    table: str
    object_type: str
    object_id: str            # SQL over the row (as {r}.column)
    user_id: str
    account_id: str
    tracked: dict[str, str]   # column → key; a change to any of them is recorded
    context: dict[str, str]   # column → key, carried along for reading
    insert_actor: str = "NULL"
    update_actor: str = "NULL"
    on_insert: bool = True


SPECS = (
    # 保护单 (stop / take): the writers that move a level say who (changed_by); status steps are the system's.
    Spec("ai_stop_orders", "protection", "{r}.id", "{r}.user_id", "{r}.trading_account_id",
         {"trigger_price": "triggerPrice", "take_profit_price": "takeProfitPrice", "status": "status", "volume": "volume"},
         {"symbol": "symbol", "position_direction": "direction", "position_bucket": "bucket"},
         insert_actor="NEW.changed_by",
         update_actor="CASE WHEN NEW.trigger_price IS NOT OLD.trigger_price"
                      " OR NEW.take_profit_price IS NOT OLD.take_profit_price THEN NEW.changed_by END"),
    Spec("target_position_plans", "target_plan", "{r}.id", "{r}.user_id", "{r}.trading_account_id",
         {"target_net": "targetNet", "leg_index": "legIndex", "status": "status"}, {"symbol": "symbol"}),
    Spec("price_alerts", "price_alert", "{r}.id", "{r}.user_id", "NULL",
         {"direction": "direction", "price": "price", "name": "name", "note": "note", "status": "status"},
         {"symbol": "symbol"}, insert_actor="NEW.created_via"),
    Spec("ai_execution_controls", "execution_controls", "{r}.user_id || ':' || {r}.trading_account_id", "{r}.user_id",
         "{r}.trading_account_id",
         {"stop_status": "stopStatus", "max_order_volume": "maxOrderVolume", "max_orders": "maxOrders"}, {}),
    # A strategy's new version is a new row (its history already); only the in-place rewrites are recorded.
    Spec("strategy_drafts", "strategy", "{r}.id", "{r}.user_id", "{r}.trading_account_id",
         {"status": "status", "definition_digest": "definitionDigest"}, {"name": "name", "version": "version"},
         on_insert=False),
)


def _object(spec: Spec, row: str) -> str:
    pairs = [f"'{key}', {row}.{column}" for column, key in {**spec.context, **spec.tracked}.items()]
    return f"json_object({', '.join(pairs)})"


def _insert(spec: Spec, row: str, change: str, before: str, after: str, actor: str) -> str:
    def at(expr: str) -> str:
        return expr.format(r=row)
    return (f"INSERT INTO state_changes (at, object_type, object_id, user_id, trading_account_id, change,"
            f" before_json, after_json, actor) VALUES ({NOW}, '{spec.object_type}', {at(spec.object_id)},"
            f" {at(spec.user_id)}, {at(spec.account_id)}, '{change}', {before}, {after}, {actor});")


def trigger_sql(spec: Spec) -> list[str]:
    name = f"state_changes_{spec.table}"
    changed = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in spec.tracked)
    statements = [f"DROP TRIGGER IF EXISTS {name}_{kind}" for kind in ("insert", "update", "delete")]
    if spec.on_insert:
        statements.append(f"CREATE TRIGGER {name}_insert AFTER INSERT ON {spec.table} BEGIN "
                          + _insert(spec, "NEW", "created", "NULL", _object(spec, "NEW"), spec.insert_actor) + " END")
    statements.append(f"CREATE TRIGGER {name}_update AFTER UPDATE ON {spec.table} WHEN {changed} BEGIN "
                      + _insert(spec, "NEW", "updated", _object(spec, "OLD"), _object(spec, "NEW"), spec.update_actor)
                      + " END")
    statements.append(f"CREATE TRIGGER {name}_delete AFTER DELETE ON {spec.table} BEGIN "
                      + _insert(spec, "OLD", "deleted", _object(spec, "OLD"), "NULL", "NULL") + " END")
    return statements


def install(connection: sqlite3.Connection) -> None:
    """The table and every spec's triggers, replaced in one transaction (startup, after all migrations)."""
    connection.executescript(SCHEMA)
    with connection:
        for spec in SPECS:
            for statement in trigger_sql(spec):
                connection.execute(statement)
