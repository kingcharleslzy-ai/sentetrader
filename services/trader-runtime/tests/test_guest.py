import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.main import create_app
from trader_runtime.plans import UNLIMITED, daily_token_limit, plan_for_user


@pytest.fixture
async def app(tmp_path):
    value = create_app(tmp_path / 'state.db', agent_mode='fake', data_mode='fake', local_token='private-token', cors_origins={'http://localhost'})
    yield value
    await value.state.guests.close()
    value.state.store.close()


def client(app, origin='http://localhost'):
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://localhost', headers={'origin': origin})


async def test_guest_http_permission_and_window_isolation(app):
    async with client(app) as a, client(app) as b:
        assert (await a.get('/api/guest/session')).status_code == 401
        a.cookies.set('ai_trader_guest', 'forged')
        assert (await a.get('/api/guest/session')).status_code == 401
        a.cookies.clear()
        aa = (await a.post('/api/auth/guest')).json()
        bb = (await b.post('/api/auth/guest')).json()
        assert aa['guestId'] != bb['guestId']
        assert aa['readOnly']
        assert (await a.post('/api/guest/ui', json={'windowId': bb['windowId'], 'changes': {'theme': 'dark'}})).status_code == 403
        assert (await a.post('/api/guest/ui', json={'windowId': aa['windowId'], 'changes': {'positionsTab': 'orders'}})).status_code == 403
        result = await a.post('/api/guest/ui', json={'windowId': aa['windowId'], 'changes': {'theme': 'dark'}})
        assert result.json()['revision'] == 1
        await a.post('/api/guest/ui', json={'windowId': aa['windowId'], 'snapshot': {'appliedRevision': 1}})
        assert (await a.get('/api/guest/ui')).json()['revision'] == 1
        assert (await b.get('/api/guest/ui')).json()['changes'] == {}
        # 合约抽屉“重试” after an empty public catalog (2026-10-11): only true.
        assert (await a.post('/api/guest/ui', json={'windowId': aa['windowId'], 'changes': {'contractCatalogRetry': False}})).status_code == 422
        assert (await a.post('/api/guest/ui', json={'windowId': aa['windowId'], 'changes': {'contractCatalogRetry': True}})).json()['revision'] == 2
        for path in ('/api/chat-sessions', '/api/agent/account', '/api/strategies', '/api/app/ui/windows'):
            assert (await a.get(path)).status_code in (401, 403), path
        for operation in ('GET /api/agent/account', 'get_positions', 'POST /api/simulation/orders'):
            assert (await a.post('/api/agent/call', json={'operation': operation})).status_code == 403
        manifest = (await a.get('/api/agent/capabilities')).json()
        assert all((i['operation'].split()[1].startswith('/api/guest/') or i['operation'] == 'POST /api/auth/guest') for i in manifest['operations'])
        response = await a.post('/api/agent/call', json={'operation': 'GET /api/guest/market', 'query': {'symbol': 'SHFE.rb2610'}})
        assert response.json()['ok']
        guest = app.state.guests.sessions[a.cookies.get('ai_trader_guest')]
        guest.expires = 0
        assert (await a.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'})).status_code == 401
        assert (await b.get('/api/agent/account', headers={'authorization': 'Bearer private-token'})).status_code == 200


async def test_market_projection_shared_fetch_and_bounds(app, monkeypatch):
    import trader_runtime.shared_market as module

    shared = app.state.shared_market
    loop = asyncio.get_running_loop()
    now = loop.time()
    monkeypatch.setattr(loop, 'time', lambda: now)  # hold idle retirement as well as producer time
    all_readers = asyncio.Event()
    refreshes = asyncio.Queue()
    canonical = shared.canonical_symbol
    readers = 0

    async def joined(symbol):
        nonlocal readers
        value = await canonical(symbol)
        readers += 1
        if readers == 4:
            all_readers.set()
        return value

    read_market = shared.read_market

    async def first_read(path, fetch):
        if '/workbench?' not in path:
            return await read_market(path, fetch)
        await all_readers.wait()
        # All four HTTP requests joined one channel before its first provider read.
        if not calls:
            assert shared.markets[('SHFE.rb2610', 300)].refs == 4
        return await read_market(path, fetch)

    async def producer_wait(delay):
        assert delay == shared.fake_interval
        permit = asyncio.Event()
        refreshes.put_nowait(permit)
        await permit.wait()

    class ControlledAsyncio:
        sleep = staticmethod(producer_wait)

        def __getattr__(self, name):
            return getattr(asyncio, name)

    monkeypatch.setattr(module, 'asyncio', ControlledAsyncio())
    monkeypatch.setattr(shared, 'canonical_symbol', joined)
    monkeypatch.setattr(shared, 'read_market', first_read)
    service = app.state.guests
    provider = app.state.data_provider
    calls = []
    original = provider.market_workbench
    def workbench(symbol, duration):
        calls.append((symbol, duration))
        value = original(symbol, duration)
        value['positions'] = [{'secret': 'private'}]
        value['quote']['account'] = 'private'
        value['bars'][0]['private'] = 'secret'
        return value
    provider.market_workbench = workbench
    def no_separate_snapshot(*args, **kwargs):
        raise AssertionError("guest quote must come from the same workbench read")
    provider.market_snapshot = no_separate_snapshot
    async with client(app) as a, client(app) as b:
        await a.post('/api/auth/guest')
        await b.post('/api/auth/guest')
        results = await asyncio.gather(*(c.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'}) for c in (a, b, a, b)))
        assert len(calls) == 1
        first_refresh = await refreshes.get()
        channel = shared.markets[('SHFE.rb2610', 300)]
        producer = channel.task
        for response in results:
            frame = response.json()
            assert frame['quote'] == frame['workbench']['quote']
            assert all(isinstance(frame['quote'][key], (int, float)) for key in ('lastPrice', 'highest', 'lowest'))
        assert all('private' not in r.text and 'secret' not in r.text for r in results)
        assert (await b.get('/api/guest/market', params={'symbol': 'SHFE.rb2610', 'duration': 60})).status_code == 200
        assert calls == [('SHFE.rb2610', 300), ('SHFE.rb2610', 60)]
        await refreshes.get()  # the 60-second channel is also parked at its first refresh
        first_refresh.set()
        await refreshes.get()  # explicitly finish one 300-second refresh, then park again
        assert calls == [('SHFE.rb2610', 300), ('SHFE.rb2610', 60), ('SHFE.rb2610', 300)]
        assert shared.markets[('SHFE.rb2610', 300)] is channel and channel.task is producer
        assert (await b.get('/api/guest/market', params={'symbol': 'anything'})).status_code == 403
        assert (await b.get('/api/guest/market', params={'symbol': 'SHFE.rb2610', 'duration': 1})).status_code == 422
    for i in range(service.cache_limit + 6):
        await service.shared(('test', i), lambda: {}, 60)
    assert len(service.cache) == service.cache_limit
    assert not service.inflight


async def test_guest_sees_only_the_rebar_main_contract(app):
    catalog = app.state.data_provider.instrument_catalog()
    # The main contract follows the source's main flag, not a fixed code.
    for item in catalog['items']:
        if item['productId'] == 'rb':
            item['isMain'] = False
    catalog['items'].append({**next(i for i in catalog['items'] if i['productId'] == 'rb'), 'symbol': 'SHFE.rb2701', 'instrumentId': 'rb2701',
                             'instrumentName': '螺纹钢2701', 'isMain': True})
    app.state.data_provider.instrument_catalog = lambda: catalog
    async with client(app) as a:
        identity = (await a.post('/api/auth/guest')).json()
        assert [i['symbol'] for i in (await a.get('/api/guest/instruments')).json()['items']] == ['SHFE.rb2701']
        assert (await a.get('/api/guest/market', params={'symbol': 'rb2701'})).status_code == 200
        for symbol in ('SHFE.rb2610', 'SHFE.au2612'):
            assert (await a.get('/api/guest/market', params={'symbol': symbol})).status_code == 403
            assert (await a.get('/api/guest/stream', params={'symbol': symbol})).status_code == 403
            assert (await a.get('/api/guest/research/news', params={'symbol': symbol})).status_code == 403
        assert app.state.guests.sessions[a.cookies['ai_trader_guest']].streams == 0
        window = identity['windowId']
        for changes in ({'symbol': 'SHFE.au2612'}, {'pinnedSymbols': {'add': 'SHFE.au2612'}}, {'news': {'pinnedSymbol': 'SHFE.au2612'}},
                        {'theme': 'dark', 'symbol': 'SHFE.au2612'}):
            assert (await a.post('/api/guest/ui', json={'windowId': window, 'changes': changes})).status_code == 403
        assert (await a.get('/api/guest/ui')).json()['revision'] == 0
        assert (await a.post('/api/guest/ui', json={'windowId': window, 'changes': {'symbol': 'shfe.rb2701'}})).json()['changes'] == {'symbol': 'SHFE.rb2701'}
        said = (await a.post('/api/guest/voice/intent', json={'windowId': window, 'text': '切到沪金'})).json()
        assert said['accepted'] is False and '螺纹' in said['say']


async def test_guest_rate_limits_use_the_proxy_peer_address(app):
    # The deployed shape: uvicorn's ProxyHeadersMiddleware in front of the app
    # and the 8443 nginx emitting one XFF value — the visitor it restored. The
    # same shared entry as the owner login/register budget (auth.visitor_ip).
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    proxied = AsyncClient(
        transport=ASGITransport(app=ProxyHeadersMiddleware(app, trusted_hosts='*'), client=('127.0.0.1', 51000)),
        base_url='http://localhost', headers={'origin': 'http://localhost'})
    async with proxied as a:
        for i in range(30):
            a.cookies.clear()
            response = await a.post('/api/auth/guest', headers={'x-forwarded-for': '203.0.113.9', 'x-real-ip': f'10.255.{i}.1'})
            assert response.status_code == 200
        a.cookies.clear()
        assert (await a.post('/api/auth/guest', headers={'x-forwarded-for': '203.0.113.9'})).status_code == 429
        a.cookies.clear()
        assert (await a.post('/api/auth/guest', headers={'x-forwarded-for': '203.0.113.10'})).status_code == 200


async def test_guest_rate_limits_on_a_direct_connection_ignore_headers(app):
    # No proxy in front: a direct caller cannot rotate its budget by naming
    # other addresses in headers — the key stays on the real TCP peer.
    async with client(app) as a:
        for i in range(30):
            a.cookies.clear()
            response = await a.post('/api/auth/guest', headers={'x-real-ip': f'203.0.113.{i}', 'x-forwarded-for': f'10.0.0.{i}'})
            assert response.status_code == 200
        a.cookies.clear()
        assert (await a.post('/api/auth/guest', headers={'x-real-ip': '203.0.113.77'})).status_code == 429


async def test_intent_never_falls_back_and_limits_survive_cookie_rotation(app):
    async with client(app) as a:
        identity = (await a.post('/api/auth/guest')).json()
        for text in ('买入一手黄金', '帮我研究行情', '打开持仓', '打开策略'):
            r = await a.post('/api/guest/voice/intent', json={'windowId': identity['windowId'], 'text': text})
            assert r.json()['accepted'] is False
        for text in ('让AI们讨论一下这条', '辩一辩螺纹', '打开辩论'):  # 多 AI 研讨 and its page: never for a visitor
            r = (await a.post('/api/guest/voice/intent', json={'windowId': identity['windowId'], 'text': text})).json()
            assert r['accepted'] is False and r['reason'] == 'guest_debate_forbidden'
        r = await a.post('/api/guest/voice/intent', json={'windowId': identity['windowId'], 'text': '打开新闻'})
        assert r.json()['changes'] == {'newsOpen': True}
        for i in range(29):
            a.cookies.clear()
            assert (await a.post('/api/auth/guest')).status_code == 200
        a.cookies.clear()
        assert (await a.post('/api/auth/guest')).status_code == 429
    service = app.state.guests
    for _ in range(2):
        service.charge(('voice-ip', 'test'), 30, 86400, 15)
    with pytest.raises(Exception) as error:
        service.charge(('voice-ip', 'test'), 30, 86400, 15)
    assert error.value.status_code == 429
    assert daily_token_limit(plan_for_user(app.state.store, 'guest:abc'), env_limit=0) == 0
    assert plan_for_user(app.state.store, 'unknown-owner') is UNLIMITED


async def test_guest_disabled(app, monkeypatch):
    app.state.guests.enabled = False
    async with client(app) as c:
        assert (await c.post('/api/auth/guest')).status_code == 404


async def test_real_guest_requires_explicit_market_source(app):
    from dataclasses import replace

    app.state.guests.ctx.settings = replace(app.state.guests.ctx.settings, data_mode='tqkq-readonly')
    async with client(app) as c:
        await c.post('/api/auth/guest')
        response = await c.get('/api/guest/instruments')
        assert response.status_code == 503
        assert response.json()['detail'] == 'guest_market_source_unavailable'


async def test_selected_guest_source_reuses_pool_without_trading_assembly(tmp_path, monkeypatch):
    from test_account_pool import ORIGIN, bind, bind_account_id, local_client, pool_app
    from trader_runtime.data_providers import FakeReadOnlyDataProvider

    monkeypatch.setenv('AI_TRADER_GUEST_MARKET_ACCOUNT_ID', bind_account_id())
    application, pool, assemblies = pool_app(tmp_path, monkeypatch, 'guest-source.db')
    manager = application.state.account_pool
    guests = application.state.guests
    account_id = bind_account_id()
    owner = await local_client(application)
    provider = FakeReadOnlyDataProvider()
    reads = []

    async def market(port, path):
        assert port == pool.running[account_id]
        reads.append(path)
        if path == '/v1/public-market/instruments':
            return provider.instrument_catalog()
        assert path.startswith('/v1/public-market/SHFE.rb2610/workbench?duration=300')
        if '&after=' in path:
            await asyncio.sleep(10)
        value = provider.market_workbench('SHFE.rb2610', 300)
        value['revision'] = '1'
        value['quote']['lastPrice'] = 777
        value['quote']['account'] = 'private'
        value['positions'] = ['private']
        return value

    pool.market = market
    try:
        await bind(application, owner)
        await manager.detach(account_id, reason='test-owner-disconnected')
        assemblies.clear()
        manager.presence._last_seen.clear()
        manager._started -= manager.idle_seconds + 10
        original_spawns = len(pool.spawns)
        async with client(application, ORIGIN) as a, client(application, ORIGIN) as b:
            assert (await a.post('/api/auth/guest')).status_code == 200
            assert (await b.post('/api/auth/guest')).status_code == 200
            responses = await asyncio.gather(*(c.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'}) for c in (a, b, a, b)))
            assert all(r.status_code == 200 and r.json()['quote']['lastPrice'] == 777 for r in responses)
            assert all('private' not in r.text for r in responses)
            assert len(pool.spawns) == original_spawns  # Existing child's real side effect stays unchanged.
            # One catalog, the calendar it reads for delivery reminders (2026-10-12) and one initial frame.
            assert len([path for path in reads if '&after=' not in path]) == 3
            assert not assemblies and application.state.accounts.get(account_id) is None
            await manager._scan_idle()
            assert account_id in pool.running  # Guest activity keeps only the shared source alive.
            # A lost child is restarted, still without assembling trading loops.
            pool.running.pop(account_id)
            for channel in list(guests.markets.values()):
                guests.retire_market(channel)
            guests.cache.clear()
            assert (await a.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'})).status_code == 200
            assert len(pool.spawns) == original_spawns + 1 and not assemblies
            # No readers/users remain: the existing idle policy reclaims it.
            manager._market_last_seen[account_id] -= manager.idle_seconds + 10
            await manager._scan_idle()
            assert account_id not in pool.running
            for channel in list(guests.markets.values()):
                guests.retire_market(channel)
            guests.cache.clear()
            assert (await a.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'})).status_code == 200
            assert not assemblies
        # The same account can still enter its ordinary authenticated trading workspace.
        assert (await owner.get('/api/account/summary')).status_code == 200
        assert len(assemblies) == 1
        old_port = pool.running[account_id]
        pool.running.pop(account_id)
        for channel in list(guests.markets.values()):
            guests.retire_market(channel)
        guests.cache.clear()
        async with client(application, ORIGIN) as guest:
            assert (await guest.post('/api/auth/guest')).status_code == 200
            assert (await guest.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'})).status_code == 200
        assert pool.running[account_id] != old_port
        assert application.state.accounts.get(account_id) is None
        assert len(assemblies) == 1  # No trading loops started by guest recovery.
        assert (await owner.get('/api/account/summary')).status_code == 200
        assert assemblies[-1].sidecar_url == f'http://127.0.0.1:{pool.running[account_id]}'
    finally:
        await owner.aclose()
        await manager.shutdown()
        await guests.close()
        application.state.store.close()


@pytest.mark.parametrize('failure', ['missing', 'disabled', 'credentials', 'decrypt', 'capacity', 'unavailable'])
async def test_selected_guest_source_failure_never_uses_owner(tmp_path, monkeypatch, failure):
    from test_account_pool import ORIGIN, bind, bind_account_id, local_client, pool_app

    monkeypatch.setenv('AI_TRADER_GUEST_MARKET_ACCOUNT_ID', bind_account_id())
    application, pool, assemblies = pool_app(tmp_path, monkeypatch, 'guest-source-error.db')
    manager = application.state.account_pool
    store = application.state.store
    owner = await local_client(application)
    try:
        await bind(application, owner)
        account_id = bind_account_id()
        await manager.release(account_id, reason='test-cold-source')
        assemblies.clear()
        if failure == 'missing':
            application.state.shared_market.market_account_id = 'tqkq-sim:missing'
        elif failure == 'disabled':
            store.disable_trading_account(bind_account_id())
        elif failure == 'credentials':
            store.delete_tqkq_credentials(user_id='local-user')
        elif failure == 'decrypt':
            with store.lock, store.connection:
                store.connection.execute("UPDATE tqkq_credentials SET ciphertext = X'00'")
        elif failure == 'capacity':
            pool.capacity = True
        else:
            pool.refuse = 'pool_unavailable'
        async with client(application, ORIGIN) as guest:
            assert (await guest.post('/api/auth/guest')).status_code == 200
            response = await guest.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'})
            assert response.status_code == 503
            assert response.json()['detail'] == 'guest_market_source_unavailable'
        assert not assemblies
    finally:
        await owner.aclose()
        await manager.shutdown()
        store.close()


async def test_stream_disconnect_and_slow_client_hold_only_latest_state(app):
    async with client(app) as c:
        identity = (await c.post('/api/auth/guest')).json()
        cookie = c.cookies.get('ai_trader_guest')
    guest = app.state.guests.sessions[cookie]
    disconnected = asyncio.Event()
    first = asyncio.Event()
    frames = []
    async def receive():
        await disconnected.wait()
        return {'type': 'http.disconnect'}
    async def send(message):
        if message['type'] == 'http.response.body' and message.get('body'):
            frames.append(message['body'])
            first.set()
            await disconnected.wait()  # backpressure: no producer queue behind this send
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'method': 'GET',
             'path': '/api/guest/stream', 'raw_path': b'/api/guest/stream',
             'query_string': b'symbol=SHFE.rb2610&duration=300', 'scheme': 'http',
             'headers': [(b'cookie', ('ai_trader_guest=' + cookie).encode())],
             'client': ('127.0.0.1', 1), 'server': ('localhost', 80), 'root_path': ''}
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(first.wait(), 3)
    assert guest.streams == 1
    assert b'event: snapshot' in frames[0]
    assert len(frames) == 1
    disconnected.set()
    await asyncio.wait_for(task, 3)
    assert guest.streams == 0
    assert not app.state.guests.inflight
    assert identity['windowId'] == guest.window


def test_voice_socket_auth_and_quota(app, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import trader_runtime.voice_transcribe as module
    calls = []
    async def transcribe(service, socket, key, identity, *, meter=True):
        calls.append((identity.user_id, meter))
        await socket.send_json({'type': 'final', 'text': '打开新闻'})
        await socket.close()
    monkeypatch.setattr(module, 'run_transcribe', transcribe)
    app.state.voice_realtime = SimpleNamespace(_read_key=lambda: 'not-a-real-key')
    with TestClient(app, headers={'origin': 'http://localhost'}) as c:
        identity = c.post('/api/auth/guest').json()
        from starlette.websockets import WebSocketDisconnect
        app.state.store.set_skill_enabled('voice-call', False)
        with pytest.raises(WebSocketDisconnect) as disabled:
            with c.websocket_connect('/api/guest/voice/transcribe'):
                pass
        assert disabled.value.code == 4403 and calls == []
        app.state.store.set_skill_enabled('voice-call', True)
        for _ in range(4):
            with c.websocket_connect('/api/guest/voice/transcribe') as ws:
                assert ws.receive_json()['text'] == '打开新闻'
        with pytest.raises(WebSocketDisconnect) as error:
            with c.websocket_connect('/api/guest/voice/transcribe'):
                pass
        assert error.value.code == 4429
        assert calls == [(identity['guestId'], False)] * 4
        c.cookies.clear()
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('/api/guest/voice/transcribe'):
                pass


async def test_research_async_news_and_no_private_rows(app, monkeypatch):
    service = app.state.guests
    store = app.state.store
    tables = ('cloud_users', 'sessions', 'trading_accounts')
    before = {t: store.connection.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in tables}
    calls = []
    async def brief(symbol, topic):
        calls.append((symbol, topic))
        await asyncio.sleep(.01)
        return {'symbol': symbol, 'topic': topic, 'results': []}
    monkeypatch.setattr(service.ctx.services.research_brief, 'brief', brief)
    async with client(app) as a, client(app) as b:
        await a.post('/api/auth/guest')
        await b.post('/api/auth/guest')
        results = await asyncio.gather(*(c.get('/api/guest/research/news', params={'symbol': 'SHFE.rb2610'}) for c in (a, b)))
        assert all(r.status_code == 200 and r.json()['topic'] == 'news' for r in results)
        assert len(calls) == 1
    assert {t: store.connection.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in tables} == before


async def test_research_desk_gives_guests_facts_only(app, monkeypatch):
    calls = []
    async def report(symbol, **kwargs):
        calls.append((symbol, kwargs))
        return {'symbol': symbol, 'facts': [{'id': 'M.price'}], 'judgment': {'status': 'ready', 'headline': '偏多'},
                'news': {'net': '偏空', 'take': 'AI 对快讯的归纳'}}
    monkeypatch.setattr(app.state.guests.ctx.services.research_desk, 'report', report)
    async with client(app) as a:
        await a.post('/api/auth/guest')
        response = await a.get('/api/guest/research/desk', params={'symbol': 'SHFE.rb2610'})
    assert response.status_code == 200
    assert response.json()['facts'] == [{'id': 'M.price'}] and response.json()['judgment'] == {'status': 'signin_required'}
    assert 'news' not in response.json()  # the 新闻面 line is the AI's reading of the 快讯: signed-in only
    assert calls == [('SHFE.rb2610', {'fast': True})]  # no generator: visitors never start a judgment; never waits


def test_cli_guest_connection_uses_guest_bearer_only(tmp_path):
    import json

    from trader_runtime.app_connection import connection_options
    path = tmp_path / 'connection.json'
    path.write_text(json.dumps({'authMode': 'guest', 'url': 'http://127.0.0.1:8000', 'token': 'guest.' + 'a' * 43}))
    path.chmod(0o600)
    options = connection_options(path)
    assert options['headers'] == {'authorization': 'Bearer guest.' + 'a' * 43, 'origin': 'tauri://localhost'}
    assert not options['follow_redirects']


async def test_tier_edits_cannot_authorize_guest_trade_or_unlimited_voice(app):
    app.state.store.update_plan_tier('guest', config={'models': None, 'dailyTokens': None, 'voiceSecondsPerDay': None, 'skills': ['ai-execution', 'voice-call']})
    assert app.state.guests.voice_limit() == 0
    async with client(app) as c:
        await c.post('/api/auth/guest')
        r = await c.post('/api/agent/call', json={'operation': 'execute_trade'})
        assert r.status_code == 403
        r = await c.post('/api/sessions', json={})
        assert r.status_code in (401, 403)


async def test_guest_ui_chart_controls_are_bounded(app):
    async with client(app) as c:
        identity = (await c.post('/api/auth/guest')).json()
        changes = {'paneSizes': {'main': 60, 'kd': 20, 'macd': 20}, 'kd': True, 'macd': False, 'volume': True, 'researchExpanded': True, 'news': {'tab': 'data', 'pinnedSymbol': None}}
        result = await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': changes})
        assert result.status_code == 200
        assert result.json()['changes'] == changes
        changes['paneSizes']['main'] = 100
        assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': changes})).status_code == 422


async def test_guest_mobile_tab_allows_the_phone_tabs_but_not_chat(app):
    async with client(app) as c:
        identity = (await c.post('/api/auth/guest')).json()
        for tab in ('market', 'research', 'ai', 'notify', 'me'):
            result = await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'mobileTab': tab}})
            assert result.status_code == 200 and result.json()['changes']['mobileTab'] == tab
        for tab in ('chat', None, 'order'):
            assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'mobileTab': tab}})).status_code == 422
        for changes in ({'mobilePage': 'order'}, {'mobileNotifyFilter': 'fill'}, {'mobilePage': 'push'},
                        {'mobilePage': 'reminders'}, {'reminderCenter': {'open': True}}):
            assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': changes})).status_code == 403
        # 安装到桌面 (2026-10-06): the one full-screen page a visitor may open (and close).
        for page in ('install', None):
            assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'mobilePage': page}})).status_code == 200
        # 手机看盘 (2026-10-09): a visitor switches the view and the single chart's sub pane; the 报价 / 多图 settings
        # are a member's account preferences.
        for market in ({'view': 'multi'}, {'subPane': 'cci'}, {'subPanes': True}):
            assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'mobileMarket': market}})).status_code == 200
        assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'mobileMarket': {'board': 'main'}}})).status_code == 403
        assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'mobileMarket': {'view': 'chart'}}})).status_code == 422
        assert 'mobileTab' in (await c.get('/api/agent/capabilities')).json()['uiControls']['changes']


