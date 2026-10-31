"""Live research intel (flash feed, foreign markets, calendar, rates, hog): recorded samples only, no network."""
from __future__ import annotations

import asyncio
import builtins
import json
import sqlite3
import threading
from datetime import UTC, date, datetime, time
from time import sleep
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime import research_live as live
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.main import create_app
from trader_runtime.research_desk import (
    DeskJudgment,
    DeskPoint,
    ResearchDeskService,
    _fact,
    validate_judgment,
)
from trader_runtime.research_live import ResearchLiveService
from trader_runtime.research_sources import SourceError
from trader_runtime.trader_tools import EconomicCalendarArguments as live_tool_arguments

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)  # 14:00 Beijing
TOKEN = "live-test-token-with-32-chars!!"


class Rows(list):
    """The slice of the DataFrame surface the service uses; CI has no pandas."""

    def to_dict(self, orient: str = "records") -> list[dict]:
        assert orient == "records"
        return list(self)


CLS_ROWS = Rows([
    {"标题": "铜价上涨", "内容": "【铜价上涨】财联社10月3日电，伦敦金属交易所铜价周三上涨，因美元走弱。",
     "发布日期": date(2026, 10, 3), "发布时间": time(9, 30)},
])
SHMET_ROWS = Rows([
    {"内容": "SHMET10月03日讯，伦敦金属交易所铜价周三上涨，因美元走弱。", "发布时间": datetime(2026, 10, 3, 1, 31, tzinfo=UTC)},
    {"内容": "SHMET10月03日讯，美联储降息预期升温，市场关注非农数据。", "发布时间": datetime(2026, 10, 3, 1, 40, tzinfo=UTC)},
])
EM_ROWS = Rows([
    {"标题": "螺纹钢产量下降", "摘要": "钢厂检修增加，螺纹钢产量下降，钢材库存回落。", "发布时间": "2026-10-03 10:01:12",
     "链接": "https://kuaixun.eastmoney.com/a/1.html"},
])
THS_ROWS = Rows([  # 同花顺 repeats the 螺纹 story (merged) under its own headline
    {"标题": "螺纹钢产量下降", "内容": "钢厂检修增加，螺纹钢产量下降，钢材库存回落。", "发布时间": "2026-10-03 10:05:00",
     "链接": "https://news.10jqka.com.cn/1.shtml"},
])
SINA_ROWS = Rows([
    {"时间": "2026-10-03 11:20:00", "内容": "市场消息：OPEC+讨论延长减产，油价获得支撑。"},
])
CALENDAR_ROWS = {
    "20261003": Rows([
        {"日期": "2026-10-03", "时间": "20:30", "地区": "美国", "事件": "美国9月非农就业人口", "公布": None, "预期": 14.2, "前值": 14.7, "重要性": "3"},
        {"日期": "2026-10-03", "时间": "09:45", "地区": "中国", "事件": "中国9月财新服务业PMI", "公布": 52.0, "预期": 51.5, "前值": 51.6, "重要性": "1"},
        {"日期": "2026-10-03", "时间": "19:00", "地区": "加拿大", "事件": "加拿大就业人数", "公布": None, "预期": None, "前值": None, "重要性": "3"},
        {"日期": "2026-10-03", "时间": "06:30", "地区": "美国", "事件": "美国10月2日SPDR黄金持仓-每日更新(吨)", "公布": 1055.7, "预期": None, "前值": 1056.55, "重要性": "2"},
        {"日期": "2026-10-03", "时间": "18:00", "地区": "欧元区", "事件": "欧元区8月PPI", "公布": None, "预期": -0.1, "前值": 0.2, "重要性": "2"},
    ]),
}
BOND_ROWS = Rows([  # oldest first, as AKShare returns them
    {"日期": "2026-09-28", "中国国债收益率10年": 1.75, "美国国债收益率10年": 4.10, "美国国债收益率10年-2年": 0.15},
    {"日期": "2026-09-29", "中国国债收益率10年": None, "美国国债收益率10年": 4.12, "美国国债收益率10年-2年": 0.18},
    {"日期": "2026-09-30", "中国国债收益率10年": 1.76, "美国国债收益率10年": None, "美国国债收益率10年-2年": None},
])
SHIBOR_ROWS = Rows([{"日期": "2026-09-30", "O/N-定价": 1.36, "1W-定价": 1.37}])
LPR_ROWS = Rows([  # the source lists 1991→today: the newest row must win, not the first valued one
    {"TRADE_DATE": "1991-04-21", "LPR1Y": None, "LPR5Y": None, "RATE_1": 8.64, "RATE_2": 9.72},
    {"TRADE_DATE": "2019-08-20", "LPR1Y": 4.25, "LPR5Y": 4.85},
    {"TRADE_DATE": "2026-09-22", "LPR1Y": 3.0, "LPR5Y": 3.5},
])
HOG_ROWS = Rows([{"date": f"2026-09-2{day}", "value": round(10.4 - day * 0.01, 2)} for day in range(7)]
                + [{"date": "2026-10-03", "value": 10.34}])


