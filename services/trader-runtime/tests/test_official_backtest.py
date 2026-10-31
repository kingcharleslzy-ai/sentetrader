from __future__ import annotations

import hashlib
import json
import math
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from trader_runtime.backtest_coordinator import BacktestCoordinator
from trader_runtime.backtest_worker import MAX_DATASET_ROWS, evaluate, validate_dataset_rows
from trader_runtime.backtests import BacktestService
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.release_manager import StrategyReleaseManager
from trader_runtime.store import RuntimeStore
from trader_runtime.trader_tools import TraderReadOnlyTools

REPO_ROOT = Path(__file__).resolve().parents[3]


def service(tmp_path: Path, *, worker_path: Path | None = None):
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    releases = StrategyReleaseManager(
        engineering_root=REPO_ROOT,
        state_root=tmp_path / "state",
        store=store,
    )
    return store, BacktestService(
        store=store,
        releases=releases,
        engineering_root=REPO_ROOT,
        state_root=tmp_path / "state",
        worker_path=worker_path,
    )


def official_result() -> dict:
    bars = [
        {"datetime": 1_770_000_000_000_000_000 + index, "open": 3500 + index,
         "high": 3502 + index, "low": 3498 + index, "close": 3501 + index,
         "volume": 100 + index, "openInterest": 8000 + index}
        for index in range(30)
    ]
    digest = hashlib.sha256(json.dumps(
        bars, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "engineVersion": "tqsdk-3.10.2-tqsim-tqbacktest",
        "engineDigest": "a" * 64,
        "dataSource": "tqsdk:SHFE.rb2610:2026-01-01:2026-03-31:3600",
        "dataDigest": digest,
        "dataPointCount": len(bars),
        "contractMultiplier": 10.0,
        "priceTick": 1.0,
        "fillMode": "ACTIVE",
        "commissionModel": "tqsdk_default",
        "bars": bars,
        "baseline": {"returnPercent": 1.0, "maxDrawdownPercent": 0.8,
                     "signalCount": 4, "winRatePercent": 50.0, "tradeCount": 4,
                     "commission": 8.0, "curve": [1_000_000, 1_010_000]},
        "candidate": {"returnPercent": 1.5, "maxDrawdownPercent": 0.7,
                      "signalCount": 6, "winRatePercent": 66.67, "tradeCount": 6,
                      "commission": 12.0, "curve": [1_000_000, 1_015_000]},
        "executionEnabled": False,
        "orderCapabilities": [],
        "accountType": "TqSim",
    }


def test_official_tqsdk_backtest_reuses_existing_artifact_boundary(tmp_path: Path):
    store, backtests = service(tmp_path)
    requests: list[dict] = []
    try:
        def runner(request: dict) -> dict:
            requests.append(dict(request))
            return official_result()

        first = backtests.run_tqsdk(
            session_id="session-1", user_id="user-1", symbol="SHFE.rb2610",
            start_date="2026-01-01", end_date="2026-03-31", duration_seconds=3600,
            runner=runner,
        )
        second = backtests.run_tqsdk(
            session_id="session-1", user_id="user-1", symbol="SHFE.rb2610",
            start_date="2026-01-01", end_date="2026-03-31", duration_seconds=3600,
            runner=runner,
        )
        assert len(requests) == 2
        assert first["backtestId"] != second["backtestId"]
        assert first["artifactDigest"] == second["artifactDigest"]
        assert first["sourceKind"] == "tqsdk_history"
        assert "deploymentEligible" not in first
        assert first["dataPointCount"] == 30
        record = store.get_backtest_run(first["backtestId"], user_id="user-1")
        artifact = Path(record["artifactPath"])
        assert {path.name for path in artifact.iterdir()} == {
            "request.json", "bars.json", "candidate.json", "result.json", "manifest.json",
        }
        assert record["manifest"]["accountType"] == "TqSim"
        assert record["manifest"]["executionEnabled"] is False
        assert record["manifest"]["orderCapabilities"] == []
    finally:
        store.close()


def test_official_tqsdk_backtest_cancelled_after_save_leaves_no_completed_row(tmp_path: Path):
    """The lease's closing check can cancel after the row was saved: the caller gets
    backtest_cancelled, so no completed run may stay behind (the deterministic run() discards too)."""
    store, backtests = service(tmp_path)
    try:
        original = store.save_backtest_run

        def save_then_cancel(**kwargs):
            original(**kwargs)
            assert backtests.coordinator.cancel("cancel-me", user_id="user-1")

        store.save_backtest_run = save_then_cancel
        with pytest.raises(RuntimeError, match="backtest_cancelled"):
            backtests.run_tqsdk(
                session_id="session-1", user_id="user-1", symbol="SHFE.rb2610",
                start_date="2026-01-01", end_date="2026-03-31", duration_seconds=3600,
                runner=lambda _: official_result(), request_id="cancel-me",
            )
        with pytest.raises(KeyError):
            store.latest_backtest_run(session_id="session-1", user_id="user-1")
    finally:
        store.close()


def test_official_tqsdk_backtest_rejects_tampered_bars(tmp_path: Path):
    store, backtests = service(tmp_path)
    try:
        result = official_result()
        result["bars"][0]["close"] = 9999
        with pytest.raises(RuntimeError, match="tqsdk_backtest_data_integrity_failed"):
            backtests.run_tqsdk(
                session_id="session-1", user_id="user-1", symbol="SHFE.rb2610",
                start_date="2026-01-01", end_date="2026-03-31", duration_seconds=3600,
                runner=lambda _: result,
            )
        with pytest.raises(KeyError):
            store.latest_backtest_run(session_id="session-1", user_id="user-1")
    finally:
        store.close()


def test_official_tqsdk_backtest_is_a_research_only_agent_tool(tmp_path: Path):
    store, backtests = service(tmp_path)

    class Provider(FakeReadOnlyDataProvider):
        def historical_backtest(self, request: dict) -> dict:
            assert request["symbol"] == "SHFE.rb2610"
            return official_result()

    provider = Provider()
    tools = TraderReadOnlyTools(
        provider,
        lambda mode: {"mode": mode, **provider.runtime_status()},
        backtests=backtests,
    )
    try:
        definition = next(
            item for item in tools.definitions()
            if item["name"] == "run_tqsdk_historical_backtest"
        )
        assert "独立 TqSim" in definition["description"]
        response, card = tools.call(
            tool="run_tqsdk_historical_backtest",
            arguments={
                "symbol": "SHFE.rb2610",
                "startDate": "2026-01-01",
                "endDate": "2026-03-31",
            },
            turn_id="turn-1",
            task_id="task-backtest-1",
            session_id="session-1",
            user_id="user-1",
            mode="local",
        )
        assert response["success"] is True
        assert card is not None and card[0] == "card.backtest"
        assert card[1]["sourceKind"] == "tqsdk_history"
        assert "deploymentEligible" not in card[1]
    finally:
        store.close()
def test_backtest_is_reproducible_and_uses_immutable_inputs(tmp_path: Path):
    store, backtests = service(tmp_path)
    try:
        first = backtests.run(session_id="session-1", user_id="user-1")
        second = backtests.run(session_id="session-1", user_id="user-1")
        assert first["backtestId"] != second["backtestId"]
        assert first["artifactDigest"] == second["artifactDigest"]
        assert first["reproducible"] is True
        assert first["dataSource"].startswith("synthetic:")
        record = store.get_backtest_run(first["backtestId"], user_id="user-1")
        artifact = Path(record["artifactPath"])
        assert not artifact.stat().st_mode & 0o200
        manifest = json.loads((artifact / "manifest.json").read_text())
        assert manifest["executionEnabled"] is False
        assert manifest["orderCapabilities"] == []
        assert manifest["engineDigest"] == first["engineDigest"]
        assert manifest["candidateConfigDigest"] == first["candidateConfigDigest"]
        assert hashlib.sha256((artifact / "dataset.csv").read_bytes()).hexdigest() == first["dataDigest"]
        assert hashlib.sha256((artifact / "result.json").read_bytes()).hexdigest() == manifest["resultDigest"]
        assert hashlib.sha256((artifact / "candidate.json").read_bytes()).hexdigest() == first["candidateConfigDigest"]
        candidate = json.loads(
            (backtests.candidates_root / first["candidateVariantId"] / "manifest.json").read_text()
        )
        assert candidate["backtestArtifactDigest"] == first["artifactDigest"]
        assert candidate["candidateParameters"] == {
            "baselineThreshold": "1.00", "candidateThreshold": "0.90",
        }
    finally:
        store.close()


def test_backtest_records_are_user_isolated(tmp_path: Path):
    store, backtests = service(tmp_path)
    try:
        result = backtests.run(session_id="session-1", user_id="user-1")
        assert store.get_backtest_run(result["backtestId"], user_id="user-1")["status"] == "completed"
        try:
            store.get_backtest_run(result["backtestId"], user_id="user-2")
            raise AssertionError("cross-user backtest access unexpectedly succeeded")
        except KeyError:
            pass
    finally:
        store.close()


def test_backtest_card_labels_match_non_default_inputs(tmp_path: Path):
    engineering = tmp_path / "engineering"
    shutil.copytree(REPO_ROOT / "workspace", engineering / "workspace")
    custom_data = engineering / "workspace/backtests/fixtures/custom_window.csv"
    custom_data.write_bytes(
        (engineering / "workspace/backtests/fixtures/rb_monthly_v1.csv").read_bytes()
    )
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    releases = StrategyReleaseManager(
        engineering_root=engineering, state_root=tmp_path / "state", store=store,
    )
    backtests = BacktestService(
        store=store, releases=releases, engineering_root=engineering,
        state_root=tmp_path / "state",
    )
    try:
        result = backtests.run(
            session_id="session-custom", user_id="user-custom",
            data_path="workspace/backtests/fixtures/custom_window.csv",
            baseline_threshold="0.80", candidate_threshold="0.725",
        )
        assert result["oldVersion"] == "阈值 80%"
        assert result["newVersion"] == "阈值 72.5%"
        assert result["period"] == "数据集 · custom_window.csv"
        record = store.get_backtest_run(result["backtestId"], user_id="user-custom")
        assert record["manifest"]["dataSource"].endswith("custom_window.csv")
        assert record["manifest"]["parameters"] == {
            "baselineThreshold": "0.80", "candidateThreshold": "0.725",
        }
    finally:
        store.close()


def test_existing_release_manifest_is_upgraded_before_backtest(tmp_path: Path):
    store, backtests = service(tmp_path)
    try:
        release = backtests.releases.ensure_release(
            strategy_path="workspace/strategies/rb_trend.py"
        )
        release_dir = backtests.releases.releases_root / release["releaseId"]
        manifest_path = release_dir / "manifest.json"
        release_dir.chmod(0o755)
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("artifactName")
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.chmod(0o444)
        release_dir.chmod(0o555)

        migrated = StrategyReleaseManager(
            engineering_root=REPO_ROOT,
            state_root=tmp_path / "state",
            store=store,
        )
        assert migrated.release_artifact_path(release["releaseId"]).name == "rb_trend.py"
        assert json.loads(manifest_path.read_text())["artifactName"] == "rb_trend.py"
    finally:
        store.close()


def test_concurrent_identical_backtests_share_one_complete_artifact(tmp_path: Path):
    store, backtests = service(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda index: backtests.run(
                    session_id=f"session-{index}", user_id=f"user-{index}"
                ),
                range(2),
            ))
        assert results[0]["artifactDigest"] == results[1]["artifactDigest"]
        artifact_dirs = list(backtests.root.glob("sha256-*"))
        assert len(artifact_dirs) == 1
        assert (artifact_dirs[0] / "manifest.json").is_file()
        assert (artifact_dirs[0] / "result.json").is_file()
    finally:
        store.close()


