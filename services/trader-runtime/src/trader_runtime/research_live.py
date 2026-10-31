"""Live market intel that the research desk and the AI read, but never the trading paths:
the cross-source flash feed (exchanges and ministries first — 中金所 / 上期所 / 郑商所 / 大商所 / 广期所
notices, gov data and policy releases — then 财联社 / 东方财富 / 新浪 / 同花顺 / 富途 / 上海金属网 by metal,
the industry wires 上海有色网 / 我的钢铁网, the English wire FinancialJuice, the Chinese financial media
证券时报 / 汇通 / 财新 / 澎湃 / 每经 / 界面 (futures-related items only), 新闻联播's transcript and 金十 /
华尔街见闻 / 第一财经 / 隆众资讯 — everything past the AKShare six only when `RSSHUB_URL` names the
self-hosted RSSHub, i.e. on the cloud Runtime). A report naming its
original source (据新华社、国家统计局数据显示…) carries it as a `据:` tag (news_origin) and meets that
official source's own item when it retells that one item word for word about the same products,
the economic calendar, bond yields / SHIBOR / LPR and the hog spot price (foreign quotes are read from
foreign_market, the one foreign entry).

A product's view of the feed is recalled by its words (aliases + 产业链词表, minus 排除词) and
screened by the model (news_screen): kept items carry a one-line reading, the rest fold into
counted groups. Without the model the keyword recall stands, marked as not AI-screened.

Everything AKShare fails on its own: the flash poller runs the sources concurrently
with a per-source timeout (a hung site loses its thread, never the poll), one source
failing keeps the others, and a process without akshare never starts the loops and answers
`research_dependency_missing` instead of crashing. User requests never fan out to the
sources directly — they read the flash table, the background caches or a single-flight
shared read (rates, hog: collection.Collector) with its own TTL.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from datetime import time as time_cls
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

import httpx

from . import fast_json
from .collection import Collector
from .foreign_market import ForeignMarketService
from .host_throttle import EASTMONEY, GENTLE
from .instruments import FOREIGN_PRODUCTS, foreign_product_of
from .last_good import LastGood
from .news_analyst import NewsAnalyst
from .news_origin import ORIGIN_SOURCE, ORIGIN_TAG, origins_of, retells
from .news_screen import NewsScreener, build_events, layer_summary, screen_summary, screen_view
from .news_translate import NewsTranslator, is_english_title
from .research_data import CHINA, _number, _records, _text
from .tls import shared_ssl_context

log = logging.getLogger("uvicorn.error.research")

FLASH_POLL_SECONDS = 60.0
FLASH_SOURCE_TIMEOUT_SECONDS = 15.0
# How long stop() (the lease moving away) waits for a round's worker thread to end before the lease is
# released anyway: within docker stop's 10 s window together with uvicorn's 5 s graceful shutdown.
STOP_WAIT_SECONDS = 3.0
# 媒体条目的保留时间（小时，`AI_TRADER_FLASH_MEDIA_RETAIN_HOURS`，1–720）：默认 72 小时（3 天）。
FLASH_MEDIA_RETAIN_HOURS = 72
# 入库时间上限（#754，PR #752 审查）：发布时间晚于当前一天以上的条目不收——广期所公告页是明文 HTTP，伪造或写错
# 年份的远未来条目会按时间排在最前，长期占住官方或媒体的行数名额、把真实条目挤掉。
FLASH_FUTURE_LIMIT = timedelta(days=1)
# 媒体条目的行数上限：只是时间截止之外的保险（2026-10-15 所有者：由 4000 提到 12000——降噪后日盘每小时仍进
# 320–500 条，4000 行只覆盖约 16 小时，上限在 72 小时之前就先把条目挤掉）。
FLASH_MAX_ITEMS = 12000
# 官方来源（交易所通知、部委发布）多按日期发布、时间偏早：快讯量大时（当时上限 3000 行）约一天就满，它们一入库就被
# 按行数挤掉（2026-10-11 回读交易所条目为 0）。官方条目——含被合并进来的媒体转载——按时间单独保留
# （默认 30 天，`AI_TRADER_FLASH_OFFICIAL_RETAIN_DAYS`，1–365），不占 FLASH_MAX_ITEMS 的名额；媒体条目照旧
# `media_retain_hours()`（默认 72 小时）、FLASH_MAX_ITEMS（12000）行（2026-10-13 所有者：官方源单独保留 30 天）。
FLASH_OFFICIAL_RETAIN_DAYS = 30
# 官方条目自己的行数上限（`AI_TRADER_FLASH_OFFICIAL_MAX_ITEMS`，默认 3000；正常每天约 40 条，30 天约 1200）：
# 表始终有界，伪造或异常的来源也撑不大它。
FLASH_OFFICIAL_MAX_ITEMS = 3000
# 官方条目可按时间读回的最长窗口（小时）：快讯接口与 AI 工具只看官方条目时放宽到这里，其余仍最多 72 小时。
FLASH_OFFICIAL_HOURS_MAX = 24 * 365


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return min(max(value, low), high)


def media_retain_hours() -> int:
    return _env_int("AI_TRADER_FLASH_MEDIA_RETAIN_HOURS", FLASH_MEDIA_RETAIN_HOURS, 1, 720)


def official_retain_days() -> int:
    return _env_int("AI_TRADER_FLASH_OFFICIAL_RETAIN_DAYS", FLASH_OFFICIAL_RETAIN_DAYS, 1, 365)


def official_max_items() -> int:
    return _env_int("AI_TRADER_FLASH_OFFICIAL_MAX_ITEMS", FLASH_OFFICIAL_MAX_ITEMS, 100, 20000)
FLASH_TTL_INDEX_SECONDS = 10 * 60
# AKShare sources, in the canonical merge order (a syndicated story is filed under the first).
FLASH_SOURCES = ("财联社", "东方财富", "新浪", "同花顺", "富途", "上海金属网")
# The self-hosted RSSHub (deploy/vps compose service `rsshub`, internal network only): (source,
# route, poll seconds). Routes checked 2026-10-07 on diygod/rsshub:2026-10-06. 隆众资讯's bodies are
# behind its paywall: titles and links only. 期货业协会分析师园地 (/cfachina/...) is not read — the
# association stopped updating it in 2024.
RSSHUB_SOURCES: tuple[tuple[str, str, float], ...] = (
    # 官方：交易所通知与部委发布（2026-10-11 在 NAS diygod/rsshub:2026-10-06 镜像上逐路实测 200）。
    # 海关总署 /gov/customs、人民银行 /gov/pbc 各路由 503 或停更（最新 2022），未接。
    ("中金所", "/cffex/announcement", 600.0),
    ("国家统计局", "/gov/stats", 1800.0),
    ("粮储局", "/gov/lswz", 1800.0),
    ("能源局", "/gov/nea/sjzz/mts", 1800.0),  # 煤炭司
    ("能源局", "/gov/nea/sjzz/yqs", 1800.0),  # 油气司
    ("发改委", "/gov/ndrc/xwdt", 1800.0),  # 新闻发布
    ("发改委", "/gov/ndrc/fggz", 1800.0),  # 政策文件
    ("住建部", "/gov/mohurd/policy", 1800.0),
    ("国务院政策", "/gov/zhengce/govall", 1800.0),
    ("矿山安监局", "/gov/chinamine-safety/xw", 1800.0),
    # 媒体。
    ("金十", "/jin10", 60.0),
    ("华尔街见闻", "/wallstreetcn/live/global", 60.0),
    ("第一财经", "/yicai/brief", 120.0),
    ("隆众资讯", "/oilchem/chem", 600.0),
    ("隆众资讯", "/oilchem/oil", 600.0),
    # 第 3 优先级中文财经媒体（2026-10-13 在 NAS 与腾讯 Runtime 容器内实测）：财新最新文章、界面快报。
    ("财新", "/caixin/latest", 600.0),
    ("界面", "/jiemian/lists/4", 300.0),
)
RSSHUB_TIMEOUT_SECONDS = 15.0
# 东方财富期货频道“交易所公告”栏目的读取名（不是来源名：条目按交易所归名，见 SPLIT_SOURCES）。
EXCHANGE_BOARD = "交易所公告（东方财富）"
RSSHUB_LIMIT = 50
# 官方源（交易所通知、部委与数据发布）排在媒体源前面：同一事件多家转载时归到官方源。
# 郑商所/大商所官网用 WAF 拦脚本访问（腾讯 Runtime 容器与 NAS 内 GET 均 412，RSSHub 无路由），它们的公告
# 改从东方财富期货频道的“交易所公告”栏目读（交易所原文转载，带发布时间与摘要，2026-10-13 起）。
# 农业农村部、应急管理部、海关总署、人民银行的 RSSHub 路由不可用（见 PR #742）。
EXCHANGE_SOURCES = frozenset({"中金所", "上期所", "郑商所", "大商所", "广期所"})
OFFICIAL_SOURCES = ("中金所", "上期所", "郑商所", "大商所", "广期所", "国家统计局", "粮储局", "能源局", "发改委",
                    "住建部", "国务院政策", "矿山安监局")
# 官方条目：来源里有任一官方源（交易所含尚未接入的郑商所、大商所）。合并进来的媒体转载与官方条目是同一行，
# 随官方条目保留。
OFFICIAL_SOURCE_NAMES = tuple(sorted({*OFFICIAL_SOURCES, *EXCHANGE_SOURCES}))
_OFFICIAL_ROW = ("EXISTS (SELECT 1 FROM json_each(news_flash.sources_json) WHERE value IN ("
                 + ", ".join("?" * len(OFFICIAL_SOURCE_NAMES)) + "))")
# 官方与行业直读源（(源名, 轮询秒数)）：不经过 RSSHub，解析器只取标题、链接、发布时间（快讯类连同列表页
# 自带的正文）。与 RSSHub 源一样只在配置了 RSSHUB_URL 的云端 Runtime 启用（本机版不抓快讯）；上期所与
# 广期所 2026-10-11、其余 2026-10-13 自腾讯 Runtime 容器实测可达。
# 证券时报 / 汇通 / 澎湃 / 每经：RSSHub 有路由但不可用（证券时报无发布时间；汇通逐条抓详情页，一条缺字段整路
# 503；澎湃、每经自 NAS 出口 503 / ECONNRESET），改直读列表页。新闻联播文字稿走 AKShare news_cctv。
DIRECT_SOURCES: tuple[tuple[str, float], ...] = (
    ("上期所", 600.0),
    ("广期所", 600.0),
    (EXCHANGE_BOARD, 600.0),
    ("上海有色网", 600.0),
    ("我的钢铁网", 600.0),
    ("FinancialJuice", 120.0),
    ("证券时报", 300.0),
    ("汇通", 300.0),
    ("澎湃", 600.0),
    ("每经", 600.0),
    ("新闻联播", 3600.0),
)
DIRECT_SOURCE_NAMES = frozenset(source for source, _ in DIRECT_SOURCES)
# 一次读取产出几家来源的条目（行带 `_source`）：东方财富期货频道“交易所公告”栏目里的郑商所、大商所公告。
SPLIT_SOURCES: dict[str, tuple[str, ...]] = {EXCHANGE_BOARD: ("郑商所", "大商所")}
# 综合财经媒体：大部分条目与期货无关（个股公告、消费、科技…），只留打上品种或宏观标签的条目，不挤占
# 媒体条目的 3000 行。
TAGGED_ONLY_SOURCES = frozenset({"证券时报", "汇通", "财新", "澎湃", "每经", "界面"})
# 按来源的标题排除规则：成批的单厂报价不是新闻，不进快讯库（也就不进 AI 筛选、不占媒体行数）。
# 隆众资讯日盘每小时约 200 条“[品名]：某厂某品价格快讯 / 暂不报价 / 价格上调”，2026-10-14 当天 726 条里约 560 条是
# 这种报价，被别名表打上原油、橡胶、甲醇等品种后挤满 AI 筛选队列。每个来源一行：(排除的标题, 仍保留的标题)——
# 讲整个市场的报价（“亚洲 PTA 市场价格快讯”“国际原油价格上涨”“张家港市场乙二醇价格行情”“国内汽柴油价格上调”）照收；早间提示、
# 装置动态、停工检修、评述与价格汇总不在排除之列。新浪、富途同日抽样没有同类成批报价，不设规则。
SOURCE_TITLE_EXCLUDES: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    "隆众资讯": (re.compile(r"(价格(快讯|快报|行情|上调|下调|上涨|下跌|动态|停报)|暂(不|停|无)报价|暂无新价"
                        r"|最新报盘|报价快讯|竞拍快讯|产销动态)$"),
             # 国内 / 全国：整体调价（“[成品油]：国内汽柴油价格上调”）是全市场的消息，不是单厂报价。
             re.compile(r"市场|国际|国内|全国|亚洲|欧洲|美国|东北亚|东南亚|中东|期货")),
}
_TRAILING_NOTE = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")


def source_excluded(source: str, title: str, text: str = "") -> bool:
    """Whether the source's title rule leaves this item out of the feed: a quote title that is not about a whole
    market (a trailing （日期） is ignored)."""
    rule = SOURCE_TITLE_EXCLUDES.get(source)
    if rule is None:
        return False
    excluded, kept = rule
    line = _TRAILING_NOTE.sub("", (title or text or "").strip())
    return bool(excluded.search(line)) and not kept.search(line)


FLASH_NOTICE = "快讯跨源去重后按时间排列；第三方信息，本软件未核实。"
# 上海金属网 by metal (AKShare futures_news_shmet symbol → products it speaks for). One call returns
# about 2,000 rows, so the site is read gently: one metal every SHMET_EVERY_SECONDS, in turn — each
# metal about every 16 minutes (metal news is not minute-critical).
SHMET_SYMBOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("铜", ("cu", "bc")), ("铝", ("al",)), ("锌", ("zn",)), ("镍", ("ni",)), ("铅", ("pb",)),
    ("锡", ("sn",)), ("贵金属", ("au", "ag")), ("小金属", ()),
)
SHMET_PER_ROUND = 1
SHMET_EVERY_SECONDS = 120.0
# A product's own view offers it at most this many of the newest macro-tagged items to judge.
MACRO_CANDIDATES = 80
# An event id is looked up among judgments of the 快讯 window (24 h) plus a margin for items published at its edge.
EVENT_LOOKUP_HOURS = 26
RATES_TTL_SECONDS = 30 * 60
RATES_TIMEOUT_SECONDS = 25.0
HOG_TTL_SECONDS = 30 * 60
HOG_TIMEOUT_SECONDS = 15.0
# The rates' series by the source that answers them (a partial read names the failed source, #516).
RATE_SERIES: dict[str, tuple[str, ...]] = {"bond": ("china10Y", "us10Y", "us10Y2Y"),
                                           "shibor": ("shiborOvernight", "shibor1Week")}
# A failed, timed-out or empty rates / hog read keeps the last good value (stale) and is retried after this.
LIVE_RETRY_SECONDS = 2 * 60
CALENDAR_TTL_SECONDS = 6 * 60 * 60
CALENDAR_RETRY_SECONDS = 10 * 60
CALENDAR_DAYS = 7
CALENDAR_DAY_TIMEOUT_SECONDS = 15.0  # one request per day, each on its own: a slow day skips only itself
CALENDAR_REGIONS = ("中国", "美国", "欧元区")
CALENDAR_MIN_IMPORTANCE = 2
LPR_TTL_SECONDS = 24 * 60 * 60
LPR_RETRY_SECONDS = 60 * 60
LPR_TIMEOUT_SECONDS = 90.0
ALIASES_FILENAME = "research_flash_aliases.json"


# 品种别名（与合约目录里的品种名合并使用）。用户可在 Runtime 状态目录的
# research_flash_aliases.json 里改写：同名键整组替换，其余沿用这里的默认。
FLASH_ALIASES: dict[str, list[str]] = {
    "cu": ["铜", "沪铜", "伦铜", "LME铜", "COMEX铜", "铜价"],
    "al": ["铝", "沪铝", "伦铝", "LME铝", "电解铝", "氧化铝产能"],
    "zn": ["锌", "沪锌", "伦锌", "LME锌"],
    # 铅/锡不用单字别名：无锡、锡林郭勒、铅笔会把地名和日用品算进品种（铜的单字保留，靠排除词表兜底）。
    "pb": ["沪铅", "伦铅", "铅价", "铅锭", "铅酸", "LME铅"],
    "ni": ["镍", "沪镍", "伦镍", "LME镍", "镍豆"],
    "sn": ["沪锡", "伦锡", "锡价", "锡锭", "LME锡"],
    "au": ["黄金", "金价", "沪金", "伦敦金", "COMEX黄金", "金价的"],
    "ag": ["白银", "银价", "沪银", "伦敦银", "COMEX白银"],
    "ao": ["氧化铝"],
    "ad": ["铸造铝合金", "铝合金"],
    "rb": ["螺纹", "螺纹钢", "钢材", "钢铁", "粗钢", "钢厂"],
    "hc": ["热轧", "热卷", "卷板", "板材"],
    "wr": ["线材"],
    "ss": ["不锈钢"],
    "sf": ["硅铁"], "sm": ["锰硅"], "si": ["工业硅", "多晶硅料"], "ps": ["多晶硅"],
    "i": ["铁矿石", "铁矿", "矿石", "PB粉", "发运量"],
    "j": ["焦炭"], "jm": ["焦煤", "煤炭"], "zc": ["动力煤", "煤炭"],
    "sc": ["原油", "油价", "布伦特", "WTI", "OPEC", "石油"],
    "fu": ["燃料油", "高硫燃料油"], "lu": ["低硫燃料油"], "bu": ["沥青", "石油沥青"],
    "pg": ["液化石油气", "LPG", "丙烷"], "ta": ["PTA", "PX"], "eg": ["乙二醇"],
    "eb": ["苯乙烯"], "bz": ["纯苯"], "l": ["塑料", "聚乙烯", "LLDPE"], "pp": ["聚丙烯", "PP"],
    "v": ["PVC"], "ma": ["甲醇"], "ur": ["尿素"], "sa": ["纯碱"], "fg": ["玻璃"],
    "ru": ["橡胶", "天然橡胶", "沪胶"], "nr": ["20号胶"], "br": ["丁二烯橡胶", "顺丁胶"],
    "sp": ["纸浆"], "op": ["胶版印刷纸"], "lg": ["原木"], "fb": ["纤维板"], "bb": ["胶合板"],
    "c": ["玉米", "玉米淀粉"], "cs": ["玉米淀粉"], "m": ["豆粕", "大豆", "美豆", "豆粕的"],
    "a": ["大豆", "豆一", "美豆"], "b": ["大豆", "豆二", "美豆"], "y": ["豆油", "美豆油"],
    "p": ["棕榈油", "马棕油", "棕榈"], "oi": ["菜油", "菜籽油"], "rm": ["菜粕", "菜籽粕"],
    "rs": ["菜籽"], "wh": ["小麦"], "pm": ["普麦"], "ri": ["籼稻"], "jr": ["粳稻"],
    "rr": ["粳米"], "sr": ["白糖", "原糖", "糖价"], "cf": ["棉花", "美棉", "棉价"],
    "cy": ["棉纱"], "ap": ["苹果"], "cj": ["红枣"], "pk": ["花生"],
    "lh": ["生猪", "猪价", "外三元", "生猪的"], "jd": ["鸡蛋", "蛋价"],
    "lc": ["碳酸锂"], "ec": ["集运指数", "欧线", "运价"], "pt": ["铂金", "铂价"], "pd": ["钯金"],
    "pr": ["瓶片"], "pl": ["瓶片"], "sh": ["烧碱"], "if": ["沪深300"], "ih": ["上证50"],
    "ic": ["中证500"], "im": ["中证1000"], "t": ["国债"], "tf": ["国债"], "ts": ["国债"], "tl": ["国债"],
    # 外盘品种自己的快讯键（2026-10-21，instruments.ForeignProduct.news；没有国内对照的外盘品种用）：
    # 键用整词，避开国内品种代码（CBOT.ZN 是美债，国内 zn 是锌）。第一个词是筛选时给 AI 的品种名。
    "spx": ["标普500", "标普", "美股", "美股三大股指"], "nasdaq": ["纳指", "纳斯达克", "纳斯达克100", "美国科技股", "美股科技股", "美股"],
    "dji": ["道指", "道琼斯", "美股三大股指"], "russell": ["罗素2000", "罗素", "美国小盘股"],
    "eur": ["欧元", "欧元兑美元", "欧洲央行", "欧央行"], "jpy": ["日元", "美元兑日元", "日本央行"],
    "gbp": ["英镑", "英国央行", "英格兰银行"], "aud": ["澳元", "澳洲联储", "澳大利亚央行"], "cad": ["加元", "加拿大央行"],
    "chf": ["瑞郎", "瑞士法郎", "瑞士央行"], "nzd": ["纽元", "新西兰联储"],
    "ust": ["美债", "美国国债", "美债收益率", "10年期美债"],
    "natgas": ["美国天然气", "天然气", "液化天然气", "LNG", "亨利港"],
}
# 产业链词表（2026-10-07）：别名之外，讲到这些词的新闻也召回给该品种，再交 AI 判断有没有影响。
# 只列主要上市品种；没有专表的品种只用别名。词要具体到产业链，宽泛词（“价格”“市场”）不放。
FLASH_CHAIN_WORDS: dict[str, list[str]] = {
    "ap": ["红富士", "富士苹果", "冷库", "库容比", "开秤", "栖霞", "洛川", "静宁", "沂源", "蓬莱", "果农", "套袋",
           "苹果产区", "苹果价格", "苹果收购", "晚熟富士", "冷库苹果"],
    "cj": ["红枣产区", "阿克苏", "若羌", "灰枣", "骏枣", "新疆枣"],
    "pk": ["花生米", "花生油", "花生产区", "油料米"],
    "jd": ["蛋鸡", "存栏", "淘汰鸡", "鸡苗", "补栏"],
    "lh": ["能繁母猪", "生猪出栏", "猪肉价格", "仔猪", "二次育肥", "屠宰"],
    "c": ["东北玉米", "深加工", "新粮上市", "饲料需求", "临储"],
    "m": ["大豆进口", "大豆到港", "油厂开机", "压榨", "USDA", "巴西大豆", "美豆出口", "南美大豆"],
    "y": ["压榨", "大豆到港", "油厂开机"],
    "p": ["马来西亚棕榈油", "MPOB", "印尼棕榈油", "生物柴油"],
    "oi": ["菜籽进口", "加拿大菜籽", "油菜籽"],
    "rm": ["菜籽进口", "加拿大菜籽", "油菜籽", "水产饲料"],
    "sr": ["甘蔗", "糖厂", "压榨季", "巴西糖", "印度糖", "食糖进口", "ICE原糖"],
    "cf": ["新疆棉", "籽棉", "皮棉", "纺织订单", "纺企", "棉花收购", "ICE棉花", "USDA"],
    "rb": ["铁矿", "焦炭", "地产", "房地产", "基建", "限产", "钢厂", "高炉", "螺纹库存", "粗钢产量", "建材成交"],
    "hc": ["铁矿", "焦炭", "限产", "钢厂", "高炉", "板材库存", "汽车产量", "家电产量", "粗钢产量"],
    "i": ["铁水", "高炉", "港口库存", "钢厂", "力拓", "必和必拓", "淡水河谷", "FMG", "粗钢产量", "疏港"],
    "j": ["焦化", "焦企", "提涨", "提降", "铁水", "钢厂"],
    "jm": ["炼焦煤", "煤矿", "蒙煤", "通关", "安监", "焦化"],
    "fg": ["浮法", "深加工订单", "地产竣工", "玻璃库存", "冷修"],
    "sa": ["光伏玻璃", "浮法", "碱厂", "氨碱", "联碱", "天然碱"],
    "si": ["光伏", "有机硅", "硅厂", "云南丰水期", "四川丰水期"],
    "ps": ["光伏", "硅料", "硅片", "组件", "光伏装机"],
    "lc": ["锂矿", "锂辉石", "盐湖", "动力电池", "储能", "新能源汽车销量", "宁德时代"],
    "cu": ["铜矿", "铜精矿", "加工费", "TC", "冶炼厂", "电网投资", "智利", "秘鲁", "刚果"],
    "al": ["电解铝产能", "氧化铝", "铝锭库存", "铝棒", "云南限电"],
    "ao": ["铝土矿", "几内亚", "电解铝"],
    "zn": ["锌矿", "锌锭库存", "镀锌"],
    "ni": ["镍矿", "印尼", "镍铁", "不锈钢", "菲律宾"],
    "sn": ["锡矿", "缅甸", "佤邦", "半导体"],
    "au": ["美联储", "降息", "避险", "央行购金", "美元指数", "美债收益率", "地缘"],
    "ag": ["美联储", "降息", "光伏银浆", "避险", "金银比"],
    "sc": ["OPEC", "欧佩克", "减产", "增产", "EIA", "API库存", "原油库存", "炼厂", "中东", "伊朗", "俄罗斯石油"],
    "fu": ["船燃", "炼厂", "原油"],
    "bu": ["炼厂", "道路施工", "原油"],
    "pg": ["丙烷", "CP", "液化气", "PDH"],
    "ta": ["涤纶", "聚酯", "织机开工", "PX", "原油"],
    "eg": ["聚酯", "涤纶", "港口库存", "煤制"],
    "ma": ["甲醇装置", "MTO", "伊朗甲醇", "港口库存"],
    "pp": ["丙烯", "PDH", "石化库存", "两油库存"],
    "l": ["石化库存", "两油库存", "农膜", "乙烯"],
    "v": ["电石", "烧碱", "房地产", "地产"],
    "eb": ["纯苯", "苯乙烯装置"],
    "ur": ["化肥", "农需", "复合肥", "出口法检"],
    "ru": ["割胶", "泰国橡胶", "轮胎", "开割", "停割"],
    "sp": ["针叶浆", "阔叶浆", "纸厂", "文化纸", "生活用纸"],
    "ec": ["集装箱", "运价", "航运", "红海", "苏伊士", "马士基", "SCFI"],
    "t": ["国债收益率", "降准", "降息", "LPR", "MLF", "央行"],
    "if": ["A股", "沪深两市", "北向资金", "股市"],
}
# 排除词（品种 → {分组名: 词}）：只凭别名召回、却讲到这些词又没有讲到产业链词的新闻，直接按分组筛掉，
# 不用再问模型（苹果公司不是苹果期货）。分组名就是折叠行里“AI 筛掉了…”的原因名。
FLASH_PRODUCT_EXCLUDES: dict[str, dict[str, list[str]]] = {
    "ap": {"苹果公司新品与财报": ["苹果公司", "iPhone", "iPad", "Apple", "库克", "iOS", "Mac", "AirPods", "苹果手机",
                                  "苹果发布会", "苹果股价", "苹果市值", "苹果概念", "苹果产业链", "Vision Pro", "App Store",
                                  "财报"]},
    "au": {"黄金周等同名说法": ["黄金周", "黄金时段", "黄金档", "黄金赛道", "黄金十年", "黄金坑"]},
    "ag": {"白银市等同名说法": ["白银市", "甘肃白银"]},
    "pp": {"PPI 等同名缩写": ["PPI", "PPT", "APP"]},
    "sc": {"股票与公司新闻": ["石油股", "中国石油股价", "中石油股价"]},
    "lh": {"股票与公司新闻": ["养殖股", "猪肉股"]},
    "if": {"股票与公司新闻": ["龙虎榜", "涨停", "跌停"]},
}
# 英文关键词（2026-10-11，英文外电 FinancialJuice 用）：英文说法 → 中文锚词，词边界匹配、大小写不敏感。
# 品种归属不在英文侧重复维护：推导时按锚词在中文词表（FLASH_ALIASES + FLASH_CHAIN_WORDS）里做子串匹配
# ——英文词覆盖所有锚词命中的品种，与中文映射天然一致（copper→cu+bc、alumina→ao 也带 al）。
# 只列主要上市品种的通行英文说法；宽泛词（"price""market"）和易误命中的单词（动词 lead、地名 Corn…）不放。
ENGLISH_ALIAS_WORDS: dict[str, tuple[str, ...]] = {
    "copper": ("铜",),
    "aluminium": ("铝",), "aluminum": ("铝",), "alumina": ("氧化铝",),
    "zinc": ("锌",), "nickel": ("镍",), "tin": ("锡",),
    "lead price": ("铅",), "lead market": ("铅",), "lead ingot": ("铅",),
    "gold": ("黄金",), "bullion": ("黄金",), "silver": ("白银",),
    "rebar": ("螺纹",),
    "steel price": ("钢",), "steel mill": ("钢",), "steel output": ("钢",), "steel production": ("钢",),
    "hot-rolled coil": ("热轧",), "hot rolled coil": ("热轧",), "steel plate": ("板材",),
    "iron ore": ("铁矿石",),
    "coke": ("焦炭",),
    "coking coal": ("焦煤",), "met coal": ("焦煤",),
    "thermal coal": ("动力煤",), "coal price": ("煤炭",), "coal output": ("煤炭",),
    "crude oil": ("原油",), "crude": ("原油",), "oil price": ("原油",), "oil market": ("原油",),
    "WTI": ("WTI",), "Brent": ("布伦特",), "OPEC": ("OPEC",),
    "fuel oil": ("燃料油",),
    "low-sulphur fuel oil": ("低硫燃料油",), "low-sulfur fuel oil": ("低硫燃料油",),
    "bitumen": ("沥青",), "asphalt": ("沥青",),
    "LPG": ("LPG",), "liquefied petroleum gas": ("液化石油气",), "propane": ("丙烷",),
    "PTA": ("PTA",),
    "ethylene glycol": ("乙二醇",), "MEG": ("乙二醇",),
    "styrene": ("苯乙烯",),
    "polyethylene": ("聚乙烯",), "LLDPE": ("LLDPE",),
    "polypropylene": ("聚丙烯",),
    "PVC": ("PVC",),
    "methanol": ("甲醇",),
    "urea": ("尿素",),
    "soda ash": ("纯碱",),
    "glass": ("玻璃",),
    "rubber": ("橡胶",), "natural rubber": ("橡胶",),
    "wood pulp": ("纸浆",), "paper pulp": ("纸浆",),
    "corn": ("玉米",),
    "soymeal": ("豆粕",), "soybean meal": ("豆粕",),
    "soybean oil": ("豆油",), "soy oil": ("豆油",), "soyoil": ("豆油",),
    "soybean": ("大豆",), "soybeans": ("大豆",),
    "palm oil": ("棕榈油",),
    "rapeseed oil": ("菜籽油",), "canola oil": ("菜籽油",),
    "rapeseed meal": ("菜粕",), "canola meal": ("菜粕",),
    "sugar": ("白糖", "原糖"),
    "cotton": ("棉花",),
    "hog price": ("生猪", "猪肉"), "hog herd": ("生猪", "猪肉"), "pig price": ("生猪", "猪肉"),
    "pork price": ("生猪", "猪肉"), "pig herd": ("生猪", "猪肉"), "hog supply": ("生猪", "猪肉"),
    "egg price": ("鸡蛋",),
    "lithium": ("锂",), "lithium carbonate": ("碳酸锂",),
    "industrial silicon": ("工业硅",), "silicon metal": ("工业硅",),
    "polysilicon": ("多晶硅",),
    "container shipping": ("集装箱", "运价", "SCFI"), "freight rate": ("集装箱", "运价", "SCFI"),
    "SCFI": ("集装箱", "运价", "SCFI"),
    "CSI 300": ("沪深300",), "Chinese stocks": ("A股",), "China stocks": ("A股",), "Chinese equities": ("A股",),
    "CSI 1000": ("中证1000",),
}
# 中文词表里没有条目的品种（国际铜 bc 只在上期所金属分页的提示里）：英文词 → 额外品种。
_ENGLISH_EXTRA_PRODUCTS: dict[str, tuple[str, ...]] = {"copper": ("bc",)}


def _derive_english_words() -> dict[str, list[str]]:
    """product → its English words: each English phrase covers every product its Chinese anchors
    hit in the alias/chain tables (plus the extras), so the two sides cannot drift apart."""
    derived: dict[str, list[str]] = {}
    tables = {product: [*FLASH_ALIASES.get(product, ()), *FLASH_CHAIN_WORDS.get(product, ())]
              for product in {*FLASH_ALIASES, *FLASH_CHAIN_WORDS}}
    for english, anchors in ENGLISH_ALIAS_WORDS.items():
        products = {product for product, words in tables.items()
                    if any(anchor in word for word in words for anchor in anchors)}
        products.update(_ENGLISH_EXTRA_PRODUCTS.get(english, ()))
        for product in sorted(products):
            derived.setdefault(product, []).append(english)
    return {product: sorted(words) for product, words in derived.items()}


FLASH_ENGLISH_WORDS: dict[str, list[str]] = _derive_english_words()
# 英文宏观词：命中同样打 macro 标签（词边界匹配）。
MACRO_WORDS_EN = ("Federal Reserve", "FOMC", "CPI", "PPI", "PMI", "payrolls", "nonfarm", "non-farm",
                  "unemployment rate", "tariff", "tariffs", "trade war", "sanctions", "ECB", "Bank of Japan",
                  "BOJ", "rate cut", "rate hike", "rate decision", "Treasury yield", "Treasury yields",
                  "dollar index", "PCE", "GDP", "recession", "inflation", "stimulus")
# 大写缩写单独按原大小写匹配（含全大写 FED）：小写 fed 是动词（fed by optimism），不是美联储。
MACRO_WORDS_EN_CASED = ("Fed", "FED")
_ENGLISH_INDEX: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (product, tuple(re.compile(rf"\b{re.escape(word.lower())}\b") for word in words))
    for product, words in FLASH_ENGLISH_WORDS.items())
_MACRO_EN = tuple(re.compile(rf"\b{word.lower()}\b") for word in MACRO_WORDS_EN)
_MACRO_EN_CASED = tuple(re.compile(rf"\b{re.escape(word)}\b") for word in MACRO_WORDS_EN_CASED)

# 本周重要数据按品种过滤（2026-10-07 第六批）：品种类别 → (地区, 事件词)。一条数据的地区相符且事件里有其中一个词，
# 才算与该类品种有关；其余只在“宏观”与“全部”里列出。没有归类的品种不列任何数据。
CALENDAR_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "precious": (("美国",), ("美联储", "FOMC", "利率决议", "会议纪要", "CPI", "PCE", "非农", "失业率", "就业", "国债", "美债", "美元")),
    "energy": (("美国", "中国", "欧元区"), ("EIA", "API", "OPEC", "欧佩克", "原油", "汽油", "成品油", "天然气")),
    "agri": (("美国", "中国", "欧元区"), ("USDA", "美国农业部", "供需报告", "WASDE", "天气", "出口销售", "MPOB", "棕榈油", "生猪", "猪肉")),
    "industrial": (("中国",), ("PMI", "房地产", "地产", "房价", "基建", "固定资产投资", "工业增加值", "社会融资", "社融")),
    "financial": (("中国",), ("PMI", "CPI", "PPI", "GDP", "社会融资", "社融", "M2", "LPR", "MLF", "逆回购", "央行", "降准", "存款准备金",
                            "工业增加值", "零售", "出口", "进口", "贸易")),
    # 外盘的美股指数与美债、外汇期货（2026-10-21）：看美国（外汇另看欧元区）的利率与经济数据。
    "us_macro": (("美国",), ("美联储", "FOMC", "利率决议", "会议纪要", "CPI", "PCE", "非农", "失业率", "就业", "GDP", "零售",
                            "PMI", "国债", "美债")),
    "fx": (("美国", "欧元区"), ("美联储", "FOMC", "利率决议", "欧洲央行", "CPI", "PCE", "非农", "失业率", "GDP", "PMI")),
}
CALENDAR_CATEGORY: dict[str, str] = {
    **{product: "precious" for product in ("au", "ag", "pt", "pd")},
    **{product: "energy" for product in ("sc", "fu", "lu", "bu", "pg", "ta", "eg", "eb", "bz", "ma", "l", "pp", "v",
                                         "ur", "pr", "pl", "ru", "nr", "br", "sh")},
    **{product: "agri" for product in ("c", "cs", "m", "a", "b", "y", "p", "oi", "rm", "rs", "sr", "cf", "cy", "ap", "cj",
                                       "pk", "lh", "jd", "wh", "pm", "ri", "jr", "rr")},
    **{product: "industrial" for product in ("rb", "hc", "wr", "ss", "i", "j", "jm", "zc", "sf", "sm", "fg", "sa", "cu",
                                             "bc", "al", "ao", "ad", "zn", "pb", "ni", "sn", "si", "ps", "lc")},
    **{product: "financial" for product in ("if", "ih", "ic", "im", "t", "tf", "ts", "tl")},
    **{product: "us_macro" for product in ("spx", "nasdaq", "dji", "russell", "ust")},
    **{product: "fx" for product in ("eur", "jpy", "gbp", "aud", "cad", "chf", "nzd")},
    "natgas": "energy",
}


def calendar_related(event: dict[str, Any], product: str) -> bool:
    """Whether an economic-calendar event matters to this product's category (see CALENDAR_RULES)."""
    rule = CALENDAR_RULES.get(CALENDAR_CATEGORY.get(product, ""))
    if rule is None:
        return False
    regions, words = rule
    return event.get("region") in regions and any(word in str(event.get("event") or "") for word in words)


