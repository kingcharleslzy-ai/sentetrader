"""下单规划器（docs/ORDER_INTENT_V2.md §2.3）：一个下单意图 + 一次整账户读取 + 用户偏好 → 出卡 / 追问 / 拒绝。

所有出卡入口（交易 AI 的 propose_order、打字与语音前台的快速解析、HTTP/CLI 的三个出卡路由、目标持仓接力）都先把
自己的话或参数构造成 `OrderIntent`（合约用词由 `resolve_words` 按目录解析），再调这里的 `plan`；合约、开平与方向、手数、今昨、价格、开仓就绪门禁、挂单检查只在这里
算一次，各入口不再自己凑参数。真正写卡仍是 `ProposalService.create_trade / create_close_batch`，确认链不变。

统一规则（所有者 2026-10-16 定）：
- 价格：用户说了价（limit）照用；说了方式照用；都没说——平仓（含全平、目标持仓的平仓段）对价，开仓按账号的默认
  价格方式。规则只有 `target_position.leg_price_intent` 一处。“市价” = 对价。
- 今昨：上期所/能源中心平仓没说平今平昨时，按“平今平昨”偏好用 `close_allocation` 分配：只落在一侧就是那一侧，
  跨两侧出一张批量平仓卡（一次确认），可平量不够就拒绝并说清两侧各能平几手。
- 开仓：每张开仓卡都先过就绪门禁（行情、K 线、合约、账户、委托均为最新；日内开仓限额），不论哪个入口。
- 目标持仓：持仓与挂单取自同一份整账户切面；这个合约还有没成交的挂单就不出卡。
- “平一半”向下取整至少 1 手；不带手数的“反手”按原持仓手数，先平后开：一张卡确认一次，平仓全部成交后接力自动发开仓。
- 一句两单：各腿互不影响（不同合约——同品种不同月份也是，或同一合约都是开仓）按顺序一次出全部卡、各自确认；同一合约里有平仓（后一腿的
  仓位取决于前一腿成交）走目标持仓的接力——先出第 1 张，第 1 腿成交后再出下一张；与点卡还是口头确认无关。
- 条件单（“跌到 3000 就买”）不出即时卡；不是下单语气（问句、转述、建议、撤回）不出卡。
"""
from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .data_providers import provider_read_snapshot
from .instruments import FOREIGN_TRADE_EXCHANGE, foreign_named, foreign_product_of, foreign_words
from .order_intent import (
    CARD_ACTS,
    OrderIntent,
    Price,
    from_trade_order,
    position_bucket,
    price_intent_word,
    price_mode,
)
from .order_intent_parse import ParseContext, mentions, side_conflict
from .proposals import ProposalConflict
from .simulation_orders import (
    CLOSE_TODAY_EXCHANGES,
    closable_buckets,
    close_allocation,
    close_yesterday_first_split,
)
from .target_position import (
    leg_price_intent,
    plan_target_position,
    working_order_lots,
    working_orders_line,
)
from .voice_intents import PRODUCT_ALIASES, resolve_symbol

CANDIDATE_READY_SUMMARY = "就绪检查已通过：行情、K线、合约、账户、持仓与委托数据均为最新，委托价在涨跌停范围内"
ORDER_SAFETY_SUMMARY = "下单安全检查已通过：合约在目录中且未到期，报价、交易时段、最小变动价位与涨跌停已核对"
CLOSE_READY_SUMMARY = "就绪检查已通过：账户与持仓为最新，可平手数足够，委托价在涨跌停范围内"
CONDITIONAL_LINE = "到价才下的单不出即时卡；可以先建一个价格提醒，到价后我再出卡让你确认"
TIMED_LINE = "到时间才下的单（开盘后、收盘前、明天…）不出即时卡，到时候再说一遍"
POSITIONS_UNREADABLE_LINE = "持仓数据暂时取不到，这单先不出卡，稍后再试"
SAFETY_BOUNDARY = "实盘功能未开启"
# What the candidate builder (simulation_orders.build_candidate) can refuse after the readiness gate passed,
# in the user's words: the price band, the tick, the position to close, the quote.
CANDIDATE_ISSUE_LABELS = {
    "limit_price_outside_limits": "委托价超出当前涨跌停范围",
    "limit_price_not_on_tick": "委托价不是最小变动价位的整数倍",
    "limit_price_unavailable": "当前没有可用的对手价或挂单价",
    "invalid_limit_price": "委托价无效",
    "upper_limit_missing": "当前没有涨停价数据",
    "lower_limit_missing": "当前没有跌停价数据",
    "quote_stale": "当前行情不是最新状态",
    "quote_not_current": "当前行情不是最新状态",
    "quote_age_missing": "当前行情缺少时间戳",
    "market_session_closed": "当前不在交易时段",
    "account_not_current": "账户数据不是最新状态",
    "close_position_not_found": "没有找到要平的持仓",
    "close_volume_unavailable": "可平手数不足（含冻结手数）",
    "close_bucket_required": "上期所/能源中心平仓必须指明平今或平昨",
    "close_bucket_not_supported": "该交易所不区分平今平昨",
    "close_side_required": "平仓方向无效",
    "invalid_order_volume": "手数无效",
    "invalid_order_side": "买卖方向无效",
    "invalid_price_intent": "价格方式无效",
}
_FRESH = frozenset({"normal", "simulated"})


# -- 输入：整账户切面、偏好、出卡环境 ---------------------------------------------------------------

@dataclass(frozen=True)
class AccountSnapshot:
    """One account cut: positions and order activity from the same combined read
    (`provider_read_snapshot(account=True)`), never the poll cache's positions beside fresh orders.
    `readable=False` = the read failed; the planner then refuses whatever needs the account."""
    positions: tuple[dict[str, Any], ...] = ()
    activity: Mapping[str, Any] | None = None
    readable: bool = True

    @classmethod
    def of(cls, positions: Any, activity: Any) -> AccountSnapshot:
        rows = tuple(row for row in positions or [] if isinstance(row, dict))
        return cls(rows, activity if isinstance(activity, Mapping) else None)

    @classmethod
    def unreadable(cls) -> AccountSnapshot:
        return cls(readable=False)

    def working_orders(self) -> list[Any] | None:
        """The cut's orders when they are a current read (initialized, live, not the cache); None otherwise."""
        activity = self.activity
        if not self.readable or activity is None:
            return None
        orders = activity.get("orders")
        if (not isinstance(orders, list) or activity.get("ordersInitialized") is not True
                or activity.get("isCached") or activity.get("dataStatus") not in _FRESH):
            return None
        return orders

    def position(self, symbol: str, direction: str | None) -> dict[str, Any] | None:
        """The row a close acts on (symbol any case + direction), on any exchange; None when not held."""
        return next((row for row in self.positions
                     if str(row.get("symbol", "")).upper() == symbol.upper() and row.get("direction") == direction),
                    None)


async def read_account_snapshot(provider: Any) -> AccountSnapshot:
    """The one account read an order plan stands on. The combined snapshot covers only these two reads: the
    quotes, bars and catalog the card needs later go through their normal read paths."""
    try:
        async with provider_read_snapshot(provider, refresh=True, account=True):
            positions, activity = await asyncio.to_thread(
                lambda: (list(provider.positions() or []), provider.account_activity()))
    except Exception:
        return AccountSnapshot.unreadable()
    return AccountSnapshot.of(positions, activity)


def read_account_snapshot_now(provider: Any) -> AccountSnapshot:
    """The synchronous read for a caller already off the event loop (the sync tool path)."""
    try:
        positions = list(provider.positions() or [])
    except Exception:
        return AccountSnapshot.unreadable()
    try:
        activity = provider.account_activity()
    except Exception:
        activity = None
    return AccountSnapshot.of(positions, activity)


def needs_account(intent: OrderIntent) -> bool:
    """Whether planning this intent reads the account (positions / orders); an opening does not."""
    return intent.act != "open" or bool(intent.legs)


def side_unsettled(intent: OrderIntent, snapshot: AccountSnapshot) -> bool:
    """“卖一手” with the opposite side held may mean 卖平: the parser's own rule (`side_conflict`) against the account
    an entry read for a bare 买/卖 — an unreadable account is never taken as "nothing held"."""
    return not snapshot.readable or side_conflict(intent, {
        str(row.get("direction")) for row in snapshot.positions if product_of(row.get("symbol")) == product_of(intent.symbol)})


@dataclass(frozen=True)
class Prefs:
    """设置 → 交易：平今平昨（auto/today/historical）与默认价格方式（开仓没说价时用）。"""
    close_preference: str = "auto"
    default_open_price: str = "passive"
    default_volume: int | None = None  # an unsized opening takes it (设置 → 交易 → 默认手数, when set above 1)


def prefs_for(store: Any, user_id: str | None) -> Prefs:
    """The user's trade preferences, read once per plan; the defaults when they cannot be read."""
    if store is None or user_id is None:
        return Prefs()
    try:
        trade = store.user_preferences(user_id).trade
    except Exception:
        return Prefs()
    volume = getattr(trade, "defaultVolume", None)
    # 没说手数：设成非 1 的默认手数按它出卡；仍是出厂的 1 手就照旧追问“开几手？”（与 preference_instruction 同一口径）。
    return Prefs(default_volume=volume if isinstance(volume, int) and volume > 1 else None,
                 close_preference=str(getattr(trade, "closePreference", "auto") or "auto"),
                 default_open_price=str(getattr(trade, "defaultPriceIntent", "passive") or "passive"))