def test_worker_code_digest_changes_artifact_identity(tmp_path: Path):
    worker = tmp_path / "backtest_worker.py"
    worker.write_bytes((REPO_ROOT / "services/trader-runtime/src/trader_runtime/backtest_worker.py").read_bytes())
    store, first_service = service(tmp_path, worker_path=worker)
    try:
        first = first_service.run(session_id="session-1", user_id="user-1")
        worker.write_text(worker.read_text() + "\n# provenance change\n")
        second_service = BacktestService(
            store=store, releases=first_service.releases,
            engineering_root=REPO_ROOT, state_root=tmp_path / "state",
            worker_path=worker,
        )
        second = second_service.run(session_id="session-2", user_id="user-1")
        assert first["engineDigest"] != second["engineDigest"]
        assert first["artifactDigest"] != second["artifactDigest"]
    finally:
        store.close()


def test_dataset_is_read_once_and_immutable_copy_matches_digest(
    tmp_path: Path, monkeypatch,
):
    engineering = tmp_path / "engineering"
    shutil.copytree(REPO_ROOT / "workspace", engineering / "workspace")
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    releases = StrategyReleaseManager(
        engineering_root=engineering, state_root=tmp_path / "state", store=store,
    )
    backtests = BacktestService(
        store=store, releases=releases, engineering_root=engineering,
        state_root=tmp_path / "state",
    )
    source_dataset = engineering / "workspace/backtests/fixtures/rb_monthly_v1.csv"
    original_execute = backtests._execute

    def mutate_source_after_snapshot(**kwargs):
        source_dataset.write_text("concurrently modified\n")
        return original_execute(**kwargs)

    monkeypatch.setattr(backtests, "_execute", mutate_source_after_snapshot)
    try:
        result = backtests.run(session_id="session-1", user_id="user-1")
        record = store.get_backtest_run(result["backtestId"], user_id="user-1")
        artifact = Path(record["artifactPath"])
        assert hashlib.sha256((artifact / "dataset.csv").read_bytes()).hexdigest() == result["dataDigest"]
        assert hashlib.sha256(source_dataset.read_bytes()).hexdigest() != result["dataDigest"]
    finally:
        store.close()


