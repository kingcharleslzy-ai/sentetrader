from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RiskPolicy:
    max_loss_per_trade: Decimal | None = None
    allowed_symbols: tuple[str, ...] | None = None
    max_daily_loss: Decimal | None = None
    no_new_positions_after: str | None = None
    timezone: str = "Asia/Shanghai"
    version: int = 0


@dataclass(frozen=True)
class InstrumentSpec:
    contract_multiplier: Decimal
    price_tick: Decimal


class RiskPolicyProvider:
    """Trusted runtime policy. Agent payloads never select these limits."""

    def __init__(self, store: Any | None = None, *, default_allowed_symbols: tuple[str, ...] | None = None) -> None:
        self.store = store
        self.default_allowed_symbols = default_allowed_symbols

    def get(self, *, user_id: str, symbol: str) -> RiskPolicy:
        del symbol
        record = self.store.active_risk_policy(user_id=user_id) if self.store else None
        if record is None:
            return RiskPolicy(allowed_symbols=self.default_allowed_symbols)
        rules = record["rules"]
        return RiskPolicy(
            max_loss_per_trade=Decimal(str(rules["maxLossPerTrade"])),
            allowed_symbols=tuple(str(item).upper() for item in rules["allowedSymbols"]),
            max_daily_loss=Decimal(str(rules["maxDailyLoss"])),
            no_new_positions_after=str(rules["noNewPositionsAfter"]),
            timezone=str(rules.get("timezone", "Asia/Shanghai")),
            version=int(record["version"]),
        )


class InstrumentSpecProvider:
    """Fallback for payloads missing contract metadata: always fail closed.

    Multiplier and tick come from the official instrument catalog carried in
    the market snapshot. There is no built-in contract table, so a candidate
    without that metadata cannot have its risk computed and is rejected.
    """

    def get(self, symbol: str) -> InstrumentSpec:
        raise KeyError(f"instrument_metadata_missing:{symbol}")


def instrument_spec(
    payload: dict[str, Any], provider: InstrumentSpecProvider
) -> InstrumentSpec:
    multiplier = payload.get("contractMultiplier")
    tick = payload.get("priceTick")
    if multiplier is None or tick is None:
        return provider.get(str(payload["symbol"]))
    result = InstrumentSpec(decimal_value(multiplier), decimal_value(tick))
    if (
        not result.contract_multiplier.is_finite()
        or result.contract_multiplier <= 0
        or not result.price_tick.is_finite()
        or result.price_tick <= 0
    ):
        raise ValueError("invalid_instrument_metadata")
    return result


def decimal_value(value: Any) -> Decimal:
    return Decimal(str(value))


def floor_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def ceil_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def opening_time_allowed(
    current_minutes: int, cutoff_minutes: int, reopen_minutes: int = 9 * 60,
) -> bool:
    if cutoff_minutes >= reopen_minutes:
        return reopen_minutes <= current_minutes < cutoff_minutes
    return current_minutes >= reopen_minutes or current_minutes < cutoff_minutes


def risk_budget_summary(
    record: dict[str, Any] | None, *, today_pnl: Decimal | None,
    data_status: str, now: datetime | None = None,
) -> dict[str, Any]:
    if record is None:
        return {
            "configured": False, "dataStatus": data_status, "status": "unconfigured",
            "message": "模拟盘默认不限合约、手数和金额；策略按自身规则执行。",
        }
    rules = record["rules"]
    maximum = decimal_value(rules["maxDailyLoss"])
    per_trade = decimal_value(rules["maxLossPerTrade"])
    if not maximum.is_finite() or maximum <= 0 or not per_trade.is_finite() or per_trade <= 0:
        raise ValueError("invalid_risk_policy")
    base = {
        "configured": True,
        "policyVersion": int(record["version"]),
        "dataStatus": data_status,
        "maxLossPerTrade": float(per_trade),
        "maxDailyLoss": float(maximum),
        "noNewPositionsAfter": str(rules["noNewPositionsAfter"]),
        "allowedSymbols": [str(item).upper() for item in rules["allowedSymbols"]],
    }
    if data_status not in {"normal", "simulated"} or today_pnl is None:
        return {
            **base, "status": "unavailable", "dailyLossUsed": None,
            "dailyLossRemaining": None, "dailyLossPercent": None,
            "openingAllowedNow": None,
            "message": "账户数据暂不可用，无法计算当前风险预算。",
        }
    used = max(Decimal("0"), -today_pnl)
    remaining = max(Decimal("0"), maximum - used)
    percent = used / maximum * 100
    timezone = ZoneInfo(str(rules.get("timezone", "Asia/Shanghai")))
    local_now = (now or datetime.now(timezone)).astimezone(timezone)
    hour, minute = (int(part) for part in str(rules["noNewPositionsAfter"]).split(":"))
    opening_allowed = opening_time_allowed(
        local_now.hour * 60 + local_now.minute, hour * 60 + minute,
    )
    if used >= maximum:
        risk_status, message = "blocked", "已达到每日亏损上限，停止开新仓。"
    elif not opening_allowed:
        risk_status, message = "blocked", "当前已超过停止开仓时间。"
    elif percent >= 80:
        risk_status, message = "warning", "每日风险预算已使用八成以上。"
    else:
        risk_status, message = "normal", "当前仍在已确认的风险预算内。"
    return {
        **base,
        "status": risk_status,
        "dailyLossUsed": float(used),
        "dailyLossRemaining": float(remaining),
        "dailyLossPercent": float(percent),
        "openingAllowedNow": opening_allowed,
        "message": message,
    }


