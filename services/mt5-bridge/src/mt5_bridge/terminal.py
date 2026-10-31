"""Read-only access to the MetaTrader 5 terminal the owner keeps logged in on the 4090 desktop.

The bridge attaches to the already running terminal (no account, no password, no server
argument) and reads market data only. Every terminal call goes through `ReadOnlyTerminal`,
which exposes an explicit allow-list of data functions; the release preflight
(`scripts/check_desktop_release.mjs`) additionally fails if any file of this service names a
trading function. When the terminal is not running, cannot be attached or has lost its broker
connection, calls raise `Unavailable` and the HTTP layer answers 503 — the bridge never starts,
logs in or reconnects the terminal itself.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

DEFAULT_TERMINAL_PATH = r"C:\Program Files\AMP Global (USA) MT5 Exchange-Traded Futures Only\terminal64.exe"

# The only terminal functions the bridge may call: attach/detach, status, symbol metadata,
# market data and the market-depth subscription. Anything else raises before reaching MT5.
ALLOWED_CALLS = frozenset({
    "initialize", "shutdown", "last_error", "terminal_info", "account_info", "version",
    "symbols_get", "symbol_info", "symbol_info_tick", "symbol_select",
    "copy_rates_from_pos", "market_book_add", "market_book_get", "market_book_release",
})

TIMEFRAMES = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
              "H1": "TIMEFRAME_H1", "D1": "TIMEFRAME_D1"}
ACCOUNT_MODES = {0: "demo", 1: "contest", 2: "real"}
MAX_QUOTE_SYMBOLS = 50
MAX_BARS = 10000
MAX_DEPTH_SUBSCRIPTIONS = 20


class Unavailable(Exception):
    """The terminal cannot serve data right now (reason is a stable code)."""

    def __init__(self, reason: str, detail: Any = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class BadRequest(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ReadOnlyMt5:
    """Wraps the MetaTrader5 module and refuses every attribute outside `ALLOWED_CALLS`."""

    def __init__(self, module: Any):
        self._module = module

    def __getattr__(self, name: str) -> Any:
        if name.startswith("TIMEFRAME_") or name.startswith("BOOK_TYPE_"):
            return getattr(self._module, name)
        if name not in ALLOWED_CALLS:
            raise AttributeError(f"mt5_call_not_allowed:{name}")
        return getattr(self._module, name)


def terminal_process_running() -> bool:
    """True when a terminal64.exe process exists (checked before attaching, because attaching
    with a path would otherwise launch the terminal)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "terminal64.exe" in result.stdout.lower()


def _positive(value: Any) -> float | None:
    """A price field. MT5 reports a missing price as 0.0; any other value, negative included (energy
    futures can trade below zero), is a real price."""
    return float(value) if value else None


def _read(result: Any, mt5: Any) -> Any:
    """MT5 data calls return None on failure (IPC, history not loaded): an error, never empty data."""
    if result is None:
        error = mt5.last_error()
        raise Unavailable("terminal_read_failed", list(error) if error else None)
    return result


IPC_ERRORS_BELOW = -9999  # MT5's RES_E_INTERNAL_FAIL* codes (-10000 … -10005): the terminal link, not the symbol


def _missing_quote(name: str, error: Any) -> dict[str, Any]:
    """One symbol's row in a batch without a quote: a failed terminal read (IPC) says terminal_read_failed,
    anything else no_quote; MT5's own code and text go along as `detail` (diagnosis only, #932)."""
    detail = list(error) if error else None
    code = detail[0] if detail else None
    if isinstance(code, int) and code <= IPC_ERRORS_BELOW:
        return {"symbol": name, "error": "terminal_read_failed", "detail": detail}
    if isinstance(code, int) and code != 1:  # 1 = RES_S_OK: nothing recorded, the symbol simply has no tick
        return {"symbol": name, "error": "no_quote", "detail": detail}
    return {"symbol": name, "error": "no_quote"}


def _unselected_quote(name: str, error: Any) -> dict[str, Any]:
    """A symbol the terminal would not select: unknown, unless the selection itself failed on the IPC link."""
    detail = list(error) if error else None
    if detail and isinstance(detail[0], int) and detail[0] <= IPC_ERRORS_BELOW:
        return {"symbol": name, "error": "terminal_read_failed", "detail": detail}
    return {"symbol": name, "error": "unknown_symbol"}


