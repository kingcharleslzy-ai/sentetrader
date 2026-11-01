"""What the call heard (stage 1 of the voice chain, split from voice_realtime 2026-11-01): the recognizer's
vocabulary (hotwords and the measured mishearings), the transcript cleaned of leaked entity tags, and whether a
partial only re-sends the sentence the Runtime already answered (Doubao replays answered words)."""
from __future__ import annotations

import difflib
import re
from decimal import Decimal
from typing import Any

TRADING_HOTWORDS = (
    # 动作
    "全平", "清仓", "平仓", "平掉", "撤单", "撤销", "开多", "开空", "买入", "卖出", "搞一手", "来一手",
    "挂单", "市价", "对手价", "最新价", "止损", "止盈", "减仓", "加仓", "锁仓", "反手", "撤掉",
    # 策略信号问法 (2026-09)：ASR 需要把这几段听准才会路由到 explain_strategy_signal
    "策略信号", "按我的策略", "按策略",
    # 手数
    "一手", "两手", "三手", "四手", "五手", "六手", "七手", "八手", "九手", "十手", "半仓", "满仓",
    # 品种口语（目录名之外的自叫法）
    "螺纹", "热卷", "沪金", "沪银", "沪铜", "沪镍", "铁矿", "焦煤", "焦炭", "棉花", "白糖", "苹果", "豆粕", "豆油",
    "棕榈", "甲醇", "PTA", "纯碱", "玻璃", "原油", "燃油", "沥青", "橡胶", "股指", "IF", "IC", "IM", "IH", "国债",
    # 市况
    "持仓", "行情", "主力", "主连", "多单", "空单", "实盘", "模拟盘",
)
# Real mishearings from the 09-19 harness replay: the replacement takes effect on its own (no twopass needed).
# Every new pair is a near-homophone (pinyin in the comment) whose wrong form has no independent trading
# meaning; pairs that could corrupt an innocent sentence ("原由"→原油, "开控"→开空) are deliberately absent.
ASR_CORRECT_WORDS = {
    # 09-19 harness, kept verbatim
    "全屏": "全平", "全评": "全平", "告一首": "搞一手", "搞一首": "搞一手", "买一首": "买一手", "卖一首": "卖一手",
    "时盘": "实盘", "罗文": "螺纹", "长了没": "涨了没", "长了吗": "涨了吗",
    "幻城": "换成", "视窗": "持仓", "池仓": "持仓", "实仓": "持仓", "交义台": "交易台",
    "自大一点": "字大一点", "复盘也": "复盘页",
    # 平仓/清仓 (quánpíng→quánpíng píng；qīngcāng)
    "全瓶": "全平", "青仓": "清仓", "亲仓": "清仓",
    # 撤单 (chèdān)
    "撤蛋": "撤单", "撤但": "撤单", "车单": "撤单",
    # 搞/来一手 (gǎo lái)
    "高一手": "搞一手", "告两手": "搞两手", "赖一手": "来一手",
    # 品种近音
    "罗纹": "螺纹", "罗纹钢": "螺纹钢", "沪进": "沪金", "护金": "沪金",  # luówén / hùjìn
    "铁狂": "铁矿", "铁框": "铁矿", "焦没": "焦煤", "交煤": "焦煤", "焦碳": "焦炭",  # tiěkuàng jiāoméi jiāotàn
    "热圈": "热卷", "热倦": "热卷", "春简": "纯碱", "豆泊": "豆粕", "棕旅": "棕榈",  # rèjuǎn chúnjiǎn dòupò zōnglǚ
    "假醇": "甲醇", "波梨": "玻璃", "玻梨": "玻璃", "元油": "原油", "燃由": "燃油",  # jiǎchún bōlí yuányóu rányóu
    "力青": "沥青", "香蕉": "橡胶", "护银": "沪银", "护铜": "沪铜", "护镍": "沪镍",  # lìqīng xiàngjiāo hùyín
    "古指": "股指", "果债": "国债",  # gǔzhǐ guózhài
    # 09-24 A/B: still misheard with twopass on
    "户银": "沪银", "户金": "沪金", "户铜": "沪铜", "互同": "沪铜", "斗破": "豆粕", "菜破": "菜粕",  # hù dòupò càipò
    "正眠": "郑棉", "正棉": "郑棉", "绵沙": "棉纱", "眠沙": "棉纱", "凭吊": "平掉",  # zhèngmián miánshā píngdiào
    "两首": "两手", "三首": "三手", "一首": "一手",  # shǒu
    # 交易词近音
    "纸损": "止损", "纸盈": "止盈", "刮单": "挂单", "兑手价": "对手价",  # zhǐsūn guàdān duìshǒu
    "曼仓": "满仓", "伴仓": "半仓", "所仓": "锁仓", "返手": "反手",  # mǎncāng bàncāng suǒcāng fǎnshǒu
    "家仓": "加仓", "简仓": "减仓", "住力": "主力",  # jiācāng jiǎncāng zhǔlì
}
# The twopass final now and then carries the recognizer's internal entity tag around a word, e.g.
# `<FunctionCall>| "name : "getMusicEntity, "parameters : "content :"甲醇]| <FunctionCall>| 820 挂一手多`
# (2 of 48 in the 09-24 long session; not in the doc). The tag is replaced by the word it wraps.
_FUNCTION_CALL_LEAK = re.compile(r'<FunctionCall>\|.*?content\s*:\s*"?([^\]|]*)\]\|\s*<FunctionCall>\|\s*', re.S)


