import asyncio
import copy

import pytest
from httpx import ASGITransport, AsyncClient

from test_strategy_workbench import IDENTIFIER, SYMBOL, CycleSource, bars, plan
from trader_runtime.main import create_app
from trader_runtime.replay import ReplayProvider
from trader_runtime.strategy_chart_period import bind_definition
from trader_runtime.strategy_rules import StrategyPlan, evaluate_rules, resolve_chart_timeframe


def chart_plan():
    raw = plan().model_dump()
    raw['timeframeSeconds'] = 0
    for group in raw['rules'].values():
        for condition in group['conditions']:
            condition['left']['timeframe'] = 0
    return StrategyPlan.model_validate(raw)


def test_relative_indicators_resolve_without_mutating_library_or_explicit_daily_rules():
    raw = chart_plan().model_dump()
    raw['rules']['entryLong']['conditions'].append({'label': '固定日线均线',
        'left': {'indicator': 'ma', 'period': 5, 'timeframe': 86400}, 'operator': 'gt',
        'right': {'indicator': 'ma', 'period': 10, 'timeframe': 86400}})
    original = StrategyPlan.model_validate(raw)
    for period in [60, 180, 300, 900, 3600, 86400]:
        bound = resolve_chart_timeframe(original, period)
        assert bound.timeframeSeconds == period
        assert bound.rules.entryLong.conditions[0].left.timeframe == period
        assert bound.rules.entryLong.conditions[1].left.timeframe == 86400
        assert bound.rules.entryLong.conditions[1].right.period == 10
        assert original.rules.entryLong.conditions[0].left.timeframe == 0
    with pytest.raises(ValueError, match='图表周期'):
        resolve_chart_timeframe(original, None)


def test_relative_signals_use_selected_closed_bars_and_ignore_forming_bar():
    original = chart_plan()
    for period, closed, forming, expected in [(60, 824, 1, True), (180, 822, 999, False)]:
        result = evaluate_rules(resolve_chart_timeframe(original, period), {period: {'bars': bars([closed]*40 + [forming], period)}})
        assert result['rules']['entryLong'] is expected
        assert result['rules']['entryShort'] is not expected


def test_all_explicit_periods_stay_fixed_even_when_execution_clock_is_unspecified():
    raw = plan().model_dump()
    raw['timeframeSeconds'] = 0
    for group in raw['rules'].values():
        for condition in group['conditions']:
            condition['left']['timeframe'] = 86400
    fixed = StrategyPlan.model_validate(raw)
    assert fixed.timeframeSeconds == 86400


@pytest.mark.asyncio
async def test_chart_switch_is_atomic_preserves_execution_state_and_waits_for_inflight_decision(tmp_path, monkeypatch):
    source = CycleSource()
    source.data['workbenches'].pop('300', None)
    source.data['workbenches']['180'] = {**copy.deepcopy(source.data['workbenches']['60']), 'bars': bars([824]*180, 180)}
    provider = ReplayProvider(source, IDENTIFIER)
    await provider.command('state')
    app = create_app(tmp_path/'replay.sqlite3', runtime_state_root=tmp_path, agent_mode='fake',
        data_provider=provider, simulation_gateway=provider, _replay_provider=provider, local_token='chart-test')
    store, monitor = app.state.store, app.state.strategy_monitor
    headers = {'Origin': 'http://127.0.0.1:5173', 'Authorization': 'Bearer chart-test'}
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url='http://runtime', headers=headers) as client:
            saved = (await client.post('/api/strategy-plans', json={'plan': chart_plan().model_dump()})).json()
            library_id = saved['strategyId']
            assert (await client.post(f'/api/strategy-plans/{library_id}/validate', json={'symbol': SYMBOL})).status_code == 422
            validation = await client.post(f'/api/strategy-plans/{library_id}/validate', json={'symbol': SYMBOL, 'chartTimeframeSeconds':60})
            assert validation.status_code == 200, validation.text
            identifier = validation.json()['strategyId']
            assert (await client.post(f'/api/strategies/{identifier}/monitor/start', json={})).status_code == 200
            original = store.get_strategy_draft(identifier, user_id='local-user')
            state = store.save_strategy_monitor_state(strategy_id=identifier, user_id='local-user', trading_date='2026-09-04',
                high_watermark_equity='1000000', last_today_pnl='-10', consecutive_losses=2, last_trade_id='prior-close',
                last_executed_bar='2026-09-03T12:58:00+00:00', pause_reason=None)
            fixed = (await client.post('/api/strategy-plans', json={'plan': plan().model_dump()})).json()
            fixed_id = (await client.post(f"/api/strategy-plans/{fixed['strategyId']}/validate", json={'symbol': SYMBOL})).json()['strategyId']
            fixed_before = store.get_strategy_draft(fixed_id, user_id='local-user')
            entered, release = asyncio.Event(), asyncio.Event()
            seen = []
            async def held_decision(item):
                seen.append(item['definition']['plan']['timeframeSeconds'])
                entered.set()
                await release.wait()
                return {'status': 'waiting'}
            monkeypatch.setattr(monitor, '_run_strategy_bound', held_decision)
            decision = asyncio.create_task(monitor._run_strategy(original))
            await entered.wait()
            switching = asyncio.create_task(client.post('/api/strategy-plans/chart-period', json={'symbol': SYMBOL, 'timeframeSeconds':180}))
            await asyncio.sleep(0)
            assert not switching.done()
            assert store.get_strategy_draft(identifier, user_id='local-user')['definitionDigest'] == original['definitionDigest']
            release.set()
            await decision
            changed = await switching
            assert changed.status_code == 200, changed.text
            rebound = store.get_strategy_draft(identifier, user_id='local-user')
            assert rebound['strategyId'] == identifier and rebound['status'] == 'monitoring'
            assert rebound['definition']['plan']['timeframeSeconds'] == 180
            assert rebound['definition']['chartPlan']['timeframeSeconds'] == 0
            assert store.strategy_monitor_state(strategy_id=identifier, user_id='local-user') == state
            assert store.get_strategy_draft(fixed_id, user_id='local-user') == fixed_before
            assert store.get_strategy_draft(library_id, user_id='local-user')['definition']['plan']['timeframeSeconds'] == 0
            await monitor._run_strategy(original)  # A previously read item must be refreshed under the lock.
            assert seen == [60, 180]
            failed = await client.post('/api/strategy-plans/chart-period', json={'symbol': SYMBOL, 'timeframeSeconds':300})
            assert failed.status_code == 409, failed.text
            assert store.get_strategy_draft(identifier, user_id='local-user') == rebound
            assert store.strategy_monitor_state(strategy_id=identifier, user_id='local-user') == state
            assert not source.submits
    finally:
        store.close()


