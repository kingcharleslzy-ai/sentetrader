"""09-23 回放语音与回放控制。

1. 回放窗口的用户级接口（语音状态/音色/播报、模型列表与偏好、偏好设置、账户目录只读、
   owner 状态）转发主 Runtime 并以当前登录用户执行；账户写入接口仍留在子 Runtime 的边界外
   （403），回放关闭后整个命名空间 404，绝不回落实盘接口。
2. 语音通话绑定回放后（socket 经 "/api/replays/<id>/runtime" 打开），读账户、确认下单卡、
   回放控制说法全部作用于回放子账户；实盘模拟网关 submit 保持 0、实盘权益不变；通用 AI
   （delegate）拒绝，防止未来信息进入历史回放。
3. control_replay 进入能力目录（tool:control_replay）：内置 AI 工具、外部 agent 与 CLI 走
   同一条 /api/replays/{identifier}/control 接口；play/pause/speed 可用，回放说法映射到同一
   组动作，按时间跳（"快进到下午3点"）诚实拒绝；套餐不含 replays 时工具从目录隐藏。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
from trader_runtime.main import create_app
from trader_runtime.skill_registry import hidden_tool_names
from trader_runtime.voice_intents import looks_like_replay_control, match_replay_control
from trader_runtime.voice_realtime import CallIdentity, CallState, _identity, _replay_binding, route_utterance

TOKEN = "test-replay-token"
HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": f"Bearer {TOKEN}"}
MALE_VOICE = "zh_male_xiaotian_jupiter_bigtts"


def _app(tmp_path: Path, source: HistoricalSource):
    application = create_app(
        tmp_path / "live.sqlite3", runtime_state_root=tmp_path, engineering_root=tmp_path / "engineering",
        data_mode="tqkq-sidecar", data_provider=source, agent_mode="native", local_token=TOKEN,
    )
    (tmp_path / "voice.key").write_text("k")
    application.state.voice_realtime.key_file = tmp_path / "voice.key"
    return application


async def _create_replay(client: AsyncClient) -> str:
    created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04",
                                                      "initialBalance": 1_000_000})
    assert created.status_code == 201, created.text
    prefix = created.json()["runtimePath"]
    # Let the child consume a state through its normal authenticated source.
    stepped = await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})
    assert stepped.status_code == 200, stepped.text
    return prefix


async def test_replay_window_user_level_surfaces_come_from_the_main_runtime(tmp_path: Path):
    source = HistoricalSource()
    app = _app(tmp_path, source)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            prefix = await _create_replay(client)
            # Trading and account data stay on the child.
            assert (await client.get(prefix + "/api/account/summary")).json()["equity"] == 1_000_000
            # User-level, replay-independent surfaces answer from the MAIN runtime: the voice
            # key, the model list, the preferences and the account catalog all live there.
            status = await client.get(prefix + "/api/voice/realtime/status")
            assert status.status_code == 200 and status.json()["enabled"] is True, status.text
            listed = await client.get(prefix + "/api/voice/realtime/voices")
            assert listed.status_code == 200 and listed.json()["voices"]
            models = await client.get(prefix + "/api/models")
            assert models.status_code == 200 and isinstance(models.json(), dict)
            preferences = await client.get(prefix + "/api/preferences")
            assert preferences.status_code == 200
            accounts = await client.get(prefix + "/api/accounts")
            assert accounts.status_code == 200 and accounts.json()["accounts"]
            owner = await client.get(prefix + "/api/auth/owner/status")
            assert owner.status_code == 200 and owner.json() == {"authenticated": False, "ownerAuth": False}
            # A write to a user-level surface changes the MAIN store, read back on the main route.
            changed = await client.put(prefix + "/api/voice/realtime/voice", json={"voice": MALE_VOICE})
            assert changed.status_code == 200 and changed.json()["current"] == MALE_VOICE
            assert (await client.get("/api/voice/realtime/status")).json()["voice"] == MALE_VOICE
            tuned = await client.put(prefix + "/api/preferences", json={"voice": {"speed": "fast"}})
            assert tuned.status_code == 200, tuned.text
            assert (await client.get("/api/preferences")).json()["preferences"]["voice"]["speed"] == "fast"
            # Account writes are not forwarded: the namespace never becomes a second live client.
            assert (await client.post(prefix + "/api/accounts/bind-tqkq", json={})).status_code == 403
            # A closed (or unknown) replay never falls back to live APIs.
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})).status_code == 200
            assert (await client.get(prefix + "/api/voice/realtime/status")).status_code == 404
            stranger = "/api/replays/" + "b" * 32 + "/runtime/api/voice/realtime/status"
            assert (await client.get(stranger)).status_code == 404
    app.state.store.close()


async def test_a_replay_voice_call_acts_on_the_replay_account_never_the_live_one(tmp_path: Path):
    # The hard constraint: a call bound to a replay reads, cards and confirms on the replay's
    # child runtime; the live simulation account and gateway are untouched.
    source = HistoricalSource()
    app = _app(tmp_path, source)
    service = app.state.voice_realtime
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            prefix = await _create_replay(client)
            session = (await client.post(prefix + "/api/sessions", json={"mode": "local"})).json()
            proposal = await client.post(prefix + "/api/simulation/proposals", json={
                "sessionId": session["sessionId"], "symbol": SYMBOL, "side": "buy_open", "volume": 1,
                "priceIntent": "aggressive", "reason": "replay voice isolation test",
            })
            assert proposal.status_code == 201, proposal.text
            card = proposal.json()
        # The call, exactly as a socket opened through the replay namespace would have it.
        state = CallState(user_id="local-user", replay_id=IDENTIFIER, chat_session_id=session["sessionId"])
        token = _identity.set(CallIdentity("local-user", {"authorization": f"Bearer {TOKEN}"}))
        binding = _replay_binding.set(IDENTIFIER)
        try:
            # The spoken account reads the replay snapshot account (1,000,000), not the live 3,000,000.
            account = await service.run_tool("get_account", {}, state)
            assert account["权益"] == "1,000,000 元", account
            # General AI stays out of a historical replay: no future information may enter it.
            refused = await service.run_tool("delegate", {"utterance": "买入一手"}, state)
            assert refused["ok"] is False and refused["error"] == "delegate_disabled_in_replay"
            assert "未来信息" in refused["say"]
            # The waiting card is the child's card; a spoken 确认 confirms it on the replay gateway.
            pending = service._server_pending(state)
            assert pending and pending["proposals"][0]["proposalId"] == card["proposalId"]
            state.pending = pending
            confirmed = await service.run_tool("confirm", {}, state)
            assert confirmed["ok"] is True, confirmed
            assert len(source.submits) == 1  # the replay gateway took the order
            assert app.state.simulation_orders.list(user_id="local-user") == []  # the live one did not
            assert source.account_summary("")["equity"] == 3_000_000  # the live account is untouched
            # Playback words drive the same control route as the replay window's buttons.
            paused = await service.run_tool("replay", {"utterance": "暂停"}, state)
            assert paused["ok"] is True and paused["say"] == "回放已暂停。"
            assert app.state.replay_router.contexts[IDENTIFIER].playing is False
            faster = await service.run_tool("replay", {"utterance": "加速到5倍"}, state)
            assert faster["ok"] is True and faster["say"] == "速度调到每秒 5 步。"
            assert app.state.replay_router.contexts[IDENTIFIER].speed == 5
            resumed = await service.run_tool("replay", {"utterance": "继续播放"}, state)
            assert resumed["ok"] is True and app.state.replay_router.contexts[IDENTIFIER].playing is True
            jumped = await service.run_tool("replay", {"utterance": "快进到下午3点"}, state)
            assert jumped["ok"] is False and jumped["error"] == "replay_control_unsupported"
            assert "不能按时间跳" in jumped["say"]
        finally:
            _replay_binding.reset(binding)
            _identity.reset(token)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})
        # The binding fails closed: a closed replay raises instead of falling back to the live account.
        token = _identity.set(CallIdentity("local-user", {"authorization": f"Bearer {TOKEN}"}))
        binding = _replay_binding.set(IDENTIFIER)
        try:
            gone = await service.run_tool("get_account", {}, state)
            assert gone["ok"] is False and gone["error"] == "replay_not_found"
        finally:
            _replay_binding.reset(binding)
            _identity.reset(token)
    app.state.store.close()


async def test_control_replay_is_a_catalogued_tool_on_the_shared_control_route(tmp_path: Path):
    source = HistoricalSource()
    app = _app(tmp_path, source)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            catalog = (await client.get("/api/agent/capabilities")).json()
            operations = {item["operation"] for item in catalog["operations"]}
            assert "tool:control_replay" in operations
            assert any("回放不能按时间跳" in str(item.get("description") or "")
                       for item in catalog["operations"] if item["operation"] == "tool:control_replay")
            # Without a replay the tool says so honestly.
            none = (await client.post("/api/agent/call", json={
                "operation": "tool:control_replay", "body": {"action": "pause"}})).json()
            assert none["ok"] is False and none["data"]["errorCode"] == "replay_not_found", none
            await _create_replay(client)
            # The built-in AI tool, an external agent and the CLI use this same call surface.
            paused = (await client.post("/api/agent/call", json={
                "operation": "tool:control_replay", "body": {"action": "pause"}})).json()
            assert paused["ok"] is True and paused["data"]["data"]["playing"] is False, paused
            assert app.state.replay_router.contexts[IDENTIFIER].playing is False
            resumed = (await client.post("/api/agent/call", json={
                "operation": "tool:control_replay", "body": {"action": "play"}})).json()
            assert resumed["ok"] is True and resumed["data"]["data"]["playing"] is True
            sped = (await client.post("/api/agent/call", json={
                "operation": "tool:control_replay", "body": {"action": "speed", "speed": 5}})).json()
            assert sped["ok"] is True and sped["data"]["data"]["speed"] == 5
            assert app.state.replay_router.contexts[IDENTIFIER].speed == 5
            # A speed without a value is refused before anything moves; 3 is not one of the steps.
            missing = (await client.post("/api/agent/call", json={
                "operation": "tool:control_replay", "body": {"action": "speed"}})).json()
            assert missing["ok"] is False and missing["data"]["errorCode"] == "speed_required"
            invalid = (await client.post("/api/agent/call", json={
                "operation": "tool:control_replay", "body": {"action": "speed", "speed": 3}})).json()
            assert invalid["ok"] is False and invalid["data"]["errorCode"] == "invalid_tool_arguments"
            # A native turn inside the replay child reaches the main bridge with its own headers.
            child = app.state.replay_router.contexts[IDENTIFIER].child
            tools = child.state.adapter.trader_tools
            assert tools.replay_context is not None
            child_session = child.state.broker.create_session("local", "native", "local-user").session_id
            response, card = await tools.call_async(tool="control_replay", arguments={"action": "pause"},
                                                    turn_id="t-replay-tool", mode="local",
                                                    session_id=child_session, user_id="local-user")
            result = json.loads(response["contentItems"][0]["text"])
            assert response["success"] is True and result["data"]["playing"] is False
            assert app.state.replay_router.contexts[IDENTIFIER].playing is False
    app.state.store.close()


def test_control_replay_hides_when_the_tier_has_no_replays(tmp_path):
    # The registry owns the mapping: with both replay skills open nothing hides,
    # with them withheld from the tier the replay tools do.
    from trader_runtime.store import RuntimeStore

    store = RuntimeStore(tmp_path / "skills.sqlite3")
    owner_hidden = hidden_tool_names(store, None)
    assert not owner_hidden & {"control_replay"}

    store.update_plan_tier("basic", config={
        **store.get_plan_tier("basic")["config"], "skills": []})
    from trader_runtime.store.trading_accounts import scoped_owner_user_id

    friend = scoped_owner_user_id("friend")
    store.connection.execute(
        "INSERT INTO cloud_users (username, password_hash, role, created_at, plan)"
        " VALUES ('friend', 'x', 'user', '2024-01-01', 'basic')")
    store.connection.commit()
    assert hidden_tool_names(store, friend) >= {"control_replay"}


def test_replay_control_phrases_route_and_map():
    assert match_replay_control("加速到5倍") == ("speed", 5)
    assert match_replay_control("5倍速") == ("speed", 5)
    assert match_replay_control("十倍速") == ("speed", 10)
    assert match_replay_control("十倍") is None  # an absolute tail needs a speed verb
    assert match_replay_control("速度调到2") == ("speed", 2)
    assert match_replay_control("暂停") == ("pause", None)
    assert match_replay_control("把回放暂停一下") == ("pause", None)
    assert match_replay_control("继续播放") == ("play", None)
    assert match_replay_control("接着放") == ("play", None)
    assert match_replay_control("单步") == ("step", None)
    assert match_replay_control("下一步") == ("step", None)
    assert match_replay_control("快一点") == ("speed_up", None)
    assert match_replay_control("慢一点") == ("speed_down", None)
    assert match_replay_control("3倍") is None  # not one of the engine's speed steps
    assert match_replay_control("买一手") is None  # trading words belong elsewhere
    assert match_replay_control("保证金十倍") is None  # an absolute tail needs a speed verb
    assert match_replay_control("打开回放") is None  # interface work
    assert looks_like_replay_control("快进到下午3点") is True  # unplayable, but clearly playback
    assert looks_like_replay_control("打开回放") is False
    # Routing: playback words take the control route only inside a replay-bound call.
    assert route_utterance("暂停", pending=False, replay_active=True) == "replay"
    assert route_utterance("继续播放", pending=False, replay_active=True) == "replay"
    assert route_utterance("快进到下午3点", pending=False, replay_active=True) == "replay"
    assert route_utterance("行情怎么样", pending=False, replay_active=True) == "quick"
    assert route_utterance("买一手", pending=False, replay_active=True) == "delegate"
    assert route_utterance("暂停", pending=False) == "chat"
    assert route_utterance("快进到下午3点", pending=False) == "delegate"


def test_the_replay_namespace_voice_socket_binds_the_call_to_that_replay(tmp_path: Path, monkeypatch):
    from starlette.testclient import TestClient

    import trader_runtime.voice_realtime as voice_realtime
    captured = {}

    async def fake_run_call(service, client, key, identity, replay_id=None):
        captured.update(replay_id=replay_id, binding=_replay_binding.get(), user=identity.user_id)

    monkeypatch.setattr(voice_realtime, "_run_call", fake_run_call)
    source = HistoricalSource()
    app = _app(tmp_path, source)
    with TestClient(app) as client:
        created = client.post("/api/replays", headers=HEADERS,
                              json={"symbol": SYMBOL, "tradingDay": "2026-09-04", "initialBalance": 1_000_000})
        prefix = created.json()["runtimePath"]
        assert client.post(f"/api/replays/{IDENTIFIER}/control", headers=HEADERS,
                           json={"action": "step"}).status_code == 200
        # The handshake authenticates as the signed-in user and binds the call to this replay
        # (the token rides the auth subprotocol, exactly as the client's WebSocket does).
        from trader_runtime.voice_realtime import AUTH_SUBPROTOCOL, AUTH_TOKEN_PREFIX
        subprotocols = [AUTH_SUBPROTOCOL, AUTH_TOKEN_PREFIX + TOKEN]
        with client.websocket_connect(prefix + "/api/voice/realtime", subprotocols=subprotocols,
                                      headers={"Origin": HEADERS["Origin"]}):
            pass
        assert captured == {"replay_id": IDENTIFIER, "binding": IDENTIFIER, "user": "local-user"}
        # A closed replay refuses the socket instead of falling back to a live call.
        assert client.post(f"/api/replays/{IDENTIFIER}/control", headers=HEADERS,
                           json={"action": "close"}).status_code == 200
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(prefix + "/api/voice/realtime", subprotocols=subprotocols,
                                          headers={"Origin": HEADERS["Origin"]}):
                pass
        assert closed.value.code == 4404
    app.state.store.close()


async def test_a_replay_app_is_single_tenant_so_its_account_block_has_a_role(tmp_path: Path):
    """2026-09-24: a replay app inherits the parent's owner-password auth. Without the
    single-tenant flag its runtime.status account block said role null, the client's
    contract rejected every status event, and the replay never loaded its chart. The
    child serves exactly the one user the parent's router checked owns the replay."""
    from trader_runtime.wiring.accounts import session_account_status_payload

    source = HistoricalSource()
    app = _app(tmp_path, source)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            await _create_replay(client)
            child = app.state.replay_router.contexts[IDENTIFIER].child
            store = child.state.store
            assert store.idp_fallback is True
            # Even with no grant row in the fresh replay database (the owner-password case),
            # the block names a role the client accepts.
            store.connection.execute("DELETE FROM account_grants")
            block = session_account_status_payload(
                store, account_id=store.default_trading_account_id, user_id="owner-anyone",
            )
            assert block is not None and block["role"] == "operator"
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})).status_code == 200
    app.state.store.close()



