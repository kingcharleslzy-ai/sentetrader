"""我的流程 (owner design 2026-10-06): one sentence — 「老规矩」 — runs a chain of things in order.

The user says the steps once and names them ("以后我说『老规矩』就……"); the AI turns that into a
流程卡 (a draft: the steps, each with its type) and only the user's own 保存 writes it. Saying the name
(or "跑一下老规矩", or 运行 in 设置 → AI 与记忆 → 我的流程) runs it: the native agent works the steps in
order, each step offered only the tools of its type, and the turn's progress card ticks them off.

A routine is never a way around the order confirmation: no step type carries a direct order tool (the
whole run withholds execute_simulation_trade and every other execution/settings tool, autonomy on or
off), an order step only makes a card the user confirms, and a draft whose name or steps ask for 免确认
下单、自动下单、改风险设置或自主执行开关 is refused without a 保存 button."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import user_memory
from .store.routines import RoutineDraftUsed, RoutineLimitError, RoutineNameTaken

MAX_ROUTINES = 5
MAX_STEPS = 10
NAME_MAX = 12
STEP_TEXT_MAX = 60

StepKind = Literal["read", "draw", "backtest", "margin", "card", "alert", "watch", "review"]
# kind → (the card's type chip, what the step may do). The chips are the approved sample's words.
STEP_KINDS: dict[str, tuple[str, str]] = {
    "read": ("读", "读今日判断、调研、简报、持仓与账户"),
    "draw": ("画线", "在图上画支撑、阻力和失效位"),
    "backtest": ("回测", "回测一个打法或策略"),
    "margin": ("算", "算保证金与可开手数"),
    "card": ("出卡", "出开仓 / 平仓卡（只出卡，下单要你确认）"),
    "alert": ("提醒", "设到价提醒"),
    "watch": ("提醒", "盯品种的盘中异动（加进 AI 简报订阅）"),
    "review": ("复盘", "读今日复盘与交易日志"),
}
# The only tools a step of each type is offered (and may run). Nothing here places, cancels or arms an
# order, touches 自主执行, risk or preferences: the routine's whole run is cards-only by construction.
STEP_TOOLS: dict[str, frozenset[str]] = {
    "read": frozenset({
        "get_research_brief", "get_market_board", "get_market_snapshot", "get_market_analysis",
        "get_futures_decision_context", "get_account_summary", "get_positions", "get_account_activity",
        "search_instruments", "get_news_flash", "get_foreign_markets", "get_economic_calendar",
        "get_interest_rates", "get_preferences", "recall_memory", "get_chart_bars", "get_settlement_prices",
        "get_main_contract_history", "get_symbol_ranking", "web_search",
    }),
    "draw": frozenset({"list_chart_drawings", "create_chart_drawing", "get_market_analysis", "get_chart_bars",
                       "get_market_snapshot", "search_instruments"}),
    "backtest": frozenset({"run_strategy_backtest", "run_tqsdk_historical_backtest", "run_reproducible_backtest",
                           "list_strategy_templates", "preview_strategy_template", "list_strategy_drafts",
                           "get_strategy_draft", "search_instruments"}),
    "margin": frozenset({"estimate_order", "get_account_summary", "get_positions", "get_market_snapshot",
                         "search_instruments"}),
    "card": frozenset({"propose_order", "estimate_order", "get_positions", "get_market_snapshot", "search_instruments",
                       "recall_memory"}),
    "alert": frozenset({"create_price_alert", "list_price_alerts", "get_market_snapshot", "search_instruments"}),
    "watch": frozenset({"app_call", "get_positions", "search_instruments"}),
    "review": frozenset({"get_daily_review", "get_trade_journal", "get_review_picks", "regenerate_daily_review"}),
}
# app_call is offered to one step type only, and there only for these operations.
STEP_APP_OPERATIONS: dict[str, frozenset[str]] = {
    "watch": frozenset({"GET /api/app/brief-subscriptions", "PUT /api/app/brief-subscriptions"}),
}
# Withheld for the whole run whatever the step says (the 对象级 AI's rule, widened): direct orders and
# every execution, stop, autonomy, strategy-monitor, risk-rule and settings tool.
NEVER_IN_ROUTINE = frozenset({
    "execute_simulation_trade", "cancel_simulation_order", "set_simulation_stop_loss", "cancel_simulation_stop_loss",
    "resume_simulation_execution", "stop_simulation_execution", "start_strategy_monitoring",
    "pause_strategy_monitoring", "update_preferences", "app_capabilities",
    "propose_routine", "run_routine",
})
CARD_TOOLS = frozenset({"propose_order"})

# What a routine may never be: 免确认下单、自动下单、绕过确认、改风险设置、改自主执行开关. The memory
# layer's authorization wording is the base (a memory is never an authorization either).
_NO_CONFIRM = (r"免确认|不用确认|无需确认|不需要确认|不要确认|别确认|跳过确认|绕过|不用问我|别问我|不必问我|不经过我"
               r"|直接(?:下单|成交|买|卖|开仓|平仓|加仓|减仓|反手|执行)|自动(?:下单|成交|开仓|平仓|加仓|减仓|反手|确认|执行)"
               r"|(?:替我|代我|帮我)确认|自主执行|全自动")
_SETTINGS = r"风险(?:设置|规则|限额|参数|上限|控制)|风控|亏损上限|授权|权限|实盘|急停"
ROUTINE_REFUSAL_RE = re.compile(f"{user_memory.AUTHORIZATION_RE.pattern}|{_NO_CONFIRM}|{_SETTINGS}")
# The user's own words asking for it (judged on the sentence the draft came from).
USER_REFUSAL_RE = re.compile(f"{_NO_CONFIRM}|{_SETTINGS}")
REFUSAL_MESSAGE = ("流程里不能放免确认下单、自动下单、改风险设置或自主执行开关：要下单的步骤只出卡、等你点确认，"
                   "这些设置请到设置里自己改。")
CARD_NOTE = "带「出卡」的步骤只出卡，下单仍要你点确认"
# Short words that already mean something on their own (a card's 确认, the call's 停).
_RESERVED_NAMES = frozenset({"确认", "取消", "停", "停止", "好", "好的", "是", "对", "不", "不要", "算了", "继续", "下一步"})
_NAME_STRIP = re.compile(r"^[\s「」『』“”\"'《》【】]+|[\s「」『』“”\"'《》【】。！!？?，,]+$")


class RoutineInvalid(ValueError):
    """A 422 refusal; the message is the user's sentence."""


