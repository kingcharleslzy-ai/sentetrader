from trader_runtime.strategy_validation import _normalize_bars, _signal, luck_check, validate_strategy_history


def definition() -> dict:
    return {
        "template": "ma_cross",
        "timeframeSeconds": 300,
        "direction": "both",
        "parameters": {
            "fastPeriod": 3,
            "slowPeriod": 8,
            "breakoutPeriod": 10,
            "kdPeriod": 5,
            "macdFast": 4,
            "macdSlow": 9,
            "macdSignal": 3,
            "minimumVolumeRatio": 1.0,
        },
        "riskLimits": {
            "maxPosition": 1,
            "maxOrdersPerDay": 20,
            "stopLossTicks": 20,
            "takeProfitTicks": 20,
            "cooldownBars": 1,
        },
        "signalInput": "completed_bars_only",
        "executionMode": "simulation_only",
        "liveTrading": "disabled",
    }


def trend_cycle_bars(count: int = 500) -> list[dict]:
    bars = []
    timestamp = 1_700_000_000_000_000_000
    price = 100.0
    for index in range(count):
        step = 1.0 if index % 60 < 30 else -1.0
        opening = price
        price += step
        bars.append({
            "datetime": timestamp + index * 300_000_000_000,
            "tradingDay": f"2023-11-{15 + min(index // 96, 14):02d}",
            "open": opening,
            "high": max(opening, price) + 0.2,
            "low": min(opening, price) - 0.2,
            "close": price,
            "volume": 100,
            "openInterest": 8_000 + index,
        })
    return bars


def test_completed_bar_replay_can_pass_genuine_out_of_sample_evidence() -> None:
    bars = trend_cycle_bars()

    result = validate_strategy_history(
        definition=definition(),
        raw_bars=bars,
        split_timestamp=bars[300]["datetime"],
        price_tick=1,
        contract_multiplier=1,
        data_source="tqsdk:SHFE.rb2610:fixture",
        data_digest="a" * 64,
        slippage_ticks=0.1,
        commission_ticks_per_side=0.1,
    )

    assert result["status"] == "passed"
    assert result["evidence"]["engine"]["decisionPoint"] == "completed_bar_close"
    assert result["evidence"]["engine"]["fillPoint"] == "next_bar_open"
    assert result["evidence"]["inSample"]["tradeCount"] >= 3
    assert result["evidence"]["outOfSample"]["tradeCount"] >= 2
    assert {check["status"] for check in result["checks"]} == {"passed"}


def test_fixture_history_cannot_be_promoted_to_validation_pass() -> None:
    bars = trend_cycle_bars()

    result = validate_strategy_history(
        definition=definition(),
        raw_bars=bars,
        split_timestamp=bars[300]["datetime"],
        price_tick=1,
        contract_multiplier=1,
        data_source="fixture:SHFE.rb2610",
        data_digest="b" * 64,
    )

    assert result["status"] == "failed"
    source_check = next(
        check for check in result["checks"] if check["id"] == "official_history"
    )
    assert source_check["status"] == "failed"


def test_signal_at_completed_bar_cannot_read_the_next_bar() -> None:
    original = trend_cycle_bars()
    changed_future = [dict(bar) for bar in original]
    changed_future[121].update({
        "open": 10_000,
        "high": 10_100,
        "low": 9_900,
        "close": 10_050,
        "volume": 1_000_000,
    })

    assert _signal(_normalize_bars(original), 120, definition()) == _signal(
        _normalize_bars(changed_future), 120, definition()
    )


def test_luck_check_tells_an_edge_from_noise_and_never_gates_the_validation() -> None:
    """运气检验 (adapted from Vibe-Trading's validation.py): the bootstrap interval of the average trade, and the
    drawdown the same trades could reach in a worse order. Seeded: the same trades give the same answer."""
    edge = [300.0, -100.0, 250.0, 200.0, -50.0, 400.0, 150.0, -120.0, 350.0, 220.0, 180.0, -80.0]
    noise = [300.0, -320.0, 250.0, -260.0, 40.0, -30.0, 410.0, -400.0, 150.0, -170.0, 90.0, -60.0]
    sure = luck_check(edge, 1_000_000)
    assert sure["status"] == "edge" and sure["meanLow95"] > 0 and sure == luck_check(edge, 1_000_000)
    unclear = luck_check(noise, 1_000_000)
    assert unclear["status"] == "unclear" and unclear["meanLow95"] < 0 < unclear["meanHigh95"]
    assert unclear["shuffledDrawdownP95Percent"] >= 0 and 0 < unclear["probabilityPositive"] < 1
    assert luck_check(edge[:9], 1_000_000) == {"tradeCount": 9, "status": "insufficient"}

    # A strategy that passes every gating check still passes when its luck row is only “!”.
    bars = trend_cycle_bars()
    result = validate_strategy_history(
        definition=definition(), raw_bars=bars, split_timestamp=bars[300]["datetime"], price_tick=1,
        contract_multiplier=1, data_source="tqsdk:SHFE.rb2610:fixture", data_digest="a" * 64,
        slippage_ticks=0.1, commission_ticks_per_side=0.1)
    luck_row = result["checks"][-1]
    assert luck_row["id"] == "luck" and luck_row["label"] == "运气检验" and luck_row["status"] == "passed"
    assert luck_row["detail"].startswith("16 笔每笔平均净盈亏 +19，95% 区间 +19 ~ +20，区间在 0 以上；")
    assert result["evidence"]["luck"]["tradeCount"] == 16
    assert "trades" not in result["evidence"]["inSample"] and "trades" not in result["evidence"]["outOfSample"]
    import trader_runtime.strategy_validation as validation
    original = validation.luck_check
    validation.luck_check = lambda pnls, balance: {"tradeCount": len(pnls), "status": "insufficient"}  # type: ignore[assignment]
    try:
        cautious = validate_strategy_history(
            definition=definition(), raw_bars=bars, split_timestamp=bars[300]["datetime"], price_tick=1,
            contract_multiplier=1, data_source="tqsdk:SHFE.rb2610:fixture", data_digest="a" * 64,
            slippage_ticks=0.1, commission_ticks_per_side=0.1)
    finally:
        validation.luck_check = original
    assert cautious["status"] == "passed" and cautious["checks"][-1]["status"] == "waiting"
    assert cautious["checks"][-1]["detail"] == "成交 16 笔，不足 10 笔，判断不了是不是运气"
