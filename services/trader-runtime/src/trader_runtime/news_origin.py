"""原始来源识别（2026-10-13）：快讯标题/正文里“据新华社”“海关总署公布”“国家统计局数据显示”一类说法，
认出这条消息最初出自谁。

规则只在本文件 `ORIGIN_RULES` 一处维护：(原始来源, 正则)。只认机构全称或通行简称加引语（据、公布、
发布、数据显示、消息、表示、宣布、冒号…），不认裸的“统计局”“海关”“央行”——美国劳工统计局、
韩国海关、欧洲央行都不是国内这些机构；外国同名部委（美国商务部、美国财政部…）用前缀排除。

识别结果是快讯条目上的 `据:<原始来源>` 标签（research_live.tag_flash_item 加）。原始来源正好是已接入
的官方源（`ORIGIN_SOURCE`）时，合并去重会拿它去找同一机构的官方条目（`retells` 判断转载是不是照着这条
官方标题说的；品种一致由 research_live._cited_match 另查），找到且只找到一条就并成一行，转载归官方。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

ORIGIN_TAG = "据:"
# 引语：机构名后跟这些才算“出自它”（日期、发言人可插在中间：“海关总署10月13日公布”“商务部新闻发言人表示”）。
_SAYS = (r"(?:\d{1,2}月\d{1,2}日|同日|当日|今日|今天|日前|近日)?(?:新闻发言人|有关负责人|发言人|负责人)?(?:最新)?"
         r"(?:数据显示|数据表明|公布|发布|公告|通报|消息|表示|宣布|称|指出|：|:)")
# 外国同名机构：这些前缀之后的“商务部 / 财政部 / 证监会…”不是国内的。
_FOREIGN = "".join(f"(?<!{country})" for country in ("美国", "英国", "日本", "韩国", "德国", "法国", "印度", "俄罗斯",
                                                      "澳大利亚", "加拿大", "巴西", "印尼", "越南", "泰国", "马来西亚",
                                                      "香港", "台湾"))


def _org(name: str, *, foreign_guard: bool = False) -> str:
    """“据<机构>” or “<机构><引语>”."""
    guard = _FOREIGN if foreign_guard else ""
    return rf"据{name}|{guard}{name}{_SAYS}"


ORIGIN_RULES: tuple[tuple[str, str], ...] = (
    # 通讯社与央媒。“新华社北京10月13日电”“（新华社）”也算；“新华社区”不算。
    ("新华社", r"据新华社|新华社(?:报道|消息|记者|快讯|电)|新华社[^\s，。、；]{0,8}?\d{1,2}月\d{1,2}日电|[（(]新华社[）)]"),
    ("央视新闻", r"据央视(?:新闻)?|央视新闻(?:报道|消息|客户端|：|:)|[（(]央视新闻[）)]|据(?:中央广播电视)?总台"),
    ("人民日报", r"据人民日报|人民日报(?:报道|消息|：|:)"),
    # 部委与数据发布。
    ("国家统计局", _org("国家统计局")),
    ("海关总署", _org("海关总署") + r"|中国海关(?:\d{1,2}月\d{1,2}日)?(?:数据显示|公布|发布)"),
    ("中国人民银行", _org("(?:中国)?人民银行") + r"|中国央行" + _SAYS),
    ("国家发改委", _org("国家发(?:改委|展改革委)") + r"|^发改委[：:]"),
    ("商务部", _org("商务部", foreign_guard=True)),
    ("财政部", _org("财政部", foreign_guard=True)),
    ("农业农村部", _org("农业农村部")),
    ("国家能源局", _org("国家能源局")),
    ("证监会", _org("(?:中国)?证监会", foreign_guard=True)),
    # 交易所：媒体常写“上期所：…”“郑商所发布通知…”。
    ("上期所", _org("(?:上期所|上海期货交易所|上期能源|上海国际能源交易中心)")),
    ("郑商所", _org("(?:郑商所|郑州商品交易所)")),
    ("大商所", _org("(?:大商所|大连商品交易所)")),
    ("广期所", _org("(?:广期所|广州期货交易所)")),
    ("中金所", _org("(?:中金所|中国金融期货交易所)")),
    # 外电。
    ("路透", r"据路透(?:社)?|路透(?:社)?(?:报道|援引|消息|：|:)"),
    ("彭博", r"据彭博(?:社)?|彭博(?:社)?(?:报道|援引|消息|：|:)"),
)
_COMPILED = tuple((origin, re.compile(pattern)) for origin, pattern in ORIGIN_RULES)

# 原始来源 → 已接入的官方快讯源名（research_live.OFFICIAL_SOURCES / EXCHANGE_SOURCES）：合并去重时去找它的条目。
ORIGIN_SOURCE: dict[str, str] = {
    "国家统计局": "国家统计局", "国家发改委": "发改委", "国家能源局": "能源局",
    "上期所": "上期所", "郑商所": "郑商所", "大商所": "大商所", "广期所": "广期所", "中金所": "中金所",
}


def origins_of(text: str) -> list[str]:
    """The original sources a flash names, in rule order (several may be named: 据新华社…海关总署公布)."""
    return [origin for origin, pattern in _COMPILED if pattern.search(text)]


_NOISE = re.compile(r"[\s，。、；：:,.!！?？“”\"'‘’（）()【】\[\]《》<>—\-_/|·]+")
# 数字、合约代码、英文：官方标题里的这些一个都不能在转载里对不上（手续费数值、合约月份、生效日期）。
_KEY = re.compile(r"[0-9a-z]")


def _plain(text: str) -> str:
    return _NOISE.sub("", text).lower()


def _bigrams(plain: str) -> set[str]:
    return {plain[index:index + 2] for index in range(len(plain) - 1)}


CITE_SHARE = 0.6
CITE_MIN_BIGRAMS = 8


def _retold_in(official: str, wanted: set[str], report: str) -> bool:
    """Shingle containment (Broder: the share of the official title's character pairs found in the report,
    what MinHash estimates at scale) plus the template check near-duplicate detection needs on top of it:
    aligned character by character, the report may add words and drop the notice's wording at either end
    (“关于”“的通知”), but may not put something else in place of a word inside the title — “关于调整铜期货…”
    and “关于调整铝期货…” share 12 of 14 pairs and are still two notices — nor miss any digit or letter
    (合约代码、手续费数值、日期)."""
    if len(wanted & _bigrams(report)) / len(wanted) < CITE_SHARE:
        return False
    at_official = at_report = 0
    for block in SequenceMatcher(None, official, report, autojunk=False).get_matching_blocks():
        missing = official[at_official:block.a]
        if missing and (_KEY.search(missing)
                        or (0 < at_official and block.a < len(official) and at_report < block.b)):
            return False
        at_official, at_report = block.a + block.size, block.b + block.size
    return True


def retells(official_title: str, report_title: str, report_opening: str) -> bool:
    """Whether a media report retells this official item word for word: its title, its opening or the two
    together carry the official title (≥ 60 % of its character pairs, nothing swapped inside, every
    number and code). Conservative on purpose — a paraphrase (“9月CPI同比上涨0.4%” for “2026年9月份居民
    消费价格同比上涨0.4%”) or a same-template notice about another product stays its own row, only tagged
    with its origin: two rows wrongly merged lose one story's body and link for good, two left apart lose
    nothing."""
    official = _plain(official_title)
    wanted = _bigrams(official)
    if len(wanted) < CITE_MIN_BIGRAMS:
        return False
    parts = [_plain(report_title), _plain(report_opening)]
    return any(_retold_in(official, wanted, part) for part in (*parts, "".join(parts)) if part)
