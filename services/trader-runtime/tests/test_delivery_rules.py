"""交割月提醒 + 合约信息卡 (2026-10-02)：纯函数、快照透出与主动提醒。

日历事实都按真实日历核对过：2026-09-01 是周二、2026-09-30 是周三、2026-12-31 是周四、
2026-09-18 是交割月（2026-09）的第三个周五。交易日按工作日估算（没有节假日日历），
测试里也固定用工作日。"""
from __future__ import annotations

from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

from trader_runtime.delivery_rules import contract_payload, delivery_notice, trading_today
from trader_runtime.store import RuntimeStore

RB2610 = {"symbol": "SHFE.rb2610", "exchangeId": "SHFE", "instrumentId": "rb2610",
          "productId": "rb", "deliveryYear": 2026, "deliveryMonth": 10}
I2701 = {"symbol": "DCE.i2701", "exchangeId": "DCE", "instrumentId": "i2701",
         "productId": "i", "deliveryYear": 2027, "deliveryMonth": 1}
IF2609 = {"symbol": "CFFEX.IF2609", "exchangeId": "CFFEX", "instrumentId": "IF2609",
          "productId": "IF", "deliveryYear": 2026, "deliveryMonth": 9}


def test_commodity_deadline_is_the_prior_months_last_trading_day():
    # 2026-10 交割 → 最后能持仓的交易日是 2026-09-30（周三，9 月最后一个工作日）。
    notice = delivery_notice(RB2610, date(2026, 9, 17))
    assert notice["kind"] == "delivery_month"
    assert notice["lastHoldDay"] == "2026-09-30"
    assert notice["tradingDaysLeft"] == 10
    assert notice["level"] == "warning"
    assert "9 月 30 日" in notice["text"] and "强平" in notice["text"]


def test_holiday_months_roll_the_deadline_back_to_a_weekday():
    # 2027-01 交割 → 前一个月是 2026-12，月末 12-31 本身是周四；再往前 2027-04-30 是周五、
    # 2027-05-31 周一。跨年与跨节假日（国庆、春节所在月）都只按“月末最后一个工作日”取。
    assert delivery_notice(I2701, date(2026, 12, 18))["lastHoldDay"] == "2026-12-31"
    jan = {**RB2610, "deliveryYear": 2027, "deliveryMonth": 5}
    assert delivery_notice(jan, date(2027, 4, 19))["lastHoldDay"] == "2027-04-30"


def test_december_to_january_wrap_counts_into_the_prior_year():
    # 12 → 1 月跨年：2027-01 交割的死线在 2026-12-31，不是 2027-。
    notice = delivery_notice(I2701, date(2026, 12, 18))
    assert notice["tradingDaysLeft"] == 10 and notice["level"] == "warning"


def test_thresholds_ten_and_three_trading_days():
    assert delivery_notice(RB2610, date(2026, 9, 16)) is None        # 11 个交易日：不提醒
    assert delivery_notice(RB2610, date(2026, 9, 17))["level"] == "warning"   # 10
    assert delivery_notice(RB2610, date(2026, 9, 25))["level"] == "warning"   # 4
    assert delivery_notice(RB2610, date(2026, 9, 28))["level"] == "danger"    # 3
    assert delivery_notice(RB2610, date(2026, 9, 30))["tradingDaysLeft"] == 1  # 今天就是死线
    assert delivery_notice(IF2609, date(2026, 9, 4)) is None         # 股指 11 个交易日
    assert delivery_notice(IF2609, date(2026, 9, 7))["level"] == "warning"
    assert delivery_notice(IF2609, date(2026, 9, 16))["level"] == "danger"


def test_inside_the_delivery_month_is_danger_zero_days():
    notice = delivery_notice(RB2610, date(2026, 10, 3))  # 周六也已在交割月
    assert notice["level"] == "danger" and notice["tradingDaysLeft"] == 0
    assert "已进入交割月" in notice["text"]


