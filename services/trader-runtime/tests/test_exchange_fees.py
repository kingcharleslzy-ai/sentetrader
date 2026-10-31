"""交易所标准手续费（所有者 2026-10-08 决定）：openctp 页解析、缓存新鲜度、品种兜底与
生效优先级。测试不联网（conftest 设了 DISABLED；联网路径一律 monkeypatch `_fetch_html`），
缓存文件按用例种。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import trader_runtime.exchange_fees as exchange_fees
from conftest import per_lot_entry, seed_exchange_fees
from trader_runtime.exchange_fees import exchange_fee_schedule, parse_fees_page
from trader_runtime.research_data import CHINA
from trader_runtime.trading_costs import effective_fee_rate, effective_fee_schedule, trade_fee

# 20261008 从 http://openctp.cn/fees.html 录制的前 13 列（交易所 … 平今费用/手）。
RECORDED_ROWS = [
    "SHFE rb2701 rb2701 rb 螺纹钢 10 1 0.000101 0.01 0.000101 0.01 0.000101 0.01",
    "SHFE au2612 au2612 au 黄金 1000 0.02 0.000001 20.01 0.000001 20.01 0.000001 0",
    "CFFEX IC2612 中证500期货2612 IC 中证500指数 200 0.2 0.000024 0.01 0.000024 0.01 0.000231 0.01",
    "CZCE AP701 AP701 AP 苹果 10 1 0.000001 5.01 0.000001 5.01 0.000001 10.01",
    "DCE m2701 豆粕2701 m 豆粕 10 1 0.000001 1.51 0.000001 1.51 0.000001 1.51",
    "INE sc2611 sc2611 sc 原油 1000 0.1 0.000001 20.01 0.000001 20.01 0.000001 0",
]
HEADER = "交易所 合约代码 合约名称 品种代码 品种名称 合约乘数 最小跳动 开仓费率 开仓费用/手 平仓费率 平仓费用/手 平今费率 平今费用/手"


def _page() -> str:
    rows = "\n".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row.split()) + "</tr>" for row in RECORDED_ROWS)
    header = "<tr>" + "".join(f"<th>{cell}</th>" for cell in HEADER.split()) + "</tr>"
    return (f"<html><head><title>期货交易手续费与保证金</title></head><body><h3>期货交易手续费与保证金 - 20261008</h3>"
            f"<table>{header}{rows}</table></body></html>")


def test_parse_restores_the_exchange_standard_under_the_broker_addon():
    as_of, contracts = parse_fees_page(_page())
    assert as_of == "2026-10-08"
    # 螺纹：三栏费率 −0.000001 = 万分之 1（notional），每手 −0.01 = 0。
    assert contracts["SHFE.RB2701"]["multiplier"] == 10
    assert contracts["SHFE.RB2701"]["rate"] == {"mode": "notional", "open": 1.0, "closeYesterday": 1.0,
                                                "closeToday": 1.0, "openPerLotExtra": 0.0,
                                                "closeYesterdayPerLotExtra": 0.0, "closeTodayPerLotExtra": 0.0}
    # 黄金 / 原油：纯每手（平今免收 → 0）。
    assert contracts["SHFE.AU2612"]["rate"]["mode"] == "per_lot"
    assert contracts["SHFE.AU2612"]["rate"]["open"] == 20 and contracts["SHFE.AU2612"]["rate"]["closeToday"] == 0
    assert contracts["INE.SC2611"]["rate"]["closeYesterday"] == 20
    # 中证500：开/平万分之 0.23、平今万分之 2.3（平今是开仓的十倍），每手加收还原后为 0。
    ic = contracts["CFFEX.IC2612"]["rate"]
    assert ic["mode"] == "notional" and ic["open"] == pytest.approx(0.23) and ic["closeToday"] == pytest.approx(2.3)
    assert ic["closeTodayPerLotExtra"] == 0
    # 苹果（开/平 5、平今 10）与豆粕（1.5）都是纯每手。
    assert contracts["CZCE.AP701"]["rate"]["open"] == 5 and contracts["CZCE.AP701"]["rate"]["closeToday"] == 10
    assert contracts["DCE.M2701"]["rate"]["open"] == 1.5
    assert len(contracts) == 6


def test_exchange_fee_schedule_reads_a_fresh_cache_and_marks_old_ones_stale(tmp_path: Path):
    seed_exchange_fees(tmp_path, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 4, 4, 4)})
    fresh = exchange_fee_schedule("SHFE.rb2701", root=tmp_path)
    assert fresh is not None and fresh["source"] == "exchange"
    assert fresh["default"]["open"] == 4 and fresh["overrides"] == []
    assert fresh["reference"]["stale"] is False and fresh["reference"]["asOf"] == "2026-10-08"

    other = tmp_path / "old"
    yesterday = (datetime.now(CHINA) - timedelta(days=1)).strftime("%Y-%m-%d")
    seed_exchange_fees(other, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 4, 4, 4)},
                       fetched_day=yesterday)
    stale = exchange_fee_schedule("SHFE.rb2701", root=other)
    assert stale is not None and stale["reference"]["stale"] is True


def test_unknown_symbol_falls_back_to_its_product_then_none(tmp_path: Path):
    seed_exchange_fees(tmp_path, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 4, 4, 4)})
    # 表里没有的月份用同品种费率；没有交易所前缀也认；整个品种不在表里才是 None。
    assert exchange_fee_schedule("SHFE.rb2605", root=tmp_path)["default"]["open"] == 4   # type: ignore[union-attr]
    assert exchange_fee_schedule("RB2605", root=tmp_path)["default"]["open"] == 4        # type: ignore[union-attr]
    assert exchange_fee_schedule("SHFE.hc2610", root=tmp_path) is None


def test_product_fallback_stays_inside_the_symbols_exchange(tmp_path: Path):
    # 同字母品种在别的交易所费率不同，不跨所借用。
    seed_exchange_fees(tmp_path, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 4, 4, 4)})
    assert exchange_fee_schedule("DCE.rb2605", root=tmp_path) is None


def test_effective_fee_schedule_prefers_custom_then_exchange_then_none(tmp_path: Path):
    seed_exchange_fees(tmp_path, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 4, 4, 4)})
    custom = {"source": "custom", "default": {"mode": "per_lot", "open": 1, "closeToday": 1, "closeYesterday": 1},
              "overrides": []}
    assert effective_fee_schedule(custom, "SHFE.rb2701", root=tmp_path) is custom
    assert effective_fee_schedule(None, "SHFE.rb2701", root=tmp_path)["source"] == "exchange"   # type: ignore[union-attr]
    assert effective_fee_rate(custom, "SHFE.rb2701")["open"] == 1                                # type: ignore[union-attr]
    # 账户口径（source=account）不算自定义：仍用交易所标准。
    account = {"source": "account", "default": {"mode": "per_lot", "open": 0, "closeToday": 0, "closeYesterday": 0},
               "overrides": []}
    assert effective_fee_schedule(account, "SHFE.rb2701", root=tmp_path)["source"] == "exchange"  # type: ignore[union-attr]
    empty = tmp_path / "empty"
    assert effective_fee_schedule(None, "SHFE.rb2701", root=empty) is None
    assert effective_fee_schedule(None, "SHFE.rb2701") is None          # 没给根 → 不查


def test_notional_fees_match_the_hand_check(tmp_path: Path):
    """对拍常量（sidecar test_paper_book 用同一组数字）：rb 万分之 1 开 1 手 @3071 = 3.07；
    IC 平今万分之 2.3 @6000（乘数 200）= 276。"""
    _, contracts = parse_fees_page(_page())
    seed_exchange_fees(tmp_path, contracts)
    schedule = exchange_fee_schedule("SHFE.rb2701", root=tmp_path)
    assert trade_fee(schedule, symbol="SHFE.rb2701", price=3071, volume=1,
                     multiplier=10, offset="OPEN") == 3.07
    ic = exchange_fee_schedule("CFFEX.IC2612", root=tmp_path)
    assert trade_fee(ic, symbol="CFFEX.IC2612", price=6000, volume=1,
                     multiplier=200, offset="CLOSETODAY") == 276.0
    # 平今免收的品种（au）平今 0，开仓每手 20。
    au = exchange_fee_schedule("SHFE.au2612", root=tmp_path)
    assert trade_fee(au, symbol="SHFE.au2612", price=800, volume=2,
                     multiplier=1000, offset="CLOSETODAY") == 0.0
    assert trade_fee(au, symbol="SHFE.au2612", price=800, volume=2,
                     multiplier=1000, offset="OPEN") == 40.0


def test_read_path_never_touches_the_network_even_without_a_cache(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch):
    """请求路径（估算 / paper / 回测）只读缓存：无缓存且允许联网也返回 None，不现抓。"""
    monkeypatch.delenv("AI_TRADER_EXCHANGE_FEES_DISABLED", raising=False)
    calls = []

    def no_network() -> str:
        calls.append(1)
        raise AssertionError("读路径不得联网")

    monkeypatch.setattr(exchange_fees, "_fetch_html", no_network)
    assert exchange_fee_schedule("SHFE.rb2701", root=tmp_path) is None
    assert effective_fee_schedule(None, "SHFE.rb2701", root=tmp_path) is None
    assert calls == []


def test_refresh_fetches_parses_and_updates_both_caches(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(exchange_fees, "_fetch_html", _page)
    assert exchange_fees.refresh(tmp_path) is True
    # 落了缓存文件，之后的读路径（不再联网）直接可用。
    assert (tmp_path / "exchange-fees.json").exists()
    schedule = exchange_fee_schedule("SHFE.rb2701", root=tmp_path)
    assert schedule is not None and schedule["source"] == "exchange"
    assert schedule["reference"]["stale"] is False


def test_failed_refresh_keeps_the_old_cache_and_marks_it_stale(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch):
    yesterday = (datetime.now(CHINA) - timedelta(days=1)).strftime("%Y-%m-%d")
    seed_exchange_fees(tmp_path, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 4, 4, 4)},
                       fetched_day=yesterday)

    def broken() -> str:
        raise OSError("network down")

    monkeypatch.setattr(exchange_fees, "_fetch_html", broken)
    assert exchange_fees.refresh(tmp_path) is False
    schedule = exchange_fee_schedule("SHFE.rb2701", root=tmp_path)
    assert schedule is not None and schedule["default"]["open"] == 4
    assert schedule["reference"]["stale"] is True
    # 再读一次（不联网）仍是这份旧表：缓存文件原样保留，只是内存里的表降级为 stale。
    again = exchange_fee_schedule("SHFE.rb2605", root=tmp_path)
    assert again is not None and again["default"]["open"] == 4 and again["reference"]["stale"] is True


def test_refresher_loop_covers_the_day_then_idles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(exchange_fees, "_fetch_html", _page)
    stop = threading.Event()
    stop.set()  # 已停止：正好跑一轮就退出，不真实等待 30 分钟。
    exchange_fees._refresher_loop(tmp_path, stop)
    assert exchange_fee_schedule("SHFE.rb2701", root=tmp_path) is not None
    # 当天已有成功的表就不再抓。
    fetched: list[int] = []
    monkeypatch.setattr(exchange_fees, "_fetch_html", lambda: fetched.append(1) or _page())
    exchange_fees._refresher_loop(tmp_path, stop)
    assert fetched == []


class _Waits:
    """A stop event that records the refresher's wait and ends the loop at once."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return True


