"""Authenticated members and guests consume one public source without account assembly."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from test_ai_annotations import observation, ui_state
from test_guest_market_fanout import PublicSource
from test_owner_auth import ORIGIN, PASSWORD_HASH, SESSION_SECRET
from trader_runtime.account_pool import PoolStartRefused
from trader_runtime.auth import OWNER_COOKIE_NAME
from trader_runtime.main import create_app


@pytest.fixture
async def shared_app(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_TRADER_SHARED_MARKET_ACCOUNT_ID', 'tqkq-sim:public')
    monkeypatch.setenv('AI_TRADER_GUEST_MARKET_ACCOUNT_ID', 'must-not-use-legacy')
    app = create_app(tmp_path / 'shared.db', runtime_state_root=tmp_path / 'state',
        agent_mode='native', data_mode='fake', auth_mode='owner-password',
        owner_username='owner', owner_password_hash=PASSWORD_HASH,
        owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN})
    source = PublicSource(app)
    starts = []

    async def market_source(account):
        starts.append(account)
        return 19001

    async def market(port, path):
        assert port == 19001
        return await source.read(path, None)

    service = app.state.shared_market
    service.ctx.account_pool = SimpleNamespace(market_source=market_source, client=SimpleNamespace(market=market))
    members = []
    for username in ('alice', 'bob'):
        app.state.store.create_cloud_user(username=username, password_hash=PASSWORD_HASH, invited_by=None)
        c = AsyncClient(transport=ASGITransport(app=app), base_url='https://runtime.test', headers={'origin': ORIGIN})
        c.cookies.set(OWNER_COOKIE_NAME, app.state.auth._issue_session(username, PASSWORD_HASH))
        members.append(c)
    guest = AsyncClient(transport=ASGITransport(app=app), base_url='https://runtime.test', headers={'origin': ORIGIN})
    assert (await guest.post('/api/auth/guest')).status_code == 200
    yield app, source, starts, members, guest
    for c in [*members, guest]:
        await c.aclose()
    await service.close()
    await app.state.adapter.shutdown()
    app.state.store.close()


async def test_members_and_guest_share_source_and_keep_private_permissions(shared_app):
    app, source, starts, members, guest = shared_app
    # Inject accidental private fields at the source boundary; none may escape.
    raw = source.provider.market_workbench('SHFE.rb2610', 300)
    raw.update(account={'balance': 12345}, positions=[{'volume': 9}])
    raw['quote']['accountId'] = 'private-source-account'
    source.values['SHFE.rb2610'] = raw
    responses = await asyncio.gather(
        *(c.get('/api/markets/SHFE.rb2610/workbench') for c in members),
        guest.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'}))
    assert all(r.status_code == 200 for r in responses)
    # The guest's copy only adds its history limit (#734); members' packets never carry one.
    guest_workbench = responses[2].json()['workbench']
    assert guest_workbench.pop('historyTradingDays') == 3 and 'historyTradingDays' not in responses[0].json()
    assert responses[0].json() == responses[1].json() == guest_workbench
    assert all('private-source-account' not in r.text and 'positions' not in r.text and 'balance' not in r.text for r in responses)
    assert starts == ['tqkq-sim:public']
    assert source.calls['SHFE.rb2610'] == 2  # First frame and the single pending long poll.
    assert len(app.state.shared_market.markets) == 1
    for c in members:
        quote = await c.get('/api/market/rb2610')
        assert quote.status_code == 200
        payload = quote.json()
        assert payload['price'] == responses[0].json()['quote']['lastPrice']
        assert {'turnId', 'symbol', 'name', 'price', 'priceCurve', 'summary', 'trend', 'updatedAt', 'capturedAt', 'ageSeconds'} <= payload.keys()
        assert (await c.get('/api/instruments', params={'search': 'rb2610'})).json()['count'] == 1
        assert (await c.get('/api/positions')).status_code == 403
        assert (await c.get('/api/account/summary')).status_code == 403
        assert (await c.get('/api/market/missing')).status_code == 404
        assert (await c.get('/api/markets/SHFE.rb2610/workbench?duration=17')).status_code == 422
    assert (await guest.get('/api/instruments')).status_code == 401
    assert (await guest.get('/api/positions')).status_code == 401


async def test_the_period_whitelist_accepts_30m_and_weekly_for_members_guests_and_tools(shared_app):
    """30 分与周线 (2026-10-02) 加入展示周期白名单；两档之间的值仍拒绝。游客与 AI 工具同一份白名单。"""
    _, source, _, members, guest = shared_app
    for duration in (1800, 604800):
        source.values.pop('SHFE.rb2610', None)  # the stand-in caches one workbench per symbol
        member = await members[0].get('/api/markets/SHFE.rb2610/workbench', params={'duration': duration})
        assert member.status_code == 200, member.text
        assert member.json()['durationSeconds'] == duration
        public = await guest.get('/api/guest/market', params={'symbol': 'SHFE.rb2610', 'duration': duration})
        assert public.status_code == 200, public.text
    assert (await members[0].get('/api/markets/SHFE.rb2610/workbench?duration=1200')).status_code == 422
    tool = await members[1].post('/api/agent/call', json={'operation': 'tool:get_chart_bars',
                                                           'body': {'symbol': 'SHFE.rb2610', 'durationSeconds': 604800}})
    assert tool.status_code == 200 and tool.json()['ok'], tool.text


async def test_pure_ai_tools_use_shared_source_without_a_private_session(shared_app):
    app, source, starts, members, _ = shared_app
    tools = [
        ('search_instruments', {'search': 'rb2610'}),
        ('get_market_snapshot', {'symbol': 'SHFE.rb2610'}),
        ('get_market_analysis', {'symbol': 'SHFE.rb2610', 'durationSeconds': 300}),
        ('get_chart_bars', {'symbol': 'SHFE.rb2610', 'durationSeconds': 300}),
    ]
    for member in members:
        for name, body in tools:
            response = await member.post('/api/agent/call', json={'operation': 'tool:' + name, 'body': body})
            assert response.status_code == 200, response.text
            assert response.json()['ok'], response.text
    assert not app.state.app_capabilities.sessions
    assert source.calls['SHFE.rb2610'] == 2
    assert starts == ['tqkq-sim:public']


async def test_shared_source_failures_never_fall_back_and_guest_switch_is_independent(shared_app):
    app, source, starts, members, _ = shared_app
    app.state.guests.enabled = False
    assert (await members[0].get('/api/instruments')).status_code == 200
    service = app.state.shared_market
    service.cache.clear()

    async def missing(account):
        raise PoolStartRefused('account_unavailable')

    service.ctx.account_pool.market_source = missing
    response = await members[0].get('/api/markets/SHFE.rb2610/workbench')
    assert response.status_code == 503
    assert not source.calls and not service.markets


async def test_chart_tool_reads_only_the_callers_window_without_private_account(shared_app):
    app, _, _, members, _ = shared_app
    window = str(uuid4())
    pulse = {"windowId": window, "revision": 0,
             "state": ui_state(symbol="SHFE.rb2610", duration=300), "observation": observation()}
    assert (await members[0].post('/api/ui/pulse', json=pulse)).status_code == 200
    call = {'operation': 'tool:get_chart_bars', 'body': {'uiWindowId': window}}
    result = await members[0].post('/api/agent/call', json=call)
    assert result.json()['ok'], result.text
    assert result.json()['data']['data']['symbol'] == 'SHFE.rb2610'
    other = await members[1].post('/api/agent/call', json=call)
    assert not other.json()['ok']
    assert not app.state.app_capabilities.sessions


def test_empty_neutral_configuration_preserves_legacy_source(monkeypatch):
    from trader_runtime.shared_market import SharedMarketService
    monkeypatch.setenv('AI_TRADER_SHARED_MARKET_ACCOUNT_ID', '  ')
    monkeypatch.setenv('AI_TRADER_GUEST_MARKET_ACCOUNT_ID', 'tqkq-sim:legacy')
    assert SharedMarketService(SimpleNamespace()).market_account_id == 'tqkq-sim:legacy'


@pytest.mark.parametrize('captured,quote_time,source_age,expected_age', [
    ('2026-09-26T12:00:00+00:00', '2026-09-26T20:00:00', 0, 2),
    ('2026-09-26T12:00:00+00:00', '2026-09-25T15:00:00+08:00', 104400, 104402),
    (None, None, None, None),
])
async def test_snapshot_reports_actual_quote_age(shared_app, captured, quote_time, source_age, expected_age):
    from datetime import datetime
    app, source, _, members, _ = shared_app
    service = app.state.shared_market
    service.clock = lambda: datetime.fromisoformat('2026-09-26T12:00:02+00:00').timestamp()
    raw = source.provider.market_workbench('SHFE.rb2610', 300)
    raw['capturedAt'], raw['ageSeconds'], raw['quote']['quoteDatetime'] = captured, source_age, quote_time
    source.values['SHFE.rb2610'] = raw
    response = await members[0].get('/api/market/SHFE.rb2610')
    if expected_age is None:
        assert response.status_code == 503 and response.json()['detail'] == 'market_quote_time_unavailable'
    else:
        assert response.status_code == 200
        assert response.json()['ageSeconds'] == expected_age
        assert response.json()['updatedAt'] == ('刚刚' if expected_age < 5 else f'{expected_age // 60} 分钟前')


async def test_loading_catalog_is_not_an_unknown_symbol_and_recovers(shared_app):
    app, _, _, members, guest = shared_app
    service = app.state.shared_market
    ready = await service.instruments()
    service.cache[("catalog",)] = (service.clock() + 2, {**ready, "catalogStatus": "loading", "items": []})
    for client, path in ((members[0], '/api/instruments'), (guest, '/api/guest/instruments')):
        response = await client.get(path)
        assert response.status_code == 200 and response.json()['catalogStatus'] == 'loading'
    waiting = await members[0].get('/api/markets/SHFE.rb2610/workbench')
    assert waiting.status_code == 503 and waiting.json()['detail'] == 'market_series_not_ready'
    service.cache[("catalog",)] = (service.clock() + 2, ready)
    response = await members[0].get('/api/markets/SHFE.rb2610/workbench')
    assert response.status_code == 200 and response.json()['bars']


async def test_market_briefs_use_shared_data_and_fail_without_private_fallback(shared_app, monkeypatch):
    app, source, _, members, _ = shared_app
    monkeypatch.setattr(app.state.market_briefs, 'enabled', lambda user: True)
    response = await members[0].get('/api/app/briefs', params={'symbol': 'SHFE.rb2610'})
    assert response.status_code == 200
    assert source.calls['SHFE.rb2610'] >= 1
    channel = app.state.shared_market.markets[('SHFE.rb2610', 60)]
    assert channel.packet['value']['workbench']['quote']['lastPrice'] == 100
    app.state.shared_market.series_wait_seconds = .05  # a series still loading past the bound has failed
    source.failures.add('SHFE.rb2610')
    source.change('SHFE.rb2610')
    for _ in range(200):
        if channel.error is not None:
            break
        await asyncio.sleep(.01)
    assert channel.error is not None
    response = await members[0].get('/api/app/briefs', params={'symbol': 'SHFE.rb2610'})
    assert response.status_code == 200 and response.json()['items'] == []


async def test_board_route_serves_members_from_one_shared_read_and_refuses_guests(shared_app):
    from trader_runtime.shared_market import BOARD_ROW_FIELDS
    app, source, starts, members, guest = shared_app
    denied = await guest.get('/api/market/board')
    assert denied.status_code == 403 and denied.json()['detail'] == 'guest_board_unavailable'
    responses = await asyncio.gather(*(c.get('/api/market/board') for c in members))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    payload = responses[0].json()
    assert responses[1].json() == payload
    assert payload['truncated'] is False and payload['dataStatus'] == 'simulated'
    row = next(item for item in payload['rows'] if item['productId'] == 'rb')
    assert row['symbol'] == 'SHFE.rb2610' and row['instrumentId'] == 'rb2610'
    assert row['productName'] == '螺纹钢' and row['changePercent'] == pytest.approx(-0.42)
    assert set(row) == BOARD_ROW_FIELDS  # the projection is the whole row, nothing private rides along
    # One second of sharing: concurrent polls and the immediate re-read are one upstream hit.
    assert (await members[0].get('/api/market/board')).status_code == 200
    assert source.board_calls == 1 and starts == ['tqkq-sim:public']
    app.state.shared_market.cache.clear()
    assert (await members[0].get('/api/market/board')).status_code == 200
    assert source.board_calls == 2
    assert not app.state.app_capabilities.sessions


async def test_guest_history_projection_does_not_clip_member_http_or_ai(shared_app):
    from test_guest_market_fanout import history_bars
    app, source, starts, members, guest = shared_app
    symbol = 'SHFE.rb2610'
    source.values[symbol] = {**source.provider.market_workbench(symbol, 86400), 'bars': history_bars()}
    public = await guest.get('/api/guest/market', params={'symbol': symbol, 'duration': 86400})
    assert len(public.json()['workbench']['bars']) == 3
    for member in members:
        response = await member.get(f'/api/markets/{symbol}/workbench?duration=86400')
        assert response.status_code == 200
        assert len(response.json()['bars']) == 6
        response = await member.post('/api/agent/call', json={'operation': 'tool:get_chart_bars',
            'body': {'symbol': symbol, 'durationSeconds': 86400}})
        assert response.status_code == 200 and response.json()['ok']
        assert '2026-09-17' in response.text
    assert source.calls[symbol] == 2 and starts == ['tqkq-sim:public']
    assert not app.state.app_capabilities.sessions


async def test_a_member_chart_keeps_a_full_trading_day_of_one_minute_bars(shared_app):
    """分时图要一整个交易日的 1 分钟线（黄金夜盘+日盘约 555 根）：sidecar 给 600 根，
    Runtime 的数据包不能再截成 500（10-02 线上实测只剩 500 根）。"""
    app, source, starts, members, guest = shared_app
    symbol = 'SHFE.au2612'
    base = source.provider.market_workbench(symbol, 60)
    template = base['bars'][-1]
    start = int(template['datetime']) - 599 * 60_000_000_000
    bars = [{**template, 'datetime': start + index * 60_000_000_000} for index in range(600)]
    source.values[symbol] = {**base, 'bars': bars}
    response = await members[0].get(f'/api/markets/{symbol}/workbench?duration=60')
    assert response.status_code == 200
    assert len(response.json()['bars']) == 600


# The client's marketCache.test.ts asserts the same digest for the same bars: one canonical text, both sides.
DIGEST_BARS = [
    {'datetime': 1_791_811_500_000_000_000, 'open': 3087, 'high': 3087.5, 'low': 3084, 'close': 3085.2,
     'volume': 6472, 'openInterest': 1684859, 'tradingDay': '2026-10-13'},
    {'datetime': 1_791_811_800_000_000_000, 'open': 812.34, 'high': 0.1 + 0.2, 'low': 3084, 'close': 3085,
     'volume': 1555.0, 'openInterest': 1685064},
]
DIGEST = '17427239'


def test_bars_digest_matches_the_client_vector():
    from trader_runtime.shared_market import bars_digest
    assert bars_digest(DIGEST_BARS) == DIGEST
    assert bars_digest([]) == '00000000'
    assert bars_digest([DIGEST_BARS[0], {**DIGEST_BARS[1], 'close': 3085.001}]) != DIGEST  # a corrected close
    assert bars_digest([{**DIGEST_BARS[0], 'tradingDay': '2026-10-14'}, DIGEST_BARS[1]]) != DIGEST  # calendar change


async def test_member_workbench_since_carries_only_newer_bars_with_a_window_digest(shared_app):
    from trader_runtime.shared_market import bars_digest
    _app, source, _starts, members, _guest = shared_app
    member = members[0]
    path = '/api/markets/SHFE.rb2610/workbench'
    full = (await member.get(path, params={'duration': 300})).json()
    bars = full['bars']
    since = bars[-3]['datetime']
    delta = (await member.get(path, params={'duration': 300, 'since': since})).json()
    assert delta['bars'] == bars[-2:]
    assert delta['barsAfter'] == since
    assert delta['barsWindow'] == {'first': bars[0]['datetime'], 'count': len(bars), 'digest': bars_digest(bars[:-2])}
    assert delta['quote'] == full['quote']
    # A since the window does not hold (older than its first bar, or not a bar time) answers the whole window.
    for unknown in (bars[0]['datetime'] - 1, 1):
        whole = (await member.get(path, params={'duration': 300, 'since': unknown})).json()
        assert whole['bars'] == bars and 'barsAfter' not in whole
    # The source corrects an old bar: the same since now answers a different digest.
    corrected = source.provider.market_workbench('SHFE.rb2610', 300)
    corrected['bars'][5]['close'] += 1
    source.values['SHFE.rb2610'] = corrected
    source.change()
    for _ in range(100):
        await asyncio.sleep(0.01)
        again = (await member.get(path, params={'duration': 300, 'since': since})).json()
        if again['barsWindow']['digest'] != delta['barsWindow']['digest']:
            break
    assert again['barsWindow']['digest'] == bars_digest(corrected['bars'][:-2])
    assert (await member.get(path, params={'duration': 300, 'since': -1})).status_code == 422
