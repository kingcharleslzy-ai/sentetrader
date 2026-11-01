"""Deterministic routing for one typed (or delegated voice) turn — the fuzzy fast path, 2026-09-20.

The 09-19/09-20 fuzzy bench: four of the five remaining failure classes were model habits, not
understanding — a K-line read before every opening card, web_search on "帮我看看沪金", the same
sentence choosing different tools on consecutive runs, filler prefixes. `classify_turn` decides the
turn's kind from the words alone, reusing the tables the voice front already trusts:

- a fixed-table interface request executes directly (no model round), through the very
  `run_window_intent` the voice fast path uses;
- an order the order-intent parser is sure of (order_intent_parse, 意图重构第 3 步) is planned without a model
  round; words about an order that are not one now (问句、否定、转述、整句收回、条件单) never make a card; an
  order the parser could not settle goes to the trading AI with its one card tool, propose_order;
- a plain market question keeps the model but hands it a fixed narrowed tool set, temperature 0 and
  minimal reasoning, so tool choice is repeatable and one round suffices.

Anything ambiguous stays `general` (full tools, full reasoning) — this narrows, it never guesses.
Pure and synchronous apart from `run_window_intent`, which is the shared executor extracted from
voice_realtime so typed and spoken turns take literally the same command path.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from .message_context import (
    market_query_request,
    opinion_request,
    strategy_signal_request,
)
from .order_intent import CARD_ACTS, OrderIntent
from .order_intent_parse import MIXED_GAP, ORDER_GAPS, TIMED_GAPS, ParseContext, Uncertain, parse
from .strategy_editor import editor_open_line, editor_utterance
from .voice_intents import (
    Intent,
    intent_for_window,
    looks_like_ui,
    match_intent,
    match_product_word,
    match_quick_query,
    resolve_symbol,
)

# The trading AI's round for an order the fast parser read but could not settle (一句多单、改口、两个价格…,
# order_intent_parse.ORDER_GAPS): its one card tool, the contract search and the positions read — never the old
# card tools' parameters (direction, offset, 今昨, lots are the planner's). Market reads get the read tools and
# chart drawing; no web_search / get_research_brief — a question routed here asks the screen or the book.
CARD_TOOL = "propose_order"
ORDER_TOOLS = frozenset({CARD_TOOL, "search_instruments", "get_positions"})
# The tools that place, change or cancel an order, a position's protection, the AI's execution or a strategy's
# monitoring (or run a routine that cards): a no_card turn (问句、否定、转述、整句收回、建议、条件单) is offered none of
# them and runs none (#952 Codex: “该不该买两手螺纹” must not reach execute_simulation_trade through activate_skill).
# Stopping is not among them: “别再下单了 / 先别让 AI 下单了” is a negated order that asks exactly for a stop.
# The direct fills among them: an order placed without a card, or a card confirmed for the user. Owner 2026-10-16:
# in a turn the user's own message caused (typed, a voice delegate, chat:send, a routine or backtest the user ran) the
# AI never fills an order directly — it cards it (propose_order) and the user confirms; “AI 自主执行” is for what the
# AI starts itself (a strategy's automatic orders, spread execution), which never runs through a model turn.
DIRECT_FILL_TOOLS = frozenset({"execute_simulation_trade"})
ORDER_WRITE_TOOLS = DIRECT_FILL_TOOLS | {
    CARD_TOOL, "cancel_simulation_order", "set_simulation_stop_loss", "cancel_simulation_stop_loss",
    "resume_simulation_execution", "start_strategy_monitoring", "run_routine",
}
# The same through the catalog (app_call): the tools above and the routes behind them.
DIRECT_FILL_ROUTES = ("POST /api/proposals/{proposal_id}/confirm",)
STRATEGY_START_ROUTE = "POST /api/strategies/{strategy_id}/monitor/start"
ORDER_WRITE_ROUTES = (*DIRECT_FILL_ROUTES, "POST /api/simulation/", "POST /api/ai-execution/start", "POST /api/ai-execution/resume",
                      "POST /api/ai-execution/limits", "POST /api/ai-execution/stops/", "POST /api/ai-execution/breakeven/",
                      "POST /api/proposals/{proposal_id}/adjust-risk", "POST /api/strategies/{strategy_id}/monitor/start",
                      "POST /api/routines/{routine_id}/run")


def _app_request(arguments: dict | None) -> dict | None:
    """app_call's request (its requestJson, read the way native_agent._read_only_app_call reads it); None unreadable."""
    try:
        request = json.loads(str((arguments or {}).get("requestJson") or "{}"))
    except (TypeError, ValueError):
        return None
    return request if isinstance(request, dict) else None


