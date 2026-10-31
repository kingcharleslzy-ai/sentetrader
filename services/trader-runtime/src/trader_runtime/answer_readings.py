"""回答卡读数表 (回答卡结构化, 2026-10-17; the thresholds date from #886, 2026-10-14).

Every reading this turn's tools returned, by (contract, field, timeframe): `Facts.values`. The answer card quotes
these as field keys (answer_cards) and the Runtime renders them; nothing here reads the model's wording. The
qualitative states come from the readings with the thresholds the tools' own labels use (`analysis_labels`,
`day_move_label`), so the model quotes what the tools said:

- 放量 / 缩量 — the analysis volumeRatio (latest bar ÷ the window's average volume): > 1 / < 1.
- 增仓 / 减仓 — openInterestChange: > 0 / < 0; the quote's (board row's) change is the day's, the analysis's the window's.
- 涨 / 跌 / 持平 — the day's change against 昨结算 (snapshot / quote board, the 行情卡's figure; |change| < 0.05%
  reads 持平, ≤ 0.3% 基本持平); the window's move is the analysis periodReturnPercent. A pre-open placeholder
  quote's 0 is no reading of the day.
- 超买 / 超卖 — the RSI(14) reading (> 70 / < 30); 上行 / 下行 — the analysis trend; 多头 / 空头排列 — MA5 against MA20.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

FLAT_PERCENT = 0.05  # a move smaller than this (rounds to 0.0%) reads 持平
NEAR_FLAT_PERCENT = 0.3  # “基本持平 / 基本平盘 / 走平” allows a few ticks (铁矿 -0.15% on 10-14 21:2x), never -0.81%

KIND_WORDS = {"day": "当日涨跌（对昨结算）", "period": "区间涨跌", "volume": "量能", "oi": "区间持仓变化",
              "oiday": "当日持仓变化", "trend": "趋势", "ma": "均线排列", "rsi": "RSI", "boll": "布林位置"}
VALUE_WORDS = {
    "day": {"up": "上涨", "down": "下跌", "flat": "持平", "nearflat": "基本持平"},
    "period": {"up": "上涨", "down": "下跌", "flat": "持平", "nearflat": "基本持平"},
    "volume": {"up": "放量", "down": "缩量", "flat": "量能持平"},
    "oi": {"up": "增仓", "down": "减仓", "flat": "持仓持平"},
    "oiday": {"up": "增仓", "down": "减仓", "flat": "持仓持平"},
    "trend": {"up": "上行", "down": "下行", "range": "震荡"},
    "ma": {"up": "多头排列", "down": "空头排列"},
    "rsi": {"overbought": "超买", "oversold": "超卖", "neutral": "中性"},
    "boll": {"above": "在布林上轨之上", "below": "在布林下轨之下", "inside": "在布林带内"},
}
# 读数标签词 (fixed, owner 2026-10-17): the reading words a card template may say only through a state key. Checked for
# presence only — no negation, subject, condition or timeframe parsing; a template that has one is sent back.
LABEL_WORDS = sorted({word for words in VALUE_WORDS.values() for word in words.values()} | {
    "量增", "量缩", "地量", "持仓增", "持仓减", "平盘", "走平", "收平", "平收", "收涨", "收跌", "走高", "走低", "翻红", "翻绿",
    "飘红", "飘绿", "大涨", "大跌", "微涨", "微跌", "小涨", "小跌", "涨了", "跌了", "涨幅", "跌幅", "上轨", "下轨", "放大量", "领涨",
    "领跌", "不涨不跌", "上升趋势", "下降趋势", "多头趋势", "空头趋势", "多头格局", "空头格局"}, key=len)
_RSI_READINGS = {"超买区": "overbought", "超卖区": "oversold", "中性区间": "neutral"}
_BOLL_READINGS = {"价格在布林上轨之上": "above", "价格在布林下轨之下": "below", "价格在布林带内运行": "inside"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def move_values(percent: Any) -> set[str]:
    """up / down / flat of a percentage change; a tiny move is both its sign and 持平."""
    value = _number(percent)
    if value is None:
        return set()
    values = {"up"} if value > 0 else {"down"} if value < 0 else set()
    if abs(value) < FLAT_PERCENT:
        values.add("flat")
    if abs(value) <= NEAR_FLAT_PERCENT + 1e-9:  # 0.30% (as the board rounds it) still reads 基本持平
        values.add("nearflat")
    return values


def _sign_values(value: Any) -> set[str]:
    parsed = _number(value)
    if parsed is None:
        return set()
    return {"up"} if parsed > 0 else {"down"} if parsed < 0 else {"flat"}


def _ratio_values(value: Any) -> set[str]:
    parsed = _number(value)
    if parsed is None:
        return set()
    return {"up"} if parsed > 1 else {"down"} if parsed < 1 else {"flat"}


def _move_word(percent: Any) -> str | None:
    value = _number(percent)
    if value is None:
        return None
    return "持平" if abs(value) < FLAT_PERCENT else "上涨" if value > 0 else "下跌"


PRE_OPEN_MOVE = "本交易日还没有成交，暂无当日涨跌（价格是上一笔收盘）"


def placeholder_quote(market: dict[str, Any]) -> bool:
    """A quote of a contract with no trade yet this trading day (sidecar `pre_open_quote`: before the night
    session's open, or a contract nobody has traded in an open session): the last bar's close with a placeholder
    changePercent 0 and no open / high / low / volume. Its 0 is no reading of the day — the 10-14 20:0x answers
    called both contracts 当日持平 off it."""
    return str(market.get("dataStatus") or "") == "stale" and all(
        market.get(key) is None for key in ("open", "highest", "lowest", "volume"))


def day_move_label(market: dict[str, Any]) -> str | None:
    """The snapshot's 当日涨跌 label (against 昨结算, the 行情卡's figure); none before the day's first trade."""
    if placeholder_quote(market):
        return PRE_OPEN_MOVE
    word = _move_word(market.get("changePercent"))
    return f"当日{word}（对昨结算）" if word else None


