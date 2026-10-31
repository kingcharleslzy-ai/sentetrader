"""Public HTTP/SSE behavior: shared updates, isolation and bounded reader lifetimes."""
import asyncio
import copy
import json
from collections import Counter
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from fastapi import HTTPException

from test_guest import app as app
from test_guest import client
from trader_runtime.account_pool import PoolClient, PoolStartRefused


class PublicSource:
    def __init__(self, app):
        self.provider = app.state.data_provider
        self.generations = Counter()
        self.signals = {}
        self.calls = Counter()
        self.active = Counter()
        self.failures = set()
        self.blocked = {}
        self.closed_polls = 0
        self.values = {}
        self.board_calls = 0
        self.requested = asyncio.Event()

    def change(self, symbol='SHFE.rb2610'):
        self.generations[symbol] += 1
        signal = self.signals.pop(symbol, None)
        if signal:
            signal.set()

    async def read(self, path, _fake):
        if path == '/v1/public-market/instruments':
            return self.provider.instrument_catalog()
        if path == '/v1/public-market/calendar':  # the catalog's delivery reminders read it (2026-10-12)
            return self.provider.market_calendar()
        if path == '/v1/public-market/board':
            self.board_calls += 1
            return self.provider.market_board()
        url = urlsplit(path)
        symbol = url.path.split('/')[-2]
        query = parse_qs(url.query)
        self.calls[symbol] += 1
        self.active[symbol] += 1
        self.requested.set()
        try:
            if symbol in self.blocked:
                await self.blocked[symbol].wait()
            if query.get('after') == [str(self.generations[symbol])]:
                await self.signals.setdefault(symbol, asyncio.Event()).wait()
            if symbol in self.failures:
                raise HTTPException(503, 'market_series_not_ready')
            if symbol not in self.values:
                self.values[symbol] = self.provider.market_workbench(symbol, int(query['duration'][0]))
            value = copy.deepcopy(self.values[symbol])
            value['revision'] = str(self.generations[symbol])
            value['dataStatus'] = 'normal'
            value['quote']['dataStatus'] = 'normal'
            value['quote']['lastPrice'] = 100 + self.generations[symbol]
            value['bars'][-1]['close'] = 100 + self.generations[symbol]
            return value
        finally:
            self.active[symbol] -= 1
            self.closed_polls += 1


@pytest.fixture
async def source(app, monkeypatch):
    source = PublicSource(app)
    service = app.state.guests
    service.market_service.market_account_id = 'tqkq-sim:specified-source'
    service.market_service.market_idle_seconds = .02
    monkeypatch.setattr(service.market_service, 'read_market', source.read)
    return source


class Stream:
    def __init__(self, app, cookie, *, symbol='SHFE.rb2610', slow=False):
        self.disconnect = asyncio.Event()
        self.release = asyncio.Event()
        self.arrived = asyncio.Event()
        self.frames = []
        self.slow = slow
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'method': 'GET',
                 'path': '/api/guest/stream', 'raw_path': b'/api/guest/stream',
                 'query_string': f'symbol={symbol}&duration=300'.encode(), 'scheme': 'http',
                 'headers': [(b'cookie', ('ai_trader_guest=' + cookie).encode())],
                 'client': ('127.0.0.1', 1), 'server': ('localhost', 80), 'root_path': ''}
        self.task = asyncio.create_task(app(scope, self.receive, self.send))

    async def receive(self):
        await self.disconnect.wait()
        return {'type': 'http.disconnect'}

    async def send(self, message):
        body = message.get('body', b'')
        if body:
            self.frames.append(body.decode())
            self.arrived.set()
            if self.slow and len(self.frames) == 1:
                await self.release.wait()

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate(self.frames):
                self.arrived.clear()
                await self.arrived.wait()

    async def close(self):
        self.disconnect.set()
        await asyncio.wait_for(self.task, 2)

    def market_frames(self):
        return [frame for frame in self.frames if frame.startswith(('event: snapshot', 'event: update'))]


async def identity(app, index=1):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(f'10.0.0.{index}', 1)),
                                base_url='http://localhost', headers={'origin': 'http://localhost'}) as c:
        response = await c.post('/api/auth/guest')
        assert response.status_code == 200
        cookie = c.cookies.get('ai_trader_guest')
        return cookie, app.state.guests.sessions[cookie]