class RecordedLive:
    """The AKShare surface the live service touches, answered from recorded samples."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing, self.calls = failing or set(), []

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if name in self.failing:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def stock_info_global_cls(self, symbol="全部"):
        self._guard("flash_cls")
        return CLS_ROWS

    def stock_info_global_em(self):
        self._guard("flash_em")
        return EM_ROWS

    def stock_info_global_sina(self):
        self._guard("flash_sina")
        return SINA_ROWS

    def futures_news_shmet(self, symbol="全部"):
        self._guard("flash_shmet")
        return SHMET_ROWS if symbol == "铜" else Rows()

    def stock_info_global_ths(self):
        self._guard("flash_ths")
        return THS_ROWS

    def stock_info_global_futu(self):
        self._guard("flash_futu")
        return Rows()

    def news_economic_baidu(self, date=""):
        self._guard(f"calendar:{date}")
        return CALENDAR_ROWS.get(date, Rows())

    def bond_zh_us_rate(self, start_date=""):
        self._guard("bond")
        return BOND_ROWS

    def macro_china_shibor_all(self):
        self._guard("shibor")
        return SHIBOR_ROWS

    def macro_china_lpr(self):
        self._guard("lpr")
        return LPR_ROWS

    def futures_hog_core(self, symbol=""):
        self._guard("hog")
        return HOG_ROWS


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def service(tmp_path, recorded: RecordedLive | None = None, catalog: dict[str, Any] | None = None):
    connection = sqlite3.connect(tmp_path / "live.sqlite3", check_same_thread=False)
    sources = live.LiveSources()
    sources._module = recorded if recorded is not None else RecordedLive()
    return ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                               instrument_catalog=(lambda: catalog) if catalog else None, sources=sources,
                               clock=_Clock(), now=lambda: NOW)


def test_flash_poll_merges_sources_dedups_tags_and_prunes(tmp_path):
    svc = service(tmp_path, catalog={"items": [
        {"symbol": "SHFE.cu2612", "isMain": True, "productId": "cu", "instrumentName": "沪铜2612"},
        {"symbol": "SHFE.rb2610", "isMain": True, "productId": "rb", "instrumentName": "螺纹钢2610"},
    ]})
    round_one = svc.poll_flash_once()
    assert round_one["stored"] == 4  # 铜(去重后一条) + 宏观 + 螺纹 + 原油
    feed = svc.flash_feed()
    assert feed["status"] == "ok" and feed["count"] == 4
    items = feed["items"]
    assert [item["publishedAt"] for item in items] == sorted((item["publishedAt"] for item in items), reverse=True)
    copper = next(item for item in items if "伦敦金属交易所" in item["text"])
    # The same wire story ran in 财联社 and 上海金属网: one item, both sources, the earliest copy.
    assert copper["sources"] == ["财联社", "上海金属网"] and copper["source"] == "财联社"
    assert copper["publishedAt"] == "2026-10-03T09:30:00+08:00"
    assert copper["text"].startswith("伦敦金属交易所") and copper["title"] == "铜价上涨"
    macro = next(item for item in items if item["tags"] == ["macro"])
    # A macro story names no product by alias; the 产业链词表 (美联储 → 金银) and the 上海金属网 metal page
    # (铜) still offer it to those products for screening.
    assert "美联储" in macro["text"] and {"au", "ag", "cu"} <= set(macro["products"])
    rebar = next(item for item in items if "rb" in item["products"])
    assert rebar["link"] == "https://kuaixun.eastmoney.com/a/1.html"
    crude = next(item for item in items if "sc" in item["products"])
    assert crude["sources"] == ["新浪"] and crude["text"].startswith("OPEC")
    # Product filter, tag filter, hours and limit.
    assert [item["id"] for item in svc.flash_feed(product="SHFE.rb")["items"]] == [rebar["id"]]
    assert macro["id"] in {item["id"] for item in svc.flash_feed(tag="macro")["items"]}
    assert svc.flash_feed(hours=24, limit=2)["count"] == 2
    assert svc.flash_feed(hours=1)["count"] == 0
    # A second poll stores nothing new.
    assert svc.poll_flash_once()["stored"] == 0
    # Retention: anything older than three days goes on the next poll.
    with svc.lock, svc.connection:
        svc.connection.execute(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES ('old', '新浪', '[\"新浪\"]', '2026-09-01T09:00:00+08:00', '旧', '旧闻正文', NULL, '[]', '[]')")
    svc.poll_flash_once()
    with svc.lock:
        assert svc.connection.execute("SELECT COUNT(*) FROM news_flash WHERE id = 'old'").fetchone()[0] == 0


def test_flash_product_filter_scans_the_whole_window(tmp_path):
    """Production 10-03: /api/research/flash?product=SHFE.rb&limit=3 answered 0 while a 螺纹 item
    existed — the filter only looked at the newest limit×4 rows. A filter scans the whole window."""
    svc = service(tmp_path)
    with svc.lock, svc.connection:
        svc.connection.execute(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES ('rebar', '新浪', '[\"新浪\"]', '2026-10-03T08:00:00+08:00', '螺纹', '螺纹正文', NULL, '[\"rb\"]', '[]')")
        svc.connection.executemany(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES (?, '新浪', '[\"新浪\"]', ?, '其他', ?, NULL, '[]', '[]')",
            [(f"n{i}", f"2026-10-03T09:{i:02d}:00+08:00", f"无关正文{i}") for i in range(20)])
    assert [item["id"] for item in svc.flash_feed(product="SHFE.rb", limit=1)["items"]] == ["rebar"]
    assert svc.flash_feed(limit=1)["items"][0]["id"] == "n19"


def test_flash_reads_share_one_parsed_copy_that_follows_every_change(tmp_path):
    """调研台提速 (2026-10-22): the product views read one parsed copy of the table, shared by every reader,
    instead of parsing the whole window each time. A changed row (a merge, a translation), a new row and a
    deleted row all show on the next read, and what a caller does to its items never reaches the copy."""
    svc = service(tmp_path)
    insert = ("INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
              " VALUES (?, '新浪', '[\"新浪\"]', ?, ?, ?, NULL, '[\"rb\"]', '[]')")
    with svc.lock, svc.connection:
        svc.connection.executemany(insert, [("a", "2026-10-03T08:00:00+08:00", "螺纹甲", "螺纹甲正文"),
                                            ("b", "2026-10-03T09:00:00+08:00", "螺纹乙", "螺纹乙正文")])
    first = svc._flash_rows("rb", None, 24, None)
    assert [item["id"] for item in first] == ["b", "a"]
    first[0]["products"].append("au")
    first[0]["title"] = "改过"
    assert svc._flash_rows("au", None, 24, None) == [] and svc._flash_rows("rb", None, 24, 1)[0]["title"] == "螺纹乙"
    with svc.lock, svc.connection:
        svc.connection.execute("UPDATE news_flash SET title = '螺纹乙（译）' WHERE id = 'b'")
        svc.connection.execute(insert, ("c", "2026-10-03T10:00:00+08:00", "螺纹丙", "螺纹丙正文"))
    assert [(item["id"], item["title"]) for item in svc._flash_rows("rb", None, 24, None)] == [
        ("c", "螺纹丙"), ("b", "螺纹乙（译）"), ("a", "螺纹甲")]
    with svc.lock, svc.connection:
        svc.connection.execute("DELETE FROM news_flash WHERE id = 'a'")
    assert [item["id"] for item in svc._flash_rows("rb", None, 24, None)] == ["c", "b"]


def test_flash_retention_cap_keeps_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "FLASH_MAX_ITEMS", 3)
    svc = service(tmp_path)
    svc.poll_flash_once()
    with svc.lock, svc.connection:
        svc.connection.execute(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES ('bulk', '新浪', '[\"新浪\"]', '2026-10-03T23:00:00+08:00', '新', '批量正文', NULL, '[]', '[]')")
        svc.connection.executemany(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES (?, '新浪', '[\"新浪\"]', ?, '旧', ?, NULL, '[]', '[]')",
            [(f"x{i}", f"2026-10-0{i % 3 + 1}T08:00:00+08:00", f"更旧的正文{i}") for i in range(4)])
    svc._prune()
    with svc.lock:
        kept = [row[0] for row in svc.connection.execute(
            "SELECT id FROM news_flash ORDER BY published_at DESC").fetchall()]
    assert len(kept) == 3 and kept[0] == "bulk"


def test_one_failing_flash_source_keeps_the_others(tmp_path):
    recorded = RecordedLive(failing={"flash_em", "flash_ths"})
    svc = service(tmp_path, recorded=recorded)
    outcome = svc.poll_flash_once()
    assert outcome["collected"] == {"上海金属网": 2, "新浪": 1, "财联社": 1, "富途": 0}
    assert outcome["stored"] == 3  # 东方财富's rebar item is simply absent, nothing else broke
    assert "rb" not in {product for item in svc.flash_feed()["items"] for product in item["products"]}


async def test_the_loop_polls_and_refreshes_before_the_first_sleep(tmp_path):
    recorded = RecordedLive()
    svc = service(tmp_path, recorded=recorded)
    svc.poll_seconds = 60.0
    rounds = 0
    unwrapped = svc.poll_flash_once

    def counting() -> dict[str, Any]:
        nonlocal rounds
        rounds += 1
        return unwrapped()

    svc.poll_flash_once = counting
    svc.start()
    try:
        await asyncio.sleep(0.5)  # real seconds: the fakes answer at once, the poll interval is 60
        assert rounds == 1  # the first round ran before the first sleep; the interval holds the second back
        assert len([name for name in recorded.calls if name.startswith("calendar:")]) == 7
        assert "lpr" in recorded.calls
    finally:
        await svc.close()


def test_alias_file_overrides_and_extends_the_builtin_table(tmp_path):
    (tmp_path / "research_flash_aliases.json").write_text('{"cu": ["电解铜"], "xx": ["自定义词"]}', encoding="utf-8")
    svc = service(tmp_path, catalog={"items": [
        {"symbol": "SHFE.cu2612", "isMain": True, "productId": "cu", "instrumentName": "沪铜2612"}]})
    index = dict(svc._aliases())
    assert index["cu"] == ("沪铜", "电解铜")  # the file replaces the built-in list for cu; the catalog name stays
    assert index["xx"] == ("自定义词",) and "rb" in index  # unknown keys extend; built-ins elsewhere survive


def test_metal_tagging_masks_excluded_place_and_object_words(tmp_path):
    svc = service(tmp_path, catalog={"items": [
        {"symbol": "SHFE.cu2612", "isMain": True, "productId": "cu", "instrumentName": "沪铜2612"},
        {"symbol": "SHFE.pb2612", "isMain": True, "productId": "pb", "instrumentName": "铅2612"},
        {"symbol": "SHFE.sn2612", "isMain": True, "productId": "sn", "instrumentName": "锡2612"},
    ]})
    aliases = svc._aliases()

    def products(text: str) -> list[str]:
        return live.tag_flash_item({"title": "", "text": text}, aliases)["products"]

    # 单字金属字落在地名/日用品里不算命中（含目录注入的品种名“铅”“锡”“铜”）。
    assert products("无锡物联网产业大会公布首批场景清单") == []
    assert products("锡林郭勒盟今年饲草产量再创新高") == []
    assert products("文具板块走弱，铅笔出口订单回落") == []
    assert products("铜仁市文旅康养项目集中签约") == []
    # 两字说法照常命中；排除词只遮蔽自己那一段，同文的真说法不受影响。
    assert products("锡价上涨，锡锭升水继续走高") == ["sn"]
    assert products("铅价持稳，铅酸电池企业开工回暖") == ["pb"]
    assert products("无锡调研：铜价高位震荡，下游按需采购") == ["cu"]


SINA_FOREIGN = (
    'var hq_str_hf_CAD="9812.500,,9812,9813,9830,9790,05:59:00,9753.800,9760,0,1,1,2026-10-03,伦铜,0";'
    'var hq_str_hf_GC="4001.000,,4000.9,4001.2,4010,3990,05:59:00,3990.000,3992,0,1,1,2026-10-03,纽约黄金,0";'
    'var hq_str_hf_CL="71.200,,71.19,71.21,71.9,70.8,05:59:00,71.500,71.4,0,1,1,2026-10-03,纽约原油,0";'
    'var hq_str_hf_OIL="74.800,,74.79,74.81,75.2,74.1,05:59:00,75.000,74.9,0,1,1,2026-10-03,布伦特原油,0";'
)


def test_foreign_markets_read_the_one_foreign_entry_and_filter_by_domestic_product(tmp_path):
    from trader_runtime.foreign_market import ForeignMarketService, SinaForeignSource
    from trader_runtime.last_good import LastGood

    calls: list[str] = []
    sina = SinaForeignSource(fetch_text=lambda url: calls.append(url) or SINA_FOREIGN)
    svc = service(tmp_path)
    svc.foreign = ForeignMarketService(LastGood(), [sina])
    copper = svc.foreign_markets("SHFE.cu")
    assert copper["status"] == "ok" and [item["name"] for item in copper["items"]] == ["伦铜"]
    assert copper["items"][0]["price"] == 9812.5 and copper["items"][0]["quotedAt"] == "2026-10-03 05:59"
    assert copper["items"][0]["source"] == "新浪外盘" and copper["items"][0]["unit"] == "美元/吨"
    gold = svc.foreign_markets("au")["items"]
    assert gold[0]["name"] == "COMEX黄金" and gold[0]["changePercent"] == 0.28  # against the previous settle
    assert [item["code"] for item in svc.foreign_markets("sc")["items"]] == ["NYMEX.CL", "ICE.BRN"]
    assert svc.foreign_markets("rb")["items"] == []  # steel has no related foreign product
    assert [item["code"] for item in svc.foreign_markets()["items"]] == ["COMEX.GC", "NYMEX.CL", "ICE.BRN", "LMEOTC.CA"]
    assert len(calls) == 1  # one shared read for every reader inside the TTL


def test_foreign_markets_without_the_entry_are_unavailable(tmp_path):
    assert service(tmp_path).foreign_markets("cu") == {"status": "unavailable", "error": "foreign_market_unavailable",
                                                        "items": []}


def test_a_standby_reads_the_feed_calendar_and_poll_status_the_holder_wrote(tmp_path):
    """2026-10-12: only the lease holder polls and refreshes; the other slot (its own connection to the same
    state database, never polling) serves the same feed, calendar and last-poll status. Two merges at a
    handoff instant serialize in one write transaction each: no duplicate rows, no lost source."""
    holder = service(tmp_path)
    standby = service(tmp_path)
    assert standby.calendar_feed()["status"] == "loading"
    rounds = [threading.Thread(target=svc.poll_flash_once) for svc in (holder, standby)]
    for thread in rounds:
        thread.start()
    for thread in rounds:
        thread.join(timeout=30)
    feed = standby.flash_feed()
    assert feed["count"] == 4 and feed["lastPoll"]["failedSources"] == []
    copper = next(item for item in feed["items"] if "伦敦金属交易所" in item["text"])
    assert copper["sources"] == ["财联社", "上海金属网"]
    holder.refresh_calendar()
    calendar = standby.calendar_feed()
    assert calendar["status"] == "ok" and calendar["items"] == holder.calendar_feed()["items"]
    assert [event["region"] for event in standby.calendar_feed(region="美国")["items"]] == ["美国"]


def test_calendar_filters_regions_and_importance_and_serves_from_cache(tmp_path):
    recorded = RecordedLive()
    svc = service(tmp_path, recorded=recorded)
    assert svc.calendar_feed()["status"] == "loading"  # only the background loop fetches
    svc.refresh_calendar()
    feed = svc.calendar_feed()
    assert feed["status"] == "ok" and feed["asOf"] and feed["missingDays"] == []
    assert [(event["region"], event["event"]) for event in feed["items"]] == [
        ("欧元区", "欧元区8月PPI"), ("美国", "美国9月非农就业人口")]
    assert feed["items"][1]["forecast"] == 14.2 and feed["items"][1]["importance"] == 3
    assert [event["region"] for event in svc.calendar_feed(region="美国")["items"]] == ["美国"]
    # One request per day: the seven days were asked for once and stay cached.
    assert len([name for name in recorded.calls if name.startswith("calendar:")]) == 7
    svc.calendar_feed()
    assert len([name for name in recorded.calls if name.startswith("calendar:")]) == 7


def test_calendar_skips_a_failed_day_and_reports_it(tmp_path):
    recorded = RecordedLive(failing={"calendar:20261005"})
    svc = service(tmp_path, recorded=recorded)
    svc.refresh_calendar()  # one day out of seven failing does not sink the refresh
    feed = svc.calendar_feed()
    assert feed["status"] == "stale" and feed["missingDays"] == ["2026-10-05"] and feed["staleDays"] == []
    assert all(event["date"] != "2026-10-05" for event in feed["items"])
    assert len([name for name in recorded.calls if name.startswith("calendar:")]) == 7  # asked once, like the rest


def test_calendar_with_no_answering_day_keeps_the_old_list(tmp_path):
    svc = service(tmp_path, recorded=RecordedLive())
    svc.refresh_calendar()
    before = svc.calendar_feed()
    svc.sources._module.failing = {f"calendar:2026100{day}" for day in range(3, 10)}
    with pytest.raises(LookupError, match="research_calendar_empty"):
        svc.refresh_calendar()
    after = svc.calendar_feed()
    assert after["asOf"] == before["asOf"] and after["items"] == before["items"]


def test_a_calendar_with_no_events_is_an_answer_not_loading(tmp_path):
    """#488: seven days that answer with nothing worth listing are a confirmed empty week."""
    class Quiet(RecordedLive):
        def news_economic_baidu(self, date=""):
            self._guard(f"calendar:{date}")
            return Rows()

    svc = service(tmp_path, recorded=Quiet())
    complete = svc.refresh_calendar()
    feed = svc.calendar_feed()
    assert feed["status"] == "ok" and feed["items"] == [] and feed["asOf"] and feed["missingDays"] == []
    assert complete is True


