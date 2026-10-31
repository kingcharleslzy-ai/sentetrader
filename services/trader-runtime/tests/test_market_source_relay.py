"""公共行情多账号接力 (2026-10-04): the ordered source list in SQLite (env-seeded
once), per-source channel caps with relay on limit/failure and no reclaim after
recovery, the quote top-up across sources, the main-sidecar source reading
through its own provider, and the owner-only market-sources routes."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from test_owner_auth import ORIGIN, PASSWORD, PASSWORD_HASH, SESSION_SECRET
from trader_runtime.account_pool import PoolStartRefused
from trader_runtime.auth import OWNER_COOKIE_NAME
from trader_runtime.main import create_app

FIRST, SECOND = 'tqkq-sim:first', 'tqkq-sim:second'


class RelaySource:
    """One fake public source: serves catalog/board/quotes/workbench from the
    app's fake provider, with per-test failure and limit injection. Workbench
    reads long-poll quietly once the revision matches, so producer loops stay
    calm while tests steer them."""

    def __init__(self, provider):
        self.provider = provider
        self.calls: Counter[str] = Counter()
        self.generation = 0
        self.fail_reason: str | None = None
        self.limit_workbench = False
        self.limit_quotes: set[str] = set()

    def workbench_reads(self) -> int:
        return sum(count for path, count in self.calls.items() if '/workbench' in path)

    def quotes_reads(self) -> int:
        return sum(count for path, count in self.calls.items() if path.startswith('/v1/public-market/quotes'))

    async def read(self, path: str):
        self.calls[path] += 1
        if self.fail_reason:
            raise PoolStartRefused(self.fail_reason)
        if path == '/v1/public-market/instruments':
            return self.provider.instrument_catalog()
        if path == '/v1/public-market/board':
            return self.provider.market_board()
        if path == '/v1/public-market/calendar':
            return self.provider.market_calendar()
        if path.startswith('/v1/public-market/quotes?symbols='):
            wanted = path.split('symbols=', 1)[1].split(',')
            payload = self.provider.market_quotes(wanted)
            rows = [{'symbol': row['symbol'], 'errorCode': 'market_subscription_limit'}
                    if row.get('symbol') in self.limit_quotes else row
                    for row in payload.get('rows', [])]
            return {**payload, 'rows': rows}
        parts, query = urlsplit(path), parse_qs(urlsplit(path).query)
        symbol = parts.path.split('/')[-2]
        if self.limit_workbench:
            raise PoolStartRefused('market_subscription_limit')
        if query.get('after') != [str(self.generation)]:
            self.generation += 1
        else:
            await asyncio.sleep(.05)
        value = self.provider.market_workbench(symbol, int(query['duration'][0]))
        return {**value, 'revision': str(self.generation), 'dataStatus': 'normal',
                'quote': {**value['quote'], 'dataStatus': 'normal'}}


@pytest.fixture
async def relay(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_TRADER_SHARED_MARKET_ACCOUNT_IDS', f'{FIRST},{SECOND}')
    monkeypatch.setenv('AI_TRADER_GUEST_MARKET_ACCOUNT_ID', 'must-not-use-legacy')
    app = create_app(tmp_path / 'relay.db', runtime_state_root=tmp_path / 'state',
                     agent_mode='native', data_mode='fake', auth_mode='owner-password',
                     owner_username='owner', owner_password_hash=PASSWORD_HASH,
                     owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN})
    service = app.state.shared_market
    provider = app.state.data_provider
    sources = {FIRST: RelaySource(provider), SECOND: RelaySource(provider)}
    ports: dict[str, int] = {}

    async def market_source(account_id):
        return ports.setdefault(account_id, 19001 + len(ports))

    async def market(port, path):
        account_id = next(account for account, value in ports.items() if value == port)
        return await sources[account_id].read(path)

    service.ctx.account_pool = SimpleNamespace(market_source=market_source,
                                               client=SimpleNamespace(market=market))
    store = app.state.store
    for account_id, name in ((FIRST, '一号源'), (SECOND, '二号源')):
        store.ensure_trading_account(account_id, execution_owner_user_id='relay-owner',
                                     provider_kind='tqkq-pool', display_name=name, sidecar_url=None)
    owner = AsyncClient(transport=ASGITransport(app=app), base_url='https://runtime.test',
                        headers={'origin': ORIGIN})
    assert (await owner.post('/api/auth/owner', json={'username': 'owner', 'password': PASSWORD})).status_code == 200
    store.create_cloud_user(username='alice', password_hash=PASSWORD_HASH, invited_by=None)
    member = AsyncClient(transport=ASGITransport(app=app), base_url='https://runtime.test',
                         headers={'origin': ORIGIN})
    member.cookies.set(OWNER_COOKIE_NAME, app.state.auth._issue_session('alice', PASSWORD_HASH))
    yield SimpleNamespace(app=app, service=service, sources=sources, store=store, owner=owner, member=member)
    for client in (owner, member):
        await client.aclose()
    await service.close()
    await app.state.adapter.shutdown()
    app.state.store.close()


async def test_channels_fill_the_first_source_then_relay_to_the_second(relay):
    """两源：一号源满了新通道落到二号源；已有通道不迁移；一号源通道退役后新通道回到一号源。"""
    service = relay.service
    service.market_capacity = 1
    first = await service.acquire_market('SHFE.rb2610', 300)
    second = await service.acquire_market('SHFE.au2612', 300)
    assert (first.source, second.source) == (FIRST, SECOND)
    with pytest.raises(HTTPException, match='guest_market_capacity'):
        await service.acquire_market('SHFE.rb2610', 60)
    assert service.markets[('SHFE.rb2610', 300)].source == FIRST  # 已固定的通道不迁移
    service.release_market(second)
    service.retire_market(second)
    third = await service.acquire_market('SHFE.hc2610', 300)
    assert third.source == SECOND  # 二号源名额已释放
    service.release_market(first)
    service.retire_market(first)
    fourth = await service.acquire_market('SHFE.rb2610', 60)
    assert fourth.source == FIRST  # 一号源通道退役后，新通道回到一号源


async def test_a_subscription_limit_moves_the_channel_and_retries_once(relay):
    relay.sources[FIRST].limit_workbench = True
    channel = await relay.service.acquire_market('SHFE.rb2610', 300)
    assert channel.source == SECOND  # 满额：通道转下一源，本次读取就在新源上成功
    assert channel.packet is not None and channel.packet['value']['workbench']['bars']
    assert relay.sources[FIRST].workbench_reads() == 1  # 只试了一次就转走


async def test_an_unavailable_source_loses_channels_and_never_reclaims_them(relay):
    service = relay.service
    channel = await service.acquire_market('SHFE.rb2610', 300)
    assert channel.source == FIRST
    relay.sources[FIRST].fail_reason = 'account_unavailable'
    async with asyncio.timeout(2):
        while channel.packet['value']['workbench']['dataStatus'] == 'normal':
            await channel.changed.wait()
    # 换源期间旧价显式降级为 stale，不继续冒充 normal；读者照常拿到它、不报错（2026-10-14 先接后断）。
    assert channel.packet['value']['workbench']['dataStatus'] == 'stale' and channel.error is None
    async with asyncio.timeout(2):
        while channel.source != SECOND or channel.packet['value']['workbench']['dataStatus'] != 'normal':
            await channel.changed.wait()
    assert channel.error is None
    assert channel.packet['value']['workbench']['dataStatus'] == 'normal'  # 读者数据恢复
    relay.sources[FIRST].fail_reason = None
    reads = relay.sources[FIRST].workbench_reads()
    await asyncio.sleep(.2)  # 恢复后跑过几个生产轮次
    assert channel.source == SECOND
    assert relay.sources[FIRST].workbench_reads() == reads  # 恢复后不抢回已转走的通道


async def test_all_sources_full_or_unavailable_report_their_own_error(relay):
    service = relay.service
    service.market_capacity = 1
    await service.acquire_market('SHFE.rb2610', 300)
    await service.acquire_market('SHFE.au2612', 300)
    with pytest.raises(HTTPException, match='guest_market_capacity'):
        await service.acquire_market('SHFE.hc2610', 300)
    for source in relay.sources.values():
        source.fail_reason = 'account_unavailable'
    service.cache.clear()
    with pytest.raises(HTTPException, match='guest_market_source_unavailable'):
        await service.acquire_market('SHFE.hc2610', 300)


async def test_quote_rows_capped_on_the_first_source_are_completed_by_the_second(relay):
    service = relay.service
    relay.sources[FIRST].limit_quotes = {'SHFE.au2612'}
    payload = await service.quotes(['shfe.rb2610', 'SHFE.AU2612'])
    assert [row['symbol'] for row in payload['rows']] == ['SHFE.au2612', 'SHFE.rb2610']  # 顺序与请求一致
    assert all('errorCode' not in row for row in payload['rows'])
    assert relay.sources[FIRST].quotes_reads() == 1 and relay.sources[SECOND].quotes_reads() == 1
    service.cache.clear()
    await service.quotes(['SHFE.rb2610'])  # 无满额行时不再碰二号源
    assert relay.sources[SECOND].quotes_reads() == 1


async def test_quote_rows_are_shared_per_contract_across_different_sets(relay):
    """按合约缓存 (2026-10-12): readers holding different sets share their common contracts — a
    contract cached or already being read is not asked again; only the rest goes up, in one read."""
    service, first = relay.service, relay.sources[FIRST]

    def quote_paths():
        return [path for path in first.calls.elements() if path.startswith('/v1/public-market/quotes')]

    await service.quotes(['SHFE.rb2610', 'SHFE.au2612'])
    payload = await service.quotes(['SHFE.au2612', 'SHFE.hc2610'])
    assert [row['symbol'] for row in payload['rows']] == ['SHFE.au2612', 'SHFE.hc2610']
    assert quote_paths() == ['/v1/public-market/quotes?symbols=SHFE.AU2612,SHFE.RB2610',
                             '/v1/public-market/quotes?symbols=SHFE.HC2610']
    # Concurrent sets: the second joins the first's read for the contract they share.
    service.cache.clear()
    first.calls.clear()
    together = await asyncio.gather(service.quotes(['SHFE.rb2610', 'SHFE.au2612']),
                                    service.quotes(['SHFE.au2612', 'SHFE.hc2610']))
    assert [[row['symbol'] for row in payload['rows']] for payload in together] == [
        ['SHFE.au2612', 'SHFE.rb2610'], ['SHFE.au2612', 'SHFE.hc2610']]
    assert quote_paths() == ['/v1/public-market/quotes?symbols=SHFE.AU2612,SHFE.RB2610',
                             '/v1/public-market/quotes?symbols=SHFE.HC2610']
    assert not service.quote_pending


async def test_each_kind_of_shared_read_has_its_own_in_flight_budget(relay):
    """2026-10-12: a burst of quote reads fills only the quotes budget — the board, catalog and
    calendar still go up instead of answering guest_market_capacity."""
    service = relay.service
    service.inflight_limit = 2
    gate = asyncio.Event()

    async def held():
        await gate.wait()
        return {'rows': []}
    readers = [asyncio.create_task(service.shared(('quotes', name), held, 1)) for name in ('A', 'B')]
    await asyncio.sleep(0)
    with pytest.raises(HTTPException, match='guest_market_capacity'):
        await service.shared(('quotes', 'C'), held, 1)
    assert (await service.board())['rows']
    assert (await service.instruments())['items']
    assert (await service.calendar())['tradingDay']
    gate.set()
    await asyncio.gather(*readers)


async def test_a_retired_channel_stops_even_when_its_read_swallows_the_cancellation(relay):
    """2026-10-12 计数对不上: an HTTP timeout firing in the instant a retired channel's producer was
    cancelled turned the cancellation into a failed read; the producer carried on, pinned itself to
    the next source and kept long-polling — the source counted a channel the Runtime no longer had."""
    service, first, second = relay.service, relay.sources[FIRST], relay.sources[SECOND]
    channel = await service.acquire_market('SHFE.rb2610', 300)
    assert channel.source == FIRST
    parked = asyncio.Event()
    original = first.read

    async def swallowing(path):
        if 'waitMs=' not in path:
            return await original(path)
        parked.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise PoolStartRefused('sidecar_public_market_unavailable') from None
    first.read = swallowing
    async with asyncio.timeout(2):
        await parked.wait()
    service.release_market(channel)
    service.retire_market(channel)
    async with asyncio.timeout(2):
        await asyncio.gather(channel.task, return_exceptions=True)
    reads = second.workbench_reads()
    await asyncio.sleep(.2)
    assert not service.markets
    assert {account: state.channels for account, state in service.source_states.items()} == {FIRST: 0, SECOND: 0}
    assert second.workbench_reads() == reads == 0


async def test_a_main_sidecar_account_serves_as_a_source_through_its_provider(relay):
    app, service, store = relay.app, relay.service, relay.store
    store.ensure_trading_account('tqkq-sidecar:main', execution_owner_user_id='relay-owner',
                                 provider_kind='tqkq-sidecar', display_name='主 Sidecar', sidecar_url=None)
    served: list[str] = []

    class Provider:
        async def public_market(self, path):
            served.append(path)
            assert path.startswith('/v1/public-market/')
            return app.state.data_provider.instrument_catalog() \
                if path == '/v1/public-market/instruments' else {'schemaVersion': 1, 'rows': []}

    def get(account_id):  # AccountRegistry.get 是同步方法
        return SimpleNamespace(provider=Provider()) if account_id == 'tqkq-sidecar:main' else None

    service.ctx.services.accounts.get = get
    assert (await relay.owner.put('/api/admin/market-sources',
                                  json={'accountIds': ['tqkq-sidecar:main']})).status_code == 200
    service.cache.clear()
    assert (await service.instruments())['items']
    # 走主 sidecar 自己的 provider，不进连接池；目录顺带读一次交易日历（交割提醒扣节假日，2026-10-12）
    assert served == ['/v1/public-market/instruments', '/v1/public-market/calendar']
    assert not relay.sources[FIRST].calls and not relay.sources[SECOND].calls


async def test_the_market_source_routes_are_owner_only_and_validate_their_input(relay):
    owner, member = relay.owner, relay.member
    # 读取走 require_owner 的 404（不暴露存在性）；写与 PUT /api/admin/plans 一致，
    # 被 admin-console 技能网关以 403 拒绝；匿名 401。
    assert (await member.get('/api/admin/market-sources')).status_code == 404
    assert (await member.put('/api/admin/market-sources', json={'accountIds': []})).status_code == 403
    async with AsyncClient(transport=ASGITransport(app=relay.app), base_url='https://runtime.test',
                           headers={'origin': ORIGIN}) as anonymous:
        assert (await anonymous.get('/api/admin/market-sources')).status_code == 401
        assert (await anonymous.put('/api/admin/market-sources', json={'accountIds': []})).status_code == 401
    listed = await owner.get('/api/admin/market-sources')
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [item['accountId'] for item in body['items']] == [FIRST, SECOND]
    assert body['seriesPerSource'] == 48
    item = body['items'][0]
    assert item['providerKind'] == 'tqkq-pool' and item['displayName'] == '一号源' and item['health'] == 'healthy'
    assert {'accountId', 'position', 'providerKind', 'displayName', 'accountStatus', 'health',
            'channels', 'channelLimit', 'quoteSymbols', 'lastError', 'lastErrorAt'} <= item.keys()
    # 只有库中 active 的池/sidecar 账户能进清单；重复与未知字段都 422。
    relay.store.ensure_trading_account('paper:u1', execution_owner_user_id='relay-owner',
                                       provider_kind='paper', display_name='本站模拟', sidecar_url=None)
    for bad in (['tqkq-sim:missing'], [FIRST, FIRST], ['paper:u1']):
        refused = await owner.put('/api/admin/market-sources', json={'accountIds': bad})
        assert refused.status_code == 422, (bad, refused.text)
    assert (await owner.put('/api/admin/market-sources',
                            json={'accountIds': [FIRST], 'extra': 1})).status_code == 422
    # PUT 立即生效：目录读取改走新的一号位，不用重启。
    assert (await owner.put('/api/admin/market-sources',
                            json={'accountIds': [SECOND, FIRST]})).status_code == 200
    relay.service.cache.clear()
    before = sum(relay.sources[SECOND].calls.values())
    assert (await relay.service.instruments())['items']
    assert sum(relay.sources[SECOND].calls.values()) > before


async def test_board_and_calendar_fail_over_to_the_next_healthy_source(relay):
    service = relay.service
    relay.sources[FIRST].fail_reason = 'account_unavailable'
    payload = await service.board()
    assert payload['rows']
    assert sum(relay.sources[FIRST].calls.values()) == 1  # 一号源只被试了一次
    assert relay.sources[SECOND].calls['/v1/public-market/board'] == 1
    calendar = await service.calendar()
    assert calendar['tradingDay'] and calendar['recentTradingDays']


async def test_calendar_keeps_the_last_good_copy_when_every_source_fails(relay):
    """2026-10-12 数据获取审查：sidecar 全挂时交易日历不再是空/503——state_root 里最近一次成功的
    日历标 stale 照用，重启后也在；从没成功过才如实失败。"""
    service = relay.service
    good = await service.calendar()
    assert good['tradingDay'] and 'stale' not in good
    for source in relay.sources.values():
        source.fail_reason = 'account_unavailable'
    service.cache.clear()
    kept = await service.calendar()
    assert kept['stale'] is True and kept['savedAt']
    assert (kept['tradingDay'], kept['recentTradingDays']) == (good['tradingDay'], good['recentTradingDays'])
    restarted = type(service)(service.ctx)
    try:
        assert (await restarted.calendar())['tradingDay'] == good['tradingDay']
        # A copy that no longer names the current trading day (an outage across days) is no calendar:
        # the replay dialog must not default to a day sessions behind.
        saved = json.loads(restarted._calendar_file.read_text(encoding='utf-8'))
        restarted._calendar_file.write_text(json.dumps({**saved, 'tradingDay': '2026-01-05'}), encoding='utf-8')
        restarted.cache.clear()
        with pytest.raises(HTTPException):
            await restarted.calendar()
        restarted._calendar_file.unlink()
        restarted.cache.clear()
        with pytest.raises(HTTPException):
            await restarted.calendar()
    finally:
        await restarted.close()


async def test_an_empty_calendar_on_a_cold_start_is_unavailable_not_a_good_answer(relay, monkeypatch):
    """#788：冷启动没有存档时，sidecar 答一份空日历曾被当成功返回（并共享 10 秒）；应如实报不可用（路由 503）。"""
    for source in relay.sources.values():
        monkeypatch.setattr(source.provider, 'market_calendar',
                            lambda: {'tradingDay': '', 'previousTradingDay': '', 'recentTradingDays': []})
    with pytest.raises(RuntimeError, match='market_calendar_unavailable'):
        await relay.service.calendar()


