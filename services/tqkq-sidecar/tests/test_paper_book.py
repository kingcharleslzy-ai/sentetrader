"""本站模拟盘撮合核心（paper_book / paper 引擎）的场景与数值对照测试。

撮合、记账全部走 TqSdk 官方 SimTrade；测试用 RecordedQuoteSource 形状的录制报价与
虚拟时钟离线驱动，不连真实快期。数值对照用例的期望值按交易所口径手算写成常量。
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime

import pytest

from tqkq_readonly_sidecar.paper import PaperEngine, VirtualClock
from tqkq_readonly_sidecar.paper_book import (
    BASE_STATE_VERSION,
    LOTS_TRADE_SEQ_KEY,
    STATE_VERSION,
    PaperBook,
)

TOKEN = "paper-test-token-with-at-least-32-characters"
ACCOUNT = "paper:0000000000a1"  # the Runtime's id format (colon included)
SECOND_ACCOUNT = "paper:0000000000b2"
# 合成日历：2026-11-02(一) ~ 11-06(五)、11-09(一) ~ 11-10(二)；11-07/08 周末休市。
TRADING_DATES = {
    "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
    "2026-11-09", "2026-11-10",
}
DAY = [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]]
NIGHT = [["21:00:00", "02:30:00"]]
FAR_EXPIRY_NS = 1_800_000_000_000_000_000  # 2027 年前后，远未到期
# 真实到期时刻（北京时 15:00 收盘后到期）：到期结算测试用真实日期与明确的
# 目标日/到期日，不用 expire_ns=1 逼出合成边界（#527 返修）。
EXPIRY_1102_NS = int(datetime.fromisoformat("2026-11-02T15:00:00+08:00").timestamp() * 1e9)
EXPIRY_1103_NS = int(datetime.fromisoformat("2026-11-03T15:00:00+08:00").timestamp() * 1e9)


def au_quote(moment: str, last: float, *, bid: float | None = None, ask: float | None = None,
             settlement: float = float("nan"), pre_settlement: float = float("nan"),
             margin: float = 80_000.0,
             commission: float = 20.0, delivery: tuple[int, int] = (2026, 12),
             expire_ns: int = FAR_EXPIRY_NS, symbol: str = "SHFE.au2612") -> dict:
    tick = 0.02
    return {
        "symbol": symbol,
        "datetime": moment if "-" in moment else f"2026-11-02 {moment}",
        "last_price": last,
        "ask_price1": ask if ask is not None else last + tick,
        "bid_price1": bid if bid is not None else last - tick,
        "ask_volume1": 9,
        "bid_volume1": 12,
        "ins_class": "FUTURE",
        # 官方 SimTrade 按品种（exchange_id + product_id）对同品种多空取大边
        # 保证金；真实行情带这个字段，_detach 也必须保留它（审查 527-2）。
        "product_id": "au",
        "volume_multiple": 1000.0,
        "price_tick": tick,
        "upper_limit": 900.0,
        "lower_limit": 740.0,
        "margin": margin,
        "commission": commission,
        "settlement": settlement,
        "pre_settlement": pre_settlement,
        "pre_close": 818.5,
        "trading_time": {"day": DAY, "night": NIGHT},
        "expire_datetime": expire_ns,
        "delivery_year": delivery[0],
        "delivery_month": delivery[1],
        "instrument_name": "沪金2612",
    }


def submit_request(client_order_id: str, **overrides) -> dict:
    request = {
        "commandId": f"cmd-{client_order_id}",
        "clientOrderId": client_order_id,
        "symbol": "SHFE.au2612",
        "direction": "BUY",
        "offset": "OPEN",
        "volume": 1,
        "priceType": "LIMIT",
        "limitPrice": 820.02,
    }
    request.update(overrides)
    return request


@pytest.fixture()
def engine(tmp_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    service = PaperEngine(
        db_path=str(tmp_path / "paper.sqlite3"),
        clock=clock,
        trading_dates=TRADING_DATES,
    )
    service.open()
    service.start()
    service.create_account(ACCOUNT)
    yield service
    service.close()


def feed(service: PaperEngine, quote: dict) -> None:
    service.feed_quote(quote)
    assert service.drain(timeout=5.0)


# -- 开平仓基础场景 -------------------------------------------------------


def test_open_long_short_close_today_and_partial_close(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0))
    opened = engine.submit(ACCOUNT, submit_request("order-open-long", volume=2))
    assert opened["found"] is True
    assert opened["order"]["status"] == "FINISHED"
    assert opened["order"]["filledVolume"] == 2
    assert opened["order"]["isError"] is False
    positions = engine.positions(ACCOUNT)
    assert positions == [{
        "symbol": "SHFE.au2612", "exchangeId": "SHFE", "instrumentId": "au2612",
        "direction": "long", "volume": 2, "todayVolume": 2, "historicalVolume": 0,
        "frozenToday": 0, "frozenHistorical": 0,
        "openPrice": pytest.approx(820.02), "lastPrice": pytest.approx(820.0),
        "margin": pytest.approx(160_000.0),
        "unrealizedPnl": pytest.approx((820.0 - 820.02) * 2 * 1000),
    }]

    # 开空（另一个账户不行，同一账户多空分开记）——用卖出开仓验证。
    feed(engine, au_quote("09:41:00.000000", 821.0))
    short = engine.submit(ACCOUNT, submit_request(
        "order-open-short", direction="SELL", volume=1, limitPrice=820.98))
    assert short["order"]["status"] == "FINISHED"
    by_direction = {item["direction"]: item for item in engine.positions(ACCOUNT)}
    assert by_direction["short"]["volume"] == 1
    assert by_direction["short"]["todayVolume"] == 1

    # 部分平多：持仓 2 手只平 1 手（平今）。
    feed(engine, au_quote("10:00:00.000000", 820.5))
    closed = engine.submit(ACCOUNT, submit_request(
        "order-close-today", direction="SELL", offset="CLOSETODAY", volume=1, limitPrice=820.48))
    assert closed["order"]["status"] == "FINISHED"
    assert by_direction_long(engine)["volume"] == 1
    assert by_direction_long(engine)["todayVolume"] == 1
    assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((820.48 - 820.02) * 1000)


def by_direction_long(service: PaperEngine) -> dict:
    return next(item for item in service.positions(ACCOUNT) if item["direction"] == "long")


def test_close_today_on_non_shfe_exchange_is_rejected_before_dispatch(engine):
    iron = au_quote("09:31:00.000000", 820.0, symbol="DCE.i2609")
    iron.update({
        "price_tick": 0.5, "volume_multiple": 100.0,
        "margin": 60_000.0, "commission": 8.0,
        "product_id": "i", "instrument_name": "铁矿石2609",
    })
    feed(engine, iron)
    with pytest.raises(ValueError, match="close_today_exchange_required"):
        engine.submit(ACCOUNT, submit_request(
            "order-close-dce", symbol="DCE.i2609", offset="CLOSETODAY", limitPrice=820.0))


def test_non_shfe_close_covers_today_and_history(engine):
    iron = au_quote("09:31:00.000000", 820.0, symbol="DCE.i2609")
    iron.update({"price_tick": 0.5, "volume_multiple": 100.0,
                 "margin": 60_000.0, "commission": 8.0, "product_id": "i"})
    feed(engine, iron)
    opened = engine.submit(ACCOUNT, submit_request(
        "order-dce-open", symbol="DCE.i2609", volume=2, limitPrice=820.5))
    assert opened["order"]["status"] == "FINISHED"
    feed(engine, {**iron, "datetime": "2026-11-02 10:00:00.000000",
                  "last_price": 819.5, "ask_price1": 820.0, "bid_price1": 819.0})
    closed = engine.submit(ACCOUNT, submit_request(
        "order-dce-close", symbol="DCE.i2609", direction="SELL",
        offset="CLOSE", volume=2, limitPrice=819.0))
    assert closed["order"]["status"] == "FINISHED"
    assert engine.positions(ACCOUNT) == []


# -- 拒单场景 -------------------------------------------------------------


def rejected(service: PaperEngine, request: dict) -> dict:
    report = service.submit(ACCOUNT, request)
    assert report["found"] is True
    order = report["order"]
    assert order["status"] == "FINISHED"
    assert order["isError"] is True
    assert order["volumeLeft"] == order["volume"]
    return order


def test_insufficient_funds_is_rejected_as_error_order(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0, margin=900_000.0))
    order = rejected(engine, submit_request("order-no-funds", volume=2))
    assert "资金不足" in order["lastMsg"]
    assert engine.positions(ACCOUNT) == []


def test_nan_margin_is_rejected_by_the_engine_not_poisoning_the_book(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0, margin=float("nan")))
    order = rejected(engine, submit_request("order-nan-margin"))
    assert "保证金" in order["lastMsg"] or "margin" in order["lastMsg"]
    account = engine.account(ACCOUNT)
    assert math.isfinite(account["equity"]) and account["equity"] == 1_000_000.0


def test_option_symbol_is_rejected(engine):
    option = au_quote("09:31:00.000000", 820.0)
    option["ins_class"] = "OPTION"
    feed(engine, option)
    order = rejected(engine, submit_request("order-option"))
    assert "期权" in order["lastMsg"] or "不支持" in order["lastMsg"]


def test_price_outside_limit_band_is_rejected(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0))
    rejected(engine, submit_request("order-out-of-band", limitPrice=940.0))
    rejected(engine, submit_request("order-below-band", limitPrice=700.0))


def test_price_off_tick_is_rejected(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0))
    rejected(engine, submit_request("order-off-tick", limitPrice=820.03))


def test_opening_into_delivery_month_is_rejected_but_close_is_allowed(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0, delivery=(2026, 11)))
    order = rejected(engine, submit_request("order-delivery-open"))
    assert "交割月" in order["lastMsg"]
    # 平仓不受个人账户交割月限制：先用非交割月行情建仓，再让行情进入交割月。
    feed(engine, au_quote("09:41:00.000000", 820.0, delivery=(2026, 12)))
    opened = engine.submit(ACCOUNT, submit_request("order-delivery-pre-open"))
    assert opened["order"]["status"] == "FINISHED"
    feed(engine, au_quote("09:51:00.000000", 820.0, delivery=(2026, 11)))
    closed = engine.submit(ACCOUNT, submit_request(
        "order-delivery-close", direction="SELL", offset="CLOSETODAY", limitPrice=819.98))
    assert closed["order"]["status"] == "FINISHED"
    assert closed["order"]["isError"] is False


def future_quote(symbol: str, last: float, *, tick: float, multiple: float,
                 margin: float, delivery: tuple[int, int] = (2026, 11)) -> dict:
    """交割月规则测试用的通用期货行情：默认交割月就是当前交易日所在月（2026-11）。"""
    quote = au_quote("09:31:00.000000", last, delivery=delivery, symbol=symbol)
    quote.update({
        "product_id": symbol.split(".", 1)[-1].rstrip("0123456789"),
        "price_tick": tick,
        "volume_multiple": multiple,
        "margin": margin,
        "commission": 10.0,
        "ask_price1": last + tick,
        "bid_price1": last - tick,
        "upper_limit": last * 1.2,
        "lower_limit": last * 0.8,
    })
    return quote


def test_cffex_index_futures_open_normally_in_delivery_month(engine):
    # 中金所股指期货现金交割，自然人可正常交易交割月（当月）合约（10-09 IF2610
    # 被误拒的根因）。对手价 4231.2 比限价优，按对手价成交。
    feed(engine, future_quote("CFFEX.IF2610", 4231.0, tick=0.2, multiple=300.0,
                              margin=380_000.0))
    opened = engine.submit(ACCOUNT, submit_request(
        "order-if-open", symbol="CFFEX.IF2610", limitPrice=4231.4))
    assert opened["order"]["status"] == "FINISHED"
    assert opened["order"]["isError"] is False
    assert opened["order"]["filledVolume"] == 1
    assert by_direction_long(engine)["openPrice"] == pytest.approx(4231.2)


@pytest.mark.parametrize("symbol", [
    "CFFEX.T2611", "CFFEX.TF2611", "CFFEX.TS2611", "CFFEX.TL2611",
    "DCE.m2611", "CZCE.TA611", "GFEX.si2611", "INE.sc2611",
])
def test_physical_delivery_products_still_reject_delivery_month_opens(engine, symbol):
    # 商品期货自然人不得持仓进入交割月；中金所国债期货自然人须先申报国债托管
    # 账户（本站模拟不建模申报），与商品期货同口径禁开。
    feed(engine, future_quote(symbol, 900.0, tick=1.0, multiple=10.0, margin=9_000.0))
    order = rejected(engine, submit_request(
        f"order-dm-{symbol.split('.', 1)[1]}", symbol=symbol, limitPrice=900.0 + 1.0))
    assert "交割月" in order["lastMsg"]


def test_open_price_tracks_actual_fills_across_settlement_and_mixed_closes(engine):
    # 10-09 实测 rb2701：昨仓结算后与今仓混开混平，剩 1 手今仓的开仓价显示成
    # 昨日结算价。开仓均价必须等于剩余持仓实际成交的加权均价；结算只改持仓价。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-mixed-open", volume=2))  # 2×820.02
    feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    position = by_direction_long(engine)
    assert position["historicalVolume"] == 2
    assert position["openPrice"] == pytest.approx(820.02)  # 开仓价不因结算改变
    raw = engine._accounts[ACCOUNT].book._sim._positions["SHFE.au2612"]
    assert raw["position_price_long"] == pytest.approx(825.0)  # 持仓价标到结算价
    # 当日开今仓 1 手（限价贴 ask 826.02 立即成交），再平昨 1 手：
    # 开仓均价＝昨仓实际开仓价与今仓成交价的加权。
    engine.submit(ACCOUNT, submit_request("order-mixed-today", limitPrice=826.02))
    assert by_direction_long(engine)["openPrice"] == pytest.approx((820.02 * 2 + 826.02) / 3)
    feed(engine, au_quote("2026-11-03 10:00:00.000000", 827.0))
    engine.submit(ACCOUNT, submit_request(
        "order-mixed-close-his", direction="SELL", offset="CLOSE", volume=1, limitPrice=826.98))
    position = by_direction_long(engine)
    assert (position["todayVolume"], position["historicalVolume"]) == (1, 1)
    assert position["openPrice"] == pytest.approx((820.02 + 826.02) / 2)
    # 平掉最后的昨仓后，剩下纯今仓的开仓均价＝它自己的实际成交价。
    feed(engine, au_quote("2026-11-03 10:30:00.000000", 827.5))
    engine.submit(ACCOUNT, submit_request(
        "order-mixed-close-his-2", direction="SELL", offset="CLOSE", volume=1, limitPrice=827.48))
    position = by_direction_long(engine)
    assert position["volume"] == 1
    assert position["todayVolume"] == 1
    assert position["openPrice"] == pytest.approx(826.02)


def test_legacy_book_without_buckets_initializes_without_open_price_jump(engine):
    # 旧版本快照只有混合 open_cost、没有分桶键：恢复后第一笔成交按成交前的
    # 昨/今手数摊开初始化，开仓均价不跳变、昨仓桶拿到完整的旧成本。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-legacy-open", volume=2))  # 2×820.02
    feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    raw = engine._accounts[ACCOUNT].book._sim._positions["SHFE.au2612"]
    blended = raw["open_cost_long"]
    for key in ("open_cost_long_today", "open_cost_long_his"):
        raw.pop(key, None)
    engine.submit(ACCOUNT, submit_request("order-legacy-today", limitPrice=826.02))
    position = by_direction_long(engine)
    assert position["openPrice"] == pytest.approx((820.02 * 2 + 826.02) / 3)
    assert raw["open_cost_long_his"] == pytest.approx(blended)
    assert raw["open_cost_long_today"] == pytest.approx(826.02 * 1000)
    # 无桶旧快照保持混合成本口径：浮盈与混合开仓价一致，不因惰性初始化跳变。
    assert position["unrealizedPnl"] == pytest.approx(
        (826.0 - (820.02 * 2 + 826.02) / 3) * 3 * 1000)
    assert engine.account(ACCOUNT)["floatProfit"] == pytest.approx(
        (826.0 - (820.02 * 2 + 826.02) / 3) * 3 * 1000)


# -- #666：分桶开仓成本与浮盈派生量同步 ------------------------------------


def test_bucketed_float_profit_after_closing_history_lots_issue_666_repro(engine):
    # issue 复现数字：昨 2 手 @820.02、结算 825、今开 1 手 @826.02、依次平掉两笔
    # 昨仓后剩 1 手今仓，估值价 827.50。分桶只重写 open_cost 时浮盈停在旧混合
    # 口径 5480，且行情与重启都纠不回；必须等于 (827.50 − 826.02) × 1000 = 1480。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-float-open", volume=2))  # 2×820.02
    feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    engine.submit(ACCOUNT, submit_request("order-float-today", limitPrice=826.02))
    feed(engine, au_quote("2026-11-03 09:41:00.000000", 827.0))
    engine.submit(ACCOUNT, submit_request(
        "order-float-close-his", direction="SELL", offset="CLOSE", volume=1, limitPrice=826.98))
    feed(engine, au_quote("2026-11-03 09:51:00.000000", 827.5))
    engine.submit(ACCOUNT, submit_request(
        "order-float-close-his-2", direction="SELL", offset="CLOSE", volume=1, limitPrice=827.48))
    position = by_direction_long(engine)
    assert (position["volume"], position["todayVolume"]) == (1, 1)
    assert position["openPrice"] == pytest.approx(826.02)
    assert position["unrealizedPnl"] == pytest.approx((827.50 - 826.02) * 1000)  # 1480
    account = engine.account(ACCOUNT)
    assert account["floatProfit"] == pytest.approx(1480.0)  # 账户汇总＝各持仓之和
    # 权益口径不受影响（浮盈不进 balance/available）：权益 = 静态 1_009_920
    # ＋当日持仓盈亏 1_480 ＋平仓盈亏 4_460 −手续费 60。平昨按昨结算价 825：
    # (826.98 − 825) + (827.48 − 825) = 1_980 + 2_480（不是混合持仓价的 3_780）。
    assert account["equity"] == pytest.approx(1_015_800.0)
    assert account["available"] == pytest.approx(935_800.0)
    assert account["margin"] == pytest.approx(80_000.0)
    assert account["commission"] == pytest.approx(60.0)  # 当日 3 笔 × 20
    assert account["closeProfit"] == pytest.approx(1_980.0 + 2_480.0)
    assert account["positionProfit"] == pytest.approx(1_480.0)
    # 行情到 828：以 last_price 为基准只加差额，不重复计入也不漏计。
    feed(engine, au_quote("2026-11-03 10:00:00.000000", 828.0))
    assert by_direction_long(engine)["unrealizedPnl"] == pytest.approx((828.0 - 826.02) * 1000)
    account = engine.account(ACCOUNT)
    assert account["floatProfit"] == pytest.approx(1_980.0)
    assert account["equity"] == pytest.approx(1_016_300.0)  # 剩 1 手，只加 500


@pytest.mark.parametrize(("close_offset", "remaining_open_price", "close_profit",
                          "position_profit"), [
    # 平昨剩（昨 819.98 + 今 825.98）／2；平今剩昨 819.98 × 2。
    # 平仓盈亏按逐日盯市：平昨对昨结算价 825，平今对今仓开仓价 825.98；
    # 持仓盈亏＝剩余各桶相对各自成本（昨仓 825、今仓 825.98）按 827.5 估值。
    ("CLOSE", (819.98 + 825.98) / 2, (825.0 - 827.02) * 1000,
     (825.0 - 827.5) * 1000 + (825.98 - 827.5) * 1000),
    ("CLOSETODAY", 819.98, (825.98 - 827.02) * 1000, (825.0 - 827.5) * 2 * 1000),
])
def test_bucketed_float_profit_short_side_close_today_and_history(engine, close_offset,
                                                                  remaining_open_price,
                                                                  close_profit,
                                                                  position_profit):
    # 空头矩阵：昨 2 手 @819.98（==bid 按自身价成交）、结算 825、今开 1 手
    # @825.98、平 1 手。空头浮盈 =（开仓价 − 估值价）× 手数 × 乘数（反号）。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request(
        "order-short-open", direction="SELL", volume=2, limitPrice=819.98))
    feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    engine.submit(ACCOUNT, submit_request(
        "order-short-today", direction="SELL", limitPrice=825.98))
    feed(engine, au_quote("2026-11-03 09:41:00.000000", 827.0))
    closed = engine.submit(ACCOUNT, submit_request(
        "order-short-close", direction="BUY", offset=close_offset, limitPrice=827.02))
    assert closed["order"]["isError"] is False
    position = next(item for item in engine.positions(ACCOUNT)
                    if item["direction"] == "short")
    assert position["openPrice"] == pytest.approx(remaining_open_price)
    assert position["unrealizedPnl"] == pytest.approx((remaining_open_price - 827.0) * 2 * 1000)
    assert engine.account(ACCOUNT)["floatProfit"] == pytest.approx(
        (remaining_open_price - 827.0) * 2 * 1000)
    # 行情连续更新：每一步都等于剩余开仓成本口径。
    feed(engine, au_quote("2026-11-03 09:51:00.000000", 827.5))
    position = next(item for item in engine.positions(ACCOUNT)
                    if item["direction"] == "short")
    assert position["unrealizedPnl"] == pytest.approx((remaining_open_price - 827.5) * 2 * 1000)
    account = engine.account(ACCOUNT)
    assert account["floatProfit"] == pytest.approx((remaining_open_price - 827.5) * 2 * 1000)
    assert account["closeProfit"] == pytest.approx(close_profit)
    assert account["positionProfit"] == pytest.approx(position_profit)
    # 权益 = 静态 989_920 ＋当日持仓盈亏 ＋平仓盈亏 −手续费 40，与拆分无关。
    assert account["equity"] == pytest.approx(983_840.0)
    assert account["margin"] == pytest.approx(160_000.0)


def test_non_shfe_close_across_buckets_keeps_float_profit_consistent(engine):
    # 非上期所跨桶：DCE 的 CLOSE 先平昨再平今。昨 2 手 @820.5（==ask 按自身价
    # 成交）、今开 1 手 @818.5 后 CLOSE 2 手把昨仓清空，剩 1 手今仓按自己的
    # 成交价记浮盈。
    iron = au_quote("09:31:00.000000", 820.0, symbol="DCE.i2609")
    iron.update({"price_tick": 0.5, "volume_multiple": 100.0,
                 "margin": 60_000.0, "commission": 8.0, "product_id": "i"})
    feed(engine, iron)
    engine.submit(ACCOUNT, submit_request(
        "order-iron-open", symbol="DCE.i2609", volume=2, limitPrice=820.5))
    feed(engine, iron | {"datetime": "2026-11-02 15:10:00.000000", "last_price": 821.0,
                         "settlement": 822.0, "ask_price1": 821.5, "bid_price1": 820.5})
    feed(engine, iron | {"datetime": "2026-11-03 09:31:00.000000", "last_price": 818.0,
                         "pre_settlement": 822.0, "ask_price1": 818.5, "bid_price1": 817.5})
    engine.submit(ACCOUNT, submit_request(
        "order-iron-today", symbol="DCE.i2609", limitPrice=818.5))
    feed(engine, iron | {"datetime": "2026-11-03 09:41:00.000000", "last_price": 819.0,
                         "ask_price1": 819.5, "bid_price1": 818.5})
    closed = engine.submit(ACCOUNT, submit_request(
        "order-iron-close", symbol="DCE.i2609", direction="SELL",
        offset="CLOSE", volume=2, limitPrice=818.5))
    assert closed["order"]["isError"] is False
    position = by_direction_iron(engine)
    assert (position["volume"], position["todayVolume"]) == (1, 1)
    assert position["openPrice"] == pytest.approx(818.5)
    assert position["unrealizedPnl"] == pytest.approx((819.0 - 818.5) * 1 * 100)
    assert engine.account(ACCOUNT)["floatProfit"] == pytest.approx(50.0)
    # 相同桶均价不产生浮盈口径跳变：今开价与剩余昨仓同价时浮盈仍一致。
    feed(engine, iron | {"datetime": "2026-11-03 09:51:00.000000", "last_price": 818.5,
                         "ask_price1": 819.0, "bid_price1": 818.0})
    assert by_direction_iron(engine)["unrealizedPnl"] == pytest.approx(0.0)


def by_direction_iron(service: PaperEngine) -> dict:
    return next(item for item in service.positions(ACCOUNT)
                if item["symbol"] == "DCE.i2609")


def test_full_close_brings_float_profit_to_exact_zero(engine):
    # 全平归零：昨今混持后全平，持仓清空、浮盈精确为 0（消掉比例扣减的浮点残差）。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-zero-open", volume=2))
    feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    engine.submit(ACCOUNT, submit_request("order-zero-today", limitPrice=826.02))
    feed(engine, au_quote("2026-11-03 09:41:00.000000", 827.0))
    engine.submit(ACCOUNT, submit_request(
        "order-zero-close-his", direction="SELL", offset="CLOSE", volume=2, limitPrice=826.98))
    engine.submit(ACCOUNT, submit_request(
        "order-zero-close-today", direction="SELL", offset="CLOSETODAY", volume=1,
        limitPrice=826.98))
    assert engine.positions(ACCOUNT) == []
    account = engine.account(ACCOUNT)
    assert account["floatProfit"] == pytest.approx(0.0, abs=1e-9)
    assert account["positionCount"] == 0
    assert account["margin"] == 0.0


def test_resting_close_order_fill_keeps_float_profit_consistent(engine):
    # 被动挂单随行情成交：挂高价卖平昨仓，行情上穿后由行情驱动撮合，成交钩子
    # 同样同步浮盈。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-rest-open", volume=2))
    feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    engine.submit(ACCOUNT, submit_request("order-rest-today", limitPrice=826.02))
    resting = engine.submit(ACCOUNT, submit_request(
        "order-rest-close", direction="SELL", offset="CLOSE", volume=1, limitPrice=828.04))
    assert resting["order"]["status"] == "ALIVE"  # 高于买一，先挂住
    feed(engine, au_quote("2026-11-03 09:41:00.000000", 828.1))
    position = by_direction_long(engine)
    assert (position["volume"], position["todayVolume"]) == (2, 1)
    assert position["openPrice"] == pytest.approx((820.02 + 826.02) / 2)
    assert position["unrealizedPnl"] == pytest.approx(
        (828.1 - (820.02 + 826.02) / 2) * 2 * 1000)
    assert engine.account(ACCOUNT)["floatProfit"] == pytest.approx(
        (828.1 - (820.02 + 826.02) / 2) * 2 * 1000)


# -- 平仓盈亏 / 持仓盈亏按逐日盯市拆分（2026-10-12 夜盘缺陷） ------------------


def _his_and_today_long(service: PaperEngine) -> None:
    """昨 1 手（11-02 开 @820.02，结算价 830）+ 11-03 今开 1 手 @826.02，估值 826.14。
    结算价高于今仓成交价，与 10-12 夜盘 au（昨结 907.78、今开 902.4x）同形。"""
    feed(service, au_quote("09:31:00.000000", 820.0))
    service.submit(ACCOUNT, submit_request("order-mtm-his"))  # 820.02
    feed(service, au_quote("15:10:00.000000", 829.5, settlement=830.0))
    feed(service, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=830.0))
    service.submit(ACCOUNT, submit_request("order-mtm-today", limitPrice=826.02))
    feed(service, au_quote("2026-11-03 09:41:00.000000", 826.14))  # 买一 826.12


@pytest.mark.parametrize(("offset", "close_profit", "position_profit", "remaining_price"), [
    # 黄金例子：平今 @826.12 实赚 (826.12 − 826.02) × 1000 = +100（混合持仓价
    # (830 + 826.02)/2 会算成 −1_890）；剩昨仓持仓价仍是结算价 830。
    ("CLOSETODAY", 100.0, (826.14 - 830.0) * 1000, 830.0),
    # 上期所平昨：对昨结算价 830，剩今仓持仓价＝今仓成交价 826.02。
    ("CLOSE", (826.12 - 830.0) * 1000, (826.14 - 826.02) * 1000, 826.02),
])
def test_shfe_close_profit_follows_the_bucket_actually_closed(engine, offset, close_profit,
                                                             position_profit, remaining_price):
    _his_and_today_long(engine)
    before = engine.account(ACCOUNT)
    closed = engine.submit(ACCOUNT, submit_request(
        f"order-mtm-{offset.lower()}", direction="SELL", offset=offset, limitPrice=826.12))
    assert closed["order"]["isError"] is False
    account = engine.account(ACCOUNT)
    assert account["closeProfit"] == pytest.approx(close_profit)
    assert account["positionProfit"] == pytest.approx(position_profit)
    raw = engine._accounts[ACCOUNT].book._sim._positions["SHFE.au2612"]
    assert raw["position_price_long"] == pytest.approx(remaining_price)
    # 权益只随手续费变：静态 1_009_960 − 手续费 40 ＋盈亏合计，两种指令一致；
    # 独立核算 = 初始 100 万 ＋昨仓 820.02→826.14 的 6_120 ＋今仓 +100 − 手续费 60。
    assert account["equity"] == pytest.approx(1_006_160.0)
    assert account["equity"] == pytest.approx(before["equity"] - 20.0 - 20.0)
    assert account["staticBalance"] == pytest.approx(1_009_960.0)
    assert account["equity"] == pytest.approx(
        account["staticBalance"] + account["closeProfit"] + account["positionProfit"]
        - account["commission"], abs=0.01)
    # 行情继续走：持仓盈亏只按剩余那桶的成本跟价。
    feed(engine, au_quote("2026-11-03 09:51:00.000000", 827.0))
    assert engine.account(ACCOUNT)["positionProfit"] == pytest.approx(
        (827.0 - remaining_price) * 1000)


def test_legacy_position_without_cost_buckets_closes_today_at_its_fill_price(engine):
    # 上线前的持仓没有持仓成本分桶键：惰性初始化取今仓开仓成本作今仓持仓
    # 成本（二者恒等），昨仓桶取其余，平今仍按今仓成交价算平仓盈亏。
    _his_and_today_long(engine)
    raw = engine._accounts[ACCOUNT].book._sim._positions["SHFE.au2612"]
    for key in ("position_cost_long_today", "position_cost_long_his"):
        raw.pop(key, None)
    engine.submit(ACCOUNT, submit_request(
        "order-mtm-legacy", direction="SELL", offset="CLOSETODAY", limitPrice=826.12))
    assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx(100.0)
    assert raw["position_price_long"] == pytest.approx(830.0)


def test_non_shfe_mixed_close_takes_history_first_at_settlement_price(engine):
    # 大商所 CLOSE 先平昨后平今（与 SimTrade 实际扣减一致）。昨 1 手 @820.5、
    # 结算价 822；今开 2 手 @818.5；CLOSE 2 手 @818.5 = 平昨 1（对 822）+ 平今 1
    # （对 818.5）：平仓盈亏 −350（混合持仓价 819.67 会算成 −233.33）。
    iron = au_quote("09:31:00.000000", 820.0, symbol="DCE.i2609")
    iron.update({"price_tick": 0.5, "volume_multiple": 100.0, "ask_price1": 820.5,
                 "bid_price1": 819.5, "margin": 60_000.0, "commission": 8.0,
                 "product_id": "i"})
    feed(engine, iron)
    engine.submit(ACCOUNT, submit_request(
        "order-mix-his", symbol="DCE.i2609", limitPrice=820.5))
    feed(engine, iron | {"datetime": "2026-11-02 15:10:00.000000", "last_price": 821.0,
                         "settlement": 822.0, "ask_price1": 821.5, "bid_price1": 820.5})
    feed(engine, iron | {"datetime": "2026-11-03 09:31:00.000000", "last_price": 818.0,
                         "pre_settlement": 822.0, "ask_price1": 818.5, "bid_price1": 817.5})
    engine.submit(ACCOUNT, submit_request(
        "order-mix-today", symbol="DCE.i2609", volume=2, limitPrice=818.5))
    feed(engine, iron | {"datetime": "2026-11-03 09:41:00.000000", "last_price": 819.0,
                         "ask_price1": 819.5, "bid_price1": 818.5})
    closed = engine.submit(ACCOUNT, submit_request(
        "order-mix-close", symbol="DCE.i2609", direction="SELL", offset="CLOSE",
        volume=2, limitPrice=818.5))
    assert closed["order"]["isError"] is False
    position = by_direction_iron(engine)
    assert (position["volume"], position["todayVolume"], position["historicalVolume"]) == (1, 1, 0)
    account = engine.account(ACCOUNT)
    assert account["closeProfit"] == pytest.approx((818.5 - 822.0) * 100 + 0.0)
    assert account["positionProfit"] == pytest.approx((819.0 - 818.5) * 100)
    # 独立核算：昨仓 820.5→818.5 −200，今仓平掉 0，剩 1 手 818.5→819 +50，手续费 8+32。
    assert account["equity"] == pytest.approx(1_000_000.0 - 200.0 + 50.0 - 40.0)
    assert account["equity"] == pytest.approx(
        account["staticBalance"] + account["closeProfit"] + account["positionProfit"]
        - account["commission"], abs=0.01)


def test_settlement_resets_both_cost_buckets_to_the_settlement_price(engine):
    # 昨 830 + 今 826.02 混持过日：结算后两手都是昨仓、持仓价＝新结算价 828，
    # 分桶键清掉；次日平昨两笔都对 828 算平仓盈亏。
    _his_and_today_long(engine)
    feed(engine, au_quote("2026-11-03 15:10:00.000000", 827.5, settlement=828.0))
    feed(engine, au_quote("2026-11-04 09:31:00.000000", 829.0, pre_settlement=828.0))
    raw = engine._accounts[ACCOUNT].book._sim._positions["SHFE.au2612"]
    assert raw["position_price_long"] == pytest.approx(828.0)
    assert "position_cost_long_today" not in raw and "position_cost_long_his" not in raw
    position = by_direction_long(engine)
    assert (position["historicalVolume"], position["todayVolume"]) == (2, 0)
    assert position["openPrice"] == pytest.approx((820.02 + 826.02) / 2)
    engine.submit(ACCOUNT, submit_request(
        "order-reset-close-1", direction="SELL", offset="CLOSE", limitPrice=828.98))
    raw = engine._accounts[ACCOUNT].book._sim._positions["SHFE.au2612"]
    assert raw["position_price_long"] == pytest.approx(828.0)
    engine.submit(ACCOUNT, submit_request(
        "order-reset-close-2", direction="SELL", offset="CLOSE", limitPrice=828.98))
    account = engine.account(ACCOUNT)
    assert engine.positions(ACCOUNT) == []
    assert account["closeProfit"] == pytest.approx((828.98 - 828.0) * 2 * 1000)
    assert account["positionProfit"] == pytest.approx(0.0, abs=1e-6)


# -- 持仓明细先开先平（#932，2026-10-17，与期货公司 CTP 逐笔明细同口径） -----------


def test_closing_todays_lots_leaves_yesterdays_open_price_untouched_issue_932(engine):
    # #932 实测序列：昨 1 手 + 今开 5 手 + 平今 5 手 → 剩下那 1 手昨仓的开仓均价不变
    # （快期的混合扣减会把今仓成交价留进剩余昨仓：895.97 → 897.31）。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-932-his"))  # 820.02
    feed(engine, au_quote("15:10:00.000000", 829.5, settlement=830.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=830.0))
    engine.submit(ACCOUNT, submit_request("order-932-open", volume=5, limitPrice=826.02))
    assert by_direction_long(engine)["openPrice"] == pytest.approx((820.02 + 5 * 826.02) / 6)
    feed(engine, au_quote("2026-11-03 09:41:00.000000", 826.14))  # 买一 826.12
    closed = engine.submit(ACCOUNT, submit_request(
        "order-932-close", direction="SELL", offset="CLOSETODAY", volume=5, limitPrice=826.12))
    assert closed["order"]["isError"] is False
    position = by_direction_long(engine)
    assert (position["volume"], position["todayVolume"], position["historicalVolume"]) == (1, 0, 1)
    assert position["openPrice"] == pytest.approx(820.02)
    raw = engine._accounts[ACCOUNT].book._sim._positions["SHFE.au2612"]
    assert raw["position_price_long"] == pytest.approx(830.0)
    assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((826.12 - 826.02) * 5 * 1000)


def _his_and_two_today_lots(service: PaperEngine) -> None:
    """昨 1 手 @820.02（结算价 830）+ 今仓两笔 @826.02、@827.02，估值 826.14。"""
    feed(service, au_quote("09:31:00.000000", 820.0))
    service.submit(ACCOUNT, submit_request("order-fifo-his"))  # 820.02
    feed(service, au_quote("15:10:00.000000", 829.5, settlement=830.0))
    feed(service, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=830.0))
    service.submit(ACCOUNT, submit_request("order-fifo-today-1", limitPrice=826.02))
    feed(service, au_quote("2026-11-03 09:35:00.000000", 827.0))
    service.submit(ACCOUNT, submit_request("order-fifo-today-2", limitPrice=827.02))
    feed(service, au_quote("2026-11-03 09:41:00.000000", 826.14))  # 买一 826.12


@pytest.mark.parametrize(("offset", "remaining_open", "close_profit", "remaining_position_price"), [
    # 上期所平今：今仓明细先开先平，平掉 826.02 那笔（桶内均价会扣 826.52、剩 826.52）。
    ("CLOSETODAY", (820.02 + 827.02) / 2, (826.12 - 826.02) * 1000, (830.0 + 827.02) / 2),
    # 上期所平昨：对昨结算价 830，今仓两笔原样保留。
    ("CLOSE", (826.02 + 827.02) / 2, (826.12 - 830.0) * 1000, (826.02 + 827.02) / 2),
])
def test_shfe_close_takes_the_earliest_lot_of_the_bucket_it_closes(engine, offset, remaining_open,
                                                                   close_profit,
                                                                   remaining_position_price):
    _his_and_two_today_lots(engine)
    closed = engine.submit(ACCOUNT, submit_request(
        f"order-fifo-{offset.lower()}", direction="SELL", offset=offset, limitPrice=826.12))
    assert closed["order"]["isError"] is False
    position = by_direction_long(engine)
    assert position["openPrice"] == pytest.approx(remaining_open)
    assert position["unrealizedPnl"] == pytest.approx((826.14 - remaining_open) * 2 * 1000)
    raw = engine._accounts[ACCOUNT].book._sim._positions["SHFE.au2612"]
    assert raw["position_price_long"] == pytest.approx(remaining_position_price)
    account = engine.account(ACCOUNT)
    assert account["closeProfit"] == pytest.approx(close_profit)
    # 逐日盯市合计与平哪一笔无关：平今 +100 −3_860 −880；平昨 −3_880 +120 −880。
    assert account["closeProfit"] + account["positionProfit"] == pytest.approx(-4_640.0)
    assert account["equity"] == pytest.approx(
        account["staticBalance"] + account["closeProfit"] + account["positionProfit"]
        - account["commission"], abs=0.01)


def test_non_shfe_close_takes_history_then_the_earliest_today_lot(engine):
    # 大商所 CLOSE 先平昨、再按今仓明细先开先平。昨 1 手 @820.5（结算价 822）；
    # 今开 @818.5、@819.5 各 1 手；CLOSE 2 @818.5 = 平昨 1（对 822）+ 平今 818.5 那笔。
    iron = au_quote("09:31:00.000000", 820.0, symbol="DCE.i2609")
    iron.update({"price_tick": 0.5, "volume_multiple": 100.0, "ask_price1": 820.5,
                 "bid_price1": 819.5, "margin": 60_000.0, "commission": 8.0,
                 "product_id": "i"})
    feed(engine, iron)
    engine.submit(ACCOUNT, submit_request("order-fifo-iron-his", symbol="DCE.i2609",
                                          limitPrice=820.5))
    feed(engine, iron | {"datetime": "2026-11-02 15:10:00.000000", "last_price": 821.0,
                         "settlement": 822.0, "ask_price1": 821.5, "bid_price1": 820.5})
    feed(engine, iron | {"datetime": "2026-11-03 09:31:00.000000", "last_price": 818.0,
                         "pre_settlement": 822.0, "ask_price1": 818.5, "bid_price1": 817.5})
    engine.submit(ACCOUNT, submit_request("order-fifo-iron-1", symbol="DCE.i2609",
                                          limitPrice=818.5))
    feed(engine, iron | {"datetime": "2026-11-03 09:35:00.000000", "last_price": 819.0,
                         "ask_price1": 819.5, "bid_price1": 818.5})
    engine.submit(ACCOUNT, submit_request("order-fifo-iron-2", symbol="DCE.i2609",
                                          limitPrice=819.5))
    closed = engine.submit(ACCOUNT, submit_request(
        "order-fifo-iron-close", symbol="DCE.i2609", direction="SELL", offset="CLOSE",
        volume=2, limitPrice=818.5))
    assert closed["order"]["isError"] is False
    position = by_direction_iron(engine)
    assert (position["volume"], position["todayVolume"], position["historicalVolume"]) == (1, 1, 0)
    assert position["openPrice"] == pytest.approx(819.5)  # 桶内均价会剩 819.0
    account = engine.account(ACCOUNT)
    assert account["closeProfit"] == pytest.approx((818.5 - 822.0) * 100 + (818.5 - 818.5) * 100)
    assert account["positionProfit"] == pytest.approx((819.0 - 819.5) * 100)


def test_settlement_keeps_lot_order_so_the_older_day_closes_first(engine):
    # 结算翻桶：今仓明细按原顺序接到昨仓之后。11-02 开 @820.02、11-03 开 @826.02，
    # 11-04 两笔都是昨仓（持仓价＝结算价 828）；平 1 手平掉 11-02 那笔，剩 826.02
    # （桶内均价会剩 823.02）；平仓盈亏仍对昨结算价。明细随快照 JSON 往返不变。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-carry-1"))  # 820.02
    feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    engine.submit(ACCOUNT, submit_request("order-carry-2", limitPrice=826.02))
    feed(engine, au_quote("2026-11-03 15:10:00.000000", 827.5, settlement=828.0))
    feed(engine, au_quote("2026-11-04 09:31:00.000000", 829.0, pre_settlement=828.0))
    book = engine._accounts[ACCOUNT].book
    raw = book._sim._positions["SHFE.au2612"]
    assert raw["open_lots_long_his"] == [[pytest.approx(820.02), 1], [pytest.approx(826.02), 1]]
    assert raw.get("open_lots_long_today") is None
    assert by_direction_long(engine)["openPrice"] == pytest.approx((820.02 + 826.02) / 2)
    restored = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=engine._clock,
                         trading_dates=TRADING_DATES)
    restored.restore_state(json.loads(json.dumps(book.export_state())))
    assert restored._sim._positions["SHFE.au2612"]["open_lots_long_his"] == raw["open_lots_long_his"]
    engine.submit(ACCOUNT, submit_request(
        "order-carry-close", direction="SELL", offset="CLOSE", limitPrice=828.98))
    assert by_direction_long(engine)["openPrice"] == pytest.approx(826.02)
    assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((828.98 - 828.0) * 1000)


@pytest.mark.parametrize("snapshot", ["before-lots", "lots-stale-after-rollback"])
def test_snapshot_without_matching_lots_restores_one_lot_per_bucket(engine, snapshot):
    # 2026-10-09 ~ 10-17 的快照只有桶成本（open_cost_long_today/_his），没有明细；
    # 回滚到旧版本再升级回来时，明细可能落后于旧代码改过的桶成本。两种都按桶重建
    # 一条明细（价＝桶均价、量＝桶手数）：恢复后数值不变，之后的成交先开先平。
    _his_and_two_today_lots(engine)
    book = engine._accounts[ACCOUNT].book
    raw = book._sim._positions["SHFE.au2612"]
    before = engine.positions(ACCOUNT)
    state = json.loads(json.dumps(book.export_state()))
    stored = state["_positions"]["SHFE.au2612"]
    if snapshot == "before-lots":
        for key in ("open_lots_long_today", "open_lots_long_his"):
            stored.pop(key)
    else:
        stored["open_lots_long_today"] = [[800.0, 2]]  # 手数对、成本与桶对不上
    restored = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=engine._clock,
                         trading_dates=TRADING_DATES)
    restored.restore_state(state)
    assert restored.positions() == [pytest.approx(row) for row in before]
    assert restored.sanity()
    # 引擎里同样换成这份快照的持仓，再平今 1 手：今仓按一条 826.52 的明细扣减。
    book._sim._positions["SHFE.au2612"] = dict(stored)
    engine.submit(ACCOUNT, submit_request(
        f"order-legacy-lots-{snapshot}", direction="SELL", offset="CLOSETODAY",
        limitPrice=826.12))
    raw = book._sim._positions["SHFE.au2612"]
    assert raw["open_lots_long_today"] == [[pytest.approx(826.52), 1]]
    assert raw["open_lots_long_his"] == [[pytest.approx(820.02), 1]]
    assert by_direction_long(engine)["openPrice"] == pytest.approx((820.02 + 826.52) / 2)
    assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((826.12 - 826.52) * 1000)


# 回滚前（明细之前）的版本 export_state 只写这些顶层键（6b71401 及更早各版逐一核过：
# 都按固定键重建顶层 dict），不认识的顶层键一律丢掉；持仓 dict 原样带回。
PRE_LOTS_EXPORT_KEYS = ("version", "init_balance", "book_trading_day", "_account", "_positions",
                        "_orders", "quotes", "_max_datetime", "fill_consumed")


def _old_writer_commit(db_path, *, trades: int, keep_keys: bool = False) -> None:
    """旧写者（明细之前的版本）在回滚期间给这个账户提交一次：当前状态行按旧版本的
    导出重写（顶层只剩固定键，持仓 dict 连同明细原样），revision+1，追加 trades 笔成交行。
    ABA：旧版在今仓桶内平 1 手再按桶均价开 1 手，手数与桶成本复原、明细没动。"""
    db = sqlite3.connect(str(db_path))
    try:
        with db:
            revision, from_seq, head_json = db.execute(
                "SELECT revision, trades_from_seq, head_json FROM paper_account_heads "
                "WHERE account_id=?", (ACCOUNT,)).fetchone()
            head = json.loads(head_json)
            # keep_keys：假想一个原样保留未知顶层键的写者——成交序号对不上同样识别。
            old_head = head if keep_keys else {key: head[key] for key in PRE_LOTS_EXPORT_KEYS if key in head}
            (count, book_day, trade_json) = db.execute(
                "SELECT count(*), max(book_day), max(trade_json) FROM paper_trades "
                "WHERE account_id=? AND seq>=?", (ACCOUNT, from_seq)).fetchone()
            for index in range(trades):
                trade = json.loads(trade_json)
                trade["trade_id"] = trade["exchange_trade_id"] = f"old-writer-{index}"
                db.execute("INSERT INTO paper_trades (account_id, seq, book_day, trading_day, "
                           "trade_json) VALUES (?,?,?,?,?)",
                           (ACCOUNT, from_seq + count + index, book_day, None, json.dumps(trade)))
            db.execute("UPDATE paper_account_heads SET revision=?, head_json=? WHERE account_id=?",
                       (revision + 1, json.dumps(old_head), ACCOUNT))
    finally:
        db.close()


@pytest.mark.parametrize("old_writer", [None, "aba-trades", "no-trades", "keeps-key-adds-trades"])
def test_lots_survive_new_restarts_but_not_an_old_writer_in_between_issue_932(tmp_path,
                                                                              old_writer):
    # #932 B2：新→旧→新。新版本今仓两笔 826.02、827.02（+昨 820.02）；
    # 本版本自己重启（含跨过一次结算、成交序号起点不为 0）明细原样，平今 1 手先平
    # 826.02。中间换旧版本写过（ABA 平了又开、或只写了一次无成交的状态）：明细来源
    # 无法证明，按桶成本重建一条 826.52，平今后剩 826.52——与旧版本写过后按桶均价
    # 继续的口径一致，而不是沿用过期明细平掉 826.02。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    first = build_engine(db, clock=clock)
    try:
        _his_and_two_today_lots(first)
        assert first._accounts[ACCOUNT].trades_from_seq == 1  # 结算推进过序号起点
    finally:
        first.close()
    if old_writer is not None:
        _old_writer_commit(db, trades=0 if old_writer == "no-trades" else 2,
                           keep_keys=old_writer == "keeps-key-adds-trades")
    clock.advance(datetime.fromisoformat("2026-11-03T09:42:00+08:00"))
    restarted = build_engine(db, clock=clock)
    try:
        book = restarted._accounts[ACCOUNT].book
        raw = book._sim._positions["SHFE.au2612"]
        assert book.lots_dropped is (old_writer is not None)
        if old_writer is None:
            assert raw["open_lots_long_today"] == [[pytest.approx(826.02), 1],
                                                   [pytest.approx(827.02), 1]]
        else:
            assert "open_lots_long_today" not in raw and "open_lots_long_his" not in raw
        # 丢明细不动账：开仓均价、桶成本、持仓都与重启前相同。
        assert by_direction_long(restarted)["openPrice"] == pytest.approx(
            (820.02 + 826.02 + 827.02) / 3)
        assert raw["open_cost_long_today"] == pytest.approx((826.02 + 827.02) * 1000)
        feed(restarted, au_quote("2026-11-03 09:42:00.000000", 826.14))  # 买一 826.12
        closed = restarted.submit(ACCOUNT, submit_request(
            "order-b2-close", direction="SELL", offset="CLOSETODAY", limitPrice=826.12))
        assert closed["order"]["isError"] is False
        raw = book._sim._positions["SHFE.au2612"]
        today_left = 827.02 if old_writer is None else 826.52
        assert raw["open_lots_long_today"] == [[pytest.approx(today_left), 1]]
        assert raw["open_lots_long_his"] == [[pytest.approx(820.02), 1]]
        assert by_direction_long(restarted)["openPrice"] == pytest.approx((820.02 + today_left) / 2)
        # 平今盈亏按被平那笔的开仓价（逐日盯市平今口径）。
        assert restarted.account(ACCOUNT)["closeProfit"] == pytest.approx(
            (826.12 - (826.02 if old_writer is None else 826.52)) * 1000)
    finally:
        restarted.close()
    # 新快照旧版本照常读：版本号不变、桶成本镜像继续写且等于明细之和、写者证明只在
    # 顶层（旧版本丢掉它），持仓 dict 里没有。
    db_conn = sqlite3.connect(str(db))
    try:
        (head_json,) = db_conn.execute("SELECT head_json FROM paper_account_heads "
                                       "WHERE account_id=?", (ACCOUNT,)).fetchone()
    finally:
        db_conn.close()
    head = json.loads(head_json)
    assert head["version"] == BASE_STATE_VERSION
    assert head[LOTS_TRADE_SEQ_KEY]["from"] == 1  # 已提交的序号起点（跨过一次结算）
    stored = head["_positions"]["SHFE.au2612"]
    assert LOTS_TRADE_SEQ_KEY not in stored
    for bucket in ("today", "his"):
        assert stored[f"open_cost_long_{bucket}"] == pytest.approx(
            sum(price * volume for price, volume in stored[f"open_lots_long_{bucket}"]) * 1000)


def test_bucketed_float_profit_survives_reopen_and_double_restore(tmp_path):
    # SQLite 重开：快照带着修正后的浮盈落盘，重开同价位行情回读不变；同一份
    # 快照重复恢复结果幂等；修复前落盘的失真快照（浮盈 5480）在恢复时被
    # 按保存的桶成本、数量和估值价纠正回 1480。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-reopen-open", volume=2))
        feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
        feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
        engine.submit(ACCOUNT, submit_request("order-reopen-today", limitPrice=826.02))
        feed(engine, au_quote("2026-11-03 09:41:00.000000", 827.0))
        engine.submit(ACCOUNT, submit_request(
            "order-reopen-close-his", direction="SELL", offset="CLOSE", volume=1,
            limitPrice=826.98))
        feed(engine, au_quote("2026-11-03 09:51:00.000000", 827.5))
        engine.submit(ACCOUNT, submit_request(
            "order-reopen-close-his-2", direction="SELL", offset="CLOSE", volume=1,
            limitPrice=827.48))
        state = engine._accounts[ACCOUNT].book.export_state()
    finally:
        engine.close()
    clock.advance(datetime.fromisoformat("2026-11-03T10:00:00+08:00"))
    restarted = build_engine(db, clock=clock)
    try:
        feed(restarted, au_quote("2026-11-03 10:01:00.000000", 827.5))
        assert by_direction_long(restarted)["unrealizedPnl"] == pytest.approx(1480.0)
        assert restarted.account(ACCOUNT)["floatProfit"] == pytest.approx(1480.0)
        # 行情推进后仍只加差额。
        feed(restarted, au_quote("2026-11-03 10:02:00.000000", 828.0))
        assert restarted.account(ACCOUNT)["floatProfit"] == pytest.approx(1980.0)
    finally:
        restarted.close()
    # 重复恢复幂等 + 修复前的失真快照被纠正。
    clock_now = VirtualClock(datetime.fromisoformat("2026-11-03T10:05:00+08:00"))
    for _ in range(2):
        book = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0,
                         clock=clock_now, trading_dates=TRADING_DATES)
        book.restore_state(state)
        position = book._sim._positions["SHFE.au2612"]
        assert position["float_profit_long"] == pytest.approx(1480.0)
        assert book._sim._account["float_profit"] == pytest.approx(1480.0)
    buggy = json.loads(json.dumps(state))
    buggy["_positions"]["SHFE.au2612"]["float_profit_long"] = 5480.0  # #666 失真值
    buggy["_positions"]["SHFE.au2612"]["float_profit"] = 5480.0
    buggy["_account"]["float_profit"] = 5480.0
    book = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0,
                     clock=clock_now, trading_dates=TRADING_DATES)
    book.restore_state(buggy)
    assert book._sim._account["float_profit"] == pytest.approx(1480.0)
    # 权益等其余字段照快照保留，不因纠正浮盈被改写。
    assert book._sim._account["balance"] == pytest.approx(1_015_800.0)


def test_expired_contract_is_rejected(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0, expire_ns=1))
    order = rejected(engine, submit_request("order-expired"))
    assert "到期" in order["lastMsg"] or "过期" in order["lastMsg"]


# -- 非交易时段 -----------------------------------------------------------


def test_order_outside_trading_time_is_rejected(engine):
    feed(engine, au_quote("12:30:00.000000", 820.0))  # 午间休市
    order = rejected(engine, submit_request("order-lunch"))
    assert "不在可交易时间段内" in order["lastMsg"]


def test_holiday_is_not_trading_time(engine):
    book = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0,
                     clock=VirtualClock(datetime.fromisoformat("2026-11-07T10:00:00+08:00")),
                     trading_dates=lambda: TRADING_DATES)
    assert book.is_in_trading_time(au_quote("2026-11-07 10:00:00.000000", 820.0)) is False


def test_quote_update_outside_trading_time_moves_price_but_never_matches(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0))
    # 挂一张不触发的买单，随后午间休市价格穿过限价：不得成交。
    alive = engine.submit(ACCOUNT, submit_request("order-resting", limitPrice=815.0))
    assert alive["order"]["status"] == "ALIVE"
    feed(engine, au_quote("12:30:00.000000", 810.0))
    assert engine.query_order(ACCOUNT, "order-resting")["order"]["status"] == "ALIVE"
    # 价格本身已更新（浮动盈亏按新价计算）。
    assert engine.positions(ACCOUNT) == []  # 未成交自然没有持仓
    quote = engine.market_quote(ACCOUNT, "SHFE.au2612")
    assert quote["lastPrice"] == 810.0
    # 恢复交易时段后同一张单按限价成交。
    feed(engine, au_quote("13:35:00.000000", 812.0))
    assert engine.query_order(ACCOUNT, "order-resting")["order"]["status"] == "FINISHED"
    assert by_direction_long(engine)["openPrice"] == pytest.approx(815.0)


# -- 撤单与幂等 -----------------------------------------------------------


def test_cancel_and_idempotency(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0))
    alive = engine.submit(ACCOUNT, submit_request("order-idem", limitPrice=815.0))
    assert alive["order"]["status"] == "ALIVE"
    with pytest.raises(RuntimeError, match="order_not_cancelable"):
        engine.cancel_order(ACCOUNT, "order-unknown01", "cmd-cancel-unknown01")
    cancelled = engine.cancel_order(ACCOUNT, "order-idem", "cmd-cancel-order-idem")
    assert cancelled["order"]["status"] == "FINISHED"
    assert cancelled["order"]["isError"] is False
    # 已结束的委托不能再撤。
    with pytest.raises(RuntimeError, match="order_not_cancelable"):
        engine.cancel_order(ACCOUNT, "order-idem", "cmd-cancel-order-idem2")

    # 同一 clientOrderId 重复提交：不会下第二单，返回当前状态。
    again = engine.submit(ACCOUNT, submit_request("order-idem", limitPrice=815.0))
    assert again["order"]["status"] == "FINISHED"
    orders = engine.activity(ACCOUNT)["orders"]
    assert [item["clientOrderId"] for item in orders].count("order-idem") == 1

    # 同一 commandId 重放：返回缓存结果。
    replay = engine.submit(ACCOUNT, submit_request(
        "order-idem2", commandId="cmd-order-idem", limitPrice=815.0))
    assert replay == again


# -- 审查 527-9：撤单命令重放跨两日内存裁剪 -----------------------------------


def test_cancel_command_replay_after_memory_trim_reads_persisted_order(tmp_path):
    # 同一撤单命令隔两个交易日后重放：内存归档已按天裁剪，但 paper_orders 里
    # 终态还在——重放按已撤终态幂等返回（与 submit/query 共用 _known_order），
    # 而不是报 order_not_cancelable。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-aged-hold"))  # 持仓推动每日结算
        engine.submit(ACCOUNT, submit_request("order-cancel-aged", limitPrice=815.0))
        cancelled = engine.cancel_order(ACCOUNT, "order-cancel-aged", "cmd-cancel-aged")
        assert cancelled["order"]["status"] == "FINISHED"
        assert cancelled["order"]["isError"] is False
        feed(engine, au_quote("2026-11-03 09:31:00.000000", 821.0, settlement=821.0))
        feed(engine, au_quote("2026-11-04 09:31:00.000000", 820.0, settlement=820.0))
        assert "order-cancel-aged" not in engine._accounts[ACCOUNT].archived_orders
        replay = engine.cancel_order(ACCOUNT, "order-cancel-aged", "cmd-cancel-aged")
        assert replay["order"]["status"] == "FINISHED"
        assert replay["order"]["isError"] is False
        # 新命令撤同一个已结束委托仍然拒绝，不会把历史查询变成重发。
        with pytest.raises(RuntimeError, match="order_not_cancelable"):
            engine.cancel_order(ACCOUNT, "order-cancel-aged", "cmd-cancel-other")
    finally:
        engine.close()


# -- 跨日结算与到期 -------------------------------------------------------


def test_settlement_marks_prices_carries_history_and_cancels_resting_orders(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-settle-open", volume=2))
    engine.submit(ACCOUNT, submit_request("order-settle-rest", limitPrice=815.0))
    # 收盘：结算价 825.00 随行情发布。
    closing = au_quote("15:10:00.000000", 824.5, settlement=825.0)
    feed(engine, closing)
    # 下一交易日（11-03）首笔行情触发结算：行情携带的 pre_settlement 就是
    # 上一交易日的正式结算价（2026-10-08 起不再用最新价）。
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    position = next(item for item in engine.positions(ACCOUNT) if item["direction"] == "long")
    assert position["volume"] == 2
    assert position["todayVolume"] == 0
    assert position["historicalVolume"] == 2
    assert position["openPrice"] == pytest.approx(820.02)  # 开仓价不因结算改变
    account = engine.account(ACCOUNT)
    assert account["commission"] == 0.0  # 当日字段清零
    assert account["closeProfit"] == 0.0
    # 结算后的挂单被撤销（GFD）。
    resting = engine.query_order(ACCOUNT, "order-settle-rest")
    assert resting["order"]["status"] == "FINISHED"
    assert resting["order"]["isError"] is False
    # 平昨：CLOSE 对上期所平昨仓。
    feed(engine, au_quote("2026-11-03 10:00:00.000000", 825.5))
    closed = engine.submit(ACCOUNT, submit_request(
        "order-settle-close", direction="SELL", offset="CLOSE", volume=2, limitPrice=825.48))
    assert closed["order"]["status"] == "FINISHED"
    assert engine.positions(ACCOUNT) == []
    assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((825.48 - 825.0) * 2 * 1000)


def test_settlement_runs_once_across_multi_day_downtime(tmp_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    first = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock,
                        trading_dates=TRADING_DATES)
    first.open()
    first.start()
    first.create_account(ACCOUNT)
    feed(first, au_quote("09:31:00.000000", 820.0))
    first.submit(ACCOUNT, submit_request("order-downtime"))
    first.close()

    # 停机跨过 11-03、11-04 两个交易日，11-05 首笔行情到达：只结一次。
    restarted = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"),
                            clock=VirtualClock(datetime.fromisoformat("2026-11-05T09:00:00+08:00")),
                            trading_dates=TRADING_DATES)
    restarted.open()
    restarted.start()
    assert restarted.account(ACCOUNT)["positionCount"] == 1  # 恢复后持仓不变
    feed(restarted, au_quote("2026-11-05 09:31:00.000000", 821.0, settlement=821.0))
    position = next(item for item in restarted.positions(ACCOUNT) if item["direction"] == "long")
    assert position["historicalVolume"] == 1
    assert restarted.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-05"
    assert restarted.settlement_count(ACCOUNT) == 1
    feed(restarted, au_quote("2026-11-05 10:00:00.000000", 821.5))
    assert restarted.settlement_count(ACCOUNT) == 1
    restarted.close()


def test_weekend_and_holiday_idle_do_not_settle(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-holiday"))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 821.0, settlement=821.0))
    assert engine.settlement_count(ACCOUNT) == 1
    assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-03"
    # 11-06（周五）首笔行情结算一次；尾盘同日行情与周末停盘空闲都不再结算。
    feed(engine, au_quote("2026-11-06 09:31:00.000000", 819.0, settlement=819.5))
    assert engine.settlement_count(ACCOUNT) == 2
    feed(engine, au_quote("2026-11-06 15:10:00.000000", 819.2))
    assert engine.settlement_count(ACCOUNT) == 2
    engine.drain(timeout=2.0)
    assert engine.settlement_count(ACCOUNT) == 2


def test_expiry_auto_closes_at_settlement_price(engine):
    # 合约 11-02（当日 15:00）到期：次日起已过期，普通目标日 11-02 恰与到期日
    # 同日，按该日正式结算价走原 Book 到期强平（#527 返修：真实到期日期）。
    feed(engine, au_quote("09:31:00.000000", 820.0, expire_ns=EXPIRY_1102_NS))
    engine.submit(ACCOUNT, submit_request("order-expiry-open", volume=2))
    # 到期日的下一次结算（换到 11-03 且合约已过期）按结算价自动平仓。
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 824.0, settlement=824.0,
                          expire_ns=EXPIRY_1102_NS))
    trades = engine.activity(ACCOUNT)["trades"]
    expiry_trades = [item for item in trades if item.get("reason") == "expiry"]
    assert len(expiry_trades) == 1
    assert expiry_trades[0]["volume"] == 2
    assert expiry_trades[0]["price"] == pytest.approx(824.0)
    assert expiry_trades[0]["offset"] in {"CLOSE", "CLOSETODAY"}
    assert engine.positions(ACCOUNT) == []
    account = engine.account(ACCOUNT)
    assert account["closeProfit"] == pytest.approx((824.0 - 820.02) * 2 * 1000)
    assert account["margin"] == 0.0


# -- 数值对照：固定成交序列，期望值按交易所口径手算 ------------------------


def test_numeric_parity_with_exchange_convention(engine):
    # 合约参数：每手保证金 quote.margin=80000，手续费 quote.commission=20，
    # 合约乘数 1000，最小变动价位 0.02。
    #
    # 1) 11-02 买开 2 手 @820.02：
    #    手续费 = 2×20 = 40；保证金占用 = 2×80000 = 160000；
    #    浮动盈亏 =（820.00 − 820.02）×1000×2 = −40；
    #    权益 = 1_000_000 − 40 − 40 = 999_920。
    # 2) 11-02 平今 1 手 @819.98：
    #    平仓盈亏 =（819.98 − 820.02）×1000×1 = −40；手续费 +20；
    #    保证金 = 80000；权益 = 999_920 − 40 − 20 = 999_860 现金口径，
    #    持仓口径浮动盈亏剩（820.00−820.02）×1000 = −20 → 权益 999_880。
    # 3) 11-03 首跳先结算（结算价 825.00，来自行情的 pre_settlement）再入账标记
    #    826.00：今仓转昨仓，持仓价重置为 825.00；静态权益 =
    #    999_880 +（825.00−820.00）×1000 = 1_004_880；随后按 826.00 标记浮盈
    #    +1000，可用 = 1_004_880 + 1000 − 80000 = 925_880。
    # 4) 11-03 平昨 1 手 @814.98：
    #    平仓盈亏 =（814.98 − 825.00）×1000 = −10_020；手续费 20；
    #    保证金 0；权益 = 1_004_880 − 10_020 − 20 = 994_840。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-num-open", volume=2, limitPrice=820.02))
    account = engine.account(ACCOUNT)
    assert account["commission"] == 40.0
    assert account["margin"] == 160_000.0
    assert account["floatProfit"] == pytest.approx(-40.0)
    assert account["equity"] == pytest.approx(999_920.0)
    assert account["closeProfit"] == 0.0

    feed(engine, au_quote("10:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request(
        "order-num-close-today", direction="SELL", offset="CLOSETODAY",
        volume=1, limitPrice=819.98))
    account = engine.account(ACCOUNT)
    assert account["commission"] == 60.0
    assert account["margin"] == 80_000.0
    assert account["floatProfit"] == pytest.approx(-20.0)
    assert account["closeProfit"] == pytest.approx(-40.0)
    assert account["equity"] == pytest.approx(999_880.0)

    feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
    account = engine.account(ACCOUNT)
    assert account["staticBalance"] == pytest.approx(1_004_880.0)
    assert account["available"] == pytest.approx(925_880.0)
    assert account["commission"] == 0.0
    assert account["closeProfit"] == 0.0
    position = by_direction_long(engine)
    assert position["historicalVolume"] == 1 and position["todayVolume"] == 0

    feed(engine, au_quote("2026-11-03 10:31:00.000000", 815.0))
    engine.submit(ACCOUNT, submit_request(
        "order-num-close-his", direction="SELL", offset="CLOSE", volume=1, limitPrice=814.98))
    account = engine.account(ACCOUNT)
    assert account["equity"] == pytest.approx(994_840.0)
    assert account["closeProfit"] == pytest.approx(-10_020.0)
    assert account["commission"] == 20.0
    assert account["margin"] == 0.0
    assert account["available"] == pytest.approx(994_840.0)
    assert account["positionCount"] == 0
    # 全序列现金守恒：权益 = 初始 − 手续费合计(40+20+20) + 两次平仓盈亏 + 结算后浮盈兑现。
    assert account["equity"] == pytest.approx(
        1_000_000.0 - 80.0 + (-40.0 - 10_020.0) + (825.0 - 820.02) * 1000)


# -- 数值对照：保证金按最新 quote.margin 重算 ------------------------------


def test_margin_tracks_quote_margin_changes(engine):
    feed(engine, au_quote("09:31:00.000000", 820.0, margin=80_000.0))
    engine.submit(ACCOUNT, submit_request("order-margin-open", volume=1))
    assert engine.account(ACCOUNT)["margin"] == 80_000.0
    # 交易所上调保证金：行情字段变化后持仓保证金随之变化。
    feed(engine, au_quote("10:31:00.000000", 820.0, margin=90_000.0))
    assert engine.account(ACCOUNT)["margin"] == 90_000.0


# -- 审查 527-2：同品种多空保证金按品种取大边 ----------------------------------


def test_same_product_dual_side_margin_takes_the_larger_side(engine):
    # 官方 SimTrade 按 exchange_id + product_id 分组、每组取 max(多, 空)：同品种
    # 一多一空（锁仓）只占单边 80_000，不是两边相加 160_000。行情里没有
    # product_id 时官方内核退化为 other_margin 两边相加——TqQuoteSource._detach
    # 必须保留该字段（配套断言见 test_paper_service）。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-hedge-long", volume=1))
    feed(engine, au_quote("09:36:00.000000", 821.0))  # 推价过限价，空单成交
    engine.submit(ACCOUNT, submit_request(
        "order-hedge-short", direction="SELL", volume=1, limitPrice=820.98))
    account = engine.account(ACCOUNT)
    assert account["margin"] == pytest.approx(80_000.0)
    assert len(engine.positions(ACCOUNT)) == 2  # 一多一空都在
    # 单向持仓不受分组影响：保证金仍是逐手相加。
    feed(engine, au_quote("09:41:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request(
        "order-hedge-close", direction="SELL", offset="CLOSETODAY", limitPrice=819.98))
    assert [item["direction"] for item in engine.positions(ACCOUNT)] == ["short"]
    assert engine.account(ACCOUNT)["margin"] == pytest.approx(80_000.0)


# -- 审查 #465：行情按合约索引分发 ------------------------------------------


def build_engine(db_path, *, clock, **overrides):
    engine = PaperEngine(
        db_path=str(db_path),
        clock=clock,
        trading_dates=overrides.pop("trading_dates", TRADING_DATES),
        **overrides,
    )
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT)
    return engine


def test_quote_dispatch_follows_symbol_account_index(tmp_path):
    # 100 个账户里只有 1 个持有 rb：一笔 rb 行情只应驱动那一个账户的 SimTrade。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        for index in range(100):
            engine.create_account(f"paper-acct-{index:04d}")
        feed(engine, au_quote("09:31:00.000000", 820.0, symbol="SHFE.rb2610"))
        opened = engine.submit("paper-acct-0042",
                               submit_request("order-rb-idx", symbol="SHFE.rb2610"))
        assert opened["order"]["status"] == "FINISHED"
        assert opened["order"]["isError"] is False
        calls: list[int] = []
        for entry in engine._accounts.values():
            sim = entry.book._sim
            original = sim.update_quotes

            def counted(symbol, pack, _original=original):
                calls.append(1)
                return _original(symbol, pack)

            sim.update_quotes = counted
        feed(engine, au_quote("09:32:00.000000", 821.0, symbol="SHFE.rb2610"))
        assert sum(calls) == 1
    finally:
        engine.close()


# -- 审查 #465：跨日结算只在引擎层判断一次 ----------------------------------


def test_holiday_startup_without_calendar_settles_nothing(tmp_path):
    # 节假日期间启动、真实日历未就绪：开户照常，但既不结算也不用兜底日期定
    # 账本日；日历就绪后只把新账户直接定到下一交易日，不发生多余结算。
    loaded: list[set[str]] = [set()]
    clock = VirtualClock(datetime.fromisoformat("2026-11-07T10:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock,
                          trading_dates=lambda: loaded[0])
    try:
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == ""
        feed(engine, au_quote("2026-11-07 10:00:00.000000", 820.0))
        assert engine.settlement_count(ACCOUNT) == 0
        loaded[0] = set(TRADING_DATES)
        feed(engine, au_quote("2026-11-07 10:01:00.000000", 820.0))
        assert engine.settlement_count(ACCOUNT) == 0
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-09"
    finally:
        engine.close()


# -- 审查 #465：账户数据新鲜度不跟随行情 ------------------------------------


def test_account_freshness_does_not_follow_quote_age(tmp_path):
    # 休市（最后一笔行情 3 小时前）：账户/委托成交/状态仍是 normal，行情新鲜度
    # 只体现在行情端点上。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        clock.advance(datetime.fromisoformat("2026-11-02T12:31:00+08:00"))
        account = engine.account(ACCOUNT)
        assert account["dataStatus"] == "normal"
        assert account["ageSeconds"] < 1.0
        activity = engine.activity(ACCOUNT)
        assert activity["dataStatus"] == "normal"
        assert activity["ageSeconds"] < 1.0
        status = engine.status(ACCOUNT)
        assert status["dataStatus"] == "normal"
        assert status["ordersInitialized"] is True
        assert engine.market_quote(ACCOUNT, "SHFE.au2612")["dataStatus"] == "stale"
    finally:
        engine.close()


def test_source_reconnect_window(tmp_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.mark_source_reconnecting()
        clock.advance(datetime.fromisoformat("2026-11-02T09:31:30+08:00"))
        assert engine.status(ACCOUNT)["dataStatus"] == "normal"
        clock.advance(datetime.fromisoformat("2026-11-02T09:32:31+08:00"))
        assert engine.status(ACCOUNT)["dataStatus"] == "unavailable"
    finally:
        engine.close()


# -- 审查 #465：结算价按交易日 ----------------------------------------------


def test_settlement_price_is_day_scoped(tmp_path):
    # 11-02 结算价 825 只结 11-02/11-03 这一次账；11-04 的结算用 11-03 自己的
    # 结算价（819.5），残留的 11-02 结算价不得复用（2026-10-08 起也不用最新价）。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-sprice"))
        feed(engine, au_quote("15:10:00.000000", 824.5, settlement=825.0))
        feed(engine, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-03"
        assert engine.account(ACCOUNT)["staticBalance"] == pytest.approx(1_004_960.0)
        feed(engine, au_quote("2026-11-04 09:31:00.000000", 819.0, pre_settlement=819.5))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-04"
        assert engine.account(ACCOUNT)["staticBalance"] == pytest.approx(999_460.0)
    finally:
        engine.close()


# -- 2026-10-08：每日结算等交易所正式结算价 ---------------------------------


def trade_log_sources(engine: PaperEngine, trading_day: str) -> dict:
    probe = sqlite3.connect(f"file:{engine.db_path}?mode=ro", uri=True)
    try:
        row = probe.execute(
            "SELECT log_json FROM paper_trade_log WHERE account_id=? AND trading_day=?",
            (ACCOUNT, trading_day)).fetchone()
    finally:
        probe.close()
    assert row is not None, f"no trade log for {trading_day}"
    return dict(json.loads(row[0])["settlementSources"])


def test_settlement_waits_for_the_published_settlement_price(tmp_path):
    """18:00 换日时结算价未公布（交易所约 19:00 才给）：不结算、账本日保持
    previous_day；夜盘开盘后行情带上 previous_day 的 settlement，立刻按它结算，
    source 记 "settlement"。无持仓账户没有可等的价，直接日结。"""
    empty = "paper:0000000000c3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        engine.create_account(empty)
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-wait-settle"))
        # 18:31 一笔（任何合约的）行情把引擎日翻到 11-03：持仓账户推迟结算，
        # 无持仓账户不等价，直接日结。
        feed(engine, au_quote("18:31:00.000000", 820.5))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-02"
        assert engine.settlement_count(ACCOUNT) == 0
        assert engine.account_info(empty)["bookTradingDay"] == "2026-11-03"
        assert engine.settlement_count(empty) == 1
        # 夜盘开盘后的行情带 11-02 的正式结算价 825：立刻结算。
        feed(engine, au_quote("2026-11-02 21:05:00.000000", 821.0, settlement=825.0))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-03"
        assert engine.settlement_count(ACCOUNT) == 1
        assert trade_log_sources(engine, "2026-11-02") == {"SHFE.au2612": "settlement"}
        position = by_direction_long(engine)
        assert position["historicalVolume"] == 1  # 结算后今仓转昨仓
    finally:
        engine.close()


def test_flat_account_crossing_day_settles_without_waiting_for_prices(tmp_path):
    """当日开平后空仓的账户跨日也走正式结算：当日损益/费用归零、静态权益结转、
    日报落在上一交易日、剩余 GFD 挂单跨日失效；没有持仓就没有可等的结算价，
    不推迟。生产 2026-10-09 对账实证：空仓账户跨日只推进 book_trading_day，
    当日字段保留旧值、日报缺失。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-flat-open", volume=2))
        feed(engine, au_quote("10:00:00.000000", 820.5))
        engine.submit(ACCOUNT, submit_request(
            "order-flat-close", direction="SELL", offset="CLOSETODAY", volume=2,
            limitPrice=820.48))
        assert engine.account(ACCOUNT)["positionCount"] == 0
        engine.submit(ACCOUNT, submit_request("order-flat-rest", limitPrice=815.0))
        before = engine.account(ACCOUNT)
        assert before["closeProfit"] == pytest.approx((820.48 - 820.02) * 2 * 1000)
        assert before["commission"] == 80.0  # 开 2 + 平 2，各 20/手
        # 18:31 换日且结算价未公布：空持仓账户直接日结，不等行情。
        feed(engine, au_quote("18:31:00.000000", 820.5))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-03"
        assert engine.settlement_count(ACCOUNT) == 1
        after = engine.account(ACCOUNT)
        assert after["commission"] == 0.0  # 当日字段清零
        assert after["closeProfit"] == 0.0
        assert after["equity"] == pytest.approx(before["equity"])  # 权益不变
        assert after["staticBalance"] == pytest.approx(before["equity"])
        # 挂单跨日失效（GFD），终态可查。
        resting = engine.query_order(ACCOUNT, "order-flat-rest")
        assert resting["order"]["status"] == "FINISHED"
        assert resting["order"]["isError"] is False
        # 日报落在上一交易日，含当日开平两笔成交。
        with sqlite3.connect(engine.db_path) as connection:
            row = connection.execute(
                "SELECT log_json FROM paper_trade_log WHERE account_id=? AND trading_day=?",
                (ACCOUNT, "2026-11-02")).fetchone()
        assert row is not None
        assert len(json.loads(row[0])["trades"]) == 2
        # 同一交易日内的后续行情不重复结算、不重复写日报。
        feed(engine, au_quote("2026-11-03 10:00:00.000000", 821.0))
        assert engine.settlement_count(ACCOUNT) == 1
        engine.close()
        # 重启回到同一数据库：跨日不重复副作用。
        restarted = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"),
                                clock=VirtualClock(datetime.fromisoformat(
                                    "2026-11-03T10:30:00+08:00")),
                                trading_dates=TRADING_DATES)
        restarted.open()
        restarted.start()
        try:
            feed(restarted, au_quote("2026-11-03 10:31:00.000000", 821.0))
            assert restarted.settlement_count(ACCOUNT) == 1
            assert restarted.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-03"
            with sqlite3.connect(restarted.db_path) as connection:
                days = [r[0] for r in connection.execute(
                    "SELECT trading_day FROM paper_trade_log WHERE account_id=?",
                    (ACCOUNT,))]
            assert days == ["2026-11-02"]
        finally:
            restarted.close()
    finally:
        engine.close()


