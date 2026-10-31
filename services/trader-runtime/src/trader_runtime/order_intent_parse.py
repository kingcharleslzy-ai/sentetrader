"""一句话 → 下单意图：唯一解析器（docs/ORDER_INTENT_V2.md §2.2，意图重构第 2 步）。纯函数、无 I/O。

数字（手数、口语价格）、品种词与指代（“这个合约” = 当前图表）、否定 / 询问 / 建议 / 条件 / 时间 / 转述 / 同句撤回 /
改口（“不对，…”取最后的说法）/ 一句两单 / 答卡（确认、改单、有卡时的“算了”）都只在这里判。快速规则只对有把握
的句子给 `OrderIntent`，任何拿不准的成分返回 `Uncertain`——交给交易 AI 或追问，绝不猜。测试集
`tests/intent_corpus`（真实语句）强制“给对或不确定”：给错一句即失败。

所有者口径（2026-10-16）：八百二 = 820 限价（只对价格读省略尾数，手数“两百五”不猜）；市价 = 对价（counter）；
一句两单 = `legs`；平一半 = frac 0.5（规划器向下取整、至少 1 手）；反手不带手数；条件句 stance=conditional；
问句后跟“算了”仍是问句；整句收回 = act none + retracted，有待确认卡时“算了” = act retract；否定 = negated。

输出约定：`product` 是目录品种名（螺纹、沪金…，口语别名已归一）；`symbol` 是说出的合约（品种名 + 月份，如
螺纹2701），或指向当前图表时的 `ParseContext.chart`。入口把它们解析成合约代码后再交规划器。没点品种：开仓 /
目标持仓 / 反手取当前图表（`symbol=ctx.chart`，入口不知道图表时为 None、由入口补），全平是全部持仓，部分平仓
留空（由入口按图表或唯一持仓定）。
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

from .instruments import FOREIGN_ALIASES, FOREIGN_PRODUCTS, foreign_tradable
from .order_intent import CARD_ACTS, OrderIntent, Price
from .voice_intents import POSITION_UI_NOUNS, PRODUCT_ALIASES, UI_SURFACE_WORDS


@dataclass(frozen=True)
class Uncertain:
    """快速规则拿不准：不给结构，交给交易 AI（或追问）。`gap` 是缺口类别（覆盖率统计、日志）。"""
    gap: str
    detail: str = ""


@dataclass(frozen=True)
class ParseContext:
    """What the words are read against. `products`: the catalog's product names (生产：合约名去掉月份，螺纹/沪金…);
    `aliases`: spoken name → catalog name (None = the shared `PRODUCT_ALIASES`); `chart`: the contract on screen when
    the entry knows it; `chart_product`: its catalog product name; `pending_card`: a card waits for 确认 (“算了” then
    takes the card back, “确认” confirms it, a new order is not read here); `held`: catalog product name → the
    directions held (None = the account was not read: a bare 买/卖 is then not checked against it)."""
    products: tuple[str, ...] = ()
    aliases: Mapping[str, str] | None = None
    chart: str | None = None
    chart_product: str | None = None
    pending_card: bool = False
    held: Mapping[str, frozenset[str]] | None = None


Parsed = OrderIntent | Uncertain

# Uncertain gaps where the words clearly state an order the fast rules could not settle (拆不开的一句多单、改口、两个手数或
# 价格、调到几手…): the typed router hands them to the trading AI's propose_order round (意图重构第 3 步).
ORDER_GAPS = frozenset({"legs", "count", "correction", "verbs", "products", "restate", "side_vs_position",
                        "limit_price", "price", "price_word", "volume", "takeback", "target", "close", "open",
                        "reverse", "direction", "bucket", "protection"})
# An order with a time the rules cannot place (“待会买一手”) or two conditions: not an order now — no card.
TIMED_GAPS = frozenset({"time", "condition"})
# A command and a question in one sentence (`_mixed`): the trading AI's full round does both — never a no-card answer.
MIXED_GAP = "mixed"


def trade_talk(text: str, products: tuple[str, ...] | list[str] | None = None) -> bool:
    """Whether the words are about an order — one stated (any act), or one asked about, refused, reported, taken
    back or put off, or the 全平 / 全撤 words with something the rules do not read riding along (`book_words`), or a
    command to start a strategy (`commands_start`: “开始监控螺纹”, #932 复核) — so never interface work (the UI
    fallback's exclusion, 意图重构第 3–4 步). A plain question (“持仓面板在哪”) is not, nor are words the rules do not
    know."""
    parsed = parse(text, ParseContext(products=tuple(products or ())))
    if isinstance(parsed, Uncertain):
        talk = parsed.gap in ORDER_GAPS or parsed.gap in TIMED_GAPS or parsed.gap == MIXED_GAP
    else:
        talk = parsed.act != "none" or parsed.stance != "question"
    return (talk or book_words(text) is not None
            or (commands_start(text) and _STRATEGY_WORDS.search(text or "") is not None))


# 全平 / 全撤的说法（#713/#722/#730/#738）：关、退出、撤、平、清 + 所有 / 全部 + 持仓、仓位、委托（倒过来说、“都”也算）。
# 清单、平仓记录是界面名词，不是平、清。
_BOOK_VERB = rf"(?:关|退出|撤|(?!{POSITION_UI_NOUNS})[平清])"
_BOOK_WORDS = re.compile(
    rf"{_BOOK_VERB}[掉了上闭销]?(?:所有|全部)的?(?:持仓|仓位|委托)"
    rf"|(?:所有|全部)的?(?:持仓|仓位)(?:都|全)?{_BOOK_VERB}[掉了上闭销]?"
    rf"|(?:持仓|仓位)(?:都|全部|全){_BOOK_VERB}[掉了上闭销]?"
    rf"|委托(?:都|全部|全)?{_BOOK_VERB}[掉了上闭销]?")
_CLAUSE_END = re.compile(r"，|然后|接着|之后|再|并且|并|同时|顺便")


def book_words(text: str) -> tuple[str, str] | None:
    """The 全平 / 全撤 words a sentence says, as what comes before and after them (the normalized sentence); None when
    it says none, or when an interface part follows them in their clause (“关掉所有持仓这个面板 / 持仓都关上面板”:
    interface work). A part named before them is only where it happens (“在持仓面板里把所有仓位都关掉”), and a scope,
    a deadline or a spoken tail after them (“两个合约 / 一分钟后 / 啦”) leaves them standing. Only words: whether they
    state an order is `parse`'s to say."""
    norm = _normalize(text)
    found = _BOOK_WORDS.search(norm)
    if found is None:
        return None
    before, after = norm[:found.start()], norm[found.end():]
    return None if UI_SURFACE_WORDS.search(_CLAUSE_END.split(after, maxsplit=1)[0]) else (before, after)


def commands_order(text: str, products: tuple[str, ...] | list[str] | None = None) -> bool:
    """An order stated now (`states_order`) or an order cancellation commanded (“撤掉刚才的委托”): words a 复盘 answer
    never swallows (#952 Codex)."""
    parsed = parse(text, ParseContext(products=tuple(products or ())))
    return (_states(parsed) or (isinstance(parsed, OrderIntent) and parsed.stance == "order" and parsed.act == "cancel")
            or (isinstance(parsed, Uncertain) and parsed.gap == MIXED_GAP))


def states_order(text: str, products: tuple[str, ...] | list[str] | None = None) -> bool:
    """Whether the words state an order now: the test other routes ask before taking a sentence for themselves
    (策略编辑卡的补一句, a 复盘 answer). `products`: the catalog's product names."""
    return _states(parse(text, ParseContext(products=tuple(products or ()))))


def _states(parsed: Parsed) -> bool:
    """An order stated now: one the fast rules read (one order or its `legs`), or one they could not settle."""
    if isinstance(parsed, Uncertain):
        return parsed.gap in ORDER_GAPS
    return parsed.stance == "order" and parsed.act in CARD_ACTS


@dataclass(frozen=True)
class Guard:
    """What the words say about one protection kind (止损 or 止盈), read in its own stretch of the sentence (#932 P2:
    never the whole sentence's numbers): `prices` the numbers said after it up to the next 止损/止盈 or the pause (lot
    counts and 跳 left out), `drop` 撤/取消/不要/去掉 beside it, `cost` 成本/保本 beside it (a stop moved to the cost)."""
    said: bool = False
    prices: frozenset[float] = frozenset()
    drop: bool = False
    cost: bool = False


@dataclass(frozen=True)
class Mentions:
    """What a sentence names, read word by word (unknown words stepped over): the evidence an order's slots must
    have in the user's own words — by value, not by mere mention (order_planner.ground, 意图重构第 3 步)."""
    products: frozenset[str]  # catalog names said (aliases folded)
    codes: frozenset[str]     # contract codes said, lower case (rb2701)
    months: frozenset[tuple[str, str]]  # (catalog name, month) said together: 螺纹2705 → ("螺纹", "2705")
    pointing: bool            # 这个 / 当前合约 …
    acts: frozenset[str]      # the verbs said: open / close / shut / target / reverse
    opening: frozenset[str]   # the side an opening names: 买/开多/做多/多 → long; 卖/开空/空 → short
    held: frozenset[str]      # the held side a close names: 卖平/多单/买的 → long; 买平/空单/卖的 → short
    counts: frozenset[int]    # the lot counts said plainly (N 手; “两百五手” reads as none)
    half: bool                # 一半
    whole: bool               # 全部 / 所有 / 都 / 全平 / 清仓
    zero: bool                # 清零 / 归零
    modes: frozenset[str]     # price ways said (counter / queue / last)
    prices: frozenset[float]  # numbers said that are not lot counts (a limit price, 八百二 = 820)
    price_word: bool          # 限价 / 涨停 / 跌停: a price the words ask for
    position: bool            # 持仓 / 仓位 / 多单 / 空单 / 全平 / 清仓 / 平、平掉、平仓
    surface: bool             # an interface part as the object: 面板、窗口、列表…
    stop: Guard               # 止损: its own prices, 撤, 成本
    take: Guard               # 止盈: the same
    buckets: frozenset[str]   # 平今 / 今仓 → today, 平昨 / 昨仓 → yesterday
    cond: dict[str, Any] | None  # the condition stated (跌到 3000 就… / 收盘前…), whatever else the sentence says
    lots_said: bool = False   # a lot count was said (N 手), read or not (“两百五手” reads as no count but was said)
    filtered: bool = False    # a pick among the positions no intent slot holds, in the closing clause: 盈利的 / 亏的 … (#932 A12)
    bare_products: frozenset[str] = frozenset()  # product names said on their own, not as the name of a month (螺纹2705)
    # Only 买 / 卖 said, no 开 / 平 / 多 / 空 (“卖一手白银”): with the opposite side held it may mean 平 (#1006, owner
    # 2026-10-19: ask 平多还是开空 — never guess either way).
    bare_side: bool = False


_CODE = re.compile(r"(?<![A-Za-z])[A-Za-z]{1,2}\d{3,4}(?!\d)")
_GUARD_WORD = re.compile(r"止损|止盈")
_GUARD_DROP = re.compile(r"撤|取消|不要|去掉")
_GUARD_COST = re.compile(r"成本|保本")
# Positions picked by how they stand (“平掉盈利的单子”“把亏的都平了”): the intent has no slot for it, so a 全平 the
# model reads from such words would close more than was said (#932 A12, owner 2026-10-19: ask which ones).
# Only a pick in the clause that closes counts (“把亏的都平了”); a loss said as the reason before it (“螺纹亏钱了，把螺纹
# 全平了”) picks nothing.
_CLOSE_VERB = re.compile(r"平|关|清")
_POSITION_FILTER = re.compile(r"盈利|赚钱|挣钱|赚了|赚的|有赚|亏损|亏钱|亏本|亏了|亏的|赔钱|赔本|赔了|赔的|浮盈|浮亏|被套|套住的")


def _guards(norm: str) -> dict[str, Guard]:
    """止损 / 止盈 each read in its own stretch of each pause-separated clause: its prices from the words right after it
    (up to the other one), its 撤 / 成本 from the words between its neighbours (“撤掉止损，止盈改到 3200”)."""
    found: dict[str, Guard] = {"止损": Guard(), "止盈": Guard()}
    for clause in norm.split("，"):
        hits = list(_GUARD_WORD.finditer(clause))
        for index, hit in enumerate(hits):
            start = hits[index - 1].end() if index else 0
            end = hits[index + 1].start() if index + 1 < len(hits) else len(clause)
            around, after = clause[start:end], clause[hit.end():end]
            prices = {price for number in re.finditer(rf"(?:{_NUMERAL})(?![\d.{_CN_CHARS}]|手|跳|个点)", after)
                      if (price := spoken_number(number.group(), clipped=True)) is not None and price > 0}
            old = found[hit.group()]
            found[hit.group()] = Guard(said=True, prices=old.prices | frozenset(prices),
                                       drop=old.drop or bool(_GUARD_DROP.search(around)),
                                       cost=old.cost or bool(_GUARD_COST.search(around)))
    return found


def mentions(text: str, ctx: ParseContext | None = None) -> Mentions:
    """The words of `text` the order slots can rest on. Pure."""
    ctx = ctx or ParseContext()
    norm = _normalize(re.sub(r"(?<=[A-Za-z\d])\s+(?=\d)", "，", text or ""))  # “ag2612 1 手”: code and count stay apart
    tokens = _tokens(_CODE.sub("，", norm), ctx, skim=True)  # a contract code's digits are its month, not a number
    assert isinstance(tokens, list)
    kinds = [token.kind for token in tokens]
    lots_at = {index for index, kind in enumerate(kinds[:-1]) if kind == "num" and kinds[index + 1] == "hand"}
    counts = {_count(str(tokens[index].value)) for index in lots_at}
    sides = {str(token.value) for token in tokens if token.kind == "dirn"}
    guards = _guards(norm)
    clauses: list[set[str]] = [set()]
    for kind in kinds:
        if kind == "sep":
            clauses.append(set())
        else:
            clauses[-1].add(kind)
    # 买/卖 is bare by the clauses that order something: “我手里有白银多单，卖一手白银” is still a bare 卖 (#932 T036).
    ordered: set[str] = set().union(*(clause for clause in clauses if clause & _VERBS))
    return Mentions(
        products=frozenset(str(token.value) for token in tokens if token.kind == "product"),
        codes=frozenset(code.lower() for code in _CODE.findall(unicodedata.normalize("NFKC", text or ""))),
        months=frozenset((str(token.value), str(tokens[index + 1].value)) for index, token in enumerate(tokens[:-1])
                         if token.kind == "product" and tokens[index + 1].kind == "num"
                         and _month(str(tokens[index + 1].value))),
        pointing="chart" in kinds,
        acts=frozenset(_FAMILY[kind] for kind in kinds if kind in _FAMILY) | ({"target"} if "tnoun" in kinds else set()),
        opening=frozenset(sides | {str(token.value) for token in tokens if token.kind in {"open", "side"} and token.value}),
        held=frozenset(sides | {str(token.value) for token in tokens if token.kind in {"close", "held"} and token.value}),
        counts=frozenset(count for count in counts if count), half="frac" in kinds,
        whole=bool({"all", "close_all"} & set(kinds)), zero="zero" in kinds,
        modes=frozenset(str(token.value) for token in tokens if token.kind == "mode"),
        prices=frozenset(price for index, token in enumerate(tokens) if token.kind == "num" and index not in lots_at
                         and not (index and tokens[index - 1].kind == "product" and _month(str(token.value)))
                         and (price := spoken_number(str(token.value), clipped=True)) is not None),
        price_word=bool(re.search(r"限价|涨停|跌停", norm)),
        position=bool({"pos", "close_all", "close", "close_bucket"} & set(kinds)) or any(
            token.text in {"多单", "空单"} for token in tokens),
        surface=bool(UI_SURFACE_WORDS.search(norm)),
        stop=guards["止损"], take=guards["止盈"],
        buckets=frozenset(str(token.value) for token in tokens if token.kind in {"bucket", "close_bucket"}),
        bare_products=frozenset(str(token.value) for index, token in enumerate(tokens) if token.kind == "product" and not (
            index + 1 < len(tokens) and tokens[index + 1].kind == "num" and _month(str(tokens[index + 1].value)))),
        bare_side="side" in ordered and not ({"open", "close", "close_all", "close_bucket", "dirn", "held", "shut", "reverse",
                                               "swap", "target", "keep", "zero"} & ordered),
        cond=_said_condition(norm), lots_said=bool(lots_at), filtered=any(
            _POSITION_FILTER.search(clause) and _CLOSE_VERB.search(clause) for clause in norm.split("，")))


def side_conflict(intent: OrderIntent, held: frozenset[str] | set[str]) -> bool:
    """A bare 买/卖 opening (no 开/多/空 said) while the opposite side is held: “卖一手” with a long held may mean
    卖平 — never guessed (corpus c020)."""
    if intent.act != "open" or not intent.bare or intent.dir is None:
        return False
    return ("short" if intent.dir == "long" else "long") in held


# -- 数字（唯一一处：手数、口语价格） -----------------------------------------------------------------

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_CHARS = "".join(_CN_DIGITS) + "".join(_CN_UNITS)
_NUMERAL = rf"\d+(?:\.\d+)?|[{_CN_CHARS}]+"
# A stop / take-profit riding an order (“开多一手螺纹，止损 3100”): the order without it reads as one.
_PROTECT_CLAUSE = re.compile(rf"，?(?:止损|止盈)价?(?:设在|设到|设|在|到|挂)?(?:{_NUMERAL})(?:块|元)?")


def spoken_number(token: str, *, clipped: bool) -> float | None:
    """"3080.5" / "两" 2 / "十二" 12 / "三千零八十" 3080. The clipped tail after 百/千/万 — "八百二" 820, "一万三"
    13000 — is read only when `clipped` (a spoken price, owner 2026-10-16); a count never reads it ("两百五手":
    250 or 205? — never guessed). None when the token is not one number."""
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    total = section = 0
    digit: int | None = None
    last_unit: int | None = None
    zero = False
    for char in token:
        if char in _CN_DIGITS:
            if digit is not None:
                return None  # "一二": digits read one by one, not a number
            if _CN_DIGITS[char] == 0:
                zero = True
                continue
            digit = _CN_DIGITS[char]
        elif char == "万":
            total += (section + (digit or 0) or 1) * 10000
            section, digit, last_unit, zero = 0, None, 10000, False
        elif char in _CN_UNITS:
            section += (1 if digit is None else digit) * _CN_UNITS[char]
            digit, last_unit, zero = None, _CN_UNITS[char], False
        else:
            return None
    if digit is not None:
        if last_unit is not None and last_unit >= 100 and not zero:
            if not clipped:
                return None
            section += digit * last_unit // 10
        else:
            section += digit
    return float(total + section)


def _count(token: str) -> int | None:
    value = spoken_number(token, clipped=False)
    return int(value) if value is not None and value == int(value) and value > 0 else None


def _month(token: str) -> bool:
    """A spoken contract month right after a product name: 2701 / 2612 (YYMM) or 611 (郑商所 YMM)."""
    if not token.isdigit():
        return False
    if len(token) == 4:
        return 24 <= int(token[:2]) <= 29 and 1 <= int(token[2:]) <= 12
    return len(token) == 3 and 1 <= int(token[1:]) <= 12


# -- 句子层标记 ---------------------------------------------------------------------------------------

_DROP = re.compile(r"[\s《》〈〉<>「」『』“”‘’\"'()（）【】\[\]]")
_SEPARATORS = re.compile(r"(?:[,，。!！?？;；、~～…·:：]|(?<!\d)\.|\.(?!\d))+")
MAX_CHARS = 36  # a longer sentence carries more than one plain order: the trading AI reads it

# 同句撤回 (#523 / #754): a sentence that states an order and ENDS in a take-back is withdrawn as a whole. 算了 takes
# back whatever it follows; the weaker tails (不要了 / 不做了 / 取消) only after an order stated with a verb or a volume
# (“沪金的止损不要了” / “那张挂单，取消” are instructions about an existing stop or order).
_TAKEBACK = r"(?:那|还是|要不|那就)?先?(?:算了|不要了|不做了|不买了|不卖了|不下了|不开了|不平了|不弄了|不搞了|取消)(?:吧|啊|呀|哈|啦)?"
_RETRACT_TAIL = re.compile(rf"(?:{_TAKEBACK})+$")
_RETRACT_ORDER = re.compile(r"买|卖|开|平|调到|调整到|做多|做空|加仓|减仓|[零〇一二两三四五六七八九十百\d]+手"
                            r"|撤|设|改|移|挂(?!单)")  # cancel / protection actions too, not the noun 挂单
_RETRACT_ORDER_STRONG = re.compile(r"撤|追|取消|止损|止盈|保护|多单|空单|全平|清仓|下单|挂单|订单|委托|单子")
# A question ending in the same characters is no take-back (“我的多单怎么算了”); 怎么 / 咋 only right before the tail,
# 什么 not at all (“不管什么价”).
_RETRACT_QUESTION = re.compile(r"为什么|为啥|吗|呢|多少|几|是不是|有没有|能不能|会不会|可不可以|行不行|要不要|好不好"
                               r"|(?:怎么|咋)$")
_PAST = r"已经|我刚|刚刚|我已|之前|昨天|上次"
_DONE = r"(?:买|卖|开|做|挂|下|加|减)了"
_RETRACT_REPORTED = re.compile(rf"{_DONE}|{_PAST}")
# Take-back words alone. With a card waiting they take the card back (所有者第 8 项), and so do the bare 撤 words
# (有卡时“撤了”指卡); with none, there is nothing here to take back (the entry's fixed words decide).
_TAKEBACK_WORDS = rf"{_TAKEBACK}|不行了|取消这张卡|取消这单"
_TAKEBACK_ONLY = re.compile(rf"(?:{_TAKEBACK_WORDS})+")
_CARD_TAKEBACK = re.compile(rf"(?:{_TAKEBACK_WORDS}|撤单|撤了|撤掉|撤销|撤|撤吧)+(?:吧)?")
_CONFIRM = frozenset({"确认", "确定", "确认下单"})
_CONFIRM_WITH_CARD = _CONFIRM | {"好", "好的", "可以", "行", "没问题", "下吧", "可以下吧", "好可以下吧", "好的可以下吧",
                                 "下单吧"}

_POLITE = re.compile(r"(?:好吗|行吗|可以吗|好不好|行不行|可以不)$")
_ADVICE = re.compile(r"该不该|建议|值不值|值得|能不能(?:买|卖|做|追|空|多|开|抄)|可不可以|能追|能买吗|能卖吗|可以买吗|"
                     r"可以卖吗|推荐|看法")
_QUESTION = re.compile(r"(?<!不管)(?<!无论)(?:多少|几|什么|啥|怎么|怎样|如何|为什么|为啥|是不是|有没有|会不会|要不要|"
                       r"能不能|哪|吗|假设)|呢$")
# 否定：一个分句里否定词直接管着下单动作——「不要全部平仓 / 别撤单 / 先不平」（四份否定词表 #754 起收成这一份）。
NEGATED_ORDER = re.compile(r"别|不要|不用|不必|甭|不撤|先不|没让|没说|不是让|不想|不能|不会|不打算|不准备|没想|不该|不许"
                           r"|不买|不卖|不开|不平|不做|不下")
_NEGATED_VERB = re.compile(r"买|卖|开|平|做|下单|撤|清仓|关|调|加|减|动")
# “不用问我 / 别再问了 / 不用再确认”: the negation is about asking, not about the act the sentence commands.
_NO_ASK = re.compile(r"(?:不用|不必|别|甭|无需|不需要)再?(?:问我|问了|问|跟我确认|和我确认|确认)(?:了|啦|吧)?")
_REPORTED = re.compile(rf"{_PAST}|成交了|(?<![帮给替让请叫])我(?:把.{{0,6}})?(?:撤|买|卖|开|平|做|加|减)了")
_REPORTED_WEAK = re.compile(_DONE)
_COMMAND = re.compile(r"帮我|给我|替我|请|麻烦|吧$")
_PRICE_CONDITION = re.compile(
    rf"(?:如果|要是|假如|等)?(?P<verb>跌到|跌破|涨到|涨破|突破|升到|做到|到了)(?P<num>{_NUMERAL})(?![\d.手])(?:块|元)?"
    r"(?:的话|以后|之后|的时候|时)?(?:就|再|那就)?")
_CONDITION_OPS = {"跌到": "<=", "跌破": "<", "涨到": ">=", "升到": ">=", "涨破": ">", "突破": ">", "做到": "reach",
                  "到了": "reach"}
_TIME_CONDITION = re.compile(
    r"(?P<when>(?:明天|后天|大后天|下周[一二三四五]?|今晚|晚上|下午|上午|中午|夜盘)?(?:开盘后|开盘前|开盘时|开盘|收盘前|收盘后|收盘时|收盘)"
    r"(?!价)|明天|后天|大后天|下周[一二三四五]?|今晚)(?:的时候|时|就|再)?")
_VAGUE_TIME = re.compile(r"待会|一会|稍后|等下|等会|过会|几点|点钟|以后|晚上|下午|上午|夜盘|早上|中午|今天|月底|周[一二三四五]")
_CORRECTION = re.compile(r"不对|说错了|错了|哦不|呃不|不不|我是说")
_AMEND = re.compile(r"改成|改为|换成")
# “算了，还是开两手沪金多单”：收回在前、新单在后（取消 不算：“取消委托”是撤单）。
_LEAD_TAKEBACK = re.compile(r"^(?:(?:那|要不)?先?(?:算了|不要了|不做了|不买了|不卖了|不平了|不下了)(?:吧|啊|呀)?，?)+(?:还是|那就|那)?")
_HESITATION = re.compile(r"^(?:那个|这个|嗯|呃|啊|哦|那)，")  # “那个，帮我买两手螺纹”：开口的口头禅，不是指代
_MIDDLE_TAKEBACK = re.compile(r"，(?:算了|不要了|不做了|不买了|不卖了|不平了)(?:，|还是)|(?:算了|不要了|不做了)还是")


# “下多单”“下一手空单”“下个多单”（所有者 2026-10-19 语音实测）：“下”只在它是句首的下单动词时读成开仓——句首、逗号后，
# 或紧跟“再（不是不再 / 没再 / 别再）/ 就 / 先 / 我 / 请 / 麻烦 / 现在 / 给我 / 帮我 / 我要 / 马上 / 立刻 / 赶紧”，且后面是（几手 / 个）多单 / 空单、不以“了”收尾。
# 白名单（PR #1004 审查）：“一下”“剩下”“往下”、各种否定（不下 / 不能下 / 不打算下）、“我下多单了”都不改写。
# 只在 parse 读句子时用（_sentence 末尾），不进 _normalize / mentions（PR #1009 审查）。
_PLACE = re.compile(r"(?:^|(?<=[，就先我请])|(?<=(?<![不没别])再)|(?<=给我)|(?<=帮我)|(?<=我要)|(?<=马上)|(?<=立刻)|(?<=赶紧)"
                    r"|(?<=麻烦)|(?<=现在))"
                    r"下(?=(?:[一二两俩三四五六七八九十\d]+(?:手|张)|个)?[多空]单(?!了))")


def _normalize(text: str) -> str:
    """Spaces, brackets and ASR quotes dropped; every pause mark one "，" (a decimal point stays the price's)."""
    folded = _DROP.sub("", unicodedata.normalize("NFKC", text or ""))
    return _SEPARATORS.sub("，", folded).strip("，")


def _retracted(bare: str, asked: bool) -> bool:
    """The sentence states an order (open, close, cancel, protect) and then takes it back at its end."""
    tail = _RETRACT_TAIL.search(bare)
    if tail is None or tail.start() == 0:
        return False
    head = bare[:tail.start()]
    if _RETRACT_QUESTION.search(head) or asked or _RETRACT_REPORTED.search(head):
        return False  # a question, or a report of what was done (“我刚买了一手螺纹又取消”), is not taking it back
    if _RETRACT_ORDER.search(head):
        return True
    return "算了" in tail.group() and _RETRACT_ORDER_STRONG.search(head) is not None


# -- 词表与切词 ---------------------------------------------------------------------------------------

_VOCAB: dict[str, tuple[str, Any]] = {}


def _words(kind: str, value: Any, *words: str) -> None:
    for word in words:
        _VOCAB[word] = (kind, value)


_words("open", "long", "买开", "开多", "做多", "追多")  # 追多 / 追空 = 开多 / 开空（#932 实测“螺纹追多两张”）
_words("open", "short", "卖开", "开空", "做空", "卖空", "追空")
_words("side", "long", "买入", "买进", "买")
_words("side", "short", "卖出", "卖")
_words("open", None, "开仓", "开", "来", "做", "搞", "下单")
_words("close", "long", "卖平")
_words("close", "short", "买平")
_words("close", None, "平掉", "平了", "平仓", "平", "减")
_words("close_all", None, "全平", "清仓")
_words("close_bucket", "today", "平今", "平今仓")
_words("close_bucket", "yesterday", "平昨", "平昨仓")
_words("shut", None, "关掉", "关闭", "关了", "关")
_words("cancel", None, "撤单", "撤掉", "撤了", "撤销", "撤回", "撤", "取消")
_words("reverse", None, "反手")
_words("swap", None, "换成")
_words("target", None, "做到", "调到", "调整到", "加到", "减到", "降到", "变成", "做成")
_words("zero", None, "清零", "归零")
_words("keep", None, "保持")  # 保持 N 手多/空 = 目标持仓；没说几手、多空（“保持现在的仓位”）不猜
_words("dirn", "long", "多单", "多头", "多仓", "多的", "多")
_words("dirn", "short", "空单", "空头", "空仓", "空的", "空")
_words("held", "long", "买的", "买进的", "买入的", "做多的")
_words("held", "short", "卖的", "卖出的", "卖空的", "做空的")
_words("all", None, "所有", "全部", "都", "全")
_words("bucket", "today", "今仓")
_words("bucket", "yesterday", "昨仓")
_words("frac", 0.5, "一半")
_words("mode", "counter", "对手价", "对价", "市价", "挂对手价")
_words("mode", "queue", "挂单价", "排队价", "排队", "本方价", "挂买一价", "挂卖一价", "买一价", "卖一价")
_words("mode", "last", "最新价", "现价")
_words("limit", None, "限价")
_words("at", None, "价格", "价位", "挂在", "在", "按", "挂")
_words("unit", None, "块", "元")
_words("chart", None, "这个合约", "当前合约", "这个品种", "当前品种", "我现在看的这个", "现在看的这个", "我看的这个", "这个",
       "当前的", "当前")
_words("onoun", None, "委托", "挂单", "单子", "订单", "单", "那张", "这张", "那个", "那笔", "这笔")
_words("last", None, "刚才", "刚下的", "上一个", "上一笔")
_words("tnoun", None, "目标持仓", "净持仓")
_words("pos", None, "持仓", "仓位")
_words("each", None, "各")
_words("hand", None, "手", "张")  # 口语“两张” = 两手（“这张 / 那张”是更长的词，仍指卡或委托）
_words("restate", None, "就", "只", "仅")
_words("then", None, "然后", "之后再", "之后", "接着", "另外", "再")
_words("filler", None, "帮我", "给我", "替我", "麻烦", "请", "那就", "那", "吧", "了", "啊", "呀", "呗", "嗯", "呃", "哦",
       "一下", "把", "将", "的", "个", "我", "你", "我的", "先", "重新", "现在", "给", "上", "我要", "马上", "立刻", "赶紧")
_AMBIGUOUS = "\x00"  # a prefix two catalog names share
# 可下单的外盘品种（展示名）与它明确的叫法（美黄金 → COMEX黄金）：账户目录里没有也认，规划器把这些单转到美元子账户。
_FOREIGN_NAMES = tuple(product.name for product in FOREIGN_PRODUCTS if foreign_tradable(product))
_FOREIGN_SAID = {alias: product.name for product in FOREIGN_PRODUCTS if foreign_tradable(product)
                 for alias in FOREIGN_ALIASES.get(product.key, ())}


@lru_cache(maxsize=8)
def _lexicon(products: tuple[str, ...], aliases: tuple[tuple[str, str], ...]) -> tuple[re.Pattern[str], dict[str, str]]:
    """One alternation of every product word and vocabulary word, longest first, then numerals. Product words: the
    catalog name, an alias whose catalog name is listed, and a prefix of a ≥3-char name with ≥2 non-digit chars
    (螺纹 → 螺纹钢; one rule with `voice_intents.product_words`); a prefix two names share answers to neither."""
    names: dict[str, str] = {name: name for name in products if name}
    for spoken, name in aliases:
        if name in names and spoken not in names:
            names[spoken] = name
    prefixes: dict[str, set[str]] = {}
    for name in products:
        for size in range(2, len(name)):
            prefix = name[:size]
            if len(name) >= 3 and sum(not char.isdigit() for char in prefix) >= 2 and prefix not in names:
                prefixes.setdefault(prefix, set()).add(name)
    for prefix, owners in prefixes.items():
        names[prefix] = next(iter(owners)) if len(owners) == 1 else _AMBIGUOUS
    literals = sorted({*names, *_VOCAB}, key=len, reverse=True)
    # A letter code (A 豆一, I 铁矿) is a word only on its own: the A and I of “用AI执行” name nothing (#932 T024).
    words = "|".join(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])" if word.isascii() and word.isalpha() else re.escape(word)
                     for word in literals)
    pattern = re.compile(f"(?P<w>{words})|(?P<n>{_NUMERAL})|(?P<s>，)")
    return pattern, names