class StepIn(BaseModel):
    kind: StepKind
    text: str = Field(min_length=1, max_length=STEP_TEXT_MAX * 2)


def clean_name(name: str) -> str:
    cleaned = _NAME_STRIP.sub("", re.sub(r"\s+", "", name or ""))
    if not cleaned:
        raise RoutineInvalid("流程要有个名字，比如「老规矩」")
    if len(cleaned) > NAME_MAX:
        raise RoutineInvalid(f"流程名最多 {NAME_MAX} 个字")
    if cleaned in _RESERVED_NAMES:
        raise RoutineInvalid(f"「{cleaned}」已经有别的意思（确认 / 取消 / 停），换个名字吧")
    return cleaned


def clean_steps(steps: list[Any]) -> list[dict[str, str]]:
    if not steps:
        raise RoutineInvalid("流程至少要有一步")
    if len(steps) > MAX_STEPS:
        raise RoutineInvalid(f"一个流程最多 {MAX_STEPS} 步，这次有 {len(steps)} 步；可以拆成两个流程")
    cleaned = []
    for step in steps:
        kind = str(getattr(step, "kind", None) or (step.get("kind") if isinstance(step, dict) else "") or "")
        text = re.sub(r"\s+", " ", str(getattr(step, "text", None) or (step.get("text") if isinstance(step, dict) else "") or "")).strip()
        if kind not in STEP_KINDS:
            raise RoutineInvalid(f"不认识的步骤类型 {kind!r}；能放的是：读、画线、回测、算保证金、出卡、到价提醒、盯异动、复盘")
        if not text:
            raise RoutineInvalid("每一步都要写做什么")
        if len(text) > STEP_TEXT_MAX:
            raise RoutineInvalid(f"每一步最多 {STEP_TEXT_MAX} 个字")
        cleaned.append({"kind": kind, "text": text})
    return cleaned


def refusal_reason(name: str, steps: list[dict[str, str]], user_text: str | None = None) -> str | None:
    """The 流程卡's refusal (no 保存 button) when the name, a step or the user's own sentence asks for
    what a routine may never do; None when it may be saved."""
    if ROUTINE_REFUSAL_RE.search(name) or any(ROUTINE_REFUSAL_RE.search(step["text"]) for step in steps):
        return REFUSAL_MESSAGE
    if user_text and USER_REFUSAL_RE.search(user_text):
        return REFUSAL_MESSAGE
    return None