async def test_a_failed_day_keeps_its_old_events_marked_and_retries_sooner(tmp_path):
    """#488: a day that fails on a later round keeps the events it had, the feed says which day is
    old, and the loop retries on the failure interval; a whole failed round says so too."""
    recorded = RecordedLive()
    svc = service(tmp_path, recorded=recorded)
    await svc._refresh_on_loop("calendar", svc.refresh_calendar, live.CALENDAR_TTL_SECONDS, live.CALENDAR_RETRY_SECONDS)
    assert svc._calendar_due == svc.clock.now + live.CALENDAR_TTL_SECONDS
    full = svc.calendar_feed()
    recorded.failing = {"calendar:20261003"}
    await svc._refresh_on_loop("calendar", svc.refresh_calendar, live.CALENDAR_TTL_SECONDS, live.CALENDAR_RETRY_SECONDS)
    assert svc._calendar_due == svc.clock.now + live.CALENDAR_RETRY_SECONDS
    partial = svc.calendar_feed()
    assert partial["status"] == "stale" and partial["staleDays"] == ["2026-10-03"] and partial["missingDays"] == []
    assert partial["items"] == full["items"]  # the 10-03 events stay, marked old, instead of vanishing
    recorded.failing = {f"calendar:2026100{day}" for day in range(3, 10)}
    with pytest.raises(LookupError, match="research_calendar_empty"):
        svc.refresh_calendar()
    failed = svc.calendar_feed()
    assert failed["status"] == "stale" and failed["items"] == full["items"] and len(failed["staleDays"]) == 7
    assert failed["error"] == "research_calendar_empty"