def test_reused_artifact_verifies_result_and_dataset_bytes(tmp_path: Path):
    store, backtests = service(tmp_path)
    try:
        result = backtests.run(session_id="session-1", user_id="user-1")
        record = store.get_backtest_run(result["backtestId"], user_id="user-1")
        artifact = Path(record["artifactPath"])
        artifact.chmod(0o755)
        result_path = artifact / "result.json"
        result_path.chmod(0o644)
        result_path.write_text("{}\n")
        with pytest.raises(RuntimeError, match="backtest_artifact_integrity_failed"):
            backtests.run(session_id="session-2", user_id="user-1")
    finally:
        store.close()


def test_reused_artifact_rejects_missing_immutable_worker(tmp_path: Path):
    store, backtests = service(tmp_path)
    try:
        result = backtests.run(session_id="session-1", user_id="user-1")
        artifact = Path(store.get_backtest_run(
            result["backtestId"], user_id="user-1"
        )["artifactPath"])
        artifact.chmod(0o755)
        worker = artifact / "worker.py"
        worker.chmod(0o644)
        worker.unlink()
        with pytest.raises(RuntimeError, match="backtest_artifact_integrity_failed"):
            backtests.run(session_id="session-2", user_id="user-1")
    finally:
        store.close()


class _AlwaysReadyStrategy:
    class TrendInputs:
        def __init__(self, *_args):
            pass

    class TrendParameters:
        def __init__(self, *, volume_threshold):
            self.volume_threshold = volume_threshold

    @staticmethod
    def signal_ready(_inputs, _parameters):
        return True


