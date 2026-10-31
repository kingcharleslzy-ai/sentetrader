"""Behaviour of the read-only MT5 bridge against a fake terminal module (stdlib unittest, any OS)."""

from __future__ import annotations

import json
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE / "src"))

from mt5_bridge.server import make_server  # noqa: E402
from mt5_bridge.terminal import ALLOWED_CALLS, ReadOnlyMt5, Terminal, Unavailable  # noqa: E402

TOKEN = "t" * 40
NOW = 1_792_000_000.0


def symbol(name: str, **extra):
    base = dict(
        name=name, description=f"{name} future", path=f"Exchange-Futures\\COMEX\\{name}", exchange="COMEX",
        currency_profit="USD", trade_tick_size=0.1, trade_tick_value=10.0, trade_contract_size=100.0, digits=1,
        volume_min=1.0, volume_step=1.0, margin_initial=12000.0, margin_maintenance=11000.0,
        session_price_settlement=4181.1, session_price_limit_min=0.0, session_price_limit_max=0.0,
        start_time=1_700_000_000, expiration_time=int(NOW) + 86400 * 60, session_volume=1234.0,
        session_open=4170.0, session_close=4181.1, lasthigh=4190.0, lastlow=4165.0, session_interest=50000.0,
    )
    base.update(extra)
    return SimpleNamespace(**base)


class Rates:
    """Shape of the numpy structured array copy_rates_from_pos returns."""

    dtype = SimpleNamespace(names=("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"))

    def __init__(self, rows):
        self._rows = rows

    def tolist(self):
        return self._rows


class FakeMt5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1, TIMEFRAME_D1 = 1, 5, 15, 16385, 16408
    BOOK_TYPE_SELL, BOOK_TYPE_BUY = 1, 2

    def __init__(self):
        self.calls: list[tuple] = []
        self.connected = True
        self.attach_ok = True
        self.symbols = {"GCEZ26": symbol("GCEZ26"), "CLEZ26": symbol("CLEZ26", trade_tick_size=0.01),
                        "GCEQ26": symbol("GCEQ26", expiration_time=int(NOW) - 86400)}

    def _log(self, *call):
        self.calls.append(call)

    def initialize(self, **kwargs):
        self._log("initialize", kwargs)
        return self.attach_ok

    def shutdown(self):
        self._log("shutdown")

    error = (1, "Success")

    def last_error(self):
        return self.error

    def terminal_info(self):
        self._log("terminal_info")
        return SimpleNamespace(connected=self.connected, build=6272, company="AMP Global", ping_last=31_500,
                               trade_allowed=False)

    def account_info(self):
        return SimpleNamespace(server="AMPGlobalUSA-Demo", currency="USD", trade_mode=0)

    def symbols_get(self):
        return tuple(self.symbols.values())

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbol_select(self, name, enable):
        self._log("symbol_select", name)
        return name in self.symbols

    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=4180.5, ask=4180.7, last=4180.6, volume=2, volume_real=2.0,
                               time_msc=int(NOW * 1000) - 250)

    def copy_rates_from_pos(self, name, timeframe, start, count):
        self._log("copy_rates_from_pos", name, timeframe, start, count)
        return Rates([(1_791_999_940, 4180.0, 4181.0, 4179.5, 4180.6, 42, 2, 17)])

    def market_book_add(self, name):
        self._log("market_book_add", name)
        return True

    def market_book_get(self, name):
        return (SimpleNamespace(type=1, price=4180.8, volume_dbl=3.0), SimpleNamespace(type=1, price=4180.7, volume_dbl=1.0),
                SimpleNamespace(type=2, price=4180.5, volume_dbl=2.0), SimpleNamespace(type=2, price=4180.6, volume_dbl=4.0))

    def market_book_release(self, name):
        return True

    # Present on the real module; the bridge must never reach them.
    def order_send(self, request):  # pragma: no cover - reaching this fails the test
        raise AssertionError("trading call reached the terminal")


