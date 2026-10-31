"""Trading time is injectable; authentication, leases and audit time stay wall-clock."""
from collections.abc import Callable
from datetime import UTC, datetime

TradingClock = Callable[[], datetime]


def wall_clock() -> datetime:
    return datetime.now(UTC)


def fill_trading_day(trade: dict) -> str:
    """Require the provider's exchange-calendar annotation, never today's date."""
    value = trade.get("tradingDay")
    try:
        if not isinstance(value, str) or datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
            raise ValueError()
    except ValueError as error:
        raise ValueError("trade_trading_day_unavailable") from error
    return value