def test_the_calendar_window_matches_the_days_asked_for(tmp_path):
    """#488: [today, today + days) in Beijing time — after midnight yesterday is gone, and a day the
    last round never fetched is reported missing; more than the seven fetched days is refused."""
    svc = service(tmp_path)
    svc.refresh_calendar()
    svc.now = lambda: datetime(2026, 10, 3, 16, 30, tzinfo=UTC)  # 10-04 00:30 Beijing, before the next round
    tomorrow = svc.calendar_feed(days=1)
    assert tomorrow["status"] == "ok" and tomorrow["items"] == [] and tomorrow["missingDays"] == []
    week = svc.calendar_feed(days=7)
    assert week["status"] == "stale" and week["missingDays"] == ["2026-10-10"]
    with pytest.raises(ValueError):
        live_tool_arguments(days=14)


def test_rates_take_each_series_latest_valued_date_and_lpr_survives_a_failure(tmp_path):
    recorded = RecordedLive()
    svc = service(tmp_path, recorded=recorded)
    svc.refresh_lpr()
    rates = svc.rates()
    assert rates["status"] == "ok"
    # China's series lags the US one by a day: each keeps its own latest date with a value.
    assert rates["china10Y"] == {"value": 1.76, "date": "2026-09-30"}
    assert rates["us10Y"] == {"value": 4.12, "date": "2026-09-29"}
    assert rates["us10Y2Y"] == {"value": 0.18, "date": "2026-09-29"}
    assert rates["shiborOvernight"] == {"value": 1.36, "date": "2026-09-30"}
    assert rates["shibor1Week"] == {"value": 1.37, "date": "2026-09-30"}
    assert rates["lpr1Y"] == {"value": 3.0, "date": "2026-09-22"} and rates["lpr5Y"] == {"value": 3.5, "date": "2026-09-22"}
    # The rates block re-reads after its TTL; the LPR failure keeps the last cached value.
    svc.clock.now += 31 * 60
    recorded.failing.add("lpr")
    with pytest.raises(ValueError):
        svc.refresh_lpr()
    again = svc.rates()
    assert again["lpr1Y"] == {"value": 3.0, "date": "2026-09-22"}
    assert recorded.calls.count("lpr") == 2  # the failed refresh did retry the source


def test_hog_price_takes_the_seven_day_change(tmp_path):
    hog = service(tmp_path).hog_price()
    assert hog["status"] == "ok" and hog["price"] == 10.34 and hog["date"] == "2026-10-03"
    assert hog["change7d"] == pytest.approx(-0.06) and hog["change7dPercent"] == pytest.approx(-0.6)


def test_rates_keep_the_series_that_answered_when_the_other_fails(tmp_path):
    recorded = RecordedLive(failing={"bond"})
    svc = service(tmp_path, recorded=recorded)
    bond_down = svc.rates()
    assert bond_down["status"] == "ok"  # bonds failing leaves SHIBOR in place, not an error
    assert bond_down["china10Y"] is None and bond_down["us10Y"] is None and bond_down["us10Y2Y"] is None
    assert bond_down["shiborOvernight"] == {"value": 1.36, "date": "2026-09-30"}
    # …but says so (#516): which source failed, nothing carried over on a cold start, read time.
    assert bond_down["partial"] == {"failed": ["bond"], "carried": []} and bond_down["error"] == "research_rates_partial"
    assert bond_down["fetchedAt"]
    # A partial read is retried after the short interval, not after the 30-minute TTL.
    calls = recorded.calls.count("bond")
    svc.clock.now += live.LIVE_RETRY_SECONDS + 1
    recorded.failing = set()
    whole = svc.rates()
    assert recorded.calls.count("bond") == calls + 1 and "partial" not in whole and "error" not in whole
    svc.clock.now += 31 * 60
    recorded.failing = {"shibor"}
    shibor_down = svc.rates()
    assert shibor_down["status"] == "ok"
    # The failed source's series keep their last good values (with their own dates), named in `carried`.
    assert shibor_down["us10Y"] == {"value": 4.12, "date": "2026-09-29"}
    assert shibor_down["shiborOvernight"] == {"value": 1.36, "date": "2026-09-30"}
    assert shibor_down["partial"] == {"failed": ["shibor"], "carried": ["shiborOvernight", "shibor1Week"]}
    svc.clock.now += 31 * 60
    recorded.failing = {"bond", "shibor"}
    # Both down: the last good block stays, marked stale — never replaced by "unavailable" (2026-10-12).
    both_down = svc.rates()
    assert both_down["status"] == "stale" and both_down["stale"] is True
    assert both_down["error"] == "research_rates_unavailable"
    assert both_down["us10Y"] == {"value": 4.12, "date": "2026-09-29"} and both_down["fetchedAt"]
    # Retried after the short interval, not after the 30-minute TTL; the recovery is fresh again.
    calls = recorded.calls.count("bond")
    svc.clock.now += 60
    assert svc.rates()["status"] == "stale" and recorded.calls.count("bond") == calls
    svc.clock.now += live.LIVE_RETRY_SECONDS
    recorded.failing = set()
    recovered = svc.rates()
    assert recovered["status"] == "ok" and "stale" not in recovered and recovered["shiborOvernight"] is not None


