"""The store's shared connection must never mix identities, cursors or transactions."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_owner_auth import PASSWORD_HASH
from trader_runtime.auth import OwnerAuth
from trader_runtime.store import RuntimeStore


def test_concurrent_authentication_and_cursor_reads_keep_the_requested_identity(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "concurrent.sqlite3")
    auth = OwnerAuth(username="owner", password_hash=PASSWORD_HASH, session_secret="test-secret" * 4,
                     allowed_login_origins=set(), user_store=store, session_store=store)
    count = 24
    with store.lock, store.connection:
        for i in range(count):
            store.connection.execute(
                "INSERT INTO cloud_users(username,password_hash,role,created_at) VALUES (?,?,?,?)",
                (f"user-{i}", f"hash-{i}", "admin" if i % 2 else "user", "2026-10-23"))
    tokens = [auth._issue_session(f"user-{i}", f"hash-{i}") for i in range(count)]
    barrier = threading.Barrier(count)

    def read(i: int) -> None:
        barrier.wait(timeout=5)
        for _ in range(100):
            user = store.get_cloud_user(f"user-{i}")
            assert user is not None and user["username"] == f"user-{i}"
            assert user["passwordHash"] == f"hash-{i}"
            identity = auth.validate(tokens[i])
            assert identity.username == f"user-{i}"
            assert identity.role == ("admin" if i % 2 else "user")
            # Both shortcut execute and explicit cursor consumption are production SQLite APIs.
            with_cursor = store.connection.cursor()
            try:
                with_cursor.execute("SELECT username FROM cloud_users WHERE username = ?", (f"user-{i}",))
                assert [row[0] for row in with_cursor] == [f"user-{i}"]
            finally:
                with_cursor.close()
    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(read, range(count)))
    finally:
        store.close()


@pytest.mark.parametrize("commit", [False, True])
def test_unlocked_reader_waits_for_owned_transaction_and_sees_only_its_outcome(tmp_path: Path, commit: bool) -> None:
    store = RuntimeStore(tmp_path / "transaction.sqlite3")
    with store.connection:
        store.connection.execute(
            "INSERT INTO cloud_users(username,password_hash,role,created_at) VALUES ('member','old','user','today')")
    attempted, finished = threading.Event(), threading.Event()
    answer = []

    def read() -> None:
        attempted.set()
        answer.append(store.get_cloud_user("member"))
        finished.set()

    class Abort(Exception):
        pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with store.transaction() as connection:
                connection.execute("UPDATE cloud_users SET password_hash='new',role='admin' WHERE username='member'")
                future = pool.submit(read)
                assert attempted.wait(timeout=5)
                assert not finished.wait(timeout=0.05), "reader saw an uncommitted identity"
                if not commit:
                    raise Abort
        except Abort:
            pass
        future.result(timeout=5)
    assert answer[0]["passwordHash"] == ("new" if commit else "old")
    assert answer[0]["role"] == ("admin" if commit else "user")
    store.close()
    reopened = RuntimeStore(tmp_path / "transaction.sqlite3")
    try:
        assert reopened.get_cloud_user("member") == answer[0]
    finally:
        reopened.close()


def test_cursor_script_cannot_commit_an_owned_transaction_before_rollback(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "cursor-script.sqlite3")
    try:
        with pytest.raises(RuntimeError, match="commit_inside_owned_transaction"):
            with store.transaction() as connection:
                connection.execute("INSERT INTO owner_session_revocations VALUES ('must-rollback',1)")
                cursor = connection.cursor()
                try:
                    cursor.executescript("SELECT 1;")
                finally:
                    cursor.close()
        assert store.connection.execute("SELECT * FROM owner_session_revocations").fetchall() == []
        assert not store.connection.in_transaction
    finally:
        store.close()
