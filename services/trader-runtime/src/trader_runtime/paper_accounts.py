"""本站模拟账户（paper）：注册即开户，托管在本站撮合引擎。

`AI_TRADER_PAPER_ENGINE_URL` 配置了引擎才启用；不配置时一切行为与之前完全
一致。引擎的每账户 API 与只读 sidecar 同形，只多了 `/accounts/{id}` 路径前
缀，所以 `PaperProvider` 直接继承 sidecar provider：账户、持仓、委托、成交
和盘前校验行情都走引擎，其余行情委托给默认账户的 provider（共享行情源，
不重复订阅）。`PaperAccountManager` 账户行变成一套真实服务：不起子进程、
无容量排队——有活动策略/止损/未完成委托的账户常驻，其余按需装配、无人在线
时卸下。注册与老用户首次请求经 `ensure_paper_account` 幂等开户：登记该用户
唯一的账户编号 → 引擎建账户（初始资金 100 万，同编号幂等）→ 建账户行并授权
→ 无默认账户时设为默认。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .data_providers import ReadOnlyDataProvider, SidecarReadOnlyDataProvider
from .foreign_market import CHART_TIMEFRAMES
from .instruments import PAPER_FOREIGN_PREFIX, foreign_product_of, foreign_trade_name, is_foreign_paper
from .presence import SCAN_INTERVAL_SECONDS, idle_seconds_from_env
from .sidecar_http import SidecarHttp, SidecarHttpError
from .tls import shared_ssl_context
from .trading_costs import FeeSettings
from .wiring.accounts import AccountRegistry, AccountServices, AccountSpec
from .wiring.services import assemble_account, build_account_services, dismantle_account

log = logging.getLogger("uvicorn.error.paper_accounts")

PAPER_ENGINE_URL_ENV = "AI_TRADER_PAPER_ENGINE_URL"
PAPER_KIND = "paper"
PAPER_DISPLAY_NAME = "本站模拟"
PAPER_ACCOUNT_PREFIX = "paper:"
PAPER_INITIAL_BALANCE = 1_000_000.0
# 外盘本站模拟（外盘二期，2026-10-19）：每个本站模拟账户下一个美元子账户，账本在外盘撮合引擎
# （services/tqkq-sidecar paper_foreign.py，同一进程的第二个实例）。它和人民币账户同为 paper 类账户，
# 只靠编号前缀和引擎地址区分；编号与人民币账户同号（paper:abc → paperusd:abc），开户与补开都幂等。
PAPER_FOREIGN_ENGINE_URL_ENV = "AI_TRADER_PAPER_FOREIGN_ENGINE_URL"
PAPER_FOREIGN_DISPLAY_NAME = "本站模拟 · 美元"
# 美元子账户在事件循环里（策略监控）给的 MT5 读数多久内不重读：监控每秒一轮，桥一秒读一次足够。
FOREIGN_READ_FRESH_SECONDS = 1.0
PAPER_FOREIGN_INITIAL_BALANCE = 100_000.0


def foreign_paper_id(account_id: str) -> str:
    """人民币本站模拟账户的美元子账户编号（paper:abc → paperusd:abc）。"""
    return PAPER_FOREIGN_PREFIX + str(account_id).removeprefix(PAPER_ACCOUNT_PREFIX)
# One shared poller reads every assembled account's status/account/positions
# from the engine once a second, in one batch request per `PAPER_SNAPSHOT_BATCH`
# accounts (2026-10-03: one thread and three requests per account before).
PAPER_POLL_INTERVAL_SECONDS = 1.0
PAPER_SNAPSHOT_BATCH = 200
_SNAPSHOT_PARTS = {"/v1/status": "status", "/v1/account": "account", "/v1/positions": "positions"}
# connecting while assembly is in flight, connected once assembled, offline otherwise.
PAPER_STATE_CONNECTING = "connecting"
PAPER_STATE_CONNECTED = "connected"
PAPER_STATE_OFFLINE = "offline"


class PaperEngineError(RuntimeError):
    """The paper engine answered something the runtime cannot assemble from."""


class PaperEngineClient:
    """The paper engine's control plane over HTTP; Bearer token like the sidecar."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    async def create_account(self, account_id: str, initial_balance: float) -> dict[str, Any]:
        """Create (or take over) one account; the engine keeps this idempotent."""
        # the engine's create contract: exactly {accountId, initBalance}
        return await self._call("POST", "/accounts", {"accountId": account_id, "initBalance": initial_balance})

    async def reset(self, account_id: str) -> dict[str, Any]:
        """重置本站模拟账户 (#1204): the engine cancels live orders, clears positions and puts equity back to the
        initial balance, keeping the trade history; the body repeats the id as its confirmation."""
        return await self._call("POST", f"/accounts/{account_id}/reset", {"confirmAccountId": account_id})

    async def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=shared_ssl_context()) as client:
                response = await client.request(
                    method, f"{self.base_url}{path}", json=payload,
                    headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as error:
            raise PaperEngineError("paper_engine_unavailable") from error
        try:
            body: Any = response.json()
        except ValueError as error:
            raise PaperEngineError("paper_engine_protocol_error") from error
        if response.status_code >= 400:
            reason = str(body.get("error")) if isinstance(body, dict) else "paper_engine_error"
            raise PaperEngineError(reason or "paper_engine_error")
        return body if isinstance(body, dict) else {}

    async def health(self) -> dict[str, Any]:
        return await self._call("GET", "/healthz")

    async def set_market_sources(self, sources: list[dict[str, Any]]) -> dict[str, Any]:
        """The engine's ordered market-account list (paper_sources.py on the engine
        side). Pool accounts carry their TqKq login; the main sidecar account is
        `local` — the engine logs it in with its own mounted secrets."""
        return await self._call("POST", "/v1/market-sources", {"sources": sources})


# How often the lease holder checks the engine still has the current list.
PAPER_SOURCE_SYNC_SECONDS = 30.0
ENGINE_STATUS_TIMEOUT_SECONDS = 2.0


class PaperMarketSourceSync:
    """行情账号接力 (2026-10-12): hands the paper engine its ordered TqKq market
    accounts, so the engine's matching quotes relay across the same accounts, in
    the same order, as the public market (`market_sources`, maintained through
    `PUT /api/admin/market-sources`). One ordered list, one place to change it.

    Pool accounts go with their TqKq username and the password sealed in the
    account store (opened here, sent over the engine's Bearer-authenticated
    loopback — the same channel the pool start uses); the main sidecar account
    goes as `local` and the engine uses its own mounted secrets — the only
    sidecar account whose login the engine has (a static secondary sidecar is
    left out). Inactive or credential-less accounts are left out. A deployment
    that never configured a source list pushes nothing (the engine keeps its
    mounted account); a configured list that ends up empty is pushed empty and
    the engine has no matching source, like the public market. Only the lease
    holder pushes; it pushes when the list (or a login) changed, or when the
    engine's revision says it no longer holds what was pushed (an engine
    restart). No password is logged or kept beyond the request."""

    def __init__(self, *, client: Callable[[], Any], store: Any,
                 shared_market: Callable[[], Any], account_pool: Callable[[], Any],
                 may_run: Callable[[], bool], local_account_id: Callable[[], str | None],
                 interval_seconds: float = PAPER_SOURCE_SYNC_SECONDS) -> None:
        self._client = client  # the manager's current engine client
        self._local_account_id = local_account_id  # the main sidecar account (the engine's mounted login)
        self.store = store
        self._shared_market = shared_market
        self._account_pool = account_pool
        self._may_run = may_run
        self.interval_seconds = interval_seconds
        self._pushed: tuple[str, str] | None = None  # (payload fingerprint, engine revision)
        self._unsupported = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    def ordered_ids(self) -> list[str] | None:
        """The source order, or None when this deployment never configured one."""
        shared = self._shared_market()
        ids = shared.ordered_sources() if shared is not None else []
        if not ids and not self.store.market_sources_seeded():
            return None
        return ids

    def payload(self) -> list[dict[str, Any]] | None:
        ordered = self.ordered_ids()
        if ordered is None:
            return None
        sources: list[dict[str, Any]] = []
        pool = self._account_pool()
        local = self._local_account_id()
        for account_id in ordered:
            record = self.store.get_trading_account(account_id)
            if record is None or str(record["status"]) != "active":
                continue
            kind = str(record["provider_kind"])
            if kind == "tqkq-sidecar":
                if account_id == local:
                    sources.append({"accountId": account_id, "local": True})
                else:  # a secondary static sidecar: its login is not mounted in the engine
                    log.warning("paper_market_source_skipped account=%s reason=not_engine_mounted", account_id)
            elif kind == "tqkq-pool" and pool is not None:
                try:
                    username, password = pool.market_credentials(account_id)
                    sources.append({"accountId": account_id, "username": username, "password": password()})
                except Exception as error:  # a missing or unopenable login: leave it out
                    log.warning("paper_market_source_skipped account=%s reason=%s",
                                account_id, getattr(error, "reason", None) or type(error).__name__)
        return sources

    @staticmethod
    def _fingerprint(sources: list[dict[str, Any]]) -> str:
        return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()

    async def sync_once(self) -> bool:
        """Push when needed; True when a push happened."""
        if self._unsupported or not self._may_run():
            return False
        sources = self.payload()
        if sources is None:
            return False  # no source list configured: the engine keeps its mounted account
        fingerprint = self._fingerprint(sources)
        try:
            client = self._client()
            health = await client.health()
            revision = str((health.get("marketSource") or {}).get("revision") or "")
            if self._pushed == (fingerprint, revision):
                self.last_error = None
                return False
            answer = await client.set_market_sources(sources)
        except PaperEngineError as error:
            if str(error) == "paper_market_sources_unsupported":
                self._unsupported = True  # a recorded-quote engine: no TqKq login at all
            self.last_error = str(error)
            return False
        self._pushed = (fingerprint, str(answer.get("revision") or ""))
        self.last_error = None
        log.info("paper_market_sources_pushed accounts=%s",
                 ",".join(str(item["accountId"]) for item in sources) or "-")
        return True

    def poke(self) -> None:
        """The admin changed the list: check right away instead of at the next tick."""
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.sync_once()
            except Exception:
                log.exception("paper_market_source_sync_failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
            self._wake.clear()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def engine_status(self) -> dict[str, Any]:
        """Which account the engine's matching quotes come from now (ids and
        positions only), for the owner's market-sources view."""
        try:
            # Optional on the owner's source list: a hung engine must not hold it up.
            health = await asyncio.wait_for(self._client().health(), timeout=ENGINE_STATUS_TIMEOUT_SECONDS)
        except TimeoutError:
            return {"error": "paper_engine_timeout"}
        except PaperEngineError as error:
            return {"error": str(error)}
        status = health.get("marketSource")
        return status if isinstance(status, dict) else {"error": "paper_market_sources_unsupported"}


class PaperSnapshotPoller:
    """One thread refreshes every assembled paper account: each second one
    `POST /v1/snapshots` per batch fetches the status, account and positions of
    all of them, and each provider runs its usual refresh on its own slice —
    same status rules, freshness and degradation as its own reads. An account
    the engine cannot read degrades alone; a failed batch degrades the accounts
    in it. A newly added account is read right away instead of at the next tick.

    An engine without the batch route (404: the Runtime is switched before the
    engine image during a release, and an engine update may fail) is read
    account by account, as before."""

    def __init__(self, base_url: str, token: str, *,
                 interval_seconds: float = PAPER_POLL_INTERVAL_SECONDS) -> None:
        self._http = SidecarHttp(base_url.rstrip("/"), token)
        self.interval_seconds = interval_seconds
        self._providers: dict[PaperProvider, None] = {}
        self._fresh: set[PaperProvider] = set()
        self._lock = threading.Lock()
        # Held while one provider applies its slice; `remove` takes it so no
        # refresh lands on a provider after it was taken out.
        self._apply_lock = threading.Lock()
        self._wake = threading.Event()
        # Runs while any account is registered; exits when the last one leaves.
        self._thread: threading.Thread | None = None

    def add(self, provider: PaperProvider) -> None:
        with self._lock:
            self._providers[provider] = None
            self._fresh.add(provider)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="paper-snapshot-poller", daemon=True)
                self._thread.start()
        self._wake.set()

    def remove(self, provider: PaperProvider) -> None:
        """Blocking until an in-flight refresh of this provider has finished."""
        with self._lock:
            self._providers.pop(provider, None)
            self._fresh.discard(provider)
            if not self._providers:
                self._wake.set()  # the thread exits now instead of at the next tick
        with self._apply_lock:
            pass

    def _run(self) -> None:
        next_due = 0.0
        while True:
            with self._lock:
                if not self._providers:
                    self._thread = None  # the next add starts a new one
                    return
                now = time.monotonic()
                if now >= next_due:
                    targets = list(self._providers)
                    next_due = now + self.interval_seconds
                else:
                    targets = [provider for provider in self._providers if provider in self._fresh]
                self._fresh.clear()
            for start in range(0, len(targets), PAPER_SNAPSHOT_BATCH):
                self._refresh(targets[start:start + PAPER_SNAPSHOT_BATCH])
            self._wake.wait(max(0.0, next_due - time.monotonic()))
            self._wake.clear()

    def _refresh(self, providers: list[PaperProvider]) -> None:
        accounts: dict[str, Any] | None = None
        failure: Exception | None = None
        try:
            body = self._http.request(
                "POST", "/v1/snapshots", {"accountIds": [provider.account_id for provider in providers]},
                timeout=4.0)
            accounts = body.get("accounts") if isinstance(body, dict) else None
            if not isinstance(accounts, dict):
                raise ValueError("invalid_paper_snapshots")
        except SidecarHttpError as error:
            if error.status == 404:
                accounts = None  # an engine from before the batch route
            else:
                failure = error
        except (OSError, ValueError) as error:
            failure = error
        for provider in providers:
            with self._apply_lock:
                with self._lock:
                    if provider not in self._providers:
                        continue
                # One account's surprise (a subscriber raising) must not end the
                # thread every other account is refreshed by.
                try:
                    if failure is None and accounts is None:
                        provider._poll_once(provider._fetch)
                    else:
                        provider._poll_once(self._slice(accounts, provider.account_id, failure))
                except Exception:
                    log.exception("paper_snapshot_refresh_failed account=%s", provider.account_id)

    @staticmethod
    def _slice(accounts: dict[str, Any] | None, account_id: str,
               failure: Exception | None) -> Callable[[str], Any]:
        def fetch(path: str) -> Any:
            if failure is not None:
                raise failure
            entry = (accounts or {}).get(account_id)
            if not isinstance(entry, dict):
                raise ValueError("invalid_paper_snapshot")
            if "error" in entry:
                raise SidecarHttpError("sidecar_http_error", status=entry.get("httpStatus"), payload=entry)
            part = _SNAPSHOT_PARTS.get(path)
            if part is None or part not in entry:
                raise ValueError("paper_snapshot_part_missing")
            return entry[part]
        return fetch


class PaperProvider(SidecarReadOnlyDataProvider):
    """One paper account's provider: account state from the site-hosted matching
    engine, market data shared with the default account's provider.

    The engine's per-account API is the sidecar's shape under `/accounts/{id}`,
    so every inherited account read — `/v1/status`, `/v1/account`, `/v1/positions`,
    `/v1/activity`, the order gateway and the pre-order `/v1/market/{sym}/quote`
    (validation and matching stay same-source) — keeps working against the engine.
    The remaining market reads delegate to the default provider: one set of
    subscriptions, one source of truth for prices."""

    def __init__(
        self, *, account_id: str, base_url: str, token: str, market_source: ReadOnlyDataProvider,
        poller: PaperSnapshotPoller,
    ) -> None:
        super().__init__(base_url=base_url, token=token, symbols=(), mode="paper")
        self.account_id = account_id
        self._poller = poller
        self._first_snapshot = threading.Event()
        # The default provider's optional sidecar-only surface (settlements,
        # ranking, backtest …) is probed per deployment like everywhere else;
        # the base protocol deliberately does not name those methods.
        self.market_source: Any = market_source

    # -- snapshots: the shared poller instead of a thread per account -----------

    async def start(self) -> None:
        self._stop.clear()
        self._poller.add(self)

    async def stop(self) -> None:
        self._stop.set()
        await asyncio.to_thread(self._poller.remove, self)
        # Remove the only Runtime-held copy of the engine token for this account.
        await self._http.close()
        self._token = ""

    def _poll_once(self, fetch: Callable[[str], Any], *, read_unavailable: bool = False) -> None:
        # Paper's ledger remains readable when its market source is offline.
        # Keep the reported status/gateway unavailable: only the read projection
        # accepts these snapshots; execution's positions() still fails closed.
        super()._poll_once(fetch, read_unavailable=True)
        with self._lock:
            received = self._positions is not None
        if received:
            self._first_snapshot.set()

    async def positions_view(self, *, wait_seconds: float = 2.5) -> dict[str, Any]:
        """Bounded first read, then the latest ledger facts with summary's status.

        This is a display read, deliberately separate from positions() used by
        execution/recovery, where unknown current holdings must still fail.
        """
        if not self._first_snapshot.is_set() and wait_seconds > 0:
            try:
                # Include executor queue time in the bound during concurrent logins.
                await asyncio.wait_for(asyncio.to_thread(self._first_snapshot.wait, wait_seconds), wait_seconds)
            except TimeoutError:
                pass
        with self._lock:
            available = self._positions is not None
            items = [dict(item) for item in self._positions or []]
            status = str(self._status["accountStatus"]) if available else "unavailable"
        return {"items": items, "dataStatus": status, "snapshotAvailable": available,
                "isCached": status in {"connecting", "stale", "unavailable"},
                "ageSeconds": self._local_snapshot_age()}

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        """The engine's pre-order quote; a USD sub-account's contract is named from its trade catalog
        (“COMEX黄金2612”) instead of the MT5 description the engine carries (“Gold (Globex): December 2026”)."""
        snapshot = super().market_snapshot(turn_id, symbol)
        if is_foreign_paper(self.account_id):
            # Derived from the code alone (no catalog or board read on the quote path).
            name = foreign_trade_name(str(snapshot.get("symbol") or symbol))
            if name:
                snapshot = {**snapshot, "name": name}
        return snapshot

    # -- market data: shared with the deployment's default account ------------

    def instrument_catalog(self) -> dict[str, Any]:
        return self.market_source.instrument_catalog()

    def market_board(self) -> dict[str, Any]:
        return self.market_source.market_board()

    def market_quotes(self, symbols: Sequence[str]) -> dict[str, Any]:
        return self.market_source.market_quotes(symbols)

    def market_calendar(self) -> dict[str, Any]:
        return self.market_source.market_calendar()

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        return self.market_source.market_workbench(symbol, duration_seconds)

    def market_history_nowait(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        return self.market_source.market_history_nowait(symbol, duration_seconds, length)

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        return self.market_source.market_history(symbol, duration_seconds, length)

    def symbol_settlements(self, symbols: Sequence[str], days: int) -> dict[str, Any]:
        return self.market_source.symbol_settlements(symbols, days)

    def main_contract_history(self, symbol: str, days: int) -> dict[str, Any]:
        return self.market_source.main_contract_history(symbol, days)

    def symbol_ranking(self, symbol: str, ranking_type: str, days: int) -> dict[str, Any]:
        return self.market_source.symbol_ranking(symbol, ranking_type, days)

    def historical_backtest(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.market_source.historical_backtest(request)


class ForeignPaperMarket:
    """美元子账户的行情面（外盘二期）：合约目录是外盘的可下单月份合约（foreign_market.trade_catalog）；
    月份合约分钟到小时的 K 线与实时行情（回测、策略判定）读 MT5 这个月份本身（外盘三期），日线读同品种的
    快期外盘主连；其余行情读与人民币账户共用默认账户的行情源。下单前的报价、校验和撮合都在外盘引擎本身（PaperProvider 的账户路径）。"""

    def __init__(self, foreign: Any, default: Callable[[], Any]) -> None:
        self._foreign = foreign
        self._default = default
        self._reads: dict[tuple[str, str, int, int], tuple[float, Any]] = {}
        self._reading: set[tuple[str, str, int, int]] = set()
        self._lock = threading.Lock()

    def _mt5(self, key: tuple[str, str, int, int], fetch: Callable[[], Any]) -> Any:
        """一次 MT5 读（经行情桥的 HTTP）。工作线程里（回测、校验、条件读数）直接读；事件循环里（策略监控）
        绝不阻塞：给最近一次读到的结果（报价年龄加上缓存的时长，新鲜度照常判断），过期就在后台线程重读，
        一次都没读到时 ``market_series_not_ready``——监控按“K 线正在载入”等下一轮（与快期行情同一约定）。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            value = fetch()
            with self._lock:
                self._reads[key] = (time.monotonic(), value)
            return value
        with self._lock:
            held = self._reads.get(key)
            start = key not in self._reading and (held is None or time.monotonic() - held[0] > FOREIGN_READ_FRESH_SECONDS)
            if start:
                self._reading.add(key)
        if start:
            def run() -> None:
                try:
                    value = fetch()
                    with self._lock:
                        self._reads[key] = (time.monotonic(), value)
                except Exception as error:  # noqa: BLE001 — a failed refresh keeps the last read; the age says so
                    log.info("foreign_mt5_read_failed key=%s error=%s", key, type(error).__name__)
                finally:
                    with self._lock:
                        self._reading.discard(key)
            threading.Thread(target=run, name="foreign-mt5-read", daemon=True).start()
        if held is None:
            raise RuntimeError("market_series_not_ready")
        stamp, value = held
        if isinstance(value, dict) and isinstance(value.get("ageSeconds"), (int, float)):
            aged = value["ageSeconds"] + time.monotonic() - stamp
            value = {**value, "ageSeconds": aged, **({"quote": {**value["quote"], "ageSeconds": aged}}
                                                     if isinstance(value.get("quote"), dict) else {})}
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._default(), name)

    def instrument_catalog(self) -> dict[str, Any]:
        return self._foreign.trade_catalog()

    @staticmethod
    def _chart_symbol(symbol: str) -> str:
        product = foreign_product_of(symbol)
        return product.kqd if product is not None and product.kqd else symbol

    def market_workbench(self, symbol: str, duration_seconds: int) -> dict[str, Any]:
        # 外盘三期（方案 A）：月份合约的分钟到小时实时行情（策略判定）读 MT5 这个月份本身，与回测、撮合同源；日线
        # （外盘尾项 2026-10-21）也是，由它的小时线按 CME 交易日合成（快期外盘主连的日线按国内日历切）。
        if str(symbol).upper().startswith("CME.") and duration_seconds in CHART_TIMEFRAMES:
            workbench = self._mt5(("workbench", symbol.upper(), duration_seconds, 0),
                                  lambda: self._foreign.workbench(symbol, duration_seconds))
            if workbench is None:
                # 这个月份此刻没有 MT5 报价或 K 线：等它回来，不拿快期主连顶替（判定和撮合必须是同一份价格）。
                raise RuntimeError("market_series_not_ready")
            return workbench
        return self._default().market_workbench(self._chart_symbol(symbol), duration_seconds)

    def market_history_nowait(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        if str(symbol).upper().startswith("CME.") and duration_seconds in CHART_TIMEFRAMES:
            history = self._mt5(("history", symbol.upper(), duration_seconds, length),
                                lambda: self._foreign.history(symbol, duration_seconds, length))
            if history is None:
                raise RuntimeError("market_series_not_ready")
            return history
        return self._default().market_history_nowait(self._chart_symbol(symbol), duration_seconds, length)

    def market_history(self, symbol: str, duration_seconds: int, length: int) -> dict[str, Any]:
        # 外盘三期（方案 A）：月份合约的分钟到小时线读 MT5 这份合约本身（回测与实际撮合同一份价格）；
        # 日线由小时线按 CME 交易日合成；MT5 不给的周期读快期主连。
        if str(symbol).upper().startswith("CME."):
            history = self._foreign.history(symbol, duration_seconds, length)
            if history is not None:
                return history
        return self._default().market_history(self._chart_symbol(symbol), duration_seconds, length)


class PaperAccountManager:
    """Assembles `paper:<id>` accounts on demand and provisions them per user."""

    def __init__(
        self, *, client: PaperEngineClient, registry: AccountRegistry, store: Any,
        services: Any, settings: Any, runtime_feed: Any,
        status_for_session: Callable[[str], dict[str, object]],
        foreign_client: PaperEngineClient | None = None,
    ) -> None:
        self.client = client
        # 外盘撮合引擎（外盘二期）：没配地址时没有美元子账户，其余行为不变。
        self.foreign_client = foreign_client
        self.foreign_poller = (PaperSnapshotPoller(foreign_client.base_url, foreign_client.token)
                               if foreign_client is not None else None)
        self.registry = registry
        self.store = store
        self.services = services
        self.settings = settings
        self.runtime_feed = runtime_feed
        self._status_for_session = status_for_session
        self._simulation_trading = os.getenv("AI_TRADER_SIMULATION_TRADING") == "1"
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, str] = {}
        self._unwire: dict[str, Callable[[], None]] = {}
        # Same lease discipline as the pool: the holder runs the ordering and
        # bookkeeping loops; a blue-green standby slot assembles read-only.
        self.may_run_loops: Callable[[], bool] = lambda: True
        # Idle reclamation (same heartbeat as the pool, `app.state.presence`): an
        # assembled account with nothing working for it and no granted user
        # online past `idle_seconds` is detached — its poller stops; the engine
        # keeps the book, the next request re-assembles it.
        self.presence: Any | None = None
        self.idle_seconds = idle_seconds_from_env()
        self._started = time.monotonic()
        self._scan_task: asyncio.Task[None] | None = None
        self._twin_tasks: set[asyncio.Task[None]] = set()
        self._stopped = False
        self.poller = PaperSnapshotPoller(client.base_url, client.token)
        # 行情账号接力 (2026-10-12): main.py attaches it once the shared market exists.
        self.market_sync: PaperMarketSourceSync | None = None

    # -- assembly -------------------------------------------------------------

    def _build_provider(self, record: dict[str, Any]) -> PaperProvider:
        if is_foreign_paper(record["id"]):
            if self.foreign_client is None or self.foreign_poller is None:
                raise PaperEngineError("paper_foreign_engine_not_configured")
            registry = self.registry
            return PaperProvider(
                account_id=str(record["id"]),
                base_url=f"{self.foreign_client.base_url}/accounts/{record['id']}",
                token=self.foreign_client.token,
                market_source=ForeignPaperMarket(self.services.foreign_market, lambda: registry.default.provider),
                poller=self.foreign_poller,
            )
        return PaperProvider(
            account_id=str(record["id"]),
            base_url=f"{self.client.base_url}/accounts/{record['id']}",
            token=self.client.token,
            market_source=self.registry.default.provider,
            poller=self.poller,
        )

    async def ensure(self, account_id: str, *, reason: str) -> AccountServices:
        """The account's services, assembling them first when needed. Safe to
        call concurrently: one assembler per account, everyone else waits; an
        assembly that fails or is cancelled leaves nothing registered behind."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            existing = self.registry.get(account_id)
            if existing is not None:
                return existing
            record = self.store.get_trading_account(account_id)
            if record is None or record["provider_kind"] != PAPER_KIND or record["status"] != "active":
                raise PaperEngineError("account_not_paper")
            self._states[account_id] = PAPER_STATE_CONNECTING
            try:
                services = build_account_services(
                    AccountSpec(
                        id=account_id, display_name=str(record["display_name"]),
                        provider_kind=PAPER_KIND,
                    ),
                    store=self.store, broker=self.services.broker,
                    policies=self.services.policies, instruments=self.services.instruments,
                    auth_mode=self.settings.auth_mode, simulation_gateway=None,
                    simulation_trading=self._simulation_trading,
                    injected=self._build_provider(record),
                    fee_settings=FeeSettings(Path(self.settings.state_root)),
                )
                self._unwire[account_id] = await assemble_account(
                    services, registry=self.registry, store=self.store,
                    proposals=self.services.proposals, runtime_feed=self.runtime_feed,
                    publish_status=lambda: self._publish_status(account_id),
                    run_loops=self.may_run_loops(),
                )
            finally:
                self._states.pop(account_id, None)
            log.info("paper_account start account=%s reason=%s", account_id, reason)
            await self._publish_status(account_id)
        self._assemble_twin(account_id)
        return services

    def twin_of(self, account_id: str) -> str | None:
        """同一个本站模拟账户的另一本账（两本账第三批，2026-10-22）：人民币 paper:x ↔ 已开好的美元 paperusd:x；
        没有另一本（快期账户、没开美元子账户）为 None。"""
        if is_foreign_paper(account_id):
            return PAPER_ACCOUNT_PREFIX + str(account_id).removeprefix(PAPER_FOREIGN_PREFIX)
        return self.foreign_account_of(account_id)

    def _assemble_twin(self, account_id: str) -> None:
        """两本账一起装配（两本账第三批，2026-10-22）：一本刚装配好，另一本在后台跟着装配。收盘前 / 风险度 / 保本 /
        交割提醒、收盘复盘推送与异动提醒都只看已装配的账户，美元账因此和人民币账一样被照看，不再只在恰好有人读它时才有。
        另一本装配不上只记日志，不拖住这一本。"""
        twin = self.twin_of(account_id)
        if twin is None or self.registry.get(twin) is not None or self._stopped:
            return

        async def assemble() -> None:
            try:
                await self.ensure(twin, reason="twin")
            except Exception:
                log.warning("paper_account_twin_failed account=%s", twin, exc_info=True)

        task = asyncio.create_task(assemble())
        self._twin_tasks.add(task)
        task.add_done_callback(self._twin_tasks.discard)

    # -- release --------------------------------------------------------------

    async def detach(self, account_id: str, *, reason: str) -> bool:
        """Dismantle the wiring for one paper account; the engine keeps its
        state, so a later request re-assembles onto the same balance."""
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            services = self.registry.get(account_id)
            if services is None:
                return False
            self.registry.remove(account_id)
            self._states.pop(account_id, None)
            await dismantle_account(services, self._unwire.pop(account_id, lambda: None))
            log.info("paper_account detach account=%s reason=%s", account_id, reason)
            await self._publish_status(account_id)
            return True

    async def release(self, account_id: str, *, reason: str) -> bool:
        """Detach the wiring for an account that goes away (a disabled user).
        The engine-side state is not deleted: history stays for audit."""
        return await self.detach(account_id, reason=reason)

    def account_state(self, account_id: str) -> str:
        """connecting while assembly is in flight, connected once assembled,
        offline otherwise."""
        state = self._states.get(account_id)
        if state is not None:
            return state
        if self.registry.get(account_id) is not None:
            return PAPER_STATE_CONNECTED
        return PAPER_STATE_OFFLINE

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

    # -- provisioning -----------------------------------------------------------

    async def ensure_paper_account(self, user_id: str, *, make_default: bool = True) -> dict[str, Any]:
        """One paper account per user, created idempotently: the engine account
        (initial balance 1,000,000), the store row with this user as owner, and
        the default-account switch when they hold none yet. A user who already
        has one keeps it — re-registration and the 403 backfill both land here.

        The account id is claimed in the store first (one row per user, atomic
        across connections and processes); every concurrent or retried call —
        another manager, an engine timeout, a crash before the row was written —
        resumes that same id. The engine's create is idempotent per id."""
        existing = self.store.find_user_paper_account(user_id=user_id)
        if existing is not None:
            await self.ensure_foreign_account(user_id, str(existing["id"]))
            return existing
        account_id = self.store.claim_paper_account(
            user_id=user_id, account_id=f"{PAPER_ACCOUNT_PREFIX}{uuid4().hex[:12]}")
        await self.client.create_account(account_id, PAPER_INITIAL_BALANCE)
        record = self.store.ensure_trading_account(
            account_id, execution_owner_user_id=user_id, provider_kind=PAPER_KIND,
            display_name=PAPER_DISPLAY_NAME, sidecar_url=None, sidecar_token_file=None,
            grants=((user_id, "owner"),),
        )
        await self.ensure_foreign_account(user_id, account_id)
        # Registration / the 403 backfill / a 快期 login make it the default only
        # for a user who had nothing to trade before; opening it from settings
        # (make_default=False) never moves anyone off the account they use.
        if make_default and self.store.get_user_default_account(user_id=user_id) is None:
            self.store.set_user_default_account(user_id=user_id, account_id=account_id)
        log.info("paper_account provisioned account=%s user=%s", account_id, user_id)
        return record

    async def ensure_foreign_account(self, user_id: str, account_id: str) -> dict[str, Any] | None:
        """人民币本站模拟账户 ``account_id`` 的美元子账户（外盘二期）：外盘引擎开户（初始 10 万美元，同号幂等）
        → 账户行（paper 类、本人 owner）。没配外盘引擎时什么也不做；外盘引擎暂时不通只记日志——人民币
        账户照常，下次登录、补开或重启时再开。不改任何人的默认账户。"""
        if self.foreign_client is None:
            return None
        foreign_id = foreign_paper_id(account_id)
        record = self.store.get_trading_account(foreign_id)
        if record is not None and record["status"] == "active":
            return record
        try:
            await self.foreign_client.create_account(foreign_id, PAPER_FOREIGN_INITIAL_BALANCE)
        except PaperEngineError as error:
            log.warning("paper_foreign_account_deferred account=%s reason=%s", foreign_id, error)
            return None
        record = self.store.ensure_trading_account(
            foreign_id, execution_owner_user_id=user_id, provider_kind=PAPER_KIND,
            display_name=PAPER_FOREIGN_DISPLAY_NAME, sidecar_url=None, sidecar_token_file=None,
            grants=((user_id, "owner"),),
        )
        log.info("paper_foreign_account provisioned account=%s user=%s", foreign_id, user_id)
        return record

    def foreign_account_of(self, account_id: str) -> str | None:
        """一个人民币本站模拟账户已开好的美元子账户编号；没有（没配外盘引擎、还没开成）为 None。"""
        if self.foreign_client is None or is_foreign_paper(account_id):
            return None
        foreign_id = foreign_paper_id(account_id)
        record = self.store.get_trading_account(foreign_id)
        return foreign_id if record is not None and record["status"] == "active" else None

    async def backfill_foreign_accounts(self) -> int:
        """老用户补开美元子账户（启动时一次）：每个已开好的人民币本站模拟账户都补一个。"""
        if self.foreign_client is None:
            return 0
        opened = 0
        for user_id, account_id in self.store.list_paper_account_claims():
            if self.store.get_trading_account(account_id) is None:
                continue  # 认领了还没开成的账户：等它自己的开户流程
            if self.foreign_account_of(account_id) is not None:
                continue
            if await self.ensure_foreign_account(user_id, account_id) is None:
                break  # 外盘引擎此刻不通：不逐个等超时，下一次闲置扫描再补
            opened += 1
        return opened

    # -- lifecycle ---------------------------------------------------------------

    def _idle(self, account_id: str, busy: set[str]) -> bool:
        if account_id in busy or self.presence is None:
            return False
        grantees = self.store.list_account_grantees(account_id=account_id)
        if not grantees:
            return False
        now = time.monotonic()
        for user in grantees:
            idle = self.presence.idle_seconds(user, now=now)
            if idle is None:
                idle = now - self._started
            if idle < self.idle_seconds:
                return False
        return True

    async def scan_idle(self) -> list[str]:
        """Detach assembled paper accounts that nobody is using; returns their ids.
        An account whose users are away but which still counts as busy asks
        the engine about its resting orders first, so an order that finished
        unwatched does not keep it assembled forever."""
        assembled = [
            services for services in self.registry.all()
            if (record := self.store.get_trading_account(services.account_id)) is not None
            and record["provider_kind"] == PAPER_KIND
        ]
        busy = set(self.store.list_busy_paper_accounts())
        for services in assembled:
            if services.account_id in busy and self._idle(services.account_id, set()):
                await services.simulation_orders.refresh_resting()
        busy = set(self.store.list_busy_paper_accounts())

        def idle(account_id: str) -> bool:
            # 两本账一起卸载：另一本还装配着且有事在做（策略、止损、挂单），这一本也留着。
            twin = self.twin_of(account_id)
            return self._idle(account_id, busy) and (
                twin is None or self.registry.get(twin) is None or self._idle(twin, busy))

        detached: list[str] = []
        for services in assembled:
            if idle(services.account_id) and await self.detach(services.account_id, reason="idle"):
                detached.append(services.account_id)
        return detached

    async def _scan_loop(self) -> None:
        while not self._stopped:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            if self._stopped:
                return
            try:
                await self.scan_idle()
            except Exception:
                log.exception("paper_account_idle_scan_failed")
            try:
                # 启动时外盘引擎还没起来（同一次发布里 Runtime 先切）：补开美元子账户在这里继续重试。
                await self.backfill_foreign_accounts()
            except Exception:
                log.exception("paper_foreign_backfill_failed")

    async def start_background(self) -> None:
        """Boot-time assembly of the busy paper accounts — monitoring strategies,
        armed stops, in-flight order links. Their loops must not wait for the
        owner's first request; idle accounts assemble on demand."""
        for account_id in self.store.list_busy_paper_accounts():
            try:
                await self.ensure(account_id, reason="boot")
            except Exception:
                log.exception("paper_account_boot_failed account=%s", account_id)
        try:
            opened = await self.backfill_foreign_accounts()
            if opened:
                log.info("paper_foreign_backfill opened=%s", opened)
        except Exception:
            log.exception("paper_foreign_backfill_failed")
        self._stopped = False
        if self._scan_task is None:
            self._scan_task = asyncio.create_task(self._scan_loop())
        if self.market_sync is not None:
            self.market_sync.start()

    async def shutdown(self) -> None:
        """Runtime is going down: detach this process's wiring for every paper
        account. The engine-side state stays; the next boot or request
        re-assembles onto it."""
        self._stopped = True
        for task in list(self._twin_tasks):
            task.cancel()
        await asyncio.gather(*self._twin_tasks, return_exceptions=True)
        if self._scan_task is not None:
            self._scan_task.cancel()
            await asyncio.gather(self._scan_task, return_exceptions=True)
            self._scan_task = None
        if self.market_sync is not None:
            await self.market_sync.stop()
        for services in list(self.registry.all()):
            if (record := self.store.get_trading_account(services.account_id)) is not None \
                    and record["provider_kind"] == PAPER_KIND:
                await self.detach(services.account_id, reason="shutdown")
