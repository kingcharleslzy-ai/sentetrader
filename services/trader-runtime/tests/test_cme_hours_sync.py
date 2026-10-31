"""The foreign paper engine keeps its own copy of the CME hours (it cannot import the Runtime): the Runtime's
``trading_calendar`` and the engine's ``cme_hours`` must say the same closures, hours and holiday hours, and give
every tradable foreign product the same hours kind — a holiday added on one side only would let the engine take
orders the Runtime refuses (or the reverse)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from trader_runtime import trading_calendar
from trader_runtime.instruments import FOREIGN_PRODUCTS

ENGINE = Path(__file__).resolve().parents[2] / "tqkq-sidecar" / "src" / "tqkq_readonly_sidecar" / "cme_hours.py"


def _engine_hours():
    spec = importlib.util.spec_from_file_location("engine_cme_hours", ENGINE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_engine_and_the_runtime_keep_the_same_cme_hours():
    engine = _engine_hours()
    assert engine.CME_CLOSED_DAYS == trading_calendar.CME_CLOSED_DAYS
    assert engine.CME_HOURS == trading_calendar.CME_HOURS
    assert engine.CME_HOLIDAY_HOURS == trading_calendar.CME_HOLIDAY_HOURS
    assert engine.DEFAULT_KIND == trading_calendar.DEFAULT_CME_HOURS
    for product in FOREIGN_PRODUCTS:
        if product.mt5:
            assert engine.kind_of(product.mt5) == (product.hours or trading_calendar.DEFAULT_CME_HOURS), product.key
