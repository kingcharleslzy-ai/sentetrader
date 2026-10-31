"""语音前台直接查询（2026-10-13，所有者定的“实时语音模型直接调业务工具”第 1 步：查询链路）。

开关 `AI_TRADER_VOICE_FRONT_QUERY`：空 = 关（通话与之前逐项一致）；`all` = 全部用户；逗号分隔的
user_id = 只对这些用户。开着时，豆包前台只有一个高层只读工具 `query`（what 枚举 + 可选品种名），
查询、概念解释、寒暄、话说一半的反问都由豆包本轮自己回答，不再交给交易 AI；下单、平仓、撤单、
策略、研究判断、设置与界面仍由 Runtime 路由给交易 AI，行为不变。

这里只放纯函数与常量：开关、工具定义与前台说明、整句分类（哪些句子留给豆包）、几类查询结果的紧凑
中文整理、行情不新鲜时的原因句。取数仍走 UI / CLI 同一套已认证正式接口（`VoiceRealtimeService.front_query`）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .order_intent import OrderIntent
from .order_intent_parse import Parsed
from .voice_intents import ALERT_WORDS, PRODUCT_ALIASES, match_product_word

FRONT_QUERY_ENV = "AI_TRADER_VOICE_FRONT_QUERY"
# 第 2 步（2026-10-13）：下单类直接出卡。同样的写法（空 = 关，all，或逗号分隔的 user_id）；只在前台直接查询
# 也开着的通话里生效（它用的是前台的说明与工具表）。
FRONT_ACTION_ENV = "AI_TRADER_VOICE_FRONT_ACTION"
CHINA = timezone(timedelta(hours=8))


def _switched_on(env: str, user_id: str | None) -> bool:
    raw = os.getenv(env, "").strip()
    if not raw or not user_id:
        return False
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    return "all" in wanted or user_id in wanted


def front_query_enabled(user_id: str | None) -> bool:
    """Read at call start (the env may change between deployments, never mid-call)."""
    return _switched_on(FRONT_QUERY_ENV, user_id)


def front_action_enabled(user_id: str | None) -> bool:
    """下单类直接出卡, read at call start like the query switch."""
    return _switched_on(FRONT_ACTION_ENV, user_id)


QUERY_WHATS = ("quote", "positions", "account", "orders", "news", "brief", "analysis")

QUERY_TOOL: dict[str, Any] = {
    "type": "function", "name": "query",
    "description": "用户问行情、持仓、资金、委托、新闻、简讯、走势时调用，只念返回的内容。",
    "parameters": {"type": "object", "properties": {
        "what": {"type": "string", "enum": list(QUERY_WHATS),
                 "description": "quote价 positions仓 account钱 orders委托 news新闻 brief简讯 analysis走势"},
        "query": {"type": "string", "description": "用户说的品种名，如 螺纹、沪金；没说就留空"}},
        "required": ["what"]},
}

# 前台说明：与工具说明合计不超过 12K tokens（豆包上下文），这里只写一次每件事。
FRONT_INSTRUCTIONS = (
    "你是 Sente 的语音前台，中文口语，像懂期货的朋友。"
    "你自己回答：行情、持仓、资金、委托、新闻、简讯、走势这类查询，先调 query，调之前一个字都不要说，"
    "只念它返回的数字和说法，返回里有“说明”就照着说原因；止损、保证金、平今这类概念用大白话直接解释；寒暄；"
    "没听清或话只说了一半就反问想问什么；同音误听按上下文纠正（如“肖战”是焦炭）。"
    "第一句先给结论，整段一般不超过一百五十字，不编数字。"
    "下单、平仓、撤单、策略、设置、界面操作和“能不能买、会不会涨”这类判断由后台交易 AI 处理，"
    "你听到这类请求一声不吭，也不复述它念出的内容。"
    "系统念完待确认的下单内容后，用户说“确认/是/好”就调 confirm，说“取消/算了”就调 cancel；没有等确认的操作时不要调。"
    "read_screen 只在分不清用户指界面哪一处时用。"
)

# 下单类直接出卡（AI_TRADER_VOICE_FRONT_ACTION）：同一份前台说明，只把“下单一声不吭”这一句换成 action 的用法。
# action 只出待确认卡（撤单只撤唯一一笔挂单），参数以 Runtime 从原话听出的为准；确认仍是用户口头“确认”或点卡。
_DELEGATE_ORDERS = "下单、平仓、撤单、策略、设置、界面操作和“能不能买、会不会涨”这类判断由后台交易 AI 处理，你听到这类请求一声不吭，也不复述它念出的内容。"
ACTION_INSTRUCTION = (
    "用户明确要下单、平仓、全平、调到几手、撤挂单就调 action，调之前一个字都不要说，只念它返回的内容；"
    "没说多空、几个品种一起、策略、设置、界面操作和“能不能买、会不会涨”这类判断由后台交易 AI 处理，你一声不吭，也不复述它念出的内容。")
assert _DELEGATE_ORDERS in FRONT_INSTRUCTIONS
FRONT_ACTION_INSTRUCTIONS = FRONT_INSTRUCTIONS.replace(_DELEGATE_ORDERS, ACTION_INSTRUCTION)

ACTION_KINDS = ("open", "close", "close_all", "target", "cancel")
ACTION_TOOL: dict[str, Any] = {
    "type": "function", "name": "action",
    "description": "用户要下单、平仓、全平、调到几手或撤挂单时调用；只出待确认卡，念返回的内容。",
    "parameters": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(ACTION_KINDS),
                 "description": "open开仓 close平几手 close_all全平或平某品种 target调到几手 cancel撤挂单"},
        "product": {"type": "string", "description": "品种名，如 螺纹、沪金；没说留空"},
        "direction": {"type": "string", "enum": ["long", "short"], "description": "long多/买 short空/卖；没说留空"},
        "volume": {"type": "integer", "description": "手数；target 填目标手数，清零填0"},
        "price": {"type": "string", "description": "用户说的价：数字或 对手价/挂单/最新价；没说留空"}},
        "required": ["kind"]},
}


@dataclass(frozen=True)
class FrontTurn:
    """A sentence the front desk answers itself: `kind` query / explain / chat / half; for a query
    `what` (one of QUERY_WHATS) and the spoken contract (`symbol_query`, None = the one on screen)."""
    kind: str
    what: str | None = None
    symbol_query: str | None = None


_STRIP = re.compile(r"[\s，。,.!！?？、~～…·\"'“”‘’]")
# 交易 AI 的活：下单 / 平仓 / 撤单 / 策略 / 设置 / 研究判断 / 复合句。命中任何一个都不留给前台。
_TRADE_WORDS = re.compile(
    r"买|卖|开多|开空|开仓|做多|做空|下单|平仓(?!记录)|平掉|全平|清仓|撤|加仓|减仓|锁仓|反手|多单|空单|对手价|市价|"
    r"[一二两三四五六七八九十\d]+手(?![里上头中])|止损(?:设|改|挂|放|调|到|在|价)|止盈(?:设|改|挂|放|调|到|在|价)")
_WORK_WORDS = re.compile(
    rf"{ALERT_WORDS}|策略|回测|流程|老规矩|设置|语速|音色|默认手数|播报|推送|通知|主题|字号|画线|标注|画|切到|切换|打开|关掉|关闭|收起")
_JUDGE_WORDS = re.compile(
    r"能不能|可不可以|该不该|要不要|会不会|还会|能买|能卖|值不值|值得|建议|看法|怎么看|预测|判断|机会|入场|进场|抄底|追|"
    r"为什么|为啥|原因|影响|分析一下|研究|复盘|哪个|哪一个|比较|对比|接下来|后面|这波|能涨|能跌|要涨|要跌|涨到|跌到|"
    r"然后|顺便|接着|再帮|同时|并且")
# 概念解释：强标记直接算；“是什么/是啥”只在句子没点名品种、没问数时算。
_EXPLAIN_STRONG = re.compile(r"意思|啥叫|什么叫|^(?:请问)?(?:什么是|啥是)|怎么理解|不懂|不太懂|不明白|解释一下|解释下|科普|是什么东西|是个啥")
# The trend summary (_market_analysis) computes no MACD / KD and the spoken read drops RSI: a question
# about one of these indicators stays with the trading AI, which reads the indicator itself.
_INDICATOR = re.compile(r"MACD|KDJ?|RSI|布林|BOLL|CCI|金叉|死叉|指标", re.I)
_EXPLAIN_WEAK = re.compile(r"是什么|是啥|指什么|指的是")
# 话说一半：去掉这些口头填充后什么都不剩，且句子很短。
_HALF_FILLER = re.compile(
    r"呃|嗯|啊|哎|诶|那个|这个|就是|我想|我要|我想要|想|问一下|问问|问你一下|问你|请问|帮我|给我|帮忙|你|看看|看一下|查一下|查查|"
    r"说一下|说说|一下|那|我|的|呢|吧|呀|哦|嘛|还有|对了|关于")
_REFERENTIAL = re.compile(r"它|这个|那个|刚才|刚刚|上一个|前面|这品种|那品种")
_MULTI = re.compile(r"和|跟|与|以及|或者")
_ACCOUNT = re.compile(r"账户|资金|余额|权益|保证金|还剩多少钱|还有多少钱|可用|多少钱了")
_ORDERS = re.compile(r"委托|挂单|挂着的|没成交|未成交")
_POSITIONS = re.compile(r"手里|手上|持仓|仓位|浮盈|浮亏|盈亏|亏了|赚了|亏多少|赚多少|亏钱|赚钱")
_NEWS = re.compile(r"新闻|消息|快讯|资讯|要闻")
_BRIEF = re.compile(r"简讯|简报|早报|晚报")
_ANALYSIS = re.compile(r"走势|趋势|均线|波动|强不强|强势|弱势|形态|K线", re.I)
_QUOTE = re.compile(r"多少钱|价格|价位|什么价|报价|价|涨|跌|行情|怎么样|咋样|如何|多少|一吨|一克|成交量|盘面")
_FOLLOW = re.compile(r"^那?(.{1,8}?)(?:呢|现在呢|的呢|又呢|怎么样了)$")
FRONT_MAX_CHARS = 30


def _symbol_of(stripped: str, product_names: list[str] | None, last_symbol: str | None) -> tuple[bool, str | None]:
    """(ok, spoken contract): the one product the sentence names, else the one named earlier in the call
    when the words point back ("它" / "那个"), else None = the contract on screen. Two products → not ok."""
    hit = match_product_word(stripped, product_names)
    if hit is not None:
        return True, hit[1]  # the catalog name ("黄金" → 沪金): the instrument search knows no aliases
    if _MULTI.search(stripped) or (product_names and any(name and name in stripped for name in product_names)):
        return False, None  # two contracts ("螺纹和沪金都多少钱"), or a comparison: the trading AI decides
    if _REFERENTIAL.search(stripped) and last_symbol:
        return True, last_symbol
    return True, None


def classify_front(text: str, route: str, product_names: list[str] | None = None, *,
                   last_symbol: str | None = None, last_what: str | None = None) -> FrontTurn | None:
    """Which sentences the front desk answers itself (flag on), from the Runtime's own route of the same
    words: a chat or quick route always stays with the front desk; a delegate route only when the
    sentence is a plain query, a concept question or half a sentence. Everything with an order, a
    strategy, a setting, an interface word or a judgment stays with the trading AI (None)."""
    s = _STRIP.sub("", text or "")
    if not s:
        return FrontTurn("chat")
    if route not in {"chat", "quick", "delegate"}:
        return None
    trade = _TRADE_WORDS.search(s) is not None
    work = _WORK_WORDS.search(s) is not None
    judge = _JUDGE_WORDS.search(s) is not None
    if len(s) <= FRONT_MAX_CHARS and not judge and not work:
        # 概念解释先判：“止损是啥意思”带“止损”，但它问的是词，不是下单。
        has_product = match_product_word(s, product_names) is not None
        if (_EXPLAIN_STRONG.search(s) or _EXPLAIN_WEAK.search(s)) and (
                _NEWS.search(s) or re.search(r"这条|那条|这根|那根|图上|这段", s)):
            return None  # "这条新闻是什么意思": interpreting a report stays with the trading AI's sourced research
        if _EXPLAIN_STRONG.search(s) and not re.search(r"买|卖|下单|撤|平掉|全平|清仓|[一二两三四五\d]+手", s):
            return FrontTurn("explain")
        if _EXPLAIN_WEAK.search(s) and not trade and not has_product and not _QUOTE.search(s) \
                and not _ACCOUNT.search(s) and not _POSITIONS.search(s) and not _ORDERS.search(s):
            return FrontTurn("explain")
    if route == "chat":
        return FrontTurn("chat")
    if len(s) <= 12 and not _HALF_FILLER.sub("", s):
        return FrontTurn("half")
    if route == "delegate" and (trade or work or judge or len(s) > FRONT_MAX_CHARS):
        return None
    if _INDICATOR.search(s):
        return None  # "原油的MACD怎么样": the summary has no such reading — the trading AI reads the indicator
    if _ACCOUNT.search(s) and not _ORDERS.search(s):
        return FrontTurn("query", "account")  # the account names no contract
    ok, symbol = _symbol_of(s, product_names, last_symbol)
    if not ok:
        return None
    if _ORDERS.search(s):
        return FrontTurn("query", "orders", symbol)
    if _BRIEF.search(s):
        return FrontTurn("query", "brief", symbol)
    if _NEWS.search(s):
        return FrontTurn("query", "news", symbol)
    if _POSITIONS.search(s):
        return FrontTurn("query", "positions", symbol)
    if _ANALYSIS.search(s):
        return FrontTurn("query", "analysis", symbol)
    if _QUOTE.search(s):
        return FrontTurn("query", "quote", symbol)
    follow = _FOLLOW.fullmatch(s)
    if follow:
        hit = match_product_word(follow.group(1), product_names)
        named = hit[1] if hit is not None else PRODUCT_ALIASES.get(follow.group(1))
        if named:
            return FrontTurn("query", last_what if last_what in QUERY_WHATS else "quote", named)
    if route == "quick":
        return FrontTurn("query", "quote", symbol)
    return None


def front_arguments(front: dict[str, Any], asked: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `query` arguments for a front turn: the kind and contract the words named win over the model's
    guess (`asked`); what the words left open comes from the model."""
    asked = asked or {}
    what = front.get("what") if front.get("kind") == "query" and front.get("what") else asked.get("what")
    query = front.get("symbol_query") or asked.get("query")
    if what == "account":
        query = None
    return {key: value for key, value in (("what", what), ("query", query)) if value}


