"""第六批新闻改版: stricter screening (transmission, eventLabel reuse, old cache rows), one event per
story (merged reports, strongest reading, 分歧, research-day boundary), the three tiers and the
hidden pending items, the news analyst's 今日新闻要点 (when it rewrites, the caps, dropped points,
failure, restart) and the per-product calendar. Fake models and fixed clocks; no network, no sleeping."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from trader_runtime import research_live as live
from trader_runtime.news_analyst import MAX_INPUT_EVENTS, MIN_GAP_SECONDS, BriefDraft, NewsAnalyst
from trader_runtime.news_screen import (
    NO_PATH_GROUP,
    NewsScreener,
    ScreenBatch,
    ScreenVerdict,
    build_events,
    layer_summary,
    normalise_verdict,
    screen_summary,
)
from trader_runtime.research_live import ResearchLiveService, calendar_related

NOW = datetime(2026, 10, 7, 10, 40, tzinfo=UTC)  # 18:40 Beijing


def item(item_id: str, published: str, *, label: str | None = None, direction: str = "偏空", strength: str = "中",
         sources: list[str] | None = None, product: str = "au", ai: bool = True, **extra: Any) -> dict[str, Any]:
    reading = {"event": "价格", "direction": direction, "strength": strength, "reason": "r", "transmission": "t",
               "eventLabel": label, "product": product} if ai else None
    return {"id": item_id, "source": (sources or ["新浪"])[0], "sources": sources or ["新浪"], "publishedAt": published,
            "title": f"标题{item_id}", "text": f"正文{item_id}", "link": f"https://example.com/{item_id}", "ai": reading,
            "direct": True, **extra}


# -- screening


def test_a_relevant_verdict_without_a_transmission_is_dropped():
    verdict = ScreenVerdict(key="1", relevant=True, event="宏观", direction="偏空", strength="中", reason="官员讲话",
                            eventLabel="欧央行官员表态")
    assert normalise_verdict(verdict) == {"relevant": False, "dropReason": NO_PATH_GROUP}
    kept = normalise_verdict(verdict.model_copy(update={"transmission": "  经美元影响金价  "}))
    assert kept["relevant"] and kept["transmission"] == "经美元影响金价" and kept["eventLabel"] == "欧央行官员表态"


class Rows(list):
    def to_dict(self, orient: str = "records") -> list[dict]:
        return list(self)


GOLD = Rows([
    {"标题": "", "内容": "财联社10月7日电，现货黄金日内跌1%，现报4121.22美元/盎司。",
     "发布日期": datetime(2026, 10, 7).date(), "发布时间": datetime(2026, 10, 7, 17, 6).time()},
    {"标题": "", "内容": "财联社10月7日电，现货黄金失守4110美元/盎司，日内跌1.31%。",
     "发布日期": datetime(2026, 10, 7).date(), "发布时间": datetime(2026, 10, 7, 18, 32).time()},
    {"标题": "", "内容": "财联社10月7日电，美国30年期国债收益率升至5.70%，创2002年以来新高，美联储降息预期回落。",
     "发布日期": datetime(2026, 10, 7).date(), "发布时间": datetime(2026, 10, 7, 18, 11).time()},
    {"标题": "", "内容": "财联社10月7日电，欧洲央行管委称黄金储备政策不变。",
     "发布日期": datetime(2026, 10, 7).date(), "发布时间": datetime(2026, 10, 7, 18, 20).time()},
])


class Recorded:
    def stock_info_global_cls(self, symbol="全部"):
        return GOLD

    def news_economic_baidu(self, date):
        if date != "20261007":
            return Rows()
        return Rows([{"日期": "2026-10-07", "时间": clock, "地区": "美国", "事件": event, "重要性": 2,
                      "公布": None, "预期": None, "前值": None}
                     for clock, event in (("20:30", "9月非农就业人数(万人)"), ("22:30", "EIA原油库存(万桶)"))])

    def __getattr__(self, name):
        return lambda *args, **kwargs: Rows()


class LabelModel:
    """Labels each 现货黄金 report the same; records the knownLabels it was offered."""

    agent_authenticated = True
    model = "fake-flash"

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def complete_structured(self, *, instructions, input_text, schema, usage_user=None, **_):
        payload = json.loads(input_text)
        self.payloads.append(payload)
        if schema is BriefDraft:
            raise RuntimeError("model_request_failed")
        verdicts = []
        for pair in payload["pairs"]:
            text = pair["text"]
            if "现货黄金" in text:
                strength = "强" if "4110" in text else "中"
                verdicts.append({"key": pair["key"], "relevant": True, "event": "价格", "direction": "偏空", "strength": strength,
                                 "reason": "外盘下跌", "transmission": "外盘下跌带动沪金", "eventLabel": "现货黄金日内下跌"})
            elif "国债收益率" in text:
                verdicts.append({"key": pair["key"], "relevant": True, "event": "宏观", "direction": "偏空", "strength": "中",
                                 "reason": "利率上行", "transmission": "持金机会成本上升", "eventLabel": "美债收益率创新高"})
            elif "欧洲央行" in text:
                verdicts.append({"key": pair["key"], "relevant": True, "event": "宏观", "direction": "中性", "strength": "弱",
                                 "reason": "表态", "transmission": "", "eventLabel": "欧央行官员表态"})  # no path: dropped
            else:
                verdicts.append({"key": pair["key"], "relevant": False, "dropReason": "无关宏观数据"})
        return type("Result", (), {"value": ScreenBatch.model_validate({"verdicts": verdicts}), "tokens": 500})()


def build(tmp_path, model: Any) -> ResearchLiveService:
    connection = sqlite3.connect(tmp_path / "news.sqlite3", check_same_thread=False)
    lock = threading.Lock()
    sources = live.LiveSources()
    sources._module = Recorded()
    screener = NewsScreener(connection=connection, lock=lock, generator=lambda: model, model="", daily_calls=50,
                            now=lambda: NOW)
    catalog = {"items": [{"symbol": "SHFE.au2612", "isMain": True, "productId": "au", "instrumentName": "沪金2612"}]}
    service = ResearchLiveService(connection=connection, lock=lock, instrument_catalog=lambda: catalog, sources=sources,
                                  now=lambda: NOW, screener=screener)
    service.poll_flash_once()
    return service


def test_the_same_story_from_several_reports_becomes_one_event_and_labels_are_offered_for_reuse(tmp_path):
    model = LabelModel()
    svc = build(tmp_path, model)
    first = svc.flash_feed(product="SHFE.au")
    # Not judged yet: ai mode shows no events, only how many the AI is still reading.
    assert first["screen"]["mode"] == "ai" and first["screen"]["pending"] == 4 and first["events"] == []
    assert first["items"] and all(entry["unjudged"] == "pending" for entry in first["items"])
    assert asyncio.run(svc.screener.burst()) == 4
    view = svc.flash_feed(product="SHFE.au")
    events = {event["eventLabel"]: event for event in view["events"]}
    assert set(events) == {"现货黄金日内下跌", "美债收益率创新高"}
    drop = events["现货黄金日内下跌"]
    assert len(drop["items"]) == 2 and drop["publishedAt"].startswith("2026-10-07T18:32")
    # Titled by the report naming the event best (日内跌 over 失守), not just the newest one.
    assert drop["firstAt"].startswith("2026-10-07T17:06") and drop["title"] == drop["items"][1]["title"]
    assert drop["ai"]["strength"] == "强" and drop["ai"]["transmission"] == "外盘下跌带动沪金" and drop["tier"] == "major"
    assert drop["reports"] == 1 and drop["items"][1]["link"] is None  # one outlet (财联社) filed both
    # The 欧央行 remark had no transmission: dropped under its own group, not shown.
    assert {"reason": NO_PATH_GROUP, "count": 1} in view["screen"]["dropped"]["groups"]
    assert view["layers"]["major"] == 2 and view["layers"]["minor"] == 0

    # A new report of the same story: the next call offers today's labels for reuse.
    svc.screener.want([({"id": "late", "title": "", "text": "现货黄金继续下跌", "source": "新浪",
                         "publishedAt": "2026-10-07T18:39:00+08:00"}, "au", "沪金")], urgent=True)
    asyncio.run(svc.screener.burst())
    offered = model.payloads[-1]["knownLabels"]
    assert set(offered["沪金（au）"]) == {"现货黄金日内下跌", "美债收益率创新高"}


def test_rows_judged_before_labels_existed_still_read_each_as_its_own_event(tmp_path):
    connection = sqlite3.connect(tmp_path / "old.sqlite3", check_same_thread=False)
    connection.execute(
        "CREATE TABLE news_screen (item_id TEXT NOT NULL, product TEXT NOT NULL, relevant INTEGER NOT NULL, event TEXT,"
        " direction TEXT, strength TEXT, reason TEXT, drop_reason TEXT, model TEXT, judged_at TEXT NOT NULL,"
        " PRIMARY KEY (item_id, product))")
    for item_id in ("a", "b"):
        connection.execute("INSERT INTO news_screen VALUES (?, 'au', 1, '价格', '偏空', '中', '旧理由', NULL, 'm', ?)",
                           (item_id, NOW.isoformat()))
    connection.commit()
    screener = NewsScreener(connection=connection, lock=threading.Lock(), now=lambda: NOW)
    verdicts = screener.verdicts("au", ["a", "b"])
    assert verdicts["a"] == {"relevant": True, "event": "价格", "direction": "偏空", "strength": "中", "reason": "旧理由",
                             "transmission": "", "eventLabel": None}
    assert screener.event_labels("au", "2026-10-07") == []
    kept = [item(item_id, "2026-10-07T18:00:00+08:00", label=None) for item_id in ("a", "b")]
    assert [event["id"] for event in build_events(kept, hide_pending=True)] == ["b", "a"]


# -- merging and tiers


def test_merge_takes_the_newest_report_the_strongest_reading_and_marks_disagreement():
    items = [
        item("n3", "2026-10-07T18:32:00+08:00", label="现货黄金日内下跌", strength="中", sources=["新浪", "东方财富"]),
        item("n2", "2026-10-07T17:07:00+08:00", label="现货黄金日内下跌", strength="强", sources=["华尔街见闻"]),
        item("n1", "2026-10-07T17:06:00+08:00", label="现货黄金日内下跌", strength="弱", sources=["新浪"]),
        item("y", "2026-10-07T18:11:00+08:00", label="美债收益率", direction="偏空", sources=["金十"]),
        item("y2", "2026-10-07T18:12:00+08:00", label="美债收益率", direction="偏多", strength="弱", sources=["金十"]),
        # Same name the evening before the 08:30 turn: another research day, another event.
        item("old", "2026-10-07T08:10:00+08:00", label="现货黄金日内下跌", sources=["财联社"]),
    ]
    events = build_events(items, hide_pending=True)
    assert [event["eventLabel"] for event in events] == ["现货黄金日内下跌", "美债收益率", "现货黄金日内下跌"]
    gold = events[0]
    assert gold["publishedAt"] == "2026-10-07T18:32:00+08:00" and gold["title"] == "标题n3"
    assert gold["reports"] == 3 and gold["sources"] == ["新浪", "东方财富", "华尔街见闻"]
    assert [entry["link"] for entry in gold["items"]] == [f"https://example.com/{key}" for key in ("n3", "n2", "n1")]
    assert gold["ai"]["strength"] == "强" and gold["ai"]["direction"] == "偏空" and gold["researchDay"] == "2026-10-07"
    assert events[1]["ai"]["direction"] == "分歧" and events[1]["ai"]["strength"] == "中"
    assert events[2]["researchDay"] == "2026-10-06" and events[2]["id"] != gold["id"]


def test_events_carry_the_original_title_of_translated_reports():
    """品种视图/持仓与自选/手机版走 events：翻译过的条目要把 origTitle 带进事件（标题行挑中的
    那份）和每份报道里，客户端才能在这些主视图折叠看英文原文。"""
    items = [
        item("n3", "2026-10-07T18:32:00+08:00", label="现货黄金日内下跌", sources=["FinancialJuice"],
             title="金价日内下跌", origTitle="Gold slips as dollar firms", text="Gold slips as dollar firms"),
        item("n2", "2026-10-07T17:07:00+08:00", label="现货黄金日内下跌", sources=["财联社"]),
    ]
    events = build_events(items, hide_pending=True)
    gold = events[0]
    assert gold["origTitle"] == "Gold slips as dollar firms"  # 事件标题行挑中的是 n3
    assert gold["tags"] == []  # 成员的标签并集：都还不是交易所通知
    by_id = {entry["id"]: entry for entry in gold["items"]}
    assert by_id["n3"]["origTitle"] == "Gold slips as dollar firms"
    assert by_id["n2"]["origTitle"] is None  # 中文报道没有原文
    # 一份上期所通知（tags 带 交易所）进同一事件：事件的 tags 并上它，筛过的视图也认得出。
    notice = item("n1", "2026-10-07T16:00:00+08:00", label="现货黄金日内下跌", sources=["上期所"], tags=["交易所"])
    gold = next(event for event in build_events([*items, notice], hide_pending=True) if event["eventLabel"] == "现货黄金日内下跌")
    assert gold["tags"] == ["交易所"]
    assert {entry["id"]: entry for entry in gold["items"]}["n1"]["tags"] == ["交易所"]


def test_the_brief_reads_the_last_24_hours_across_the_0830_research_day_turn(tmp_path):
    """10-08: rb's pre-open 重要 news (mills' price rises, Fed minutes) fell on the previous research
    day, so the analyst saw no new event and wrote no brief all morning while the 快讯 page showed them."""
    svc = build(tmp_path, LabelModel())
    svc.flash_feed(product="SHFE.au")
    asyncio.run(svc.screener.burst())
    svc.now = lambda: NOW + timedelta(hours=15)  # 10-08 09:40 Beijing, the next research day
    _, events = svc.product_events("SHFE.au")
    assert {event["eventLabel"] for event in events if event["tier"] == "major"} == {"现货黄金日内下跌", "美债收益率创新高"}


def test_a_truncated_title_never_merges_two_different_stories():
    """#618: 新浪 rows have no title; the shown title is the body's first 18 chars + …. Two different
    stories that share that lead (the 劳工部 data intro) must stay two events with their own readings,
    not one 分歧 event."""
    cpi = "美国劳工部公布的最新数据显示，美国9月CPI同比上涨3%，高于预期。"
    jobs = "美国劳工部公布的最新数据显示，美国9月非农就业新增8万人，低于预期。"
    assert cpi[:18] == jobs[:18] and len(cpi) > 18
    items = [
        item("jobs", "2026-10-07T21:00:00+08:00", label="美国非农不及预期", direction="偏多", title=jobs[:18] + "…", text=jobs),
        item("cpi", "2026-10-07T20:00:00+08:00", label="美国通胀超预期", direction="偏空", title=cpi[:18] + "…", text=cpi),
    ]
    events = build_events(items, hide_pending=True)
    assert len(events) == 2
    by_label = {event["eventLabel"]: event for event in events}
    assert by_label["美国通胀超预期"]["ai"]["direction"] == "偏空"
    assert by_label["美国非农不及预期"]["ai"]["direction"] == "偏多"


def test_a_truncated_title_cannot_carry_yesterdays_story_into_today():
    """#618 的跨日反例: 昨晚的旧新闻与今天的新闻共用同一个截断标题时，旧事件不得并进今天的事件
    （24 小时窗口内 product_events 读到的、也就是新闻要点的输入），今天的事件保持自己的方向。"""
    old_cpi = "美国劳工部公布的最新数据显示，美国9月CPI同比上涨3%，高于预期。"
    today = "美国劳工部公布的最新数据显示，美国9月非农就业新增8万人，低于预期。"
    items = [
        item("today", "2026-10-07T20:00:00+08:00", label="美国非农不及预期", direction="偏多", title=today[:18] + "…", text=today),
        item("old", "2026-10-06T22:00:00+08:00", label="美国通胀超预期", direction="偏空", title=old_cpi[:18] + "…", text=old_cpi),
    ]
    events = build_events(items, hide_pending=True)
    assert [(event["researchDay"], event["eventLabel"], event["ai"]["direction"]) for event in events] \
        == [("2026-10-07", "美国非农不及预期", "偏多"), ("2026-10-06", "美国通胀超预期", "偏空")]


def test_a_full_same_title_still_merges_across_the_0830_turn():
    """#618 的正控: 同花顺 08:10 与富途 08:40 的两条完整同标题黄金报道（正文措辞不同）跨研究日仍是一条。"""
    title = "黄金创历史新高，央行购金构成支撑"
    items = [
        item("futu", "2026-10-07T08:40:00+08:00", label="黄金创新高", title=title,
             text="富途快讯：现货黄金今日创出历史新高，多位分析师认为各国央行持续购金构成支撑。", sources=["富途"]),
        item("ths", "2026-10-07T08:10:00+08:00", label="黄金创新高", title=title,
             text="同花顺报道：黄金价格创下纪录高位。", sources=["同花顺"]),
    ]
    events = build_events(items, hide_pending=True)
    assert len(events) == 1
    assert events[0]["reports"] == 2 and events[0]["sources"] == ["富途", "同花顺"]
    assert events[0]["researchDay"] == "2026-10-07" and len(events[0]["items"]) == 2


