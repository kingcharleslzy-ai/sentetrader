"""Keep the import-time Runtime app out of shared and real user state."""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # Fixtures run after collection; main.create_app() already runs on import.
    state = TemporaryDirectory(prefix="ai-trader-pytest-")
    database = Path(state.name) / "ai-trader.sqlite3"
    environment = pytest.MonkeyPatch()
    environment.setenv("AI_TRADER_STATE_ROOT", state.name)
    environment.setenv("AI_TRADER_DB_PATH", str(database))
    # 交易所标准手续费默认走 openctp 抓取；测试不联网，需要缓存的用例自己种
    # exchange-fees.json（见 test_exchange_fees / test_order_estimate）。
    environment.setenv("AI_TRADER_EXCHANGE_FEES_DISABLED", "1")

    def cleanup() -> None:
        try:
            module = sys.modules.get("trader_runtime.main")
            if module is not None and module.app.state.store.database_path == database:
                module.app.state.store.close()
        finally:
            environment.undo()
            state.cleanup()

    config.add_cleanup(cleanup)


def seed_exchange_fees(root: Path, contracts: dict[str, dict[str, Any]], *, fetched_day: str | None = None) -> None:
    """种一份 exchange-fees.json（测试不联网，见上方 DISABLED 开关）。fetched_day 缺省为今天
    （北京时间）→ 新鲜缓存；传旧日期得到标 stale 的表。contracts 的键是 "SHFE.au2612" 形式。"""
    from trader_runtime.research_data import CHINA

    day = fetched_day or datetime.now(CHINA).strftime("%Y-%m-%d")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "exchange-fees.json").write_text(json.dumps(
        {"asOf": "2026-10-08", "fetchedDay": day, "stale": False, "contracts": contracts},
        ensure_ascii=False), encoding="utf-8")


def per_lot_entry(exchange: str, contract: str, product: str, multiplier: float,
                  open_fee: float, close_fee: float, close_today_fee: float) -> dict[str, Any]:
    """种缓存用的每手费率条目（三栏各自独立）。"""
    return {"exchange": exchange, "contract": contract, "product": product, "multiplier": multiplier,
            "rate": {"mode": "per_lot", "open": open_fee, "closeYesterday": close_fee,
                     "closeToday": close_today_fee, "openPerLotExtra": 0.0,
                     "closeYesterdayPerLotExtra": 0.0, "closeTodayPerLotExtra": 0.0}}


@pytest.fixture(autouse=True)
def uvicorn_logs_reach_caplog() -> None:
    """Tests that start a real uvicorn.Server (test_replay_delivery, test_stream_revocation) run
    uvicorn's logging dictConfig: "uvicorn" stops propagating and "uvicorn.error" takes their
    log_level. Later tests on the same xdist worker then lose caplog records from our
    "uvicorn.error.*" module loggers, so each test starts from the propagating default."""
    logging.getLogger("uvicorn").propagate = True
    logging.getLogger("uvicorn.error").setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def isolated_runtime_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Apps created by individual tests also need independent file-backed state.
    monkeypatch.setenv("AI_TRADER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("AI_TRADER_DB_PATH", str(tmp_path / "ai-trader.sqlite3"))


@pytest.fixture(autouse=True)
def unpaced_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scraped hosts are paced ≥ 0.5–1.5 s apart in production (host_throttle); recorded sources
    in tests never wait. The pacing itself is tested on a fake clock in test_host_throttle."""
    from trader_runtime import host_throttle

    async def no_wait(_: float) -> None:
        return None

    for throttle in (host_throttle.EASTMONEY, host_throttle.GENTLE):
        monkeypatch.setattr(throttle, "sleep", lambda _: None)
        monkeypatch.setattr(throttle, "async_sleep", no_wait)