async def test_desktop_bearer_is_guest_only_and_browser_has_no_token(app):
    # Tauri has no cross-site cookie: all later requests have only its bearer.
    app.state.ctx.settings.origins.add('tauri://localhost')
    async with client(app) as c:
        assert 'accessToken' not in (await c.post('/api/auth/guest')).json()
        c.cookies.clear()
        desktop = (await c.post('/api/auth/guest', headers={'origin': 'tauri://localhost'})).json()
        token = desktop['accessToken']
        assert token.startswith('guest.')
        c.cookies.clear()
        headers = {'authorization': 'Bearer ' + token, 'origin': 'tauri://localhost'}
        assert (await c.get('/api/guest/session', headers=headers)).json()['guestId'] == desktop['guestId']
        assert (await c.get('/api/agent/capabilities', headers=headers)).json()['role'] == 'guest'
        assert (await c.post('/api/agent/call', headers=headers, json={'operation': 'GET /api/guest/ui'})).json()['ok']
        assert (await c.get('/api/agent/account', headers=headers)).status_code == 401
        assert (await c.get('/api/guest/session', headers={'authorization': 'Bearer guest.forged'})).status_code == 401
        assert (await c.get('/api/agent/capabilities', headers={'authorization': 'Bearer guest.forged'})).status_code == 401
        assert (await c.get('/api/guest/session', headers={'authorization': 'Bearer private-token'})).status_code == 401


