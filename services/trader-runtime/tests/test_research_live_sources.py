"""The direct news sources (exchange notices, industry wires, the English wire) and English tagging:
recorded page samples only, no network."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import sleep

import pytest

from test_news_translate import _Model as _TranslateModel
from test_research_live import NOW, RecordedLive, Rows
from trader_runtime import research_live as live
from trader_runtime.news_screen import NewsScreener
from trader_runtime.news_translate import TITLE_CHARS, NewsTranslator

FIXTURES = Path(__file__).parent / "fixtures" / "news"


@pytest.fixture
def recordings_after_now(monkeypatch):
    """The recorded pages (2026-10-08 to 10-13) are dated after the tests' clock (NOW, 10-03): the poll's
    upper bound (now + FLASH_FUTURE_LIMIT) would drop them, so tests replaying them widen it."""
    monkeypatch.setattr(live, "FLASH_FUTURE_LIMIT", timedelta(days=60))


pytestmark = pytest.mark.usefixtures("recordings_after_now")


def test_the_exchange_notice_parsers_read_title_link_and_day():
    rows = live.parse_shfe_notices((FIXTURES / "shfe-notices.html").read_text(encoding="utf-8"))
    assert rows[0]["标题"] == "上海国际能源交易中心发布关于调整原油、低硫燃料油期货相关合约交易限额的通知"
    assert rows[0]["链接"] == "https://www.shfe.com.cn/publicnotice/notice/202610/t20261009_833672.html"
    assert rows[0]["发布时间"].isoformat() == "2026-10-09T00:00:00+08:00"  # 页面只给日期，记当天 00:00 北京
    gfex = live.parse_gfex_notices((FIXTURES / "gfex-notices.html").read_text(encoding="utf-8"))
    assert gfex[0]["标题"] == "关于就氢氧化锂期货和期权合约及相关规则公开征求意见的公告"
    assert gfex[0]["链接"].startswith("http://www.gfex.com.cn/gfex/tzts/202609/")
    assert [row["发布时间"].isoformat() for row in gfex] == ["2026-10-08T00:00:00+08:00",
                                                             "2026-09-24T00:00:00+08:00", "2026-09-24T00:00:00+08:00"]
    # 标题 attr 里的 <br> 折成空格，不进正文。
    assert "有关事项 的通知" in gfex[1]["标题"]


def test_the_industry_wire_parsers_read_title_link_and_time():
    smm = live.parse_smm_news((FIXTURES / "smm-news.html").read_text(encoding="utf-8"))
    assert smm[0]["标题"].startswith("金属普涨 伦锡领涨")
    assert smm[0]["链接"] == "https://news.smm.cn/news/104149228"
    assert smm[0]["发布时间"].isoformat() == "2026-10-11T09:39:31+08:00"
    assert live.parse_smm_news("<html>没有 __NEXT_DATA__ 的页面</html>") == []
    mysteel = live.parse_mysteel_news((FIXTURES / "mysteel-news.html").read_text(encoding="utf-8"))
    assert [(row["标题"], row["链接"]) for row in mysteel] == [
        ("乘联分会：9月全国乘用车厂商新能源批发预估167万辆", "https://news.mysteel.com/a/26101008/14066555C6BED1CE.html"),
        ("钢材下游周报：房地产进入存量时代", "https://news.mysteel.com/a/26100917/435A1736B5B20A12.html")]
    # 发布时间编在路径 a/YYMMDDHH/hash.html 里，整点精度，最新在前。
    assert [row["发布时间"].isoformat() for row in mysteel] == ["2026-10-10T08:00:00+08:00",
                                                                "2026-10-09T17:00:00+08:00"]


def test_the_financialjuice_parser_reads_rss_titles_and_times():
    rows = live.parse_financialjuice((FIXTURES / "finjuice-rss.xml").read_text(encoding="utf-8"))
    assert rows[0]["标题"].startswith("All 22 Indian crew members rescued")  # "FinancialJuice: " 前缀剥掉
    assert rows[0]["发布时间"].utcoffset().total_seconds() == 0  # RFC822 GMT 时间进来了
    assert rows[1]["链接"].startswith("https://www.financialjuice.com/News/")
    assert live.parse_financialjuice("not xml") == []


def test_english_headlines_tag_products_and_macro():
    aliases: list[tuple[str, tuple[str, ...]]] = []

    def tagged(title: str, words: list[tuple[str, tuple[str, ...]]] | None = None) -> tuple[list[str], list[str]]:
        item = live.tag_flash_item({"title": title, "text": "", "source": "FinancialJuice"},
                                   words if words is not None else aliases)
        return item["products"], item["tags"]

    assert tagged("LME copper stocks fall as premiums rise in Asia")[0] == ["bc", "cu"]
    assert tagged("Iron ore climbs on China stimulus hopes")[0] == ["i"]
    assert "sc" in tagged("Brent crude slips after OPEC+ output decision")[0]
    assert tagged("US CPI comes in hotter than expected, Fed rate cut bets fade")[1] == ["macro"]
    # 中文表里的拉丁缩写按拉丁边界命中：US CPI 里的 CP 不是 LPG、US PPI 里的 PP 不是聚丙烯；
    # 中文标题里的 PTA装置、英文里的 TC 加工费照常命中。
    assert tagged("US CPI comes in hotter than expected, Fed rate cut bets fade")[0] == []
    assert tagged("US PPI rises more than forecast")[0] == []
    assert tagged("Smelter TC fees for copper concentrate rise")[0] == ["bc", "cu"]
    assert tagged("PTA装置检修增加，供应收紧", [("ta", ("PTA",))]) == (["ta"], [])
    # Fed 按原大小写匹配：小写 fed 是动词，不是美联储；全大写 FED 同样命中。
    assert tagged("Risk rally fed by optimism across equity markets") == ([], [])
    assert tagged("Fed signals patience on policy path")[1] == ["macro"]
    assert tagged("FED signals patience on policy path")[1] == ["macro"]
    # 词边界：动词 lead 不是铅，corner 里没有 corn。
    assert tagged("Team lead changes at the Treasury Department")[0] == []
    assert tagged("Team takes a corner office in the Treasury building") == ([], [])
    # 英文标题里的中文单字不参与遮蔽逻辑之外的误命中。
    assert tagged("Copper rallies while Shanghai steel rebar weakens")[0] == ["bc", "cu", "rb"]


def test_english_words_are_derived_from_the_chinese_tables():
    """英文词的品种归属从中文别名/产业链词表推导，不另维护一份：copper 覆盖 cu+bc（bc 无中文
    条目，走上期所金属分页的额外提示），alumina 到 ao 而不只是 al，钢/焦/油这类链词带上产业链品种。"""
    assert live.FLASH_ENGLISH_WORDS["cu"] == ["copper"] and live.FLASH_ENGLISH_WORDS["bc"] == ["copper"]
    # 英式 aluminium 与美式 aluminum 都认（“Aluminum stocks fall”曾打不上品种，#516）。
    assert live.FLASH_ENGLISH_WORDS["ao"] == ["alumina", "aluminium", "aluminum"]
    assert live.FLASH_ENGLISH_WORDS["al"] == ["alumina", "aluminium", "aluminum"]
    steel = ["steel mill", "steel output", "steel price", "steel production"]
    assert live.FLASH_ENGLISH_WORDS["ss"] == steel  # 不锈钢也在中文“钢”锚词里
    assert live.FLASH_ENGLISH_WORDS["rb"] == ["coke", "rebar", *steel]  # 螺纹的产业链词带焦炭
    assert live.FLASH_ENGLISH_WORDS["i"] == ["iron ore", *steel]  # 矿山的词表里有钢厂/粗钢产量
    assert live.FLASH_ENGLISH_WORDS["a"] == live.FLASH_ENGLISH_WORDS["b"] == ["soybean", "soybeans"]
    # 锚词精确：黄金只归黄金（白银不含在黄金的锚词里）。
    assert live.FLASH_ENGLISH_WORDS["au"] == ["bullion", "gold"] and "gold" not in live.FLASH_ENGLISH_WORDS.get("ag", [])


def test_exchange_sources_carry_the_exchange_tag():
    aliases: list[tuple[str, tuple[str, ...]]] = [("rb", ("螺纹钢",))]
    notice = live.tag_flash_item({"title": "关于调整螺纹钢期货交易保证金的通知", "text": "", "source": "上期所"}, aliases)
    assert notice["tags"] == ["交易所"] and notice["products"] == ["rb"]
    media = live.tag_flash_item({"title": "螺纹钢产量下降", "text": "", "source": "财联社"}, aliases)
    assert media["tags"] == []


# -- the poll over the direct sources (cloud runtime shape): recorded pages, no network.

FINJUICE_ROW = {"标题": "Copper rises as Fed signals patience", "内容": "Copper rises as Fed signals patience",
                "发布时间": datetime(2026, 10, 11, 5, 30, tzinfo=UTC),
                "链接": "https://www.financialjuice.com/News/9800415/example.aspx"}


class DirectLive(live.LiveSources):
    """The cloud runtime's source surface: the recorded AKShare double plus the direct pages and
    RSSHub (both answered from the recorded fixtures)."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded = RecordedLive()

    def module(self):  # the AKShare surface, answered by the recorded double
        return self.recorded

    def rsshub(self, base, route):
        self.recorded._guard(f"rsshub:{route}")
        return {"items": []}

    def notice_shfe(self):
        self.recorded._guard("notice_shfe")
        return Rows(live.parse_shfe_notices((FIXTURES / "shfe-notices.html").read_text(encoding="utf-8")))

    def notice_gfex(self):
        self.recorded._guard("notice_gfex")
        return Rows(live.parse_gfex_notices((FIXTURES / "gfex-notices.html").read_text(encoding="utf-8")))

    def news_smm(self):
        self.recorded._guard("news_smm")
        return Rows(live.parse_smm_news((FIXTURES / "smm-news.html").read_text(encoding="utf-8")))

    def news_mysteel(self):
        self.recorded._guard("news_mysteel")
        return Rows(live.parse_mysteel_news((FIXTURES / "mysteel-news.html").read_text(encoding="utf-8")))

    def flash_financialjuice(self):
        self.recorded._guard("financialjuice")
        return Rows([dict(FINJUICE_ROW)])

    def news_stcn(self):
        self.recorded._guard("news_stcn")
        return Rows(live.parse_stcn_kx((FIXTURES / "stcn-kx.json").read_text(encoding="utf-8")))

    def news_fx678(self):
        self.recorded._guard("news_fx678")
        return Rows(live.parse_fx678_kx((FIXTURES / "fx678-kx.html").read_text(encoding="utf-8")))

    def news_thepaper(self):
        self.recorded._guard("news_thepaper")
        return Rows(live.parse_thepaper_channel((FIXTURES / "thepaper-channel.html").read_text(encoding="utf-8")))

    def news_nbd(self):
        self.recorded._guard("news_nbd")
        return Rows(live.parse_nbd_column((FIXTURES / "nbd-column.html").read_text(encoding="utf-8")))

    def notice_exchange_board(self):
        self.recorded._guard("notice_exchange_board")
        return live.exchange_board_rows(live.parse_eastmoney_column((FIXTURES / "eastmoney-525.json").read_text(encoding="utf-8")))

    def news_cctv(self, day):
        self.recorded._guard(f"news_cctv:{day.isoformat()}")
        return json.loads((FIXTURES / "cctv-xwlb.json").read_text(encoding="utf-8"))


