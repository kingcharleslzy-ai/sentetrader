"""统一提醒 (#932): the one delivery entry and the 提醒中心 record — once only (a second round, a restart,
the other blue-green slot, a slot still running the previous build), one switch that silences every
channel, 信息 kinds never written into a conversation, a business card that still lands with its
reminder off, the older ledgers migrated so the switch-over day repeats nothing, the read/paging
interface and the retention sweep."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from test_simulation_orders import USER, Gateway, NormalProvider, application
from trader_runtime import reminders
from trader_runtime.broker import EventBroker
from trader_runtime.reminders import ReminderSpec, deliver_reminder, update_state
from trader_runtime.store import RuntimeStore
from trader_runtime.trading_calendar import trading_day_at
from trader_runtime.web_push import PushMessage


class Voice:
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def user_calls(self, user_id: str) -> int:
        return 1

    def announce(self, kind: str, text: str, *, user_id: str, replay_id: str | None = None,
                 key: str | None = None) -> dict[str, Any]:
        self.lines.append({"kind": kind, "text": text, "key": key})
        return {"queued": True}


class Push:
    def __init__(self) -> None:
        self.sent: list[tuple[str, PushMessage]] = []

    def notify(self, user_id: str, message: PushMessage, *, account_id: str | None = None) -> None:
        self.sent.append((user_id, message))


def world(path: Path) -> tuple[RuntimeStore, EventBroker, str]:
    store = RuntimeStore(path)
    broker = EventBroker(store)
    session = broker.create_session("local", "native", USER)
    broker.subscribers[session.session_id] = 1
    return store, broker, session.session_id


def spec(kind: str = "market_close", key: str = "acct:2026-10-15:day", **extra: Any) -> ReminderSpec:
    values: dict[str, Any] = {
        "kind": kind, "user_id": USER, "dedupe_key": key, "title": "收盘提醒", "body": "5 分钟后日盘收盘",
        "trading_day": "2026-10-15", "message": "收盘提醒：5 分钟后日盘收盘，你还有持仓未平。",
        "cards": [("card.reminder", {"cardId": f"reminder-{key}", "kind": "market_close", "title": "收盘提醒",
                                     "summary": "5 分钟后日盘收盘", "figures": [], "actions": [], "at": "x"})],
        "speech": "5 分钟后收盘", "push": PushMessage(kind="market_close", title="收盘前持仓", body="b", short="s",
                                                     tag="t")}
    values.update(extra)
    return ReminderSpec(**values)


def messages(store: RuntimeStore, session_id: str) -> list[dict[str, Any]]:
    return [event for _, event in store.list_events(session_id) if event.get("name") == "message.completed"]


def feeds(broker: EventBroker, session_id: str) -> list[dict[str, Any]]:
    return [event["payload"] for _, event in broker.transient_events.get(session_id, ())
            if event.get("name") == "feed.reminder"]


def switch(store: RuntimeStore, **notify: Any) -> None:
    store.update_preferences(USER, {"notify": notify})


async def test_a_reminder_goes_out_once_on_every_channel_and_never_again(tmp_path: Path):
    store, broker, session_id = world(tmp_path / "once.sqlite3")
    switch(store, marketCloseReminder=True)
    voice, push = Voice(), Push()
    first = await deliver_reminder(store, broker, spec(), voice=voice, push=push)
    assert first.reminder is not None and first.landed and first.session_id == session_id
    assert [event["payload"]["text"] for event in messages(store, session_id)] == [spec().message]
    [feed] = feeds(broker, session_id)
    assert feed["id"] == first.reminder["id"] and feed["type"] == "new" and feed["kind"] == "market_close"
    assert len(voice.lines) == 1 and voice.lines[0]["key"] == "acct:2026-10-15:day" and len(push.sent) == 1
    # The second round, a restart on the same database, the other slot on the same database: all quiet.
    again = await deliver_reminder(store, broker, spec(), voice=voice, push=push)
    store.close()
    other, other_broker, _ = world(tmp_path / "once.sqlite3")
    third = await deliver_reminder(other, other_broker, spec(), voice=voice, push=push)
    assert again.reminder is None and third.reminder is None
    assert len(messages(other, session_id)) == 1 and len(voice.lines) == 1 and len(push.sent) == 1
    other.close()


async def test_the_switch_off_silences_every_channel_and_records_nothing(tmp_path: Path):
    store, broker, session_id = world(tmp_path / "off.sqlite3")
    switch(store, marketCloseReminder=False)
    voice, push = Voice(), Push()
    result = await deliver_reminder(store, broker, spec(), voice=voice, push=push)
    assert result.reminder is None and not result.landed
    assert messages(store, session_id) == [] and feeds(broker, session_id) == []
    assert voice.lines == [] and push.sent == []
    assert store.reminders_page(USER, trading_day="2026-10-15", scope="today")[0] == []
    # Turned on the same day, the reminder is still due (nothing was claimed while off).
    switch(store, marketCloseReminder=True)
    assert (await deliver_reminder(store, broker, spec(), voice=voice, push=push)).reminder is not None
    store.close()


async def test_the_retired_voice_switches_do_not_mute_a_kind_whose_switch_is_on(tmp_path: Path):
    """PR-2 removed the voice page's 成交/简讯播报 switches: a user who once turned them off must still hear a
    fill or an AI 简讯 while that kind's own switch is on (one switch per kind, every channel)."""
    store, broker, _ = world(tmp_path / "voice-legacy.sqlite3")
    store.update_preferences(USER, {"voice": {"announceFills": False, "announceBriefs": False},
                                    "notify": {"desktop": {"fills": True, "briefs": True}}})
    voice = Voice()
    await deliver_reminder(store, broker, spec(kind="fill", key="acct:o1:t1", message=None, cards=[],
                                               target={"type": "orders"}), voice=voice)
    await deliver_reminder(store, broker, spec(kind="brief", key="SHFE.rb2701:news:1", message=None, cards=[],
                                               target={"type": "news"}), voice=voice)
    assert [line["kind"] for line in voice.lines] == ["fill", "brief"]
    store.close()


