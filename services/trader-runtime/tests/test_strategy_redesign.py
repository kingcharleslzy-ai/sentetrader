"""策略页改版第八批 (2026-10-08): the one-sentence main line, stored backtests, “AI 怎么看”, 试几组参数比一比,
停止并平仓, the 今日判断 clash, and the #530-C lab fixes.

What each test protects:
* one sentence → design job (fake model) → saved strategy → backtest → the card's latestBacktest / planRows;
* backtest results survive a restart and are trimmed per strategy; another user never reads them;
* the verdict's facts are computed from the trades (best month's share, what is left without it, the
  tuned-on-the-same-period warning) and a model sentence with a number the facts do not hold is refused;
* the comparison runs three groups of the key parameter in the cloud and says the held-out stretch;
* 停止并平仓 makes close cards in the conversation and never sends an order, also for 自动下单;
* the clash appears only for 偏多 / 偏空 against the strategy's direction;
* #530-C: a browser upload never replaces the cloud run, a cancelled run keeps its slot until the
  worker finishes, and the cloud-run registry forgets finished runs.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from test_strategy_monitor import USER, AutonomyStub, DataStub
from test_strategy_templates import app_for, continuous_fake_schedule
from trader_runtime.research_desk import stance_clash
from trader_runtime.store import RuntimeStore
from trader_runtime.strategy_lab import compare_line, value_words
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_plain import plain_rows
from trader_runtime.strategy_templates import build_plan, templates
from trader_runtime.strategy_verdict import check_sentence, model_verdict, verdict_facts

HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer tpl-token"}
DAY = 86400 * 10**9
JOB = "0123456789abcdef0123456789abcdef"


def client_for(app):
    return AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS)


def trade(month: int, net: float, day: int = 10) -> dict[str, Any]:
    close = int(datetime(2026, month, day, 6, tzinfo=UTC).timestamp() * 1e9)
    return {"openTime": close - 3600 * 10**9, "closeTime": close, "net": net}


# -- “AI 怎么看” -------------------------------------------------------------------------------------------

def test_verdict_facts_name_the_best_month_and_what_is_left_without_it():
    trades = [trade(7, 1000), trade(8, 1900), trade(9, 6000), trade(9, 3140), *[trade(8, 0) for _ in range(8)]]
    result = {"summary": {"trades": len(trades), "netProfit": 12040}, "trades": trades}
    facts = verdict_facts(result)["facts"]
    assert facts[0]["id"] == "concentration"
    # 9 月 9,140 of 12,040 = 76%; without September 2,900 is left.
    assert facts[0]["text"] == "能赚钱，但赚的钱有 76% 来自 9 月那一段；去掉那个月只赚 2,900。"
    assert facts[0]["tone"] == "warning" and facts[0]["numbers"] == ["76", "9", "2,900"]
    tuned = verdict_facts(result, tuned=True)["facts"]
    assert tuned[-1] == {"id": "tuned", "text": "提醒：参数是在同一段行情里挑出来的，实盘可能没这么好。", "tone": "warning", "numbers": []}


def test_verdict_facts_for_spread_out_profit_losses_few_and_no_trades():
    even = [trade(month, 1000) for month in (6, 7, 8, 9) for _ in range(3)]
    spread = verdict_facts({"summary": {"trades": 12, "netProfit": 12000}, "trades": even})["facts"]
    assert spread[0]["id"] == "spread_out" and "只占 25%" in spread[0]["text"]
    losing = verdict_facts({"summary": {"trades": 3, "netProfit": -1500},
                            "trades": [trade(8, -2000), trade(9, 300), trade(9, 200)], "feeEstimate": {"source": "none"}})["facts"]
    assert [item["id"] for item in losing] == ["loss", "few_trades", "no_fees"]
    assert losing[0]["text"] == "这段时间按这个做是亏的，一共亏了 1,500，亏得最多的是 8 月。"
    assert verdict_facts({"summary": {"trades": 0, "netProfit": 0}, "trades": []})["facts"][0]["id"] == "no_trades"
    # A truncated trade list cannot be split by month honestly: only the totals are said.
    truncated = verdict_facts({"summary": {"trades": 20, "netProfit": 500}, "trades": [trade(9, 500)], "tradesTruncated": True})
    assert truncated["facts"][0] == {"id": "profit", "text": "能赚钱：一共赚了 500。", "tone": "info", "numbers": ["500"]}


def test_a_model_sentence_may_only_use_the_facts_numbers():
    verdict = {"facts": [{"id": "concentration", "text": "…", "numbers": ["76", "9", "2,900"]}]}
    assert check_sentence("能赚，但七成多靠 9 月；去掉 9 月只剩 2900。", verdict) is None
    assert check_sentence("能赚，去掉 9 月只剩 5,900。", verdict) == "number_not_in_facts:5,900"
    assert check_sentence("能赚，去掉九月只剩五千九。", verdict) == "chinese_numeral"
    assert check_sentence("ATR 止损偏紧。", verdict) == "jargon"
    assert check_sentence("一。二。三。", verdict) == "too_many_sentences"


async def test_model_verdict_falls_back_to_the_facts():
    facts = verdict_facts({"summary": {"trades": 12, "netProfit": 12040},
                           "trades": [trade(9, 9140), trade(8, 2900), *[trade(8, 0) for _ in range(10)]]})

    class Model:
        agent_authenticated = True

        def __init__(self, text=None, error=None):
            self.text, self.error = text, error

        async def complete_structured(self, **_: Any):
            if self.error:
                raise self.error
            return type("R", (), {"value": type("V", (), {"text": self.text})()})()

    good = await model_verdict(Model("能赚钱，但 76% 靠 9 月那一段，去掉它只赚 2,900。"), facts)
    assert good == {"text": "能赚钱，但 76% 靠 9 月那一段，去掉它只赚 2,900。", "source": "model"}
    invented = await model_verdict(Model("能赚钱，一共赚了 13,000。"), facts)
    assert invented["source"] == "facts" and invented["text"].startswith("能赚钱，但赚的钱有 76%")
    assert (await model_verdict(Model(error=TimeoutError()), facts))["source"] == "facts"
    assert (await model_verdict(None, facts))["source"] == "facts"


# -- 规则四行 ---------------------------------------------------------------------------------------------

def test_plain_rows_move_the_professional_names_behind_the_question_mark():
    plan, _ = build_plan(templates()["dual_thrust"])
    rows = {row["key"]: row for row in plain_rows(plan)}
    assert [row["label"] for row in plain_rows(plan)] == ["开仓", "平仓", "止损", "风险"]
    entry = "".join(part["text"] for part in rows["entry"]["parts"])
    assert "2 成前 5 日振幅" in entry and "K1" not in entry and "0.2" not in entry
    stop = rows["stop"]["parts"]
    termed = next(part for part in stop if part.get("term"))
    assert termed["text"] == "平均波动" and termed["term"] == "ATR14" and "ATR（平均真实波幅）" in termed["help"]
    assert all("ATR" not in part["text"] for part in stop)
    # 10-08 用户决定 A：模板不替用户定当日亏损上限；填了才在“风险”行出现。
    assert "当天亏满" not in "".join(part["text"] for part in rows["risk"]["parts"])
    capped, _ = build_plan(templates()["dual_thrust"], {"maxLoss": 5000})
    capped_rows = {row["key"]: row for row in plain_rows(capped)}
    assert "当天亏满 5000 元就停止开新仓" in "".join(part["text"] for part in capped_rows["risk"]["parts"])


# -- 今日判断联动 -----------------------------------------------------------------------------------------

def test_the_clash_appears_only_against_a_bullish_or_bearish_judgment():
    assert stance_clash("偏空", "long", holding=True) == {"stance": "偏空", "direction": "long",
                                                         "text": "今日判断偏空，这个策略今天在做多"}
    assert stance_clash("偏多", "short", holding=False)["text"] == "今日判断偏多，这个策略今天发出的是做空信号，方向相反"
    assert stance_clash("偏多", "long", holding=True) is None
    assert stance_clash("震荡", "long", holding=True) is None
    assert stance_clash("不明", "short", holding=True) is None
    assert stance_clash(None, "short", holding=True) is None
    assert stance_clash("偏空", None, holding=False) is None


# -- 试几组参数比一比 -------------------------------------------------------------------------------------

def test_compare_line_says_what_the_held_out_stretch_did():
    def lab(out: dict[str, Any] | None, status="done"):
        split = 1_000 * DAY
        return {"status": status, "request": {"currentVariant": "v2"}, "results": [
            {"variantId": "v2", "label": "2 成（当前）", "splitTime": split,
             "coverage": {"lastBarTime": split + 61 * DAY}, "outSample": out}]}
    assert compare_line(lab({"trades": 7, "net": 2100})) == "后 2 个月单独检验：2 成那组仍赚 2,100，比较稳。"
    assert compare_line(lab({"trades": 4, "net": -800})) == "后 2 个月单独检验：2 成那组亏了 800，前面赚的可能是凑出来的，实盘要当心。"
    assert compare_line(lab({"trades": 0, "net": 0})) == "后 2 个月单独检验：2 成那组没开出单，说明不了稳不稳。"
    assert compare_line(lab({"trades": 3, "net": 10}, status="running")) is None
    assert value_words("k1", 0.15) == "1.5 成" and value_words("fast", 10) == "10"


# -- routes: the main line, stored results, verdict and comparison -------------------------------------------

async def test_one_sentence_becomes_a_saved_strategy_whose_backtest_survives_a_restart(tmp_path, monkeypatch):
    from trader_runtime.testing.fake_research_live import FakeNewsModel

    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "redesign")
    app.state.fixed_strategy_model = FakeNewsModel()
    async with app.router.lifespan_context(app), client_for(app) as client:
        job = (await client.post("/api/strategy-plans/design-jobs", json={
            "id": JOB, "description": "螺纹突破前几天的高点就追进去，跌回来就走",
            "constraints": {"direction": "both", "maxPosition": 1, "maxLoss": 1000}})).json()
        for _ in range(200):
            job = (await client.get(f"/api/strategy-plans/design-jobs/{JOB}")).json()
            if job["status"] != "running":
                break
            await asyncio.sleep(0)
        assert job["status"] == "completed", job
        saved = (await client.post("/api/strategy-plans", json={"plan": job["result"],
                                                                 "instrumentScope": {"mode": "any", "symbols": []}})).json()
        ran = await client.post(f"/api/strategies/{saved['strategyId']}/backtest",
                                json={"symbol": "SHFE.au2612", "tradingDays": 3})
        assert ran.status_code == 200, ran.text
        result = ran.json()
        assert result["strategyId"] == saved["strategyId"]
        assert [item["id"] for item in result["verdict"]["facts"]] and result["verdict"]["text"] is None
        assert "definition" not in result
        verdict = (await client.post(f"/api/strategy-backtests/{result['backtestId']}/verdict")).json()
        assert verdict["source"] == "model" and verdict["text"]
        listed = (await client.get("/api/strategy-plans")).json()["items"]
        card = next(item for item in listed if item["strategyId"] == saved["strategyId"])
        assert card["latestBacktest"]["backtestId"] == result["backtestId"]
        assert card["latestBacktest"]["netProfit"] == result["summary"]["netProfit"]
        assert [row["label"] for row in card["planRows"]] == ["开仓", "平仓", "止损", "风险"]
    # A new Runtime on the same database: the result, with its verdict, is still there.
    again = app_for(tmp_path, "redesign")
    async with again.router.lifespan_context(again), client_for(again) as client:
        items = (await client.get("/api/strategy-backtests", params={"strategyId": saved["strategyId"]})).json()["items"]
        assert [item["backtestId"] for item in items] == [result["backtestId"]]
        assert items[0]["verdict"]["text"] == verdict["text"]
        assert (await client.post(f"/api/strategy-backtests/{result['backtestId']}/verdict")).json() == verdict


def test_stored_results_keep_the_newest_five_per_strategy_and_stay_per_user(tmp_path):
    store = RuntimeStore(tmp_path / "results.sqlite3")
    for index in range(7):
        store.save_backtest_result(user_id="a", result={"backtestId": f"r{index}", "strategyId": "s1",
                                                         "createdAt": f"2026-10-08T00:00:0{index}", "summary": {"netProfit": index},
                                                         "trades": [{"id": n} for n in range(400)]})
    store.save_backtest_result(user_id="a", result={"backtestId": "plain", "strategyId": None, "createdAt": "2026-10-08T00:01:00",
                                                     "summary": {"netProfit": 1}})
    kept = store.list_backtest_results(user_id="a", strategy_id="s1")
    assert [item["backtestId"] for item in kept] == ["r6", "r5", "r4", "r3", "r2"]
    assert len(kept[0]["trades"]) == 300 and kept[0]["tradesTruncated"] is True
    assert store.latest_backtest_summaries(user_id="a")["s1"]["netProfit"] == 6
    assert store.list_backtest_results(user_id="b") == [] and store.latest_backtest_summaries(user_id="b") == {}
    with pytest.raises(KeyError):
        store.get_backtest_result("r6", user_id="b")
    store.close()


async def test_compare_runs_three_groups_of_the_key_parameter_in_the_cloud(tmp_path, monkeypatch):
    from trader_runtime.routes import strategy_lab as lab_routes

    async def quick_run(application, store, provider, schedule, user, lab_id):
        """The cloud run itself is the lab's (tested in test_strategy_lab): here only its written shape."""
        lab = store.get_lab(lab_id, user_id=user)
        results = [{"variantId": item["id"], "label": item["label"], "summary": {"netProfit": 100 * index, "maxDrawdown": 50,
                    "winRate": 50.0, "trades": 10}, "inSample": {"trades": 6, "net": 80}, "outSample": {"trades": 4, "net": 20},
                    "splitTime": 1, "coverage": {"lastBarTime": 1 + 60 * DAY}} for index, item in enumerate(lab["variants"])]
        store.update_lab(lab_id, user_id=user, status="done", source="server", results=results, ranking={"tried": 3})

    monkeypatch.setattr(lab_routes, "run_lab_on_server", quick_run)
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "compare")
    async with app.router.lifespan_context(app), client_for(app) as client:
        saved = (await client.post("/api/strategy-templates/dual_thrust/instantiate", json={})).json()
        started = await client.post(f"/api/strategy-plans/{saved['strategyId']}/compare", json={"symbol": "SHFE.au2612"})
        assert started.status_code == 201, started.text
        view = started.json()
        assert view["paramLabel"] == "向上突破幅度" and [row["label"] for row in view["rows"]] == ["1.5 成", "2 成（当前）", "3 成"]
        assert [row["current"] for row in view["rows"]] == [False, True, False]
        lab = (await client.get(f"/api/strategy-labs/{view['labId']}")).json()
        # Dual Thrust's K2 has K1's value and range: it moves with it, so both sides are compared.
        assert [(item["params"]["k1"], item["params"]["k2"]) for item in lab["variants"]] == [(0.15, 0.15), (0.2, 0.2), (0.3, 0.3)]
        assert lab["request"]["tradingDays"] == 120 and lab["request"]["strategyId"] == saved["strategyId"]
        await lab_routes._RUNNING[view["labId"]]
        done = (await client.get(f"/api/strategy-plans/{saved['strategyId']}/compare")).json()
        assert done["status"] == "done" and [row["summary"]["netProfit"] for row in done["rows"]] == [0, 100, 200]
        assert done["line"] == "后 2 个月单独检验：2 成那组仍赚 20，比较稳。"
        # A plan that is not a template varies its stop distance.
        plan = (await client.get("/api/strategy-templates/double_ma")).json()["plan"]
        plain = (await client.post("/api/strategy-plans", json={"plan": {**plan, "name": "手写均线"},
                                                                 "instrumentScope": {"mode": "any", "symbols": []}})).json()
        other = (await client.post(f"/api/strategy-plans/{plain['strategyId']}/compare", json={"symbol": "SHFE.au2612"})).json()
        assert [row["label"] for row in other["rows"]] == ["2 倍", "3 倍（当前）", "4.5 倍"]
        assert (await client.get("/api/strategy-plans/nope/compare")).json()["status"] == "none"


