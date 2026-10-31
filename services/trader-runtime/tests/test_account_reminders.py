"""收盘前与风险度主动提醒 (2026-10-03)：窗口判定的纯函数边界（日盘/夜盘各自只看最后一段、
小节休息与午休不算收盘、跨零点的两种时段写法、周五夜盘属于周一交易日），与挂在持仓/账户
读取上的整条链路——时段只读合约目录（不占 K 线订阅）、每个（用户、账户、交易日、日盘/夜盘）
只发一次、落库幂等重启不重发、三处投递（对话消息 / feed.reminder / 语音口播）、开关与回放
静默、只列即将收盘的合约、顺风车失败绝不弄坏它搭的那次读取。#457 账户维度（两账户两条、
单账户不标名）；#458 只有同账户同一次读取的新鲜快照才提醒（过期不发也不落库）。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from test_account_registry import BETA_ACCOUNT, DEFAULT_ACCOUNT, write_accounts_file
from test_simulation_orders import SHANGHAI, USER, Gateway, NormalProvider, application
from trader_runtime import account_reminders
from trader_runtime.account_reminders import closing_window, normalize_schedule, risk_message
from trader_runtime.main import create_app
from trader_runtime.store import RuntimeStore

CST = SHANGHAI
NS = 1_000_000_000
# 固定日历：2026-10-06 周二（其夜盘收在 10-07 周三），2026-10-09 周五（夜盘属于 10-12 周一）。
TUE = date(2026, 10, 6)
FRI = date(2026, 10, 9)
# 落点测试里的第三个账户（注册表文件里现写的，不需要 test_account_registry 常量）。
GAMMA_ACCOUNT = "tqkq-sim:gamma"

# 日盘收 15:00、夜盘收 23:00 的时段（同一份 daytime 两段式）。
CLOSE_15 = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
            "night": [["21:00:00", "23:00:00"]]}
# 夜盘跨零点的两种写法：TqSdk 的 ≥24 小时（收 01:00）与信息卡的 end < start（收 02:30）。
NIGHT_1 = {"day": CLOSE_15["day"], "night": [["21:00:00", "25:00:00"]]}
NIGHT_230 = {"day": CLOSE_15["day"], "night": [["21:00:00", "02:30:00"]]}


def at(day: date, hour: int, minute: int, second: int = 0) -> int:
    moment = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=CST)
    return int(moment.timestamp()) * NS


async def login(app) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                         headers={"Origin": "http://127.0.0.1:5173"})
    assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
    return client


def reminder_notices(app, session_id: str, prefix: str = "reminder-") -> list[dict[str, Any]]:
    return [event for _, event in app.state.store.list_events(session_id)
            if event.get("name") == "message.completed"
            and str(event.get("payload", {}).get("turnId", "")).startswith(prefix)]


class ReminderProvider(NormalProvider):
    """三个旋钮：持仓、账户风险度覆盖、按合约的目录时段（收盘提醒读合约目录条目的
    tradingSchedule，绝不碰 shared.market/snapshot 的 K 线订阅——market_reads 计数为证）。"""

    def __init__(self) -> None:
        super().__init__()
        self.schedules: dict[str, dict[str, Any]] = {}
        self.summary_overrides: dict[str, Any] = {}
        self.market_reads = 0
        self.position_items = [
            {"symbol": "SHFE.au2612", "direction": "long", "volume": 2, "unrealizedPnl": 2670,
             "todayVolume": 2, "historicalVolume": 0, "frozenToday": 0, "frozenHistorical": 0},
            {"symbol": "SHFE.rb2610", "direction": "short", "volume": 1, "unrealizedPnl": -310,
             "todayVolume": 0, "historicalVolume": 1, "frozenToday": 0, "frozenHistorical": 0},
        ]

    def positions(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.position_items]

    def account_summary(self, turn_id: str) -> dict[str, Any]:
        return {**super().account_summary(turn_id), "tradingDay": "2026-10-06", **self.summary_overrides}

    def instrument_catalog(self) -> dict[str, Any]:
        catalog = super().instrument_catalog()
        return {**catalog, "items": [
            {**item, "tradingSchedule": self.schedules[item["symbol"]]}
            if item.get("symbol") in self.schedules else item
            for item in catalog.get("items", [])]}

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        self.market_reads += 1
        return super().market_workbench(symbol, duration_seconds)

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        self.market_reads += 1
        return super().market_snapshot(turn_id, symbol)


def count_shared_market_reads(app, monkeypatch) -> list[str]:
    """把共享行情源的 market/snapshot 也计入：提醒链路一处都不许走。"""
    reads: list[str] = []
    for name in ("market", "snapshot"):
        original = getattr(app.state.shared_market, name)

        async def counted(*args, _original=original, _name=name, **kwargs):
            reads.append(_name)
            return await _original(*args, **kwargs)

        monkeypatch.setattr(app.state.shared_market, name, counted)
    return reads


# -- 纯函数 -------------------------------------------------------------------------------------


def test_closing_window_judges_each_halfs_last_segment_only():
    schedule = normalize_schedule(CLOSE_15)
    assert closing_window(schedule, at(TUE, 14, 56)) == ("day", "2026-10-06", 4)   # 最后一段 15:00 收
    assert closing_window(schedule, at(TUE, 14, 50)) is None                        # 窗口还差 5 分钟
    assert closing_window(schedule, at(TUE, 15, 0, 0)) == ("day", "2026-10-06", 1)  # 正点仍在窗内
    assert closing_window(schedule, at(TUE, 10, 12)) is None                        # 10:15 是小节休息，不是收盘
    assert closing_window(schedule, at(TUE, 11, 29)) is None                        # 午休前的 11:30 也不是
    assert closing_window(schedule, at(TUE, 22, 57)) == ("night", "2026-10-07", 3)  # ≥18:00 属于次日
    assert closing_window(schedule, at(TUE, 22, 50)) is None
    assert closing_window(schedule, at(FRI, 22, 57)) == ("night", "2026-10-12", 3)  # 周五夜盘属于周一
    assert closing_window(normalize_schedule({"day": [["09:30", "11:30"], ["13:00", "15:00"]], "night": []}),
                          at(TUE, 22, 57)) is None                                  # 无夜盘的品种夜盘不提醒
    # 跨零点的两种写法都归一到次日凌晨，窗口跟着各自时段走。
    assert closing_window(normalize_schedule(NIGHT_1), at(TUE, 0, 56)) == ("night", "2026-10-06", 4)
    assert closing_window(normalize_schedule(NIGHT_1), at(TUE, 0, 54)) is None
    assert closing_window(normalize_schedule(NIGHT_230), at(TUE, 2, 26)) == ("night", "2026-10-06", 4)
    assert closing_window(normalize_schedule(NIGHT_230), at(TUE, 1, 56)) is None    # 02:30 收的品种此刻不在窗内
    assert closing_window(None, at(TUE, 14, 56)) is None                            # 拿不到时段就静默


def test_normalize_schedule_rejects_unusable_shapes():
    assert normalize_schedule(NIGHT_1)["night"] == [["21:00:00", "25:00:00"]]
    assert normalize_schedule(NIGHT_230)["night"] == [["21:00:00", "26:30:00"]]     # end < start → +24h
    assert normalize_schedule({"day": [["09:00", "25:99"]], "night": []}) is None   # 分钟越界
    assert normalize_schedule({"day": [["09:00"]], "night": []}) is None            # 不是成对时段
    assert normalize_schedule({"day": [], "night": []}) is None                     # 全空没有意义
    assert normalize_schedule("休市") is None


def test_risk_message_needs_a_trusted_ratio_at_or_above_80():
    hot = risk_message({"riskRatio": 83.1, "margin": 415_000, "equity": 500_000, "available": 85_000,
                        "dataStatus": "normal", "tradingDay": "2026-10-06"})
    assert hot == ("风险度提醒：现在风险度 83%（保证金占用 41.5 万 / 权益 50 万），"
                   "越高越接近被强平；可用 8.5 万。")
    assert risk_message({"riskRatio": 79.9, "dataStatus": "normal"}) is None
    assert risk_message({"riskRatio": 120, "dataStatus": "stale"}) is None           # 缓存快照不可信
    assert risk_message({"riskRatio": 99.5, "dataStatus": "normal", "isCached": True}) is None  # #458 标了缓存
    assert risk_message({"riskRatio": None, "dataStatus": "normal"}) is None         # 缺字段不算数
    assert risk_message({"dataStatus": "normal"}) is None


# -- 整链路 -------------------------------------------------------------------------------------


async def test_day_close_reminds_once_and_delivers_all_three_ways(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    app = application(tmp_path / "close-day.sqlite3", Gateway(), ReminderProvider())
    provider = app.state.data_provider
    provider.schedules["SHFE.au2612"] = CLOSE_15
    provider.schedules["SHFE.rb2610"] = CLOSE_15
    http = await login(app)
    account_id = (await http.get("/api/accounts")).json()["current"]
    assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    session_id = session["sessionId"]
    app.state.broker.subscribers[session_id] = 1   # 一个挂着流的窗口
    app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)  # 通话在线
    reads = count_shared_market_reads(app, monkeypatch)

    assert (await http.get("/api/positions")).status_code == 200
    notices = reminder_notices(app, session_id, "reminder-market_close_day")
    assert len(notices) == 1
    # 时段只来自合约目录：提醒链路不占 K 线订阅（shared.market/snapshot 与 fake 计数都为 0）。
    assert reads == [] and provider.market_reads == 0
    text = str(notices[0]["payload"]["text"])
    assert "收盘提醒：4 分钟后日盘收盘" in text
    assert "沪金2612 多 2 手（浮盈 +2,670）" in text and "螺纹钢2610 空 1 手（浮亏 -310）" in text
    assert text.endswith("未平。")
    # 单账户用户文案不带账户名前缀（#457：多账户才标）。
    assert not text.startswith("账户")
    assert notices[0]["payload"]["turnId"] == f"reminder-market_close_day-{account_id}-2026-10-06"
    cards = reminder_cards(app, session_id, notices[0]["payload"]["turnId"])
    assert len(cards) == 1 and cards[0]["kind"] == "market_close" and cards[0]["title"] == "收盘提醒"
    figures = {f["label"]: f["value"] for f in cards[0]["figures"]}
    assert figures["距收盘"] == "4 分钟" and figures["未平持仓"] == "2 个 · 3 手"
    assert_no_order_action(cards[0])
    feed = [event for _, event in app.state.broker.transient_events[session_id]
            if event.get("name") == "feed.reminder"]
    assert len(feed) == 1 and feed[0]["payload"]["title"] == "收盘提醒"
    # #932: the feed carries the reminder kind, the card's one line, the 提醒中心 row id and the day's unread.
    assert feed[0]["payload"]["kind"] == "market_close" and feed[0]["payload"]["text"] == cards[0]["summary"]
    assert feed[0]["payload"]["accountId"] == account_id and feed[0]["payload"]["type"] == "new"
    # The clock here is pinned to 2026-10-06, an earlier trading day than the wall clock's.
    page = (await http.get("/api/reminders", params={"scope": "earlier"})).json()
    assert [item["kind"] for item in page["items"]] == ["market_close"]
    assert page["items"][0]["id"] == feed[0]["payload"]["id"] and page["items"][0]["read"] is False
    target = {key: value for key, value in page["items"][0]["target"].items() if value is not None}
    assert target == {"type": "chat", "sessionId": session_id, "cardId": cards[0]["cardId"]}
    assert page["tradingDay"] and feed[0]["payload"]["unread"] == page["unread"]
    announcements = app.state.voice_realtime._pending_announcements[USER]
    assert announcements[-1]["kind"] == f"reminder-market_close_day-{account_id}-2026-10-06"
    assert announcements[-1]["text"] == text

    # 当天第二次读取不重发（幂等在库里，与轮询频率无关）。
    assert (await http.get("/api/positions")).status_code == 200
    assert len(reminder_notices(app, session_id)) == 1
    await http.aclose()
    app.state.store.close()
    # 重启不重发：同一份库上的新 store 认出这条提醒已发过（提醒记录与上一版仍读的旧账本行都在）。
    reopened = RuntimeStore(tmp_path / "close-day.sqlite3")
    assert reopened.reminder_claimed(user_id=USER, kind="market_close", dedupe_key=f"{account_id}:2026-10-06:day")
    assert reopened.record_reminder_notice(user_id=USER, account_id=account_id, trade_day="2026-10-06",
                                           kind="market_close_day", text=text) is False
    reopened.close()


async def test_close_reminder_stays_silent_before_the_window_without_positions_switch_off_or_in_replay(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    app = application(tmp_path / "close-negative.sqlite3", Gateway(), ReminderProvider())
    provider = app.state.data_provider
    provider.schedules["SHFE.au2612"] = CLOSE_15
    provider.schedules["SHFE.rb2610"] = CLOSE_15
    http = await login(app)
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    session_id = session["sessionId"]
    app.state.broker.subscribers[session_id] = 1

    # 开关默认关：窗口内、有持仓，也不发。
    assert (await http.get("/api/positions")).status_code == 200
    assert not reminder_notices(app, session_id)
    assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
    # 窗口还差几分钟（14:50）不发。
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 50))
    assert (await http.get("/api/positions")).status_code == 200
    assert not reminder_notices(app, session_id)
    # 10:12 小节休息不是收盘。
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 10, 12))
    assert (await http.get("/api/positions")).status_code == 200
    assert not reminder_notices(app, session_id)
    # 没有持仓不发。
    provider.position_items = []
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    assert (await http.get("/api/positions")).status_code == 200
    assert not reminder_notices(app, session_id)
    # 回放里没有“现在”，不发。
    provider.position_items = ReminderProvider().position_items
    app.state.replay_context = SimpleNamespace()
    assert (await http.get("/api/positions")).status_code == 200
    assert not reminder_notices(app, session_id)
    # 前面的静默都没有消耗掉这个交易日的一次提醒：回放结束后同窗内照发。
    app.state.replay_context = None
    assert (await http.get("/api/positions")).status_code == 200
    assert len(reminder_notices(app, session_id, "reminder-market_close_day")) == 1
    await http.aclose()
    app.state.store.close()


async def test_night_close_follows_each_contracts_own_schedule_and_reminds_once_per_night(
        tmp_path: Path, monkeypatch):
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 0, 56))
    app = application(tmp_path / "close-night.sqlite3", Gateway(), ReminderProvider())
    provider = app.state.data_provider
    provider.schedules["SHFE.au2612"] = NIGHT_1     # au 收 01:00（TqSdk ≥24h 写法）
    # rb2610 留在目录默认时段：收 02:30（信息卡 end < start 写法）。
    http = await login(app)
    account_id = (await http.get("/api/accounts")).json()["current"]
    assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    session_id = session["sessionId"]
    app.state.broker.subscribers[session_id] = 1

    # 00:56：只有 01:00 收的 au 在窗内；02:30 收的 rb 不列——只提醒即将收盘的合约。
    assert (await http.get("/api/positions")).status_code == 200
    notices = reminder_notices(app, session_id, "reminder-market_close_night")
    assert len(notices) == 1
    text = str(notices[0]["payload"]["text"])
    assert "4 分钟后夜盘收盘" in text and "沪金2612 多 2 手" in text and "螺纹钢2610" not in text
    assert notices[0]["payload"]["turnId"] == f"reminder-market_close_night-{account_id}-2026-10-06"

    # 02:26：轮到 rb 的 02:30，但这一夜（同一交易日）已经提醒过，不再发第二条。
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 2, 26))
    assert (await http.get("/api/positions")).status_code == 200
    assert len(reminder_notices(app, session_id)) == 1
    await http.aclose()
    app.state.store.close()


async def test_risk_reminder_fires_once_per_trading_day_and_respects_the_switch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    hot = ReminderProvider()
    hot.summary_overrides = {"riskRatio": 83.1, "margin": 415_000, "equity": 500_000, "available": 85_000}
    app = application(tmp_path / "risk-on.sqlite3", Gateway(), hot)
    http = await login(app)
    account_id = (await http.get("/api/accounts")).json()["current"]
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    session_id = session["sessionId"]
    app.state.broker.subscribers[session_id] = 1
    app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)

    # 开关默认开：81% 以上发一次（默认账户 18.6% 不动，这里的覆盖才触发）。
    assert (await http.get("/api/account/summary")).status_code == 200
    notices = reminder_notices(app, session_id, "reminder-risk_ratio")
    assert len(notices) == 1
    text = str(notices[0]["payload"]["text"])
    assert text == ("风险度提醒：现在风险度 83%（保证金占用 41.5 万 / 权益 50 万），"
                    "越高越接近被强平；可用 8.5 万。")
    assert notices[0]["payload"]["turnId"] == f"reminder-risk_ratio-{account_id}-2026-10-06"
    cards = reminder_cards(app, session_id, notices[0]["payload"]["turnId"])
    assert len(cards) == 1 and cards[0]["kind"] == "risk_ratio"
    assert [(f["label"], f["value"]) for f in cards[0]["figures"]] == [
        ("风险度", "83%"), ("保证金占用", "41.5 万"), ("权益", "50 万"), ("可用", "8.5 万")]
    assert cards[0]["figures"][0]["tone"] == "warning"
    assert_no_order_action(cards[0])
    feed = [event for _, event in app.state.broker.transient_events[session_id]
            if event.get("name") == "feed.reminder"]
    assert len(feed) == 1 and feed[0]["payload"]["title"] == "风险度提醒"
    announcements = app.state.voice_realtime._pending_announcements[USER]
    assert announcements[-1]["text"] == text
    # 同一交易日再读不重发。
    assert (await http.get("/api/account/summary")).status_code == 200
    assert len(reminder_notices(app, session_id, "reminder-risk_ratio")) == 1
    await http.aclose()
    app.state.store.close()

    # 79.9% 不发；开关关了，多高也不发（只提醒的开关，绝不是风控规则）。
    warm = ReminderProvider()
    warm.summary_overrides = {"riskRatio": 79.9}
    app = application(tmp_path / "risk-off.sqlite3", Gateway(), warm)
    http = await login(app)
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    session_id = session["sessionId"]
    app.state.broker.subscribers[session_id] = 1
    assert (await http.get("/api/account/summary")).status_code == 200
    assert not reminder_notices(app, session_id)
    assert (await http.put("/api/preferences", json={"notify": {"riskReminder": False}})).status_code == 200
    warm.summary_overrides = {"riskRatio": 99.5, "margin": 415_000, "equity": 500_000, "available": 85_000}
    assert (await http.get("/api/account/summary")).status_code == 200
    assert not reminder_notices(app, session_id)
    await http.aclose()
    app.state.store.close()


async def test_reminder_failure_never_breaks_the_read_it_rides_on(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    app = application(tmp_path / "remind-broken.sqlite3", Gateway(), ReminderProvider())
    provider = app.state.data_provider
    provider.schedules["SHFE.au2612"] = CLOSE_15
    provider.schedules["SHFE.rb2610"] = CLOSE_15
    http = await login(app)

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("preferences unavailable")

    monkeypatch.setattr(app.state.preference_store, "get", broken)
    positions = await http.get("/api/positions")
    assert positions.status_code == 200 and len(positions.json()["items"]) == 2  # 提醒挂了，持仓照常回
    provider.summary_overrides = {"riskRatio": 99.5, "margin": 415_000, "equity": 500_000, "available": 85_000}
    summary = await http.get("/api/account/summary")
    assert summary.status_code == 200 and summary.json()["riskRatio"] == 99.5
    await http.aclose()
    app.state.store.close()


async def test_a_broken_delivery_round_never_marks_the_reminder_sent_without_its_message(
        tmp_path: Path, monkeypatch):
    """#521：去重行与落库消息同进同退。投递这一步整轮挂掉（broker 的两个投递入口都坏），
    一次性账本行不能先落——旧顺序里这一轮过后“账本说发过、对话里永远没有这条消息”。
    修好后：消息随去重行在同一事务里写进 outbox，恢复后一次 flush 补送；再读也不重发。"""
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    app = application(tmp_path / "remind-seam.sqlite3", Gateway(), ReminderProvider())
    provider = app.state.data_provider
    provider.schedules["SHFE.au2612"] = CLOSE_15
    provider.schedules["SHFE.rb2610"] = CLOSE_15
    http = await login(app)
    assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    session_id = session["sessionId"]
    app.state.broker.subscribers[session_id] = 1

    broker = app.state.broker
    original_publish, original_flush = broker.publish, broker.flush

    async def broken(*args: Any, **kwargs: Any):
        raise RuntimeError("broker unavailable")

    broker.publish = broken  # type: ignore[method-assign]
    broker.flush = broken  # type: ignore[method-assign]
    assert (await http.get("/api/positions")).status_code == 200  # 顺风车失败不弄坏读取
    assert not reminder_notices(app, session_id)

    # 恢复后一次投递把已随账本行一起落库的消息补送出去。
    broker.publish, broker.flush = original_publish, original_flush  # type: ignore[method-assign]
    await broker.flush()
    notices = reminder_notices(app, session_id, "reminder-market_close_day")
    assert len(notices) == 1 and "收盘提醒" in str(notices[0]["payload"]["text"])
    # 那次投递仍然只有一次：恢复后再读不重发。
    assert (await http.get("/api/positions")).status_code == 200
    assert len(reminder_notices(app, session_id)) == 1
    await http.aclose()
    app.state.store.close()


async def test_two_accounts_each_keep_their_own_reminder_ids_and_label(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """#457：同一用户名下两个账户，同一天同一类提醒是两条——对话消息、feed 事件、语音口播
    三处的标识都带账户维度，互不顶掉；文案前缀标账户名；同一账户仍然一天只发一次。"""
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(
        tmp_path / "accounts.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake"},
        ])))
    app = create_app(
        tmp_path / "remind-two-accounts.sqlite3", local_token="test-local-token",
        agent_mode="fake", data_mode="fake",
        account_providers={DEFAULT_ACCOUNT: ReminderProvider(), BETA_ACCOUNT: ReminderProvider()},
    )
    try:
        # 名下两个账户（授权行是“有几个账户”的事实来源），文案才会标账户名。
        app.state.store.grant_account(account_id=DEFAULT_ACCOUNT, user_id=USER,
                                      role="owner", granted_by=USER)
        app.state.store.grant_account(account_id=BETA_ACCOUNT, user_id=USER,
                                      role="owner", granted_by=USER)
        provider_a = app.state.accounts.get(DEFAULT_ACCOUNT).provider
        provider_b = app.state.accounts.get(BETA_ACCOUNT).provider
        provider_a.schedules["SHFE.au2612"] = CLOSE_15   # 共享目录读默认账户的 provider
        session = app.state.broker.create_session("local", "native", USER)
        session_id = session.session_id
        app.state.broker.subscribers[session_id] = 1
        app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)
        http = await login(app)
        assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200

        # 换个顺序先乙后甲：两账户各自一条，互不顶掉。
        await account_reminders.check_positions(
            app, user_id=USER, account_id=BETA_ACCOUNT, positions=provider_b.positions(),
            summary=provider_b.account_summary(""))
        await account_reminders.check_positions(
            app, user_id=USER, account_id=DEFAULT_ACCOUNT, positions=provider_a.positions(),
            summary=provider_a.account_summary(""))
        notices = reminder_notices(app, session_id, "reminder-market_close_day")
        assert sorted(str(n["payload"]["turnId"]) for n in notices) == sorted([
            f"reminder-market_close_day-{BETA_ACCOUNT}-2026-10-06",
            f"reminder-market_close_day-{DEFAULT_ACCOUNT}-2026-10-06",
        ])
        texts = {str(n["payload"]["turnId"]): str(n["payload"]["text"]) for n in notices}
        assert texts[f"reminder-market_close_day-{BETA_ACCOUNT}-2026-10-06"].startswith("账户乙：收盘提醒：")
        assert texts[f"reminder-market_close_day-{DEFAULT_ACCOUNT}-2026-10-06"].startswith("账户甲：收盘提醒：")
        feed = [event for _, event in app.state.broker.transient_events[session_id]
                if event.get("name") == "feed.reminder"]
        assert sorted((f["payload"]["accountId"], f["payload"]["accountName"]) for f in feed) == sorted([
            (BETA_ACCOUNT, "账户乙"), (DEFAULT_ACCOUNT, "账户甲")])
        announced = [item["kind"] for item in app.state.voice_realtime._pending_announcements[USER]]
        assert sorted(set(announced)) == sorted([
            f"reminder-market_close_day-{BETA_ACCOUNT}-2026-10-06",
            f"reminder-market_close_day-{DEFAULT_ACCOUNT}-2026-10-06",
        ])

        # 同一账户同一天不重发；两个账户都各自保持一条。
        await account_reminders.check_positions(
            app, user_id=USER, account_id=DEFAULT_ACCOUNT, positions=provider_a.positions(),
            summary=provider_a.account_summary(""))
        assert len(reminder_notices(app, session_id, "reminder-market_close_day")) == 2

        # 风险度同理：两账户各一条，ID 带账户维度。
        for provider in (provider_b, provider_a):
            provider.summary_overrides = {"riskRatio": 99.5, "margin": 415_000, "equity": 500_000,
                                          "available": 85_000}
        await account_reminders.check_account(app, user_id=USER, account_id=BETA_ACCOUNT,
                                              summary=provider_b.account_summary(""))
        await account_reminders.check_account(app, user_id=USER, account_id=DEFAULT_ACCOUNT,
                                              summary=provider_a.account_summary(""))
        risks = reminder_notices(app, session_id, "reminder-risk_ratio")
        assert sorted(str(n["payload"]["turnId"]) for n in risks) == sorted([
            f"reminder-risk_ratio-{BETA_ACCOUNT}-2026-10-06",
            f"reminder-risk_ratio-{DEFAULT_ACCOUNT}-2026-10-06",
        ])
        await http.aclose()
    finally:
        app.state.store.close()


async def test_stale_snapshot_sends_no_reminder_and_writes_no_ledger_row(
        tmp_path: Path, monkeypatch):
    """#458：依据必须是同账户同一次读取的新鲜快照——过期（stale / isCached / 读不到）时
    不提醒也绝不落库（窗口恢复新鲜后那次提醒仍可发，只发一次）；账户快照读失败绝不影响
    /api/positions 本身。"""
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    app = application(tmp_path / "remind-stale.sqlite3", Gateway(), ReminderProvider())
    provider = app.state.data_provider
    provider.schedules["SHFE.au2612"] = CLOSE_15
    provider.schedules["SHFE.rb2610"] = CLOSE_15
    http = await login(app)
    assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
    session = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
    session_id = session["sessionId"]
    app.state.broker.subscribers[session_id] = 1

    def ledger_rows() -> int:
        with app.state.store.lock:
            return int(app.state.store.connection.execute(
                "SELECT COUNT(*) FROM reminder_notices").fetchone()[0])

    # 收盘提醒：快照 stale（网关重连中的典型形状）——不发、不落库，接口本身 200。
    provider.summary_overrides = {"dataStatus": "stale"}
    positions = await http.get("/api/positions")
    assert positions.status_code == 200 and len(positions.json()["items"]) == 2
    assert not reminder_notices(app, session_id) and ledger_rows() == 0
    # 标了 isCached 的缓存快照同样不算数。
    provider.summary_overrides = {"dataStatus": "normal", "isCached": True}
    assert (await http.get("/api/positions")).status_code == 200
    assert not reminder_notices(app, session_id) and ledger_rows() == 0
    # 窗口内恢复新鲜：那一次提醒照发（此前过期读没有消耗掉它），再读不重发。
    provider.summary_overrides = {}
    assert (await http.get("/api/positions")).status_code == 200
    assert len(reminder_notices(app, session_id, "reminder-market_close_day")) == 1
    assert ledger_rows() == 1
    assert (await http.get("/api/positions")).status_code == 200
    assert len(reminder_notices(app, session_id)) == 1

    # 风险度提醒：缓存的 99.5% 不提醒也不落库；新鲜后发一次。
    provider.summary_overrides = {"riskRatio": 99.5, "margin": 415_000, "equity": 500_000,
                                  "available": 85_000, "isCached": True}
    assert (await http.get("/api/account/summary")).status_code == 200
    assert not reminder_notices(app, session_id, "reminder-risk_ratio") and ledger_rows() == 1
    provider.summary_overrides = {"riskRatio": 99.5, "margin": 415_000, "equity": 500_000,
                                  "available": 85_000}
    assert (await http.get("/api/account/summary")).status_code == 200
    assert len(reminder_notices(app, session_id, "reminder-risk_ratio")) == 1
    assert ledger_rows() == 2

    # 账户快照读失败（涨破 RuntimeError）：持仓接口照常回，提醒只是不发。
    original_summary = provider.account_summary

    def broken_summary(turn_id: str) -> dict[str, Any]:
        raise RuntimeError("account unavailable")

    provider.account_summary = broken_summary  # type: ignore[method-assign]
    fresh = await http.get("/api/positions")
    assert fresh.status_code == 200 and len(fresh.json()["items"]) == 2
    provider.account_summary = original_summary  # type: ignore[method-assign]
    await http.aclose()
    app.state.store.close()


async def test_reminders_land_in_the_conversation_the_user_is_looking_at(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """落点规则（所有者 2026-10-08 决定，修“落进最近会话而不是正在看的对话”）：优先该用户
    当前在线订阅且绑定该提醒所属账户的会话（多个时取最近活跃的）；没有在线的取绑定该账户
    的最近会话；再没有才回退到用户最近会话。同一提醒仍只落一次——去重行与 outbox 同事务
    不变，绝不往所有会话各写一份。"""
    monkeypatch.setattr(account_reminders, "_now_ns", lambda: at(TUE, 14, 56))
    monkeypatch.setenv("AI_TRADER_ACCOUNTS_FILE", str(write_accounts_file(
        tmp_path / "accounts.json", [
            {"id": DEFAULT_ACCOUNT, "displayName": "账户甲", "dataMode": "fake"},
            {"id": BETA_ACCOUNT, "displayName": "账户乙", "dataMode": "fake"},
            {"id": GAMMA_ACCOUNT, "displayName": "账户丙", "dataMode": "fake"},
        ])))
    app = create_app(
        tmp_path / "remind-target.sqlite3", local_token="test-local-token",
        agent_mode="fake", data_mode="fake",
        account_providers={DEFAULT_ACCOUNT: ReminderProvider(), BETA_ACCOUNT: ReminderProvider(),
                           GAMMA_ACCOUNT: ReminderProvider()},
    )
    try:
        for account_id in (DEFAULT_ACCOUNT, BETA_ACCOUNT, GAMMA_ACCOUNT):
            app.state.store.grant_account(account_id=account_id, user_id=USER,
                                          role="owner", granted_by=USER)
        providers = {key: app.state.accounts.get(key).provider
                     for key in (DEFAULT_ACCOUNT, BETA_ACCOUNT, GAMMA_ACCOUNT)}
        providers[DEFAULT_ACCOUNT].schedules["SHFE.au2612"] = CLOSE_15   # 共享目录读默认账户的 provider
        http = await login(app)
        assert (await http.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
        # 四个会话，创建顺序（也是没有用户发言时的活跃顺序）：beta_older → viewing →
        # on_beta → newest_overall。viewing 绑账户甲、on_beta 绑账户乙，各自都不是全场最新。
        broker = app.state.broker

        def chat_on(account_id: str) -> str:
            return broker.create_session("local", "native", USER,
                                         trading_account_id=account_id).session_id

        beta_older = chat_on(BETA_ACCOUNT)
        viewing = chat_on(DEFAULT_ACCOUNT)
        on_beta = chat_on(BETA_ACCOUNT)
        newest_overall = chat_on(DEFAULT_ACCOUNT)
        sessions = (beta_older, viewing, on_beta, newest_overall)
        assert [item["sessionId"] for item in
                app.state.store.chat_sessions_by_recency(user_id=USER, mode=None)] == list(sessions[::-1])

        def notices(turn_prefix: str) -> dict[str, int]:
            return {session_id: len(reminder_notices(app, session_id, turn_prefix))
                    for session_id in sessions}

        # 在线且同账户优先于更新的离线会话（事故形状：用户看着老会话，API 刚建过新会话）。
        broker.subscribers[viewing] = 1
        await account_reminders.check_positions(
            app, user_id=USER, account_id=DEFAULT_ACCOUNT, positions=providers[DEFAULT_ACCOUNT].positions(),
            summary=providers[DEFAULT_ACCOUNT].account_summary(""))
        assert notices(f"reminder-market_close_day-{DEFAULT_ACCOUNT}") == {
            beta_older: 0, viewing: 1, on_beta: 0, newest_overall: 0}

        # 同账户多个在线时取最近活跃的那个（newest_overall 比 viewing 晚，两个都在线）。
        broker.subscribers[newest_overall] = 1
        providers[DEFAULT_ACCOUNT].summary_overrides = {"riskRatio": 99.5, "margin": 415_000,
                                                        "equity": 500_000, "available": 85_000}
        await account_reminders.check_account(app, user_id=USER, account_id=DEFAULT_ACCOUNT,
                                              summary=providers[DEFAULT_ACCOUNT].account_summary(""))
        assert notices(f"reminder-risk_ratio-{DEFAULT_ACCOUNT}") == {
            beta_older: 0, viewing: 0, on_beta: 0, newest_overall: 1}

        # 没有在线的：取绑定该账户的最近会话（账户乙 → on_beta，而不是全场最新的 newest_overall）。
        broker.subscribers.pop(viewing)
        broker.subscribers.pop(newest_overall)
        await account_reminders.check_positions(
            app, user_id=USER, account_id=BETA_ACCOUNT, positions=providers[BETA_ACCOUNT].positions(),
            summary=providers[BETA_ACCOUNT].account_summary(""))
        assert notices(f"reminder-market_close_day-{BETA_ACCOUNT}") == {
            beta_older: 0, viewing: 0, on_beta: 1, newest_overall: 0}

        # 回退路径：账户丙没有绑定会话，落到用户最近的会话（newest_overall）。
        await account_reminders.check_positions(
            app, user_id=USER, account_id=GAMMA_ACCOUNT, positions=providers[GAMMA_ACCOUNT].positions(),
            summary=providers[GAMMA_ACCOUNT].account_summary(""))
        assert notices(f"reminder-market_close_day-{GAMMA_ACCOUNT}") == {
            beta_older: 0, viewing: 0, on_beta: 0, newest_overall: 1}

        # 去重仍只一次：再读不重发，每个 turnId 全部会话加起来仍是一条。
        for account_id in (DEFAULT_ACCOUNT, BETA_ACCOUNT, GAMMA_ACCOUNT):
            await account_reminders.check_positions(
                app, user_id=USER, account_id=account_id, positions=providers[account_id].positions(),
                summary=providers[account_id].account_summary(""))
        assert sum(notices("reminder-market_close_day").values()) == 3   # 三个账户各一条，重读不重发
        assert sum(notices("reminder-risk_ratio").values()) == 1
        await http.aclose()
    finally:
        app.state.store.close()


def reminder_cards(app, session_id: str, turn_id: str) -> list[dict]:
    """提醒卡 (2026-10-04) published on a reminder's own turn."""
    return [event["payload"] for _, event in app.state.store.list_events(session_id)
            if event.get("name") == "card.reminder" and event.get("payload", {}).get("turnId") == turn_id]


def assert_no_order_action(card: dict) -> None:
    """A reminder card only asks the AI or opens a panel — never an order."""
    assert card["actions"] and {action["kind"] for action in card["actions"]} <= {"ask", "ui"}
    for action in card["actions"]:
        assert set(action.get("changes") or {}) <= {"panel", "news", "positionsTab", "symbol", "view"}
