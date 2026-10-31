"""多 AI 研讨 (owner-approved mechanism v2, 2026-10-11; conversational turns 2026-10-24): a bull and a bear model
argue one product from the research desk's facts, turn by turn, the program checks every sentence, and an
anonymous judge decides.

1. 整理资料 (no model): the desk's facts (`ResearchDeskService.debate_material`, ids, sources, dates, plus the
   program's 20-day levels) and the news event that started it, split into five 底稿 — 持仓, 基差与仓单, 期限结构,
   新闻, 技术面 — each marked 完整 / 部分 / 缺失 / 过期.
1b. 分析师 (owner 2026-10-24, after TauricResearch/TradingAgents' analyst team): four analysts — 技术面, 供需基本面,
   资金持仓, 新闻宏观 — write at the same time, each from its own area's facts. One whose area lacks an item or holds
   only an old value may search the web for those gaps only (SEARCHES_PER_ANALYST, SEARCHES_PER_DEBATE); every
   result it keeps becomes a fact (W.…, with source, date and link), so the same number check holds. Each report
   (conclusion, causal reasoning, key points) streams to the page and is checked sentence by sentence; one that
   fails or runs out of time is 没写成 and the debate goes on. The 上次复盘 line (the product's last settled debate:
   who was right, what the other side said) goes to both sides and the judge.
2. 四次发言, one after another, in plain spoken Chinese, arguing from the four reports in causal chains: the bull
   opens, the bear answers the bull, the bull rebuts, the bear rebuts. Each side sees the whole exchange so far;
   every turn has the same length cap. A fact
   is cited with an inline marker the model writes (`[F12]`, the fact's 资料来源 number); the text streams to
   the reader's windows while it is written (feed.debate).
3. 核对 (program), sentence by sentence once a turn ends: the markers name known facts, every number in the
   sentence is in the facts it cites, no order advice (the desk's rule, `sentence_problem`). A sentence that
   fails stays in the record marked 未核对 and the judge never sees it.
4. 裁决: the judge sees only A and B (the mapping is random and recorded) and their checked sentences.
   The stance tier follows the winner and the margin; 看不清 (too little evidence) is not 震荡. A reply that
   cannot be read marks the debate 需复核 — never a default tier. Confidence is computed here, not reported
   by the model. It also names the key disagreements and two or three 如果…那么… scenarios.

Results are cached by (event or 今日议题, product, facts hash); 再辩一轮 forces a new run. Runs are capped per
day, one or two at a time, every call bounded in time and output; tokens book to `platform:research-debate`.
At the verdict the reckoning is frozen (entry price, 失效位, the single-model 今日判断 of the moment) and five
trading days later the background loop settles it on closing prices. Nothing about any account enters it,
and it never places or suggests an order. Records from before the turns (two parallel structured rounds)
are read through `turns_of`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import time
import uuid
from bisect import bisect_right
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from .instance_lease import HEARTBEAT_SECONDS, LEASE_SECONDS
from .model_provider import KIMI_MODEL
from .research_data import FOREIGN_RESEARCH, research_target
from .research_desk import (
    CHINA,
    NUMBER,
    QUOTE_NOISE,
    STATEMENT_CHARS,
    DeskJudgment,
    DeskPoint,
    ResearchDeskService,
    closed_closes,
    sentence_problem,
)
from .trading_calendar import trading_day_of

log = logging.getLogger("uvicorn.error.research")

USAGE_USER = "platform:research-debate"
ROLES = ("bull", "bear", "judge")
ROLE_NAMES = {"bull": "多方", "bear": "空方", "judge": "裁判", "analyst": "分析师"}
SIDE_MARK = {"bull": "多", "bear": "空"}
# The four analysts write on DeepSeek Flash (platform-billed like the rest); not required to start a debate — one
# that cannot write is 没写成 and the debate goes on.
# The judge runs on DeepSeek Flash (owner 2026-10-25: Kimi Code timed out at 90 s three times in a row in live
# debates while DeepSeek judged in 6 s); Kimi K3.1 Pro is its stand-in.
DEFAULT_MODELS = {"bull": "deepseek-flash", "bear": "glm-5.3-flash", "judge": "deepseek-flash", "analyst": "deepseek-flash"}
MODEL_ENV = {"bull": "AI_TRADER_DEBATE_BULL_MODEL", "bear": "AI_TRADER_DEBATE_BEAR_MODEL", "judge": "AI_TRADER_DEBATE_JUDGE_MODEL"}
# A role's stand-in when its model is unavailable (owner 2026-10-12: the bear only — GLM's 429 failed a whole
# debate). The switch holds for the rest of that debate, and the record names the model that actually spoke.
# The bear falls back to DeepSeek Flash after GLM, the judge to Kimi K3.1 Pro after DeepSeek (owner 2026-10-24/25);
# the record names the model that actually spoke and why.
DEFAULT_FALLBACKS = {"bear": "deepseek-flash", "judge": KIMI_MODEL}
FALLBACK_ENV = {"bear": "AI_TRADER_DEBATE_BEAR_FALLBACK", "judge": "AI_TRADER_DEBATE_JUDGE_FALLBACK"}
# Unavailable = the model never answered: rate limit, auth/quota, missing key, broken stream, timeout, and a
# request that failed without an answer (connection, 5xx, 404). An answer that does not fit the schema
# (model_output_invalid / model_output_empty) is a prompt or parsing problem and never switches.
UNAVAILABLE_CODES = {"model_rate_limited", "model_auth_failed", "model_unavailable", "model_stream_interrupted",
                     "model_request_failed"}
# The same cap per side and turn (the judge must not prefer the longer case), markers not counted. The prompt asks
# for TURN_CHARS; a turn that still runs on is cut at the sentence that passes TURN_LIMIT and ends in "…" — length
# never makes a sentence 未核对 (owner 2026-10-24: 未核对 means a figure its facts do not hold).
# 400–600 characters a turn (owner 2026-10-24: 220-character turns only traded figures, with no causal chain).
# Asked per paragraph: given only the total, the 10-24/25 turns ran 600–970 (mean 745, five of twelve cut); at that
# ~1.25× overshoot three paragraphs of 150 land near 560.
TURN_CHARS = 600
TURN_LIMIT = 750
# The four turns in order: (side, what the turn does).
TURNS = (("bull", "open"), ("bear", "answer"), ("bull", "rebut"), ("bear", "rebut"))
# A streamed turn reaches the windows at most this often (the full text each time, so a lost one costs nothing).
DELTA_SECONDS = 0.25
REASON_CHARS = 40
VERDICT_REASON_CHARS = 130
HEADLINE_CHARS = 70
SWING_CHARS = 40
CHANGE_CHARS = 70
WATCH_CHARS = 36
SIDE_TIMEOUT_SECONDS = 90.0
JUDGE_TIMEOUT_SECONDS = 90.0
# Runaway guards only (2026-10-12): the argument length is bounded by the *_CHARS caps above, and on a
# reasoning provider the thinking counts against this cap too — 1500 left DeepSeek's round-two reply
# empty. Every role call also runs at the lightest reasoning effort (`light_reasoning`): filling a form.
SIDE_MAX_OUTPUT_TOKENS = 6000
JUDGE_MAX_OUTPUT_TOKENS = 8000
# The analysts (owner 2026-10-24). `sheets`: the 底稿 an analyst reads (plus the main contract's price); `needs`: what
# its area should hold (fact id prefixes) — an item with no fact, or only an old value, is a gap; `search`: whether it
# may fill gaps from the web (prices come from the market provider, never a web page).
ANALYSTS: tuple[dict[str, Any], ...] = (
    {"key": "technical", "title": "技术面", "mark": "技", "sheets": ("technical",), "search": False,
     "needs": (("行情", ("M.price",)), ("近20日走势", ("M.trend",)), ("日线指标", ("M.indicators",)), ("日线历史", ("K.history",))),
     "focus": "只看价格本身：趋势、动量、关键价位、量价配合；日线历史可以用来找前高前低、均线位置和最近几天的节奏。"},
    {"key": "supply", "title": "供需基本面", "mark": "供", "sheets": ("basis", "term"), "search": True,
     "needs": (("现货基差", ("D.spot_basis",)), ("仓单或库存", ("D.warehouse_receipt", "O.shfe_stock", "V.inv", "D.inventory")),
               ("开工率", ()), ("利润", ())),
     "focus": "看供需：库存与仓单、开工率、利润、现货基差与期限结构，讲清供给和需求哪边在变、变化怎么传到价格。"},
    {"key": "flow", "title": "资金持仓", "mark": "资", "sheets": ("position",), "search": True,
     "needs": (("前20会员持仓", ("D.position_rank", "O.cftc")), ("持仓与成交量", ("M.position",))),
     "focus": "看资金：前20会员净持仓、持仓量变化、成交量，讲清资金在进场还是离场、多空哪边更主动。"},
    {"key": "news", "title": "新闻宏观", "mark": "讯", "sheets": ("news",), "search": True,
     "needs": (("近期新闻", ("T.", "N.")), ("宏观数据", ("E.", "O.fred"))),
     "focus": "看消息与宏观：快讯、报道、机构观点和宏观数据，讲清每条消息通过什么渠道影响这个品种，哪些已经反映在价格里。"},
)
REPORT_CHARS = 1000
REPORT_LIMIT = 1300
# One analyst — its search and its report together — is bounded, so a slow one cannot hold the debate.
ANALYST_TIMEOUT_SECONDS = 100.0
ANALYST_CALL_TIMEOUT_SECONDS = 75.0
ANALYST_MAX_OUTPUT_TOKENS = 3000
# Web search for gaps only: at most this many queries per analyst and per debate (DeepSeek Search through the Runtime's
# shared client and its daily budget); the first RESULTS_PER_SEARCH results that carry figures are kept; `after:` this
# many days back unless the analyst set its own.
SEARCHES_PER_ANALYST = 2
SEARCHES_PER_DEBATE = 6
RESULTS_PER_SEARCH = 3
SEARCH_SINCE_DAYS = 14
QUERY_CHARS = 120
# 上次复盘: the product's latest debate settled within this many days; the other side's claim cut to this length.
LESSON_DAYS = 30
LESSON_CLAIM_CHARS = 40
DISAGREEMENT_CHARS = 50
SCENARIO_CHARS = 80
SETTLE_DAYS = 5
RANGE_PERCENT = 1.5
RATE_MIN_SAMPLES = 30
SETTLE_AFTER = clock_time(15, 30)
SETTLE_GIVE_UP_DAYS = 12
SETTLE_INTERVAL_SECONDS = 30 * 60
# 失效位: only a price figure (the main contract's price, its limits, the program's 20-day levels), and no farther
# from the entry price than this (percent; AI_TRADER_DEBATE_INVALIDATION_MAX_PERCENT).
PRICE_FACTS = ("M.price", "M.limit", "K.levels")
INVALIDATION_MAX_PERCENT = 8.0
# Blue-green slots share the database: a run belongs to the process that started it (`owner`), which beats
# `heartbeat_at` like the instance lease; one silent for STALE_SECONDS has no owner left and is failed.
STALE_SECONDS = LEASE_SECONDS * 2
ACTIVE = ("queued", "running")
BULLISH = ("偏多", "略偏多")
BEARISH = ("偏空", "略偏空")
STANCES = ("偏多", "略偏多", "震荡", "略偏空", "偏空", "看不清")
DESK_STANCE = {"偏多": "偏多", "略偏多": "偏多", "偏空": "偏空", "略偏空": "偏空", "震荡": "震荡", "看不清": "不明"}
# Why a sentence (or an older record's argument) is 未核对.
PROBLEMS = {"length": "超出这一轮的字数", "no_basis": "没有引用资料", "unknown_basis": "引用的资料编号不存在",
            "advice": "含交易建议", "number_mismatch": "这个数在所引资料里找不到", "quote_mismatch": "摘录不是资料原文",
            "unknown_target": "回应的论点编号不存在"}
SHEETS = (("position", "持仓"), ("basis", "基差与仓单"), ("term", "期限结构"), ("news", "新闻"), ("technical", "技术面"))
# What a 底稿 needs to be 完整: (label, any of these fact ids or id prefixes).
SHEET_CORE: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "position": (("前20会员持仓", ("D.position_rank",)), ("持仓变化", ("M.position",))),
    "basis": (("现货基差", ("D.spot_basis",)), ("仓单或库存", ("D.warehouse_receipt", "O.shfe_stock", "V.inv"))),
    "term": (("期限结构", ("D.term_structure",)),),
    "news": (("新闻", ("T.", "N.")),),
    "technical": (("行情", ("M.price",)), ("近20日走势", ("M.trend",)), ("日线指标", ("M.indicators",)), ("关键价位", ("K.levels",))),
}

# The analysts borrow TradingAgents' analyst team (Apache-2.0, tradingagents/agents/analysts/*.py: a detailed, nuanced
# report per area ending in key points); the search is bounded here: the program lists the gaps and runs the queries.
ANALYST_INSTRUCTIONS = f"""你是期货研究团队里的{{title}}分析师，为接下来的多空辩论写一份中立、细致、有层次的报告。{{focus}}
输入是本软件整理的、你这一块的资料，每条有编号 n、来源和日期，数字都已由程序算好或核对过。
只根据这些资料写，不得使用资料以外的数字、事件或记忆；资料缺的地方直接说缺，不要编。
要讲因果链：一个事实怎么一步步传到价格（比如 供给收缩→库存下降→价格受支撑；成本上升→利润收缩→开工下降→供应减少），不要只罗列数字。多空两面的证据都写，最后说哪边更重。
用到资料里的事实时，在那句话的句号前写上它的编号，格式 [F编号]，例如“仓单连降三天[F4]。”；一句话里的每个数字都必须原样出现在你标注的那条资料里，不要自己计算新数字（编号本身不算数字）。
status 为 stale 的是旧值，用时要说明；kind 为 report 的是第三方说法（网页摘要、机构观点、快讯），说“据报道”或“某某称”。
不给交易建议，不说买入、卖出、开仓、平仓、加仓、减仓、止损、止盈。
格式：第一行写“结论：”加一句话（不超过 30 字）；然后两三段推理；最后一行写“要点：”，下面 3 到 4 行，每行“项目：判断”，例如“库存：连续两周下降[F5]”。
全文 {REPORT_CHARS - 400} 到 {REPORT_CHARS} 字，用大白话，不要 Markdown 标题和表格。"""
PLAN_INSTRUCTIONS = f"""你是期货研究团队里的{{title}}分析师。写报告前可以联网补资料，但只补下面列出的“缺口”：调研页没有、或只有旧值的数据；调研页已有的资料不要再搜。
每个缺口最多一条搜索词，总共不超过 {SEARCHES_PER_ANALYST} 条；搜索词要具体（品种名加指标，例如“螺纹钢 高炉开工率”），不超过 40 个字。
可以加 after:YYYY-MM-DD 只要最近的结果，加 site:域名 限定网站（如 mysteel.com、smm.cn、sci99.com、oilchem.net、stats.gov.cn）。
gap 原样写缺口名称。没有值得搜的就返回空列表。"""

# The turns' wording borrows the style of TauricResearch/TradingAgents' bull / bear researchers (Apache-2.0,
# tradingagents/agents/researchers/*.py: "conversational style, engaging directly with the other analyst's points").
TURN_INSTRUCTIONS = f"""你在一场期货多空辩论里发言，对面是另一位分析师。输入先给“底稿”：本软件刚整理的事实清单，每条有编号 n、来源和日期，数字都已由程序算好；再给四位分析师（技术面、供需基本面、资金持仓、新闻宏观）的中立报告，可能还有“上次复盘”：这个品种上一次辩论五个交易日后的对账，吸取它的教训。
像两个人当面辩那样说大白话：直接冲着对方刚才的话回应（比如“你说表需没起来，我认。但你漏了一点……”），不要列清单、不要小标题、不要堆术语和数据。
要讲因果链，不要只报数字：一个事实怎么一步步传到价格（比如 供给收缩→库存下降→价格受支撑；成本上升→利润收缩→开工下降→供应减少），对方的链条断在哪一环。
只根据底稿和分析师报告说话，不得使用它们以外的数字、事件或记忆；报告里的数字也按它所引的底稿编号标出。
用到底稿里的事实时，在那句话的句号前写上它的编号，格式 [F编号]，例如“RSI 已经掉到 25[F12]。”；一句话里的每个数字都必须原样出现在你标注的那条事实里，不要自己计算新数字（编号本身不算数字）。没有出处的数字会被标成“未核对”，裁判看不到。
status 为 stale 的事实是旧值，用时要说明；kind 为 report 的是第三方说法，说“据报道”或“快讯说”。
不给交易建议，不说买入、卖出、开仓、平仓、加仓、减仓、止损、止盈。
这一次发言写三段，每段不超过 {TURN_CHARS // 3 - 50} 字（全篇 {TURN_CHARS - 200} 到 {TURN_CHARS} 字），宁短勿长。
最后另起一行写“总体看法：偏多”“总体看法：偏空”或“总体看法：震荡”——看完底稿后你自己对价格的真实看法，可以和你代表的一方不同；这一行不算字数，对方看不到。"""
ROLE_LINE = {"bull": "你是多方：找出底稿里支持价格上涨的理由，替上涨说话。", "bear": "你是空方：找出底稿里支持价格下跌的理由，替下跌说话。"}
TURN_TASK = {1: "现在由你开场：先亮出结论，再沿着两三条因果链说最站得住的理由。",
             2: "多方刚开完场（见上面的交锋记录）。现在轮到你：直接回应多方的话，指出哪里站不住，再说你的理由。",
             3: "现在轮到你反驳：回应空方刚才的话，对的就认，再指出他漏掉或说错的地方。",
             4: "最后轮到你：回应多方刚才的反驳，把你的判断收个尾。"}
JUDGE_INSTRUCTIONS = """你是期货调研员，现在裁决一场多空辩论。A 方和 B 方都只根据同一份底稿和四位分析师的中立报告发言，各说了两次（报告和“上次复盘”在输入里，供你一起参考）；你不知道哪一方是哪家模型，判断与发言顺序和篇幅无关。称呼双方一律写“A方”“B方”，不要只写一个字母。
给出的句子都已经过程序核对（引用的事实存在、数字在所引事实里），没通过核对的已经去掉；只在这些句子之间取舍。
winner 写 A、B 或 平；margin 写 明显 或 略（winner 为 平 时忽略）。
双方有冲突，本身不是判“平”的理由：要判断哪一方的理由更站得住。只有证据薄弱或双方基本平衡时才判“平”；证据不足以判断时 insufficient 设为 true，这与“平”不同。
adopted 和 rejected 逐条列出你采纳和驳回的句子编号（只能用给出的编号），各附不超过 30 字的理由。
headline 是不超过 60 字的一句结论，数字只能用底稿里出现过的。
reason 用不超过 120 字的大白话讲清为什么这么判：双方各自哪点站得住、哪点站不住；数字只能用底稿里出现过的。
swing 是决定胜负的那条关键假设（不超过 30 字）；change_mind 写什么情况会让你改判（不超过 60 字）。
invalidation 是失效位：胜方逻辑不再成立的价格，只能取底稿里出现过的价格数字；判“平”或证据不足时为 null。
watch 最多 3 条接下来要看的事，每条不超过 30 字。
disagreements 写 2 到 3 条双方真正的关键分歧（各自认定的因果链在哪一环不同），每条不超过 40 字。
scenarios 写 2 到 3 条“如果……那么……”的情景，每条不超过 70 字，例如“如果仓单继续减少，那么现货偏紧的逻辑会加强”；数字只能用底稿里出现过的。
不给交易建议，不写买入、卖出、开仓、平仓、加仓、减仓、止损、止盈。"""


class VerdictItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    reason: str = ""


class DebateVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    winner: Literal["A", "B", "平"]
    margin: Literal["明显", "略"] = "略"
    insufficient: bool = False
    headline: str
    # The plain-language why (the verdict page's paragraph); checked like every other line.
    reason: str = ""
    adopted: list[VerdictItem] = []
    rejected: list[VerdictItem] = []
    swing: str = ""
    change_mind: str = ""
    invalidation: float | None = None
    watch: list[str] = []
    # 2026-10-24: the key disagreements and 如果…那么… scenarios (absent from older verdicts).
    disagreements: list[str] = []
    scenarios: list[str] = []


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gap: str
    query: str


class SearchPlan(BaseModel):
    """An analyst's web searches for its area's gaps (the program checks each gap and the caps)."""
    model_config = ConfigDict(extra="ignore")
    queries: list[SearchQuery] = []


class DebateUnavailable(Exception):
    """The debate cannot run: `code` is stable, `message` says which role and model in plain words."""

    def __init__(self, code: str, message: str, *, role: str | None = None, model: str | None = None) -> None:
        super().__init__(code)
        self.code, self.message, self.role, self.model = code, message, role, model


class EventProductError(LookupError):
    """An event's debate needs one of its own products: `products` (lower-case ids) are the ones it is about."""

    def __init__(self, code: str, products: list[str]) -> None:
        super().__init__(code)
        self.code, self.products = code, products


def _unavailable(error: BaseException, code: str) -> bool:
    """Whether a failed call means the model is unavailable (switch to the fallback), not that it answered badly."""
    if isinstance(error, TimeoutError):
        return True
    if code.endswith("_api_key_required"):
        return True
    if code not in UNAVAILABLE_CODES:
        return False
    # A request the provider refused as malformed (4xx other than 404/408/409/429) is not an outage.
    status = getattr(error.__cause__, "status_code", None)
    return code != "model_request_failed" or not isinstance(status, int) or status >= 500 or status in {404, 408, 409}


class _CallFailed(Exception):
    def __init__(self, role: str, model: str, code: str, *, unavailable: bool = False) -> None:
        super().__init__(code)
        self.role, self.model, self.code, self.unavailable = role, model, code, unavailable


# -- the program's parts (pure) ---------------------------------------------------------------------

def sheet_of(fact_id: str) -> str:
    if fact_id in {"D.position_rank", "M.position", "O.cftc"}:
        return "position"
    if fact_id in {"D.spot_basis", "D.warehouse_receipt", "O.shfe_stock", "O.nbs_price", "O.eia_crude", "D.hog_price"} \
            or fact_id.startswith("V.inv"):
        return "basis"
    if fact_id == "D.term_structure":
        return "term"
    if fact_id.startswith(("M.", "K.", "R.")):
        return "technical"
    return "news"


def sheets(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The five 底稿 with their state: complete (完整), partial (部分, `missing` names what), missing (缺失),
    stale (过期: a fact in it is an old value the source failed to refresh)."""
    out = []
    for key, title in SHEETS:
        mine = [fact for fact in facts if sheet_of(fact["id"]) == key]
        ids = [fact["id"] for fact in mine]
        lacking = [label for label, wanted in SHEET_CORE[key]
                   if not any(item == want or (want.endswith(".") or want == "V.inv") and item.startswith(want)
                              for item in ids for want in wanted)]
        status = ("missing" if not mine else "stale" if any(fact.get("status") == "stale" for fact in mine)
                  else "partial" if lacking else "complete")
        out.append({"key": key, "title": title, "status": status, "factIds": ids, "missing": lacking})
    return out


def numbered(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Facts in 底稿 order, each with its display number `n` (the 资料来源 list's)."""
    order = {key: index for index, (key, _) in enumerate(SHEETS)}
    ranked = sorted(enumerate(facts), key=lambda pair: (order[sheet_of(pair[1]["id"])], pair[0]))
    return [{**fact, "sheet": sheet_of(fact["id"]), "n": index + 1} for index, (_, fact) in enumerate(ranked)]


MODEL_FIELDS = ("id", "sheet", "kind", "label", "value", "detail", "title", "source", "asOf", "status")


def facts_hash(facts: list[dict[str, Any]]) -> str:
    """Every field the models are shown (`_material_text`), so a changed date, source or stale mark is a new debate."""
    rows = sorted(json.dumps({name: str(({**fact, "sheet": sheet_of(fact["id"])}).get(name) or "") for name in MODEL_FIELDS},
                             ensure_ascii=False, sort_keys=True) for fact in facts)
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:12]


PRICE_NUMBER = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.]*\s*%)")