def cloud_service(tmp_path, translator=None):
    connection = sqlite3.connect(tmp_path / "direct.sqlite3", check_same_thread=False)
    return live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                     sources=DirectLive(), clock=lambda: 1000.0, now=lambda: NOW,
                                     rsshub_url=lambda: "http://rsshub.local", translator=translator)


def test_the_cloud_poll_stores_direct_sources_and_tags_them(tmp_path):
    svc = cloud_service(tmp_path)
    # 来源清单：官方（交易所与部委）排在前，媒体在后；RSSHub 与直读源只在云端出现。
    assert svc.flash_sources()[:6] == ["中金所", "上期所", "郑商所", "大商所", "广期所", "国家统计局"]
    assert svc.flash_sources()[-8:] == ["上海有色网", "我的钢铁网", "FinancialJuice", "证券时报", "汇通", "澎湃", "每经", "新闻联播"]
    assert live.EXCHANGE_BOARD not in svc.flash_sources()  # 读取名不是来源名：条目按交易所归名
    outcome = svc.poll_flash_once()
    assert {"上期所", "广期所", "上海有色网", "我的钢铁网", "FinancialJuice"} <= set(outcome["collected"])
    feed = svc.flash_feed()
    notice = next(item for item in feed["items"] if item["source"] == "上期所")
    assert notice["tags"] == ["交易所"] and "sc" in notice["products"]  # 原油/低硫燃料油通知
    english = next(item for item in feed["items"] if item["source"] == "FinancialJuice")
    assert english["products"] == ["bc", "cu"] and english["tags"] == ["macro"]
    assert english["origTitle"] is None  # 还没翻译：原文照常显示
    wire = next(item for item in feed["items"] if item["source"] == "上海有色网")
    assert wire["link"].startswith("https://news.smm.cn/news/")
    assert any(item["source"] == "我的钢铁网" for item in feed["items"])
    # 没配 RSSHUB_URL 的本机版一轮轮询不碰任何直读源。
    svc.rsshub_url = lambda: ""
    svc._direct_due.clear()
    local = svc.poll_flash_once()
    assert not {"上期所", "广期所", "上海有色网", "我的钢铁网", "FinancialJuice"} & set(local["collected"])