def test_settlement_feed_from_the_active_query_settles_a_deferred_account(tmp_path):
    """主动查询（query_symbol_settlement）的结果一到就结算：这是 18:00–夜盘开盘
    之间没有行情时拿到结算价的主通道。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-active-query"))
        feed(engine, au_quote("18:31:00.000000", 820.5))
        assert engine.pending_settlement_symbols() == {"SHFE.au2612"}
        engine.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-02", "settlement": 825.0}])
        assert engine.drain(timeout=5.0)
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-03"
        assert engine.settlement_count(ACCOUNT) == 1
        assert trade_log_sources(engine, "2026-11-02") == {"SHFE.au2612": "settlement"}
        assert engine.pending_settlement_symbols() == set()
    finally:
        engine.close()


def test_consecutive_settlement_feeds_pair_the_drain_count_by_batch(tmp_path):
    """两次 feed_settlements 之间 actor 没来得及排空：drain 计数按批次结对，
    等待方正常返回，不会因 _posted/_processed 错位挂到超时。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-drain-batch"))
        feed(engine, au_quote("18:31:00.000000", 820.5))
        engine.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-02", "settlement": 824.0}])
        engine.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-02", "settlement": 825.0}])
        assert engine.drain(timeout=5.0)
        assert engine.settlement_count(ACCOUNT) == 1
        assert trade_log_sources(engine, "2026-11-02") == {"SHFE.au2612": "settlement"}
        # 批次按先后合并，后到的同日价格覆盖先到的。
        assert engine.account(ACCOUNT)["staticBalance"] == pytest.approx(
            1_000_000.0 - 20.0 + (825.0 - 820.02) * 1000)
    finally:
        engine.close()