@dataclass(frozen=True)
class _Token:
    kind: str
    value: Any
    text: str


def _tokens(text: str, ctx: ParseContext, *, skim: bool = False) -> list[_Token] | Uncertain:
    """The words the rules know, in order. `skim` steps over what they do not know (and over a shared prefix)
    instead of giving up — what `mentions` reads a whole sentence with."""
    aliases = {**_FOREIGN_SAID, **(PRODUCT_ALIASES if ctx.aliases is None else ctx.aliases)}
    pattern, names = _lexicon((*ctx.products, *_FOREIGN_NAMES), tuple(sorted(aliases.items())))
    out: list[_Token] = []
    at = 0
    while at < len(text):
        match = pattern.match(text, at)
        if match is None or not match.group():
            if skim:
                at += 1
                continue
            return Uncertain("leftover", text[at:at + 6])  # a word the rules do not know: 误听、限定词、别的事
        word = match.group()
        if match.group("s"):
            out.append(_Token("sep", None, word))
        elif match.group("n"):
            out.append(_Token("num", word, word))
        elif word in names:
            if names[word] == _AMBIGUOUS:
                if not skim:
                    return Uncertain("product", word)
            else:
                out.append(_Token("product", names[word], word))
        else:
            kind, value = _VOCAB[word]
            out.append(_Token(kind, value, word))
        at = match.end()
    return out