@dataclass(frozen=True)
class PlanContext:
    """Where the cards go and what the planner may read: the account's provider (catalog, readiness reads),
    the proposal service, the conversation and turn, and the opening gate's verified context reader
    (`TraderReadOnlyTools._decision_context` for this account and mode). `target_position` is the 目标持仓
    annotation the cards carry (the relay's next leg); `origin` "ai" marks an AI-proposed card."""
    provider: Any
    proposals: Any
    session_id: str
    turn_id: str
    user_id: str | None = None
    decision_context: Callable[[str], tuple[dict[str, Any] | None, str | None]] | None = None
    origin: str | None = None
    target_position: dict[str, Any] | None = None
    # 开仓门禁的层级（#947）：`decision` = 下单安全检查 + AI 决策就绪（`opening_gate`）——AI 发起的出卡与目标持仓；
    # `order_safety` = 只做下单安全检查（`_order_safety`）——HTTP 单笔出卡路由（下单票与外部调用方分不出谁在调）。
    gate: Literal["decision", "order_safety"] = "decision"
    # 调用方显式指定、已核过授权且可写的账户（HTTP 账户头 / CLI --account）；None = 会话所属账户。
    account_id: str | None = None
    # 本次规划冻结的账户（#932 A6）：`provider` 读的就是它（入口选定账户时绑好的那套工具）。卡与目标持仓计划都落在它上面，
    # 不在规划途中重读会话——会话改绑别的账户不让按 A 的持仓算出的卡漂到 B。显式与否仍只看 `account_id`
    # （accountExplicit）。None = 入口没有多账户装配，照旧跟会话。
    resolved_account_id: str | None = None
    # 一句两单：同一句里前面几腿已出卡的开仓手数（按合约，小写），后一腿的日内开仓限额把它算进去（#950 审查）。
    opened_before: Mapping[str, int] | None = None
    # 意图重构第 3 步：谁从原话读出的这单——"parser"（快速规则有把握）/ "parser_legs"（快速规则从一句多单里逐句读出）/
    # "model"（交易 AI 的 propose_order）/ None（HTTP 与下单票：参数就是用户自己填的）。记在卡上；只有 parser 的卡可以按
    # “语音免口头确认”代确认——一句多单的卡各自由用户确认（所有者 2026-10-16）。
    intent_source: Literal["parser", "parser_legs", "model"] | None = None


# -- 输出：出卡 / 追问 / 拒绝 ---------------------------------------------------------------------

@dataclass(frozen=True)
class Card:
    event: str  # "proposal.trade" | "proposal.close_batch"
    proposal: dict[str, Any]
    # 一句两单：这张卡来自哪一腿（`leg` 是原句里的序号，`source` 是那一腿的意图）。叙述按卡自己的源腿，不按卡在
    # 列表里的位置对齐——前面某腿被拒时后面的卡会前移（#950 审查）。单意图的卡为 None。
    leg: int | None = None
    source: OrderIntent | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Cards:
    cards: tuple[Card, ...]
    target_position: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()  # another leg of the same sentence that made no card, in the user's words
    extra: Mapping[str, Any] | None = None  # e.g. the superseded 目标持仓 plan


@dataclass(frozen=True)
class Ask:
    """Something the words left open that the account cannot settle (two months held, long and short both
    held, no volume): the entry asks (追问卡) or hands the sentence to the trading AI."""
    missing: Literal["dir", "vol", "product", "symbol", "price", "act"]  # the corpus's slot names (tests/intent_corpus)
    question: str
    options: tuple[str, ...] = ()
    # The order as the words vouch for it when the question was asked (PR #1013): quick answers are built from it,
    # never from the model's raw reading (a size no word said must not ride a quick answer).
    basis: OrderIntent | None = None


@dataclass(frozen=True)
class Refuse:
    """No card. `form`: refusal (a fact about the account / contract / price, `issues` in the user's words),
    gate (the opening readiness gate: `reasons` + the context's own `user_facing`), unavailable (a read or
    service failure: only the code), unchanged (目标持仓 already met — not an error). `error` keeps the
    builder's own exception so an HTTP route maps it exactly as before."""
    code: str
    issues: tuple[str, ...] = ()
    form: Literal["refusal", "gate", "unavailable", "unchanged"] = "refusal"
    reasons: tuple[str, ...] = ()
    user_facing: Mapping[str, Any] | None = None
    error: Exception | None = field(default=None, compare=False)
    data: Mapping[str, Any] | None = None

    def facing(self) -> dict[str, Any] | None:
        """The userFacing block every entry shows for this refusal (None for a read/service failure)."""
        if self.form == "unavailable":
            return None
        if self.form == "gate":
            return dict(self.user_facing or {})
        if self.form == "unchanged":
            return {"candidateStatus": "当前已经是目标持仓，不用下单", "candidateIssues": list(self.issues),
                    "safetyBoundary": SAFETY_BOUNDARY}
        facing = {"candidateStatus": "", "candidateIssues": [], "safetyBoundary": SAFETY_BOUNDARY,
                  **dict(self.user_facing or {})}
        facing.update(candidateStatus="暂时不能生成交易方案", candidateIssues=list(self.issues))
        return facing

    def readiness_reasons(self) -> list[str]:
        return list(self.reasons) if self.form == "gate" else [self.code]


Outcome = Cards | Ask | Refuse


_PRICE_WORDS = {"counter": "对手价", "queue": "排队价", "last": "现价"}


def intent_words(intent: OrderIntent, name: Callable[[str], str]) -> str:
    """One order said back in words the fast parser reads (order_intent_parse): a 追问卡's quick answer is the
    user's own order with the missing slot filled, so tapping it goes straight to its card. `name` turns a contract
    code into its catalog name (螺纹钢2701); a product-wide intent drops the month."""
    contract = ""
    if intent.symbol:
        contract = name(intent.symbol)
        if intent.product:
            contract = re.sub(r"\d+$", "", contract)
    side = {"long": "多单", "short": "空单"}.get(str(intent.dir), "")
    lots = f"{intent.vol}手" if intent.vol else ""
    if intent.act == "open":
        words = f"{'买开' if intent.dir == 'long' else '卖开' if intent.dir == 'short' else '开'}{lots}{contract}"
    elif intent.act == "close":
        verb = {"today": "平今", "yesterday": "平昨"}.get(intent.bucket, "平")
        words = f"{verb}{'一半' if intent.frac and not intent.vol else lots}{contract}{side}"
    elif intent.act == "close_all":
        words = f"平掉{contract or '所有'}{side or ('' if contract else '持仓')}"
    elif intent.act == "target":
        net = int(intent.target or 0)
        words = f"{contract}清零" if net == 0 else f"{contract}调到{'多' if net > 0 else '空'}{abs(net)}手"
    else:
        words = f"{contract}{side}反手"
    price = intent.price
    if price.limit is not None:
        words += f"，限价{price.limit:g}"
    elif price.mode in _PRICE_WORDS:
        words += f"，{_PRICE_WORDS[str(price.mode)]}"
    return words


def ask_choices(intent: OrderIntent, ask: Ask, name: Callable[[str], str]) -> list[dict[str, str]]:
    """The 追问卡's quick answers for an Ask on a single intent: 多/空, or the candidate contracts (the months held, the
    window's, the latest cards'), each a whole order (`intent_words`); none for a free answer (几手)."""
    intent = ask.basis or intent
    if intent.legs or ask.missing not in {"dir", "symbol", "product", "act"}:
        return []
    choices: list[dict[str, str]] = []
    for option in ask.options:
        if ask.missing == "act":
            # 持反向仓的“卖 / 买一手X”（#1006）：两个答案各是一整单，合约与手数随答案走，不落回图表合约或默认手数。
            if intent.dir not in {"long", "short"}:
                return []
            held: Literal["long", "short"] = "short" if intent.dir == "long" else "long"
            closing = option.startswith("close:")
            filled = (replace(intent, act="close", dir=held, symbol=option.split(":", 1)[1], product=None, symbols=())
                      if closing else intent)
            closes = [item for item in ask.options if item.startswith("close:")]
            label = ((({"long": "平多", "short": "平空"}[held]) + (f" {name(option.split(':', 1)[1])}" if len(closes) > 1 else ""))
                     if closing else {"long": "开多", "short": "开空"}[intent.dir])
        elif ask.missing == "dir":
            filled = replace(intent, dir=option)  # type: ignore[arg-type]
            label = ({"long": "买开（做多）", "short": "卖开（做空）"} if intent.act == "open"
                     else {"long": "多单", "short": "空单"})[option]
        else:
            filled = replace(intent, symbol=option, product=None, symbols=())
            label = name(option)
        choices.append({"label": label, "prompt": intent_words(filled, name)})
    return choices


def outcome_line(outcome: Outcome) -> str:
    """One Chinese line for a non-card outcome (a refused second leg, a log)."""
    if isinstance(outcome, Ask):
        return outcome.question
    if isinstance(outcome, Refuse):
        facing = outcome.facing() or {}
        return "；".join(str(issue) for issue in facing.get("candidateIssues") or []) or outcome.code
    return ""


# -- 合约目录 -------------------------------------------------------------------------------------

def catalog_item(provider: Any, symbol: str) -> dict[str, Any] | None | bool:
    """The catalog row a contract name means (its symbol, or EXCHANGE.instrumentId / the bare instrumentId,
    any case); False when the catalog is readable, holds items and none matches; None when it cannot say
    (unreadable or empty catalog)."""
    try:
        items = provider.instrument_catalog().get("items") or []
    except Exception:
        return None
    if not items:
        return None
    return next(iter(_exact(symbol, items)), False)


def _exact(word: str, items: list[Any]) -> list[dict[str, Any]]:
    """The catalog rows a contract code names: symbol, instrumentId or EXCHANGE.instrumentId, any case."""
    wanted = word.strip().upper()
    return [item for item in items if isinstance(item, dict) and wanted in {
        str(item.get("symbol") or "").upper(), str(item.get("instrumentId") or "").upper(),
        f"{str(item.get('exchangeId') or '').upper()}.{str(item.get('instrumentId') or '').upper()}"}]


def catalog_names(items: list[Any]) -> dict[str, str]:
    """symbol (lower case) → catalog instrumentName, for lines the user reads (rows without a name left out)."""
    return {str(item.get("symbol") or "").lower(): str(item.get("instrumentName"))
            for item in items if isinstance(item, dict) and item.get("instrumentName")}


def catalog_symbol(provider: Any, symbol: str) -> str:
    """The catalog's own spelling of a contract (`SHFE.RB2701` → `SHFE.rb2701`), the one name every card, plan
    and quote read uses (10-13 production: an upper-cased name broke the paper engine's market subscription).
    The name as given when the catalog does not know it — the opening check refuses that honestly, and a
    close finds its position row case-insensitively."""
    item = catalog_item(provider, symbol)
    return str(item.get("symbol") or symbol) if isinstance(item, dict) else symbol.strip()


def instrument_display(provider: Any, symbol: str) -> str:
    """`螺纹钢2610（SHFE.rb2610）` for a line the user reads; the bare code without a Chinese name."""
    item = catalog_item(provider, symbol)
    name = str(item.get("instrumentName") or "").strip() if isinstance(item, dict) else ""
    return f"{name}（{symbol}）" if name else symbol