def clean_transcript(text: str) -> str:
    """ASR text without the recognizer's leaked `<FunctionCall>` entity tags."""
    if "<FunctionCall>" not in text:
        return text
    return re.sub(r"<FunctionCall>\|?", "", _FUNCTION_CALL_LEAK.sub(lambda m: m.group(1).strip(), text)).strip()


_BARE_DROP = re.compile(r"[\s，。,.!！?？、~～…·《》“”\"'：:；;]")


def _dropped(text: str, index: int) -> bool:
    """Whether `text[index]` is dropped from its bare form: punctuation, except a "." between two
    digits — that is a decimal point ("0.1 倍"), and losing it made 0.1 compare as 01 = 一 (10-09)."""
    if (text[index] == "." and 0 < index < len(text) - 1
            and text[index - 1].isdigit() and text[index + 1].isdigit()):
        return False
    return _BARE_DROP.match(text[index]) is not None


def bare_form(text: str) -> str:
    """The words of a transcript without punctuation (decimal points stay with their digits): Doubao's
    partials alternate "确认" / "确认。"."""
    return "".join(char for index, char in enumerate(text) if not _dropped(text, index))


_ASCII_DIGITS = frozenset("0123456789")


def _digit_grouping(text: str, index: int) -> bool:
    """A comma or space between two ASCII digits is the second pass's digit grouping (“止损改成 1,000
    跳”), not a word boundary: the digits stay one number token, so the answered 止损改成1000跳 still
    strips and only the appended sentence routes (10-10 review: the grouping comma re-routed the
    answered instruction). Separators around Chinese numerals keep breaking words (二十，点一下)."""
    return (text[index] in ", " and 0 < index < len(text) - 1
            and text[index - 1] in _ASCII_DIGITS and text[index + 1] in _ASCII_DIGITS)


def _bare_breaks(text: str) -> frozenset[int]:
    """Bare indexes a dropped separator starts: the comma in "止损改成二十，点一下画线工具栏" keeps 点 the
    click verb, not the head of the number 二十点一 — removing punctuation must not remove the word
    boundary with it (10-10 review: the lost boundary re-routed the answered instruction). A digit-
    grouping separator (see `_digit_grouping`) breaks nothing."""
    breaks: set[int] = set()
    kept = 0
    boundary = False
    for index, char in enumerate(text):
        if _dropped(text, index):
            if not _digit_grouping(text, index):
                boundary = True
        else:
            if boundary and kept:
                breaks.add(kept)
            boundary = False
            kept += 1
    return frozenset(breaks)


def after_bare(text: str, count: int) -> str:
    """What of `text` follows its first `count` word characters (punctuation around the cut dropped;
    a kept decimal point counts with its digits, like `bare` does)."""
    seen = 0
    for index, char in enumerate(text):
        if seen >= count:
            return text[index:].lstrip("。．.，,！!？?；;、 ")
        if not _dropped(text, index):
            seen += 1
    return ""


