"""The card-building tests' old argument shapes as propose_order's intent (意图重构第 3 步).

The three card tools the model filled before step 3 (create_trade_proposal / create_close_batch_proposal /
create_target_position_proposal) are gone; propose_order takes the OrderIntent. The service-level tests still state
their cards the old way (one side, one contract, one size), which is exactly one intent — converted here, once.
"""
from __future__ import annotations

from typing import Any

_PRICE = {"aggressive": "counter", "passive": "queue", "last": "last"}
_SIDE = {"buy_open": ("open", "long"), "sell_open": ("open", "short"),
         "sell_close": ("close", "long"), "buy_close": ("close", "short")}
_BUCKET = {"today": "today", "historical": "yesterday"}


def legacy_card_args(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The propose_order arguments the same card is asked for with. Unknown keys pass through, so a test that
    sends a malformed old argument still gets the schema's refusal."""
    args = dict(arguments)
    out: dict[str, Any] = {"reason": args.pop("reason", "测试出卡")}
    price = args.pop("priceIntent", None)
    if price is not None:
        out["price"] = _PRICE.get(price, price)
    if tool == "create_trade_proposal":
        side = args.pop("side", None)
        act, direction = _SIDE.get(side, (side, None))
        out.update(act=act, symbol=args.pop("symbol", None), dir=direction, vol=args.pop("volume", None))
        if args.get("limitPrice") is not None:
            out["limitPrice"] = args.pop("limitPrice")
        args.pop("limitPrice", None)
        bucket = args.pop("positionBucket", None)
        if bucket in _BUCKET:
            out["bucket"] = _BUCKET[bucket]
    elif tool == "create_close_batch_proposal":
        out.update(act="close_all", dir=args.pop("direction", None))
        symbols = args.pop("symbols", None)
        if symbols:
            out["symbols"] = symbols
    elif tool == "create_target_position_proposal":
        out.update(act="target", symbol=args.pop("symbol", None), target=args.pop("targetNet", None))
    else:
        raise ValueError(tool)
    out.update(args)  # protection / remainingProtection / anything else, as given
    return {key: value for key, value in out.items() if value is not None or key in arguments}
