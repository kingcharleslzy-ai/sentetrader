"""价差策略第二期 (2026-09-29): the live two-leg executor end to end.

Real monitor / proposals / autonomy / simulation_orders over a fake gateway that can fill, stick,
reject or go unknown per leg, with the fill waits monkeypatched tiny: 两腿同时下单、瘸腿补单一次、
开仓回退 / 平仓搁置 / 结果未知、每秒价差止损止盈、出卡确认（含语音）、开关降级、重启清扫，
以及单腿策略不受影响。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import Counter, deque
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import trader_runtime.spread_execution as spread_execution
from test_simulation_orders import USER, Gateway, NormalProvider, application
from test_strategy_monitor import STRATEGY_ID
from test_strategy_spread import (
    ALWAYS,
    GT_ZERO,
    NEVER,
    SHFE,
    SYMBOL_A,
    SYMBOL_B,
    _plan,
    _walked_bar,
)
from test_voice_realtime import WINDOW, CallState, VoiceRealtimeService, _register_window
from trader_runtime.data_providers import (
    SidecarReadOnlyDataProvider,
    _read_snapshot,
    fake_market_snapshot,
    fake_market_workbench,
)
from trader_runtime.proposals import ProposalConflict
from trader_runtime.simulation_orders import SimulationOrderError
from trader_runtime.strategy_monitor import StrategyMonitorService
from trader_runtime.strategy_workbench import definition_for

FILL = {"status": "FINISHED", "volume_left": 0}          # 提交即全成
REJECT = {"status": "FINISHED", "isError": True}         # 提交即被拒
PARTIAL_FILL = {"status": "ALIVE", "volume_left": 0}     # 已全部成交但状态仍在场上
STUCK = {"status": "ALIVE"}                              # 一直挂在场上不成交


class LiveProvider(NormalProvider):
    """两腿各自的行情: market_snapshot 按合约回显（新鲜、带买卖价与最小变动价位）, 工作台的
    形成中K线尾报价用该腿的 lastPrice; 持仓由测试摆好 todayVolume, 平仓按今昨分桶。"""

    def __init__(self) -> None:
        super().__init__()
        self.day = "2026-09-01"
        self.activity: dict[str, Any] = {"dataStatus": "normal", "isCached": False, "ageSeconds": 0,
                                         "ordersInitialized": True, "orders": [], "trades": []}
        self.position_items: list[dict[str, Any]] = []
        self.walked: dict[str, list[float]] = {}
        self.quotes: dict[str, dict[str, float]] = {
            SYMBOL_A: {"bid": 814.0, "ask": 815.0, "last": 815.0},
            SYMBOL_B: {"bid": 810.0, "ask": 811.0, "last": 811.0},
        }
        self.quote_age: dict[str, float] = {}

    def account_activity(self) -> dict[str, Any]:
        return self.activity

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return {**super().account_summary(turn_id), "tradingDay": self.day}

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        quote = self.quotes.get(symbol)
        if quote is None:
            return super().market_snapshot(turn_id, symbol)
        return {**fake_market_snapshot(turn_id, symbol), "symbol": symbol,
                "dataStatus": "normal", "isCached": False, "ageSeconds": self.quote_age.get(symbol, 0.0),
                "quoteDatetime": datetime.now(UTC).isoformat(), "marketSessionStatus": "trading",
                "priceTick": 1.0, "volumeMultiple": 10.0,
                "bidPrice1": quote["bid"], "askPrice1": quote["ask"], "price": quote["last"]}

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        workbench = fake_market_workbench(symbol, duration_seconds)
        bars = list(workbench["bars"])
        for close in self.walked.get(symbol, []):
            bars.append(_walked_bar(bars[-1], close, duration_seconds))
        if self.walked.get(symbol):
            bars.append(_walked_bar(bars[-1], self.walked[symbol][-1], duration_seconds))
        workbench["bars"] = bars
        quote = dict(workbench["quote"])
        quote["tradingSchedule"] = SHFE
        if symbol in self.quotes:
            quote["lastPrice"] = self.quotes[symbol]["last"]
        workbench["quote"] = quote
        if symbol in self.quote_age:
            workbench["ageSeconds"] = self.quote_age[symbol]
        return workbench

    def walk_to(self, symbol: str, close: float) -> None:
        self.walked.setdefault(symbol, []).append(close)

    def hold(self, symbol: str, direction: str, *, volume: int, open_price: float, today: int) -> None:
        self.position_items.append({
            "symbol": symbol, "direction": direction, "volume": volume, "openPrice": open_price,
            "todayVolume": today, "historicalVolume": volume - today, "frozenToday": 0, "frozenHistorical": 0,
        })

    def stale(self, symbol: str, age: float) -> None:
        self.quote_age[symbol] = age


class LaggingProvider(LiveProvider):
    """成交后账户持仓晚几拍才出现: positions() 前几次读数为空, 之后才回真持仓。"""

    def __init__(self, empty_reads: int) -> None:
        super().__init__()
        self._empty_reads_left = empty_reads

    def positions(self) -> list[dict[str, Any]]:
        if self._empty_reads_left > 0:
            self._empty_reads_left -= 1
            return []
        return super().positions()


class SnapshotScopedProvider(LiveProvider, SidecarReadOnlyDataProvider):
    """受控快照行为 (#675): 在 provider_read_snapshot 之内读到新事实（持仓 / 交易日 / 成交
    记录）, 之外读到一份旧的。哪一次读取溜出了快照, 就会看见旧事实而行为不同——出卡与确认
    因此被证明只用同一份快照内的事实。Sidecar 基类只让 provider_read_snapshot 真正走快照
    分支（其内部机制不被使用）。"""

    def __init__(self) -> None:
        super().__init__()                            # LiveProvider 的初始化; Sidecar 内部状态不用
        self.stale_positions: list[dict[str, Any]] = []
        self.stale_day = "1999-12-31"
        self.stale_activity: dict[str, Any] = {
            "dataStatus": "normal", "isCached": False, "ageSeconds": 0,
            "ordersInitialized": True, "orders": [], "trades": []}

    def _in_snapshot(self) -> bool:
        current = _read_snapshot.get()
        return bool(current and current[0] is self)

    def sync_stale(self) -> None:
        """把快照外的事实对齐成快照内的一样（正控准备）。"""
        self.stale_positions = [dict(item) for item in self.position_items]
        self.stale_day = self.day
        self.stale_activity = {**self.activity, "trades": [dict(item) for item in self.activity["trades"]]}

    async def _request_async(self, method, path, payload=None, *, timeout=None):
        if str(path).casefold() == "/v1/activity":
            return self.activity
        if str(path).casefold() == "/v1/snapshot":
            # 同源快照预取 (#700 review): account=True 的 combined 读，读到的是当前事实。
            return {"schemaVersion": 1,
                    "account": {"tradingDay": self.day, "dataStatus": "normal", "ageSeconds": 0.0},
                    "positions": [dict(item) for item in self.position_items],
                    "activity": self.activity}
        return {}

    def positions(self) -> list[dict[str, Any]]:
        if self._in_snapshot():
            return super().positions()
        return [dict(item) for item in self.stale_positions]

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        summary = super().account_summary(turn_id)
        if not self._in_snapshot():
            summary["tradingDay"] = self.stale_day
        return summary

    def account_activity(self) -> dict[str, Any]:
        return self.activity if self._in_snapshot() else self.stale_activity


class GatedSnapshotProvider(SnapshotScopedProvider):
    """/v1/activity 预取停在一条可控制的门上 (#700 review)：重叠的确认各自停在守卫的快照
    await 处，等两条都到齐再放行——真实事件循环下的交错，不靠推导。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None
        self.waiting = 0
        self.on_parked = None                       # 每次一条请求停到门上时同步回调

    async def _request_async(self, method, path, payload=None, *, timeout=None):
        if str(path).casefold() in {"/v1/activity", "/v1/snapshot"} and self.gate is not None:
            self.waiting += 1
            if self.on_parked is not None:
                self.on_parked()
            await self.gate.wait()
        return await super()._request_async(method, path, payload, timeout=timeout)


class BarlessProvider(LiveProvider):
    """B 腿工作台没有K线: 评估抛 strategy_bars_insufficient, 但两腿报价仍然新鲜可读。"""

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        workbench = super().market_workbench(symbol, duration_seconds)
        if symbol == SYMBOL_B:
            workbench["bars"] = []
        return workbench


class MultiGateway(Gateway):
    """两腿在同一网关上并发进出: 每笔委托按 client_order_id 记住请求与当前回报, 新回报按合约排队
    （submit / query / cancel 各一队, 元素是 report 的 kwargs 或要抛的异常）。队列为空时 submit 报
    已受理、query 复述当前状态、cancel 撤成 FINISHED 且剩余不变 —— 即“一直不成交”。"""

    def __init__(self) -> None:
        super().__init__()
        self.requests: dict[str, dict[str, Any]] = {}
        self.order_state: dict[str, dict[str, Any]] = {}
        self.submit_queue: dict[str, deque] = {}
        self.query_queue: dict[str, deque] = {}
        self.cancel_queue: dict[str, deque] = {}
        self.submits_by_symbol: Counter = Counter()

    def on_submit(self, symbol: str, *outcomes: Any) -> None:
        self.submit_queue.setdefault(symbol, deque()).extend(outcomes)

    def on_query(self, symbol: str, *outcomes: Any) -> None:
        self.query_queue.setdefault(symbol, deque()).extend(outcomes)

    def on_cancel(self, symbol: str, *outcomes: Any) -> None:
        self.cancel_queue.setdefault(symbol, deque()).extend(outcomes)

    @staticmethod
    def _pop(queue: dict[str, deque], symbol: str) -> Any:
        items = queue.get(symbol)
        return items.popleft() if items else None

    def _state(self, client_order_id: str, request: dict[str, Any]) -> dict[str, Any]:
        return self.order_state.get(
            client_order_id, {"status": "ALIVE", "volumeLeft": request["volume"], "isError": False})

    def _record(self, client_order_id: str, report: dict[str, Any]) -> dict[str, Any]:
        order = report.get("order")
        if isinstance(order, dict):
            self.order_state[client_order_id] = {key: order[key] for key in ("status", "volumeLeft", "isError")}
        return report

    async def submit_order_once(self, request: dict[str, Any]) -> dict[str, Any]:
        client_order_id = request["clientOrderId"]
        self.requests[client_order_id] = dict(request)
        self.request = dict(request)
        self.submits_by_symbol[request["symbol"]] += 1
        self.submit_count += 1
        outcome = self._pop(self.submit_queue, request["symbol"])
        if isinstance(outcome, BaseException):
            raise outcome
        return self._record(client_order_id, self.report(request, **(outcome or {})))

    async def query_order(self, client_order_id: str) -> dict[str, Any]:
        request = self.requests[client_order_id]
        outcome = self._pop(self.query_queue, request["symbol"])
        if isinstance(outcome, BaseException):
            raise outcome
        self.query_count += 1
        if outcome:
            return self._record(client_order_id, self.report(request, **outcome))
        return self.report(request, **self._state(client_order_id, request))

    async def cancel_order_once(self, client_order_id: str, command_id: str) -> dict[str, Any]:
        assert command_id == f"cancel:{client_order_id}"
        request = self.requests[client_order_id]
        outcome = self._pop(self.cancel_queue, request["symbol"])
        if isinstance(outcome, BaseException):
            raise outcome
        self.cancel_count += 1
        state = self._state(client_order_id, request)
        merged = {"status": "FINISHED", "volumeLeft": state["volumeLeft"], "isError": state["isError"],
                  **(outcome or {})}
        return self._record(client_order_id, self.report(request, **merged))


def _app(path, provider: LiveProvider, gateway: MultiGateway, plan, *, signal_action: str = "auto"):
    app = application(path, gateway, provider)
    store = app.state.store
    store.ensure_default_trading_account(execution_owner_user_id=USER, provider_kind="fake",
                                         display_name="假数据模拟账户", sidecar_url=None)
    store.save_strategy_draft(strategy_id=STRATEGY_ID, user_id=USER, source_session_id="session-spread-live",
                              name="价差实盘测试", symbol=SYMBOL_A, version=1, status="monitoring",
                              definition=definition_for(plan), definition_digest="d" * 64)
    store.save_strategy_validation_report(report_id="report-spread-live", strategy_id=STRATEGY_ID,
                                          user_id=USER, strategy_digest="d" * 64, status="passed",
                                          checks=[], evidence={})
    session = app.state.broker.create_session("local", "native", USER)
    store.set_strategy_signal_action(STRATEGY_ID, user_id=USER, signal_action=signal_action,
                                     session_id=session.session_id)
    monitor = app.state.strategy_monitor
    return SimpleNamespace(app=app, store=store, monitor=monitor, executor=monitor.spread_execution,
                           session_id=session.session_id)


def _events(app, name: str) -> list[dict[str, Any]]:
    return [event["payload"]
            for session in app.state.store.connection.execute("SELECT id FROM sessions").fetchall()
            for _, event in app.state.store.list_events(session[0]) if event.get("name") == name]


def _notices(app, *keywords: str) -> list[str]:
    return [str(item.get("text", "")) for item in _events(app, "message.completed")
            if all(keyword in str(item.get("text", "")) for keyword in keywords)]


def _attempts(ns) -> list[dict[str, Any]]:
    return ns.store.list_strategy_execution_attempts(strategy_id=STRATEGY_ID, user_id=USER)


def _draft_status(ns) -> str:
    return ns.store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"]


def _requests(gateway: MultiGateway, *, offset: str | None = None) -> list[dict[str, Any]]:
    matches = {"CLOSE": {"CLOSE", "CLOSETODAY"}}.get(offset, {offset} if offset else None)
    return [request for request in gateway.requests.values()
            if matches is None or request["offset"] in matches]


@pytest.fixture(autouse=True)
def tiny_waits(monkeypatch):
    monkeypatch.setattr(spread_execution, "SPREAD_FILL_WAIT_SECONDS", 0.01)


def _walk_open_signal(provider: LiveProvider) -> None:
    provider.walk_to(SYMBOL_A, 830.0)
    provider.walk_to(SYMBOL_B, 810.0)                    # 价差收盘 20 > 0：开多条件成立


# -- (1) 自动开仓: 两腿同时下、逐笔记账 ---------------------------------------------------------------


async def test_auto_open_dispatches_both_legs_at_opponent_prices(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-auto.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    _walk_open_signal(provider)

    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "submitted", result
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "SUBMITTED"
    assert attempt["detail"] == "A腿 1 手：模拟委托已成交；B腿 1 手：模拟委托已成交"
    assert attempt["signalAction"] is None and attempt["cardProposalIds"] == []
    assert [leg["status"] for leg in attempt["legs"]] == ["SUBMITTED", "SUBMITTED"]
    assert all(leg["clientOrderId"] for leg in attempt["legs"])
    buys = _requests(gateway, offset="OPEN")
    assert [(r["symbol"], r["direction"], r["volume"], r["limitPrice"]) for r in buys] == \
        [(SYMBOL_A, "BUY", 1, 815.0), (SYMBOL_B, "SELL", 1, 810.0)]   # 买打 ask、卖打 bid
    # 价差两腿不挂各自的保护单: 策略侧的保护是每秒价差止损止盈判断。
    assert ns.store.connection.execute("SELECT COUNT(*) FROM ai_stop_orders").fetchone()[0] == 0
    assert ns.store.count_strategy_attempts_today(
        strategy_id=STRATEGY_ID, user_id=USER, trading_date="2026-09-01") == 1

    again = (await ns.monitor.run_once())[0]              # 同一根已完成K线不重复下单（冷却期挡住）
    assert again["status"] in {"waiting", "duplicate", "blocked"} and gateway.submit_count == 2
    ns.store.close()


# -- (1b) 即时成交: 等待按轮询解除, 不再睡满整段 --------------------------------------------------------


async def test_an_instant_fill_settles_within_a_few_polls_not_the_full_wait(tmp_path, monkeypatch):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-instant.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, PARTIAL_FILL)              # 已全成但仍在场: 要一次回读才见到
    _walk_open_signal(provider)
    monkeypatch.setattr(spread_execution, "SPREAD_FILL_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(spread_execution, "SPREAD_POLL_SECONDS", 0.01)

    assert (await ns.monitor.run_once())[0]["status"] in {"submitted", "partial"}
    await asyncio.wait_for(ns.executor.drain(), timeout=1.0)   # 30 秒等待上限下 1 秒内走完
    assert ns.executor.running(STRATEGY_ID) is False           # running 立刻解除, 每秒止损不再被挡
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "SUBMITTED" and attempt["outcome"] == "filled"
    ns.store.close()


# -- (2) 瘸腿: 一次补单 ------------------------------------------------------------------------------


async def test_a_lame_leg_is_cancelled_and_chased_once_at_the_new_opponent_price(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-chase.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, STUCK, FILL)              # 第一笔 B 悬着, B腿补单全成
    _walk_open_signal(provider)
    await ns.monitor.run_once()
    provider.quotes[SYMBOL_B]["bid"] = 812.0              # 补单按最新对手价
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "SUBMITTED"
    assert "B腿 1 手：模拟委托已撤销" in attempt["detail"]
    assert "B腿补单 1 手：模拟委托已成交" in attempt["detail"]
    assert [r["symbol"] for r in gateway.requests.values()] == [SYMBOL_A, SYMBOL_B, SYMBOL_B]
    chase = list(gateway.requests.values())[-1]
    assert chase["volume"] == 1 and chase["limitPrice"] == 812.0 and chase["direction"] == "SELL"
    assert gateway.cancel_count == 1
    assert _draft_status(ns) == "monitoring"              # 补齐了: 不暂停
    ns.store.close()


# -- (3) 开仓补完仍不成: 回退并暂停 --------------------------------------------------------------------


async def test_an_open_that_never_fills_rolls_the_filled_leg_back_and_pauses(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-rollback.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, FILL, FILL)               # A 开仓全成; 回退平A腿也全成
    provider.hold(SYMBOL_A, "long", volume=1, open_price=815.0, today=1)
    _walk_open_signal(provider)
    await ns.monitor.run_once()
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "CANCELLED"
    assert "回退平A腿 1 手：模拟委托已成交" in attempt["detail"]
    assert "；开仓未成齐已回退：回退完成。" in attempt["detail"]
    rollback = _requests(gateway, offset="CLOSE")
    assert [(r["symbol"], r["direction"], r["volume"]) for r in rollback] == [(SYMBOL_A, "SELL", 1)]
    assert _draft_status(ns) == "paused"
    assert _notices(ns.app, "开仓两腿没有同时成交（A腿成交 1 手）", "已回退已成交部分（回退完成）", "策略已暂停")
    await ns.monitor.run_once()                           # 暂停后不再有任何动作
    assert gateway.submit_count == 4 and gateway.cancel_count == 2   # B 悬单 + B补单 悬单都被撤掉
    ns.store.close()


# -- (3b) 回退不等可能滞后的账户持仓, 也绝不静默少平 ----------------------------------------------------


async def test_the_rollback_waits_for_late_positions_and_closes_everything(tmp_path, monkeypatch):
    provider, gateway = LaggingProvider(empty_reads=5), MultiGateway()
    ns = _app(tmp_path / "spread-rollback-late.sqlite3", provider, gateway, _plan())
    monkeypatch.setattr(spread_execution, "SPREAD_POSITION_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(spread_execution, "SPREAD_POLL_SECONDS", 0.01)
    gateway.on_submit(SYMBOL_A, FILL, FILL)               # A 开仓全成; 回退平A腿也全成
    provider.hold(SYMBOL_A, "long", volume=1, open_price=815.0, today=1)
    _walk_open_signal(provider)
    await ns.monitor.run_once()
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "CANCELLED" and attempt["outcome"] == "rolled_back"
    assert "；开仓未成齐已回退：回退完成。" in attempt["detail"]   # 等到了持仓: 全额回退
    rollback = _requests(gateway, offset="CLOSE")
    assert [(r["symbol"], r["direction"], r["volume"]) for r in rollback] == [(SYMBOL_A, "SELL", 1)]
    assert _notices(ns.app, "已回退已成交部分（回退完成）")
    ns.store.close()


async def test_the_rollback_names_the_unclosable_lots_when_positions_never_show(tmp_path, monkeypatch):
    provider, gateway = LaggingProvider(empty_reads=10**9), MultiGateway()
    ns = _app(tmp_path / "spread-rollback-none.sqlite3", provider, gateway, _plan())
    monkeypatch.setattr(spread_execution, "SPREAD_POSITION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(spread_execution, "SPREAD_POLL_SECONDS", 0.01)
    gateway.on_submit(SYMBOL_A, FILL)                      # A 开仓全成
    gateway.on_submit(SYMBOL_B, STUCK)                     # B 一直悬着: 补单也不成 → 回退
    _walk_open_signal(provider)
    await ns.monitor.run_once()
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "CANCELLED" and attempt["outcome"] == "leftover"
    assert "；开仓未成齐已回退：A腿（SHFE.rb2701）还有 1 手需手动平掉。" in attempt["detail"]
    assert _requests(gateway, offset="CLOSE") == []        # 持仓一直没出现: 一张平仓单都不瞎下
    assert _notices(ns.app, "A腿（SHFE.rb2701）还有 1 手需手动平掉", "策略已暂停")
    ns.store.close()


# -- (4) 平仓补完仍不成: 搁置并暂停, 不反向开仓 --------------------------------------------------------


async def test_a_close_that_never_fills_is_stranded_and_never_reopened(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-strand.sqlite3", provider, gateway,
              _plan(rules={"entryLong": {"conditions": [NEVER]}, "exitLong": {"conditions": [ALWAYS]}}))
    gateway.on_submit(SYMBOL_A, FILL)
    provider.hold(SYMBOL_A, "long", volume=1, open_price=815.0, today=1)
    provider.hold(SYMBOL_B, "short", volume=1, open_price=811.0, today=1)

    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "submitted", result
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "PARTIAL"                 # A腿平掉了, B腿没平掉
    assert "B腿（SHFE.rb2705）还剩 1 手没平掉，请手动处理。" in attempt["detail"]
    assert _requests(gateway, offset="OPEN") == []        # 绝不反向开仓
    assert [r["direction"] for r in _requests(gateway, offset="CLOSE")] == ["SELL", "BUY", "BUY"]
    assert _draft_status(ns) == "paused"
    assert _notices(ns.app, "B腿（SHFE.rb2705）还剩 1 手没平掉", "不会自动重发", "策略已暂停")
    ns.store.close()


# -- (5) 被拒立即回退 / 结果未知不动 -------------------------------------------------------------------


async def test_a_rejected_leg_rolls_the_other_leg_back_immediately(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-reject.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, REJECT)                   # A 被拒
    gateway.on_submit(SYMBOL_B, PARTIAL_FILL, FILL)       # B 已成交 1 手; 回退平B腿全成
    provider.hold(SYMBOL_B, "short", volume=1, open_price=811.0, today=1)
    _walk_open_signal(provider)
    await ns.monitor.run_once()
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "CANCELLED"
    assert "A腿 1 手：模拟委托已拒绝" in attempt["detail"]
    assert "回退平B腿 1 手：模拟委托已成交" in attempt["detail"] and "回退完成" in attempt["detail"]
    assert [(r["symbol"], r["direction"]) for r in _requests(gateway, offset="CLOSE")] == \
        [(SYMBOL_B, "BUY")]
    assert _draft_status(ns) == "paused"
    assert _notices(ns.app, "开仓两腿没有同时成交（B腿成交 1 手）")
    ns.store.close()


async def test_an_unknown_leg_is_never_resent_and_never_rolled_back(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-unknown.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, TimeoutError("response lost"))   # B 发送中断: 结果未知
    _walk_open_signal(provider)
    await ns.monitor.run_once()
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "UNKNOWN"
    assert "B腿 1 手：结果未知，只回读原委托" in attempt["detail"]
    assert "；结果未知，不自动重发、不回退。" in attempt["detail"]
    assert gateway.cancel_count == 0 and gateway.submit_count == 2    # 不撤、不再发
    assert _requests(gateway, offset="CLOSE") == []                   # 不回退
    assert _draft_status(ns) == "paused"
    assert _notices(ns.app, "B腿（SHFE.rb2705）委托结果未知", "不自动重发也不回退", "策略已暂停")
    ns.store.close()


# -- (6) 每秒价差止损 / 止盈 -------------------------------------------------------------------------


def _stop_plan():
    return _plan(rules={"entryLong": {"conditions": [NEVER]}, "exitLong": {"conditions": [NEVER]}},
                 spread={"kind": "difference",
                         "legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_B}], "stopLoss": 5})


def _hold_a_long_spread(provider: LiveProvider) -> None:
    provider.hold(SYMBOL_A, "long", volume=1, open_price=830.0, today=1)   # 开仓价差 20
    provider.hold(SYMBOL_B, "short", volume=1, open_price=810.0, today=1)
    provider.quotes[SYMBOL_A]["last"] = 815.0
    provider.quotes[SYMBOL_B]["last"] = 811.0                             # 当前价差 4


async def test_the_per_second_stop_loss_closes_both_legs_once(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-stop.sqlite3", provider, gateway, _stop_plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    _hold_a_long_spread(provider)

    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "submitted", result
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["action"] == "close_long" and attempt["status"] == "SUBMITTED"
    assert [(r["symbol"], r["direction"]) for r in _requests(gateway, offset="CLOSE")] == \
        [(SYMBOL_A, "SELL"), (SYMBOL_B, "BUY")]
    # 触发时只说“正在平仓”, 结果由 follow 结束时说: 先触发、后完成, 两条各一次。
    assert _notices(ns.app, "价差止损触发（当前价差 4，开仓价差 20）", "两腿正在按对手价平仓")
    assert _notices(ns.app, "价差止损平仓完成，两腿已全部平仓")

    await ns.monitor.run_once()                           # 同一持仓只触发一次
    assert gateway.submit_count == 2
    assert len(_notices(ns.app, "价差止损触发")) == 1
    ns.store.close()


async def test_a_stale_leg_quote_skips_that_second(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-stale.sqlite3", provider, gateway, _stop_plan())
    _hold_a_long_spread(provider)
    provider.stale(SYMBOL_A, 30.0)                        # A 腿行情超 15 秒不新鲜

    result = (await ns.monitor.run_once())[0]
    assert result["status"] not in {"submitted", "partial"}
    assert gateway.submit_count == 0 and _attempts(ns) == []
    ns.store.close()


async def test_a_stuck_stop_loss_close_strands_without_a_success_notice(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-stop-strand.sqlite3", provider, gateway, _stop_plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, STUCK)                     # B 平仓悬着, 补单也不成
    _hold_a_long_spread(provider)

    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "submitted", result
    await ns.executor.drain()

    assert _notices(ns.app, "价差止损触发（当前价差 4，开仓价差 20）", "两腿正在按对手价平仓")
    assert _notices(ns.app, "B腿（SHFE.rb2705）还剩 1 手没平掉")
    assert _notices(ns.app, "平仓完成") == []              # 平不掉绝不说“已平仓”
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "PARTIAL" and attempt["outcome"] == "leftover"
    ns.store.close()


# -- (6b) 评估失败不拦每秒止损 ------------------------------------------------------------------------


async def test_the_stop_still_fires_when_the_evaluation_cannot_run(tmp_path):
    provider, gateway = BarlessProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-stop-nobars.sqlite3", provider, gateway, _stop_plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    _hold_a_long_spread(provider)

    result = (await ns.monitor.run_once())[0]              # 评估会抛 strategy_bars_insufficient
    assert result["status"] == "submitted", result         # 止损在评估之前已判断并下单
    await ns.executor.drain()
    assert _attempts(ns)[0]["action"] == "close_long"
    assert [(r["symbol"], r["direction"]) for r in _requests(gateway, offset="CLOSE")] == \
        [(SYMBOL_A, "SELL"), (SYMBOL_B, "BUY")]
    ns.store.close()


async def test_a_barless_leg_leaves_the_evaluation_waiting(tmp_path):
    provider, gateway = BarlessProvider(), MultiGateway()  # 对照: 没持仓时坏K线确实让评估 waiting
    ns = _app(tmp_path / "spread-nobars.sqlite3", provider, gateway, _stop_plan())

    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "waiting" and gateway.submit_count == 0
    ns.store.close()


async def test_a_flat_position_is_never_judged(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-flat.sqlite3", provider, gateway, _stop_plan())
    result = (await ns.monitor.run_once())[0]
    assert result["status"] in {"waiting", "duplicate", "blocked"} and gateway.submit_count == 0
    ns.store.close()


async def test_alert_mode_reminds_once_per_position(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-alert.sqlite3", provider, gateway, _stop_plan(), signal_action="alert")
    _hold_a_long_spread(provider)

    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "alerted", result
    assert "价差止损触发（当前价差 4，开仓价差 20），只提醒，未下单" in result["detail"]
    assert gateway.submit_count == 0
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "SKIPPED" and attempt["signalAction"] == "alert"

    await ns.monitor.run_once()                           # 同一持仓只提醒一次
    assert len(_attempts(ns)) == 1 and gateway.submit_count == 0
    assert len(_notices(ns.app, "价差止损触发")) == 1
    ns.store.close()


async def test_no_second_judgement_while_a_close_is_in_progress(tmp_path, monkeypatch):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-inflight.sqlite3", provider, gateway, _stop_plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    _hold_a_long_spread(provider)
    monkeypatch.setattr(spread_execution, "SPREAD_FILL_WAIT_SECONDS", 5.0)

    assert (await ns.monitor.run_once())[0]["status"] == "submitted"
    await ns.monitor.run_once()                           # 平仓进行中: 不再判断、不再下单
    assert gateway.submit_count == 2 and len(_attempts(ns)) == 1
    assert len(_notices(ns.app, "价差止损触发")) == 1

    monkeypatch.setattr(spread_execution, "SPREAD_FILL_WAIT_SECONDS", 0.01)
    await ns.executor.drain()
    assert _attempts(ns)[0]["status"] == "SUBMITTED"
    ns.store.close()


# -- (7) 出卡确认 ------------------------------------------------------------------------------------


async def _proposed_card(ns, provider) -> dict[str, Any]:
    _walk_open_signal(provider)
    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "proposed", result
    cards = _events(ns.app, "proposal.spread")
    assert cards, "the monitor must publish a proposal.spread card"
    return cards[-1]


async def test_a_confirmed_card_dispatches_both_legs_and_reports_each_order(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-card.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    card = await _proposed_card(ns, provider)

    assert gateway.submit_count == 0 and _events(ns.app, "proposal.trade") == []
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "SKIPPED" and attempt["cardProposalIds"] == [card["proposalId"]]
    assert [leg["symbol"] for leg in card["spread"]["legs"]] == [SYMBOL_A, SYMBOL_B]
    assert card["currentSpread"] is not None and card["units"] == 1
    assert card["approvalId"] and card["spreadWords"]

    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    confirmed = await ns.app.state.proposals.confirm(
        card["proposalId"], user_id=USER, approval_id=card["approvalId"],
        proposal_version=card["version"], idempotency_key="spread-confirm-1")
    assert confirmed["status"] == "candidate_recorded"
    assert "两腿同时以对手价发送" in [event["message"] for event in _events(ns.app, "proposal.result")][-1]
    assert [order["displayStatus"] for order in confirmed["simulationOrders"]] == ["已成交", "已成交"]
    # 线上 09-29 回归：跟踪还没结束时不给结论（曾把空的 outcome 读成“结果未知请核对”）。
    assert "executionConclusion" not in confirmed
    await ns.executor.drain()

    attempt = _attempts(ns)[0]                            # 同一条尝试记录接住了两腿
    assert attempt["status"] == "SUBMITTED"
    assert len([item for item in attempt["clientOrderId"].split(",") if item]) == 2
    republished = _events(ns.app, "proposal.spread")[-1]  # 卡以事件更新, 带逐笔真实结果
    assert [order["displayStatus"] for order in republished["simulationOrders"]] == ["已成交", "已成交"]

    replay = await ns.app.state.proposals.confirm(        # 幂等重放: 不再下单
        card["proposalId"], user_id=USER, approval_id=card["approvalId"],
        proposal_version=card["version"], idempotency_key="spread-confirm-1")
    assert replay["status"] == "candidate_recorded" and gateway.submit_count == 2
    ns.store.close()


async def test_the_card_gets_the_final_conclusion_on_success(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-card-done.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    card = await _proposed_card(ns, provider)
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    await ns.app.state.proposals.confirm(
        card["proposalId"], user_id=USER, approval_id=card["approvalId"],
        proposal_version=card["version"], idempotency_key="spread-confirm-done")
    await ns.executor.drain()

    republished = _events(ns.app, "proposal.spread")[-1]   # 执行结束再推一次卡, 带结论行
    assert republished["proposalId"] == card["proposalId"]
    assert republished["executionConclusion"] == {"code": "filled", "text": "成交完成"}
    replay = await ns.app.state.proposals.confirm(         # 幂等重放说同一句
        card["proposalId"], user_id=USER, approval_id=card["approvalId"],
        proposal_version=card["version"], idempotency_key="spread-confirm-done")
    assert replay["executionConclusion"] == {"code": "filled", "text": "成交完成"}
    ns.store.close()


async def test_the_card_gets_the_final_conclusion_after_a_rollback(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-card-rollback.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    card = await _proposed_card(ns, provider)
    gateway.on_submit(SYMBOL_A, FILL, FILL)                # A 开仓全成; 回退平A腿也全成
    gateway.on_submit(SYMBOL_B, STUCK)                     # B 一直悬着 → 回退
    provider.hold(SYMBOL_A, "long", volume=1, open_price=815.0, today=1)
    await ns.app.state.proposals.confirm(
        card["proposalId"], user_id=USER, approval_id=card["approvalId"],
        proposal_version=card["version"], idempotency_key="spread-confirm-rb")
    await ns.executor.drain()

    republished = _events(ns.app, "proposal.spread")[-1]
    assert republished["proposalId"] == card["proposalId"]
    assert republished["executionConclusion"] == {"code": "rolled_back", "text": "已回退并暂停"}
    replay = await ns.app.state.proposals.confirm(
        card["proposalId"], user_id=USER, approval_id=card["approvalId"],
        proposal_version=card["version"], idempotency_key="spread-confirm-rb")
    assert replay["executionConclusion"]["text"] == "已回退并暂停"
    ns.store.close()


async def test_an_interrupted_confirm_marks_the_attempt_failed_when_no_leg_went_out(tmp_path):
    """#518：出卡确认后执行被中断——还没标记 follow（一条腿都没发出去）时，尝试写 FAILED 并
    说明原因；已进 follow 的属于 UNKNOWN 语义，不改。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-card-broken.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    card = await _proposed_card(ns, provider)
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)

    async def interrupted(**_: Any) -> dict[str, Any]:
        raise RuntimeError("runtime interrupted mid-confirm")

    ns.executor._dispatch_and_follow = interrupted
    with pytest.raises(RuntimeError):
        await ns.app.state.proposals.confirm(
            card["proposalId"], user_id=USER, approval_id=card["approvalId"],
            proposal_version=card["version"], idempotency_key="spread-confirm-broken")
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "FAILED"
    assert attempt["detail"] == "确认后执行中断，两腿没有发出；请核对持仓后重新确认。"
    assert gateway.submit_count == 0
    ns.store.close()


async def test_a_paused_strategy_card_is_refused_without_orders(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-card-paused.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    card = await _proposed_card(ns, provider)
    ns.app.state.proposals.transition_strategy_status(strategy_id=STRATEGY_ID, user_id=USER, target="paused")

    with pytest.raises(ProposalConflict) as error:
        await ns.app.state.proposals.confirm(
            card["proposalId"], user_id=USER, approval_id=card["approvalId"],
            proposal_version=card["version"], idempotency_key="spread-confirm-paused")
    assert error.value.code == "strategy_not_monitoring"
    assert str(error.value) == "策略已暂停或停止，这张价差卡没有发单。"
    assert gateway.submit_count == 0 and gateway.cancel_count == 0
    blocked = _events(ns.app, "proposal.spread")[-1]       # 卡带中文原因重推, 不下任何单
    assert blocked["proposalId"] == card["proposalId"]
    assert blocked["confirmBlocked"]["code"] == "strategy_not_monitoring"
    assert blocked["confirmBlocked"]["reason"] == "策略已暂停或停止，这张价差卡没有发单。"
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "SKIPPED" and attempt["legs"] == []
    ns.store.close()


async def test_a_rejected_card_orders_nothing(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-card-reject.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    card = await _proposed_card(ns, provider)

    await ns.app.state.proposals.reject(card["proposalId"], user_id=USER,
                                        proposal_version=card["version"], idempotency_key="spread-reject-1")
    assert gateway.submit_count == 0
    # 线上 09-29：放弃后运行卡片仍写“出卡待确认”——尝试随卡改为已取消并写明原因，策略照常监控。
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "CANCELLED" and attempt["detail"] == "确认卡已放弃，未下单"
    assert _draft_status(ns) == "monitoring"
    ns.store.close()


async def test_an_expired_card_confirm_returns_approval_expired(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-card-expire.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    card = await _proposed_card(ns, provider)

    with ns.store.connection:                             # 10 分钟有效期已过
        ns.store.connection.execute(
            "UPDATE proposals SET approval_expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (card["proposalId"],))
    with pytest.raises(ProposalConflict) as error:
        await ns.app.state.proposals.confirm(
            card["proposalId"], user_id=USER, approval_id=card["approvalId"],
            proposal_version=card["version"], idempotency_key="spread-confirm-late")
    assert error.value.code == "approval_expired"
    assert gateway.submit_count == 0 and _attempts(ns)[0]["status"] == "SKIPPED"
    ns.store.close()


async def test_a_spoken_confirm_sends_both_legs(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-voice.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    await _register_window(ns.app, window_id=WINDOW)
    await _proposed_card(ns, provider)

    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    service = VoiceRealtimeService(ns.app, key_file=tmp_path / "missing-key")
    state = CallState(window_id=WINDOW)
    state.chat_session_id = ns.session_id                 # 服务器侧还在等的卡就是这张价差卡
    confirmed = await service.run_tool("confirm", {}, state)
    assert confirmed["ok"] is True, confirmed
    await ns.executor.drain()
    assert gateway.submit_count == 2
    assert _attempts(ns)[0]["status"] == "SUBMITTED"
    ns.store.close()


# -- (8) 开关降级 ------------------------------------------------------------------------------------


async def test_auto_degrades_to_a_card_when_ai_autonomy_is_off(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-autonomy.sqlite3", provider, gateway, _plan())
    ns.store.update_preferences(USER, {"trade": {"aiAutonomy": False}})
    card = await _proposed_card(ns, provider)
    assert card["proposalId"] and gateway.submit_count == 0
    assert _attempts(ns)[0]["signalAction"] == "confirm"
    ns.store.close()


async def test_halting_new_opens_blocks_the_open_but_not_the_stop(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-halt.sqlite3", provider, gateway,
              _plan(spread={"kind": "difference",
                            "legs": [{"symbol": SYMBOL_A}, {"symbol": SYMBOL_B}], "stopLoss": 5}))
    await ns.app.state.autonomy.deactivate(user_id=USER, scope="opens")
    _walk_open_signal(provider)

    blocked = (await ns.monitor.run_once())[0]            # 开仓被只停新开仓挡住
    assert blocked["status"] == "blocked" and "已只停新开仓" in blocked["detail"]
    assert gateway.submit_count == 0

    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    _hold_a_long_spread(provider)
    stopped = (await ns.monitor.run_once())[0]            # 止损平仓照常执行
    assert stopped["status"] == "submitted", stopped
    await ns.executor.drain()
    assert [(r["symbol"], r["direction"]) for r in _requests(gateway, offset="CLOSE")] == \
        [(SYMBOL_A, "SELL"), (SYMBOL_B, "BUY")]
    ns.store.close()


async def test_an_emergency_stop_orders_nothing(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-emergency.sqlite3", provider, gateway, _plan())
    await ns.app.state.autonomy.deactivate(user_id=USER, emergency=True)
    _walk_open_signal(provider)

    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "not_dispatched", result
    await ns.executor.drain()

    assert gateway.submit_count == 0 and gateway.cancel_count == 0
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "CANCELLED"
    assert "两腿没有成交，无需回退" in attempt["detail"]    # 两腿都没发出去: 无需回退
    assert _draft_status(ns) == "paused"
    assert _notices(ns.app, "开仓两腿没有同时成交（两腿均未成交）")
    ns.store.close()


# -- (9) 重启清扫 ------------------------------------------------------------------------------------


async def test_a_restart_mid_execution_sweeps_the_attempt_to_unknown(tmp_path, monkeypatch):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-restart.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    _walk_open_signal(provider)
    monkeypatch.setattr(spread_execution, "SPREAD_FILL_WAIT_SECONDS", 5.0)

    assert (await ns.monitor.run_once())[0]["status"] == "submitted"
    attempt_id = _attempts(ns)[0]["attemptId"]
    task = ns.executor._tasks[attempt_id]                 # Runtime 在跟单中途被杀
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    fresh = StrategyMonitorService(store=ns.store, proposals=ns.app.state.proposals,
                                   autonomy=ns.app.state.autonomy)
    await fresh.run_once()                                # 首轮先跑重启清扫
    attempt = _attempts(ns)[0]
    assert attempt["status"] == "UNKNOWN"
    assert "Runtime 在价差执行中途重启，两腿执行状态未知，请核对两腿持仓后手动处理。" in attempt["detail"]
    assert _draft_status(ns) == "paused"
    assert _notices(ns.app, "Runtime 在价差执行中途重启", "已暂停策略")
    assert gateway.submit_count == 2 and gateway.cancel_count == 0   # 清扫本身不下单不撤单
    ns.store.close()


async def test_a_restart_between_the_card_confirm_and_the_follow_marker_fails_the_unsent_attempt(tmp_path):
    """#518: confirming a spread card reopens its attempt (SKIPPED → RESERVED, signal_action set); a kill
    before the follow marker left it RESERVED for good — the sweep took only 'following' and signal-less
    RESERVED rows. No leg goes out before that marker, so the sweep ends it FAILED like 518-F: nothing sent
    again, the strategy keeps running. A card still waiting for its confirmation is left alone."""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-restart-confirm.sqlite3", provider, gateway, _plan(), signal_action="confirm")
    await _proposed_card(ns, provider)
    attempt_id = _attempts(ns)[0]["attemptId"]

    def sweep() -> StrategyMonitorService:
        return StrategyMonitorService(store=ns.store, proposals=ns.app.state.proposals, autonomy=ns.app.state.autonomy)

    await sweep().spread_execution.recover_interrupted()
    assert next(item for item in _attempts(ns) if item["attemptId"] == attempt_id)["status"] == "SKIPPED"
    assert ns.store.reopen_strategy_execution_attempt(attempt_id=attempt_id)   # confirmed, then the Runtime was killed
    await sweep().spread_execution.recover_interrupted()
    attempt = next(item for item in _attempts(ns) if item["attemptId"] == attempt_id)
    assert (attempt["status"], attempt["detail"]) == ("FAILED", "确认后执行中断，两腿没有发出；请核对持仓后重新确认。")
    assert _draft_status(ns) == "monitoring"
    assert _notices(ns.app, "价差卡确认后 Runtime 重启，两腿没有发出")
    assert gateway.submit_count == 0
    ns.store.close()


async def test_a_completed_spread_trade_is_not_swept(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "spread-restart-done.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, FILL)
    _walk_open_signal(provider)
    await ns.monitor.run_once()
    await ns.executor.drain()                             # 已走到终态 SUBMITTED

    fresh = StrategyMonitorService(store=ns.store, proposals=ns.app.state.proposals,
                                   autonomy=ns.app.state.autonomy)
    await fresh.run_once()
    assert _attempts(ns)[0]["status"] == "SUBMITTED"      # 完成的交易不被误清扫
    assert _draft_status(ns) == "monitoring"
    assert _notices(ns.app, "Runtime 在价差执行中途重启") == []
    ns.store.close()


# -- (10) 单腿策略不受影响 ---------------------------------------------------------------------------


async def test_a_single_contract_strategy_still_orders_one_leg(tmp_path):
    provider, gateway = LiveProvider(), MultiGateway()
    plan = _plan(rules={"entryLong": {"conditions": [GT_ZERO]},
                        "exitLong": {"conditions": [NEVER]}}, spread=None)
    ns = _app(tmp_path / "spread-single.sqlite3", provider, gateway, plan)
    gateway.on_submit(SYMBOL_A, FILL)
    _walk_open_signal(provider)

    result = (await ns.monitor.run_once())[0]
    assert result["status"] == "submitted", result
    assert gateway.submit_count == 1
    attempt = _attempts(ns)[0]
    assert len(attempt["legs"]) == 1 and attempt["legs"][0]["volume"] == 1
    with pytest.raises(SimulationOrderError):
        await ns.executor.launch_confirmed(
            payload={"strategySource": {"attemptId": attempt["attemptId"], "strategyId": STRATEGY_ID},
                     "action": "open_long", "units": 1},
            user_id=USER, session_id=ns.session_id)   # 单腿不走价差执行器
    ns.store.close()


# -- (11) 撤单没派发出去 ≠ 已撤 (2026-10-04 #518) ----------------------------------------------------


class UndispatchedCancelGateway(MultiGateway):
    """撤单回执 NOT_DISPATCHED：命令没送到交易所，原单还挂在场上 —— 按约定这不是“已撤”。"""

    def __init__(self) -> None:
        super().__init__()
        self.undispatched_cancels = 0

    async def cancel_order_once(self, client_order_id: str, command_id: str) -> dict[str, Any]:
        if self.undispatched_cancels > 0:
            self.undispatched_cancels -= 1
            request = self.requests[client_order_id]
            return self._record(client_order_id, {
                "commandStatus": "NOT_DISPATCHED",
                **self.report(request, **self._state(client_order_id, request))})
        return await super().cancel_order_once(client_order_id, command_id)


async def test_a_cancel_that_never_dispatched_settles_unknown_instead_of_chasing(tmp_path):
    provider, gateway = LiveProvider(), UndispatchedCancelGateway()
    ns = _app(tmp_path / "spread-cancel-undispatched.sqlite3", provider, gateway, _plan())
    gateway.on_submit(SYMBOL_A, FILL)
    gateway.on_submit(SYMBOL_B, STUCK)              # B腿悬着 → 补单前先撤
    gateway.undispatched_cancels = 1                # 这次撤单没有派发出去
    _walk_open_signal(provider)
    await ns.monitor.run_once()
    await ns.executor.drain()

    attempt = _attempts(ns)[0]
    assert attempt["status"] == "UNKNOWN"
    assert "B腿 1 手：撤单未派发，原单仍在场，只回读原委托" in attempt["detail"]
    assert "；结果未知，不自动重发、不回退。" in attempt["detail"]
    assert gateway.submits_by_symbol[SYMBOL_B] == 1  # B 腿只有原单，没有补单
    assert _requests(gateway, offset="CLOSE") == []  # 也没有回退平仓
    assert _draft_status(ns) == "paused"
    assert _notices(ns.app, "B腿（SHFE.rb2705）委托结果未知", "不自动重发也不回退", "策略已暂停")
    ns.store.close()



# -- 停止并平仓 (#675): 价差逐腿归属, 首腿成交不挡另一腿 ----------------------------------------------


def _hold_spread(provider: LiveProvider, direction: str = "long", units: int = 1, *,
                 a_today: int | None = None, b_today: int | None = None) -> None:
    a_dir, b_dir = ("long", "short") if direction == "long" else ("short", "long")
    provider.hold(SYMBOL_A, a_dir, volume=units, open_price=815.0,
                  today=units if a_today is None else a_today)
    provider.hold(SYMBOL_B, b_dir, volume=units, open_price=811.0,
                  today=units if b_today is None else b_today)


async def _stop_close_cards(ns, session_id: str) -> list[dict[str, Any]]:
    """The real 停止并平仓: stop the monitoring instance (an already-stopped instance stays
    stopped — a second call issues a second card group), then the app's own monitor issues
    one close card per leg / bucket into the session (the #654 attribution chain)."""
    if ns.store.get_strategy_draft(STRATEGY_ID, user_id=USER)["status"] in {"monitoring", "paused"}:
        ns.app.state.proposals.transition_strategy_status(
            strategy_id=STRATEGY_ID, user_id=USER, target="ready")
    outcome = await ns.monitor.propose_close_after_stop(
        ns.store.get_strategy_draft(STRATEGY_ID, user_id=USER), session_id=session_id)
    assert outcome["cards"], outcome
    return outcome["cards"]


async def _confirm_trade(app, card: dict[str, Any], key: str):
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local",
                                  headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        return await client.post(f"/api/proposals/{card['proposalId']}/confirm", json={
            "approvalId": card["approvalId"], "proposalVersion": card["version"], "idempotencyKey": key})


def _close_request_direction(card: dict[str, Any]) -> str:
    return "SELL" if card.get("side") == "sell_close" else "BUY"


@pytest.mark.parametrize("direction,first", [("long", SYMBOL_A), ("long", SYMBOL_B),
                                             ("short", SYMBOL_A), ("short", SYMBOL_B)])
async def test_both_stop_close_legs_confirm_whichever_is_clicked_first(tmp_path, direction, first):
    """#675 反例正控：多价差 / 空价差的两张停止平仓卡都能确认，先点 A 或先点 B 一样——
    守卫按腿核验，不再把整体价差方向当单腿方向比较。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-order.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, direction)
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    assert set(cards) == {(SYMBOL_A, "today"), (SYMBOL_B, "today")}
    second = SYMBOL_B if first == SYMBOL_A else SYMBOL_A
    original_ids: dict[str, str] = {}
    for symbol in (first, second):
        gateway.on_submit(symbol, FILL)
        card = cards[(symbol, "today")]
        confirmed = await _confirm_trade(ns.app, card, f"stop-close-{symbol}")
        assert confirmed.status_code == 200, confirmed.text
        original_ids[symbol] = confirmed.json()["clientOrderId"]
        request = gateway.requests[original_ids[symbol]]
        assert (request["direction"], request["offset"], request["volume"]) == (
            _close_request_direction(card), "CLOSETODAY", 1)
        attempt = ns.store.connection.execute(
            "SELECT * FROM strategy_execution_attempts WHERE id = ?",
            (card["strategySource"]["attemptId"],)).fetchone()
        assert original_ids[symbol] in str(attempt["client_order_id"])
    replay = await _confirm_trade(ns.app, cards[(first, "today")], f"stop-close-{first}")
    assert replay.status_code == 200
    assert replay.json()["clientOrderId"] == original_ids[first]
    assert gateway.submit_count == 2                      # 幂等重放不再发单
    ns.store.close()


@pytest.mark.parametrize("filled", ["full", "partial"])
async def test_the_other_legs_card_still_confirms_after_the_first_leg_fills(tmp_path, filled):
    """首腿全成 / 真实部分成交之后, spread_position 把不足完整组合的持仓读成 flat——原 B 卡仍要能按
    停止时的持久目标确认 (#675), 不因 A 腿已动而要求完整价差组合继续存在。部分成交用网关的真实
    部分成回报（2 手成 1、仍在场上）, 核对累计成交与账上剩余, 重放原卡不重发。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-{filled}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long", units=2)
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    assert (cards[(SYMBOL_A, "today")]["volume"], cards[(SYMBOL_B, "today")]["volume"]) == (2, 2)
    # 真实部分成交回报: A 腿 2 手成 1 手, 委托仍在场上; 全成则 FILL。
    gateway.on_submit(SYMBOL_A, FILL if filled == "full" else {"status": "ALIVE", "volume_left": 1})
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], "stop-close-a-first")
    assert confirmed.status_code == 200, confirmed.text
    a_order = confirmed.json()["clientOrderId"]
    if filled == "full":                                  # A 腿全成: 账户只剩 B 腿
        provider.position_items = [item for item in provider.position_items if item["symbol"] != SYMBOL_A]
    else:                                                 # A 腿部分成: 账户还剩 1 手今仓
        for item in provider.position_items:
            if item["symbol"] == SYMBOL_A:
                item["volume"], item["todayVolume"], item["historicalVolume"] = 1, 1, 0
    link = ns.store.connection.execute(
        "SELECT request_json, last_report_json FROM tqkq_sim_order_links WHERE client_order_id = ?",
        (a_order,)).fetchone()
    report_order = json.loads(link["last_report_json"])["order"]
    assert (int(json.loads(link["request_json"])["volume"]) - int(report_order["volumeLeft"]),
            int(report_order["volumeLeft"])) == ((2, 0) if filled == "full" else (1, 1))
    # A 腿自己的在途/已成交不挡 B 腿的原卡
    gateway.on_submit(SYMBOL_B, FILL)
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_B, "today")], "stop-close-b-after")
    assert confirmed.status_code == 200, confirmed.text
    request = gateway.requests[confirmed.json()["clientOrderId"]]
    assert (request["direction"], request["offset"], request["volume"]) == ("BUY", "CLOSETODAY", 2)
    assert gateway.submit_count == 2
    replay = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], "stop-close-a-first")
    assert replay.status_code == 200 and replay.json()["clientOrderId"] == a_order
    assert gateway.submit_count == 2                      # 部分成交后的重放也不重发
    if filled == "partial":                               # A 腿账上还剩的 1 手要新卡才平
        assert next(item for item in provider.position_items if item["symbol"] == SYMBOL_A)["volume"] == 1
    ns.store.close()


async def test_stop_close_cards_split_today_and_historical_and_confirm_each(tmp_path):
    """今昨分桶各出各卡：A 腿今 1 昨 1、B 腿今 1,三张卡都按自己的桶确认发送。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-buckets.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long", units=2, a_today=1)     # A 腿今 1 昨 1；B 腿今 2
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    assert set(cards) == {(SYMBOL_A, "historical"), (SYMBOL_A, "today"), (SYMBOL_B, "today")}
    assert (cards[(SYMBOL_A, "today")]["volume"], cards[(SYMBOL_A, "historical")]["volume"],
            cards[(SYMBOL_B, "today")]["volume"]) == (1, 1, 2)
    for (symbol, bucket), card in cards.items():
        gateway.on_submit(symbol, FILL)
        confirmed = await _confirm_trade(ns.app, card, f"stop-close-{symbol}-{bucket}")
        assert confirmed.status_code == 200, confirmed.text
        request = gateway.requests[confirmed.json()["clientOrderId"]]
        assert request["offset"] == ("CLOSETODAY" if bucket == "today" else "CLOSE")
        assert request["volume"] == card["volume"]
    assert gateway.submit_count == 3
    ns.store.close()


@pytest.mark.parametrize("fault", ["shrunk", "rolled", "day_roll", "flipped", "reedited"])
async def test_stop_close_card_refused_when_the_leg_no_longer_matches(tmp_path, fault):
    """确认前这腿不足 / 今昨桶变化 / 交易日已换（新一天的今仓是新一代, 不拿它填旧 today 卡）/
    换向 / 策略换代（版本摘要变了）：明确拒绝, 不拿其它仓补足。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-{fault}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long", units=2)
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    card = cards[(SYMBOL_B, "today")]
    expected = "stop_close_position_changed"
    if fault == "shrunk":                                 # 这腿被平掉 1 手, 剩 1 手 < 卡上 2 手
        for item in provider.position_items:
            if item["symbol"] == SYMBOL_B:
                item["volume"], item["todayVolume"] = 1, 1
    elif fault == "rolled":                               # 隔日: 原今仓成了昨仓
        for item in provider.position_items:
            if item["symbol"] == SYMBOL_B:
                item["todayVolume"], item["historicalVolume"] = 0, 2
    elif fault == "day_roll":                             # 交易日换到次日: 同数量的新今仓也不填旧卡
        provider.day = "2026-09-02"
    elif fault == "flipped":                              # 换向: 这腿反向重开, 原方向没有仓了
        for item in provider.position_items:
            if item["symbol"] == SYMBOL_B:
                item["direction"] = "long"
        expected = "stop_close_position_gone"
    else:                                                 # 策略重新编辑换代（新版本 + 新摘要）
        ns.store.connection.execute(
            "UPDATE strategy_drafts SET version = 2, definition_digest = ?, updated_at = ? WHERE id = ?",
            ("e" * 64, datetime.now(UTC).isoformat(), STRATEGY_ID))
        ns.store.connection.commit()
    refused = await _confirm_trade(ns.app, card, f"stop-close-{fault}")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == expected
    assert gateway.submit_count == 0
    assert ns.store.get(card["proposalId"])["payload"]["confirmBlocked"]["code"] == expected
    ns.store.close()


@pytest.mark.parametrize("status", ["SUBMITTED", "UNKNOWN"])
async def test_stop_close_card_refused_while_another_close_of_the_same_leg_is_unresolved(
        tmp_path, status):
    """同腿同桶还有一张在途 / 结果未知的平仓（第二次停止并平仓出的卡已确认但委托悬着）：
    这张卡不发单；另一腿不受影响。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-flight-{status}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long", units=2)
    first = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    second = {(card["symbol"], card.get("positionBucket")): card
              for card in await _stop_close_cards(ns, ns.session_id)}
    gateway.on_submit(SYMBOL_A, STUCK)                    # 第二组的 A 卡确认了, 委托一直悬着
    confirmed = await _confirm_trade(ns.app, second[(SYMBOL_A, "today")], "stop-close-second-a")
    assert confirmed.status_code == 200, confirmed.text
    if status == "UNKNOWN":                               # 重启清扫把 SUBMITTED 标成 UNKNOWN
        ns.store.connection.execute(
            "UPDATE strategy_execution_attempts SET status = 'UNKNOWN' WHERE id = ?",
            (second[(SYMBOL_A, "today")]["strategySource"]["attemptId"],))
        ns.store.connection.commit()
    refused = await _confirm_trade(ns.app, first[(SYMBOL_A, "today")], "stop-close-first-a")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_protection_in_flight"
    assert gateway.submit_count == 1
    gateway.on_submit(SYMBOL_B, FILL)                     # 另一腿的卡不受这单影响
    confirmed = await _confirm_trade(ns.app, first[(SYMBOL_B, "today")], "stop-close-first-b")
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 2
    ns.store.close()


@pytest.mark.parametrize("direction", ["long", "short"])
async def test_stop_close_card_refused_when_the_leg_was_closed_and_reopened(tmp_path, direction):
    """同腿被外部平掉又原数重开（多/空价差都验）: 持仓看起来没变, 本地账本也还没轮询到——但
    provider 最新 activity 里已有停止后的同腿平仓成交。守卫按停止时持久化的成交基线比对最新
    事实, 原卡不再误花新仓; 另一腿照常。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-reopen-{direction}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, direction)
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    # 停止后外部平掉 A 腿又原数重开: 只有 provider 的最新 activity 看得见, 账本是空的。
    provider.activity["trades"] = [dict(
        tradeId="external-close-a", clientOrderId="EXT1", symbol=SYMBOL_A,
        direction="SELL" if direction == "long" else "BUY", offset="CLOSETODAY",
        price=815.0, volume=1, tradeDateTime=int(datetime.now(UTC).timestamp() * 1_000_000_000),
        tradingDay=provider.day)]
    assert ns.store.list_account_trades(trading_account_id=ns.store.default_trading_account_id) == []
    refused = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], f"stop-close-reopen-{direction}")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_leg_consumed"
    assert gateway.submit_count == 0
    gateway.on_submit(SYMBOL_B, FILL)
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_B, "today")], "stop-close-reopen-b")
    assert confirmed.status_code == 200, confirmed.text
    ns.store.close()


@pytest.mark.parametrize("state,expected", [
    ("prepared", 409), ("unknown", 409), ("identity", 409),
    ("finished", 200), ("abandoned", 200),
])
async def test_stop_close_card_refused_while_a_same_leg_close_is_not_provably_settled(
        tmp_path, state, expected):
    """同腿同桶的另一张平仓委托只有确凿终态（已成交/已撤销/已拒绝/已放弃）才让路：待发送
    (PREPARED——已占用、可能恢复发送)、结果未知、身份异常都挡; 本腿的卡不发单。控制组：
    已成交/已放弃的旧平仓不挡（其成交通过持仓与基线读回）。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-settled-{state}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    link = {
        "prepared": {"submit_phase": "PREPARED", "cancel_phase": "NONE", "last_remote_status": None,
                     "last_report_json": None},
        "unknown": {"submit_phase": "UNKNOWN", "cancel_phase": "NONE", "last_remote_status": None,
                    "last_report_json": None},
        "identity": {"submit_phase": "CONFIRMED", "cancel_phase": "NONE",
                     "last_remote_status": "IDENTITY_MISMATCH", "last_report_json": None},
        "finished": {"submit_phase": "CONFIRMED", "cancel_phase": "NONE",
                     "last_remote_status": "FINISHED",
                     "last_report_json": json.dumps({"ordersInitialized": True, "found": True, "order": {
                         "status": "FINISHED", "volumeLeft": 0, "isError": False}})},
        "abandoned": {"submit_phase": "ABANDONED", "cancel_phase": "NONE", "last_remote_status": None,
                      "last_report_json": None},
    }[state]
    now = datetime.now(UTC).isoformat()
    ns.store.connection.execute(
        """INSERT INTO tqkq_sim_order_links
           (id, user_id, proposal_id, client_order_id, request_json, submit_phase, cancel_phase,
            last_remote_status, last_report_json, last_synced_at, created_at, updated_at, trading_account_id)
           VALUES ('link-other', ?, ?, 'other-close', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (USER, cards[(SYMBOL_B, "today")]["proposalId"],
         json.dumps({"clientOrderId": "other-close", "symbol": SYMBOL_A,
                                         "direction": "SELL", "offset": "CLOSETODAY", "volume": 1,
                                         "priceType": "LIMIT", "limitPrice": 814.0}),
         link["submit_phase"], link["cancel_phase"], link["last_remote_status"],
         link["last_report_json"], now, now, now, ns.store.default_trading_account_id))
    ns.store.connection.commit()
    gateway.on_submit(SYMBOL_A, FILL)
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], f"stop-close-{state}")
    assert confirmed.status_code == expected, confirmed.text
    if expected == 409:
        assert confirmed.json()["detail"]["code"] == "stop_close_protection_in_flight"
        assert gateway.submit_count == 0
    else:
        assert gateway.submit_count == 1
    ns.store.close()


async def test_stop_close_card_needs_a_current_activity_read_and_a_persisted_baseline(tmp_path):
    """两份必需证据 (#675): 出卡时读不到当前成交记录就不出价差平仓卡（不猜基线）; 确认时
    activity 不新鲜明确拒绝; 没有停止时基线的旧卡作废, 不回填猜测。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-baseline.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    provider.activity["dataStatus"] = "stale"             # 出卡时成交记录不可信
    outcome = await ns.monitor.propose_close_after_stop(
        ns.store.get_strategy_draft(STRATEGY_ID, user_id=USER), session_id=ns.session_id)
    assert outcome["cards"] == [] and "成交记录" in outcome["detail"]
    provider.activity["dataStatus"] = "normal"
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    card = cards[(SYMBOL_A, "today")]
    intent_id = ns.store.connection.execute(
        "SELECT intent_id FROM strategy_execution_attempts WHERE id = ?",
        (card["strategySource"]["attemptId"],)).fetchone()[0]
    payload = json.loads(ns.store.connection.execute(
        "SELECT payload_json FROM strategy_intent_snapshots WHERE id = ?", (intent_id,)).fetchone()[0])
    del payload["baselineFillIds"]                        # 归属链修复前的旧卡: 没有基线
    ns.store.connection.execute(
        "UPDATE strategy_intent_snapshots SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), intent_id))
    ns.store.connection.commit()
    refused = await _confirm_trade(ns.app, card, "stop-close-baseline-missing")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_baseline_missing"
    assert gateway.submit_count == 0
    provider.activity["ageSeconds"] = 30                  # 确认时成交记录不新鲜
    refused = await _confirm_trade(ns.app, cards[(SYMBOL_B, "today")], "stop-close-stale")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_position_unreadable"
    assert gateway.submit_count == 0
    provider.activity["ageSeconds"] = 0
    gateway.on_submit(SYMBOL_B, FILL)
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_B, "today")], "stop-close-fresh-b")
    assert confirmed.status_code == 200, confirmed.text
    ns.store.close()


async def test_stop_close_confirm_replay_after_restart_keeps_orders_and_the_other_leg(tmp_path):
    """重启后：原 clientOrderId / 提交次数不变（幂等重放不重发），剩余腿的原卡照常确认。"""
    path = tmp_path / "stop-close-restart.sqlite3"
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(path, provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    gateway.on_submit(SYMBOL_A, FILL)
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], "stop-close-restart-a")
    assert confirmed.status_code == 200, confirmed.text
    original = confirmed.json()["clientOrderId"]
    ns.store.close()

    app = application(path, gateway, provider)
    replay = await _confirm_trade(app, cards[(SYMBOL_A, "today")], "stop-close-restart-a")
    assert replay.status_code == 200, replay.text
    assert replay.json()["clientOrderId"] == original
    assert gateway.submit_count == 1
    gateway.on_submit(SYMBOL_B, FILL)
    confirmed = await _confirm_trade(app, cards[(SYMBOL_B, "today")], "stop-close-restart-b")
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 2
    app.state.store.close()


async def test_stop_close_card_with_a_mismatched_attempt_binding_is_refused(tmp_path):
    """卡的 strategySource 文本指向别的尝试：绑定核实不通过, 明确作废且不发单。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-unbound.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    card = cards[(SYMBOL_B, "today")]
    other_attempt = ns.store.connection.execute(
        "SELECT id FROM strategy_execution_attempts WHERE id != ? LIMIT 1",
        (card["strategySource"]["attemptId"],)).fetchone()[0]
    payload = json.loads(ns.store.connection.execute(
        "SELECT payload_json FROM proposals WHERE id = ?", (card["proposalId"],)).fetchone()[0])
    payload["strategySource"]["attemptId"] = other_attempt
    ns.store.connection.execute(
        "UPDATE proposals SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), card["proposalId"]))
    ns.store.connection.commit()
    refused = await _confirm_trade(ns.app, card, "stop-close-unbound")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_card_unbound"
    assert gateway.submit_count == 0
    assert ns.store.get(card["proposalId"])["payload"]["confirmBlocked"]["code"] == "stop_close_card_unbound"
    ns.store.close()


async def test_stop_close_issuance_reads_positions_and_day_inside_one_snapshot(tmp_path):
    """出卡不混读 (#675): 快照外的持仓 / 交易日是旧的（空仓、1999-12-31）——出卡仍按快照内的
    新事实出卡（不是“没有持仓”），卡上登记的交易日也是快照内读到的当天。"""
    provider, gateway = SnapshotScopedProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-snapshot-issue.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    assert set(cards) == {(SYMBOL_A, "today"), (SYMBOL_B, "today")}
    intent_payload = json.loads(ns.store.connection.execute(
        "SELECT payload_json FROM strategy_intent_snapshots WHERE id = "
        "(SELECT intent_id FROM strategy_execution_attempts WHERE id = ?)",
        (cards[(SYMBOL_A, "today")]["strategySource"]["attemptId"],)).fetchone()[0])
    assert intent_payload["tradingDay"] == provider.day    # 快照内的事实, 不是快照外的 1999-12-31
    ns.store.close()


async def test_stop_close_confirm_judges_on_the_snapshot_facts_only(tmp_path):
    """确认不混读 (#675): 三样事实都取自同一快照。快照内交易日已换日 → 换代拒绝（在 with 外
    再读 summary/positions 的旧读法会拿快照外的旧放行）; 快照内 activity 有停止后的外部同腿
    平仓 → 已被消费拒绝（快照外的 activity 看不见它）。正控: 快照内外一致时原卡照常确认。"""
    provider, gateway = SnapshotScopedProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-snapshot-confirm.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    card = cards[(SYMBOL_A, "today")]
    # 快照外是停止那一刻的旧事实: 旧交易日、原样的旧持仓、还没有外部平仓的旧 activity。
    provider.stale_day, provider.stale_positions = provider.day, [dict(item) for item in provider.position_items]
    provider.day = "2026-09-02"                           # 快照内: 交易日已换到次日
    refused = await _confirm_trade(ns.app, card, "stop-close-snapshot-day")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_position_changed"
    assert gateway.submit_count == 0
    provider.day = "2026-09-01"                           # 快照内: 交易日没换, 但最新 activity
    provider.stale_positions = []                         # 旧读法在 with 外连持仓都看不见
    provider.activity["trades"] = [dict(                   # 有了停止后的外部同腿平仓
        tradeId="external-close-a", clientOrderId="EXT1", symbol=SYMBOL_A,
        direction="SELL", offset="CLOSETODAY", price=815.0, volume=1,
        tradeDateTime=int(datetime.now(UTC).timestamp() * 1_000_000_000), tradingDay=provider.day)]
    refused = await _confirm_trade(ns.app, card, "stop-close-snapshot-consumed")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_leg_consumed"
    assert gateway.submit_count == 0
    provider.activity["trades"] = []                       # 正控: 内外一致, 另一腿原卡照常确认
    provider.sync_stale()
    gateway.on_submit(SYMBOL_B, FILL)
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_B, "today")], "stop-close-snapshot-b")
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 1
    ns.store.close()