# Doubao's second pass rewrites a spoken numeral between the partial and the completed ("止损改成二十跳"
# → "止损改成 20 跳", production 10-09), so an already-answered prefix matches with whole number tokens
# compared by value. A number is one token — Arabic digits together with their decimal point ("1.05" is
# never the integer runs 1 and 05, which made 05 = 5 = 1.5's tail, 10-09) or a run of Chinese numeral
# characters — and the Chinese side matches only through the unambiguous spoken grammar for 0..99 (一 /
# 十五 / 二十 / 二十五). Anything looser — digit-by-digit "一二", "一二十", 一百 — has no value here: a
# refused match keeps the whole sentence instead of guessing a number the words may not carry.
_NUMERAL_CHARS = frozenset("0123456789零〇一二两三四五六七八九十百千万亿")
_ASCII_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_CN_DIGIT_VALUES = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_TENS_HEAD = frozenset("一二三四五六七八九")  # 两十 / 零十 are not how tens are spoken


def _numeral_token_at(text: str, index: int, breaks: frozenset[int] = frozenset()) -> str:
    """Keep adjacent digits, decimal points and Chinese numerals together. Unsupported mixed forms
    such as 20万 or 一点五 must be refused whole, never stripped as an answered integer.
    点 joins a numeral only before another numeral; 点开 remains an ordinary operation word, and a
    dropped separator (`breaks`, from `_bare_breaks`) never joins what it stood between."""
    end = index + 1
    while end < len(text) and end not in breaks and (
        text[end] in _NUMERAL_CHARS or text[end] == "."
        or (text[end] == "点" and end + 1 < len(text) and text[end + 1] in _NUMERAL_CHARS)
    ):
        end += 1
    return text[index:end]


def _small_chinese_value(run: str) -> int | None:
    """A Chinese numeral run through the unambiguous spoken grammar for 0..99 only: one digit (零〇一…
    九, 两 = 2), 十, 十X, X十, X十X. None for every other shape — bigger numbers (一百), digit-by-digit
    readings (一二) — the caller keeps the sentence rather than guess."""
    if not run:
        return None
    if len(run) == 1:
        if run in _CN_DIGIT_VALUES:
            return _CN_DIGIT_VALUES[run]
        return 10 if run == "十" else None
    head, rest = run[0], run[1:]
    if head == "十":  # 十五: the 十 itself is the tens; 十两 / 十零 are not spoken
        if len(rest) == 1 and rest in _CN_DIGIT_VALUES and rest not in "零〇两":
            return 10 + _CN_DIGIT_VALUES[rest]
        return None
    if head not in _CN_TENS_HEAD:
        return None
    if rest == "十":
        return _CN_DIGIT_VALUES[head] * 10
    if len(rest) == 2 and rest[0] == "十" and rest[1] in _CN_DIGIT_VALUES and rest[1] not in "零〇两":
        return _CN_DIGIT_VALUES[head] * 10 + _CN_DIGIT_VALUES[rest[1]]
    return None


def _numeral_tokens_equal(bare_token: str, done_token: str) -> bool:
    """Whether two number tokens spell the same number: the same spelling outright; two Arabic numbers
    by value (1.50 = 1.5, 1.05 ≠ 1.5); Arabic against Chinese only as whole numbers within the 0..99
    grammar (一 = 1, 二十 = 20) — an answered integer is never the head of a new decimal."""
    if bare_token == done_token:
        return True
    bare_arabic = _ASCII_NUMBER.fullmatch(bare_token) is not None
    done_arabic = _ASCII_NUMBER.fullmatch(done_token) is not None
    if bare_arabic and done_arabic:
        return Decimal(bare_token) == Decimal(done_token)
    if bare_arabic or done_arabic:
        arabic, spoken = (bare_token, done_token) if bare_arabic else (done_token, bare_token)
        if "." in arabic:
            return False
        value = _small_chinese_value(spoken)
        return value is not None and value == int(arabic)
    value, done_value = _small_chinese_value(bare_token), _small_chinese_value(done_token)
    return value is not None and value == done_value


