from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from trader_runtime.release_manager import StrategyReleaseManager
from trader_runtime.store import RuntimeStore

# The retired deployment table exactly as older Runtimes created it.
LEGACY_DEPLOYMENTS_TABLE = """
CREATE TABLE strategy_deployments (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE,
    release_id TEXT NOT NULL,
    backtest_id TEXT,
    status TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(release_id) REFERENCES strategy_releases(id)
);
CREATE INDEX idx_strategy_deployments_status ON strategy_deployments(status, created_at);
CREATE UNIQUE INDEX idx_strategy_deployments_backtest
    ON strategy_deployments(backtest_id) WHERE backtest_id IS NOT NULL;
"""


def _table_exists(database: Path, name: str) -> bool:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None
    finally:
        connection.close()


def _legacy_database(database: Path, *, deployment_rows: int) -> None:
    RuntimeStore(database).close()
    connection = sqlite3.connect(database)
    try:
        connection.executescript(LEGACY_DEPLOYMENTS_TABLE)
        if deployment_rows:
            connection.execute(
                "INSERT INTO strategy_releases VALUES (?, ?, ?, ?, ?)",
                ("sha256-" + "a" * 64, "a" * 64, "workspace/strategies/rb_trend.py", "{}",
                 "2026-08-26T00:00:00+00:00"),
            )
        for index in range(deployment_rows):
            connection.execute(
                """INSERT INTO strategy_deployments
                (id, proposal_id, release_id, status, manifest_json, created_at, updated_at)
                VALUES (?, ?, ?, 'active', '{}', ?, ?)""",
                (f"deployment-{index}", f"proposal-{index}", "sha256-" + "a" * 64,
                 "2026-08-26T00:00:00+00:00", "2026-08-26T00:00:00+00:00"),
            )
        connection.commit()
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


def test_short_release_ids_upgrade_without_deleting_persisted_artifacts(tmp_path: Path) -> None:
    state_root = tmp_path / "runtime-state"
    database = state_root / "runtime.sqlite3"
    content = b"legacy"
    digest = hashlib.sha256(content).hexdigest()
    legacy_id = f"sha256-{digest[:16]}"
    canonical_id = f"sha256-{digest}"
    store = RuntimeStore(database)
    with store.lock, store.connection:
        store.connection.execute(
            "INSERT INTO strategy_releases VALUES (?, ?, ?, ?, ?)",
            (legacy_id, digest, "workspace/strategies/rb_trend.py",
             json.dumps({"releaseId": legacy_id, "artifactDigest": digest}), "2026-08-26T00:00:00+00:00"),
        )
    legacy_dir = state_root / "strategy-runtime" / "releases" / legacy_id
    legacy_dir.mkdir(parents=True)
    artifact = legacy_dir / "rb_trend.py"
    artifact.write_bytes(content)
    (legacy_dir / "manifest.json").write_text(
        json.dumps({"releaseId": legacy_id, "artifactDigest": digest}), encoding="utf-8"
    )
    store.close()

    migrated_store = RuntimeStore(database)
    engineering = tmp_path / "engineering"
    engineering.mkdir()
    manager = StrategyReleaseManager(
        engineering_root=engineering, state_root=state_root, store=migrated_store
    )
    canonical_dir = manager.releases_root / canonical_id
    assert canonical_dir.is_dir()
    assert (canonical_dir / "rb_trend.py").read_bytes() == content
    assert json.loads((canonical_dir / "manifest.json").read_text(encoding="utf-8"))["releaseId"] == canonical_id
    assert migrated_store.get_strategy_release(canonical_id)["artifactDigest"] == digest
    migrated_store.close()


def test_new_database_has_no_deployment_table(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    RuntimeStore(database).close()
    assert not _table_exists(database, "strategy_deployments")
    assert _table_exists(database, "strategy_releases")
    assert _table_exists(database, "backtest_runs")


def test_empty_retired_deployment_table_is_dropped_repeatably(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    _legacy_database(database, deployment_rows=0)
    assert _table_exists(database, "strategy_deployments")
    RuntimeStore(database).close()
    assert not _table_exists(database, "strategy_deployments")
    RuntimeStore(database).close()
    assert not _table_exists(database, "strategy_deployments")


def test_non_empty_retired_deployment_table_is_kept_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    _legacy_database(database, deployment_rows=2)
    for _ in range(2):
        with caplog.at_level(logging.WARNING, logger="uvicorn.error.agent"):
            RuntimeStore(database).close()
        assert "strategy_deployments kept: 2 rows" in caplog.text
        caplog.clear()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM strategy_deployments").fetchone()[0] == 2
    finally:
        connection.close()


def test_retired_deployment_directory_is_removed_only_when_empty(tmp_path: Path) -> None:
    engineering = tmp_path / "engineering"
    engineering.mkdir()
    state_root = tmp_path / "runtime-state"
    legacy = state_root / "strategy-runtime" / "deployments"
    legacy.mkdir(parents=True)
    store = RuntimeStore(state_root / "runtime.sqlite3")
    StrategyReleaseManager(engineering_root=engineering, state_root=state_root, store=store)
    assert not legacy.exists()

    (legacy / "deployment-1").mkdir(parents=True)
    StrategyReleaseManager(engineering_root=engineering, state_root=state_root, store=store)
    assert (legacy / "deployment-1").is_dir()
    store.close()
