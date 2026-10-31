"""外盘本站模拟（paper_foreign.py）：美东交易日、CME 时段、MT5 行情桥喂价、收盘价近似结算。

引擎与账本是国内同一份代码，只换时区、日历、时段表和行情源；这里用假桥离线驱动整条链路。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tqkq_readonly_sidecar.main import trading_day_from_calendar
from tqkq_readonly_sidecar.paper import PaperEngine, VirtualClock
from tqkq_readonly_sidecar.paper_book import in_trading_session
from tqkq_readonly_sidecar.paper_foreign import (
    CME_SCHEDULE,
    NEW_YORK,
    ExchangeMargins,
    Mt5PaperSource,
    close_before,
    cme_trading_dates,
    contract_record,
    delivery,
    parse_exchange_margins,
    quote_record,
)

ACCOUNT = "paperusd:0000000000a1"
SYMBOL = "CME.GCEZ26"
DATES = cme_trading_dates(datetime(2026, 11, 2, 12, tzinfo=NEW_YORK))
GC_ROW = {
    "symbol": "GCEZ26", "description": "Gold (Globex): December 2026", "path": "Exchange-Futures\\CME\\GCEZ26",
    "tickSize": 0.1, "tickValue": 10.0, "multiplier": 100.0, "marginInitial": 5797.0,
    "settlement": 4181.1, "expirationTime": "2026-12-29T23:59:00+00:00",
}


def ny(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=NEW_YORK)


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def test_cme_trading_day_flips_at_six_pm_new_york():
    assert trading_day_from_calendar(ny("2026-11-02T17:59:00"), DATES, NEW_YORK) == "2026-11-02"
    assert trading_day_from_calendar(ny("2026-11-02T18:00:00"), DATES, NEW_YORK) == "2026-11-03"
    # 周五 18:00 之后到周日都归下周一
    assert trading_day_from_calendar(ny("2026-11-06T19:00:00"), DATES, NEW_YORK) == "2026-11-09"
    assert trading_day_from_calendar(ny("2026-11-08T19:00:00"), DATES, NEW_YORK) == "2026-11-09"


@pytest.mark.parametrize(("moment", "open_"), [
    ("2026-11-02T10:00:00", True),   # 周一日间
    ("2026-11-02T17:30:00", False),  # 每日 17:00–18:00 停盘
    ("2026-11-02T18:30:00", True),   # 周二的交易日从周一 18:00 开始
    ("2026-11-06T19:00:00", False),  # 周五晚上不开盘
    ("2026-11-07T12:00:00", False),  # 周六
    ("2026-11-08T18:30:00", True),   # 周日 18:00 开周一
    ("2026-12-25T10:00:00", False),  # 圣诞全天休市
])
def test_cme_session_follows_globex_hours(moment, open_):
    dates = cme_trading_dates(ny(moment))
    assert in_trading_session(ny(moment), CME_SCHEDULE, dates, NEW_YORK) is open_


def test_contract_and_quote_records_from_the_bridge():
    assert delivery("GCEZ26") == ("GCE", 2026, 12)
    assert delivery("HEG27") == ("HE", 2027, 2)
    assert delivery("GCE") is None
    record = contract_record(GC_ROW)
    assert record is not None
    assert record["symbol"] == SYMBOL and record["product_id"] == "GCE"
    assert record["volume_multiple"] == 100.0 and record["price_tick"] == 0.1
    assert record["margin"] == 5797.0 and record["commission"] == 2.5
    assert (record["delivery_year"], record["delivery_month"]) == (2026, 12)
    quote = quote_record({"symbol": "GCEZ26", "bid": 4190.0, "ask": 4190.3, "last": 4190.1,
                          "timeMsc": ms(ny("2026-11-02T10:00:00")), "settlement": 4181.1}, record,
                         as_of_ms=ms(ny("2026-11-02T10:00:05")))
    assert quote is not None
    assert quote["datetime"] == "2026-11-02 23:00:05.000000"  # 北京时间字符串
    assert quote["settlement"] != quote["settlement"]  # 结算价不进撮合引擎（NaN）
    assert quote["lower_limit"] < 4190.0 < quote["upper_limit"]
    assert quote["trading_time"] == {"cmeHours": "globex"}  # 黄金：23 小时（节假日按 cme_hours）
    # 几天没跳过的报价（休市中的品种）不借读取时刻显得新：如实记最后一笔的时间
    idle = quote_record({"symbol": "GCEZ26", "bid": 4190.0, "ask": 4190.3, "last": 4190.1,
                         "timeMsc": ms(ny("2026-10-30T10:00:00"))}, record, as_of_ms=ms(ny("2026-11-02T10:00:00")))
    assert idle is not None and idle["datetime"] == "2026-10-30 22:00:00.000000"


def test_close_before_takes_the_last_bar_before_five_pm():
    bars = [
        {"time": int(ny("2026-11-02T16:58:00").timestamp()), "close": 4200.0},
        {"time": int(ny("2026-11-02T16:59:00").timestamp()), "close": 4201.5},
        {"time": int(ny("2026-11-02T18:00:00").timestamp()), "close": 4203.0},  # 下一交易日
        {"time": int(ny("2026-11-01T17:30:00").timestamp()), "close": 4100.0},  # 上一交易日之前
    ]
    assert close_before(bars, "2026-11-02") == 4201.5
    assert close_before(bars[2:], "2026-11-02") is None


class FakeBridge:
    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.quote = {"symbol": "GCEZ26", "bid": 4190.0, "ask": 4190.3, "last": 4190.1}
        self.bars: list[dict] = []
        self.paths: list[str] = []

    def __call__(self, path: str):
        self.paths.append(path)
        now = ms(self.clock())
        if path.startswith("/v1/symbols"):
            return {"symbols": [GC_ROW]}
        if path.startswith("/v1/quotes"):
            return {"bridgeTimeMs": now, "quotes": [{**self.quote, "timeMsc": now}]}
        if path.startswith("/v1/bars"):
            return {"symbol": "GCEZ26", "timeframe": "M1", "bars": self.bars}
        raise AssertionError(path)


@pytest.fixture()
def foreign(tmp_path):
    clock = VirtualClock(ny("2026-11-02T10:00:00"))
    engine = PaperEngine(db_path=str(tmp_path / "paper-foreign.sqlite3"), clock=clock,
                         trading_dates=lambda: cme_trading_dates(clock()), timezone=NEW_YORK,
                         quote_wait_seconds=0.05)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT, 100_000.0)
    bridge = FakeBridge(clock)
    source = Mt5PaperSource("http://bridge", "token", get_json=bridge,
                            clock=lambda: clock().timestamp())
    yield engine, source, bridge, clock
    engine.close()


def step(engine: PaperEngine, source: Mt5PaperSource) -> None:
    source.step(engine)
    assert engine.drain(timeout=5.0)


def test_foreign_order_fills_from_mt5_and_settles_on_the_close(foreign):
    engine, source, bridge, clock = foreign
    step(engine, source)  # 合约资料
    with pytest.raises(Exception):
        engine.market_quote(ACCOUNT, "CME.NOPEZ26")  # 桥没列出的合约不存在
    with pytest.raises(RuntimeError, match="market_quote_not_ready"):
        engine.market_quote(ACCOUNT, SYMBOL)  # 读一次报价 = 进订阅集，行情源下一轮就取
    step(engine, source)
    quote = engine.market_quote(ACCOUNT, SYMBOL)
    assert quote["lastPrice"] == pytest.approx(4190.1)
    assert quote["marketSessionStatus"] == "trading" and quote["commissionPerLot"] == 2.5

    report = engine.submit(ACCOUNT, {
        "commandId": "cmd-1", "clientOrderId": "o-1", "symbol": SYMBOL, "direction": "BUY",
        "offset": "OPEN", "volume": 1, "priceType": "LIMIT", "limitPrice": 4191.0,
    })
    assert report["order"]["status"] == "FINISHED" and report["order"]["filledVolume"] == 1
    position = engine.positions(ACCOUNT)[0]
    assert position["openPrice"] == pytest.approx(4190.3)  # 对手方卖一价
    assert position["margin"] == pytest.approx(5797.0)
    account = engine.account(ACCOUNT)
    assert account["commission"] == pytest.approx(2.5)

    # 美东 18:00 换日：账户欠结算，按 17:00 前最后一根 1 分钟线收盘结算。
    bridge.bars = [{"time": int(ny("2026-11-02T16:59:00").timestamp()), "close": 4200.0}]
    clock.advance(ny("2026-11-02T18:00:30"))
    assert engine.pending_settlement_symbols() == {SYMBOL}
    assert engine.settlement_target_day() == "2026-11-02"
    source._settlement_at = 0.0
    step(engine, source)
    assert engine.drain(timeout=5.0)
    assert engine.pending_settlement_symbols() == set()
    position = engine.positions(ACCOUNT)[0]
    assert position["historicalVolume"] == 1
    assert any("/v1/bars?symbol=GCEZ26&timeframe=M1" in path for path in bridge.paths)


def test_foreign_order_outside_globex_hours_does_not_fill(foreign):
    engine, source, bridge, clock = foreign
    clock.advance(ny("2026-11-02T17:20:00"))  # 每日停盘
    step(engine, source)
    with pytest.raises(RuntimeError, match="market_quote_not_ready"):
        engine.market_quote(ACCOUNT, SYMBOL)
    step(engine, source)
    report = engine.submit(ACCOUNT, {
        "commandId": "cmd-2", "clientOrderId": "o-2", "symbol": SYMBOL, "direction": "BUY",
        "offset": "OPEN", "volume": 1, "priceType": "LIMIT", "limitPrice": 4191.0,
    })
    assert report["order"]["filledVolume"] == 0


def test_settles_on_the_exchange_settlement_mt5_publishes_during_the_day(foreign):
    """2026-10-20 所有者定：盘中 MT5 的结算价字段从上一交易日的值（4181.1）变成当日正式结算价（4160.1），
    换日结算就用它，不用 17:00 收盘价近似（这里 1 分钟线收盘是 4200）。"""
    engine, source, bridge, clock = foreign
    step(engine, source)
    with pytest.raises(RuntimeError, match="market_quote_not_ready"):
        engine.market_quote(ACCOUNT, SYMBOL)
    bridge.quote = {**bridge.quote, "settlement": 4181.1}
    step(engine, source)
    engine.submit(ACCOUNT, {
        "commandId": "cmd-s", "clientOrderId": "o-s", "symbol": SYMBOL, "direction": "BUY",
        "offset": "OPEN", "volume": 1, "priceType": "LIMIT", "limitPrice": 4191.0,
    })
    clock.advance(ny("2026-11-02T13:45:00"))  # CME 13:30 公布，MT5 随即更新
    bridge.quote = {**bridge.quote, "settlement": 4160.1}
    step(engine, source)
    bridge.bars = [{"time": int(ny("2026-11-02T16:59:00").timestamp()), "close": 4200.0}]
    clock.advance(ny("2026-11-02T18:00:30"))
    source._settlement_at = 0.0
    step(engine, source)
    assert engine.pending_settlement_symbols() == set()
    assert not any("/v1/bars" in path for path in bridge.paths)  # 有正式结算价就不取 1 分钟线
    # 静态权益 = 10 万 − 手续费 2.5 + (结算价 4160.1 − 开仓 4190.3) × 100
    assert engine.account(ACCOUNT)["staticBalance"] == pytest.approx(100_000 - 2.5 + (4160.1 - 4190.3) * 100)


def _margin_page(rows: dict[str, float], extra: int = 20) -> str:
    """AMP 保证金表的形状：名称、交易所代码、CQG 代码、交易所、*Maintenance、Day Trading。"""
    head = ("<tr><td><strong>Name</strong></td><td>Symbol</td><td>CQG Symbol</td><td>Exchange</td>"
            "<td>*Maintenance</td><td>Day Trading</td></tr>")
    body = "".join(f"<tr><td>{root}</td><td>{root}</td><td>{root}</td><td>CME</td>"
                   f"<td><span>${value:,.2f}</span></td><td>$400.00</td></tr>" for root, value in rows.items())
    filler = "".join(f"<tr><td>X{i}</td><td>X{i}</td><td>XQ{i}</td><td>CME</td><td>$1,000.00</td><td>$50.00</td></tr>"
                     for i in range(extra))
    return f"<table>{head}{body}{filler}</table>"


def test_foreign_margin_is_the_exchange_overnight_margin_not_amps_day_trading_one(foreign, tmp_path):
    """所有者 10-21 定：外盘一直按交易所隔夜保证金算。MT5 的 marginInitial 是 AMP 日内保证金（黄金 5797），
    AMP 公开表的 *Maintenance（黄金 23190）才是交易所标准；表按 CQG 代码 = MT5 根代码对上。"""
    assert parse_exchange_margins(_margin_page({"GCE": 23190.0, "EP": 28813.0}))["EP"] == 28813.0
    assert contract_record(GC_ROW, overnight={"GCE": 23190.0})["margin"] == 23190.0
    assert contract_record(GC_ROW, overnight={"EP": 28813.0})["margin"] == 5797.0   # not in the table: MT5 as before

    engine, _source, bridge, clock = foreign
    pages = [_margin_page({"GCE": 23190.0})]
    margins = ExchangeMargins(str(tmp_path / "foreign-margins.json"), fetch=lambda: pages[-1])
    source = Mt5PaperSource("http://bridge", "token", get_json=bridge, clock=lambda: clock().timestamp(), margins=margins)
    step(engine, source)
    with pytest.raises(RuntimeError, match="market_quote_not_ready"):
        engine.market_quote(ACCOUNT, SYMBOL)  # 读一次报价 = 进订阅集
    step(engine, source)
    assert engine.market_quote(ACCOUNT, SYMBOL)["marginPerLot"] == 23190.0
    # 读失败、读残都留着上一份；存档重启照用。
    pages.append("<html>maintenance page</html>")
    assert margins.refresh_if_due(clock().timestamp() + 13 * 3600) is False and margins.margins["GCE"] == 23190.0
    assert ExchangeMargins(str(tmp_path / "foreign-margins.json")).margins["GCE"] == 23190.0
    # 交易所调了保证金：下一次刷新后合约资料立即按新值。
    pages.append(_margin_page({"GCE": 25000.0}))
    later = ExchangeMargins(str(tmp_path / "foreign-margins.json"), fetch=lambda: pages[-1])
    assert later.refresh_if_due(clock().timestamp() + 13 * 3600) is True and later.margins["GCE"] == 25000.0


ZC_ROW = {
    "symbol": "ZCEZ26", "description": "Corn (Globex): December 2026", "path": "Exchange-Futures\\CME\\ZCEZ26",
    "tickSize": 0.25, "tickValue": 12.5, "multiplier": 50.0, "marginInitial": 1100.0,
    "settlement": 430.0, "expirationTime": "2026-12-14T23:59:00+00:00",
}


@pytest.mark.parametrize(("moment", "open_"), [
    ("2026-11-02T10:00:00", True),   # 芝加哥 09:00，日间段
    ("2026-11-02T09:00:00", False),  # 芝加哥 08:00，07:45–08:30 间歇
    ("2026-11-02T15:00:00", False),  # 芝加哥 14:00，13:20 收盘后（23 小时表里还算开着）
    ("2026-11-02T20:30:00", True),   # 芝加哥 19:30，周二交易日的夜间段
])
def test_grains_follow_their_own_two_sessions(moment, open_):
    quote = quote_record({**ZC_ROW, "bid": 430.0, "ask": 430.25, "last": 430.0, "timeMsc": ms(ny(moment))},
                         contract_record(ZC_ROW))
    assert in_trading_session(ny(moment), quote["trading_time"], cme_trading_dates(ny(moment)), NEW_YORK) is open_


def test_a_resting_grain_order_does_not_fill_while_corn_is_halted(tmp_path):
    """所有者 2026-10-21：谷物停盘时新单被 Runtime 挡住，但引擎按 23 小时撮合，已挂的单照样成交。"""
    clock = VirtualClock(ny("2026-11-02T08:00:00"))  # 芝加哥 07:00，夜间段
    engine = PaperEngine(db_path=str(tmp_path / "paper-grains.sqlite3"), clock=clock,
                         trading_dates=lambda: cme_trading_dates(clock()), timezone=NEW_YORK, quote_wait_seconds=0.05)
    engine.open()
    engine.start()
    engine.create_account(ACCOUNT, 100_000.0)
    quote = {"symbol": "ZCEZ26", "bid": 430.0, "ask": 430.25, "last": 430.0}

    def bridge(path: str):
        now = ms(clock())
        if path.startswith("/v1/symbols"):
            return {"symbols": [ZC_ROW]}
        if path.startswith("/v1/quotes"):
            return {"bridgeTimeMs": now, "quotes": [{**quote, "timeMsc": now}]}
        return {"symbol": "ZCEZ26", "timeframe": "M1", "bars": []}

    source = Mt5PaperSource("http://bridge", "token", get_json=bridge, clock=lambda: clock().timestamp())
    try:
        step(engine, source)
        with pytest.raises(RuntimeError, match="market_quote_not_ready"):
            engine.market_quote(ACCOUNT, "CME.ZCEZ26")
        step(engine, source)
        report = engine.submit(ACCOUNT, {
            "commandId": "cmd-zc", "clientOrderId": "o-zc", "symbol": "CME.ZCEZ26", "direction": "BUY",
            "offset": "OPEN", "volume": 1, "priceType": "LIMIT", "limitPrice": 429.0,
        })
        assert report["order"]["filledVolume"] == 0  # 挂在买一下面
        clock.advance(ny("2026-11-02T09:00:00"))  # 芝加哥 08:00：07:45–08:30 间歇，价格穿过挂单价也不成交
        quote.update(bid=428.0, ask=428.25, last=428.0)
        step(engine, source)
        assert engine.positions(ACCOUNT) == []
        clock.advance(ny("2026-11-02T09:45:00"))  # 芝加哥 08:45：日间段开盘
        step(engine, source)
        assert engine.positions(ACCOUNT)[0]["volume"] == 1
    finally:
        engine.close()


@pytest.mark.parametrize(("kind", "moment", "open_"), [
    # 感恩节（2026-11-26 周四，没有 CME 交易日）：周四的盘归周五。股指 / 美债周四芝加哥 12:00 停、金属能源 13:30 停。
    ("equity", "2026-11-26T10:00:00", True),
    ("equity", "2026-11-26T13:30:00", False),   # 芝加哥 12:30，股指已停
    ("globex", "2026-11-26T14:00:00", True),    # 芝加哥 13:00，金属能源还开
    ("grains", "2026-11-26T10:00:00", False),   # 谷物感恩节周四不开
    ("equity", "2026-11-27T13:30:00", False),   # 周五股指芝加哥 12:15 收
    ("globex", "2026-11-27T13:30:00", True),    # 金属能源 13:45 收
    ("grains", "2026-11-27T10:00:00", True),    # 谷物周五只开 08:30–12:05（芝加哥）
    ("grains", "2026-11-27T13:30:00", False),
    ("globex", "2026-12-25T10:00:00", False),   # 圣诞全天休市
    ("globex", "2026-12-24T14:00:00", False),   # 平安夜金属能源芝加哥 12:45 收
    ("globex", "2026-11-02T10:00:00", True),    # 平日 23 小时
])
def test_cme_holidays_follow_the_exchange_hours(kind, moment, open_):
    dates = cme_trading_dates(ny(moment))
    assert in_trading_session(ny(moment), {"cmeHours": kind}, dates, NEW_YORK) is open_


def test_the_engine_closes_the_days_cme_has_no_trade_date():
    dates = cme_trading_dates(ny("2026-11-20T12:00:00"))
    assert "2026-11-26" not in dates and "2026-11-27" in dates
    # 感恩节周三 18:00 之后到周四的盘都归周五的交易日
    assert trading_day_from_calendar(ny("2026-11-26T10:00:00"), dates, NEW_YORK) == "2026-11-27"


def test_bridge_outage_keeps_batch_ledger_readable(foreign):
    """The bridge's 503 affects quote freshness, not committed account facts."""
    from tqkq_readonly_sidecar.paper_foreign import BridgeError

    engine, source, bridge, clock = foreign
    step(engine, source)
    with pytest.raises(RuntimeError, match="market_quote_not_ready"):
        engine.market_quote(ACCOUNT, SYMBOL)
    step(engine, source)
    engine.submit(ACCOUNT, {
        "commandId": "outage-open", "clientOrderId": "outage-open", "symbol": SYMBOL,
        "direction": "BUY", "offset": "OPEN", "volume": 1,
        "priceType": "LIMIT", "limitPrice": 4191.0,
    })
    before = engine.positions(ACCOUNT)
    def offline(path):
        raise BridgeError("mt5_bridge_http_503")
    # Same source-state transition as Mt5PaperSource._run's BridgeError branch.
    source._get = offline
    with pytest.raises(BridgeError, match="503"):
        source.step(engine)
    engine.mark_source_reconnecting()
    clock.advance(ny("2026-11-02T10:01:01"))
    snapshot = engine.snapshots([ACCOUNT])["accounts"][ACCOUNT]
    assert "error" not in snapshot
    assert snapshot["status"]["dataStatus"] == "unavailable"
    assert snapshot["account"]["dataStatus"] == "unavailable"
    assert snapshot["positions"] == before
    assert before[0]["volume"] == 1 and before[0]["openPrice"] == pytest.approx(4190.3)