def analysis_labels(*, volume_ratio: Any, open_interest_change: Any, period_return: Any, trend: Any,
                    rsi_reading: Any) -> dict[str, str]:
    """The K-line summary's qualitative labels, with the thresholds the claim check uses."""
    labels: dict[str, str] = {}
    volume = _ratio_values(volume_ratio)
    if volume:
        labels["volume"] = VALUE_WORDS["volume"][next(iter(volume))]
    interest = _sign_values(open_interest_change)
    if interest:
        labels["openInterest"] = "区间" + VALUE_WORDS["oi"][next(iter(interest))]
    word = _move_word(period_return)
    if word:
        labels["period"] = f"区间{word}（不是当日涨跌，当日看行情快照）"
    if str(trend) in VALUE_WORDS["trend"]:
        labels["trend"] = VALUE_WORDS["trend"][str(trend)]
    rsi = _RSI_READINGS.get(str(rsi_reading or ""))
    if rsi:
        labels["rsi"] = VALUE_WORDS["rsi"][rsi]
    return labels


def short_code(symbol: str) -> str:
    return symbol.split(".", 1)[1] if "." in symbol and not symbol.startswith("KQ.") else symbol


# 读数表 (回答卡结构化第 1 步, 2026-10-17): every reading this turn's tools returned, by (contract, field, timeframe) — the
# one source the answer card's field keys and their rendering read. A quote field has no timeframe (0);
# a K-line summary field has the summary's durationSeconds. A field read again keeps the latest value.
# field → (name on the card, format, where a read has it: "a|b" either key, "a.b" nested).
QUOTE_FIELDS = {"price": ("最新价", "plain", "price|last|lastPrice"), "day": ("当日涨跌", "pct", "changePercent"),
                "oiDay": ("当日增仓", "signed", "openInterestChange"), "open": ("开盘", "plain", "open"),
                "high": ("最高", "plain", "highest|high"), "low": ("最低", "plain", "lowest|low"),
                "volume": ("成交量", "lots", "volume"), "oi": ("持仓量", "lots", "openInterest"),
                "preSettlement": ("昨结算", "plain", "preSettlement")}
ANALYSIS_FIELDS = {"period": ("区间涨跌", "pct", "periodReturnPercent"), "range": ("振幅", "range", "rangePercent"),
                   "ma5": ("MA5", "plain", "sma5"), "ma20": ("MA20", "plain", "sma20"), "volRatio": ("量比", "plain", "volumeRatio"),
                   "oiPeriod": ("区间增仓", "signed", "openInterestChange"), "rsi": ("RSI", "plain", "indicators.rsi.value"),
                   "atr": ("ATR", "plain", "indicators.atr.value"), "cci": ("CCI", "plain", "indicators.cci.value"),
                   "bollTop": ("布林上轨", "plain", "indicators.boll.top"), "bollMid": ("布林中轨", "plain", "indicators.boll.mid"),
                   "bollBottom": ("布林下轨", "plain", "indicators.boll.bottom")}
FIELDS = {**QUOTE_FIELDS, **ANALYSIS_FIELDS}
# Readings that are words, not numbers: field → (where, the reading's value for each word the tool writes).
_WORD_FIELDS = {"trend": ("trend", {value: value for value in VALUE_WORDS["trend"]}),
                "rsiZone": ("indicators.rsi.reading", _RSI_READINGS), "bollPlace": ("indicators.boll.reading", _BOLL_READINGS),
                "maOrder": ("", {"up": "up", "down": "down"})}
