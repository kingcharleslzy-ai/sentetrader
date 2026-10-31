"""Trading-account specs, per-account services and request account resolution.

A deployment without `AI_TRADER_ACCOUNTS_FILE` runs exactly one account — the
default — with the same provider, orders, autonomy, monitor and ledger wiring
4.1 had. A file adds accounts: each gets its own service set, assembled by
`build_services` into one `AccountRegistry`, and each request resolves to the
account it acts on via the `X-AI-Trader-Account` header or its session.
"""
from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from ..account_ledger import AccountLedger
from ..autonomy import AutonomousSimulationService
from ..data_providers import (
    FakeReadOnlyDataProvider,
    ReadOnlyDataProvider,
    SidecarReadOnlyDataProvider,
    TqKqReadOnlyProvider,
)
from ..instruments import account_currency
from ..runtime_presentation import describe_runtime
from ..secret_values import MAX_SECRET_BYTES, secret_value
from ..simulation_orders import SimulationOrderService
from ..store.base import DEFAULT_TRADING_ACCOUNT_ID
from ..store.trading_accounts import scoped_owner_user_id
from ..strategy_monitor import StrategyMonitorService
from .config import RuntimeSettings

ACCOUNT_HEADER = "X-AI-Trader-Account"
ACCOUNTS_FILE_ENV = "AI_TRADER_ACCOUNTS_FILE"
PROVIDER_KINDS = ("fake", "tqkq-readonly", "tqkq-sidecar")
DEFAULT_SYMBOLS = "SHFE.au2612,SHFE.rb2610"


@dataclass(frozen=True)
class AccountSpec:
    """One account from the accounts file (or the synthesized default account)."""

    id: str
    display_name: str
    provider_kind: str
    sidecar_url: str | None = None
    sidecar_token_file: str | None = None
    symbols: tuple[str, ...] | None = None
    execution_owner_user_id: str | None = None
    # 4.4a: a one-time full rename of a previous account id onto this entry's id
    # (the fingerprint rename off `tqkq-sim:default`), and the declared grant set
    # the account is reconciled to on every boot (owner plus exactly these).
    rename_from: str | None = None
    grants: tuple[tuple[str, str], ...] | None = None
    # M1a: pool-assembled accounts go away when everyone is offline; a spec
    # flagged here (and the default account) are never idle-released.
    always_on: bool = False
    # M1a pool accounts: the token of the one child process this assembly is
    # bound to (per spawn, memory only). Any other process on that port refuses it.
    sidecar_token: str | None = field(default=None, repr=False, compare=False)


def _default_spec(settings: RuntimeSettings, *, execution_owner_user_id: str) -> AccountSpec:
    """The account every deployment without an accounts file runs: the exact
    4.1 wiring — same display name, sidecar URL and symbol sources."""
    symbols = None
    if settings.data_mode.startswith("tqkq"):
        symbols = tuple(filter(None, os.getenv("AI_TRADER_TQKQ_SYMBOLS", DEFAULT_SYMBOLS).split(",")))
    return AccountSpec(
        id=DEFAULT_TRADING_ACCOUNT_ID,
        display_name="TqKq 模拟账户" if settings.data_mode.startswith("tqkq") else "假数据模拟账户",
        provider_kind=settings.data_mode,
        sidecar_url=os.getenv("AI_TRADER_READONLY_SIDECAR_URL"),
        symbols=symbols,
        execution_owner_user_id=execution_owner_user_id,
    )


def _grants_from_entry(entry: Any, execution_owner_user_id: str) -> tuple[tuple[str, str], ...] | None:
    """Declared grants: `{"operator": [username…], "viewer": [username…]} → the
    account's grant table is reconciled to exactly the owner plus these. A user
    may not appear under both roles, and the execution owner is always `owner` —
    declaring either is a file error, not something to silently rewrite."""
    grants = entry.get("grants")
    if grants is None:
        return None
    if not isinstance(grants, dict) or set(grants) - {"operator", "viewer"}:
        raise RuntimeError("accounts_file_invalid")
    declared: dict[str, str] = {}
    for role in ("operator", "viewer"):
        usernames = grants.get(role)
        if usernames is None:
            continue
        if not isinstance(usernames, list) or not usernames:
            raise RuntimeError("accounts_file_invalid")
        for username in usernames:
            if not isinstance(username, str) or not username.strip():
                raise RuntimeError("accounts_file_invalid")
            user_id = scoped_owner_user_id(username.strip())
            if user_id in declared and declared[user_id] != role:
                raise RuntimeError("accounts_file_invalid")
            declared[user_id] = role
    if execution_owner_user_id in declared:
        raise RuntimeError("accounts_file_invalid")
    return tuple(declared.items())