def test_rates_and_hog_cold_failure_is_unavailable_and_retried_soon(tmp_path):
    recorded = RecordedLive(failing={"bond", "shibor", "hog"})
    svc = service(tmp_path, recorded=recorded)
    assert svc.rates() == {"status": "unavailable", "error": "research_rates_unavailable"}
    assert svc.hog_price() == {"status": "unavailable", "error": "research_source_error:ValueError"}
    recorded.failing = set()
    svc.clock.now += live.LIVE_RETRY_SECONDS
    assert svc.rates()["status"] == "ok" and svc.hog_price()["status"] == "ok"


def test_hog_empty_answer_keeps_the_last_price_and_a_restart_reads_it_back(tmp_path):
    class EmptyHog(RecordedLive):
        empty = False

        def futures_hog_core(self, symbol=""):
            return [] if self.empty else super().futures_hog_core(symbol)

    recorded = EmptyHog()
    svc = service(tmp_path, recorded=recorded)
    first = svc.hog_price()
    assert first["status"] == "ok" and first["price"] == 10.34
    # An empty answer is not "no price": the last one stays, stale.
    svc.clock.now += live.HOG_TTL_SECONDS + 1
    recorded.empty = True
    empty = svc.hog_price()
    assert empty["status"] == "stale" and empty["price"] == 10.34 and empty["error"] == "research_hog_unavailable"
    # A restarted runtime over the same database still has it when the source fails outright (past the stored
    # value's TTL; inside it, a restart reuses the value without fetching, #788).
    sources = live.LiveSources()
    sources._module = RecordedLive(failing={"hog", "bond", "shibor"})
    restarted = ResearchLiveService(connection=svc.connection, lock=threading.Lock(), state_root=tmp_path,
                                    sources=sources, clock=svc.clock, now=lambda: NOW)
    started = restarted._kept.wall()
    restarted._kept.wall = lambda: started + live.HOG_TTL_SECONDS + 1
    again = restarted.hog_price()
    assert again["status"] == "stale" and again["price"] == 10.34 and again["date"] == "2026-10-03"
    assert restarted.rates() == {"status": "unavailable", "error": "research_rates_unavailable"}


def test_hog_price_failure_is_a_stable_error_code(tmp_path):
    hog = service(tmp_path, recorded=RecordedLive(failing={"hog"})).hog_price()
    assert hog == {"status": "unavailable", "error": "research_source_error:ValueError"}


def test_hog_price_fetches_once_for_concurrent_callers(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowHog(RecordedLive):
        def __init__(self) -> None:
            super().__init__()
            self.fetches = 0

        def futures_hog_core(self, symbol=""):
            self._guard("hog")
            self.fetches += 1
            entered.set()
            release.wait(5)
            return HOG_ROWS

    slow = SlowHog()
    svc = service(tmp_path, recorded=slow)
    results: list[dict[str, Any]] = []

    def reader() -> None:
        results.append(svc.hog_price())

    first, second = threading.Thread(target=reader), threading.Thread(target=reader)
    first.start()
    assert entered.wait(5)
    second.start()
    sleep(0.2)
    assert second.is_alive()  # the second caller waits on the in-flight fetch instead of starting its own
    release.set()
    first.join(5)
    second.join(5)
    assert slow.fetches == 1 and [item["status"] for item in results] == ["ok", "ok"]


def test_a_hog_or_rates_call_still_running_after_its_timeout_is_waited_on_not_sent_again(tmp_path, monkeypatch):
    """#932: the inner time limit leaves the source call running; after the back-off the next read waits on that
    same call instead of starting a second thread against a site that has not answered the first."""
    monkeypatch.setattr(live, "HOG_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(live, "RATES_TIMEOUT_SECONDS", 0.1)
    release = threading.Event()

    class HungSites(RecordedLive):
        def __init__(self) -> None:
            super().__init__()
            self.started: list[str] = []

        def futures_hog_core(self, symbol=""):
            self.started.append("hog")
            release.wait(5)
            return HOG_ROWS

        def bond_zh_us_rate(self, start_date=""):
            self.started.append("bond")
            release.wait(5)
            return super().bond_zh_us_rate(start_date)

    hung = HungSites()
    svc = service(tmp_path, recorded=hung)
    assert svc.hog_price()["status"] == "unavailable"
    assert svc.rates()["partial"]["failed"] == ["bond"]
    svc.clock.now += live.LIVE_RETRY_SECONDS + 1  # the back-off is over, the first calls still hang
    assert svc.hog_price()["status"] == "unavailable"
    svc.rates()
    assert hung.started == ["hog", "bond"]  # waited on again, never a second thread
    release.set()
    sleep(0.3)  # the released calls finish and leave the registry
    svc.clock.now += live.LIVE_RETRY_SECONDS + 1
    assert svc.hog_price()["status"] == "ok" and svc.rates()["status"] == "ok"
    assert sorted(hung.started) == ["bond", "bond", "hog", "hog"]  # finished calls are not reused


def test_without_akshare_every_interface_reports_the_missing_dependency(tmp_path):
    svc = service(tmp_path)
    svc.sources._module = None
    real_import = builtins.__import__

    def no_akshare(name, *args, **kwargs):
        if name == "akshare":
            raise ImportError("no akshare")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = no_akshare
    try:
        # The foreign quotes no longer go through akshare (foreign_market reads Sina / 快期 directly).
        for payload in (svc.flash_feed(), svc.calendar_feed(), svc.rates(), svc.hog_price()):
            assert payload["status"] == "unavailable" and payload["error"] == "research_dependency_missing"
        with pytest.raises(RuntimeError, match="research_dependency_missing"):
            svc.poll_flash_once()
    finally:
        builtins.__import__ = real_import


async def test_flash_and_calendar_routes_precede_the_dataset_fallback(tmp_path):
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path / "state", local_token=TOKEN,
                     agent_mode="fake", data_mode="fake")
    app.state.research_live.sources._module = RecordedLive()
    app.state.research_live.now = lambda: NOW  # the recorded rows are dated 10-03, not the wall clock
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Origin": "http://127.0.0.1:5173"}) as client:
            assert (await client.get("/api/research/flash")).status_code == 401
            await client.post("/api/auth/local", headers={"Authorization": f"Bearer {TOKEN}"})
            empty = (await client.get("/api/research/flash", params={"product": "SHFE.rb"})).json()
            assert empty["status"] == "ok" and empty["count"] == 0  # the table is empty, not the route missing
            app.state.research_live.poll_flash_once()
            feed = (await client.get("/api/research/flash", params={"product": "SHFE.rb", "hours": 24, "limit": 10})).json()
            assert feed["status"] == "ok" and feed["count"] == 1 and feed["items"][0]["title"] == "螺纹钢产量下降"
            assert (await client.get("/api/research/calendar", params={"days": 7})).json()["status"] == "loading"
            app.state.research_live.refresh_calendar()
            calendar = (await client.get("/api/research/calendar", params={"region": "美国"})).json()
            assert calendar["status"] == "ok" and [event["region"] for event in calendar["items"]] == ["美国"]
            # The rounds fetch seven days: a longer window is refused, not answered as if covered.
            assert (await client.get("/api/research/calendar", params={"days": 14})).status_code == 422
            assert (await client.get("/api/research/nope", params={"symbol": "SHFE.rb2610"})).status_code == 404
            catalog = (await client.get("/api/agent/capabilities")).json()
            operations = {item["operation"]: item for item in catalog["operations"]}
            assert operations["GET /api/research/flash"]["readOnly"] is True
            assert operations["GET /api/research/calendar"]["readOnly"] is True
    finally:
        app.state.research.close()
        app.state.store.close()


def test_fake_research_live_samples_feed_the_fake_runtime(tmp_path, monkeypatch):
    """The browser tests' 快讯/日历 samples go through the real normalise, tag, merge and calendar code."""
    from trader_runtime.testing.fake_research_live import ENV_FLAG
    monkeypatch.setenv(ENV_FLAG, "1")
    app = create_app(tmp_path / "runtime.sqlite3", runtime_state_root=tmp_path / "state", local_token=TOKEN,
                     agent_mode="fake", data_mode="fake")
    try:
        svc = app.state.research_live
        svc.poll_flash_once()
        svc.refresh_calendar()
        feed = svc.flash_feed()
        assert feed["status"] == "ok" and feed["count"] >= 8
        assert any(item["sources"] == ["东方财富", "新浪"] and "au" in item["products"] for item in feed["items"])
        assert svc.flash_feed(tag="macro")["count"] >= 3 and svc.flash_feed(product="CFFEX.IF")["count"] == 1
        calendar = svc.calendar_feed()
        assert calendar["status"] == "ok" and {event["importance"] for event in calendar["items"]} == {2, 3}
        assert any(event["actual"] is not None for event in calendar["items"])
        assert "EIA原油库存(万桶)" not in {event["event"] for event in svc.calendar_feed(product="SHFE.au")["items"]}
        # The recorded fake model screens 沪金's rows and writes its 今日新闻要点 (the browser tests' AI mode).
        assert svc.flash_feed(product="SHFE.au")["screen"]["mode"] == "ai"
        asyncio.run(svc.screener.burst())
        view = svc.flash_feed(product="SHFE.au")
        drop = next(event for event in view["events"] if event["eventLabel"] == "现货黄金日内下跌")
        assert drop["reports"] >= 4 and drop["tier"] == "major" and view["layers"]["minor"] >= 2
        assert asyncio.run(svc.analyst.run_once()) == 1
        brief = svc.flash_feed(product="SHFE.au")["brief"]
        assert brief["status"] == "ready" and brief["net"] == "偏空" and 1 <= len(brief["points"]) <= 3
    finally:
        app.state.research.close()
        app.state.store.close()


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["contentItems"][0]["text"])