async def test_an_info_kind_never_lands_in_a_conversation(tmp_path: Path):
    store, broker, session_id = world(tmp_path / "info.sqlite3")
    result = await deliver_reminder(store, broker, spec(kind="research_debate", key="debate-1",
                                                        target={"type": "research", "debateId": "debate-1"},
                                                        session_id=session_id))
    assert result.reminder is not None and not result.landed
    assert messages(store, session_id) == []
    assert feeds(broker, session_id)[0]["kind"] == "research_debate"
    assert result.reminder["target"]["type"] == "research"
    store.close()


async def test_an_essential_card_lands_with_its_reminder_switched_off(tmp_path: Path):
    store, broker, session_id = world(tmp_path / "essential.sqlite3")
    switch(store, strategySignalAlerts=False)
    result = await deliver_reminder(store, broker, spec(kind="strategy", key="card-1", session_id=session_id,
                                                        essential=True))
    assert result.reminder is None and result.landed
    assert len(messages(store, session_id)) == 1 and feeds(broker, session_id) == []
    assert [event["name"] for _, event in store.list_events(session_id)].count("card.reminder") == 1
    assert not store.reminder_claimed(user_id=USER, kind="strategy", dedupe_key="card-1")
    store.close()


async def test_a_claim_the_previous_build_already_made_keeps_it_quiet(tmp_path: Path):
    """A slot still running the previous build wrote its own ledger row first: the new claim reads it in the
    same transaction and stays quiet — and writes nothing of its own."""
    store, broker, session_id = world(tmp_path / "legacy.sqlite3")
    switch(store, marketCloseReminder=True)
    assert store.record_reminder_notice(user_id=USER, account_id="acct", trade_day="2026-10-15",
                                        kind="market_close_day", text="old")

    def legacy(connection: Any) -> bool:
        return bool(store.record_reminder_notice(user_id=USER, account_id="acct", trade_day="2026-10-15",
                                                 kind="market_close_day", text="new", connection=connection))

    result = await deliver_reminder(store, broker, spec(legacy=legacy))
    assert result.reminder is None and messages(store, session_id) == []
    assert not store.reminder_claimed(user_id=USER, kind="market_close", dedupe_key="acct:2026-10-15:day")
    store.close()


