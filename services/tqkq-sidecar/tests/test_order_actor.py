"""2026-10-12 数据获取审查：排队超时的下单不晚发、会话重建丢掉的下单答“未派发”、
实盘会话合约目录加载失败后同一会话退避重试。"""
from __future__ import annotations

import asyncio
import queue
import time

import pytest

from tqkq_readonly_sidecar.main import (
    ORDER_COMMAND_WAIT_SECONDS,
    ActorCommand,
    OfficialTqKqSession,
    SnapshotService,
)


class OrderSessionStub:
    """The actor's order boundary: records what reached TqSdk."""

    def __init__(self) -> None:
        self.submits: list[str] = []
        self.queries: list[str] = []

    def submit_order_once(self, request):
        self.submits.append(request["clientOrderId"])
        return {"ordersInitialized": True, "found": True, "order": {"status": "ALIVE"}}

    def query_order(self, client_order_id):
        self.queries.append(client_order_id)
        return {"ordersInitialized": True, "found": client_order_id in self.submits}

    def cancel_order_once(self, client_order_id):
        return {"ordersInitialized": True, "found": True, "commandStatus": "CONFIRMED"}


def order_command(kind: str, client_order_id: str, *, waited: float = 0.0) -> ActorCommand:
    command = ActorCommand(kind, f"{kind}:{client_order_id}", client_order_id,
                           {"clientOrderId": client_order_id}, queue.Queue(maxsize=1))
    command.queued_at -= waited
    return command


def run(service: SnapshotService, session, command: ActorCommand) -> dict:
    service._handle_command(session, command)
    return command.response.get_nowait()


def trading_service() -> SnapshotService:
    return SnapshotService(username=None, password=None, symbols=["SHFE.au2612"],
                           fixture=True, trading_enabled=True)


def test_a_submit_whose_caller_gave_up_is_never_sent_and_queries_say_not_dispatched():
    """调用方等不到（gateway_command_timeout，Runtime 记结果未知并按 clientOrderId 回查）
    后才轮到的下单，不再以旧价发出；回查得到确定的“未派发”，重复回查不变；之后从
    “未发送”重新发出的同一单照常下出。撤单不受此限。"""
    service = trading_service()
    session = OrderSessionStub()
    late = run(service, session, order_command("submit", "late-order-1",
                                               waited=ORDER_COMMAND_WAIT_SECONDS + 0.5))
    assert late == {"ordersInitialized": True, "found": False,
                    "commandStatus": "NOT_DISPATCHED", "errorCode": "gateway_command_expired"}
    assert session.submits == []
    for _ in range(2):
        queried = run(service, session, order_command("query", "late-order-1"))
        assert queried == late
    assert session.queries == []  # the actor's own record, not a bare TqSdk "not found"
    # Not expired: sent. The same order sent again from 未发送 goes out too (its commandId is
    # fixed per order, so "not dispatched" must not be cached as "never send").
    resent = run(service, session, order_command("submit", "late-order-1", waited=0.1))
    assert resent["found"] is True and session.submits == ["late-order-1"]
    assert run(service, session, order_command("query", "late-order-1"))["found"] is True
    assert session.queries == ["late-order-1"]
    cancelled = run(service, session, order_command("cancel", "late-order-1",
                                                    waited=ORDER_COMMAND_WAIT_SECONDS + 5))
    assert cancelled["commandStatus"] == "CONFIRMED"  # a late cancel is harmless: it runs


def test_an_already_sent_order_is_never_reported_not_dispatched():
    service = trading_service()
    session = OrderSessionStub()
    run(service, session, order_command("submit", "sent-order-1"))
    late_duplicate = run(service, session, order_command("submit", "sent-order-1",
                                                         waited=ORDER_COMMAND_WAIT_SECONDS + 1))
    assert late_duplicate["found"] is True  # the cached real result, never "not dispatched"
    assert run(service, session, order_command("query", "sent-order-1"))["found"] is True


def test_a_submit_dropped_by_a_session_rebuild_is_reported_not_dispatched():
    service = trading_service()
    submit = order_command("submit", "rebuild-order-1")
    service._commands.put(submit)
    service._begin_rebuild("market_session_stalled", service._generation)
    assert submit.response.get_nowait() == {
        "ordersInitialized": True, "found": False,
        "commandStatus": "NOT_DISPATCHED", "errorCode": "sidecar_reconnecting"}
    session = OrderSessionStub()
    queried = run(service, session, order_command("query", "rebuild-order-1"))
    assert queried["commandStatus"] == "NOT_DISPATCHED" and session.submits == []


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
