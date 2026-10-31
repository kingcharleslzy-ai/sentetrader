"""交易验尸 (owner 2026-10-25, 第 3 批): each 笔 of the 今日复盘 checked against the user's own plan, by the
program and never by a model —

- 提前离场 ``early_exit``: the user closed by hand (ticket or card) while their stop was still armed and the
  price had reached neither the stop nor the take;
- 连亏后再开 ``after_losses``: the user opened again after two or more losing closes in a row that day;
- 没标打法 ``untagged``: an opening order of the user's with no setup, when they keep any setups at all;
- 止损差距 ``stop_beyond`` / ``no_stop``: a losing close that went past the planned stop, or a losing round
  trip that never had one;
- 不合入场规则 ``entry_miss``: the setup's linked strategy gave no entry signal for that side on the last bar
  completed before the order (a setup written only in words is judged by the review's model, checked there).

Each flagged 笔 that belongs to a round trip gets one question, answered into that round trip's 补充. The
last trading day of a week also names 本周最该改的一件事: the check that came up most that week, as a lesson the
user may 记住."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

NS = 1_000_000_000
USER_SOURCES = frozenset({"manual", "card"})
# The order a tie between two checks is broken in (the more costly habit first).
CODES = ("stop_beyond", "no_stop", "early_exit", "after_losses", "entry_miss", "untagged")
LABELS = {"early_exit": "提前离场", "after_losses": "连亏后再开", "untagged": "没标打法",
          "stop_beyond": "亏过止损", "no_stop": "没设止损", "entry_miss": "不合入场规则"}
QUESTIONS = {
    "early_exit": "止损和目标都没到，当时为什么先平了？",
    "after_losses": "连亏之后又开了这笔，当时是怎么想的？",
    "untagged": "这笔用的是哪个打法？",
    "stop_beyond": "价格过了止损还没走，当时在等什么？",
    "no_stop": "这笔没设止损，当时为什么没设？",
    "entry_miss": "入场不合打法的规则，当时看到了什么？",
}
LESSONS = {
    "early_exit": "止损和目标都没到时，不凭感觉先手动平仓",
    "after_losses": "连亏两笔后先停一停，不马上开新仓",
    "untagged": "每次开仓先标上用的是哪个打法",
    "stop_beyond": "到了计划的止损就走，不让亏损超过计划",
    "no_stop": "开仓就设好止损",
    "entry_miss": "只在符合打法入场规则时开仓",
}


def _price(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.4f}".rstrip("0").rstrip(".")


def _ns(value: Any) -> int | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return int((moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp() * NS)


def _level(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


def load_stops(store: Any, *, user_id: str, account_id: str) -> list[dict[str, Any]]:
    """The user's stops on this account (user-set and the AI's alike: every stop runs through ai_stop_orders),
    with the times they were armed and last changed, and each move of their levels (改动留痕, 2026-10-30)."""
    with store.lock:
        rows = store.connection.execute(
            "SELECT id, symbol, position_direction, trigger_price, take_profit_price, status, close_client_order_id,"
            " created_at, updated_at FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?",
            (user_id, account_id)).fetchall()
        moves = store.connection.execute(
            "SELECT object_id, at, before_json FROM state_changes WHERE object_type = 'protection'"
            " AND change = 'updated' AND user_id = ? AND trading_account_id = ?"
            " AND (json_extract(before_json, '$.triggerPrice') IS NOT json_extract(after_json, '$.triggerPrice')"
            " OR json_extract(before_json, '$.takeProfitPrice') IS NOT json_extract(after_json, '$.takeProfitPrice'))"
            " ORDER BY seq", (user_id, account_id)).fetchall()
    history: dict[str, list[tuple[int, float | None, float | None]]] = {}
    for move in moves:
        at, before = _ns(move["at"]), json.loads(move["before_json"])
        if at is not None:
            history.setdefault(str(move["object_id"]), []).append(
                (at, _level(before.get("triggerPrice")), _level(before.get("takeProfitPrice"))))
    stops = []
    for row in rows:
        created, updated = _ns(row["created_at"]), _ns(row["updated_at"])
        if created is None:
            continue
        stops.append({"symbol": str(row["symbol"]).upper(), "direction": row["position_direction"],
                      "trigger": _level(row["trigger_price"]), "take": _level(row["take_profit_price"]),
                      "armed": row["status"] == "ARMED", "closeOrderId": str(row["close_client_order_id"] or ""),
                      "created": created, "updated": updated if updated is not None else created,
                      "moves": history.get(str(row["id"]), [])})
    return stops


def _levels_at(stop: dict[str, Any], at: int) -> dict[str, Any]:
    """The stop with the levels it held at ``at``: the value before its first move after ``at``, else today's.
    A stop moved before 改动留痕 existed has no moves and reads at today's levels."""
    later = next((move for move in stop.get("moves") or () if move[0] > at), None)
    return stop if later is None else {**stop, "trigger": later[1], "take": later[2]}