def test_the_older_ledgers_become_read_reminders_once(tmp_path: Path):
    path = tmp_path / "migrate.sqlite3"
    store = RuntimeStore(path)
    now = datetime.now(UTC).isoformat()
    day = trading_day_at(now)   # 18:00 and later (and nights) belong to the next trading day
    with store.lock, store.connection:
        store.connection.execute("DELETE FROM reminder_meta")
        store.connection.execute(
            "INSERT INTO reminder_notices (user_id, account_id, trade_day, kind, text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (USER, "acct", day, "risk_ratio", "风险度提醒：现在风险度 85%", now))
        store.connection.execute(
            "INSERT INTO push_notices (user_id, key, created_at, text) VALUES (?, ?, ?, ?)",
            (USER, "market:SHFE.rb2701:1:上涨", now, "盘中异动：螺纹"))
        store.connection.execute(
            "INSERT INTO delivery_notices (user_id, symbol, trade_day, kind, level, text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (USER, "CZCE.CF611", day, "delivery_month", "warning", "交割提醒", now))
    store.close()
    store = RuntimeStore(path)   # the migration runs on open, once
    assert store.reminder_claimed(user_id=USER, kind="risk_ratio", dedupe_key=f"acct:{day}")
    assert store.reminder_claimed(user_id=USER, kind="market_movement", dedupe_key="market:SHFE.rb2701:1:上涨")
    assert store.reminder_claimed(user_id=USER, kind="delivery", dedupe_key=f"CZCE.CF611:{day}")
    items, _ = store.reminders_page(USER, trading_day=day, scope="today")
    assert len(items) == 3 and all(item["read"] for item in items)
    assert store.unread_reminder_count(USER, trading_day=day) == 0
    assert store.reminders_epoch()
    store.close()
    store = RuntimeStore(path)
    assert len(store.reminders_page(USER, trading_day=day, scope="today")[0]) == 3
    store.close()


async def test_an_existing_reminders_table_is_kept_checked_by_column_and_never_migrated_twice(tmp_path: Path):
    """A database that already has a reminders table with migrated rows (an earlier build of it, or this one)
    opens without duplicating anything; a column the earlier table lacked is added, not assumed."""
    import sqlite3
    path = tmp_path / "existing.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE reminders (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
            trading_day TEXT NOT NULL, created_at TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            dedupe_key TEXT NOT NULL, UNIQUE (user_id, kind, dedupe_key));
        CREATE TABLE reminder_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO reminder_meta VALUES ('epoch', '2026-10-15T08:56:46+00:00'),
                                         ('legacy_migrated', '2026-10-15T08:56:46+00:00');
        INSERT INTO reminders VALUES ('r1', 'local-user', 'risk_ratio', '2026-10-15', '2026-10-15T08:00:00+00:00',
                                      '风险度提醒', 'b', 'acct:2026-10-15');
    """)
    raw.commit()
    raw.close()
    for _ in range(2):
        store = RuntimeStore(path)
        columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(reminders)")}
        assert {"actions_json", "state_json", "read_at", "card_id", "target_json", "account_id"} <= columns
        assert store.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1
        assert store.reminders_epoch() == "2026-10-15T08:56:46+00:00"
        items, _ = store.reminders_page("local-user", trading_day="2026-10-15", scope="today")
        assert items[0]["target"] == {"type": "chat"} and items[0]["read"] is False
        store.close()
    store, broker, _ = world(path)
    assert (await deliver_reminder(store, broker, spec(kind="risk_ratio", key="acct:2026-10-15"))).reminder is None
    assert (await deliver_reminder(store, broker, spec(kind="risk_ratio", key="acct:2026-10-16"))).reminder is not None
    store.close()


async def test_state_follows_the_card_as_an_update_event(tmp_path: Path):
    store, broker, session_id = world(tmp_path / "state.sqlite3")
    result = await deliver_reminder(store, broker, spec(kind="stop_triggered", key="stop-1:order-1",
                                                        state={"text": "平仓单 已报", "tone": "wait"}))
    assert result.reminder is not None
    card_id = "reminder-stop-1:order-1"
    assert await update_state(store, broker, card_id=card_id, state={"text": "平仓单 已成交 771.5 ×1", "tone": "ok"}) == 1
    assert await update_state(store, broker, card_id=card_id, state={"text": "平仓单 已成交 771.5 ×1", "tone": "ok"}) == 0
    updates = [feed for feed in feeds(broker, session_id) if feed["type"] == "update"]
    assert len(updates) == 1 and updates[0]["id"] == result.reminder["id"]
    [item] = store.reminders_page(USER, trading_day="2026-10-15", scope="today")[0]
    assert item["state"] == {"text": "平仓单 已成交 771.5 ×1", "tone": "ok"}
    store.close()


def test_retention_drops_rows_past_the_retained_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = RuntimeStore(tmp_path / "retain.sqlite3")
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    with store.transaction() as connection:
        store.claim_reminder(connection, user_id=USER, kind="fill", dedupe_key="old", trading_day=old[:10],
                             title="成交", body="b", created_at=old)
        store.claim_reminder(connection, user_id=USER, kind="fill", dedupe_key="new", trading_day="2026-10-15",
                             title="成交", body="b")
        store.claim_push_notice(user_id=USER, key="market:x", connection=connection, at=old)
    monkeypatch.setenv(reminders.RETAIN_DAYS_ENV, "30")

    class App:
        class state:
            pass
    App.state.store = store
    counts = reminders.ReminderRetention(App).run_once()
    assert counts["reminders"] == 1 and counts["push_notices"] == 1
    assert not store.reminder_claimed(user_id=USER, kind="fill", dedupe_key="old")
    assert store.reminder_claimed(user_id=USER, kind="fill", dedupe_key="new")
    store.close()


async def test_fills_are_judged_once_by_the_ledger_for_each_owner_even_across_a_restart(tmp_path: Path):
    """#932: the account ledger is the one judge of 成交 / 撤单 — one reminder per owner and fill, never a
    conversation message; a new ledger (a restart, the other slot) re-reading the same activity stays quiet."""
    from trader_runtime.account_ledger import AccountLedger
    store, broker, session_id = world(tmp_path / "fills.sqlite3")
    account = store.default_trading_account_id
    store.account_owner_ids = lambda account_id: [USER]  # type: ignore[method-assign]
    now = int(datetime.now(UTC).timestamp() * 1_000_000_000)
    activity = {"trades": [{"tradeId": "t1", "clientOrderId": "o1", "symbol": "SHFE.rb2701", "direction": "BUY",
                            "offset": "OPEN", "price": 3060, "volume": 1, "tradeDateTime": now - 5_000_000_000}],
                "orders": []}

    async def sink(spec: ReminderSpec, *, voice: Any = None):
        return await deliver_reminder(store, broker, spec, voice=voice)
    store.reminder_sink = sink
    ledger = AccountLedger(store=store, provider=None, trading_account_id=account)   # type: ignore[arg-type]
    await ledger._push_activity({"trades": [], "orders": []})   # the first read after the start
    await ledger._push_activity(activity)
    await ledger._push_activity(activity)
    items = store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]
    assert [(item["kind"], item["target"]["type"]) for item in items] == [("fill", "orders")]
    assert "买开 1 手 @ 3060" in items[0]["body"] and messages(store, session_id) == []
    restarted = AccountLedger(store=store, provider=None, trading_account_id=account)  # type: ignore[arg-type]
    await restarted._push_activity(activity)   # its first read: the fill is after the epoch, but claimed
    assert len(store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]) == 1
    # #932 P2 重组: the next holder sees t1 with a new t2 in one read — only t2 is told, never “2 手” again;
    # and a holder that sees just t1 of the t1+t2 already told stays quiet.
    fresh = int(datetime.now(UTC).timestamp() * 1_000_000_000)   # both after the claims' epoch: not history
    t1 = {**activity["trades"][0], "tradeDateTime": fresh}
    t2 = {**t1, "tradeId": "t2", "price": 3062}
    regrouped = AccountLedger(store=store, provider=None, trading_account_id=account)  # type: ignore[arg-type]
    await regrouped._push_activity({"trades": [t1, t2], "orders": []})
    items = store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]
    assert len(items) == 2 and "买开 1 手 @ 3062" in items[0]["body"]
    again = AccountLedger(store=store, provider=None, trading_account_id=account)  # type: ignore[arg-type]
    await again._push_activity({"trades": [t2], "orders": []})
    assert len(store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]) == 2
    store.close()


@pytest.mark.parametrize("first", ["A", "B"])
async def test_two_holders_that_read_the_same_fills_before_either_commits_tell_each_fill_once(tmp_path: Path,
                                                                                             first: str):
    """#932 R1: two ledgers on two real store connections both read the fills before either commits — A holds
    t1, B holds t1+t2. Whichever commits first, each fill is told once, and the record, the feed, the spoken
    line and the push say exactly the fills that reminder's own transaction claimed."""
    import asyncio

    from trader_runtime.account_ledger import AccountLedger
    path = tmp_path / f"race-{first}.sqlite3"
    store_a, broker_a, session_id = world(path)
    store_b = RuntimeStore(path)
    broker_b = EventBroker(store_b)
    broker_b.subscribers[session_id] = 1
    account = store_a.default_trading_account_id
    voice, push = Voice(), Push()
    gates = {"A": asyncio.Event(), "B": asyncio.Event()}
    arrived = {"A": asyncio.Event(), "B": asyncio.Event()}
    done = {"A": asyncio.Event(), "B": asyncio.Event()}
    results: dict[str, Any] = {}
    ledgers: dict[str, AccountLedger] = {}
    for label, store, broker in (("A", store_a, broker_a), ("B", store_b, broker_b)):
        store.account_owner_ids = lambda account_id: [USER]  # type: ignore[method-assign]

        async def sink(spec: ReminderSpec, *, voice_: Any = None, label: str = label, store: RuntimeStore = store,
                       broker: EventBroker = broker, **_: Any):
            arrived[label].set()   # both holders have read what was told before either commits
            await gates[label].wait()
            results[label] = await deliver_reminder(store, broker, spec, voice=voice, push=push)
            done[label].set()
            return results[label]
        store.reminder_sink = sink  # type: ignore[attr-defined]
        ledgers[label] = AccountLedger(store=store, provider=None, trading_account_id=account)  # type: ignore[arg-type]
        await ledgers[label]._push_activity({"trades": [], "orders": []})   # the first read after the start
    now = int(datetime.now(UTC).timestamp() * 1_000_000_000)
    t1 = {"tradeId": "t1", "clientOrderId": "o1", "symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN",
          "price": 3060, "volume": 1, "tradeDateTime": now}
    t2 = {**t1, "tradeId": "t2", "price": 3062}
    reads = asyncio.gather(ledgers["A"]._push_activity({"trades": [t1], "orders": []}),
                           ledgers["B"]._push_activity({"trades": [t1, t2], "orders": []}))
    await arrived["A"].wait()
    await arrived["B"].wait()
    second = "B" if first == "A" else "A"
    gates[first].set()
    await done[first].wait()
    gates[second].set()
    await reads

    items = store_a.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]
    told = sorted(item["body"] for item in items)
    sent = [message for _, message in push.sent]
    if first == "A":
        assert told == ["买开 1 手 @ 3060", "买开 1 手 @ 3062"]
        assert results["B"].outcome == "delivered" and results["B"].reminder["body"] == "买开 1 手 @ 3062"
        assert store_a.reminder_claimed(user_id=USER, kind="fill", dedupe_key=f"{account}:o1:t2")
        assert sorted(message.body for message in sent) == ["买开 1 手，成交价 3,060。", "买开 1 手，成交价 3,062。"]
        assert len(voice.lines) == 2
    else:
        assert told == ["买开 2 手 @ 3062"]
        assert results["A"].outcome == "claimed" and results["A"].reminder is None
        assert store_a.reminder_claimed(user_id=USER, kind="fill", dedupe_key=f"{account}:o1:t1,t2")
        assert [message.body for message in sent] == ["买开 2 手，成交价 3,062。"]
        assert len(voice.lines) == 1 and "2 手" in voice.lines[0]["text"]
    # Every channel said what the record says: one feed per record, one push per record, one line per record.
    assert len(feeds(broker_a, session_id)) + len(feeds(broker_b, session_id)) == len(items) == len(sent)
    assert sorted(item["body"] for item in items) == sorted(line["text"].split(" ", 1)[1].removesuffix("，已成交。")
                                                            for line in voice.lines)
    store_b.close()
    store_a.close()


