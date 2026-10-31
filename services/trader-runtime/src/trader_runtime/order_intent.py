"""下单意图（docs/ORDER_INTENT_V2.md §2.1）：每个出卡入口交给下单规划器（`order_planner.plan`）的同一个结构。

这里是类型，以及从现有入口参数（HTTP 路由、目标持仓接力）构造它的转换函数（交易 AI 的 propose_order 直接按本结构填）；一句话到意图的唯一
解析器在 `order_intent_parse.parse`（第 2 步，纯函数、无 I/O）。

口径（所有者 2026-10-16 定）：
- `dir` 是多/空，不是买/卖：开多 = open+long，平多 = close+long（卖平）。
- `price`：`counter` 对价（“市价”也是它）/ `queue` 排队价（挂买一卖一，即旧的 passive“挂单”）/
  `last` 最新价；`limit` 是用户自己说出的价，优先于任何方式。都没说 = 规划器按统一默认规则定价。
- `bucket`：`today` 平今 / `yesterday` 平昨 / `auto` 按“平今平昨”偏好由规划器分配（只对上期所、能源中心有意义）。
- `vol` / `target` / `frac` 三选一：手数 / 目标净持仓（正多负空）/ 持仓比例（“平一半”= 0.5，向下取整至少 1 手）。
- `cond` 有值（价格或时间条件）时这句不出即时卡。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# 撤单 is `cancel` here (the corpus writes the long name, which the release guard check_desktop_release.mjs bans
# in runtime source). The planner makes no card of it: it belongs to the cancel chain.
Act = Literal["open", "close", "close_all", "target", "reverse", "cancel",
              "retract", "amend", "confirm", "none"]
Stance = Literal["order", "question", "conditional", "reported", "retracted", "advice", "negated"]
Direction = Literal["long", "short"]
PriceMode = Literal["counter", "queue", "last"]
Bucket = Literal["today", "yesterday", "auto"]

# The acts the planner turns into cards; the others (撤单、撤回、改单、确认) belong to other chains.
CARD_ACTS = frozenset({"open", "close", "close_all", "target", "reverse"})
# The skill each card act belongs to (agent_skills/*/SKILL.md): the one gate every entry applies before planning —
# the trading AI's propose_order, the typed fast path and the voice front desk (a skill switched off or withheld
# from the user's tier stops its acts, whichever entry the sentence came through).
CARD_ACT_SKILLS: dict[str, str] = {"open": "order-proposal", "close": "order-proposal", "close_all": "batch-close",
                                   "target": "target-position", "reverse": "target-position"}

# The order builder's price-intent words ↔ the intent's price modes (one table, both directions).
_MODE_OF_PRICE_INTENT: dict[str, PriceMode] = {"aggressive": "counter", "passive": "queue", "last": "last"}
_PRICE_INTENT_OF_MODE: dict[str, str] = {mode: word for word, mode in _MODE_OF_PRICE_INTENT.items()}
_POSITION_BUCKET_OF_BUCKET: dict[Bucket, str] = {"today": "today", "yesterday": "historical"}  # auto: none
_BUCKET_OF_POSITION_BUCKET: dict[str, Bucket] = {word: bucket for bucket, word in _POSITION_BUCKET_OF_BUCKET.items()}
_SIDES: dict[str, tuple[Act, Direction]] = {
    "buy_open": ("open", "long"), "sell_open": ("open", "short"),
    "sell_close": ("close", "long"), "buy_close": ("close", "short"),
}
_SIDE_OF: dict[tuple[str, str | None], str] = {act_dir: side for side, act_dir in _SIDES.items()}


@dataclass(frozen=True)
class Price:
    mode: PriceMode | None = None
    limit: float | None = None

    @property
    def stated(self) -> bool:
        return self.mode is not None or self.limit is not None


@dataclass(frozen=True)
class OrderIntent:
    """One thing the user (or an entry acting for them) asked to order. `product` set means "whichever
    month of this product the account holds" (the product code, e.g. ``rb``), with `symbol` the contract to
    fall back on when none is held; `symbol` alone is that contract exactly. `symbols` narrows a close_all to
    named contracts. `reason`/`protection`/`remaining_protection` ride along onto the card unchanged."""
    act: Act
    stance: Stance = "order"
    product: str | None = None
    symbol: str | None = None
    dir: Direction | None = None
    vol: int | None = None
    target: int | None = None
    frac: float | None = None
    price: Price = field(default_factory=Price)
    bucket: Bucket = "auto"
    cond: dict[str, Any] | None = None  # {"type": "price", "op": "<=", "value": 3000} / {"type": "time", "text": "收盘前"}
    legs: tuple[OrderIntent, ...] = ()
    symbols: tuple[str, ...] = ()
    reason: str = ""
    protection: dict[str, Any] | None = None
    remaining_protection: dict[str, Any] | None = None
    # What the parser read beyond the order itself: `scope` the object of a 撤单/撤回/改单/确认 (pending_card /
    # last_order / working_order / all_orders); `fixed` the sentence corrected itself (“开三手，不对，两手…”, the
    # last saying counts); `bare` the side was only 买/卖 (no 开/多/空) — with the opposite side held it could
    # be a close, so the parser says Uncertain when it knows the account (`side_conflict`).
    scope: str | None = None
    fixed: bool = False
    bare: bool = False

    @property
    def side(self) -> str | None:
        """buy_open / sell_open / buy_close / sell_close for an open or close with a direction."""
        return _SIDE_OF.get((self.act, self.dir))


def card_skills(intent: OrderIntent) -> list[str]:
    """The skills an intent's card acts need (its legs' too), in the order they were said, each once."""
    acts = [leg.act for leg in intent.legs] if intent.legs else [intent.act]
    return list(dict.fromkeys(CARD_ACT_SKILLS[act] for act in acts if act in CARD_ACT_SKILLS))


def price_mode(price_intent: str | None) -> PriceMode | None:
    """The intent's price mode for a builder price-intent word (aggressive/passive/last); None for none."""
    return _MODE_OF_PRICE_INTENT.get(str(price_intent)) if price_intent else None


