"""策略实验室 (2026-09-30): the shared engine's split / rank, the routes, and the browser
bundle's self-containment.

The split and ranking are unit-tested on constructed trades (分段、回撤、too_few、overfit both
ways、negative_oos、decay None 两种情况); the routes cover create / list / inputs (gzip) /
results (digest 409, 别人的 404, 服务端重算排名), the cloud fallback (run-server 到 done、运行中
409) and choose (存为我的策略); the bundle test writes GET /api/strategy-engine/bundle's modules
to a clean directory and runs run_variant in a fresh interpreter — the summary must equal the
run-server result's, proving the bundle carries everything the browser needs.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest

from test_strategy_backtest import DOUBLE_MA, DOUBLE_MA_CLOSES
from test_strategy_templates import app_for, continuous_fake_schedule
from trader_runtime.strategy_lab import engine_bundle
from trader_runtime.strategy_lab_engine import MIN_IS_TRADES, SPLIT, _segment, rank, run_variant, split_metrics

HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer tpl-token"}


# -- split_metrics / rank on constructed trades ------------------------------------------------------------

def seg(nets):
    """A segment as run_variant builds one, from a list of trade nets."""
    return _segment([{"net": net} for net in nets])


def test_segment_counts_winrate_net_and_its_own_drawdown():
    segment = seg([10, -30, 20])
    assert segment["trades"] == 3 and segment["wins"] == 2 and segment["winRate"] == 66.7
    assert segment["net"] == 0.0 and segment["avgNet"] == 0.0
    assert segment["maxDrawdown"] == 30          # cumulative 10 → −20: 30 below the peak 10
    assert seg([]) == {"trades": 0, "wins": 0, "winRate": None, "net": 0, "avgNet": None,
                       "maxDrawdown": 0}


def test_split_metrics_splits_at_the_70_percent_mark_by_close_and_open():
    """L-A：样本内只收切点前已平仓的交易，样本外只收切点后开的交易；跨切点的那笔两段
    都不计（它的样本外价格不能移动样本内的数字）。"""
    coverage = {"firstBarTime": 0, "lastBarTime": 1000}
    trades = [{"openTime": 100, "closeTime": 200, "net": 5},   # 切点前开、切点前平
              {"openTime": 600, "closeTime": 699, "net": 6},   # 切点前开、切点前平
              {"openTime": 650, "closeTime": 800, "net": 7},   # 跨切点：两段都不计
              {"openTime": 700, "closeTime": 900, "net": 8},   # 切点上开仓：样本外
              {"openTime": 900, "closeTime": 950, "net": 9}]
    split_time, in_sample, out_sample, straddling = split_metrics(coverage, trades)
    assert split_time == int(SPLIT * 1000)       # exactly at the 70 % mark
    assert in_sample["trades"] == 2 and in_sample["net"] == 11
    assert out_sample["trades"] == 2 and out_sample["net"] == 17
    assert straddling == 1


def test_in_sample_metrics_ignore_a_trade_straddling_the_split(monkeypatch):
    """L-A：只改样本外价格（跨切点那笔在样本外段里的盈亏）不能改变样本内指标，也不能
    改变“最佳变体”——样本外行情不再通过跨切点的持仓漏进挑选。"""
    from trader_runtime import strategy_lab_engine

    holder = {}
    monkeypatch.setattr(strategy_lab_engine, "run_backtest_with_trades",
                        lambda setup: (holder["result"], holder["result"]["trades"]))
    inputs = {"symbol": "SHFE.au2612", "priceTick": 0.02, "volumeMultiple": 1000,
              "feeSchedule": {}, "initialBalance": 100000, "slippageTicks": 0, "series": {"60": []}}

    def compare(straddling_net):
        results = []
        for vid, nets in (("a", [10] * 6), ("b", [9] * 5 + [10])):
            trades = [{"openTime": 100 * i, "closeTime": 100 * i + 40, "net": net} for i, net in enumerate(nets, 1)]
            trades += [{"openTime": 650, "closeTime": 800, "net": straddling_net},   # 跨切点的那笔
                       {"openTime": 720, "closeTime": 740, "net": 4}]                 # 样本外
            holder["result"] = {"summary": {"trades": len(trades)},
                                "coverage": {"firstBarTime": 0, "lastBarTime": 1000}, "trades": trades}
            results.append(run_variant(inputs, {"id": vid, "label": vid, "plan": DOUBLE_MA.model_dump()}))
        return results

    steady, crashed = compare(6), compare(-600)
    assert [item["inSample"] for item in steady] == [item["inSample"] for item in crashed]
    assert [item["outSample"] for item in steady] == [item["outSample"] for item in crashed]
    assert steady[0]["straddling"] == 1
    assert rank(steady)["best"] == "a" and rank(crashed)["best"] == "a"


def variant(vid, is_nets, oos_nets, error=None):
    entry = {"variantId": vid, "inSample": seg(is_nets), "outSample": seg(oos_nets)}
    if error is not None:
        entry["error"] = error
    return entry


def test_rank_marks_best_decay_overfit_and_negative_oos():
    qualified = [variant("a", [10] * 6, [-2] * 6),          # best in-sample, loses out of sample
                 variant("b", [8] * 6, [-1] * 6),
                 variant("c", [6] * 6, [4] * 6),
                 variant("d", [4] * 6, [5] * 6),
                 variant("e", [2] * 3, [9] * 6)]            # too few in-sample trades
    table = rank(qualified)
    assert table["tried"] == 5 and table["best"] == "a"
    items = {item["variantId"]: item for item in table["items"]}
    assert [items[key]["isRank"] for key in "abcd"] == [1, 2, 3, 4]
    assert items["a"]["oosRank"] == 4                      # last of the qualified out of sample
    assert items["a"]["flags"] == ["best", "overfit", "negative_oos"]   # oos net ≤ 0 and rank past 3
    assert items["b"]["flags"] == ["negative_oos"]
    assert items["c"]["flags"] == [] and items["d"]["flags"] == []
    assert items["e"]["flags"] == ["too_few"] and items["e"]["isRank"] is None
    assert items["a"]["decay"] == round((-2) / 10, 2)
    assert items["b"]["decay"] == round((-1) / 8, 2)
    assert items["d"]["decay"] == round(5 / 4, 2)


def test_rank_flags_overfit_when_the_best_loses_its_out_of_sample_rank_only():
    # The best keeps a positive out-of-sample net but drops to 4th of the qualified.
    table = rank([variant("a", [10] * 6, [1] * 6), variant("b", [9] * 6, [2] * 6),
                  variant("c", [8] * 6, [3] * 6), variant("d", [7] * 6, [4] * 6)])
    best = table["items"][0]
    assert table["best"] == "a" and best["flags"] == ["best", "overfit"] and best["oosRank"] == 4
    # The same best within the first three out of sample is not overfit.
    table = rank([variant("a", [10] * 6, [2] * 6), variant("b", [9] * 6, [3] * 6),
                  variant("c", [8] * 6, [4] * 6), variant("d", [7] * 6, [1] * 6)])
    assert table["items"][0]["flags"] == ["best"]


def test_rank_decay_is_none_without_out_of_sample_trades_or_a_poor_in_sample():
    no_oos = rank([variant("a", [10] * 6, []), variant("b", [6] * MIN_IS_TRADES, [])])
    assert no_oos["best"] == "a"
    assert all(item["decay"] is None for item in no_oos["items"])
    losing = rank([variant("a", [-5] * 6, [10, 10]), variant("b", [-9] * 6, [10, 10])])
    # Every qualified variant loses in-sample: nothing is recommended (09-30) — no best, no best/overfit
    # flags, noProfit true — though the ranks stay; decay stays None because the in-sample average is ≤ 0.
    assert losing["best"] is None and losing["noProfit"] is True
    assert [item["isRank"] for item in losing["items"]] == [1, 2]
    assert all("best" not in item["flags"] and "overfit" not in item["flags"] for item in losing["items"])
    assert all(item["decay"] is None for item in losing["items"])
    assert no_oos["noProfit"] is False and rank([])["noProfit"] is False


def test_rank_with_no_qualified_variant_has_no_best_and_errored_items_rank_none():
    table = rank([variant("a", [1, 2], []), {"variantId": "x", "label": "x", "error": "历史K线不足"}])
    assert table["best"] is None and table["tried"] == 2
    items = {item["variantId"]: item for item in table["items"]}
    assert items["a"]["flags"] == ["too_few"]
    assert items["x"]["isRank"] is None and items["x"]["flags"] == []


def test_rank_counts_tried_from_the_caller():
    # tried is how many variants the comparison asked for, not how many results arrived.
    table = rank([variant("a", [10] * 6, [2] * 6), variant("b", [8] * 6, [1] * 6)], tried=3)
    assert table["tried"] == 3


def test_the_split_is_computed_before_the_display_cut_of_the_trade_list(monkeypatch):
    """#530：逐笔只给展示留最近 TRADES_KEPT 笔；样本内/外与 best 在完整序列上算，后段新增的
    交易不能把前段挤出样本内统计（以前先裁后分，OOS 行情会改变 IS 净利与排名）。"""
    from test_strategy_backtest import NO_FEES, gapless, series
    from trader_runtime import strategy_backtest

    bars = series(gapless(DOUBLE_MA_CLOSES * 6))
    inputs = {"symbol": "SHFE.rb2610", "priceTick": 1, "volumeMultiple": 10, "feeSchedule": NO_FEES,
              "initialBalance": 100_000, "slippageTicks": 0, "series": {300: bars}}
    spec = {"id": "v1", "label": "x", "plan": DOUBLE_MA.model_dump()}
    full = run_variant(inputs, spec)
    assert full["tradesUsed"] > 6 and full["inSample"]["trades"] > 3
    monkeypatch.setattr(strategy_backtest, "TRADES_KEPT", 3)
    cut = run_variant(inputs, spec)
    for key in ("inSample", "outSample", "splitTime", "tradesUsed", "straddling"):
        assert cut[key] == full[key]
    assert "truncated" not in cut
    assert all("truncated" not in item["flags"] for item in rank([cut])["items"])


def test_the_fee_warning_reaches_the_lab_results_and_the_ai_table(monkeypatch):
    from trader_runtime import strategy_lab_engine
    from trader_runtime.trader_tools import _lab_table

    warning = {"source": "none", "warning": True, "text": "未扣手续费，结果偏乐观：拿不到这个合约的手续费参考。"}
    monkeypatch.setattr(strategy_lab_engine, "run_backtest_with_trades",
                        lambda setup: ({"summary": {"trades": 1}, "coverage": {"firstBarTime": 0, "lastBarTime": 100},
                                        "feeEstimate": warning}, [{"openTime": 10, "closeTime": 20, "net": 1}]))
    result = run_variant({"symbol": "SHFE.au2612", "priceTick": 0.02, "volumeMultiple": 1000,
                          "feeSchedule": {}, "initialBalance": 100000, "slippageTicks": 0,
                          "series": {"60": []}}, {"id": "v1", "label": "x", "plan": DOUBLE_MA.model_dump()})
    assert result["feeEstimate"] == warning
    table = _lab_table({"id": "lab-1", "symbol": "SHFE.au2612", "status": "done", "source": "browser",
                        "results": [{"variantId": "v0", "error": "没有算完"}, result], "ranking": {}})
    assert table["feeEstimate"] == warning


# -- the routes --------------------------------------------------------------------------------------------

def lab_body(**extra):
    body = {"symbol": "SHFE.au2612", "tradingDays": 3, "variants": [
        {"templateId": "double_ma", "params": {"fast": 5, "slow": 20}},
        {"templateId": "double_ma", "params": {"fast": 10, "slow": 30}},
        {"plan": DOUBLE_MA.model_dump()},
    ]}
    body.update(extra)
    return body


async def test_create_list_and_refuse_bad_labs(tmp_path, monkeypatch):
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-create")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            created = await client.post("/api/strategy-labs", json=lab_body())
            assert created.status_code == 201, created.text
            lab = created.json()
            assert lab["status"] == "ready" and lab["source"] is None
            assert [item["id"] for item in lab["variants"]] == ["v1", "v2", "v3"]
            assert lab["variants"][2]["label"] == DOUBLE_MA.name
            assert all(item["plan"]["timeframeSeconds"] for item in lab["variants"])
            assert (await client.get(f"/api/strategy-labs/{lab['id']}")).json()["id"] == lab["id"]
            assert [item["id"] for item in (await client.get("/api/strategy-labs")).json()["items"]] == [lab["id"]]
            # 组名只列与模板默认值不同的参数（参数名取 label 里“（”之前的部分），最多 3 个。
            assert lab["variants"][0]["label"] == "双均线（基准） · 短均线 5"
            assert lab["variants"][1]["label"] == "双均线（基准） · 长均线 30"
            # 全是默认值的一组叫“默认参数”。
            assert (await client.post("/api/strategy-labs", json={"symbol": "SHFE.au2612", "variants": [
                {"templateId": "double_ma"}, {"templateId": "double_ma", "params": {"slow": 30}}]}
            )).json()["variants"][0]["label"] == "双均线（基准） · 默认参数"
            # 五个参数都改了也只列前三个。
            everything = {"fast": 5, "slow": 30, "stopAtr": 2, "maxPosition": 2, "maxLoss": 3000}
            assert (await client.post("/api/strategy-labs", json={"symbol": "SHFE.au2612", "variants": [
                {"templateId": "double_ma", "params": everything}, {"templateId": "double_ma", "params": {"slow": 30}}]}
            )).json()["variants"][0]["label"] == "双均线（基准） · 短均线 5 / 长均线 30 / 保护止损 2"
            # 超过 12 组、价差模板、坏参数（指明第几组）都被拒绝。
            too_many = lab_body(variants=[{"templateId": "double_ma"}] * 13)
            assert (await client.post("/api/strategy-labs", json=too_many)).status_code == 422
            spread = await client.post("/api/strategy-labs", json={"symbol": "SHFE.rb2701", "variants": [
                {"templateId": "spread_boll_reversion",
                 "params": {"near": "SHFE.rb2701", "far": "SHFE.rb2705"}}]})
            assert spread.status_code == 422 and "价差策略暂不支持实验室对比" in spread.json()["detail"]
            bad = lab_body(variants=[{"templateId": "double_ma"},
                                     {"templateId": "double_ma", "params": {"fast": 30, "slow": 20}}])
            refused = await client.post("/api/strategy-labs", json=bad)
            assert refused.status_code == 422 and refused.json()["detail"].startswith("第 2 组：")
            assert (await client.get("/api/strategy-labs/nope")).status_code == 404


async def test_skip_invalid_skips_unbuildable_groups_and_refuses_when_too_few_survive(tmp_path, monkeypatch):
    """模板页的参数网格带 skipInvalid：建不起来的组跳过并记进 lab.skipped；能比的不到 2 组
    （一次比较至少要两组）时仍整次拒绝，理由取第一个被跳过的原因。"""
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-skip-invalid")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            grid = {"symbol": "SHFE.au2612", "skipInvalid": True, "variants": [
                {"templateId": "double_ma", "params": {"fast": 10, "slow": 20}},
                {"templateId": "double_ma", "params": {"fast": 26, "slow": 20}},   # 快线 ≥ 慢线
                {"templateId": "double_ma", "params": {"fast": 5, "slow": 40}},
            ]}
            created = await client.post("/api/strategy-labs", json=grid)
            assert created.status_code == 201, created.text
            lab = created.json()
            assert [item["id"] for item in lab["variants"]] == ["v1", "v3"]
            assert lab["skipped"] == [{"index": 2, "reason": "快线周期须小于慢线周期"}]
            assert (await client.get(f"/api/strategy-labs/{lab['id']}")).json()["skipped"] == lab["skipped"]
            # 能比的组合不到 2 组：报第一个被跳过的原因。
            few = await client.post("/api/strategy-labs", json={"symbol": "SHFE.au2612", "skipInvalid": True, "variants": [
                {"templateId": "double_ma"},
                {"templateId": "double_ma", "params": {"fast": 30, "slow": 20}},
                {"templateId": "does_not_exist"}]})
            assert few.status_code == 422
            assert few.json()["detail"] == "能比的组合不到 2 组：快线周期须小于慢线周期"


async def test_inputs_carry_the_history_and_the_engine_digest_gzipped(tmp_path, monkeypatch):
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-inputs")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            lab = (await client.post("/api/strategy-labs", json=lab_body())).json()
            plain = await client.get(f"/api/strategy-labs/{lab['id']}/inputs",
                                     headers={"Accept-Encoding": "identity"})
            assert plain.status_code == 200 and "Content-Encoding" not in plain.headers
            payload = plain.json()
            assert payload["symbol"] == "SHFE.au2612" and payload["feeSchedule"]["source"] in {"account", "none"}
            assert payload["initialBalance"] > 0 and payload["slippageTicks"] == 1
            assert payload["series"] and all(isinstance(bars, list) and len(bars) >= 2
                                             for bars in payload["series"].values())
            assert payload["engineDigest"] == engine_bundle()["digest"]
            assert [item["id"] for item in payload["variants"]] == ["v1", "v2", "v3"]
            zipped = await client.get(f"/api/strategy-labs/{lab['id']}/inputs",
                                      headers={"Accept-Encoding": "gzip"})
            assert zipped.headers.get("Content-Encoding") == "gzip"
            assert int(zipped.headers["content-length"]) < int(plain.headers["content-length"])
            assert zipped.content == plain.content          # httpx hands back the decompressed body


async def test_results_are_reranked_server_side_and_digest_must_match(tmp_path, monkeypatch):
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-results")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            lab = (await client.post("/api/strategy-labs", json=lab_body())).json()
            stale = await client.post(f"/api/strategy-labs/{lab['id']}/results",
                                      json={"engineDigest": "0000000000000000",
                                            "results": [{"variantId": "v1", "error": "x"}]})
            assert stale.status_code == 409 and stale.json()["detail"] == "回测引擎已更新，请刷新页面后重跑"
            foreign = await client.post("/api/strategy-labs/nope/results",
                                        json={"engineDigest": engine_bundle()["digest"], "results": []})
            assert foreign.status_code == 404 and foreign.json()["detail"] == "没有这次比较"
            unknown = await client.post(f"/api/strategy-labs/{lab['id']}/results",
                                        json={"engineDigest": engine_bundle()["digest"],
                                              "results": [{"variantId": "v9", "error": "x"}]})
            assert unknown.status_code == 422
            # A result without an error must carry both segments; the same variant twice is refused.
            bad = await client.post(f"/api/strategy-labs/{lab['id']}/results",
                                    json={"engineDigest": engine_bundle()["digest"],
                                          "results": [{"variantId": "v1", "inSample": seg([1] * 6)}]})
            assert bad.status_code == 422
            duplicate = await client.post(f"/api/strategy-labs/{lab['id']}/results",
                                          json={"engineDigest": engine_bundle()["digest"],
                                                "results": [{"variantId": "v1", "error": "x"},
                                                            {"variantId": "v1", "error": "y"}]})
            assert duplicate.status_code == 422
            # The client's own flags are ignored: the server rebuilds them from the numbers.
            forged = [
                {"variantId": "v1", "summary": {"trades": 6}, "coverage": {}, "splitTime": 0,
                 "inSample": seg([10] * 6), "outSample": seg([-2] * 6), "flags": ["best"], "decay": 9.99,
                 "feeEstimate": {"source": "none", "warning": True, "text": "未扣手续费，结果偏乐观"}},
                {"variantId": "v2", "summary": {"trades": 6}, "coverage": {}, "splitTime": 0,
                 "inSample": seg([8] * 6), "outSample": seg([3] * 6), "flags": []},
            ]
            accepted = await client.post(f"/api/strategy-labs/{lab['id']}/results",
                                         json={"engineDigest": engine_bundle()["digest"], "results": forged})
            assert accepted.status_code == 200, accepted.text
            stored = accepted.json()
            assert stored["status"] == "done" and stored["source"] == "browser"
            assert "flags" not in stored["results"][0]    # the client's own flags are dropped…
            assert stored["results"][0]["feeEstimate"]["warning"] is True   # the fee warning is kept
            ranking = {item["variantId"]: item for item in stored["ranking"]["items"]}
            assert ranking["v1"]["flags"] == ["best", "overfit", "negative_oos"]   # …and ranked here
            assert ranking["v1"]["decay"] == round(-2 / 10, 2) and ranking["v2"]["decay"] == round(3 / 8, 2)
            assert stored["ranking"]["best"] == "v1"
            # tried is the lab's variant count; the variant that arrived without a result is
            # completed as 没有算完 instead of shrinking the comparison.
            assert stored["ranking"]["tried"] == 3
            missing = next(item for item in stored["results"] if item["variantId"] == "v3")
            assert missing["error"] == "没有算完" and ranking["v3"]["flags"] == []


async def test_run_server_computes_in_the_shared_worker_pool(tmp_path, monkeypatch):
    """The cloud fallback must not hold the Runtime's GIL: every variant goes through the backtest
    worker pool (the same spawned processes rule backtests use), not a plain thread."""
    from concurrent.futures import ThreadPoolExecutor

    from trader_runtime import strategy_backtest

    pool = ThreadPoolExecutor(max_workers=1)
    calls = []

    def counting_pool():
        calls.append(True)
        return pool

    monkeypatch.setattr(strategy_backtest, "_worker_pool", counting_pool)
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-pool")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            lab = (await client.post("/api/strategy-labs", json=lab_body())).json()
            assert (await client.post(f"/api/strategy-labs/{lab['id']}/run-server")).status_code == 200

            async def finished():
                for _ in range(600):
                    current = (await client.get(f"/api/strategy-labs/{lab['id']}")).json()
                    if current["status"] in {"done", "error"}:
                        return current
                    await asyncio.sleep(0.05)
                return current

            done = await asyncio.wait_for(finished(), timeout=60)
            assert done["status"] == "done" and done["source"] == "server", done
            assert calls                                        # every variant through the pool
    pool.shutdown()


async def test_run_server_completes_matches_the_bundle_and_choose_saves(tmp_path, monkeypatch):
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-run")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            # Two full server runs below (the initial one plus the stale-row rerun) plus the
            # standalone bundle interpreter: one trading day still gives double_ma enough
            # trades to split and rank while cutting the real bar-by-bar compute to size.
            lab = (await client.post("/api/strategy-labs", json=lab_body(tradingDays=1))).json()
            inputs = (await client.get(f"/api/strategy-labs/{lab['id']}/inputs")).json()

            # The bundle runs standalone: its modules, a clean interpreter, the same inputs —
            # and the summary equals what run-server computes for the same variant.
            bundle = (await client.get("/api/strategy-engine/bundle")).json()
            assert bundle["digest"] == engine_bundle()["digest"] and len(bundle["digest"]) == 16
            assert bundle["entry"] == "trader_runtime.strategy_lab_engine"
            assert bundle["pyodide"] == {"version": "314.0.7", "packages": ["pydantic", "tzdata"]}
            assert set(bundle["modules"]) >= {"trader_runtime/strategy_lab_engine.py",
                                              "trader_runtime/strategy_backtest.py",
                                              "trader_runtime/__init__.py"}
            root = tmp_path / "bundle"
            for name, source in bundle["modules"].items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")
            variant_file = tmp_path / "variant.json"
            variant_file.write_text(json.dumps(inputs["variants"][0]), encoding="utf-8")
            inputs_file = tmp_path / "inputs.json"
            inputs_file.write_text(json.dumps(inputs), encoding="utf-8")
            script = (
                "import json, sys; sys.path.insert(0, sys.argv[1]);\n"
                "from trader_runtime.strategy_lab_engine import run_variant\n"
                "inputs = json.load(open(sys.argv[2])); variant = json.load(open(sys.argv[3]))\n"
                "print(json.dumps(run_variant(inputs, variant)))\n"
            )
            done = subprocess.run([sys.executable, "-c", script, str(root), str(inputs_file), str(variant_file)],
                                  capture_output=True, text=True, check=True, cwd=tmp_path)
            bundled = json.loads(done.stdout)
            assert "error" not in bundled and bundled["variantId"] == "v1"

            started = await client.post(f"/api/strategy-labs/{lab['id']}/run-server")
            assert started.status_code == 200 and started.json()["status"] == "running"

            async def finished():
                for _ in range(600):
                    current = (await client.get(f"/api/strategy-labs/{lab['id']}")).json()
                    if current["status"] in {"done", "error"}:
                        return current
                    await asyncio.sleep(0.05)
                return current

            lab = await asyncio.wait_for(finished(), timeout=60)
            assert lab["status"] == "done" and lab["source"] == "server", lab
            results = {item["variantId"]: item for item in lab["results"]}
            assert set(results) == {"v1", "v2", "v3"}
            for item in lab["results"]:
                assert "inSample" in item and "outSample" in item and "splitTime" in item
                # 手机版回测结果页 (2026-10-06): each variant's equity curve, thinned for the phone.
                assert 0 < len(item["equityCurve"]) <= 60 and {"time", "equity"} <= set(item["equityCurve"][0])
            assert bundled["equityCurve"] == results["v1"]["equityCurve"]
            # 策略 → 最近的回测 reads its rows from the list: name, period, last change, best out-of-sample net.
            summary = (await client.get("/api/strategy-labs")).json()["items"][0]
            best = results.get(lab["ranking"]["best"] or "")
            assert summary["name"] == lab["variants"][0]["label"] and summary["updatedAt"] == lab["updatedAt"]
            assert summary["bestOutNet"] == (best["outSample"]["net"] if best else None)
            assert summary["noProfit"] == bool(lab["ranking"].get("noProfit"))
            server_summary = results["v1"]["summary"]
            assert bundled["summary"] == server_summary                     # bundle self-contained
            assert lab["ranking"]["tried"] == 3 and lab["ranking"]["best"] in {None, "v1", "v2", "v3"}

            # A lab already running refuses a second cloud run — covered with the restart
            # leftover case in test_run_server_claims_the_cloud_run_once_and_a_stale_row_may_rerun.

            chosen = await client.post(f"/api/strategy-labs/{lab['id']}/choose",
                                       json={"variantId": "v1", "name": "实验室选中的双均线"})
            assert chosen.status_code == 200, chosen.text
            picked = chosen.json()
            assert picked["strategyId"] and picked["backtest"]["trades"] == server_summary["trades"]
            strategies = (await client.get("/api/strategies")).json()["items"]
            assert picked["strategyId"] in {item["strategyId"] for item in strategies}
            stored = (await client.get(f"/api/strategy-labs/{lab['id']}")).json()
            assert stored["chosen"]["variantId"] == "v1" and stored["chosen"]["strategyId"] == picked["strategyId"]
            assert stored["chosen"]["backtestSummary"] == picked["backtest"]
            # Choosing a plan variant (no templateId) saves through the plain plan path.
            plan_choice = await client.post(f"/api/strategy-labs/{lab['id']}/choose", json={"variantId": "v3"})
            assert plan_choice.status_code == 200 and plan_choice.json()["strategyId"]
            missing = await client.post(f"/api/strategy-labs/{lab['id']}/choose", json={"variantId": "v9"})
            assert missing.status_code == 404

            # L-C2: a “running” row this process has no live task for is a restart leftover —
            # it reruns on the server instead of staying 409 forever.
            app.state.store.update_lab(lab["id"], user_id="local-user", status="running")
            rerun = await client.post(f"/api/strategy-labs/{lab['id']}/run-server")
            assert rerun.status_code == 200
            again = await asyncio.wait_for(finished(), timeout=60)
            assert again["status"] == "done" and again["source"] == "server"


def test_the_bundle_runs_an_equity_percent_plan_without_the_server(tmp_path):
    """#510 / #817：按资金比例定手数的计划在 Pyodide 里不能因缺 strategy_funds 模块报 ModuleNotFoundError
    再静默回服务端——只用包里的模块，独立解释器里的结果等于本机进程的结果。"""
    from test_strategy_backtest import DOUBLE_MA_CLOSES, FEES, gapless, series
    from trader_runtime.strategy_backtest import BacktestSetup, setup_inputs
    from trader_runtime.strategy_lab_engine import run_rule
    from trader_runtime.strategy_rules import StrategyPlan

    sized = StrategyPlan.model_validate({**DOUBLE_MA.model_dump(),
                                         "sizing": {"mode": "equity_percent", "equityPercent": 50}})
    setup = BacktestSetup(plan=sized, symbol="SHFE.rb2610", series={300: series(gapless(DOUBLE_MA_CLOSES))},
                          price_tick=1, volume_multiple=10, fee_schedule=FEES, initial_balance=100_000,
                          margin_per_lot=2_000)
    inputs = json.loads(json.dumps(setup_inputs(setup)))
    expected = run_rule(inputs)["summary"]
    assert expected["trades"] > 0

    root = tmp_path / "bundle"
    for name, source in engine_bundle()["modules"].items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(json.dumps(inputs), encoding="utf-8")
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1])\n"
        "from trader_runtime.strategy_lab_engine import run_rule\n"
        "print(json.dumps(run_rule(json.load(open(sys.argv[2])))['summary']))\n"
    )
    done = subprocess.run([sys.executable, "-c", script, str(root), str(inputs_file)],
                          capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == expected


async def test_run_server_claims_the_cloud_run_once_and_a_stale_row_may_rerun(tmp_path, monkeypatch):
    """L-C1/L-C2：同一比较并发双启动只跑一个（占坑在第一个 await 之前，浏览器双击、两个标签页
    都算）；本进程没有对应任务的“运行中”是重启残留，允许重跑，不再永久 409。"""
    from trader_runtime.routes import strategy_lab as lab_routes

    gate = asyncio.Event()
    starts = []

    async def gated_run(*args, **kwargs):
        starts.append(True)
        await gate.wait()

    monkeypatch.setattr(lab_routes, "run_lab_on_server", gated_run)
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-claim")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            lab = (await client.post("/api/strategy-labs", json=lab_body())).json()
            first, second = await asyncio.gather(
                client.post(f"/api/strategy-labs/{lab['id']}/run-server"),
                client.post(f"/api/strategy-labs/{lab['id']}/run-server"))
            assert sorted([first.status_code, second.status_code]) == [200, 409]   # 只有一个人占到坑
            assert starts == [True]
            # 占坑的任务还在跑（行状态也是 running）：再来一次仍拒绝。
            assert (await client.post(f"/api/strategy-labs/{lab['id']}/run-server")).status_code == 409
            gate.set()
            await asyncio.sleep(0.05)          # gated_run 收尾，不写库：行状态留在 running
            # 此时这行“running”没有任何活任务（重启/部署后就是这种形状）：允许重跑。
            rerun = await client.post(f"/api/strategy-labs/{lab['id']}/run-server")
            assert rerun.status_code == 200
            for _ in range(100):                  # 让新任务真正开跑
                if len(starts) == 2:
                    break
                await asyncio.sleep(0.01)
            assert starts == [True, True]


async def test_run_server_marks_the_lab_error_when_reading_history_raises_oserror(tmp_path, monkeypatch):
    """L-C2：读历史抛 OSError（磁盘、临时文件、部署中的文件系统）也把这次比较标成 error，
    不再永远停在 running。"""
    from trader_runtime import strategy_lab

    def broken_inputs(*args, **kwargs):
        raise OSError("synthetic history outage")

    monkeypatch.setattr(strategy_lab, "build_inputs", broken_inputs)
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-oserror")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            lab = (await client.post("/api/strategy-labs", json=lab_body())).json()
            assert (await client.post(f"/api/strategy-labs/{lab['id']}/run-server")).status_code == 200
            current = None
            for _ in range(100):
                current = (await client.get(f"/api/strategy-labs/{lab['id']}")).json()
                if current["status"] == "error":
                    break
                await asyncio.sleep(0.05)
            assert current["status"] == "error"
            assert "synthetic history outage" in current["ranking"]["error"]


async def test_choose_saves_the_reviewed_plan_not_a_rebuild_from_the_current_template(tmp_path, monkeypatch):
    """L-B3：“存为我的策略”保存比较时已复核的那份方案：Lab 创建后模板变了（一次部署），
    保存的定义仍等于变体里的 plan，而不是按当前模板重新生成的那份。"""
    from trader_runtime import strategy_templates

    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-choose-plan")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            lab = (await client.post("/api/strategy-labs", json={"symbol": "SHFE.au2612", "tradingDays": 3,
                "variants": [{"templateId": "double_ma", "params": {"fast": 5, "slow": 20}},
                             {"templateId": "double_ma", "params": {"fast": 10, "slow": 30}}]})).json()
            expected = lab["variants"][0]["plan"]
            # 比较建好后模板漂移：同参数重新实例化会得到另一份 plan。
            original_build = strategy_templates.build_plan
            monkeypatch.setattr(strategy_templates, "build_plan",
                                lambda template, params, timeframe=None:
                                original_build(template, {"fast": 3, "slow": 60}, timeframe=timeframe))
            chosen = await client.post(f"/api/strategy-labs/{lab['id']}/choose",
                                       json={"variantId": "v1", "name": "实验室选中的双均线"})
            assert chosen.status_code == 200, chosen.text
            saved = app.state.store.get_strategy_draft(chosen.json()["strategyId"], user_id="local-user")
            assert saved["definition"]["plan"] == {**expected, "name": "实验室选中的双均线"}


async def test_choose_with_a_removed_template_is_a_409_before_any_rerun(tmp_path, monkeypatch):
    """#530：变体来自的模板之后被删了，保存是 4xx 并说明，不是 500，也不白跑一遍权威回测。"""
    from trader_runtime.routes import strategy_lab as lab_routes

    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-choose-gone")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            lab = (await client.post("/api/strategy-labs", json=lab_body())).json()
            reruns = []
            real = app.state.run_rule_backtest

            async def counting(*args, **kwargs):
                reruns.append(1)
                return await real(*args, **kwargs)

            monkeypatch.setattr(app.state, "run_rule_backtest", counting)
            monkeypatch.setattr(lab_routes, "templates", lambda: {})
            gone = await client.post(f"/api/strategy-labs/{lab['id']}/choose", json={"variantId": "v1"})
            assert gone.status_code == 409 and "模板已下线" in gone.json()["detail"]
            assert reruns == []
            assert app.state.store.get_lab(lab["id"], user_id="local-user")["chosen"] is None