def _reaches(name: str, arguments: dict | None, tools: frozenset[str], routes: tuple[str, ...]) -> bool:
    if name in tools:
        return True
    if name != "app_call":
        return False
    request = _app_request(arguments)
    if request is None:
        return True  # unreadable: fail closed
    operation = str(request.get("operation") or "")
    return operation.removeprefix("tool:") in tools or operation.startswith(routes)


def order_write(name: str, arguments: dict | None = None) -> bool:
    """Whether a tool call places, changes or cancels an order, protection, the AI's execution or a strategy's run
    (ORDER_WRITE_TOOLS / ORDER_WRITE_ROUTES) — what a no_card turn withholds and refuses."""
    return _reaches(name, arguments, ORDER_WRITE_TOOLS, ORDER_WRITE_ROUTES)


def fills_directly(name: str, arguments: dict | None = None) -> bool:
    """Whether a tool call fills an order without the user's card confirmation (DIRECT_FILL_TOOLS / _ROUTES) — what a
    turn the user's message caused withholds and refuses."""
    return _reaches(name, arguments, DIRECT_FILL_TOOLS, DIRECT_FILL_ROUTES)


STRATEGY_START_TOOL = "start_strategy_monitoring"


def _strategy_start_arguments(name: str, arguments: dict | None) -> dict | None:
    """The call as the strategy start it is, whichever way it is spelled — the tool itself, the catalog's
    `tool:start_strategy_monitoring` through app_call, or the HTTP route the tool shares — as the tool's own arguments
    ({strategyId, signalAction}); {} when app_call is unreadable (fail closed); None for any other call (#932 复核:
    one identity, never a prefix of one spelling)."""
    if name == STRATEGY_START_TOOL:
        return arguments if isinstance(arguments, dict) else {}
    if name != "app_call":
        return None
    request = _app_request(arguments)
    if request is None:
        return {}
    # The catalog dispatches an operation by its exact name (agent_api.AppCapabilities.call): so is it read here.
    operation = str(request.get("operation") or "")
    raw = request.get("body")
    body: dict = raw if isinstance(raw, dict) else {}
    if operation == f"tool:{STRATEGY_START_TOOL}":
        return body
    if operation == STRATEGY_START_ROUTE:
        path = request.get("path")
        return {**body, "strategyId": path.get("strategy_id") if isinstance(path, dict) else None}
    return None


def auto_strategy_start(name: str, arguments: dict | None = None) -> str | None:
    """The strategy a tool call starts (or switches) into monitoring that may order by itself — signalAction auto, or
    left out (the instance keeps its own, which may be auto); "" when the id is unreadable; None for any other call.
    A user's turn runs it only when the user asked for that strategy (#953 Codex: never slipped into “820挂一手”)."""
    started = _strategy_start_arguments(name, arguments)
    if started is None:
        return None
    action, strategy = started.get("signalAction"), started.get("strategyId")
    return str(strategy or "") if action in (None, "auto") else None
MARKET_QUERY_TOOLS = frozenset({
    "get_market_snapshot", "get_market_analysis", "get_positions", "get_account_summary",
    "get_futures_decision_context", "get_chart_bars", "annotate_chart", "search_instruments",
})
# Interface work the fixed table misses ("把图表那个东西调出来"): one app_call round, no business tools.
# The keyword heuristic only guesses, so the round has an exit (#759/#748): one that ends without
# attempting any interface write is re-run as a general turn (native_agent), never a fixed line.
UI_FALLBACK_TOOLS = frozenset({"app_call", "app_capabilities", "highlight_ui"})
# "按我的策略现在该怎么做": the read-only explain (it draws the numbered annotations itself), plus the
# chart tools in case the user's phrasing asks for more drawing. Tier-gated like every strategies tool;
# classify_turn only routes here when the caller says the feature is available.
STRATEGY_SIGNAL_TOOLS = frozenset({"explain_strategy_signal", "annotate_chart"})
# The routed subsets, for contract validation in tests (the fake Responses server allows these too).
ROUTE_TOOLSETS = (ORDER_TOOLS, MARKET_QUERY_TOOLS, STRATEGY_SIGNAL_TOOLS, UI_FALLBACK_TOOLS)