# -- 结果整理（纯函数） --------------------------------------------------------------------------

# 报价单位：常见品种的价格单位；没列出的只说“元”。股指、集运是点。
PRICE_UNITS: dict[str, str] = {
    **{code: "元/吨" for code in (
        "rb", "hc", "i", "j", "jm", "cu", "al", "zn", "pb", "ni", "sn", "ss", "ao", "bc", "ru", "nr", "br", "bu", "fu",
        "lu", "sp", "ma", "ta", "pf", "pta", "eg", "eb", "pp", "l", "v", "sa", "fg", "ur", "sh", "px", "sr", "cf", "cy",
        "ap", "cj", "pk", "oi", "rm", "rs", "m", "y", "p", "a", "b", "c", "cs", "rr", "wh", "pm", "ri", "jr", "lr", "sf",
        "sm", "zc", "lh", "si", "lc", "ps", "wr")},
    "au": "元/克", "ag": "元/千克", "sc": "元/桶", "jd": "元/500千克", "fb": "元/立方米", "bb": "元/张", "lg": "元/立方米",
    "if": "点", "ih": "点", "ic": "点", "im": "点", "ec": "点", "t": "元", "tf": "元", "ts": "元", "tl": "元",
}


def price_unit(symbol: str) -> str:
    """"元/吨" for SHFE.rb2701; "元" when the product is not in the table."""
    instrument = str(symbol or "").split(".")[-1]
    product = re.sub(r"\d+$", "", instrument).lower()
    return PRICE_UNITS.get(product, "元")