def _spec_from_entry(entry: Any, settings: RuntimeSettings, *, default_owner_user_id: str) -> AccountSpec:
    if not isinstance(entry, dict):
        raise RuntimeError("accounts_file_invalid")
    account_id = entry.get("id")
    if not isinstance(account_id, str) or not account_id.strip() or len(account_id.strip()) > 120:
        raise RuntimeError("accounts_file_invalid")
    provider_kind = entry.get("dataMode") or settings.data_mode
    if provider_kind not in PROVIDER_KINDS:
        raise RuntimeError("accounts_file_invalid")
    rename_from = entry.get("renameFrom")
    if rename_from is not None and (
        not isinstance(rename_from, str) or not rename_from.strip()
        or len(rename_from.strip()) > 120 or rename_from.strip() == account_id.strip()
    ):
        raise RuntimeError("accounts_file_invalid")
    rename_from = rename_from.strip() if isinstance(rename_from, str) else None
    sidecar_url = entry.get("sidecarUrl")
    if sidecar_url is None and (
        account_id == DEFAULT_TRADING_ACCOUNT_ID or rename_from == DEFAULT_TRADING_ACCOUNT_ID
    ):
        # The default account keeps the env-provided URL of a 4.1 deployment —
        # also once it has been renamed onto a fingerprint id.
        sidecar_url = os.getenv("AI_TRADER_READONLY_SIDECAR_URL")
    if provider_kind == "tqkq-sidecar" and (not isinstance(sidecar_url, str) or not sidecar_url):
        raise RuntimeError("accounts_file_requires_sidecar")
    symbols = entry.get("symbols")
    if symbols is not None:
        if not isinstance(symbols, list) or not all(isinstance(item, str) and item for item in symbols):
            raise RuntimeError("accounts_file_invalid")
        symbols = tuple(symbols)
    token_file = entry.get("sidecarTokenFile")
    if token_file is not None and (not isinstance(token_file, str) or not token_file):
        raise RuntimeError("accounts_file_invalid")
    owner = entry.get("executionOwnerUsername")
    if owner is not None and (not isinstance(owner, str) or not owner.strip()):
        raise RuntimeError("accounts_file_invalid")
    display_name = entry.get("displayName")
    if display_name is not None and not isinstance(display_name, str):
        raise RuntimeError("accounts_file_invalid")
    execution_owner_user_id = (
        scoped_owner_user_id(owner.strip()) if owner else default_owner_user_id
    )
    return AccountSpec(
        id=account_id.strip(),
        display_name=(display_name or account_id).strip(),
        provider_kind=provider_kind,
        sidecar_url=sidecar_url if isinstance(sidecar_url, str) else None,
        sidecar_token_file=token_file,
        symbols=symbols,
        execution_owner_user_id=execution_owner_user_id,
        rename_from=rename_from,
        grants=_grants_from_entry(entry, execution_owner_user_id),
    )


def load_account_specs(
    settings: RuntimeSettings, *, execution_owner_user_id: str, replay_provider: Any | None = None,
) -> list[AccountSpec]:
    """The account set for this boot. Replay children always run the single
    default account; so does every deployment without an accounts file."""
    path = os.getenv(ACCOUNTS_FILE_ENV)
    if replay_provider is not None or not path:
        return [_default_spec(settings, execution_owner_user_id=execution_owner_user_id)]
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        raise RuntimeError("accounts_file_invalid") from None
    entries = document.get("accounts") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("accounts_file_invalid")
    if not entries:
        raise RuntimeError("accounts_file_empty")
    specs = [_spec_from_entry(entry, settings, default_owner_user_id=execution_owner_user_id)
             for entry in entries]
    ids = [spec.id for spec in specs]
    if len(set(ids)) != len(ids):
        raise RuntimeError("accounts_file_duplicate_id")
    renames = [spec.rename_from for spec in specs if spec.rename_from]
    if renames and (set(renames) & set(ids) or len(set(renames)) != len(renames)):
        # Renaming an id another entry still runs, or two entries renaming the
        # same old id, would leave the rename target ambiguous.
        raise RuntimeError("accounts_file_invalid")
    return specs