MACRO_WORDS = ("美联储", "降息", "加息", "非农", "CPI", "PPI", "PMI", "央行", "关税", "地缘",
               "国债收益率", "美债", "LPR", "MLF", "逆回购", "社融", "M2", "GDP", "美国经济",
               "欧元区", "欧洲央行", "日本央行", "中东局势", "俄乌", "制裁", "汇率", "人民币")

# 地名与日用品里的单字金属字（无锡、锡林郭勒、铅笔、铜仁…，含合约目录注入的品种名“锡”“铅”“铜”）：
# 标注前先把这些词遮蔽，被遮住的那一段不给任何品种计命中，同文其他段落的真说法照常命中。
FLASH_EXCLUDE_WORDS = ("无锡", "锡林郭勒", "锡林浩特", "锡盟", "铅笔", "铜仁", "铜川")
FLASH_EXCLUDE_MASK = "□"  # □ 不会出现在别名表里

_TAGS = re.compile(r"<[^>]+>")
_FLASH_SHELL = re.compile(r"^【[^】]{0,80}】")
_FLASH_PREFIX = re.compile(
    r"^(?:财联社\s*\d{1,2}月\d{1,2}日电|SHMET\s*\d{1,2}月\d{1,2}日讯|新浪财经\s*\d{1,2}月\d{1,2}日讯"
    r"|人民财讯\s*\d{1,2}月\d{1,2}日电|汇通财经APP讯\s*[—-]*|市场消息|快讯|媒体消息|金融界)\s*[：，,]?"
)