async def test_the_four_ai_tools_dispatch_through_the_wired_service(tmp_path):
    app = create_app(tmp_path / "tools.sqlite3", runtime_state_root=tmp_path / "state", local_token=TOKEN,
                     agent_mode="native", agent_provider="deepseek", data_mode="fake")
    app.state.research_live.sources._module = RecordedLive()
    app.state.research_live.now = lambda: NOW  # the recorded rows are dated 10-03, not the wall clock
    adapter = app.state.adapter
    adapter = adapter.adapter_for_user("local-user") if hasattr(adapter, "adapter_for_user") else adapter
    tools = adapter.trader_tools
    session_id = app.state.broker.create_session("local", "native", "local-user").session_id
    try:
        assert tools.research_live is app.state.research_live
        app.state.research_live.poll_flash_once()
        app.state.research_live.refresh_lpr()
        definitions = {item["name"] for item in tools.definitions()}
        assert {"get_news_flash", "get_foreign_markets", "get_economic_calendar", "get_interest_rates"} <= definitions

        async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result, _ = await tools.call_async(tool=tool, arguments=arguments, turn_id="live-tools",
                                               mode="local", session_id=session_id, user_id="local-user")
            return _payload(result)

        news = await call("get_news_flash", {"product": "SHFE.rb", "hours": 24})
        assert news["tool"] == "get_news_flash" and news["readOnly"] and news["data"]["status"] == "ok"
        # A product's read answers merged events (no model here: keyword events, each its own) and no items list.
        assert "items" not in news["data"] and [event["title"] for event in news["data"]["events"]] == ["螺纹钢产量下降"]
        assert news["data"]["events"][0]["tier"] == "unscreened" and set(news["data"]["events"][0]["items"][0]) == {"source", "publishedAt", "link"}
        macro = await call("get_news_flash", {"macroOnly": True})
        assert macro["data"]["items"] and all(item["tags"] == ["macro"] for item in macro["data"]["items"])
        foreign = await call("get_foreign_markets", {"product": "SHFE.cu"})
        assert [item["name"] for item in foreign["data"]["items"]] == ["伦铜"]
        app.state.research_live.refresh_calendar()
        calendar = await call("get_economic_calendar", {"days": 7, "region": "美国"})
        assert [event["region"] for event in calendar["data"]["items"]] == ["美国"]
        rates = await call("get_interest_rates", {})
        assert rates["data"]["us10Y"]["value"] == 4.12 and rates["data"]["lpr5Y"]["value"] == 3.5
        # Bad arguments and the sync path refuse in a stable shape.
        bad = await call("get_news_flash", {"hours": 0})
        assert not bad.get("data") and bad["errorCode"] == "invalid_tool_arguments"
        sync = tools.call(tool="get_interest_rates", arguments={}, turn_id="live-tools", mode="local")[0]
        assert _payload(sync)["errorCode"] == "research_live_async_required"
    finally:
        app.state.research.close()
        app.state.store.close()


# -- the desk's live sections


class _FailingSources:
    async def nbs_releases(self):
        raise SourceError("nbs_timeout")

    async def nbs_price_page(self, url):
        raise SourceError("nbs_timeout")

    async def cftc(self, code):
        raise SourceError("cftc_unreachable")

    async def fred(self, series):
        raise SourceError("fred_key_missing")

    async def eia_crude_stocks(self):
        raise SourceError("eia_key_missing")

    async def shfe_weekly(self, now):
        raise SourceError("shfe_weekly_unavailable")

    async def macro(self):
        raise SourceError("macro_timeout")

    async def org_notes(self, name, now):
        return []

    async def org_feed(self, now):
        return []


class _StubResearch:
    def summary(self, symbol: str, names: Any = None) -> dict[str, Any]:
        return {"datasets": {}}


async def _no_news(symbol: str) -> dict[str, Any]:
    return {"status": "ok", "results": []}


