"""The background ledger books fills and daily settlement with no client online.

The push feed only reads for sessions with an attached SSE reader; the ledger
owns the account_trades/account_daily writes now, on the Runtime's own loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from time import monotonic
from typing import Any

from test_replay import IDENTIFIER, HistoricalSource
from test_simulation_orders import NormalProvider
from trader_runtime.account_ledger import AccountLedger
from trader_runtime.main import create_app
from trader_runtime.replay import ReplayProvider
from trader_runtime.store import RuntimeStore

TOKEN = "ledger-test-token"
AU = "SHFE.au2612"


def at(day: str, hour: int) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+08:00").timestamp() * 1e9)


def fill(trade_id: str, *, price: float = 820.0, day: str = "2026-09-18") -> dict[str, Any]:
    return {"tradeId": trade_id, "clientOrderId": f"order-{trade_id}", "symbol": AU, "direction": "BUY",
            "offset": "OPEN", "price": price, "volume": 1, "tradeDateTime": at(day, 10), "tradingDay": day}


class LedgerProvider(NormalProvider):
    """Normal account data whose fills and equity the test moves between runs."""

    def __init__(self) -> None:
        super().__init__()
        self.day = "2026-09-18"
        self.trades: list[dict[str, Any]] = []
        self.equity = 1_000_000.0
        self.static_balance: float | None = None
        self.close_profit: float | None = None
        self.position_profit: float | None = None
        self.commission: float | None = None
        self.today_net_pnl: float | None = None
        self.simulated = False
        self.activity_error: RuntimeError | None = None

    def account_activity(self) -> dict[str, Any]:
        if self.activity_error is not None:
            raise self.activity_error
        return {**super().account_activity(), "dataStatus": "simulated" if self.simulated else "normal",
                "trades": list(self.trades)}

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        summary = {**super().account_summary(turn_id), "tradingDay": self.day, "equity": self.equity,
                   "staticBalance": self.static_balance, "closeProfit": self.close_profit,
                   "positionProfit": self.position_profit, "commission": self.commission,
                   "todayNetPnl": self.today_net_pnl}
        if self.simulated:
            summary["dataStatus"] = "simulated"
        return summary


async def test_books_fills_and_daily_without_any_client_session(tmp_path):
    provider = LedgerProvider()
    provider.trades = [fill("ledger-open")]
    store = RuntimeStore(tmp_path / "ledger.sqlite3")
    try:
        ledger = AccountLedger(store=store, provider=provider)
        result = await ledger.run_once()
        assert result == {"fills": 1, "backfilled": 0, "daily": 1}
        rows = store.list_account_trades()
        assert [(row["tradeId"], row["tradingDay"], row["volumeMultiple"]) for row in rows] == [
            ("ledger-open", "2026-09-18", 1000.0)]
        assert [(row["tradingDay"], row["equity"]) for row in store.list_account_daily()] == [
            ("2026-09-18", 1_000_000.0)]
    finally:
        store.close()


async def test_unchanged_fills_are_not_rewritten_and_today_s_row_is_overwritten(tmp_path):
    provider = LedgerProvider()
    provider.trades = [fill("ledger-open")]
    store = RuntimeStore(tmp_path / "dedup.sqlite3")
    try:
        ledger = AccountLedger(store=store, provider=provider)
        await ledger.run_once()
        captured = store.connection.execute(
            "SELECT captured_at FROM account_trades WHERE trade_id = 'ledger-open'").fetchone()[0]
        provider.equity = 1_010_000.0
        assert await ledger.run_once() == {"fills": 0, "backfilled": 0, "daily": 1}
        assert store.connection.execute(
            "SELECT captured_at FROM account_trades WHERE trade_id = 'ledger-open'").fetchone()[0] == captured
        assert [row["equity"] for row in store.list_account_daily()] == [1_010_000.0]
    finally:
        store.close()


async def test_a_fill_gaining_its_trading_day_later_is_booked_again(tmp_path):
    """#514-6: the dedup cache keys on (tradeId, tradingDay), so a fill the sidecar first reported
    without its trading day is written again once the day arrives (the upsert fills the column);
    an unchanged pass after that still writes nothing."""
    provider = LedgerProvider()
    provider.trades = [{**fill("day-late"), "tradingDay": None}]
    store = RuntimeStore(tmp_path / "day-key.sqlite3")
    try:
        ledger = AccountLedger(store=store, provider=provider)
        assert await ledger.run_once() == {"fills": 1, "backfilled": 0, "daily": 1}
        assert store.list_account_trades()[0]["tradingDay"] is None
        provider.trades = [fill("day-late")]   # the same id, now with its trading day
        assert await ledger.run_once() == {"fills": 1, "backfilled": 0, "daily": 1}
        assert store.list_account_trades()[0]["tradingDay"] == "2026-09-18"
        assert await ledger.run_once() == {"fills": 0, "backfilled": 0, "daily": 1}
    finally:
        store.close()


def test_settlement_zeroing_keeps_the_day_s_pnl_columns(tmp_path):
    """TqKq clears the day's P&L fields after the 15:15 settlement while equity moves on;
    the stored row must keep the intraday figures instead of being flattened to zero."""
    store = RuntimeStore(tmp_path / "settled.sqlite3")
    try:
        day = "2026-09-18"
        store.upsert_account_daily(day, {"equity": 1036929.0, "staticBalance": 1036929.0,
                                         "closeProfit": -1500.0, "positionProfit": -80.0,
                                         "commission": 222.0, "todayNetPnl": -1722.0})
        # The 30 s loop books the cleared snapshot: equity and static balance move, P&L survives.
        store.upsert_account_daily(day, {"equity": 1035207.0, "staticBalance": 1035207.0,
                                         "closeProfit": 0.0, "positionProfit": 0.0,
                                         "commission": 0.0, "todayNetPnl": 0.0})
        row = store.list_account_daily()[0]
        assert (row["equity"], row["staticBalance"]) == (1035207.0, 1035207.0)
        assert (row["closeProfit"], row["positionProfit"], row["commission"], row["todayNetPnl"]) == \
            (-1500.0, -80.0, 222.0, -1722.0)
        # A genuinely new non-zero figure (e.g. the next trading session) still overwrites.
        store.upsert_account_daily(day, {"equity": 1035007.0, "closeProfit": -1800.0, "positionProfit": -100.0,
                                         "commission": 250.0, "todayNetPnl": -2150.0})
        row = store.list_account_daily()[0]
        assert (row["equity"], row["closeProfit"], row["positionProfit"], row["commission"], row["todayNetPnl"]) == \
            (1035007.0, -1800.0, -100.0, 250.0, -2150.0)
        # A snapshot without the fields at all must not erase what the day already has.
        store.upsert_account_daily(day, {"equity": 1034907.0})
        row = store.list_account_daily()[0]
        assert (row["equity"], row["closeProfit"], row["commission"], row["todayNetPnl"]) == \
            (1034907.0, -1800.0, 250.0, -2150.0)
    finally:
        store.close()


def test_intraday_net_pnl_back_at_zero_is_booked_not_mistaken_for_settlement(tmp_path):
    """#500: a normal snapshot whose net P&L is exactly zero (position profit eaten by the commission)
    is a real reading — the row, the headline and the curve all say 0, after a reopen too; only the
    settlement's all-cleared snapshot keeps the day's earlier figures."""
    from trader_runtime.account_pnl import account_pnl
    day = "2026-09-18"
    eleven = datetime.fromisoformat(f"{day}T11:00:00+08:00")
    for earlier in (100.0, -100.0):
        path = tmp_path / f"zero-{earlier}.sqlite3"
        store = RuntimeStore(path)
        store.upsert_account_daily(day, {"equity": 1_000_000 + earlier, "staticBalance": 1_000_000.0,
                                         "closeProfit": 0.0, "positionProfit": earlier + 10, "commission": 10.0,
                                         "todayNetPnl": earlier})
        back = {"equity": 1_000_000.0, "staticBalance": 1_000_000.0, "closeProfit": 0.0,
                "positionProfit": 10.0, "commission": 10.0, "todayNetPnl": 0.0}
        store.upsert_account_daily(day, back)
        store.close()
        store = RuntimeStore(path)
        try:
            row = store.list_account_daily()[0]
            assert (row["positionProfit"], row["commission"], row["todayNetPnl"]) == (10.0, 10.0, 0.0)
            review = {"account": {**back, "dataStatus": "normal", "tradingDay": day}, "trades": []}
            pnl = account_pnl(review, daily=store.list_account_daily(), initial_balance=None, months=6, now=eleven)
            assert pnl["today"] == 0.0
            assert [(p["day"], p["equity"], p["pnl"]) for p in pnl["equityCurve"]] == [(day, 1_000_000.0, 0.0)]
            # The settlement clears every field: the booked zero stays, read after read.
            cleared = {"equity": 1_000_000.0, "staticBalance": 1_000_000.0, "closeProfit": 0.0,
                       "positionProfit": 0.0, "commission": 0.0, "todayNetPnl": 0.0}
            store.upsert_account_daily(day, cleared)
            store.upsert_account_daily(day, cleared)
            row = store.list_account_daily()[0]
            assert (row["positionProfit"], row["commission"], row["todayNetPnl"]) == (10.0, 10.0, 0.0)
            review = {"account": {**cleared, "dataStatus": "normal", "tradingDay": day}, "trades": []}
            assert account_pnl(review, daily=store.list_account_daily(), initial_balance=None, months=6,
                               now=eleven)["today"] == 0.0
        finally:
            store.close()
    # Contrast: a settlement after a non-zero day keeps the day's whole result for the headline.
    store = RuntimeStore(tmp_path / "settled-contrast.sqlite3")
    try:
        store.upsert_account_daily(day, {"equity": 1_000_100.0, "staticBalance": 1_000_000.0, "closeProfit": 0.0,
                                         "positionProfit": 110.0, "commission": 10.0, "todayNetPnl": 100.0})
        cleared = {"equity": 1_000_100.0, "staticBalance": 1_000_100.0, "closeProfit": 0.0,
                   "positionProfit": 0.0, "commission": 0.0, "todayNetPnl": 0.0}
        store.upsert_account_daily(day, cleared)
        assert store.list_account_daily()[0]["todayNetPnl"] == 100.0
        review = {"account": {**cleared, "dataStatus": "normal", "tradingDay": day}, "trades": []}
        late = datetime.fromisoformat(f"{day}T15:20:00+08:00")
        assert account_pnl(review, daily=store.list_account_daily(), initial_balance=None, months=6,
                           now=late)["today"] == 100.0
    finally:
        store.close()


async def test_ledger_pass_after_settlement_keeps_the_booked_pnl(tmp_path):
    provider = LedgerProvider()
    provider.equity = 1036929.0
    provider.static_balance = 1036929.0
    provider.close_profit = -1500.0
    provider.position_profit = -80.0
    provider.commission = 222.0
    provider.today_net_pnl = -1802.0
    store = RuntimeStore(tmp_path / "settled-loop.sqlite3")
    try:
        ledger = AccountLedger(store=store, provider=provider)
        await ledger.run_once()
        # 15:15 结算后：sidecar 报清零的当日盈亏，权益按结算更新。
        provider.equity = 1035207.0
        provider.static_balance = 1035207.0
        provider.close_profit = 0.0
        provider.position_profit = 0.0
        provider.commission = 0.0
        provider.today_net_pnl = 0.0
        await ledger.run_once()
        assert len(store.list_account_daily()) == 1
        row = store.list_account_daily()[0]
        assert (row["tradingDay"], row["equity"], row["staticBalance"]) == ("2026-09-18", 1035207.0, 1035207.0)
        assert (row["closeProfit"], row["positionProfit"], row["commission"], row["todayNetPnl"]) == \
            (-1500.0, -80.0, 222.0, -1802.0)
    finally:
        store.close()


async def test_provider_failure_returns_zeroes_without_flooding_the_log(tmp_path, caplog):
    provider = LedgerProvider()
    provider.activity_error = RuntimeError("sidecar_reconnecting")
    store = RuntimeStore(tmp_path / "failed.sqlite3")
    try:
        ledger = AccountLedger(store=store, provider=provider)
        with caplog.at_level(logging.INFO, logger="uvicorn.error.agent"):
            assert await ledger.run_once() == {"fills": 0, "backfilled": 0, "daily": 0}
            assert await ledger.run_once() == {"fills": 0, "backfilled": 0, "daily": 0}
        failures = [record for record in caplog.records
                    if record.getMessage().startswith("account_ledger_read_failed")]
        assert len(failures) == 1 and failures[0].getMessage().endswith("sidecar_reconnecting")
        assert store.list_account_trades() == [] and store.list_account_daily() == []
    finally:
        store.close()


async def test_fixture_data_is_never_booked(tmp_path):
    provider = LedgerProvider()
    provider.trades = [fill("fixture-open")]
    provider.simulated = True
    store = RuntimeStore(tmp_path / "simulated.sqlite3")
    try:
        ledger = AccountLedger(store=store, provider=provider)
        assert await ledger.run_once() == {"fills": 0, "backfilled": 0, "daily": 0}
        assert store.list_account_trades() == [] and store.list_account_daily() == []
    finally:
        store.close()


async def test_the_loop_runs_and_a_thread_wake_books_within_a_second(tmp_path):
    provider = LedgerProvider()
    provider.trades = [fill("loop-open")]
    store = RuntimeStore(tmp_path / "loop.sqlite3")
    try:
        ledger = AccountLedger(store=store, provider=provider, tick_seconds=60.0, min_interval=0.05)
        original = ledger.run_once
        calls: list[int] = []

        async def counted() -> dict[str, int]:
            calls.append(1)
            return await original()

        ledger.run_once = counted
        await ledger.start()
        try:
            threading.Thread(target=ledger.wake, name="ledger-wake").start()
            deadline = monotonic() + 1.0
            while not store.list_account_trades() and monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert [row["tradeId"] for row in store.list_account_trades()] == ["loop-open"]
            while len(calls) < 2 and monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert len(calls) >= 2  # the wake from another thread scheduled a second pass
        finally:
            await ledger.stop()
        assert ledger._task is None
    finally:
        store.close()


async def test_the_app_runs_the_ledger_but_replay_children_do_not(tmp_path):
    app = create_app(tmp_path / "assembled.sqlite3", local_token=TOKEN, agent_mode="fake", data_mode="fake",
                     engineering_root=tmp_path / "project")
    ledger = app.state.account_ledger
    assert isinstance(ledger, AccountLedger)
    async with app.router.lifespan_context(app):
        assert ledger._task is not None and not ledger._task.done()
    assert ledger._task is None

    source = HistoricalSource()
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command("state")
    replay_app = create_app(tmp_path / "replay.sqlite3", runtime_state_root=tmp_path, agent_mode="fake",
                            data_provider=provider, simulation_gateway=provider, _replay_provider=provider)
    async with replay_app.router.lifespan_context(replay_app):
        assert replay_app.state.account_ledger._task is None
