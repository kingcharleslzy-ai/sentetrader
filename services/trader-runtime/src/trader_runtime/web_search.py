"""Runtime-owned DeepSeek Search/Fetch; models receive evidence, never provider keys."""
from __future__ import annotations

import ipaddress
import logging
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from .daily_budget import DailyCallBudget
from .secret_values import secret_value

SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
FETCH_ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESULT_COUNT = 10
ARTICLE_CHARS = 6000
CHINA = timezone(timedelta(hours=8))
# Original domestic rule/statistics publishers. Named publishers take precedence;
# a generic official question searches gov.cn rather than an arbitrary exchange.
OFFICIAL_DOMAINS = {
    "shfe.com.cn": ("上海期货", "上期所"),
    "dce.com.cn": ("大连商品", "大商所"),
    "czce.com.cn": ("郑州商品", "郑商所"),
    "cffex.com.cn": ("中国金融期货", "中金所"),
    "ine.cn": ("上海国际能源", "能源中心"),
    "gfex.com.cn": ("广州期货", "广期所"),
    "stats.gov.cn": ("统计局", "PMI", "工业生产"),
    "customs.gov.cn": ("海关",),
    "ndrc.gov.cn": ("发改委",),
    "pbc.gov.cn": ("人民银行", "央行"),
    "gov.cn": (),
}
OFFICIAL_TERMS = ("官方", "公告", "保证金", "涨跌停", "交易规则", "统计", "宏观数据",
                  "PMI", "海关", "发改委", "人民银行", "央行")
logger = logging.getLogger("uvicorn.error.agent")


def official_query(query: str, *, official: bool = False) -> str | None:
    if not official and not any(word in query for word in OFFICIAL_TERMS):
        return None
    domains = [domain for domain, words in OFFICIAL_DOMAINS.items() if any(word in query for word in words)]
    domains = domains or ["gov.cn"]
    query = " ".join(word for word in query.split() if word != "OR" and not word.startswith("site:"))
    suffix = " " + " OR ".join("site:" + domain for domain in domains[:3])
    return query[:128 - len(suffix)].rstrip() + suffix


def public_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (len(url) > 8192 or parsed.scheme not in {"http", "https"} or not host
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
                or "." not in host or host.endswith((".localhost", ".local", ".internal"))):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


class WebSearchError(RuntimeError):
    """Fail closed: a missing current source is never permission to invent one."""


class WebSearchClient:
    def __init__(self, api_key: str | None = None, endpoint: str = SEARCH_ENDPOINT, *,
                 budget: DailyCallBudget | None = None, daily_limit: int = 600):
        self._api_key = api_key
        self._endpoint = endpoint
        self.budget = budget
        self.daily_limit = daily_limit
        self._retry_at = 0.0
        self._failures = 0

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = self._api_key if self._api_key is not None else secret_value("DEEPSEEK_API_KEY")
        if not key:
            raise WebSearchError("web_search_unconfigured")
        if time.monotonic() < self._retry_at:
            raise WebSearchError("web_search_unavailable")
        day = datetime.now(CHINA).date().isoformat()
        if self.budget is not None and not self.budget.take(day, self.daily_limit):
            raise WebSearchError("web_search_unavailable")
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(endpoint, json=payload, headers={"Authorization": f"Bearer {key}"})
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("invalid search response")
        except (httpx.HTTPError, ValueError) as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            if status == 429 or status is None or status >= 500:
                self._failures = min(self._failures + 1, 5)
                self._retry_at = time.monotonic() + min(30 * 2 ** (self._failures - 1), 300)
            logger.warning("web_search_failed provider=deepseek status=%s error=%s", status, type(error).__name__)
            raise WebSearchError("web_search_unavailable") from error
        self._failures = 0
        logger.info("web_search_request provider=deepseek tool=%s", endpoint.rsplit("/", 1)[-1])
        return body

    async def search(self, query: str, *, count: int = 5, official: bool = False) -> dict[str, Any]:
        if not query.strip() or len(query) > 128:
            raise WebSearchError("invalid_tool_arguments")
        count = max(1, min(count, MAX_RESULT_COUNT))
        extra = official_query(query, official=official)
        results: dict[str, dict[str, str]] = {}
        for text in [query, *([extra] if extra and extra != query else [])]:
            body = await self._post(self._endpoint, {"query": text, "count": count, "need_content": False})
            items = body.get("results")
            if not isinstance(items, list):
                raise WebSearchError("web_search_unavailable")
            for item in items[:count]:
                if not isinstance(item, dict):
                    raise WebSearchError("web_search_unavailable")
                url = str(item.get("url") or "")
                if not public_url(url):
                    continue
                results.setdefault(url.split("#")[0], {
                    "title": str(item.get("title") or "")[:200], "link": url[:500],
                    "content": str(item.get("summary") or item.get("snippet") or "")[:1200],
                    "media": str(item.get("site_name") or "")[:80],
                    "publishDate": str(item.get("date") or "")[:32],
                })
        return {"query": query, "results": list(results.values()),
                "searchedAt": datetime.now(UTC).isoformat(timespec="seconds")}

    async def read_url(self, url: str, *, max_chars: int = ARTICLE_CHARS) -> dict[str, Any]:
        if not public_url(url):
            raise WebSearchError("web_search_unavailable")
        body = await self._post(FETCH_ENDPOINT, {"url": url})
        if not isinstance(body.get("content"), str) or not body["content"].strip():
            raise WebSearchError("web_search_unavailable")
        return {"url": url, "title": str(body.get("title") or "")[:200],
                "text": body["content"][:min(max_chars, ARTICLE_CHARS)],
                "readAt": datetime.now(UTC).isoformat(timespec="seconds")}
