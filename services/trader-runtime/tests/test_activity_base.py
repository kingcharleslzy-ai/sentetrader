"""活动帧共用底稿（2026-10-13 第二轮省 CPU）：同一份数据库与行情帧，改后的活动帧与改前逐字节相同；
推送通路每轮每账户只读一次库，换轮、新快照、委托变化后重读；底稿不跨用户、不跨账户（#518）。

`_reference_snapshot` 是改前（main 4a16fff8）`activity_snapshot` 的原样副本，只作比对。"""
from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path

import pytest

from test_order_protection import armed_card, fill_entry
from test_simulation_orders import USER, confirm
from trader_runtime import agent_activity
from trader_runtime.agent_activity import activity_base, activity_snapshot
from trader_runtime.simulation_orders import order_display_status


def _reference_snapshot(app, user, symbol, activity, simulation_orders=None, strategy_monitor=None,
                        trading_account_id=None):
    state = app.state
    store = state.store
    orders_service = simulation_orders or state.simulation_orders
    monitor_service = strategy_monitor or state.strategy_monitor
    account_filter = trading_account_id or store.default_trading_account_id
    rows = store.connection.execute("""SELECT l.client_order_id, l.created_at, p.payload_json
        FROM tqkq_sim_order_links l JOIN proposals p ON p.id = l.proposal_id
        WHERE l.user_id = ? AND l.trading_account_id = ?
        ORDER BY l.created_at DESC""", (user, account_filter)).fetchall()
    ai_orders = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        if payload.get("automationSource") not in {"ai_autonomous", "deterministic_stop"}:
            continue
        order = orders_service.get(row["client_order_id"], user_id=user)
        if symbol and order["request"]["symbol"] != symbol:
            continue
        ai_orders[row["client_order_id"]] = {**order, "reason": payload.get("reason", ""),
            "source": "stop" if payload.get("automationSource") == "deterministic_stop" else "ai"}
    fills = [{**trade, "source": ai_orders[trade["clientOrderId"]]["source"]}
             for trade in activity.get("trades", []) if trade.get("clientOrderId") in ai_orders]
    reports = {report.get("clientOrderId"): report for report in activity.get("orders", [])}
    for identifier, order in ai_orders.items():
        report = reports.get(identifier)
        request = order["request"]
        if not report or any(report.get(key) != request.get(key) for key in ("symbol", "direction", "offset", "volume")):
            continue
        volume = int(request["volume"])
        filled = max(int(order["filledVolume"]),
                     sum(fill["volume"] for fill in fills if fill["clientOrderId"] == identifier))
        order["filledVolume"] = filled
        if order["displayStatus"] in {"已受理", "部分成交"} and report.get("status") in {"ALIVE", "FINISHED"}:
            order["displayStatus"] = order_display_status(
                remote_status=order["remoteStatus"], submit_phase=order["submitPhase"],
                cancel_phase=order["cancelPhase"], volume=volume,
                order={**report, "volumeLeft": volume - filled})
    strategies = []
    for strategy in store.list_strategy_drafts(user_id=user, limit=50):
        if strategy["symbol"] == "*" or symbol and strategy["symbol"] != symbol:
            continue
        monitor = store.strategy_monitor_state(strategy_id=strategy["strategyId"], user_id=user)
        if strategy["status"] != "monitoring" and monitor is None:
            continue
        strategies.append({"strategyId": strategy["strategyId"], "name": strategy["name"],
            "status": strategy["status"], "decision": monitor_service.latest_results.get(strategy["strategyId"]),
            "monitorState": monitor,
            "recentAttempts": store.list_strategy_execution_attempts(
                strategy_id=strategy["strategyId"], user_id=user, limit=10)})
    for strategy in strategies:
        for attempt in strategy["recentAttempts"]:
            for identifier in [attempt.get("clientOrderId"), *(leg.get("clientOrderId") for leg in attempt.get("legs", []))]:
                if identifier in ai_orders:
                    ai_orders[identifier]["strategyId"] = strategy["strategyId"]
    stops = store.connection.execute(
        "SELECT * FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?",
        (user, account_filter)).fetchall()
    protections = []
    for row in stops:
        if symbol and row["symbol"] != symbol:
            continue
        from trader_runtime.autonomy import AutonomousSimulationService
        protection = AutonomousSimulationService._stop_public(row)
        protections.append(protection)
        if protection["closeClientOrderId"] in ai_orders:
            ai_orders[protection["closeClientOrderId"]]["strategyId"] = protection["strategyId"]
    for fill in fills:
        fill["strategyId"] = ai_orders[fill["clientOrderId"]].get("strategyId")
    return {"environment": "replay" if getattr(state.data_provider, "identifier", None) else "simulation",
        "replayId": getattr(state.data_provider, "identifier", None), "symbol": symbol,
        "capturedAt": activity.get("capturedAt"), "dataStatus": activity.get("dataStatus"),
        "ageSeconds": activity.get("ageSeconds"), "orders": list(ai_orders.values())[:100],
        "fills": sorted(fills, key=lambda item: item["tradeDateTime"]), "strategies": strategies, "protections": protections}


