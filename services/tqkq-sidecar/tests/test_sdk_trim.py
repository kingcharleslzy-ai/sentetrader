"""过期合约表裁剪（2026-10-13 内存调查第 4 项）：只删 2020-09 前已过期的非期货行，期货行原样保留；
TqSdk 版本或表结构不认识时整表不动。表用已安装 TqSdk 自带的同一份文件、同一种读法。"""
from __future__ import annotations

import json
import lzma
import math
import os
import sys
from types import SimpleNamespace

import pytest

from tqkq_readonly_sidecar.sdk_trim import TRIM_TQSDK_VERSION, trim_expired_contract_table

tqsdk = pytest.importorskip("tqsdk")


def bundled_table() -> dict:
    """What TqApi.__init__ puts in `_pre20_ins_info` (api.py: lzma → json.loads)."""
    path = os.path.join(os.path.dirname(os.path.realpath(tqsdk.__file__)), "expired_quotes.json.lzma")
    with lzma.open(path, "rt", encoding="utf-8") as handle:
        return json.loads(handle.read())


def same_row(left: dict, right: dict) -> bool:
    return left.keys() == right.keys() and all(
        (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b)) or a == b
        for a, b in ((left[key], right[key]) for key in left))


def test_the_pinned_sdk_is_the_one_the_trim_was_written_for():
    # The trim turns itself off on any other version: a lock bump must re-read api.py/symbols.py.
    assert tqsdk.__version__ == TRIM_TQSDK_VERSION


def test_trim_drops_expired_non_futures_and_keeps_every_futures_row_unchanged():
    original = bundled_table()
    api = SimpleNamespace(_pre20_ins_info=bundled_table())
    dropped = trim_expired_contract_table(api)
    futures = {symbol: row for symbol, row in original.items() if row["ins_class"] == "FUTURE"}
    assert dropped == len(original) - len(futures) > 10_000
    assert api._pre20_ins_info.keys() == futures.keys() and len(futures) > 2_000
    assert all(same_row(api._pre20_ins_info[symbol], row) for symbol, row in futures.items())
    # TqSdk's own query planner still sends a pre-2020 futures symbol alone (so the
    # contract service's error falls back to this row, symbols.py), as before the trim.
    from tqsdk.utils import _query_for_quote
    old_future = next(iter(futures))
    assert len(_query_for_quote([old_future], api._pre20_ins_info.keys())) == 1
    assert trim_expired_contract_table(api) == 0  # idempotent


@pytest.mark.parametrize("api", [
    SimpleNamespace(),                                                   # attribute gone
    SimpleNamespace(_pre20_ins_info=[]),                                 # not a dict
    SimpleNamespace(_pre20_ins_info={"X.a": {"ins_class": "OPTION"}}),   # no `expired` flag
    SimpleNamespace(_pre20_ins_info={"X.a": "row"}),                     # rows are not dicts
])
def test_an_unknown_table_layout_is_left_alone(api):
    before = getattr(api, "_pre20_ins_info", None)
    assert trim_expired_contract_table(api) == 0
    assert getattr(api, "_pre20_ins_info", None) is before


def test_another_sdk_version_is_left_alone():
    api = SimpleNamespace(_pre20_ins_info={"X.o": {"ins_class": "OPTION", "expired": True}})
    assert trim_expired_contract_table(api, version="3.11.0") == 0
    assert "X.o" in api._pre20_ins_info


def test_live_session_opens_data_only_when_market_only_and_trims_both_ways(monkeypatch):
    """The live session's TqApi: TqKq for a trading child, TqSim (data-only, the TqSdk skill's
    data-access pattern) for a 行情专用 child; either way the expired table is trimmed."""
    built = []

    class FakeApi:
        def __init__(self, account, auth=None, **_):
            self.account_kind = type(account).__name__
            self._pre20_ins_info = {"X.o": {"ins_class": "OPTION", "expired": True},
                                    "X.f": {"ins_class": "FUTURE", "expired": True}}
            built.append(self)

        def get_account(self): return SimpleNamespace()
        def get_position(self): return {}
        def get_order(self): return {}
        def get_trade(self): return {}
        def get_quote(self, symbol): return SimpleNamespace()
        def get_trading_status(self, symbol): return SimpleNamespace(trade_status="")

    fake = SimpleNamespace(
        __version__=TRIM_TQSDK_VERSION, TqApi=FakeApi, TqAuth=lambda *a: a,
        TqKq=type("TqKq", (), {}), TqSim=type("TqSim", (), {}), TqNotify=lambda api: None)
    monkeypatch.setitem(sys.modules, "tqsdk", fake)
    from tqkq_readonly_sidecar.main import OfficialTqKqSession

    trading = OfficialTqKqSession("u", "p", ["SHFE.au2612"])
    market = OfficialTqKqSession("u", "p", ["SHFE.au2612"], market_only=True)
    assert [api.account_kind for api in built] == ["TqKq", "TqSim"]
    assert trading.market_only is False and market.market_only is True
    assert all(set(api._pre20_ins_info) == {"X.f"} for api in built)