async def test_latest_compare_filters_by_strategy_before_limiting(tmp_path, monkeypatch):
    """#530：这条策略的最近一次比较不会被其他 50 次实验室挤出窗口。"""
    continuous_fake_schedule(monkeypatch)
    app = app_for(tmp_path, "lab-latest-compare")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        store = app.state.store
        variants = [{"id": "v1", "label": "x", "plan": DOUBLE_MA.model_dump()}]
        older = store.save_lab(lab_id="older", user_id="local-user", symbol="SHFE.au2612",
                               request={"symbol": "SHFE.au2612", "strategyId": "s-old"}, variants=variants)
        for index in range(55):
            store.save_lab(lab_id=f"other-{index}", user_id="local-user", symbol="SHFE.au2612",
                           request={"symbol": "SHFE.au2612", "strategyId": "s-new"}, variants=variants)
        assert store.latest_lab_for_strategy(user_id="local-user", strategy_id="s-old")["id"] == older["id"]
        assert store.latest_lab_for_strategy(user_id="local-user", strategy_id="s-none") is None
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            shown = (await client.get("/api/strategy-plans/s-old/compare")).json()
            assert shown["labId"] == "older"
            assert (await client.get("/api/strategy-plans/s-none/compare")).json()["status"] == "none"


# -- the AI tools (no window: the cloud fallback runs; get waits; choose saves) ----------------------------