def _seed_service(tmp_path, monkeypatch, *, plural, single, legacy) -> tuple:
    from trader_runtime.shared_market import SharedMarketService
    from trader_runtime.store import RuntimeStore
    monkeypatch.setenv('AI_TRADER_SHARED_MARKET_ACCOUNT_IDS', plural)
    monkeypatch.setenv('AI_TRADER_SHARED_MARKET_ACCOUNT_ID', single)
    monkeypatch.setenv('AI_TRADER_GUEST_MARKET_ACCOUNT_ID', legacy)
    store = RuntimeStore(tmp_path / 'seed.sqlite3')
    service = SharedMarketService(SimpleNamespace(
        services=SimpleNamespace(store=store), settings=SimpleNamespace(data_mode='fake')))
    return service, store


def test_the_source_list_seeds_from_the_environment_once(tmp_path, monkeypatch):
    """播种优先级：ACCOUNT_IDS > 单账号变量 > 旧变量；播种后以库为准，环境再变不影响。"""
    service, store = _seed_service(tmp_path, monkeypatch, plural=f' {FIRST}, {SECOND} ',
                                   single='tqkq-sim:single-legacy', legacy='tqkq-sim:guest-legacy')
    assert service._ordered_sources() == [FIRST, SECOND]  # 新变量优先且按序、去空白
    assert store.list_market_sources() == [FIRST, SECOND] and store.market_sources_seeded()
    monkeypatch.setenv('AI_TRADER_SHARED_MARKET_ACCOUNT_IDS', 'tqkq-sim:other')
    service.market_account_id = ''
    service.reload_sources()
    assert service._ordered_sources() == [FIRST, SECOND]  # 已播种：以库为准
    store.close()


def test_the_source_list_falls_back_to_the_legacy_single_id(tmp_path, monkeypatch):
    service, store = _seed_service(tmp_path, monkeypatch, plural='', single='tqkq-sim:single-legacy',
                                   legacy='tqkq-sim:guest-legacy')
    assert service._ordered_sources() == ['tqkq-sim:single-legacy']
    assert store.list_market_sources() == ['tqkq-sim:single-legacy']
    store.close()


def test_an_unseeded_empty_list_stays_live_until_something_exists(tmp_path, monkeypatch):
    """空清单不落库：环境/属性之后仍能生效（兼容测试与部署期改写）。"""
    service, store = _seed_service(tmp_path, monkeypatch, plural='', single=' ', legacy=' ')
    assert service._ordered_sources() == []
    assert not store.market_sources_seeded()
    service.market_account_id = 'tqkq-sim:written-later'
    service.reload_sources()  # 管理端/属性改写后丢弃 5 秒清单缓存再读
    assert service._ordered_sources() == ['tqkq-sim:written-later']
    assert store.market_sources_seeded()  # 有内容才播种
    store.close()
