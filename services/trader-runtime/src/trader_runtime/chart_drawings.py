"""用户画线 (2026-10-02; 画线第二版 2026-10-09): the lines and shapes the user draws on the chart.
They are stored per user × contract (not per account or period), so they survive a refresh and follow
the user across devices, and the AI can read, draw and change them (“我画的 3100 被突破了吗”).

This module owns the shared shape rules — a real contract from the catalog, finite positive prices
(a visual tool: the client rounds to the tick, the server does not force it), bar timestamps in
seconds, the label length, the per-contract cap, the style fields — plus the one-line receipt the UI
and the AI tools read. A drawing never trades and never counts against the AI's own annotation cap;
they are two independent layers.

Kinds (画线第二版): the three first-version kinds keep their stored coordinate shapes exactly —
hline {price}, trendline {t1,p1,t2,p2}, zone {low,high} — so every line saved before still reads and
draws the same. Every other kind stores `{"anchors": [{t, p}, …]}` (fib / percent also an optional
`levels` list). A write for a first-version kind may also send anchors; they are stored in that
kind's own shape. Style lives apart from the coordinates (`style`, every key optional — an absent key
means the kind's original look)."""
from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_DRAWINGS_PER_SYMBOL = 200
DRAWING_LABEL_MAX = 12
# 文字 (text) carries its words in the label: a sentence, not a tag.
DRAWING_TEXT_MAX = 60
MAX_POLYLINE_POINTS = 20
MAX_LEVELS = 12

# kind → (Chinese name, group, min anchors, max anchors). Groups follow the toolbar's six buttons.
DRAWING_SPECS: dict[str, tuple[str, str, int, int]] = {
    "trendline": ("趋势线", "lines", 2, 2),
    "ray": ("射线", "lines", 2, 2),
    "extended": ("直线", "lines", 2, 2),
    "hline": ("水平线", "lines", 1, 1),
    "hray": ("水平射线", "lines", 1, 1),
    "vline": ("垂直线", "lines", 1, 1),
    "channel": ("平行通道", "lines", 3, 3),
    "polyline": ("折线", "lines", 2, MAX_POLYLINE_POINTS),
    "zone": ("价格区间", "levels", 2, 2),
    "rect_zone": ("矩形区间", "levels", 2, 2),
    "fib": ("黄金分割", "fib", 2, 2),
    "percent": ("百分比线", "fib", 2, 2),
    "gann": ("江恩八线", "fib", 2, 2),
    "rect": ("矩形", "shapes", 2, 2),
    "ellipse": ("圆或椭圆", "shapes", 2, 2),
    "triangle": ("三角形", "shapes", 3, 3),
    "text": ("文字", "notes", 1, 1),
    "arrow": ("箭头", "notes", 2, 2),
    "price_label": ("价格标签", "notes", 1, 1),
    "measure": ("测量", "measure", 2, 2),
}
DRAWING_KINDS = tuple(DRAWING_SPECS)
KIND_WORDS = {kind: spec[0] for kind, spec in DRAWING_SPECS.items()}
# The first version's kinds and their stored coordinate shapes.
LEGACY_KINDS = ("hline", "trendline", "zone")
DEFAULT_LEVELS = {"fib": (0.0, 0.236, 0.382, 0.5, 0.618, 1.0), "percent": (0.0, 0.25, 0.5, 0.75, 1.0)}

# Style (画线第二版). Colours are a theme-tuned preset (the canvas reads the matching token in each
# theme) or a custom #rrggbb; `user` is the first version's fuchsia. `period` is the chart the line was
# drawn on (K 线 seconds, 0 = 分时); with `allPeriods` false the line only shows there.
DRAWING_COLOR_PRESETS = ("user", "amber", "red", "green", "blue", "ink")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DRAWING_DASHES = ("solid", "dashed", "dotted")
DRAWING_PERIODS = (0, 60, 180, 300, 900, 1800, 3600, 86400, 604800)
STYLE_FLAGS = ("extendLeft", "extendRight", "showPrice", "locked", "hidden", "allPeriods")
STYLE_KEYS = ("color", "width", "dash", "period", *STYLE_FLAGS)
_CHINA = timezone(timedelta(hours=8))


def price_text(price: Any) -> str:
    """A price as humans write it: no trailing .0, no scientific notation."""
    try:
        return format(Decimal(str(price)).normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        return str(price)


def _positive_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name}看不懂，要是一个数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name}看不懂，要是一个数字") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name}要是大于 0 的数")
    return number