def test_desktop_voice_bearer_subprotocol_isolated(app, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import trader_runtime.voice_transcribe as module
    app.state.ctx.settings.origins.add('tauri://localhost')
    calls = []
    async def transcribe(service, socket, key, identity, *, meter=True):
        calls.append(identity.user_id)
        await socket.send_json({'type': 'final', 'text': '打开新闻'})
        await socket.close()
    monkeypatch.setattr(module, 'run_transcribe', transcribe)
    app.state.voice_realtime = SimpleNamespace(_read_key=lambda: 'test-key')
    with TestClient(app, headers={'origin': 'tauri://localhost'}) as c:
        identity = c.post('/api/auth/guest').json()
        c.cookies.clear()
        with c.websocket_connect('/api/guest/voice/transcribe', subprotocols=['ai-trader-token', 'bearer.' + identity['accessToken']]) as ws:
            assert ws.accepted_subprotocol == 'ai-trader-token'
            assert ws.receive_json()['text'] == '打开新闻'
        assert calls == [identity['guestId']]
        for token in ('guest.forged', 'private-token'):
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect('/api/guest/voice/transcribe', subprotocols=['ai-trader-token', 'bearer.' + token]):
                    pass


async def test_market_packets_share_encoding_and_only_changed_tail(app, monkeypatch):
    import json

    import trader_runtime.shared_market as module
    service = app.state.guests
    provider = app.state.data_provider
    original = provider.market_workbench
    reads, encodes = [], []
    original_event = module.event
    def encode(name, value):
        encodes.append(name)
        return original_event(name, value)
    monkeypatch.setattr(module, 'event', encode)
    def workbench(symbol, duration):
        reads.append(symbol)
        raw = original(symbol, duration)
        # Use 500 bars and change only the latest one on each provider refresh.
        raw['bars'] = [{**raw['bars'][-1], 'datetime': i, 'close': 10} for i in range(500)]
        raw['bars'][-1]['close'] += len(reads)
        raw['marketSessionStatus'] = 'closed'
        return raw
    monkeypatch.setattr(provider, 'market_workbench', workbench)
    first = await service.market_packet('SHFE.rb2610', 300)
    assert len(first['value']['workbench']['bars']) == 500
    channel = service.markets[('SHFE.rb2610', 300)]
    await asyncio.wait_for(channel.changed.wait(), 1)
    packets = await asyncio.gather(*(service.market_packet('SHFE.rb2610', 300) for _ in range(20)))
    assert all(packet is packets[0] for packet in packets)
    assert len(reads) == 2
    assert encodes.count('update') == 0  # 2026-10-13: encoded only once a guest stream reads it
    payload = json.loads(packets[0]['update'].split('data: ')[1])
    assert packets[0]['update'] is packets[0]['update'] and encodes.count('update') == 1
    assert 'bars' not in payload['workbench']
    assert len(payload['workbench']['barsPatch']['bars']) == 1
    assert payload['workbench']['barsPatch']['bars'][0]['datetime'] == 499
    assert 'capturedAt' not in payload['workbench']
    assert first['value']['workbench']['marketSessionStatus'] == 'closed'


async def test_catalog_status_and_the_period_whitelist(app, monkeypatch):
    provider = app.state.data_provider
    catalog = provider.instrument_catalog()
    monkeypatch.setattr(provider, 'instrument_catalog', lambda: {**catalog, 'catalogStatus': 'loading'})
    async with client(app) as c:
        identity = (await c.post('/api/auth/guest')).json()
        assert (await c.get('/api/guest/instruments')).json()['catalogStatus'] == 'loading'
        # 30 分 (2026-10-02) joined the display whitelist everywhere; anything between the steps still refuses.
        assert (await c.get('/api/guest/market', params={'symbol': 'SHFE.rb2610', 'duration': 1800})).status_code == 200
        assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'duration': 1800}})).status_code == 200
        for duration in (1200, 14400):
            assert (await c.get('/api/guest/market', params={'symbol': 'SHFE.rb2610', 'duration': duration})).status_code == 422
            assert (await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'duration': duration}})).status_code == 422