# -- #530-C ---------------------------------------------------------------------------------------------------

async def test_a_browser_upload_never_replaces_the_cloud_run_and_finished_runs_are_forgotten(tmp_path, monkeypatch):
    from trader_runtime.routes import strategy_lab as lab_routes
    from trader_runtime.strategy_lab import engine_bundle

    gate = asyncio.Event()

    async def gated_run(application, store, provider, schedule, user, lab_id):
        await gate.wait()
        store.update_lab(lab_id, user_id=user, status="done", source="server", results=[], ranking={"tried": 2})

    monkeypatch.setattr(lab_routes, "run_lab_on_server", gated_run)
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-530c")
    async with app.router.lifespan_context(app), client_for(app) as client:
        lab = (await client.post("/api/strategy-labs", json={"symbol": "SHFE.au2612", "variants": [
            {"templateId": "double_ma"}, {"templateId": "double_ma", "params": {"slow": 30}}]})).json()
        upload = {"engineDigest": engine_bundle()["digest"], "results": [{"variantId": "v1", "error": "没有算完"}]}
        assert (await client.post(f"/api/strategy-labs/{lab['id']}/run-server")).status_code == 200
        running = await client.post(f"/api/strategy-labs/{lab['id']}/results", json=upload)
        assert running.status_code == 409 and "以云端为准" in running.json()["detail"]
        gate.set()
        task = lab_routes._RUNNING[lab["id"]]
        await task
        await asyncio.sleep(0)            # the done callback runs on the next loop turn
        assert lab["id"] not in lab_routes._RUNNING
        late = await client.post(f"/api/strategy-labs/{lab['id']}/results", json=upload)
        assert late.status_code == 409 and "云端已经算完" in late.json()["detail"]
        assert (await client.get(f"/api/strategy-labs/{lab['id']}")).json()["source"] == "server"