# What a turn whose words are about an order but not one now is told (意图重构第 3 步): answer as the words mean,
# no card (the card tool is withheld). A plain question needs no line.
NO_CARD_HINTS = {
    "advice": "本轮用户在问该不该下单，不是下单指令：给看法，不出卡；用户明确说要下再出卡。\n",
    "negated": "本轮用户说的是不要这样下单：不出卡，一句话回应。\n",
    "reported": "本轮用户在说已经发生的交易，不是新的下单：不出卡；要说账户现状只按本轮读回的。\n",
    "retracted": "本轮用户把这单自己收回了：不出卡，一句话说明没有下单。\n",
}
CONDITIONAL_PRICE_HINT = ("本轮是到价才下的条件单：不出即时卡；告诉用户可以先建一个价格提醒（create_price_alert），"
                          "到价提醒后再说一遍下单、出卡确认。用户同意才建提醒。\n")
CONDITIONAL_TIME_HINT = "本轮是到时间才下的单（开盘后、收盘前、明天、待会…）：不出即时卡，告诉用户到时候再说一遍。\n"


@dataclass(frozen=True)
class TurnRoute:
    kind: str  # "editor_ask" | "ui_intent" | "order_intent" | "order" | "strategy_signal" | "opinion" | "market_query" | "general"
    # ui_intent with `intent`: executed directly, no model turn. ui_intent with `tool_names` instead
    # (the shared UI owner) and every other non-general kind run a model turn over that
    # fixed subset. `general` leaves everything to the model as before.
    intent: Intent | None = None
    tool_names: frozenset[str] | None = None
    one_round: bool = False  # at most one tool round, then the answer
    temperature: float | None = None
    reasoning: str | None = None
    pending_card_reply: bool = False  # unresolved contract candidate answers a waiting card, not the chart
    # order_intent: the fast parser's intent (order_intent_parse), planned by the Runtime without a model turn.
    order: OrderIntent | None = None
    # The words are about an order but are not one now (问句、否定、转述、整句收回、建议、条件单): the turn answers as
    # they mean and the card tool is withheld; `hint` is the line the model is told, `mount` a skill it may need
    # (到价的条件单：价格提醒).
    no_card: bool = False
    hint: str = ""
    mount: str | None = None
    # The trading AI's order round (`ORDER_ROUTE`): every round must call a tool until propose_order comes back with a
    # card, a refusal or the planner's question — the model's prose never stands in for a card.
    card_required: bool = False


GENERAL_ROUTE = TurnRoute("general")
ORDER_ROUTE = TurnRoute("order", tool_names=ORDER_TOOLS, one_round=True, temperature=0.0, reasoning="minimal",
                        card_required=True)
EDITOR_ASK_ROUTE = TurnRoute("editor_ask")
OPINION_ROUTE = TurnRoute("opinion")


def _names_any_product(text: str, catalog: list[str]) -> bool:
    """Whether the sentence names two or more catalog products (match_product_word saw a name but
    no single one) — then no judgment card is attached."""
    lowered = text.lower()
    return sum(1 for name in catalog if len(name) >= 2 and name.lower()[:2] in lowered) >= 2

# A symbol intent deferred past the market check is a plain switch command ("切到螺纹"); a question
# riding on the name ("帮我看看沪金的新闻") stays with the model even though the shape matches.
_SYMBOL_QUESTION = re.compile(r"新闻|消息|为什么|原因|观点|解读|分析|建议|怎么样|咋样|怎样|如何|多少|吗|呢|亏|赚|盈")


def catalog_product_names(items: list[Any]) -> list[str] | None:
    """Product words shared by the Native router and spoken pending-card routing."""
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\d+$", "", str(item.get("instrumentName") or "")).strip()
        if name and re.search(r"[一-龥]", name) and name not in names:
            names.append(name)
        code = str(item.get("productId") or "").strip().upper()
        if re.fullmatch(r"[A-Z]{1,4}", code) and code not in names:
            names.append(code)
    return names or None