async def test_a_fill_or_cancel_whose_transaction_failed_is_told_on_the_next_pass_of_the_same_ledger(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """#932 R2: the transaction rolled back (nothing recorded) — the same ledger, not restarted, tells it on its
    next pass; a channel failing after the commit is never retried as a second reminder; a cancel that keeps
    failing is given up after its window (fake clock)."""
    import sqlite3

    from trader_runtime.account_ledger import AccountLedger
    from trader_runtime.web_push import CANCEL_RECENT_SECONDS
    store, broker, _ = world(tmp_path / "retry.sqlite3")
    account = store.default_trading_account_id
    store.account_owner_ids = lambda account_id: [USER]  # type: ignore[method-assign]
    push = Push()

    class FailingPush(Push):
        def notify(self, user_id: str, message: PushMessage, *, account_id: str | None = None) -> None:
            raise RuntimeError("push service down")

    channel: dict[str, Any] = {"push": push}

    async def sink(spec: ReminderSpec, *, voice: Any = None):
        return await deliver_reminder(store, broker, spec, voice=voice, push=channel["push"])
    store.reminder_sink = sink  # type: ignore[attr-defined]
    real_claim = store.claim_reminder
    broken = {"on": True}

    def claim_reminder(*args: Any, **kwargs: Any):
        if broken["on"]:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(*args, **kwargs)
    monkeypatch.setattr(store, "claim_reminder", claim_reminder)

    clock = {"now": int(datetime.now(UTC).timestamp() * 1_000_000_000)}
    ledger = AccountLedger(store=store, provider=None, trading_account_id=account)  # type: ignore[arg-type]
    ledger.clock = lambda: clock["now"]
    await ledger._push_activity({"trades": [], "orders": []})
    fill = {"tradeId": "t1", "clientOrderId": "o1", "symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN",
            "price": 3060, "volume": 1, "tradeDateTime": clock["now"]}
    order = {"clientOrderId": "o9", "symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN",
             "status": "FINISHED", "volume": 3, "volumeLeft": 3, "lastMsg": "已撤单", "insertDateTime": clock["now"]}
    activity = {"trades": [fill], "orders": [order]}

    def told() -> list[str]:
        return sorted(item["kind"] for item in
                      store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0])

    await ledger._push_activity(activity)
    assert told() == [] and push.sent == []
    # The rollback took the fill's claim with it: it is still not told.
    assert store.told_fill_trades(user_id=USER, account_id=account, order_id="o1") == set()
    broken["on"] = False
    clock["now"] += 30 * 1_000_000_000
    await ledger._push_activity(activity)
    assert told() == ["cancel", "fill"] and len(push.sent) == 2
    await ledger._push_activity(activity)   # told: never again
    assert told() == ["cancel", "fill"] and len(push.sent) == 2

    # A push that fails after the commit stays a delivered reminder: the next pass sends nothing more.
    channel["push"] = FailingPush()
    second = {**fill, "tradeId": "t2", "tradeDateTime": clock["now"]}
    await ledger._push_activity({"trades": [fill, second], "orders": [order]})
    await ledger._push_activity({"trades": [fill, second], "orders": [order]})
    assert told() == ["cancel", "fill", "fill"]

    # A cancel whose transaction keeps failing is retried within its window, then given up.
    broken["on"] = True
    late = {**order, "clientOrderId": "o10"}
    stuck = {"trades": [fill, second], "orders": [order, late]}
    await ledger._push_activity(stuck)
    clock["now"] += (CANCEL_RECENT_SECONDS - 60) * 1_000_000_000
    await ledger._push_activity(stuck)
    assert ("cancelled", "o10") in ledger._pending
    clock["now"] += 120 * 1_000_000_000
    await ledger._push_activity(stuck)
    assert ("cancelled", "o10") not in ledger._pending
    broken["on"] = False
    await ledger._push_activity(stuck)
    assert told() == ["cancel", "fill", "fill"]
    store.close()


