"""The research desk: one product's facts from every source, then one anchored model judgment.

Facts come from the market provider (the product's main contract and up to three
related products), the six AKShare datasets, official releases (NBS, CFTC, and
FRED/EIA when keyed) and the product's news. Every fact carries a stable id,
its source and its date. The judgment is a single structured model call without
tools that may only cite those ids; `validate_judgment` drops any point that
cites nothing, cites an unknown id, carries a number its cited facts do not
contain, or gives order advice, and computes the shown confidence from what is
left (`rated_confidence`; the model's own level stays as modelConfidence). A judgment keeps a copy of the facts it cited,
so it stays traceable after the facts refresh; a cited id whose current fact reads
differently is listed in the shown judgment's changedBasis.

A fact keeps its source's quality: `fetchedAt` when the source says when it was
read, and `status: stale` (with `error`) when it is an old value kept because the
source failed to refresh or the quote stopped updating. A section holding such a
fact is stale too.

The report is per exchange+product and shared by every reader: facts refresh at
most every five minutes, a judgment is regenerated only when the material facts
changed (and it is 30 minutes old or the price moved a point), generation is
capped per day, and nothing about any account ever enters the model input.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from . import linkage
from .collection import AsyncCollector, SourceFailed
from .daily_budget import DailyCallBudget
from .instruments import FOREIGN_PRODUCTS, ForeignProduct, foreign_for_domestic
from .last_good import Kept, LastGood
from .order_boundaries import MAX_FUTURE_QUOTE_SKEW_SECONDS, SimulationOrderError, quote_is_fresh
from .research_data import FOREIGN_RESEARCH, ResearchDataService, research_target
from .research_live import CALENDAR_CATEGORY, CALENDAR_DAYS, ResearchLiveService, calendar_related
from .research_sources import (
    CFTC_MARKETS,
    CHINA,
    EIA_PRODUCTS,
    FRED_NOTICE,
    FRED_SERIES,
    NBS_PRICE_ROWS,
    NBS_PRICE_TITLE,
    SHFE_WEEKLY_NAMES,
    PublicSources,
    SourceError,
    feed_notes,
    inventory_mentions,
    merge_notes,
    parse_nbs_price_row,
    product_aliases,
    shfe_weekly_row,
)
from .trading_calendar import Session, daily_bar_day, previous_trading_day, sessions_of_day, trading_day_of

SHFE_WEEKLY_PRODUCTS = frozenset(SHFE_WEEKLY_NAMES)
# Products whose demand leans on construction and investment: they also get fixed-asset investment.
INVESTMENT_PRODUCTS = frozenset({"rb", "hc", "wr", "i", "j", "jm", "fg", "sa", "v", "sf", "sm"})

log = logging.getLogger("uvicorn.error.research")

FACTS_TTL_SECONDS = 5 * 60
FACTS_LOADING_TTL_SECONDS = 30
FORCE_MIN_SECONDS = 60
HISTORY_BARS = 60
CATALOG_TTL_SECONDS = 10 * 60
SOURCE_DEADLINE_SECONDS = 8.0
JUDGMENT_FAILURE_BACKOFF_SECONDS = 5 * 60
JUDGMENT_TIMEOUT_SECONDS = 60
# 529-B2 (owner decision, personal-use phase): a research-desk judgment is
# shared per contract, so its model tokens book against the platform marker —
# never the quota of whoever happened to trigger the generation. A reader whose
# daily cap is 0 still sees the generated judgment.
PLATFORM_USAGE_USER = "platform:research-desk"
# One judgment per product per research day (owner decision 2026-09-29). The day turns at 08:30 Beijing,
# when the futures companies' morning notes are out; 08:45 pre-generates the products read in the last week.
RESEARCH_DAY_START = clock_time(8, 30)
PREGENERATE_AT = clock_time(8, 45)
# A lease holder that starts between 08:45 and the day session's close catches up the day's pre-generation.
PREGENERATE_CATCHUP_UNTIL = clock_time(15, 0)
PREGENERATE_LOOKBACK_DAYS = 7
PREGENERATE_LIMIT = 12
# A black swan re-runs it once that day: the main contract is ±3% on the day and moved 1.5 points since.
SWAN_DAY_CHANGE_POINTS = 3.0
SWAN_MOVE_POINTS = 1.5
# …or the price comes within 1% of the day's limit (涨跌停) when it was not at the last judgment.
NEAR_LIMIT_PERCENT = 1.0
# 今日判断自动更新 (owner 2026-10-24): a product with a night session is judged again once after this time
# (shortcut: weekdays stand for night sessions; a holiday eve has none, upgrade with the trading calendar if it
# matters), and the 判断可能过时 news hint re-runs it by itself — at most this many times a research day, never
# sooner than this after the last judgment.
NIGHT_REJUDGE_AT = clock_time(20, 45)
NEWS_REJUDGES_PER_DAY = 2
NEWS_REJUDGE_MIN_SECONDS = 30 * 60
# Facts older than their five minutes but younger than this are served at once while a re-read runs behind.
STALE_SERVE_SECONDS = 30 * 60
# 调研台一秒内打开 (2026-10-22): a reader of the panel never waits for a collection. Facts of any age go out at
# once (re-read behind, `factsRefreshing`); a product with no facts at all waits this long, then gets its
# sections as 加载中 and reads again.
FIRST_READ_WAIT_SECONDS = 0.3
MANUAL_MIN_SECONDS = 60 * 60
# 多 AI 研讨: how long a debate waits for a loading section before it works from what is there.
DEBATE_SETTLE_SECONDS = 12.0
# 判断可能过时 (第七批): this many 重要 news events after the judgment make it a candidate for a re-run.
STALE_MAJOR_EVENTS = 3
EXPECTED_FACTS_SECONDS = 8
EXPECTED_MODEL_SECONDS = 12
REPORT_FACTS = 6
MIN_SECTIONS_FOR_JUDGMENT = 3
SLOW_TTL = {"shfe_weekly": 12 * 3600, "nbs": 3 * 3600, "nbs_page": 12 * 3600, "cftc": 12 * 3600, "fred": 6 * 3600, "eia": 6 * 3600,
            "macro": 6 * 3600, "notes": 30 * 60, "orgfeed": 30 * 60}
# A failed, timed-out or empty slow read is retried after this; the last good value (kept in
# research_live_cache, read back after a restart) is served stale meanwhile, never replaced by the failure.
SLOW_FAILURE_TTL_SECONDS = 10 * 60
# Eastmoney requests queue ≥ 1 s apart per host (host_throttle): the five macro series and a
# note search plus its article pages need longer than the shared deadline on a cold read.
# Their deadline is this multiple of the shared one (8 s → 12 s).
SLOW_DEADLINE_FACTOR = {"macro": 1.5, "notes": 1.5, "orgfeed": 1.5}
SECTIONS = (("market", "行情", "data"), ("related", "关联品种", "data"), ("fundamental", "基本面", "data"),
            ("official", "产业数据", "data"), ("macro", "宏观数据", "data"), ("research", "机构观点", "report"),
            ("report", "相关报道", "report"))
REPORT_SECTIONS = frozenset({"research", "report", "flash"})
# 外盘品种的研究台（2026-10-21 所有者定三块）：行情和走势（MT5 等外盘报价 + 快期外盘主连日线）、对应的国内品种、
# 按外盘自己的名字搜的报道；快讯按它的快讯键筛（同国内，作判断的输入）。国内的基本面、产业、宏观、机构观点不适用。
FOREIGN_SECTIONS = (("market", "行情", "data"), ("related", "对应国内品种", "data"), ("report", "相关报道", "report"))
# 快讯 (when the runtime wires the live service) are the judgment's input and listed under 资料来源, but the
# report has no 快讯 section any more (第七批 2026-10-07): the page shows one 新闻面 line and links the 快讯 tab.
FLASH_SOURCE_LABEL = "跨源快讯（财联社、东方财富、新浪、同花顺、富途、上海金属网等，按本品种筛过）"
NOTICE = ("数据类事实带来源与日期；机构观点与报道是第三方的说法，本软件未核实；AI 判断只能引用这些事实，"
          "是推断不是事实，也不是交易建议。")

# Product → up to three related products and how they relate. Keys are lower-case product ids.
RELATED: dict[str, tuple[tuple[str, str], ...]] = {
    "rb": (("i", "上游原料"), ("j", "上游燃料"), ("hc", "同链成材")),
    "hc": (("i", "上游原料"), ("j", "上游燃料"), ("rb", "同链成材")),
    "wr": (("rb", "同链成材"), ("i", "上游原料")),
    "i": (("rb", "下游成材"), ("hc", "下游成材"), ("j", "同链炉料")),
    "j": (("jm", "上游原料"), ("i", "同链炉料"), ("rb", "下游成材")),
    "jm": (("j", "下游产品"), ("rb", "终端成材")),
    "sf": (("rb", "下游成材"), ("sm", "同类合金")),
    "sm": (("rb", "下游成材"), ("sf", "同类合金")),
    "ss": (("ni", "上游原料"),),
    "cu": (("al", "同类有色"), ("zn", "同类有色"), ("ni", "同类有色")),
    "al": (("ao", "上游原料"), ("cu", "同类有色"), ("zn", "同类有色")),
    "ao": (("al", "下游产品"),),
    "zn": (("cu", "同类有色"), ("al", "同类有色"), ("pb", "同类有色")),
    "pb": (("zn", "同类有色"),),
    "ni": (("ss", "下游产品"), ("cu", "同类有色")),
    "sn": (("cu", "同类有色"),),
    "au": (("ag", "同类贵金属"),),
    "ag": (("au", "同类贵金属"),),
    "sc": (("fu", "下游产品"), ("bu", "下游产品"), ("lu", "下游产品")),
    "fu": (("sc", "上游原料"), ("lu", "同类燃料油")),
    "lu": (("sc", "上游原料"), ("fu", "同类燃料油")),
    "bu": (("sc", "上游原料"),),
    "pg": (("sc", "上游原料"),),
    "px": (("ta", "下游产品"), ("sc", "上游原料")),
    "ta": (("px", "上游原料"), ("eg", "同链聚酯原料"), ("pf", "下游产品")),
    "eg": (("ta", "同链聚酯原料"),),
    "pf": (("ta", "上游原料"),),
    "ma": (("pp", "下游产品"), ("ur", "同为煤化工")),
    "pp": (("l", "同类塑料"), ("ma", "上游原料"), ("sc", "上游原料")),
    "l": (("pp", "同类塑料"), ("v", "同类塑料")),
    "v": (("l", "同类塑料"), ("sh", "同链氯碱")),
    "sh": (("v", "同链氯碱"),),
    "eb": (("bz", "上游原料"),),
    "bz": (("eb", "下游产品"),),
    "sa": (("fg", "下游产品"),),
    "fg": (("sa", "上游原料"),),
    "ur": (("ma", "同为煤化工"),),
    "ru": (("nr", "同类橡胶"), ("br", "替代品")),
    "nr": (("ru", "同类橡胶"),),
    "br": (("ru", "替代品"),),
    "m": (("a", "上游原料"), ("y", "联产品"), ("rm", "替代品")),
    "y": (("a", "上游原料"), ("m", "联产品"), ("p", "替代品")),
    "a": (("m", "下游产品"), ("y", "下游产品")),
    "b": (("m", "下游产品"), ("y", "下游产品")),
    "p": (("y", "替代品"), ("oi", "替代品")),
    "oi": (("rm", "联产品"), ("y", "替代品")),
    "rm": (("m", "替代品"), ("oi", "联产品")),
    "c": (("cs", "下游产品"), ("lh", "饲料需求")),
    "cs": (("c", "上游原料"),),
    "lh": (("c", "饲料成本"), ("m", "饲料成本")),
    "jd": (("c", "饲料成本"), ("m", "饲料成本")),
    "cf": (("cy", "下游产品"),),
    "cy": (("cf", "上游原料"),),
    "lc": (("si", "同为新能源材料"), ("ps", "同为新能源材料")),
    "si": (("ps", "下游产品"), ("lc", "同为新能源材料")),
    "ps": (("si", "上游原料"), ("lc", "同为新能源材料")),
    "if": (("ih", "同类股指"), ("ic", "同类股指")),
    "ih": (("if", "同类股指"),),
    "ic": (("im", "同类股指"), ("if", "同类股指")),
    "im": (("ic", "同类股指"),),
    "t": (("tf", "同类国债"), ("tl", "同类国债")),
    "tf": (("t", "同类国债"), ("ts", "同类国债")),
    "ts": (("tf", "同类国债"),),
    "tl": (("t", "同类国债"),),
    "sr": (("cs", "淀粉糖替代"), ("c", "淀粉糖原料")),
    "ap": (("cj", "同为果品"), ("pk", "同为郑商所农产品")),
    "cj": (("ap", "同为果品"),),
    "pk": (("y", "同为食用油原料"), ("oi", "同为食用油原料")),
    "sp": (("op", "下游产品"), ("lg", "同为林产品")),
    "op": (("sp", "上游原料"),),
    "lg": (("sp", "同为林产品"), ("fb", "下游板材")),
    "ec": (("sc", "燃油成本"),),
    "pt": (("pd", "同类铂族金属"), ("au", "同类贵金属"), ("ag", "同类贵金属")),
    "pd": (("pt", "同类铂族金属"), ("au", "同类贵金属")),
    "pr": (("ta", "上游原料"), ("eg", "上游原料"), ("pf", "同链聚酯")),
    "pl": (("pp", "下游产品"), ("ma", "MTO 原料"), ("sc", "上游原料")),
    "bc": (("cu", "同为铜（内外盘）"), ("al", "同类有色")),
    "ad": (("al", "上游原料"), ("ao", "上游原料")),
    "zc": (("jm", "同为煤炭"), ("j", "同为煤炭")),
    "rs": (("oi", "下游产品"), ("rm", "下游产品")),
    "wh": (("pm", "同类小麦"), ("c", "饲用替代")),
    "pm": (("wh", "同类小麦"),),
    "ri": (("jr", "同类稻谷"),),
    "jr": (("rr", "下游产品"), ("ri", "同类稻谷")),
    "rr": (("jr", "上游原料"),),
    "fb": (("bb", "同类板材"), ("lg", "上游原料")),
    "bb": (("fb", "同类板材"), ("lg", "上游原料")),
}
# Eastmoney's "inventory" is the exchanges' registered receipts again (same figures, checked 2026-09-29):
# the desk leaves it out; SHFE/INE products get the exchange's weekly warehouse stock instead.
DATASET_FACTS = ("spot_basis", "warehouse_receipt", "position_rank", "term_structure")
SHAPES = {"contango": "远月升水", "backwardation": "远月贴水", "flat": "近远月持平"}
TRENDS = {"up": "上升", "down": "下降", "flat": "持平"}
# Order verbs anywhere, or advice wording around a position change (a fact may say "多头加仓").
ADVICE = re.compile(r"买入|卖出|开仓|平仓|抄底|逢低|逢高|止损|止盈|追多|追空"
                    r"|(建议|可以|可考虑|应当|应该|不妨)[^，。；]{0,6}(做多|做空|加仓|减仓|持有|离场)")
NUMBER = re.compile(r"\d+(?:\.\d+)?")

DESK_INSTRUCTIONS = """你是期货调研员。输入是本软件刚采集的事实清单，每条有 id、类别、来源和日期。
只根据这些事实写判断，不得使用清单以外的数字、事件或记忆；清单里没有的就不要说。
status 为 stale 的事实是上次取到的旧值（来源这次没刷新成功或行情已停更，fetchedAt 是当时的采集时间），引用时要说明是旧值，不能当作最新情况。
kind 为 report 的事实是第三方的说法，不是核实过的数据：section 为 research 的是期货公司观点，引用时写"某某期货认为"；section 为 report 的是媒体报道，引用时写"据报道"；section 为 flash 的是跨源快讯，引用时写"快讯"。
stance 取 偏多、偏空、震荡、不明 之一；confidence 照常给你自己的判断（低、中、高），最终显示的信心由程序按论点的依据计算，所以下面每条论点的 direction、kind、basis 必须写准；headline 是不超过40字的一句话结论，headlineBasis 列出它依据的事实 id。
points 写2到4条论点：basis 列出所依据的事实 id（至少一个，只能用清单里的 id，只列真正支撑这条论点的事实）；kind 为 推断 表示事实直接支持，推测 表示合理但超出事实的猜测。
每条论点里出现的数字都必须原样出现在它引用的事实（label、value 或 detail）里；不要自己计算新的数字。
每条论点、风险和关注都要给 quote：从它引用的某条事实的 label、value、detail 或 title 里原样摘一段 4 到 30 个字的原文，一个字都不能改。
risks 最多3条反向风险，watch 最多3条接下来值得关注的事项，同样写 basis。
每条 points 和 risks 都要给 direction：这条理由本身指向的价格方向，偏多 / 偏空 / 中性（看它对价格的作用，不是照抄 stance；风险通常与 stance 相反；与 stance 相反的理由如实标出，不要改成中性）。
conclusion 是不超过60字的一句结论：多空两边谁占上风、为什么，可以写“若……倾向转……”；数字只能用清单里出现过的。
不给下单建议，不写买入、卖出、开仓、平仓、加仓、减仓。"""


class DeskPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    basis: list[str] = []
    quote: str = ""
    kind: Literal["推断", "推测"] = "推断"
    # 第七批: the price direction this reason points to (偏多 / 偏空 / 中性); anything else is inferred.
    direction: str = ""


class DeskJudgment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    stance: Literal["偏多", "偏空", "震荡", "不明"]
    confidence: Literal["低", "中", "高"]
    headline: str
    headlineBasis: list[str] = []
    points: list[DeskPoint] = []
    risks: list[DeskPoint] = []
    watch: list[DeskPoint] = []
    conclusion: str = ""


def _numbers(text: str) -> set[float]:
    return {float(value) for value in NUMBER.findall(text.replace(",", ""))}


def _anchored(text: str, allowed: set[float]) -> bool:
    return _numbers(text) <= allowed


def _fact_numbers(fact: dict[str, Any]) -> set[float]:
    return _numbers(f"{fact['label']} {fact['value']} {fact.get('detail') or ''}")


QUOTE_NOISE = re.compile(r"[\s“”\"「」『』]")
# The longest cited statement the desk shows (a 今日判断 point, risk or watch line).
STATEMENT_CHARS = 200


def _quoted(quote: str, basis: list[str], by_id: dict[str, dict[str, Any]]) -> bool:
    """The point's quote is copied from one of the facts it cites (spacing and quote marks aside)."""
    wanted = QUOTE_NOISE.sub("", quote)
    if not 4 <= len(wanted) <= 40:
        return False
    return any(wanted in QUOTE_NOISE.sub("", " ".join(str(by_id[item].get(name) or "") for name in ("label", "value", "detail", "title")))
               for item in basis)