async def test_sse_ui_initial_and_revision_events_without_polling(app):
    import json
    async with client(app) as c:
        identity = (await c.post('/api/auth/guest')).json()
        cookie = c.cookies.get('ai_trader_guest')
        disconnect = asyncio.Event()
        changed = asyncio.Event()
        ui_events = []
        async def receive():
            await disconnect.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            body = message.get('body', b'')
            if b'event: ui' in body:
                ui_events.append(json.loads(body.decode().split('data: ')[1]))
                if len(ui_events) == 1:
                    await c.post('/api/guest/ui', json={'windowId': identity['windowId'], 'changes': {'theme': 'dark'}})
                else:
                    changed.set()
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'method': 'GET',
                 'path': '/api/guest/stream', 'raw_path': b'/api/guest/stream',
                 'query_string': b'symbol=SHFE.rb2610&duration=300', 'scheme': 'http',
                 'headers': [(b'authorization', ('Bearer guest.' + cookie).encode())],
                 'client': ('127.0.0.1', 1), 'server': ('localhost', 80), 'root_path': ''}
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(changed.wait(), 4)
        await asyncio.sleep(.35)  # ~3 more fake market refreshes (every .1 s) must not re-emit identical UI
        disconnect.set()
        await asyncio.wait_for(task, 3)
        assert [i['revision'] for i in ui_events] == [0, 1]
        assert ui_events[-1]['changes'] == {'theme': 'dark'}
        assert app.state.guests.sessions[cookie].streams == 0