async def test_a_fill_that_failed_near_the_end_of_its_age_window_is_retried_from_the_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """#932 R2 (Codex r4307841901): a fill first seen 590 s after it traded fails its transaction; the retry window counts
    from the failure, so the age rule (600 s from the trade) does not drop it on the next pass."""
    import sqlite3

    from trader_runtime.account_ledger import AccountLedger
    from trader_runtime.web_push import FILL_RECENT_SECONDS
    store, broker, _ = world(tmp_path / "late-retry.sqlite3")
    store.account_owner_ids = lambda account_id: [USER]  # type: ignore[method-assign]

    async def sink(spec: ReminderSpec, *, voice: Any = None):
        return await deliver_reminder(store, broker, spec, voice=voice)
    store.reminder_sink = sink  # type: ignore[attr-defined]
    real_claim = store.claim_reminder
    broken = {"on": True}

    def claim_reminder(*args: Any, **kwargs: Any):
        if broken["on"]:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(*args, **kwargs)
    monkeypatch.setattr(store, "claim_reminder", claim_reminder)
    clock = {"now": int(datetime.now(UTC).timestamp() * 1_000_000_000)}
    ledger = AccountLedger(store=store, provider=None, trading_account_id=store.default_trading_account_id)  # type: ignore[arg-type]
    ledger.clock = lambda: clock["now"]
    await ledger._push_activity({"trades": [], "orders": []})
    fill = {"tradeId": "t1", "clientOrderId": "o1", "symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN",
            "price": 3060, "volume": 1, "tradeDateTime": clock["now"] - (FILL_RECENT_SECONDS - 10) * 1_000_000_000}
    await ledger._push_activity({"trades": [fill], "orders": []})
    broken["on"] = False
    clock["now"] += 60 * 1_000_000_000          # the trade is now 650 s old, the failure 60 s
    await ledger._push_activity({"trades": [fill], "orders": []})
    items = store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]
    assert [item["kind"] for item in items] == ["fill"] and ledger._pending == {}
    store.close()


