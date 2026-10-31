import sys
import time
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tqkq_readonly_sidecar.main import OfficialTqKqSession, Snapshot
from tqkq_readonly_sidecar.replay import HistoricalSession, ReplayActor, validate_request


def test_replay_http_consumes_state_and_close_bodies_before_responding():
    import json
    from http.client import HTTPConnection
    from threading import Thread

    from tqkq_readonly_sidecar.main import ReadOnlyServer

    calls = []
    snapshot = {"accountType": "TqSim", "replayId": "a" * 32, "bars": [1] * 150_000}
    actor = SimpleNamespace(state=lambda: (calls.append("state"), snapshot)[1],
                            close=lambda: calls.append("close"))
    service = SimpleNamespace(replays=SimpleNamespace(get=lambda _: actor))
    server = ReadOnlyServer(("127.0.0.1", 0), "test-token", service)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    def post(action, body):
        connection = HTTPConnection(*server.server_address, timeout=5)
        try:
            connection.request("POST", "/v1/replays/" + "a" * 32 + "/" + action, body=body,
                               headers={"Authorization": "Bearer test-token"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        # Invalid JSON must not be ignored or close an active historical actor.
        for action in ("state", "close"):
            assert post(action, "{")[0] == 422
        assert calls == []
        assert post("state", "{}") == (200, snapshot)
        assert post("state", None) == (200, snapshot)
        assert post("close", "{}")[0] == 200
        assert calls == ["state", "state", "close"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_replay_request_is_one_completed_day_and_one_actual_contract():
    request = {"symbol": "SHFE.au2612", "tradingDay": "2026-09-04", "initialBalance": 1_000_000}
    assert validate_request(request) == request
    for patch in ({"tradingDay": (date.today() + timedelta(days=1)).isoformat()},
                  {"symbol": "KQ.m@SHFE.au"}, {"initialBalance": float("nan")}, {"initialBalance": True},
                  {"accountType": "TqKq"}):
        with pytest.raises((ValueError, TypeError)):
            validate_request({**request, **patch})


def test_a_trading_day_can_be_replayed_once_its_day_session_closed():
    """10-13 夜盘验收：21:42 选 10-13（白盘 15:00 已收、夜盘已属 10-14 交易日）被拒，因为这里比的是自然日。
    交易日在它那天的白盘收盘后结束：当天 15:30 之后放行，之前（含白盘中）拒绝，原因码不变。"""
    from zoneinfo import ZoneInfo

    china = ZoneInfo("Asia/Shanghai")
    request = {"symbol": "SHFE.rb2701", "tradingDay": "2026-10-13", "initialBalance": 1_000_000}
    for now in (datetime(2026, 10, 13, 21, 42, tzinfo=china), datetime(2026, 10, 13, 15, 31, tzinfo=china),
                datetime(2026, 10, 14, 10, 0, tzinfo=china)):
        assert validate_request(request, now=now)["tradingDay"] == "2026-10-13"
    for now in (datetime(2026, 10, 13, 10, 0, tzinfo=china), datetime(2026, 10, 12, 22, 0, tzinfo=china),
                datetime(2026, 10, 13, 2, 0, tzinfo=UTC)):  # 10:00 Beijing, the day session is running
        with pytest.raises(ValueError, match="replay_requires_completed_day"):
            validate_request(request, now=now)


def test_the_unfinished_day_refusal_names_itself_over_http():
    import json
    from http.client import HTTPConnection
    from threading import Thread

    from tqkq_readonly_sidecar.main import ReadOnlyServer

    def create(body):
        validate_request(body)
        raise AssertionError("an unfinished day must not reach the replay")

    service = SimpleNamespace(replays=SimpleNamespace(create=create))
    server = ReadOnlyServer(("127.0.0.1", 0), "test-token", service)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        connection = HTTPConnection(*server.server_address, timeout=5)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        connection.request("POST", "/v1/replays", body=json.dumps(
            {"symbol": "SHFE.rb2701", "tradingDay": tomorrow, "initialBalance": 1_000_000}),
            headers={"Authorization": "Bearer test-token"})
        response = connection.getresponse()
        assert (response.status, json.loads(response.read())) == (
            422, {"error": "invalid_replay_request", "reason": "replay_requires_completed_day"})
        connection.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_replay_request_accepts_the_display_period_for_the_ready_gate():
    request = {"symbol": "SHFE.au2612", "tradingDay": "2026-09-04", "initialBalance": 1_000_000}
    assert validate_request({**request, "defaultDurationSeconds": 300}) == {**request, "defaultDurationSeconds": 300}
    # An unusable period (0, a foreign value, a wrong type) opens on the 60s
    # default instead of refusing the whole replay creation.
    for bad in (0, 45, "300", 300.0, True, None):
        assert validate_request({**request, "defaultDurationSeconds": bad})["defaultDurationSeconds"] == 60


def test_the_resolved_default_duration_is_logged_before_anything_can_stall(capsys):
    import time

    session = HistoricalSession.__new__(HistoricalSession)
    session._timing = {"start": time.monotonic(), "logged": set()}
    session._stage("default_duration", 86400)
    session._stage("default_duration", 60)  # one line per stage, the first answer stands
    session._stage("first_quote")
    err = capsys.readouterr().err
    assert "replay_timing stage=default_duration value=86400\n" in err
    assert "replay_timing stage=first_quote ms=" in err


def session_fixture():
    session = HistoricalSession.__new__(HistoricalSession)
    session.symbol, session.day = "SHFE.au2612", date(2026, 9, 4)
    session.quotes = {session.symbol: SimpleNamespace(price_tick=.02, datetime="2026-09-03 21:00:00", last_price=960)}
    session.orders, session.trades = {}, {}
    session.sim = SimpleNamespace(trade_log={})
    session._submitted, session._cancelled, session._observed_order_ids = {}, set(), set()
    session.calls = []

    def insert_order(**kwargs):
        session.calls.append(kwargs)
        exchange, instrument = kwargs["symbol"].split(".")
        session.orders[kwargs["order_id"]] = SimpleNamespace(order_id=kwargs["order_id"],
            exchange_id=exchange, instrument_id=instrument, direction=kwargs["direction"], offset=kwargs["offset"],
            volume_orign=kwargs["volume"], volume_left=kwargs["volume"], limit_price=kwargs["limit_price"],
            price_type="LIMIT", status="ALIVE")

    session.api = SimpleNamespace(insert_order=insert_order, cancel_order=lambda order: session.calls.append("cancel"))
    return session


def test_paused_order_queues_once_without_advancing_or_claiming_confirmation():
    session = session_fixture()
    order = {"clientOrderId": "AITreplay0001", "commandId": "submit:AITreplay0001", "symbol": session.symbol,
             "direction": "BUY", "offset": "OPEN", "volume": 1, "limitPrice": 960.02, "priceType": "LIMIT"}
    assert session.command("submit", order)["found"] is False
    assert session.command("submit", order)["found"] is False
    assert len(session.calls) == 1
    with pytest.raises(ValueError, match="identity_conflict"):
        session.command("submit", {**order, "volume": 2})
    with pytest.raises(ValueError, match="symbol_mismatch"):
        session.command("submit", {**order, "symbol": "SHFE.rb2610"})
    with pytest.raises(ValueError, match="invalid_replay_order"):
        session.command("submit", {**order, "limitPrice": 960.01})
    session._observed_order_ids.add(order["clientOrderId"])
    assert session.command("query", {"clientOrderId": order["clientOrderId"]})["found"] is True
    assert session.command("cancel", {"clientOrderId": order["clientOrderId"]})["commandStatus"] == "UNKNOWN"
    session.command("cancel", {"clientOrderId": order["clientOrderId"]})
    assert session.calls.count("cancel") == 1


def test_export_preserves_historical_time_and_hides_future_bars():
    session = session_fixture()
    now = session.snapshot_time()
    assert now == datetime(2026, 9, 3, 13, tzinfo=UTC)
    assert session._trading_day(now) == "2026-09-04"
    account = dict(balance=1_000_000, closeProfit=0, positionProfit=0, commission=0, floatProfit=0, margin=0,
                   available=1_000_000)
    session.snapshot = lambda: Snapshot(account, [], [], [], {session.symbol: {}}, "2026-09-04", now.isoformat())
    rows = [SimpleNamespace(datetime=int((now + timedelta(minutes=offset)).timestamp() * 1e9),
                            open=960, high=960, low=960, close=960, volume=1, close_oi=10)
            for offset in (-1, 0, 1)]
    session._kline_serials = {(session.symbol, 60): SimpleNamespace(itertuples=lambda **kwargs: iter(rows))}
    session.api.is_serial_ready = lambda serial: True
    session._annotate_trading_days = lambda bars: [{**bar, "tradingDay": "2026-09-04"} for bar in bars]
    result = session.export()
    assert len(result["workbenches"]["60"]["bars"]) == 2
    assert result["clock"] == now.isoformat()


def test_export_marks_loading_periods_without_blocking_or_leaking_future_bars():
    session = session_fixture()
    session.default_duration = 60
    now = session.snapshot_time()
    account = dict(balance=1_000_000, closeProfit=0, positionProfit=0, commission=0, floatProfit=0, margin=0,
                   available=1_000_000)
    session.snapshot = lambda: Snapshot(account, [], [], [], {session.symbol: {}}, "2026-09-04", now.isoformat())
    rows = [SimpleNamespace(datetime=int((now + timedelta(minutes=offset)).timestamp() * 1e9),
                            open=960, high=960, low=960, close=960, volume=1, close_oi=10)
            for offset in (-1, 0, 1)]
    session._kline_serials = {
        (session.symbol, 60): SimpleNamespace(itertuples=lambda **kwargs: iter(rows)),
        # A still-downloading serial carries warmup rows including a future bar;
        # the placeholder must expose none of them.
        (session.symbol, 300): SimpleNamespace(itertuples=lambda **kwargs: iter(rows)),
    }
    session.api.is_serial_ready = lambda serial: serial is session._kline_serials[(session.symbol, 60)]
    session._annotate_trading_days = lambda bars: [{**bar, "tradingDay": "2026-09-04"} for bar in bars]
    result = session.export()
    assert result is not None
    assert len(result["workbenches"]["60"]["bars"]) == 2
    assert result["workbenches"]["300"] == {
        "schemaVersion": 1, "durationSeconds": 300,
        "quote": result["workbenches"]["60"]["quote"], "bars": [], "loading": True,
    }
    assert session.default_duration_ready() is True
    assert session.pending_durations() == [300]


def test_replay_calendar_cache_keeps_night_session_days_but_not_stale_prices():
    session = session_fixture()
    session._bar_trading_days = {}
    session._calendar = {"2026-09-03", "2026-09-04", "2026-09-07"}
    timestamps = ["2026-09-03T07:00:00+00:00", "2026-09-03T13:00:00+00:00", "2026-09-04T13:00:00+00:00"]
    bars = [{"datetime": int(datetime.fromisoformat(t).timestamp() * 1e9), "close": 960} for t in timestamps]
    expected = OfficialTqKqSession._annotate_trading_days(session, bars)
    assert session._annotate_trading_days(bars) == expected
    assert [b["tradingDay"] for b in expected] == ["2026-09-03", "2026-09-04", "2026-09-07"]
    updated = [{**bar, "close": 961} for bar in bars]
    assert session._annotate_trading_days(updated) == [{**bar, "close": 961} for bar in expected]


@pytest.mark.parametrize("floating", [0, 100])
def test_settled_day_keeps_official_profit_and_fees_instead_of_next_day_zeros(floating):
    session = session_fixture()
    equity = 997640 + floating
    reset_account = dict(balance=equity, closeProfit=0, positionProfit=0, commission=0, floatProfit=floating, margin=0)
    session.snapshot = lambda: Snapshot(reset_account, [], [], [], {session.symbol: {}}, "2026-09-04", session.snapshot_time().isoformat())
    session._kline_serials = {}
    session.sim.trade_log["2026-09-04"] = {"account": dict(balance=equity, close_profit=-2240,
        position_profit=floating, float_profit=floating, commission=120, margin=0, available=equity)}
    result = session.export()
    assert result["settled"] is True
    assert result["account"]["commission"] == 120
    assert (result["account"]["margin"], result["account"]["available"]) == (0, equity)
    assert result["account"]["closeProfit"] == -2240
    assert result["account"]["todayNetPnl"] == equity - 1_000_000
    assert result["account"]["equity"] == equity


def test_export_carries_the_source_margin_and_available_amounts():
    # #478：回放账户带出来源账本的保证金和可用资金（含挂单冻结），不只给一个比例。
    session = session_fixture()
    account = dict(balance=1000, staticBalance=1000, available=300, closeProfit=0, positionProfit=0,
                   commission=0, floatProfit=0, margin=500)
    session.snapshot = lambda: Snapshot(account, [], [], [], {session.symbol: {}}, "2026-09-04",
                                        session.snapshot_time().isoformat())
    session._kline_serials = {}
    result = session.export()["account"]
    assert (result["equity"], result["margin"], result["available"]) == (1000, 500, 300)
    assert result["marginRatio"] == 50


def test_actor_reaches_ready_on_the_display_period_and_never_reads_while_paused(monkeypatch):
    # Every SDK read may consume the next market event in TqBacktest: once ready, a paused
    # replay's clock must not move (PR #972 review). A period still loading at ready fills in
    # with the next step instead of background reads.
    monkeypatch.setattr("tqkq_readonly_sidecar.replay.IDLE_POLL_SECONDS", 0.05)
    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(BacktestFinished=type("BacktestFinished", (Exception,), {})))

    class WarmingSession:
        historical = True

        def __init__(self):
            self.reads, self.closed = 0, False
            self.stages = []

        def _stage(self, stage, value=None):
            self.stages.append(stage)

        def read(self, timeout_seconds):
            self.reads += 1

        def step(self):
            self.reads += 1

        def export(self):
            if self.reads < 1:
                return None
            three_hundred = ({"schemaVersion": 1, "durationSeconds": 300, "bars": [1, 2]}
                             if self.reads >= 3 else {"schemaVersion": 1, "durationSeconds": 300,
                                                      "bars": [], "loading": True})
            return {"clock": f"2026-09-03T13:0{self.reads}:00+00:00",
                    "workbenches": {"60": {"schemaVersion": 1, "durationSeconds": 60, "bars": [1]},
                                    "300": three_hundred}}

        def default_duration_ready(self):
            return self.reads >= 2

        def at_first_bar(self):
            return True

        def pending_durations(self):
            return [] if self.reads >= 3 else [300]

        def close(self):
            self.closed = True

    session = WarmingSession()
    actor = ReplayActor(("user", "password"),
                        {"symbol": "SHFE.au2612", "tradingDay": "2026-09-04",
                         "initialBalance": 1_000_000, "defaultDurationSeconds": 60},
                        factory=lambda credentials, request: session)
    try:
        deadline = time.time() + 5
        while actor.state()["status"] != "ready" and time.time() < deadline:
            time.sleep(0.01)
        # Ready while the 300s period is still pending: the snapshot carries it as loading.
        assert actor.state()["status"] == "ready"
        assert session.stages == ["ready", "ready_pending"]
        assert session.reads == 2
        clock = actor.state()["clock"]
        assert actor.state()["workbenches"]["300"]["loading"] is True
        time.sleep(0.3)  # six idle wakes while paused
        assert session.reads == 2
        assert actor.state()["clock"] == clock
        # The next step carries the period that was still loading.
        state = actor.call("advance", {})
        assert state["clock"] != clock
        assert state["workbenches"]["300"]["bars"] == [1, 2]
    finally:
        actor.close()
        deadline = time.time() + 5
        while not session.closed and time.time() < deadline:
            time.sleep(0.01)
        assert session.closed is True


CST = timezone(timedelta(hours=8))


def stepping_session(symbol, day, events, previous_bar):
    """A HistoricalSession over a TqBacktest stand-in: each wait_update moves to the next event
    time (the quote takes it); a 1-minute OPEN appends that bar to the 60s serial; `settles`
    writes the day's settlement like TqSim at the day's end."""
    import pandas as pd

    session = session_fixture()
    session.symbol, session.day = symbol, day
    session.quotes = {symbol: SimpleNamespace(price_tick=.2, last_price=0,
                                              datetime=previous_bar.strftime("%Y-%m-%d %H:%M:%S.%f"))}
    opens = [int(previous_bar.timestamp()) * 1_000_000_000]
    session._kline_serials = {(symbol, 60): pd.DataFrame({"datetime": opens})}
    session.pending = list(events)

    def wait_update(deadline=None):
        when, kind = session.pending.pop(0)
        if kind == "fail":  # the SDK answers nothing before the deadline
            return False
        if kind == "raise":  # the SDK or its connection fails mid-step
            raise ConnectionError("synthetic transport failure")
        if kind == "end":  # TqBacktest ran out of data: the day is over
            raise sys.modules["tqsdk"].BacktestFinished()
        session.quotes[symbol].datetime = when.strftime("%Y-%m-%d %H:%M:%S.%f")
        if kind == "minute":
            opens.append(int(when.timestamp()) * 1_000_000_000)
            session._kline_serials[(symbol, 60)] = pd.DataFrame({"datetime": opens})
        if kind == "settles":
            session.sim.trade_log[day.isoformat()] = {"account": {}}
        return True

    session.api.wait_update = wait_update
    session.api.is_changing = lambda item: False
    session.api.close = lambda: None
    session.snapshot = lambda: None
    session.default_duration_ready = lambda: True
    session.pending_durations = lambda: []
    session._stage = lambda stage, value=None: None
    session.export = lambda: {"clock": session.snapshot_time().isoformat(), "workbenches": {},
                              "settled": day.isoformat() in session.sim.trade_log}
    return session


def at(day, clock):
    return datetime.combine(day, datetime.strptime(clock, "%H:%M:%S.%f").time(), tzinfo=CST)


def test_replay_opens_on_the_first_minute_bar_and_steps_one_whole_minute_bar(monkeypatch):
    # CFFEX.IC opens 09:30 without a night session. The 1-hour serial opens on the hour, so
    # TqBacktest first stops at 09:00 with nothing trading (10-15 user-sim: the first press of
    # play crossed 30 empty minutes). Ready must stand on 09:30; one step is one whole minute
    # bar (its close and the next open); the lunch gap, with the hour bar closing inside it,
    # passes in one step.
    # advance imports BacktestFinished; a cold tqsdk import outlasts the actor's 5 s answer window.
    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(BacktestFinished=type("BacktestFinished", (Exception,), {})))
    day, eve = date(2026, 10, 14), date(2026, 10, 13)
    events = [(at(eve, "18:00:00.0"), "daily"), (at(day, "09:00:00.0"), "hour"),
              (at(day, "09:30:00.0"), "minute"), (at(day, "09:30:59.999999"), "close"),
              (at(day, "09:31:00.0"), "minute"), (at(day, "09:31:59.999999"), "close"),
              (at(day, "11:29:00.0"), "minute"), (at(day, "11:29:59.999999"), "close"),
              (at(day, "11:59:59.999999"), "hour"), (at(day, "13:00:00.0"), "minute")]
    session = stepping_session("CFFEX.IC2612", day, events, at(eve, "14:59:00.0"))
    actor = ReplayActor(("user", "password"), {"symbol": "CFFEX.IC2612", "tradingDay": day.isoformat(),
                                               "initialBalance": 1_000_000}, factory=lambda *_: session)
    try:
        deadline = time.time() + 5
        while actor.state()["status"] != "ready" and time.time() < deadline:
            time.sleep(0.01)
        assert actor.state()["clock"] == at(day, "09:30:00.0").astimezone(UTC).isoformat()
        assert actor.call("advance", {})["clock"] == at(day, "09:31:00.0").astimezone(UTC).isoformat()
        assert actor.call("advance", {})["clock"] == at(day, "11:29:00.0").astimezone(UTC).isoformat()
        assert actor.call("advance", {})["clock"] == at(day, "13:00:00.0").astimezone(UTC).isoformat()
    finally:
        actor.close()


def test_night_session_replay_opens_on_the_night_open():
    # SHFE.au trades from 21:00 the evening before; that is the day's first minute bar.
    day, eve = date(2026, 9, 4), date(2026, 9, 3)
    session = stepping_session("SHFE.au2612", day, [
        (at(eve, "18:00:00.0"), "daily"), (at(eve, "21:00:00.0"), "minute"),
        (at(eve, "21:00:59.999999"), "close"), (at(eve, "21:01:00.0"), "minute")], at(eve, "14:59:00.0"))
    assert session.at_first_bar() is False
    session.api.wait_update()
    assert session.at_first_bar() is False
    session.api.wait_update()
    assert session.at_first_bar() is True
    assert session.snapshot_time() == at(eve, "21:00:00.0")
    session.step()
    assert session.snapshot_time() == at(eve, "21:01:00.0")


def test_the_last_step_stops_at_settlement_never_past_the_day():
    day = date(2026, 10, 14)
    session = stepping_session("CFFEX.IC2612", day, [
        (at(day, "14:59:59.999999"), "close"), (at(day, "17:59:59.999999"), "settles"),
        (at(day, "18:00:00.0"), "minute")], at(day, "14:59:00.0"))
    session.step()
    assert session.snapshot_time() == at(day, "17:59:59.999999")
    assert len(session.pending) == 1


@pytest.mark.parametrize(("progressed", "stop"), [(False, "fail"), (True, "fail"), (True, "raise")])
def test_a_step_that_stops_part_way_is_never_left_ready_issue_932(monkeypatch, progressed, stop):
    # #932 B3: a step may consume some SDK events and then run out of time before the next bar's
    # open. TqBacktest cannot undo them, so the old 09:30 snapshot no longer matches the SDK:
    # the replay turns incomplete (not ready — old Runtimes trade only on ready/finished), keeps
    # answering order queries, refuses new orders, and the next advance finishes the same minute.
    # A failure before any event leaves the replay ready where it was. One rule for every way a
    # step stops short: wait_update answering False or raising.
    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(BacktestFinished=type("BacktestFinished", (Exception,), {})))
    day, eve = date(2026, 10, 14), date(2026, 10, 13)
    events = [(at(eve, "18:00:00.0"), "daily"), (at(day, "09:30:00.0"), "minute")]
    if progressed:
        events.append((at(day, "09:30:20.0"), "tick"))
    events += [(at(day, "09:30:59.0"), stop), (at(day, "09:30:59.999999"), "close"),
               (at(day, "09:31:00.0"), "minute")]
    session = stepping_session("CFFEX.IC2612", day, events, at(eve, "14:59:00.0"))
    actor = ReplayActor(("user", "password"), {"symbol": "CFFEX.IC2612", "tradingDay": day.isoformat(),
                                               "initialBalance": 1_000_000}, factory=lambda *_: session)
    try:
        deadline = time.time() + 5
        while actor.state()["status"] != "ready" and time.time() < deadline:
            time.sleep(0.01)
        opened = at(day, "09:30:00.0").astimezone(UTC).isoformat()
        assert actor.state()["clock"] == opened and actor.state()["stepInFlight"] is None
        # While the advance is queued or stepping, a state read names it (stepInFlight).
        seen = []
        stepping = session.api.wait_update

        def observed(deadline=None):
            seen.append(actor.state()["stepInFlight"])
            return stepping(deadline=deadline)

        session.api.wait_update = observed
        with pytest.raises(RuntimeError, match="replay_command_failed") as failure:
            actor.call("advance", {"stepId": "step-1"})
        assert seen and all(step == "step-1" for step in seen)
        assert actor.state()["stepInFlight"] is None
        # The same step id again answers the same outcome and never steps again.
        remaining = len(session.pending)
        with pytest.raises(RuntimeError, match="replay_command_failed"):
            actor.call("advance", {"stepId": "step-1"})
        assert len(session.pending) == remaining
        expected = "replay_advance_incomplete" if progressed else "replay_advance_timeout"
        assert str(failure.value.__cause__) == expected
        for _ in range(3):  # repeated state reads never turn it back into ready
            state = actor.state()
            assert state["status"] == ("incomplete" if progressed else "ready")
            assert state["clock"] == opened
        if progressed:
            assert state["error"] == "replay_advance_incomplete"
            order = {"clientOrderId": "AITreplay0002", "commandId": "submit:AITreplay0002",
                     "symbol": "CFFEX.IC2612", "direction": "BUY", "offset": "OPEN", "volume": 1,
                     "limitPrice": 6000.2, "priceType": "LIMIT"}
            with pytest.raises(RuntimeError, match="replay_not_ready"):
                actor.call("submit", order)
            assert actor.call("query", {"clientOrderId": "AITreplay0001"})["found"] is False
        finished = actor.call("advance", {"stepId": "step-2"})
        assert finished["status"] == "ready" and "error" not in finished
        assert finished["stepInFlight"] is None
        # A lost answer resent: the recorded success, the clock does not move again.
        assert actor.call("advance", {"stepId": "step-2"})["clock"] == finished["clock"]
        assert session.pending == []
        assert finished["clock"] == at(day, "09:31:00.0").astimezone(UTC).isoformat()
        assert session.pending == []
    finally:
        actor.close()


