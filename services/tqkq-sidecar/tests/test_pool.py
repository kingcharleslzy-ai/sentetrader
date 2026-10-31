"""接入池 M1a：start/stop/幂等/上限/凭据拒绝缓存/verify 与控制面 HTTP。

`_spawn`/`_wait_ready`/`_probe` 是池的进程边界，测试用假对象替身驱动状态机；
一条真实子进程用例（fixture 模式）走完整 stdin 配置 → readiness → /healthz 路径。
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import tqkq_readonly_sidecar.pool as pool_module
from tqkq_readonly_sidecar.pool import (
    PoolManager,
    PoolServer,
    PoolStartError,
)

TOKEN = "pool-test-token-with-at-least-32-characters"
ACCOUNT = "tqkq-sim:pooltest01"


class FakeProcess:
    def __init__(self) -> None:
        self.pid = -1
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def fake_manager(max_accounts: int = 8, statuses: list | None = None) -> tuple[PoolManager, list, list]:
    """A pool whose child boundary is a list of canned /healthz documents."""
    manager = PoolManager(
        token=TOKEN, port=0, max_accounts=max_accounts,
        base_port=19100, symbols=["SHFE.au2612"], fixture=True,
    )
    processes: list[FakeProcess] = []
    spawns: list[dict] = []
    probes: list = list(statuses or [])

    def spawn(account_id, port, username, password, symbols, child_token, market_only=False):
        spawns.append({"account_id": account_id, "port": port, "token": child_token,
                       "username": username, "password": password, "symbols": symbols,
                       "market_only": market_only})
        process = FakeProcess()
        process.pid = 20000 + len(processes)
        processes.append(process)
        return process

    manager._spawn = spawn  # type: ignore[method-assign]
    manager._wait_ready = lambda process: None  # type: ignore[method-assign]
    manager._probe = lambda port, token: probes.pop(0) if probes else {"dataStatus": "simulated"}  # type: ignore[method-assign]
    return manager, spawns, processes


def test_start_waits_for_health_then_is_idempotent():
    manager, spawns, processes = fake_manager(statuses=[
        None, {"dataStatus": "connecting"}, {"dataStatus": "simulated"},
    ])
    result = manager.start(ACCOUNT, "pool-user", "pool-pass")
    assert result["port"] == 19100 and result["status"] == "running"
    assert spawns[0]["password"] == "pool-pass"
    # 凭据只经 stdin 进程边界：spawn 收到原值（真实路径写 stdin，不进命令行/环境）。
    again = manager.start(ACCOUNT, "pool-user", "pool-pass")
    assert again["port"] == result["port"] and len(spawns) == 1
    entries = manager.entries()
    assert [item["status"] for item in entries] == ["running"]
    assert set(entries[0]) >= {"accountId", "port", "pid", "startedAt", "rssKb"}


def test_start_of_running_account_is_a_takeover_not_a_restart():
    # 蓝绿换槽：新槽对已在运行的账户 start，拿到的是现有子连接（同端口同
    # pid），不重启、不另起——哪怕它带来的是自己的凭据参数。
    manager, spawns, _ = fake_manager()
    first = manager.start(ACCOUNT, "slot-a-user", "slot-a-pass")
    takeover = manager.start(ACCOUNT, "slot-b-user", "slot-b-pass")
    assert takeover["port"] == first["port"] and takeover["pid"] == first["pid"]
    assert takeover["status"] == "running" and len(spawns) == 1
    assert [item["accountId"] for item in manager.entries()] == [ACCOUNT]


def test_market_only_start_marks_the_child_and_a_trading_start_replaces_it():
    """行情专用（2026-10-13）：行情专用账号的子进程按 marketOnly 起（数据会话、不开交易），
    列表里看得见；对它的交易 start 换成交易子进程；交易子进程在跑时，行情专用 start 直接接管
    （交易子进程本来就供公共行情），不重启。"""
    manager, spawns, processes = fake_manager()
    market = manager.start(ACCOUNT, "u", "p", market_only=True)
    assert spawns[-1]["market_only"] is True and market["marketOnly"] is True
    assert manager.entries()[0]["marketOnly"] is True
    again = manager.start(ACCOUNT, "u", "p", market_only=True)
    assert again["childId"] == market["childId"] and len(spawns) == 1
    trading = manager.start(ACCOUNT, "u", "p")
    assert len(spawns) == 2 and spawns[-1]["market_only"] is False
    assert processes[0].terminated and trading["childId"] != market["childId"]
    assert trading["marketOnly"] is False and [e["marketOnly"] for e in manager.entries()] == [False]
    takeover = manager.start(ACCOUNT, "u", "p", market_only=True)
    assert takeover["childId"] == trading["childId"] and len(spawns) == 2


def test_stop_terminates_and_frees_capacity():
    manager, _, processes = fake_manager(max_accounts=1)
    first = manager.start(ACCOUNT, "u", "p")
    with pytest.raises(PoolStartError) as full:
        manager.start("tqkq-sim:second0001", "u2", "p2")
    assert full.value.reason == "capacity_full" and len(processes) == 1
    assert manager.stop(ACCOUNT)["stopped"] is True
    assert processes[0].terminated
    assert manager.stop(ACCOUNT)["stopped"] is False  # 已不在
    second = manager.start("tqkq-sim:second0001", "u2", "p2")
    # 刚空出的端口先隔离，不立即交给下一个账户的子进程。
    assert second["port"] != first["port"]


def test_each_spawn_has_its_own_token_and_child_id():
    """每次起子进程各发一枚随机 token（只在 start 答复里）与 childId（列表可见）；
    幂等接管拿到同一枚；崩溃重启换新的一枚。池的控制 token 不交给子进程当账户口令。"""
    manager, spawns, processes = fake_manager()
    first = manager.start(ACCOUNT, "u", "p")
    assert len(first["token"]) >= 32 and first["token"] != TOKEN
    assert spawns[0]["token"] == first["token"] and first["childId"]
    takeover = manager.start(ACCOUNT, "u", "p")
    assert (takeover["token"], takeover["childId"]) == (first["token"], first["childId"])
    listing = manager.entries()
    assert listing[0]["childId"] == first["childId"] and "token" not in listing[0]
    other = manager.start("tqkq-sim:second0001", "u2", "p2")
    assert other["token"] != first["token"] and other["childId"] != first["childId"]
    processes[0].returncode = 137  # A 崩溃后重启：新身份
    revived = manager.start(ACCOUNT, "u", "p")
    assert revived["token"] != first["token"] and revived["childId"] != first["childId"]


def test_crashed_childs_port_is_not_handed_to_another_account_at_once():
    manager, _, processes = fake_manager()
    first = manager.start(ACCOUNT, "u", "p")
    processes[0].returncode = 137  # A 崩溃，条目仍在
    second = manager.start("tqkq-sim:second0001", "u2", "p2")
    assert second["port"] != first["port"]
    revived = manager.start(ACCOUNT, "u", "p")  # A 自己重启也不回到刚死的端口
    assert revived["port"] not in {first["port"], second["port"]}
    pool_module.PORT_QUARANTINE_SECONDS = 0.0
    try:
        manager.stop("tqkq-sim:second0001")
        third = manager.start("tqkq-sim:third00001", "u3", "p3")
        assert third["port"] in {first["port"], second["port"]}  # 隔离期过后可复用
    finally:
        pool_module.PORT_QUARANTINE_SECONDS = 300.0


def test_auth_failure_is_cached_and_expires():
    refused_status = {"dataStatus": "unavailable", "dataErrorCode": "tqkq_authentication_failed"}
    manager, spawns, processes = fake_manager(statuses=[refused_status, refused_status])
    with pytest.raises(PoolStartError) as refused:
        manager.start(ACCOUNT, "u", "wrong")
    assert refused.value.reason == "auth_failed"
    assert processes[0].terminated
    # 缓存期内不再起子进程（避免密码错反复重启）。
    with pytest.raises(PoolStartError) as cached:
        manager.start(ACCOUNT, "u", "wrong")
    assert cached.value.reason == "auth_failed" and len(spawns) == 1
    pool_module.AUTH_FAILURE_CACHE_SECONDS = 0.0
    try:
        with pytest.raises(PoolStartError) as retried:  # 过期后允许重试
            manager.start(ACCOUNT, "u", "wrong")
        assert retried.value.reason == "auth_failed" and len(spawns) == 2
    finally:
        pool_module.AUTH_FAILURE_CACHE_SECONDS = 600.0


def test_crashed_child_is_reported_and_restarted():
    manager, _, processes = fake_manager()
    manager.start(ACCOUNT, "u", "p")
    processes[0].returncode = 137
    assert [(item["status"], item.get("exitCode")) for item in manager.entries()] == [("exited", 137)]
    revived = manager.start(ACCOUNT, "u", "p")
    assert revived["status"] == "running"
    assert [(item["status"], item.get("exitCode")) for item in manager.entries()] == [("running", None)]


def test_timeout_kills_child():
    manager, spawns, processes = fake_manager(statuses=[{"dataStatus": "connecting"}])
    pool_module.START_TIMEOUT_SECONDS = 0.2
    try:
        with pytest.raises(PoolStartError) as timed_out:
            manager.start(ACCOUNT, "u", "p")
        assert timed_out.value.reason == "timeout"
    finally:
        pool_module.START_TIMEOUT_SECONDS = 60.0
    assert processes[0].terminated


def _pipe_process(line: str | None, *, hang: bool = False) -> SimpleNamespace:
    import os

    read_fd, write_fd = os.pipe()
    if line is not None:
        os.write(write_fd, (line + "\n").encode())
    os.close(write_fd)
    reader = os.fdopen(read_fd, "r")
    state = {"killed": False}

    def kill():
        state["killed"] = True

    process = SimpleNamespace(
        pid=-1, stdout=reader, stdin=io.StringIO(),
        poll=lambda: None if not state["killed"] else -9,
        kill=kill, wait=lambda timeout=None: -9,
    )
    if hang:
        process.poll = lambda: None
    return process


def test_verify_reads_result_line_and_cleans_up():
    # fixture=False: this exercises the real verify-subprocess protocol (the
    # fixture branch above accepts credentials without spawning anything).
    manager = PoolManager(
        token=TOKEN, port=0, max_accounts=2, base_port=19100,
        symbols=["SHFE.au2612"], fixture=False,
    )
    process = _pipe_process(json.dumps({"ok": True, "kqUsername": "kq-user"}))
    manager._spawn_verify = lambda username, password: process  # type: ignore[method-assign]
    assert manager.verify("kq-user", "pw") == {"ok": True, "kqUsername": "kq-user"}
    assert process.poll() == -9  # 结束即关


def test_verify_maps_the_child_refusal_to_the_control_plane_reason():
    # A wrong password reaches the login page as `auth_failed` (the same word
    # start() uses), not the child's raw tqkq_* code (09-22 acceptance).
    manager = PoolManager(
        token=TOKEN, port=0, max_accounts=2, base_port=19100,
        symbols=["SHFE.au2612"], fixture=False,
    )
    process = _pipe_process(json.dumps({"ok": False, "reason": "tqkq_authentication_failed"}))
    manager._spawn_verify = lambda username, password: process  # type: ignore[method-assign]
    assert manager.verify("kq-user", "wrong") == {"ok": False, "reason": "auth_failed"}


def test_verify_timeout_returns_reason():
    manager = PoolManager(
        token=TOKEN, port=0, max_accounts=2, base_port=19100,
        symbols=["SHFE.au2612"], fixture=False,
    )
    process = _pipe_process(None, hang=True)
    manager._spawn_verify = lambda username, password: process  # type: ignore[method-assign]
    pool_module.VERIFY_TIMEOUT_SECONDS = 0.2
    try:
        assert manager.verify("kq-user", "pw") == {"ok": False, "reason": "timeout"}
    finally:
        pool_module.VERIFY_TIMEOUT_SECONDS = 60.0
        process.stdout.close()


def _request(server: PoolServer, method: str, path: str, payload: dict | None = None,
             token: str = TOKEN):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=body, method=method,
        headers={"Authorization": f"Bearer {token}",
                 **({"Content-Type": "application/json"} if body else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


def test_control_plane_auth_routes_and_error_mapping():
    manager, _, _ = fake_manager(max_accounts=1, statuses=[{"dataStatus": "simulated"}])
    manager.fixture = False  # exercise the real verify-subprocess protocol below
    manager._spawn_verify = lambda username, password: _pipe_process(  # type: ignore[method-assign]
        json.dumps({"ok": True, "kqUsername": username}))
    server = PoolServer(("127.0.0.1", 0), manager)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        status, _ = _request(server, "GET", "/pool/accounts", token="wrong-token-x")
        assert status == 401
        status, listing = _request(server, "GET", "/pool/accounts")
        assert status == 200 and listing["capacity"] == {"max": 1, "running": 0}
        status, started = _request(server, "POST", f"/pool/accounts/{ACCOUNT}/start",
                                   {"username": "u", "password": "p"})
        assert status == 200 and started["port"] == 19100
        status, refused = _request(server, "POST", "/pool/accounts/tqkq-sim:second0001/start",
                                   {"username": "u", "password": "p"})
        assert status == 503 and refused == {"error": "capacity_full"}
        status, verified = _request(server, "POST", "/pool/verify",
                                    {"username": "u", "password": "p"})
        assert status == 200 and "ok" in verified
        status, stopped = _request(server, "POST", f"/pool/accounts/{ACCOUNT}/stop")
        assert status == 200 and stopped["stopped"] is True
        status, _ = _request(server, "GET", "/healthz")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_real_fixture_child_starts_and_stops():
    """真实子进程（fixture 模式）：stdin 配置 → readiness → /healthz → 停止。"""
    manager = PoolManager(
        token=TOKEN, port=0, max_accounts=2, base_port=19100,
        symbols=["SHFE.au2612"], fixture=True,
    )
    deadline = time.monotonic() + 30.0
    result = None
    while result is None and time.monotonic() < deadline:
        try:
            result = manager.start(ACCOUNT, "fixture-user", "fixture-pass")
        except PoolStartError:
            time.sleep(1.0)  # 端口偶发占用：换端口重试
    assert result is not None and result["status"] == "running"
    health = manager._probe(result["port"], result["token"])
    assert health is not None and health["dataStatus"] in {"simulated", "normal"}
    assert manager.stop(ACCOUNT)["stopped"] is True
    assert manager.entries() == []


def _child_get(port: int, path: str, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


def test_real_child_on_a_reused_port_refuses_the_previous_childs_token():
    """端口复用串号（2026-10-12 审查）：A 的子进程崩溃，B 的子进程拿到同一端口；
    拿着 A 的旧 token 打这个端口一律 401，读不到 B 的账户；池的共享 token 在子进程上
    只开公共行情，开不了账户。"""
    manager = PoolManager(
        token=TOKEN, port=0, max_accounts=2, base_port=19300,
        symbols=["SHFE.au2612"], fixture=True,
    )
    pool_module.PORT_QUARANTINE_SECONDS = 0.0  # 强制复用，模拟最坏情况
    try:
        first = manager.start(ACCOUNT, "fixture-a", "pass-a")
        manager._entries[ACCOUNT].process.kill()  # A 崩溃，条目清掉、端口释放
        manager.stop(ACCOUNT)
        second = manager.start("tqkq-sim:second0001", "fixture-b", "pass-b")
        assert second["port"] == first["port"]  # B 拿到了 A 的端口
        port = second["port"]
        for path in ("/v1/status", "/v1/account", "/v1/positions", "/v1/activity", "/v1/snapshot"):
            status, body = _child_get(port, path, first["token"])
            assert (status, body) == (401, {"error": "sidecar_auth_required"}), path
            status, _ = _child_get(port, path, TOKEN)  # 池控制 token 也开不了账户
            assert status == 401, path
        status, _ = _child_get(port, "/v1/account", second["token"])
        assert status == 200
        status, body = _child_get(port, "/v1/public-market/instruments", TOKEN)
        assert status != 401, body  # 公共行情仍认池 token
    finally:
        pool_module.PORT_QUARANTINE_SECONDS = 300.0
        manager.stop_all()


def _child_post(port: int, path: str, token: str, payload: dict) -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {token}",
                                                             "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_real_market_only_child_serves_public_market_and_no_account():
    """行情专用子进程（真实子进程，fixture 行情）：经 stdin 拿到 marketOnly，healthz 报 marketOnly、
    能力里没有账户与下单；它自己的 token 也读不到账户/持仓/委托（409 market_only_session），
    下单 405；公共行情（合约、报价总表、K 线工作台）照常。"""
    manager = PoolManager(
        token=TOKEN, port=0, max_accounts=2, base_port=19400,
        symbols=["SHFE.au2612"], fixture=True,
    )
    try:
        result = manager.start(ACCOUNT, "fixture-user", "fixture-pass", market_only=True)
        port, token = result["port"], result["token"]
        assert result["marketOnly"] is True
        status, health = _child_get(port, "/healthz", token)
        assert status == 200 and health["marketOnly"] is True and health["simulationTrading"] is False
        assert not {"account.read", "positions.read", "simulation_order.submit"} & set(health["capabilities"])
        assert "market.read" in health["capabilities"]
        for path in ("/v1/account", "/v1/positions", "/v1/activity", "/v1/snapshot"):
            assert _child_get(port, path, token) == (409, {"error": "market_only_session"}), path
        assert _child_post(port, "/v1/simulation/orders/submit", token, {"clientOrderId": "x"}) == 405
        status, instruments = _child_get(port, "/v1/public-market/instruments", TOKEN)
        assert status == 200 and instruments.get("items"), instruments
        status, workbench = _child_get(port, "/v1/public-market/SHFE.au2612/workbench?duration=300&waitMs=5000", TOKEN)
        assert status == 200 and workbench.get("bars"), workbench
    finally:
        manager.stop_all()


def test_both_local_http_servers_queue_a_burst_of_connections():
    """10-12 容量测试（同 #663）: the sidecar's read server and the pool's control plane listen with a
    backlog of 128 — the stdlib default of 5 refused connections when the Runtime opened many at once."""
    from tqkq_readonly_sidecar.main import ReadOnlyServer

    assert ReadOnlyServer.request_queue_size == PoolServer.request_queue_size == 128
