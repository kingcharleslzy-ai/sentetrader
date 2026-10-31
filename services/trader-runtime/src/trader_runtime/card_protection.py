"""合并卡 (第四批, 2026-10-06): one close card, two steps — ① close N lots, ② the remaining position's
stop / take moved to the card's levels. One 确认 authorizes both; it runs as the user's own action
(the account write path), never through AI 自主执行.

The card carries `remainingProtection` (built and checked here when the card is made): for the stop and
the take, `set` (a price), `remove` (null in the request) or `keep`, plus what the position stands on
now (`original`) and its cost (`cost`, the open average on the tick grid). The confirmation writes a
`card_protection_steps` row in its own transaction; the order-watch loop then calls `advance_steps`
every 2 s:

- the close order(s) still working, or their outcome unknown → wait (unknown for 30 minutes → skipped);
- ended with nothing filled (refused, cancelled, never sent) → skipped, nothing changed;
- ended with a fill → step ② for what the account actually holds now
  (AutonomousSimulationService.replace_card_protection: the old user-set protection is replaced, never
  doubled; a failed replacement leaves still-valid protection in place; existing fill convergence may retire or shrink old rows).

Each outcome republishes the card with `remainingProtection.status` (done / failed / skipped), its
reason and its result; a live call hears one line. Nothing here places an order."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .conversation_cards import ask_action
from .order_boundaries import price_on_tick
from .risk import ceil_to_tick, floor_to_tick
from .simulation_orders import CLOSE_TODAY_EXCHANGES, order_is_working

log = logging.getLogger("uvicorn.error.cardprotection")

UNKNOWN_STALL_SECONDS = 1800.0
# The close's own fills not yet in the account's report this long after the close ended → failed.
REPORT_LAG_SECONDS = 120.0
_UNKNOWN_STATUSES = frozenset({"结果未知", "身份异常", "回报异常"})


class RemainingProtectionInvalid(ValueError):
    """A remainingProtection the card cannot carry (422): `code` plus the Chinese sentence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def price_words(value: Any) -> str:
    try:
        number = Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return str(value)
    text = format(number, "f")
    head, _, tail = text.partition(".")
    return f"{int(head):,}" + (f".{tail}" if tail else "")


def cost_on_tick(open_price: Any, tick: Decimal, *, long: bool) -> Decimal | None:
    """The position's cost as a protective price: the open average on the tick grid, rounded to the
    side that never loses (多单向上、空单向下)."""
    try:
        cost = Decimal(str(open_price))
    except (InvalidOperation, ValueError):
        return None
    if not cost.is_finite() or cost <= 0 or not tick.is_finite() or tick <= 0:
        return None
    return (ceil_to_tick(cost, tick) if long else floor_to_tick(cost, tick)).quantize(tick)


def held_protection_buckets(positions: list[dict[str, Any]], symbol: str, direction: str) -> list[tuple[str, int]]:
    keys = (("today", "todayVolume"), ("historical", "historicalVolume")) if (
        symbol.split(".", 1)[0].upper() in CLOSE_TODAY_EXCHANGES) else (("all", "volume"),)
    owned = [item for item in positions if str(item.get("symbol") or "").casefold() == symbol.casefold()
             and item.get("direction") == direction]
    return [(bucket, volume) for bucket, key in keys
            if (volume := sum(max(0, int(item.get(key) or 0)) for item in owned)) > 0]


def common_protection_price(levels: list[dict[str, Any]], key: str) -> str | None:
    values = {Decimal(item[key]) if item[key] is not None else None for item in levels}
    return levels[0][key] if len(values) == 1 and levels else None


def bucket_protection_levels(buckets: list[tuple[str, int]], rows: list[Any], *,
                             keep: set[str]) -> list[dict[str, Any]]:
    """Read only proven bucket prices. Direct rows can overlap: summing their volumes cannot prove
    full coverage. A single covering direct row, or the existing distinct fill-backed sources,
    can prove one price; differing prices / partly unprotected inventory cannot be kept safely."""
    levels = []
    for bucket, held in buckets:
        owned = [row for row in rows if row["position_bucket"] == bucket]
        direct = [row for row in owned if not row["strategy_metadata_json"]]
        covered = (max((int(row["remaining_volume"]) for row in direct), default=0) if direct
                   else sum(int(row["remaining_volume"]) for row in owned))
        item: dict[str, Any] = {"positionBucket": bucket, "volume": held}
        for key, column, word in (("stopLoss", "trigger_price", "止损"),
                                  ("takeProfit", "take_profit_price", "止盈")):
            values = {Decimal(row[column]) if row[column] else None for row in owned}
            if not owned or covered < held:
                values.add(None)
            ambiguous = len(values) > 1 or (owned and covered < held and any(value is not None for value in values))
            if key in keep and ambiguous:
                label = {"today": "今仓", "historical": "昨仓", "all": "持仓"}[bucket]
                raise RemainingProtectionInvalid(
                    "remaining_protection_ambiguous",
                    f"{label}{word}有不同价位或部分仓位没有保护，无法确定各手数的原价；"
                    "请明确设置或撤掉该项，或先在持仓面板整理保护单")
            value = next(iter(values)) if len(values) == 1 else None
            item[key] = next((str(row[column]) for row in owned
                              if row[column] and Decimal(row[column]) == value), None)
        levels.append(item)
    return levels