async def test_ui_ack_consumes_applied_changes_without_losing_pending_commands(app):
    async with client(app) as c:
        identity = (await c.post('/api/auth/guest')).json()
        window = identity['windowId']
        await c.post('/api/guest/ui', json={'windowId': window, 'changes': {'newsOpen': True}})
        await c.post('/api/guest/ui', json={'windowId': window, 'snapshot': {'appliedRevision': 1}})
        assert (await c.get('/api/guest/ui')).json()['changes'] == {}
        await c.post('/api/guest/ui', json={'windowId': window, 'snapshot': {'appliedRevision': 1, 'newsOpen': False}})
        changed = (await c.post('/api/guest/ui', json={'windowId': window, 'changes': {'duration': 900}})).json()
        assert changed['changes'] == {'duration': 900}
        await c.post('/api/guest/ui', json={'windowId': window, 'changes': {'theme': 'dark'}})
        # An older or forged future receipt cannot consume outstanding commands.
        for revision in [2, 999, True]:
            await c.post('/api/guest/ui', json={'windowId': window, 'snapshot': {'appliedRevision': revision}})
            assert (await c.get('/api/guest/ui')).json()['changes'] == {'duration': 900, 'theme': 'dark'}
        denied = await c.post('/api/guest/ui', json={'windowId': 'foreign', 'snapshot': {'appliedRevision': 3}})
        assert denied.status_code == 403
        await c.post('/api/guest/ui', json={'windowId': window, 'snapshot': {'appliedRevision': 3}})
        assert (await c.get('/api/guest/ui')).json()['changes'] == {}


