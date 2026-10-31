from __future__ import annotations

import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .instruments import CONTEXT_SYMBOL_PATTERN
from .voice_intents import ALERT_WORDS

_CONTEXT_PREFIX = "AI_TRADER_FUTURES_CONTEXT_V1:"
# Internal voice → authenticated enqueue handoff. Never a public request field or authorization.
PENDING_CARD_REPLY: ContextVar[tuple[str, str] | None] = ContextVar("pending_card_reply", default=None)


class WorkspacePositionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    direction: Literal["long", "short"]
    volume: int = Field(ge=0, le=1_000_000)
    todayVolume: int = Field(default=0, ge=0, le=1_000_000)
    historicalVolume: int = Field(default=0, ge=0, le=1_000_000)
    frozenToday: int = Field(default=0, ge=0, le=1_000_000)
    frozenHistorical: int = Field(default=0, ge=0, le=1_000_000)
    openPrice: float | None = Field(default=None, gt=0)
    margin: float | None = Field(default=None, ge=0)
    unrealizedPnl: float = Field(default=0)


class WorkspaceOrderDraftContext(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    side: Literal["buy_open", "sell_open", "buy_close", "sell_close"]
    volume: int = Field(ge=1, le=1_000_000)
    limitPrice: float | None = Field(default=None, gt=0)
    stopPrice: float | None = Field(default=None, gt=0)
    source: Literal["voice", "context-menu", "three-key"]
    transcript: str | None = Field(default=None, max_length=500)


class WorkspaceOrderContext(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    clientOrderId: str = Field(min_length=1, max_length=160)
    symbol: str = Field(min_length=1, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    direction: str = Field(min_length=1, max_length=24)
    offset: str = Field(min_length=1, max_length=24)
    volume: int = Field(ge=0, le=1_000_000)
    volumeLeft: int = Field(ge=0, le=1_000_000)
    filledVolume: int = Field(ge=0, le=1_000_000)
    limitPrice: float | None = Field(default=None, gt=0)
    status: str = Field(min_length=1, max_length=40)
    insertDateTime: int = Field(ge=0)


class WorkspaceTradeContext(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    tradeId: str = Field(min_length=1, max_length=160)
    clientOrderId: str = Field(min_length=1, max_length=160)
    symbol: str = Field(min_length=1, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    direction: str = Field(min_length=1, max_length=24)
    offset: str = Field(min_length=1, max_length=24)
    price: float | None = Field(default=None, gt=0)
    volume: int = Field(ge=0, le=1_000_000)
    tradeDateTime: int = Field(ge=0)


class WorkspaceRecentActionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    action: Literal[
        "view_changed", "contract_selected", "duration_changed", "price_selected",
        "position_selected", "order_draft_created", "manual_order_submitted", "order_action",
    ]
    occurredAt: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    view: Literal["watch", "trade", "review", "board"] | None = None
    durationSeconds: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800] | None = None
    price: float | None = Field(default=None, gt=0)
    side: Literal["buy_open", "sell_open", "buy_close", "sell_close"] | None = None
    volume: int | None = Field(default=None, ge=1, le=1_000_000)
    positionDirection: Literal["long", "short"] | None = None
    orderAction: Literal["continue", "abandon", "query", "cancel"] | None = None
    clientOrderId: str | None = Field(default=None, max_length=160)


class WorkspaceEventContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategyId: str | None = Field(default=None, max_length=100)
    eventType: Literal["fill", "attempt", "protection", "order"]
    eventId: str = Field(min_length=1, max_length=200)
    eventTime: str = Field(max_length=100)


class WorkspaceFocusContext(BaseModel):
    """对象级 AI: the object an "AI ▾" menu item asked about."""
    model_config = ConfigDict(extra="forbid")
    # strategy_edit (策略页改版第八批, 2026-10-08): the 结果页's 跟 AI 说怎么改 / the phone's 跟 AI 说……
    # input — the user's own words say how to change this strategy.
    kind: Literal["strategy", "strategy_instance", "position", "order", "strategy_edit"]
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=80)


class FuturesWorkspaceContext(BaseModel):
    selectedEvent: WorkspaceEventContext | None = None
    focus: WorkspaceFocusContext | None = None
    """Small, display-derived context; never an execution authorization source."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    view: Literal["watch", "trade", "review", "board"]
    # "depth" (the 价格阶梯, removed 2026-10-07) is only sent by older desktop clients.
    rightDock: Literal["ai", "depth", "order"] | None = None
    symbol: str = Field(min_length=1, max_length=80, pattern=CONTEXT_SYMBOL_PATTERN)
    instrumentName: str | None = Field(default=None, max_length=120)
    durationSeconds: Literal[60, 180, 300, 900, 1800, 3600, 86400, 604800]
    selectedPrice: float | None = Field(default=None, gt=0)
    quoteDatetime: str = Field(min_length=1, max_length=80)
    capturedAt: str = Field(min_length=1, max_length=80)
    ageSeconds: float = Field(ge=0, le=604_800)
    dataStatus: Literal["simulated", "normal", "stale", "unavailable", "backup"]
    marketSessionStatus: Literal["trading", "closed", "unknown"] = "unknown"
    lastPrice: float | None = Field(default=None, gt=0)
    bidPrice1: float | None = Field(default=None, gt=0)
    askPrice1: float | None = Field(default=None, gt=0)
    priceTick: float | None = Field(default=None, gt=0)
    positions: list[WorkspacePositionContext] = Field(default_factory=list, max_length=12)
    selectedPosition: WorkspacePositionContext | None = None
    orderDraft: WorkspaceOrderDraftContext | None = None
    activeOrderCount: int = Field(default=0, ge=0, le=10_000)
    activeOrders: list[WorkspaceOrderContext] = Field(default_factory=list, max_length=8)
    recentTrades: list[WorkspaceTradeContext] = Field(default_factory=list, max_length=8)
    recentActions: list[WorkspaceRecentActionContext] = Field(default_factory=list, max_length=12)


@dataclass(frozen=True)
class DecodedInstruction:
    user_text: str
    model_text: str
    requires_web_search: bool = False
    ui_window_id: str | None = None
    # "voice": the turn was delegated by the real-time voice front end; the agent answers in the same
    # session as typed turns and adds a 1–3 sentence `spoken` summary for the voice to read.
    origin: str | None = None
    # "ptt": the voice turn came from 按住说话, not a call (origin stays "voice" for the spoken summary).
    push_to_talk: bool = False
    pending_card_reply: bool = False
    # A voice turn that only reads simple facts (price, positions, account, research): no freshness
    # gate, minimal reasoning, read-only tools — the "fast answer" path.
    fast: bool = False
    # 对象级 AI (2026-09-25): the object the question was asked about ({kind, id, label}), from an
    # "AI ▾" menu on a strategy, a running instance, a position or an order.
    focus: dict[str, str] | None = None
    # 我的流程 (#522): the routine the user picked to run (设置里的“运行” / POST /api/routines/{id}/run);
    # the run takes it by id, the user text is only what the chat shows.
    routine_id: str | None = None


def focus_instruction(focus: dict[str, str]) -> str:
    """The object-level AI turn: read the object's facts first, answer about it, never trade."""
    if focus.get("kind") == "strategy_edit":
        return (
            f"本轮用户是在策略结果页（或手机策略页）的“跟 AI 说”输入框里直接说了要怎么改这条策略：focus={json.dumps(focus, ensure_ascii=False)}。"
            "先用 get_strategy_draft 读取它的定义（id 是策略库版本或运行实例的 strategyId），按用户的话改 definition.plan，用 create_strategy_draft"
            "（plan 填改后的完整定义，baseStrategyId 填这个 strategyId）另存为新版本——原版本和运行中的实例都不变；再用 run_strategy_backtest（strategyId 填新版本）"
            "回测，用一两句大白话对比改前改后（一共赚多少、最多一次亏多少、胜率）。听不出要改哪一项时先问一句再改。"
            "不上线、不改执行方式、不出下单卡、不下单。\n"
        )
    readers = {
        "strategy": "get_strategy_draft 读取这条策略的定义",
        "strategy_instance": "get_strategy_monitor_status 读取运行状态与执行记录，再用 explain_strategy_signal（strategyId）读取当前信号",
        "position": "get_positions 读取这笔持仓（必要时 get_market_snapshot）",
        "order": "get_account_activity 读取这笔委托（必要时 query_simulation_order 按 clientOrderId 回读）",
    }
    return (
        f"本轮用户是从界面上一个对象的“AI”菜单发问的：focus={json.dumps(focus, ensure_ascii=False)}。"
        f"先用现有工具读取这个对象的事实（{readers.get(focus.get('kind', ''), '对应的读取工具')}），再只针对它回答。"
        "平仓、追单这类交易建议只能用 propose_order 出卡，由用户在卡上确认；"
        "本轮不得直接下单。调整策略参数：先用文字列出 旧值→新值 和理由，不修改正在运行的版本；"
        "用户同意后用 create_strategy_draft（plan 填改后的完整定义，baseStrategyId 填该策略或实例的 strategyId）另存为新版本，"
        "回测与上线仍由用户决定。\n"
    )


VOICE_TURN_INSTRUCTION = (
    "本轮问题来自实时语音通话（用户是说出来的，回答的第一段会被逐句实时念给用户）。"
    "回答的第一段用口语直接回答问题，最多 5 句、不用符号和列表、数字照原文、不加称呼和“好的”；"
    "细节、表格、来源放在第一段之后另起段落（对话面板显示，不会念）。"
    "解释走势/指标/形态的问题只用 get_market_analysis（必要时加 get_chart_bars），不调 get_market_snapshot、get_futures_decision_context；后者只用于方向判断。"
    "查持仓只调 get_positions（不再调 get_account_summary / get_account_activity / get_runtime_data_status）；查账户只调 get_account_summary。"
    "问价格、涨跌、成交量、某品种怎么样：只用 get_market_snapshot / get_market_analysis，不联网；"
    "问库存、基差、持仓排名、仓单、期限结构、手续费保证金参考：用 get_research_brief 对应主题；"
    "问消息面、新闻、观点、为什么涨跌：用 get_research_brief 的 news；要综合分析某品种、问怎么看：先用 get_research_brief 的 desk（联网搜索之前）；"
    "只有核验当前日期、时效性时才 web_search。"
    "“哪里能进/进场点/关键位/标一下”：get_market_analysis 后用 annotate_chart 把价位标上，不用 highlight_ui。"
    "下单直接调 propose_order 出卡（Runtime 自己读最新行情/账户/持仓/委托、算开平今昨手数并做就绪检查，未就绪不出卡并给中文原因），"
    "不先调 get_futures_decision_context，也不先读持仓/账户/活动/K 线。"
    "一句话里既要看又要标（先看行情再标关键位）：本轮 get_market_analysis 之后必须调 annotate_chart 把位置标上。"
    "用户只是叮嘱或表达情绪（心情不好、别下单啊）：一句话回应，不调工具。提到实盘/真钱：一句话说明只有模拟盘，不生成任何方案。"
    "模拟账户没有风险规则（止损规则、每日亏损上限），设置里也没有这一项：说明不做，可改为给某个持仓挂止损单。"
    "用户要求标注、画图或讲解某段走势时才调 annotate_chart，且一轮内只调一次、一次把所有标注给全；其它问题不标注、不调 highlight_ui；能一轮回答就不要多轮查工具。"
    "出方案卡时第一段就是要确认的内容（合约、方向、手数、价格）并以“确认吗”结尾，不再另写正文；查询类回答数据不少于两行才用表格。"
    "开仓、平仓、全平、调到目标持仓、反手：只用 propose_order 生成待确认方案，只填用户说了的（说了价格就填 limitPrice，原样下单；没说价格不填，Runtime 按默认价格方式定价），没说的方向、手数、合约留空由 Runtime 追问；一句里有几单时写进 legs，Runtime 不出卡，照返回的话请用户一单一单说。"
    "方案卡只能由 propose_order 产生，不要用 app_call 去调下单/平仓接口（那样卡不会进本对话，用户也无法确认）；不要直接执行。"
    "用户会在语音里说“确认”或“算了”，那等价于点方案卡的确认或放弃；一张卡被放弃或超时后就结束了，用户再要求时必须重新生成新卡，不能说“卡还在等确认”。"
    "撤单、启停策略：本轮先用一句话复述要做什么并请用户确认，用户下一句确认后再执行。"
    "改账号偏好（语速、音色、回答长短、默认手数、默认价格方式、播报和通知开关等）不用确认：直接用 update_preferences 改好，回一句改了什么。"
    "切合约、切页面、开关面板和设置这类界面请求：必须通过 app_call 真正发出 POST /api/app/ui/commands 并等到 applied 才能说“已经切好/已经打开”，否则如实说没做到。"
    "不要让用户去“点击”“查看”界面上的东西，不要输出链接和工具名。"
    "用户要求画图或讲解某段走势时先画后说：先 get_chart_bars 再 annotate_chart（一轮一次给全；多空点位用 point、压力支撑用线或区、可画在当前打开的副图 kd/macd/rsi/oi，"
    "最多 6 条、短标签），spoken 里按图上编号指着说（“看图上①，15420 空进；②是止损”），要指路就用 highlight_ui 把位置亮出来再说“看亮起来的地方”。"
    # 第 5 批 (owner 2026-10-25): the chart talk names levels and scenarios, never a 偏多 / 偏空 verdict.
    "在图上讲解行情只讲价位和情景（到了哪个价位会怎样、站上或跌破之后看哪里），不下偏多、偏空的方向结论，也不说信心高低。"
    "用户说“带我看看/教我用/怎么下单”就用 start_tour 开分步引导，spoken 只念当前这一步的 say；用户说“下一步/继续”发 tour: {op: next}，“结束”发 tour: {op: stop}。\n"
)

# 按住说话 (09-28): the same spoken-answer rules without the call — the answer's first paragraph is
# read out once, and a card is confirmed on the card itself (no spoken 确认, no confirm window).
PTT_TURN_INSTRUCTION = VOICE_TURN_INSTRUCTION.replace(
    "本轮问题来自实时语音通话（用户是说出来的，回答的第一段会被逐句实时念给用户）。",
    "本轮问题是用户按住说话说出来的（不是通话），回答的第一段会由软件自动念给用户听——这条朗读不需要接通通话，也不用你调用任何播报接口；"
    "运行状态里显示“没有通话”不影响它。不要说你发不出声音，也不要让用户为了听你的回答去开通话。",
).replace(
    "用户会在语音里说“确认”或“算了”，那等价于点方案卡的确认或放弃；",
    "方案卡由用户在卡片上点确认或放弃（不是通话，没有口头确认）；",
)
assert PTT_TURN_INSTRUCTION.count("按住说话") == 1 and "没有口头确认" in PTT_TURN_INSTRUCTION

# The window focus a turn is told (UI fallback rounds): its contract, view and open surfaces.
SCREEN_FOCUS_PREFIX = "当前窗口焦点（系统已附上）："


# "按我的策略现在该怎么做 / 策略怎么看 / 现在能开多吗": the user asks what their strategy says about
# this moment. The explain tool evaluates the strategy on the current data source (live or the replay
# clock) and draws the numbered annotations itself, so the turn is one read-only tool round, decided
# here like the order intents. Managing the strategy itself — designing, backtesting, starting,
# stopping, changing parameters — is excluded and stays a general turn.
_STRATEGY_SIGNAL_INTENT = re.compile(
    r"按(?:我|咱|自己|咱们)的?策略"
    r"|策略(?:现在|当前|眼下|这会儿|到底)?(?:怎么看|怎么讲|怎么说|怎么样|咋样|怎样|如何)"
    r"|(?:现在|当前|眼下|这会儿|目前)?(?:该|能|可以|要不要|该不该|能不能)(?:开多|开空|做多|做空|平仓|平多|平空|平掉)[了吗呢]"
    r"|(?:该|能)(?:开|做)(?:多|空)还是(?:开|做)(?:多|空)"
    r"|策略信号")
_STRATEGY_SIGNAL_EXCLUDE = re.compile(
    r"回测|验证|设计|起草|生成|创建|新建|保存|删除|改名|修改|编辑|启动|开始|停止|暂停|恢复|停用|启用|设置|参数")
STRATEGY_SIGNAL_INSTRUCTION = (
    "本轮是策略信号解读请求：直接调用 explain_strategy_signal（不传参数即当前窗口合约与图表周期，"
    "回放中按回放时钟评估；它只读、不下单、不出方案卡，并已在图上画编号标注）。"
    "按返回的 conclusion/action 讲清楚现在该开多/开空/平仓还是观望，每条条件的满足情况和差距点到为止，"
    "正文按图上的编号（①②③）讲解；参考价和策略的止损止盈价有就带上，未验证的策略要说明未验证；"
    "多张监控中的策略时先把候选念给用户问清用哪张。"
    "用户随后说“那就下/按这个下”才另起一轮用 propose_order 出卡。\n"
)


def strategy_signal_request(user_text: str) -> bool:
    """A spoken/typed ask for what the user's strategy says right now: the read-only explain turn."""
    text = "".join(user_text.split())
    return bool(text) and _STRATEGY_SIGNAL_INTENT.search(text) is not None and _STRATEGY_SIGNAL_EXCLUDE.search(text) is None


# "螺纹怎么看 / 沪金能不能做 / 铁矿看多还是看空 / 你觉得能不能买" (判断卡, 2026-10-04): the user asks for a
# view on one product. The Runtime attaches the research desk's judgment for that product today (a
# card the model does not have to repeat) and the model answers below it as a general turn. News,
# reasons, the user's own positions and chart reading stay plain general turns.
_OPINION_INTENT = re.compile(
    r"怎么看|如何看|咋看|能不能做|能做吗|可以做吗|值得做吗|能不能买|能买吗|可以买吗|能不能空|能空吗|能不能进|能进吗"
    r"|看多还是看空|看空还是看多|看涨还是看跌|看跌还是看涨|偏多还是偏空|做多还是做空|多还是空"
    r"|有什么判断|什么判断|判断一下|你的判断|观点|看法|后市|后势")
_OPINION_EXCLUDE = re.compile(
    r"新闻|消息|为什么|原因|策略|回测|持仓|仓位|我的|我那|多单|空单|指标|K线|k线|MACD|macd|KD|kd|RSI|均线|图|这根|这波"
    r"|保证金|手续费|实盘|真钱|软件|界面|怎么用|怎么下单")
OPINION_INSTRUCTION = (
    "本轮是问看法：系统已在回答上方附上该品种当日的调研判断卡（立场、置信、关键事实与引用、失效条件），"
    "回答不要重复卡片里已有的立场、置信和事实条目，直接说你的看法与它的异同、现在怎么做；"
    "判断卡写着生成中或不可用时照常回答，但不要编造判断内容。不给下单指令，用户要下单再另起一轮出卡。\n"
)


def opinion_request(user_text: str) -> bool:
    """A view on one product ("螺纹怎么看"): the Runtime attaches the research judgment card first."""
    text = "".join(user_text.split())
    return bool(text) and len(text) <= 30 and _OPINION_INTENT.search(text) is not None and _OPINION_EXCLUDE.search(text) is None


# 我的流程 (2026-10-06): "以后我说「老规矩」就帮我…… / 把这几步存成流程" defines a routine; "老规矩去掉回测那步"
# changes one (the user's routine named in the sentence with an edit verb). Both are general turns whose
# answer is a 流程卡 made by propose_routine — never the steps run now, never an order route.
_ROUTINE_DEFINE_INTENT = re.compile(
    r"以后(?:我|只要我|我只要)?(?:一)?说[「『“\"][^」』”\"]{1,12}[」』”\"]"
    r"|(?:存成|存为|做成|记成|设成|保存成|保存为)(?:一个|个)?(?:我的)?流程|(?:新建|建|加|添加|建立)(?:一个|个)(?:我的)?流程")
# The unquoted form ("以后我说老规矩，就帮我看判断、画线、出卡") counts only with a list of things to do.
_ROUTINE_DEFINE_LOOSE = re.compile(r"以后(?:我|只要我|我只要)?(?:一)?说[^，,。：:]{1,12}?(?:的时候)?[，,：:]?(?:你)?(?:就|帮我|给我)")
_ROUTINE_LIST = re.compile(r"[，,、；;]")
_ROUTINE_EDIT_VERBS = re.compile(r"去掉|删掉|删除|拿掉|不要|加上|加一步|再加|添上|改成|换成|改为|调换|挪到|放到|顺序|改名|重命名|改一下|改改")
ROUTINE_DEFINE_INSTRUCTION = (
    "本轮是建一个“我的流程”：用 propose_routine 把用户说的步骤按原顺序拆好——每步选一个 kind（read 读今日判断/调研/简报/持仓账户、"
    "draw 画线、backtest 回测、margin 算保证金与可开手数、card 出开仓/平仓卡、alert 到价提醒、watch 盯异动、review 复盘），"
    "text 尽量保留用户原话写一句（≤60 字），不要自己追加“需要就设 / 有必要就设”这类执行承诺：用户说“看看要不要设个提醒”就照写，"
    "这一步运行时只给建议、不会设；用户自己说了“需要就设”才这样写，运行时判断后会设。回复里按同样口径说（只给建议 / 需要时执行），"
    "不要说成会设。name 用用户起的名字（≤12 字）。只出流程卡：用户点“保存”才存，不要说已经存好，"
    "也不要现在就去做这些步骤。用户要求免确认下单、自动下单、改风险设置或自主执行开关时照实写进步骤，流程卡会被拒绝，"
    "照实转告原因，不要替用户改写。超过 10 步时告诉用户最多 10 步、建议拆成两个流程。用户其实不是在建流程时忽略这一段。\n"
)


def routine_define_request(user_text: str) -> bool:
    """"以后我说「老规矩」就帮我…… / 把这几步存成流程": the turn drafts a 流程卡 (propose_routine)."""
    text = "".join(user_text.split())
    if not text:
        return False
    if _ROUTINE_DEFINE_INTENT.search(text) is not None:
        return True
    loose = _ROUTINE_DEFINE_LOOSE.search(text)
    return loose is not None and len(_ROUTINE_LIST.findall(text[loose.end():])) >= 2


def routine_edit_target(user_text: str, routines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """"老规矩去掉回测那步 / 改一下『收盘检查』：加一步设夜盘提醒": the user's routine the sentence changes."""
    text = "".join(user_text.split())
    if not text or _ROUTINE_EDIT_VERBS.search(text) is None:
        return None
    named = [routine for routine in routines if routine["name"] in text]
    return max(named, key=lambda routine: len(routine["name"])) if named else None


def routine_edit_instruction(routine: dict[str, Any], kind_labels: dict[str, str]) -> str:
    steps = "；".join(f"{index}. [{kind_labels.get(step['kind'], step['kind'])}·{step['kind']}] {step['text']}"
                     for index, step in enumerate(routine["steps"], start=1))
    return (f"本轮是改“我的流程”「{routine['name']}」（routineId={routine['id']}），它现在的步骤：{steps}。"
            "按用户说的改完，用 propose_routine（带这个 routineId，写改完后的完整步骤；改名就换 name）出新的流程卡；"
            "只出卡，用户点“保存”才生效，不要现在就去做这些步骤。\n")


# "现在螺纹多少钱 / 棉花最近咋样 / 帮我看看沪金 / 这个怎么样": a plain market read. Decided from the
# words so such a turn takes the narrowed read tools instead of the model's own wandering (the 09-20
# bench: typed "帮我看看沪金" still opened web_search on consecutive runs). 新闻/为什么/观点 stay with the
# model; the subject must be a catalog product, or a bare reference to what is on screen.
_MARKET_QUERY_INTENT = re.compile(
    r"看看|瞅瞅|看下|怎么样|咋样|怎样|多少钱|什么价|啥价|报价|涨还是跌|涨跌|涨了|跌了|涨没|跌没|大不大|强不强")
_MARKET_QUERY_EXCLUDE = re.compile(
    r"新闻|消息|为什么|原因|观点|解读|分析|建议|带我|教我|怎么用|怎么操作|怎么下单|在哪里|哪里|哪个|软件|账号|密码|实盘|真钱|"
    r"买|卖|开仓|平仓|下单|撤单|止损|止盈|策略|回测|颜色|主题|皮肤|字号|字体|界面|面板|设置|打开|关闭|切|复盘|"
    r"然后|再|先|接着|顺便|同时|之后|并且")
_MARKET_QUERY_NOISE = re.compile(
    r"现在|今天|目前|当前|最新|最近|请问|问一下|帮我|给我|看看|瞅瞅|看下|看一下|一下|查一下|查查|查|报一下|报|说一下|说|告诉我|"
    r"有没有|能不能|怎么样|咋样|怎样|如何|多少钱|什么价|啥价|报价|价格|涨还是跌|涨跌|涨了|跌了|涨没|跌没|大不大|强不强|没|量|"
    r"那个|这个|它|是|的|呢|吧|啊|呀|了|吗|嘛|我|那|个|价|多少")
_MARKET_QUERY_NAME = re.compile(r"^[一-龥A-Za-z]{1,6}(?:\d{3,4})?$")


def market_query_request(user_text: str, product_names: list[str] | None = None) -> bool:
    """A plain market question the read tools answer directly, or a bare reference ("这个怎么样").
    Anything else riding along (two products, an opinion word, a UI verb) goes back to the model."""
    text = "".join((user_text or "").split())
    if not text or len(text) > 24 or _MARKET_QUERY_EXCLUDE.search(text) or not _MARKET_QUERY_INTENT.search(text):
        return False
    rest = _MARKET_QUERY_NOISE.sub("", text)
    if not rest:
        return True  # "这个怎么样" / "现在怎么样": the thing on screen
    if not _MARKET_QUERY_NAME.match(rest):
        return False
    if product_names is None:
        return True
    # Case-insensitive: the catalog carries Chinese names and productId codes ("IF"), and the ASR
    # may hand back either case ("if现在多少钱").
    lowered = rest.lower()
    return any(lowered == name.lower() or lowered in name.lower() for name in product_names)


# 持仓/挂单/保护的现状（2026-10-13 夜盘 P1）：说到这些的回合，Runtime 在上下文附上现场读的账户现状，
# 模型讲“还剩什么”只能照它或本轮工具，不照之前对话里说过的。
_ACCOUNT_STATE_WORDS = re.compile(
    r"止损|止盈|保护|挂单|挂着|委托|订单|单子|[那这哪几]张单|那笔|单还在|成交了[吗没么]|撤单|撤了|撤掉|撤销"
    r"|持仓|仓位|多单|空单|空仓|有仓|满仓|还剩|剩下|剩余|还有几手")


def account_state_request(user_text: str) -> bool:
    """Whether a sentence is about the account's positions, working orders or protections."""
    return _ACCOUNT_STATE_WORDS.search("".join(user_text.split())) is not None


# "这个 / 屏幕上 / 现在显示的…": the user points at the interface. Runtime attaches the window's own
# snapshot to that turn (no app_call round); nothing is attached otherwise, and the model is not asked
# to read the screen on its own any more (2026-09-19 round reduction).
_SCREEN_REFERENCE = re.compile(r"(这个|这里|这张|这条|这根|这波|这个数|屏幕上|现在显示|我看到)")
SCREEN_SNAPSHOT_CHARS = 1500
SCREEN_SNAPSHOT_PREFIX = "页面快照（本对话窗口现在显示的内容，系统已附上；不要再用 app_call 读窗口快照，直接按它回答）："


def screen_reference(user_text: str) -> bool:
    """Does the sentence point at something on screen?"""
    text = "".join(user_text.split())
    return bool(text) and _SCREEN_REFERENCE.search(text) is not None


EDITOR_FLOW_CHARS = 400


def editor_flow_line(card: Any) -> str:
    """策略编辑卡第二版 (2026-10-08): the open card's 策略流程 (`state.strategyEditor.flow`) in one bounded line —
    each block's title with the preview's count where the card has one; empty when the card is closed or older."""
    if not isinstance(card, dict) or not card.get("open") or not isinstance(card.get("flow"), dict):
        return ""
    flow = card["flow"]

    def box(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        count, unit = item.get("count"), "笔" if item.get("id") == "action" else "次"
        return f"{item.get('title', '')}{'（待填）' if item.get('pending') else ''}{f' {count} {unit}' if isinstance(count, int) else ''}"

    parts = [box(flow.get("trigger")), " / ".join(box(item) for item in flow.get("entries") or []), box(flow.get("action")),
             " / ".join(box(item) for item in flow.get("exits") or [])]
    basis = f"（{flow['basis']}）" if isinstance(flow.get("basis"), str) else ""
    line = f"strategyEditor={card.get('name', '')} 流程{basis}：" + " → ".join(part for part in parts if part)
    return line[:EDITOR_FLOW_CHARS]


def screen_snapshot_instruction(window: dict, *, focus_only: bool = False) -> str:
    """One bounded line (≤ SCREEN_SNAPSHOT_CHARS) from GET /api/app/ui/windows/{id}: the focus fields
    of `state`, the open dialog, the highlight targets and the visible text. `focus_only` (an opening
    order without a named product) attaches just the focus fields, so the model neither reads the
    window in a round of its own nor guesses the contract."""
    raw_state, raw_observation = window.get("state"), window.get("observation")
    state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
    observation: dict[str, Any] = raw_observation if isinstance(raw_observation, dict) else {}
    focus = "；".join(f"{key}={state[key]}" for key in (
        "view", "symbol", "duration", "panel", "dialog", "settingsOpen", "settingsPage", "remoteSettings",
        "positionsBar", "positionsTab", "fillsSource", "strategyTab", "aiFilter", "aiPage", "replay", "selectedPrice",
        "mobileTab", "mobilePage", "mobileNotifyFilter", "mobileNotifyProduct", "mobileLab", "installEnvironment",
        "pushSupport", "pushSubscribed",
    ) if state.get(key) not in (None, False, ""))
    news = state.get("news")
    if state.get("panel") == "news" and isinstance(news, dict) and news.get("tab"):
        # 调研面板（原新闻面板）开着时带上页签（快讯页另带筛选），“这条快讯”之类的指代才有着落。
        focus += f"；news.tab={news['tab']}" + (f"；news.flashFilter={news.get('flashFilter')}" if news["tab"] == "flash" else "")
    center = state.get("reminderCenter")
    if isinstance(center, dict) and center.get("open"):
        # 提醒中心开着时带上筛选与未读数，“这条提醒 / 未读那几条”才有着落（条目用 GET /api/reminders 读）。
        focus += f"；reminderCenter.filter={center.get('filter')}；reminderCenter.unread={center.get('unread')}"
        if center.get("moreToday"):   # 当天超过一次读出的条数：reminderCenter {more: true} 接着读 (#932 R5)
            focus += "；reminderCenter.moreToday=true"
    debate = state.get("researchDebate")
    if isinstance(debate, dict) and debate.get("open"):
        # 辩论页（2026-10-24 起是右侧栏单独一页，panel=debate）开着时带上它是哪一次、到哪一步、谁在说、结论是什么，
        # “这次辩论 / 空方刚才那句 / 裁判为什么这么判”才有着落（详情用 GET /api/research/debates/{id} 读）。
        focus += f"；researchDebate.id={debate.get('id')}；researchDebate.status={debate.get('status')}" + (
            f"；researchDebate.step={debate['step']}" if debate.get("step") else "") + (
            f"；researchDebate.stance={debate['stance']}" if debate.get("stance") else "")
    preferences = state.get("preferences")
    market = (preferences.get("ui") or {}).get("mobileMarket") if isinstance(preferences, dict) else None
    if state.get("mobileTab") == "market" and isinstance(market, dict) and market.get("view"):
        # 手机看盘 (2026-10-09): 报价 / 多图 / 单图 and what it shows, so “这一行 / 第二格 / 这个副图”有着落。
        view = market["view"]
        focus += f"；mobileMarket.view={view}" + (
            f"；mobileMarket.board={market.get('board')}" if view == "quotes"
            else f"；mobileMarket.layout={market.get('layout')}" if view == "multi"
            else f"；mobileMarket.subPane={market.get('subPane')}" if market.get("subPane") else "")
    editor = editor_flow_line(state.get("strategyEditor"))
    if editor:
        # 策略编辑卡开着时带上它的流程图一行，“这条条件 / 这个止损触发了几次”之类的指代才有着落。
        focus += f"；{editor}"
    if focus_only:
        return SCREEN_FOCUS_PREFIX + (focus or "无") + "\n"
    dialog = observation.get("activeDialog") or (state.get("dialog") if isinstance(state.get("dialog"), str) else None)
    raw_context = observation.get("context")
    context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
    raw_targets = context.get("highlightTargets")
    targets: list[Any] = raw_targets if isinstance(raw_targets, list) else []
    head = SCREEN_SNAPSHOT_PREFIX + (f"焦点 {focus}。" if focus else "") + (f"当前弹窗 {dialog}。" if dialog else "")
    if targets:
        head += "可高亮的界面目标 " + "、".join(str(item) for item in targets[:16]) + "。"
    text = " ".join(str(observation.get("text") or "").split())
    room = SCREEN_SNAPSHOT_CHARS - len(head) - len("可见文字：…\n")
    if text and room > 0:
        head += "可见文字：" + (text if len(text) <= room else text[:room] + "…")
    return head + "\n"


VOICE_FOLLOW_UP_INSTRUCTION = (
    "用户在上一轮回答还没念完时就插话问了这句：优先回答这句；上一轮的结论只在必要时用一句带过，不要重复念上一轮的内容。"
)

_VOICE_FAST_INTENT = re.compile(
    r"(行情|价格|最新价|多少钱|涨跌|涨了|跌了|成交量|持仓量|持仓|仓位|账户|权益|可用资金|盈亏|保证金|基差|仓单|库存|期限结构|持仓排名)"
)
_VOICE_SLOW_INTENT = re.compile(
    r"(怎么看|怎么走|怎么操作|如何|建议|方向|趋势|分析|原因|为什么|新闻|消息|影响|策略|回测|设计|"
    r"买|卖|开仓|平仓|下单|撤单|止损|止盈|设置|打开|关闭|切换|切到|启动|停止|暂停|恢复|急停|"
    # 到价提醒（AI 审计 2026-10-13）：“螺纹价格到3500提醒我”要建提醒，只读快查没有 create_price_alert。
    rf"{ALERT_WORDS})"
)


def voice_fast_query(user_text: str) -> bool:
    """A spoken request that only asks for current facts the read tools answer directly."""
    text = "".join(user_text.split())
    return bool(text) and _VOICE_FAST_INTENT.search(text) is not None and _VOICE_SLOW_INTENT.search(text) is None


_META_QUESTION_INTENT = re.compile(
    r"(什么模型|哪个模型|模型名|模型的名字|用的什么|什么版本|哪个版本|你是谁|你叫什么|"
    r"你能做什么|能干什么|你的能力|你能干嘛|什么软件|哪个软件|什么程序)"
)


_FRESH_MARKET_INTENT = re.compile(
    r"(怎么看|怎么走|怎么操作|如何操作|现在|当前|今天|最新|建议|方向|趋势|行情|"
    r"涨|跌|利多|利空|原因|为什么|消息|新闻|影响|持仓|止盈|止损|开仓|平仓)"
)
_WEB_SEARCH_OPTOUT = re.compile(
    r"(不要联网|不用联网|无需联网|不要搜索|不用搜索|无需搜索|只看技术面|仅看技术面)"
)
_RISK_CONTROL_INTENT = re.compile(
    r"(急停|撤单|(?:撤掉|撤销|取消).{0,24}(?:委托|订单)|"
    r"(?:暂停|停止).{0,24}(?:策略|监控|模拟|交易|执行))"
)
_ADVICE_OR_QUESTION = re.compile(
    r"(怎么|如何|分析|走势|建议|方向|趋势|行情|涨|跌|利多|利空|原因|为什么|"
    r"消息|新闻|影响|止盈|止损|开仓|平仓|应该|是否|要不要|能否|能不能|吗|么|[?？])"
)


def requires_fresh_market_search(user_text: str) -> bool:
    """Classify freshness-sensitive market intent without matching whole questions."""
    text = "".join(user_text.split()).lower()
    if not text or _WEB_SEARCH_OPTOUT.search(text):
        return False
    # Questions about the assistant or app itself carry no market freshness.
    if _META_QUESTION_INTENT.search(text):
        return False
    # Only exempts a pure risk-control request from the news prerequisite. It
    # neither dispatches a tool nor authorizes an action; the model must still
    # interpret the whole request and the execution service retains its checks.
    if _RISK_CONTROL_INTENT.search(text) and not _ADVICE_OR_QUESTION.search(text):
        return False
    return _FRESH_MARKET_INTENT.search(text) is not None


def _freshness_gate_instruction() -> str:
    return (
        "本轮问题可能与当前行情或新闻相关：涉及价格、方向、新闻或市场判断的结论，必须以本轮实际"
        "调用实时行情/账户工具返回的数据或调用 web_search 工具完成的联网搜索为依据，由你自行判断"
        "是否需要以及需要哪种核验：价格、涨跌、成交量、指标、持仓这类实时行情/账户工具能直接回答的，只调工具、不联网；"
        "库存、基差、仓单、持仓排名、期限结构、手续费参考用 get_research_brief 的对应主题；"
        "消息面、新闻、观点、原因、宏观事件用 get_research_brief 的 news，综合分析某品种用 desk；只有核验当前日期、时效性或行情判断新鲜度才 web_search。"
        "若本轮没有调用任何工具，回答会被标注为未经核验后照常显示，"
        "因此缺少可核验的最新信息时明确说明，不要编造日期、价格或新闻，也不要输出搜索状态。\n"
    )


def encode_instruction(user_text: str, context: FuturesWorkspaceContext | None, ui_window_id: str | None = None,
                       origin: str | None = None, follow_up: bool = False, routine_id: str | None = None,
                       pending_card_reply: bool = False) -> str:
    text = user_text.strip()
    if context is None and ui_window_id is None and origin is None and routine_id is None:
        return text
    payload = {
        "userText": text,
        **({"workspaceContext": context.model_dump(mode="json", exclude_none=True)} if context else {}),
        **({"uiWindowId": ui_window_id} if ui_window_id else {}),
        **({"origin": origin} if origin else {}),
        **({"followUp": True} if origin == "voice" and follow_up else {}),
        **({"pendingCardReply": True} if origin == "voice" and pending_card_reply else {}),
        **({"routineId": routine_id} if routine_id else {}),
    }
    return _CONTEXT_PREFIX + json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def decode_instruction(instruction: str) -> DecodedInstruction:
    result = _decode_with_context(instruction)
    if instruction.startswith(_CONTEXT_PREFIX):
        try:
            routine_id = json.loads(instruction[len(_CONTEXT_PREFIX):]).get("routineId")
        except (ValueError, TypeError, AttributeError):
            routine_id = None
        if isinstance(routine_id, str) and routine_id:
            result = replace(result, routine_id=routine_id)
    return result


def _decode_with_context(instruction: str) -> DecodedInstruction:
    from uuid import UUID
    if instruction.startswith(_CONTEXT_PREFIX):
        try:
            raw = json.loads(instruction[len(_CONTEXT_PREFIX):])
        except (ValueError, TypeError):
            return _decode_instruction(instruction)
        result = None
        try:
            window = str(UUID(raw["uiWindowId"]))
            result = _decode_instruction(instruction if "workspaceContext" in raw else raw["userText"])
            result = replace(result, ui_window_id=window,
                model_text=f"本次对话来自窗口 windowId={window}。默认只操作该窗口；其他窗口必须明确指定。\n" + result.model_text)
        except (ValueError, TypeError, KeyError):
            pass
        if raw.get("origin") in {"voice", "ptt"} and isinstance(raw.get("userText"), str):
            if result is None:
                result = _decode_instruction(instruction if "workspaceContext" in raw else raw["userText"])
            fast = voice_fast_query(result.user_text)
            model_text = result.model_text
            if fast and result.requires_web_search:
                # The fast path answers from the read tools: no freshness gate on this turn.
                window_line = f"本次对话来自窗口 windowId={result.ui_window_id}。默认只操作该窗口；其他窗口必须明确指定。\n" if result.ui_window_id else ""
                model_text = window_line + result.user_text
            follow_up = VOICE_FOLLOW_UP_INSTRUCTION if raw.get("followUp") is True else ""
            strategy_signal = STRATEGY_SIGNAL_INSTRUCTION if strategy_signal_request(result.user_text) else ""
            ptt = raw.get("origin") == "ptt"
            card_reply = raw.get("pendingCardReply") is True and not ptt
            card_hint = ("本轮是上一张交易卡的补充；旧卡已失效，不可继续确认旧授权。按补充重新生成方案卡，含义不明先澄清。\n"
                         if card_reply else "")
            return replace(result, origin="voice", push_to_talk=ptt, fast=fast, pending_card_reply=card_reply, requires_web_search=result.requires_web_search and not fast,
                           model_text=(PTT_TURN_INSTRUCTION if ptt else VOICE_TURN_INSTRUCTION) + follow_up + card_hint + strategy_signal + model_text)
        if result is not None:
            return _with_route_hints(result)
        if "workspaceContext" not in raw and isinstance(raw.get("userText"), str):
            # Only a routine id rides with the text (#522): the text is an ordinary typed message.
            return _with_route_hints(_decode_instruction(raw["userText"]))
    return _with_route_hints(_decode_instruction(instruction))


def _with_route_hints(result: DecodedInstruction) -> DecodedInstruction:
    """Typed turns get the same deterministic strategy-signal hint as voice turns. Order words carry no hint any
    more: the typed router reads them with the order-intent parser (意图重构第 3 步)."""
    if strategy_signal_request(result.user_text) and STRATEGY_SIGNAL_INSTRUCTION not in result.model_text:
        result = replace(result, model_text=STRATEGY_SIGNAL_INSTRUCTION + result.model_text)
    return result


def _decode_instruction(instruction: str) -> DecodedInstruction:
    if not instruction.startswith(_CONTEXT_PREFIX):
        text = instruction.strip()
        requires_web_search = requires_fresh_market_search(text)
        return DecodedInstruction(
            user_text=text,
            model_text=(
                f"{_freshness_gate_instruction()}用户问题={text}"
                if requires_web_search else text
            ),
            requires_web_search=requires_web_search,
        )
    try:
        raw = json.loads(instruction[len(_CONTEXT_PREFIX):])
        user_text = str(raw["userText"]).strip()
        context = FuturesWorkspaceContext.model_validate(raw["workspaceContext"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        text = instruction.strip()
        requires_web_search = requires_fresh_market_search(text)
        return DecodedInstruction(
            user_text=text,
            model_text=(
                f"{_freshness_gate_instruction()}用户问题={text}"
                if requires_web_search else text
            ),
            requires_web_search=requires_web_search,
        )
    context_json = json.dumps(
        context.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    focus = context.focus.model_dump() if context.focus is not None else None
    requires_web_search = requires_fresh_market_search(user_text)
    search_gate = (
        _freshness_gate_instruction()
        if requires_web_search else ""
    )
    model_text = (
        "你正在 Sente（AI 私人交易助理）的期货工作区内回答唯一用户。\n"
        "下面的 workspaceContext 是当前页面焦点、订单草稿、本次软件会话内最近操作，以及账户最近"
        "委托成交的有界快照。请根据完整语义理解用户说的‘这个’‘这一手’‘当前持仓’‘现在怎么做’等"
        "自然指代，不要依赖固定问法或关键词表。最近页面操作只表示用户关注和操作意图，不代表委托已经"
        "提交或成交，也不得替代 Runtime 工具对行情时效、账户、持仓、活动委托和风险的重新读取。\n"
        "用户要下单（开仓、平仓、全平、调仓、反手）：本轮直接调用 propose_order 出卡（Runtime 自己读取最新行情、K线、账户、持仓、委托，"
        "算合约月份、开平、今昨与手数并做就绪检查，未就绪时不出卡并返回中文原因），不先调 get_futures_decision_context 或其他读数工具。"
        "只填用户说了的：用户自己说了委托价就填 limitPrice，卡上必须是这个价；没说的不填，不要猜。"
        "对话里（用户这一句引起的回合）AI 一律不直接成交（所有者 2026-10-16）：下单只用 propose_order 出卡等用户确认，"
        "本轮没有直接下单工具；AI 自主执行只用于策略自动下单等 AI 自己发起的操作。设置止损前先用正式工具重新读取最新"
        "Runtime 状态、行情、账户、持仓和活动委托；模拟盘没有平台默认手数或次数上限，策略里用户写的预算仍然生效。"
        "用户说以后都先问他、别让 AI（策略）自己下单时，"
        "用 update_preferences 把 trade.aiAutonomy 设为 false（自动下单的策略改为出卡），不调用停止工具。用户明确停止或急停后，只有用户"
        "再次明确说恢复或继续时才可调用恢复工具；不得从普通分析问题中推断恢复。恢复本身不下单，也不恢复"
        "急停时取消的委托、止损或已暂停策略。数据陈旧或结果 UNKNOWN 时必须失败关闭；UNKNOWN 只查询"
        "原 clientOrderId，绝不重新提交。未经正式历史验证的策略不得启动；用户明确要求启动已经验证"
        "通过的策略时，可以调用正式策略监控工具直接启动模拟监控，不再询问额外授权；用户没提到策略的别的请求里"
        "不得顺带启动（会自动下单的执行方式 Runtime 会拒绝）。不得连接或声称"
        "使用真实账户，liveTrading 始终 disabled。\n"
        "明确的急停、停止执行、暂停策略或撤单指令不需要新闻作为前提；不要为了搜索或行情分析延迟这些"
        "操作。是否执行仍须按完整语义判断，不得把询问、否定、引用或页面快照当作执行授权。\n"
        "以上英文名词和字段只供内部校验。面向用户时必须改写成自然中文，不得原样输出工具名、"
        "字段名、状态码或错误码。对‘现在怎么看’这类简短问题，默认只写一句结论、最多两句关键原因，"
        "再用一句‘现在怎么做’同时说明触发条件和判断失效条件；只有会改变操作的非显然风险才另列风险段，"
        "通常四到七个短句足够，不是强制字数上限。不要重复页面已经显示的模拟环境提示，也不要在用户未要求"
        "交易时复述系统不能代下单。若行情不可用，只写‘结论、为什么、现在怎么做’三个短段且每段一句；"
        "不得承诺等待分钟数，不得解释内部规则，不得要求用户稍后重问。每段只保留会改变判断或操作的内容。"
        "资金占用、保证金、手续费和金额风险只有正式工具给出精确值及依据时才能写，"
        "不得凭经验估算；成交量和持仓量变化也不能被改写成‘资金流入’‘增量资金推动’等确定性资金结论。"
        "get_futures_decision_context 已包含行情、技术摘要、账户、持仓和活动委托，字段"
        "齐全且有效时不得重复调用同类工具。\n"
        "先按语义把问题归类再选工具：①软件用法、功能位置、如何让外部 Agent 连接、设置或排障，"
        "先用 app_capabilities 读取操作手册（GET /api/app/help，可带 ?topic= 只读一个主题）再回答，不要凭猜测描述界面；"
        "给用户解释功能时先用主题的人读层（summary 一句话、steps 怎么用、faq 常见问题）的说法，需要细节再引用 detail 完整说明；"
        "②当前价格、开盘价、均价、成交量、持仓量、账户权益、盈亏、手续费、委托和成交数量等实时事实，"
        "必须调用对应的行情/账户/活动工具或 get_futures_decision_context 取数，不得凭记忆或估算；"
        "③新闻、天气、政策、宏观数据、当天事件等外部信息，先调用 get_research_brief（topic=news）再引用，"
        "只有核验当前日期、时效性或行情判断新鲜度才用 web_search；"
        "④交易常识、指标含义、术语解释等不依赖当前数据的问题，直接回答，不需要联网或读数；"
        "⑤持仓排名（主力席位多空）、仓单、现货价与基差、库存、期限结构（近远月价差）、保证金率或手续费参考，"
        "用 get_research_brief 的对应主题（position_rank、warehouse_receipt、spot_basis、inventory、term_structure、"
        "fees_margin；desk 一次汇总行情、关联品种、基本面、官方数据与报道，事实带 id 且与 AI 判断分开），"
        "引用时说明数据日期 asOf 与来源 source；status 为 stale 要说明是上次成功的旧值，unavailable 只说该项暂缺，"
        "缺失是爬虫源的常态不是故障，不得用记忆补数。"
        "一个问题同时涉及多类时按需组合调用。\n"
        "讲行情、讲走势、教用户看图或指路时，先画后说：先用 get_chart_bars 读用户图上可见的 K 线（time 是秒级时间戳），"
        "再用 annotate_chart 在图上画（图要干净：同一屏最多 6 条）：多空入场、止损、止盈、平仓一律用 point 精确到一个 time+price 的点"
        "（role 为 buy/sell/close/note，颜色随 role），压力/支撑才用 hline 或 zone（同一价位只画一条），趋势用 trendline，"
        "MACD 背离、KD 超买、RSI 超买这类副图内容用 pane 加副图类型（kd/macd/rsi/oi 持仓量）画 point/hline/text，窗口没打开的副图不会显示（回执 notDrawnPanes 会注明，改画到已打开的副图）；每条只带 ≤8 字短标签（如“空 15420”“压力”），"
        "解释文字和时间戳不上图。图上会按 items 顺序显示编号 ①②③，回复正文按同样编号逐条讲（“① 空 15420：……；② 止损 15480：……”），"
        "需要时用 focus 把图滚到那段。被拒并返回 say 时按提示精简后重画。讲完不要清除标注，用户会自己右键清除。"
        "在图上讲解只讲价位和情景（到了哪个价位会怎样、站上或跌破之后看哪里），不下偏多、偏空的方向结论，也不说信心高低。"
        "要让用户注意某个界面位置（下单区、持仓栏、设置等）时，用 highlight_ui 指给用户看，target 取窗口快照 context.highlightTargets 里的 id，"
        "气泡 label 写一句话。画图和高亮都只是观点与指引，不产生任何委托。\n"
        "用户说“带我看看这个软件”“教我用”“怎么下单/怎么切合约/在哪里看持仓”这类想学操作的话，用 start_tour 生成一段分步引导（每步 target + ≤40 字 label + say，"
        "focus 先打开目标所在表面；默认七步课程在 GET /api/app/help 的 tour 主题里，可按问题只取相关几步），正文只写一句“已开始引导，看屏幕上亮起的位置”，"
        "不要把每步内容再写一遍；用户说“下一步/继续/结束”时用 app_call 发 tour: {op: next|stop}。“教我看这个图/讲讲这段走势”则用 annotate_chart 画完后分步讲，"
        "每讲一处配一次 highlight_ui 或对应编号。\n"
        "按问题的完整语义判断意图。只问价格、涨跌、成交量、指标、持仓这类实时工具能答的，只调工具、不联网；"
        "库存、基差、仓单、持仓排名、期限结构、手续费参考用 get_research_brief 对应主题。"
        "涉及当前合约方向、操作建议或当前涨跌原因时，由你自行判断需要"
        "哪种核验：可用 get_research_brief（topic=news）查当前交易日或过去24小时内与品种直接相关的消息面，"
        "或用 web_search 工具联网核验当前日期与时效性"
        "（不要用模型记忆冒充搜索结果），也可依赖本轮正式工具返回的实时行情与账户数据；两者都没有时"
        "明确说明缺少最新信息，不要编造。只有确实改变判断的最新信息才可用一句话写入回答并附来源；"
        "没有合格信息时直接省略新闻内容，不要说搜索过、"
        "没搜到、暂无新闻或任何搜索状态。使用外部信息时，把标题、链接和发布时间集中放在最后一个‘来源’"
        "短段落，方便界面折叠。若当前是模拟造数，现实市场的价格或新闻不能用于印证造数图表；外部合约或"
        "价格与正式工具上下文明显冲突时立即静默弃用，不要花篇幅解释两套数据为什么不一致。\n"
        f"{search_gate}"
        f"{focus_instruction(focus) if focus else ''}"
        f"workspaceContext={context_json}\n"
        f"用户问题={user_text}"
    )
    return DecodedInstruction(
        user_text=user_text,
        model_text=model_text,
        requires_web_search=requires_web_search,
        focus=focus,
    )