async def test_a_replay_event_stream_verifies_its_session_in_the_replay_database(tmp_path: Path):
    """2026-10-04: under owner-password auth (production) the merged per-event identity read
    (#508) looked the session up in the parent's store; a replay child keeps its sessions in its
    own database, so every replay event stream was cut on its first verify and the replay window
    never loaded. The child's verify must find its own session."""
    from trader_runtime.auth import hash_owner_password

    source = HistoricalSource()
    origin = "http://127.0.0.1:5173"
    app = create_app(
        tmp_path / "live.sqlite3", runtime_state_root=tmp_path, engineering_root=tmp_path / "engineering",
        data_mode="tqkq-sidecar", data_provider=source, agent_mode="native", auth_mode="owner-password",
        owner_username="test-owner", owner_password_hash=hash_owner_password("isolated-demo-password"),
        owner_session_secret="isolated-owner-session-secret-more-than-32-bytes", cors_origins={origin},
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers={"Origin": origin}) as client:
            login = await client.post("/api/auth/owner", json={"username": "test-owner", "password": "isolated-demo-password"})
            assert login.status_code == 200, login.text
            await _create_replay(client)
            created = await client.post(f"/api/replays/{IDENTIFIER}/runtime/api/sessions?chat=true", json={"mode": "local"})
            assert created.status_code in {200, 201}, created.text
            session_id = created.json()["sessionId"]
            child = app.state.replay_router.contexts[IDENTIFIER].child
            user = child.state.store.get_session(session_id)["userId"]
            cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
            bridge = child.state.app_capabilities
            identity = bridge.capture_identity(user, {"origin": origin, "cookie": cookie}, session_id)
            await bridge.verify_identity(identity)  # raised 401 session_auth_required before the fix
            assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "close"})).status_code == 200
    app.state.store.close()