def _iso(seconds: Any) -> str | None:
    return datetime.fromtimestamp(int(seconds), UTC).isoformat() if seconds else None


def _plain(value: Any) -> Any:
    return value.item() if hasattr(value, "item") else value


def symbol_record(info: Any) -> dict[str, Any]:
    tick_size = float(info.trade_tick_size or 0)
    tick_value = float(info.trade_tick_value or 0)
    return {
        "symbol": info.name,
        "description": info.description,
        "path": info.path,
        "exchange": getattr(info, "exchange", "") or None,
        "currency": info.currency_profit,
        "tickSize": tick_size or None,
        "tickValue": tick_value or None,
        # Value of a 1.0 price move for one contract, in the account currency (USD).
        "multiplier": round(tick_value / tick_size, 10) if tick_size and tick_value else None,
        "contractSize": info.trade_contract_size,
        "digits": info.digits,
        "volumeMin": info.volume_min,
        "volumeStep": info.volume_step,
        "marginInitial": info.margin_initial,
        "marginMaintenance": info.margin_maintenance,
        "settlement": _positive(info.session_price_settlement),
        "limitMin": _positive(info.session_price_limit_min),
        "limitMax": _positive(info.session_price_limit_max),
        "startTime": _iso(info.start_time),
        "expirationTime": _iso(info.expiration_time),
    }


def quote_record(info: Any, tick: Any) -> dict[str, Any]:
    return {
        "symbol": info.name,
        "bid": _positive(tick.bid),
        "ask": _positive(tick.ask),
        "last": _positive(tick.last),
        "lastVolume": tick.volume_real or tick.volume or None,
        # Trade-server clock in epoch milliseconds (AMP's server runs on UTC).
        "timeMsc": int(tick.time_msc) if tick.time_msc else None,
        "sessionVolume": info.session_volume,
        "sessionOpen": _positive(info.session_open),
        "sessionClose": _positive(info.session_close),
        "sessionHigh": _positive(info.lasthigh),
        "sessionLow": _positive(info.lastlow),
        "openInterest": info.session_interest,
        "settlement": _positive(info.session_price_settlement),
    }


def _rows(array: Any) -> list[dict[str, Any]]:
    names = array.dtype.names
    return [dict(zip(names, (_plain(value) for value in row), strict=True)) for row in array.tolist()]


