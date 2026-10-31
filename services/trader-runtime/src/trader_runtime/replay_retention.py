"""Conservative retention of confirmed-closed, disposable replay directories."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import fcntl
except ImportError:  # The cloud Runtime is POSIX; unsupported platforms retain everything.
    fcntl = None  # type: ignore[assignment]

CLOSED_NAME = "closed-replay.json"
SAVED_TABLES = (
    "daily_reviews", "review_observation_verdicts", "trade_notes", "user_memories", "user_memory_changes",
    "strategy_drafts", "strategy_releases", "strategy_labs", "strategy_backtest_results", "backtest_runs",
    "artifacts", "chart_drawings", "user_routines", "routine_drafts",
)
TEMPORARY_FILES = frozenset({CLOSED_NAME, "runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm",
                             "exchange-fees.json", "research_flash_aliases.json", "ui-workspace/identity"})
EMPTY_DIRECTORIES = frozenset({"backtests", "backtests/candidates", "strategy-runtime",
                               "strategy-runtime/releases", "ui-workspace"})


def open_replay_directory(root: Path, *, exclusive: bool = False) -> int | None:
    """Hold the directory inode while a child uses it, or exclusively while deleting it."""
    if fcntl is None or (exclusive and not shutil.rmtree.avoids_symlink_attacks):
        if exclusive:
            raise OSError("replay_cleanup_unsupported")
        return None
    if root.parent.is_symlink() or root.parent.resolve() != root.parent.absolute():
        raise OSError("unsafe_replay_root")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        if not os.path.samestat(os.fstat(descriptor), root.stat(follow_symlinks=False)):
            raise OSError("replay_directory_changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def close_replay_directory(descriptor: int | None) -> None:
    if descriptor is not None:
        os.close(descriptor)


def closed_record(root: Path, identifier: str, *, now: datetime) -> None:
    """A new proof starts the clock now; a retry never resets an earlier proof."""
    target = root / CLOSED_NAME
    if target.exists() or target.is_symlink():
        return
    temporary = root / f".{CLOSED_NAME}.{os.getpid()}.tmp"
    try:
        with temporary.open("x") as stream:
            json.dump({"version": 1, "replayId": identifier, "closedAt": now.isoformat()}, stream)
        try:
            os.link(temporary, target)  # Atomic create: another close cannot reset the retention clock.
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def disposable_reason(root: Path, identifier: str, *, now: datetime, known_tables: set[str]) -> str:
    """All uncertainty preserves data. This check runs again under the deletion lock."""
    if (root / "review.json").exists() or (root / "review.json").is_symlink():
        return "review"
    if (root / "replay.json").exists() or (root / "replay.json").is_symlink():
        return "recoverable"
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            return "unknown_files"
        if path.is_dir() and relative in EMPTY_DIRECTORIES:
            continue
        if not path.is_file() or relative not in TEMPORARY_FILES or path.stat().st_nlink != 1:
            return "unknown_files"
    proof = json.loads((root / CLOSED_NAME).read_text())
    if (not isinstance(proof, dict) or proof.get("version") != 1 or proof.get("replayId") != identifier
            or not isinstance(proof.get("closedAt"), str)):
        return "invalid_proof"
    ended = datetime.fromisoformat(proof["closedAt"])
    if ended.tzinfo is None or ended > now:
        return "invalid_proof"
    if now - ended < timedelta(days=30):
        return "retained_30_days"
    database = root / "runtime.sqlite3"
    if not database.is_file():
        return "unknown_database"
    wal = root / "runtime.sqlite3-wal"
    if wal.exists() and wal.stat().st_size:
        return "uncheckpointed_database"
    # A clean, locked child needs no recovery. Immutable reads cannot create WAL/SHM during a preview.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            return "invalid_database"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(SAVED_TABLES).issubset(tables) or not tables.issubset(known_tables):
            return "unknown_database"
        for table in SAVED_TABLES:
            if connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                return "saved_content"
        if connection.execute("SELECT 1 FROM events WHERE kind='message' "
                              "AND json_extract(payload_json, '$.role')='user' LIMIT 1").fetchone():
            return "saved_content"
        if connection.execute("SELECT 1 FROM sessions WHERE expires_at='9999-12-31T00:00:00+00:00' LIMIT 1").fetchone():
            return "saved_content"
    return "expired"


def cleanup_replays(state_root: Path, *, known_tables: set[str], now: datetime | None = None,
                    dry_run: bool = False) -> dict[str, str]:
    now = now or datetime.now(UTC)
    directory = state_root / "replays"
    if directory.is_symlink() or not directory.is_dir():
        return {}
    report = {}
    for root in sorted(directory.iterdir()):
        identifier = root.name
        if re.fullmatch(r"[a-f0-9]{32}", identifier) is None or root.is_symlink() or not root.is_dir():
            report[identifier] = "unknown_path"
            continue
        descriptor = None
        try:
            descriptor = open_replay_directory(root, exclusive=True)
            reason = disposable_reason(root, identifier, now=now, known_tables=known_tables)
            if reason == "expired" and not dry_run:
                # Recheck references and contents immediately before removing the locked inode.
                reason = disposable_reason(root, identifier, now=now, known_tables=known_tables)
                if (reason == "expired" and descriptor is not None
                        and os.path.samestat(os.fstat(descriptor), root.stat(follow_symlinks=False))):
                    shutil.rmtree(root)
                    reason = "removed"
            report[identifier] = reason
        except (OSError, ValueError, TypeError, sqlite3.Error):
            report[identifier] = "unconfirmed_or_in_use"
        finally:
            close_replay_directory(descriptor)
    return report
