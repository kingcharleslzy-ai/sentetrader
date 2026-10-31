"""今日作战表 (2026-10-25 第 5 批): the program's key levels, news strength, priority and 打法 matching, and the
route that ranks the signed-in user's 自选 with their own 打法 (no direction anywhere)."""
from __future__ import annotations

from datetime import datetime

from httpx import ASGITransport, AsyncClient

from trader_runtime.battle_sheet import key_levels, news_of, playbooks_for, priority_of, with_distances

NS = 1_000_000_000


def _bar(day: str, at: str, high: float, low: float, close: float, volume: float = 10) -> dict:
    moment = datetime.fromisoformat(f"{at}+08:00")
    return {"datetime": int(moment.timestamp() * NS), "tradingDay": day, "open": close, "high": high, "low": low, "close": close, "volume": volume}


def test_levels_come_from_the_previous_daily_bar_and_the_current_days_first_15_minutes() -> None:
    daily = [_bar("2026-10-22", "2026-10-22T00:00:00", 3200, 3100, 3150), _bar("2026-10-23", "2026-10-23T00:00:00", 3230, 3180, 3200),
             _bar("2026-10-26", "2026-10-26T00:00:00", 3260, 3190, 3240)]  # the forming day is not 昨
    intraday = [_bar("2026-10-23", "2026-10-23T14:55:00", 3205, 3199, 3200),
                _bar("2026-10-26", "2026-10-23T21:00:00", 3210, 3200, 3205, 30),
                _bar("2026-10-26", "2026-10-23T21:05:00", 3222, 3204, 3220, 10),
                _bar("2026-10-26", "2026-10-23T21:10:00", 3215, 3208, 3210, 20),
                _bar("2026-10-26", "2026-10-23T21:15:00", 3240, 3214, 3236, 40)]
    levels = {level["key"]: level["price"] for level in key_levels(daily, intraday, "2026-10-26", [])}
    assert levels["prev_high"] == 3230 and levels["prev_low"] == 3180
    assert levels["opening_high"] == 3222 and levels["opening_low"] == 3200
    typical = [(3210 + 3200 + 3205) / 3, (3222 + 3204 + 3220) / 3, (3215 + 3208 + 3210) / 3, (3240 + 3214 + 3236) / 3]
    assert abs(levels["vwap"] - sum(price * volume for price, volume in zip(typical, (30, 10, 20, 40))) / 100) < 1e-9
    # A series that starts inside the current day cannot vouch for its open: no 开盘区间 or 均价.
    assert {level["key"] for level in key_levels(daily, intraday[1:], "2026-10-26", [])} == {"prev_high", "prev_low"}
    # Before the day opens (no bar of it yet) only 昨高 / 昨低.
    assert {level["key"] for level in key_levels(daily, intraday[:1], "2026-10-26", [])} == {"prev_high", "prev_low"}


def test_priority_adds_news_strength_and_closeness_and_playbooks_match_by_words() -> None:
    assert news_of([{"tier": "major", "eventLabel": "钢厂减产", "publishedAt": "2026-10-26T09:12:00+08:00", "reports": 3, "id": "e1",
                     "ai": {"direction": "偏多"}}, {"tier": "minor", "title": "x", "id": "e2"}]) == {
        "strength": "强", "items": [{"id": "e1", "title": "钢厂减产", "at": "2026-10-26T09:12:00+08:00", "reports": 3}], "major": 1, "other": 0, "minor": 1}
    assert news_of([{"tier": "other", "title": "库存", "id": "e3"}])["strength"] == "中" and news_of([])["strength"] == "无"
    assert priority_of("强", 0.8) == ("高", 3) and priority_of("无", 0.2) == ("中", 2) and priority_of("中", 2.0) == ("低", 1)
    assert priority_of("无", None) == ("低", 0)
    levels = with_distances([{"key": "opening_high", "label": "开盘区间高", "price": 3222.0},
                             {"key": "prev_low", "label": "昨低", "price": 3100.0}], 3215.0)
    assert levels[0]["distancePercent"] == 0.22 and levels[1]["distancePercent"] == 3.58
    playbooks = [{"id": "p1", "name": "开盘突破", "entryRules": "突破开盘半小时高点"},
                 {"id": "p2", "name": "昨低反抽", "entryRules": ""},
                 {"id": "p3", "name": "回踩做", "entryRules": ""},
                 {"id": "p4", "name": "消息驱动", "entryRules": ""}]
    assert playbooks_for(playbooks, levels, "无") == [{"id": "p1", "name": "开盘突破", "why": "离开盘区间高 0.22%"},
                                                     {"id": "p3", "name": "回踩做", "why": "离开盘区间高 0.22%"}]
    assert [item["name"] for item in playbooks_for(playbooks, levels, "强")] == ["开盘突破", "回踩做", "消息驱动"]
    assert playbooks_for(playbooks, [], "无") == []


async def test_the_route_ranks_the_users_watchlist_with_their_playbooks(tmp_path, monkeypatch) -> None:
    from trader_runtime.main import create_app

    # The fake daily bars run past today: read them from a later day so they are all 昨.
    monkeypatch.setattr("trader_runtime.battle_sheet.trading_day_of", lambda *_: "2099-01-01")

    app = create_app(tmp_path / "runtime.sqlite3", local_token="sheet-test-token-with-32-characters!!", agent_mode="fake", data_mode="fake")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"}) as client:
            assert (await client.get("/api/research/sheet")).status_code == 401
            assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer sheet-test-token-with-32-characters!!"})).status_code == 200
            assert (await client.get("/api/research/sheet")).json()["rows"] == []
            assert (await client.put("/api/preferences", json={"watchlist": {"products": ["SHFE.rb", "COMEX.GC", "SHFE.au"]}})).status_code == 200
            assert (await client.post("/api/playbooks", json={"name": "昨高突破", "entryRules": "站上昨高"})).status_code == 201
            sheet = (await client.get("/api/research/sheet")).json()
            assert [row["product"] for row in sorted(sheet["rows"], key=lambda row: row["product"])] == ["SHFE.au", "SHFE.rb"]
            assert sheet["foreign"] == ["COMEX.GC"] and sheet["playbookCount"] == 1
            rb = next(row for row in sheet["rows"] if row["product"] == "SHFE.rb")
            assert rb["symbol"] == "SHFE.rb2610" and not rb.get("error"), rb
            assert {"prev_high", "prev_low"} <= {level["key"] for level in rb["levels"]}
            assert rb["priority"] in {"高", "中", "低"} and "stance" not in rb
            catalog = (await client.get("/api/agent/capabilities")).json()
            operation = next(item for item in catalog["operations"] if item["operation"] == "GET /api/research/sheet")
            assert operation["readOnly"] is True
    finally:
        app.state.research.close()
        app.state.store.close()