def _bar_seconds(value: Any, *, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name}看不懂，要是秒级时间戳")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name}看不懂，要是秒级时间戳") from error
    if seconds < 0:
        raise ValueError(f"{name}要是不早于 0 的时间戳")
    return seconds


def _anchor_list(kind: str, points: dict[str, Any]) -> list[dict[str, Any]]:
    """`points.anchors` checked for the kind: the count, each {t, p} (a 垂直线 needs only t)."""
    words, _group, low, high = DRAWING_SPECS[kind]
    anchors = points.get("anchors")
    count = f"{low} 个" if low == high else f"{low}–{high} 个"
    if not isinstance(anchors, list) or not low <= len(anchors) <= high:
        raise ValueError(f"{words}需要 anchors：{count}点，每个点是 {{t, p}}（秒级 bar 时间和价格）")
    parsed: list[dict[str, Any]] = []
    for index, anchor in enumerate(anchors, start=1):
        if not isinstance(anchor, dict):
            raise ValueError(f"{words}第 {index} 个点不对，要是 {{t, p}}")
        allowed = {"t"} if kind == "vline" else {"t", "p"}
        given = set(anchor)
        if kind == "vline":
            if "t" not in given or not given <= {"t", "p"}:
                raise ValueError("垂直线只需要一个点的 t（秒级 bar 时间），p 可省略")
        elif given != allowed:
            raise ValueError(f"{words}第 {index} 个点要有 t 和 p（秒级 bar 时间和价格）")
        point: dict[str, Any] = {"t": _bar_seconds(anchor["t"], name=f"第 {index} 个点的时间")}
        if "p" in anchor:
            point["p"] = _positive_number(anchor["p"], name=f"第 {index} 个点的价格")
        parsed.append(point)
    return parsed


def _levels(kind: str, value: Any) -> list[float]:
    words = KIND_WORDS[kind]
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LEVELS:
        raise ValueError(f"{words}的比例（levels）要是 1–{MAX_LEVELS} 个数，如 [0, 0.382, 0.618, 1]")
    levels: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{words}的比例要是数字")
        try:
            number = float(item)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{words}的比例要是数字") from error
        if not math.isfinite(number) or not -5 <= number <= 5:
            raise ValueError(f"{words}的比例要在 -5 到 5 之间（1 = 100%）")
        levels.append(round(number, 6))
    if len(set(levels)) != len(levels):
        raise ValueError(f"{words}的比例不能重复")
    return sorted(levels)


def parse_points(kind: str, points: Any) -> dict[str, Any]:
    """The coordinates of one drawing, checked for its kind, in the stored shape. The first-version
    kinds keep theirs (hline {price}; trendline {t1,p1,t2,p2}; zone {low,high}) and also accept
    anchors; every other kind is {anchors: [{t, p}, …]} (fib / percent: optional levels). Unknown or
    missing keys are refused with a Chinese sentence."""
    words = KIND_WORDS.get(kind)
    if words is None:
        raise ValueError("线的类型不认识；能画：" + "、".join(f"{word}（{name}）" for name, word in KIND_WORDS.items()))
    if not isinstance(points, dict):
        raise ValueError(f"{words}的坐标（points）不对，画不出这条线")
    given = set(points)
    if kind in LEGACY_KINDS and given == {"anchors"}:
        anchors = _anchor_list(kind, points)
        if kind == "hline":
            return {"price": anchors[0]["p"]}
        if kind == "trendline":
            return {"t1": anchors[0]["t"], "p1": anchors[0]["p"], "t2": anchors[1]["t"], "p2": anchors[1]["p"]}
        low, high = sorted((anchors[0]["p"], anchors[1]["p"]))
        return {"low": low, "high": high}
    if kind == "hline":
        if given != {"price"}:
            raise ValueError("水平线只需要一个 price（价位）")
        return {"price": _positive_number(points["price"], name="价位")}
    if kind == "trendline":
        if given != {"t1", "p1", "t2", "p2"}:
            raise ValueError("趋势线需要 t1、p1、t2、p2（两个点的秒级时间和价格）")
        return {"t1": _bar_seconds(points["t1"], name="t1"), "p1": _positive_number(points["p1"], name="p1"),
                "t2": _bar_seconds(points["t2"], name="t2"), "p2": _positive_number(points["p2"], name="p2")}
    if kind == "zone":
        if given != {"low", "high"}:
            raise ValueError("区间需要 low 和 high（上沿下沿两个价）")
        low = _positive_number(points["low"], name="区间下沿")
        high = _positive_number(points["high"], name="区间上沿")
        if high < low:
            raise ValueError("区间的上沿要不低于下沿")
        return {"low": low, "high": high}
    allowed = {"anchors", "levels"} if kind in DEFAULT_LEVELS else {"anchors"}
    if "anchors" not in given or not given <= allowed:
        extra = "，可选 levels（比例）" if kind in DEFAULT_LEVELS else ""
        raise ValueError(f"{words}的坐标要写成 anchors{extra}")
    parsed: dict[str, Any] = {"anchors": _anchor_list(kind, points)}
    if "levels" in points:
        parsed["levels"] = _levels(kind, points["levels"])
    return parsed


