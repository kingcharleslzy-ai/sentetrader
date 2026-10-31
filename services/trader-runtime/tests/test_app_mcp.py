"""The external control transport must never initialize another account Runtime."""
import os
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters


@pytest.mark.asyncio
async def test_stdio_discovery_and_connection_failure_do_not_initialize_runtime(tmp_path):
    state = tmp_path / "state"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trader_runtime.sidecar_entry", "mcp", "--connection-file", str(tmp_path / "missing.json")],
        env={
            # setup-python's Linux interpreter needs its own libpython in child
            # processes too; the MCP SDK's default environment omits this path.
            **({"LD_LIBRARY_PATH": os.environ["LD_LIBRARY_PATH"]} if "LD_LIBRARY_PATH" in os.environ else {}),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AI_TRADER_ENGINEERING_ROOT": str(tmp_path / "engineering"),
            "AI_TRADER_STATE_ROOT": str(state),
            "AI_TRADER_DB_PATH": str(state / "runtime.sqlite3"),
            "AI_TRADER_DATA_MODE": "fake",
            "AI_TRADER_AGENT_MODE": "fake",
            "AI_TRADER_AGENT_PROVIDER": "chatgpt",
        },
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        # 技能注册表的三层披露工具与能力目录一起暴露（本 PR 新增），仍不初始化 Runtime
        assert {tool.name for tool in tools.tools} == {"app_capabilities", "app_call",
                                                       "list_skills", "activate_skill",
                                                       "read_skill_resource"}
        assert all(tool.output_schema for tool in tools.tools)
        assert not state.exists()
        result = await client.call_tool("app_capabilities", {})
        assert result.is_error
        assert not state.exists()
    assert not state.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('dispatched', [False, True])
@pytest.mark.parametrize('closing', [False, True])
async def test_reload_reclaims_offline_window_without_replaying_old_command(tmp_path, monkeypatch, dispatched, closing):
    from uuid import uuid4

    from httpx import ASGITransport, AsyncClient

    from trader_runtime.main import create_app
    from trader_runtime.ui_control import ChartOptions

    clock = [0.0]
    monkeypatch.setattr('trader_runtime.ui_control.monotonic', lambda: clock[0])
    app = create_app(tmp_path / 'runtime.sqlite3', runtime_state_root=tmp_path,
                     agent_mode='fake', local_token='test-local-token')
    state = dict(symbol='SHFE.rb2610', duration=60, view='trade', panel=None, settingsOpen=False,
                 theme='dark', dockSide='right', dockWidth=400, replay=False, settingsPage='connection',
                 fontScale='standard', density='standard', chart={k: True for k in ChartOptions.model_fields},
                 chartSizes=dict(main=58, kd=19, macd=23), strategy=None)
    observation = dict(capturedAt='2026-09-08T00:00:00Z', viewport=dict(width=1280, height=720),
                       text='交易台', truncated=False, regions=[], controls=[], context={})
    headers = {'Authorization': 'Bearer test-local-token', 'Origin': 'http://127.0.0.1:5173'}
    async with AsyncClient(transport=ASGITransport(app), base_url='http://runtime', headers=headers) as c:
        async def pulse(identifier, closing=False):
            return await c.post('/api/ui/pulse', json=dict(windowId=identifier, revision=0,
                                state=state, observation=observation, closing=closing))
        identifiers = [str(uuid4()) for _ in range(8)]
        for identifier in identifiers:
            assert (await pulse(identifier)).status_code == 200
        new_id = str(uuid4())
        assert (await pulse(new_id)).status_code == 429  # Eight active windows remain bounded.
        command_id = str(uuid4())
        assert (await c.post('/api/app/ui/commands', json=dict(commandId=command_id,
            windowId=identifiers[0], expectedRevision=0, changes={'theme': 'light'}))).status_code == 200
        if dispatched:
            assert (await pulse(identifiers[0])).json()['command']['commandId'] == command_id
        if closing:
            assert (await pulse(identifiers[0], closing=True)).status_code == 200
            assert (await pulse(identifiers[0], closing=True)).status_code == 200
        else:
            clock[0] = 76.0  # past ONLINE_SECONDS: the old window is offline and may be reclaimed
        assert (await pulse(new_id)).status_code == 200
        result = (await c.get(f'/api/app/ui/commands/{command_id}')).json()
        assert result['status'] == ('unknown' if dispatched else 'expired')
        windows = (await c.get('/api/app/ui/windows')).json()['windows']
        assert len(windows) == 8 and identifiers[0] not in {w['windowId'] for w in windows}
        # A waking old document must not receive a queued operation from before retirement.
        old = await pulse(identifiers[0])
        if closing:
            assert old.status_code == 410
        else:
            assert old.json()['command'] is None
        assert (await c.get(f'/api/app/ui/commands/{command_id}')).json()['status'] == result['status']
    app.state.store.close()