def test_submit_while_behind_settles_first_and_survives_the_settlement(tmp_path):
    """账本日落后收到委托：先结算再进账本（settle 会撤昨日 GFD 挂单，新委托不能
    被这次结算撤掉）；结算价未到时拒绝（可重试），结算完成后夜盘开盘重试同一
    委托照常受理——18:00–21:00 之间的盘后时段 SimTrade 本就拒单（与午休同口径），
    挂单只能从交易时段开始。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-early-open"))
        feed(engine, au_quote("18:35:00.000000", 820.5))
        # 结算价未公布：下单被拒（可重试），命令不进账本。
        with pytest.raises(RuntimeError, match="paper_settlement_pending"):
            engine.submit(ACCOUNT, submit_request("order-early", limitPrice=815.0))
        assert engine.query_order(ACCOUNT, "order-early") == {
            "ordersInitialized": True, "found": False}
        # 主动查询结果到达，结算完成。
        engine.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-02", "settlement": 825.0}])
        assert engine.drain(timeout=5.0)
        assert engine.settlement_count(ACCOUNT) == 1
        # 同一委托重试：夜盘开盘（交易时段内）后入账，晚于结算，不被结算撤销。
        feed(engine, au_quote("2026-11-02 21:05:00.000000", 821.0))
        placed = engine.submit(ACCOUNT, submit_request("order-early", limitPrice=815.0))
        assert placed["order"]["status"] == "ALIVE"
        feed(engine, au_quote("2026-11-02 21:10:00.000000", 820.0))
        assert engine.query_order(ACCOUNT, "order-early")["order"]["status"] == "ALIVE"
    finally:
        engine.close()


def test_restart_settles_with_pre_settlement_not_the_persisted_stale_quote(tmp_path):
    """10-08 生产事故形状：引擎重启后内存没有新行情，账本里持久化的是几个小时
    前的旧报价；新交易日首笔行情带 pre_settlement 时按它结算，旧报价不参与。"""
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-restart-fill"))
        feed(engine, au_quote("14:06:55.000000", 821.0))
        # 一笔盘后委托把 14:06:55 的旧报价随快照落盘。
        resting = engine.submit(ACCOUNT, submit_request("order-restart-rest", limitPrice=815.0))
        assert resting["order"]["status"] == "ALIVE"
    finally:
        engine.close()

    restarted = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-03T08:59:00+08:00")))
    try:
        # 重启后的行情快照：上一交易日 15:00 收盘价（只够 "close" 兜底）。
        feed(restarted, au_quote("2026-11-02 15:00:00.000000", 819.0))
        assert restarted.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-02"
        assert restarted.settlement_count(ACCOUNT) == 0
        # 新交易日首笔行情带 pre_settlement=825：按它结算，不用 821 的旧报价，
        # 也不用 819 的收盘兜底。
        feed(restarted, au_quote("2026-11-03 09:31:00.000000", 826.0, pre_settlement=825.0))
        assert restarted.settlement_count(ACCOUNT) == 1
        assert trade_log_sources(restarted, "2026-11-02") == {"SHFE.au2612": "settlement"}
        assert restarted.account(ACCOUNT)["staticBalance"] == pytest.approx(
            1_000_000.0 - 20.0 + (825.0 - 820.02) * 1000)
    finally:
        restarted.close()


def test_close_price_is_no_longer_a_settlement_source_first_order_waits(tmp_path):
    """所有者 2026-10-11 合同：普通持仓只认目标日交易所正式结算价，收盘价不再
    是兜底来源——新交易日交易时段已开始但正式结算价未到时，账户继续 pending、
    首笔委托被拒（可重试）；目标日的正式价到达后按它结算。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-no-close-fb"))
        feed(engine, au_quote("15:00:00.000000", 819.0))  # 只有上一交易日收盘价
        feed(engine, au_quote("2026-11-03 09:31:00.000000", 820.0))
        assert engine.settlement_count(ACCOUNT) == 0  # 不用收盘价完成结算
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-02"
        with pytest.raises(RuntimeError, match="paper_settlement_pending"):
            engine.submit(ACCOUNT, submit_request("order-no-close-blocked"))
        engine.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-02", "settlement": 825.0}])
        assert engine.drain(timeout=5.0)
        assert engine.settlement_count(ACCOUNT) == 1
        assert trade_log_sources(engine, "2026-11-02") == {"SHFE.au2612": "settlement"}
        assert engine.account(ACCOUNT)["staticBalance"] == pytest.approx(
            1_000_000.0 - 20.0 + (825.0 - 820.02) * 1000)
    finally:
        engine.close()


