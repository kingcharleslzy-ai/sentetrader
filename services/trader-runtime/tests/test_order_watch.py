"""The unfilled-order watchdog (09-21): a confirmed resting limit order is reminded at 15 s and
again at 60 s — in the conversation and through the voice — and the watch ends the moment the
order fills, is cancelled, refuses or reads a quarantined report; 结果未知 is queried on (#932 F3) up to the 30-minute cap. 追单/撤单 are the voice's two commands on the
watched order; both only ever query and cancel through the normal routes (TQKQ_SIM_THIN_V1)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

from test_simulation_orders import USER, Gateway, NormalProvider, application, quote_now
from trader_runtime import reminders


async def _confirmed_resting_order(app: Path, gateway: Gateway, tmp_path: Path, name: str,
                                   *, volume: int = 1) -> dict[str, Any]:
    """One order confirmed through the real HTTP route (that is where the watch registers), resting
    on the book (已受理)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session = (await client.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()
        candidate = (await client.post("/api/simulation/proposals", json={
            "sessionId": session["sessionId"], "symbol": "SHFE.au2612", "side": "buy_open",
            "volume": volume, "priceIntent": "passive", "reason": "挂单看护测试"})).json()
        app.state.data_provider.quote_datetime = quote_now()
        confirmed = await client.post(f"/api/proposals/{candidate['proposalId']}/confirm", json={
            "approvalId": candidate["approvalId"], "proposalVersion": candidate["version"],
            "idempotencyKey": f"watch-{name}"})
        assert confirmed.status_code == 200
        order = confirmed.json()["simulationOrder"]
        assert order["displayStatus"] == "已受理"
        return {"order": order, "sessionId": session["sessionId"]}


def _notices(app, session_id: str) -> list[str]:
    return [str(event.get("payload", {}).get("text"))
            for _, event in app.state.store.list_events(session_id)
            if event.get("name") == "message.completed" and str(event.get("payload", {}).get("turnId", "")).startswith("orderwatch-")]


async def test_resting_order_is_reminded_at_15s_and_again_at_60s_then_ends_on_fill(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "watch-remind.sqlite3", gateway, NormalProvider())
    made = await _confirmed_resting_order(app, gateway, tmp_path, "remind")
    watch, order = app.state.order_watch, made["order"]
    target = watch.target_for(USER)
    assert target and target["label"].startswith("SHFE.AU2612 买开 1 手")

    async def age_and_tick(seconds: float) -> None:
        watch._orders[order["clientOrderId"]].confirmed_at -= seconds
        await watch._tick(order["clientOrderId"])

    # 15 s unfilled: the notice lands in the conversation (with the live book) and with the voice —
    # the voice only while a call is online (a queued announcement would otherwise be read out,
    # stale, at the start of the next call).
    from types import SimpleNamespace
    app.state.voice_realtime.calls["test-call"] = SimpleNamespace(user_id=USER)
    await age_and_tick(16)
    assert target in [watch.target_for(USER)]
    notices = _notices(app, made["sessionId"])
    assert len(notices) == 1 and "挂了 16 秒没成交" in notices[0] and "买一 823 / 卖一 824" in notices[0]
    assert "追单" in notices[0] and "撤单" in notices[0]
    # 提醒卡 (2026-10-04): on the notice's own turn — the order price, how long it rested, the book;
    # actions ask the AI or open the orders tab, never an order button.
    events = [event for _, event in app.state.store.list_events(made["sessionId"])]
    notice_turn = [event["payload"]["turnId"] for event in events if event.get("name") == "message.completed"
                   and str(event["payload"].get("turnId", "")).startswith("orderwatch-")][0]
    cards = [event["payload"] for event in events if event.get("name") == "card.reminder"]
    assert len(cards) == 1 and cards[0]["turnId"] == notice_turn and cards[0]["kind"] == "order_unfilled"
    figures = {figure["label"]: figure["value"] for figure in cards[0]["figures"]}
    assert figures["已挂"] == "16 秒" and figures["买一"] == "823" and figures["卖一"] == "824"
    assert {action["kind"] for action in cards[0]["actions"]} == {"ask", "ui"}
    assert app.state.voice_realtime._pending_announcements[USER][-1]["kind"] == "order_unfilled"
    # 60 s: the second reminder is a distinct kind (the 60 s per-kind throttle would swallow it).
    await age_and_tick(45)
    assert len(_notices(app, made["sessionId"])) == 2
    assert app.state.voice_realtime._pending_announcements[USER][-1]["kind"] == "order_unfilled_late"
    # The same step again (a restart, the other slot, a lost in-memory flag) finds its claim (#932):
    # no second notice, nothing queued for the call.
    app.state.voice_realtime.calls.clear()
    watch._orders[order["clientOrderId"]].reminded_second = False
    queued = len(app.state.voice_realtime._pending_announcements[USER])
    await watch._tick(order["clientOrderId"])
    assert len(app.state.voice_realtime._pending_announcements[USER]) == queued
    assert len(_notices(app, made["sessionId"])) == 2
    kinds = [item["kind"] for item in app.state.store.reminders_page(
        USER, trading_day=reminders.current_trading_day(), scope="today")[0]]
    assert kinds == ["order_unfilled", "order_unfilled"]
    # A fill ends the watch; no third reminder.
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await watch._tick(order["clientOrderId"])
    assert watch.target_for(USER) is None
    assert len(_notices(app, made["sessionId"])) == 2  # the two reminders, nothing more
    app.state.store.close()


async def test_a_restarted_watch_picks_the_order_up_reminds_nothing_twice_and_follows_the_fill(
        tmp_path: Path) -> None:
    """#932: the watched order is a row — a restart (or the other blue-green slot) restores it with its own
    clock, the 15 s reminder it already had stays claimed, and the 提醒中心 row follows the order's fill."""
    from trader_runtime.order_watch import OrderWatch
    gateway = Gateway()
    app = application(tmp_path / "watch-restart.sqlite3", gateway, NormalProvider())
    made = await _confirmed_resting_order(app, gateway, tmp_path, "restart")
    watch, order = app.state.order_watch, made["order"]
    watch._orders[order["clientOrderId"]].confirmed_at -= 16
    await watch._tick(order["clientOrderId"])
    assert len(_notices(app, made["sessionId"])) == 1
    # A fresh watch on the same database (a new process): the order is back, aged like the original.
    restarted = OrderWatch(app)
    assert restarted.restore() == 1
    await restarted._tick(order["clientOrderId"])
    assert len(_notices(app, made["sessionId"])) == 1
    today = reminders.current_trading_day()
    [item] = app.state.store.reminders_page(USER, trading_day=today, scope="today")[0]
    assert item["kind"] == "order_unfilled" and item["target"]["type"] == "orders"
    assert item["state"]["tone"] == "wait"
    # The fill ends the watch, removes its row, and the reminder's state says how it ended.
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await restarted._tick(order["clientOrderId"])
    [item] = app.state.store.reminders_page(USER, trading_day=today, scope="today")[0]
    assert item["state"]["tone"] == "ok" and "已成交" in item["state"]["text"]
    assert OrderWatch(app).restore() == 0
    app.state.store.close()


async def test_a_watch_that_reads_unknown_keeps_querying_until_it_settles_without_any_resend(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "watch-unknown.sqlite3", gateway, NormalProvider())
    made = await _confirmed_resting_order(app, gateway, tmp_path, "unknown")
    watch, order = app.state.order_watch, made["order"]
    client_order_id = order["clientOrderId"]
    # A cancel whose answer is lost reads 结果未知 — not known, so not an end (#932 F3: the watch used to stop
    # there and the order stayed 结果未知 for good). The watch keeps it, reminds nothing (it is not known to be
    # resting) and only ever queries it (TQKQ_SIM_THIN_V1).
    gateway.cancel_error = True
    assert (await app.state.simulation_orders.cancel(client_order_id, user_id=USER))["displayStatus"] == "结果未知"
    gateway.query_error = True
    watch._orders[client_order_id].confirmed_at -= 16
    await watch._tick(client_order_id)
    assert watch._orders[client_order_id].display_status == "结果未知"
    assert watch.target_for(USER) is None  # still read, but not a 撤单 / 追单 target meanwhile
    assert _notices(app, made["sessionId"]) == []
    # The next read settles it (the cancel did go through): the watch follows it to 已撤销 and ends.
    gateway.query_error = False
    gateway.query_report = gateway.report(gateway.request, status="FINISHED")
    await watch._tick(client_order_id)
    assert client_order_id not in watch._orders
    assert app.state.simulation_orders.get(client_order_id, user_id=USER)["displayStatus"] == "已撤销"
    assert gateway.cancel_count == 1 and gateway.submit_count == 1  # the one cancel asked for, nothing re-sent
    app.state.store.close()


async def test_an_order_that_stays_unknown_is_dropped_at_the_watch_cap(tmp_path: Path) -> None:
    from trader_runtime.order_watch import WATCH_HARD_CAP_SECONDS
    gateway = Gateway()
    app = application(tmp_path / "watch-unknown-cap.sqlite3", gateway, NormalProvider())
    made = await _confirmed_resting_order(app, gateway, tmp_path, "unknown-cap")
    watch, client_order_id = app.state.order_watch, made["order"]["clientOrderId"]
    gateway.cancel_error = True
    await app.state.simulation_orders.cancel(client_order_id, user_id=USER)
    gateway.query_error = True
    await watch._tick(client_order_id)
    assert client_order_id in watch._orders
    queries = gateway.query_count
    watch._orders[client_order_id].confirmed_at -= WATCH_HARD_CAP_SECONDS + 1
    await watch._tick(client_order_id)
    assert client_order_id not in watch._orders and gateway.query_count == queries  # not queried past the cap
    app.state.store.close()


async def test_a_quarantined_report_ends_the_watch_without_any_resend(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "watch-mismatch.sqlite3", gateway, NormalProvider())
    made = await _confirmed_resting_order(app, gateway, tmp_path, "mismatch")
    # The read comes back with another order's identity: 身份异常, which no later normal report dismisses —
    # the watch ends rather than read it for 30 minutes; nothing is cancelled or re-sent.
    gateway.query_report = gateway.report(gateway.request, symbol="SHFE.wr2612")
    await app.state.order_watch._tick(made["order"]["clientOrderId"])
    assert app.state.order_watch.target_for(USER) is None
    assert gateway.cancel_count == 0 and gateway.submit_count == 1
    app.state.store.close()


def test_close_today_and_yesterday_orders_are_named_in_chinese() -> None:
    """#932 F3: 上期所 / 能源中心 平今 / 平昨 read like the client's orderLegLabel, never "SELL"."""
    from trader_runtime.order_watch import WatchedOrder

    def watched(symbol: str, direction: str, offset: str) -> WatchedOrder:
        return WatchedOrder(client_order_id="x", account_id="a", user_id=USER, session_id="", symbol=symbol,
                            name="", direction=direction, offset=offset, volume=2, limit_price=800,
                            confirmed_at=0.0)

    assert (watched("SHFE.au2612", "SELL", "CLOSETODAY").side_word, watched("SHFE.au2612", "SELL", "CLOSETODAY").side_phrase) \
        == ("卖平今", "卖出平今仓")
    assert (watched("INE.sc2612", "BUY", "CLOSE").side_word, watched("INE.sc2612", "BUY", "CLOSE").side_phrase) \
        == ("买平昨", "买入平昨仓")
    assert watched("DCE.m2701", "sell", "CLOSE").side_word == "卖平"
    assert watched("DCE.m2701", "buy", "OPEN").label == "DCE.m2701 买开 2 手 800"


async def test_spoken_chase_cancels_then_delegates_the_opponent_price_order(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "watch-chase.sqlite3", gateway, NormalProvider())
    await _confirmed_resting_order(app, gateway, tmp_path, "chase")
    service = app.state.voice_realtime
    from trader_runtime.voice_realtime import CallState
    state = CallState(user_id=USER)
    # The cancel lands (FINISHED → 已撤销) and the read-back confirms it before anything is re-sent.
    gateway.cancel_report = gateway.report(gateway.request, status="FINISHED")
    gateway.query_reports = [gateway.report(gateway.request, status="FINISHED")]
    delegated: list[str] = []

    async def fake_delegate(utterance, state, **_):
        delegated.append(utterance)
        return {"ok": True, "spoken": "已挂单，确认吗？", "say": "已挂单，确认吗？"}
    service._delegate = fake_delegate  # type: ignore[method-assign]
    outcome = await service.run_tool("chase", {}, state)
    assert outcome["ok"] is True and outcome["spoken"].startswith("旧单已撤，")
    assert delegated == ["以对手价下单：SHFE.AU2612 买入开仓 1 手"]
    assert gateway.cancel_count == 1
    assert service.order_watch_target(USER) is None  # the old order is no longer watched
    app.state.store.close()


async def test_spoken_chase_stops_when_the_cancel_reads_unknown(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "watch-chase-unknown.sqlite3", gateway, NormalProvider())
    made = await _confirmed_resting_order(app, gateway, tmp_path, "chase-unknown")
    service = app.state.voice_realtime
    from trader_runtime.voice_realtime import CallState
    # The cancel response is lost; the read-back still shows the order resting → nothing re-sent,
    # the chase stops, and the order is no longer a 撤单 / 追单 target (the next cancel would be a second
    # attempt on an unconfirmed outcome, which TQKQ_SIM_THIN_V1 forbids). The watch reads it on (#932 F3).
    gateway.cancel_error = True
    gateway.query_reports = [gateway.report(gateway.request)]  # still ALIVE
    outcome = await service.run_tool("chase", {}, CallState(user_id=USER))
    assert outcome["ok"] is False and outcome["error"] == "cancel_unconfirmed"
    assert "先不追" in outcome["say"]
    assert gateway.cancel_count == 1
    assert service.order_watch_target(USER) is None
    client_order_id = made["order"]["clientOrderId"]
    watch = app.state.order_watch
    # A restart (or the other slot) restores it as unknown too: still not a 撤单 / 追单 target.
    from trader_runtime.order_watch import OrderWatch
    restored = OrderWatch(app)
    assert restored.restore() == 1 and restored.target_for(USER) is None
    await watch._tick(client_order_id)  # reads it still resting: still unknown, still read, nothing reminded
    assert client_order_id in watch._orders and service.order_watch_target(USER) is None
    gateway.query_report = gateway.report(gateway.request, status="FINISHED")  # the cancel did go through
    await watch._tick(client_order_id)
    assert client_order_id not in watch._orders
    assert app.state.simulation_orders.get(client_order_id, user_id=USER)["displayStatus"] == "已撤销"
    assert gateway.cancel_count == 1
    app.state.store.close()


async def test_spoken_cancel_withdraws_the_watched_order(tmp_path: Path) -> None:
    gateway = Gateway()
    app = application(tmp_path / "watch-cancel.sqlite3", gateway, NormalProvider())
    await _confirmed_resting_order(app, gateway, tmp_path, "cancel")
    service = app.state.voice_realtime
    from trader_runtime.voice_realtime import CallState
    gateway.cancel_report = gateway.report(gateway.request, status="FINISHED")
    outcome = await service.run_tool("watch_cancel", {}, CallState(user_id=USER))
    assert outcome["ok"] is True and "已撤销" in outcome["say"]
    assert gateway.cancel_count == 1
    assert service.order_watch_target(USER) is None
    # With nothing watched, the word is not a command.
    nothing = await service.run_tool("watch_cancel", {}, CallState(user_id=USER))
    assert nothing["ok"] is False and "没有看着的挂单" in nothing["say"]
    app.state.store.close()


# -- 执行链共用判断 (2026-10-04 #518): 部分成交后已结束（FINISHED）不再是“在挂” ---------------------


async def test_a_finished_partial_order_ends_the_watch_without_a_reminder(tmp_path: Path) -> None:
    """2 手挂单成交 1 手后结束（FINISHED，投影“部分成交”）：看护立刻结束，不再提醒
    “挂了 N 秒没成交”——那句话只属于还挂在场上的单。"""
    gateway = Gateway()
    app = application(tmp_path / "watch-ended-partial.sqlite3", gateway, NormalProvider())
    made = await _confirmed_resting_order(app, gateway, tmp_path, "ended-partial", volume=2)
    watch, order = app.state.order_watch, made["order"]
    watch._orders[order["clientOrderId"]].confirmed_at -= 16  # 已过提醒时刻
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=1)
    await watch._tick(order["clientOrderId"])
    assert watch.target_for(USER) is None  # 结束的单不在看护里
    assert _notices(app, made["sessionId"]) == []  # 也不会有“挂了 16 秒没成交”
    app.state.store.close()


