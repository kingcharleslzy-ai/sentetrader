"""Runtime-native model loop over the OpenAI Responses wire.

One adapter serves every configured model; each model has its own client,
base URL and key. Tools are the in-process `TraderReadOnlyTools` functions.
Conversation history is kept by the Runtime (rebuilt from persisted events
after a restart); no provider-side state such as `previous_response_id`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import openai
from fastapi import HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel

from . import __version__, exchange_calendar
from .answer_cards import (
    ANSWER_CARD_DEFINITION,
    ANSWER_CARD_INSTRUCTION,
    ANSWER_CARD_RULE,
    ANSWER_CARD_TOOL,
    AUTO_DATA_TOOLS,
    TurnCards,
    answer_card_wanted,
    conclusion_text,
    has_numeric_claim,
    tool_data,
)
from .app_help import skill_catalog_line, skill_mount_words
from .autonomy import AutonomousSimulationService
from .backtests import BacktestService
from .broker import EventBroker
from .conversation_cards import now_iso, ui_action
from .daily_budget import DailyCallBudget
from .data_providers import FakeReadOnlyDataProvider, ReadOnlyDataProvider
from .message_context import (
    OPINION_INSTRUCTION,
    ROUTINE_DEFINE_INSTRUCTION,
    DecodedInstruction,
    account_state_request,
    decode_instruction,
    routine_define_request,
    routine_edit_instruction,
    routine_edit_target,
    screen_reference,
    screen_snapshot_instruction,
    strategy_signal_request,
)
from .model_provider import (
    DEEPSEEK_BASE_URL,
    KIMI_BASE_URL,
    KIMI_MODEL,
    MODEL_CATALOG,
    ZHIPU_BASE_URL,
    canonical_model_slug,
    validate_model_url,
)
from .order_intent import OrderIntent
from .order_intent_parse import ParseContext, commands_start, parse
from .order_planner import Ask, Cards, Outcome, Refuse, ask_choices, catalog_names, resolve_words, side_unsettled
from .proposals import ProposalService
from .research_brief import ResearchBriefService
from .research_live import ResearchLiveService
from .routines import GONE_LINE as ROUTINE_GONE_LINE
from .routines import NEVER_IN_ROUTINE as ROUTINE_WITHHELD
from .routines import REFUSED_CALL as ROUTINE_REFUSED_CALL
from .routines import STEP_KINDS as ROUTINE_STEP_KINDS
from .routines import call_name as routine_call_name
from .routines import run_card as routine_run_card
from .routines import run_summary as routine_run_summary
from .routines import step_instruction as routine_step_instruction
from .routines import step_manner as routine_step_manner
from .routines import step_operations as routine_step_operations
from .routines import step_outcome as routine_step_outcome
from .routines import step_tools as routine_step_tools
from .runtime_presentation import describe_runtime
from .secret_values import secret_value
from .skill_registry import (
    all_skills,
    allowed_skills,
    hidden_tool_names,
    parent_block_reason,
    read_skill_resource,
    skill_allowed,
    skill_block_reason,
    skill_by_id,
    skill_content_block,
    tool_owner_map,
    unavailable_line,
)
from .store import RuntimeStore, utc_now
from .trader_tools import (
    ACCOUNT_STATE_WRITE_TOOLS,
    DECISION_CONTEXT_UNAVAILABLE,
    TraderReadOnlyTools,
    card_events,
    card_skill_block,
)
from .trading_calendar import previous_trading_day, trading_day_at
from .turn_router import (
    CARD_TOOL,
    DIRECT_FILL_TOOLS,
    GENERAL_ROUTE,
    ORDER_ROUTE,
    ORDER_WRITE_TOOLS,
    UI_FALLBACK_TOOLS,
    TurnRoute,
    auto_strategy_start,
    catalog_product_names,
    classify_turn,
    fills_directly,
    order_write,
    run_editor_ask,
    run_window_intent,
    strip_filler_prefix,
)
from .ui_control import UI_AGENT_OPERATIONS, describe_ui_change, ui_command_index
from .user_preferences import preference_instruction
from .voice_front import card_line, other_legs_line, refusal_line
from .voice_intents import Intent, match_product_word, product_words, resolve_symbol, routine_request
from .web_search import WebSearchClient, WebSearchError
from .wiring.accounts import AccountNotAssembled, merge_paper_account_status, session_account_status_payload
from .workspace_policy import WorkspacePolicy

if TYPE_CHECKING:
    from .wiring.accounts import AccountRegistry

log = logging.getLogger("uvicorn.error.agent")

PROVIDER_NAMES = {"deepseek": "DeepSeek", "zhipu": "智谱", "moonshot": "Kimi", "chatgpt": "ChatGPT"}


def _voice_temperature() -> float | None:
    raw = os.getenv("AI_TRADER_VOICE_TEMPERATURE", "").strip()
    try:
        return max(0.0, min(2.0, float(raw))) if raw else None
    except ValueError:
        return None


VOICE_TEMPERATURE = _voice_temperature()
# The only provider differences the loop knows about. Everything else is the
# plain Responses wire. A provider that rejects a reasoning summary request
# is downgraded once and remembered for the process lifetime.
# `tool_choice` (2026-10-04, verified against each provider's Responses endpoint): how a forced
# tool round is requested. DeepSeek honours "required" only outside thinking mode — the forced
# turn runs every round at reasoning effort "none" (a later thinking round would 400 without the
# reasoning text of the first); GLM accepts the field but ignores it (answers in prose), so it is
# not sent there. Whatever a provider
# does, a forced round that still returns no call gets one retry and then the Runtime's own
# "没能生成方案卡" card — never the model's prose standing in for a card.
# `light_effort`: the lightest reasoning effort the provider takes, for form-filling structured calls
# (`light_reasoning`): DeepSeek "none" (non-thinking), Kimi "low" (2026-10-12: it rejects minimal/none
# with a 400, and dropping the field falls back to its default "max").
PROVIDER_TRAITS: dict[str, dict[str, Any]] = {
    "deepseek": {"reasoning": True, "reasoning_summary": False, "max_output_tokens": None,
                 "tool_choice": "required_without_thinking", "light_effort": "none"},
    "zhipu": {"reasoning": True, "reasoning_summary": False, "max_output_tokens": None, "tool_choice": None,
              "light_effort": "minimal"},
    "moonshot": {"reasoning": True, "reasoning_summary": False, "max_output_tokens": None, "tool_choice": None,
                 "light_effort": "low"},
}
# The retry of a forced order round that came back as prose, and the Runtime's own end when it still does (#952
# Codex: the trading AI's order round is card-required again).
# The AI's answer asked to start a strategy (A22): the question clause is a plain start request, nothing else —
# [那/现在…][要/要不要/是否/需要…][用… / 把…] + one of the parser's start verbs (order_intent_parse._START) + [the
# strategy / 它 / 这个策略][运行/监控] + 吗/？. A question that only mentions starting (“想看看需要满足什么条件才能启动吗？”)
# or a status (“还没有启动，要查看详情吗？”) is no request; anything outside this form needs the user's own start command.
_START_VERBS = r"(?:启动|上线|开启|启用|恢复|开始(?:监控|运行|跑)|运行起来|跑起来|开跑|改成自动下单|自动(?:下单|执行|交易))"
_START_LEAD = r"^(?:那么?|好的?|现在|就)?(?:我们)?(?:要不要|要|是否|需不需要|需要|可以|现在就)?(?:我)?(?:帮你|给你)?"
_ASK_SENTENCE_END = re.compile(r"[。！!？?\n]")
def _plain(text: str) -> str:
    """Text without Markdown emphasis marks and spaces — the answer and the strategy names alike (RB_均线 → RB均线)."""
    return re.sub(r"[*_`\s]+", "", text)


def _sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence, the closing mark included (the question mark ends the asking sentence)."""
    spans, start = [], 0
    for mark in _ASK_SENTENCE_END.finditer(text):
        spans.append((start, mark.start()))
        start = mark.end()
    if start < len(text):
        spans.append((start, len(text) - 1))
    return spans


FORCED_RETRY_LINE = "（系统）这一轮必须调用工具：用 propose_order 把这句话填成下单意图（缺的留空，Runtime 会追问），不要只回文字。"
NO_CARD_LINE = "这次没能生成方案卡，可以换个说法再说一次，或打开下单台自己下。"
# 收尾轮防线 (#721)：轮数用完后 tool_choice="none" 的收尾轮里，DeepSeek 会把没能发出的工具调用按
# 自己的 DSML 标记写进正文。这段原始标记不是给用户看的答案，换成固定的如实一句。
TOOL_CALL_MARKUP_MARKERS = ("｜DSML｜", "<tool_call>", "<function_calls>", "<invoke name=")
TOOL_MARKUP_CLOSING_LINE = "没有生成卡片，这次没办成，稍后再试一次。"
# 思考不进正文（2026-10-13 夜盘 P1）：供应商的思考走独立通道（DeepSeek 的 reasoning_text / reasoning 项，
# _consume 不收），但模型有时把自言自语直接写进正文——“Wait — 用户此前设的止损是 3076… 不要赘述。保持简短。
# 重新组织：…”。正文出口按这些标记识别：<think> 块整段去掉；其余标记从所在句起截断，流式不再外发，
# 收尾时让模型禁工具重写一轮；重写仍带标记就只留标记前的话，空了用固定一句。
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.S)
_THINKING_MARKERS = re.compile(
    r"<think>"
    # English self-talk openers only — not ordinary transitions ("Actually, your order is still working").
    r"|(?:^|(?<=[\n。！？.!?:：—]))[ \t]*(?:Wait|Hmm+|Let me (?:think|re-?check|reconsider|re-?read)|Hold on|But wait)\b"
    # Chinese self-talk only where a sentence starts: the same words inside an ordinary explanation
    # ("如果用户之前设置了止损…", "回复偏好是保持简短") are an answer, not thinking.
    r"|(?:^|(?<=[\n。！？!?—]))[ \t]*(?:等等[，,—:：]|重新组织[：:，,]|重新整理一下(?:回复|回答)|不要赘述[。，,]|保持简短[。，,]"
    r"|就精简说明|我不该编|让我(?:再)?想想|我再想想"
    r"|用户(?:此前|之前|刚才|刚刚|上一条|前面说|的意思是|想要的是|要我|让我|说的是|问的是|设的))"
)
_SENTENCE_END = "\n。！？!?"
THINKING_REWRITE_LINE = ("（系统）你上一版回复把思考过程写进了正文，不能给用户看。只输出直接对用户说的最终回复：按本轮工具结果陈述，"
                         "不要写自我纠正、计划或“用户……”这类第三人称说法。")
THINKING_FALLBACK_LINE = "这次回复没整理好，结果以上方卡片和持仓面板为准，可以再问一次。"
THINKING_PLACEHOLDER = "正在整理回复…"


def wire_schema(value: Any, *, properties: bool = False) -> Any:
    """A tool's parameter schema as sent to the model (2026-10-14 token 瘦身): pydantic's generated
    `title` on every node ("Symbol" for symbol) carries nothing the model uses and is dropped; property
    names, descriptions, types and constraints stay exactly as validated."""
    if isinstance(value, dict):
        return {key: wire_schema(child, properties=key in {"properties", "$defs"} and not properties)
                for key, child in value.items() if properties or not (key == "title" and isinstance(child, str))}
    if isinstance(value, list):
        return [wire_schema(child) for child in value]
    return value


def thinking_leak_at(text: str) -> int | None:
    """Where the model's own thinking starts inside its answer text (the start of the sentence that
    carries the first marker), or None when the text is all answer. Closed <think> blocks are not
    counted here: visible_answer drops them whole."""
    match = _THINKING_MARKERS.search(text)
    if match is None:
        return None
    start = match.start()
    if not text.startswith("<think>", start):
        return start  # every other marker is anchored where its sentence starts (its own delimiters)
    while start > 0 and text[start - 1] not in _SENTENCE_END:
        start -= 1
    return start


def visible_answer(text: str) -> tuple[str, bool]:
    """(the part of an answer a user may see, whether thinking was cut from it). Closed <think>
    blocks go silently — unless nothing is left after them, which is a leak (no answer was written);
    any other thinking marker cuts the text at its sentence."""
    stripped = _THINK_BLOCK.sub("", text)
    if stripped != text and not stripped.strip():
        return "", True
    text = stripped
    leak = thinking_leak_at(text)
    if leak is None:
        return text, False
    return text[:leak].rstrip(), True
TOOL_LABELS = {
    "get_market_snapshot": "读取行情", "get_market_workbench": "读取图表指标",
    "get_market_analysis": "读取行情摘要", "get_futures_decision_context": "读取决策上下文",
    "get_account_summary": "读取账户", "get_positions": "读取持仓",
    "get_account_activity": "读取委托与成交", "get_trade_journal": "读取交易日志", "get_review_picks": "挑选复盘交易", "set_trade_note": "记下复盘补充",
    "get_daily_review": "读取今日复盘", "regenerate_daily_review": "重新生成复盘", "answer_review_observation": "回答复盘观察", "web_search": "查询公开信息",
    "remember": "记下这条", "forget": "删掉记忆", "recall_memory": "读取记忆",
    "propose_routine": "拆成流程卡", "list_routines": "读取我的流程", "run_routine": "运行流程",
    "run_reproducible_backtest": "执行回测", "run_tqsdk_historical_backtest": "执行历史回测", "run_strategy_backtest": "规则回测",
    "list_strategy_templates": "读取策略模板", "preview_strategy_template": "模板回测", "create_strategy_from_template": "从模板创建策略",
    "create_strategy_lab": "开策略实验室", "get_strategy_lab": "读实验室结果", "choose_strategy_lab_variant": "存为我的策略",
    "search_instruments": "搜索合约", "app_capabilities": "读取操作手册", "app_call": "调用软件功能",
    "get_runtime_data_status": "读取运行状态", "get_market_board": "读取报价总表",
    "reject_proposal": "作废待确认卡", ANSWER_CARD_TOOL: "整理回答卡",
}
# Shown in the conversation while the model works; chat_send filters it out of the final reply.
PROCESSING_PLACEHOLDER = "正在处理这个请求…"
# Tools whose successful result is a live fact an answer can rest on. Reading the operation manual
# (app_capabilities / app_call), reading the account's preferences, drawing on the chart or a call that
# failed proves nothing about the market or the account, and none of them satisfy the freshness gate.
LIVE_FACT_TOOLS = frozenset({
    "get_market_snapshot", "get_market_workbench", "get_market_analysis", "get_chart_bars",
    "get_market_board", "get_futures_decision_context", "get_account_summary", "get_positions",
    "get_account_activity", "get_trade_journal", "get_review_picks", "get_daily_review", "web_search",
})
# Evidence about the account (#872): once the turn's latest account read failed, these no longer verify it.
ACCOUNT_EVIDENCE_TOOLS = frozenset({"account_state", "get_account_summary", "get_positions", "get_account_activity",
                                    *ACCOUNT_STATE_WRITE_TOOLS})
MAX_TOOL_ROUNDS = 12
# 回答卡片化 (2026-10-14): reads that put no fact of their own in the answer (finding the contract, the agent's own
# capability list and skill manuals, drawing) — they neither make a card nor keep the answer text long. The data
# status and the user's preferences are facts a user asks about: a mixed question keeps them in its text (#878 review).
CARD_NEUTRAL_TOOLS = frozenset({"search_instruments", "app_capabilities", "annotate_chart", "highlight_ui",
                                "activate_skill", "read_skill_resource"})
# The read cards and the answer card (event names): with only these the text may keep just its conclusion.
READ_CARD_EVENTS = frozenset({"card.market", "card.account", "card.data", "card.answer"})
# 同一回合不重读 (回答卡去重跟进, 2026-10-14): market and news reads whose result is already in this turn's
# history. The same call again (same tool, same arguments) gets a one-line pointer instead of the whole result
# a second time; account reads — and the decision context, which carries the account — are not on the list (a
# write in the same turn changes them, #886 review).
ONCE_PER_TURN_READS = frozenset({"get_market_snapshot", "get_market_analysis", "get_chart_bars", "get_market_board",
                                 "get_news_flash", "get_research_brief", "get_foreign_markets"})
REPEATED_READ_OUTPUT = json.dumps({
    "success": True, "repeated": True,
    "userFacing": "这份数据本轮已经读过（同样的参数），结果就在前面的工具输出里，直接用，不要再读。"}, ensure_ascii=False)
# The UI round's budget (AI audit 2026-10-13): "看今天成交，只看AI下的" is three commands (panel, tab, filter);
# three rounds of one call each left it at the first step. Six rounds fit a three-step request with a retry.
UI_FALLBACK_ROUNDS = 6
HISTORY_TOKEN_BUDGET = 60_000
# 按技能延迟挂载 (2026-10-14 token 瘦身): a skill mounted for a session (its deferred tools and its manual)
# stays mounted this many turns after the last turn that needed it, then leaves the requests again.
SKILL_MOUNT_IDLE_TURNS = 2
# 语音委派短上下文 (2026-10-14): a turn delegated from the real-time call sends only the last few earlier
# turns (the front desk's own answers included, appended by append_voice_turn) plus the account's
# positions line as the current state, not the whole typed conversation.
VOICE_HISTORY_TURNS = 3
# A finished turn's tool outputs ride the next turn this long at most (one-turn lookback for follow-ups).
CARRIED_TOOL_OUTPUT_CHARS = 4000
CARRIED_TOOL_OUTPUT_CUT = "…（上一轮的工具结果到此截断；需要细节就重新读取）"
# The skills an object-level AI turn ("AI ▾" on a strategy, an instance or an order) mounts: the tools its
# focus instruction (message_context.focus_instruction) tells the model to use.
FOCUS_SKILLS: dict[str, tuple[str, ...]] = {
    "strategy": ("strategy-management", "strategy-backtests"),
    "strategy_edit": ("strategy-management", "strategy-backtests"),
    "strategy_instance": ("strategy-management", "strategy-signals"),
    "order": ("order-ops",),
}
TURN_USAGE_KEYS = ("requests", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")
SUMMARY_TIMEOUT_SECONDS = 30.0
STREAM_COALESCE_SECONDS = 0.15
STREAM_PERSIST_SECONDS = 5.0


class NativeAgentError(RuntimeError):
    """Turn-scoped failure; `str(error)` is the stable error code."""


_LIVE_FACT_WORDS = re.compile(
    r"(最新价|现价|收盘|开盘|报价|涨跌|涨了|跌了|涨幅|跌幅|成交量|持仓量|基差|主力|新闻|消息|公告|"
    r"持仓|权益|盈亏|保证金|可用资金|委托|成交)"
)
_LIVE_FACT_NUMBER = re.compile(r"\d[\d,.]*\s*(元|点|%|％|手|吨|美元|万|亿)|\d{1,2}月\d{1,2}日")


def first_paragraph(content: str) -> str:
    """The spoken part of an answer: everything before the first blank line, table or code block."""
    lines: list[str] = []
    for raw in content.split("\n"):
        line = raw.strip()
        if lines and (not line or line.startswith("|") or line.startswith("```")):
            break
        if line.startswith("```"):
            break
        if line:
            lines.append(raw)
    return "\n".join(lines)


def asserts_live_facts(text: str | None) -> bool:
    """Whether a reply states market, news or account facts that only a tool could have supplied."""
    if not text:
        return False
    return _LIVE_FACT_WORDS.search(text) is not None and _LIVE_FACT_NUMBER.search(text) is not None


def extract_json_object(text: str) -> str:
    """Return the first JSON object in a model reply, tolerating code fences and prose."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped[4:] if stripped.lower().startswith("json") else stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start:end + 1] if 0 <= start < end else stripped


@dataclass(frozen=True)
class ModelEndpoint:
    slug: str
    provider: str
    base_url: str
    api_key: str = field(repr=False)
    # The provider's model id when deployment configuration overrides the catalog slug.
    upstream: str | None = None


def build_model_endpoints(
    *, default_provider: str, deepseek_api_key: str | None = None,
) -> dict[str, ModelEndpoint]:
    """Every catalog model whose key is present gets an endpoint; keys never leave here."""
    endpoints: dict[str, ModelEndpoint] = {}
    for spec in MODEL_CATALOG:
        key = (deepseek_api_key if spec.provider == "deepseek" and deepseek_api_key is not None
               else secret_value(spec.api_key_env))
        if not key:
            continue
        if spec.provider == "deepseek":
            base_url = DEEPSEEK_BASE_URL
        elif spec.provider == "zhipu":
            base_url = ZHIPU_BASE_URL
        elif spec.provider == "moonshot":
            # The key only goes to HTTPS (or a local HTTP) endpoints.
            base_url = validate_model_url(os.getenv("AI_TRADER_KIMI_BASE_URL", "").strip() or KIMI_BASE_URL)
            upstream = os.getenv("AI_TRADER_KIMI_MODEL", "").strip() or KIMI_MODEL
            endpoints[spec.slug] = ModelEndpoint(spec.slug, spec.provider, base_url, key, upstream)
            continue
        else:
            continue
        endpoints[spec.slug] = ModelEndpoint(spec.slug, spec.provider, base_url, key)
    return endpoints


def default_model_slug(endpoints: dict[str, ModelEndpoint], provider: str) -> str | None:
    configured = canonical_model_slug(os.getenv("AI_TRADER_AGENT_MODEL", ""))
    if configured and configured in endpoints:
        return configured
    # Platform-only models (the debate fallback) never become the conversation default.
    platform = {spec.slug for spec in MODEL_CATALOG if spec.tier == "platform"}
    for endpoint in endpoints.values():
        if endpoint.provider == provider and endpoint.slug not in platform:
            return endpoint.slug
    return next((slug for slug in endpoints if slug not in platform), None)


@dataclass
class _ToolCall:
    item_id: str
    call_id: str
    name: str
    arguments: str = ""


@dataclass
class _Round:
    text: str = ""
    calls: list[_ToolCall] = field(default_factory=list)
    # Reasoning output items, passed back verbatim on the next round: DeepSeek's
    # thinking mode rejects a follow-up that omits them.
    reasoning_items: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    usage_tokens: int | None = None
    # The provider's usage split for this request (input / output / cached / reasoning tokens).
    usage: dict[str, int] = field(default_factory=dict)
    incomplete: bool = False
    completed: bool = False


@dataclass
class _RoutineRun:
    """我的流程 (2026-10-06): the routine a turn is running and the step in progress. `allowed` is the
    step's own tools (the only ones `_execute_tool` will run), `declared` what the request lists (those
    plus any tool an earlier step of this turn already called, so the history's items stay declared)."""
    routine: dict[str, Any]
    index: int = 0
    allowed: set[str] = field(default_factory=set)
    declared: set[str] = field(default_factory=set)
    app_operations: frozenset[str] = frozenset()
    # This step's calls: (tool, ok, made a proposal card).
    calls: list[tuple[str, bool, bool]] = field(default_factory=list)
    used: set[str] = field(default_factory=set)


@dataclass
class _Turn:
    session_id: str
    run_id: str
    turn_id: str
    mode: str
    gated: bool
    task: dict[str, Any]
    web_search_used: bool = False
    # What this turn actually read: one record per successful live-fact tool call
    # ({tool, ok, symbol/account, capturedAt/ageSeconds}). A refused call, the operation manual and
    # the preference document are not evidence, so they no longer suppress the freshness footnote.
    evidence: list[dict[str, Any]] = field(default_factory=list)
    # 账户现状的末次读取 (#872): every read of the positions / working orders / protections this turn, in
    # order ({source, available, asOf, write}). The latest one backs — or forbids — stating the account
    # now; `evidence` keeps the history for the audit log.
    account_reads: list[dict[str, Any]] = field(default_factory=list)
    market_unavailable: bool = False
    model_text: str = ""
    history_mark: int = 0
    # Endpoint slug the turn runs on; reported in the task card so agents can confirm the model from events.
    model: str | None = None
    # "voice" turns come from the real-time call: same session, plus a spoken summary of the answer.
    origin: str | None = None
    # The voice fast path: a simple read-only question, answered with minimal reasoning and read tools only.
    fast: bool = False
    # The turn's deterministic route (None = general): a fixed tool subset, temperature 0 and minimal
    # reasoning; `one_round` caps the tool loop so the card or the answer comes in one round.
    route: TurnRoute | None = None
    # 对象级 AI (2026-09-25): the object an "AI ▾" menu asked about; such a turn never places an order
    # itself (execute_simulation_trade is withheld), trade suggestions stay cards.
    focus: dict[str, str] | None = None
    # A card tool in a one_round route refused (errorCode + userFacing), or the route's
    # search_instruments found nothing: that line is the answer, published without another model round.
    route_refused: bool = False
    # Card-tool refusal codes seen this turn (a general turn): the same refusal twice means the model
    # is retrying a fact ("没有可平的持仓") — the refusal line ends the turn (AI audit 2026-10-13).
    card_refusals: set[str] = field(default_factory=set)
    # Consecutive search_instruments calls that returned zero items (the empty-search cutoff counts them).
    zero_result_searches: int = 0
    # The screen's draft of this round was replaced by the placeholder (thinking showed up in it).
    thinking_retracted: bool = False
    # Tools bound to the trading account the turn's session acts on (set in handle_message).
    tools: TraderReadOnlyTools | None = None
    # The planner's question published this turn (propose_order came back asking: its question card is the
    # turn's answer).
    asked: str | None = None
    # A forced turn on a provider that forces only outside thinking mode runs every round at this effort.
    reasoning_override: str | None = None
    # propose_order came back with a card, a refusal or a question: the order round's structured end.
    structured_done: bool = False
    # The user's own words.
    user_text: str = ""
    # The window the turn was asked from: propose_order's intent naming no contract means its contract.
    window_id: str | None = None
    # Who caused the turn (owner 2026-10-16): every turn this agent runs comes from the user's own message — typed, a
    # voice delegate, chat:send, a routine or backtest the user ran — so it never fills an order directly (no
    # execute_simulation_trade, no confirming a card for the user, no silent start of an auto strategy); its orders
    # are cards the user confirms. AI-initiated execution (a strategy's automatic orders, spread execution) calls the
    # execution service itself and never runs through a model turn; this stays False until such a turn exists.
    ai_initiated: bool = False
    # UI receipts confirm individual steps; they do not end a multi-step request.
    ui_tool_called: bool = False
    # An interface write (a UI command or highlight_ui) was sent or refused this turn; a UI round
    # without one is re-run as a general turn instead of answering.
    ui_attempted: bool = False
    ui_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    ui_error: str | None = None
    # The latest write was refused before it was queued (nothing ran): why, for the closing line.
    ui_not_run: str | None = None
    # The step the window refused (its change in the user's words), for the closing line.
    ui_refused: str | None = None
    # 我的流程: set while the turn runs a routine (step-scoped tools, no direct order tool at all).
    routine: _RoutineRun | None = None
    # Token 计量 (2026-10-14): this turn's model requests and their usage split, summed per request;
    # the first visible answer text, in ms after the turn started.
    usage: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TURN_USAGE_KEYS, 0))
    started_at: float = field(default_factory=monotonic)
    first_text_ms: int | None = None
    # The UI round attempted no write and the sentence went on as a general turn.
    escalated: bool = False
    # 回答卡片化 (2026-10-14): the turn's automatic read cards and what it read (TurnCards); whether the
    # answer card is offered; what the published answer showed (cards) and stated (figures) — the stats.
    cards: TurnCards | None = None
    answer_wanted: bool = False
    # The ONCE_PER_TURN_READS calls this turn already read successfully (tool + its arguments).
    reads_done: set[str] = field(default_factory=set)
    # A routed turn's tools as the latest request declared them (None on the general loop, whose deferred
    # tools mount by being called): a call outside them is refused before it runs (#929).
    offered_tools: frozenset[str] | None = None
    card_count: int | None = None
    numeric_text: bool | None = None

    @property
    def ui_fallback(self) -> bool:
        return bool(self.route is not None and self.route.tool_names == UI_FALLBACK_TOOLS)

    @property
    def forcing(self) -> bool:
        """Whether this turn's next round must call a tool (the order round, propose_order not yet structured)."""
        return bool(self.route is not None and self.route.card_required and not self.structured_done and not self.fast)

    @property
    def account_unconfirmed(self) -> bool:
        """The latest whole-account read this turn failed (#872): no earlier successful read vouches for the
        account after it — typically a write whose re-read (accountAfter) came back unavailable. Only another
        whole read (the same combined snapshot: a later write's accountAfter) restores it; get_positions and
        get_account_activity each read one half, not in one snapshot, so they never do (#900 review)."""
        return bool(self.account_reads) and self.account_reads[-1]["available"] is not True

    @property
    def verified(self) -> bool:
        """Whether a live fact was actually read this turn (a successful market / account / web call).
        Once the latest account read failed, the earlier account reads no longer count (#872); market
        and news reads still do."""
        if not self.account_unconfirmed:
            return bool(self.evidence)
        return any(record.get("tool") not in ACCOUNT_EVIDENCE_TOOLS for record in self.evidence)