async def test_a_cancelled_cloud_run_keeps_its_slot_until_the_worker_finishes(monkeypatch):
    from trader_runtime import strategy_lab
    from trader_runtime.strategy_backtest import BacktestQueue

    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_worker(fn, *args):
        started.set()
        await release.wait()
        return {"variantId": "v1", "label": "v1", "error": "synthetic"}

    queue = BacktestQueue(global_limit=1, per_user_limit=1)
    application = type("App", (), {"state": type("State", (), {"strategy_backtest_queue": queue})()})()

    class Store:
        def get_lab(self, lab_id, *, user_id):
            return {"id": lab_id, "symbol": "SHFE.au2612", "request": {}, "variants": [{"id": "v1", "label": "v1"}]}

        def update_lab(self, *args, **kwargs):
            return {}

    monkeypatch.setattr(strategy_lab, "build_inputs", lambda *args: {})
    monkeypatch.setattr(strategy_lab, "run_callable_in_worker", slow_worker)
    run = asyncio.create_task(strategy_lab.run_lab_on_server(application, Store(), None, {}, "u", "lab"))
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    # The coroutine is gone but the worker's job is not: the slot stays taken.
    assert queue.limits()["running"] == 1
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert queue.limits()["running"] == 0


# -- 停止并平仓 ---------------------------------------------------------------------------------------------

