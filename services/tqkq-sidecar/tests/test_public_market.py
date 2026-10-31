from __future__ import annotations

import asyncio
import copy
import json
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd
import pytest

from test_kline_series import Serial, sorted_days
from tqkq_readonly_sidecar.kline_series import SeriesRenderer, bars_digest
from tqkq_readonly_sidecar.main import (
    FixtureSession,
    OfficialTqKqSession,
    ReadOnlyServer,
    SnapshotService,
    fixture_instrument_catalog,
    last_outside_day_range,
)
from tqkq_readonly_sidecar.public_market import SERIES_KEY, PublicMarketService, PublicMarketWorker

SYMBOL = fixture_instrument_catalog()[0]['symbol']
OTHER = fixture_instrument_catalog()[1]['symbol']
TOKEN = 'public-market-test-token'


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class ControlledWorker:
    def __init__(self, credentials, changed, **kwargs):
        self.changed = changed
        self.started = time.monotonic()
        self.keys = set()
        self.snapshots = {}
        self.catalog = fixture_instrument_catalog()
        self.error = None
        self.healthy = True
        self.closed = False
        self.subscribe_calls = []
        self.board_rows = {}

    def subscribe(self, key):
        if key not in self.keys:
            self.subscribe_calls.append(key)
        self.keys.add(key)

    def publish(self, key, price=100):
        payload = FixtureSession([]).market_workbench(*key)
        payload['quote']['lastPrice'] = price
        self.snapshots[key] = payload
        self.changed()

    def publish_board(self, symbols, status='normal', *, data=True):
        # A rendered row carries its quote's time / 昨结算 when the entity had data; the failed
        # placeholder and an entity still empty after its first-data wait carry neither.
        for symbol in symbols:
            filled = data and status != 'market_quote_unavailable'
            self.board_rows[symbol] = {'symbol': symbol, 'status': status,
                                       'preSettlement': 100 if filled else None, 'quoteTime': None}
        self.changed()

    def board_read(self):
        return dict(self.board_rows)

    def read(self):
        return self.catalog, dict(self.snapshots), set(self.keys), self.error, self.healthy

    def close(self):
        self.closed = True
        self.changed()


@pytest.fixture
def market():
    workers = []

    def factory(*args, **kwargs):
        worker = ControlledWorker(*args, **kwargs)
        workers.append(worker)
        return worker

    service = PublicMarketService(lambda: ('test', 'secret'), worker_factory=factory,
                                  ready_wait=0.1, retry_seconds=0.2, warm_timeout=0.15,
                                  fixture=True)
    yield service, workers
    service.close()
    assert all(worker.closed for worker in workers)


def request_key(service, key):
    try:
        return service.workbench(*key)
    except RuntimeError as error:
        assert str(error) in {'market_series_not_ready', 'market_subscription_limit'}


def test_hot_and_cold_requests_are_independent_deduplicated_and_bounded(market):
    service, workers = market
    service.max_series = 2
    key = (SYMBOL, 300)
    request_key(service, key)
    workers[0].publish(key)
    with ThreadPoolExecutor(max_workers=4) as executor:
        cold = executor.submit(request_key, service, (OTHER, 300))
        started = time.monotonic()
        for _ in range(5):
            assert service.workbench(*key)['quote']['lastPrice'] == 100
        assert time.monotonic() - started < 0.08
        cold.result()
    assert workers[0].subscribe_calls.count(key) == 1
    with pytest.raises(RuntimeError, match='market_subscription_limit'):
        service.workbench(SYMBOL, 60)
    assert len(service._leases) == 2
    assert len(workers[0].keys) == 2


def test_idle_closes_actual_worker_and_reopen_revision_changes(market):
    service, workers = market
    service.lease_seconds = 0.15
    key = (SYMBOL, 300)
    request_key(service, key)
    workers[0].publish(key)
    original = service.workbench(*key)['revision']
    eventually(lambda: workers[0].closed)
    request_key(service, key)
    assert len(workers) == 2
    workers[1].publish(key)
    assert service.workbench(*key)['revision'] != original


@pytest.mark.parametrize('success', [True, False])
def test_partial_idle_warm_swap_preserves_hot_data_and_failed_candidate_closes(market, success):
    service, workers = market
    service.max_series = 2
    service.lease_seconds = 0.4
    service.warm_timeout = 0.2
    hot, idle = (SYMBOL, 300), (OTHER, 300)
    request_key(service, hot)
    request_key(service, idle)
    workers[0].publish(hot)
    workers[0].publish(idle)
    # Expire only the unobserved series while the hot request keeps its lease.
    with service._condition:
        service._leases[idle] = 0
        service._condition.notify_all()
    eventually(lambda: len(workers) == 2)
    assert service.workbench(*hot)['quote']['lastPrice'] == 100
    assert workers[1].keys == {hot}
    if success:
        workers[1].publish(hot, 200)
        eventually(lambda: workers[0].closed)
        assert service.workbench(*hot)['quote']['lastPrice'] == 200
        assert not workers[1].closed
    else:
        workers[1].error = 'public_market_unavailable'
        workers[1].changed()
        eventually(lambda: workers[1].closed)
        assert service.workbench(*hot)['quote']['lastPrice'] == 100
        assert not workers[0].closed


@pytest.mark.parametrize('placeholder', ['unavailable', 'empty', False])
def test_candidate_with_placeholder_board_rows_never_replaces_a_healthy_worker(market, placeholder):
    """516-1：候选 worker 行情板里还是 market_quote_unavailable 占位行时不算就绪——
    健康的旧 worker 不被换下，候选到 warm_timeout 丢弃；#516：首包为空实体、5 秒后按原样
    发布的行（盘前状态，但既无行情时间也无昨结算）同样不算；盘前合法行（pre_open，
    只有 preSettlement、还没有成交价）照常切换。"""
    service, workers = market
    service.lease_seconds = 5
    service.warm_timeout = 0.2
    key = (SYMBOL, 300)
    request_key(service, key)
    workers[0].publish(key)
    symbols = [item['symbol'] for item in workers[0].catalog]
    workers[0].publish_board(symbols)
    assert service.board()['rows']  # 行情板兴趣挂上，读的是 worker[0] 的正常行
    # 老 worker 报错 → 起候选；候选只差行情板这一门。
    workers[0].error = 'public_market_unavailable'
    workers[0].changed()
    eventually(lambda: len(workers) == 2)
    workers[1].publish(key)
    workers[1].publish_board(
        symbols, 'market_quote_unavailable' if placeholder == 'unavailable' else 'market_quote_pre_open',
        data=placeholder != 'empty')
    workers[1].changed()
    if placeholder:
        eventually(lambda: workers[1].closed)  # warm_timeout 到期丢弃，不切换
        assert not workers[0].closed
        assert service.workbench(*key)['quote']['lastPrice'] == 100
    else:
        eventually(lambda: workers[0].closed)  # 盘前行是真数据：照常切换
        assert not workers[1].closed
        assert service.workbench(*key)['quote']['lastPrice'] == 100


def test_disconnection_and_staleness_change_revision_without_changing_prices(market):
    service, workers = market
    service.fixture = False
    key = (SYMBOL, 300)
    request_key(service, key)
    workers[0].publish(key)
    first = service.workbench(*key)
    workers[0].healthy = False
    disconnected = service.workbench(*key, after=first['revision'], wait_ms=100)
    assert disconnected['revision'] != first['revision']
    assert disconnected['dataStatus'] == disconnected['quote']['dataStatus'] == 'stale'
    assert disconnected['dataErrorCode'] == disconnected['quote']['dataErrorCode'] == 'public_market_disconnected'
    workers[0].healthy = True
    recovered = service.workbench(*key)
    assert recovered['revision'] != disconnected['revision']
    time.sleep(0.01)
    service.stale_after_seconds = 0.001
    stale = service.workbench(*key)
    assert stale['revision'] != recovered['revision']
    assert stale['dataStatus'] == 'stale'


def http(server, path, token=TOKEN):
    req = Request(f'http://127.0.0.1:{server.server_port}{path}',
                  headers={'Authorization': f'Bearer {token}'})
    try:
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        return response.code, json.load(response)