def test_official_copies_file_a_syndicated_story_under_the_exchange(tmp_path):
    """同一事件媒体转载（东方财富 10-01）与上期所通知（00:00）合并成一条，归在上期所名下。"""

    class RebarNotice(DirectLive):
        def notice_shfe(self):
            self.recorded._guard("notice_shfe")
            return Rows([{"标题": "关于螺纹钢期货的通知", "内容": "钢厂检修增加，螺纹钢产量下降，钢材库存回落。",
                          "发布时间": datetime(2026, 10, 3, 0, 0, tzinfo=live.CHINA),
                          "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/t1.html"}])

    connection = sqlite3.connect(tmp_path / "merge.sqlite3", check_same_thread=False)
    svc = live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                     sources=RebarNotice(), clock=lambda: 1000.0, now=lambda: NOW,
                                     rsshub_url=lambda: "http://rsshub.local")
    svc.poll_flash_once()
    rebar = next(item for item in svc.flash_feed()["items"] if "钢厂检修" in item["text"])
    assert rebar["source"] == "上期所" and rebar["sources"] == ["上期所", "东方财富", "同花顺"]
    assert rebar["tags"] == ["交易所"] and rebar["publishedAt"] == "2026-10-03T00:00:00+08:00"


async def test_the_poll_queues_english_items_and_translation_round_trips(tmp_path):
    model = _TranslateModel()
    svc = cloud_service(tmp_path)
    translator = NewsTranslator(connection=svc.connection, lock=svc.lock, generator=lambda: model,
                                clock=lambda: 1000.0, now=lambda: NOW)
    svc.translator = translator
    svc.poll_flash_once()
    assert list(translator._pending)  # 轮询把带品种/宏观标签的英文条目排进翻译队列
    translated = await translator.burst()
    assert translated >= 1
    english = next(item for item in svc.flash_feed()["items"] if item["source"] == "FinancialJuice")
    assert english["title"] == "〔中文〕Copper rises as Fed signals patience"
    assert english["origTitle"] == "Copper rises as Fed signals patience"


def test_a_later_exchange_copy_merges_tags_and_keeps_the_translated_title(tmp_path):
    """媒体先入库、交易所通知带着更早的发布时间后到：合并补上 交易所 标签，已译的中文标题
    不被英文原文覆盖（原文只在 origTitle 折叠里）。"""
    notice = {"标题": "Copper rises as Fed signals patience", "内容": "Copper rises as Fed signals patience",
              "发布时间": datetime(2026, 10, 11, 5, 0, tzinfo=UTC),
              "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/t2.html"}
    state = {"notice": False}

    class WireThenNotice(DirectLive):
        def notice_shfe(self):
            self.recorded._guard("notice_shfe")
            return Rows([dict(notice)]) if state["notice"] else Rows([])

    connection = sqlite3.connect(tmp_path / "late.sqlite3", check_same_thread=False)
    svc = live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                   sources=WireThenNotice(), clock=lambda: 1000.0, now=lambda: NOW,
                                   rsshub_url=lambda: "http://rsshub.local")
    svc.poll_flash_once()
    english = next(item for item in svc.flash_feed()["items"] if item["source"] == "FinancialJuice")
    assert english["tags"] == ["macro"]  # 还没有交易所副本
    with svc.lock, svc.connection:
        connection.execute("UPDATE news_flash SET title = ?, orig_title = ? WHERE id = ?",
                           ("美联储按兵不动，铜价上涨", "Copper rises as Fed signals patience", english["id"]))
    state["notice"] = True
    svc._direct_due.clear()  # 直读源按各自间隔读：让这轮到期
    svc.poll_flash_once()
    merged = next(item for item in svc.flash_feed()["items"] if item["id"] == english["id"])
    assert merged["publishedAt"] == "2026-10-11T13:00:00+08:00"  # 更早的一份胜出
    assert merged["source"] == "上期所" and set(merged["sources"]) == {"上期所", "FinancialJuice"}
    assert merged["title"] == "美联储按兵不动，铜价上涨"  # 译文留下
    assert merged["origTitle"] == "Copper rises as Fed signals patience"
    assert merged["tags"] == ["macro", "交易所"]  # 后到的交易所副本补上标签


