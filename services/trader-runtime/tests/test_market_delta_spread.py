"""扩容 ②③ (2026-10-13): K-line producers read source bar deltas (`barsVersion` → `barsDelta`) and fall
back to the whole window whenever a delta does not rebuild it; new channels spread over the sources."""
from __future__ import annotations

import asyncio
import random
import uuid
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from test_owner_auth import ORIGIN, PASSWORD_HASH, SESSION_SECRET
from trader_runtime.account_pool import PoolStartRefused
from trader_runtime.main import create_app
from trader_runtime.shared_market import SourceBars, bars_digest, market_patch

NS = 1_000_000_000
SYMBOLS = ('SHFE.rb2610', 'SHFE.au2612', 'SHFE.hc2610')


class VersionedSource:
    """The sidecar's public workbench protocol in miniature: per-bar versions, a floor raised when a
    reader could not rebuild the window from a delta, and the whole window for anyone else. `legacy`
    is a sidecar from before 扩容 ② (no barsVersion: always the whole window)."""

    def __init__(self, provider, *, legacy: bool = False) -> None:
        self.provider, self.legacy = provider, legacy
        self.calls: Counter[str] = Counter()
        self.answers: Counter[str] = Counter()
        self.series: dict[tuple[str, int], dict] = {}
        self.random = random.Random(5)
        self.corrupt_next = False

    def _series(self, symbol: str, duration: int) -> dict:
        key = (symbol, duration)
        if key not in self.series:
            value = self.provider.market_workbench(symbol, duration)
            bars = [dict(bar) for bar in value['bars']]
            self.series[key] = {'quote': value['quote'], 'bars': bars, 'versions': [1] * len(bars),
                                'epoch': uuid.uuid4().hex[:12], 'version': 1, 'floor': 1, 'revision': 1,
                                'duration': duration}
        return self.series[key]

    def _bump(self, series: dict) -> int:
        series['version'] += 1
        series['revision'] += 1
        return series['version']

    def tick(self, symbol: str, duration: int) -> None:
        series = self._series(symbol, duration)
        version = self._bump(series)
        bar = dict(series['bars'][-1])
        bar['close'] += self.random.choice([-1.0, 1.0])
        bar['high'] = max(bar['high'], bar['close'])
        bar['volume'] += 3
        series['bars'][-1], series['versions'][-1] = bar, version
        series['quote'] = {**series['quote'], 'lastPrice': bar['close']}

    def roll(self, symbol: str, duration: int, count: int = 1) -> None:
        series = self._series(symbol, duration)
        version = self._bump(series)
        for _ in range(count):
            last = series['bars'][-1]
            bar = {**last, 'datetime': last['datetime'] + duration * NS, 'open': last['close'], 'volume': 0.0}
            series['bars'] = series['bars'][1:] + [bar]
            series['versions'] = series['versions'][1:] + [version]

    def correct(self, symbol: str, duration: int) -> None:
        series = self._series(symbol, duration)
        version = self._bump(series)
        index = len(series['bars']) // 2
        series['bars'][index] = {**series['bars'][index], 'close': series['bars'][index]['close'] + 0.5}
        series['versions'][index] = version

    def replace(self, symbol: str, duration: int) -> None:
        """Every bar moves (a resend, another trading day attribution): readers get the whole window."""
        series = self._series(symbol, duration)
        version = self._bump(series)
        series['bars'] = [{**bar, 'datetime': bar['datetime'] + 60 * NS} for bar in series['bars']]
        series['versions'] = [version] * len(series['bars'])
        series['floor'] = version

    async def read(self, path: str):
        self.calls[path] += 1
        if path == '/v1/public-market/instruments':
            return self.provider.instrument_catalog()
        if path == '/v1/public-market/calendar':
            return self.provider.market_calendar()
        parts, query = urlsplit(path), parse_qs(urlsplit(path).query)
        assert parts.path.endswith('/workbench')
        series = self._series(parts.path.split('/')[-2], int(query['duration'][0]))
        if query.get('after') == [str(series['revision'])]:
            await asyncio.sleep(.02)  # a quiet long poll
        meta = {'schemaVersion': 1, 'durationSeconds': series['duration'], 'dataStatus': 'normal',
                'revision': str(series['revision']), 'quote': {**series['quote'], 'dataStatus': 'normal'}}
        if self.legacy:
            self.answers['legacy'] += 1
            return {**meta, 'bars': [dict(bar) for bar in series['bars']]}
        token = f"{series['epoch']}:{series['version']}"
        held = query.get('barsVersion', [''])[0]
        epoch, _, raw = held.rpartition(':')
        if epoch == series['epoch'] and raw.isdigit() and series['floor'] <= int(raw) <= series['version']:
            self.answers['delta'] += 1
            digest = bars_digest(series['bars'])
            if self.corrupt_next:
                self.corrupt_next, digest = False, '00000000'
            return {**meta, 'barsVersion': token, 'barsDelta': {
                'base': held, 'bars': [dict(bar) for bar, version in zip(series['bars'], series['versions'])
                                       if version > int(raw)],
                'first': series['bars'][0]['datetime'], 'count': len(series['bars']), 'digest': digest}}
        self.answers['whole'] += 1
        return {**meta, 'barsVersion': token, 'bars': [dict(bar) for bar in series['bars']]}