def test_period_change_after_the_close_only_needs_computable_bars():
    # 2026-09-25 (Mid-Autumn holiday): switching the chart to 5 minutes answered 409 "所需周期行情正在连接或已过期"
    # because the rebinding re-checked quote freshness; the chart itself then refused to switch.
    from trader_runtime.strategy_rules import StrategyRuleError, evaluate_plan
    closed = {'bars': bars([824] * 200, 60), 'dataStatus': 'closed', 'ageSeconds': 70000, 'isCached': True}

    class ClosedMarket:
        def market_workbench(self, symbol, timeframe):
            return copy.deepcopy(closed)

    definition = {'plan': plan().model_dump()}
    with pytest.raises(StrategyRuleError):
        evaluate_plan(ClosedMarket(), definition, SYMBOL)          # order paths keep the freshness gate
    assert evaluate_plan(ClosedMarket(), definition, SYMBOL, require_fresh=False)['evaluatedBarTime']


@pytest.mark.asyncio
async def test_chart_period_changes_only_selected_account_and_uses_its_provider(tmp_path, monkeypatch):
    from test_account_registry import BETA_ACCOUNT, DEFAULT_ACCOUNT, dual_account_app, local_client
    from trader_runtime import strategy_chart_period

    app, provider_a, provider_b = dual_account_app(tmp_path, monkeypatch, "chart-accounts.sqlite3")
    store = app.state.store
    raw = {"chartPlan": chart_plan().model_dump()}
    definition = bind_definition(raw, 60)
    for identifier, account_id in (("alpha", DEFAULT_ACCOUNT), ("beta", BETA_ACCOUNT)):
        store.save_strategy_draft(strategy_id=identifier, user_id="local-user", source_session_id="test",
            name=identifier, symbol=SYMBOL, version=1, status="ready", definition=definition,
            definition_digest=identifier, trading_account_id=account_id)
    seen = []
    default_available = [False]

    def check_provider(source, *_args, **_kwargs):
        seen.append(source)
        if source is provider_a and not default_available[0]:
            raise RuntimeError("default source unavailable")
        return {"evaluatedBarTime": "2026-09-26T00:00:00Z"}

    monkeypatch.setattr(strategy_chart_period, "evaluate_plan", check_provider)
    try:
        client = await local_client(app)
        try:
            switched = await client.post("/api/strategy-plans/chart-period",
                headers={"X-AI-Trader-Account": BETA_ACCOUNT},
                json={"symbol": SYMBOL, "timeframeSeconds": 180})
            assert switched.status_code == 200, switched.text
            assert switched.json()["strategyIds"] == ["beta"]
            assert seen == [provider_b]
            assert store.get_strategy_draft("alpha", user_id="local-user")["definition"]["chartTimeframeSeconds"] == 60
            assert store.get_strategy_draft("beta", user_id="local-user")["definition"]["chartTimeframeSeconds"] == 180
            with store.lock, store.connection:
                store.connection.execute("DELETE FROM account_grants WHERE account_id=? AND user_id=?",
                                         (BETA_ACCOUNT, "local-user"))
            refused = await client.post("/api/strategy-plans/chart-period",
                headers={"X-AI-Trader-Account": BETA_ACCOUNT},
                json={"symbol": SYMBOL, "timeframeSeconds": 300})
            assert refused.status_code == 403
            default = await client.post("/api/strategy-plans/chart-period",
                headers={"X-AI-Trader-Account": DEFAULT_ACCOUNT},
                json={"symbol": SYMBOL, "timeframeSeconds": 180})
            assert default.status_code == 409
            assert store.get_strategy_draft("beta", user_id="local-user")["definition"]["chartTimeframeSeconds"] == 180
            default_available[0] = True
            alpha = await client.post("/api/strategy-plans/chart-period",
                headers={"X-AI-Trader-Account": DEFAULT_ACCOUNT},
                json={"symbol": SYMBOL, "timeframeSeconds": 180})
            assert alpha.status_code == 200 and alpha.json()["strategyIds"] == ["alpha"]
            assert store.get_strategy_draft("beta", user_id="local-user")["definition"]["chartTimeframeSeconds"] == 180
        finally:
            await client.aclose()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_library_validation_preserves_legacy_execution_and_isolates_accounts(tmp_path, monkeypatch):
    from uuid import NAMESPACE_URL, uuid5

    from test_account_registry import BETA_ACCOUNT, DEFAULT_ACCOUNT, dual_account_app, local_client

    app, provider_a, provider_b = dual_account_app(tmp_path, monkeypatch, "validation-accounts.sqlite3")
    store = app.state.store
    client = await local_client(app)
    try:
        saved = (await client.post("/api/strategy-plans", json={"plan": chart_plan().model_dump()})).json()
        # A deployed execution with the old account-less id must retain its id and state.
        legacy_id = str(uuid5(NAMESPACE_URL,
            f'local-user:{saved["strategyId"]}:{saved["definitionDigest"]}:{SYMBOL.casefold()}'))
        definition = bind_definition({**saved["definition"], "libraryStrategyId": saved["strategyId"]}, 60)
        store.save_strategy_draft(strategy_id=legacy_id, user_id="local-user",
            source_session_id=saved["sourceSessionId"], name="已有执行", symbol=SYMBOL, version=1,
            status="ready", definition=definition, definition_digest="legacy-digest",
            trading_account_id=DEFAULT_ACCOUNT)
        body = {"symbol": SYMBOL, "chartTimeframeSeconds": 60}
        route = f'/api/strategy-plans/{saved["strategyId"]}/validate'
        alpha = await client.post(route, json=body)
        assert alpha.status_code == 200 and alpha.json()["strategyId"] == legacy_id

        def unavailable(*args, **kwargs):
            raise RuntimeError("账户甲行情不可用")

        monkeypatch.setattr(provider_a, "market_workbench", unavailable)
        headers = {"X-AI-Trader-Account": BETA_ACCOUNT}
        beta = await client.post(route, headers=headers, json=body)
        assert beta.status_code == 200, beta.text
        beta_id = beta.json()["strategyId"]
        assert beta_id != legacy_id
        assert store.get_strategy_draft(beta_id, user_id="local-user")["tradingAccountId"] == BETA_ACCOUNT
        counts = tuple(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                       for table in ("strategy_drafts", "sessions"))
        repeated = await client.post(route, headers=headers, json=body)
        assert repeated.status_code == 200 and repeated.json()["strategyId"] == beta_id
        assert counts == tuple(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                               for table in ("strategy_drafts", "sessions"))
        refused = await client.post(f"/api/strategy-plans/{legacy_id}/validate", headers=headers, json=body)
        assert refused.status_code == 422 and "其他交易账户" in refused.json()["detail"]
        changed = await client.post("/api/strategy-plans/chart-period", headers=headers,
                                    json={"symbol": SYMBOL, "timeframeSeconds": 180})
        assert changed.status_code == 200 and changed.json()["strategyIds"] == [beta_id]
        legacy = store.get_strategy_draft(legacy_id, user_id="local-user")
        assert legacy["definitionDigest"] == "legacy-digest"
        assert legacy["definition"]["chartTimeframeSeconds"] == 60 and legacy["status"] == "ready"
        assert (await client.post(route, json=body)).status_code == 422
    finally:
        await client.aclose()
        store.close()