def test_a_cloud_outage_is_measured_against_the_sources_actually_attempted(tmp_path):
    """全部失败按这一轮实际尝试的来源算（RSSHub 与直读源只在云端、到期才跑），不是写死的六家：
    六家媒体挂了、英文外电还答着时不报 research_flash_unavailable；全挂才报。"""

    class MediaDown(DirectLive):
        def __init__(self) -> None:
            super().__init__()
            self.recorded.failing = {"flash_cls", "flash_em", "flash_sina", "flash_shmet", "flash_ths", "flash_futu"}

    class AllDown(MediaDown):
        def rsshub(self, base, route):
            raise OSError("rsshub down")

        def notice_shfe(self):
            raise OSError("shfe down")

        def notice_gfex(self):
            raise OSError("gfex down")

        def news_smm(self):
            raise OSError("smm down")

        def news_mysteel(self):
            raise OSError("mysteel down")

        def flash_financialjuice(self):
            raise OSError("finjuice down")

        def news_stcn(self):
            raise OSError("stcn down")

        def news_fx678(self):
            raise OSError("fx678 down")

        def news_thepaper(self):
            raise OSError("thepaper down")

        def news_nbd(self):
            raise OSError("nbd down")

        def notice_exchange_board(self):
            raise OSError("eastmoney down")

        def news_cctv(self, day):
            raise OSError("cctv down")

    svc = live.ResearchLiveService(connection=sqlite3.connect(tmp_path / "outage.sqlite3", check_same_thread=False),
                                   lock=threading.Lock(), state_root=tmp_path, sources=MediaDown(),
                                   clock=lambda: 1000.0, now=lambda: NOW, rsshub_url=lambda: "http://rsshub.local")
    svc.poll_flash_once()
    served = svc.flash_feed()
    assert served["status"] == "ok"  # 外电与直读源还活着：不是全部中断
    assert {item["source"] for item in served["items"]} >= {"FinancialJuice", "上期所"}
    assert set(served["lastPoll"]["failedSources"]) == {"财联社", "东方财富", "新浪", "同花顺", "富途", "上海金属网"}

    dead = live.ResearchLiveService(connection=sqlite3.connect(tmp_path / "dead.sqlite3", check_same_thread=False),
                                     lock=threading.Lock(), state_root=tmp_path, sources=AllDown(),
                                     clock=lambda: 1000.0, now=lambda: NOW, rsshub_url=lambda: "http://rsshub.local")
    dead.poll_flash_once()
    failed = dead.flash_feed()
    assert failed["status"] == "unavailable" and failed["error"] == "research_flash_unavailable"
    assert len(failed["lastPoll"]["failedSources"]) == len(dead.flash_sources())  # 这一轮尝试的都失败了


def test_a_stalled_rsshub_route_neither_blocks_nor_piles_up(tmp_path, monkeypatch):
    """一条 RSSHub 路由卡住：别的路由照常答（每条路由自己的车道，等锁不占超时），卡住的路由
    在它真正结束前不再被排新一轮（线程不堆积）。可控阻塞桩 + 缩短的超时，单用例 ≤2 秒。"""
    monkeypatch.setattr(live, "FLASH_SOURCE_TIMEOUT_SECONDS", 0.15)
    release = threading.Event()
    calls: list[str] = []

    class OneStalled(DirectLive):
        def rsshub(self, base, route):
            calls.append(route)
            if route == "/cffex/announcement":
                assert release.wait(2)  # 卡到主线程放行（比 0.15 秒超时长）
                return {"items": []}
            return {"items": []}

    svc = live.ResearchLiveService(connection=sqlite3.connect(tmp_path / "stall.sqlite3", check_same_thread=False),
                                   lock=threading.Lock(), state_root=tmp_path, sources=OneStalled(),
                                   clock=lambda: 1000.0, now=lambda: NOW, rsshub_url=lambda: "http://rsshub.local")
    first = svc.poll_flash_once()
    assert "金十" in first["collected"]  # 金十照常答，没被卡住的路由拖死
    assert "中金所" not in first["collected"]  # 卡住的这条（中金所唯一路由）超时了
    svc.poll_flash_once()  # 时钟不动：到期的只有超时重置的这条
    assert calls.count("/cffex/announcement") == 1  # 上一轮还没真正结束：不再排它，线程不堆积
    release.set()  # 卡住的请求真正结束（丢弃结果），下一轮可以再排它
    for _ in range(100):  # 等被卡的线程真正退出（≤2s 的 20ms 轮询）
        if "/cffex/announcement" not in svc._rsshub_busy:
            break
        sleep(0.02)
    third = svc.poll_flash_once()
    assert calls.count("/cffex/announcement") == 2
    assert "中金所" in third["collected"]  # 这次答上了


async def test_long_titles_survive_to_translation_intact(tmp_path):
    """超过 80 字符的英文标题（fixture 第二条，117 字符）：入库、翻译、折叠的原文都是完整标题，
    不再存 title[:80]——截断会让“正文=原标题”去重判断失配，正文与截断的原文重复显示。"""
    row = live.parse_financialjuice((FIXTURES / "finjuice-rss.xml").read_text(encoding="utf-8"))[1]
    assert len(row["标题"]) > 80

    class LongWire(DirectLive):
        def flash_financialjuice(self):
            self.recorded._guard("financialjuice")
            return Rows([dict(row)])

    connection = sqlite3.connect(tmp_path / "long.sqlite3", check_same_thread=False)
    model = _TranslateModel()
    svc = live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                   sources=LongWire(), clock=lambda: 1000.0, now=lambda: NOW,
                                   rsshub_url=lambda: "http://rsshub.local")
    translator = NewsTranslator(connection=connection, lock=svc.lock, generator=lambda: model,
                                clock=lambda: 1000.0, now=lambda: NOW)
    svc.translator = translator
    svc.poll_flash_once()
    english = next(item for item in svc.flash_feed()["items"] if item["source"] == "FinancialJuice")
    assert english["title"] == row["标题"]  # 存的是完整的，不是前 80 字符
    assert await translator.burst() >= 1
    english = next(item for item in svc.flash_feed()["items"] if item["source"] == "FinancialJuice")
    # 译文基于完整标题（存储按 TITLE_CHARS 截 120，只 bound 译文列）
    assert english["title"] == f"〔中文〕{row['标题']}"[:TITLE_CHARS]
    assert english["origTitle"] == row["标题"]  # 折叠的原文也是完整的
    assert english["text"] == row["标题"]  # 正文与原文一致：界面靠 origTitle 折叠，不重复显示


def test_english_candidates_are_not_capped_out_by_chinese_rows(tmp_path):
    """窗口里 500 多条更新的带标签中文快讯不能把更老的英文条目挤出翻译候选：先筛英文、分页
    读满 200 条英文，任何预筛 LIMIT 都轮不到把英文挤出去。"""
    model = _TranslateModel()
    svc = cloud_service(tmp_path)
    translator = NewsTranslator(connection=svc.connection, lock=svc.lock, generator=lambda: model,
                                clock=lambda: 1000.0, now=lambda: NOW)
    svc.translator = translator
    svc.poll_flash_once()
    english = next(item for item in svc.flash_feed()["items"] if item["source"] == "FinancialJuice")
    base = datetime(2026, 10, 11, 14, 0, tzinfo=live.CHINA)
    with svc.lock, svc.connection:  # 520 条更新（排在英文前面）的带标签中文条目
        svc.connection.executemany(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json,"
            " tags_json) VALUES (?, '财联社', '[\"财联社\"]', ?, ?, ?, NULL, '[\"cu\"]', '[]')",
            [(f"zh{i}", (base + timedelta(minutes=i)).isoformat(timespec="seconds"),
              f"中文快讯第{i}条", f"中文快讯第{i}条内容") for i in range(520)])
    svc._queue_translations()
    assert list(translator._pending) == [english["id"]]  # 英文条目仍然排进了队列