def change_words(change_percent: Any) -> str:
    """"跌 0.23%" / "涨 1.10%" / "平"; "—" when unknown. Spoken signs read badly ("负零点二三")."""
    try:
        value = float(change_percent)
    except (TypeError, ValueError):
        return "—"
    if abs(value) < 0.005:
        return "平"
    return f"{'涨' if value > 0 else '跌'} {abs(value):.2f}%"


def pnl_words(value: Any) -> str:
    """"浮盈 1,200 元" / "浮亏 300 元" / "持平" for one position's floating P&L (a spoken "+0 元" reads badly)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "浮动盈亏不明"
    if abs(number) < 0.5:
        return "持平"
    return f"{'浮盈' if number > 0 else '浮亏'} {abs(number):,.0f} 元"


def session_note(*, closed: bool, stale: bool, has_night: bool, exchange: str,
                 now: datetime | None = None) -> str | None:
    """Why the quote is not current, in words a person says: 已收盘 and when it opens again. None
    when the quote is current. Holidays are not known here: past a session it says 下个交易日."""
    if not closed and not stale:
        return None
    moment = (now or datetime.now(CHINA)).astimezone(CHINA)
    minutes = moment.hour * 60 + moment.minute
    weekday = moment.weekday()  # Monday = 0
    day_open = "上午九点半" if exchange.upper() == "CFFEX" else "上午九点"
    if not closed:
        return "行情这会儿没有更新，下面是最后一笔的价格"
    if weekday < 5 and 11 * 60 + 30 <= minutes < 13 * 60 + 30:
        return "现在午间休市，下午一点半开盘，这是上午收盘的价格"
    if weekday < 5 and 10 * 60 + 15 <= minutes < 10 * 60 + 30 and exchange.upper() != "CFFEX":
        return "现在是上午小节休息，十点半继续，这是休息前的价格"
    if weekday < 5 and 15 * 60 <= minutes < 21 * 60:
        if has_night:  # Friday has its night session too (it opens the next trading day); holiday eves are not known here
            return "现在已收盘，晚上九点开夜盘，这是收盘价"
        return f"现在已收盘，下个交易日{day_open}开盘，这是收盘价"
    if weekday < 5 and minutes < 9 * 60:
        return f"现在还没开盘，{day_open}开盘，这是上一节收盘的价格"
    return f"现在已收盘，下个交易日{day_open}开盘，这是收盘价"


def _clock(value: Any) -> str:
    """"10:32" for an ISO time (Beijing), "" when it cannot be read."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CHINA)
    return moment.astimezone(CHINA).strftime("%H:%M")


