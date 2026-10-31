"""Shared primitives for legacy strategy previews and historical validation.

boll / atr / rsi / cci are pure-Python implementations matching tqsdk.ta
(tqsdk 3.10.2) exactly: same windows, same NaN placement, same sample std (ddof=1)
and the same recursive sma(x, n, 1) seeding for RSI. The parity is pinned by a
test that feeds one K-line series through both. Missing values are None.
"""


def ema(values: list[float], period: int) -> list[float]:
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * multiplier + result[-1] * (1 - multiplier))
    return result


def cross_signal(current_fast: float, current_slow: float,
                 previous_fast: float, previous_slow: float) -> str:
    if current_fast > current_slow and previous_fast <= previous_slow:
        return "buy"
    if current_fast < current_slow and previous_fast >= previous_slow:
        return "sell"
    return "wait"


def rolling_mean(values: list[float | None], n: int) -> list[float | None]:
    """SMA over complete windows only; None inside a window propagates (tqsdk
    rolling().mean() leaves a NaN when any member is NaN)."""
    result: list[float | None] = []
    window: list[float] = []
    for value in values:
        if value is None:
            window.clear()
            result.append(None)
            continue
        window.append(value)
        if len(window) > n:
            window.pop(0)
        result.append(sum(window) / len(window) if len(window) == n else None)
    return result


def rolling_std(values: list[float | None], n: int) -> list[float | None]:
    """Sample standard deviation (ddof=1), pandas rolling(n).std() default."""
    result: list[float | None] = []
    window: list[float] = []
    for value in values:
        if value is None:
            window.clear()
            result.append(None)
            continue
        window.append(value)
        if len(window) > n:
            window.pop(0)
        if len(window) < n:
            result.append(None)
            continue
        mean = sum(window) / n
        variance = sum((item - mean) ** 2 for item in window) / (n - 1)
        result.append(variance ** 0.5)
    return result


def boll(closes: list[float | None], n: int = 20, p: int = 2) -> dict[str, list[float | None]]:
    """BOLL(n, p): mid = SMA(n), top/bottom = mid ± p * std(n) (sample std)."""
    mid = rolling_mean(closes, n)
    std = rolling_std(closes, n)
    top = [None if m is None or s is None else m + p * s for m, s in zip(mid, std, strict=True)]
    bottom = [None if m is None or s is None else m - p * s for m, s in zip(mid, std, strict=True)]
    return {"mid": mid, "top": top, "bottom": bottom}


def atr(highs: list[float | None], lows: list[float | None],
        closes: list[float | None], n: int = 14) -> list[float | None]:
    """ATR(n): mean of true range, where tr[0] is undefined (no previous close)."""
    tr: list[float | None] = []
    for index, (high, low) in enumerate(zip(highs, lows, strict=True)):
        previous = closes[index - 1] if index else None
        if index == 0:
            tr.append(None)  # tqsdk: tr[0] = NaN
            continue
        if high is None or low is None or previous is None:
            tr.append(None)
            continue
        tr.append(max(high - low, abs(previous - high), abs(previous - low)))
    return rolling_mean(tr, n)


def rsi(closes: list[float | None], n: int = 14) -> list[float | None]:
    """RSI(n) exactly as tqsdk.ta.RSI: sma(max(delta, 0), n, 1) divided by
    sma(|delta|, n, 1), where sma(x, n, 1) is an EWM with alpha = 1/n
    (adjust=False). Note the two seeds differ: tqsdk builds the numerator with
    np.where(delta > 0, delta, 0), so its first element is a real 0, while the
    denominator's |delta| starts undefined (NaN) and seeds at the first change.
    rsi[0] is NaN; 0/0 stays NaN instead of 100. The parity test pins this."""
    def sma_recursive(values: list[float | None]) -> list[float | None]:
        alpha = 1 / n
        result: list[float | None] = []
        state: float | None = None
        for value in values:
            if value is None:
                result.append(None)
                continue
            state = value if state is None else value * alpha + state * (1 - alpha)
            result.append(state)
        return result

    ups: list[float | None] = [0.0]
    absolutes: list[float | None] = [None]
    for index in range(1, len(closes)):
        current, previous = closes[index], closes[index - 1]
        if current is None or previous is None:
            ups.append(None)
            absolutes.append(None)
            continue
        change = current - previous
        ups.append(max(change, 0.0))
        absolutes.append(abs(change))
    average_up = sma_recursive(ups)
    average_absolute = sma_recursive(absolutes)
    return [
        None if up is None or absolute is None or absolute == 0
        else 100.0 * up / absolute
        for up, absolute in zip(average_up, average_absolute, strict=True)
    ]


def cci(highs: list[float | None], lows: list[float | None],
        closes: list[float | None], n: int = 14) -> list[float | None]:
    """CCI(n): (typical - SMA(typical, n)) / (0.015 * mean absolute deviation)."""
    typical: list[float | None] = [
        None if high is None or low is None or close is None else (high + low + close) / 3
        for high, low, close in zip(highs, lows, closes, strict=True)
    ]
    mean = rolling_mean(typical, n)
    result: list[float | None] = []
    window: list[float] = []
    for index, value in enumerate(typical):
        if value is None:
            window.clear()
            result.append(None)
            continue
        window.append(value)
        if len(window) > n:
            window.pop(0)
        average = mean[index]
        if average is None or len(window) < n:
            result.append(None)
            continue
        # Offsets from the window's first value (#998): a flat window of decimals (4123.2 × 14) leaves a rounding
        # residue in the running mean that the deviation divides into ±66.7; its offsets are exactly 0.
        base = window[0]
        offsets = [item - base for item in window]
        offset_mean = sum(offsets) / n
        deviation = sum(abs(item - offset_mean) for item in offsets) / n
        # A flat window reads 0 — the chart's and the strategy engine's value — not None, which the AI reading
        # would report as 样本不足 although the sample is complete.
        result.append(0.0 if deviation == 0 else (value - base - offset_mean) / (0.015 * deviation))
    return result