async def test_100_sse_readers_share_one_producer_and_encoding(app, source, monkeypatch):
    import trader_runtime.shared_market as module
    encodes = Counter()
    original = module.event

    def encode(name, value):
        encodes[name] += 1
        return original(name, value)

    monkeypatch.setattr(module, 'event', encode)
    identities = await asyncio.gather(*(identity(app, i) for i in range(1, 101)))
    streams = [Stream(app, cookie) for cookie, _ in identities]
    try:
        await asyncio.gather(*(stream.until(lambda frames: len(frames) >= 2) for stream in streams))
        # One guest-tier projection (it carries historyTradingDays, #734), not 100; the raw packet no
        # guest reads is never encoded (2026-10-13 扩容①: its texts are built on first use).
        assert encodes['snapshot'] == 1 and encodes['update'] == 0
        assert source.calls['SHFE.rb2610'] == 2  # First frame + one pending long poll.
        assert source.active['SHFE.rb2610'] == 1
        source.change()
        await asyncio.gather(*(stream.until(lambda frames: any('event: update' in frame for frame in frames)) for stream in streams))
        assert encodes['snapshot'] == 2 and encodes['update'] == 1
        assert len({stream.market_frames()[-1] for stream in streams}) == 1
        assert source.active['SHFE.rb2610'] == 1
    finally:
        await asyncio.gather(*(stream.close() for stream in streams))
    assert all(guest.streams == 0 for _, guest in identities)
    await asyncio.sleep(.04)
    assert not app.state.guests.markets and not app.state.guests.market_tasks
    assert source.active['SHFE.rb2610'] == 0


async def test_slow_reader_skips_intermediate_updates_and_gets_full_snapshot(app, source):
    cookie, _ = await identity(app)
    slow, fast = Stream(app, cookie, slow=True), Stream(app, cookie)
    try:
        await slow.until(lambda frames: len(frames) == 1)
        await fast.until(lambda frames: len(frames) >= 2)
        for i in range(2):
            source.change()
            await fast.until(lambda frames: len(fast.market_frames()) == i + 2)
        assert len(slow.frames) == 1
        slow.release.set()
        await slow.until(lambda frames: len(slow.market_frames()) >= 2)
        latest = slow.market_frames()[-1]
        assert latest.startswith('event: snapshot')
        payload = json.loads(latest.split('data: ')[1])
        assert payload['quote']['lastPrice'] == 102
        assert payload['workbench']['bars']
        assert len(slow.market_frames()) == 2
    finally:
        await slow.close()
        await fast.close()


async def test_cold_and_failed_keys_do_not_block_updates_or_idle_ui(app, source, monkeypatch):
    cookie, guest = await identity(app)
    # A series still loading is waited for up to this bound (2026-10-14), then fails as before. The test ends
    # the cold read's wait itself: a real short bound expired on slow CI before the hot frames arrived.
    app.state.shared_market.series_wait_seconds = 60
    original_timeout = asyncio.timeout
    series_waits = set()

    @asynccontextmanager
    async def track_timeout(delay):
        async with original_timeout(delay) as deadline:
            if delay == 60:
                series_waits.add(deadline)
            try:
                yield deadline
            finally:
                series_waits.discard(deadline)

    monkeypatch.setattr(asyncio, 'timeout', track_timeout)
    hot = Stream(app, cookie)
    source.blocked['SHFE.au2612'] = asyncio.Event()
    # A member's cold read on another product (guests only see 螺纹).
    cold = asyncio.create_task(app.state.shared_market.market('SHFE.au2612', 300))
    try:
        await hot.until(lambda frames: len(frames) >= 2)
        source.change()
        await hot.until(lambda frames: len(hot.market_frames()) == 2)
        assert not cold.done()
        # No market update follows this command; it still wakes the SSE immediately.
        async with client(app) as c:
            c.cookies.set('ai_trader_guest', cookie)
            result = await c.post('/api/guest/ui', json={'windowId': guest.window, 'changes': {'theme': 'dark'}})
            assert result.status_code == 200
        await asyncio.wait_for(hot.until(lambda frames: any('"revision":1' in frame for frame in frames)), .25)
        source.failures.add('SHFE.au2612')
        source.blocked['SHFE.au2612'].set()
        for deadline in list(series_waits):
            deadline.reschedule(asyncio.get_running_loop().time())
        with pytest.raises(HTTPException, match='market_series_not_ready'):
            await cold
        assert ('SHFE.au2612', 300) not in app.state.guests.markets
        app.state.shared_market.series_wait_seconds = .3  # a failed read past this bound marks the chart unavailable
        source.change()
        await hot.until(lambda frames: len(hot.market_frames()) == 3)
        # Failure after a good frame explicitly revokes its normal status, then recovers.
        source.failures.add('SHFE.rb2610')
        source.change()
        await hot.until(lambda frames: '"dataStatus":"unavailable"' in frames[-1])
        source.failures.clear()
        await hot.until(lambda frames: '"dataStatus":"normal"' in frames[-1] and len(hot.market_frames()) >= 5)
    finally:
        cold.cancel()
        await asyncio.gather(cold, return_exceptions=True)
        await hot.close()