def test_an_official_notice_files_syndicated_copies_by_title(tmp_path):
    """交易所通知的正文就是标题，媒体转载另写正文：正文键对不上时按（去标点的）标题合并，
    归到官方源名下并带上 交易所 标签——不是各存一条重复。"""

    class MediaCopy(DirectLive):
        def flash_cls(self, symbol="全部"):
            self.recorded._guard("flash_cls")
            return Rows([{"标题": "关于调整螺纹钢期货交易保证金的通知！",
                          "内容": "财联社10月11日电，上期所发布通知，调整螺纹钢期货相关合约交易保证金，自即日起执行。",
                          "发布日期": date(2026, 10, 11), "发布时间": time(10, 0)}])

        def notice_shfe(self):
            self.recorded._guard("notice_shfe")
            return Rows([{"标题": "关于调整螺纹钢期货交易保证金的通知", "内容": "关于调整螺纹钢期货交易保证金的通知",
                          "发布时间": datetime(2026, 10, 11, 0, 0, tzinfo=live.CHINA),
                          "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/t3.html"}])

    connection = sqlite3.connect(tmp_path / "title.sqlite3", check_same_thread=False)
    svc = live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                   sources=MediaCopy(), clock=lambda: 1000.0, now=lambda: NOW,
                                   rsshub_url=lambda: "http://rsshub.local")
    outcome = svc.poll_flash_once()
    rebar_items = [item for item in svc.flash_feed()["items"] if "交易保证金" in item["text"]]
    assert len(rebar_items) == 1 and outcome["mergedSources"] >= 1  # 合成一条，不是各存一份
    merged = rebar_items[0]
    assert merged["source"] == "上期所" and merged["sources"] == ["上期所", "财联社"]
    assert merged["tags"] == ["交易所"] and merged["products"] == ["rb"]
    assert merged["text"] == "关于调整螺纹钢期货交易保证金的通知"  # 更早的官方副本胜出


def test_a_title_merge_files_the_body_key_for_later_copies(tmp_path):
    """官方通知先入库，同轮又来两份共用正文、标题各异的转载：第一份按标题并入后，它的正文键
    也要登记——第二份（标题对不上）凭同一份正文仍能并进来，不另存一条。"""

    class TwoCopies(DirectLive):
        def flash_cls(self, symbol="全部"):
            self.recorded._guard("flash_cls")
            body = "财联社10月11日电，上期所发布通知，调整铜期货相关合约交易保证金，自即日起执行。"
            return Rows([
                {"标题": "上期所调整铜期货交易保证金", "内容": body,
                 "发布日期": date(2026, 10, 11), "发布时间": time(10, 0)},
                {"标题": "铜期货保证金要变了", "内容": body,
                 "发布日期": date(2026, 10, 11), "发布时间": time(10, 5)},
            ])

        def notice_shfe(self):
            self.recorded._guard("notice_shfe")
            return Rows([{"标题": "上期所调整铜期货交易保证金", "内容": "上期所调整铜期货交易保证金",
                          "发布时间": datetime(2026, 10, 11, 0, 0, tzinfo=live.CHINA),
                          "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/t4.html"}])

    connection = sqlite3.connect(tmp_path / "bodykey.sqlite3", check_same_thread=False)
    svc = live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                   sources=TwoCopies(), clock=lambda: 1000.0, now=lambda: NOW,
                                   rsshub_url=lambda: "http://rsshub.local")
    svc.poll_flash_once()
    copies = [item for item in svc.flash_feed()["items"] if "交易保证金" in item["text"]]
    assert len(copies) == 1  # 三份（通知+两转载）合成一条
    assert copies[0]["source"] == "上期所" and copies[0]["sources"] == ["上期所", "财联社"]


def test_a_filed_body_alias_survives_to_the_next_round(tmp_path):
    """标题合并登记的正文别名落库（`news_flash_alias`）跨轮生效：第一轮通知+第一份转载按标题
    并入，下一轮另一份同正文、标题对不上的转载凭登记的正文键仍并进来——不靠当轮内存。"""
    body = "财联社10月11日电，上期所发布通知，调整铜期货相关合约交易保证金，自即日起执行。"
    state = {"second": False}

    class SplitCopies(DirectLive):
        def flash_cls(self, symbol="全部"):
            self.recorded._guard("flash_cls")
            if not state["second"]:
                return Rows([{"标题": "上期所调整铜期货交易保证金", "内容": body,
                              "发布日期": date(2026, 10, 11), "发布时间": time(10, 0)}])
            return Rows([{"标题": "铜期货保证金调整落地", "内容": body,
                          "发布日期": date(2026, 10, 11), "发布时间": time(10, 30)}])

        def notice_shfe(self):
            self.recorded._guard("notice_shfe")
            if state["second"]:
                return Rows([])
            return Rows([{"标题": "上期所调整铜期货交易保证金", "内容": "上期所调整铜期货交易保证金",
                          "发布时间": datetime(2026, 10, 11, 0, 0, tzinfo=live.CHINA),
                          "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/t5.html"}])

    connection = sqlite3.connect(tmp_path / "alias.sqlite3", check_same_thread=False)
    svc = live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                   sources=SplitCopies(), clock=lambda: 1000.0, now=lambda: NOW,
                                   rsshub_url=lambda: "http://rsshub.local")
    svc.poll_flash_once()  # 第一轮：通知 + 标题相同的转载按标题并入，正文键登记进别名表
    first_round = [item for item in svc.flash_feed()["items"] if "交易保证金" in item["text"]]
    assert len(first_round) == 1 and first_round[0]["sources"] == ["上期所", "财联社"]
    assert connection.execute("SELECT COUNT(*) FROM news_flash_alias WHERE id = ?",
                               (first_round[0]["id"],)).fetchone()[0] == 1
    state["second"] = True
    svc._direct_due.clear()  # 直读源按各自间隔读：让这轮到期
    svc.poll_flash_once()  # 第二轮：同正文、标题对不上的另一份转载
    copies = [item for item in svc.flash_feed()["items"] if "交易保证金" in item["text"]]
    assert len(copies) == 1  # 凭落库的别名并进同一条，不是各存一份
    assert copies[0]["source"] == "上期所" and copies[0]["sources"] == ["上期所", "财联社"]