def step_view(step: dict[str, str]) -> dict[str, str]:
    """A step as cards and lists show it; `manner` marks a writing step the user left to judgment
    (advisory 只给建议 / conditional 需要时执行), absent on an ordinary step."""
    view = {"kind": step["kind"], "kindLabel": STEP_KINDS[step["kind"]][0], "text": step["text"]}
    manner = step_manner(step)
    if manner:
        view["manner"] = manner
    return view


def routine_view(routine: dict[str, Any]) -> dict[str, Any]:
    steps = [step_view(step) for step in routine["steps"]]
    return {"id": routine["id"], "name": routine["name"], "steps": steps, "stepCount": len(steps),
            "createdAt": routine["createdAt"], "updatedAt": routine["updatedAt"], "lastRunAt": routine["lastRunAt"]}


def routine_card(draft: dict[str, Any], *, turn_id: str | None = None) -> dict[str, Any]:
    """A `card.routine` payload (流程卡) from a draft: draft → 保存 / 改一下; saved; refused (no 保存)."""
    steps = [step_view(step) for step in draft["steps"]]
    status = "saved" if draft.get("savedAt") else "refused" if draft.get("refusal") else "draft"
    return {"turnId": turn_id or draft.get("turnId") or draft["id"], "cardId": f"routine-{draft['id']}",
            "draftId": draft["id"], "routineId": draft.get("routineId"),
            "mode": "update" if draft.get("routineId") else "create", "name": draft["name"], "steps": steps,
            "status": status, "refusal": draft.get("refusal"), "savedAt": draft.get("savedAt"),
            "note": CARD_NOTE if any(step["kind"] == "card" for step in draft["steps"]) else None}


# -- running -----------------------------------------------------------------------------------------

_RUN_STATUS = Literal["waiting", "active", "done", "confirm", "failed", "skipped"]


def run_card(*, turn_id: str, routine: dict[str, Any], states: list[dict[str, Any]], status: str,
             started_at: str, elapsed: float, finished_at: str | None = None) -> dict[str, Any]:
    """A `card.routine_run` payload (进度卡): every step with its state and one result line, republished
    under the same cardId as the run moves on."""
    steps = [{**step_view(step), "status": state["status"], "result": state.get("result")}
             for step, state in zip(routine["steps"], states, strict=True)]
    return {"turnId": turn_id, "cardId": f"routine-run-{turn_id}", "routineId": routine["id"], "name": routine["name"],
            "status": status, "steps": steps, "startedAt": started_at, "finishedAt": finished_at,
            "elapsedSeconds": round(max(0.0, elapsed), 1),
            "confirmCount": sum(state["status"] == "confirm" for state in states)}


# A writing step (提醒、画线、盯异动) the user worded as "看看有没有必要 / 要不要 / 需要的话" only advises:
# its writing tools are withheld and the model says what it would do and "要设就说一声" (所有者 2026-10-06
# 测试：说了“会先问”就必须先问). The same words followed by an execution clause ("需要就设 / 有必要就设 /
# 该设就设 / 需要的话就设 / 合适就设") are conditional: the model judges and, if needed, sets it itself.
# The user's own words decide; a 出卡 step's card waits for 确认 anyway.
ADVISORY_RE = re.compile(r"有没有必要|有无必要|要不要|需不需要|是否需要|是否要|有必要的话|必要时|需要的话|有需要|"
                         r"视情况|看情况|如果需要|若需要|如有需要|酌情")
EXECUTE_RE = re.compile(r"(?:需要|有必要|必要|合适|值得|该[设画加盯挂]|要[设画加盯挂])(?:的话|时)?[，,]?\s*(?:就|便)"
                        r"(?:设|画|加|盯|挂|建|标|做|弄)")