async def test_failed_first_frame_limit_expiry_and_get_only_reclaim(app, source, monkeypatch):
    service = app.state.guests
    now = [service.clock()]
    monkeypatch.setattr(service, 'clock', lambda: now[0])
    cookie, guest = await identity(app)
    original_timeout = asyncio.timeout
    session_timeouts = set()

    @asynccontextmanager
    async def track_timeout(delay):
        async with original_timeout(delay) as deadline:
            if delay == service.ttl:
                session_timeouts.add(deadline)
            try:
                yield deadline
            finally:
                session_timeouts.discard(deadline)

    monkeypatch.setattr(asyncio, 'timeout', track_timeout)
    app.state.shared_market.series_wait_seconds = .3  # a series still loading is waited for this long
    source.failures.add('SHFE.rb2610')
    async with client(app) as c:
        c.cookies.set('ai_trader_guest', cookie)
        response = await c.get('/api/guest/stream', params={'symbol': 'SHFE.rb2610'})
        assert response.status_code == 503
        assert guest.streams == 0 and not app.state.guests.markets
        source.failures.clear()
        # HTTP-only reads also acquire and retire the producer.
        response = await c.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'})
        assert response.status_code == 200
        await asyncio.sleep(.04)
        assert not app.state.guests.markets and not app.state.guests.market_tasks
        first, second = Stream(app, cookie), Stream(app, cookie, slow=True)
        try:
            await first.until(lambda frames: len(frames) >= 2)
            await second.until(lambda frames: len(frames) >= 1)
            assert guest.streams == 2
            response = await c.get('/api/guest/stream', params={'symbol': 'SHFE.rb2610'})
            assert response.status_code == 429
            assert response.json()['detail'] == 'guest_stream_limit'
            # Acquisition timeouts have exited; these are the two response deadlines.
            deadlines = tuple(session_timeouts)
            assert len(deadlines) == 2
            # Advancing the session clock alone cannot cancel a blocked ASGI send.
            # Expire the real timeouts without releasing or disconnecting either reader.
            now[0] = guest.expires
            for deadline in deadlines:
                deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.wait_for(asyncio.gather(first.task, second.task), 1)
            assert all(deadline.expired() for deadline in deadlines)
            assert not second.release.is_set() and len(second.frames) == 1
            assert guest.streams == 0
            await asyncio.sleep(.04)
            assert not app.state.guests.markets and not app.state.guests.market_tasks
            assert source.active['SHFE.rb2610'] == 0
        finally:
            await first.close()
            await second.close()


async def test_capacity_reuses_idle_key_and_shutdown_cancels_pending_reads(app, source):
    service = app.state.guests
    service.market_service.market_capacity = 2
    service.market_service.market_idle_seconds = 10
    first = await service.acquire_market('SHFE.rb2610', 300)
    second = await service.acquire_market('SHFE.au2612', 300)
    with pytest.raises(HTTPException, match='guest_market_capacity'):
        await service.acquire_market('SHFE.rb2610', 60)
    service.release_market(first)
    third = await service.acquire_market('SHFE.rb2610', 60)
    assert ('SHFE.rb2610', 300) not in service.markets
    await service.close()
    assert not service.markets and not service.market_tasks and not service.inflight
    assert sum(source.active.values()) == 0
    service.release_market(second)
    service.release_market(third)
    with pytest.raises(HTTPException, match='guest_market_closed'):
        await service.market('SHFE.rb2610', 300)


