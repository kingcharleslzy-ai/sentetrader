from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from httpx import ASGITransport, AsyncClient

from test_simulation_orders import USER, Gateway, HangingGateway, NormalProvider, application
from test_strategy_monitor import STRATEGY_ID
from test_strategy_protection import filled_entry, setup
from trader_runtime.autonomy import AutonomyError
from trader_runtime.data_providers import SidecarReadOnlyDataProvider, fake_market_workbench, provider_read_snapshot
from trader_runtime.replay import ReplayContext
from trader_runtime.strategy_rules import StrategyPlan, evaluate_rules
from trader_runtime.strategy_workbench import definition_for


@pytest.mark.parametrize('restart,resume', [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize('outcome', ['alive', 'filled', 'unknown'])
async def test_emergency_cancel_intent_survives_unknown_reply_restart_and_resume(tmp_path, restart, resume, outcome):
    class LostReply(HangingGateway):
        async def submit_order_once(self, request):
            await super().submit_order_once(request)
            raise TimeoutError('lost reply')
    gateway = LostReply()
    path = tmp_path / 'emergency.sqlite3'
    app = application(path, gateway)
    session = app.state.broker.create_session('local', 'native', USER)
    task = asyncio.create_task(app.state.autonomy.execute_trade(user_id=USER, session_id=session.session_id,
        symbol='SHFE.au2612', side='buy_open', volume=1, price_intent='passive', reason='test'))
    await asyncio.wait_for(gateway.submit_started.wait(), 1)
    await app.state.autonomy.deactivate(user_id=USER, emergency=True)
    gateway.submit_release.set()
    with pytest.raises(AutonomyError, match='ai_execution_stopped'):
        await task
    identifier = gateway.request['clientOrderId']
    if restart:
        app.state.store.close()
        app = application(path, gateway)
    if resume:
        app.state.autonomy.resume_runtime_access(user_id=USER, session_id=session.session_id, symbol='SHFE.au2612')
    gateway.query_report = (gateway.report(gateway.request, found=False) if outcome == 'unknown' else
        gateway.report(gateway.request, status='FINISHED', volume_left=0) if outcome == 'filled' else gateway.report(gateway.request))
    await app.state.autonomy.run_once()
    order = app.state.simulation_orders.get(identifier, user_id=USER)
    assert order['cancelRequestedAt']
    assert gateway.cancel_count == (1 if outcome == 'alive' else 0)
    assert order['displayStatus'] == {'alive': '已撤销', 'filled': '已成交', 'unknown': '结果未知'}[outcome]
    assert gateway.submit_count == 1
    app.state.store.close()


@pytest.mark.parametrize('offset,direction,blocked', [('OPEN','BUY',False), ('CLOSETODAY','SELL',True),
                                                       ('CLOSE','SELL',False), ('CLOSETODAY','BUY',False)])
async def test_protection_distinguishes_unrelated_orders_from_conflicting_closes(tmp_path, offset, direction, blocked):
    app, provider, gateway, _, entry = await filled_entry(tmp_path)
    provider.activity['orders'].append({**entry, 'clientOrderId':'manual-unrelated', 'offset':offset,
                                       'direction':direction, 'status':'ALIVE', 'volumeLeft':1})
    provider.last = 820
    row = app.state.store.connection.execute('SELECT * FROM ai_stop_orders').fetchone()
    await app.state.autonomy._check_stop(row)
    assert gateway.submit_count == (1 if blocked else 2)
    assert gateway.cancel_count == 0  # Never cancel unrelated manual orders.
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    if blocked:
        assert stop['lastError'] and stop['status'] == 'ARMED'
    else:
        assert gateway.request['offset'] == 'CLOSETODAY'
    app.state.store.close()


@pytest.mark.parametrize('slow_path', ['activity', 'workbench', 'quote'])
async def test_slow_sidecar_monitor_reads_do_not_delay_http_emergency_stop(tmp_path, slow_path):
    app, normal, gateway, monitor = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    main_thread = threading.get_ident()
    sql_threads = []
    class SlowProvider(SidecarReadOnlyDataProvider):
        def __init__(self):
            super().__init__(base_url='http://127.0.0.1:19999', token='fixture-' * 5, symbols=['SHFE.au2612'])
            self._status = {'accountStatus':'normal','marketDataStatus':'normal'}
            self._account = normal.account_summary('')
            self._positions = normal.positions()
            self._markets = {'SHFE.au2612': normal.market_snapshot('', 'SHFE.au2612')}
    provider = SlowProvider()
    async def slow_read(request):
        assert request.method == 'GET'
        assert threading.get_ident() == main_thread
        if slow_path in request.url.path:
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
        if request.url.path == '/v1/activity':
            payload = {'schemaVersion':1, **normal.activity}
        elif request.url.path.endswith('/workbench'):
            payload = fake_market_workbench('SHFE.au2612', 300)
        else:
            payload = normal.market_snapshot('', 'SHFE.au2612')
        return httpx2.Response(200, json=payload)
    await provider._http.async_client.aclose()
    provider._http.async_client = httpx2.AsyncClient(transport=httpx2.MockTransport(slow_read), base_url=provider._base_url)
    session = app.state.broker.create_session('local', 'native', USER)
    app.state.autonomy.activate(user_id=USER, session_id=session.session_id, symbols=['SHFE.au2612'],
        permissions=['open','close','cancel','stop'], max_order_volume=2, max_orders=4, duration_minutes=30)
    app.state.autonomy.data_provider = app.state.simulation_orders.data_provider = provider
    app.state.store.connection.set_trace_callback(lambda _: sql_threads.append(threading.get_ident()))
    task = asyncio.create_task(monitor.run_once())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with AsyncClient(transport=ASGITransport(app), base_url='http://runtime',
                headers={'Authorization':'Bearer test-local-token', 'Origin':'http://127.0.0.1:5173'}) as client:
            response = await asyncio.wait_for(client.post('/api/ai-execution/stop', json={'emergency':True}), .5)
        assert response.status_code == 200
        assert response.json()['authorization']['status'] == 'EMERGENCY_STOP'
        assert app.state.autonomy.current(user_id=USER)['status'] == 'EMERGENCY_STOP'
        assert not release.is_set()
    finally:
        release.set()
        await task
    assert sql_threads and set(sql_threads) == {main_thread}
    assert gateway.submit_count == 0
    async with provider_read_snapshot(provider, symbols=[' shfe.AU2612 '], activity=False):
        assert provider.market_snapshot('', 'SHFE.au2612')['symbol'] == 'SHFE.au2612'
    await provider.stop()
    app.state.store.close()


def replay_fixture(tmp_path, symbol):
    provider, gateway = NormalProvider(), Gateway()
    provider.canonical_symbol = symbol
    provider.position_items = [{'symbol':symbol, 'direction':'long', 'volume':2,
        'todayVolume':1, 'historicalVolume':1, 'frozenToday':0, 'frozenHistorical':0}]
    app = application(tmp_path / 'replay-end.sqlite3', gateway, provider)
    app.state.simulation_orders.policies.default_allowed_symbols = (symbol.split('.')[1].upper(),)
    snapshot = {'status':'ready', 'symbol':symbol, 'orders':[], 'trades':[], 'positions':provider.positions()}
    replay = SimpleNamespace(_current=lambda:snapshot, entries_paused=False)
    return app, provider, gateway, snapshot, ReplayContext(USER, app, replay)


@pytest.mark.parametrize('symbol,buckets', [('SHFE.au2612',['today','historical']), ('INE.sc2610',['today','historical']),
                                         ('DCE.i2609',['all']), ('CZCE.SR701',['all']), ('CFFEX.IF2609',['all'])])
async def test_replay_finish_uses_exchange_close_plans(tmp_path, symbol, buckets):
    app, _, gateway, _, context = replay_fixture(tmp_path, symbol)
    calls = []
    execute = app.state.autonomy.execute_trade
    async def track(**kwargs):
        calls.append(kwargs['position_bucket'])
        return await execute(**kwargs)
    app.state.autonomy.execute_trade = track
    await context.finish()
    assert calls == buckets
    assert gateway.submit_count == len(buckets)
    await context.finish()
    assert calls == buckets  # Active identities are not submitted again.
    app.state.store.close()


@pytest.mark.parametrize('failure', ['before_prepare', 'not_dispatched', 'unknown', 'retry_unknown'])
async def test_replay_finish_retries_only_definitely_unsent_orders(tmp_path, failure):
    app, provider, gateway, snapshot, context = replay_fixture(tmp_path, 'DCE.i2609')
    if failure == 'before_prepare':
        provider.position_items[0]['frozenToday'] = 2
        with pytest.raises(Exception, match='close_volume_unavailable'):
            await context.finish()
        assert not context.end_orders and gateway.submit_count == 0
        provider.position_items[0]['frozenToday'] = 0
    elif failure in {'not_dispatched', 'retry_unknown'}:
        gateway.submit_report = {'commandStatus':'NOT_DISPATCHED'}
        await context.finish()
        original = gateway.request['clientOrderId']
        gateway.submit_report = None
        gateway.submit_error = failure == 'retry_unknown'
    else:
        gateway.submit_error = True
        await context.finish()
        original = gateway.request['clientOrderId']
        gateway.submit_error = False
    await context.finish()
    assert gateway.submit_count == (2 if failure in {'not_dispatched', 'retry_unknown'} else 1)
    if failure != 'before_prepare':
        assert gateway.request['clientOrderId'] == original
    if failure in {'unknown', 'retry_unknown'}:
        assert not context.playing
    app.state.store.close()


async def test_replay_partial_close_waits_for_fill_snapshot_then_closes_only_remainder(tmp_path):
    app, provider, gateway, snapshot, context = replay_fixture(tmp_path, 'DCE.i2609')
    await context.finish()
    original = dict(gateway.request)
    gateway.query_report = gateway.report(original, status='FINISHED', volume_left=1)
    await app.state.simulation_orders.query(original['clientOrderId'], user_id=USER)
    await context.finish()
    assert gateway.submit_count == 1  # Query alone cannot justify a second close.
    provider.position_items[0].update(volume=1, todayVolume=0)
    snapshot['positions'] = provider.positions()
    snapshot['orders'] = [{**original, 'status':'FINISHED', 'volumeLeft':1}]
    snapshot['trades'] = [{'clientOrderId':original['clientOrderId'],'volume':1}]
    await context.finish()
    assert gateway.submit_count == 2
    assert gateway.request['volume'] == 1
    assert gateway.request['clientOrderId'] != original['clientOrderId']
    app.state.store.close()


def ma_plan(period=200, within=1):
    return StrategyPlan(name='长均线', description='测试已收盘条件', direction='long', timeframeSeconds=300,
        rules={'entryLong':{'conditions':[{'label':'价格高于均线','left':{'indicator':'close','timeframe':300},
            'operator':'gt','right':{'indicator':'ma','timeframe':300,'period':period},'withinBars':within}]}})


def sample_bars(count):
    return [{'datetime':1000000000000000000+i*300000000000,'open':100+i,'high':101+i,
             'low':99+i,'close':100+i,'volume':100} for i in range(count)]


@pytest.mark.parametrize('count,closed,expected', [(20,False,'waiting'), (256,True,'waiting')])
async def test_v2_warmup_and_closed_market_do_not_accumulate_failures(tmp_path, count, closed, expected):
    app, provider, gateway, monitor = setup(tmp_path)
    definition = definition_for(ma_plan())
    app.state.store.connection.execute('UPDATE strategy_drafts SET definition_json=? WHERE id=?',
                                      (json.dumps(definition), STRATEGY_ID))
    app.state.store.connection.commit()
    provider.market_workbench = lambda *args: {'bars':sample_bars(count), 'dataStatus':'stale' if closed else 'normal',
                                              'isCached':False, 'ageSeconds':0}
    if closed:
        provider.market_session_status = 'closed'
    # own_position (策略只管自己开的仓, 2026-10-08) is the preview's extra kwarg; this stub only wants the read.
    monitor.proposals.preview_strategy_intent = lambda **kwargs: app.state.proposals.evaluate_strategy_draft(
        **{key: value for key, value in kwargs.items() if key != "own_position"})
    for _ in range(4):
        assert (await monitor.run_once())[0]['status'] == expected
    state = app.state.store.strategy_monitor_state(strategy_id=STRATEGY_ID, user_id=USER)
    assert state is None or state['consecutiveFailures'] == 0
    assert gateway.submit_count == 0
    app.state.store.close()


@pytest.mark.parametrize('within', [1,20])
async def test_ma200_can_compute_at_max_supported_event_window(within, monkeypatch):
    # This is the production Sidecar window, not a hand-picked larger fixture.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tqkq-sidecar' / 'src'))
    from tqkq_readonly_sidecar.main import KLINE_DATA_LENGTH
    result = evaluate_rules(ma_plan(within=within), {300:{'bars':sample_bars(KLINE_DATA_LENGTH)}})
    assert result['rules']['entryLong']


@pytest.mark.parametrize('wall_age', [0, .1, 3])
async def test_replay_step_reconciles_protection_independently_of_wall_clock(tmp_path, wall_age):
    app, provider, gateway, _, _ = await filled_entry(tmp_path)
    provider.last = 820
    gateway.submit_error = True
    row = app.state.store.connection.execute('SELECT * FROM ai_stop_orders').fetchone()
    await app.state.autonomy._check_stop(row)
    close = dict(gateway.request)
    gateway.submit_error = False
    async def query_original(identifier):
        if identifier == close['clientOrderId']:
            return gateway.report(close, status='FINISHED', volume_left=0)
        return app.state.simulation_orders.get(identifier, user_id=USER)['report']
    gateway.query_order = query_original
    provider.activity['orders'].append({**close,'status':'FINISHED','volumeLeft':0})
    provider.activity['trades'].append({'clientOrderId':close['clientOrderId'],'tradeId':'close-fill',
        'symbol':close['symbol'],'direction':close['direction'],'offset':close['offset'],'price':820,'volume':1,'tradeDateTime':2,
        'tradingDay':provider.day})
    provider.position_items = []
    app.state.store.connection.execute('UPDATE ai_stop_orders SET updated_at=?',
                                      ((datetime.now(UTC)-timedelta(seconds=wall_age)).isoformat(),))
    app.state.store.connection.commit()
    async def advance(action, body=None):
        assert action == 'advance'
    replay_provider = SimpleNamespace(command=advance,snapshot={'status':'ready'},pending_step=None)
    context = ReplayContext(USER, app, replay_provider)
    # Pause the entry monitor: the protection must consume this step on its own.
    async def idle():
        return []
    app.state.strategy_monitor.run_once = idle
    await context.advance()
    stop = app.state.autonomy.list_stops(user_id=USER)[0]
    assert stop['status'] == 'EXECUTED' and stop['remainingVolume'] == 0
    assert stop['closeClientOrderId'] == close['clientOrderId'] and gateway.submit_count == 2
    app.state.store.close()


@pytest.mark.parametrize('method,path', [('account_activity','/api/account/activity'),
    ('market_workbench','/api/market/SHFE.au2612'), ('instrument_catalog','/api/instruments')])
async def test_slow_read_api_does_not_block_emergency_control(tmp_path, method, path):
    app, provider, gateway, _ = setup(tmp_path)
    session = app.state.broker.create_session('local', 'native', USER)
    app.state.autonomy.activate(user_id=USER, session_id=session.session_id, symbols=['SHFE.au2612'],
        permissions=['open','cancel'], max_order_volume=1, max_orders=2, duration_minutes=30)
    entered, release = threading.Event(), threading.Event()
    original = getattr(provider, method)
    def slow(*args):
        entered.set()
        assert release.wait(2)
        return original(*args)
    setattr(provider, method, slow)
    async with AsyncClient(transport=ASGITransport(app), base_url='http://runtime',
            headers={'Authorization':'Bearer test-local-token','Origin':'http://127.0.0.1:5173'}) as client:
        read = asyncio.create_task(client.get(path))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            stopped = await asyncio.wait_for(client.post('/api/ai-execution/stop', json={'emergency':True}), .5)
            assert stopped.status_code == 200
            assert stopped.json()['authorization']['status'] == 'EMERGENCY_STOP'
            assert not release.is_set()
        finally:
            release.set()
            result = await read
        assert result.status_code == 200
    assert gateway.submit_count == 0
    app.state.store.close()