def test_cash_settled_index_futures_expire_on_the_third_friday():
    notice = delivery_notice(IF2609, date(2026, 9, 7))
    assert notice["kind"] == "expiry" and notice["lastHoldDay"] == "2026-09-18"
    assert notice["tradingDaysLeft"] == 10 and notice["level"] == "warning"
    assert "现金交割" in notice["text"] and "强平" not in notice["text"]
    assert delivery_notice(IF2609, date(2026, 9, 21)) is None  # 过了第三个周五：已到期


def test_contracts_without_dates_or_past_delivery_do_not_remind():
    assert delivery_notice({"symbol": "KQ.m@SHFE.rb", "instrumentId": "KQ.m@SHFE.rb"},
                           date(2026, 9, 28)) is None            # 主连没有交割月
    au = {**RB2610, "symbol": "SHFE.au2612", "instrumentId": "au2612", "deliveryMonth": 12}
    assert delivery_notice(au, date(2026, 10, 2)) is None        # 还有 42 个交易日
    assert delivery_notice(RB2610, date(2026, 11, 2)) is None    # 交割月都过完了


def test_missing_catalog_dates_fall_back_to_the_symbol_tail():
    hc = {"symbol": "SHFE.hc2611", "exchangeId": "SHFE", "instrumentId": "hc2611",
          "productId": "hc", "deliveryYear": 0, "deliveryMonth": 0}
    notice = delivery_notice(hc, date(2026, 10, 19))             # 2611 → 2026-11，死线 10-30
    assert notice["lastHoldDay"] == "2026-10-30" and notice["tradingDaysLeft"] == 10


def test_contract_payload_passes_money_fields_and_schedule():
    payload = contract_payload({"volumeMultiple": 1000.0, "priceTick": 0.5, "marginPerLot": 82_350.0,
                                "commissionPerLot": 24.71,
                                "tradingSchedule": {"day": [["09:00:00", "10:15:00"]], "night": []}})
    assert payload == {"multiplier": 1000.0, "priceTick": 0.5, "marginPerLot": 82_350.0,
                       "commissionPerLot": 24.71,
                       "tradingSchedule": {"day": [["09:00:00", "10:15:00"]], "night": []}}
    empty = contract_payload({})
    assert empty["multiplier"] is None and empty["tradingSchedule"] is None


def test_trading_today_rolls_evenings_and_weekends():
    # 2026-10-02 是周五：周五 20:00 的夜盘属于下一交易日（跳过周末到周一 10-05）。
    assert trading_today(1_790_913_600) == date(2026, 10, 2)     # 2026-10-02 12:00 CST
    assert trading_today(1_790_942_400) == date(2026, 10, 5)     # 2026-10-02 20:00 CST


