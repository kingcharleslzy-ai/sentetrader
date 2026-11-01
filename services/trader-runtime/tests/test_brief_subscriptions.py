"""AI 简报多品种订阅 (2026-10-05): 档位上限 briefProducts（basic 3 / pro 10 的种子与
老库迁移）、默认订阅推导（自选前 N，无自选取当前主力所在品种）、GET/PUT
/api/app/brief-subscriptions、不带 symbol 的合并读取与轮转刷新，以及档位调低后的
overLimit——全部走公共 HTTP 面。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from test_guest_market_fanout import PublicSource
from test_owner_auth import ORIGIN, PASSWORD, PASSWORD_HASH, SESSION_SECRET
from trader_runtime.auth import OWNER_COOKIE_NAME
from trader_runtime.main import create_app
from trader_runtime.market_briefs import MarketBriefs
from trader_runtime.plans import UNLIMITED, plan_for_user, plan_from_config
from trader_runtime.store import RuntimeStore


def news_item(text: str, published: datetime, minutes: int = 30) -> dict:
    now = datetime.now(UTC)
    return {"id": "news:" + text, "kind": "news", "text": text, "source": "www.shfe.com.cn",
            "url": "https://www.shfe.com.cn/" + text + ".html",
            "publishedAt": published.isoformat(), "eventAt": published.isoformat(),
            "expiresAt": (now + timedelta(minutes=minutes)).isoformat()}


@pytest.fixture
async def brief_app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A shared-market Runtime with a basic-tier member and the owner logged in;
    the model boundary (`_collect`) is stubbed per test from outside."""
    monkeypatch.setenv('AI_TRADER_SHARED_MARKET_ACCOUNT_ID', 'tqkq-sim:public')
    monkeypatch.setenv('AI_TRADER_GUEST_MARKET_ACCOUNT_ID', 'must-not-use-legacy')
    app = create_app(tmp_path / 'briefs.db', runtime_state_root=tmp_path / 'state',
                     agent_mode='native', data_mode='fake', auth_mode='owner-password',
                     owner_username='owner', owner_password_hash=PASSWORD_HASH,
                     owner_session_secret=SESSION_SECRET, cors_origins={ORIGIN})
    source = PublicSource(app)

    async def market_source(account):
        return 19001

    async def market(port, path):
        return await source.read(path, None)

    service = app.state.shared_market
    service.ctx.account_pool = SimpleNamespace(market_source=market_source,
                                               client=SimpleNamespace(market=market))
    app.state.store.create_cloud_user(username='alice', password_hash=PASSWORD_HASH, invited_by=None)
    member = AsyncClient(transport=ASGITransport(app=app), base_url='https://runtime.test',
                         headers={'origin': ORIGIN})
    member.cookies.set(OWNER_COOKIE_NAME, app.state.auth._issue_session('alice', PASSWORD_HASH))
    owner = AsyncClient(transport=ASGITransport(app=app), base_url='https://runtime.test',
                        headers={'origin': ORIGIN})
    assert (await owner.post('/api/auth/owner', json={'username': 'owner', 'password': PASSWORD})).status_code == 200
    # The rotation test drives the real pending path; the adapter is otherwise
    # unauthenticated and every refresh would stop at "unavailable".
    monkeypatch.setattr(type(app.state.adapter), 'agent_authenticated',
                        property(lambda self: True))
    collected: list[str] = []

    async def record_collect(symbol):
        collected.append(symbol)

    monkeypatch.setattr(app.state.market_briefs, '_collect', record_collect)
    yield app, member, owner, collected
    await member.aclose()
    await owner.aclose()
    await service.close()
    await app.state.adapter.shutdown()
    app.state.store.close()


async def put_subscriptions(client: AsyncClient, products: list[str]):
    return await client.put('/api/app/brief-subscriptions', json={'products': products})


# -- the tier key --------------------------------------------------------------