def check_label(label: Any, kind: str | None = None) -> str:
    """The optional label, stored stripped: ≤12 chars, a 文字 drawing's words ≤60."""
    text = str(label or "").strip()
    limit = DRAWING_TEXT_MAX if kind == "text" else DRAWING_LABEL_MAX
    if len(text) > limit:
        raise ValueError(f"{'文字' if kind == 'text' else '标签'}最多 {limit} 个字")
    return text


def parse_style(style: Any, *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """The style patch checked and merged onto `base` (an update keeps the keys it does not name;
    null removes one, back to the kind's own default). Every refusal is a Chinese sentence."""
    if style is None:
        style = {}
    if not isinstance(style, dict):
        raise ValueError("样式（style）要是一个对象")
    unknown = set(style) - set(STYLE_KEYS)
    if unknown:
        raise ValueError(f"样式里没有 {sorted(unknown)[0]}；能改：{'、'.join(STYLE_KEYS)}")
    merged = dict(base or {})
    for key, value in style.items():
        if value is None:
            merged.pop(key, None)
            continue
        if key == "color":
            if not (isinstance(value, str) and (value in DRAWING_COLOR_PRESETS or _COLOR.match(value))):
                raise ValueError("颜色要是 " + "/".join(DRAWING_COLOR_PRESETS) + " 之一，或 #rrggbb")
            merged[key] = value.lower() if value.startswith("#") else value
        elif key == "width":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4:
                raise ValueError("粗细要是 1–4 的整数")
            merged[key] = value
        elif key == "dash":
            if value not in DRAWING_DASHES:
                raise ValueError("线型要是 solid（实线）、dashed（虚线）或 dotted（点线）")
            merged[key] = value
        elif key == "period":
            if isinstance(value, bool) or value not in DRAWING_PERIODS:
                raise ValueError("period 要是画线时的图表周期秒数（0 = 分时）")
            merged[key] = int(value)
        else:
            if not isinstance(value, bool):
                raise ValueError(f"{key} 要是 true 或 false")
            merged[key] = value
    if merged.get("allPeriods") is False and (isinstance(merged.get("period"), bool)
                                              or merged.get("period") not in DRAWING_PERIODS):
        raise ValueError("只在绘制周期显示时，要同时填写 period（图表周期秒数，0 = 分时）")
    return merged


async def create_drawing(app: Any, *, user_id: str, symbol: str, kind: str,
                         points: Any, label: str = "", style: Any = None) -> dict[str, Any]:
    """The creation rules, shared by the HTTP route and the AI tool (the tool goes through the
    route). Every refusal is a Chinese sentence; the caller maps ValueError → 422. In a replay
    child (2026-10-03) the catalog is the replay's own — only the replayed contract is drawable,
    and the lines live in the replay child's store."""
    symbol = str(symbol or "").strip()
    if not symbol:
        raise ValueError("要先选一个合约")
    replay = getattr(app.state, "replay_context", None)
    if replay is not None:
        try:
            catalog = await asyncio.to_thread(replay.provider.instrument_catalog)
        except Exception as error:
            raise ValueError(f"现在读不到回放的行情，稍后再试（{error}）") from error
    else:
        shared = getattr(app.state, "shared_market", None)
        if shared is None:
            raise ValueError("行情服务还没就绪，稍后再试")
        try:
            catalog = await shared.instruments()
        except Exception as error:
            raise ValueError(f"现在读不到合约目录（{error}），稍后再试") from error
    item = next((entry for entry in catalog.get("items", [])
                 if str(entry.get("symbol", "")).upper() == symbol.upper()), None)
    if item is None:
        if replay is not None:
            raise ValueError("回放里只能画本次回放的合约；要画其他合约，回主窗口")
        raise ValueError(f"目录里没有合约 {symbol}；换一个实际合约，或说品种名让 AI 找主力")
    parsed = parse_points(kind, points)
    text = check_label(label, kind)
    checked = parse_style(style)
    created = app.state.store.create_chart_drawing(
        user_id=user_id, symbol=str(item["symbol"]), kind=kind, points=parsed,
        label=text, style=checked, max_per_symbol=MAX_DRAWINGS_PER_SYMBOL)
    if isinstance(created, str):
        raise ValueError(created)
    return created


def anchors_of(drawing: dict[str, Any]) -> list[dict[str, Any]]:
    """Every drawing's points as anchors (the first-version shapes converted; t 0 = no time)."""
    kind = str(drawing.get("kind"))
    points = drawing.get("points") or {}
    if kind == "hline":
        return [{"t": 0, "p": points.get("price")}]
    if kind == "zone":
        return [{"t": 0, "p": points.get("low")}, {"t": 0, "p": points.get("high")}]
    if kind == "trendline":
        return [{"t": points.get("t1"), "p": points.get("p1")}, {"t": points.get("t2"), "p": points.get("p2")}]
    return list(points.get("anchors") or [])


def level_prices(drawing: dict[str, Any]) -> list[tuple[float, float]]:
    """The (ratio, price) pairs a 黄金分割 / 百分比线 / 江恩八线 draws. 黄金分割 counts from the
    second point (0) back to the first (1, a retracement); 百分比线 and 江恩八线 from the first."""
    kind = str(drawing.get("kind"))
    anchors = anchors_of(drawing)
    if kind not in {"fib", "percent", "gann"} or len(anchors) < 2:
        return []
    start, end = float(anchors[0]["p"]), float(anchors[1]["p"])
    if kind == "gann":
        ratios = [step / 8 for step in range(9)]
    else:
        ratios = list((drawing.get("points") or {}).get("levels") or DEFAULT_LEVELS[kind])
    if kind == "fib":
        return [(ratio, end + (start - end) * ratio) for ratio in ratios]
    return [(ratio, start + (end - start) * ratio) for ratio in ratios]


def _clock(seconds: Any) -> str:
    try:
        return datetime.fromtimestamp(int(seconds), _CHINA).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(seconds)


def _ratio_text(ratio: float) -> str:
    return f"{price_text(round(ratio * 100, 2))}%"


def _line_body(drawing: dict[str, Any]) -> str:
    """The coordinates as one readable phrase (“3100 一条水平线”)."""
    kind = str(drawing.get("kind"))
    points = drawing.get("points") or {}
    anchors = anchors_of(drawing)
    first = price_text(anchors[0].get("p")) if anchors else ""
    last = price_text(anchors[-1].get("p")) if anchors else ""
    prices = [float(item["p"]) for item in anchors if isinstance(item.get("p"), (int, float))]
    span = f"{price_text(min(prices))}–{price_text(max(prices))}" if prices else ""
    if kind == "hline":
        return f"{price_text(points.get('price'))} 一条水平线"
    if kind == "trendline":
        return f"从 {price_text(points.get('p1'))} 到 {price_text(points.get('p2'))} 的趋势线"
    if kind == "zone":
        return f"{price_text(points.get('low'))}–{price_text(points.get('high'))} 的区间"
    if kind == "ray":
        return f"从 {first} 经 {last} 的射线"
    if kind == "extended":
        return f"过 {first} 和 {last} 的直线"
    if kind == "hray":
        return f"{first} 的水平射线（{_clock(anchors[0].get('t'))} 起向右）"
    if kind == "vline":
        return f"{_clock(anchors[0].get('t'))} 的垂直线"
    if kind == "channel":
        return f"{span} 的平行通道"
    if kind == "polyline":
        return f"{len(anchors)} 个点的折线（{first} → {last}）"
    if kind == "rect_zone":
        return f"{span} 的矩形区间（{_clock(anchors[0].get('t'))}–{_clock(anchors[-1].get('t'))}）"
    if kind in {"fib", "percent", "gann"}:
        levels = "、".join(f"{_ratio_text(ratio) if kind != 'gann' else f'{round(ratio * 8)}/8'} {price_text(round(price, 6))}"
                          for ratio, price in level_prices(drawing))
        return f"{first}→{last} 的{KIND_WORDS[kind]}（{levels}）"
    if kind in {"rect", "ellipse", "triangle"}:
        return f"{span} 的{KIND_WORDS[kind]}"
    if kind == "text":
        return f"{first} 处的文字"
    if kind == "arrow":
        return f"从 {first} 指向 {last} 的箭头"
    if kind == "price_label":
        return f"{first} 的价格标签"
    if kind == "measure" and len(anchors) == 2:
        start, end = float(anchors[0]["p"]), float(anchors[1]["p"])
        diff = end - start
        pct = diff / start * 100 if start else 0.0
        sign = "+" if diff >= 0 else "-"
        return f"测量 {first}→{last}（{sign}{price_text(round(abs(diff), 6))}，{sign}{abs(pct):.2f}%）"
    return KIND_WORDS.get(kind, kind)


def reference_price(drawing: dict[str, Any]) -> Any:
    """The price the receipt measures the market against (被突破了吗): a level's own price, a zone's
    upper edge, a line's second point; None for shapes and notes that are not a price level."""
    kind = str(drawing.get("kind"))
    points = drawing.get("points") or {}
    if kind == "hline":
        return points.get("price")
    if kind == "zone":
        return points.get("high")
    if kind == "trendline":
        return points.get("p2")
    anchors = anchors_of(drawing)
    if kind in {"hray", "price_label"} and anchors:
        return anchors[0].get("p")
    if kind in {"ray", "extended", "rect_zone"} and len(anchors) >= 2:
        return anchors[1].get("p") if kind != "rect_zone" else max(float(anchors[0]["p"]), float(anchors[1]["p"]))
    return None


# 碰到这条线就提醒 (改版 2026-10-31): the kinds that mark a price level (the client's ALERTABLE_KINDS). A level or a zone
# holds one price (its reference price); a sloped line's price moves with time.
SLOPED_KINDS = frozenset({"trendline", "ray", "extended"})
ALERTABLE_KINDS = frozenset({"hline", "hray", "price_label", "zone", "rect_zone"}) | SLOPED_KINDS


def line_price_at(drawing: dict[str, Any], now: int, trading_seconds: Any) -> float | None:
    """Where the line is at `now` (seconds): a level's own price, a sloped line's value along its two points. The
    chart lays bars side by side, so the slope runs over trading time (`trading_seconds(a, b)` in seconds, None
    when unknown), not wall time — a night or a weekend does not move the line. None where the line is not drawn
    (a segment that has ended and is not extended) or cannot be worked out."""
    kind = str(drawing.get("kind"))
    if kind not in ALERTABLE_KINDS:
        return None
    if kind not in SLOPED_KINDS:
        reference = reference_price(drawing)
        return None if reference is None else float(reference)
    anchors = anchors_of(drawing)
    try:
        (t1, p1), (t2, p2) = ((int(item["t"]), float(item["p"])) for item in anchors[:2])
    except (KeyError, TypeError, ValueError):
        return None
    style = drawing.get("style") or {}
    # A ray runs on past its second point; a segment ends at its points unless its style extends it.
    right = kind == "extended" or style.get("extendRight") or (kind == "ray" and t2 > t1)
    left = kind == "extended" or style.get("extendLeft") or (kind == "ray" and t2 < t1)
    if (now > max(t1, t2) and not right) or (now < min(t1, t2) and not left):
        return None
    span, elapsed = trading_seconds(t1, t2), trading_seconds(t1, now)
    if not span or elapsed is None:
        return None
    return p1 + (p2 - p1) * elapsed / span


def drawing_line(drawing: dict[str, Any], *, last_price: Any = None) -> str:
    """The one-line receipt the UI and the AI tools read, in the alert-line shape; with a live
    price it says where the market sits relative to the line (“现价 3112，在 3100 上方 12”)."""
    line = f"{drawing.get('symbol')} {_line_body(drawing)}"
    if drawing.get("label"):
        line += f"（{drawing['label']}）"
    style = drawing.get("style") or {}
    marks = [word for key, word in (("hidden", "已隐藏"), ("locked", "已锁定")) if style.get(key)]
    if marks:
        line += "，" + "、".join(marks)
    reference = reference_price(drawing)
    if last_price is None or reference is None:
        return line
    try:
        last = Decimal(str(last_price))
        anchor = Decimal(str(reference))
    except (InvalidOperation, ValueError, TypeError):
        return line
    if last == anchor:
        return f"{line}，现价正好在 {price_text(anchor)}"
    where = "上方" if last > anchor else "下方"
    return f"{line}，现价 {price_text(last)}，在 {price_text(anchor)} {where} {price_text(abs(last - anchor))}"