def test_the_backtest_ending_mid_step_finishes_the_replay_instead_of_leaving_it_incomplete(monkeypatch):
    # #932 review: BacktestFinished after an event in the last step is the day's end, not a
    # part-way failure — the replay is finished at once, no extra play/step to discover it.
    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(BacktestFinished=type("BacktestFinished", (Exception,), {})))
    day, eve = date(2026, 10, 14), date(2026, 10, 13)
    session = stepping_session("CFFEX.IC2612", day, [
        (at(eve, "18:00:00.0"), "daily"), (at(day, "09:30:00.0"), "minute"),
        (at(day, "14:59:30.0"), "tick"), (at(day, "14:59:59.0"), "end")], at(eve, "14:59:00.0"))
    actor = ReplayActor(("user", "password"), {"symbol": "CFFEX.IC2612", "tradingDay": day.isoformat(),
                                               "initialBalance": 1_000_000}, factory=lambda *_: session)
    try:
        deadline = time.time() + 5
        while actor.state()["status"] != "ready" and time.time() < deadline:
            time.sleep(0.01)
        assert actor.call("advance", {})["status"] == "finished"
        assert "error" not in actor.state()
    finally:
        actor.close()


def test_a_step_whose_state_cannot_be_exported_is_not_left_ready_and_exports_before_stepping_again(monkeypatch):
    # #932 review: step() reached the next bar open but export failed — the SDK moved past the
    # published snapshot, so the replay is incomplete (not ready); the next advance publishes that
    # same minute without stepping again, so no minute is skipped.
    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(BacktestFinished=type("BacktestFinished", (Exception,), {})))
    day, eve = date(2026, 10, 14), date(2026, 10, 13)
    session = stepping_session("CFFEX.IC2612", day, [
        (at(eve, "18:00:00.0"), "daily"), (at(day, "09:30:00.0"), "minute"),
        (at(day, "09:30:59.999999"), "close"), (at(day, "09:31:00.0"), "minute"),
        (at(day, "09:31:59.999999"), "close"), (at(day, "09:32:00.0"), "minute")], at(eve, "14:59:00.0"))
    exported = session.export
    failures = []

    def export():
        if failures:
            failures.pop()
            raise RuntimeError("replay_settlement_account_unavailable")
        return exported()

    session.export = export
    actor = ReplayActor(("user", "password"), {"symbol": "CFFEX.IC2612", "tradingDay": day.isoformat(),
                                               "initialBalance": 1_000_000}, factory=lambda *_: session)
    try:
        deadline = time.time() + 5
        while actor.state()["status"] != "ready" and time.time() < deadline:
            time.sleep(0.01)
        opened = at(day, "09:30:00.0").astimezone(UTC).isoformat()
        assert actor.state()["clock"] == opened
        failures.append(True)
        with pytest.raises(RuntimeError, match="replay_command_failed"):
            actor.call("advance", {"stepId": "step-1"})
        state = actor.state()
        assert state["status"] == "incomplete" and state["clock"] == opened
        assert state["stepInFlight"] is None
        remaining = len(session.pending)
        published = actor.call("advance", {"stepId": "step-2"})
        assert published["status"] == "ready" and len(session.pending) == remaining  # exported, not stepped
        assert published["clock"] == at(day, "09:31:00.0").astimezone(UTC).isoformat()
        assert actor.call("advance", {"stepId": "step-3"})["clock"] == at(day, "09:32:00.0").astimezone(UTC).isoformat()
    finally:
        actor.close()