def prefix_match(text: str, done: str) -> tuple[int | None, bool]:
    """How many leading word characters of `text`'s bare form spell the already-answered `done` words,
    and whether the walk stopped at two number tokens of unequal value — the second pass corrected the
    number (止损改成1 → 止损改成一点五跳). `done` is the answered transcript, punctuation and all: its own
    separators bound its words too, so the answered “止损改成二十，点一下画线工具栏” never reads its 点一下
    back as the head of 二十点一 (10-10 review: the bare-only record did, and the same completed
    re-routed as a numeral correction). Whole number tokens compare by value (二十 = 20 and 1.50 = 1.5
    are the same words; 20 ≠ 30, 1.05 ≠ 1.5, and a shape the grammar does not read never matches),
    every other character exactly, and a dropped separator breaks both the join and the match.
    None: `text` does not lead with the answered words."""
    bare, breaks = bare_form(text), _bare_breaks(text)
    done_bare, done_breaks = bare_form(done), _bare_breaks(done)
    i = j = 0
    while j < len(done_bare):
        if i >= len(bare):
            return None, False
        if bare[i] in _NUMERAL_CHARS and done_bare[j] in _NUMERAL_CHARS:
            bare_token, done_token = _numeral_token_at(bare, i, breaks), _numeral_token_at(done_bare, j, done_breaks)
            if not _numeral_tokens_equal(bare_token, done_token):
                return None, True
            i += len(bare_token)
            j += len(done_token)
        elif bare[i] != done_bare[j]:
            return None, False
        else:
            i += 1
            j += 1
    return i, False


def answered_prefix(text: str, done: str) -> int | None:
    """The word count `prefix_match` strips (see there; both sides are transcripts, punctuation and all)."""
    return prefix_match(text, done)[0]


def _numeral_tokens(text: str) -> list[str]:
    """The whole number tokens of a transcript, in order (as `prefix_match` reads them)."""
    bare, breaks = bare_form(text), _bare_breaks(text)
    tokens: list[str] = []
    i = 0
    while i < len(bare):
        if bare[i] in _NUMERAL_CHARS:
            token = _numeral_token_at(bare, i, breaks)
            tokens.append(token)
            i += max(1, len(token))
        else:
            i += 1
    return tokens


# What makes a sentence another order however alike the rest reads: its action and its direction (#905 review:
# "平三手" after the answered "开三手…" is 2/3 alike, and it is a new order).
_ORDER_WORD_CHARS = frozenset("开平买卖撤加减追多空")


def _order_words_differ(bare: str, head: str) -> bool:
    """Whether the words that differ between a partial and the answered start include an action or direction word."""
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, bare, head).get_opcodes():
        if tag != "equal" and _ORDER_WORD_CHARS.intersection(bare[i1:i2] + head[j1:j2]):
            return True
    return False


def replays_answered(partial: str, done: str, products: Any = ()) -> bool:
    """A partial that only re-sends the sentence the Runtime already answered (`done`, the early / stall final):
    the whole of it or a growing start of it, in the first pass's words or the second pass's (开三首…互金 →
    开三手…沪金) — not the user saying something new (#905 review: "开三手" after "开三手，不对，…算了。"). A
    rewording never changes the number, the action, the direction or the product (`products`: catalog names)."""
    bare = bare_form(partial)
    if not bare:
        return True
    if not done:
        return False
    answered = answered_prefix(partial, done)
    if answered is not None:
        return answered >= len(bare)
    if answered_prefix(done, partial) is not None:
        return True
    # A second-pass rewording of the answered start (开三首 → 开 3 手) — but never a changed number: 开五手 after
    # 开三手 is a correction (#905 review), whatever the rest of the words.
    numbers, answered_numbers = _numeral_tokens(partial), _numeral_tokens(done)
    if len(numbers) > len(answered_numbers) or not all(
            _numeral_tokens_equal(a, b) for a, b in zip(numbers, answered_numbers, strict=False)):
        return False
    done_bare = bare_form(done)
    head = done_bare[:len(bare)]
    named = {name for name in products or () if name and name in bare}
    answered_names = {name for name in products or () if name and name in done_bare}
    # Another product than the answered one (a misheard name there — 互金 — says nothing either way).
    if _order_words_differ(bare, head) or (named and answered_names and not named <= answered_names):
        return False
    return difflib.SequenceMatcher(None, bare, head).ratio() >= 0.5