# -- 订单实况推送 (feed.order, 2026-10-08): the watch's read-back change is pushed to the
# confirming session, so a confirmed card's rows follow the order live without a refresh.


def _order_facts(app, session_id: str) -> list[dict[str, Any]]:
    return [event["payload"] for _, event in app.state.broker.transient_events.get(session_id, ())
            if event.get("name") == "feed.order"]


async def test_a_resting_orders_fill_is_pushed_as_one_feed_order(tmp_path: Path) -> None:
    """挂单后成交：读回从 已受理 变 已成交 的那一刻，会话的流上多一条 feed.order（同一订单
    视图加 accountId），确认卡的行和委托栏由此实时变 已成交——不是等刷新。"""
    gateway = Gateway()
    app = application(tmp_path / "watch-order-fact.sqlite3", gateway, NormalProvider())
    made = await _confirmed_resting_order(app, gateway, tmp_path, "order-fact")
    watch, order = app.state.order_watch, made["order"]
    app.state.broker.subscribers[made["sessionId"]] = 1  # 一个挂着流的窗口
    # 还挂在场上：读回没变，不推。
    await watch._tick(order["clientOrderId"])
    assert _order_facts(app, made["sessionId"]) == []
    gateway.query_report = gateway.report(gateway.request, status="FINISHED", volume_left=0)
    await watch._tick(order["clientOrderId"])
    facts = _order_facts(app, made["sessionId"])
    assert len(facts) == 1 and facts[0]["clientOrderId"] == order["clientOrderId"]
    assert facts[0]["displayStatus"] == "已成交" and facts[0]["accountId"]
    # 成交后看护结束：不会有第二条。
    await watch._tick(order["clientOrderId"])
    assert len(_order_facts(app, made["sessionId"])) == 1
    app.state.store.close()


