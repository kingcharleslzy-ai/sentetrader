"""冷序列首读与换源 (2026-10-14): a series a source is still loading answers
`market_series_not_ready` (the sidecar after its own ready wait). That answer is
"not yet", not a failure: the channel's one producer — every reader of the chart
shares it, the coalescing of Go's singleflight — retries, and the readers keep
waiting within their first-frame budget instead of all getting the 503. A channel
already serving keeps its last packet (marked stale) while its new source loads
(make-before-break). Both are bounded by `series_wait_seconds`."""
from __future__ import annotations

import asyncio
from collections import Counter

from test_market_source_relay import FIRST, SECOND, RelaySource, relay  # noqa: F401 - fixture
from trader_runtime.account_pool import PoolStartRefused
from trader_runtime.shared_market import bars_digest

PATH = '/api/markets/SHFE.rb2610/workbench'


class ColdSource(RelaySource):
    """A source whose series answer their first `cold` whole reads (no `after`) with
    market_series_not_ready after `delay`, the way the sidecar does when TqSdk is still
    loading a serial past its ready wait."""

    def __init__(self, provider, cold: int = 1, delay: float = .05):
        super().__init__(provider)
        self.cold, self.delay = cold, delay
        self.loads: Counter[str] = Counter()

    async def read(self, path: str):
        if '/workbench' in path and '&after=' not in path:
            series = path.split('&', 1)[0]
            self.loads[series] += 1
            if self.loads[series] <= self.cold:
                self.calls[path] += 1
                await asyncio.sleep(self.delay)
                raise PoolStartRefused('market_series_not_ready')
        return await super().read(path)