def test_cold_refresher_retries_soon_until_it_has_any_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """2026-10-12：冷启动一份表都没有时每 2 分钟重试，不再干等 30 分钟；有表（哪怕旧的）照常 30 分钟。"""
    def broken() -> str:
        raise OSError("network down")

    monkeypatch.setattr(exchange_fees, "_fetch_html", broken)
    stop = _Waits()
    exchange_fees._refresher_loop(tmp_path, stop)
    assert stop.waits == [exchange_fees.COLD_RETRY_SECONDS]
    assert exchange_fee_schedule("SHFE.rb2701", root=tmp_path) is None
    yesterday = (datetime.now(CHINA) - timedelta(days=1)).strftime("%Y-%m-%d")
    seed_exchange_fees(tmp_path, {"SHFE.rb2701": per_lot_entry("SHFE", "rb2701", "rb", 10, 4, 4, 4)},
                       fetched_day=yesterday)
    exchange_fees._refresher_loop(tmp_path, stop)
    assert stop.waits[-1] == exchange_fees.REFRESH_INTERVAL_SECONDS


def test_needs_fetch_finds_the_table_under_a_linked_root_and_writes_leave_no_temp_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    monkeypatch.setattr(exchange_fees, "_fetch_html", _page)
    assert exchange_fees.refresh(linked) is True
    (real / "exchange-fees.json").unlink()  # only the in-memory table is left
    assert exchange_fees._needs_fetch(linked) is False
    exchange_fees._write_cache(linked, {"contracts": {"x": 1}})
    assert sorted(path.name for path in real.iterdir()) == ["exchange-fees.json"]


