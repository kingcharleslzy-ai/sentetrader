"""Authenticated, isolated replay runtime and its historical data boundary."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .data_providers import SidecarReadOnlyDataProvider
from .fake_agent import FakeAgentAdapter
from .operations import owned_operation
from .replay_retention import (
    CLOSED_NAME,
    cleanup_replays,
    close_replay_directory,
    closed_record,
    open_replay_directory,
)
from .sidecar_http import SidecarHttpError
from .wiring.accounts import session_account_status_payload

log = logging.getLogger("uvicorn.error.replay")
# The sidecar's step stopped part-way through a minute (#932 B3): the shown snapshot is the last
# whole minute, trading and new orders wait until play/step finishes that minute.
INCOMPLETE_NOTICE = "这一分钟只推进了一部分，已暂停；点单步或播放会先走完这一分钟，不重发委托。"
# A step whose answer was lost (#932 B3): resent with the same id until the sidecar answers.
UNCONFIRMED_NOTICE = "回放这一步还没确认完成，正在重试…（同一步只会执行一次）"
# An older sidecar cannot answer a resent step: the replay stays unconfirmed.
UNCONFIRMED_LEGACY_NOTICE = "回放这一步的结果无法确认，已暂停交易；请退出回放后重新开始。"
# Sidecar errors that settle an advance (#932 B3): the step's recorded outcome
# (replay_command_failed) or a refusal before it was queued (replay_not_ready, an invalid
# request). Every other failure is ambiguous.
DEFINITE_ADVANCE_ERRORS = frozenset({"replay_command_failed", "replay_not_ready", "invalid_replay_request"})
# Backoff between resends of a pending step (seconds).
STEP_RESEND_SECONDS = (0.75, 1.5, 3.0, 6.0)


def replay_account(source: dict[str, Any], commission: float | None = None) -> dict[str, Any]:
    """One replay snapshot's account, one formula (#478). The sidecar's source margin and
    available (frozen orders included) are kept; a custom-fee commission replaces the source's
    once — the difference comes off equity, available and PnL, never off margin — and riskRatio
    and marginRatio are both margin ÷ the resulting equity. An older sidecar sends only
    marginRatio: margin comes back from it and available falls back to equity − margin, which
    cannot know frozen orders (compatibility limit until both sides are updated)."""
    account = dict(source)
    equity = float(account["equity"])
    margin = float(account["margin"]) if account.get("margin") is not None \
        else equity * float(account["marginRatio"]) / 100
    available = float(account["available"]) if account.get("available") is not None else equity - margin
    if commission is not None:
        actual = account.get("commission", 0)
        difference = commission - actual
        equity -= difference
        available -= difference
        account.update(commission=commission, sourceCommission=actual,
                       todayPnl=account["todayPnl"] - difference,
                       todayNetPnl=account.get("todayNetPnl", account["todayPnl"]) - difference)
    ratio = margin / equity * 100 if equity > 0 else 0.0
    account.update(equity=equity, margin=margin, available=available, riskRatio=ratio, marginRatio=ratio)
    return account


class ReplayAdapter(FakeAgentAdapter):
    """No model/tools/news in v1: this is not a fake AI trading session."""
    adapter_id = "historical-replay"

    def runtime_status(self, mode):
        return {**super().runtime_status(mode), "agentConnected": False, "connected": False,
                "agentNotice": "历史回放仅支持手工交易及确定性策略，不连接通用 AI。"}

    async def bootstrap(self, session_id):
        session = self.broker.get_session(session_id)
        if session is None:
            return
        await self.broker.publish(session_id, kind="result", name="session.ready",
                                  payload={"mode": session.mode, "agentId": "native"})
        account = session_account_status_payload(
            self.proposals.store, account_id=str(session.trading_account_id), user_id=session.user_id,
        ) if session.trading_account_id else None
        await self.broker.publish(session_id, kind="result", name="runtime.status",
                                  payload={**self.runtime_status(session.mode), **({"account": account} if account else {})})
        for strategy in self.proposals.store.list_strategy_drafts(user_id=session.user_id, limit=50):
            definition = strategy["definition"]
            if definition.get("schemaVersion") == 2:
                continue  # The dedicated library shows draft/validation state honestly.
            await self.broker.publish(session_id, kind="artifact", name="card.strategy", payload={
                "turnId": str(uuid4()), "strategyId": strategy["strategyId"], "name": strategy["name"],
                "symbol": strategy["symbol"], "version": f"v{strategy['version']}", "status": strategy["status"],
                "summary": "已验证策略的隔离副本，默认暂停。启动只影响本次历史回放。",
                "conditions": [], "template": definition["template"], "timeframeSeconds": definition["timeframeSeconds"],
                "direction": definition["direction"], "riskLimits": definition["riskLimits"],
                "definitionDigest": strategy["definitionDigest"], "validationStatus": "passed",
                "backtestStatus": "not_run", "executionMode": "simulation_only", "createdAt": strategy["createdAt"],
            })

    async def handle_message(self, session_id, text, run_id, *, user_id=None, model_slug=None):
        await self._message(session_id, "assistant", "回放中不连接通用 AI，以免引入未来信息。请使用图表和模拟下单面板。")


class ReplayProvider:
    # Preserve the existing data contract; replay identity is explicit and is
    # never used as a request-supplied freshness bypass.
    mode = "tqkq-readonly"

    def __init__(self, source: SidecarReadOnlyDataProvider, identifier: str):
        self.source, self.identifier = source, identifier
        self.snapshot: dict[str, Any] = {"status": "loading"}
        self.received_at = 0.0
        self.subscribers = set()
        self.fee_schedule = None
        self.entries_paused = False
        self.stopped = False
        self.history: dict[str, dict[int, dict]] = {}
        # An advance whose outcome is unknown (#932 B3: its answer was lost): resent with the same
        # id until the sidecar answers; meanwhile the snapshot is unconfirmed.
        self.pending_step: str | None = None
        self.step_ids_supported = False  # the sidecar reports stepInFlight: it runs an id once

    async def start(self):
        pass

    async def stop(self):
        pass

    def subscribe_status(self, subscriber):
        self.subscribers.add(subscriber)
        return lambda: self.subscribers.discard(subscriber)

    async def command(self, action: str, body: dict | None = None):
        step_id = (body or {}).get("stepId") if action == "advance" else None
        if step_id:
            # Pending from the moment it is sent (#932 B3): only a definite sidecar refusal, or the
            # caller after its post-step work succeeded, settles it.
            self.pending_step = step_id
        try:
            result = await self.source._request_async("POST",
                                            f"/v1/replays/{self.identifier}/{action}", body or {}, timeout=7)
        except (TimeoutError, OSError) as error:
            if action == "advance":
                # Allowlist (#932 B3): only the sidecar's explicit outcome of this step counts as an
                # answer; anything else (replay_command_unknown, 5xx, no response) may hide a step
                # that still runs, so it stays pending and is resent with the same id.
                answered = (isinstance(error, SidecarHttpError) and error.status is not None
                            and (error.payload or {}).get("error") in DEFINITE_ADVANCE_ERRORS)
                if answered:
                    # The sidecar answered: a definite failure (the step may have stopped
                    # part-way, incomplete). Sync the snapshot before the failure is answered, so
                    # nothing in between trades on the previous minute; if even that read fails,
                    # unconfirmed until the next one.
                    if step_id and step_id == self.pending_step:
                        self.pending_step = None
                    try:
                        await self.command("state")
                    except Exception:  # noqa: BLE001
                        self._mark_unconfirmed()
                elif step_id:
                    # No answer: the sidecar may or may not have taken it (#932 B3). Unconfirmed
                    # until the same step id, resent, answers — the sidecar runs an id once.
                    self._mark_unconfirmed()
            raise RuntimeError("replay_sidecar_unavailable") from error
        try:
            return self._accept(action, result, step_id)
        except Exception:
            if step_id:
                # An answer that fails validation or normalization settles nothing (#932 B3).
                self.pending_step = step_id
                self._mark_unconfirmed()
            raise

    def _accept(self, action: str, result: dict, step_id: str | None) -> dict:
        """Validate and normalize a state / advance answer into the snapshot."""
        if action in {"state", "advance"}:
            if result.get("accountType") != "TqSim" or result.get("replayId") != self.identifier:
                raise RuntimeError("invalid_replay_identity")
            self.snapshot, self.received_at = result, time.monotonic()
            self.step_ids_supported = self.step_ids_supported or "stepInFlight" in result
            benches = {}
            for duration, chart in result.get("workbenches", {}).items():
                history = self.history.setdefault(duration, {})
                history.update({int(bar["datetime"]): bar for bar in chart["bars"]})
                ordered = sorted(history)[-20000:]
                self.history[duration] = {stamp: history[stamp] for stamp in ordered}
                benches[duration] = {**chart, "bars": list(self.history[duration].values())}
            self.snapshot = {**result, "workbenches": benches}
            if result.get("status") in {"ready", "finished"}:
                commission = None
                if self.fee_schedule and self.fee_schedule["source"] == "custom":
                    from .trading_costs import review_trades
                    computed = review_trades(result["trades"], self.fee_schedule,
                        result["instrument"]["volumeMultiple"])
                    if computed["unmatched"]:
                        self.snapshot = {"status": "error"}
                        raise RuntimeError("replay_fee_opening_fill_missing")
                    commission = computed["estimatedCommission"]
                # Recompute from the SDK snapshot, never adjust a previous read.
                self.snapshot = {**self.snapshot, "account": replay_account(result["account"], commission)}
            if self.pending_step is not None and self.pending_step != step_id:
                # A state read never settles a pending step (#932 B3): only its own answer, once
                # the caller's post-step work succeeded, does.
                self.snapshot = {**self.snapshot, "status": "unconfirmed"}
            for subscriber in tuple(self.subscribers):
                subscriber(self.runtime_status())
        return result

    def _mark_unconfirmed(self) -> None:
        self.snapshot = {**self.snapshot, "status": "unconfirmed"}
        for subscriber in tuple(self.subscribers):
            subscriber(self.runtime_status())


    def clock(self):
        self._current()
        return datetime.fromisoformat(self.snapshot["clock"])

    def _current(self):
        if self.snapshot.get("status") not in {"ready", "finished"} or time.monotonic() - self.received_at > 15:
            raise RuntimeError("replay_snapshot_unavailable")
        return self.snapshot

    def _metadata(self):
        snapshot = self._current()
        finished = snapshot["status"] == "finished" or self.stopped
        return {"capturedAt": snapshot["clock"], "ageSeconds": 0, "dataStatus": "normal",
                "isCached": False, "liveTrading": "disabled", "replayId": self.identifier,
                "marketSessionStatus": "closed" if finished else "trading"}

    def runtime_status(self):
        current = self.snapshot.get("status") in {"ready", "finished"} and time.monotonic() - self.received_at <= 15
        return {"accountMode": self.mode, "marketDataStatus": "normal" if current else "connecting",
                "accountStatus": "normal" if current else "connecting",
                "tradingGatewayStatus": "read_only" if current else "unavailable", "riskStatus": "normal",
                "simulationTrading": current and self.snapshot["status"] == "ready" and not self.stopped,
                "liveTrading": "disabled", "replayId": self.identifier}

    def account_summary(self, turn_id):
        # margin/available/riskRatio come from replay_account, applied once per snapshot.
        account = copy.deepcopy(self._current()["account"])
        return {**account, **self._metadata(), "turnId": turn_id,
                "updatedAt": "回放时间", "status": "normal", "riskBudgetUsed": 0, "equityCurve": [],
                "insight": "独立历史账户；与快期模拟盘无资金、持仓或委托关联。"}

    def positions(self):
        return copy.deepcopy(self._current()["positions"])

    def account_activity(self):
        snapshot = self._current()
        return {"schemaVersion": 1, **self._metadata(), "ordersInitialized": True,
                "orders": copy.deepcopy(snapshot["orders"]), "trades": copy.deepcopy(snapshot["trades"])}

    def instrument_catalog(self):
        return {"schemaVersion": 1, **self._metadata(), "items": [copy.deepcopy(self._current()["instrument"])]}

    def _symbol(self, symbol):
        actual = self._current()["symbol"]
        if symbol.casefold() not in {actual.casefold(), actual.split(".")[-1].casefold()}:
            raise KeyError(symbol)

    def market_snapshot(self, turn_id, symbol):
        self._symbol(symbol)
        current = self._current()
        market = copy.deepcopy(current["market"])
        # The sidecar's replay export puts no tradingSchedule on the market entry; the day's own
        # workbench quotes carry it. 收盘前提醒在回放里按这份时段判定。
        if market.get("tradingSchedule") is None:
            schedule = next((chart.get("quote", {}).get("tradingSchedule")
                             for chart in current["workbenches"].values()
                             if isinstance(chart.get("quote"), dict)
                             and chart["quote"].get("tradingSchedule") is not None), None)
            if schedule is not None:
                market["tradingSchedule"] = schedule
        return {**market, **self._metadata(), "turnId": turn_id,
                "updatedAt": "回放时间", "trend": "flat", "summary": "分钟报价回放", "priceCurve": []}

    def market_workbench(self, symbol, duration_seconds):
        self._symbol(symbol)
        chart = self._current()["workbenches"].get(str(duration_seconds))
        # A period the sidecar is still backfilling answers with the shared
        # recognizable code, never a 500 and never bars it does not have yet.
        if chart is None or chart.get("loading"):
            raise RuntimeError("market_series_not_ready")
        return {**copy.deepcopy(chart), **self._metadata()}

    async def submit_order_once(self, request):
        self._current()
        return await self.command("submit", request)

    async def query_order(self, client_order_id):
        return await self.command("query", {"clientOrderId": client_order_id})

    async def cancel_order_once(self, client_order_id, command_id):
        return await self.command("cancel", {"clientOrderId": client_order_id, "commandId": command_id})


class ReplayContext:
    def __init__(self, user_id, child, provider, parent=None):
        self.user_id, self.child, self.provider = user_id, child, provider
        # The main runtime that mounted this namespace: user-level surfaces
        # (voice, models, preferences) and replay control live there.
        self.parent = parent
        self.playing, self.speed = False, 1
        self.error: str | None = None
        self.lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self.last_access = time.monotonic()
        self.lifespan = child.router.lifespan_context(child)
        self.task = None
        self.closed = False
        self.sidecar_closed = False
        self.directory_descriptor: int | None = None
        self.closing = False
        self.ending = False
        self.resend_attempts, self.resend_at = 0, 0.0  # pending-step resend backoff (#932 B3)
        self.ended = False
        self.end_orders: dict[tuple[str, str], str] = {}
        self.end_close_ids: set[str] = set()
        self.end_session = None
        child.state.replay_context = self
        tools = getattr(child.state.adapter, "trader_tools", None)
        if tools is not None:
            tools.replay_context = self

    def require_open(self, user_id):
        if self.closed or self.closing or user_id != self.user_id:
            raise HTTPException(404, "replay_not_found")

    async def call_tool(self, user_id, call, *, authorize=None):
        self.require_open(user_id)

        async def check():
            self.require_open(user_id)
            if authorize is not None:
                await authorize()
            self.require_open(user_id)
            self.last_access = time.monotonic()

        return await owned_operation(call, lock=self.lock, authorize=check)

    async def dispatch(self, scope, receive, send, *, authorize=None):
        self.require_open(self.user_id)
        self.last_access = time.monotonic()
        # app_call is an envelope: its selected REST operation or native tool
        # acquires this same boundary. Locking the envelope would acquire twice.
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"} and scope["path"] not in {"/api/agent/call", "/api/ui/pulse"}:
            if authorize is None:
                identity = self.child.state.app_capabilities.capture_identity(
                    self.user_id, Request(scope).headers)
                authorize = identity.verify
            # The owner task must not depend on a cancelled HTTP connection for
            # its body or response. Only completed messages go back to the waiter.
            body = await Request(scope, receive).body()
            received = False
            messages = []

            async def buffered_receive():
                nonlocal received
                if not received:
                    received = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()

            async def buffered_send(message):
                messages.append(message)

            await self.call_tool(self.user_id,
                lambda: self.child(scope, buffered_receive, buffered_send), authorize=authorize)
            for message in messages:
                await send(message)
        else:
            if authorize is not None:
                await authorize()
                # A GET may perform bounded order recovery. Keep the original
                # host/session check available when that work later queues.
                scope = {**scope, "ai_trader.replay_authorize": authorize}
            await self.child(scope, receive, send)

    async def start(self):
        await self.lifespan.__aenter__()
        self.task = asyncio.create_task(self._loop())

    async def stop(self, *, detach: bool = False):
        """Stop this process's replay machinery. `detach` (process shutdown, a
        blue-green slot switch) stops the advance loop and leaves the sidecar
        replay alive for the next slot to adopt paused; only an explicit close
        (the user's stop action) ends the sidecar replay itself."""
        self.closing = True
        async with self._stop_lock:
            if self.closed:
                return
            async with self.lock:
                self.closed = True
                self.playing = False
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            if not detach:
                try:
                    result = await self.provider.command("close")
                    self.sidecar_closed = isinstance(result, dict) and result.get("status") == "closed"
                except Exception:
                    # Driver is stopped and old URLs are revoked. The sidecar actor is
                    # paused by construction and expires without a heartbeat.
                    pass
            await self.lifespan.__aexit__(None, None, None)
            close_replay_directory(self.directory_descriptor)
            self.directory_descriptor = None

    async def advance(self):
        # No next quote until protection and strategy execution have consumed
        # this one. Higher speeds do not skip risk evaluations.
        if self.provider.pending_step is not None:
            raise RuntimeError("replay_step_unconfirmed")  # resend the pending step first
        step_id = uuid4().hex
        await self.provider.command("advance", {"stepId": step_id})
        await self._settle_step(step_id)

    async def _settle_step(self, step_id: str) -> None:
        """A step's post-step work; only after it all succeeded is the step settled (#932 B3).
        Any failure keeps it pending and the replay unconfirmed; its resend runs the work again."""
        try:
            await self._after_step()
        except BaseException:
            self.provider.pending_step = step_id
            self.provider._mark_unconfirmed()
            raise
        if self.provider.pending_step == step_id:
            self.provider.pending_step = None

    def adopt_step(self) -> None:
        """Adoption (a deploy's slot switch, #932 B3): an advance the sidecar still has queued or
        running becomes this context's pending step — unconfirmed until its resend answers, then
        the minute's order queries, protection, strategy and reminders run here."""
        self.provider.step_ids_supported = "stepInFlight" in self.provider.snapshot
        step_id = self.provider.snapshot.get("stepInFlight")
        if isinstance(step_id, str):
            self.provider.pending_step = step_id
            self.provider._mark_unconfirmed()

    async def _resend_pending_step(self) -> None:
        """Resend the pending step with its own id (the sidecar runs an id once): an answer is
        its outcome — success runs the post-step work, a refusal ends it; no answer keeps it
        pending. An older sidecar cannot answer by id: nothing is resent (#932 B3)."""
        step_id = self.provider.pending_step
        if step_id is None or not self.provider.step_ids_supported:
            return
        if time.monotonic() < self.resend_at:
            return
        try:
            await self.provider.command("advance", {"stepId": step_id})
            await self._settle_step(step_id)
        except Exception as error:  # noqa: BLE001
            if self.provider.pending_step == step_id:
                # No usable answer, or the post-step work failed: still pending — back off, retry.
                log.warning("replay step %s still unconfirmed: %s", step_id, type(error).__name__)
                self.resend_attempts += 1
                delay = STEP_RESEND_SECONDS[min(self.resend_attempts, len(STEP_RESEND_SECONDS) - 1)]
                self.resend_at = time.monotonic() + delay
                return
            # The sidecar answered the step: a definite failure (the snapshot is synced).
            self.resend_attempts, self.resend_at = 0, 0.0
            return
        self.resend_attempts, self.resend_at = 0, 0.0
        self.error = None  # the "单步结果未确认" notice is resolved

    async def _after_step(self):
        state = self.child.state
        for link in state.simulation_orders.list(user_id=self.user_id):
            if link["submitPhase"] in {"UNKNOWN", "CONFIRMED"}:
                await state.simulation_orders.query(link["clientOrderId"], user_id=self.user_id)
        if self.ending:
            await self.finish()
        else:
            await state.autonomy.run_once(force_reconcile=True)
        if self.provider.snapshot["status"] == "ready" and not self.ending and not self.ended:
            await state.strategy_monitor.run_once()
            await self._remind()
        else:
            self.playing = False
            if self.provider.snapshot["status"] == "finished" and not self.ended:
                await self.finish()

    async def _remind(self):
        """主动提醒跟着回放时钟的推进走 (2026-10-03)：10 步/秒下收盘前窗口与一次性的价格低点
        都只有零点几秒真实时间，等界面的下一次轮询会整段跳过。每推进一步就对到价提醒与
        收盘前/风险度各判一次——与持仓/账户读取的顺风车同一套判定与幂等，谁先到谁触发。
        提醒绝不弄坏推进：任何失败只丢这一轮。"""
        alerts = getattr(self.child.state, "price_alerts", None)
        if alerts is not None:
            try:
                await alerts._tick()
            except Exception as error:  # noqa: BLE001
                log.debug("replay advance price alert check failed: %s", error)
        try:
            from .account_reminders import check_account, check_positions
            state = self.child.state
            provider = state.accounts.default.provider
            positions = await asyncio.to_thread(provider.positions)
            summary = await asyncio.to_thread(provider.account_summary, "")
            await check_positions(self.child, user_id=self.user_id,
                                  account_id=state.accounts.default.account_id,
                                  positions=positions, summary=summary)
            await check_account(self.child, user_id=self.user_id,
                                account_id=state.accounts.default.account_id, summary=summary)
        except Exception as error:  # noqa: BLE001
            log.debug("replay advance reminder check failed: %s", error)

    async def finish(self):
        """Flatten using original order identities until each close is settled."""
        from .order_planner import close_legs
        # The snapshot first (#932 review): an unusable one (incomplete / unconfirmed / stale) refuses
        # the finish before it marks the replay ending.
        snapshot = self.provider._current()
        self.ending = True
        self.provider.entries_paused = True
        state = self.child.state
        if snapshot["status"] == "finished":
            self.error = "行情已结束，剩余持仓按浮盈亏列示，未伪造成交。" if snapshot["positions"] else None
            self.ending, self.ended, self.playing = False, True, False
        else:
            active = [o for o in snapshot["orders"] if o.get("status") == "ALIVE"]
            for order in active:
                identifier = order.get("clientOrderId")
                if identifier and identifier not in self.end_close_ids:
                    # Only runtime-owned orders exist in this isolated account.
                    await state.simulation_orders.cancel(identifier, user_id=self.user_id)
            if active:
                self.playing = True
                return
            unknown = [o for o in state.simulation_orders.list(user_id=self.user_id) if o["submitPhase"] == "UNKNOWN"]
            if unknown:
                self.error = "委托结果未知，停盘平仓已暂停；只查询原委托，不重复提交。"
                self.playing = False
                return
            if self.end_session is None:
                self.end_session = state.broker.create_session("local", "native", self.user_id).session_id
            for direction in ("long", "short"):
                # Everything held, so the 平今平昨 preference cannot change the split (each bucket closes whole).
                plans = close_legs(snapshot["positions"], symbol=snapshot["symbol"], direction=direction,
                                   volume=sum(int(p.get("volume", 0)) for p in snapshot["positions"]
                                              if p["direction"] == direction), preference="auto")
                for bucket, volume in plans:
                    key = (direction, bucket)
                    identifier = self.end_orders.get(key)
                    if identifier:
                        order = state.simulation_orders.get(identifier, user_id=self.user_id)
                        if order["submitPhase"] == "PREPARED":
                            result = await state.autonomy.retry_trade(user_id=self.user_id, client_order_id=identifier)
                            if result["simulationOrder"]["submitPhase"] == "UNKNOWN":
                                self.error = "委托结果未知，停盘平仓已暂停；只查询原委托，不重复提交。"
                                self.playing = False
                                return
                            continue
                        # A query may precede the next account snapshot. Never use
                        # an old position snapshot to create a second close.
                        remote = next((o for o in snapshot["orders"] if o.get("clientOrderId") == identifier), None)
                        fills = sum(int(t["volume"]) for t in snapshot.get("trades", [])
                                    if t.get("clientOrderId") == identifier)
                        if (order["remoteStatus"] != "FINISHED" or not remote
                                or remote.get("status") != "FINISHED" or fills != order["filledVolume"]):
                            continue
                        if order["filledVolume"] == 0:
                            self.error = "停盘平仓委托未成交，已暂停；请检查原委托并手动处理剩余持仓。"
                            self.playing = False
                            return
                        del self.end_orders[key]

                    def record_identity(_, client_order_id, key=key):
                        self.end_orders[key] = client_order_id
                        self.end_close_ids.add(client_order_id)

                    # Reservation starts only when the order service has a
                    # durable identity. Pre-send validation failures remain retryable.
                    try:
                        await state.autonomy.execute_trade(user_id=self.user_id, session_id=self.end_session,
                            symbol=snapshot["symbol"], side="sell_close" if direction == "long" else "buy_close",
                            volume=volume, position_bucket=bucket, price_intent="aggressive", reason="结束历史回放并平仓",
                            before_dispatch=record_identity)
                    except Exception:
                        self.error = "停盘平仓未完成，已暂停；再次处理时会先检查原委托。"
                        self.playing = False
                        raise
                    if self.end_orders.get(key):
                        order = state.simulation_orders.get(self.end_orders[key], user_id=self.user_id)
                        if order["submitPhase"] == "UNKNOWN":
                            self.error = "委托结果未知，停盘平仓已暂停；只查询原委托，不重复提交。"
                            self.playing = False
                            return
            if not snapshot["positions"]:
                self.ending, self.ended, self.playing = False, True, False
            else:
                self.playing = True
        if self.ended:
            self.provider.stopped = True
            for strategy in state.store.list_monitoring_strategies():
                state.proposals.transition_strategy_status(strategy_id=strategy["strategyId"], user_id=self.user_id, target="paused")
            path = state.runtime_state_root / "review.json"
            path.write_text(json.dumps({"snapshot": snapshot, "feeSchedule": self.provider.fee_schedule}, ensure_ascii=False))

    async def _loop(self):
        while True:
            try:
                async with self.lock:
                    if self.closing or self.closed:
                        return
                    if time.monotonic() - self.last_access > 30:
                        self.playing = False
                    if self.provider.pending_step is not None:
                        await self._resend_pending_step()
                    elif self.playing:
                        await self.advance()
                    else:
                        await self.provider.command("state")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.playing, self.error = False, "回放连接中断，已暂停；未自动重发委托。"
            await asyncio.sleep(1 / self.speed if self.playing else 0.75)

    def public(self):
        self.last_access = time.monotonic()
        snapshot = self.provider.snapshot
        return {key: snapshot.get(key) for key in ("replayId", "status", "symbol", "tradingDay", "clock", "precision", "error")} | {
            "playing": self.playing, "speed": self.speed,
            "notice": (INCOMPLETE_NOTICE if snapshot.get("status") == "incomplete"
                       else (UNCONFIRMED_NOTICE if self.provider.step_ids_supported else UNCONFIRMED_LEGACY_NOTICE)
                       if self.provider.pending_step is not None else self.error),
            "entriesPaused": self.provider.entries_paused, "ending": self.ending, "ended": self.ended,
            "feeSchedule": self.provider.fee_schedule,
            "runtimePath": f"/api/replays/{self.provider.identifier}/runtime"}


# User-level, replay-independent surfaces a replay window still needs: the
# voice call (WebSocket included), the model list and its preference, the
# user's preferences, the admin status, and (read-only) the account catalog.
# They are forwarded to the MAIN runtime as the signed-in user; trading,
# market and session data stay on the child.
USER_LEVEL_PATHS = frozenset({
    "/api/voice/realtime", "/api/voice/realtime/status", "/api/voice/realtime/voices",
    "/api/voice/realtime/voice", "/api/voice/realtime/announce", "/api/voice/transcribe", "/api/voice/speak",
    "/api/models/preference", "/api/preferences",
    # 锁屏提醒 (2026-10-06): the user's devices live on the main runtime.
    "/api/app/push", "/api/app/push/subscription", "/api/app/push/test",
})
USER_LEVEL_GET_PATHS = frozenset({"/api/models", "/api/accounts", "/api/auth/owner/status"})


def _zh_day(day: str) -> str:
    """An ISO day as humans say it: 10 月 1 日."""
    picked = datetime.strptime(day, "%Y-%m-%d").date()
    return f"{picked.month} 月 {picked.day} 日"


def replay_day_refusal(trading_day: Any, calendar: dict[str, Any]) -> str | None:
    """The Chinese 422 for a day the exchange was closed or has not finished trading
    (2026-10-03): None when the day may be replayed. The calendar certifies only its
    recent window — a weekday older than that passes and the sidecar's own replay owns
    the deep history. Whether the current trading day has finished is the calendar's
    `latestClosedTradingDay` (the sidecar's 15:30 rule, the one its replay applies; a
    sidecar without the field: not before the 18:00 turn) — #754."""
    day = str(trading_day or "")
    try:
        picked = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return None  # shape errors belong to the request validation
    current = str(calendar.get("tradingDay") or "")
    previous = str(calendar.get("previousTradingDay") or "")
    known = sorted({*(str(item) for item in calendar.get("recentTradingDays") or []),
                    previous, current} - {""})
    if not known or not current or not previous:
        return None  # an unreadable calendar never blocks a replay
    if day == current:
        if str(calendar.get("latestClosedTradingDay") or "") == current:
            return None
        return f"{_zh_day(current)} 的行情还没有走完；最近的已结束交易日是 {_zh_day(previous)}"
    if day > previous:
        # Between the last ended trading day and the current one, a weekday can only
        # be a holiday (consecutive trading days leave no room between them) and a
        # weekend never trades; past the current day the session has not happened yet.
        if picked.weekday() >= 5 or day < current:
            return f"{_zh_day(day)}休市，没有行情；最近的交易日是 {_zh_day(previous)}"
        return f"{_zh_day(day)} 还没有行情；最近的已结束交易日是 {_zh_day(previous)}"
    if day in known:
        return None
    if picked.weekday() >= 5:
        nearest = picked - timedelta(days=1)
        while nearest.weekday() >= 5:
            nearest -= timedelta(days=1)
        return f"{_zh_day(day)}休市，没有行情；最近的交易日是 {_zh_day(nearest.isoformat())}"
    if any(item < day for item in known):
        # A weekday inside the certified window that the exchange did not trade.
        nearest = max(item for item in known if item < day)
        return f"{_zh_day(day)}休市，没有行情；最近的交易日是 {_zh_day(nearest)}"
    return None


def unfinished_day_line(trading_day: Any, calendar: dict[str, Any] | None) -> str:
    """The sidecar's own refusal of a day whose day session has not closed yet, said as a person would:
    which day, why, and what can be picked instead (the calendar's latest ended day when it is earlier)."""
    day = str(trading_day or "")
    try:
        label = _zh_day(day)
    except ValueError:
        label = "这一天"
    previous = str((calendar or {}).get("previousTradingDay") or "")
    try:
        option = f"可以选 {_zh_day(previous)} 或更早的交易日" if previous and previous < day else "可以选更早的交易日"
    except ValueError:
        option = "可以选更早的交易日"
    return f"{label} 的行情还没收盘（白盘 15:00 结束后才能回放），{option}"


def adoptable_public(metadata):
    """The listing shape of a replay this process has not mounted: present so a
    refreshed client finds it again; the next per-replay request adopts it.
    `ended` (review.json on disk, put here by adoptable) keeps a finished replay
    looking finished before adoption too — it is not reopened (R2)."""
    identifier = metadata.get("replayId")
    ended = bool(metadata.get("ended"))
    return {"replayId": identifier, "status": "loading", "symbol": metadata.get("symbol"),
            "tradingDay": metadata.get("tradingDay"), "clock": None, "precision": None, "error": None,
            "playing": False, "speed": 1, "notice": None, "entriesPaused": ended, "ending": False,
            "ended": ended, "feeSchedule": metadata.get("feeSchedule"),
            "runtimePath": f"/api/replays/{identifier}/runtime"}


class ReplayRuntimeRouter:
    # The record a later process adopts a replay from. It sits beside the child's
    # database on the persistent state root and holds ownership and the fee
    # scheme only — never credentials.
    METADATA_NAME = "replay.json"

    def __init__(self, auth, source, state_root: Path, create_child, parent=None):
        self.auth, self.source, self.state_root, self.create_child = auth, source, state_root, create_child
        self.parent = parent
        self.contexts: dict[str, ReplayContext] = {}
        self.lock = asyncio.Lock()

    def _metadata_path(self, identifier) -> Path:
        return self.state_root / "replays" / identifier / self.METADATA_NAME

    def owned(self, identifier, user_id):
        """The mounted context only — for bindings (a voice socket) that must not
        lazily adopt; HTTP surfaces use resolve()."""
        context = self.contexts.get(identifier)
        if context is None:
            raise HTTPException(404, "replay_not_found")
        context.require_open(user_id)
        return context

    def _read_metadata(self, identifier):
        try:
            metadata = json.loads(self._metadata_path(identifier).read_text())
        except (OSError, ValueError):
            return None
        if isinstance(metadata, dict) and metadata.get("replayId") == identifier:
            return metadata
        return None

    def forget(self, identifier, *, confirmed_closed=False):
        """Drop the adoption record; used when the replay is explicitly closed or
        the sidecar no longer has it."""
        path = self._metadata_path(identifier)
        if confirmed_closed and self._read_metadata(identifier) is not None:
            descriptor = open_replay_directory(path.parent)
            try:
                closed_record(path.parent, identifier, now=datetime.now(UTC))
                path.unlink(missing_ok=True)
            finally:
                close_replay_directory(descriptor)
        else:
            path.unlink(missing_ok=True)

    async def cleanup_expired(self, *, now=None, dry_run=False):
        async with self.lock:
            if self.parent is None:
                return {}
            known_tables = {row[0] for row in self.parent.state.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            report = cleanup_replays(self.state_root, known_tables=known_tables, now=now, dry_run=dry_run)
            if "removed" in report.values() or not getattr(self, "_retention_reported", False):
                log.info("replay_retention_sweep dry_run=%s scanned=%s removed=%s", dry_run,
                         len(report), sum(reason == "removed" for reason in report.values()))
                self._retention_reported = True
            return report

    def adoptable(self, user_id):
        """This user's replays that exist on disk but are not mounted here — what
        a refreshed client lists so it can find them again after a deploy."""
        items = []
        for path in sorted((self.state_root / "replays").glob(f"*/{self.METADATA_NAME}")):
            identifier = path.parent.name
            if identifier in self.contexts or re.fullmatch(r"[a-f0-9]{32}", identifier) is None:
                continue
            metadata = self._read_metadata(identifier)
            if metadata is not None and metadata.get("userId") == user_id:
                # review.json on disk marks a replay an earlier process flattened to
                # its end; the listing shows it ended instead of reopening it (R2).
                items.append({**metadata, "ended": (path.parent / "review.json").exists()})
        return items

    async def _probe(self, identifier):
        """Return (state, confirmed_closed). Generic legacy refusals still revoke
        adoption but cannot prove closure: that code also covers malformed input
        and state-read errors. Transport failures decide neither fact."""
        try:
            result = await self.source._request_async("POST", f"/v1/replays/{identifier}/state", {}, timeout=7)
        except SidecarHttpError as error:
            if error.status in {404, 422} and isinstance(error.payload, dict):
                if error.payload.get("error") == "invalid_replay_request":
                    return None, False
            raise  # 401/5xx: the sidecar could not answer — undecided, the record stays
        if (not isinstance(result, dict) or result.get("accountType") != "TqSim"
                or result.get("replayId") != identifier or result.get("status") == "error"):
            raise OSError("invalid_replay_probe")
        if result.get("status") == "closed":
            return None, True
        return result, False

    async def _adoptable_exists(self) -> bool:
        """Any replay the sidecar could still hand back. Transport trouble is
        undecided: report yes and let the create request itself give the verdict."""
        for path in sorted((self.state_root / "replays").glob(f"*/{self.METADATA_NAME}")):
            identifier = path.parent.name
            try:
                state, confirmed_closed = await self._probe(identifier)
                if state is not None:
                    return True
            except OSError:
                return True
            self.forget(identifier, confirmed_closed=confirmed_closed)
        return False

    async def create(self, user_id, body):
        from .trading_costs import FeeSchedule, FeeSettings
        body = dict(body)
        schedule = FeeSchedule.model_validate(body.pop("feeSchedule", FeeSettings(self.state_root).get(user_id))).model_dump()
        if not isinstance(self.source, SidecarReadOnlyDataProvider):
            raise HTTPException(409, "replay_requires_configured_sidecar")
        # A day the exchange did not trade (or has not finished) is refused before the
        # sidecar is asked (2026-10-03): the sidecar's TqSdk calendar is the authority.
        # An unreadable calendar never blocks — a rolling deploy can pair this Runtime
        # with a sidecar that does not serve the calendar yet.
        try:
            calendar = await asyncio.to_thread(self.source.market_calendar)
        except Exception:
            calendar = None
        if calendar is not None:
            refusal = replay_day_refusal(body.get("tradingDay"), calendar)
            if refusal is not None:
                raise HTTPException(422, refusal)
        async with self.lock:
            # One replay per user counts mounted *and* adoptable replays: a slot
            # switch handed the sidecar's replay to this process's future self.
            if self.contexts or await self._adoptable_exists():
                raise HTTPException(409, "replay_already_active")
            try:
                snapshot = await self.source._request_async("POST", "/v1/replays", body, timeout=7)
            except SidecarHttpError as error:
                if error.status == 422 and error.payload.get("reason") == "replay_requires_completed_day":
                    raise HTTPException(422, unfinished_day_line(body.get("tradingDay"), calendar)) from error
                raise HTTPException(409, "replay_start_failed_check_credentials_date") from error
            except Exception as error:
                raise HTTPException(409, "replay_start_failed_check_credentials_date") from error
            identifier = snapshot.get("replayId", "")
            if re.fullmatch(r"[a-f0-9]{32}", identifier) is None or snapshot.get("accountType") != "TqSim":
                raise HTTPException(502, "invalid_replay_identity")
            provider = ReplayProvider(self.source, identifier)
            provider.fee_schedule = schedule
            provider.snapshot = snapshot
            provider.step_ids_supported = "stepInFlight" in snapshot
            root = self.state_root / "replays" / identifier
            root.mkdir(parents=True, exist_ok=True)
            descriptor = open_replay_directory(root)
            try:
                self._write_metadata(identifier, user_id, body, schedule)
                child = self.create_child(root, provider, user_id)
                context = ReplayContext(user_id, child, provider, parent=self.parent)
                context.directory_descriptor = descriptor
                await context.start()
            except BaseException:
                try:
                    await provider.command("close")
                    self.forget(identifier)
                finally:
                    close_replay_directory(descriptor)
                raise
            self.contexts[identifier] = context
            return context.public()

    def _write_metadata(self, identifier, user_id, body, schedule):
        self._metadata_path(identifier).parent.mkdir(parents=True, exist_ok=True)
        (self._metadata_path(identifier).parent / CLOSED_NAME).unlink(missing_ok=True)
        self._metadata_path(identifier).write_text(json.dumps({
            "replayId": identifier, "userId": user_id, "symbol": body.get("symbol"),
            "tradingDay": body.get("tradingDay"), "initialBalance": body.get("initialBalance"),
            "defaultDurationSeconds": body.get("defaultDurationSeconds"), "feeSchedule": schedule,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False))

    async def resolve(self, identifier, user_id) -> ReplayContext:
        """The context for this replay, lazily adopting one an earlier process
        left behind (a deploy's slot switch). Adoption is paused: only an
        explicit play advances the replay again — and a replay whose review.json
        exists comes back ended (R2), never reopened."""
        context = self.contexts.get(identifier)
        if context is not None:
            context.require_open(user_id)
            return context
        metadata = (self._read_metadata(identifier)
                    if re.fullmatch(r"[a-f0-9]{32}", identifier or "") is not None else None)
        if metadata is None or metadata.get("userId") != user_id:
            raise HTTPException(404, "replay_not_found")
        async with self.lock:
            context = self.contexts.get(identifier)
            if context is not None:
                context.require_open(user_id)
                return context
            if self.contexts:
                # One replay per process bounds an adopted replay too.
                raise HTTPException(404, "replay_not_found")
            try:
                state, confirmed_closed = await self._probe(identifier)
            except OSError as error:
                raise HTTPException(404, "replay_not_found") from error
            if state is None:
                # The sidecar replay is gone; its record dies with it.
                self.forget(identifier, confirmed_closed=confirmed_closed)
                raise HTTPException(404, "replay_not_found")
            provider = ReplayProvider(self.source, identifier)
            provider.fee_schedule = metadata.get("feeSchedule")
            provider.snapshot, provider.received_at = state, time.monotonic()
            root = self.state_root / "replays" / identifier
            context = None
            try:
                descriptor = open_replay_directory(root)
            except OSError as error:
                raise HTTPException(404, "replay_not_found") from error
            try:
                # A close in another process may have revoked adoption while we probed.
                if self._read_metadata(identifier) != metadata:
                    raise HTTPException(404, "replay_not_found")
                # The child keeps the database from its first mount; the copied
                # configuration must not be imported a second time.
                child = self.create_child(root, provider, user_id, configure=False)
                context = ReplayContext(user_id, child, provider, parent=self.parent)
                context.directory_descriptor = descriptor
                if (root / "review.json").exists():
                    # An earlier process flattened this replay to its end (R2): adoption
                    # restores the terminal state — ended, entries paused, nothing plays.
                    context.ended, provider.entries_paused, provider.stopped = True, True, True
                else:
                    context.adopt_step()
                await context.start()
            except BaseException:
                if context is not None:
                    await context.stop(detach=True)
                else:
                    close_replay_directory(descriptor)
                raise
            self.contexts[identifier] = context
            return context

    async def close(self):
        """Lifespan shutdown: detach every replay instead of closing it. The old
        slot stops its advance loop (cancelled and joined in stop) and the next
        slot adopts the replay paused, so no two processes ever advance one
        replay; the sidecar replay itself survives the deploy."""
        for context in list(self.contexts.values()):
            await context.stop(detach=True)
        self.contexts.clear()

    async def __call__(self, scope, receive, send):
        # The mount is a namespace, not a proxy to the live runtime. An unknown
        # id, forbidden route, or expired replay never falls back to live APIs.
        websocket = scope["type"] == "websocket"
        auth_scope = scope
        if websocket:
            # A browser cannot set Authorization on a WebSocket: the session token rides the
            # auth subprotocol (the voice handshake's own rule). Present it as the bearer this
            # check reads; the forwarded surface still establishes the call identity itself.
            from .voice_realtime import AUTH_TOKEN_PREFIX
            token = ""
            for key, value in scope.get("headers") or []:
                if key != b"sec-websocket-protocol":
                    continue
                for offered in value.decode("latin-1").split(","):
                    offered = offered.strip()
                    if offered.startswith(AUTH_TOKEN_PREFIX):
                        token = offered[len(AUTH_TOKEN_PREFIX):]
            headers = list(scope.get("headers") or [])
            if token:
                headers.append((b"authorization", f"Bearer {token}".encode()))
            # A websocket scope carries no "method" (and its type is not "http"); the auth's
            # read-only path expects an HTTP scope, so the handshake is authenticated as a GET.
            auth_scope = {**scope, "headers": headers, "type": "http", "method": "GET"}
        request = Request(auth_scope, receive)
        try:
            user_id = self.auth.user(request)
            match = re.search(r"/api/replays/([a-f0-9]{32})/runtime(?P<path>/.*)$", scope["path"])
            if not match:
                raise HTTPException(404, "replay_route_not_found")
            context = await self.resolve(match[1], user_id)
            path = match["path"]
            if self.parent is not None and (
                    path in USER_LEVEL_PATHS
                    or (not websocket and scope.get("method") == "GET" and path in USER_LEVEL_GET_PATHS)):
                # User-level, replay-independent surfaces (the voice call, the
                # model list and preference, preferences, admin status, the
                # account catalog's read-only view) live on the MAIN runtime;
                # the child has none of that state. Still the replay owner's
                # namespace: the voice socket learns which replay to bind to.
                await self.parent({**scope, "path": path, "raw_path": path.encode(), "root_path": "",
                                   "ai_trader.replay_id": match[1]}, receive, send)
                return
            if not path.startswith(("/api/sessions", "/api/events", "/api/runtime/status", "/api/account/", "/api/positions",
                                    "/api/risk/", "/api/instruments", "/api/market", "/api/simulation/", "/api/proposals/", "/api/strategies", "/api/strategy-plans", "/api/trade-review", "/api/ai-execution", "/api/app/", "/api/trading-costs")) and path not in {"/api/agent/account", "/api/agent/capabilities", "/api/agent/call", "/api/ui/pulse"}:
                raise HTTPException(403, "replay_route_unavailable")
            child_scope = {**scope, "path": path, "raw_path": path.encode(), "root_path": ""}
            await context.dispatch(child_scope, receive, send)
        except HTTPException as error:
            if websocket:
                # The same close codes the voice socket itself uses (4401 unauthenticated,
                # 4404 the replay is gone), so a client reads one language everywhere.
                code = {401: 4401, 403: 4403, 404: 4404}.get(error.status_code,
                                                            4400 + min(int(error.status_code), 99))
                await send({"type": "websocket.close", "code": code, "reason": str(error.detail)})
                return
            await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)


def copy_replay_configuration(source, child, user_id, symbol):
    """Copy rules/validated definitions, never positions, orders, leases or PnL."""
    from .strategy_scope import allows_symbol
    store = child.state.store
    policy = source.active_risk_policy(user_id=user_id)
    if policy is not None:
        with store.transaction() as connection:
            store.apply_risk_policy(user_id=user_id, expected_version=0,
                proposal_id=f"replay-import:{policy['policyId']}", rules=policy["rules"],
                rules_digest=policy["rulesDigest"], connection=connection)
    imported_session = None
    for strategy in {item["strategyId"]: item for item in (source.list_strategy_drafts(user_id=user_id, limit=50) + source.list_strategy_library(user_id=user_id))}.values():
        if strategy["definition"].get("libraryStrategyId"):
            continue
        if strategy["definition"].get("schemaVersion") == 2 and allows_symbol(strategy, symbol) and strategy["status"] != "archived":
            if imported_session is None:
                imported_session = child.state.broker.create_session("local", "native", user_id)
            # Validate against this replay's past data; never import a later report.
            store.save_strategy_draft(strategy_id=strategy["strategyId"], user_id=user_id,
                source_session_id=imported_session.session_id, name=strategy["name"], symbol=symbol,
                version=strategy["version"], status="draft", definition=strategy["definition"],
                definition_digest=strategy["definitionDigest"])
            continue
        if strategy["symbol"].casefold() != symbol.casefold() or strategy["status"] not in {"ready", "monitoring", "paused"}:
            continue
        report = source.latest_strategy_validation_report(strategy["strategyId"], user_id=user_id)
        if report is None or report["status"] != "passed" or report["strategyDigest"] != strategy["definitionDigest"]:
            continue
        if imported_session is None:
            imported_session = child.state.broker.create_session("local", "native", user_id)
        store.save_strategy_draft(strategy_id=strategy["strategyId"], user_id=user_id,
            source_session_id=imported_session.session_id, name=strategy["name"], symbol=symbol,
            version=strategy["version"], status="paused", definition=strategy["definition"],
            definition_digest=strategy["definitionDigest"])
        store.save_strategy_validation_report(report_id=report["reportId"], strategy_id=strategy["strategyId"],
            user_id=user_id, strategy_digest=report["strategyDigest"], status="passed", checks=report["checks"],
            evidence={**report["evidence"], "replayImportedValidationAt": report["createdAt"]})