# -- 一个分句的槽位 -----------------------------------------------------------------------------------

_VERBS = frozenset({"open", "side", "close", "close_all", "close_bucket", "shut", "cancel", "reverse", "swap", "target",
                    "zero", "keep"})
_FAMILY = {"open": "open", "side": "open", "close": "close", "close_all": "close", "close_bucket": "close",
           "shut": "shut", "cancel": "cancel", "reverse": "reverse", "swap": "reverse", "target": "target",
           "zero": "target", "keep": "target"}


@dataclass
class _Clause:
    verbs: list[tuple[str, Any]] = field(default_factory=list)
    products: list[tuple[str, str | None]] = field(default_factory=list)
    vols: list[int] = field(default_factory=list)
    limits: list[float] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    dirns: list[str] = field(default_factory=list)
    helds: list[str] = field(default_factory=list)
    buckets: list[str] = field(default_factory=list)
    marks: set[str] = field(default_factory=set)  # chart / all / onoun / last / tnoun / pos / each / frac / restate / side_word
    bad: str | None = None

    def merge(self, other: _Clause) -> _Clause:
        return _Clause(self.verbs + other.verbs, self.products + other.products, self.vols + other.vols,
                       self.limits + other.limits, self.modes + other.modes, self.dirns + other.dirns,
                       self.helds + other.helds, self.buckets + other.buckets,
                       (self.marks | other.marks) - {"restate"}, self.bad or other.bad)

    @property
    def ordering(self) -> bool:
        """An order clause: a verb; or 多/空 said as the verb with lots (“空两手棉花”), or a side with its own price
        (“820 挂一手沪金多单”). “空单两手” (平还是开？) is none."""
        return bool(self.verbs) or bool(self.dirns and (self.limits or ("side_word" in self.marks and self.vols)))

    @property
    def stated(self) -> bool:
        return bool(self.verbs or self.products or self.vols or self.limits or self.modes or self.dirns
                    or self.helds or self.buckets or self.marks - {"restate"})