@pytest.mark.parametrize("with_time", [True, False])
async def test_stop_close_card_refused_when_a_new_same_leg_close_fill_has_no_trading_day(
        tmp_path, with_time):
    """成交先到、交易日晚补 (#675): provider 最新 activity 里的同腿新平仓成交缺 tradingDay 字段
    （账本明确支持的合法形状）——不能因缺日期就略过它。缺日期的成交无法证明属于别的交易日,
    按当日对待（不猜安全）: 有没有成交时刻都拒绝, 不发单。另一交易日的成交（带当日以外
    tradingDay）照旧不挡。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-dayless-{with_time}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    fill = dict(tradeId="external-close-a", clientOrderId="EXT1", symbol=SYMBOL_A,
                direction="SELL", offset="CLOSETODAY", price=815.0, volume=1)
    if with_time:
        fill["tradeDateTime"] = int(datetime.now(UTC).timestamp() * 1_000_000_000)
    provider.activity["trades"] = [fill]
    gateway.on_submit(SYMBOL_A, FILL)
    refused = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], f"stop-close-dayless-{with_time}")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_leg_consumed"
    assert gateway.submit_count == 0
    provider.activity["trades"] = [dict(fill, tradeId="older-fill-a", tradingDay="2026-08-28")]
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], f"stop-close-oldday-{with_time}")
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 1
    ns.store.close()


async def test_stop_close_card_refused_while_another_users_close_on_the_same_account_is_unresolved(tmp_path):
    """#699 模型: 同一交易账户可授予多个用户——另一授权用户挂在这条腿上的未决平仓（待发送）同样
    占用这些手数。内部风控读同账户全部原订单（同一归一投影）, 公开接口仍只看得到自己的。换到
    另一账户的同形状委托不挡。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-other-user.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    now = datetime.now(UTC).isoformat()
    ns.store.connection.execute(
        """INSERT INTO tqkq_sim_order_links
           (id, user_id, proposal_id, client_order_id, request_json, submit_phase, cancel_phase,
            last_remote_status, last_report_json, last_synced_at, created_at, updated_at, trading_account_id)
           VALUES ('link-other-user', 'user-colleague', ?, 'other-user-close', ?, 'PREPARED', 'NONE',
                   NULL, NULL, ?, ?, ?, ?)""",
        (cards[(SYMBOL_B, "today")]["proposalId"],
         json.dumps({"clientOrderId": "other-user-close", "symbol": SYMBOL_A, "direction": "SELL",
                     "offset": "CLOSETODAY", "volume": 1, "priceType": "LIMIT", "limitPrice": 814.0}),
         now, now, now, ns.store.default_trading_account_id))
    ns.store.connection.commit()
    order_service = ns.app.state.accounts.default.simulation_orders
    assert [order for order in order_service.list(user_id=USER)
            if order["clientOrderId"] == "other-user-close"] == []   # 公开读法看不到别人的单
    gateway.on_submit(SYMBOL_A, FILL)
    refused = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], "stop-close-other-user")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_protection_in_flight"
    assert gateway.submit_count == 0
    ns.store.connection.execute(                              # 同形状委托换到另一账户: 不挡
        "UPDATE tqkq_sim_order_links SET trading_account_id = 'tqkq-sim:elsewhere' WHERE id = 'link-other-user'")
    ns.store.connection.commit()
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], "stop-close-other-account")
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 1
    ns.store.close()