STEP_WRITE_TOOLS: dict[str, frozenset[str]] = {
    "alert": frozenset({"create_price_alert"}),
    "draw": frozenset({"create_chart_drawing"}),
}
STEP_WRITE_OPERATIONS: dict[str, frozenset[str]] = {"watch": frozenset({"PUT /api/app/brief-subscriptions"})}
# The action a step exists for (a read before it is only preparation): its writing tool or operation,
# or for 出卡 the card itself. Its last attempt decides the step — a failed one is not covered by a
# successful read (Codex #522 10-06: GET 订阅成功、PUT 超档 422 仍记“完成”), a successful retry is.
STEP_ACTIONS: dict[str, frozenset[str]] = {
    **STEP_WRITE_TOOLS, **STEP_WRITE_OPERATIONS,
    "card": CARD_TOOLS,
}
# A run picked by id whose routine was deleted while the run waited (#522): said plainly, nothing else runs.
GONE_LINE = "这个流程已经不在了（可能刚被删掉），这次没有运行。现有的流程在设置里的“我的流程”。"
REFUSED_CALL = "routine_refused"  # a call outside the step's tools: recorded as failed, never the step's action
StepManner = Literal["advisory", "conditional"]
MANNER_WORDS: dict[str, str] = {"advisory": "只给建议", "conditional": "需要时执行"}


def step_manner(step: dict[str, str]) -> StepManner | None:
    """How a writing step runs by the user's words: advisory (只给建议, no writing tools), conditional
    (需要时执行: the model judges, then may write), or None (an ordinary step, or not a writing kind)."""
    if step["kind"] not in STEP_WRITE_TOOLS and step["kind"] not in STEP_WRITE_OPERATIONS:
        return None
    if EXECUTE_RE.search(step["text"]):
        return "conditional"
    return "advisory" if ADVISORY_RE.search(step["text"]) else None


def advisory_step(step: dict[str, str]) -> bool:
    """A writing step the user asked to only consider (“看看有没有必要设提醒”)."""
    return step_manner(step) == "advisory"


def step_tools(kind: str, available: set[str] | frozenset[str], *, advisory: bool = False) -> set[str]:
    """The tools a step of `kind` is offered, out of what this account's turn has at all (an advisory
    step: without its writing tools)."""
    tools = (set(STEP_TOOLS.get(kind, frozenset())) & set(available)) - NEVER_IN_ROUTINE
    return tools - STEP_WRITE_TOOLS.get(kind, frozenset()) if advisory else tools


def step_operations(kind: str, *, advisory: bool = False) -> frozenset[str]:
    """The app_call operations a step may run (an advisory step: the reads only)."""
    operations = STEP_APP_OPERATIONS.get(kind, frozenset())
    return operations - STEP_WRITE_OPERATIONS.get(kind, frozenset()) if advisory else operations


def step_instruction(routine: dict[str, Any], index: int) -> str:
    step = routine["steps"][index]
    label, scope = STEP_KINDS[step["kind"]]
    total = len(routine["steps"])
    extra = ""
    if step["kind"] == "card":
        extra = "要下单就用出卡工具出卡，卡由用户点确认才下单；不要说已经下单。"
    elif step["kind"] == "watch" and not advisory_step(step):
        extra = ("盯异动：用 app_call 读 GET /api/app/brief-subscriptions，再用 PUT /api/app/brief-subscriptions "
                 "（body {\"products\": [\"交易所.品种\", …]} 写整段清单）把品种加进去；持仓合约 Runtime 本来就在盯。")
    manner = step_manner(step)
    if manner == "advisory":
        # The user's own words are the constraint: consider it, do not do it.
        extra = (f"用户这一步的原话是「{step['text']}」——只判断有没有必要、给建议，不要设、不要画、不要改订阅"
                 "（这一步也没有写入的工具）；有必要就说建议的价位或品种，并以「要设就说一声」结尾。")
    elif manner == "conditional":
        extra += (f"用户这一步的原话是「{step['text']}」——先判断有没有必要：有必要就用这一步的工具做好并说清价位或品种，"
                  "没必要就不做，说明为什么。")
    return (f"现在只做流程「{routine['name']}」的第 {index + 1}/{total} 步（{label}：{scope}）：{step['text']}。"
            f"前面步骤的结果在上文，接着用。只用这一步提供的工具；{extra}"
            "做完用一句话（不超过 40 字，带关键数字）说结果，不要写标题和列表；"
            "做不了就以「失败：」开头说明原因。")


def call_name(name: str, arguments: dict[str, Any]) -> str:
    """How a step's call is recorded for its outcome: the tool, or for app_call its operation."""
    if name != "app_call":
        return name
    try:
        return str(json.loads(str(arguments.get("requestJson") or "{}")).get("operation") or name)
    except (TypeError, ValueError, AttributeError):
        return name


