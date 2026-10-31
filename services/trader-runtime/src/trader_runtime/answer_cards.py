"""对话回答卡片化 (2026-10-14, owner-approved three layers).

1. 工具结果自动成卡 — deterministic, never the model's choice: a successful read of positions, working
   orders, news, the market analysis or the quote board becomes a `card.data` built here from the tool's
   own result; quotes and the account keep their `card.market` / `card.account`. Several reads of one kind
   in a turn merge into one card (same cardId, republished in place): a second contract rides the quote
   card as a peer, a second analysis becomes another row.
2. 回答卡 `card.answer` — the model's synthesized answer to an analysis / compare / "咋样" question as one
   card through the `answer_card` tool: title, one conclusion, ≤6 figures, optional two-sided compare,
   ≤3 reasons, one risk line. 结构化 (2026-10-17): every reading on it is a field key into this turn's 读数表
   (`answer_readings.Facts`) — cells are keys, the conclusion, the columns, the reasons and the risk are templates —
   and the Runtime renders the value, unit and direction; the model's own words carry no number and (but for the
   risk) no reading word. A key with no reading or a contradicted value is dropped and the model is told; such a
   conclusion refuses the card. The basis (when, read from what) and the two buttons (看图表 / 问 AI 怎么办) are
   the Runtime's own. 去重: one fact once — a compare card shows its two columns and no cells repeating them; a
   reason does not repeat a key the card shows; and the turn's read cards shown before the answer card fold into
   its 依据 (`folded`, the client renders them there) instead of standing beside it.
3. Small talk and short confirmations stay plain text.

With a card on screen the answer text keeps its first one or two sentences (`conclusion_text`); the turn
records whether its text stated figures and how many cards it showed (`has_numeric_claim`), so turns that
still answered data in prose can be counted and covered next.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .answer_readings import (
    ANALYSIS_FIELDS,
    FIELDS,
    KIND_WORDS,
    LABEL_WORDS,
    QUOTE_FIELDS,
    STATES,
    VALUE_WORDS,
    Facts,
    short_code,
)
from .conversation_cards import ask_action, price_text, ui_action
from .instruments import money_unit, symbol_currency

ANSWER_CARD_TOOL = "answer_card"
_SIDE = {"type": "object", "properties": {
    "symbol": {"type": "string", "description": "这一栏的合约（本轮读过的），键默认取它"}, "title": {"type": "string"},
    "lines": {"type": "array", "maxItems": 3, "items": {"type": "string"}}}}
# 示例与常见错误 (10-17 成本): the 10-17 readback took four answer_card calls for “螺纹现在怎么样” — a number in the
# title, a reading word in the conclusion, a state key without its value. Two whole correct calls (tests check that both
# pass the Runtime's own check) and the mistakes it sends back, so the first call is right.
ANSWER_CARD_EXAMPLES: tuple[dict[str, Any], ...] = (
    {"title": "螺纹短线", "symbol": "SHFE.rb2701", "conclusion": "螺纹{day}，{trend=up}但{oiPeriodState=down}，追高要谨慎",
     "figures": ["day", "volumeState", "oiPeriodState", "rsiZone"], "reasons": ["价格站在{ma20}上方，回踩有支撑"],
     "risk": "若跌破{ma20}且继续减仓，偏强判断失效"},
    {"title": "铁矿与焦炭", "symbol": "DCE.i2701", "conclusion": "铁矿{i2701.dayMove=up}、{i2701.volumeState=up}，焦炭{j2701.dayMove=down}，铁矿更强",
     "compare": {"left": {"symbol": "DCE.i2701", "lines": ["{day}", "{trend=up}"]},
                 "right": {"symbol": "DCE.j2701", "lines": ["{day}", "{trend=down}"]}},
     "risk": "若铁矿跌回{i2701.ma20}下方，强弱可能翻转"},
)
ANSWER_CARD_DEFINITION: dict[str, Any] = {
    "type": "function", "name": ANSWER_CARD_TOOL,
    "description": (
        "读完数据后把本轮综合回答做成一张回答卡给用户看，作为本轮最后一步单独调一次：调完本轮即结束，卡上的一句结论就是回答正文。"
        "卡上的读数一律写字段键 {[合约.]字段[@周期][=取值]}（格子只写键，结论、对比行、理由、风险是嵌键的模板），"
        "Runtime 按本轮读数填值、单位和方向。"
        f"数值字段：{' '.join(QUOTE_FIELDS)}（行情快照/报价总表）；{' '.join(ANALYSIS_FIELDS)}（走势分析）。定性键："
        + "；".join(f"{key}={'/'.join(VALUE_WORDS[STATES[key][1]])}" for key in STATES)
        + "。day/dayMove 是对昨结算的当日涨跌，来自行情快照，没读快照就没有；periodMove 是走势分析的区间涨跌。"
        "周期 5m/15m/1h/1d/1w 只在这个合约本轮读了两种周期时写；合约省略取这一栏或卡的 symbol，卡上有两个合约时结论、理由、风险里的键都写合约。"
        "正确示例：" + "；".join(json.dumps(example, ensure_ascii=False) for example in ANSWER_CARD_EXAMPLES)
        + "。常见错误（会退回重写）：①标题、结论、理由、对比行里自己写数字——“5 分钟”“MA20”“2701”也是数字，周期和均线写进键（{ma20}、{volRatio@5m}）；"
        "②结论、理由、对比行里自己写读数词（放量、缩量、增仓、减仓、上涨、下跌、持平、上行、下行、震荡、超买、超卖、多头排列、上轨等），"
        "要写成带取值的定性键（{volumeState=up}、{oiPeriodState=down}、{trend=range}），取值照抄 labels 和 dayMove；"
        "③句子里的定性键不写取值（{trend} 要写 {trend=up}，格子里可以不写）；④字段名照上面的写（ma5 不是 sma5，volRatio 不是 volumeRatio）；"
        "⑤day 与 dayMove 是同一个当日涨跌，一张卡只写一个。"),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "品种与时段的文字，如 沪金夜盘、螺纹短线；不写数字（含合约月份和周期）"},
            "conclusion": {"type": "string", "description": "一句结论（模板），最多 3 个键：每个键填上读数约 15 字，全句不超过 80 字；见示例"},
            "figures": {"type": "array", "maxItems": 6, "items": {"type": "string"},
                        "description": "字段键，如 day、volumeState、j2701.day；对比两个品种时不填，写进 compare 两栏"},
            "compare": {"type": "object", "properties": {"left": _SIDE, "right": _SIDE}, "description": (
                "对比两个品种或两种情况时才填（填了就不填 figures）。每行是模板，如 {day}、{volumeState=down}，键默认取这一栏的合约；"
                "填了 compare，结论、理由、风险里的每个键都要写合约，如 {i2701.dayMove=up}、{j2701.ma20}")},
            "reasons": {"type": "array", "maxItems": 3, "description": "只写原因（模板）；格子和对比栏已显示的键不再显示",
                        "items": {"type": "string"}},
            "risk": {"type": "string", "description": "风险或前提，一句（模板）；可写读数词，不写数字"},
            "symbol": {"type": "string", "description": "主要合约代码，如 SHFE.au2612"},
        },
        "required": ["title", "conclusion"],
    },
    "strict": False,
}
# The turn's request carries this line when the answer card is offered (analysis-type questions only).
ANSWER_CARD_INSTRUCTION = ("本轮读完数据后最后单独调一次 answer_card 出回答卡，卡的结论就是回答；照 answer_card 说明的示例写，"
                           "数字和读数词只写成字段键，避开说明里的常见错误；本轮已读到的数据不要重读。\n")
ANSWER_CARD_RULE = "回答卡已显示给用户：正文只写一两句结论，不要复述卡里的数字。"

# Analysis-type wording: the answer card is offered (and asked for) on these, beside a product or the screen.
_ANALYSIS_WORDS = re.compile(
    r"咋样|怎么样|怎样|如何|怎么看|看法|观点|对比|比较|比一比|哪个强|哪个弱|哪个(?:涨|跌)得|谁强|谁弱|强还是弱|强不强|弱不弱"
    r"|波动|走势|能追|能买|能做|能进|值得|分析|趋势|指标|均线|MACD|macd|KD|kd|RSI|rsi|布林|后市|还会涨|还会跌|会涨|会跌")
# Questions the data cards already answer (the account, orders, news) or that are not about the market.
_ANSWER_EXCLUDE = re.compile(r"新闻|消息|快讯|持仓|仓位|账户|资金|委托|挂单|止损|止盈|策略|回测|设置|提醒|画|标|手续费|保证金|"
                             r"下单|开仓|平仓|买开|卖开|买平|卖平|[一二两三四五六七八九十\d]+\s*手|怎么用|界面")


def answer_card_wanted(user_text: str, *, route_kind: str, names_product: bool, screen_reference: bool) -> bool:
    """Whether this turn offers (and asks for) the answer card: an opinion question always; a market
    question or a general one only when it asks for analysis (a plain price question has its quote card)."""
    text = "".join((user_text or "").split())
    if not text or route_kind not in {"opinion", "market_query", "general"}:
        return False
    if route_kind == "opinion":
        return True
    if _ANSWER_EXCLUDE.search(text) or not _ANALYSIS_WORDS.search(text):
        return False
    return route_kind == "market_query" or names_product or screen_reference


# --- layer 1: read results as cards -------------------------------------------------------------

AUTO_DATA_TOOLS = {"get_positions": "positions", "get_account_activity": "orders", "get_news_flash": "news",
                   "get_market_analysis": "analysis", "get_market_board": "quotes", "get_research_brief": "news"}
SOURCE_LABELS = {
    "get_market_snapshot": "行情快照", "get_market_analysis": "走势分析", "get_chart_bars": "K 线",
    "get_market_board": "报价总表", "get_positions": "持仓", "get_account_summary": "账户",
    "get_account_activity": "委托与保护", "get_news_flash": "快讯", "get_research_brief": "调研数据",
    "get_futures_decision_context": "决策上下文", "web_search": "联网搜索", "get_foreign_markets": "外盘",
    "get_settlement_prices": "结算价", "get_symbol_ranking": "持仓排名", "get_main_contract_history": "主力历史",
    "get_economic_calendar": "财经日历", "get_trade_journal": "交易日志", "get_daily_review": "今日复盘",
}
_DURATIONS = {60: "1 分钟", 180: "3 分钟", 300: "5 分钟", 900: "15 分钟", 1800: "30 分钟", 3600: "1 小时",
              14400: "4 小时", 86400: "日线", 604800: "周线"}
_SHANGHAI = timezone(timedelta(hours=8))
_NEWS_DIRECTION = {"偏多": "up", "偏空": "down"}
_TIER = {"major": "重要", "other": "", "minor": "次要", "unscreened": ""}
_BOARD_SORT = {"changePercent": "涨跌幅", "volume": "成交量", "openInterest": "持仓量", "openInterestChange": "增仓"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def number(value: Any, digits: int = 2) -> str:
    """A figure as text: integers stay integers, others keep at most `digits` decimals."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return str(value)
    if parsed == int(parsed) and abs(parsed) < 1e15:
        return str(int(parsed))
    return f"{parsed:.{digits}f}".rstrip("0").rstrip(".")