def _reject(text: str, basis: list[str], by_id: dict[str, dict[str, Any]]) -> str | None:
    """Why a statement cannot be shown, or None when it is anchored."""
    if not text or len(text) > STATEMENT_CHARS:
        return "length"
    if not basis:
        return "no_basis"
    if any(item not in by_id for item in basis):
        return "unknown_basis"
    if ADVICE.search(text):
        return "advice"
    if not _anchored(text, set().union(*(_fact_numbers(by_id[item]) for item in basis))):
        return "number_mismatch"
    return None


DIRECTIONS = ("偏多", "偏空", "中性")
CONCLUSION_CHARS = 60
OPPOSITE = {"偏多": "偏空", "偏空": "偏多"}


def stance_clash(stance: str | None, direction: str | None, *, holding: bool) -> dict[str, str] | None:
    """策略与今日判断相反 (策略页改版第八批, 2026-10-08): the yellow line a strategy card shows when
    the product's 今日判断 is 偏多 / 偏空 and the strategy holds or signalled the other way today
    (`direction` long / short; `holding` = a position, else only today's open signal). 震荡 / 不明 /
    no judgment / no direction: None — nothing to warn about."""
    side = {"long": "偏多", "short": "偏空"}.get(direction or "")
    if stance not in OPPOSITE or side is None or OPPOSITE[stance] != side:
        return None
    doing = "做多" if direction == "long" else "做空"
    text = (f"今日判断{stance}，这个策略今天在{doing}" if holding
            else f"今日判断{stance}，这个策略今天发出的是{doing}信号，方向相反")
    return {"stance": stance, "direction": str(direction), "text": text}


def implied_direction(group: str, stance: str) -> str:
    """A reason's direction when the model gave none (a judgment stored before 第七批): points go with
    the stance, risks against it; under 震荡 / 不明 both are 中性 and stay in their own column."""
    if stance not in OPPOSITE:
        return "中性"
    return stance if group == "points" else OPPOSITE[stance]


def with_directions(value: dict[str, Any]) -> dict[str, Any]:
    """A stored judgment with every point and risk carrying a direction (old ones are inferred, not re-run)."""
    stance = str(value.get("stance") or "")
    out = dict(value)
    for group in ("points", "risks"):
        out[group] = [point if point.get("direction") in DIRECTIONS else {**point, "direction": implied_direction(group, stance)}
                      for point in value.get(group) or []]
    return out


def facts_numbers(facts: list[dict[str, Any]]) -> set[float]:
    """Every number any of the facts carries (label, value or detail)."""
    return set().union(*(_fact_numbers(fact) for fact in facts)) if facts else set()


def sentence_problem(text: str, facts: list[dict[str, Any]], limit: int, extra: tuple[float, ...] = ()) -> str | None:
    """Why a sentence that cites nothing (the judgment's conclusion, a debate verdict's lines) cannot be shown:
    empty or longer than `limit`, order advice, or a number no fact carries (`extra`: figures the caller
    already checked, such as a 失效位). None when it may be shown."""
    if not text or len(text) > limit:
        return "length"
    if ADVICE.search(text):
        return "advice"
    if not _anchored(text, facts_numbers(facts) | set(extra)):
        return "number_mismatch"
    return None


def statement_problem(text: str, basis: list[str], quote: str, by_id: dict[str, dict[str, Any]]) -> str | None:
    """Why a cited statement cannot be shown — length, no_basis, unknown_basis, advice, number_mismatch
    (a number its cited facts do not contain) or quote_mismatch (the quote is not copied from them) —
    or None when it is anchored. The one check the desk's judgment and 多 AI 研讨's arguments share."""
    return _reject(text, basis, by_id) or (None if _quoted(quote, basis, by_id) else "quote_mismatch")


# 信心按依据计算 (owner 2026-10-12): the model's own 低/中/高 was nearly always 中 (77 of 81 judgments), so the
# shown confidence is decided here from the judgment's cited facts; the model's level is kept as modelConfidence.
# 高 needs two direct inferences for the stance — one fact can be misread, two agreeing ones rarely both are.
CONFIDENCE_MIN_ALIGNED = 2
# ...resting on two independent kinds of data (行情 plus 基差 / 仓单 / 持仓排名 …): two readings of the same
# price series are one piece of evidence, not two.
CONFIDENCE_MIN_KINDS = 2
# One reverse risk is normal hedging; a second means the other side has a real case.
CONFIDENCE_MAX_OPPOSING_RISKS = 1
# Fewer current facts than this leaves too little to cross-check anything: 低 whatever the model wrote.
CONFIDENCE_MIN_FRESH_FACTS = 3
# What kind of evidence a fact is, for "different kinds": market facts are one kind (行情), related products one,
# macro one; the fundamental and official sections bundle independent datasets, so each of those is its own kind
# (two inventory reports are still one kind). Third-party reports are no kind at all — they are not verified data.
EVIDENCE_KINDS = {"market": "行情", "related": "关联品种", "macro": "宏观"}
DATASET_KINDS = {"D.spot_basis": "基差", "D.warehouse_receipt": "仓单", "D.position_rank": "持仓排名",
                 "D.inventory": "库存", "D.term_structure": "期限结构", "D.hog_price": "现货价",
                 "O.shfe_stock": "库存", "O.cftc": "CFTC持仓", "O.eia_crude": "原油库存", "O.nbs_price": "统计局价格"}


def evidence_kind(fact: dict[str, Any]) -> str | None:
    """The kind of evidence a fact is (行情, 基差, 仓单 …), or None for a third-party report."""
    if fact.get("kind") == "report" or fact.get("section") in REPORT_SECTIONS:
        return None
    section = str(fact.get("section") or "")
    if section in EVIDENCE_KINDS:
        return EVIDENCE_KINDS[section]
    return DATASET_KINDS.get(str(fact.get("id"))) or re.split(r"[（( ]", str(fact.get("label") or section))[0][:6] or section


def rated_confidence(stance: str, points: list[dict[str, Any]], risks: list[dict[str, Any]],
                     by_id: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """(信心, 一句依据) from the kept points and risks (each with direction and kind) and the input facts.
    低 when any holds: stance 不明; fewer than 3 current facts; no 推断 point for the stance (only 推测); more than half
    of the cited facts stale; reverse points + risks at least as many as the stance's 推断 points. 高 when the stance
    is 偏多/偏空 with ≥ 2 推断 points its way, their facts spanning ≥ 2 kinds of data, none of those facts stale and
    not mostly reports, no reverse point and at most one reverse risk. Otherwise 中. Under 震荡 every 推断 point
    supports the range view (both sides balancing is what 震荡 says) and there is no reverse side; it is at most 中."""
    if stance == "不明":
        return "低", "倾向不明"
    fresh = sum(1 for fact in by_id.values() if fact.get("status") != "stale")
    if fresh < CONFIDENCE_MIN_FRESH_FACTS:
        return "低", f"可用的新鲜事实只有 {fresh} 条"
    ranged = stance not in OPPOSITE
    aligned = [point for point in points if point.get("kind") == "推断" and (ranged or point.get("direction") == stance)]
    if not aligned:
        return "低", "没有直接依据，只有推测" if ranged else "没有直接依据支持倾向，只有推测"
    cited = list(dict.fromkeys(item for point in (*points, *risks) for item in point["basis"] if item in by_id))
    stale = [item for item in cited if by_id[item].get("status") == "stale"]
    if len(stale) * 2 > len(cited):
        return "低", f"引用的 {len(cited)} 条事实里 {len(stale)} 条是旧值"
    against_points = 0 if ranged else sum(1 for point in points if point.get("direction") == OPPOSITE[stance])
    against_risks = 0 if ranged else sum(1 for point in risks if point.get("direction") == OPPOSITE[stance])
    if against_points + against_risks >= len(aligned):
        return "低", f"反向理由 {against_points + against_risks} 条，不少于同向直接依据 {len(aligned)} 条"
    if ranged:
        return "中", "震荡判断信心最高为中"
    support = list(dict.fromkeys(item for point in aligned for item in point["basis"] if item in by_id))
    kinds = list(dict.fromkeys(kind for kind in (evidence_kind(by_id[item]) for item in support) if kind))
    names = "、".join(kinds[:3])
    if len(aligned) < CONFIDENCE_MIN_ALIGNED:
        return "中", f"同向直接依据只有 {len(aligned)} 条"
    if len(kinds) < CONFIDENCE_MIN_KINDS:
        return "中", f"同向依据只来自一类数据（{names}）" if kinds else "同向依据没有核实过的数据"
    if any(by_id[item].get("status") == "stale" for item in support):
        return "中", "同向依据里有旧值"
    if sum(1 for item in support if evidence_kind(by_id[item]) is None) * 2 > len(support):
        return "中", "同向依据主要是第三方说法"
    if against_points:
        return "中", f"有 {against_points} 条论点方向相反"
    if against_risks > CONFIDENCE_MAX_OPPOSING_RISKS:
        return "中", f"反向风险有 {against_risks} 条"
    return "高", f"{len(aligned)} 条直接依据（{names}）同向，" + (f"反向风险 {against_risks} 条" if against_risks else "无反向论点")


def validate_judgment(value: DeskJudgment, facts: list[dict[str, Any]], dropped: dict[str, int] | None = None) -> dict[str, Any]:
    """Keep only statements that cite known facts and whose numbers those facts contain.
    `dropped` (optional) counts the reasons statements were removed, for the log."""
    by_id = {fact["id"]: fact for fact in facts}
    reasons = dropped if dropped is not None else {}

    def keep(points: list[DeskPoint], limit: int, group: str = "") -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for point in points:
            text, basis = point.text.strip(), list(dict.fromkeys(point.basis))
            reason = statement_problem(text, basis, point.quote, by_id)
            if reason is not None:
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            if len(kept) < limit:
                # Whether it rests on data, on reports, or both — decided here, not by the model's wording.
                kinds = {by_id[item]["kind"] for item in basis}
                entry = {"text": text, "basis": basis, "kind": point.kind, "quote": point.quote.strip(),
                         "grounds": kinds.pop() if len(kinds) == 1 else "mixed"}
                if group:
                    direction = point.direction.strip()
                    entry["direction"] = direction if direction in DIRECTIONS else implied_direction(group, value.stance)
                kept.append(entry)
        return kept

    points = keep(value.points, 4, "points")
    headline, headline_basis = value.headline.strip(), list(dict.fromkeys(value.headlineBasis))
    if not points:
        raise ValueError("judgment_unanchored")
    risks, watch = keep(value.risks, 3, "risks"), keep(value.watch, 3)
    # The shown confidence is computed from what survived the checks; the model's own level is kept beside it.
    confidence, confidence_basis = rated_confidence(value.stance, points, risks, by_id)
    reason = _reject(headline, headline_basis, by_id) or ("length" if len(headline) > 60 else None)
    if reason is not None:
        # An unanchored headline is replaced by one that claims nothing beyond the stance.
        reasons["headline_" + reason] = reasons.get("headline_" + reason, 0) + 1
        headline, headline_basis = f"倾向{value.stance}，信心{confidence}", points[0]["basis"]
    cited = {*headline_basis, *(item for group in (points, risks, watch) for point in group for item in point["basis"])}
    basis = {item: {**{key: by_id[item].get(key) for key in ("section", "kind", "label", "value", "source", "asOf", "url")},
                    **{key: by_id[item][key] for key in QUALITY_KEYS if by_id[item].get(key)}}
             for item in by_id if item in cited}
    # The one-sentence conclusion cites nothing: its numbers must still be in some fact, and no advice.
    conclusion = value.conclusion.strip()
    if conclusion and sentence_problem(conclusion, facts, CONCLUSION_CHARS + 10):
        reasons["conclusion_dropped"] = reasons.get("conclusion_dropped", 0) + 1
        conclusion = ""
    return {"stance": value.stance, "confidence": confidence, "confidenceBasis": confidence_basis,
            "modelConfidence": value.confidence, "headline": headline, "headlineBasis": headline_basis,
            "points": points, "risks": risks, "watch": watch, "basis": basis, "conclusion": conclusion}


QUALITY_KEYS = ("status", "fetchedAt", "error")


def _quality(fact: dict[str, Any], status: Any, fetched_at: Any = None, error: Any = None) -> dict[str, Any]:
    """The source's own read time, and the stale mark (with its error) when the value is an old one."""
    if fetched_at:
        fact["fetchedAt"] = str(fetched_at)
    if status == "stale":
        fact["status"] = "stale"
        if error:
            fact["error"] = str(error)
    return fact


def _kept_quality(kept: Kept, fact: dict[str, Any]) -> dict[str, Any]:
    """A fact built from a slow source's kept value: stale (with the failure and the fetch time) when it is old."""
    return _quality(fact, "stale", kept.fetched_at, kept.error) if kept.stale else fact


def _fact(fact_id: str, section: str, label: str, value: str, source: str, as_of: str | None,
          url: str | None = None, detail: str | None = None, title: str | None = None) -> dict[str, Any]:
    kind = "report" if section in REPORT_SECTIONS else "data"
    fact = {"id": fact_id, "section": section, "kind": kind, "label": label, "value": value,
            "source": source, "asOf": as_of}
    if url:
        fact["url"] = url
    if detail:
        fact["detail"] = detail
    if title:
        fact["title"] = title
    return fact


def _with(fact: dict[str, Any], **figures: Any) -> dict[str, Any]:
    """The raw figures behind a fact's text, for the 关键数据 tiles; removed before the report goes out."""
    fact["figures"] = figures
    return fact


# 宏观按品种类别（第七批 2026-10-07，沿用第六批日历的类别 CALENDAR_CATEGORY）：每类只留与它有关的宏观事实，
# 其余不进这个品种的调研（沪金不再列 PMI、M2）；没有归类的品种不列宏观。O.nbs 是统计局最新发布的标题。
MACRO_BY_CATEGORY: dict[str, frozenset[str]] = {
    "precious": frozenset({"O.fred_DTWEXBGS", "O.fred_DGS10", "E.rates", "E.calendar"}),
    "energy": frozenset({"E.pmi", "E.ppi", "O.fred_DTWEXBGS", "E.calendar"}),
    "industrial": frozenset({"E.pmi", "E.ppi", "E.m2", "E.fai", "O.nbs", "E.rates", "E.calendar"}),
    "agri": frozenset({"E.cpi", "O.fred_DTWEXBGS", "E.calendar"}),
    "financial": frozenset({"E.pmi", "E.cpi", "E.ppi", "E.m2", "O.nbs", "O.fred_DGS10", "E.rates", "E.calendar"}),
}
# Which rates the 国债与资金利率 line keeps for a category (股指国债 keep all of them).
RATES_BY_CATEGORY: dict[str, tuple[str, ...]] = {"precious": ("us10Y", "us10Y2Y"), "industrial": ("china10Y", "lpr1Y", "lpr5Y")}


def macro_relevant(fact_id: str, product: str) -> bool:
    kinds = MACRO_BY_CATEGORY.get(CALENDAR_CATEGORY.get(product.lower(), ""), frozenset())
    return fact_id in kinds or (fact_id.startswith("O.nbs_") and "O.nbs" in kinds)


MACRO_SOURCE = "国家统计局 / 人民银行（东方财富数据中心）"


def macro_facts(series: dict[str, dict[str, Any]], product: str) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    if (row := series.get("pmi")) is not None:
        facts.append(_with(_fact("E.pmi", "macro", f"制造业PMI（{row['period']}）",
                                 f"{_num(row.get('MAKE_INDEX'))}，非制造业 {_num(row.get('NMAKE_INDEX'))}", MACRO_SOURCE, row["date"]),
                           make=row.get("MAKE_INDEX"), period=row["period"]))
    if (row := series.get("ppi")) is not None:
        facts.append(_fact("E.ppi", "macro", f"PPI（{row['period']}）", f"同比 {_signed(row.get('BASE_SAME'))}%",
                           MACRO_SOURCE, row["date"]))
    if (row := series.get("cpi")) is not None:
        facts.append(_fact("E.cpi", "macro", f"CPI（{row['period']}）",
                           f"同比 {_signed(row.get('NATIONAL_SAME'))}%，环比 {_signed(row.get('NATIONAL_SEQUENTIAL'))}%",
                           MACRO_SOURCE, row["date"]))
    if (row := series.get("m2")) is not None:
        facts.append(_fact("E.m2", "macro", f"M2（{row['period']}）",
                           f"同比 {_signed(row.get('BASIC_CURRENCY_SAME'))}%，M1 同比 {_signed(row.get('CURRENCY_SAME'))}%",
                           MACRO_SOURCE, row["date"]))
    if product in INVESTMENT_PRODUCTS and (row := series.get("fai")) is not None:
        facts.append(_fact("E.fai", "macro", f"城镇固定资产投资（{row['period']}当月）",
                           f"同比 {_signed(row.get('BASE_SAME'))}%", MACRO_SOURCE, row["date"]))
    return facts


def _lead(text: str, chars: int = 120) -> str:
    """The passage's opening, cut at a sentence end when there is one."""
    head = text[:chars]
    end = max(head.rfind("。"), head.rfind("；"))
    return head[:end + 1] if end >= 40 else head


def research_facts(notes: list[dict[str, Any]], aliases: list[str] | None = None) -> list[dict[str, Any]]:
    facts = [_fact(f"V.{hashlib.sha1((note['link'] or note['title']).encode()).hexdigest()[:8]}", "research",
                   note["media"], _lead(note["summary"]), "eastmoney", note["publishDate"], note["link"] or None,
                   note["summary"][:400], note["title"])
             for note in notes if note.get("summary")]
    # Stock figures the notes quote (social, port and plant stocks from paid consultancies): the only
    # inventory there is for DCE/CZCE/GFEX products, which publish no warehouse stock report.
    mentions: list[dict[str, Any]] = []
    for note in notes:
        for text in inventory_mentions(note.get("summary") or "", aliases or []):
            if len(mentions) < 3 and all(text != fact["value"] for fact in mentions):
                mentions.append(_fact(f"V.inv{hashlib.sha1(text.encode()).hexdigest()[:6]}", "research",
                                      f"{note['media']}引用的库存", text, "eastmoney", note["publishDate"], note["link"] or None))
    return facts + mentions


def _num(value: Any, digits: int = 2) -> str:
    if not isinstance(value, int | float):
        return "—"
    rounded = round(float(value), digits)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.{digits}f}".rstrip("0").rstrip(".")