def _clause(tokens: list[_Token]) -> _Clause:
    clause = _Clause()
    skip: set[int] = set()
    for index, token in enumerate(tokens):
        if index in skip:
            continue
        before = tokens[index - 1] if index else None
        after = tokens[index + 1] if index + 1 < len(tokens) else None
        kind = token.kind
        if kind == "product":
            month = after.value if after is not None and after.kind == "num" and _month(after.value) else None
            if month is not None:
                skip.add(index + 1)
            clause.products.append((token.value, month))
        elif kind == "num":
            if after is not None and after.kind == "hand":
                count = _count(token.value)
                if count is None:
                    clause.bad = clause.bad or "count"  # “两百五手”：250 还是 205？
                clause.vols.append(count or 0)
                skip.add(index + 1)
            elif (before is not None and before.kind in {"at", "limit"}) or (
                    after is not None and (after.kind in {"side", "open", "unit"} or after.text == "挂")):
                price = spoken_number(token.value, clipped=True)
                if price is None or price <= 0:
                    clause.bad = clause.bad or "price"
                else:
                    clause.limits.append(price)
                if after is not None and after.kind == "unit":
                    skip.add(index + 1)
            else:
                clause.bad = clause.bad or "number"  # a number the sentence does not explain
        elif kind == "hand":
            clause.bad = clause.bad or "count"  # 手 with no number (“那手”“单手”)
        elif kind == "limit":
            if after is None or after.kind != "num":
                clause.bad = clause.bad or "limit_price"  # 限价, but the price was never said
        elif kind == "at":
            near_number = (after is not None and after.kind == "num") or (before is not None and before.kind == "num")
            if not near_number and (after is None or after.kind != "mode"):
                clause.bad = clause.bad or "price_word"  # 挂 / 在 / 按 with nothing to price
        elif kind == "unit":
            clause.bad = clause.bad or "number"
        elif kind in _VERBS:
            clause.verbs.append((kind, token.value))
            if kind == "close_bucket":
                clause.buckets.append(token.value)
            if kind == "close_all":
                clause.marks.add("all")
        elif kind == "dirn":
            clause.dirns.append(token.value)
            if token.text in {"多", "空"}:
                clause.marks.add("side_word")
        elif kind == "held":
            clause.helds.append(token.value)
        elif kind == "bucket":
            clause.buckets.append(token.value)
        elif kind == "mode":
            clause.modes.append(token.value)
        elif kind in {"frac", "chart", "all", "onoun", "last", "tnoun", "pos", "each"}:
            clause.marks.add(kind)
        elif kind == "restate" and index == 0:
            clause.marks.add("restate")
    return clause