def test_authenticated_http_public_payload_longpoll_and_errors_without_trading_actor(market):
    public, workers = market
    service = SnapshotService(username=None, password=None, symbols=[], fixture=True)
    service.public_market.close()
    service.public_market = public
    # Never start the private actor. Any fallback to it would fail this test.
    server = ReadOnlyServer(('127.0.0.1', 0), TOKEN, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    route = f'/v1/public-market/{SYMBOL}/workbench?duration=300'
    try:
        assert http(server, route, 'wrong')[0] == 401
        code, catalog = http(server, '/v1/public-market/instruments')
        assert code == 200 and catalog['items']
        assert http(server, route)[0] == 503
        workers[0].publish((SYMBOL, 300))
        code, first = http(server, route)
        assert code == 200 and first['bars'] and first['revision']
        assert first['liveTrading'] == 'disabled'
        assert not {'account', 'positions', 'orders', 'trades'} & first.keys()
        with ThreadPoolExecutor() as executor:
            pending = executor.submit(http, server, route + f'&after={first["revision"]}&waitMs=1000')
            time.sleep(0.04)
            assert not pending.done()
            workers[0].publish((SYMBOL, 300), 123)
            code, changed = pending.result()
        assert code == 200 and changed['revision'] != first['revision']
        assert changed['quote']['lastPrice'] == 123
        code, unchanged = http(server, route + f'&after={changed["revision"]}&waitMs=50')
        assert unchanged['revision'] == changed['revision']
        assert http(server, '/v1/public-market/UNKNOWN/workbench')[0] == 404
        assert http(server, route.replace('300', '301'))[0] == 400
        assert service._commands.empty() and service._worker is None
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_fixture_http_works_without_private_session():
    service = SnapshotService(username=None, password=None, symbols=[], fixture=True)
    try:
        data = service.public_market.workbench(SYMBOL, 300)
        assert data['dataStatus'] == 'simulated' and data['bars']
        assert service._commands.empty() and service._worker is None
    finally:
        service.stop()


class FakeApi:
    """A real asyncio loop exercises create_task/await; `touch` + `is_changing` report updates."""
    def __init__(self, credentials):
        self.owner = threading.get_ident()
        self.loop = asyncio.new_event_loop()
        self.tasks = []
        self.waiting = set()
        self.release = set()
        self.max_loading = 0
        self.closed = False
        self.calls = []
        self.price = 100
        self.calendar_calls = []
        # is_changing: what the last wait_update changed; `touch` (on the SDK loop) marks the next one.
        self.pending = set()
        self.changed = set()

    def touch(self, *items):
        self.pending.update(id(item) for item in items)

    def is_changing(self, item):
        self.check()
        return id(item) in self.changed

    def advance(self):
        self.changed, self.pending = self.pending, set()
        return bool(self.changed)

    def check(self):
        assert threading.get_ident() == self.owner

    def create_task(self, task):
        self.check()
        result = self.loop.create_task(task)
        self.tasks.append(result)
        return result

    def get_quote(self, symbol):
        self.check()
        assert asyncio.get_running_loop() == self.loop
        return object()

    async def get_kline_serial(self, symbol, duration, data_length):
        self.check()
        assert asyncio.get_running_loop() == self.loop
        assert data_length == 256
        key = (symbol, duration)
        self.calls.append(key)
        self.waiting.add(key)
        self.max_loading = max(self.max_loading, len(self.waiting))
        try:
            while key not in self.release:
                await asyncio.sleep(0.005)
            return object()
        finally:
            self.waiting.discard(key)

    async def get_trading_status(self, symbol):
        raise RuntimeError('optional permission unavailable')

    def get_trading_calendar(self, start, end):
        self.check()
        self.calendar_calls.append((start, end, self.loop.is_running(), len(self.calls)))
        dates = pd.date_range(start, end)
        return {'date': dates, 'trading': [date.weekday() < 5 for date in dates]}

    def is_serial_ready(self, serial):
        self.check()
        return True

    def wait_update(self, deadline):
        self.check()
        self.loop.run_until_complete(asyncio.sleep(0.005))
        return self.advance()

    def close(self):
        self.check()
        for task in self.tasks:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*self.tasks, return_exceptions=True))
        self.loop.close()
        self.closed = True


def test_sdk_async_cold_limit_updates_and_close_on_owner_thread(monkeypatch):
    apis = []

    def factory(credentials):
        api = FakeApi(credentials)
        apis.append(api)
        return api

    async def catalog(worker):
        worker.catalog = fixture_instrument_catalog()
        worker._changed()

    class Notify:
        def __init__(self, api):
            api.check()
        def get_notifies(self):
            return []

    def render(worker, symbol, duration):
        return {'schemaVersion': 1, 'quote': {'symbol': symbol, 'lastPrice': worker.api.price},
                'durationSeconds': duration, 'bars': [{'close': 1}]}

    monkeypatch.setitem(sys.modules, 'tqsdk', SimpleNamespace(TqNotify=Notify))
    monkeypatch.setattr(PublicMarketWorker, '_catalog', catalog)
    monkeypatch.setattr(PublicMarketWorker, '_render', render)
    worker = PublicMarketWorker(('test', 'secret'), lambda *topics: None, api_factory=factory,
                                max_series=10, max_cold=2)
    keys = [(SYMBOL, i) for i in range(10)]
    try:
        for key in keys:
            worker.subscribe(key)
        eventually(lambda: apis and len(apis[0].waiting) == 2)
        api = apis[0]
        assert len(api.calls) == 2
        hot = api.calls[0]
        api.release.add(hot)
        eventually(lambda: hot in worker.read()[1])
        eventually(lambda: len(api.calls) == 3)
        assert api.max_loading == 2
        worker.subscribe(hot)
        assert api.calls.count(hot) == 1
        with pytest.raises(RuntimeError, match='market_subscription_limit'):
            worker.subscribe((OTHER, 300))
        # An SDK notification updates the already-hot series while two cold
        # downloads remain blocked. The channel itself stores only one signal.
        previous = worker.read()[1][hot]['quote']['lastPrice']
        api.price += 1
        api.loop.call_soon_threadsafe(api.touch, worker.quotes[SYMBOL])
        eventually(lambda: worker.read()[1][hot]['quote']['lastPrice'] > previous)
    finally:
        worker.close()
    assert worker.closed and api.closed and not api.waiting
    assert api.owner != threading.get_ident()


def test_warm_candidate_shares_service_cold_budget_and_cannot_starve_serving_worker(monkeypatch):
    apis = []
    slots = threading.BoundedSemaphore(8)

    def factory(credentials):
        api = FakeApi(credentials)
        apis.append(api)
        return api

    async def catalog(worker):
        worker.catalog = fixture_instrument_catalog()

    class Notify:
        def __init__(self, api):
            pass
        def get_notifies(self):
            return []

    monkeypatch.setitem(sys.modules, 'tqsdk', SimpleNamespace(TqNotify=Notify))
    monkeypatch.setattr(PublicMarketWorker, '_catalog', catalog)
    candidate = PublicMarketWorker(('test', 'secret'), lambda *topics: None, api_factory=factory,
                                   max_cold=2, cold_slots=slots)
    serving = None
    try:
        for duration in range(12):
            candidate.subscribe((SYMBOL, duration))
        eventually(lambda: apis and len(apis[0].waiting) == 2)
        serving = PublicMarketWorker(('test', 'secret'), lambda *topics: None, api_factory=factory,
                                     max_cold=8, cold_slots=slots)
        for duration in range(12):
            serving.subscribe((OTHER, duration))
        eventually(lambda: len(apis) == 2 and len(apis[1].waiting) == 6)
        assert sum(len(api.waiting) for api in apis) == 8
        assert candidate.loading == 2 and serving.loading == 6
        candidate.close()
        eventually(lambda: len(apis[1].waiting) == 8)
        assert candidate.closed and apis[0].closed
    finally:
        candidate.close()
        if serving:
            serving.close()
    assert all(api.closed for api in apis)


