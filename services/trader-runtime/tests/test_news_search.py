"""Clocked news collection through the real merge and screening entry; no network/sleep."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

from trader_runtime.news_screen import NewsScreener
from trader_runtime.news_search import GAP_THEMES, NewsSearchService
from trader_runtime.research_live import ResearchLiveService
from trader_runtime.web_search import WebSearchError

SCHEDULE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
            "night": [["21:00:00", "23:00:00"]]}
CATALOG = [{"symbol": "SHFE.rb2701", "productId": "rb", "exchangeId": "SHFE",
            "instrumentName": "螺纹钢2701", "tradingSchedule": SCHEDULE}]


class Clock:
    value = datetime.fromisoformat("2026-10-26T09:30:00+08:00")

    def __call__(self):
        return self.value


class Desk:
    days = ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]

    def trading_days(self):
        return self.days

    def recently_read(self):
        return [("SHFE.rb", "SHFE.rb2701", "test-user")]

    async def _catalog_items(self):
        return getattr(self, "catalog", CATALOG)


class Search:
    def __init__(self, now):
        self.now, self.calls, self.fail = now, [], False
        self.title = "螺纹钢社会库存下降"

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if self.fail:
            raise WebSearchError("web_search_unavailable")
        base = {"title": self.title, "content": self.title + "，需求改善。", "media": "我的钢铁",
                "publishDate": self.now().isoformat(), "link": "https://news.mysteel.com/a/notice.html"}
        return {"results": [base, {**base, "link": "https://unlisted.example/story"},
                {**base, "title": "昨日螺纹钢旧闻", "publishDate": (self.now()-timedelta(days=1)).isoformat()}]}


def make():
    clock = Clock()
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    lock = threading.RLock()
    screener = NewsScreener(connection=connection, lock=lock, now=clock)
    screener.generator = lambda: SimpleNamespace(agent_authenticated=True, complete_structured=None)
    live = ResearchLiveService(connection=connection, lock=lock, now=clock,
        instrument_catalog=lambda: {"items": CATALOG}, screener=screener)
    search = Search(clock)
    service = NewsSearchService(Desk(), live, search, now=clock)  # type: ignore[arg-type]
    return service, live, search, clock


def test_collect_reuses_merge_screening_and_persistent_half_hour_due():
    service, live, search, clock = make()
    async def scenario():
        assert await service.run_once() == 1
        query = search.calls[0][0]
        assert "after:2026-10-26" in query and "新闻" not in query  # a gap theme, not a generic news search
        assert any(theme in query for theme in GAP_THEMES)
        assert not any(feed in query for feed in ("eastmoney", "sina", "10jqka", "cls.cn", "jin10", "stcn"))
        assert live.connection.execute("SELECT source FROM news_flash").fetchone()[0].startswith("全网搜索")
        assert live.connection.execute("SELECT count(*) FROM news_flash").fetchone()[0] == 1
        assert live.screener is not None and live.screener.usage()["pending"] == 1
        # A new collector instance (slot handoff) still sees the persisted due time.
        other = NewsSearchService(service.desk, live, search, now=clock)  # type: ignore[arg-type]
        assert await other.run_once() == 0 and len(search.calls) == 1
        clock.value += timedelta(minutes=29)
        assert await other.run_once() == 0 and len(search.calls) == 1
        clock.value += timedelta(minutes=1)
        await other.run_once()
        assert len(search.calls) == 2 and search.calls[-1][1]["official"] is True
        assert live.connection.execute("SELECT count(*) FROM news_flash").fetchone()[0] == 1
    asyncio.run(scenario())


def test_closed_weekend_lunch_holiday_and_missing_calendar_skip():
    service, live, search, clock = make()
    for moment in ["2026-10-24T09:30:00+08:00", "2026-10-26T12:00:00+08:00", "2026-10-26T20:30:00+08:00"]:
        clock.value = datetime.fromisoformat(moment)
        assert asyncio.run(service.run_once()) == 0
    service.desk.days = ["2026-10-23", "2026-10-27"]  # Monday is a holiday in this fixture
    clock.value = datetime.fromisoformat("2026-10-26T09:30:00+08:00")
    assert asyncio.run(service.run_once()) == 0
    service.desk.days = []
    assert asyncio.run(service.run_once()) == 0
    assert not search.calls


def test_night_session_collects_and_failure_waits_five_minutes():
    service, live, search, clock = make()
    clock.value = datetime.fromisoformat("2026-10-26T21:00:00+08:00")
    search.fail = True
    assert asyncio.run(service.run_once()) == 0 and len(search.calls) == 1
    clock.value += timedelta(minutes=4)
    assert asyncio.run(service.run_once()) == 0 and len(search.calls) == 1
    clock.value += timedelta(minutes=1)
    search.fail = False
    assert asyncio.run(service.run_once()) == 1 and len(search.calls) == 2


def test_foreign_reading_uses_its_existing_cme_calendar_not_chinese_holidays():
    service, live, search, clock = make()
    service.desk.days = []
    service.desk.catalog = [{"symbol": "CME.NQZ26", "exchangeId": "CME", "productId": "NQ",
                             "tradingSchedule": {"dayRule": "cme", "cmeHours": "equity"}}]
    service.desk.recently_read = lambda: [("CME.NQZ26", "CME.NQZ26", "test-user")]
    search.title = "纳指期货上涨"
    clock.value = datetime.fromisoformat("2026-10-26T22:00:00+08:00")
    assert asyncio.run(service.run_once()) == 1 and len(search.calls) == 1
    clock.value = datetime.fromisoformat("2026-10-24T22:00:00+08:00")
    assert asyncio.run(service.run_once()) == 0 and len(search.calls) == 1