def step_outcome(kind: str, text: str, calls: list[tuple[str, bool, bool]], *, judged: bool = False) -> tuple[str, str]:
    """(state, result line) of a finished step from the model's closing sentence and the step's tool
    calls [(call_name, ok, made_card)]. A step that read or did nothing through a tool is a failure — the
    model's words alone are never a result, except on a step the user left to judgment (只给建议 /
    需要时执行 and it judged no need); a card waiting for the user is 需你确认; a step whose own action
    (STEP_ACTIONS) last failed is a failure whatever it read first."""
    line = re.sub(r"\s+", " ", (text or "").strip())
    line = re.sub(r"^\*+|\*+$", "", line).strip()
    if len(line) > 80:
        line = line[:79] + "…"
    if line.startswith(("失败：", "失败:")):
        return "failed", line.split("：", 1)[-1].split(":", 1)[-1].strip() or "这一步没做成"
    if kind == "card" and any(made for _, ok, made in calls if ok):
        return "confirm", line or "出了卡，等你确认"
    actions = [ok for name, ok, _ in calls if name in STEP_ACTIONS.get(kind, frozenset())]
    if actions and not actions[-1]:
        return "failed", line or "这一步要做的事没做成"
    if any(ok for _, ok, _ in calls):
        return "done", line or "完成"
    if calls:
        return "failed", line or "这一步的工具没有成功"
    if judged and line:
        return "done", line  # 只给建议 / 判断不需要的一步：它的结果就是那句判断
    return "failed", "这一步没有调用任何工具，没做成"


def run_summary(routine: dict[str, Any], states: list[dict[str, Any]], *, cancelled: bool = False) -> str:
    """The run's closing message (the voice reads it; the card shows the detail)."""
    name = routine["name"]
    total = len(states)
    confirm = sum(state["status"] == "confirm" for state in states)
    failed = next((index for index, state in enumerate(states) if state["status"] == "failed"), None)
    if cancelled:
        done = sum(state["status"] in {"done", "confirm"} for state in states)
        return f"「{name}」已停止：做完 {done} 步，后面的没有做。"
    if failed is not None:
        reason = states[failed].get("result") or "这一步没做成"
        tail = f"后面 {total - failed - 1} 步没有做。" if failed < total - 1 else ""
        cards = "已出的卡照常可以确认。" if confirm else ""
        return f"「{name}」停在第 {failed + 1} 步：{reason}。{tail}{cards}"
    cards = f"，{confirm} 步出了卡等你确认" if confirm else ""
    return f"「{name}」跑完了：{total} 件事{cards}。"


# -- HTTP surface ------------------------------------------------------------------------------------


class DraftBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    steps: list[StepIn] = Field(max_length=40)
    # A change to an existing routine (改一下 / "老规矩去掉回测那步"): the routine's id.
    routineId: str | None = Field(default=None, max_length=40)
    # The conversation the 流程卡 belongs to (the embedded AI's own session and turn).
    sessionId: str | None = Field(default=None, max_length=80)
    turnId: str | None = Field(default=None, max_length=80)


class SaveBody(BaseModel):
    # The 流程卡's 保存 (the draft carries name and steps) …
    draftId: str | None = Field(default=None, max_length=40)
    # … or the user's own submission in the interface.
    name: str | None = Field(default=None, max_length=40)
    steps: list[StepIn] | None = Field(default=None, max_length=40)


