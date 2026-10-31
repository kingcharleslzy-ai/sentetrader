"""Exercise the real host/tool/REST boundary without starting a model or account."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from test_cloud_isolation import ORIGIN as CLOUD_ORIGIN
from test_cloud_isolation import SECRET, cloud_client
from test_owner_auth import ORIGIN, PASSWORD, owner_app  # noqa: F401  # pytest fixture
from trader_runtime.auth import OWNER_COOKIE_NAME
from trader_runtime.main import create_app
from trader_runtime.replay import ReplayContext
from trader_runtime.trader_tools import TOOL_ARGUMENT_MODELS, TraderReadOnlyTools


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external HTTP")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


async def invoke(tools, session, tool="get_positions", arguments=None):
    result, _ = await tools.call_async(tool=tool, arguments=arguments or {},
        turn_id="offline-review", mode=session.mode,
        session_id=session.session_id, user_id=session.user_id)
    return result


async def test_cloud_jwt_late_adapters_discover_and_dispatch_as_their_own_user(tmp_path):
    template = tmp_path / "template"
    for relative in ("workspace/strategies/rb_trend.py", "workspace/backtests/fixtures/rb_monthly_v1.csv"):
        destination = template / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((Path(__file__).resolve().parents[3] / relative).read_bytes())
    app = create_app(tmp_path / "cloud.sqlite3", runtime_state_root=tmp_path / "state",
        engineering_root=template, agent_mode="native", auth_mode="cloud-jwt",
        data_mode="fake", cloud_jwt_secret=SECRET, cors_origins={CLOUD_ORIGIN})
    bridge = app.state.app_capabilities
    clients, sessions, adapters = [], [], []
    try:
        assert not app.state.adapter.adapters
        for subject in ("alice", "bob"):
            client = await cloud_client(app, tenant="tenant-a", subject=subject)
            clients.append(client)
            user = app.state.auth.validate(app.state.auth.issue_for_test(
                tenant_id="tenant-a", subject=subject)).scoped_user_id
            # Discovery itself may construct the user adapter, but never starts it.
            manifest = await client.get("/api/agent/capabilities")
            assert manifest.status_code == 200
            assert "tool:get_positions" in {item["operation"] for item in manifest.json()["operations"]}
            adapter = app.state.adapter.adapter_for_user(user)
            adapters.append(adapter)
            assert adapter.trader_tools.app_capabilities is bridge
            assert adapter.adapter_id == "native-agent"
            session = app.state.broker.create_session("cloud", "native", user)
            sessions.append(session)
            headers = dict(client.build_request("GET", "/").headers)
            bridge.bind_session(session.session_id, user, headers)
            assert (await invoke(adapter.trader_tools, session))["success"]
            assert (await invoke(adapter.trader_tools, session, "app_capabilities"))["success"]
            proxy = await invoke(adapter.trader_tools, session, "app_call",
                {"requestJson": json.dumps({"operation": "GET /api/app/config"})})
            assert proxy["success"]
            native = await client.post("/api/agent/call", json={"operation": "tool:get_positions"})
            assert native.json()["ok"]
        assert adapters[0] is not adapters[1]
        assert not (await invoke(adapters[0].trader_tools,
            SimpleNamespace(**{**vars(sessions[0]), "user_id": sessions[1].user_id})))["success"]
        bridge.forget_user(sessions[0].user_id)
        assert not (await invoke(adapters[0].trader_tools, sessions[0]))["success"]
        assert (await invoke(adapters[1].trader_tools, sessions[1]))["success"]
    finally:
        for client in clients:
            await client.aclose()
        await app.state.adapter.shutdown()
        app.state.store.close()


@pytest.mark.parametrize("boundary", ["native", "rest", "root-replay", "replay-native"])
@pytest.mark.parametrize("revocation", ["expiry", "logout", "delete", "queued"])
async def test_interactive_identity_is_rechecked_at_dispatch(owner_app, boundary, revocation):
    app, bridge = owner_app, owner_app.state.app_capabilities
    tools = TraderReadOnlyTools(app.state.data_provider, app.state.adapter.runtime_status)
    tools.app_capabilities = bridge
    app.state.adapter.trader_tools = tools
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
        base_url="https://runtime.example.test", headers={"Origin": ORIGIN}) as client:
        assert (await client.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})).status_code == 200
        cookie = client.cookies.get(OWNER_COOKIE_NAME)
        user = app.state.auth.validate(cookie).scoped_user_id
        session = app.state.broker.create_session("cloud", "native", user)
        bridge.bind_session(session.session_id, user, dict(client.build_request("GET", "/").headers))
        identifier = "b" * 32
        if boundary in {"root-replay", "replay-native"}:
            context = ReplayContext(user, app, SimpleNamespace())
            app.state.replay_router = SimpleNamespace(owned=lambda replay_id, owner: context)

        async def call():
            if boundary in {"native", "replay-native"}:
                return await invoke(tools, session)
            return await invoke(tools, session, "app_call", {"requestJson": json.dumps({
                "operation": "PUT /api/app/config", "body": {"revision": 0, "config": {"showAiActivity": False}},
                **({"replayId": identifier} if boundary == "root-replay" else {})})})

        if revocation == "queued":
            # A real dispatch check pauses after the identity was captured.
            entered, release = asyncio.Event(), asyncio.Event()
            original = bridge.current_user
            async def delayed_auth(request):
                entered.set()
                await release.wait()
                return await original(request)
            bridge.current_user = delayed_auth
            pending = asyncio.create_task(call())
            await asyncio.wait_for(entered.wait(), 1)
            bridge.forget_user(user)
            release.set()
            result = await pending
        else:
            if revocation == "expiry":
                from unittest.mock import patch

                import trader_runtime.auth as auth_module
                with patch.object(auth_module.time, "time", return_value=auth_module.time.time() + auth_module.OWNER_SESSION_SECONDS + 86400):
                    result = await call()
            elif revocation == "logout":
                assert (await client.post("/api/auth/owner/logout")).status_code == 200
                result = await call()
            else:
                assert (await client.delete(f"/api/sessions/{session.session_id}")).status_code == 200
                result = await call()
        assert not result["success"]
        # No rejected config mutation reached the actual storage service.
        assert (await client.get("/api/app/config")).status_code in {200, 401}
        assert not list(app.state.market_briefs.settings.root.glob("*.json"))


async def test_tool_schemas_validate_the_registered_call_arguments(owner_app):
    from pydantic import ValidationError
    tools = TraderReadOnlyTools(owner_app.state.data_provider, owner_app.state.adapter.runtime_status,
        backtests=owner_app.state.backtest_service, proposals=owner_app.state.proposals, autonomy=owner_app.state.autonomy)
    tools.app_capabilities = owner_app.state.app_capabilities
    for definition in tools.definitions():
        model = TOOL_ARGUMENT_MODELS[definition["name"]]
        assert definition["inputSchema"] == model.model_json_schema()
        with pytest.raises(ValidationError):
            model.model_validate({"unrecognized_execution_permission": True})
    result, _ = tools.call(tool="get_market_analysis", arguments={"symbol": "SHFE.au2612", "lookbackBars": 999},
        turn_id="invalid-arguments", mode="cloud")
    assert not result["success"]


@pytest.mark.parametrize("entry", ["root-rest", "root-native", "child-rest", "child-native"])
@pytest.mark.parametrize("revocation", ["logout", "delete", "close"])
async def test_real_replay_queue_rechecks_identity_and_close_state(tmp_path, entry, revocation):
    from test_owner_auth import PASSWORD_HASH, SESSION_SECRET
    from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
    class ObservedLock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.queued = asyncio.Event()
        async def acquire(self):
            if self.locked():
                self.queued.set()
            return await super().acquire()

    source = HistoricalSource()
    app = create_app(tmp_path / "root.sqlite3", runtime_state_root=tmp_path,
        agent_mode="fake", data_provider=source, auth_mode="owner-password",
        owner_username="charles", owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
            base_url="https://runtime.example.test", headers={"Origin": ORIGIN}) as client:
            await client.post("/api/auth/owner", json={"username": "charles", "password": PASSWORD})
            user = app.state.auth.validate(client.cookies.get(OWNER_COOKIE_NAME)).scoped_user_id
            created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1000000})
            assert created.status_code == 201
            context = app.state.replay_router.owned(IDENTIFIER, user)
            context.lock = ObservedLock()
            selected = app if entry.startswith("root") else context.child
            tools = TraderReadOnlyTools(selected.state.data_provider, selected.state.adapter.runtime_status,
                autonomy=selected.state.autonomy)
            tools.app_capabilities = selected.state.app_capabilities
            selected.state.adapter.trader_tools = tools
            if selected is context.child:
                tools.replay_context = context
            else:
                child_tools = TraderReadOnlyTools(context.child.state.data_provider, context.child.state.adapter.runtime_status,
                    autonomy=context.child.state.autonomy)
                context.child.state.adapter.trader_tools = child_tools
                child_tools.app_capabilities = context.child.state.app_capabilities
                child_tools.replay_context = context
            session = selected.state.broker.create_session("local", "native", user)
            selected.state.app_capabilities.bind_session(session.session_id, user, dict(client.build_request("GET", "/").headers))
            operation = {"operation": "tool:stop_simulation_execution", "body": {"emergency": True}} if entry.endswith("native") else {
                "operation": "PUT /api/app/config", "body": {"revision": 0, "config": {"showAiActivity": False}}}
            if selected is app:
                operation["replayId"] = IDENTIFIER
            await context.lock.acquire()
            pending = asyncio.create_task(invoke(tools, session, "app_call", {"requestJson": json.dumps(operation)}))
            try:
                await asyncio.wait_for(context.lock.queued.wait(), 1)
                if revocation == "logout":
                    assert (await client.post("/api/auth/owner/logout")).status_code == 200
                elif revocation == "delete":
                    # Session deletion uses the same replay queue, so revoke its
                    # host binding at the entry point before releasing that queue.
                    selected.state.app_capabilities.forget_session(session.session_id)
                else:
                    context.closing = True
            finally:
                context.lock.release()
            result = await asyncio.wait_for(pending, 1)
            assert not result["success"]
            assert not list(context.child.state.market_briefs.settings.root.glob("*.json"))
            assert context.child.state.autonomy.current(user_id=user) is None


@pytest.mark.parametrize('replay', [False, True])
async def test_ui_receipt_wait_allows_pulse_and_preserves_command_identity(tmp_path, replay):
    from uuid import uuid4

    from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
    from trader_runtime.ui_control import ChartOptions

    source = HistoricalSource()
    app = create_app(tmp_path / 'runtime.sqlite3', runtime_state_root=tmp_path,
        data_provider=source, data_mode='tqkq-sidecar', agent_mode='fake', local_token='ui-test-token')
    headers = {'Authorization': 'Bearer ui-test-token', 'Origin': 'http://127.0.0.1:5173'}
    state = dict(symbol=SYMBOL, duration=60, view='trade', panel=None, settingsOpen=False,
        theme='dark', dockSide='right', dockWidth=400, replay=replay, settingsPage='connection',
        fontScale='standard', density='standard', chart={k: True for k in ChartOptions.model_fields},
        chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    observation = dict(capturedAt='2026-09-08T00:00:00Z', viewport=dict(width=1280, height=720),
        text='交易台', truncated=False, regions=[], controls=[], context={})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://runtime', headers=headers) as c:
            prefix = ''
            if replay:
                created = await c.post('/api/replays', json={'symbol': SYMBOL,
                    'tradingDay': '2026-09-04', 'initialBalance': 1000000})
                assert created.status_code == 201
                prefix = created.json()['runtimePath']
            wid, cid = str(uuid4()), str(uuid4())
            pulse = dict(windowId=wid, revision=0, state=state, observation=observation)
            assert (await c.post(prefix + '/api/ui/pulse', json=pulse)).status_code == 200
            body = dict(commandId=cid, windowId=wid, expectedRevision=0, changes={'theme': 'light'})
            call = dict(operation='POST /api/app/ui/commands', body=body, query={'waitMs': 1000})
            if replay:
                call['replayId'] = IDENTIFIER
            pending = asyncio.create_task(c.post('/api/agent/call', json=call))
            try:
                for _ in range(50):
                    await asyncio.sleep(.01)
                    dispatched = (await c.post(prefix + '/api/ui/pulse', json=pulse)).json()['command']
                    if dispatched: break
                assert dispatched['commandId'] == cid
                assert not pending.done()  # Dispatch is not an acknowledgement.
                acknowledged = {**pulse, 'revision': 1, 'state': {**state, 'theme': 'light'},
                    'receipt': {'commandId': cid, 'status': 'applied'}}
                assert (await c.post(prefix + '/api/ui/pulse', json=acknowledged)).status_code == 200
                result = (await asyncio.wait_for(pending, 2)).json()
                assert result['ok'] and result['data']['status'] == 'applied'
                assert result['data']['observedRevision'] == 1
                assert result['data']['state']['theme'] == 'light'
                repeated = (await c.post('/api/agent/call', json={**call, 'query': {'waitMs': 0}})).json()
                assert repeated['data'] == result['data']
                assert (await c.post(prefix + '/api/ui/pulse', json=acknowledged)).json()['command'] is None
                # A bounded wait expiring does not imply execution or permit duplication.
                second = {**body, 'commandId': str(uuid4()), 'expectedRevision': 1, 'changes': {'theme': 'dark'}}
                timeout = (await c.post('/api/agent/call', json={**call, 'body': second, 'query': {'waitMs': 1}})).json()
                assert timeout['data']['status'] == 'queued' and timeout['data']['observedRevision'] is None
                assert (await c.post(prefix + '/api/ui/pulse', json={**acknowledged, 'closing': True})).status_code == 200
                stopped = (await c.post('/api/agent/call', json={**call, 'body': second, 'query': {'waitMs': 1}})).json()
                assert stopped['data']['status'] == 'expired'
                assert (await c.post(prefix + '/api/ui/pulse', json=acknowledged)).status_code == 410
                # Revoke a real host session while its command waits for a UI
                # receipt. The wait must end without dispatching an old identity.
                from trader_runtime.agent_api import AppCall
                fresh = {**pulse, 'windowId': str(uuid4())}
                assert (await c.post(prefix + '/api/ui/pulse', json=fresh)).status_code == 200
                session = (await c.post('/api/sessions', json={'mode': 'local'})).json()
                sid = session['sessionId']
                revoked_body = {**body, 'windowId': fresh['windowId'], 'commandId': str(uuid4())}
                waiting = asyncio.create_task(app.state.app_capabilities.call(
                    AppCall.model_validate({**call, 'body': revoked_body}), user='local-user', session_id=sid))
                for _ in range(50):
                    response = await c.get(prefix + '/api/app/ui/commands/' + revoked_body['commandId'])
                    if response.status_code == 200: break
                    await asyncio.sleep(.01)
                assert response.status_code == 200 and not waiting.done()
                assert (await c.delete('/api/sessions/' + sid)).status_code == 200
                denied = await asyncio.wait_for(waiting, 2)
                assert denied['status'] == 401
                assert (await c.post(prefix + '/api/ui/pulse', json=fresh)).json()['command'] is None
                rejected = (await c.get(prefix + '/api/app/ui/commands/' + revoked_body['commandId'])).json()
                assert rejected['status'] == 'rejected'
            finally:
                if not pending.done(): pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                if replay:
                    assert (await c.post(f'/api/replays/{IDENTIFIER}/control', json={'action': 'close'})).status_code == 200


async def test_capability_names_are_identifiers_and_tool_entries_carry_descriptions(tmp_path: Path) -> None:
    """Agents index the catalog by name; a `tool:*` entry used to expose its Chinese description there."""
    import re

    local_origin = "http://127.0.0.1:5173"
    app = create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake", cors_origins={local_origin})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": local_origin}) as client:
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer token"})).status_code == 200
            manifest = (await client.get("/api/agent/capabilities")).json()
    entries = manifest["operations"]
    assert entries
    for item in entries:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", item["name"]), item
        assert isinstance(item["deprecated"], bool), item
        if item["operation"].startswith("tool:"):
            assert item["name"] == item["operation"].removeprefix("tool:")
            assert item["description"].strip(), item


async def test_visible_row_quotes_are_a_bounded_member_read(tmp_path: Path) -> None:
    """手机报价列表 (2026-10-09): the held months the board does not carry are read through the quote-only
    channel — a catalogued read-only route, at most 20 symbols a read (the list asks only for the rows on
    screen), never for a visitor."""
    local_origin = "http://127.0.0.1:5173"
    app = create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake", cors_origins={local_origin})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": local_origin}) as client:
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer token"})).status_code == 200
            manifest = (await client.get("/api/agent/capabilities")).json()
            read = await client.get("/api/market/quotes", params={"symbols": "SHFE.rb2610,SHFE.au2612"})
            too_many = await client.get("/api/market/quotes", params={"symbols": ",".join(f"SHFE.rb26{n:02d}" for n in range(21))})
    operations = {item["operation"]: item for item in manifest["operations"]}
    assert operations["GET /api/market/quotes"]["readOnly"] is True
    assert read.status_code == 200 and {row["symbol"] for row in read.json()["rows"]} >= {"SHFE.rb2610"}
    assert too_many.status_code == 422


async def test_the_market_board_is_a_catalogued_route_and_tool(tmp_path: Path) -> None:
    """报价总表: the AI operability contract — the board HTTP route is in the capability
    catalog, the get_market_board tool is registered read-only and the market-lookup
    skill owns it (no hidden interfaces outside the catalog)."""
    from trader_runtime.data_providers import FakeReadOnlyDataProvider
    from trader_runtime.skill_registry import tool_owner_map

    local_origin = "http://127.0.0.1:5173"
    app = create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake", cors_origins={local_origin})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": local_origin}) as client:
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer token"})).status_code == 200
            manifest = (await client.get("/api/agent/capabilities")).json()
    operations = {item["operation"]: item for item in manifest["operations"]}
    assert operations["GET /api/market/board"]["readOnly"] is True
    assert "get_market_board" in TOOL_ARGUMENT_MODELS
    assert tool_owner_map()["get_market_board"] == "market-lookup"
    definitions = TraderReadOnlyTools(FakeReadOnlyDataProvider(), lambda mode: {}).definitions()
    board = next(item for item in definitions if item["name"] == "get_market_board")
    assert board["description"].startswith("读取报价总表")
    assert board["inputSchema"]["properties"]["sort"]["enum"] == ["changePercent", "volume", "openInterest", "openInterestChange"]


def test_reading_the_research_desk_is_not_marked_read_only() -> None:
    """529-A2c：当天判断还没生成时，`GET /api/research/desk` 的读取会触发一次模型
    生成并计入当日用量，能力目录必须标为非只读；同一服务的相邻读取没有这个副作用。"""
    from trader_runtime.agent_api import operation_is_read_only

    assert operation_is_read_only("GET /api/research/desk") is False
    assert operation_is_read_only("GET /api/research/news") is True
    assert operation_is_read_only("GET /api/research/summary") is True


async def test_account_lifecycle_writes_are_human_only_outside_the_catalog(tmp_path: Path) -> None:
    """526-A3: 开户/绑定/解绑 change which accounts the user holds and touch hosted
    credentials — the user's own actions in settings, never AI capabilities. The
    catalog lists none of them, app_call 404s them, while the account reads stay
    listed and the routes themselves keep serving the user's own UI."""
    local_origin = "http://127.0.0.1:5173"
    human_only = {"POST /api/accounts/open-paper", "POST /api/accounts/bind-tqkq",
                  "POST /api/accounts/unbind-tqkq"}
    app = create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake", cors_origins={local_origin})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": local_origin}) as client:
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer token"})).status_code == 200
            manifest = (await client.get("/api/agent/capabilities")).json()
            operations = {item["operation"] for item in manifest["operations"]}
            assert "GET /api/accounts" in operations  # reads stay agent-operable
            assert not (human_only & operations)
            for operation in sorted(human_only):
                refused = await client.post("/api/agent/call", json={"operation": operation, "body": {}})
                assert refused.status_code == 404, operation
    # The routes still exist in the app for the user's own UI — the catalog is
    # what excludes them, not the router.
    from fastapi.routing import APIRoute
    app_paths = {f"{method} {route.path}" for route in app.routes
                 if isinstance(route, APIRoute) for method in (route.methods or set())}
    assert human_only <= app_paths