def test_large_catalog_hot_requests_only_copy_selected_workbench(market, monkeypatch):
    service, workers = market
    service.instruments()
    workers[0].catalog = [
        {'symbol': f'SHFE.xx{index:04}', 'instrumentName': 'test'} for index in range(8000)
    ] + fixture_instrument_catalog()
    key = (SYMBOL, 300)
    request_key(service, key)
    workers[0].publish(key)
    original_copy = copy.deepcopy
    full_copies = []

    def observed_copy(value, *args, **kwargs):
        if value is workers[0].catalog:
            full_copies.append(value)
        return original_copy(value, *args, **kwargs)

    monkeypatch.setattr('tqkq_readonly_sidecar.public_market.copy.deepcopy', observed_copy)
    for _ in range(20):
        assert service.workbench(*key)['quote']['symbol'] == SYMBOL
    assert not full_copies
    service.instruments()
    assert len(full_copies) == 1


class MarketApi(FakeApi):
    def __init__(self, credentials):
        super().__init__(credentials)
        self.quotes = {}
        self.serials = {}

    async def query_quotes(self, **kwargs):
        self.check()
        return [item['symbol'] for item in fixture_instrument_catalog()]

    async def query_cont_quotes(self):
        self.check()
        return []

    async def query_symbol_info(self, symbols):
        self.check()
        return pd.DataFrame([{
            'instrument_id': item['symbol'], 'exchange_id': item['exchangeId'],
            'product_id': item['productId'], 'instrument_name': item['instrumentName'],
            'price_tick': item['priceTick'], 'volume_multiple': item['volumeMultiple'],
        } for item in fixture_instrument_catalog()])

    def get_quote(self, symbol):
        super().get_quote(symbol)
        return self.quotes.setdefault(symbol, SimpleNamespace(
            last_price=100, datetime=datetime.now(UTC).isoformat(),
            price_tick=0.2, volume_multiple=10, pre_close=99,
            trading_time={'day': [['09:00:00', '15:00:00']], 'night': []},
        ))

    async def get_kline_serial(self, symbol, duration, data_length):
        await super().get_kline_serial(symbol, duration, data_length)
        return self.serials.setdefault((symbol, duration), pd.DataFrame([{
            'datetime': 1_788_192_000_000_000_000 + i * duration * 1_000_000_000,
            'open': 100, 'high': 102, 'low': 99, 'close': 101, 'volume': 10, 'close_oi': 20,
        } for i in range(data_length)]))

    def wait_update(self, deadline):
        self.check()
        # Like the SDK, the wait lasts until its deadline unless an update arrives: then it returns at
        # once and the worker publishes that update without waiting out the deadline.
        async def until_update():
            while not self.pending and time.time() < deadline:
                await asyncio.sleep(0.001)
        self.loop.run_until_complete(until_update())
        return self.advance()


@pytest.fixture
def real_renderer_workers(monkeypatch):
    workers, apis = [], []

    class Notify:
        def __init__(self, api):
            api.check()
        def get_notifies(self):
            return []

    def factory(credentials):
        api = MarketApi(credentials)
        apis.append(api)
        return api

    def create(**kwargs):
        worker = PublicMarketWorker(('test', 'secret'), lambda *topics: None, api_factory=factory, **kwargs)
        workers.append(worker)
        eventually(lambda: worker.read()[0] is not None)
        return worker, apis[-1]

    monkeypatch.setitem(sys.modules, 'tqsdk', SimpleNamespace(TqNotify=Notify))
    yield create
    for worker in workers:
        worker.close()
    assert all(api.closed for api in apis)


def test_real_renderer_256_bars_calendar_and_async_publish_ignores_outer_deadline(real_renderer_workers):
    worker, api = real_renderer_workers()
    key = (SYMBOL, 300)
    api.release.add(key)
    worker.subscribe(key)
    eventually(lambda: key in worker.read()[1])
    payload = worker.read()[1][key]
    assert len(payload['bars']) == 256
    assert all(bar['tradingDay'] for bar in payload['bars'])
    assert payload['quote']['lastPrice'] == 100
    assert payload['quote']['priceTick'] == 0.2
    quote_time = payload['quote']['quoteDatetime']
    started = time.monotonic()

    def tick():
        api.quotes[SYMBOL].last_price = 105
        api.touch(api.quotes[SYMBOL])

    api.loop.call_soon_threadsafe(tick)
    eventually(lambda: worker.read()[1][key]['quote']['lastPrice'] == 105)
    assert time.monotonic() - started < 0.1
    assert worker.read()[1][key]['quote']['quoteDatetime'] == quote_time


def test_no_tick_session_clock_refresh_preserves_price_and_quote_time(real_renderer_workers, monkeypatch):
    state = ['trading']
    monkeypatch.setattr('tqkq_readonly_sidecar.main.trading_schedule_status', lambda *_, **__: state[0])
    worker, api = real_renderer_workers()
    key = (SYMBOL, 300)
    api.release.add(key)
    worker.subscribe(key)
    eventually(lambda: key in worker.read()[1])
    quote = worker.read()[1][key]['quote']
    assert quote['marketSessionStatus'] == 'trading'
    state[0] = 'closed'
    eventually(lambda: worker.read()[1][key]['quote']['marketSessionStatus'] == 'closed')
    updated = worker.read()[1][key]['quote']
    assert updated['lastPrice'] == quote['lastPrice']
    assert updated['quoteDatetime'] == quote['quoteDatetime']


def test_eight_cold_timeouts_release_slots_and_same_key_recovers_while_hot_updates(real_renderer_workers):
    worker, api = real_renderer_workers(cold_timeout=0.08, cold_retry_seconds=0.2)
    hot = (SYMBOL, 300)
    api.release.add(hot)
    worker.subscribe(hot)
    eventually(lambda: hot in worker.read()[1])
    cold = [(OTHER, duration) for duration in range(100, 108)]
    for key in cold:
        worker.subscribe(key)
    eventually(lambda: len(api.calls) >= 9)
    eventually(lambda: len(worker._retry_at) == 8)
    assert worker.loading == 0 and not api.waiting
    assert api.max_loading <= 8
    api.quotes[SYMBOL].last_price = 107
    api.loop.call_soon_threadsafe(api.touch, api.quotes[SYMBOL])
    eventually(lambda: worker.read()[1][hot]['quote']['lastPrice'] == 107)
    api.release.update(cold)
    eventually(lambda: set(cold) <= worker.read()[1].keys())
    assert api.calls.count(cold[0]) == 2
    assert len(api.serials) == 9
    assert not worker.failed_keys


def test_exhausted_cold_retries_request_rebuild_without_poisoning_hot_data(real_renderer_workers):
    worker, api = real_renderer_workers(cold_timeout=0.03, cold_retry_seconds=0.01)
    hot, failed = (SYMBOL, 300), (OTHER, 300)
    api.release.add(hot)
    worker.subscribe(hot)
    eventually(lambda: hot in worker.read()[1])
    worker.subscribe(failed)
    eventually(lambda: failed in worker.failed_keys)
    assert api.calls.count(failed) == 3
    assert worker.loading == 0 and worker.error is None
    assert worker.read()[1][hot]['quote']['lastPrice'] == 100


def test_pre_open_quote_and_optional_status_do_not_block_ready_chart(real_renderer_workers, monkeypatch):
    worker, api = real_renderer_workers()
    key = (SYMBOL, 300)
    api.quotes[SYMBOL] = SimpleNamespace(last_price=float('nan'), datetime='',
                                        price_tick=0.2, volume_multiple=10)

    async def status_never_ready(symbol):
        await asyncio.Event().wait()

    monkeypatch.setattr(api, 'get_trading_status', status_never_ready)
    api.release.add(key)
    worker.subscribe(key)
    eventually(lambda: key in worker.read()[1])
    payload = worker.read()[1][key]
    assert len(payload['bars']) == 256
    assert payload['quote']['dataStatus'] == 'stale'
    assert payload['quote']['dataErrorCode'] == 'market_quote_pre_open'
    assert payload['quote']['lastPrice'] == payload['bars'][-1]['close']
    service = PublicMarketService(lambda: ('test', 'secret'))
    service._worker = worker
    first = service._view(key)
    assert first['dataStatus'] == first['quote']['dataStatus'] == 'stale'
    assert worker.loading == 1  # Optional status still waits; first chart is already available.

    def quote_arrives():
        api.quotes[SYMBOL].last_price = 108
        api.quotes[SYMBOL].datetime = datetime.now(UTC).isoformat()
        api.touch(api.quotes[SYMBOL])

    api.loop.call_soon_threadsafe(quote_arrives)
    eventually(lambda: worker.read()[1][key]['quote']['lastPrice'] == 108)
    recovered = service._view(key)
    assert recovered['dataStatus'] == recovered['quote']['dataStatus'] == 'normal'
    assert recovered['dataErrorCode'] is None and recovered['quote']['dataErrorCode'] is None
    assert recovered['revision'] != first['revision']