def _agree(values: list[str]) -> tuple[bool, str | None]:
    return len(set(values)) <= 1, (values[0] if values else None)


def _price(clause: _Clause) -> Price | Uncertain:
    modes, limits = set(clause.modes), set(clause.limits)
    if len(modes) > 1 or len(limits) > 1 or (modes and limits):
        return Uncertain("price", "两种价格")  # “对手价…挂单价”“820 对手价”：哪一个？
    return Price(mode=next(iter(modes), None), limit=next(iter(limits), None))  # type: ignore[arg-type]


def _decide(clause: _Clause, ctx: ParseContext) -> Parsed | None:
    """One clause's intent; None = no order in it (only modifiers, or nothing)."""
    if clause.bad == "number" and clause.ordering:
        # An order with a number the rules cannot place (“螺纹做到多 2 手 3066”: a price said bare) is still an order:
        # the trading AI's propose_order round reads it, the price checked against the words (#932 P2).
        return Uncertain("price", "下单句里没说明的数字")
    if clause.bad:
        return Uncertain(clause.bad)
    if not clause.ordering:
        return None
    verbs = clause.verbs or [("open", None)]  # a side word with lots is an opening
    if len(verbs) > 1:
        return Uncertain("verbs", "一个分句两个动作")  # “卖一手螺纹平掉”“买一手螺纹吧就买”
    kind, verb_dir = verbs[0]
    said_dir = [verb_dir] if verb_dir and kind in {"open", "side", "close"} else []  # 平今 / 平昨: a bucket, not a side
    marks = clause.marks
    names = list(dict.fromkeys(name for name, _ in clause.products))
    if len(names) > 1:
        return Uncertain("products", "一句两个品种")
    if "each" in marks:
        return Uncertain("legs", "“各…”一句多单")
    months = {month for _, month in clause.products if month}
    product = names[0] if names else None
    symbol = f"{product}{months.pop()}" if product and len(months) == 1 else None
    if months:
        return Uncertain("products", "两个月份")
    if "chart" in marks and kind != "cancel":
        if product is not None:
            return Uncertain("products", "品种和“这个”并存")
        symbol = ctx.chart
    aimed = symbol or (None if product else ctx.chart)  # an opening, target or reverse naming nothing: the chart
    price = _price(clause)
    if isinstance(price, Uncertain):
        return price
    if len(set(clause.vols)) > 1:
        return Uncertain("volume", "两个手数")
    vol = clause.vols[0] if clause.vols else None
    frac = 0.5 if "frac" in marks else None
    ok, bucket = _agree(clause.buckets)
    if not ok:
        return Uncertain("bucket")

    if kind == "shut":  # 关掉所有持仓 = 全平；关闭全部委托 = 撤全部挂单；别的“关”是界面话
        if {"onoun", "all"} <= marks and "pos" not in marks:
            kind = "cancel"
        elif {"pos", "all"} <= marks and "onoun" not in marks:
            kind = "close_all"
        else:
            return Uncertain("shut", "关掉什么")
    if kind == "cancel":
        if (clause.vols or clause.dirns or clause.helds or clause.limits or clause.modes or clause.buckets
                or marks & {"frac", "tnoun", "pos"} or ("all" in marks and product)):
            return Uncertain("cancel_qualifier", "撤单带了查不了的限定")  # “撤掉螺纹两手的挂单”
        if ctx.pending_card:
            if product is None and not marks & {"last", "all"}:
                return OrderIntent(act="retract", scope="pending_card")  # 有卡时“撤了”指卡
            return Uncertain("pending_card")
        scope = "last_order" if "last" in marks else "all_orders" if "all" in marks else "working_order"
        return OrderIntent(act="cancel", product=product, symbol=symbol, scope=scope)
    if marks & {"last", "onoun"}:
        return Uncertain("noun", "下单句里有委托/刚才")
    if _FAMILY[kind] == "reverse":
        if clause.vols or frac or clause.limits or clause.buckets or marks & {"all", "tnoun"}:
            return Uncertain("reverse", "反手不带手数和限价")
        if kind == "swap":  # “把螺纹多单换成空单”
            if len(clause.dirns) != 2 or clause.dirns[0] == clause.dirns[1] or clause.helds:
                return Uncertain("reverse", "换成什么")
            direction: str | None = clause.dirns[0]
        else:
            ok, direction = _agree(clause.dirns + clause.helds)
            if not ok:
                return Uncertain("direction")
        return OrderIntent(act="reverse", product=product, symbol=aimed, dir=direction, price=price)  # type: ignore[arg-type]
    if _FAMILY[kind] == "target":
        if clause.limits or frac or clause.buckets or clause.helds or "all" in marks:
            return Uncertain("target")
        ok, direction = _agree(clause.dirns)
        if not ok:
            return Uncertain("direction")
        if kind == "zero":
            if vol is not None or direction is not None:
                return Uncertain("target")
            net = 0
        elif vol is None or direction is None:
            # “保持现在的仓位 / 保持一手”说的多半是别动：不交下单回合（gap keep 不在 ORDER_GAPS）。
            return Uncertain("keep" if kind == "keep" else "target", "调到几手、多还是空")
        else:
            net = vol if direction == "long" else -vol
        return OrderIntent(act="target", product=product, symbol=aimed, target=net, price=price)
    if _FAMILY[kind] == "close":
        if "tnoun" in marks or (vol is not None and frac is not None):
            return Uncertain("close")
        ok, direction = _agree(said_dir + clause.helds + clause.dirns)
        if not ok:
            return Uncertain("direction")
        if vol is not None or frac is not None:
            if "all" in marks:
                return Uncertain("close", "全部还是几手")
            return OrderIntent(act="close", product=product, symbol=symbol, dir=direction,  # type: ignore[arg-type]
                               vol=vol, frac=frac, price=price, bucket=bucket or "auto")  # type: ignore[arg-type]
        if not (product or symbol or "all" in marks or bucket):
            return Uncertain("close", "平哪个、平几手")  # “平仓”“把多单平了”
        return OrderIntent(act="close_all", product=product, symbol=symbol, dir=direction,  # type: ignore[arg-type]
                           price=price, bucket=bucket or "auto")  # type: ignore[arg-type]
    # 开仓
    if clause.helds or clause.buckets or frac or marks & {"all", "pos", "tnoun"}:
        return Uncertain("open")
    ok, direction = _agree(said_dir + clause.dirns)
    if not ok:
        return Uncertain("direction", "买卖与多空不一致")
    if direction is None and vol is None and product is None and "chart" not in marks:
        return Uncertain("open", "开什么")
    return OrderIntent(act="open", product=product, symbol=aimed, dir=direction, vol=vol, price=price,  # type: ignore[arg-type]
                       bare=kind == "side" and not clause.dirns)