async def test_research_symbol_validation_never_opens_market_subscription(app, source, monkeypatch):
    async def brief(symbol, view):
        return {'symbol': symbol, 'view': view}
    monkeypatch.setattr(app.state.guests.ctx.services.research_brief, 'brief', brief)
    async with client(app) as c:
        await c.post('/api/auth/guest')
        response = await c.get('/api/guest/research/news', params={'symbol': 'shfe.rb2610'})
        assert response.status_code == 200 and response.json()['symbol'] == 'SHFE.rb2610'
        assert not source.calls and not app.state.guests.markets


async def test_public_http_pools_are_per_child_and_keep_long_polls_apart(monkeypatch):
    """号池行情读 (2026-10-12): one SidecarHttp per pool child, reused across reads; a K-line long
    poll rides the child's long-poll pool and a short read its short pool, each wide enough that
    48 parked channels never starve a quote; close() closes every pool."""
    made, paths = [], []
    original = httpx2.AsyncClient

    def make_client(**kwargs):
        assert kwargs['trust_env'] is False
        index = len(made)
        async def handle(request):
            paths.append(index)
            assert request.headers['authorization'] == 'Bearer secret'
            return httpx2.Response(200, json={'revision': '1'})
        value = original(**kwargs, transport=httpx2.MockTransport(handle))
        made.append((kwargs['base_url'], kwargs['limits'].max_connections, value))
        return value

    monkeypatch.setattr(httpx2, 'AsyncClient', make_client)
    pool = PoolClient('http://localhost:19000', 'secret')
    long_poll = '/v1/public-market/SHFE.rb2610/workbench?duration=300&after=1&waitMs=10000'
    await asyncio.gather(*(pool.market(19100, long_poll) for _ in range(100)),
                         pool.market(19100, '/v1/public-market/quotes?symbols=SHFE.RB2610'))
    await pool.market(19101, '/v1/public-market/board')
    assert [(base, limit) for base, limit, _ in made] == [
        ('http://127.0.0.1:19100', 48), ('http://127.0.0.1:19100', 64),
        ('http://127.0.0.1:19101', 48), ('http://127.0.0.1:19101', 64)]
    assert Counter(paths) == {1: 100, 0: 1, 2: 1}  # 100 long polls on 19100's long-poll pool
    with pytest.raises(PoolStartRefused, match='public_market_path_required'):
        await pool.market(19100, '/v1/market/SHFE.rb2610/workbench')
    await pool.close()
    assert all(client.is_closed for _, _, client in made)


async def test_disconnect_and_expiry_during_first_frame_cancel_upstream(app, source):
    for expired in (False, True):
        cookie, guest = await identity(app, 1 + int(expired))
        source.blocked['SHFE.rb2610'] = asyncio.Event()
        source.requested.clear()
        if expired:
            guest.expires = app.state.guests.clock() + .1
        stream = Stream(app, cookie)
        await asyncio.wait_for(source.requested.wait(), 1)
        if not expired:
            stream.disconnect.set()
        await asyncio.wait_for(stream.task, 1)
        await asyncio.sleep(0)
        assert guest.streams == 0
        assert not app.state.guests.markets and not app.state.guests.market_tasks
        assert source.active['SHFE.rb2610'] == 0


