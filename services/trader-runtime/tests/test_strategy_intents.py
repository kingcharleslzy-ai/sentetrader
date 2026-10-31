from trader_runtime.risk import InstrumentSpecProvider, RiskPolicyProvider
from trader_runtime.strategy_intents import build_strategy_intent


def strategy(maximum: int = 1, stop_ticks: int = 20) -> dict:
    return {
        "strategyId": "strategy-ready",
        "symbol": "SHFE.rb2610",
        "definitionDigest": "a" * 64,
        "definition": {
            "riskLimits": {
                "maxPosition": maximum,
                "maxOrdersPerDay": 20,
                "stopLossTicks": stop_ticks,
                "takeProfitTicks": 40,
                "cooldownBars": 1,
            }
        },
    }


def evaluation(signal: str = "buy") -> dict:
    return {
        "signal": signal,
        "evaluatedBarTime": "1767225600000000000",
        "dataStatus": "simulated",
    }


def market(*, age: float = 0) -> dict:
    return {
        "symbol": "SHFE.rb2610",
        "price": 3500.0,
        "priceTick": 1.0,
        "volumeMultiple": 10.0,
        "capturedAt": "2026-01-01T09:00:00+08:00",
        "ageSeconds": age,
        "dataStatus": "simulated",
    }


def account(*, age: float = 0) -> dict:
    return {
        "todayNetPnl": 0.0,
        "capturedAt": "2026-01-01T09:00:00+08:00",
        "ageSeconds": age,
        "dataStatus": "simulated",
        "isCached": False,
    }


def activity(*, age: float = 0) -> dict:
    return {
        "capturedAt": "2026-01-01T09:00:00+08:00",
        "ageSeconds": age,
        "dataStatus": "simulated",
        "ordersInitialized": True,
        "isCached": False,
    }


def build(**overrides):
    arguments = {
        "strategy": strategy(),
        "evaluation": evaluation(),
        "market": market(),
        "account": account(),
        "positions": [],
        "activity": activity(),
        "user_id": "local-user",
        "policies": RiskPolicyProvider(),
        "instruments": InstrumentSpecProvider(),
    }
    arguments.update(overrides)
    return build_strategy_intent(**arguments)


def test_buy_signal_becomes_bounded_open_intent_without_execution_permission() -> None:
    result = build()

    assert result["signalSnapshot"]["signal"] == "buy"
    assert result["positionIntent"]["action"] == "open_long"
    assert result["riskAdjustedIntent"]["status"] == "allowed"
    assert result["riskAdjustedIntent"]["volume"] == 1
    assert result["riskAdjustedIntent"]["executionEligible"] is False
    assert result["riskAdjustedIntent"]["liveTrading"] == "disabled"


def test_opposite_position_is_closed_before_any_reversal() -> None:
    result = build(
        positions=[{
            "symbol": "SHFE.rb2610", "direction": "short", "volume": 2,
        }],
        account=account(age=60),
    )

    assert result["positionIntent"] == {
        "action": "close_short",
        "requestedVolume": 2,
        "currentLong": 0,
        "currentShort": 2,
        "targetLong": 0,
        "targetShort": 0,
        "reason": "买入信号出现，先平已有空仓；本次不同时反手开多。",
    }
    assert result["riskAdjustedIntent"]["status"] == "reduce_only"
    assert result["riskAdjustedIntent"]["volume"] == 2


def test_stale_context_blocks_new_risk() -> None:
    result = build(market=market(age=60))

    assert result["riskAdjustedIntent"]["status"] == "blocked"
    assert result["riskAdjustedIntent"]["volume"] == 0
    assert result["riskAdjustedIntent"]["executionEligible"] is False


def test_per_trade_budget_clamps_volume_instead_of_approving_oversize_intent() -> None:
    from decimal import Decimal

    from trader_runtime.risk import RiskPolicy
    class ExplicitBudget(RiskPolicyProvider):
        def get(self, **kwargs):
            return RiskPolicy(max_loss_per_trade=Decimal("2000"))
    result = build(strategy=strategy(maximum=10, stop_ticks=50), policies=ExplicitBudget())

    assert result["positionIntent"]["requestedVolume"] == 10
    assert result["riskAdjustedIntent"]["status"] == "allowed"
    assert result["riskAdjustedIntent"]["volume"] == 4
    assert result["riskAdjustedIntent"]["riskCheck"]["estimatedLoss"] == 2000.0


def test_context_digest_ignores_age_drift_until_freshness_changes() -> None:
    first = build(market=market(age=1), account=account(age=1), activity=activity(age=1))
    still_fresh = build(
        market=market(age=10), account=account(age=10), activity=activity(age=10)
    )
    stale = build(
        market=market(age=16), account=account(age=16), activity=activity(age=16)
    )

    assert first["contextDigest"] == still_fresh["contextDigest"]
    assert stale["contextDigest"] != first["contextDigest"]
    assert stale["riskAdjustedIntent"]["status"] == "blocked"