# 止损的运动状态 (#1193): what makes a kept stop keep moving — the trailing rule and its frozen ATR, 1R 保本, the
# entry it trails from and how far it has trailed. A ticket's stop keeps these in strategy_metadata_json beside its
# fill ownership, a position row's stop in trailing_json; only these keys move to the replacement.
STOP_MOTION_KEYS = ("trailing", "entryAtr", "breakeven", "exitAtr", "entryPrice", "priceTick", "trailExtreme",
                    "trailActive", "breakevenActive")


def bucket_stop_motion(buckets: list[tuple[str, int]], rows: list[Any]) -> dict[str, str | None]:
    """Each held bucket's kept stop motion (#1193), read by bucket and by source like the prices: a fixed stop is
    None; stops in one bucket that move differently cannot be kept as one and are refused before anything changes."""
    motions: dict[str, str | None] = {}
    for bucket, _ in buckets:
        found: set[str | None] = set()
        for row in rows:
            if row["position_bucket"] != bucket or not row["trigger_price"]:
                continue
            state = json.loads(row["strategy_metadata_json"] or row["trailing_json"] or "{}")
            motion = {key: state[key] for key in STOP_MOTION_KEYS if key in state}
            found.add(json.dumps(motion, sort_keys=True) if motion.get("trailing") or motion.get("breakeven") else None)
        if len(found) > 1:
            label = {"today": "今仓", "historical": "昨仓", "all": "持仓"}[bucket]
            raise RemainingProtectionInvalid(
                "remaining_protection_ambiguous",
                f"{label}的止损有不同的追踪方式，无法确定保留哪一种；请明确设置止损，或先在持仓面板整理保护单")
        motions[bucket] = next(iter(found), None)
    return motions


def plan_remaining_protection(
    spec: dict[str, Any] | None, *, side: str, symbol: str, close_volume: int,
    positions: list[dict[str, Any]], market: dict[str, Any], rows: list[Any],
) -> dict[str, Any] | None:
    """The card's `remainingProtection` from the request's {stopLoss?, takeProfit?} (key present with a
    price = set, present with null = remove, absent = keep), or None when nothing was asked. Refuses
    (RemainingProtectionInvalid) an open card, a close that leaves no position, a price off the tick
    grid or on the wrong side of the current price."""
    if not spec:
        return None
    if not side.endswith("_close"):
        raise RemainingProtectionInvalid("remaining_protection_close_only",
                                         "剩余仓位的止损/止盈只能跟平仓卡一起带；开仓用 protection")
    direction = "long" if side == "sell_close" else "short"
    long = direction == "long"
    held = sum(int(item.get("volume") or 0) for item in positions
               if str(item.get("symbol") or "").casefold() == symbol.casefold()
               and str(item.get("direction") or "") == direction)
    remaining = held - close_volume
    if remaining <= 0:
        raise RemainingProtectionInvalid("remaining_protection_nothing_left",
                                         "这张卡平完全部持仓，没有剩余仓位可以改止损/止盈")
    try:
        tick = Decimal(str(market.get("priceTick")))
        latest = Decimal(str(market.get("price")))
    except (InvalidOperation, ValueError):
        raise RemainingProtectionInvalid("remaining_protection_quote_unavailable", "读不到现价，没法核对止损/止盈") from None
    position = next((item for item in positions
                     if str(item.get("symbol") or "").casefold() == symbol.casefold()
                     and str(item.get("direction") or "") == direction), {})
    cost = cost_on_tick(position.get("openPrice"), tick, long=long)
    original = bucket_protection_levels(
        held_protection_buckets(positions, symbol, direction), rows,
        keep={key for key in ("stopLoss", "takeProfit") if key not in spec})
    old_stop = common_protection_price(original, "stopLoss")
    old_take = common_protection_price(original, "takeProfit")
    plan: dict[str, Any] = {}
    for key, word, old in (("stopLoss", "止损", old_stop), ("takeProfit", "止盈", old_take)):
        if key not in spec:
            plan[key] = {"action": "keep", "price": old}
            continue
        value = spec[key]
        if value is None:
            plan[key] = {"action": "remove", "price": None}
            continue
        try:
            level = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise RemainingProtectionInvalid("remaining_protection_price_invalid", f"{word}价要是数字") from None
        if not level.is_finite() or level <= 0 or not price_on_tick(level, tick):
            raise RemainingProtectionInvalid(
                "remaining_protection_price_invalid", f"{word}价 {value} 不是这个合约最小变动价位的整数倍")
        stop_side = key == "stopLoss"
        wrong = ((level >= latest if long else level <= latest) if stop_side
                 else (level <= latest if long else level >= latest))
        if wrong:
            relation = ("低于" if long else "高于") if stop_side else ("高于" if long else "低于")
            raise RemainingProtectionInvalid(
                "remaining_protection_wrong_side", f"剩余仓位的{word}要{relation}现价 {price_words(latest)}")
        plan[key] = {"action": "set", "price": format(level.normalize(), "f"),
                     **({"atCost": True} if cost is not None and level == cost else {})}
    if plan["stopLoss"]["action"] == "keep" and plan["takeProfit"]["action"] == "keep":
        return None
    return {
        "direction": direction, "remainingVolume": remaining,
        "stopLoss": plan["stopLoss"], "takeProfit": plan["takeProfit"],
        "original": {"stopLoss": old_stop, "takeProfit": old_take},
        "perBucketOriginal": original,
        "cost": format(cost, "f") if cost is not None else None,
        "status": "pending",
    }