class Terminal:
    """Thread-safe read-only facade; one terminal call at a time (the MT5 IPC is not concurrent)."""

    def __init__(self, module: Any, path: str = DEFAULT_TERMINAL_PATH,
                 process_running: Callable[[], bool] = terminal_process_running,
                 clock: Callable[[], float] = time.time):
        self._mt5 = ReadOnlyMt5(module)
        self._path = path
        self._process_running = process_running
        self._clock = clock
        self._lock = threading.Lock()
        self._attached = False
        self._selected: set[str] = set()
        self._books: set[str] = set()

    # -- attachment -----------------------------------------------------------------------
    def _detach(self) -> None:
        if self._attached:
            try:
                self._mt5.shutdown()
            except Exception:  # detaching a dead IPC link may fail; state resets anyway
                pass
        self._attached = False
        self._selected.clear()
        self._books.clear()

    def _ready(self) -> Any:
        """Returns terminal_info of an attached, broker-connected terminal or raises Unavailable."""
        if self._attached:
            info = self._mt5.terminal_info()
            if info is None:
                self._detach()
        if not self._attached:
            if not self._process_running():
                raise Unavailable("terminal_not_running")
            # Path only: no login, password or server, so it can only attach to the session the
            # owner logged in on the desktop.
            if not self._mt5.initialize(path=self._path, timeout=10000):
                error = self._mt5.last_error()
                self._mt5.shutdown()
                raise Unavailable("terminal_attach_failed", list(error) if error else None)
            self._attached = True
            info = self._mt5.terminal_info()
            if info is None:
                self._detach()
                raise Unavailable("terminal_attach_failed")
        if not info.connected:
            raise Unavailable("terminal_disconnected")
        return info

    def _select(self, symbol: str) -> bool:
        if symbol in self._selected:
            return True
        if self._mt5.symbol_select(symbol, True):
            self._selected.add(symbol)
            return True
        return False

    # -- reads ----------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            try:
                info = self._ready()
            except Unavailable as error:
                return {"status": "unavailable", "reason": error.reason, "detail": error.detail}
            account = self._mt5.account_info()
            return {
                "status": "ok",
                "terminal": {"build": info.build, "company": info.company, "connected": bool(info.connected),
                             "pingMs": round(info.ping_last / 1000, 1) if info.ping_last else None,
                             "tradeAllowed": bool(info.trade_allowed)},
                "account": None if account is None else {
                    "server": account.server, "currency": account.currency,
                    "mode": ACCOUNT_MODES.get(account.trade_mode, str(account.trade_mode))},
                "bridgeTimeMs": int(self._clock() * 1000),
            }

    def symbols(self, path_prefix: str | None = None, root: str | None = None,
                include_expired: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            self._ready()
            now = self._clock()
            items = _read(self._mt5.symbols_get(), self._mt5)
            out = []
            for info in items:
                if path_prefix and not info.path.startswith(path_prefix):
                    continue
                if root and not info.name.upper().startswith(root.upper()):
                    continue
                if not include_expired and info.expiration_time and info.expiration_time < now:
                    continue
                out.append(symbol_record(info))
            return out

    def symbol(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            self._ready()
            info = self._mt5.symbol_info(name)
            return None if info is None else symbol_record(info)

    def quotes(self, names: Iterable[str]) -> dict[str, Any]:
        wanted = list(dict.fromkeys(name for name in names if name))
        if not wanted:
            raise BadRequest("symbols_required")
        if len(wanted) > MAX_QUOTE_SYMBOLS:
            raise BadRequest("too_many_symbols")
        with self._lock:
            self._ready()
            quotes = []
            for name in wanted:
                if not self._select(name):
                    quotes.append(_unselected_quote(name, self._mt5.last_error()))
                    continue
                info = self._mt5.symbol_info(name)
                # The failing call's own last_error, read before the next call replaces it.
                error = self._mt5.last_error() if info is None else None
                tick = self._mt5.symbol_info_tick(name) if info is not None else None
                if info is not None and tick is None:
                    error = self._mt5.last_error()
                if info is None or tick is None:
                    quotes.append(_missing_quote(name, error))
                    continue
                quotes.append(quote_record(info, tick))
            return {"bridgeTimeMs": int(self._clock() * 1000), "quotes": quotes}

    def bars(self, name: str, timeframe: str, count: int, start: int = 0) -> dict[str, Any]:
        constant = TIMEFRAMES.get(timeframe)
        if constant is None:
            raise BadRequest("timeframe_unsupported")
        if not 1 <= count <= MAX_BARS or start < 0:
            raise BadRequest("count_out_of_range")
        with self._lock:
            self._ready()
            if not self._select(name):
                raise BadRequest("unknown_symbol")
            rates = self._mt5.copy_rates_from_pos(name, getattr(self._mt5, constant), start, count)
            rows = _rows(_read(rates, self._mt5))
            return {"symbol": name, "timeframe": timeframe, "bars": [{
                "time": int(row["time"]), "open": row["open"], "high": row["high"], "low": row["low"],
                "close": row["close"], "tickVolume": row["tick_volume"], "volume": row["real_volume"],
            } for row in rows]}

    def depth(self, name: str) -> dict[str, Any]:
        with self._lock:
            self._ready()
            if not self._select(name):
                raise BadRequest("unknown_symbol")
            if name not in self._books:
                if len(self._books) >= MAX_DEPTH_SUBSCRIPTIONS:
                    raise BadRequest("too_many_depth_subscriptions")
                if not self._mt5.market_book_add(name):
                    raise Unavailable("depth_subscribe_failed")
                self._books.add(name)
            book = _read(self._mt5.market_book_get(name), self._mt5)
            sell, buy = self._mt5.BOOK_TYPE_SELL, self._mt5.BOOK_TYPE_BUY
            asks = sorted(([e.price, e.volume_dbl] for e in book if e.type == sell), key=lambda e: e[0])
            bids = sorted(([e.price, e.volume_dbl] for e in book if e.type == buy), key=lambda e: -e[0])
            return {"symbol": name, "bridgeTimeMs": int(self._clock() * 1000), "bids": bids, "asks": asks}