async def test_a_cancel_says_what_filled_and_names_the_account_when_there_are_two(tmp_path: Path):
    """#932 P2: “已撤单：已撤单” said the gateway's word twice and nothing of the lots that filled; with two
    accounts the 成交 / 撤单 rows carry the account's name like 风险度 and 收盘提醒."""
    from trader_runtime.account_ledger import AccountLedger
    store, broker, _ = world(tmp_path / "cancel.sqlite3")
    account = store.default_trading_account_id
    store.account_owner_ids = lambda account_id: [USER]  # type: ignore[method-assign]
    store.list_account_grants = lambda user_id: [{"id": "a"}, {"id": "b"}]  # type: ignore[method-assign]
    store.get_trading_account = lambda account_id: {"display_name": "快期测试"}  # type: ignore[method-assign]
    now = int(datetime.now(UTC).timestamp() * 1_000_000_000)

    async def sink(spec: ReminderSpec, *, voice: Any = None):
        return await deliver_reminder(store, broker, spec, voice=voice)
    store.reminder_sink = sink
    ledger = AccountLedger(store=store, provider=None, trading_account_id=account)   # type: ignore[arg-type]
    await ledger._push_activity({"trades": [], "orders": []})
    order = {"clientOrderId": "o9", "symbol": "SHFE.rb2701", "direction": "BUY", "offset": "OPEN", "status": "FINISHED",
             "volume": 3, "volumeLeft": 2, "lastMsg": "已撤单", "insertDateTime": now - 5_000_000_000}
    await ledger._push_activity({"trades": [], "orders": [order]})
    item = store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0][0]
    assert item["kind"] == "cancel" and item["title"].endswith(" · 快期测试")
    assert item["body"] == "买开 3 手，已成交 1 手，余 2 手已撤" and "已撤单：" not in item["body"]
    store.close()


