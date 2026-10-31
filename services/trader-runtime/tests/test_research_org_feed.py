"""机构观点栏目（2026-10-13）：东方财富期货频道“期货机构观点”(516) 与“国内期货”(513) 一次读全部品种，调研台
按品种挑出、与按品种搜到的期货公司笔记合在一起（每家一条），进 机构观点 区，供研讨引用。录制样本，不出网。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from test_research_desk import _collected, _desk, _Sources
from trader_runtime.research_sources import (
    CHINA,
    PublicSources,
    SourceError,
    feed_notes,
    merge_notes,
    parse_org_feed,
    product_aliases,
)

FIXTURES = Path(__file__).parent / "fixtures" / "news"
FEED_NOW = datetime(2026, 10, 13, 18, 0, tzinfo=CHINA)


def _column(number: int) -> str:
    return (FIXTURES / f"eastmoney-{number}.json").read_text(encoding="utf-8")


def test_the_columns_keep_futures_institutions_and_their_fields_only():
    notes = parse_org_feed(_column(516), 516, FEED_NOW)
    media = [note["media"] for note in notes]
    # 期货公司与期货日报、东方财富研究中心的期市收评留下；它的“图解期货”科普不算机构观点。
    assert media == ["期货日报", "期货日报", "一德期货", "创元期货", "南华期货", "东方财富研究中心"]
    methanol = notes[2]
    assert methanol == {"title": "能化 | 甲醇：伊朗多套装置胀库停车 多套沿海MTO装置维持停车",
                        "summary": "能化 | 甲醇：伊朗多套装置胀库停车 多套沿海MTO装置维持停车", "media": "一德期货",
                        "link": "https://futures.eastmoney.com/a/202610133893421022.html",
                        "publishDate": "2026-10-13T09:04:21+08:00", "source": "eastmoney"}
    # 513 只取期货日报的市场分析。
    assert {note["media"] for note in parse_org_feed(_column(513), 513, FEED_NOW)} == {"期货日报"}
    # 超过 3 天的不要；接口改版答不出列表是来源错误，不是“没有观点”。
    assert parse_org_feed(_column(516), 516, datetime(2026, 10, 20, tzinfo=CHINA)) == []
    with pytest.raises(SourceError):
        parse_org_feed("<html>改版</html>", 516, FEED_NOW)


def test_a_product_picks_its_notes_out_of_the_shared_feed():
    feed = parse_org_feed(_column(516), 516, FEED_NOW) + parse_org_feed(_column(513), 513, FEED_NOW)
    methanol = feed_notes(feed, product_aliases("甲醇"), FEED_NOW)
    assert [(note["media"], note["summary"]) for note in methanol] == [
        ("一德期货", "甲醇：伊朗多套装置胀库停车 多套沿海MTO装置维持停车")]  # 标题里点名：观点是板块之后那段
    # 只在摘要里提到：从提到处取一段（创元期货的有色笔记标题是沪铝，摘要讲碳酸锂）。
    lithium = feed_notes(feed, product_aliases("碳酸锂"), FEED_NOW)
    assert lithium[0]["media"] == "创元期货" and lithium[0]["summary"].startswith("碳酸锂：主力2701合约收盘价104740元/吨")
    silver = feed_notes(feed, product_aliases("沪银"), FEED_NOW)
    assert [note["media"] for note in silver] == ["期货日报"]  # 一家机构一条（最新的）
    assert feed_notes(feed, product_aliases("苹果"), FEED_NOW) == []


def test_merging_keeps_one_note_per_institution_and_link():
    a = {"title": "t", "summary": "s", "media": "南华期货", "link": "https://x/1", "publishDate": "2026-10-13T08:00:00+08:00"}
    b = {**a, "link": "https://x/2", "publishDate": "2026-10-13T09:00:00+08:00"}
    c = {**a, "media": "一德期货", "link": "https://x/2", "publishDate": "2026-10-13T07:00:00+08:00"}
    d = {**a, "media": "光大期货", "link": "https://x/3", "publishDate": "2026-10-12T07:00:00+08:00"}
    assert merge_notes([a, d], [b, c]) == [b, d]  # 南华取更新的一条；与已选同链接的那条不重复


async def test_the_org_feed_reads_both_columns_and_survives_one_failing():
    def answer(request: httpx.Request) -> httpx.Response:
        column = request.url.params["column"]
        if column == "513":
            return httpx.Response(502)
        return httpx.Response(200, text=_column(516))

    notes = await PublicSources(transport=httpx.MockTransport(answer)).org_feed(FEED_NOW)
    assert len(notes) == 6 and notes[0]["media"] == "期货日报"
    with pytest.raises(SourceError):
        await PublicSources(transport=httpx.MockTransport(lambda request: httpx.Response(502))).org_feed(FEED_NOW)


class _ColumnSources(_Sources):
    """The note search answers as before; the shared column feed adds two more institutions on 螺纹钢."""

    async def org_feed(self, now: Any) -> list[dict[str, Any]]:
        self.calls.append("orgfeed")
        return [{"title": "黑色系 | 螺纹钢：产量、需求、库存均处于较低水平 供需层面驱动有限",
                 "summary": "螺纹钢：昨日螺纹盘面震荡偏强。", "media": "光大期货", "link": "https://futures.eastmoney.com/a/31.html",
                 "publishDate": "2026-09-28T08:40:47+08:00", "source": "eastmoney"},
                {"title": "黑色系 | 双焦：复产预期兑现不足", "summary": "钢材：20mm三级螺纹钢全国均价3323/吨（+8)。",
                 "media": "中财期货", "link": "https://futures.eastmoney.com/a/32.html",
                 "publishDate": "2026-09-28T08:15:21+08:00", "source": "eastmoney"},
                # 已由搜索找到的同一家机构（铜冠金源）：只留更新的那条。
                {"title": "黑色系 | 螺纹：旧观点", "summary": "螺纹钢：旧", "media": "铜冠金源期货",
                 "link": "https://futures.eastmoney.com/a/30.html", "publishDate": "2026-09-27T08:00:00+08:00",
                 "source": "eastmoney"}]


async def test_the_desk_research_section_adds_the_column_notes_once_per_institution() -> None:
    sources = _ColumnSources()
    desk, _ = _desk(sources=sources)
    await desk.report("SHFE.rb2701")
    await _collected(desk)
    report = await desk.report("SHFE.rb2701")
    research = [fact for fact in report["facts"] if fact["section"] == "research"]
    assert [fact["label"] for fact in research] == ["铜冠金源期货", "光大期货", "中财期货", "南华期货"]  # 跨两路仍按时间新到旧
    everbright = research[1]
    assert everbright["value"] == "螺纹钢：产量、需求、库存均处于较低水平 供需层面驱动有限。螺纹钢：昨日螺纹盘面震荡偏强。"
    assert everbright["url"] == "https://futures.eastmoney.com/a/31.html" and everbright["kind"] == "report"
    assert research[2]["value"].startswith("螺纹钢全国均价3323")  # 只在摘要里提到：从提到处取
    sections = {item["key"]: item for item in report["sections"]}
    assert sections["research"]["status"] == "ok"
    # 全部品种共用一份栏目读取：再读别的品种不再请求栏目。
    await desk.report("DCE.i2701")
    await _collected(desk)
    assert sources.calls.count("orgfeed") == 1


class _FeedOnly(_Sources):
    async def org_notes(self, name: str, now: Any) -> list[dict[str, Any]]:
        raise SourceError("eastmoney_timeout")

    org_feed = _ColumnSources.org_feed


async def test_the_column_notes_stand_when_the_note_search_fails() -> None:
    desk, _ = _desk(sources=_FeedOnly())
    await desk.report("SHFE.rb2701")
    await _collected(desk)
    report = await desk.report("SHFE.rb2701")
    research = [fact["label"] for fact in report["facts"] if fact["section"] == "research"]
    assert research == ["光大期货", "中财期货", "铜冠金源期货"]
    assert {item["key"]: item for item in report["sections"]}["research"]["status"] == "ok"