# -- #527：待结算价格事实的目标日与来源约束 --------------------------------


MULTI_DAY_CALENDAR = TRADING_DATES | {"2026-10-30"}


def _open_one_lot_and_cold_close(tmp_path, calendar=MULTI_DAY_CALENDAR):
    """11-02 开一手多单并真正关闭引擎/SQLite：多日冷恢复场景的公共前置。"""
    db = tmp_path / "paper.sqlite3"
    first = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-02T09:00:00+08:00")), trading_dates=calendar)
    feed(first, au_quote("09:31:00.000000", 820.0))
    first.submit(ACCOUNT, submit_request("order-target-day"))
    first.close()
    return db


def test_settlement_selects_the_target_day_price_after_multi_day_downtime(tmp_path):
    """#527 原例 A：11-02 开仓冷关，11-04 18:10（北京时）重开，18 点规则令账本
    要换日到 11-05——未到期持仓的目标结算日是 11-04。主动查询只回 11-03 的价
    823 时必须继续 pending（更旧日期不能提前推进）；11-04 的 825 到达才结算。"""
    db = _open_one_lot_and_cold_close(tmp_path)
    restarted = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-04T18:10:00+08:00")),
        trading_dates=MULTI_DAY_CALENDAR)
    try:
        feed(restarted, au_quote("2026-11-04 18:10:00.000000", 821.0))
        assert restarted.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-02"
        assert restarted.pending_settlement_symbols() == {"SHFE.au2612"}
        # SDK 形状的查询只回了 11-03 的结算价（11-04 还没公布）。
        restarted.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-03", "settlement": 823.0}])
        assert restarted.drain(timeout=5.0)
        assert restarted.settlement_count(ACCOUNT) == 0
        assert restarted.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-02"
        assert restarted.pending_settlement_symbols() == {"SHFE.au2612"}
        # 19:10 目标日 11-04 的正式结算价到达：按它结算。
        restarted.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-04", "settlement": 825.0}])
        assert restarted.drain(timeout=5.0)
        assert restarted.settlement_count(ACCOUNT) == 1
        assert restarted.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-05"
        assert restarted.account(ACCOUNT)["staticBalance"] == pytest.approx(
            1_000_000.0 - 20.0 + (825.0 - 820.02) * 1000)
    finally:
        restarted.close()