def price_numbers(facts: list[dict[str, Any]]) -> set[float]:
    """The prices a 失效位 may be: numbers in the value of the price facts, percentages left out."""
    return {float(match) for fact in facts if fact["id"] in PRICE_FACTS
            for match in PRICE_NUMBER.findall(str(fact.get("value") or "").replace(",", ""))}


def invalidation_problem(value: float | None, stance: str, price: float | None, facts: list[dict[str, Any]],
                         max_percent: float) -> str | None:
    """Why a 失效位 cannot stand (None when it can): no_line, no_stance (震荡 / 看不清 / 需复核 have no side to break),
    no_entry (no 发起价 to measure the side and distance from), not_price, wrong_side, too_far."""
    if value is None:
        return "no_line"
    if stance not in (*BULLISH, *BEARISH):
        return "no_stance"
    if not price:
        return "no_entry"
    if float(value) not in price_numbers(facts):
        return "not_price"
    if (stance in BULLISH and not value < price) or (stance in BEARISH and not value > price):
        return "wrong_side"
    # Rounded: 3348 against 3100 is exactly 8 %, never 8.000000000000007 % (#749).
    if round(abs(value / price - 1) * 100, 6) > max_percent:
        return "too_far"
    return None


FOREIGN_SETTLEMENT_VOID = "外盘品种暂不计入对账（CME 的交易日与收盘价口径和国内不同）"


def void_reason(code: str) -> str:
    """Why a debate without a 发起价 is not reckoned, in words: only a quote gone stale in trading hours is 行情过期;
    a closed market that cannot give its price says so (#809)."""
    if code in ("quote_stale", "quote_not_current"):
        return "交易时段内行情过期，没有发起价"
    if code.startswith("quote_"):  # quote_price_missing, quote_age_missing, quote_datetime_missing / _invalid
        return "交易时段内行情缺价格或时间，没有发起价"
    return {"closed_price_missing": "休市中，当天已有成交却拿不到可靠的成交价，没有发起价",
            "close_missing": "休市中，没有最近一个交易日的收盘价"}.get(code, "没有发起时的价格")


