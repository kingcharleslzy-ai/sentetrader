"""本站模拟盘真实成交模型（2026-10-13，paper_fill.py）的行为测试。

保护什么：时间差（按到达后的行情成交，10-12 夜盘 rb 对价单本站即成、快期排队未成）、
盘口量与冲击（超量逐档变差、限价以外转挂单）、挂单排队（成交量推进、一档量封顶、
穿价全成）、模式开关；以及部分成交拆分后账目（冻结、手续费、平仓盈亏、权益恒等式）、
落盘重启、行情静止兜底、日终撤单与到期强平不受影响。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from test_paper_book import (
    ACCOUNT,
    EXPIRY_1102_NS,
    TRADING_DATES,
    au_quote,
    feed,
    submit_request,
)
from tqkq_readonly_sidecar.paper import PaperEngine, VirtualClock
from tqkq_readonly_sidecar.paper_fill import (
    ENV_MODE,
    IDEAL,
    FillModel,
    join_queue,
    queue_step,
    taker_plan,
)

TICK = 0.02
START = datetime.fromisoformat("2026-11-02T09:00:00+08:00")


def quote(moment: str, last: float, *, bid: float, ask: float, bid_volume: float = 12,
          ask_volume: float = 9, volume: float | None = None, margin: float = 10_000.0,
          **extra) -> dict:
    """au 合约一档行情：价格、一档量与累计成交量都显式给出。"""
    payload = au_quote(moment, last, bid=bid, ask=ask, margin=margin, **extra)
    payload.update({"bid_volume1": bid_volume, "ask_volume1": ask_volume})
    if volume is not None:
        payload["volume"] = volume
    return payload


def make_engine(tmp_path, fill_model: FillModel, name: str = "paper.sqlite3",
                clock: VirtualClock | None = None) -> PaperEngine:
    engine = PaperEngine(db_path=str(tmp_path / name), clock=clock or VirtualClock(START),
                         trading_dates=TRADING_DATES, fill_model=fill_model)
    engine.open()
    engine.start()
    return engine


@pytest.fixture()
def realistic(tmp_path):
    engine = make_engine(tmp_path, FillModel())
    engine.create_account(ACCOUNT)
    yield engine
    engine.close()


@pytest.fixture()
def instant(tmp_path):
    """真实模式、时间差 0：到达即下单时刻，专测盘口量与排队。"""
    engine = make_engine(tmp_path, FillModel(latency_ms=0))
    engine.create_account(ACCOUNT)
    yield engine
    engine.close()


def trades_of(engine: PaperEngine, coid: str) -> list[dict]:
    rows = [row for row in engine.activity(ACCOUNT)["trades"] if row["clientOrderId"] == coid]
    return sorted(rows, key=lambda row: (row["tradeDateTime"], -int(row["tradeId"].split("|")[1])))


def assert_equity_identity(engine: PaperEngine) -> None:
    """权益 = 静态权益 + 平仓盈亏 + 持仓盈亏 − 手续费（期货账户恒等式，0.01 元容差）。"""
    account = engine.account(ACCOUNT)
    assert account["equity"] == pytest.approx(
        account["staticBalance"] + account["closeProfit"] + account["positionProfit"]
        - account["commission"], abs=0.01)


# -- 纯模型 -----------------------------------------------------------------


def test_taker_plan_walks_synthetic_levels_until_the_limit():
    book = {"ask_price1": 820.02, "ask_volume1": 9, "bid_price1": 819.98, "bid_volume1": 12}
    assert taker_plan("BUY", 820.10, 20, book, TICK, 1) == [
        (820.02, 9, 0), (820.04, 9, 1), (820.06, 2, 2)]
    # 限价就是对手价：只吃一档量，剩余由调用方转挂单。
    assert taker_plan("BUY", 820.02, 20, book, TICK, 1) == [(820.02, 9, 0)]
    # 每档 2 跳冲击。
    assert taker_plan("SELL", 819.90, 15, book, TICK, 2) == [(819.98, 12, 0), (819.94, 3, 2)]
    # 冲击 0 跳 = 对手价上量不限（理想口径）。
    assert taker_plan("BUY", 820.02, 50, book, TICK, 0) == [(820.02, 50, 0)]
    # 同一笔行情里已被同账户吃掉的量要扣除。
    assert taker_plan("BUY", 820.02, 20, book, TICK, 1, {820.02: 7.0}) == [(820.02, 2, 0)]
    # 前面的单已吃光前两档：1 手的单接着吃第三档，不因“本单只要 1 手”就停在前两档
    # （10-13 高频压测发现：之后的单全被挂在限价上）。
    assert taker_plan("BUY", 820.10, 1, book, TICK, 1, {820.02: 9.0, 820.04: 9.0}) == [
        (820.06, 1, 2)]
    # 对手价在限价之外、或没有对手盘（涨停无卖方）：不成交。
    assert taker_plan("BUY", 820.00, 1, book, TICK, 1) == []
    assert taker_plan("BUY", 900.0, 1, {**book, "ask_price1": float("nan")}, TICK, 1) == []


def test_join_queue_tail_inside_spread_and_deeper_levels():
    book = {"ask_price1": 820.02, "ask_volume1": 9, "bid_price1": 819.98, "bid_volume1": 12}
    assert join_queue("BUY", 819.98, book, TICK) == 12.0   # 买一：排在 12 手之后
    assert join_queue("BUY", 820.00, book, TICK) == 0.0    # 价差里：本价位只有自己
    assert join_queue("BUY", 819.90, book, TICK) is None   # 更深：看不到那一档
    assert join_queue("SELL", 820.02, book, TICK) == 9.0


def test_queue_step_volume_share_cap_cross_and_reset():
    book = {"ask_price1": 820.02, "ask_volume1": 9, "bid_price1": 819.98, "bid_volume1": 12,
            "last_price": 820.02, "volume": 1010}
    # 卖 3 手挂 820.02、队前 9：成交量 +10 且最新价在本价位 → 计 5，队前剩 4。
    step = queue_step("SELL", 820.02, 3, 9.0, 1000, book, TICK, 0.5)
    assert (step.fill, step.queue_ahead, step.last_volume) == (0, 4.0, 1010)
    # 再 +10：队前清零后余 1 手成交。
    step = queue_step("SELL", 820.02, 3, 4.0, 1010, {**book, "volume": 1020}, TICK, 0.5)
    assert (step.fill, step.queue_ahead) == (1, 0.0)
    # 最新价不在本价位：成交量增量不推进本价位队列；一档量变小把队前量封顶。
    step = queue_step("SELL", 820.02, 3, 9.0, 1000,
                      {**book, "last_price": 820.0, "ask_volume1": 3}, TICK, 0.5)
    assert (step.fill, step.queue_ahead) == (0, 3.0)
    # 穿价：最新价越过委托价 → 剩余全部成交。
    step = queue_step("SELL", 820.02, 3, 9.0, 1000, {**book, "last_price": 820.04}, TICK, 0.5)
    assert (step.fill, step.crossed) == (3, True)
    # 对手价恰等于委托价：按对手一档量成交。
    step = queue_step("BUY", 820.02, 20, 5.0, 1000, book, TICK, 0.5)
    assert (step.fill, step.crossed) == (9, False)
    # 累计成交量倒退（换日重置）：只重设基准，不推进。
    step = queue_step("SELL", 820.02, 3, 9.0, 5000, book, TICK, 0.5)
    assert (step.fill, step.queue_ahead, step.last_volume) == (0, 9.0, 1010)


def test_fill_model_config_from_env_and_validation():
    assert FillModel.from_env({}) == FillModel()
    assert FillModel().realistic and FillModel().describe() == {
        "mode": "realistic", "latencyMs": 300.0, "arrivalWaitMs": 500.0,
        "impactTicks": 1, "queueShare": 0.5}
    assert FillModel.from_env({ENV_MODE: "ideal"}).mode == "ideal"
    assert FillModel.from_env({"AI_TRADER_PAPER_FILL_LATENCY_MS": "120",
                               "AI_TRADER_PAPER_FILL_IMPACT_TICKS": "2"}).latency_ms == 120.0
    for bad in ({ENV_MODE: "fast"}, {"AI_TRADER_PAPER_FILL_QUEUE_SHARE": "0"},
                {"AI_TRADER_PAPER_FILL_LATENCY_MS": "-1"}):
        with pytest.raises(ValueError):
            FillModel.from_env(bad)


# -- 时间差 -------------------------------------------------------------------


def test_order_is_matched_on_the_quote_after_arrival_not_the_decision_quote(realistic, tmp_path):
    """10-12 夜盘那笔：对价卖单下单那一刻可成交，委托到达前盘口下移——真实模式挂着
    排队（快期同样），理想模式当场成交。"""
    feed(realistic, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
    report = realistic.submit(ACCOUNT, submit_request(
        "order-latency", direction="SELL", limitPrice=819.98))
    assert report["order"]["status"] == "ALIVE"
    assert report["order"]["filledVolume"] == 0
    # 到达前（+200ms）的行情不撮合这张单。
    feed(realistic, quote("09:31:00.200000", 820.0, bid=819.98, ask=820.02))
    assert realistic.query_order(ACCOUNT, "order-latency")["order"]["volumeLeft"] == 1
    # 到达后的第一笔行情：买一已下移到 819.96，卖单 819.98 不再可成交 → 排在卖一。
    feed(realistic, quote("09:31:00.500000", 819.97, bid=819.96, ask=819.98, ask_volume=6))
    order = realistic.query_order(ACCOUNT, "order-latency")["order"]
    assert (order["status"], order["volumeLeft"]) == ("ALIVE", 1)
    assert realistic.positions(ACCOUNT) == []

    ideal = make_engine(tmp_path, IDEAL, name="ideal.sqlite3")
    try:
        ideal.create_account(ACCOUNT)
        feed(ideal, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
        report = ideal.submit(ACCOUNT, submit_request(
            "order-latency", direction="SELL", limitPrice=819.98))
        assert report["order"]["status"] == "FINISHED"
        assert report["order"]["filledVolume"] == 1
        [trade] = trades_of(ideal, "order-latency")
        assert trade["fill"]["model"] == "ideal"
        assert trade["fill"]["liquidity"] == "taker"
        assert trade["fill"]["slippageTicks"] == 0.0
    finally:
        ideal.close()


def test_arrival_fill_records_decision_arrival_and_slippage(realistic):
    feed(realistic, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
    realistic.submit(ACCOUNT, submit_request("order-slip", limitPrice=820.06))
    feed(realistic, quote("09:31:00.400000", 820.04, bid=820.02, ask=820.04))
    [trade] = trades_of(realistic, "order-slip")
    assert trade["price"] == pytest.approx(820.04)
    fill = trade["fill"]
    assert fill["model"] == "realistic"
    assert fill["liquidity"] == "taker"
    assert fill["decisionPrice"] == pytest.approx(820.02)
    assert fill["arrivalPrice"] == pytest.approx(820.04)
    assert fill["fillPrice"] == pytest.approx(820.04)
    assert fill["slippageTicks"] == pytest.approx(1.0)
    assert fill["impactTicks"] == 0
    assert fill["queueWaitMs"] == 0.0
    assert fill["partial"] is False
    assert fill["arrivalAt"] - fill["decisionAt"] == 400_000_000
    position = realistic.positions(ACCOUNT)[0]
    assert position["openPrice"] == pytest.approx(820.04)
    assert realistic.health()["fillModel"]["mode"] == "realistic"


def test_quiet_market_arrives_on_the_cached_book_after_the_wait(realistic):
    """合约没有新行情（盘口没变）：到达时刻 + 等待上限后按缓存盘口到达；由别的
    合约的行情推进时钟触发（回放确定），真实时钟下空闲轮询同样会触发。"""
    feed(realistic, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
    realistic.submit(ACCOUNT, submit_request("order-quiet", limitPrice=820.02))
    rebar = au_quote("09:31:00.600000", 3071.0, symbol="SHFE.rb2701")
    rebar.update({"product_id": "rb", "price_tick": 1.0, "volume_multiple": 10.0,
                  "upper_limit": 3300.0, "lower_limit": 2800.0})
    feed(realistic, rebar)   # 600ms < 300 + 500：还在等
    assert trades_of(realistic, "order-quiet") == []
    feed(realistic, {**rebar, "datetime": "2026-11-02 09:31:00.900000"})
    [trade] = trades_of(realistic, "order-quiet")
    assert trade["price"] == pytest.approx(820.02)
    assert realistic.query_order(ACCOUNT, "order-quiet")["order"]["status"] == "FINISHED"


# -- 盘口量与冲击 ------------------------------------------------------------


def test_large_marketable_order_walks_the_book_with_impact(instant):
    feed(instant, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
    report = instant.submit(ACCOUNT, submit_request("order-big", volume=20, limitPrice=820.10))
    assert report["order"]["status"] == "FINISHED"
    assert report["order"]["filledVolume"] == 20
    trades = trades_of(instant, "order-big")
    assert [(t["price"], t["volume"]) for t in trades] == [
        (pytest.approx(820.02), 9), (pytest.approx(820.04), 9), (pytest.approx(820.06), 2)]
    assert [t["fill"]["impactTicks"] for t in trades] == [0, 1, 2]
    assert [t["fill"]["slippageTicks"] for t in trades] == [
        pytest.approx(0.0), pytest.approx(1.0), pytest.approx(2.0)]
    assert all(t["fill"]["partial"] for t in trades)
    assert len({t["tradeId"] for t in trades}) == 3
    position = instant.positions(ACCOUNT)[0]
    assert position["volume"] == 20
    assert position["openPrice"] == pytest.approx((9 * 820.02 + 9 * 820.04 + 2 * 820.06) / 20)
    account = instant.account(ACCOUNT)
    assert account["commission"] == pytest.approx(20 * 20.0)
    assert account["margin"] == pytest.approx(20 * 10_000.0)
    assert account["available"] == pytest.approx(account["equity"] - account["margin"])
    assert_equity_identity(instant)


def test_counterparty_limit_fills_top_level_and_rests_the_rest_then_cancel(instant):
    feed(instant, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
    report = instant.submit(ACCOUNT, submit_request("order-part", volume=20, limitPrice=820.02))
    order = report["order"]
    assert (order["status"], order["volume"], order["volumeLeft"], order["filledVolume"]) == (
        "ALIVE", 20, 11, 9)
    assert order["lastMsg"] == "部分成交"
    account = instant.account(ACCOUNT)
    assert account["margin"] == pytest.approx(9 * 10_000.0)
    # 剩余 11 手的冻结保证金仍占着可用资金。
    assert account["available"] == pytest.approx(account["equity"] - 9 * 10_000.0 - 11 * 10_000.0)
    live = [row for row in instant.activity(ACCOUNT)["orders"] if row["clientOrderId"] == "order-part"]
    assert live[0]["filledVolume"] == 9 and live[0]["status"] == "ALIVE"
    cancelled = instant.cancel_order(ACCOUNT, "order-part", "cmd-cancel-part")["order"]
    assert (cancelled["status"], cancelled["volumeLeft"], cancelled["isError"]) == ("FINISHED", 11, False)
    account = instant.account(ACCOUNT)
    assert account["available"] == pytest.approx(account["equity"] - 9 * 10_000.0)
    assert instant.positions(ACCOUNT)[0]["volume"] == 9
    assert_equity_identity(instant)


def test_partial_close_today_books_close_profit_fees_and_frozen_volume(instant):
    feed(instant, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02, ask_volume=20))
    instant.submit(ACCOUNT, submit_request("order-open10", volume=10, limitPrice=820.02))
    feed(instant, quote("09:40:00.000000", 820.52, bid=820.50, ask=820.54, bid_volume=4))
    close = instant.submit(ACCOUNT, submit_request(
        "order-close10", direction="SELL", offset="CLOSETODAY", volume=10, limitPrice=820.50))
    assert (close["order"]["filledVolume"], close["order"]["volumeLeft"]) == (4, 6)
    long = instant.positions(ACCOUNT)[0]
    assert (long["volume"], long["todayVolume"], long["frozenToday"]) == (6, 6, 6)
    account = instant.account(ACCOUNT)
    assert account["closeProfit"] == pytest.approx((820.50 - 820.02) * 4 * 1000)
    assert account["commission"] == pytest.approx(14 * 20.0)
    assert_equity_identity(instant)
    # 剩余 6 手：价格穿越（买一升到 820.52）→ 按委托价全部成交。
    feed(instant, quote("09:41:00.000000", 820.54, bid=820.52, ask=820.56))
    assert instant.query_order(ACCOUNT, "order-close10")["order"]["status"] == "FINISHED"
    assert instant.positions(ACCOUNT) == []
    account = instant.account(ACCOUNT)
    assert account["closeProfit"] == pytest.approx((820.50 - 820.02) * 10 * 1000)
    assert account["commission"] == pytest.approx(20 * 20.0)
    assert account["margin"] == 0.0
    assert account["available"] == pytest.approx(account["equity"])
    assert_equity_identity(instant)


# -- 挂单排队 -----------------------------------------------------------------


def test_resting_order_queues_behind_the_level_and_fills_after_it(instant):
    feed(instant, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02, volume=1000))
    report = instant.submit(ACCOUNT, submit_request(
        "order-queue", direction="SELL", volume=3, limitPrice=820.02))
    assert report["order"]["filledVolume"] == 0     # 排在卖一 9 手之后
    feed(instant, quote("09:31:01.000000", 820.02, bid=819.98, ask=820.02, volume=1010))
    assert trades_of(instant, "order-queue") == []  # 计 5 手，队前剩 4
    feed(instant, quote("09:31:02.000000", 820.02, bid=819.98, ask=820.02, volume=1020))
    [first] = trades_of(instant, "order-queue")      # 再计 5 手：队前清零后余 1 手
    assert first["volume"] == 1 and first["price"] == pytest.approx(820.02)
    fill = first["fill"]
    assert fill["liquidity"] == "maker"
    assert fill["queueAheadAtJoin"] == 9.0
    assert fill["queueWaitMs"] == pytest.approx(2000.0)
    assert fill["slippageTicks"] == pytest.approx(-2.0)   # 挂单相对买一决策价改善 2 跳
    assert fill["partial"] is True
    assert instant.query_order(ACCOUNT, "order-queue")["order"]["volumeLeft"] == 2
    feed(instant, quote("09:31:03.000000", 820.04, bid=820.02, ask=820.06, volume=1030))
    trades = trades_of(instant, "order-queue")
    assert [t["volume"] for t in trades] == [1, 2]
    short = instant.positions(ACCOUNT)[0]
    assert (short["direction"], short["volume"]) == ("short", 3)
    assert short["openPrice"] == pytest.approx(820.02)
    assert_equity_identity(instant)


def test_shrinking_level_caps_the_queue_ahead(instant):
    feed(instant, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02, volume=1000))
    instant.submit(ACCOUNT, submit_request("order-cap", direction="SELL", limitPrice=820.02))
    # 卖一从 9 手撤到 3 手：排在前面的最多还有 3 手。
    feed(instant, quote("09:31:01.000000", 820.0, bid=819.98, ask=820.02, ask_volume=3, volume=1000))
    # 本价位成交 8 手 × 0.5 = 4 > 3 → 成交 1 手；不封顶（队前 9）就不会成交。
    feed(instant, quote("09:31:02.000000", 820.02, bid=819.98, ask=820.02, ask_volume=3, volume=1008))
    assert [t["volume"] for t in trades_of(instant, "order-cap")] == [1]


# -- 落盘重启、日终与到期 -------------------------------------------------------


def _readback(engine: PaperEngine) -> dict:
    activity = engine.activity(ACCOUNT)
    # 行情源状态（dataStatus）随新进程重新判断，不属于账本。
    account = {k: v for k, v in engine.account(ACCOUNT).items() if k not in {"capturedAt", "dataStatus"}}
    return {"account": account, "positions": engine.positions(ACCOUNT),
            "orders": activity["orders"], "trades": activity["trades"]}


def test_partial_fill_and_queue_state_survive_a_restart(tmp_path):
    model = FillModel(latency_ms=0)
    engine = make_engine(tmp_path, model)
    engine.create_account(ACCOUNT)
    feed(engine, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
    engine.submit(ACCOUNT, submit_request("order-restart", volume=20, limitPrice=820.02))
    before = _readback(engine)
    engine.close()
    engine = make_engine(tmp_path, model,
                         clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00+08:00")))
    try:
        assert _readback(engine) == before
        order = engine.query_order(ACCOUNT, "order-restart")["order"]
        assert (order["status"], order["volumeLeft"]) == ("ALIVE", 11)
        # 重启后继续撮合：卖一跌破委托价 → 剩余 11 手按委托价成交。
        feed(engine, quote("09:32:00.000000", 819.98, bid=819.96, ask=820.00))
        assert engine.query_order(ACCOUNT, "order-restart")["order"]["status"] == "FINISHED"
        trades = trades_of(engine, "order-restart")
        assert [t["volume"] for t in trades] == [9, 11]
        assert len({t["tradeId"] for t in trades}) == 2
        assert engine.positions(ACCOUNT)[0]["volume"] == 20
        assert_equity_identity(engine)
    finally:
        engine.close()


def test_settlement_cancels_partial_resting_order_and_expiry_still_force_closes(instant):
    feed(instant, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02, expire_ns=EXPIRY_1102_NS))
    instant.submit(ACCOUNT, submit_request("order-exp-open", volume=2, limitPrice=820.02))
    instant.submit(ACCOUNT, submit_request("order-exp-rest", volume=5, limitPrice=819.90))
    assert instant.query_order(ACCOUNT, "order-exp-rest")["order"]["status"] == "ALIVE"
    feed(instant, quote("2026-11-03 09:31:00.000000", 824.0, bid=823.98, ask=824.02,
                        settlement=824.0, expire_ns=EXPIRY_1102_NS))
    rest = instant.query_order(ACCOUNT, "order-exp-rest")["order"]
    assert (rest["status"], rest["isError"]) == ("FINISHED", False)
    expiry = [t for t in instant.activity(ACCOUNT)["trades"] if t.get("reason") == "expiry"]
    assert [(t["volume"], t["price"]) for t in expiry] == [(2, pytest.approx(824.0))]
    assert instant.positions(ACCOUNT) == []
    account = instant.account(ACCOUNT)
    assert account["margin"] == 0.0
    assert account["available"] == pytest.approx(account["equity"])
    assert account["closeProfit"] == pytest.approx((824.0 - 820.02) * 2 * 1000)


# -- 审查修正（Codex P1，2026-10-13） ------------------------------------------


def test_queue_step_shares_tick_volume_and_carries_fractions():
    book = {"ask_price1": 820.02, "ask_volume1": 9, "bid_price1": 819.98, "bid_volume1": 12,
            "last_price": 820.02, "volume": 1010}
    # 同一笔行情里本账户本价位已成交 5 手（同价位另一张挂单）：10 × 0.5 = 5 手已被占完。
    step = queue_step("SELL", 820.02, 3, 0.0, 1000, book, TICK, 0.5, consumed_at_limit=5.0)
    assert step.fill == 0
    # 每笔只成交 1 手 × 0.5：不足一手的余量记为负的队前量，第二笔累计到 1 手成交。
    step = queue_step("SELL", 820.02, 3, 0.0, 1000, {**book, "volume": 1001}, TICK, 0.5)
    assert (step.fill, step.queue_ahead) == (0, -0.5)
    step = queue_step("SELL", 820.02, 3, step.queue_ahead, 1001, {**book, "volume": 1002}, TICK, 0.5)
    assert (step.fill, step.queue_ahead) == (1, 0.0)


def test_same_price_resting_orders_share_one_passive_volume(instant):
    feed(instant, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02, volume=1000))
    # 两张卖单都挂在价差里的 820.00（队前 0）。
    for coid in ("order-share-a", "order-share-b"):
        instant.submit(ACCOUNT, submit_request(coid, direction="SELL", volume=3, limitPrice=820.00))
    # 本价位成交 10 手 × 0.5 = 5 手：两张单合计只能成交 5 手，不是各 3 手。
    feed(instant, quote("09:31:01.000000", 820.00, bid=819.98, ask=820.02, volume=1010))
    filled = sum(t["volume"] for coid in ("order-share-a", "order-share-b")
                 for t in trades_of(instant, coid))
    assert filled == 5
    assert_equity_identity(instant)


def test_quiet_market_fallback_ignores_a_stale_cached_book(realistic):
    feed(realistic, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
    realistic.submit(ACCOUNT, submit_request("order-stale", limitPrice=820.02))
    rebar = au_quote("09:31:20.000000", 3071.0, symbol="SHFE.rb2701")
    rebar.update({"product_id": "rb", "price_tick": 1.0, "volume_multiple": 10.0,
                  "upper_limit": 3300.0, "lower_limit": 2800.0})
    feed(realistic, rebar)   # 沪金缓存盘口已是 20 秒前（> 15 秒过期阈值）：不按它到达
    assert trades_of(realistic, "order-stale") == []
    assert realistic.query_order(ACCOUNT, "order-stale")["order"]["status"] == "ALIVE"
    feed(realistic, quote("09:31:21.000000", 820.0, bid=819.98, ask=820.02))
    [trade] = trades_of(realistic, "order-stale")
    assert trade["price"] == pytest.approx(820.02)


def test_snapshot_with_partial_live_order_is_versioned_for_old_readers(instant, monkeypatch):
    from tqkq_readonly_sidecar import paper_book

    feed(instant, quote("09:31:00.000000", 820.0, bid=819.98, ask=820.02))
    instant.submit(ACCOUNT, submit_request("order-version", volume=20, limitPrice=820.02))
    book = instant._accounts[ACCOUNT].book
    state = book.export_state()
    assert state["version"] == paper_book.STATE_VERSION > paper_book.BASE_STATE_VERSION
    # 不认识部分成交的旧版本（只读到 1）拒绝恢复，不按原始手数记账。
    monkeypatch.setattr(paper_book, "STATE_VERSION", paper_book.BASE_STATE_VERSION)
    with pytest.raises(RuntimeError, match="paper_state_version_too_new"):
        book.restore_state(state)
    monkeypatch.undo()
    # 剩余全部成交后没有部分成交活单：回到旧版本也能读的版本号。
    feed(instant, quote("09:32:00.000000", 819.98, bid=819.96, ask=820.00))
    assert instant.query_order(ACCOUNT, "order-version")["order"]["status"] == "FINISHED"
    assert book.export_state()["version"] == paper_book.BASE_STATE_VERSION


# -- 同一笔行情的已耗盘口量随快照恢复（#834） -----------------------------------


def _restart(engine: PaperEngine, tmp_path, model: FillModel, *, crash: bool = False,
             at: str = "2026-11-02T09:31:00+08:00") -> PaperEngine:
    """正常停机（写检查点）或模拟进程被杀（不走 close），再按同一库重启。"""
    if crash:
        from test_paper_persistence import _crash
        _crash(engine)
    else:
        engine.close()
    return make_engine(tmp_path, model, clock=VirtualClock(datetime.fromisoformat(at)))


def test_identical_quote_after_restart_does_not_resupply_consumed_depth(tmp_path):
    """issue #834 的 Q：20 手买单在 Q 吃掉卖一 9 手、剩 11 手。同进程重放 Q 不再成交；
    正常停机后重启再收到完全相同的 Q（行情源重连主动推首包）同样不再成交。同一时间
    但盘口量变了的行情与同一笔共用预算（只补增加的量）；新时间的行情重新供应一档量。"""
    model = FillModel(latency_ms=0)
    engine = make_engine(tmp_path, model)
    engine.create_account(ACCOUNT)
    q = quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02)
    try:
        feed(engine, q)
        engine.submit(ACCOUNT, submit_request("order-834", volume=20, limitPrice=820.02))
        feed(engine, q)
        assert [t["volume"] for t in trades_of(engine, "order-834")] == [9]
        before = _readback(engine)
        engine = _restart(engine, tmp_path, model)
        feed(engine, q)
        assert _readback(engine) == before
        assert engine.query_order(ACCOUNT, "order-834")["order"]["volumeLeft"] == 11
        # 同一时间、卖一从 9 手增到 15 手：同一笔行情的预算，只补多出来的 6 手。
        feed(engine, quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02, ask_volume=15))
        assert [t["volume"] for t in trades_of(engine, "order-834")] == [9, 6]
        # 新时间的行情：一档量重新供应，剩余 5 手成交。
        feed(engine, quote("09:31:01.000000", 820.02, bid=819.98, ask=820.02))
        trades = trades_of(engine, "order-834")
        assert [t["volume"] for t in trades] == [9, 6, 5]
        assert len({t["tradeId"] for t in trades}) == 3
        assert engine.positions(ACCOUNT)[0]["volume"] == 20
        assert_equity_identity(engine)
    finally:
        engine.close()


def _shared_budget_run(tmp_path, name: str, side: str, *, restart: bool) -> tuple[dict, dict]:
    """同价位多张单（平今 + 两张开仓）在同一笔行情上共享一档量；restart=True 时在
    同一笔行情重放前正常停机重启。返回 (新行情前, 新行情后) 的账本回读。"""
    model = FillModel(latency_ms=0)
    folder = tmp_path / name
    folder.mkdir()
    engine = make_engine(folder, model)
    engine.create_account(ACCOUNT)
    near, far = ("BUY", "SELL") if side == "BUY" else ("SELL", "BUY")
    limit = 820.02 if side == "BUY" else 819.98
    try:
        # 最新价 = 委托价：不触发穿价。先反向开 5 手，供平今。
        feed(engine, quote("09:30:59.000000", limit, bid=819.98, ask=820.02))
        engine.submit(ACCOUNT, submit_request(f"{name}-seed", direction=far, volume=5,
                                              limitPrice=820.02 if far == "BUY" else 819.98))
        q = quote("09:31:00.000000", limit, bid=819.98, ask=820.02)
        feed(engine, q)
        engine.submit(ACCOUNT, submit_request(f"{name}-close", direction=near, offset="CLOSETODAY",
                                              volume=5, limitPrice=limit))
        engine.submit(ACCOUNT, submit_request(f"{name}-a", direction=near, volume=10, limitPrice=limit))
        engine.submit(ACCOUNT, submit_request(f"{name}-b", direction=near, volume=3, limitPrice=limit))
        feed(engine, q)
        if restart:
            engine = _restart(engine, folder, model)
        feed(engine, q)
        held = _readback(engine)
        feed(engine, quote("09:31:01.000000", limit, bid=819.98, ask=820.02))
        assert_equity_identity(engine)
        return held, _readback(engine)
    finally:
        engine.close()


def _volumes(readback: dict, suffix: str) -> list[int]:
    return [t["volume"] for t in readback["trades"] if t["clientOrderId"].endswith(suffix)]


def _amounts(readback: dict) -> dict:
    # 两次运行的委托号前缀不同（steady/restart），比的是资金、持仓与每笔数量金额。
    return {"account": readback["account"], "positions": readback["positions"],
            "trades": sorted((t["clientOrderId"].split("-", 1)[1], t["volume"], t["price"],
                              t["commission"]) for t in readback["trades"]),
            "orders": sorted((o["clientOrderId"].split("-", 1)[1], o["volumeLeft"], o["status"])
                             for o in readback["orders"])}


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_shared_depth_budget_is_identical_with_and_without_a_restart(tmp_path, side):
    held, after = _shared_budget_run(tmp_path, "steady", side, restart=False)
    held_r, after_r = _shared_budget_run(tmp_path, "restart", side, restart=True)
    # 一档量（卖一 9 / 买一 12）在这笔行情上只供应一次：平今 5 手先吃，开仓 a 吃剩下的，b 排队。
    depth = 9 if side == "BUY" else 12
    for readback in (held, held_r):
        assert _volumes(readback, "-close") == [5]
        assert _volumes(readback, "-a") == [depth - 5]
        assert _volumes(readback, "-b") == []
    assert _amounts(held_r) == _amounts(held)
    # 新时间的行情：两张同价位挂单共享新一档量（够两张单剩余），重启与否结果相同。
    assert _amounts(after_r) == _amounts(after)
    assert sum(_volumes(after_r, "-a")) + sum(_volumes(after_r, "-b")) == 13


def test_cancel_after_restart_releases_only_the_remaining_freeze(tmp_path):
    model = FillModel(latency_ms=0)
    engine = make_engine(tmp_path, model)
    engine.create_account(ACCOUNT)
    q = quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02)
    try:
        feed(engine, q)
        engine.submit(ACCOUNT, submit_request("order-834c", volume=20, limitPrice=820.02))
        engine = _restart(engine, tmp_path, model)
        feed(engine, q)   # 同一笔行情重放：不再成交
        before = _readback(engine)
        engine.cancel_order(ACCOUNT, "order-834c", "cmd-cancel-834c")
        order = engine.query_order(ACCOUNT, "order-834c")["order"]
        assert (order["status"], order["volumeLeft"], order["filledVolume"]) == ("FINISHED", 11, 9)
        after = _readback(engine)
        assert after["trades"] == before["trades"]
        assert after["positions"] == before["positions"]
        assert len({t["tradeId"] for t in after["trades"]}) == 1
        account = engine.account(ACCOUNT)
        # 只解除剩余 11 手的冻结：可用 = 权益 − 已成交 9 手的保证金。
        assert account["available"] == pytest.approx(account["equity"] - 9 * 10_000.0)
        assert account["margin"] == before["account"]["margin"]
        assert account["commission"] == before["account"]["commission"]
        assert_equity_identity(engine)
    finally:
        engine.close()


def test_crash_restart_keeps_the_committed_budget_and_redoes_an_uncommitted_fill(tmp_path, monkeypatch):
    """进程被杀（不走 close、不合并检查点）：已提交的成交连同已耗量一起恢复，重放同一
    笔行情不再成交；成交在内存里发生但没来得及提交就被杀，成交与已耗量一起丢失，
    重放这笔行情按第一次的结果成交一次，不多不少。"""
    model = FillModel(latency_ms=0)
    engine = make_engine(tmp_path, model)
    engine.create_account(ACCOUNT)
    try:
        feed(engine, quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02))
        # 挂在价差里（820.00），下一笔卖一跌到 820.00 时按对价吃一档量。
        engine.submit(ACCOUNT, submit_request("order-crash", volume=20, limitPrice=820.00))
        hit = quote("09:31:01.000000", 820.00, bid=819.98, ask=820.00)
        feed(engine, hit)
        assert [t["volume"] for t in trades_of(engine, "order-crash")] == [9]
        committed = _readback(engine)
        engine = _restart(engine, tmp_path, model, crash=True, at="2026-11-02T09:31:01+08:00")
        feed(engine, hit)
        assert _readback(engine) == committed

        # 下一笔行情的成交只发生在内存里（落盘前被杀）。
        nxt = quote("09:31:02.000000", 820.00, bid=819.98, ask=820.00)
        monkeypatch.setattr(engine, "_persist_account", lambda *args, **kwargs: None)
        feed(engine, nxt)
        in_memory = engine._accounts[ACCOUNT].book.trades()   # 读接口读的是已提交的库
        assert [t["volume"] for t in in_memory if t["clientOrderId"] == "order-crash"] == [9, 9]
        monkeypatch.undo()
        engine = _restart(engine, tmp_path, model, crash=True, at="2026-11-02T09:31:02+08:00")
        assert _readback(engine) == committed
        feed(engine, nxt)
        feed(engine, nxt)
        trades = trades_of(engine, "order-crash")
        assert [t["volume"] for t in trades] == [9, 9]
        assert len({t["tradeId"] for t in trades}) == 2
        assert engine.query_order(ACCOUNT, "order-crash")["order"]["volumeLeft"] == 2
        assert_equity_identity(engine)
    finally:
        engine.close()


def _drop_consumed(db_path) -> None:
    """把库里的当前状态行与检查点改成 #834 修复前的样子（没有已耗量键）。"""
    import json
    import sqlite3

    from tqkq_readonly_sidecar.paper_book import CONSUMED_KEY

    connection = sqlite3.connect(db_path)
    try:
        with connection:
            for table, column, key in (("paper_account_heads", "head_json", "account_id"),
                                       ("paper_accounts", "state_json", "id")):
                for row_id, text in connection.execute(f"SELECT {key}, {column} FROM {table}").fetchall():
                    state = json.loads(text)
                    assert CONSUMED_KEY in state
                    state.pop(CONSUMED_KEY)
                    connection.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?",
                                       (json.dumps(state), row_id))
    finally:
        connection.close()