def test_start_refresher_is_idempotent_and_disabled_by_env(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch):
    exchange_fees.start_refresher(tmp_path)  # conftest 设了 DISABLED：不起线程。
    assert exchange_fees._refreshers == {}
    monkeypatch.delenv("AI_TRADER_EXCHANGE_FEES_DISABLED", raising=False)
    monkeypatch.setattr(exchange_fees, "_fetch_html", _page)
    exchange_fees.start_refresher(tmp_path)
    exchange_fees.start_refresher(tmp_path)
    assert len(exchange_fees._refreshers) == 1
    thread = next(iter(exchange_fees._refreshers.values()))[0]
    exchange_fees.stop_refresher()
    thread.join(timeout=5)
    assert not thread.is_alive() and exchange_fees._refreshers == {}


def test_stop_refresher_waits_for_a_fetch_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """失租移交：stop 带 wait 时等正在抓取的线程写完再返回，新持有者不与它并行写缓存；等待有上限。"""
    import threading
    entered, release = threading.Event(), threading.Event()

    def slow_page() -> str:
        entered.set()
        release.wait(5)
        return _page()
    monkeypatch.delenv("AI_TRADER_EXCHANGE_FEES_DISABLED", raising=False)
    monkeypatch.setattr(exchange_fees, "_fetch_html", slow_page)
    exchange_fees.start_refresher(tmp_path)
    thread = next(iter(exchange_fees._refreshers.values()))[0]
    assert entered.wait(5)
    threading.Timer(0.1, release.set).start()
    exchange_fees.stop_refresher(tmp_path, wait=5)
    assert not thread.is_alive() and (tmp_path / exchange_fees.CACHE_NAME).exists()
    entered.clear(), release.clear()
    exchange_fees.start_refresher(tmp_path / "other")
    thread = next(iter(exchange_fees._refreshers.values()))[0]
    assert entered.wait(5)
    exchange_fees.stop_refresher(tmp_path / "other", wait=0.05)  # 等不到：按上限返回
    assert thread.is_alive()
    release.set()
    thread.join(5)