async def test_a_close_batchs_children_push_facts_but_never_remind(tmp_path: Path) -> None:
    """批量平仓的子单跟着看护只为推送实况（remind=False）：每笔成交都推一条 feed.order，
    但“挂了 N 秒没成交”的提醒一张卡不会说 N 遍（09-21 的决定原样保留）。"""
    gateway = Gateway()
    app = application(tmp_path / "watch-batch-facts.sqlite3", gateway, NormalProvider())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:5173"}) as client:
        assert (await client.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
        session_id = (await client.post("/api/sessions?chat=true", json={"mode": "local"})).json()["sessionId"]
        created = (await client.post("/api/simulation/close-batch-proposals",
                                     json={"sessionId": session_id, "direction": "short", "reason": "平掉所有空单"})).json()
        app.state.data_provider.quote_datetime = quote_now()
        confirmed = await client.post(f"/api/proposals/{created['proposalId']}/confirm", json={
            "approvalId": created["approvalId"], "proposalVersion": created["version"],
            "idempotencyKey": "watch-batch-facts"})
        assert confirmed.status_code == 200
        children = confirmed.json()["simulationOrders"]
    watch = app.state.order_watch
    assert all(child["clientOrderId"] in watch._orders for child in children)
    assert all(not watch._orders[child["clientOrderId"]].remind for child in children)
    app.state.broker.subscribers[session_id] = 1
    for child in children:
        watch._orders[child["clientOrderId"]].confirmed_at -= 61  # 早过了两次提醒的时刻
    gateway.query_reports = [gateway.report(child["request"], status="FINISHED", volume_left=0)
                             for child in children]
    for child in children:
        await watch._tick(child["clientOrderId"])
    facts = _order_facts(app, session_id)
    assert sorted(fact["clientOrderId"] for fact in facts) == sorted(child["clientOrderId"] for child in children)
    assert all(fact["displayStatus"] == "已成交" for fact in facts)
    assert _notices(app, session_id) == []  # 子单只推实况，不提醒
    app.state.store.close()
