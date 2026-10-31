from __future__ import annotations

import json
import sqlite3
from typing import Any

from .base import StoreBase, utc_after, utc_now


class StrategyStore(StoreBase):
    """Backtest strategy snapshots, drafts, validation, intents, monitoring, execution attempts and backtests."""

    def save_strategy_release(
        self, *, release_id: str, artifact_digest: str, source_ref: str,
        manifest: dict[str, Any],
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO strategy_releases
                (id, artifact_digest, source_ref, manifest_json, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (release_id, artifact_digest, source_ref,
                 json.dumps(manifest, ensure_ascii=False), utc_now()),
            )

    # -- 回撤停机 (2026-10-09)：一个策略一份持久状态；跨日/重启不自启 ----------------------------------

    def strategy_drawdown_state(
        self, *, strategy_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM strategy_drawdown_states WHERE strategy_id = ? AND user_id = ?",
                (strategy_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "strategyId": row["strategy_id"],
            "userId": row["user_id"],
            "tradingAccountId": row["trading_account_id"],
            "status": str(row["status"]),
            "limitPercent": float(row["limit_percent"]),
            "initialEquity": row["initial_equity"],
            "baselinePnl": row["baseline_pnl"],
            "peakEquity": row["peak_equity"],
            "currentEquity": row["current_equity"],
            "strategyDigest": row["strategy_digest"],
            "roundStartedAt": row["round_started_at"],
            "triggeredAt": row["triggered_at"],
            "reason": row["reason"],
            "feeSource": row["fee_source"],
            "curve": json.loads(row["curve_json"] or "[]"),
            "updatedAt": row["updated_at"],
        }

    def save_strategy_drawdown_state(
        self, *, strategy_id: str, user_id: str, status: str, limit_percent: float,
        initial_equity: str | None, peak_equity: str | None, current_equity: str | None,
        strategy_digest: str | None, round_started_at: str, triggered_at: str | None = None,
        reason: str | None = None, curve: list[dict[str, Any]] | None = None,
        trading_account_id: str | None = None, baseline_pnl: str | None = None,
        fee_source: str | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_drawdown_states
                (strategy_id, user_id, trading_account_id, status, limit_percent,
                 initial_equity, baseline_pnl, peak_equity, current_equity, strategy_digest,
                 round_started_at, triggered_at, reason, fee_source, curve_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    trading_account_id = excluded.trading_account_id,
                    status = excluded.status,
                    limit_percent = excluded.limit_percent,
                    initial_equity = excluded.initial_equity,
                    baseline_pnl = excluded.baseline_pnl,
                    peak_equity = excluded.peak_equity,
                    current_equity = excluded.current_equity,
                    strategy_digest = excluded.strategy_digest,
                    -- 新一轮的起点与基准同批落库 (2026-10-10 #692)：开新轮的写入必须带上自己的
                    -- round_started_at，否则新基准配旧起点，旧轮亏损会被再算一遍。沿用旧轮的调用
                    -- 传入的本来就是原值，行为不变。
                    round_started_at = excluded.round_started_at,
                    triggered_at = excluded.triggered_at,
                    reason = excluded.reason,
                    fee_source = excluded.fee_source,
                    curve_json = excluded.curve_json,
                    updated_at = excluded.updated_at""",
                (strategy_id, user_id,
                 trading_account_id or self.default_trading_account_id,
                 status, float(limit_percent), initial_equity, baseline_pnl,
                 peak_equity, current_equity,
                 strategy_digest, round_started_at, triggered_at, reason, fee_source,
                 json.dumps(curve or [], ensure_ascii=False), utc_now()),
            )
        state = self.strategy_drawdown_state(strategy_id=strategy_id, user_id=user_id)
        if state is None:
            raise RuntimeError("strategy_drawdown_state_not_persisted")
        return state

    def reset_strategy_drawdown_state(self, *, strategy_id: str, user_id: str) -> None:
        """重置回撤并恢复 (2026-10-09): drop the round entirely — the next monitor poll establishes
        a fresh baseline and peak (the caller refuses while the round is still liquidating)."""
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM strategy_drawdown_states WHERE strategy_id = ? AND user_id = ?",
                (strategy_id, user_id),
            )

    def get_strategy_release(self, release_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM strategy_releases WHERE id = ?", (release_id,)
        ).fetchone()
        if row is None:
            raise KeyError(release_id)
        return {"releaseId": row["id"], "artifactDigest": row["artifact_digest"],
                "sourceRef": row["source_ref"], "manifest": json.loads(row["manifest_json"]),
                "createdAt": row["created_at"]}

    def save_strategy_draft(
        self, *, strategy_id: str, user_id: str, source_session_id: str,
        name: str, symbol: str, version: int, status: str,
        definition: dict[str, Any], definition_digest: str,
        trading_account_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_drafts
                (id, user_id, source_session_id, name, symbol, version, status,
                 definition_json, definition_digest, created_at, updated_at, trading_account_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    strategy_id, user_id, source_session_id, name, symbol, version,
                    status, json.dumps(definition, ensure_ascii=False, sort_keys=True),
                    definition_digest, now, now,
                    trading_account_id or self.default_trading_account_id,
                ),
            )
        return self.get_strategy_draft(strategy_id, user_id=user_id)

    @staticmethod
    def _strategy_draft(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "strategyId": row["id"],
            "userId": row["user_id"],
            "sourceSessionId": row["source_session_id"],
            "name": row["name"],
            "symbol": row["symbol"],
            "version": int(row["version"]),
            "status": row["status"],
            "definition": json.loads(row["definition_json"]),
            "definitionDigest": row["definition_digest"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "tradingAccountId": row["trading_account_id"],
            # 执行方式 (2026-09-25): alert 只提醒 / confirm 出卡确认 / auto 自动下单, and the
            # conversation its reminders and cards go to (null = the owner's latest chat).
            "signalAction": row["signal_action"],
            "signalSessionId": row["signal_session_id"],
        }

    def set_strategy_signal_action(
        self, strategy_id: str, *, user_id: str, signal_action: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        """Change how a monitored instance handles its signal (and where it reports); a field left
        None keeps its stored value."""
        if signal_action is not None and signal_action not in {"alert", "confirm", "auto"}:
            raise ValueError("signal_action_invalid")
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_drafts
                SET signal_action = COALESCE(?, signal_action),
                    signal_session_id = COALESCE(?, signal_session_id), updated_at = ?
                WHERE id = ? AND user_id = ?""",
                (signal_action, session_id, utc_now(), strategy_id, user_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(strategy_id)
        return self.get_strategy_draft(strategy_id, user_id=user_id)

    def get_strategy_draft(
        self, strategy_id: str, *, user_id: str,
    ) -> dict[str, Any]:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM strategy_drafts WHERE id = ? AND user_id = ?",
                (strategy_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(strategy_id)
        return self._strategy_draft(row)

    def update_strategy_scope(self, strategy_id: str, *, user_id: str, definition: dict, digest: str) -> dict:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE strategy_drafts SET symbol = '*', definition_json = ?, definition_digest = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (json.dumps(definition, ensure_ascii=False, sort_keys=True), digest, utc_now(), strategy_id, user_id),
            )
        return self.get_strategy_draft(strategy_id, user_id=user_id)

    def list_strategy_drafts(
        self, *, user_id: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 50)
        with self.lock:
            rows = self.connection.execute(
                """SELECT * FROM strategy_drafts WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (user_id, safe_limit),
            ).fetchall()
        return [self._strategy_draft(row) for row in rows]

    def list_strategy_draft_heads(
        self, *, user_id: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """`list_strategy_drafts` without the definitions (the same rows and order): the activity
        frame reads only these four fields every second, and parsing 50 definitions was a tenth
        of the Runtime at 60 readers (2026-10-13)."""
        safe_limit = min(max(int(limit), 1), 50)
        with self.lock:
            rows = self.connection.execute(
                """SELECT id, name, symbol, status FROM strategy_drafts WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (user_id, safe_limit),
            ).fetchall()
        return [{"strategyId": row["id"], "name": row["name"], "symbol": row["symbol"],
                 "status": row["status"]} for row in rows]

    def list_strategy_library(self, *, user_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                """SELECT * FROM strategy_drafts WHERE user_id = ?
                AND json_extract(definition_json, '$.schemaVersion') = 2
                AND json_extract(definition_json, '$.libraryStrategyId') IS NULL
                AND status != 'archived' ORDER BY updated_at DESC LIMIT 50""", (user_id,),
            ).fetchall()
        return [self._strategy_draft(row) for row in rows]

    def find_library_strategy(self, *, user_id: str, digest: str) -> dict[str, Any] | None:
        # Deduplication is not limited to the first page displayed in the UI.
        with self.lock:
            row = self.connection.execute("""SELECT * FROM strategy_drafts WHERE user_id = ?
                AND definition_digest = ? AND symbol = '*' AND status != 'archived'
                AND json_extract(definition_json, '$.libraryStrategyId') IS NULL
                ORDER BY updated_at DESC LIMIT 1""", (user_id, digest)).fetchone()
        return self._strategy_draft(row) if row is not None else None

    def next_strategy_draft_version(self, *, user_id: str, name: str) -> int:
        with self.lock:
            row = self.connection.execute(
                """SELECT COALESCE(MAX(version), 0) AS current_version
                FROM strategy_drafts WHERE user_id = ? AND name = ?""",
                (user_id, name),
            ).fetchone()
            return int(row["current_version"]) + 1

    def update_strategy_draft_status(
        self, strategy_id: str, *, user_id: str, expected_status: str,
        status: str, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        database = connection or self.connection
        updated_at = utc_now()
        cursor = database.execute(
            """UPDATE strategy_drafts SET status = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = ?""",
            (status, updated_at, strategy_id, user_id, expected_status),
        )
        if connection is None:
            database.commit()
        if cursor.rowcount != 1:
            raise ValueError("strategy_status_conflict")
        return self.get_strategy_draft(strategy_id, user_id=user_id)

    def save_strategy_validation_report(
        self, *, report_id: str, strategy_id: str, user_id: str,
        strategy_digest: str, status: str, checks: list[dict[str, Any]],
        evidence: dict[str, Any], connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        database = connection or self.connection
        now = utc_now()
        database.execute(
            """INSERT INTO strategy_validation_reports
            (id, strategy_id, user_id, strategy_digest, status, checks_json,
             evidence_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report_id, strategy_id, user_id, strategy_digest, status,
                json.dumps(checks, ensure_ascii=False, sort_keys=True),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True), now, now,
            ),
        )
        if connection is None:
            database.commit()
        return self.get_strategy_validation_report(report_id, user_id=user_id)

    @staticmethod
    def _strategy_validation_report(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reportId": row["id"],
            "strategyId": row["strategy_id"],
            "userId": row["user_id"],
            "strategyDigest": row["strategy_digest"],
            "status": row["status"],
            "checks": json.loads(row["checks_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def get_strategy_validation_report(
        self, report_id: str, *, user_id: str,
    ) -> dict[str, Any]:
        with self.lock:
            row = self.connection.execute(
                """SELECT * FROM strategy_validation_reports
                WHERE id = ? AND user_id = ?""",
                (report_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(report_id)
        return self._strategy_validation_report(row)

    def latest_strategy_validation_report(
        self, strategy_id: str, *, user_id: str,
    ) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                """SELECT * FROM strategy_validation_reports
                WHERE strategy_id = ? AND user_id = ?
                ORDER BY created_at DESC, id DESC LIMIT 1""",
                (strategy_id, user_id),
            ).fetchone()
        return self._strategy_validation_report(row) if row is not None else None

    def complete_strategy_validation_report(
        self, report_id: str, *, user_id: str, strategy_digest: str,
        status: str, checks: list[dict[str, Any]], evidence: dict[str, Any],
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        cursor = connection.execute(
            """UPDATE strategy_validation_reports
            SET status = ?, checks_json = ?, evidence_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND strategy_digest = ? AND status = 'incomplete'""",
            (
                status,
                json.dumps(checks, ensure_ascii=False, sort_keys=True),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                utc_now(), report_id, user_id, strategy_digest,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("strategy_validation_report_conflict")
        return self.get_strategy_validation_report(report_id, user_id=user_id)

    @staticmethod
    def _strategy_intent_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "intentId": row["id"],
            "strategyId": row["strategy_id"],
            "userId": row["user_id"],
            "strategyDigest": row["strategy_digest"],
            "signalBarTime": row["signal_bar_time"],
            "contextDigest": row["context_digest"],
            "payload": json.loads(row["payload_json"]),
            "createdAt": row["created_at"],
        }

    def save_strategy_intent_snapshot(
        self, *, intent_id: str, strategy_id: str, user_id: str,
        strategy_digest: str, signal_bar_time: str, context_digest: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO strategy_intent_snapshots
                (id, strategy_id, user_id, strategy_digest, signal_bar_time,
                 context_digest, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    intent_id, strategy_id, user_id, strategy_digest,
                    signal_bar_time, context_digest,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True), now,
                ),
            )
            row = self.connection.execute(
                """SELECT * FROM strategy_intent_snapshots
                WHERE user_id = ? AND strategy_id = ? AND context_digest = ?""",
                (user_id, strategy_id, context_digest),
            ).fetchone()
        if row is None:
            raise RuntimeError("strategy_intent_not_persisted")
        return self._strategy_intent_snapshot(row)

    def list_monitoring_strategies(
        self, *, trading_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        account_id = trading_account_id or self.default_trading_account_id
        with self.lock:
            rows = self.connection.execute(
                """SELECT * FROM strategy_drafts
                WHERE status = 'monitoring' AND trading_account_id = ?
                ORDER BY updated_at, id""",
                (account_id,),
            ).fetchall()
        return [self._strategy_draft(row) for row in rows]

    def strategy_monitor_state(
        self, *, strategy_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                """SELECT * FROM strategy_monitor_states
                WHERE strategy_id = ? AND user_id = ?""",
                (strategy_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "strategyId": row["strategy_id"],
            "userId": row["user_id"],
            "tradingDate": row["trading_date"],
            "highWatermarkEquity": row["high_watermark_equity"],
            "lastTodayPnl": row["last_today_pnl"],
            "consecutiveLosses": int(row["consecutive_losses"]),
            "lastTradeId": row["last_trade_id"],
            "lastExecutedBar": row["last_executed_bar"],
            "pauseReason": row["pause_reason"],
            "lastError": row["last_error"],
            "lastErrorAt": row["last_error_at"],
            "consecutiveFailures": int(row["consecutive_failures"]),
            "nextRetryAt": row["next_retry_at"],
            "updatedAt": row["updated_at"],
        }

    def save_strategy_monitor_state(
        self, *, strategy_id: str, user_id: str, trading_date: str,
        high_watermark_equity: str | None, last_today_pnl: str | None,
        consecutive_losses: int, last_trade_id: str | None,
        last_executed_bar: str | None,
        pause_reason: str | None,
        trading_account_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_monitor_states
                (strategy_id, user_id, trading_date, high_watermark_equity,
                 last_today_pnl, consecutive_losses, last_executed_bar,
                 last_trade_id, pause_reason, updated_at, trading_account_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    trading_date = excluded.trading_date,
                    high_watermark_equity = excluded.high_watermark_equity,
                    last_today_pnl = excluded.last_today_pnl,
                    consecutive_losses = excluded.consecutive_losses,
                    last_trade_id = excluded.last_trade_id,
                    last_executed_bar = excluded.last_executed_bar,
                    pause_reason = excluded.pause_reason,
                    updated_at = excluded.updated_at""",
                (
                    strategy_id, user_id, trading_date, high_watermark_equity,
                    last_today_pnl, consecutive_losses, last_executed_bar, last_trade_id,
                    pause_reason, utc_now(),
                    trading_account_id or self.default_trading_account_id,
                ),
            )
        state = self.strategy_monitor_state(strategy_id=strategy_id, user_id=user_id)
        if state is None:
            raise RuntimeError("strategy_monitor_state_not_persisted")
        return state

    def record_strategy_monitor_error(
        self, *, strategy_id: str, user_id: str, detail: str,
        retry_after_seconds: int, trading_account_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        retry_at = utc_after(retry_after_seconds)
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_monitor_states
                (strategy_id, user_id, trading_date, high_watermark_equity,
                 last_today_pnl, consecutive_losses, last_trade_id,
                 last_executed_bar, pause_reason, last_error, last_error_at,
                 consecutive_failures, next_retry_at, updated_at, trading_account_id)
                VALUES (?, ?, '', NULL, NULL, 0, NULL, NULL, NULL, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    last_error = excluded.last_error,
                    last_error_at = excluded.last_error_at,
                    consecutive_failures = strategy_monitor_states.consecutive_failures + 1,
                    next_retry_at = excluded.next_retry_at,
                    updated_at = excluded.updated_at""",
                (strategy_id, user_id, detail[:300], now, retry_at, now,
                 trading_account_id or self.default_trading_account_id),
            )
        state = self.strategy_monitor_state(strategy_id=strategy_id, user_id=user_id)
        if state is None:
            raise RuntimeError("strategy_monitor_error_not_persisted")
        return state

    def clear_strategy_monitor_error(self, *, strategy_id: str, user_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """UPDATE strategy_monitor_states
                SET last_error = NULL, last_error_at = NULL, consecutive_failures = 0,
                    next_retry_at = NULL, updated_at = ?
                WHERE strategy_id = ? AND user_id = ?""",
                (utc_now(), strategy_id, user_id),
            )

    def strategy_alignment_used(
        self, *, strategy_id: str, user_id: str, alignment_id: str, dispatched_only: bool = False,
        exclude_bar_time: str | None = None,
    ) -> bool:
        # Identity lives in the existing intent/protection records, so it also
        # survives a restart or a stop that has already closed the position.
        dispatch_filter = "AND a.client_order_id IS NOT NULL" if dispatched_only else ""
        with self.lock:
            attempt = self.connection.execute(
                f"""SELECT 1 FROM strategy_execution_attempts a
                JOIN strategy_intent_snapshots i ON i.id = a.intent_id
                WHERE a.user_id = ? AND a.strategy_id = ?
                  AND a.action IN ('open_long', 'open_short')
                  AND COALESCE(a.signal_action, '') != 'alert'
                  AND json_extract(i.payload_json, '$.signalSnapshot.alignmentId') = ?
                  AND (? IS NULL OR a.signal_bar_time != ?)
                  {dispatch_filter} LIMIT 1""", (user_id, strategy_id, alignment_id, exclude_bar_time, exclude_bar_time),
            ).fetchone()
            stop = self.connection.execute(
                """SELECT 1 FROM ai_stop_orders WHERE user_id = ?
                  AND json_extract(strategy_metadata_json, '$.strategyId') = ?
                  AND json_extract(strategy_metadata_json, '$.alignmentId') = ? LIMIT 1""",
                (user_id, strategy_id, alignment_id),
            ).fetchone()
        return attempt is not None or stop is not None

    def reserve_strategy_execution_attempt(
        self, *, attempt_id: str, strategy_id: str, user_id: str,
        intent_id: str, signal_bar_time: str, trading_day: str,
        action: str, volume: int, trading_account_id: str | None = None,
        signal_action: str | None = None,
    ) -> bool:
        now = utc_now()
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO strategy_execution_attempts
                (id, strategy_id, user_id, intent_id, signal_bar_time, trading_day,
                 action, volume, status, client_order_id, detail, created_at, updated_at, trading_account_id,
                 signal_action)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', NULL, NULL, ?, ?, ?, ?)""",
                (
                    attempt_id, strategy_id, user_id, intent_id,
                    signal_bar_time, trading_day, action, volume, now, now,
                    trading_account_id or self.default_trading_account_id, signal_action,
                ),
            )
        return cursor.rowcount == 1

    def _latest_offer(self, *, strategy_id: str, user_id: str) -> tuple[Any, Any] | None:
        """The instance's latest reminder / card attempt and its signal reset time."""
        offer = self.connection.execute(
            """SELECT action, created_at FROM strategy_execution_attempts
            WHERE strategy_id = ? AND user_id = ? AND signal_action IN ('alert', 'confirm')
              AND status != 'FAILED'
            ORDER BY created_at DESC, id DESC LIMIT 1""",
            (strategy_id, user_id),
        ).fetchone()
        if offer is None:
            return None
        state = self.connection.execute(
            "SELECT signal_reset_at FROM strategy_monitor_states WHERE strategy_id = ? AND user_id = ?",
            (strategy_id, user_id),
        ).fetchone()
        return offer, (state["signal_reset_at"] if state is not None else None)

    def monitoring_since(self, *, strategy_id: str, user_id: str) -> str | None:
        """When the instance last went into monitoring (start or 继续), read from 改动留痕: a 提醒's
        到期 (今天收盘 / 本周) counts from here. None = no recorded start (started before 2026-10-30)."""
        with self.lock:
            row = self.connection.execute(
                """SELECT at FROM state_changes WHERE object_type = 'strategy' AND object_id = ? AND user_id = ?
                  AND change = 'updated' AND json_extract(after_json, '$.status') = 'monitoring'
                  AND json_extract(before_json, '$.status') IS NOT 'monitoring'
                ORDER BY seq DESC LIMIT 1""",
                (strategy_id, user_id),
            ).fetchone()
        return str(row["at"]) if row is not None else None

    def signal_already_offered(self, *, strategy_id: str, user_id: str, action: str) -> bool:
        """只提醒 / 出卡确认: the latest reminder or card was for this same action and the signal has
        not moved away from it since — so it is still the same signal, already offered."""
        with self.lock:
            latest = self._latest_offer(strategy_id=strategy_id, user_id=user_id)
        if latest is None:
            return False
        offer, reset_at = latest
        return str(offer["action"]) == action and (reset_at is None or str(reset_at) < str(offer["created_at"]))

    def note_strategy_signal(self, *, strategy_id: str, user_id: str, action: str) -> None:
        """Record that the evaluated signal moved away from the last offered one (so the next
        time it comes back it is offered again)."""
        with self.lock:
            latest = self._latest_offer(strategy_id=strategy_id, user_id=user_id)
        if latest is None:
            return
        offer, reset_at = latest
        if str(offer["action"]) != action and (reset_at is None or str(reset_at) < str(offer["created_at"])):
            self.reset_strategy_signal(strategy_id=strategy_id, user_id=user_id)

    def reset_strategy_signal(self, *, strategy_id: str, user_id: str) -> None:
        """The current signal is offered afresh (signal changed, 执行方式 changed, stopped)."""
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE strategy_monitor_states SET signal_reset_at = ? WHERE strategy_id = ? AND user_id = ?",
                (utc_now(), strategy_id, user_id),
            )

    def strategy_signal_marks(
        self, *, strategy_id: str, user_id: str,
    ) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
        """主动提醒去重 (2026-10-12): the last (action, bar) the monitor judged and the last one it
        announced. Kept beside the instance's monitor state so a restart or a blue-green switch
        neither re-announces the same bar nor mistakes a standing signal for a fresh change."""
        with self.lock:
            row = self.connection.execute(
                "SELECT signal_seen, signal_alerted FROM strategy_monitor_states WHERE strategy_id = ? AND user_id = ?",
                (strategy_id, user_id),
            ).fetchone()

        return (self._signal_mark(row["signal_seen"]), self._signal_mark(row["signal_alerted"])) \
            if row is not None else (None, None)

    @staticmethod
    def _signal_mark(value: Any) -> tuple[str, str] | None:
        try:
            action, bar = json.loads(value)
        except (TypeError, ValueError):
            return None
        return str(action), str(bar)

    def claim_strategy_signal_marks(
        self, *, strategy_id: str, user_id: str,
        expected: tuple[tuple[str, str] | None, tuple[str, str] | None],
        seen: tuple[str, str], alerted: tuple[str, str] | None, trading_account_id: str | None = None,
    ) -> bool:
        """Conditional write of the marks `strategy_signal_marks` returned as `expected`: True only
        when this call changed them from exactly those values, so of two monitors judging the same
        bar (a blue-green overlap) one wins the announcement. A False return means someone else
        already moved the marks — the caller does not announce."""
        encoded_seen = json.dumps(list(seen))
        encoded_alerted = json.dumps(list(alerted)) if alerted is not None else None
        with self.lock, self.connection:
            row = self.connection.execute(
                "SELECT signal_seen, signal_alerted FROM strategy_monitor_states WHERE strategy_id = ? AND user_id = ?",
                (strategy_id, user_id),
            ).fetchone()
            if row is None:
                if expected != (None, None):
                    return False
                cursor = self.connection.execute(
                    """INSERT INTO strategy_monitor_states
                    (strategy_id, user_id, trading_date, consecutive_losses, consecutive_failures,
                     signal_seen, signal_alerted, updated_at, trading_account_id)
                    VALUES (?, ?, '', 0, 0, ?, ?, ?, ?)
                    ON CONFLICT(strategy_id) DO NOTHING""",
                    (strategy_id, user_id, encoded_seen, encoded_alerted, utc_now(),
                     trading_account_id or self.default_trading_account_id),
                )
                return cursor.rowcount == 1
            if (self._signal_mark(row["signal_seen"]), self._signal_mark(row["signal_alerted"])) != expected:
                return False
            cursor = self.connection.execute(
                """UPDATE strategy_monitor_states SET signal_seen = ?, signal_alerted = ?
                WHERE strategy_id = ? AND user_id = ? AND signal_seen IS ? AND signal_alerted IS ?""",
                (encoded_seen, encoded_alerted, strategy_id, user_id, row["signal_seen"], row["signal_alerted"]),
            )
            return cursor.rowcount == 1

    def finish_strategy_execution_attempt(
        self, *, attempt_id: str, status: str,
        client_order_id: str | None = None, detail: str | None = None,
    ) -> None:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_execution_attempts
                SET status = ?, client_order_id = ?, detail = ?, updated_at = ?
                WHERE id = ? AND status = 'RESERVED'""",
                (status, client_order_id, detail, utc_now(), attempt_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("strategy_execution_attempt_conflict")

    def settle_strategy_card_attempt(
        self, *, attempt_id: str, detail: str, connection: sqlite3.Connection | None = None,
    ) -> None:
        """A strategy's confirmation card was given up (放弃) or timed out (作废): its attempt, which
        waited as SKIPPED with the card, reads CANCELLED with why — the 运行中 card stops saying
        出卡待确认. Nothing else about the attempt changes (it still marks the bar as handled)."""
        db = connection or self.connection
        db.execute(
            """UPDATE strategy_execution_attempts SET status = 'CANCELLED', detail = ?, updated_at = ?
            WHERE id = ? AND status = 'SKIPPED' AND card_proposal_ids IS NOT NULL""",
            (detail, utc_now(), attempt_id),
        )

    def retry_strategy_execution_attempt(
        self, *, strategy_id: str, user_id: str, signal_bar_time: str,
        trading_day: str, action: str, volume: int, strategy_digest: str,
        plans: list[tuple[str | None, int]],
    ) -> tuple[str, list[dict[str, Any]]] | None:
        with self.transaction() as connection:
            attempt = connection.execute(
                """SELECT a.* FROM strategy_execution_attempts a
                JOIN strategy_intent_snapshots i ON i.id = a.intent_id
                WHERE a.user_id = ? AND a.strategy_id = ? AND a.signal_bar_time = ?
                AND a.trading_day = ? AND a.action = ? AND a.volume = ?
                AND a.status = 'NOT_DISPATCHED' AND i.strategy_digest = ?""",
                (user_id, strategy_id, signal_bar_time, trading_day, action, volume, strategy_digest),
            ).fetchone()
            if attempt is None:
                return None
            legs = connection.execute(
                "SELECT * FROM strategy_execution_legs WHERE attempt_id = ? ORDER BY leg_index",
                (attempt["id"],),
            ).fetchall()
            # Do not reopen a partial/unknown execution or change a persisted leg.
            if not legs or len(legs) > len(plans):
                return None
            for index, leg in enumerate(legs):
                if (leg["status"] != "NOT_DISPATCHED" or leg["leg_index"] != index
                        or (leg["position_bucket"], leg["volume"]) != plans[index]):
                    return None
                order = connection.execute(
                    "SELECT submit_phase FROM tqkq_sim_order_links WHERE client_order_id = ? AND user_id = ?",
                    (leg["client_order_id"], user_id),
                ).fetchone()
                if order is None or order["submit_phase"] != "PREPARED":
                    return None
            connection.execute(
                "UPDATE strategy_execution_attempts SET status = 'RESERVED', updated_at = ? WHERE id = ?",
                (utc_now(), attempt["id"]),
            )
            connection.execute(
                "UPDATE strategy_execution_legs SET status = 'RESERVED', updated_at = ? WHERE attempt_id = ?",
                (utc_now(), attempt["id"]),
            )
            return str(attempt["id"]), [dict(leg) for leg in legs]

    def bind_strategy_execution_leg(self, *, leg_id: str, client_order_id: str) -> None:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_execution_legs SET client_order_id = ?, updated_at = ?
                WHERE id = ? AND status = 'RESERVED'
                AND (client_order_id IS NULL OR client_order_id = ?)""",
                (client_order_id, utc_now(), leg_id, client_order_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("strategy_execution_leg_conflict")

    def reserve_strategy_execution_leg(
        self, *, leg_id: str, attempt_id: str, leg_index: int,
        position_bucket: str | None, volume: int,
    ) -> None:
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO strategy_execution_legs
                (id, attempt_id, leg_index, position_bucket, volume,
                 client_order_id, status, detail, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, 'RESERVED', NULL, ?, ?)""",
                (leg_id, attempt_id, leg_index, position_bucket, volume, now, now),
            )

    def finish_strategy_execution_leg(
        self, *, leg_id: str, status: str,
        client_order_id: str | None = None, detail: str | None = None,
    ) -> None:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_execution_legs
                SET status = ?, client_order_id = COALESCE(?, client_order_id), detail = ?, updated_at = ?
                WHERE id = ? AND status = 'RESERVED'""",
                (status, client_order_id, detail, utc_now(), leg_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("strategy_execution_leg_conflict")

    def update_strategy_execution_attempt(
        self, *, attempt_id: str, status: str,
        client_order_id: str | None = None, detail: str | None = None, outcome: str | None = None,
    ) -> None:
        """价差执行的多段生命周期 (撤单 / 补单 / 回退 / 未知): the spread executor rewrites the
        attempt unconditionally as its legs move — unlike finish_, which is the single RESERVED →
        terminal write of the single-contract path. ``outcome`` is the structured conclusion the
        settled card shows; COALESCE keeps an earlier one when a later write carries none."""
        with self.lock, self.connection:
            self.connection.execute(
                """UPDATE strategy_execution_attempts
                SET status = ?, client_order_id = COALESCE(?, client_order_id), detail = ?,
                    outcome = COALESCE(?, outcome), updated_at = ?
                WHERE id = ?""",
                (status, client_order_id, detail, outcome, utc_now(), attempt_id),
            )

    def update_strategy_execution_leg(
        self, *, leg_id: str, status: str,
        client_order_id: str | None = None, detail: str | None = None,
    ) -> None:
        """The same unconditional write for one spread leg (chase and rollback legs included)."""
        with self.lock, self.connection:
            self.connection.execute(
                """UPDATE strategy_execution_legs
                SET status = ?, client_order_id = COALESCE(?, client_order_id), detail = ?, updated_at = ?
                WHERE id = ?""",
                (status, client_order_id, detail, utc_now(), leg_id),
            )

    def reopen_strategy_execution_attempt(self, *, attempt_id: str) -> bool:
        """出卡确认 (价差): the card-waiting SKIPPED attempt resumes as the executor's target, so its
        legs land on the same attempt row the card came from."""
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE strategy_execution_attempts SET status = 'RESERVED', updated_at = ? WHERE id = ? AND status = 'SKIPPED'",
                (utc_now(), attempt_id),
            )
        return cursor.rowcount == 1

    def mark_strategy_execution_follow(self, *, attempt_id: str, state: str) -> None:
        """价差执行: 'following' while the executor's background task is still watching the legs,
        'done' once it reached its outcome. The restart sweep marks a still-following attempt
        UNKNOWN — SUBMITTED alone cannot tell dispatch from a completed fill."""
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE strategy_execution_attempts SET follow_state = ?, updated_at = ? WHERE id = ?",
                (state, utc_now(), attempt_id),
            )

    def note_strategy_executed_bar(
        self, *, strategy_id: str, user_id: str, trading_day: str, bar_time: str,
    ) -> None:
        """A spread attempt fully filled: the same cooldown-bar write confirm_strategy_card_order
        does (the monitor re-reading the same bar must not treat the position as fresh signal)."""
        with self.lock, self.connection:
            self.connection.execute(
                """UPDATE strategy_monitor_states SET last_executed_bar = ?, updated_at = ?
                WHERE strategy_id = ? AND user_id = ? AND trading_date = ?
                AND (last_executed_bar IS NULL OR last_executed_bar < ?)""",
                (bar_time, utc_now(), strategy_id, user_id, trading_day, bar_time),
            )

    def record_strategy_cards(
        self, *, attempt_id: str, proposal_ids: list[str], detail: str,
    ) -> None:
        """出卡确认: the reserved attempt produced confirmation cards instead of orders. It is
        finished as SKIPPED (not an open toward maxOrdersPerDay) and remembers its cards, so a
        later confirmation can carry the order back onto it."""
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """UPDATE strategy_execution_attempts
                SET status = 'SKIPPED', card_proposal_ids = ?, detail = ?, updated_at = ?
                WHERE id = ? AND status = 'RESERVED'""",
                (",".join(proposal_ids), detail, utc_now(), attempt_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("strategy_execution_attempt_conflict")

    def confirm_strategy_card_order(
        self, *, attempt_id: str, user_id: str, proposal_id: str, client_order_id: str,
        status: str, detail: str, connection: sqlite3.Connection | None = None,
    ) -> bool:
        """A strategy card was confirmed and its order sent through the normal confirm path: the
        attempt takes the order (so the fill reads as this strategy's), the dispatched status (so
        it now counts toward maxOrdersPerDay) and, on the attempt's trading day, the cooldown bar.
        A stop_close attempt (停止并平仓) skips the cooldown write — its signal_bar_time is the
        stop's own identity token, not a bar. ``connection`` links the order inside the caller's
        confirmation transaction, before dispatch (#654); only the attempt that issued this card
        is touched; a second card's confirm appends."""
        if connection is not None:
            return self._confirm_strategy_card_order(
                connection, attempt_id=attempt_id, user_id=user_id, proposal_id=proposal_id,
                client_order_id=client_order_id, status=status, detail=detail, cooldown=True)
        with self.transaction() as db:
            return self._confirm_strategy_card_order(
                db, attempt_id=attempt_id, user_id=user_id, proposal_id=proposal_id,
                client_order_id=client_order_id, status=status, detail=detail, cooldown=True)

    def _confirm_strategy_card_order(
        self, db: sqlite3.Connection, *, attempt_id: str, user_id: str, proposal_id: str,
        client_order_id: str, status: str, detail: str, cooldown: bool,
    ) -> bool:
        dispatched = {"SUBMITTED", "PARTIAL", "UNKNOWN"}
        row = db.execute(
            "SELECT * FROM strategy_execution_attempts WHERE id = ? AND user_id = ?",
            (attempt_id, user_id),
        ).fetchone()
        if row is None or proposal_id not in str(row["card_proposal_ids"] or "").split(","):
            return False
        if str(row["action"]) == "stop_close":
            cooldown = False
        orders = [item for item in str(row["client_order_id"] or "").split(",") if item]
        if client_order_id not in orders:
            orders.append(client_order_id)
        current = str(row["status"])
        if "UNKNOWN" in {current, status}:
            merged = "UNKNOWN"
        elif current in dispatched:
            merged = "SUBMITTED" if status == current == "SUBMITTED" else "PARTIAL"
        else:
            merged = status
        db.execute(
            """UPDATE strategy_execution_attempts
            SET status = ?, client_order_id = ?, detail = ?, updated_at = ?
            WHERE id = ?""",
            (merged, ",".join(orders), detail, utc_now(), attempt_id),
        )
        if cooldown and merged in dispatched:
            db.execute(
                """UPDATE strategy_monitor_states SET last_executed_bar = ?, updated_at = ?
                WHERE strategy_id = ? AND user_id = ? AND trading_date = ?
                AND (last_executed_bar IS NULL OR last_executed_bar < ?)""",
                (row["signal_bar_time"], utc_now(), row["strategy_id"], user_id,
                 row["trading_day"], row["signal_bar_time"]),
            )
        return True

    def count_strategy_attempts_today(
        self, *, strategy_id: str, user_id: str, trading_date: str,
    ) -> int:
        with self.lock:
            row = self.connection.execute(
                """SELECT COUNT(*) AS count FROM strategy_execution_attempts
                WHERE strategy_id = ? AND user_id = ?
                  AND trading_day = ?
                  AND action IN ('open_long', 'open_short')
                  AND status IN ('SUBMITTED', 'PARTIAL', 'UNKNOWN')""",
                (strategy_id, user_id, trading_date),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def strategy_signals_today(
        self, *, strategy_id: str, user_id: str, trading_date: str,
    ) -> dict[str, Any]:
        """今天几个信号 (策略卡, 2026-10-08): every execution attempt of the day — a reminder, a
        card, an order, a blocked one — counts as one signal; `lastOpen` is the direction of the
        day's latest open signal (long / short / None), whatever became of it. 停止并平仓's
        stop_close attempts are the user's own exit, not a signal, and do not count (#654)."""
        with self.lock:
            count = self.connection.execute(
                """SELECT COUNT(*) AS count FROM strategy_execution_attempts
                WHERE strategy_id = ? AND user_id = ? AND trading_day = ? AND action != 'stop_close'""",
                (strategy_id, user_id, trading_date)).fetchone()
            latest = self.connection.execute(
                """SELECT action FROM strategy_execution_attempts
                WHERE strategy_id = ? AND user_id = ? AND trading_day = ? AND action IN ('open_long', 'open_short')
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (strategy_id, user_id, trading_date)).fetchone()
        return {"count": int(count["count"] if count is not None else 0),
                "lastOpen": ("long" if latest["action"] == "open_long" else "short") if latest is not None else None}

    def count_strategy_opens_by_direction(
        self, *, strategy_id: str, user_id: str, trading_date: str,
    ) -> dict[str, int]:
        """每日每方向开仓次数: dispatched opens of one trading day, per direction (the same
        dispatched statuses as count_strategy_attempts_today). 加仓 fills are not new entries and
        do not count here (they still count towards maxOrdersPerDay)."""
        with self.lock:
            rows = self.connection.execute(
                """SELECT action, COUNT(*) AS count FROM strategy_execution_attempts
                WHERE strategy_id = ? AND user_id = ? AND trading_day = ?
                  AND action IN ('open_long', 'open_short')
                  AND status IN ('SUBMITTED', 'PARTIAL', 'UNKNOWN')
                  AND NOT EXISTS (SELECT 1 FROM strategy_intent_snapshots s
                      WHERE s.id = strategy_execution_attempts.intent_id
                      AND json_extract(s.payload_json, '$.signalSnapshot.add') = 1)
                GROUP BY action""",
                (strategy_id, user_id, trading_date),
            ).fetchall()
        counts = {str(row["action"]): int(row["count"]) for row in rows}
        return {"long": counts.get("open_long", 0), "short": counts.get("open_short", 0)}

    def strategy_last_open_bars(self, *, strategy_id: str, user_id: str) -> dict[str, int | None]:
        """复位: the signal bar (ns) of this strategy's latest dispatched open per direction — whatever
        the 执行方式 (a confirmed card counts once its order is sent) — so the latch survives a restart.
        加仓 does not re-arm the latch: only a fresh entry does."""
        with self.lock:
            rows = self.connection.execute(
                """SELECT action, MAX(CAST(signal_bar_time AS INTEGER)) AS bar FROM strategy_execution_attempts
                WHERE strategy_id = ? AND user_id = ? AND action IN ('open_long', 'open_short')
                  AND status IN ('SUBMITTED', 'PARTIAL', 'UNKNOWN') AND signal_bar_time GLOB '[0-9]*'
                  AND NOT EXISTS (SELECT 1 FROM strategy_intent_snapshots s
                      WHERE s.id = strategy_execution_attempts.intent_id
                      AND json_extract(s.payload_json, '$.signalSnapshot.add') = 1)
                GROUP BY action""",
                (strategy_id, user_id),
            ).fetchall()
        bars = {str(row["action"]): int(row["bar"]) for row in rows if row["bar"] is not None}
        return {"long": bars.get("open_long"), "short": bars.get("open_short")}

    def strategy_exit_keys(self, *, strategy_id: str, user_id: str) -> frozenset[str]:
        """Time exits (收盘前平仓 / 最长持仓) this strategy already acted on — a reminder, a card or
        an order, whatever the 执行方式 — read from its attempts' intent snapshots, so a restart
        never repeats one. A failed attempt does not count."""
        with self.lock:
            rows = self.connection.execute(
                """SELECT json_extract(i.payload_json, '$.signalSnapshot.exitKey') AS exit_key
                FROM strategy_execution_attempts a
                JOIN strategy_intent_snapshots i ON i.id = a.intent_id
                WHERE a.user_id = ? AND a.strategy_id = ? AND a.status != 'FAILED'
                  AND json_extract(i.payload_json, '$.signalSnapshot.exitKey') IS NOT NULL
                ORDER BY a.created_at DESC LIMIT 200""",
                (user_id, strategy_id),
            ).fetchall()
        return frozenset(str(row["exit_key"]) for row in rows)

    def strategy_stop_close_attempt(self, attempt_id: str, *, user_id: str) -> dict[str, Any] | None:
        """A stop_close attempt with its intent snapshot's payload — the confirm-time binding
        check of a stop-close card (#675): the card's claimed attribution is proven against
        what 停止并平仓 persisted (attempt ↔ card ↔ intent ↔ strategy), never the
        strategySource text alone. The intent joins on its own user_id / strategy_id matching
        the attempt's, so a snapshot pasted onto another strategy's attempt reads as no
        binding. None when the attempt is missing, not this user's or not a stop_close
        attempt."""
        with self.lock:
            row = self.connection.execute(
                """SELECT a.*, i.payload_json AS intent_payload, i.created_at AS intent_created_at
                FROM strategy_execution_attempts a
                LEFT JOIN strategy_intent_snapshots i
                    ON i.id = a.intent_id AND i.user_id = a.user_id AND i.strategy_id = a.strategy_id
                WHERE a.id = ? AND a.user_id = ?""",
                (attempt_id, user_id),
            ).fetchone()
        if row is None or str(row["action"]) != "stop_close":
            return None
        return {
            "attemptId": row["id"],
            "strategyId": row["strategy_id"],
            "tradingAccountId": row["trading_account_id"],
            "tradingDay": row["trading_day"],
            "status": row["status"],
            "clientOrderIds": [item for item in str(row["client_order_id"] or "").split(",") if item],
            "cardProposalIds": [item for item in str(row["card_proposal_ids"] or "").split(",") if item],
            "intentPayload": json.loads(row["intent_payload"] or "null") or {},
        }

    def list_strategy_execution_attempts(
        self, *, strategy_id: str, user_id: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 100)
        with self.lock:
            rows = self.connection.execute(
                """SELECT * FROM strategy_execution_attempts
                WHERE strategy_id = ? AND user_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?""",
                (strategy_id, user_id, safe_limit),
            ).fetchall()
        attempts = [{
            "attemptId": row["id"],
            "strategyId": row["strategy_id"],
            "intentId": row["intent_id"],
            "signalBarTime": row["signal_bar_time"],
            "tradingDay": row["trading_day"],
            "action": row["action"],
            "volume": int(row["volume"]),
            "status": row["status"],
            "clientOrderId": row["client_order_id"],
            "detail": row["detail"],
            # 出卡确认: the cards this attempt issued (empty for auto attempts); SKIPPED with
            # cards = waiting for the user's confirmation.
            "cardProposalIds": [item for item in str(row["card_proposal_ids"] or "").split(",") if item],
            # alert / confirm when a reminder or a card produced it; null = auto.
            "signalAction": row["signal_action"],
            # The structured conclusion of a settled spread follow (null until it settles).
            "outcome": row["outcome"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        } for row in rows]
        if not attempts:
            return attempts
        attempt_ids = [attempt["attemptId"] for attempt in attempts]
        placeholders = ",".join("?" for _ in attempt_ids)
        with self.lock:
            leg_rows = self.connection.execute(
                f"""SELECT * FROM strategy_execution_legs
                WHERE attempt_id IN ({placeholders}) ORDER BY attempt_id, leg_index""",
                attempt_ids,
            ).fetchall()
        legs_by_attempt: dict[str, list[dict[str, Any]]] = {}
        for row in leg_rows:
            legs_by_attempt.setdefault(str(row["attempt_id"]), []).append({
                "legId": row["id"],
                "legIndex": int(row["leg_index"]),
                "positionBucket": row["position_bucket"],
                "volume": int(row["volume"]),
                "clientOrderId": row["client_order_id"],
                "status": row["status"],
                "detail": row["detail"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            })
        for attempt in attempts:
            attempt["legs"] = legs_by_attempt.get(str(attempt["attemptId"]), [])
        return attempts

    def save_backtest_run(
        self, *, backtest_id: str, session_id: str, user_id: str,
        artifact_digest: str, strategy_release_id: str, data_digest: str,
        parameters: dict[str, Any], manifest: dict[str, Any], artifact_path: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO backtest_runs
                (id, session_id, user_id, status, artifact_digest, strategy_release_id,
                 data_digest, parameters_json, manifest_json, artifact_path, created_at, updated_at)
                VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (backtest_id, session_id, user_id, artifact_digest, strategy_release_id,
                 data_digest, json.dumps(parameters, ensure_ascii=False),
                 json.dumps(manifest, ensure_ascii=False), artifact_path, now, now),
            )
        return self.get_backtest_run(backtest_id, user_id=user_id)

    def get_backtest_run(self, backtest_id: str, *, user_id: str | None = None) -> dict[str, Any]:
        query = "SELECT * FROM backtest_runs WHERE id = ?"
        values: tuple[Any, ...] = (backtest_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            values += (user_id,)
        with self.lock:
            row = self.connection.execute(query, values).fetchone()
        if row is None:
            raise KeyError(backtest_id)
        return {
            "backtestId": row["id"], "sessionId": row["session_id"],
            "userId": row["user_id"], "status": row["status"],
            "artifactDigest": row["artifact_digest"],
            "strategyReleaseId": row["strategy_release_id"],
            "dataDigest": row["data_digest"],
            "parameters": json.loads(row["parameters_json"]),
            "manifest": json.loads(row["manifest_json"]),
            "artifactPath": row["artifact_path"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    def latest_backtest_run(self, *, session_id: str, user_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.connection.execute(
                """SELECT id FROM backtest_runs
                WHERE session_id = ? AND user_id = ? AND status = 'completed'
                ORDER BY created_at DESC, id DESC LIMIT 1""",
                (session_id, user_id),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return self.get_backtest_run(str(row["id"]), user_id=user_id)

    def discard_backtest_run(self, backtest_id: str, *, user_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM backtest_runs WHERE id = ? AND user_id = ?",
                (backtest_id, user_id),
            )