def test_a_displaced_body_stays_findable_when_the_notice_arrives_later(tmp_path):
    """媒体正文先入库、更早的官方 title-only 通知后到按标题并入：正文换成通知标题后，被换掉的
    正文键落库为别名——下一轮另一份同正文、标题对不上的转载凭它并进来，不另存一条。"""
    body = "财联社10月11日电，上期所发布通知，调整铜期货相关合约交易保证金，自即日起执行。"
    state = {"notice": False, "second": False}

    class LateNotice(DirectLive):
        def flash_cls(self, symbol="全部"):
            self.recorded._guard("flash_cls")
            if state["second"]:
                return Rows([{"标题": "铜期货保证金调整落地", "内容": body,
                              "发布日期": date(2026, 10, 11), "发布时间": time(11, 0)}])
            return Rows([{"标题": "上期所调整铜期货交易保证金", "内容": body,
                          "发布日期": date(2026, 10, 11), "发布时间": time(10, 0)}])

        def notice_shfe(self):
            self.recorded._guard("notice_shfe")
            if state["second"] or not state["notice"]:
                return Rows([])
            return Rows([{"标题": "上期所调整铜期货交易保证金", "内容": "上期所调整铜期货交易保证金",
                          "发布时间": datetime(2026, 10, 11, 0, 0, tzinfo=live.CHINA),
                          "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/t6.html"}])

    connection = sqlite3.connect(tmp_path / "displaced.sqlite3", check_same_thread=False)
    svc = live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                   sources=LateNotice(), clock=lambda: 1000.0, now=lambda: NOW,
                                   rsshub_url=lambda: "http://rsshub.local")
    svc.poll_flash_once()  # 第一轮：只有财联社的转载，正文 B 是这一条的正身
    state["notice"] = True
    svc._direct_due.clear()
    svc.poll_flash_once()  # 第二轮：更早的官方通知按标题并入，正文换成通知标题
    merged = [item for item in svc.flash_feed()["items"] if "交易保证金" in item["text"]]
    assert len(merged) == 1
    assert merged[0]["source"] == "上期所" and merged[0]["text"] == "上期所调整铜期货交易保证金"
    assert connection.execute("SELECT COUNT(*) FROM news_flash_alias WHERE key = ? AND id = ?",
                               (live.flash_key(body), merged[0]["id"])).fetchone()[0] == 1
    state["second"] = True
    svc._direct_due.clear()
    svc.poll_flash_once()  # 第三轮：同正文 B、标题对不上的另一份转载，凭落库的别名并进来
    copies = [item for item in svc.flash_feed()["items"] if "交易保证金" in item["text"]
              or "调整落地" in item["title"]]
    assert len(copies) == 1 and copies[0]["source"] == "上期所"


def test_a_stalled_direct_source_neither_blocks_nor_piles_up(tmp_path, monkeypatch):
    """一条直读源卡住：别的来源照常答，卡住的这条在它真正结束前不再被排新一轮（与 RSSHub
    路由同型的在飞守卫）。可控阻塞桩 + 缩短的超时，单用例 ≤2 秒。"""
    monkeypatch.setattr(live, "FLASH_SOURCE_TIMEOUT_SECONDS", 0.15)
    release = threading.Event()
    calls: list[str] = []

    class OneStalled(DirectLive):
        def notice_shfe(self):
            calls.append("shfe")
            assert release.wait(2)  # 卡到主线程放行（比 0.15 秒超时长）
            return Rows([])

    svc = live.ResearchLiveService(connection=sqlite3.connect(tmp_path / "stall-direct.sqlite3", check_same_thread=False),
                                   lock=threading.Lock(), state_root=tmp_path, sources=OneStalled(),
                                   clock=lambda: 1000.0, now=lambda: NOW, rsshub_url=lambda: "http://rsshub.local")
    first = svc.poll_flash_once()
    assert "FinancialJuice" in first["collected"]  # 别的直读源照常答，没被卡住的拖死
    assert "上期所" not in first["collected"]  # 卡住的这条超时了
    svc.poll_flash_once()  # 超时把到期重置了，但在飞守卫挡住：不再排它，线程不堆积
    assert calls.count("shfe") == 1
    release.set()  # 卡住的请求真正结束（丢弃结果），下一轮可以再排它
    for _ in range(100):  # 等被卡的线程真正退出（≤2s 的 20ms 轮询）
        if "上期所" not in svc._direct_busy:
            break
        sleep(0.02)
    third = svc.poll_flash_once()
    assert calls.count("shfe") == 2
    assert "上期所" in third["collected"]  # 这次答上了


# -- retention: official notices keep a week outside the media row cap (2026-10-11 回读交易所条目为 0)

def _fill_media(svc, count: int, start: datetime, prefix: str = "m") -> None:
    """`count` media rows one minute apart from `start` (all newer than the notices under test)."""
    with svc.lock, svc.connection:
        svc.connection.executemany(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES (?, '新浪', '[\"新浪\"]', ?, '快讯', ?, NULL, '[]', '[]')",
            [(f"{prefix}{i}", (start + timedelta(minutes=i)).isoformat(timespec="seconds"), f"媒体快讯正文{prefix}{i}")
             for i in range(count)])


def _official(svc, item_id: str, source: str, published: datetime, sources: list[str] | None = None) -> None:
    with svc.lock, svc.connection:
        svc.connection.execute(
            "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link, products_json, tags_json)"
            " VALUES (?, ?, ?, ?, '通知', ?, NULL, '[]', '[]')",
            (item_id, source, json.dumps(sources or [source], ensure_ascii=False),
             published.isoformat(timespec="seconds"), f"官方通知正文{item_id}"))


def _ids(svc) -> set[str]:
    with svc.lock:
        return {row[0] for row in svc.connection.execute("SELECT id FROM news_flash")}