def _armed_at(stops: list[dict[str, Any]], symbol: str, direction: str, at: int) -> dict[str, Any] | None:
    """The latest stop on this position that was in force at ``at`` (armed before it, not ended before it)."""
    live = [item for item in stops if item["symbol"] == symbol.upper() and item["direction"] == direction
            and item["created"] <= at and (item["armed"] or item["updated"] >= at)]
    return max(live, key=lambda item: item["created"], default=None)


def _leg_ns(leg: dict[str, Any]) -> int:
    return int(datetime.fromisoformat(leg["time"]).timestamp() * NS)


def check_legs(legs: list[dict[str, Any]], *, rounds: list[dict[str, Any]], sources: dict[str, dict[str, Any]],
               stops: list[dict[str, Any]], keeps_playbooks: bool) -> None:
    """Adds to each 笔, in place: ``roundId`` (the round trip it opened or closed, when there is one),
    ``playbookName`` (an opening 笔's setup — a later tag in the review wins over the card's), ``checks``
    ([{code, label, text}]) and ``question`` (for a flagged 笔 with a round trip)."""
    by_close = {str(item.get("closeTradeId")): item for item in rounds}
    by_open = {str(item.get("openTradeId")): item for item in rounds}
    losses = 0
    for leg in legs:
        trade_ids = leg.get("tradeIds") or []
        closing = leg["side"] in ("平多", "平空")
        mine = [by_close[t] for t in trade_ids if t in by_close] if closing else [by_open[t] for t in trade_ids if t in by_open]
        # shortcut: an order that closed several round trips answers into the first one's 补充.
        leg["roundId"] = mine[0]["id"] if mine else None
        info = sources.get(str(leg.get("orderId") or "")) or {}
        checks: list[dict[str, str]] = []
        at = _leg_ns(leg)
        if closing:
            direction = "long" if leg["side"] == "平多" else "short"
            long = direction == "long"
            price = float(leg["price"])
            stop = _armed_at(stops, leg["symbol"], direction, at)
            stop = _levels_at(stop, at) if stop is not None else None
            if leg["source"] in USER_SOURCES and stop is not None and stop["trigger"] is not None:
                inside = price > stop["trigger"] if long else price < stop["trigger"]
                if stop["take"] is not None:
                    inside = inside and (price < stop["take"] if long else price > stop["take"])
                if inside:
                    levels = f"止损 {_price(stop['trigger'])}" + (f"、止盈 {_price(stop['take'])}" if stop["take"] is not None else "")
                    checks.append({"code": "early_exit", "text": f"{levels} 都还没到，就手动平了"})
            if leg["pnl"] is not None and leg["pnl"] < 0:
                planned = next((_levels_at(item, at) for item in stops
                                if item["closeOrderId"] and item["closeOrderId"] == leg.get("orderId")), stop)
                if planned is not None and planned["trigger"] is not None:
                    gap = (planned["trigger"] - price) if long else (price - planned["trigger"])
                    if gap > 0:
                        checks.append({"code": "stop_beyond",
                                       "text": f"计划止损 {_price(planned['trigger'])}，实际 {_price(price)} 出场，多走了 {_price(round(gap, 4))}"})
                else:
                    opened = min((int(item["openTime"]) for item in mine), default=at)
                    ever = any(item["symbol"] == leg["symbol"].upper() and item["direction"] == direction
                               and item["created"] <= at and item["updated"] >= opened for item in stops)
                    if not ever:
                        checks.append({"code": "no_stop", "text": "这笔从开到平都没设止损"})
            if leg["pnl"] is not None:
                losses = losses + 1 if leg["pnl"] < 0 else 0
        else:
            tagged = next((item.get("playbookName") for item in mine if item.get("playbookName")), None)
            leg["playbookName"] = tagged or info.get("playbookName")
            leg["playbookId"] = next((item.get("playbookId") for item in mine if item.get("playbookName")), None) or info.get("playbookId")
            if leg["source"] in USER_SOURCES:
                if losses >= 2:
                    checks.append({"code": "after_losses", "text": f"连亏 {losses} 笔之后又开了这笔"})
                if keeps_playbooks and not leg["playbookName"]:
                    checks.append({"code": "untagged", "text": "开仓时没标打法"})
        leg["checks"] = [{**item, "label": LABELS[item["code"]]} for item in checks]
        leg["question"] = QUESTIONS[checks[0]["code"]] if checks and leg["roundId"] else None