def signed(value: Any, digits: int = 2, suffix: str = "") -> str:
    """A signed figure: "+" on a rise, never "-0"; a percentage keeps exactly `digits` decimals (the client's
    signedPercent writes quote changes the same way)."""
    try:
        parsed = round(float(value), digits)
    except (TypeError, ValueError):
        return f"{value}{suffix}"
    if parsed == 0:
        parsed = 0.0
    text = f"{parsed:.{digits}f}" if suffix == "%" else number(parsed, digits)
    return f"{'+' if parsed > 0 else ''}{text}{suffix}"


def direction(value: Any, digits: int = 2) -> str | None:
    """up / down of a figure as shown (rounded to `digits`): a value that rounds to zero has no direction."""
    try:
        parsed = round(float(value), digits)
    except (TypeError, ValueError):
        return None
    return "up" if parsed > 0 else "down" if parsed < 0 else None


def _moment(value: Any) -> datetime | None:
    """An ISO time as an aware datetime (naive = Beijing time); None when unreadable."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=_SHANGHAI)


def local_clock(value: Any) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()):
        return str(value).strip()[5:]  # date only: no clock the source never gave
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_SHANGHAI)
    return moment.astimezone(_SHANGHAI).strftime("%H:%M")


def duration_label(seconds: Any) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return ""
    return _DURATIONS.get(value) or (f"{value // 60} 分钟" if value % 60 == 0 and value else f"{value} 秒")


def short_symbol(symbol: Any) -> str:
    text = str(symbol or "")
    return text.split(".", 1)[1] if "." in text and not text.startswith("KQ.") else text


def _figure(label: str, value: str, *, unit: str | None = None, dir: str | None = None) -> dict[str, Any]:
    return {"label": label[:12], "value": value[:24], **({"unit": unit[:8]} if unit else {}), **({"dir": dir} if dir else {})}


def _row(primary: str, *, secondary: str | None = None, value: str | None = None, dir: str | None = None,
         tag: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"primary": primary[:80]}
    if secondary:
        row["secondary"] = secondary[:120]
    if value:
        row["value"] = value[:24]
    if dir:
        row["dir"] = dir
    if tag:
        row["tag"] = tag[:12]
    return row


_STATUS_WARNINGS = {"stale": "数据陈旧", "backup": "备用行情"}
STALE_BRIEF_WARNING = "要点陈旧"
_FLASH_ACTION = ("打开快讯", {"news": {"tab": "flash"}})
_RESEARCH_ACTION = ("打开消息面", {"news": {"tab": "news"}})


def _source_line(label: str, data: dict[str, Any]) -> str:
    status = str(data.get("dataStatus") or "")
    suffix = {"stale": "（数据陈旧）", "simulated": "（演示数据）", "backup": "（备用行情）"}.get(status, "")
    return label + suffix


def _both_ledgers(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """本站模拟两本账（2026-10-21）：这一项（持仓、委托、保护、成交）本账户的在前，同一用户美元子账户（外盘，
    工具结果的 ``foreignAccount``）的接在后面并标 ``_usd``——卡上分开写，不和人民币相加。"""
    own = [row for row in data.get(key) or [] if isinstance(row, dict)]
    foreign = data.get("foreignAccount")
    if not isinstance(foreign, dict) or foreign.get("status") != "ok":
        return own
    return own + [{**row, "_usd": True} for row in foreign.get(key) or [] if isinstance(row, dict)]


def positions_card(data: dict[str, Any], *, account_word: str) -> dict[str, Any]:
    rows, total, lots, usd_total = [], 0.0, 0, None
    for position in _both_ledgers(data, "positions"):
        volume = int(position.get("volume") or 0)
        pnl = float(position.get("unrealizedPnl") or 0)
        if position.get("_usd"):
            usd_total = (usd_total or 0.0) + pnl
        else:
            total += pnl
        lots += volume
        side = "多" if position.get("direction") == "long" else "空"
        today, history = int(position.get("todayVolume") or 0), int(position.get("historicalVolume") or 0)
        detail = [f"开仓均价 {price_text(position['openPrice'])}"] if position.get("openPrice") is not None else []
        if today and history:
            detail.append(f"今 {today} 昨 {history}")
        if position.get("_usd"):
            detail.append("外盘 · 美元")
        rows.append(_row(f"{position.get('instrumentId') or short_symbol(position.get('symbol'))} {side} {volume} 手",
                         secondary=" · ".join(detail) or None, value=signed(pnl, 0), dir=direction(pnl)))
    # 一个账户的持仓一个币种：美元子账户（外盘）的浮动盈亏是美元。
    currencies = {symbol_currency(position.get("symbol")) for position in data.get("positions") or [] if isinstance(position, dict)}
    unit = money_unit(currencies.pop()) if len(currencies) == 1 else "元"
    figures = [_figure("浮动盈亏", signed(total, 0), unit=unit, dir=direction(total))] if rows else []
    if usd_total is not None:
        figures.append(_figure("外盘浮动盈亏", signed(usd_total, 0), unit="美元", dir=direction(usd_total)))
    if rows:
        figures.append(_figure("持仓", str(lots), unit="手"))
    return {"kind": "positions", "title": f"持仓 · {len(rows)} 个" if rows else "持仓", "figures": figures,
            "rows": rows[:12], "truncated": len(rows) > 12, "empty": None if rows else "当前没有持仓",
            "sources": [_source_line(f"持仓（{account_word}）", data)],
            "actions": [ui_action("打开持仓", {"panel": "positions"})]}


def _alive(order: dict[str, Any]) -> bool:
    try:
        left = int(order.get("volumeLeft", order.get("volume")) or 0)
    except (TypeError, ValueError):
        left = 0
    return str(order.get("status") or "").upper() == "ALIVE" and left > 0


def _trade_clock(trade: dict[str, Any]) -> str:
    """A fill's clock time (tradeDateTime in nanoseconds, Beijing time); empty when unreadable."""
    try:
        moment = datetime.fromtimestamp(int(trade["tradeDateTime"]) / 1e9, tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.astimezone(_SHANGHAI).strftime("%H:%M")


def orders_card(data: dict[str, Any]) -> dict[str, Any]:
    # An uninitialized order feed or an unreadable protection list is unknown, never "none" (#878 review).
    orders_known = data.get("ordersInitialized") is not False
    protections_known = "protections" not in data or isinstance(data.get("protections"), list)
    orders = [order for order in _both_ledgers(data, "orders") if _alive(order)]
    protections = [row for row in _both_ledgers(data, "protections") if row.get("effective")]
    rows = []
    for order in orders:
        side = {"BUY": "买", "SELL": "卖"}.get(str(order.get("direction") or "").upper(), "")
        offset = "开" if str(order.get("offset") or "").upper() == "OPEN" else "平"
        left = order.get("volumeLeft", order.get("volume"))
        price = order.get("limitPrice")
        rows.append(_row(f"{order.get('instrumentId') or short_symbol(order.get('symbol'))} {side}{offset} {left} 手",
                         secondary=f"委托 {order.get('volume')} 手" + (f" · 限价 {price_text(price)}" if price else " · 市价")
                         + (" · 外盘 · 美元" if order.get("_usd") else ""),
                         value=price_text(price) if price else None, tag="委托"))
    for stop in protections:
        side = "多单" if stop.get("positionDirection") == "long" else "空单"
        parts = [f"止损 {price_text(stop['triggerPrice'])}" if stop.get("triggerPrice") else None,
                 f"止盈 {price_text(stop['takeProfitPrice'])}" if stop.get("takeProfitPrice") else None,
                 f"{stop.get('remainingVolume') or stop.get('volume')} 手"]
        if stop.get("_usd"):
            parts.append("外盘 · 美元")
        rows.append(_row(f"{short_symbol(stop.get('symbol'))} {side}保护",
                         secondary=" · ".join(part for part in parts if part), tag="保护"))
    trades = _both_ledgers(data, "trades")
    # Today's fills one per row (#878 review: "今天成交了哪些" had only their count on the card), newest first.
    for trade in reversed(trades):
        side = {"BUY": "买", "SELL": "卖"}.get(str(trade.get("direction") or "").upper(), "")
        offset = "开" if str(trade.get("offset") or "").upper() == "OPEN" else "平"
        price = trade.get("price")
        rows.append(_row(f"{trade.get('instrumentId') or short_symbol(trade.get('symbol'))} {side}{offset} {trade.get('volume')} 手",
                         secondary=" · ".join(part for part in (
                             f"成交价 {price_text(price)}" if price is not None else "", _trade_clock(trade),
                             str(trade.get("sourceLabel") or ""), "外盘 · 美元" if trade.get("_usd") else "") if part) or None,
                         value=price_text(price) if price is not None else None, tag="成交"))
    figures = [_figure("挂单", str(len(orders)), unit="笔") if orders_known else _figure("挂单", "读不到"),
               _figure("保护", str(len(protections)), unit="条") if protections_known else _figure("保护", "读不到")]
    if orders_known:
        figures.append(_figure("今日成交", str(len(trades)), unit="笔"))
    unknown = [part for part, known in (("委托还在初始化，暂时读不准", orders_known),
                                        ("保护单暂时读不到，不代表没有", protections_known)) if not known]
    return {"kind": "orders", "title": "挂单与保护", "figures": figures, "rows": rows[:12], "truncated": len(rows) > 12,
            "note": "；".join(unknown) + "，以委托栏为准" if unknown else None,
            "empty": None if rows or unknown else "没有挂着的委托，也没有生效的止损止盈",
            "sources": [_source_line("委托与保护", data)],
            "actions": [ui_action("打开委托", {"positionsTab": "orders"})]}


def news_card(data: dict[str, Any], *, product: str | None) -> dict[str, Any] | None:
    rows = []
    if data.get("topic") == "news":
        # 调研“消息面” (get_research_brief topic=news): third-party reports.
        if data.get("status") != "ok":
            return None
        for item in data.get("results") or []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            when = local_clock(item.get("publishDate") or item.get("publishedAt") or "")
            rows.append(_row(str(item["title"]), secondary=" · ".join(part for part in (
                when, str(item.get("media") or item.get("source") or "")) if part) or None))
        title = f"消息面 · {data.get('query') or product or ''}".rstrip(" ·")
        return {"kind": "news", "title": title[:40], "rows": rows[:6],
                "truncated": len(rows) > 6, "warnings": [],
                "empty": None if rows else "最近没有相关消息", "sources": ["调研 · 消息面（第三方报道）"],
                "actions": [ui_action("打开新闻", {"news": {"tab": "news"}})]}
    events = [event for event in data.get("events") or [] if isinstance(event, dict)]
    screening = "events" in data  # the AI-screened view: its events are authoritative, even when empty
    if events:
        for event in events:
            ai: dict[str, Any] = event["ai"] if isinstance(event.get("ai"), dict) else {}
            stance = str(ai.get("direction") or "")
            reports = event.get("reports")
            meta = [local_clock(event.get("publishedAt") or ""), _TIER.get(str(event.get("tier") or ""), ""),
                    f"{reports} 家报道" if isinstance(reports, int) and reports > 1 else ""]
            rows.append(_row(str(event.get("title") or event.get("eventLabel") or ""),
                             secondary=" · ".join(part for part in meta if part) or None,
                             tag=(stance + (f" · {ai['strength']}" if ai.get("strength") else "")) if stance else None,
                             dir=_NEWS_DIRECTION.get(stance)))
    elif not screening:
        for item in data.get("items") or []:
            if not isinstance(item, dict) or not (item.get("title") or item.get("text")):
                continue
            rows.append(_row(str(item.get("title") or item.get("text")), secondary=" · ".join(part for part in (
                local_clock(item.get("publishedAt") or ""), str(item.get("source") or "")) if part) or None))
    rows = [row for row in rows if row["primary"]]
    brief: dict[str, Any] = data["brief"] if isinstance(data.get("brief"), dict) else {}
    note = str(brief.get("take") or "")[:160] if brief.get("status") in {"ready", "stale"} else None
    pending = (data.get("screen") or {}).get("pending") if isinstance(data.get("screen"), dict) else None
    unavailable = str(data.get("status") or "ok") not in {"ok", "stale"}
    empty = ("快讯暂时读不到，不代表没有消息" if unavailable
             else f"AI 正在看新到的 {pending} 条，看完再列出" if screening and pending else "最近没有相关快讯")
    return {"kind": "news", "title": f"快讯 · {short_symbol(product)}" if product else "快讯", "rows": rows[:6],
            # more headlines than the card shows, or a source outage: the text keeps its length
            "truncated": len(rows) > 6 or unavailable,
            "note": note or None, "empty": None if rows else empty,
            # an AI take kept from an earlier screening says so on the card face, not only as a plain note
            "warnings": [STALE_BRIEF_WARNING] if note and brief.get("status") == "stale" else [],
            "sources": ["快讯（第三方，未经核实）"], "actions": [ui_action("打开新闻", {"news": {"tab": "flash"}})]}


def _trend_word(trend: Any) -> str:
    return {"up": "上行", "down": "下行"}.get(str(trend), "震荡")


def analysis_card(entries: list[dict[str, Any]]) -> dict[str, Any]:
    first = entries[0]
    symbol = str(first.get("symbol") or "")
    actions = [ui_action("看图表", {"symbol": symbol, "view": "trade"})] if symbol else []
    sources = [_source_line(f"走势分析 {short_symbol(entry.get('symbol'))}（{entry.get('barCount')} 根 "
                            f"{duration_label(entry.get('durationSeconds'))} K 线）", entry) for entry in entries][:8]
    if len(entries) == 1:
        indicators: dict[str, Any] = first["indicators"] if isinstance(first.get("indicators"), dict) else {}
        figures = [_figure("区间涨跌", signed(first.get("periodReturnPercent"), 2, "%"), dir=direction(first.get("periodReturnPercent"))),
                   _figure("振幅", f"{number(first.get('rangePercent'), 2)}%"),
                   _figure("MA5", number(first.get("sma5"))), _figure("MA20", number(first.get("sma20")))]
        if first.get("volumeRatio") is not None:
            figures.append(_figure("量比", number(first.get("volumeRatio"))))
        if first.get("openInterestChange") is not None:
            figures.append(_figure("持仓变化", signed(first.get("openInterestChange"), 0), unit="手",
                                   dir=direction(first.get("openInterestChange"))))
        rows = []
        for key, label in (("rsi", "RSI"), ("cci", "CCI"), ("atr", "ATR"), ("boll", "布林")):
            item: dict[str, Any] | None = indicators[key] if isinstance(indicators.get(key), dict) else None
            if not item or not item.get("reading"):
                continue
            value = item.get("value")
            rows.append(_row(label, secondary=str(item["reading"]), value=number(value) if value is not None else None))
        return {"kind": "analysis", "symbol": symbol,
                "title": f"走势 · {short_symbol(symbol)} · {duration_label(first.get('durationSeconds'))} · {_trend_word(first.get('trend'))}",
                "figures": figures[:6], "rows": rows, "sources": sources, "actions": actions}
    rows = [_row(f"{short_symbol(entry.get('symbol'))} · {duration_label(entry.get('durationSeconds'))}",
                 secondary=f"MA5 {number(entry.get('sma5'))} / MA20 {number(entry.get('sma20'))}"
                           + (f" · 量比 {number(entry.get('volumeRatio'))}" if entry.get("volumeRatio") is not None else ""),
                 value=signed(entry.get("periodReturnPercent"), 2, "%"), dir=direction(entry.get("periodReturnPercent")),
                 tag=_trend_word(entry.get("trend"))) for entry in entries]
    return {"kind": "analysis", "symbol": symbol, "title": f"走势 · {len(entries)} 组", "rows": rows[:12],
            "truncated": len(rows) > 12,
            "sources": sources, "actions": actions}


def quotes_card(data: dict[str, Any]) -> dict[str, Any]:
    rows = []
    by_interest = data.get("sort") == "openInterest"
    for item in data.get("rows") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("instrumentName") or item.get("productName") or "")
        code = str(item.get("instrumentId") or short_symbol(item.get("symbol")))
        meta = [f"最新 {price_text(item['last'])}" if item.get("last") is not None else "",
                f"成交 {number(item['volume'], 0)}" if item.get("volume") is not None else "",
                # sorted by open interest: the figure it is sorted by is on the row (#878 review)
                f"持仓 {number(item['openInterest'], 0)}" if by_interest and item.get("openInterest") is not None else "",
                f"增仓 {signed(item['openInterestChange'], 0)}" if item.get("openInterestChange") is not None else ""]
        change = item.get("changePercent")
        rows.append(_row(f"{name} {code}".strip(), secondary=" · ".join(part for part in meta if part) or None,
                         value=signed(change, 2, "%") if change is not None else None, dir=direction(change)))
    order = "从小到大" if data.get("order") == "asc" else "从大到小"
    return {"kind": "quotes", "title": f"报价 · {_BOARD_SORT.get(str(data.get('sort')), '涨跌幅')}{order}",
            "rows": rows[:12], "truncated": len(rows) > 12, "empty": None if rows else "报价总表暂时没有数据",
            "sources": [_source_line("报价总表", data)], "actions": []}


