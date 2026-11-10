"""Shared primitives for legacy (schemaVersion 1) strategy previews and historical validation.
Every other indicator is strategy_rules.indicator_values."""


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
