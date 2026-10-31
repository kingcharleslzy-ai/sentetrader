"""News screening for a product view: keyword recall and 排除词, the model's verdicts (a fake model),
the per-pair cache, the daily cap and model failures falling back to keywords, the desk's picks,
and the RSSHub sources (skipped unless configured). Recorded rows only; no network, no sleeping."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from trader_runtime import research_live as live
from trader_runtime.news_screen import BATCH_PAIRS, FAILURE_BACKOFF_SECONDS, MAX_PENDING, NewsScreener, ScreenBatch
from trader_runtime.research_live import ResearchLiveService, keyword_drop, parse_rsshub_feed, product_key

NOW = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)  # 10:00 Beijing


class Rows(list):
    def to_dict(self, orient: str = "records") -> list[dict]:
        return list(self)


CLS = Rows([
    {"标题": "", "内容": "财联社10月7日电，山东栖霞冷库红富士出库偏慢，客商采购以质优货为主，一般货走货缓慢。",
     "发布日期": datetime(2026, 10, 7).date(), "发布时间": datetime(2026, 10, 7, 9, 12).time()},
    {"标题": "", "内容": "财联社10月7日电，也门胡塞武装称使用无人机袭击了利雅得机场，美联储官员同日表示降息仍需观察。",
     "发布日期": datetime(2026, 10, 7).date(), "发布时间": datetime(2026, 10, 7, 9, 30).time()},
])
THS = Rows([
    {"标题": "苹果发布新品", "内容": "苹果公司发布新一代 iPhone，库克称本季度营收有望创新高。", "发布时间": "2026-10-07 09:40:00",
     "链接": "https://news.10jqka.com.cn/1.shtml"},
])


class Recorded:
    def stock_info_global_cls(self, symbol="全部"):
        return CLS

    def stock_info_global_ths(self):
        return THS

    def stock_info_global_em(self):
        return Rows()

    def stock_info_global_sina(self):
        return Rows()

    def stock_info_global_futu(self):
        return Rows()

    def futures_news_shmet(self, symbol="全部"):
        return Rows()


class FakeModel:
    """The adapter surface the screener uses: answers each pair from `answer(pair)`."""

    agent_authenticated = True
    model = "fake-flash"

    def __init__(self, answer, fail: bool = False) -> None:
        self.answer, self.fail, self.calls = answer, fail, []

    async def complete_structured(self, *, instructions, input_text, schema, timeout_seconds, model=None, usage_user=None, **_):
        assert schema is ScreenBatch and usage_user == "platform:news-screen"
        pairs = json.loads(input_text)["pairs"]
        self.calls.append(pairs)
        if self.fail:
            raise RuntimeError("model_request_failed")
        verdicts = [{"key": pair["key"], **self.answer(pair)} for pair in pairs]
        return type("Result", (), {"value": ScreenBatch.model_validate({"verdicts": verdicts}), "tokens": 900})()


class GatedModel(FakeModel):
    """FakeModel whose calls wait on `gate`: a model call still in flight while readers come back."""

    def __init__(self, answer) -> None:
        super().__init__(answer)
        self.started = 0
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def complete_structured(self, **kwargs):
        self.started += 1
        self.entered.set()
        await self.gate.wait()
        return await super().complete_structured(**kwargs)


def judge(pair: dict[str, Any]) -> dict[str, Any]:
    if "冷库" in pair["text"]:
        return {"relevant": True, "event": "库存", "direction": "偏空", "strength": "中",
                "reason": "冷库出库慢于去年，现货压力偏大，库存去化不及预期还会继续拖累价格",
                "transmission": "冷库出库慢，现货压力传到期货", "eventLabel": "栖霞冷库出库偏慢"}
    return {"relevant": False, "dropReason": "无关国际新闻"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build(tmp_path, model: FakeModel | None, *, daily_calls: int = 50, rsshub: str = "",
          rsshub_fetch: Any = None) -> ResearchLiveService:
    tmp_path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(tmp_path / "screen.sqlite3", check_same_thread=False)
    lock = threading.Lock()
    sources = live.LiveSources()
    sources._module = Recorded()
    if rsshub_fetch is not None:
        sources.rsshub = rsshub_fetch  # type: ignore[method-assign]
        # 云端一并启用的直读源（交易所公告页、行业线、英文外电、中文财经媒体）也换成录制的空答案：测试不出网；
        # 漏换的直读源读页面时直接失败，同样不出网。
        sources.notice_shfe = sources.notice_gfex = sources.news_smm = sources.news_mysteel = \
            sources.flash_financialjuice = sources.news_stcn = sources.news_fx678 = sources.news_thepaper = \
            sources.news_nbd = lambda: []  # type: ignore[method-assign]
        sources.notice_exchange_board = lambda: {}  # type: ignore[method-assign]
        sources.news_cctv = lambda day: []  # type: ignore[method-assign]

        def offline(*args: Any, **kwargs: Any) -> str:
            raise OSError("offline")

        sources._fetch_page = offline  # type: ignore[method-assign]
    clock = Clock()
    screener = NewsScreener(connection=connection, lock=lock, generator=(lambda: model) if model else None,
                            model="", daily_calls=daily_calls, clock=clock, now=lambda: NOW)
    catalog = {"items": [{"symbol": "CZCE.AP701", "isMain": True, "productId": "AP", "instrumentName": "苹果701"}]}
    service = ResearchLiveService(connection=connection, lock=lock, instrument_catalog=lambda: catalog, sources=sources,
                                  clock=clock, now=lambda: NOW, screener=screener, rsshub_url=lambda: rsshub)
    service.poll_flash_once()
    return service


def test_apple_view_drops_the_company_keeps_the_orchard_with_a_reading_and_judges_once(tmp_path):
    model = FakeModel(judge)
    svc = build(tmp_path, model)
    first = svc.flash_feed(product="CZCE.ap")
    # Before the model runs: 苹果公司 is already out by its 排除词; the orchard item shows on its
    # keywords; the macro item waits for the model (not shown, counted as pending).
    assert first["screen"]["mode"] == "ai" and first["screen"]["pending"] == 2
    assert [item["text"][:6] for item in first["items"]] == ["山东栖霞冷库"] and first["items"][0]["ai"] is None
    assert first["screen"]["dropped"]["groups"] == [{"reason": "苹果公司新品与财报", "count": 1}]
    assert first["screen"]["dropped"]["items"][0]["droppedBy"] == "keyword"
    assert not any("iPhone" in pair["text"] for batch in model.calls for pair in batch)

    assert asyncio.run(svc.screener.burst()) == 2
    assert len(model.calls) == 1 and {pair["product"] for pair in model.calls[0]} == {"苹果（ap）"}
    view = svc.flash_feed(product="CZCE.ap")
    orchard = view["items"][0]
    assert orchard["ai"] == {"event": "库存", "direction": "偏空", "strength": "中",
                             "reason": "冷库出库慢于去年，现货压力偏大，库存去化不及预期还会继续拖累价格"[:30], "product": "ap",
                             "transmission": "冷库出库慢，现货压力传到期货", "eventLabel": "栖霞冷库出库偏慢"}
    assert len(orchard["ai"]["reason"]) == 30
    assert view["count"] == 1 and view["screen"]["pending"] == 0 and "note" not in view["screen"]
    assert view["screen"]["dropped"]["count"] == 2
    assert {group["reason"] for group in view["screen"]["dropped"]["groups"]} == {"苹果公司新品与财报", "无关国际新闻"}
    assert view["sources"] == ["财联社", "东方财富", "新浪", "同花顺", "富途", "上海金属网"]

    # Judged once: later reads and another burst make no model call.
    svc.flash_feed(product="CZCE.ap")
    assert asyncio.run(svc.screener.burst()) == 0 and len(model.calls) == 1
    # The desk's picks: the orchard item only — a dropped item never becomes a fact.
    assert [item["text"][:6] for item in svc.flash_pick("ap")] == ["山东栖霞冷库"]


def test_over_the_daily_cap_or_with_a_failing_model_the_view_falls_back_to_keywords(tmp_path):
    capped = build(tmp_path / "cap", FakeModel(judge), daily_calls=0)
    view = capped.flash_feed(product="CZCE.ap")
    assert view["screen"]["mode"] == "keyword" and "今天的 AI 筛选额度已用完" in view["screen"]["note"]
    assert "未经 AI 筛选" in view["screen"]["note"] and view["screen"]["pending"] == 0
    assert [item["text"][:6] for item in view["items"]] == ["山东栖霞冷库"]  # keyword recall stands
    assert view["screen"]["dropped"]["groups"] == [{"reason": "苹果公司新品与财报", "count": 1}]
    assert asyncio.run(capped.screener.burst()) == 0

    failing = FakeModel(judge, fail=True)
    svc = build(tmp_path / "fail", failing)
    svc.flash_feed(product="CZCE.ap")
    assert asyncio.run(svc.screener.burst()) == 0 and len(failing.calls) == 1  # backs off after the failure
    view = svc.flash_feed(product="CZCE.ap")
    assert view["screen"]["mode"] == "keyword" and "AI 筛选暂时失败" in view["screen"]["note"]
    assert view["items"] and view["items"][0]["ai"] is None
    assert [item["text"][:6] for item in svc.flash_pick("ap")] == ["山东栖霞冷库"]

    unwired = build(tmp_path / "none", None)
    assert unwired.flash_feed(product="CZCE.ap")["screen"]["mode"] == "keyword"


def test_keyword_drop_needs_an_exclude_word_without_a_chain_word():
    company = {"title": "", "text": "苹果公司发布新一代 iPhone"}
    orchard_next_to_iphone = {"title": "", "text": "栖霞冷库出库偏慢，有果商用 iPhone 拍视频"}
    assert keyword_drop(company, "ap") == "苹果公司新品与财报"
    assert keyword_drop(orchard_next_to_iphone, "ap") is None
    assert keyword_drop(company, "rb") is None


def test_rsshub_sources_are_skipped_unless_configured_and_parse_when_they_are(tmp_path):
    calls: list[str] = []

    def rsshub(base: str, route: str) -> Any:
        calls.append(f"{base}{route}")
        if route == "/jin10":
            return {"items": [{"id": "jin10:index:1", "title": "<b>郑商所</b>上调苹果期货保证金",
                               "content_html": "郑商所上调苹果期货保证金", "date_published": "2026-10-07T01:55:00.000Z"}]}
        if route.startswith("/oilchem"):
            return {"items": [{"id": "https://www.oilchem.net/1.html", "url": "https://www.oilchem.net/1.html",
                               "title": "[甲醇]：港口库存回落", "content_html": "<table>免费开通</table>",
                               "date_published": "2026-10-07T01:40:00.000Z"}]}
        raise OSError("route down")

    plain = build(tmp_path / "off", None)
    assert "金十" not in plain.flash_sources() and calls == []

    svc = build(tmp_path / "on", None, rsshub="http://rsshub:1200", rsshub_fetch=rsshub)
    assert "金十" in svc.flash_sources() and "隆众资讯" in svc.flash_sources()
    assert {item["source"] for item in svc.flash_feed()["items"]} >= {"金十", "隆众资讯"}
    assert {"华尔街见闻", "第一财经"} <= set(svc.flash_feed()["lastPoll"]["failedSources"])
    items = {item["source"]: item for item in svc.flash_feed()["items"]}
    assert items["金十"]["text"] == "郑商所上调苹果期货保证金" and items["金十"]["link"] is None
    assert "ap" in items["金十"]["products"]
    assert items["隆众资讯"]["text"] == "[甲醇]：港口库存回落" and items["隆众资讯"]["link"] == "https://www.oilchem.net/1.html"
    # A route read this round waits for its interval; a failed one is tried again next round —
    # the official routes (exchanges and ministries) fail like the down media routes and retry too.
    calls.clear()
    svc.poll_flash_once()
    retried = sorted(calls)
    assert "http://rsshub:1200/wallstreetcn/live/global" in retried and "http://rsshub:1200/yicai/brief" in retried
    assert "http://rsshub:1200/cffex/announcement" in retried and "http://rsshub:1200/gov/stats" in retried
    assert "http://rsshub:1200/jin10" not in calls  # 这轮读到内容的按各自间隔等
    assert len(retried) == 14  # 四条媒体 + 十条官方路由，成功的不再读


def test_parse_rsshub_feed_skips_undated_items():
    rows = parse_rsshub_feed({"items": [{"title": "无日期"}, {"title": "有", "date_published": "2026-10-07T01:00:00Z",
                                                                 "content_html": "<p>正文</p>", "id": 3782056}]})
    assert len(rows) == 1 and rows[0]["内容"] == "正文" and rows[0]["链接"] is None


def test_several_products_view_unions_the_screened_items(tmp_path):
    watched = ["CZCE.ap", "SHFE.rb"]
    model = FakeModel(judge)
    svc = build(tmp_path, model)
    svc.flash_feed(products=watched)
    asyncio.run(svc.screener.burst())
    view = svc.flash_feed(products=watched)
    assert view["products"] == ["ap", "rb"] and [item["text"][:6] for item in view["items"]] == ["山东栖霞冷库"]
    assert view["screen"]["dropped"]["groups"] == [{"reason": "苹果公司新品与财报", "count": 1}]


def test_related_reports_go_through_the_same_screening(tmp_path):
    from trader_runtime.research_brief import ResearchBriefService

    model = FakeModel(judge)
    svc = build(tmp_path, model)
    brief = ResearchBriefService(research=None, web_search=None, instrument_catalog=lambda: {}, live=svc)  # type: ignore[arg-type]
    payload = {"status": "ok", "source": "eastmoney", "results": [
        {"title": "苹果公司三季度财报超预期", "summary": "iPhone 销量增长", "media": "东方财富", "source": "eastmoney",
         "publishDate": "2026-10-07T09:00:00+08:00", "link": "https://finance.eastmoney.com/a/1.html"},
        {"title": "栖霞冷库苹果出库偏慢", "summary": "冷库出库慢于去年", "media": "期货日报", "source": "eastmoney",
         "publishDate": "2026-10-07T08:00:00+08:00", "link": "https://finance.eastmoney.com/a/2.html"},
    ]}
    first = brief._screened(payload, "ap")
    assert [entry["title"] for entry in first["results"]] == ["栖霞冷库苹果出库偏慢"]
    assert first["results"][0]["ai"] is None and first["results"][0]["source"] == "eastmoney"
    assert first["screen"]["dropped"]["groups"] == [{"reason": "苹果公司新品与财报", "count": 1}]
    asyncio.run(svc.screener.burst())
    again = brief._screened(payload, "ap")
    assert again["results"][0]["ai"]["event"] == "库存" and again["screen"]["pending"] == 0


def test_drop_groups_are_the_fixed_names_or_the_products_own():
    from trader_runtime.news_screen import DROP_GROUPS, ScreenVerdict, normalise_verdict

    coined = ScreenVerdict(key="1", relevant=False, dropReason="无关国际宏观")
    assert normalise_verdict(coined)["dropReason"] == "其他无关内容"  # a coined name would split the fold's counts
    own = ScreenVerdict(key="1", relevant=False, dropReason="苹果公司新品与财报")
    assert normalise_verdict(own, DROP_GROUPS + ("苹果公司新品与财报",))["dropReason"] == "苹果公司新品与财报"
    loose = normalise_verdict(ScreenVerdict(key="1", relevant=True, event="天气", direction="利多", strength="很强",
                                            transmission="减产推高现货"))
    assert loose == {"relevant": True, "event": "天气·供给", "direction": "中性", "strength": "弱", "reason": "",
                     "transmission": "减产推高现货", "eventLabel": None}


def test_gold_futures_are_not_mistaken_for_the_golden_week():
    assert keyword_drop({"title": "", "text": "国庆黄金周出游人次创新高"}, "au") == "黄金周等同名说法"
    assert keyword_drop({"title": "", "text": "黄金期货夜盘走高 0.8%"}, "au") is None


def test_the_poller_screens_recently_read_products_only(tmp_path):
    model = FakeModel(judge)
    svc = build(tmp_path, model)
    assert svc.screener.active() == []  # nobody has read a product yet: the poll queues nothing
    svc.flash_feed(product="CZCE.ap")
    assert [product for product, _ in svc.screener.active()] == ["ap"]
    # The read is in the shared table: the lease holder's screener (another process) polls it too.
    holder = NewsScreener(connection=svc.connection, lock=svc.lock, model="", now=lambda: NOW)
    assert [product for product, _ in holder.active()] == ["ap"]
    svc.screener.now = lambda: NOW + timedelta(hours=4)  # wall clock: comparable across processes
    svc.poll_flash_once()  # the poller's own refill does not count as a read
    assert svc.screener.active() == []


def test_a_pair_in_flight_is_not_queued_again_and_is_judged_once(tmp_path):
    async def scenario() -> None:
        model = GatedModel(judge)
        svc = build(tmp_path, model)
        screener = svc.screener
        assert svc.flash_feed(product="CZCE.ap")["screen"]["pending"] == 2
        burst = asyncio.create_task(screener.burst())
        await model.entered.wait()  # the model call is in flight
        screener.clock.now += 15  # type: ignore[attr-defined]  # FlashFeed's 15 s pending re-read
        again = svc.flash_feed(product="CZCE.ap")
        assert again["screen"]["mode"] == "ai" and again["screen"]["pending"] == 2  # in flight, still honest
        svc.flash_feed(products=["CZCE.ap", "SHFE.rb"])  # another reader
        assert svc.flash_feed(product="SHFE.rb")["screen"]["pending"] == 1  # a new pair queues as usual
        assert screener.usage()["pending"] == 3 and screener.state(again["items"][0]["id"], "ap") == "inflight"
        model.gate.set()
        assert await burst == 3
        assert model.started == 2 and len(model.calls[0]) == 2
        assert [pair["product"] for pair in model.calls[1]] == ["螺纹（rb）"]  # the ap pairs are not judged again
        view = svc.flash_feed(product="CZCE.ap")
        assert view["screen"]["pending"] == 0 and view["items"][0]["ai"]["event"] == "库存" and "note" not in view["screen"]
        assert view["screen"]["unscreened"] == 0
        assert await screener.burst() == 0 and model.started == 2 and screener.usage()["pending"] == 0
        with screener.lock:
            assert screener.connection.execute("SELECT COUNT(*) FROM news_screen WHERE product = 'ap'").fetchone()[0] == 2
        # A stale duplicate queued for a judged pair is re-checked against the store before any call.
        screener.want([(view["items"][0], "ap", "苹果")], urgent=True)
        assert await screener.burst() == 0 and model.started == 2 and screener.usage()["pending"] == 0
    asyncio.run(scenario())


def test_a_pair_that_failed_twice_stays_on_keywords_marked_unscreened_and_is_not_retried(tmp_path):
    failing = FakeModel(judge, fail=True)
    svc = build(tmp_path, failing)
    clock = svc.screener.clock
    svc.flash_feed(product="CZCE.ap")
    assert asyncio.run(svc.screener.burst()) == 0 and len(failing.calls) == 1
    clock.now += FAILURE_BACKOFF_SECONDS + 1  # type: ignore[attr-defined]
    assert svc.flash_feed(product="CZCE.ap")["screen"]["pending"] == 2  # requeued after the first failure
    assert asyncio.run(svc.screener.burst()) == 0 and len(failing.calls) == 2
    clock.now += FAILURE_BACKOFF_SECONDS + 1  # type: ignore[attr-defined]
    for _ in range(3):
        view = svc.flash_feed(product="CZCE.ap")
        assert asyncio.run(svc.screener.burst()) == 0
    assert len(failing.calls) == 2 and svc.screener.usage()["pending"] == 0
    screen = view["screen"]
    assert screen["mode"] == "ai" and screen["pending"] == 0  # not "AI 正在筛选"
    assert [item["text"][:6] for item in view["items"]] == ["山东栖霞冷库"] and view["items"][0]["ai"] is None
    assert screen["unscreened"] == 1 and screen["note"] == "1 条 AI 判断失败，按关键词显示，未经 AI 筛选。"
    assert [item["text"][:6] for item in svc.flash_pick("ap")] == ["山东栖霞冷库"]


def test_an_exhausted_budget_makes_no_more_calls_however_often_it_is_read(tmp_path):
    model = FakeModel(judge)
    svc = build(tmp_path, model, daily_calls=1)
    svc.flash_feed(product="CZCE.ap")
    assert asyncio.run(svc.screener.burst()) == 2 and len(model.calls) == 1
    for _ in range(3):
        view = svc.flash_feed(product="SHFE.rb")  # an unjudged pair, over the cap
        assert asyncio.run(svc.screener.burst()) == 0
    assert len(model.calls) == 1 and view["screen"]["mode"] == "keyword" and view["screen"]["pending"] == 0
    assert "今天的 AI 筛选额度已用完" in view["screen"]["note"]
    cached = svc.flash_feed(product="CZCE.ap")  # judged earlier: still shown with its reading
    assert cached["items"][0]["ai"]["event"] == "库存"


def test_one_call_judges_at_most_a_batch_of_pairs(tmp_path):
    model = FakeModel(judge)
    svc = build(tmp_path, model)
    items = [{"id": f"x{index}", "title": "", "text": f"栖霞冷库第{index}批出库", "source": "财联社",
              "publishedAt": "2026-10-07T09:00:00+08:00"} for index in range(BATCH_PAIRS + 6)]
    assert svc.screener.want([(item, "ap", "苹果") for item in items], urgent=True) == BATCH_PAIRS + 6
    assert asyncio.run(svc.screener.burst()) == BATCH_PAIRS + 6
    assert [len(call) for call in model.calls] == [BATCH_PAIRS, 6]


def _pairs(product: str, name: str, count: int) -> list[tuple[dict[str, Any], str, str]]:
    return [({"id": f"{product}{index}", "title": "", "text": f"{name}第{index}条", "source": "隆众资讯",
              "publishedAt": "2026-10-07T09:00:00+08:00"}, product, name) for index in range(count)]


def test_a_flooded_product_does_not_starve_another_one(tmp_path):
    """10-14: 隆众's quotes put hundreds of 原油 pairs in the queue ahead of 黄金's few; 黄金 waited an hour."""
    for urgent in (True, False):
        model = FakeModel(judge)
        svc = build(tmp_path / str(urgent), model)
        screener = svc.screener
        assert screener.want(_pairs("sc", "原油", 500), urgent=True) == 500  # someone is reading 原油
        assert screener.want(_pairs("au", "黄金", 5), urgent=urgent) == 5
        assert asyncio.run(screener.run_round()) == BATCH_PAIRS
        asyncio.run(screener.run_round())
        assert len(screener.verdicts("au", [f"au{index}" for index in range(5)])) == 5  # within two rounds
        assert screener.usage()["pending"] == 500 + 5 - 2 * BATCH_PAIRS
        # The product being read still gets at least half of a batch.
        assert sum(1 for pair in model.calls[0] if pair["product"] == "原油（sc）") >= BATCH_PAIRS // 2


def test_a_product_read_through_the_other_slot_goes_first_on_the_lease_holder(tmp_path):
    model = FakeModel(judge)
    svc = build(tmp_path, model)
    holder = svc.screener
    other = NewsScreener(connection=svc.connection, lock=svc.lock, model="", now=lambda: NOW)  # the standby slot
    other.touch("au", "黄金")
    assert holder.looked() == {"au"}
    holder.want(_pairs("sc", "原油", 100), urgent=False)
    holder.want(_pairs("au", "黄金", 30), urgent=False)
    asyncio.run(holder.run_round())
    assert [pair["product"] for pair in model.calls[0]].count("黄金（au）") >= BATCH_PAIRS // 2
    # The holder's poller queues the read product's new items and starts a round soon, as the read would have.
    other.touch("ap", "苹果")
    holder._urgent = False
    svc.poll_flash_once()
    assert holder._urgent and any(product == "ap" for _, product in holder._pending)


def test_products_take_turns_and_a_full_queue_trims_the_busiest_product(tmp_path):
    model = FakeModel(judge)
    svc = build(tmp_path, model)
    screener = svc.screener
    screener.want(_pairs("sc", "原油", MAX_PENDING), urgent=False)
    screener.want(_pairs("au", "黄金", 3) + _pairs("rb", "螺纹", 3), urgent=False)
    assert screener.usage()["pending"] == MAX_PENDING  # bounded: 原油 gave up its oldest six, not 黄金/螺纹's
    assert screener.state("sc5", "sc") is None and screener.state("sc6", "sc") == "queued"
    assert screener.state("au0", "au") == "queued"
    asyncio.run(screener.run_round())
    first = [pair["product"] for pair in model.calls[0]]
    assert first.count("黄金（au）") == 3 and first.count("螺纹（rb）") == 3 and model.calls[0][0]["text"] == "原油第6条"
    asyncio.run(screener.run_round())  # 黄金 and 螺纹 are empty: 原油 takes the whole next batch
    assert [pair["product"] for pair in model.calls[1]] == ["原油（sc）"] * BATCH_PAIRS


def test_a_contract_or_continuous_code_reads_as_its_product(tmp_path):
    assert {product_key(code) for code in ("SHFE.au2612", "au2612", "KQ.m@SHFE.au", "SHFE.au", "au", "AU")} == {"au"}
    assert product_key("DCE.m2601") == "m" and product_key(None) is None
    svc = build(tmp_path, FakeModel(judge))
    svc.flash_feed(product="SHFE.rb2701")
    svc.flash_feed(product="KQ.m@CZCE.AP")
    assert sorted(product for product, _ in svc.screener.active()) == ["ap", "rb"]  # never "rb2701"


OILCHEM_TITLES = ["[甲醇]：内蒙古久泰甲醇价格快讯", "[硫磺]：中石化安庆石化硫磺暂不报价", "[液化气]：欧华能源液化气价格上调",
                  "[甲苯]：中石化华南甲苯价格快讯", "[甲醇]：甲醇市场早间提示（20261007）",
                  "[丙烯]：东华能源丙烯装置动态", "[沥青]：10月7日10#建筑沥青价格汇总",
                  "[PTA]：10月6日亚洲PTA市场价格快讯", "[乙二醇]：10月7日（09：30）张家港市场乙二醇价格行情",
                  "[成品油]：国内汽柴油价格上调"]


def oilchem(base: str, route: str) -> Any:
    if route == "/oilchem/chem":
        return {"items": [{"id": f"https://www.oilchem.net/{index}.html", "url": f"https://www.oilchem.net/{index}.html",
                           "title": title, "content_html": "<table>免费开通</table>",
                           "date_published": "2026-10-07T01:40:00.000Z"} for index, title in enumerate(OILCHEM_TITLES)]}
    raise OSError("route down")


def test_oilchem_single_plant_quotes_never_enter_the_feed(tmp_path):
    """10-14: most of 隆众's 726 items that day were one plant's price quote; its market notes and whole-market
    quotes stay."""
    svc = build(tmp_path, None, rsshub="http://rsshub:1200", rsshub_fetch=oilchem)
    kept = sorted(item["text"] for item in svc.flash_feed()["items"] if item["source"] == "隆众资讯")
    assert kept == sorted(OILCHEM_TITLES[4:])
    # A quote stored before the rule existed leaves with the next prune; one another source also ran stays.
    with svc.lock, svc.connection:
        for item_id, sources in (("old", ["隆众资讯"]), ("shared", ["隆众资讯", "金十"])):
            svc.connection.execute(
                "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json,"
                " tags_json) VALUES (?, ?, ?, ?, '', ?, NULL, '[]', '[]')",
                (item_id, "隆众资讯", json.dumps(sources, ensure_ascii=False), "2026-10-07T09:30:00+08:00",
                 f"[基础油]：{item_id}基础油价格快讯"))
    svc._prune()
    with svc.lock:
        left = {row[0] for row in svc.connection.execute("SELECT id FROM news_flash WHERE id IN ('old', 'shared')")}
    assert left == {"shared"}