# -- 多分句：补充语、复述、一句两单、改口 --------------------------------------------------------------

def _segments(tokens: list[_Token]) -> list[list[_Token]]:
    """Split at pauses and at 然后 / 之后 / 接着 / 另外; 再 splits only after a clause that already has its verb (“平掉螺纹
    再买一手沪金”), at the start it is just 再 (“再买一手螺纹”)."""
    segments: list[list[_Token]] = [[]]
    for token in tokens:
        current = segments[-1]
        if token.kind == "sep" or (token.kind == "then" and (token.text != "再" or any(
                item.kind in _VERBS for item in current))):
            if current:
                segments.append([])
        elif token.kind != "then":
            current.append(token)
    return [segment for segment in segments if segment]


def _same_order(earlier: _Clause, later: _Clause) -> bool:
    """“…买一手螺纹吧，就买一手”: the later clause only repeats what the earlier one said — every order slot it
    states (动作与方向、手数、品种与月份、平哪边、今昨) is one the earlier clause said, and it adds no price, scope or
    other qualifier (#950 review: “平买的一手螺纹，就平卖的一手螺纹” is no restatement)."""
    return (set(later.verbs) <= set(earlier.verbs) and set(later.vols) <= set(earlier.vols)
            and set(later.products) <= set(earlier.products) and set(later.dirns) <= set(earlier.dirns)
            and set(later.helds) <= set(earlier.helds) and set(later.buckets) <= set(earlier.buckets)
            and not later.limits and not later.modes and not (later.marks - {"restate"}))


def _order(text: str, ctx: ParseContext) -> Parsed:
    if _CORRECTION.search(text):
        return _corrected(text, ctx)
    tokens = _tokens(text, ctx)
    if isinstance(tokens, Uncertain):
        return tokens
    merged: list[_Clause] = []
    said: list[list[list[_Token]]] = []  # the segments each merged clause was said in
    for segment in _segments(tokens):
        clause = _clause(segment)
        if not merged:
            merged.append(clause)
            said.append([segment])
        elif not clause.ordering or not merged[-1].ordering:
            merged[-1] = merged[-1].merge(clause)  # a price / chart / object said in its own clause joins the order
            said[-1].append(segment)
        elif "restate" in clause.marks:
            if not _same_order(merged[-1], clause):
                return Uncertain("restate", "“就…”和前面说的不一样")
            said[-1].append(segment)
        else:
            merged.append(clause)
            said.append([segment])
    if len(merged) > 1:
        return _legs(said, ctx)
    return _decide(merged[0], ctx) or Uncertain("no_order", "没有下单动作")


_LEG_ACTS = frozenset({"open", "close", "close_all"})  # what the planner's legs card (independent or relayed)


def _legs(said: list[list[list[_Token]]], ctx: ParseContext) -> Parsed:
    """一句多单（所有者 2026-10-16：按顺序出卡、各自确认）：each order clause — split at 然后 / 再 / 接着 / 另外 or a
    pause — is read on its own as one plain sentence by `parse`, so every leg's contract, side, lots and price come from
    its own words and never from a neighbour's. Legs only when every clause is a sure order (开仓 with its side and
    lots, 平几手 / 平一半, 全平) naming its own contract (a product, a month, “这个”, or 全部 for a 全平), each said in one
    piece; anything else — a clause the rules are unsure of or that is no order, a price or contract said apart from
    its order (“…，对手价”: whose?), a 撤单 / 目标持仓 / 反手 among them, the same order said twice (“买一手螺纹，再买
    一手螺纹”), a shared verb (“螺纹、沪金各买一手”) — is the whole sentence Uncertain: one order at a time."""
    legs: list[OrderIntent] = []
    for segments in said:
        if len(segments) != 1:
            return Uncertain("legs", "一单的话分在几处说")
        words = "".join(token.text for token in segments[0])
        leg = parse(words, ctx)
        if not (isinstance(leg, OrderIntent) and leg.stance == "order" and leg.act in _LEG_ACTS
                and _whole_leg(leg, mentions(words, ctx))):
            return Uncertain("legs", "一句多单里有一单读不准")
        legs.append(leg)
    if len({(leg.act, leg.product, leg.symbol, leg.dir) for leg in legs}) < len(legs):
        return Uncertain("legs", "同一单说了两遍还是两单")
    return OrderIntent(act=legs[0].act, legs=tuple(legs))