def orders_fields(activity: dict[str, Any], names: dict[str, str], *, product: str | None = None) -> dict[str, Any]:
    """The live (unfilled) orders: ≤4 lines plus a count. `product` keeps one product's orders."""
    if not activity.get("ordersInitialized", True) or activity.get("dataStatus") not in {"normal", "simulated", None}:
        return {"error": "委托数据暂时没更新，稍后再问"}
    live = [order for order in activity.get("orders") or []
            if isinstance(order, dict) and str(order.get("status")) == "ALIVE" and int(order.get("volumeLeft") or 0) > 0]
    if product:
        live = [order for order in live if re.sub(r"\d+$", "", str(order.get("symbol", "")).split(".")[-1]).lower() == product.lower()]
    if not live:
        return {"委托": "现在没有挂着没成交的委托"}
    lines = []
    for order in live[:4]:
        symbol = str(order.get("symbol") or "")
        direction = {"BUY": "买", "SELL": "卖"}.get(str(order.get("direction")).upper(), "")
        offset = {"OPEN": "开", "CLOSE": "平", "CLOSETODAY": "平今", "CLOSEYESTERDAY": "平昨"}.get(str(order.get("offset")).upper(), "")
        price = order.get("limitPrice")
        price_text = f"，限价 {price:g} {price_unit(symbol)}" if isinstance(price, (int, float)) else "，市价"
        lines.append(f"{names.get(symbol.lower()) or symbol} {direction}{offset} {int(order.get('volume') or 0)} 手{price_text}，"
                     f"还有 {int(order.get('volumeLeft') or 0)} 手没成交")
    out: dict[str, Any] = {"委托": "；".join(lines), "共": f"{len(live)} 笔"}
    return out