def test_settlement_target_day_control_first_query_price_settles(tmp_path):
    """#527 原例 A 正控：重开后首次主动查询就带目标日 11-04 的价，立即按 825 结算。"""
    db = _open_one_lot_and_cold_close(tmp_path)
    restarted = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-04T18:10:00+08:00")),
        trading_dates=MULTI_DAY_CALENDAR)
    try:
        feed(restarted, au_quote("2026-11-04 18:10:00.000000", 821.0))
        restarted.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-04", "settlement": 825.0}])
        assert restarted.drain(timeout=5.0)
        assert restarted.settlement_count(ACCOUNT) == 1
        assert restarted.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-05"
        assert restarted.account(ACCOUNT)["staticBalance"] == pytest.approx(
            1_000_000.0 - 20.0 + (825.0 - 820.02) * 1000)
    finally:
        restarted.close()


SECOND_SYMBOL = "SHFE.au2606"


EXPIRING = "SHFE.au2612"
# au 万分之1 开平（对拍：@822 × 乘数 1000 × 1 手 = 82.2；@824 = 82.4）。
AU_RATE = {"mode": "notional", "open": 1.0, "closeYesterday": 1.0, "closeToday": 1.0,
           "openPerLotExtra": 0.0, "closeYesterdayPerLotExtra": 0.0, "closeTodayPerLotExtra": 0.0}


def _open_expiring_and_far_then_cold_close(tmp_path, *, fee_rate=None):
    """11-02 开 au2612（11-03 到期）与 au2606（远月）各一手多单后冷关；之后的重启都跨过了
    au2612 的最后交易日 11-03。"""
    db = tmp_path / "paper.sqlite3"
    first = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-02T09:00:00+08:00")))
    feed(first, au_quote("09:31:00.000000", 820.0, expire_ns=EXPIRY_1103_NS))
    feed(first, au_quote("09:31:00.000000", 820.0, symbol=SECOND_SYMBOL))
    first.submit(ACCOUNT, submit_request(
        "order-cross-expiring", **({"feeRate": fee_rate} if fee_rate else {})))
    first.submit(ACCOUNT, submit_request("order-cross-far", symbol=SECOND_SYMBOL))
    first.close()
    return db


def _restart_past_expiry(db, moment="2026-11-05T09:00:00+08:00"):
    """重开并收到远月 11-05 的第一笔行情（带 11-04 = 目标日的正式结算价 828）。"""
    engine = build_engine(db, clock=VirtualClock(datetime.fromisoformat(moment)))
    feed(engine, au_quote("2026-11-05 09:31:00.000000", 830.0, pre_settlement=828.0,
                          symbol=SECOND_SYMBOL))
    return engine


def _expiry_rows(engine: PaperEngine) -> list[tuple]:
    with sqlite3.connect(engine.db_path) as connection:
        return connection.execute(
            "SELECT symbol, final_day, status, since_day FROM paper_expiry_settlements "
            "WHERE account_id=?", (ACCOUNT,)).fetchall()


def _expiry_trades(engine: PaperEngine) -> list[dict]:
    return [trade for trade in engine.activity(ACCOUNT)["trades"] if trade.get("reason") == "expiry"]


def _volumes(engine: PaperEngine) -> dict[str, int]:
    return {item["symbol"]: item["volume"] for item in engine.positions(ACCOUNT)}


def test_cross_downtime_past_expiry_suspends_only_that_contract(tmp_path):
    """#527 最终结算日协议（所有者 2026-10-15）：停机跨过 au2612 的最后交易日 11-03，
    11-05 重开。远月 au2606 拿到目标日 11-04 的正式结算价，账户照常结算、照常下单；
    只有 au2612 挂起（本次结算标记到持仓均价、不强平、不确认盈亏），等最后交易日的价。"""
    db = _open_expiring_and_far_then_cold_close(tmp_path)
    engine = _restart_past_expiry(db)
    try:
        assert engine.settlement_count(ACCOUNT) == 1
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-05"
        assert _volumes(engine) == {EXPIRING: 1, SECOND_SYMBOL: 1}
        assert trade_log_sources(engine, "2026-11-02") == {
            SECOND_SYMBOL: "settlement", EXPIRING: "expiry-pending"}
        [logged] = _trade_log(engine, "2026-11-02")["expirySettlements"]
        assert (logged["symbol"], logged["finalDay"], logged["status"], logged["since"]) == (
            EXPIRING, "2026-11-03", "pending", "2026-11-05")
        # au2612 只按持仓均价结转：权益里只有远月 828 的结算盈亏，到期合约没有确认任何盈亏。
        assert engine.account(ACCOUNT)["staticBalance"] == pytest.approx(
            1_000_000.0 - 40.0 + (828.0 - 820.02) * 1000)
        [pending] = engine.account(ACCOUNT)["expirySettlements"]
        assert (pending["status"], pending["statusLabel"]) == ("pending", "待最后交易日结算价")
        assert engine.pending_final_settlements() == {EXPIRING: "2026-11-03"}
        assert engine.pending_settlement_symbols() == set()
        # 其他合约照常下单（以前整户 paper_settlement_pending）。
        opened = engine.submit(ACCOUNT, submit_request(
            "order-cross-far-2", symbol=SECOND_SYMBOL, limitPrice=830.02))
        assert opened["order"]["filledVolume"] == 1
        # 最后交易日 11-03 的正式结算价 824 补查到：按原到期语义强平（相对持仓均价）。
        engine.feed_settlements([{"symbol": EXPIRING, "tradingDay": "2026-11-03",
                                  "settlement": 824.0}], checked={EXPIRING: "2026-11-03"})
        assert engine.drain(timeout=5.0)
        assert _volumes(engine) == {SECOND_SYMBOL: 2}
        account = engine.account(ACCOUNT)
        assert account["closeProfit"] == pytest.approx((824.0 - 820.02) * 1000)
        assert account["expirySettlements"] == []
        [trade] = _expiry_trades(engine)
        assert trade["price"] == pytest.approx(824.0)
        assert trade["expirySettlement"]["source"] == "settlement"
        assert trade["expirySettlement"]["status"] == "settled"
        assert _expiry_rows(engine) == [(EXPIRING, "2026-11-03", "settled", "2026-11-05")]
        assert engine.pending_final_settlements() == {}
    finally:
        engine.close()

    # 重复恢复：已结的不再平、不再结算。
    again = _restart_past_expiry(db, "2026-11-05T10:00:00+08:00")
    try:
        again.feed_settlements([{"symbol": EXPIRING, "tradingDay": "2026-11-03",
                                 "settlement": 824.0}], checked={EXPIRING: "2026-11-03"})
        assert again.drain(timeout=5.0)
        assert again.settlement_count(ACCOUNT) == 1
        assert again.account(ACCOUNT)["closeProfit"] == pytest.approx((824.0 - 820.02) * 1000)
        assert len(_expiry_trades(again)) == 1
        assert _expiry_rows(again) == [(EXPIRING, "2026-11-03", "settled", "2026-11-05")]
    finally:
        again.close()


def test_final_day_price_already_in_hand_closes_right_after_the_settlement(tmp_path):
    """普通查询（8 个交易日）已经带回最后交易日 11-03 的价：重开第一次结算就当场强平，
    结果与原到期语义一致（结算时标记到持仓均价，新交易日按最后交易日结算价平）。"""
    db = _open_expiring_and_far_then_cold_close(tmp_path)
    engine = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-05T09:00:00+08:00")))
    try:
        engine.feed_settlements([
            {"symbol": EXPIRING, "tradingDay": "2026-11-03", "settlement": 824.0},
            {"symbol": SECOND_SYMBOL, "tradingDay": "2026-11-04", "settlement": 828.0}])
        assert engine.drain(timeout=5.0)
        feed(engine, au_quote("2026-11-05 09:31:00.000000", 830.0, symbol=SECOND_SYMBOL))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-05"
        assert _volumes(engine) == {SECOND_SYMBOL: 1}
        assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((824.0 - 820.02) * 1000)
        assert _expiry_rows(engine) == [(EXPIRING, "2026-11-03", "settled", "2026-11-05")]
    finally:
        engine.close()


def test_final_day_price_missing_for_a_trading_day_is_estimated_then_adjusted(tmp_path):
    """挂起满一个交易日、当天补查仍不到：按最后可得价格（重连后行情源送来的 au2612
    最后交易日收盘行情 822，比账本里停机前的 820 新）暂估强平，成交与日结标“暂估，待核”；之后查到正式价 824 补差一笔（平仓盈亏 +2000、
    按开仓费率补手续费差 0.2）并标“已核”。暂估与补差都随账本同一事务推进，重启不重做。"""
    db = _open_expiring_and_far_then_cold_close(tmp_path, fee_rate=AU_RATE)
    engine = _restart_past_expiry(db)
    try:
        feed(engine, au_quote("2026-11-03 14:59:59.000000", 822.0, expire_ns=EXPIRY_1103_NS))
        # 挂起当天补查不到：不暂估（还没满一个交易日）。
        engine.feed_settlements([], checked={EXPIRING: "2026-11-03"})
        assert engine.drain(timeout=5.0)
        assert _volumes(engine)[EXPIRING] == 1
        # 夜盘行情把账本推到 11-06（远月带 11-05 的结算价 829）。
        feed(engine, au_quote("2026-11-05 21:05:00.000000", 831.0, pre_settlement=829.0,
                              symbol=SECOND_SYMBOL))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-06"
        assert _volumes(engine)[EXPIRING] == 1  # 换日本身不暂估：要今天再补查一次
        [still] = _trade_log(engine, "2026-11-05")["expirySettlements"]
        assert still["status"] == "pending"
        # 11-06 补查仍不到：按 822 暂估强平。
        engine.feed_settlements([], checked={EXPIRING: "2026-11-03"})
        assert engine.drain(timeout=5.0)
        assert EXPIRING not in _volumes(engine)
        account = engine.account(ACCOUNT)
        assert account["closeProfit"] == pytest.approx((822.0 - 820.02) * 1000)
        assert account["commission"] == pytest.approx(82.2)
        [estimated] = account["expirySettlements"]
        assert (estimated["status"], estimated["statusLabel"]) == ("estimated", "暂估，待核")
        assert estimated["estimate"]["price"] == pytest.approx(822.0)
        assert estimated["estimate"]["kind"] == "last"
        [trade] = _expiry_trades(engine)
        assert trade["price"] == pytest.approx(822.0)
        assert (trade["expirySettlement"]["source"], trade["expirySettlement"]["statusLabel"]) == (
            "estimated", "暂估，待核")
        assert engine.pending_final_settlements() == {EXPIRING: "2026-11-03"}
    finally:
        engine.close()

    # 重启后同一天再补查不到：已暂估的不再平一次。
    restarted = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-05T21:10:00+08:00")))
    try:
        feed(restarted, au_quote("2026-11-05 21:10:00.000000", 831.0, symbol=SECOND_SYMBOL))
        restarted.feed_settlements([], checked={EXPIRING: "2026-11-03"})
        assert restarted.drain(timeout=5.0)
        assert len(_expiry_trades(restarted)) == 1
        assert restarted.account(ACCOUNT)["closeProfit"] == pytest.approx((822.0 - 820.02) * 1000)
        # 正式价 824 查到：补差一笔，标“已核”。
        restarted.feed_settlements([{"symbol": EXPIRING, "tradingDay": "2026-11-03",
                                     "settlement": 824.0}], checked={EXPIRING: "2026-11-03"})
        assert restarted.drain(timeout=5.0)
        account = restarted.account(ACCOUNT)
        assert account["closeProfit"] == pytest.approx((824.0 - 820.02) * 1000)
        assert account["commission"] == pytest.approx(82.4)
        assert account["expirySettlements"] == []
        [trade] = _expiry_trades(restarted)
        assert trade["price"] == pytest.approx(822.0)  # 成交本身保留暂估价，补差另记
        assert (trade["expirySettlement"]["status"], trade["expirySettlement"]["statusLabel"]) == (
            "adjusted", "已核，已补差")
        assert _expiry_rows(restarted) == [(EXPIRING, "2026-11-03", "adjusted", "2026-11-05")]
    finally:
        restarted.close()

    # 补差也只做一次：重启后再喂同一个正式价，盈亏不变。
    third = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-05T21:20:00+08:00")))
    try:
        third.feed_settlements([{"symbol": EXPIRING, "tradingDay": "2026-11-03",
                                 "settlement": 824.0}], checked={EXPIRING: "2026-11-03"})
        assert third.drain(timeout=5.0)
        assert third.account(ACCOUNT)["closeProfit"] == pytest.approx((824.0 - 820.02) * 1000)
        assert third.account(ACCOUNT)["commission"] == pytest.approx(82.4)
        # 11-06 日结日志：暂估与补差两件事都记在这一天，最终状态“已核”。
        feed(third, au_quote("2026-11-06 21:05:00.000000", 832.0, pre_settlement=830.0,
                             symbol=SECOND_SYMBOL))
        assert third.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-09"
        log = _trade_log(third, "2026-11-06")
        [entry] = log["expirySettlements"]
        assert (entry["status"], entry["statusLabel"]) == ("adjusted", "已核，已补差")
        assert [event["kind"] for event in entry["events"]] == ["estimated", "adjusted"]
        assert entry["adjustment"]["closeProfit"] == pytest.approx(2000.0)
        assert entry["adjustment"]["commission"] == pytest.approx(0.2)
        [logged_trade] = [trade for trade in log["trades"] if trade.get("reason") == "expiry"]
        assert logged_trade["expirySettlement"]["source"] == "estimated"
    finally:
        third.close()


def _estimate_after_one_more_day(engine: PaperEngine) -> None:
    """挂起当天补查不到 → 夜盘行情把账本推到 11-06 → 11-06 再补查仍不到：到暂估条件。"""
    engine.feed_settlements([], checked={EXPIRING: "2026-11-03"})
    assert engine.drain(timeout=5.0)
    feed(engine, au_quote("2026-11-05 21:05:00.000000", 831.0, pre_settlement=829.0,
                          symbol=SECOND_SYMBOL))
    assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-06"
    engine.feed_settlements([], checked={EXPIRING: "2026-11-03"})
    assert engine.drain(timeout=5.0)


def test_estimate_takes_the_later_quote_of_the_same_day_not_the_higher_one(tmp_path):
    """#527 暂估选价（#928 复核 P2-A）：挂起时记下的是 11-03 14:59:00 的 824，随后到的
    同日 14:59:59 真实价 822 更新——暂估按时间较新的 822，不按同日较高的 824。"""
    db = _open_expiring_and_far_then_cold_close(tmp_path)
    engine = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-05T09:00:00+08:00")))
    try:
        feed(engine, au_quote("2026-11-03 14:59:00.000000", 824.0, expire_ns=EXPIRY_1103_NS))
        feed(engine, au_quote("2026-11-05 09:31:00.000000", 830.0, pre_settlement=828.0,
                              symbol=SECOND_SYMBOL))
        [pending] = engine.account(ACCOUNT)["expirySettlements"]
        assert pending["status"] == "pending"
        feed(engine, au_quote("2026-11-03 14:59:59.000000", 822.0, expire_ns=EXPIRY_1103_NS))
        _estimate_after_one_more_day(engine)
        [estimated] = engine.account(ACCOUNT)["expirySettlements"]
        assert estimated["status"] == "estimated"
        assert estimated["estimate"]["price"] == pytest.approx(822.0)
        assert estimated["estimate"]["at"].startswith("2026-11-03T14:59:59")
        [trade] = _expiry_trades(engine)
        assert trade["price"] == pytest.approx(822.0)
    finally:
        engine.close()


def test_estimate_basis_received_while_pending_survives_a_restart(tmp_path):
    """#527 暂估选价（#928 复核 P2-B）：挂起时只有停机前 11-02 的 820；挂起中收到最后交易日
    的真实价 822，随即持久化进挂起记录。暂估前冷重启、且重启后不再收到 au2612 行情，
    暂估仍按 822，不退回 820。"""
    db = _open_expiring_and_far_then_cold_close(tmp_path)
    engine = _restart_past_expiry(db)
    try:
        feed(engine, au_quote("2026-11-03 14:59:59.000000", 822.0, expire_ns=EXPIRY_1103_NS))
        engine.feed_settlements([], checked={EXPIRING: "2026-11-03"})
        assert engine.drain(timeout=5.0)
        feed(engine, au_quote("2026-11-05 21:05:00.000000", 831.0, pre_settlement=829.0,
                              symbol=SECOND_SYMBOL))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-06"
        assert _volumes(engine)[EXPIRING] == 1
    finally:
        engine.close()

    restarted = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-05T21:10:00+08:00")))
    try:
        feed(restarted, au_quote("2026-11-05 21:10:00.000000", 831.0, symbol=SECOND_SYMBOL))
        restarted.feed_settlements([], checked={EXPIRING: "2026-11-03"})
        assert restarted.drain(timeout=5.0)
        assert EXPIRING not in _volumes(restarted)
        [estimated] = restarted.account(ACCOUNT)["expirySettlements"]
        assert estimated["estimate"]["price"] == pytest.approx(822.0)
        assert restarted.account(ACCOUNT)["closeProfit"] == pytest.approx((822.0 - 820.02) * 1000)
    finally:
        restarted.close()


def test_source_queries_the_last_trading_day_by_date(tmp_path):
    """行情源按日期补查：query_symbol_settlement(合约, days=1, start_dt=最后交易日)，
    返回行的交易所内代码映射回完整代码；查询失败也算“今天补查过”（暂估据此判断）。"""
    from datetime import date
    from types import SimpleNamespace

    from tqkq_readonly_sidecar.paper import TqQuoteSource

    db = _open_expiring_and_far_then_cold_close(tmp_path)
    engine = _restart_past_expiry(db)
    calls = []

    class FailingApi:
        def query_symbol_settlement(self, symbols, days=1, start_dt=None):
            calls.append((symbols, days, start_dt))
            raise RuntimeError("settlement service down")

    class SettlementApi:
        def query_symbol_settlement(self, symbols, days=1, start_dt=None):
            calls.append((symbols, days, start_dt))
            return SimpleNamespace(to_dict=lambda orient: [
                {"symbol": "au2612", "datetime": "20261103", "settlement": 824.0}])

    try:
        source = TqQuoteSource(lambda: ("user", "pass"))
        source._query_final_settlements(engine, FailingApi())
        assert engine.drain(timeout=5.0)
        assert engine._final_checked == {EXPIRING: "2026-11-05"}
        assert _volumes(engine)[EXPIRING] == 1
        source._query_final_settlements(engine, SettlementApi())
        assert engine.drain(timeout=5.0)
        assert calls == [([EXPIRING], 1, date(2026, 11, 3))] * 2
        assert EXPIRING not in _volumes(engine)
        assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((824.0 - 820.02) * 1000)
    finally:
        engine.close()


def test_check_db_covers_the_suspended_expiry_records(tmp_path):
    """--check-db：挂起记录随开库解析；pending 的合约账本里没持仓就判不一致，坏记录开库即失败。"""
    from tqkq_readonly_sidecar.paper import check_db

    db = _open_expiring_and_far_then_cold_close(tmp_path)
    _restart_past_expiry(db).close()
    report = check_db(str(db))
    assert report["status"] == "ok" and report["expirySettlementsOpen"] == 1
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE paper_expiry_settlements SET symbol='SHFE.au2610'")
    report = check_db(str(db))
    assert report["status"] == "mismatch"
    assert any("pending expiry SHFE.au2610 has no position" in item for item in report["mismatches"])
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE paper_expiry_settlements SET record_json='[]'")
    with pytest.raises(RuntimeError, match="paper_expiry_record_corrupt"):
        check_db(str(db))


def _open_two_leg_positions(engine: PaperEngine) -> None:
    feed(engine, au_quote("09:31:00.000000", 820.0))
    feed(engine, au_quote("09:31:00.000000", 820.0, symbol=SECOND_SYMBOL))
    engine.submit(ACCOUNT, submit_request("order-same-day-a"))
    engine.submit(ACCOUNT, submit_request("order-same-day-b", symbol=SECOND_SYMBOL))
    assert len(engine.positions(ACCOUNT)) == 2