async def test_lab_tools_without_a_window_run_in_the_cloud_and_choose_saves(tmp_path, monkeypatch):
    continuous_fake_schedule(monkeypatch)
    # The tool's wait cadence is a knob (production 2 s): the assertion is that get
    # waits for the cloud run to finish, not that it waits a full round per poll.
    from trader_runtime import trader_tools
    monkeypatch.setattr(trader_tools, "STRATEGY_LAB_POLL_SECONDS", 0.05)
    from test_ai_annotations import with_tools
    app = app_for(tmp_path, "lab-tools")
    async with app.router.lifespan_context(app):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            with_tools(app)
            catalog = {item["operation"] for item in (await client.get("/api/agent/capabilities")).json()["operations"]}
            for name in ("POST /api/strategy-labs", "POST /api/strategy-labs/{lab_id}/run-server",
                         "tool:create_strategy_lab", "tool:get_strategy_lab"):
                assert name in catalog, name
            assert "tool:choose_strategy_lab_variant" in catalog

            # No uiWindowId and no bound window: the cloud fallback starts, where stays "server".
            created = (await client.post("/api/agent/call", json={"operation": "tool:create_strategy_lab", "body": {
                "symbol": "SHFE.au2612", "tradingDays": 3,
                "variants": [{"templateId": "double_ma", "params": {"fast": 5, "slow": 20}},
                             {"templateId": "double_ma", "params": {"fast": 10, "slow": 30}}]}})).json()
            assert created["ok"], created
            started = created["data"]["data"]
            assert started["where"] == "server" and started["tried"] == 2
            assert {item["id"] for item in started["variants"]} == {"v1", "v2"}
            lab_id = started["labId"]

            # get waits until the cloud run finishes and reports the ranked 简表.
            fetched = (await client.post("/api/agent/call", json={"operation": "tool:get_strategy_lab", "body": {
                "labId": lab_id, "waitSeconds": 90}})).json()
            assert fetched["ok"], fetched
            table = fetched["data"]["data"]
            assert table["status"] == "done" and table["where"] == "server" and table["tried"] == 2
            assert len(table["rows"]) == 2
            row = table["rows"][0]
            assert {"inSample", "outSample", "isRank", "decay", "flags"} <= set(row)
            assert row["inSample"]["net"] is not None or row["outSample"]["net"] is not None

            # An unknown lab comes back as a warning, not a crash.
            lost = (await client.post("/api/agent/call", json={"operation": "tool:get_strategy_lab", "body": {
                "labId": "nope", "waitSeconds": 0}})).json()
            assert not lost["ok"] and "没有这次比较" in lost["data"]["warning"]

            picked = (await client.post("/api/agent/call", json={"operation": "tool:choose_strategy_lab_variant", "body": {
                "labId": lab_id, "variantId": "v1", "name": "实验室存的双均线"}})).json()
            assert picked["ok"], picked
            saved = picked["data"]["data"]
            assert saved["strategyId"] and saved["backtest"]["trades"] is not None
            strategies = (await client.get("/api/strategies")).json()["items"]
            assert saved["strategyId"] in {item["strategyId"] for item in strategies}