def tool_data(output: str) -> dict[str, Any] | None:
    """The `data` of a TraderToolResult serialized in a tool's output text."""
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


# 字段键 (回答卡结构化第 1 步): {[合约.]字段[@周期][=取值]} — a formal grammar over ASCII keys, never Chinese wording.
_KEY = re.compile(r"(?:([A-Za-z]+\d{3,4}|[A-Za-z]+\.[A-Za-z]+\d{3,4})\.)?([A-Za-z][A-Za-z0-9]*)(?:@(\d+)([mhdw]))?(?:=([a-z]+))?")
_KEY_IN_TEXT = re.compile(r"\{([^{}]*)\}")
_FRAME_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
# What a correction message quotes back: the stretch of a template's own words around a digit ("5分钟", "MA20").
_DIGIT_FRAGMENT = re.compile(r"[^\s，。、；：,;:！？!?（）()“”\"]*[0-9０-９][^\s，。、；：,;:！？!?（）()“”\"]*")
# A reading word → the state keys that say it (for the correction message only; the check is LABEL_WORDS).
def _word_keys() -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for key, (_, kind, _, _) in STATES.items():
        for value, word in VALUE_WORDS[kind].items():
            if value != "nearflat":
                table.setdefault(word, []).append(f"{key}={value}")
    return table