class _HogCatalogProvider(FakeReadOnlyDataProvider):
    def instrument_catalog(self) -> dict[str, Any]:
        catalog = super().instrument_catalog()
        catalog["items"] = catalog["items"] + [
            {"symbol": "DCE.lh2611", "exchangeId": "DCE", "instrumentId": "lh2611", "instrumentName": "生猪2611",
             "productId": "lh", "priceTick": 5.0, "volumeMultiple": 16.0, "isMain": True, "hasNight": False}]
        return catalog


def _desk(svc: ResearchLiveService, provider: FakeReadOnlyDataProvider | None = None) -> ResearchDeskService:
    return ResearchDeskService(research=_StubResearch(), news=_no_news,
                                provider=lambda: provider or FakeReadOnlyDataProvider(),
                                sources=_FailingSources(), extra=svc, now=lambda: NOW)


async def test_the_desk_carries_the_live_sections_and_facts(tmp_path):
    from trader_runtime.foreign_market import ForeignMarketService, SinaForeignSource
    from trader_runtime.last_good import LastGood

    svc = service(tmp_path)
    svc.foreign = ForeignMarketService(LastGood(), [SinaForeignSource(fetch_text=lambda url: SINA_FOREIGN)])
    desk = _desk(svc, _HogCatalogProvider())
    svc.poll_flash_once()
    svc.refresh_calendar()
    svc.refresh_lpr()
    gold = await desk.report("SHFE.au2612")
    sections = {item["key"]: item for item in gold["sections"]}
    # 第七批: no 快讯 section any more; the picks stay facts (the judgment's input, listed under 资料来源).
    assert "flash" not in sections
    assert sections["macro"]["status"] == "ok"  # the live macro facts stand in for the failing sources
    foreign = next(fact for fact in gold["facts"] if fact["id"] == "M.foreign")
    assert foreign["section"] == "market" and "COMEX黄金 4001" in foreign["value"] and "北京时间 05:59" in foreign["value"]
    flash = [fact for fact in gold["facts"] if fact["section"] == "flash"]
    assert flash and all(fact["label"] == "快讯" and fact["kind"] == "report" for fact in flash)
    assert any(fact["id"] == "E.calendar" and "美国9月非农就业人口" in fact["value"] for fact in gold["facts"])
    # 宏观 only as far as gold goes: the US yields, not China's 10-year, SHIBOR or the LPR.
    rates = next(fact for fact in gold["facts"] if fact["id"] == "E.rates")
    assert "美国10年期国债 4.12%（09-29）" in rates["value"]
    assert "LPR" not in rates["value"] and "中国10年期" not in rates["value"] and "SHIBOR" not in rates["value"]
    assert all("figures" not in fact for fact in gold["facts"])
    tiles = {tile["label"]: tile for tile in gold["tiles"]}
    assert tiles["COMEX黄金"]["ref"] == "M.foreign" and tiles["COMEX黄金"]["value"].startswith("4001")
    assert tiles["美 10 年期国债"] == {"label": "美 10 年期国债", "value": "4.12%", "color": "none", "note": "09-29", "ref": "E.rates"}
    copper = await desk.report("SHFE.rb2610")
    # Rebar (黑色) keeps China's 10-year and the LPR, and no US calendar event.
    rebar_rates = next((fact for fact in copper["facts"] if fact["id"] == "E.rates"), None)
    assert rebar_rates is None or ("LPR" in rebar_rates["value"] and "美国10年期" not in rebar_rates["value"])
    assert not any(fact["id"] == "E.calendar" and "非农" in fact["value"] for fact in copper["facts"])
    hog_report = await desk.report("DCE.lh2611")
    hog = next(fact for fact in hog_report["facts"] if fact["id"] == "D.hog_price")
    assert hog["value"] == "10.34 元/公斤，7 日（-0.06，-0.6%）"


def test_judgments_anchor_on_the_foreign_and_flash_facts():
    facts = [
        _fact("M.foreign", "market", "外盘联动", "伦铜 9812.5（+0.6%，北京时间 05:59）", "新浪外盘行情", "2026-10-03"),
        _fact("N.abc12345", "flash", "快讯", "09:30 财联社／上海金属网：伦敦金属交易所铜价周三上涨，因美元走弱。",
              "财联社／东方财富／新浪／上海金属网（快讯）", "2026-10-03T09:30:00+08:00", None,
              "伦敦金属交易所铜价周三上涨，因美元走弱。"),
    ]
    judgment = DeskJudgment(stance="偏多", confidence="低", headline="外盘走强", headlineBasis=["M.foreign"],
                            points=[DeskPoint(text="伦铜隔夜上涨 0.6%", basis=["M.foreign"], quote="北京时间 05:59"),
                                    DeskPoint(text="快讯：LME铜价上涨", basis=["N.abc12345"], quote="铜价周三上涨")])
    kept = validate_judgment(judgment, facts)
    assert [point["text"] for point in kept["points"]] == ["伦铜隔夜上涨 0.6%", "快讯：LME铜价上涨"]
    assert kept["points"][1]["grounds"] == "report" and kept["basis"]["N.abc12345"]["kind"] == "report"


async def test_a_cancelled_refresh_round_clears_its_busy_flag(tmp_path):
    """#516-6：刷新任务在运行中被取消（close 的取消路径）不再因 due 未定义抛 UnboundLocalError，
    busy 标志照常归零。"""
    entered, release = threading.Event(), threading.Event()
    svc = service(tmp_path)

    def slow_refresh() -> bool:
        entered.set()
        release.wait(5)
        return True

    svc._calendar_busy = True
    task = asyncio.create_task(svc._refresh_on_loop("calendar", slow_refresh, 60.0, 60.0))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert svc._calendar_busy is False
    assert svc._calendar_due == pytest.approx(svc.clock.now + 60.0)


async def test_close_cancels_the_loop_and_its_refresh_tasks(tmp_path):
    """#516-6：close() 连同循环里在跑的 calendar/LPR 刷新任务一起取消，不留无主任务。"""
    entered, release = threading.Event(), threading.Event()
    svc = service(tmp_path)

    def slow_refresh() -> bool:
        entered.set()
        release.wait(5)
        return True

    svc.refresh_calendar = slow_refresh
    svc._lpr_due = svc.clock.now + 3600.0
    svc.start()
    assert await asyncio.to_thread(entered.wait, 5)
    assert len(svc._refresh_tasks) == 1
    release.set()
    await svc.close()
    assert svc._refresh_tasks == set() and svc._loop is None


async def test_stop_waits_for_the_rounds_worker_thread_before_the_lease_moves(tmp_path, monkeypatch):
    """2026-10-12: cancelling the loop does not stop a poll already in its worker thread; stop() (the lease
    moving away) waits for that thread, so the next holder never polls alongside it. The wait is bounded."""
    entered, release = threading.Event(), threading.Event()
    ended: list[bool] = []
    svc = service(tmp_path)

    def slow_poll() -> dict[str, Any]:
        entered.set()
        release.wait(5)
        ended.append(True)
        return {}

    svc.poll_flash_once = slow_poll
    svc._calendar_due = svc._lpr_due = svc.clock.now + 3600.0
    svc.start()
    assert await asyncio.to_thread(entered.wait, 5)
    stopping = asyncio.create_task(svc.stop())
    await asyncio.sleep(0.05)
    assert not stopping.done()  # the poll thread is still writing
    release.set()
    await asyncio.wait_for(stopping, 5)
    assert ended == [True] and svc._threads == set()
    # A thread that never ends holds the stop only STOP_WAIT_SECONDS.
    monkeypatch.setattr(live, "STOP_WAIT_SECONDS", 0.05)
    entered.clear(), release.clear()
    svc.start()
    assert await asyncio.to_thread(entered.wait, 5)
    await asyncio.wait_for(svc.stop(), 2)
    assert len(svc._threads) == 1
    release.set()