def _token_from_file(path: str) -> str:
    """Read a per-account sidecar token file under the `secret_value` rules."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise RuntimeError("accounts_file_invalid")
    try:
        payload = candidate.read_bytes()
    except OSError as error:
        raise RuntimeError("accounts_file_invalid") from error
    if len(payload) > MAX_SECRET_BYTES:
        raise RuntimeError("accounts_file_invalid")
    try:
        return payload.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as error:
        raise RuntimeError("accounts_file_invalid") from error


def account_sidecar_token(spec: AccountSpec) -> str:
    """The sidecar token: the account's own token file, else the shared env
    secret — the same `AI_TRADER_READONLY_SIDECAR_TOKEN` source 4.1 used. A pool
    account carries its child's own per-spawn token instead."""
    if spec.sidecar_token:
        return spec.sidecar_token
    if spec.sidecar_token_file:
        token = _token_from_file(spec.sidecar_token_file)
        if token:
            return token
    return secret_value("AI_TRADER_READONLY_SIDECAR_TOKEN", required=True)


def build_account_provider(
    spec: AccountSpec, *, injected: ReadOnlyDataProvider | None,
) -> ReadOnlyDataProvider:
    """The account's data provider: an injected instance wins (tests and replay
    children), then the provider kind constructs it exactly as 4.1 did."""
    if injected is not None:
        return injected
    if spec.provider_kind == "tqkq-readonly":
        return TqKqReadOnlyProvider(username=None, password=None, symbols=spec.symbols or ())
    if spec.provider_kind == "tqkq-sidecar":
        if not spec.sidecar_url:
            raise KeyError("AI_TRADER_READONLY_SIDECAR_URL")
        return SidecarReadOnlyDataProvider(
            base_url=spec.sidecar_url,
            token=account_sidecar_token(spec),
            symbols=spec.symbols or (),
        )
    if spec.provider_kind == "fake":
        return FakeReadOnlyDataProvider()
    raise RuntimeError("accounts_file_invalid")


@dataclass
class AccountServices:
    """One trading account's service set. The monitor and ledger loops are
    attached after the registry and proposals exist, so they stay optional;
    the order and autonomy services are assembled with the account itself."""

    account_id: str
    provider: ReadOnlyDataProvider
    simulation_orders: SimulationOrderService
    autonomy: AutonomousSimulationService
    spec: AccountSpec | None = None
    strategy_monitor: StrategyMonitorService | None = None
    account_ledger: AccountLedger | None = None
    # Restart recovery ran for this assembly (`recover_interrupted_execution`).
    recovered: bool = False


class AccountNotAssembled(KeyError):
    """The account a session/proposal/strategy row is bound to is not in this
    boot's registry — removed from the accounts file and disabled. Nothing on
    it may fall back to the default account; callers must refuse instead."""

    def __init__(self, account_id: str) -> None:
        super().__init__(account_id)
        self.account_id = account_id


class AccountAssemblyRequired(KeyError):
    """A granted on-demand account (`tqkq-pool` or `paper`) that is not
    assembled yet. Unlike a removed account this one is reachable: the caller
    assembles it via the pool or paper manager and resolves again (M1a)."""

    def __init__(self, account_id: str) -> None:
        super().__init__(account_id)
        self.account_id = account_id


