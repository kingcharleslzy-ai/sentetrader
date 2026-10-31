"""目标持仓 planning: the orders that take one contract from its current positions to a wanted
net position. Pure — no provider, no store, no gateway; the card tools and the relay ask it what
to do, they never re-derive the arithmetic themselves.

Semantics (2026-09-22 target-position ticket):

- Net = 多头手数 − 空头手数；双向持仓先轧差（多 2 空 1 = 净 +1）。
- 目标等于当前净持仓 → unchanged，没有 leg，不出卡。
- 同向加仓 → 一个 open leg；同向减仓 → 一个方向性 close leg（平今/平昨由出卡逻辑决定，
  不在本模块）；目标 0 → 一个 close_all leg（双向都平，按毛持仓）；反向 → 先 close_all
  全部毛持仓、再按目标方向开仓（crossesZero=True，两段接力；确认一次，第 1 段全部成交后自动发第 2 段）。
- 价格方式（#941）：用户说了的（挂单 passive / 对手价 aggressive / 最新价 last）记在计划上，计划的每一步
  （第 1 步的全平或单向平/开、接力出卡的第 2 步）都按它定价；没说时每步各按默认——平仓对手价、
  开仓按账号的默认价格方式。`leg_price_intent` 是这条规则的唯一出处，出卡工具与接力都问它。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

LegSide = Literal["buy_open", "sell_open", "buy_close", "sell_close", "close_all"]
PriceIntent = Literal["passive", "aggressive", "last"]
_PRICE_INTENTS = frozenset({"passive", "aggressive", "last"})

# The one-line Chinese each leg contributes to the plan summary.
_SIDE_LABELS: dict[str, str] = {
    "buy_open": "买开", "sell_open": "卖开",
    "buy_close": "买平", "sell_close": "卖平",
    "close_all": "平掉全部",
}


@dataclass(frozen=True)
class TargetLeg:
    """One ordered step of the plan: `close_all` closes both directions of the contract (gross),
    the directional closes open only what the card layer still has to split into 今/昨."""
    action: Literal["open", "close"]
    side: LegSide
    volume: int


@dataclass(frozen=True)
class TargetPlan:
    symbol: str
    current_net: int
    target_net: int
    long_volume: int
    short_volume: int
    legs: tuple[TargetLeg, ...]
    summary: str
    crosses_zero: bool
    price_intent: PriceIntent | None = None  # the user's stated price mode for every leg; None = defaults

    @property
    def unchanged(self) -> bool:
        return not self.legs

    def payload(self) -> dict[str, Any]:
        """The camelCase shape stored on proposal cards (`targetPosition`)."""
        return {
            "symbol": self.symbol,
            "currentNet": self.current_net,
            "targetNet": self.target_net,
            "longVolume": self.long_volume,
            "shortVolume": self.short_volume,
            "legs": [{"action": leg.action, "side": leg.side, "volume": leg.volume}
                     for leg in self.legs],
            "summary": self.summary,
            "crossesZero": self.crosses_zero,
            "unchanged": self.unchanged,
            "priceIntent": self.price_intent,
        }


def working_order_lots(orders: list[Any], symbol: str) -> dict[str, int]:
    """This contract's orders still on the book (ALIVE with lots left, 部分成交 counts its remainder),
    by kind: {"buy_open": n, "sell_close": m, ...}, empty when none. A target is planned from filled
    positions only, and any resting order changes the net it was planned against once it fills — an
    opening one overshoots, a closing one falls short (#942 review) — so the card tool refuses
    instead of counting it in."""
    wanted = str(symbol).strip().casefold()
    lots: dict[str, int] = {}
    for order in orders or []:
        if not isinstance(order, dict) or str(order.get("symbol") or "").strip().casefold() != wanted:
            continue
        if str(order.get("status") or "").upper() != "ALIVE":
            continue
        try:
            left = int(order.get("volumeLeft", order.get("volume")) or 0)
        except (TypeError, ValueError):
            continue
        direction = {"BUY": "buy", "SELL": "sell"}.get(str(order.get("direction") or "").upper())
        if direction is None or left <= 0:
            continue
        kind = "open" if str(order.get("offset") or "").upper() == "OPEN" else "close"
        lots[f"{direction}_{kind}"] = lots.get(f"{direction}_{kind}", 0) + left
    return lots


def working_orders_line(display: str, lots: Mapping[str, int]) -> str:
    """The refusal the user reads when orders of this contract are still working."""
    order = ("buy_open", "sell_open", "buy_close", "sell_close")
    parts = "、".join(f"{lots[side]} 手{_SIDE_LABELS[side]}" for side in order if lots.get(side))
    return (f"{display} 还有 {parts}挂单没成交，先撤掉或等成交后再调目标持仓"
            "（在委托页或底栏“委托”里点撤单，也可以跟 AI 说撤掉这笔挂单）")


def leg_price_intent(plan: Mapping[str, Any], leg_side: str, *, default_open: Callable[[], str]) -> str:
    """How one leg of a stored plan (`TargetPlan.payload()`) is priced: the plan's stated price mode
    for every leg; unstated, a close takes the 对手价 and an opening the account's default."""
    stated = plan.get("priceIntent")
    if stated in _PRICE_INTENTS:
        return str(stated)
    return "aggressive" if leg_side.endswith("close") or leg_side == "close_all" else default_open()


def _fmt(net: int) -> str:
    return "0 手" if net == 0 else f"{net:+d} 手"


def _leg_text(leg: TargetLeg) -> str:
    return f"{_SIDE_LABELS[leg.side]} {leg.volume} 手"


def plan_target_position(
    positions: list[dict[str, Any]], symbol: str, target_net: int,
    *, allow_cross_zero: bool = True, price_intent: str | None = None,
) -> TargetPlan:
    """The plan from this account's current rows for `symbol` (provider position dicts) to
    `target_net` (positive = net long, negative = net short, 0 = flat). Raises ValueError when the
    plan must cross zero and `allow_cross_zero` is False; every caller today allows it.
    The plan keeps the contract's own spelling — the caller passes the catalog symbol, and a
    held position's row spells it the same — never a re-cased copy: 10-13 production upper-cased
    it here (SHFE.RB2701), and that name reached the paper engine's quote read and subscription."""
    wanted = str(symbol).strip()
    rows = [item for item in positions or []
            if str(item.get("symbol", "")).casefold() == wanted.casefold()]
    wanted = str(rows[0].get("symbol")) if rows else wanted
    long_volume = sum(int(item.get("volume") or 0) for item in rows if item.get("direction") == "long")
    short_volume = sum(int(item.get("volume") or 0) for item in rows if item.get("direction") == "short")
    current_net = long_volume - short_volume
    target = int(target_net)
    legs: list[TargetLeg] = []
    crosses_zero = False
    if target == current_net:
        pass  # already at the wanted net (dual holdings count as their net): nothing to order
    elif target == 0:
        legs = [TargetLeg("close", "close_all", long_volume + short_volume)]
    elif current_net == 0:
        legs = [TargetLeg("open", "buy_open" if target > 0 else "sell_open", abs(target))]
    elif current_net > 0 and target > 0 or current_net < 0 and target < 0:
        delta = abs(target) - abs(current_net)
        if delta > 0:
            legs = [TargetLeg("open", "buy_open" if target > 0 else "sell_open", delta)]
        else:
            legs = [TargetLeg("close", "sell_close" if current_net > 0 else "buy_close", -delta)]
    else:
        if not allow_cross_zero:
            raise ValueError("target_position_crosses_zero")
        crosses_zero = True
        legs = [
            TargetLeg("close", "close_all", long_volume + short_volume),
            TargetLeg("open", "buy_open" if target > 0 else "sell_open", abs(target)),
        ]
    if not legs:
        summary = f"目标 {_fmt(target)}，当前 {_fmt(current_net)} → 已是目标持仓，不用下单"
    elif crosses_zero:
        # 反手确认一次 (2026-10-24): the card says both steps up front — one confirmation covers both.
        held = "平多" if not short_volume else "平空" if not long_volume else "平掉全部"
        summary = (f"目标 {_fmt(target)}，当前 {_fmt(current_net)} → {held} {legs[0].volume} 手，"
                   f"成交后自动{'开多' if target > 0 else '开空'} {legs[1].volume} 手（确认一次）")
    else:
        summary = f"目标 {_fmt(target)}，当前 {_fmt(current_net)} → " + "，再".join(_leg_text(leg) for leg in legs)
    return TargetPlan(
        symbol=wanted, current_net=current_net, target_net=target,
        long_volume=long_volume, short_volume=short_volume,
        legs=tuple(legs), summary=summary, crosses_zero=crosses_zero,
        price_intent=price_intent if price_intent in _PRICE_INTENTS else None,  # type: ignore[arg-type]
    )
