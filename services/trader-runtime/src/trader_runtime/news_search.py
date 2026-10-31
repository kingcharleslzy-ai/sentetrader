"""Trading-session web news for the desk's existing seven-day reading list."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .research_desk import debate_sessions
from .research_live import product_key
from .trading_calendar import cme_trading_day, sessions_of_day, trading_day_of
from .web_search import CHINA, OFFICIAL_DOMAINS, WebSearchClient, WebSearchError

if TYPE_CHECKING:
    from .research_desk import ResearchDeskService
    from .research_live import ResearchLiveService

# 查漏补缺 (owner 2026-10-24): search only where the feeds we already read do not reach — official publishers and the
# commodity-industry data sites (财联社 / 东方财富 / 新浪 / 同花顺 / 证券时报 / 金十 … are read directly and left out).
# Sites rotate in groups of three to fit the search API's 128-character limit.
NEWS_DOMAINS = (*OFFICIAL_DOMAINS, "mysteel.com", "smm.cn", "sci99.com", "oilchem.net", "cngrain.com",
                "lgmi.com", "100ppi.com", "chinaccm.com", "ccf.com.cn", "feedtrade.com.cn")
# …and on the industry facts the headline feeds rarely carry, one theme per round instead of a generic “新闻”.
GAP_THEMES = ("开工 检修 减产", "库存 到港 发货", "利润 成本 亏损", "政策 环保 限产", "进口 出口 海外")
POLL_SECONDS = 60
INTERVAL_SECONDS = 30 * 60
FAILURE_SECONDS = 5 * 60
log = logging.getLogger("uvicorn.error.research")


@dataclass
class NewsSearchService:
    desk: ResearchDeskService
    live: ResearchLiveService
    search: WebSearchClient
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    async def run_once(self) -> int:
        now = self.now().astimezone(CHINA)
        stamp = now.timestamp()
        ns = int(stamp * 1e9)
        days = self.desk.trading_days()
        day = trading_day_of(ns, days)
        catalog = await self.desk._catalog_items()
        items = {str(item.get("symbol") or "").upper(): item for item in catalog}
        collected = 0
        slot = int(stamp // INTERVAL_SECONDS)
        sites = [NEWS_DOMAINS[(slot * 3 + offset) % len(NEWS_DOMAINS)] for offset in range(3)]
        suffix = " " + " OR ".join("site:" + site for site in sites)
        for desk_key, symbol, _user in self.desk.recently_read():
            product = product_key(desk_key)
            if product is None:
                continue
            item = items.get(symbol.upper())
            if item is None:
                continue
            schedule = item.get("tradingSchedule")
            if isinstance(schedule, dict) and schedule.get("dayRule") == "cme":
                sessions = sessions_of_day(cme_trading_day(ns), schedule)
            else:
                if day not in days:
                    continue  # without the exchange calendar, do not guess that the market is open
                sessions = debate_sessions(day, schedule, days) or []
            if not sessions or not any(session.start_ns <= ns < session.end_ns for session in sessions):
                continue
            cache_key = f"web_news:{product}"
            state = self.live._cache_get(cache_key) or {}
            if float(state.get("due", 0)) > stamp:
                continue
            # Persist before the request: a crash or handoff cannot repeat a burst.
            self.live._cache_put(cache_key, {"due": stamp + INTERVAL_SECONDS})
            official = slot % 2 == 0
            exchange = str(item.get("exchangeId") or "").upper()
            publisher = next((words[0] for domain, words in OFFICIAL_DOMAINS.items()
                              if words and domain.split(".")[0].upper() == exchange), "") if official else ""
            name = self.live.product_name(product)
            theme = GAP_THEMES[slot % len(GAP_THEMES)]
            query = f"{publisher} {name} {theme} after:{now.date().isoformat()}".strip()
            query = query[:128 - len(suffix)].rstrip() + suffix
            try:
                result = await self.search.search(query, count=10, official=official)
            except WebSearchError:
                self.live._cache_put(cache_key, {"due": stamp + FAILURE_SECONDS})
                log.info("news_search_unavailable product=%s", product)
                break
            rows = []
            for entry in result["results"]:
                host = urlsplit(entry["link"]).hostname or ""
                if not any(host == site or host.endswith("." + site) for site in NEWS_DOMAINS):
                    continue
                rows.append({"标题": entry["title"], "内容": entry["content"] or entry["title"],
                             "发布时间": entry["publishDate"], "链接": entry["link"], "_products": [product]})
            receipt = self.live.ingest_search(product, rows)
            collected += receipt["stored"]
            log.info("news_search_collected provider=deepseek product=%s rows=%d stored=%d official=%s",
                     product, len(rows), receipt["stored"], official)
        return collected

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            log.info("news_search_started interval_seconds=%d", POLL_SECONDS)

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 — one source round never stops the lease's producer
                log.warning("news_search_round_failed", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)