class PatchBody(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    steps: list[StepIn] | None = Field(default=None, max_length=40)


class RunBody(BaseModel):
    # The chat the run happens in (the user's current conversation).
    sessionId: str = Field(min_length=1, max_length=80)
    # The AI starting it for the user: the user's own words asking for it (must name the routine and be
    # in what they said recently; the AI never starts a routine on its own).
    quote: str | None = Field(default=None, max_length=200)


def _status_for(error: Exception) -> int:
    return 409 if isinstance(error, (RoutineNameTaken, RoutineDraftUsed)) else 422


def _message_for(error: Exception) -> str:
    if isinstance(error, RoutineNameTaken):
        return f"已经有叫「{error}」的流程了，换个名字，或者说“改一下『{error}』”"
    if isinstance(error, RoutineLimitError):
        return f"最多 {MAX_ROUTINES} 个流程，先在设置 → AI 与记忆 → 我的流程里删一个"
    if isinstance(error, RoutineDraftUsed):
        return "这张流程卡已经保存过了"
    return str(error)


def register(application: FastAPI, ctx: Any) -> None:
    from .skill_registry import human_request
    store = ctx.services.store
    broker = ctx.services.broker
    current_user = ctx.current_user

    def listing(user_id: str) -> dict[str, Any]:
        return {"items": [routine_view(item) for item in store.list_routines(user_id)],
                "limits": {"routines": MAX_ROUTINES, "steps": MAX_STEPS, "nameChars": NAME_MAX},
                "kinds": [{"kind": kind, "label": label, "scope": scope} for kind, (label, scope) in STEP_KINDS.items()]}

    async def republish(user_id: str, draft: dict[str, Any]) -> None:
        if draft.get("sessionId") and broker.get_session(draft["sessionId"], user_id) is not None:
            await broker.publish(draft["sessionId"], kind="artifact", name="card.routine", payload=routine_card(draft))

    def require_human(request: Request, what: str) -> None:
        if not human_request(request):
            raise HTTPException(403, f"{what}只能由你在界面上操作：AI 只出流程卡，点“保存”才存")

    @application.get("/api/routines")
    async def list_routines(request: Request) -> dict[str, Any]:
        """我的流程：本人的全部流程（名称、步骤 [{kind, kindLabel, text}]、上次运行时间）、上限（5 个、每个 10 步、
        名称 12 字）和能放的步骤类型。只读。"""
        return listing(await current_user(request))

    @application.post("/api/routines/drafts", status_code=201)
    async def create_draft(request: Request, body: DraftBody) -> dict[str, Any]:
        """把一段做法拆成流程卡（草稿，不落成流程）：name 1–12 字、steps 1–10 步，kind 只能是
        read/draw/backtest/margin/card/alert/watch/review；routineId 表示改已有流程。名称或步骤（或用户这句话）
        含免确认下单、自动下单、改风险设置、自主执行开关之类时照样出卡，但 status=refused、没有保存按钮。带
        sessionId/turnId 时卡发到那段对话。超过 10 步、已有 5 个流程时新建 → 422。返回 {draft, card}。"""
        user_id = await current_user(request)
        try:
            name = clean_name(body.name)
            steps = clean_steps(list(body.steps))
        except RoutineInvalid as error:
            raise HTTPException(422, str(error)) from error
        routine_id = body.routineId
        names = store.routine_names(user_id)
        if routine_id is not None:
            if store.get_routine(user_id, routine_id) is None:
                raise HTTPException(404, "没有这个流程")
            if names.get(name, routine_id) != routine_id:
                raise HTTPException(409, _message_for(RoutineNameTaken(name)))
        elif name in names:
            # Same name as an existing routine: the draft is a change to it.
            routine_id = names[name]
        elif len(names) >= MAX_ROUTINES:
            raise HTTPException(422, _message_for(RoutineLimitError(str(MAX_ROUTINES))))
        session_id = body.sessionId
        if session_id is not None and broker.get_session(session_id, user_id) is None:
            session_id = None
        recent = [] if human_request(request) else store.recent_user_texts(user_id, days=1, limit=1)
        refusal = refusal_reason(name, steps, recent[0] if recent else None)
        draft = store.insert_routine_draft(user_id, routine_id=routine_id, name=name, steps=steps,
                                           session_id=session_id, turn_id=body.turnId if session_id else None,
                                           refusal=refusal)
        card = routine_card(draft)
        if session_id:
            await broker.publish(session_id, kind="artifact", name="card.routine", payload=card)
        return {"draft": {key: draft[key] for key in ("id", "routineId", "name", "steps", "refusal")}, "card": card}

    @application.post("/api/routines", status_code=201)
    async def save_routine(request: Request, body: SaveBody) -> dict[str, Any]:
        """保存流程：draftId（流程卡上的“保存”）或 name+steps（你在界面上提交）。只接受你本人在界面上的操作——
        AI 或外部 agent 调用一律 403（AI 只能出流程卡）。被拒的流程卡 422；超过 5 个 / 10 步 422；重名 409；
        同一张卡再保存 409。保存后流程卡在原对话里变成“已保存”。"""
        user_id = await current_user(request)
        require_human(request, "保存流程")
        if body.draftId:
            draft = store.get_routine_draft(user_id, body.draftId)
            if draft is None:
                raise HTTPException(404, "这张流程卡已过期，请再说一次")
            if draft["refusal"]:
                raise HTTPException(422, draft["refusal"])
            try:
                routine = store.save_routine_draft(user_id, body.draftId, limit=MAX_ROUTINES)
            except KeyError as error:
                raise HTTPException(404, "要改的流程已经删了") from error
            except (RoutineLimitError, RoutineNameTaken, RoutineDraftUsed) as error:
                raise HTTPException(_status_for(error), _message_for(error)) from error
            saved = store.get_routine_draft(user_id, body.draftId)
            if saved is not None:
                await republish(user_id, saved)
            return routine_view(routine)
        if body.name is None or body.steps is None:
            raise HTTPException(422, "需要 draftId，或 name 与 steps")
        try:
            name, steps = clean_name(body.name), clean_steps(list(body.steps))
        except RoutineInvalid as error:
            raise HTTPException(422, str(error)) from error
        refusal = refusal_reason(name, steps)
        if refusal:
            raise HTTPException(422, refusal)
        draft = store.insert_routine_draft(user_id, routine_id=None, name=name, steps=steps, session_id=None,
                                           turn_id=None, refusal=None)
        try:
            return routine_view(store.save_routine_draft(user_id, draft["id"], limit=MAX_ROUTINES))
        except (RoutineLimitError, RoutineNameTaken) as error:
            raise HTTPException(_status_for(error), _message_for(error)) from error

    @application.patch("/api/routines/{routine_id}")
    async def patch_routine(request: Request, routine_id: str, body: PatchBody) -> dict[str, Any]:
        """改名 / 改步骤（你本人在界面上；AI 改流程要出新的流程卡、你点保存）。规则同保存：12 字、10 步、
        不能含免确认下单之类（422），重名 409。"""
        user_id = await current_user(request)
        require_human(request, "改流程")
        routine = store.get_routine(user_id, routine_id)
        if routine is None:
            raise HTTPException(404, "没有这个流程")
        try:
            name = clean_name(body.name) if body.name is not None else routine["name"]
            steps = clean_steps(list(body.steps)) if body.steps is not None else routine["steps"]
        except RoutineInvalid as error:
            raise HTTPException(422, str(error)) from error
        refusal = refusal_reason(name, steps)
        if refusal:
            raise HTTPException(422, refusal)
        try:
            return routine_view(store.update_routine(user_id, routine_id, name=name, steps=steps))
        except RoutineNameTaken as error:
            raise HTTPException(409, _message_for(error)) from error

    @application.delete("/api/routines/{routine_id}")
    async def delete_routine(request: Request, routine_id: str) -> dict[str, Any]:
        """删除一个流程（设置里先二次确认；AI 只在你明确说删时调用）。"""
        user_id = await current_user(request)
        if not store.delete_routine(user_id, routine_id):
            raise HTTPException(404, "没有这个流程")
        return {"deleted": routine_id}

    @application.post("/api/routines/{routine_id}/run", status_code=202)
    async def run_routine(request: Request, routine_id: str, body: RunBody) -> dict[str, Any]:
        """运行流程：在 sessionId 那段对话里以你的名义发出“跑一下<名称>”，和你亲口说一样排队、计额度；运行的
        就是路径里这个 id 的流程（不按名字再找，别的流程名字像也不会跑错；排队期间被删则回答“这个流程已经不在了”，
        不运行别的）。AI 按步骤做完，要下单的步骤只出卡。AI 代你运行必须带 quote——你最近亲口说的、含流程名的
        原话（对不上 422），AI 不会自己启动流程。"""
        user_id = await current_user(request)
        routine = store.get_routine(user_id, routine_id)
        if routine is None:
            raise HTTPException(404, "没有这个流程")
        if not human_request(request):
            if not body.quote or user_memory._normalized(routine["name"]) not in user_memory._normalized(body.quote):
                raise HTTPException(422, "AI 代你运行流程必须带你说的、含流程名的原话 quote")
            try:
                user_memory.verify_quote(store, user_id, body.quote)
            except user_memory.MemoryRefused as error:
                raise HTTPException(422, str(error)) from error
        ctx.require_session(body.sessionId, user_id)
        from .routes.requests import MessageRequest
        return await application.state.enqueue_chat_message(
            request, body.sessionId, user_id, MessageRequest(text=f"跑一下{routine['name']}", routineId=routine["id"]))