def _signed(value: Any, digits: int = 2) -> str:
    text = _num(value, digits)
    return f"+{text}" if isinstance(value, int | float) and value > 0 else text


def dataset_fact(name: str, item: dict[str, Any]) -> dict[str, Any] | None:
    fact = _dataset_fact(name, item)
    return None if fact is None else _quality(fact, item.get("status"), item.get("fetchedAt"), item.get("error"))


def _dataset_fact(name: str, item: dict[str, Any]) -> dict[str, Any] | None:
    data = item.get("data") or {}
    source, as_of = str(item.get("source") or "AKShare"), item.get("asOf")
    if name == "spot_basis":
        rate = data.get("dominantBasisRate")
        value = f"现货 {_num(data.get('spotPrice'))}，主力基差 {_signed(data.get('dominantBasis'))}" + (
            f"（{_signed(rate * 100)}%）" if isinstance(rate, int | float) else "")
        basis = data.get("dominantBasis")
        if isinstance(basis, int | float) and basis:  # futures minus spot: say which side is higher
            value += f"，期货比现货{'高' if basis > 0 else '低'} {_num(abs(basis))}"
        return _with(_fact("D.spot_basis", "fundamental", "现货与基差", value, source, as_of), basis=data.get("dominantBasis"))
    if name == "warehouse_receipt":
        return _fact("D.warehouse_receipt", "fundamental", "仓单",
                     f"{_num(data.get('total'))}，较上日 {_signed(data.get('change'))}", source, as_of)
    if name == "position_rank":
        # Which contract the rank is for (or the whole product, where the exchange ranks per product).
        scope = str(data.get("contract") or "").upper()
        whose = f"（{scope}{' 全品种' if data.get('scope') == 'product' else ''}）" if scope else ""
        return _with(_fact("D.position_rank", "fundamental", f"前20会员净多持仓{whose}",
                           f"{_signed(data.get('netLong'))}（多 {_num(data.get('longTotal'))}，空 {_num(data.get('shortTotal'))}）",
                           source, as_of), netLong=data.get("netLong"))
    if name == "inventory":
        raw = data.get("latest")
        latest: dict[str, Any] = raw if isinstance(raw, dict) else {}
        return _fact("D.inventory", "fundamental", "库存",
                     f"{_num(latest.get('value'))}，{TRENDS.get(str(data.get('trend')), '趋势未知')}",
                     source, str(latest.get("date") or as_of or ""))
    if name == "term_structure":
        return _fact("D.term_structure", "fundamental", "期限结构",
                     f"{SHAPES.get(str(data.get('shape')), '形态未知')}，{data.get('nearContract')} 到 {data.get('farContract')} 价差 {_signed(data.get('spread'))}",
                     source, as_of)
    return None


def _stamp(value: Any) -> str:
    """A bar time (epoch nanoseconds) or ISO instant as China time, to the minute."""
    try:
        if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
            instant = datetime.fromtimestamp(int(value) / 1e9, UTC)
        else:
            instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            instant = instant if instant.tzinfo else instant.replace(tzinfo=CHINA)
    except (ValueError, OverflowError, OSError):
        return str(value or "")
    return instant.astimezone(CHINA).isoformat(timespec="minutes")


def market_facts(symbol: str, workbench: dict[str, Any]) -> list[dict[str, Any]]:
    from .trader_tools import _market_analysis  # lazy: trader_tools imports the research modules

    analysis = _market_analysis(workbench, lookback=20)
    quote = workbench.get("quote") or {}
    code = symbol.split(".")[-1].upper()
    as_of = _stamp(analysis.get("windowEnd"))[:10] if analysis.get("windowEnd") else _stamp(workbench.get("capturedAt"))
    day = quote.get("changePercent")
    # The live quote carries its own time and status; without one the last daily close stands in, on its bar date.
    live = isinstance(quote.get("lastPrice"), int | float)
    price = quote["lastPrice"] if live else analysis["lastClose"]
    indicators = analysis.get("indicators") or {}
    rsi = indicators.get("rsi") or {}
    side = "上方" if analysis["sma5"] > analysis["sma20"] else "下方" if analysis["sma5"] < analysis["sma20"] else "附近"
    source = "快期行情"
    quoted = _stamp(quote["quotedAt"]) if live and quote.get("quotedAt") else as_of
    quote_status = "stale" if live and quote.get("dataStatus") == "stale" else None
    facts = [
        _with(_quality(_fact("M.price", "market", f"主力 {code} {'最新价' if live else '最近收盘价'}",
                             f"{_num(price, 4)}" + (f"，日涨跌 {_signed(day)}%" if isinstance(day, int | float) else ""), source, quoted),
                       quote_status, error="quote_stale"), price=price, day=day, code=code),
        _with(_fact("M.trend", "market", "近20个交易日",
                    f"涨跌 {_signed(analysis['periodReturnPercent'])}%，振幅 {_num(analysis['rangePercent'])}%，5日均线在20日均线{side}",
                    source, as_of), change=analysis["periodReturnPercent"], side=side),
        _with(_fact("M.position", "market", "近20个交易日持仓变化",
                    f"{_signed(analysis['openInterestChange'], 0)} 手" + (
                        f"，最新成交量为20日均量的 {_num(analysis['volumeRatio'])} 倍" if analysis.get("volumeRatio") is not None else ""),
                    source, as_of), change=analysis["openInterestChange"], volumeRatio=analysis.get("volumeRatio")),
        _with(_fact("M.indicators", "market", "日线指标",
                    f"RSI {_num(rsi.get('value'), 1)}（{rsi.get('reading', '样本不足')}），{(indicators.get('boll') or {}).get('reading', '')}，"
                    f"{(indicators.get('atr') or {}).get('reading', '')}", source, as_of), rsi=rsi.get("value"), reading=rsi.get("reading")),
    ]
    upper, lower = quote.get("upperLimit"), quote.get("lowerLimit")
    if isinstance(upper, int | float) and isinstance(lower, int | float) and isinstance(price, int | float) and price > 0:
        distance = min(upper - price, price - lower) / price * 100
        facts.append(_quality(_fact("M.limit", "market", "今日涨跌停价",
                                    f"涨停 {_num(upper, 4)}，跌停 {_num(lower, 4)}，距最近停板 {_num(max(distance, 0.0))}%", source, quoted),
                              quote_status, error="quote_stale"))
    return facts


def related_fact(product: str, relation: str, name: str, symbol: str, workbench: dict[str, Any]) -> dict[str, Any]:
    bars = [bar for bar in workbench.get("bars") or [] if isinstance(bar, dict)]
    quote = workbench.get("quote") or {}
    closes = [float(bar["close"]) for bar in bars[-21:] if isinstance(bar.get("close"), int | float)]
    period = (closes[-1] / closes[0] - 1) * 100 if len(closes) >= 2 and closes[0] else None
    day = quote.get("changePercent")
    parts = [f"日涨跌 {_signed(day)}%" if isinstance(day, int | float) else "日涨跌未知"]
    if period is not None:
        parts.append(f"近20个交易日 {_signed(period)}%")
    return _fact(f"R.{product}", "related", f"{name} {symbol.split('.')[-1].upper()}（{relation}）", "，".join(parts),
                 "快期行情", _stamp(workbench.get("capturedAt")))


