"""价差序列与价差持仓的推导 (2026-09-29 第一期).

A spread plan (``SpreadSpec`` in strategy_rules) trades two concrete contracts of one account as
one unit — 做多价差 = 买 A 卖 B, 做空价差 = 卖 A 买 B — and judges its rules on the spread series
synthesized from the two legs' same-period closed K-lines, inner-joined by ``datetime``: a bar one
leg lacks is not judged. A joined bar carries both legs' opens and closes (a backtest's fills read
them) and takes its high / low as the max / min of its own open and close, which is why spread
plans may only read indicators that never look at high / low (``SPREAD_INDICATORS``).

Live reads each leg's workbench (255 completed bars plus the forming one). The joined series ends
with one tail bar — the true forming spread bar when both legs' forming bars share a datetime, else
a valueless sentinel — so ``evaluate_rules``' [:-1] drops exactly the tail and the last *completed*
joined bar stays the decision bar whichever leg lags. A backtest joins the legs' whole series and,
per decision bar, slices the window the live monitor would see: the join of each leg's last
``window`` completed bars (``joined_window_start``), so a value at a bar is the same in any window.
"""
from __future__ import annotations

from bisect import bisect_left

from .strategy_rules import (
    SpreadSpec,
    StrategyPlan,
    StrategyRuleError,
    evaluate_rules,
    plan_timeframes,
    workbench_read_error,
)


def spread_price(kind: str, a: float, b: float, weight_a: float, weight_b: float) -> float:
    if kind == "ratio":
        if b == 0:
            raise StrategyRuleError("spread_ratio_zero", "B腿价格为 0，无法计算比值价差")
        return a / b
    return a * weight_a - b * weight_b


def join_legs(spec: SpreadSpec, bars_a: list[dict], bars_b: list[dict]) -> tuple[list[dict], list[int], list[int]]:
    """The two legs' completed bars inner-joined by ``datetime`` (oldest first), with each joined
    bar's index into each leg's list: (spread bars, A indices, B indices)."""
    bars: list[dict] = []
    ja: list[int] = []
    jb: list[int] = []
    weight_a, weight_b = spec.legs[0].weight, spec.legs[1].weight
    at = bt = 0
    while at < len(bars_a) and bt < len(bars_b):
        ta, tb = int(bars_a[at]["datetime"]), int(bars_b[bt]["datetime"])
        if ta != tb:
            if ta < tb:
                at += 1
            else:
                bt += 1
            continue
        a, b = bars_a[at], bars_b[bt]
        opening = spread_price(spec.kind, float(a["open"]), float(b["open"]), weight_a, weight_b)
        closing = spread_price(spec.kind, float(a["close"]), float(b["close"]), weight_a, weight_b)
        bars.append({"datetime": ta, "open": opening, "close": closing,
                     "high": max(opening, closing), "low": min(opening, closing), "volume": 0.0,
                     "tradingDay": a.get("tradingDay") or b.get("tradingDay"),
                     "legA": {"open": float(a["open"]), "close": float(a["close"])},
                     "legB": {"open": float(b["open"]), "close": float(b["close"])}})
        ja.append(at)
        jb.append(bt)
        at += 1
        bt += 1
    return bars, ja, jb


def joined_window_start(ja: list[int], jb: list[int], k: int, window: int) -> int:
    """The first joined index of the window that judges joined bar ``k``: the join of each leg's
    last ``window`` completed bars — a leg's own bars the other leg lacks still count toward its
    window — which is exactly what live sees on the two legs' 255-bar workbenches."""
    start_a = max(0, ja[k] + 1 - window)
    start_b = max(0, jb[k] + 1 - window)
    return max(bisect_left(ja, start_a), bisect_left(jb, start_b))