def add_entry_fit(leg: dict[str, Any], *, fits: bool, basis: str, text: str) -> None:
    """合不合入场规则 on an opening 笔: a miss is also a check (and the 笔's question when it has none yet)."""
    leg["entryFit"] = {"fits": fits, "basis": basis, "text": text}
    if not fits:
        leg["checks"] = [*leg.get("checks", []), {"code": "entry_miss", "label": LABELS["entry_miss"], "text": text}]
        if leg.get("roundId") and not leg.get("question"):
            leg["question"] = QUESTIONS["entry_miss"]


def strategy_entry_fit(provider: Any, definition: dict[str, Any], symbol: str, *, at_ns: int, long: bool) -> bool | None:
    """Whether the strategy's entry rule for that side held on the last bar completed before ``at_ns`` — the
    monitor's own evaluation on the bars of that moment. None when it cannot say (a spread or chart-period
    plan, history not reaching back, a rule that needs data the workbench does not hold)."""
    from .strategy_rules import (
        StrategyPlan,
        StrategyRuleError,
        _schedule_of,
        evaluate_completed,
        plan_timeframes,
        uses_chart_timeframe,
    )
    try:
        plan = StrategyPlan.model_validate(definition["plan"])
        if plan.spread is not None or uses_chart_timeframe(plan):
            return None
        workbenches = {timeframe: provider.market_workbench(symbol, timeframe) for timeframe in plan_timeframes(plan)}
        bars = {timeframe: [bar for bar in item.get("bars") or [] if int(bar["datetime"]) + timeframe * NS <= at_ns]
                for timeframe, item in workbenches.items()}
        if not bars[plan.timeframeSeconds]:
            return None
        daily = (workbenches[86400].get("bars") or []) if 86400 in workbenches else None
        evaluation = evaluate_completed(plan, bars, daily=daily, schedule=_schedule_of(workbenches))
    except (KeyError, ValueError, TypeError, RuntimeError, StrategyRuleError):
        return None
    return bool(evaluation["rules"]["entryLong" if long else "entryShort"])


def is_week_end(trading_day: str, next_day: str | None) -> bool:
    """The last trading day of its week: the next trading day falls in a later week (a Friday holiday moves it
    to Thursday); without a calendar, a Friday."""
    day = date.fromisoformat(trading_day)
    if next_day is None:
        return day.weekday() == 4
    return date.fromisoformat(next_day).isocalendar()[:2] != day.isocalendar()[:2]


def week_lesson(trading_day: str, legs: list[dict[str, Any]],
                history: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
    """本周最该改的一件事: the check that came up most in this week's 笔 (today's and the earlier reviews of the
    same week), as {code, text, count, legs, tradeIds}; None for a week without any."""
    week = date.fromisoformat(trading_day).isocalendar()[:2]
    pool = [(leg["id"], leg) for leg in legs] + [
        (f"{day}/{leg['id']}", leg) for day, leg in history if date.fromisoformat(day).isocalendar()[:2] == week]
    counts: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for ref, leg in pool:
        for code in dict.fromkeys(item["code"] for item in leg.get("checks") or []):
            counts.setdefault(code, []).append((ref, leg))
    if not counts:
        return None
    code = max(counts, key=lambda item: (len(counts[item]), -CODES.index(item)))
    hits = counts[code]
    return {"code": code, "label": LABELS[code], "text": LESSONS[code], "count": len(hits),
            "legs": [ref for ref, _ in hits], "tradeIds": [trade for _, leg in hits for trade in leg.get("tradeIds") or []]}
