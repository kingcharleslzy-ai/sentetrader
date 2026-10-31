"""Order recovery through actual replay HTTP/tool boundaries, without a sidecar."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
from test_simulation_orders import USER, Gateway, confirm, proposal
from trader_runtime.main import create_app
from trader_runtime.trader_tools import TraderReadOnlyTools

ORIGIN = "http://127.0.0.1:5173"


class RecoverySource(HistoricalSource):
    def __init__(self):
        super().__init__()
        self.block_advance = False
        self.advance_entered = asyncio.Event()
        self.advance_release = asyncio.Event()
        self.query_entered = asyncio.Event()
        self.block_query = False
        self.query_release = asyncio.Event()
        self.initialized = True
        self.queries = []
        self.cancels = []

    async def _request_async(self, method, path, payload=None, **kwargs):
        action = path.rsplit("/", 1)[-1]
        if action == "advance" and self.block_advance:
            self.advance_entered.set()
            await self.advance_release.wait()
        if action == "query":
            self.queries.append(payload["clientOrderId"])
            self.query_entered.set()
            if self.block_query:
                await self.query_release.wait()
            if not self.initialized:
                return {"ordersInitialized": False, "found": False}
        if action == "cancel":
            identifier = payload["clientOrderId"]
            self.cancels.append(identifier)
            self.reports[identifier] = Gateway.report(self.submits[0], status="FINISHED")
            return self.reports[identifier]
        return await super()._request_async(method, path, payload, **kwargs)

    async def stop(self):
        await self._http.close()


class ObservedLock(asyncio.Lock):
    def __init__(self):
        super().__init__()
        self.queued = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.queued.set()
        return await super().acquire()


@pytest.fixture
async def replay_orders(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external HTTP")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    source = RecoverySource()
    app = create_app(tmp_path / "root.sqlite3", runtime_state_root=tmp_path,
        data_provider=source, agent_mode="fake", local_token="test-replay-token")
    headers = {"Origin": ORIGIN, "Authorization": "Bearer test-replay-token"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
            base_url="http://runtime", headers=headers) as client:
            created = await client.post("/api/replays", json={
                "symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
            assert created.status_code == 201, created.text
            context = app.state.replay_router.owned(IDENTIFIER, USER)
            # Exercise steps explicitly so the passive heartbeat cannot consume
            # a test barrier or contend for the observed operation lock.
            context.task.cancel()
            await asyncio.gather(context.task, return_exceptions=True)
            context.task = None
            context.lock = ObservedLock()
            control = f"/api/replays/{IDENTIFIER}/control"
            assert (await client.post(control, json={"action": "step"})).status_code == 200
            child = context.child
            result = await confirm(child, proposal(child))
            identifier = result["clientOrderId"]
            assert result["simulationOrder"]["submitPhase"] == "UNKNOWN"
            with child.state.store.transaction() as connection:
                child.state.simulation_orders.request_cancel(identifier, user_id=USER, connection=connection)
            agents = {}
            for name, selected in (("root", app), ("child", child)):
                tools = TraderReadOnlyTools(selected.state.data_provider,
                    selected.state.adapter.runtime_status, autonomy=selected.state.autonomy)
                tools.app_capabilities = selected.state.app_capabilities
                if selected is child:
                    tools.replay_context = context
                selected.state.adapter.trader_tools = tools
                session = selected.state.broker.create_session("local", "native", USER)
                tools.app_capabilities.bind_session(session.session_id, USER,
                    dict(client.build_request("GET", "/").headers))
                await tools.app_capabilities.capture_identity(USER, session_id=session.session_id).verify()
                agents[name] = (tools, session)

            async def call_agent(name, *, native=False):
                tools, session = agents[name]
                operation = {"operation": "GET /api/simulation/orders", "query": {"recoveryWait": False}}
                if name == "root":
                    operation["replayId"] = IDENTIFIER
                result, _ = await tools.call_async(
                    tool="query_simulation_order" if native else "app_call",
                    arguments={"clientOrderId": identifier} if native else {"requestJson": json.dumps(operation)},
                    turn_id="recovery-boundary", mode="local", session_id=session.session_id, user_id=USER)
                return result

            try:
                yield SimpleNamespace(source=source, app=app, context=context, child=child,
                    client=client, prefix=created.json()["runtimePath"], control=control,
                    identifier=identifier, agents=agents, call_agent=call_agent)
            finally:
                source.advance_release.set()
                source.query_release.set()


@pytest.mark.parametrize("entry", ["list-rest", "root-agent", "child-agent", "query-post", "query-native"])
async def test_order_recovery_waits_for_replay_step_and_keeps_original_identity(replay_orders, entry):
    r = replay_orders
    r.source.block_advance = True
    step = asyncio.create_task(r.client.post(r.control, json={"action": "step"}))
    await asyncio.wait_for(r.source.advance_entered.wait(), 1)
    if entry == "list-rest":
        call = r.client.get(r.prefix + "/api/simulation/orders?recoveryWait=false")
    elif entry == "query-post":
        call = r.client.post(r.prefix + f"/api/simulation/orders/{r.identifier}/query")
    else:
        call = r.call_agent("root" if entry == "root-agent" else "child", native=entry == "query-native")
    pending = asyncio.create_task(call)
    try:
        await asyncio.wait_for(r.context.lock.queued.wait(), 1)
        assert not r.source.queries and not r.source.cancels
        assert r.child.state.simulation_orders.get(r.identifier, user_id=USER)["submitPhase"] == "UNKNOWN"
    finally:
        r.source.advance_release.set()
    assert (await asyncio.wait_for(step, 2)).status_code == 200
    response = await asyncio.wait_for(pending, 2)
    assert response["success"] if isinstance(response, dict) else response.status_code == 200
    assert r.source.cancels == [r.identifier]
    assert set(r.source.queries) == {r.identifier}
    assert len(r.source.submits) == 1
    order = r.child.state.simulation_orders.get(r.identifier, user_id=USER)
    assert order["clientOrderId"] == r.identifier and order["displayStatus"] == "已撤销"
    if entry in {"root-agent", "child-agent"}:
        payload = json.loads(response["contentItems"][0]["text"])
        assert payload["readOnly"] is False and payload["executionEnabled"] is True
    manifest = r.child.state.app_capabilities.manifest(user=USER)
    operation = next(item for item in manifest["operations"] if item["operation"] == "GET /api/simulation/orders")
    assert operation["readOnly"] is False


async def test_recovery_poll_wait_releases_lock_for_replay_control(replay_orders):
    r = replay_orders
    r.source.initialized = False
    pending = asyncio.create_task(r.client.get(r.prefix + "/api/simulation/orders"))
    try:
        await asyncio.wait_for(r.source.query_entered.wait(), 1)
        response = await asyncio.wait_for(r.client.post(r.control, json={"action": "pause"}), .5)
        assert response.status_code == 200 and not pending.done()
        assert not r.source.cancels
        r.source.initialized = True
        response = await asyncio.wait_for(pending, 2)
        assert response.status_code == 200
        assert response.json()["items"][0]["displayStatus"] == "已撤销"
        assert r.source.cancels == [r.identifier] and len(r.source.submits) == 1
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cancelled_list_keeps_lock_until_original_recovery_is_saved(replay_orders):
    r = replay_orders
    r.source.block_query = True
    pending = asyncio.create_task(r.client.get(r.prefix + "/api/simulation/orders?recoveryWait=false"))
    await asyncio.wait_for(r.source.query_entered.wait(), 1)
    pending.cancel()
    contender = asyncio.create_task(r.client.post(r.control, json={"action": "pause"}))
    try:
        await asyncio.wait_for(r.context.lock.queued.wait(), 1)
        assert not contender.done() and not r.source.cancels
        assert r.child.state.simulation_orders.get(r.identifier, user_id=USER)["submitPhase"] == "UNKNOWN"
    finally:
        r.source.query_release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await asyncio.wait_for(contender, 2)).status_code == 200
    assert r.source.cancels == [r.identifier] and len(r.source.submits) == 1
    assert r.child.state.simulation_orders.get(r.identifier, user_id=USER)["displayStatus"] == "已撤销"


@pytest.mark.parametrize("initialized", [True, False])
async def test_list_returns_saved_snapshot_when_http_close_finishes_before_waiter_resumes(replay_orders, monkeypatch, initialized):
    r = replay_orders
    r.source.initialized = initialized
    completed, release = asyncio.Event(), asyncio.Event()
    original = r.context.call_tool

    async def deliver_after_close(user_id, call, *, authorize=None):
        result = await original(user_id, call, authorize=authorize)
        # Model a valid scheduling order: the operation released its lock, but
        # its HTTP waiter has not yet resumed to deliver the completed result.
        completed.set()
        await release.wait()
        return result

    monkeypatch.setattr(r.context, "call_tool", deliver_after_close)
    pending = asyncio.create_task(r.client.get(r.prefix + "/api/simulation/orders?recoveryWait=false"))
    try:
        await asyncio.wait_for(completed.wait(), 1)
        closed = await asyncio.wait_for(r.client.post(r.control, json={"action": "close"}), 2)
        assert closed.status_code == 200 and closed.json() == {"status": "closed"}
        assert r.context.closed and IDENTIFIER not in r.app.state.replay_router.contexts
    finally:
        release.set()
    response = await asyncio.wait_for(pending, 2)
    assert response.status_code == 200
    order = response.json()["items"][0]
    assert order["clientOrderId"] == r.identifier
    assert order["displayStatus"] == ("已撤销" if initialized else "结果未知")
    assert r.source.queries == [r.identifier] and len(r.source.submits) == 1
    assert r.source.cancels == ([r.identifier] if initialized else [])


@pytest.mark.parametrize("agent", ["root", "child"])
@pytest.mark.parametrize("revocation", ["session", "close"])
async def test_queued_agent_list_rechecks_original_session_and_replay(replay_orders, agent, revocation):
    r = replay_orders
    await r.context.lock.acquire()
    pending = asyncio.create_task(r.call_agent(agent))
    try:
        await asyncio.wait_for(r.context.lock.queued.wait(), 1)
        if revocation == "session":
            tools, session = r.agents[agent]
            tools.app_capabilities.forget_session(session.session_id)
        else:
            r.context.closing = True
    finally:
        r.context.lock.release()
    result = await asyncio.wait_for(pending, 1)
    assert not result["success"]
    assert not r.source.queries and not r.source.cancels
    assert r.child.state.simulation_orders.get(r.identifier, user_id=USER)["submitPhase"] == "UNKNOWN"