# An inline citation the model writes after a sentence ([F12], the fact's 资料来源 number; full-width brackets too).
MARKER = re.compile(r"[\[【［]\s*[Ff]\s*(\d{1,3})\s*[\]】］]")
# Markers written after the sentence's own stop ("……区。[F3]") belong to that sentence.
MARKER_AFTER_STOP = re.compile(r"([。！？；!?])\s*((?:" + MARKER.pattern + r"\s*)+)")
SENTENCE = re.compile(r"[^。！？；!?\n]+[。！？；!?]*")
# The side's own view, the last line of each turn (never shown, never checked): the reckoning's 多方 / 空方 lean.
LEAN_LINE = re.compile(r"总体看法\s*[:：]\s*(偏多|偏空|震荡)")


def plain(text: str) -> str:
    """A sentence as the judge and 今日判断 read it: markers and the closing stop left out."""
    return MARKER.sub("", text).strip().rstrip("。；;")


def spoken(text: str) -> tuple[str, str | None]:
    """A turn's words without the closing 总体看法 line (cut there while it is still being written), and that lean."""
    found = LEAN_LINE.search(text)
    return text.split("总体看法", 1)[0].strip(), found[1] if found else None


def check_turn(side: str, text: str, first: int, by_n: dict[int, str], by_id: dict[str, dict[str, Any]], *,
               limit: int = TURN_LIMIT, mark: str | None = None) -> list[dict[str, Any]]:
    """A finished turn, sentence by sentence (ids 多1, 多2 … go on across the side's turns from `first`): its
    markers must name known facts, every number must be in the facts it cites, no advice (the desk's
    `sentence_problem`); a turn past `limit` is cut before the sentence that passes it, the last kept one ending
    in "…". A failed one keeps its reason. An analyst's report is checked the same way (`side` its key, `mark` its
    id prefix, REPORT_LIMIT)."""
    out: list[dict[str, Any]] = []
    used = 0
    for piece in SENTENCE.findall(MARKER_AFTER_STOP.sub(r"\2\1", text)):
        piece = piece.strip()
        numbers = [int(n) for n in MARKER.findall(piece)]
        if not piece or not plain(piece):
            continue
        used += len(plain(piece))
        if used > limit and out:
            out[-1]["text"] = out[-1]["text"].rstrip("。！？!?") + "…"
            break
        basis = list(dict.fromkeys(by_n[n] for n in numbers if n in by_n))
        problem = ("unknown_basis" if any(n not in by_n for n in numbers)
                   else sentence_problem(plain(piece), [by_id[item] for item in basis], limit))
        out.append({"id": f"{mark or SIDE_MARK[side]}{first + len(out)}", "side": side, "text": piece, "factIds": basis,
                    "verified": problem is None, **({"problem": PROBLEMS.get(problem, problem)} if problem else {})})
    return out


