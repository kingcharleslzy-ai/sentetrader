"""plan_target_position, the pure arithmetic of the 目标持仓 ticket: every leg shape the card
tools and the relay build on — netting, add, reduce, flatten, reverse and the unchanged exits."""
from __future__ import annotations

import pytest

from trader_runtime.target_position import (
    leg_price_intent,
    plan_target_position,
    working_order_lots,
    working_orders_line,
)


def _pos(direction: str, volume: int, *, symbol: str = "SHFE.au2612", today: int = 0, hist: int = 0):
    return {"symbol": symbol, "direction": direction, "volume": volume,
            "todayVolume": today, "historicalVolume": hist, "frozenToday": 0, "frozenHistorical": 0}


def test_legs_of_every_shape():
    cases = [
        # (positions, target, expected legs, crosses_zero)
        ([], 3, [("open", "buy_open", 3)], False),                      # 无持仓 → 买开
        ([], -2, [("open", "sell_open", 2)], False),                    # 无持仓 → 卖开
        ([], 0, [], False),                                             # 无持仓清零 → 不用下单
        ([_pos("long", 1)], 3, [("open", "buy_open", 2)], False),       # 同向加仓
        ([_pos("long", 3, today=1, hist=2)], 1, [("close", "sell_close", 2)], False),  # 同向减仓
        ([_pos("long", 2)], 0, [("close", "close_all", 2)], False),     # 清零 → 双向全平
        ([_pos("short", 2)], -4, [("open", "sell_open", 2)], False),    # 空头同向加仓
        ([_pos("short", 4)], -1, [("close", "buy_close", 3)], False),   # 空头同向减仓
        ([_pos("short", 1)], 0, [("close", "close_all", 1)], False),    # 空头清零
        ([_pos("long", 1)], -2, [("close", "close_all", 1), ("open", "sell_open", 2)], True),   # 反向两段
        ([_pos("long", 2), _pos("short", 1)], 1, [], False),            # 双向轧差已达标
        ([_pos("long", 2), _pos("short", 1)], -1, [("close", "close_all", 3), ("open", "sell_open", 1)], True),
        ([_pos("long", 3)], 3, [], False),                              # 目标等于当前
    ]
    for positions, target, expected, crosses in cases:
        plan = plan_target_position(positions, "SHFE.au2612", target)
        assert [(leg.action, leg.side, leg.volume) for leg in plan.legs] == expected, (positions, target)
        assert plan.crosses_zero == crosses
        assert plan.unchanged == (not expected)


def test_netting_and_matching_are_case_insensitive_and_per_symbol():
    positions = [
        _pos("long", 2, symbol="shfe.au2612"),
        _pos("short", 1, symbol="SHFE.AU2612"),
        _pos("long", 5, symbol="SHFE.ag2612"),
    ]
    plan = plan_target_position(positions, "SHFE.au2612", 1)
    assert (plan.long_volume, plan.short_volume, plan.current_net) == (2, 1, 1)
    assert plan.unchanged


def test_summary_reads_like_the_card():
    assert plan_target_position([_pos("long", 1)], "SHFE.au2612", 3).summary == \
        "目标 +3 手，当前 +1 手 → 买开 2 手"
    assert plan_target_position([_pos("long", 1)], "SHFE.au2612", -2).summary == \
        "目标 -2 手，当前 +1 手 → 平多 1 手，成交后自动开空 2 手（确认一次）"
    assert plan_target_position([_pos("short", 3)], "SHFE.au2612", 3).summary == \
        "目标 +3 手，当前 -3 手 → 平空 3 手，成交后自动开多 3 手（确认一次）"
    assert plan_target_position([_pos("long", 2), _pos("short", 1)], "SHFE.au2612", -1).summary == \
        "目标 -1 手，当前 +1 手 → 平掉全部 3 手，成交后自动开空 1 手（确认一次）"
    assert plan_target_position([], "SHFE.au2612", 0).summary == \
        "目标 0 手，当前 0 手 → 已是目标持仓，不用下单"


