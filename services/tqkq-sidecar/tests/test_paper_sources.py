"""本站模拟撮合引擎的行情账号接力（2026-10-12）：合成 TqApi 与凭据，不连真实账号。

认证失败立即换下一个并继续撮合；运行中连续断线超过阈值才换；全部失败行情不可用；
换账号后的首包旧行情被防倒退水位拒绝；清单变更后跟随（移除即换、调序不抢回）；
口令不进日志、健康输出与 HTTP 答复。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pytest

from test_paper_book import ACCOUNT, DAY, NIGHT, TOKEN, TRADING_DATES, au_quote, submit_request
from tqkq_readonly_sidecar.paper import (
    PaperEngine,
    PaperServer,
    TqQuoteSource,
    UnknownSymbolError,
    VirtualClock,
    nonexistent_instruments,
)
from tqkq_readonly_sidecar.paper_sources import MAX_FAILURES, MarketSourceRelay, SourceAccount, parse_sources

SYMBOL = "SHFE.au2612"
SECRET_A, SECRET_B = "pw-alpha-7f3e9c", "pw-bravo-2d81aa"
# The production message (10-13) for a contract name the contract service does not know.
NONEXISTENT = ("查询合约服务报错 failed to execute graphql operation, errors: "
               "[variable instrument_id: [{0}] contains non-existent instrument: {0}]")


def tq_quote(moment: str, price: float) -> SimpleNamespace:
    """A TqSdk-shaped quote object (expiry in seconds, trading_time with attributes)."""
    fields = {key: value for key, value in au_quote(moment, price).items() if key != "trading_time"}
    fields["expire_datetime"] = fields["expire_datetime"] / 1e9
    return SimpleNamespace(**fields, trading_time=SimpleNamespace(day=DAY, night=NIGHT))


class FakeApi:
    """One synthetic TqApi session of one account."""

    def __init__(self, username: str, snapshot: SimpleNamespace, *, broken: bool = False) -> None:
        self.username = username
        self.snapshot = snapshot
        self.broken = broken
        self.closed = False
        self.calendar_calls = 0
        self.changed: list[SimpleNamespace] = []
        self.nonexistent: set[str] = set()
        self.misreport: str | None = None

    def get_quote(self, symbol):
        if self.misreport is not None:
            raise Exception(NONEXISTENT.format(self.misreport))
        if symbol in self.nonexistent:
            # TqSdk 3.10 symbols.py: the contract service's answer kills the query task (the session
            # is unusable afterwards) and get_quote's wait raises it.
            raise Exception(NONEXISTENT.format(symbol))
        return self.snapshot

    def get_trading_calendar(self, start_dt, end_dt):
        self.calendar_calls += 1
        return {"date": [], "trading": []}

    def wait_update(self, deadline=None):
        if self.broken:
            raise ConnectionError(f"connection lost for {self.username}")
        time.sleep(0.01)
        return False

    def is_changing(self, obj):
        if obj in self.changed:
            self.changed.remove(obj)
            return True
        return False

    def push(self, moment: str, price: float) -> None:
        for name, value in vars(tq_quote(moment, price)).items():
            setattr(self.snapshot, name, value)
        self.changed.append(self.snapshot)

    def close(self):
        self.closed = True


class Accounts:
    """The api factory: per username, an auth failure, a broken session or a working one."""

    def __init__(self) -> None:
        self.auth_fail: set[str] = set()
        self.broken: set[str] = set()
        self.snapshots: dict[str, SimpleNamespace] = {}
        self.nonexistent: set[str] = set()  # contract names the contract service does not know
        self.misreport: dict[str, str] = {}  # username → a name every subscription reports as non-existent
        self.sessions: list[FakeApi] = []
        self.lock = threading.Lock()

    def __call__(self, username: str, password: str) -> FakeApi:
        with self.lock:
            if username in self.auth_fail:
                raise Exception(f"用户权限认证失败 {username}/{password}")
            api = FakeApi(username, self.snapshots.get(username) or tq_quote("09:31:00.000000", 820.0),
                          broken=username in self.broken)
            api.nonexistent = self.nonexistent
            api.misreport = self.misreport.get(username)
            self.sessions.append(api)
            return api

    def logins(self, username: str) -> int:
        return sum(1 for api in self.sessions if api.username == username)


@pytest.fixture()
def engine(tmp_path):
    clock = VirtualClock(datetime.fromisoformat("2026-11-02T09:00:00+08:00"))
    paper = PaperEngine(db_path=str(tmp_path / "paper.sqlite3"), clock=clock, trading_dates=TRADING_DATES)
    paper.open()
    paper.start()
    paper.create_account(ACCOUNT)
    with paper._state_lock:
        paper._leases[SYMBOL] = paper._clock()  # a reader asked for it: the source subscribes
    yield paper
    paper.close()


def make_source(accounts: Accounts, *pool: tuple[str, str, str]) -> TqQuoteSource:
    source = TqQuoteSource(lambda: ("local-user", "local-pass"), api_factory=accounts)
    source.RETRY_SECONDS = 0.01
    source.SWITCH_SECONDS = 0.01
    source._calendar = {"2026-12-31"}  # calendar coverage is fine: no renewal branch
    source._contract_attempt_at = time.monotonic()  # contract refresh not under test here
    if pool:
        source.relay.configure(parse_sources({"sources": [
            {"accountId": account_id, "username": username, "password": password}
            for account_id, username, password in pool]}))
    return source


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_auth_failure_switches_to_the_next_account_and_matching_continues(engine, capsys):
    accounts = Accounts()
    accounts.auth_fail.add("user-a")
    source = make_source(accounts, ("tqkq-sim:a", "user-a", SECRET_A), ("tqkq-sim:b", "user-b", SECRET_B))
    source.start(engine)
    try:
        wait_for(lambda: SYMBOL in engine._quotes)
        order = engine.submit(ACCOUNT, submit_request("order-after-switch"))["order"]
        assert order["status"] == "FINISHED"  # the second account's quote keeps matching
        status = source.relay.status()
        assert status["current"] == "tqkq-sim:b" and status["position"] == 2 and status["connected"]
        assert status["switchReason"] == "auth_failed" and status["switchedAt"]
        first = status["accounts"][0]
        assert first["state"] == "cooling" and first["lastError"] == "tqkq_authentication_failed"
        assert accounts.logins("user-b") == 1  # one account at a time, no retry storm
        health = engine.health()
        assert health["dataStatus"] == "normal"
    finally:
        source.stop()
    logged = capsys.readouterr().err
    assert "tqkq-sim:a" in logged and "switching account" in logged
    for secret in (SECRET_A, SECRET_B, "user-a"):
        assert secret not in logged
        assert secret not in json.dumps(source.relay.status())


def test_a_session_that_keeps_dropping_switches_after_the_threshold(engine):
    accounts = Accounts()
    accounts.broken.add("user-a")
    source = make_source(accounts, ("tqkq-sim:a", "user-a", SECRET_A), ("tqkq-sim:b", "user-b", SECRET_B))
    source.start(engine)
    try:
        wait_for(lambda: source.relay.status()["current"] == "tqkq-sim:b" and source.relay.status()["connected"])
        # The same account reconnects (network jitter) until the threshold, then the next one takes over.
        assert accounts.logins("user-a") == MAX_FAILURES
        assert source.relay.status()["switchReason"] == "disconnected"
        assert all(api.closed for api in accounts.sessions if api.username == "user-a")
    finally:
        source.stop()


def test_every_account_failing_leaves_the_market_unavailable(engine):
    accounts = Accounts()
    accounts.auth_fail |= {"user-a", "user-b"}
    source = make_source(accounts, ("tqkq-sim:a", "user-a", SECRET_A), ("tqkq-sim:b", "user-b", SECRET_B))
    source.start(engine)
    try:
        wait_for(lambda: all(item["state"] == "cooling" for item in source.relay.status()["accounts"]))
        time.sleep(0.05)
        assert source.relay.pick() is None
        status = source.relay.status()
        assert status["connected"] is False
        # The engine's existing rule: past 60 s of reconnecting, account data is unavailable.
        engine._clock.advance(datetime.fromisoformat("2026-11-02T09:01:01+08:00"))
        assert engine.status(ACCOUNT)["dataStatus"] == "unavailable"
        gate = engine.submit(ACCOUNT, submit_request("order-no-market"))
        assert gate["commandStatus"] == "NOT_DISPATCHED"
        assert accounts.logins("user-a") == 0 and accounts.logins("user-b") == 0
    finally:
        source.stop()


def test_the_next_accounts_older_first_packet_is_refused_by_the_watermark(engine):
    """The next account's session may lag: its first snapshot (09:30, 800) is older than
    what the engine already matched on (09:31, 820) and must not roll the price back."""
    accounts = Accounts()
    accounts.snapshots["user-b"] = tq_quote("09:30:00.000000", 800.0)
    source = make_source(accounts, ("tqkq-sim:a", "user-a", SECRET_A), ("tqkq-sim:b", "user-b", SECRET_B))
    source.start(engine)
    try:
        wait_for(lambda: SYMBOL in engine._quotes)
        # A held position keeps the contract subscribed on whichever account serves next.
        assert engine.submit(ACCOUNT, submit_request("order-hold"))["order"]["status"] == "FINISHED"
        accounts.broken.add("user-a")
        for api in accounts.sessions:
            api.broken = True  # the first account's live session drops, and keeps dropping
        wait_for(lambda: source.relay.status()["current"] == "tqkq-sim:b" and source.relay.status()["connected"])
        assert engine.drain(timeout=5.0)
        assert engine._quotes[SYMBOL]["last_price"] == 820.0  # the older packet was dropped
        second = next(api for api in accounts.sessions if api.username == "user-b")
        second.push("09:32:00.000000", 821.0)
        wait_for(lambda: engine._quotes[SYMBOL]["last_price"] == 821.0)
        assert second.calendar_calls == 1  # the calendar follows the live connection
    finally:
        source.stop()


def test_the_engine_follows_the_list_order(engine):
    accounts = Accounts()
    source = make_source(accounts, ("tqkq-sim:a", "user-a", SECRET_A), ("tqkq-sim:b", "user-b", SECRET_B),
                         ("tqkq-sim:c", "user-c", "pw-charlie-11"))
    source.start(engine)
    relay = source.relay
    try:
        wait_for(lambda: relay.status()["current"] == "tqkq-sim:a" and relay.status()["connected"])
        # Reordered with the live account still listed: no switch (a recovered or
        # promoted account never takes the connection back).
        relay.configure(parse_sources({"sources": [
            {"accountId": "tqkq-sim:c", "username": "user-c", "password": "pw-charlie-11"},
            {"accountId": "tqkq-sim:a", "username": "user-a", "password": SECRET_A},
            {"accountId": "tqkq-sim:b", "username": "user-b", "password": SECRET_B}]}))
        time.sleep(0.1)
        assert relay.status()["current"] == "tqkq-sim:a" and accounts.logins("user-a") == 1
        # The live account removed: the engine reconnects to the first of the new order.
        relay.configure(parse_sources({"sources": [
            {"accountId": "tqkq-sim:c", "username": "user-c", "password": "pw-charlie-11"},
            {"accountId": "tqkq-sim:b", "username": "user-b", "password": SECRET_B}]}))
        wait_for(lambda: relay.status()["current"] == "tqkq-sim:c" and relay.status()["connected"])
        assert relay.status()["switchReason"] == "removed"
        assert accounts.sessions[0].closed and accounts.logins("user-b") == 0
    finally:
        source.stop()


def test_the_mounted_account_takes_the_runtime_name_without_reconnecting(engine):
    accounts = Accounts()
    source = make_source(accounts)  # nothing pushed yet: the engine's own mounted account
    source.start(engine)
    relay = source.relay
    try:
        wait_for(lambda: relay.status()["current"] == "local" and relay.status()["connected"])
        relay.configure(parse_sources({"sources": [
            {"accountId": "tqkq-sim:a", "username": "user-a", "password": SECRET_A},
            {"accountId": "tqkq-sim:default", "local": True}]}))
        time.sleep(0.1)
        status = relay.status()
        assert status["current"] == "tqkq-sim:default" and status["position"] == 2 and status["connected"]
        assert status["configured"] is True
        assert accounts.logins("local-user") == 1 and accounts.logins("user-a") == 0
    finally:
        source.stop()


def test_the_market_sources_route_needs_the_token_and_never_echoes_a_password(engine):
    relay = MarketSourceRelay(lambda: ("local-user", "local-pass"))
    server = PaperServer(("127.0.0.1", 0), TOKEN, engine, relay)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = server.server_address[1]

    def call(path, *, token=TOKEN, payload=None):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        data = json.dumps(payload).encode() if payload is not None else None
        return urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", headers=headers, data=data,
            method="POST" if data is not None else "GET"), timeout=10)

    sources = {"sources": [{"accountId": "tqkq-sim:a", "username": "user-a", "password": SECRET_A},
                           {"accountId": "tqkq-sim:default", "local": True}]}
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            call("/v1/market-sources", token="wrong-token-x", payload=sources)
        assert refused.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as invalid:
            call("/v1/market-sources", payload={"sources": [{"accountId": "tqkq-sim:a", "username": "user-a"}]})
        assert invalid.value.code == 422
        with call("/v1/market-sources", payload=sources) as response:
            answer = response.read().decode()
        revision = json.loads(answer)["revision"]
        with call("/v1/market-sources", payload=sources) as response:
            assert json.load(response)["revision"] == revision  # same list, same revision
        with call("/healthz") as response:
            health = response.read().decode()
        market = json.loads(health)["marketSource"]
        assert [item["accountId"] for item in market["accounts"]] == ["tqkq-sim:a", "tqkq-sim:default"]
        assert market["revision"] == revision and market["configured"] is True
        for text in (answer, health):
            assert SECRET_A not in text and "user-a" not in text and "local-pass" not in text
    finally:
        server.shutdown()
        server.server_close()


def test_relay_unit_rules():
    clock = [1000.0]
    relay = MarketSourceRelay(lambda: ("", ""), monotonic=lambda: clock[0])
    relay.configure([SourceAccount("a", False, "user-a", "pa"), SourceAccount("b", False, "user-b", "pb")])
    first = relay.pick()
    assert first.account_id == "a"
    # A long-stable session that drops starts its count afresh.
    assert relay.failed(first, "tqkq_sidecar_connectionerror", connecting=False, session_seconds=120) is False
    assert relay.failed(first, "tqkq_sidecar_connectionerror", connecting=False, session_seconds=1) is False
    assert relay.failed(first, "tqkq_sidecar_connectionerror", connecting=False, session_seconds=1) is True
    assert relay.pick().account_id == "b"
    # The cooled account comes back after its cooldown, but does not take over a healthy one.
    clock[0] += 61
    assert relay.pick().account_id == "b"
    # A changed login for the live account keeps the account, reconnects with the new login.
    live = relay.pick()
    relay.configure([SourceAccount("a", False, "user-a", "pa"), SourceAccount("b", False, "user-b", "pb-new")])
    assert relay.take_change() is True and relay.still_current(live) is False
    assert relay.credentials(relay.pick()) == ("user-b", "pb-new")
    assert relay.redact("login user-b/pb-new failed") == "login ***/*** failed"


def test_a_pushed_empty_list_leaves_no_source():
    relay = MarketSourceRelay(lambda: ("local-user", "local-pass"))
    assert relay.pick().account_id == "local"  # never configured: the mounted account
    relay.configure([])
    assert relay.pick() is None and relay.status()["current"] is None


def test_an_error_from_a_retired_account_is_still_redacted():
    relay = MarketSourceRelay(lambda: ("", ""))
    old = SourceAccount("a", False, "user-old", "pw-old-secret")
    relay.configure([old])
    relay.configure([SourceAccount("a", False, "user-new", "pw-new-secret")])
    text = relay.redact("用户权限认证失败 user-old/pw-old-secret", old)
    assert "user-old" not in text and "pw-old-secret" not in text


def test_a_change_between_pick_and_connect_is_still_seen(engine):
    """Codex #801: the list changes after pick() but before the session is up — the
    removed account must not stay the matching source."""
    accounts = Accounts()
    source = make_source(accounts, ("tqkq-sim:a", "user-a", SECRET_A), ("tqkq-sim:b", "user-b", SECRET_B))
    relay = source.relay
    real_connect = source._connect

    def connect_while_removed(symbols):
        if source._account is not None and source._account.account_id == "tqkq-sim:a":
            relay.configure(parse_sources({"sources": [
                {"accountId": "tqkq-sim:b", "username": "user-b", "password": SECRET_B}]}))
        return real_connect(symbols)

    source._connect = connect_while_removed
    source.start(engine)
    try:
        wait_for(lambda: relay.status()["current"] == "tqkq-sim:b" and relay.status()["connected"])
        assert accounts.sessions[0].username == "user-a" and accounts.sessions[0].closed
    finally:
        source.stop()


def test_a_nonexistent_contract_is_dropped_without_failing_or_switching_the_source(engine, capsys):
    """10-13 production: an upper-cased SHFE.RB2701 reached the engine's lease, the contract service
    answered "non-existent instrument", and the source counted it as a connection failure — four
    market accounts switched round and round and no contract had a current quote. Now the bad
    name alone leaves the subscription set: same account, no failure counted, the other contracts
    keep matching, and a read of it answers symbol_not_found at once."""
    accounts = Accounts()
    accounts.nonexistent.add("SHFE.RB2701")
    source = make_source(accounts, ("tqkq-sim:a", "user-a", SECRET_A), ("tqkq-sim:b", "user-b", SECRET_B))
    relay = source.relay
    source.start(engine)
    try:
        wait_for(lambda: SYMBOL in engine._quotes and relay.status()["connected"])
        with engine._state_lock:  # the way 10-13's name got there (the read entry now refuses it)
            engine._leases["SHFE.RB2701"] = engine._clock()
        wait_for(lambda: "SHFE.RB2701" in engine.rejected_symbols())
        wait_for(lambda: relay.status()["connected"] and len(accounts.sessions) >= 2
                 and not accounts.sessions[-1].closed)
        status = relay.status()
        assert status["current"] == "tqkq-sim:a" and status["switchReason"] is None
        assert all(item["failures"] == 0 and item["lastError"] is None for item in status["accounts"])
        assert accounts.logins("user-b") == 0
        assert "SHFE.RB2701" not in engine.wanted_symbols()
        time.sleep(0.1)
        assert accounts.logins("user-a") == 2  # one reconnect, not a loop
        order = engine.submit(ACCOUNT, submit_request("order-beside-a-bad-name"))["order"]
        assert order["status"] == "FINISHED"  # the other contract still matches
        # Through the read entry: the case is fixed before the lease, and a name the contract
        # service does not know answers symbol_not_found as soon as the source drops it.
        assert engine.market_quote(ACCOUNT, "SHFE.RB2701")["symbol"] == "SHFE.rb2701"
        accounts.nonexistent.add("SHFE.rb9999")
        with pytest.raises(UnknownSymbolError):
            engine.market_quote(ACCOUNT, "SHFE.rb9999")
        wait_for(lambda: relay.status()["connected"])
        assert relay.status()["current"] == "tqkq-sim:a" and accounts.logins("user-b") == 0
    finally:
        source.stop()
    logged = capsys.readouterr().err
    assert "dropped non-existent contract(s) SHFE.RB2701" in logged
    assert "switching account" not in logged and "paper quote source retry" not in logged


def test_a_non_existent_answer_naming_no_subscribed_contract_is_still_a_source_failure(engine):
    """The isolation only trusts a name this connection was subscribing: an answer naming anything
    else (the message format changed, another cause) keeps the old failure path — counted, and
    switched after the threshold — instead of reconnecting the same account in a tight loop."""
    accounts = Accounts()
    accounts.misreport["user-a"] = "SHFE.zz0000"
    source = make_source(accounts, ("tqkq-sim:a", "user-a", SECRET_A), ("tqkq-sim:b", "user-b", SECRET_B))
    source.start(engine)
    try:
        wait_for(lambda: source.relay.status()["current"] == "tqkq-sim:b" and source.relay.status()["connected"])
        assert accounts.logins("user-a") == MAX_FAILURES and engine.rejected_symbols() == []
    finally:
        source.stop()


def test_nonexistent_instruments_reads_only_the_contract_service_answer():
    assert nonexistent_instruments(Exception(NONEXISTENT.format("SHFE.RB2701"))) == ["SHFE.RB2701"]
    assert nonexistent_instruments(Exception("代码 ['DCE.X9999'] 不存在, 请检查合约代码是否填写正确")) == ["DCE.X9999"]
    for error in (ConnectionError("connection lost"), Exception("用户权限认证失败"), TimeoutError("timed out")):
        assert nonexistent_instruments(error) == []