def flash_body(text: Any) -> str:
    """The passage under its 【…】 shell and wire prefix (财联社…电，/SHMET…讯，…), markup removed."""
    body = html.unescape(_TAGS.sub(" ", _text(text))).strip()
    body = _FLASH_SHELL.sub("", body)
    body = _FLASH_PREFIX.sub("", body)
    return body.strip()


def flash_key(text: Any) -> str:
    """Dedup key: the first 40 characters of the normalised body (whitespace aside)."""
    return re.sub(r"\s+", "", flash_body(text)).lower()[:40]


_TITLE_NOISE = re.compile(r"[\s\W_]+")


def merge_title_key(title: Any) -> str:
    """A title's punctuation- and space-free form, lower-case: the second dedup key, for when the
    bodies cannot meet (an exchange notice stores its title as the body; the syndicated copies
    rewrite theirs). Short keys are not distinctive enough to merge on."""
    key = _TITLE_NOISE.sub("", flash_body(title)).lower()
    return key if len(key) >= 6 else ""


def _title_only(item: dict[str, Any]) -> bool:
    return item["text"] == item["title"]


def _moment(*values: Any) -> datetime | None:
    """A source timestamp (date+time, string, pandas Timestamp or datetime) as Beijing time."""
    if len(values) == 2:  # 发布日期 + 发布时间
        day, clock = values
        if isinstance(day, date_cls) and isinstance(clock, time_cls):
            return datetime.combine(day, clock, tzinfo=CHINA)
        values = (f"{day} {clock}",)
    value = values[0] if values else None
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(CHINA) if value.tzinfo else value.replace(tzinfo=CHINA)
    if isinstance(value, date_cls):
        return datetime.combine(value, time_cls(0, 0), tzinfo=CHINA)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return parsed.astimezone(CHINA) if parsed.tzinfo else parsed.replace(tzinfo=CHINA)
    except ValueError:
        pass
    text = str(value).strip().replace("T", " ")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y年%m月%d日 %H:%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=CHINA)
        except ValueError:
            continue
    return None


def _link(value: Any) -> str | None:
    text = _text(value)
    return text if text.startswith(("http://", "https://")) else None


def product_key(product: str | None) -> str | None:
    """`SHFE.rb`, `rb`, a contract (`SHFE.rb2701`, `rb2701`) or a continuous code (`KQ.m@SHFE.rb`) → the
    lower-case product id the alias table and maps are keyed on: the instrument's leading letters, as
    review_tags.product_key reads a symbol. 10-14: a contract read as its own product (`au2612`) and the
    screener queued its macro candidates again under that name."""
    if not product:
        return None
    # 外盘代码（外盘图表的快期外盘主连 KQD.m@COMEX.GC、美元子账户的月份合约 CME.GCEZ26）读作它对照的国内品种
    # （COMEX黄金 → au）：快讯、事件、日历按国内品种标注，外盘图表看到的是同一件事的新闻；没有国内对照的
    # （美国天然气）为 None，即不按品种筛。
    foreign = foreign_product_of(product.strip()) if product.strip().upper().startswith(("KQD.", "CME.")) else None
    if foreign is not None:
        return foreign.news_product
    tail = re.sub(r"^KQ\.[A-Za-z]@", "", product.strip()).rsplit(".", 1)[-1].strip().lower()
    match = re.match(r"[a-z]+", tail)
    return match.group(0) if match else (tail or None)


# akshare writes these sources' epoch stamps with datetime.fromtimestamp, i.e. in this process's
# timezone: in the UTC cloud container a 12:00 Beijing item read 04:00 and sank as old (10-08).
PROCESS_LOCAL_STAMPS = frozenset({"同花顺", "富途"})


def _process_local(value: Any) -> datetime | None:
    """A naive 'YYYY-MM-DD HH:MM:SS' written in this process's local time, as Beijing time."""
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S").astimezone(CHINA)
    except ValueError:
        return _moment(value)


def normalise_flash_rows(source: str, rows: Any, since: datetime | None = None,
                         until: datetime | None = None) -> list[dict[str, Any]]:
    """One source's raw rows as flash candidates: id, source, publishedAt, title, text, link (and
    `hint`: products the source itself files the row under). Rows older than `since` or dated after
    `until` (the poll passes now + FLASH_FUTURE_LIMIT) are skipped."""
    items: list[dict[str, Any]] = []
    for row in _records(rows):
        if source == "财联社":
            published, raw, title = _moment(row.get("发布日期"), row.get("发布时间")), row.get("内容"), _text(row.get("标题"))
        elif source == "东方财富":
            published, raw, title = _moment(row.get("发布时间")), row.get("摘要"), _text(row.get("标题"))
        elif source == "新浪":
            published, raw, title = _moment(row.get("时间")), row.get("内容"), ""
        elif source == "上海金属网":
            published, raw, title = _moment(row.get("发布时间")), row.get("内容"), ""
        else:  # 同花顺、富途 and the RSSHub sources: 标题 / 内容 / 发布时间 / 链接
            stamp = row.get("发布时间")
            published = _process_local(stamp) if source in PROCESS_LOCAL_STAMPS else _moment(stamp)
            raw, title = row.get("内容") or row.get("标题"), _text(row.get("标题"))
        body = flash_body(raw)
        if (published is None or not body or (since is not None and published < since)
                or (until is not None and published > until)):
            continue
        shell = _FLASH_SHELL.match(_text(raw))
        title = title or (shell.group(0)[1:-1] if shell else "") or (body[:18] + "…" if len(body) > 18 else body)
        key = flash_key(raw)
        if not key:
            continue
        item: dict[str, Any] = {"id": hashlib.sha1(key.encode()).hexdigest()[:16], "source": source,
                "publishedAt": published.isoformat(timespec="seconds"), "title": title,
                "text": body[:1000], "link": _link(row.get("链接"))}
        if row.get("_products"):
            item["hint"] = list(row["_products"])
        items.append(item)
    return items