def test_fresh_tiers_seed_brief_products_and_plan_reads_follow(tmp_path: Path):
    store = RuntimeStore(tmp_path / 'seed.sqlite3')
    try:
        assert store.get_plan_tier('basic')['config']['briefProducts'] == 3
        assert store.get_plan_tier('pro')['config']['briefProducts'] == 10
        # The disposable test tier predates the key on purpose: reading it
        # falls back to 3, only an explicit null lifts the cap.
        assert 'briefProducts' not in store.get_plan_tier('test')['config']
        assert plan_for_user(store, 'nobody').brief_products is None
        assert plan_from_config('basic', '基础档', {'skills': []}).brief_products == 3
        assert plan_from_config('basic', '基础档', {'briefProducts': 0, 'skills': []}).brief_products == 0
        assert plan_from_config('basic', '基础档', {'briefProducts': None, 'skills': []}).brief_products is None
        assert plan_from_config('guest', '游客', {}).brief_products == 0
        assert UNLIMITED.brief_products is None
    finally:
        store.close()


async def test_admin_plan_surface_edits_brief_products_by_key(brief_app):
    _, _, owner, _ = brief_app
    listed = (await owner.get('/api/admin/plans')).json()['items']
    by_name = {item['name']: item for item in listed}
    assert by_name['basic']['briefProducts'] == 3 and by_name['pro']['briefProducts'] == 10
    edited = await owner.put('/api/admin/plans/pro', json={'briefProducts': 6})
    assert edited.status_code == 200 and edited.json()['briefProducts'] == 6
    # Other keys keep their merge semantics: a briefProducts-only edit does not
    # touch the tier's tokens.
    assert edited.json()['dailyTokens'] == 2_000_000
    unlimited = await owner.put('/api/admin/plans/pro', json={'briefProducts': None})
    assert unlimited.status_code == 200 and unlimited.json()['briefProducts'] is None
    assert (await owner.put('/api/admin/plans/pro', json={'briefProducts': -1})).status_code == 422


# -- the default derivation and the two endpoints ------------------------------

async def test_default_subscriptions_follow_watchlist_then_symbol_then_board(brief_app):
    app, member, _, _ = brief_app
    # No watchlist, no choice: the caller's current main contract wins, else the
    # board's most-traded main contract (the fake board's rb).
    board_default = (await member.get('/api/app/brief-subscriptions')).json()
    assert board_default == {'products': [{'product': 'SHFE.rb', 'name': '螺纹钢',
                                           'symbol': 'SHFE.rb2610'}],
                             'limit': 3, 'source': 'default', 'overLimit': False}
    hinted = (await member.get('/api/app/brief-subscriptions', params={'symbol': 'SHFE.au2612'})).json()
    assert [item['product'] for item in hinted['products']] == ['SHFE.au']

    # A watchlist becomes the default, capped to the tier's first N.
    assert (await member.put('/api/preferences', json={'watchlist': {'products': [
        'SHFE.au', 'DCE.i', 'SHFE.rb', 'CFFEX.IF']}})).status_code == 200
    followed = (await member.get('/api/app/brief-subscriptions')).json()
    assert [item['product'] for item in followed['products']] == ['SHFE.au', 'DCE.i', 'SHFE.rb']
    assert followed['source'] == 'default' and followed['limit'] == 3

    # The user's own choice wins over the watchlist, deduped, order kept.
    saved = await put_subscriptions(member, ['DCE.i', 'SHFE.rb', 'DCE.i'])
    assert saved.status_code == 200
    assert saved.json() == {'products': [{'product': 'DCE.i', 'name': '铁矿石', 'symbol': 'DCE.i2609'},
                                         {'product': 'SHFE.rb', 'name': '螺纹钢', 'symbol': 'SHFE.rb2610'}],
                            'limit': 3, 'source': 'user', 'overLimit': False}
    # The choice lives in the same preferences document every writer shares.
    document = (await member.get('/api/preferences')).json()['preferences']
    assert document['briefs']['products'] == ['DCE.i', 'SHFE.rb']