def area_gaps(analyst: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What an analyst's area lacks: an item no fact holds (`missing`), or one held only by old values (`stale`)."""
    out = []
    for label, wanted in analyst["needs"]:
        held = [fact for fact in facts if any(fact["id"].startswith(prefix) for prefix in wanted)]
        if not held or all(fact.get("status") == "stale" for fact in held):
            out.append({"label": label, "state": "stale" if held else "missing"})
    return out


POINTS_HEAD = re.compile(r"^要点\s*[:：]?\s*(.*)$")
POINT_LINE = re.compile(r"^(?:[-•·*]|\d+[.、)）])?\s*([^：:|]{1,12})\s*[：:]\s*(.+)$")


def report_parts(text: str) -> tuple[str, str, list[tuple[str, str]]]:
    """An analyst's report as written: the 结论 line, the reasoning, and the 要点 rows (项目：判断; a Markdown table's
    rows are read too, its header and rule left out)."""
    conclusion, listing = "", False
    body: list[str] = []
    points: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip().strip("*#").strip()
        if not line:
            continue
        if not conclusion and not body and not listing and line.startswith("结论"):
            conclusion = re.sub(r"^结论\s*[:：]?\s*", "", line)
            continue
        head = POINTS_HEAD.match(line)
        if head and not listing:
            listing, line = True, head[1].strip()
            if not line:
                continue
        if listing:
            if line.startswith("|"):
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                if len(cells) >= 2 and cells[0] and not set(cells[0]) <= set("-: ") and cells[0] not in ("项目", "维度", "指标"):
                    points.append((cells[0][:12], "，".join(cell for cell in cells[1:] if cell)))
                continue
            found = POINT_LINE.match(line)
            if found:
                points.append((found[1].strip(), found[2].strip()))
            continue
        body.append(line)
    return conclusion, "\n".join(body), points[:4]


def check_report(analyst: dict[str, Any], text: str, by_n: dict[int, str], by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A finished report, checked like a turn: the conclusion and each key point as one line, the reasoning
    sentence by sentence (cut past REPORT_LIMIT)."""
    key, mark = analyst["key"], analyst["mark"]

    def one(line: str) -> dict[str, Any]:
        pieces = check_turn(key, line, 1, by_n, by_id, limit=REPORT_LIMIT, mark=mark)
        problem = next((piece["problem"] for piece in pieces if not piece["verified"]), None)
        return {"text": line, "factIds": list(dict.fromkeys(item for piece in pieces for item in piece["factIds"])),
                "verified": problem is None, **({"problem": problem} if problem else {})}
    conclusion, body, points = report_parts(text)
    return {"conclusion": one(conclusion) if conclusion else None,
            "sentences": check_turn(key, body, 1, by_n, by_id, limit=REPORT_LIMIT, mark=mark),
            "points": [{"label": label, **one(value)} for label, value in points]}


def reports_text(analysts: list[dict[str, Any]]) -> str:
    """The four reports as the sides and the judge read them: checked lines only, markers kept (they cite the 底稿)."""
    rows = []
    for report in analysts:
        if report.get("status") != "done":
            rows.append(f"【{report['title']}分析师】没写成")
            continue
        conclusion = report.get("conclusion") or {}
        parts = [f"结论：{conclusion['text']}。" if conclusion.get("verified") else "",
                 *(item["text"] for item in report.get("sentences") or [] if item["verified"])]
        points = "；".join(f"{item['label']}：{item['text']}" for item in report.get("points") or [] if item["verified"])
        rows.append(f"【{report['title']}分析师】" + "".join(parts).replace("\n", " ") + (f"要点：{points}" if points else ""))
    return "分析师报告（中立，只留核对过的句子）：\n" + "\n".join(rows) if rows else ""


def lesson_of(record: dict[str, Any]) -> dict[str, Any] | None:
    """上次复盘 from a settled debate (its frozen five-day reckoning): whether the judge was right, then each side by
    its own scored lean (`outcomes.bull` / `outcomes.bear` — never inferred from the judge, whose 失效位 can make
    it wrong while the winner's direction held) and what a side that turned out wrong said — the judge's pick of its
    sentences first, else its first checked one. None when the judge was not scored."""
    settlement, verdict = record.get("settlement") or {}, record.get("verdict") or {}
    outcomes = settlement.get("outcomes") or {}
    outcome = outcomes.get("judge")
    if settlement.get("status") != "settled" or outcome not in ("right", "wrong") or not verdict:
        return None
    when = str(record.get("researchDay") or "")[5:]
    winner, right = verdict.get("winner"), outcome == "right"
    if winner not in ("bull", "bear"):
        text = f"上次（{when}）裁判判双方相当（{verdict.get('stance')}），5 个交易日后{'说对了' if right else '说错了，价格走出了方向'}。"
    else:
        said = sentences_of(record)
        picks = [item.get("id") for item in [*verdict.get("adopted", []), *verdict.get("rejected", [])]]

        def claim_of(side: str) -> str:
            claim_id = next((item for item in picks if item in said and said[item]["side"] == side), None) or next(
                (item_id for item_id, item in said.items() if item["side"] == side and item.get("verified")), None)
            claim = plain(said[claim_id]["text"]) if claim_id else ""
            return claim if len(claim) <= LESSON_CLAIM_CHARS else claim[:LESSON_CLAIM_CHARS] + "…"
        sides = [f"{ROLE_NAMES[side]}看对了" for side in ("bull", "bear") if outcomes.get(side) == "right"]
        for side in ("bull", "bear"):
            if outcomes.get(side) == "wrong":
                claim = claim_of(side)
                sides.append(f"{ROLE_NAMES[side]}当时说“{claim}”，没有兑现" if claim else f"{ROLE_NAMES[side]}没看对")
        text = (f"上次（{when}）裁判判{ROLE_NAMES[winner]}{'明显胜出' if verdict.get('margin') == '明显' else '略胜'}（{verdict.get('stance')}），"
                f"5 个交易日后{'说对了' if right else '说错了'}" + (f"：{'，'.join(sides)}。" if sides else "。"))
    return {"id": record["id"], "researchDay": record.get("researchDay"), "outcome": outcome, "text": text}


def turns_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The exchange as turns. A record from before the turns (two parallel structured rounds, 2026-10-11 … 10-24)
    reads as four: each side's round-one arguments, then each side's rebuttals."""
    if "turns" in record:
        return list(record["turns"])
    rounds = record.get("rounds") or {}
    parts = [((rounds.get("opening") or {}).get("bull"), "bull"), ((rounds.get("opening") or {}).get("bear"), "bear"),
             ((rounds.get("rebuttal") or {}).get("bull"), "bull"), ((rounds.get("rebuttal") or {}).get("bear"), "bear")]
    return [{"n": n, "side": side, "status": "done", "text": "", "sentences": list(items or [])}
            for n, (items, side) in enumerate(parts, start=1) if items is not None]


def sentences_of(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for turn in turns_of(record) for item in turn.get("sentences") or []}


def excerpt(fact: dict[str, Any]) -> str:
    """A cited fact's own words, for a 今日判断 point made from a debate sentence (the desk's quote check):
    turns cite by marker, so the program copies the excerpt from the fact itself."""
    return QUOTE_NOISE.sub("", " ".join(str(fact.get(name) or "") for name in ("label", "value")))[:30]


# The judge writes about "A 方" / "A3" (JUDGE_INSTRUCTIONS); the reader sees 空方 / 空3 (#768). Only the anonymous
# side names and the argument ids the judge was actually shown: a bare letter (A股), an index or contract name
# that merely looks like an id (富时A50, 中证A500 — no such argument) and words like B方案 / A 方面 are left alone (#771).
ANON_SIDE = re.compile(r"(?<![A-Za-z0-9])([AB])\s*方(?![案面法式向针])")
ANON_ID = re.compile(r"(?<![A-Za-z0-9])([AB])(驳?)(\d+)(?![\d.])")
# …and a bare A / B the judge wrote without 方 ("A抓住…", "B的期货…", "可改判B", owner 2026-10-24): a lone capital not
# touching another letter, digit, space or a word it names (A股, B级, B超, A类…).
ANON_BARE = re.compile(r"(?<![A-Za-z0-9])([AB])(?![A-Za-z0-9\s方股级类档区型款版轮超面案])")


def named(text: str, mapping: dict[str, str], from_anon: dict[str, str]) -> tuple[str, str]:
    """A verdict line with the judge's A/B names and argument ids (`from_anon`: anonymous id → real id) put back to
    the real sides, and the same line with those ids left out for the number check (an argument's number is a
    label, not a figure)."""
    text = ANON_SIDE.sub(lambda match: ROLE_NAMES[mapping[match[1]]], text.strip())

    def bare(line: str) -> str:
        return ANON_BARE.sub(lambda match: ROLE_NAMES[mapping[match[1]]], line)
    return (bare(ANON_ID.sub(lambda match: from_anon.get(match[0], match[0]), text)),
            bare(ANON_ID.sub(lambda match: "" if match[0] in from_anon else match[0], text)))


def side_supported(argument: dict[str, Any]) -> str:
    """The side an argument supports: its speaker's, except a concession (承认对方), which supports the other side."""
    side = str(argument["side"])
    return ("bear" if side == "bull" else "bull") if argument.get("concede") else side


def stance_of(winner: str | None, margin: str, insufficient: bool) -> str:
    """The tier follows the winner and the margin (62d3479); 看不清 is too little evidence, not 震荡."""
    if insufficient:
        return "看不清"
    if winner is None:
        return "震荡"
    strong = margin == "明显"
    return ("偏多" if strong else "略偏多") if winner == "bull" else ("偏空" if strong else "略偏空")


def confidence(stance: str, winner: str | None, margin: str, adopted: list[dict[str, Any]], refuted: int,
               by_id: dict[str, dict[str, Any]], sheet_states: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """信心 (computed, never self-reported): the share of the adopted arguments' cited facts that are data
    (not reports) and current (not stale) gives 0–2 points, a 明显 margin +0.5, each of the winner's arguments
    the judge rejected −0.5; ≥ 1.8 高, ≥ 1.0 中, else 低. A 底稿 missing or stale caps it at 中, two or more
    missing at 低; 震荡 is at most 中 and 看不清 is 低."""
    cited = list(dict.fromkeys(item for argument in adopted for item in argument["factIds"] if item in by_id))
    solid = [item for item in cited if by_id[item].get("kind") == "data" and by_id[item].get("status") != "stale"]
    share = len(solid) / len(cited) if cited else 0.0
    score = share * 2 + (0.5 if winner and margin == "明显" else 0.0) - 0.5 * min(refuted, 3)
    level = "高" if score >= 1.8 else "中" if score >= 1.0 else "低"
    missing = sum(1 for sheet in sheet_states if sheet["status"] == "missing")
    stale = sum(1 for sheet in sheet_states if sheet["status"] == "stale")
    cap = "低" if missing >= 2 else "中" if missing or stale else "高"
    if winner is None:
        cap = min(cap, "中", key="低中高".index)
    if stance == "看不清":
        cap = "低"
    level = min(level, cap, key="低中高".index)
    return level, {"evidenceShare": round(share, 2), "refuted": refuted, "missingSheets": missing, "staleSheets": stale,
                   "cap": cap, "score": round(score, 2)}


def settle_outcome(stance: str | None, entry: float | None, invalidation: float | None, closes: list[float]) -> str | None:
    """The frozen rule, five closes after the debate: 偏多 / 略偏多 is right when no close fell below the 失效位
    and the fifth closed above the entry price (偏空 / 略偏空 the mirror); 震荡 is right when the fifth close is
    within 1.5% of the entry; 看不清 / 需复核 / no stance or price: not counted (None)."""
    if not entry or len(closes) < SETTLE_DAYS or stance not in (*BULLISH, *BEARISH, "震荡"):
        return None
    last = closes[SETTLE_DAYS - 1]
    window = closes[:SETTLE_DAYS]
    if stance in BULLISH:
        broke = invalidation is not None and any(close < invalidation for close in window)
        return "right" if not broke and last > entry else "wrong"
    if stance in BEARISH:
        broke = invalidation is not None and any(close > invalidation for close in window)
        return "right" if not broke and last < entry else "wrong"
    return "right" if abs(last / entry - 1) * 100 <= RANGE_PERCENT else "wrong"


def event_headline(event: dict[str, Any]) -> str:
    title, text = str(event.get("title") or ""), str(event.get("text") or "")
    bare = title.rstrip("…").strip()
    return title if bare and not text.startswith(bare) else (text or title)


def _now_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


@dataclass
class ResearchDebateService:
    desk: ResearchDeskService
    connection: sqlite3.Connection
    lock: Any  # the store's lock (threading.Lock or RLock)
    # The 快讯 events (research_live.product_events); None: only the 今日议题 can be debated.
    events: Callable[[str], tuple[str, list[dict[str, Any]]]] | None = None
    # Which products an event id belongs to (research_live.event_products): the one event → product lookup.
    event_products: Callable[[str], list[str]] | None = None
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    rng: random.Random = field(default_factory=random.Random)
    daily_runs: int = field(default_factory=lambda: int(os.getenv("AI_TRADER_DEBATE_DAILY_RUNS", "30") or 0))
    models: dict[str, str] = field(default_factory=lambda: {
        role: (os.getenv(MODEL_ENV[role], "") or DEFAULT_MODELS[role]).strip() for role in ROLES})
    fallbacks: dict[str, str] = field(default_factory=lambda: {
        role: (os.getenv(FALLBACK_ENV[role], "") or DEFAULT_FALLBACKS[role]).strip() for role in FALLBACK_ENV})
    # The model adapter (the Runtime's own, platform-billed); set by the research routes.
    generator: Callable[[], Any] | None = None
    # The Runtime's shared web search (web_search.WebSearchClient: `search(query, count=)`); None: analysts do not search.
    search: Any = None
    # Called with (user id, event) on every step and at the end: {"kind": "progress"|"done", ...}.
    notify: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    concurrency: int = field(default_factory=lambda: max(1, min(2, int(os.getenv("AI_TRADER_DEBATE_CONCURRENCY", "1") or 1))))
    invalidation_max_percent: float = field(default_factory=lambda: float(
        os.getenv("AI_TRADER_DEBATE_INVALIDATION_MAX_PERCENT", "") or INVALIDATION_MAX_PERCENT))
    # This process: the runs it started carry it and its heartbeat (blue-green slots share the table).
    owner: str = field(default_factory=lambda: uuid.uuid4().hex)
    _starting: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    _beat: asyncio.Task[None] | None = field(default=None, repr=False)
    # Runs this process failed for a silent owner, still to be announced to their waiters.
    _announce: list[str] = field(default_factory=list, repr=False)
    # Runs of this process another slot reaped while it stalled (their writes are dropped).
    _fenced: set[str] = field(default_factory=set, repr=False)
    _live: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, repr=False)
    _slots: asyncio.Semaphore | None = field(default=None, repr=False)
    _loop: asyncio.Task[None] | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.models.setdefault("analyst", DEFAULT_MODELS["analyst"])
        with self.lock, self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS research_debates (id TEXT PRIMARY KEY, product_key TEXT NOT NULL,"
                " symbol TEXT NOT NULL, topic_key TEXT NOT NULL, research_day TEXT NOT NULL, day TEXT NOT NULL,"
                " facts_hash TEXT NOT NULL, status TEXT NOT NULL, user_id TEXT, created_at TEXT NOT NULL,"
                " settle_on TEXT, outcome TEXT, owner TEXT, heartbeat_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,"
                " payload_json TEXT NOT NULL)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS research_debates_product"
                                    " ON research_debates (product_key, research_day)")
            # Everyone waiting on a run (its starter and whoever reused it while it ran) gets its progress and end.
            self.connection.execute("CREATE TABLE IF NOT EXISTS research_debate_watchers (debate_id TEXT NOT NULL, user_id TEXT NOT NULL,"
                                    " PRIMARY KEY (debate_id, user_id))")
            # One run at a time per (topic, product, research day), across processes too.
            self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS research_debates_one_active"
                                    " ON research_debates (product_key, topic_key, research_day) WHERE status IN ('queued', 'running')")
        self._reap()

    def _reap(self) -> int:
        """A run whose owner stopped beating (its process ended mid-run) cannot resume: failed, said so, and its
        waiters told on the next beat. A run another live slot is executing keeps its fresh heartbeat."""
        cutoff = (self.now() - timedelta(seconds=STALE_SECONDS)).astimezone(UTC).isoformat(timespec="seconds")
        reaped = 0
        with self.lock, self.connection:
            stale = [row[0] for row in self.connection.execute(
                "SELECT id FROM research_debates WHERE status IN ('queued', 'running')"
                " AND (heartbeat_at IS NULL OR heartbeat_at < ?) AND (owner IS NULL OR owner != ?)", (cutoff, self.owner))]
            for debate_id in stale:
                # Only the slot whose update lands announces it (two slots may reap at once). The UPDATE checks the
                # heartbeat and owner again (#749): a run its owner renewed after the SELECT is alive, not reaped.
                if self.connection.execute(
                        "UPDATE research_debates SET status = 'failed', owner = NULL, payload_json = json_set(payload_json, '$.status',"
                        " 'failed', '$.error', 'Runtime 重启或切换，这次研讨中断了') WHERE id = ? AND status IN ('queued', 'running')"
                        " AND (heartbeat_at IS NULL OR heartbeat_at < ?) AND (owner IS NULL OR owner != ?)",
                        (debate_id, cutoff, self.owner)).rowcount:
                    self._announce.append(debate_id)
                    reaped += 1
            if not self._closed:
                # A slot that closed mid-run stored the run's end but could not announce it (its windows are going
                # away): whichever live slot comes next does, once (#744).
                ended = [row[0] for row in self.connection.execute(
                    "SELECT id FROM research_debates WHERE status NOT IN ('queued', 'running')"
                    " AND json_extract(payload_json, '$.pendingNotice') = 1")]
                for debate_id in ended:
                    if self.connection.execute(
                            "UPDATE research_debates SET payload_json = json_remove(payload_json, '$.pendingNotice')"
                            " WHERE id = ? AND json_extract(payload_json, '$.pendingNotice') = 1", (debate_id,)).rowcount:
                        self._announce.append(debate_id)
        return reaped

    async def _announce_reaped(self) -> None:
        while self._announce:
            record = self._stored(self._announce.pop(0))
            if record is not None:
                await self._publish(record, "done", save=False)  # the reap's own UPDATE is the stored end

    def beat(self) -> None:
        """This process's heartbeat on its running debates, and a cancel another slot asked for."""
        if not self._live:
            return
        ids = list(self._live)
        marks = ",".join("?" for _ in ids)
        with self.lock, self.connection:
            self.connection.execute(f"UPDATE research_debates SET heartbeat_at = ? WHERE owner = ? AND id IN ({marks})",  # noqa: S608
                                    (_now_iso(self.now()), self.owner, *ids))
            asked = [row[0] for row in self.connection.execute(
                f"SELECT id FROM research_debates WHERE cancel_requested = 1 AND id IN ({marks})", ids)]  # noqa: S608
        for debate_id in asked:
            task = self._tasks.get(debate_id)
            if task is not None:
                task.cancel()

    async def _beat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                self.beat()
                self._reap()
                await self._announce_reaped()
            except sqlite3.Error:
                log.exception("research_debate_heartbeat_failed")

    # -- reading

    def get(self, debate_id: str) -> dict[str, Any] | None:
        live = self._live.get(debate_id)
        if live is not None:
            return self.view(live)
        self._reap()
        with self.lock:
            row = self.connection.execute("SELECT payload_json FROM research_debates WHERE id = ?", (debate_id,)).fetchone()
        return self.view(json.loads(row[0])) if row else None

    def listing(self, *, product: str | None = None, events: list[str] | None = None, day: str | None = None,
             limit: int = 20) -> dict[str, Any]:
        """Summaries newest first — by product (`SHFE.rb`), by event ids (the 已研讨 tags), by research day —
        and the track record (`stats`)."""
        clauses, args = [], []
        if product:
            exchange, name, _ = research_target(product)
            clauses.append("product_key = ?")
            args.append(f"{exchange}.{name.lower()}")
        if events:
            clauses.append(f"topic_key IN ({','.join('?' for _ in events)})")
            args.extend(f"event:{item}" for item in events)
        if day:
            clauses.append("research_day = ?")
            args.append(day)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.lock:
            rows = self.connection.execute(f"SELECT id, payload_json FROM research_debates{where}"  # noqa: S608 — fixed clauses
                                           " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
        items = [self.summary(self._live.get(debate_id) or json.loads(payload)) for debate_id, payload in rows]
        return {"items": items, "stats": self.stats(), "day": self.desk.research_day(self.now())}

    @staticmethod
    def summary(record: dict[str, Any]) -> dict[str, Any]:
        verdict = record.get("verdict") or {}
        topic = record.get("topic") or {}
        return {"id": record["id"], "status": record["status"], "symbol": record["symbol"], "productKey": record["productKey"],
                "name": record.get("name") or "", "researchDay": record["researchDay"], "createdAt": record["createdAt"],
                "finishedAt": record.get("finishedAt"), "topic": {"kind": topic.get("kind"), "eventId": topic.get("eventId"),
                                                                 "title": topic.get("title")},
                "stance": verdict.get("stance"), "confidence": verdict.get("confidence"),
                # 以前的辩论 (2026-10-24): who won and the 5-day reckoning's outcome for the judge.
                "winner": verdict.get("winner"), "margin": verdict.get("margin"), "insufficient": verdict.get("insufficient"),
                "settlement": {"status": settlement.get("status"), "due": settlement.get("due"),
                               "outcome": (settlement.get("outcomes") or {}).get("judge")} if (settlement := record.get("settlement")) else None}

    def view(self, record: dict[str, Any]) -> dict[str, Any]:
        out = {key: value for key, value in record.items() if key not in ("userId", "pendingNotice")}
        out["turns"] = turns_of(record)
        # The exchange's counts as the page shows them (two rounds of two turns; sentences that failed the program's
        # check; facts the passing ones cite) — the chat AI reads the same numbers.
        arguments = list(sentences_of(record).values())
        out["checks"] = {"rounds": 2 if len(out["turns"]) > 2 else 1, "arguments": len(arguments),
                         "unverified": sum(1 for item in arguments if not item.get("verified")),
                         "cited": len({fact for item in arguments if item.get("verified") for fact in item.get("factIds") or []})}
        out["models"] = {role: record.get("models", {}).get(role) for role in (*ROLES, "analyst")}
        mapping, verdict = record.get("mapping"), record.get("verdict")
        if mapping and isinstance(verdict, dict):
            # A verdict stored before the bare-letter mapping (2026-10-24) still reads in 多方 / 空方.
            def real(text: Any) -> Any:
                return ANON_BARE.sub(lambda match: ROLE_NAMES[mapping[match[1]]], text) if isinstance(text, str) else text
            out["verdict"] = {**verdict, **{key: real(verdict.get(key)) for key in ("headline", "reason", "swing", "changeMind")},
                              **{key: [real(item) for item in verdict.get(key) or []] for key in ("watch", "disagreements", "scenarios")},
                              **{key: [{**item, "reason": real(item.get("reason"))} for item in verdict.get(key) or []]
                                 for key in ("adopted", "rejected")}}
        # Whether it is of the research day now (only today's may be written into 今日判断) — the Runtime's clock,
        # never the reader's.
        out["today"] = record.get("researchDay") == self.desk.research_day(self.now())
        return out

    def stats(self) -> dict[str, Any]:
        """研讨战绩: settled debates, and once there are RATE_MIN_SAMPLES of them each participant's hits —
        the bull model's and the bear model's own lean, the judge's verdict, the single-model 今日判断."""
        with self.lock:
            rows = self.connection.execute("SELECT outcome, json_extract(payload_json, '$.settlement')"
                                           " FROM research_debates WHERE outcome IS NOT NULL OR settle_on IS NOT NULL").fetchall()
        settled = [json.loads(raw) for outcome, raw in rows if outcome == "settled" and raw]
        pending = sum(1 for outcome, _ in rows if outcome is None)
        out: dict[str, Any] = {"settled": len(settled), "pending": pending, "minSamples": RATE_MIN_SAMPLES, "rates": None}
        if len(settled) >= RATE_MIN_SAMPLES:
            rates = {}
            for who in ("bull", "bear", "judge", "desk"):
                counted = [item["outcomes"].get(who) for item in settled if (item.get("outcomes") or {}).get(who)]
                rates[who] = {"right": sum(1 for value in counted if value == "right"), "total": len(counted)}
            out["rates"] = rates
        return out

    # -- starting

    def check_models(self, generator: Any) -> None:
        """Every role needs a model on hand (its own or its configured fallback): which role and which model is
        missing, never an unconfigured swap."""
        if generator is None or not all(hasattr(generator, name) for name in ("complete_structured", "stream_text")) or not getattr(generator, "agent_authenticated", False):
            raise DebateUnavailable("research_debate_model_unavailable", "模型服务未连接，不能研讨")
        endpoints = getattr(generator, "endpoints", None)
        if isinstance(endpoints, dict):
            for role in ROLES:
                if not any(model in endpoints for model in self._chain(role)):
                    raise DebateUnavailable("research_debate_model_unavailable",
                                            f"{ROLE_NAMES[role]}模型 {self.models[role]} 不可用（没有配置这个模型的密钥）",
                                            role=role, model=self.models[role])

    def _chain(self, role: str, current: str | None = None) -> list[str]:
        """The models a role may speak with, in order: its current one (the configured model, or the fallback once
        a debate switched to it), then its fallback."""
        first = current or self.models[role]
        fallback = self.fallbacks.get(role)
        return [first] + ([fallback] if fallback and fallback != first else [])

    async def start(self, symbol: str | None, *, event_id: str | None = None, force: bool = False,
                    user_id: str | None = None, generator: Any = None) -> dict[str, Any]:
        """A debate on the product's 今日议题, or on one news event: the cached one when (topic, product, facts)
        is unchanged — an event is debated once per product and research day — else a new run. `force`
        (再辩一轮) always runs again. Answers the debate view with `reused` true for a cached one.
        An event's product comes from the event itself (`event_symbol`); a `symbol` given with it only picks
        among the event's products and is refused when the event is not about it."""
        if self._closed:
            raise DebateUnavailable("research_debate_closed", "Runtime 正在关闭")
        # The caller's adapter (the routes resolve the reader's own, as the desk does); else the service's.
        generator = generator if generator is not None else (self.generator() if self.generator is not None else None)
        if event_id:
            symbol = await self.event_symbol(event_id, symbol)
        elif not symbol:
            raise ValueError("research_symbol_invalid")
        exchange, product, foreign = research_target(symbol)  # ValueError("research_symbol_invalid")
        key = f"{exchange}.{product.lower()}"
        topic_key = f"event:{event_id}" if event_id else "today"
        # 外盘（2026-10-21）：快讯事件按它的快讯键归属（纳指 → nasdaq）。
        screen = (foreign.news_product or product) if foreign is not None else product
        # Two clicks on one topic in this process wait for each other; across processes the unique index decides.
        async with self._starting.setdefault(f"{key}|{topic_key}", asyncio.Lock()):
            return await self._start(symbol, key, screen, topic_key, event_id, force, user_id, generator)

    async def _start(self, symbol: str, key: str, product: str, topic_key: str, event_id: str | None, force: bool,
                     user_id: str | None, generator: Any) -> dict[str, Any]:
        day = self.desk.research_day(self.now())
        self._reap()
        if not force:
            same = self._latest(key, topic_key, day)
            if same is not None and (same["status"] in ("queued", "running")
                                     or (event_id and same["status"] in ("done", "review"))):
                return self._reused(same, user_id)
        started = self.clock()
        event = await self._event(product, event_id) if event_id else None
        material = await self.desk.debate_material(symbol)
        # Gathering may have crossed 08:30: the material's research day is the one this run belongs to (#744).
        day = material.get("researchDay") or day
        facts = list(material["facts"])
        if event is not None:
            facts.append(event["fact"])
        digest = facts_hash(facts)
        if not force:
            cached = self._cached(key, topic_key, digest, day)
            if cached is not None:
                return self._reused(cached, user_id)
        # Only a new run needs the models: a reusable result is answered even while a role's model is down.
        self.check_models(generator)
        # Gathering took a while: a run that started meanwhile (here or in another slot) is the one to show.
        same = self._latest(key, topic_key, day)
        if same is not None and (same["status"] in ACTIVE or (not force and event_id and same["status"] in ("done", "review"))):
            return self._reused(same, user_id)
        record = self._record(material, facts, digest, event, topic_key, user_id, round(self.clock() - started, 1))
        if force and same is not None:
            record["previousId"] = same["id"]
        self._live[record["id"]] = record
        try:
            inserted = self._insert(record)
        except sqlite3.IntegrityError:
            self._live.pop(record["id"], None)
            # The material may belong to the next research day (the request began before 08:30).
            active = self._latest(key, topic_key, record["researchDay"])
            if active is None:
                raise
            return self._reused(active, user_id)
        if not inserted:
            self._live.pop(record["id"], None)
            raise DebateUnavailable("research_debate_budget", f"今天的研讨次数（{self.daily_runs} 次）已用完，明天再来")
        if user_id:
            self._watch(record["id"], user_id)
        task = asyncio.create_task(self._run(record, generator))
        self._tasks[record["id"]] = task
        task.add_done_callback(lambda done: self._finished(record, done))
        return {**self.view(record), "reused": False}

    def _finished(self, record: dict[str, Any], task: asyncio.Task[None]) -> None:
        if task.cancelled() and record["status"] in ACTIVE:
            # Cancelled before its first step ran (the body never started): still a terminal state on disk.
            record["status"], record["error"] = ("failed", "Runtime 重启或切换，这次研讨中断了") if self._closed else ("cancelled", "已取消")
            record["finishedAt"] = _now_iso(self.now())
            if self._closed:
                record["pendingNotice"] = True  # the next live slot tells its waiters (_reap)
            self._save(record)
        self._tasks.pop(record["id"], None)
        self._live.pop(record["id"], None)

    async def event_symbol(self, event_id: str, symbol: str | None = None) -> str:
        """The contract an event's debate runs on, decided here for every caller (a 快讯 row, a UI command, voice,
        an agent, the CLI): the event's own product — its main contract, or the caller's contract of it.
        LookupError research_debate_event_unknown (no such event in the 24-hour window); EventProductError when
        `symbol` is not one of the event's products, or none is given and the event has several."""
        products = await asyncio.to_thread(self.event_products, event_id) if self.event_products is not None else []
        if not products:
            raise LookupError("research_debate_event_unknown")
        if symbol:
            _, product, foreign = research_target(symbol)  # ValueError("research_symbol_invalid")
            if foreign is not None:
                product = foreign.news_product or product
            if product.lower() not in products:
                raise EventProductError("research_debate_event_product", products)
            return symbol
        if len(products) > 1:
            raise EventProductError("research_debate_event_ambiguous", products)
        main = await self.desk.main_symbol(products[0])
        if main is None:
            raise ValueError("research_symbol_unknown")
        return main

    async def _event(self, product: str, event_id: str) -> dict[str, Any]:
        if self.events is None:
            raise LookupError("research_debate_event_unknown")
        _, events = await asyncio.to_thread(self.events, product.lower())
        found = next((item for item in events if item.get("id") == event_id), None)
        if found is None:
            raise LookupError("research_debate_event_unknown")
        ai = found.get("ai") or {}
        headline = event_headline(found)
        published = str(found.get("publishedAt") or "")
        try:
            clock_text = datetime.fromisoformat(published).astimezone(CHINA).strftime("%H:%M")
        except ValueError:
            clock_text = ""
        sources = [str(item) for item in found.get("sources") or [] if item]
        fact = {"id": "T.event", "section": "flash", "kind": "report", "label": "引发研讨的快讯",
                "value": f"{clock_text} {'／'.join(sources[:3])}：{headline}".strip()[:160], "source": "快讯（多家报道已合并）",
                "asOf": published or None, "detail": str(found.get("text") or "")[:400] or None}
        link = next((item.get("link") for item in found.get("items") or [] if item.get("link")), None)
        if link:
            fact["url"] = link
        topic = {"kind": "event", "eventId": event_id, "title": headline, "text": str(found.get("text") or "")[:400],
                 "publishedAt": published or None, "tier": found.get("tier"), "direction": ai.get("direction"),
                 "strength": ai.get("strength"), "transmission": ai.get("transmission"), "reports": found.get("reports")}
        return {"topic": topic, "fact": {name: value for name, value in fact.items() if value is not None}}

    def _record(self, material: dict[str, Any], facts: list[dict[str, Any]], digest: str, event: dict[str, Any] | None,
                topic_key: str, user_id: str | None, collect_seconds: float) -> dict[str, Any]:
        now = self.now()
        name = material.get("productName") or material["product"]
        code = str(material.get("mainSymbol") or "").split(".")[-1]
        topic = (event or {}).get("topic") or {"kind": "today", "title": f"今天{name}怎么看"}
        month = re.search(r"\d+$", code)  # 2701 / CZCE's three-digit 701 — the contract's own digits, never a fixed width
        topic["question"] = (f"这条消息对 {name}{month.group() if month else ''} 意味着什么？" if topic["kind"] == "event"
                             else f"今天{name}怎么看？")
        listed = numbered(facts)
        judgment = material.get("judgment") or {}
        research_day = material.get("researchDay") or self.desk.research_day(now)
        # The benchmark is this research day's 今日判断 only: before today's exists (08:30 turned, not generated yet,
        # failing) the desk still shows yesterday's, which must not be scored as today's single-model call.
        compare = ({"stance": judgment.get("stance"), "confidence": judgment.get("confidence"),
                    "headline": judgment.get("headline"), "generatedAt": judgment.get("generatedAt"),
                    "researchDay": judgment.get("researchDay"),
                    # 今日判断 written from an earlier debate is not the single-model benchmark.
                    "fromDebate": judgment.get("reason") == "debate"}
                   if judgment.get("status") == "ready" and judgment.get("researchDay") == research_day else None)
        entry = material.get("entry") or {"price": material.get("price"), "source": "quote" if material.get("price") else None}
        product_key = f"{material['exchange']}.{str(material['product']).lower()}"
        # One card per analyst: its area's gaps now, its streamed text while it writes, its checked report after.
        analysts = [{"key": item["key"], "title": item["title"], "status": "wait", "text": "", "conclusion": None, "sentences": [],
                     "points": [], "found": [], "searches": [],
                     "gaps": area_gaps(item, [fact for fact in listed if fact["sheet"] in item["sheets"]])} for item in ANALYSTS]
        return {
            "id": "db" + uuid.uuid4().hex[:12], "status": "queued", "symbol": material.get("mainSymbol") or material["symbol"],
            "productKey": product_key, "name": name,
            "researchDay": research_day,
            "day": now.astimezone(CHINA).date().isoformat(), "topicKey": topic_key, "factsHash": digest, "userId": user_id,
            "createdAt": _now_iso(now), "finishedAt": None, "seconds": None, "topic": topic,
            "sheets": sheets(listed), "facts": listed, "price": entry.get("price"), "entry": entry, "compare": compare,
            "steps": [{"key": "collect", "status": "done", "seconds": collect_seconds,
                       "detail": {"facts": len(listed), "event": event is not None}},
                      {"key": "analysts", "status": "wait"},
                      *({"key": f"turn{n}", "status": "wait", "side": side} for n, (side, _) in enumerate(TURNS, start=1)),
                      {"key": "judge", "status": "wait"}],
            "analysts": analysts, "lesson": self._lesson(product_key), "searches": 0,
            # One entry per turn once it starts: its streamed text while it runs, its checked sentences after.
            "turns": [],
            "leans": {}, "verdict": None, "review": False, "error": None,
            "models": dict(self.models), "fallbacks": [], "mapping": None, "calls": [], "tokens": 0, "settlement": None, "adopted": None,
        }

    def _lesson(self, key: str) -> dict[str, Any] | None:
        """上次复盘: the product's latest debate the reckoning settled within LESSON_DAYS (no new storage: the settled
        record holds the verdict, its sentences and the outcome)."""
        since = _now_iso(self.now() - timedelta(days=LESSON_DAYS))
        with self.lock:
            row = self.connection.execute(
                "SELECT payload_json FROM research_debates WHERE product_key = ? AND outcome = 'settled' AND created_at >= ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1", (key, since)).fetchone()
        return lesson_of(json.loads(row[0])) if row else None

    def _latest(self, key: str, topic_key: str, day: str) -> dict[str, Any] | None:
        for record in self._live.values():
            if record["productKey"] == key and record["topicKey"] == topic_key and record["researchDay"] == day:
                return record
        with self.lock:
            row = self.connection.execute(
                "SELECT payload_json FROM research_debates WHERE product_key = ? AND topic_key = ? AND research_day = ?"
                " AND status NOT IN ('failed', 'cancelled')"
                " ORDER BY status IN ('queued', 'running') DESC, created_at DESC, rowid DESC LIMIT 1", (key, topic_key, day)).fetchone()
        return json.loads(row[0]) if row else None

    def _cached(self, key: str, topic_key: str, digest: str, day: str) -> dict[str, Any] | None:
        """The same topic, product and facts within this research day (a weekend's unchanged facts are not
        yesterday's conclusion)."""
        with self.lock:
            row = self.connection.execute(
                "SELECT payload_json FROM research_debates WHERE product_key = ? AND topic_key = ? AND facts_hash = ?"
                " AND research_day = ? AND status IN ('done', 'review') ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (key, topic_key, digest, day)).fetchone()
        return json.loads(row[0]) if row else None

    def _insert(self, record: dict[str, Any]) -> bool:
        """A new run, inserted only while the day's count is under the cap — one statement, so two slots
        starting different topics at once cannot both take the last run. False when the cap is reached."""
        with self.lock, self.connection:
            return self.connection.execute(
                "INSERT INTO research_debates (id, product_key, symbol, topic_key, research_day, day, facts_hash, status, user_id,"
                " created_at, owner, heartbeat_at, payload_json) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                " WHERE (SELECT COUNT(*) FROM research_debates WHERE day = ?) < ?",
                (record["id"], record["productKey"], record["symbol"], record["topicKey"], record["researchDay"], record["day"],
                 record["factsHash"], record["status"], record.get("userId"), record["createdAt"], self.owner, _now_iso(self.now()),
                 json.dumps(record, ensure_ascii=False), record["day"], self.daily_runs)).rowcount == 1

    def _watch(self, debate_id: str, user_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT OR IGNORE INTO research_debate_watchers (debate_id, user_id) VALUES (?, ?)",
                                    (debate_id, user_id))

    def _reused(self, record: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """An existing run answers the request; a still-running one also notifies this requester when it ends."""
        if user_id and record["status"] in ACTIVE:
            self._watch(record["id"], user_id)
        return {**self.view(record), "reused": True}

    def _watchers(self, record: dict[str, Any]) -> list[str]:
        with self.lock:
            rows = self.connection.execute("SELECT user_id FROM research_debate_watchers WHERE debate_id = ?", (record["id"],)).fetchall()
        return list(dict.fromkeys([*([record["userId"]] if record.get("userId") else []), *(row[0] for row in rows)]))

    def _save(self, record: dict[str, Any]) -> None:
        if record["id"] in self._fenced:
            return  # reaped by another slot: this process writes nothing more for it
        settlement = record.get("settlement") or {}
        mine = record["id"] in self._live
        with self.lock, self.connection:
            written = self.connection.execute(
                "INSERT INTO research_debates (id, product_key, symbol, topic_key, research_day, day, facts_hash, status, user_id,"
                " created_at, settle_on, outcome, owner, heartbeat_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET status = excluded.status, settle_on = excluded.settle_on,"
                " outcome = excluded.outcome, heartbeat_at = COALESCE(excluded.heartbeat_at, heartbeat_at),"
                " payload_json = excluded.payload_json"
                # A running record is written only by its owner: once reaped (owner cleared) a resumed slot is fenced.
                " WHERE excluded.owner IS NULL OR research_debates.owner = excluded.owner",
                (record["id"], record["productKey"], record["symbol"], record["topicKey"], record["researchDay"], record["day"],
                 record["factsHash"], record["status"], record.get("userId"), record["createdAt"],
                 settlement.get("due") if settlement.get("status") == "pending" else None,
                 {"settled": "settled", "void": "void"}.get(str(settlement.get("status"))),
                 self.owner if mine else None, _now_iso(self.now()) if mine else None,
                 json.dumps(record, ensure_ascii=False))).rowcount
        if mine and not written:
            # Another slot reaped this run while this process stalled: stop it, write nothing more.
            log.warning("research_debate_fenced id=%s", record["id"])
            self._fenced.add(record["id"])
            task = self._tasks.get(record["id"])
            if task is not None and not task.done():
                task.cancel()

    async def _publish(self, record: dict[str, Any], kind: str = "progress", *, save: bool = True,
                       turn: dict[str, Any] | None = None, analyst: dict[str, Any] | None = None) -> None:
        """progress (saved; the windows re-read), done (saved; the reminder), delta (a turn's or an analyst's text so
        far, not saved: the live record in this process already holds it for a reload)."""
        if save:
            self._save(record)
        if record["id"] in self._fenced:
            # Another slot reaped this run and announced its failure; a late "研讨完成" from here would contradict
            # what is stored (#749).
            return
        users = self._watchers(record) if self.notify is not None else []
        if not users:
            return
        event: dict[str, Any] = {"kind": kind, "id": record["id"], "status": record["status"]}
        if turn is not None:
            event.update(turn=turn["n"], side=turn["side"], text=turn["text"])
        if analyst is not None:
            event.update(analyst=analyst["key"], text=analyst["text"])
        if kind == "done":
            verdict = record.get("verdict") or {}
            what = (f"{verdict.get('stance')} · 信心{verdict.get('confidence')}" if record["status"] == "done"
                    else "需复核" if record["status"] == "review" else str(record.get("error") or "没能完成"))
            event.update(title="研讨完成" if record["status"] in ("done", "review") else "研讨没能完成",
                         text=f"{record.get('name') or record['symbol']}：{what}",
                         symbol=str(record.get("symbol") or ""), name=str(record.get("name") or ""))
        for user in users:
            try:
                await self.notify(user, event)  # type: ignore[misc]
            except Exception:  # noqa: BLE001 — a lost nudge only delays the page's own re-read.
                log.debug("research_debate_notify_failed id=%s", record["id"], exc_info=True)

    def _step(self, record: dict[str, Any], key: str) -> dict[str, Any]:
        return next(step for step in record["steps"] if step["key"] == key)

    # -- running

    async def _run(self, record: dict[str, Any], generator: Any) -> None:
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.concurrency)
        started = self.clock()
        try:
            await self._publish(record)
            async with self._slots:
                record["status"] = "running"
                await self._debate(record, generator)
        except asyncio.CancelledError:
            record["status"], record["error"] = ("failed", "Runtime 重启或切换，这次研讨中断了") if self._closed else ("cancelled", "已取消")
            for step in record["steps"]:
                if step["status"] == "run":
                    step["status"] = "wait"
            raise
        except _CallFailed as error:
            record["status"] = "failed"
            tried = [switch["from"] for switch in record.get("fallbacks") or [] if switch["role"] == error.role]
            record["error"] = f"{ROLE_NAMES[error.role]}模型 {'、'.join([*tried, error.model])} {'都' if tried else ''}没有完成（{error.code}）"
            for step in record["steps"]:
                if step["status"] == "run":
                    step["status"] = "failed"
        except Exception as error:  # noqa: BLE001 — a failed debate is reported, never left running.
            log.exception("research_debate_failed id=%s", record["id"])
            record["status"], record["error"] = "failed", type(error).__name__
        finally:
            for turn in [*(record.get("turns") or []), *(record.get("analysts") or [])]:
                if turn["status"] == "run":
                    turn["status"] = "failed"  # cut off (stopped, failed, shut down): what was written stays, never checked
            record["seconds"] = round(self.clock() - started + float(self._step(record, "collect").get("seconds") or 0), 1)
            record["finishedAt"] = _now_iso(self.now())
            log.info("research_debate id=%s product=%s status=%s tokens=%d seconds=%.1f", record["id"], record["productKey"],
                     record["status"], record["tokens"], record["seconds"])
            if not self._closed:
                await asyncio.shield(self._publish(record, "done"))
            else:
                # Closing: this slot's windows are going away; the next live slot tells the waiters (_reap).
                record["pendingNotice"] = True
                self._save(record)

    async def _debate(self, record: dict[str, Any], generator: Any) -> None:
        # The four analysts first, at the same time; what they found on the web joins the facts.
        await self._analysts(record, generator)
        facts = record["facts"]
        by_id = {fact["id"]: fact for fact in facts}
        by_n = {int(fact["n"]): fact["id"] for fact in facts}
        material = self._material_text(record)
        lesson = record.get("lesson") or {}
        context = "".join(f"\n{part}" for part in (reports_text(record.get("analysts") or []),
                                                   f"上次复盘：{lesson['text']}" if lesson.get("text") else "") if part)
        # Four turns, one after another; each side sees the reports and the whole exchange so far.
        for n, (side, _) in enumerate(TURNS, start=1):
            await self._speak(record, generator, n, side, material, context, by_n, by_id)
        # The verdict: A and B only, the mapping random and recorded; only the checked sentences.
        judge = self._step(record, "judge")
        judge.update(status="run", startedAt=_now_iso(self.now()))
        judge_started = self.clock()
        await self._publish(record)
        a_side = "bull" if self.rng.random() < 0.5 else "bear"
        mapping = {"A": a_side, "B": "bear" if a_side == "bull" else "bull"}
        record["mapping"] = mapping
        letter = {side: name for name, side in mapping.items()}
        said = sentences_of(record)
        to_anon = {item_id: item_id.replace(SIDE_MARK[item["side"]], letter[item["side"]], 1) for item_id, item in said.items()}
        cases = {f"{name} 方": [{"id": to_anon[item["id"]], "text": plain(item["text"]), "fact_ids": item["factIds"]}
                                for item in said.values() if item["side"] == mapping[name] and item["verified"]]
                 for name in ("A", "B")}
        judge_input = material + "\n议题：" + str(record["topic"].get("question")) + context + "\n" + json.dumps(cases, ensure_ascii=False)
        try:
            verdict = await self._call(record, generator, "judge", "verdict", DebateVerdict, JUDGE_INSTRUCTIONS, judge_input)
        except _CallFailed as error:
            if error.code not in {"model_output_invalid", "model_output_empty"}:
                raise
            # 需复核 (43fc275): an unreadable verdict gets no default tier.
            record["status"], record["review"] = "review", True
            judge.update(status="done", seconds=round(self.clock() - judge_started, 1))
            record["settlement"] = self._freeze(record)
            return
        from_anon = {anon: real for real, anon in to_anon.items()}
        result = self._verdict(record, verdict, mapping, from_anon)
        judge.update(status="done", seconds=round(self.clock() - judge_started, 1))
        # Support is an adopted sentence with its own cited facts (an uncited one supports nothing that could be
        # written into 今日判断).
        anchored = [item for item in result["adopted"] if said[item["id"]]["factIds"]]
        if not anchored or (result["winner"] and not any(item["side"] == result["winner"] for item in anchored)):
            # No anchored sentence adopted at all (a draw included), or none for the winner: the tier would stand
            # on nothing — 需复核.
            record["status"], record["review"] = "review", True
            record["unsupportedVerdict"] = result
            record["settlement"] = self._freeze(record)
            return
        record["verdict"] = result
        record["status"] = "done"
        record["settlement"] = self._freeze(record)

    def _material_text(self, record: dict[str, Any]) -> str:
        """The shared 底稿 every request starts with (the same text: providers' prefix caches can hit)."""
        # `n`: the number a turn cites the fact by ([F12]), the same as the page's 资料来源.
        rows = [{"n": fact["n"], **{name: fact[name] for name in MODEL_FIELDS if fact.get(name)}} for fact in record["facts"]]
        head = {"品种": record.get("name"), "合约": record["symbol"], "底稿": rows}
        return "底稿：" + json.dumps(head, ensure_ascii=False)

    async def _analysts(self, record: dict[str, Any], generator: Any) -> None:
        """The four analysts at the same time; one that fails or runs out of time is 没写成, never the debate's end."""
        step = self._step(record, "analysts")
        step.update(status="run", startedAt=_now_iso(self.now()))
        started = self.clock()
        await self._publish(record)
        await asyncio.gather(*(self._analyst(record, generator, spec, report)
                               for spec, report in zip(ANALYSTS, record["analysts"], strict=True)))
        done = sum(1 for report in record["analysts"] if report["status"] == "done")
        step.update(status="done", seconds=round(self.clock() - started, 1),
                    detail={"done": done, "failed": len(record["analysts"]) - done, "searches": record.get("searches", 0)})
        await self._publish(record)

    def _area(self, record: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
        """An analyst's facts: its 底稿 (found web items included) and the main contract's price."""
        return [fact for fact in record["facts"] if fact.get("sheet") in spec["sheets"] or fact["id"] == "M.price"]

    async def _analyst(self, record: dict[str, Any], generator: Any, spec: dict[str, Any], report: dict[str, Any]) -> None:
        report.update(status="run", startedAt=_now_iso(self.now()), model=record["models"].get("analyst"))
        started, sent = self.clock(), [0.0]
        await self._publish(record)
        try:
            async with asyncio.timeout(ANALYST_TIMEOUT_SECONDS):
                await self._fill_gaps(record, generator, spec, report)

                async def streamed(text: str) -> None:
                    report["text"] = text
                    if self.clock() - sent[0] >= DELTA_SECONDS:
                        sent[0] = self.clock()
                        await self._publish(record, "delta", save=False, analyst=report)
                rows = [{"n": fact["n"], **{name: fact[name] for name in MODEL_FIELDS if fact.get(name)}} for fact in self._area(record, spec)]
                missing = [gap["label"] for gap in report["gaps"]]
                text = (f"品种：{record.get('name')}（{record['symbol']}）\n议题：{record['topic'].get('question')}\n资料："
                        + json.dumps(rows, ensure_ascii=False) + (f"\n这一块调研页缺：{'、'.join(missing)}" if missing else ""))
                reply = await self._call(record, generator, "analyst", f"analyst:{spec['key']}", None,
                                         ANALYST_INSTRUCTIONS.format(title=spec["title"], focus=spec["focus"]), text, on_text=streamed)
            by_id = {fact["id"]: fact for fact in record["facts"]}
            by_n = {int(fact["n"]): fact["id"] for fact in record["facts"]}
            report.update(status="done", text=str(reply).strip(), **check_report(spec, str(reply), by_n, by_id))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — 没写成: the card says so and the debate goes on without it.
            code = error.code if isinstance(error, _CallFailed) else type(error).__name__
            log.warning("research_debate_analyst_failed id=%s analyst=%s error=%s", record["id"], spec["key"], code)
            report.update(status="failed", error=code)
        report["seconds"] = round(self.clock() - started, 1)
        await self._publish(record)

    async def _fill_gaps(self, record: dict[str, Any], generator: Any, spec: dict[str, Any], report: dict[str, Any]) -> None:
        """Gap-only web search: the analyst picks a query per gap the program listed (never an item the desk holds),
        the program caps the count, runs them and keeps the results that carry figures as facts (W.…: source, date,
        link), so a report's numbers check against what the page actually said."""
        if not spec["search"] or not report["gaps"] or self.search is None or record.get("searches", 0) >= SEARCHES_PER_DEBATE:
            return
        held = "、".join(dict.fromkeys(str(fact.get("label")) for fact in self._area(record, spec)))
        since = (self.now().astimezone(CHINA).date() - timedelta(days=SEARCH_SINCE_DAYS)).isoformat()
        plan_input = (f"品种：{record.get('name')}（{record['symbol']}）\n今天：{self.now().astimezone(CHINA).date().isoformat()}\n"
                      f"缺口：{'、'.join(gap['label'] for gap in report['gaps'])}\n调研页已有（不要再搜）：{held or '无'}")
        try:
            plan = await self._call(record, generator, "analyst", f"search:{spec['key']}", SearchPlan,
                                    PLAN_INSTRUCTIONS.format(title=spec["title"]), plan_input)
        except _CallFailed:
            return  # no plan: the report is written from the desk's facts alone
        labels = {gap["label"] for gap in report["gaps"]}
        queries: list[tuple[str, str]] = []
        for item in plan.queries:
            gap, query = item.gap.strip(), " ".join(item.query.split())
            # Only a listed gap, one query each, within both caps (taken now: the analysts run at the same time).
            if gap not in labels or any(gap == taken for taken, _ in queries) or not query \
                    or len(queries) >= SEARCHES_PER_ANALYST or record.get("searches", 0) >= SEARCHES_PER_DEBATE:
                continue
            if "after:" not in query:
                query = f"{query[:QUERY_CHARS - 17]} after:{since}"
            record["searches"] = record.get("searches", 0) + 1
            queries.append((gap, query[:QUERY_CHARS]))
        results = await asyncio.gather(*(self.search.search(query, count=5) for _, query in queries), return_exceptions=True)
        for (gap, query), result in zip(queries, results, strict=True):
            entry: dict[str, Any] = {"gap": gap, "query": query, "found": 0}
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                entry["error"] = str(result) or type(result).__name__
                report["searches"].append(entry)
                continue
            for item in result.get("results") or []:
                if entry["found"] >= RESULTS_PER_SEARCH:
                    break
                title, content, link = str(item.get("title") or "").strip(), str(item.get("content") or "").strip(), str(item.get("link") or "")
                if not link or not NUMBER.search(f"{title}{content}"):
                    continue  # a gap is a figure: a page without one fills nothing
                fact_id = "W." + hashlib.sha1(link.encode()).hexdigest()[:8]
                if not any(fact["id"] == fact_id for fact in record["facts"]):
                    record["facts"].append({
                        "id": fact_id, "section": "web", "kind": "report", "label": title[:60] or "网页", "value": content[:240] or title[:60],
                        "source": str(item.get("media") or "").strip() or urlsplit(link).hostname or "网页",
                        "asOf": str(item.get("publishDate") or "").strip() or None, "url": link, "sheet": spec["sheets"][0],
                        "n": max(int(fact["n"]) for fact in record["facts"]) + 1, "found": {"analyst": spec["key"], "gap": gap}})
                if fact_id not in report["found"]:
                    report["found"].append(fact_id)
                    entry["found"] += 1
            report["searches"].append(entry)
        await self._publish(record)

    async def _speak(self, record: dict[str, Any], generator: Any, n: int, side: str, material: str, context: str,
                     by_n: dict[int, str], by_id: dict[str, dict[str, Any]]) -> None:
        """One turn: the text streams to the windows while it is written, then its checked sentences replace it."""
        step = self._step(record, f"turn{n}")
        step.update(status="run", startedAt=_now_iso(self.now()))
        before = list(record["turns"])
        # What was said so far, as each side saw it on the page (未核对 marked: the other side may call it out).
        exchange = "\n".join(f"{ROLE_NAMES[turn['side']]}：" + "".join(
            item["text"] + ("" if item["verified"] else "（未核对）") for item in turn["sentences"]) for turn in before)
        turn: dict[str, Any] = {"n": n, "side": side, "status": "run", "text": "", "sentences": [], "startedAt": step["startedAt"]}
        record["turns"].append(turn)
        await self._publish(record)
        started, sent = self.clock(), [0.0]

        async def streamed(text: str) -> None:
            turn["text"] = spoken(text)[0]
            if self.clock() - sent[0] >= DELTA_SECONDS:
                sent[0] = self.clock()
                await self._publish(record, "delta", save=False, turn=turn)
        text = material + "\n议题：" + str(record["topic"].get("question")) + context + (
            "\n交锋记录：\n" + exchange if exchange else "") + "\n" + TURN_TASK[n]
        reply = await self._call(record, generator, side, f"turn{n}", None, TURN_INSTRUCTIONS + "\n" + ROLE_LINE[side], text,
                                 on_text=streamed)
        words, lean = spoken(str(reply))
        first = 1 + sum(len(item["sentences"]) for item in before if item["side"] == side)
        turn.update(status="done", text=words, sentences=check_turn(side, words, first, by_n, by_id), model=record["models"][side],
                    seconds=round(self.clock() - started, 1))
        if lean:
            record["leans"][side] = lean
        step.update(status="done", seconds=turn["seconds"])
        await self._publish(record)

    async def _call(self, record: dict[str, Any], generator: Any, role: str, stage: str, schema: type[BaseModel] | None,
                    instructions: str, input_text: str, on_text: Callable[[str], Awaitable[None]] | None = None) -> Any:
        """One bounded model call for a role (a spoken turn streams plain text when `schema` is None); DeepSeek gets
        one more try (upstream #1489). A model that is unavailable (UNAVAILABLE_CODES, a timeout, no key) hands the
        role to its fallback for the rest of the debate: `models[role]` becomes the model that actually spoke, and
        `fallbacks` says why."""
        endpoints = getattr(generator, "endpoints", None)
        chain = self._chain(role, (record.get("models") or {}).get(role))
        for position, model in enumerate(chain):
            last = position + 1 == len(chain)
            if isinstance(endpoints, dict) and model not in endpoints and not last:
                self._switch(record, role, stage, model, chain[position + 1], "model_not_configured")
                continue
            try:
                return await self._call_model(record, generator, role, stage, schema, instructions, input_text, model, on_text)
            except _CallFailed as error:
                # A fallback without a key is no fallback: the primary's own failure is the one reported.
                if last or not error.unavailable or (isinstance(endpoints, dict) and chain[position + 1] not in endpoints):
                    raise
                self._switch(record, role, stage, model, chain[position + 1], error.code)
        raise _CallFailed(role, chain[-1], "model_request_failed")  # pragma: no cover — the loop returns or raises

    def _switch(self, record: dict[str, Any], role: str, stage: str, source: str, target: str, reason: str) -> None:
        record.setdefault("fallbacks", []).append({"role": role, "stage": stage, "from": source, "to": target, "reason": reason})
        record.setdefault("models", {})[role] = target
        log.warning("research_debate_fallback id=%s role=%s stage=%s from=%s to=%s reason=%s",
                    record.get("id"), role, stage, source, target, reason)

    async def _call_model(self, record: dict[str, Any], generator: Any, role: str, stage: str, schema: type[BaseModel] | None,
                          instructions: str, input_text: str, model: str,
                          on_text: Callable[[str], Awaitable[None]] | None = None) -> Any:
        # DeepSeek gets one more try (upstream #1489).
        attempts = 2 if model.startswith("deepseek") else 1
        # A stand-in's every call names the model it replaced and why.
        switch = next((item for item in reversed(record.get("fallbacks") or []) if item["role"] == role and item["to"] == model), None)
        stand_in = {"fallbackFrom": switch["from"], "fallbackReason": switch["reason"]} if switch else {}
        timeout, cap = {"judge": (JUDGE_TIMEOUT_SECONDS, JUDGE_MAX_OUTPUT_TOKENS),
                        "analyst": (ANALYST_CALL_TIMEOUT_SECONDS, ANALYST_MAX_OUTPUT_TOKENS)}.get(role, (SIDE_TIMEOUT_SECONDS, SIDE_MAX_OUTPUT_TOKENS))
        for attempt in range(attempts):
            started = self.clock()
            bounds = {"instructions": instructions, "input_text": input_text, "model": model, "usage_user": USAGE_USER,
                      "timeout_seconds": timeout, "max_output_tokens": cap, "light_reasoning": True}
            try:
                result = await (generator.stream_text(**bounds, on_text=on_text) if schema is None
                                else generator.complete_structured(**bounds, schema=schema))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — recorded, retried once for DeepSeek, then the model fails.
                code = str(error) if str(error) else type(error).__name__
                record["calls"].append({"role": role, "stage": stage, "model": model, "tokens": 0,
                                        "seconds": round(self.clock() - started, 1), "error": code, **stand_in})
                if attempt + 1 < attempts:
                    continue
                raise _CallFailed(role, model, code, unavailable=_unavailable(error, code)) from error
            tokens = int(getattr(result, "tokens", 0) or 0)
            record["tokens"] += tokens
            record["calls"].append({"role": role, "stage": stage, "model": model, "tokens": tokens,
                                    "seconds": round(self.clock() - started, 1),
                                    "output": result.value if schema is None else result.value.model_dump(), **stand_in})
            return result.value
        raise _CallFailed(role, model, "model_request_failed")  # pragma: no cover — the loop returns or raises

    def _verdict(self, record: dict[str, Any], value: DebateVerdict, mapping: dict[str, str],
                 from_anon: dict[str, str]) -> dict[str, Any]:
        facts = record["facts"]
        by_id = {fact["id"]: fact for fact in facts}
        winner = mapping.get(value.winner)
        stance = stance_of(winner, value.margin, value.insufficient)
        arguments = sentences_of(record)

        def line(text: str, limit: int, extra: tuple[float, ...] = ()) -> str:
            """A free-text line in real names (A/B and A3 only in the record), or empty when the check rejects it."""
            shown, checked = named(text, mapping, from_anon)
            return shown if len(shown) <= limit and not sentence_problem(checked, facts, limit, extra) else ""

        def picked(items: list[VerdictItem], taken: set[str]) -> list[dict[str, Any]]:
            out = []
            for item in items:
                real = from_anon.get(item.id.strip())
                if real is None or real in taken or not arguments[real]["verified"]:
                    continue  # unknown ids are dropped (TradingAgents-AShare's ledger)
                taken.add(real)
                out.append({"id": real, "side": side_supported(arguments[real]), "reason": line(item.reason, REASON_CHARS)})
            return out
        taken: set[str] = set()
        adopted, rejected = picked(value.adopted, taken), picked(value.rejected, taken)
        refuted = sum(1 for item in rejected if winner and item["side"] == winner)
        price = record.get("price")
        invalidation = value.invalidation
        dropped = invalidation_problem(invalidation, stance, price, facts, self.invalidation_max_percent)
        if dropped:
            invalidation = None
        extra = (invalidation,) if invalidation is not None else ()
        headline = line(value.headline, HEADLINE_CHARS, extra) or {"看不清": "证据不足，看不清", "震荡": "双方基本平衡，震荡"}.get(
            stance, f"{ROLE_NAMES[winner or 'bull']}{'明显胜出' if value.margin == '明显' else '略胜'}，{stance}")
        reason = line(value.reason, VERDICT_REASON_CHARS, extra)
        swing = line(value.swing, SWING_CHARS, extra)
        change = line(value.change_mind, CHANGE_CHARS, extra)
        watch = [text for text in (line(item, WATCH_CHARS, extra) for item in value.watch[:3]) if text]
        disagreements = [text for text in (line(item, DISAGREEMENT_CHARS, extra) for item in value.disagreements[:3]) if text]
        scenarios = [text for text in (line(item, SCENARIO_CHARS, extra) for item in value.scenarios[:3]) if text]
        # Confidence weighs the evidence behind every adopted argument (a caveat on a stale report lowers it too);
        # only the rejected-argument penalty is the winner's.
        winning = [arguments[item["id"]] for item in adopted]
        level, basis = confidence(stance, winner, value.margin, winning, refuted, by_id, record["sheets"])
        if dropped and value.invalidation is not None:
            # The judge's line was not a usable price (an RSI, a year, a stock count) or lay too far away.
            basis["invalidationDropped"] = {"value": value.invalidation, "reason": dropped,
                                            "maxPercent": self.invalidation_max_percent}
        return {"stance": stance, "winner": winner, "margin": value.margin if winner else None, "insufficient": value.insufficient,
                "headline": headline, "reason": reason, "adopted": adopted, "rejected": rejected, "swing": swing, "changeMind": change,
                "invalidation": invalidation, "watch": watch, "disagreements": disagreements, "scenarios": scenarios,
                "confidence": level, "confidenceBasis": basis}

    # -- reckoning

    def _due(self, trading_day: str, days: list[str]) -> str:
        if days:
            index = bisect_right(days, trading_day)
            if index + SETTLE_DAYS - 1 < len(days):
                return days[index + SETTLE_DAYS - 1]
        current, counted = date.fromisoformat(trading_day), 0
        while counted < SETTLE_DAYS:
            current += timedelta(days=1)
            if current.weekday() < 5:
                counted += 1
        return current.isoformat()

    def _freeze(self, record: dict[str, Any]) -> dict[str, Any]:
        """对账口径, fixed at the verdict: the trading day, the due day (five trading days on), the entry price,
        the 失效位, the verdict, each side's own lean and the single-model 今日判断 of the moment."""
        days = self.desk.trading_days()
        created = datetime.fromisoformat(record["createdAt"])
        entry = record.get("entry") or {"price": record.get("price"), "source": "quote"}
        # The five closes are read after the entry price's own trading day: the quote's trading day, or the day of
        # the close that stood in for it while the market was shut (a weekend debate is scored from Friday's close).
        trading_day = entry.get("day") or trading_day_of(int(created.timestamp() * 1e9), days or None)
        verdict = record.get("verdict") or {}
        compare = record.get("compare")
        settlement: dict[str, Any] = {"tradingDay": trading_day, "due": self._due(trading_day, days), "entryPrice": entry.get("price"),
                      "entrySource": entry.get("source"),
                      "invalidation": verdict.get("invalidation"), "stance": verdict.get("stance") if not record.get("review") else None,
                      "deskStance": None if not compare or compare.get("fromDebate") else compare.get("stance"),
                      "leans": dict(record.get("leans") or {}), "status": "pending",
                      "closes": [], "outcomes": {}}
        if not compare:
            settlement["deskMissing"] = True  # no 今日判断 of this research day when it began: 单模型 not counted
        if record.get("review"):
            # 需复核: no tier was given, so nobody's view in this run is counted (the leans and 今日判断 neither).
            settlement.update(status="void", reason="需复核，不计入对账", stance=None, deskStance=None, leans={})
        elif str(record.get("productKey") or "").startswith(f"{FOREIGN_RESEARCH}."):
            # 外盘（2026-10-21）：五个交易日后的收盘价按国内日历与 15:30 收盘取，套不到 CME 上；不打分，明说。
            settlement.update(status="void", reason=FOREIGN_SETTLEMENT_VOID)
        elif not entry.get("price"):
            settlement.update(status="void", reason=void_reason(str(entry.get("reason") or "")))
        return settlement

    def _closes(self, symbol: str, trading_day: str) -> list[float]:
        # Enough daily bars to reach back to the debate's own trading day, however late the settlement runs
        # (an outage or repeated read failures): never the first five of some later window.
        span = (self.now().astimezone(CHINA).date() - date.fromisoformat(trading_day)).days + 10
        history = self.desk.provider().market_history(symbol, 86400, max(30, min(span, 1000)))
        # Only closed bars (the desk's one rule: a forming bar's moving close never scores); the stored `due` is only
        # when to start looking — with no calendar it is a weekday estimate a holiday can put too early.
        bars = closed_closes([bar for bar in history.get("bars") or [] if isinstance(bar, dict)], self.now(), self.desk.trading_days())
        return [close for day, close in bars if day > trading_day][:SETTLE_DAYS]

    async def settle_due(self) -> int:
        """Settle every debate whose due day has closed: four outcomes from the same rule (judge, the bull
        and bear models' own lean, the single-model 今日判断); a contract without five closes long after the
        due day is void."""
        now = self.now().astimezone(CHINA)
        today = now.date().isoformat()
        with self.lock:
            rows = self.connection.execute("SELECT payload_json FROM research_debates WHERE outcome IS NULL"
                                           " AND settle_on IS NOT NULL AND settle_on <= ?", (today,)).fetchall()
        settled = 0
        for (payload,) in rows:
            record = json.loads(payload)
            settlement = record["settlement"]
            if settlement["due"] == today and now.time() < SETTLE_AFTER:
                continue
            try:
                closes = await asyncio.to_thread(self._closes, record["symbol"], settlement["tradingDay"])
            except (RuntimeError, ValueError, KeyError, TypeError) as error:
                log.warning("research_debate_settle_read_failed id=%s error=%s", record["id"], error)
                if (now.date() - date.fromisoformat(settlement["due"])).days > SETTLE_GIVE_UP_DAYS:
                    settlement.update(status="void", reason="合约收盘价读不到")  # delisted or unknown: give up
                    self._settle_write(record["id"], settlement)
                continue
            if len(closes) < SETTLE_DAYS:
                if (now.date() - date.fromisoformat(settlement["due"])).days > SETTLE_GIVE_UP_DAYS:
                    settlement.update(status="void", reason="收盘价不足五个交易日")
                    self._settle_write(record["id"], settlement)
                continue
            entry, line = settlement.get("entryPrice"), settlement.get("invalidation")
            leans = settlement.get("leans") or {}
            outcomes = {"judge": settle_outcome(settlement.get("stance"), entry, line, closes),
                        "bull": settle_outcome(leans.get("bull"), entry, None, closes),
                        "bear": settle_outcome(leans.get("bear"), entry, None, closes),
                        "desk": settle_outcome(settlement.get("deskStance"), entry, None, closes)}
            settlement.update(closes=closes, outcomes={key: value for key, value in outcomes.items() if value},
                              settledAt=_now_iso(self.now()),
                              status="settled" if any(outcomes.values()) else "void")
            settled += self._settle_write(record["id"], settlement)
        return settled

    def _settle_write(self, debate_id: str, settlement: dict[str, Any]) -> bool:
        """A settlement's terminal write — only while the row is still unsettled, and only its settlement field:
        two blue-green slots may settle the same row at once, and the first result stands (a slot whose history
        read failed can never turn another slot's `settled` into `void`). False when another write got there."""
        outcome = {"settled": "settled", "void": "void"}.get(str(settlement.get("status")))
        with self.lock, self.connection:
            return self.connection.execute(
                "UPDATE research_debates SET outcome = ?, settle_on = NULL, payload_json = json_set(payload_json, '$.settlement', json(?))"
                " WHERE id = ? AND outcome IS NULL", (outcome, json.dumps(settlement, ensure_ascii=False), debate_id)).rowcount == 1

    async def _settle_loop(self) -> None:
        while True:
            try:
                await self.settle_due()
            except Exception:  # noqa: BLE001 — one failed pass must not end the loop.
                log.exception("research_debate_settle_error")
            await asyncio.sleep(SETTLE_INTERVAL_SECONDS)

    # -- 写进今日判断 and cancel

    def adoption(self, debate_id: str) -> tuple[dict[str, Any], DeskJudgment]:
        """The debate's conclusion as a desk judgment: the winner's adopted arguments as points (both sides'
        under 震荡), the other side's as risks; the tier folded to the desk's four (略偏多 → 偏多, 看不清 → 不明)."""
        record = self._live.get(debate_id) or self._stored(debate_id)
        if record is None:
            raise LookupError("research_debate_unknown")
        verdict = record.get("verdict")
        if record["status"] != "done" or not verdict:
            raise ValueError("research_debate_not_done")
        if record["researchDay"] != self.desk.research_day(self.now()):
            raise ValueError("research_debate_not_today")
        arguments = sentences_of(record)
        by_id = {fact["id"]: fact for fact in record["facts"]}
        winner = verdict.get("winner")
        direction = {"bull": "偏多", "bear": "偏空"}

        def point(item_id: str) -> DeskPoint | None:
            item = arguments.get(item_id)
            if item is None or not item["factIds"] or item["factIds"][0] not in by_id:
                return None
            text = plain(item["text"])
            if len(text) > STATEMENT_CHARS:
                # A debate sentence may run to the turn's length; 今日判断 shows at most STATEMENT_CHARS: cut at the
                # last clause break that fits (a number is never split there), else at the limit.
                cut = max(text.rfind(mark, 0, STATEMENT_CHARS) for mark in "，；、")
                text = text[:cut if cut > 0 else STATEMENT_CHARS - 1].rstrip() + "…"
            return DeskPoint(text=text, basis=item["factIds"], quote=item.get("quote") or excerpt(by_id[item["factIds"][0]]),
                             direction=direction[side_supported(item)])
        points: list[DeskPoint] = []
        risks: list[DeskPoint] = []
        for item in verdict["adopted"]:
            found = point(item["id"])
            if found is not None:
                (points if not winner or item["side"] == winner else risks).append(found)
        cited = list(dict.fromkeys(item for found in (*points, *risks) for item in found.basis))
        value = DeskJudgment(stance=DESK_STANCE[verdict["stance"]], confidence=verdict["confidence"],  # type: ignore[arg-type]
                             headline=verdict["headline"], headlineBasis=cited, points=points[:4], risks=risks[:3],
                             conclusion=verdict.get("swing") and f"关键分歧：{verdict['swing']}" or "")
        return record, value

    def adopt(self, debate_id: str) -> dict[str, Any]:
        stored = self._live.get(debate_id) or self._stored(debate_id)
        if stored is not None and stored.get("adopted"):
            # Already written: a retry answers the first write and never writes this debate over a newer judgment.
            return {"debate": self.view(stored), "judgment": {"stance": stored["adopted"]["stance"], "generatedAt": stored["adopted"]["at"],
                                                             "researchDay": stored["researchDay"]}, "repeated": True}
        record, value = self.adoption(debate_id)
        verdict = record["verdict"]
        try:
            # The desk decides "written once" in the same transaction as the write; the record's marker below
            # only lets a later read answer without asking the desk.
            entry, repeated = self.desk.adopt(record["symbol"], value, record["facts"], model="多 AI 研讨",
                                              debate_id=record["id"], name=record.get("name"),
                                              extra={"debateStance": verdict["stance"]})
        except ValueError as error:
            if str(error) == "judgment_write_conflict":
                raise
            raise ValueError("research_debate_unanchored") from error
        record["adopted"] = {"at": entry["generatedAt"], "stance": entry["value"]["stance"]}
        # Only the marker field (never the whole record read earlier, which could put back an older state); the
        # desk already decided "once" in its own transaction, so two slots writing the same marker agree.
        with self.lock, self.connection:
            self.connection.execute("UPDATE research_debates SET payload_json = json_set(payload_json, '$.adopted', json(?))"
                                    " WHERE id = ?", (json.dumps(record["adopted"], ensure_ascii=False), record["id"]))
        if record["id"] in self._live:
            self._live[record["id"]]["adopted"] = record["adopted"]
        answer = {"debate": self.view(record), "judgment": {"stance": entry["value"]["stance"],
                                                            "generatedAt": entry["generatedAt"], "researchDay": entry["researchDay"]}}
        return {**answer, "repeated": True} if repeated else answer

    def _stored(self, debate_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute("SELECT payload_json FROM research_debates WHERE id = ?", (debate_id,)).fetchone()
        return json.loads(row[0]) if row else None

    async def cancel(self, debate_id: str, user_id: str | None = None) -> dict[str, Any]:
        """Stop a running debate — only its starter may (others who joined it share the result and its cost)."""
        task = self._tasks.get(debate_id)
        record = self._live.get(debate_id)
        owner = (record or self._stored(debate_id) or {}).get("userId")
        if user_id is not None and owner and owner != user_id:
            raise PermissionError("research_debate_not_yours")
        if task is None or record is None:
            found = self.get(debate_id)
            if found is None:
                raise LookupError("research_debate_unknown")
            if found["status"] in ACTIVE:
                # Another slot runs it: ask it to stop (its heartbeat picks this up within one beat).
                with self.lock, self.connection:
                    self.connection.execute("UPDATE research_debates SET cancel_requested = 1 WHERE id = ?"
                                            " AND status IN ('queued', 'running')", (debate_id,))
                return {**found, "cancelRequested": True}
            return found
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return self.view(record)

    # -- lifecycle

    def start_beat(self) -> None:
        """Every process: the heartbeat of the debates this process runs (they are started by its readers)."""
        if self._beat is None:
            self._beat = asyncio.create_task(self._beat_loop())

    def start_settle(self) -> None:
        """The settlement loop (market reads for due debates): only the instance-lease holder runs it
        (wiring/producers.py); the `outcome IS NULL` update stays the second guard."""
        if self._loop is None or self._loop.done():
            self._loop = asyncio.create_task(self._settle_loop())

    async def stop_settle(self) -> None:
        loop, self._loop = self._loop, None
        if loop is not None:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        tasks = [*self._tasks.values(), *([self._loop] if self._loop else []), *([self._beat] if self._beat else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