def test_payload_is_camelcase_for_the_card():
    payload = plan_target_position([_pos("long", 2), _pos("short", 1)], "SHFE.au2612", -1).payload()
    assert payload == {
        "symbol": "SHFE.au2612", "currentNet": 1, "targetNet": -1,
        "longVolume": 2, "shortVolume": 1,
        "legs": [{"action": "close", "side": "close_all", "volume": 3},
                 {"action": "open", "side": "sell_open", "volume": 1}],
        "summary": payload["summary"], "crossesZero": True, "unchanged": False, "priceIntent": None,
    }


def test_reverse_without_cross_zero_allowed_raises():
    with pytest.raises(ValueError):
        plan_target_position([_pos("long", 1)], "SHFE.au2612", -1, allow_cross_zero=False)


def test_the_plan_keeps_the_contracts_own_spelling():
    """10-13 production: the plan upper-cased the contract (SHFE.RB2701) and that name reached the
    paper engine's quote read, which broke its market subscription. The plan names the contract
    the way the held row (or, with none, the caller's catalog symbol) spells it."""
    assert plan_target_position([_pos("long", 1)], "SHFE.AU2612", -2).symbol == "SHFE.au2612"
    assert plan_target_position([], "SHFE.rb2701", -2).symbol == "SHFE.rb2701"
    assert plan_target_position([], "CZCE.SR701", 1).symbol == "CZCE.SR701"


def test_the_stated_price_mode_prices_every_leg_and_unstated_legs_take_the_per_side_default():
    """#941: 用户说的价格方式记在计划上，计划的每一步都按它定价；没说时平仓对手价、开仓按账号默认。"""
    reverse = plan_target_position([_pos("long", 1)], "SHFE.au2612", -1, price_intent="passive").payload()
    assert reverse["priceIntent"] == "passive"
    assert [leg_price_intent(reverse, leg["side"], default_open=lambda: "last") for leg in reverse["legs"]] == \
        ["passive", "passive"]
    unstated = plan_target_position([_pos("long", 1)], "SHFE.au2612", -1).payload()
    assert unstated["priceIntent"] is None
    assert [leg_price_intent(unstated, leg["side"], default_open=lambda: "last") for leg in unstated["legs"]] == \
        ["aggressive", "last"]
    assert leg_price_intent({"priceIntent": "bogus"}, "sell_close", default_open=lambda: "passive") == "aggressive"
    assert plan_target_position([], "SHFE.au2612", 1, price_intent="bogus").price_intent is None


def test_working_order_lots_count_only_this_contracts_live_orders():
    """#942 review: 数本合约还挂着的单（开仓、平仓都算，部分成交按剩余），已结束的单、别的合约不算。"""
    def order(direction, offset, left, *, status="ALIVE", symbol="SHFE.rb2701"):
        return {"symbol": symbol, "direction": direction, "offset": offset, "volume": 5,
                "volumeLeft": left, "status": status}
    orders = [order("BUY", "OPEN", 1), order("BUY", "OPEN", 2), order("SELL", "OPEN", 1),
              order("SELL", "CLOSETODAY", 3), order("BUY", "OPEN", 4, status="FINISHED"),
              order("BUY", "OPEN", 0), order("BUY", "OPEN", 6, symbol="SHFE.rb2705")]
    lots = working_order_lots(orders, "shfe.RB2701")
    assert lots == {"buy_open": 3, "sell_open": 1, "sell_close": 3}
    assert working_orders_line("螺纹2701", lots).startswith(
        "螺纹2701 还有 3 手买开、1 手卖开、3 手卖平挂单没成交，先撤掉或等成交后再调目标持仓")
    assert working_order_lots([order("SELL", "CLOSE", 1)], "SHFE.rb2701") == {"sell_close": 1}
    assert working_order_lots([order("BUY", "OPEN", 1, status="FINISHED")], "SHFE.rb2701") == {}