def _whole_leg(leg: OrderIntent, said: Mentions) -> bool:
    """A leg the planner can card without asking about its words: its own contract, and an opening's side and lots,
    a close's lots or half."""
    if not (said.products or said.codes or said.pointing or (leg.act == "close_all" and said.whole)):
        return False
    if leg.act == "open":
        return leg.dir is not None and bool(leg.vol)
    return leg.act == "close_all" or bool(leg.vol) or leg.frac is not None


def _side_of(clause: _Clause) -> set[str]:
    return {str(value) for kind, value in clause.verbs if value in {"long", "short"}} | set(clause.dirns) | set(clause.helds)


def _corrected(text: str, ctx: ParseContext) -> Parsed:
    """改口（“开三手，不对，两手沪金多单”）：each correction, in order, replaces what it states; the rest of the earlier
    saying stands. Not guessed: a correction with nothing after it, one that changes what to do, one that leaves a
    word unread — and, with several corrections, a middle one that switched the product or the side when the last
    one does not say it again (“买一手螺纹，不对，买两手沪金，不对，买三手”：螺纹还是沪金？#950 review)."""
    marks = list(_CORRECTION.finditer(text))
    pieces = [text[:marks[0].start()]] + [text[mark.end():(marks[index + 1].start() if index + 1 < len(marks) else len(text))]
                                          for index, mark in enumerate(marks)]
    clauses: list[_Clause] = []
    for index, piece in enumerate(pieces):
        tokens = _tokens(re.sub(r"^[，]*(?:改成|改为|是)?", "", piece) if index else piece, ctx)
        if isinstance(tokens, Uncertain):
            return tokens
        if any(token.kind == "then" for token in tokens):
            return Uncertain("correction", "改口里还有下一单")
        clause = _clause([token for token in tokens if token.kind != "sep"])
        if clause.bad:
            return Uncertain(clause.bad)
        if index and not clause.stated:
            return Uncertain("correction", "改口没有下文")  # “买一手螺纹，不对。”
        clauses.append(clause)
    first, last = clauses[0], clauses[-1]
    if not first.ordering:
        return Uncertain("correction", "改口没有下文")
    families = {_FAMILY[kind] for clause in clauses for kind, _ in clause.verbs}
    if len(families) > 1:
        return Uncertain("correction", "改口换了动作")  # “平一手螺纹多单，不对，买一手螺纹”
    merged = first
    for position, later in enumerate(clauses[1:], 1):
        if position < len(clauses) - 1:  # a middle correction that switched the product or the side
            moved_product = bool((later.products and later.products != merged.products) or "chart" in later.marks)
            moved_side = bool(_side_of(later) and _side_of(merged) and _side_of(later) != _side_of(merged))
            if (moved_product and not (last.products or "chart" in last.marks)) or (moved_side and not _side_of(last)):
                return Uncertain("correction", "中间改了品种或方向，最后一次没再说")
        said_side = bool(later.verbs or later.dirns or later.helds)
        merged = _Clause(
            verbs=later.verbs or merged.verbs,
            products=later.products or ([] if "chart" in later.marks else merged.products),
            vols=later.vols or merged.vols,
            limits=later.limits if (later.limits or later.modes) else merged.limits,
            modes=later.modes if (later.limits or later.modes) else merged.modes,
            dirns=later.dirns if said_side else merged.dirns,
            helds=later.helds if said_side else merged.helds,
            buckets=later.buckets or merged.buckets,
            marks=(merged.marks - ({"chart"} if later.products else set())) | later.marks)
    decided = _decide(merged, ctx)
    if not isinstance(decided, OrderIntent):
        return decided or Uncertain("correction")
    return replace(decided, fixed=True)


def _amend(bare: str, ctx: ParseContext) -> Parsed:
    """改单（“改成三手”“换成螺纹”“改成限价3500”，有卡时“不对，是卖”）：exactly one thing about the waiting card."""
    marks = list(_CORRECTION.finditer(bare))
    if len(marks) > 1:
        return Uncertain("amend", "改单里改了几次口")  # 中间改的那一项最后一次没再说时会丢
    body = bare[marks[-1].end():] if marks else bare[_AMEND.match(bare).end():]  # type: ignore[union-attr]
    body = re.sub(r"^(?:改成|改为|换成|是)", "", body)
    tokens = _tokens(body, ctx)
    if isinstance(tokens, Uncertain):
        return tokens
    clause = _clause(tokens)
    if clause.bad:
        return Uncertain(clause.bad)
    sides = [value for kind, value in clause.verbs if kind in {"side", "open"} and value] + clause.dirns
    if len(clause.verbs) > len(sides) - len(clause.dirns) or clause.helds or clause.buckets or clause.marks:
        return Uncertain("amend", "改单只改一项")
    price = _price(clause)
    if isinstance(price, Uncertain):
        return price
    stated = [bool(clause.vols), bool(sides), bool(clause.products), price.stated]
    if sum(stated) != 1 or len(set(clause.vols)) > 1 or len(set(sides)) > 1 or len(clause.products) > 1:
        return Uncertain("amend", "改单只改一项")
    product, month = clause.products[0] if clause.products else (None, None)
    return OrderIntent(act="amend", scope="pending_card" if ctx.pending_card else None, fixed=bool(marks),
                       vol=clause.vols[0] if clause.vols else None, dir=sides[0] if sides else None,
                       product=product, symbol=f"{product}{month}" if month else None, price=price)


def _timed(rest: str, ctx: ParseContext) -> Uncertain:
    """A time the rules cannot place (待会、今天、晚上…): `time` when the rest of the sentence states an order (not
    one to card now), else `time_words` — words that only mention a time are not about an order at all."""
    about_order = _states(_order(_VAGUE_TIME.sub("", rest), ctx))
    return Uncertain("time", "说不清的时间") if about_order else Uncertain("time_words", "带时间的话")


def _said_condition(text: str) -> dict[str, Any] | None:
    """The price or time condition a (normalized) sentence states, read by the same rules as `_conditional`."""
    price = _PRICE_CONDITION.search(text)
    if price is not None:
        return _price_condition(price)
    time = _TIME_CONDITION.search(text)
    return {"type": "time", "text": time.group("when")} if time is not None else None


def _price_condition(found: re.Match[str]) -> dict[str, Any]:
    """{"type": "price", "op", "value"} for a `_PRICE_CONDITION` match; no "value" when its number does not read."""
    value = spoken_number(found.group("num"), clipped=True)
    return {"type": "price", "op": _CONDITION_OPS[found.group("verb")],
            **({"value": int(value) if value == int(value) else value} if value is not None else {})}


def _conditional(text: str, ctx: ParseContext) -> Parsed | None:
    """条件句（“跌到 3000 就买一手”“收盘前全平”）：the order part read as usual, stance conditional; None = no condition."""
    price, time = _PRICE_CONDITION.search(text), _TIME_CONDITION.search(text)
    if price is None and time is None:
        return _timed(text, ctx) if _VAGUE_TIME.search(text) else None
    if price is not None and time is not None:
        return Uncertain("condition", "价格和时间两个条件")
    if price is not None:
        cond, span = _price_condition(price), price
        if "value" not in cond:
            return Uncertain("condition")
        before = _tokens(text[:price.start()], ctx)
        if not isinstance(before, Uncertain) and before and before[-1].kind == "product":
            cond["product"] = before[-1].value
    else:
        assert time is not None
        cond, span = {"type": "time", "text": time.group("when")}, time
    rest = text[:span.start()] + text[span.end():]
    if _VAGUE_TIME.search(rest):
        return _timed(rest, ctx)
    order = _order(rest, ctx)
    if not isinstance(order, OrderIntent):
        return order
    if order.act not in CARD_ACTS or order.legs:
        return Uncertain("condition", "条件后面不是一张单")  # 几单时条件管哪一单说不清
    return replace(order, stance="conditional", cond=cond)


def _long_manner(norm: str, bare: str, asked: bool) -> OrderIntent | None:
    """A long sentence's manner, read by the same markers as a short one's: a question, advice, a negation, a
    condition, a report or a take-back is no order now (stance only, no slots); None = the trading AI reads it."""
    for found in (_asking(bare, asked), _negated(norm, bare)):
        if isinstance(found, OrderIntent):
            return found
    cond = _said_condition(norm)
    if cond is not None:
        return OrderIntent(act="none", stance="conditional", cond=cond)
    if _REPORTED.search(bare) and not _COMMAND.search(bare):
        return OrderIntent(act="none", stance="reported")
    if _retracted(bare, asked):
        return OrderIntent(act="none", stance="retracted")
    return None