def _worker_row(period_return: str) -> dict[str, str]:
    return {
        "priceAboveShortAverage": "1",
        "mediumTrendAligned": "1",
        "volumeRatio": "1",
        "returnPercent": period_return,
    }


def test_worker_rejects_non_finite_compounded_equity():
    with pytest.raises(RuntimeError, match="backtest_non_finite_equity"):
        evaluate(
            _AlwaysReadyStrategy,
            [_worker_row("1e308"), _worker_row("1e308")],
            Decimal("0.9"),
        )


def test_worker_handles_total_loss_but_rejects_negative_equity():
    result = evaluate(
        _AlwaysReadyStrategy,
        [_worker_row("-100"), _worker_row("20")],
        Decimal("0.9"),
    )
    assert result["returnPercent"] == -100
    assert all(math.isfinite(value) and value >= 0 for value in result["curve"])
    with pytest.raises(RuntimeError, match="dataset_return_below_minus_100"):
        evaluate(_AlwaysReadyStrategy, [_worker_row("-100.01")], Decimal("0.9"))


def test_worker_rejects_non_finite_returns_and_oversized_dataset():
    for value in ("nan", "inf", "-inf"):
        with pytest.raises(RuntimeError, match="dataset_non_finite_return"):
            evaluate(_AlwaysReadyStrategy, [_worker_row(value)], Decimal("0.9"))
    with pytest.raises(RuntimeError, match="backtest_dataset_too_large"):
        validate_dataset_rows([{}] * (MAX_DATASET_ROWS + 1))