def test_lab_command_validates_and_is_judged_by_the_reported_state():
    from pydantic import ValidationError

    from trader_runtime.ui_control import UiChanges, changes_match
    command = UiChanges.model_validate({"strategyLab": {"open": True, "labId": "lab-1", "run": True}})
    assert command.strategyLab.run is True
    for bad in ({}, {"run": False}, {"labId": "bad id"}, {"open": None}, {"other": 1}):
        with pytest.raises(ValidationError):
            UiChanges.model_validate({"strategyLab": bad})
    card = {"labId": "lab-1", "status": "done", "where": "browser", "progress": None, "tried": 3,
            "best": "v1", "rows": [{"variantId": "v1", "label": "a", "flags": ["best"]},
                                   {"variantId": "v2", "label": "b", "flags": []}]}
    assert changes_match({"strategyLab": card}, {"strategyLab": {"labId": "lab-1"}})
    assert changes_match({"strategyLab": card}, {"strategyLab": {"run": True}})
    assert changes_match({"strategyLab": card}, {"strategyLab": {"choose": "v2"}})
    # The tab is elsewhere: open did not land. A different lab, a status that never started
    # running, a variant the comparison does not know: all rejected.
    assert not changes_match({"strategyTab": "library", "strategyLab": card}, {"strategyLab": {"open": True}})
    assert not changes_match({"strategyLab": card}, {"strategyLab": {"labId": "lab-2"}})
    assert not changes_match({"strategyLab": {**card, "status": "ready"}}, {"strategyLab": {"run": True}})
    assert not changes_match({"strategyLab": card}, {"strategyLab": {"choose": "v9"}})
    assert changes_match({}, {"strategyLab": {"open": True}})       # an older client reports nothing