_WORD_KEYS = _word_keys()
# The longest template a card line may be before rendering: the card's own limits apply to the rendered line, so a
# template is never cut inside a key (10-17: a cut "{bollPl" reached the card face).
_TEMPLATE_CHARS = 240


class TurnCards:
    """One turn's automatic cards: the payload per kind, merged across reads and republished under one
    cardId; plus what the turn read (for the answer card's numbers and its 依据)."""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.market: dict[str, Any] | None = None
        self.analysis: dict[tuple[str, int], dict[str, Any]] = {}
        self.news: dict[str, dict[str, Any]] = {}  # product (or query) → its news card as read, newest read last
        self.reads: list[tuple[str, str]] = []  # (tool, symbol) for the basis
        self.symbol: str | None = None
        # Qualitative readings (放量 / 增仓 / 当日涨跌 / 超买 / 趋势) per contract, and the catalog's names for
        # telling which product a sentence is about (set by the agent; the reads' own names otherwise).
        self.facts = Facts()
        self.conclusion_rejected = False  # the last answer_card call's conclusion was unsupported (claims or numbers)
        self.rejections: list[str] = []  # the last answer_card call's rejected keys and template lines, by code
        self.oldest_read: str | None = None
        self.read_warnings: list[str] = []
        # 接受链 (#754): an answer card counts — its conclusion becomes the turn's answer text, the voice line and
        # the history — only once it is shown (`accept`, after the broker took the event); a checked card that was
        # refused or never published leaves these as they were.
        self.answered = False
        self.conclusion: str | None = None
        self.spoken: str | None = None  # the shown card's conclusion as the voice reads it (no bracketed basis)
        self._pending_spoken: str | None = None
        # Successful reads whose facts no card shows (the answer text then keeps its length), and how many
        # there were when the answer card was shown (a later uncarded read is not on that card).
        self.uncarded = 0
        self.uncarded_at_answer = 0
        # Reads a card shows only in part — rows beyond the card, a stale / backup peer, peers beyond six, an
        # earlier ranking, a news product still being screened. No answer card shows the rest either, so the
        # text keeps its length and the answer card does not close the turn (#754: an answer card used to offset it).
        self.partial = 0
        self.boards = 0
        # The answer card's terminal shortcut (native_agent): this call's card reached the user and the turn read
        # at least one fact first.
        self.card_shown = False
        self.last_dropped = False
        # The turn's read cards (market / account / data), by cardId in the order first shown: the answer card
        # folds those shown before it into its 依据; `folded` is what the shown answer card folded.
        self.read_card_ids: list[str] = []
        self.folded: list[str] = []

    def note_card(self, card_id: Any) -> None:
        if isinstance(card_id, str) and card_id and card_id not in self.read_card_ids:
            self.read_card_ids.append(card_id)

    def record_read(self, tool: str, arguments: dict[str, Any], output: str) -> None:
        # Freshness of what the answer card may rest on: the oldest read time and any stale / backup read.
        data = tool_data(output) or {}
        named = str(arguments.get("symbol") or "")
        # A quote or K-line read is about the contract it was asked for, even when the result does not repeat it.
        self.facts.add({**data, "symbol": named} if "." in named and not data.get("symbol")
                       and tool in {"get_market_snapshot", "get_market_analysis"} else data,
                       quote=tool == "get_market_snapshot")
        captured = data.get("capturedAt") or data.get("fetchedAt")
        moment = _moment(captured)
        if moment is not None and (self.oldest_read is None or moment < _moment(self.oldest_read)):  # type: ignore[operator]
            self.oldest_read = moment.isoformat()
        warning = _STATUS_WARNINGS.get(str(data.get("dataStatus") or ""))
        if warning and warning not in self.read_warnings:
            self.read_warnings.append(warning)
        symbol = str(arguments.get("symbol") or arguments.get("product") or "")
        if (tool, symbol) not in self.reads:
            self.reads.append((tool, symbol))
        if tool in {"get_market_snapshot", "get_market_analysis", "get_chart_bars"} and "." in symbol:
            self.symbol = self.symbol or symbol

    @property
    def facts_read(self) -> bool:
        """Whether the turn read any fact a card can rest on (not just a contract search or the manual)."""
        return any(tool in SOURCE_LABELS for tool, _ in self.reads)

    def basis(self) -> dict[str, Any]:
        sources = []
        for tool, symbol in self.reads:
            label = SOURCE_LABELS.get(tool)
            if label:
                line = f"{label} {short_symbol(symbol)}".strip()
                if line not in sources:
                    sources.append(line)
        return {"at": self.oldest_read or now_iso(), "sources": sources[:8]}

    def market_card(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The turn's one quote card: the first contract read is the card, later ones ride as peers."""
        if self.market is None or str(self.market.get("symbol")) == str(payload.get("symbol")):
            peers = (self.market or {}).get("peers")
            self.market = {**payload, "cardId": f"market-{self.turn_id}", **({"peers": peers} if peers else {})}
        else:
            peers = [peer for peer in self.market.get("peers") or [] if peer["symbol"] != payload.get("symbol")]
            try:
                peer = {"symbol": str(payload["symbol"]), "name": str(payload.get("name") or payload["symbol"]),
                        "price": float(payload["price"]), "changePercent": float(payload["changePercent"])}
            except (KeyError, TypeError, ValueError):
                return self.market
            status = str(payload.get("dataStatus") or "")
            if status in _STATUS_WARNINGS:
                # A stale / backup peer says so on its row, and the text keeps its own warning (#878 review).
                peer["dataStatus"] = status
                self.partial += 1
            peers.append(peer)
            if len(peers) > 6:
                self.partial += 1  # the card keeps the latest six peers: the text keeps the earlier ones
            self.market = {**self.market, "peers": peers[-6:]}
        return self.market

    def account_card(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "cardId": f"account-{self.turn_id}"}

    def data_card(self, tool: str, arguments: dict[str, Any], data: dict[str, Any], *,
                  account_word: str) -> dict[str, Any] | None:
        kind = AUTO_DATA_TOOLS.get(tool)
        built: dict[str, Any] | None
        if kind == "positions":
            built = positions_card(data, account_word=account_word)
        elif kind == "orders":
            built = orders_card(data)
        elif kind == "news":
            if tool == "get_research_brief" and data.get("topic") != "news":
                return None
            product = str(arguments.get("product") or arguments.get("symbol") or "") or None
            built = news_card(data, product=product)
            if built is not None:
                news_key = short_symbol(data.get("query") or product or "")
                self.news.pop(news_key, None)
                self.news[news_key] = {
                    **built, "research": data.get("topic") == "news",
                    "warnings": [_STATUS_WARNINGS[status] for status in [str(data.get("dataStatus") or "")]
                                 if status in _STATUS_WARNINGS] + built["warnings"],
                    "at": str(data.get("capturedAt") or data.get("fetchedAt") or now_iso())}
                if len(self.news) > 1:
                    built = self._merged_news()
        elif kind == "analysis":
            key = (str(data.get("symbol") or ""), int(data.get("durationSeconds") or 0))
            self.analysis[key] = data
            built = analysis_card(list(self.analysis.values()))
        elif kind == "quotes":
            built = quotes_card(data)
            self.boards += 1
            if self.boards > 1:
                self.partial += 1  # the card shows the latest ranking only; the earlier one stays in the text
        else:
            return None
        if built is None:
            return None
        if built.pop("truncated", False):
            self.partial += 1  # rows beyond the card: the text keeps listing them
        sources = built.pop("sources")
        warnings = [_STATUS_WARNINGS[status] for status in [str(data.get("dataStatus") or "")] if status in _STATUS_WARNINGS]
        warnings += [item for item in built.pop("warnings", []) if item not in warnings]
        at = str(built.pop("at", None) or data.get("capturedAt") or data.get("fetchedAt") or now_iso())
        built.pop("research", None)
        return {"turnId": self.turn_id, "cardId": f"data-{kind}-{self.turn_id}", "note": None, "empty": None,
                "symbol": None, "figures": [], "rows": [], **built, "warning": "、".join(warnings[:2]) or None,
                "basis": {"at": at, "sources": sources}}

    def _merged_news(self) -> dict[str, Any]:
        """Several news reads in a turn as one card (#878 review, #754): newest read first; a headline two products
        share with the same take is one row naming both, with different takes it stays once per product; a product
        whose read shows no row (still being screened, unavailable) or rows beyond six mark the card truncated so the
        text keeps them; the sources, warnings, oldest read time, title and buttons cover every read."""
        merged: list[dict[str, Any]] = []
        labels: list[list[str]] = []
        index: dict[tuple[str, str | None], int] = {}
        truncated, empties, notes, warnings, sources = False, [], [], [], []
        for name, entry in reversed(list(self.news.items())):
            label = name or "全部"
            truncated = truncated or bool(entry.get("truncated"))
            if entry.get("empty") and not entry["rows"]:
                empties.append(f"{label}：{entry['empty']}")
            if entry.get("note"):
                notes.append(f"{label}：{entry['note']}")
            warnings += [item for item in entry["warnings"] if item not in warnings]
            sources += [item for item in entry["sources"] if item not in sources]
            for row in entry["rows"]:
                key = (row["primary"], row.get("tag"))
                if key in index:
                    labels[index[key]].append(label)
                    continue
                index[key] = len(merged)
                merged.append(row)
                labels.append([label])
        rows = [{**row, "secondary": "、".join(names) + (f" · {row['secondary']}" if row.get("secondary") else "")}
                for row, names in zip(merged, labels, strict=True)]
        kinds = {bool(entry["research"]) for entry in self.news.values()}
        prefix = "消息面与快讯 · " if len(kinds) > 1 else "消息面 · " if kinds == {True} else "快讯 · "
        actions = [ui_action(*action) for action in (_FLASH_ACTION, _RESEARCH_ACTION)] if len(kinds) > 1 \
            else [ui_action("打开新闻", (_RESEARCH_ACTION if kinds == {True} else _FLASH_ACTION)[1])]
        return {"kind": "news", "title": (prefix + "、".join(name or "全部" for name in self.news))[:40], "rows": rows[:6],
                "truncated": truncated or len(rows) > 6 or bool(rows and empties),
                "note": "；".join(notes)[:160] or None, "empty": None if rows else "；".join(empties)[:60] or "最近没有相关快讯",
                "warnings": warnings, "sources": sources,
                "at": min((entry["at"] for entry in self.news.values()), key=lambda at: _moment(at) or datetime.max.replace(tzinfo=UTC)),
                "actions": actions}

    def _lookup(self, key: str, default: str) -> tuple[tuple[str, str, str, int, str | None] | None, tuple[str, str]]:
        """A field key against the 读数表: ((contract, key, field, timeframe, asserted value), ("", "")) or
        (None, (rejection code, what to tell the model)). Every message is made from the table, never parsed text."""
        match = _KEY.fullmatch(key.strip())
        if match is None:
            return None, ("ref_syntax", "键写成 {[合约.]字段[@周期][=取值]}，如 {day}、{j2701.volumeState@1d=down}")
        code, name, count, unit, asserted = match.groups()
        facts = self.facts
        symbols = sorted({symbol for symbol, _, _ in facts.values} | facts.pre_open)
        symbol = default if not code else next((item for item in symbols if code.lower() in {
            item.lower(), short_code(item).lower()}), "")
        if not symbol:
            return None, ("ref_symbol", f"{f'本轮没读 {code} 的数据' if code else '键没写合约'}；"
                                        f"读过：{'、'.join(map(short_code, symbols)) or '无'}")
        field = STATES[name][2] if name in STATES else name
        read = {item: duration for item_symbol, item, duration in facts.values if item_symbol == symbol}
        if field not in FIELDS and name not in STATES:
            return None, ("ref_field", f"没有字段 {name}；{short_code(symbol)} 本轮可用：{'、'.join(sorted(read)) or '无'}")
        frames = sorted(duration for item_symbol, item, duration in facts.values if item_symbol == symbol and item == field)
        wanted = int(count) * _FRAME_SECONDS[unit] if count and field not in QUOTE_FIELDS else None
        if field == "day" and not frames and symbol in facts.pre_open:
            return None, ("ref_preopen", f"{short_code(symbol)} 本交易日还没有成交，没有当日涨跌")
        if not frames or (wanted is not None and wanted not in frames):
            what = f"{duration_label(wanted)}的 {name}" if wanted else name
            return None, ("ref_missing", f"{short_code(symbol)} 本轮没有 {what}；可用：{'、'.join(sorted(read)) or '无'}")
        if wanted is None and len(frames) > 1:
            return None, ("ref_ambiguous", f"{short_code(symbol)} 的 {name} 读了{'、'.join(map(duration_label, frames))}，"
                                           "用 @5m / @1d 写明周期")
        duration = frames[0] if wanted is None else wanted
        if asserted:  # 定性键的 =取值, or =up / =down on a number: checked against the reading
            kind = STATES[name][1] if name in STATES else ""
            choices = list(VALUE_WORDS[kind]) if kind else ["up", "down"]
            if asserted not in choices:
                return None, ("ref_value", f"{name} 的取值只有 {'/'.join(choices)}")
            value = facts.values[(symbol, field, duration)]
            if asserted not in (facts.state(symbol, name, duration) if kind else {direction(value)}):
                actual = (f"{KIND_WORDS[kind]}是{'、'.join(VALUE_WORDS[kind][item] for item in sorted(facts.state(symbol, name, duration)))}"
                          f"（{facts.detail(symbol, name, duration)}）" if kind else f"{name} 是 {number(value)}")
                return None, ("ref_mismatch", f"与本轮数据相反：{short_code(symbol)} {actual}，不是“{VALUE_WORDS[kind][asserted] if kind else asserted}”")
        return (symbol, name, field, duration, asserted), ("", "")

    def _render(self, ref: tuple[str, str, str, int, str | None], main: str) -> tuple[dict[str, Any], str, str]:
        """A looked-up key as (a figure cell, the card text a template shows in its place, the spoken text: no
        bracketed basis, a move said as 涨 / 跌)."""
        symbol, name, field, duration, asserted = ref
        product = re.sub(r"\d+$", "", self.facts.names.get(symbol, "")).strip() or short_code(symbol)
        frame = f"{duration_label(duration)} " if duration else ""
        prefix = ("" if symbol == main else product + " ") + frame
        basis = None
        if field in FIELDS:
            shown, fmt, _ = FIELDS[field]
            value = self.facts.values[(symbol, field, duration)]
            number_text = (signed(value, 2, "%") if fmt == "pct" else signed(value, 0) if fmt == "signed"
                           else f"{number(value, 2)}%" if fmt == "range" else number(value, 0 if fmt == "lots" else 2))
            unit = "手" if fmt in {"signed", "lots"} else None
            if name not in STATES:
                move = direction(value)
                spoken = (f"{frame}{shown.removesuffix('涨跌')}{'涨' if move == 'up' else '跌' if move == 'down' else '持平'}"
                          + (f" {abs(round(value, 2)):.2f}%" if move else "") if fmt == "pct" else f"{frame}{shown} {number_text}{unit or ''}")
                return (_figure(prefix + shown, number_text, unit=unit, dir=move if fmt in {"pct", "signed"} else None),
                        f"{frame}{shown} {number_text}{unit or ''}", spoken)
            basis = f"{shown} {number_text}" if len(shown) + len(number_text) < 8 else number_text
        card_name, kind = STATES[name][:2]
        value = asserted or self._state_value(symbol, name, duration)
        word, detail = VALUE_WORDS[kind][value], self.facts.detail(symbol, name, duration)
        return (_figure(prefix + card_name, word, unit=basis, dir=value if kind in {"day", "period"} and value in {"up", "down"} else None),
                f"{word}（{detail if word not in detail else duration_label(duration) or detail}）", word)

    def _state_value(self, symbol: str, name: str, duration: int) -> str:
        """The value a state key reads (持平 before its sign, never 基本持平 alone)."""
        values = self.facts.state(symbol, name, duration)
        return "flat" if "flat" in values else sorted(values - {"nearflat"})[0]

    def _template(self, line: str, default: str, keys: set[tuple[str, str, int]], *, words: bool = True,
                  column: bool = False, omit: frozenset[tuple[str, str, int]] = frozenset(), named: bool = False,
                  ) -> tuple[tuple[str, str] | None, list[tuple[str, str]]]:
        """A template (方案 C): ((card text, spoken text), []) or (None, every (rejection code, why) it has, so one
        correction fixes them all). Its keys are rendered from the 读数表; its own words are checked for two things
        only — no number, and (`words`) no reading word. `column`: every key is the column's contract; `named`: the
        card shows two contracts, so a key without one is ambiguous; `omit`: keys the card already shows render as
        nothing."""
        problems: list[tuple[str, str]] = []
        rest = _KEY_IN_TEXT.sub(" ", line)
        digits = list(dict.fromkeys(_DIGIT_FRAGMENT.findall(rest)))
        if digits:
            problems.append(("text_digit", f"{'、'.join(f'“{item}”' for item in digits[:3])}里有数字；数字、周期和均线名"
                                           "（5 分钟、MA20）只能写进键，如 {ma20}、{volRatio@5m}"))
        if words:
            seen = rest
            for word in reversed(LABEL_WORDS):  # longest first: 多头排列 is one word, not 多头排列 and 排列
                if word in seen:
                    seen = seen.replace(word, " ")
                    hint = "或".join(f"{{{key}}}" for key in _WORD_KEYS.get(word, ())) or "带取值的定性键"
                    problems.append(("text_label", f"“{word}”是读数说法，写成 {hint}，或换个不是读数的说法"))
        card, spoken, used = [], [], []
        for match in _KEY_IN_TEXT.finditer(line):
            key = match.group(1).strip()
            ref, problem = self._lookup(key, default)
            if ref is not None and named and not _KEY.fullmatch(key).group(1):  # type: ignore[union-attr]
                problem = ("ref_symbol", f"这张卡有两个合约，{{{key}}} 要写明合约，如 {{{short_code(ref[0])}.{key}}}")
            elif ref is not None and ref[1] in STATES and not ref[4] and rest.strip():
                # Words around a state key say something about its value, so it is written; a line that is the key
                # alone (a compare row "{volumeState}") says nothing of its own and shows the reading, like a cell.
                value = self._state_value(ref[0], ref[1], ref[3])
                problem = ("ref_value", f"{{{key}}} 在句子里要写取值：本轮是{VALUE_WORDS[STATES[ref[1]][1]][value]}，"
                                        f"写 {{{key}={value}}}")
            elif ref is not None and column and ref[0] != default:
                problem = ("ref_column", f"这一栏是 {short_code(default)}，键写了 {short_code(ref[0])}")
            if ref is None or problem[0]:
                problems.append(problem)
                continue
            used.append((ref[0], ref[2], ref[3]))
            shown = ("", "") if used[-1] in omit else self._render(ref, default)[1:]
            card.append(shown[0])
            spoken.append(shown[1])
        if problems:
            return None, problems
        keys.update(used)  # only a line that is shown shows its keys
        return (_filled(line, card), _filled(line, spoken)), []

    def answer_card(self, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str | None]:
        """The `answer_card` call as a card payload: (card, dropped cells, error). Every reading on it is a field key
        into this turn's 读数表 that the Runtime renders (结构化第 1+2 步); the model's own words carry no number."""
        def text(value: Any, limit: int) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

        self.rejections = []
        self.conclusion_rejected = False
        title = text(arguments.get("title"), 24)
        raw_figures = arguments.get("figures") or []
        source = text(arguments.get("conclusion"), _TEMPLATE_CHARS)
        if not title or not source:
            return None, [], "answer_card 需要 title 与 conclusion。"
        if not isinstance(raw_figures, list) or any(not isinstance(item, str) for item in raw_figures):
            return None, [], ("answer_card 的 figures 是字段键的列表，如 [\"day\", \"volumeState\", \"j2701.day\"]，"
                              "值、单位和方向由 Runtime 按本轮读数填；改好再出卡。")
        symbols = {item for item, _, _ in self.facts.values} | self.facts.pre_open
        named = text(arguments.get("symbol"), 32).lower()
        # The chart button opens a contract this turn read, the whole code (SHFE.au261 is not SHFE.au2612, #878 review).
        symbol = next((item for item in [name for _, name in self.reads if "." in name] + sorted(symbols)
                       if item.lower() == named), None) or self.symbol or ""
        main = next((item for item in sorted(symbols) if item.lower() == symbol.lower()), None) \
            or (next(iter(symbols)) if len(symbols) == 1 else symbol)  # the 读数表's spelling of it

        def contract(code: str) -> str:
            return next((item for item in symbols if code.lower() in {item.lower(), short_code(item).lower()}), "")

        raw = arguments.get("compare")
        sides_in = [raw["left"], raw["right"]] if isinstance(raw, dict) and isinstance(raw.get("left"), dict) \
            and isinstance(raw.get("right"), dict) else []
        columns = [contract(text(side.get("symbol"), 32)) if text(side.get("symbol"), 32) else main for side in sides_in]
        # 两个合约的卡 (10-17): with two contracts on the card a key without one could be either one — the sentences
        # (conclusion, reasons, risk) name it, so 焦炭的风险句 never shows 铁矿's MA20.
        sentences = [source, text(arguments.get("risk"), _TEMPLATE_CHARS),
                     *(text(item, _TEMPLATE_CHARS) for item in (arguments.get("reasons") or [])[:3])]
        coded = {contract(match.group(1)) for line in sentences for key in _KEY_IN_TEXT.findall(line)
                 if (match := _KEY.fullmatch(key.strip())) is not None and match.group(1)}
        # The cells name contracts too (“j2701.day” beside the main one, #932 F1): a card whose cells show two contracts
        # is a two-contract card, and a bare key in its sentences could be either. Only a cell that renders counts — a
        # key with no reading is dropped and never puts a second contract on the card.
        coded |= {ref[0] for item in raw_figures[:6] if (ref := self._lookup(item.strip().strip("{}"), main)[0]) is not None}
        two = len({item for item in columns if item}) == 2 or bool(coded - {main, ""})
        if re.search(r"[0-9０-９]", title):
            # A title's own number has no reading behind it: the Runtime names the card instead (no correction round).
            self.rejections.append("text_title")
            shown_contracts = [item for item in dict.fromkeys(columns if len(set(columns)) == 2 else [main]) if item]
            title = "与".join(re.sub(r"\d+$", "", self.facts.names.get(item, "") or short_code(item)).strip()
                             for item in shown_contracts)[:24] or "回答"
        # The conclusion is the answer text, the voice line and the history when the card closes the turn: one that
        # does not render — a key with no reading or a contradicted value, a number or a reading word of its own —
        # refuses the card and goes back to the model with every problem it has (#754 接受链; 10-17: one round).
        rendered, problems = self._template(source, main, set(), named=two)
        if rendered is not None and len(rendered[0]) > 80:
            problems = [("text_long", f"填上读数后 {len(rendered[0])} 字，超过 80 字，少写一两个键")]
            rendered = None
        if rendered is None or not rendered[0]:
            self.conclusion_rejected = True
            problems = problems or [("text_empty", "没有内容")]
            self.rejections.extend(code for code, _ in problems)
            return None, [], f"answer_card 的结论要改：{'；'.join(dict.fromkeys(why for _, why in problems))}。其余照旧，改好再出卡。"
        conclusion, spoken = rendered
        dropped: list[str] = []

        def reject(what: Any, problems: list[tuple[str, str]]) -> None:
            self.rejections.append(problems[0][0])
            dropped.append(f"{what}：{problems[0][1]}")

        figures, figure_keys = [], []
        for item in raw_figures[:6]:
            ref, problem = self._lookup(item.strip().strip("{}"), main)
            if ref is None:
                reject(item, [problem])
                continue
            if (ref[0], ref[2], ref[3]) in figure_keys:
                continue  # one reading, one cell: day and dayMove are the same 当日涨跌 (10-17 两格重复)
            figures.append(self._render(ref, main)[0])
            figure_keys.append((ref[0], ref[2], ref[3]))
        compare, column_keys = None, set[tuple[str, str, int]]()
        if sides_in:
            sides = []
            for side, column in zip(sides_in, columns, strict=True):
                lines: list[str] = []
                here: set[tuple[str, str, int]] = set()
                if not column:
                    reject(text(side.get("symbol"), 32), [("ref_symbol", "这一栏的合约本轮没读")])
                for line in (side.get("lines") or [])[:3] if column else []:
                    # A reading an earlier line of the column shows is not shown again (one reading, one place) — and
                    # only a line that is kept shows its keys: a line refused for its rendered length, or too short to
                    # keep, never takes a key from a later line, a cell or a reason (#932 F2).
                    line_keys: set[tuple[str, str, int]] = set()
                    shown, problems = self._template(text(line, _TEMPLATE_CHARS), column, line_keys, column=True,
                                                     omit=frozenset(here))
                    if shown is not None and len(shown[0]) > 60:
                        shown, problems = None, [("text_long", "填上读数后超过 60 字")]
                    if shown is None:
                        reject(line, problems)
                    elif len(re.sub(r"[\W_]", "", shown[0])) >= 2:
                        lines.append(shown[0])
                        here |= line_keys
                column_keys |= here
                fallback = re.sub(r"\d+$", "", self.facts.names.get(column, "")).strip() or short_code(column)
                sides.append({"title": text(side.get("title"), 20) or fallback[:20] or "—", "lines": lines})
            if any(side["lines"] for side in sides):
                compare = {"left": sides[0], "right": sides[1]}
            else:
                column_keys = set()  # no column is shown: nothing it would have shown is taken out of the cells
        if compare is not None and all(side["lines"] for side in compare.values()):
            # A compare card shows its two columns: a cell they already show goes (按键去重), a cell only it has stays.
            figures = [figure for figure, key in zip(figures, figure_keys, strict=True) if key not in column_keys]
            figure_keys = [key for key in figure_keys if key not in column_keys]
        elif compare is not None and figures:
            compare, column_keys = None, set()  # one side lost its lines: the cells say it better than a half-empty compare
        # A reason keeps the cause: a key the cells or the columns already show is not said again (按键去重).
        shown_keys = frozenset(figure_keys) | frozenset(column_keys)
        reasons: list[str] = []
        for item in (arguments.get("reasons") or [])[:3]:
            shown, problems = self._template(text(item, _TEMPLATE_CHARS), main, set(), omit=shown_keys, named=two)
            if shown is not None and len(shown[0]) > 80:
                shown, problems = None, [("text_long", "填上读数后超过 80 字")]
            if shown is None:
                reject(item, problems)
            elif len(re.sub(r"[\W_]", "", shown[0])) >= 2 and shown[0] not in reasons:
                reasons.append(shown[0])
        # The risk line is a condition by its field, not by a 若: it may name a reading word, never a number of its own.
        risk, problems = self._template(text(arguments.get("risk"), _TEMPLATE_CHARS), main, set(), words=False, named=two)
        if risk is not None and len(risk[0]) > 80:
            risk, problems = None, [("text_long", "填上读数后超过 80 字")]
        if risk is None:
            reject(arguments.get("risk"), problems)
        actions = ([ui_action("看图表", {"symbol": symbol, "view": "trade"})] if symbol else []) \
            + [ask_action("问 AI 怎么办", f"{title}现在该怎么办？")]
        self._pending_spoken = spoken
        return ({"turnId": self.turn_id, "cardId": f"answer-{self.turn_id}", "title": title, "conclusion": conclusion,
                 "figures": figures, "compare": compare, "reasons": reasons, "risk": risk[0] if risk and risk[0] else None,
                 "symbol": symbol or None, "basis": self.basis(), "actions": actions,
                 "warning": "、".join(self.read_warnings) or None, "folded": list(self.read_card_ids)}, dropped, None)

    def accept(self, card: dict[str, Any]) -> None:
        """The answer card reached the user (the broker took it): from here its conclusion is the turn's answer —
        the text when the card closes the turn, the voice line (`spoken`, without the bracketed basis), the history —
        and the reads before it are on it."""
        self.answered = True
        self.uncarded_at_answer = self.uncarded
        self.conclusion = str(card["conclusion"])
        self.spoken = self._pending_spoken
        self.folded = list(card.get("folded") or [])


def _filled(line: str, pieces: list[str]) -> str:
    """A template with its keys replaced by `pieces`, without the punctuation an omitted key leaves at either end."""
    rest = iter(pieces)
    text = _KEY_IN_TEXT.sub(lambda _: next(rest), line)
    return re.sub(r"^[\s，,；;、]+|[\s，,；;、]+$", "", re.sub(r"([，,；;、])(?:\s*[，,；;、])+", r"\1", text))


# --- the answer text ---------------------------------------------------------------------------------

_TIME = re.compile(r"\d{1,2}[:：]\d{2}(?:[:：]\d{2})?")
# Dates only (2026-10-14, 10月14日, a standalone 10-14) — never a decimal like 3072.5 or a range like 3072-3100.
_DATE = re.compile(r"(?:19|20)\d{2}\.\d{1,2}\.\d{1,2}(?!\d)|(?:19|20)\d{2}[-/年]\d{1,2}(?!\d)(?:[-/月]\d{1,2}(?!\d)[日号]?)?|\d{1,2}月\d{1,2}[日号]?|(?<![\d.])\d{1,2}-\d{1,2}(?![\d.])")
_CODE = re.compile(r"[A-Za-z]{1,3}\d{3,4}")
_CLAIM = re.compile(r"\d+(?:\.\d+)?\s*[%％]|\d{3,}|\d+\.\d+|\d+\s*(?:手|笔|张|元|点|跳|个合约)")


def has_numeric_claim(text: str) -> bool:
    """Whether an answer states figures: a percentage, a decimal or a number of three or more digits,
    ignoring clock times, dates and contract codes (rb2701)."""
    stripped = _CODE.sub(" ", _DATE.sub(" ", _TIME.sub(" ", text or "")))
    return _CLAIM.search(stripped) is not None


_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
CONCLUSION_SENTENCES = 2
CONCLUSION_CHARS = 160


def conclusion_text(text: str) -> str:
    """The answer cut to its first one or two sentences when a card shows the details: markdown markers
    go, a table never survives (a text that is nothing but a table cuts to "", #878 review — the caller says the
    card's conclusion instead), the cut lands on a sentence end."""
    sentences: list[str] = []
    for line in (text or "").split("\n"):
        line = re.sub(r"\*\*|`", "", line)  # bold and code marks only: identifiers keep their underscores
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)]|#{1,6})\s*", "", line).strip()
        if not line or line.startswith("|"):
            continue
        for match in _SENTENCE.finditer(line):
            sentence = match.group(0).strip()
            if sentence:
                sentences.append(sentence)
        if len(sentences) >= CONCLUSION_SENTENCES:
            break
    short = "".join(sentences[:CONCLUSION_SENTENCES])
    if len(short) > CONCLUSION_CHARS:
        cut = short[:CONCLUSION_CHARS]
        end = max(cut.rfind("。"), cut.rfind("；"), cut.rfind("，"))
        short = cut[:end + 1] if end > CONCLUSION_CHARS // 2 else cut
    return short