def test_a_full_media_table_does_not_push_out_an_exchange_notice(tmp_path):
    """3000 条更新的媒体快讯塞满后，3 天前的上期所通知仍在库里，并能按“交易所”和品种读到。"""

    class OlderNotices(DirectLive):
        def notice_shfe(self):
            self.recorded._guard("notice_shfe")
            return Rows([
                {"标题": "关于调整螺纹钢期货交易保证金的通知", "内容": "关于调整螺纹钢期货交易保证金的通知",
                 "发布时间": datetime(2026, 10, 1, 0, 0, tzinfo=live.CHINA),
                 "链接": "https://www.shfe.com.cn/publicnotice/notice/202610/t9.html"},
                # 5 天前：媒体的 3 天窗口外、官方的 7 天窗口内，照样入库。
                {"标题": "关于铜期货交割有关事项的通知", "内容": "关于铜期货交割有关事项的通知",
                 "发布时间": datetime(2026, 9, 28, 0, 0, tzinfo=live.CHINA),
                 "链接": "https://www.shfe.com.cn/publicnotice/notice/202609/t8.html"}])

    connection = sqlite3.connect(tmp_path / "keep.sqlite3", check_same_thread=False)
    svc = live.ResearchLiveService(connection=connection, lock=threading.Lock(), state_root=tmp_path,
                                   sources=OlderNotices(), clock=lambda: 1000.0, now=lambda: NOW,
                                   rsshub_url=lambda: "http://rsshub.local")
    svc.poll_flash_once()
    _fill_media(svc, live.FLASH_MAX_ITEMS, datetime(2026, 10, 1, 1, 0, tzinfo=live.CHINA))
    svc._prune()
    with svc.lock:
        notices = dict(svc.connection.execute(
            "SELECT title, published_at FROM news_flash WHERE source = '上期所'").fetchall())
        media = svc.connection.execute(
            f"SELECT COUNT(*) FROM news_flash WHERE source NOT IN ({', '.join('?' * len(live.OFFICIAL_SOURCE_NAMES))})",
            live.OFFICIAL_SOURCE_NAMES).fetchone()[0]
    assert notices == {"关于调整螺纹钢期货交易保证金的通知": "2026-10-01T00:00:00+08:00",
                       "关于铜期货交割有关事项的通知": "2026-09-28T00:00:00+08:00"}
    assert media == live.FLASH_MAX_ITEMS  # 媒体照旧按行数上限，官方不占名额
    # 读取一侧：72 小时窗口里有 3000 多行，按标签、按品种都不能被行数上限截掉。
    exchange = svc.flash_feed(tag="交易所", hours=72, limit=50)["items"]
    shfe = [item["title"] for item in exchange if item["source"] == "上期所"]
    assert shfe == ["关于调整螺纹钢期货交易保证金的通知"]  # 5 天前的在 72 小时窗口外，不扩大窗口
    rebar = svc.flash_feed(product="SHFE.rb", hours=72, limit=50)["items"]
    assert any(item["title"] == "关于调整螺纹钢期货交易保证金的通知" for item in rebar)


def test_official_rows_keep_thirty_days_and_media_rows_keep_three_days_and_their_cap(tmp_path, monkeypatch):
    """官方条目按时间单独保留（默认 30 天，可配置），快讯量大时也不被媒体的行数上限挤掉；媒体照旧 3 天与行数上限。"""
    svc = cloud_service(tmp_path)
    beijing = NOW.astimezone(live.CHINA)
    _official(svc, "stats-31d", "国家统计局", beijing - timedelta(days=31))
    _official(svc, "stats-29d", "国家统计局", beijing - timedelta(days=29))
    _official(svc, "cffex-6d", "中金所", beijing - timedelta(days=6, hours=12))
    _official(svc, "czce-20d", "郑商所", beijing - timedelta(days=20))
    _official(svc, "media-4d", "新浪", beijing - timedelta(days=4))
    _fill_media(svc, live.FLASH_MAX_ITEMS + 1, beijing - timedelta(days=2, hours=12))
    svc._prune()
    kept = _ids(svc)
    assert "stats-31d" not in kept  # 官方也只留 30 天
    assert {"stats-29d", "cffex-6d", "czce-20d"} <= kept
    assert "media-4d" not in kept  # 媒体照旧 3 天
    assert "m0" not in kept and "m1" in kept  # 媒体照旧只留最新 FLASH_MAX_ITEMS 行
    assert len(kept) == live.FLASH_MAX_ITEMS + 3
    # 30 天里的官方条目按 official 读回（不经品种筛选、不受 72 小时限制）；普通读取仍最多 72 小时。
    official = svc.flash_feed(hours=24 * 30, limit=50, official=True)
    assert official["official"] is True and official["hours"] == 720
    assert {item["id"] for item in official["items"]} == {"stats-29d", "cffex-6d", "czce-20d"}
    # 带 product 也不按品种筛（多数公告没有品种标签，#853）。
    narrowed = svc.flash_feed(product="SHFE.rb2701", products=["DCE.i"], hours=24 * 30, limit=50, official=True)
    assert {item["id"] for item in narrowed["items"]} == {"stats-29d", "cffex-6d", "czce-20d"}
    assert "screen" not in narrowed and "brief" not in narrowed
    assert svc.flash_feed(hours=24 * 30)["hours"] == 72
    # 保留天数可配置：改成 7 天后 20 天前的郑商所条目下一次清理就删。
    monkeypatch.setenv("AI_TRADER_FLASH_OFFICIAL_RETAIN_DAYS", "7")
    svc._prune()
    assert "czce-20d" not in _ids(svc) and "cffex-6d" in _ids(svc)


@pytest.mark.parametrize("days", [1, 2, 3, 7, 30, 365])
def test_each_kind_is_pruned_by_its_own_age_whatever_the_official_retention(tmp_path, monkeypatch, days):
    """官方保留天数配成 1–2 天也只删官方条目：仍在 3 天窗口里的媒体条目（含只带“据:”的）不受影响（#855）。
    两类都在恰好截止时刻保留、超界 1 秒删除。"""
    monkeypatch.setenv("AI_TRADER_FLASH_OFFICIAL_RETAIN_DAYS", str(days))
    svc = cloud_service(tmp_path)
    beijing = NOW.astimezone(live.CHINA).replace(microsecond=0)
    official_edge = beijing - timedelta(days=days)
    media_edge = beijing - timedelta(hours=live.FLASH_MEDIA_RETAIN_HOURS)
    _official(svc, "official-edge", "上期所", official_edge)
    _official(svc, "official-past", "上期所", official_edge - timedelta(seconds=1))
    _official(svc, "merged-past", "上期所", official_edge - timedelta(seconds=1), sources=["上期所", "新浪"])
    _official(svc, "media-48h", "新浪", beijing - timedelta(hours=48))
    _official(svc, "media-edge", "新浪", media_edge)
    _official(svc, "media-past", "新浪", media_edge - timedelta(seconds=1))
    svc._prune()
    assert _ids(svc) == {"official-edge", "media-48h", "media-edge"}


