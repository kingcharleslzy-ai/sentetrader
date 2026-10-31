"""回放里的到价提醒与收盘前/风险度提醒 (2026-10-03)：演练模式里铃铛、右键设提醒照常可用——
目录、参考价与盯价都只读回放行情（只限本次回放的合约，别的合约中文原因拒绝），触发与时间戳
都用回放时钟，投递只落在回放子进程自己的会话上；收盘前提醒按回放时钟落窗（时段优先合约目录、
没有退回回放行情自己的时段），风险度读回放账户（来源保证金与可用资金，风险度按同一版快照算，#478）；
提醒与落库都只在回放子进程的库里，主进程一条不留，回放结束一起消失。开关沿用主进程的偏好
（回放窗口里改设置本来就写到主进程）。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from test_replay import IDENTIFIER, SYMBOL, HistoricalSource
from trader_runtime.main import create_app

USER = "local-user"
# 日盘 15:00 收、夜盘收 02:30（跨零点的 end < start 写法）：与 fake 行情快照同一份时段。
SCHEDULE = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]],
            "night": [["21:00:00", "02:30:00"]]}


class ReplayAlertSource(HistoricalSource):
    """回放源的三个旋钮：图表行情保留 tradingSchedule（sidecar 的回放目录条目没有，收盘提醒
    退回这份）、账户改成 sidecar 回放的真实形状（来源 margin/available 与 marginRatio，没有
    riskRatio）、价格与回放时钟随手拨。"""

    def __init__(self) -> None:
        super().__init__()
        for chart in self.data["workbenches"].values():
            chart["quote"]["tradingSchedule"] = SCHEDULE
        self.data["account"] = {key: value for key, value in self.data["account"].items()
                                if key != "riskRatio"}
        self.data["account"].update(margin=0.0, available=self.data["account"]["equity"], marginRatio=0.0)


async def login(app) -> AsyncClient:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime",
                         headers={"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer test-replay-token"})
    assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-replay-token"})).status_code == 200
    return client


async def start_replay(tmp_path: Path, source: ReplayAlertSource | None = None):
    """主应用 + 一次回放：返回 (app, client, prefix, child, provider, source, lifespan)。"""
    source = source or ReplayAlertSource()
    app = create_app(tmp_path / "live.sqlite3", runtime_state_root=tmp_path, data_mode="tqkq-sidecar",
                     data_provider=source, agent_mode="fake", local_token="test-replay-token")
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = await login(app)
    created = await client.post("/api/replays", json={"symbol": SYMBOL, "tradingDay": "2026-09-04",
                                                     "initialBalance": 1_000_000})
    assert created.status_code == 201, created.text
    prefix = created.json()["runtimePath"]
    assert (await client.post(f"/api/replays/{IDENTIFIER}/control", json={"action": "step"})).status_code == 200
    context = app.state.replay_router.contexts[IDENTIFIER]
    return app, client, prefix, context.child, context.provider, source, lifespan


async def stop_replay(app, client, lifespan) -> None:
    await client.aclose()
    await lifespan.__aexit__(None, None, None)


def child_notices(child, session_id: str, prefix: str = "reminder-") -> list[dict[str, Any]]:
    return [event.get("payload") for _, event in child.state.store.list_events(session_id)
            if event.get("name") == "message.completed"
            and str(event.get("payload", {}).get("turnId", "")).startswith(prefix)]


def reminder_rows(store) -> int:
    with store.lock:
        return int(store.connection.execute("SELECT COUNT(*) FROM reminder_notices").fetchone()[0])


async def child_session(client, prefix: str, child) -> str:
    session = (await client.post(prefix + "/api/sessions", json={"mode": "local"})).json()
    session_id = session["sessionId"]
    child.state.broker.subscribers[session_id] = 1  # 一个挂着流的回放窗口
    return session_id


async def test_replay_price_alerts_cross_on_the_replay_clock_and_stay_in_the_child(tmp_path: Path):
    app, client, prefix, child, provider, source, lifespan = await start_replay(tmp_path)
    try:
        session_id = await child_session(client, prefix, child)
        app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)  # 通话挂在主进程

        # 别的合约提醒不了回放价——中文原因原话拒绝；列表接口在回放里也不再 503。
        refused = await client.post(prefix + "/api/market/alerts",
                                    json={"symbol": "SHFE.rb2610", "price": 3300})
        assert refused.status_code == 422 and "回放里只能给本次回放的合约设提醒" in refused.json()["detail"]
        listed = (await client.get(prefix + "/api/market/alerts")).json()
        assert listed == {"active": [], "triggered": []}

        # 方向按回放最新价判定（823.5 之下 823.0 → below）。
        made = await client.post(prefix + "/api/market/alerts",
                                 json={"symbol": SYMBOL, "price": 823.0, "note": "回落就走"})
        assert made.status_code == 201, made.text
        alert = made.json()["alert"]
        assert alert["direction"] == "below" and alert["name"] == "沪金2612" and alert["status"] == "active"

        await child.state.price_alerts._tick()  # 现价 823.5 在目标上方：不触发
        assert [item["id"] for item in child.state.store.list_price_alerts(user_id=USER)["active"]] == [alert["id"]]

        source.data["market"]["price"] = 822.5  # 回放价跌破 823
        await provider.command("state")
        await child.state.price_alerts._tick()
        await child.state.price_alerts._tick()  # 穿越轮绝不投递两次
        rows = child.state.store.list_price_alerts(user_id=USER)
        assert not rows["active"] and rows["triggered"][0]["id"] == alert["id"]
        fired = rows["triggered"][0]
        assert fired["triggerPrice"] == "822.5"
        # 触发时刻是回放时钟的“现在”，不是墙上时钟（step 已把回放拨到 13:01）。
        assert fired["triggeredAt"] == "2026-09-03T13:01:00+00:00"

        notices = child_notices(child, session_id, "pricealert-")
        assert len(notices) == 1 and str(notices[0]["turnId"]) == f"pricealert-{alert['id']}"
        assert "到价提醒：沪金2612 跌到 823（现 822.5）" in str(notices[0]["text"])
        assert "备注：回落就走。" in str(notices[0]["text"])
        feed = [event for _, event in child.state.broker.transient_events[session_id]
                if event.get("name") == "feed.price-alert"]
        assert len(feed) == 1 and feed[0]["payload"]["lastPrice"] == 822.5
        # 口播找的是主进程的语音服务（回放绑定的通话挂在那边）；条目带回放 id，
        # 只念给绑到同一回放的通话（#521 R2）。
        announcements = app.state.voice_realtime._pending_announcements[USER]
        assert announcements[-1]["kind"] == f"price-alert-{alert['id']}"
        assert announcements[-1]["replayId"] == IDENTIFIER

        # 主进程一条不留：提醒是回放子进程自己的，随回放一起消失。
        empty = app.state.store.list_price_alerts(user_id=USER)
        assert empty == {"active": [], "triggered": []}
    finally:
        await stop_replay(app, client, lifespan)


async def test_replay_close_reminder_fires_once_on_the_replay_clock(tmp_path: Path):
    app, client, prefix, child, provider, source, lifespan = await start_replay(tmp_path)
    try:
        session_id = await child_session(client, prefix, child)
        app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)
        # 开关在主进程的偏好里（回放窗口里改设置本来就写到那边）。
        assert (await client.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
        source.data["positions"] = [{"symbol": SYMBOL, "direction": "long", "volume": 2,
                                     "unrealizedPnl": 2670, "todayVolume": 2, "historicalVolume": 0,
                                     "frozenToday": 0, "frozenHistorical": 0}]
        source.data["clock"] = "2026-09-03T06:56:00+00:00"  # 回放时钟 14:56：日盘 15:00 收盘前 4 分钟
        await provider.command("state")

        assert (await client.get(prefix + "/api/positions")).status_code == 200
        notices = child_notices(child, session_id, "reminder-market_close_day")
        assert len(notices) == 1
        text = str(notices[0]["text"])
        assert "收盘提醒：4 分钟后日盘收盘" in text and "沪金2612 多 2 手（浮盈 +2,670）" in text
        assert str(notices[0]["turnId"]).startswith("reminder-market_close_day-") and "2026-09-03" in str(notices[0]["turnId"])
        assert str(notices[0]["at"]).startswith("2026-09-03T06:56:00")  # 投递时间戳也是回放时钟
        feed = [event for _, event in child.state.broker.transient_events[session_id]
                if event.get("name") == "feed.reminder"]
        assert len(feed) == 1 and feed[0]["payload"]["title"] == "收盘提醒"
        announcements = app.state.voice_realtime._pending_announcements[USER]
        assert announcements[-1]["text"] == text
        assert announcements[-1]["replayId"] == IDENTIFIER   # 只念给同一回放的通话（#521 R2）

        # 同一交易日再读不重发；主进程的提醒台账一条不留。
        assert (await client.get(prefix + "/api/positions")).status_code == 200
        assert len(child_notices(child, session_id)) == 1
        assert reminder_rows(app.state.store) == 0 and reminder_rows(child.state.store) == 1
    finally:
        await stop_replay(app, client, lifespan)


async def test_replay_close_reminder_borrows_a_same_product_schedule_for_an_expired_contract(tmp_path: Path):
    """#486：回放已到期的合约——导出与图表行情都没有时段，父进程实时目录（只列在市合约）
    也没有这个完整代码；同交易所同品种各月份时段相同，借在市的 au2706 那份照样提醒。
    别的品种（同交易所的 ag）的时段不借。"""
    source = ReplayAlertSource()
    for chart in source.data["workbenches"].values():
        chart["quote"].pop("tradingSchedule", None)
    morning_only = {"day": [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"]]}
    source.instrument_catalog = lambda: {"items": [  # type: ignore[method-assign]
        {"symbol": "SHFE.ag2706", "tradingSchedule": morning_only},
        {"symbol": "SHFE.au2706", "tradingSchedule": SCHEDULE}]}
    app, client, prefix, child, provider, source, lifespan = await start_replay(tmp_path, source)
    try:
        session_id = await child_session(client, prefix, child)
        assert (await client.put("/api/preferences", json={"notify": {"marketCloseReminder": True}})).status_code == 200
        source.data["positions"] = [{"symbol": SYMBOL, "direction": "long", "volume": 1,
                                     "unrealizedPnl": 0, "todayVolume": 1, "historicalVolume": 0,
                                     "frozenToday": 0, "frozenHistorical": 0}]
        source.data["clock"] = "2026-09-03T06:56:00+00:00"  # 回放时钟 14:56
        await provider.command("state")

        assert (await client.get(prefix + "/api/positions")).status_code == 200
        notices = child_notices(child, session_id, "reminder-market_close_day")
        assert len(notices) == 1 and "收盘提醒：4 分钟后日盘收盘" in str(notices[0]["text"])
    finally:
        await stop_replay(app, client, lifespan)


async def test_replay_risk_reminder_reads_the_replay_account(tmp_path: Path):
    app, client, prefix, child, provider, source, lifespan = await start_replay(tmp_path)
    try:
        session_id = await child_session(client, prefix, child)
        # sidecar 回放账户带来源保证金与可用资金：风险度 = 保证金 ÷ 权益，与 marginRatio 同口径。
        source.data["account"].update(equity=1_000_000, margin=831_000, available=169_000, marginRatio=83.1)
        await provider.command("state")
        summary = (await client.get(prefix + "/api/account/summary")).json()
        assert summary["riskRatio"] == summary["marginRatio"] == pytest.approx(83.1)
        assert summary["margin"] == pytest.approx(831_000) and summary["available"] == pytest.approx(169_000)

        notices = child_notices(child, session_id, "reminder-risk_ratio")
        assert len(notices) == 1
        assert str(notices[0]["text"]) == ("风险度提醒：现在风险度 83%（保证金占用 83.1 万 / 权益 100 万），"
                                           "越高越接近被强平；可用 16.9 万。")
        assert (await client.get(prefix + "/api/account/summary")).status_code == 200
        assert len(child_notices(child, session_id, "reminder-risk_ratio")) == 1  # 同一交易日只发一次
        assert reminder_rows(app.state.store) == 0
    finally:
        await stop_replay(app, client, lifespan)


async def test_replay_chart_drawings_live_in_the_child_store(tmp_path: Path):
    app, client, prefix, child, _provider, _source, lifespan = await start_replay(tmp_path)
    try:
        refused = await client.post(prefix + "/api/market/drawings",
                                    json={"symbol": "SHFE.rb2610", "kind": "hline", "points": {"price": 3300}})
        assert refused.status_code == 422 and "回放里只能画本次回放的合约" in refused.json()["detail"]
        made = await client.post(prefix + "/api/market/drawings",
                                 json={"symbol": SYMBOL, "kind": "hline", "points": {"price": 820}})
        assert made.status_code == 201, made.text
        drawing = made.json()["drawing"]
        assert drawing["symbol"] == SYMBOL
        listed = (await client.get(prefix + "/api/market/drawings")).json()["drawings"]
        assert [item["id"] for item in listed] == [drawing["id"]]  # 画线接口在回放里不再 503
        # 主进程的画线一条不留。
        assert app.state.store.list_chart_drawings(user_id=USER, symbol=SYMBOL) == []
        assert child.state.store.list_chart_drawings(user_id=USER, symbol=SYMBOL)[0]["id"] == drawing["id"]
    finally:
        await stop_replay(app, client, lifespan)
