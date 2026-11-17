"""The release backup must survive WAL (外审 09-19 A1).

`store/base.py` opens the database with `PRAGMA journal_mode = WAL`, so committed transactions live in
the `-wal` file until a checkpoint. `deploy/vps/deploy-release.sh` used to copy the main file alone,
which silently produced a backup that passed `integrity_check` and was missing rows. The first test
pins that failure mode; the second runs the verifier the deploy script actually ships.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = REPO_ROOT / "deploy" / "vps" / "deploy-release.sh"


def _live_wal_database(path: Path) -> sqlite3.Connection:
    """A database whose last committed row is still only in the -wal, as a running Runtime leaves it."""
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE events (id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE proposals (id TEXT PRIMARY KEY)")
    # Fold the schema into the main file first, so the row below is the only thing the -wal holds.
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO proposals VALUES ('proposal-1')")
    assert connection.execute("SELECT count(*) FROM proposals").fetchone()[0] == 1
    return connection


def test_copying_the_main_file_loses_committed_rows_that_the_backup_api_keeps(tmp_path: Path) -> None:
    source = tmp_path / "ai-trader.sqlite3"
    writer = _live_wal_database(source)

    naive = tmp_path / "cp" / "before.sqlite3"
    naive.parent.mkdir()
    shutil.copy2(source, naive)
    copied = sqlite3.connect(naive)
    # The defect in full: a backup that looks healthy and has lost the trade anyway.
    assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copied.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0
    copied.close()

    consistent = tmp_path / "backup" / "before.sqlite3"
    consistent.parent.mkdir()
    destination = sqlite3.connect(consistent)
    writer.backup(destination)
    assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert destination.execute("SELECT count(*) FROM proposals").fetchone()[0] == 1
    destination.close()
    writer.close()


def _shipped_verifier(tmp_path: Path) -> Path:
    """The backup+verify program embedded in deploy-release.sh, extracted verbatim."""
    lines = DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.endswith("<<'PY'"))
    end = next(index for index, line in enumerate(lines) if index > start and line == "PY")
    program = tmp_path / "backup_check.py"
    program.write_text("\n".join(lines[start + 1:end]) + "\n", encoding="utf-8")
    return program


def test_the_deploy_script_backs_up_and_verifies_a_live_wal_database(tmp_path: Path) -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    # A plain copy of the database file is what the audit reproduced; it must not come back.
    assert "cp -p /var/lib/docker" not in text
    assert "BACKUP_NOT_CONSISTENT" in text

    source = tmp_path / "ai-trader.sqlite3"
    writer = _live_wal_database(source)
    backup = tmp_path / "backups" / "before-abc1234.sqlite3"
    backup.parent.mkdir()
    result = subprocess.run(
        [sys.executable, str(_shipped_verifier(tmp_path)), str(source), str(backup)],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "backup verified: events=0, proposals=1, sessions=0" in result.stdout
    writer.close()

    restored = sqlite3.connect(backup)
    assert restored.execute("SELECT count(*) FROM proposals").fetchone()[0] == 1
    restored.close()


def test_backup_rejects_missing_empty_wrong_and_incomplete_sources(tmp_path: Path) -> None:
    checker = _shipped_verifier(tmp_path)
    for case in ("missing", "empty", "unrelated", "missing_core", "corrupt"):
        source = tmp_path / f"{case}.sqlite3"
        backup = tmp_path / f"{case}-backup.sqlite3"
        if case == "empty":
            source.touch()
        elif case == "corrupt":
            source.write_bytes(b"not a sqlite database")
        elif case in ("unrelated", "missing_core"):
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE sessions (id TEXT)" if case == "missing_core"
                                   else "CREATE TABLE unrelated (id TEXT)")
        before = source.read_bytes() if source.exists() else None
        result = subprocess.run([sys.executable, str(checker), str(source), str(backup)],
                                capture_output=True, text=True, check=False)
        assert result.returncode != 0, (case, result.stdout, result.stderr)
        assert "backup verified:" not in result.stdout
        assert not backup.exists(), case
        assert (source.read_bytes() if source.exists() else None) == before


def test_backup_accepts_initialized_database_with_no_business_rows(tmp_path: Path) -> None:
    source = tmp_path / "empty-runtime #1.sqlite3"
    writer = _live_wal_database(source)
    writer.execute("DELETE FROM proposals")
    backup = tmp_path / "empty-runtime-backup.sqlite3"
    result = subprocess.run([sys.executable, str(_shipped_verifier(tmp_path)), str(source), str(backup)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "events=0, proposals=0, sessions=0" in result.stdout
    writer.close()
    restored = sqlite3.connect(backup)
    assert restored.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0
    restored.close()