def test_exhausted_key_causes_warm_replacement_and_keeps_existing_hot_snapshot(market):
    service, workers = market
    key = (SYMBOL, 300)
    request_key(service, key)
    workers[0].publish(key)
    workers[0].failed_keys = {(OTHER, 300)}
    workers[0].changed()
    eventually(lambda: len(workers) == 2)
    assert service.workbench(*key)['quote']['lastPrice'] == 100
    workers[1].publish(key, 102)
    eventually(lambda: workers[0].closed)
    assert service.workbench(*key)['quote']['lastPrice'] == 102


def test_calendar_initializes_on_owner_before_any_subscription_or_publish(real_renderer_workers):
    worker, api = real_renderer_workers()
    assert len(api.calendar_calls) == 1
    start, end, in_loop, subscriptions = api.calendar_calls[0]
    assert start == end and in_loop is False and subscriptions == 0
    assert not worker.read()[1]
    key = (SYMBOL, 300)
    api.release.add(key)
    worker.subscribe(key)
    eventually(lambda: key in worker.read()[1])
    assert len(worker.read()[1][key]['bars']) == 256
    assert api.calendar_calls[1][2] is True
    assert api.calendar_calls[1][3] == 1


def test_single_series_render_error_marks_stale_and_other_hot_series_keeps_updating(real_renderer_workers, monkeypatch):
    worker, api = real_renderer_workers()
    bad, good = (SYMBOL, 300), (OTHER, 300)
    api.release.update({bad, good})
    worker.subscribe(bad)
    worker.subscribe(good)
    eventually(lambda: {bad, good} <= worker.read()[1].keys())
    original = worker._render

    def render(symbol, duration):
        if symbol == SYMBOL:
            raise ValueError('calendar conversion failed for this series')
        return original(symbol, duration)

    monkeypatch.setattr(worker, '_render', render)

    def notify_both(price=111):
        api.quotes[OTHER].last_price = price
        api.touch(api.quotes[SYMBOL], api.quotes[OTHER])

    api.loop.call_soon_threadsafe(notify_both)
    eventually(lambda: bad in worker.failed_keys)
    eventually(lambda: worker.read()[1][good]['quote']['lastPrice'] == 111)
    stale = worker.read()[1][bad]['quote']
    assert stale['dataStatus'] == 'stale'
    assert stale['dataErrorCode'] == 'market_series_render_failed'
    assert worker.error is None and worker.healthy
    service = PublicMarketService(lambda: ('test', 'secret'))
    service._worker = worker
    assert service._view(bad)['dataStatus'] == 'stale'
    assert service._view(good)['dataStatus'] == 'normal'
    # The shared publisher remains alive for another subsequent market update.
    api.loop.call_soon_threadsafe(notify_both, 112)
    eventually(lambda: worker.read()[1][good]['quote']['lastPrice'] == 112)
    monkeypatch.setattr(worker, '_render', original)
    api.loop.call_soon_threadsafe(notify_both)
    eventually(lambda: bad not in worker.failed_keys)
    assert service._view(bad)['dataStatus'] == 'normal'