async def test_put_refuses_unknown_products_over_cap_lists_and_accepts_empty(brief_app):
    app, member, owner, _ = brief_app
    unknown = await put_subscriptions(member, ['SHFE.rb', 'XXX.zz'])
    assert unknown.status_code == 422
    assert unknown.json()['detail'] == {'code': 'brief_product_unknown', 'product': 'XXX.zz'}
    over = await put_subscriptions(member, ['SHFE.au', 'DCE.i', 'SHFE.rb', 'CFFEX.IF'])
    assert over.status_code == 422
    assert over.json()['detail'] == {'code': 'brief_limit_exceeded', 'limit': 3}
    # Both refusals stored nothing: the derivation still reports the default.
    assert (await member.get('/api/app/brief-subscriptions')).json()['source'] == 'default'

    empty = await put_subscriptions(member, [])
    assert empty.status_code == 200 and empty.json()['products'] == [] and empty.json()['source'] == 'user'
    merged = await member.get('/api/app/briefs')
    assert merged.status_code == 200 and merged.json() == {'items': []}

    # The owner is unlimited: the same four products save.
    assert (await owner.put('/api/admin/plans/basic', json={'briefProducts': 4})).status_code == 200
    four = await put_subscriptions(member, ['SHFE.au', 'DCE.i', 'SHFE.rb', 'CFFEX.IF'])
    assert four.status_code == 200 and len(four.json()['products']) == 4
    assert (await owner.put('/api/admin/plans/basic', json={'briefProducts': 3})).status_code == 200


# -- the merged read and the rotation refresh -----------------------------------

async def test_merged_read_covers_subscriptions_newest_first(brief_app):
    app, member, _, _ = brief_app
    assert (await put_subscriptions(member, ['DCE.i', 'SHFE.au'])).status_code == 200
    briefs = app.state.market_briefs
    older, newer = datetime.now(UTC) - timedelta(minutes=20), datetime.now(UTC) - timedelta(minutes=2)
    briefs.cache['SHFE.au2612'] = news_item('au-old', older)
    briefs.cache['DCE.i2609'] = news_item('i-new', newer)

    merged = (await member.get('/api/app/briefs')).json()
    assert [item['text'] for item in merged['items']] == ['i-new', 'au-old']
    assert merged['items'][0]['product'] == 'DCE.i' and merged['items'][0]['productName'] == '铁矿石'
    assert merged['items'][0]['symbol'] == 'DCE.i2609'
    assert merged['items'][1]['product'] == 'SHFE.au'

    # The 1.6.x single-symbol contract is byte-identical to before: the same
    # cache, no product fields, movement first when one qualifies.
    single = await member.get('/api/app/briefs', params={'symbol': 'SHFE.au2612'})
    assert single.status_code == 200
    assert single.json() == {'items': [briefs.cache['SHFE.au2612']]}

    monkeypatch_disabled = pytest.MonkeyPatch()
    monkeypatch_disabled.setattr(briefs, 'enabled', lambda user: False)
    try:
        disabled = await member.get('/api/app/briefs')
        assert disabled.json() == {'items': [], 'status': 'disabled'}
    finally:
        monkeypatch_disabled.undo()


async def test_refresh_rotates_through_subscriptions_and_keeps_the_gates(brief_app):
    app, member, _, collected = brief_app
    briefs = app.state.market_briefs
    assert (await put_subscriptions(member, ['SHFE.au', 'DCE.i', 'SHFE.rb'])).status_code == 200

    def reopen_global_gate():
        briefs.last_global = 0.0

    first = await member.post('/api/app/briefs/refresh', json={})
    assert first.status_code == 200 and first.json() == {'status': 'pending', 'product': 'SHFE.au'}
    await asyncio.sleep(0)  # let the stubbed collect task run
    assert collected[-1] == 'SHFE.au2612'
    # The site-wide 60-second spacing still binds even with every product due.
    crowded = await member.post('/api/app/briefs/refresh', json={})
    assert crowded.json() == {'status': 'idle'}

    reopen_global_gate()
    second = await member.post('/api/app/briefs/refresh', json={})
    assert second.json() == {'status': 'pending', 'product': 'DCE.i'}
    await asyncio.sleep(0)  # let the stubbed collect task run
    reopen_global_gate()
    third = await member.post('/api/app/briefs/refresh', json={})
    assert third.json() == {'status': 'pending', 'product': 'SHFE.rb'}
    await asyncio.sleep(0)  # let the stubbed collect task run
    reopen_global_gate()
    # Every product was attempted within the last 10 minutes: nothing is due.
    exhausted = await member.post('/api/app/briefs/refresh', json={})
    assert exhausted.json() == {'status': 'idle'}
    # A product whose attempt aged out becomes due again (rotation continues
    # from the cursor, wrapping to the front).
    briefs.last_attempt['SHFE.au2612'] = -600
    reopen_global_gate()
    wrapped = await member.post('/api/app/briefs/refresh', json={})
    assert wrapped.json() == {'status': 'pending', 'product': 'SHFE.au'}
    await asyncio.sleep(0)  # let the stubbed collect task run

    # The symbol contract stays exactly the 1.6.x shape — status only, and the
    # per-symbol 10-minute rule applies to it too.
    old_path = await member.post('/api/app/briefs/refresh', json={'symbol': 'SHFE.au2612'})
    assert old_path.status_code == 200 and set(old_path.json()) == {'status'}
    assert old_path.json()['status'] == 'idle'