def test_same_day_quote_does_not_override_the_query_settlement_price(tmp_path):
    """#527 原例 B：账户持 A/B。换日后 query 先给 A 的 11-02 正式价 825、B 缺价
    pending；夜盘 quote 归 11-03 但 pre_settlement 同属 11-02、值 824——同日低
    优先级（quote）不得覆盖 query；B 补价后 A 仍按 825 结账。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        _open_two_leg_positions(engine)
        feed(engine, au_quote("18:31:00.000000", 820.5))  # 18 点换日，结算推迟
        engine.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-02", "settlement": 825.0}])
        assert engine.drain(timeout=5.0)
        assert engine.settlement_count(ACCOUNT) == 0  # B 缺价：整体等待
        # 夜盘行情的 pre_settlement 属 11-02，值 824：不得覆盖 query 的 825。
        feed(engine, au_quote("2026-11-02 21:05:00.000000", 821.0, pre_settlement=824.0))
        assert engine.settlement_count(ACCOUNT) == 0
        # B 的正式价到达：A 按 825、B 按 830 结算。
        engine.feed_settlements([
            {"symbol": SECOND_SYMBOL, "tradingDay": "2026-11-02", "settlement": 830.0}])
        assert engine.drain(timeout=5.0)
        assert engine.settlement_count(ACCOUNT) == 1
        assert trade_log_sources(engine, "2026-11-02") == {
            "SHFE.au2612": "settlement", SECOND_SYMBOL: "settlement"}
        assert engine.account(ACCOUNT)["staticBalance"] == pytest.approx(
            1_000_000.0 - 40.0 + (825.0 - 820.02) * 1000 + (830.0 - 820.02) * 1000)
    finally:
        engine.close()


def test_later_query_upgrades_a_same_day_quote_settlement_price(tmp_path):
    """#527 原例 B 反向（合同保留项）：同日 quote 先到（824）、账户因另一合约
    缺价仍在等待时 query 到达（825）——同日高优先级应升级，A 最终按 825 结账。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        _open_two_leg_positions(engine)
        feed(engine, au_quote("18:31:00.000000", 820.5))
        feed(engine, au_quote("2026-11-02 21:05:00.000000", 821.0, pre_settlement=824.0))
        assert engine.settlement_count(ACCOUNT) == 0  # B 缺价：整体等待
        engine.feed_settlements([
            {"symbol": "SHFE.au2612", "tradingDay": "2026-11-02", "settlement": 825.0},
            {"symbol": SECOND_SYMBOL, "tradingDay": "2026-11-02", "settlement": 830.0}])
        assert engine.drain(timeout=5.0)
        assert engine.settlement_count(ACCOUNT) == 1
        assert engine.account(ACCOUNT)["staticBalance"] == pytest.approx(
            1_000_000.0 - 40.0 + (825.0 - 820.02) * 1000 + (830.0 - 820.02) * 1000)
    finally:
        engine.close()


def test_deferred_settlement_never_uses_an_intraday_stale_quote(tmp_path):
    """只有早于收盘的旧报价、没有任何结算价来源：整体推迟结算，撮合暂停，
    绝不拿旧报价补位。"""
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-stale-only"))
        feed(engine, au_quote("14:06:55.000000", 821.0))
        feed(engine, au_quote("2026-11-03 09:31:00.000000", 820.0))
        assert engine.settlement_count(ACCOUNT) == 0
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-02"
        assert engine.account(ACCOUNT)["tradingDay"] == "2026-11-02"  # 如实反映账本日
        with pytest.raises(RuntimeError, match="paper_settlement_pending"):
            engine.submit(ACCOUNT, submit_request("order-stale-blocked"))
    finally:
        engine.close()





def test_finished_orders_queryable_beyond_two_trading_days(tmp_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-aged"))
        feed(engine, au_quote("2026-11-03 09:31:00.000000", 821.0, settlement=821.0))
        feed(engine, au_quote("2026-11-04 09:31:00.000000", 820.0, settlement=820.0))
        assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-04"
        assert "order-aged" not in engine._accounts[ACCOUNT].archived_orders
        report = engine.query_order(ACCOUNT, "order-aged")
        assert report["found"] is True
        assert report["order"]["status"] == "FINISHED"
        assert report["order"]["filledVolume"] == 1
    finally:
        engine.close()


def test_replay_after_memory_trim_returns_persisted_order_without_matching(tmp_path):
    # #473：内存归档只留两天，第三个交易日重放同一命令 / 同一 clientOrderId 也不能再撮合。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    second = "paper:0000000000b2"
    try:
        engine.create_account(second)
        feed(engine, au_quote("09:31:00.000000", 820.0))
        first = engine.submit(ACCOUNT, submit_request("order-replay"))
        assert first["order"]["filledVolume"] == 1
        feed(engine, au_quote("2026-11-03 09:31:00.000000", 821.0, settlement=821.0))
        feed(engine, au_quote("2026-11-04 09:31:00.000000", 820.0, settlement=820.0))
        assert "order-replay" not in engine._accounts[ACCOUNT].archived_orders
        before = engine.account(ACCOUNT)
        before = (before["available"], before["commission"])
        same_command = engine.submit(ACCOUNT, submit_request("order-replay"))
        same_order = engine.submit(ACCOUNT, submit_request(
            "order-replay", commandId="cmd-order-replay-again"))
        assert same_command["order"]["clientOrderId"] == "order-replay"
        assert same_order == same_command
        assert [item["volume"] for item in engine.positions(ACCOUNT)] == [1]
        after = engine.account(ACCOUNT)
        assert (after["available"], after["commission"]) == before
        # 同号订单在别的账户是另一笔；未知订单号照常提交。
        other = engine.submit(second, submit_request("order-replay"))
        assert other["order"]["filledVolume"] == 1
        fresh = engine.submit(ACCOUNT, submit_request("order-fresh"))
        assert fresh["order"]["filledVolume"] == 1
    finally:
        engine.close()
    restarted = build_engine(db, clock=VirtualClock(
        datetime.fromisoformat("2026-11-04T10:00:00+08:00")))
    try:
        feed(restarted, au_quote("2026-11-04 10:31:00.000000", 820.0))
        replay = restarted.submit(ACCOUNT, submit_request(
            "order-replay", commandId="cmd-order-replay-third"))
        assert replay["order"]["clientOrderId"] == "order-replay"
        assert sum(item["volume"] for item in restarted.positions(ACCOUNT)) == 2
    finally:
        restarted.close()


# -- 审查 527-4/5/6：关停、到期缺价与停车恢复 --------------------------------


def test_close_keeps_the_db_and_lock_open_when_the_actor_is_stuck(tmp_path):
    # 527-4：join 超时说明 actor 卡在行情处理里——数据库连接与 flock 不关闭，
    # 交给进程退出回收；关掉句柄会让还活着的 actor 写进已关闭的连接。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    stuck = threading.Event()
    release = threading.Event()
    original = engine._apply_quote

    def blocking(quote):
        stuck.set()
        release.wait(timeout=10)
        return original(quote)

    engine._apply_quote = blocking
    try:
        engine.feed_quote(au_quote("09:31:00.000000", 820.0))
        assert stuck.wait(timeout=5.0)
        engine.close(join_seconds=0.1)
        assert engine._thread.is_alive()
        assert engine._db is not None and engine._lock_fd is not None
    finally:
        release.set()
        engine._thread.join(timeout=5.0)
    assert not engine._thread.is_alive()


def test_expiry_without_a_price_waits_for_the_next_settlement(engine):
    # 527-5 的精神在 2026-10-08 结算改版后的形态：到期结算没有该日正式结算价时，
    # 整个账户推迟结算（不拿持仓成本或旧报价冒充结算价），等下一次带结算价的
    # 行情再结算并按真实价格强平。合约 11-03 到期：11-03 目标日缺价先 pending，
    # 11-04 的行情带 11-03 正式结算价（普通目标日 11-03 恰与到期日同日）再结算。
    feed(engine, au_quote("09:31:00.000000", 820.0, expire_ns=EXPIRY_1103_NS))
    engine.submit(ACCOUNT, submit_request("order-expiry-pending"))
    feed(engine, au_quote("2026-11-03 09:31:00.000000", float("nan"),
                          expire_ns=EXPIRY_1103_NS))
    assert engine.settlement_count(ACCOUNT) == 0  # 没有可用价格：推迟结算
    assert engine.account_info(ACCOUNT)["bookTradingDay"] == "2026-11-02"
    assert [item["volume"] for item in engine.positions(ACCOUNT)] == [1]  # 没被强平
    assert [trade for trade in engine.activity(ACCOUNT)["trades"]
            if trade.get("reason") == "expiry"] == []
    # 下一交易日带来结算价：先结算再按结算价强平，盈亏相对结算后持仓成本。
    feed(engine, au_quote("2026-11-04 09:31:00.000000", 823.0, settlement=824.0,
                          expire_ns=EXPIRY_1103_NS))
    expiry_trades = [trade for trade in engine.activity(ACCOUNT)["trades"]
                     if trade.get("reason") == "expiry"]
    assert len(expiry_trades) == 1 and expiry_trades[0]["price"] == pytest.approx(824.0)
    assert engine.positions(ACCOUNT) == []
    assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((824.0 - 820.02) * 1000)


def test_parked_orders_survive_a_kernel_crash_during_off_session_update(engine):
    # 527-6：盘外行情只更新价格，挂单临时停进旁路；SimTrade 抛异常时挂单也要
    # 放回账本（恢复逻辑在 finally 里），而不是消失在停车旁路。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    alive = engine.submit(ACCOUNT, submit_request("order-parked-crash", limitPrice=815.0))
    assert alive["order"]["status"] == "ALIVE"
    sim = engine._accounts[ACCOUNT].book._sim

    def crash(symbol, pack):
        raise RuntimeError("sim kernel crash")

    sim.update_quotes = crash
    engine.feed_quote(au_quote("12:30:00.000000", 810.0))  # 午间休市：走停车路径
    assert engine.drain(timeout=5.0)  # 异常触发 527-3 故障停机，队列照常清空
    assert engine.query_order(ACCOUNT, "order-parked-crash")["order"]["status"] == "ALIVE"


def test_parked_orders_survive_a_crash_during_settlement_marking(engine):
    # 527-6 的 settle 一侧：结算标记阶段抛异常，停进旁路的挂单同样要放回去。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-settle-crash"))
    resting = engine.submit(ACCOUNT, submit_request("order-settle-crash-rest", limitPrice=815.0))
    assert resting["order"]["status"] == "ALIVE"
    sim = engine._accounts[ACCOUNT].book._sim

    def crash(symbol, pack):
        raise RuntimeError("sim kernel crash")

    sim.update_quotes = crash
    engine.feed_quote(au_quote("2026-11-03 09:31:00.000000", 821.0, settlement=821.0))
    assert engine.drain(timeout=5.0)
    assert engine.query_order(ACCOUNT, "order-settle-crash-rest")["order"]["status"] == "ALIVE"


# -- 审查 527-7/8：门口校验与命令行 ------------------------------------------


def test_invalid_volume_multiple_is_rejected_before_the_kernel(engine):
    # 527-7：行情带 NaN 合约乘数时在门口拒单，不让它灌进内核把账目变 NaN。
    quote = au_quote("09:31:00.000000", 820.0)
    quote["volume_multiple"] = float("nan")
    feed(engine, quote)
    order = rejected(engine, submit_request("order-nan-multiple"))
    assert "乘数" in order["lastMsg"]
    account = engine.account(ACCOUNT)
    assert math.isfinite(account["equity"]) and engine.positions(ACCOUNT) == []


def test_command_rows_keep_the_original_request_and_bind_kind(engine):
    # 527-8：命令行只写第一次的原文，重放不覆盖 request_json；commandId 绑定
    # 命令类型，提交命令的 commandId 不能拿来撤单。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-cmd-bind", limitPrice=820.02))
    replay = engine.submit(ACCOUNT, submit_request("order-cmd-bind", limitPrice=825.0))
    assert replay["order"]["status"] == "FINISHED" and replay["order"]["isError"] is False
    probe = sqlite3.connect(f"file:{engine.db_path}?mode=ro", uri=True)
    try:
        row = probe.execute(
            "SELECT kind, request_json FROM paper_commands WHERE account_id=?",
            (ACCOUNT,)).fetchone()
    finally:
        probe.close()
    assert row[0] == "submit"
    assert json.loads(row[1])["limitPrice"] == 820.02  # 原始请求没被重放覆盖
    with pytest.raises(RuntimeError, match="command_kind_mismatch"):
        engine.cancel_order(ACCOUNT, "order-cmd-bind", "cmd-order-cmd-bind")


LEGACY_GHOST = "order-legacy-ghost"


def _command_rows(db) -> list[tuple]:
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return probe.execute(
            "SELECT command_id, client_order_id, kind, request_json, result_json "
            "FROM paper_commands ORDER BY command_id").fetchall()
    finally:
        probe.close()


def _account_row(db) -> tuple:
    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return probe.execute(
            "SELECT revision, state_json FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone()
    finally:
        probe.close()


def _legacy_command_db(tmp_path, *, drop_kind_column: bool):
    """合成迁移前的旧库（#527 D4）：A、B、D 三张挂单由各自 command 提交，C 挂单
    后被 cmd-cancel-order-legacy-c 撤掉；D 的原文判不出类型，另有一行指向不存在
    的原单。kind 列整列删除（更老的库）或全部置空。"""
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        for name in ("order-legacy-a", "order-legacy-b", "order-legacy-c", "order-legacy-d"):
            placed = engine.submit(ACCOUNT, submit_request(name, limitPrice=815.0))
            assert placed["order"]["status"] == "ALIVE"
        engine.cancel_order(ACCOUNT, "order-legacy-c", "cmd-cancel-order-legacy-c")
    finally:
        engine.close()
    probe = sqlite3.connect(db)
    try:
        if drop_kind_column:
            probe.execute("ALTER TABLE paper_commands DROP COLUMN kind")
        else:
            probe.execute("UPDATE paper_commands SET kind=NULL")
        # 判不出类型的原文：缺 _account_id，既不像提交也不像撤单。
        probe.execute(
            "UPDATE paper_commands SET request_json=? WHERE command_id=?",
            (json.dumps({"clientOrderId": "order-legacy-d"}), "cmd-order-legacy-d"))
        probe.execute(
            "INSERT INTO paper_commands (account_id, command_id, client_order_id, "
            "request_json, result_json) VALUES (?,?,?,?,?)",
            (ACCOUNT, "cmd-legacy-ghost", LEGACY_GHOST, "not json", "{}"))
        probe.commit()
    finally:
        probe.close()
    return db, clock


@pytest.mark.parametrize("drop_kind_column", [True, False])
def test_migration_backfills_command_kind_from_the_stored_request(tmp_path, drop_kind_column):
    # #527 D4：迁移按已存原文一次性回填 kind，不改写 request_json；判不出的标
    # legacy；重复启动不再变化。
    db, clock = _legacy_command_db(tmp_path, drop_kind_column=drop_kind_column)
    probe = sqlite3.connect(db)
    try:
        before = probe.execute(
            "SELECT command_id, client_order_id, request_json, result_json "
            "FROM paper_commands ORDER BY command_id").fetchall()
    finally:
        probe.close()
    build_engine(db, clock=clock).close()
    migrated = _command_rows(db)
    assert [(row[0], row[1], row[3], row[4]) for row in migrated] == before
    assert {row[0]: row[2] for row in migrated} == {
        "cmd-cancel-order-legacy-c": "cancel",
        "cmd-legacy-ghost": "legacy",
        "cmd-order-legacy-a": "submit",
        "cmd-order-legacy-b": "submit",
        "cmd-order-legacy-c": "submit",
        "cmd-order-legacy-d": "legacy",
    }
    account = _account_row(db)
    build_engine(db, clock=clock).close()
    assert _command_rows(db) == migrated
    assert _account_row(db) == account


def test_migrated_commands_reject_cross_kind_replay(tmp_path):
    # #550 复核第 3 节原例：旧库 kind 为空时，cancel(B, command-A) 曾撤掉 A。迁移
    # 回填后跨类型重放一律拒绝，两张单、账户快照与命令行都不变，重启后仍拒绝。
    db, clock = _legacy_command_db(tmp_path, drop_kind_column=True)
    for _ in range(2):
        engine = build_engine(db, clock=clock)
        try:
            feed(engine, au_quote("09:32:00.000000", 820.0))
            rows, account = _command_rows(db), _account_row(db)
            with pytest.raises(RuntimeError, match="command_kind_mismatch"):
                engine.cancel_order(ACCOUNT, "order-legacy-b", "cmd-order-legacy-a")
            with pytest.raises(RuntimeError, match="command_kind_mismatch"):
                engine.submit(ACCOUNT, submit_request(
                    "order-legacy-x", commandId="cmd-cancel-order-legacy-c", limitPrice=815.0))
            for name in ("order-legacy-a", "order-legacy-b"):
                assert engine.query_order(ACCOUNT, name)["order"]["status"] == "ALIVE"
            assert engine.query_order(ACCOUNT, "order-legacy-x")["found"] is False
            assert _command_rows(db) == rows and _account_row(db) == account
        finally:
            engine.close()


def test_migrated_commands_replay_their_own_kind_idempotently(tmp_path):
    # #527 D4：回填出类型的命令按原类型重放照常幂等——提交重放返回原单当前状态、
    # 不下第二单；撤单重放返回已撤结果；原文、快照版本都不变。
    db, clock = _legacy_command_db(tmp_path, drop_kind_column=False)
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:32:00.000000", 820.0))
        rows, account = _command_rows(db), _account_row(db)
        replay = engine.submit(ACCOUNT, submit_request("order-legacy-a", limitPrice=816.0))
        assert replay["order"]["status"] == "ALIVE"
        assert replay["order"]["limitPrice"] == pytest.approx(815.0)
        cancelled = engine.cancel_order(
            ACCOUNT, "order-legacy-c", "cmd-cancel-order-legacy-c")
        assert cancelled["order"]["status"] == "FINISHED"
        assert _command_rows(db) == rows and _account_row(db) == account
    finally:
        engine.close()


def test_legacy_command_rows_only_read_back_the_original_order(tmp_path):
    # #527 D4：判不出类型的 legacy 行只按原单只读查询：提交重放回原单（不按新
    # clientOrderId 下单），撤单重放不撤活单，原单不存在的拒绝；行不被改写。
    db, clock = _legacy_command_db(tmp_path, drop_kind_column=True)
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:32:00.000000", 820.0))
        rows, account = _command_rows(db), _account_row(db)
        read_back = engine.submit(ACCOUNT, submit_request(
            "order-legacy-new", commandId="cmd-order-legacy-d", limitPrice=815.0))
        assert read_back["order"]["clientOrderId"] == "order-legacy-d"
        assert read_back["order"]["status"] == "ALIVE"
        assert engine.query_order(ACCOUNT, "order-legacy-new")["found"] is False
        with pytest.raises(RuntimeError, match="command_kind_mismatch"):
            engine.cancel_order(ACCOUNT, "order-legacy-d", "cmd-order-legacy-d")
        assert engine.query_order(ACCOUNT, "order-legacy-d")["order"]["status"] == "ALIVE"
        with pytest.raises(RuntimeError, match="command_kind_mismatch"):
            engine.submit(ACCOUNT, submit_request(
                LEGACY_GHOST, commandId="cmd-legacy-ghost", limitPrice=815.0))
        assert engine.query_order(ACCOUNT, LEGACY_GHOST)["found"] is False
        assert _command_rows(db) == rows and _account_row(db) == account
        # 原单结束后，legacy 撤单重放也只读回它的终态。
        engine.cancel_order(ACCOUNT, "order-legacy-d", "cmd-cancel-order-legacy-d")
        ended = engine.cancel_order(ACCOUNT, "order-legacy-x", "cmd-order-legacy-d")
        assert ended["order"]["clientOrderId"] == "order-legacy-d"
        assert ended["order"]["status"] == "FINISHED"
        legacy = [row for row in _command_rows(db) if row[0] == "cmd-order-legacy-d"]
        assert legacy == [row for row in rows if row[0] == "cmd-order-legacy-d"]
    finally:
        engine.close()


# -- 审查 527-11/12/13：快照版本、乱序行情与重启幂等 --------------------------


def test_state_snapshot_carries_a_version_and_refuses_newer(tmp_path):
    # 527-11：快照带版本号；restore 拒绝比自己新的版本，老快照（无版本号）照收。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    book = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=clock,
                     trading_dates=lambda: TRADING_DATES)
    state = book.export_state()
    assert state["version"] == BASE_STATE_VERSION  # 没有部分成交活单：旧版本也能读
    book.restore_state(state)  # 自己导出的快照总能恢复
    fresh = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=clock,
                      trading_dates=lambda: TRADING_DATES)
    with pytest.raises(RuntimeError, match="paper_state_version_too_new"):
        fresh.restore_state({**state, "version": STATE_VERSION + 1})
    fresh.restore_state({key: value for key, value in state.items() if key != "version"})


def test_account_info_reports_the_persisted_revision(engine):
    # 527-11：account_info 的 revision 读库里真实的落盘版本，不再恒为 0。
    assert engine.account_info(ACCOUNT)["revision"] == 0
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-revision"))
    assert engine.account_info(ACCOUNT)["revision"] == 1
    feed(engine, au_quote("2026-11-03 09:31:00.000000", 821.0, settlement=821.0))
    assert engine.account_info(ACCOUNT)["revision"] == 2  # 跨日结算也是一次落盘


def test_out_of_order_quote_is_dropped_not_matched(engine):
    # 527-12：乱序迟到的旧行情整笔丢弃——缓存价格不倒退，也不驱动撮合。
    feed(engine, au_quote("10:00:00.000000", 830.0))
    alive = engine.submit(ACCOUNT, submit_request("order-stale-quote", limitPrice=825.0))
    assert alive["order"]["status"] == "ALIVE"
    feed(engine, au_quote("09:31:00.000000", 820.0))  # 迟到的 09:31 旧行情
    assert engine.query_order(ACCOUNT, "order-stale-quote")["order"]["status"] == "ALIVE"
    assert engine.market_quote(ACCOUNT, "SHFE.au2612")["lastPrice"] == 830.0
    # 更新的行情照常入账并撮合。
    feed(engine, au_quote("10:01:00.000000", 824.0))
    assert engine.query_order(ACCOUNT, "order-stale-quote")["order"]["status"] == "FINISHED"