def order_route(text: str, catalog: list[str] | None) -> TurnRoute | None:
    """What the order-intent parser (order_intent_parse.parse, the one reading of order words) makes of a typed or
    delegated sentence: an order it is sure of → `order_intent` (the planner, no model); words about an order that
    are not one now → `no_card` with the line to answer by; an order it could not settle → the trading AI's
    `order` round (propose_order); anything else → None (the other routes decide)."""
    parsed = parse(text, ParseContext(products=tuple(catalog or ())))
    if isinstance(parsed, Uncertain):
        if parsed.gap in ORDER_GAPS:
            return ORDER_ROUTE
        if parsed.gap in TIMED_GAPS:
            return TurnRoute("general", no_card=True, hint=CONDITIONAL_TIME_HINT)
        if parsed.gap == MIXED_GAP:
            return GENERAL_ROUTE  # 命令 + 问句：完整回合两件都办，不按问句收走写工具（#932）
        return None
    if parsed.stance == "order":
        return TurnRoute("order_intent", order=parsed) if parsed.act in CARD_ACTS else None
    if parsed.stance == "conditional":
        price = str((parsed.cond or {}).get("type")) == "price"
        return TurnRoute("general", no_card=True, hint=CONDITIONAL_PRICE_HINT if price else CONDITIONAL_TIME_HINT,
                         mount="price-alerts" if price else None)
    return TurnRoute("general", no_card=True, hint=NO_CARD_HINTS.get(parsed.stance, ""))


def classify_turn(
    text: str, *, has_pending_card: bool, ui_available: bool, catalog: list[str] | None = None,
    strategies_available: bool = True, editor_open: bool = False,
) -> TurnRoute:
    """Where one typed turn goes. Pure; `catalog` is the instrument catalog's Chinese product
    names (matches orders, quick queries and market questions against real products). `strategies_available`
    is False when the user's tier withholds the strategies feature: the strategy-signal route then
    stays general rather than naming a tool the request would filter out. `editor_open`: the window
    shows the 策略编辑卡, whose 补一句 takes the strategy's own sentences."""
    user_text = text or ""
    # Pure navigation does not answer or replace a waiting card. Only the deterministic fixed
    # table can vouch that a sentence is navigation and nothing else (#756): the keyword heuristic
    # (`looks_like_ui`) also takes "取消这张卡，切到日线" / "确认并切到日线", so with a card waiting it
    # stays general — the full confirmation/editing toolbox, and a spoken turn retires the old card
    # before delegating.
    if has_pending_card:
        route = classify_turn(user_text, has_pending_card=False, ui_available=ui_available,
                              catalog=catalog, strategies_available=strategies_available,
                              editor_open=editor_open)
        if route.no_card:
            # 问句、否定、转述、条件单 while a card waits (#952 Codex): still no new card — the card tool stays withheld,
            # the waiting card's own reject / correction tools stay with the general turn.
            return TurnRoute("general", no_card=True, hint=route.hint, mount=route.mount)
        if route.kind != "ui_intent" or route.intent is None:
            return GENERAL_ROUTE
        if route.intent.symbol_query is not None and not route.intent.explicit_chart:
            return TurnRoute("general", pending_card_reply=True)
        return route
    # The 策略编辑卡 is open: a sentence about the strategy's own fields ("止损改成 20 跳" / "每次开两手")
    # is its 补一句 and never reaches the order / market routes — the same rule as the voice
    # router. A sentence naming its contract ("螺纹买两手") is an order; interface words stay interface.
    if editor_open and editor_utterance(user_text, catalog):
        return EDITOR_ASK_ROUTE
    # The fixed interface table first (theme, panels, duration, …), so "看看日线" is a duration
    # command, not a market read. Contract switches ("切到螺纹") wait until after the market check:
    # "帮我看看沪金" asks about gold, it does not switch the chart.
    intent = match_intent(user_text)
    if intent is not None and intent.kind != "symbol" and ui_available:
        return TurnRoute("ui_intent", intent=intent)
    # 意图重构第 3 步: the order words are read once, by the parser. A sure order is planned without a model; words
    # about an order that are not one now keep the route below them but never make a card.
    ordered = order_route(user_text, catalog)
    if ordered is not None and not ordered.no_card:
        return ordered
    route = _other_route(user_text, intent, ui_available=ui_available, catalog=catalog,
                         strategies_available=strategies_available)
    if ordered is None:
        return route
    return ordered if route.kind == "general" else replace(route, no_card=True)