# -- one shared retrieval per symbol (owner 2026-10-05) -------------------------

async def test_one_retrieval_per_symbol_is_shared_across_users(brief_app, monkeypatch):
    """两个用户订阅同一品种：检索全站只发生一次（且不再带用户、不计入任何人的
    用量），10 分钟内第二人直接复用；一人关闭简讯只挡自己的读取。"""
    app, member, owner, _ = brief_app
    briefs = app.state.market_briefs
    assert (await put_subscriptions(member, ['SHFE.rb'])).status_code == 200
    assert (await put_subscriptions(owner, ['SHFE.rb'])).status_code == 200

    # Drive the real _collect: a fresh shared quote plus a faked model
    # boundary that records every call (any stray user argument would surface
    # in `retrieved`).
    monkeypatch.setattr(briefs, '_collect', MethodType(MarketBriefs._collect, briefs))

    async def fresh_snapshot(symbol, turn_id=""):
        return {"dataStatus": "normal", "ageSeconds": 0,
                "name": "螺纹钢2610", "symbol": symbol}

    monkeypatch.setattr(app.state.shared_market, 'snapshot', fresh_snapshot)
    retrieved: list[tuple[str, str, dict]] = []

    async def record_news(adapter, symbol, product, **unexpected):
        retrieved.append((symbol, product, unexpected))
        return news_item('rb-shared', datetime.now(UTC))

    monkeypatch.setattr('trader_runtime.market_briefs.collect_news', record_news)

    first = await member.post('/api/app/briefs/refresh', json={})
    assert first.status_code == 200 and first.json() == {'status': 'pending', 'product': 'SHFE.rb'}
    for _ in range(20):
        if briefs.task is not None and briefs.task.done():
            break
        await asyncio.sleep(0)
    assert retrieved == [('SHFE.rb2610', '螺纹钢', {})]
    assert list(briefs.cache) == ['SHFE.rb2610']

    # The other subscriber inside the 10-minute window: no second retrieval.
    second = await owner.post('/api/app/briefs/refresh', json={})
    assert second.json() == {'status': 'idle'}
    assert len(retrieved) == 1

    # Both read the one shared story (labels carry each reader's product).
    member_items = (await member.get('/api/app/briefs')).json()['items']
    owner_items = (await owner.get('/api/app/briefs')).json()['items']
    assert [item['id'] for item in member_items] == [item['id'] for item in owner_items]
    assert [item['text'] for item in member_items] == ['rb-shared']
    assert member_items[0]['product'] == 'SHFE.rb'

    # Turning briefs off is one reader's own gate: disabled for them, the
    # shared story (and retrieval) untouched for everyone else.
    revision = (await member.get('/api/app/config')).json()['revision']
    off = await member.put('/api/app/config', json={'revision': revision,
                                                    'config': {'showMarketBriefs': False}})
    assert off.status_code == 200
    assert (await member.get('/api/app/briefs')).json() == {'items': [], 'status': 'disabled'}
    assert [(item['text'], item['product']) for item in (await owner.get('/api/app/briefs')).json()['items']] \
        == [('rb-shared', 'SHFE.rb')]


