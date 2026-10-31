"""策略编辑卡 (2026-10-08): the one card that creates or edits a strategy, replacing the AI 引导设计
(voice / chat guided design) and 我的策略's ask box.

What lives here, all deterministic:

* ``habit_prefill`` — 按我的习惯: the fields that follow from the account's own closed trades by counting
  alone (no model): the contract (the most traded product's main contract), the direction (one side
  ≥ 70%), the stop in ticks (the median exit distance of the losing trades of that product), 收盘前平仓
  (≥ 5 trades and none held overnight), the entry session (≥ 70% of entries in one continuous session)
  and the lots per entry (the mode). Below ``MIN_TRADES`` closed trades in the last ``WINDOW_TRADING_DAYS``
  trading days nothing is prefilled. Every field carries one sentence of basis and the trade ids behind
  it. The entry conditions are never prefilled (归纳开仓规则 is the next phase).
* ``editor_utterance`` — the routing words: with the card open, a sentence about the strategy's own
  fields ("止损改成 20 跳", "每次开两手", "加一个 MACD 金叉") goes to the card's 补一句, never to an
  order route (a real order names its contract: "螺纹买两手").
* ``register_retired_guide_routes`` — the guided design's endpoints answer 410: the 1.7.2 desktop client
  still calls them, and the REST contract (contracts/openapi-baseline.json) keeps every path until the
  next desktop release.
"""
from __future__ import annotations

import re
from collections import Counter
from statistics import median
from typing import Any

from fastapi import Body, HTTPException

from .instruments import product_scope
from .trading_calendar import local_time, naive_trading_day, previous_trading_day

WINDOW_TRADING_DAYS = 20
MIN_TRADES = 5
SIDE_SHARE = 0.7          # 方向：一侧占比 ≥ 70% 才填
SESSION_SHARE = 0.7       # 开仓时段：≥ 70% 落在同一连续时段才填
MIN_LOSERS = 3            # 止损跳数：该品种至少 3 笔亏损单才取中位数
CLOSE_BEFORE_END_MINUTES = 5
MAX_EVIDENCE = 30
NOTICE = "开仓条件不预填：从你的交易归纳开仓规则是下一期的功能，现在要你自己选。"
# The continuous sessions an entry can fall in (Beijing time of the opening fill) and the window the plan
# gets (`entryWindows`, judged on the signal bar's close; a window may cross midnight).
SESSIONS = (("night", "夜盘", "21:00", "02:30"), ("morning", "上午", "09:00", "11:30"),
            ("afternoon", "下午", "13:00", "15:00"))
DIRECTION_WORDS = {"long": "做多", "short": "做空"}


def _session_of(timestamp_ns: int) -> str:
    moment = local_time(timestamp_ns)
    minute = moment.hour * 60 + moment.minute
    if minute >= 20 * 60 or minute < 3 * 60:
        return "night"
    return "morning" if minute < 12 * 60 else "afternoon"


def _product_key(symbol: str) -> str:
    """SHFE.rb2701 → SHFE.rb, CME.GCEZ26 → CME.GCE (the exchange and product, months merged)."""
    return product_scope(symbol)


def _pnl(item: dict[str, Any]) -> float:
    value = item.get("net")
    return float(value if value is not None else (item.get("gross") or 0.0))


