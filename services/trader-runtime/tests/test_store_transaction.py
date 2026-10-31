"""The transaction-ownership contract: one owner per store transaction, no
nested commits, and notifications that fire only after the commit."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from trader_runtime.store import RuntimeStore


def test_a_self_committing_write_inside_a_transaction_is_rejected_and_rolled_back(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "guard.sqlite3")
    with pytest.raises(RuntimeError, match="commit_inside_owned_transaction"):
        with store.transaction() as connection:
            connection.execute("INSERT INTO owner_session_revocations VALUES ('guard-token', 1)")
            store.upsert_account_daily("2026-09-19", {"equity": 1000.0})
    assert store.connection.execute(
        "SELECT COUNT(*) FROM owner_session_revocations WHERE token_hash = 'guard-token'"
    ).fetchone()[0] == 0
    assert store.connection.execute("SELECT COUNT(*) FROM account_daily").fetchone()[0] == 0
    store.close()


def test_a_nested_transaction_is_rejected(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "nested.sqlite3")
    with pytest.raises(RuntimeError, match="nested_transaction"):
        with store.transaction():
            with store.transaction():
                pass
    assert store.connection.owner_thread is None and not store.connection.in_transaction
    store.close()


def test_after_commit_runs_once_the_transaction_commits_and_not_when_it_rolls_back(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "after-commit.sqlite3")
    ran: list[str] = []
    with store.transaction():
        store.after_commit(lambda: ran.append("committed"))
        assert ran == []
    assert ran == ["committed"]
    with pytest.raises(ValueError, match="business failure"):
        with store.transaction():
            store.after_commit(lambda: ran.append("dropped"))
            raise ValueError("business failure")
    assert ran == ["committed"]
    store.close()


def test_after_commit_outside_a_transaction_runs_immediately(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "immediate.sqlite3")
    ran: list[str] = []
    store.after_commit(lambda: ran.append("immediate"))
    assert ran == ["immediate"]
    store.close()


def test_after_commit_from_another_thread_does_not_wait_for_the_owner(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "cross-thread.sqlite3")
    ran: list[str] = []
    done = threading.Event()

    def notify_from_outside():
        store.after_commit(lambda: ran.append(threading.current_thread().name))
        done.set()

    with store.transaction():
        other = threading.Thread(target=notify_from_outside, name="notifier")
        other.start()
        assert done.wait(timeout=5)
        assert ran == ["notifier"]  # ran in its own thread, not parked on our commit
    other.join()
    assert ran == ["notifier"]
    store.close()


def test_a_failing_after_commit_callback_does_not_fail_the_committed_operation(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "callback-failure.sqlite3")
    ran: list[str] = []

    def broken():
        raise ValueError("notification transport down")

    with store.transaction() as connection:
        connection.execute("INSERT INTO owner_session_revocations VALUES ('kept-token', 1)")
        store.after_commit(broken)
        store.after_commit(lambda: ran.append("second"))
    assert ran == ["second"]
    assert store.connection.execute(
        "SELECT COUNT(*) FROM owner_session_revocations WHERE token_hash = 'kept-token'"
    ).fetchone()[0] == 1
    store.close()