class CardStub:
    """The proposal service as far as 停止并平仓 uses it: it only ever makes cards."""

    def __init__(self) -> None:
        self.cards: list[dict[str, Any]] = []

    def create_trade(self, session_id, turn_id, **fields):
        source = fields.pop("strategy_source", None)   # the real card carries it as strategySource
        card = {"proposalId": f"card-{len(self.cards) + 1}", "sessionId": session_id, **fields}
        if source is not None:
            card["strategySource"] = source
        self.cards.append(card)
        return card


def _strategy(store: RuntimeStore, *, signal_action: str = "auto") -> dict[str, Any]:
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    saved = store.save_strategy_draft(strategy_id="run-1", user_id=USER, source_session_id="s", name="开盘突破追单 · SHFE.rb2701",
                                      symbol="SHFE.rb2701", version=1, status="ready",
                                      definition={"timeframeSeconds": 60, "riskLimits": {}}, definition_digest="d" * 64)
    now = datetime.now(UTC).isoformat()
    store.connection.execute("PRAGMA foreign_keys = OFF")   # the authorization / session rows are not the point here
    store.connection.execute(
        "INSERT INTO ai_stop_orders (id,user_id,authorization_id,session_id,symbol,position_direction,position_bucket,volume,"
        "trigger_price,price_intent,status,strategy_metadata_json,remaining_volume,created_at,updated_at)"
        " VALUES ('stop-1',?,'auth','s','SHFE.rb2701','long','today',3,'3000','passive','ARMED',?,3,?,?)",
        (USER, '{"strategyId":"run-1"}', now, now))
    store.connection.commit()
    return {**saved, "signalAction": signal_action}