def news_fields(feed: dict[str, Any], label: str | None) -> dict[str, Any]:
    """The 今日新闻要点 one-liner (when the analyst wrote one) and ≤4 newest headlines with their time."""
    if feed.get("status") != "ok":
        return {"error": "新闻暂时取不到，稍后再问"}
    out: dict[str, Any] = {}
    brief = feed.get("brief") or {}
    if brief.get("status") in {"ready", "stale"} and brief.get("take"):
        out["要点"] = str(brief.get("take"))[:80]
    # Events hide the rows the AI is still screening: an empty event list falls back to the raw items.
    entries = feed.get("events") or feed.get("items") or []
    titles = []
    for entry in entries[:4]:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("eventLabel") or entry.get("title") or "").strip()
        if title:
            at = _clock(entry.get("publishedAt"))
            titles.append(f"{at} {title[:40]}".strip())
    if not titles and not out:
        pending = int((feed.get("screen") or {}).get("pending") or 0)
        if pending:
            return {"新闻": f"有 {pending} 条{label or ''}相关的快讯还在筛选，稍后再问"}
        return {"新闻": f"最近一天没有{label or ''}相关的快讯"}
    if titles:
        out["新闻"] = "；".join(titles)
    out["说明"] = "这是第三方快讯，没核实过"
    return out


