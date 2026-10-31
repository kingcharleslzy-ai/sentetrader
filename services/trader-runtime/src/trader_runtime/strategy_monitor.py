from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import time_ns
from typing import Any
from uuid import uuid4

from .autonomy import AutonomousSimulationService, AutonomyError
from .conversation_cards import execution_card, track_execution_card
from .data_providers import provider_read_snapshot
from .order_planner import CANDIDATE_ISSUE_LABELS, close_legs, instrument_display, prefs_for
from .proposals import ProposalConflict, ProposalService
from .simulation_orders import SimulationOrderError
from .spread_execution import SpreadExecutionService
from .store import RuntimeStore
from .strategy_protection import closed_results, require_current_activity, sync_protection

MAX_CONSECUTIVE_ACCOUNT_LOSSES = 3
MAX_CONSECUTIVE_MONITOR_FAILURES = 3
LOSS_STREAK_PAUSE_REASON = "策略连续三笔已完成平仓委托毛亏损，已暂停开新仓（不含手续费）。"
# 主动提醒 (2026-09): the actions worth announcing when they appear — a fresh entry or a close,
# not the risk trims (reduce_*) which follow position rules rather than the signal itself.
SIGNAL_ALERT_ACTIONS = frozenset({"open_long", "open_short", "close_long", "close_short"})
# 执行方式 (2026-09-25): what a monitored instance does with an executable signal.
SIGNAL_ACTIONS = ("alert", "confirm", "auto")
# The conversation notices that are also a lock-screen push: 只提醒's message, 出卡确认's cards and
# 自动下单's execution cards (pauses, warnings and schedule notices stay in the conversation).
PUSHED_NOTICE_KINDS = frozenset({"strategy-signal", "strategy-card", "strategy-execution"})
# The notices whose cards are the business itself (a confirmation card, an automatic order's execution
# card): they land in the conversation even with 策略 reminders off (#932) — only the reminder is gone.
ESSENTIAL_NOTICE_KINDS = frozenset({"strategy-card", "strategy-execution"})
SIGNAL_SIDES = {
    "open_long": "buy_open", "open_short": "sell_open",
    "close_long": "sell_close", "reduce_long": "sell_close",
    "close_short": "buy_close", "reduce_short": "buy_close",
}
CARD_WAITING_DETAIL = "已出确认卡，等待你确认；未确认的卡不计入每日开仓次数。"
LOGGER = logging.getLogger(__name__)


def _error_code(error: Exception) -> str:
    """The internal code of a card failure (ProposalConflict.code, else the exception's text)."""
    return str(getattr(error, "code", None) or error)


def trigger_words(trigger: dict[str, Any], action: str) -> str:
    """盘中触价 in words: which way the price has to go to reach the level."""
    rises = (action == "open_long") == (trigger.get("kind") == "stop")
    return "涨到" if rises else "跌到"


