"""Deterministic order boundaries shared by the order service and the AI autonomy loop.

These checks decide whether a simulated order may be prepared or a stop may
fire: the quote must be current, the price must sit on the instrument's tick,
numeric inputs must be finite. They own no state and touch no gateway.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .risk import decimal_value
from .trading_clock import wall_clock

MAX_FUTURE_QUOTE_SKEW_SECONDS = 1.0


class SimulationOrderError(ValueError):
    pass


def finite_positive(value: Any, code: str) -> Decimal:
    try:
        result = decimal_value(value)
    except Exception as error:
        raise SimulationOrderError(code) from error
    if not result.is_finite() or result <= 0:
        raise SimulationOrderError(code)
    return result


def price_on_tick(price: Decimal, tick: Decimal) -> bool:
    """True when the price is an exact multiple of the instrument's tick."""
    return (price / tick) == (price / tick).to_integral_value()


def quote_is_fresh(market: dict[str, Any], *, allow_fixture: bool, now: datetime | None = None) -> None:
    """Raise unless the market snapshot is live, uncached and at most 15 s old by both clocks."""
    if market.get("marketSessionStatus") == "closed":
        raise SimulationOrderError("market_session_closed")
    accepted = {"normal"} | ({"simulated"} if allow_fixture else set())
    if market.get("dataStatus") not in accepted or bool(market.get("isCached")):
        raise SimulationOrderError("quote_not_current")
    try:
        age = float(market.get("ageSeconds"))  # type: ignore[arg-type]  # None is rejected below
    except (TypeError, ValueError) as error:
        raise SimulationOrderError("quote_age_missing") from error
    if not math.isfinite(age) or age < 0 or age > 15:
        raise SimulationOrderError("quote_stale")
    quote_datetime = str(market.get("quoteDatetime") or "").strip()
    if not quote_datetime:
        raise SimulationOrderError("quote_datetime_missing")
    try:
        quoted_at = datetime.fromisoformat(quote_datetime.replace("Z", "+00:00"))
    except ValueError as error:
        raise SimulationOrderError("quote_datetime_invalid") from error
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=timezone(timedelta(hours=8)))
    quote_age = ((now or wall_clock()) - quoted_at.astimezone(UTC)).total_seconds()
    if (
        not math.isfinite(quote_age)
        or quote_age < -MAX_FUTURE_QUOTE_SKEW_SECONDS
        or quote_age > 15
    ):
        raise SimulationOrderError("quote_stale")