@pytest.mark.asyncio
async def test_catalog_and_snapshot_carry_the_notice_and_contract(tmp_path, monkeypatch):
    import trader_runtime.shared_market as shared_market
    from trader_runtime.main import create_app

    monkeypatch.setattr(shared_market, "trading_today", lambda *_, **__: date(2026, 9, 28))
    app = create_app(tmp_path / "state.db", agent_mode="fake", data_mode="fake",
                     local_token="test-token", cors_origins={"http://localhost"})
    headers = {"Origin": "http://localhost", "Authorization": "Bearer test-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            catalog = (await client.get("/api/instruments")).json()
            rb = next(item for item in catalog["items"] if item["symbol"] == "SHFE.rb2610")
            assert rb["deliveryNotice"]["lastHoldDay"] == "2026-09-30"
            assert rb["deliveryNotice"]["level"] == "danger"
            assert all("deliveryNotice" in item for item in catalog["items"])
            if_item = next(item for item in catalog["items"] if item["symbol"] == "CFFEX.IF2609")
            assert if_item["deliveryNotice"] is None  # 9 月合约在 09-28 已到期
            snapshot = (await client.get("/api/market/SHFE.rb2610")).json()
            # fake 行情对任何合约都回同一份沪金样例数据；这里只看字段链路通了没有。
            assert snapshot["contract"]["multiplier"] == 1000.0
            assert snapshot["contract"]["priceTick"] == 0.5
            assert snapshot["contract"]["marginPerLot"] == 82_350.0
            assert snapshot["contract"]["tradingSchedule"]["day"][0] == ["09:00", "10:15"]
            assert snapshot["deliveryNotice"]["lastHoldDay"] == "2026-09-30"
    await app.state.guests.close()
    app.state.store.close()


@pytest.mark.asyncio
async def test_the_account_loop_reminds_once_per_contract_per_day_and_client_reads_do_not(tmp_path, monkeypatch):
    """#932: 交割提醒 is judged by the Runtime's own 30 s account loop — a positions read or a feed tick no
    longer reminds; the loop sends it once per contract per day, across rounds and a restart."""
    import trader_runtime.delivery_reminders as reminders
    from trader_runtime.main import create_app

    monkeypatch.setattr(reminders, "trading_today", lambda *_, **__: date(2026, 9, 28))
    monkeypatch.setattr("trader_runtime.shared_market.trading_today", lambda *_, **__: date(2026, 9, 28))
    app = create_app(tmp_path / "state.db", agent_mode="fake", data_mode="fake",
                     local_token="test-token", cors_origins={"http://localhost"})
    headers = {"Origin": "http://localhost", "Authorization": "Bearer test-token"}
    spoken: list[str] = []

    class Voice:
        def user_calls(self, user_id):
            return 1

        def announce(self, kind, text, user_id=None, replay_id=None, key=None):
            spoken.append(text)

    app.state.voice_realtime = Voice()

    def delivered(session: str) -> list[dict]:
        return [event for _, event in app.state.store.list_events(session)
                if str(event["payload"].get("turnId", "")).startswith("delivery-")]

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            session = (await client.post("/api/sessions/restore",
                                         json={"mode": "local", "agentId": "native"})).json()["sessionId"]
            app.state.broker.subscribers[session] = 1
            first = (await client.get("/api/positions")).json()
            assert any(item["symbol"] == "SHFE.hc2610" for item in first["items"])
            await app.state.runtime_feed.tick()
            assert delivered(session) == []   # client reads no longer judge it
            await app.state.account_reminder_loop.run_once()
            notices = [event for event in delivered(session) if event["name"] == "message.completed"]
            assert len(notices) == 1  # 只有 hc2610 带 danger；au2612/ag2612 还远
            text = notices[0]["payload"]["text"]
            # 提醒卡 (2026-10-04): same turn as the text, the last holding day and the days left, and only
            # ask-the-AI / open-a-panel actions — never an order button.
            cards = [event["payload"] for event in delivered(session) if event["name"] == "card.reminder"]
            assert len(cards) == 1 and cards[0]["turnId"] == notices[0]["payload"]["turnId"]
            assert cards[0]["kind"] == "delivery" and cards[0]["symbol"] == "SHFE.hc2610"
            assert {figure["label"] for figure in cards[0]["figures"]} == {"最后持仓日", "剩余交易日"}
            assert {action["kind"] for action in cards[0]["actions"]} <= {"ask", "ui"}
            assert "热轧卷板2610" in text and "平仓" in text
            assert notices[0]["payload"]["turnId"] == "delivery-SHFE.hc2610-2026-09-28"
            assert spoken == [text]  # 通话在线时口播同一条
            feeds = [event["payload"] for _, event in app.state.broker.transient_events[session]
                     if event["name"] == "feed.reminder"]
            assert [feed["kind"] for feed in feeds] == ["delivery"]
            # 同一天下一轮不再发。
            await app.state.account_reminder_loop.run_once()
            assert len([event for event in delivered(session) if event["name"] == "message.completed"]) == 1
            user = str(app.state.store.get_session(session)["userId"])
    # “重启不重发”：同一份库上有这条提醒的认领，也有上一版仍读的旧账本行。
    reopened = RuntimeStore(tmp_path / "state.db")
    assert reopened.reminder_claimed(user_id=user, kind="delivery", dedupe_key="SHFE.hc2610:2026-09-28")
    assert reopened.record_delivery_notice(user_id=user, symbol="SHFE.hc2610", trade_day="2026-09-28",
                                           kind="delivery_month", level="danger", text=text) is False
    reopened.close()
    await app.state.guests.close()
    app.state.store.close()


@pytest.mark.asyncio
async def test_positions_without_a_notice_stay_silent(tmp_path, monkeypatch):
    from trader_runtime.main import create_app

    # 2026-09-15：rb2610 离死线还有 12 个交易日、au2612 更远、ag2612 不在目录里 → 全都静默。
    monkeypatch.setattr("trader_runtime.shared_market.trading_today", lambda *_, **__: date(2026, 9, 15))
    monkeypatch.setattr("trader_runtime.delivery_reminders.trading_today", lambda *_, **__: date(2026, 9, 15))
    app = create_app(tmp_path / "state.db", agent_mode="fake", data_mode="fake",
                     local_token="test-token", cors_origins={"http://localhost"})
    headers = {"Origin": "http://localhost", "Authorization": "Bearer test-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            session = (await client.post("/api/sessions/restore",
                                         json={"mode": "local", "agentId": "native"})).json()["sessionId"]
            assert (await client.get("/api/positions")).status_code == 200
            await app.state.account_reminder_loop.run_once()
            events = [event for _, event in app.state.store.list_events(session)]
            assert not [event for event in events
                        if str(event["payload"].get("turnId", "")).startswith("delivery-")]
    await app.state.guests.close()
    app.state.store.close()


