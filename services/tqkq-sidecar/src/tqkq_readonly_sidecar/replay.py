"""Single-day historical TqSim actor. Never constructs or calls TqKq."""
from __future__ import annotations

import copy
import queue
import re
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from uuid import uuid4

from .main import (
    ACTUAL_FUTURES_SYMBOL,
    ALLOWED_KLINE_DURATIONS,
    CHINA_TIMEZONE,
    KLINE_DATA_LENGTH,
    TRADING_DAY_CLOSED_AT,
    OfficialTqKqSession,
    Snapshot,
    detach_klines,
    detach_quote,
    finite_number,
    tqkq_error_code,
)

# An idle actor wakes this often to check its access timeout (tests shorten it).
IDLE_POLL_SECONDS = 1.0
# One step may take several SDK events (a bar's close, an hour bar closing inside the lunch
# break, the next bar's open); the whole step stays inside the actor call's 5 s answer window.
STEP_SECONDS = 4.0
# Outcomes of this many recent advance step ids are kept for resends (#932 B3).
STEP_RESULTS = 32


class ReplayStepIncomplete(RuntimeError):
    """A step consumed SDK events but ran out of time before the next bar's open (#932 B3).
    TqBacktest cannot undo consumed events: the replay is part-way through a minute, so its
    last exported snapshot is no longer the SDK's state and must not be offered as ready."""

    def __init__(self) -> None:
        super().__init__("replay_advance_incomplete")


def trading_day_start(day: date) -> datetime:
    """When `day`'s trading day begins: 18:00 of the previous weekday — TqBacktest's own rule
    (tqsdk.datetime._get_trading_day_start_time), so a night session belongs to `day`."""
    previous = day - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return datetime.combine(previous, dt_time(18), tzinfo=CHINA_TIMEZONE)