async def test_chinese_news_observation_fits_but_oversize_cannot_change_state(app):
    async with client(app) as reader:
        session = (await reader.post('/api/auth/guest')).json()
        snapshot = {'observation': {'text': '新闻摘要。' * 1300}, 'appliedRevision': 0}
        response = await reader.post('/api/guest/ui', json={'windowId': session['windowId'], 'snapshot': snapshot})
        assert response.status_code == 200
        oversized = await reader.post('/api/guest/ui', json={'windowId': session['windowId'],
            'changes': {'duration': 900}, 'snapshot': {'text': '中' * 22000}})
        assert oversized.status_code == 413
        state = (await reader.get('/api/guest/ui')).json()
        assert state['revision'] == 0
        assert state['changes'] == {}
        assert state['snapshot'] == snapshot


async def test_guest_workspace_read_only_controls_and_receipts(app):
    async with client(app) as c:
        window = (await c.post('/api/auth/guest')).json()['windowId']
        controls = (await c.get('/api/agent/capabilities')).json()['uiControls']['changes']
        assert {'about', 'feedback'} <= set(controls['settingsPage'])
        assert controls['disclosure'] == {'name': ['feedback-diagnostics'], 'open': 'boolean'}
        cases = [
            *({'panel': value} for value in (None, 'news')),
            *({'settingsOpen': value} for value in (False, True)),
            *({'settingsPage': value} for value in ('chart', 'ui', 'chart-advanced', 'about', 'feedback')),
            *({'dockSide': value} for value in ('left', 'right')),
            *({'dockWidth': value} for value in (280, 400, 520, 1600)),
            *({'fontScale': value} for value in ('standard', 'large', 'extra-large')),
            *({'density': value} for value in ('compact', 'standard', 'comfortable')),
            *({key: {'main': main, 'kd': kd, 'macd': macd}}
              for key in ('paneSizes', 'chartSizes') for main, kd, macd in ((34, 12, 12), (76, 36, 36), (50.5, 24.5, 25))),
            *({'chartOption': {'name': name, 'visible': visible}} for name in
              ('kd', 'macd', 'volume', 'crosshair', 'lastPriceLine', 'ma5', 'ma10', 'ma25', 'ma60', 'ma200')
              for visible in (True, False)),
            {'pinnedSymbols': {'add': 'SHFE.rb2610'}}, {'pinnedSymbols': {'remove': 'SHFE.rb2610'}},
            *({'dialog': {'name': name, 'open': opened}} for name in ('contract-drawer', 'market-session') for opened in (True, False)),
            *({'disclosure': {'name': 'feedback-diagnostics', 'open': opened}} for opened in (True, False)),
            {'presentation': {'reset': True}},
        ]
        for revision, changes in enumerate(cases, 1):
            assert changes.keys() <= controls.keys()
            response = await c.post('/api/guest/ui', json={'windowId': window, 'changes': changes})
            assert response.status_code == 200, response.text
            assert response.json()['changes'] == changes
            assert response.json()['revision'] == revision
            response = await c.post('/api/guest/ui', json={'windowId': window, 'snapshot': {'appliedRevision': revision}})
            assert response.json()['changes'] == {}
            assert response.json()['revision'] == revision