def bars_of(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return response.json()['bars']


async def test_sixty_first_reads_of_a_cold_series_share_one_load_and_get_no_503(relay):
    provider = relay.app.state.data_provider
    cold = relay.sources[FIRST] = ColdSource(provider)
    responses = await asyncio.gather(*(relay.member.get(PATH, params={'duration': 300}) for _ in range(60)))
    codes = Counter(response.status_code for response in responses)
    assert codes == {200: 60}, codes
    # One producer for the chart: one failed load and one good one, whatever the reader count.
    assert sum(cold.loads.values()) == 2 and len(relay.service.markets) == 1
    expected = provider.market_workbench('SHFE.rb2610', 300)['bars']
    assert all(bars_of(response) == expected for response in responses)


async def test_a_switch_to_a_cold_source_keeps_serving_the_last_packet(relay):
    """满额换源到冷源：新源载入期间读者照常拿到上一份（标 stale），不中断；新源就绪后数据与时间戳不变。"""
    service, provider = relay.service, relay.app.state.data_provider
    held = await service.acquire_market('SHFE.rb2610', 300)
    try:
        before = bars_of(await relay.member.get(PATH, params={'duration': 300}))
        cold = relay.sources[SECOND] = ColdSource(provider, cold=3, delay=.05)
        relay.sources[FIRST].limit_workbench = True  # A drops the series: the channel moves to cold B
        statuses = []
        async with asyncio.timeout(5):
            while True:
                response = await relay.member.get(PATH, params={'duration': 300})
                assert bars_of(response) == before  # never a 503, never other bars
                statuses.append(response.json()['dataStatus'])
                if sum(cold.loads.values()) > cold.cold and statuses[-1] == 'normal':  # B serves
                    break
                await asyncio.sleep(.01)
        assert 'stale' in statuses  # served while B was loading, and said so
        assert held.source == SECOND and held.error is None
        # K 线增量：新源上 since 增量拼回整窗与整份逐根一致。
        whole = bars_of(await relay.member.get(PATH, params={'duration': 300}))
        since = whole[-5]['datetime']
        part = (await relay.member.get(PATH, params={'duration': 300, 'since': since})).json()
        assert part['barsAfter'] == since and whole[:-4] + part['bars'] == whole
        assert part['barsWindow'] == {'first': whole[0]['datetime'], 'count': len(whole),
                                      'digest': bars_digest(whole[:-4])}
    finally:
        service.release_market(held)


async def test_a_series_that_never_loads_still_fails_within_the_bound(relay):
    service, provider = relay.service, relay.app.state.data_provider
    service.series_wait_seconds = .3
    relay.sources[FIRST] = ColdSource(provider, cold=10 ** 6, delay=.02)
    started = asyncio.get_running_loop().time()
    response = await relay.member.get(PATH, params={'duration': 300})
    assert response.status_code == 503 and response.json()['detail'] == 'market_series_not_ready'
    assert asyncio.get_running_loop().time() - started < 2
    # A serving chart whose source stays cold past the bound loses its normal status as before.
    relay.sources[FIRST] = RelaySource(provider)
    held = await service.acquire_market('SHFE.au2612', 300)
    try:
        relay.sources[SECOND] = ColdSource(provider, cold=10 ** 6, delay=.02)
        relay.sources[FIRST].limit_workbench = True
        async with asyncio.timeout(3):
            while held.error is None:
                await held.changed.wait()
        assert held.error.detail == 'market_series_not_ready'
        assert held.packet['value']['workbench']['dataStatus'] == 'unavailable'
    finally:
        service.release_market(held)


async def test_a_backup_chart_stays_backup_while_a_recovering_source_warms_up(relay, monkeypatch):
    """新浪备用中的图表：快期源恢复但序列还在载入时，图表仍如实标 backup（不改成 stale 丢掉来源），载好后回到快期。"""
    import time

    from test_market_backup import FakeSina, fail_all, install, until

    service = relay.service
    install(service, FakeSina())
    fail_all(relay)
    channel = await service.acquire_market('SHFE.au2612', 300)
    try:
        assert channel.packet['value']['workbench']['dataStatus'] == 'backup'
        for source in relay.sources.values():
            source.fail_reason = 'market_series_not_ready'  # reachable again, the series still loading
        monkeypatch.setattr(service, 'clock', lambda: time.time() + 120)  # past every backoff
        reads = sum(source.workbench_reads() for source in relay.sources.values())
        await until(lambda: sum(source.workbench_reads() for source in relay.sources.values()) > reads + 1)
        workbench = channel.packet['value']['workbench']
        assert (workbench['dataStatus'], workbench['quote']['dataStatus']) == ('backup', 'backup')
        assert channel.error is None
        for source in relay.sources.values():
            source.fail_reason = None
        await until(lambda: channel.packet['value']['workbench']['dataStatus'] == 'normal')
    finally:
        service.release_market(channel)


class GatedSource(RelaySource):
    """A source whose first whole workbench read waits (a cold load inside the sidecar's ready wait)
    until the test opens the gate, then answers — or answers `market_series_not_ready` after `hang`."""

    def __init__(self, provider, hang: float | None = None):
        super().__init__(provider)
        self.hang = hang
        self.entered, self.gate = asyncio.Event(), asyncio.Event()
        self.first = True

    async def read(self, path: str):
        if '/workbench' in path and '&after=' not in path and self.first:
            self.first = self.hang is not None  # a hanging source stays cold
            self.entered.set()
            if self.hang is not None:
                await asyncio.sleep(self.hang)
                raise PoolStartRefused('market_series_not_ready')
            await self.gate.wait()
        return await super().read(path)


async def test_a_switch_marks_the_chart_stale_before_the_new_source_answers(relay):
    """#893 跟进：换源后新源冷读取在答之前就要等几秒，这期间旧包原来仍标 normal。现在换源一刻就标 stale。"""
    service, provider = relay.service, relay.app.state.data_provider
    held = await service.acquire_market('SHFE.rb2610', 300)
    try:
        assert held.packet['value']['workbench']['dataStatus'] == 'normal'
        slow = relay.sources[SECOND] = GatedSource(provider)
        relay.sources[FIRST].limit_workbench = True
        async with asyncio.timeout(2):
            await slow.entered.wait()
        workbench = held.packet['value']['workbench']
        assert (workbench['dataStatus'], workbench['quote']['dataStatus']) == ('stale', 'stale')
        assert held.source == SECOND and held.error is None
        slow.gate.set()
        async with asyncio.timeout(2):
            while held.packet['value']['workbench']['dataStatus'] != 'normal':
                await held.changed.wait()
    finally:
        service.release_market(held)


async def test_the_switch_grace_counts_from_the_switch_not_the_first_failure(relay):
    """#893 跟进：宽限从换源起算。新源冷读取本身就等了超过宽限才答 not_ready，第一次失败就按失败处理，
    不再从这次失败起再给一整段宽限。"""
    service, provider = relay.service, relay.app.state.data_provider
    service.series_wait_seconds = .3
    held = await service.acquire_market('SHFE.rb2610', 300)
    try:
        relay.sources[SECOND] = GatedSource(provider, hang=.5)
        loop = asyncio.get_running_loop()
        switched = loop.time()
        relay.sources[FIRST].limit_workbench = True
        async with asyncio.timeout(2):
            while held.error is None:
                await held.changed.wait()
        assert loop.time() - switched < .8  # the first failure (≈.5 s), not .5 + .3 + another load
        assert held.error.detail == 'market_series_not_ready'
        assert held.packet['value']['workbench']['dataStatus'] == 'unavailable'
    finally:
        service.release_market(held)


async def test_the_source_diagnostic_code_reaches_the_chart_and_its_change_is_an_update(relay):
    """#516：dataErrorCode（盘前/无成交/渲染失败）原来不在白名单里，过共享行情就丢了，变化也不触发更新。"""
    service, provider = relay.service, relay.app.state.data_provider

    class Diagnosed(RelaySource):
        code: str | None = 'market_quote_pre_open'

        async def read(self, path: str):
            result = await super().read(path)
            if '/workbench' in path:
                result = {**result, 'dataErrorCode': self.code,
                          'quote': {**result['quote'], 'dataErrorCode': self.code}}
            return result

    source = relay.sources[FIRST] = Diagnosed(provider)
    held = await service.acquire_market('SHFE.rb2610', 300)
    try:
        value = held.packet['value']
        assert value['quote']['dataErrorCode'] == 'market_quote_pre_open'
        assert value['workbench']['dataErrorCode'] == 'market_quote_pre_open'
        packet = held.packet['id']
        source.code = None
        source.generation += 1  # the sidecar's revision moves with its display error
        async with asyncio.timeout(2):
            while held.packet['id'] == packet:
                await held.changed.wait()
        assert held.packet['value']['quote']['dataErrorCode'] is None
        assert held.packet['patch']['quote']['dataErrorCode'] is None
    finally:
        service.release_market(held)