@pytest.mark.parametrize("partial", [True, False], ids=["v2-partial", "v1-shared"])
def test_old_snapshot_without_budget_waits_for_a_new_quote(tmp_path, partial):
    """修复前写的快照（版本 2：部分成交活单；版本 1：另一张单已在这笔行情上吃光一档量，
    本单 0 成交挂着）没有已耗量：保守按恢复的那笔行情盘口全部用尽，同一笔行情（含同
    时间盘口变化）不成交，新时间的行情照常撮合；恢复两次结果相同。"""
    from tqkq_readonly_sidecar import paper_book

    model = FillModel(latency_ms=0)
    engine = make_engine(tmp_path, model)
    engine.create_account(ACCOUNT)
    q = quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02)
    feed(engine, q)
    if partial:
        engine.submit(ACCOUNT, submit_request("order-old", volume=20, limitPrice=820.02))
        left, refill = 11, 9
    else:
        engine.submit(ACCOUNT, submit_request("order-full", volume=9, limitPrice=820.02))
        engine.submit(ACCOUNT, submit_request("order-old", volume=5, limitPrice=820.02))
        left, refill = 5, 5
    version = engine._accounts[ACCOUNT].book.export_state()["version"]
    assert version == (paper_book.STATE_VERSION if partial else paper_book.BASE_STATE_VERSION)
    engine.close()
    _drop_consumed(tmp_path / "paper.sqlite3")
    for _ in range(2):   # 恢复两次：第一次开库后正常停机，检查点写回带保守标记的键
        engine = make_engine(tmp_path, model,
                             clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00+08:00")))
        try:
            feed(engine, q)
            feed(engine, quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02, ask_volume=15))
            assert engine.query_order(ACCOUNT, "order-old")["order"]["volumeLeft"] == left
            exported = engine._accounts[ACCOUNT].book.export_state()[paper_book.CONSUMED_KEY]
            assert exported == {"SHFE.au2612": {"datetime": q["datetime"], "exhausted": True}}
        finally:
            engine.close()
    engine = make_engine(tmp_path, model,
                         clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00+08:00")))
    try:
        feed(engine, quote("09:31:01.000000", 820.02, bid=819.98, ask=820.02))
        assert engine.query_order(ACCOUNT, "order-old")["order"]["volumeLeft"] == left - refill
        assert engine._accounts[ACCOUNT].book.export_state()[paper_book.CONSUMED_KEY][
            "SHFE.au2612"]["levels"] == [[820.02, float(refill)]]
        assert_equity_identity(engine)
    finally:
        engine.close()


def test_old_snapshot_pending_order_arrives_on_the_next_new_quote_as_taker(tmp_path):
    """修复前快照里还没到达的委托：不按已耗量未知的恢复行情到达（到达了吃不到量，
    只会被当成挂单、下一笔按委托价穿价成交），等下一笔新行情按吃单逐档成交。"""
    from tqkq_readonly_sidecar import paper_book

    engine = make_engine(tmp_path, FillModel())   # 时间差 300ms
    engine.create_account(ACCOUNT)
    q = quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02)
    feed(engine, q)
    engine.submit(ACCOUNT, submit_request("order-pending", volume=20, limitPrice=820.06))
    assert engine._accounts[ACCOUNT].book.export_state()["version"] == paper_book.BASE_STATE_VERSION
    engine.close()
    _drop_consumed(tmp_path / "paper.sqlite3")
    engine = make_engine(tmp_path, FillModel(),
                         clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00.500+08:00")))
    try:
        feed(engine, q)
        assert engine._accounts[ACCOUNT].book.pending_arrival()
        feed(engine, quote("09:31:01.000000", 820.02, bid=819.98, ask=820.02))
        trades = trades_of(engine, "order-pending")
        assert [(t["volume"], t["price"]) for t in trades] == [
            (9, pytest.approx(820.02)), (9, pytest.approx(820.04)), (2, pytest.approx(820.06))]
        assert {t["fill"]["liquidity"] for t in trades} == {"taker"}
        assert_equity_identity(engine)
    finally:
        engine.close()


def test_old_snapshot_crossed_remainder_waits_for_a_new_quote(tmp_path):
    """修复前快照里已到达、部分成交、限价已越过行情的挂单：重放恢复的那笔行情不走穿价
    分支把剩余全部成交（审查 P1，2026-10-13），等行情时间变了才按穿价成交。"""
    model = FillModel(latency_ms=0)
    engine = make_engine(tmp_path, model)
    engine.create_account(ACCOUNT)
    q = quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02)
    feed(engine, q)
    # 卖一 9 手 820.02，超出一档量多滑 1 跳到 820.04 再吃 9 手，剩 2 手挂 820.04（最新价 820.02 已低于它）。
    engine.submit(ACCOUNT, submit_request("order-cross", volume=20, limitPrice=820.04))
    assert engine.query_order(ACCOUNT, "order-cross")["order"]["volumeLeft"] == 2
    engine.close()
    _drop_consumed(tmp_path / "paper.sqlite3")
    engine = make_engine(tmp_path, model,
                         clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00+08:00")))
    try:
        before = _readback(engine)
        feed(engine, q)
        assert _readback(engine) == before
        assert engine.query_order(ACCOUNT, "order-cross")["order"]["volumeLeft"] == 2
        feed(engine, quote("09:31:01.000000", 820.02, bid=819.98, ask=820.02))
        assert [t["volume"] for t in trades_of(engine, "order-cross")][-1] == 2
        assert engine.query_order(ACCOUNT, "order-cross")["order"]["volumeLeft"] == 0
        assert_equity_identity(engine)
    finally:
        engine.close()


def test_zero_latency_order_on_an_old_snapshot_quote_arrives_on_the_next_new_quote(tmp_path):
    """时间差 0 的下单当场到达也过同一道守卫（#837 Codex P2）：在修复前快照恢复的那笔
    行情（已耗量未知）上下单，不按它到达成挂单（下一笔会走穿价/排队），留到下一笔新
    行情按吃单逐档成交。"""
    model = FillModel(latency_ms=0)
    engine = make_engine(tmp_path, model)
    engine.create_account(ACCOUNT)
    q = quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02)
    feed(engine, q)
    engine.submit(ACCOUNT, submit_request("order-first", volume=1, limitPrice=820.02))
    engine.close()
    _drop_consumed(tmp_path / "paper.sqlite3")
    engine = make_engine(tmp_path, model,
                         clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00+08:00")))
    try:
        feed(engine, q)
        report = engine.submit(ACCOUNT, submit_request("order-zero", volume=5, limitPrice=820.02))
        assert report["order"]["volumeLeft"] == 5
        assert engine._accounts[ACCOUNT].book.pending_arrival()
        assert trades_of(engine, "order-zero") == []
        feed(engine, quote("09:31:01.000000", 820.02, bid=819.98, ask=820.02))
        trades = trades_of(engine, "order-zero")
        assert [(t["volume"], t["price"]) for t in trades] == [(5, pytest.approx(820.02))]
        assert {t["fill"]["liquidity"] for t in trades} == {"taker"}
        assert_equity_identity(engine)
    finally:
        engine.close()


def test_budget_survives_an_ideal_mode_restart_in_between(tmp_path):
    """真实 → 理想 → 真实：理想模式不用已耗量，但原样保留并写回；切回真实模式后
    同一笔行情仍不再供应。"""
    from tqkq_readonly_sidecar import paper_book

    model = FillModel(latency_ms=0)
    engine = make_engine(tmp_path, model)
    engine.create_account(ACCOUNT)
    q = quote("09:31:00.000000", 820.02, bid=819.98, ask=820.02)
    feed(engine, q)
    engine.submit(ACCOUNT, submit_request("order-switch", volume=20, limitPrice=820.02))
    budget = engine._accounts[ACCOUNT].book.export_state()[paper_book.CONSUMED_KEY]
    assert budget == {"SHFE.au2612": {"datetime": q["datetime"], "levels": [[820.02, 9.0]]}}
    engine = _restart(engine, tmp_path, IDEAL)
    try:
        assert engine._accounts[ACCOUNT].book.export_state()[paper_book.CONSUMED_KEY] == budget
    finally:
        engine.close()
    engine = make_engine(tmp_path, model,
                         clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00+08:00")))
    try:
        feed(engine, q)
        assert engine.query_order(ACCOUNT, "order-switch")["order"]["volumeLeft"] == 11
    finally:
        engine.close()