async def test_stop_and_close_makes_close_cards_and_never_sends_an_order(tmp_path: Path):
    store = RuntimeStore(tmp_path / "stop-close.sqlite3")
    data = DataStub()
    # The account holds 2 lots today and 1 from yesterday; the strategy's protection covers all 3.
    data.position_rows = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 3, "todayVolume": 2, "historicalVolume": 1}]
    autonomy = AutonomyStub(store, data)
    cards = CardStub()
    monitor = StrategyMonitorService(store=store, proposals=cards, autonomy=autonomy)  # type: ignore[arg-type]
    strategy = _strategy(store, signal_action="auto")
    assert monitor.held_position(strategy) == {"direction": "long", "volume": 3, "legs": [("SHFE.rb2701", "long", 3)]}
    outcome = await monitor.propose_close_after_stop(strategy, session_id="chat-1")
    # SHFE: 平今 and 平昨 are separate cards; both are cards to confirm, nothing was executed.
    assert [(card["side"], card["volume"], card["position_bucket"]) for card in outcome["cards"]] == [
        ("sell_close", 2, "today"), ("sell_close", 1, "historical")]
    assert all(card["sessionId"] == "chat-1" and card["price_intent"] == "aggressive" for card in outcome["cards"])
    # 归属 (#654): every card carries its strategy's stop_close attempt, registered before the
    # card exists — the confirmed close then spends only this strategy's protection rows.
    attempts = store.list_strategy_execution_attempts(strategy_id="run-1", user_id=USER)
    assert sorted((item["action"], item["volume"], item["status"]) for item in attempts) == [
        ("stop_close", 1, "SKIPPED"), ("stop_close", 2, "SKIPPED")]
    by_attempt = {item["attemptId"]: item for item in attempts}
    for card in outcome["cards"]:
        source = card["strategySource"]
        assert source["strategyId"] == "run-1" and source["purpose"] == "stop_close"
        assert by_attempt[source["attemptId"]]["cardProposalIds"] == [card["proposalId"]]
    assert autonomy.calls == [] and outcome["detail"] == "已出平仓卡，在对话里确认后才平仓。"
    # A position the user opened by hand beside it is not the strategy's: only the protected lots count.
    data.position_rows = [{"symbol": "SHFE.rb2701", "direction": "long", "volume": 5, "todayVolume": 5, "historicalVolume": 0}]
    assert monitor.held_position(strategy)["volume"] == 3
    data.position_rows = []
    assert (await monitor.propose_close_after_stop(strategy, session_id="chat-1")) == {
        "cards": [], "detail": "这个策略现在没有持仓，不用平仓。"}
    store.close()