@pytest.fixture
async def market(tmp_path, monkeypatch):
    ids = [f'tqkq-sim:source{index}' for index in range(4)]
    monkeypatch.setenv('AI_TRADER_SHARED_MARKET_ACCOUNT_IDS', ','.join(ids))
    app = create_app(tmp_path / 'delta.db', runtime_state_root=tmp_path / 'state', agent_mode='native',
                     data_mode='fake', auth_mode='owner-password', owner_username='owner',
                     owner_password_hash=PASSWORD_HASH, owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN})
    service = app.state.shared_market
    provider = app.state.data_provider
    sources = {account_id: VersionedSource(provider) for account_id in ids}
    ports: dict[str, int] = {}

    async def market_source(account_id):
        return ports.setdefault(account_id, 19001 + len(ports))

    async def market(port, path):
        account_id = next(account for account, value in ports.items() if value == port)
        return await sources[account_id].read(path)

    service.ctx.account_pool = SimpleNamespace(market_source=market_source, client=SimpleNamespace(market=market))
    for account_id in ids:
        app.state.store.ensure_trading_account(account_id, execution_owner_user_id='delta-owner',
                                               provider_kind='tqkq-pool', display_name=account_id, sidecar_url=None)
    yield SimpleNamespace(service=service, sources=sources, ids=ids, started=ports)
    await service.close()
    await app.state.adapter.shutdown()
    app.state.store.close()