def _other_route(user_text: str, intent: Intent | None, *, ui_available: bool, catalog: list[str] | None,
                 strategies_available: bool) -> TurnRoute:
    """The routes after the order words: the strategy's signal, a view, a market read, a contract switch, interface
    work (never words about an order: looks_like_ui asks the parser) — else the general turn."""
    # "按我的策略现在该怎么做 / 现在能开多吗" is a question about the strategy's signal, answered read-only by the
    # explain tool; a tier with strategies switched off falls through to the general turn instead of routing to a
    # hidden tool.
    if strategies_available and strategy_signal_request(user_text):
        return TurnRoute("strategy_signal", tool_names=STRATEGY_SIGNAL_TOOLS, one_round=True, temperature=0.0, reasoning="minimal")
    # 判断卡 (2026-10-04): a view on one product — the named one, or the window's contract when the
    # sentence names none. Two products named stay general (one card per turn). The turn itself is a
    # general model turn; native_agent attaches the research judgment card before it.
    if opinion_request(user_text) and (catalog is None or match_product_word(user_text, catalog) is not None
                                       or not _names_any_product(user_text, catalog)):
        return OPINION_ROUTE
    if (match_quick_query(user_text, catalog) is not None
            or market_query_request(user_text, catalog)):
        return TurnRoute("market_query", tool_names=MARKET_QUERY_TOOLS, temperature=0.0, reasoning="minimal")
    if intent is not None and intent.kind == "symbol" and ui_available and not _SYMBOL_QUESTION.search(user_text):
        return TurnRoute("ui_intent", intent=intent)
    if ui_available and looks_like_ui(user_text, catalog):
        return TurnRoute("ui_intent", tool_names=UI_FALLBACK_TOOLS, one_round=True, temperature=0.0, reasoning="minimal")
    return GENERAL_ROUTE