@pytest.mark.asyncio
async def test_delivery_switched_off_says_nothing(tmp_path, monkeypatch):
    """#932: notify.delivery off — the loop sends no message, line, push or 提醒中心 row."""
    import trader_runtime.delivery_reminders as reminders
    from trader_runtime.main import create_app

    monkeypatch.setattr(reminders, "trading_today", lambda *_, **__: date(2026, 9, 28))
    monkeypatch.setattr("trader_runtime.shared_market.trading_today", lambda *_, **__: date(2026, 9, 28))
    app = create_app(tmp_path / "state.db", agent_mode="fake", data_mode="fake",
                     local_token="test-token", cors_origins={"http://localhost"})
    headers = {"Origin": "http://localhost", "Authorization": "Bearer test-token"}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime", headers=headers) as client:
            session = (await client.post("/api/sessions/restore",
                                         json={"mode": "local", "agentId": "native"})).json()["sessionId"]
            assert (await client.put("/api/preferences", json={"notify": {"delivery": False}})).status_code == 200
            await app.state.account_reminder_loop.run_once()
            assert not [event for _, event in app.state.store.list_events(session)
                        if str(event["payload"].get("turnId", "")).startswith("delivery-")]
            assert (await client.get("/api/reminders", params={"scope": "earlier"})).json()["items"] == []
            assert (await client.get("/api/reminders")).json()["items"] == []
    await app.state.guests.close()
    app.state.store.close()


def test_record_delivery_notice_is_idempotent_per_day(tmp_path):
    store = RuntimeStore(tmp_path / "db.sqlite")
    kwargs = dict(user_id="one", symbol="SHFE.rb2610", trade_day="2026-09-28",
                  kind="delivery_month", level="danger", text="须平仓")
    assert store.record_delivery_notice(**kwargs) is True
    assert store.record_delivery_notice(**kwargs) is False
    assert store.record_delivery_notice(**{**kwargs, "user_id": "two"}) is True
    assert store.record_delivery_notice(**{**kwargs, "trade_day": "2026-09-29"}) is True
    store.close()


def test_three_digit_czce_code_without_catalog_dates_resolves_to_this_decade() -> None:
    """目录缺交割年月时，郑商所三位后缀 610 是 2026-10，不是 2006-10（否则当过期跳过、漏提醒）。"""
    cf610 = {"symbol": "CZCE.CF610", "exchangeId": "CZCE", "instrumentId": "CF610", "productId": "CF"}
    notice = delivery_notice(cf610, date(2026, 9, 28))
    assert notice is not None and notice["kind"] == "delivery_month" and notice["level"] == "danger"
    assert notice["lastHoldDay"] == "2026-09-30"
    # 跨十年：2029 年底看 001 是 2030-01。
    cf001 = {**cf610, "symbol": "CZCE.CF001", "instrumentId": "CF001"}
    assert delivery_notice(cf001, date(2029, 12, 24))["lastHoldDay"] == "2029-12-31"