# -- the story outlives the process --------------------------------------------

async def _generate_one_story(app, member, briefs, monkeypatch, item):
    """Drive the real `_collect` once for SHFE.rb with a faked model boundary; `item` is the
    story to return, NO_NEWS / None (explicitly empty / inconclusive), or an exception to raise."""
    monkeypatch.setattr(briefs, '_collect', MethodType(MarketBriefs._collect, briefs))
    briefs.last_attempt.clear()
    briefs.last_global = 0.0
    briefs.task = None

    async def fresh_snapshot(symbol, turn_id=""):
        return {"dataStatus": "normal", "ageSeconds": 0, "name": "螺纹钢2610", "symbol": symbol}

    monkeypatch.setattr(app.state.shared_market, 'snapshot', fresh_snapshot)

    async def fake_news(adapter, symbol, product):
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr('trader_runtime.market_briefs.collect_news', fake_news)
    assert (await member.post('/api/app/briefs/refresh', json={})).json()['status'] == 'pending'
    for _ in range(20):
        if briefs.task is not None and briefs.task.done():
            break
        await asyncio.sleep(0)
    assert briefs.task.done()


async def test_a_verified_story_survives_a_runtime_restart(brief_app, monkeypatch):
    """10-08 线上：Runtime 盘中连续部署重启后内存缓存全站清空，休市窗口（行情不新鲜）又不能重新
    检索，订阅者读到的都是空列表。A stored story rehydrates into a fresh process on read until its
    own expiresAt; an expired row is not resurrected."""
    app, member, _, _ = brief_app
    briefs = app.state.market_briefs
    assert (await put_subscriptions(member, ['SHFE.rb'])).status_code == 200
    await _generate_one_story(app, member, briefs, monkeypatch, news_item('rb-restart', datetime.now(UTC)))
    assert [item['text'] for item in (await member.get('/api/app/briefs')).json()['items']] == ['rb-restart']

    # The restart: a fresh MarketBriefs over the same store, nothing in memory.
    restarted = MarketBriefs(app, briefs.settings)
    assert restarted.cache == {}
    assert [item['text'] for item in (await restarted.read('alice'))['items']] == ['rb-restart']
    assert list(restarted.cache) == ['SHFE.rb2610']  # rehydrated on read, still one shared story

    # The row dies at its own expiry, not at the process's.
    restarted._store('SHFE.rb2610', news_item('rb-too-old', datetime.now(UTC), minutes=-1))
    stale_items = (await restarted.read('alice', 'SHFE.rb2610'))['items']
    assert not [item for item in stale_items if item.get('text') == 'rb-too-old']


async def test_only_an_explicitly_empty_search_withdraws_the_stored_story(brief_app, monkeypatch):
    """2026-10-12 数据获取审查：检索成功且明确为空才撤下全站共享的已核实简报；模型异常或结论不明
    （没检索、候选没过核验）保留未过期的旧简报，它照样在自己的 expiresAt 消失。"""
    from trader_runtime.market_briefs import NO_NEWS
    app, member, _, _ = brief_app
    briefs = app.state.market_briefs
    assert (await put_subscriptions(member, ['SHFE.rb'])).status_code == 200
    await _generate_one_story(app, member, briefs, monkeypatch, news_item('rb-first', datetime.now(UTC)))
    assert [item['text'] for item in (await member.get('/api/app/briefs')).json()['items']] == ['rb-first']

    # A model error keeps the verified story, in memory and in the stored row a restart reads.
    await _generate_one_story(app, member, briefs, monkeypatch, RuntimeError('model down'))
    assert [item['text'] for item in (await member.get('/api/app/briefs')).json()['items']] == ['rb-first']
    assert MarketBriefs(app, briefs.settings)._brief_for('SHFE.rb2610')['text'] == 'rb-first'
    # An inconclusive attempt (no search ran / the candidate failed verification) keeps it too.
    await _generate_one_story(app, member, briefs, monkeypatch, None)
    assert [item['text'] for item in (await member.get('/api/app/briefs')).json()['items']] == ['rb-first']

    # The search ran and found nothing qualifying: withdrawn everywhere.
    await _generate_one_story(app, member, briefs, monkeypatch, NO_NEWS)
    assert 'SHFE.rb2610' not in briefs.cache
    assert (await member.get('/api/app/briefs')).json()['items'] == []
    assert MarketBriefs(app, briefs.settings)._brief_for('SHFE.rb2610') is None


