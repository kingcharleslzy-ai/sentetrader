"""保本提醒 (第四批, 2026-10-06): when a position's floating profit has covered its risk, ask once whether
to move its stop to the cost.

Judged in the Runtime's 30 s account loop (AccountReminderLoop, the same fresh snapshot the 收盘前 /
风险度 reminders read), for each owner of each assembled account, per position (contract × direction):

- only a position with a user-set stop (a direct stop, or one an open card carried — a strategy's own
  protection is the strategy's) whose stop is still worse than the cost (多单 止损 < 成本, 空单相反);
- R = |开仓均价 − 止损| (the farthest such stop); asked when the profit distance reaches
  notify.breakEvenRule.multiple × R (default 1 R), or — mode amount — when the position's floating P&L
  reaches breakEvenRule.amount 元 (a USD sub-account: breakEvenRule.amountUsd 美元, else by its multiple);
- a stale account snapshot or quote judges nothing; notify.breakEven off asks nothing;
- at most once per position per trading day (breakeven_notices, committed with the message: a restart
  never re-asks, and 不用 needs nothing more — the day's question is spent).

Delivery is the 盘中异动 pattern: a message + a 保本提醒 card in the conversation the user is
looking at (reminder_target_session: online on this account first), feed.reminder
(the desktop notification), the line on a live call, the lock-screen push (kind breakeven). The card's
移到成本 / 不用 (and a spoken 移 / 不用) answer through POST /api/ai-execution/breakeven/{id}/move|decline:
the user's own action — the stops the card was asked about (bound by row when it is made, #528) move to
the cost (止盈不动) after their original position is re-proven and a fresh price and cost-basis check
pass; a stop that appeared since, a replaced or re-priced row, or a changed average fails the card —
never an order."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .account_reminders import _fresh, _preferences
from .card_protection import cost_on_tick, price_words
from .conversation_cards import ask_action, reminder_card, reminder_target_session
from .instruments import account_currency, money_unit
from .order_boundaries import SimulationOrderError, quote_is_fresh
from .reminder_kinds import reminder_enabled
from .user_preferences import BreakEvenRule, preference_value

log = logging.getLogger("uvicorn.error.breakeven")

DIRECTION_WORDS = {"long": "多", "short": "空"}


def _now() -> datetime:
    return datetime.now(UTC)


def _rule(preferences: dict[str, Any]) -> BreakEvenRule:
    value = preference_value(preferences, "notify.breakEvenRule")
    try:
        return BreakEvenRule.model_validate(value) if isinstance(value, dict) else BreakEvenRule()
    except ValueError:
        return BreakEvenRule()


def account_rule(rule: BreakEvenRule, account_id: str) -> BreakEvenRule:
    """The threshold as this account can read it: the amount mode's `amount` is in 元, so a USD sub-account
    (外盘，盈亏是美元) is judged against its own `amountUsd` 美元 (owner 2026-10-21), and without one by the
    multiple of its risk — never a USD P&L against a CNY figure, and no exchange rate is guessed (3A)."""
    currency = account_currency(account_id)
    if rule.mode != "amount" or currency == "CNY":
        return rule
    if currency == "USD" and rule.amountUsd is not None:
        return rule.model_copy(update={"amount": rule.amountUsd})
    return rule.model_copy(update={"mode": "multiple"})


def _instrument_name(provider: Any, symbol: str, market: dict[str, Any]) -> str:
    """The contract's name as the catalog says it (沪金2612), else the quote's, else the code."""
    try:
        for item in provider.instrument_catalog().get("items") or []:
            if str(item.get("symbol") or "").casefold() == symbol.casefold() and item.get("instrumentName"):
                return str(item["instrumentName"])
    except Exception:  # noqa: BLE001
        pass
    return str(market.get("instrumentName") or market.get("name") or symbol)


def _lots_word(count: int) -> str:
    return "止损" if count <= 1 else f"{'两' if count == 2 else count}笔止损"


def judge(position: dict[str, Any], rows: list[Any], market: dict[str, Any], rule: BreakEvenRule) -> dict[str, Any] | None:
    """The question for one position, or None: its user-set stops worse than the cost, R, the profit and
    whether the threshold is reached. Pure — the caller has already checked the snapshot is fresh."""
    direction = str(position.get("direction") or "")
    if direction not in DIRECTION_WORDS or int(position.get("volume") or 0) <= 0:
        return None
    long = direction == "long"
    try:
        tick = Decimal(str(market.get("priceTick")))
        latest = Decimal(str(market.get("price")))
        average = Decimal(str(position.get("openPrice")))
    except (InvalidOperation, ValueError):
        return None
    if not all(value.is_finite() and value > 0 for value in (tick, latest, average)):
        return None
    cost = cost_on_tick(average, tick, long=long)
    stops = [row for row in rows if row["status"] == "ARMED" and row["trigger_price"]]
    worse = [row for row in stops if (Decimal(row["trigger_price"]) < average if long else Decimal(row["trigger_price"]) > average)]
    if cost is None or not worse:
        return None  # no stop, or the stop is already at the cost or better: nothing to ask
    risk = max(abs(average - Decimal(row["trigger_price"])) for row in worse)
    profit = latest - average if long else average - latest
    if profit <= 0 or ((cost >= latest) if long else (cost <= latest)):
        return None
    if rule.mode == "amount":
        try:
            pnl = Decimal(str(position.get("unrealizedPnl")))
        except (InvalidOperation, ValueError):
            return None
        if not pnl.is_finite() or pnl < rule.amount:
            return None
    elif risk <= 0 or profit < Decimal(str(rule.multiple)) * risk:
        return None
    takes = sorted({row["take_profit_price"] for row in rows if row["status"] == "ARMED" and row["take_profit_price"]})
    return {"direction": direction, "cost": cost, "latest": latest, "average": average, "risk": risk,
            "profit": profit, "stops": sorted({Decimal(row["trigger_price"]) for row in worse}),
            "count": len(worse), "take": Decimal(takes[0]) if len(takes) == 1 else None,
            "mode": rule.mode, "multiple": rule.multiple, "amount": rule.amount,
            "pnl": position.get("unrealizedPnl"),
            # The stops this question is about, by row (#528): the answer may only ever touch these.
            "stopIds": [str(row["id"]) for row in worse], "triggers": [str(row["trigger_price"]) for row in worse]}


def card_for(*, notice_id: str, symbol: str, name: str, question: dict[str, Any], at: str) -> tuple[dict[str, Any], str]:
    """The 保本提醒 card and the message's text; an amount is said in the account's own unit (元 / 美元)."""
    stop_words = " / ".join(price_words(item) for item in question["stops"])
    cost, latest = price_words(question["cost"]), price_words(question["latest"])
    if question["mode"] == "amount":
        unit = question.get("unit") or "元"
        reason = f"浮盈已到 {price_words(question['pnl'])} {unit}（你设的 {question['amount']:,} {unit}）"
    elif float(question["multiple"]) == 1.0:
        reason = "浮盈已超过止损距离"
    else:
        reason = f"浮盈已到止损距离的 {question['multiple']:g} 倍"
    summary = f"{reason}：成本 {cost}，现价 {latest}，止损 {stop_words}。"
    lots = _lots_word(question["count"])
    step = {"title": f"{lots} {stop_words} → {cost}（成本）",
            "detail": f"止盈 {price_words(question['take'])} 不变" if question["take"] is not None else "止盈不变"}
    card = reminder_card(
        turn_id=notice_id, kind="breakeven", title=f"保本提醒 · {name}", summary=summary, at=at, symbol=symbol,
        figures=[("成本", cost), ("现价", latest), ("止损", stop_words, "warning")],
        actions=[{"kind": "breakeven", "label": "移到成本", "choice": "move", "breakevenId": notice_id},
                 {"kind": "breakeven", "label": "不用", "choice": "decline", "breakevenId": notice_id}])
    if question.get("strategyNote"):
        card["note"] = question["strategyNote"]  # the strategy's own stop stays: say there are two
    card.update({"step": step, "breakeven": {"id": notice_id, "state": "pending", "cost": format(question["cost"], "f"),
                                             "direction": question["direction"], "result": None,
                                             # The card's own targets (#528): the move answers for these rows only.
                                             "stopIds": question["stopIds"], "triggers": question["triggers"]}})
    text = f"保本提醒：{name}{reason}，要把{lots}从 {stop_words} 移到成本 {cost} 吗？"
    return card, text


async def check_breakeven(app: Any, *, user_id: str, account_id: str, services: Any,
                          positions: list[dict[str, Any]], summary: dict[str, Any] | None) -> int:
    """One round for one owner of one account; returns how many questions went out. Never raises."""
    asked = 0
    try:
        if not _fresh(summary):
            return 0
        assert summary is not None
        preferences = _preferences(app, user_id)
        if not reminder_enabled(preferences, "breakeven"):
            return 0
        autonomy = getattr(services, "autonomy", None)
        trade_day = str(summary.get("tradingDay") or "")
        if autonomy is None or not trade_day:
            return 0
        rule = account_rule(_rule(preferences), account_id)
        for position in positions:
            symbol, direction = str(position.get("symbol") or ""), str(position.get("direction") or "")
            if not symbol or direction not in DIRECTION_WORDS or int(position.get("volume") or 0) <= 0:
                continue
            rows = autonomy.manual_protection_rows(user_id=user_id, symbol=symbol, direction=direction)
            if not any(row["status"] == "ARMED" and row["trigger_price"] for row in rows):
                continue
            notice_id = f"breakeven-{account_id}-{trade_day}-{symbol}-{direction}"
            if app.state.store.breakeven_notice(notice_id) is not None:
                continue  # asked today already (answered or not)
            try:
                market = services.provider.market_snapshot("", symbol)
                quote_is_fresh(market, allow_fixture=False, now=services.simulation_orders.trading_now())
            except (SimulationOrderError, RuntimeError, KeyError):
                continue  # a stale quote judges nothing
            question = judge(position, rows, market, rule)
            if question is None:
                continue
            name = _instrument_name(services.provider, symbol, market)
            question["unit"] = money_unit(account_currency(account_id))
            question["strategyNote"] = autonomy.strategy_protection_note(user_id=user_id, symbol=symbol, direction=direction)
            if await _deliver(app, user_id=user_id, account_id=account_id, trade_day=trade_day, symbol=symbol,
                              name=name, notice_id=notice_id, question=question):
                asked += 1
    except Exception as error:  # noqa: BLE001 — a reminder never breaks the loop it rides
        log.debug("breakeven check failed: %s", error)
    return asked


async def _deliver(app: Any, *, user_id: str, account_id: str, trade_day: str, symbol: str, name: str,
                   notice_id: str, question: dict[str, Any]) -> bool:
    """Through the one delivery entry (#932): the question row (the card's own state, answered once) is the
    business claim on the reminder's transaction; the 提醒中心 row carries the same 移到成本 / 不用 actions
    while the question is open."""
    from .reminders import ReminderSpec, deliver
    from .web_push import breakeven_message
    store, broker = app.state.store, app.state.broker
    at = _now().isoformat()
    card, text = card_for(notice_id=notice_id, symbol=symbol, name=name, question=question, at=at)
    session = reminder_target_session(broker, store, user_id, account_id=account_id)
    session_id = str(session["sessionId"]) if session else ""

    def claim(connection: Any) -> bool:
        return bool(store.claim_breakeven_notice(
            connection=connection, notice_id=notice_id, user_id=user_id, trading_account_id=account_id,
            trade_day=trade_day, symbol=symbol, direction=question["direction"], session_id=session_id, card=card))

    result = await deliver(app, ReminderSpec(
        kind="breakeven", user_id=user_id, account_id=account_id, dedupe_key=notice_id, trading_day=trade_day,
        title=card["title"], body=card["summary"], symbol=symbol, name=name, at=at, message=text,
        turn_id=notice_id, cards=[("card.reminder", card)], session_id=session_id or None, auto_landing=False,
        actions=list(card["actions"]),
        # The announcement's kind is the notice id: the call answers 移 / 不用 for this one only.
        speech=f"{text}说“移到成本”就移，说“不用”就不移。", voice_kind=notice_id,
        push=breakeven_message(name=name, body=text.removeprefix("保本提醒："), card_id=card["cardId"]),
        legacy=claim))
    return result.reminder is not None


class BreakEvenAnswerError(Exception):
    """The answer could not be carried out now (HTTP 409); `message` is the user's sentence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Refusals that leave the question open (try again in a moment) rather than settle it as failed.
_TRANSIENT = frozenset({"quote_not_current", "protection_in_flight", "protection_changed",
                        "activity_not_current"})


async def answer(app: Any, *, user_id: str, notice_id: str, choice: str) -> dict[str, Any]:
    """移到成本 / 不用 for one question, once. KeyError when the question is not the user's; an already
    answered question returns its outcome again (a second click, a spoken 移 after the click)."""
    from .autonomy import AutonomyError
    store = app.state.store
    notice = store.breakeven_notice(notice_id)
    if notice is None or notice["userId"] != user_id:
        raise KeyError(notice_id)
    if notice["status"] != "pending":
        return _view(notice)
    card = dict(notice["card"])
    state = dict(card.get("breakeven") or {})
    if choice == "decline":
        result = "这笔持仓今天不再问。"
        settled = {**card, "actions": [], "breakeven": {**state, "state": "declined", "result": result}}
        if store.settle_breakeven_notice(notice_id, status="declined", result=result, card=settled):
            await _republish(app, notice, settled)
        return _view(store.breakeven_notice(notice_id) or notice)
    registry = getattr(app.state, "accounts", None)
    services = registry.get(notice["tradingAccountId"]) if registry is not None else None
    autonomy = getattr(services, "autonomy", None)
    if autonomy is None:
        raise BreakEvenAnswerError("account_not_assembled", "这个账户现在没连上，稍后再点")
    cost = Decimal(str(state["cost"]))
    stop_ids = [str(item) for item in (state.get("stopIds") or [])]
    triggers = [str(item) for item in (state.get("triggers") or [])]
    if not stop_ids or len(stop_ids) != len(triggers):
        # A card from before the binding existed (#528): it names no rows of its own, so it can
        # never prove which position it was about — fail closed rather than touch today's stops.
        result = "这张保本提醒卡没有绑定当时的止损，核对不了目标，没移；请在持仓面板重设，或等下一次提醒重新确认"
        settled = {**card, "actions": [], "breakeven": {**state, "state": "failed", "result": result}}
        if store.settle_breakeven_notice(notice_id, status="failed", result=result, card=settled):
            await _republish(app, notice, settled)
        return _view(store.breakeven_notice(notice_id) or notice)
    moved_card: dict[str, Any] = {}

    def finish(connection: Any, result: dict[str, Any]) -> bool:
        words = f"{_lots_word(result['moved'])}已上移到 {price_words(cost)}"
        moved_card.update({**card, "actions": [], "breakeven": {**state, "state": "moved", "result": words}})
        return store.settle_breakeven_notice(notice_id, status="moved", result=words, card=moved_card,
                                             connection=connection)

    try:
        outcome = await autonomy.move_stops_to_cost(user_id=user_id, symbol=notice["symbol"],
                                                    direction=notice["direction"], cost=cost,
                                                    stop_ids=stop_ids, triggers=triggers, finish=finish)
    except AutonomyError as error:
        reason = error.message or str(error)
        if error.code in _TRANSIENT or error.code in {"account_read_only", "account_not_granted"}:
            raise BreakEvenAnswerError(error.code, reason) from error
        failed = {**card, "actions": [ask_action("问 AI", f"{notice['symbol']} 的止损没移到成本（{reason}），现在怎么办？")],
                  "breakeven": {**state, "state": "failed", "result": f"没移：{reason}"}}
        if store.settle_breakeven_notice(notice_id, status="failed", result=f"没移：{reason}", card=failed):
            await _republish(app, notice, failed)
        return _view(store.breakeven_notice(notice_id) or notice)
    if outcome is not None:
        await _republish(app, notice, moved_card)
    return _view(store.breakeven_notice(notice_id) or notice)


_ANSWER_TONES = {"moved": "ok", "declined": "muted", "failed": "warn"}


async def _republish(app: Any, notice: dict[str, Any], card: dict[str, Any]) -> None:
    """The settled card goes back to its conversation, and the 提醒中心 row follows it (#932): its state
    becomes the outcome and its 移到成本 / 不用 actions go away."""
    from .reminders import update_state
    state = card.get("breakeven") or {}
    result = str(state.get("result") or "")
    words = {"declined": "不移"}.get(str(state.get("state")), result)
    await update_state(app.state.store, app.state.broker, card_id=str(card.get("cardId") or ""),
                       state={"text": words[:80], "tone": _ANSWER_TONES.get(str(state.get("state")), "muted")},
                       clear_actions=True)
    if not notice.get("sessionId"):
        return
    try:
        await app.state.broker.publish(str(notice["sessionId"]), kind="artifact", name="card.reminder",
                                       payload={**card, "turnId": notice["id"]})
    except Exception as error:  # noqa: BLE001
        log.debug("breakeven card not republished: %s", error)


def _view(notice: dict[str, Any]) -> dict[str, Any]:
    return {"id": notice["id"], "symbol": notice["symbol"], "direction": notice["direction"],
            "tradeDay": notice["tradeDay"], "status": notice["status"], "result": notice["result"],
            "card": notice["card"], "createdAt": notice["createdAt"]}


def notices(app: Any, *, user_id: str) -> list[dict[str, Any]]:
    return [_view(item) for item in app.state.store.breakeven_notices(user_id=user_id)]
