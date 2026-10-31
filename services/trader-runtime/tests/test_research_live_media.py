"""第 3 优先级中文财经媒体、郑商所/大商所公告（东方财富交易所公告栏）与原始来源关联（2026-10-13）：
录制页面，不出网。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, date, datetime, time, timedelta

import pytest

from test_research_live import NOW, Rows
from test_research_live_sources import FIXTURES, DirectLive, cloud_service, recordings_after_now  # noqa: F401 — fixture
from trader_runtime import research_live as live


def test_the_chinese_media_parsers_read_title_time_and_link():
    stcn = live.parse_stcn_kx((FIXTURES / "stcn-kx.json").read_text(encoding="utf-8"))
    assert stcn[0]["标题"] == "新希望：9月商品猪销售收入13.82亿元，环比降8.20%"
    assert stcn[0]["内容"].startswith("人民财讯10月13日电")
    assert stcn[0]["链接"].startswith("https://www.stcn.com/article/detail/")
    assert stcn[0]["发布时间"].utcoffset() == timedelta(hours=8)  # 毫秒时间戳记北京时间
    fx678 = live.parse_fx678_kx((FIXTURES / "fx678-kx.html").read_text(encoding="utf-8"))
    assert len(fx678) == 4  # 数据公布类条目（前值/预期/公布值）不算新闻
    assert fx678[0]["内容"] == "韩国央行行长：目前无需降息。预计韩元将进一步升值"
    assert fx678[0]["发布时间"].isoformat() == "2026-10-13T17:32:16+08:00"  # 条目 id 编着时间
    assert fx678[1]["标题"] == "欧洲央行隔夜存款规模小幅回落，借款需求维持低位"  # 【…】壳当标题
    paper = live.parse_thepaper_channel((FIXTURES / "thepaper-channel.html").read_text(encoding="utf-8"))
    assert len(paper) == 5 and paper[0]["链接"] == "https://www.thepaper.cn/newsDetail_forward_34242770"
    assert [row["发布时间"] for row in paper] == sorted((row["发布时间"] for row in paper), reverse=True)
    nbd = live.parse_nbd_column((FIXTURES / "nbd-column.html").read_text(encoding="utf-8"))
    assert nbd[0]["发布时间"].isoformat() == "2026-10-13T17:26:12+08:00"  # 路径里的日期 + 旁边的时分秒
    assert nbd[0]["链接"] == "https://www.nbd.com.cn/articles/2026-10-13/4603097.html"
    cctv = live.cctv_rows(json.loads((FIXTURES / "cctv-xwlb.json").read_text(encoding="utf-8")), date(2026, 10, 12))
    assert cctv[-1]["标题"] == "国际联播快讯" and cctv[0]["发布时间"].isoformat() == "2026-10-12T19:00:00+08:00"
    assert cctv[0]["链接"] == "https://tv.cctv.com/lm/xwlb/day/20261012.shtml"
    for parse in (live.parse_stcn_kx, live.parse_fx678_kx, live.parse_thepaper_channel, live.parse_nbd_column,
                  live.parse_eastmoney_column):
        assert parse("<html>改版后的页面</html>") == []


def test_wire_prefixes_are_stripped_for_cross_source_dedup():
    assert live.flash_body("人民财讯10月13日电，工业和信息化部近日印发通知") == "工业和信息化部近日印发通知"
    assert live.flash_body("<b>汇通财经APP讯——</b>交易员：韩国SK Energy已采购") == "交易员：韩国SK Energy已采购"


def test_the_exchange_board_files_czce_and_dce_notices_under_their_exchange():
    board = live.exchange_board_rows(live.parse_eastmoney_column((FIXTURES / "eastmoney-525.json").read_text(encoding="utf-8")))
    assert set(board) == {"郑商所", "大商所"}  # 上期所等已直读官网，栏目里的副本不再读
    czce = board["郑商所"][0]
    assert czce["标题"] == "关于就修订《郑州商品交易所违规处理办法》公开征求意见的公告"
    assert czce["内容"] == czce["标题"]  # 只留标题：正文开头是套话
    assert czce["发布时间"].isoformat() == "2026-10-12T17:21:30+08:00"
    assert czce["链接"].startswith("https://www.eastmoneyfutures.com/a/")


@pytest.mark.usefixtures("recordings_after_now")
def test_the_cloud_poll_reads_the_new_sources_and_keeps_only_futures_related_media(tmp_path):
    svc = cloud_service(tmp_path)
    outcome = svc.poll_flash_once()
    assert {"郑商所", "大商所", "证券时报", "汇通", "澎湃", "新闻联播"} <= set(outcome["collected"])
    # 14:00 北京还没到当天新闻联播文字稿上网的时间：读前一天的。
    assert "news_cctv:2026-10-02" in svc.sources.recorded.calls
    with svc.lock:
        rows = {row[0]: (row[1], json.loads(row[2]), json.loads(row[3])) for row in svc.connection.execute(
            "SELECT title, source, products_json, tags_json FROM news_flash")}
    czce = rows["关于就修订《郑州商品交易所违规处理办法》公开征求意见的公告"]
    assert czce[0] == "郑商所" and "交易所" in czce[2]
    # 综合财经媒体只留打上品种或宏观标签的：新希望的商品猪（生猪）留下，长鑫存储、5G 工厂不进表。
    assert rows["新希望：9月商品猪销售收入13.82亿元，环比降8.20%"][1] == ["lh"]
    assert "三佳科技：公司目前与长鑫存储无业务往来" not in rows and "2026年5G工厂名录遴选工作启动" not in rows
    assert "新玩家要来了！Visa中国获准筹备银行卡清算机构" not in rows  # 澎湃同理
    # 录制的三条每经要闻里只有“美股盘前”与期货有关：外盘美股指数期货（2026-10-21）的快讯键标普、纳指认它，其余两条不进表。
    nbd = {title: products for title, (source, products, _) in rows.items() if source == "每经"}
    assert len(nbd) == 1 and next(iter(nbd)).startswith("【美股盘前】") and {"spx", "nasdaq"} <= set(next(iter(nbd.values())))
    assert any(source == "新闻联播" for source, _, _ in rows.values())  # 新闻联播不受此限
    # 交易所公告栏读取失败时按交易所名报（与来源清单一致）。
    svc.sources.notice_exchange_board = lambda: (_ for _ in ()).throw(OSError("eastmoney down"))
    svc._direct_due.clear()
    svc.poll_flash_once()
    assert {"郑商所", "大商所"} <= set(svc.flash_feed()["lastPoll"]["failedSources"])


class _Gov(DirectLive):
    """RSSHub routes answer `routes` (route → JSON Feed items); 财联社 answers `cls`."""

    def __init__(self, routes, cls) -> None:
        super().__init__()
        self.routes, self.cls = routes, cls

    def rsshub(self, base, route):
        self.recorded._guard(f"rsshub:{route}")
        return {"items": self.routes.get(route, [])}

    def flash_cls(self):
        self.recorded._guard("flash_cls")
        return Rows(self.cls)


STATS_ITEM = {"title": "2026年9月份居民消费价格同比上涨0.4%", "url": "https://www.stats.gov.cn/sj/zxfb/202610/t1.html",
              "date_published": "2026-10-03T01:30:00Z"}  # 09:30 北京
STATS_COPY = {"标题": "国家统计局：9月份居民消费价格同比上涨0.4%",
              "内容": "财联社10月3日电，国家统计局数据显示，2026年9月份，全国居民消费价格同比上涨0.4%。其中，城市上涨0.4%。",
              "发布日期": date(2026, 10, 3), "发布时间": time(9, 31)}


def _service(tmp_path, name, sources, clock=lambda: 1000.0):
    return live.ResearchLiveService(connection=sqlite3.connect(tmp_path / name, check_same_thread=False),
                                    lock=threading.Lock(), state_root=tmp_path, sources=sources, clock=clock,
                                    now=lambda: NOW, rsshub_url=lambda: "http://rsshub.local")


def _cpi(svc):
    return [item for item in svc.flash_feed(limit=200)["items"] if "居民消费价格" in item["title"]]


def test_a_report_citing_an_official_release_merges_into_it_either_way(tmp_path):
    """“国家统计局数据显示…”的转载认出原始来源，并进统计局自己的条目——官方先到或转载先到都一样。"""
    # 同一轮：转载并进官方条目，归在统计局名下，不再标“据”。
    svc = _service(tmp_path, "a.sqlite3", _Gov({"/gov/stats": [STATS_ITEM]}, [STATS_COPY]))
    svc.poll_flash_once()
    merged = _cpi(svc)
    assert len(merged) == 1
    assert merged[0]["source"] == "国家统计局" and merged[0]["sources"] == ["国家统计局", "财联社"]
    assert not any(tag.startswith("据:") for tag in merged[0]["tags"])
    # 转载先到（统计局这一轮没答）：单独成行并标“据:国家统计局”；统计局条目下一轮到了，并进同一行。
    clock = [1000.0]
    later = _Gov({}, [STATS_COPY])
    svc2 = _service(tmp_path, "b.sqlite3", later, clock=lambda: clock[0])
    svc2.poll_flash_once()
    (copy,) = _cpi(svc2)
    assert copy["source"] == "财联社" and "据:国家统计局" in copy["tags"]
    later.routes = {"/gov/stats": [STATS_ITEM]}
    clock[0] += 3600
    svc2.poll_flash_once()
    (merged,) = _cpi(svc2)
    assert merged["id"] == copy["id"]
    assert merged["source"] == "国家统计局" and set(merged["sources"]) == {"国家统计局", "财联社"}
    assert merged["publishedAt"] == "2026-10-03T09:30:00+08:00"  # 官方更早：标题、时间归官方
    assert "据:国家统计局" not in merged["tags"]


def test_a_paraphrase_keeps_its_own_row_with_its_original_source(tmp_path):
    paraphrase = {"标题": "统计局：9月CPI同比涨0.4%", "内容": "财联社10月3日电，据国家统计局，9月CPI同比上涨0.4%，核心CPI上涨0.8%。",
                  "发布日期": date(2026, 10, 3), "发布时间": time(9, 40)}
    svc = _service(tmp_path, "p.sqlite3", _Gov({"/gov/stats": [STATS_ITEM]}, [paraphrase]))
    svc.poll_flash_once()
    rows = [item for item in svc.flash_feed(limit=200)["items"] if "0.4%" in f"{item['title']}{item['text']}"]
    assert {item["source"] for item in rows} == {"国家统计局", "财联社"}  # 说法改写过：不硬并
    assert "据:国家统计局" in next(item for item in rows if item["source"] == "财联社")["tags"]
    # 带“据:”标签的媒体条目不算官方条目：保留期仍按媒体算。
    official = {item["id"] for item in svc.flash_feed(hours=24 * 30, limit=200, official=True)["items"]}
    assert [item["source"] for item in rows if item["id"] in official] == ["国家统计局"]


def test_an_unrelated_official_item_of_the_same_source_is_not_merged(tmp_path):
    """同一机构的另一条发布（标题对不上）不会把转载吸过去。"""
    other = {**STATS_ITEM, "title": "2026年9月份工业生产者出厂价格同比下降2.3%", "url": "https://www.stats.gov.cn/sj/zxfb/202610/t2.html"}
    svc = _service(tmp_path, "u.sqlite3", _Gov({"/gov/stats": [other]}, [STATS_COPY]))
    svc.poll_flash_once()
    (copy,) = _cpi(svc)
    assert copy["source"] == "财联社" and copy["sources"] == ["财联社"] and "据:国家统计局" in copy["tags"]


COPPER_NOTICE = {"标题": "关于调整铜期货交易保证金的通知", "内容": "关于调整铜期货交易保证金的通知",
                 "发布时间": datetime(2026, 10, 3, 9, 0, tzinfo=live.CHINA),
                 "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/copper.html"}
ALUMINIUM_REPORT = {"标题": "关于调整铝期货交易保证金的通知", "内容": "上期所发布通知，调整铝期货交易保证金，自即日起执行",
                    "发布日期": date(2026, 10, 3), "发布时间": time(9, 1)}
COPPER_REPORT = {"标题": "上期所：关于调整铜期货交易保证金的通知", "内容": "上期所发布通知，调整铜期货相关合约交易保证金，自即日起执行。",
                 "发布日期": date(2026, 10, 3), "发布时间": time(9, 5)}


class _Shfe(_Gov):
    """上期所 answers `notices`, 财联社 answers `cls`."""

    def __init__(self, notices, cls) -> None:
        super().__init__({}, cls)
        self.notices = notices

    def notice_shfe(self):
        self.recorded._guard("notice_shfe")
        return Rows(self.notices)


def _margin(svc):
    return sorted((item for item in svc.flash_feed(limit=200)["items"] if "交易保证金" in item["title"]),
                  key=lambda item: item["publishedAt"])


# 品种在标题中间（字面就对不上）与在标题开头（字面像删掉的开头套话，靠品种不一致分开）。
LEADING = ({**COPPER_NOTICE, "标题": "铜期货交易保证金调整的通知", "内容": "铜期货交易保证金调整的通知"},
           {**ALUMINIUM_REPORT, "标题": "铝期货交易保证金调整的通知"})


@pytest.mark.parametrize("official_first", [True, False])
@pytest.mark.parametrize(("notice", "report"), [(COPPER_NOTICE, ALUMINIUM_REPORT), LEADING])
def test_a_same_template_notice_for_another_product_keeps_its_own_row(tmp_path, official_first, notice, report):
    """上期所铜保证金通知与财联社转述的铝保证金通知共用一个模板（#854）：两条新闻各留一行、各带自己的
    品种、正文与来源——同一轮官方先入表，或媒体前一轮先到、官方下一轮才到，结果一样。"""
    clock = [1000.0]
    sources = _Shfe([notice] if official_first else [], [report])
    svc = _service(tmp_path, "tpl.sqlite3", sources, clock=lambda: clock[0])
    svc.poll_flash_once()
    if not official_first:
        sources.notices = [notice]
        clock[0] += 3600
        svc._direct_due.clear()
        svc.poll_flash_once()
    copper, aluminium = _margin(svc)
    assert copper["sources"] == ["上期所"] and "cu" in copper["products"] and "al" not in copper["products"]
    assert copper["link"] == COPPER_NOTICE["链接"]
    assert aluminium["sources"] == ["财联社"] and aluminium["products"] == ["al"]
    assert aluminium["text"] == report["内容"] and "据:上期所" in aluminium["tags"]


@pytest.mark.parametrize("official_first", [True, False])
def test_a_report_retelling_the_notice_still_merges_into_it_either_way(tmp_path, official_first):
    clock = [1000.0]
    sources = _Shfe([COPPER_NOTICE] if official_first else [], [COPPER_REPORT])
    svc = _service(tmp_path, "same.sqlite3", sources, clock=lambda: clock[0])
    svc.poll_flash_once()
    if not official_first:
        sources.notices = [COPPER_NOTICE]
        clock[0] += 3600
        svc._direct_due.clear()
        svc.poll_flash_once()
    (merged,) = _margin(svc)
    assert merged["source"] == "上期所" and set(merged["sources"]) == {"上期所", "财联社"}
    assert merged["link"] == COPPER_NOTICE["链接"] and "据:上期所" not in merged["tags"]


def test_a_report_fitting_two_similar_notices_of_one_source_stays_on_its_own(tmp_path):
    """同一交易所两条相近的通知都对得上一篇转载：说不准是哪一条，就不并（不靠遍历顺序挑第一条）。"""
    wider = {**COPPER_NOTICE, "标题": "关于调整铜期货交易保证金和涨跌停板幅度的通知",
             "内容": "关于调整铜期货交易保证金和涨跌停板幅度的通知",
             "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/copper-limit.html"}
    report = {**COPPER_REPORT, "标题": "上期所：关于调整铜期货交易保证金和涨跌停板幅度的通知"}
    svc = _service(tmp_path, "two.sqlite3", _Shfe([COPPER_NOTICE, wider], [report]))
    svc.poll_flash_once()
    rows = _margin(svc)
    assert len(rows) == 3 and [row["sources"] for row in rows] == [["上期所"], ["上期所"], ["财联社"]]


def test_a_digest_mentioning_the_release_further_down_is_a_story_of_its_own(tmp_path):
    """综述在后半段顺带转述统计局发布（10-14 线上：午间综述被并进 CPI 条目，正文与链接没了）：各留一行。"""
    digest = {"标题": "午间要闻速递",
              "内容": "1、截至午间收盘，上证指数涨0.01%，深证成指跌0.51%，创业板指跌1.15%，两市半日成交额1.1万亿元，"
                      "较上个交易日缩量800亿元，全市场超3000只个股下跌；2、商务部：前三季度服务贸易保持增长，知识密集型服务"
                      "进出口增长较快；3、国家统计局数据显示，2026年9月份，全国居民消费价格同比上涨0.4%。",
              "发布日期": date(2026, 10, 3), "发布时间": time(11, 40)}
    assert digest["内容"].index("2026年9月份") > live.CITE_OPENING_CHARS
    svc = _service(tmp_path, "digest.sqlite3", _Gov({"/gov/stats": [STATS_ITEM]}, [digest]))
    svc.poll_flash_once()
    rows = {item["source"]: item for item in svc.flash_feed(limit=200)["items"]
            if item["source"] in {"国家统计局", "财联社"}}
    assert rows["国家统计局"]["sources"] == ["国家统计局"]
    assert rows["财联社"]["text"] == digest["内容"] and "据:国家统计局" in rows["财联社"]["tags"]


def test_an_official_notice_arriving_after_several_retellings_takes_them_all(tmp_path):
    """#853：两篇措辞不同的转载先入库（各自一行、标“据:上期所”），上期所通知下一轮才到——两篇都并进同一行，
    不再按正文各留一行（否则同一发布出多个事件、筛两次）；被并掉那篇的正文下次再来也落到这一行。"""
    second = {"标题": "上期所：关于调整铜期货交易保证金的通知",
              "内容": "财联社10月3日电，上期所调整铜期货交易保证金，涉及铜期货全部合约，自下一交易日起执行。",
              "发布日期": date(2026, 10, 3), "发布时间": time(9, 20)}
    clock = [1000.0]
    sources = _Shfe([], [COPPER_REPORT, second])
    svc = _service(tmp_path, "many.sqlite3", sources, clock=lambda: clock[0])
    svc.poll_flash_once()
    assert len(_margin(svc)) == 2
    sources.notices = [COPPER_NOTICE]
    clock[0] += 3600
    svc._direct_due.clear()
    svc.poll_flash_once()
    (merged,) = _margin(svc)
    assert merged["source"] == "上期所" and set(merged["sources"]) == {"上期所", "财联社"}
    assert merged["link"] == COPPER_NOTICE["链接"] and "据:上期所" not in merged["tags"]
    clock[0] += 3600
    svc.poll_flash_once()
    assert [item["id"] for item in _margin(svc)] == [merged["id"]]
    with svc.lock:
        orphans = svc.connection.execute("SELECT COUNT(*) FROM news_flash_alias WHERE id NOT IN (SELECT id FROM news_flash)")
        assert orphans.fetchone()[0] == 0


def test_a_future_dated_item_never_enters_the_feed(tmp_path):
    """#754：发布时间晚于当前一天以上的条目（伪造或写错年份）不入库，免得按时间长期排在最前、占住行数名额。"""
    ahead = {**COPPER_NOTICE, "标题": "关于调整铝期货交易保证金的通知", "内容": "关于调整铝期货交易保证金的通知",
             "发布时间": NOW.astimezone(live.CHINA) + timedelta(days=400),
             "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/ahead.html"}
    # 官方按日期发布，时间记得偏早偏晚都在一天之内：照收。
    soon = {**COPPER_NOTICE, "发布时间": NOW.astimezone(live.CHINA) + timedelta(hours=2)}
    svc = _service(tmp_path, "ahead.sqlite3", _Shfe([ahead, soon], []))
    svc.poll_flash_once()
    shown = svc.flash_feed(hours=24 * 30, limit=200, official=True)["items"]
    assert [item["link"] for item in shown if "shfe.com.cn" in (item["link"] or "")] == [COPPER_NOTICE["链接"]]


