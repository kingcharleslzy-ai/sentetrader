import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader_runtime.backtests import BacktestService
from trader_runtime.broker import EventBroker
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.proposals import ProposalConflict, ProposalService
from trader_runtime.release_manager import StrategyReleaseManager
from trader_runtime.store import RuntimeStore
from trader_runtime.trader_tools import TraderReadOnlyTools


def strategy_tools(tmp_path: Path) -> tuple[RuntimeStore, TraderReadOnlyTools, str]:
    store = RuntimeStore(tmp_path / "strategies.sqlite3")
    broker = EventBroker(store)
    provider = FakeReadOnlyDataProvider()
    proposals = ProposalService(store, broker, data_provider=provider)
    session = broker.create_session("local", "native", "local-user")
    tools = TraderReadOnlyTools(
        provider,
        lambda mode: {"mode": mode, **provider.runtime_status()},
        proposals=proposals,
    )
    return store, tools, session.session_id


# Old-format (schemaVersion 1) drafts: the AI's create_strategy_draft saved these before #932 F5 (it now saves a
# StrategyPlan through POST /api/strategy-plans); drafts saved then keep the lifecycle these tests cover.
LEGACY_DEFAULTS = {"timeframeSeconds": 300, "direction": "both", "fastPeriod": 5, "slowPeriod": 20, "breakoutPeriod": 20,
                   "kdPeriod": 9, "macdFast": 12, "macdSlow": 26, "macdSignal": 9, "minimumVolumeRatio": 1.0,
                   "maxPosition": 1, "maxOrdersPerDay": 3, "stopLossTicks": 20, "takeProfitTicks": 40, "cooldownBars": 1}


def legacy_draft(tools: TraderReadOnlyTools, session_id: str, arguments: dict) -> tuple[str, dict]:
    values = {**LEGACY_DEFAULTS, **arguments}
    parameters = {key: values[key] for key in ("fastPeriod", "slowPeriod", "breakoutPeriod", "kdPeriod", "macdFast",
                                                "macdSlow", "macdSignal", "minimumVolumeRatio")}
    assert tools.proposals is not None
    return "card.strategy", tools.proposals.create_strategy_draft(
        session_id, "turn-create", name=values["name"], symbol=values["symbol"], template=values["template"],
        timeframe_seconds=values["timeframeSeconds"], direction=values["direction"], parameters=parameters,
        max_position=values["maxPosition"], max_orders_per_day=values["maxOrdersPerDay"],
        stop_loss_ticks=values["stopLossTicks"], take_profit_ticks=values["takeProfitTicks"],
        cooldown_bars=values["cooldownBars"], rationale=values["rationale"])