def brief_fields(snapshot: dict[str, Any]) -> dict[str, Any]:
    """≤4 newest 简讯 (AI 简报) items with product and time."""
    if snapshot.get("status") == "disabled":
        return {"简讯": "简讯没有开，可以在 AI 动态卡里打开"}
    items = [item for item in snapshot.get("items") or [] if isinstance(item, dict)]
    if not items:
        return {"简讯": "现在没有新的简讯"}
    lines = [f"{_clock(item.get('publishedAt'))} {item.get('productName') or ''} {str(item.get('text') or '')[:50]}".strip()
             for item in items[:4]]
    return {"简讯": "；".join(lines), "共": f"{len(items)} 条"}


def analysis_fields(analysis: dict[str, Any], label: str, *, note: str | None = None) -> dict[str, Any]:
    """The deterministic summary (trader_tools._market_analysis on the daily workbench) in a few readings:
    trend, the window's change, the 5/20-day averages, volatility and RSI. The voice reads, never infers."""
    symbol = str(analysis.get("symbol") or "")
    unit = price_unit(symbol)
    trend = {"up": "偏强，五日均价在二十日均价上方", "down": "偏弱，五日均价在二十日均价下方",
             "range": "震荡，没有明确方向"}.get(str(analysis.get("trend")), "看不出方向")
    indicators = analysis.get("indicators") or {}
    out: dict[str, Any] = {
        "合约": label,
        "走势": f"日线{trend}",
        "近期涨跌": f"近 {int(analysis.get('barCount') or 0)} 个交易日{change_words(analysis.get('periodReturnPercent'))}",
        "均价": f"五日 {float(analysis.get('sma5') or 0):.0f}、二十日 {float(analysis.get('sma20') or 0):.0f} {unit}",
        "波动": str((indicators.get("atr") or {}).get("reading") or "—"),
        "RSI": str((indicators.get("rsi") or {}).get("reading") or "—"),
    }
    if note:
        out["说明"] = note
    return out


def spoken_line(fields: dict[str, Any]) -> str:
    """What the Runtime says itself for a query it answers without Doubao (Doubao left the sentence
    open, or talked before calling the tool): the fields in order, one sentence."""
    if "error" in fields:
        # A failed tool says it in words (`say`); `error` is then a machine code ("TimeoutError").
        return str(fields.get("say") or fields["error"]).rstrip("。") + "。"
    parts = []
    for key, value in fields.items():
        if key in {"更新于", "买一卖一", "均价"}:
            continue  # a time, a bid/ask pair and the averages (走势 already says how they lie) read badly aloud
        text = str(value)
        signed = re.fullmatch(r"([+-])(.+)", text)
        if key in _PNL_WORDS and signed:
            gain, loss = _PNL_WORDS[key]
            amount = signed.group(2)
            parts.append(f"{gain if signed.group(1) == '+' else loss} {amount}" if amount.strip("0 元,") else f"{key}持平")
        elif key in {"说明", "委托", "新闻", "简讯", "要点", "走势", "近期涨跌", "合约", "账户", "涨跌", "波动"} \
                or (key == "持仓" and not re.fullmatch(r"\d+ 个", text)):  # the account's "3 个" keeps its key
            parts.append(text)
        else:
            parts.append(f"{key} {text}")
    return "，".join(parts) + "。"


_PNL_WORDS = {"今日盈亏": ("今天赚", "今天亏"), "浮动盈亏": ("浮盈", "浮亏"), "合计浮动盈亏": ("合计浮盈", "合计浮亏")}


# Doubao never finalised the sentence (强制判停后仍无定稿), so it will not answer it either, and the
# realtime API has no "answer this text" event (response.create is refused, measured 2026-10-13).
# A query the Runtime answers from the tool; the other kinds get one short line instead of the agent.
TAKEOVER_LINES = {
    "chat": "在的，你说。",
    "half": "你想问什么？说品种或者问题就行。",
    "explain": "刚才没听全，能再说一遍吗？",
}
_THANKS = re.compile(r"谢谢|多谢|辛苦")


def takeover_line(kind: str, text: str) -> str:
    """"" = say nothing (a stalled "嗯" / "好" needs no answer)."""
    if kind == "chat" and _THANKS.search(text or ""):
        return "不客气。"
    if kind == "chat" and len(_STRIP.sub("", text or "")) <= 2:
        return ""
    return TAKEOVER_LINES.get(kind, TAKEOVER_LINES["explain"])