def test_media_rows_are_cut_by_the_configured_hours_first_and_the_row_cap_backs_it_up(tmp_path, monkeypatch):
    """媒体条目先按 `AI_TRADER_FLASH_MEDIA_RETAIN_HOURS`（默认 72 小时）删，再按行数上限兜底；官方条目两者都不受影响。"""
    monkeypatch.setattr(live, "FLASH_MAX_ITEMS", 5)
    svc = cloud_service(tmp_path)
    beijing = NOW.astimezone(live.CHINA).replace(microsecond=0)
    _official(svc, "official-10d", "上期所", beijing - timedelta(days=10))
    _official(svc, "merged-10d", "中金所", beijing - timedelta(days=10), sources=["中金所", "新浪"])
    _official(svc, "media-71h", "新浪", beijing - timedelta(hours=71))
    _official(svc, "media-73h", "新浪", beijing - timedelta(hours=73))
    svc._prune()
    assert _ids(svc) == {"official-10d", "merged-10d", "media-71h"}  # 默认 72 小时：73 小时的媒体行删，官方行留
    # 缩短到 24 小时：71 小时的媒体行也删；官方行照旧。
    monkeypatch.setenv("AI_TRADER_FLASH_MEDIA_RETAIN_HOURS", "24")
    _fill_media(svc, 8, beijing - timedelta(hours=10))
    svc._prune()
    kept = _ids(svc)
    assert "media-71h" not in kept and {"official-10d", "merged-10d"} <= kept
    # 时间之内的媒体行超过行数上限时只留最新的 5 行（官方行不占名额）。
    assert {i for i in kept if i.startswith("m") and i[1:].isdigit()} == {f"m{i}" for i in range(3, 8)}
    assert len(kept) == 5 + 2


def test_official_rows_have_a_row_cap_of_their_own(tmp_path, monkeypatch):
    """官方条目不占媒体的 3000 行，但有自己的上限：异常或伪造的来源刷不大这张表。"""
    monkeypatch.setattr(live, "official_max_items", lambda: 3)
    svc = cloud_service(tmp_path)
    beijing = NOW.astimezone(live.CHINA)
    for day in range(5):
        _official(svc, f"gfex-{day}", "广期所", beijing - timedelta(days=day, hours=1))
    _fill_media(svc, 5, beijing - timedelta(days=1))
    svc._prune()
    kept = _ids(svc)
    assert {"gfex-0", "gfex-1", "gfex-2"} <= kept and not {"gfex-3", "gfex-4"} & kept
    assert {f"m{i}" for i in range(5)} <= kept  # 媒体名额不受影响


def test_a_syndicated_copy_merged_into_a_notice_stays_with_it_with_its_reading(tmp_path):
    """媒体转载按标题并进 5 天前的上期所通知：这一行（连同它的译文与 AI 筛选）随官方条目保留；
    被挤掉的媒体条目的译文与筛选跟着删。"""

    class MergedCopy(DirectLive):
        def flash_cls(self, symbol="全部"):
            self.recorded._guard("flash_cls")
            return Rows([{"标题": "关于调整铜期货交易保证金的通知！",
                          "内容": "财联社10月1日电，上期所发布通知，调整铜期货相关合约交易保证金，自即日起执行。",
                          "发布日期": date(2026, 10, 1), "发布时间": time(10, 0)}])

        def notice_shfe(self):
            self.recorded._guard("notice_shfe")
            return Rows([{"标题": "关于调整铜期货交易保证金的通知", "内容": "关于调整铜期货交易保证金的通知",
                          "发布时间": datetime(2026, 9, 28, 0, 0, tzinfo=live.CHINA),
                          "链接": "https://www.shfe.com.cn/publicnotice/notice/202609/t7.html"}])

    connection = sqlite3.connect(tmp_path / "merged.sqlite3", check_same_thread=False)
    lock = threading.Lock()
    svc = live.ResearchLiveService(connection=connection, lock=lock, state_root=tmp_path,
                                   sources=MergedCopy(), clock=lambda: 1000.0, now=lambda: NOW,
                                   rsshub_url=lambda: "http://rsshub.local",
                                   screener=NewsScreener(connection=connection, lock=lock, now=lambda: NOW),
                                   translator=NewsTranslator(connection=connection, lock=lock, now=lambda: NOW))
    svc.poll_flash_once()
    with svc.lock:
        merged = svc.connection.execute(
            "SELECT id, sources_json FROM news_flash WHERE title = '关于调整铜期货交易保证金的通知'").fetchall()
    assert len(merged) == 1 and json.loads(merged[0][1]) == ["上期所", "财联社"]
    notice_id = merged[0][0]
    _fill_media(svc, live.FLASH_MAX_ITEMS + 1, datetime(2026, 10, 1, 1, 0, tzinfo=live.CHINA))
    judged = (NOW - timedelta(days=5)).isoformat(timespec="seconds")  # 判断早于筛选自己的 4 天窗口
    with svc.lock, svc.connection:
        for item_id in (notice_id, "m0"):
            svc.connection.execute("INSERT INTO news_screen (item_id, product, relevant, judged_at) VALUES (?, 'cu', 1, ?)",
                                   (item_id, judged))
            svc.connection.execute("INSERT INTO news_translation (item_id, title_zh, translated_at) VALUES (?, '译', ?)",
                                   (item_id, judged))
    svc._prune()
    assert notice_id in _ids(svc) and "m0" not in _ids(svc)
    with svc.lock:
        screened = {row[0] for row in svc.connection.execute("SELECT item_id FROM news_screen")}
        translated = {row[0] for row in svc.connection.execute("SELECT item_id FROM news_translation")}
    assert screened == {notice_id} and translated == {notice_id}