def price_intent_word(mode: str | None) -> str | None:
    """The builder's price-intent word for an intent price mode; None when no mode was stated."""
    return _PRICE_INTENT_OF_MODE.get(str(mode)) if mode else None


def position_bucket(bucket: Bucket) -> str | None:
    """The builder's positionBucket for an intent bucket (auto → None: the planner decides)."""
    return _POSITION_BUCKET_OF_BUCKET.get(bucket)


# -- 从现有入口参数构造（工具、HTTP 路由、目标持仓接力：参数已是结构） ----------------------------------------------

def from_trade_order(
    *, symbol: str, side: str, volume: int, price_intent: str | None = None,
    limit_price: float | None = None, bucket: str | None = None, reason: str = "",
    protection: dict[str, Any] | None = None, remaining_protection: dict[str, Any] | None = None,
) -> OrderIntent:
    """POST /api/simulation/proposals / a 目标持仓 leg: one side, one contract.
    positionBucket all/None is auto (on SHFE/INE the planner allocates; elsewhere there is no bucket)."""
    act, direction = _SIDES[side]
    return OrderIntent(
        act=act, symbol=symbol, dir=direction, vol=volume,
        price=Price(mode=price_mode(price_intent), limit=limit_price),
        bucket=_BUCKET_OF_POSITION_BUCKET.get(str(bucket), "auto"), reason=reason,
        protection=protection, remaining_protection=remaining_protection,
    )


def from_close_batch(
    *, symbols: list[str] | None, direction: str | None, reason: str, price_intent: str | None = None,
) -> OrderIntent:
    """POST /api/simulation/close-batch-proposals: every position, or the
    named contracts, one or both directions."""
    return OrderIntent(
        act="close_all", symbols=tuple(symbols or ()), reason=reason,
        dir=direction if direction in {"long", "short"} else None,  # type: ignore[arg-type]
        price=Price(mode=price_mode(price_intent)),
    )


def from_target_position(
    *, symbol: str, target_net: int, price_intent: str | None, reason: str, product: str | None = None,
) -> OrderIntent:
    """POST /api/simulation/target-position/proposals."""
    return OrderIntent(act="target", symbol=symbol, product=product, target=int(target_net),
                       price=Price(mode=price_mode(price_intent)), reason=reason)