def _mark_automated(app, proposal_id: str, source: str, reason: str) -> None:
    with app.state.store.connection:
        app.state.store.connection.execute(
            "UPDATE proposals SET payload_json = json_set(payload_json, '$.automationSource', ?, '$.reason', ?)"
            " WHERE id = ?", (source, reason, proposal_id))


async def _book(tmp_path: Path):
    """A manual card with its protection armed and filled, an AI order (filled, linked to a monitored
    strategy's attempt), a deterministic-stop order still live, and strategies of every shape."""
    app, provider, gateway, session, manual = await armed_card(tmp_path, name="activity-base.sqlite3")
    store = app.state.store
    ids = []
    for index, (source, reason) in enumerate([("ai_autonomous", "AI 开仓"), ("deterministic_stop", "止损")]):
        card = app.state.proposals.create_trade(
            session.session_id, f"turn-ai-{index}", symbol="SHFE.au2612", side="buy_open", volume=2,
            price_intent="passive", reason="待改写")
        _mark_automated(app, card["proposalId"], source, reason)
        await confirm(app, card, f"confirm-ai-{index}")
        ids.append(dict(gateway.request))
    filled, live = ids
    provider.order_reports = [{**manual, "status": "FINISHED", "volumeLeft": 0},
                              {**filled, "status": "ALIVE", "volumeLeft": 1},
                              {**live, "status": "ALIVE", "volumeLeft": 2}]
    fill_entry(provider, filled, trade_id="fill-ai", volume=1, time=2)
    for strategy_id, symbol, status in [("st-mon", "SHFE.au2612", "monitoring"), ("st-all", "*", "monitoring"),
                                        ("st-paused", "SHFE.au2612", "paused"), ("st-draft", "DCE.m2701", "draft")]:
        store.save_strategy_draft(strategy_id=strategy_id, user_id=USER, source_session_id=session.session_id,
                                  name=f"策略 {strategy_id}", symbol=symbol, version=1, status=status,
                                  definition={"schemaVersion": 2, "rules": [{"x": strategy_id}]},
                                  definition_digest=f"digest-{strategy_id}")
    store.save_strategy_monitor_state(strategy_id="st-paused", user_id=USER, trading_date="2026-08-25",
                                      high_watermark_equity=None, last_today_pnl=None, consecutive_losses=1,
                                      last_trade_id=None, last_executed_bar=None, pause_reason="连亏")
    store.save_strategy_intent_snapshot(intent_id="intent-1", strategy_id="st-mon", user_id=USER,
                                        strategy_digest="digest-st-mon", signal_bar_time="2026-08-25T09:01:00+08:00",
                                        context_digest="ctx", payload={"action": "open_long"})
    assert store.reserve_strategy_execution_attempt(
        attempt_id="att-1", strategy_id="st-mon", user_id=USER, intent_id="intent-1",
        signal_bar_time="2026-08-25T09:01:00+08:00", trading_day="2026-08-25", action="open_long", volume=2)
    store.finish_strategy_execution_attempt(attempt_id="att-1", status="SUBMITTED",
                                            client_order_id=filled["clientOrderId"])
    app.state.strategy_monitor.latest_results["st-mon"] = {"signal": "hold"}
    return app, provider


SYMBOLS = ("", "SHFE.au2612", "DCE.m2701")


