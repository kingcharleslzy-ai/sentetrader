"""规则回测的只读历史取数：独立于实盘会话的 history actor（2026-09-25）。

TqSdk never unsubscribes a serial, so a 10 000-bar history serial on the live TqKq session would
hold one of its MAX_MARKET_SERIES slots until the next idle rebuild — a few backtests would stop
the chart from opening new contracts (the 09-13 incident was the live actor wedging under a
growing subscription count). History therefore runs here, apart from the live session:

* its own thread and its own data-only ``TqApi(TqSim(), auth=TqAuth(...))`` — the pattern the
  TqSdk skill gives for data access (no TqKq account, no trading, no order API is touched);
  a TqApi is created, used and closed on the one thread that owns it, like the replay actor;
* its own subscription cap (``HISTORY_MAX_SERIES``): the next new (symbol, period) past the cap
  closes the API and opens a fresh one, so it is rebuilt with zero subscriptions;
* closed after ``HISTORY_IDLE_SECONDS`` without a request and reopened on demand;
* a failure only fails the history request (with its code); the live session never sees it.

Everything it serves is read-only history, so recycling it at any moment is safe.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .main import (
    CHINA_TIMEZONE,
    HISTORY_DATA_LENGTH,
    detach_klines,
    detach_trading_schedule,
    finite_number,
    tqkq_error_code,
    trading_day_from_calendar,
)

HISTORY_MAX_SERIES = 12
HISTORY_IDLE_SECONDS = 600.0
# How long one request lets a fresh serial download before answering market_series_not_ready
# (the serial stays subscribed; the Runtime asks again and gets it once it is in).
HISTORY_READY_WAIT_SECONDS = 8.0
HISTORY_REQUEST_TIMEOUT_SECONDS = 15.0
# A ready subscription only needs one bounded SDK update cycle before a snapshot.
# Waiting for new data for the full download deadline would stall quiet markets.
HISTORY_UPDATE_WAIT_SECONDS = 0.2


def data_only_api(credentials: tuple[str, str]) -> Any:
    from tqsdk import TqApi, TqAuth, TqSim

    from .sdk_trim import trim_expired_contract_table
    api = TqApi(TqSim(), auth=TqAuth(*credentials), debug=False, disable_print=True)
    trim_expired_contract_table(api)
    return api


class HistoryActor:
    """One thread owning one data-only TqApi, rebuilt at its cap or after idling."""

    def __init__(self, credentials: Callable[[], tuple[str, str]], *,
                 api_factory: Callable[[tuple[str, str]], Any] = data_only_api,
                 max_series: int = HISTORY_MAX_SERIES, idle_seconds: float = HISTORY_IDLE_SECONDS,
                 ready_wait_seconds: float = HISTORY_READY_WAIT_SECONDS) -> None:
        self._credentials = credentials
        self._factory = api_factory
        self.max_series = max_series
        self.idle_seconds = idle_seconds
        self.ready_wait_seconds = ready_wait_seconds
        self._commands: queue.Queue[tuple[dict[str, Any], queue.Queue[dict[str, Any]]] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Owned by the actor thread only.
        self._api: Any = None
        self._serials: dict[tuple[str, int], Any] = {}
        self._quotes: dict[str, Any] = {}
        self._last_request = time.monotonic()
        # Observable counters (tests, /v1/status).
        self.opened = 0
        self.closed = 0

    def status(self) -> dict[str, Any]:
        return {"historySeriesCount": len(self._serials), "historySessionOpen": self._api is not None,
                "historySessionsOpened": self.opened}

    def request(self, symbol: str, duration_seconds: int, length: int,
                timeout: float = HISTORY_REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
        self._ensure_thread()
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._commands.put(({"symbol": symbol, "duration": duration_seconds, "length": length}, response))
        try:
            return response.get(timeout=timeout)
        except queue.Empty:
            return {"errorCode": "market_history_timeout"}

    def close(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None:
            self._commands.put(None)
            thread.join(timeout=5)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="tqsdk-history", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                item = self._commands.get(timeout=0.1 if self._api is not None else 1.0)
            except queue.Empty:
                if self._api is not None and time.monotonic() - self._last_request > self.idle_seconds:
                    self._close_api()
                elif self._api is not None:
                    try:
                        self._api.wait_update(deadline=time.time() + HISTORY_UPDATE_WAIT_SECONDS)
                    except Exception:
                        # A failed data-only session is recreated for the next request.
                        self._close_api()
                continue
            if item is None:
                self._close_api()
                return
            request, response = item
            self._last_request = time.monotonic()
            try:
                result = self._serve(request)
            except KeyError:
                result = {"errorCode": "symbol_not_found"}
            except Exception as error:
                # A broken history API only fails this request; the next one starts a fresh API.
                self._close_api()
                message = str(error)
                code = message if re.fullmatch(r"[a-z_]{3,60}", message) else tqkq_error_code(error)
                result = {"errorCode": "history_session_failed" if code.startswith("tqkq_sidecar_") else code}
            response.put(result)

    def _open_api(self) -> Any:
        if self._api is None:
            credentials = self._credentials()
            if not all(credentials):
                raise RuntimeError("tqkq_credentials_missing")
            self._api = self._factory(credentials)
            self.opened += 1
        return self._api

    def _close_api(self) -> None:
        api, self._api = self._api, None
        self._serials, self._quotes = {}, {}
        if api is not None:
            self.closed += 1
            try:
                api.close()
            except Exception:
                pass

    def _serve(self, request: dict[str, Any]) -> dict[str, Any]:
        symbol, duration, length = str(request["symbol"]), int(request["duration"]), int(request["length"])
        key = (symbol, duration)
        if key not in self._serials and len(self._serials) >= self.max_series:
            self._close_api()   # at the cap: rebuild with zero subscriptions
        api = self._open_api()
        if key not in self._serials:
            self._serials[key] = api.get_kline_serial(symbol, duration, data_length=HISTORY_DATA_LENGTH)
        serial = self._serials[key]
        # All TqSdk references are advanced on this owning thread.  This cycle
        # establishes the request's update boundary even for an already-ready
        # serial; otherwise a repeat read can return its first snapshot forever.
        api.wait_update(deadline=time.time() + HISTORY_UPDATE_WAIT_SECONDS)
        deadline = time.time() + self.ready_wait_seconds
        while not api.is_serial_ready(serial):
            if time.time() >= deadline:
                return {"errorCode": "market_series_not_ready"}
            api.wait_update(deadline=min(deadline, time.time() + 1.0))
        bars = detach_klines(serial, min(length, HISTORY_DATA_LENGTH))
        if not bars:
            raise KeyError(symbol)   # no bars at all: an unknown or never-traded contract
        if symbol not in self._quotes:
            self._quotes[symbol] = api.get_quote(symbol)
        quote = self._quotes[symbol]
        return {"schemaVersion": 1, "symbol": symbol, "durationSeconds": duration,
                "priceTick": finite_number(getattr(quote, "price_tick", None)),
                "volumeMultiple": finite_number(getattr(quote, "volume_multiple", None)),
                "tradingSchedule": detach_trading_schedule(getattr(quote, "trading_time", None)),
                "bars": self._annotate(api, bars)}

    @staticmethod
    def _annotate(api: Any, bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        local = [datetime.fromtimestamp(int(bar["datetime"]) / 1e9, tz=UTC).astimezone(CHINA_TIMEZONE) for bar in bars]
        calendar = api.get_trading_calendar((min(local) - timedelta(days=7)).date(), (max(local) + timedelta(days=21)).date())
        dates = {value.date().isoformat() for value, trading in zip(calendar["date"], calendar["trading"], strict=True)
                 if bool(trading)}
        return [{**bar, "tradingDay": trading_day_from_calendar(moment, dates)} for bar, moment in zip(bars, local, strict=True)]
