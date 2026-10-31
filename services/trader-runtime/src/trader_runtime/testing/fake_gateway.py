"""An in-process simulation gateway for browser tests.

It stands in for the TqKq sidecar's order channel so the e2e suite can drive
the real activate / prepare / dispatch / stop chain instead of mocking the
polled endpoints. Every accepted order fills at its limit price on the next
query; nothing here touches a real account.
"""
from __future__ import annotations

from typing import Any

ENV_FLAG = "AI_TRADER_FAKE_SIMULATION_GATEWAY"


class FakeSimulationGateway:
    """Deterministic order channel: submit → ALIVE, first query → FINISHED, cancel → FINISHED."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.submit_count = 0

    @staticmethod
    def _report(request: dict[str, Any], *, status: str, volume_left: int) -> dict[str, Any]:
        return {"ordersInitialized": True, "found": True, "order": {
            "clientOrderId": request["clientOrderId"],
            "exchangeOrderId": f"fake-{request['clientOrderId'][:8]}",
            "symbol": request["symbol"],
            "direction": request["direction"],
            "offset": request["offset"],
            "priceType": "LIMIT",
            "volume": request["volume"],
            "volumeLeft": volume_left,
            "limitPrice": request["limitPrice"],
            "status": status,
            "isOnline": status == "ALIVE",
            "isDead": status == "FINISHED",
            "isError": False,
        }}

    async def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]:
        self.submit_count += 1
        self.orders[request["clientOrderId"]] = dict(request)
        return self._report(request, status="ALIVE", volume_left=int(request["volume"]))

    async def query_order(self, client_order_id: str) -> dict[str, Any]:
        request = self.orders.get(client_order_id)
        if request is None:
            return {"ordersInitialized": True, "found": False}
        return self._report(request, status="FINISHED", volume_left=0)

    async def cancel_order_once(self, client_order_id: str, command_id: str) -> dict[str, Any]:
        request = self.orders.get(client_order_id)
        if request is None:
            return {"ordersInitialized": True, "found": False}
        return self._report(request, status="FINISHED", volume_left=int(request["volume"]))