def validate_request(body: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    fields = set(body)
    # defaultDurationSeconds is optional: the period the client is displaying
    # when it starts the replay. The sidecar reaches `ready` once that period's
    # klines are in and keeps loading the rest in the background.
    if not {"symbol", "tradingDay", "initialBalance"} <= fields or fields - {
        "symbol", "tradingDay", "initialBalance", "defaultDurationSeconds",
    }:
        raise ValueError("invalid_replay_fields")
    symbol = body["symbol"]
    if not isinstance(symbol, str) or not ACTUAL_FUTURES_SYMBOL.fullmatch(symbol):
        raise ValueError("invalid_replay_symbol")
    day = date.fromisoformat(body["tradingDay"])
    current = (now or datetime.now(CHINA_TIMEZONE)).astimezone(CHINA_TIMEZONE)
    # A day is replayable once its day session closed (main.TRADING_DAY_CLOSED_AT — the same rule as the
    # calendar summary's latestClosedTradingDay). 2026-10-13 night acceptance: at 21:42 the 10-13 trading
    # day had closed at 15:00, but this check compared the calendar date (day >= today) and refused it.
    if current < datetime.combine(day, TRADING_DAY_CLOSED_AT, tzinfo=CHINA_TIMEZONE):
        raise ValueError("replay_requires_completed_day")
    balance = body["initialBalance"]
    if isinstance(balance, bool) or not isinstance(balance, (int, float)) or not 100_000 <= balance <= 100_000_000:
        raise ValueError("invalid_replay_balance")
    result = {"symbol": symbol, "tradingDay": day.isoformat(), "initialBalance": balance}
    if "defaultDurationSeconds" in fields:
        duration = body["defaultDurationSeconds"]
        if isinstance(duration, bool) or not isinstance(duration, int) or duration not in ALLOWED_KLINE_DURATIONS:
            # An unusable value (0, a foreign period) still opens the replay on the
            # 60s default instead of refusing the whole creation.
            duration = 60
        result["defaultDurationSeconds"] = duration
    return result


class HistoricalSession(OfficialTqKqSession):
    historical = True

    def __init__(self, credentials: tuple[str, str], request: dict[str, Any]):
        from tqsdk import TqApi, TqAuth, TqBacktest, TqSim

        # Startup timing, one stderr line per stage for post-deploy log reading.
        self._timing = {"start": time.monotonic(), "logged": set()}
        self.day = date.fromisoformat(request["tradingDay"])
        self.symbol = request["symbol"]
        self.default_duration = int(request.get("defaultDurationSeconds") or 60)
        # Which period gates `ready` — the production log must be able to answer
        # why a replay waited (one line, before anything can stall).
        self._stage("default_duration", self.default_duration)
        self.sim = TqSim(init_balance=request["initialBalance"])
        self.api = TqApi(
            account=self.sim,
            backtest=TqBacktest(start_dt=self.day, end_dt=self.day),
            auth=TqAuth(*credentials), debug=False, disable_print=True,
        )
        self._stage("api_construct")
        try:
            self.account = self.api.get_account()
            self.position_entities = self.api.get_position()
            self.orders, self.trades = self.api.get_order(), self.api.get_trade()
            self.quotes = {self.symbol: self.api.get_quote(self.symbol)}
            # Subscribe once, before play. Changing the displayed period cannot
            # change replay scheduling or accidentally advance the SDK clock.
            self._kline_serials = {
                (self.symbol, duration): self.api.get_kline_serial(self.symbol, duration, data_length=KLINE_DATA_LENGTH)
                for duration in sorted(ALLOWED_KLINE_DURATIONS)
            }
            self._on_stage = lambda _: None
            self._initial_snapshot_pending = True
            self.orders_initialized, self._observed_order_ids = True, set()
            self._submitted: dict[str, dict[str, Any]] = {}
            self._cancelled: set[str] = set()
            self._calendar: set[str] | None = None
            self._bar_trading_days: dict[int, str] = {}
        except BaseException:
            self.api.close()
            raise

    def _stage(self, stage: str, value: Any = None) -> None:
        timing = getattr(self, "_timing", None)
        if timing is None or stage in timing["logged"]:
            return
        timing["logged"].add(stage)
        if value is None:
            sys.stderr.write(f"replay_timing stage={stage} ms={round((time.monotonic() - timing['start']) * 1000)}\n")
        else:
            sys.stderr.write(f"replay_timing stage={stage} value={value}\n")
        sys.stderr.flush()

    def default_duration_ready(self) -> bool:
        serial = self._kline_serials.get((self.symbol, self.default_duration))
        return serial is not None and self.api.is_serial_ready(serial)

    def pending_durations(self) -> list[int]:
        return [duration for (_, duration), serial in self._kline_serials.items()
                if not self.api.is_serial_ready(serial)]

    def _last_minute_bar(self) -> int | None:
        """Open time (ns) of the newest 1-minute bar the SDK has delivered; None before any."""
        serial = self._kline_serials.get((self.symbol, 60))
        if serial is None or len(serial) == 0:
            return None
        value = finite_number(serial["datetime"].iloc[-1])
        return int(value) if value is not None and value > 0 else None

    def at_first_bar(self) -> bool:
        """Whether the clock stands on the open of the day's first 1-minute bar (the night session's
        when the product has one). The 1-hour serial opens on the hour, so for a 09:30 open (CFFEX
        index futures) the SDK first stops at 09:00 with nothing trading; the ready loop keeps
        reading until this holds. Only delivered data decides it, never a future bar."""
        newest = self._last_minute_bar()
        return newest is not None and newest >= int(trading_day_start(self.day).timestamp()) * 1_000_000_000

    def step(self) -> None:
        """One step = one whole 1-minute bar (owner 2026-10-17): run the SDK through the current
        bar's close and stop on the next bar's open. A minute without trades, the lunch break
        (with the hour bar closing inside it) and the night-to-day gap pass within that one
        step. The day's last step ends in settlement, never past the trading day's end.

        Failure model (#932 B3), one rule for every way a step can stop short — the step budget,
        wait_update answering False, or wait_update raising (SDK or transport): before any event
        the replay is where it was and the original error propagates; after some event it is
        ReplayStepIncomplete. BacktestFinished is the day's end, not a failure: it passes through
        to the actor's finished path. The next step continues from the SDK's current state to the same
        next bar open (the 1-minute serial has not moved): the minute is finished, never skipped."""
        start = self._last_minute_bar()
        deadline = time.time() + STEP_SECONDS
        progressed = False
        while True:
            try:
                if time.time() >= deadline or not self.api.wait_update(deadline=min(deadline, time.time() + 2)):
                    raise RuntimeError("replay_advance_timeout")
            except Exception as error:
                # The SDK raised it, so tqsdk is importable here (tests stand it in).
                from tqsdk import BacktestFinished
                if progressed and not isinstance(error, BacktestFinished):
                    raise ReplayStepIncomplete() from error
                raise
            progressed = True
            # is_changing covers only the latest update: collect after every event of the step.
            self._observed_order_ids.update(str(key) for key in self.orders
                                            if self.api.is_changing(self.orders[key]))
            newest = self._last_minute_bar()
            if (newest is not None and (start is None or newest > start)) or self.day.isoformat() in self.sim.trade_log:
                return

    def read(self, timeout_seconds: float) -> Snapshot | None:
        # Only advance the subscriptions created for this historical contract.
        # The live session's read also starts discovery of today's instruments.
        self.api.wait_update(deadline=time.time() + timeout_seconds)
        return self.snapshot()

    def snapshot_time(self) -> datetime:
        raw = str(self.quotes[self.symbol].datetime)
        parsed = datetime.fromisoformat(raw)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA_TIMEZONE)).astimezone(UTC)

    def _trading_day(self, current: datetime) -> str:
        return self.day.isoformat()

    def _market_session_status(self, symbol: str) -> str:
        return "trading"

    def _trading_dates(self, start: datetime, end: datetime) -> set[str]:
        # Cache the calendar for the bounded warmup window, not today's session.
        if self._calendar is None:
            anchor = datetime.combine(self.day, datetime.min.time(), tzinfo=CHINA_TIMEZONE)
            self._calendar = super()._trading_dates(anchor - timedelta(days=730), anchor + timedelta(days=30))
        return self._calendar

    def export(self) -> dict[str, Any] | None:
        snapshot = self.snapshot()
        if snapshot is None:
            return None
        self._stage("first_quote")
        quote = detach_quote(self.symbol, self.quotes[self.symbol])
        workbenches = {}
        for (_, duration), serial in self._kline_serials.items():
            if not self.api.is_serial_ready(serial):
                # Start-open, backfill: a period still downloading never blocks
                # the whole snapshot; it is marked loading and answers requests
                # with a recognizable state instead of data it does not have.
                workbenches[str(duration)] = {"schemaVersion": 1, "durationSeconds": duration,
                                              "quote": quote, "bars": [], "loading": True}
                continue
            self._stage(f"kline_ready_{duration}")
            bars = self._annotate_trading_days(detach_klines(serial))
            # SDK serials can include warmup history, but never expose a future
            # bar to the workbench or strategy at the current replay clock.
            now_ns = int(self.snapshot_time().timestamp() * 1_000_000_000)
            bars = [bar for bar in bars if int(bar["datetime"]) <= now_ns]
            workbenches[str(duration)] = {"schemaVersion": 1, "durationSeconds": duration,
                                         "quote": quote, "bars": bars}
        account = snapshot.account
        settlement = self.sim.trade_log.get(self.day.isoformat())
        if settlement is not None:
            # TqSim resets its live account fields for the next day during
            # settlement. The completed replay must use that day's official
            # account log, not display zero fees/profit beside changed equity.
            names = {"balance": "balance", "closeProfit": "close_profit", "positionProfit": "position_profit",
                     "floatProfit": "float_profit", "commission": "commission", "margin": "margin",
                     "available": "available"}
            account = {key: finite_number(settlement["account"].get(source)) for key, source in names.items()}
            if any(value is None for value in account.values()):
                raise RuntimeError("replay_settlement_account_unavailable")
        equity = account["balance"]
        pnl = account["closeProfit"] + account["positionProfit"] - account["commission"]
        exchange, instrument = self.symbol.split(".")
        return {
            "clock": snapshot.captured_at,
            "settled": settlement is not None,
            "account": {"accountName": "历史回放 · TqSim 独立账户", "equity": equity,
                        "tradingDay": snapshot.trading_day, "todayPnl": pnl, "todayNetPnl": pnl,
                        "floatProfit": account["floatProfit"], "positionProfit": account["positionProfit"],
                        "closeProfit": account["closeProfit"], "commission": account["commission"],
                        # 来源账本的保证金与可用资金（含挂单冻结）原样带出，#478。
                        "margin": account["margin"], "available": account["available"],
                        "marginRatio": account["margin"] / equity * 100 if equity > 0 else 0,
                        "positionCount": len(snapshot.positions)},
            "positions": snapshot.positions, "orders": snapshot.orders, "trades": snapshot.trades,
            "market": snapshot.markets[self.symbol], "workbenches": workbenches,
            "instrument": {"symbol": self.symbol, "exchangeId": exchange, "instrumentId": instrument,
                           "instrumentName": quote["name"], "productId": instrument.rstrip("0123456789"),
                           "priceTick": quote["priceTick"], "volumeMultiple": quote["volumeMultiple"],
                           "isMain": False},
        }

    def _annotate_trading_days(self, bars):
        # A bar's trading day never changes within this single-day session.
        # Reuse only that calendar annotation, never mutable OHLC/volume data.
        missing = {int(bar["datetime"]): bar for bar in bars
                   if int(bar["datetime"]) not in self._bar_trading_days}
        for bar in super()._annotate_trading_days(list(missing.values())):
            self._bar_trading_days[int(bar["datetime"])] = bar["tradingDay"]
        return [{**bar, "tradingDay": self._bar_trading_days[int(bar["datetime"])]} for bar in bars]

    def command(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        identifier = str(body["clientOrderId"])
        if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", identifier) is None:
            raise ValueError("invalid_replay_order_id")
        if action == "submit":
            required = {"commandId", "clientOrderId", "symbol", "direction", "offset", "volume", "priceType", "limitPrice"}
            price = finite_number(body.get("limitPrice"))
            tick = finite_number(self.quotes[self.symbol].price_tick)
            if (set(body) != required or body["direction"] not in {"BUY", "SELL"}
                    or body["offset"] not in {"OPEN", "CLOSE", "CLOSETODAY"} or body["priceType"] != "LIMIT"
                    or isinstance(body["volume"], bool) or not isinstance(body["volume"], int) or body["volume"] <= 0
                    or price is None or price <= 0 or tick is None or tick <= 0
                    or abs(price / tick - round(price / tick)) > 1e-8):
                raise ValueError("invalid_replay_order")
            if body["symbol"] != self.symbol:
                raise ValueError("replay_symbol_mismatch")
            previous = self._submitted.get(identifier)
            if previous is not None and previous != body:
                raise ValueError("replay_order_identity_conflict")
            if previous is None:
                # Record before calling: an ambiguous SDK error never permits
                # automatic resubmission with the same client order id.
                self._submitted[identifier] = dict(body)
                self.api.insert_order(symbol=self.symbol, direction=body["direction"], offset=body["offset"],
                                      volume=body["volume"], limit_price=body["limitPrice"], order_id=identifier)
        elif action == "cancel":
            if identifier not in self.orders:
                raise ValueError("replay_order_not_found")
            if identifier not in self._cancelled:
                self._cancelled.add(identifier)
                self.api.cancel_order(self.orders[identifier])
        order = self.orders.get(identifier)
        # SDK insertion is only queued while paused, not a confirmed exchange
        # acknowledgement. Ordinary query/reconciliation observes the next step.
        observed = identifier in self._observed_order_ids
        report = self._order_report(order, observed) if order is not None else {"found": False, "ordersInitialized": True}
        if action == "cancel" and report.get("order", {}).get("status") != "FINISHED":
            report["commandStatus"] = "UNKNOWN"
        return report


class ReplayActor:
    def __init__(self, credentials: tuple[str, str], request: dict[str, Any], factory=HistoricalSession):
        self.id = uuid4().hex
        self.request = request
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = {"status": "loading"}
        self._commands: queue.Queue = queue.Queue()
        # Idempotent advance (#932 B3): each step id the caller sends runs once. Its outcome is
        # kept (the last STEP_RESULTS ids); the same id again answers that outcome — the actor's
        # serial queue checks it, so a resend queued behind a running step waits for it. State
        # reads name the advance still queued or stepping (`stepInFlight`), which a slot that
        # adopts the replay resends to learn its outcome.
        self._step_results: dict[str, Exception | None] = {}
        self._step_in_flight: str | None = None
        self._stop = threading.Event()
        self._last_access = time.monotonic()
        self._thread = threading.Thread(target=self._run, args=(credentials, factory), daemon=True,
                                        name=f"historical-replay-{self.id[:8]}")
        self._thread.start()

    def state(self) -> dict[str, Any]:
        self._last_access = time.monotonic()
        with self._lock:
            return {"replayId": self.id, **self.request, "accountType": "TqSim", "liveTrading": "disabled",
                    "precision": "minute-derived-quotes", "priceLimitsModeled": False,
                    "stepInFlight": self._step_in_flight, **copy.deepcopy(self._snapshot)}

    def close(self) -> None:
        self._stop.set()
        self._commands.put(None)

    def call(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # finished answers order queries; incomplete (a step stopped part-way, #932 B3) answers
        # queries and the advance that finishes the minute — no new orders or cancels until the
        # snapshot matches the SDK again.
        step_id = None
        if action == "advance":
            step_id = body.get("stepId") or uuid4().hex
            if not isinstance(step_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", step_id) is None:
                raise ValueError("invalid_replay_step_id")
        with self._lock:
            # A step id already queued, running or answered is a resend: it never steps again.
            known = step_id is not None and (step_id in self._step_results or step_id == self._step_in_flight)
        status = self.state()["status"]
        if not (known or status == "ready" or (status == "finished" and action == "query")
                or (status == "incomplete" and action in {"advance", "query"})):
            raise RuntimeError("replay_not_ready")
        response: queue.Queue = queue.Queue(maxsize=1)
        if step_id is not None:
            with self._lock:
                if not known:
                    self._step_in_flight = step_id
            body = {**body, "stepId": step_id}
        self._commands.put((action, body, response))
        try:
            result = response.get(timeout=5)
        except queue.Empty as error:
            raise RuntimeError("replay_command_unknown") from error
        if isinstance(result, Exception):
            raise RuntimeError("replay_command_failed") from result
        return result

    def _run(self, credentials, factory) -> None:
        session = None
        try:
            session = factory(credentials, self.request)
            credentials = None
            deadline = time.monotonic() + 120
            while not self._stop.is_set():
                session.read(0.5)
                snapshot = session.export()
                # Ready once the quote and the period the client asked to display
                # are in and the clock stands on the day's first 1-minute bar open.
                if snapshot is not None and session.default_duration_ready() and session.at_first_bar():
                    session._stage("ready")
                    with self._lock:
                        self._snapshot = {"status": "ready", **snapshot}
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("replay_initialization_timeout")
            # The SDK moved past the published snapshot but its export failed (#932 B3): the next
            # advance exports first instead of stepping again, so no minute is skipped.
            export_pending = False
            day_ended = False
            # No SDK reads while paused: in TqBacktest every wait_update may consume
            # the next market event, so a paused read would move the clock off the
            # first bar (PR #972 review). A period still marked loading fills in with
            # the next step; in practice none is, since TqBacktest downloads every
            # subscribed serial before its clock leaves the trading-day start.
            if pending := session.pending_durations():
                session._stage("ready_pending", ",".join(map(str, pending)))
            while not self._stop.is_set():
                if time.monotonic() - self._last_access > 600:
                    break
                try:
                    item = self._commands.get(timeout=IDLE_POLL_SECONDS)
                except queue.Empty:
                    continue
                if item is None:
                    break
                action, body, response = item
                try:
                    if action == "advance" and body["stepId"] in self._step_results:
                        # The same step again: its outcome, never a second step.
                        recorded = self._step_results[body["stepId"]]
                        if recorded is not None:
                            raise recorded
                        response.put(self.state())
                    elif action == "advance":
                        # Only advance touches the SDK; dev/test environments
                        # without the tqkq extra can still run this loop. The step's outcome is
                        # a failure until its state is published (#932 B3).
                        outcome: Exception | None = RuntimeError("replay_advance_failed")
                        try:
                            from tqsdk import BacktestFinished
                            if not export_pending:
                                try:
                                    session.step()
                                except BacktestFinished:
                                    day_ended = True  # TqBacktest's end: the day's final export follows
                                export_pending = True
                            snapshot = session.export()
                            if snapshot is None:
                                raise RuntimeError("replay_snapshot_unavailable")
                            with self._lock:
                                finished = day_ended or snapshot.get("settled")
                                self._snapshot = {"status": "finished" if finished else "ready", **snapshot}
                            export_pending, outcome = False, None
                        except ReplayStepIncomplete as error:
                            # Paused part-way through a minute: the last snapshot stays for
                            # display but is no longer ready (old Runtimes trade only on
                            # ready/finished); the next advance finishes the minute.
                            with self._lock:
                                self._snapshot = {**self._snapshot, "status": "incomplete",
                                                  "error": "replay_advance_incomplete"}
                            outcome = error
                            raise
                        except Exception as error:
                            outcome = error
                            if export_pending:
                                # The SDK moved (a whole minute, or to the day's end) but its state
                                # could not be published: same rule as a step stopped part-way; the
                                # next advance exports before stepping again.
                                with self._lock:
                                    self._snapshot = {**self._snapshot, "status": "incomplete",
                                                      "error": tqkq_error_code(error)}
                            raise  # nothing consumed otherwise: the replay is where it was
                        finally:
                            with self._lock:
                                self._step_results[body["stepId"]] = outcome
                                while len(self._step_results) > STEP_RESULTS:
                                    self._step_results.pop(next(iter(self._step_results)))
                                if self._step_in_flight == body["stepId"]:
                                    self._step_in_flight = None
                        response.put(self.state())
                    else:
                        response.put(session.command(action, body))
                except Exception as error:
                    response.put(error)
        except Exception as error:
            with self._lock:
                self._snapshot = {"status": "error", "error": tqkq_error_code(error)}
        finally:
            if session is not None:
                session.close()
            with self._lock:
                if self._snapshot["status"] not in {"error", "finished"}:
                    self._snapshot["status"] = "closed"


class ReplayManager:
    def __init__(self, credentials):
        self.credentials = credentials
        self.actor: ReplayActor | None = None
        self.lock = threading.Lock()

    def create(self, request):
        request = validate_request(request)
        with self.lock:
            if self.actor and self.actor.state()["status"] not in {"closed", "error", "finished"}:
                raise RuntimeError("replay_already_active")
            credentials = self.credentials()
            if not all(credentials):
                raise RuntimeError("replay_credentials_required")
            if self.actor:
                self.actor.close()
            self.actor = ReplayActor(credentials, request)
            return self.actor.state()

    def get(self, identifier):
        with self.lock:
            if self.actor is None or self.actor.id != identifier:
                raise KeyError(identifier)
            return self.actor

    def close(self):
        with self.lock:
            if self.actor:
                self.actor.close()