class StrategyWaiting(Exception):
    """Expected warm-up without a successful complete provider read."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# 等待行情 (2026-10-12): data that is not ready yet is not a strategy failure. The one table the
# monitor classifies a cycle's exception by — the source code the provider / evaluation kept
# (`.code`, or a provider RuntimeError's own code). A code here: status "waiting", no failure
# count, no automatic pause; anything else keeps the failure count and the pause after three.
MARKET_WAIT_REASONS = {
    "strategy_connection_initializing": "账户与行情正在连接",
    "market_series_not_ready": "所需周期 K 线正在载入",
    "market_quote_not_ready": "合约报价还没到",
    "market_subscription_limit": "行情订阅名额已满，等名额腾出",
    "strategy_bars_insufficient": "所需周期的已收盘 K 线还不够",
    "strategy_market_not_fresh": "行情暂未更新（开盘初期或刚重连）",
    "market_session_closed": "当前不在交易时段，等待开盘",
}
# A wait this long (outside a closed session) says so in the result's detail and `stalled`.
MARKET_WAIT_STALLED_SECONDS = 600


def market_wait_code(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    if not isinstance(code, str) and isinstance(error, RuntimeError):
        code = str(error)
    return code if code in MARKET_WAIT_REASONS else None


class _StopCloseContextUnavailable(Exception):
    """停止并平仓 (#675): the account context a spread's stop-close cards need (positions /
    trading day / a current activity read) could not be trusted — no card is issued at all."""


def _finite_decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _bar_gap(current: str, previous: str | None, timeframe_seconds: int) -> float | None:
    if previous is None:
        return None
    try:
        current_ns, previous_ns = int(current), int(previous)
        return (current_ns - previous_ns) / (timeframe_seconds * 1_000_000_000)
    except ValueError:
        try:
            current_time = datetime.fromisoformat(current.replace("Z", "+00:00"))
            previous_time = datetime.fromisoformat(previous.replace("Z", "+00:00"))
            return (current_time - previous_time).total_seconds() / timeframe_seconds
        except ValueError:
            return None


def _active_order_for_symbol(activity: dict[str, Any], symbol: str) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("symbol") or "").casefold() == symbol.casefold()
        and str(item.get("status") or "").upper() == "ALIVE"
        and int(item.get("volumeLeft") or 0) > 0
        for item in activity.get("orders", [])
    )


def _execution_status(display_status: str) -> tuple[str, str, bool]:
    mapping = {
        "已受理": ("SUBMITTED", "模拟委托已受理。", True),
        "已成交": ("SUBMITTED", "模拟委托已成交。", True),
        "部分成交": ("PARTIAL", "模拟委托部分成交。", True),
        "部分成交后已结束": ("PARTIAL", "模拟委托部分成交后已结束。", True),
        "结果未知": ("UNKNOWN", "结果未知，只回读原委托。", True),
        "身份异常": ("UNKNOWN", "订单身份异常，只回读原委托。", True),
        "待发送": ("NOT_DISPATCHED", "网关确认未发送。", False),
        "已放弃": ("NOT_DISPATCHED", "委托未发送并已放弃。", False),
        "已拒绝": ("REJECTED", "模拟委托已拒绝。", False),
        "已撤销": ("CANCELLED", "模拟委托已撤销。", False),
    }
    return mapping.get(display_status, ("UNKNOWN", "无法识别委托状态，只回读原委托。", True))


def link_confirmed_strategy_card(
    store: RuntimeStore, record: dict[str, Any], result: dict[str, Any], *, user_id: str,
) -> bool:
    """出卡确认: the user confirmed a strategy's card through the ordinary confirm route. The
    order it sent is carried back onto the attempt that issued the card, so the fill reads as the
    strategy's and a dispatched open now counts toward maxOrdersPerDay. A 停止并平仓 card links
    the same way onto its stop_close attempt (no cooldown bar — see confirm_strategy_card_order).
    Any other card: no-op."""
    source = (record.get("payload") or {}).get("strategySource")
    order = result.get("simulationOrder") if isinstance(result, dict) else None
    if not isinstance(source, dict) or not source.get("attemptId") or not isinstance(order, dict):
        return False
    client_order_id = str(order.get("clientOrderId") or "")
    if not client_order_id:
        return False
    status, detail, _ = _execution_status(str(order.get("displayStatus") or ""))
    stop_close = str(source.get("purpose") or "") == "stop_close"
    return store.confirm_strategy_card_order(
        attempt_id=str(source["attemptId"]), user_id=user_id, proposal_id=str(record["id"]),
        client_order_id=client_order_id, status=status,
        detail=f"{'停止平仓卡' if stop_close else '确认卡'}已确认：{detail}",
    )


class StrategyMonitorService:
    """Poll validated strategies and create at most one execution attempt per completed bar."""

    def __init__(
        self, *, store: RuntimeStore, proposals: ProposalService,
        autonomy: AutonomousSimulationService, poll_interval_seconds: float = 1.0,
        trading_account_id: str | None = None,
    ) -> None:
        self.store = store
        self.account_id = trading_account_id or store.default_trading_account_id
        self.proposals = proposals
        self.autonomy = autonomy
        # 每账户一个监控：把它挂回本账户的 autonomy (2026-10-10 #692)，HTTP 与内置工具的启动/
        # 状态共用入口从这里取到本账户的监控服务，不再各写一套迁移。
        autonomy.strategy_monitor = self
        self.poll_interval_seconds = max(0.25, poll_interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self.period_lock = asyncio.Lock()
        self._stopping = False
        self._sessions: dict[str, str] = {}
        self.latest_results: dict[str, dict[str, Any]] = {}
        # Pushes one strategy-signal alert to the owner's sessions; main replaces the default with
        # the version that also speaks on an active voice call (on-demand pool accounts assembled
        # after boot keep the default — their spoken side is the voice poller's own decision line).
        self.signal_alert_sink = self._default_signal_alert_sink
        # 等待行情: when each strategy's current uninterrupted wait began, and whether its stall was
        # logged (per process; a restart starts the clock again).
        self._waiting_since: dict[str, tuple[datetime, bool]] = {}
        # 执行方式 (2026-09-25): main points this at the voice service so a reminder, a card or an
        # automatic pause is also spoken while the owner is on a call (None = no voice here).
        self.voice_lookup: Callable[[], Any] | None = None
        # alert / confirm offer the same signal once — one reminder, one card — not again on every
        # following bar, and again once the signal changed. That record lives in the database
        # (the reminder / card attempts and the state's signal_reset_at), so a restart or a
        # blue-green switch never repeats it.
        # An automatic pause and the missing-schedule warning are once per strategy and day by their
        # reminder claims (#932), not a per-process memory.
        # 价差第二期: both legs of one spread action, dispatched together and followed to a
        # definite outcome in the background (this loop never waits on a fill).
        self.spread_execution = SpreadExecutionService(self)
        self._spread_recovered = False

    @property
    def enabled(self) -> bool:
        return self.autonomy.enabled

    async def start(self) -> None:
        if not self.enabled or (self._task is not None and not self._task.done()):
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="ai-trader-strategy-monitor")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("strategy_monitor_loop_failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                continue

    async def run_once(self) -> list[dict[str, Any]]:
        if not self._spread_recovered:
            # One sweep on the first cycle after a (re)start: a spread attempt that was mid-flight
            # is UNKNOWN — its legs' true state cannot be rebuilt — the strategy pauses for the user.
            self._spread_recovered = True
            await self.spread_execution.recover_interrupted()
        results = []
        monitoring = self.store.list_monitoring_strategies(trading_account_id=self.account_id)
        # 等待行情: a paused / stopped strategy's wait ends with its monitoring — a later resume
        # starts a fresh clock instead of reporting the pause as a stalled wait.
        for stale in self._waiting_since.keys() - {strategy["strategyId"] for strategy in monitoring}:
            del self._waiting_since[stale]
        for strategy in monitoring:
            strategy_id, user_id = strategy["strategyId"], strategy["userId"]
            existing = self.store.strategy_monitor_state(
                strategy_id=strategy_id, user_id=user_id
            )
            if existing and existing.get("nextRetryAt"):
                try:
                    if datetime.fromisoformat(str(existing["nextRetryAt"])) > datetime.now(UTC):
                        results.append({
                            "strategyId": strategy_id,
                            "status": "retrying",
                            "detail": existing.get("lastError") or "监控暂时重试中",
                        })
                        continue
                except ValueError:
                    pass
            try:
                result = await self._run_strategy(strategy)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                wait_code = market_wait_code(error)
                if wait_code is not None:
                    # 等待行情: neither counts as a failure nor clears an earlier one.
                    results.append(self._market_wait(strategy, wait_code))
                    continue
                self._waiting_since.pop(strategy_id, None)
                # 异常归类的唯一责任入口 (#698 复审)：同一轮清算 / 停机中的任何失败——不只是
                # 某张异常名单——都不是普通监控错误。失败计数三次会自动暂停，清单只扫
                # monitoring，清算中的一轮被暂停即永久搁置。这里按持久状态行判同轮（不依赖
                # 已成功构造的 plan）：是清算 / 停机的一轮就如实报告当前故障并保持轮次、
                # 保护与 monitoring，不计数、不暂停、不重派；其余策略照旧走失败计数。
                try:
                    persisted = self._persisted_drawdown_round(strategy=strategy)
                except Exception:  # noqa: BLE001 — 状态行本身读不出来时只能按普通失败处理
                    persisted = None
                if persisted is not None:
                    liquidating = str(persisted["status"]) == "liquidating"
                    fault = f"{type(error).__name__}: {error}"
                    LOGGER.info("drawdown_round_monitor_failed strategy=%s user=%s status=%s fault=%s",
                                strategy_id, user_id, persisted["status"], fault)
                    detail = ((persisted["reason"] or "回撤清算进行中。") if liquidating
                              else "本策略回撤停机中，暂不开新仓。")
                    detail += (f"本轮监控暂时失败（{fault}），"
                               + ("保持清算状态，下一轮自动重试。" if liquidating
                                  else "保持停机锁定，下一轮自动重试。"))
                    result = {"strategyId": strategy_id, "status": "blocked",
                              "action": "close" if liquidating else "hold", "detail": detail}
                    results.append(result)
                    continue
                state = self.store.record_strategy_monitor_error(
                    strategy_id=strategy_id,
                    user_id=user_id,
                    detail=f"{type(error).__name__}: {error}",
                    retry_after_seconds=max(1, int(self.poll_interval_seconds * 5)),
                    trading_account_id=self.account_id,
                )
                if state["consecutiveFailures"] >= MAX_CONSECUTIVE_MONITOR_FAILURES:
                    pause_reason = (state["pauseReason"] if state["pauseReason"] == LOSS_STREAK_PAUSE_REASON
                                    else "策略监控连续失败三次，已自动暂停。")
                    self.proposals.transition_strategy_status(
                        strategy_id=strategy_id, user_id=user_id, target="paused"
                    )
                    self.store.save_strategy_monitor_state(
                        strategy_id=strategy_id,
                        user_id=user_id,
                        trading_date=state["tradingDate"],
                        high_watermark_equity=state["highWatermarkEquity"],
                        last_today_pnl=state["lastTodayPnl"],
                        consecutive_losses=state["consecutiveLosses"],
                        last_trade_id=state["lastTradeId"],
                        last_executed_bar=state["lastExecutedBar"],
                        pause_reason=pause_reason,
                        trading_account_id=self.account_id,
                    )
                    result = {"strategyId": strategy_id, "status": "paused", "detail": pause_reason}
                    await self._pause_notice(strategy, pause_reason, state["tradingDate"])
                else:
                    result = {
                        "strategyId": strategy_id,
                        "status": "error",
                        "detail": state["lastError"],
                    }
            else:
                self._waiting_since.pop(strategy_id, None)
                self.store.clear_strategy_monitor_error(
                    strategy_id=strategy_id, user_id=user_id
                )
            results.append(result)
        # `since` / `checks`: how long this run has been monitoring and how many judgements it has
        # made, for the 对话面板's running-strategy banner. Both restart with the run, because
        # `monitor/start` drops the strategy's entry from `latest_results`.
        now = datetime.now(UTC).isoformat()
        for item in results:
            previous = self.latest_results.get(item["strategyId"]) or {}
            self.latest_results[item["strategyId"]] = {
                **item, "checkedAt": now,
                "since": previous.get("since") or now,
                "checks": int(previous.get("checks") or 0) + 1,
            }
        return results

    def _market_wait(self, strategy: dict[str, Any], code: str) -> dict[str, Any]:
        """One 等待行情 result: the reason in words, since when, and `stalled` once it has lasted
        MARKET_WAIT_STALLED_SECONDS outside a closed session (logged once per wait)."""
        strategy_id = strategy["strategyId"]
        if code == "strategy_market_not_fresh":
            try:
                market = self.autonomy.data_provider.market_snapshot(
                    f"strategy-monitor:{uuid4()}", strategy["symbol"])
            except Exception:  # noqa: BLE001 — the session is unknown: still a wait for fresh quotes
                market = None
            if isinstance(market, dict) and market.get("marketSessionStatus") == "closed":
                code = "market_session_closed"
        now = datetime.now(UTC)
        since, logged = self._waiting_since.get(strategy_id) or (now, False)
        if code == "market_session_closed":
            since, logged = now, False  # a closed session is the expected wait; the clock starts at the open
        elif strategy_id not in self._waiting_since:
            LOGGER.info("strategy_market_wait strategy=%s code=%s", strategy_id, code)
        waited = (now - since).total_seconds()
        stalled = waited >= MARKET_WAIT_STALLED_SECONDS
        if stalled and not logged:
            LOGGER.warning("strategy_market_wait_stalled strategy=%s code=%s seconds=%d",
                           strategy_id, code, int(waited))
            logged = True
        self._waiting_since[strategy_id] = (since, logged)
        if code == "market_session_closed":
            detail = "market_session_closed"
        elif stalled:
            detail = (f"等待行情已 {int(waited // 60)} 分钟：{MARKET_WAIT_REASONS[code]}。"
                      "请检查行情连接或合约是否在交易。")
        else:
            detail = f"等待行情：{MARKET_WAIT_REASONS[code]}。"
        return {"strategyId": strategy_id, "status": "waiting", "waitReason": code,
                "waitingSince": since.isoformat(), "stalled": stalled, "detail": detail}

    def _state(
        self, *, strategy: dict[str, Any], account: dict[str, Any],
        activity: dict[str, Any],
    ) -> tuple[dict[str, Any], Decimal | None, Decimal | None]:
        today = str(account.get("tradingDay") or "")
        try:
            datetime.strptime(today, "%Y-%m-%d")
        except ValueError as error:
            raise RuntimeError("trading_day_unavailable") from error
        existing = self.store.strategy_monitor_state(
            strategy_id=strategy["strategyId"], user_id=strategy["userId"]
        )
        equity = _finite_decimal(account.get("equity"))
        pnl = _finite_decimal(account.get("todayNetPnl", account.get("todayPnl")))
        results = closed_results(self.store, strategy["userId"], strategy["strategyId"], today)
        pause_reason = None
        if existing is None or existing["tradingDate"] != today:
            high = equity
            last_bar = None
        else:
            high = _finite_decimal(existing["highWatermarkEquity"])
            last_bar = existing["lastExecutedBar"]
            if equity is not None and (high is None or equity > high):
                high = equity
            # Preserve an already triggered daily entry pause separately from
            # the actual streak, which late confirmations can legitimately fix.
            # Only migrate the fill-backed counter, never the old equity heuristic.
            fill_backed_pause = (str(existing["lastTradeId"] or "").startswith("close:")
                                 and int(existing["consecutiveLosses"]) >= MAX_CONSECUTIVE_ACCOUNT_LOSSES)
            if existing["pauseReason"] == LOSS_STREAK_PAUSE_REASON or fill_backed_pause:
                pause_reason = LOSS_STREAK_PAUSE_REASON
        # Recompute the day's bounded result sequence. An incremental ID cursor
        # would skip a late completed order inserted before that cursor.
        previous_trade_id, losses = None, 0
        for order_id, realized in results:
            losses = losses + 1 if realized < 0 else 0
            previous_trade_id = order_id
            if losses >= MAX_CONSECUTIVE_ACCOUNT_LOSSES:
                pause_reason = LOSS_STREAK_PAUSE_REASON
        state = self.store.save_strategy_monitor_state(
            strategy_id=strategy["strategyId"], user_id=strategy["userId"],
            trading_date=today,
            high_watermark_equity=format(high, "f") if high is not None else None,
            last_today_pnl=format(pnl, "f") if pnl is not None else None,
            consecutive_losses=losses,
            last_trade_id=previous_trade_id,
            last_executed_bar=last_bar,
            pause_reason=pause_reason,
            trading_account_id=self.account_id,
        )
        return state, equity, high

    async def _run_strategy(self, strategy: dict[str, Any]) -> dict[str, Any]:
        async with self.period_lock:
            # A chart switch and its full decision/submit cycle cannot interleave.
            current = self.store.get_strategy_draft(strategy["strategyId"], user_id=strategy["userId"])
            if current["status"] != "monitoring":
                return {"strategyId": current["strategyId"], "status": current["status"], "detail": "策略已停止监控"}
            if not self.store.is_execution_owner(account_id=self.account_id, user_id=current["userId"]):
                # 4.3 执行归属: the strategy belongs to a user who is not this account's
                # execution owner. Like the autonomy switch, it keeps monitoring and keeps
                # its bar; only the owner's strategies submit.
                LOGGER.info("strategy_execution_skipped_not_execution_owner strategy=%s user=%s action=monitor",
                            current["strategyId"], current["userId"])
                return {"strategyId": current["strategyId"], "status": "blocked",
                        "detail": "not_execution_owner：这个账户的 AI 自主执行由执行负责人管理，策略只监控不下单。"}
            return await self._run_strategy_bound(current)

    async def _run_strategy_bound(self, strategy: dict[str, Any]) -> dict[str, Any]:
        from .strategy_rules import StrategyPlan, plan_timeframes
        definition = strategy["definition"]
        plan = (StrategyPlan.model_validate(definition["plan"])
                if definition.get("schemaVersion") == 2 else None)
        timeframes = plan_timeframes(plan) if plan is not None else [definition["timeframeSeconds"]]
        # 价差: both legs' workbenches are read (A drives the schedule; B joins the spread series).
        symbols = ([leg.symbol for leg in plan.spread.legs] if plan is not None and plan.spread is not None
                   else [strategy["symbol"]])
        async with provider_read_snapshot(self.autonomy.data_provider,
                                          symbols=symbols, timeframes=timeframes):
            return await self._run_strategy_snapshot(strategy, plan=plan)

    async def _run_strategy_snapshot(
        self, strategy: dict[str, Any], *, plan: Any = None,
    ) -> dict[str, Any]:
        strategy_id, user_id = strategy["strategyId"], strategy["userId"]
        turn_id = f"strategy-monitor:{uuid4()}"
        status = self.autonomy.data_provider.runtime_status()
        if status.get("marketDataStatus") == "connecting" or status.get("accountStatus") == "connecting":
            raise StrategyWaiting("strategy_connection_initializing")
        persisted = self._persisted_drawdown_round(strategy=strategy)
        if persisted is not None:
            # 持久停机 / 清算先于一切普通评估读取 (#692 复审)：这之前的 sync_protection 与
            # account/positions/market 读取任何一步失败或陈旧，都会把停机 / 清算中的一轮落进
            # 普通错误计数——三次自动暂停后清单不再扫描它，清算被永久搁置。这里只读数据库即可
            # 判定；清算推进（撤单 / 查询 / 平仓派发）继续走各自的新鲜度、归属与授权围栏，
            # 事实恢复后自动接着清算，永不自动恢复新开仓。
            if str(persisted["status"]) == "stopped":
                return {"strategyId": strategy_id, "status": "blocked", "action": "hold",
                        "detail": str(persisted["reason"] or "本策略回撤停机中，暂不开新仓。")}
            return await self._drawdown_liquidating(
                strategy=strategy, state=persisted, limit=float(persisted["limitPercent"]))
        sync_protection(self.store, self.autonomy.data_provider,
                        user_id=user_id, symbol_filter=strategy["symbol"],
                        trading_account_id=self.account_id)
        account: dict[str, Any] | None = None
        activity: dict[str, Any] | None = None
        positions: list[dict[str, Any]] | None = None
        state: dict[str, Any] | None = None
        equity: Decimal | None = None
        high: Decimal | None = None
        if plan is not None and plan.spread is not None:
            # 每秒价差止损/止盈 runs BEFORE the bar evaluation — it needs only the held spread, live
            # leg orders and both legs' latest quotes, so an evaluation that raises (bars
            # insufficient, a stale period) or returns on pending data must not silence the
            # protective exit for that second.
            account = self.autonomy.data_provider.account_summary(turn_id)
            activity = self.autonomy.data_provider.account_activity()
            positions = self.autonomy.data_provider.positions()
            state, equity, high = self._state(
                strategy=strategy, account=account, activity=activity
            )
            exit_result = await self._spread_exit_check(
                strategy=strategy, plan=plan, positions=positions, activity=activity,
                state=state)
            if exit_result is not None:
                return exit_result
        if plan is not None and plan.maxDrawdownPercent is not None:
            # 回撤停机 (2026-10-09): judged on every poll's fresh snapshot, BEFORE the bar
            # evaluation — an instance whose K线 is still warming up still gets its round's
            # net value checked (and a liquidating round keeps making progress).
            drawdown_block = await self._drawdown_evaluate(
                strategy=strategy, plan=plan,
                account=account if account is not None else self.autonomy.data_provider.account_summary(turn_id),
                positions=positions if positions is not None else self.autonomy.data_provider.positions(),
                market=self.autonomy.data_provider.market_snapshot(turn_id, strategy["symbol"]))
            if drawdown_block is not None:
                return drawdown_block
        # Data not ready yet (K 线载入 / 订阅满额 / 开盘初期未新鲜 …) raises with its source code;
        # run_once classifies it once (MARKET_WAIT_REASONS) as 等待行情.
        intent = self.proposals.preview_strategy_intent(
            strategy_id=strategy_id, user_id=user_id, turn_id=turn_id,
            own_position=self._own_position(strategy, plan=plan),
        )
        account = account if account is not None else self.autonomy.data_provider.account_summary(turn_id)
        activity = activity if activity is not None else self.autonomy.data_provider.account_activity()
        positions = positions if positions is not None else self.autonomy.data_provider.positions()
        # 主动提醒: the position intent changed (观望 → 开/平, or a reversal) — one event per
        # (action, completed bar). Re-firing on the same bar and an intra-bar flip-back stay quiet;
        # every early return below (waiting / blocked / paused) still sees the alert first, because
        # the reminder is about the signal, not about the execution. The notify preference is read
        # in the sink, so monitoring itself is identical with alerts off.
        signal_action = str(intent["positionIntent"]["action"])
        signal_bar = str(intent["signalSnapshot"]["barTime"])
        if intent["signalSnapshot"].get("dataPending") and signal_action == "hold":
            # 数据未就绪 (当日最高 / 最低 still reading the day's earlier bars): this judgement is not a
            # signal — it neither consumes the bar nor resets the offered signal; the next poll judges it.
            return {"strategyId": strategy_id, "status": "waiting", "detail": str(intent["signalSnapshot"]["dataPending"])}
        bar_exit = (plan is not None and plan.spread is not None
                    and str(intent["signalSnapshot"].get("exitKey") or "").split(":", 1)[0]
                    in {"spread_stop", "spread_take"})
        # 价差止损/止盈 (第二期) is judged every second off the legs' latest quotes, not on the
        # completed bar: the bar-close judgement here is only a slower duplicate of it, so it is
        # not announced, not offered and not consumed — the per-second check above already ran on
        # this very cycle (the exit must not wait for the bar to agree).
        if not bar_exit:
            # The signal moved away from the last reminder / card: the next one is offered again.
            self.store.note_strategy_signal(strategy_id=strategy_id, user_id=user_id,
                                            action=str(intent["riskAdjustedIntent"]["action"]))
            # The last judged / announced (action, bar) live in the monitor state row, written before
            # the announcement: a restart or a blue-green switch never announces the same bar twice.
            current_signal = (signal_action, signal_bar)
            previous_signal, alerted = self.store.strategy_signal_marks(strategy_id=strategy_id, user_id=user_id)
            announce = (signal_action in SIGNAL_ALERT_ACTIONS
                        and (previous_signal is None or previous_signal[0] != signal_action)
                        and alerted != current_signal)
            if announce or previous_signal != current_signal:
                # A conditional write: only the monitor that moved the marks announces (a second
                # process judging the same bar finds them already changed and stays quiet).
                claimed = self.store.claim_strategy_signal_marks(
                    strategy_id=strategy_id, user_id=user_id, expected=(previous_signal, alerted),
                    seen=current_signal, alerted=current_signal if announce else alerted,
                    trading_account_id=self.account_id)
                announce = announce and claimed
            if announce:
                await self._announce_signal_change(
                    strategy=strategy, intent=intent, action=signal_action, turn_id=turn_id
                )
        if state is None:
            state, equity, high = self._state(
                strategy=strategy, account=account, activity=activity
            )
        if not bar_exit:
            await self._schedule_notice(strategy, intent, positions, state["tradingDate"])
        if plan is not None and plan.spread is not None and bar_exit:
            # 每秒价差止损/止盈 already ran above, before the evaluation: the bar-close judgement
            # is only its slower duplicate, so it is not announced, offered or consumed.
            return {"strategyId": strategy_id, "status": "waiting",
                    "detail": "价差止损/止盈由每秒判断处理。"}
        policy = self.autonomy.simulation_orders.policies.get(
            user_id=user_id, symbol=strategy["symbol"]
        )
        drawdown = (high - equity) if high is not None and equity is not None else None
        pause_reason = None
        if state["pauseReason"] == LOSS_STREAK_PAUSE_REASON:
            pause_reason = LOSS_STREAK_PAUSE_REASON
        elif policy.max_daily_loss is not None and drawdown is not None and drawdown >= policy.max_daily_loss:
            pause_reason = "账户从日内权益高点回撤已达到风险预算，策略已暂停。"
        if pause_reason is not None and strategy["definition"].get("schemaVersion") == 2:
            # Pausing entries must not disable rule-based exits or protection.
            if intent["riskAdjustedIntent"]["action"] in {"open_long", "open_short"}:
                await self._pause_notice(strategy, pause_reason, state["tradingDate"], entries_only=True)
                return {"strategyId": strategy_id, "status": "blocked", "detail": pause_reason}
            pause_reason = None
        if pause_reason is not None:
            self.proposals.transition_strategy_status(
                strategy_id=strategy_id, user_id=user_id, target="paused"
            )
            self.store.save_strategy_monitor_state(
                strategy_id=strategy_id, user_id=user_id,
                trading_date=state["tradingDate"],
                high_watermark_equity=state["highWatermarkEquity"],
                last_today_pnl=state["lastTodayPnl"],
                consecutive_losses=state["consecutiveLosses"],
                last_trade_id=state["lastTradeId"],
                last_executed_bar=state["lastExecutedBar"],
                pause_reason=pause_reason,
                trading_account_id=self.account_id,
            )
            await self._pause_notice(strategy, pause_reason, state["tradingDate"])
            return {"strategyId": strategy_id, "status": "paused", "detail": pause_reason}
        adjusted = intent["riskAdjustedIntent"]
        action = str(adjusted["action"])
        volume = int(adjusted["volume"])
        if action in {"open_long", "open_short"}:
            if getattr(self.autonomy.data_provider, "entries_paused", False):
                return {"strategyId": strategy_id, "status": "waiting", "detail": "已暂停新开仓，继续监控平仓条件。"}
            maximum_loss = strategy["definition"]["riskLimits"].get("maxLoss")
            net = _finite_decimal(account.get("todayNetPnl", account.get("todayPnl")))
            if maximum_loss is not None and (net is None or net <= -Decimal(str(maximum_loss))):
                if net is not None:
                    await self._pause_notice(strategy, "已达到本次亏损上限", state["tradingDate"], entries_only=True)
                return {"strategyId": strategy_id, "status": "blocked", "action": action, "detail": "已达到本次亏损上限，继续监控平仓。"}
            if plan is not None and plan.maxDrawdownPercent is not None:
                # 回撤停机: an unattributable round (or a stopped one still polling) opens nothing.
                dd = self.store.strategy_drawdown_state(strategy_id=strategy_id, user_id=user_id)
                if dd is not None and str(dd["status"]) in {"stopped", "unavailable"}:
                    detail = str(dd["reason"] or "本策略回撤停机中，暂不开新仓。")
                    return {"strategyId": strategy_id, "status": "blocked", "action": action, "detail": detail}
        bar_time = str(intent["signalSnapshot"]["barTime"])
        if (action == "hold" and str(intent["positionIntent"]["action"]) == "hold"
                and intent["signalSnapshot"].get("exitKey")):
            # 分批止盈's one-lot skip: the tier is consumed without an order. A SKIPPED attempt
            # records the exit key (the position reads it as fired across restarts), exactly like
            # an acted-on close would; without it the position could keep judging this tier — and
            # 加仓 — every bar.
            skip_id = str(uuid4())
            if self.store.reserve_strategy_execution_attempt(
                attempt_id=skip_id, strategy_id=strategy_id, user_id=user_id,
                intent_id=intent["intentId"], signal_bar_time=bar_time, trading_day=state["tradingDate"],
                action=action, volume=0, trading_account_id=self.account_id, signal_action="alert",
            ):
                self.store.finish_strategy_execution_attempt(
                    attempt_id=skip_id, status="SKIPPED", detail="分批止盈这一档只剩 1 手，不再平仓")
        if adjusted["status"] not in {"allowed", "reduce_only"} or action == "hold" or volume <= 0:
            return {"strategyId": strategy_id, "status": "waiting", "detail": adjusted["reason"]}
        if any(_active_order_for_symbol(activity, symbol) for symbol in (
                [leg.symbol for leg in plan.spread.legs] if plan is not None and plan.spread is not None
                else [strategy["symbol"]])):
            return {"strategyId": strategy_id, "status": "blocked", "action": action, "detail": "同合约仍有活动委托。"}
        alignment_id = intent["signalSnapshot"].get("alignmentId")
        if action in {"open_long", "open_short"} and alignment_id and self.store.strategy_alignment_used(
                strategy_id=strategy_id, user_id=user_id, alignment_id=alignment_id, exclude_bar_time=bar_time):
            return {"strategyId": strategy_id, "status": "waiting", "detail": "本段方向已尝试开仓，等待方向被打断后重新确认。"}
        max_orders = int(strategy["definition"]["riskLimits"]["maxOrdersPerDay"])
        if action in {"open_long", "open_short"} and self.store.count_strategy_attempts_today(
            strategy_id=strategy_id, user_id=user_id, trading_date=state["tradingDate"]
        ) >= max_orders:
            return {"strategyId": strategy_id, "status": "blocked", "action": action, "detail": "已达到策略当日委托上限。"}
        is_add = bool(intent["signalSnapshot"].get("add"))
        cooldown = int(strategy["definition"]["riskLimits"]["cooldownBars"])
        gap = _bar_gap(
            bar_time, state["lastExecutedBar"],
            int(strategy["definition"]["timeframeSeconds"]),
        )
        if (gap is not None and gap <= cooldown and not is_add
                and (strategy["definition"].get("schemaVersion") != 2 or action in {"open_long", "open_short"})):
            return {"strategyId": strategy_id, "status": "blocked", "action": action, "detail": "策略仍在冷却周期。"}
        if action in {"open_long", "open_short"} and self.autonomy.controls(user_id=user_id)["stopStatus"] == "HALT_OPENS":
            # 只停新开仓 (scope=opens): like the AI-autonomy switch, the strategy keeps
            # monitoring and keeps its bar; only new opens wait for the resume.
            LOGGER.info("strategy_open_skipped_new_opens_halted strategy=%s user=%s bar=%s action=%s",
                        strategy_id, user_id, bar_time, action)
            return {"strategyId": strategy_id, "status": "blocked", "action": action,
                    "detail": "已只停新开仓，策略只监控不开仓；恢复模拟执行后继续。"}
        trigger = intent["signalSnapshot"].get("entryTrigger")
        if action in {"open_long", "open_short"} and trigger and not trigger.get("reached"):
            # 盘中触价: armed at the signal bar's close; nothing is offered or sent until the latest price
            # reaches the level inside the next bar (the next bar's close judges again).
            other = trigger.get("other")
            both = (f"；另一边挂 {other['price']}：价格{trigger_words(other, other['action'])}就反向开仓，先到先成交"
                    if other else "")
            return {"strategyId": strategy_id, "status": "waiting", "action": action,
                    "detail": f"条件已满足，挂触价 {trigger['price']}：价格{trigger_words(trigger, action)}才开仓{both}，"
                              "只在这根K线内有效。"}
        # 执行方式 (2026-09-25): every check above applies to all three; only here they part.
        # alert 只提醒 (a message, no card, no order), confirm 出卡确认 (a confirmation card per
        # leg), auto 自动下单 (the path below, unchanged). auto with 设置 → 交易 → AI 自主执行 off
        # is handled as confirm: the signal still reaches the user, as a card they confirm.
        mode = self.signal_mode(strategy)
        if mode != strategy.get("signalAction", "auto"):
            LOGGER.info("strategy_auto_as_confirm_ai_autonomy_disabled strategy=%s user=%s bar=%s action=%s",
                        strategy_id, user_id, bar_time, action)
        if mode != "auto":
            if self.store.signal_already_offered(strategy_id=strategy_id, user_id=user_id, action=action):
                return {"strategyId": strategy_id, "status": "waiting", "action": action,
                        "detail": "这个信号已提醒过，等待信号变化。" if mode == "alert"
                        else "这个信号已出确认卡，等待确认或信号变化。"}
            if mode == "alert":
                return await self._alert_signal(strategy=strategy, intent=intent, action=action, volume=volume,
                                                bar_time=bar_time, trading_day=state["tradingDate"], turn_id=turn_id)
            if plan is not None and plan.spread is not None:
                return await self._propose_spread_signal(
                    strategy=strategy, plan=plan, intent=intent, action=action, volume=volume,
                    bar_time=bar_time, trading_day=state["tradingDate"], positions=positions, turn_id=turn_id)
            return await self._propose_signal(strategy=strategy, intent=intent, action=action,
                                              volume=volume, bar_time=bar_time, trading_day=state["tradingDate"],
                                              positions=positions, turn_id=turn_id)
        if plan is not None and plan.spread is not None:
            # 自动下单 (价差): both legs dispatch together and are followed in the background.
            return await self.spread_execution.execute_auto(
                strategy=strategy, plan=plan, intent=intent, action=action, volume=volume,
                bar_time=bar_time, trading_day=state["tradingDate"])
        attempt_id = str(uuid4())
        reserved = self.store.reserve_strategy_execution_attempt(
            attempt_id=attempt_id, strategy_id=strategy_id, user_id=user_id,
            intent_id=intent["intentId"], signal_bar_time=bar_time,
            trading_day=state["tradingDate"],
            action=action, volume=volume,
            trading_account_id=self.account_id,
        )
        side = {
            "open_long": "buy_open", "open_short": "sell_open",
            "close_long": "sell_close", "reduce_long": "sell_close",
            "close_short": "buy_close", "reduce_short": "buy_close",
        }.get(action)
        if side is None:
            if not reserved:
                return {"strategyId": strategy_id, "status": "duplicate", "detail": "该已完成K线已处理。"}
            self.store.finish_strategy_execution_attempt(
                attempt_id=attempt_id, status="SKIPPED", detail="没有可执行动作"
            )
            return {"strategyId": strategy_id, "status": "waiting", "detail": "没有可执行动作。"}
        plans: list[tuple[str | None, int]]
        if side.endswith("_close"):
            direction = "long" if side == "sell_close" else "short"
            plans = list(close_legs(positions, symbol=strategy["symbol"], direction=direction, volume=volume,
                                    preference=prefs_for(self.store, user_id).close_preference))
        else:
            plans = [(None, volume)]
        retry_legs: list[dict[str, Any]] = []
        if not reserved:
            retry = self.store.retry_strategy_execution_attempt(
                strategy_id=strategy_id, user_id=user_id, signal_bar_time=bar_time,
                trading_day=state["tradingDate"], action=action, volume=volume,
                strategy_digest=strategy["definitionDigest"], plans=plans,
            )
            if retry is None:
                return {"strategyId": strategy_id, "status": "duplicate", "detail": "该已完成K线已处理。"}
            attempt_id, retry_legs = retry
        if not plans:
            self.store.finish_strategy_execution_attempt(
                attempt_id=attempt_id, status="BLOCKED", detail="没有足够可平持仓"
            )
            return {"strategyId": strategy_id, "status": "blocked", "action": action, "detail": "没有足够可平持仓。"}
        client_ids: list[str] = []
        leg_statuses: list[str] = []
        details: list[str] = []
        sent_orders: list[dict[str, Any]] = []  # each leg's read-back, for the execution card
        current_client_id: str | None = None
        try:
            for leg_index, (bucket, plan_volume) in enumerate(plans):
                current_client_id = None

                def record_identity(proposal_id: str, client_order_id: str) -> None:
                    nonlocal current_client_id
                    self.store.bind_strategy_execution_leg(leg_id=leg_id, client_order_id=client_order_id)
                    current_client_id = client_order_id
                    if client_order_id not in client_ids:
                        client_ids.append(client_order_id)

                if leg_index < len(retry_legs):
                    leg_id = str(retry_legs[leg_index]["id"])
                    current_client_id = str(retry_legs[leg_index]["client_order_id"])
                    client_ids.append(current_client_id)
                    result = await self.autonomy.retry_trade(user_id=user_id, client_order_id=current_client_id)
                else:
                    leg_id = str(uuid4())
                    self.store.reserve_strategy_execution_leg(
                        leg_id=leg_id, attempt_id=attempt_id, leg_index=leg_index,
                        position_bucket=bucket, volume=plan_volume,
                    )
                    # Structural-stop intents use the last price. A passive
                    # quote can land on that stop despite a valid preview.
                    from .strategy_rules import requires_entry_recheck, uses_structural_stop
                    structural_entry = (side.endswith("_open") and uses_structural_stop(strategy["definition"])
                                        and not is_add)
                    result = await self.autonomy.execute_trade(
                        user_id=user_id, session_id=self._session_id(user_id),
                        symbol=strategy["symbol"], side=side, volume=plan_volume,
                        # 盘中触价: a reached stop takes the opposite side's price (it chases a move, like a
                        # protective stop), a reached limit the last price.
                        price_intent=("aggressive" if trigger and side.endswith("_open") and trigger.get("kind") == "stop"
                                      else "last" if structural_entry or (trigger and side.endswith("_open"))
                                      else "passive"), position_bucket=bucket,
                        reason=f"策略 {strategy['name']} · 已完成K线 {bar_time}",
                        before_dispatch=record_identity,
                        strategy_id=strategy_id, ai_initiated=True,
                        # 本笔的预留不再和发送前预算复核重复计入（其他预留照常占用）。
                        strategy_attempt_id=attempt_id,
                        strategy_levels=intent["signalSnapshot"].get("protectionLevels"),
                        # 加仓 skips the sending-time structural / alignment recheck: it joins the
                        # held position instead of opening a fresh one off the entry structure.
                        strategy_add=is_add,
                        **({"strategy_bar_time": bar_time}
                           if requires_entry_recheck(strategy["definition"]) and not is_add else {}),
                    )
                client_order_id = str(result["clientOrderId"])
                if client_order_id not in client_ids:
                    client_ids.append(client_order_id)
                if isinstance(result.get("simulationOrder"), dict):
                    sent_orders.append({"clientOrderId": client_order_id, **result["simulationOrder"]})
                leg_status, leg_detail, _ = _execution_status(
                    str(result["simulationOrder"].get("displayStatus") or "")
                )
                leg_statuses.append(leg_status)
                details.append(f"第 {leg_index + 1} 腿：{leg_detail}")
                self.store.finish_strategy_execution_leg(
                    leg_id=leg_id,
                    status=leg_status,
                    client_order_id=client_order_id,
                    detail=leg_detail,
                )
                if leg_status in {"UNKNOWN", "NOT_DISPATCHED", "REJECTED", "CANCELLED"}:
                    break
            if "UNKNOWN" in leg_statuses:
                status = "UNKNOWN"
            elif any(item in {"NOT_DISPATCHED", "REJECTED", "CANCELLED", "FAILED"} for item in leg_statuses):
                terminal = next(
                    item for item in reversed(leg_statuses)
                    if item in {"NOT_DISPATCHED", "REJECTED", "CANCELLED", "FAILED"}
                )
                status = "PARTIAL" if any(
                    item in {"SUBMITTED", "PARTIAL"} for item in leg_statuses[:-1]
                ) else terminal
            elif "PARTIAL" in leg_statuses:
                status = "PARTIAL"
            else:
                status = "SUBMITTED"
            self.store.finish_strategy_execution_attempt(
                attempt_id=attempt_id,
                status=status,
                client_order_id=",".join(client_ids),
                detail=" ".join(details) or "没有产生模拟委托。",
            )
            if status in {"SUBMITTED", "PARTIAL", "UNKNOWN"}:
                self.store.save_strategy_monitor_state(
                    strategy_id=strategy_id, user_id=user_id,
                    trading_date=state["tradingDate"],
                    high_watermark_equity=state["highWatermarkEquity"],
                    last_today_pnl=state["lastTodayPnl"],
                    consecutive_losses=state["consecutiveLosses"],
                    last_trade_id=state["lastTradeId"],
                    last_executed_bar=bar_time,
                    pause_reason=state["pauseReason"],
                    trading_account_id=self.account_id,
                )
            await self._execution_notice(strategy, sent_orders, reason=self._signal_text(intent), turn_id=turn_id)
            return {"strategyId": strategy_id, "status": status.lower(), "clientOrderIds": client_ids}
        except (AutonomyError, SimulationOrderError, KeyError, RuntimeError, ValueError) as error:
            current_status = "FAILED"
            if current_client_id is not None:
                try:
                    order = self.autonomy.simulation_orders.get(current_client_id, user_id=user_id)
                    current_status, _, _ = _execution_status(str(order["displayStatus"]))
                    if not any(item.get("clientOrderId") == current_client_id for item in sent_orders):
                        sent_orders.append(order)
                except (KeyError, RuntimeError, ValueError):
                    current_status = "UNKNOWN"
            await self._execution_notice(strategy, sent_orders, reason=self._signal_text(intent), turn_id=turn_id)
            if "leg_id" in locals() and len(leg_statuses) < len(plans):
                self.store.finish_strategy_execution_leg(
                    leg_id=leg_id,
                    status=current_status,
                    client_order_id=current_client_id,
                    detail=str(error)[:200],
                )
            failed_status = (
                "UNKNOWN" if "UNKNOWN" in [*leg_statuses, current_status]
                else "PARTIAL" if any(item in {"SUBMITTED", "PARTIAL"} for item in leg_statuses)
                else current_status
            )
            self.store.finish_strategy_execution_attempt(
                attempt_id=attempt_id,
                status=failed_status,
                client_order_id=",".join(client_ids) or None,
                detail=str(error)[:200],
            )
            if str(error) == "ai_execution_stopped":
                self.proposals.transition_strategy_status(
                    strategy_id=strategy_id, user_id=user_id, target="paused"
                )
                await self._pause_notice(strategy, "AI 模拟执行已停止", state["tradingDate"])
            return {"strategyId": strategy_id, "status": failed_status.lower(), "detail": str(error)}

    # -- 停止并平仓 / 持仓方向 (策略页改版第八批, 2026-10-08) -----------------------------------------

    def _own_position(self, strategy: dict[str, Any], *, plan: Any) -> tuple[str | None, int] | None:
        """策略只管自己开的仓 (所有者 2026-10-08): 自动下单 and 出卡确认 judge off the strategy's own
        lots — held_position's read (its armed protection, capped by the account) — so a manual or
        AI position on the same contract is never counted, never closed by the strategy and never
        suppresses its own open. 只提醒 keeps the account-wide read (the user acts on the reminder
        by hand, so a manually opened lot must still earn its close reminder); a spread keeps its
        two-leg account read (held_position would only rebuild the same legs)."""
        if plan is not None and plan.spread is not None:
            return None
        if self.signal_mode(strategy) not in {"auto", "confirm"}:
            return None
        held = self.held_position(strategy)
        return (held["direction"], int(held["volume"]))

    def held_position(self, strategy: dict[str, Any],
                      positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """The instance's own position: {direction long|short|None, volume, legs}. A single contract
        counts the lots its armed strategy protection still covers (fills the strategy made — never
        a position the user opened by hand), capped by what the account holds; a spread reads its
        two legs from the account (``spread_position``). ``legs`` = [(symbol, direction, volume)]."""
        user_id, symbol = str(strategy["userId"]), str(strategy["symbol"])
        definition = strategy.get("definition") or {}
        if positions is None:
            try:
                positions = list(self.autonomy.data_provider.positions() or [])
            except Exception:  # noqa: BLE001 — an unreadable account reads as no position
                positions = []
        plan = definition.get("plan") or {}
        if plan.get("spread"):
            from .strategy_rules import StrategyPlan
            from .strategy_spread import spread_position
            spec = StrategyPlan.model_validate(plan).spread
            assert spec is not None
            held = spread_position(spec, positions)
            if not held["direction"]:
                return {"direction": None, "volume": 0, "legs": []}
            units = int(held["volume"])
            first, second = ("long", "short") if held["direction"] == "long" else ("short", "long")
            return {"direction": held["direction"], "volume": units, "legs": [
                (spec.legs[0].symbol, first, spec.legs[0].lots * units),
                (spec.legs[1].symbol, second, spec.legs[1].lots * units)]}
        with self.store.lock:
            rows = self.store.connection.execute(
                """SELECT position_direction, SUM(remaining_volume) AS volume FROM ai_stop_orders
                   WHERE user_id = ? AND lower(symbol) = lower(?) AND remaining_volume > 0
                   AND json_extract(strategy_metadata_json, '$.strategyId') = ?
                   GROUP BY position_direction""", (user_id, symbol, strategy["strategyId"])).fetchall()
        for row in rows:
            direction = str(row["position_direction"])
            account = sum(max(0, int(item.get("volume") or 0)) for item in positions
                          if isinstance(item, dict) and str(item.get("symbol") or "").casefold() == symbol.casefold()
                          and item.get("direction") == direction)
            volume = min(int(row["volume"] or 0), account)
            if volume > 0:
                return {"direction": direction, "volume": volume, "legs": [(symbol, direction, volume)]}
        return {"direction": None, "volume": 0, "legs": []}

    async def propose_close_after_stop(self, strategy: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
        """停止并平仓: after the instance was stopped, one close card per leg (平今 / 平昨 split) for
        the position it still holds, in its conversation. Each card carries its strategy's
        attribution — a persisted stop_close attempt on the same protocol as a signal's
        confirmation card — so its confirmed close spends only this strategy's own protection
        rows, never the account's older (possibly manual) ones (#654). Nothing is sent: the user
        confirms each card through the ordinary confirm → order — also when the instance ran
        自动下单. Returns {cards, detail}; no position (or nowhere to put a card) makes no card."""
        # 同一快照 (#675): the account context (positions / trading day / for a spread the
        # activity baseline) and the cards built from it are all obtained inside ONE provider
        # snapshot, and held / the today-historical buckets are computed only after that
        # context is in hand — the buckets never pair one snapshot's day with another
        # snapshot's positions, and the candidate build's own position re-read sees the same
        # source. For a spread the whole context must be trusted (a failed or stale read
        # issues no card at all, never guessed); a single-contract stop keeps the tolerant
        # read it always had.
        plan = ((strategy.get("definition") or {}).get("plan") or {})
        is_spread = bool(plan.get("spread"))
        symbols: list[str] = []
        if is_spread:
            from .strategy_rules import StrategyPlan
            spread_spec = StrategyPlan.model_validate(plan).spread
            symbols = [leg.symbol for leg in spread_spec.legs] if spread_spec is not None else []
        elif str(strategy.get("symbol") or "") != "*":
            symbols = [str(strategy["symbol"])]
        try:
            # 价差出卡读同一份采集事实 (#700 review): account=True 把交易日 / 持仓 / 成交基线
            # 收进 sidecar 的一次 combined 读，不再把新鲜 activity 配轮询缓存的持仓。
            # 单合约停止保持它一直以来的宽容读法。
            async with provider_read_snapshot(self.autonomy.data_provider, symbols=symbols, refresh=True,
                                              account=is_spread):
                return await self._stop_close_cards_for(strategy, session_id=session_id, is_spread=is_spread)
        except _StopCloseContextUnavailable:
            return {"cards": [], "detail": "账户成交记录暂时读不到，没有出平仓卡；请稍后重新停止并平仓。"}

    async def _stop_close_cards_for(
        self, strategy: dict[str, Any], *, session_id: str | None, is_spread: bool,
    ) -> dict[str, Any]:
        """The body of 停止并平仓, run inside the caller's provider snapshot (its reads are
        same-source by construction; `_StopCloseContextUnavailable` is the spread's no-card
        outcome when that context is not trusted)."""
        try:
            positions = list(self.autonomy.data_provider.positions() or [])
            trading_day = str((self.autonomy.data_provider.account_summary("") or {})
                              .get("tradingDay") or "")
            activity = (self.autonomy.data_provider.account_activity() if is_spread else None)
        except Exception:  # noqa: BLE001
            if is_spread:
                raise _StopCloseContextUnavailable from None
            positions, trading_day, activity = [], "", None
        held = self.held_position(strategy, positions)
        if not held["legs"]:
            return {"cards": [], "detail": "这个策略现在没有持仓，不用平仓。"}
        session_id = session_id or self.notice_session(strategy)
        if session_id is None:
            return {"cards": [], "detail": "没有可以出平仓卡的对话：请打开一个对话后在持仓里平仓。"}
        name = self._display_name(strategy)
        user_id, strategy_id = str(strategy["userId"]), str(strategy["strategyId"])
        # 价差基线 (#675): a spread leg's card is only provably safe to confirm if the fills
        # the account had at stop time are persisted with it — confirm compares the same read
        # (provider activity + kept fills) against this baseline to see whether another close
        # spent the leg since the stop. Without a current activity read there is no trusted
        # baseline, so no spread card is issued at all (never guessed, never backfilled later).
        # 定向基线 (#700 review): the baseline carries only the fills confirm will actually
        # judge — this leg's own close direction / offset, and not a close provably dated to
        # another trading day; the account's whole fill history no longer rides on every card.
        baselines: dict[tuple[str, str, str], list[str]] | None = None
        if is_spread:
            try:
                from .trading_costs import account_fills
                if not isinstance(activity, dict):
                    raise ValueError("strategy_activity_missing")
                require_current_activity(activity)
                baselines = {}
            except Exception:  # noqa: BLE001 — no trusted baseline, no spread card
                raise _StopCloseContextUnavailable from None

            def baseline_ids(symbol: str, direction: str, bucket: str) -> list[str]:
                key = (symbol, direction, bucket)
                if key not in baselines:  # noqa: SIM108 — cache per leg symbol / direction / bucket
                    offsets = {"today": ("CLOSETODAY",), "historical": ("CLOSE",)}.get(
                        bucket, ("CLOSE", "CLOSETODAY"))
                    close_direction = "SELL" if direction == "long" else "BUY"
                    fills = account_fills(self.store, list(activity.get("trades") or []), symbol,
                                          trading_account_id=self.account_id,
                                          close_direction=close_direction, offsets=offsets,
                                          trading_day=trading_day)
                    baselines[key] = sorted(str(fill["tradeId"]) for fill in fills if fill.get("tradeId"))
                return baselines[key]
        card_turn = f"strategy-stop-close-{uuid4().hex[:12]}"
        cards: list[dict[str, Any]] = []
        failure: Exception | None = None
        failed_symbol = ""
        for symbol, direction, volume in held["legs"]:
            side = "sell_close" if direction == "long" else "buy_close"
            for bucket, plan_volume in close_legs(positions, symbol=symbol, direction=direction, volume=volume,
                                                  preference=prefs_for(self.store, user_id).close_preference):
                # The card's attribution, registered before the card exists: an intent snapshot
                # (symbol / direction / bucket / lots / strategy version / the day the buckets
                # were read / the fills already known) plus the stop_close attempt the
                # confirmed order is carried back onto.
                attempt_id, intent_id = str(uuid4()), str(uuid4())
                bar_token = f"stop-close-{uuid4().hex[:16]}"
                payload = {"kind": "stop_close", "strategyId": strategy_id, "symbol": symbol,
                           "direction": direction, "positionBucket": bucket, "volume": plan_volume,
                           "tradingDay": trading_day, "strategyVersion": strategy.get("version"),
                           "strategyDigest": strategy.get("definitionDigest")}
                if baselines is not None:
                    payload["baselineFillIds"] = baseline_ids(symbol, direction, bucket)
                self.store.save_strategy_intent_snapshot(
                    intent_id=intent_id, strategy_id=strategy_id, user_id=user_id,
                    strategy_digest=str(strategy.get("definitionDigest") or ""),
                    signal_bar_time=bar_token, context_digest=str(uuid4()),
                    payload=payload,
                )
                if not self.store.reserve_strategy_execution_attempt(
                    attempt_id=attempt_id, strategy_id=strategy_id, user_id=user_id,
                    intent_id=intent_id, signal_bar_time=bar_token, trading_day=trading_day,
                    action="stop_close", volume=plan_volume, trading_account_id=self.account_id,
                ):
                    failure = RuntimeError("strategy_execution_attempt_conflict")
                    continue
                try:
                    card = self.proposals.create_trade(
                        session_id, card_turn, symbol=symbol, side=side, volume=plan_volume,
                        price_intent="aggressive", reason=f"停止策略「{name}」后平掉它的持仓",
                        position_bucket=None if bucket == "all" else bucket,
                        strategy_source={"strategyId": strategy_id, "name": name, "attemptId": attempt_id,
                                         "purpose": "stop_close"},
                        account_id=self._card_account_id(),
                    )
                except (ProposalConflict, SimulationOrderError, KeyError, RuntimeError, ValueError) as error:
                    failure, failed_symbol = error, symbol
                    LOGGER.warning("strategy_stop_close_card_failed strategy=%s account=%s symbol=%s code=%s",
                                   strategy_id, self.account_id, symbol, _error_code(error))
                    self.store.finish_strategy_execution_attempt(
                        attempt_id=attempt_id, status="FAILED",
                        detail=self._stop_close_failure_text(error, symbol)[:200])
                    continue
                self.store.record_strategy_cards(
                    attempt_id=attempt_id, proposal_ids=[str(card["proposalId"])],
                    detail="停止平仓已出卡，等待确认；确认后只平这个策略自己的仓。")
                cards.append(card)
        if not cards:
            return {"cards": [], "detail": (self._stop_close_failure_text(failure, failed_symbol)[:200]
                                            if failure else "没有足够可平的持仓。")}
        legs = "、".join(f"{card.get('symbol', '')} {card.get('volume', '')} 手" for card in cards)
        text = f"已停止「{name}」。它还有持仓，出了平仓卡（{legs}），确认后才平仓。"
        await self._session_notice(strategy, text, kind="strategy-card", session_id=session_id,
                                   cards=[("proposal.trade", card) for card in cards], turn_id=card_turn)
        detail = ("已出平仓卡，在对话里确认后才平仓。" if not failure
                  else f"部分{self._stop_close_failure_text(failure, failed_symbol)}"[:200])
        return {"cards": cards, "detail": detail, "sessionId": session_id}

    def _card_account_id(self) -> str | None:
        """The account a strategy card acts on: the instance's own (#1011), never the account the
        conversation it lands in is switched to now — a stop-close card made on the conversation's
        other account found no position there. None without account wiring (single account)."""
        return self.account_id if getattr(self.proposals, "accounts", None) is not None else None

    def _stop_close_failure_text(self, error: Exception, symbol: str) -> str:
        """停止并平仓 that made no card, as one sentence the user can act on; the internal code goes to
        the log, never alone onto the screen (#1011)."""
        code = _error_code(error)
        try:
            contract = instrument_display(self.autonomy.data_provider, symbol) if symbol else "这个合约"
        except Exception:  # noqa: BLE001 — the catalog only names the contract
            contract = symbol or "这个合约"
        if code == "close_position_not_found":
            return f"平仓卡没有生成：没找到这个策略在{contract}的持仓，请到持仓里手动平。"
        reason = CANDIDATE_ISSUE_LABELS.get(code) or getattr(error, "message", None) or "出卡时出错"
        return f"平仓卡没有生成（{contract}）：{reason}，请到持仓里手动平。"

    # -- 执行方式 (2026-09-25) ------------------------------------------------------------------

    def forget_offer(self, strategy_id: str, user_id: str) -> None:
        """A changed 执行方式 or a stop: the current signal is offered afresh next time."""
        self.store.reset_strategy_signal(strategy_id=strategy_id, user_id=user_id)

    def signal_mode(self, strategy: dict[str, Any]) -> str:
        """The instance's own 执行方式; auto degrades to confirm while AI 自主执行 is off. A 只提醒 strategy
        (提醒页的提醒, `plan.reminderOnly`) only ever reminds, whatever its row says."""
        if ((strategy.get("definition") or {}).get("plan") or {}).get("reminderOnly"):
            return "alert"
        mode = str(strategy.get("signalAction") or "auto")
        if mode not in SIGNAL_ACTIONS:
            mode = "auto"
        if mode == "auto" and not self.autonomy.autonomy_enabled(str(strategy["userId"])):
            return "confirm"
        return mode

    def _display_name(self, strategy: dict[str, Any]) -> str:
        """The library strategy's own name (an instance is named "名称 · 合约")."""
        library_id = strategy["definition"].get("libraryStrategyId")
        if library_id:
            try:
                return str(self.store.get_strategy_draft(str(library_id), user_id=strategy["userId"])["name"])
            except KeyError:
                pass
        name = str(strategy["name"])
        suffix = f" · {strategy['symbol']}"
        return name[: -len(suffix)] if name.endswith(suffix) else name

    def _contract_label(self, strategy: dict[str, Any], turn_id: str) -> str:
        try:
            market = self.autonomy.data_provider.market_snapshot(turn_id, strategy["symbol"])
        except Exception:
            market = {}
        name = market.get("name") if isinstance(market, dict) else None
        return str(name or strategy["symbol"])

    @staticmethod
    def _signal_text(intent: dict[str, Any]) -> str:
        reason = str((intent.get("positionIntent") or {}).get("reason")
                     or (intent.get("riskAdjustedIntent") or {}).get("reason") or "").strip()
        return reason.rstrip("。.")

    def notice_session(self, strategy: dict[str, Any]) -> str | None:
        """Where the instance's reminders, cards and pause notices go: the conversation it was
        started from, else the owner's most recently active chat on this account; None = nowhere
        (the caller only logs)."""
        user_id = str(strategy["userId"])
        session_id = strategy.get("signalSessionId")
        if session_id:
            try:
                if self.store.get_session(str(session_id), user_id=user_id)["status"] == "active":
                    return str(session_id)
            except KeyError:
                pass
        recent = self.store.recent_chat_session(user_id=user_id, mode=None,
                                                trading_account_id=self.account_id)
        return str(recent["sessionId"]) if recent else None

    async def _execution_notice(self, strategy: dict[str, Any], orders: list[dict[str, Any]], *,
                                reason: str, turn_id: str) -> None:
        """执行回报卡 (2026-10-04): what 自动下单 just sent, one card per leg, in the same conversation
        the instance's confirmation cards go to — so an automatic order leaves a record in the chat.
        Each card's status is the order's read-back (结果未知 stays unknown); the order watch re-reads
        them and republishes the cards as they fill or end. Not spoken: fills have their own voice."""
        if not orders:
            return
        session_id = self.notice_session(strategy)
        if session_id is None:
            return
        name = self._display_name(strategy)
        card_turn = f"strategy-exec-{uuid4().hex[:12]}"
        contract = self._contract_label(strategy, turn_id)
        cards = [execution_card(turn_id=card_turn, source="strategy", source_label=f"策略 · {name}", order=order,
                                reason=reason, name=contract) for order in orders]
        statuses = "、".join(card["statusLabel"] for card in cards)
        text = (f"{name} · {contract}：自动下单 {len(cards)} 笔委托（{cards[0]['sideLabel']} "
                f"{sum(card['volume'] for card in cards)} 手），回读状态：{statuses}。")
        if await self._session_notice(strategy, text, kind="strategy-execution", session_id=session_id,
                                      cards=[("card.execution", card) for card in cards], turn_id=card_turn,
                                      speak=False):
            broker = getattr(self.autonomy, "broker", None)
            for card in cards:
                track_execution_card(broker, session_id, card, simulation_orders=self.autonomy.simulation_orders,
                                     user_id=str(strategy["userId"]))

    async def _session_notice(
        self, strategy: dict[str, Any], text: str, *, kind: str, session_id: str | None = None,
        cards: list[tuple[str, dict[str, Any]]] | None = None, turn_id: str | None = None,
        speak: bool = True, dedupe_key: str | None = None,
    ) -> bool:
        """One message (plus the given cards) in the instance's conversation through the one reminder entry
        (#932): the 策略 switch (notify.strategySignalAlerts) over every channel, the 提醒中心 row, the desktop
        notification, the line on a call that is online now (named by the strategy, so two strategies'
        lines are both heard) and — for a signal, its cards or an automatic order — the push. A confirmation
        card or an execution card is the business itself: it lands with the switch off. `dedupe_key` makes a
        notice once across restarts and slots (pauses, the schedule warning); without one the caller's own
        record already made it once. Returns whether the message landed."""
        from .reminders import ReminderSpec, deliver_via_store
        from .web_push import strategy_message
        user_id = str(strategy["userId"])
        session_id = session_id or self.notice_session(strategy)
        broker = getattr(self.autonomy, "broker", None)
        if session_id is None or broker is None:
            LOGGER.info("strategy_notice_without_session strategy=%s kind=%s text=%s",
                        strategy["strategyId"], kind, text)
        turn = turn_id or f"strategy-{uuid4().hex[:12]}"
        first = (cards or [(None, {})])[0][1]
        card_id = first.get("proposalId") or first.get("cardId")
        voice = self.voice_lookup() if self.voice_lookup is not None and speak else None
        delivery = await deliver_via_store(self.store, ReminderSpec(
            kind="strategy", user_id=user_id, account_id=self.account_id,
            dedupe_key=f"{strategy['strategyId']}:{dedupe_key}" if dedupe_key else turn,
            title=f"策略 · {self._display_name(strategy)}", body=text, symbol=str(strategy.get("symbol") or ""),
            message=text, turn_id=turn, cards=list(cards or []), session_id=session_id, auto_landing=False,
            essential=kind in ESSENTIAL_NOTICE_KINDS, speech=text if speak else None, voice_kind=kind,
            voice_key=str(strategy["strategyId"]),
            push=(strategy_message(name=self._display_name(strategy), text=text,
                                   card_id=str(card_id) if card_id else None)
                  if kind in PUSHED_NOTICE_KINDS else None)),
            broker=broker, voice=voice)
        return delivery.landed

    async def _alert_signal(
        self, *, strategy: dict[str, Any], intent: dict[str, Any], action: str, volume: int, bar_time: str,
        trading_day: str, turn_id: str,
    ) -> dict[str, Any]:
        """只提醒: one message per signal (never twice for one bar), no card, no order. The reminder
        is recorded as a SKIPPED attempt marked alert — never an open toward maxOrdersPerDay, never
        an entry attempt of the alignment segment — so it is not repeated after a restart."""
        from .strategy_intents import signal_action_label
        strategy_id = strategy["strategyId"]
        attempt_id = str(uuid4())
        if not self.store.reserve_strategy_execution_attempt(
            attempt_id=attempt_id, strategy_id=strategy_id, user_id=strategy["userId"],
            intent_id=intent["intentId"], signal_bar_time=bar_time, trading_day=trading_day,
            action=action, volume=volume, trading_account_id=self.account_id, signal_action="alert",
        ):
            return {"strategyId": strategy_id, "status": "duplicate", "detail": "该已完成K线已提醒。"}
        label = signal_action_label(intent, action)
        act = "加仓" if (intent["signalSnapshot"] or {}).get("add") else label
        signal = self._signal_text(intent)
        subject = self._contract_label(strategy, turn_id)
        legs_text = None
        spec = (strategy["definition"].get("plan") or {}).get("spread")
        if spec is not None:
            # 价差: the act names the spread direction and the legs spell out what it would trade.
            from .strategy_rules import SpreadSpec
            from .strategy_spread import spread_legs_words, spread_words
            spread = SpreadSpec.model_validate(spec)
            act = {"open_long": "做多价差", "open_short": "做空价差",
                   "close_long": "平掉做多价差", "close_short": "平掉做空价差"}.get(action, act)
            subject = spread_words(spread)
            legs_text = spread_legs_words(spread, action, max(1, volume))
        text = (f"{self._display_name(strategy)} · {subject}："
                f"{signal + '，' if signal else ''}{act}条件满足（只提醒，未下单）")
        if legs_text is not None:
            text += f"：{legs_text}"
        self.store.finish_strategy_execution_attempt(attempt_id=attempt_id, status="SKIPPED",
                                                     detail=f"只提醒，未下单：{act}条件满足")
        await self._session_notice(strategy, text, kind="strategy-signal")
        return {"strategyId": strategy_id, "status": "alerted", "action": action, "detail": text}

    async def _propose_signal(
        self, *, strategy: dict[str, Any], intent: dict[str, Any], action: str, volume: int,
        bar_time: str, trading_day: str, positions: list[dict[str, Any]], turn_id: str,
    ) -> dict[str, Any]:
        """出卡确认: one confirmation card per planned leg in the instance's conversation, at most
        once per completed bar (the reserved attempt is the dedupe). Nothing is sent: confirming a
        card is the ordinary confirm → order, which then carries the order back onto the attempt."""
        from .strategy_intents import signal_action_label
        from .strategy_rules import uses_structural_stop
        strategy_id, user_id = strategy["strategyId"], strategy["userId"]
        session_id = self.notice_session(strategy)
        attempt_id = str(uuid4())
        if not self.store.reserve_strategy_execution_attempt(
            attempt_id=attempt_id, strategy_id=strategy_id, user_id=user_id,
            intent_id=intent["intentId"], signal_bar_time=bar_time, trading_day=trading_day,
            action=action, volume=volume, trading_account_id=self.account_id, signal_action="confirm",
        ):
            # This bar already has an attempt (a card, or a card that could not be made): nothing
            # new to offer, and a failed card must not read as "已出确认卡".
            return {"strategyId": strategy_id, "status": "duplicate", "detail": "该已完成K线已处理。"}
        if session_id is None:
            # Nowhere to put a card: only the log (and this record, so it is not logged every poll).
            LOGGER.info("strategy_card_skipped_no_session strategy=%s user=%s bar=%s action=%s",
                        strategy_id, user_id, bar_time, action)
            self.store.finish_strategy_execution_attempt(attempt_id=attempt_id, status="SKIPPED",
                                                         detail="没有可出卡的对话，本次信号只记录")
            return {"strategyId": strategy_id, "status": "blocked", "action": action,
                    "detail": "没有可出卡的对话，本次信号只记录；打开一个对话后下个信号会出卡。"}
        side = SIGNAL_SIDES.get(action)
        if side is None:
            self.store.finish_strategy_execution_attempt(attempt_id=attempt_id, status="SKIPPED", detail="没有可执行动作")
            return {"strategyId": strategy_id, "status": "waiting", "detail": "没有可执行动作。"}
        plans: list[tuple[str | None, int]]
        if side.endswith("_close"):
            plans = list(close_legs(positions, symbol=strategy["symbol"],
                                    direction="long" if side == "sell_close" else "short", volume=volume,
                                    preference=prefs_for(self.store, user_id).close_preference))
        else:
            plans = [(None, volume)]
        if not plans:
            self.store.finish_strategy_execution_attempt(attempt_id=attempt_id, status="BLOCKED", detail="没有足够可平持仓")
            return {"strategyId": strategy_id, "status": "blocked", "action": action, "detail": "没有足够可平持仓。"}
        name = self._display_name(strategy)
        signal = self._signal_text(intent)
        label = signal_action_label(intent, action)
        is_add = bool(intent["signalSnapshot"].get("add"))
        card_turn = f"strategy-card-{uuid4().hex[:12]}"
        price_intent = ("last" if side.endswith("_open") and uses_structural_stop(strategy["definition"])
                        and not is_add else "aggressive")
        cards: list[dict[str, Any]] = []
        failure: Exception | None = None
        for bucket, plan_volume in plans:
            try:
                cards.append(self.proposals.create_trade(
                    session_id, card_turn, symbol=strategy["symbol"], side=side, volume=plan_volume,
                    price_intent=price_intent, reason=f"{name}：{signal or label + '信号'}",
                    position_bucket=bucket, account_id=self._card_account_id(),
                    # barTime: the signal bar the confirm-time entry recheck (structural stops,
                    # pullbacks) holds the card to, exactly as for the monitor's own order.
                    strategy_source={"strategyId": strategy_id, "name": name, "attemptId": attempt_id,
                                     "barTime": bar_time,
                                     **({"add": True} if is_add else {}),
                                     **({"protectionLevels": intent["signalSnapshot"]["protectionLevels"]}
                                        if intent["signalSnapshot"].get("protectionLevels") else {})},
                ))
            except (ProposalConflict, SimulationOrderError, KeyError, RuntimeError, ValueError) as error:
                failure = error
                break
        if not cards:
            detail = f"确认卡没有生成：{failure}"[:200]
            self.store.finish_strategy_execution_attempt(attempt_id=attempt_id, status="FAILED", detail=detail)
            return {"strategyId": strategy_id, "status": "failed", "action": action, "detail": detail}
        self.store.record_strategy_cards(attempt_id=attempt_id,
                                         proposal_ids=[str(card["proposalId"]) for card in cards],
                                         detail=CARD_WAITING_DETAIL)
        legs = "、".join(f"{'平今' if bucket == 'today' else '平昨' if bucket == 'historical' else ''}{plan_volume} 手"
                        for bucket, plan_volume in plans[:len(cards)])
        text = (f"{name} · {self._contract_label(strategy, turn_id)}：{signal + '，' if signal else ''}"
                f"{label}信号出了确认卡（{legs}），确认后才下单。")
        await self._session_notice(strategy, text, kind="strategy-card", session_id=session_id,
                                   cards=[("proposal.trade", card) for card in cards], turn_id=card_turn)
        return {"strategyId": strategy_id, "status": "proposed", "action": action,
                "detail": "已出确认卡，等待确认。", "proposalIds": [card["proposalId"] for card in cards]}

    async def _propose_spread_signal(
        self, *, strategy: dict[str, Any], plan: Any, intent: dict[str, Any], action: str,
        volume: int, bar_time: str, trading_day: str, positions: list[dict[str, Any]],
        turn_id: str = "",
    ) -> dict[str, Any]:
        """出卡确认 (价差): ONE card carrying both legs — the spread definition, the current spread,
        one unit's composition and the spread stop / take — at most once per completed bar (the
        reserved attempt is the dedupe). Nothing is sent: confirming the card is the ordinary
        confirm route, which hands both legs to the spread executor together."""
        from .strategy_intents import signal_action_label
        from .strategy_spread import spread_legs_words, spread_position, spread_words
        strategy_id, user_id = strategy["strategyId"], strategy["userId"]
        session_id = self.notice_session(strategy)
        attempt_id = str(uuid4())
        if not self.store.reserve_strategy_execution_attempt(
                attempt_id=attempt_id, strategy_id=strategy_id, user_id=user_id,
                intent_id=intent["intentId"], signal_bar_time=bar_time, trading_day=trading_day,
                action=action, volume=volume, trading_account_id=self.account_id,
                signal_action="confirm",
        ):
            # This bar already has an attempt (a card, or a card that could not be made): nothing
            # new to offer, and a failed card must not read as "已出确认卡".
            return {"strategyId": strategy_id, "status": "duplicate", "detail": "该已完成K线已处理。"}
        if session_id is None:
            # Nowhere to put a card: only the log (and this record, so it is not logged every poll).
            LOGGER.info("strategy_card_skipped_no_session strategy=%s user=%s bar=%s action=%s",
                        strategy_id, user_id, bar_time, action)
            self.store.finish_strategy_execution_attempt(attempt_id=attempt_id, status="SKIPPED",
                                                         detail="没有可出卡的对话，本次信号只记录")
            return {"strategyId": strategy_id, "status": "blocked", "action": action,
                    "detail": "没有可出卡的对话，本次信号只记录；打开一个对话后下个信号会出卡。"}
        held = spread_position(plan.spread, positions)
        name = self._display_name(strategy)
        signal = self._signal_text(intent)
        label = {"open_long": "做多价差", "open_short": "做空价差", "close_long": "平掉做多价差",
                 "close_short": "平掉做空价差"}.get(action) or signal_action_label(intent, action)
        card_turn = f"strategy-card-{uuid4().hex[:12]}"
        try:
            card = self.proposals.create_spread(
                session_id, card_turn, spec=plan.spread, action=action, units=max(1, volume),
                reason=f"{name}：{signal or label + '信号'}",
                strategy_source={"strategyId": strategy_id, "name": name, "attemptId": attempt_id,
                                 "barTime": bar_time},
                current_spread=self._current_spread(plan),
                entry_spread=(float(held["entry_price"])
                              if held["entry_price"] is not None else None),
                exit_reason=None, account_id=self._card_account_id(),
            )
        except (ProposalConflict, SimulationOrderError, KeyError, RuntimeError, ValueError) as error:
            detail = f"确认卡没有生成：{error}"[:200]
            self.store.finish_strategy_execution_attempt(attempt_id=attempt_id, status="FAILED", detail=detail)
            return {"strategyId": strategy_id, "status": "failed", "action": action, "detail": detail}
        self.store.record_strategy_cards(attempt_id=attempt_id, proposal_ids=[str(card["proposalId"])],
                                         detail=CARD_WAITING_DETAIL)
        legs_text = spread_legs_words(plan.spread, action, max(1, volume))
        text = (f"{name} · {spread_words(plan.spread)}：{signal + '，' if signal else ''}"
                f"{label}信号出了确认卡（{legs_text}），确认后两腿同时下单。")
        await self._session_notice(strategy, text, kind="strategy-card", session_id=session_id,
                                   cards=[("proposal.spread", card)], turn_id=card_turn)
        return {"strategyId": strategy_id, "status": "proposed", "action": action,
                "detail": "已出确认卡，等待确认。", "proposalIds": [card["proposalId"]]}

    async def _spread_exit_check(
        self, *, strategy: dict[str, Any], plan: Any, positions: list[dict[str, Any]],
        activity: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """每秒价差止损/止盈 (第二期): while a spread position is held, the spread is computed from
        both legs' LATEST quotes every poll — the completed bar stays the backtest's judgement.
        Nothing judges while a close is already working (the executor's own follow or a live leg
        order) or while either leg's quote is stale; 只提醒 only reminds, once per position."""
        from .strategy_rules import StrategyRuleError
        from .strategy_spread import spread_position, spread_price, spread_value_text, spread_words
        spec = plan.spread
        strategy_id, user_id = strategy["strategyId"], strategy["userId"]
        if spec.stopLoss is None and spec.takeProfit is None:
            return None
        held = spread_position(spec, positions)
        if held["direction"] is None or int(held["volume"] or 0) <= 0 or held["entry_price"] is None:
            return None
        if (self.spread_execution.running(strategy_id)
                or any(_active_order_for_symbol(activity, leg.symbol) for leg in spec.legs)):
            return None
        prices: dict[str, float] = {}
        for leg in spec.legs:
            item = self.autonomy.data_provider.market_workbench(leg.symbol, int(plan.timeframeSeconds))
            if (item.get("dataStatus") not in {"normal", "simulated"} or item.get("isCached") is True
                    or float(item.get("ageSeconds", 999)) > 15):
                return None                       # 任一腿行情不新鲜：这一秒不判断
            try:
                prices[leg.symbol] = float((item.get("quote") or {}).get("lastPrice"))
            except (TypeError, ValueError):
                return None
        leg_a, leg_b = spec.legs
        try:
            current = spread_price(spec.kind, prices[leg_a.symbol], prices[leg_b.symbol],
                                   leg_a.weight, leg_b.weight)
        except StrategyRuleError:
            return None
        entry = float(held["entry_price"])
        pnl = current - entry if held["direction"] == "long" else entry - current
        triggered = ("spread_stop" if spec.stopLoss is not None and pnl <= -abs(spec.stopLoss)
                     else "spread_take" if spec.takeProfit is not None and pnl >= abs(spec.takeProfit)
                     else None)
        if triggered is None:
            return None
        label = "止损" if triggered == "spread_stop" else "止盈"
        action = f"close_{held['direction']}"
        units = int(held["volume"])
        exit_key = f"{triggered}:{state['tradingDate']}:{held['direction']}:{entry:.6f}:{units}"
        if exit_key in self.store.strategy_exit_keys(strategy_id=strategy_id, user_id=user_id):
            return None                          # 同一持仓只触发一次
        bar_time = str(time_ns())
        intent_id = str(uuid4())
        self.store.save_strategy_intent_snapshot(
            intent_id=intent_id, strategy_id=strategy_id, user_id=user_id,
            strategy_digest=strategy["definitionDigest"], signal_bar_time=bar_time,
            context_digest=uuid4().hex,
            payload={"signalSnapshot": {"barTime": bar_time, "exitKey": exit_key,
                                        "exitReason": triggered, "spreadCurrent": current,
                                        "spreadEntry": entry},
                     "positionIntent": {"action": action, "requestedVolume": units,
                                        "reason": f"价差{label}触发"},
                     "riskAdjustedIntent": {"action": action, "volume": units, "status": "allowed",
                                            "reason": f"价差{label}触发"}},
        )
        if self.signal_mode(strategy) == "alert":
            attempt_id = str(uuid4())
            if not self.store.reserve_strategy_execution_attempt(
                    attempt_id=attempt_id, strategy_id=strategy_id, user_id=user_id,
                    intent_id=intent_id, signal_bar_time=bar_time, trading_day=state["tradingDate"],
                    action=action, volume=units, trading_account_id=self.account_id,
                    signal_action="alert",
            ):
                return None
            self.store.finish_strategy_execution_attempt(
                attempt_id=attempt_id, status="SKIPPED", detail=f"只提醒，未下单：价差{label}触发")
            text = (f"{self._display_name(strategy)} · {spread_words(spec)}：价差{label}触发"
                    f"（当前价差 {spread_value_text(current)}，开仓价差 {spread_value_text(entry)}），"
                    "只提醒，未下单，请手动处理。")
            await self._session_notice(strategy, text, kind="strategy-signal")
            return {"strategyId": strategy_id, "status": "alerted", "action": action, "detail": text}
        result = await self.spread_execution.execute_exit(
            strategy=strategy, plan=plan, held=held, exit_reason=triggered, intent_id=intent_id,
            bar_time=bar_time, trading_day=state["tradingDate"], current=current, entry=entry)
        return {"strategyId": strategy_id, **result}

    def _current_spread(self, plan: Any) -> float | None:
        """Both legs' latest prices as the spread's current value (lenient — a card may show it even
        when a quote is not fresh; None = not readable now)."""
        from .strategy_rules import StrategyRuleError
        from .strategy_spread import spread_price
        spec = plan.spread
        prices: dict[str, float] = {}
        for leg in spec.legs:
            try:
                item = self.autonomy.data_provider.market_workbench(
                    leg.symbol, int(plan.timeframeSeconds))
                prices[leg.symbol] = float((item.get("quote") or {}).get("lastPrice"))
            except (KeyError, TypeError, ValueError, RuntimeError):
                return None
        leg_a, leg_b = spec.legs
        try:
            return spread_price(spec.kind, prices[leg_a.symbol], prices[leg_b.symbol],
                                leg_a.weight, leg_b.weight)
        except StrategyRuleError:
            return None

    # -- 回撤停机 (2026-10-09) -------------------------------------------------------------------------

    def _round_now(self) -> datetime:
        """The clock the round is judged on: the replay clock inside a replay, else the wall."""
        return self.autonomy.simulation_orders.trading_now() or datetime.now(UTC)

    def _persisted_drawdown_round(
        self, *, strategy: dict[str, Any],
    ) -> dict[str, Any] | None:
        """同一轮的持久停机 / 清算状态行 (#692)：只读数据库即可判定——切图表周期是同一风险轮
        （旧行存的完整定义摘要同样认），真正改了定义、绑定账户或停机线才是另一轮。停机线直接读
        definition.plan 的原值，不依赖已成功构造的 StrategyPlan——构造失败的一轮同样要先被识别。
        返回状态行或 None；调用方据此在普通评估读取之前先锁定停机 / 推进清算。"""
        try:
            limit = float((strategy["definition"].get("plan") or {}).get("maxDrawdownPercent"))
        except (TypeError, ValueError):
            return None
        state = self.store.strategy_drawdown_state(
            strategy_id=str(strategy["strategyId"]), user_id=str(strategy["userId"]))
        if state is None or str(state["status"]) not in {"stopped", "liquidating"}:
            return None
        from .strategy_funds import round_identity_digest
        digest = str(strategy["definitionDigest"])
        same_round = (str(state["strategyDigest"]) in {digest, round_identity_digest(strategy["definition"])}
                      and str(state["tradingAccountId"]) == str(self.account_id)
                      and float(state["limitPercent"]) == limit)
        return state if same_round else None

    async def _drawdown_evaluate(
        self, *, strategy: dict[str, Any], plan: Any,
        account: dict[str, Any], positions: list[dict[str, Any]], market: dict[str, Any],
    ) -> dict[str, Any] | None:
        """One poll of the strategy's drawdown round. The net value is this strategy's alone
        (its baseline + this round's attributed realized PnL − its fees + its unrealized PnL);
        deposits, withdrawals and other hands never move it. Returns a blocked result only when
        the round stops opening (a trigger in progress, an unknowable net value); a healthy or
        unchanged round returns None so the normal evaluation — closes included — continues."""
        from .strategy_funds import (
            _current_fact,
            curve_append,
            drawdown_percent,
            round_identity_digest,
            round_started_ns,
            strategy_round_facts,
        )
        strategy_id, user_id = str(strategy["strategyId"]), str(strategy["userId"])
        symbol, digest, limit = str(strategy["symbol"]), str(strategy["definitionDigest"]), float(plan.maxDrawdownPercent)
        state = self.store.strategy_drawdown_state(strategy_id=strategy_id, user_id=user_id)
        # 同一轮的判定身份 (#692)：切图表周期只改由 chartPlan 派生的执行周期部分，是同一风险轮；
        # 旧行存的完整定义摘要同样认。真正改了定义（或绑定账户 / 停机线）才是另一轮。
        same_round = (state is not None
                      and str(state["strategyDigest"]) in {digest, round_identity_digest(strategy["definition"])}
                      and str(state["tradingAccountId"]) == str(self.account_id)
                      and float(state["limitPercent"]) == limit)
        if same_round and str(state["status"]) in {"stopped", "liquidating"}:
            # 已持久清算 / 停机先于本快照的新鲜度判断 (#692)：清算要继续推进（原 ID 查询/撤单照常），
            # 停机要保持锁定——不能因为行情或账户不新鲜落进普通信号评估，被三次错误计数暂停后
            # 永久搁置。平仓派发仍走自己的新鲜度、归属与授权围栏；事实恢复后自动接着清算，
            # 永不自动恢复新开仓。
            if str(state["status"]) == "stopped":
                return {"strategyId": strategy_id, "status": "blocked", "action": "hold",
                        "detail": str(state["reason"] or "本策略回撤停机中，暂不开新仓。")}
            return await self._drawdown_liquidating(strategy=strategy, state=state, limit=limit)
        if not (_current_fact(account) and _current_fact(market)):
            # This snapshot cannot judge the round. The intent's own freshness gates refuse
            # opens on the same stale facts, so nothing slips past while we wait.
            return None
        try:
            equity = float(Decimal(str(account.get("equity"))))
            price = float(Decimal(str(market.get("price"))))
            multiplier = float(Decimal(str(market.get("volumeMultiple"))))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if equity <= 0 or price <= 0 or multiplier <= 0:
            return None
        now = self._round_now()
        now_ns = int(now.timestamp() * 1_000_000_000)
        if not same_round:
            # First enable of this round — or the definition / bound account changed, whose old
            # net value must not be carried over. 起始权益 = the account's equity at enable; the
            # strategy's open PnL at that moment is kept as the round's baseline so only its
            # increments move the round's net value (a position already held never counts its
            # past PnL twice). 起点与基准同批写入（新轮的 round_started_at 就是本次）。
            opening = strategy_round_facts(
                self.store, user_id=user_id, strategy_id=strategy_id, symbol=symbol,
                account_id=self.account_id, since_ns=now_ns, latest_price=price,
                multiplier=multiplier,
                fee_settings=getattr(self.autonomy.simulation_orders, "fee_settings", None))
            state = self.store.save_strategy_drawdown_state(
                strategy_id=strategy_id, user_id=user_id, status="running", limit_percent=limit,
                initial_equity=str(round(equity, 2)), baseline_pnl=str(round(float(opening["unrealized"]), 2)),
                peak_equity=str(round(equity, 2)),
                current_equity=str(round(equity, 2)), strategy_digest=round_identity_digest(strategy["definition"]),
                round_started_at=now.isoformat(), reason=None,
                curve=[{"time": now_ns, "equity": round(equity, 2)}],
                trading_account_id=self.account_id, fee_source=str(opening["feeSource"]))
        status = str(state["status"])   # 到这里只可能是本轮的 running / unavailable
        # unavailable 也重算事实 (2026-10-10 审查)：费用口径恢复（佣金或费率重新可得）不是
        # 永久短路——沿用本轮 initial/baseline/peak 接着判（已过线仍停机）；归属不明的手动平仓
        # 不可恢复，只能显式 resetDrawdown 开新一轮。跑 unavailable 与 running 共用下面的
        # 事实计算；新开仓在整个 unavailable 期间一直被发送围栏挡着。
        facts = strategy_round_facts(
            self.store, user_id=user_id, strategy_id=strategy_id, symbol=symbol,
            account_id=self.account_id, since_ns=round_started_ns(str(state["roundStartedAt"])),
            latest_price=price, multiplier=multiplier,
            fee_settings=getattr(self.autonomy.simulation_orders, "fee_settings", None))
        if facts["unattributed"]:
            reason = str(facts["unattributed"])
        elif facts["feeSource"] == "unavailable":
            # 费用口径 (2026-10-10 审查)：本策略有成交既无实际佣金也无费率估算依据——净值
            # 不可用、暂停新开仓，不能按费用 0 继续跑；规则平仓与保护照常。
            reason = "本策略有成交缺少手续费依据（无实际佣金、也无费率），本轮净值不可用，已暂停新开仓。"
        else:
            reason = None
        if reason is not None:
            if status != "unavailable" or str(state["reason"] or "") != reason:
                # 持续不可用不重复刷同一条提示：只在进入或原因变化时存状态并提醒一次。
                state = self.store.save_strategy_drawdown_state(
                    strategy_id=strategy_id, user_id=user_id, status="unavailable",
                    limit_percent=limit,
                    initial_equity=state["initialEquity"], peak_equity=state["peakEquity"],
                    current_equity=state["currentEquity"], strategy_digest=round_identity_digest(strategy["definition"]),
                    round_started_at=str(state["roundStartedAt"]), reason=reason,
                    curve=list(state["curve"] or []), trading_account_id=self.account_id,
                    baseline_pnl=state["baselinePnl"], fee_source=str(facts["feeSource"]))
                await self._session_notice(
                    strategy, f"{self._display_name(strategy)} · {symbol}：{reason}",
                    kind="strategy-warning")
            return None
        initial = float(Decimal(str(state["initialEquity"])))
        baseline = float(Decimal(str(state["baselinePnl"] or 0)))
        current = initial + float(facts["realized"] - facts["fees"]
                                  + facts["unrealized"] - Decimal(str(baseline)))
        peak = max(float(Decimal(str(state["peakEquity"]))), current)
        curve = curve_append(list(state["curve"] or []), time_ns=now_ns, value=current)
        drawdown = drawdown_percent(peak, current)
        if drawdown >= limit:
            reason = (f"本轮策略净值从高点 {peak:,.2f} 回撤 {drawdown:.2f}%，已达回撤停机线 {limit:.2f}%："
                      "撤销本策略未成交开仓单并平掉本策略持仓。")
            state = self.store.save_strategy_drawdown_state(
                strategy_id=strategy_id, user_id=user_id, status="liquidating", limit_percent=limit,
                initial_equity=str(round(initial, 2)), peak_equity=str(round(peak, 2)),
                current_equity=str(round(current, 2)), strategy_digest=round_identity_digest(strategy["definition"]),
                round_started_at=str(state["roundStartedAt"]), triggered_at=now.isoformat(),
                reason=reason, curve=curve, trading_account_id=self.account_id,
                baseline_pnl=state["baselinePnl"], fee_source=str(facts["feeSource"]))
            await self._session_notice(
                strategy, f"{self._display_name(strategy)} · {symbol}：{reason}", kind="strategy-drawdown")
            return await self._drawdown_liquidating(strategy=strategy, state=state, limit=limit)
        self.store.save_strategy_drawdown_state(
            strategy_id=strategy_id, user_id=user_id, status="running", limit_percent=limit,
            initial_equity=str(round(initial, 2)), peak_equity=str(round(peak, 2)),
            current_equity=str(round(current, 2)), strategy_digest=round_identity_digest(strategy["definition"]),
            round_started_at=str(state["roundStartedAt"]), reason=None, curve=curve,
            trading_account_id=self.account_id,
            baseline_pnl=state["baselinePnl"], fee_source=str(facts["feeSource"]))
        return None

    def _observed_fill_volume(self, user_id: str, client_order_id: str) -> int:
        """How many lots of this order the fill stream has actually attributed so far (the
        same source pending_open_lots counts observed fills from)."""
        with self.store.lock:
            rows = self.store.connection.execute(
                """SELECT payload_json FROM strategy_fill_events
                   WHERE user_id = ? AND trading_account_id = ? AND client_order_id = ?""",
                (user_id, self.account_id, client_order_id)).fetchall()
        observed = 0
        for row in rows:
            try:
                observed += int(json.loads(row[0]).get("trade", {}).get("volume") or 0)
            except (KeyError, TypeError, ValueError):
                continue
        return observed

    def _strategy_open_orders_pending(self, *, user_id: str, strategy_id: str) -> list[dict[str, Any]]:
        """This strategy's own orders that have not reached a terminal state, open or close,
        read back through the simulation gateway — every formal link the strategy has, not
        only its execution legs: an attempt's own client_order_id (a confirmed card's order
        never gets a leg) and its armed protection's close order (a protective close lives in
        ai_stop_orders.close_client_order_id). No parallel order ledger. 终态不等于对齐
        (2026-10-10 审查)：回读 FINISHED 但成交量还没被成交流观察到（回报在途）的，带着
        fillsPending 标记保留在单子里；一个 RESERVED 但还没拿到委托号的执行中尝试同样不是
        终态（attemptInFlight），发送链路按既有 attempt 恢复逻辑处理，不另建订单账。"""
        with self.store.lock:
            identifiers: set[str] = set()
            legs = self.store.connection.execute(
                """SELECT DISTINCT l.client_order_id FROM strategy_execution_legs l
                   JOIN strategy_execution_attempts a ON a.id = l.attempt_id
                   WHERE a.user_id = ? AND a.trading_account_id = ? AND a.strategy_id = ?
                   AND a.status IN ('RESERVED', 'SUBMITTED', 'PARTIAL', 'UNKNOWN')
                   AND l.client_order_id IS NOT NULL""",
                (user_id, self.account_id, strategy_id)).fetchall()
            identifiers.update(str(row["client_order_id"]) for row in legs)
            attempts = self.store.connection.execute(
                """SELECT client_order_id FROM strategy_execution_attempts
                   WHERE user_id = ? AND trading_account_id = ? AND strategy_id = ?
                   AND status IN ('RESERVED', 'SUBMITTED', 'PARTIAL', 'UNKNOWN')
                   AND client_order_id IS NOT NULL""",
                (user_id, self.account_id, strategy_id)).fetchall()
            for row in attempts:
                identifiers.update(
                    identifier for identifier in str(row["client_order_id"]).split(",") if identifier)
            reserved = self.store.connection.execute(
                """SELECT 1 FROM strategy_execution_attempts
                   WHERE user_id = ? AND trading_account_id = ? AND strategy_id = ?
                   AND status = 'RESERVED' AND client_order_id IS NULL LIMIT 1""",
                (user_id, self.account_id, strategy_id)).fetchone()
            stops = self.store.connection.execute(
                """SELECT close_client_order_id FROM ai_stop_orders
                   WHERE user_id = ? AND trading_account_id = ? AND close_client_order_id IS NOT NULL
                   AND status IN ('TRIGGERING', 'SUBMITTED', 'PARTIAL', 'UNKNOWN')
                   AND json_extract(strategy_metadata_json, '$.strategyId') = ?""",
                (user_id, self.account_id, strategy_id)).fetchall()
            identifiers.update(str(row["close_client_order_id"]) for row in stops)
        orders: list[dict[str, Any]] = []
        if reserved is not None:
            orders.append({"clientOrderId": None, "attemptInFlight": True})
        for identifier in sorted(identifiers):
            try:
                order = self.autonomy.simulation_orders.get(identifier, user_id=user_id)
            except (KeyError, SimulationOrderError, RuntimeError):
                orders.append({"clientOrderId": identifier, "unknown": True})
                continue
            if order.get("submitPhase") in {"ABANDONED", "PREPARED"}:
                continue
            if order.get("remoteStatus") == "FINISHED":
                # FINISHED 只说明网关侧完成；成交还没被事件流观察到之前，这一单仍占着未终态。
                filled = int(order.get("filledVolume") or 0)
                if filled > 0 and filled > self._observed_fill_volume(user_id, identifier):
                    orders.append({**order, "fillsPending": True})
                continue
            orders.append(order)
        return orders

    def _trusted_positions(self) -> list[dict[str, Any]] | None:
        """A positions read recovery / flatness decisions may rely on (2026-10-10 审查): a
        failed read — or a cached/stale account snapshot, where an old empty position list is
        not a trustworthy flat — is None, never “按空仓”, so the fence stays up until the facts
        are trustworthy. The market snapshot is deliberately not required: 休市不挡恢复。
        Other (older) callers keep held_position's lenient semantics."""
        from .strategy_funds import _current_fact
        try:
            account = self.autonomy.data_provider.account_summary("drawdown-positions")
            positions = list(self.autonomy.data_provider.positions() or [])
        except Exception:  # noqa: BLE001 — an unreadable account keeps the fence up
            return None
        if not isinstance(account, dict) or not _current_fact(account):
            return None
        return positions

    def _unreconciled_close_lots(self, strategy: dict[str, Any],
                                 positions: list[dict[str, Any]] | None) -> int | None:
        """对账缺口 (2026-10-10 审查)：本策略保护行的剩余量比账户同方向实际持仓多出的手数。
        多出来的部分就是“持仓已经不在、平仓成交回报还没归属到保护行上”（回报在途），对齐前
        不能当已平完——判平完与恢复都等这一差额归零。持仓事实不可信时返回 None。"""
        user_id, symbol = str(strategy["userId"]), str(strategy["symbol"])
        strategy_id = str(strategy["strategyId"])
        if positions is None:
            return None
        with self.store.lock:
            rows = self.store.connection.execute(
                """SELECT position_direction, SUM(remaining_volume) AS volume FROM ai_stop_orders
                   WHERE user_id = ? AND trading_account_id = ? AND lower(symbol) = lower(?)
                   AND remaining_volume > 0
                   AND json_extract(strategy_metadata_json, '$.strategyId') = ?
                   GROUP BY position_direction""",
                (user_id, self.account_id, symbol, strategy_id)).fetchall()
        gap = 0
        for row in rows:
            account_lots = sum(
                max(0, int(item.get("volume") or 0)) for item in positions
                if isinstance(item, dict) and str(item.get("symbol") or "").casefold() == symbol.casefold()
                and item.get("direction") == str(row["position_direction"]))
            gap += max(0, int(row["volume"] or 0) - account_lots)
        return gap

    def recovery_blockers(self, strategy: dict[str, Any]) -> str | None:
        """回撤恢复（resetDrawdown）的前提核对，与 `canResume` 同一来源：先取得可信持仓
        事实，本策略持仓已平、且没有仍未到终态的委托单。返回 None 表示可以恢复；否则
        返回中文原因。"""
        positions = self._trusted_positions()
        if positions is None:
            return "持仓读取失败或账户快照不新鲜，无法确认本策略已平完，暂不能重置回撤。"
        held = int(self.held_position(strategy, positions)["volume"] or 0)
        if held > 0:
            return f"本策略仍有 {held} 手持仓未平，先完成平仓再重置回撤。"
        unreconciled = self._unreconciled_close_lots(strategy, positions)
        if unreconciled is None:
            return "持仓读取失败，无法确认本策略已平完，暂不能重置回撤。"
        if unreconciled > 0:
            return (f"本策略还有 {unreconciled} 手平仓的成交回报未对齐（持仓已不见、保护量未消耗），"
                    "等回报对齐后再重置回撤。")
        pending = self._strategy_open_orders_pending(
            user_id=str(strategy["userId"]), strategy_id=str(strategy["strategyId"]))
        if pending:
            return "本策略仍有未完成（成交中/未确认/回报未对齐）的委托单，等它们到终态后再重置回撤。"
        return None

    def start_monitoring(self, strategy: dict[str, Any], *, signal_action: str | None = None,
                         session_id: str | None = None, reset_drawdown: bool = False) -> dict[str, Any]:
        """启动 / 显式重置回撤的共用链路 (2026-10-10 #692)：HTTP 的 monitor/start 与内置工具
        start_strategy_monitoring 同一入口、同一状态迁移——显式 resetDrawdown 的前提核对
        （liquidating 拒绝、非停机拒绝、恢复前提失败拒绝）、排期核对、状态迁移、执行方式与
        提醒会话更新、全部成功之后才删停机状态，最后唤醒监控。普通 start 绝不重置。权限、
        会话归属与回放检查由各自入口先行完成。"""
        from .strategy_rules import StrategyRuleError, require_close_schedule
        strategy_id, user_id = str(strategy["strategyId"]), str(strategy["userId"])
        if not self.store.is_execution_owner(account_id=self.account_id, user_id=user_id):
            # 一账户一执行负责人 (4.x)：启动 / 重置回撤是 AI 自主动作的显式入口，只属于负责人——
            # 与 HTTP monitor/start 的 require_execution_owner 同一核对，内置工具不再只看账户
            # 绑定：拥有策略记录但不是执行负责人，一样不能迁移状态或重置停机。
            raise ProposalConflict(
                "not_execution_owner",
                "这个账户的 AI 自主执行由执行负责人管理，只有执行负责人能启动策略监控或重置回撤。")
        if ((strategy.get("definition") or {}).get("plan") or {}).get("reminderOnly"):
            # 提醒页的提醒 (第 6 批): its 执行方式 is 只提醒 and nothing else.
            if signal_action not in (None, "alert"):
                raise ProposalConflict("strategy_reminder_only",
                                       "这是提醒页的提醒，只能“信号来了只提醒”，不出卡、不下单。")
            if strategy.get("signalAction") != "alert":
                signal_action = "alert"
        if reset_drawdown:
            # 回撤停机 (2026-10-09): 重置回撤并恢复 — only after the triggered round's close and
            # cancel confirmations are done; a plain start never resets (a stopped round stays
            # stopped — no new opens, no re-evaluation — until this explicit reset). unavailable
            # (2026-10-10 审查): 净值不可用的一轮不能靠费率恢复时，也可显式重置开新一轮——
            # 前提同样是已平仓且全部委托与成交回报对齐；running 仍拒绝。
            dd = self.store.strategy_drawdown_state(strategy_id=strategy_id, user_id=user_id)
            if dd is not None and str(dd["status"]) == "liquidating":
                raise ProposalConflict(
                    "strategy_drawdown_liquidating",
                    "回撤停机仍在平仓处理中，等平仓和撤单确认完成后才能重置回撤。")
            if dd is None or str(dd["status"]) not in {"stopped", "unavailable"}:
                raise ProposalConflict(
                    "strategy_drawdown_not_stopped",
                    "当前没有停机中的回撤状态，无需重置；直接启动即可。")
            blocker = self.recovery_blockers(strategy)
            if blocker is not None:
                raise ProposalConflict("strategy_drawdown_recovery_precondition", blocker)
        if strategy["status"] != "monitoring":
            try:
                require_close_schedule(self.autonomy.data_provider, strategy["definition"],
                                       str(strategy["symbol"]))
            except StrategyRuleError as error:
                raise ProposalConflict(error.code, str(error)) from error
            # 迁移后的行回赋 (2026-10-10 复审)：不带执行方式/会话的启动（内置工具的普通 start）
            # 之前把迁移前的旧行原样返回——回读还是 ready/paused，与库里不一致。
            strategy = self.proposals.transition_strategy_status(
                strategy_id=strategy_id, user_id=user_id, target="monitoring")
            self.latest_results.pop(strategy_id, None)
        if signal_action is not None or session_id is not None:
            strategy = self.store.set_strategy_signal_action(
                strategy_id, user_id=user_id, signal_action=signal_action, session_id=session_id)
            # A new 执行方式 offers the current signal afresh.
            self.forget_offer(strategy_id, user_id)
        if reset_drawdown:
            # 状态本身在启动链路（排期核对、状态迁移、执行方式更新）全部成功之后才删：
            # 中途任何一步失败，停机围栏原样保留。
            self.store.reset_strategy_drawdown_state(strategy_id=strategy_id, user_id=user_id)
        self.wake()
        return strategy

    def funds_and_drawdown(self, strategy: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """按资金比例定手数 / 回撤停机的读数块 (2026-10-10 #692)：HTTP 的 monitor 载荷与内置
        工具的 status 同一份——the same shared cores the monitor and the send-time recheck judge
        with; the payload only reads, it never reserves anything. Returns (funds, drawdown); a
        strategy without either feature gets (None, None)."""
        user_id, strategy_id = str(strategy["userId"]), str(strategy["strategyId"])
        plan_spec = (strategy.get("definition") or {}).get("plan") or {}
        sizing = plan_spec.get("sizing") or {}
        funds: dict[str, Any] | None = None
        drawdown: dict[str, Any] | None = None
        if sizing.get("mode") == "equity_percent" and sizing.get("equityPercent") is not None:
            from .strategy_funds import live_budget
            try:
                funds = live_budget(
                    self.store, self.autonomy.data_provider,
                    user_id=user_id, strategy_id=strategy_id, symbol=str(strategy["symbol"]),
                    account=None, market=None,
                    fee_settings=getattr(self.autonomy.simulation_orders, "fee_settings", None),
                    gateway=self.autonomy.simulation_orders,
                    equity_percent=float(sizing["equityPercent"]),
                    max_lots=(int(sizing["maxLots"]) if sizing.get("maxLots") is not None else None))
            except Exception:  # noqa: BLE001 — 读数不可用时如实说，不给一个像数的数
                funds = {"equity": None, "equityPercent": float(sizing["equityPercent"]),
                         "usedLots": None, "pendingLots": None, "usedMargin": None, "pendingMargin": None,
                         "remainingBudget": None, "marginPerLot": None,
                         "availableLots": 0, "unavailableReason": "行情或账户数据不可用，暂无法估算预算。",
                         "lots": 0, "feeSource": None}
        if plan_spec.get("maxDrawdownPercent") is not None:
            from .strategy_funds import drawdown_percent
            limit_percent = float(plan_spec["maxDrawdownPercent"])
            dd = self.store.strategy_drawdown_state(strategy_id=strategy_id, user_id=user_id)

            def _float(value: Any) -> float | None:
                try:
                    result = float(value)
                except (TypeError, ValueError):
                    return None
                return result
            drawdown = {
                "status": str(dd["status"]) if dd is not None else "inactive",
                "limitPercent": limit_percent,
                "initialEquity": _float(dd["initialEquity"]) if dd is not None else None,
                "peakEquity": _float(dd["peakEquity"]) if dd is not None else None,
                "currentEquity": _float(dd["currentEquity"]) if dd is not None else None,
                "reason": str(dd["reason"] or "") if dd is not None else None,
                # 本轮净值的费用口径：actual=按真实成交佣金、estimated=按费率估算、
                # unavailable=无费率基准（净值里不含费用，只能当参考）。
                "feeSource": (str(dd["feeSource"]) if dd is not None and dd["feeSource"]
                              else None),
                "curve": list(dd["curve"] or []) if dd is not None else [],
                "canResume": False,
                "resumeBlockedReason": None,
            }
            # 恢复按钮的前提与正式恢复入口（monitor/start 的 resetDrawdown）同一套核对：
            # 只有停机/不可用中、已平仓、且没有未终态委托单（含成交回报对齐）才可恢复；
            # 否则给出中文原因。
            if dd is not None and str(dd["status"]) in {"stopped", "unavailable"}:
                blocker = self.recovery_blockers(strategy)
                drawdown["canResume"] = blocker is None
                drawdown["resumeBlockedReason"] = blocker
            peak, current = drawdown["peakEquity"], drawdown["currentEquity"]
            drawdown["drawdownPercent"] = (round(drawdown_percent(peak, current), 2)
                                           if peak is not None and current is not None else None)
        if funds is not None:
            funds["availableLots"] = int(funds.pop("lots") or 0)
        return funds, drawdown

    async def _drawdown_liquidating(
        self, *, strategy: dict[str, Any], state: dict[str, Any], limit: float,
    ) -> dict[str, Any]:
        """A triggered round making progress: cancel this strategy's unfilled opens (an unknown
        result is queried, never bypassed), fire its armed protection as protective closes
        through the same guarded execution a stop hit takes, and only when the position is gone
        AND every order of the round is terminal report stopped (the instance itself pauses)."""
        from .simulation_orders import order_needs_reconciliation
        from .strategy_funds import round_started_ns, sample_curve, strategy_round_facts
        strategy_id, user_id = str(strategy["strategyId"]), str(strategy["userId"])
        reason = str(state["reason"] or "回撤停机处理中。")
        working = False
        for order in self._strategy_open_orders_pending(user_id=user_id, strategy_id=strategy_id):
            client_id = str(order.get("clientOrderId"))
            if order.get("attemptInFlight") or order.get("unknown") or order.get("fillsPending"):
                # 尚未拿到委托号的执行中预留 / 读不回来的单 / 终态但成交回报未对齐的单：
                # 都按“还有事没落定”保持围栏，沿既有 attempt 恢复与对账路径收敛。
                working = True
                continue
            if order_needs_reconciliation(order):
                try:
                    await self.autonomy.simulation_orders.query(client_id, user_id=user_id)
                except (KeyError, SimulationOrderError, RuntimeError):
                    pass
                working = True
            elif str(order.get("request", {}).get("offset")) == "OPEN" and order.get("displayStatus") in {"已受理", "部分成交"}:
                try:
                    await self.autonomy.simulation_orders.cancel(client_id, user_id=user_id)
                except (KeyError, SimulationOrderError, RuntimeError):
                    pass
                working = True
        # 平仓派发链（触发 → 出口就绪 → 围栏 → 发单）的任何失败不再在本地换算成 blocked——
        # 同一轮清算的异常归类已收敛到 run_once 的责任入口 (#698 复审)：不在某张异常名单里
        # 特判，任何失败都保持轮次与 monitoring、下一轮事实恢复后自动接着清算；已发出的单
        # 由在途扫描与对账继续跟进，不重发。
        result = await self.autonomy.trigger_drawdown_exits(
            user_id=user_id, strategy_id=strategy_id, reason_text=reason)
        positions = self._trusted_positions()
        still_held = (int(self.held_position(strategy, positions)["volume"] or 0)
                      if positions is not None else None)
        unreconciled = self._unreconciled_close_lots(strategy, positions)
        if (result["fired"] or result["blocked"] or working or still_held is None or still_held > 0
                or unreconciled is None or unreconciled > 0
                or self._strategy_open_orders_pending(user_id=user_id, strategy_id=strategy_id)):
            detail = f"{reason}平仓处理中，新开仓已锁定。"
            if unreconciled:
                detail = (f"{reason}还有 {unreconciled} 手平仓的成交回报未对齐，等待对账；"
                          "新开仓已锁定。")
            return {"strategyId": strategy_id, "status": "blocked", "action": "close",
                    "detail": detail}
        # 终态净值按完成成交回读：这一轮自己的已实现盈亏 − 手续费（含平仓滑点后的真实成交价），
        # 不是触发前那一拍的旧值——最终平仓的费用与价差要体现在停机时的净值上。
        final_multiplier: float | None = None
        try:
            snapshot = self.autonomy.data_provider.market_snapshot("drawdown-final", str(strategy["symbol"]))
            value = _finite_decimal(snapshot.get("volumeMultiple"))
            final_multiplier = float(value) if value is not None and value > 0 else None
        except (KeyError, RuntimeError):
            final_multiplier = None
        final = strategy_round_facts(
            self.store, user_id=user_id, strategy_id=strategy_id, symbol=str(strategy["symbol"]),
            account_id=self.account_id, since_ns=round_started_ns(str(state["roundStartedAt"])),
            latest_price=None, multiplier=final_multiplier,
            fee_settings=getattr(self.autonomy.simulation_orders, "fee_settings", None))
        initial = float(Decimal(str(state["initialEquity"])))
        baseline = float(Decimal(str(state["baselinePnl"] or 0)))
        final_equity = initial + float(final["realized"] - final["fees"]) - baseline
        done_reason = (f"{reason}本策略持仓已平、未成交开仓单已撤，策略保持停止；"
                       "要重新开始一轮，用「重置回撤并恢复」。")
        # 终态补最后一个真实净值点 (2026-10-10 审查)：不经一分钟节流直接落点（同一纳秒
        # 时覆盖旧值），图尾与 currentEquity 一致；点数上限照旧。
        final_curve = list(state["curve"] or [])
        final_ns = int(self._round_now().timestamp() * 1_000_000_000)
        point = {"time": final_ns, "equity": round(final_equity, 2)}
        final_curve = ([*final_curve[:-1], point] if final_curve and final_curve[-1]["time"] == final_ns
                       else sample_curve([*final_curve, point]))
        self.store.save_strategy_drawdown_state(
            strategy_id=strategy_id, user_id=user_id, status="stopped", limit_percent=limit,
            initial_equity=state["initialEquity"], peak_equity=state["peakEquity"],
            current_equity=str(round(final_equity, 2)), strategy_digest=state["strategyDigest"],
            round_started_at=str(state["roundStartedAt"]), triggered_at=state["triggeredAt"],
            reason=done_reason, curve=final_curve, trading_account_id=self.account_id,
            baseline_pnl=state["baselinePnl"], fee_source=str(final["feeSource"]))
        self.proposals.transition_strategy_status(strategy_id=strategy_id, user_id=user_id, target="paused")
        await self._session_notice(
            strategy, f"{self._display_name(strategy)} · {strategy['symbol']}：{done_reason}",
            kind="strategy-drawdown")
        return {"strategyId": strategy_id, "status": "stopped", "detail": done_reason}

    async def _schedule_notice(
        self, strategy: dict[str, Any], intent: dict[str, Any], positions: list[dict[str, Any]], trading_day: str,
    ) -> None:
        """收盘前平仓 needs the contract's session schedule. When it is missing while the instance
        holds a position, say so once per trading day (spoken on a live call) instead of silently
        not closing."""
        from .trading_calendar import parse_schedule
        if not (strategy["definition"].get("plan") or {}).get("closeBeforeEndMinutes"):
            return
        if parse_schedule((intent.get("engine") or {}).get("schedule")) is not None:
            return
        symbol = str(strategy["symbol"])
        if not any(isinstance(item, dict) and str(item.get("symbol") or "").casefold() == symbol.casefold()
                   and int(item.get("volume") or 0) > 0 for item in positions):
            return
        text = (f"{self._display_name(strategy)} · {symbol}：拿不到这个合约的交易时段，"
                "今天可能无法按时收盘前平仓，请手动处理。")
        # Once per strategy and trading day, by its claim (#932: a restart or the other slot stays quiet).
        await self._session_notice(strategy, text, kind="strategy-warning",
                                   dedupe_key=f"schedule:{trading_day}")

    async def _pause_notice(
        self, strategy: dict[str, Any], reason: str, trading_day: str, *, entries_only: bool = False,
    ) -> None:
        """主动提醒: an instance paused itself (or stopped opening for the day) — one message in
        its conversation per reason and trading day, spoken on a live call."""
        name = self._display_name(strategy)
        outcome = ("今天不再开新仓，平仓条件和保护继续运行" if entries_only
                   else "已自动暂停；已有持仓不受影响，要继续请在策略面板的运行中点继续")
        text = f"{name} · {strategy['symbol']}：{reason.rstrip('。')}，{outcome}。"
        # Once per strategy, trading day and reason, by its claim (#932: was a per-process set).
        await self._session_notice(strategy, text, kind="strategy-paused",
                                   dedupe_key=f"pause:{trading_day}:{reason}")

    async def _default_signal_alert_sink(self, user_id: str, payload: dict[str, Any]) -> None:
        """The default sink: the transient event to the owner's attached sessions (the client draws
        the annotations and shows the light notice), honouring the notify preference."""
        broker = getattr(self.autonomy, "broker", None)
        if broker is None:
            return
        if not self.store.user_preferences(user_id).notify.strategySignalAlerts:
            return
        for session_id in broker.subscribed_session_ids():
            try:
                if str(self.store.get_session(session_id).get("userId") or "") != user_id:
                    continue
            except KeyError:
                continue
            await broker.publish_transient(
                session_id, kind="result", name="feed.strategy-signal", payload=payload
            )

    async def _announce_signal_change(
        self, *, strategy: dict[str, Any], intent: dict[str, Any], action: str, turn_id: str,
    ) -> None:
        """One proactive alert payload for a signal change: the conclusion, the reference price, and
        the same chart annotations explain_strategy_signal draws. The sink (main) applies the
        notify preference, pushes the transient event to the owner's sessions and speaks one line on
        an active voice call. In a replay the monitor runs on the replay clock, so this follows it."""
        if self.signal_alert_sink is None:
            return
        from .strategy_intents import signal_action_label, signal_annotation_items
        label = signal_action_label(intent, action)
        market = self.autonomy.data_provider.market_snapshot(turn_id, strategy["symbol"])
        annotations = signal_annotation_items(
            action=action, evaluation=intent, intent=intent, market=market
        )
        try:
            bar_seconds = int(int(intent["signalSnapshot"]["barTime"]) // 1_000_000_000)
        except (TypeError, ValueError):
            bar_seconds = None
        price = market.get("price") if isinstance(market, dict) else None
        spoken = f"策略提醒：{strategy['name']} 刚满足{label}条件"
        trigger = (intent.get("signalSnapshot") or {}).get("entryTrigger")
        if trigger and not trigger.get("reached"):
            spoken += f"，挂触价 {trigger['price']}，价格{trigger_words(trigger, action)}才开"
        elif price is not None:
            spoken += f"，参考价 {price}"
        spoken += "，已在图上标注。"
        payload = {
            "strategyId": strategy["strategyId"],
            "name": strategy["name"],
            "symbol": strategy["symbol"],
            "action": action,
            "actionLabel": label,
            "volume": int((intent.get("positionIntent") or {}).get("requestedVolume") or 0),
            "barTime": bar_seconds,
            "evaluatedBarTime": (intent.get("signalSnapshot") or {}).get("barTime"),
            "validated": True,
            "annotations": annotations,
            "spoken": spoken,
            "executionMode": "simulation_only",
        }
        try:
            await self.signal_alert_sink(strategy["userId"], payload)
        except Exception:
            LOGGER.debug("strategy_signal_alert_sink_failed", exc_info=True)

    def _session_id(self, user_id: str) -> str:
        existing = self._sessions.get(user_id)
        if existing is not None:
            try:
                self.store.get_session(existing, user_id=user_id)
                return existing
            except KeyError:
                pass
        session = self.autonomy.broker.create_session(
            "local", "strategy-monitor", user_id, trading_account_id=self.account_id,
        )
        self._sessions[user_id] = session.session_id
        return session.session_id
