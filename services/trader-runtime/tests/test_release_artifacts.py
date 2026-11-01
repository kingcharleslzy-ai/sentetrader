from __future__ import annotations

import sqlite3
from pathlib import Path

from trader_runtime.release_manager import StrategyReleaseManager
from trader_runtime.store import RuntimeStore


def _table_exists(database: Path, name: str) -> bool:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None
    finally:
        connection.close()


def test_backtest_strategy_snapshot_is_immutable_and_outside_engineering_workspace(
    tmp_path: Path,
) -> None:
    engineering = tmp_path / "engineering"
    strategy = engineering / "workspace" / "strategies" / "rb_trend.py"
    strategy.parent.mkdir(parents=True)
    strategy.write_text("THRESHOLD = 0.90\n", encoding="utf-8")
    state_root = tmp_path / "runtime-state"
    store = RuntimeStore(state_root / "runtime.sqlite3")
    releases = StrategyReleaseManager(engineering_root=engineering, state_root=state_root, store=store)

    first = releases.ensure_release(strategy_path="workspace/strategies/rb_trend.py")
    first_artifact = releases.release_artifact_path(first["releaseId"])
    assert engineering not in first_artifact.parents
    assert first["executionEnabled"] is False
    assert first["orderCapabilities"] == []

    strategy.write_text("THRESHOLD = 0.85\n", encoding="utf-8")
    second = releases.ensure_release(strategy_path="workspace/strategies/rb_trend.py")
    assert second["artifactDigest"] != first["artifactDigest"]
    assert first_artifact.read_text(encoding="utf-8") == "THRESHOLD = 0.90\n"
    assert not (state_root / "strategy-runtime" / "deployments").exists()
    store.close()


def test_content_addressed_release_reuses_persisted_artifact_name(tmp_path: Path) -> None:
    engineering = tmp_path / "engineering"
    first = engineering / "workspace" / "strategies" / "first.py"
    second = engineering / "workspace" / "strategies" / "second.py"
    first.parent.mkdir(parents=True)
    first.write_text("THRESHOLD = 0.90\n", encoding="utf-8")
    second.write_bytes(first.read_bytes())
    state_root = tmp_path / "runtime-state"
    store = RuntimeStore(state_root / "runtime.sqlite3")
    releases = StrategyReleaseManager(
        engineering_root=engineering, state_root=state_root, store=store
    )
    first_release = releases.ensure_release(strategy_path="workspace/strategies/first.py")
    second_release = releases.ensure_release(strategy_path="workspace/strategies/second.py")
    assert first_release["releaseId"] == second_release["releaseId"]
    release = store.get_strategy_release(first_release["releaseId"])
    assert release["manifest"]["artifactName"] == "first.py"
    store.close()


def test_new_database_has_no_deployment_table(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    RuntimeStore(database).close()
    assert not _table_exists(database, "strategy_deployments")
    assert _table_exists(database, "strategy_releases")
    assert _table_exists(database, "backtest_runs")