def test_same_command_with_a_new_coid_after_restart_executes_once(tmp_path):
    # 527-13：命令行与账本快照同一事务（527-1）之后，“快照已提交、命令未写”的
    # 崩溃窗口不复存在——重启后同一 commandId 换 coid 重试仍指回原来那笔，
    # 不会第二次成交。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        first = engine.submit(ACCOUNT, submit_request("order-once"))
        assert first["order"]["filledVolume"] == 1
    finally:
        engine.close()
    restarted = build_engine(db, clock=clock)
    try:
        feed(restarted, au_quote("09:32:00.000000", 820.0))
        retry = restarted.submit(ACCOUNT, submit_request(
            "order-once-other", commandId="cmd-order-once"))
        assert retry["order"]["clientOrderId"] == "order-once"  # 还是原来那笔
        assert [item["volume"] for item in restarted.positions(ACCOUNT)] == [1]
    finally:
        restarted.close()


# -- feeRate：本站模拟盘按 Runtime 生效费率计佣金（2026-10-08） ---------------------------


def rb_quote(moment: str, last: float, **overrides) -> dict:
    au_keys = {"bid", "ask", "settlement"}
    quote = au_quote(moment, last, symbol="SHFE.rb2701", margin=4_000.0, commission=0.3116,
                     **{key: value for key, value in overrides.items() if key in au_keys})
    quote.update({"product_id": "rb", "volume_multiple": 10.0, "price_tick": 1.0,
                  "upper_limit": 3300.0, "lower_limit": 2800.0, "instrument_name": "螺纹钢2701"})
    quote.update({key: value for key, value in overrides.items() if key not in au_keys})
    return quote


# 对拍常量（runtime test_exchange_fees 用同一组数字）：rb 万分之1 开 1 手 @3071 = 3.07。
RB_RATE = {"mode": "notional", "open": 1.0, "closeYesterday": 1.0, "closeToday": 1.0,
           "openPerLotExtra": 0.0, "closeYesterdayPerLotExtra": 0.0, "closeTodayPerLotExtra": 0.0}
# IC：开/平万分之0.23、平今万分之2.3（对拍：平今 @6000 × 乘数200 = 276.0）。
IC_RATE = {"mode": "notional", "open": 0.23, "closeYesterday": 0.23, "closeToday": 2.3,
           "openPerLotExtra": 0.0, "closeYesterdayPerLotExtra": 0.0, "closeTodayPerLotExtra": 0.0}


def test_fee_rate_orders_charge_the_rate_and_others_keep_quote_commission(engine):
    # 不带 feeRate 的单保持 SimTrade 原口径：quote 每手 20 → 开 1 手扣 20。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-no-rate"))
    assert engine.account(ACCOUNT)["commission"] == 20.0

    # 带 feeRate 的 rb 单按交易所标准计：万分之1 × 3071 × 10 = 3.07，不是行情源的 0.3116。
    feed(engine, rb_quote("09:41:00.000000", 3071.0, ask=3071.0))
    opened = engine.submit(ACCOUNT, submit_request(
        "order-rb-open", symbol="SHFE.rb2701", limitPrice=3071.0, feeRate=RB_RATE))
    assert opened["order"]["isError"] is False and opened["order"]["filledVolume"] == 1
    # 平今同费率：3.07。
    feed(engine, rb_quote("09:51:00.000000", 3071.0, bid=3071.0))
    engine.submit(ACCOUNT, submit_request(
        "order-rb-close-today", symbol="SHFE.rb2701", direction="SELL", offset="CLOSETODAY",
        limitPrice=3071.0, feeRate=RB_RATE))
    assert engine.account(ACCOUNT)["commission"] == pytest.approx(20.0 + 3.07 + 3.07)


def test_activity_trades_carry_the_real_commission_through_serialization(engine):
    """逐笔佣金序列化 (2026-10-10)：feeRate 单的成交经正式 activity 序列化（PaperBook
    → OfficialTqKqSession._activity_trade）把实际佣金带出去，Runtime 的成交事件不中途丢；
    不带 feeRate 的单也带 SimTrade 自己算的每手佣金（quote 20 → 开 1 手 20），字段不缺失。"""
    feed(engine, rb_quote("09:31:00.000000", 3071.0, ask=3071.0))
    engine.submit(ACCOUNT, submit_request(
        "order-rb-open", symbol="SHFE.rb2701", limitPrice=3071.0, feeRate=RB_RATE))
    feed(engine, au_quote("09:41:00.000000", 820.0, ask=820.0))
    engine.submit(ACCOUNT, submit_request("order-au-open"))
    by_order = {trade["clientOrderId"]: trade for trade in engine.activity(ACCOUNT)["trades"]}
    assert by_order["order-rb-open"]["commission"] == pytest.approx(3.07)
    assert by_order["order-au-open"]["commission"] == pytest.approx(20.0)


def test_ic_close_charges_the_ten_times_close_today_rate(engine):
    ic = au_quote("09:31:00.000000", 6000.0, ask=6000.0, symbol="CFFEX.IC2612",
                  margin=100_000.0, commission=252.0)
    ic.update({"product_id": "IC", "volume_multiple": 200.0, "price_tick": 1.0,
               "upper_limit": 6600.0, "lower_limit": 5400.0, "instrument_name": "中证500期货2612"})
    feed(engine, ic)
    # 开仓万分之0.23：0.23 × 6000 × 200 / 10000 = 27.6。
    engine.submit(ACCOUNT, submit_request(
        "order-ic-open", symbol="CFFEX.IC2612", limitPrice=6000.0, feeRate=IC_RATE))
    assert engine.account(ACCOUNT)["commission"] == pytest.approx(27.6)
    # 中金所 CLOSE 不分今昨字段，但今仓按 SimTrade 真实平仓顺序落在平今一栏：
    # 万分之2.3 × 6000 × 200 / 10000 = 276.0（不是 quote 的 252）。
    feed(engine, {**ic, "datetime": "2026-11-02 09:41:00.000000",
                  "last_price": 6000.0, "bid_price1": 6000.0, "ask_price1": 6000.0})
    engine.submit(ACCOUNT, submit_request(
        "order-ic-close", symbol="CFFEX.IC2612", direction="SELL",
        offset="CLOSE", limitPrice=6000.0, feeRate=IC_RATE))
    assert engine.account(ACCOUNT)["commission"] == pytest.approx(27.6 + 276.0)


def test_non_shfe_close_splits_yesterday_first_then_today(engine):
    """非上期所/能源中心的 CLOSE 按 SimTrade 的真实平仓顺序（先昨后期）分别计费：
    持仓昨 2 今 1 时平 3 手 = 2×平昨 + 1×平今，不是整笔按同一栏。"""
    iron = au_quote("2026-11-02 09:31:00.000000", 820.0, symbol="DCE.i2609")
    iron.update({"price_tick": 0.5, "volume_multiple": 100.0,
                 "margin": 60_000.0, "commission": 8.0, "product_id": "i"})
    rate = {"mode": "per_lot", "open": 1.0, "closeYesterday": 2.0, "closeToday": 10.0,
            "openPerLotExtra": 0.0, "closeYesterdayPerLotExtra": 0.0, "closeTodayPerLotExtra": 0.0}
    feed(engine, iron)
    engine.submit(ACCOUNT, submit_request(
        "order-i-open-1", symbol="DCE.i2609", volume=2, limitPrice=820.5, feeRate=rate))
    # 收盘结算把今仓转昨仓，第二天再开 1 手 → 昨 2 今 1。
    settle = au_quote("2026-11-02 15:10:00.000000", 824.5, symbol="DCE.i2609",
                      settlement=825.0, margin=60_000.0, commission=8.0)
    settle.update({"price_tick": 0.5, "volume_multiple": 100.0, "product_id": "i"})
    feed(engine, settle)
    # 第二天首笔行情带 11-02 的正式结算价（2026-10-11 起收盘价不再兜底）。
    day2 = au_quote("2026-11-03 09:31:00.000000", 825.0, symbol="DCE.i2609",
                    pre_settlement=825.0)
    day2.update({"price_tick": 0.5, "volume_multiple": 100.0,
                 "margin": 60_000.0, "commission": 8.0, "product_id": "i"})
    feed(engine, day2)
    engine.submit(ACCOUNT, submit_request(
        "order-i-open-2", symbol="DCE.i2609", limitPrice=825.5, feeRate=rate))
    # 3 手 CLOSE：昨 2 手（先冻结）×2 + 今 1 手×10 = 14。
    feed(engine, {**day2, "datetime": "2026-11-03 10:00:00.000000",
                  "last_price": 824.5, "ask_price1": 825.0, "bid_price1": 824.5})
    closed = engine.submit(ACCOUNT, submit_request(
        "order-i-close", symbol="DCE.i2609", direction="SELL", offset="CLOSE",
        volume=3, limitPrice=824.5, feeRate=rate))
    assert closed["order"]["isError"] is False
    # 11-03 当日：开 1×1 + 平 3 手（昨 2×2 + 今 1×10）= 15；昨仓那两笔是前一天开的（前日已结清）。
    assert engine.account(ACCOUNT)["commission"] == pytest.approx(1.0 + 14.0)


def test_fee_rate_survives_a_restart_on_a_resting_order(tmp_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    service = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock, trading_dates=TRADING_DATES)
    service.open()
    service.start()
    service.create_account(ACCOUNT)
    feed(service, rb_quote("09:31:00.000000", 3071.0))
    resting = service.submit(ACCOUNT, submit_request(
        "order-rest-rate", symbol="SHFE.rb2701", limitPrice=3050.0, feeRate=RB_RATE))
    assert resting["order"]["status"] == "ALIVE"
    service.close()

    # 重启后挂单成交仍按费率计佣金（feeRate 随订单进快照重建）：万分之1 × 3050 × 10 = 3.05。
    revived = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"),
                          clock=VirtualClock(datetime.fromisoformat("2026-11-02T10:00:00+08:00")),
                          trading_dates=TRADING_DATES)
    revived.open()
    revived.start()
    feed(revived, rb_quote("10:31:00.000000", 3049.0, ask=3050.0))
    assert revived.account(ACCOUNT)["commission"] == pytest.approx(3.05)
    assert next(item for item in revived.positions(ACCOUNT) if item["symbol"] == "SHFE.rb2701")["volume"] == 1
    revived.close()


# -- 到期强平按开仓费率收平仓费（#643 / #527 PR-3） --------------------------------

# 平昨与开仓、平今不同，才能看出强平用的是哪一栏：开 万分之1、平昨 万分之1.5、平今 万分之3。
RB_EXPIRY_RATE = {"mode": "notional", "open": 1.0, "closeYesterday": 1.5, "closeToday": 3.0,
                  "openPerLotExtra": 0.0, "closeYesterdayPerLotExtra": 0.0,
                  "closeTodayPerLotExtra": 0.0}


def as_legacy_db(db_path) -> None:
    """把（已正常关闭、检查点已合并的）库改回增量落盘之前的形状：只有整本快照
    state_json，没有当前状态行、成交行与 checkpoint_of——旧版本留下的库就是这样。"""
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE paper_accounts SET checkpoint_of=NULL")
        connection.execute("DELETE FROM paper_account_heads")
        connection.execute("DELETE FROM paper_trades")


def _strip_open_fee_rates(db_path) -> None:
    """把落盘快照改成本次修复之前的形状：持仓上没有开仓费率记录。"""
    as_legacy_db(db_path)
    with sqlite3.connect(db_path) as connection:
        (state_json,) = connection.execute(
            "SELECT state_json FROM paper_accounts WHERE id=?", (ACCOUNT,)).fetchone()
        state = json.loads(state_json)
        for position in state["_positions"].values():
            position.pop("fee_rate_long", None)
            position.pop("fee_rate_short", None)
        connection.execute("UPDATE paper_accounts SET state_json=? WHERE id=?",
                           (json.dumps(state), ACCOUNT))


def _trade_log(engine: PaperEngine, trading_day: str) -> dict:
    with sqlite3.connect(engine.db_path) as connection:
        row = connection.execute(
            "SELECT log_json FROM paper_trade_log WHERE account_id=? AND trading_day=?",
            (ACCOUNT, trading_day)).fetchone()
    assert row is not None, f"no trade log for {trading_day}"
    return json.loads(row[0])


@pytest.mark.parametrize("path", ["live", "restart", "legacy-snapshot"])
def test_expiry_close_charges_the_open_fee_rate(tmp_path, path):
    """#643：到期强平单带开仓单的 feeRate，按平昨一栏收费（上期所到期强平是 CLOSE
    平昨），不是行情源每手手续费。重启后费率随快照恢复；旧快照（持仓上还没有
    费率记录）沿用行情手续费，日结的 expiryFeeSources 如实标明，不回算历史。"""
    db = tmp_path / "paper.sqlite3"
    engine = build_engine(db, clock=VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00")))
    try:
        feed(engine, rb_quote("09:41:00.000000", 3071.0, ask=3071.0, expire_datetime=EXPIRY_1102_NS))
        opened = engine.submit(ACCOUNT, submit_request(
            "order-rb-expiry-open", symbol="SHFE.rb2701", volume=2, limitPrice=3071.0,
            feeRate=RB_EXPIRY_RATE))
        assert opened["order"]["filledVolume"] == 2
        # 开仓：万分之1 × 3071 × 10 × 2 = 6.142 → 6.14。
        assert engine.account(ACCOUNT)["commission"] == pytest.approx(6.14)
        if path != "live":
            engine.close()
            if path == "legacy-snapshot":
                _strip_open_fee_rates(db)
            engine = build_engine(db, clock=VirtualClock(
                datetime.fromisoformat("2026-11-03T09:30:00+08:00")))
        # 11-03 首笔行情带 11-02（= 到期日）的正式结算价 3080：结算并按 3080 强平。
        feed(engine, rb_quote("2026-11-03 09:31:00.000000", 3080.0, settlement=3080.0,
                              expire_datetime=EXPIRY_1102_NS))
        assert engine.settlement_count(ACCOUNT) == 1
        assert engine.positions(ACCOUNT) == []
        expiry = [item for item in engine.activity(ACCOUNT)["trades"] if item.get("reason") == "expiry"]
        assert len(expiry) == 1 and expiry[0]["volume"] == 2
        log = _trade_log(engine, "2026-11-02")
        if path == "legacy-snapshot":
            # 旧快照：行情每手 0.3116 × 2 手 = 0.6232，来源标明。
            assert expiry[0]["commission"] == pytest.approx(0.6232)
            assert log["expiryFeeSources"] == {"SHFE.rb2701:long": "quote-commission"}
        else:
            # 平昨：万分之1.5 × 3080 × 10 × 2 = 9.24（行情口径只有 0.62，平今口径 18.48）。
            assert expiry[0]["commission"] == pytest.approx(9.24)
            assert engine.account(ACCOUNT)["commission"] == pytest.approx(9.24)
            assert log["expiryFeeSources"] == {"SHFE.rb2701:long": "open-fee-rate"}
        assert log["settlementSources"] == {"SHFE.rb2701": "expiry-carry"}
    finally:
        engine.close()


def test_open_fee_rate_record_follows_the_latest_open_and_clears_when_flat(engine):
    """持仓上的开仓费率记录：最近一笔开仓决定口径（不带费率的开仓清掉记录，
    回到行情手续费口径），该方向平到 0 时清掉，不留给下一次开仓。"""
    feed(engine, rb_quote("09:41:00.000000", 3071.0, ask=3071.0, bid=3070.0))
    engine.submit(ACCOUNT, submit_request(
        "order-rb-rate-1", symbol="SHFE.rb2701", limitPrice=3071.0, feeRate=RB_EXPIRY_RATE))
    position = engine._accounts[ACCOUNT].book._sim._positions["SHFE.rb2701"]
    assert position["fee_rate_long"] == RB_EXPIRY_RATE
    engine.submit(ACCOUNT, submit_request(
        "order-rb-rate-2", symbol="SHFE.rb2701", limitPrice=3071.0))
    assert "fee_rate_long" not in position
    engine.submit(ACCOUNT, submit_request(
        "order-rb-rate-3", symbol="SHFE.rb2701", limitPrice=3071.0, feeRate=RB_RATE))
    assert position["fee_rate_long"] == RB_RATE
    engine.submit(ACCOUNT, submit_request(
        "order-rb-rate-close", symbol="SHFE.rb2701", direction="SELL", offset="CLOSETODAY",
        volume=2, limitPrice=3070.0, feeRate=RB_RATE))
    assert "fee_rate_long" in position  # 还剩 1 手
    engine.submit(ACCOUNT, submit_request(
        "order-rb-rate-close-2", symbol="SHFE.rb2701", direction="SELL", offset="CLOSETODAY",
        limitPrice=3070.0, feeRate=RB_RATE))
    assert "fee_rate_long" not in position


def test_non_shfe_close_fee_bucket_matches_the_lots_simtrade_closes(engine):
    """PR #643 review r4217434932 复核：非上期所/能源中心 CLOSE 按整个持仓的冻结
    昨仓量拆今昨费率，是否会在“先挂的平仓单冻住昨仓、后到的平仓单先成交”时
    收错？SimTrade 成交时同样按持仓级 frozen_his 扣昨仓（交易所先开先平：成交
    那一刻还有昨仓就先平昨），费率分栏与它实际扣掉的昨/今手数逐笔一致。"""
    iron = au_quote("2026-11-02 09:31:00.000000", 820.0, symbol="DCE.i2609")
    iron.update({"price_tick": 0.5, "volume_multiple": 100.0,
                 "margin": 60_000.0, "commission": 8.0, "product_id": "i"})
    rate = {"mode": "per_lot", "open": 1.0, "closeYesterday": 2.0, "closeToday": 10.0,
            "openPerLotExtra": 0.0, "closeYesterdayPerLotExtra": 0.0, "closeTodayPerLotExtra": 0.0}
    feed(engine, iron)
    engine.submit(ACCOUNT, submit_request(
        "order-i-open-1", symbol="DCE.i2609", volume=2, limitPrice=820.5, feeRate=rate))
    day2 = au_quote("2026-11-03 09:31:00.000000", 825.0, symbol="DCE.i2609", pre_settlement=825.0)
    day2.update({"price_tick": 0.5, "volume_multiple": 100.0,
                 "margin": 60_000.0, "commission": 8.0, "product_id": "i"})
    feed(engine, day2)
    engine.submit(ACCOUNT, submit_request(
        "order-i-open-2", symbol="DCE.i2609", limitPrice=825.5, feeRate=rate))
    feed(engine, {**day2, "datetime": "2026-11-03 10:00:00.000000",
                  "last_price": 824.5, "ask_price1": 825.0, "bid_price1": 824.5})
    # A：先挂一张平 2 手的高价单，冻住全部 2 手昨仓。
    resting = engine.submit(ACCOUNT, submit_request(
        "order-i-close-rest", symbol="DCE.i2609", direction="SELL", offset="CLOSE",
        volume=2, limitPrice=830.0, feeRate=rate))
    assert resting["order"]["status"] == "ALIVE"
    position = by_direction_long(engine)
    assert (position["frozenHistorical"], position["frozenToday"]) == (2, 0)
    # B：后到的平 1 手立即成交（冻结落在今仓）；成交时仍持有昨仓 → 平昨。
    engine.submit(ACCOUNT, submit_request(
        "order-i-close-hit", symbol="DCE.i2609", direction="SELL", offset="CLOSE",
        volume=1, limitPrice=824.5, feeRate=rate))
    position = by_direction_long(engine)
    assert (position["historicalVolume"], position["todayVolume"]) == (1, 1)  # SimTrade 扣的是昨仓
    assert engine.account(ACCOUNT)["commission"] == pytest.approx(1.0 + 2.0)  # 收的也是平昨
    # A 成交：剩昨 1 + 今 1 → 平昨 2 + 平今 10。
    feed(engine, {**day2, "datetime": "2026-11-03 10:05:00.000000",
                  "last_price": 830.0, "ask_price1": 830.5, "bid_price1": 830.0})
    assert engine.positions(ACCOUNT) == []
    assert engine.account(ACCOUNT)["commission"] == pytest.approx(1.0 + 2.0 + 12.0)


# -- 价格改善：主动限价单按对手方最优价成交（2026-10-08 双账本对照） --------------
#
# 交易所连续竞价按价格优先、时间优先撮合：新到的可成交限价单以对手方最优申报价
# 成交（买单按卖一、卖单按买一），不劣于自身限价。TqSdk SimTrade 原生一律按自身
# 限价成交，本站在下单入口改写撮合价；委托记录仍显示用户限价。


def latest_trade(service: PaperEngine, client_order_id: str) -> dict:
    return next(
        trade for trade in service.activity(ACCOUNT)["trades"]
        if trade["clientOrderId"] == client_order_id)


def test_marketable_buy_limit_fills_at_best_ask_and_keeps_reported_price(engine):
    # 买一 819.98 / 卖一 820.02，限价 820.10 高于卖一 4 跳：按卖一 820.02 成交。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    opened = engine.submit(ACCOUNT, submit_request("order-buy-improve", limitPrice=820.10))
    assert opened["order"]["status"] == "FINISHED"
    assert opened["order"]["filledVolume"] == 1
    assert opened["order"]["limitPrice"] == pytest.approx(820.10)  # 委托价保留用户限价
    assert latest_trade(engine, "order-buy-improve")["price"] == pytest.approx(820.02)
    assert by_direction_long(engine)["openPrice"] == pytest.approx(820.02)
    # 手续费按手数不变，浮盈按改善后的成交价计算。
    assert engine.account(ACCOUNT)["commission"] == 20.0
    assert engine.account(ACCOUNT)["floatProfit"] == pytest.approx((820.0 - 820.02) * 1000)


def test_marketable_sell_limit_fills_at_best_bid(engine):
    # 10-08 事故形状：卖出平今限价 892.72、买一 892.74，快期按 892.74 成交。
    # 同构到测试行情：买开 1 手 @卖一 820.02 后，卖平今限价 819.90 低于买一
    # 819.98 —— 按买一 819.98 成交，比自身限价好 4 跳。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-sell-improve-open", limitPrice=820.02))
    closed = engine.submit(ACCOUNT, submit_request(
        "order-sell-improve", direction="SELL", offset="CLOSETODAY", limitPrice=819.90))
    assert closed["order"]["status"] == "FINISHED"
    assert closed["order"]["limitPrice"] == pytest.approx(819.90)
    assert latest_trade(engine, "order-sell-improve")["price"] == pytest.approx(819.98)
    assert engine.account(ACCOUNT)["closeProfit"] == pytest.approx((819.98 - 820.02) * 1000)
    assert engine.positions(ACCOUNT) == []


def test_limit_equal_to_counterparty_price_fills_at_that_price(engine):
    # 限价恰好等于对手价：原生行为已是该价，不触发改写，结果一致。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    engine.submit(ACCOUNT, submit_request("order-equal", limitPrice=820.02))
    assert latest_trade(engine, "order-equal")["price"] == pytest.approx(820.02)


def test_resting_limit_fills_at_own_price_when_quote_crosses_later(engine):
    # 被动单不吃价格改善：挂低价买 815.00，行情跌到卖一 814.92 后按挂单价成交。
    feed(engine, au_quote("09:31:00.000000", 820.0))
    resting = engine.submit(ACCOUNT, submit_request("order-resting-price", limitPrice=815.0))
    assert resting["order"]["status"] == "ALIVE"
    feed(engine, au_quote("10:00:00.000000", 814.9))
    assert engine.query_order(ACCOUNT, "order-resting-price")["order"]["status"] == "FINISHED"
    assert latest_trade(engine, "order-resting-price")["price"] == pytest.approx(815.0)


# -- #527 B2：行情进账本的统一入口（静态元数据合同、重启水位、交割年月） --------


def _broken_metadata(quote: dict) -> dict:
    """行情源偶发缺值的形状：_detach 对缺字段给 NaN（乘数/交割年月给 0）。"""
    quote.update({
        "margin": float("nan"), "commission": float("nan"), "volume_multiple": 0.0,
        "expire_datetime": float("nan"), "product_id": "",
        "delivery_year": 0, "delivery_month": 0,
    })
    return quote


def _no_nan(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_no_nan(item) for item in value.values())
    if isinstance(value, list):
        return all(_no_nan(item) for item in value)
    return True


def _persisted_state(db) -> dict:
    """库里最后已提交的整本账（当前状态行 + 当日成交行）。"""
    connection = sqlite3.connect(db)
    try:
        return PaperEngine._committed_state(connection, ACCOUNT)[0]
    finally:
        connection.close()


def _persisted_account(db) -> dict:
    # ctp_balance / ctp_available 官方内核恒为 NaN，不属于本站算出的资金字段。
    return {key: value for key, value in _persisted_state(db)["_account"].items()
            if not key.startswith("ctp_")}


def test_broken_metadata_keeps_the_last_valid_values_and_survives_restart(tmp_path):
    # B2b：持仓后行情缺保证金/手续费/乘数，盘中与盘外两条入账路径都由合约资料表补齐，
    # 持仓/账户保证金不变 NaN、快照落盘干净，冷重启不报 paper_state_corrupt。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-meta-open"))
        resting = engine.submit(ACCOUNT, submit_request("order-meta-rest", limitPrice=815.0))
        assert resting["order"]["status"] == "ALIVE"
        feed(engine, _broken_metadata(au_quote("09:41:00.000000", 821.0)))  # 盘中
        feed(engine, _broken_metadata(au_quote("11:40:00.000000", 822.0)))  # 午休（盘外）
        account = engine.account(ACCOUNT)
        assert account["margin"] == pytest.approx(80_000.0)
        assert account["floatProfit"] == pytest.approx((822.0 - 820.02) * 1000)
        quote = engine._accounts[ACCOUNT].book.latest_quote("SHFE.au2612")
        assert quote["last_price"] == 822.0  # 价格照常更新
        assert (quote["margin"], quote["commission"], quote["volume_multiple"]) == (80_000.0, 20.0, 1000.0)
        assert (quote["product_id"], quote["delivery_year"], quote["delivery_month"]) == ("au", 2026, 12)
        assert quote["expire_datetime"] == FAR_EXPIRY_NS
        # 有限但不合法的交割月同样不覆盖旧值。
        feed(engine, au_quote("11:41:00.000000", 822.0, delivery=(2026, 13)))
        assert engine._accounts[ACCOUNT].book.latest_quote("SHFE.au2612")["delivery_month"] == 12
        # 引擎缓存同一合同：行情读接口的每手保证金也不跳 NaN。
        assert engine.market_quote(ACCOUNT, "SHFE.au2612")["marginPerLot"] == 80_000.0
        # 新的有效值照常生效，之后再缺值沿用的是新值。
        feed(engine, au_quote("13:31:00.000000", 822.0, margin=90_000.0))
        assert engine.account(ACCOUNT)["margin"] == pytest.approx(90_000.0)
        feed(engine, _broken_metadata(au_quote("13:32:00.000000", 822.0)))
        assert engine.account(ACCOUNT)["margin"] == pytest.approx(90_000.0)
        engine.submit(ACCOUNT, submit_request("order-meta-flush", limitPrice=800.0))  # 落一次盘
    finally:
        engine.close()
    assert _no_nan(_persisted_account(db))
    assert _no_nan(_persisted_state(db)["_positions"]["SHFE.au2612"]["margin"])
    restarted = build_engine(db, clock=clock)
    try:
        assert restarted.account(ACCOUNT)["margin"] == pytest.approx(90_000.0)
        assert restarted.query_order(ACCOUNT, "order-meta-rest")["order"]["status"] == "ALIVE"
    finally:
        restarted.close()


def test_contract_without_valid_metadata_only_updates_price_and_rejects_orders(tmp_path):
    # B2b：从未收到过有效保证金/手续费/乘数的合约，行情只更新价格、不进撮合内核，
    # 下单明确拒绝，账本与落盘快照里没有 NaN；有效元数据到达后恢复正常交易。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, _broken_metadata(au_quote("09:31:00.000000", 820.0)))
        rejected(engine, submit_request("order-meta-none"))
        book = engine._accounts[ACCOUNT].book
        assert "SHFE.au2612" not in book._sim._positions  # 内核没碰过这个合约
        assert book.latest_quote("SHFE.au2612")["last_price"] == 820.0
        assert _persisted_state(db)["_positions"] == {}
        assert _no_nan(_persisted_account(db))
        feed(engine, au_quote("09:41:00.000000", 820.0))
        opened = engine.submit(ACCOUNT, submit_request("order-meta-later"))
        assert opened["order"]["status"] == "FINISHED" and opened["order"]["isError"] is False
        assert engine.account(ACCOUNT)["margin"] == pytest.approx(80_000.0)
    finally:
        engine.close()
    # 账本级：没有有效旧值时 apply_quote 不调内核（持仓字典都不建）。
    book = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=clock,
                     trading_dates=TRADING_DATES)
    assert book.apply_quote(_broken_metadata(au_quote("09:51:00.000000", 821.0))) == []
    assert book._sim._positions == {} and book.sanity()


