"""The agent's single research entry: AKShare datasets, product news and the research desk.

Dataset topics delegate to ResearchDataService. News uses the same DeepSeek search
and bounded Fetch entry as the freshness gate; the desk combines these facts
with the shared judgment. Each source fails independently.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .collection import AsyncCollector, SourceFailed
from .instruments import ForeignProduct
from .last_good import LastGood
from .news_screen import build_events, layer_summary, screen_summary
from .research_data import DATASETS, ResearchDataService, research_target
from .web_search import WebSearchClient, WebSearchError

if TYPE_CHECKING:
    from .research_desk import ResearchDeskService
    from .research_live import ResearchLiveService

log = logging.getLogger("uvicorn.error.research")

NEWS_TTL_SECONDS = 15 * 60
NEWS_FAILURE_TTL_SECONDS = 60
# Search answering nothing for a week-long search of a product name is a degraded source, not "no news":
# the last good results stay (stale) and the search is retried after this, not after the full TTL.
NEWS_EMPTY_TTL_SECONDS = 5 * 60
NEWS_RESULT_COUNT = 5
NEWS_READ_COUNT = 2
NEWS_HEADLINES = 3
TOPICS = (*DATASETS, "news", "desk")
NEWS_NOTICE = "消息面由 DeepSeek 搜索第三方媒体与官方原文，仅为背景信息；不得当作行情事实或编造缺失内容。"


@dataclass
class ResearchBriefService:
    """`brief(symbol, topic)` for the agent tool; dataset caching stays inside the research service."""

    research: ResearchDataService
    web_search: WebSearchClient
    instrument_catalog: Callable[[], dict[str, Any]]
    desk: ResearchDeskService | None = None
    # The live service screens 相关报道 like the 快讯: keyword 排除词 first, then the model's verdicts.
    live: ResearchLiveService | None = None
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        # The last good news per product, kept beside the live caches (a restart reads it back).
        self._news = LastGood(getattr(self.research, "connection", None), getattr(self.research, "lock", None),
                              prefix="news:", clock=lambda: self.clock())
        self._news_collect = AsyncCollector(self._news, lane="research-news")
        # The last query each product's search used: an unavailable answer still says what was searched.
        self._news_queries: dict[str, str] = {}

    async def close(self) -> None:
        """Shutdown (lifespan): running news searches are cancelled and no new one starts (#932)."""
        await self._news_collect.close()

    async def brief(self, symbol: str, topic: str) -> dict[str, Any]:
        if topic not in TOPICS:
            raise ValueError("research_topic_unknown")
        research_target(symbol)  # ValueError("research_symbol_invalid")
        if topic in DATASETS:
            return await self._dataset_brief(symbol, topic)
        if topic == "news":
            return await self._news_brief(symbol)
        if self.desk is None:
            raise RuntimeError("research_desk_unavailable")
        # The agent reads the facts and any existing judgment; it never starts a generation.
        return {**await self.desk.report(symbol), "topic": "desk", "kind": "desk"}

    async def _dataset_brief(self, symbol: str, topic: str) -> dict[str, Any]:
        payload = await asyncio.to_thread(self.research.dataset, topic, symbol)
        return {
            "symbol": symbol, "topic": topic, "kind": "dataset", "source": "akshare",
            "status": payload.get("status"), "asOf": payload.get("asOf"),
            "sourceDetail": payload.get("source"), "error": payload.get("error"),
            "fetchedAt": payload.get("fetchedAt"), "data": payload.get("data"),
        }

    async def _news_brief(self, symbol: str) -> dict[str, Any]:
        exchange, product, foreign = research_target(symbol)
        key = f"{exchange}.{product.lower()}"
        # 外盘按它自己的快讯键筛（纳指 → nasdaq，COMEX黄金 → au），与快讯页同一把尺子（2026-10-21）。
        screen = (foreign.news_product or "") if foreign is not None else product.lower()
        fresh = not self._news.due(key, NEWS_TTL_SECONDS)
        try:
            # One search per product at a time, every reader waits on it (the shared async collector).
            kept = await self._news_collect.get(key, lambda: self._fetch_news(key, product, foreign), ttl=NEWS_TTL_SECONDS,
                                                retry=NEWS_FAILURE_TTL_SECONDS, timeout=None,
                                                timeout_error="research_news_timeout", unavailable="research_news_unavailable")
            payload = kept.mark(kept.value)
        except SourceFailed as failure:
            payload = self._news_unavailable(failure.code, self._news_queries.get(key, ""))
        return self._screened({**payload, "symbol": symbol, **({"cached": True} if fresh else {})}, screen)

    def _screened(self, payload: dict[str, Any], product: str) -> dict[str, Any]:
        """The cached search results through the product's screening, read fresh on every call so a
        verdict reached since the search shows at once: kept results carry `ai`, the rest fold into
        `screen.dropped`; `events` merges the reports of one event and tiers them like the 快讯."""
        results = payload.get("results") or []
        if self.live is None or not results or not product:
            return payload
        candidates = []
        for entry in results:
            digest = hashlib.sha1(str(entry.get("link") or entry.get("title") or "").encode()).hexdigest()[:15]
            candidates.append(({"id": f"n{digest}", "title": str(entry.get("title") or ""),
                                "text": str(entry.get("summary") or ""), "source": str(entry.get("media") or ""),
                                "publishedAt": str(entry.get("publishDate") or ""), "link": entry.get("link") or None,
                                "entry": entry}, True))
        view = self.live.screen_items(product, candidates)
        screen = screen_summary(view, [{**item["entry"], "dropReason": item["dropReason"], "droppedBy": item["droppedBy"]}
                                       for item in view["dropped"]])
        events = build_events(view["kept"], hide_pending=view["mode"] == "ai")
        return {**payload, "results": [{**item["entry"], "ai": item["ai"]} for item in view["kept"]], "screen": screen,
                "events": events, "layers": layer_summary(events)}

    @staticmethod
    def _news_unavailable(error: str, query: str) -> dict[str, Any]:
        return {"topic": "news", "kind": "news", "status": "unavailable", "source": "deepseek_search",
                "error": error or "research_news_unavailable", "query": query, "results": [],
                "notice": "研究信息暂不可用；不要编造消息面内容。"}

    def _foreign_name(self, foreign: ForeignProduct) -> str:
        """The words a foreign product's news is searched by: its own name when it has a domestic counterpart
        (COMEX黄金), else the name its 快讯 key screens under (迷你纳指100 → 纳指), which is what the news says."""
        if foreign.domestic or self.live is None or not foreign.news_product:
            return foreign.name
        return self.live.product_name(foreign.news_product)

    async def _fetch_news(self, key: str, product: str, foreign: ForeignProduct | None = None) -> dict[str, Any]:
        query = ""
        try:
            if foreign is not None:
                name = self._foreign_name(foreign)
            else:
                catalog = await asyncio.to_thread(self.instrument_catalog)
                exchange = key.split(".")[0]
                item = next((item for item in catalog.get("items", [])
                             if str(item.get("exchangeId", "")).upper() == exchange
                             and str(item.get("productId", "")).lower() == product.lower()
                             and item.get("instrumentName")), None)
                if item is None:
                    raise WebSearchError("research_product_name_unavailable")
                name = re.sub(r"[0-9]+$", "", str(item["instrumentName"]))
            since = (self.now() - timedelta(days=7)).date().isoformat()
            query = f"{name} 期货 最新 消息 观点 after:{since}"
            self._news_queries[key] = query
            search = await self.web_search.search(query, count=NEWS_RESULT_COUNT)
        except (WebSearchError, RuntimeError, ValueError) as error:
            log.warning("research_news_failed symbol=%s error=%s", key, error)
            raise SourceFailed(str(error), NEWS_FAILURE_TTL_SECONDS) from error
        entries: list[dict[str, Any]] = [
            {
                "title": item.get("title", ""), "link": item.get("link", ""),
                "summary": item.get("content", ""), "media": item.get("media", ""),
                "publishDate": item.get("publishDate", ""), "source": "deepseek_search",
            }
            for item in search.get("results") or [] if isinstance(item, dict)
        ]
        if not entries:
            log.warning("research_news_empty symbol=%s", key)
            self._news_queries[key] = search.get("query") or query
            raise SourceFailed("research_news_empty", NEWS_EMPTY_TTL_SECONDS)
        for entry in entries[:NEWS_READ_COUNT]:
            link = entry.get("link") or ""
            if not link.startswith(("http://", "https://")):
                continue
            try:
                page = await self.web_search.read_url(link)
            except WebSearchError as error:
                # A page that will not load only loses its body; the summary stays.
                log.warning("research_news_read_failed url=%r error=%s", link, error)
                entry["article"] = {"source": "deepseek_fetch", "status": "unavailable", "error": str(error)}
                continue
            entry["article"] = {"source": "deepseek_fetch", "status": "ok", "text": page["text"]}
        payload = {
            "topic": "news", "kind": "news", "status": "ok",
            "source": "deepseek_search", "query": search.get("query") or query,
            "searchedAt": search.get("searchedAt"), "fetchedAt": self.now().isoformat(timespec="seconds"), "results": entries, "notice": NEWS_NOTICE,
        }
        return payload
