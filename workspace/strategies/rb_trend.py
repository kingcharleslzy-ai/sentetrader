"""Synthetic strategy fixture for AI Trader engineering tasks.

It evaluates a signal only. It has no account, broker or order execution access.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TrendInputs:
    price_above_short_average: bool
    medium_trend_aligned: bool
    volume_ratio: Decimal


@dataclass(frozen=True)
class TrendParameters:
    volume_threshold: Decimal = Decimal("1.00")


def signal_ready(inputs: TrendInputs, parameters: TrendParameters | None = None) -> bool:
    configured = parameters or TrendParameters()
    return (
        inputs.price_above_short_average
        and inputs.medium_trend_aligned
        and inputs.volume_ratio >= configured.volume_threshold
    )