def test_cloud_mcp_session_is_bound_to_the_official_endpoint(tmp_path):
    import json

    from trader_runtime.app_connection import connection_options
    p = tmp_path / 'connection.json'
    token = 'v1.test.session'
    def save(url):
        p.write_text(json.dumps({'url': url, 'token': token, 'authMode':'owner-password'}))
        p.chmod(0o600)
    save('https://ai-runtime.qilinshuzhi.com:8443')
    options = connection_options(p)
    assert options['headers']['authorization'] == 'Bearer '+token
    assert options['follow_redirects'] is False
    for url in ['http://ai-runtime.qilinshuzhi.com:8443', 'https://evil.example',
                'https://ai-runtime.qilinshuzhi.com:8443@evil.example',
                'https://ai-runtime.qilinshuzhi.com:8443/other']:
        save(url)
        with pytest.raises(ValueError): connection_options(p)


@pytest.mark.parametrize('platform,env,expected', [
    ('darwin', {}, 'Library/Application Support/com.kingcharles.ai-trader/private-credentials/agent-cli.json'),
    ('win32', {'APPDATA':'/windows/Roaming'}, '/windows/Roaming/com.kingcharles.ai-trader/private-credentials/agent-cli.json'),
    ('linux', {'XDG_DATA_HOME':'/linux/data'}, '/linux/data/com.kingcharles.ai-trader/private-credentials/agent-cli.json'),
])
def test_mcp_finds_the_session_saved_by_each_desktop_platform(monkeypatch, platform, env, expected):
    from trader_runtime.app_connection import default_connection_file
    monkeypatch.setattr(sys, 'platform', platform)
    for key, value in env.items(): monkeypatch.setenv(key, value)
    assert str(default_connection_file()).replace('\\', '/').endswith(expected)


@pytest.mark.asyncio
async def test_mcp_and_cli_act_on_the_account_the_caller_names(tmp_path, monkeypatch):
    """2026-10-09: an external agent could not choose an account — the CLI and the MCP app_call sent
    no X-AI-Trader-Account, so every call landed on the session's or default account. The named
    account reaches the Runtime (a foreign one is refused there, never silently replaced)."""
    import json

    import httpx
    from httpx import ASGITransport

    import trader_runtime.app_cli as app_cli
    import trader_runtime.app_connection as app_connection
    from trader_runtime.app_mcp import create_server
    from trader_runtime.main import create_app

    app = create_app(tmp_path / 'runtime.sqlite3', runtime_state_root=tmp_path,
                     agent_mode='fake', local_token='test-local-token')
    headers = {'authorization': 'Bearer test-local-token', 'origin': 'http://127.0.0.1:5173'}
    monkeypatch.setattr(app_connection, 'connection_options', lambda _path: {
        'base_url': 'http://runtime', 'transport': ASGITransport(app), 'headers': headers})
    default_id = app.state.accounts.default.account_id
    try:
        async with Client(create_server(tmp_path / 'agent-cli.json'), raise_exceptions=True) as client:
            async def accounts(**extra):
                result = await client.call_tool('app_call', {'operation': 'GET /api/accounts', **extra})
                return result
            assert (await accounts()).structured_content['data']['current'] == default_id
            assert (await accounts(account=default_id)).structured_content['data']['current'] == default_id
            refused = await accounts(account='paper:not-mine')
            assert refused.is_error and 'account_not_granted' in refused.content[0].text
            malformed = await accounts(account='paper x')
            assert malformed.is_error and '账户 id 格式不对' in malformed.content[0].text
    finally:
        app.state.store.close()

    seen = []

    def handler(request):
        seen.append(request.headers.get('x-ai-trader-account'))
        return httpx.Response(200, json={'ok': True})
    monkeypatch.setattr(app_cli, 'connection_options', lambda _path: {
        'base_url': 'http://runtime', 'transport': httpx.MockTransport(handler)})
    body = tmp_path / 'call.json'
    body.write_text(json.dumps({'operation': 'GET /api/accounts'}))
    assert app_cli.main(['--account', 'paper:abc123', 'call', '--file', str(body)]) == 0
    assert app_cli.main(['call', '--file', str(body)]) == 0
    assert seen == ['paper:abc123', None]
    with pytest.raises(SystemExit):
        app_cli.main(['--account', 'paper x', 'call', '--file', str(body)])