def calculate_trade_risk(
    payload: dict[str, Any], *, user_id: str, policies: RiskPolicyProvider,
    instruments: InstrumentSpecProvider, today_pnl: Decimal | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy = policies.get(user_id=user_id, symbol=payload["symbol"])
    spec = instrument_spec(payload, instruments)
    volume = decimal_value(payload["volume"])
    has_stop = payload.get("stopPrice") is not None
    loss = abs(decimal_value(payload["limitPrice"]) - decimal_value(payload["stopPrice"])) * spec.contract_multiplier * volume if has_stop else Decimal("0")
    loss = loss.quantize(Decimal("0.01"))
    policy_symbol = str(payload["symbol"]).split(".")[-1].upper()
    symbol_allowed = policy.allowed_symbols is None or policy_symbol in policy.allowed_symbols
    loss_passed = policy.max_loss_per_trade is None or (has_stop and loss <= policy.max_loss_per_trade)
    daily_passed = policy.max_daily_loss is None or (
        today_pnl is not None and today_pnl > -policy.max_daily_loss
    )
    cutoff_passed = True
    if policy.no_new_positions_after and str(payload.get("side", "")).endswith("_open"):
        local_now = (now or datetime.now(ZoneInfo(policy.timezone))).astimezone(ZoneInfo(policy.timezone))
        cutoff_hour, cutoff_minute = (int(part) for part in policy.no_new_positions_after.split(":"))
        current_minutes = local_now.hour * 60 + local_now.minute
        cutoff_minutes = cutoff_hour * 60 + cutoff_minute
        cutoff_passed = opening_time_allowed(current_minutes, cutoff_minutes)
    passed = symbol_allowed and loss_passed and daily_passed and cutoff_passed
    if not symbol_allowed:
        reason = f"{payload["symbol"]} 不在已设置的账户合约范围内。"
    elif not loss_passed:
        reason = f"预计最大亏损超过单笔风险上限 ¥{policy.max_loss_per_trade:,.0f}。"
    elif not daily_passed:
        reason = "当日亏损已达到停止开新仓上限，或账户数据当前不可用。"
    elif not cutoff_passed:
        reason = f"当前已超过 {policy.no_new_positions_after}，不再允许开新仓。"
    else:
        reason = "模拟盘未设置账户交易限制。" if policy.version == 0 and policy.max_loss_per_trade is None else "已通过当前账户交易规则检查。"
    return {
        "passed": passed,
        "limit": float(policy.max_loss_per_trade) if policy.max_loss_per_trade is not None else 0.0,
        "estimatedLoss": float(loss),
        "contractMultiplier": float(spec.contract_multiplier),
        "priceTick": str(spec.price_tick),
        "reason": reason,
        "policyVersion": policy.version,
        "checks": {"lossEstimateAvailable": has_stop, "unlimitedPerTradeLoss": policy.max_loss_per_trade is None,
            "symbolAllowed": symbol_allowed, "perTradeLoss": loss_passed,
            "dailyLoss": daily_passed, "openingTime": cutoff_passed,
        },
    }