def test_a_merged_event_is_titled_by_its_most_informative_report():
    """保护：合并后的标题不被最新那条的短标题顶掉（10-07 截图里只剩“美债收益率”）。"""
    items = [
        item("short", "2026-10-07T18:55:00+08:00", label="美债收益率创新高", title="美债收益率"),
        item("cut", "2026-10-07T18:40:00+08:00", label="美债收益率创新高", title="美国30年期国债收益率升至5.704…"),
        item("full", "2026-10-07T18:11:00+08:00", label="美债收益率创新高", title="30年期美国国债收益率攀升至2002年以来最高水平"),
    ]
    event = build_events(items, hide_pending=True)[0]
    assert event["title"] == "30年期美国国债收益率攀升至2002年以来最高水平"
    assert event["publishedAt"] == "2026-10-07T18:55:00+08:00" and event["reports"] == 1


def test_a_round_up_post_never_titles_an_event_and_one_headline_under_two_names_is_one_event():
    """保护：综述帖不当事件标题；同一标题被起了两个名字也合成一条（10-07 线上重判后）。"""
    items = [
        item("roundup", "2026-10-07T20:40:00+08:00", label="现货黄金日内下跌",
             title="美股盘前要闻一览：AMD称对HBM4“非常满意”；谷歌与英伟达合作扩大，现货黄金跌超2%"),
        item("gold", "2026-10-07T20:30:00+08:00", label="现货黄金日内下跌", title="现货黄金失守4080美元/盎司，日内跌2.01%。"),
        item("house1", "2026-10-07T19:00:00+08:00", label="国庆新房认购量上涨", title="机构：国庆假期全国重点50城新房认购量同比上涨超15%"),
        item("house2", "2026-10-07T18:00:00+08:00", label="假期楼市回暖", title="机构：国庆假期全国重点50城新房认购量同比上涨超15%",
             sources=["财联社"]),
    ]
    events = build_events(items, hide_pending=True)
    assert len(events) == 2
    gold = next(event for event in events if event["eventLabel"] == "现货黄金日内下跌")
    assert gold["title"] == "现货黄金失守4080美元/盎司，日内跌2.01%。" and gold["publishedAt"] == "2026-10-07T20:40:00+08:00"
    house = next(event for event in events if event["eventLabel"] == "国庆新房认购量上涨")
    assert [entry["id"] for entry in house["items"]] == ["house1", "house2"] and house["reports"] == 2
    # The same report before and after the 08:30 turn is still one story.
    early = item("house0", "2026-10-07T08:10:00+08:00", label="国庆新房认购量上涨",
                 title="机构：国庆假期全国重点50城新房认购量同比上涨超15%", sources=["同花顺"])
    merged = [event for event in build_events([*items, early], hide_pending=True) if "新房" in event["title"]]
    assert len(merged) == 1 and merged[0]["reports"] == 3