def test_a_total_flash_failure_reports_unavailable_and_old_items_still_serve(tmp_path):
    """#516-7：四个来源全失败后窗口内一条没有是明确 unavailable（带 lastPoll 的失败来源与时间），
    不是 ok 的空列表；窗口内还有旧条目时照常服务；下一轮成功后 lastPoll 恢复。"""
    recorded = RecordedLive(failing={"flash_cls", "flash_em", "flash_sina", "flash_shmet", "flash_ths", "flash_futu"})
    svc = service(tmp_path, recorded=recorded)
    outcome = svc.poll_flash_once()
    assert outcome["collected"] == {} and outcome["stored"] == 0
    failed = svc.flash_feed()
    assert failed["status"] == "unavailable" and failed["error"] == "research_flash_unavailable"
    assert failed["items"] == [] and failed["count"] == 0
    assert failed["lastPoll"]["failedSources"] == ["财联社", "东方财富", "新浪", "同花顺", "富途", "上海金属网"]
    assert failed["lastPoll"]["at"]

    with svc.lock, svc.connection:
        svc.connection.execute(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES ('kept', '新浪', '[\"新浪\"]', '2026-10-03T09:00:00+08:00', '旧条目', '旧正文', NULL, '[]', '[]')")
    served = svc.flash_feed()
    assert served["status"] == "ok" and served["count"] == 1 and served["items"][0]["id"] == "kept"
    assert served["lastPoll"]["failedSources"] == ["财联社", "东方财富", "新浪", "同花顺", "富途", "上海金属网"]

    recorded.failing = set()
    svc.poll_flash_once()
    healthy = svc.flash_feed()
    assert healthy["status"] == "ok" and healthy["lastPoll"]["failedSources"] == []


@pytest.mark.parametrize("zone,stamp", [("UTC", "2026-10-08 04:00:57"), ("Asia/Shanghai", "2026-10-08 12:00:57")])
def test_ths_and_futu_stamps_are_read_in_the_process_timezone(monkeypatch, zone, stamp):
    """10-08: akshare formats 同花顺 / 富途 epochs in the process's local time; in the UTC cloud container a
    12:00 Beijing item read 04:00 and sank below the morning's news. The same item reads 12:00 anywhere."""
    import time as clock
    monkeypatch.setenv("TZ", zone)
    clock.tzset()
    try:
        row = {"标题": "飞利浦签署合作协议", "内容": "10月8日，飞利浦官微宣布签署合作协议。", "发布时间": stamp,
               "链接": "https://news.10jqka.com.cn/20261008/c680465271.shtml"}
        for source in ("同花顺", "富途"):
            assert live.normalise_flash_rows(source, [row])[0]["publishedAt"] == "2026-10-08T12:00:57+08:00"
        # Sources that already write Beijing time are left alone.
        assert live.normalise_flash_rows("东方财富", [{**row, "摘要": row["内容"]}])[0]["publishedAt"] == f"{stamp.replace(' ', 'T')}+08:00"
    finally:
        monkeypatch.undo()
        clock.tzset()


def test_a_foreign_row_carried_from_a_failed_source_makes_the_research_answer_stale(tmp_path):
    from trader_runtime.foreign_market import ForeignMarketService
    from trader_runtime.last_good import LastGood

    class Carried:
        name = "sina"

        def quotes(self, products):
            return {}

    svc = service(tmp_path)
    svc.foreign = ForeignMarketService(LastGood(), [Carried()])
    svc.foreign.snapshot = lambda: {"status": "ok", "fetchedAt": "2026-10-18T09:00:00+00:00", "sourceLabels": {"sina": "新浪外盘"},
                                    "partial": {"failed": ["sina"], "carried": ["LMEOTC.CA"]},
                                    "rows": [{"key": "LMEOTC.CA", "name": "伦铜", "last": 9812.5, "changePercent": 0.6,
                                              "preSettlement": 9753.8, "unit": "美元/吨", "source": "sina", "stale": True,
                                              "quoteTime": "2026-10-17T05:59:00+08:00"}]}
    answer = svc.foreign_markets("cu")
    assert answer["status"] == "stale" and answer["items"][0]["stale"] is True
    assert answer["error"] == "foreign_sources_failed:sina"
    # A row the source left out of an HTTP 200 (no failed source): the reason says so, never a bare prefix.
    snapshot = svc.foreign.snapshot()
    svc.foreign.snapshot = lambda: {**snapshot, "partial": {"failed": [], "carried": ["LMEOTC.CA"], "missing": []}}
    assert svc.foreign_markets("cu")["error"] == "foreign_rows_missing"


async def test_a_related_foreign_product_never_seen_is_named_missing_and_the_desk_line_says_so(tmp_path):
    """#932: a cold start where only WTI answered — Brent is named missing (research, AI and the desk line);
    with no related row at all the product reads unavailable, not an empty ok."""
    from types import SimpleNamespace

    from trader_runtime.foreign_market import ForeignMarketService, SinaForeignSource
    from trader_runtime.last_good import LastGood
    from trader_runtime.research_desk import ResearchDeskService

    only_wti = 'var hq_str_hf_CL="71.200,,71.19,71.21,71.9,70.8,05:59:00,71.500,71.4,0,1,1,2026-10-03,纽约原油,0";'
    svc = service(tmp_path)
    svc.foreign = ForeignMarketService(LastGood(), [SinaForeignSource(fetch_text=lambda url: only_wti)])
    crude = svc.foreign_markets("sc")
    assert crude["status"] == "ok" and [item["code"] for item in crude["items"]] == ["NYMEX.CL"]
    assert crude["missing"] == [{"code": "ICE.BRN", "name": "布伦特原油"}]

    async def passthrough(awaitable):
        return await awaitable

    desk = SimpleNamespace(extra=svc, _deadline=passthrough)
    fact = await ResearchDeskService._foreign_fact(desk, "sc")
    assert fact["value"].startswith("WTI原油 71.2") and fact["value"].endswith("布伦特原油 暂无报价")
    gold = svc.foreign_markets("au")
    assert gold == {**gold, "status": "unavailable", "items": [], "missing": [{"code": "COMEX.GC", "name": "COMEX黄金"}]}
    # Every related product missing: the desk still says so, it does not drop the line (Codex r4307848107).
    all_missing = await ResearchDeskService._foreign_fact(desk, "au")
    assert all_missing["value"] == "COMEX黄金 暂无报价" and all_missing["figures"]["price"] is None
    # No product (every market): the board's partial.missing reaches the AI too (Codex r4307848110).
    every = svc.foreign_markets()
    assert {"code": "COMEX.GC", "name": "COMEX黄金"} in every["missing"]
    assert [item["code"] for item in every["items"]] == ["NYMEX.CL"]
