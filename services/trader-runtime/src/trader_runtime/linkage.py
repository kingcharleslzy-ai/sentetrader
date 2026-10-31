"""关联品种联动度 (2026-10-25): how tightly a product and its related products move together, from daily closes.

Adapted from HKUDS/Vibe-Trading (MIT, commit 41f333b6) `agent/backtest/regime.py`: edge density (the share of
product pairs whose rolling |correlation| of daily returns clears 0.5) and its hysteresis state machine (抱团 opens
at a smoothed density of 0.65 and closes at 0.45; the smoothing is trailing, never centred, so no future day is
read). Rewritten to the standard library — the packaged desktop Runtime ships without numpy and pandas — and the
correlation window shortened from 60 to 20 bars: the research desk reads 60 daily bars per contract.
"""
from __future__ import annotations

import math
from itertools import combinations

WINDOW = 20
EDGE = 0.5
SMOOTH = 5
ENTER, EXIT = 0.65, 0.45


def correlation(left: list[float], right: list[float]) -> float | None:
    """Pearson correlation; None when either side does not move (an undefined pair is missing evidence, not 0)."""
    count = len(left)
    if count < 2 or count != len(right):
        return None
    mean_left, mean_right = sum(left) / count, sum(right) / count
    spread_left = math.fsum((value - mean_left) ** 2 for value in left)
    spread_right = math.fsum((value - mean_right) ** 2 for value in right)
    if spread_left <= 0 or spread_right <= 0:
        return None
    joint = math.fsum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True))
    return joint / math.sqrt(spread_left * spread_right)


def aligned_returns(closes: dict[str, dict[str, float]]) -> tuple[list[str], dict[str, list[float]]]:
    """Daily returns on the trading days every series shares (a day one contract lacks is dropped for all)."""
    shared = sorted(set.intersection(*(set(series) for series in closes.values()))) if closes else []
    returns = {name: [series[after] / series[before] - 1 for before, after in zip(shared, shared[1:], strict=False)
                      if series[before]] for name, series in closes.items()}
    if any(len(values) != len(shared) - 1 for values in returns.values()):
        return [], {}  # a zero close: no honest return for that day
    return shared[1:], returns


def edge_density(returns: dict[str, list[float]], window: int = WINDOW) -> list[float | None]:
    """Per day, the share of pairs with |ρ| ≥ EDGE over the trailing `window` returns; None in the warm-up and
    whenever any pair is undefined (the whole basket must be measurable)."""
    names = sorted(returns)
    pairs = list(combinations(names, 2))
    length = len(returns[names[0]]) if names else 0
    out: list[float | None] = [None] * length
    if not pairs:
        return out
    for end in range(window, length + 1):
        values = [correlation(returns[a][end - window:end], returns[b][end - window:end]) for a, b in pairs]
        if all(value is not None for value in values):
            out[end - 1] = sum(1 for value in values if value is not None and abs(value) >= EDGE) / len(pairs)
    return out


def regimes(density: list[float | None], smooth: int = SMOOTH) -> list[tuple[float | None, bool]]:
    """(trailing-mean density, fused) per day: fused opens at ENTER and holds until the smoothed density falls to EXIT."""
    fused = False
    out: list[tuple[float | None, bool]] = []
    for index in range(len(density)):
        seen = [value for value in density[max(0, index - smooth + 1):index + 1] if value is not None]
        smoothed = sum(seen) / len(seen) if seen else None
        if smoothed is not None:
            if not fused and smoothed >= ENTER:
                fused = True
            elif fused and smoothed <= EXIT:
                fused = False
        out.append((smoothed, fused))
    return out