class StrategyHistoryProvider(FakeReadOnlyDataProvider):
    def historical_backtest(self, request: dict) -> dict:
        start = int(datetime(
            2025, 12, 31, 16, tzinfo=UTC
        ).timestamp() * 1_000_000_000)
        bars = []
        price = 100.0
        for index in range(500):
            step = 1.0 if index % 60 < 30 else -1.0
            opening = price
            price += step
            bars.append({
                "datetime": start + index * 300_000_000_000,
                "open": opening,
                "high": max(opening, price) + 0.2,
                "low": min(opening, price) - 0.2,
                "close": price,
                "volume": 100 + index % 3,
                "openInterest": 8_000 + index,
                "tradingDay": datetime.fromtimestamp(
                    (start + index * 300_000_000_000) / 1_000_000_000,
                    tz=UTC,
                ).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
            })
        digest = hashlib.sha256(json.dumps(
            bars, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        metrics = {
            "returnPercent": 1.0,
            "maxDrawdownPercent": 0.5,
            "signalCount": 8,
            "winRatePercent": 60.0,
            "tradeCount": 8,
            "commission": 10.0,
            "curve": [1_000_000.0, 1_010_000.0],
        }
        return {
            "engineVersion": "tqsdk-3.10.2-tqsim-tqbacktest-test",
            "engineDigest": "a" * 64,
            "dataSource": f"tqsdk:{request['symbol']}:test",
            "dataDigest": digest,
            "dataPointCount": len(bars),
            "contractMultiplier": 1.0,
            "priceTick": 1.0,
            "fillMode": "ACTIVE",
            "commissionModel": "tqsdk_default",
            "bars": bars,
            "baseline": metrics,
            "candidate": metrics,
            "executionEnabled": False,
            "orderCapabilities": [],
            "accountType": "TqSim",
        }


class IntentPreviewProvider(StrategyHistoryProvider):
    def positions(self) -> list[dict]:
        return []

    def account_activity(self) -> dict:
        return {
            "schemaVersion": 1,
            "capturedAt": "2026-01-03T09:00:00+08:00",
            "ageSeconds": 0,
            "dataStatus": "simulated",
            "ordersInitialized": True,
            "orders": [],
            "trades": [],
            "liveTrading": "disabled",
        }

    def account_summary(self, turn_id: str) -> dict:
        result = super().account_summary(turn_id)
        result.update({
            "capturedAt": "2026-01-03T09:00:00+08:00",
            "ageSeconds": 0,
            "dataStatus": "simulated",
            "isCached": False,
        })
        return result

    def market_snapshot(self, turn_id: str, symbol: str) -> dict:
        result = super().market_snapshot(turn_id, symbol)
        result.update({
            "symbol": symbol,
            "price": 110.0,
            "priceTick": 1.0,
            "volumeMultiple": 10.0,
            "capturedAt": "2026-01-03T09:00:00+08:00",
            "ageSeconds": 0,
            "dataStatus": "simulated",
        })
        return result

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict:
        result = super().market_workbench(symbol, duration_seconds)
        for bar in result["bars"][-31:-1]:
            bar.update({
                "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.0, "volume": 100,
            })
        result["bars"][-2].update({
            "open": 100.0, "high": 111.0, "low": 99.0,
            "close": 110.0, "volume": 200,
        })
        return result


def test_ai_can_read_failed_validation_reasons_without_starting_strategy(tmp_path: Path) -> None:
    store, tools, session_id = strategy_tools(tmp_path)
    try:
        strategy = store.save_strategy_draft(strategy_id="failed-strategy", user_id="local-user",
            source_session_id=session_id, name="亏损策略", symbol="SHFE.au2612", version=1,
            status="validating", definition={}, definition_digest="a" * 64)
        store.save_strategy_validation_report(report_id="failed-validation", strategy_id=strategy["strategyId"],
            user_id="local-user", strategy_digest="a" * 64, status="failed",
            checks=[{"id": "out_of_sample", "label": "样本外验证", "status": "failed", "detail": "扣成本收益 -0.244%"}],
            evidence={"outOfSample": {"returnPercent": -0.244}, "liveTrading": "disabled"})
        result, _ = tools.call(tool="get_strategy_draft", arguments={"strategyId": strategy["strategyId"]},
            turn_id="read-validation", mode="local", session_id=session_id, user_id="local-user")
        payload = json.loads(result["contentItems"][0]["text"])["data"]
        assert payload["latestValidationReport"]["checks"][0]["detail"] == "扣成本收益 -0.244%"
        assert payload["latestValidationReport"]["status"] == "failed"
        assert store.get_strategy_draft(strategy["strategyId"], user_id="local-user")["status"] == "validating"
        denied, _ = tools.call(tool="get_strategy_draft", arguments={"strategyId": strategy["strategyId"]},
            turn_id="not-owner", mode="local", user_id="another-user")
        assert denied["success"] is False
        assert "-0.244" not in json.dumps(denied)
        assert store.connection.execute("SELECT COUNT(*) FROM tqkq_sim_order_links").fetchone()[0] == 0
    finally:
        store.close()


def test_saved_strategy_is_read_back_by_owner_only(tmp_path: Path) -> None:
    store, tools, session_id = strategy_tools(tmp_path)
    event = legacy_draft(tools, session_id, {
            "name": "沪金组合草案",
            "symbol": "SHFE.au2612",
            "template": "combined",
            "rationale": "均线、突破和动量共同确认后才允许形成信号。",
        })
    assert event is not None
    strategy_id = event[1]["strategyId"]

    readback, _ = tools.call(
        tool="get_strategy_draft",
        arguments={"strategyId": strategy_id},
        turn_id="turn-read",
        mode="local",
        session_id=session_id,
        user_id="local-user",
    )
    denied, _ = tools.call(
        tool="get_strategy_draft",
        arguments={"strategyId": strategy_id},
        turn_id="turn-denied",
        mode="local",
        session_id=session_id,
        user_id="another-user",
    )

    assert readback["success"] is True
    assert denied["success"] is False
    store.close()


def test_saved_strategy_uses_completed_bars_for_read_only_signal_preview(tmp_path: Path) -> None:
    store, tools, session_id = strategy_tools(tmp_path)
    created = legacy_draft(tools, session_id, {
            "name": "沪金突破草案",
            "symbol": "SHFE.au2612",
            "template": "breakout",
            "breakoutPeriod": 20,
            "minimumVolumeRatio": 1,
            "rationale": "只在已完成K线突破近20根区间时形成信号。",
        })
    assert created is not None

    result, evaluated = tools.call(
        tool="evaluate_strategy_draft",
        arguments={"strategyId": created[1]["strategyId"]},
        turn_id="turn-evaluate",
        mode="local",
        session_id=session_id,
        user_id="local-user",
    )

    assert result["success"] is True
    assert evaluated is not None and evaluated[0] == "card.strategy"
    assert evaluated[1]["signal"] in {"buy", "sell", "wait"}
    assert evaluated[1]["summary"].endswith("不会自动下单。")
    assert evaluated[1]["evaluatedBarTime"]
    assert store.connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert store.connection.execute("SELECT COUNT(*) FROM tqkq_sim_order_links").fetchone()[0] == 0
    store.close()


def test_validation_plan_cannot_be_mistaken_for_passed_backtest(tmp_path: Path) -> None:
    store, tools, session_id = strategy_tools(tmp_path)
    created = legacy_draft(tools, session_id, {
            "name": "沪金验证草案",
            "symbol": "SHFE.au2612",
            "template": "macd_cross",
            "rationale": "测试策略验证生命周期。",
        })
    assert created is not None
    strategy_id = created[1]["strategyId"]

    result, validation = tools.call(
        tool="prepare_strategy_validation",
        arguments={"strategyId": strategy_id},
        turn_id="turn-validate",
        mode="local",
        session_id=session_id,
        user_id="local-user",
    )

    assert result["success"] is True
    assert validation is not None and validation[0] == "card.strategy"
    assert validation[1]["status"] == "validating"
    assert validation[1]["validationStatus"] == "incomplete"
    report = store.get_strategy_validation_report(
        validation[1]["validationReportId"], user_id="local-user"
    )
    assert report["status"] == "incomplete"
    assert {item["status"] for item in report["checks"]} == {"passed", "pending"}
    assert store.get_strategy_draft(strategy_id, user_id="local-user")["status"] == "validating"
    assert store.connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert store.connection.execute("SELECT COUNT(*) FROM ai_execution_authorizations").fetchone()[0] == 0

    proposals = tools.proposals
    assert proposals is not None
    with pytest.raises(ProposalConflict, match="strategy_validation_not_passed"):
        proposals.transition_strategy_status(
            strategy_id=strategy_id,
            user_id="local-user",
            target="ready",
            validation_report_id=report["reportId"],
        )
    with pytest.raises(ProposalConflict, match="strategy_transition_invalid"):
        proposals.transition_strategy_status(
            strategy_id=strategy_id,
            user_id="local-user",
            target="monitoring",
        )
    store.close()


def test_preparing_validation_twice_reuses_incomplete_report(tmp_path: Path) -> None:
    store, tools, session_id = strategy_tools(tmp_path)
    created = legacy_draft(tools, session_id, {
            "name": "幂等验证草案",
            "symbol": "SHFE.au2612",
            "template": "ma_cross",
            "rationale": "重复请求不重复制造验证报告。",
        })
    assert created is not None
    strategy_id = created[1]["strategyId"]

    events = []
    for index in range(2):
        _, event = tools.call(
            tool="prepare_strategy_validation",
            arguments={"strategyId": strategy_id},
            turn_id=f"turn-{index}",
            mode="local",
            session_id=session_id,
            user_id="local-user",
        )
        assert event is not None
        events.append(event[1])

    assert events[0]["validationReportId"] == events[1]["validationReportId"]
    assert store.connection.execute(
        "SELECT COUNT(*) FROM strategy_validation_reports"
    ).fetchone()[0] == 1
    store.close()


def test_passed_report_only_moves_strategy_to_ready_without_authorization(tmp_path: Path) -> None:
    store, tools, session_id = strategy_tools(tmp_path)
    created = legacy_draft(tools, session_id, {
            "name": "完成验证但不启动",
            "symbol": "SHFE.au2612",
            "template": "ma_cross",
            "rationale": "验证通过与启动必须分离。",
        })
    assert created is not None
    strategy_id = created[1]["strategyId"]
    prepared = tools.proposals.prepare_strategy_validation(
        strategy_id=strategy_id, user_id="local-user", turn_id="turn-prepare"
    )
    with pytest.raises(ProposalConflict, match="strategy_validation_result_invalid"):
        tools.proposals.complete_strategy_validation(
            strategy_id=strategy_id,
            report_id=prepared["validationReportId"],
            user_id="local-user",
            turn_id="turn-forged",
            result={
                "status": "passed",
                "checks": [{
                    "id": "definition_integrity", "label": "策略定义完整",
                    "status": "passed", "detail": "缺少其他证据",
                }],
                "evidence": {"liveTrading": "disabled"},
            },
            source_backtest_id="forged",
        )
    checks = [
        {"id": check_id, "label": check_id, "status": "passed", "detail": "证据完整"}
        for check_id in (
            "definition_integrity", "completed_bars_only", "lookahead_bias",
            "out_of_sample", "trading_costs", "risk_protections",
            "minimum_signals", "official_history",
        )
    ]

    result = tools.proposals.complete_strategy_validation(
        strategy_id=strategy_id,
        report_id=prepared["validationReportId"],
        user_id="local-user",
        turn_id="turn-complete",
        result={
            "status": "passed",
            "checks": checks,
            "evidence": {
                "engineDigest": "c" * 64,
                "dataDigest": "d" * 64,
                "dataSource": "tqsdk:SHFE.au2612:test",
                "liveTrading": "disabled",
            },
        },
        source_backtest_id="backtest-history-only",
    )

    assert result["status"] == "ready"
    assert result["validationStatus"] == "passed"
    assert store.get_strategy_draft(strategy_id, user_id="local-user")["status"] == "ready"
    assert store.connection.execute(
        "SELECT COUNT(*) FROM ai_execution_authorizations"
    ).fetchone()[0] == 0
    assert store.connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    store.close()


def test_strategy_validation_tool_replays_bound_draft_and_stops_at_ready(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "tool-validation.sqlite3")
    broker = EventBroker(store)
    provider = StrategyHistoryProvider()
    repository = Path(__file__).resolve().parents[3]
    releases = StrategyReleaseManager(
        engineering_root=repository,
        state_root=tmp_path / "release-state",
        store=store,
    )
    backtests = BacktestService(
        store=store,
        releases=releases,
        engineering_root=repository,
        state_root=tmp_path / "backtest-state",
    )
    proposals = ProposalService(
        store, broker, data_provider=provider
    )
    session = broker.create_session("local", "native", "local-user")
    tools = TraderReadOnlyTools(
        provider,
        lambda mode: {"mode": mode, **provider.runtime_status()},
        backtests=backtests,
        proposals=proposals,
    )
    created = legacy_draft(tools, session.session_id, {
            "name": "正式历史验证",
            "symbol": "SHFE.au2612",
            "template": "ma_cross",
            "timeframeSeconds": 300,
            "fastPeriod": 3,
            "slowPeriod": 8,
            "minimumVolumeRatio": 0.5,
            "maxOrdersPerDay": 20,
            "stopLossTicks": 20,
            "takeProfitTicks": 20,
            "rationale": "验证工具必须回放当前草案指纹。",
        })
    assert created is not None

    response, event = tools.call(
        tool="run_strategy_validation",
        arguments={
            "strategyId": created[1]["strategyId"],
            "startDate": "2026-01-01",
            "splitDate": "2026-01-02",
            "endDate": "2026-01-03",
            "slippageTicks": 0.1,
            "commissionTicksPerSide": 0.1,
        },
        turn_id="turn-validate",
        mode="local",
        session_id=session.session_id,
        user_id="local-user",
        task_id="task-strategy-validation",
    )

    assert response["success"] is True
    assert event is not None and event[0] == "card.strategy"
    assert event[1]["status"] == "ready"
    assert event[1]["validationStatus"] == "passed"
    report = store.get_strategy_validation_report(
        event[1]["validationReportId"], user_id="local-user"
    )
    assert report["evidence"]["sourceBacktestRole"] == "immutable_history_artifact_only"
    assert report["evidence"]["strategyDigest"] == created[1]["definitionDigest"]
    assert store.connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM ai_execution_authorizations"
    ).fetchone()[0] == 0
    store.close()


def test_ready_strategy_creates_idempotent_read_only_intent_snapshot(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "intent-preview.sqlite3")
    broker = EventBroker(store)
    provider = IntentPreviewProvider()
    repository = Path(__file__).resolve().parents[3]
    releases = StrategyReleaseManager(
        engineering_root=repository,
        state_root=tmp_path / "intent-release-state",
        store=store,
    )
    backtests = BacktestService(
        store=store,
        releases=releases,
        engineering_root=repository,
        state_root=tmp_path / "intent-backtest-state",
    )
    proposals = ProposalService(
        store, broker, data_provider=provider
    )
    session = broker.create_session("local", "native", "local-user")
    tools = TraderReadOnlyTools(
        provider,
        lambda mode: {"mode": mode, **provider.runtime_status()},
        backtests=backtests,
        proposals=proposals,
    )
    created = legacy_draft(tools, session.session_id, {
            "name": "只读意图预览",
            "symbol": "SHFE.rb2610",
            "template": "ma_cross",
            "timeframeSeconds": 300,
            "fastPeriod": 3,
            "slowPeriod": 8,
            "minimumVolumeRatio": 0.5,
            "maxOrdersPerDay": 20,
            "stopLossTicks": 20,
            "takeProfitTicks": 40,
            "rationale": "验证后只生成信号、仓位和风险意图。",
        })
    assert created is not None
    validated, _ = tools.call(
        tool="run_strategy_validation",
        arguments={
            "strategyId": created[1]["strategyId"],
            "startDate": "2026-01-01",
            "splitDate": "2026-01-02",
            "endDate": "2026-01-03",
            "slippageTicks": 0.1,
            "commissionTicksPerSide": 0.1,
        },
        turn_id="turn-validate",
        mode="local",
        session_id=session.session_id,
        user_id="local-user",
        task_id="task-intent-validation",
    )
    assert validated["success"] is True

    events = []
    for index in range(2):
        response, event = tools.call(
            tool="preview_strategy_intent",
            arguments={"strategyId": created[1]["strategyId"]},
            turn_id=f"turn-intent-{index}",
            mode="local",
            session_id=session.session_id,
            user_id="local-user",
        )
        assert response["success"] is True
        assert event is not None and event[0] == "card.strategy"
        events.append(event[1])

    assert events[0]["intentId"] == events[1]["intentId"]
    assert events[0]["signalSnapshot"]["signal"] == "buy"
    assert events[0]["positionIntent"]["action"] == "open_long"
    assert events[0]["riskAdjustedIntent"]["status"] == "allowed"
    assert events[0]["riskAdjustedIntent"]["executionEligible"] is False
    assert store.connection.execute(
        "SELECT COUNT(*) FROM strategy_intent_snapshots"
    ).fetchone()[0] == 1
    assert store.connection.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM ai_execution_authorizations"
    ).fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM tqkq_sim_order_links"
    ).fetchone()[0] == 0
    store.close()