# 命令与问句同句（#932：“撤掉螺纹的止盈，现在持仓还剩几手？”）：问句的分句不把整句变成问句。一个分句不带问句、
# 建议、假设、否定或转述的标记而说了动作，另一个分句在问——整句交给交易 AI 的完整回合，两件事各自照办（下单照样出卡）。
_COMMAND_CLAUSE = re.compile(r"买|卖|(?<!打)开(?!盘|始|心|会)|平|调到|调整到|做多|做空|加仓|减仓|撤|设|改|移|挂(?!单)")
_HYPOTHETICAL = re.compile(r"如果|要是|假如|假设|万一|的话")


def _mixed(norm: str) -> bool:
    """Whether a pause-separated clause commands something while another one asks."""
    clauses = [clause for clause in norm.split("，") if clause]
    if len(clauses) < 2:
        return False
    asks = [bool(_QUESTION.search(clause) or _ADVICE.search(clause)) for clause in clauses]
    return any(asks) and any(
        not asked and _COMMAND_CLAUSE.search(clause) is not None and not _HYPOTHETICAL.search(clause)
        and _negated(clause, clause) is None and not _REPORTED.search(clause) and not _REPORTED_WEAK.search(clause)
        for clause, asked in zip(clauses, asks))


def _asking(bare: str, asked: bool) -> Parsed | None:
    if _POLITE.search(bare):
        return Uncertain("polite", "“好吗/行不行”：客气的命令还是问句")  # “关掉所有持仓好吗”
    if _ADVICE.search(bare):
        return OrderIntent(act="none", stance="advice")
    if asked or _QUESTION.search(bare):
        return OrderIntent(act="none", stance="question")
    return None


def _negated(text: str, bare: str) -> Parsed | None:
    """“不要全部平仓”“别撤单”“不买一手螺纹”“不是全平”：一个分句里否定词直接管着下单动作才算否定；否定词在别处
    （“不对”之外的“不是卖”）说不清。“不用问我 / 不用再确认”否定的是问和确认，不是这句的动作（#932：“直接启动，
    不用问我”），先去掉再判。"""
    text = re.sub(r"，{2,}", "，", _NO_ASK.sub("", text)).strip("，")
    bare = _NO_ASK.sub("", bare)
    hit = NEGATED_ORDER.search(bare)
    if bare.startswith("不是"):
        word, end = "不是", 2
    elif hit is not None:
        word, end = hit.group(), hit.end()
    else:
        return None
    if "，" not in text and (_NEGATED_VERB.search(word[-1]) or _NEGATED_VERB.search(bare[end:end + 8])):
        return OrderIntent(act="none", stance="negated")
    return Uncertain("negation", "否定词管的是什么说不清")


# 策略的启动 (#953 Codex, #932 复核): a strategy that may order by itself starts in the user's turn only when the words
# command its start — 启动、上线、开启、启用、恢复、开始监控 / 运行、跑起来, or 改成自动下单 — in the manner of a command,
# read by the same markers as an order's: a question, advice, a negation, a condition, a report or a take-back is no
# start (“启动这个策略吗”“别上线”). Mentioning, introducing or judging a strategy (“介绍一下这个策略”“这个策略怎么样”)
# names no start at all.
_START = re.compile(r"启动|上线|开启|启用|恢复|开始(?:监控|运行|跑)|运行起来|跑起来|开跑|自动(?:下单|执行|交易)")
_STRATEGY_WORDS = re.compile(r"策略|监控")  # a start about a strategy (“开启深色主题”“恢复默认布局” start none)


def commands_start(text: str) -> bool:
    """Whether the words command a start (or a switch to 自动下单) — what they start is the caller's to bind. Pure."""
    norm = _HESITATION.sub("", _normalize(text))
    bare = norm.replace("，", "")
    asked = bool(re.search(r"[?？]", text or ""))
    if not bare or _START.search(bare) is None:
        return False
    if _asking(bare, asked) is not None or _negated(norm, bare) is not None:
        return False
    return _long_manner(norm, bare, asked) is None  # a condition, a report or a take-back


def parse(text: str, ctx: ParseContext | None = None) -> Parsed:
    """The intent one sentence states, or `Uncertain`. Pure; `ctx` says what the words are read against."""
    ctx = ctx or ParseContext()
    norm = _HESITATION.sub("", _normalize(text))
    bare = norm.replace("，", "")
    asked = bool(re.search(r"[?？]", text or ""))
    if not bare:
        return Uncertain("empty")
    if len(bare) > MAX_CHARS:
        # A long sentence is the trading AI's — unless its manner already says it is no order now (#952 Codex:
        # a 45-character “…我是不是应该买一手螺纹钢多单呢” is a question, never a card).
        if _mixed(norm):
            return Uncertain("mixed", "命令和问句在同一句")
        return _long_manner(norm, bare, asked) or Uncertain("long", "长句")
    result = _sentence(norm, bare, asked, ctx)
    if isinstance(result, Uncertain) and result.gap == "leftover" and _PROTECT_CLAUSE.search(norm):
        # 下单主体 + 随单止损止盈从句：an order the trading AI carries (with its protection), never the 策略编辑卡's
        # field or a 复盘 answer (states_order).
        inner = parse(_PROTECT_CLAUSE.sub("", norm), ctx)
        if isinstance(inner, OrderIntent) and inner.stance == "conditional":
            return inner  # “跌到 3000 就开多一手螺纹，止损 2900”: still a conditional order — no card now
        if isinstance(inner, OrderIntent) and inner.stance == "order" and inner.act in CARD_ACTS:
            return Uncertain("protection", "下单带随单止损止盈")
    if not isinstance(result, OrderIntent):
        return result
    if ctx.pending_card and result.stance in {"order", "conditional"} and (result.act in CARD_ACTS
                                                                            or result.act == "cancel"):
        return Uncertain("pending_card", "有卡等确认时的新单：加一张还是改这张")  # “再来一手”
    if ctx.held is not None:
        name = result.product or (ctx.chart_product if result.symbol in (None, ctx.chart) else None)
        if name is not None and side_conflict(result, ctx.held.get(name, frozenset())):
            return Uncertain("side_vs_position", "买/卖 与持仓方向相反：开新仓还是平仓")
    return result


def _sentence(norm: str, bare: str, asked: bool, ctx: ParseContext) -> Parsed:
    if ctx.pending_card and _CARD_TAKEBACK.fullmatch(bare):
        return OrderIntent(act="retract", scope="pending_card")
    if _TAKEBACK_ONLY.fullmatch(bare):
        return Uncertain("nothing_pending", "没有待确认卡时单说“算了”")
    if bare in (_CONFIRM_WITH_CARD if ctx.pending_card else _CONFIRM):
        return OrderIntent(act="confirm", scope="pending_card" if ctx.pending_card else None)
    if _retracted(bare, asked):
        return OrderIntent(act="none", stance="retracted")
    lead = _LEAD_TAKEBACK.match(norm)
    if lead is not None and lead.end() < len(norm):  # “算了，还是开两手沪金多单”：收回之后的新单成立
        return parse(norm[lead.end():].strip("，"), ctx)
    middle = _MIDDLE_TAKEBACK.search(norm)
    if middle is not None:  # “开一手沪金，算了，开两手吧”
        later = parse(norm[middle.end():].strip("，"), ctx)
        earlier = _tokens(norm[:middle.start()], ctx)
        named_before = isinstance(earlier, list) and any(token.kind == "product" for token in earlier)
        if isinstance(later, OrderIntent) and later.stance == "order" and named_before and later.product is None:
            return Uncertain("takeback", "收回后的新单没说品种")
        return later
    if _mixed(norm):
        return Uncertain("mixed", "命令和问句在同一句")
    asking = _asking(bare, asked)
    if asking is not None:
        return asking
    negated = _negated(norm, bare)
    if negated is not None:
        return negated
    if _REPORTED.search(bare):
        if _COMMAND.search(bare):
            return Uncertain("reported", "转述还是指令")
        return OrderIntent(act="none", stance="reported")
    if _REPORTED_WEAK.search(bare):
        return Uncertain("reported", "“…了”：做过了还是要做")
    if _AMEND.match(bare) or (ctx.pending_card and _CORRECTION.match(bare)):
        return _amend(bare, ctx)
    # “下多单” as “来多单” only here, after the question / negation / report checks read the words as said, and only for
    # the parser's own reading (it makes a card only when every word is understood): never in `mentions`, so the
    # trading AI's grounding is not handed an opening verb the user did not say (PR #1009 review).
    placed = _PLACE.sub("来", norm)
    conditional = _conditional(placed, ctx)
    if conditional is not None:
        return conditional
    return _order(placed, ctx)