def test_the_days_end_whose_final_export_fails_is_incomplete_then_finishes_on_the_same_retry(monkeypatch):
    # #932 review: BacktestFinished, then the final export raises — the replay is not left ready
    # and the step is not recorded as a success; the same step id answers that failure, and the
    # next advance exports the day's end (no further step) and finishes.
    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(BacktestFinished=type("BacktestFinished", (Exception,), {})))
    day, eve = date(2026, 10, 14), date(2026, 10, 13)
    session = stepping_session("CFFEX.IC2612", day, [
        (at(eve, "18:00:00.0"), "daily"), (at(day, "09:30:00.0"), "minute"),
        (at(day, "14:59:30.0"), "tick"), (at(day, "14:59:59.0"), "end")], at(eve, "14:59:00.0"))
    exported = session.export
    failures = []

    def export():
        if failures:
            failures.pop()
            raise RuntimeError("replay_settlement_account_unavailable")
        return exported()

    session.export = export
    actor = ReplayActor(("user", "password"), {"symbol": "CFFEX.IC2612", "tradingDay": day.isoformat(),
                                               "initialBalance": 1_000_000}, factory=lambda *_: session)
    try:
        deadline = time.time() + 5
        while actor.state()["status"] != "ready" and time.time() < deadline:
            time.sleep(0.01)
        failures.append(True)
        with pytest.raises(RuntimeError, match="replay_command_failed"):
            actor.call("advance", {"stepId": "last"})
        assert actor.state()["status"] == "incomplete"
        with pytest.raises(RuntimeError, match="replay_command_failed"):
            actor.call("advance", {"stepId": "last"})  # the recorded failure, not a success
        assert actor.call("advance", {"stepId": "export-again"})["status"] == "finished"
    finally:
        actor.close()