class AccountRegistry:
    """The accounts assembled for this boot: the default first, the rest by id."""

    def __init__(self, default: AccountServices, others: dict[str, AccountServices]) -> None:
        self._default = default
        self._others = dict(sorted(others.items()))
        # 外盘二期：用户 → 已装配好的美元子账户编号（paper_accounts 配了外盘引擎时由 main 挂上）；外盘单据此转过去。
        self.foreign_account: Callable[[str], Awaitable[str | None]] | None = None
        # 人民币本站模拟账户 → 它已开好的美元子账户编号（不装配，只读编号；同样由 main 挂上）。
        self.foreign_account_of: Callable[[str], str | None] | None = None

    @property
    def default(self) -> AccountServices:
        return self._default

    def get(self, account_id: str) -> AccountServices | None:
        if account_id == self._default.account_id:
            return self._default
        return self._others.get(account_id)

    def require(self, account_id: str) -> AccountServices:
        services = self.get(account_id)
        if services is None:
            raise AccountNotAssembled(account_id)
        return services

    def all(self) -> list[AccountServices]:
        return [self._default, *self._others.values()]

    def ids(self) -> list[str]:
        return [services.account_id for services in self.all()]

    def add(self, services: AccountServices) -> None:
        """M1a: an on-demand account enters the registry once its child is up.
        The default account is static and can never be re-added."""
        if services.account_id == self._default.account_id:
            raise ValueError("default_account_is_static")
        self._others[services.account_id] = services

    def remove(self, account_id: str) -> bool:
        """M1a: an on-demand account leaves the registry (idle/unbind/shutdown).
        The default account can never be removed."""
        if account_id == self._default.account_id:
            return False
        return self._others.pop(account_id, None) is not None


@dataclass(frozen=True)
class AccountContext:
    """The account a request acts on, resolved from the header, the session or
    the user's grants, with the store row and the caller's role attached."""

    account: AccountServices
    record: dict[str, Any]
    role: str
    user_id: str
    # 4.3: the account's single execution owner — the only one whose AI may act
    # on its own behalf (execute, lease, stops, strategy submissions).
    execution_owner: bool
    # M1a: where a pool-managed account's child is (connecting / queued /
    # connected / offline); None for non-pool accounts.
    pool_status: str | None = None


def resolve_account(
    registry: AccountRegistry, store: Any, *, request: Request | None, user_id: str,
    session_account_id: str | None = None, account_id: str | None = None,
    idp_fallback: bool = True,
) -> AccountContext:
    """① an explicit target (`account_id`, e.g. the account a stored row belongs
    to), ② the `X-AI-Trader-Account` header, ③ the session's account, ④ the
    user's chosen default, ⑤ the user's grants — their own paper account, else
    the default account when granted, else the first grant by id. A target
    naming an account the user holds no grant on — including one this boot no longer assembles — is refused
    outright; it must never fall back to the default account, which would act on
    the wrong account. A user holding no grants at all (the IdP fallback) keeps
    the default account — unless `idp_fallback` is off (M1b: an unbound role=user
    login owns no trading account and is refused, never parked on the owner's).
    A granted pool-managed account that is merely not assembled yet raises
    `AccountAssemblyRequired` instead of refusing."""
    grants = {grant["account_id"]: grant["role"] for grant in store.list_account_grants(user_id=user_id)}
    header = request.headers.get(ACCOUNT_HEADER, "").strip() if request is not None else ""
    target = account_id or header

    def granted(candidate: str) -> bool:
        # One source of truth (`store.account_role`): a grant row, or the IdP
        # fallback that keeps a user without any grant on the default account —
        # a fallback M1b turns off for unbound role=user logins.
        return candidate in grants or (
            idp_fallback and not grants and candidate == registry.default.account_id
        )

    if target:
        selected = target if granted(target) else None
    elif session_account_id:
        # A session bound to an ungranted account, or to an account this boot
        # no longer assembles, resolves to nothing — refusing below.
        selected = session_account_id if granted(session_account_id) else None
    else:
        preferred = store.get_user_default_account(user_id=user_id)
        if preferred is not None and preferred in grants:
            selected = preferred  # the account the user (or their login) made default
        elif not idp_fallback and not grants:
            raise HTTPException(status_code=403, detail="tqkq_account_required")
        elif not grants or registry.default.account_id in grants:
            # The user's own 本站模拟 comes before the static main account (快期模拟停用交易，#1205).
            paper = store.find_user_paper_account(user_id=user_id) if grants else None
            selected = str(paper["id"]) if paper is not None else registry.default.account_id
        else:
            # The implicit default picks only among grants this boot can actually serve
            # (526-B1): an assembled account, or an active pool/paper record the request
            # itself may assemble. A grant left on an account removed from the accounts
            # file must not turn every request into a 403 while a healthy grant exists.
            def selectable(candidate: str) -> bool:
                if registry.get(candidate) is not None:
                    return True
                row = store.get_trading_account(candidate)
                return (row is not None and row["provider_kind"] in ("tqkq-pool", "paper")
                        and row["status"] == "active")
            usable = [grant for grant in grants if selectable(grant)]
            selected = min(usable) if usable else min(grants)
    services = registry.get(selected) if selected else None
    if services is None:
        record = store.get_trading_account(selected) if selected else None
        if (selected is not None and record is not None
                and record["provider_kind"] in ("tqkq-pool", "paper") and record["status"] == "active"):
            # Granted and active, only its services are not up: the request
            # itself is the trigger to assemble them (RuntimeContext.
            # account_context — the pool starts the sidecar child, the paper
            # manager wires the engine-backed provider).
            raise AccountAssemblyRequired(selected)
        # Unknown target, an account the user holds no grant on, or an account
        # this boot no longer assembles: nothing about it may be read or written.
        raise HTTPException(status_code=403, detail="account_not_granted")
    record = store.get_trading_account(selected)
    if record is None:
        raise HTTPException(status_code=403, detail="account_not_granted")
    # The role comes from the store alone — routes and services must not
    # re-query grants to decide what a caller may write. The one exception is
    # this resolver's own IdP fallback: a single-tenant user (local token,
    # cloud-jwt shared account, an owner without rows) selected the default
    # account without a row, and runs on it as operator exactly as before.
    role = grants.get(selected)
    if role is None and idp_fallback and not grants and selected == registry.default.account_id:
        role = "operator"
    if role is None:  # unreachable while granted() holds; refusing is the safe side
        raise HTTPException(status_code=403, detail="account_not_granted")
    return AccountContext(
        account=services, record=record,
        role=role,
        user_id=user_id,
        execution_owner=store.is_execution_owner(account_id=selected, user_id=user_id),
    )


