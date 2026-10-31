"""策略编辑卡 (2026-10-08): the habits prefill, saving an edited plan as a new version without losing what
the card cannot show, the live preview backtest with per-condition counts, and the retired guide."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from test_ai_annotations import with_tools
from test_strategy_backtest import cond, plan
from trader_runtime.main import create_app
from trader_runtime.strategy_editor import MIN_TRADES, habit_prefill
from trader_runtime.trading_calendar import CST

RB, AU = "SHFE.rb2701", "SHFE.au2612"
CATALOG = [
    {"symbol": "SHFE.rb2701", "exchangeId": "SHFE", "productId": "rb", "instrumentName": "螺纹钢2701", "priceTick": 1.0,
     "isMain": True},
    {"symbol": "SHFE.rb2610", "exchangeId": "SHFE", "productId": "rb", "instrumentName": "螺纹钢2610", "priceTick": 1.0,
     "isMain": False},
    {"symbol": "SHFE.au2612", "exchangeId": "SHFE", "productId": "au", "instrumentName": "沪金2612", "priceTick": 0.02,
     "isMain": True},
]
HEADERS = {"Origin": "http://127.0.0.1:5173", "Authorization": "Bearer editor-token"}

# The pure habit_prefill tests share one frozen clock (#657): the samples and the call read the same
# now instead of the wall clock, so no sample ever closes in the future and the 18:00 trading-day
# turn or a weekend cannot flip what the window keeps. A Friday midday: every sample below closes
# before it.
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=CST)


def weekdays_back(count: int, now: datetime | None = None) -> list[datetime]:
    """The last `count` weekdays up to `now` (Beijing; defaults to the real clock for the route test,
    whose samples close on their own trading day), newest first."""
    day = (now or datetime.now(CST)).replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days


def ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1e9)


def round_trip(identifier: str, symbol: str, direction: str, day: datetime, *, hour: int = 10, ticks: float = 10,
               loss: bool = False, volume: int = 1, hold_days: int = 0, tick: float = 1.0) -> dict:
    opened = day.replace(hour=hour, minute=5)
    closed = opened + timedelta(days=hold_days, minutes=40)
    move = (1 if direction == "long" else -1) * (-1 if loss else 1)
    pnl = -100.0 if loss else 100.0
    return {"id": f"{identifier}:c", "symbol": symbol, "direction": direction, "volume": volume,
            "openTime": ns(opened), "closeTime": ns(closed), "openPrice": 3000.0, "closePrice": 3000.0 + move * ticks * tick,
            "net": pnl, "gross": pnl, "openTradeId": identifier, "openOrderId": f"order-{identifier}"}


def test_prefill_needs_five_closed_trades_in_the_last_twenty_trading_days():
    days = weekdays_back(30, NOW)
    recent = [round_trip(f"r{i}", RB, "long", days[i]) for i in range(MIN_TRADES - 1)]
    old = [round_trip(f"o{i}", RB, "long", days[25 + i]) for i in range(3)]   # older than 20 trading days
    result = habit_prefill(recent + old, catalog_items=CATALOG, now_ns=ns(NOW))
    assert result["eligible"] is False and result["fields"] == {}
    assert result["sample"]["trades"] == MIN_TRADES - 1 and "不到 5 笔" in result["reason"]
    assert "开仓条件不预填" in result["notice"]


def test_prefill_fields_follow_the_trades_with_their_basis_and_ids():
    days = weekdays_back(12, NOW)
    rounds = [round_trip(f"rb{i}", "SHFE.rb2610", "long" if i < 7 else "short", days[i], hour=10 if i < 8 else 14,
                         ticks=[12, 14, 15, 9][i % 4], loss=i % 2 == 0, volume=2 if i in (3, 5) else 1)
              for i in range(9)]
    rounds.append(round_trip("au0", AU, "long", days[9], hour=9, tick=0.02))
    # One opening order closed in two fills is one trade of two lots, not two of one.
    split = round_trip("rb9", RB, "long", days[10], hour=10)
    rounds += [{**split, "id": "rb9:c1"}, {**split, "id": "rb9:c2"}]
    result = habit_prefill(rounds, catalog_items=CATALOG, now_ns=ns(NOW))
    assert result["eligible"] and result["sample"]["trades"] == 11
    fields = result["fields"]
    # 合约：最常做的品种取其主力（目录里的 isMain），不是成交里的旧月份。
    assert fields["symbol"]["value"] == RB and fields["symbol"]["label"] == "螺纹钢 主力（rb2701）"
    assert fields["symbol"]["basis"] == "11 笔里 10 笔是螺纹钢" and "au0:c" not in fields["symbol"]["tradeIds"]
    # 方向：做多 9/11 ≥ 70%。
    assert fields["direction"]["value"] == "long" and "11 笔里 9 笔做多" == fields["direction"]["basis"]
    # 止损：螺纹亏损单（i = 0, 2, 4, 6, 8 → 12, 15, 12, 15, 12 跳）出场跳数的中位数。
    assert fields["stopLossTicks"]["value"] == 12 and fields["stopLossTicks"]["basis"] == "螺纹钢 5 笔亏损单出场中位 12 跳"
    assert set(fields["stopLossTicks"]["tradeIds"]) == {"rb0:c", "rb2:c", "rb4:c", "rb6:c", "rb8:c"}
    # 都没留隔夜 → 收盘前 5 分钟平仓。
    assert fields["closeBeforeEndMinutes"]["value"] == 5
    # 10/11 在上午开仓（≥ 70%）。
    assert fields["entryWindow"]["value"] == {"start": "09:00", "end": "11:30"}
    assert fields["entryWindow"]["basis"] == "11 笔里 10 笔在上午（09:00–11:30）开仓"
    # 每次手数：众数（两笔 2 手 + 拆成两次平的 2 手，其余 1 手）。
    assert fields["lots"]["value"] == 1 and fields["lots"]["basis"] == "11 笔里 8 笔每次 1 手"
    assert result["summary"] == "依据：近 20 个交易日 11 笔已平仓成交（螺纹钢 10 笔）。"
    assert len(result["trades"]) == 11 and result["trades"][0]["id"] == "rb0:c"   # newest first (rb0 is today)


@pytest.mark.parametrize("clock", [
    datetime(2026, 10, 8, 17, 59, tzinfo=CST),   # minutes before the 18:00 trading-day turn
    datetime(2026, 10, 9, 0, 15, tzinfo=CST),    # small hours: yesterday's night session is still today's trading day
    datetime(2026, 10, 9, 12, 0, tzinfo=CST),    # a midday run
])
def test_prefill_leaves_out_what_the_trades_do_not_settle(clock):
    days = weekdays_back(8, clock)
    # The overnight round trip closes half an hour before the frozen clock, on the clock's own
    # trading day: closed (never a future fill), inside the 20-day window and still crossing days
    # whichever side of 18:00 or the weekend the clock sits on.
    overnight_closed = clock - timedelta(minutes=30)
    overnight = {"id": "t0:c", "symbol": RB, "direction": "short", "volume": 1,
                 "openTime": ns(overnight_closed - timedelta(days=1)), "closeTime": ns(overnight_closed),
                 "openPrice": 3000.0, "closePrice": 2990.0, "net": -100.0, "gross": -100.0,
                 "openTradeId": "t0", "openOrderId": "order-t0"}
    rounds = [overnight] + [round_trip(f"t{i}", RB, "long" if i % 2 else "short", days[i + 1], hour=(10, 14, 21)[i % 3],
                                       loss=i < 2) for i in range(1, 6)]
    result = habit_prefill(rounds, catalog_items=CATALOG, now_ns=ns(clock))
    assert result["sample"]["trades"] == 6
    assert all(entry["closeTime"] <= ns(clock) for entry in result["trades"])
    fields = result["fields"]
    # 多空各半、时段分散、有一笔留了隔夜、亏损单不到 3 笔：这些都不填；合约与手数照填。
    assert set(fields) == {"symbol", "lots"}


@pytest.mark.asyncio
async def test_prefill_route_reads_only_the_account_of_the_request(tmp_path):
    app = create_app(tmp_path / "editor.sqlite3", runtime_state_root=tmp_path, agent_mode="fake",
                     local_token="editor-token")
    days = weekdays_back(8)

    def fills(prefix: str, symbol: str, count: int) -> list[dict]:
        rows = []
        for i in range(count):
            opened = days[i].replace(hour=10, minute=5)
            for side, offset, moment, price in (("BUY", "OPEN", opened, 3000.0),
                                                ("SELL", "CLOSETODAY", opened + timedelta(minutes=30), 2990.0)):
                rows.append({"tradeId": f"{prefix}{i}-{offset}", "clientOrderId": f"{prefix}-order-{i}-{offset}",
                             "symbol": symbol, "direction": side, "offset": offset, "price": price, "volume": 1,
                             "tradeDateTime": ns(moment), "tradingDay": moment.strftime("%Y-%m-%d")})
        return rows

    async with app.router.lifespan_context(app):
        store = app.state.store
        store.upsert_account_trades(fills("rb", "SHFE.rb2610", 5), {"SHFE.rb2610": 10.0})
        store.upsert_account_trades(fills("au", AU, 7), {AU: 1000.0}, trading_account_id="tqkq-sim:someone-else")
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            body = (await client.get("/api/strategy-plans/prefill")).json()
    assert body["eligible"] and body["sample"]["trades"] == 5
    assert body["fields"]["symbol"]["value"].startswith("SHFE.rb")
    assert all(item["symbol"] == "SHFE.rb2610" for item in body["trades"])


@pytest.mark.asyncio
async def test_saving_an_edit_is_a_new_version_that_keeps_what_the_card_cannot_show(tmp_path):
    app = create_app(tmp_path / "editor-save.sqlite3", runtime_state_root=tmp_path, agent_mode="fake",
                     local_token="editor-token")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            made = await client.post("/api/strategy-templates/double_ma/instantiate", json={"name": "均线"})
            assert made.status_code == 201, made.text
            base = made.json()
            assert base["definition"]["templateSource"]["defaults"] is True
            edited = {**base["definition"]["plan"], "stopLossTicks": 33}
            saved = await client.post("/api/strategy-plans", json={
                "plan": edited, "instrumentScope": base["definition"]["instrumentScope"], "baseStrategyId": base["strategyId"]})
            assert saved.status_code == 201, saved.text
            item = saved.json()
            assert item["name"] == "均线" and item["version"] == base["version"] + 1 and item["strategyId"] != base["strategyId"]
            assert item["definition"]["plan"]["stopLossTicks"] == 33
            source = item["definition"]["templateSource"]
            assert source["templateId"] == "double_ma" and source["modified"] is True and source["defaults"] is False
            original = (await client.get("/api/strategy-plans")).json()["items"]
            assert {entry["strategyId"] for entry in original} >= {base["strategyId"], item["strategyId"]}
            # 新建：从模板起点、和模板默认一样时记为模板默认；重名加 (2)。
            template = (await client.get("/api/strategy-templates/double_ma")).json()
            fresh = (await client.post("/api/strategy-plans", json={
                "plan": {**template["plan"], "name": "均线"}, "templateId": "double_ma"})).json()
            assert fresh["name"] == "均线 (2)" and fresh["definition"]["templateSource"]["defaults"] is True
            assert (await client.post("/api/strategy-plans", json={
                "plan": edited, "baseStrategyId": "nope"})).status_code == 404


@pytest.mark.asyncio
async def test_preview_backtest_counts_each_condition_and_is_not_stored(tmp_path):
    app = create_app(tmp_path / "editor-preview.sqlite3", runtime_state_root=tmp_path, agent_mode="fake",
                     local_token="editor-token")
    raw = plan(entry=[cond("金叉", "ma", "cross_up", {"indicator": "ma", "period": 20}, tf=300, period=5),
                      cond("站上", "close", "gt", {"indicator": "ma", "period": 60}, tf=300)]).model_dump()
    raw["rules"]["entryLong"]["join"] = "any"
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            response = await client.post("/api/strategy-backtests", json={
                "plan": raw, "symbol": AU, "tradingDays": 60, "preview": True})
            assert response.status_code == 200, response.text
            body = response.json()
            group = body["diagnosis"]["entryLong"]
            assert group["join"] == "any" and [row["label"] for row in group["conditions"]] == ["金叉", "站上"]
            alone = [row["alone"] for row in group["conditions"]]
            assert min(alone) > 0 and 0 < group["anyPass"] <= sum(alone)
            assert body["planSummary"] and body["advice"] == []
            assert (await client.get("/api/strategy-backtests")).json()["items"] == []


@pytest.mark.asyncio
async def test_the_retired_guide_answers_410_and_is_not_in_the_catalog(tmp_path):
    app = create_app(tmp_path / "editor-guide.sqlite3", runtime_state_root=tmp_path, agent_mode="fake",
                     local_token="editor-token")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=HEADERS) as client:
            started = await client.post("/api/strategy-plans/guide", json={"symbol": AU})
            assert started.status_code == 410 and "策略编辑卡" in started.json()["detail"]
            assert (await client.get("/api/strategy-plans/guide")).status_code == 410
            with_tools(app)
            operations = {item["operation"] for item in (await client.get("/api/agent/capabilities")).json()["operations"]}
            assert "GET /api/strategy-plans/prefill" in operations
            assert not {name for name in operations if "/guide" in name}