def test_daily_loss_cap_blocks_opens_only_when_the_strategy_sets_one() -> None:
    """maxLoss=None（没设当日亏损上限，10-08 用户决定 A）不挡开仓、不报错；设了照旧挡。"""
    losing = {**account(), "todayNetPnl": -50_000.0}
    unlimited = strategy()
    unlimited["definition"]["riskLimits"]["maxLoss"] = None
    allowed = build(strategy=unlimited, account=losing)["riskAdjustedIntent"]
    assert allowed["status"] == "allowed" and all(check["id"] != "strategy_loss_limit" for check in allowed["checks"])
    capped = strategy()
    capped["definition"]["riskLimits"]["maxLoss"] = 1000
    assert build(strategy=capped, account=losing)["riskAdjustedIntent"]["status"] == "blocked"


# -- 策略只管自己开的仓 (所有者 2026-10-08): own_position 是策略自己的仓（held_position 口径） --------


def test_own_position_holds_beside_a_manual_long_on_a_sell_signal() -> None:
    """自动下单口径：账户有手动/AI 多单、策略自己空仓，卖出信号 → 不平任何仓。"""
    manual = [{"symbol": "SHFE.rb2610", "direction": "long", "volume": 1}]

    result = build(positions=manual, evaluation=evaluation("sell"), own_position=(None, 0))

    # 不平手动多单；本策略规则允许开空，按规则开空（hold 的策略则是 hold）。
    assert result["positionIntent"]["action"] == "open_short"
    assert result["positionIntent"]["currentLong"] == 0
    # 只提醒口径（不传 own_position）仍按账户持仓给平仓提醒。
    assert build(positions=manual, evaluation=evaluation("sell"))["positionIntent"]["action"] == "close_long"


def test_own_position_closes_only_the_strategys_lots() -> None:
    """策略自己多 1 + 手动多 1，卖出信号 → 只平 1 手（策略那手）。"""
    result = build(
        positions=[{"symbol": "SHFE.rb2610", "direction": "long", "volume": 2}],
        evaluation=evaluation("sell"),
        own_position=("long", 1),
    )

    intent = result["positionIntent"]
    assert intent["action"] == "close_long" and intent["requestedVolume"] == 1
    assert intent["currentLong"] == 1 and intent["targetLong"] == 0
    assert result["riskAdjustedIntent"]["status"] == "reduce_only"


def test_own_position_opens_to_maximum_beside_manual_lots() -> None:
    """手动空单在场、策略空仓：买入信号 → 按上限开多，不去平手动空单、也不因手动多单少开。"""
    manual_short = [{"symbol": "SHFE.rb2610", "direction": "short", "volume": 1}]
    manual_long = [{"symbol": "SHFE.rb2610", "direction": "long", "volume": 1}]

    opened = build(strategy=strategy(maximum=2), positions=manual_short,
                   evaluation=evaluation("buy"), own_position=(None, 0))
    assert opened["positionIntent"]["action"] == "open_long"
    assert opened["positionIntent"]["requestedVolume"] == 2

    topped_up = build(strategy=strategy(maximum=2), positions=manual_long,
                      evaluation=evaluation("buy"), own_position=(None, 0))
    assert topped_up["positionIntent"]["action"] == "open_long"
    assert topped_up["positionIntent"]["requestedVolume"] == 2
    # 只提醒口径仍先平账户里的反向仓。
    assert build(strategy=strategy(maximum=2), positions=manual_short,
                 evaluation=evaluation("buy"))["positionIntent"]["action"] == "close_short"


def _v2_strategy(maximum: int = 1) -> dict:
    base = strategy(maximum=maximum)
    base["definition"] = {
        **base["definition"],
        "schemaVersion": 2,
        "plan": {"name": "双均线", "description": "测试计划", "rules": {}},
    }
    return base


def _v2_evaluation(signal: str, *, entry: bool = False, exit_rule: bool = False) -> dict:
    return {
        **evaluation(signal),
        "rules": {"entryLong": entry and signal == "buy", "entryShort": entry and signal == "sell",
                  "exitLong": exit_rule and signal == "sell", "exitShort": exit_rule and signal == "buy"},
        "engine": {},
    }


def test_v2_state_reads_the_strategy_own_lots_not_the_accounts() -> None:
    """schemaVersion 2 的 PositionState 方向/手数也用策略自己的仓；入场价等字段照旧来自 engine_state。"""
    manual = [{"symbol": "SHFE.rb2610", "direction": "long", "volume": 2}]
    held = build(strategy=_v2_strategy(), evaluation=_v2_evaluation("sell", exit_rule=True),
                 positions=manual, own_position=("long", 1))
    assert held["positionIntent"]["action"] == "close_long"
    assert held["positionIntent"]["requestedVolume"] == 1
    assert held["positionIntent"]["currentLong"] == 1

    # 策略空仓 + 手动空单，买入信号 → 开多，不平手动空单（只提醒口径才会平）。
    manual_short = [{"symbol": "SHFE.rb2610", "direction": "short", "volume": 1}]
    fresh = build(strategy=_v2_strategy(), evaluation=_v2_evaluation("buy", entry=True),
                  positions=manual_short, own_position=(None, 0))
    assert fresh["positionIntent"]["action"] == "open_long"
    assert fresh["positionIntent"]["requestedVolume"] == 1
    account_read = build(strategy=_v2_strategy(), evaluation=_v2_evaluation("buy", exit_rule=True),
                         positions=manual_short)
    assert account_read["positionIntent"]["action"] == "close_short"