def linkage_fact(name: str, workbench: dict[str, Any], related: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
    """关联品种联动度 (2026-10-25, linkage.py from Vibe-Trading): this product's correlation of daily returns with each
    related product over the last 20 shared trading days, and whether the basket moves as one (抱团) — when it
    started, by the hysteresis state on the same days. None without 21 shared daily closes."""
    def closes(bench: dict[str, Any]) -> dict[str, float]:
        return {daily_bar_day(bar): float(bar["close"]) for bar in bench.get("bars") or []
                if isinstance(bar, dict) and isinstance(bar.get("close"), int | float) and bar.get("datetime") is not None}
    series = {name: closes(workbench), **{other: closes(bench) for other, bench in related}}
    days, returns = linkage.aligned_returns(series)
    if len(days) < linkage.WINDOW or len(series) < 2:
        return None
    window = slice(-linkage.WINDOW, None)
    pairs = [(other, linkage.correlation(returns[name][window], returns[other][window])) for other, _ in related]
    parts = [f"与{other} {value:.2f}" for other, value in pairs if value is not None]
    if not parts:
        return None
    states = linkage.regimes(linkage.edge_density(returns))
    smoothed, fused = states[-1]
    text = f"近 {linkage.WINDOW} 个交易日日收益相关系数：" + "、".join(parts)
    if smoothed is not None and len(series) >= 3:  # two products: the one correlation already says it
        if fused:
            since = len(states) - 1
            while since > 0 and states[since - 1][1]:
                since -= 1
            first = next(index for index, (value, _) in enumerate(states) if value is not None)
            text += f"；{len(series)} 个品种整体抱团（{'至少' if since <= first else ''}自 {days[since][5:]} 起）"
        else:
            text += f"；{len(series)} 个品种没有抱团"
    return _fact("R.linkage", "related", "关联品种联动度", text, "快期行情（程序计算）", days[-1])


LEVEL_BARS = 20


# A trading day's daily bar is final once its day session has closed and settled (15:00, 中金所国债 15:15).
DAY_BAR_CLOSED_AT = clock_time(15, 30)


def closed_closes(bars: list[dict[str, Any]], now: datetime, trading_days: list[str]) -> list[tuple[str, float]]:
    """(trading day, close) of the daily bars that have closed, oldest first — the one rule for every figure
    多 AI 研讨 freezes or scores (K.levels, the closed-market 发起价, the five-day reckoning). The current trading
    day's bar is still forming until DAY_BAR_CLOSED_AT; after the night turn (18:00) that day is behind the
    current one. The desk's own 今日判断 facts (行情、走势、指标) keep reading the live bar."""
    current = trading_day_of(int(now.timestamp() * 1e9), trading_days or None)
    local = now.astimezone(CHINA)
    today_closed = local.date().isoformat() == current and local.time() >= DAY_BAR_CLOSED_AT
    out: list[tuple[str, float]] = []
    for bar in bars:
        if not isinstance(bar, dict) or not isinstance(bar.get("close"), int | float) or bar["close"] <= 0:
            continue
        try:
            day = daily_bar_day(bar)
        except (KeyError, TypeError, ValueError):
            continue
        if day < current or (day == current and today_closed):
            out.append((day, float(bar["close"])))
    return sorted(out)


def levels_fact(workbench: dict[str, Any], symbol: str, now: datetime, trading_days: list[str]) -> dict[str, Any] | None:
    """多 AI 研讨's 技术面 levels, computed by the program (never by a model): the last 20 closed daily closes'
    high, low and average — the price figures a 失效位 may be taken from (a forming bar's moving close never is).
    None with fewer than 5 bars. Used only by the debate: the desk's 今日判断 facts are unchanged."""
    closes = [close for _, close in closed_closes(workbench.get("bars") or [], now, trading_days)[-LEVEL_BARS:]]
    if len(closes) < 5:
        return None
    count = len(closes)
    return _fact("K.levels", "market", f"近{count}个交易日收盘价",
                 f"最高 {_num(max(closes), 4)}，最低 {_num(min(closes), 4)}，均价 {_num(sum(closes) / count, 4)}",
                 "快期行情", _stamp(workbench.get("capturedAt")))


HISTORY_FACT_BARS = 30


def history_fact(workbench: dict[str, Any], now: datetime, trading_days: list[str]) -> dict[str, Any] | None:
    """多 AI 研讨's 技术面分析师 reads the K-line history itself (owner 2026-10-24): the last 30 closed daily closes
    as one fact (MM-DD close; …), so every figure it reads off them passes the same number check. None with fewer
    than 5 bars."""
    closes = closed_closes(workbench.get("bars") or [], now, trading_days)[-HISTORY_FACT_BARS:]
    if len(closes) < 5:
        return None
    return _fact("K.history", "market", f"近{len(closes)}个交易日日线收盘", "；".join(f"{day[5:]} {_num(close, 4)}" for day, close in closes),
                 "快期行情（日线，只取已收盘）", closes[-1][0])


def _last_trade_day(quote: dict[str, Any], now: datetime, trading_days: list[str]) -> str | None:
    """The trading day of a quote's last trade, by the same calendar as `closed_closes` — only for a quote the
    source vouches for (normal / fixture, not a cache) whose trade time is readable and not ahead of `now`;
    None when the quote cannot say."""
    if quote.get("dataStatus") not in {"normal", "simulated"} or bool(quote.get("isCached")):
        return None
    try:
        traded = datetime.fromisoformat(str(quote.get("quoteDatetime") or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    traded = traded if traded.tzinfo else traded.replace(tzinfo=CHINA)
    if (traded - now).total_seconds() > MAX_FUTURE_QUOTE_SKEW_SECONDS:
        return None
    return trading_day_of(int(traded.timestamp() * 1e9), trading_days or None)


def debate_sessions(trading_day: str, schedule: dict[str, Any] | None, trading_days: list[str]) -> list[Session] | None:
    """The sessions of one trading day for the 发起价's closed-market rule; None without a usable schedule.
    The schedule is normalized first (a night end written 02:30 is 26:30). `sessions_of_day` puts the night session
    on the previous trading day's evening; across a holiday (a weekday between the two that is not a trading day)
    the exchanges hold no night session, so it is left out — the evening before the holiday and the holiday itself
    are closed, and a placeholder bar is not taken for a trade."""
    from .account_reminders import normalize_schedule  # lazy: account_reminders pulls in the card stack

    normalized = normalize_schedule(schedule)
    if normalized is None:
        return None
    sessions = sessions_of_day(trading_day, normalized, trading_days)
    if trading_days:
        # Only a weekday the calendar covers can be a holiday; outside its span the weekday rule stands in.
        evening = date.fromisoformat(previous_trading_day(trading_day, trading_days))
        current, known = date.fromisoformat(trading_day), set(trading_days)
        gap = (evening + timedelta(days=offset) for offset in range(1, (current - evening).days))
        if any(day.weekday() < 5 and trading_days[0] <= day.isoformat() <= trading_days[-1]
               and day.isoformat() not in known for day in gap):
            sessions = [item for item in sessions if not item.night]
    return sessions


def debate_entry(quote: dict[str, Any], bars: list[dict[str, Any]], now: datetime,
                 trading_days: list[str], schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    """多 AI 研讨's 发起价, which the five-day reckoning is scored from, with where it came from:
    - a quote current by the order path's own rule (`quote_is_fresh`): source quote, the trading day it trades in;
    - the market closed after the current trading day has traded (a break inside the day session, 15:00–15:30,
      after the night session until the day session, a weekend after a Friday night session): source last, the
      quote's last price — the price the 底稿 shows — on the current trading day, so the move made before the
      debate began is not scored (#761);
    - the market closed and the current trading day not traded yet (before the night session, a product with no
      night session, a weekend or holiday) or already settled: the last closed daily close (`closed_closes`;
      source close, that bar's trading day);
    - the market closed, the current trading day traded, and no quote that can say at what price: no price
      (`closed_price_missing`); no closed close at all: no price (`close_missing`);
    - a quote gone stale while the market trades: no price (the order path's own reason, `quote_*`).
    Whether the market is closed is not left to the quote alone (#809): between sessions the source may serve no
    quote, or one whose session status is unknown, and that is not a stale quote in trading hours. The quote's
    own `closed` decides first; otherwise the contract's trading schedule with the exchange calendar
    (`debate_sessions`) says whether now is inside a session. Without a usable schedule only the quote can say.
    The reckoning reads the five closes after `day`."""
    moment = int(now.timestamp() * 1e9)
    today = trading_day_of(moment, trading_days or None)
    snapshot = {key: quote.get(key) for key in ("marketSessionStatus", "dataStatus", "isCached", "ageSeconds", "quoteDatetime")}
    raw = quote.get("lastPrice")
    price = float(raw) if isinstance(raw, int | float) and raw > 0 else None
    sessions = debate_sessions(today, schedule, trading_days)
    # Outside the schedule a quote is never the 发起价, however fresh: in the call auction before the night open the
    # source still shows the last session's price (#809 review).
    off_schedule = sessions is not None and not any(item.start_ns <= moment <= item.end_ns for item in sessions)
    # Nothing of the current trading day has traded before its first session begins (a placeholder bar, or a
    # pre-open packet stamped after the 18:00 turn, is no trade).
    begun = sessions is None or any(item.start_ns <= moment for item in sessions)
    try:
        quote_is_fresh(snapshot, allow_fixture=True, now=now)
    except SimulationOrderError as error:
        reason = str(error)
    else:
        reason = "quote_price_missing"
        if price is not None and not off_schedule:
            return {"price": price, "source": "quote", "day": today, "at": quote.get("quotedAt")}
    if quote.get("marketSessionStatus") == "closed" or off_schedule:
        done = closed_closes(bars, now, trading_days)
        if done and done[-1][0] == today:  # the current trading day has already closed and settled
            return {"price": done[-1][1], "source": "close", "day": today, "at": today}
        traded = _last_trade_day(quote, now, trading_days) if begun else None
        if traded == today and price is not None:
            return {"price": price, "source": "last", "day": today, "at": quote.get("quotedAt")}
        forming = begun and any(isinstance(bar, dict) and _bar_day(bar) == today for bar in bars)
        if traded == today or (traded is None and forming):
            reason = "closed_price_missing"
        elif done:
            day, close = done[-1]
            return {"price": close, "source": "close", "day": day, "at": day}
        else:
            reason = "close_missing"
    return {"price": None, "source": None, "day": None, "reason": reason}


def _bar_day(bar: dict[str, Any]) -> str | None:
    try:
        return daily_bar_day(bar)
    except (KeyError, TypeError, ValueError):
        return None


def report_facts(news: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for item in (news.get("results") or [])[:REPORT_FACTS]:
        title, link = str(item.get("title") or "").strip(), str(item.get("link") or "")
        if not title:
            continue
        digest = hashlib.sha1((link or title).encode()).hexdigest()[:8]
        media = str(item.get("media") or "").strip() or "来源未注明"
        facts.append(_fact(f"N.{digest}", "report", media, title, str(item.get("source") or "news"),
                           str(item.get("publishDate") or "") or None, link or None,
                           str(item.get("summary") or "")[:200] or None))
    return facts


TILE_LIMIT = 8


def _when(value: Any) -> str:
    """A fact's date for a tile's small print: MM-DD, or MM-DD HH:MM for an instant (China time)."""
    text = str(value or "")
    if re.fullmatch(r"\d{8}", text):
        return f"{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text[5:]
    stamp = _stamp(text) if text else ""
    return f"{stamp[5:10]} {stamp[11:16]}" if len(stamp) >= 16 else text[5:16]


def _color(value: Any) -> str:
    return "up" if isinstance(value, int | float) and value > 0 else "down" if isinstance(value, int | float) and value < 0 else "none"


def _wan(value: float, unit: str = "") -> str:
    """Large counts in 万 (+21.9 万, +9.6 万手); small ones as they are."""
    return f"{_signed(value / 10000, 1)} 万{unit}" if abs(value) >= 10000 else f"{_signed(value, 0)} {unit}".rstrip()


def desk_tiles(product: str, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """关键数据 (第七批): up to eight figures picked from the facts — the main price, the foreign
    counterpart, the 20-day move, the daily RSI, the spot basis, the top-20 net long, the 20-day
    open-interest change and the one macro figure that matters most to the product's category. Each
    tile: label, value, color (up / down / none), a note with the data's date, and `ref` (the fact id).
    A figure the facts do not carry is left out, never shown as zero."""
    by_id = {fact["id"]: fact for fact in facts}
    tiles: list[dict[str, Any]] = []

    def figures(fact_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        fact = by_id.get(fact_id)
        return (fact, fact["figures"]) if fact is not None and isinstance(fact.get("figures"), dict) else None

    def add(label: str, value: str, color: str, note: str, ref: str) -> None:
        tiles.append({"label": label, "value": value, "color": color, "note": note, "ref": ref})

    def number(value: Any) -> bool:
        return isinstance(value, int | float)

    if (found := figures("M.price")) and number(found[1].get("price")):
        fact, data = found
        day = data.get("day")
        add(f"主力 {data.get('code') or ''}".strip(), _num(data["price"]), _color(day),
            " · ".join(filter(None, (f"{_signed(day)}%" if number(day) else "", _when(fact.get("asOf"))))), "M.price")
    if (found := figures("M.foreign")) and number(found[1].get("price")):
        fact, data = found
        change = data.get("change")
        moment = str(data.get("quotedAt") or "")
        add(str(data.get("name") or "外盘"), _num(data["price"]), _color(change),
            " · ".join(filter(None, (f"{_signed(change)}%" if number(change) else "", moment[5:] if len(moment) > 10 else _when(fact.get("asOf"))))),
            "M.foreign")
    if (found := figures("M.trend")) and number(found[1].get("change")):
        fact, data = found
        add("近 20 日", f"{_signed(data['change'])}%", _color(data["change"]),
            f"5日线在20日线{data.get('side') or ''} · {_when(fact.get('asOf'))}", "M.trend")
    if (found := figures("M.indicators")) and number(found[1].get("rsi")):
        fact, data = found
        add("日线 RSI", _num(data["rsi"], 1), "none", " · ".join(filter(None, (str(data.get("reading") or ""), _when(fact.get("asOf"))))),
            "M.indicators")
    if (found := figures("D.spot_basis")) and number(found[1].get("basis")):
        fact, data = found
        basis = data["basis"]
        add("现货基差", _signed(basis), _color(basis),
            # AKShare's dom_basis is futures minus spot (10-08 rb: spot 3200, RB2701 3112, -88).
            f"{'期货比现货高' if basis > 0 else '期货比现货低' if basis < 0 else '期现持平'} · {_when(fact.get('asOf'))}", "D.spot_basis")
    if (found := figures("D.position_rank")) and number(found[1].get("netLong")):
        fact, data = found
        net = data["netLong"]
        add("前 20 净多", _wan(net), _color(net), f"{'净多头' if net > 0 else '净空头' if net < 0 else '多空持平'} · {_when(fact.get('asOf'))}",
            "D.position_rank")
    if (found := figures("M.position")) and number(found[1].get("change")):
        fact, data = found
        ratio = data.get("volumeRatio")
        add("20 日持仓变化", _wan(data["change"], "手"), _color(data["change"]),
            " · ".join(filter(None, (f"量 {_num(ratio)} 倍均量" if number(ratio) else "", _when(fact.get("asOf"))))), "M.position")
    macro = _macro_tile(CALENDAR_CATEGORY.get(product.lower(), ""), figures)
    if macro is not None:
        tiles.append(macro)
    return tiles[:TILE_LIMIT]


def _macro_tile(category: str, figures: Callable[[str], tuple[dict[str, Any], dict[str, Any]] | None]) -> dict[str, Any] | None:
    """The category's macro tile: 贵金属 the US 10-year yield, 能源化工 the EIA crude stocks (else the dollar),
    黑色 / 有色 / 股指国债 China's manufacturing PMI; 农产品 and unmapped products have none."""
    def change_note(change: Any, asof: Any) -> str:
        return " · ".join(filter(None, (f"较前值 {_signed(change)}" if isinstance(change, int | float) else "", _when(asof))))

    if category == "precious":
        if (found := figures("E.rates")) and isinstance(found[1].get("us10Y"), dict) and isinstance(found[1]["us10Y"].get("value"), int | float):
            entry = found[1]["us10Y"]
            return {"label": "美 10 年期国债", "value": f"{_num(entry['value'])}%", "color": "none",
                    "note": _when(entry.get("date")), "ref": "E.rates"}
        if (found := figures("O.fred_DGS10")) and isinstance(found[1].get("value"), int | float):
            fact, data = found
            return {"label": "美 10 年期国债", "value": f"{_num(data['value'])}%", "color": _color(data.get("change")),
                    "note": change_note(data.get("change"), fact.get("asOf")), "ref": "O.fred_DGS10"}
        return None
    if category == "energy":
        if (found := figures("O.eia_crude")) and isinstance(found[1].get("change"), int | float):
            fact, data = found
            total = data.get("value")
            return {"label": "EIA 原油库存周变化", "value": f"{_signed(data['change'], 0)} 千桶", "color": _color(data["change"]),
                    "note": " · ".join(filter(None, (f"总量 {_num(total / 1000, 1)} 百万桶" if isinstance(total, int | float) else "",
                                                     _when(fact.get("asOf"))))), "ref": "O.eia_crude"}
        if (found := figures("O.fred_DTWEXBGS")) and isinstance(found[1].get("value"), int | float):
            fact, data = found
            return {"label": "美元指数", "value": _num(data["value"]), "color": _color(data.get("change")),
                    "note": change_note(data.get("change"), fact.get("asOf")), "ref": "O.fred_DTWEXBGS"}
        return None
    if category in {"industrial", "financial"}:
        if (found := figures("E.pmi")) and isinstance(found[1].get("make"), int | float):
            fact, data = found
            make = data["make"]
            return {"label": "中国制造业 PMI", "value": _num(make), "color": "up" if make > 50 else "down" if make < 50 else "none",
                    "note": f"{'扩张' if make > 50 else '收缩' if make < 50 else '荣枯线'} · {re.sub(r'^\d{4}年', '', str(data.get('period') or '')) or _when(fact.get('asOf'))}",
                    "ref": "E.pmi"}
    return None


@dataclass
class ResearchDeskService:
    research: ResearchDataService
    news: Callable[[str], Awaitable[dict[str, Any]]]
    provider: Callable[[], Any]
    # The live intel service (快讯/外盘/日历/利率/生猪): flash facts, the foreign line and the
    # macro extras come through it; None keeps the desk on its original seven sections.
    extra: ResearchLiveService | None = None
    sources: PublicSources = field(default_factory=PublicSources)
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    daily_calls: int = field(default_factory=lambda: int(os.getenv("AI_TRADER_RESEARCH_DESK_DAILY_CALLS", "200") or 0))
    model: str = field(default_factory=lambda: os.getenv("AI_TRADER_RESEARCH_DESK_MODEL", "deepseek-flash").strip())
    _facts: dict[str, tuple[float, float, dict[str, Any]]] = field(default_factory=dict, repr=False)
    _facts_pending: dict[str, asyncio.Task[dict[str, Any]]] = field(default_factory=dict, repr=False)
    _catalog: tuple[float, dict[str, Any]] | None = field(default=None, repr=False)
    _judgments: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _judging: dict[str, asyncio.Task[None]] = field(default_factory=dict, repr=False)
    # The last failed run until one succeeds: (monotonic end of backoff, error, ISO time a retry may start).
    _failures: dict[str, tuple[float, str, str]] = field(default_factory=dict, repr=False)
    _limit: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2), repr=False)
    # A judgment waiting for a loading section: (generator, reason), started once the section lands.
    _deferred: dict[str, tuple[Any, str]] = field(default_factory=dict, repr=False)
    # A deferred judgment that did not run once its section landed (too few facts, the day's cap, a backoff): its
    # 重新判断 receipt, shown with the judgment until the next run starts (#516) — never a silent "done".
    _skipped: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _started: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)  # key → (stage, ISO start)
    _durations: deque[float] = field(default_factory=lambda: deque(maxlen=10), repr=False)
    _seen: dict[str, float] = field(default_factory=dict, repr=False)
    _loop: asyncio.Task[None] | None = field(default=None, repr=False)
    # Products the pre-generation asked a judgment for: stop_loop() cancels their model calls with it.
    _pregenerated: set[str] = field(default_factory=set, repr=False)
    # Set by close(): done-callbacks that fire after it must not collect facts or write judgments.
    _closed: bool = field(default=False, repr=False)
    # Set by the research routes: the model adapter a user may start judgments with (None: not allowed).
    generator_for: Callable[[str], Any] | None = None
    # 锁屏推送 · 开盘前判断 (2026-10-06): called after the morning pre-generation with the day's
    # judgments (`day_judgments`) and the research day; the main runtime's push hub sets it.
    on_pregenerated: Callable[[dict[str, dict[str, Any]], str], Awaitable[None]] | None = None
    # Browser tests only: the recorded fake model every signed-in reader's judgment is written with.
    fixed_generator: Any = None
    # 判断可能过时 (第七批): the publish times of the product's 重要 news events of the research day,
    # re-read with the facts; the shown judgment counts those after it.
    _majors: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _changes: dict[str, tuple[str, dict[str, Any] | None]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._db: sqlite3.Connection | None = getattr(self.research, "connection", None)
        self._db_lock = getattr(self.research, "lock", None)
        if self._db is not None and self._db_lock is not None:
            with self._db_lock, self._db:
                self._db.execute("CREATE TABLE IF NOT EXISTS research_desk_judgments (product_key TEXT PRIMARY KEY,"
                                 " payload_json TEXT NOT NULL)")
                # The last collected facts per product (2026-10-22): a restart or the other slot serves them at once.
                self._db.execute("CREATE TABLE IF NOT EXISTS research_desk_facts (product_key TEXT PRIMARY KEY,"
                                 " collected_at TEXT NOT NULL, payload_json TEXT NOT NULL)")
                self._db.execute("CREATE TABLE IF NOT EXISTS research_desk_watch (product_key TEXT PRIMARY KEY,"
                                 " symbol TEXT NOT NULL, user_id TEXT NOT NULL, seen_at TEXT NOT NULL)")
                # Every judgment ever made, never overwritten: the record the AI's track record is scored on.
                # The latest one per product (the table above) seeds it once when it first appears.
                self._db.execute("CREATE TABLE IF NOT EXISTS research_desk_judgment_history (product_key TEXT NOT NULL,"
                                 " generated_at TEXT NOT NULL, research_day TEXT NOT NULL, payload_json TEXT NOT NULL,"
                                 " PRIMARY KEY (product_key, generated_at))")
                self._db.execute("INSERT OR IGNORE INTO research_desk_judgment_history"
                                 " SELECT product_key, json_extract(payload_json, '$.generatedAt'),"
                                 " json_extract(payload_json, '$.researchDay'), payload_json FROM research_desk_judgments"
                                 " WHERE json_extract(payload_json, '$.generatedAt') IS NOT NULL"
                                 " AND json_extract(payload_json, '$.researchDay') IS NOT NULL")
        # The day's model calls count in the shared database (a restart or the other slot spends the same allowance).
        self._budget = DailyCallBudget("research_desk", self._db, self._db_lock)
        # The slow sources' last good values (国家统计局、CFTC、FRED、EIA、上期所周报、宏观、机构观点).
        self._slow = LastGood(self._db, self._db_lock, prefix="desk:", clock=lambda: self.clock())
        self._slow_collect = AsyncCollector(self._slow, lane="desk-slow")

    def _current(self, key: str) -> dict[str, Any] | None:
        """The product's latest judgment, read from the shared table every time: the lease holder pre-generates,
        a debate may be adopted on the other slot, and a reader on either must see the same judgment. Without a
        database (or before anything is stored) the in-memory copy stands in."""
        if self._db is not None and self._db_lock is not None:
            with self._db_lock:
                row = self._db.execute("SELECT payload_json FROM research_desk_judgments WHERE product_key = ?",
                                       (key,)).fetchone()
            if row is not None:
                try:
                    entry = json.loads(row[0])
                except ValueError:
                    entry = None
                if isinstance(entry, dict):
                    self._judgments[key] = entry
                    return entry
        return self._judgments.get(key)

    async def report(self, symbol: str, *, generator: Any = None, force: bool = False,
                     user_id: str | None = None, fast: bool = False) -> dict[str, Any]:
        """Facts plus the shared judgment. `generator` (a model adapter) may start a new
        judgment; without one, a reader sees whatever judgment already exists. `fast` (the panel's read)
        never waits for a collection: see FIRST_READ_WAIT_SECONDS."""
        exchange, product, foreign = research_target(symbol)  # ValueError("research_symbol_invalid")
        key = f"{exchange}.{product.lower()}"
        cached = self._facts.get(key) or self._stored_facts(key)
        now = self.clock()
        forced = force and cached is not None and now - cached[1] >= FORCE_MIN_SECONDS
        refreshing = False
        if cached is not None and cached[0] <= now and not forced and (fast or now - cached[1] <= STALE_SERVE_SECONDS):
            # Recent facts go out at once (the chat AI and the panel do not wait); the re-read runs behind.
            # The panel takes older ones too and reads again once the re-read lands.
            self._recollect(key, exchange, product)
            facts, refreshing = cached[2], now - cached[1] > STALE_SERVE_SECONDS
        elif cached is None or cached[0] <= now or forced:
            task = self._recollect(key, exchange, product)
            if fast and cached is None:
                try:
                    facts = await asyncio.wait_for(asyncio.shield(task), FIRST_READ_WAIT_SECONDS)
                except TimeoutError:
                    facts, refreshing = self._placeholder(exchange, product), True
            else:
                facts = await asyncio.shield(task)
        else:
            facts = cached[2]
        if user_id:
            self._note_seen(key, symbol, user_id)
        # Facts being re-read because they are old (or not there yet) do not start a judgment: it waits for the
        # re-read, exactly as for a section still loading (`_deferred`, resumed when the collection lands).
        judged = {**facts, "sections": [{**item, "status": "loading"} for item in facts["sections"]]} if refreshing else facts
        news = self._news_line(foreign.news_product or "" if foreign is not None else product.lower())
        judgment, outcome = self._judgment(key, judged, generator, force, news)
        if judgment.get("status") == "ready":
            judgment = self._annotated(key, judgment, news)
        report = {**facts, "symbol": symbol, "judgment": judgment}
        if refreshing:
            report["factsRefreshing"] = True
        if news is not None:
            report["news"] = news
        if force:
            report["rejudge"] = self._rejudge(outcome, judgment)
        return report

    # -- facts

    def _recollect(self, key: str, exchange: str, product: str) -> asyncio.Task[dict[str, Any]]:
        task = self._facts_pending.get(key)
        if task is None:
            task = asyncio.create_task(self._collect(key, exchange, product))
            self._facts_pending[key] = task

            def done(finished: asyncio.Task[dict[str, Any]]) -> None:
                self._facts_pending.pop(key, None)
                if finished.cancelled():
                    return
                if finished.exception() is not None:
                    log.warning("research_desk_collect_failed product=%s error=%r", key, finished.exception())
                elif not self._closed:
                    self._resume_deferred(key, finished.result())
            task.add_done_callback(done)
        return task

    def _placeholder(self, exchange: str, product: str) -> dict[str, Any]:
        """A product's sections as 加载中 while its first collection runs (the panel reads again shortly)."""
        spec = FOREIGN_SECTIONS if exchange == FOREIGN_RESEARCH else SECTIONS
        return {"exchange": exchange, "product": product, "productName": "", "mainSymbol": "",
                "generatedAt": self.now().isoformat(timespec="seconds"),
                "sections": [{"key": section, "title": title, "kind": kind, "status": "loading", "count": 0}
                             for section, title, kind in spec],
                "facts": [], "tiles": [], "notice": NOTICE}

    def _stored_facts(self, key: str) -> tuple[float, float, dict[str, Any]] | None:
        """The product's last collected facts from the shared table (a restart, the other slot), as an expired
        entry dated by its age; None when it was never collected."""
        if self._db is None or self._db_lock is None:
            return None
        with self._db_lock:
            row = self._db.execute("SELECT collected_at, payload_json FROM research_desk_facts WHERE product_key = ?",
                                   (key,)).fetchone()
        if row is None:
            return None
        try:
            age = (self.now() - datetime.fromisoformat(row[0])).total_seconds()
            payload = json.loads(row[1])
        except (TypeError, ValueError):
            return None
        return (0.0, self.clock() - max(0.0, age), payload)

    def _store_facts(self, key: str, payload: dict[str, Any]) -> None:
        if self._db is None or self._db_lock is None:
            return
        try:
            with self._db_lock, self._db:
                self._db.execute("INSERT INTO research_desk_facts (product_key, collected_at, payload_json) VALUES (?, ?, ?)"
                                 " ON CONFLICT(product_key) DO UPDATE SET collected_at = excluded.collected_at,"
                                 " payload_json = excluded.payload_json",
                                 (key, self.now().isoformat(timespec="seconds"), json.dumps(payload, ensure_ascii=False)))
        except sqlite3.Error as error:
            log.warning("research_desk_facts_store_failed product=%s error=%r", key, error)

    def _resume_deferred(self, key: str, facts: dict[str, Any]) -> None:
        """A judgment that waited for the facts (a loading section, or facts being re-read) starts once they land."""
        if any(item["status"] == "loading" for item in facts["sections"]):
            return
        if self._started.get(key, ("",))[0] == "facts":
            self._started.pop(key, None)
        deferred = self._deferred.pop(key, None)
        if deferred is not None:
            generator, reason = deferred
            judgment, outcome = self._judgment(key, facts, generator, reason == "manual")
            if outcome not in ("started", "running"):
                self._skipped[key] = self._rejudge(outcome, judgment)

    async def _collect(self, key: str, exchange: str, product: str) -> dict[str, Any]:
        if exchange == FOREIGN_RESEARCH:
            return await self._collect_foreign(key, product)
        catalog = await self._deadline(self._catalog_items())
        main = self._main(catalog, product, exchange) if isinstance(catalog, list) else None
        if isinstance(catalog, list) and main is None:
            raise ValueError("research_symbol_unknown")  # not cached: no entry per arbitrary string
        name = re.sub(r"[0-9]+$", "", str(main.get("instrumentName") or "")) if main else ""
        main_symbol = str(main["symbol"]) if main else ""
        majors = asyncio.ensure_future(self._major_events(key, product.lower()))
        flashed = asyncio.ensure_future(self._flash_facts(product.lower())) if self.extra is not None else None
        # One read of the main contract's daily bars, shared by the market section and the related section's 联动度.
        own = asyncio.ensure_future(self._workbench(main_symbol, with_quote=True)) if main_symbol else None
        results = await asyncio.gather(
            self._market_section(key, exchange, product, main_symbol, own),
            self._related_section(product.lower(), catalog if isinstance(catalog, list) else [], own, name),
            self._fundamental_section(key, exchange, product, main_symbol or f"{exchange}.{product}"),
            self._official_section(product.lower()),
            self._macro_section(product.lower()),
            self._research_section(name),
            self._report_section(main_symbol),
            return_exceptions=True,
        )
        return await self._assembled(key, SECTIONS, list(results), flashed, majors, exchange=exchange, product=product, name=name,
                               main_symbol=main_symbol, tiles_product=product, has_night=bool(main and main.get("hasNight")))

    async def _assembled(self, key: str, spec: tuple[tuple[str, str, str], ...], results: list[Any],
                         flashed: asyncio.Future[Any] | None, majors: asyncio.Future[Any] | None, *, exchange: str,
                         product: str, name: str, main_symbol: str, tiles_product: str, has_night: bool = False) -> dict[str, Any]:
        """The sections' results, the 快讯 picks and the major events into the cached facts payload."""
        # 快讯 picks are the judgment's input and source list, not a section of the report (第七批).
        flash: list[dict[str, Any]] = []
        for side in (flashed, majors):
            try:
                found = await side if side is not None else None
            except (ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
                log.warning("research_desk_news_failed product=%s error=%r", key, error)
                continue
            if side is flashed and isinstance(found, list):
                flash = found
        # A note shown under 机构观点 is not repeated under 相关报道.
        noted = {fact.get("url") for result in results if isinstance(result, tuple)
                 for fact in result[0] if fact["section"] == "research"}
        facts: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []
        for (section, title, kind), result in zip(spec, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                log.warning("research_desk_section_failed section=%s product=%s error=%r", section, key, result)
                result = ([], {"status": "unavailable", "error": "research_section_failed"})
            items, state = result
            if section == "report":
                items = [fact for fact in items if fact.get("url") not in noted]
            if not main_symbol and section in {"market", "related", "research", "report"}:
                state = {"status": "unavailable", "error": "research_product_name_unavailable"}
            old = next((fact for fact in items if fact.get("status") == "stale"), None)
            if old is not None and state.get("status") == "ok":
                state = {**state, "status": "stale", "error": state.get("error") or old.get("error")}
            facts.extend(items)
            sections.append({"key": section, "title": title, "kind": kind, **state, "count": len(items)})
        facts.extend(flash)
        tiles = desk_tiles(tiles_product, facts)
        for fact in facts:
            fact.pop("figures", None)
        # A loading section or missing market data is re-read after 30 seconds, not cached for five minutes.
        loading = any(item["status"] == "loading" or (item["key"] in {"market", "related"} and item.get("error") == "market_unavailable")
                      for item in sections)
        payload = {"exchange": exchange, "product": product, "productName": name, "mainSymbol": main_symbol,
                   "hasNight": has_night,
                   "generatedAt": self.now().isoformat(timespec="seconds"), "sections": sections, "facts": facts, "tiles": tiles,
                   "notice": NOTICE + (" " + FRED_NOTICE if any(f["id"].startswith("O.fred") for f in facts) else "")}
        self._facts[key] = (self.clock() + (FACTS_LOADING_TTL_SECONDS if loading else FACTS_TTL_SECONDS), self.clock(), payload)
        if not loading:
            self._store_facts(key, payload)
        return payload

    async def _collect_foreign(self, key: str, code: str) -> dict[str, Any]:
        """外盘品种的研究台事实（FOREIGN_SECTIONS）：快讯与重要事件按它的快讯键读（纳指 → nasdaq）。"""
        item = next((product for product in FOREIGN_PRODUCTS if product.key == code), None)
        if item is None:
            raise ValueError("research_symbol_unknown")
        screen = item.news_product or ""
        symbol = item.kqd or code
        majors = asyncio.ensure_future(self._major_events(key, screen)) if screen else None
        flashed = asyncio.ensure_future(self._flash_facts(screen)) if self.extra is not None and screen else None
        # 没有国内对照的外盘品种（纳指、外汇、美债）不出“对应国内品种”这一块，而不是列成缺失。
        spec = tuple(entry for entry in FOREIGN_SECTIONS if entry[0] != "related" or item.domestic)
        readers = {"market": self._foreign_market_section(item), "related": self._foreign_related_section(item),
                   "report": self._report_section(symbol)}
        if not item.domestic:
            readers.pop("related").close()
        results = await asyncio.gather(*readers.values(), return_exceptions=True)
        return await self._assembled(key, spec, list(results), flashed, majors, exchange=FOREIGN_RESEARCH,
                                     product=code, name=item.name, main_symbol=symbol, tiles_product=screen)

    async def _foreign_market_section(self, item: ForeignProduct) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """外盘的行情块：最新价与日涨跌取外盘报价（外盘报价页同一份，MT5 等源整行取自一个源），近 20 个交易日的
        走势与日线指标取快期外盘主连的日线（外盘图表的日线同源）。外盘没有涨跌停、持仓变化不可比，不出这两条。"""
        if self.extra is None:
            return [], {"status": "unavailable", "error": "foreign_market_unavailable"}
        markets = await self._deadline(asyncio.to_thread(self.extra.foreign_markets, None))
        rows = markets.get("items") or [] if isinstance(markets, dict) else []
        row = next((entry for entry in rows if entry.get("code") == item.key), None)
        bars: list[dict[str, Any]] = []
        if item.kqd:
            history = await self._deadline(asyncio.to_thread(self.provider().market_history, item.kqd, 86400, HISTORY_BARS))
            if isinstance(history, dict):
                bars = [bar for bar in history.get("bars") or [] if isinstance(bar, dict)]
        if row is None and not bars:
            return [], {"status": "unavailable", "error": "foreign_market_unavailable"}
        source = str(row.get("source") or "外盘行情") if row else "快期外盘主连"
        quote = {"symbol": item.kqd or item.key}
        if row is not None and isinstance(row.get("price"), int | float):
            quote.update(lastPrice=row["price"], changePercent=row.get("changePercent"), quotedAt=row.get("quotedAt"),
                         dataStatus="stale" if row.get("stale") else "normal")
        workbench = {"schemaVersion": 1, "liveTrading": "disabled", "dataStatus": "normal", "durationSeconds": 86400,
                     "capturedAt": self.now().isoformat(timespec="seconds"), "quote": quote, "bars": bars}
        try:
            facts = market_facts(item.kqd or item.key, workbench)
        except (ValueError, TypeError, KeyError):
            facts = []
            if "lastPrice" in quote:
                day = quote.get("changePercent")
                facts.append(_with(_fact("M.price", "market", f"{item.name} 最新价",
                                         f"{_num(quote['lastPrice'], 4)}" + (f"，日涨跌 {_signed(day)}%" if isinstance(day, int | float) else ""),
                                         source, _stamp(quote.get("quotedAt"))), price=quote["lastPrice"], day=day, code=item.key))
        shown = []
        for fact in facts:
            if fact["id"] in {"M.position", "M.limit"}:
                continue
            if fact["id"] == "M.price":
                fact = {**fact, "label": f"{item.name} {'最新价' if 'lastPrice' in quote else '最近收盘价'}（{row.get('unit') if row else ''}）".replace("（）", ""),
                        "source": source}
            elif fact["section"] == "market":
                fact = {**fact, "source": "快期外盘主连日线"}
            shown.append(fact)
        return shown, {"status": "ok", "source": source}

    async def _foreign_related_section(self, item: ForeignProduct) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """外盘对应的国内品种（COMEX黄金 → 沪金）：主力合约的日涨跌与近 20 个交易日涨跌；没有对照的不出这一块。"""
        if not item.domestic:
            return [], {"status": "unavailable", "error": "research_related_unmapped"}
        catalog = await self._deadline(self._catalog_items())
        items = catalog if isinstance(catalog, list) else []
        targets = [(other, main) for other in item.domestic if (main := self._main(items, other)) is not None]
        if not targets:
            return [], {"status": "unavailable", "error": "research_related_unavailable"}
        workbenches = await asyncio.gather(*(self._deadline(self._workbench(str(main["symbol"]), with_quote=True))
                                             for _, main in targets))
        facts = [related_fact(other, "国内对应品种", re.sub(r"[0-9]+$", "", str(main.get("instrumentName") or other)),
                              str(main["symbol"]), workbench)
                 for (other, main), workbench in zip(targets, workbenches, strict=True)
                 if not isinstance(workbench, BaseException)]
        return facts, ({"status": "ok", "source": "快期行情"} if facts else {"status": "unavailable", "error": "market_unavailable"})

    async def _deadline(self, awaitable: Awaitable[Any], seconds: float | None = None) -> Any:
        try:
            return await asyncio.wait_for(awaitable, SOURCE_DEADLINE_SECONDS if seconds is None else seconds)
        except TimeoutError:
            return TimeoutError()
        except (RuntimeError, ValueError, KeyError, TypeError, SourceError) as error:
            return error

    async def _catalog_items(self) -> list[dict[str, Any]]:
        if self._catalog is not None and self._catalog[0] > self.clock():
            return self._catalog[1]["items"]
        catalog = await asyncio.to_thread(self.provider().instrument_catalog)
        self._catalog = (self.clock() + CATALOG_TTL_SECONDS, catalog)
        return catalog.get("items") or []

    @staticmethod
    def _main(items: list[dict[str, Any]], product: str, exchange: str | None = None) -> dict[str, Any] | None:
        return next((item for item in items if item.get("isMain") and str(item.get("productId", "")).lower() == product.lower()
                     and (exchange is None or str(item.get("exchangeId", "")).upper() == exchange)), None)

    async def _workbench(self, symbol: str, *, with_quote: bool = False) -> dict[str, Any]:
        """Daily bars from the sidecar's history session (its own data-only TqApi, cached five minutes):
        research never adds K-line subscriptions to the live session, which TqSdk cannot unsubscribe.
        Only the main contract also reads its live quote (last price, day change, price limits)."""
        provider = self.provider()
        history = await asyncio.to_thread(provider.market_history, symbol, 86400, HISTORY_BARS)
        bars = [bar for bar in history.get("bars") or [] if isinstance(bar, dict)]
        quote: dict[str, Any] = {"symbol": symbol}
        if with_quote:
            try:
                snapshot = await asyncio.to_thread(provider.market_snapshot, "", symbol)
                quote.update(lastPrice=snapshot.get("price"), changePercent=snapshot.get("changePercent"),
                             upperLimit=snapshot.get("upperLimit"), lowerLimit=snapshot.get("lowerLimit"),
                             quotedAt=snapshot.get("quoteDatetime") or snapshot.get("capturedAt"),
                             dataStatus=snapshot.get("dataStatus"),
                             # What `quote_is_fresh` reads (多 AI 研讨's 发起价).
                             quoteDatetime=snapshot.get("quoteDatetime"), ageSeconds=snapshot.get("ageSeconds"),
                             isCached=snapshot.get("isCached"), marketSessionStatus=snapshot.get("marketSessionStatus"))
            except (RuntimeError, KeyError, TypeError):
                pass  # the bars still give the trend; the day change falls back to them below
        closes = [float(bar["close"]) for bar in bars[-2:] if isinstance(bar.get("close"), int | float)]
        if not isinstance(quote.get("changePercent"), int | float) and len(closes) == 2 and closes[0]:
            quote["changePercent"] = round((closes[1] / closes[0] - 1) * 100, 2)
        return {"schemaVersion": 1, "liveTrading": "disabled", "dataStatus": "normal", "durationSeconds": 86400,
                "capturedAt": history.get("capturedAt") or self.now().isoformat(timespec="seconds"),
                "quote": quote, "bars": bars}

    async def _market_section(self, key: str, exchange: str, product: str, symbol: str,
                              task: asyncio.Future[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not symbol or task is None:
            return [], {"status": "unavailable"}
        done, _ = await asyncio.wait({task}, timeout=SOURCE_DEADLINE_SECONDS)
        if not done:
            # A product's first daily-history download can outrun the deadline (白糖 9.4 s on 2026-09-29):
            # shown as loading, and the facts are re-read the moment it lands.
            self._started.setdefault(key, ("facts", self.now().isoformat(timespec="seconds")))
            task.add_done_callback(lambda _: asyncio.ensure_future(self._loaded(key, exchange, product)))
            return [], {"status": "loading"}
        if task.exception() is not None:
            return [], {"status": "unavailable", "error": "market_unavailable"}
        workbench = task.result()
        try:
            facts = market_facts(symbol, workbench)
        except (ValueError, TypeError, KeyError):
            return [], {"status": "unavailable", "error": "insufficient_market_bars"}
        foreign = await self._foreign_fact(product)
        if foreign is not None:
            facts.append(foreign)
        return facts, {"status": "ok", "source": "快期行情"}

    async def _foreign_fact(self, product: str) -> dict[str, Any] | None:
        """The market section's 外盘联动 line, read from the one foreign entry (foreign_market);
        a product without a mapped foreign counterpart, or a failed fetch, simply shows nothing — except related
        products a configured source should carry but none has a row for yet (``missing``, #932): those are said
        (“某某 暂无报价”), also when it is every related product."""
        if self.extra is None or not foreign_for_domestic(product):
            return None
        markets = await self._deadline(asyncio.to_thread(self.extra.foreign_markets, product.lower()))
        if not isinstance(markets, dict):
            return None
        items = (markets.get("items") or []) if markets.get("status") in {"ok", "stale"} else []
        missing = [item for item in markets.get("missing") or [] if isinstance(item, dict)]
        if not items and not missing:
            return None
        parts = []
        for item in items:
            moment = str(item.get("quotedAt") or "")[-5:]  # the 行情时间 tail: HH:MM
            percent = item.get("changePercent")
            change = f"（{_signed(percent)}%" if isinstance(percent, int | float) else ""
            if change and moment:
                change += f"，北京时间 {moment}）"
            elif change:
                change += "）"
            parts.append(f"{item['name']} {_num(item['price'])}{change}")
        parts.extend(f"{item['name']} 暂无报价" for item in missing)
        first = items[0] if items else {}
        return _with(_quality(_fact("M.foreign", "market", "外盘联动", "；".join(parts),
                                    str(markets.get("source") or "外盘行情"),
                                    str(first.get("quotedAt") or "")[:10] or None),
                              markets.get("status"), markets.get("fetchedAt"), markets.get("error")),
                     name=first.get("name"), price=first.get("price"), change=first.get("changePercent"),
                     quotedAt=first.get("quotedAt"))

    async def _related_section(self, product: str, items: list[dict[str, Any]],
                               own_task: asyncio.Future[dict[str, Any]] | None = None,
                               main_name: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
        targets = [(other, relation, main) for other, relation in RELATED.get(product, ())
                   if (main := self._main(items, other)) is not None]
        if not targets:
            return [], {"status": "unavailable",
                        "error": "research_related_unavailable" if product in RELATED else "research_related_unmapped"}
        workbenches = await asyncio.gather(*(self._deadline(self._workbench(str(main["symbol"]))) for _, _, main in targets))
        names = [re.sub(r"[0-9]+$", "", str(main.get("instrumentName") or other)) for other, _, main in targets]
        facts = [related_fact(other, relation, label, str(main["symbol"]), workbench)
                 for (other, relation, main), label, workbench in zip(targets, names, workbenches, strict=True)
                 if not isinstance(workbench, BaseException)]
        # The market section's own read of the main contract (shielded: its deadline here must not cancel that read).
        own = await self._deadline(asyncio.shield(own_task)) if own_task is not None else None
        if isinstance(own, dict):
            linked = [(label, bench) for label, bench in zip(names, workbenches, strict=True) if isinstance(bench, dict)]
            fact = linkage_fact(main_name or product, own, linked) if linked else None
            if fact is not None:
                facts.append(fact)
        return facts, ({"status": "ok", "source": "快期行情"} if facts else {"status": "unavailable", "error": "market_unavailable"})

    async def _fundamental_section(self, key: str, exchange: str, product: str,
                                   symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        task = asyncio.ensure_future(asyncio.to_thread(self.research.summary, symbol, DATASET_FACTS))
        done, _ = await asyncio.wait({task}, timeout=SOURCE_DEADLINE_SECONDS)
        if not done:
            # Still loading: once it lands, the facts are re-read at once and a waiting judgment starts.
            self._started.setdefault(key, ("facts", self.now().isoformat(timespec="seconds")))
            task.add_done_callback(lambda _: asyncio.ensure_future(self._loaded(key, exchange, product)))
            return [], {"status": "loading"}
        try:
            summary = task.result()
        except (RuntimeError, ValueError, KeyError, TypeError) as error:
            return [], {"status": "unavailable", "error": str(error) or "research_unavailable"}
        datasets = summary.get("datasets") or {}
        facts = [fact for name in DATASET_FACTS
                 if isinstance(datasets.get(name), dict) and datasets[name].get("status") in {"ok", "stale"} and datasets[name].get("data")
                 for fact in [dataset_fact(name, datasets[name])] if fact is not None]
        if product.lower() == "lh" and self.extra is not None:
            hog = await self._deadline(asyncio.to_thread(self.extra.hog_price))
            if isinstance(hog, dict) and hog.get("status") in {"ok", "stale"} and hog.get("price") is not None:
                change = hog.get("change7d")
                moved = (f"（{_signed(change)}，{_signed(hog.get('change7dPercent'))}%）"
                         if isinstance(change, int | float) and isinstance(hog.get("change7dPercent"), int | float) else "")
                facts.append(_quality(_fact("D.hog_price", "fundamental", "生猪现货价（外三元）",
                                            f"{_num(hog['price'])} 元/公斤，7 日{moved}", str(hog.get("source") or "涌益咨询"),
                                            str(hog.get("date") or None)),
                                      hog.get("status"), hog.get("fetchedAt") if hog.get("stale") else None, hog.get("error")))
        return facts, ({"status": "ok", "source": "AKShare"} if facts else {"status": "unavailable", "error": "research_unavailable"})

    async def _slow_source(self, key: str, kind: str, fetch: Callable[[], Awaitable[Any]]) -> Kept:
        """The source's last good value (`stale` after a failed refresh); SourceError only when there is none.
        One fetch per key at a time, every reader waits on it (the shared async collector, collection.py)."""
        async def checked() -> Any:
            try:
                value = await fetch()
            except SourceError as error:
                raise SourceFailed(str(error)) from error
            except (RuntimeError, ValueError, KeyError, TypeError) as error:
                raise SourceFailed(f"{kind}_failed") from error
            if not value or (isinstance(value, str) and not value.strip()):
                # No slow source has a valid empty answer (#788): an empty note search is no proof the institutions
                # stopped writing, an empty page is no release — the last good value stays and reads stale.
                raise SourceFailed("research_notes_none_recent" if kind in {"notes", "orgfeed"} else f"{kind}_empty")
            return value
        try:
            return await self._slow_collect.get(
                key, checked, ttl=SLOW_TTL[kind], retry=SLOW_FAILURE_TTL_SECONDS,
                timeout=SOURCE_DEADLINE_SECONDS * SLOW_DEADLINE_FACTOR.get(kind, 1.0), timeout_error=f"{kind}_timeout",
                unavailable=f"{kind}_failed")
        except SourceFailed as failure:
            raise SourceError(failure.code) from failure

    async def _official_section(self, product: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        errors: list[str] = []
        releases: list[dict[str, str]] = []
        try:
            directory = await self._slow_source("nbs", "nbs", self.sources.nbs_releases)
            releases = directory.value
        except SourceError as error:
            errors.append(str(error))
        price_release = next((item for item in releases if NBS_PRICE_TITLE in item["title"]), None)
        row_name = NBS_PRICE_ROWS.get(product)
        if price_release is not None and row_name:
            try:
                page = await self._slow_source(f"nbs:{price_release['url']}", "nbs_page",
                                               lambda: self.sources.nbs_price_page(price_release["url"]))
                row = parse_nbs_price_row(page.value, row_name)
                if row is not None:
                    period = price_release["title"].split(NBS_PRICE_TITLE)[0]
                    facts.append(_kept_quality(directory, _kept_quality(page, _fact("O.nbs_price", "official", f"国家统计局 {period} {row['name']}",
                                       f"{_num(row['price'])} 元/{row['unit']}，较上期 {_signed(row['change'])}（{_signed(row['changePercent'])}%）",
                                       "国家统计局", price_release["date"], price_release["url"]))))
            except SourceError as error:
                errors.append(str(error))
        market = CFTC_MARKETS.get(product)
        if market is not None:
            try:
                kept = await self._slow_source(f"cftc:{market[0]}", "cftc", lambda: self.sources.cftc(market[0]))
                cftc = kept.value
                facts.append(_kept_quality(kept, _fact("O.cftc", "official", f"CFTC {market[1]} 非商业净多",
                                   f"{_signed(cftc['netLong'], 0)} 手，周变化 {_signed(cftc['netChange'], 0)}", "CFTC", cftc["date"])))
            except SourceError as error:
                errors.append(str(error))
        if product in SHFE_WEEKLY_PRODUCTS:
            try:
                kept = await self._slow_source("shfe_weekly", "shfe_weekly", lambda: self.sources.shfe_weekly(self.now()))
                friday, sections = kept.value
                row = shfe_weekly_row(sections, product)
                if row is not None:
                    facts.append(_kept_quality(kept, _fact("O.shfe_stock", "official", f"上期所仓库库存（周报 {friday[5:]}）",
                                       f"{_num(row['stock'], 0)} {row['unit']}，较上周 {_signed(row['change'], 0)}",
                                       "上海期货交易所", friday)))
            except SourceError as error:
                errors.append(str(error))
        if product in EIA_PRODUCTS:
            try:
                kept = await self._slow_source("eia:crude", "eia", self.sources.eia_crude_stocks)
                stocks = kept.value
                facts.append(_kept_quality(kept, _with(_fact("O.eia_crude", "official", "EIA 美国商业原油库存",
                                         f"{_num(stocks['value'], 0)} 千桶，周变化 {_signed(stocks['change'], 0)}", "EIA", stocks["date"]),
                                   value=stocks["value"], change=stocks["change"])))
            except SourceError as error:
                if str(error) != "eia_key_missing":
                    errors.append(str(error))
        state: dict[str, Any] = {"status": "ok" if facts else "unavailable"}
        if errors:
            state["error"] = errors[0]
        return facts, state

    async def _macro_section(self, product: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """China's monthly macro figures, the statistics bureau's latest releases and FRED's dollar and yields."""
        facts: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            kept = await self._slow_source("macro", "macro", self.sources.macro)
            facts.extend(_kept_quality(kept, fact) for fact in macro_facts(kept.value, product))
        except SourceError as error:
            errors.append(str(error))
        try:
            kept = await self._slow_source("nbs", "nbs", self.sources.nbs_releases)
            today = self.now().astimezone(CHINA).date()
            headlines = [item for item in kept.value if "%" in item["title"] and NBS_PRICE_TITLE not in item["title"]
                         and (today - datetime.fromisoformat(item["date"]).date()).days <= 20][:2]
            facts.extend(_kept_quality(kept, _fact(f"O.nbs_{hashlib.sha1(item['url'].encode()).hexdigest()[:6]}", "macro",
                                                   "国家统计局发布", item["title"], "国家统计局", item["date"], item["url"]))
                         for item in headlines)
        except SourceError as error:
            errors.append(str(error))
        for series, label, unit in FRED_SERIES:
            try:
                kept = await self._slow_source(f"fred:{series}", "fred", partial(self.sources.fred, series))
                value = kept.value
                facts.append(_kept_quality(kept, _with(_fact(f"O.fred_{series}", "macro", label,
                                         f"{_num(value['value'])}{unit}，较前值 {_signed(value['change'])}", "FRED", value["date"]),
                                   value=value["value"], change=value["change"])))
            except SourceError as error:
                if str(error) != "fred_key_missing":
                    errors.append(str(error))
        if self.extra is not None:
            facts.extend(await self._live_macro_facts(product))
        facts = [fact for fact in facts if macro_relevant(fact["id"], product)]
        state: dict[str, Any] = {"status": "ok" if facts else "unavailable"}
        if errors:
            state["error"] = errors[0]
        return facts, state

    async def _live_macro_facts(self, product: str) -> list[dict[str, Any]]:
        """The economic calendar and the rates block, each only as far as they concern the product's
        category. Both read the live service's shared caches; a miss adds nothing and never blocks."""
        assert self.extra is not None
        facts: list[dict[str, Any]] = []
        calendar = await self._deadline(asyncio.to_thread(self.extra.calendar_feed, CALENDAR_DAYS))
        if isinstance(calendar, dict) and calendar.get("status") in {"ok", "stale"}:
            upcoming = [event for event in calendar.get("items") or []
                        if event.get("datetime", "9999") > self.now().astimezone(CHINA).isoformat(timespec="minutes")
                        and calendar_related(event, product)][:3]
            if upcoming:
                parts = []
                for event in upcoming:
                    weekday = "周" + "一二三四五六日"[datetime.fromisoformat(event["datetime"]).weekday()]
                    details = "，".join(f"{name} {_num(event.get(key))}"
                                       for name, key in (("预期", "forecast"), ("前值", "previous")) if event.get(key) is not None)
                    parts.append(f"{event['date'][5:]} {weekday} {event['time']} {event['region']} {event['event']}"
                                 + (f"（{details}）" if details else ""))
                facts.append(_quality(_fact("E.calendar", "macro", "未来重要经济事件", "；".join(parts),
                                            str(calendar.get("source") or "百度财经经济日历"), str(calendar.get("asOf") or None)),
                                      calendar.get("status"), error=calendar.get("error")))
        rates = await self._deadline(asyncio.to_thread(self.extra.rates))
        wanted = RATES_BY_CATEGORY.get(CALENDAR_CATEGORY.get(product, ""))  # None: every rate
        if isinstance(rates, dict) and rates.get("status") in {"ok", "stale"}:
            def rate(name: str, key: str) -> str:
                entry = rates.get(key)
                if wanted is not None and key not in wanted:
                    return ""
                return f"{name} {_num(entry.get('value'))}%（{str(entry.get('date') or '')[5:]}）" if isinstance(entry, dict) else ""
            bonds = "，".join(filter(None, (rate("中国10年期国债", "china10Y"), rate("美国10年期国债", "us10Y"),
                                           rate("美债10Y-2Y利差", "us10Y2Y"))))
            shibor = "，".join(filter(None, (rate("SHIBOR隔夜", "shiborOvernight"), rate("SHIBOR1周", "shibor1Week"))))
            lpr = "，".join(filter(None, (rate("LPR 1年", "lpr1Y"), rate("LPR 5年", "lpr5Y"))))
            parts = [part for part in (bonds, shibor, lpr) if part]
            if parts:
                dates = [str(entry.get("date")) for key in ("china10Y", "us10Y", "us10Y2Y", "shiborOvernight", "shibor1Week")
                         if (wanted is None or key in wanted) and isinstance((entry := rates.get(key)), dict) and entry.get("date")]
                facts.append(_quality(_with(_fact("E.rates", "macro", "国债与资金利率", "；".join(parts),
                                                  str(rates.get("source") or "中美国债收益率与SHIBOR"), max(dates) if dates else None),
                                            us10Y=rates.get("us10Y"), china10Y=rates.get("china10Y")),
                                      # A partial read (one source failed, its series carried over) reads stale too.
                                      "stale" if rates.get("partial") else rates.get("status"),
                                      rates.get("fetchedAt") if rates.get("stale") or rates.get("partial") else None,
                                      rates.get("error")))
        return facts

    async def _flash_facts(self, product: str) -> list[dict[str, Any]]:
        """快讯 facts for the judgment: this product's recent items plus the macro ones the model kept for
        it, read from the shared flash table the background poller fills — the reader never touches the
        sources. Any failure simply adds none."""
        assert self.extra is not None
        feed = await self._deadline(asyncio.to_thread(self.extra.flash_feed, product, None, 24, 1))
        if isinstance(feed, BaseException) or feed.get("status") != "ok":
            return []
        picks = await self._deadline(asyncio.to_thread(self.extra.flash_pick, product))
        if isinstance(picks, BaseException):
            return []
        # Items the model has not judged yet were just queued: give it a bounded moment so a
        # dropped item (苹果公司 for 苹果) never becomes a fact, then pick again.
        settled = await self._deadline(self.extra.settle_screen(product, SOURCE_DEADLINE_SECONDS * 0.75))
        if not isinstance(settled, BaseException):
            again = await self._deadline(asyncio.to_thread(self.extra.flash_pick, product))
            picks = picks if isinstance(again, BaseException) else again
        facts = []
        for item in picks:
            moment = datetime.fromisoformat(item["publishedAt"])
            digest = hashlib.sha1(item["id"].encode()).hexdigest()[:8]
            facts.append(_fact(f"N.{digest}", "flash", "快讯",
                               f"{moment.strftime('%H:%M')} {'／'.join(item['sources'])}：{item['text'][:80]}",
                               FLASH_SOURCE_LABEL, item["publishedAt"], item.get("link"), item["text"][:400]))
        return facts

    async def _major_events(self, key: str, product: str) -> None:
        """Remember when the product's 重要 events of the research day were published (for `stale`)."""
        if self.extra is None:
            return
        result = await self._deadline(asyncio.to_thread(self.extra.product_events, product))
        if isinstance(result, tuple):
            self._majors[key] = [str(event["publishedAt"]) for event in result[1]
                                 if event.get("tier") == "major" and event.get("publishedAt")]

    def _news_line(self, product: str) -> dict[str, Any] | None:
        """The 新闻面 line: today's news brief's net direction and sentence (news_analyst), or None."""
        analyst = getattr(self.extra, "analyst", None) if self.extra is not None else None
        if analyst is None:
            return None
        try:
            brief = analyst.brief(product)
        except (sqlite3.Error, ValueError, KeyError):
            return None
        if brief.get("status") not in {"ready", "stale"} or not brief.get("take"):
            return None
        return {"net": brief.get("net"), "take": brief["take"], "generatedAt": brief.get("generatedAt")}

    async def _research_section(self, name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Futures companies' notes on the product, one note per institution: the news search's (the passage
        about it, per product) and the futures columns' (机构观点 / 期货日报, one shared read for every product)."""
        if not name:
            return [], {"status": "unavailable"}
        searched, columns = await asyncio.gather(
            self._slow_source(f"notes:{name}", "notes", lambda: self.sources.org_notes(name, self.now())),
            self._slow_source("orgfeed", "orgfeed", lambda: self.sources.org_feed(self.now())), return_exceptions=True)
        for result in (searched, columns):
            if isinstance(result, BaseException) and not isinstance(result, SourceError):
                raise result
        if isinstance(searched, SourceError) and isinstance(columns, SourceError):
            return [], {"status": "unavailable", "error": str(searched)}
        aliases = product_aliases(name)
        search = searched.value if isinstance(searched, Kept) else []
        column = feed_notes(columns.value, aliases, self.now()) if isinstance(columns, Kept) else []
        chosen = merge_notes(search, column)
        facts: list[dict[str, Any]] = []
        for kept, group in ((searched, search), (columns, column)):
            mine = [note for note in chosen if any(note is own for own in group)]
            if isinstance(kept, Kept) and mine:
                facts += [_kept_quality(kept, fact) for fact in research_facts(mine, aliases)]
        # merge_notes' newest-first order across both fetch paths; the quoted stock figures after the notes.
        facts.sort(key=lambda fact: fact["asOf"] or "", reverse=True)
        facts.sort(key=lambda fact: fact["id"].startswith("V.inv"))
        return facts, ({"status": "ok", "source": "东方财富（期货公司早评/日报、期货机构观点栏目、期货日报）"} if facts
                       else {"status": "unavailable", "error": "research_notes_none_recent"})

    async def _report_section(self, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not symbol:
            return [], {"status": "unavailable"}
        news = await self._deadline(self.news(symbol))
        if isinstance(news, BaseException):
            return [], {"status": "unavailable", "error": "research_news_unavailable"}
        facts = report_facts(news)
        state: dict[str, Any] = {"status": news.get("status") or "ok", "source": news.get("source")}
        if news.get("error"):
            state["error"] = news["error"]
        return facts, state

    async def _loaded(self, key: str, exchange: str, product: str) -> None:
        if self._closed:
            return  # a callback armed before close() lands after it: no fresh collection starts
        self._facts.pop(key, None)
        try:
            facts = await self._collect(key, exchange, product)
        except (ValueError, RuntimeError) as error:
            log.warning("research_desk_reload_failed product=%s error=%s", key, error)
            return
        self._resume_deferred(key, facts)

    # -- judgment

    @staticmethod
    def research_day(moment: datetime) -> str:
        local = moment.astimezone(CHINA)
        day = local.date() if local.time() >= RESEARCH_DAY_START else local.date() - timedelta(days=1)
        return day.isoformat()

    def view_at(self, symbol: str, moment: datetime) -> dict[str, Any] | None:
        """What the desk said about this product when `moment` happened (交易日志's 当时的调研判断): the
        latest judgment made before it on the same research day, from the history. None when there was none."""
        if self._db is None or self._db_lock is None:
            return None
        try:
            exchange, product, _ = research_target(symbol)
        except ValueError:
            return None
        with self._db_lock:
            rows = self._db.execute("SELECT generated_at, payload_json FROM research_desk_judgment_history"
                                    " WHERE product_key = ? AND research_day = ?",
                                    (f"{exchange}.{product.lower()}", self.research_day(moment))).fetchall()
        made = [(datetime.fromisoformat(generated), payload) for generated, payload in rows]
        before = sorted(((at, payload) for at, payload in made if at <= moment), key=lambda item: item[0])
        if not before:
            return None
        entry = json.loads(before[-1][1])
        value = entry.get("value") or {}
        # confidenceBasis: absent (None) on judgments stored before 2026-10-12, whose confidence is the model's own.
        return {"stance": value.get("stance"), "confidence": value.get("confidence"), "confidenceBasis": value.get("confidenceBasis"),
                "headline": value.get("headline"), "generatedAt": entry.get("generatedAt"), "researchDay": entry.get("researchDay")}

    def price_line(self, product: str) -> str | None:
        """The latest cached price fact of a product ("主力 au2612 最新价 960.5，日涨跌 -1.3%"), for the news
        analyst; None when the desk has not read the product (no fetch is started here)."""
        for key, (_, _, facts) in list(self._facts.items()):
            if key.rsplit(".", 1)[-1].lower() != product.lower():
                continue
            price = next((fact for fact in facts.get("facts") or [] if fact.get("id") == "M.price"), None)
            if price:
                return f"{price['label']} {price['value']}"
        return None

    @staticmethod
    def _day_change(facts: dict[str, Any]) -> float:
        price = next((fact for fact in facts["facts"] if fact["id"] == "M.price"), None)
        match = re.search(r"日涨跌 ([+-]?\d+(?:\.\d+)?)%", price["value"]) if price else None
        return float(match.group(1)) if match else 0.0

    @staticmethod
    def _limit_distance(facts: dict[str, Any]) -> float | None:
        limit = next((fact for fact in facts["facts"] if fact["id"] == "M.limit"), None)
        match = re.search(r"距最近停板 ([\d.]+)%", limit["value"]) if limit else None
        return float(match.group(1)) if match else None

    @staticmethod
    def _price(facts: dict[str, Any]) -> float | None:
        price = next((fact for fact in facts["facts"] if fact["id"] == "M.price"), None)
        match = re.match(r"([\d.]+)", price["value"]) if price else None
        return float(match.group(1)) if match else None

    def _shown(self, key: str, facts: dict[str, Any]) -> dict[str, Any] | None:
        current = self._current(key)
        if current is None:
            return None
        generated = datetime.fromisoformat(current["generatedAt"])
        shown = {"status": "ready", **current["value"], "generatedAt": current["generatedAt"], "model": current["model"],
                 "researchDay": current["researchDay"], "today": current["researchDay"] == self.research_day(self.now()),
                 "manualAvailableAt": (generated + timedelta(seconds=MANUAL_MIN_SECONDS)).isoformat(timespec="seconds")}
        price, then = self._price(facts), current.get("price")
        if price and then:
            shown["movedSincePercent"] = round((price / then - 1) * 100, 2)
        # A cited id that now reads differently (same id, refreshed value): its snapshot stays in basis.
        now_by_id = {fact["id"]: fact for fact in facts["facts"]}
        shown["changedBasis"] = [item for item, cited in (current["value"].get("basis") or {}).items()
                                 if item in now_by_id and any(now_by_id[item].get(key) != cited.get(key) for key in ("label", "value"))]
        failure = self._failures.get(key)
        if failure is not None:
            # The last re-run failed: the earlier judgment stays, the failure and retry time beside it.
            shown["refreshError"], shown["retryAt"] = failure[1], failure[2]
        skipped = self._skipped.get(key)
        if skipped is not None:
            shown["rejudgeSkipped"] = skipped
        # 今日判断自动更新: the research day's runs (newest first) and when the next automatic one may come.
        shown["updates"] = self._day_updates(key, current["researchDay"])
        local, night = self.now().astimezone(CHINA), None
        if facts.get("hasNight") and local.weekday() < 5:
            at = datetime.combine(local.date(), NIGHT_REJUDGE_AT, tzinfo=CHINA)
            night = at.isoformat(timespec="seconds") if local < at and generated < at else None
        shown["nextAuto"] = {"nightAt": night, "news": int(current.get("newsRuns") or 0) < NEWS_REJUDGES_PER_DAY}
        return shown

    def _day_updates(self, key: str, research_day: str) -> list[dict[str, Any]]:
        if self._db is None or self._db_lock is None:
            return []
        with self._db_lock:
            rows = self._db.execute("SELECT payload_json FROM research_desk_judgment_history WHERE product_key = ?"
                                    " AND research_day = ? ORDER BY generated_at DESC LIMIT 8", (key, research_day)).fetchall()
        updates = []
        for (payload,) in rows:
            try:
                entry = json.loads(payload)
            except ValueError:
                continue
            value = entry.get("value") or {}
            updates.append({"generatedAt": entry.get("generatedAt"), "stance": value.get("stance"),
                            "reason": value.get("reason"), **({"trigger": value["trigger"]} if value.get("trigger") else {})})
        return updates

    def _need(self, key: str, facts: dict[str, Any], force: bool, news: dict[str, Any] | None = None) -> str | None:
        """Why a new judgment is due now, or None."""
        current = self._current(key)
        now = self.now()
        if current is None or current["researchDay"] != self.research_day(now):
            return "daily"
        day = self._day_change(facts)
        limit = self._limit_distance(facts)
        swung = abs(day) >= SWAN_DAY_CHANGE_POINTS and abs(day - current.get("dayChange", 0.0)) >= SWAN_MOVE_POINTS
        at_limit = limit is not None and limit <= NEAR_LIMIT_PERCENT and (current.get("limitDistance") or 100.0) > NEAR_LIMIT_PERCENT
        if (swung or at_limit) and not current.get("swan"):
            return "swan"
        generated = datetime.fromisoformat(current["generatedAt"])
        local = now.astimezone(CHINA)
        night = datetime.combine(local.date(), NIGHT_REJUDGE_AT, tzinfo=CHINA)
        if facts.get("hasNight") and local.weekday() < 5 and local >= night and generated < night:
            return "night"
        if ((now - generated).total_seconds() >= NEWS_REJUDGE_MIN_SECONDS
                and int(current.get("newsRuns") or 0) < NEWS_REJUDGES_PER_DAY and self._stale(key, current, news)):
            return "news"
        if force and (now - generated).total_seconds() >= MANUAL_MIN_SECONDS:
            return "manual"
        return None

    def _waiting(self, key: str, shown: dict[str, Any] | None) -> dict[str, Any]:
        stage, started = self._started.get(key, ("model", self.now().isoformat(timespec="seconds")))
        expected = (EXPECTED_FACTS_SECONDS + self._expected_model()) if stage == "facts" else self._expected_model()
        progress = {"stage": stage, "startedAt": started, "expectedSeconds": expected}
        if not shown:
            return {"status": "pending", **progress}
        # A retry is running: the last failure and the last skipped receipt no longer describe it.
        earlier = {name: value for name, value in shown.items() if name not in ("refreshError", "retryAt", "rejudgeSkipped")}
        return {**earlier, "refreshing": True, **progress}

    def _expected_model(self) -> int:
        if not self._durations:
            return EXPECTED_MODEL_SECONDS
        ordered = sorted(self._durations)
        return int(round(ordered[len(ordered) // 2]))

    def _judgment(self, key: str, facts: dict[str, Any], generator: Any, force: bool,
                  news: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
        """The judgment to show and what happened to a request for a new one: started, running,
        current (none was due), limited (a manual re-run inside the hour), disabled, insufficient,
        backoff (the last run failed) or budget (the day's cap is used up)."""
        shown = self._shown(key, facts)
        if key in self._judging:
            return self._waiting(key, shown), "running"
        reason = self._need(key, facts, force, news)
        if reason is None:
            return shown, ("limited" if force else "current")  # type: ignore[return-value]  # None only with a judgment
        if generator is None or not hasattr(generator, "complete_structured") or not getattr(generator, "agent_authenticated", False):
            return shown or {"status": "disabled"}, "disabled"
        if any(item["status"] == "loading" for item in facts["sections"]):
            # Judge the full set: this starts as soon as the loading section lands.
            self._deferred[key] = (generator, reason)
            self._skipped.pop(key, None)
            return self._waiting(key, shown), "started"
        sections = {item["key"] for item in facts["sections"] if item["count"]}
        # 筛过的快讯是判断的一类依据（第七批起不单列成块）：外盘没有国内对照的品种只有行情与报道两块，
        # 不算快讯就永远出不了判断（2026-10-21 迷你纳指“资料不足”）。
        if any(fact.get("section") == "flash" for fact in facts.get("facts") or []):
            sections.add("flash")
        if "market" not in sections or len(sections) < MIN_SECTIONS_FOR_JUDGMENT:
            return shown or {"status": "insufficient"}, "insufficient"
        failure = self._failures.get(key)
        if failure is not None and failure[0] > self.clock():
            return shown or {"status": "failed", "error": failure[1], "retryAt": failure[2]}, "backoff"
        today = self.now().astimezone(CHINA).date().isoformat()
        if not self._budget.take(today, self.daily_calls):
            return shown or {"status": "budget_exhausted"}, "budget"
        self._started[key] = ("model", self.now().isoformat(timespec="seconds"))
        self._skipped.pop(key, None)
        trigger = self._trigger(key, news) if reason == "news" else None
        task = asyncio.create_task(self._generate(key, facts, generator, reason, trigger))
        self._judging[key] = task

        def finished(_: asyncio.Task[None]) -> None:
            self._judging.pop(key, None)
            self._started.pop(key, None)
        task.add_done_callback(finished)
        return self._waiting(key, shown), "started"

    def _rejudge(self, outcome: str, judgment: dict[str, Any]) -> dict[str, Any]:
        """The receipt of 重新判断 (the manual refresh): what happened and, when it could not run, why
        and from when it can — the same limits as before (one manual re-run an hour, the day's cap)."""
        def clock(value: Any) -> str:
            try:
                return datetime.fromisoformat(str(value)).astimezone(CHINA).strftime("%H:%M")
            except ValueError:
                return ""
        if outcome == "started":
            return {"status": "started", "message": "已按最新消息重新判断，完成后在这里更新"}
        if outcome == "running":
            return {"status": "running", "message": "正在重新判断，完成后在这里更新"}
        if outcome == "limited":
            at = judgment.get("manualAvailableAt")
            return {"status": "limited", "availableAt": at,
                    "message": f"1 小时内已重判过，{clock(at)} 后可再判" if clock(at) else "1 小时内已重判过，稍后可再判"}
        if outcome == "backoff":
            at = judgment.get("retryAt")
            return {"status": "limited", "availableAt": at,
                    "message": f"上次判断没能完成，{clock(at)} 后可再判" if clock(at) else "上次判断没能完成，几分钟后可再判"}
        if outcome == "budget":
            return {"status": "limited", "message": "今天的判断次数已用完，明天 08:30 后再判"}
        if outcome == "disabled":
            return {"status": "unavailable", "message": "当前档位未开通「联网研究」，不能重新判断"}
        if outcome == "insufficient":
            return {"status": "unavailable", "message": "行情缺失或事实不足三类，不能重新判断"}
        return {"status": "current", "message": "判断已是最新"}

    def _annotated(self, key: str, shown: dict[str, Any], news: dict[str, Any] | None) -> dict[str, Any]:
        """A ready judgment with 第七批's reading aids: each point and risk's direction (inferred for an
        old one), `change` against the previous research day, and `stale` when news since says it may be out of date."""
        out = with_directions(shown)
        current = self._current(key)
        if current is None:
            return out
        change = self._change(key, current)
        if change is not None:
            out["change"] = change
        stale = self._stale(key, current, news)
        if stale is not None:
            out["stale"] = stale
        return out

    def _change(self, key: str, current: dict[str, Any]) -> dict[str, Any] | None:
        """The previous research day's last judgment of the product, against this one (None without one)."""
        cached = self._changes.get(key)
        if cached is not None and cached[0] == current["generatedAt"]:
            return cached[1]
        change: dict[str, Any] | None = None
        if self._db is not None and self._db_lock is not None:
            with self._db_lock:
                row = self._db.execute("SELECT research_day, payload_json FROM research_desk_judgment_history"
                                       " WHERE product_key = ? AND research_day < ? ORDER BY generated_at DESC LIMIT 1",
                                       (key, current["researchDay"])).fetchone()
            if row is not None:
                try:
                    before = str((json.loads(row[1]).get("value") or {}).get("stance") or "")
                except ValueError:
                    before = ""
                stance = str(current["value"].get("stance") or "")
                if before and stance:
                    day = str(row[0])[5:]
                    now = "今天" if current["researchDay"] == self.research_day(self.now()) else "这次"
                    change = {"previousDay": row[0], "previousStance": before, "same": before == stance,
                              "text": f"与上次（{day}）相同" if before == stance else f"上次（{day}）{before} → {now}转{stance}"}
        self._changes[key] = (current["generatedAt"], change)
        return change

    def _stale(self, key: str, current: dict[str, Any], news: dict[str, Any] | None) -> dict[str, Any] | None:
        """判断可能过时: STALE_MAJOR_EVENTS or more 重要 news events since the judgment, or a news brief
        written since it whose net direction (偏多 / 偏空) differs from the stance (震荡 included). Since
        2026-10-24 it also re-runs the judgment by itself (`_need` reason "news", NEWS_REJUDGES_PER_DAY a day)."""
        generated = datetime.fromisoformat(current["generatedAt"])

        def after(value: Any) -> bool:
            try:
                moment = datetime.fromisoformat(str(value))
            except ValueError:
                return False
            return (moment if moment.tzinfo else moment.replace(tzinfo=CHINA)) > generated
        count = sum(1 for moment in self._majors.get(key, []) if after(moment))
        net = (news or {}).get("net")
        stance = current["value"].get("stance")
        differs = bool(news and after(news.get("generatedAt")) and net in OPPOSITE and net != stance)
        if count >= STALE_MAJOR_EVENTS or differs:
            return {"count": count, "newsNet": net}
        return None

    def _trigger(self, key: str, news: dict[str, Any] | None) -> str | None:
        """Why the news re-run happened, in one line for the card (今日判断自动更新)."""
        current = self._current(key)
        stale = self._stale(key, current, news) if current is not None else None
        if stale is None:
            return None
        if stale.get("newsNet") and news and news.get("take"):
            return f"新闻面转{stale['newsNet']}：{news['take']}"
        return f"判断后又出了 {stale['count']} 条重要新闻"

    def _adopted_since(self, key: str, moment: datetime) -> bool:
        """A debate conclusion was written into the product's judgment at or after `moment` (the shared table
        is read, so another runtime slot's write counts too)."""
        current = self._current(key)
        if not current or (current.get("value") or {}).get("reason") != "debate":
            return False
        try:
            return datetime.fromisoformat(str(current.get("generatedAt"))) >= moment.replace(microsecond=0)
        except ValueError:
            return False

    async def _generate(self, key: str, facts: dict[str, Any], generator: Any, reason: str, trigger: str | None = None) -> None:
        if self._closed:
            return  # a judgment armed before close() writes nothing after it
        begun = self.now()
        payload = {"product": facts["productName"] or facts["product"], "mainSymbol": facts["mainSymbol"],
                   "now": self.now().astimezone(CHINA).isoformat(timespec="minutes"),
                   "facts": [{name: fact[name] for name in ("id", "section", "kind", "label", "value", "detail", "title", "source", "asOf",
                                                            *QUALITY_KEYS)
                              if fact.get(name)} for fact in facts["facts"]]}
        endpoints = getattr(generator, "endpoints", None)
        model = self.model if self.model and (not isinstance(endpoints, dict) or self.model in endpoints) else None
        started = self.clock()
        try:
            async with self._limit:
                result = await generator.complete_structured(
                    instructions=DESK_INSTRUCTIONS, input_text=json.dumps(payload, ensure_ascii=False),
                    schema=DeskJudgment, timeout_seconds=JUDGMENT_TIMEOUT_SECONDS, model=model,
                    usage_user=PLATFORM_USAGE_USER)
            dropped: dict[str, int] = {}
            try:
                value = validate_judgment(result.value, facts["facts"], dropped)
            finally:
                if dropped:
                    log.info("research_desk_statements_dropped product=%s reasons=%s", key, dropped)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — any model failure only leaves the facts shown.
            code = str(error) if isinstance(error, RuntimeError | ValueError) and str(error) else type(error).__name__
            log.warning("research_desk_judgment_failed product=%s error=%s", key, code)
            retry_at = (self.now() + timedelta(seconds=JUDGMENT_FAILURE_BACKOFF_SECONDS)).isoformat(timespec="seconds")
            self._failures[key] = (self.clock() + JUDGMENT_FAILURE_BACKOFF_SECONDS, code, retry_at)
            return
        self._failures.pop(key, None)
        tokens, seconds = int(getattr(result, "tokens", 0) or 0), round(self.clock() - started, 1)
        self._durations.append(seconds)
        log.info("research_desk_judgment product=%s reason=%s tokens=%d seconds=%.1f", key, reason, tokens, seconds)
        now = self.now()
        previous = self._current(key)
        same_day = bool(previous and previous.get("researchDay") == self.research_day(now))
        entry = {"value": {**value, "tokens": tokens, "seconds": seconds, "reason": reason,
                           **({"trigger": trigger} if trigger else {})},
                 "generatedAt": now.isoformat(timespec="seconds"), "researchDay": self.research_day(now),
                 "model": model or getattr(generator, "model", None) or "default", "symbol": facts.get("mainSymbol"),
                 "name": facts.get("productName") or facts.get("product"),
                 "dayChange": self._day_change(facts), "price": self._price(facts),
                 "limitDistance": self._limit_distance(facts),
                 # One black-swan re-run per day: remembered through that day's later manual runs too.
                 "swan": reason == "swan" or bool(previous and previous.get("swan")
                                                    and previous.get("researchDay") == self.research_day(now)),
                 # 今日判断自动更新: the day's news-triggered runs count against NEWS_REJUDGES_PER_DAY.
                 "newsRuns": (int(previous.get("newsRuns") or 0) if same_day and previous else 0) + (reason == "news")}
        if not self._store(key, entry, unless_adopted_since=begun):
            # 写进今日判断 landed while this model call ran (here or in another slot): the user's choice stands.
            log.info("research_desk_judgment_superseded_by_debate product=%s", key)

    def _store(self, key: str, entry: dict[str, Any], *, unless_adopted_since: datetime | None = None) -> bool:
        """The product's judgment of the day: in memory, the latest-per-product table and the history.
        `unless_adopted_since` (a generated judgment): written only if no debate conclusion was adopted at or
        after that moment — checked in the same upsert statement, so another slot's adoption cannot slip between
        the check and the write. False when it was not written."""
        if unless_adopted_since is not None and self._adopted_since(key, unless_adopted_since):
            return False
        if self._db is not None and self._db_lock is not None:
            since = unless_adopted_since.replace(microsecond=0).isoformat(timespec="seconds") if unless_adopted_since else None
            with self._db_lock, self._db:
                written = self._db.execute(
                    "INSERT INTO research_desk_judgments (product_key, payload_json) VALUES (?, ?)"
                    " ON CONFLICT(product_key) DO UPDATE SET payload_json = excluded.payload_json"
                    " WHERE ? IS NULL OR NOT (json_extract(research_desk_judgments.payload_json, '$.value.reason') = 'debate'"
                    " AND json_extract(research_desk_judgments.payload_json, '$.generatedAt') >= ?)",
                    (key, json.dumps(entry, ensure_ascii=False), since, since)).rowcount
                if not written:
                    return False
                self._db.execute("INSERT OR IGNORE INTO research_desk_judgment_history"
                                 " (product_key, generated_at, research_day, payload_json) VALUES (?, ?, ?, ?)",
                                 (key, entry["generatedAt"], entry["researchDay"], json.dumps(entry, ensure_ascii=False)))
        self._judgments[key] = entry
        return True

    # -- 多 AI 研讨 (2026-10-11): the debate reads the desk's facts and writes its conclusion back here

    async def debate_material(self, symbol: str, *, settle_seconds: float = DEBATE_SETTLE_SECONDS) -> dict[str, Any]:
        """The facts a debate works from: the report's facts (waiting a bounded moment for a loading
        section), plus `K.levels` — the 20-day closing high, low and average, computed here from the
        same daily history — and `K.history` (the last 30 closes, for the 技术面分析师), the shown judgment (the single-model 今日判断 the debate is compared with)
        and the main contract's price. Never starts a judgment."""
        report = await self.report(symbol)
        waited = 0.0
        while any(item["status"] == "loading" for item in report["sections"]) and waited < settle_seconds:
            await asyncio.sleep(1.0)
            waited += 1.0
            report = await self.report(symbol)
        facts = [dict(fact) for fact in report["facts"]]
        main = str(report.get("mainSymbol") or "")
        # The research day is the request's (it began before or after 08:30); the quote is judged at the moment
        # it was read — a fresh quote read after a slow catalog or workbench call is not "from the future" (#749).
        now = self.now()
        entry: dict[str, Any] = {"price": None, "source": None, "day": None, "reason": "market_unavailable"}
        foreign = report.get("exchange") == FOREIGN_RESEARCH
        if foreign:
            # 外盘（2026-10-21）：发起价取外盘报价页同一份（MT5 等）的最新价；20 日高低位按国内日历取收盘，不出。
            # 对账不打分（research_debate.FOREIGN_SETTLEMENT_VOID），这个价只记录发起时的行情。
            row = None
            if self.extra is not None:
                markets = await self._deadline(asyncio.to_thread(self.extra.foreign_markets, None))
                rows = markets.get("items") or [] if isinstance(markets, dict) else []
                row = next((item for item in rows if item.get("code") == report["product"]), None)
            if row is not None and isinstance(row.get("price"), int | float) and not row.get("stale"):
                entry = {"price": row["price"], "source": "quote", "day": None, "reason": None}
        if main and not foreign:
            try:  # the main contract's trading schedule: whether now is inside a session (the 发起价's rule)
                schedule = next((item.get("tradingSchedule") for item in await self._catalog_items()
                                 if str(item.get("symbol") or "").upper() == main.upper()), None)
            except (RuntimeError, ValueError, KeyError, TypeError, AttributeError):
                schedule = None
            try:
                workbench = await self._workbench(main, with_quote=True)
            except (RuntimeError, ValueError, KeyError, TypeError):
                workbench = None
            if workbench is not None:
                read_at = self.now()
                level = levels_fact(workbench, main, read_at, self.trading_days())
                if level is not None:
                    facts.append(level)
                history = history_fact(workbench, read_at, self.trading_days())
                if history is not None:
                    facts.append(history)
                # The quote's own price (a 国债 103.125 keeps its third decimal) only while current; never the
                # price fact, which may be a stale quote.
                entry = debate_entry(workbench["quote"], workbench["bars"], read_at, self.trading_days(), schedule)
        return {"symbol": report.get("symbol") or symbol, "mainSymbol": main, "exchange": report["exchange"],
                "product": report["product"], "productName": report.get("productName") or "", "facts": facts,
                "sections": report["sections"], "judgment": report.get("judgment") or {},
                "price": entry["price"], "entry": entry, "researchDay": self.research_day(now)}

    def trading_days(self) -> list[str]:
        """The exchange calendar's trading days (ISO, sorted); empty when unknown (weekdays stand in)."""
        calendar = getattr(getattr(self.research, "sources", None), "trading_days", None)
        try:
            days = calendar() if callable(calendar) else frozenset()
        except Exception:  # noqa: BLE001 — no calendar: weekdays stand in
            days = frozenset()
        return sorted(f"{day[:4]}-{day[4:6]}-{day[6:]}" for day in days if isinstance(day, str) and len(day) == 8)

    async def main_symbol(self, product: str) -> str | None:
        """A product's main contract from the catalog (`rb` → SHFE.rb2701); None when the catalog has none."""
        try:
            found = self._main(await self._catalog_items(), product)
        except (RuntimeError, ValueError, KeyError, TypeError):
            return None
        return str(found["symbol"]) if found and found.get("symbol") else None

    def adopt(self, symbol: str, value: DeskJudgment, facts: list[dict[str, Any]], *, model: str, debate_id: str,
              name: str | None = None, extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        """写进今日判断: a judgment made elsewhere (多 AI 研讨's conclusion) becomes the product's judgment of
        the day — through `validate_judgment` against the facts it was made from, then the same store as a
        generated one (latest table and history). Once per debate: whether this debate was already written is
        decided in the same transaction as the write, so a retry, a crash between steps or another slot adopting
        at the same moment never adds a second history entry or puts an old debate back over a newer judgment.
        Returns (entry, repeated); a repeat answers the first write. ValueError("judgment_unanchored") when
        nothing survives."""
        exchange, product, _ = research_target(symbol)
        key = f"{exchange}.{product.lower()}"
        checked = validate_judgment(value, facts)
        now = self.now()
        previous = self._current(key)
        material = {"facts": facts}
        entry = {"value": {**checked, "tokens": 0, "seconds": 0.0, "reason": "debate", **(extra or {}), "debateId": debate_id},
                 "generatedAt": now.isoformat(timespec="seconds"), "researchDay": self.research_day(now),
                 "model": model, "symbol": symbol, "name": name or (previous or {}).get("name"),
                 "dayChange": self._day_change(material), "price": self._price(material),
                 "limitDistance": self._limit_distance(material),
                 "swan": bool(previous and previous.get("swan") and previous.get("researchDay") == self.research_day(now))}
        self._failures.pop(key, None)
        first = self._adopted(key, debate_id)
        if first is not None:
            return first, True
        if self._db is None or self._db_lock is None:
            self._judgments[key] = entry
            return entry, False
        payload = json.dumps(entry, ensure_ascii=False)
        with self._db_lock, self._db:
            # One write statement claims the debate (no history entry of it yet) and records it; the latest
            # judgment follows in the same transaction, so both land or neither does.
            claimed = self._db.execute(
                "INSERT OR IGNORE INTO research_desk_judgment_history (product_key, generated_at, research_day, payload_json)"
                " SELECT ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM research_desk_judgment_history"
                " WHERE product_key = ? AND json_extract(payload_json, '$.value.debateId') = ?)",
                (key, entry["generatedAt"], entry["researchDay"], payload, key, debate_id)).rowcount
            if claimed:
                self._db.execute("INSERT INTO research_desk_judgments (product_key, payload_json) VALUES (?, ?)"
                                 " ON CONFLICT(product_key) DO UPDATE SET payload_json = excluded.payload_json", (key, payload))
        if not claimed:
            first = self._adopted(key, debate_id)
            if first is not None:
                return first, True
            raise ValueError("judgment_write_conflict")  # another judgment holds this exact second; try again
        self._judgments[key] = entry
        return entry, False

    def _adopted(self, key: str, debate_id: str) -> dict[str, Any] | None:
        """The judgment this debate already became, if it was written (here or by another slot)."""
        if self._db is None or self._db_lock is None:
            current = self._judgments.get(key)
            return current if current and current["value"].get("debateId") == debate_id else None
        with self._db_lock:
            row = self._db.execute("SELECT payload_json FROM research_desk_judgment_history WHERE product_key = ?"
                                   " AND json_extract(payload_json, '$.value.debateId') = ? ORDER BY generated_at LIMIT 1",
                                   (key, debate_id)).fetchone()
        return json.loads(row[0]) if row else None

    # -- pre-generation

    def _note_seen(self, key: str, symbol: str, user_id: str) -> None:
        if self._db is None or self._db_lock is None or self.clock() - self._seen.get(key, -1e9) < 600:
            return
        self._seen[key] = self.clock()
        with self._db_lock, self._db:
            self._db.execute("INSERT INTO research_desk_watch (product_key, symbol, user_id, seen_at) VALUES (?, ?, ?, ?)"
                             " ON CONFLICT(product_key) DO UPDATE SET symbol = excluded.symbol, user_id = excluded.user_id,"
                             " seen_at = excluded.seen_at", (key, symbol, user_id, self.now().isoformat(timespec="seconds")))

    def recently_read(self) -> list[tuple[str, str, str]]:
        """The shared seven-day reading list for pre-generation and news collection."""
        if self._db is None or self._db_lock is None:
            return []
        since = (self.now() - timedelta(days=PREGENERATE_LOOKBACK_DAYS)).isoformat(timespec="seconds")
        with self._db_lock:
            rows = self._db.execute("SELECT product_key, symbol, user_id FROM research_desk_watch WHERE seen_at >= ?"
                                    " ORDER BY seen_at DESC LIMIT ?", (since, PREGENERATE_LIMIT)).fetchall()
        return [(str(key), str(symbol), str(user)) for key, symbol, user in rows]

    async def pregenerate(self) -> list[str]:
        """The day's judgments for the products read in the last week, before anyone opens them."""
        if self.generator_for is None:
            return []
        rows = self.recently_read()
        done: list[str] = []
        today = self.research_day(self.now())
        self._pregenerated.clear()  # an earlier round's calls have long finished
        for key, symbol, user_id in rows:
            current = self._current(key)
            if current is not None and current.get("researchDay") == today:
                done.append(key)  # already judged today (an earlier run, the other slot, a reader): no model call
                continue
            generator = self.generator_for(user_id)
            if generator is None:
                continue
            self._pregenerated.add(key)
            try:
                await self.report(symbol, generator=generator, user_id=user_id)
                for _ in range(90):  # a loading section defers the start; wait for both
                    if key not in self._judging and key not in self._deferred:
                        break
                    task = self._judging.get(key)
                    await (asyncio.wait({task}, timeout=2) if task else asyncio.sleep(1))
            except (ValueError, RuntimeError) as error:
                log.warning("research_desk_pregenerate_failed product=%s error=%s", key, error)
                continue
            done.append(key)
        log.info("research_desk_pregenerated products=%s", ",".join(done))
        return done

    def day_judgments(self) -> dict[str, dict[str, Any]]:
        """Each product's latest judgment as a line needs it: key → stance, name, research day (from the shared
        table, so judgments written by the other slot count)."""
        entries = dict(self._judgments)
        if self._db is not None and self._db_lock is not None:
            with self._db_lock:
                rows = self._db.execute("SELECT product_key, payload_json FROM research_desk_judgments").fetchall()
            for key, payload in rows:
                try:
                    entries[key] = json.loads(payload)
                except ValueError:
                    continue
        return {key: {"stance": (entry.get("value") or {}).get("stance"), "name": entry.get("name"),
                      "researchDay": entry.get("researchDay")} for key, entry in entries.items()}

    def _trading_day(self, day: datetime) -> bool:
        calendar = getattr(getattr(self.research, "sources", None), "trading_days", None)
        days = calendar() if callable(calendar) else frozenset()
        return day.strftime("%Y%m%d") in days if days else day.weekday() < 5

    async def _pregenerate_round(self, day: datetime) -> None:
        if not await asyncio.to_thread(self._trading_day, day):
            return
        try:
            await self.pregenerate()
        except Exception:  # noqa: BLE001 — a failed morning run must not end the loop.
            log.exception("research_desk_pregenerate_error")
        if self.on_pregenerated is not None:
            try:
                # The push claims once per user and research day, so a catch-up never sends it twice.
                await self.on_pregenerated(self.day_judgments(), self.research_day(self.now()))
            except Exception:  # noqa: BLE001
                log.exception("research_desk_pregenerated_hook_error")

    async def _pregenerate_loop(self) -> None:
        # Started (or taken over by a new lease holder) after 08:45 on the same day session: catch up now, so a
        # slot switch or a restart never skips the day. `pregenerate` only calls the model for products that
        # have no judgment of the research day yet.
        now = self.now().astimezone(CHINA)
        if PREGENERATE_AT <= now.time() < PREGENERATE_CATCHUP_UNTIL:
            log.info("research_desk_pregenerate_catchup at=%s", now.isoformat(timespec="minutes"))
            await self._pregenerate_round(now)
        while True:
            now = self.now().astimezone(CHINA)
            target = datetime.combine(now.date(), PREGENERATE_AT, tzinfo=CHINA)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await self._pregenerate_round(target)

    def start(self) -> None:
        """The 08:45 pre-generation loop: only the instance-lease holder runs it (wiring/producers.py)."""
        if self._loop is None or self._loop.done():
            self._loop = asyncio.create_task(self._pregenerate_loop())

    async def stop_loop(self) -> None:
        """Stop the pre-generation loop (the lease moved away) before the lease is released: its model
        calls run as their own tasks (and a deferred one starts when its facts land), so cancelling the
        loop alone would let this slot keep calling the model and writing while the new holder's
        catch-up generates the same products again. Readers' judgments keep running."""
        loop, self._loop = self._loop, None
        keys, self._pregenerated = self._pregenerated, set()
        calls = [task for key in keys if (task := self._judging.get(key)) is not None]
        for key in keys:
            self._deferred.pop(key, None)
        for task in [*([loop] if loop is not None else []), *calls]:
            task.cancel()
        await asyncio.gather(*([loop] if loop is not None else []), *calls, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True  # late done-callbacks (_loaded/_generate) stop at their fence
        tasks = [*self._facts_pending.values(), *self._slow_collect.pending(), *self._judging.values(),
                 *([self._loop] if self._loop else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