def test_tiers_and_the_folded_minor_count_by_event_name():
    items = [
        item("m", "2026-10-07T18:30:00+08:00", label="美债收益率", strength="中"),
        item("o", "2026-10-07T18:29:00+08:00", label="加息概率下降", direction="偏多", strength="弱"),
        *[item(f"e{index}", f"2026-10-07T18:{10 + index}:00+08:00", label="欧央行官员表态", direction="中性", strength="弱")
          for index in range(4)],
        *[item(f"f{index}", f"2026-10-07T17:{10 + index}:00+08:00", label="美联储纪要前瞻", direction="中性", strength="弱")
          for index in range(3)],
        item("x", "2026-10-07T16:00:00+08:00", label="黄金ETF持仓", direction="中性", strength="弱"),
        item("z", "2026-10-07T15:00:00+08:00", label=None, direction="中性", strength="弱"),
        item("k", "2026-10-07T14:00:00+08:00", ai=False, unjudged="failed"),
        item("p", "2026-10-07T14:30:00+08:00", ai=False, unjudged="pending"),
    ]
    events = build_events(items, hide_pending=True)
    tiers = {event["eventLabel"] or event["id"]: event["tier"] for event in events}
    assert tiers == {"美债收益率": "major", "加息概率下降": "other", "欧央行官员表态": "minor", "美联储纪要前瞻": "minor",
                     "黄金ETF持仓": "minor", "z": "minor", "k": "unscreened"}  # the pending one is not shown
    layers = layer_summary(events)
    assert layers["major"] == 1 and layers["other"] == 1 and layers["minor"] == 4 and layers["unscreened"] == 1
    assert layers["minorReports"] == 9
    assert layers["minorGroups"] == [{"label": "欧央行官员表态", "count": 4}, {"label": "美联储纪要前瞻", "count": 3},
                                     {"label": "黄金ETF持仓", "count": 1}, {"label": "其他", "count": 1}]
    # Keyword mode (no model): nothing is held back.
    assert "p" in {event["id"] for event in build_events(items, hide_pending=False)}


