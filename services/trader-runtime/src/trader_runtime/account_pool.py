"""M1a 按需装配：tqkq-pool 账户 ↔ 接入池子进程。

`ensure` 把账户表里的 `tqkq-pool` 行变成一套真实服务：解密托管密码 → 让池
起子进程 → 按 sidecar 端口装配 provider/订单/自主执行/账本 → 复刻启动时的
循环启停。`release` 反向拆掉并停子进程（idle/unbind/用户禁用）；主账号与
`always_on` 的账户永不回收。进程退出只 `detach` 自己的装配不停子进程——蓝绿
部署的新旧槽共享池子里的子连接，旧槽退出把它停掉就断了在线成员；留在池里
没人接的子连接由启动时的孤儿收养交给同一套空闲回收。所有口令只经内存与本
地 HTTP 传递，日志只记账户与原因。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from .credential_box import CredentialBox
from .presence import SCAN_INTERVAL_SECONDS, PresenceTracker, idle_seconds_from_env
from .sidecar_http import SidecarHttp, SidecarHttpError
from .store.trading_accounts import is_market_source_owner
from .wiring.accounts import AccountRegistry, AccountServices, AccountSpec
from .wiring.services import assemble_account, build_account_services, dismantle_account

log = logging.getLogger("uvicorn.error.account_pool")

POOL_URL_ENV = "AI_TRADER_TQKQ_POOL_URL"
POOL_STATE_CONNECTING = "connecting"
POOL_STATE_QUEUED = "queued"
POOL_STATE_CONNECTED = "connected"
POOL_STATE_OFFLINE = "offline"
# Pool-child market clients kept at once (one per child port; the pool runs a handful of children).
MARKET_CHILD_CLIENTS = 32


class PoolError(RuntimeError):
    """The access pool answered something the runtime cannot assemble from."""


class PoolStartRefused(PoolError):
    """The pool refused a start; `reason` is its control-plane contract
    (auth_failed / permission_denied / timeout / start_failed / pool_unavailable…)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PoolCapacityFull(PoolError):
    """The pool is at its account limit; the account stays bound and queued."""


def _child_identity(started: dict[str, Any]) -> tuple[int, str, str]:
    """(port, childId, token) of the child a pool start answered with. The token is
    that spawn's own: the assembled provider uses it, so a request reaching another
    child on a reused port is refused (401) instead of reading that account. A pool
    that names no child identity is refused outright — never fall back to the shared
    token, which every child used to accept (2026-10-12 port-reuse review)."""
    try:
        port = int(started["port"])
    except (KeyError, TypeError, ValueError) as error:
        raise PoolStartRefused("pool_protocol_error") from error
    child_id = started.get("childId")
    token = started.get("token")
    if (not isinstance(child_id, str) or not child_id
            or not isinstance(token, str) or len(token) < 32):
        raise PoolStartRefused("pool_protocol_error")
    return port, child_id, token


def _child_matches(item: Any, account_id: str, child: tuple[int, str] | None) -> bool:
    """A pool listing row is the running child this process assembled against."""
    return (child is not None and isinstance(item, dict)
            and str(item.get("accountId")) == account_id and item.get("status") == "running"
            and item.get("childId") == child[1] and item.get("port") == child[0])