async def test_rejecting_a_waiting_card_is_catalogued_and_confirming_is_not(tmp_path: Path) -> None:
    """#756: the card's 取消 is a business action any AI may take — the catalog lists the button's own
    route (same Owner: version check, audit, approvalUsedAt) — while confirming stays the user's click
    or spoken 确认: no confirm operation is listed and app_call 404s it."""
    local_origin = "http://127.0.0.1:5173"
    reject = "POST /api/proposals/{proposal_id}/reject"
    app = create_app(tmp_path / "runtime.sqlite3", local_token="token", agent_mode="fake", cors_origins={local_origin})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Origin": local_origin}) as client:
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer token"})).status_code == 200
            operations = {item["operation"] for item in (await client.get("/api/agent/capabilities")).json()["operations"]}
            assert reject in operations
            assert not any("/api/proposals/" in operation and operation != reject for operation in operations)
            assert not any("confirm" in operation for operation in operations)
            session_id = (await client.post("/api/sessions", json={"mode": "local"})).json()["sessionId"]
            card = app.state.proposals.create_trade(session_id, "external-reject", symbol="SHFE.au2612", side="buy_open",
                                                    volume=1, price_intent="passive", reason="external reject")
            proposal_id = card["proposalId"]
            record = app.state.store.get(proposal_id)

            async def call(version: int, key: str) -> dict:
                return (await client.post("/api/agent/call", json={
                    "operation": reject, "path": {"proposal_id": proposal_id},
                    "body": {"proposalVersion": version, "idempotencyKey": key}})).json()

            stale = await call(record["version"] + 1, "external-reject-stale")
            assert stale["status"] == 409 and stale["data"]["detail"]["code"] == "stale_proposal_version"
            assert app.state.store.get(proposal_id)["status"] == "approval_required"
            done = await call(record["version"], "external-reject-ok")
            assert done["ok"] and done["data"]["status"] == "rejected"
            after = app.state.store.get(proposal_id)
            assert after["status"] == "rejected" and after["approvalUsedAt"] is not None
            # The same key replays the answer; a fresh one finds the card already ended.
            assert (await call(record["version"], "external-reject-ok"))["data"]["status"] == "rejected"
            again = await call(record["version"], "external-reject-again")
            assert again["status"] == 409 and again["data"]["detail"]["code"] == "invalid_state_transition"
            refused = await client.post("/api/agent/call", json={
                "operation": "POST /api/proposals/{proposal_id}/confirm", "path": {"proposal_id": proposal_id},
                "body": {"approvalId": after["approvalId"], "proposalVersion": after["version"],
                         "idempotencyKey": "external-confirm"}})
            assert refused.status_code == 404
            confirm = await client.post(f"/api/proposals/{proposal_id}/confirm", json={
                "approvalId": after["approvalId"], "proposalVersion": after["version"], "idempotencyKey": "click-after-reject"})
            assert confirm.status_code == 409
            assert app.state.store.get(proposal_id)["status"] == "rejected"