# 定性键: key → (name on the card, claim kind, the field it reads, that reading's values).
STATES: dict[str, tuple[str, str, str, Callable[[Any], set[str]]]] = {
    "dayMove": ("当日", "day", "day", move_values), "oiDayState": ("当日持仓", "oiday", "oiDay", _sign_values),
    "periodMove": ("区间", "period", "period", move_values), "volumeState": ("量能", "volume", "volRatio", _ratio_values),
    "oiPeriodState": ("区间持仓", "oi", "oiPeriod", _sign_values), "trend": ("趋势", "trend", "trend", lambda value: {value}),
    "maOrder": ("均线", "ma", "maOrder", lambda value: {value}), "rsiZone": ("RSI", "rsi", "rsiZone", lambda value: {value}),
    "bollPlace": ("布林", "boll", "bollPlace", lambda value: {value})}
_DETAILS = {"dayMove": "对昨结算 {:+.2f}%", "oiDayState": "当日持仓变化 {:+.0f}", "periodMove": "区间涨跌 {:+.2f}%",
            "volumeState": "量比 {:.2f}", "oiPeriodState": "区间持仓变化 {:+.0f}"}


def _at(data: dict[str, Any], path: str) -> Any:
    for option in path.split("|"):
        value: Any = data
        for part in option.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def _word_of(readings: dict[str, str], value: str) -> str:
    return next(word for word, item in readings.items() if item == value)


class Facts:
    """读数表: (contract, field, timeframe) → the value this turn read last (`values`); a state key's values are
    derived from its reading (`state`)."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str, int], Any] = {}
        self.pre_open: set[str] = set()  # quotes read before the trading day's first trade: no day reading
        self.names: dict[str, str] = {}

    def _put(self, symbol: str, field: str, value: Any, duration: int = 0) -> None:
        value = _WORD_FIELDS[field][1].get(str(value)) if field in _WORD_FIELDS else _number(value)
        if value is None:
            return
        self.values[(symbol, field, duration)] = value

    def state(self, symbol: str, key: str, duration: int) -> set[str]:
        """A state key's values on one reading (empty: not read)."""
        value = self.values.get((symbol, STATES[key][2], duration))
        return STATES[key][3](value) if value is not None else set()

    def detail(self, symbol: str, key: str, duration: int) -> str:
        """The figure behind a state reading, for the model and the card ("日线 量比 0.80")."""
        value = self.values[(symbol, STATES[key][2], duration)]
        rsi = self.values.get((symbol, "rsi", duration))
        text = (_DETAILS[key].format(round(value, 2) + 0.0) if key in _DETAILS else f"趋势{VALUE_WORDS['trend'][value]}" if key == "trend"
                else f"MA5 {'高于' if value == 'up' else '低于'} MA20" if key == "maOrder"
                else (f"RSI {rsi:.1f}" if rsi is not None else f"RSI {_word_of(_RSI_READINGS, value)}") if key == "rsiZone"
                else _word_of(_BOLL_READINGS, value))
        return (f"{_frame_word(duration)} " if duration else "") + text

    def add(self, data: Any, depth: int = 0, *, quote: bool = False) -> None:
        """Every quote-like (symbol + price) and K-line-summary-like (symbol + volumeRatio / periodReturnPercent)
        object in a read: the snapshot, the quote board's rows, the decision context's market and analysis, the
        chart read's analysis. `quote`: `data` itself is a quote snapshot."""
        if depth > 6:
            return
        if isinstance(data, list):
            for item in data[:400]:
                self.add(item, depth + 1)
            return
        if not isinstance(data, dict):
            return
        symbol = data.get("symbol")
        if isinstance(symbol, str) and symbol.strip():
            symbol = symbol.strip()
            name = data.get("instrumentName") or data.get("name")
            if isinstance(name, str) and name.strip() and name.strip() != symbol:
                self.names.setdefault(symbol, name.strip())
            if "volumeRatio" in data or "periodReturnPercent" in data:
                duration = int(_number(data.get("durationSeconds")) or 0)
                for field, (_, _, path) in ANALYSIS_FIELDS.items():
                    self._put(symbol, field, _at(data, path), duration)
                for field, (path, _) in _WORD_FIELDS.items():
                    self._put(symbol, field, _at(data, path) if path else None, duration)
                short, long = _number(data.get("sma5")), _number(data.get("sma20"))
                if short is not None and long is not None and short != long:
                    self._put(symbol, "maOrder", "up" if short > long else "down", duration)
            if quote or any(data.get(key) is not None for key in ("price", "last", "lastPrice")):
                placeholder = placeholder_quote(data)  # its changePercent 0 is no reading of the day
                if placeholder:
                    self.pre_open.add(symbol)
                for field, (_, _, path) in QUOTE_FIELDS.items():
                    if not (placeholder and field == "day"):
                        self._put(symbol, field, _at(data, path))
        for value in data.values():
            if isinstance(value, (dict, list)):
                self.add(value, depth + 1)


def _frame_word(seconds: int) -> str:
    if seconds == 604800:
        return "周线"
    if seconds % 86400 == 0:
        return "日线" if seconds == 86400 else f"{seconds // 86400} 日线"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时线"
    return f"{seconds // 60} 分钟线" if seconds % 60 == 0 else f"{seconds} 秒线"
