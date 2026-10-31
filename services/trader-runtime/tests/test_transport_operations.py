import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from test_simulation_orders import USER, Gateway, application, confirm, proposal
from trader_runtime.data_providers import SidecarReadOnlyDataProvider
from trader_runtime.operations import owned_operation
from trader_runtime.replay import ReplayContext
from trader_runtime.simulation_orders import SimulationOrderError


@pytest.mark.parametrize("cancel", ["once", "repeated", "timeout"])
@pytest.mark.parametrize("outcome", ["accepted", "lost"])
async def test_cancelled_replay_order_keeps_lock_until_http_result_is_saved(tmp_path, cancel, outcome):
    gateway = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:19999", token="offline-fixture-" * 3, symbols=[])
    entered, release, next_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []
    async def transport(request):
        assert request.method == "POST" and request.url.path.endswith("submit")
        payload = json.loads(request.content)
        requests.append(payload)
        entered.set()
        await release.wait()
        if outcome == "lost":
            raise httpx.ReadTimeout("offline lost reply", request=request)
        return httpx.Response(200, json=Gateway.report(payload))
    await gateway._http.async_client.aclose()
    gateway._http.async_client = httpx.AsyncClient(base_url=gateway._base_url, transport=httpx.MockTransport(transport))
    app = application(tmp_path / "orders.sqlite3", gateway)
    context = ReplayContext(USER, app, SimpleNamespace())
    item = proposal(app)
    owner = asyncio.create_task(context.call_tool(USER, lambda: confirm(app, item)))
    await asyncio.wait_for(entered.wait(), 1)
    identifier = requests[0]["clientOrderId"]
    def saved():
        return app.state.simulation_orders.get(identifier, user_id=USER)
    assert saved()["submitPhase"] == "CALL_STARTED"
    async def next_step():
        assert saved()["submitPhase"] == ("UNKNOWN" if outcome == "lost" else "CONFIRMED")
        next_entered.set()
    contender = asyncio.create_task(context.call_tool(USER, next_step))
    if cancel == "timeout":
        waiter = asyncio.create_task(asyncio.wait_for(owner, .01))
        while not owner.cancelling():
            await asyncio.sleep(.001)
    else:
        waiter = owner
        for _ in range(4 if cancel == "repeated" else 1):
            owner.cancel()
            await asyncio.sleep(0)
    assert not next_entered.is_set() and context.lock.locked()
    assert not waiter.done()
    release.set()
    with pytest.raises(TimeoutError if cancel == "timeout" else asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(contender, 1)
    assert saved()["clientOrderId"] == identifier
    with pytest.raises(SimulationOrderError, match="order_not_prepared"):
        await app.state.simulation_orders.dispatch(identifier, user_id=USER)
    assert len(requests) == 1
    assert not gateway._http.async_client.is_closed
    await gateway.stop()
    assert gateway._http.async_client.is_closed and gateway._http.sync.is_closed
    app.state.store.close()


async def test_cancel_before_owner_first_runs_respects_committed_order_boundary():
    completed = []
    async def persist():
        completed.append("saved")
    async def call():
        # Same event-loop boundary as persisted CALL_STARTED before ownership.
        asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        await owned_operation(persist, committed=True)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(call())
    assert completed == ["saved"]


async def test_queued_operation_is_cancelled_without_running():
    lock = asyncio.Lock()
    await lock.acquire()
    entered = []
    async def call():
        entered.append(True)
    pending = asyncio.create_task(owned_operation(call, lock=lock))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    lock.release()
    assert not entered


async def test_runtime_closes_pools_after_adapter_business_finishes(tmp_path):
    gateway = Gateway()
    app = application(tmp_path / "lifecycle.sqlite3", gateway)
    http = SidecarReadOnlyDataProvider(base_url="http://127.0.0.1:19999", token="offline-fixture-" * 3, symbols=[])._http
    order = []
    async def finish_adapter():
        assert not http.async_client.is_closed
        order.append("business-finished")
    async def stop_provider():
        assert order == ["business-finished"]
        await http.close()
        order.append("pool-closed")
    app.state.adapter.shutdown = finish_adapter
    app.state.data_provider.stop = stop_provider
    async with app.router.lifespan_context(app):
        pass
    assert order == ["business-finished", "pool-closed"]