def step_words(plan: dict[str, Any], *, volume: int | None = None) -> str:
    """“剩下 1 手止损 → 81,420（成本）” — the second step in one line."""
    lots = f"剩下 {volume if volume is not None else plan.get('remainingVolume')} 手"
    parts = []
    for key, word in (("stopLoss", "止损"), ("takeProfit", "止盈")):
        item = plan.get(key) or {}
        if item.get("action") == "set":
            parts.append(f"{word} → {price_words(item['price'])}{'（成本）' if item.get('atCost') else ''}")
        elif item.get("action") == "remove":
            parts.append(f"撤掉{word}")
    return f"{lots}{'、'.join(parts)}"


def _payload_order_ids(payload: dict[str, Any]) -> list[str]:
    if isinstance(payload.get("orders"), list):
        return [str(order.get("clientOrderId")) for order in payload["orders"] if order.get("clientOrderId")]
    return [str(payload["clientOrderId"])] if payload.get("clientOrderId") else []


class CardProtectionRelay:
    """Advances the confirmed merged cards' second steps (driven by the order-watch loop)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def advance_steps(self) -> int:
        store = getattr(self.app.state, "store", None)
        if store is None:
            return 0
        done = 0
        for step in store.awaiting_card_protection_steps():
            try:
                done += int(await self._advance(step))
            except Exception as error:  # one broken step never stops the others
                log.debug("card protection step %s failed: %s", step["proposalId"], error)
        return done

    async def _advance(self, step: dict[str, Any]) -> bool:
        from .autonomy import AutonomyError
        app, store = self.app, self.app.state.store
        registry = getattr(app.state, "accounts", None)
        services = registry.get(step["tradingAccountId"]) if registry is not None else None
        simulation_orders = getattr(services, "simulation_orders", None)
        autonomy = getattr(services, "autonomy", None)
        if simulation_orders is None or autonomy is None:
            return False  # the account is not assembled now: the step waits for it
        record = store.get(step["proposalId"])
        payload = record.get("payload") or {}
        order_ids = _payload_order_ids(payload)
        if not order_ids:
            return await self._finish(step, record, "skipped", "平仓委托没有发出，止损没改")
        views = []
        for client_order_id in order_ids:
            try:
                views.append(await simulation_orders.query(client_order_id, user_id=step["userId"]))
            except Exception:
                return False  # unreadable now: the next round decides, nothing is guessed
        statuses = [str(view.get("displayStatus") or "结果未知") for view in views]
        if any(status in _UNKNOWN_STATUSES for status in statuses):
            created = datetime.fromisoformat(str(step["createdAt"]))
            if (_now() - created).total_seconds() < UNKNOWN_STALL_SECONDS:
                return False
            return await self._finish(step, record, "skipped", "平仓结果一直未知，止损没改")
        if any(order_is_working(view) for view in views):
            return False
        filled = sum(int(view.get("filledVolume") or 0) for view in views)
        if filled <= 0:
            return await self._finish(step, record, "skipped", "平仓没有成交，止损没改")
        if step["readySince"] is None:
            store.mark_card_protection_ready(step["proposalId"], _now().isoformat())
            step = {**step, "readySince": _now().isoformat()}
        session_id = str(record.get("sessionId") or step["sessionId"])

        def finish(connection: Any, result: dict[str, Any]) -> bool:
            return store.finish_card_protection_step(
                step["proposalId"], status="done", result={**result, "filled": filled}, connection=connection)

        try:
            result = await autonomy.replace_card_protection(
                user_id=step["userId"], session_id=session_id, symbol=step["symbol"],
                direction=step["direction"], plan=step["plan"], close_order_ids=order_ids, filled=filled,
                finish=finish)
        except AutonomyError as error:
            if error.code == "card_protection_not_ready":
                ready = datetime.fromisoformat(str(step["readySince"]))
                if (_now() - ready).total_seconds() < REPORT_LAG_SECONDS:
                    return False
                return await self._finish(step, record, "failed", "账户成交回报一直没跟上，止损没改", filled=filled)
            reason = error.message or FAILURE_WORDS.get(error.code, f"止损没改（{error.code}）")
            return await self._finish(step, record, "failed", reason, filled=filled)
        if result is None:
            return False  # already finished by another round
        await self._publish(record, {"status": "done", "reason": None, "result": {**result, "filled": filled}})
        return True

    async def _finish(self, step: dict[str, Any], record: dict[str, Any], status: str, reason: str,
                      *, filled: int = 0) -> bool:
        result = {"filled": filled, "reason": reason}
        if not self.app.state.store.finish_card_protection_step(step["proposalId"], status=status, result=result):
            return False
        await self._publish(record, {"status": status, "reason": reason, "result": result})
        return True

    async def _publish(self, record: dict[str, Any], outcome: dict[str, Any]) -> None:
        payload = dict(record.get("payload") or {})
        plan = dict(payload.get("remainingProtection") or {})
        result = outcome.get("result") or {}
        plan.update({"status": outcome["status"], "reason": outcome.get("reason"), "result": result})
        name = str(payload.get("name") or payload.get("symbol") or "")
        if outcome["status"] == "failed":
            remaining = result.get("remainingVolume") or plan.get("remainingVolume")
            plan["actions"] = [
                ask_action("按现价重新设", f"按现价给我剩下的 {remaining} 手{name}重新设止损，先出卡给我确认"),
                ask_action("问 AI", f"{name}平仓后剩余仓位的止损没改成（{outcome.get('reason')}），现在怎么办？"),
            ]
        payload["remainingProtection"] = plan
        event = "proposal.close_batch" if record.get("proposalType") == "close_batch" else "proposal.trade"
        broker = self.app.state.broker
        try:
            await broker.publish(str(record["sessionId"]), kind="approval", name=event, payload=payload)
        except Exception as error:
            log.debug("card protection card not republished: %s", error)
        if outcome["status"] == "done":
            text = f"{name} 平仓后，{step_words(plan, volume=result.get('remainingVolume'))}，已改好。"
        else:
            text = f"{name} 本次保护修改未执行：{outcome.get('reason')}。已有保护继续按实际成交更新。"
        voice = getattr(self.app.state, "voice_realtime", None)
        user_id = str(record.get("userId") or "")
        if voice is not None and user_id and voice.user_calls(user_id) > 0:
            voice.announce(f"card-protection-{record['id']}", text, user_id=user_id)


FAILURE_WORDS = {
    "ai_execution_stopped": "AI 模拟执行已停止或急停",
    "ai_new_opens_halted": "已只停新开仓",
    "account_read_only": "这个账户你只有查看权限",
    "account_not_granted": "这个账户没有授权给你",
}


def remaining_protection_view(record: dict[str, Any], step: dict[str, Any] | None) -> dict[str, Any]:
    """A stored card with its second step's current state merged in (GET reads and replays)."""
    payload = dict(record.get("payload") or {})
    if step is None or not isinstance(payload.get("remainingProtection"), dict) or step["status"] == "awaiting_close":
        return payload
    result = step.get("result") or {}
    payload["remainingProtection"] = {**payload["remainingProtection"], "status": step["status"],
                                      "reason": result.get("reason"), "result": result}
    return payload
