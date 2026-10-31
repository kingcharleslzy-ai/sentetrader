from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from trader_runtime.risk import opening_time_allowed, risk_budget_summary


@pytest.mark.parametrize(
    ("current_minutes", "cutoff_minutes", "expected"),
    [
        (21 * 60 + 59, 22 * 60, True),
        (22 * 60, 22 * 60, False),
        (23 * 60 + 59, 22 * 60, False),
        (1, 22 * 60, False),
        (2 * 60, 22 * 60, False),
        (8 * 60 + 59, 22 * 60, False),
        (9 * 60, 22 * 60, True),
        (1 * 60 + 59, 2 * 60, True),
        (2 * 60, 2 * 60, False),
        (8 * 60 + 59, 2 * 60, False),
        (9 * 60, 2 * 60, True),
    ],
)
def test_opening_time_window_reopens_at_09_00(
    current_minutes: int, cutoff_minutes: int, expected: bool,
) -> None:
    assert opening_time_allowed(current_minutes, cutoff_minutes) is expected


def policy() -> dict:
    return {
        "version": 3,
        "rules": {
            "allowedSymbols": ["AU2612", "RB2610"],
            "maxLossPerTrade": "1000.00",
            "maxDailyLoss": "3000.00",
            "noNewPositionsAfter": "22:00",
            "timezone": "Asia/Shanghai",
        },
    }


@pytest.mark.parametrize(
    ("pnl", "hour", "expected_status", "opening_allowed"),
    [
        ("-100", 10, "normal", True),
        ("-2500", 10, "warning", True),
        ("-3000", 10, "blocked", True),
        ("100", 23, "blocked", False),
    ],
)
def test_risk_budget_summary_uses_daily_loss_and_opening_window(
    pnl: str, hour: int, expected_status: str, opening_allowed: bool,
) -> None:
    result = risk_budget_summary(
        policy(), today_pnl=Decimal(pnl), data_status="normal",
        now=datetime(2026, 8, 31, hour, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert result["status"] == expected_status
    assert result["openingAllowedNow"] is opening_allowed
    assert result["dailyLossUsed"] == max(0, -float(pnl))


def test_risk_budget_summary_does_not_turn_missing_account_data_into_zero_loss() -> None:
    result = risk_budget_summary(
        policy(), today_pnl=None, data_status="unavailable",
    )
    assert result["status"] == "unavailable"
    assert result["dailyLossUsed"] is None
    assert result["openingAllowedNow"] is None


def test_unconfigured_simulation_accepts_new_contract_and_large_size_without_account_caps():
    from trader_runtime.risk import InstrumentSpecProvider, RiskPolicyProvider, calculate_trade_risk
    payload = {"symbol": "SHFE.rb2701", "side": "sell_open", "volume": 10000,
               "limitPrice": 3157, "stopPrice": 3200, "contractMultiplier": 10, "priceTick": 1}
    result = calculate_trade_risk(payload, user_id="user", policies=RiskPolicyProvider(), instruments=InstrumentSpecProvider())
    assert result["passed"]
    assert result["estimatedLoss"] == 4300000
    assert result["checks"]["unlimitedPerTradeLoss"]
    assert result["checks"]["symbolAllowed"]
    del payload["stopPrice"]
    result = calculate_trade_risk(payload, user_id="user", policies=RiskPolicyProvider(), instruments=InstrumentSpecProvider())
    assert result["passed"]
    assert not result["checks"]["lossEstimateAvailable"]


def test_explicit_policy_and_replay_contract_remain_explicit_not_hidden_defaults():
    from trader_runtime.risk import InstrumentSpecProvider, RiskPolicyProvider, calculate_trade_risk
    class Store:
        def active_risk_policy(self, **kwargs): return policy()
    payload = {"symbol": "SHFE.rb2701", "side": "sell_open", "volume": 3,
               "limitPrice": 3157, "stopPrice": 3200, "contractMultiplier": 10, "priceTick": 1}
    result = calculate_trade_risk(payload, user_id="user", policies=RiskPolicyProvider(Store()), instruments=InstrumentSpecProvider())
    assert not result["checks"]["symbolAllowed"]
    assert not result["checks"]["perTradeLoss"]
    assert "rb2701" in result["reason"]
    replay = RiskPolicyProvider(default_allowed_symbols=("RB2701",)).get(user_id="user",symbol="SHFE.rb2701")
    assert replay.max_loss_per_trade is None
    assert replay.allowed_symbols == ("RB2701",)


def test_missing_contract_metadata_fails_closed_instead_of_builtin_table():
    from trader_runtime.risk import InstrumentSpecProvider, RiskPolicyProvider, calculate_trade_risk
    payload = {"symbol": "SHFE.au2612", "side": "buy_open", "volume": 1,
               "limitPrice": 800, "stopPrice": 798}
    with pytest.raises(KeyError, match="instrument_metadata_missing"):
        calculate_trade_risk(payload, user_id="user", policies=RiskPolicyProvider(), instruments=InstrumentSpecProvider())