# -- the tier cut ---------------------------------------------------------------

async def test_tier_cut_flags_over_limit_and_covers_only_the_first_n(brief_app):
    app, member, owner, _ = brief_app
    briefs = app.state.market_briefs
    assert (await put_subscriptions(member, ['DCE.i', 'SHFE.rb', 'SHFE.au'])).status_code == 200
    assert (await owner.put('/api/admin/plans/basic', json={'briefProducts': 1})).status_code == 200

    flagged = (await member.get('/api/app/brief-subscriptions')).json()
    assert flagged['overLimit'] is True and flagged['limit'] == 1
    # The stored choice is never trimmed: the whole list stays visible for edits.
    assert [item['product'] for item in flagged['products']] == ['DCE.i', 'SHFE.rb', 'SHFE.au']

    fresh = datetime.now(UTC)
    briefs.cache['DCE.i2609'] = news_item('i-live', fresh)
    briefs.cache['SHFE.rb2610'] = news_item('rb-hidden', fresh)
    merged = (await member.get('/api/app/briefs')).json()
    assert [item['text'] for item in merged['items']] == ['i-live']

    refreshed = await member.post('/api/app/briefs/refresh', json={})
    assert refreshed.json() == {'status': 'pending', 'product': 'DCE.i'}
    await asyncio.sleep(0)  # let the stubbed collect task run
    briefs.last_global = 0.0
    assert (await member.post('/api/app/briefs/refresh', json={})).json() == {'status': 'idle'}

    # Raising the cap back restores the full coverage without any rewrite.
    assert (await owner.put('/api/admin/plans/basic', json={'briefProducts': 3})).status_code == 200
    restored = (await member.get('/api/app/brief-subscriptions')).json()
    assert restored['overLimit'] is False
    merged_again = (await member.get('/api/app/briefs')).json()
    assert {item['text'] for item in merged_again['items']} == {'i-live', 'rb-hidden'}


async def test_after_a_tier_cut_the_extra_products_cancel_one_at_a_time(brief_app):
    """3 → 1：每次“取消”立即保存剩余整表；仍超限的删减也要能存下，否则超出两个以上时
    每一次取消都被整表拒绝、永远删不到上限内。新增或换入别的品种仍按上限拒绝。"""
    app, member, owner, _ = brief_app
    assert (await put_subscriptions(member, ['DCE.i', 'SHFE.rb', 'SHFE.au'])).status_code == 200
    assert (await owner.put('/api/admin/plans/basic', json={'briefProducts': 1})).status_code == 200

    # Swapping in a product that was not stored is not a trim: refused whole.
    swapped = await put_subscriptions(member, ['DCE.i', 'CFFEX.IF'])
    assert swapped.status_code == 422
    assert swapped.json()['detail'] == {'code': 'brief_limit_exceeded', 'limit': 1}

    first = await put_subscriptions(member, ['DCE.i', 'SHFE.au'])
    assert first.status_code == 200
    assert [item['product'] for item in first.json()['products']] == ['DCE.i', 'SHFE.au']
    assert first.json()['overLimit'] is True
    # Still over: adding back what was just removed is growth, not a trim.
    assert (await put_subscriptions(member, ['DCE.i', 'SHFE.au', 'SHFE.rb'])).status_code == 422

    second = await put_subscriptions(member, ['SHFE.au'])
    assert second.status_code == 200
    assert second.json()['overLimit'] is False
    assert [item['product'] for item in second.json()['products']] == ['SHFE.au']
    # Inside the cap the normal rule is back: two products are over a cap of 1.
    assert (await put_subscriptions(member, ['SHFE.au', 'DCE.i'])).status_code == 422
    assert (await owner.put('/api/admin/plans/basic', json={'briefProducts': 3})).status_code == 200


# -- the capability catalog ------------------------------------------------------