@dataclass
class StructuredResult:
    value: Any
    tool_calls: list[tuple[str, dict[str, Any], dict[str, Any]]]
    tokens: int = 0


class NativeAgentAdapter:
    adapter_id = "native-agent"

    def __init__(
        self, *, store: RuntimeStore, broker: EventBroker, workspace_root: Path,
        endpoints: dict[str, ModelEndpoint], provider: str,
        data_provider: ReadOnlyDataProvider | None = None,
        backtests: BacktestService | None = None,
        proposals: ProposalService | None = None,
        autonomy: AutonomousSimulationService | None = None,
        accounts: AccountRegistry | None = None,
        research_brief: ResearchBriefService | None = None,
        research_live: ResearchLiveService | None = None,
        status_overrides: dict[str, Any] | None = None,
        turn_timeout_seconds: float | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        # The user a per-user (cloud) adapter serves: background structured calls count against them.
        self.usage_user: str | None = None
        self.broker = broker
        self.workspace = WorkspacePolicy(workspace_root)
        self.workspace.prepare()
        self.data_provider = data_provider or FakeReadOnlyDataProvider()
        self.status_overrides = status_overrides or {}
        self.provider = provider
        self.endpoints = dict(endpoints)
        self.model = default_model_slug(self.endpoints, provider)
        self.backtests = backtests
        # Per-turn tools bind to the trading account the session acts on; without
        # account wiring every turn uses the default tools built below.
        self.accounts = accounts
        self._http_transport = http_transport
        self.model_clients: dict[str, AsyncOpenAI] = {
            slug: self._make_client(endpoint) for slug, endpoint in self.endpoints.items()
        }
        self._traits: dict[str, dict[str, Any]] = {
            slug: dict(PROVIDER_TRAITS.get(endpoint.provider, PROVIDER_TRAITS["deepseek"]))
            for slug, endpoint in self.endpoints.items()
        }
        configured = os.getenv("AI_TRADER_AGENT_TURN_TIMEOUT_SECONDS") or "1800"
        try:
            resolved = float(configured) if turn_timeout_seconds is None else turn_timeout_seconds
        except ValueError:
            resolved = 1800.0
        self.turn_timeout_seconds = resolved if resolved > 0 else 1800.0
        self.trader_tools = TraderReadOnlyTools(
            self.data_provider, self.runtime_status, backtests, proposals, autonomy,
            # Chat search has its own daily cap so a busy chat cannot starve news collection's (owner 10-30).
            web_search=WebSearchClient(
                budget=DailyCallBudget("agent-search", store.connection, store.lock),
                daily_limit=int(os.getenv("AI_TRADER_AGENT_SEARCH_DAILY_CALLS", "600"))),
            research_brief=research_brief, research_live=research_live,
        )
        # The shared set reads the default account's provider: its cards go to that account (#932 A6).
        self.trader_tools.account_id = accounts.default.account_id if accounts is not None else None
        # Catalog tables for the deterministic router and the Runtime-attached position lines
        # (monotonic time → (product names, symbol → instrumentName)); rebuilt every ~10 minutes.
        self._router_catalog: tuple[float, tuple[list[str], dict[str, str]]] | None = None
        self._task_cards: dict[str, dict[str, Any]] = {}
        # 回答卡片化统计 (2026-10-14): turnId → "event|cardId" of the cards published under it while it runs.
        self._turn_card_ids: dict[str, set[str]] = {}
        self._turns: dict[str, _Turn] = {}
        # 判断卡: background follow-ups of a 生成中 judgment card (held so they are not collected).
        self._judgment_tasks: set[asyncio.Task[None]] = set()
        self._history: dict[str, list[dict[str, Any]]] = {}
        # 按技能延迟挂载 (2026-10-14): skills mounted per session — by `activate_skill`, by the sentence's
        # own words (the skill's mount-words) or by a call to one of its tools — mapped to the session
        # turn number that last needed them. A mounted skill's deferred tools ride the requests and its
        # manual rides the instructions until SKILL_MOUNT_IDLE_TURNS turns pass without it.
        self._mounted_skills: dict[str, dict[str, int]] = {}
        self._session_turn_no: dict[str, int] = {}
        self._history_loaded: set[str] = set()
        self._session_ids: set[str] = set()
        # 需要确认 cards this session's user has not answered yet: the next message in the
        # session — an option button's prompt or any typed reply — settles them.
        self._open_questions: dict[str, list[dict[str, Any]]] = {}
        self._tool_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._stream_pending: dict[tuple[str, str, str], tuple[str, str, dict[str, Any]]] = {}
        self._stream_tasks: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self._stream_snapshot_at: dict[tuple[str, str, str], float] = {}
        self._stream_inflight: dict[tuple[str, str, str], asyncio.Future[Any]] = {}
        self._seen_stream_keys: set[tuple[str, str, str]] = set()
        # In-process listeners for a session's assistant text as it streams (the voice call reads it
        # out sentence by sentence); they see every delta before the SSE coalescer.
        self._turn_text_listeners: dict[str, list[tuple[Callable[[dict[str, Any]], None],
                                                       Callable[[str], None] | None]]] = {}
        # Shared with the side channels (explanations, briefs, design).
        self._gated_runs: set[str] = set()
        self._design_lock = asyncio.Lock()

    # -- configuration -------------------------------------------------------

    def _make_client(self, endpoint: ModelEndpoint) -> AsyncOpenAI:
        http_client = (
            httpx.AsyncClient(transport=self._http_transport, timeout=httpx.Timeout(60.0, read=300.0))
            if self._http_transport is not None else None
        )
        return AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key,
                           http_client=http_client, max_retries=1)

    @property
    def agent_authenticated(self) -> bool:
        return bool(self.model and self.model in self.model_clients)

    def runtime_status(self, mode: str) -> dict[str, Any]:
        connected = self.agent_authenticated
        return describe_runtime({
            "mode": mode, "adapter": self.adapter_id, "runtimeConnected": True,
            "agentConnected": connected, "connected": connected, "version": __version__,
            "agentProvider": self.provider, "agentModel": self.model,
            **self.data_provider.runtime_status(), **self.status_overrides,
        })

    def session_runtime_status(self, session: Any) -> dict[str, Any]:
        """`runtime_status` plus the account block of the account this session
        acts on (4.4a) — the snapshot a turn or a resume publishes for it."""
        account_id = (session.get("tradingAccountId") if isinstance(session, dict)
                      else getattr(session, "trading_account_id", None))
        user_id = (session.get("userId") if isinstance(session, dict)
                   else getattr(session, "user_id", None))
        status = self.runtime_status(
            str(session.get("mode") if isinstance(session, dict) else getattr(session, "mode", "local")))
        if not account_id:
            return status
        account = session_account_status_payload(
            self.store, account_id=str(account_id), user_id=str(user_id))
        if account is None:
            return status
        # A paper session describes its own engine account, not the deployment
        # default (the greeting and 设置 → 账户 read these fields).
        if self.accounts is not None:
            status = merge_paper_account_status(
                status, store=self.store, accounts=self.accounts, account_id=str(account_id))
        return {**status, "account": account}

    def _account_for(self, session: Any):
        """The account services the session acts on, or the default account."""
        if self.accounts is None:
            return None
        account_id = (session.get("tradingAccountId") if isinstance(session, dict)
                      else getattr(session, "trading_account_id", None))
        if not account_id:
            return self.accounts.default
        account = self.accounts.get(str(account_id))
        if account is None:
            # Running the turn on the default account would act on the wrong
            # book; the turn must stop instead.
            raise AccountNotAssembled(str(account_id))
        return account

    def _turn_tools(self, session: Any) -> TraderReadOnlyTools:
        """Tools bound to the session's trading account: its provider, its autonomy
        and its simulation-trading flag. The default account reuses the shared set."""
        account = self._account_for(session)
        if account is None:
            return self.trader_tools
        return self.tools_for_account(account.account_id)

    def tools_for_account(self, account_id: str) -> TraderReadOnlyTools:
        """Tools bound to one account for a caller that already resolved which
        account it acts on (an agent turn's session, the app_call bridge's
        session) — never the shared default set for another account's user."""
        registry = self.accounts
        if registry is None or account_id == registry.default.account_id:
            return self.trader_tools
        account = registry.get(account_id)
        if account is None:
            # Running the caller on the default account would act on the wrong
            # book; the call must stop instead.
            raise AccountNotAssembled(account_id)

        def account_status(mode: str) -> dict[str, Any]:
            overrides = dict(self.status_overrides)
            overrides["simulationTrading"] = account.simulation_orders.enabled
            return describe_runtime({
                "mode": mode, "adapter": self.adapter_id, "runtimeConnected": True,
                "agentConnected": self.agent_authenticated, "connected": self.agent_authenticated,
                "version": __version__,
                "agentProvider": self.provider, "agentModel": self.model,
                **account.provider.runtime_status(), **overrides,
            })

        tools = TraderReadOnlyTools(
            account.provider, account_status, self.backtests, self.trader_tools.proposals, account.autonomy,
            web_search=self.trader_tools.web_search, research_brief=self.trader_tools.research_brief,
            research_live=self.trader_tools.research_live,
        )
        # The shared set is bound to the app catalog after construction; without it every
        # tool of a non-default account (each invited user's own TqKq account) fails.
        tools.app_capabilities = self.trader_tools.app_capabilities
        tools.account_id = account.account_id  # the cards this set plans go to this account (#932 A6)
        return tools

    def account_status(self) -> dict[str, Any]:
        configured = self.agent_authenticated
        return {
            "available": True, "authenticated": configured, "loginRequired": False,
            "configurationRequired": not configured, "credentialConfigured": configured,
            "provider": self.provider, "providerName": PROVIDER_NAMES.get(self.provider, self.provider),
            "model": self.model, "authMode": None, "planType": None,
        }

    async def refresh_account(self) -> dict[str, Any]:
        return self.account_status()

    def error_payload(self, error: Exception, run_id: str) -> dict[str, Any]:
        code = str(error) if isinstance(error, NativeAgentError) else "agent_run_failed"
        if code.endswith("_api_key_required"):
            name = PROVIDER_NAMES.get(code.removesuffix("_api_key_required"), "模型")
            message = f"请先在 Sente 内配置 {name} API Key，再发送请求。"
        else:
            message = {
                "model_request_failed": "模型请求失败，当前任务已安全停止；Runtime 仍保持在线。",
                "model_stream_interrupted": "模型响应中途中断，当前任务已停止，可以重试。",
                "agent_turn_timeout": "模型响应超时，已中断并释放队列；Runtime 仍保持在线。",
                "model_output_invalid": "模型返回了无法解析的内容，当前任务已停止。",
                "model_output_empty": "模型没有返回内容（可能被输出上限截断），当前任务已停止。",
                "model_unavailable": "所选模型当前不可用（密钥未配置或已下线），请在设置里重新选择模型。",
                "model_auth_failed": "模型认证失败，请检查当前模型的密钥配置。",
                "model_rate_limited": "模型额度不足或请求过于频繁，请稍后重试。",
                "account_not_assembled": "这个会话绑定的交易账户当前未装配，本轮对话已停止。",
            }.get(code, "AI 任务失败，Runtime 仍保持在线。")
        return {"code": code, "message": message, "runId": run_id, "scope": "task"}

    def _endpoint_for(self, model_slug: str | None) -> ModelEndpoint:
        model_slug = canonical_model_slug(model_slug) if model_slug is not None else None
        if model_slug is not None and model_slug not in self.endpoints:
            # A stored preference whose key disappeared must not silently
            # switch the account to another model.
            raise NativeAgentError("model_unavailable")
        slug = model_slug or self.model
        if not slug or slug not in self.endpoints:
            provider = self.provider if self.provider in PROVIDER_NAMES else "model"
            raise NativeAgentError(f"{provider}_api_key_required")
        return self.endpoints[slug]

    def _reasoning_effort(self) -> str:
        effort = os.getenv("AI_TRADER_AGENT_REASONING_EFFORT", "low").strip().lower()
        return effort if effort in {"minimal", "low", "medium", "high", "xhigh", "max"} else "low"

    def _tool_definitions(
        self, names: set[str] | frozenset[str] | None = None, *, user_id: str | None = None,
        disclosure: bool = False,
    ) -> list[dict[str, Any]]:
        tools = []
        replay = bool(getattr(self.data_provider, "identifier", None))
        for definition in self.trader_tools.definitions():
            if names is not None and definition["name"] not in names:
                continue
            if replay and definition["name"] in {"web_search", "get_research_brief",
                                                    "get_news_flash", "get_foreign_markets",
                                                    "get_economic_calendar", "get_interest_rates"}:
                continue
            tools.append({
                "type": "function", "name": definition["name"],
                "description": definition.get("description", ""),
                "parameters": wire_schema(definition["inputSchema"]), "strict": False,
            })
        if disclosure:
            # Progressive disclosure (2026-09-24): the general loop can open a
            # skill's manual and read its resources; the deterministic routed
            # subsets keep their fixed tool lists. The enum carries only the
            # skills this user may see — an unavailable skill cannot be named.
            available = allowed_skills(self.store, user_id) if user_id else all_skills()
            tools.append({
                "type": "function", "name": "activate_skill",
                "description": "挂上一个技能：它的操作手册进系统说明，它的专用工具（目录里标 * 的技能才有）从下一轮起可直接调用。"
                               "要做的事需要当前没有的工具，或要按某技能的详细步骤做时先调它；挂上后几轮不用会自动卸下，再要用再挂。",
                "parameters": {
                    "type": "object",
                    "properties": {"skillId": {"type": "string",
                                               "enum": [skill.skill_id for skill in available]}},
                    "required": ["skillId"], "additionalProperties": False,
                },
                "strict": False,
            })
            tools.append({
                "type": "function", "name": "read_skill_resource",
                "description": "读取一个已加载技能的附加资源（references/ 下的细节文件）。"
                               "path 用技能手册里 <skill_resources> 列出的相对路径。",
                "parameters": {
                    "type": "object",
                    "properties": {"skillId": {"type": "string"},
                                   "path": {"type": "string"}},
                    "required": ["skillId", "path"], "additionalProperties": False,
                },
                "strict": False,
            })
        return tools

    def _instructions(self, *, user_id: str | None = None, session_id: str | None = None) -> str:
        instructions = self.workspace.developer_instructions()
        # Progressive disclosure tier 1: the catalog of available skills (name +
        # description only). The full manual loads on `activate_skill`, and an
        # activated skill stays in the instructions for the rest of the session,
        # so history folding or compaction never loses it.
        available = allowed_skills(self.store, user_id) if user_id else all_skills()
        mounted = self._mounted_skills.get(session_id or "", {})
        if available:
            deferred = TraderReadOnlyTools.DEFERRED_TOOL_NAMES
            instructions += ("\n可用技能目录（标 * 的有专用工具，没挂上时先调 activate_skill 挂上；需要详细步骤时也先挂上）：\n"
                             + "\n".join(("*" if skill.skill_id not in mounted
                                           and any(tool in deferred for tool in skill.tools) else "")
                                          + skill_catalog_line(skill) for skill in available))
        for skill_id in sorted(mounted):
            skill = skill_by_id(skill_id)
            if skill is not None and skill in available:
                instructions += "\n" + skill_content_block(skill)
        if getattr(self.data_provider, "identifier", None):
            instructions += (
                " 当前是独立历史回放。只使用本次回放接口与回放时钟之前的已收盘数据；"
                "禁止网络检索、读取当前模拟账户或用模型记忆的未来行情作判断。"
                "临场AI判断不等于无未来信息的确定性回测。诊断以策略与成交记录为准。")
        return instructions

    # -- session lifecycle ---------------------------------------------------

    async def bootstrap(self, session_id: str) -> None:
        session = self.broker.get_session(session_id)
        if session is None:
            return
        self._session_ids.add(session_id)
        await self.broker.publish(session_id, kind="result", name="session.ready",
                                  payload={"mode": session.mode, "agentId": "native"})
        await self.broker.publish(session_id, kind="result", name="runtime.status",
                                  payload=self.session_runtime_status(session))
        turn_id = str(uuid4())
        if not self.agent_authenticated:
            await self.broker.publish(session_id, kind="error", name="error", payload={
                "code": "agent_not_configured",
                "message": self.error_payload(NativeAgentError(f"{self.provider}_api_key_required"), "bootstrap")["message"],
                "scope": "agent"})
            text = "Sente 已就绪，但模型暂未配置。只读数据状态可继续查看。"
        else:
            text = "Sente 已连接。" + self.session_runtime_status(session)["capabilitySummary"] + "。"
        await self.broker.publish(session_id, kind="message", name="message.completed",
                                  payload={"turnId": turn_id, "role": "assistant", "agentId": "native", "text": text})
        try:
            account = self._account_for(session)
            provider = account.provider if account is not None else self.data_provider
            await self.broker.publish(session_id, kind="artifact", name="card.account",
                                      payload=provider.account_summary(turn_id))
        except AccountNotAssembled:
            # The session's account is gone from this boot: no account card.
            # Showing the default account's card here would mislabel the session.
            pass

    async def refresh_session(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        self._session_ids.add(session_id)
        await self.broker.publish(session_id, kind="result", name="runtime.status",
                                  payload=self.session_runtime_status(session))

    async def release_session(self, session_id: str) -> None:
        self._history.pop(session_id, None)
        self._history_loaded.discard(session_id)
        self._session_ids.discard(session_id)
        self._mounted_skills.pop(session_id, None)
        self._session_turn_no.pop(session_id, None)

    # -- 按技能延迟挂载 (2026-10-14) --------------------------------------------------------------

    def _begin_mount_turn(self, session_id: str, user_id: str | None, text: str) -> set[str]:
        """A new turn of the session: skills idle for more than SKILL_MOUNT_IDLE_TURNS turns unmount,
        and the skills whose mount-words the sentence uses mount for this turn (no activate_skill round
        spent on "螺纹到 3150 提醒我"). Returns the word-mounted skills that carry deferred tools."""
        turn_no = self._session_turn_no.get(session_id, 0) + 1
        self._session_turn_no[session_id] = turn_no
        mounted = self._mounted_skills.setdefault(session_id, {})
        for skill_id, last in list(mounted.items()):
            if turn_no - last > SKILL_MOUNT_IDLE_TURNS:
                del mounted[skill_id]
                log.info("agent_skill_unmounted session=%s skill=%s", session_id, skill_id)
        available = allowed_skills(self.store, user_id) if user_id else all_skills()
        named: set[str] = set()
        for skill in available:
            if any(word in text for word in skill_mount_words(skill)):
                if skill.skill_id not in mounted:
                    log.info("agent_skill_mounted session=%s skill=%s by=words", session_id, skill.skill_id)
                mounted[skill.skill_id] = turn_no
                if any(tool in TraderReadOnlyTools.DEFERRED_TOOL_NAMES for tool in skill.tools):
                    named.add(skill.skill_id)
        return named

    def _mount_skill(self, session_id: str, skill_id: str, *, by: str) -> bool:
        """Mount (or keep mounted) one skill for the session's current turn; True when newly mounted."""
        mounted = self._mounted_skills.setdefault(session_id, {})
        fresh = skill_id not in mounted
        mounted[skill_id] = self._session_turn_no.get(session_id, 0)
        if fresh:
            log.info("agent_skill_mounted session=%s skill=%s by=%s", session_id, skill_id, by)
        return fresh

    def _visible_tool_names(self, session_id: str) -> set[str]:
        """The tools a general turn carries: every definition not deferred, plus the deferred tools
        of the skills mounted for the session."""
        mounted_tools = {tool for skill_id in self._mounted_skills.get(session_id, {})
                         for tool in getattr(skill_by_id(skill_id), "tools", ())}
        return {definition["name"] for definition in self.trader_tools.definitions()
                if not definition.get("deferLoading") or definition["name"] in mounted_tools}

    def forget_history(self) -> None:
        """Drop the in-memory history so the next turn rebuilds it from events."""
        self._history.clear()
        self._history_loaded.clear()

    def append_voice_turn(self, session_id: str, user_text: str, assistant_text: str) -> None:
        """A turn the voice front desk answered itself (its pair is already published as message.completed):
        keep this session's in-memory history in step, so a typed "那它呢" right after the call sees it.
        A history not loaded yet picks it up from the events; while a turn of this session is running its
        history marks would shift, so that session alone is rebuilt from the events on its next turn."""
        if session_id not in self._history_loaded or not user_text or not assistant_text:
            return  # an unanswered question is not history (the events rebuild skips it too)
        if any(turn.session_id == session_id for turn in self._turns.values()):
            self._history.pop(session_id, None)
            self._history_loaded.discard(session_id)
            return
        self._history.setdefault(session_id, []).extend(
            [{"role": "user", "content": user_text}, {"role": "assistant", "content": assistant_text}])

    async def cancel_run(self, run_id: str) -> None:
        self._cancel_backtest_lease(run_id)
        await self._wait_for_tool_tasks(run_id)

    async def _wait_for_tool_tasks(self, run_id: str) -> None:
        # A tool call runs in its own task so that a cancelled turn still drains
        # the worker thread (backtest lease, order query) before the run closes.
        while tasks := tuple(self._tool_tasks.get(run_id, ())):
            await asyncio.gather(*tasks, return_exceptions=True)

    def _track_tool_task(self, run_id: str, task: asyncio.Task[Any]) -> None:
        self._tool_tasks.setdefault(run_id, set()).add(task)

        def discard(completed: asyncio.Task[Any]) -> None:
            tasks = self._tool_tasks.get(run_id)
            if tasks is None:
                return
            tasks.discard(completed)
            if not tasks:
                self._tool_tasks.pop(run_id, None)

        task.add_done_callback(discard)

    def _cancel_backtest_lease(self, run_id: str) -> bool:
        if self.backtests is None:
            return False
        try:
            user_id = self.store.run_user_id(run_id)
        except KeyError:
            return False
        return self.backtests.coordinator.cancel(run_id, user_id=user_id)

    async def shutdown(self) -> None:
        for key in list(self._stream_pending):
            await self._flush_stream_now(key, persist=True)
        for task in list(self._stream_tasks.values()):
            task.cancel()
        await asyncio.gather(*self._stream_tasks.values(), return_exceptions=True)
        self._stream_tasks.clear()
        if self._tool_tasks:
            await asyncio.gather(*(task for tasks in self._tool_tasks.values() for task in tasks),
                                 return_exceptions=True)
        for client in self.model_clients.values():
            await client.close()
        self._history.clear()
        self._session_ids.clear()

    # -- conversation history ------------------------------------------------

    # "candidate_recorded" is the user's approval, not a send receipt: what each order
    # became is read back from the order links (see _proposal_status_line).
    PROPOSAL_STATUS_LABELS = {"approval_required": "等待确认", "candidate_recorded": "已确认",
                              "rejected": "已放弃或超时", "risk_blocked": "风险未通过", "draft": "草稿",
                              "risk_rules_applied": "已生效"}

    def _clock_line(self) -> str:
        """现在几点 (#932 P2: the trading AI read a tool's UTC capturedAt as Beijing time and said 16:25 at 00:25):
        the turn's own Beijing time, and how the tools' times read. An independent replay keeps its replay clock."""
        if getattr(self.data_provider, "identifier", None):
            return ""
        now = datetime.now(UTC).astimezone(timezone(timedelta(hours=8)))
        return (f"现在是北京时间 {now:%Y-%m-%d %H:%M}（星期{'一二三四五六日'[now.weekday()]}）。工具结果里带 +00:00 或 Z 的时间是 UTC，"
                "说给用户的时刻一律换算成北京时间（加 8 小时）。\n")

    def _preference_line(self, user_id: str, *, origin: str | None) -> str:
        """The account settings this turn must honour (GET /api/preferences): stated before the routing
        instructions so they override the generic defaults written there."""
        try:
            preferences = self.store.user_preferences(user_id)
        except Exception:
            return ""
        line = preference_instruction(preferences, origin=origin)
        web_research = skill_by_id("web-research")
        if user_id and web_research is not None and not skill_allowed(
                self.store, user_id, web_research):
            # The tools are already withheld; the model must know it cannot
            # browse, so it answers from its own knowledge instead of promising
            # to look something up online.
            line += " 该账户未开通联网研究：没有联网工具，不要承诺联网查询，直接说明信息来自自身知识。"
        return line

    def _memory_line(self, user_id: str, user_text: str) -> str:
        """The resident private-memory block (画像/约定/相关经验) for this turn: reference
        only, never authorization, and the user's own words this turn win on conflict."""
        from . import user_memory
        try:
            return user_memory.turn_block(self.store, user_id, user_text)
        except Exception:
            return ""

    def _hidden_tools(self, user_id: str | None) -> frozenset[str]:
        """The tool names this user cannot see (tier-withheld, globally
        disabled or owner-only skills): the general loop, the fast read-only
        subset and the routed subsets all filter through here."""
        if not user_id:
            return frozenset()
        return hidden_tool_names(self.store, user_id)

    def _skill_unavailable_line(self, user_id: str | None, skill_id: str) -> str | None:
        """The unified refusal line when a deterministic hit routed to a skill
        the user lacks (typed side; voice builds the same line in run_tool).
        None when the skill is available — the routed path then runs as usual."""
        skill = skill_by_id(skill_id)
        reason = skill_block_reason(self.store, user_id, skill) if skill else "disabled"
        if reason is None or skill is None:
            return None
        return unavailable_line(skill, reason, parent_block_reason(self.store, user_id, skill))

    async def _ui_window_line(
        self, decoded: DecodedInstruction, user_id: str, session_id: str, tools: TraderReadOnlyTools,
        *, review_only: bool = False,
    ) -> str:
        """The UI round's window, read once by the Runtime (AI audit 2026-10-13): its id and revision so the
        first command needs no read round, the focus fields, and the trading days a "昨天的复盘" means.
        Nothing when the turn has no window or it cannot be read (the model reads it itself, as before)."""
        if not decoded.ui_window_id:
            return ""
        try:
            window = await self.trader_tools._ui_window(decoded.ui_window_id, user_id=user_id,
                                                        session_id=session_id, task_id=None)
        except Exception as error:
            log.info("agent_ui_window_line_skipped session=%s reason=%s", session_id, error)
            return ""
        raw_state = window.get("state")
        state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
        review = state.get("review") if state.get("view") == "review" or state.get("mobilePage") == "review" else None
        review_line = ""
        if isinstance(review, dict):
            scope = {key: review.get(key) for key in ("currency", "currencies", "filter", "month", "day", "selectedTradeId")}
            review_line = ("当前复盘范围：" + json.dumps(scope, ensure_ascii=False) + "。"
                           "currency是复盘账本，不是对话的交易账户；month/day是展开分组，不自动限制历史。"
                           "选历史记录先用app_call GET /api/account/pnl（query.currency按此币种，近366天），"
                           "或get_trade_journal（currency同上、days=366）；activity只含当日成交，空表不能证明历史为空。"
                           "按用户说的日期/合约与当前filter核对历史；未指定先后时“第一笔”按界面顺序：平仓时间倒序、同时间id升序。"
                           "范围有歧义先问，无匹配只报告本次范围无匹配。reviewSelect只用读回的真实id，等applied后核对"
                           "review.currency/selectedTradeId；不得切交易账户或拿图表主连代码替换历史合约过滤。\n")
        if review_only:
            return (f"本轮界面窗口：windowId={window['windowId']}，revision={window['revision']}；" + review_line) if review_line else ""
        days = await exchange_calendar.trading_days(tools.provider)
        today = trading_day_at(datetime.now(UTC), days)
        focus = screen_snapshot_instruction(window, focus_only=True).strip()
        # The appearance the relative words act on ("字大一点" from large is extra-large): live 10-13 the
        # round set large again on a large window because it no longer read the window itself.
        looks = "；".join(f"{key}={state[key]}" for key in ("theme", "fontScale", "density") if state.get(key))
        return (f"本轮界面窗口：windowId={window['windowId']}，revision={window['revision']}；"
                f"当前交易日 {today}，上一个交易日 {previous_trading_day(today, days)}。"
                + (f"外观：{looks}。" if looks else "") + f"{focus}\n" + review_line)

    async def _screen_snapshot_line(self, decoded: DecodedInstruction, user_id: str, session_id: str) -> str:
        """The window snapshot for a turn that points at the screen (这个 / 屏幕上 / 现在显示的…),
        attached by Runtime instead of costing the model an app_call round. Nothing when the sentence
        does not point at the screen, the account turned chat.autoReadScreen off, the turn has no
        window, or the window cannot be read (offline: answer from what is known, as before). An order needs
        none of it: the planner reads the account and the window's contract itself (意图重构第 3 步)."""
        if not decoded.ui_window_id or not screen_reference(decoded.user_text):
            return ""
        if not self.store.user_preferences(user_id).chat.autoReadScreen:
            return ""
        try:
            window = await self.trader_tools._ui_window(decoded.ui_window_id, user_id=user_id,
                                                        session_id=session_id, task_id=None)
        except Exception as error:
            log.info("agent_screen_snapshot_skipped session=%s reason=%s", session_id, error)
            return ""
        line = screen_snapshot_instruction(window, focus_only=False)
        log.info("agent_screen_snapshot session=%s chars=%d", session_id, len(line))
        return line

    def _symbol_label(self, symbol: str) -> str:
        names = self._router_catalog[1][1] if self._router_catalog is not None else {}
        name = names.get(symbol.upper()) or names.get(symbol.split(".", 1)[-1].upper())
        return f"{name} {symbol}" if name else symbol

    def _positions_text(self, rows: list[dict[str, Any]], *, limit: int | None = None) -> str:
        parts: list[str] = []
        for row in rows[:limit]:
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            direction = "多" if row.get("direction") == "long" else "空"
            volume = int(row.get("volume") or 0)
            today = int(row.get("todayVolume") or 0)
            historical = int(row.get("historicalVolume") or 0)
            split = f"（今{today} 昨{historical}）" if today or historical else ""
            # 均价 (合并卡 2026-10-06): “剩下的止损挪到成本” takes the cost from this line.
            average = row.get("openPrice")
            cost = f" 均价 {average:g}" if isinstance(average, (int, float)) and average > 0 else ""
            usd = "（外盘·美元账）" if row.get("ledger") == "USD" else ""
            parts.append(f"{self._symbol_label(symbol)}{usd} {direction} {volume} 手{split}{cost}")
        text = "；".join(parts)
        if limit is not None and len(rows) > limit:
            text += f"；另有 {len(rows) - limit} 笔"
        return text

    # 持仓/挂单/保护的现状（2026-10-13 夜盘 P1）：说到这些的回合附上现场读的一份。
    ACCOUNT_STATE_PREFIX = "账户现状（系统本轮现场读取，{at}）："
    ACCOUNT_STATE_RULE_LINE = ("说剩余持仓、挂单或保护只按这段和本轮工具结果，不按之前对话里说过的；本轮中途撤单或挂撤保护后，"
                               "以那次工具结果里的 accountAfter 为准。\n")
    ACCOUNT_STATE_UNREADABLE_LINE = "账户现状本轮读不到：不要陈述剩余持仓、挂单或保护，需要时请用户看持仓面板。\n"
    # After a write this turn (#872) the account line read before it is replaced: it describes the account
    # before the write, never what is left now.
    ACCOUNT_AFTER_WRITE_PREFIX = "账户现状（本轮中途撤单/挂撤保护/急停之后）："
    ACCOUNT_AFTER_WRITE_LINE = (ACCOUNT_AFTER_WRITE_PREFIX + "以本轮最近一次写操作工具结果里的 accountAfter 为准；"
                                "回合开头读的那份是操作前的，已过时，不要再用。\n")
    ACCOUNT_AFTER_WRITE_UNREADABLE_LINE = (ACCOUNT_AFTER_WRITE_PREFIX + "操作后没能重新读到账户，现在的持仓、挂单和保护未知；"
                                           "回合开头读的那份是操作前的，已过时。操作本身的结果照工具回报说；不要陈述剩余持仓、"
                                           "挂单或保护（也不要说没有），请用户看持仓面板。\n")
    ACCOUNT_UNCONFIRMED_WARNING = "账户现状未能确认"
    ACCOUNT_UNCONFIRMED_SPOKEN = "账户现状没能重新读到，剩下的持仓、挂单和保护以持仓面板为准。"
    # The account lines Runtime put in a turn's request text (the state line, its unreadable form, the voice
    # positions line and an earlier after-write line) — only these exact prefixes, never the user's own words.
    _INJECTED_ACCOUNT_LINES = re.compile("(?:" + "|".join(re.escape(prefix) for prefix in (
        ACCOUNT_STATE_PREFIX.split("{")[0], ACCOUNT_STATE_UNREADABLE_LINE.split("：")[0] + "：",
        ACCOUNT_AFTER_WRITE_PREFIX)) + ")[^\n]*\n?")

    def _note_account_read(self, turn: _Turn, source: str, *, available: bool, as_of: Any = None,
                           write: bool = False) -> None:
        """The one entry for this turn's account reads (#872): the opening read, every write's re-read
        (accountAfter) — whole-account snapshots only. The latest one decides whether the account
        may be stated now (`_Turn.account_unconfirmed`: stream, footnote, verified). Once the turn wrote,
        the account lines in its request text are replaced, so later rounds never see the pre-write
        snapshot as "现场读取": an unreadable re-read says the state is unknown, a later good read restores."""
        turn.account_reads.append({"source": source, "available": available, "asOf": as_of, "write": write})
        if not any(read["write"] for read in turn.account_reads):
            return
        text, replaced = self._INJECTED_ACCOUNT_LINES.subn("", turn.model_text)
        if replaced or not available:
            turn.model_text = (self.ACCOUNT_AFTER_WRITE_LINE if available
                               else self.ACCOUNT_AFTER_WRITE_UNREADABLE_LINE) + text

    async def _voice_positions_line(self, tools: TraderReadOnlyTools, user_id: str, *, mode: str) -> str:
        """语音委派短上下文's current state: the positions from `account_state` (stale / unreadable →
        nothing, never a cached row presented as current). Not turn evidence: it backs no market fact."""
        try:
            state = await tools.account_state(user_id, mode=mode)
        except Exception:
            log.exception("agent_voice_positions_line_failed")
            return ""
        if state.get("available") is not True:
            return ""
        positions = state.get("positions") or []
        text = self._positions_text(positions, limit=None) if positions else "无"
        if (state.get("foreignAccount") or {}).get("status") == "unavailable":
            text += "（外盘美元账这次读不到）"
        return (self.ACCOUNT_STATE_PREFIX.format(at=f"北京时间 {state.get('asOfLocal') or ''}")
                + "持仓：" + text + "。\n")

    @staticmethod
    def _wants_account_state(user_text: str, route: TurnRoute) -> bool:
        """A sentence about positions / orders / protections on a turn that may answer in prose. The order
        round ends in its card (the planner reads the account itself)."""
        return account_state_request(user_text) and route.kind != "order"

    async def _account_state_line(self, turn: _Turn, tools: TraderReadOnlyTools, user_id: str) -> str:
        """The account's positions, working orders and effective protections, read now, for a sentence
        about them: the model states what is left from this line or this turn's tools, never from an
        earlier turn's words. A successful read is this turn's evidence (the freshness footnote does not
        call it model knowledge); unreadable: the line says so and forbids the claim."""
        try:
            state = await tools.account_state(user_id, mode=turn.mode)
        except Exception:
            log.exception("agent_account_state_line_failed")
            state = {}
        if state.get("available") is not True:
            self._note_account_read(turn, "account_state", available=False)
            return self.ACCOUNT_STATE_UNREADABLE_LINE
        turn.evidence.append({"tool": "account_state", "ok": True, "asOf": state.get("asOf")})
        self._note_account_read(turn, "account_state", available=True, as_of=state.get("asOf"))
        orders: list[str] = []
        for row in state.get("workingOrders") or []:
            side = {"BUY": "买", "SELL": "卖"}.get(str(row.get("direction") or "").upper(), "")
            offset = "开" if str(row.get("offset") or "").upper() == "OPEN" else "平"
            price = row.get("limitPrice")
            price_text = f" 限价 {price:g}" if isinstance(price, (int, float)) else ""
            left = row.get("volumeLeft", row.get("volume"))
            usd = "（外盘·美元账）" if row.get("ledger") == "USD" else ""
            orders.append(f"{self._symbol_label(str(row.get('symbol') or ''))}{usd} {side}{offset} 剩 {left} 手{price_text}")
        protections: list[str] = []
        for row in state.get("protections") or []:
            direction = "多" if row.get("positionDirection") == "long" else "空"
            levels = "，".join(part for part in (
                f"止损 {row['triggerPrice']:g}" if isinstance(row.get("triggerPrice"), (int, float)) else "",
                f"止盈 {row['takeProfitPrice']:g}" if isinstance(row.get("takeProfitPrice"), (int, float)) else "",
            ) if part)
            volume = row.get("remainingVolume", row.get("volume"))
            usd = "（外盘·美元账）" if row.get("ledger") == "USD" else ""
            protections.append(f"{self._symbol_label(str(row.get('symbol') or ''))}{usd} {direction} {volume} 手 {levels}")
        foreign = state.get("foreignAccount") or {}
        unreadable = "外盘美元账这次读不到，不要说外盘没有持仓或挂单。" if foreign.get("status") == "unavailable" else ""
        return (self.ACCOUNT_STATE_PREFIX.format(at=f"北京时间 {state.get('asOfLocal') or ''}")
                + "持仓：" + (self._positions_text(state.get("positions") or [], limit=None) or "无")
                + "。挂着的委托：" + ("；".join(orders) or "无")
                + "。仍生效的保护：" + ("；".join(protections) or "无")
                + "。" + unreadable + self.ACCOUNT_STATE_RULE_LINE)

    async def _product_hint_line(self, user_text: str, tools: TraderReadOnlyTools, *, several: bool = False) -> str:
        """An order, target-position or market-query sentence that names exactly one catalog product:
        resolve it here (the same catalog read search_instruments uses) and attach the contract, so
        the model's first round is the card or the read instead of a search. Zero names or several:
        no line, the model searches as before. An answer-card turn (`several`, 回答卡 token 收敛 2026-10-14)
        resolves up to three named products the same way ("铁矿跟焦炭哪个强"); one that does not resolve
        leaves the whole line out."""
        names = await self._product_names()
        if not names:
            return ""
        matches = product_words(user_text, names)
        if not matches or len(matches) > (3 if several else 1):
            return ""
        if len({word for word, _ in matches}) != len(matches):
            return ""  # one word naming two products ("菜籽" → 菜籽油 / 菜籽粕) is ambiguous: the model asks or searches
        try:
            catalog = await asyncio.to_thread(tools.provider.instrument_catalog)
        except Exception as error:
            log.debug("agent product hint catalog unavailable: %s", error)
            return ""
        items = [item for item in catalog.get("items") or [] if isinstance(item, dict)]
        parts = []
        for word, name in matches:
            item = resolve_symbol(name, items)
            if item is None:
                return ""
            main = "，主力" if item.get("isMain") is True else ""
            log.info("agent_product_hint word=%s symbol=%s", word, item.get("symbol"))
            parts.append(f"{word} = {item.get('symbol')}（{item.get('instrumentName')}{main}）")
        which = "这个合约" if len(parts) == 1 else "这些合约"
        return f"用户说的品种：{'；'.join(parts)}，直接用{which}，不用 search_instruments。\n"

    async def _product_names(self) -> list[str] | None:
        """The catalog's Chinese product names (and productId codes — "IF现在多少钱" names the product
        as much as 沪金 does) for the router. None when the catalog is unavailable or empty: the router
        then matches shapes only, as it did on day one."""
        tables = await self._catalog_tables()
        return tables[0] if tables is not None else None

    async def _catalog_tables(self) -> tuple[list[str], dict[str, str]] | None:
        """(product names, symbol/instrumentId → instrumentName) from the instrument catalog, cached
        ~10 min. The name map serves the Runtime-attached position lines (`_symbol_label`); both share one catalog read."""
        now = monotonic()
        cached = self._router_catalog
        if cached is not None and now - cached[0] < 600.0:
            return cached[1]
        try:
            catalog = await asyncio.to_thread(self.data_provider.instrument_catalog)
            names = catalog_product_names(list(catalog.get("items") or []))
            display: dict[str, str] = {}
            for item in catalog.get("items") or []:
                if not isinstance(item, dict):
                    continue
                full_name = str(item.get("instrumentName") or "").strip()
                if full_name:
                    for key in (str(item.get("symbol") or "").strip(), str(item.get("instrumentId") or "").strip()):
                        if key:
                            display[key.upper()] = full_name
        except Exception as error:
            log.debug("agent router catalog unavailable: %s", error)
            return None
        if not names:
            return None
        self._router_catalog = (now, (names, display))
        return names, display

    def _has_pending_card(self, session_id: str) -> bool:
        """Whether this session has a card waiting for confirmation or editing. Pure navigation
        keeps that card; other sentences retain the model's complete card toolbox."""
        try:
            from .proposals import awaiting_confirmation
            return any(awaiting_confirmation(record) for record in self.store.recent_proposals(session_id, limit=5))
        except Exception:
            return False

    async def _editor_open(self, decoded: DecodedInstruction, text: str, *, user_id: str, session_id: str,
                           product_names: list[str] | None) -> bool:
        """Whether this turn's window shows the 策略编辑卡 — asked only for a sentence the card's 补一句
        could take (a cheap word check first), so an ordinary turn never reads the window for it."""
        from .strategy_editor import editor_open, editor_utterance
        if not decoded.ui_window_id or not editor_utterance(text, product_names):
            return False
        try:
            window = await self.trader_tools._ui_window(decoded.ui_window_id, user_id=user_id,
                                                        session_id=session_id, task_id=None)
        except Exception as error:
            log.info("agent_editor_window_unavailable session=%s reason=%s", session_id, error)
            return False
        return editor_open((window or {}).get("state"))

    async def _execute_tour_step(self, decoded: DecodedInstruction, *, session_id: str) -> str | None:
        """The tour move through the shared `run_tour_step` (the call's too) when the words ask for one and this turn's
        window is in a guided tour; None otherwise — the turn then routes as usual."""
        from .turn_router import run_tour_step
        from .voice_route import tour_op
        op = tour_op(decoded.user_text) if decoded.ui_window_id else None
        if op is None:
            return None
        session = self.store.get_session(session_id)
        user_id = str(session["userId"])
        call = self._bridge_call(user_id, session_id)
        if call is None:
            return None
        try:
            window = await self.trader_tools._ui_window(decoded.ui_window_id, user_id=user_id, session_id=session_id, task_id=None)
        except Exception as error:
            log.info("agent_tour_window_unavailable session=%s reason=%s", session_id, error)
            return None
        if not ((window.get("state") or {}).get("tour") or {}).get("active"):
            return None
        return str((await run_tour_step(op, window=window, call=call)).get("say") or "")

    async def _execute_window_intent(
        self, decoded: DecodedInstruction, intent: Intent, *, session_id: str,
    ) -> str | None:
        """A fixed-table interface request ("把设置打开" / "看看日线") runs without a model turn: one UI
        command through the same `run_window_intent` the voice fast path uses, then one assistant line.
        Returns None when the command did not apply (window offline, unknown contract): the turn then
        falls back to the model exactly as before."""
        session = self.store.get_session(session_id)
        user_id = str(session["userId"])
        call = self._bridge_call(user_id, session_id)
        if call is None:
            return None  # no app bridge wired: the model turn, as before
        try:
            window = await self.trader_tools._ui_window(decoded.ui_window_id, user_id=user_id,
                                                        session_id=session_id, task_id=None)
            outcome = await run_window_intent(intent, window=window, call=call)
        except Exception as error:
            log.info("agent_ui_direct_skipped session=%s reason=%s", session_id, error)
            return None
        if not outcome.get("ok"):
            log.info("agent_ui_direct_fallback session=%s kind=%s error=%s",
                     session_id, intent.kind, outcome.get("error"))
            return None
        line = str(outcome.get("say") or "已执行。")
        answer = {"turnId": str(uuid4()), "role": "assistant", "agentId": "native", "text": line,
                  **({"origin": "voice", "spoken": line} if decoded.origin == "voice" else {})}
        self._notify_turn_text(session_id, "message.completed", answer)
        await self.broker.publish(session_id, kind="message", name="message.completed", payload=answer)
        return line

    async def _execute_order_intent(
        self, decoded: DecodedInstruction, order: OrderIntent, *, session_id: str, run_id: str,
    ) -> str | None:
        """意图重构第 3 步: a typed (or delegated) order the parser is sure of goes straight to the order planner — the
        cards (each its own, in order, each confirmed by the user), the planner's question card, or the refusal in
        the user's words — and one assistant line. No model round: the same skill gate, contract reading (the
        window's contract when none was said), account cut, readiness gate and cards as propose_order. Returns None
        when the account says the words are not settled (卖一手 with the long side held: 开空还是平多？) — the
        trading AI's order round reads them."""
        session = self.store.get_session(session_id)
        user_id = str(session["userId"])
        turn_id = str(uuid4())
        blocked = card_skill_block(self.store, user_id, order)
        if blocked is not None:
            await self._publish_routed_line(session_id, decoded, blocked, run_id, turn_id=turn_id)
            return blocked
        try:
            tools = self._turn_tools(session)
        except AccountNotAssembled:
            return None  # the model path reports the unassembled account
        chart = await self._window_symbol(decoded.ui_window_id, user_id=user_id, session_id=session_id)
        try:
            catalog = await asyncio.to_thread(tools.provider.instrument_catalog)
            items = [item for item in catalog.get("items") or [] if isinstance(item, dict)]
        except Exception:
            items = []  # never read: resolve_words says 合约目录暂时读不到 (#932 F4)
        reason = f"{'语音' if decoded.origin == 'voice' else '打字'}：{decoded.user_text.strip()}"[:200]
        resolved = resolve_words(replace(order, reason=reason), items, chart)
        if isinstance(resolved, OrderIntent) and resolved.bare and side_unsettled(resolved, await tools.account_snapshot()):
            return None  # 卖一手 with the long side held: 开空还是平多？ — the trading AI's order round
        names = catalog_names(items)
        if isinstance(resolved, Refuse):
            outcome: Outcome = resolved
        else:
            outcome = await tools.plan_order(resolved, mode=str(session["mode"]), session_id=session_id,
                                             turn_id=turn_id, user_id=user_id, origin="ai", intent_source="parser")
        if isinstance(outcome, Cards):
            lines = []
            for card in outcome.cards:
                await self._publish(session_id, "approval", card.event, card.proposal)
                # 一句多单：each card is said by its own leg (its own price words; a relay's first card is the first leg's).
                lines.append(card_line(card.source or (order.legs[0] if order.legs else order), card.event,
                                       card.proposal, names))
            line = lines[0] if len(lines) == 1 else f"一共 {len(lines)} 张卡，各自确认。" + "".join(
                f"第 {index} 张：{text}" for index, text in enumerate(lines, 1))
            line += other_legs_line(outcome.notes)
        elif isinstance(outcome, Ask):
            choices = ask_choices(resolved, outcome, lambda symbol: names.get(symbol.lower()) or symbol) \
                if isinstance(resolved, OrderIntent) else []
            line = await self._publish_question(session_id, turn_id, {
                "missing": outcome.missing, "question": outcome.question, "options": choices})
        else:
            facing = outcome.facing() or {}
            issues = [str(issue) for issue in facing.get("candidateIssues") or [] if str(issue).strip()]
            line = (refusal_line(issues or [str(facing.get("candidateStatus") or "")])
                    if outcome.form != "unavailable" else "这会儿出不了卡，稍后再说一遍。")
        await self._publish_routed_line(session_id, decoded, line, run_id, turn_id=turn_id)
        return line

    def _bridge_call(self, user_id: str, session_id: str) -> Callable[..., Awaitable[dict[str, Any]]] | None:
        """The AppCapabilities bridge runner (operation, path=, query=, body=) the shared window executors take —
        the voice service passes its own `_call`; None when no app bridge is wired."""
        capabilities = self.trader_tools.app_capabilities
        if capabilities is None:
            return None

        async def call(operation: str, *, path=None, query=None, body=None) -> dict[str, Any]:
            from .agent_api import AppCall
            result = await capabilities.call(
                AppCall(operation=operation, path=path or {}, query=query or {}, body=body),
                user=user_id, session_id=session_id)
            return result if isinstance(result, dict) else {"data": result}
        return call

    async def _execute_editor_ask(self, decoded: DecodedInstruction, *, session_id: str) -> str:
        """补一句 on the open 策略编辑卡 through the shared `run_editor_ask` (the voice's too). Returns the line to publish."""
        from .strategy_editor import EDITOR_ASK_FAILED, EDITOR_ASK_LINE
        session = self.store.get_session(session_id)
        user_id = str(session["userId"])
        call = self._bridge_call(user_id, session_id)
        line = EDITOR_ASK_FAILED
        if call is not None and decoded.ui_window_id:
            try:
                window = await self.trader_tools._ui_window(decoded.ui_window_id, user_id=user_id,
                                                            session_id=session_id, task_id=None)
                if await run_editor_ask(decoded.user_text, window=window, call=call):
                    line = EDITOR_ASK_LINE
            except Exception as error:
                log.info("agent_editor_ask_failed session=%s reason=%s", session_id, error)
        return line

    @staticmethod
    def _tool_payload(response: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(str((response.get("contentItems") or [{}])[0].get("text") or "{}"))
        except (ValueError, TypeError, AttributeError, IndexError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _tool_evidence(cls, name: str, arguments: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """One record of what this turn read: the tool, that it succeeded, what it was about and how
        fresh the data was. Kept on the turn and logged, so "已核验" can be checked afterwards."""
        data = cls._tool_payload(response).get("data")
        record: dict[str, Any] = {"tool": name, "ok": True}
        scopes: list[Any] = [arguments.get("symbol"), arguments.get("query")]
        if isinstance(data, dict):
            scopes.append(data.get("symbol"))
            for key in ("capturedAt", "ageSeconds", "quoteDatetime", "asOf"):
                value = data.get(key)
                if value is None:
                    nested = next((sub.get(key) for sub in data.values()
                                   if isinstance(sub, dict) and sub.get(key) is not None), None)
                    value = nested
                if value is not None and key not in record:
                    record[key] = value
        scope = next((str(item) for item in scopes if item), None)
        if scope is not None:
            record["scope"] = scope[:80]
        return record

    @classmethod
    def _account_after_read(cls, response: dict[str, Any]) -> bool:
        data = cls._tool_payload(response).get("data")
        after = data.get("accountAfter") if isinstance(data, dict) else None
        return isinstance(after, dict) and after.get("available") is True

    @classmethod
    def _account_write_read(cls, name: str, arguments: dict[str, Any], response: dict[str, Any]) -> bool | None:
        """For a successful account write — called directly or through app_call's `tool:` operation (the
        same native tool, its result nested one level down) — whether its accountAfter re-read the account;
        None for anything else (#872, #900 review)."""
        if response.get("success") is not True:
            return None
        if name in ACCOUNT_STATE_WRITE_TOOLS:
            return cls._account_after_read(response)
        if name != "app_call" or not isinstance(arguments, dict):
            return None
        try:
            request = json.loads(str(arguments.get("requestJson") or "{}"))
        except (TypeError, ValueError):
            return None
        operation = request.get("operation") if isinstance(request, dict) else None
        if not (isinstance(operation, str) and operation.startswith("tool:")
                and operation.removeprefix("tool:") in ACCOUNT_STATE_WRITE_TOOLS):
            return None
        outer = cls._tool_payload(response).get("data")
        if not isinstance(outer, dict) or outer.get("ok") is not True:
            return None
        inner = outer.get("data")
        data = inner.get("data") if isinstance(inner, dict) else None
        after = data.get("accountAfter") if isinstance(data, dict) else None
        return isinstance(after, dict) and after.get("available") is True

    @classmethod
    def _error_code(cls, response: dict[str, Any]) -> str | None:
        """The errorCode inside a tool response's JSON text, if any."""
        return cls._tool_payload(response).get("errorCode")

    @staticmethod
    def _route_refusal(output: str) -> tuple[str, str] | None:
        """(errorCode, userFacing line) when a card tool refused with copy for the user, else None.
        Read-only tools fail without userFacing text, so their turns keep the model loop as before.
        The published line carries the issues alone — the candidateStatus prefix said nothing the
        issues did not already say (fuzzy P1: 生硬且多余)."""
        try:
            payload = json.loads(output)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        code = payload.get("errorCode")
        user_facing = (payload.get("data") or {}).get("userFacing")
        if not code or not isinstance(user_facing, dict):
            return None
        status = str(user_facing.get("candidateStatus") or "").strip()
        issues = [str(issue).strip() for issue in user_facing.get("candidateIssues") or [] if str(issue).strip()]
        if not status and not issues:
            return None
        return str(code), "；".join(issues) or status

    @staticmethod
    def _plain_closing_text(text: str) -> str:
        """The closing round's prose as the user's answer — the fixed honest line instead when the
        model wrote its tool calls into the text (DeepSeek's DSML and the <tool_call> family), which
        is markup, not an answer (#721). Ordinary closing prose passes through unchanged."""
        if any(marker in text for marker in TOOL_CALL_MARKUP_MARKERS):
            return TOOL_MARKUP_CLOSING_LINE
        return text

    @staticmethod
    def _route_instrument_miss(call: _ToolCall, output: str) -> tuple[str, str] | None:
        """(instrument_not_found, honest line) when a search_instruments call succeeded with zero
        items: the product is not in the catalog, and over a narrowed tool subset the model can only
        repeat the same search until the turn dies as “未完成。”. The line names the query word the
        call used, the same shape as the order planner's own refusal."""
        if call.name != "search_instruments":
            return None
        try:
            payload = json.loads(output)
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("errorCode"):
            return None
        data = payload.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or items:
            return None
        try:
            query = str((json.loads(call.arguments) if call.arguments else {}).get("search") or "").strip()
        except ValueError:
            query = ""
        named = f"叫“{query}”的合约" if query else "匹配的合约"
        return "instrument_not_found", f"没找到{named}，请说品种名或代码"

    def _empty_search_refusal(self, turn: _Turn, call: _ToolCall, output: str) -> tuple[str, str] | None:
        """An empty search_instruments result ends the turn honestly instead of looping: immediately
        on a one-round or market_query route, and on a general route (typed or delegated voice) only
        after the second empty search — the first may just be a poor word the model rephrases."""
        miss = self._route_instrument_miss(call, output)
        if miss is None:
            if call.name == "search_instruments":
                turn.zero_result_searches = 0  # a search that found something resets the streak
            return None
        turn.zero_result_searches += 1
        limit = 2 if turn.route is not None and turn.route.kind == "general" else 1
        return miss if turn.zero_result_searches >= limit else None

    def _proposal_status_line(self, session_id: str) -> str:
        """What became of this conversation's recent proposal cards. History only keeps question and
        answer, so without this the model reads its own "等你确认" and believes a card the user
        abandoned (or the voice call timed out) is still waiting — and refuses to make a new one."""
        try:
            recent = self.store.recent_proposals(session_id, limit=5)
        except Exception:
            return ""
        if not recent:
            return ""
        from .proposals import awaiting_confirmation
        proposals = self.trader_tools.proposals
        def describe(record: dict[str, Any]) -> str:
            payload = record.get("payload") or {}
            what = payload.get("name") or payload.get("symbol") or record.get("proposalType") or "方案"
            detail = f"{payload.get('side', '')} {payload.get('volume', '')}手".strip() if payload.get("volume") else ""
            state = self.PROPOSAL_STATUS_LABELS.get(str(record.get("status")), record.get("status"))
            if record.get("status") == "approval_required" and not awaiting_confirmation(record):
                state = "确认已超时，已作废"   # the 30 s sweep has not marked it yet
            if record.get("proposalType") == "close_batch":
                what, detail = "批量平仓", f"{payload.get('totalOrders', '')}笔"
                # A confirmed batch says what its orders became, not that it was "sent":
                # some may still be unsent after an interrupted confirmation.
                state = (proposals and proposals.close_batch_state(record)) or state
            if record.get("proposalType") == "spread":
                what, detail = "价差", str(payload.get("legsWords") or "")
            return f"{what} {detail}（{state}）".replace("  ", " ")
        pending = [item for item in recent if awaiting_confirmation(item)]
        if pending:
            # #756: the typed turn can retire a card the way its 取消 button does (reject_proposal, the
            # same route and Owner); it can never confirm one.
            cards = "；".join(f"{describe(item)} proposalId={item['id']} proposalVersion={item['version']}"
                             for item in pending)
            return ("系统说明：本对话有 " + str(len(pending)) + " 张方案卡仍在等待用户确认：" + cards
                    + "。用户确认前不要重复生成同一笔。"
                    "用户明确说取消/算了/不要了/放弃这张卡时，调用 reject_proposal（照抄上面的 proposalId 与 proposalVersion）作废它；"
                    "一句话里既取消卡又要做别的（如“取消这张卡，切到日线”），先作废卡，再做其余部分。"
                    "只按工具回执汇报：返回 status=rejected 才说已作废，被拒就照原因说，没调用或本轮没有这个工具时请用户点卡上的“取消”，不能说已经作废。"
                    "你不能替用户确认下单：确认只能用户自己点卡上的“确认”或在语音里说“确认”。\n")
        return ("系统说明：本对话此前的方案卡都已结束——" + "；".join(describe(item) for item in recent)
                + "。现在没有等待确认的方案卡；用户再要求下单或平仓时必须重新调用工具生成新方案卡，不要说“卡还在等确认”。\n")

    def _routine_status_line(self, session_id: str, user_id: str) -> str:
        """What became of this conversation's 流程卡 (the user's 保存 on the card, a later 改名 / 删除 in
        设置). Like the proposal cards: history keeps only the model's own "点保存才算存下", so without this
        it tells the user to save a routine they already saved (10-06 实测)."""
        try:
            drafts = self.store.session_routine_drafts(user_id, session_id)
            if not drafts:
                return ""
            routines = {item["id"]: item["name"] for item in self.store.list_routines(user_id)}
        except Exception:
            return ""
        names = set(routines.values())

        def state(draft: dict[str, Any]) -> str:
            if draft.get("refusal"):
                return "不能保存"
            if not draft.get("savedAt"):
                return "还没保存（要用户点卡上的“保存”）"
            routine_id = draft.get("routineId")
            if routine_id and routine_id in routines:
                current = routines[routine_id]
                return "已保存" if current == draft["name"] else f"已保存，后来改名为「{current}」"
            if not routine_id and draft["name"] in names:
                return "已保存"
            return "已保存，后来在设置里改名或删除了"
        lines = "；".join(f"「{draft['name']}」{state(draft)}" for draft in drafts)
        saved = "、".join(f"「{name}」" for name in routines.values()) or "没有"
        return (f"系统说明：本对话的流程卡——{lines}。用户现在保存的流程：{saved}。"
                "已保存的不要再说还要点保存。\n")

    def _load_history(self, session_id: str) -> list[dict[str, Any]]:
        if session_id in self._history_loaded:
            return self._history.setdefault(session_id, [])
        items: list[dict[str, Any]] = []
        pending_user: dict[str, Any] | None = None
        for _, event in self.store.list_events(session_id):
            if event.get("name") != "message.completed":
                continue
            payload = event.get("payload") or {}
            turn_id = str(payload.get("turnId") or "")
            text = str(payload.get("text") or "")
            if payload.get("role") == "user" and turn_id.endswith("-user"):
                # An unanswered question (cancelled or failed turn) is not history.
                pending_user = {"role": "user", "content": text}
            elif payload.get("role") == "assistant" and pending_user is not None and text and not payload.get("ended"):
                items.append(pending_user)
                items.append({"role": "assistant", "content": text})
                pending_user = None
        self._history[session_id] = items
        self._history_loaded.add(session_id)
        return items

    @staticmethod
    def _estimate_tokens(items: list[dict[str, Any]]) -> int:
        # One character per token is the conservative bound for Chinese text.
        return sum(len(json.dumps(item, ensure_ascii=False)) for item in items)

    @staticmethod
    def _turn_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """History split at every user message: a question together with the reasoning, function_call,
        function_call_output and answer items that belong to it. Compaction and the request check move
        whole groups, because a function_call_output whose function_call was dropped is a malformed
        request, and a function_call without its output asks the model to answer a call nobody ran."""
        groups: list[list[dict[str, Any]]] = []
        for item in items:
            if item.get("role") == "user" or not groups:
                groups.append([])
            groups[-1].append(item)
        return groups

    async def _compact_history(self, session_id: str, endpoint: ModelEndpoint, *, turn: _Turn | None = None) -> None:
        items = self._history.setdefault(session_id, [])
        if self._estimate_tokens(items) <= HISTORY_TOKEN_BUDGET:
            return
        kept = self._turn_groups(items)
        dropped: list[dict[str, Any]] = []
        while len(kept) > 1 and self._estimate_tokens(
                [item for group in kept for item in group]) > HISTORY_TOKEN_BUDGET // 2:
            dropped.extend(kept.pop(0))
        if not dropped:
            return
        transcript = "\n".join(
            f"{item.get('role', 'tool')}: {str(item.get('content') or item.get('output') or item.get('arguments') or '')[:2000]}"
            for item in dropped)
        try:
            summary = await asyncio.wait_for(self._complete_text(
                endpoint, instructions="用中文把这段对话压缩成不超过300字的事实摘要，只保留用户问过什么、得到的结论和数字。",
                input_text=transcript, meter=turn), timeout=SUMMARY_TIMEOUT_SECONDS)
        except Exception:
            # Nothing is removed until there is a summary to put in its place; the old code had
            # already deleted the turns by now and lost them when the summary call failed.
            return
        items[:] = [{"role": "user", "content": f"（此前对话摘要）{summary}"},
                    *(item for group in kept for item in group)]
        log.info("agent_history_compacted session=%s dropped_items=%d kept_groups=%d",
                 session_id, len(dropped), len(kept))

    # -- turns ---------------------------------------------------------------

    async def _publish_routed_line(
        self, session_id: str, decoded: DecodedInstruction, line: str, run_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        """One deterministic assistant line published to the session: the typed
        router paths that skip the model turn (window intents, the 策略编辑卡's 补一句, the
        registry's unified refusal lines) all close through here. `run_id` links
        the line to the turn that produced it, so a chat:send collector waiting on
        that run can tell this answer from a reminder that landed in between."""
        answer = {"turnId": turn_id or str(uuid4()), "role": "assistant", "agentId": "native", "text": line,
                  **({"runId": run_id} if run_id else {}),
                  **({"origin": "voice", "spoken": line} if decoded.origin == "voice" else {})}
        self._notify_turn_text(session_id, "message.completed", answer)
        await self.broker.publish(session_id, kind="message", name="message.completed", payload=answer)

    # -- 我的流程 (2026-10-06) -----------------------------------------------------------------------

    ROUTINE_STEP_ROUNDS = 4

    def _routine_for(self, user_id: str, text: str) -> dict[str, Any] | None:
        """The user's routine this sentence runs (exactly its name, or "跑一下 / 运行 + name"); guests and
        replays have none."""
        if user_id.startswith("guest:") or getattr(self.data_provider, "identifier", None):
            return None
        try:
            names = self.store.routine_names(user_id)
        except Exception:
            return None
        name = routine_request(text, names) if names else None
        return self.store.get_routine(user_id, names[name]) if name else None

    def _routine_by_id(self, user_id: str, routine_id: str) -> dict[str, Any] | None:
        """The routine the user picked (设置里的“运行”, POST /api/routines/{id}/run — #522): by its id, so a
        name that another routine's "跑一下 X" also matches never runs the wrong one."""
        if user_id.startswith("guest:") or getattr(self.data_provider, "identifier", None):
            return None
        try:
            return self.store.get_routine(user_id, routine_id)
        except Exception:
            return None

    def _routine_draft_line(self, user_id: str, text: str) -> str:
        """The instruction of a turn that defines ("以后我说『老规矩』就……") or changes ("老规矩去掉回测那步")
        a routine: either way the answer is a 流程卡, made by propose_routine."""
        if user_id.startswith("guest:") or "propose_routine" in self._hidden_tools(user_id):
            return ""
        try:
            routines = self.store.list_routines(user_id)
        except Exception:
            routines = []
        target = routine_edit_target(text, routines)
        if target is not None:
            return routine_edit_instruction(target, {kind: label for kind, (label, _) in ROUTINE_STEP_KINDS.items()})
        return ROUTINE_DEFINE_INSTRUCTION if routine_define_request(text) else ""

    @staticmethod
    def _routine_tool_refusal(run: _RoutineRun, name: str, arguments: dict[str, Any]) -> str | None:
        """Why a routine step may not run this tool (None = it may): only the step's own tools, and
        app_call only for the step type's listed operations."""
        step = run.routine["steps"][run.index]
        if name not in run.allowed:
            return json.dumps({"success": False, "errorCode": "routine_step_tool",
                               "userFacing": f"这一步（{ROUTINE_STEP_KINDS[step['kind']][0]}）不能用 {name}；"
                                             f"只用这一步提供的工具：{'、'.join(sorted(run.allowed)) or '无'}"},
                              ensure_ascii=False)
        if name == "app_call":
            try:
                operation = str(json.loads(str(arguments.get("requestJson") or "{}")).get("operation") or "")
            except (TypeError, ValueError):
                operation = ""
            if operation not in run.app_operations:
                return json.dumps({"success": False, "errorCode": "routine_step_operation",
                                   "userFacing": f"这一步只能调用 {'、'.join(sorted(run.app_operations)) or '（无）'}"},
                                  ensure_ascii=False)
        return None

    def _asks_for_strategy(self, turn: _Turn, strategy_id: str) -> bool:
        """Whether the user's words this turn ask to start this strategy (#953 Codex, #932 复核): a command to start it
        (`commands_start`: 启动 / 上线 / 恢复 / 改成自动下单…, never a question, advice, negation, condition, report or
        take-back) naming it — by its name, or as 策略 / 监控 while naming none of the user's other strategies — or the
        confirmation (确认 / 好 / 可以…, the parser's words) of the AI's last answer when that answer named this
        strategy. Mentioning, introducing or judging a strategy asks for no start; no new authorization words."""
        text = turn.user_text or ""
        try:
            owner = str(self.store.get_session(turn.session_id)["userId"])
            name = str(self.store.get_strategy_draft(strategy_id, user_id=owner).get("name") or "").strip()
            others = [str(head.get("name") or "").strip() for head in
                      self.store.list_strategy_draft_heads(user_id=owner, limit=50) if head.get("strategyId") != strategy_id]
        except (KeyError, TypeError):
            name, others = "", []
        if commands_start(text):
            if name and name in text:
                return True
            if ("策略" in text or "监控" in text) and not any(other and other in text for other in others):
                return True
        reading = parse(text, ParseContext(pending_card=True))
        if not (name and isinstance(reading, OrderIntent) and reading.act == "confirm"):
            return False
        answers = [str(item["content"]) for item in self._history.get(turn.session_id, [])[:turn.history_mark]
                   if item.get("role") == "assistant" and isinstance(item.get("content"), str)]
        # A22 (#932, owner 10-19): the answer must have asked to start this strategy (“要用自动下单上线吗？”) — an
        # answer that only described or judged it is no pending start request, and a bare 好 to it starts nothing.
        # The question is bound to this strategy: its own sentence names it, or refers back (它 / 这个策略) in an
        # answer naming no other of the user's strategies (“螺纹均线表现平稳。要启动沪金突破吗？” asks about 沪金突破).
        if not answers or name not in answers[-1]:
            return False
        last = answers[-1]
        # Fail closed (A22 review rounds): only the answer's final sentence counts (a question withdrawn later —
        # “要启动螺纹均线吗？算了，先不要启动。” — leaves none), Markdown emphasis and spaces are ignored, and the
        # question must name this strategy itself (它 / 这个策略 or no name can point at several). Anything else: the
        # user says the start.
        plain = _plain(last)
        spans = _sentences(plain)
        if not spans:
            return False
        start, end = spans[-1]
        sentence = plain[start:end + 1]
        if not sentence.endswith(("？", "?")):
            return False
        asked = re.split(r"[，,；;：:]", sentence)[-1]
        def mentions_other(text: str) -> bool:
            return any(_plain(other) and other not in name and _plain(other) in text for other in others)
        if mentions_other(asked):
            return False
        if not _plain(name):
            return False  # a name made only of Markdown marks or spaces can never be said
        target = re.escape(_plain(name))
        # The name must fill the name slot itself (把<名字>… or …<动词><名字>), never be read out of the verb
        # phrase (a strategy called 自动下单 is not named by “要自动下单吗？”).
        named = re.compile(_START_LEAD + rf"(?:用(?:自动下单|出卡确认|只提醒)(?:的?方式)?|把(?P<before>{target}))?"
                           + _START_VERBS + rf"(?P<after>{target})?(?:运行|监控)?(?:吗|呢)?[？?]$")
        match = named.search(asked)
        return match is not None and (match.group("before") is not None or match.group("after") is not None)

    @staticmethod
    def _read_only_app_call(arguments: dict[str, Any]) -> bool:
        """Whether an app_call's operation only reads (the catalog's own read-only judgement)."""
        from .agent_api import operation_is_read_only
        try:
            operation = str(json.loads(str(arguments.get("requestJson") or "{}")).get("operation") or "")
        except (TypeError, ValueError, AttributeError):
            return False
        return bool(operation) and operation_is_read_only(operation)

    @staticmethod
    def _ui_tool_refusal(name: str, arguments: dict[str, Any]) -> str | None:
        """Enforce the UI round's offered tools and the UI Owner's operation boundary before execution."""
        if name not in UI_FALLBACK_TOOLS:
            return json.dumps({"success": False, "errorCode": "ui_only_tool",
                               "userFacing": "本轮只处理界面操作，不能使用业务或出卡工具。"}, ensure_ascii=False)
        if name == "app_call":
            try:
                operation = str(json.loads(str(arguments.get("requestJson") or "{}")).get("operation") or "")
            except (TypeError, ValueError, AttributeError):
                operation = ""
            if operation not in UI_AGENT_OPERATIONS:
                return json.dumps({"success": False, "errorCode": "ui_only_operation",
                                   "userFacing": "本轮只能查看界面现场或发送正式界面命令，没有执行业务操作。"},
                                  ensure_ascii=False)
        return None

    @classmethod
    def _escalated_tool_refusal(cls, name: str, arguments: dict[str, Any], said: str = "") -> str | None:
        """#523 P1: a UI round that attempted no write goes on as the general turn so it can read and answer
        (a clarification, a question), but the sentence was interface wording — the escalation must not hand
        it the business writes the UI round refused. Interface tools/operations, reads (the catalog's own
        read-only judgement) and the answer card run; anything else (orders and cards, protections, switches,
        strategies, drawings) is refused before it runs. The user's own settings are the exception: “打开语音免确认”
        “关闭自动读屏” are interface wording for a settings change (update_preferences, which the owner lets the AI
        make on the user's word, 2026-10-13); refusing it left the user rephrasing three times (owner 2026-10-19). A
        trading control in the patch (AI 自主执行 / 语音下单免口头确认) runs only when the sentence names it
        (PR #1004 review: “打开持仓清单” must never switch them because the model wrote the patch)."""
        from .agent_api import operation_is_read_only
        if name in {"activate_skill", "read_skill_resource", ANSWER_CARD_TOOL} or name in LIVE_FACT_TOOLS:
            return None
        if name == "update_preferences" and cls._preferences_said(arguments, said):
            return None
        if cls._ui_tool_refusal(name, arguments) is None:
            return None
        if cls._read_only_app_call(arguments) if name == "app_call" else operation_is_read_only(f"tool:{name}"):
            return None
        return json.dumps({"success": False, "errorCode": "ui_scope_write",
                           "userFacing": "这句话是按界面请求处理的，本轮只能查看、回答和改设置，不能下单、改保护、开关或改策略；"
                                         "要做请直接说要做什么。"}, ensure_ascii=False)

    @staticmethod
    def _preferences_said(arguments: dict[str, Any], said: str) -> bool:
        """Whether an escalated UI round's preference patch is the user's to make: every trading control it touches
        is named in the sentence (语音下单免口头确认 by “确认”, AI 自主执行 by “自主”); other settings pass."""
        from .ui_control import TRADE_CONTROL_KEYS
        changes = arguments.get("changes")
        trade = changes.get("trade") if isinstance(changes, dict) else None
        touched = TRADE_CONTROL_KEYS & set(trade) if isinstance(trade, dict) else set()
        words = {"voiceOrderConfirm": "确认", "aiAutonomy": "自主"}
        return all(words[key] in (said or "") for key in touched)

    async def _execute_routine_turn(
        self, session_id: str, decoded: DecodedInstruction, routine: dict[str, Any], run_id: str, *,
        user_id: str | None, model_slug: str | None, history: list[dict[str, Any]], started: float,
    ) -> None:
        """Run a routine as one turn: its steps in order, each over its own tools only, with the
        progress card (`card.routine_run`) republished as each step starts and ends. A failed step stops
        the run there; the cards earlier steps made stay confirmable. No step can place an order: the
        tools that could are never offered, and a card step's proposal waits for the user's 确认."""
        endpoint = self._endpoint_for(model_slug)
        session = self.store.get_session(session_id)
        owner = str(session["userId"])
        try:
            tools = self._turn_tools(session)
        except AccountNotAssembled as error:
            await self.broker.publish(session_id, kind="error", name="error", payload={
                "code": "account_not_assembled",
                "message": "这个会话绑定的交易账户当前未装配，本轮对话已停止。",
                "scope": "agent"})
            raise NativeAgentError("account_not_assembled") from error
        turn = _Turn(session_id=session_id, run_id=run_id, turn_id=str(uuid4()), mode=str(session["mode"]),
                     gated=False, task={}, model=endpoint.slug, origin=decoded.origin, tools=tools,
                     user_text=decoded.user_text, routine=_RoutineRun(routine=routine), started_at=started)
        turn.task = self._new_task(turn)
        turn.task["title"] = f"流程「{routine['name']}」"
        self._turns[run_id] = turn
        self._task_cards[run_id] = deepcopy(turn.task)
        await self._compact_history(session_id, endpoint, turn=turn)
        history_mark = len(history)
        history.append({"role": "user", "content": decoded.user_text})
        turn.history_mark = history_mark
        overview = "；".join(f"{index}. {step['text']}" for index, step in enumerate(routine["steps"], start=1))
        turn.model_text = (self._clock_line()
                           + self._preference_line(owner, origin=None if decoded.push_to_talk else decoded.origin)
                           + self._memory_line(owner, decoded.user_text)
                           + f"用户在运行他的流程「{routine['name']}」，共 {len(routine['steps'])} 步：{overview}。"
                             "系统会一步一步交给你做；每一步只做那一步。\n"
                           + decoded.user_text)
        available = {definition["name"] for definition in tools.definitions()} - self._hidden_tools(owner)
        states: list[dict[str, Any]] = [{"status": "waiting", "result": None} for _ in routine["steps"]]
        started_at = utc_now()
        clock = monotonic()
        try:
            self.store.mark_routine_run(owner, routine["id"])
        except Exception:
            log.exception("routine_mark_run_failed run_id=%s", run_id)

        async def publish_card(status: str, finished: bool = False) -> None:
            await self._publish(session_id, "artifact", "card.routine_run", routine_run_card(
                turn_id=turn.turn_id, routine=routine, states=states, status=status, started_at=started_at,
                elapsed=monotonic() - clock, finished_at=utc_now() if finished else None))

        status = "failed"
        outcome = "completed"
        try:
            await self._publish(session_id, "progress", "task.started", deepcopy(turn.task))
            await publish_card("running")
            async with asyncio.timeout(self.turn_timeout_seconds):
                for index, step in enumerate(routine["steps"]):
                    run = turn.routine
                    assert run is not None
                    run.index = index
                    manner = routine_step_manner(step)
                    advisory = manner == "advisory"
                    run.allowed = routine_step_tools(step["kind"], available, advisory=advisory)
                    run.declared = run.allowed | (run.used & available)
                    run.app_operations = routine_step_operations(step["kind"], advisory=advisory)
                    run.calls = []
                    states[index] = {"status": "active", "result": None}
                    await publish_card("running")
                    text = ""
                    if run.allowed:
                        history.append({"role": "user", "content": routine_step_instruction(routine, index)})
                        for _ in range(self.ROUTINE_STEP_ROUNDS):
                            round_result = await self._stream_round(endpoint, turn, history, user_id)
                            if not round_result.calls:
                                text = visible_answer(round_result.text)[0]
                                break
                            history.extend(round_result.reasoning_items)
                            for call in round_result.calls:
                                history.append({"type": "function_call", "call_id": call.call_id,
                                                "name": call.name, "arguments": call.arguments or "{}"})
                            for call in round_result.calls:
                                output = await self._execute_tool(turn, call, user_id)
                                history.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
                            # A failed market read ends a plain turn; in a routine it is this step's failure.
                            turn.market_unavailable = False
                        else:
                            closing = await self._stream_round(endpoint, turn, history, user_id, no_tool_calls=True)
                            text = visible_answer(closing.text)[0]
                        history.append({"role": "assistant", "content": text or "（这一步没有回复）"})
                        state, line = routine_step_outcome(step["kind"], text, run.calls, judged=manner is not None)
                    else:
                        state, line = "failed", "这一步要用的功能现在不可用"
                    states[index] = {"status": state, "result": line}
                    if state == "failed":
                        outcome = "stopped"
                        for later in range(index + 1, len(states)):
                            states[later] = {"status": "skipped", "result": None}
                        break
                    await publish_card("running")
            status = "completed"
        except asyncio.CancelledError:
            status = outcome = "cancelled"
            self._cancel_backtest_lease(run_id)
            raise
        except TimeoutError as error:
            self._cancel_backtest_lease(run_id)
            outcome = "stopped"
            await self._finish_task(turn, "failed", detail="流程超时")
            raise NativeAgentError("agent_turn_timeout") from error
        except NativeAgentError as error:
            outcome = "stopped"
            await self._finish_task(turn, "failed", detail=self.error_payload(error, run_id)["message"])
            raise
        finally:
            await self._wait_for_tool_tasks(run_id)
            for index, entry in enumerate(states):
                if entry["status"] == "active":
                    states[index] = {"status": "failed", "result": "已停止" if outcome == "cancelled" else "没做完"}
                elif entry["status"] == "waiting":
                    states[index] = {"status": "skipped", "result": None}
            try:
                await publish_card(outcome, finished=True)
            except Exception:
                log.exception("routine_card_publish_failed run_id=%s", run_id)
            # The conversation keeps the user's words and the run's one-line result; the steps' tool
            # items are this turn's working notes only.
            del history[history_mark:]
            if status == "completed":
                history.append({"role": "user", "content": decoded.user_text})
            if status != "failed":
                await self._finish_task(turn, status)
            self._record_turn_metrics(turn, user_id=owner, status=status, elapsed_ms=(monotonic() - started) * 1000)
            log.info("agent_turn run_id=%s route=routine routine=%s status=%s outcome=%s elapsed_ms=%.1f",
                     run_id, routine["id"], status, outcome, (monotonic() - started) * 1000)
            self._turns.pop(run_id, None)
        summary = routine_run_summary(routine, states)
        answer = {"turnId": turn.turn_id, "role": "assistant", "agentId": "native", "text": summary,
                  **self._voice_fields(turn, summary)}
        async with self._usage_status_follows_publish(run_id):
            await self._publish(session_id, "message", "message.completed", answer)
        history.append({"role": "assistant", "content": summary})

    async def handle_message(
        self, session_id: str, text: str, run_id: str, *,
        user_id: str | None = None, model_slug: str | None = None,
    ) -> None:
        started = monotonic()
        decoded = decode_instruction(text)
        if getattr(self.data_provider, "identifier", None):
            decoded = replace(decoded, requires_web_search=False,
                              model_text="当前为独立历史回放，忽略下文关于当前新闻检索的通用要求。只读取本回放事实。" + decoded.model_text)
        self._session_ids.add(session_id)
        word_mounted = self._begin_mount_turn(session_id, user_id, decoded.user_text)
        # Load before publishing this turn's user bubble so a rebuild never
        # duplicates the message that is appended below.
        history = self._load_history(session_id)
        user_message = {"turnId": f"{run_id}-user", "role": "user", "text": decoded.user_text,
                        **({"origin": decoded.origin} if decoded.origin else {}),
                        # 对象级 AI: the client shows "@ label" above this message.
                        **({"focus": {"kind": decoded.focus["kind"], "label": decoded.focus["label"]}}
                           if decoded.focus else {})}
        self._notify_turn_text(session_id, "message.completed", user_message)
        await self.broker.publish(session_id, kind="message", name="message.completed", payload=user_message)
        # The reply settles this session's unanswered 需要确认 cards before anything acts on it — except a pure
        # interface turn ("把设置打开"): it answers no question, and the card keeps its choices (#753 review).
        # The deterministic router (2026-09-20): a fixed-table interface request runs without a model
        # turn at all; orders, batch closes and plain market questions run over a fixed narrowed tool
        # set with temperature 0 and minimal reasoning. Anything ambiguous stays general.
        # The 策略编辑卡 (2026-10-08): with the card open in the window, a sentence about the strategy's
        # own fields is its 补一句 (one UI command, no model turn); "新建策略" opens it as an interface intent.
        # 我的流程 (2026-10-06): the whole sentence is one of the user's routine names (or "跑一下 X"):
        # the Runtime runs the routine step by step — before every other route, since the name is the
        # user's own fixed phrase. Anything that only mentions the name is an ordinary turn.
        routine_owner = str(user_id or self.store.get_session(session_id)["userId"])
        # A run the user picked carries the routine's id (#522): it runs exactly that routine, and one
        # deleted while the run waited in the queue ends with a plain answer — never another routine
        # whose name the text happens to match, never an ordinary chat turn.
        if decoded.routine_id is not None:
            await self._settle_open_questions(session_id, decoded.user_text)
            routine = self._routine_by_id(routine_owner, decoded.routine_id)
            if routine is None:
                await self._publish_routed_line(session_id, decoded, ROUTINE_GONE_LINE, run_id)
                history.append({"role": "user", "content": decoded.user_text})
                history.append({"role": "assistant", "content": ROUTINE_GONE_LINE})
                return
        else:
            routine = self._routine_for(routine_owner, decoded.user_text)
            if routine is not None:
                await self._settle_open_questions(session_id, decoded.user_text)
        if routine is not None:
            line = self._skill_unavailable_line(routine_owner, "routines")
            if line is not None:
                await self._publish_routed_line(session_id, decoded, line, run_id)
                history.append({"role": "user", "content": decoded.user_text})
                history.append({"role": "assistant", "content": line})
                return
            await self._execute_routine_turn(session_id, decoded, routine, run_id, user_id=user_id,
                                             model_slug=model_slug, history=history, started=started)
            return
        # 新手引导 (11-01): "下一步" typed or held while the window's tour runs moves the tour — the call's rule.
        tour_line = await self._execute_tour_step(decoded, session_id=session_id)
        if tour_line is not None:
            await self._publish_routed_line(session_id, decoded, tour_line, run_id)
            history.append({"role": "user", "content": decoded.user_text})
            history.append({"role": "assistant", "content": tour_line})
            log.info("agent_turn run_id=%s route=tour status=completed elapsed_ms=%.1f", run_id, (monotonic() - started) * 1000)
            return
        product_names = await self._product_names()
        strategies_available = "explain_strategy_signal" not in self._hidden_tools(user_id)
        has_card = self._has_pending_card(session_id) or decoded.pending_card_reply
        editor_open = (not has_card and await self._editor_open(
            decoded, decoded.user_text, user_id=routine_owner, session_id=session_id, product_names=product_names))
        route = classify_turn(decoded.user_text, has_pending_card=has_card,
                              ui_available=bool(decoded.ui_window_id), catalog=product_names,
                              strategies_available=strategies_available, editor_open=editor_open)
        if route.tool_names == UI_FALLBACK_TOOLS and word_mounted:
            log.info("agent_ui_fallback_skipped run_id=%s skills=%s", run_id, ",".join(sorted(word_mounted)))
            route = GENERAL_ROUTE
        if route.kind != "ui_intent":
            await self._settle_open_questions(session_id, decoded.user_text)
        if route.kind == "editor_ask":
            # 策略编辑卡's 补一句 (2026-10-08): the card takes the sentence; never an order turn.
            line = await self._execute_editor_ask(decoded, session_id=session_id)
            await self._publish_routed_line(session_id, decoded, line, run_id)
            history.append({"role": "user", "content": decoded.user_text})
            history.append({"role": "assistant", "content": line})
            log.info("agent_turn run_id=%s route=editor_ask status=completed elapsed_ms=%.1f",
                     run_id, (monotonic() - started) * 1000)
            return
        if (not strategies_available and route.kind == "general"
                and not self._has_pending_card(session_id)
                and strategy_signal_request(decoded.user_text)):
            # The registry gate on the typed strategy-signal hit: classify_turn
            # refused to route to a hidden tool; the user still gets the one
            # unified line instead of a general model turn.
            line = self._skill_unavailable_line(user_id, "strategy-signals")
            if line is not None:
                await self._publish_routed_line(session_id, decoded, line, run_id)
                history.append({"role": "user", "content": decoded.user_text})
                history.append({"role": "assistant", "content": line})
                log.info("agent_turn run_id=%s route=strategy_signal status=skill_unavailable elapsed_ms=%.1f",
                         run_id, (monotonic() - started) * 1000)
                return
        if route.kind == "order_intent" and route.order is not None:
            # 意图重构第 3 步: an order the parser is sure of is planned here — cards, a question card or the
            # refusal's words — without a model round. None: the account says the words are not settled
            # (卖一手 with the long side held) and the trading AI reads them.
            line = await self._execute_order_intent(decoded, route.order, session_id=session_id, run_id=run_id)
            if line is not None:
                history.append({"role": "user", "content": decoded.user_text})
                history.append({"role": "assistant", "content": line})
                log.info("agent_turn run_id=%s route=order_intent act=%s status=completed elapsed_ms=%.1f",
                         run_id, route.order.act, (monotonic() - started) * 1000)
                return
            route = ORDER_ROUTE
        if route.mount:
            self._mount_skill(session_id, route.mount, by="route")
        if route.kind == "ui_intent" and route.intent is not None:
            line = await self._execute_window_intent(decoded, route.intent, session_id=session_id)
            if line is not None:
                history.append({"role": "user", "content": decoded.user_text})
                history.append({"role": "assistant", "content": line})
                log.info("agent_turn run_id=%s route=ui_intent kind=%s status=completed elapsed_ms=%.1f",
                         run_id, route.intent.kind, (monotonic() - started) * 1000)
                return
            route = GENERAL_ROUTE  # the window refused or is offline: the model turn, as before
            await self._settle_open_questions(session_id, decoded.user_text)
        # 我的流程: defining or changing a routine is a general turn whose answer is a 流程卡 — never an
        # order route (the definition names 出卡 steps) and never the steps run now.
        routine_line = self._routine_draft_line(routine_owner, decoded.user_text)
        if routine_line:
            route = GENERAL_ROUTE
            self._mount_skill(session_id, "routines", by="routine-draft")
        # 按技能延迟挂载: an object-level AI turn (its focus instruction names the object's tools) mounts the
        # skills that read and change that object.
        for skill_id in FOCUS_SKILLS.get(str((decoded.focus or {}).get("kind") or ""), ()):
            self._mount_skill(session_id, skill_id, by="focus")
        # 判断卡 (2026-10-04): an opinion question is a general model turn with the research desk's
        # judgment card attached first (below, once the turn has its id).
        opinion = route.kind == "opinion"
        if opinion:
            route = TurnRoute("general", no_card=route.no_card, hint=route.hint)  # “能不能买两手螺纹”: a view, no card
        endpoint = self._endpoint_for(model_slug)
        session = self.store.get_session(session_id)
        try:
            tools = self._turn_tools(session)
        except AccountNotAssembled as error:
            # Before the turn exists: a session bound to an account this boot no
            # longer assembles must not run on the default account's tools.
            await self.broker.publish(session_id, kind="error", name="error", payload={
                "code": "account_not_assembled",
                "message": "这个会话绑定的交易账户当前未装配，本轮对话已停止。",
                "scope": "agent"})
            raise NativeAgentError("account_not_assembled") from error
        turn = _Turn(session_id=session_id, run_id=run_id, turn_id=str(uuid4()), mode=str(session["mode"]),
                     gated=decoded.requires_web_search, task={}, model=endpoint.slug,
                     # The voice fast path yields to a one_round route (order / close_all / UI fallback):
                     # the route's fixed tool subset must reach the model, or the sentence narrows to
                     # nothing ("持仓都平了" matched the fast word 持仓 and then browsed app_call). Voice
                     # market questions keep the fast path exactly as before.
                     origin=decoded.origin, fast=decoded.fast and not route.one_round,
                     tools=tools, route=route, focus=decoded.focus, user_text=decoded.user_text,
                     started_at=started, window_id=decoded.ui_window_id)
        if (turn.forcing or turn.ui_fallback) and self._traits[endpoint.slug].get("tool_choice") == "required_without_thinking":
            turn.reasoning_override = "none"
        turn.cards = TurnCards(turn.turn_id)
        turn.answer_wanted = (decoded.focus is None and route.kind != "ui_intent" and answer_card_wanted(
            decoded.user_text, route_kind="opinion" if opinion else route.kind,
            names_product=bool(product_names) and any(len(name) >= 2 and name.lower() in decoded.user_text.lower()
                                                      for name in product_names or []),
            screen_reference=screen_reference(decoded.user_text)))
        turn.task = self._new_task(turn)
        self._turns[run_id] = turn
        self._task_cards[run_id] = deepcopy(turn.task)
        if turn.gated:
            self._gated_runs.add(run_id)
        await self._compact_history(session_id, endpoint, turn=turn)
        history_mark = len(history)
        # History keeps the plain question; the workspace context and routing
        # instructions of a turn are only sent with that turn's requests, so a
        # later turn never sees an old snapshot as "current".
        history.append({"role": "user", "content": decoded.user_text})
        turn.model_text = (self._clock_line()
                           + self._proposal_status_line(session_id)
                           + self._routine_status_line(session_id, str(session["userId"]))
                           + self._preference_line(str(session["userId"]), origin=None if decoded.push_to_talk else decoded.origin)
                           + self._memory_line(str(session["userId"]), decoded.user_text)
                           + await self._screen_snapshot_line(decoded, str(session["userId"]), session_id)
                           + (await self._account_state_line(turn, tools, str(session["userId"]))
                              if self._wants_account_state(decoded.user_text, route) else "")
                           + await self._ui_window_line(decoded, str(session["userId"]), session_id, tools,
                                                       review_only=route.tool_names != UI_FALLBACK_TOOLS)
                           + (await self._product_hint_line(decoded.user_text, tools, several=turn.answer_wanted)
                              if turn.answer_wanted or route.kind in {"order", "market_query"} else "")
                           + route.hint
                           + (OPINION_INSTRUCTION if opinion else "")
                           + (ANSWER_CARD_INSTRUCTION if turn.answer_wanted else "")
                           + routine_line
                           + decoded.model_text)
        if decoded.origin == "voice" and self.ACCOUNT_STATE_PREFIX.split("{")[0] not in turn.model_text:
            # 语音委派短上下文: the shortened history comes with the account's positions, read now through the
            # same fail-closed account read as the state line (stale or unreadable = no line at all).
            turn.model_text = await self._voice_positions_line(tools, str(session["userId"]), mode=turn.mode) \
                + turn.model_text
        turn.history_mark = history_mark
        status = "failed"
        final_text: str | None = None
        incomplete = False
        if opinion:
            try:
                await self._attach_judgment_card(turn, decoded, user_id=str(session["userId"]),
                                                 product_names=product_names, tools=tools)
            except Exception:  # the card is an extra: the answer goes on without it
                log.exception("agent_judgment_card_failed run_id=%s", run_id)
        try:
            if not turn.gated:
                await self._publish(session_id, "message", "message.delta", {
                    "turnId": turn.turn_id, "role": "assistant", "text": PROCESSING_PLACEHOLDER, "agentId": "native"})
            await self._publish(session_id, "progress", "task.started", deepcopy(turn.task))
            try:
                async with asyncio.timeout(self.turn_timeout_seconds):
                    rounds = await self._tool_rounds(endpoint, turn, history, user_id)
                    if rounds is None:
                        await self._escalate_ui_round(turn, history, fast=decoded.fast)
                        rounds = await self._tool_rounds(endpoint, turn, history, user_id)
                    assert rounds is not None  # a general turn has no UI round to escalate
                    final_text, incomplete = rounds
            except TimeoutError as error:
                self._cancel_backtest_lease(run_id)
                raise NativeAgentError("agent_turn_timeout") from error
            status = "completed"
        except asyncio.CancelledError:
            status = "cancelled"
            self._cancel_backtest_lease(run_id)
            raise
        except NativeAgentError as error:
            await self._finish_task(turn, "failed", detail=self.error_payload(error, run_id)["message"])
            raise
        finally:
            # Whatever ended the turn, no tool worker thread outlives it.
            await self._wait_for_tool_tasks(run_id)
            if status != "completed":
                # A cancelled or failed turn leaves no half-question behind: the
                # next request must not answer what the user already withdrew.
                del history[history_mark:]
            else:
                self._fold_tool_items(history, keep_from=history_mark)
            if status != "failed":
                await self._finish_task(turn, status)
            if status != "completed" and not turn.gated:
                # The placeholder / partial answer was streamed as message.delta and persisted: close that
                # turn, or every client reading this session keeps it "still answering" and blocks
                # 新对话 / 历史对话 / sending for good (1.6.5 web report: 停止 on a streaming turn).
                await self._close_streamed_turn(turn, status)
            self._record_turn_metrics(turn, user_id=str(session["userId"]), status=status,
                                      elapsed_ms=(monotonic() - started) * 1000)
            log.info("agent_turn run_id=%s model=%s status=%s elapsed_ms=%.1f evidence=%s account_reads=%s",
                     run_id, endpoint.slug, status, (monotonic() - started) * 1000,
                     json.dumps(turn.evidence, ensure_ascii=False),
                     json.dumps(turn.account_reads, ensure_ascii=False, default=str))
            self._turns.pop(run_id, None)
            self._gated_runs.discard(run_id)
        async with self._usage_status_follows_publish(run_id):
            await self._publish_answer(turn, final_text, incomplete=incomplete, user_id=user_id,
                                       instruction=decoded.model_text, history=history, endpoint=endpoint)

    @asynccontextmanager
    async def _usage_status_follows_publish(self, run_id: str):
        """The turn's agent_turn_usage row says "completed" before its answer is published; a publish that is
        cancelled or raises corrects it to what the run actually became (#866)."""
        try:
            yield
        except asyncio.CancelledError:
            self._amend_turn_status(run_id, "cancelled")
            raise
        except Exception:
            self._amend_turn_status(run_id, "failed")
            raise

    def _amend_turn_status(self, run_id: str, status: str) -> None:
        try:
            self.store.set_turn_status(run_id, status)
        except Exception:  # measurement must never break a turn
            log.exception("turn_usage_amend_failed")

    async def _tool_rounds(
        self, endpoint: ModelEndpoint, turn: _Turn, history: list[dict[str, Any]], user_id: str | None,
    ) -> tuple[str | None, bool] | None:
        """The turn's tool loop: (final text, incomplete). None when a heuristic UI round ended
        without attempting any interface write — the caller re-runs the turn as a general one."""
        run_id = turn.run_id
        # A one-round route (order card, batch close, UI fallback) allows two tool rounds
        # (search then card) plus the answer round; more than that means the route's fixed
        # subset failed to satisfy the model, and the loop ends as a model failure as usual.
        rounds = (UI_FALLBACK_ROUNDS if turn.ui_fallback
                  else 3 if turn.route is not None and turn.route.one_round else MAX_TOOL_ROUNDS)
        retried = False
        for index in range(rounds):
            if turn.ui_fallback and index >= 3 and not turn.ui_attempted:
                # The extra UI rounds finish a multi-step request already under way; a UI round that
                # only read for three rounds was not interface work and goes to the general turn.
                break
            forced = turn.forcing
            round_result = await self._stream_round(endpoint, turn, history, user_id)
            if not round_result.calls and forced and not retried:
                # A forced order round answered in prose (a provider that ignores tool_choice, or one that
                # complied loosely): one retry over the same tools, told plainly.
                retried = True
                log.info("agent_forced_round_retry run_id=%s route=%s", run_id, turn.route.kind if turn.route else "-")
                round_result = await self._stream_round(endpoint, turn, history, user_id, extra_input=FORCED_RETRY_LINE)
            if not round_result.calls and forced:
                # Still prose: the Runtime says no card was made — the model's words never stand in for a card
                # (“已经给你下好了” with nothing to confirm).
                turn.route_refused = True
                return await self._publish_no_card(turn, reason="模型没有调用出卡工具"), False
            if not round_result.calls:
                if not turn.ui_fallback:
                    return await self._answer_text(endpoint, turn, history, user_id, round_result.text), round_result.incomplete
                # The UI round answers only through receipts: its prose never stands in for a step
                # that did not apply. With no write attempted the sentence was not interface work
                # after all (a question, a clarification): the general turn answers it (#759/#748).
                return (self._ui_result_line(turn), False) if turn.ui_attempted else None
            history.extend(round_result.reasoning_items)
            for call in round_result.calls:
                history.append({"type": "function_call", "call_id": call.call_id,
                                "name": call.name, "arguments": call.arguments or "{}"})
            refusal: tuple[str, str] | None = None
            for call in round_result.calls:
                output = ("本轮界面操作结果未确认，不再执行后续操作。" if turn.ui_error
                          else await self._execute_tool(turn, call, user_id))
                history.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
                if refusal is None and turn.route is not None and turn.route.one_round:
                    refusal = self._route_refusal(output)
                elif refusal is None and call.name == CARD_TOOL:
                    # A general turn keeps its toolbox after a card refusal (it may search and retry
                    # with another contract), but the same refusal again is a retried fact, not progress.
                    repeated = self._route_refusal(output)
                    if repeated is not None:
                        if repeated[0] in turn.card_refusals:
                            refusal = repeated
                        turn.card_refusals.add(repeated[0])
                if refusal is None:
                    refusal = self._empty_search_refusal(turn, call, output)
            if turn.ui_error:
                return self._ui_result_line(turn), False
            if turn.market_unavailable:
                return None, False
            if turn.asked is not None:
                # propose_order came back asking (the planner's Ask): the question card is the answer; its
                # quick options come back as the user's next message.
                turn.route_refused = True
                return turn.asked, False
            if refusal is not None:
                # The card tool itself refused (no card was made), or the route's contract
                # search found nothing: the refusal's line is the honest end of the turn.
                # A model continuation over the route's fixed tool subset mostly failed
                # again and the turn ended as "未完成。".
                turn.route_refused = True
                log.info("agent_route_tool_refused run_id=%s route=%s code=%s",
                         run_id, turn.route.kind if turn.route else "-", refusal[0])
                return refusal[1], False
            cards = turn.cards
            if (cards is not None and cards.card_shown and cards.conclusion
                    and cards.facts_read and round_result.calls and not round_result.incomplete
                    and all(call.name == ANSWER_CARD_TOOL for call in round_result.calls)
                    and cards.uncarded == cards.uncarded_at_answer and not cards.partial):
                # 回答卡是这一轮的收尾 (2026-10-14 token): the card is on screen and rests on this turn's reads, so its
                # one-line conclusion is the answer text — no further model request just to write the one or two
                # sentences the card already holds. Cells without a source were already taken off the card; that is
                # the correction, not another round (去重跟进: the correction round was most of #883's extra tokens).
                # A card before any read goes back to the model, and so does a turn with a read some card shows only
                # in part (#754: the text has to say the rest); a conclusion whose numbers were not read is no card.
                log.info("agent_answer_card_closed run_id=%s dropped=%s", run_id, int(cards.last_dropped))
                if turn.first_text_ms is None:
                    turn.first_text_ms = round((monotonic() - turn.started_at) * 1000)
                return cards.conclusion, False
        # Rounds exhausted without an answer: one more round with tool_choice="none"
        # (tools still declared for the history's function items), so the model says
        # in one sentence how far it got and what is missing (fuzzy P1: these turns
        # used to die as “未完成。”). The UI round reports its receipts instead, or — with no
        # write attempted — hands the sentence to the general turn.
        if turn.ui_fallback:
            return (self._ui_result_line(turn), False) if turn.ui_attempted else None
        if turn.forcing:
            # An order round whose rounds ran out with no card, refusal or question ends in the Runtime's no-card
            # card, not a closing round the model fills with prose or tool markup.
            turn.route_refused = True
            return await self._publish_no_card(turn, reason="模型几轮都没有出卡或提问"), False
        closing = await self._stream_round(endpoint, turn, history, user_id, no_tool_calls=True)
        if not closing.text:
            raise NativeAgentError("model_request_failed")
        closing_text = self._plain_closing_text(closing.text)
        if closing_text != TOOL_MARKUP_CLOSING_LINE:
            closing_text = await self._answer_text(endpoint, turn, history, user_id, closing_text)
        return closing_text, closing.incomplete

    async def _answer_text(
        self, endpoint: ModelEndpoint, turn: _Turn, history: list[dict[str, Any]], user_id: str | None, text: str,
    ) -> str:
        """The turn's answer without the model's own thinking (2026-10-13 P1). A clean answer passes
        unchanged; one that carries thinking is rewritten once with tools off (the history holds this
        turn's tool results, never the leaked draft); a rewrite that still thinks aloud keeps only what
        precedes its first marker, and an empty remainder becomes the fixed honest line. Whatever is
        returned is what the screen, the spoken summary and the history get."""
        shown, leaked = visible_answer(text)
        if not leaked:
            return shown
        log.info("agent_thinking_leak run_id=%s chars=%d kept=%d", turn.run_id, len(text), len(shown))
        turn.thinking_retracted = False
        try:
            rewrite = await self._stream_round(endpoint, turn, history, user_id, no_tool_calls=True,
                                               extra_input=THINKING_REWRITE_LINE)
        except NativeAgentError:
            rewrite = None
        if rewrite is not None and rewrite.text:
            again, still = visible_answer(self._plain_closing_text(rewrite.text))
            if still:
                log.info("agent_thinking_leak_rewrite run_id=%s kept=%d", turn.run_id, len(again))
            return again or THINKING_FALLBACK_LINE
        # The rewrite itself failed: the draft before the thinking is suspect too (the 22:27 one stated a
        # stop that was gone), so the fixed line, not the draft.
        return THINKING_FALLBACK_LINE

    async def _escalate_ui_round(self, turn: _Turn, history: list[dict[str, Any]], *, fast: bool) -> None:
        """The heuristic UI round attempted no interface write: re-run the sentence, inside the same
        run, as the general turn it should have been — full tools, normal reasoning, the voice fast
        path as decided for the turn. The round's tool items leave the history, and its task steps
        leave the task card: they were only reads the general turn takes over, and a refused read
        left there showed as a 失败 step on a turn that went on to succeed (#779 P3)."""
        log.info("agent_ui_fallback_escalated run_id=%s", turn.run_id)
        turn.escalated = True
        del history[turn.history_mark + 1:]
        turn.reads_done.clear()  # those reads left the history with the round: the general turn may read them again
        turn.route = GENERAL_ROUTE
        turn.fast = fast
        turn.reasoning_override = None
        steps = turn.task.get("steps") or []
        understand = next((step for step in steps if step.get("id") == "understand"), None)
        if understand is not None:
            understand.update({"status": "active", "detail": "处理中"})
        turn.task["steps"] = [understand] if understand is not None else []
        turn.task["progress"] = 12
        await self._publish_task(turn)

    async def _close_streamed_turn(self, turn: _Turn, status: str) -> None:
        text = "已停止。" if status == "cancelled" else "未完成。"
        answer = {"turnId": turn.turn_id, "role": "assistant", "agentId": "native", "text": text,
                  "ended": "cancelled" if status == "cancelled" else "failed"}
        self._notify_turn_text(turn.session_id, "message.completed", answer)
        try:
            await self._publish(turn.session_id, "message", "message.completed", answer)
        except Exception:
            log.exception("agent_turn close run_id=%s failed", turn.run_id)

    async def _publish_answer(
        self, turn: _Turn, final_text: str | None, *, incomplete: bool,
        user_id: str | None, instruction: str, history: list[dict[str, Any]],
        endpoint: ModelEndpoint | None = None,
    ) -> None:
        session_id = turn.session_id
        # 回答卡片化统计: the cards this turn showed (the answer is published after the turn's usage row).
        shown = self._turn_card_ids.pop(turn.turn_id, set())
        if turn.account_unconfirmed:
            # The latest account read this turn failed (#872): said every time, not only when the model
            # happens to say it could not read — the earlier read describes the account before the write.
            wrote = any(read["write"] for read in turn.account_reads)
            await self.broker.publish(session_id, kind="warning", name="warning", payload={
                "turnId": turn.turn_id, "title": self.ACCOUNT_UNCONFIRMED_WARNING,
                "message": ("操作已执行，但之后没能重新读到账户；剩余持仓、挂单和保护以持仓面板为准。" if wrote
                            else "本轮没能读到账户；持仓、挂单和保护以持仓面板为准。"),
                "severity": "warning", "deterministic": True})
        # A voice turn hears no warning card (#900 review): what the voice reads opens with the same fact.
        voice_note = self.ACCOUNT_UNCONFIRMED_SPOKEN if turn.origin == "voice" and turn.account_unconfirmed else ""
        if turn.market_unavailable:
            self._record_turn_cards(turn, shown, numeric=False)
            text = (voice_note + "结论：实时行情还没就绪，暂时不判断这个合约的方向。\n\n"
                    "为什么：缺少最新价格和K线，新闻不能代替盘面。\n\n"
                    "现在怎么做：数据恢复前先观望。")
            unavailable = {"turnId": turn.turn_id, "role": "assistant", "agentId": "native", "text": text,
                           **self._voice_fields(turn, voice_note + "实时行情还没就绪，先观望，数据恢复了再看。")}
            await self._publish(session_id, "message", "message.completed", unavailable)
            history.append({"role": "assistant", "content": text})
            return
        if final_text is None:
            self._record_turn_cards(turn, shown, numeric=False)
            return
        # A spoken-style filler ("好的，已切到日线") the instructions already ban: stripped from the
        # published text, the spoken summary and the history the next turn sees.
        final_text = strip_filler_prefix(final_text)
        trimmed = bool(final_text) and self._read_cards_only(turn, shown)
        if trimmed:
            # 回答卡片化: the card carries the figures; the text keeps its first one or two sentences.
            short = conclusion_text(final_text)
            if short != final_text.strip():
                log.info("agent_answer_trimmed run_id=%s chars=%d kept=%d", turn.run_id, len(final_text), len(short))
            assert turn.cards is not None
            # The facts are on the cards; the sentences beside them are not parsed for claims (结构化第 3 步).
            # A text that was only a table says the shown card's conclusion.
            final_text = short or turn.cards.conclusion or "数据见上面的卡片。"
        numeric = has_numeric_claim(final_text or "")
        self._record_turn_cards(turn, shown, numeric=numeric)
        if numeric and not shown:
            log.info("agent_card_gap run_id=%s route=%s origin=%s chars=%d", turn.run_id,
                     self._turn_route_label(turn), turn.origin or "-", len(final_text or ""))
        if voice_note and final_text:
            final_text = voice_note + final_text  # the answer's first paragraph is what the voice reads
        spoken: str | None = None
        if turn.origin == "voice":
            # 语音念稿只念回答卡的一句结论 (回答卡片化); otherwise the answer's first paragraph.
            spoken = (turn.cards.spoken if turn.cards is not None and turn.cards.spoken and trimmed
                      else await self._spoken_summary(turn, endpoint, final_text))
            if voice_note and spoken and not spoken.startswith(voice_note):
                spoken = voice_note + spoken
        verified = self._search_satisfied(turn) or turn.verified
        # One footnote line, only when the answer states live facts that no tool backed this turn;
        # small talk on a gated question (weather, greetings) gets nothing. A relayed tool refusal
        # quotes the tool's own volumes (持仓 N 手) — that is evidence, not a memory assertion.
        # An unconfirmed account already has its own, more precise line.
        if turn.gated and not verified and asserts_live_facts(final_text) and not turn.route_refused \
                and not turn.account_unconfirmed:
            await self.broker.publish(session_id, kind="warning", name="warning", payload={
                "turnId": turn.turn_id, "title": "本轮未读取实时数据",
                "message": "价格、日期和新闻来自模型自身知识，说“先查一下再回答”可核验。",
                "severity": "warning", "deterministic": True})
        if incomplete:
            await self.broker.publish(session_id, kind="warning", name="warning", payload={
                "turnId": turn.turn_id, "title": "回答被截断，可以说“接着说”",
                "message": "模型输出已被长度上限截断。",
                "severity": "warning", "deterministic": True})
        answer = {"turnId": turn.turn_id, "role": "assistant", "agentId": "native", "text": final_text or "任务已完成。",
                  **self._voice_fields(turn, spoken)}
        # Through _publish, which drops a text snapshot still in the coalescing window: sent after this as a transient
        # it reached the client last and reopened the answered turn (11-02: a quote-card answer stuck "AI 正在处理…").
        await self._publish(session_id, "message", "message.completed", answer)
        history.append({"role": "assistant", "content": final_text or "任务已完成。"})

    def _record_turn_cards(self, turn: _Turn, shown: set[str], *, numeric: bool) -> None:
        """回答卡片化统计 (2026-10-14): the turn's card count and whether its answer stated figures, onto
        its agent_turn_usage row (written when the turn closed, just before the answer). Never breaks a turn.
        Read cards folded into the answer card's 依据 are not counted: the count is what stands in the conversation."""
        folded = set(turn.cards.folded) if turn.cards is not None else set()
        count = sum(entry.split("|", 1)[-1] not in folded for entry in shown)
        turn.card_count, turn.numeric_text = count, numeric
        try:
            self.store.set_turn_cards(turn.run_id, card_count=count, numeric_text=numeric)
        except Exception:  # measurement must never break a turn
            log.exception("turn_cards_record_failed")

    @staticmethod
    def _voice_fields(turn: _Turn, spoken: str | None) -> dict[str, Any]:
        return {"origin": "voice", "spoken": spoken} if turn.origin == "voice" else {}

    # The spoken part is the first paragraph as written (voice instruction: ≤5 sentences); the cap only
    # guards a model that ignores it, at a sentence end.
    SPOKEN_READ_AS_IS_CHARS = 200

    @staticmethod
    def _plain_speech(content: str) -> str:
        """Markdown → plain sentences a voice can read: headings, bullets, emphasis, links and code
        go; table rows become comma-joined phrases."""
        text = re.sub(r"```.*?```", "", content, flags=re.S)
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"https?://\S+", "", text)
        lines: list[str] = []
        for raw in text.split("\n"):
            line = raw.strip()
            if not line or re.fullmatch(r"\|?[\s:|-]+\|?", line):
                continue  # blank or a table separator row
            if line.startswith("|"):
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                line = "，".join(cell for cell in cells if cell)
            line = re.sub(r"^\s*(?:[-*•]|\d+[.、)])\s*", "", line)
            line = re.sub(r"^#{1,6}\s*", "", line)
            line = re.sub(r"[*_`>#]+", "", line).strip()
            if line:
                lines.append(line.rstrip("：:"))
        speech = "。".join(lines)
        speech = re.sub(r"[。！？!?]。", lambda m: m.group(0)[0], speech)
        return re.sub(r"。{2,}", "。", speech).strip("。 ")

    async def _spoken_summary(self, turn: _Turn, endpoint: ModelEndpoint | None, content: str) -> str | None:
        """What the voice reads: the answer's first paragraph as written (the voice instruction asks
        for a spoken first paragraph of at most five sentences). No second generation — 1.6.0–1.6.3
        condensed the answer with another model call, which cost seconds and paraphrased numbers."""
        plain = self._plain_speech(first_paragraph(content))
        return self._truncate_speech(plain, 2 * self.SPOKEN_READ_AS_IS_CHARS) or None

    @staticmethod
    def _truncate_speech(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        cut = text[:limit]
        end = max(cut.rfind("。"), cut.rfind("；"), cut.rfind("！"), cut.rfind("？"))
        return cut[:end + 1] if end > limit // 2 else cut

    def _search_satisfied(self, turn: _Turn) -> bool:
        return not turn.gated or turn.web_search_used

    @classmethod
    def _request_input(cls, history: list[dict[str, Any]], turn: _Turn) -> list[dict[str, Any]]:
        items = list(history)
        if turn.history_mark < len(items) and items[turn.history_mark].get("role") == "user":
            items[turn.history_mark] = {"role": "user", "content": turn.model_text or items[turn.history_mark]["content"]}
        if turn.origin == "voice":
            # 语音委派短上下文: the last VOICE_HISTORY_TURNS earlier turns, then this turn whole.
            earlier = cls._turn_groups(items[:turn.history_mark])[-VOICE_HISTORY_TURNS:]
            items = [item for group in earlier for item in group] + items[turn.history_mark:]
        return cls._paired_tool_items(items)

    @classmethod
    def _paired_tool_items(cls, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Last check before the request: an earlier turn whose function_call and function_call_output
        items do not pair up is dropped whole. The current turn is never dropped — it holds the
        question being answered, and its calls pair up by the time the next round is sent."""
        groups = cls._turn_groups(items)
        result: list[dict[str, Any]] = []
        for index, group in enumerate(groups):
            calls = sorted(str(item.get("call_id")) for item in group if item.get("type") == "function_call")
            outputs = sorted(str(item.get("call_id")) for item in group if item.get("type") == "function_call_output")
            if index < len(groups) - 1 and calls != outputs:
                log.warning("agent_history_group_dropped calls=%d outputs=%d items=%d",
                            len(calls), len(outputs), len(group))
                continue
            result.extend(group)
        return result

    @staticmethod
    def _fold_tool_items(history: list[dict[str, Any]], keep_from: int) -> None:
        # Earlier turns keep only their question and answer: the decision
        # context, quotes and search results they read are stale by now.
        # The turn just finished keeps its tool traffic one more turn (a follow-up may refer to it), but
        # each output only up to CARRIED_TOOL_OUTPUT_CHARS (2026-10-14 token 瘦身: a manual or catalog
        # read rode every request of the next turn whole).
        kept: list[dict[str, Any]] = []
        for index, item in enumerate(history):
            kind = item.get("type")
            if index < keep_from and kind in {"function_call", "function_call_output", "reasoning"}:
                continue
            if kind == "function_call_output":
                output = str(item.get("output") or "")
                if len(output) > CARRIED_TOOL_OUTPUT_CHARS:
                    item = {**item, "output": output[:CARRIED_TOOL_OUTPUT_CHARS] + CARRIED_TOOL_OUTPUT_CUT}
            kept.append(item)
        history[:] = kept

    async def _stream_round(
        self, endpoint: ModelEndpoint, turn: _Turn, history: list[dict[str, Any]], user_id: str | None,
        *, no_tool_calls: bool = False, extra_input: str | None = None,
    ) -> _Round:
        route = turn.route if turn.route is not None and not turn.fast else None
        request: dict[str, Any] = {
            "model": endpoint.slug,
            "instructions": self._instructions(user_id=user_id, session_id=turn.session_id),
            "input": self._request_input(history, turn),
            "stream": True, "store": False,
        }
        if turn.ui_fallback:
            request["instructions"] += ("\n本轮仅处理界面请求。用正式界面工具执行，每条命令只改一个字段；"
                "一句话要改几处（例如打开持仓面板、切到成交页签、只看 AI 下的）就把这几条命令按顺序在同一轮依次发出，"
                "每条只写 changes，windowId、revision 由 Runtime 补，不必先读窗口（版本对不上时 Runtime 会按最新版本重发一次）。"
                "applied只证明该步骤，继续处理其余步骤，不能把首个回执当整句完成。"
                "最终只汇总实际生效步骤，不能声称已完成。"
                "这句话不是界面操作（提问、分析、出卡）或要先问清目标时，不发界面命令，直接用文字结束本轮，"
                "完整回合会接着回答。")
            # The compact command index (2026-10-14 token 瘦身), not the whole UiCommand schema: one line
            # per field with its values; a field's full definition is app_capabilities operation="ui:<field>".
            request["instructions"] += "\n本轮界面命令索引（不猜造命令结构）：" + ui_command_index()
        if extra_input:
            # Request-only (never stored in history): the forced round's plain retry line, appended to
            # this turn's own question so the model reads one request, not a second user message.
            items = list(request["input"])
            for index in range(len(items) - 1, -1, -1):
                if items[index].get("role") == "user":
                    items[index] = {**items[index], "content": f"{items[index]['content']}\n{extra_input}"}
                    break
            request["input"] = items
        if no_tool_calls:
            # The exhaustion close still carries the turn's function_call / function_call_output items,
            # and a Responses request with those but no tools is a 400 on some compatible providers
            # (DeepSeek included) — which used to bring the turn right back to "未完成。". The tools stay
            # declared and tool_choice="none" forbids the calls: same honest one-sentence close.
            request["tool_choice"] = "none"
        # 按技能延迟挂载: the general loop and the voice fast path carry the tools not deferred plus the
        # mounted skills' ones; routed subsets and routine steps keep their own fixed lists.
        visible = self._visible_tool_names(turn.session_id)
        allowed_names: set[str] | frozenset[str] | None = (
            route.tool_names if route is not None
            else ((turn.tools or self.trader_tools).read_only_names() & visible if turn.fast else None))
        if turn.routine is not None:
            # 我的流程: this step's tools (plus the ones earlier steps already called), never a direct
            # order / execution / settings tool — autonomy on or off.
            allowed_names = set(turn.routine.declared) - ROUTINE_WITHHELD
        # Progressive disclosure rides the general loop only; the deterministic
        # routed subsets keep their fixed tool lists. Judged before the hidden
        # set materialises `allowed_names` — a user with one withheld skill
        # still gets the disclosure tools on general turns.
        general_loop = allowed_names is None
        if allowed_names is None:
            allowed_names = visible
        hidden = self._hidden_tools(user_id)
        if hidden:
            # M2: a tier's switched-off features never see their tools, on the
            # general loop, the fast read-only subset and the routed subsets alike.
            allowed_names = {name for name in allowed_names if name not in hidden}
        if not turn.ai_initiated:
            # The user's own turn: no direct fill, whatever skill is mounted (every round recomputes this).
            allowed_names = set(allowed_names) - DIRECT_FILL_TOOLS
        if route is not None and route.no_card:
            # 问句、否定、转述、整句收回、建议、条件单: the words are not an order now — no card and no order write this
            # turn, whatever skill the model activates (every round recomputes this from the mounted tools).
            allowed_names = set(allowed_names) - ORDER_WRITE_TOOLS
        request["tools"] = self._tool_definitions(allowed_names, user_id=user_id, disclosure=general_loop)
        traits = self._traits[endpoint.slug]
        if turn.forcing and not no_tool_calls and traits.get("tool_choice"):
            request["tool_choice"] = "required"
        if turn.answer_wanted and turn.routine is None and not turn.ui_fallback:
            # 回答卡 (2026-10-14): offered only on analysis-type turns, so other turns carry no extra definition.
            request["tools"].append(dict(ANSWER_CARD_DEFINITION))
        if turn.ui_fallback and not turn.ui_tool_called and not no_tool_calls and traits.get("tool_choice"):
            request["tool_choice"] = "required"
        # A routed subset is a boundary, not a hint: providers do not hold the model to the declared tools
        # (#929: DeepSeek called a card tool the route withheld).
        turn.offered_tools = (frozenset(str(tool.get("name")) for tool in request["tools"])
                              if route is not None and route.tool_names is not None and turn.routine is None else None)
        if traits.get("max_output_tokens"):
            request["max_output_tokens"] = traits["max_output_tokens"]
        result = await self._stream(endpoint, request, turn=turn)
        if user_id:
            self._record_turn_usage(user_id, result, history)
        return result

    async def _stream(
        self, endpoint: ModelEndpoint, request: dict[str, Any], *, turn: _Turn | None,
        on_summary: Callable[[str], None] | None = None, reasoning_effort: str | None = None,
        meter: _Turn | None = None, on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> _Round:
        client = self.model_clients[endpoint.slug]
        traits = self._traits[endpoint.slug]
        attempt = dict(request)
        if endpoint.upstream:
            attempt["model"] = endpoint.upstream
        quick = turn is not None and (turn.fast or turn.origin == "voice")
        route = turn.route if turn is not None and turn.route is not None else None
        routed = route is not None
        if traits.get("reasoning", True):
            # Voice turns take the low reasoning tier: the caller is waiting for the first sentence.
            # A forced order turn on DeepSeek runs at "none" throughout (its forced tool choice is
            # refused in thinking mode).
            override = turn.reasoning_override if turn is not None else None
            attempt["reasoning"] = {"effort": override or reasoning_effort
                                    or ("minimal" if quick or routed else self._reasoning_effort())}
            if traits.get("reasoning_summary"):
                attempt["reasoning"]["summary"] = "auto"
        # Tool selection on quick turns can be made more repeatable with a low temperature (2026-09-19 fuzzy
        # evaluation: the same sentence chose different tools on consecutive runs). Opt-in per deployment.
        # A routed turn (not the voice fast path, whose read-only semantics stay as they were) is pinned
        # at its route's temperature (0.0): the same sentence must take the same tools every run.
        if turn is not None and not turn.fast and route is not None and route.temperature is not None:
            attempt["temperature"] = route.temperature
        elif quick and VOICE_TEMPERATURE is not None and "temperature" not in attempt:
            attempt["temperature"] = VOICE_TEMPERATURE
        started = monotonic()
        status = "failed"
        usage: dict[str, int] = {}
        # The turn whose usage this request counts toward (a side request of the turn — history
        # compaction — passes it as `meter`, it must not stream into the turn's answer).
        meter = meter or turn
        try:
            try:
                stream = await client.responses.create(**attempt)
            except openai.BadRequestError:
                if "reasoning" not in attempt:
                    raise
                if meter is not None:
                    meter.usage["requests"] += 1  # the rejected request was sent too
                log.info("agent_request run_id=%s model=%s status=http_400_reasoning elapsed_ms=%.1f",
                         meter.run_id if meter else "-", endpoint.slug, (monotonic() - started) * 1000)
                # Table-driven downgrade: the provider rejected the reasoning
                # block, so this model never receives it again in this process.
                traits["reasoning"] = False
                traits["reasoning_summary"] = False
                attempt.pop("reasoning")
                stream = await client.responses.create(**attempt)
            result = await self._consume(stream, turn, on_summary=on_summary, on_text=on_text)
            usage = result.usage
            status = "completed" if result.completed else "interrupted"
            if not result.completed:
                raise NativeAgentError("model_stream_interrupted")
            return result
        except openai.APIStatusError as error:
            status = f"http_{error.status_code}"
            # Classify without echoing the response body, which may quote headers.
            if error.status_code in {401, 403}:
                raise NativeAgentError("model_auth_failed") from error
            if error.status_code == 429:
                raise NativeAgentError("model_rate_limited") from error
            raise NativeAgentError("model_request_failed") from error
        except (openai.APIConnectionError, httpx.HTTPError) as error:
            status = "connection_error"
            raise NativeAgentError("model_request_failed") from error
        finally:
            if meter is not None:
                meter.usage["requests"] += 1
                for key, value in usage.items():
                    meter.usage[key] = meter.usage.get(key, 0) + value
            log.info("agent_request run_id=%s model=%s status=%s elapsed_ms=%.1f input=%d output=%d cached=%d"
                     " reasoning=%d tools=%d",
                     meter.run_id if meter else "-", endpoint.slug, status, (monotonic() - started) * 1000,
                     usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("cached_tokens", 0),
                     usage.get("reasoning_tokens", 0), len(request.get("tools") or ()))

    async def _consume(
        self, stream: Any, turn: _Turn | None, *, on_summary: Callable[[str], None] | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> _Round:
        result = _Round()
        calls: dict[str, _ToolCall] = {}
        try:
            async for event in stream:
                kind = str(getattr(event, "type", "") or "")
                if kind == "response.output_text.delta":
                    result.text += str(getattr(event, "delta", "") or "")
                    if on_text is not None:
                        await on_text(visible_answer(result.text)[0])
                    # A gated turn streams once it is verified (a business tool or a search ran); only an
                    # answer from memory is held back until its freshness footnote can precede it.
                    # The same holds once the turn's latest account read failed (#872): the account footnote
                    # goes before the answer.
                    if turn is not None and (not turn.gated or turn.verified) and not turn.account_unconfirmed \
                            and not turn.forcing and not turn.ui_fallback and turn.routine is None:
                        # Thinking the model wrote into its answer never streams out (the screen, the
                        # voice listener and the persisted delta all read this payload). Once a marker
                        # shows, the draft before it is suspect too (the 22:27 one stated a stop that
                        # was gone): the screen falls back to the placeholder until the rewrite lands.
                        # The voice listener is not handed that retraction — it reads the paragraph by
                        # offset; it only stops getting the draft.
                        shown, leaked = visible_answer(result.text)
                        if leaked:
                            if not turn.thinking_retracted:
                                turn.thinking_retracted = True
                                await self._publish(turn.session_id, "message", "message.delta", {
                                    "turnId": turn.turn_id, "role": "assistant", "text": THINKING_PLACEHOLDER,
                                    "agentId": "native"}, notify=False)
                        else:
                            if shown and turn.first_text_ms is None:
                                turn.first_text_ms = round((monotonic() - turn.started_at) * 1000)
                            await self._publish(turn.session_id, "message", "message.delta", {
                                "turnId": turn.turn_id, "role": "assistant", "text": shown or "正在处理…",
                                "agentId": "native"})
                elif kind == "response.reasoning_summary_text.delta":
                    result.summary = (result.summary + str(getattr(event, "delta", "") or ""))[-12000:]
                    if on_summary is not None:
                        on_summary(result.summary)
                    if turn is not None:
                        turn.task["summary"] = (turn.task.get("summary", "") + str(getattr(event, "delta", "") or ""))[-12000:]
                        turn.task["steps"][0].update(label="模型正在分析", detail="等待模型输出")
                        await self._publish_task(turn)
                elif kind == "response.reasoning_summary_part.added":
                    if turn is not None and turn.task.get("summary"):
                        turn.task["summary"] += "\n"
                elif kind == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if getattr(item, "type", None) == "function_call":
                        call = _ToolCall(item_id=str(getattr(item, "id", "") or uuid4()),
                                         call_id=str(getattr(item, "call_id", "") or ""),
                                         name=str(getattr(item, "name", "") or ""))
                        calls[call.item_id] = call
                        result.calls.append(call)
                        if turn is not None:
                            self._advance_task(turn, call, detail="处理中")
                            await self._publish_task(turn)
                    elif turn is not None and getattr(item, "type", None) == "message":
                        turn.task["steps"][0].update(label="模型正在分析", detail="等待模型输出")
                elif kind == "response.function_call_arguments.done":
                    pending_call = calls.get(str(getattr(event, "item_id", "") or ""))
                    if pending_call is not None:
                        pending_call.arguments = str(getattr(event, "arguments", "") or "")
                elif kind == "response.output_item.done":
                    item = getattr(event, "item", None)
                    item_type = getattr(item, "type", None)
                    if item_type == "reasoning":
                        result.reasoning_items.append(self._item_payload(item))
                    if item_type == "function_call":
                        done_call = calls.get(str(getattr(item, "id", "") or ""))
                        if done_call is not None:
                            done_call.arguments = str(getattr(item, "arguments", "") or done_call.arguments)
                            done_call.call_id = str(getattr(item, "call_id", "") or done_call.call_id)
                    elif item_type == "message":
                        text = "".join(str(getattr(part, "text", "") or "")
                                       for part in (getattr(item, "content", None) or [])
                                       if getattr(part, "type", "") == "output_text")
                        if text:
                            result.text = text
                elif kind in {"response.completed", "response.incomplete"}:
                    response = getattr(event, "response", None)
                    usage = getattr(response, "usage", None)
                    total = getattr(usage, "total_tokens", None)
                    if isinstance(total, int) and total > 0:
                        result.usage_tokens = total
                    result.usage = self._usage_split(usage)
                    result.incomplete = kind == "response.incomplete"
                    result.completed = True
                elif kind in {"response.failed", "error"}:
                    raise NativeAgentError(self._stream_failure(event))
        except (openai.APIError, httpx.HTTPError) as error:
            # An overload the SDK raised from the stream is a rate limit, not a broken connection.
            if "rate_limit" in str(getattr(error, "code", "") or "").lower() or "overloaded" in str(error).lower():
                raise NativeAgentError("model_rate_limited") from error
            raise NativeAgentError("model_stream_interrupted") from error
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                await close()
        return result

    @staticmethod
    def _usage_split(usage: Any) -> dict[str, int]:
        """The Responses usage block as input / output / cached / reasoning tokens. Cache hits come as
        input_tokens_details.cached_tokens; a provider that reports them the chat-completions way
        (DeepSeek's prompt_cache_hit_tokens) is read too."""
        if usage is None:
            return {}
        def number(value: Any) -> int:
            return value if isinstance(value, int) and value > 0 else 0
        def part(owner: Any, name: str) -> Any:
            return owner.get(name) if isinstance(owner, dict) else getattr(owner, name, None)
        cached = number(part(part(usage, "input_tokens_details"), "cached_tokens"))
        if not cached:
            cached = number(part(usage, "prompt_cache_hit_tokens"))
            extra = getattr(usage, "model_extra", None)
            if not cached and isinstance(extra, dict):
                cached = number(extra.get("prompt_cache_hit_tokens"))
        return {"input_tokens": number(part(usage, "input_tokens")),
                "output_tokens": number(part(usage, "output_tokens")),
                "cached_tokens": cached,
                "reasoning_tokens": number(part(part(usage, "output_tokens_details"), "reasoning_tokens"))}

    @staticmethod
    def _stream_failure(event: Any) -> str:
        """A failure reported inside a 200 stream: GLM sends its 429 this way (`response.failed`, error code
        `rate_limit_exceeded`, 2026-10-12), which is a rate limit like the HTTP one, not a broken request."""
        response = getattr(event, "response", None)
        error = getattr(response, "error", None) if response is not None else None
        # Kimi sends its overload as an `error` event carrying the error itself (2026-10-24: "The engine is
        # currently overloaded", code rate_limit_exceeded).
        error = error if error is not None else getattr(event, "error", None)
        code = getattr(error, "code", None) if error is not None else getattr(event, "code", None)
        if isinstance(error, dict):
            code = error.get("code")
        text = str(code or "").lower()
        return "model_rate_limited" if "rate_limit" in text or "quota" in text else "model_request_failed"

    @staticmethod
    def _item_payload(item: Any) -> dict[str, Any]:
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            payload = dump(exclude_none=True)
            if isinstance(payload, dict):
                return payload
        return dict(item) if isinstance(item, dict) else {"type": str(getattr(item, "type", "") or "")}

    @staticmethod
    def _turn_route_label(turn: _Turn) -> str:
        if turn.routine is not None:
            return "routine"
        if turn.escalated:
            return "ui_fallback>general"
        if turn.route is None:
            return "general"
        if turn.ui_fallback:
            return "ui_fallback"
        return turn.route.kind

    def _record_turn_metrics(self, turn: _Turn, *, user_id: str, status: str, elapsed_ms: float) -> None:
        """Token 计量 (2026-10-14): one log line and one agent_turn_usage row per turn — requests and
        the input / output / cache-hit split summed over them. Never breaks the turn."""
        usage = turn.usage
        route = self._turn_route_label(turn)
        log.info("agent_turn_usage run_id=%s route=%s origin=%s status=%s requests=%d input=%d output=%d cached=%d"
                 " reasoning=%d first_text_ms=%s elapsed_ms=%.0f",
                 turn.run_id, route, turn.origin or "-", status, usage.get("requests", 0),
                 usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("cached_tokens", 0),
                 usage.get("reasoning_tokens", 0),
                 turn.first_text_ms if turn.first_text_ms is not None else "-", elapsed_ms)
        try:
            self.store.add_turn_usage(run_id=turn.run_id, user_id=user_id, session_id=turn.session_id, route=route,
                                      origin=turn.origin, model=turn.model, status=status, usage=usage,
                                      first_text_ms=turn.first_text_ms, elapsed_ms=round(elapsed_ms))
        except Exception:  # measurement must never break a turn
            log.exception("turn_usage_record_failed")
        if status != "completed":
            self._turn_card_ids.pop(turn.turn_id, None)  # no answer follows to read them

    def _record_turn_usage(self, user_id: str, result: _Round, history: list[dict[str, Any]]) -> None:
        tokens = result.usage_tokens
        if tokens is None:
            tokens = max(self._estimate_tokens(history[-1:]) + len(result.text) // 2, 1)
        try:
            self.store.add_token_usage(user_id, datetime.now(UTC).date().isoformat(), tokens)
        except Exception:  # quota accounting must never break a completed turn
            log.exception("token_usage_record_failed")

    # -- tools ---------------------------------------------------------------

    def _execute_skill_tool(
        self, name: str, arguments: dict[str, Any], *, user_id: str, session_id: str,
    ) -> str:
        """`activate_skill` / `read_skill_resource`: the registry's progressive
        disclosure, availability-checked per user and deduped per session."""
        skill = skill_by_id(str(arguments.get("skillId") or ""))
        if skill is None:
            return f"没有叫 {arguments.get('skillId')} 的技能，请按目录里的名称调用。"
        reason = skill_block_reason(self.store, user_id, skill)
        if reason is not None:
            return unavailable_line(skill, reason, parent_block_reason(self.store, user_id, skill))
        if name == "activate_skill":
            # 按技能延迟挂载: the manual rides the instructions from the next request on (not repeated in
            # this output); the skill's deferred tools are declared from the next request on.
            fresh = self._mount_skill(session_id, skill.skill_id, by="activate")
            deferred = [tool for tool in skill.tools if tool in TraderReadOnlyTools.DEFERRED_TOOL_NAMES]
            tools_line = f"专用工具 {'、'.join(deferred)} 已挂上，下一轮起直接调用。" if deferred else ""
            if not fresh:
                return f"技能 {skill.skill_id} 已加载过（手册在系统说明里），直接按其手册执行。{tools_line}"
            return f"技能 {skill.skill_id} 已挂上：操作手册已加入系统说明，按它执行。{tools_line}"
        try:
            return read_skill_resource(skill, str(arguments.get("path") or ""))
        except HTTPException as error:
            return f"读取技能资源失败（{error.detail}）；path 必须是该技能目录里的相对路径。"

    # -- structured cards (2026-10-04) --------------------------------------------------------------

    async def _publish_question(self, session_id: str, turn_id: str, ask: dict[str, Any]) -> str:
        """The order planner asked (缺方向/手数/合约, two months held, long and short both held): the "需要确认的信息"
        card — its quick answers send a whole order as the user's next message — and its question as the answer.
        Nothing is ordered, no proposal is made."""
        question = str(ask.get("question") or "").strip()[:300] or "下单还缺一点信息，请补充。"
        missing = str(ask.get("missing") or "")
        options = [{"label": str(option["label"])[:40], "prompt": str(option["prompt"])[:200]}
                   for option in ask.get("options") or [] if option.get("label") and option.get("prompt")]
        title = {"dir": "需要确认：方向", "vol": "需要确认：手数", "product": "需要确认：合约",
                 "symbol": "需要确认：合约", "price": "需要确认：价格",
                 "act": "需要确认：开仓还是平仓"}.get(missing, "需要确认的信息")
        card = {"turnId": turn_id, "cardId": f"question-{turn_id}", "kind": "ask", "title": title,
                "question": question, "missing": [missing] if missing else [], "options": options[:4], "actions": [],
                "createdAt": now_iso()}
        await self._publish(session_id, "artifact", "card.order_question", card)
        self._open_questions.setdefault(session_id, []).append(card)
        return question

    async def _publish_no_card(self, turn: _Turn, *, reason: str) -> str:
        """The order round ended without propose_order's card, refusal or question (prose twice, or rounds spent):
        the Runtime's own "没能生成方案卡" card (the reason, the user's words, a 打开下单台 shortcut) and a fixed honest
        line as the answer."""
        card = {"turnId": turn.turn_id, "cardId": f"question-{turn.turn_id}", "kind": "no_card",
                "title": "没能生成下单卡", "question": NO_CARD_LINE, "reason": reason,
                "userText": turn.user_text[:200] or None,
                "actions": [ui_action("打开下单台", {"panel": "order"})], "createdAt": now_iso()}
        log.info("agent_forced_round_no_card run_id=%s", turn.run_id)
        await self._publish(turn.session_id, "artifact", "card.order_question", card)
        return NO_CARD_LINE

    async def _answer_card(self, turn: _Turn, call: _ToolCall, arguments: dict[str, Any]) -> str:
        """answer_card (回答卡, 2026-10-14): the model's synthesized answer as one card. Figures and compare
        lines are field keys the Runtime fills from this turn's reads; a key with no reading or a contradicted
        value is dropped (the model is told which); the basis and the buttons are the Runtime's own. Calling it
        again in the turn replaces the card in place."""
        cards = turn.cards or TurnCards(turn.turn_id)
        turn.cards = cards
        cards.card_shown = False  # set again only once this call's card is published
        card, dropped, error = cards.answer_card(arguments)
        cards.last_dropped = bool(dropped)
        if card is None:
            self._advance_task(turn, call, detail="结论要改" if cards.conclusion_rejected else "参数不对", completed=True,
                               failed=True, error_code="answer_card_invalid")
            await self._publish_task(turn)
            return json.dumps({"success": False, "errorCode": "answer_card_invalid", "userFacing": error}, ensure_ascii=False)
        if dropped:
            log.info("agent_answer_card_dropped run_id=%s cells=%d", turn.run_id, len(dropped))
        # Field keys the 读数表 refused and template lines with a number or a reading word of their own (结构化第 1 步):
        # how often each model gets the grammar wrong, and how often it wants a number the table does not have.
        for kind in ("ref", "text"):
            codes = [code for code in cards.rejections if code.startswith(kind + "_")]
            if codes:
                log.info("agent_answer_%s_rejected run_id=%s codes=%s", kind, turn.run_id, ",".join(codes))
        try:
            await self._publish(turn.session_id, "artifact", "card.answer", card)
        except Exception:
            log.exception("agent_answer_card_publish_failed run_id=%s", turn.run_id)
            self._advance_task(turn, call, detail="失败", completed=True, failed=True, error_code="answer_card_not_shown")
            await self._publish_task(turn)
            return json.dumps({"success": False, "errorCode": "answer_card_not_shown",
                               "userFacing": "回答卡没能显示，用正文回答。"}, ensure_ascii=False)
        # 接受链: only a card the broker took is the turn's answer (its conclusion for the text, the voice, history).
        cards.accept(card)
        cards.card_shown = True
        self._advance_task(turn, call, detail="已出卡", completed=True)
        await self._publish_task(turn)
        result: dict[str, Any] = {"success": True, "shown": True, "rule": ANSWER_CARD_RULE}
        if dropped:
            result["dropped"] = dropped
            result["note"] = "这些格或说法在本轮工具读回里找不到出处、或与读回的数据相反，已从卡上去掉；正文也不要这样说。"
        if cards.partial:
            result["rest"] = "本轮有读数卡片只显示了一部分（行数超出、较早的排名、陈旧的对比合约等），正文把卡上没有的部分说完。"
        return json.dumps(result, ensure_ascii=False)

    def _read_card(self, turn: _Turn, tool: str, arguments: dict[str, Any], output: str,
                   card: tuple[str, dict[str, Any]] | None, tools: TraderReadOnlyTools,
                   ) -> tuple[str, dict[str, Any]] | None:
        """回答卡片化第一层: a successful read becomes (or merges into) the turn's card of its kind —
        deterministic, from the tool's own result, never the model's choice."""
        cards = turn.cards
        assert cards is not None
        cards.record_read(tool, arguments, output)
        try:
            # (the caller notes the card for the answer card's 依据 once the broker took it)
            if card is not None and card[0] == "card.market":
                return "card.market", cards.market_card(card[1])
            if card is not None and card[0] == "card.account":
                return "card.account", cards.account_card(card[1])
            if card is None and tool in AUTO_DATA_TOOLS:
                data = tool_data(output)
                if data is not None:
                    built = cards.data_card(tool, arguments, data, account_word=tools._account_word())
                    if built is not None:
                        return "card.data", built
            if card is None and tool not in CARD_NEUTRAL_TOOLS:
                cards.uncarded += 1
        except Exception:  # a card is an extra: the read's answer goes on without it
            log.exception("agent_read_card_failed run_id=%s tool=%s", turn.run_id, tool)
        return card

    @staticmethod
    def _read_settled(tool: str, arguments: dict[str, Any], output: str) -> bool:
        """Whether a successful ONCE_PER_TURN_READS result answers the same call for the rest of the turn."""
        if tool == "get_chart_bars" and not (arguments.get("symbol") and arguments.get("durationSeconds")):
            return False  # the window's own contract / timeframe: the same arguments read another chart after a switch
        data = tool_data(output) or {}
        screen: dict[str, Any] = data["screen"] if isinstance(data.get("screen"), dict) else {}
        return str(data.get("status") or "") not in {"pending", "unavailable"} and not screen.get("pending")

    @staticmethod
    def _read_cards_only(turn: _Turn, shown: set[str]) -> bool:
        """Whether the turn's cards carry its facts, so the text may keep only its conclusion: the answer
        card was made, or every fact the turn read is on a read card. Order, routine and object-level turns
        keep their text."""
        cards = turn.cards
        if cards is None or turn.routine is not None or turn.focus or (turn.route is not None and turn.route.kind == "order"):
            return False
        names = {entry.split("|", 1)[0] for entry in shown}
        if names - READ_CARD_EVENTS - {"card.research_judgment"}:
            return False  # a proposal, a question or another card: the text stays as written
        if cards.partial:
            return False  # a card shows a read only in part (no answer card shows the rest): the text keeps it
        if cards.answered:
            return cards.uncarded == cards.uncarded_at_answer  # a read after the card is not on it
        return cards.uncarded == 0 and bool(names & READ_CARD_EVENTS)

    async def _settle_open_questions(self, session_id: str, user_text: str) -> None:
        """The message that arrives after a 需要确认 card is its answer, whatever it says: the card
        is republished under the same cardId with `answered` (and the option's label when the reply
        is exactly that option's prompt), so its buttons stop offering an answer the conversation
        already consumed — the reply itself stays in the conversation right below."""
        cards = self._open_questions.pop(session_id, None)
        if not cards:
            return
        text = re.sub(r"\s+", "", str(user_text or ""))
        for card in cards:
            answer = next((option["label"] for option in card.get("options") or []
                           if re.sub(r"\s+", "", str(option.get("prompt") or "")) == text), None)
            settled = {**card, "answered": True, "answeredAt": now_iso(), "answer": answer}
            await self._publish(session_id, "artifact", "card.order_question", settled)

    JUDGMENT_POLL_SECONDS = 3.0
    JUDGMENT_WAIT_SECONDS = 180.0
    _JUDGMENT_NOTES = {
        "disabled": "当前档位不能生成调研判断，可以打开调研看事实。",
        "insufficient": "这个品种的调研数据还不够，暂不出判断。",
        "failed": "今日判断生成失败，稍后会重试。",
        "budget_exhausted": "今天的判断额度已用完，明天 08:30 翻篇后再生成。",
    }

    async def _attach_judgment_card(self, turn: _Turn, decoded: DecodedInstruction, *, user_id: str,
                                    product_names: list[str] | None, tools: TraderReadOnlyTools) -> None:
        """判断卡: the research desk's judgment of the product asked about (or the window's contract),
        published before the model streams. A judgment not ready yet goes out as 生成中 and is
        republished under the same cardId when it is (a background task; the turn never waits)."""
        desk = getattr(getattr(self.trader_tools, "research_brief", None), "desk", None)
        if desk is None or getattr(self.data_provider, "identifier", None):
            return  # no desk, or an isolated replay (the desk reads today's facts)
        symbol = await self._opinion_symbol(decoded, user_id=user_id, session_id=turn.session_id,
                                            product_names=product_names, tools=tools)
        if not symbol:
            return
        card_id = f"judgment-{turn.turn_id}"
        generator_for = getattr(desk, "generator_for", None)
        generator = generator_for(user_id) if callable(generator_for) else None
        try:
            report = await asyncio.wait_for(desk.report(symbol, generator=generator, user_id=user_id), timeout=4.0)
        except TimeoutError:
            report = None
        except Exception as error:  # an unknown product, a desk failure: no card, the turn goes on
            log.info("agent_judgment_card_skipped symbol=%s reason=%s", symbol, error)
            return
        card = self._judgment_card(turn.turn_id, card_id, symbol, report)
        await self._publish(turn.session_id, "artifact", "card.research_judgment", card)
        if card["status"] == "pending":
            task = asyncio.create_task(self._follow_judgment(turn.session_id, turn.turn_id, card_id, symbol, desk))
            self._judgment_tasks.add(task)
            task.add_done_callback(self._judgment_tasks.discard)

    async def _follow_judgment(self, session_id: str, turn_id: str, card_id: str, symbol: str, desk: Any) -> None:
        waited = 0.0
        while waited < self.JUDGMENT_WAIT_SECONDS:
            await asyncio.sleep(self.JUDGMENT_POLL_SECONDS)
            waited += self.JUDGMENT_POLL_SECONDS
            try:
                report = await desk.report(symbol)
            except Exception as error:
                log.info("agent_judgment_follow_failed symbol=%s reason=%s", symbol, error)
                continue
            card = self._judgment_card(turn_id, card_id, symbol, report)
            if card["status"] != "pending":
                await self.broker.publish(session_id, kind="artifact", name="card.research_judgment", payload=card)
                return
        await self.broker.publish(session_id, kind="artifact", name="card.research_judgment", payload={
            **self._judgment_card(turn_id, card_id, symbol, None), "status": "unavailable",
            "note": "今日判断还在生成，稍后打开调研查看。"})

    @classmethod
    def _judgment_card(cls, turn_id: str, card_id: str, symbol: str, report: dict[str, Any] | None) -> dict[str, Any]:
        """A card.research_judgment payload from a desk report (None = facts still loading)."""
        product = str((report or {}).get("productName") or (report or {}).get("product") or symbol)
        card: dict[str, Any] = {"turnId": turn_id, "cardId": card_id, "symbol": str((report or {}).get("mainSymbol") or symbol),
                                "productName": product, "status": "pending", "facts": [], "invalidation": [],
                                "actions": [ui_action("打开调研", {"news": {"tab": "desk"}})]}
        judgment = (report or {}).get("judgment") or {}
        state = str(judgment.get("status") or ("pending" if report is None else "disabled"))
        if state == "pending" or (state == "ready" and judgment.get("refreshing") and not judgment.get("today")):
            return {**card, "note": "今日判断生成中，完成后会在这里更新。"}
        if state != "ready":
            return {**card, "status": "unavailable", "note": cls._JUDGMENT_NOTES.get(state, "暂时拿不到调研判断。")}
        basis = judgment.get("basis") or {}

        def lines(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
            result = []
            for point in points[:limit]:
                cited = basis.get((point.get("basis") or [""])[0]) or {}
                result.append({"text": str(point.get("text") or ""), "quote": str(point.get("quote") or ""),
                               "source": str(cited.get("source") or ""), "asOf": cited.get("asOf")})
            return [line for line in result if line["text"]]
        return {**card, "status": "ready", "stance": judgment.get("stance"), "confidence": judgment.get("confidence"),
                "headline": judgment.get("headline"), "facts": lines(judgment.get("points") or [], 4),
                "invalidation": lines(judgment.get("risks") or [], 3), "generatedAt": judgment.get("generatedAt"),
                "researchDay": judgment.get("researchDay"),
                "note": None if judgment.get("today", True) else f"这是 {judgment.get('researchDay')} 的判断，今日的还没生成。"}

    async def _window_symbol(self, window_id: str | None, *, user_id: str, session_id: str) -> str | None:
        """The contract the asking window shows (None without a window, or when it cannot be read)."""
        if not window_id:
            return None
        try:
            window = await self.trader_tools._ui_window(window_id, user_id=user_id, session_id=session_id, task_id=None)
        except Exception:
            return None
        symbol = str(((window.get("state") or {}).get("symbol")) or "").strip()
        return symbol or None

    async def _opinion_symbol(self, decoded: DecodedInstruction, *, user_id: str, session_id: str,
                              product_names: list[str] | None, tools: TraderReadOnlyTools) -> str | None:
        """The product an opinion question is about: the one it names (catalog main contract), else
        the contract on the asking window."""
        match = match_product_word(decoded.user_text, product_names or []) if product_names else None
        if match is not None:
            try:
                catalog = await asyncio.to_thread(tools.provider.instrument_catalog)
            except Exception:
                return None
            item = resolve_symbol(match[1], [entry for entry in catalog.get("items") or [] if isinstance(entry, dict)])
            return str(item["symbol"]) if item else None
        return await self._window_symbol(decoded.ui_window_id, user_id=user_id, session_id=session_id)

    async def _execute_tool(self, turn: _Turn, call: _ToolCall, user_id: str | None) -> str:
        started = monotonic()
        try:
            arguments = json.loads(call.arguments) if call.arguments else {}
        except json.JSONDecodeError:
            arguments = {}
        if turn.ui_fallback:
            turn.ui_tool_called = True
            refusal = self._ui_tool_refusal(call.name, arguments)
            if refusal is not None:
                self._advance_task(turn, call, detail="不属于界面操作", completed=True, failed=True)
                await self._publish_task(turn)
                return refusal
        elif turn.escalated:
            refusal = self._escalated_tool_refusal(call.name, arguments, turn.user_text)
            if refusal is not None:
                self._advance_task(turn, call, detail="界面请求不能改动业务", completed=True, failed=True,
                                   error_code="ui_scope_write")
                await self._publish_task(turn)
                return refusal
        elif (turn.offered_tools is not None and call.name not in turn.offered_tools) or (
                turn.route is not None and turn.route.no_card and order_write(call.name, arguments)) or (
                not turn.ai_initiated and fills_directly(call.name, arguments)):
            # #929: a routed turn runs only the tools its request declared; a turn whose words are no order now
            # (问句、否定、转述、收回、建议、条件单) never runs a card or order write (turn_router.order_write), the
            # catalog's app_call included; the user's own turn never fills an order directly
            # (turn_router.fills_directly) — propose_order cards it. No userFacing: the model goes on and answers.
            log.info("agent_tool_not_offered run_id=%s route=%s tool=%s", turn.run_id,
                     turn.route.kind if turn.route else "-", call.name)
            self._advance_task(turn, call, detail="本轮没有这个工具", completed=True, failed=True,
                               error_code="tool_not_offered")
            await self._publish_task(turn)
            return json.dumps({"success": False, "errorCode": "tool_not_offered",
                               "message": f"本轮没有提供 {call.name}，没有执行；只能调用："
                                          f"{'、'.join(sorted(turn.offered_tools or ()))}。"}, ensure_ascii=False)
        auto_start = None if turn.ai_initiated else auto_strategy_start(call.name, arguments)
        if auto_start is not None and not self._asks_for_strategy(turn, auto_start):
            # A strategy that may order by itself starts in the user's turn only when the user's words ask for it —
            # a start command naming it (or 策略 / 监控), or confirming the AI's restatement of it — never slipped into
            # another request (“820挂一手”, #953 Codex). No new authorization words: an explicit start starts at once.
            self._advance_task(turn, call, detail="用户没要启动策略", completed=True, failed=True,
                               error_code="strategy_start_not_asked")
            await self._publish_task(turn)
            return json.dumps({"success": False, "errorCode": "strategy_start_not_asked",
                               "message": "没有启动：用户这句话没有要启动这张策略。会自动下单的策略只在用户明确要启动它时"
                                          "启动，不要在别的请求里顺带启动。"}, ensure_ascii=False)
        if turn.fast and call.name == "app_call" and not self._read_only_app_call(arguments):
            # The voice fast path is read-only (AI audit 2026-10-13): its one app_call reads the window or
            # the catalog; a write through it (POST/PUT/DELETE) is refused before anything runs.
            self._advance_task(turn, call, detail="只读快查不能改动", completed=True, failed=True,
                               error_code="fast_path_read_only")
            await self._publish_task(turn)
            return json.dumps({"success": False, "errorCode": "fast_path_read_only",
                               "userFacing": "这是只读快查，不能改动任何东西；要改请直接说要做什么。"},
                              ensure_ascii=False)
        session = self.store.get_session(turn.session_id)
        tools = turn.tools or self.trader_tools
        if call.name in {"activate_skill", "read_skill_resource"}:
            # The registry's own tools: they read static SKILL.md files, run no
            # account or trading path, and need the session's dedup state.
            return self._execute_skill_tool(
                call.name, arguments, user_id=str(session["userId"]), session_id=turn.session_id)
        owner = tool_owner_map().get(call.name)
        if owner is not None:
            # 529-A1: re-check the owning skill at execution time — the tool list
            # this turn carries must not outlive the skill's availability (the
            # external app_call path hides them per call the same way).
            entitlement = self._skill_unavailable_line(str(session["userId"]), owner)
            if entitlement is not None:
                return entitlement
            # 按技能延迟挂载: using any of a skill's tools mounts it (its manual — where the detailed rules
            # moved out of the standing prompt live — rides the next rounds) and keeps it mounted; a skill
            # idle for SKILL_MOUNT_IDLE_TURNS turns unmounts.
            self._mount_skill(turn.session_id, owner, by="call")
        if call.name == ANSWER_CARD_TOOL:
            return await self._answer_card(turn, call, arguments)
        if turn.routine is not None:
            refusal = self._routine_tool_refusal(turn.routine, call.name, arguments)
            if refusal is not None:
                turn.routine.calls.append((ROUTINE_REFUSED_CALL, False, False))
                self._advance_task(turn, call, detail="不属于这一步", completed=True, failed=True)
                await self._publish_task(turn)
                return refusal
        read_key = (f"{call.name}|{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
                    if call.name in ONCE_PER_TURN_READS and turn.routine is None else None)
        if read_key is not None and read_key in turn.reads_done:
            # 同一回合不重读: the result is already in this turn's history (token 收敛, 2026-10-14).
            log.info("agent_tool_repeated run_id=%s tool=%s", turn.run_id, call.name)
            self._advance_task(turn, call, detail="本轮已读过", completed=True)
            await self._publish_task(turn)
            return REPEATED_READ_OUTPUT
        outcome = "cancelled"
        try:
            chart = (await self._window_symbol(turn.window_id, user_id=str(session["userId"]), session_id=turn.session_id)
                     if call.name == CARD_TOOL else None)
            tool_task = asyncio.create_task(tools.call_async(
                tool=call.name, arguments=arguments, turn_id=turn.turn_id, mode=turn.mode,
                session_id=turn.session_id, user_id=str(session["userId"]), task_id=turn.run_id, chart=chart,
                # 槽位出处：the user's words of this turn. A routine's card step states the user's saved steps (its card
                # is never confirmed for them either), so it is not held to the run's two words.
                said=turn.user_text if call.name == CARD_TOOL and turn.routine is None else None,
                card_reply=turn.route is not None and turn.route.pending_card_reply,
            ))
            self._track_tool_task(turn.run_id, tool_task)
            response, card = await asyncio.shield(tool_task)
            # Evidence, not intent: only a call that came back successful and reads a live fact makes
            # this turn "verified". Until 1.6.6 any business tool — including reading the manual, and
            # including a failed read — cleared the freshness footnote.
            if response.get("success") is True and (
                    call.name in LIVE_FACT_TOOLS
                    # A write whose result carries the account re-read after it (accountAfter) is a
                    # live fact about positions, orders and protections, like get_account_activity.
                    or (call.name in ACCOUNT_STATE_WRITE_TOOLS and self._account_after_read(response))):
                turn.evidence.append(self._tool_evidence(call.name, arguments, response))
            after_read = self._account_write_read(call.name, arguments, response)
            if after_read is not None:
                # The write ran (its success stands either way); its re-read is now the account's state —
                # an unreadable one retires every earlier read (#872).
                self._note_account_read(turn, call.name, available=after_read, write=True)
            if call.name == "web_search" and response.get("success") is True:
                turn.web_search_used = True
            if response.get("success") is not True and (
                call.name == "get_futures_decision_context"
                # The gate inside propose_order failing at the context level is the same
                # "market not ready" as the context tool failing (a gate that is merely not ready
                # returns proposal_gate_not_ready and the model explains the missing items).
                or (call.name == CARD_TOOL and self._error_code(response) in DECISION_CONTEXT_UNAVAILABLE)
            ):
                turn.market_unavailable = True
            try:
                cancelled = self.store.get_run(turn.run_id, user_id=str(session["userId"]))["status"] == "cancelled"
            except KeyError:
                cancelled = False
            if cancelled:
                if card is not None and card[0] == "card.backtest":
                    backtest_id = card[1].get("backtestId")
                    if isinstance(backtest_id, str):
                        self.store.discard_backtest_run(backtest_id, user_id=str(session["userId"]))
                response = tools._unavailable(call.name, "backtest_cancelled")
                card = None
            failed = response.get("success") is not True
            if turn.routine is not None:
                turn.routine.calls.append((routine_call_name(call.name, arguments), not failed, bool(card is not None and card[0].startswith("proposal."))))
                turn.routine.used.add(call.name)
            if call.name == CARD_TOOL and (not failed or self._route_refusal(
                    "".join(str(item.get("text", "")) for item in response.get("contentItems") or [])) is not None):
                # A card made, refused with the user's own reason or asked: the order round's structured end.
                turn.structured_done = True
            ask = (self._tool_payload(response).get("data") or {}).get("ask") if call.name == CARD_TOOL else None
            if isinstance(ask, dict):
                # The planner asked: the question card, published once; it is the turn's answer.
                turn.asked = await self._publish_question(turn.session_id, turn.turn_id, ask)
            outcome = (self._error_code(response) or "failed") if failed else "ok"
            # AI 指令栏 (改版第二步): what this call changed, for "AI 做了 N 件事" and its 撤销.
            from .agent_actions import step_action
            action = None if failed else step_action(call.name, arguments, self._tool_payload(response))
            self._advance_task(turn, call, detail="失败" if failed else "已完成", completed=True, failed=failed,
                               error_code=outcome if failed else None, action=action)
            await self._publish_task(turn)
            output = "".join(str(item.get("text", "")) for item in response.get("contentItems") or [])
            read_card = not failed and turn.cards is not None
            if read_card:
                card = self._read_card(turn, call.name, arguments, output, card, tools)
            for event, payload in card_events(card):  # 一句多单: each card on its own, in order, each confirmed alone
                card_kind = ("approval" if event.startswith("proposal.")
                             else "chart" if event in {"card.backtest", "card.strategy_backtest"} else "artifact")
                try:
                    await self._publish(turn.session_id, card_kind, event, payload)
                except Exception:
                    if read_card and turn.cards is not None and event in READ_CARD_EVENTS:
                        turn.cards.uncarded += 1  # the read's facts are on no card after all
                    raise
                if read_card and turn.cards is not None and event in READ_CARD_EVENTS:
                    turn.cards.note_card(payload.get("cardId"))  # shown: the answer card may fold it into its 依据
            # Registered once the result is on its way to the model (a publish failure above returns an error
            # instead), and only when it is the answer: a source still pending / unavailable may be there on the
            # next read, and a chart read without its contract or timeframe follows the window (#886 review).
            if read_key is not None and not failed and self._read_settled(call.name, arguments, output):
                turn.reads_done.add(read_key)
            self._record_ui_receipt(turn, call, arguments, output)
            return output
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unknown tool names and invalid arguments already come back as
            # `_unavailable` responses; anything else is an unexpected failure
            # that the model is told about so it can recover, not a dead turn.
            log.exception("agent_tool_failed run_id=%s tool=%s", turn.run_id, call.name)
            outcome = "tool_failed"
            if turn.routine is not None:
                turn.routine.calls.append((routine_call_name(call.name, arguments), False, False))
            self._advance_task(turn, call, detail="失败", completed=True, failed=True, error_code=outcome)
            await self._publish_task(turn)
            await self._publish(turn.session_id, "warning", "warning", {
                "turnId": turn.turn_id, "title": "工具执行失败",
                "message": "一个数据工具在执行时出错，本轮继续；相关数据未被采信。",
                "severity": "warning", "deterministic": True})
            fallback = tools._unavailable(call.name, "tool_failed")
            output = "".join(str(item.get("text", "")) for item in fallback.get("contentItems") or [])
            self._record_ui_receipt(turn, call, arguments, output)
            return output
        finally:
            # outcome = ok, or the failed call's errorCode: a failure is traceable from the log alone.
            log.info("agent_tool run_id=%s tool=%s outcome=%s elapsed_ms=%.1f",
                     turn.run_id, call.name, outcome, (monotonic() - started) * 1000)

    @staticmethod
    def _record_ui_receipt(turn: _Turn, call: _ToolCall, arguments: dict[str, Any], output: str) -> None:
        """One interface write's outcome: an applied receipt confirms that step; a refusal before
        queueing (`uiCommandAttempted: False`, #759) ran nothing, so the round may re-read and retry;
        anything else attempted is unknown and is never sent again."""
        if not turn.ui_fallback or call.name not in {"app_call", "highlight_ui"}:
            return
        if call.name == "app_call":
            try:
                operation = json.loads(arguments.get("requestJson") or "{}").get("operation")
            except (ValueError, TypeError, AttributeError):
                operation = None  # unparseable: refused before any request, not a known write
            if operation != "POST /api/app/ui/commands":
                return
        turn.ui_attempted = True
        try:
            payload = json.loads(output)
            data = payload.get("data") or {}
            if data.get("uiCommandAttempted") is False:
                detail = (data.get("data") or {}).get("detail") if call.name == "app_call" else None
                reason = detail if isinstance(detail, str) else str(payload.get("warning") or "")
                turn.ui_not_run = re.sub(r"^[a-z_]+:\s*", "", reason).split("，")[0]
                return
            receipt = (data.get("data") or {}) if call.name == "app_call" else data
            if receipt.get("status") == "applied":
                turn.ui_receipts[str(receipt["commandId"] if call.name == "app_call" else call.call_id)] = receipt
                turn.ui_not_run = None
            else:
                turn.ui_error = str(receipt.get("status") or "unknown")
                if turn.ui_error == "rejected" and call.name == "app_call":
                    turn.ui_refused = describe_ui_change(receipt.get("changes") or {})
        except (ValueError, TypeError, KeyError, AttributeError):
            # An attempted write with no usable receipt cannot be sent again.
            turn.ui_error = "unknown"

    @staticmethod
    def _ui_result_line(turn: _Turn) -> str:
        """The UI round's answer once a write was attempted: only what the receipts prove."""
        steps = [describe_ui_change(receipt.get("changes") or {}) for receipt in turn.ui_receipts.values()]
        done = f"已改好：{'；'.join(steps)}。" if steps else ""
        if turn.ui_error == "rejected" and turn.ui_refused:
            # The window refused this step (a day with no review, a surface that is not there): say which.
            return done + f"「{turn.ui_refused}」没有生效（窗口没有接受这一步，比如那一天没有记录或这里没有这个入口），没有重复提交。"
        if turn.ui_error:
            return done + "其余界面操作尚未确认完成，没有重复提交。请查看当前界面或明确要切到哪里。"
        if turn.ui_not_run is not None:
            reason = f"（{turn.ui_not_run}）" if turn.ui_not_run else ""
            # Only this command is proven unsent: never claim the window itself did not change.
            return done + f"{'其余' if done else ''}界面命令没有发出{reason}。"
        return done

    # -- task cards ----------------------------------------------------------

    def _new_task(self, turn: _Turn) -> dict[str, Any]:
        gated = turn.gated
        return {
            "turnId": turn.turn_id, "taskId": turn.run_id,
            "title": "正在核验最新信息" if gated else "AI 正在处理",
            "startedAt": utc_now(), "summary": "",
            "subtitle": "先读取实时行情或联网搜索，再给出结论" if gated else "Sente 工程区 · 完整工程能力",
            "progress": 8, "status": "running", "model": turn.model,
            "steps": [{"id": "understand",
                       "label": "先读取实时行情或联网搜索，再给出结论" if gated else "等待模型响应",
                       "status": "active", "detail": "处理中"}],
        }

    def _advance_task(self, turn: _Turn, call: _ToolCall, *, detail: str,
                      completed: bool = False, failed: bool = False, error_code: str | None = None,
                      action: dict[str, Any] | None = None) -> None:
        steps = turn.task["steps"]
        understand = next((step for step in steps if step["id"] == "understand"), None)
        if understand and understand["status"] == "active":
            understand.update({"status": "done", "detail": "完成"})
        step = next((entry for entry in steps if entry["id"] == call.item_id), None)
        if step is None:
            step = {"id": call.item_id, "label": TOOL_LABELS.get(call.name, "查询并处理数据"), "tool": call.name,
                    "status": "active", "detail": detail}
            steps.append(step)
        if completed:
            step.update({"status": "failed" if failed else "done", "detail": detail})
            if error_code:
                # Why the call failed, on the task event itself (audit 10-13: steps said only 失败).
                step["errorCode"] = error_code
            if action is not None:
                step["action"] = action
        else:
            step.update({"status": "active", "detail": detail})
        done = sum(step["status"] == "done" for step in steps)
        turn.task["progress"] = min(92, 12 + round(76 * done / max(1, len(steps))))

    async def _publish_task(self, turn: _Turn) -> None:
        self._task_cards[turn.run_id] = deepcopy(turn.task)
        await self._publish(turn.session_id, "progress", "task.progress", deepcopy(turn.task))

    async def _finish_task(self, turn: _Turn, status: str, *, detail: str | None = None) -> None:
        task = turn.task
        task["status"] = status
        task["finishedAt"] = utc_now()
        if status == "completed":
            task["progress"] = 100
        for step in task["steps"]:
            if step["status"] == "active":
                step.update({"status": "done" if status == "completed" else "failed",
                             "detail": "完成" if status == "completed" else detail or "已停止"})
        self._task_cards.pop(turn.run_id, None)
        await self._publish(turn.session_id, "progress", "task.completed", deepcopy(task))
        await self._publish(turn.session_id, "result", "agent.result", {
            "turnId": turn.turn_id, "taskId": turn.run_id, "threadId": turn.session_id, "status": status, "model": turn.model})

    # -- streaming coalescer -------------------------------------------------

    @staticmethod
    def _stream_key(session_id: str, name: str, payload: dict[str, Any]) -> tuple[str, str, str] | None:
        if name in {"message.delta", "message.completed"}:
            return session_id, "message", str(payload.get("turnId") or "")
        if name in {"task.progress", "task.completed"}:
            return session_id, "task", str(payload.get("taskId") or "")
        return None

    def subscribe_turn_text(self, session_id: str, listener: Callable[[dict[str, Any]], None],
                           *, on_run_id: Callable[[str], None] | None = None) -> Callable[[], None]:
        """Call `listener({"turnId", "text", "final"})` for every assistant text update of the session,
        uncoalesced; `final` marks message.completed. `on_run_id(run_id)` gets each turn's run id from
        its task.started (what a voice call cancels mid-flight). Returns the unsubscribe."""
        listeners = self._turn_text_listeners.setdefault(session_id, [])
        entry = (listener, on_run_id)
        listeners.append(entry)

        def unsubscribe() -> None:
            try:
                listeners.remove(entry)
            except ValueError:
                pass
            if not listeners:
                self._turn_text_listeners.pop(session_id, None)
        return unsubscribe

    def _notify_turn_text(self, session_id: str, name: str, payload: dict[str, Any]) -> None:
        """Assistant text as it streams, plus each user message (so a listener can bind to the answer
        of its own question when several turns queue on one session)."""
        if name == "task.started":
            # The run id a voice call needs to cancel an in-flight turn (POST /api/runs/{id}/cancel)
            # goes to the subscription's own callback, never into the text stream itself.
            run_id = str(payload.get("taskId") or "")
            for _listener, on_run_id in list(self._turn_text_listeners.get(session_id, [])):
                if on_run_id is not None and run_id:
                    try:
                        on_run_id(run_id)
                    except Exception:
                        log.exception("turn text listener failed")
            return
        if name not in {"message.delta", "message.completed"} or payload.get("role") not in {"assistant", "user"}:
            return
        for listener, _on_run_id in list(self._turn_text_listeners.get(session_id, [])):
            try:
                listener({"turnId": str(payload.get("turnId") or ""), "role": str(payload.get("role")),
                          "text": str(payload.get("text") or ""), "final": name == "message.completed"})
            except Exception:  # a listener must never break the turn
                log.exception("turn text listener failed")

    async def _publish(self, session_id: str, kind: str, name: str, payload: dict[str, Any], *,
                       notify: bool = True) -> None:
        if notify:
            self._notify_turn_text(session_id, name, payload)
        # (a routine run publishes its own closing line, never _publish_answer: not tracked)
        tracked = name.startswith(("card.", "proposal.")) and payload.get("turnId") and any(
            turn.turn_id == payload["turnId"] and turn.routine is None for turn in self._turns.values())
        key = self._stream_key(session_id, name, payload)
        if name in {"message.delta", "task.progress"} and key is not None:
            if key not in self._seen_stream_keys:
                self._seen_stream_keys.add(key)
                self._stream_snapshot_at[key] = monotonic()
                await self.broker.publish(session_id, kind=kind, name=name, payload=payload)
            else:
                self._stream_pending[key] = (kind, name, payload)
                if key not in self._stream_tasks:
                    self._stream_tasks[key] = asyncio.create_task(self._flush_stream_later(key))
            return
        if key is not None:
            # The closing event carries the whole state, so a snapshot still waiting in the coalescing
            # window is dropped, not sent: as a transient it would reach the client after this persisted
            # event (SSE sends a batch's stored events before its transients) and roll the task back to
            # running — the 等待模型响应 row stuck under a question card or a refusal (2026-10-04).
            self._stream_pending.pop(key, None)
            await self._flush_stream_now(key, persist=False)
            self._seen_stream_keys.discard(key)
            self._stream_snapshot_at.pop(key, None)
        await self.broker.publish(session_id, kind=kind, name=name, payload=payload)
        if tracked:
            # 回答卡片化统计: the distinct cards each running turn showed (republished in place counts once) — only
            # once the broker validated and stored the event (#878 review: a refused card is no card shown).
            self._turn_card_ids.setdefault(str(payload["turnId"]), set()).add(
                f"{name}|{payload.get('cardId') or payload.get('proposalId') or payload.get('taskId') or ''}")

    async def _flush_stream_later(self, key: tuple[str, str, str]) -> None:
        task = asyncio.current_task()
        try:
            await asyncio.sleep(STREAM_COALESCE_SECONDS)
            pending = self._stream_pending.pop(key, None)
            if pending is None:
                return
            persist = monotonic() - self._stream_snapshot_at.get(key, 0.0) >= STREAM_PERSIST_SECONDS
            publisher = self.broker.publish if persist else self.broker.publish_transient
            inflight = asyncio.ensure_future(publisher(key[0], kind=pending[0], name=pending[1], payload=pending[2]))
            self._stream_inflight[key] = inflight
            try:
                await asyncio.shield(inflight)
            finally:
                if inflight.done():
                    self._stream_inflight.pop(key, None)
            if persist:
                self._stream_snapshot_at[key] = monotonic()
        finally:
            if self._stream_tasks.get(key) is task:
                self._stream_tasks.pop(key, None)
            if key in self._stream_pending and key not in self._stream_tasks:
                self._stream_tasks[key] = asyncio.create_task(self._flush_stream_later(key))

    async def _flush_stream_now(self, key: tuple[str, str, str], *, persist: bool) -> None:
        pending = self._stream_pending.pop(key, None)
        task = self._stream_tasks.pop(key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        inflight = self._stream_inflight.pop(key, None)
        if inflight is not None and not inflight.done():
            # Never let a coalesced delta land after the message it belongs to.
            await asyncio.gather(inflight, return_exceptions=True)
        if pending is None:
            return
        publisher = self.broker.publish if persist else self.broker.publish_transient
        await publisher(key[0], kind=pending[0], name=pending[1], payload=pending[2])

    # -- side channels (explanations, briefs, strategy design) ----------------

    async def _complete_text(self, endpoint: ModelEndpoint, *, instructions: str, input_text: str,
                             reasoning_effort: str | None = None, max_output_tokens: int | None = None,
                             meter: _Turn | None = None) -> str:
        request = {"model": endpoint.slug, "instructions": instructions,
                   "input": [{"role": "user", "content": input_text}], "stream": True, "store": False,
                   **({"max_output_tokens": max_output_tokens} if max_output_tokens else {})}
        return (await self._stream(endpoint, request, turn=None, reasoning_effort=reasoning_effort,
                                   meter=meter)).text

    async def stream_text(
        self, *, instructions: str, input_text: str, timeout_seconds: float, model: str | None = None,
        usage_user: str | None = None, max_output_tokens: int | None = None, light_reasoning: bool = False,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> StructuredResult:
        """One bounded plain-text reply without conversation history, streamed as it is written (多 AI 研讨's
        spoken turns): `on_text` gets the visible text so far after every delta, thinking cut as in a chat
        answer. The same bounds as `complete_structured`; an empty reply is `model_output_empty`."""
        endpoint = self._endpoint_for(model)
        effort = (self._traits[endpoint.slug].get("light_effort") or "minimal") if light_reasoning else None
        request: dict[str, Any] = {"model": endpoint.slug, "instructions": instructions,
                                   "input": [{"role": "user", "content": input_text}], "stream": True, "store": False,
                                   **({"max_output_tokens": max_output_tokens} if max_output_tokens else {})}
        tokens = 0
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await self._stream(endpoint, request, turn=None, reasoning_effort=effort, on_text=on_text)
            tokens = result.usage_tokens or max(self._estimate_tokens(request["input"]) + len(result.text) // 2, 1)
            text = visible_answer(result.text)[0].strip()
            if not text:
                raise NativeAgentError("model_output_empty")
            return StructuredResult(text, [], tokens)
        finally:
            self._record_structured_usage(tokens, usage_user or self.usage_user)

    async def complete_structured(
        self, *, instructions: str, input_text: str, schema: type[BaseModel],
        timeout_seconds: float, model: str | None = None, tools: set[str] | None = None,
        on_progress: Callable[..., None] | None = None, json_schema: dict[str, Any] | None = None,
        usage_user: str | None = None,
        max_output_tokens: int | None = None, light_reasoning: bool = False,
    ) -> StructuredResult:
        """One bounded request/response without conversation history.

        Only `web_search` may be offered as a tool; it runs against the search
        client directly, never against account or trading operations. A caller
        that must cite deep source pages (AI 简报) names the search engine;
        otherwise the client's default MCP-first path answers. `max_output_tokens`
        caps each reply (多 AI 研讨 bounds every role's turn the same way); on a reasoning
        provider the thinking counts against it too. `light_reasoning` (form-filling calls)
        runs at the lightest effort the provider takes (its `light_effort` trait): "none" on
        DeepSeek (non-thinking, the same setting its forced tool turns use), "low" on Kimi,
        "minimal" elsewhere. A reply that is still
        cut off or empty after the correction round is `model_output_empty`, told
        apart from `model_output_invalid` (text that does not fit the schema).
        """
        endpoint = self._endpoint_for(model)
        effort: str | None = None
        if light_reasoning:
            effort = self._traits[endpoint.slug].get("light_effort") or "minimal"
        allowed = {name for name in (tools or set()) if name == "web_search"}
        resolved_schema = json_schema or schema.model_json_schema()
        # Not every provider honors text.format (GLM returns prose), so the
        # schema is also stated in the instructions and the reply is parsed
        # leniently: the first JSON object in the text.
        request: dict[str, Any] = {
            "model": endpoint.slug,
            "instructions": instructions + "\n\n只输出一个符合下面 JSON Schema 的 JSON 对象，不要 Markdown、不要解释：\n"
            + json.dumps(resolved_schema, ensure_ascii=False),
            "input": [{"role": "user", "content": input_text}], "stream": True, "store": False,
            "text": {"format": {"type": "json_schema", "name": schema.__name__,
                                "schema": resolved_schema, "strict": False}},
            **({"max_output_tokens": max_output_tokens} if max_output_tokens else {}),
        }
        if allowed:
            request["tools"] = self._tool_definitions(allowed)
        tool_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        tokens = 0
        corrected = False
        try:
            async with asyncio.timeout(timeout_seconds):
                for _ in range(5):
                    result = await self._stream(
                        endpoint, request, turn=None,
                        on_summary=(lambda summary: on_progress(stage="模型正在分析交易条件", summary=summary))
                        if on_progress else None, reasoning_effort=effort,
                    )
                    tokens += result.usage_tokens or max(
                        self._estimate_tokens(request["input"]) + len(result.text) // 2, 1)
                    if not result.calls:
                        try:
                            value = schema.model_validate_json(extract_json_object(result.text))
                        except ValueError as error:
                            problem = str(error)[:600]
                            # Cut off at the output cap (response.incomplete) or no text at all: not a format error.
                            empty = result.incomplete or not result.text.strip()
                            log.warning("structured_output_invalid schema=%s corrected=%s empty=%s incomplete=%s problem=%s",
                                        schema.__name__, corrected, empty, result.incomplete, problem.replace("\n", " | "))
                            if corrected:
                                raise NativeAgentError("model_output_empty" if empty else "model_output_invalid") from error
                            # One correction round with the validator's own words (10-08 线上：一句话建策略
                            # 三次有两次因一个越界字段整份作废): the model fixes its JSON, nothing else changes.
                            corrected = True
                            request["input"].extend(result.reasoning_items)
                            request["input"].append({"role": "assistant", "content": result.text[:20000] or "（空）"})
                            request["input"].append({"role": "user", "content":
                                "上面的 JSON 没有通过校验，问题如下（字段路径与原因）：\n" + problem
                                + "\n只修正这些问题，其余内容保持不变；字段取值必须在 Schema 允许的范围内"
                                "（没有的项用 null 或省略，不要用超出范围的数代替）。只输出修正后的一个 JSON 对象。"})
                            continue
                        return StructuredResult(value, tool_calls, tokens)
                    # Same shape as a conversation round: reasoning, every call,
                    # then every output. DeepSeek rejects call/output interleaving.
                    request["input"].extend(result.reasoning_items)
                    for call in result.calls:
                        if call.name not in allowed:
                            raise NativeAgentError("unexpected_tool")
                        request["input"].append({"type": "function_call", "call_id": call.call_id,
                                                 "name": call.name, "arguments": call.arguments or "{}"})
                    for call in result.calls:
                        try:
                            arguments = json.loads(call.arguments or "{}")
                        except json.JSONDecodeError:
                            arguments = {}
                        if on_progress:
                            on_progress(stage="正在联网检索")
                        search = self.trader_tools.web_search
                        try:
                            if search is None:
                                raise WebSearchError("web_search_unconfigured")
                            output = await search.search(
                                str(arguments.get("query") or ""), count=int(arguments.get("count") or 5))
                        except (WebSearchError, ValueError, TypeError) as error:
                            output = {"error": str(error)}
                        tool_calls.append((call.name, arguments, output))
                        request["input"].append({"type": "function_call_output", "call_id": call.call_id,
                                                 "output": json.dumps(output, ensure_ascii=False)})
            raise NativeAgentError("model_request_failed")
        finally:
            self._record_structured_usage(tokens, usage_user or self.usage_user)

    def _record_structured_usage(self, tokens: int, user: str | None) -> None:
        # The shared (owner-password) adapter serves everyone: the caller names the user.
        if not user or tokens <= 0:
            return
        try:
            self.store.add_token_usage(user, datetime.now(UTC).date().isoformat(), tokens)
        except Exception:  # quota accounting must never break the caller
            log.exception("token_usage_record_failed")


__all__ = [
    "ModelEndpoint", "NativeAgentAdapter", "NativeAgentError", "StructuredResult",
    "build_model_endpoints", "default_model_slug",
]