# -- (#700 review 收口): 生命周期判在途 / 畸形交易日 / 定向基线 / 重叠确认 ---------------------------


def _same_leg_close_link(ns, *, proposal_id: str, client_order_id: str, submit_phase: str,
                         cancel_phase: str, last_remote_status: str | None,
                         last_report_json: str | None, volume: int = 1) -> None:
    """一条挂在这条腿上的历史平仓委托（同合约、同平向、CLOSETODAY），直接按给定阶段落库。"""
    now = datetime.now(UTC).isoformat()
    ns.store.connection.execute(
        """INSERT INTO tqkq_sim_order_links
           (id, user_id, proposal_id, client_order_id, request_json, submit_phase, cancel_phase,
            last_remote_status, last_report_json, last_synced_at, created_at, updated_at, trading_account_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (f"link-{client_order_id}", USER, proposal_id, client_order_id,
         json.dumps({"clientOrderId": client_order_id, "symbol": SYMBOL_A,
                     "direction": "SELL", "offset": "CLOSETODAY", "volume": volume,
                     "priceType": "LIMIT", "limitPrice": 814.0}),
         submit_phase, cancel_phase, last_remote_status, last_report_json, now, now, now,
         ns.store.default_trading_account_id))
    ns.store.connection.commit()


@pytest.mark.parametrize("phase,expected", [
    # 终态部分成交: 2 手成 1、余量已撤（FINISHED）——展示文案是“部分成交”，生命周期已结束。
    ({"submit_phase": "CONFIRMED", "cancel_phase": "NONE", "last_remote_status": "FINISHED",
      "last_report_json": json.dumps({"ordersInitialized": True, "found": True, "order": {
          "status": "FINISHED", "volumeLeft": 1, "isError": False}})}, 200),
    # 还挂在场上的部分成交: ALIVE，仍在簿上，照旧挡。
    ({"submit_phase": "CONFIRMED", "cancel_phase": "NONE", "last_remote_status": "ALIVE",
      "last_report_json": json.dumps({"ordersInitialized": True, "found": True, "order": {
          "status": "ALIVE", "volumeLeft": 1, "isError": False}})}, 409),
])
async def test_stop_close_card_judges_in_flight_by_lifecycle_not_display_text(tmp_path, phase, expected):
    """终态部分成交 (#700 review r4236576811)：FINISHED、2 手成 1、余量已取消的旧平仓不再被
    展示文案“部分成交”永久当在途——它已成的手数在停止基线里，剩余可平量照常核验，新卡可以
    确认；仍 ALIVE 的部分成交照旧拒绝。旧委托的成交先落在基线里（停止前已发生）。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-lifecycle-{expected}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long", units=2)
    provider.activity["trades"] = [dict(             # 旧单停止前已成的那 1 手：进基线
        tradeId="old-partial-fill", clientOrderId="old-partial", symbol=SYMBOL_A,
        direction="SELL", offset="CLOSETODAY", price=815.0, volume=1,
        tradeDateTime=int(datetime.now(UTC).timestamp() * 1_000_000_000),
        tradingDay=provider.day)]
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    _same_leg_close_link(ns, proposal_id=cards[(SYMBOL_B, "today")]["proposalId"],
                         client_order_id="old-partial", volume=2, **phase)
    gateway.on_submit(SYMBOL_A, FILL)
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], f"stop-close-lifecycle-{expected}")
    assert confirmed.status_code == expected, confirmed.text
    if expected == 409:
        assert confirmed.json()["detail"]["code"] == "stop_close_protection_in_flight"
        assert gateway.submit_count == 0
    else:
        assert gateway.submit_count == 1
    ns.store.close()


