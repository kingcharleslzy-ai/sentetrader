"""2026-10-12 数据获取审查：实盘会话合约目录加载失败后同一会话退避重试。"""
from __future__ import annotations

import asyncio
import time

import pytest

from tqkq_readonly_sidecar.main import (
    OfficialTqKqSession,
    SnapshotService,
)


class CatalogApi:
    def __init__(self) -> None:
        self.fail = True
        self.tasks: list = []

    async def query_quotes(self, **kwargs):
        if self.fail:
            raise TimeoutError("catalog download timed out")
        return ["SHFE.au2612", "SHFE.rb2701"]

    async def query_cont_quotes(self):
        return ["SHFE.rb2701"]

    async def query_symbol_info(self, symbols):
        rows = [{"instrument_id": symbol, "instrument_name": symbol, "exchange_id": "SHFE",
                 "product_id": symbol.split(".")[1][:2], "expired": False, "price_tick": 1,
                 "volume_multiple": 10, "delivery_year": 2026, "delivery_month": 12,
                 "expire_rest_days": 60, "trading_time_day": [["09:00:00", "15:00:00"]],
                 "trading_time_night": []} for symbol in symbols]
        return type("Frame", (), {"to_dict": lambda self, orient: rows})()

    def create_task(self, coroutine):
        self.tasks.append(coroutine)
        return coroutine

    def wait_update(self, deadline):
        return True


def test_live_catalog_load_failure_is_retried_on_the_same_session():
    """首次加载失败：同一会话按退避重试，期间配置外的合约答 instrument_catalog_not_ready
    （不是 symbol_not_found），重试成功后可用；加载成功后不再重复取。"""
    session = object.__new__(OfficialTqKqSession)
    session.api = CatalogApi()
    session.quotes = {"SHFE.au2612": object()}
    session._catalog_symbols = {"SHFE.AU2612": "SHFE.au2612"}
    session._instrument_catalog = None
    session._instrument_catalog_error = None
    session.catalog_pending = True
    session._catalog_task = None
    session.commands_waiting = None
    session.snapshot = lambda: object()

    session.read(0.01)
    asyncio.run(session.api.tasks[-1])
    assert session._instrument_catalog_error == "tqkq_instrument_catalog_unavailable"
    assert session.catalog_pending is True  # a retry is scheduled: the service keeps polling
    assert session._resolve_symbol("SHFE.au2612") == "SHFE.au2612"
    with pytest.raises(RuntimeError, match="instrument_catalog_not_ready"):
        session._resolve_symbol("SHFE.rb2701")
    session.read(0.01)
    assert len(session.api.tasks) == 1  # backoff not over: no new attempt
    session._catalog_retry_at = time.monotonic() - 1
    session.api.fail = False
    session.read(0.01)
    assert len(session.api.tasks) == 2
    asyncio.run(session.api.tasks[-1])
    assert session._instrument_catalog_error is None and session.catalog_pending is False
    assert session._resolve_symbol("shfe.rb2701") == "SHFE.rb2701"
    with pytest.raises(KeyError):
        session._resolve_symbol("SHFE.zz9999")  # catalogue loaded: unknown means not found
    session.read(0.01)
    assert len(session.api.tasks) == 2


def test_service_reports_a_partial_catalogue_as_not_ready_for_other_contracts():
    service = SnapshotService(username="u", password="p", symbols=["SHFE.au2612"])
    with service._lock:
        service._instruments = [{"symbol": "SHFE.au2612"}]
        service._instrument_catalog_error = "tqkq_instrument_catalog_unavailable"
        service._instrument_catalog_pending = True
    assert service.status()["instrumentCatalogStatus"] == "partial"
    with pytest.raises(RuntimeError, match="instrument_catalog_not_ready"):
        service.market_history("SHFE.rb2701", 60, 10)
    service._begin_rebuild("market_session_stalled", service._generation)
    assert service._instrument_catalog_pending is True  # the next session's catalogue is taken