async def test_a_strategy_fill_reminder_names_its_strategy(tmp_path: Path):
    """#932：策略自动下单的执行卡只进策略自己的对话；成交提醒（用户在哪都看得到）末尾带来源
    “策略 · 名称”，AI 自主与止损同理，用户自己的单不带。来源取复盘“来源”的同一入口 order_sources。"""
    from trader_runtime.account_ledger import AccountLedger
    from trader_runtime.strategy_workbench import source_label
    store, broker, _ = world(tmp_path / "strategy-fill.sqlite3")
    store.account_owner_ids = lambda account_id: [USER]  # type: ignore[method-assign]
    now = int(datetime.now(UTC).timestamp() * 1_000_000_000)

    def trade(trade_id: str, order_id: str) -> dict[str, Any]:
        return {"tradeId": trade_id, "clientOrderId": order_id, "symbol": "SHFE.rb2701", "direction": "BUY",
                "offset": "OPEN", "price": 3068, "volume": 1, "tradeDateTime": now - 5_000_000_000}

    async def sink(spec: ReminderSpec, *, voice: Any = None):
        return await deliver_reminder(store, broker, spec, voice=voice)
    store.reminder_sink = sink
    ledger = AccountLedger(store=store, provider=None, trading_account_id=store.default_trading_account_id)  # type: ignore[arg-type]
    ledger._order_sources = lambda user_id: {  # type: ignore[method-assign]
        "o-strategy": {"source": "strategy", "strategyName": "双均线"}, "o-card": {"source": "card"}}
    await ledger._push_activity({"trades": [], "orders": []})
    await ledger._push_activity({"trades": [trade("t1", "o-strategy"), trade("t2", "o-card")], "orders": []})
    bodies = sorted(item["body"] for item in
                    store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0])
    assert bodies == ["买开 1 手 @ 3068", "买开 1 手 @ 3068 · 策略 · 双均线"]
    assert [source_label(item) for item in (
        {"source": "ai"}, {"source": "stop"}, {"source": "strategy"}, {"source": "card"}, None)] == [
        "AI 自主", "止损", "策略", None, None]
    store.close()


async def test_a_forced_liquidation_fill_reads_qiangping_through_the_one_source_entry(tmp_path: Path):
    """#1204：本站模拟盘强平的平仓单（clientOrderId 以 paper-force- 开头）没有任何 App 记录，经同一入口
    order_sources 读成“强平”——成交提醒末尾、成交页回合来源（盖过开仓方）、复盘标签都一样。"""
    from trader_runtime.account_ledger import AccountLedger
    from trader_runtime.review_tags import tag_for
    from trader_runtime.strategy_workbench import order_sources, source_label
    from trader_runtime.trading_costs import round_source
    store, broker, _ = world(tmp_path / "forced-fill.sqlite3")
    store.account_owner_ids = lambda account_id: [USER]  # type: ignore[method-assign]
    now = int(datetime.now(UTC).timestamp() * 1_000_000_000)

    async def sink(spec: ReminderSpec, *, voice: Any = None):
        return await deliver_reminder(store, broker, spec, voice=voice)
    store.reminder_sink = sink
    ledger = AccountLedger(store=store, provider=None, trading_account_id=store.default_trading_account_id)  # type: ignore[arg-type]
    await ledger._push_activity({"trades": [], "orders": []})
    forced = {"tradeId": "t1", "clientOrderId": "paper-force-paper:abc-1", "symbol": "SHFE.rb2701", "direction": "SELL",
              "offset": "CLOSE", "price": 3068, "volume": 1, "tradeDateTime": now - 5_000_000_000}
    await ledger._push_activity({"trades": [forced], "orders": []})
    [item] = store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]
    assert item["body"].endswith(" · 强平")
    sources = order_sources(store, USER)
    assert source_label(sources.get("paper-force-paper:abc-1")) == "强平" and sources.get("o-mine") is None
    assert round_source(sources, "o-mine", "paper-force-paper:abc-1") == {"source": "liquidation"}
    assert tag_for(source="liquidation", placed_ns=now, symbol="SHFE.rb2701", reminders=[])[0] == "plan"
    store.close()