async def run_window_intent(
    intent: Intent, *, window: dict[str, Any],
    call: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """One UI command (plus its follow-ups) on `window` with a real receipt, and the one-line result
    to speak. `call` is the AppCapabilities bridge runner (operation, path=, query=, body=) — the
    voice service passes its own `_call`, the typed fast path passes an equivalent. Anything not
    `applied` reports ok=False so the caller falls back to the agent. A contract name is resolved
    through the catalog first."""
    adapted = intent_for_window(intent, window.get("state") or {})
    if adapted is None:
        return {"ok": False, "error": "mobile_only", "uiCommandAttempted": False}
    intent = adapted
    changes = dict(intent.changes)
    say = intent.say
    if changes.get("fontScale") in {"larger", "smaller"}:
        scales = ["standard", "large", "extra-large"]
        current = str(((window.get("state") or {}).get("fontScale")) or "standard")
        index = scales.index(current) if current in scales else 1
        target = index + (1 if changes["fontScale"] == "larger" else -1)
        if not 0 <= target < len(scales):
            return {"ok": True, "changes": {}, "say": "字已经最大了" if target >= len(scales) else "字已经最小了"}
        changes["fontScale"] = scales[target]
        say = "字放大了" if target > index else "字调小了"
    if changes.get("theme") == "toggle":
        current = str(((window.get("state") or {}).get("theme")) or "light")
        changes["theme"] = "light" if current == "dark" else "dark"
        say = f"换成{'浅色' if changes['theme'] == 'light' else '深色'}了，要换涨跌颜色就说“换涨跌色”"
    if intent.symbol_query is not None:
        catalog = await call("GET /api/instruments", query={"search": intent.symbol_query})
        match = resolve_symbol(intent.symbol_query, list((catalog.get("data") or {}).get("items") or []))
        if match is None:
            return {"ok": False, "error": "symbol_unresolved", "uiCommandAttempted": False}
        changes = {"symbol": match["symbol"]}
        say = f"切到{match.get('instrumentName') or match['symbol']}了"
    if intent.drawing is not None:
        # 画线第二版 (2026-10-09): the window's contract, through the same catalog route the toolbar and the
        # chart-drawings skill use; the route's Chinese refusal falls back to the agent like a rejected command.
        symbol = str((window.get("state") or {}).get("symbol") or "")
        if not symbol:
            return {"ok": False, "error": "no_symbol"}
        if intent.drawing.get("op") == "clear":
            result = await call("DELETE /api/market/drawings", query={"symbol": symbol})
            if not result.get("ok"):
                return {"ok": False, "error": str((result.get("data") or {}).get("detail") or "not_cleared")}
            cleared = int((result.get("data") or {}).get("cleared") or 0)
            say = f"删掉了这个合约的 {cleared} 条画线" if cleared else "这个合约没有画过线"
        else:
            body = {"symbol": symbol, "kind": intent.drawing["kind"], "points": intent.drawing["points"]}
            result = await call("POST /api/market/drawings", body=body)
            if not result.get("ok"):
                return {"ok": False, "error": str((result.get("data") or {}).get("detail") or "not_drawn")}
        if not changes and not intent.follow:
            return {"ok": True, "changes": {}, "say": say}
    revision = window["revision"]
    receipt: dict[str, Any] = {}
    for step in [changes, *intent.follow]:
        result = await send_ui_command(call, window, step, revision=revision)
        receipt = result.get("data") or {}
        if receipt.get("status") != "applied":
            return {"ok": False, "error": str(receipt.get("status") or result.get("status") or "not_applied"), "changes": step}
        revision = int(receipt.get("observedRevision") or receipt.get("revision") or revision + 1)
    if "strategyEditor" in changes:
        # 策略编辑卡: what the habits prefill filled (or why it could not) is in the card the receipt reports.
        say = editor_open_line(changes, receipt.get("state"))
    if any("researchDebate" in step for step in [changes, *intent.follow]):
        # 多 AI 研讨: the command is applied once the view took the request; what the Runtime answered (started,
        # reused, refused with why) lands a moment later in the window's state — say that, never a false start.
        say = await debate_start_line(receipt.get("state"), window["windowId"], call, intent.say)
    return {"ok": True, "changes": changes, "say": say}


async def send_ui_command(call: Callable[..., Awaitable[dict[str, Any]]], window: dict[str, Any], changes: dict[str, Any], *,
                          revision: int | None = None, wait_ms: int = 3000) -> dict[str, Any]:
    """One UI command on `window` (at its revision, or the one a previous step moved it to): the bridge result, the
    window's receipt in `data`. Every Runtime-side command — typed and spoken fast path, tour, 补一句 — is sent here."""
    body = {"commandId": str(uuid4()), "windowId": window["windowId"],
            "expectedRevision": window["revision"] if revision is None else revision, "changes": changes}
    return await call("POST /api/app/ui/commands", query={"waitMs": wait_ms}, body=body)


async def run_editor_ask(text: str, *, window: dict[str, Any], call: Callable[..., Awaitable[dict[str, Any]]]) -> bool:
    """补一句 on the open 策略编辑卡 (typed or spoken): the sentence as one UI command with a real receipt — the card
    runs the design job, highlights what changed and never saves by itself. Whether the window applied it."""
    result = await send_ui_command(call, window, {"strategyEditor": {"ask": text.strip()[:500]}})
    return ((result.get("data") if isinstance(result, dict) else None) or {}).get("status") == "applied"


DEBATE_POLL_SECONDS = 0.5
DEBATE_POLL_TRIES = 16


async def debate_start_line(state: Any, window_id: str, call: Callable[..., Awaitable[dict[str, Any]]], started: str) -> str:
    """The spoken result of a voice-started debate: started / reused, or the refusal's own words."""
    request = ((state or {}).get("researchDebate") or {}).get("request") if isinstance(state, dict) else None
    for _ in range(DEBATE_POLL_TRIES):
        if not isinstance(request, dict) or request.get("status") != "sending":
            break
        await asyncio.sleep(DEBATE_POLL_SECONDS)
        window = await call("GET /api/app/ui/windows/{window_id}", path={"window_id": window_id})
        request = (((window.get("data") or {}).get("state") or {}).get("researchDebate") or {}).get("request")
    status = request.get("status") if isinstance(request, dict) else None
    if status == "reused":
        return "这个议题今天已经研讨过，打开了已有结果"
    if status in ("refused", "error"):
        return f"没能开始研讨：{request.get('message') or '请稍后再试'}"  # type: ignore[union-attr]
    if status == "sending":
        return "研讨正在发起，好了会提醒你"
    if status == "started":
        return started
    # No answer to read (the view closed, another request replaced it, the read failed): never claim a start (#749).
    return "研讨请求已发出，但没确认开始；可以打开研讨看看"


# Assistant replies sometimes open with a spoken filler the instructions already ban ("好的，已切
# 到日线"). Stripped once, only at the start, and only when followed by punctuation, so 好价格 and
# 好的是 the word for "good" stay untouched.
_FILLER_PREFIX = re.compile(r"^(?:好的|好|收到|明白|嗯)[，,。！!？?、：:\s]+")


def strip_filler_prefix(text: str | None) -> str:
    """The published/spoken answer without its leading filler ("好的，" / "收到。"), if any."""
    if not text:
        return text or ""
    return _FILLER_PREFIX.sub("", text, count=1)