# -- 下单类直接出卡（第 2 步，AI_TRADER_VOICE_FRONT_ACTION）：哪些句子前台自己出卡（纯函数） ------------------------
# Measured 2026-10-13 against the live Doubao (~/ai-trader-release-artifacts/voice-action-20261013/): with `action` declared it
# calls the tool on clear orders, but its arguments are not trustworthy — "空两手白银" came back long,
# "820挂一手沪金多单" as 螺纹 at 3082, "来一手螺纹" (no direction) as a long open. So the Runtime decides from the
# words alone, with the one intent parser every entry shares (order_intent_parse, 意图重构第 2 步), and only a
# sentence it reads completely becomes a card here; anything it is unsure of or that leaves something open (no
# direction or lots, a question, a condition, a strategy, a spread) keeps the trading AI and its follow-up card.

def front_order(parsed: Parsed) -> OrderIntent | None:
    """The parsed sentence when the front desk cards it itself: an opening with its side and lots, a close of some
    lots or a half (平一半), 全平, 目标持仓, 反手, 撤单 (one working order), or several of these in one sentence (the
    parser's `legs`: carded in order, each asked on its own). None = the sentence keeps its route (the trading AI asks
    what is missing)."""
    if not isinstance(parsed, OrderIntent) or parsed.stance != "order":
        return None
    return parsed if _carded(parsed) else None


def _carded(intent: OrderIntent) -> bool:
    if intent.legs:
        return all(_carded(leg) for leg in intent.legs)
    if intent.act == "open":
        return intent.dir is not None and bool(intent.vol)
    if intent.act == "close":
        return bool(intent.vol) or intent.frac is not None
    return intent.act in {"close_all", "target", "reverse", "cancel"}


def spoken_contract(intent: OrderIntent) -> str | None:
    """The contract the words named: a spoken month ("螺纹2701") or the catalog name ("螺纹"); None = the window's
    (or, for 全平, every position)."""
    return intent.symbol or intent.product


# -- 卡片念法与拒绝原因（纯函数） ----------------------------------------------------------------------

_SKIP_WORDS = {"quote_stale": "行情不是最新", "quote_not_current": "行情不是最新", "market_session_closed": "不在交易时段",
               "limit_price_unavailable": "没有可用的对手价", "close_volume_unavailable": "可平手数不足"}
SIDE_WORDS = {"buy_open": "买开", "sell_open": "卖开", "buy_close": "买平", "sell_close": "卖平"}
PRICE_INTENT_WORDS = {"aggressive": "对手价", "passive": "挂单价", "last": "最新价"}
_BUCKET_WORDS = {"today": "平今", "historical": "平昨"}


def _price_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:g}"


def _order_words(order: dict[str, Any], *, own_price: bool, names: dict[str, str] | None = None) -> str:
    """"螺纹2701，买开 1 手，对手价 3078" — contract (its catalog name), side, lots and the card's price."""
    symbol = str(order.get("symbol") or "")
    name = (names or {}).get(symbol.lower()) or str(order.get("name") or symbol)
    side = SIDE_WORDS.get(str(order.get("side")), "")
    bucket = _BUCKET_WORDS.get(str(order.get("positionBucket") or ""), "")
    price = _price_number(order.get("limitPrice"))
    how = "限价" if own_price else PRICE_INTENT_WORDS.get(str(order.get("priceIntent") or ""), "限价")
    return (f"{name}，{side} {int(order.get('volume') or 0)} 手{f'（{bucket}）' if bucket else ''}"
            + (f"，{how} {price}" if price else ""))


def _net_words(net: int) -> str:
    return "0 手" if net == 0 else f"{'多' if net > 0 else '空'} {abs(net)} 手"