def product_of(symbol: Any) -> str:
    """`SHFE.rb2701` → `rb`: the product a month-less name means. A foreign month (`CME.GCEZ26`, 外盘二期) → its
    MT5 root `gce`, the same for every month of it (the month letter is not part of the product)."""
    foreign = foreign_product_of(str(symbol or ""))
    if foreign is not None and foreign.mt5 and str(symbol).upper().startswith(f"{FOREIGN_TRADE_EXCHANGE}."):
        return foreign.mt5.lower()
    return re.sub(r"\d+$", "", str(symbol or "").split(".")[-1]).lower()


# The instrument catalog could not be read (instrument_catalog raised, or it is empty): one refusal for the words that
# need it and the opening that needs its row (#947 review, #932).
CATALOG_UNREADABLE = Refuse("instrument_catalog_unavailable", ("合约目录暂时读不到，这单先不出卡，稍后再试",))


def _catalog_match(word: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The catalog row a contract word means: an exact code (symbol, instrumentId or EXCHANGE.instrumentId, any
    case), else the one row the instruments route's search and `resolve_symbol` pick (the name with its month, or
    the product's main contract) — the same reading the voice front and the search tool use."""
    wanted = word.strip()
    if not wanted:
        return None
    exact = _exact(wanted, items)
    if len(exact) == 1:
        return exact[0]
    # A spoken alias (白银 → 沪银, 黄金 → 沪金) reads as its catalog name, month kept, before any name search when this
    # catalog names it: the trading AI writes the user's word (#932：本站账户“卖一手白银”交模型后“没找到合约”), and a
    # search for the word itself finds the foreign main contracts the catalog also lists (美白银主连 / 美黄金主连,
    # #932 F4). A catalog without it (the USD sub-account's: COMEX黄金2612) goes on to the name search.
    spoken = re.match(r"(.*?)(\d*)$", wanted)
    if spoken is not None and spoken.group(1) in PRODUCT_ALIASES:
        aliased = _catalog_match(PRODUCT_ALIASES[spoken.group(1)] + spoken.group(2), items)
        if aliased is not None:
            return aliased
    # 外盘展示名 (#1218 回归修复): the parser folds an alias to the product's display name (美黄金 → COMEX黄金), but the
    # RMB catalog names the main line 美黄金主连 — a word that is exactly one tradable foreign product's name (or alias)
    # reads as its 快期外盘主连 row when this catalog has it; plan_order then moves the order to the USD sub-account.
    foreign = foreign_named(wanted)
    if foreign is not None and foreign.kqd and wanted.upper() in {word.upper() for word in foreign_words(foreign)}:
        main_line = _exact(foreign.kqd, items)
        if len(main_line) == 1:
            return main_line[0]
    needle = wanted.lower()
    found = [item for item in items if needle in " ".join(
        str(item.get(key, "")) for key in ("symbol", "instrumentId", "instrumentName", "productId")).lower()]
    return resolve_symbol(wanted, found)


def _names_month(word: str, item: dict[str, Any]) -> bool:
    """Whether the words named one month (a code, or a name with its month) rather than the product."""
    return word.strip()[-1:].isdigit() or word.strip().upper() in {
        str(item.get("symbol") or "").upper(), str(item.get("instrumentId") or "").upper()}


def resolve_words(intent: OrderIntent, items: list[dict[str, Any]], chart: str | None = None) -> OrderIntent | Refuse:
    """An intent's contract words as the planner takes them (ORDER_INTENT_V2 §2.3 合约（目录解析）): the parser's
    catalog name / spoken month, the trading AI's code or product word, or — nothing said — the window's `chart`.
    A named month (or a code) is that contract only; a product word (or the chart, for anything but an opening) is
    whichever month the account holds, the main contract standing in (`product` set, settled by `resolve_scope`).
    A 全平 naming nothing stays every position; its `symbols` resolve one by one. Unknown words are refused, never
    guessed. Pure: `items` is the instrument catalog."""
    if intent.legs:
        legs: list[OrderIntent] = []
        for leg in intent.legs:
            resolved = resolve_words(leg, items, chart)
            if isinstance(resolved, Refuse):
                return resolved
            legs.append(resolved)
        return replace(intent, legs=tuple(legs))
    if intent.act not in CARD_ACTS:
        return intent
    if not items:
        # No catalog to read the words against: said honestly, never the words passed on as said and then refused as
        # an unknown contract (#932 F4: “卖一手白银” → 没找到叫“白银”的合约). Nothing to read (a 全平 naming nothing)
        # needs no catalog.
        word = intent.symbol or intent.product or bool(intent.symbols) or (None if intent.act == "close_all" else chart)
        return CATALOG_UNREADABLE if word else intent

    def missing(word: str) -> Refuse:
        return Refuse("instrument_not_found", (f"没找到叫“{word}”的合约，请说品种名或代码",))

    if intent.symbols:
        named: list[str] = []
        for word in intent.symbols:
            item = _catalog_match(word, items)
            if item is None:
                return missing(word)
            # A product word in a 全平 list is every month of it held (`resolve_scope` expands the bare code).
            named.append(str(item["symbol"]) if _names_month(word, item) else product_of(item["symbol"]))
        return replace(intent, symbols=tuple(dict.fromkeys(named)), product=None, symbol=None)
    spoken = intent.symbol or intent.product
    if spoken is None and intent.act == "close_all":
        return intent  # 全平 / 空单都平了: every position (one side), no contract to name
    word = spoken or chart
    if not word:
        return replace(intent, product=None, symbol=None)  # the planner asks which contract
    item = _catalog_match(word, items)
    if item is None:
        return missing(word)
    symbol = str(item["symbol"])
    month = spoken is not None and _names_month(spoken, item)
    return replace(intent, symbol=symbol, product=None if month or intent.act == "open" else product_of(symbol))


def ground(intent: OrderIntent, said: str, *, products: tuple[str, ...], chart: str | None,
           positions: Any, cards: Any = (), items: Any = ()) -> OrderIntent | Ask:
    """意图重构第 3 步，propose_order's one validation: the trading AI's intent keeps only the slots the user's words —
    or the context: the window's contract, the account's one position, the conversation's latest cards — vouch for.

    Its boundary (PR #997, main agent 2026-10-19): it does two things only — a card never carries a contract, size or
    modifier the user did not say (the model makes nothing up), and a close is never narrower than what the user named.
    Grammar and which of the things said a modifier or size belongs to are the model's call; the user's confirmation of
    the card is the last check. It attributes no clause or phrase; a question where a card could have gone is low priority.

    The contract: the words are read once into the candidates they leave (`_said_contracts`: codes, a month after a
    product name, product names; “这个” as the window's contract; when nothing is named, the waiting card an explicit
    correction changes, then the account's one position, and for an opening the window's contract — owner 2026-10-19) — one order needs exactly one (two contracts and one order is
    #932 A11's question), and the model's must be it; a 全平's contracts must be the named ones, all of them. The size
    (`_said_lots`): one the words say. The verb said (two kinds of action → asked), a 全平 naming positions (not an
    interface part, not a pick by profit or loss — owner 2026-10-19), a target's lots and side, each protection's kind
    and price, a price way or limit. An unsaid side or half is dropped (the planner asks); a 平今 / 平昨 or a price way the
    words say once and the model left out is put back. `cards`: the waiting card an explicit correction of it
    (“改成三手”) changes — the correction keeps the card's action and every slot it does not state; `items`: the
    instrument catalog the model's codes are read against; `positions` None: the account could not be read."""
    context = ParseContext(products=products)
    words = mentions(said, context)
    if words.cond is not None and not intent.cond:
        # “跌到 3000 就开多一手螺纹，止损 2900” read without its condition: still a conditional order — the planner
        # makes no card now (到价的建议先建价格提醒).
        return replace(intent, cond=words.cond, stance="conditional")
    catalog = [item for item in items or [] if isinstance(item, dict)]
    rows = [row for row in positions or [] if isinstance(row, dict) and row.get("direction") in {"long", "short"}]
    seen = [card for card in cards or [] if isinstance(card, dict) and card.get("symbol")]
    held = list(dict.fromkeys(str(row.get("symbol")) for row in rows))
    options = tuple(dict.fromkeys([*([chart] if chart else []), *held, *(str(card["symbol"]) for card in seen)]))
    verbs = _VERBS.get(intent.act, set())
    if len({"close" if act == "shut" else act for act in words.acts} & {"open", "close", "target", "reverse"}) > 1:
        # 一句一单（#932 A11）：“卖一手螺纹平掉”——卖开还是卖平，是用户的，追问。
        return Ask("act", "这句话里有开仓也有平仓（或调仓、反手），要做的是哪一个？")
    moves = {(move.act, move.dir) for move in (_card_move(card) for card in seen) if move is not None}
    if len({act for act, _ in moves}) == 1 and (act := next(iter(moves))[0]) != intent.act and not words.acts & verbs:
        # 改单只改说了的槽位（#932）：动作沿用待确认卡的——“改成三手”不能把开仓卡换成平仓卡。没说多空就用卡的方向。
        directions = {direction for _, direction in moves}
        said_side = words.opening if act == "open" else words.held
        intent = replace(intent, act=act, target=None, symbols=(),
                         dir=intent.dir if said_side else (next(iter(directions)) if len(directions) == 1 else None))
        verbs = _VERBS[act]
    if intent.bucket in {"today", "yesterday"} and intent.bucket not in words.buckets:
        intent = replace(intent, bucket="auto")  # 平今 / 平昨 the words never said: the account's preference decides
    if intent.act in {"close", "close_all"} and intent.bucket == "auto" and len(words.buckets) == 1:
        intent = replace(intent, bucket=next(iter(words.buckets)))  # type: ignore[arg-type]  # 原话说的那一侧，不落偏好
    named = _said_contracts(words, context, catalog)
    if words.filtered and (intent.act == "close_all" or (intent.act == "close" and not named)):
        # “平掉盈利的单子”“把亏的都平了”：按盈亏挑持仓意图里没有这个槽位，模型读成全平会多平（#932 A12，所有者
        # 2026-10-19）：问清平哪些，不出卡。前一分句说“亏钱了”只是原因（Mentions.filtered 只看平仓那一分句）。
        return Ask("product", "要平掉哪些持仓？按盈亏挑的请直接说合约", tuple(held))
    pointed = [_Named(symbol=chart)] if words.pointing and chart else []
    if intent.act == "close_all":
        if not words.position or words.surface:
            return Ask("product", "要平掉哪些持仓？", tuple(held))
        # A product scope (`product` set) is the product, whatever month stands in as its `symbol`.
        model_words = [*intent.symbols, *([intent.product] if intent.product else [intent.symbol] if intent.symbol else [])]
        allowed = list(dict.fromkeys([*named, *pointed]))  # each scope named is its own: nothing folds here
        # The model's scope is the words' scope: every contract it names is one they name, and every one they name is
        # in it (“把螺纹和沪金都平了” read as 螺纹 alone closes less than said, PR #997 review) — else asked.
        # A named month is that month only: a bare product word in the model's scope would close every month held
        # (“把 rb2701 和 rb2705 都平了” read as 螺纹, PR #997 review) — it never stands for an exact candidate here.
        # A product named on its own is covered only by a product-wide close, a month only by that month.
        def covers(candidate: _Named, word: str) -> bool:
            item = _catalog_match(word, catalog) if catalog else None
            monthed = (item is not None and _names_month(word, item)) or bool(re.search(r"\d", word.split(".")[-1]))
            return candidate.matches(word, context, catalog, exact=True) if candidate.symbol is not None else (
                not monthed and candidate.matches(word, context, catalog))
        if (words.pointing and not chart) or (allowed and not model_words) or not all(
                any(covers(candidate, word) for candidate in allowed) for word in model_words) or not all(
                any(covers(candidate, word) for word in model_words) for candidate in allowed):
            return Ask("product", "要平掉哪个品种？", tuple(held))
    else:
        word = intent.symbol or intent.product
        on_side = [row for row in rows if intent.dir in (None, row.get("direction"))]
        only = str(on_side[0].get("symbol")) if len({str(row.get("symbol")) for row in on_side}) == 1 else None
        if named or words.pointing:
            # The words name the contract (or point at the window's): exactly one candidate, and the model's is it.
            found = _one_contract([*named, *pointed]) if not (words.pointing and not chart) else None
            cards_held = {str(card["symbol"]) for card in seen}
            if found is not None and found.symbol is None and len(cards_held) == 1 and (
                    product_of(next(iter(cards_held))) == found.product):
                found = _Named(symbol=next(iter(cards_held)))  # “螺纹改成三手” corrects the card's own month
            if found is None or (word and not found.matches(word, context, catalog)):
                return Ask("product", "说的是哪个合约？", options)
            if found.symbol is not None:
                intent = replace(intent, symbol=found.symbol, product=None)
            elif not word:
                intent = replace(intent, symbol=f"{found.name or found.product}{found.month or ''}")
        elif len({str(card["symbol"]) for card in seen}) == 1:
            # 改单没点名合约（#932 A16）：就是待确认卡的合约本身——模型给的若是别的月份（rb2705 对 rb2701 的卡），追问。
            card = _Named(symbol=str(seen[0]["symbol"]))
            if word and not card.matches(word, context, catalog, exact=True):
                return Ask("product", "说的是哪个合约？", options)
            intent = replace(intent, symbol=card.symbol, product=None)
        elif intent.act != "open" and only:
            intent = replace(intent, symbol=only, product=None)  # nothing named: the account's one position is it
        elif intent.act == "open" and chart and not _OTHER_CONTRACT.search(said):
            # 开仓没说合约：就是图表上的合约（所有者 2026-10-19：“下多单”不追问），卡上写明合约，确认卡是最后一关；
            # 模型给的若是别的合约（它自己编的），追问；“换个合约 / 其他合约”说的正是不要图表这个，也追问（PR #1004 审查）。
            if word and not _Named(symbol=chart).matches(word, context, catalog):
                return Ask("product", "说的是哪个合约？", options)
            intent = replace(intent, symbol=chart, product=None)
        else:
            return Ask("product", "说的是哪个合约？", options)
    bare_open = intent.act == "open" and not words.acts and bool(words.opening and words.counts)  # 空两手棉花
    if not (words.acts & verbs or bare_open or seen):
        return Ask("act", "是开仓、平仓、调到目标持仓还是反手？")  # “螺纹多单平一手” read as an opening
    # A correction's own side or size is the words' (“不对，是卖”“改成三手”); the card's only when it states none.
    sides = words.opening if intent.act == "open" else words.held
    if intent.dir and intent.dir not in sides and not (not sides and (intent.act, intent.dir) in moves):
        intent = replace(intent, dir=None)  # opening: the planner asks 开多还是开空; closing: the held side
    lots = _said_lots(intent, words, seen)
    if isinstance(lots, Ask):
        return lots
    intent = lots
    if intent.frac and not words.half:
        intent = replace(intent, frac=None)
    price = intent.price
    priced = bool(words.modes or words.price_word or (price.limit is not None and price.limit in words.prices))
    if not (seen and not priced and price in {_card_price(card) for card in seen}):  # a correction keeps the card's
        limit_said = price.limit is not None and price.limit in words.prices
        if len(words.modes) > 1 or ((words.price_word or price.limit is not None) and not limit_said):
            # 对手价还是挂单价 / 限价没说价 / 涨停价 / a limit no word states (“螺纹2701” is a month): the user's to say
            return Ask("price", "按什么价下？")
        if words.modes and (words.price_word or price.limit is not None):
            return Ask("price", "说了限价又说了价格方式，按哪个下？")  # “限价3000按对手价开多”（#932）
        mode: str | None = price.mode if price.mode in words.modes else None
        if mode is None and len(words.modes) == 1 and price.limit is None:
            mode = next(iter(words.modes))  # 原话唯一的价格方式，模型漏填：照原话，不落账号默认
        intent = replace(intent, price=Price(mode=mode, limit=price.limit))  # type: ignore[arg-type]
    net = int(intent.target or 0)
    if intent.act == "target" and not (
            (net == 0 and words.zero)
            or (abs(net) in words.counts and ("long" if net > 0 else "short") in words.opening | words.held)):
        return Ask("vol", "调到多少手、多还是空？")  # “调到三手多单” filled as -3: the side is the user's
    if not _protection_said(intent, words, _costs(intent, rows, catalog)):
        return Ask("price", "止损止盈价是多少？")
    side_ask = _bare_side_ask(intent, words, None if positions is None else rows, seen, [*named, *pointed], context,
                              catalog)
    return side_ask if side_ask is not None else intent


def _bare_side_ask(intent: OrderIntent, words: Any, rows: list[dict[str, Any]] | None, seen: list[dict[str, Any]],
                   named: list[_Named], context: ParseContext, catalog: list[dict[str, Any]]) -> Ask | None:
    """持反向仓说“卖 / 买一手X”（#1006，所有者 2026-10-19）：平还是开是用户的，问清再出卡——不默认平，也不默认开.
    Runs last, on an intent whose every slot the words already vouch for (PR #1013 review). The holdings it asks
    about: the month the words name exactly, else every month of the product (the model may give the main month while
    another is held). A correction of a waiting card keeps that card's action; with a stop / take-profit riding the
    order the answer is free text (a quick answer cannot carry it); otherwise one quick answer per held month to close,
    and the order as read to open — each a whole order."""
    if (intent.act != "open" or not words.bare_side or intent.dir not in {"long", "short"} or seen
            or intent.cond or intent.stance == "conditional"):
        return None  # a conditional order makes no card now: nothing to ask, and no quick answer may drop its condition
    opposite = "short" if intent.dir == "long" else "long"
    question = "平多还是开空？" if opposite == "long" else "平空还是开多？"
    if rows is None:
        return Ask("act", question)  # 持仓读不到（#932 T037）：不当作没持仓，照样问
    word = intent.symbol or intent.product
    if not word:
        return None
    exact = next((candidate.symbol for candidate in named if candidate.symbol), None)
    item = _catalog_match(word, catalog) if catalog else None
    product = product_of(str(item["symbol"])) if item is not None else None

    def held(row: dict[str, Any]) -> bool:
        symbol = str(row.get("symbol"))
        if row.get("direction") != opposite:
            return False
        if exact is not None:
            return symbol.upper() == exact.upper()
        return product_of(symbol) == product if product is not None else _Named(symbol=symbol).matches(word, context, catalog)

    months = list(dict.fromkeys(str(row.get("symbol")) for row in rows if held(row)))
    if not months:
        return None
    if intent.protection is not None or intent.remaining_protection is not None:
        return Ask("act", question)
    return Ask("act", question, (*(f"close:{month}" for month in months), "open"), basis=intent)


@dataclass(frozen=True)
class _Named:
    """One contract the words leave as a candidate: an exact contract (`symbol`, the catalog's spelling: a code, a
    month after a product name, the window's for “这个”, the waiting card's), or a month-less product (`product` code,
    `name` the catalog name said) — or, when the catalog cannot place a spoken month, `name` + `month`."""
    symbol: str | None = None
    product: str | None = None
    name: str | None = None
    month: str | None = None

    def matches(self, word: str, context: ParseContext, catalog: list[dict[str, Any]], *, exact: bool = False) -> bool:
        """The model's contract word is this candidate: the very contract for an exact one (a product word stands for
        it only when it is that contract's product and `exact` is off); the same product for a product; the same
        product and month for a spoken month the catalog cannot place."""
        item = _catalog_match(word, catalog) if catalog else None
        spelled = str(item["symbol"]) if item is not None else word.strip()
        monthed = (_names_month(word, item) if item is not None else False) or bool(re.search(r"\d", word.split(".")[-1]))
        if self.symbol is not None:
            if spelled.split(".")[-1].casefold() == self.symbol.split(".")[-1].casefold():
                return True
            return not exact and not monthed and product_of(spelled) == product_of(self.symbol)
        names = set(mentions(word, context).products) | (
            set(mentions(str(item.get("instrumentName") or ""), context).products) if item is not None else set())
        if self.month is not None:
            digits = re.search(r"\d+$", str(item.get("instrumentId") or "")) if item is not None else None
            own = digits.group() if digits and monthed else next(iter({m for _, m in mentions(word, context).months}), None)
            return self.name in names and own is not None and (
                own == self.month or (len(own) != len(self.month) and own[-3:] == self.month[-3:]))
        return (self.product is not None and item is not None and product_of(spelled) == self.product) or (
            self.name is not None and self.name in names)


# Words that turn away from the window's contract (“换个合约下一手多单”“下一手其他合约多单”): never the chart fallback.
_OTHER_CONTRACT = re.compile(r"换(个|一个|一下)?(合约|品种|月份|月)|(别的|其他|其它|另一个|另外一个)(合约|品种|月份|月)")


def _said_contracts(words: Any, context: ParseContext, catalog: list[dict[str, Any]]) -> list[_Named]:
    """Every contract the words name, once: codes (rb2701) and months after a product name (螺纹2705) as exact
    contracts, product names said on their own as products — a product named beside a code of it is its own, wider
    scope (“把 rb2701 和螺纹其他月份都平了”, PR #997 review), never folded into the code."""
    named: list[_Named] = []
    for code in sorted(words.codes):
        item = _catalog_match(code, catalog) if catalog else None
        named.append(_Named(symbol=str(item["symbol"]) if item is not None else code))
    for name, month in sorted(words.months):
        item = _catalog_match(f"{name}{month}", catalog) if catalog else None
        named.append(_Named(symbol=str(item["symbol"])) if item is not None and _names_month(f"{name}{month}", item)
                     else _Named(name=name, month=month))
    for name in sorted(words.bare_products):
        item = _catalog_match(name, catalog) if catalog else None
        named.append(_Named(product=product_of(item["symbol"]) if item is not None else None, name=name))
    return list(dict.fromkeys(named))


def named_contract(text: str, items: Any) -> str | None:
    """The one catalog contract a sentence names — a code or a month after a product name exactly, a product alone
    as its main contract — read as the order card reads it; None when it names none, several, or one the catalog
    lacks (the 策略编辑卡 then keeps its own contract, 2026-11-02)."""
    from .turn_router import catalog_product_names
    catalog = [item for item in items or [] if isinstance(item, dict)]
    context = ParseContext(products=tuple(catalog_product_names(catalog) or ()))
    one = _one_contract(_said_contracts(mentions(text, context), context, catalog))
    if one is None:
        return None
    rows = _exact(one.symbol, catalog) if one.symbol else (
        [row] if one.name and not one.month and (row := _catalog_match(one.name, catalog)) is not None else [])
    return str(rows[0]["symbol"]) if len(rows) == 1 else None


def _distinct(candidates: list[_Named]) -> list[_Named]:
    """The distinct contracts the candidates are: a product an exact one (“这个”, a code) already is folds into it."""
    exact = {candidate.symbol.casefold(): candidate for candidate in candidates if candidate.symbol}
    loose = [candidate for candidate in candidates if not candidate.symbol and not (
        candidate.product and any(product_of(symbol) == candidate.product for symbol in exact))]
    return [*exact.values(), *dict.fromkeys(loose)]


def _one_contract(candidates: list[_Named]) -> _Named | None:
    """The one contract the words leave, or None when they leave several (“rb2701挂一手au2612多单”“豆二挂一手沪金多单”,
    #932 A11)."""
    left = _distinct(candidates)
    return left[0] if len(left) == 1 else None


def _said_lots(intent: OrderIntent, words: Any, seen: list[dict[str, Any]]) -> OrderIntent | Ask:
    """The size (#932 A15): the model's when the words say it (which of two said sizes is the model's call, the card the
    user's check); else the one size the words say (“改成三手” is 3, never the card's 5 nor the default size); several
    or one that cannot be read (“两百五手”) → asked; none said → the model's only when the words still vouch for it
    (全部平掉; a correction keeping the card's size), else none (the planner asks, or takes the default size)."""
    if intent.act not in {"open", "close", "reverse"} or (intent.frac and not intent.vol):
        return intent
    if intent.vol and intent.vol in words.counts:
        return intent
    if len(words.counts) == 1:
        return replace(intent, vol=next(iter(words.counts)))
    if words.lots_said:
        return Ask("vol", "几手？")
    card_lots = {int(card.get("volume") or 0) for card in seen}
    if intent.vol and ((intent.act == "close" and words.whole) or intent.vol in card_lots):
        return intent
    return replace(intent, vol=None)


_VERBS: dict[str, set[str]] = {"open": {"open"}, "close": {"close", "shut"}, "close_all": {"close", "shut"},
                               "target": {"target"}, "reverse": {"reverse"}}


def _card_move(card: Mapping[str, Any]) -> OrderIntent | None:
    """A waiting card's order (act + side) as an intent; None for a side the cards never carry."""
    try:
        return from_trade_order(symbol=str(card.get("symbol")), side=str(card.get("side")), volume=1)
    except KeyError:
        return None


def _card_price(card: Mapping[str, Any]) -> Price:
    """The price a waiting card was made with, as the intent states it: its way, or the user's own limit (a limit card
    is priced `last` with that limit, `_price`)."""
    word, limit = card.get("priceIntent"), card.get("limitPrice")
    if word == "last" and isinstance(limit, (int, float)) and not isinstance(limit, bool):
        return Price(limit=float(limit))
    return Price(mode=price_mode(str(word)) if word else None)


def _costs(intent: OrderIntent, rows: list[dict[str, Any]], catalog: list[dict[str, Any]]) -> set[float]:
    """“挪到成本”: the cost of the position the intent acts on — its open average on the tick grid (多单向上、空单向下,
    card_protection.cost_on_tick, the same as move_stops_to_cost); empty when it cannot be read. A contract named with
    its month is that contract's cost only — another month of the product has its own (#932)."""
    from decimal import Decimal, InvalidOperation

    from .card_protection import cost_on_tick
    item = _catalog_match(intent.symbol, catalog) if intent.symbol and catalog else None
    exact = str(item["symbol"]).casefold() if item is not None and _names_month(str(intent.symbol), item) else None
    product = product_of(item["symbol"] if item is not None else intent.symbol) if intent.symbol else None
    costs: set[float] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if (exact is not None and symbol.casefold() != exact) or (product is not None and product_of(symbol) != product):
            continue
        if intent.dir is not None and row.get("direction") != intent.dir:
            continue
        held = _catalog_match(symbol, catalog) if catalog else None
        try:
            tick = Decimal(str((held or {}).get("priceTick")))
        except (InvalidOperation, ValueError):
            continue
        cost = cost_on_tick(row.get("openPrice"), tick, long=row.get("direction") == "long")
        if cost is not None:
            costs.add(float(cost))
    return costs


def _protection_said(intent: OrderIntent, words: Any, costs: set[float] | frozenset[float] = frozenset()) -> bool:
    """A stop or take-profit riding the card (protection on an opening, remaining_protection on a close) is one the
    words state, each by its own words (#932: never another kind's number, never a 撤 said of the other one): its kind
    (止损 / 止盈) and the price said with it, or 成本 said with it for a stop moved to the position's tick-rounded cost
    (`costs`, the value must equal it), or 撤 said with it for one taken away."""
    kinds = {"stopLossPrice": words.stop, "takeProfitPrice": words.take, "stopLoss": words.stop, "takeProfit": words.take}
    for key, value in {**(intent.protection or {}), **(intent.remaining_protection or {})}.items():
        guard = kinds.get(key)
        if guard is None or not guard.said:
            return False
        if value is None:
            if not guard.drop:
                return False
        elif float(value) not in guard.prices and not (guard.cost and float(value) in costs):
            return False
    return True


def close_legs(positions: Any, *, symbol: str, direction: str, volume: int,
               preference: str) -> list[tuple[str, int]]:
    """(positionBucket, lots) for closing `volume` of one contract's long or short side, the split every
    card path shares: SHFE/INE by `close_allocation` (the 平今平昨 preference over the closable — frozen
    deducted — volumes), elsewhere one ``all`` order of what is closable. What is not closable is left out;
    an empty list = nothing to close. 策略出卡、停止并平仓、策略自动执行、价差两腿（出卡、执行、开仓回退）和回放收尾
    都用它（意图重构第 4 步删掉了不扣冻结、不看偏好的 strategy_monitor._close_plans）。"""
    rows = [row for row in positions or [] if isinstance(row, dict)
            and str(row.get("symbol") or "").casefold() == symbol.casefold() and row.get("direction") == direction]
    if not rows or volume <= 0:
        return []
    keys = ("volume", "todayVolume", "historicalVolume", "frozenToday", "frozenHistorical")
    position = rows[0] if len(rows) == 1 else {
        key: sum(int(row.get(key) or 0) for row in rows) for key in keys if any(key in row for row in rows)}
    if symbol.split(".", 1)[0].upper() in CLOSE_TODAY_EXCHANGES:
        parts = close_allocation(position, volume=volume, bucket=None, preference=preference)
        return [(bucket, parts[bucket]) for bucket in ("today", "historical") if parts[bucket] > 0]
    split = close_yesterday_first_split(position, volume=volume)
    closable = split["today"] + split["historical"]
    return [("all", closable)] if closable > 0 else []


# -- 规划 -----------------------------------------------------------------------------------------

def plan(intent: OrderIntent, snapshot: AccountSnapshot | None, prefs: Prefs, ctx: PlanContext) -> Outcome:
    """The cards one intent becomes (created through the proposal service, each still awaiting the user's
    confirmation), or what to ask, or why not — never an order."""
    snapshot = snapshot if snapshot is not None else AccountSnapshot.unreadable()
    if intent.cond or intent.stance == "conditional":
        # 条件单不出即时卡（所有者 2026-10-16）：到价的提议建价格提醒，到时间的到时候再说。
        if str((intent.cond or {}).get("type")) == "price":
            return Refuse("conditional_order", (CONDITIONAL_LINE,), data={"suggest": "price_alert"})
        return Refuse("conditional_order", (TIMED_LINE,))
    if intent.stance != "order":
        # 问句、转述、建议、整句收回、否定：都不出卡（照常回答由入口决定）。
        return Refuse("not_an_order", ("这句话不是下单指令，没有出卡",))
    if intent.legs:
        return _plan_legs(intent.legs, snapshot, prefs, ctx)
    if intent.act not in CARD_ACTS:
        return Refuse("not_a_card_act", ("这句话不是开仓、平仓或调仓，没有出卡",))
    if intent.act == "open":
        return _open(intent, prefs, ctx)
    scoped = resolve_scope(intent, snapshot)
    if not isinstance(scoped, OrderIntent):
        return scoped
    if scoped.act == "close":
        return _close(scoped, snapshot, prefs, ctx)
    if scoped.act == "close_all":
        if scoped.bucket != "auto":
            return _close_bucket(scoped, snapshot, prefs, ctx)
        return _close_all(scoped, ctx)
    if scoped.act == "reverse":
        reversed_ = _reverse_as_target(scoped, snapshot, ctx)
        if not isinstance(reversed_, OrderIntent):
            return reversed_
        scoped = reversed_
    return _target(scoped, snapshot, prefs, ctx)


def resolve_scope(intent: OrderIntent, snapshot: AccountSnapshot) -> OrderIntent | Ask | Refuse:
    """Which contract(s) and which side a close / 全平 / 目标持仓 / 反手 acts on, from the account cut: a
    month-less product takes the month held (two months held → ask); a close without a side takes the side
    held (long and short both held → ask). An intent already naming them comes back unchanged."""
    if intent.act not in {"close", "close_all", "target", "reverse"}:
        return intent
    products = [name for name in intent.symbols if not any(char.isdigit() for char in name)]
    # 今仓 / 昨仓全平 reads the positions too: an unreadable account is “读不到”, never “没有今仓可平” (#932).
    needs_rows = intent.act != "close_all" or intent.product is not None or bool(products) or intent.bucket != "auto"
    if needs_rows and not snapshot.readable:
        if intent.act in {"target", "reverse"}:
            return ORDERS_UNREADABLE
        return Refuse("account_positions_unreadable", (POSITIONS_UNREADABLE_LINE,))
    if products:
        # 全平 named by product (“把螺纹和沪金都平了”, resolve_words): every month of each held, on the side said. A
        # list that names only products nobody holds is refused — never widened to every position.
        wanted = {name.lower() for name in products}
        months = sorted({str(row.get("symbol")) for row in snapshot.positions
                         if intent.dir in (None, row.get("direction")) and product_of(row.get("symbol")) in wanted})
        listed = [name for name in intent.symbols if name not in products] + months
        if not listed:
            return Refuse("close_position_not_found", ("没有可平的持仓",))
        intent = replace(intent, symbols=tuple(dict.fromkeys(listed)))
    if intent.product is not None:
        product = intent.product.lower()
        held = [row for row in snapshot.positions if product_of(row.get("symbol")) == product
                and row.get("direction") in {"long", "short"}]
        if intent.act == "close_all":
            named = sorted({str(row.get("symbol")) for row in held if intent.dir in (None, row.get("direction"))})
            return replace(intent, product=None, symbols=tuple(named or ([intent.symbol] if intent.symbol else [])))
        contracts = sorted({str(row.get("symbol")) for row in held
                            if intent.act != "close" or intent.dir in (None, row.get("direction"))})
        if len(contracts) > 1:
            return Ask("symbol", f"持有{'、'.join(contracts)}几个月份，说的是哪一个？", tuple(contracts))
        intent = replace(intent, product=None, symbol=contracts[0] if contracts else intent.symbol)
    if intent.act == "close_all":
        # A named contract is that contract only; nothing named is every position.
        return replace(intent, symbols=(intent.symbol,)) if intent.symbol and not intent.symbols else intent
    if not intent.symbol:
        return Ask("product", "说的是哪个合约？")
    if intent.act in {"close", "reverse"} and intent.dir is None:
        directions = {str(row.get("direction")) for row in snapshot.positions
                      if str(row.get("symbol", "")).casefold() == intent.symbol.casefold()
                      and row.get("direction") in {"long", "short"}}
        if len(directions) > 1:
            return BOTH_SIDES_ASK
        if directions:
            intent = replace(intent, dir=directions.pop())  # type: ignore[arg-type]
    return intent


BOTH_SIDES_ASK = Ask("dir", "多单和空单都有，说的是哪一边？", ("long", "short"))
ORDERS_UNREADABLE = Refuse("target_position_orders_unreadable",
                           ("暂时读不到这个账户的委托，没法确认有没有没成交的挂单；稍后再调目标持仓",))
ONE_BY_ONE = "这句话里有几单，请一单一单说"
LEGS_TANGLED_LINE = "这几单牵涉同一个合约（或有不限范围的全平）又各有平仓、调仓或平今平昨，不好按顺序出卡：请一单一单说"


def _plan_legs(legs: tuple[OrderIntent, ...], snapshot: AccountSnapshot, prefs: Prefs,
               ctx: PlanContext) -> Outcome:
    """一句两单（所有者 2026-10-16：按顺序出卡，各自确认；不看确认方式）。Every leg's open slots and scope are settled
    first, so no card is made for a sentence that then has to be asked about. Legs that touch different contracts —
    or one contract only by opening — are independent: all their cards now, in order, each confirmed on its own.
    Legs where one contract is closed and touched again depend on the earlier fill: the 目标持仓 relay carries them
    (the first card now, the next one from order_watch once that leg has filled)."""
    if ctx.intent_source == "parser":
        ctx = replace(ctx, intent_source="parser_legs")  # each card the user's own 确认, 免口头确认 or not
    scoped: list[OrderIntent] = []
    for leg in legs:
        if leg.legs or leg.act not in CARD_ACTS:
            return Refuse("not_a_card_act", ("一句话里有一段不是开仓、平仓或调仓，没有出卡",))
        if leg.cond or leg.stance != "order":
            return plan(leg, snapshot, prefs, ctx)
        if leg.act == "open" and leg.bare and side_unsettled(leg, snapshot):
            # “…，卖一手沪金” with 沪金 long held: 开空还是平多？ — never guessed, and never asked mid-sentence.
            return Refuse("legs_unsettled", (f"有一单只说了买/卖，和持仓方向相反，开新仓还是平仓说不清；{ONE_BY_ONE}",))
        settled = _open_missing(leg) if leg.act == "open" else resolve_scope(leg, snapshot)
        if isinstance(settled, Ask):
            # A question about one leg (两个月份、多空都有) cannot be answered for the sentence: one order at a time.
            return Refuse("legs_unsettled", (f"{settled.question}{ONE_BY_ONE}",))
        if isinstance(settled, Refuse):
            return settled
        scoped.append(settled if isinstance(settled, OrderIntent) else leg)
    if any(leg.act == "close_all" and not leg.symbols for leg in scoped):
        # 不限范围的全平和别的单同句（“全部平仓，然后买一手螺纹”）：既不能缩成只平一个合约，也不好接力——请一单一单说。
        return Refuse("legs_entangled", (LEGS_TANGLED_LINE,))
    touched = [_touched(leg) for leg in scoped]
    entangled = any(touched[i] & touched[j] and not (scoped[i].act == scoped[j].act == "open")
                    for i in range(len(scoped)) for j in range(i + 1, len(scoped)))
    if entangled:
        return _relay_legs(tuple(scoped), snapshot, prefs, ctx)
    cards: list[Card] = []
    misses: list[Ask | Refuse] = []
    opened: dict[str, int] = {}  # 同一合约前面几腿已出卡的开仓手数：共用的日内开仓限额按合计核（#950 审查）
    for index, leg in enumerate(scoped):
        outcome = plan(leg, snapshot, prefs, replace(ctx, opened_before=dict(opened)))
        if isinstance(outcome, Cards):
            cards.extend(replace(card, leg=index, source=leg) for card in outcome.cards)
            if leg.act == "open" and leg.symbol:
                key = catalog_symbol(ctx.provider, leg.symbol).casefold()
                opened[key] = opened.get(key, 0) + int(leg.vol or 0)
        else:
            misses.append(outcome)
    if not cards:
        return misses[0]
    return Cards(tuple(cards), notes=tuple(outcome_line(miss) for miss in misses))


def _open_missing(intent: OrderIntent) -> OrderIntent | Ask:
    if intent.dir is None:
        return Ask("dir", "开多还是开空？", ("long", "short"))
    if not intent.symbol:
        return Ask("product", "开哪个合约？")
    if not intent.vol:
        return Ask("vol", "开几手？")
    return intent


def _touched(intent: OrderIntent) -> set[str]:
    """The contracts a leg acts on, as resolved (an unscoped 全平 never reaches here: refused with any other leg). Two
    months of one product are two books: “平 rb2701，开 rb2705” is two independent cards (#932)."""
    symbols = intent.symbols if intent.act == "close_all" else (intent.symbol,)
    return {str(symbol).split(".")[-1].casefold() for symbol in symbols}


def _relay_legs(legs: tuple[OrderIntent, ...], snapshot: AccountSnapshot, prefs: Prefs,
                ctx: PlanContext) -> Outcome:
    """Dependent legs on one contract (“平掉螺纹再买一手螺纹”): each leg's side and lots from the position the legs
    before it leave (the relay only moves on once a leg has filled completely), stored as a 目标持仓 plan so
    order_watch issues the next card on the fill — the same relay as a reverse."""
    symbols = {symbol for leg in legs for symbol in (leg.symbols if leg.act == "close_all" else (leg.symbol,))}
    if (len(symbols) != 1 or any(leg.act not in {"open", "close", "close_all"} or leg.bucket != "auto"
                                 for leg in legs)):
        return Refuse("legs_entangled", (LEGS_TANGLED_LINE,))
    symbol = catalog_symbol(ctx.provider, str(symbols.pop()))
    unsettled = _orders_unsettled(snapshot, symbol, ctx)
    if unsettled is not None:
        return unsettled
    held = {direction: sum(int(row.get("volume") or 0) for row in snapshot.positions
                           if str(row.get("symbol", "")).casefold() == symbol.casefold()
                           and row.get("direction") == direction) for direction in ("long", "short")}
    start = dict(held)
    steps: list[dict[str, Any]] = []
    firsts: list[OrderIntent] = []
    for leg in legs:
        if leg.act == "open":
            direction, volume = str(leg.dir), int(leg.vol or 0)
            held[direction] += volume
        else:
            sides = [name for name, lots in held.items() if lots > 0]
            direction = str(leg.dir) if leg.dir else (sides[0] if len(sides) == 1 else "")
            if not direction and sides:
                return BOTH_SIDES_ASK
            have = held.get(direction, 0)
            if have <= 0:
                return _not_held(ctx, symbol)
            volume = have if leg.act == "close_all" else int(leg.vol or 0) or max(1, math.floor(have * float(leg.frac or 0)))
            if volume > have:
                return Refuse("close_volume_exceeds_position", (
                    f"{instrument_display(ctx.provider, symbol)}到这一步只有 {have} 手可平，平不了 {volume} 手",))
            held[direction] -= volume
        order = replace(leg, act="open" if leg.act == "open" else "close", symbol=symbol, symbols=(), product=None,
                        dir=direction, vol=volume, frac=None)  # type: ignore[arg-type]
        step: dict[str, Any] = {"action": order.act, "side": order.side, "volume": volume}
        if leg.price.stated:
            step["priceIntent"] = price_intent_word(leg.price.mode) or "last"
            if leg.price.limit is not None:
                step["limitPrice"] = leg.price.limit
        steps.append(step)
        firsts.append(order)
    words = {"buy_open": "买开", "sell_open": "卖开", "buy_close": "买平", "sell_close": "卖平"}
    summary = f"{instrument_display(ctx.provider, symbol)}一句{len(steps)}单：" + "，成交后".join(
        f"{words[step['side']]} {step['volume']} 手" for step in steps)
    payload = {"symbol": symbol, "currentNet": start["long"] - start["short"], "targetNet": held["long"] - held["short"],
               "longVolume": start["long"], "shortVolume": start["short"], "legs": steps, "summary": summary,
               "crossesZero": False, "unchanged": False, "priceIntent": None, "sentence": True}
    first = firsts[0]
    return _start_relay(payload, symbol=symbol, reason=summary, ctx=ctx, first=lambda leg_ctx: (
        _open(first, prefs, leg_ctx) if first.act == "open" else _close(first, snapshot, prefs, leg_ctx)))


def _price(price: Price, side: str, prefs: Prefs) -> tuple[str, float | None]:
    """(priceIntent, limitPrice) for one order: the user's own price as given (a card carrying it is recorded
    against the stated mode, else "last"); a stated mode; else the one default rule (`leg_price_intent`):
    a close takes the 对价, an opening the account's default."""
    stated = price_intent_word(price.mode)
    if stated is not None:
        return stated, price.limit
    if price.limit is not None:
        return "last", price.limit
    return leg_price_intent({}, side, default_open=lambda: prefs.default_open_price), None


def _open(intent: OrderIntent, prefs: Prefs, ctx: PlanContext) -> Outcome:
    if intent.vol is None and prefs.default_volume:
        intent = replace(intent, vol=prefs.default_volume)  # 没说手数：账号的默认手数（与原先交易 AI 的做法一致）
    missing = _open_missing(intent)
    if isinstance(missing, Ask):
        return missing
    assert intent.symbol is not None and intent.vol is not None
    symbol = catalog_symbol(ctx.provider, intent.symbol)
    item = catalog_item(ctx.provider, symbol)
    if item is None:
        # The catalog cannot say (unreadable or empty): fail closed — no opening card on a contract nobody confirmed
        # (#947 review, #932).
        return CATALOG_UNREADABLE
    if item is False:
        # A contract the data side never confirmed (a hallucinated or mistyped code) must not become a card
        # built on invented quotes; the refusal says what to say instead.
        return Refuse("instrument_not_found", (f"没找到叫“{symbol}”的合约，请说品种名或代码",))
    gate = opening_gate(ctx, symbol, intent.vol)
    if isinstance(gate, Refuse):
        return gate
    readiness, facing = gate
    return _trade_card(ctx, intent, symbol=symbol, side=str(intent.side), volume=intent.vol, bucket=None,
                       readiness=readiness, facing=facing, prefs=prefs)


def opening_gate(
    ctx: PlanContext, symbol: str, volume: int,
) -> tuple[dict[str, Any] | None, Mapping[str, Any] | None] | Refuse:
    """开仓就绪门禁（唯一实现）：the verified decision context must say proposalReady (market, bars, contract,
    account, positions, orders fresh; risk normal), and the exchange's intraday open cap must leave `volume`.
    Returns the card's readiness block and the context's userFacing, or the refusal."""
    if ctx.gate == "order_safety":
        return _order_safety(ctx, symbol)
    if ctx.decision_context is None:
        return Refuse("invalid_or_unavailable_decision_context", form="unavailable")
    context, error = ctx.decision_context(symbol)
    if context is None:
        return Refuse(str(error), form="unavailable")
    facing = context["userFacing"]
    if not context["proposalReady"]:
        return Refuse("proposal_gate_not_ready", form="gate",
                      reasons=tuple(context["proposalReadinessReasons"]), user_facing=facing)
    open_limit = context.get("openLimit") or {}
    before = int((ctx.opened_before or {}).get(symbol.casefold(), 0))
    if int(open_limit.get("limit") or 0) > 0 and volume + before > int(open_limit.get("remaining") or 0):
        remaining = int(open_limit.get("remaining") or 0)
        return Refuse("open_limit_exceeded", (
            f"{instrument_display(ctx.provider, symbol)}今日还能开 {remaining} 手"
            f"（日内开仓限额 {open_limit['limit']} 手，已开 {open_limit.get('openedToday') or 0} 手"
            + (f"，这句话前面已出卡 {before} 手" if before else "") + "）",))
    readiness = {"passed": True, "summary": CANDIDATE_READY_SUMMARY, "issues": [], "source": context["source"]}
    if str((context.get("market") or {}).get("tradingStatus") or "") == "AUCTIONORDERING":
        # A notice, not a refusal: the order is accepted into the call auction and matches when the session opens.
        readiness = {**readiness, "summary": CANDIDATE_READY_SUMMARY + "；集合竞价中，开盘撮合后才有成交"}
    return readiness, facing


def _order_safety(ctx: PlanContext, symbol: str) -> tuple[dict[str, Any], None] | Refuse:
    """下单安全检查（所有入口的开仓都有）：合约在目录里（不在已由 `_open` 拒绝）且没到期；报价新鲜、交易时段、
    最小变动价位、涨跌停与账户最新由 build_candidate 在写卡时核对，不过同样不出卡。到期日读不到（历史回放的导出）
    不当作到期。"""
    item = catalog_item(ctx.provider, symbol)
    try:
        expired = isinstance(item, dict) and int(item.get("expireRestDays")) <= 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        expired = False
    if expired:
        return Refuse("instrument_expired", (f"{instrument_display(ctx.provider, symbol)}已到期，不能开仓",))
    return {"passed": True, "summary": ORDER_SAFETY_SUMMARY, "issues": [], "source": "runtime_order_safety_v1"}, None


def _close(intent: OrderIntent, snapshot: AccountSnapshot, prefs: Prefs, ctx: PlanContext) -> Outcome:
    symbol = catalog_symbol(ctx.provider, str(intent.symbol))
    # 先说有没有持仓：任何交易所都先按合约+方向找一遍，没有就诚实收尾——SHFE/INE 缺今昨时 builder 会先报
    # close_bucket_required，把“没有持仓”藏在“必须指明平今平昨”后面。
    position = snapshot.position(symbol, intent.dir)
    if position is None:
        return _not_held(ctx, symbol)
    volume = intent.vol
    if intent.frac is not None and volume is None:
        volume = max(1, math.floor(int(position.get("volume") or 0) * float(intent.frac)))  # 平一半：向下取整，至少 1 手
    if not volume:
        return Ask("vol", "平几手？")
    bucket = position_bucket(intent.bucket)
    shfe = symbol.split(".", 1)[0].upper() in CLOSE_TODAY_EXCHANGES
    if shfe and bucket is not None:
        # 说了平今/平昨而那一侧根本没有可平的仓（扣冻结）：直说没有这一侧；有但不够仍由 builder 按
        # close_volume_unavailable 拒绝（原口径）。
        if closable_buckets(position)[0 if bucket == "today" else 1] <= 0:
            side_word = "今仓" if bucket == "today" else "昨仓"
            return Refuse("close_bucket_empty",
                          (f"当前没有{instrument_display(ctx.provider, symbol)}的{side_word}可平",))
    elif shfe:
        # SHFE/INE 没说平今平昨：与估算同一份分配（#480）——偏好 × 可平量（扣冻结）。一侧 → 那一侧；
        # 两侧 → 一张批量平仓卡；不够 → 拒绝并说两侧各能平几手。
        today_available, historical_available = closable_buckets(position)
        parts = close_allocation(position, volume=volume, bucket=None, preference=prefs.close_preference)
        if parts["today"] + parts["historical"] != volume:
            return Refuse("close_volume_exceeds_position", (
                f"当前没有足够的{instrument_display(ctx.provider, symbol)}持仓可平 {volume} 手"
                f"（今仓可平 {today_available} 手、昨仓可平 {historical_available} 手），平不了",))
        if parts["today"] and parts["historical"]:
            return _close_batch(ctx, intent, symbols=[symbol], max_volume=volume,
                                remaining_protection=intent.remaining_protection)
        bucket = "today" if parts["today"] else "historical"
    readiness = {"passed": True, "summary": CLOSE_READY_SUMMARY, "issues": [], "source": "runtime_close_checks_v1"}
    return _trade_card(ctx, intent, symbol=symbol, side=str(intent.side), volume=volume, bucket=bucket,
                       readiness=readiness, facing=None, prefs=prefs)


def _close_batch(ctx: PlanContext, intent: OrderIntent, **scope: Any) -> Outcome:
    """One close_batch card (one confirmation, per-order report-back) of `intent`'s side over `scope` (symbols, and
    for a close spanning 今仓+昨仓 its volume and remaining protection, #941), priced by the stated mode or the 对价.
    A failure of the builder's own reads (the second positions read of a 跨今昨 close, #947 review) is a refusal, never
    an exception out of the planner (an HTTP 500)."""
    try:
        proposal = ctx.proposals.create_close_batch(
            ctx.session_id, ctx.turn_id, direction=intent.dir, reason=intent.reason, target_position=ctx.target_position,
            price_intent=price_intent_word(intent.price.mode) or "aggressive", limit_price=intent.price.limit,
            account_id=ctx.account_id or ctx.resolved_account_id, intent_source=ctx.intent_source, **scope)
    except ProposalConflict as error:
        return _batch_refusal(ctx, error)
    except (KeyError, RuntimeError, ValueError) as error:
        return Refuse("invalid_close_batch_proposal", form="unavailable", error=error)
    return Cards((Card("proposal.close_batch", proposal),), target_position=ctx.target_position)


def _close_all(intent: OrderIntent, ctx: PlanContext) -> Outcome:
    return _close_batch(ctx, intent, symbols=list(intent.symbols) or None)


def _close_bucket(intent: OrderIntent, snapshot: AccountSnapshot, prefs: Prefs, ctx: PlanContext) -> Outcome:
    """“把螺纹今仓全平”：the named side's whole closable volume of each position in scope, one card each (most
    often one); nothing closable on that side is said."""
    side_word = "今仓" if intent.bucket == "today" else "昨仓"
    wanted = {symbol.casefold() for symbol in intent.symbols}
    cards: list[Card] = []
    first_miss: Ask | Refuse | None = None
    for row in snapshot.positions:
        symbol = str(row.get("symbol") or "")
        if (wanted and symbol.casefold() not in wanted) or intent.dir not in (None, row.get("direction")):
            continue
        if symbol.split(".", 1)[0].upper() not in CLOSE_TODAY_EXCHANGES:
            continue  # 不分今昨的交易所没有“今仓全平”
        available = closable_buckets(row)[0 if intent.bucket == "today" else 1]
        if available <= 0:
            continue
        outcome = _close(replace(intent, act="close", symbol=symbol, symbols=(), dir=row.get("direction"),
                                 vol=available), snapshot, prefs, ctx)
        if isinstance(outcome, Cards):
            cards.extend(outcome.cards)
        else:
            first_miss = first_miss or outcome
    if cards:
        return Cards(tuple(cards), target_position=ctx.target_position)
    if first_miss is not None:
        return first_miss
    named = "、".join(instrument_display(ctx.provider, symbol) for symbol in intent.symbols) or "账户里"
    return Refuse("close_bucket_empty", (f"{named}没有{side_word}可平",))


def _reverse_as_target(intent: OrderIntent, snapshot: AccountSnapshot, ctx: PlanContext) -> OrderIntent | Refuse:
    """反手 = 目标持仓到反方向：不带手数按原持仓手数（先平后开，确认一次）。"""
    symbol = catalog_symbol(ctx.provider, str(intent.symbol))
    held = sum(int(row.get("volume") or 0) for row in snapshot.positions
               if str(row.get("symbol", "")).casefold() == symbol.casefold() and row.get("direction") == intent.dir)
    if intent.dir is None or held <= 0:
        return Refuse("reverse_position_not_found",
                      (f"当前没有{instrument_display(ctx.provider, symbol)}的持仓，没法反手",))
    volume = intent.vol or held
    return replace(intent, act="target", symbol=symbol, target=-volume if intent.dir == "long" else volume, vol=None)


def _orders_unsettled(snapshot: AccountSnapshot, symbol: str, ctx: PlanContext) -> Refuse | None:
    """A stepwise plan's first check (#942): its lots count filled positions only, so a working order on the contract
    (its fill would overshoot or fall short) or orders that cannot be read (stale, cached, not initialized) stop it."""
    orders = snapshot.working_orders()
    if orders is None:
        return ORDERS_UNREADABLE
    working = working_order_lots(orders, symbol)
    if working:
        return Refuse("target_position_orders_working",
                      (working_orders_line(instrument_display(ctx.provider, symbol), working),))
    return None


def _target(intent: OrderIntent, snapshot: AccountSnapshot, prefs: Prefs, ctx: PlanContext) -> Outcome:
    if ctx.user_id is None or intent.target is None:
        return Refuse("proposal_service_unavailable", form="unavailable")
    symbol = catalog_symbol(ctx.provider, str(intent.symbol))
    unsettled = _orders_unsettled(snapshot, symbol, ctx)
    if unsettled is not None:
        return unsettled
    stated = price_intent_word(intent.price.mode)
    plan_ = plan_target_position(list(snapshot.positions), symbol, intent.target, price_intent=stated)
    if plan_.unchanged:
        return Refuse("target_position_unchanged", (plan_.summary,), form="unchanged",
                      data={"targetPosition": plan_.payload()})
    reason = f"目标持仓：{plan_.summary}"
    first = plan_.legs[0]

    def first_card(leg_ctx: PlanContext) -> Outcome:
        if first.side == "close_all":
            # Flatten / reverse first leg: the batch close of this contract's every position (全平's card and flow).
            # Priced by the plan's stated mode, else the 对价 (`_close_all`'s default — the same rule).
            outcome = _close_all(OrderIntent(act="close_all", symbols=(plan_.symbol,), reason=reason,
                                             price=intent.price), leg_ctx)
            if isinstance(outcome, Refuse) and outcome.code == "invalid_close_batch_proposal":
                outcome = replace(outcome, code="invalid_target_position_proposal")
            return outcome
        leg = replace(from_trade_order(symbol=plan_.symbol, side=first.side, volume=first.volume, reason=reason),
                      price=intent.price)
        return _open(leg, prefs, leg_ctx) if leg.act == "open" else _close(leg, snapshot, prefs, leg_ctx)
    payload = plan_.payload()
    if intent.price.limit is not None:
        # The user's own price (“调到三手多单，限价 3100”) rides every step, the relay's later legs too (order_watch).
        payload["legs"] = [{**leg, "limitPrice": intent.price.limit} for leg in payload.get("legs") or []]
    return _start_relay(payload, symbol=plan_.symbol, reason=reason, ctx=ctx, first=first_card)


def _start_relay(payload: dict[str, Any], *, symbol: str, reason: str, ctx: PlanContext,
                 first: Callable[[PlanContext], Outcome]) -> Outcome:
    """A stepwise plan (目标持仓, 反手, 一句两单 on one contract): stored for the relay (order_watch issues each next
    leg once the leg before it filled — confirmed on the first card's confirmation for 目标持仓 / 反手, as its own
    card for 一句两单), its first card made now carrying the plan annotation. A plan whose
    first card cannot be made dies at once — the refusal is the answer."""
    if ctx.user_id is None:
        return Refuse("proposal_service_unavailable", form="unavailable")
    store = ctx.proposals.store
    try:
        session_account = str(store.get_session(ctx.session_id)["tradingAccountId"] or "") or None
    except KeyError:
        session_account = None
    trading_account_id = (ctx.account_id or ctx.resolved_account_id or session_account
                          or store.default_trading_account_id)
    # accountExplicit: the plan is bound to its own account — the one the caller named, or (#932 A6 review) the one
    # this plan was frozen to while the conversation had already moved to another: its next leg goes to that
    # account (each card still confirmed by the user), and the conversation being elsewhere is no "account switched".
    bound = bool(ctx.account_id) or bool(ctx.resolved_account_id and session_account
                                         and session_account != ctx.resolved_account_id)
    plan_row, superseded = store.create_target_plan(
        user_id=ctx.user_id, trading_account_id=trading_account_id, symbol=symbol,
        target_net=int(payload["targetNet"]),
        leg_json={"plan": payload, "sessionId": ctx.session_id, "turnId": ctx.turn_id, "reason": reason,
                  **({"accountExplicit": True} if bound else {}),
                  # 反手确认一次 (#1140): the plan records the approval contract its first card promised, so a plan
                  # made before it (whose card promised its own second card) is never confirmed by the relay.
                  **({"confirmOnce": True} if len(payload.get("legs") or []) > 1 and not payload.get("sentence") else {})})
    # The 目标持仓 block the first card carries: the plan, which step of how many, and the relay's promise.
    total = len(payload.get("legs") or [])
    annotation = {**payload, "planId": plan_row["id"], "step": 1, "totalSteps": total}
    if total > 1:
        # 反手确认一次 (2026-10-24): this confirmation also covers a 目标持仓 / 反手 plan's second leg; a 一句多单
        # plan's next legs are still cards of their own.
        annotation["note"] = (f"这是目标持仓的第 1 步 / 共 {total} 步；这段成交后我再给你第 2 步的确认卡。"
                              if payload.get("sentence") else
                              f"确认一次，共 {total} 步：这段全部成交后自动发出第 2 步，不再出确认卡；没有全部成交就不会开仓。")
    extra = ({"supersededPlan": {"note": f"已作废之前 {symbol} 的目标持仓计划，按最新目标执行",
                                 "targetNet": superseded["target_net"]}}
             if superseded is not None else None)
    if superseded is not None and superseded.get("current_proposal_id"):
        # #518: 旧计划还挂着的确认卡随计划一起作废——10 分钟窗口内点它不再发单。
        try:
            ctx.proposals.supersede_stale_card(str(superseded["current_proposal_id"]), user_id=ctx.user_id)
        except Exception:
            pass  # 作废失败不挡新计划；旧卡自身仍有状态与确认校验兜底
    outcome = first(replace(ctx, target_position=annotation))
    if not isinstance(outcome, Cards):
        # The first leg was refused (no position, stale quote, open limit…): the plan dies before it ever had
        # a card; the refusal is the answer.
        store.update_target_plan(plan_row["id"], status="abandoned")
        return outcome
    store.update_target_plan(plan_row["id"], current_proposal_id=outcome.cards[0].proposal["proposalId"])
    return Cards(outcome.cards, target_position=annotation, extra=extra)


def _trade_card(ctx: PlanContext, intent: OrderIntent, *, symbol: str, side: str, volume: int,
                bucket: str | None, readiness: dict[str, Any] | None, facing: Mapping[str, Any] | None,
                prefs: Prefs) -> Outcome:
    price_intent, limit_price = _price(intent.price, side, prefs)
    try:
        proposal = ctx.proposals.create_trade(
            ctx.session_id, ctx.turn_id, symbol=symbol, side=side, volume=volume,
            price_intent=price_intent, limit_price=limit_price, position_bucket=bucket,
            reason=intent.reason, readiness=readiness, target_position=ctx.target_position,
            origin=ctx.origin, protection=intent.protection, remaining_protection=intent.remaining_protection,
            account_id=ctx.account_id or ctx.resolved_account_id, intent_source=ctx.intent_source)
    except (KeyError, ProposalConflict, RuntimeError, ValueError) as failure:
        code = getattr(failure, "code", None) or (str(failure.args[0]) if failure.args else "invalid_trade_proposal")
        if code == "close_position_not_found":
            label: str | None = _not_held(ctx, symbol).issues[0]
        else:
            # A refusal carrying its own Chinese sentence (protection prices, a strategy card guard) says that;
            # the fixed table covers the candidate builder's codes.
            label = getattr(failure, "message", None) or CANDIDATE_ISSUE_LABELS.get(code)
        if label is None:
            return Refuse("invalid_trade_proposal", form="unavailable", error=failure)
        return Refuse(code, (label,), user_facing=facing, error=failure)
    return Cards((Card("proposal.trade", proposal),), target_position=ctx.target_position)


def _not_held(ctx: PlanContext, symbol: str) -> Refuse:
    return Refuse("close_position_not_found", (f"当前没有{instrument_display(ctx.provider, symbol)}的持仓，平不了",))


def _batch_refusal(ctx: PlanContext, error: ProposalConflict) -> Refuse:
    """A close-batch card that could not be made: its own sentence; or each skipped contract and why (#721),
    「没有可平的持仓」 when nothing matched; else just the code (the order service being unavailable)."""
    skipped = getattr(error, "skipped", None)
    if skipped is not None:
        issues = [f"{instrument_display(ctx.provider, str(item.get('symbol') or ''))}："
                  f"{CANDIDATE_ISSUE_LABELS.get(str(item.get('reason')), str(item.get('reason')))}"
                  for item in skipped] or ["没有可平的持仓"]
        return Refuse(error.code, tuple(issues), error=error)
    if error.message:
        return Refuse(error.code, (error.message,), error=error)
    return Refuse(error.code, form="unavailable", error=error)