def parse_rsshub_feed(payload: Any) -> list[dict[str, Any]]:
    """An RSSHub JSON Feed (`?format=json`) as 标题 / 内容 / 发布时间 / 链接 rows. A body that is the
    site's paywall form (隆众资讯) is dropped: the title carries the item."""
    rows: list[dict[str, Any]] = []
    for entry in (payload.get("items") if isinstance(payload, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        title = html.unescape(_TAGS.sub("", str(entry.get("title") or ""))).strip()
        body = html.unescape(_TAGS.sub(" ", str(entry.get("content_html") or entry.get("content_text") or "")))
        body = re.sub(r"\s+", " ", body).strip()
        if "免费开通" in body or "会员登录" in body:
            body = ""
        if body == title:
            title = ""
        stamp = str(entry.get("date_published") or "")
        try:
            published = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(CHINA)
        except ValueError:
            continue
        link = entry.get("url") or (entry.get("id") if str(entry.get("id") or "").startswith("http") else None)
        rows.append({"标题": title, "内容": body or title, "发布时间": published, "链接": link})
    return rows


# -- direct sources (exchanges and industry wires): title, link and time only, never the body.
# Pure parsers over the fetched page so tests run on recorded samples; each returns the same
# 标题 / 内容 / 发布时间 / 链接 rows the RSSHub path produces. Bodies behind paywalls (SMM, Mysteel,
# 隆众) are never fetched — the title carries the item.

NOTICE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                                " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                  "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
SHFE_NOTICE_URL = "https://www.shfe.com.cn/publicnotice/notice/"
GFEX_NOTICE_URL = "http://www.gfex.com.cn/gfex/tzts/list_yw.shtml"
SMM_NEWS_URL = "https://news.smm.cn/"
MYSTEEL_NEWS_URL = "https://news.mysteel.com/"
FINJUICE_FEED_URL = "https://www.financialjuice.com/feed.ashx?xy=rss"
FINJUICE_PREFIX = "FinancialJuice: "
_SHFE_ROW = re.compile(r'<a href="(\./\d{6}/t\d{8}_\d+\.html)"[^>]*title="([^"]+)"[\s\S]*?'
                      r'<div class="info_item_date">(\d{4}-\d{2}-\d{2})</div>')
_GFEX_ROW = re.compile(r'<span class="dd">(\d{1,2})</span>\s*<span class="yyMM">(\d{4}\.\d{2})</span>([\s\S]*?)</li>')
_GFEX_ANCHOR = re.compile(r'<a[^>]*title="([^"]+)"[^>]*href="([^"]+\.shtml)"')
_SMM_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">([\s\S]*?)</script>')
_MYSTEEL_ROW = re.compile(r'<a href="(https://news\.mysteel\.com/a/(\d{8})/[0-9A-Za-z]+\.html)"[^>]*title="([^"]{4,200})"')


def parse_shfe_notices(body: str) -> list[dict[str, Any]]:
    """上期所公告通知（含上期能源）：页面只给日期，时间记当天 00:00 北京。"""
    rows = []
    for href, title, day in _SHFE_ROW.findall(body):
        published = _moment(datetime.strptime(day, "%Y-%m-%d"))
        if published is not None:
            rows.append({"标题": title.strip(), "内容": title.strip(), "发布时间": published,
                         "链接": urljoin(SHFE_NOTICE_URL, href)})
    return rows


def parse_gfex_notices(body: str) -> list[dict[str, Any]]:
    """广期所通知公告：日期拆在 dd（日）与 yyMM（年.月）两个 span 里，时间记当天 00:00 北京。"""
    rows = []
    for day, year_month, block in _GFEX_ROW.findall(body):
        anchor = _GFEX_ANCHOR.search(block)
        if anchor is None:
            continue
        title = _TAGS.sub("", re.sub(r"(?i)\s*<br\s*/?>\s*", " ", anchor.group(1))).strip()
        href = anchor.group(2)
        try:
            published = _moment(datetime.strptime(f"{year_month}.{int(day):02d}", "%Y.%m.%d"))
        except ValueError:
            continue
        if published is not None:
            rows.append({"标题": title, "内容": title, "发布时间": published,
                         "链接": urljoin(GFEX_NOTICE_URL, href)})
    return rows


def parse_smm_news(body: str) -> list[dict[str, Any]]:
    """上海有色网最新要闻：列表页 __NEXT_DATA__ 里的 newsList（标题、epoch 发布时间、newsId）。"""
    match = _SMM_DATA.search(body)
    if match is None:
        return []
    try:
        payload = json.loads(match.group(1))
        entries = ((((payload.get("props") or {}).get("pageProps") or {}).get("newsList") or {})
                   .get("data") or {}).get("listList") or []
    except ValueError:
        return []
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title, news_id, stamp = _text(entry.get("title")), entry.get("newsId"), entry.get("pubDate")
        if not title or not isinstance(news_id, int) or not isinstance(stamp, (int, float)):
            continue
        rows.append({"标题": title, "内容": title, "发布时间": datetime.fromtimestamp(stamp, CHINA),
                     "链接": f"https://news.smm.cn/news/{news_id}"})
    return rows


def parse_mysteel_news(body: str) -> list[dict[str, Any]]:
    """我的钢铁网资讯首页：各栏目的条目链接把发布时间编在路径里（a/YYMMDDHH/hash.html，整点）。"""
    rows: dict[str, dict[str, Any]] = {}
    for href, stamp, title in _MYSTEEL_ROW.findall(body):
        if href in rows:
            continue
        try:
            published = _moment(datetime(2000 + int(stamp[0:2]), int(stamp[2:4]), int(stamp[4:6]), int(stamp[6:8])))
        except ValueError:
            continue
        if published is not None:
            rows[href] = {"标题": title.strip(), "内容": title.strip(), "发布时间": published, "链接": href}
    return sorted(rows.values(), key=lambda row: row["发布时间"], reverse=True)[:RSSHUB_LIMIT]


def parse_financialjuice(body: str) -> list[dict[str, Any]]:
    """FinancialJuice 的公开 RSS（英文外电快讯，RFC822 GMT 时间）。"""
    try:
        channel = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    rows = []
    for node in channel.iter("item"):
        title, link, stamp = node.findtext("title") or "", node.findtext("link") or "", node.findtext("pubDate") or ""
        title = html.unescape(title).strip()
        if title.startswith(FINJUICE_PREFIX):
            title = title[len(FINJUICE_PREFIX):].strip()
        try:
            published = parsedate_to_datetime(stamp)
        except (TypeError, ValueError):
            continue
        if not title or published is None:
            continue
        rows.append({"标题": title, "内容": title, "发布时间": published, "链接": link or None})
    return rows[:RSSHUB_LIMIT]


# -- 第 3 优先级中文财经媒体与郑商所/大商所公告（2026-10-13）：列表页/列表接口自带标题、时间（快讯类连同正文），
# 一次请求一页，不逐条抓详情页。

STCN_KX_URL = "https://www.stcn.com/article/list.html?type=kx"
FX678_KX_URL = "https://www.fx678.com/kx/"
THEPAPER_FINANCE_URL = "https://www.thepaper.cn/channel_25951"
NBD_NEWS_URL = "https://www.nbd.com.cn/columns/3/"
EASTMONEY_COLUMN_URL = ("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col"
                        "&column={column}&order=1&needInteractData=0&page_index=1&page_size={size}&req_trace=ai-trader"
                        "&fields=code,showTime,title,mediaName,summary,url,uniqueUrl&types=1,20")
# 东方财富期货频道“交易所公告”栏目（发布者即交易所，正文是公告原文开头）。
EXCHANGE_BOARD_COLUMN = 525
# 栏目里的发布者 → 快讯来源名：只取官网读不到的两家，上期所/广期所/中金所已直读或经 RSSHub 读官网。
EXCHANGE_BOARD_MEDIA = {"郑州商品交易所": "郑商所", "大连商品交易所": "大商所"}
CCTV_XWLB_URL = "https://tv.cctv.com/lm/xwlb/day/{day}.shtml"
# 新闻联播 19:00 开播、约 19:30 结束，文字稿随后上网：此前读的是前一天。
CCTV_READY = time_cls(19, 45)
_FX678_ROW = re.compile(r'<a id="aid(\d{14})\d*"[^>]*?href="(https://www\.fx678\.com/C/\d{8}/\d+\.html)"\s*>\s*'
                        r'<span>([\s\S]*?)</span>')
_NBD_ROW = re.compile(r'<a href="(https://www\.nbd\.com\.cn/articles/(\d{4}-\d{2}-\d{2})/\d+\.html)"[^>]*>([^<]{4,200})</a>'
                      r'\s*<span>\s*(\d{2}:\d{2}:\d{2})\s*</span>')


def _json_body(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def parse_stcn_kx(body: str) -> list[dict[str, Any]]:
    """证券时报网·人民财讯快讯：列表接口 JSON（标题、正文、毫秒时间戳、详情页路径）。RSSHub 的
    /stcn/article/list/kx 不带发布时间，所以直读这个接口。"""
    payload = _json_body(body)
    rows = []
    for entry in (payload.get("data") if isinstance(payload, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        title, text, stamp = _text(entry.get("title")), _text(entry.get("content")), entry.get("time")
        if not title or not isinstance(stamp, (int, float)):
            continue
        link = _text(entry.get("url"))
        rows.append({"标题": title, "内容": text or title, "发布时间": datetime.fromtimestamp(stamp / 1000, CHINA),
                     "链接": urljoin("https://www.stcn.com/", link) if link else None})
    return rows


def parse_fx678_kx(body: str) -> list[dict[str, Any]]:
    """汇通网 7×24 快讯列表页：条目 id 编着发布时间（YYYYMMDDHHMMSS…），正文就在列表里。数据公布类条目
    （前值/预期/公布值）不是新闻，跳过。"""
    rows = []
    for stamp, link, raw in _FX678_ROW.findall(body):
        text = re.sub(r"\s+", " ", html.unescape(_TAGS.sub(" ", raw))).strip()
        try:
            published = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=CHINA)
        except ValueError:
            continue
        if text:
            shell = _FLASH_SHELL.match(text)
            rows.append({"标题": shell.group(0)[1:-1].strip() if shell else "", "内容": text, "发布时间": published,
                         "链接": link})
    return rows[:RSSHUB_LIMIT * 2]


def parse_thepaper_channel(body: str) -> list[dict[str, Any]]:
    """澎湃新闻财经频道页：__NEXT_DATA__ 里的频道列表与“财经资讯”栏（标题、毫秒时间戳、contId），只有标题。"""
    match = _SMM_DATA.search(body)
    payload = _json_body(match.group(1)) if match else None
    props = ((payload or {}).get("props") or {}).get("pageProps") or {}
    entries = [*((((props.get("data") or {}).get("data") or {}).get("list")) or []),
               *((((props.get("topData") or {}).get("data") or {}).get("financialInformationNews")) or [])]
    rows: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title, cont_id, stamp = _text(entry.get("name")), _text(entry.get("contId")), entry.get("pubTimeLong")
        if not title or not cont_id.isdigit() or not isinstance(stamp, (int, float)) or cont_id in rows:
            continue
        rows[cont_id] = {"标题": title, "内容": title, "发布时间": datetime.fromtimestamp(stamp / 1000, CHINA),
                         "链接": f"https://www.thepaper.cn/newsDetail_forward_{cont_id}"}
    return sorted(rows.values(), key=lambda row: row["发布时间"], reverse=True)


def parse_nbd_column(body: str) -> list[dict[str, Any]]:
    """每经网要闻栏目页：链接路径带日期、旁边的 span 带时分秒，只有标题。"""
    rows: dict[str, dict[str, Any]] = {}
    for link, day, title, clock in _NBD_ROW.findall(body):
        published = _moment(f"{day} {clock}")
        title = html.unescape(title).strip()
        if published is not None and title and link not in rows:
            rows[link] = {"标题": title, "内容": title, "发布时间": published, "链接": link}
    return sorted(rows.values(), key=lambda row: row["发布时间"], reverse=True)


def parse_eastmoney_column(body: str) -> list[dict[str, Any]]:
    """东方财富资讯栏目列表接口（getNewsByColumns）：标题、摘要、发布者（mediaName）、发布时间、链接。"""
    payload = _json_body(body)
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = []
    for entry in (data.get("list") if isinstance(data, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        title, published = _text(entry.get("title")), _moment(entry.get("showTime"))
        if not title or published is None:
            continue
        link = _text(entry.get("uniqueUrl") or entry.get("url"))
        rows.append({"标题": title, "内容": _text(entry.get("summary")) or title, "发布时间": published,
                     "链接": link.replace("http://", "https://", 1) if link else None, "_media": _text(entry.get("mediaName"))})
    return rows


def exchange_board_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """“交易所公告”栏目里郑商所、大商所的公告，按交易所分开。与直读的交易所通知一样只留标题（正文是
    “〔2026〕154号根据《期货和衍生品法》…”一类套话开头，作去重键会把不同公告认成一条；只有标题的条目
    还能按标题并上媒体转载）。"""
    split: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source = EXCHANGE_BOARD_MEDIA.get(row.get("_media") or "")
        if source is not None:
            split.setdefault(source, []).append({"标题": row["标题"], "内容": row["标题"], "发布时间": row["发布时间"],
                                                 "链接": row["链接"]})
    return split


def cctv_rows(records: Any, day: date_cls) -> list[dict[str, Any]]:
    """AKShare news_cctv 的新闻联播文字稿（每段一行：标题、正文）；只有日期，时间记当天 19:00 北京。"""
    published = datetime.combine(day, time_cls(19, 0), tzinfo=CHINA)
    link = CCTV_XWLB_URL.format(day=day.strftime("%Y%m%d"))
    rows = []
    for row in _records(records):
        title, text = _text(row.get("title")), _text(row.get("content"))
        if title and text:
            rows.append({"标题": title, "内容": text, "发布时间": published, "链接": link})
    return rows


def _item_from_row(row: sqlite3.Row | tuple) -> dict[str, Any]:
    # Lists of short strings this module wrote: orjson reads them to the same values (2026-10-13).
    return {"id": row[0], "source": row[1], "sources": fast_json.loads(row[2]), "publishedAt": row[3],
            "title": row[4], "text": row[5], "link": row[6] or None,
            "products": fast_json.loads(row[7]), "tags": fast_json.loads(row[8]), "origTitle": row[9] or None}


_ITEM_COLUMNS = ("id, source, sources_json, published_at, title, text, link, products_json, tags_json, orig_title")
_OFFICIAL_NAMES = frozenset(OFFICIAL_SOURCE_NAMES)


def _item_copy(item: dict[str, Any]) -> dict[str, Any]:
    """A caller's own copy of a shared parsed row: the merge and the views may change an item's lists."""
    return {**item, "sources": list(item["sources"]), "products": list(item["products"]), "tags": list(item["tags"])}
# The columns a row's content lives in (a change to any of them takes a new `rev`), and the item keys they
# become: a compact feed (`compact_flash`) leaves exactly these out of an item the reader already holds.
_ROW_CONTENT = "source, sources_json, published_at, title, text, link, products_json, tags_json, orig_title"
FLASH_ROW_FIELDS = ("source", "sources", "publishedAt", "title", "text", "link", "products", "tags", "origTitle")
# A merged event's own copy of its headline report: in the compact feed the event names that report (`headline`).
EVENT_HEADLINE_FIELDS = ("title", "text", "origTitle")
# At most this many ids in one `include` (rows the reader found missing from its copy).
FLASH_INCLUDE_MAX = 100


# 纯拉丁字词（PP、TC、LPG…）也出现在中文别名/产业链词表里：命中要求拉丁边界——US PPI 里的
# PP 不是聚丙烯、US CPI 里的 CP 不是液化气，而中文标题里的 PTA装置、PDH检修 照常命中。
_LATIN_ONLY = re.compile(r"^[0-9A-Za-z][0-9A-Za-z./+&-]*$")
_word_hits: dict[str, re.Pattern[str]] = {}


def _hits(word: str, haystack: str) -> bool:
    """One alias/chain word in the haystack: words with non-Latin characters by substring (the
    Chinese reading), Latin-only words on Latin boundaries (see the comment above)."""
    if not _LATIN_ONLY.match(word):
        return word in haystack
    pattern = _word_hits.get(word)
    if pattern is None:
        pattern = _word_hits[word] = re.compile(rf"(?<![0-9A-Za-z]){re.escape(word)}(?![0-9A-Za-z])")
    return pattern.search(haystack) is not None


def _masked(text: str) -> str:
    for word in FLASH_EXCLUDE_WORDS:
        text = text.replace(word, FLASH_EXCLUDE_MASK * len(word))
    return text


def _products_named(haystack: str, aliases: list[tuple[str, tuple[str, ...]]]) -> set[str]:
    """The products a (masked) text names: the alias index (catalog names + table), the 产业链词表 and the
    English keyword table (词边界)."""
    recalled = {product for product, words in aliases if any(_hits(word, haystack) for word in words)}
    recalled |= {product for product, words in FLASH_CHAIN_WORDS.items() if any(_hits(word, haystack) for word in words)}
    english = haystack.lower()
    recalled |= {product for product, patterns in _ENGLISH_INDEX if any(p.search(english) for p in patterns)}
    return recalled


def tag_flash_item(item: dict[str, Any], aliases: list[tuple[str, tuple[str, ...]]]) -> dict[str, Any]:
    """Product keys and the macro tag for one item: the products its title and body name
    (`_products_named`) and the products the source filed it under (上海金属网's metal). 交易所 notice
    items also carry the 交易所 tag; a report naming its original source (据新华社、国家统计局数据显示…)
    carries `据:<来源>` (news_origin)."""
    haystack = _masked(f"{item['title']}{item['text']}")
    recalled = _products_named(haystack, aliases) | set(item.pop("hint", None) or ())
    english = haystack.lower()
    item["products"] = sorted(recalled)
    tags = ["macro"] if any(word in haystack for word in MACRO_WORDS) or any(p.search(english) for p in _MACRO_EN) \
        or any(p.search(haystack) for p in _MACRO_EN_CASED) else []
    if item.get("source") in EXCHANGE_SOURCES:
        tags.append("交易所")
    # 原始来源（据新华社、海关总署公布…）：news_origin 的规则表；官方源自己的条目不标自己。
    tags += [f"{ORIGIN_TAG}{origin}" for origin in origins_of(haystack)
             if ORIGIN_SOURCE.get(origin, origin) != item.get("source")]
    item["tags"] = tags
    return item


def _read_sources(key: str) -> tuple[str, ...]:
    """The source names one fetch key reads (`source|route` for an RSSHub route; a split read's sources)."""
    base = key.split("|", 1)[0]
    return SPLIT_SOURCES.get(base, (base,))


def _cited_sources(item: dict[str, Any]) -> list[str]:
    """The official sources a row says it retells (its `据:` tags that name a source we read)."""
    return [source for tag in item["tags"] if tag.startswith(ORIGIN_TAG)
            for source in [ORIGIN_SOURCE.get(tag[len(ORIGIN_TAG):])] if source]


# A retelling comes after the official item (an exchange notice is dated 00:00 of its day, the reports run
# hours or days later); a few hours earlier is allowed for a release time the official list rounds down.
CITE_BEFORE = timedelta(hours=6)
CITE_AFTER = timedelta(days=3)
# A retelling leads with the release: the official title must be in the report's title or its opening
# sentences. A digest (“1、截至午间收盘…；2、国家统计局…”) that mentions it further down is a story of its own
# (2026-10-14 回读：两篇这样的综述被并进了统计局 CPI/PPI 条目，正文与链接没了).
CITE_OPENING_CHARS = 120


def _same_story(official: dict[str, Any], report: dict[str, Any],
                aliases: list[tuple[str, tuple[str, ...]]]) -> bool:
    """Whether a media report is the official item retold — the one test both arrival orders use. Close in
    time, the official title retold word for word (news_origin.retells), and about the same products: a
    same-template notice for another product (“关于调整铝期货交易保证金的通知” beside the 铜 one) is a
    different notice. One side's products sharing none with the other's (an empty side included) keeps
    them apart, and so does a product either headline names that the other item never mentions."""
    gap = datetime.fromisoformat(report["publishedAt"]) - datetime.fromisoformat(official["publishedAt"])
    if not -CITE_BEFORE <= gap <= CITE_AFTER:
        return False
    if not retells(official["title"], report["title"], report["text"][:CITE_OPENING_CHARS]):
        return False
    official_products, report_products = set(official["products"]), set(report["products"])
    if (official_products or report_products) and not official_products & report_products:
        return False
    return (_products_named(_masked(official["title"]), aliases) <= report_products
            and _products_named(_masked(report["title"]), aliases) <= official_products)


def _cited_match(candidate: dict[str, Any], official_rows: dict[str, list[dict[str, Any]]],
                 cited_rows: dict[str, list[dict[str, Any]]],
                 aliases: list[tuple[str, tuple[str, ...]]]) -> list[dict[str, Any]]:
    """The rows a candidate belongs with by its original source, earliest first (empty: none). An official
    candidate meets every stored media row that cites its source and retells it — several reports of the one
    notice that arrived before it all join it (#853), the earliest row carrying the merge whatever order the
    table was read in; a citing media candidate meets that source's official row — only when exactly one
    official row fits: two similar notices of one source leave the report on its own."""
    if candidate["source"] in OFFICIAL_SOURCE_NAMES:
        reports = {id(row): row for row in cited_rows.get(candidate["source"], ()) if _same_story(candidate, row, aliases)}
        return sorted(reports.values(), key=lambda row: (row["publishedAt"], row["id"]))
    found = {id(row): row for source in _cited_sources(candidate) for row in official_rows.get(source, ())
             if _same_story(row, candidate, aliases)}
    return list(found.values()) if len(found) == 1 else []


def keyword_drop(item: dict[str, Any], product: str) -> str | None:
    """The 排除词 group that rules this item out for the product: an exclude word without any of the
    product's 产业链词 (苹果公司财报 is not 苹果期货; 栖霞冷库 next to iPhone still is)."""
    groups = FLASH_PRODUCT_EXCLUDES.get(product)
    if not groups:
        return None
    haystack = f"{item.get('title') or ''}{item.get('text') or ''}"
    if any(word in haystack for word in FLASH_CHAIN_WORDS.get(product, ())):
        return None
    for group, words in groups.items():
        if any(word in haystack for word in words):
            return group
    return None


class LiveSources:
    """The AKShare calls, one per dataset. Names follow the AKShare docs exactly."""

    def __init__(self) -> None:
        self._module: Any | None = None

    def module(self) -> Any:
        if self._module is None:
            try:
                import akshare  # deferred: heavy, optional
            except ImportError as error:
                raise RuntimeError("research_dependency_missing") from error
            self._module = akshare
        return self._module

    def flash_cls(self) -> Any:
        return self.module().stock_info_global_cls(symbol="全部")

    def flash_em(self) -> Any:
        with EASTMONEY.turn("np-weblist.eastmoney.com"):  # every Eastmoney request takes the Eastmoney lane
            return self.module().stock_info_global_em()

    def flash_sina(self) -> Any:
        return self.module().stock_info_global_sina()

    def flash_ths(self) -> Any:
        with GENTLE.turn("news.10jqka.com.cn"):
            return self.module().stock_info_global_ths()

    def flash_futu(self) -> Any:
        with GENTLE.turn("news.futunn.com"):
            return self.module().stock_info_global_futu()

    def flash_shmet(self, symbol: str) -> Any:
        with GENTLE.turn("www.shmet.com"):
            return self.module().futures_news_shmet(symbol=symbol)

    def rsshub(self, base: str, route: str) -> Any:
        """One route of the self-hosted RSSHub as its JSON Feed. Each route paces on its own lane:
        routes are read concurrently and a stalled one must not eat another route's timeout while
        it waits for a shared lock."""
        separator = "&" if "?" in route else "?"
        url = f"{base.rstrip('/')}{route}{separator}format=json&limit={RSSHUB_LIMIT}"
        with GENTLE.turn(f"rsshub:{route}"), httpx.Client(timeout=RSSHUB_TIMEOUT_SECONDS,
                                                         verify=shared_ssl_context()) as client:
            response = client.get(url)
        response.raise_for_status()
        return response.json()

    def _fetch_page(self, host: str, url: str, headers: dict[str, str] | None = None, lane: Any = GENTLE) -> str:
        with lane.turn(host), httpx.Client(timeout=FLASH_SOURCE_TIMEOUT_SECONDS, headers={**NOTICE_HEADERS, **(headers or {})},
                                           follow_redirects=True, verify=shared_ssl_context()) as client:
            response = client.get(url)
        response.raise_for_status()
        return response.text

    def notice_shfe(self) -> Any:
        return parse_shfe_notices(self._fetch_page("www.shfe.com.cn", SHFE_NOTICE_URL))

    def notice_gfex(self) -> Any:
        return parse_gfex_notices(self._fetch_page("www.gfex.com.cn", GFEX_NOTICE_URL))

    def news_smm(self) -> Any:
        return parse_smm_news(self._fetch_page("news.smm.cn", SMM_NEWS_URL))

    def news_mysteel(self) -> Any:
        return parse_mysteel_news(self._fetch_page("news.mysteel.com", MYSTEEL_NEWS_URL))

    def flash_financialjuice(self) -> Any:
        return parse_financialjuice(self._fetch_page("www.financialjuice.com", FINJUICE_FEED_URL))

    def news_stcn(self) -> Any:
        return parse_stcn_kx(self._fetch_page("www.stcn.com", STCN_KX_URL, {"X-Requested-With": "XMLHttpRequest"}))

    def news_fx678(self) -> Any:
        return parse_fx678_kx(self._fetch_page("www.fx678.com", FX678_KX_URL))

    def news_thepaper(self) -> Any:
        return parse_thepaper_channel(self._fetch_page("www.thepaper.cn", THEPAPER_FINANCE_URL))

    def news_nbd(self) -> Any:
        return parse_nbd_column(self._fetch_page("www.nbd.com.cn", NBD_NEWS_URL))

    def notice_exchange_board(self) -> dict[str, list[dict[str, Any]]]:
        url = EASTMONEY_COLUMN_URL.format(column=EXCHANGE_BOARD_COLUMN, size=50)
        return exchange_board_rows(parse_eastmoney_column(self._fetch_page("np-listapi.eastmoney.com", url, lane=EASTMONEY)))

    def news_cctv(self, day: date_cls) -> Any:
        return self.module().news_cctv(date=day.strftime("%Y%m%d"))

    def economic_calendar(self, date: str) -> Any:
        return self.module().news_economic_baidu(date=date)

    def bond_rates(self, start_date: str) -> Any:
        return self.module().bond_zh_us_rate(start_date=start_date)

    def shibor(self) -> Any:
        return self.module().macro_china_shibor_all()

    def lpr(self) -> Any:
        return self.module().macro_china_lpr()

    def hog(self) -> Any:
        return self.module().futures_hog_core(symbol="外三元")


def _with_timeout(seconds: float, label: str, fetch: Callable[[], Any]) -> Any:
    """The scraper runs on its own throwaway thread: a hung site loses the thread, not the caller."""
    single = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"research-live-{label}")
    try:
        return single.submit(fetch).result(timeout=seconds)
    finally:
        single.shutdown(wait=False, cancel_futures=True)


class _SourceCalls:
    """One call per label at a time, each on its own throwaway thread (#932). AKShare requests often carry no
    timeout, so a caller stops waiting after `seconds` while the call goes on; the next caller (after the
    collector's back-off) waits on that same running call instead of starting a second thread on a site that
    has not answered the first."""

    def __init__(self) -> None:
        self._running: dict[str, Future[Any]] = {}
        self._guard = threading.RLock()  # a call that is already done runs its done-callback inline

    def call(self, seconds: float, label: str, fetch: Callable[[], Any]) -> Any:
        with self._guard:
            future = self._running.get(label)
            if future is None:
                single = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"research-live-{label}")
                future = self._running[label] = single.submit(fetch)
                single.shutdown(wait=False)
                future.add_done_callback(functools.partial(self._finished, label))
        return future.result(timeout=seconds)

    def _finished(self, label: str, done: Future[Any]) -> None:
        with self._guard:
            if self._running.get(label) is done:
                del self._running[label]


@dataclass
class ResearchLiveService:
    """Flash feed store + shared caches. `sources` is swapped for recorded samples in tests."""

    connection: sqlite3.Connection
    lock: threading.Lock | threading.RLock
    state_root: Path | None = None
    instrument_catalog: Callable[[], dict[str, Any]] | None = None
    sources: LiveSources = field(default_factory=LiveSources)
    foreign: ForeignMarketService | None = None  # the one foreign-quote entry (foreign_market.py)
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    poll_seconds: float = FLASH_POLL_SECONDS
    _loop: asyncio.Task[None] | None = field(default=None, repr=False)
    _alias_index: tuple[float, list[tuple[str, tuple[str, ...]]]] | None = field(default=None, repr=False)
    # (expires, items, fetchedAt) of the last good read; the failure shared until its retry time.
    # Beijing day → (fetchedAt, events) from the last round that answered it; days whose latest
    # request failed; the start day of the last round (None until one has run).
    _calendar_days: dict[str, tuple[str, list[dict[str, Any]]]] = field(default_factory=dict, repr=False)
    _calendar_failed: frozenset[str] = field(default=frozenset(), repr=False)
    _calendar_round: str | None = field(default=None, repr=False)
    _calendar_loaded: float | None = field(default=None, repr=False)  # fetched_at of the stored copy read last
    _calendar_due: float = 0.0
    _calendar_busy: bool = field(default=False, repr=False)
    _lpr_due: float = 0.0
    _lpr_busy: bool = field(default=False, repr=False)
    # The last LPR refresh failed (the rates then quote the cached LPR and say so, #516).
    _lpr_failed: bool = field(default=False, repr=False)
    # The calendar/LPR refresh tasks the loop started: close() cancels them with it.
    _refresh_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)
    # The worker threads of the loop's rounds (flash poll, calendar, LPR): a cancelled await does not
    # stop a thread, so stop() waits for them before the lease is released (bounded, STOP_WAIT_SECONDS).
    _threads: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    # The last flash poll: (ISO time, sources that failed); None until one has run.
    _flash_last_poll: tuple[str, list[str], int] | None = field(default=None, repr=False)
    # AI screening of product views (None: keyword recall only).
    screener: NewsScreener | None = None
    # Batch translation of English flash titles (None: originals shown).
    translator: NewsTranslator | None = None
    # 今日新闻要点 (None: no briefs): reads this service's events and calendar, wired below.
    analyst: NewsAnalyst | None = None
    rsshub_url: Callable[[], str] = lambda: os.getenv("RSSHUB_URL", "").strip()
    _shmet_cursor: int = field(default=0, repr=False)
    _shmet_due: float = field(default=0.0, repr=False)
    _rsshub_due: dict[str, float] = field(default_factory=dict, repr=False)
    # Routes whose fetch is still running (a timed-out route's abandoned thread included): a new
    # round of the same route waits until it truly ends, so stalls never pile up worker threads.
    _rsshub_busy: set[str] = field(default_factory=set, repr=False)
    _direct_due: dict[str, float] = field(default_factory=dict, repr=False)
    # Direct sources whose fetch is still running, for the same reason as the routes above.
    _direct_busy: set[str] = field(default_factory=set, repr=False)
    # The 新闻联播 day the last read asked for: at 19:45 the day turns and the read is due at once, not an
    # hour after a 19:44 read of the day before (#853).
    _cctv_day: date_cls | None = field(default=None, repr=False)
    _names: dict[str, str] = field(default_factory=dict, repr=False)
    _source_calls: _SourceCalls = field(default_factory=_SourceCalls, repr=False)  # rates / hog source calls
    # 快讯整表的解析副本（2026-10-22 调研台提速，所有读者共用）：(计数器, 行数) 没变就直接用；变了只解析新增或
    # 改过的行（rev 变了的），删掉的行随之离开。id → (rev, item)，以及按 (published_at DESC, id) 排好的列表。
    _rows_version: tuple[int, int] | None = field(default=None, repr=False)
    _rows_by_id: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict, repr=False)
    _rows_sorted: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # One rebuild at a time; the store lock is held only to read, never while parsing or sorting.
    _rows_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        # The last good LPR / calendar / rates / hog and the last flash poll, in research_live_cache.
        self._kept = LastGood(self.connection, self.lock, clock=lambda: self.clock())
        # Rates and the hog price: on-demand shared reads over the same store, one lane of their own.
        self._collect = Collector(self._kept, lane="research-live", workers=2, error_prefix="research_source_error",
                                  passthrough=frozenset({"research_dependency_missing"}))
        if self.screener is not None:
            self.screener.product_groups = lambda product: tuple(FLASH_PRODUCT_EXCLUDES.get(product, {}))
        if self.analyst is not None:
            self.analyst.events_for = self.product_events
            self.analyst.calendar_for = self.coming_calendar
            if self.screener is not None:
                self.analyst.active = self.screener.active
                self.screener.on_judged = self.analyst.wake
        with self.lock, self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS news_flash (id TEXT PRIMARY KEY, source TEXT NOT NULL,"
                " sources_json TEXT NOT NULL, published_at TEXT NOT NULL, title TEXT NOT NULL,"
                " text TEXT NOT NULL, link TEXT, products_json TEXT NOT NULL, tags_json TEXT NOT NULL)"
            )
            # A title merge files a syndicated body under the notice's item; this table keeps that
            # alias across polls (the stored text is the notice's title, not that body).
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS news_flash_alias (key TEXT PRIMARY KEY, id TEXT NOT NULL)"
            )
            # 2026-10-11 英文外电：翻译后的中文标题写进 title，原英文标题留在这里（界面可折叠看原文）。
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(news_flash)")}
            if "orig_title" not in columns:
                self.connection.execute("ALTER TABLE news_flash ADD COLUMN orig_title TEXT")
            # 快讯游标增量 (2026-10-12): every insert and every change of a row's content (a merged source, a
            # translation, the earliest copy's body) takes the next number of one counter that never goes
            # back — not MAX(rev), which a prune could lower. The triggers number every writer (the merge,
            # the translator) at the table itself; `epoch` names this counter so a rebuilt table cannot
            # be mistaken for the one a reader's numbers came from.
            if "rev" not in columns:
                self.connection.execute("ALTER TABLE news_flash ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS news_flash_seq (one INTEGER PRIMARY KEY CHECK (one = 1),"
                " epoch TEXT NOT NULL, n INTEGER NOT NULL)")
            self.connection.execute("INSERT OR IGNORE INTO news_flash_seq VALUES (1, lower(hex(randomblob(6))), 0)")
            # 读取（快讯列表、筛选队列、翻译队列）与清理都按 published_at 范围加倒序取行：没有这条索引就是整表
            # 扫描加临时排序（媒体 12000 行 + 官方 3000 行后更明显）。官方判断在 sources_json 里，不是列，无复合索引。
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS news_flash_published ON news_flash (published_at DESC, id)")
            for name, when in (("news_flash_rev_insert", "AFTER INSERT ON news_flash"),
                               ("news_flash_rev_update", "AFTER UPDATE OF " + _ROW_CONTENT + " ON news_flash")):
                self.connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {name} {when} BEGIN"
                    " UPDATE news_flash_seq SET n = n + 1 WHERE one = 1;"
                    " UPDATE news_flash SET rev = (SELECT n FROM news_flash_seq WHERE one = 1) WHERE id = NEW.id;"
                    " END")
        if self.state_root is not None and not (self.state_root / ALIASES_FILENAME).exists():
            try:
                self.state_root.mkdir(parents=True, exist_ok=True)
                (self.state_root / ALIASES_FILENAME).write_text(
                    json.dumps(FLASH_ALIASES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass  # read-only state: the built-in table stands in
        lpr = self._cache_get("lpr")
        if lpr is not None:
            self._lpr_due = 0.0  # a cached LPR is served at once; the first poll refreshes it

    # -- background loops: only the instance-lease holder runs them (wiring/producers.py), never in a
    # replay. The holder writes the feed, the calendar and the poll status into the shared database;
    # every process (the standby slot included) reads them from there.

    def start(self) -> None:
        if self._loop is None:
            self._loop = asyncio.create_task(self._run_loops())
        if self.screener is not None:
            self.screener.start()
        if self.analyst is not None:
            self.analyst.start()
        if self.translator is not None:
            self.translator.start()

    async def stop(self) -> None:
        """Stop the background work (the lease moved away); `start` may run it again later."""
        if self.analyst is not None:
            await self.analyst.close()
        if self.screener is not None:
            await self.screener.close()
        if self.translator is not None:
            await self.translator.close()
        loop, self._loop = self._loop, None
        refresh = list(self._refresh_tasks)
        for task in refresh:
            task.cancel()
        if loop is not None:
            loop.cancel()
        await asyncio.gather(*([loop] if loop else []), *refresh, return_exceptions=True)
        # A round already in its worker thread still fetches and writes the feed/cache: wait for it, so
        # the next holder's first round never runs alongside it or is overwritten by an older result.
        threads = list(self._threads)
        if threads:
            _, running = await asyncio.wait(threads, timeout=STOP_WAIT_SECONDS)
            if running:
                log.warning("research_live_stop_threads_still_running count=%d", len(running))

    async def _in_thread(self, work: Callable[[], Any]) -> Any:
        """`work` in a worker thread that stop() can wait for: cancelling the caller leaves the thread's
        task tracked until it really ends."""
        task = asyncio.ensure_future(asyncio.to_thread(work))
        self._threads.add(task)
        task.add_done_callback(self._threads.discard)
        return await asyncio.shield(task)

    async def close(self) -> None:
        await self.stop()
        self._collect.close()

    async def _run_loops(self) -> None:
        """Flash at once and then every minute; the calendar every 6 h and LPR once a day piggyback.
        The first round runs before the first sleep, so a freshly started runtime does not sit
        empty for a whole poll interval after every launch."""
        try:
            # Off the event loop: the akshare import is heavy and must never stall the runtime's loop.
            await asyncio.to_thread(self.sources.module)
        except RuntimeError as error:
            log.info("research_live_disabled error=%s", error)  # no akshare: no background work at all
            return
        while True:
            try:
                await self._in_thread(self.poll_flash_once)
            except Exception:  # noqa: BLE001 — one bad round never ends the loop
                log.warning("research_flash_poll_failed", exc_info=True)
            now = self.clock()
            # A new Beijing day starts a round at once: the window must not end a day short.
            new_day = self._calendar_round not in (None, self.now().astimezone(CHINA).date().isoformat())
            if (now >= self._calendar_due or new_day) and not self._calendar_busy:
                self._calendar_busy = True
                self._track_refresh(asyncio.create_task(self._refresh_on_loop(
                    "calendar", self.refresh_calendar, CALENDAR_TTL_SECONDS, CALENDAR_RETRY_SECONDS)))
            if now >= self._lpr_due and not self._lpr_busy:
                self._lpr_busy = True
                self._track_refresh(asyncio.create_task(self._refresh_on_loop(
                    "lpr", self.refresh_lpr, LPR_TTL_SECONDS, LPR_RETRY_SECONDS)))
            await asyncio.sleep(self.poll_seconds)

    def _track_refresh(self, task: asyncio.Task[None]) -> None:
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    async def _refresh_on_loop(self, kind: str, refresh: Callable[[], bool | None], ttl: float, retry: float) -> None:
        # `due` is bound before the try so a cancellation (close()) still clears the busy flag in
        # the finally below instead of dying on an unbound name.
        due = self.clock() + retry
        try:
            complete = await self._in_thread(refresh)
            # A round that only partly answered (False) is retried on the failure interval.
            due = self.clock() + (retry if complete is False else ttl)
            if kind == "lpr":
                self._lpr_failed = False
        except Exception:  # noqa: BLE001 — a failed refresh keeps the last cache and retries later
            log.warning("research_live_refresh_failed kind=%s", kind, exc_info=True)
            due = self.clock() + retry
            if kind == "lpr":
                self._lpr_failed = True
        finally:
            if kind == "calendar":
                self._calendar_due, self._calendar_busy = due, False
            else:
                self._lpr_due, self._lpr_busy = due, False

    # -- flash

    def flash_sources(self) -> list[str]:
        """Every source the feed reads, in canonical merge order: exchanges and ministries first
        (a syndicated story is filed under the official source), then the media wires. The RSSHub
        and direct sources only exist when `RSSHUB_URL` names the self-hosted RSSHub (云端 Runtime)."""
        cloud = self.rsshub_url()
        rsshub_names = list(dict.fromkeys(source for source, _, _ in RSSHUB_SOURCES)) if cloud else []
        direct_names = [name for source, _ in DIRECT_SOURCES for name in SPLIT_SOURCES.get(source, (source,))] if cloud else []
        added = [*rsshub_names, *direct_names]
        names = [source for source in OFFICIAL_SOURCES if source in added] + list(FLASH_SOURCES)
        names += [source for source in added if source not in OFFICIAL_SOURCES]
        return list(dict.fromkeys(names))

    def poll_flash_once(self) -> dict[str, Any]:
        """One round: the sources concurrently, each with its own timeout, merged and pruned.
        上海金属网 reads one metal when its interval is due, in turn; an RSSHub route only when its interval is due."""
        self.sources.module()  # RuntimeError("research_dependency_missing") stops the caller politely
        shmet_due = self._shmet_due <= self.clock()
        metals = [SHMET_SYMBOLS[(self._shmet_cursor + offset) % len(SHMET_SYMBOLS)] for offset in range(SHMET_PER_ROUND)]
        if shmet_due:
            self._shmet_due = self.clock() + SHMET_EVERY_SECONDS
            self._shmet_cursor = (self._shmet_cursor + SHMET_PER_ROUND) % len(SHMET_SYMBOLS)

        def shmet() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            failures = 0
            for symbol, products in metals:
                try:
                    rows += [{**row, "_products": products} for row in _records(self.sources.flash_shmet(symbol))]
                except Exception as error:  # noqa: BLE001 — the other metal still counts
                    failures += 1
                    log.warning("research_flash_source_failed source=上海金属网:%s error=%s", symbol, type(error).__name__)
            if failures == len(metals):
                raise LookupError("research_shmet_failed")
            return rows

        fetches: dict[str, Callable[[], Any]] = {
            "财联社": self.sources.flash_cls, "东方财富": self.sources.flash_em, "新浪": self.sources.flash_sina,
            "同花顺": self.sources.flash_ths, "富途": self.sources.flash_futu}
        if shmet_due:
            fetches["上海金属网"] = shmet
        base = self.rsshub_url()
        for source, route, every in RSSHUB_SOURCES if base else ():
            if self._rsshub_due.get(route, 0.0) > self.clock() or route in self._rsshub_busy:
                continue
            self._rsshub_due[route] = self.clock() + every
            self._rsshub_busy.add(route)
            fetches[f"{source}|{route}"] = functools.partial(self._rsshub_rows, base, route)
        beijing = self.now().astimezone(CHINA)
        cctv_day = beijing.date() if beijing.time() >= CCTV_READY else beijing.date() - timedelta(days=1)
        direct_calls: dict[str, Callable[[], Any]] = {
            "上期所": self.sources.notice_shfe, "广期所": self.sources.notice_gfex,
            EXCHANGE_BOARD: self.sources.notice_exchange_board,
            "上海有色网": self.sources.news_smm, "我的钢铁网": self.sources.news_mysteel,
            "FinancialJuice": self.sources.flash_financialjuice,
            "证券时报": self.sources.news_stcn, "汇通": self.sources.news_fx678,
            "澎湃": self.sources.news_thepaper, "每经": self.sources.news_nbd,
            "新闻联播": lambda: cctv_rows(self.sources.news_cctv(cctv_day), cctv_day)}
        for source, every in DIRECT_SOURCES if base else ():
            turned = source == "新闻联播" and self._cctv_day != cctv_day
            if (self._direct_due.get(source, 0.0) > self.clock() and not turned) or source in self._direct_busy:
                continue
            if source == "新闻联播":
                self._cctv_day = cctv_day
            self._direct_due[source] = self.clock() + every
            self._direct_busy.add(source)
            fetches[source] = functools.partial(self._direct_rows, source, direct_calls[source])
        collected: dict[str, Any] = {}

        def fetch(source: str, call: Callable[[], Any]) -> None:
            try:
                collected[source] = _with_timeout(FLASH_SOURCE_TIMEOUT_SECONDS, f"flash-{source}", call)
            except Exception as error:  # noqa: BLE001 — one source failing keeps the others
                quiet = "|" in source or source in DIRECT_SOURCE_NAMES
                # RSSHub or a direct notice page down (blocked, restarting) is quiet: the feed just
                # lacks those sources and the failed one is tried again next round.
                log.log(logging.INFO if quiet else logging.WARNING, "research_flash_source_failed source=%s error=%s",
                        source, type(error).__name__)
                if quiet:
                    if "|" in source:
                        self._rsshub_due[source.split("|", 1)[1]] = 0.0
                    else:
                        self._direct_due[source] = 0.0

        pool = ThreadPoolExecutor(max_workers=len(fetches), thread_name_prefix="research-live-flash")
        try:
            futures = [pool.submit(fetch, source, call) for source, call in fetches.items()]
            for future in futures:
                future.result(timeout=FLASH_SOURCE_TIMEOUT_SECONDS + 5)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        # The feed reports which sources failed this round and when it ran (#516-7).
        # A split read (the exchange board) answers for each source it files.
        attempted = list(dict.fromkeys(name for key in fetches for name in _read_sources(key)))
        answered = {name for key in collected for name in _read_sources(key)}
        self._flash_last_poll = (self.now().isoformat(timespec="seconds"),
                                 [source for source in attempted if source not in answered], len(attempted))
        self._cache_put("flash_last_poll", {"at": self._flash_last_poll[0], "failed": self._flash_last_poll[1],
                                            "attempted": self._flash_last_poll[2]})
        aliases = self._aliases()
        since = self.now().astimezone(CHINA) - timedelta(hours=media_retain_hours())
        official_since = self.now().astimezone(CHINA) - timedelta(days=official_retain_days())
        until = self.now().astimezone(CHINA) + FLASH_FUTURE_LIMIT
        # One read that files several sources (the exchange board: 郑商所 and 大商所) is split by source here.
        for key in [key for key in collected if key in SPLIT_SOURCES]:
            parts = collected.pop(key)
            for source, rows in (parts.items() if isinstance(parts, dict) else ()):
                collected[f"{source}|{key}"] = rows
        # Canonical source order for the merge: which scraper answered first must not decide
        # whose name a syndicated story is filed under.
        incoming: dict[str, list[dict[str, Any]]] = {}
        for source in self.flash_sources():
            for key, rows in collected.items():
                if key.split("|", 1)[0] == source:
                    window = official_since if source in OFFICIAL_SOURCE_NAMES else since
                    incoming.setdefault(source, []).extend(normalise_flash_rows(source, rows, window, until))
        candidates = [candidate for items in incoming.values() for candidate in items]
        stored, merged_sources = self._merge(candidates, aliases)
        self._prune()
        self._screen_active()
        self._queue_translations()
        return {"collected": {source: len(items) for source, items in incoming.items()},
                "stored": stored, "mergedSources": merged_sources}

    def ingest_search(self, product: str, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Search candidates enter the same recall, merge and AI screening as every feed."""
        now = self.now().astimezone(CHINA)
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        incoming = normalise_flash_rows("全网搜索", rows, since, now)  # the source name the news list shows
        stored, merged = self._merge(incoming, self._aliases())
        self._prune()
        self._product_view(product, 24, urgent=False)
        return {"stored": stored, "mergedSources": merged}

    def _rsshub_rows(self, base: str, route: str) -> list[dict[str, Any]]:
        # Busy holds until the request truly ends (its own lane, its own timeout): a route stalled
        # past the poll's timeout is not started again while its abandoned thread still runs.
        try:
            return parse_rsshub_feed(self.sources.rsshub(base, route))
        finally:
            self._rsshub_busy.discard(route)

    def _direct_rows(self, source: str, call: Callable[[], Any]) -> Any:
        # The same in-flight guard the routes carry: a direct page stalled past the poll's timeout
        # is not started again while its abandoned thread still runs (it queues on the host lock).
        try:
            return call()
        finally:
            self._direct_busy.discard(source)

    def _queue_translations(self) -> None:
        """Offer the untranslated English items of the window (tagged products or macro) to the
        translator; anything it cannot translate keeps showing its original title."""
        if self.translator is None:
            return
        cutoff = (self.now().astimezone(CHINA) - timedelta(hours=media_retain_hours())).isoformat(timespec="seconds")
        # English first, the 200-item cap second — and paged, not one LIMIT: newer Chinese rows must
        # not fill any pre-filter cap ahead of older untranslated English ones (is_english_title is
        # Python, not SQL). The table is bounded (FLASH_MAX_ITEMS media rows plus official_max_items()
        # official notices), so the paging ends.
        english: list[dict[str, str]] = []
        offset = 0
        while len(english) < 200:
            with self.lock:
                rows = self.connection.execute(
                    f"SELECT {_ITEM_COLUMNS} FROM news_flash WHERE published_at >= ? AND orig_title IS NULL"
                    " AND id NOT IN (SELECT item_id FROM news_translation)"
                    " AND (products_json != '[]' OR tags_json != '[]')"
                    " ORDER BY published_at DESC LIMIT 500 OFFSET ?", (cutoff, offset)).fetchall()
            if not rows:
                break
            english.extend({"id": row[0], "title": row[4]} for row in rows if is_english_title(row[4]))
            offset += len(rows)
        pending = english[:200]
        try:
            self.translator.offer(pending)
        except Exception:  # noqa: BLE001 — translation never breaks the poll
            log.warning("research_flash_translate_queue_failed", exc_info=True)

    def _screen_active(self) -> None:
        """Queue the new items of products read in the last hours (background, batched). A product a reader is
        looking at right now (possibly through the other slot) with items waiting starts a round soon, as the
        reader's own request would have in this process."""
        if self.screener is None:
            return
        looked = self.screener.looked()
        waiting = False
        for product, _ in self.screener.active():
            try:
                view = self._product_view(product, 24, urgent=False)
            except Exception:  # noqa: BLE001 — screening never breaks the poll
                log.warning("research_flash_screen_queue_failed product=%s", product, exc_info=True)
                continue
            waiting = waiting or (product in looked and view["pending"] > 0)
        if waiting:
            self.screener.wake()

    def _aliases(self) -> list[tuple[str, tuple[str, ...]]]:
        """(productId, words) built from the instrument catalog's product names plus the alias table."""
        cached = self._alias_index
        if cached is not None and cached[0] > self.clock():
            return cached[1]
        table: dict[str, list[str]] = {}
        catalog_names: dict[str, str] = {}
        if self.instrument_catalog is not None:
            try:
                items = self.instrument_catalog().get("items") or []
            except Exception:  # noqa: BLE001 — a missing catalog only loses the names
                items = []
            for item in items:
                if not isinstance(item, dict) or not item.get("isMain"):
                    continue
                product = _text(item.get("productId")).lower()
                name = re.sub(r"[0-9]+$", "", _text(item.get("instrumentName"))).strip()
                if product and name and len(name) >= 2:
                    catalog_names[product] = name
                    table.setdefault(product, []).insert(0, name)
        overlay: dict[str, list[str]] = {}
        if self.state_root is not None:
            try:
                loaded = json.loads((self.state_root / ALIASES_FILENAME).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
            overlay = {str(key).lower(): [str(word) for word in words if word]
                       for key, words in (loaded if isinstance(loaded, dict) else {}).items() if isinstance(words, list)}
        table.update(overlay)  # a product the file names keeps the file's list, not the built-in one
        for product, words in FLASH_ALIASES.items():
            if product in overlay:
                continue
            merged = table.get(product, [])
            table[product] = list(dict.fromkeys([*merged, *words]))
        for product, name in catalog_names.items():  # the catalog's own name always leads
            words = table.setdefault(product, [])
            if name not in words:
                words.insert(0, name)
        index = sorted((product, tuple(dict.fromkeys(words))) for product, words in table.items() if words)
        self._alias_index = (self.clock() + FLASH_TTL_INDEX_SECONDS, index)
        self._names = {**{product: words[0] for product, words in table.items() if words}, **catalog_names}
        return index

    def _merge(self, incoming: list[dict[str, Any]], aliases: list[tuple[str, tuple[str, ...]]]) -> tuple[int, int]:
        """Cross-source dedup on the normalised body's first 40 characters — a title-only notice and
        a syndicated copy with a rewritten body also meet on their punctuation-free title: the
        earliest copy wins and carries every source that ran the story."""
        # One write transaction from the read to the last upsert: only the lease holder polls, but at a
        # handoff two processes may merge at once, and neither may write back a table the other changed
        # since it read it (a lost sources_json update, a duplicate row). The alias tagging (the costly part)
        # runs before it, so the shared store lock is held only for the read, the index and the writes.
        # 成批的单厂报价按来源规则不进库（SOURCE_TITLE_EXCLUDES）。
        incoming = [candidate for candidate in incoming if flash_key(candidate["text"])
                    and not source_excluded(candidate["source"], candidate["title"], candidate["text"])]
        for candidate in incoming:
            tag_flash_item(candidate, aliases)
        # 综合财经媒体只留与期货相关的条目（打上品种或宏观标签）。
        incoming = [candidate for candidate in incoming if candidate["source"] not in TAGGED_ONLY_SOURCES
                    or candidate["products"] or "macro" in candidate["tags"]]
        with self.lock, self.connection:
            if not self.connection.in_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            return self._merge_locked(incoming, aliases)

    def _merge_locked(self, incoming: list[dict[str, Any]],
                      aliases: list[tuple[str, tuple[str, ...]]]) -> tuple[int, int]:
        rows = self.connection.execute(f"SELECT {_ITEM_COLUMNS} FROM news_flash").fetchall()
        alias_rows = self.connection.execute("SELECT key, id FROM news_flash_alias").fetchall()
        by_id: dict[str, dict[str, Any]] = {}
        by_key: dict[str, dict[str, Any]] = {}
        by_title: dict[str, dict[str, Any]] = {}
        # 原始来源关联：官方源 → 它的条目；官方源 → 标着“据:它”的媒体条目。
        official_rows: dict[str, list[dict[str, Any]]] = {}
        cited_rows: dict[str, list[dict[str, Any]]] = {}

        def index_origin(item: dict[str, Any]) -> None:
            for source in item["sources"]:
                if source in OFFICIAL_SOURCE_NAMES:
                    official_rows.setdefault(source, []).append(item)
            for cited in _cited_sources(item):
                cited_rows.setdefault(cited, []).append(item)

        for row in rows:
            item = _item_from_row(row)
            by_id[item["id"]] = item
            by_key.setdefault(flash_key(item["text"]), item)
            title_id = merge_title_key(item["title"])
            if title_id:
                by_title.setdefault(title_id, item)
            index_origin(item)
        # Body aliases filed by earlier title merges: the stored text is the notice's title, so the
        # syndicated body only finds its story through this table when the indexes are rebuilt.
        for alias_key, item_id in alias_rows:
            filed = by_id.get(item_id)
            if filed is not None:
                by_key.setdefault(alias_key, filed)
        dirty: set[int] = set()
        new_aliases: list[tuple[str, str]] = []
        # Stored rows folded into another one this round (further retellings of one official notice, #853):
        # deleted, their alias keys moved to the row they joined.
        absorbed: dict[str, str] = {}
        stored, merged_sources = 0, 0

        def fold(known: dict[str, Any], other: dict[str, Any], key: str) -> int:
            """Merge a copy of the story (a candidate, or a stored row joining it) into `known`: its sources,
            products and tags, its body key as an alias; the earliest copy wins the body. The number of
            sources it added."""
            added = 0
            by_key.setdefault(key, known)
            if key != flash_key(known["text"]):
                new_aliases.append((key, known["id"]))
            for source in other.get("sources") or [other["source"]]:
                if source not in known["sources"]:
                    known["sources"].append(source)
                    added += 1
                    dirty.add(id(known))
            if not set(other["products"]) <= set(known["products"]):  # 上海金属网 names the metal
                known["products"] = sorted({*known["products"], *other["products"]})
                dirty.add(id(known))
            if not set(other["tags"]) <= set(known["tags"]):  # a later exchange notice adds 交易所
                known["tags"] = list(dict.fromkeys([*known["tags"], *other["tags"]]))
                dirty.add(id(known))
            # “据:国家统计局” on a row that now carries 国家统计局's own copy says nothing more.
            told = [tag for tag in known["tags"] if not (tag.startswith(ORIGIN_TAG) and ORIGIN_SOURCE.get(
                tag[len(ORIGIN_TAG):], tag[len(ORIGIN_TAG):]) in known["sources"])]
            if told != known["tags"]:
                known["tags"] = told
                dirty.add(id(known))
            if other["publishedAt"] < known["publishedAt"]:
                # The earliest copy wins the body, but a title already translated keeps its Chinese
                # (origTitle present): the original English only rides along untranslated items.
                displaced = flash_key(known["text"])
                if displaced and displaced != flash_key(other["text"]):
                    # The body being replaced was this row's canonical key until now: file it as an
                    # alias too, or the next index rebuild loses it and a later copy of that body
                    # inserts as a duplicate (the other copy's own key is filed above).
                    new_aliases.append((displaced, known["id"]))
                known.update({key_: other[key_] for key_ in ("publishedAt", "link", "source")})
                known["text"] = other["text"]
                if not known.get("origTitle"):
                    known["title"] = other["title"]
                dirty.add(id(known))
            return added

        for candidate in incoming:
            key = flash_key(candidate["text"])
            if not key:
                continue
            known = by_key.get(key)
            if known is None:
                # A title-only notice and a syndicated copy with a rewritten body still meet on
                # their (punctuation-free) title — but only when one of the two is title-only, so
                # two media rewrites with the same headline stay apart unless their bodies match.
                match = by_title.get(merge_title_key(candidate["title"]))
                if match is not None and (_title_only(candidate) or _title_only(match)):
                    known = match
            extras: list[dict[str, Any]] = []
            if known is None:
                # A report that names an official source we read (国家统计局数据显示…) and retells one of its
                # items meets that item — whichever of the two arrives first; an official notice arriving
                # after several such reports takes them all into one row.
                matched = _cited_match(candidate, official_rows, cited_rows, aliases)
                if matched:
                    known, extras = matched[0], matched[1:]
            if known is None:
                fresh = {**candidate, "sources": [candidate["source"]]}
                by_key[key] = fresh
                title_id = merge_title_key(candidate["title"])
                if title_id:
                    by_title.setdefault(title_id, fresh)
                index_origin(fresh)
                dirty.add(id(fresh))
                stored += 1
                continue
            # A title merge also files the candidate's body key: a second syndicated copy sharing
            # that body (under yet another headline) finds the item either way — and the alias is
            # persisted, because the stored text is the notice's title, not this body.
            merged_sources += fold(known, candidate, key)
            for extra in extras:
                # Another stored retelling of the same notice joins the row too: its sources, products, tags
                # and body key, and every alias it held (moved below); the row itself goes.
                merged_sources += fold(known, extra, flash_key(extra["text"]))
                absorbed[extra["id"]] = known["id"]
                for alias, item in list(by_key.items()):
                    if item is extra:
                        by_key[alias] = known
                for title_id, item in list(by_title.items()):
                    if item is extra:
                        by_title[title_id] = known
                for listed in (*official_rows.values(), *cited_rows.values()):
                    listed[:] = [row for row in listed if row is not extra]
                dirty.discard(id(extra))
        for item in by_key.values():
            if id(item) not in dirty:
                continue
            self.connection.execute(
                "INSERT INTO news_flash (id, source, sources_json, published_at, title, text, link,"
                " products_json, tags_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET source = excluded.source, sources_json = excluded.sources_json,"
                " published_at = excluded.published_at, title = excluded.title, text = excluded.text,"
                " link = excluded.link, products_json = excluded.products_json, tags_json = excluded.tags_json",
                (item["id"], item["source"], json.dumps(item["sources"], ensure_ascii=False),
                 item["publishedAt"], item["title"], item["text"], item.get("link"),
                 json.dumps(item["products"], ensure_ascii=False), json.dumps(item["tags"], ensure_ascii=False)))
        if absorbed:
            self.connection.executemany("UPDATE OR IGNORE news_flash_alias SET id = ? WHERE id = ?",
                                        [(into, gone) for gone, into in absorbed.items()])
            self.connection.executemany("DELETE FROM news_flash WHERE id = ?", [(gone,) for gone in absorbed])
        if new_aliases:
            self.connection.executemany("INSERT OR IGNORE INTO news_flash_alias (key, id) VALUES (?, ?)",
                                        [(key, absorbed.get(item_id, item_id)) for key, item_id in new_aliases])
        return stored, merged_sources

    def _prune(self) -> None:
        """Media rows: `media_retain_hours()` (72 by default) and the newest FLASH_MAX_ITEMS (the row cap backs
        up the time cutoff; the time cutoff runs first). Official rows (any official source,
        syndicated copies merged in included): `official_retain_days()` (30 by default) and the newest
        `official_max_items()`, a cap of their own — so the table stays bounded either way."""
        # published_at is always Beijing (+08:00); the cutoff must be too, or the string
        # comparison in sqlite would ignore the offsets.
        now = self.now().astimezone(CHINA)
        cutoff = (now - timedelta(hours=media_retain_hours())).isoformat(timespec="seconds")
        official_cutoff = (now - timedelta(days=official_retain_days())).isoformat(timespec="seconds")
        with self.lock, self.connection:
            # Each kind by its own age: an official retention shorter than the media's window must not
            # take media rows with it.
            self.connection.execute(f"DELETE FROM news_flash WHERE published_at < ? AND {_OFFICIAL_ROW}",
                                    (official_cutoff, *OFFICIAL_SOURCE_NAMES))
            self.connection.execute(f"DELETE FROM news_flash WHERE published_at < ? AND NOT {_OFFICIAL_ROW}",
                                    (cutoff, *OFFICIAL_SOURCE_NAMES))
            # Rows stored before a source's title rule existed (or changed) leave with the next prune; a row
            # another source also ran stays.
            excluded = [row[0] for row in self.connection.execute(
                f"SELECT id, source, sources_json, title, text FROM news_flash WHERE source IN"
                f" ({','.join('?' * len(SOURCE_TITLE_EXCLUDES))})", tuple(SOURCE_TITLE_EXCLUDES))
                if json.loads(row[2] or "[]") in ([], [row[1]]) and source_excluded(row[1], row[3], row[4])]
            self.connection.executemany("DELETE FROM news_flash WHERE id = ?", [(item_id,) for item_id in excluded])
            self.connection.execute(
                f"DELETE FROM news_flash WHERE id IN (SELECT id FROM news_flash WHERE NOT {_OFFICIAL_ROW}"
                " ORDER BY published_at DESC, id LIMIT -1 OFFSET ?)", (*OFFICIAL_SOURCE_NAMES, FLASH_MAX_ITEMS))
            self.connection.execute(
                f"DELETE FROM news_flash WHERE id IN (SELECT id FROM news_flash WHERE {_OFFICIAL_ROW}"
                " ORDER BY published_at DESC, id LIMIT -1 OFFSET ?)", (*OFFICIAL_SOURCE_NAMES, official_max_items()))
            self.connection.execute("DELETE FROM news_flash_alias WHERE id NOT IN (SELECT id FROM news_flash)")
            kept = [row[0] for row in self.connection.execute("SELECT id FROM news_flash")]
        if self.screener is not None:
            # An official notice outlives the screener's own judged_at window: its judgments stay with it.
            self.screener.prune(keep=kept)
        if self.translator is not None:
            self.translator.prune()

    def _parsed_rows(self) -> tuple[list[dict[str, Any]], dict[str, tuple[int, dict[str, Any]]]]:
        """Every stored row parsed, newest first (published_at DESC, id), and id → (rev, item): one copy all
        readers share. Every insert and content change bumps the table's counter and a prune or merge changes
        the row count, so an unchanged (counter, count) is an unchanged table; otherwise only the rows whose
        `rev` moved are read and parsed again (2026-10-22: each product view used to parse the whole window).
        The store lock (the whole database's) is held only while reading; parsing and sorting run outside it."""
        return self._parsed_rows_versioned()[:2]

    def _parsed_rows_versioned(self) -> tuple[list[dict[str, Any]], dict[str, tuple[int, dict[str, Any]]], tuple[int, int]]:
        """`_parsed_rows` plus the (counter, count) the copy was read at."""
        with self._rows_guard:
            with self.lock:
                version = tuple(self.connection.execute(
                    "SELECT (SELECT n FROM news_flash_seq WHERE one = 1), (SELECT count(*) FROM news_flash)").fetchone())
                if version == self._rows_version:
                    return self._rows_sorted, self._rows_by_id, version
                stamps = dict(self.connection.execute("SELECT id, rev FROM news_flash").fetchall())
                known = self._rows_by_id
                stale = [item_id for item_id, rev in stamps.items() if known.get(item_id, (None,))[0] != rev]
                raw = []
                for start in range(0, len(stale), 500):
                    chunk = stale[start:start + 500]
                    marks = ", ".join("?" * len(chunk))
                    raw.extend(self.connection.execute(
                        f"SELECT {_ITEM_COLUMNS}, rev FROM news_flash WHERE id IN ({marks})", chunk).fetchall())
            fresh = {row[0]: (int(row[10]), _item_from_row(row[:10])) for row in raw}
            by_id = {item_id: fresh.get(item_id) or known[item_id] for item_id in stamps if item_id in fresh or item_id in known}
            ordered = sorted((entry[1] for entry in by_id.values()), key=lambda item: item["id"])
            ordered.sort(key=lambda item: item["publishedAt"], reverse=True)
            self._rows_version, self._rows_by_id, self._rows_sorted = version, by_id, ordered
            return ordered, by_id, version

    def _flash_rows(self, product: str | None, tag: str | None, hours: int, limit: int | None,
                    products: frozenset[str] | None = None, official: bool = False) -> list[dict[str, Any]]:
        """The window's items, newest first; `limit=None` is the whole window; `official` only the rows
        an official source (exchange, ministry) carries. A product or tag filter scans the whole window: a
        related item older than the newest `limit` rows must still be found. Read from the shared parsed copy
        (`_parsed_rows`); each caller gets its own copies of the items it keeps."""
        cutoff = (self.now().astimezone(CHINA) - timedelta(seconds=hours * 3600)).isoformat(timespec="seconds")
        wanted = products if products is not None else (frozenset({product}) if product else None)
        rows, _ = self._parsed_rows()
        kept: list[dict[str, Any]] = []
        for item in rows:
            if item["publishedAt"] < cutoff:
                break
            if official and _OFFICIAL_NAMES.isdisjoint(item["sources"]):
                continue
            if wanted and wanted.isdisjoint(item["products"]):
                continue
            if tag and tag not in item["tags"]:
                continue
            kept.append(_item_copy(item))
            if limit is not None and len(kept) >= limit:
                break
        return kept

    def product_name(self, product: str) -> str:
        if not self._names:
            self._aliases()
        return self._names.get(product, product.upper())

    def screen_items(self, product: str, candidates: list[tuple[dict[str, Any], bool]], *,
                     urgent: bool = True) -> dict[str, Any]:
        """A product's screening of recalled candidates (快讯 or 相关报道): see news_screen.screen_view."""
        return screen_view(self.screener, product, self.product_name(product), candidates,
                           lambda item: keyword_drop(item, product), urgent=urgent)

    def _product_view(self, product: str, hours: int, *, urgent: bool = True) -> dict[str, Any]:
        """One product's items (recalled by its words) plus the newest macro items offered to the model."""
        direct = self._flash_rows(product, None, hours, None)
        seen = {item["id"] for item in direct}
        macro = [item for item in self._flash_rows(None, "macro", hours, MACRO_CANDIDATES) if item["id"] not in seen]
        candidates = sorted([(item, True) for item in direct] + [(item, False) for item in macro],
                            key=lambda pair: (pair[0]["publishedAt"], pair[0]["id"]), reverse=True)
        return self.screen_items(product, candidates, urgent=urgent)

    def _products_view(self, products: list[str], hours: int) -> dict[str, Any]:
        """Several products' recalled items (持仓与自选): kept when any of them keeps it."""
        if len(products) == 1:
            return self._product_view(products[0], hours)
        views = [self.screen_items(product, [(item, True) for item in self._flash_rows(product, None, hours, None)])
                 for product in products]
        kept: dict[str, dict[str, Any]] = {}
        dropped: dict[str, dict[str, Any]] = {}
        for view in views:
            for item in view["kept"]:
                current = kept.get(item["id"])
                if current is None or (current["ai"] is None and item["ai"] is not None):
                    kept[item["id"]] = item
            for item in view["dropped"]:
                dropped.setdefault(item["id"], item)
        return {"kept": sorted(kept.values(), key=lambda item: (item["publishedAt"], item["id"]), reverse=True),
                "dropped": [item for key, item in dropped.items() if key not in kept],
                "pending": sum(view["pending"] for view in views),
                "unscreened": sum(view["unscreened"] for view in views),
                "mode": "ai" if all(view["mode"] == "ai" for view in views) else "keyword",
                "reason": next((view["reason"] for view in views if view["reason"]), None)}

    def flash_feed(self, product: str | None = None, tag: str | None = None,
                   hours: int = 24, limit: int = 50, products: list[str] | None = None,
                   official: bool = False) -> dict[str, Any]:
        """The feed, newest first. With `product` (or several `products`) it is that product's
        screened view: its recalled items (and macro items the model kept for it), each with an `ai`
        reading, plus `screen` — mode (ai / keyword), pending count and the dropped groups and items.
        `official` lists only the official sources' rows (exchanges, ministries) over up to
        FLASH_OFFICIAL_HOURS_MAX hours — they are kept 30 days — with no product filter: `product` /
        `products` are ignored, since many notices carry no product tag (#853); anything else reads at
        most 72 hours."""
        try:
            self.sources.module()
        except RuntimeError as error:
            return {"status": "unavailable", "error": str(error), "items": [], "count": 0}
        hours = max(1, min(hours, FLASH_OFFICIAL_HOURS_MAX if official else 72))
        keys = [] if official else [key for key in dict.fromkeys(product_key(value) for value in (products or ([product] if product else []))) if key]
        screen: dict[str, Any] | None = None
        events: list[dict[str, Any]] | None = None
        if official:
            items = self._flash_rows(None, tag, hours, limit, official=True)
        elif keys:
            view = self._products_view(keys, hours)
            items = view["kept"][:limit]
            # 同一件事合成一个事件，分重要 / 其他 / 次要；ai 模式下模型还没判的条目不进事件（screen.pending 计数）。
            kept = [item for item in view["kept"] if tag in item["tags"]] if tag else view["kept"]
            events = build_events(kept, hide_pending=view["mode"] == "ai")
            if tag:
                items = [item for item in items if tag in item["tags"]]
            screen = screen_summary(view, view["dropped"], items)
        else:
            items = self._flash_rows(None, tag, hours, limit)
        feed: dict[str, Any] = {"status": "ok", "count": len(items), "hours": hours, "product": product, "tag": tag,
                                "items": items, "notice": FLASH_NOTICE, "sources": self.flash_sources()}
        if official:
            feed["official"] = True
        if products:
            feed["products"] = keys
        if screen is not None:
            feed["screen"] = screen
        if events is not None:
            feed["events"] = events
            feed["layers"] = layer_summary(events)
        if len(keys) == 1 and not products and not official:
            if self.analyst is not None:
                feed["brief"] = self.analyst.brief(keys[0])
            feed["tonight"] = self.tonight_calendar(keys[0])
        last = self._last_poll()
        if last is not None:
            feed["lastPoll"] = {"at": last[0], "failedSources": last[1]}
            # "Every source failed" is measured against the sources this poll actually attempted
            # (RSSHub and direct sources only run when configured and due), not a fixed list: with
            # the media six down but an exchange notice page still answering, the feed stands.
            if len(last[1]) >= last[2] and not items and not self._flash_rows(None, None, hours, 1):
                # Every source failed the last poll and nothing survives in the window: an explicit
                # failure, not an empty feed (an empty answer is only meaningful once one has run).
                feed["status"], feed["error"] = "unavailable", "research_flash_unavailable"
        return feed

    def compact_flash(self, feed: dict[str, Any], since: str | None, include: frozenset[str] = frozenset()) -> dict[str, Any]:
        """快讯游标增量 (2026-10-12): the feed with every item reduced to its own view fields plus `id` and
        `rev` — in `items`, `screen.dropped.items` and each event's `items` — and the rows' content once in
        `rows` ({id, rev, source, sources, publishedAt, title, text, link, products, tags, origTitle}), but
        only for rows the reader cannot hold: changed after its cursor `since` ("epoch:n"; absent, unknown
        or from another epoch means every row), or named in `include`. `cursor` is the counter now. The
        reader rebuilds each item from its copy of the row with that exact `rev`; a row it lacks is asked
        for again through `include`. An item whose content differs from the row read now (changed while
        this feed was built) keeps its content and is not reduced. An event whose title, text and origTitle
        are its headline report's drops them and names that report in `headline` (one of its items)."""
        lists: list[list[Any]] = []
        if isinstance(feed.get("items"), list):
            lists.append(feed["items"])
        dropped = (feed.get("screen") or {}).get("dropped")
        if isinstance(dropped, dict) and isinstance(dropped.get("items"), list):
            lists.append(dropped["items"])
        lists.extend(event["items"] for event in feed.get("events") or []
                     if isinstance(event, dict) and isinstance(event.get("items"), list))
        ids = sorted({item["id"] for items in lists for item in items
                      if isinstance(item, dict) and isinstance(item.get("id"), str)})
        # The rows and the cursor must be one moment: read the shared copy, then the counter, and read again if
        # the table changed in between (the copy is read outside the store lock).
        for _ in range(5):
            _, parsed, version = self._parsed_rows_versioned()
            with self.lock:
                epoch, counter = self.connection.execute("SELECT epoch, n FROM news_flash_seq WHERE one = 1").fetchone()
            if counter == version[0]:
                break
        rows = {item_id: parsed[item_id] for item_id in ids if item_id in parsed}
        known = -1
        if since:
            since_epoch, _, number = since.partition(":")
            if since_epoch == epoch and number.isdigit():
                known = int(number)
        sent: dict[str, dict[str, Any]] = {}

        def reduce(item: Any) -> Any:
            found = rows.get(item["id"]) if isinstance(item, dict) and isinstance(item.get("id"), str) else None
            if found is None:
                return item
            rev, row = found
            if any(item[key] != row[key] for key in FLASH_ROW_FIELDS if key in item):
                return item
            if (rev > known or item["id"] in include) and item["id"] not in sent:
                sent[item["id"]] = {"id": item["id"], "rev": rev, **{key: row[key] for key in FLASH_ROW_FIELDS}}
            return {**{key: value for key, value in item.items() if key not in FLASH_ROW_FIELDS}, "rev": rev}

        def reduce_event(event: Any) -> Any:
            if not isinstance(event, dict) or not isinstance(event.get("items"), list):
                return event
            reduced = [reduce(item) for item in event["items"]]
            # An event's title, text and origTitle are its headline report's: name that report instead.
            for original, small in zip(event["items"], reduced, strict=True):
                if small is not original and "rev" in small and all(
                        event.get(key) == rows[original["id"]][1][key] for key in EVENT_HEADLINE_FIELDS):
                    own = {key: value for key, value in event.items() if key not in EVENT_HEADLINE_FIELDS}
                    return {**own, "items": reduced, "headline": original["id"]}
            return {**event, "items": reduced}

        compact = {**feed}
        if isinstance(feed.get("items"), list):
            compact["items"] = [reduce(item) for item in feed["items"]]
        if isinstance(dropped, dict) and isinstance(dropped.get("items"), list):
            compact["screen"] = {**feed["screen"], "dropped": {**dropped, "items": [reduce(item) for item in dropped["items"]]}}
        if isinstance(feed.get("events"), list):
            compact["events"] = [reduce_event(event) for event in feed["events"]]
        return {**compact, "compact": True, "cursor": f"{epoch}:{counter}", "rows": list(sent.values())}

    def _last_poll(self) -> tuple[str, list[str], int] | None:
        """The last flash poll, whichever process ran it (the lease holder polls; a standby reads its record)."""
        stored = self._cache_get("flash_last_poll")
        if stored is None:
            return self._flash_last_poll
        return str(stored.get("at") or ""), [str(item) for item in stored.get("failed") or []], int(stored.get("attempted") or 0)

    def product_events(self, product: str) -> tuple[str, list[dict[str, Any]]]:
        """The product's screened events of the last 24 hours, the window the 快讯 page shows (the news
        analyst's input). 10-08: the research day starts at 08:30, so a day-only filter dropped the
        overnight and pre-open news (Fed minutes, mills' price rises) and rb had no brief all morning."""
        key = product_key(product) or product
        view = self._product_view(key, 24, urgent=False)
        events = [event for event in build_events(view["kept"], hide_pending=True) if event["ai"] is not None]
        return self.product_name(key), events

    def event_products(self, event_id: str) -> list[str]:
        """多 AI 研讨: the products (lower-case ids) whose 24-hour screened events include this event — the one
        place an event id becomes a product, for every caller (a row's click, a UI command, voice, an agent).
        Normally one: a named event's id is made from its product."""
        if self.screener is None:
            return []
        since = (self.now().astimezone(CHINA) - timedelta(hours=EVENT_LOOKUP_HOURS)).isoformat(timespec="seconds")
        return [product for product in self.screener.event_products(event_id, since)
                if any(event["id"] == event_id for event in self.product_events(product)[1])]

    def flash_pick(self, product: str, product_limit: int = 5, macro_limit: int = 3) -> list[dict[str, Any]]:
        """The desk's 快讯 picks from the product's screened view: its own items of the last 24 h,
        then the macro items the model judged relevant to it. Dropped items never become facts."""
        key = product_key(product) or product
        kept = self._product_view(key, 24)["kept"]
        own = [item for item in kept if item.get("direct")][:product_limit]
        macro = [item for item in kept if not item.get("direct")][:macro_limit]
        return own + macro

    async def settle_screen(self, product: str, timeout: float) -> None:
        """Wait (bounded) for the model to judge the product's queued items (the desk's facts)."""
        if self.screener is not None:
            await self.screener.settle(product_key(product) or product, timeout)

    # -- foreign markets

    def foreign_markets(self, product: str | None = None) -> dict[str, Any]:
        """The foreign quotes as research and the AI read them: every product, or a domestic product's related
        ones (none for most). Read from the one foreign entry (foreign_market), never fetched here."""
        if self.foreign is None:
            return {"status": "unavailable", "error": "foreign_market_unavailable", "items": []}
        snapshot = self.foreign.for_product(product_key(product))
        # The related foreign products without a row (a cold start with their source down): named, never a
        # silent empty answer (#932).
        # Filtered by product: the product's own ``missing``; every product: the board's ``partial.missing``.
        names = {item.key: item.name for item in FOREIGN_PRODUCTS}
        absent = snapshot.get("missing") or ([] if product else (snapshot.get("partial") or {}).get("missing"))
        missing = [{"code": key, "name": names.get(key, key)} for key in absent or []]
        if snapshot.get("status") not in {"ok", "stale"}:
            return {"status": "unavailable", "error": snapshot.get("error") or "foreign_market_unavailable", "items": [],
                    **({"missing": missing} if missing else {})}
        labels = snapshot.get("sourceLabels") or {}
        items = [{"code": row["key"], "name": row["name"], "price": row["last"], "changePercent": row["changePercent"],
                  "prevSettle": row["preSettlement"], "unit": row["unit"], "source": labels.get(row["source"], row["source"]),
                  "quotedAt": str(row.get("quoteTime") or "")[:16].replace("T", " "),
                  **({"stale": True} if row.get("stale") else {})}
                 for row in snapshot["rows"]]
        # stale: the last good quotes, kept while the sources fail; fetchedAt is when they were read. A row
        # carried over from a failed source makes the answer stale too, so the desk fact says so.
        status = "stale" if snapshot["status"] == "stale" or any(item.get("stale") for item in items) else snapshot["status"]
        payload = {"status": status, "product": product, "items": items, "fetchedAt": snapshot.get("fetchedAt"),
                   "source": "、".join(dict.fromkeys(item["source"] for item in items)) or "外盘行情",
                   "note": snapshot.get("note")}
        if missing:
            payload["missing"] = missing
        if snapshot.get("error"):
            payload["error"] = snapshot["error"]
        elif status == "stale":
            # A source that raised is named; one that answered without the row (HTTP 200) is not a failed source.
            failed = (snapshot.get("partial") or {}).get("failed") or []
            payload["error"] = "foreign_sources_failed:" + ",".join(failed) if failed else "foreign_rows_missing"
        return payload

    # -- economic calendar

    def refresh_calendar(self) -> bool:
        """The next 7 days, one request per day: China / US / Eurozone at mid importance or above.
        Each day is its own request with its own timeout. A day that answers replaces that day; a
        day that fails keeps the events an earlier round got for it (served as stale) and the round
        returns False so the loop retries sooner. No day answering raises."""
        start = self.now().astimezone(CHINA).date()
        fetched_at = self.now().isoformat(timespec="seconds")
        answered: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        failed: list[str] = []
        for offset in range(CALENDAR_DAYS):
            day = start + timedelta(days=offset)
            code = day.strftime("%Y%m%d")

            def fetch_day(code: str = code) -> Any:
                return self.sources.economic_calendar(code)

            try:
                rows = _records(_with_timeout(CALENDAR_DAY_TIMEOUT_SECONDS, f"calendar-{code}", fetch_day))
            except Exception as error:  # noqa: BLE001 — a failed day skips only that day
                log.warning("research_calendar_day_failed day=%s error=%s", day.isoformat(), type(error).__name__)
                failed.append(day.isoformat())
                continue
            events: list[dict[str, Any]] = []
            for row in rows:
                if _text(row.get("地区")) not in CALENDAR_REGIONS:
                    continue
                try:
                    importance = int(float(_text(row.get("重要性")) or 0))
                except ValueError:
                    importance = 0
                # 每日更新的 ETF 持仓、交易所库存是例行流水，不是要等的“数据公布”。
                if importance < CALENDAR_MIN_IMPORTANCE or "每日更新" in _text(row.get("事件")):
                    continue
                moment = _moment(row.get("日期"), _text(row.get("时间")) or None)
                if moment is None:
                    continue
                events.append({
                    "date": moment.strftime("%Y-%m-%d"), "time": moment.strftime("%H:%M"),
                    "datetime": moment.isoformat(timespec="minutes"), "region": _text(row.get("地区")),
                    "event": _text(row.get("事件"))[:120], "importance": importance,
                    "actual": _number(row.get("公布")), "forecast": _number(row.get("预期")),
                    "previous": _number(row.get("前值")),
                })
            answered[day.isoformat()] = (fetched_at, events)
        # Past days drop out; a failed day keeps what an earlier round got for it (whichever process got it).
        earlier, _, _ = self._calendar_state()
        kept = {day: entry for day, entry in earlier.items() if day >= start.isoformat()}
        self._calendar_days = {**kept, **answered}
        self._calendar_failed = frozenset(failed)
        self._calendar_round = start.isoformat()
        self._cache_put("calendar", {"days": {day: [at, events] for day, (at, events) in self._calendar_days.items()},
                                     "failed": sorted(self._calendar_failed), "round": self._calendar_round})
        if not answered:
            raise LookupError("research_calendar_empty")  # nothing came back: old days stay, retry sooner
        return not failed

    def coming_calendar(self, product: str) -> list[dict[str, Any]]:
        """The product's related events of the next two days still to come (the analyst's watch input)."""
        now = self.now().astimezone(CHINA).isoformat(timespec="minutes")
        feed = self.calendar_feed(2, product=product)
        return [event for event in feed.get("items") or [] if event["datetime"] > now]

    def tonight_calendar(self, product: str) -> list[dict[str, Any]]:
        """Related events from now until the research day turns (08:30 the next morning): the 今晚 line."""
        now = self.now().astimezone(CHINA)
        turn = datetime.combine(now.date() + timedelta(days=1), time_cls(8, 30), tzinfo=CHINA)
        if now.time() < time_cls(8, 30):
            turn -= timedelta(days=1)
        return [{key: event[key] for key in ("date", "time", "datetime", "region", "event", "importance")}
                for event in self.coming_calendar(product) if event["datetime"] < turn.isoformat(timespec="minutes")]

    def calendar_feed(self, days: int = CALENDAR_DAYS, region: str | None = None, product: str | None = None) -> dict[str, Any]:
        """Events in [today, today + days) Beijing time. ok: every day of that window answered on the
        last round (an empty list is then a confirmed quiet week); stale: some day failed — staleDays
        still show the events an earlier round got, missingDays have none; unavailable: no day of
        the window was ever fetched. loading only until the first round has run. With `product` only
        the events related to that product's category are listed (`otherCount` the rest, which the
        宏观 and 全部 views keep)."""
        try:
            self.sources.module()
        except RuntimeError as error:
            return {"status": "unavailable", "error": str(error), "items": []}
        stored, failed, calendar_round = self._calendar_state()
        if calendar_round is None:
            return {"status": "loading", "items": [], "asOf": None,
                    "notice": "经济日历还在后台拉取，稍后再读。"}
        today = self.now().astimezone(CHINA).date()
        window = [(today + timedelta(days=offset)).isoformat() for offset in range(days)]
        covered = [day for day in window if day in stored]
        missing = [day for day in window if day not in stored]
        stale = [day for day in covered if day in failed]
        items = sorted((event for day in covered for event in stored[day][1] if window[0] <= event["date"] <= window[-1]),
                       key=lambda item: (item["datetime"], item["region"], item["event"]))
        if region:
            items = [event for event in items if event["region"] == region]
        key = product_key(product)
        other = 0
        if key:
            related = [event for event in items if calendar_related(event, key)]
            other, items = len(items) - len(related), related
        status = "ok" if not missing and not stale else "stale" if covered else "unavailable"
        payload: dict[str, Any] = {
            "status": status, "days": days, "region": region, **({"product": key, "otherCount": other} if key else {}),
            "asOf": min((stored[day][0] for day in covered), default=None),
            "missingDays": missing, "staleDays": stale, "items": items,
            "source": "百度财经经济日历（AKShare news_economic_baidu）"}
        if status != "ok":
            payload["error"] = "research_calendar_empty" if len(failed) >= CALENDAR_DAYS else "research_calendar_partial"
        return payload

    # -- rates (bonds, SHIBOR, LPR) and the hog spot price

    def _series(self, rows: list[dict[str, Any]], column: str) -> dict[str, Any] | None:
        for row in rows:  # newest first: the latest date that actually has a value
            value = _number(row.get(column))
            if value is not None:
                return {"value": value, "date": _text(row.get("日期") or row.get("TRADE_DATE"))[:10]}
        return None

    def refresh_lpr(self) -> None:
        """LPR is monthly and the source answers in ~47 s: fetched only here, failures keep the cache."""
        rows = _records(_with_timeout(LPR_TIMEOUT_SECONDS, "lpr", self.sources.lpr))
        rows.reverse()  # the source lists 1991→today; _series reads the newest first or we'd quote 1991
        one_year = self._series(rows, "LPR1Y") or self._series(rows, "1年")
        five_year = self._series(rows, "LPR5Y") or self._series(rows, "5年")
        if one_year is None and five_year is None:
            raise LookupError("research_lpr_empty")
        payload = {"lpr1Y": one_year, "lpr5Y": five_year}
        self._cache_put("lpr", payload)

    def rates(self) -> dict[str, Any]:
        # A partial answer (some sources failed, their series carried over from the last good value) is kept
        # but retried as soon as a failure would be (collection.Collector).
        return self._collect.read("rates", self._rates_fetch, ttl=RATES_TTL_SECONDS, retry=LIVE_RETRY_SECONDS,
                                  wait=RATES_TIMEOUT_SECONDS + 10, unavailable="research_rates_unavailable")

    def _rates_fetch(self) -> dict[str, Any]:
        self.sources.module()  # RuntimeError("research_dependency_missing") goes straight back to rates()
        start = (self.now() - timedelta(days=30)).strftime("%Y%m%d")
        rows: dict[str, list[dict[str, Any]]] = {}

        def fetch(kind: str, call: Callable[[], Any]) -> None:
            try:  # bonds and SHIBOR are independent: one failing keeps the other in the payload
                rows[kind] = _records(self._source_calls.call(RATES_TIMEOUT_SECONDS, f"rates-{kind}", call))
            except Exception as error:  # noqa: BLE001
                log.warning("research_rates_source_failed kind=%s error=%s", kind, type(error).__name__)

        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research-live-rates")
        try:
            futures = [pool.submit(fetch, "bond", lambda: self.sources.bond_rates(start)),
                       pool.submit(fetch, "shibor", self.sources.shibor)]
            for future in futures:
                future.result(timeout=RATES_TIMEOUT_SECONDS + 5)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        bond, shibor = rows.get("bond", []), rows.get("shibor", [])
        bond.reverse()  # newest first
        shibor.reverse()
        lpr = self._cache_get("lpr") or {}
        payload: dict[str, Any] = {
            "status": "ok",
            "china10Y": self._series(bond, "中国国债收益率10年"),
            "us10Y": self._series(bond, "美国国债收益率10年"),
            "us10Y2Y": self._series(bond, "美国国债收益率10年-2年"),
            "shiborOvernight": self._series(shibor, "O/N-定价"),
            "shibor1Week": self._series(shibor, "1W-定价"),
            "lpr1Y": lpr.get("lpr1Y"), "lpr5Y": lpr.get("lpr5Y"),
            "source": "中美国债收益率（AKShare bond_zh_us_rate）、SHIBOR（macro_china_shibor_all）、LPR（macro_china_lpr）",
            "fetchedAt": self.now().isoformat(timespec="seconds"),
        }
        if not any(payload[name] for name in ("china10Y", "us10Y", "us10Y2Y", "shiborOvernight", "shibor1Week")):
            return {"status": "unavailable"}  # both sources down or empty: the collector keeps the last good rates
        # One source failed or answered empty (#516): its series keep the last good values (each with its own
        # date) and the payload says which part is old — never a silent "ok" over a missing half.
        previous = self._kept.value("rates") or {}
        failed: list[str] = []
        carried: list[str] = []
        for kind, names in RATE_SERIES.items():
            if all(payload[name] for name in names):
                continue
            failed.append(kind)
            for name in names:
                if not payload[name] and isinstance(previous.get(name), dict):
                    payload[name] = previous[name]
                    carried.append(name)
        if self._lpr_failed:
            failed.append("lpr")
        if failed:
            payload["partial"] = {"failed": failed, "carried": carried}
            payload["error"] = "research_rates_partial"
        return payload

    def hog_price(self) -> dict[str, Any]:
        return self._collect.read("hog", self._hog_fetch, ttl=HOG_TTL_SECONDS, retry=LIVE_RETRY_SECONDS,
                                  wait=HOG_TIMEOUT_SECONDS + 5, unavailable="research_hog_unavailable")

    def _hog_fetch(self) -> dict[str, Any]:
        rows = _records(self._source_calls.call(HOG_TIMEOUT_SECONDS, "hog", self.sources.hog))
        points: list[tuple[str, float]] = []
        for row in rows:
            day, value = _text(row.get("date"))[:10], _number(row.get("value"))
            if day and value is not None:
                points.append((day, value))
        if not points:
            return {"status": "unavailable"}  # an empty answer: the collector keeps the last good price
        day, value = points[-1]
        week_before = points[-8] if len(points) >= 8 else None
        change = round(value - week_before[1], 2) if week_before else None
        return {"status": "ok", "date": day, "price": value, "change7d": change,
                "change7dPercent": round(change / week_before[1] * 100, 1)
                if week_before and change is not None else None,
                "source": "涌益咨询外三元生猪价（AKShare futures_hog_core）", "unit": "元/公斤"}

    def _calendar_state(self) -> tuple[dict[str, tuple[str, list[dict[str, Any]]]], frozenset[str], str | None]:
        """The calendar's days, failed days and last round as stored by whichever process refreshed it
        (the lease holder); re-parsed only when the stored copy changed."""
        saved_at = self._kept.saved_at("calendar")
        if saved_at is not None and saved_at != self._calendar_loaded:
            stored = self._cache_get("calendar") or {}
            try:
                days = {str(day): (str(entry[0]), list(entry[1])) for day, entry in (stored.get("days") or {}).items()}
            except (TypeError, IndexError, ValueError):
                days = None
            if days is not None:
                self._calendar_days = days
                self._calendar_failed = frozenset(str(day) for day in stored.get("failed") or [])
                self._calendar_round = stored.get("round") or self._calendar_round
            self._calendar_loaded = saved_at
        return self._calendar_days, self._calendar_failed, self._calendar_round

    # -- small sqlite cache (LPR today, the calendar, the last flash poll)

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        return self._kept.value(key)

    def _cache_put(self, key: str, payload: dict[str, Any]) -> None:
        self._kept.put(key, payload)