def test_restart_drops_a_quote_older_than_the_restored_book(tmp_path):
    # B2e：重启后进程内缓存是空的，防倒退水位取自各账本快照里的行情时间——
    # 比它旧的第一条行情整笔丢弃，不倒退账本价格、不驱动撮合；历史快照也不
    # 冒充实时行情（健康仍 unavailable）；相同/更新时间的行情照常处理。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("10:00:00.000000", 830.0))
        alive = engine.submit(ACCOUNT, submit_request("order-watermark", limitPrice=825.0))
        assert alive["order"]["status"] == "ALIVE"
    finally:
        engine.close()
    clock.advance(datetime.fromisoformat("2026-11-02T10:01:00+08:00"))
    restarted = build_engine(db, clock=clock)
    try:
        health = restarted.health()
        assert health["dataStatus"] == "unavailable" and health["ordersInitialized"] is False
        revision = restarted.account_info(ACCOUNT)["revision"]
        feed(restarted, au_quote("09:00:00.000000", 820.0))  # 迟到的旧行情
        assert restarted.query_order(ACCOUNT, "order-watermark")["order"]["status"] == "ALIVE"
        book = restarted._accounts[ACCOUNT].book
        assert book.latest_quote("SHFE.au2612")["last_price"] == 830.0
        assert restarted.account_info(ACCOUNT)["revision"] == revision
        assert restarted.health()["dataStatus"] == "unavailable"
        feed(restarted, au_quote("10:00:00.000000", 829.0))  # 与水位同一时刻：照常处理
        assert book.latest_quote("SHFE.au2612")["last_price"] == 829.0
        feed(restarted, au_quote("10:01:00.000000", 824.0))
        assert restarted.query_order(ACCOUNT, "order-watermark")["order"]["status"] == "FINISHED"
        assert restarted.health()["dataStatus"] == "normal"
    finally:
        restarted.close()


def test_missing_delivery_month_rejects_open_but_not_close(tmp_path):
    # B2c：缺交割年月判断不了是否已进交割月，开仓拒单；平仓与现金交割的股指
    # 期货不看交割月，不受影响。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:31:00+08:00"))
    book = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=clock,
                     trading_dates=TRADING_DATES)
    book.inject_quote(au_quote("09:31:00.000000", 820.0, delivery=(0, 0)))
    assert "交割年月" in book.pre_check(submit_request("order-no-delivery"))
    assert book.pre_check(submit_request(
        "order-no-delivery-close", direction="SELL", offset="CLOSE", limitPrice=819.98)) is None
    index = future_quote("CFFEX.IF2612", 4231.0, tick=0.2, multiple=300.0, margin=380_000.0,
                         delivery=(0, 0))
    book.inject_quote(index)
    assert book.pre_check(submit_request(
        "order-if-no-delivery", symbol="CFFEX.IF2612", limitPrice=4231.4)) is None


def test_sanity_checks_position_and_account_fields(tmp_path):
    # F3：恢复自检覆盖持仓保证金/盈亏/成本、持仓手数与冻结、账户资金字段。
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(tmp_path / "paper.sqlite3", clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        engine.submit(ACCOUNT, submit_request("order-sanity", volume=2))
        state = engine._accounts[ACCOUNT].book.export_state()
    finally:
        engine.close()

    def restored(mutate) -> bool:
        broken = json.loads(json.dumps(state))
        mutate(broken)
        book = PaperBook(account_id=ACCOUNT, init_balance=1_000_000.0, clock=clock,
                         trading_dates=TRADING_DATES)
        book.restore_state(broken)
        return book.sanity()

    symbol = "SHFE.au2612"
    assert restored(lambda s: None) is True
    # 空仓一侧的持仓均价本来就是 NaN（官方内核口径），不误判。
    assert math.isnan(state["_positions"][symbol]["position_price_short"])
    assert restored(lambda s: s["_positions"][symbol].update(margin_long=float("nan"))) is False
    assert restored(lambda s: s["_positions"][symbol].update(margin=float("nan"))) is False
    assert restored(lambda s: s["_positions"][symbol].update(position_price_long=float("nan"))) is False
    assert restored(lambda s: s["_positions"][symbol].update(volume_long=-1)) is False
    assert restored(lambda s: s["_positions"][symbol].update(volume_long_frozen=3)) is False
    # 今/昨分桶（#777 review）：合计照旧对，分桶坏了也拒绝——平今/平昨校验只看分桶。
    assert state["_positions"][symbol]["volume_long_today"] == 2
    assert restored(lambda s: s["_positions"][symbol].update(volume_long_today=3, volume_long_his=-1)) is False
    assert restored(lambda s: s["_positions"][symbol].update(volume_long_today=1)) is False
    assert restored(lambda s: s["_positions"][symbol].update(volume_long_his=float("nan"))) is False
    assert restored(lambda s: s["_positions"][symbol].pop("volume_long_today")) is False
    assert restored(lambda s: s["_positions"][symbol].update(volume_long_frozen_today=1)) is False
    assert restored(lambda s: s["_positions"][symbol].update(
        volume_long_frozen=1, volume_long_frozen_his=1)) is False
    assert restored(lambda s: s["_positions"][symbol].update(
        volume_long_today=1, volume_long_his=1)) is True
    assert restored(lambda s: s["_account"].update(commission=float("nan"))) is False
    assert restored(lambda s: s["_account"].pop("close_profit")) is False
    assert restored(lambda s: s.update(init_balance=float("nan"))) is False


# -- #527 B2：持久化合约资料表 paper_contracts -----------------------------------

RB_CONTRACT = {
    "symbol": "SHFE.rb2701", "ins_class": "FUTURE", "product_id": "rb",
    "volume_multiple": 10.0, "price_tick": 1.0, "expire_datetime": FAR_EXPIRY_NS,
    "delivery_year": 2027, "delivery_month": 1, "margin": 4_000.0, "commission": 0.3116,
}


def _contract_row(db, symbol: str) -> dict | None:
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM paper_contracts WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row is not None else None


def _feed_contracts(service: PaperEngine, records: list[dict]) -> None:
    service.feed_contracts(records)
    assert service.drain(timeout=5.0)


def test_live_quote_writes_back_the_contract_table(tmp_path):
    # 实时行情的有效资料回写资料表；值变了更新、同日值不变不重复写。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
        row = _contract_row(db, "SHFE.au2612")
        assert (row["margin"], row["commission"], row["volume_multiple"]) == (80_000.0, 20.0, 1000.0)
        assert (row["product_id"], row["delivery_year"], row["delivery_month"]) == ("au", 2026, 12)
        assert row["fee_source"] == "quote" and row["static_source"] == "quote"
        first_stamp = row["fee_updated_at"]
        feed(engine, au_quote("09:32:00.000000", 821.0))
        assert _contract_row(db, "SHFE.au2612")["fee_updated_at"] == first_stamp  # 同日不变不重写
        feed(engine, au_quote("09:33:00.000000", 821.0, margin=90_000.0))
        row = _contract_row(db, "SHFE.au2612")
        assert row["margin"] == 90_000.0 and row["fee_updated_at"] > first_stamp
        assert engine.health()["contracts"]["rows"] == 1
    finally:
        engine.close()


def test_restart_first_quote_without_metadata_uses_the_contract_table(tmp_path):
    # Codex P2：重启后进程内什么都没有，首条缺资料的行情由资料表补齐——行情读接口
    # 有每手保证金，没存过该合约的新账户也能下单，下单结果标注资料表来源。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
    finally:
        engine.close()
    clock.advance(datetime.fromisoformat("2026-11-02T09:40:00+08:00"))
    restarted = build_engine(db, clock=clock)
    try:
        feed(restarted, _broken_metadata(au_quote("09:41:00.000000", 821.0)))
        assert restarted.market_quote(ACCOUNT, "SHFE.au2612")["marginPerLot"] == 80_000.0
        restarted.create_account(SECOND_ACCOUNT)
        report = restarted.submit(SECOND_ACCOUNT, submit_request("order-table-open", limitPrice=821.02))
        assert report["order"]["status"] == "FINISHED" and report["order"]["isError"] is False
        source = {"fees": "contractTable", "feeSource": "quote",
                  "feeUpdatedAt": "2026-11-02T09:31:00+08:00", "feeStale": False}
        assert report["order"]["contractSource"] == source
        assert restarted.account(SECOND_ACCOUNT)["margin"] == pytest.approx(80_000.0)
        # #777 review：标注随委托存下——查询与幂等重放重建的回执同样带它。
        assert restarted.query_order(SECOND_ACCOUNT, "order-table-open")["order"]["contractSource"] == source
        replay = restarted.submit(SECOND_ACCOUNT, submit_request("order-table-open", limitPrice=821.02))
        assert replay["order"]["contractSource"] == source
    finally:
        restarted.close()
    again = build_engine(db, clock=clock)
    try:
        assert again.query_order(SECOND_ACCOUNT, "order-table-open")["order"]["contractSource"] == source
    finally:
        again.close()


def test_never_subscribed_contract_trades_after_a_refresh(tmp_path):
    # 批量刷新写进资料表的合约：首条实时行情缺保证金/手续费/乘数/到期也能下单；
    # 刷新前同样的行情只能拒单。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, _broken_metadata(rb_quote("09:31:00.000000", 3071.0)))
        rejected(engine, submit_request("order-rb-before", symbol="SHFE.rb2701", limitPrice=3072.0))
        _feed_contracts(engine, [RB_CONTRACT])
        assert _contract_row(db, "SHFE.rb2701")["fee_source"] == "contract_service"
        assert engine.health()["contracts"]["refreshedAt"] is not None
        feed(engine, _broken_metadata(rb_quote("09:32:00.000000", 3071.0)))
        opened = engine.submit(ACCOUNT, submit_request(
            "order-rb-after", symbol="SHFE.rb2701", limitPrice=3072.0))
        assert opened["order"]["status"] == "FINISHED" and opened["order"]["isError"] is False
        assert engine.account(ACCOUNT)["margin"] == pytest.approx(4_000.0)
    finally:
        engine.close()


def test_expired_contracts_never_enter_the_table(tmp_path):
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0, expire_ns=EXPIRY_1102_NS - 86400 * 10**9))
        assert _contract_row(db, "SHFE.au2612") is None
        _feed_contracts(engine, [{**RB_CONTRACT, "expire_datetime": 1_000_000_000 * 10**9}])
        assert _contract_row(db, "SHFE.rb2701") is None
    finally:
        engine.close()


def test_stale_table_fees_are_flagged_on_the_order_and_in_health(tmp_path):
    # 每手保证金/手续费超过 3 个交易日没被确认：静态字段照用、下单照常，但下单
    # 结果与健康都标旧值。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
    finally:
        engine.close()
    clock.advance(datetime.fromisoformat("2026-11-06T09:40:00+08:00"))
    restarted = build_engine(db, clock=clock)
    try:
        assert restarted.health()["contracts"]["staleFeeRows"] == 1
        feed(restarted, _broken_metadata(au_quote("2026-11-06 09:41:00.000000", 821.0)))
        report = restarted.submit(ACCOUNT, submit_request("order-stale-fee", limitPrice=821.02))
        assert report["order"]["isError"] is False
        assert report["order"]["contractSource"]["feeStale"] is True
        # 有效实时值一到即确认，旧值标记消失。
        feed(restarted, au_quote("2026-11-06 09:42:00.000000", 821.0))
        assert restarted.health()["contracts"]["staleFeeRows"] == 0
    finally:
        restarted.close()


def test_one_fee_field_does_not_confirm_the_other(tmp_path):
    # #777 review：只到每手保证金、手续费缺失时，保证金照写，但费用组的确认时间
    # 不前移——资料表补的旧手续费照样标旧值；两项都到才算确认。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        feed(engine, au_quote("09:31:00.000000", 820.0))
    finally:
        engine.close()
    clock.advance(datetime.fromisoformat("2026-11-06T09:40:00+08:00"))
    restarted = build_engine(db, clock=clock)
    try:
        feed(restarted, au_quote("2026-11-06 09:41:00.000000", 821.0, margin=90_000.0,
                                 commission=float("nan")))
        row = _contract_row(db, "SHFE.au2612")
        assert row["margin"] == 90_000.0 and row["fee_updated_at"].startswith("2026-11-02")
        assert restarted.health()["contracts"]["staleFeeRows"] == 1
        report = restarted.submit(ACCOUNT, submit_request("order-half-fee", limitPrice=821.02))
        assert report["order"]["contractSource"]["feeStale"] is True
        feed(restarted, au_quote("2026-11-06 09:42:00.000000", 821.0, margin=90_000.0))
        assert restarted.health()["contracts"]["staleFeeRows"] == 0
    finally:
        restarted.close()


def test_contract_batch_is_serialized_with_account_writes(tmp_path):
    # #777 review：批量刷新写表与账户写入共用 _state_lock，同一连接上不会并发两个事务。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        with engine._state_lock:
            engine.feed_contracts([RB_CONTRACT])
            assert not engine.drain(timeout=0.3)
            assert _contract_row(db, "SHFE.rb2701") is None
        assert engine.drain(timeout=5.0)
        assert _contract_row(db, "SHFE.rb2701")["margin"] == 4_000.0
    finally:
        engine.close()


def test_order_uses_the_refreshed_table_without_a_new_quote(tmp_path):
    # #777 review：缓存行情里资料表补的字段按当前资料表重补——表刷新后、下一笔
    # 行情到达前的下单与行情读接口用新保证金，不沿用缓存时的旧值。
    db = tmp_path / "paper.sqlite3"
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    engine = build_engine(db, clock=clock)
    try:
        _feed_contracts(engine, [RB_CONTRACT])
        feed(engine, _broken_metadata(rb_quote("09:31:00.000000", 3071.0)))
        _feed_contracts(engine, [{**RB_CONTRACT, "margin": 5_000.0}])
        assert engine.market_quote(ACCOUNT, "SHFE.rb2701")["marginPerLot"] == 5_000.0
        opened = engine.submit(ACCOUNT, submit_request(
            "order-rb-refreshed", symbol="SHFE.rb2701", limitPrice=3072.0))
        assert opened["order"]["status"] == "FINISHED" and opened["order"]["isError"] is False
        assert engine.account(ACCOUNT)["margin"] == pytest.approx(5_000.0)
    finally:
        engine.close()