class PoolClient:
    """The pool's control plane over localhost HTTP; Bearer token like the sidecar."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 75.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Public market reads, one SidecarHttp per pool child (by port): each child gets its own
        # short-read and long-poll pools, so one slow child or a burst of parked K-line polls never
        # takes another child's connections or the control plane's (2026-10-12 容量测试).
        self._market_http: OrderedDict[int, SidecarHttp] = OrderedDict()
        self._closing: set[asyncio.Task] = set()
        # The control plane's own kept-alive pool (2026-10-13 扩容①): a client per call rebuilt its
        # TLS context and connection every time the account list was asked for (~2% of a core).
        self._control: SidecarHttp | None = None

    def _control_http(self) -> SidecarHttp:
        if self._control is None:
            self._control = SidecarHttp(self.base_url, self.token, short_connections=8, long_poll_connections=1)
        return self._control

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            body: Any = await self._control_http().request_async(
                method, path, payload, timeout=self.timeout, long_poll=False)
        except SidecarHttpError as error:
            if error.status is None:
                raise PoolStartRefused("pool_unavailable" if str(error) == "sidecar_transport_unavailable"
                                       else "pool_protocol_error") from error
            if str(error) == "sidecar_invalid_json":
                raise PoolStartRefused("pool_protocol_error") from error
            reason = str(error.payload.get("error") or "pool_error")
            if reason == "capacity_full":
                raise PoolCapacityFull(reason)
            raise PoolStartRefused(reason) from error
        return body if isinstance(body, dict) else {}

    async def start(self, account_id: str, username: str, password: str, *,
                    market_only: bool = False) -> dict[str, Any]:
        """`market_only`: a 行情专用 child (2026-10-13) — data-only session, public market only."""
        return await self._request(
            "POST", f"/pool/accounts/{account_id}/start",
            {"username": username, "password": password, **({"marketOnly": True} if market_only else {})},
        )

    async def stop(self, account_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/pool/accounts/{account_id}/stop")

    async def verify(self, username: str, password: str) -> dict[str, Any]:
        return await self._request("POST", "/pool/verify",
                                   {"username": username, "password": password})

    async def entries(self) -> dict[str, Any]:
        return await self._request("GET", "/pool/accounts")

    async def market(self, port: int, path: str) -> dict[str, Any]:
        if not path.startswith("/v1/public-market/"):
            raise PoolStartRefused("public_market_path_required")
        http = self._market_http.get(port)
        if http is None:
            http = self._market_http[port] = SidecarHttp(f"http://127.0.0.1:{port}", self.token)
            while len(self._market_http) > MARKET_CHILD_CLIENTS:
                # A child that moved to another port long ago: its idle pools close in the background.
                _, stale = self._market_http.popitem(last=False)
                closing = asyncio.get_running_loop().create_task(stale.close())
                self._closing.add(closing)
                closing.add_done_callback(self._closing.discard)
        self._market_http.move_to_end(port)
        try:
            body = await http.request_async("GET", path, timeout=15)
        except SidecarHttpError as error:
            code = error.payload.get("error") if error.status is not None else None
            raise PoolStartRefused(str(code or "public_market_unavailable")) from error
        if not isinstance(body, dict):
            raise PoolStartRefused("public_market_protocol_error")
        return body

    async def close(self) -> None:
        control, self._control = self._control, None
        if control is not None:
            await control.close()
        clients, self._market_http = list(self._market_http.values()), OrderedDict()
        for http in clients:
            await http.close()


class AccountPoolManager:
    """Assembles and releases pool accounts; owns presence and idle reclamation."""

    def __init__(
        self, *, client: PoolClient, registry: AccountRegistry, store: Any,
        credential_box: CredentialBox, services: Any, settings: Any,
        runtime_feed: Any, status_for_session: Callable[[str], dict[str, object]],
        idle_seconds: float | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.store = store
        self.credential_box = credential_box
        self.services = services
        self.settings = settings
        self.runtime_feed = runtime_feed
        self._status_for_session = status_for_session
        self.presence = PresenceTracker()
        self.idle_seconds = idle_seconds if idle_seconds is not None else idle_seconds_from_env()
        self._simulation_trading = os.getenv("AI_TRADER_SIMULATION_TRADING") == "1"
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, str] = {}
        self._unwire: dict[str, Callable[[], None]] = {}
        # (port, childId) of the child each assembled account is wired to.
        self._children: dict[str, tuple[int, str]] = {}
        self._scan_task: asyncio.Task[None] | None = None
        self._stopped = False
        # Pool children running at this process's boot that nobody here
        # assembled — left by an exited slot; reclaimed by the idle scan.
        self._adopted: set[str] = set()
        self._market_last_seen: dict[str, float] = {}
        # The process start: a user this runtime never saw counts as absent
        # since then, so an account nobody has heartbeated here keeps its
        # child for the full `idle_seconds`, not just to the first scan.
        self._started = time.monotonic()
        # Whether this process may run the ordering/bookkeeping loops of an
        # account it assembles. The lease holder runs them; a blue-green
        # standby slot that takes a member's request over assembles the
        # read-only services only, and `lease_acquired` starts the loops for
        # every registry account once the lease lands on it. `main.py` points
        # this at the instance lease; the default keeps single-process boots
        # assembling exactly as before.
        self.may_run_loops: Callable[[], bool] = lambda: True

    # -- assembly -------------------------------------------------------------

    def market_credentials(self, account_id: str) -> tuple[str, Callable[[], str]]:
        """One active pool account's TqKq username and an opener for its sealed
        password — what a market-data session logs in with. The pool child start
        below and the paper engine's market relay (paper_accounts.
        PaperMarketSourceSync) both take them from here; the password is opened
        only when a login really needs it and never leaves memory."""
        record = self.store.get_trading_account(account_id)
        if record is None or record["provider_kind"] != "tqkq-pool" or record["status"] != "active":
            raise PoolError("account_not_pool_managed")
        owner = str(record["execution_owner_user_id"])
        credentials = self.store.load_tqkq_credentials(user_id=owner)
        if credentials is None or str(credentials["account_id"]) != account_id:
            raise PoolStartRefused("credentials_missing")

        def password() -> str:
            return self.credential_box.open(
                user_id=owner, ciphertext=bytes(credentials["ciphertext"]), nonce=bytes(credentials["nonce"]),
            )
        return str(credentials["kq_username"]), password

    async def market_source(self, account_id: str) -> int:
        """Reuse one designated child's public data without assembling trading services."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            username, password = self.market_credentials(account_id)
            entries = await self.client.entries()
            child = next((item for item in entries.get("accounts", [])
                          if item.get("accountId") == account_id and item.get("status") == "running"), None)
            existing = self.registry.get(account_id)
            if existing is not None and not _child_matches(child, account_id, self._children.get(account_id)):
                # A restarted pool may assign a different port. Detach stale trading
                # wiring before reconnecting; only an authenticated account request reassembles it.
                await self._detach_locked(account_id, reason="market_source_child_lost")
            if child is None:
                # A 行情专用 account (owner `market-source:<id>`, nobody's trading account) gets a
                # data-only child with no TqKq trading session; a member's account serving as a
                # source keeps its normal child.
                record = self.store.get_trading_account(account_id)
                market_only = record is not None and is_market_source_owner(record["execution_owner_user_id"])
                child = await self.client.start(account_id, username, password(), market_only=market_only)
            self._market_last_seen[account_id] = time.monotonic()
            if self.registry.get(account_id) is None:
                self._adopted.add(account_id)
            return int(child["port"])

    async def ensure(self, account_id: str, *, reason: str, touch_presence: bool = True) -> AccountServices:
        """The account's services, assembling them first when needed. Safe to
        call concurrently: one assembler per account, everyone else waits; an
        assembly that fails or is cancelled leaves nothing registered behind
        (the pool child stays — a blue-green sibling may share it)."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            existing = self.registry.get(account_id)
            if existing is not None:
                return existing
            record = self.store.get_trading_account(account_id)
            if record is None or record["provider_kind"] != "tqkq-pool" or record["status"] != "active":
                raise PoolError("account_not_pool_managed")
            owner_user_id = str(record["execution_owner_user_id"])
            if touch_presence:  # a background heal is not the user showing up
                self.presence.touch(owner_user_id)
            self._states[account_id] = POOL_STATE_CONNECTING
            try:
                credentials = self.store.load_tqkq_credentials(user_id=owner_user_id)
                if credentials is None or str(credentials["account_id"]) != account_id:
                    raise PoolStartRefused("credentials_missing")
                password = self.credential_box.open(
                    user_id=owner_user_id,
                    ciphertext=bytes(credentials["ciphertext"]), nonce=bytes(credentials["nonce"]),
                )
                started = await self.client.start(account_id, str(credentials["kq_username"]), password)
                port, child_id, child_token = _child_identity(started)
                services = build_account_services(
                    AccountSpec(
                        id=account_id, display_name=str(record["display_name"]),
                        provider_kind="tqkq-sidecar",
                        sidecar_url=f"http://127.0.0.1:{port}",
                        sidecar_token=child_token,
                    ),
                    store=self.store, broker=self.services.broker,
                    policies=self.services.policies, instruments=self.services.instruments,
                    auth_mode=self.settings.auth_mode, simulation_gateway=None,
                    simulation_trading=self._simulation_trading,
                )
                self._adopted.discard(account_id)  # assembled: the normal idle scan owns it
                # Recorded before the wiring is registered: a concurrent child_lost check
                # must see which child this assembly belongs to.
                self._children[account_id] = (port, child_id)
                # A standby slot leaves the loops down (the lease holder is
                # already running them for this account); the read services
                # serve its traffic meanwhile.
                self._unwire[account_id] = await assemble_account(
                    services, registry=self.registry, store=self.store,
                    proposals=self.services.proposals, runtime_feed=self.runtime_feed,
                    publish_status=lambda: self._publish_status(account_id),
                    run_loops=self.may_run_loops(),
                )
                self._states.pop(account_id, None)
                log.info("account_pool start account=%s reason=%s", account_id, reason)
                await self._publish_status(account_id)
                return services
            except PoolCapacityFull:
                self._states[account_id] = POOL_STATE_QUEUED
                log.warning("account_pool queued account=%s reason=capacity_full", account_id)
                raise
            except BaseException:
                self._states.pop(account_id, None)
                if self.registry.get(account_id) is None:
                    self._children.pop(account_id, None)
                raise

    # -- release --------------------------------------------------------------

    async def _detach_locked(self, account_id: str, *, reason: str) -> bool:
        """Dismantle the wiring for one pool account under its assembly lock:
        loops, subscriptions, provider. Caller holds the lock."""
        services = self.registry.get(account_id)
        if services is None:
            return False
        self.registry.remove(account_id)
        self._states.pop(account_id, None)
        self._children.pop(account_id, None)
        await dismantle_account(services, self._unwire.pop(account_id, lambda: None))
        log.info("account_pool detach account=%s reason=%s", account_id, reason)
        await self._publish_status(account_id)
        return True

    async def detach(self, account_id: str, *, reason: str) -> bool:
        """Dismantle this process's wiring for one pool account and leave the
        pool child running. What a Runtime exit (and the self-heal in
        `RuntimeContext.account_context`) must do: the child may be shared
        with a blue-green sibling slot still serving it."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            return await self._detach_locked(account_id, reason=reason)

    async def release(self, account_id: str, *, reason: str) -> bool:
        """Dismantle one account and stop its child — the release paths where
        the account itself goes away (idle reclamation, unbind, a disabled
        user). The default account and `always_on` specs are never released.
        An account this process never assembled is not wired here, but its
        child still gets stopped: an adopted orphan being unbound must not
        outlive its account row. The stop stays inside the assembly lock so a
        concurrent `ensure` cannot wire a child this release is stopping."""
        if account_id == self.registry.default.account_id:
            return False
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            services = self.registry.get(account_id)
            if reason == "idle" and self._market_active(account_id):
                return False
            if services is not None and services.spec is not None and services.spec.always_on:
                return False
            detached = await self._detach_locked(account_id, reason=reason)
            self._adopted.discard(account_id)
            self._market_last_seen.pop(account_id, None)
            try:
                await self.client.stop(account_id)
            except PoolError as error:
                log.warning("account_pool stop_failed account=%s error=%s", account_id, error)
            log.info("account_pool stop account=%s reason=%s", account_id, reason)
            if detached:
                await self._publish_status(account_id)
            return True

    async def child_lost(self, account_id: str) -> bool:
        """Whether an account this process has assembled is no longer served
        by the very child it was wired to — stopped from under this runtime (a
        blue-green sibling slot exiting, the pool restarting) or crashed and
        restarted as a new child (new childId, maybe another port). An
        unreachable pool counts as not lost: the wiring's own token keeps any
        other child from answering it meanwhile, and the next check retries."""
        if self.registry.get(account_id) is None:
            return False
        try:
            entries = await self.client.entries()
        except PoolError:
            return False
        return account_id in self._lost_children(entries, [account_id])

    def _lost_children(self, entries: dict[str, Any], account_ids: list[str]) -> set[str]:
        accounts = entries.get("accounts")
        if not isinstance(accounts, list):
            return set()
        return {
            account_id for account_id in account_ids
            if not any(_child_matches(item, account_id, self._children.get(account_id))
                       for item in accounts)
        }

    async def _heal_lost_children(self) -> None:
        """Background side of child_lost: an assembled account whose child is
        gone or replaced is detached even when no request comes (its loops would
        otherwise keep calling a dead or foreign port), and reassembled on the
        new child while its users are still around."""
        assembled = [
            services.account_id for services in self.registry.all()
            if (record := self.store.get_trading_account(services.account_id)) is not None
            and record["provider_kind"] == "tqkq-pool"
        ]
        if not assembled:
            return
        try:
            entries = await self.client.entries()
        except PoolError:
            return
        for account_id in sorted(self._lost_children(entries, assembled)):
            await self.detach(account_id, reason="child_lost")
            if self._everyone_idle(account_id):
                continue
            try:
                await self.ensure(account_id, reason="heal", touch_presence=False)
            except PoolError as error:
                log.warning("account_pool heal_failed account=%s error=%s", account_id, error)

    def account_state(self, account_id: str) -> str:
        """connecting / queued while assembly is in flight or waiting for
        capacity, connected once assembled, offline otherwise."""
        state = self._states.get(account_id)
        if state is not None:
            return state
        if self.registry.get(account_id) is not None:
            return POOL_STATE_CONNECTED
        return POOL_STATE_OFFLINE

    # -- observability ----------------------------------------------------------

    async def _publish_status(self, account_id: str) -> None:
        for session_id in self.services.broker.subscribed_session_ids():
            session = self.store.get_session(session_id)
            if session is None or str(session["tradingAccountId"]) != account_id:
                continue
            try:
                payload = self._status_for_session(session_id)
            except KeyError:
                continue
            await self.services.broker.publish(
                session_id, kind="result", name="runtime.status", payload=payload,
            )

    # -- idle reclamation -------------------------------------------------------

    def _market_active(self, account_id: str) -> bool:
        return time.monotonic() - self._market_last_seen.get(account_id, float("-inf")) < self.idle_seconds

    def _everyone_idle(self, account_id: str) -> bool:
        """True when every granted user is out of session: their last
        heartbeat is past `idle_seconds`. A user this process never saw
        counts as absent since its boot — a just-adopted orphan (or an
        account assembled without its own user's heartbeat yet) keeps its
        child for the full `idle_seconds` after this Runtime started, never
        a first-scan stop while the old slot may still be serving it."""
        if self._market_active(account_id):
            return False
        grantees = self.store.list_account_grantees(account_id=account_id)
        if not grantees:
            return False  # no grant rows to reason about: leave the account alone
        now = time.monotonic()
        for user in grantees:
            idle = self.presence.idle_seconds(user, now=now)
            if idle is None:
                idle = now - self._started
            if idle < self.idle_seconds:
                return False
        return True

    async def _scan_idle(self) -> None:
        await self._heal_lost_children()
        for services in list(self.registry.all()):
            account_id = services.account_id
            record = self.store.get_trading_account(account_id)
            if record is None or record["provider_kind"] != "tqkq-pool":
                continue  # the default account and file accounts stay
            if services.spec is not None and services.spec.always_on:
                continue
            if self._everyone_idle(account_id):
                await self.release(account_id, reason="idle")
        for account_id in sorted(self._adopted):
            if not self._everyone_idle(account_id):
                continue
            # Same assembly lock as ensure: a user showing up mid-scan takes
            # the child over instead of having it stopped out from under them.
            lock = self._locks.setdefault(account_id, asyncio.Lock())
            async with lock:
                if not self._everyone_idle(account_id):
                    continue
                if self.registry.get(account_id) is not None:
                    self._adopted.discard(account_id)  # assembled since: the scan above owns it
                    continue
                self._adopted.discard(account_id)
                self._market_last_seen.pop(account_id, None)
                try:
                    await self.client.stop(account_id)
                except PoolError as error:
                    log.warning("account_pool stop_failed account=%s error=%s", account_id, error)
                else:
                    log.info("account_pool stop account=%s reason=orphan_idle", account_id)

    async def _scan_loop(self) -> None:
        while not self._stopped:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            if self._stopped:
                return
            try:
                await self._scan_idle()
            except Exception:
                log.exception("account_pool_idle_scan_failed")

    async def adopt_orphans(self) -> None:
        """Pool children still running that this process never assembled —
        left behind when a previous slot exited without stopping them
        (blue-green). Adopt them for idle reclamation: no assembly, no port
        wiring; a user showing up re-assembles through `ensure` as usual."""
        try:
            entries = await self.client.entries()
        except PoolError as error:
            log.warning("account_pool adopt_skipped error=%s", error)
            return
        assembled = {services.account_id for services in self.registry.all()}
        for item in entries.get("accounts") or []:
            if not isinstance(item, dict) or item.get("status") != "running":
                continue
            account_id = str(item.get("accountId") or "")
            record = self.store.get_trading_account(account_id) if account_id else None
            if (account_id in assembled or record is None
                    or record["provider_kind"] != "tqkq-pool" or record["status"] != "active"):
                continue
            self._adopted.add(account_id)
        if self._adopted:
            log.info("account_pool adopted_orphans accounts=%s", sorted(self._adopted))

    async def start_background(self) -> None:
        """The idle scanner, preceded by boot-time orphan adoption: children
        still running that nobody here assembled get reclaimed by the same
        idle rule once their users stay away."""
        await self.adopt_orphans()
        if self._scan_task is None:
            self._scan_task = asyncio.create_task(self._scan_loop())

    async def shutdown(self) -> None:
        """Runtime is going down: stop the scanner and detach this process's
        wiring for every pool account — the children themselves keep running.
        A blue-green sibling slot may still be serving them, and any leftover
        is reclaimed by the idle scan of whichever slot outlives it; stopping
        them here is what took an online member's connection down with the
        old slot (2026-09-24)."""
        self._stopped = True
        if self._scan_task is not None:
            self._scan_task.cancel()
            await asyncio.gather(self._scan_task, return_exceptions=True)
            self._scan_task = None
        pool_accounts = [
            services.account_id for services in self.registry.all()
            if (record := self.store.get_trading_account(services.account_id)) is not None
            and record["provider_kind"] == "tqkq-pool"
        ]
        for account_id in pool_accounts:
            await self.detach(account_id, reason="shutdown")
        close = getattr(self.client, "close", None)
        if close is not None:
            await close()