# -- the news analyst


def major(label: str, *, reports: int = 2, direction: str = "偏空", tier: str = "major") -> dict[str, Any]:
    return {"id": f"id-{label}", "eventLabel": label, "title": f"{label}，报 4121.22 美元", "text": "", "reports": reports,
            "publishedAt": "2026-10-07T18:30:00+08:00", "researchDay": "2026-10-07", "tier": tier,
            "ai": {"direction": direction, "strength": "中" if tier == "major" else "弱", "transmission": "外盘下跌带动沪金"}}


class BriefModel:
    agent_authenticated = True
    model = "fake-pro"

    def __init__(self, draft: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.draft, self.fail, self.calls = draft, fail, []

    async def complete_structured(self, *, instructions, input_text, schema, usage_user=None, **_):
        assert schema is BriefDraft and usage_user == "platform:news-analyst"
        self.calls.append(json.loads(input_text))
        if self.fail:
            raise RuntimeError("model_request_failed")
        return type("Result", (), {"value": BriefDraft.model_validate(self.draft), "tokens": 2400})()


GOOD = {"net": "偏空", "take": "现货黄金跌到 4121.22 美元，美债收益率走高，新闻面偏空。",
        "points": [{"text": "现货黄金报 4121.22 美元，沪金跟跌", "direction": "偏空", "eventLabel": "现货黄金日内下跌"},
                   {"text": "央行购金跌了 77%", "direction": "偏多", "eventLabel": "现货黄金日内下跌"},  # 77 not in the input
                   {"text": "某个不存在的事件", "direction": "偏多", "eventLabel": "编出来的事件"}],
        "watch": ["周四 02:00 美联储会议纪要"]}


class Moment:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


def analyst(tmp_path, model: Any, events: list[dict[str, Any]], moment: Moment, *, daily: int = 16,
            name: str = "brief.sqlite3") -> NewsAnalyst:
    connection = sqlite3.connect(tmp_path / name, check_same_thread=False)
    return NewsAnalyst(connection=connection, lock=threading.Lock(), generator=lambda: model, model="",
                       events_for=lambda product: ("沪金", events), active=lambda: [("au", "沪金")],
                       calendar_for=lambda product: [{"date": "2026-10-09", "time": "02:00", "event": "美联储会议纪要"}],
                       price_for=lambda product: "主力 au2612 最新价 960.5，日涨跌 -1.3%", daily_per_product=daily, now=moment)


def test_brief_reads_bounded_original_and_keeps_summary_on_fetch_failure(tmp_path):
    from trader_runtime.web_search import WebSearchError

    for failed in (False, True):
        class Reader:
            async def read_url(self, url, *, max_chars):
                assert url == "https://example.com/original" and max_chars == 2000
                if failed:
                    raise WebSearchError("web_search_unavailable")
                return {"text": "原文：现货黄金日内下跌，报 4121.22 美元。"}

        event = major("现货黄金日内下跌")
        event["text"] = "标题摘要保留"
        event["items"] = [{"link": "https://example.com/original"}]
        model = BriefModel(GOOD)
        writer = analyst(tmp_path, model, [event], Moment(), name=f"fetch-{failed}.sqlite3")
        writer.search = Reader()
        assert asyncio.run(writer.run_once()) == 1
        sent = model.calls[0]["events"][0]
        assert sent["summary"] == "标题摘要保留"
        assert ("article" in sent) is not failed


def test_the_brief_is_written_for_new_important_events_and_unanchored_points_are_dropped(tmp_path):
    moment = Moment()
    events = [major("现货黄金日内下跌"), major("欧央行官员表态", tier="minor", direction="中性")]
    model = BriefModel(GOOD)
    writer = analyst(tmp_path, model, events, moment)
    assert writer.brief("au") == {"status": "none"}
    assert asyncio.run(writer.run_once()) == 1
    sent = model.calls[0]
    assert sent["price"].endswith("日涨跌 -1.3%") and sent["calendar"] == [{"time": "10-09 02:00", "event": "美联储会议纪要"}]
    assert {event["eventLabel"] for event in sent["events"]} == {"现货黄金日内下跌", "欧央行官员表态"}
    brief = writer.brief("au")
    assert brief["status"] == "ready" and brief["net"] == "偏空" and brief["basedOn"] == 2
    assert brief["points"] == [{"text": "现货黄金报 4121.22 美元，沪金跟跌", "direction": "偏空", "eventLabel": "现货黄金日内下跌",
                                "eventId": "id-现货黄金日内下跌", "reports": 2}]
    assert brief["watch"] == ["周四 02:00 美联储会议纪要"] and brief["model"] == "fake-pro"

    # No new 重要 event: no rewrite however often it checks.
    moment.value += timedelta(hours=2)
    assert asyncio.run(writer.run_once()) == 0 and len(model.calls) == 1
    # A new one inside 30 minutes of the last attempt waits; after the gap it rewrites.
    events.append(major("美债收益率创新高"))
    writer.usage("au")
    moment.value += timedelta(seconds=1)
    first_at = datetime.fromisoformat(writer.brief("au")["generatedAt"])
    assert (moment.value - first_at).total_seconds() > MIN_GAP_SECONDS
    assert asyncio.run(writer.run_once()) == 1 and len(model.calls) == 2
    events.append(major("多国央行增持黄金", direction="偏多"))
    moment.value += timedelta(minutes=29)
    assert asyncio.run(writer.run_once()) == 0 and len(model.calls) == 2
    moment.value += timedelta(minutes=2)
    assert asyncio.run(writer.run_once()) == 1 and len(model.calls) == 3

    # Restart: the brief, the call count and the covered events come back from the store.
    again = analyst(tmp_path, model, events, moment)
    assert again.brief("au")["generatedAt"] == writer.brief("au")["generatedAt"] and again.usage("au")["calls"] == 3
    moment.value += timedelta(hours=1)
    assert asyncio.run(again.run_once()) == 0 and len(model.calls) == 3


def test_reports_judged_before_labels_still_reach_the_brief_by_their_title(tmp_path):
    """保护：上线前判过、没有事件名的快讯也交给新闻分析师（10-07 线上只读到 1 条，写成“仅有一条”）。"""
    legacy = {**major("x"), "id": "legacy-1", "eventLabel": None, "title": "现货黄金跌近1%，报4121.22美元/盎司"}
    events = [legacy, major("英国国债收益率创新高")]
    draft = {"net": "偏空", "take": "现货黄金跌到 4121.22 美元，新闻面偏空。",
             "points": [{"text": "现货黄金报 4121.22 美元", "direction": "偏空", "eventLabel": "现货黄金跌近1%，报4121.2"}],
             "watch": []}
    model = BriefModel(draft)
    writer = analyst(tmp_path, model, events, Moment())
    assert asyncio.run(writer.run_once()) == 1
    assert {event["eventLabel"] for event in model.calls[0]["events"]} == {"现货黄金跌近1%，报4121.2", "英国国债收益率创新高"}
    brief = writer.brief("au")
    assert brief["basedOn"] == 2 and brief["points"][0]["eventId"] == "legacy-1"


def test_an_important_event_beyond_the_input_cap_is_not_covered_and_is_read_next_time(tmp_path):
    """保护：要点最多读 30 个事件；没读到的重要事件不算已覆盖，下一轮优先读（Codex #516 10-07）。"""
    moment = Moment()
    events = [{**major(f"事件{index:02d}"), "publishedAt": f"2026-10-07T17:{index:02d}:00+08:00"} for index in range(MAX_INPUT_EVENTS + 1)]
    draft = {"net": "偏空", "take": "新闻面偏空。", "points": [{"text": "事件偏空", "direction": "偏空", "eventLabel": "事件30"}],
             "watch": []}
    model = BriefModel(draft)
    writer = analyst(tmp_path, model, events, moment)
    assert asyncio.run(writer.run_once()) == 1
    first = {event["eventLabel"] for event in model.calls[0]["events"]}
    assert len(first) == MAX_INPUT_EVENTS and "事件00" not in first  # the oldest waits
    assert "id-事件00" not in writer._entry("au", "2026-10-07")["covered"]
    moment.value += timedelta(minutes=31)
    assert writer.due("au", events)  # the unread one still calls for a rewrite
    assert asyncio.run(writer.run_once()) == 1
    assert "事件00" in {event["eventLabel"] for event in model.calls[1]["events"]}
    assert "id-事件00" in writer._entry("au", "2026-10-07")["covered"]
    moment.value += timedelta(minutes=31)
    assert not writer.due("au", events)


def test_the_unscreened_note_counts_shown_news_once():
    """保护：“N 条 AI 判断失败”只数页面上真的显示、没有解读的新闻，每条一次（Codex #516 P3）。"""
    failed = lambda key: {"id": key, "ai": None, "unjudged": "failed", "publishedAt": "2026-10-07T18:00:00+08:00"}  # noqa: E731
    judged = item("a", "2026-10-07T18:00:00+08:00", label="x")
    view = {"mode": "ai", "pending": 0, "unscreened": 9, "kept": [judged, failed("b"), failed("b"), failed("c")], "reason": None}
    assert screen_summary(view, [])["unscreened"] == 2 and screen_summary(view, [])["note"].startswith("2 条")
    # Only what the page shows: the limit left "c" out; one judged for another product shows its reading.
    assert screen_summary(view, [], [judged, failed("b")])["unscreened"] == 1
    assert "note" not in screen_summary(view, [], [judged])


def test_the_daily_cap_and_a_failed_run_keep_the_previous_brief(tmp_path):
    moment = Moment()
    events = [major("现货黄金日内下跌")]
    model = BriefModel(GOOD)
    writer = analyst(tmp_path, model, events, moment, daily=2)
    assert asyncio.run(writer.run_once()) == 1
    written = writer.brief("au")

    model.fail = True
    events.append(major("美债收益率创新高"))
    moment.value += timedelta(minutes=31)
    assert asyncio.run(writer.run_once()) == 0 and len(model.calls) == 2
    stale = writer.brief("au")
    assert stale["status"] == "stale" and stale["take"] == written["take"] and stale["generatedAt"] == written["generatedAt"]
    assert stale["failedAt"] > written["generatedAt"]

    # Two calls today: the cap holds even with new events and a working model.
    model.fail = False
    events.append(major("多国央行增持黄金", direction="偏多"))
    moment.value += timedelta(hours=1)
    assert asyncio.run(writer.run_once()) == 0 and len(model.calls) == 2

    # An answer whose every point is unanchored is a failed run too.
    unanchored = BriefModel({**GOOD, "points": [GOOD["points"][2]]})
    other = analyst(tmp_path, unanchored, [major("现货黄金日内下跌")], Moment(), name="other.sqlite3")
    assert asyncio.run(other.run_once()) == 0 and other.brief("au") == {"status": "none"}


def test_no_brief_without_an_important_event_or_without_a_model(tmp_path):
    moment = Moment()
    quiet = analyst(tmp_path, BriefModel(GOOD), [major("欧央行官员表态", tier="minor", direction="中性")], moment)
    assert asyncio.run(quiet.run_once()) == 0
    unwired = analyst(tmp_path, None, [major("现货黄金日内下跌")], moment, name="none.sqlite3")
    assert asyncio.run(unwired.run_once()) == 0 and unwired.brief("au") == {"status": "none"}


# -- the calendar by product


def test_the_week_calendar_keeps_the_events_related_to_the_product():
    eia = {"region": "美国", "event": "EIA原油库存(万桶)"}
    payrolls = {"region": "美国", "event": "9月非农就业人数(万人)"}
    minutes = {"region": "美国", "event": "美联储公布会议纪要"}
    pmi = {"region": "中国", "event": "9月官方制造业PMI"}
    assert not calendar_related(eia, "au") and calendar_related(payrolls, "au") and calendar_related(minutes, "au")
    assert calendar_related(eia, "sc") and not calendar_related(payrolls, "sc")
    assert calendar_related(pmi, "rb") and calendar_related(pmi, "if") and not calendar_related(pmi, "au")
    assert not calendar_related(payrolls, "ec")  # no category: nothing listed for it


def test_the_feed_filters_the_calendar_and_lists_tonights_related_data(tmp_path):
    svc = build(tmp_path, LabelModel())
    svc.refresh_calendar()
    gold = svc.calendar_feed(product="SHFE.au")
    assert [event["event"] for event in gold["items"]] == ["9月非农就业人数(万人)"] and gold["otherCount"] == 1
    assert [event["event"] for event in svc.calendar_feed(product="sc")["items"]] == ["EIA原油库存(万桶)"]
    assert len(svc.calendar_feed()["items"]) == 2  # 宏观 / 全部 keep everything
    feed = svc.flash_feed(product="SHFE.au")
    assert [event["event"] for event in feed["tonight"]] == ["9月非农就业人数(万人)"]
    assert feed["brief"] if svc.analyst else "brief" not in feed


def test_an_event_id_resolves_to_its_own_product_without_any_view_being_open(tmp_path):
    """多 AI 研讨: the Runtime decides which product an event is about (the click, a UI command, voice and
    agents all send only the event id); an id that is not a screened event of the window resolves to nothing."""
    svc = build(tmp_path, LabelModel())
    svc.flash_feed(product="SHFE.au")
    asyncio.run(svc.screener.burst())
    _, events = svc.product_events("SHFE.au")
    gold = next(event for event in events if event["eventLabel"] == "现货黄金日内下跌")
    assert svc.event_products(gold["id"]) == ["au"]
    assert svc.event_products("e000000000000") == []
    svc.now = lambda: NOW + timedelta(hours=30)  # out of the 24-hour window
    assert svc.event_products(gold["id"]) == []
