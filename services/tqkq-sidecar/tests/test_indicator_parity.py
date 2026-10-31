"""The Runtime's pure-Python BOLL/ATR/RSI/CCI must equal tqsdk.ta exactly.

tqsdk (and pandas, which tqsdk.ta is built on) only exists in the sidecar
environment, so the parity test lives here and imports the Runtime source
straight from its src tree. Tolerance 1e-6 pins the definitions: sample std
(ddof=1) for BOLL, tr[0] undefined for ATR, Wilder-style recursive sma(x, n, 1)
for RSI, mean absolute deviation for CCI; NaN placement must match too.
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

RUNTIME_SRC = Path(__file__).resolve().parents[2] / "trader-runtime" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from trader_runtime.indicators import atr, boll, cci, rsi  # noqa: E402


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
    count = 300
    highs, lows, closes = bars(count)
    frame = pd.DataFrame({"high": highs, "low": lows, "close": closes})

    expected_boll = ta.BOLL(frame, 20, 2)
    ours_boll = boll(closes, 20, 2)
    for name in ("mid", "top", "bottom"):
        for index, value in enumerate(expected_boll[name].tolist()):
            assert nearly_equal(value, ours_boll[name][index], 1e-6), (name, index)

    expected_atr = ta.ATR(frame, 14)["atr"].tolist()
    ours_atr = atr(highs, lows, closes, 14)
    for index, value in enumerate(expected_atr):
        assert nearly_equal(value, ours_atr[index], 1e-6), index

    expected_rsi = ta.RSI(frame, 14)["rsi"].tolist()
    ours_rsi = rsi(closes, 14)
    for index, value in enumerate(expected_rsi):
        assert nearly_equal(value, ours_rsi[index], 1e-6), index

    expected_cci = ta.CCI(frame, 14)["cci"].tolist()
    ours_cci = cci(highs, lows, closes, 14)
    for index, value in enumerate(expected_cci):
        assert nearly_equal(value, ours_cci[index], 1e-6), index
