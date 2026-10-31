"""价差策略第二期 (2026-09-29): the live two-leg executor.

One spread unit = A lotsA 手 + B lotsB 手; opening or closing dispatches both legs simultaneously
at the opponent's price (aggressive). Any leg not fully filled within ``SPREAD_FILL_WAIT_SECONDS``
is cancelled and chased ONCE at the latest opponent price; a leg still short after the second wait
rolls an open back (close the filled lots of both legs, pause, notify) or strands a close (cancel
the remainder, pause, notify — never a reverse open, never an automatic re-send). A leg whose
result is unknown is never re-sent and never rolled back: the strategy pauses and the user
verifies. Every order — chase and rollback legs included — is recorded on
strategy_execution_attempts / strategy_execution_legs with its real result, never a claim that it
was sent when it was not.

The legs carry no per-leg ai_stop_orders (``execute_trade(strategy_spread=True)``): the spread's
protection is the per-second 价差止损/止盈 judgement the monitor runs while the position is held.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic, time_ns
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from .data_providers import provider_read_snapshot
from .order_planner import close_legs, prefs_for
from .simulation_orders import SimulationOrderError, close_batch_execution

if TYPE_CHECKING:
    from .strategy_monitor import StrategyMonitorService

LOGGER = logging.getLogger(__name__)

# 瘸腿等待：how long a leg may stay unfilled before its remainder is cancelled and chased once
# (module-level so tests run the whole lifecycle quickly).
SPREAD_FILL_WAIT_SECONDS = 15.0
# 成交回读节奏：while a wait lasts, the in-flight legs are re-read this often, so an instant fill
# settles on the first poll instead of holding the strategy's protective exit hostage for the whole
# wait (每秒回读一次在途腿).
SPREAD_POLL_SECONDS = 1.0
# 回退持仓等待：fills can reach the account's position rows late; the rollback re-reads them this
# long before closing only what it can see (以执行器自己记录的成交手数为准，绝不静默少平).
SPREAD_POSITION_WAIT_SECONDS = 10.0

LEG_BAD_STATUSES = frozenset({"NOT_DISPATCHED", "REJECTED", "CANCELLED", "FAILED"})
LEG_ALIVE_STATUSES = frozenset({"SUBMITTED", "PARTIAL"})

# The settled attempt's conclusion in the user's words — the card's 执行结论 line, both on the
# executor's final push and on every idempotent replay of the confirmed card.
CARD_CONCLUSIONS = {
    "filled": "成交完成",
    "filled_chased": "已补单成交",
    "rolled_back": "已回退并暂停",
    "leftover": "仍有剩余请手动处理",
    "unknown": "结果未知请核对",
}
# A confirmed card's attempt stopped before any leg went out (an exception, or a restart before the follow marker).
CONFIRM_INTERRUPTED_DETAIL = "确认后执行中断，两腿没有发出；请核对持仓后重新确认。"


def spread_leg_sides(action: str) -> tuple[str, str]:
    """做多价差 = 买A卖B，做空 = 卖A买B；平仓是各自的反向。"""
    first_buys = action in {"open_long", "close_short"}
    first = "buy" if first_buys else "sell"
    second = "sell" if first_buys else "buy"
    suffix = "open" if action.startswith("open") else "close"
    return f"{first}_{suffix}", f"{second}_{suffix}"


def spread_leg_plans(
    spec: Any, action: str, units: int, positions: list[dict[str, Any]], *, preference: str,
) -> list[dict[str, Any]]:
    """The concrete orders one spread action becomes: per leg ``units × lots`` 手, a close split the
    way every card path splits it (`order_planner.close_legs`: frozen deducted, SHFE / INE by the
    平今平昨 `preference`)."""
    side_a, side_b = spread_leg_sides(action)
    plans: list[dict[str, Any]] = []
    for index, (leg, side) in enumerate(zip(spec.legs, (side_a, side_b))):
        volume = max(0, int(units)) * int(leg.lots)
        if volume <= 0:
            continue
        if side.endswith("_close"):
            direction = "long" if side == "sell_close" else "short"
            for bucket, bucket_volume in close_legs(positions, symbol=leg.symbol, direction=direction,
                                                    volume=volume, preference=preference):
                plans.append({"symbol": leg.symbol, "side": side, "volume": bucket_volume,
                              "bucket": bucket, "tag": f"{'AB'[index]}腿"})
        else:
            plans.append({"symbol": leg.symbol, "side": side, "volume": volume,
                          "bucket": None, "tag": f"{'AB'[index]}腿"})
    return plans


@dataclass
class _Leg:
    """One order row inside a spread attempt (an initial leg, its 补单 chase, or a rollback close)."""

    leg_id: str
    index: int
    symbol: str
    side: Literal["buy_open", "sell_open", "buy_close", "sell_close"]
    volume: int
    bucket: Literal["today", "historical", "all"] | None
    tag: str
    client_order_id: str | None = None
    status: str = "RESERVED"
    detail: str = ""
    filled: int = 0
    parent: _Leg | None = field(default=None, repr=False)

    @property
    def root(self) -> _Leg:
        leg = self
        while leg.parent is not None:
            leg = leg.parent
        return leg


def _group_filled(legs: list[_Leg], leg: _Leg) -> int:
    """The lots a leg's whole tree (itself + its chases) has filled."""
    root = leg.root
    return sum(other.filled for other in legs if other.root is root)