class TerminalTest(unittest.TestCase):
    def setUp(self):
        self.mt5 = FakeMt5()
        self.running = True
        self.terminal = Terminal(self.mt5, path="C:/mt5/terminal64.exe", process_running=lambda: self.running,
                                 clock=lambda: NOW)

    def test_attaches_with_path_only_and_reports_demo_account(self):
        status = self.terminal.status()
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["account"], {"server": "AMPGlobalUSA-Demo", "currency": "USD", "mode": "demo"})
        self.assertFalse(status["terminal"]["tradeAllowed"])
        init = [call for call in self.mt5.calls if call[0] == "initialize"]
        self.assertEqual(init, [("initialize", {"path": "C:/mt5/terminal64.exe", "timeout": 10000})])

    def test_terminal_not_running_is_unavailable_without_launching_it(self):
        self.running = False
        status = self.terminal.status()
        self.assertEqual((status["status"], status["reason"]), ("unavailable", "terminal_not_running"))
        self.assertFalse(any(call[0] == "initialize" for call in self.mt5.calls))

    def test_disconnected_terminal_is_unavailable_and_recovers_when_connected(self):
        self.mt5.connected = False
        self.assertEqual(self.terminal.status()["reason"], "terminal_disconnected")
        self.mt5.connected = True
        self.assertEqual(self.terminal.status()["status"], "ok")

    def test_attach_failure_is_unavailable(self):
        self.mt5.attach_ok = False
        self.mt5.error = (-10003, "IPC initialize failed")
        status = self.terminal.status()
        self.assertEqual(status["reason"], "terminal_attach_failed")
        self.assertEqual(status["detail"], [-10003, "IPC initialize failed"])

    def test_symbol_properties_carry_multiplier_settlement_and_expiry(self):
        record = self.terminal.symbol("GCEZ26")
        self.assertEqual(record["multiplier"], 100.0)
        self.assertEqual(record["settlement"], 4181.1)
        self.assertIsNone(record["limitMin"])
        self.assertTrue(record["expirationTime"].startswith("2026-"))
        self.assertEqual(self.terminal.symbol("CLEZ26")["multiplier"], 1000.0)

    def test_symbol_list_filters_expired_and_root(self):
        names = [item["symbol"] for item in self.terminal.symbols(root="GC")]
        self.assertEqual(names, ["GCEZ26"])
        names = [item["symbol"] for item in self.terminal.symbols(root="gc", include_expired=True)]
        self.assertEqual(names, ["GCEZ26", "GCEQ26"])

    def test_a_quote_read_that_fails_says_why(self):
        # #932: an IPC failure is a terminal read failure, not "no quote"; MT5's code goes along for diagnosis.
        self.mt5.symbol_info_tick = lambda name: None
        cases = [((-10004, "IPC connect failed"), {"error": "terminal_read_failed", "detail": [-10004, "IPC connect failed"]}),
                 ((-4, "Terminal: Not found"), {"error": "no_quote", "detail": [-4, "Terminal: Not found"]}),
                 ((1, "Success"), {"error": "no_quote"})]
        for error, expected in cases:
            self.mt5.error = error
            row = self.terminal.quotes(["GCEZ26"])["quotes"][0]
            self.assertEqual(row, {"symbol": "GCEZ26", **expected})

    def test_a_selection_that_fails_on_the_ipc_link_is_not_an_unknown_symbol(self):
        self.mt5.error = (-10001, "IPC send failed")
        row = self.terminal.quotes(["GCEZ26", "NOPE"])["quotes"][1]
        self.assertEqual(row, {"symbol": "NOPE", "error": "terminal_read_failed", "detail": [-10001, "IPC send failed"]})

    def test_quotes_select_once_and_mark_unknown(self):
        first = self.terminal.quotes(["GCEZ26", "NOPE"])
        self.terminal.quotes(["GCEZ26"])
        self.assertEqual(first["quotes"][0]["bid"], 4180.5)
        self.assertEqual(first["quotes"][0]["timeMsc"], int(NOW * 1000) - 250)
        self.assertEqual(first["quotes"][1], {"symbol": "NOPE", "error": "unknown_symbol"})
        selects = [call for call in self.mt5.calls if call == ("symbol_select", "GCEZ26")]
        self.assertEqual(len(selects), 1)

    def test_bars_map_timeframe_and_fields(self):
        bars = self.terminal.bars("GCEZ26", "H1", 10)
        self.assertEqual(bars["bars"][0], {"time": 1_791_999_940, "open": 4180.0, "high": 4181.0, "low": 4179.5,
                                           "close": 4180.6, "tickVolume": 42, "volume": 17})
        self.assertIn(("copy_rates_from_pos", "GCEZ26", FakeMt5.TIMEFRAME_H1, 0, 10), self.mt5.calls)

    def test_depth_sorts_sides_best_first(self):
        depth = self.terminal.depth("GCEZ26")
        self.assertEqual(depth["asks"], [[4180.7, 1.0], [4180.8, 3.0]])
        self.assertEqual(depth["bids"], [[4180.6, 4.0], [4180.5, 2.0]])

    def test_failed_reads_are_unavailable_not_empty(self):
        # MT5 returns None on a failed read; that must not look like an empty, fresh answer.
        self.mt5.copy_rates_from_pos = lambda *args: None
        self.mt5.symbols_get = lambda: None
        self.mt5.market_book_get = lambda name: None
        for read in (lambda: self.terminal.bars("GCEZ26", "M1", 10), lambda: self.terminal.symbols(),
                     lambda: self.terminal.depth("GCEZ26")):
            with self.assertRaises(Unavailable) as caught:
                read()
            self.assertEqual(caught.exception.reason, "terminal_read_failed")
        self.mt5.copy_rates_from_pos = lambda *args: Rates([])
        self.assertEqual(self.terminal.bars("GCEZ26", "M1", 10)["bars"], [])

    def test_negative_prices_are_kept_and_zero_is_missing(self):
        self.mt5.symbols["CLEZ26"] = symbol("CLEZ26", session_price_settlement=-37.63, session_price_limit_min=0.0)
        self.mt5.symbol_info_tick = lambda name: SimpleNamespace(bid=-38.0, ask=0.0, last=-37.5, volume=1,
                                                                 volume_real=1.0, time_msc=int(NOW * 1000))
        quote = self.terminal.quotes(["CLEZ26"])["quotes"][0]
        self.assertEqual((quote["bid"], quote["ask"], quote["last"], quote["settlement"]), (-38.0, None, -37.5, -37.63))
        self.assertIsNone(self.terminal.symbol("CLEZ26")["limitMin"])