def idp_fallback_for_auth(auth: Any) -> bool:
    """Whether a user with no grant rows anywhere may run on the shared default
    account without a request. Yes for the single-tenant modes (local token and
    the cloud-jwt shared fake account); no for owner-password, where the owner
    holds an explicit owner row and a member without grants owns no account at
    all — the request-less callers (feed, tool bridge) must refuse, never park
    them on the owner's account."""
    from ..auth import OwnerAuth
    return not isinstance(auth, OwnerAuth)


def effective_account_role(
    store: Any, *, user_id: str, account_id: str, idp_fallback: bool | None = None,
) -> str | None:
    """The role a caller runs with on one account: their grant row, or the
    single-tenant IdP fallback (operator on the shared default account). This is
    the same verdict `resolve_account` hands a request and `user_may_use_account`
    a request-less caller, stated for the surfaces that only describe the account
    — the runtime.status account block and the accounts directory. `idp_fallback`
    defaults to the deployment fact create_app records on the store; a caller
    holding a request may pass its own verdict (the owner-password login-role
    refinement in RuntimeContext)."""
    role = store.account_role(account_id=account_id, user_id=user_id)
    if role is not None:
        return role
    allowed = getattr(store, "idp_fallback", False) if idp_fallback is None else idp_fallback
    if allowed and not store.list_account_grants(user_id=user_id) \
            and account_id == store.default_trading_account_id:
        return "operator"
    return None


def user_may_use_account(store: Any, *, user_id: str, account_id: str, idp_fallback: bool) -> bool:
    """Whether a user may act on one account outside a request: a grant row, or
    the IdP fallback of the single-tenant modes. The request path resolves this
    through `resolve_account`; this is the same verdict for callers that only
    hold a session row (the feed, the app_call tool bridge)."""
    return effective_account_role(
        store, user_id=user_id, account_id=account_id, idp_fallback=idp_fallback) is not None


def require_account_write(context: AccountContext) -> None:
    """owner 与 operator 可写账户；viewer（只读）的一切账户写操作拒绝。"""
    if context.role == "viewer":
        raise HTTPException(status_code=403, detail="account_read_only")


def require_execution_owner(context: AccountContext) -> None:
    """一账户一执行负责人：AI 自主动作的显式入口（启动策略监控等）只属于负责人。"""
    if not context.execution_owner:
        raise HTTPException(status_code=403, detail="not_execution_owner")