async def until(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


def source_bars(source: VersionedSource, key) -> list[dict]:
    return source.series[key]['bars']


async def test_the_producer_follows_deltas_and_publishes_the_same_bars_and_patches_as_whole_reads(market):
    service = market.service
    source = market.sources[market.ids[0]]
    key = (SYMBOLS[0], 300)
    channel = await service.acquire_market(*key)
    assert channel.source == market.ids[0]
    seen = [channel.packet]
    chance = random.Random(3)
    for step in range(120):
        roll = chance.random()
        if roll < .6:
            source.tick(*key)
        elif roll < .8:
            source.roll(*key, chance.choice([1, 1, 2]))
        elif roll < .9:
            source.correct(*key)
        elif roll < .95:
            source.replace(*key)
        else:
            source.tick(*key)
            source.roll(*key)
        await until(lambda: channel.packet['value']['workbench']['bars'] == source_bars(source, key))
        packet = channel.packet
        if packet is not seen[-1]:
            previous = seen[-1]
            assert packet['previous'] == previous['id']
            # The delta-built patch equals the patch a whole-window comparison would send.
            assert packet['patch'] == market_patch(previous['value'], packet['value']), step
            seen.append(packet)
    assert len(seen) > 60
    assert source.answers['delta'] > 5 * source.answers['whole']
    # Readers of the channel still see one whole snapshot and the window digest of the source.
    assert bars_digest(channel.packet['value']['workbench']['bars']) == bars_digest(source_bars(source, key))
    service.release_market(channel)


async def test_a_delta_that_does_not_rebuild_the_window_is_never_published_and_reads_whole(market):
    service = market.service
    source = market.sources[market.ids[0]]
    key = (SYMBOLS[0], 300)
    channel = await service.acquire_market(*key)
    await until(lambda: source.answers['delta'] >= 1)
    published = []
    original = service.publish_market

    def record(channel_, current, bars_change=None):
        published.append(current['workbench']['bars'])
        original(channel_, current, bars_change)
    service.publish_market = record
    whole = source.answers['whole']
    source.corrupt_next = True
    source.tick(*key)
    await until(lambda: source.answers['whole'] == whole + 1)
    await until(lambda: channel.packet['value']['workbench']['bars'] == source_bars(source, key))
    assert all(bars == source_bars(source, key) for bars in published)
    # And back on deltas after the whole read.
    deltas = source.answers['delta']
    source.tick(*key)
    await until(lambda: source.answers['delta'] > deltas)
    await until(lambda: channel.packet['value']['workbench']['bars'] == source_bars(source, key))
    service.release_market(channel)


async def test_an_older_source_without_bar_versions_keeps_serving_whole_windows(market):
    service = market.service
    for source in market.sources.values():
        source.legacy = True
    source = market.sources[market.ids[0]]
    key = (SYMBOLS[1], 60)
    channel = await service.acquire_market(*key)
    for _ in range(10):
        source.tick(*key)
        await until(lambda: channel.packet['value']['workbench']['bars'] == source_bars(source, key))
    source.roll(*key)
    await until(lambda: channel.packet['value']['workbench']['bars'] == source_bars(source, key))
    assert not any('barsVersion=' in path for path in source.calls)
    assert source.answers['legacy'] >= 11
    service.release_market(channel)


async def test_a_channel_moved_to_another_source_reads_the_whole_window_there_first(market):
    service = market.service
    first, second = (market.sources[account_id] for account_id in market.ids[:2])
    key = (SYMBOLS[2], 900)
    channel = await service.acquire_market(*key)
    await until(lambda: first.answers['delta'] >= 1)
    service.ctx.services.store.set_market_sources(market.ids[1:])
    service.reload_sources()
    second._series(*key)
    second.tick(*key)
    await until(lambda: channel.source == market.ids[1] and second.answers['whole'] >= 1)
    reads = [path for path in second.calls if '/workbench' in path]
    assert 'barsVersion=' not in reads[0]
    second.tick(*key)
    await until(lambda: channel.packet['value']['workbench']['bars'] == source_bars(second, key))
    await until(lambda: second.answers['delta'] >= 1)
    service.release_market(channel)


def test_source_bars_refuse_what_they_cannot_rebuild():
    bars = [{'datetime': index * 60 * NS, 'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 1.0,
             'openInterest': 2.0, 'tradingDay': '2026-10-13'} for index in range(1, 6)]
    held = SourceBars.whole('e:1', bars)
    assert SourceBars.whole(None, bars) is None  # an older sidecar: no deltas
    assert SourceBars.whole('e:1', list(reversed(bars))) is None
    forming = {**bars[-1], 'close': 1.75}
    grown = {**bars[-1], 'datetime': 6 * 60 * NS}
    good = {'base': 'e:1', 'bars': [forming], 'first': bars[0]['datetime'], 'count': 5,
            'digest': bars_digest([*bars[:-1], forming])}
    state, (changed, trimmed) = held.apply('e:2', good)
    assert state.bars == [*bars[:-1], forming] and changed == [forming] and not trimmed
    assert held.bars == bars  # the published bars never change under a reader
    rolled = {'bars': [grown], 'first': bars[1]['datetime'], 'count': 5, 'digest': bars_digest([*bars[1:], grown])}
    state, (changed, trimmed) = held.apply('e:3', rolled)
    assert state.bars == [*bars[1:], grown] and changed == [grown] and trimmed
    for broken in ({**good, 'digest': 'ffffffff'}, {**good, 'count': 6}, {**good, 'first': bars[1]['datetime']},
                   {**good, 'bars': [{'close': 1}]}, {**good, 'count': 0}, 'nope'):
        assert held.apply('e:2', broken) is None
    assert held.apply(None, good) is None


async def test_new_channels_spread_over_sources_and_the_last_source_starts_only_when_needed(market):
    """扩容 ③: list order up to the spread point, then the least loaded; a source's child starts
    (warm-up included) only when the sources before it approach the spread point."""
    service, ids = market.service, market.ids
    service.market_spread = 4
    keys = [(symbol, duration) for symbol in SYMBOLS for duration in (60, 180, 300, 900, 1800, 3600)]
    channels = []
    for index, key in enumerate(keys[:16]):
        channels.append(await service.acquire_market(*key))
        await asyncio.sleep(0)
        if index < 9:
            # The 行情专用 account is last in the list: nothing reads it (no child) before the third
            # source nears the spread point.
            assert ids[3] not in market.started, index
    assert [channel.source for channel in channels] == [account for account in ids for _ in range(4)]
    assert set(market.started) == set(ids)
    # Every source at the spread point: the least loaded takes the next one, list order breaks ties.
    more = [await service.acquire_market(*key) for key in keys[16:18]]
    assert [channel.source for channel in more] == ids[:2]
    # Pinned channels never move when load changes.
    for channel in channels[4:8]:
        service.release_market(channel)
        service.retire_market(channel)
    assert {account: service.source_states[account].channels for account in ids} == {
        ids[0]: 5, ids[1]: 1, ids[2]: 4, ids[3]: 4}
    assert (await service.acquire_market(*keys[4])).source == ids[1]
    assert all(channel.source == ids[0] for channel in channels[:4])


async def test_a_spread_at_the_hard_cap_keeps_filling_the_first_source(market):
    service, ids = market.service, market.ids
    service.market_spread = 48
    channels = [await service.acquire_market(SYMBOLS[0], duration) for duration in (60, 180, 300)]
    assert {channel.source for channel in channels} == {ids[0]}


async def test_after_a_failed_read_the_producer_reads_the_whole_window_again(market):
    """A dropped connection or a restarted source (another worker generation): no version is reused."""
    service = market.service
    source = market.sources[market.ids[0]]
    key = (SYMBOLS[1], 300)
    channel = await service.acquire_market(*key)
    await until(lambda: source.answers['delta'] >= 1)
    original, failed = source.read, []

    async def flaky(path):
        if '/workbench' in path and not failed:
            failed.append(path)
            raise PoolStartRefused('sidecar_public_market_unavailable')
        return await original(path)
    source.read = flaky
    source.tick(*key)
    await until(lambda: bool(failed))
    # The failed source backs off and the channel moves on (relay semantics unchanged): the next read,
    # wherever it lands, is a whole window, and deltas resume from there.
    wholes = sum(item.answers['whole'] for item in market.sources.values())
    await until(lambda: sum(item.answers['whole'] for item in market.sources.values()) > wholes, timeout=4)
    serving = market.sources[channel.source]
    deltas = serving.answers['delta']
    serving.tick(*key)
    await until(lambda: serving.answers['delta'] > deltas, timeout=4)
    await until(lambda: channel.packet['value']['workbench']['bars'] == source_bars(serving, key)
                and channel.error is None, timeout=4)
    service.release_market(channel)