def _directional_volume(positions: list[dict[str, Any]], symbol: str, direction: str) -> int:
    """The account's held volume of one symbol in one direction (the rollback waits until this
    covers its own fill record)."""
    return sum(max(0, int(item.get("volume") or 0))
               for item in positions
               if isinstance(item, dict)
               and str(item.get("symbol") or "").casefold() == symbol.casefold()
               and item.get("direction") == direction)


class SpreadExecutionService:
    """Both legs of one spread action, dispatched together and followed to a definite outcome."""

    def __init__(self, monitor: StrategyMonitorService) -> None:
        self.monitor = monitor
        self.store = monitor.store
        self.autonomy = monitor.autonomy
        self.proposals = monitor.proposals
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running: set[str] = set()

    # -- entry points ------------------------------------------------------------------

    def running(self, strategy_id: str) -> bool:
        """An attempt of this strategy is still being followed (fills pending)."""
        return strategy_id in self._running

    async def drain(self) -> None:
        """Tests: let every followed attempt reach its outcome."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute_auto(
        self, *, strategy: dict[str, Any], plan: Any, intent: dict[str, Any], action: str,
        volume: int, bar_time: str, trading_day: str,
    ) -> dict[str, Any]:
        """自动下单: the monitor's spread signal — reserve, dispatch both legs, follow in the
        background (the monitor loop never waits on a fill)."""
        attempt_id = str(uuid4())
        if not self.store.reserve_strategy_execution_attempt(
            attempt_id=attempt_id, strategy_id=strategy["strategyId"], user_id=strategy["userId"],
            intent_id=intent["intentId"], signal_bar_time=bar_time, trading_day=trading_day,
            action=action, volume=volume, trading_account_id=self.monitor.account_id,
        ):
            return {"strategyId": strategy["strategyId"], "status": "duplicate", "detail": "该已完成K线已处理。"}
        return await self._dispatch_and_follow(
            attempt_id=attempt_id, strategy=strategy, plan=plan, action=action, units=volume,
            trading_day=trading_day, bar_time=bar_time, ai_initiated=True, exit_reason=None,
        )

    async def execute_exit(
        self, *, strategy: dict[str, Any], plan: Any, held: dict[str, Any], exit_reason: str,
        intent_id: str, bar_time: str, trading_day: str, current: float, entry: float,
    ) -> dict[str, Any]:
        """每秒价差止损/止盈: close the held spread position through the ordinary two-leg flow. The
        close is protective — it does not wait for a card even in 出卡确认 mode (an armed stop does
        not either); alert mode never reaches here, the monitor reminds instead."""
        from .strategy_spread import spread_value_text, spread_words
        action = f"close_{held['direction']}"
        units = int(held["volume"])
        attempt_id = str(uuid4())
        if not self.store.reserve_strategy_execution_attempt(
            attempt_id=attempt_id, strategy_id=strategy["strategyId"], user_id=strategy["userId"],
            intent_id=intent_id, signal_bar_time=bar_time, trading_day=trading_day,
            action=action, volume=units, trading_account_id=self.monitor.account_id,
        ):
            return {"strategyId": strategy["strategyId"], "status": "duplicate", "detail": "这一价差持仓的止损/止盈已触发。"}
        result = await self._dispatch_and_follow(
            attempt_id=attempt_id, strategy=strategy, plan=plan, action=action, units=units,
            trading_day=trading_day, bar_time=bar_time, ai_initiated=False, exit_reason=exit_reason,
        )
        label = "止损" if exit_reason == "spread_stop" else "止盈"
        text = (f"{self.monitor._display_name(strategy)} · {spread_words(plan.spread)}：价差{label}触发"
                f"（当前价差 {spread_value_text(current)}，开仓价差 {spread_value_text(entry)}），"
                "两腿正在按对手价平仓。")
        await self.monitor._session_notice(strategy, text, kind="strategy-signal")
        return result

    async def launch_confirmed(
        self, *, payload: dict[str, Any], user_id: str, session_id: str,
    ) -> dict[str, Any]:
        """出卡确认: the user confirmed a spread card — its waiting attempt resumes and both legs
        dispatch on the user's own authority (works with AI 自主执行 off, like any confirmed card)."""
        from .strategy_rules import StrategyPlan
        source = payload.get("strategySource") or {}
        attempt_id = str(source.get("attemptId") or "")
        if not attempt_id:
            raise SimulationOrderError("spread_card_invalid")
        strategy = self.store.get_strategy_draft(str(source["strategyId"]), user_id=user_id)
        # The pre-confirm guard already refuses a paused strategy's card before the confirmation
        # commits; this second look only covers the race where it paused in between — refusing here
        # still orders nothing (each leg would otherwise fail dispatch and read like an accident).
        if strategy["status"] != "monitoring":
            raise SimulationOrderError("strategy_not_monitoring")
        plan = StrategyPlan.model_validate(strategy["definition"]["plan"])
        units = int(payload.get("units") or 0)
        if plan.spread is None or units <= 0:
            raise SimulationOrderError("spread_card_invalid")
        if not self.store.reopen_strategy_execution_attempt(attempt_id=attempt_id):
            raise SimulationOrderError("spread_attempt_not_waiting")
        try:
            account = self.autonomy.data_provider.account_summary("")
            await self._dispatch_and_follow(
                attempt_id=attempt_id, strategy=strategy, plan=plan, action=str(payload["action"]),
                units=units, trading_day=str(account.get("tradingDay") or ""),
                bar_time=str(source.get("barTime") or time_ns()), ai_initiated=False,
                exit_reason=payload.get("exitReason"), card=payload, card_session_id=session_id,
            )
        except Exception:
            # 518-F: 重开后的尝试带着 signal_action，重启清扫不收它。中断发生在标记 follow
            # 之前时一条腿都没发——写成 FAILED 说实话；已进 follow 的属于 UNKNOWN 语义，不碰。
            row = self.store.connection.execute(
                "SELECT follow_state FROM strategy_execution_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if row is not None and str(row["follow_state"] or "") != "following":
                self.store.update_strategy_execution_attempt(
                    attempt_id=attempt_id, status="FAILED", detail=CONFIRM_INTERRUPTED_DETAIL)
            raise
        card = {**payload, **self.confirmed_view(payload, user_id=user_id)}
        await self._republish_card(session_id, card)
        return card

    def confirmed_view(
        self, payload: dict[str, Any], *, user_id: str, connection: Any = None,
    ) -> dict[str, Any]:
        """The card's answer: what each leg's order actually is right now, read back from its own
        link — the same 口径 as close_batch (逐笔真实结果，永不冒充已发送) — plus, once the follow has
        settled, the attempt's 执行结论 line (an idempotent replay says the same thing)."""
        attempt_id = str((payload.get("strategySource") or {}).get("attemptId") or "")
        orders: list[dict[str, Any]] = []
        reader = connection or self.store.connection
        with self.store.lock:
            rows = reader.execute(
                "SELECT client_order_id FROM strategy_execution_legs WHERE attempt_id = ? AND client_order_id IS NOT NULL ORDER BY leg_index",
                (attempt_id,),
            ).fetchall()
            outcome_row = reader.execute(
                "SELECT outcome FROM strategy_execution_attempts WHERE id = ?", (attempt_id,)).fetchone()
        for row in rows:
            try:
                fact = self.autonomy.simulation_orders.get(
                    str(row["client_order_id"]), user_id=user_id, connection=connection)
            except KeyError:
                continue
            orders.append({**fact, "symbol": fact["request"]["symbol"]})
        view: dict[str, Any] = {"simulationOrders": orders, "execution": close_batch_execution(orders)}
        outcome = str(outcome_row["outcome"] or "") if outcome_row is not None else ""   # NULL = follow still running
        if outcome:
            view["executionConclusion"] = {"code": outcome, "text": CARD_CONCLUSIONS.get(outcome, "结果未知请核对")}
        return view

    async def recover_interrupted(self) -> None:
        """Restart sweep: a spread attempt whose follow was still running is UNKNOWN — the legs'
        true state cannot be rebuilt from here — and the strategy pauses for the user to verify
        both legs. A completed spread trade (follow done) and a card still waiting for its
        confirmation are left alone."""
        rows = self.store.connection.execute(
            """SELECT a.id, a.strategy_id, a.user_id FROM strategy_execution_attempts a
            JOIN strategy_drafts d ON d.id = a.strategy_id AND d.user_id = a.user_id
            WHERE a.trading_account_id = ?
              AND (a.follow_state = 'following'
                   OR (a.status = 'RESERVED' AND a.signal_action IS NULL))
              AND json_extract(d.definition_json, '$.plan.spread') IS NOT NULL""",
            (self.monitor.account_id,),
        ).fetchall()
        for row in rows:
            self.store.update_strategy_execution_attempt(
                attempt_id=str(row["id"]), status="UNKNOWN", outcome="unknown",
                detail="Runtime 在价差执行中途重启，两腿执行状态未知，请核对两腿持仓后手动处理。")
            try:
                strategy = self.store.get_strategy_draft(str(row["strategy_id"]), user_id=str(row["user_id"]))
            except KeyError:
                continue
            self._pause_strategy(strategy)
            from .strategy_rules import SpreadSpec
            from .strategy_spread import spread_words
            spec = SpreadSpec.model_validate((strategy["definition"].get("plan") or {}).get("spread") or {})
            text = (f"{self.monitor._display_name(strategy)} · {spread_words(spec)}：Runtime 在价差执行中途重启，"
                    "两腿执行状态未知，已暂停策略；请核对两腿持仓后手动处理，再决定是否继续。")
            await self.monitor._session_notice(strategy, text, kind="strategy-paused")
        # #518: a confirmed card's attempt reopened (SKIPPED → RESERVED, signal_action set) but killed before
        # `_dispatch_and_follow` marked it 'following'. That marker is written before any leg is reserved or
        # sent, so no leg went out: the attempt ends FAILED like the confirm path's own interruption (518-F),
        # nothing is sent again and the strategy keeps running.
        unsent = self.store.connection.execute(
            """SELECT a.id, a.strategy_id, a.user_id FROM strategy_execution_attempts a
            JOIN strategy_drafts d ON d.id = a.strategy_id AND d.user_id = a.user_id
            WHERE a.trading_account_id = ? AND a.status = 'RESERVED' AND a.signal_action IS NOT NULL
              AND a.follow_state IS NULL
              AND json_extract(d.definition_json, '$.plan.spread') IS NOT NULL""",
            (self.monitor.account_id,),
        ).fetchall()
        for row in unsent:
            self.store.update_strategy_execution_attempt(
                attempt_id=str(row["id"]), status="FAILED", detail=CONFIRM_INTERRUPTED_DETAIL)
            try:
                strategy = self.store.get_strategy_draft(str(row["strategy_id"]), user_id=str(row["user_id"]))
            except KeyError:
                continue
            await self.monitor._session_notice(
                strategy, f"{self.monitor._display_name(strategy)}：价差卡确认后 Runtime 重启，两腿没有发出；"
                          "请核对持仓后重新确认。", kind="strategy-signal")

    # -- one attempt -------------------------------------------------------------------

    async def _dispatch_and_follow(
        self, *, attempt_id: str, strategy: dict[str, Any], plan: Any, action: str, units: int,
        trading_day: str, bar_time: str, ai_initiated: bool, exit_reason: str | None,
        card: dict[str, Any] | None = None, card_session_id: str | None = None,
    ) -> dict[str, Any]:
        strategy_id = strategy["strategyId"]
        spec = plan.spread
        positions = self.autonomy.data_provider.positions()
        reason = f"策略 {strategy['name']} · 价差{'开仓' if action.startswith('open') else '平仓'} {units} 份"
        legs: list[_Leg] = []
        leg_plans = spread_leg_plans(spec, action, units, positions,
                                     preference=prefs_for(self.store, strategy["userId"]).close_preference)
        expected = {leg.symbol for leg in spec.legs if int(units) * int(leg.lots) > 0}
        missing = expected - {item["symbol"] for item in leg_plans}
        if missing:                                   # a close leg with nothing to close orders nothing
            names = "、".join(sorted(missing))
            self.store.update_strategy_execution_attempt(
                attempt_id=attempt_id, status="BLOCKED", detail=f"{names}没有足够可平持仓")
            return {"strategyId": strategy_id, "status": "blocked", "detail": f"{names}没有足够可平持仓。"}
        # From here the attempt is followed in the background: a restart while this marker says
        # 'following' sweeps the attempt to UNKNOWN (the crash may have been mid-fill).
        self.store.mark_strategy_execution_follow(attempt_id=attempt_id, state="following")
        for leg_plan in leg_plans:
            leg = _Leg(leg_id=str(uuid4()), index=len(legs), symbol=leg_plan["symbol"],
                       side=leg_plan["side"], volume=leg_plan["volume"], bucket=leg_plan["bucket"],
                       tag=leg_plan["tag"])
            self.store.reserve_strategy_execution_leg(
                leg_id=leg.leg_id, attempt_id=attempt_id, leg_index=leg.index,
                position_bucket=leg.bucket, volume=leg.volume)
            legs.append(leg)
        for leg in legs:
            await self._dispatch_leg(leg=leg, strategy=strategy, ai_initiated=ai_initiated, reason=reason)
        statuses = [leg.status for leg in legs]
        if "UNKNOWN" in statuses:
            status = "UNKNOWN"
        elif any(item in LEG_BAD_STATUSES for item in statuses):
            status = ("PARTIAL" if any(item in LEG_ALIVE_STATUSES for item in statuses)
                      else next(item for item in statuses if item in LEG_BAD_STATUSES))
        elif "PARTIAL" in statuses:
            status = "PARTIAL"
        else:
            status = "SUBMITTED"
        client_ids = self._client_ids(legs)
        self.store.update_strategy_execution_attempt(
            attempt_id=attempt_id, status=status, client_order_id=",".join(client_ids),
            detail=self._attempt_detail(legs))
        self._spawn(attempt_id, strategy=strategy, plan=plan, action=action, legs=legs,
                    trading_day=trading_day, bar_time=bar_time, ai_initiated=ai_initiated,
                    exit_reason=exit_reason, card=card, card_session_id=card_session_id)
        return {"strategyId": strategy_id, "status": status.lower(),
                "clientOrderIds": client_ids, "detail": self._attempt_detail(legs)}

    def _spawn(self, attempt_id: str, **kwargs: Any) -> None:
        self._running.add(str(kwargs["strategy"]["strategyId"]))
        task = asyncio.create_task(self._supervise(attempt_id=attempt_id, **kwargs))
        self._tasks[attempt_id] = task

    async def _supervise(
        self, *, attempt_id: str, strategy: dict[str, Any], plan: Any, action: str,
        legs: list[_Leg], trading_day: str, bar_time: str, ai_initiated: bool,
        exit_reason: str | None, card: dict[str, Any] | None = None,
        card_session_id: str | None = None,
    ) -> None:
        finished = False
        try:
            broken = [leg for leg in legs if leg.status in LEG_BAD_STATUSES]
            if not broken or all(_group_filled(legs, leg.root) >= leg.root.volume for leg in broken):
                outcome = await self._fill_legs(attempt_id=attempt_id, strategy=strategy, legs=legs,
                                                ai_initiated=ai_initiated)
            else:
                # A leg that never went out (or was refused) skips the waits: the rule is immediate.
                outcome = "short"
            if outcome == "unknown" or any(leg.status == "UNKNOWN" for leg in legs):
                await self._settle_unknown(attempt_id=attempt_id, strategy=strategy, plan=plan, legs=legs)
            elif outcome == "filled":
                self._settle_success(attempt_id=attempt_id, strategy=strategy, legs=legs,
                                     trading_day=trading_day, bar_time=bar_time)
                if exit_reason:
                    # 每秒价差止损/止盈's second word: the trigger notice said the legs were closing;
                    # only this says they closed. (回退 / 平不掉 / 未知 settle with their own notice.)
                    from .strategy_spread import spread_words
                    label = "止损" if exit_reason == "spread_stop" else "止盈"
                    await self.monitor._session_notice(
                        strategy,
                        f"{self.monitor._display_name(strategy)} · {spread_words(plan.spread)}："
                        f"价差{label}平仓完成，两腿已全部平仓。", kind="strategy-signal")
            elif action.startswith("open"):
                await self._rollback_open(attempt_id=attempt_id, strategy=strategy, plan=plan,
                                          legs=legs, trading_day=trading_day, ai_initiated=ai_initiated)
            else:
                await self._strand_close(attempt_id=attempt_id, strategy=strategy, plan=plan, legs=legs)
            finished = True
        except asyncio.CancelledError:
            raise          # stays 'following': a restart (or the test that cancelled this task) sweeps it to UNKNOWN
        except Exception as error:
            LOGGER.exception("spread_attempt_failed strategy=%s attempt=%s",
                             strategy["strategyId"], attempt_id)
            self.store.update_strategy_execution_attempt(
                attempt_id=attempt_id, status="FAILED", outcome="unknown",
                detail=f"价差执行异常：{error}"[:200])
            finished = True
        finally:
            if finished:
                self.store.mark_strategy_execution_follow(attempt_id=attempt_id, state="done")
                if card is not None and card_session_id:
                    # The card-initiated attempt's last word: the legs' real results plus the
                    # conclusion line, re-sent on the same proposalId so the card updates in place.
                    view = {**card, **self.confirmed_view(card, user_id=strategy["userId"])}
                    await self._republish_card(card_session_id, view)
            self._tasks.pop(attempt_id, None)
            self._running.discard(str(strategy["strategyId"]))

    async def _fill_legs(
        self, *, attempt_id: str, strategy: dict[str, Any], legs: list[_Leg], ai_initiated: bool,
    ) -> str:
        """Poll the in-flight legs every ``SPREAD_POLL_SECONDS`` until they fill (an instant fill
        settles on the first poll — the wait never blinds the strategy's protective exit for a
        full cycle), chase unfilled remainders once at the deadline, poll through the second wait:
        ``filled`` / ``unknown`` / ``short``. A leg whose result is unknown stops everything —
        never re-sent, never chased."""
        user_id = strategy["userId"]
        for cycle in (1, 2):
            deadline = monotonic() + SPREAD_FILL_WAIT_SECONDS
            while True:
                for leg in [leg for leg in legs
                            if leg.status in LEG_ALIVE_STATUSES and leg.client_order_id]:
                    await self._poll_leg(leg, user_id=user_id)
                if self._primaries_filled(legs):
                    return "filled"
                if any(leg.status == "UNKNOWN" for leg in legs):
                    return "unknown"
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(SPREAD_POLL_SECONDS, remaining))
            if cycle == 1:
                for leg in [leg for leg in legs if leg.parent is None]:
                    if _group_filled(legs, leg) < leg.volume:
                        await self._chase_leg(attempt_id=attempt_id, leg=leg, legs=legs,
                                              strategy=strategy, ai_initiated=ai_initiated)
                        if any(item.status == "UNKNOWN" for item in legs):
                            return "unknown"
        return "filled" if self._primaries_filled(legs) else "short"

    @staticmethod
    def _primaries_filled(legs: list[_Leg]) -> bool:
        primaries = [leg for leg in legs if leg.parent is None]
        return bool(primaries) and all(_group_filled(legs, leg) >= leg.volume for leg in primaries)

    async def _dispatch_leg(
        self, *, leg: _Leg, strategy: dict[str, Any], ai_initiated: bool, reason: str,
    ) -> None:
        from .strategy_monitor import _execution_status
        user_id = strategy["userId"]
        bound: dict[str, str] = {}

        def record_identity(_proposal_id: str, client_order_id: str) -> None:
            bound["client_order_id"] = client_order_id
            self.store.bind_strategy_execution_leg(
                leg_id=leg.leg_id, client_order_id=client_order_id)

        try:
            result = await self.autonomy.execute_trade(
                user_id=user_id, session_id=self.monitor._session_id(user_id),
                symbol=leg.symbol, side=leg.side, volume=leg.volume,
                price_intent="aggressive", position_bucket=leg.bucket, reason=reason,
                before_dispatch=record_identity, strategy_id=strategy["strategyId"],
                ai_initiated=ai_initiated, strategy_spread=True)
        except Exception as error:
            client_order_id = bound.get("client_order_id")
            if client_order_id:
                # The order link exists but never reached the gateway: abandoned, never auto-sent.
                try:
                    self.autonomy.simulation_orders.abandon(client_order_id, user_id=user_id)
                except SimulationOrderError:
                    pass
                try:
                    order = self.autonomy.simulation_orders.get(client_order_id, user_id=user_id)
                    status, detail, _ = _execution_status(str(order["displayStatus"]))
                except (KeyError, RuntimeError, ValueError):
                    status, detail = "NOT_DISPATCHED", str(error)[:120]
            else:
                status, detail = "NOT_DISPATCHED", str(error)[:120]
            self._settle_leg(leg, status=status, detail=detail, client_order_id=client_order_id)
            return
        order = result["simulationOrder"]
        status, detail, _ = _execution_status(str(order.get("displayStatus") or ""))
        leg.filled = max(leg.filled, int(order.get("filledVolume") or 0))
        self._settle_leg(leg, status=status, detail=detail,
                         client_order_id=str(result["clientOrderId"]))

    async def _poll_leg(self, leg: _Leg, *, user_id: str) -> None:
        from .strategy_monitor import _execution_status
        if leg.client_order_id is None:
            return
        try:
            order = await self.autonomy.query_order(user_id=user_id, client_order_id=leg.client_order_id)
        except (SimulationOrderError, KeyError, RuntimeError, ValueError):
            self._settle_leg(leg, status="UNKNOWN", detail="回读失败，只回读原委托。")
            return
        status, detail, _ = _execution_status(str(order.get("displayStatus") or ""))
        leg.filled = max(leg.filled, int(order.get("filledVolume") or 0))
        self._settle_leg(leg, status=status, detail=detail)

    async def _cancel_remainder(self, leg: _Leg, *, user_id: str) -> str:
        """Cancel what is still working on this leg: ``ok`` (the view is final) or ``unknown``."""
        from .simulation_orders import order_is_working
        from .strategy_monitor import _execution_status
        if leg.client_order_id is None:
            return "ok"
        try:
            view = await self.autonomy.cancel_order(user_id=user_id, client_order_id=leg.client_order_id)
        except SimulationOrderError:
            await self._poll_leg(leg, user_id=user_id)
            return "unknown" if leg.status == "UNKNOWN" else "ok"
        status, detail, _ = _execution_status(str(view.get("displayStatus") or ""))
        leg.filled = max(leg.filled, int(view.get("filledVolume") or 0))
        if status == "UNKNOWN" or order_is_working(view):
            # 撤单回执 NOT_DISPATCHED 等情况: 命令没派发出去，原单仍挂在场上 —— 不是“已撤”
            # (#518)。按结果未知收尾: 不补单、不回退，停下让人核对。
            self._settle_leg(leg, status="UNKNOWN", detail="撤单未派发，原单仍在场，只回读原委托。")
            return "unknown"
        self._settle_leg(leg, status=status, detail=detail)
        return "ok"

    async def _chase_leg(
        self, *, attempt_id: str, leg: _Leg, legs: list[_Leg], strategy: dict[str, Any],
        ai_initiated: bool,
    ) -> None:
        """瘸腿: cancel this leg's remainder and re-order the missing lots ONCE at the latest
        opponent price (a fresh leg row — the chase is visible as its own order)."""
        user_id = strategy["userId"]
        if leg.client_order_id and leg.status in LEG_ALIVE_STATUSES:
            if await self._cancel_remainder(leg, user_id=user_id) == "unknown":
                return
        remainder = leg.volume - _group_filled(legs, leg)
        if remainder <= 0:
            return
        chase = _Leg(leg_id=str(uuid4()), index=max(item.index for item in legs) + 1,
                     symbol=leg.symbol, side=leg.side, volume=remainder, bucket=leg.bucket,
                     tag=f"{leg.tag}补单", parent=leg)
        self.store.reserve_strategy_execution_leg(
            leg_id=chase.leg_id, attempt_id=attempt_id, leg_index=chase.index,
            position_bucket=chase.bucket, volume=chase.volume)
        legs.append(chase)
        await self._dispatch_leg(leg=chase, strategy=strategy, ai_initiated=ai_initiated,
                                 reason=f"策略 {strategy['name']} · 价差瘸腿补单")

    # -- outcomes ----------------------------------------------------------------------

    def _settle_success(
        self, *, attempt_id: str, strategy: dict[str, Any], legs: list[_Leg],
        trading_day: str, bar_time: str,
    ) -> None:
        self.store.update_strategy_execution_attempt(
            attempt_id=attempt_id, status="SUBMITTED", client_order_id=",".join(self._client_ids(legs)),
            outcome="filled_chased" if any(leg.parent is not None for leg in legs) else "filled",
            detail=self._attempt_detail(legs))
        self.store.note_strategy_executed_bar(
            strategy_id=strategy["strategyId"], user_id=strategy["userId"],
            trading_day=trading_day, bar_time=bar_time)

    async def _settle_unknown(
        self, *, attempt_id: str, strategy: dict[str, Any], plan: Any, legs: list[_Leg],
    ) -> None:
        """结果未知: nothing is re-sent and nothing rolled back — the legs' working orders stay as
        they are; the strategy pauses and the user verifies."""
        from .strategy_rules import SpreadSpec
        from .strategy_spread import spread_words
        spec = SpreadSpec.model_validate(plan.spread.model_dump())
        self.store.update_strategy_execution_attempt(
            attempt_id=attempt_id, status="UNKNOWN", client_order_id=",".join(self._client_ids(legs)),
            outcome="unknown", detail=f"{self._attempt_detail(legs)}；结果未知，不自动重发、不回退。")
        self._pause_strategy(strategy)
        unknown = "、".join(f"{leg.tag}（{leg.symbol}）" for leg in legs if leg.status == "UNKNOWN") or "有腿"
        text = (f"{self.monitor._display_name(strategy)} · {spread_words(spec)}：{unknown}委托结果未知，"
                "不自动重发也不回退，请到委托里核对两腿持仓后手动处理。策略已暂停。")
        await self.monitor._session_notice(strategy, text, kind="strategy-paused")

    async def _rollback_open(
        self, *, attempt_id: str, strategy: dict[str, Any], plan: Any, legs: list[_Leg],
        trading_day: str, ai_initiated: bool,
    ) -> None:
        """开仓没成齐：cancel what is still working, close the filled lots of both legs at the
        opponent's price (the rollback closes get the same one chase), pause, notify. The close
        size is the executor's own fill record — the account's position rows only split it into
        今/昨 buckets and can lag the fills, so they are re-read until they cover the fills, and
        whatever is still missing is said out loud (X 腿还有 N 手需手动平掉), never silently skipped."""
        from .strategy_rules import SpreadSpec
        from .strategy_spread import spread_words
        user_id = strategy["userId"]
        spec = SpreadSpec.model_validate(plan.spread.model_dump())
        for leg in [leg for leg in legs
                    if leg.status in LEG_ALIVE_STATUSES and leg.client_order_id]:
            if await self._cancel_remainder(leg, user_id=user_id) == "unknown":
                return await self._settle_unknown(attempt_id=attempt_id, strategy=strategy,
                                                  plan=plan, legs=legs)
        primaries = [leg for leg in legs if leg.parent is None]
        wanted = [(leg.symbol, "long" if leg.side == "buy_open" else "short", _group_filled(legs, leg))
                  for leg in primaries if _group_filled(legs, leg) > 0]
        positions = await self._rollback_positions(wanted)
        rollback: list[_Leg] = []
        rollback_for: dict[int, list[_Leg]] = {}
        for leg in primaries:
            filled = _group_filled(legs, leg)
            if filled <= 0:
                continue
            close_side: Literal["sell_close", "buy_close"] = "sell_close" if leg.side == "buy_open" else "buy_close"
            direction = "long" if close_side == "sell_close" else "short"
            rollback_for[id(leg)] = []
            for bucket, volume in close_legs(positions, symbol=leg.symbol, direction=direction, volume=filled,
                                             preference=prefs_for(self.store, user_id).close_preference):
                row = _Leg(leg_id=str(uuid4()), index=max((item.index for item in [*legs, *rollback]), default=-1) + 1,
                           symbol=leg.symbol, side=close_side, volume=volume,
                           bucket=cast(Literal["today", "historical", "all"], bucket),
                           tag=f"回退平{leg.tag}")
                self.store.reserve_strategy_execution_leg(
                    leg_id=row.leg_id, attempt_id=attempt_id, leg_index=row.index,
                    position_bucket=row.bucket, volume=row.volume)
                rollback.append(row)
                rollback_for[id(leg)].append(row)
        if rollback:
            for row in rollback:
                await self._dispatch_leg(leg=row, strategy=strategy, ai_initiated=ai_initiated,
                                         reason=f"策略 {strategy['name']} · 价差开仓回退")
            outcome = await self._fill_legs(attempt_id=attempt_id, strategy=strategy, legs=rollback,
                                            ai_initiated=ai_initiated)
            if outcome == "unknown" or any(row.status == "UNKNOWN" for row in rollback):
                return await self._settle_unknown(attempt_id=attempt_id, strategy=strategy,
                                                  plan=plan, legs=[*legs, *rollback])
        # 每条腿的剩余 = 执行器记到的成交 − 回退腿实际平掉 (含补单) − 持仓一直没出现而没排进回退的部分)。
        leftovers = [f"{leg.tag}（{leg.symbol}）还有 {filled - sum(_group_filled(rollback, row) for row in rollback_for[id(leg)])} 手需手动平掉"
                     for leg in primaries
                     if (filled := _group_filled(legs, leg)) > 0
                     and filled > sum(_group_filled(rollback, row) for row in rollback_for.get(id(leg), []))]
        rollback_result = ("两腿没有成交，无需回退" if not wanted
                           else "、".join(leftovers) if leftovers else "回退完成")
        fills = "、".join(f"{leg.tag}成交 {_group_filled(legs, leg)} 手"
                          for leg in primaries if _group_filled(legs, leg) > 0) or "两腿均未成交"
        legs.extend(rollback)
        self.store.update_strategy_execution_attempt(
            attempt_id=attempt_id, status="CANCELLED", client_order_id=",".join(self._client_ids(legs)),
            outcome="leftover" if leftovers else "rolled_back",
            detail=f"{self._attempt_detail(legs)}；开仓未成齐已回退：{rollback_result}。")
        self._pause_strategy(strategy)
        text = (f"{self.monitor._display_name(strategy)} · {spread_words(spec)}：开仓两腿没有同时成交"
                f"（{fills}），已回退已成交部分（{rollback_result}）。策略已暂停，请查看后再继续。")
        await self.monitor._session_notice(strategy, text, kind="strategy-paused")

    async def _rollback_positions(
        self, wanted: list[tuple[str, str, int]],
    ) -> list[dict[str, Any]]:
        """The account positions the rollback's 今/昨 split is planned from, force-refreshed: fills
        can reach the position rows late, so until they cover the executor's own filled lots they
        are re-read every second, at most ``SPREAD_POSITION_WAIT_SECONDS``. What is still missing
        afterwards becomes an explicit leftover, never a silent short close."""
        provider = self.autonomy.data_provider
        deadline = monotonic() + SPREAD_POSITION_WAIT_SECONDS
        while True:
            try:
                async with provider_read_snapshot(provider, refresh=True):
                    positions = provider.positions()
            except RuntimeError:
                positions = []
            if all(_directional_volume(positions, symbol, direction) >= volume
                   for symbol, direction, volume in wanted):
                return positions
            if monotonic() >= deadline:
                return positions
            await asyncio.sleep(min(SPREAD_POLL_SECONDS, SPREAD_POSITION_WAIT_SECONDS))

    async def _strand_close(
        self, *, attempt_id: str, strategy: dict[str, Any], plan: Any, legs: list[_Leg],
    ) -> None:
        """平仓没平掉：cancel what is still working — no reverse open, no re-send — pause, notify
        the leftover lots in the user's words."""
        from .strategy_rules import SpreadSpec
        from .strategy_spread import spread_words
        user_id = strategy["userId"]
        spec = SpreadSpec.model_validate(plan.spread.model_dump())
        for leg in [leg for leg in legs
                    if leg.status in LEG_ALIVE_STATUSES and leg.client_order_id]:
            if await self._cancel_remainder(leg, user_id=user_id) == "unknown":
                return await self._settle_unknown(attempt_id=attempt_id, strategy=strategy,
                                                  plan=plan, legs=legs)
        leftovers = [f"{leg.tag}（{leg.symbol}）还剩 {leg.volume - _group_filled(legs, leg)} 手没平掉"
                     for leg in legs if leg.parent is None and _group_filled(legs, leg) < leg.volume]
        closed_any = any(_group_filled(legs, leg) > 0 for leg in legs if leg.parent is None)
        self.store.update_strategy_execution_attempt(
            attempt_id=attempt_id, status="PARTIAL" if closed_any else "CANCELLED",
            client_order_id=",".join(self._client_ids(legs)), outcome="leftover",
            detail=f"{self._attempt_detail(legs)}；{'；'.join(leftovers)}，请手动处理。")
        self._pause_strategy(strategy)
        text = (f"{self.monitor._display_name(strategy)} · {spread_words(spec)}："
                f"{'、'.join(leftovers)}（补单一次仍未成交），已撤销剩余委托，不会自动重发。策略已暂停，请手动处理。")
        await self.monitor._session_notice(strategy, text, kind="strategy-paused")

    # -- small helpers -----------------------------------------------------------------

    def _settle_leg(
        self, leg: _Leg, *, status: str, detail: str, client_order_id: str | None = None,
    ) -> None:
        leg.status, leg.detail = status, detail
        if client_order_id:
            leg.client_order_id = client_order_id
        self.store.update_strategy_execution_leg(
            leg_id=leg.leg_id, status=status, detail=detail, client_order_id=client_order_id)

    def _pause_strategy(self, strategy: dict[str, Any]) -> None:
        try:
            self.proposals.transition_strategy_status(
                strategy_id=strategy["strategyId"], user_id=strategy["userId"], target="paused")
        except Exception:
            LOGGER.debug("spread_pause_transition_skipped strategy=%s", strategy["strategyId"], exc_info=True)

    @staticmethod
    def _client_ids(legs: list[_Leg]) -> list[str]:
        ids: list[str] = []
        for leg in legs:
            if leg.client_order_id and leg.client_order_id not in ids:
                ids.append(leg.client_order_id)
        return ids

    @staticmethod
    def _attempt_detail(legs: list[_Leg]) -> str:
        return "；".join(f"{leg.tag} {leg.volume} 手：{leg.detail}".rstrip("。")
                        for leg in legs if leg.detail) or "没有产生模拟委托。"

    async def _republish_card(self, session_id: str, view: dict[str, Any]) -> None:
        """Event-only re-send of the confirmed card with the legs' real orders, so the conversation
        shows each leg's live state (the stored card is terminal and stays as it was confirmed)."""
        broker = getattr(self.autonomy, "broker", None)
        if broker is None:
            return
        try:
            await broker.publish(session_id, kind="approval", name="proposal.spread", payload=view)
        except Exception:
            LOGGER.warning("spread_card_republish_failed", exc_info=True)