class ReadOnlyTest(unittest.TestCase):
    TRADING = re.compile(r"\b(order_send|order_check|order_calc\w*|positions?_\w+|orders?_\w+|history_\w+|login)\b")

    def test_allow_list_has_no_trading_or_account_functions(self):
        self.assertEqual([name for name in ALLOWED_CALLS if self.TRADING.search(name)], [])
        with self.assertRaisesRegex(AttributeError, "mt5_call_not_allowed:order_send"):
            ReadOnlyMt5(FakeMt5()).order_send

    def test_service_source_names_no_trading_call(self):
        offenders = [
            f"{path.relative_to(SERVICE)}:{match.group(0)}"
            for path in sorted((SERVICE / "src").rglob("*.py"))
            for match in re.finditer(r"\b(order_send|order_check|order_calc\w*)\b|\blogin\s*\(", path.read_text("utf-8"))
        ]
        self.assertEqual(offenders, [])


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.mt5 = FakeMt5()
        self.running = True
        terminal = Terminal(self.mt5, process_running=lambda: self.running, clock=lambda: NOW)
        self.server = make_server(terminal, TOKEN, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, token=TOKEN, method="GET"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        req = urllib.request.Request(self.base + path, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_every_route_requires_the_token(self):
        for path in ("/v1/status", "/v1/quotes?symbols=GCEZ26", "/nope"):
            self.assertEqual(self.request(path, token=None)[0], 401)
            self.assertEqual(self.request(path, token="x" * 40)[0], 401)

    def test_quotes_bars_and_symbol_over_http(self):
        status, body = self.request("/v1/quotes?symbols=GCEZ26,CLEZ26")
        self.assertEqual(status, 200)
        self.assertEqual([quote["symbol"] for quote in body["quotes"]], ["GCEZ26", "CLEZ26"])
        self.assertEqual(self.request("/v1/symbols/GCEZ26")[1]["multiplier"], 100.0)
        self.assertEqual(self.request("/v1/symbols/NOPE")[0], 404)
        self.assertEqual(len(self.request("/v1/bars?symbol=GCEZ26&timeframe=M5&count=2")[1]["bars"]), 1)
        self.assertEqual(self.request("/v1/bars?symbol=GCEZ26&timeframe=W1")[1], {"error": "timeframe_unsupported"})
        self.assertEqual(self.request("/v1/bars?symbol=GCEZ26&count=0")[0], 400)

    def test_unavailable_terminal_answers_503(self):
        self.running = False
        status, body = self.request("/v1/quotes?symbols=GCEZ26")
        self.assertEqual((status, body["reason"]), (503, "terminal_not_running"))
        self.assertEqual(self.request("/v1/status"), (200, {"status": "unavailable", "reason": "terminal_not_running",
                                                            "detail": None}))

    def test_writes_are_refused(self):
        self.assertEqual(self.request("/v1/quotes", method="POST"), (405, {"error": "read_only"}))

    def test_refuses_non_loopback_bind_and_short_token(self):
        terminal = Terminal(FakeMt5(), process_running=lambda: True)
        with self.assertRaisesRegex(ValueError, "loopback"):
            make_server(terminal, TOKEN, "0.0.0.0", 0)
        with self.assertRaisesRegex(ValueError, "token_too_short"):
            make_server(terminal, "short", "127.0.0.1", 0)


if __name__ == "__main__":
    unittest.main()