def _dump(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def test_frames_match_the_previous_code_byte_for_byte(tmp_path: Path) -> None:
    app, provider = await _book(tmp_path)
    try:
        activity = provider.account_activity()
        base = activity_base(app, USER)
        expected = [_dump(_reference_snapshot(app, USER, symbol, activity)) for symbol in SYMBOLS]
        assert any(json.loads(text)["orders"] and json.loads(text)["strategies"]
                   and json.loads(text)["protections"] and json.loads(text)["fills"] for text in expected)
        # Each frame built fresh, and every symbol from one shared base (twice: a frame must not
        # leave anything behind in the base for the next one).
        assert [_dump(activity_snapshot(app, USER, symbol, activity)) for symbol in SYMBOLS] == expected
        for _ in range(2):
            assert [_dump(activity_snapshot(app, USER, symbol, activity, base=base)) for symbol in SYMBOLS] == expected
    finally:
        app.state.store.close()


async def test_a_base_is_never_used_for_another_user_or_account(tmp_path: Path) -> None:
    app, provider = await _book(tmp_path)
    try:
        base = activity_base(app, USER)
        with pytest.raises(ValueError, match="activity_base_mismatch"):
            activity_snapshot(app, "someone-else", "", provider.account_activity(), base=base)
        with pytest.raises(ValueError, match="activity_base_mismatch"):
            activity_snapshot(app, USER, "", provider.account_activity(), trading_account_id="tqkq-pool:other",
                              base=base)
    finally:
        app.state.store.close()


async def test_the_feed_reads_the_database_once_per_round_and_again_after_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, provider = await _book(tmp_path)
    builds: list[str] = []
    real = agent_activity.activity_base

    def counted(*args, **kwargs):
        builds.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_activity, "activity_base", counted)
    feed = app.state.runtime_feed
    account = app.state.store.default_trading_account_id
    try:
        round_one = provider.account_activity()
        with feed._round():
            frames = [await feed._activity(account, USER, symbol, round_one) for symbol in SYMBOLS]
        assert len(builds) == 1  # one read for every focused symbol of the round
        assert [_dump(frame) for frame in frames] == [
            _dump(_reference_snapshot(app, USER, symbol, round_one)) for symbol in SYMBOLS]
        # A new round (a new provider frame) reads again — and sees a fill that arrived meanwhile.
        fill_entry(provider, provider.order_reports[2],
                   trade_id="fill-stop", volume=2, time=3)
        round_two = provider.account_activity()
        with feed._round():
            frame = await feed._activity(account, USER, "SHFE.au2612", round_two)
            assert len(builds) == 2
            assert _dump(frame) == _dump(_reference_snapshot(app, USER, "SHFE.au2612", round_two))
            # An order-link change inside the round bumps the snapshot version: the next frame rereads.
            app.state.store.save_strategy_draft(
                strategy_id="st-new", user_id=USER, source_session_id="s", name="新策略", symbol="SHFE.au2612",
                version=1, status="monitoring", definition={"schemaVersion": 2}, definition_digest="d-new")
            feed.orders_changed()
            frame = await feed._activity(account, USER, "SHFE.au2612", round_two)
            assert len(builds) == 3
            assert "st-new" in [item["strategyId"] for item in frame["strategies"]]
            assert _dump(frame) == _dump(_reference_snapshot(app, USER, "SHFE.au2612", round_two))
        # The same provider frame and version in the next round still reads again: nothing a round
        # built is kept for the next one.
        with feed._round():
            await feed._activity(account, USER, "SHFE.au2612", round_two)
        assert len(builds) == 4
    finally:
        app.state.store.close()


async def test_a_round_releases_the_account_book_when_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#862: the base was kept in the feed's closure until 256 other keys pushed it out, so a reader who
    left kept its account's whole automatic-order book in memory. It now goes with the round."""
    app, provider = await _book(tmp_path)
    built: list[weakref.ref] = []
    real = agent_activity.activity_base

    def tracked(*args, **kwargs):
        base = real(*args, **kwargs)
        built.append(weakref.ref(base))
        return base

    monkeypatch.setattr(agent_activity, "activity_base", tracked)
    feed = app.state.runtime_feed
    account = app.state.store.default_trading_account_id
    try:
        activity = provider.account_activity()
        with feed._round():
            frame = await feed._activity(account, USER, "SHFE.au2612", activity)
            assert frame["orders"] and built[0]() is not None
        assert feed.round_shared is None
        gc.collect()
        assert built[0]() is None
        # Outside a round each call builds its own and keeps nothing.
        await feed._activity(account, USER, "SHFE.au2612", activity)
        gc.collect()
        assert len(built) == 2 and built[1]() is None
    finally:
        app.state.store.close()