def spread_workbenches(spec: SpreadSpec, workbench_a: dict, workbench_b: dict) -> dict:
    """Two live legs' workbenches (same period) as the one spread workbench ``evaluate_rules`` reads:
    the joined completed bars plus one tail bar. The quote (and its trading schedule) is A's."""
    bars_a, bars_b = list(workbench_a.get("bars") or []), list(workbench_b.get("bars") or [])
    if not bars_a or not bars_b:
        raise StrategyRuleError("strategy_bars_insufficient")
    joined, _, _ = join_legs(spec, bars_a[:-1], bars_b[:-1])
    forming_a, forming_b = bars_a[-1], bars_b[-1]
    if int(forming_a["datetime"]) == int(forming_b["datetime"]):
        tail, _, _ = join_legs(spec, [forming_a], [forming_b])
        tail_bar = tail[0]
    else:
        # A leg lags: the sentinel's values are never read (the tail is never a signal input), it
        # only keeps the last completed joined bar in the judged series.
        tail_bar = {"datetime": max(int(forming_a["datetime"]), int(forming_b["datetime"])),
                    "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0}
    quote = workbench_a.get("quote") if isinstance(workbench_a.get("quote"), dict) else {}
    return {"bars": joined + [tail_bar], "quote": quote}


def spread_evaluation(provider, plan: StrategyPlan, *, require_fresh: bool = True) -> dict:
    """``evaluate_plan`` for a spread plan: both legs' workbenches per period (each leg is checked
    fresh on its own), the spread series synthesized, then the ordinary rule evaluation on it."""
    spec = plan.spread
    assert spec is not None
    workbenches: dict[int, dict] = {}
    for timeframe in plan_timeframes(plan):
        period = "日线" if timeframe == 86400 else f"{timeframe // 60} 分钟"
        legs: dict[int, dict] = {}
        for index, leg in enumerate(spec.legs):
            try:
                item = provider.market_workbench(leg.symbol, timeframe)
            except (KeyError, ValueError, RuntimeError) as error:
                raise workbench_read_error(
                    error, f"{'AB'[index]}腿{leg.symbol} {period}行情读取失败，请刷新行情后重试") from error
            if require_fresh and (item.get("dataStatus") not in {"normal", "simulated"} or item.get("isCached") is True
                    or float(item.get("ageSeconds", 999)) > 15):
                raise StrategyRuleError("strategy_market_not_fresh",
                                        f"{'AB'[index]}腿{leg.symbol} {period}：strategy_market_not_fresh")
            legs[index] = item
        try:
            workbenches[timeframe] = spread_workbenches(spec, legs[0], legs[1])
        except StrategyRuleError as error:
            raise StrategyRuleError(error.code, f"{period}：{error}") from error
    return evaluate_rules(plan, workbenches)


def spread_position(spec: SpreadSpec, positions: list[dict]) -> dict:
    """The strategy's spread position from the account's positions: 做多价差 = A long + B short (做空
    = the other way). Both legs at or above their lots in the spread's direction → that many units
    (``min`` of the legs' own quotients) with the entry spread from the legs' average prices;
    anything else reads flat — the reminder never advises repairing a partial spread."""
    def held(symbol: str, direction: str) -> tuple[int, float | None]:
        volume, weighted, total = 0, 0.0, 0
        for item in positions:
            if not isinstance(item, dict) or str(item.get("symbol", "")).upper() != symbol.upper() \
                    or item.get("direction") != direction:
                continue
            part = max(0, int(item.get("volume") or 0))
            price = item.get("openPrice")
            volume += part
            if part and isinstance(price, (int, float)):
                weighted += float(price) * part
                total += part
        return volume, (weighted / total if total else None)

    leg_a, leg_b = spec.legs
    for direction, a_side, b_side, other_a, other_b in (
            ("long", "long", "short", held(leg_a.symbol, "short"), held(leg_b.symbol, "long")),
            ("short", "short", "long", held(leg_a.symbol, "long"), held(leg_b.symbol, "short"))):
        a_volume, a_price = held(leg_a.symbol, a_side)
        b_volume, b_price = held(leg_b.symbol, b_side)
        if a_volume >= leg_a.lots and b_volume >= leg_b.lots and not other_a[0] and not other_b[0]:
            entry = None if a_price is None or b_price is None else spread_price(
                spec.kind, a_price, b_price, leg_a.weight, leg_b.weight)
            return {"direction": direction, "volume": min(a_volume // leg_a.lots, b_volume // leg_b.lots),
                    "entry_price": entry}
    return {"direction": None, "volume": 0, "entry_price": None}


def spread_legs_words(spec: SpreadSpec, action: str, units: int) -> str:
    """An alert's leg breakdown: 买 A x 手、卖 B y 手 for one spread unit × `units`."""
    leg_a, leg_b = spec.legs
    first_buy = action in {"open_long", "close_short"}
    a_verb, b_verb = ("买", "卖") if first_buy else ("卖", "买")
    return f"{a_verb}{leg_a.symbol} {leg_a.lots * units} 手、{b_verb}{leg_b.symbol} {leg_b.lots * units} 手"


def spread_words(spec: SpreadSpec) -> str:
    """The plan's spread definition in plain Chinese: A − B（或 A×2 − B、A ÷ B）."""
    leg_a, leg_b = spec.legs
    if spec.kind == "ratio":
        return f"{leg_a.symbol} ÷ {leg_b.symbol}"
    if (leg_a.weight, leg_b.weight) == (1, 1):
        return f"{leg_a.symbol} − {leg_b.symbol}"
    return f"{leg_a.symbol}×{leg_a.weight:g} − {leg_b.symbol}×{leg_b.weight:g}"


def spread_value_text(value: float) -> str:
    """One spread value in words: up to 6 decimals, trailing zeros trimmed (a ratio needs them)."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"
