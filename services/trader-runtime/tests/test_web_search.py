"""Shared search entry: provider evidence, official recall and fail-closed boundaries."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from typing import Any

import httpx
import pytest

from trader_runtime.daily_budget import DailyCallBudget
from trader_runtime.data_providers import FakeReadOnlyDataProvider
from trader_runtime.trader_tools import TraderReadOnlyTools
from trader_runtime.web_search import WebSearchClient, WebSearchError, official_query


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    calls = []
    original = httpx.AsyncClient
    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(send), **kw))
    return calls


def result(url: str = "https://example.com/news") -> dict:
    return {"title": "期货新闻", "url": url, "summary": "摘要" * 900,
            "site_name": "来源", "date": "2026-10-24T00:00:00+00:00"}


def test_search_returns_existing_shape_and_deepseek_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = transport(monkeypatch, lambda req: httpx.Response(200, json={"results": [result()]}))
    value = run(WebSearchClient(api_key="k").search("螺纹钢最新消息", count=5))
    assert value["results"] == [{"title": "期货新闻", "link": "https://example.com/news",
        "content": "摘要" * 600, "media": "来源", "publishDate": "2026-10-24T00:00:00+00:00"}]
    assert value["searchedAt"] and value["query"] == "螺纹钢最新消息"
    assert str(calls[0].url) == "https://api.deepseek.com/v1/tools/search"
    assert calls[0].headers["Authorization"] == "Bearer k"
    assert json.loads(calls[0].content) == {"query": "螺纹钢最新消息", "count": 5, "need_content": False}


def test_official_query_merges_urls_without_displacing_base(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        extra = "site:" in json.loads(req.content)["query"]
        items = [result("https://example.com/news#different"), result("https://www.shfe.com.cn/notice/a")] if extra else [result()]
        return httpx.Response(200, json={"results": items})
    calls = transport(monkeypatch, handler)
    value = run(WebSearchClient(api_key="k").search("上海期货交易所 黄金 保证金 公告"))
    assert len(calls) == 2 and len(value["results"]) == 2
    assert value["results"][0]["link"] == "https://example.com/news"
    assert json.loads(calls[1].content)["query"].endswith("site:shfe.com.cn")
    assert official_query("豆粕当天新闻") is None
    assert official_query("豆粕当天新闻", official=True).endswith("site:gov.cn")
    assert len(official_query("国家统计局 统计" + "字" * 120)) <= 128


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_fail_closed(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    transport(monkeypatch, lambda req: httpx.Response(status))
    with pytest.raises(WebSearchError, match="web_search_unavailable"):
        run(WebSearchClient(api_key="k").search("新闻"))


def test_official_failure_discards_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = transport(monkeypatch, lambda req: httpx.Response(401) if "site:" in json.loads(req.content)["query"]
                      else httpx.Response(200, json={"results": [result()]}))
    with pytest.raises(WebSearchError):
        run(WebSearchClient(api_key="k").search("最新公告"))
    assert len(calls) == 2


def test_timeout_and_retry_backoff_spend_no_extra_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")
    calls = transport(monkeypatch, handler)
    client = WebSearchClient(api_key="k")
    for _ in range(2):
        with pytest.raises(WebSearchError):
            run(client.search("新闻"))
    assert len(calls) == 1


def test_daily_budget_shared_across_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = transport(monkeypatch, lambda req: httpx.Response(200, json={"results": []}))
    budget = DailyCallBudget("news-search", sqlite3.connect(":memory:"), threading.RLock())
    run(WebSearchClient(api_key="k", budget=budget, daily_limit=1).search("新闻"))
    with pytest.raises(WebSearchError):
        run(WebSearchClient(api_key="k", budget=budget, daily_limit=1).search("新闻"))
    assert len(calls) == 1


def test_fetch_markdown_is_bounded_and_invalid_urls_never_called(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = transport(monkeypatch, lambda req: httpx.Response(200, json={"title": "原文", "content": "文" * 9000}))
    client = WebSearchClient(api_key="k")
    assert len(run(client.read_url("https://example.com/news"))["text"]) == 6000
    assert str(calls[0].url).endswith("/v1/tools/fetch")
    for url in ["http://127.0.0.1", "http://localhost", "file:///etc/passwd", "https://u:p@example.com/"]:
        with pytest.raises(WebSearchError):
            run(client.read_url(url))
    assert len(calls) == 1


def test_missing_key_and_invalid_query(monkeypatch: pytest.MonkeyPatch) -> None:
    for query in ["", "字" * 129]:
        with pytest.raises(WebSearchError):
            run(WebSearchClient(api_key="k").search(query))
    with pytest.raises(WebSearchError, match="web_search_unconfigured"):
        run(WebSearchClient(api_key="").search("新闻"))


def test_tool_dispatch_preserves_authenticated_read_only_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    transport(monkeypatch, lambda req: httpx.Response(200, json={"results": [result()]}))
    tools = TraderReadOnlyTools(FakeReadOnlyDataProvider(),
        lambda mode: {"mode": mode, "runtimeConnected": True, "agentConnected": True},
        web_search=WebSearchClient(api_key="k"))
    response, _ = run(tools._call_async(tool="web_search", arguments={"query": "螺纹钢最新消息"},
                                      turn_id="search-test", mode="local"))
    payload = json.loads(response["contentItems"][0]["text"])
    assert payload["readOnly"] and payload["data"]["results"][0]["title"] == "期货新闻"
    tools.replay_context = object()
    response, _ = run(tools._call_async(tool="web_search", arguments={"query": "新闻"},
                                      turn_id="replay-search", mode="replay"))
    assert json.loads(response["contentItems"][0]["text"])["errorCode"] == "web_search_disabled_in_replay"