async def test_a_new_brief_reminds_its_subscribers_once_and_never_in_a_conversation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = application(tmp_path / "brief.sqlite3", Gateway(), NormalProvider())
    store, briefs = app.state.store, app.state.market_briefs
    store.update_preferences(USER, {"notify": {"desktop": {"briefs": True}}})
    session = app.state.broker.create_session("local", "native", USER)
    app.state.broker.subscribers[session.session_id] = 1
    monkeypatch.setattr(briefs, "enabled", lambda user: True)

    async def mains(user: str) -> list[str]:
        return ["SHFE.rb2701"]
    monkeypatch.setattr(briefs, "_subscribed_charts", mains)
    story = {"id": "news:abc", "text": "上期所调整螺纹钢交割库容。"}
    await briefs._remind("SHFE.rb2701", "螺纹钢", story)
    await briefs._remind("SHFE.rb2701", "螺纹钢", story)
    items = store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]
    assert [(item["kind"], item["target"]["type"]) for item in items] == [("brief", "news")]
    assert messages(store, session.session_id) == []
    assert [feed["kind"] for feed in feeds(app.state.broker, session.session_id)] == ["brief"]
    store.close()


def test_the_calls_repeat_throttle_is_per_kind_and_object(tmp_path: Path):
    """#932: two strategies' signals in the same minute are both heard; the same strategy's twice is not."""
    from types import SimpleNamespace
    app = application(tmp_path / "throttle.sqlite3", Gateway(), NormalProvider())
    service = app.state.voice_realtime
    service.announce("strategy-signal", "螺纹回踩出信号", user_id=USER, key="s1")
    service.announce("strategy-signal", "沪金突破出信号", user_id=USER, key="s2")
    service.announce("strategy-signal", "螺纹回踩又出信号", user_id=USER, key="s1")
    state = SimpleNamespace(last_announce={})
    heard = [item["text"] for item in service._pending_announcements[USER]
             if not service._throttled(state, item.get("throttleKey") or item["kind"])]
    assert heard == ["螺纹回踩出信号", "沪金突破出信号"]
    app.state.store.close()


async def test_an_emergency_stop_always_reminds_whatever_the_switches_say(tmp_path: Path):
    app = application(tmp_path / "emergency.sqlite3", Gateway(), NormalProvider())
    store = app.state.store
    store.update_preferences(USER, {"notify": {"stopTriggered": False, "protectionCancelled": False,
                                               "desktop": {"fills": False, "cancels": False}}})
    session = app.state.broker.create_session("local", "native", USER)
    app.state.broker.subscribers[session.session_id] = 1
    await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    items = store.reminders_page(USER, trading_day=reminders.current_trading_day(), scope="today")[0]
    assert [item["kind"] for item in items] == ["emergency_stop"]
    assert feeds(app.state.broker, session.session_id)[-1]["kind"] == "emergency_stop"
    assert messages(store, session.session_id) == []   # no conversation message, as before
    store.close()


async def test_the_reminder_center_pages_and_marks_read(tmp_path: Path):
    app = application(tmp_path / "center.sqlite3", Gateway(), NormalProvider())
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                         headers={"Origin": "http://127.0.0.1:5173"})
    assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
    store = app.state.store
    today = reminders.current_trading_day()
    with store.transaction() as connection:
        for index in range(5):
            store.claim_reminder(connection, user_id=USER, kind="fill", dedupe_key=f"f{index}", trading_day=today,
                                 title="成交", body=f"第 {index} 笔", created_at=f"2026-10-15T0{index}:00:00+00:00",
                                 target={"type": "orders"})
        store.claim_reminder(connection, user_id=USER, kind="risk_ratio", dedupe_key="old", trading_day="2026-01-05",
                             title="风险度提醒", body="b")
    page = (await client.get("/api/reminders", params={"limit": 2})).json()
    assert [item["body"] for item in page["items"]] == ["第 4 笔", "第 3 笔"] and page["unread"] == 5
    assert page["tradingDay"] == today and page["nextCursor"] and page["moreCommand"] is True   # #932 R5 support flag
    rest = (await client.get("/api/reminders", params={"limit": 10, "before": page["nextCursor"]})).json()
    assert [item["body"] for item in rest["items"]] == ["第 2 笔", "第 1 笔", "第 0 笔"] and rest["nextCursor"] is None
    earlier = (await client.get("/api/reminders", params={"scope": "earlier"})).json()
    assert [item["kind"] for item in earlier["items"]] == ["risk_ratio"]
    read = await client.post("/api/reminders/read", json={"ids": [page["items"][0]["id"]]})
    assert read.status_code == 200 and read.json() == {"unread": 4}
    unread = (await client.get("/api/reminders", params={"unread": "true"})).json()
    assert len(unread["items"]) == 4 and all(not item["read"] for item in unread["items"])
    assert (await client.post("/api/reminders/read", json={"all": True})).json() == {"unread": 0}
    assert (await client.post("/api/reminders/read", json={})).status_code == 422
    capabilities = (await client.get("/api/agent/capabilities")).json()
    assert "GET /api/reminders" in str(capabilities) and "POST /api/reminders/read" in str(capabilities)
    await client.aclose()
