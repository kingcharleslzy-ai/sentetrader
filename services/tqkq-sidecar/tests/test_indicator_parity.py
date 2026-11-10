"""The strategy engine's ATR / BOLL / CCI / RSI (strategy_rules.indicator_values — also what the AI's market
reading reports) must equal tqsdk.ta.

tqsdk (and pandas, which tqsdk.ta is built on) only exists in the sidecar
environment, so the parity test lives here and imports the Runtime source
straight from its src tree. Tolerance 1e-6 pins the definitions: sample std
(ddof=1) for BOLL, the plain mean of true ranges with tr[0] undefined for ATR, mean absolute
deviation for CCI — values and NaN placement over the whole series. RSI is the same recursion from
another seed (Wilder's mean of the first N changes against tqsdk's sma(x, n, 1) from the first one),
so it is compared once the seed has faded.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# tqsdk is the sidecar's optional `tqsdk` extra: CI installs the project without
# extras, so the parity check skips there and runs wherever the extra is present
# (the developer sync `uv sync --all-extras`, and the release image).
pd = pytest.importorskip("pandas", reason="tqsdk extra not installed")
ta = pytest.importorskip("tqsdk.ta", reason="tqsdk extra not installed")
# The engine module needs pydantic, a Runtime dependency the sidecar environment does not carry: the check
# runs where both are importable (the Runtime's site-packages appended to PYTHONPATH) and skips elsewhere.
pytest.importorskip("pydantic", reason="Runtime dependencies not importable from the sidecar environment")

RUNTIME_SRC = Path(__file__).resolve().parents[2] / "trader-runtime" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from trader_runtime.strategy_rules import Operand, indicator_values  # noqa: E402

RSI_WARMUP = 300   # the seeds' difference shrinks by 13/14 a bar: about 1 point at 60 bars, under 1e-7 here


def bars(count: int) -> tuple[list[float], list[float], list[float]]:
    """A non-trivial deterministic series (no closed-form shortcuts)."""
    highs, lows, closes = [], [], []
    for index in range(count):
        close = 100.0 + 3.0 * math.sin(index / 3.1) + index * 0.05
        high = close + 1.5 + math.cos(index / 2.3)
        low = close - 1.2 - math.sin(index / 1.7)
        closes.append(round(close, 4))
        highs.append(round(high, 4))
        lows.append(round(low, 4))
    return highs, lows, closes


def nearly_equal(expected: float, actual: float | None, tolerance: float) -> bool:
    if actual is None:
        return math.isnan(expected)
    return math.isclose(expected, actual, rel_tol=0, abs_tol=tolerance)


def test_boll_atr_rsi_cci_match_tqsdk_ta():
    count = 400
    highs, lows, closes = bars(count)
    frame = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    series = [{"high": high, "low": low, "close": close} for high, low, close in zip(highs, lows, closes, strict=True)]

    def ours(indicator: str, period: int) -> list[float | None]:
        return indicator_values(series, Operand(indicator=indicator, period=period))

    expected_boll = ta.BOLL(frame, 20, 2)
    for name, indicator in (("mid", "boll_mid"), ("top", "boll_up"), ("bottom", "boll_down")):
        actual = ours(indicator, 20)
        for index, value in enumerate(expected_boll[name].tolist()):
            assert nearly_equal(value, actual[index], 1e-6), (name, index)

    for label, expected, actual in (("atr", ta.ATR(frame, 14)["atr"].tolist(), ours("atr", 14)),
                                    ("cci", ta.CCI(frame, 14)["cci"].tolist(), ours("cci", 14))):
        for index, value in enumerate(expected):
            assert nearly_equal(value, actual[index], 1e-6), (label, index)

    expected_rsi = ta.RSI(frame, 14)["rsi"].tolist()
    ours_rsi = ours("rsi", 14)
    for index in range(RSI_WARMUP, count):
        assert nearly_equal(expected_rsi[index], ours_rsi[index], 1e-6), index