async def test_the_monitor_payload_reports_position_signals_and_the_clash(tmp_path, monkeypatch):
    app = app_for(tmp_path, "clash")
    store = app.state.store
    async with app.router.lifespan_context(app), client_for(app) as client:
        saved = (await client.post("/api/strategy-templates/double_ma/instantiate", json={})).json()
        store.connection.execute("UPDATE strategy_drafts SET status = 'monitoring', symbol = 'SHFE.au2612' WHERE id = ?",
                                 (saved["strategyId"],))
        today = app.state.data_provider.account_summary("test")["tradingDay"]
        now = datetime.now(UTC).isoformat()
        store.connection.commit()
        store.connection.execute("PRAGMA foreign_keys = OFF")   # the intent snapshot rows are not the point here
        for index, action in enumerate(("open_long", "close_long", "open_long")):
            store.connection.execute(
                """INSERT INTO strategy_execution_attempts (id, strategy_id, user_id, intent_id, signal_bar_time, trading_day,
                action, volume, status, signal_action, created_at, updated_at) VALUES (?, ?, ?, 'i', ?, ?, ?, 1, 'SKIPPED', 'alert', ?, ?)""",
                (f"a{index}", saved["strategyId"], "local-user", f"bar-{index}", today, action, now, now))
        store.connection.commit()
        from trader_runtime.research_desk import ResearchDeskService
        monkeypatch.setattr(ResearchDeskService, "view_at", lambda self, symbol, moment: {"stance": "偏空"})
        payload = (await client.get(f"/api/strategies/{saved['strategyId']}/monitor")).json()
        assert payload["signalsToday"] == 3 and payload["position"] == {"direction": None, "volume": 0}
        assert payload["researchClash"] == {"stance": "偏空", "direction": "long",
                                            "text": "今日判断偏空，这个策略今天发出的是做多信号，方向相反"}
        monkeypatch.setattr(ResearchDeskService, "view_at", lambda self, symbol, moment: {"stance": "震荡"})
        assert (await client.get(f"/api/strategies/{saved['strategyId']}/monitor")).json()["researchClash"] is None
        # 停止并平仓 through the route: stopped either way; without a position no card is made.
        stopped = (await client.post(f"/api/strategies/{saved['strategyId']}/monitor/stop", json={"closePositions": True})).json()
        assert stopped["status"] == "ready" and stopped["closeCards"] == [] and stopped["positionsKept"] is True
        assert stopped["notice"] == "已停止：不再判断信号。这个策略现在没有持仓，不用平仓。"


def test_compare_without_a_fixed_stop_varies_the_entry_lookback():
    """保护：不设固定止损（5000 跳兜底）的策略，比一比改比开仓回看根数，不再要 6250 跳而 422（10-08 线上）。"""
    from trader_runtime.strategy_lab import compare_variants
    plan = build_plan(templates()["donchian_turtle"])[0].model_dump()
    plan.update(stopLossTicks=5000, atrStop=None, stopLookbackBars=None, pullback=None, trailingStop=None)
    variants, described = compare_variants({"definition": {"plan": plan}}, timeframe=None)
    assert described["paramKey"] == "entryLookback"
    labels = [variant["label"] for variant in variants]
    assert len(labels) == 3 and sum(label.endswith("（当前）") for label in labels) == 1
    periods = sorted(int(variant["value"]) for variant in variants)
    assert periods[0] < periods[1] < periods[2] <= 200
    # A plan with an ordinary stop still compares its stop distance, inside the 1–5000 range.
    plan.update(stopLossTicks=4800)
    variants, described = compare_variants({"definition": {"plan": plan}}, timeframe=None)
    assert described["paramKey"] == "stopLossTicks" and max(variant["value"] for variant in variants) <= 5000
