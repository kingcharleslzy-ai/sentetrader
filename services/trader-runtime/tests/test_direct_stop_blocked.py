"""保护：直接保护在本快照核对不了原仓（成交读不到/不新鲜/读不懂）时不触发、不作用到重开的新仓 (#528)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_order_protection import Provider, _direct_stop, _fill, _fresh, _long_items, _stop_notices, stop_row
from test_simulation_orders import USER, Gateway, application


class FlakyActivityProvider(Provider):
    """账户活动可按需读失败或变陈旧；行情、持仓、下单都正常。"""

    def __init__(self) -> None:
        super().__init__()
        self.activity_fault: str | None = None

    def account_activity(self) -> dict:
        if self.activity_fault == "unavailable":
            raise RuntimeError("activity timeout")
        activity = super().account_activity()
        if self.activity_fault == "stale":
            return {**activity, "dataStatus": "stale", "isCached": True, "ageSeconds": 90}
        return activity


async def _closed_and_reopened(tmp_path: Path, name: str):
    """原多今 2 手挂直接保护；外部平今 2 再开 2，价格已越过旧止损线。"""
    app = application(tmp_path / name, Gateway(), FlakyActivityProvider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    _fill(provider, "close-1", offset="CLOSETODAY", direction="SELL", volume=2, time=2)
    _fill(provider, "open-2", offset="OPEN", direction="BUY", volume=2, time=3)
    provider.position_items = _long_items(2, today=2)
    _fresh(provider, 820.0)
    return app, provider, gateway, session, stop


@pytest.mark.parametrize(("fault", "reason"), [
    ("unavailable", "activity_unavailable"),
    ("stale", "activity_stale"),
    ("no_trading_day", "fill_untrusted"),
])
async def test_an_unverified_direct_stop_never_fires_and_recovers_onto_the_original(
    tmp_path: Path, fault: str, reason: str,
) -> None:
    app, provider, gateway, session, stop = await _closed_and_reopened(tmp_path, f"blocked-{fault}.sqlite3")
    try:
        if fault == "no_trading_day":
            provider.trades[1]["tradingDay"] = None  # 同合约那笔平仓成交缺交易日
        else:
            provider.activity_fault = fault
        await app.state.autonomy.run_once()
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 0  # 零提交：旧保护不平新仓
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "ARMED" and int(row["remaining_volume"]) == 2
        listed = app.state.autonomy.list_stops(user_id=USER)[0]
        assert listed["lastError"] == f"direct_stop_blocked:{reason}"  # 原因可见

        provider.activity_fault = None  # 可信数据恢复：按原仓核对 —— 撤旧保护，不平新仓
        if fault == "no_trading_day":
            provider.trades[1]["tradingDay"] = provider.day
        await app.state.autonomy.run_once()
        await app.state.autonomy.run_once()
        row = stop_row(app, stop["stopId"])[0]
        assert row["status"] == "CANCELLED" and row["last_error"] == "anchor_position_closed"
        assert gateway.submit_count == 0
        assert len(_stop_notices(app, session.session_id, "没有保护")) == 1  # 只提醒一次
    finally:
        app.state.store.close()


async def test_a_blocked_reason_clears_once_the_original_position_checks_out(tmp_path: Path) -> None:
    """原仓仍在：一轮读不到成交只暂停触发；恢复后原因清掉，触发照常平原仓。"""
    app = application(tmp_path / "blocked-clears.sqlite3", Gateway(), FlakyActivityProvider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        provider.activity_fault = "unavailable"
        _fresh(provider, 820.0)
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 0
        provider.activity_fault = None
        _fresh(provider, 820.0)
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 1 and gateway.request["volume"] == 2
        assert stop_row(app, stop["stopId"])[0]["last_error"] is None
    finally:
        app.state.store.close()


async def test_another_contracts_untrusted_fill_does_not_block_this_stop(tmp_path: Path) -> None:
    app = application(tmp_path / "blocked-other.sqlite3", Gateway(), FlakyActivityProvider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    await _direct_stop(app, session.session_id, volume=2)
    try:
        _fill(provider, "other-1", offset="OPEN", direction="BUY", volume=1)
        provider.trades[0].update(symbol="SHFE.ag2612", tradingDay=None)
        _fresh(provider, 820.0)
        await app.state.autonomy.run_once()
        assert gateway.submit_count == 1 and gateway.request["volume"] == 2
    finally:
        app.state.store.close()


async def test_a_submitted_close_is_still_read_back_while_activity_is_unreadable(tmp_path: Path) -> None:
    app = application(tmp_path / "blocked-readback.sqlite3", Gateway(), FlakyActivityProvider())
    provider, gateway = app.state.data_provider, app.state.simulation_orders.gateway
    provider.position_items = _long_items(2, today=2)
    session = app.state.broker.create_session("local", "native", USER)
    stop = await _direct_stop(app, session.session_id, volume=2)
    try:
        _fresh(provider, 820.0)
        await app.state.autonomy.run_once()  # 平仓单已发出
        assert gateway.submit_count == 1 and stop_row(app, stop["stopId"])[0]["status"] == "SUBMITTED"
        provider.activity_fault = "unavailable"
        gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
        with app.state.store.lock, app.state.store.connection:
            app.state.store.connection.execute(
                "UPDATE ai_stop_orders SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (stop["stopId"],))
        await app.state.autonomy.run_once()
        assert stop_row(app, stop["stopId"])[0]["status"] == "EXECUTED"  # 原单回读不受影响
    finally:
        app.state.store.close()