@pytest.mark.parametrize("bad_day", ["malformed", "2026-99-99"])
async def test_stop_close_card_refused_when_a_new_same_leg_fill_has_a_malformed_trading_day(
        tmp_path, bad_day):
    """非空坏日期 (#700 review)：停止后同腿新平仓成交带畸形 tradingDay（malformed /
    2026-99-99）——无法证明属于另一个有效交易日，与缺日期一样按当日对待：拒绝、不发单。
    规范异日正控照旧放行。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-badday-{bad_day}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    provider.activity["trades"] = [dict(
        tradeId="bad-day-close", clientOrderId="EXT1", symbol=SYMBOL_A,
        direction="SELL", offset="CLOSETODAY", price=815.0, volume=1,
        tradeDateTime=int(datetime.now(UTC).timestamp() * 1_000_000_000), tradingDay=bad_day)]
    gateway.on_submit(SYMBOL_A, FILL)
    refused = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], f"stop-close-badday-{bad_day}")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "stop_close_leg_consumed"
    assert gateway.submit_count == 0
    provider.activity["trades"] = [dict(           # 正控: 规范的另一个有效交易日不挡
        tradeId="valid-other-day", clientOrderId="EXT2", symbol=SYMBOL_A,
        direction="SELL", offset="CLOSETODAY", price=815.0, volume=1,
        tradeDateTime=int(datetime.now(UTC).timestamp() * 1_000_000_000),
        tradingDay="2026-08-28")]
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], f"stop-close-oldday-{bad_day}")
    assert confirmed.status_code == 200, confirmed.text
    assert gateway.submit_count == 1
    ns.store.close()


async def test_stop_close_baseline_persists_only_the_relevant_fills(tmp_path):
    """定向基线 (#700 review r4236576831)：intent 里的 baselineFillIds 只放确认会核验的那一
    组——同合约、同平仓方向、同 offset、且交易日不是可证明的其它有效日。开仓、反向平仓、
    有效异日平仓不再进每张卡；缺失/畸形日的同向平仓保守并入。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-baseline-scope.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    when = int(datetime.now(UTC).timestamp() * 1_000_000_000)

    def persisted(trade_id: str, *, symbol: str, direction: str, offset: str, day: str | None) -> dict:
        return dict(tradeId=trade_id, clientOrderId=f"cfg-{trade_id}", symbol=symbol,
                    direction=direction, offset=offset, price=815.0, volume=1,
                    tradeDateTime=when, tradingDay=day)

    ns.store.upsert_account_trades(
        [persisted("keep-today", symbol=SYMBOL_A, direction="SELL", offset="CLOSETODAY",
                   day=provider.day),
         persisted("keep-dayless", symbol=SYMBOL_A, direction="SELL", offset="CLOSETODAY", day=None),
         persisted("keep-malformed", symbol=SYMBOL_A, direction="SELL", offset="CLOSETODAY",
                   day="malformed"),
         persisted("drop-other-day", symbol=SYMBOL_A, direction="SELL", offset="CLOSETODAY",
                   day="2026-08-20"),
         persisted("drop-open", symbol=SYMBOL_A, direction="BUY", offset="OPEN", day=provider.day),
         persisted("drop-direction", symbol=SYMBOL_A, direction="BUY", offset="CLOSETODAY",
                   day=provider.day),
         persisted("drop-symbol", symbol=SYMBOL_B, direction="SELL", offset="CLOSETODAY",
                   day=provider.day)],
        trading_account_id=ns.store.default_trading_account_id)
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    intent_payload = json.loads(ns.store.connection.execute(
        "SELECT payload_json FROM strategy_intent_snapshots WHERE id = "
        "(SELECT intent_id FROM strategy_execution_attempts WHERE id = ?)",
        (cards[(SYMBOL_A, "today")]["strategySource"]["attemptId"],)).fetchone()[0])
    assert set(intent_payload["baselineFillIds"]) == {"keep-today", "keep-dayless", "keep-malformed"}
    ns.store.close()


@pytest.mark.parametrize("stored_day,in_baseline", [
    ("2026-02-29", True),   # 非闰年 2 月 29：日历上不存在
    ("2026-02-30", True),   # SQLite 会前滚日期，而不是拒绝
    ("2026-13-01", True),
    ("0000-01-01", True),   # SQLite 接受年零，Python 日历不接受
    ("2024-02-29", False),  # 合法闰日，确实是另一个交易日
])
async def test_stop_close_keeps_persisted_fills_on_days_sqlite_cannot_disprove(
        tmp_path, stored_day, in_baseline):
    """闰年回卷 (#700 review)：SQLite date('2026-02-30')='2026-03-02' 是前滚不是拒绝——旧的
    date(trading_day)=date(?) 过滤会把这种无法严格证明是合法异日的持久成交从基线里漏掉，
    确认时同一条成交又从账本读回来，卡被当成“停止后新成交”自锁。修复后出卡与确认用同一
    规范：基线保留它，确认不当作新成交，原卡照常发出。"""
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(tmp_path / f"stop-close-undisprov-{stored_day}.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    when = int(datetime.now(UTC).timestamp() * 1_000_000_000)
    ns.store.upsert_account_trades(
        [dict(tradeId="legacy-close", clientOrderId="cfg-legacy", symbol=SYMBOL_A,
              direction="SELL", offset="CLOSETODAY", price=815.0, volume=1,
              tradeDateTime=when, tradingDay=stored_day)],
        trading_account_id=ns.store.default_trading_account_id)
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    intent_payload = json.loads(ns.store.connection.execute(
        "SELECT payload_json FROM strategy_intent_snapshots WHERE id = "
        "(SELECT intent_id FROM strategy_execution_attempts WHERE id = ?)",
        (cards[(SYMBOL_A, "today")]["strategySource"]["attemptId"],)).fetchone()[0])
    assert ("legacy-close" in intent_payload["baselineFillIds"]) is in_baseline
    gateway.on_submit(SYMBOL_A, FILL)
    confirmed = await _confirm_trade(ns.app, cards[(SYMBOL_A, "today")], f"stop-close-{stored_day}")
    assert confirmed.status_code == 200, confirmed.text             # 确认不自锁
    assert gateway.submit_count == 1
    ns.store.close()


async def _parked_confirmations(ns, provider: GatedSnapshotProvider, posts: list[tuple[dict, str]]):
    """并发发出若干确认，等它们都停在守卫的快照预取处再一起放行（#700 review 的受控交错）。
    到齐以 provider 的同步回调置 Event，不轮询；失败路径放行门、取消并收尾挂起的任务。"""
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=ns.app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local",
                                  headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        provider.gate = asyncio.Event()
        all_parked = asyncio.Event()
        provider.on_parked = (lambda: all_parked.set() if provider.waiting >= len(posts) else None)
        tasks = [asyncio.create_task(client.post(
            f"/api/proposals/{card['proposalId']}/confirm",
            json={"approvalId": card["approvalId"], "proposalVersion": card["version"],
                  "idempotencyKey": key})) for card, key in posts]
        try:
            await asyncio.wait_for(all_parked.wait(), timeout=2.0)
            provider.gate.set()
            return await asyncio.gather(*tasks)
        finally:
            provider.on_parked = None
            if provider.gate is not None and not provider.gate.is_set():
                provider.gate.set()                 # 不留任何请求停在门上
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            provider.gate = None


async def test_overlapping_confirmations_of_one_stop_close_card_keep_the_winners_facts(tmp_path):
    """同卡重叠确认 (#700 review r4236633732)：两条确认都停在快照 await 处，一条先确认成功
    （事务里 PREPARE 了委托、消费了审批），迟到的守卫拒绝只许写回仍待确认、审批未用的卡——
    成功方的终态、approvalUsedAt、原 clientOrderId 与委托次数原样保留，幂等重放照常读回。"""
    provider, gateway = GatedSnapshotProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-overlap-one.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    cards = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    card = cards[(SYMBOL_A, "today")]
    gateway.on_submit(SYMBOL_A, FILL)
    provider.sync_stale()                                # 快照外的事实与快照内一致: 只交错确认, 不混别的事实
    first, second = await _parked_confirmations(
        ns, provider, [(card, "overlap-one-a"), (card, "overlap-one-b")])
    assert sorted((first.status_code, second.status_code)) == [200, 409]
    winner, winner_key = ((first, "overlap-one-a") if first.status_code == 200
                          else (second, "overlap-one-b"))
    client_order_id = winner.json()["clientOrderId"]
    record = ns.store.get(card["proposalId"])
    assert record["status"] == "candidate_recorded"    # 迟到的拒绝没有把卡写回待确认
    assert record["approvalUsedAt"] is not None
    assert record["payload"]["clientOrderId"] == client_order_id
    assert "confirmBlocked" not in record["payload"]
    assert gateway.submit_count == 1
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=ns.app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local",
                                  headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        replay = await client.post(
            f"/api/proposals/{card['proposalId']}/confirm",
            json={"approvalId": card["approvalId"], "proposalVersion": card["version"],
                  "idempotencyKey": winner_key})
    assert replay.status_code == 200, replay.text
    assert replay.json()["clientOrderId"] == client_order_id
    assert gateway.submit_count == 1                   # 幂等重放不重发
    ns.store.close()


async def test_overlapping_confirmations_of_two_same_leg_cards_dispatch_exactly_one(tmp_path):
    """同腿两卡并发 (#700 review r4236652193)：两次停止并平仓出的两张 A 腿卡同时确认——守卫
    的账户扫描与确认事务之间没有 await，后进事件循环的那条必然看见先到者的 PREPARED 委托：
    恰好一张确认发单，另一张明确拒绝；赢家的重放稳定，输家只在自己卡上记拒绝。"""
    provider, gateway = GatedSnapshotProvider(), MultiGateway()
    ns = _app(tmp_path / "stop-close-overlap-two.sqlite3", provider, gateway, _plan())
    _hold_spread(provider, "long")
    first = {(card["symbol"], card.get("positionBucket")): card
             for card in await _stop_close_cards(ns, ns.session_id)}
    second = {(card["symbol"], card.get("positionBucket")): card
              for card in await _stop_close_cards(ns, ns.session_id)}
    a_first, a_second = first[(SYMBOL_A, "today")], second[(SYMBOL_A, "today")]
    assert a_first["proposalId"] != a_second["proposalId"]
    gateway.on_submit(SYMBOL_A, FILL)
    provider.sync_stale()                                # 快照外的事实与快照内一致: 只交错确认, 不混别的事实
    one, other = await _parked_confirmations(
        ns, provider, [(a_first, "overlap-two-first"), (a_second, "overlap-two-second")])
    assert sorted((one.status_code, other.status_code)) == [200, 409]
    refused = other if other.status_code == 409 else one
    assert refused.json()["detail"]["code"] == "stop_close_protection_in_flight"
    assert gateway.submits_by_symbol[SYMBOL_A] == 1    # 这条腿只发出一单
    assert gateway.submit_count == 1
    winner = one if one.status_code == 200 else other
    client_order_id = winner.json()["clientOrderId"]
    assert ns.store.get(a_first["proposalId"])["payload"].get("clientOrderId") in {None, client_order_id}
    loser_record = ns.store.get((a_second if refused is other else a_first)["proposalId"])
    assert loser_record["payload"]["confirmBlocked"]["code"] == "stop_close_protection_in_flight"
    ns.store.close()


@pytest.mark.parametrize("report,expected_code", [(STUCK, "stop_close_protection_in_flight"),
                                                 (FILL, "stop_close_leg_consumed")])
async def test_same_leg_confirmations_on_independent_stores_reserve_once(tmp_path, report, expected_code):
    """Two Runtime connections finish fresh guards before either prepares its order."""
    path = tmp_path / "stop-close-two-connections.sqlite3"
    provider, gateway = LiveProvider(), MultiGateway()
    ns = _app(path, provider, gateway, _plan())
    _hold_spread(provider, "long")
    first = next(card for card in await _stop_close_cards(ns, ns.session_id)
                 if card["symbol"] == SYMBOL_A and card.get("positionBucket") == "today")
    second = next(card for card in await _stop_close_cards(ns, ns.session_id)
                  if card["symbol"] == SYMBOL_A and card.get("positionBucket") == "today")
    other_provider = LiveProvider()
    _hold_spread(other_provider, "long")
    other_app = application(path, gateway, other_provider)
    assert other_app.state.store.connection is not ns.store.connection
    ready = asyncio.Event()
    releases = [asyncio.Event(), asyncio.Event()]
    arrived = 0
    for index, app in enumerate((ns.app, other_app)):
        original = app.state.proposals._stop_close_card_guard

        async def parked(*args, _original=original, _release=releases[index], **kwargs):
            nonlocal arrived
            cut = await _original(*args, **kwargs)
            arrived += 1
            if arrived == 2:
                ready.set()
            await _release.wait()
            return cut

        app.state.proposals._stop_close_card_guard = parked
    gateway.on_submit(SYMBOL_A, report)

    tasks = [asyncio.create_task(_confirm_stop_close_http(ns.app, first, "independent-first")),
             asyncio.create_task(_confirm_stop_close_http(other_app, second, "independent-second"))]
    try:
        await asyncio.wait_for(ready.wait(), 2)
        releases[0].set()
        first_result = await asyncio.wait_for(tasks[0], 2)
        releases[1].set()
        results = [first_result, await asyncio.wait_for(tasks[1], 2)]
        assert sorted(result.status_code for result in results) == [200, 409]
        refused = next(result for result in results if result.status_code == 409)
        assert refused.json()["detail"]["code"] == expected_code
        assert gateway.submit_count == 1
        winner_index = next(i for i, result in enumerate(results) if result.status_code == 200)
        winner_app, winner_card, winner_key = ((ns.app, first, "independent-first") if winner_index == 0
                                              else (other_app, second, "independent-second"))
        replay = await _confirm_stop_close_http(winner_app, winner_card, winner_key)
        assert replay.status_code == 200
        assert replay.json()["clientOrderId"] == results[winner_index].json()["clientOrderId"]
        assert gateway.submit_count == 1
    finally:
        for release in releases:
            release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        other_app.state.store.close()
        ns.store.close()


async def _confirm_stop_close_http(app, card, key):
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={
            "Authorization": "Bearer test-local-token"})).status_code == 200
        return await client.post(f"/api/proposals/{card['proposalId']}/confirm", json={
            "approvalId": card["approvalId"], "proposalVersion": card["version"],
            "idempotencyKey": key})