def test_backtest_queue_cancellation_timeout_and_cleanup():
    coordinator = BacktestCoordinator(
        global_limit=1, per_user_limit=1,
        global_queue_limit=2, per_user_queue_limit=2,
        timeout_seconds=5,
    )
    entered = threading.Event()
    release = threading.Event()
    queued_error: list[str] = []

    def occupy() -> None:
        with coordinator.lease(user_id="user-1", request_id="request-running"):
            entered.set()
            release.wait(2)

    def queue() -> None:
        try:
            with coordinator.lease(user_id="user-1", request_id="request-queued"):
                raise AssertionError("cancelled queue unexpectedly acquired a slot")
        except RuntimeError as error:
            queued_error.append(str(error))

    first = threading.Thread(target=occupy)
    second = threading.Thread(target=queue)
    first.start()
    assert entered.wait(1)
    second.start()
    for _ in range(100):
        if coordinator.snapshot()["jobs"] == 2:
            break
        time.sleep(0.01)
    assert coordinator.cancel("request-queued", user_id="user-1") is True
    second.join(2)
    release.set()
    first.join(2)
    assert queued_error == ["backtest_cancelled"]
    assert coordinator.snapshot() == {"jobs": 0, "users": 0, "queuedUsers": 0}

    coordinator.timeout_seconds = 0.05
    with pytest.raises(RuntimeError, match="backtest_timeout"):
        with coordinator.lease(user_id="user-2", request_id="request-timeout") as lease:
            lease.deadline = time.monotonic() - 1
            lease.check()
    assert coordinator.snapshot() == {"jobs": 0, "users": 0, "queuedUsers": 0}


def test_one_users_queue_does_not_consume_another_users_global_slot():
    coordinator = BacktestCoordinator(
        global_limit=2, per_user_limit=1,
        global_queue_limit=4, per_user_queue_limit=3,
        timeout_seconds=5,
    )
    first_entered = threading.Event()
    second_entered = threading.Event()
    other_entered = threading.Event()
    release_first = threading.Event()
    release_other = threading.Event()
    second_errors: list[str] = []

    def first_job() -> None:
        with coordinator.lease(user_id="tenant-a", request_id="tenant-a-first"):
            first_entered.set()
            release_first.wait(2)

    def second_job() -> None:
        try:
            with coordinator.lease(user_id="tenant-a", request_id="tenant-a-second"):
                second_entered.set()
        except RuntimeError as error:
            second_errors.append(str(error))

    def other_job() -> None:
        with coordinator.lease(user_id="tenant-b", request_id="tenant-b-first"):
            other_entered.set()
            release_other.wait(2)

    first = threading.Thread(target=first_job)
    second = threading.Thread(target=second_job)
    other = threading.Thread(target=other_job)
    first.start()
    assert first_entered.wait(1)
    second.start()
    for _ in range(100):
        if coordinator.snapshot()["jobs"] == 2:
            break
        time.sleep(0.01)
    time.sleep(0.1)
    other.start()
    assert other_entered.wait(0.5)
    assert not second_entered.is_set()
    assert coordinator.cancel("tenant-a-second", user_id="tenant-a") is True
    release_other.set()
    release_first.set()
    second.join(2)
    other.join(2)
    first.join(2)
    assert second_errors == ["backtest_cancelled"]
    assert coordinator.snapshot() == {"jobs": 0, "users": 0, "queuedUsers": 0}


def test_backtest_timeout_kills_worker_and_cleans_coordinator(tmp_path: Path):
    marker = tmp_path / "worker-finished"
    worker = tmp_path / "slow_worker.py"
    worker.write_text(
        "import pathlib, sys, time\n"
        "sys.stdin.readline()\n"
        "time.sleep(0.5)\n"
        f"pathlib.Path({str(marker)!r}).write_text('unexpected')\n"
    )
    coordinator = BacktestCoordinator(timeout_seconds=1)
    coordinator.timeout_seconds = 0.05
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    releases = StrategyReleaseManager(
        engineering_root=REPO_ROOT, state_root=tmp_path / "state", store=store,
    )
    backtests = BacktestService(
        store=store, releases=releases, engineering_root=REPO_ROOT,
        state_root=tmp_path / "state", coordinator=coordinator, worker_path=worker,
    )
    try:
        with pytest.raises(RuntimeError, match="backtest_timeout"):
            backtests.run(session_id="session-timeout", user_id="user-timeout")
        time.sleep(0.1)
        assert not marker.exists()
        assert coordinator.snapshot() == {"jobs": 0, "users": 0, "queuedUsers": 0}
    finally:
        store.close()