@pytest.mark.parametrize('path', ['/api/guest/market', '/api/guest/stream'])
async def test_first_frame_timeout_keeps_valid_identity_and_allows_retry(app, source, monkeypatch, path):
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, 'timeout', lambda delay: original_timeout(.02 if delay == 20 else delay))
    cookie, guest = await identity(app)
    source.blocked['SHFE.rb2610'] = asyncio.Event()
    async with client(app) as c:
        c.cookies.set('ai_trader_guest', cookie)
        response = await c.get(path, params={'symbol': 'SHFE.rb2610'})
        assert response.status_code == 503
        assert response.json()['detail'] == 'market_series_not_ready'
        assert (await c.get('/api/guest/session')).status_code == 200
        assert guest.streams == 0 and not app.state.guests.markets
        await asyncio.gather(*app.state.guests.market_tasks, return_exceptions=True)
        assert not app.state.guests.market_tasks and source.active['SHFE.rb2610'] == 0
        source.blocked['SHFE.rb2610'].set()
        assert (await c.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'})).status_code == 200


async def test_last_waiter_cancel_cannot_join_or_delete_replacement_fetch(app):
    service = app.state.guests
    key = ('cancel-race',)
    started, cleaning, finish_old, finish_new = (asyncio.Event() for _ in range(4))
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish_old.wait()
        await finish_new.wait()
        return {'generation': 2}

    old = asyncio.create_task(service.shared(key, fetch))
    await started.wait()
    old.cancel()
    await cleaning.wait()
    new = asyncio.create_task(service.shared(key, fetch))
    await asyncio.sleep(0)
    replacement = service.inflight[key]
    finish_old.set()
    await asyncio.gather(old, return_exceptions=True)
    assert service.inflight[key] is replacement and not replacement.cancelled()
    joined = asyncio.create_task(service.shared(key, fetch))
    finish_new.set()
    results = await asyncio.gather(new, joined)
    assert results == [{'generation': 2}, {'generation': 2}] and calls == 2
    assert not service.inflight and not service.shared_waiters


@pytest.mark.parametrize('revision', ['', None, 3])
async def test_invalid_public_revision_fails_closed_without_retry_spin(app, source, monkeypatch, revision):
    original = source.read
    reads = 0

    async def read(path, fake):
        nonlocal reads
        raw = await original(path, fake)
        if 'workbench' in path:
            reads += 1
            raw['revision'] = revision
        return raw

    monkeypatch.setattr(app.state.shared_market, 'read_market', read)
    async with client(app) as c:
        await c.post('/api/auth/guest')
        response = await c.get('/api/guest/market', params={'symbol': 'SHFE.rb2610'})
        assert response.status_code == 503 and reads == 1
        assert not app.state.guests.markets


def history_bars():
    from datetime import datetime
    days = ('2026-09-17', '2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24')
    return [{'datetime': int(datetime.fromisoformat(day + 'T09:00:00+08:00').timestamp()) * 1_000_000_000,
             'tradingDay': day, 'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1} for day in days]


async def test_limited_guest_sse_and_http_share_projection_without_raw_history(app, source, monkeypatch):
    import trader_runtime.shared_market as module
    symbol = 'SHFE.rb2610'
    bars = history_bars()
    source.values[symbol] = {**source.provider.market_workbench(symbol, 300), 'bars': bars}
    encodes = Counter()
    original = module.event
    def encode(name, value):
        encodes[name] += 1
        return original(name, value)
    monkeypatch.setattr(module, 'event', encode)
    identities = await asyncio.gather(*(identity(app, i) for i in range(1, 11)))
    now = [app.state.guests.clock()]
    monkeypatch.setattr(app.state.guests, 'clock', lambda: now[0])
    plan_reads = 0
    read_plan = app.state.store.get_plan_tier
    def counted_plan(name):
        nonlocal plan_reads
        plan_reads += 1
        return read_plan(name)
    monkeypatch.setattr(app.state.store, 'get_plan_tier', counted_plan)
    streams = [Stream(app, cookie) for cookie, _ in identities]
    try:
        await asyncio.gather(*(s.until(lambda frames: len(frames) >= 2) for s in streams))
        assert encodes == {'snapshot': 1}  # One tier projection, not ten; the raw packet stays unencoded.
        assert plan_reads == 1  # One SQLite tier read for all ten SSE readers.
        assert source.calls[symbol] == 2
        async with client(app) as c:
            await c.post('/api/auth/guest')
            reads_after_bootstrap = plan_reads
            response = (await c.get('/api/guest/market', params={'symbol': symbol})).json()
            assert plan_reads == reads_after_bootstrap
            assert [b['tradingDay'] for b in response['workbench']['bars']] == [b['tradingDay'] for b in bars[-3:]]
            assert response['workbench']['historyTradingDays'] == 3  # #734: the chart's 最近 N 个交易日 reads the frame.
            assert encodes == {'snapshot': 1}  # HTTP reuses the exact stream projection.
        old_id = app.state.shared_market.markets[symbol, 300].packet['id']
        source.change()
        await asyncio.gather(*(s.until(lambda frames: len(s.market_frames()) >= 2) for s in streams))
        assert encodes == {'snapshot': 2, 'update': 1}
        assert plan_reads == reads_after_bootstrap  # Same second, new tick: no tier reread.
        assert app.state.shared_market.markets[symbol, 300].packet['id'] != old_id
        assert len({s.market_frames()[-1] for s in streams}) == 1
        for stream in streams:
            for frame in stream.market_frames():
                assert not any(str(bar['datetime']) in frame for bar in bars[:-3])
        assert len(app.state.shared_market.markets[symbol, 300].packet['value']['workbench']['bars']) == 6
        # Tightening applies on an already-open stream, even without a new source version.
        app.state.store.update_plan_tier('guest', config={'marketHistoryTradingDays': 1})
        reads_after_edit = plan_reads
        now[0] += .5
        assert len((await app.state.guests.market(symbol, 300))['workbench']['bars']) == 3
        assert plan_reads == reads_after_edit  # No per-reader invalidation or polling.
        now[0] += .51
        app.state.shared_market.markets[symbol, 300].notify()
        await asyncio.gather(*(s.until(lambda frames: len(s.market_frames()) >= 3) for s in streams))
        for stream in streams:
            frame = stream.market_frames()[-1]
            assert frame.startswith('event: snapshot')
            assert len(json.loads(frame.split('data: ')[1])['workbench']['bars']) == 1
            # The open stream's label follows the new limit in the same frame as the re-trimmed bars (#734).
            assert json.loads(frame.split('data: ')[1])['workbench']['historyTradingDays'] == 1
        assert plan_reads == reads_after_edit + 1  # All readers refresh together after one second.
        assert source.calls[symbol] == 3  # Still only a single long-poll producer.
        next_bar = {**bars[-1], 'datetime': bars[-1]['datetime'] + 86400 * 1_000_000_000, 'tradingDay': '2026-09-25'}
        source.values[symbol]['bars'].append(next_bar)
        source.change()
        await asyncio.gather(*(s.until(lambda frames: len(s.market_frames()) >= 4) for s in streams))
        for stream in streams:
            frame = stream.market_frames()[-1]
            assert frame.startswith('event: update')
            patch = json.loads(frame.split('data: ')[1])['workbench']['barsPatch']
            assert patch['trimBefore'] == next_bar['datetime']
            assert [bar['tradingDay'] for bar in patch['bars']] == ['2026-09-25']
        # Reconnecting slow readers always receive the latest limited full snapshot.
        late = Stream(app, identities[0][0])
        try:
            await late.until(lambda frames: len(frames) >= 2)
            snapshot = json.loads(late.market_frames()[0].split('data: ')[1])
            assert [bar['tradingDay'] for bar in snapshot['workbench']['bars']] == ['2026-09-25']
        finally:
            await late.close()
    finally:
        await asyncio.gather(*(s.close() for s in streams))


@pytest.mark.parametrize('duration', [300, 3600, 86400])
async def test_history_limit_uses_bar_trading_days_on_weekends_and_daybars(app, source, duration):
    from datetime import datetime
    symbol = 'SHFE.rb2610'
    bars = history_bars()
    # A holiday annotation wins over the timestamp; no dependency on today's date.
    bars[0]['tradingDay'] = '2026-09-25'
    bars[-1].pop('tradingDay')
    bars.append({'datetime': -9999999999999999999999, 'close': 1})
    source.values[symbol] = {**source.provider.market_workbench(symbol, duration), 'bars': bars}
    app.state.store.update_plan_tier('guest', config={'marketHistoryTradingDays': 1})
    async with client(app) as c:
        await c.post('/api/auth/guest')
        response = await c.get('/api/guest/market', params={'symbol': symbol, 'duration': duration})
        assert response.status_code == 200
        assert response.json()['workbench']['bars'] == [bars[0]]
    # Missing annotation: Friday night and Saturday early morning both belong to Monday.
    if duration != 86400:
        dates = ('2026-09-24T09:00:00+08:00', '2026-09-25T09:00:00+08:00',
                 '2026-09-25T21:00:00+08:00', '2026-09-26T01:00:00+08:00')
    else:
        dates = ('2026-09-23T00:00:00+08:00', '2026-09-24T00:00:00+08:00', '2026-09-25T00:00:00+08:00')
    source.values[symbol]['bars'] = [
        {'datetime': int(datetime.fromisoformat(value).timestamp()) * 1_000_000_000, 'close': 1} for value in dates]
    channel = app.state.shared_market.markets[symbol, duration]
    old = channel.packet['id']
    source.change()
    async with asyncio.timeout(2):
        while channel.packet['id'] == old:
            await channel.changed.wait()
    value = await app.state.guests.market(symbol, duration)
    assert len(value['workbench']['bars']) == (1 if duration == 86400 else 2)