async def test_catalog_lists_and_gates_the_subscription_routes(brief_app):
    _, member, owner, _ = brief_app

    async def operations(client) -> set[str]:
        listed = await client.get('/api/agent/capabilities')
        return {item['operation'] for item in listed.json()['operations']}

    listed = await operations(member)
    assert {'GET /api/app/brief-subscriptions', 'PUT /api/app/brief-subscriptions',
            'GET /api/app/briefs', 'POST /api/app/briefs/refresh'} <= listed

    # market-briefs owns the routes: the owner's global switch takes the write
    # away (an AI-feature skill binds the human surface too) and hides all four
    # from the catalog.
    assert (await owner.put('/api/admin/skills/market-briefs', json={'enabled': False})).status_code == 200
    assert not ({'GET /api/app/brief-subscriptions', 'PUT /api/app/brief-subscriptions',
                 'POST /api/app/briefs/refresh'} & await operations(member))
    refused = await put_subscriptions(member, ['SHFE.rb'])
    assert refused.status_code == 403 and refused.json()['detail'] == 'skill_disabled'
    assert (await member.post('/api/app/briefs/refresh', json={})).status_code == 403
    assert (await member.get('/api/app/briefs')).json() == {'items': [], 'status': 'disabled'}

    assert (await owner.put('/api/admin/skills/market-briefs', json={'enabled': True})).status_code == 200
    assert 'PUT /api/app/brief-subscriptions' in await operations(member)
    recovered = await put_subscriptions(member, ['SHFE.rb'])
    assert recovered.status_code == 200


async def test_a_foreign_product_is_subscribed_and_briefed_from_its_major_screened_flash(brief_app, monkeypatch):
    """2026-10-21 所有者：外盘的 AI 简报也要有。外盘品种（键同自选，CME.NQ）能订阅，合约是它的外盘主连图表；简报取它
    24 小时内被 AI 判为“重要”的快讯事件（中文、已按纳指筛过），不调模型；没有重要事件时撤下旧的。"""
    from trader_runtime.market_briefs import MarketBriefs

    app, member, _, _ = brief_app
    saved = await put_subscriptions(member, ['SHFE.rb', 'CME.NQ'])
    assert saved.status_code == 200, saved.text
    nasdaq = next(item for item in saved.json()['products'] if item['product'] == 'CME.NQ')
    assert nasdaq == {'product': 'CME.NQ', 'name': '迷你纳指100', 'symbol': 'KQD.m@CME.NQ'}

    now = datetime.now(UTC)
    events: list[dict] = [
        {'id': 'e-minor', 'tier': 'other', 'publishedAt': (now - timedelta(minutes=5)).isoformat(), 'title': '纳指小幅波动',
         'sources': ['财联社'], 'items': []},
        {'id': 'e-major', 'tier': 'major', 'publishedAt': (now - timedelta(minutes=20)).isoformat(),
         'title': '美股股指期货扩大涨幅，纳指期货涨超1%', 'sources': ['财联社', '东方财富'],
         'items': [{'link': 'https://www.cls.cn/detail/1'}]}]
    seen: list[str] = []

    class _Live:
        def product_events(self, product: str):
            seen.append(product)
            return '纳指', list(events)

    monkeypatch.setattr(app.state, 'research_live', _Live(), raising=False)
    briefs = app.state.market_briefs
    await MarketBriefs._collect(briefs, 'KQD.m@CME.NQ')
    read = await member.get('/api/app/briefs', params={'symbol': 'KQD.m@CME.NQ'})
    assert read.status_code == 200, read.text
    story = next(item for item in read.json()['items'] if item.get('kind') == 'news')
    assert story['text'] == '美股股指期货扩大涨幅，纳指期货涨超1%' and story['url'] == 'https://www.cls.cn/detail/1'
    assert seen == ['nasdaq']
    events.clear()
    await MarketBriefs._collect(briefs, 'KQD.m@CME.NQ')
    again = await member.get('/api/app/briefs', params={'symbol': 'KQD.m@CME.NQ'})
    assert not [item for item in again.json()['items'] if item.get('kind') == 'news']