async def test_guest_workspace_invalid_commands_are_atomic(app):
    import json

    async with client(app) as c:
        window = (await c.post('/api/auth/guest')).json()['windowId']
        initial = (await c.post('/api/guest/ui', json={'windowId': window, 'changes': {'theme': 'dark'}})).json()
        cases = [
            # 'depth' (the removed 价格阶梯) now means the 下单面板, which a guest cannot open.
            *({'panel': value} for value in ('order', 'depth', 'strategy', 'ai', 'positions', {}, False)),
            *({'settingsPage': value} for value in ('account', 'voice', 'trade', 'data', 'admin', None)),
            {'settingsOpen': 1}, {'dockSide': 'top'}, {'fontScale': 'small'}, {'density': 'dense'},
            *({'dockWidth': value} for value in (279, 1601, True, 300.5, '300', None, float('inf'), float('nan'))),
            *({'chartSizes': value} for value in (None, [], {}, {'main': 50, 'kd': 25},
                {'main': 50, 'kd': 25, 'macd': 25, 'extra': 1},
                *({'main': value, 'kd': 25, 'macd': 25} for value in (True, '50', 33, 77, float('inf'), float('nan'))),
                {'main': 50, 'kd': 11, 'macd': 25}, {'main': 50, 'kd': 25, 'macd': 37})),
            *({'chartOption': value} for value in (None, {}, [], {'name': 'orders', 'visible': True},
                {'name': 'kd', 'visible': 1}, {'name': 'kd', 'visible': True, 'extra': 1})),
            *({'pinnedSymbols': value} for value in (None, {}, [], {'add': None}, {'add': 2},
                {'add': 'invalid'}, {'add': 'A.' + 'a' * 39}, {'add': 'SHFE.rb2610', 'remove': 'SHFE.rb2610'},
                {'add': 'SHFE.rb2610', 'remove': None}, {'add': 'SHFE.rb2610', 'extra': True})),
            *({'dialog': value} for value in (None, {}, [], {'name': 'quick-order', 'open': True},
                {'name': 'contract-drawer', 'open': 1}, {'name': 'contract-drawer', 'open': True, 'extra': True})),
            *({'disclosure': value} for value in (None, {}, [], {'name': 'voice-call', 'open': True},
                {'name': 'feedback-diagnostics', 'open': 1}, {'name': 'feedback-diagnostics', 'open': 'true'},
                {'name': 'feedback-diagnostics', 'open': True, 'id': None},
                {'name': 'feedback-diagnostics', 'open': True, 'id': 'private'},
                {'name': 'feedback-diagnostics', 'open': True, 'extra': 1})),
            *({'presentation': value} for value in (None, {}, [], {'reset': False}, {'reset': 1},
                {'reset': True, 'theme': 'dark'})),
        ]
        for changes in cases:
            # Raw JSON exercises rejection of non-finite numbers at the real HTTP boundary.
            response = await c.post('/api/guest/ui', content=json.dumps({
                'windowId': window, 'changes': {'theme': 'light', **changes},
                'snapshot': {'appliedRevision': 2},
            }), headers={'content-type': 'application/json'})
            assert response.status_code == 422, (changes, response.text)
        assert (await c.get('/api/guest/ui')).json() == initial


