"""ResearchBriefService tests: routing, independent failures, TTL cache, tool dispatch."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from trader_runtime.data_providers import FakeReadOnlyDataProvider, fake_instrument_catalog
from trader_runtime.research_brief import ResearchBriefService
from trader_runtime.trader_tools import TraderReadOnlyTools
from trader_runtime.web_search import WebSearchError


class _FakeResearch:
    """Stands in for ResearchDataService: recorded dataset/summary calls."""

    def __init__(self) -> None:
        self.dataset_calls: list[tuple[str, str]] = []
        self.summary_calls: list[str] = []

    def dataset(self, name: str, symbol: str) -> dict[str, Any]:
        self.dataset_calls.append((name, symbol))
        if name == "inventory":
            return {"status": "unavailable", "asOf": None, "source": "eastmoney",
                    "error": "source_timeout", "fetchedAt": "2026-09-20T09:00:00+00:00", "data": None}
        return {"status": "ok", "asOf": "2026-09-19", "source": "shfe",
                "error": None, "fetchedAt": "2026-09-20T09:00:00+00:00", "data": {"rows": [1]}}

    def summary(self, symbol: str) -> dict[str, Any]:
        self.summary_calls.append(symbol)
        return {"symbol": symbol, "exchange": "SHFE", "product": "rb", "contract": "rb2701",
                "generatedAt": "2026-09-20T09:00:00+00:00",
                "datasets": {"position_rank": {"status": "ok", "asOf": "2026-09-19", "error": None},
                             "inventory": {"status": "unavailable", "asOf": None, "error": "source_timeout"}},
                "notice": "研究数据来自 AKShare 免费爬虫源。"}


class _FakeMcp:
    """Scriptable search/reader with call recording."""

    def __init__(self, *, error: WebSearchError | None = None,
                 search: dict[str, Any] | None = None,
                 read_errors: set[str] | None = None) -> None:
        self.error = error
        self.search_result = search or {"query": "", "results": [
            {"title": f"消息{i}", "link": f"https://e.com/{i}", "content": f"摘要{i}",
             "media": "", "publishDate": "2026-09-20"} for i in range(5)
        ], "searchedAt": "2026-09-20T09:00:00+00:00"}
        self.read_errors = read_errors or set()
        self.search_calls: list[dict[str, Any]] = []
        self.read_calls: list[str] = []

    async def search(self, query: str, *, count: int = 5) -> dict[str, Any]:
        self.search_calls.append({"query": query, "count": count})
        if self.error is not None:
            raise self.error
        return {**self.search_result, "query": query}

    async def read_url(self, url: str, *, max_chars: int = 6000) -> dict[str, Any]:
        self.read_calls.append(url)
        if url in self.read_errors:
            raise WebSearchError("web_search_unavailable")
        return {"url": url, "title": "正文", "text": f"正文内容 {url}", "readAt": "2026-09-20T09:00:00+00:00"}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _service(mcp: _FakeMcp | None = None, research: _FakeResearch | None = None) -> tuple[ResearchBriefService, _Clock]:
    clock = _Clock()
    return ResearchBriefService(research=research or _FakeResearch(), web_search=mcp or _FakeMcp(), clock=clock,
                                instrument_catalog=fake_instrument_catalog), clock


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _tools(brief: ResearchBriefService) -> TraderReadOnlyTools:
    return TraderReadOnlyTools(FakeReadOnlyDataProvider(),
                               lambda: {"mode": "local", "runtimeConnected": True, "agentConnected": True},
                               research_brief=brief)


def test_dataset_topic_routes_to_the_research_service() -> None:
    research = _FakeResearch()
    service, _ = _service(research=research)
    payload = _run(service.brief("SHFE.rb2701", "position_rank"))
    assert research.dataset_calls == [("position_rank", "SHFE.rb2701")]
    assert payload["kind"] == "dataset" and payload["source"] == "akshare"
    assert payload["status"] == "ok" and payload["asOf"] == "2026-09-19"
    assert payload["data"] == {"rows": [1]}
    # A failing dataset keeps its own status; the brief does not raise.
    failing = _run(service.brief("SHFE.rb2701", "inventory"))
    assert failing["status"] == "unavailable" and failing["error"] == "source_timeout"


def test_news_topic_searches_reads_and_caches() -> None:
    mcp = _FakeMcp(read_errors={"https://e.com/1"})
    service, clock = _service(mcp=mcp)
    payload = _run(service.brief("SHFE.rb2701", "news"))
    assert payload["kind"] == "news" and payload["status"] == "ok"
    assert mcp.search_calls[0]["query"].startswith("螺纹钢 期货 最新 消息 观点 after:")
    # The top two links are read; the failing one degrades to an article error, the summary stays.
    assert mcp.read_calls == ["https://e.com/0", "https://e.com/1"]
    assert payload["results"][0]["article"]["status"] == "ok"
    assert payload["results"][0]["article"]["source"] == "deepseek_fetch"
    assert payload["results"][1]["article"]["status"] == "unavailable"
    assert all(entry["source"] == "deepseek_search" for entry in payload["results"])
    # Within the TTL the cached payload returns without another search.
    again = _run(service.brief("SHFE.rb2701", "news"))
    assert again["cached"] is True and len(mcp.search_calls) == 1
    # After the TTL the cache expires and a fresh search runs.
    clock.now += 31 * 60
    _run(service.brief("SHFE.rb2701", "news"))
    assert len(mcp.search_calls) == 2


def test_news_failure_degrades_without_touching_datasets() -> None:
    research = _FakeResearch()
    mcp = _FakeMcp(error=WebSearchError("web_search_unavailable"))
    service, _ = _service(mcp=mcp, research=research)
    payload = _run(service.brief("SHFE.rb2701", "news"))
    assert payload["status"] == "unavailable" and payload["results"] == []
    assert "不要编造" in payload["notice"]
    assert research.dataset_calls == []  # a dead search never touches the dataset path
    assert _run(service.brief("SHFE.rb2701", "position_rank"))["status"] == "ok"


def test_news_uses_the_shared_search_entry() -> None:
    mcp = _FakeMcp()
    service, _ = _service(mcp=mcp)
    payload = _run(service.brief("SHFE.rb2701", "news"))
    assert payload["source"] == "deepseek_search" and len(mcp.search_calls) == 1
    assert len(mcp.read_calls) == 2


def test_desk_topic_needs_the_desk_and_never_passes_a_generator() -> None:
    service, _ = _service()
    with pytest.raises(RuntimeError, match="research_desk_unavailable"):
        _run(service.brief("SHFE.rb2701", "desk"))

    class _Desk:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def report(self, symbol: str, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((symbol, kwargs))
            return {"symbol": symbol, "facts": [], "judgment": {"status": "disabled"}}

    service.desk = _Desk()
    payload = _run(service.brief("SHFE.rb2701", "desk"))
    assert payload["kind"] == "desk" and payload["topic"] == "desk"
    assert service.desk.calls == [("SHFE.rb2701", {})]


def test_unknown_topic_and_invalid_symbol_raise_value_error() -> None:
    service, _ = _service()
    with pytest.raises(ValueError, match="research_topic_unknown"):
        _run(service.brief("SHFE.rb2701", "weather"))
    with pytest.raises(ValueError, match="research_symbol_invalid"):
        _run(service.brief("rb", "news"))


def test_tool_dispatch_happy_path_replay_and_validation() -> None:
    service, _ = _service()
    tools = _tools(service)
    response, card = _run(tools._call_async(
        tool="get_research_brief", arguments={"symbol": "SHFE.rb2701", "topic": "news"},
        turn_id="turn-research", mode="local",
    ))
    assert card is None and response["success"] is True
    payload = json.loads(response["contentItems"][0]["text"])
    assert payload["tool"] == "get_research_brief" and payload["readOnly"] is True
    assert payload["data"]["kind"] == "news"

    invalid = _run(tools._call_async(
        tool="get_research_brief", arguments={"symbol": "SHFE.rb2701", "topic": "nope"},
        turn_id="turn-research-invalid", mode="local",
    ))
    assert json.loads(invalid[0]["contentItems"][0]["text"])["errorCode"] == "invalid_tool_arguments"

    tools.replay_context = object()
    replay = _run(tools._call_async(
        tool="get_research_brief", arguments={"symbol": "SHFE.rb2701", "topic": "news"},
        turn_id="turn-research-replay", mode="replay",
    ))
    assert json.loads(replay[0]["contentItems"][0]["text"])["errorCode"] == "research_brief_disabled_in_replay"


def test_definition_appears_only_when_the_service_is_wired() -> None:
    assert any(d["name"] == "get_research_brief" for d in _tools(_service()[0]).definitions())
    plain = TraderReadOnlyTools(FakeReadOnlyDataProvider(),
                                lambda: {"mode": "local", "runtimeConnected": True, "agentConnected": True})
    assert all(d["name"] != "get_research_brief" for d in plain.definitions())


def test_news_shares_product_inflight_and_keeps_failure_date_then_recovers() -> None:
    mcp = _FakeMcp()
    service, clock = _service(mcp=mcp)

    async def concurrent():
        return await asyncio.gather(service.brief("SHFE.rb2701", "news"), service.brief("SHFE.rb2705", "news"))

    first, second = _run(concurrent())
    assert len(mcp.search_calls) == 1
    assert first["symbol"] == "SHFE.rb2701" and second["symbol"] == "SHFE.rb2705"
    clock.now += 31 * 60
    mcp.error = WebSearchError("upstream_timeout")
    stale = _run(service.brief("SHFE.rb2705", "news"))
    assert stale["status"] == "stale" and stale["error"] == "upstream_timeout"
    assert stale["searchedAt"] == first["searchedAt"] and stale["results"] == first["results"]
    mcp.error = None
    for _ in range(3):
        still_stale = _run(service.brief("SHFE.rb2701", "news"))
        assert still_stale["status"] == "stale" and still_stale["fetchedAt"] == first["fetchedAt"]
    assert len(mcp.search_calls) == 2
    clock.now += 61
    recovered = _run(service.brief("SHFE.rb2705", "news"))
    assert recovered["status"] == "ok" and "error" not in recovered
    assert len(mcp.search_calls) == 3
    _run(service.brief("DCE.i2701", "news"))
    assert len(mcp.search_calls) == 4


def test_cancelled_news_waiter_does_not_cancel_shared_source() -> None:
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        mcp = _FakeMcp()
        original = mcp.search
        async def slow(*args, **kwargs):
            started.set()
            await finish.wait()
            return await original(*args, **kwargs)
        mcp.search = slow
        service, _ = _service(mcp=mcp)
        one = asyncio.create_task(service.brief("SHFE.rb2701", "news"))
        await started.wait()
        two = asyncio.create_task(service.brief("SHFE.rb2705", "news"))
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        finish.set()
        assert (await two)["status"] == "ok"
        assert len(mcp.search_calls) == 1
    _run(scenario())


def test_news_ui_receipt_checks_requested_fields_and_follow_mode():
    from trader_runtime.ui_control import UiChanges, changes_match
    UiChanges(news={"tab": "data"})
    state = {"panel": "news", "news": {"tab": "data", "pinnedSymbol": None, "symbol": "SHFE.rb2701"}}
    assert changes_match(state, {"news": {"tab": "data", "pinnedSymbol": None}})
    assert not changes_match(state, {"news": {"pinnedSymbol": "DCE.i2701"}})


def test_initial_news_failure_has_short_ttl_and_catalog_product_query():
    mcp = _FakeMcp(error=WebSearchError("source_timeout"))
    service, clock = _service(mcp=mcp)
    for symbol in ["DCE.i2701", "DCE.i2705", "DCE.i2709"]:
        result = _run(service.brief(symbol, "news"))
        assert result["status"] == "unavailable" and result["error"] == "source_timeout"
        assert "fetchedAt" not in result
    assert len(mcp.search_calls) == 1
    assert mcp.search_calls[0]["query"].startswith("铁矿石 期货 最新 消息 观点 after:")
    clock.now += 61
    mcp.error = None
    assert _run(service.brief("DCE.i2701", "news"))["status"] == "ok"
    assert len(mcp.search_calls) == 2


def test_an_empty_search_keeps_the_last_news_and_is_not_cached_as_no_news():
    """2026-10-12 数据获取审查：智谱一周检索空结果不当“无新闻”缓存 15 分钟：有旧结果就标 stale 照用，
    冷启动报不可用，都只等短间隔再检索。"""
    full = _FakeMcp()
    empty = {"query": "", "results": [], "searchedAt": "2026-09-20T10:00:00+00:00"}
    service, clock = _service(mcp=full)
    first = _run(service.brief("SHFE.rb2701", "news"))
    assert first["status"] == "ok" and len(first["results"]) == 5
    clock.now += 16 * 60
    full.search_result = empty
    kept = _run(service.brief("SHFE.rb2701", "news"))
    assert kept["status"] == "stale" and kept["error"] == "research_news_empty"
    assert kept["results"] == first["results"] and kept["fetchedAt"] == first["fetchedAt"]
    clock.now += 4 * 60
    assert _run(service.brief("SHFE.rb2701", "news"))["status"] == "stale" and len(full.search_calls) == 2
    clock.now += 61  # past the five-minute empty retry, well inside the 15-minute TTL
    _run(service.brief("SHFE.rb2701", "news"))
    assert len(full.search_calls) == 3

    cold = _FakeMcp(search=empty)
    service, clock = _service(mcp=cold)
    result = _run(service.brief("SHFE.rb2701", "news"))
    assert result["status"] == "unavailable" and result["error"] == "research_news_empty" and result["results"] == []
    assert result["query"]  # still says what was searched (the query the empty search ran)
    cold.search = _FakeMcp().search
    clock.now += 5 * 60 + 1
    assert _run(service.brief("SHFE.rb2701", "news"))["status"] == "ok"


def test_foreign_news_is_searched_by_the_foreign_name_and_screened_under_its_own_key() -> None:
    """2026-10-21 所有者：打开迷你纳指，资讯不可用——资讯在国内合约目录里找品种名，外盘找不到。现在外盘按自己的
    说法搜：有国内对照的用外盘名（COMEX黄金），没有的用它快讯键的名字（迷你纳指100 → 纳指）。"""
    screened: list[str] = []

    class _Live:
        def product_name(self, product: str) -> str:
            return {"nasdaq": "纳指"}.get(product, product)

        def screen_items(self, product: str, candidates: list[Any]) -> dict[str, Any]:
            screened.append(product)
            return {"kept": [], "dropped": [], "pending": 0, "unscreened": 0, "mode": "keyword", "reason": None}

    search = _FakeMcp()
    service, _ = _service(mcp=search)
    service.live = _Live()  # type: ignore[assignment]
    payload = _run(service.brief("KQD.m@CME.NQ", "news"))
    assert payload["status"] == "ok" and search.search_calls[0]["query"].startswith("纳指 ") and screened == ["nasdaq"]
    _run(service.brief("CME.GCEZ26", "news"))
    assert search.search_calls[-1]["query"].startswith("COMEX黄金 ") and screened[-1] == "au"