def card_line(action: OrderIntent, card_name: str, card: dict[str, Any], names: dict[str, str] | None = None) -> str:
    """What the voice reads for a card the action made: contract, side, lots and price, then the
    question. A risk-blocked card (no approval) says why instead of asking."""
    if card.get("status") == "risk_blocked" or not card.get("approvalId"):
        reason = str((card.get("riskCheck") or {}).get("reason") or "风控检查没通过")
        return f"这单下不了：{reason.rstrip('。')}。"
    target = card.get("targetPosition") if isinstance(card.get("targetPosition"), dict) else None
    if card_name == "proposal.close_batch":
        orders = [order for order in card.get("orders") or [] if isinstance(order, dict)]
        if len(orders) == 1:
            body = _order_words(orders[0], own_price=False, names=names)
        else:
            body = (f"{len(orders)} 张平仓单：" + "；".join(_order_words(order, own_price=False, names=names) for order in orders[:4])
                    + (f"；另有 {len(orders) - 4} 张" if len(orders) > 4 else ""))
        skipped = [item for item in card.get("skipped") or [] if isinstance(item, dict)]
        if skipped:
            # A batch that leaves positions out says so before asking (#851 review): which, and why.
            body += "；" + "、".join(
                f"{(names or {}).get(str(item.get('symbol') or '').lower()) or str(item.get('symbol') or '')}"
                f"（{_SKIP_WORDS.get(str(item.get('reason')), '没有可用报价')}）" for item in skipped[:3]) \
                + f"{'等' if len(skipped) > 3 else ''}这次平不了，不在这张卡里"
    else:
        body = _order_words(card, own_price=action.price.limit is not None, names=names)
    if target is not None and target.get("sentence"):
        # 一句两单接力（同一合约先平后开）：先说整句的几步，再念第 1 张，后面的卡在这一张成交后出。
        steps = int(target.get("totalSteps") or len(target.get("legs") or []) or 1)
        return (f"{str(target.get('summary') or '').rstrip('。')}。{body}，这是第 1 步共 {steps} 步，"
                f"成交后我再给你下一步的卡，确认吗？")
    if target is not None:
        name =action.product or re.sub(r"\d+$", "", str(card.get("name") or "")) or str(target.get("symbol") or "")
        steps = int(target.get("totalSteps") or len(target.get("legs") or []) or 1)
        lead = f"{name}调到{_net_words(int(target.get('targetNet') or 0))}，现在{_net_words(int(target.get('currentNet') or 0))}"
        # 反手确认一次 (2026-10-24): this one 确认 also sends the opening after the close has fully filled.
        step = f"，共 {steps} 步，这一次确认就够，平仓全部成交后自动开仓" if steps > 1 else ""
        return f"{lead}。{body}{step}，确认吗？"
    return f"{body}，确认吗？"


def other_legs_line(notes: tuple[str, ...] | list[str]) -> str:
    """一句多单: the legs of the sentence that made no card, said after the cards — each in the planner's own words."""
    words = "；".join(refusal_line([note]).rstrip("。") for note in notes if str(note).strip())
    return f"另有 {len(notes)} 单没出卡：{words}。" if words else ""


def reopen_words(*, has_night: bool, exchange: str, now: datetime | None = None) -> str:
    """When the contract can be ordered again, in words: "晚上九点开夜盘" / "下午一点半开盘" / …
    Holidays are not known here: past a session it says 下个交易日."""
    moment = (now or datetime.now(CHINA)).astimezone(CHINA)
    minutes, weekday = moment.hour * 60 + moment.minute, moment.weekday()
    day_open = "上午九点半开盘" if exchange.upper() == "CFFEX" else "上午九点开盘"
    if weekday < 5 and 11 * 60 + 30 <= minutes < 13 * 60 + 30:
        return "下午一点半开盘"
    if weekday < 5 and 10 * 60 + 15 <= minutes < 10 * 60 + 30 and exchange.upper() != "CFFEX":
        return "十点半继续"
    if weekday < 5 and 15 * 60 <= minutes < 21 * 60 and has_night:  # Friday included: it has a night session
        return "晚上九点开夜盘"
    if weekday < 5 and minutes < 9 * 60:
        return day_open
    return f"下个交易日{day_open}"


def refusal_line(issues: list[str], *, label: str | None = None, reopen: str | None = None) -> str:
    """A card the tool refused, in the tool's own words (userFacing.candidateIssues) with the contract
    code in parentheses dropped; `reopen` (the market is closed) says when it opens instead:
    "螺纹现在不在交易时段，晚上九点开夜盘才能下单。"."""
    if reopen is not None:
        return f"{label or '这个合约'}现在不在交易时段，{reopen}才能下单。"
    words = "；".join(re.sub(r"[（(][^）)]*[A-Za-z][^）)]*[）)]", "", str(issue)).strip()
                     for issue in issues if str(issue).strip())
    return (words or "这单现在出不了卡").rstrip("。") + "。"