async def test_guest_workspace_private_commands_and_foreign_window_remain_forbidden(app):
    async with client(app) as c:
        window = (await c.post('/api/auth/guest')).json()['windowId']
        initial = (await c.post('/api/guest/ui', json={'windowId': window, 'changes': {'panel': 'news'}})).json()
        for changes in ({'view': 'admin'}, {'tradingAccount': 'other'}, {'positionsBar': 'expanded'},
                        {'strategyTab': 'library'}, {'selectedPrice': 100}, {'preference': {'key': 'ui.theme', 'value': 'light'}},
                        {'quickOrder': {'volume': 1}}, {'chatHistory': True}, {'researchDebate': {'start': 'desk'}},
                        {'researchDebate': {'open': 'db0123456789ab'}}):
            response = await c.post('/api/guest/ui', json={'windowId': window, 'changes': {'panel': 'news', **changes}})
            assert response.status_code == 403
        # The 辩论 page (2026-10-24) is a member's: a visitor's window cannot open it.
        assert (await c.post('/api/guest/ui', json={'windowId': window, 'changes': {'panel': 'debate'}})).status_code == 422
        response = await c.post('/api/guest/ui', json={'windowId': 'foreign', 'changes': {'settingsPage': 'chart'}, 'snapshot': {'appliedRevision': 1}})
        assert response.status_code == 403
        assert (await c.get('/api/guest/ui')).json() == initial


async def test_guest_plan_safety_defaults_and_global_voice_switch(app, monkeypatch):
    from types import SimpleNamespace

    from trader_runtime.skill_registry import availability_map, skill_allowed, skill_by_id

    store = app.state.store
    monkeypatch.setattr(app.state, 'voice_realtime', SimpleNamespace(_read_key=lambda: 'fixture-key'))
    async with client(app) as c:
        session = (await c.post('/api/auth/guest')).json()
        assert session['marketHistoryTradingDays'] == 3 and session['voiceEnabled']
        for value in (None, 0, True, '5', 99):
            store.update_plan_tier('guest', config={'marketHistoryTradingDays': value, 'voiceSecondsPerDay': None,
                'skills': ['voice-call', 'order-ops', 'market-replay'], 'models': None, 'dailyTokens': None})
            response = (await c.get('/api/guest/session')).json()
            assert response['marketHistoryTradingDays'] == 3 and not response['voiceEnabled']
            plan = plan_for_user(store, session['guestId'])
            assert plan.daily_tokens == 0 and plan.models == frozenset() and not plan.unlimited
        store.update_plan_tier('guest', config={'marketHistoryTradingDays': 5, 'voiceSecondsPerDay': 999, 'skills': ['voice-call']})
        assert app.state.guests.voice_limit() == 300
        assert (await c.get('/api/guest/session')).json()['marketHistoryTradingDays'] == 5
        store.set_skill_enabled('voice-call', False)
        assert app.state.guests.voice_limit() == 0
        assert (await c.post('/api/guest/voice/intent', json={'windowId': session['windowId'], 'text': '打开新闻'})).status_code == 403
        assert not (await c.get('/api/guest/session')).json()['voiceEnabled']
        reasons = availability_map(store, session['guestId'])
        assert reasons['voice-call'] == 'disabled' and reasons['order-ops'] == 'guest_read_only'
        assert not skill_allowed(store, session['guestId'], skill_by_id('order-ops'))
        for method, path, body in (
            ('GET', '/api/replays', None), ('POST', '/api/replays', {}),
            ('GET', '/api/markets/SHFE.rb2610/workbench', None),
            ('GET', '/api/simulation/orders', None), ('POST', '/api/simulation/orders/unknown/cancel', {}),
            ('GET', '/api/strategies', None),
        ):
            assert (await c.request(method, path, json=body)).status_code in (401, 403)
        with store.lock, store.connection:
            store.connection.execute("DELETE FROM plan_tiers WHERE name='guest'")
        response = (await c.get('/api/guest/session')).json()
        assert response['marketHistoryTradingDays'] == 3 and not response['voiceEnabled']