def test_cold_catalog_reports_loading_then_recovers_without_private_actor(market):
    public, workers = market
    public.instruments()
    worker = workers[0]
    worker.catalog = None
    service = SnapshotService(username=None, password=None, symbols=[], fixture=True)
    service.public_market.close()
    service.public_market = public
    server = ReadOnlyServer(('127.0.0.1', 0), TOKEN, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, catalog = http(server, '/v1/public-market/instruments')
        assert code == 200 and catalog['catalogStatus'] == 'loading' and catalog['items'] == []
        code, waiting = http(server, f'/v1/public-market/{SYMBOL}/workbench?duration=300')
        assert code == 503 and waiting['error'] == 'market_series_not_ready'
        worker.catalog = fixture_instrument_catalog()
        worker.changed()
        code, ready = http(server, '/v1/public-market/instruments')
        assert code == 200 and ready['catalogStatus'] == 'ready' and ready['items']
        with public._condition:
            # Hold recovery in backoff: an actual failure is still an error,
            # not the ordinary first-load state tested above.
            public._retry_at = time.monotonic() + 60
            worker.catalog = None
            worker.error = 'public_market_unavailable'
        worker.changed()
        assert http(server, '/v1/public-market/instruments')[0] == 503
        assert service._worker is None
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_fixture_board_rows_full_shape_change_math_and_http_route():
    service = SnapshotService(username=None, password=None, symbols=[], fixture=True)
    server = ReadOnlyServer(('127.0.0.1', 0), TOKEN, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        board = service.public_market.board()
        assert board['truncated'] is False and board['dataStatus'] == 'normal'
        assert [row['symbol'] for row in board['rows']] == [
            item['symbol'] for item in fixture_instrument_catalog()]
        row = board['rows'][3]
        assert row['instrumentName'] == '螺纹钢2610' and row['productName'] == '螺纹钢'
        assert row['instrumentId'] == 'rb2610' and row['exchangeId'] == 'SHFE'
        assert row['change'] == round(row['last'] - row['preSettlement'], 2)
        assert abs(row['changePercent'] + 0.42) < 0.01
        assert row['volume'] == 98_700 and row['openInterest'] == 2_450_000
        assert row['openInterestChange'] == -240
        assert row['high'] and row['low'] and row['upperLimit'] and row['lowerLimit']
        assert row['quoteTime'] and row['status'] == 'normal'
        assert board['rows'][0]['productName'] == '沪深300股指'
        # Unchanged rows keep the revision: pollers skip nothing, but do not churn.
        assert service.public_market.board()['revision'] == board['revision']
        code, payload = http(server, '/v1/public-market/board')
        assert code == 200 and len(payload['rows']) == 4 and payload['revision']
        assert service._commands.empty() and service._worker is None
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_board_overflow_truncates_in_catalog_order(monkeypatch):
    service = SnapshotService(username=None, password=None, symbols=[], fixture=True)
    try:
        monkeypatch.setattr('tqkq_readonly_sidecar.public_market.MAX_BOARD_QUOTES', 2)
        board = service.public_market.board()
        assert board['truncated'] is True
        assert [row['symbol'] for row in board['rows']] == [
            item['symbol'] for item in fixture_instrument_catalog()[:2]]
    finally:
        service.stop()


def test_board_lease_expiry_retires_rows_and_reopen_reloads():
    # The leases are shortened to 0.15 s, so the restart cooldown (retry_seconds, 30 s in
    # production next to 30 s leases) is shortened with them: with the default a replacement
    # attempted earlier in the run blocks the reopen for 30 s (market_series_not_ready on CI).
    public = PublicMarketService(lambda: ('test', 'secret'), fixture=True,
                                 lease_seconds=0.15, ready_wait=10, retry_seconds=0.2)
    try:
        assert public.board()['rows']
        worker = public._worker
        # Reading the catalog keeps the worker alive past the board lease, so the
        # retire step is observable: wanted goes empty and the rows follow. It is
        # read on every poll — a single read left a busy CI host racing the
        # catalog lease (the worker could close before the rows were retired).
        eventually(lambda: (public.instruments(), not worker.board_rows and not worker._board_wanted)[1])
        assert not worker.closed
        eventually(lambda: worker.closed)
        # Reopening may meet the worker still starting (the client retries the same way).
        reopened: list[dict] = []

        def reopen() -> bool:
            try:
                reopened.append(public.board())
            except RuntimeError:
                return False
            return bool(reopened[-1]['rows'])

        eventually(reopen, timeout=10)
        assert public._worker is not worker
    finally:
        public.close()


def board_quote(last=105, *, pre_settlement=100, quote_time=None):
    return SimpleNamespace(
        last_price=last, datetime=quote_time if quote_time is not None else datetime.now(UTC).isoformat(),
        pre_settlement=pre_settlement, open_interest=500, pre_open_interest=520,
        volume=1_234, highest=106, lowest=99, upper_limit=110, lower_limit=90, bid_price1=104, ask_price1=float('nan'),
        trading_time={'day': [['09:00:00', '15:00:00']], 'night': []},
    )


def test_real_renderer_board_rows_change_math_status_tick_and_roll(real_renderer_workers, monkeypatch):
    worker, api = real_renderer_workers()
    main, quiet, extra = SYMBOL, OTHER, fixture_instrument_catalog()[2]['symbol']
    state = ['trading']
    monkeypatch.setattr('tqkq_readonly_sidecar.main.trading_schedule_status', lambda *_, **__: state[0])
    api.quotes.update({main: board_quote(), extra: board_quote()})
    api.quotes[quiet] = SimpleNamespace(
        last_price=float('nan'), datetime='', pre_settlement=100,
        trading_time={'day': [['09:00:00', '15:00:00']], 'night': []})
    worker.subscribe_board({main, quiet, extra})
    eventually(lambda: set(worker.board_read()) == {main, quiet, extra})
    rows = worker.board_read()
    assert rows[main]['change'] == 5.0 and rows[main]['changePercent'] == 5.0
    assert rows[main]['openInterestChange'] == -20 and rows[main]['status'] == 'normal'
    # 手机报价列表的买价 / 卖价: 一档; an empty side (NaN) reads None.
    assert rows[main]['bidPrice1'] == 104 and rows[main]['askPrice1'] is None
    assert rows[extra]['productName'] == '沪金'
    # No live trade yet: the session clock picks the same code the chart uses.
    assert rows[quiet]['status'] == 'market_quote_no_trade'
    assert rows[quiet]['last'] is None and rows[quiet]['changePercent'] is None
    state[0] = 'closed'
    api.loop.call_soon_threadsafe(api.touch, api.quotes[quiet])
    eventually(lambda: worker.board_read()[quiet]['status'] == 'market_quote_pre_open')

    def tick():
        # A trade above the day's high moves the high with it (the exchange quote does).
        api.quotes[main].last_price = api.quotes[main].highest = 108
        api.touch(api.quotes[main])

    api.loop.call_soon_threadsafe(tick)
    eventually(lambda: worker.board_read()[main]['last'] == 108)
    # After a roll the new main appears and the old one drops.
    rolled = fixture_instrument_catalog()[3]['symbol']
    worker.subscribe_board({main, rolled})
    eventually(lambda: set(worker.board_read()) == {main, rolled})
    assert worker.board_read()[rolled]['status'] == 'normal'


def test_single_board_quote_failure_is_isolated_per_row(real_renderer_workers, monkeypatch):
    worker, api = real_renderer_workers()
    good, bad = SYMBOL, OTHER
    api.quotes[good] = board_quote()
    original = api.get_quote

    def get_quote(symbol):
        if symbol == bad:
            raise RuntimeError('unknown symbol')
        return original(symbol)

    monkeypatch.setattr(api, 'get_quote', get_quote)
    worker.subscribe_board({good, bad})
    eventually(lambda: set(worker.board_read()) == {good, bad})
    rows = worker.board_read()
    assert rows[bad]['status'] == 'market_quote_unavailable'
    assert rows[bad]['last'] is None and rows[bad]['changePercent'] is None
    assert rows[bad]['instrumentName'] == '铁矿石2609' and rows[bad]['productName'] == '铁矿石'
    assert rows[good]['status'] == 'normal' and rows[good]['last'] == 105
    assert worker.error is None and worker.healthy


def test_board_row_with_last_outside_the_day_range_is_withheld_and_logged_once(real_renderer_workers, capsys):
    """#806: a 最新价 above 最高 / below 最低 is never shown (the row reads unavailable, no other price) and is
    logged once per episode; the row comes back as soon as the quote is consistent again."""
    worker, api = real_renderer_workers()
    api.quotes[SYMBOL] = board_quote(last=185)  # 最高 106 / 最低 99
    worker.subscribe_board({SYMBOL})
    eventually(lambda: SYMBOL in worker.board_read())
    row = worker.board_read()[SYMBOL]
    assert row['status'] == 'market_quote_unavailable'
    assert row['last'] is None and row['change'] is None and row['preSettlement'] is None

    def touch(last):
        api.quotes[SYMBOL].last_price = last
        api.touch(api.quotes[SYMBOL])

    api.loop.call_soon_threadsafe(touch, 186)
    api.loop.call_soon_threadsafe(touch, 105)
    eventually(lambda: worker.board_read()[SYMBOL]['last'] == 105)
    assert worker.board_read()[SYMBOL]['status'] == 'normal'
    logged = capsys.readouterr().err
    assert logged.count('board_quote_inconsistent') == 1 and f'symbol={SYMBOL} last=185' in logged


def test_an_inverted_day_range_is_withheld_like_a_last_outside_it():
    """#823: 最高 below 最低 is an impossible quote, not "unknown bounds"; 盘前 without a last stays no evidence."""
    assert last_outside_day_range(100, 99, 106)
    assert last_outside_day_range(105, 106, 99) is False
    assert last_outside_day_range(None, 99, 106) is False
    assert last_outside_day_range(105, None, 99) is False


def test_a_failed_board_quote_is_retried_with_backoff_while_still_wanted(real_renderer_workers, monkeypatch):
    """#504: a quote whose first read failed is read again (with a growing gap) while its lease is
    renewed, instead of staying unavailable until the lease lapses; the good row is not re-read."""
    monkeypatch.setattr('tqkq_readonly_sidecar.public_market.BOARD_RETRY_SECONDS', 0.2)
    worker, api = real_renderer_workers()
    good, flaky, broken = SYMBOL, OTHER, fixture_instrument_catalog()[2]['symbol']
    api.quotes.update({good: board_quote(), flaky: board_quote()})
    original = api.get_quote
    calls: dict[str, int] = {}

    def get_quote(symbol):
        calls[symbol] = calls.get(symbol, 0) + 1
        if symbol == broken or (symbol == flaky and calls[symbol] == 1):
            raise RuntimeError('quote_read_failed')
        return original(symbol)

    monkeypatch.setattr(api, 'get_quote', get_quote)
    wanted = {good, flaky, broken}
    worker.subscribe_board(wanted)
    eventually(lambda: worker.board_read().get(flaky, {}).get('status') == 'market_quote_unavailable')
    # Readers keep renewing the lease; the flaky quote recovers on the retry.
    deadline = time.monotonic() + 3
    while worker.board_read().get(flaky, {}).get('status') != 'normal' and time.monotonic() < deadline:
        worker.subscribe_board(wanted)
        time.sleep(0.05)
    assert worker.board_read()[flaky]['status'] == 'normal' and calls[flaky] == 2
    time.sleep(1.0)
    # A quote that keeps failing is retried at a doubling gap (0.2, 0.4, 0.8 s ...), not per read.
    assert 2 <= calls[broken] <= 5 and worker.board_read()[broken]['status'] == 'market_quote_unavailable'
    assert calls[good] == 1 and calls[flaky] == 2
    assert worker.error is None and worker.healthy


def test_board_row_waits_for_first_quote_data(real_renderer_workers, monkeypatch):
    """A fresh quote-only subscription has no data yet: its row waits for the first update
    instead of answering an empty 盘前 row (production 10-03: a new 到价提醒 on a non-main
    read last/昨结 as null). A quote that never gets data still shows after the grace."""
    monkeypatch.setattr('tqkq_readonly_sidecar.public_market.BOARD_FIRST_DATA_SECONDS', 0.5)
    worker, api = real_renderer_workers()
    empty = dict(last_price=float('nan'), datetime='', pre_settlement=float('nan'),
                 trading_time={'day': [['09:00:00', '15:00:00']], 'night': []})
    api.quotes[SYMBOL] = SimpleNamespace(**empty)
    worker.subscribe_board({SYMBOL})
    time.sleep(0.2)
    assert SYMBOL not in worker.board_read()
    # The subscription's own first update arrives still empty: the row keeps waiting.
    api.loop.call_soon_threadsafe(api.touch, api.quotes[SYMBOL])
    time.sleep(0.2)
    assert SYMBOL not in worker.board_read()

    def first_data():
        quote = api.quotes[SYMBOL]
        quote.last_price, quote.pre_settlement = 105, 100
        quote.datetime = datetime.now(UTC).isoformat()
        api.touch(quote)

    api.loop.call_soon_threadsafe(first_data)
    eventually(lambda: worker.board_read().get(SYMBOL, {}).get('last') == 105)
    assert worker.board_read()[SYMBOL]['preSettlement'] == 100
    api.quotes[OTHER] = SimpleNamespace(**empty)
    worker.subscribe_board({SYMBOL, OTHER})
    eventually(lambda: OTHER in worker.board_read(), timeout=5)
    assert worker.board_read()[OTHER]['last'] is None


def test_quotes_and_calendar_http_routes_serve_fixture_rows_and_weekend_calendar():
    """只订报价的 extras 通道与交易日历 (2026-10-03): any catalog contract rides the
    board's quote-only rows over HTTP; an unknown symbol answers that symbol alone,
    and the calendar carries the weekend rule in fixture mode."""
    service = SnapshotService(username=None, password=None, symbols=[], fixture=True)
    server = ReadOnlyServer(('127.0.0.1', 0), TOKEN, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert http(server, '/v1/public-market/quotes')[0] == 422  # no symbols
        assert http(server, '/v1/public-market/quotes?symbols=' + ','.join(['SHFE.au2612'] * 21))[0] == 422
        code, payload = http(server, '/v1/public-market/quotes?symbols=shfe.au2612,SHFE.xx9999')
        assert code == 200
        row, unknown = payload['rows']
        assert row['symbol'] == 'SHFE.au2612' and row['last'] and row['status'] == 'normal'
        assert row['instrumentName'] == '沪金2612' and row['productName'] == '沪金'
        assert unknown == {'symbol': 'SHFE.xx9999', 'errorCode': 'symbol_not_found'}
        code, calendar = http(server, '/v1/public-market/calendar')
        assert code == 200
        assert set(calendar) == {'tradingDay', 'previousTradingDay', 'recentTradingDays', 'latestClosedTradingDay'}
        assert len(calendar['recentTradingDays']) == 10
        assert calendar['recentTradingDays'] == sorted(calendar['recentTradingDays'], reverse=True)
        assert calendar['previousTradingDay'] == calendar['recentTradingDays'][0]
        assert all(day < calendar['tradingDay'] for day in calendar['recentTradingDays'])
        assert service._commands.empty() and service._worker is None
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_extras_leases_ride_the_board_channel_and_expire_on_their_own(monkeypatch):
    """The extras channel never opens a kline serial: its leases join the board's
    wanted set (the supervisor pushes the union) and expire independently — an
    expired extras symbol stops being subscribed even while the board stays hot,
    and the cap answers overflow per symbol, never the whole read."""
    public = PublicMarketService(lambda: ('test', 'secret'), fixture=True,
                                 lease_seconds=0.3, ready_wait=10, retry_seconds=0.2)
    try:
        quotes = public.quotes(['shfe.au2612', 'SHFE.xx9999'])
        assert [row['symbol'] for row in quotes['rows']] == ['SHFE.au2612', 'SHFE.xx9999']
        assert quotes['rows'][0]['last'] and quotes['rows'][1] == {
            'symbol': 'SHFE.xx9999', 'errorCode': 'symbol_not_found'}
        worker = public._worker
        assert worker._board_wanted == {'SHFE.au2612'} and not worker.keys  # no kline serial
        # The board read pushes the union of its mains and the live extras lease.
        assert [row['symbol'] for row in public.board()['rows']] == [
            item['symbol'] for item in fixture_instrument_catalog()]
        assert worker._board_wanted == {item['symbol'] for item in fixture_instrument_catalog()}
        # The board lease expires while the extras lease is kept alive by reads:
        # the wanted set shrinks back to the extras symbol and the rows retire.
        def extras_only() -> bool:
            public.quotes(['SHFE.au2612'])
            return (worker._board_wanted == {'SHFE.au2612'}
                    and set(worker.board_read()) == {'SHFE.au2612'})
        eventually(extras_only, timeout=5)
        # Overflow answers the symbol, not the whole read (au holds the one slot).
        monkeypatch.setattr('tqkq_readonly_sidecar.public_market.MAX_EXTRA_QUOTES', 1)
        capped = public.quotes(['SHFE.rb2610'])
        assert capped['rows'] == [{'symbol': 'SHFE.rb2610', 'errorCode': 'market_subscription_limit'}]
    finally:
        public.close()


def test_quote_rotation_past_the_retained_budget_replaces_the_worker_in_place(monkeypatch):
    """#456：SDK 的报价订阅只增不减（租约过期、盘面滚动都不退订）。extras 轮换把
    worker 的累计报价数推过 MAX_BOARD_QUOTES + 2×MAX_EXTRA_QUOTES 时，supervisor
    走既有的候选换机：新 worker 先把仍然有效的 extras 订齐并拿到首包（extras-only
    兴趣同样预热），再切换；旧 worker 随即关闭；仍在租期内的报价由新一代继续服务，
    累计数回到预算内，且全程不占任何 K 线槽位。"""
    workers, apis = [], []

    class Notify:
        def __init__(self, api):
            api.check()

        def get_notifies(self):
            return []

    def api_factory(credentials):
        api = MarketApi(credentials)
        apis.append(api)
        return api

    def worker_factory(credentials, changed, **kwargs):
        worker = PublicMarketWorker(credentials, changed, api_factory=api_factory, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setitem(sys.modules, 'tqsdk', SimpleNamespace(TqNotify=Notify))
    monkeypatch.setattr('tqkq_readonly_sidecar.public_market.MAX_BOARD_QUOTES', 1)
    monkeypatch.setattr('tqkq_readonly_sidecar.public_market.MAX_EXTRA_QUOTES', 1)
    public = PublicMarketService(lambda: ('test', 'secret'), worker_factory=worker_factory,
                                 lease_seconds=0.3, ready_wait=10, retry_seconds=0.05,
                                 warm_timeout=5)
    symbols = [item['symbol'] for item in fixture_instrument_catalog()]

    def rotate(symbol):
        # 等上一张 extras 租约到期；目录读取维持 worker 的兴趣，别让它走闲置关闭。
        eventually(lambda: (public.instruments(), not public._extra_leases)[1], timeout=5)
        return public.quotes([symbol])

    try:
        for symbol in symbols:
            payload = rotate(symbol)
            assert payload['rows'][0]['symbol'] == symbol
            assert payload['rows'][0]['status'] == 'normal'
        # 四个不同合约轮过一遍：累计报价 4 > 1 + 2×1 → 既有换机机制介入。
        assert workers[0].quotes_retained() == 4
        eventually(lambda: len(workers) == 2 and workers[0].closed, timeout=5)
        assert public._worker is workers[1]
        # 新一代只订仍然有效的那一个，继续照常服务；K 线槽位一个没占。
        assert workers[1].quotes_retained() == 1
        served = public.quotes([symbols[3]])
        assert served['rows'][0]['symbol'] == symbols[3] and served['rows'][0]['status'] == 'normal'
        assert not workers[0].keys and not workers[1].keys
        # 累计数回到预算内：不再连环换机。
        assert len(workers) == 2
    finally:
        public.close()
    assert all(api.closed for api in apis)


def test_calendar_summary_computes_on_demand_on_the_owner_thread(real_renderer_workers):
    """A worker nobody reads a calendar from never asks the SDK for the window
    (the startup prime stays the only call); once wanted, the summary computes
    on the owner thread between wait_updates and follows the 10 recent days."""
    worker, api = real_renderer_workers()
    assert worker.calendar_read() is None and len(api.calendar_calls) == 1
    worker.subscribe_calendar()
    eventually(lambda: worker.calendar_read() is not None)
    summary = worker.calendar_read()
    assert len(api.calendar_calls) == 2  # the startup prime plus the summary window
    assert api.calendar_calls[1][2] is False  # between wait_updates; check() pins the owner thread
    assert len(summary['recentTradingDays']) == 10
    assert summary['recentTradingDays'] == sorted(summary['recentTradingDays'], reverse=True)
    assert summary['previousTradingDay'] == summary['recentTradingDays'][0]
    assert all(day < summary['tradingDay'] for day in summary['recentTradingDays'])
    # 2026-10-12: the whole window too, oldest first, coming days included — the Runtime counts
    # trading days across holidays (order expiry, backfill, delivery reminders) from it.
    days = summary['tradingDays']
    assert days == sorted(days) and summary['tradingDay'] in days
    assert set(summary['recentTradingDays']) <= set(days)
    assert (date.fromisoformat(days[-1]) - date.fromisoformat(summary['tradingDay'])).days >= 40


# 扩容 ② (2026-10-13): the worker renders bars incrementally (same result as the full session
# render), every view is encoded once, a reader naming its barsVersion gets only the changed bars, and a
# parked long poll whose reader left ends without answering.

def on_owner(api, action):
    """Run `action` on the SDK owner thread (inside the worker's next wait_update) and return it."""
    done, result = threading.Event(), []

    def run():
        try:
            result.append(action())
        except Exception as error:  # noqa: BLE001 — surfaced to the test thread
            result.append(error)
        done.set()
    api.loop.call_soon_threadsafe(run)
    assert done.wait(2)
    if isinstance(result[0], Exception):
        raise result[0]
    return result[0]


def test_worker_incremental_render_equals_the_full_session_render(real_renderer_workers):
    worker, api = real_renderer_workers()
    key = (SYMBOL, 300)
    api.release.add(key)
    worker.subscribe(key)
    eventually(lambda: key in worker.read()[1])
    serial = api.serials[key]

    def both():
        full = OfficialTqKqSession.market_workbench(worker, *key)
        incremental = worker._render(*key)
        return full, {name: value for name, value in incremental.items() if name != SERIES_KEY}

    def tick(price):
        serial.loc[serial.index[-1], 'close'] = price
        api.quotes[SYMBOL].last_price = price
        api.touch(serial, api.quotes[SYMBOL])

    def roll():
        values = serial.to_numpy(copy=True)
        values[:-1] = values[1:]
        values[-1, 0] = values[-2, 0] + 300 * 1_000_000_000
        serial.iloc[:, :] = values
        api.touch(serial)

    for step in range(6):
        on_owner(api, (lambda price=101 + step: tick(price)) if step % 2 else roll)
        full, incremental = on_owner(api, both)
        assert incremental == full, step
        eventually(lambda: worker.read()[1][key]['bars'] == full['bars'])
    assert worker.read()[1][key]['quote']['lastPrice'] == 106


def test_one_update_batch_wakes_the_readers_once_however_many_series_and_rows_it_publishes(
        real_renderer_workers, monkeypatch):
    """号池长轮询 (2026-10-14): the data thread used to notify every parked reader once per series and
    once per board row of a batch; now one coalescing batch is one wake-up."""
    worker, api = real_renderer_workers()
    wakes = []
    worker._changed = lambda *topics: wakes.append(topics)
    symbols = [item['symbol'] for item in fixture_instrument_catalog()[:3]]
    keys = [(symbol, 300) for symbol in symbols]
    api.release.update(keys)
    for key in keys:
        worker.subscribe(key)
    eventually(lambda: set(keys) <= worker.read()[1].keys())
    worker.subscribe_board(set(symbols))
    eventually(lambda: set(symbols) <= worker.board_read().keys())
    time.sleep(0.05)
    wakes.clear()

    def tick():
        for symbol in symbols:
            api.quotes[symbol].last_price = 107
        api.touch(*(api.quotes[symbol] for symbol in symbols))

    api.loop.call_soon_threadsafe(tick)
    eventually(lambda: all(worker.read()[1][key]['quote']['lastPrice'] == 107 for key in keys))
    eventually(lambda: all(row['last'] == 107 for row in worker.board_read().values()))
    time.sleep(0.05)
    assert len(wakes) == 1


class SeriesWorker(ControlledWorker):
    """A controlled worker publishing incrementally rendered series (bars + their versions)."""

    def __init__(self, credentials, changed, **kwargs):
        super().__init__(credentials, changed, **kwargs)
        self.serial = Serial(30, datetime(2026, 10, 12, 1, 0, tzinfo=UTC))
        for _ in range(30):
            self.serial.append()
        self.renderer = SeriesRenderer(30, sorted_days)

    def publish(self, key, price=100):
        bars, meta = self.renderer.update(self.serial.frame())
        quote = {**FixtureSession([]).market_workbench(*key)['quote'], 'lastPrice': price}
        self.snapshots[key] = {'schemaVersion': 1, 'durationSeconds': key[1], 'quote': quote,
                               'bars': bars, SERIES_KEY: meta}
        self.changed()


def serve(public):
    service = SnapshotService(username=None, password=None, symbols=[], fixture=True)
    service.public_market.close()
    service.public_market = public
    server = ReadOnlyServer(('127.0.0.1', 0), TOKEN, service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return service, server


def test_http_answers_bar_deltas_to_a_reader_naming_its_version_and_whole_windows_to_everyone_else(market):
    public, _ = market
    workers = []

    def factory(*args, **kwargs):
        workers.append(SeriesWorker(*args, **kwargs))
        return workers[-1]
    public._factory = factory
    service, server = serve(public)
    route = f'/v1/public-market/{SYMBOL}/workbench?duration=300'
    try:
        assert http(server, route)[0] == 503  # lease taken, nothing rendered yet
        workers[0].publish((SYMBOL, 300))
        code, whole = http(server, route)
        assert code == 200 and len(whole['bars']) == 30 and whole['barsVersion'] and 'barsDelta' not in whole
        assert SERIES_KEY not in whole and 'ageSeconds' in whole
        held = whole['bars']
        # The forming bar ticks: the delta carries that one bar and the window's proof.
        workers[0].serial.rows[-1]['close'] += 1
        workers[0].publish((SYMBOL, 300), 101)
        code, delta = http(server, route + f'&after={whole["revision"]}&waitMs=1000&barsVersion={whole["barsVersion"]}')
        assert code == 200 and 'bars' not in delta and delta['quote']['lastPrice'] == 101
        assert delta['barsDelta']['base'] == whole['barsVersion'] and delta['barsVersion'] != whole['barsVersion']
        assert delta['barsDelta']['bars'] == [workers[0].renderer.bars[-1]]
        held = [*held[:-1], *delta['barsDelta']['bars']]
        assert delta['barsDelta'] | {'bars': None} == {
            'base': whole['barsVersion'], 'bars': None, 'first': held[0]['datetime'], 'count': 30,
            'digest': bars_digest(held)}
        # A new bar: the window moves; the reader trims before `first` and appends.
        workers[0].serial.append()
        workers[0].publish((SYMBOL, 300), 102)
        code, rolled = http(server, route + f'&barsVersion={delta["barsVersion"]}')
        assert [bar['datetime'] for bar in rolled['barsDelta']['bars']] == [workers[0].renderer.bars[-1]['datetime']]
        held = [bar for bar in held if bar['datetime'] >= rolled['barsDelta']['first']] + rolled['barsDelta']['bars']
        assert held == workers[0].renderer.bars and bars_digest(held) == rolled['barsDelta']['digest']
        # An unchanged long poll answers an empty delta; an old reader (no version), a foreign or an
        # outdated version all get the whole window.
        code, quiet = http(server, route + f'&after={rolled["revision"]}&waitMs=50&barsVersion={rolled["barsVersion"]}')
        assert quiet['revision'] == rolled['revision'] and quiet['barsDelta']['bars'] == []
        for asked in ('', '&barsVersion=elsewhere:3', f'&barsVersion={whole["barsVersion"].split(":")[0]}:999'):
            code, again = http(server, route + asked)
            assert again['bars'] == workers[0].renderer.bars and 'barsDelta' not in again
        # History replaced: the floor rises and the old version reads whole.
        workers[0].serial.rows = [dict(row, datetime=row['datetime'] + 30_000_000_000) for row in workers[0].serial.rows]
        workers[0].publish((SYMBOL, 300), 103)
        code, replaced = http(server, route + f'&barsVersion={rolled["barsVersion"]}')
        assert replaced['bars'] == workers[0].renderer.bars and 'barsDelta' not in replaced
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_each_view_is_encoded_once_however_many_readers_ask(market, monkeypatch):
    public, workers = market
    key = (SYMBOL, 300)
    try:
        public.workbench(*key)
    except RuntimeError:
        pass
    workers[0].publish(key)
    import tqkq_readonly_sidecar.public_market as module
    encoded = []
    original = module._encode
    monkeypatch.setattr(module, '_encode', lambda value: encoded.append(1) or original(value))
    first = public.workbench(*key)
    for _ in range(20):
        assert public.workbench(*key)['revision'] == first['revision']
    assert len(encoded) == 1
    workers[0].publish(key, 101)
    assert public.workbench(*key)['quote']['lastPrice'] == 101 and len(encoded) == 2


def test_a_parked_long_poll_whose_reader_left_ends_without_answering(market, monkeypatch):
    public, workers = market
    service, server = serve(public)
    outcomes = []
    original = public.workbench_body

    def observed(*args, **kwargs):
        started = time.monotonic()
        try:
            return original(*args, **kwargs)
        except Exception as error:
            outcomes.append((type(error).__name__, time.monotonic() - started))
            raise
    monkeypatch.setattr(public, 'workbench_body', observed)
    route = f'/v1/public-market/{SYMBOL}/workbench?duration=300'
    try:
        http(server, route)
        workers[0].publish((SYMBOL, 300))
        revision = http(server, route)[1]['revision']
        outcomes.clear()  # the first read's 503 (nothing rendered yet)
        reader = socket.create_connection(('127.0.0.1', server.server_port))
        reader.sendall((f'GET {route}&after={revision}&waitMs=8000 HTTP/1.1\r\nHost: x\r\n'
                        f'Authorization: Bearer {TOKEN}\r\n\r\n').encode())
        time.sleep(.3)
        reader.close()
        eventually(lambda: outcomes, timeout=3)
        assert outcomes[0][0] == 'ReaderGone' and outcomes[0][1] < 2
        # Its request thread ends with it (a parked reader no longer holds a thread for the wait).
        eventually(lambda: not any('process_request_thread' in thread.name for thread in threading.enumerate()))
        assert public._topic_waiters == {}
        assert http(server, route)[0] == 200  # the server keeps serving
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_a_change_reaching_a_parked_poll_whose_reader_left_writes_nothing(market, monkeypatch, capsys):
    public, workers = market
    service, server = serve(public)
    outcomes = []
    original = public.workbench_body

    def observed(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as error:
            outcomes.append(type(error).__name__)
            raise
    monkeypatch.setattr(public, 'workbench_body', observed)
    route = f'/v1/public-market/{SYMBOL}/workbench?duration=300'
    try:
        http(server, route)
        workers[0].publish((SYMBOL, 300))
        revision = http(server, route)[1]['revision']
        outcomes.clear()
        reader = socket.create_connection(('127.0.0.1', server.server_port))
        reader.sendall((f'GET {route}&after={revision}&waitMs=8000 HTTP/1.1\r\nHost: x\r\n'
                        f'Authorization: Bearer {TOKEN}\r\n\r\n').encode())
        eventually(lambda: public._topic_waiters)
        reader.close()
        time.sleep(0.05)
        workers[0].publish((SYMBOL, 300), 101)  # wakes the poll well before its next connection check
        eventually(lambda: outcomes)
        assert outcomes == ['ReaderGone']
        assert 'Traceback' not in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()
        service.stop()


def test_reads_reuse_one_kept_alive_connection_and_a_write_closes_its_own(market, capsys):
    """号池长轮询 (2026-10-14): HTTP/1.1 keep-alive — the Runtime's pool reuses a connection (and this
    server its thread) across reads; a write request's connection closes with its answer."""
    import http.client

    public, workers = market
    service, server = serve(public)
    route = f'/v1/public-market/{SYMBOL}/workbench?duration=300'
    headers = {'Authorization': f'Bearer {TOKEN}'}
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        connection.request('GET', route, headers=headers)
        first = connection.getresponse()
        first.read()
        assert first.status == 503 and first.version == 11 and first.getheader('Connection') is None
        workers[0].publish((SYMBOL, 300))
        sock = connection.sock
        for _ in range(3):
            connection.request('GET', route, headers=headers)
            answer = connection.getresponse()
            assert answer.status == 200 and json.loads(answer.read())['quote']['lastPrice'] == 100
            assert connection.sock is sock  # the same connection served every read
        connection.request('POST', '/v1/replays', body=b'{}', headers=headers)
        write = connection.getresponse()
        write.read()
        assert write.getheader('Connection') == 'close' and write.will_close
        # A client resetting its idle kept-alive connection (a restarting Runtime) leaves no traceback.
        idle = socket.create_connection(('127.0.0.1', server.server_port))
        idle.sendall((f'GET {route} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n\r\n').encode())
        assert idle.recv(65536).startswith(b'HTTP/1.1 200')
        idle.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        idle.close()
        time.sleep(0.1)
        assert 'Traceback' not in capsys.readouterr().err
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        service.stop()


class TopicWorker(ControlledWorker):
    """Publishes like PublicMarketWorker does: naming the series (wait topic) that changed."""

    def publish(self, key, price=100):
        payload = FixtureSession([]).market_workbench(*key)
        payload['quote']['lastPrice'] = price
        self.snapshots[key] = payload
        self.changed({key})


@pytest.fixture
def topic_market(market):
    public, _ = market
    workers = []

    def factory(*args, **kwargs):
        workers.append(TopicWorker(*args, **kwargs))
        return workers[-1]
    public._factory = factory
    checks = {}
    original = public._view_entry

    def counted(key):
        checks[key] = checks.get(key, 0) + 1
        return original(key)
    public._view_entry = counted
    return public, workers, checks


def first_revision(public, workers, key):
    request_key(public, key)
    eventually(lambda: workers)
    workers[0].publish(key)
    return public.workbench(*key)['revision']


def test_a_series_change_wakes_only_the_readers_parked_on_that_series(topic_market):
    """号池长轮询 (2026-10-14): each parked long poll waits on its own series' topic. A change to
    another series no longer wakes it (it used to: every change woke every parked reader)."""
    public, workers, checks = topic_market
    parked, busy = (SYMBOL, 300), (OTHER, 300)
    revision = first_revision(public, workers, parked)
    first_revision(public, workers, busy)
    with ThreadPoolExecutor(max_workers=1) as executor:
        answer = executor.submit(public.workbench, *parked, after=revision, wait_ms=5000)
        eventually(lambda: public._topic_waiters.get(parked) == 1)
        before = checks[parked]
        for price in range(101, 141):
            workers[0].publish(busy, price)
            time.sleep(0.002)
        assert checks[parked] == before and not answer.done()
        started = time.monotonic()
        workers[0].publish(parked, 150)
        assert answer.result(timeout=1)['quote']['lastPrice'] == 150
        assert time.monotonic() - started < 0.2
    assert public._topics == {} and public._topic_waiters == {}  # nothing left behind per topic


def test_a_parked_reader_neither_busy_waits_nor_misses_the_latest_view_under_rapid_publishing(topic_market):
    public, workers, checks = topic_market
    key = (SYMBOL, 300)
    revision = first_revision(public, workers, key)
    # Quiet: a parked reader only wakes to check its connection, at most once per READER_CHECK_SECONDS.
    before = checks[key]
    quiet = public.workbench(*key, after=revision, wait_ms=1100)
    assert quiet['revision'] == revision and checks[key] - before <= 3
    # Rapid publishing: every answer is a newer view and the last one is the final price.
    done = threading.Event()

    def publish():
        for price in range(200, 700):
            workers[0].publish(key, price)
        done.set()
    threading.Thread(target=publish, daemon=True).start()
    seen, answers = revision, 0
    before = checks[key]
    while True:
        view = public.workbench(*key, after=seen, wait_ms=300)
        if view['revision'] == seen:
            break  # nothing newer within the wait: the publisher has finished
        seen, answers = view['revision'], answers + 1
    assert done.is_set() and view['quote']['lastPrice'] == 699
    assert checks[key] - before <= 2 * answers + 3  # one check per answer (plus its wake), no spinning