def _entries(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round trips grouped back into entries: one opening order (or fill) closed by one or more fills is
    one trade — its lots are the order's, its exit the volume-weighted close."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in rounds:
        key = (str(item.get("symbol")), str(item.get("openOrderId") or item.get("openTradeId") or item.get("id")))
        groups.setdefault(key, []).append(item)
    entries = []
    for (symbol, _), items in groups.items():
        volume = sum(int(item.get("volume") or 0) for item in items)
        if volume <= 0:
            continue
        open_price = sum(float(item["openPrice"]) * int(item["volume"]) for item in items) / volume
        close_price = sum(float(item["closePrice"]) * int(item["volume"]) for item in items) / volume
        entries.append({
            "id": str(items[0]["id"]), "roundIds": [str(item["id"]) for item in items], "symbol": symbol,
            "direction": str(items[0]["direction"]), "volume": volume,
            "openTime": min(int(item["openTime"]) for item in items),
            "closeTime": max(int(item["closeTime"]) for item in items),
            "openPrice": round(open_price, 6), "closePrice": round(close_price, 6),
            "net": round(sum(_pnl(item) for item in items), 2),
        })
    entries.sort(key=lambda entry: (entry["openTime"], entry["id"]))
    return entries


def _evidence(entries: list[dict[str, Any]]) -> list[str]:
    return [identifier for entry in entries for identifier in entry["roundIds"]][:MAX_EVIDENCE]


def _main_contract(product: str, entries: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    """The product's main contract from the catalog; the latest traded contract when the catalog has none."""
    exchange, _, product_id = product.partition(".")
    same = [item for item in items if str(item.get("exchangeId", "")).upper() == exchange.upper()
            and str(item.get("productId", "")) == product_id]
    main = next((item for item in same if item.get("isMain") is True), None)
    if main is None:
        latest = max(entries, key=lambda entry: entry["openTime"])["symbol"]
        main = next((item for item in items if item.get("symbol") == latest), {"symbol": latest})
    return main


def _product_name(item: dict[str, Any], product: str) -> str:
    return re.sub(r"\d+$", "", str(item.get("instrumentName") or "")) or product.split(".")[-1]


def _price_tick(symbol: str, product: str, items: list[dict[str, Any]]) -> float | None:
    exact = next((item for item in items if item.get("symbol") == symbol and item.get("priceTick")), None)
    if exact is None:
        exchange, _, product_id = product.partition(".")
        exact = next((item for item in items if str(item.get("exchangeId", "")).upper() == exchange.upper()
                      and str(item.get("productId", "")) == product_id and item.get("priceTick")), None)
    try:
        tick = float(exact["priceTick"]) if exact else 0.0
    except (TypeError, ValueError):
        return None
    return tick if tick > 0 else None


def window_start(today: str) -> str:
    """The first of the last WINDOW_TRADING_DAYS trading days ending `today` (weekdays; holidays are not
    known here, so a holiday week reaches a few days further back)."""
    day = today
    for _ in range(WINDOW_TRADING_DAYS - 1):
        day = previous_trading_day(day)
    return day


def habit_prefill(rounds: list[dict[str, Any]], *, catalog_items: list[dict[str, Any]], now_ns: int) -> dict[str, Any]:
    """按我的习惯: the prefill of the 策略编辑卡 from the account's closed round trips (the trade review's
    `trades`). Pure. Returns {eligible, reason?, window, sample, summary, fields, trades, notice}; each field
    is {value, basis, tradeIds}."""
    today = naive_trading_day(now_ns)
    start = window_start(today)
    entries = [entry for entry in _entries(rounds) if start <= naive_trading_day(entry["closeTime"]) <= today]
    window = {"tradingDays": WINDOW_TRADING_DAYS, "from": start, "to": today}
    trades = [{key: entry[key] for key in ("id", "symbol", "direction", "volume", "openTime", "closeTime",
                                           "openPrice", "closePrice", "net")} for entry in reversed(entries)]
    sample = {"trades": len(entries), "minTrades": MIN_TRADES}
    if len(entries) < MIN_TRADES:
        return {"eligible": False, "window": window, "sample": sample, "fields": {}, "trades": trades,
                "reason": f"近 {WINDOW_TRADING_DAYS} 个交易日已平仓成交 {len(entries)} 笔，不到 {MIN_TRADES} 笔，不按习惯预填。",
                "notice": NOTICE}
    total = len(entries)
    fields: dict[str, Any] = {}

    products = Counter(_product_key(entry["symbol"]) for entry in entries)
    product, product_count = sorted(products.items(), key=lambda item: (-item[1], item[0]))[0]
    mine = [entry for entry in entries if _product_key(entry["symbol"]) == product]
    main = _main_contract(product, mine, catalog_items)
    name = _product_name(main, product)
    code = str(main["symbol"]).split(".")[-1]
    fields["symbol"] = {"value": main["symbol"], "label": f"{name} 主力（{code}）" if main.get("isMain") else f"{name}（{code}）",
                        "basis": f"{total} 笔里 {product_count} 笔是{name}", "tradeIds": _evidence(mine)}

    for side, word in DIRECTION_WORDS.items():
        same_side = [entry for entry in entries if entry["direction"] == side]
        if len(same_side) / total >= SIDE_SHARE:
            fields["direction"] = {"value": side, "basis": f"{total} 笔里 {len(same_side)} 笔{word}",
                                   "tradeIds": _evidence(same_side)}

    losers = [entry for entry in mine if entry["net"] < 0]
    tick = _price_tick(str(main["symbol"]), product, catalog_items)
    if tick and len(losers) >= MIN_LOSERS:
        ticks = round(median(abs(entry["closePrice"] - entry["openPrice"]) / tick for entry in losers))
        if ticks >= 1:
            fields["stopLossTicks"] = {"value": int(ticks),
                                       "basis": f"{name} {len(losers)} 笔亏损单出场中位 {int(ticks)} 跳",
                                       "tradeIds": _evidence(losers)}

    if all(naive_trading_day(entry["openTime"]) == naive_trading_day(entry["closeTime"]) for entry in entries):
        fields["closeBeforeEndMinutes"] = {"value": CLOSE_BEFORE_END_MINUTES,
                                           "basis": f"{total} 笔都没有留隔夜", "tradeIds": _evidence(entries)}

    sessions = Counter(_session_of(entry["openTime"]) for entry in entries)
    session, session_count = sorted(sessions.items(), key=lambda item: (-item[1], item[0]))[0]
    if session_count / total >= SESSION_SHARE:
        _, label, begin, end = next(item for item in SESSIONS if item[0] == session)
        inside = [entry for entry in entries if _session_of(entry["openTime"]) == session]
        fields["entryWindow"] = {"value": {"start": begin, "end": end},
                                 "basis": f"{total} 笔里 {session_count} 笔在{label}（{begin}–{end}）开仓",
                                 "tradeIds": _evidence(inside)}

    lots = Counter(entry["volume"] for entry in entries)
    size, size_count = sorted(lots.items(), key=lambda item: (-item[1], item[0]))[0]
    fields["lots"] = {"value": int(size), "basis": f"{total} 笔里 {size_count} 笔每次 {size} 手",
                      "tradeIds": _evidence([entry for entry in entries if entry["volume"] == size])}

    summary = (f"依据：近 {WINDOW_TRADING_DAYS} 个交易日 {total} 笔已平仓成交"
               + (f"（{name} {product_count} 笔）" if product_count != total else f"（都是{name}）") + "。")
    return {"eligible": True, "window": window, "sample": sample, "summary": summary, "fields": fields,
            "trades": trades, "notice": NOTICE}


# -- routing words: what the open card's 补一句 takes ---------------------------------------------------

_PUNCTUATION = re.compile(r"[\s，。,!！?？、~～…·：；;“”\"'「」《》()（）]")
# The strategy's own sizes and protection ("每次开两手", "止损改成 20 跳"): these outrank the order words — with
# voiceOrderConfirm=false a spoken order card would be confirmed at once — unless the sentence names a contract.
_PARAMETER_WORDS = re.compile(r"每次|每笔|每回|每单|手数|仓位|最多|止损|止盈|周期")
# Other words about the card's fields. "策略" alone is not one ("看看我的策略" is a panel command).
_FIELD_WORDS = re.compile(r"做多|做空|只做|双向|时段|夜盘|"
                          r"追踪|保本|加仓|分批|收盘前|隔夜|条件|跳|最长持仓|持仓满|持仓达到")
_LOTS = re.compile(r"(?:\d+|[一二两俩三四五六七八九十]+)\s*手")
_CONTRACT_CODE = re.compile(r"[A-Za-z]{1,2}\d{3,4}")
# Named indicators or concrete conditions ("加一个 MACD 金叉", "收盘站上 20 日均线").
_RULE_WORDS = re.compile(r"均线|金叉|死叉|上穿|下穿|交叉|macd|kdj|kd|rsi|cci|adx|atr|ema|sar|boll|布林|肯特纳|唐奇安|"
                         r"通道|成交量|放量|缩量|量比|持仓量|增仓|减仓|超买|超卖|"
                         r"[0-9一二三四五六七八九十百]+(?:日|根|天)(?:线|均线|高点|低点|新高|新低)|"
                         r"昨(?:日|天)?(?:高|低|收)|开盘价|前高|前低|新高|新低|突破|"
                         r"roc|trix|威廉|乖离|mfi|bbi|阳线|阴线|影线|均价线|vwap")


def _strip(text: str) -> str:
    return _PUNCTUATION.sub("", text or "")


def names_contract(text: str, product_names: list[str] | None) -> bool:
    """Whether the sentence names a contract: a code (rb2610) or a catalog product (螺纹 / 沪金)."""
    from .voice_intents import PRODUCT_ALIASES, match_product_word
    s = _strip(text)
    if _CONTRACT_CODE.search(s):
        return True
    names = list(product_names or [])
    return bool(match_product_word(s, names) or any(name and name in s for name in names)
                or any(alias in s and target in names for alias, target in PRODUCT_ALIASES.items()))


def editor_utterance(text: str, product_names: list[str] | None = None) -> bool:
    """With the 策略编辑卡 open: is this sentence the card's 补一句? A field or rule word ("止损改成 20 跳",
    "加一个 MACD 金叉"), or a size without any contract ("每次两手"). Interface commands, quote questions,
    the strategy-signal ask, opening the card itself and an order that names its contract keep their own
    routes. Pure."""
    from .message_context import strategy_signal_request
    from .order_intent_parse import book_words, states_order
    from .voice_intents import POSITION_SURFACE_NAMES, match_intent, match_quick_query
    s = _strip(text)
    if not s or len(s) > 200:
        return False
    if (match_intent(s) is not None or match_quick_query(s, product_names) is not None
            or strategy_signal_request(s)):
        return False
    # #738 审查: 「把仓位都关掉啦 / 全部仓位都关掉呗」是本轮清仓口语（#730 走 general），不是编辑卡
    # 的仓位字段——`仓位` 在 `_PARAMETER_WORDS` 里，须先按全平的说法（解析器的 `book_words`）退休。
    # 说法之外还有字段/条件语义的仍是策略规则的补一句：「收盘前所有仓位都平掉」「每次触发止损
    # 把仓位都关掉」不退休，沿原有 `_FIELD_WORDS`/`_RULE_WORDS`/`_PARAMETER_WORDS` 优先级。
    book = book_words(s)
    if book is not None and not (_FIELD_WORDS.search(s) or _RULE_WORDS.search(s.lower())
                                 or _PARAMETER_WORDS.search(POSITION_SURFACE_NAMES.sub("", book[0]) + book[1])):
        return False
    contract = names_contract(s, product_names)
    if _PARAMETER_WORDS.search(s) or (_LOTS.search(s) and not contract):
        return not (contract and states_order(s, product_names))
    if states_order(s, product_names):
        return False   # "帮我平仓" / "全平" stay orders
    return bool(_FIELD_WORDS.search(s) or _RULE_WORDS.search(s.lower()))


EDITOR_ASK_LINE = "好，我按这句改编辑卡，改到的地方会高亮，改完自动重算回测；满意了你点“存为草稿”。"
EDITOR_ASK_FAILED = "编辑卡没接上这句，请直接在卡里改，或者再说一次。"


def editor_open(window_state: dict[str, Any] | None) -> bool:
    card = (window_state or {}).get("strategyEditor")
    return isinstance(card, dict) and bool(card.get("open"))


def editor_open_line(changes: dict[str, Any], state: dict[str, Any] | None) -> str:
    """What the AI says after opening the card (语音 / 打字): where it is, what the habits prefill filled —
    or why it could not — and, when the user asked for a strategy from their trades, that the entry
    conditions are theirs to pick this phase."""
    raw = state.get("strategyEditor") if isinstance(state, dict) else None
    card: dict[str, Any] = raw if isinstance(raw, dict) else {}
    raw_prefill = card.get("prefill")
    prefill: dict[str, Any] = raw_prefill if isinstance(raw_prefill, dict) else {}
    asked_habits = (changes.get("strategyEditor") or {}).get("start") == "habits"
    # The phone's card is a full-screen page (2026-10-08); the desktop's sits in the right-hand panel.
    line = "我打开了策略编辑卡" if isinstance(state, dict) and state.get("mobileLayout") else "我在右边打开了策略编辑卡"
    if card.get("start") == "habits" and prefill.get("eligible"):
        count = len(card.get("aiFields") or []) or int(prefill.get("count") or 0)
        line += f"，按你的成交预填了 {count} 项，带 AI 标记，旁边写着依据" if count else "，按你的成交预填了几项"
    elif asked_habits and prefill.get("eligible") is False:
        line += "；" + str(prefill.get("reason") or f"近 {WINDOW_TRADING_DAYS} 个交易日成交不够 {MIN_TRADES} 笔，没法按习惯预填") + "先从空白或模板开始"
    line += "。"
    if asked_habits:
        line += "开仓条件要你自己选，从交易习惯自动归纳开仓规则是下一期的功能。"
    return line


# -- the retired guided design's endpoints ----------------------------------------------------------

GUIDE_RETIRED = "AI 引导设计已下线：新建或修改策略请用策略面板里的策略编辑卡（我的策略 → 新建策略）。"
RETIRED_GUIDE_ROUTES = frozenset({
    "POST /api/strategy-plans/guide", "GET /api/strategy-plans/guide", "DELETE /api/strategy-plans/guide",
    "POST /api/strategy-plans/guide/answer", "POST /api/strategy-plans/guide/undo",
    "POST /api/strategy-plans/guide/confirm", "POST /api/strategy-plans/guide/pause",
})


def register_retired_guide_routes(application) -> None:
    """410 for every endpoint of the retired guided design (2026-10-08). Kept only because an installed
    1.7.2 desktop client still calls them; the capability catalog never lists them (agent_api)."""

    def retired(**_: Any) -> None:
        raise HTTPException(410, GUIDE_RETIRED)

    async def no_body() -> None:
        retired()

    async def with_body(body: dict[str, Any] | None = Body(default=None)) -> None:
        retired()

    for route in sorted(RETIRED_GUIDE_ROUTES):
        method, path = route.split(" ", 1)
        handler = with_body if method == "POST" and path != "/api/strategy-plans/guide/pause" and not path.endswith("/undo") else no_body
        application.add_api_route(path, handler, methods=[method], deprecated=True,
                                  name=f"retired_guide_{method.lower()}_{path.rsplit('/', 1)[-1]}",
                                  description=GUIDE_RETIRED)