@pytest.mark.usefixtures("recordings_after_now")
def test_the_evening_news_is_read_again_once_the_day_turns(tmp_path):
    """#853：19:44 读了前一天的新闻联播，19:45 一到当天的文字稿就该读，不等到一小时后的 20:44。"""
    moment = [datetime(2026, 10, 3, 11, 44, tzinfo=UTC)]  # 19:44 北京
    clock = [1000.0]
    svc = live.ResearchLiveService(connection=sqlite3.connect(tmp_path / "cctv.sqlite3", check_same_thread=False),
                                   lock=threading.Lock(), state_root=tmp_path, sources=DirectLive(), clock=lambda: clock[0],
                                   now=lambda: moment[0], rsshub_url=lambda: "http://rsshub.local")
    svc.poll_flash_once()
    assert "news_cctv:2026-10-02" in svc.sources.recorded.calls
    moment[0] += timedelta(minutes=2)
    clock[0] += 120
    svc.poll_flash_once()
    assert "news_cctv:2026-10-03" in svc.sources.recorded.calls
    # 同一天不再提前重读：照旧按一小时间隔。
    calls = len([call for call in svc.sources.recorded.calls if call.startswith("news_cctv:")])
    moment[0] += timedelta(minutes=2)
    clock[0] += 120
    svc.poll_flash_once()
    assert len([call for call in svc.sources.recorded.calls if call.startswith("news_cctv:")]) == calls