async def test_same_leg_fill_after_remote_cut_before_guard_completion_is_refused(tmp_path):
    """A has an old remote cut; B fills before A can finish its guard."""
    path = tmp_path / "stop-close-prefetch-race.sqlite3"
    provider, gateway = SnapshotScopedProvider(), MultiGateway()
    ns = _app(path, provider, gateway, _plan())
    _hold_spread(provider, "long")
    provider.sync_stale()
    first = next(card for card in await _stop_close_cards(ns, ns.session_id)
                 if card["symbol"] == SYMBOL_A and card.get("positionBucket") == "today")
    second = next(card for card in await _stop_close_cards(ns, ns.session_id)
                  if card["symbol"] == SYMBOL_A and card.get("positionBucket") == "today")
    other_provider = LiveProvider()
    _hold_spread(other_provider, "long")
    other_app = application(path, gateway, other_provider)
    ready, release = asyncio.Event(), asyncio.Event()
    original = provider._request_async

    async def parked(method, endpoint, *args, **kwargs):
        cut = await original(method, endpoint, *args, **kwargs)
        if endpoint == "/v1/snapshot":
            ready.set()
            await release.wait()
        return cut

    provider._request_async = parked
    gateway.on_submit(SYMBOL_A, FILL)
    task = asyncio.create_task(_confirm_stop_close_http(ns.app, first, "prefetch-first"))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        winner = await _confirm_stop_close_http(other_app, second, "prefetch-second")
        assert winner.status_code == 200
        release.set()
        refused = await asyncio.wait_for(task, 2)
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "stop_close_leg_consumed"
        assert gateway.submit_count == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        other_app.state.store.close()
        ns.store.close()