def account_status_payload(
    *, account_id: str, record: dict[str, Any] | None, role: str | None, execution_owner: bool,
    pool_status: str | None = None,
) -> dict[str, Any]:
    """The account block every status snapshot and the account directory share:
    which account, how it is shown, the caller's role and the execution fence.
    `pool_status` (M1a) describes an on-demand account's child — connecting /
    queued / connected / offline — as `poolStatus` in the directory; static
    accounts carry no such field."""
    payload = {
        "id": account_id,
        "displayName": record["display_name"] if record is not None else account_id,
        "role": role,
        "environment": record["environment"] if record is not None else "simulation",
        "executionOwner": execution_owner,
        "currency": account_currency(account_id),
    }
    if pool_status is not None:
        payload["poolStatus"] = pool_status
    return payload


def context_account_status_payload(context: AccountContext) -> dict[str, Any]:
    payload = account_status_payload(
        account_id=context.account.account_id, record=context.record,
        role=context.role, execution_owner=context.execution_owner,
        pool_status=context.pool_status,
    )
    if context.pool_status is not None:
        payload["status"] = context.pool_status
    return payload


def merge_paper_account_status(
    status: dict[str, Any], *, store: Any, accounts: Any, account_id: str,
) -> dict[str, Any]:
    """本站模拟会话的账户口径 (2026-10-03)：账户模式、账户状态、网关与风险读该账户
    自己的引擎 provider（撮合走引擎）；行情与其余字段维持部署默认 provider 的口径
    （行情与默认账户同源）。合并后重新组装说明字段（accountLabel / capabilitySummary），
    否则它们仍写部署默认账户（2026-10-08：只有本站模拟的新账号显示“快期模拟账户”）。
    还没装配（按需装配前的一刻）时如实写“连接中”。非 paper 账户原样返回。
    每条会话 runtime.status 与 GET /api/runtime/status 共用这一处。"""
    record = store.get_trading_account(account_id)
    if record is None or record["provider_kind"] != "paper":
        return status
    services_for_account = accounts.get(account_id) if accounts is not None else None
    paper_status = (services_for_account.provider.runtime_status() if services_for_account is not None
                    else {"accountStatus": "connecting", "tradingGatewayStatus": "unavailable"})
    # 外盘仓合并显示 (2026-10-21)：人民币本站模拟账户写明它的美元子账户编号，客户端据此把外盘的持仓与委托
    # 并进同一个持仓面板（各行仍属美元子账户、按美元记）。编号只认已开好的子账户（foreign_account_of 一处）。
    account_block = status.get("account")
    foreign_of = getattr(accounts, "foreign_account_of", None)
    foreign_id = foreign_of(account_id) if foreign_of is not None else None
    if foreign_id and isinstance(account_block, dict) and account_block.get("id") == account_id:
        status = {**status, "account": {**account_block, "foreignAccountId": foreign_id}}
    # Its own order gateway too: the deployment default (快期) has none since #1205.
    simulated = (services_for_account.simulation_orders.enabled
                 and services_for_account.autonomy.supports_mode(str(status.get("mode") or "local"))
                 if services_for_account is not None else status.get("simulationTrading"))
    return describe_runtime({
        **status,
        "simulationTrading": simulated,
        "accountMode": paper_status.get("accountMode", "paper"),
        "accountStatus": paper_status.get("accountStatus", "unavailable"),
        "tradingGatewayStatus": paper_status.get("tradingGatewayStatus", "unavailable"),
        "riskStatus": paper_status.get("riskStatus", status.get("riskStatus")),
    })


def session_account_status_payload(
    store: Any, *, account_id: str, user_id: str, pool_status: str | None = None,
) -> dict[str, Any] | None:
    """The account block for a session-bound status snapshot; `None` when the
    account row is gone entirely (nothing to describe). A pool-managed account
    also states where its child is (`status`: connecting / queued / connected /
    offline) — the runtime.status field the product contract names. The role is
    the effective verdict (grant row or single-tenant IdP fallback) so the block
    a session's user sees states the account they really run on, never a null
    role the client contract rejects."""
    record = store.get_trading_account(account_id)
    if record is None:
        return None
    if pool_status is None and record["provider_kind"] in ("tqkq-pool", "paper"):
        pool_status = "offline"  # no on-demand manager attached: nothing can be up
    payload = account_status_payload(
        account_id=account_id, record=record,
        role=effective_account_role(store, account_id=account_id, user_id=user_id),
        execution_owner=store.is_execution_owner(account_id=account_id, user_id=user_id),
        pool_status=pool_status,
    )
    if pool_status is not None:
        payload["status"] = pool_status
    return payload
