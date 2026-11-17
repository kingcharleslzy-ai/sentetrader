"""Failure and cross-connection regressions for administrative/store writes."""
from __future__ import annotations

import sqlite3

import pytest

from trader_runtime.store import RuntimeStore, state_changes


@pytest.fixture
def store(tmp_path):
    instance = RuntimeStore(tmp_path / "atomic.sqlite3")
    yield instance
    instance.close()


def test_trigger_replacement_failure_restores_all_old_triggers(store, monkeypatch):
    before = list(store.connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"))
    original = state_changes.trigger_sql

    def fail_later(spec):
        if spec == state_changes.SPECS[1]:
            return original(spec) + ["INVALID DDL"]
        return original(spec)

    monkeypatch.setattr(state_changes, "trigger_sql", fail_later)
    with pytest.raises(sqlite3.OperationalError):
        state_changes.install(store.connection)
    assert list(store.connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")) == before
    assert not store.connection.in_transaction


def test_trigger_install_does_not_commit_its_callers_transaction(store):
    with pytest.raises(ValueError, match="abort migration"):
        with store.transaction() as connection:
            connection.execute("INSERT INTO owner_session_revocations VALUES ('migration', 1)")
            state_changes.install(connection)
            assert connection.in_transaction
            raise ValueError("abort migration")
    assert not store.owner_session_revoked("migration")


def test_trigger_replacement_excludes_other_writers_until_complete(store):
    with sqlite3.connect(store.database_path, timeout=0) as other:
        blocked = []

        def competing_write(sql):
            if sql.startswith("CREATE TRIGGER"):
                try:
                    other.execute("INSERT INTO owner_session_revocations VALUES ('competing', 1)")
                except sqlite3.OperationalError as error:
                    blocked.append(str(error))

        store.connection.set_trace_callback(competing_write)
        try:
            state_changes.install(store.connection)
        finally:
            store.connection.set_trace_callback(None)
        assert blocked and all("locked" in error for error in blocked)


def test_retention_refusal_preserves_pending_outbox(store):
    session = store.create_session(user_id="user", mode="local", agent_id="native")["sessionId"]
    store.stage_outbox_event(session, {"id": "pending", "payload": {"text": "keep"}})
    assert not store.delete_session(session, stale_before="2000-01-01")
    assert store.get_session(session)["sessionId"] == session
    assert store.connection.execute("SELECT id FROM outbox_events").fetchone()[0] == "pending"
    assert store.delete_session(session, stale_before="9999-01-01")
    assert store.connection.execute("SELECT id FROM outbox_events").fetchone() is None


def test_default_switch_cannot_lose_its_target_between_read_and_write(store):
    target = store.create_plan_tier(label="new default", copy_of={})["name"]
    blocked = []
    with sqlite3.connect(store.database_path, timeout=0) as other:
        def competing_delete(sql):
            if sql.startswith("SELECT 1 FROM plan_tiers WHERE name"):
                try:
                    other.execute("DELETE FROM plan_tiers WHERE name = ? AND is_default = 0", (target,))
                except sqlite3.OperationalError as error:
                    blocked.append(str(error))

        store.connection.set_trace_callback(competing_delete)
        try:
            assert store.update_plan_tier(target, is_default=True)["isDefault"]
        finally:
            store.connection.set_trace_callback(None)
    assert blocked == ["database is locked"]
    assert store.default_plan_name() == target
    assert store.update_plan_tier("absent", is_default=True) is None
    assert store.default_plan_name() == target


def test_skill_batch_failure_rolls_back_earlier_switches(store):
    store.set_skill_enabled("first", True)
    store.connection.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON skill_settings
        WHEN NEW.skill_id = 'second' BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.set_skills_enabled({"first": False, "second": False})
    assert store.skill_settings_all() == {"first": True}
    store.connection.execute("DROP TRIGGER fail_second")
    store.set_skills_enabled({"first": False, "second": False})
    assert store.skill_settings_all() == {"first": False, "second": False}


@pytest.mark.parametrize("kind", ["playbook", "drawing"])
def test_count_limit_is_reserved_before_another_connection_can_create(store, kind):
    other = RuntimeStore(store.database_path)
    other.connection.execute("PRAGMA busy_timeout = 0")
    blocked = []

    def create(instance, name):
        if kind == "playbook":
            return instance.create_playbook(user_id="user", max_count=1, fields={
                "name": name, "entryRules": "", "exitRules": "", "stopRules": ""})
        return instance.create_chart_drawing(user_id="user", symbol="AU2612", kind="horizontal",
                                            points={}, label=name, max_per_symbol=1)

    def competing_create(sql):
        if sql.startswith(f"SELECT COUNT(*) FROM {'playbooks' if kind == 'playbook' else 'chart_drawings'}"):
            try:
                create(other, "second")
            except sqlite3.OperationalError as error:
                blocked.append(str(error))

    store.connection.set_trace_callback(competing_create)
    try:
        assert isinstance(create(store, "first"), dict)
        assert blocked == ["database is locked"]
        assert isinstance(create(other, "second"), str)
    finally:
        store.connection.set_trace_callback(None)
        other.close()
