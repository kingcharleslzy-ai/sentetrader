"""Everything a route module needs, assembled once by create_app."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from ..account_pool import AccountPoolManager, PoolCapacityFull, PoolError, PoolStartRefused
from ..auth import OwnerAuth
from ..instance_lease import InstanceLease
from ..instruments import foreign_product_of, foreign_tradable
from ..lifecycle import SessionLifecycleManager
from ..paper_accounts import PaperAccountManager, PaperEngineError
from ..proposals import ProposalConflict, RiskViolation
from ..runtime_feed import RuntimeFeed
from ..tenant_adapters import SessionAgentRouter
from .accounts import (
    AccountAssemblyRequired,
    AccountContext,
    merge_paper_account_status,
    resolve_account,
    session_account_status_payload,
)
from .agent import AgentWiring
from .auth import AuthService
from .config import RuntimeSettings
from .services import RuntimeServices


@dataclass
class RuntimeContext:
    settings: RuntimeSettings
    services: RuntimeServices
    agent: AgentWiring
    auth: AuthService
    lifecycle: SessionLifecycleManager
    current_user: Callable[[Request], Awaitable[str]]
    runtime_instance_id: str | None
    agent_credential_configured: bool
    native_transport: Any | None
    replay_provider: Any | None
    # Filled in by create_app after construction: the feed needs the app, the app needs the lifespan.
    runtime_feed: RuntimeFeed = None  # type: ignore[assignment]
    # Replay children share the parent's state database and never run the loops.
    instance_lease: InstanceLease | None = None
    application: FastAPI = None  # type: ignore[assignment]
    replay_router: Any | None = None
    create_replay_child: Callable[..., FastAPI] | None = None
    # M1a: the on-demand pool manager; None when no pool is configured, in
    # which case every account behaves exactly as before this feature.
    account_pool: AccountPoolManager | None = None
    # 本站模拟账户 (2026-10-03): the paper-engine-backed account manager; None
    # when no engine URL is configured — then no paper account is provisioned
    # or assembled and every user keeps the behaviour they had.
    paper_accounts: PaperAccountManager | None = None

    def _idp_fallback_allowed(self, request: Request) -> bool:
        """M1b: the no-grants fallback onto the owner's default account belongs
        to the owner and to single-machine local-token mode. A role=user login
        holding no grants (unbound, or never bound) owns no trading account;
        every account request of theirs is refused instead."""
        if getattr(self.services.store, "idp_fallback", False):
            # Single-tenant by construction (local token, cloud-jwt, a replay app that
            # serves only the user who owns it): the deployment verdict decides.
            return True
        if isinstance(self.auth, OwnerAuth):
            try:
                return self.auth.validate(self.auth.session_token(request)).role == "owner"
            except HTTPException:
                return False
        return True

    async def account_for_symbol(
        self, request: Request, user_id: str, symbol: str | None, *, session_id: str | None = None,
    ) -> AccountContext:
        """外盘三期：一个按合约行事的请求（策略上线、回测、模板）落在哪个账户。外盘合约（CME 月份或快期外盘主连）
        只在用户的美元子账户撮合与记账，请求原本落在别的账户时转过去（与外盘出卡同一规则）；其余合约照旧
        `account_context`。美元子账户此刻装配不上时 503，不在人民币账户上将错就错。"""
        product = foreign_product_of(symbol or "")
        if product is None or not foreign_tradable(product):
            return await self.account_context(request, user_id, session_id=session_id)
        # 外盘合约直接找美元子账户，不先装配请求原本的账户（它装配不上也不该连累外盘）。
        resolver = getattr(self.services.accounts, "foreign_account", None)
        foreign_id = await resolver(user_id) if resolver is not None else None
        if foreign_id is None:
            raise HTTPException(status_code=503, detail="foreign_account_unavailable")
        return await self.account_context(request, user_id, account_id=foreign_id)

    async def account_context(
        self, request: Request, user_id: str, *, session_id: str | None = None,
        account_id: str | None = None,
    ) -> AccountContext:
        """The account this request acts on: an explicit target (`account_id`,
        e.g. the account a stored row belongs to), the header, the session's
        account, then the user's grants (see `resolve_account`). A granted
        pool account that is not assembled yet is assembled here first: the
        request itself is the user showing up. An assembled pool account whose
        child has been stopped from under this process (a blue-green sibling
        slot's exit) is dropped and re-assembled here too — one lost child
        must not become a standing 503."""
        session_account = None
        if session_id:
            session = self.services.broker.get_session(session_id, user_id)
            session_account = session.trading_account_id if session is not None else None
        idp_fallback = self._idp_fallback_allowed(request)

        def resolve() -> AccountContext:
            return self._with_pool_status(resolve_account(
                self.services.accounts, self.services.store,
                request=request, user_id=user_id, session_account_id=session_account,
                account_id=account_id, idp_fallback=idp_fallback,
            ))

        async def assemble(target: str, *, reason: str) -> None:
            record = self.services.store.get_trading_account(target) or {}
            if record.get("provider_kind") == "paper":
                paper = self.paper_accounts
                if paper is None:
                    # The row exists but this runtime has no engine configured:
                    # refuse rather than act anywhere else.
                    raise HTTPException(status_code=503, detail="paper_engine_unavailable") from None
                try:
                    await paper.ensure(target, reason=reason)
                except PaperEngineError as error:
                    raise HTTPException(status_code=503, detail="paper_engine_unavailable") from error
                return
            pool = self.account_pool
            if pool is None:
                # The row exists but this runtime cannot reach a pool: refuse
                # rather than act anywhere else.
                raise HTTPException(status_code=503, detail="account_pool_unavailable") from None
            try:
                await pool.ensure(target, reason=reason)
            except PoolCapacityFull as error:
                raise HTTPException(status_code=503, detail="runtime_capacity_full") from error
            except PoolStartRefused as error:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "account_start_failed", "reason": error.reason},
                ) from error
            except PoolError as error:
                raise HTTPException(status_code=502, detail="account_start_failed") from error

        try:
            context = resolve()
        except AccountAssemblyRequired as required:
            await assemble(required.account_id, reason="login")
            return resolve()
        except HTTPException as error:
            # 老用户补开户：注册于本站模拟账户之前、从未绑定快期的成员第一次
            # 请求时补开（注册流程当时没有引擎可调）；开完再解析一次。补开的
            # 账户行带着授权但尚未装配 —— 与上面一样先装配再解析。
            if (error.status_code == 403 and error.detail == "tqkq_account_required"
                    and self.paper_accounts is not None):
                try:
                    await self.paper_accounts.ensure_paper_account(user_id)
                except PaperEngineError as engine_error:
                    raise HTTPException(
                        status_code=503, detail="paper_engine_unavailable") from engine_error
                try:
                    return resolve()
                except AccountAssemblyRequired as required:
                    await assemble(required.account_id, reason="login")
                    return resolve()
            raise
        pool = self.account_pool
        if (pool is not None and context.record.get("provider_kind") == "tqkq-pool"
                and await pool.child_lost(context.account.account_id)):
            await pool.detach(context.account.account_id, reason="child_lost")
            await assemble(context.account.account_id, reason="heal")
            return resolve()
        return context

    def _with_pool_status(self, context: AccountContext) -> AccountContext:
        """A pool or paper account's context carries where its services are, so
        the request-scoped runtime.status block states it like the session one."""
        kind = context.record.get("provider_kind")
        if kind == "tqkq-pool":
            pool_status = (self.account_pool.account_state(context.account.account_id)
                           if self.account_pool is not None else "offline")
        elif kind == "paper":
            pool_status = (self.paper_accounts.account_state(context.account.account_id)
                           if self.paper_accounts is not None else "offline")
        else:
            return context
        return replace(context, pool_status=pool_status)

    def status_for_user(self, user_id: str, mode: str) -> dict[str, object]:
        adapter = self.agent.adapter
        if isinstance(adapter, SessionAgentRouter):
            return adapter.runtime_status_for_user(user_id, mode)
        return adapter.runtime_status(mode)

    def merge_paper_account_status(
        self, status: dict[str, object], account_id: str,
    ) -> dict[str, object]:
        """The session account's own fields over the deployment status (paper only)."""
        return merge_paper_account_status(
            status, store=self.services.store, accounts=self.services.accounts, account_id=account_id)

    def status_for_session(self, session_id: str) -> dict[str, object]:
        adapter = self.agent.adapter
        session = self.services.store.get_session(session_id)
        if isinstance(adapter, SessionAgentRouter):
            status = adapter.runtime_status_for_session(session_id)
        else:
            status = adapter.runtime_status(str(session["mode"]))
        account_id = str(session["tradingAccountId"])
        record = self.services.store.get_trading_account(account_id)
        pool_status = None
        if record is not None and record["provider_kind"] == "tqkq-pool":
            pool_status = (self.account_pool.account_state(account_id)
                           if self.account_pool is not None else "offline")
        elif record is not None and record["provider_kind"] == "paper":
            pool_status = (self.paper_accounts.account_state(account_id)
                           if self.paper_accounts is not None else "offline")
        account = session_account_status_payload(
            self.services.store, account_id=account_id,
            user_id=str(session["userId"]), pool_status=pool_status,
        )
        # #932 follow-up: the session row's account revision, the number a client's next switch names (expectedGeneration).
        generation = {"accountGeneration": int(session.get("accountRevision") or 0)}
        if account is None:
            return {**status, **generation}
        # The session's own account block goes in before the paper merge, which adds its USD sub-account to it
        # (foreignAccountId, 2026-10-21); merging first and overwriting the block afterwards dropped it.
        return {**self.merge_paper_account_status({**status, "account": account}, account_id), **generation}

    def require_session(self, session_id: str, user_id: str):
        session = self.services.broker.get_session(session_id, user_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        return session

    @staticmethod
    def proposal_error(error: Exception) -> HTTPException:
        if isinstance(error, KeyError):
            return HTTPException(status_code=404, detail="proposal_not_found")
        if isinstance(error, RiskViolation):
            return HTTPException(status_code=409, detail={"code": error.code, "riskCheck": error.risk_check})
        if isinstance(error, ProposalConflict):
            # A conflict that carries a user-facing sentence (e.g. a refused strategy card) says it.
            message = str(error)
            # 422 for a request the card cannot carry as asked (ProposalInvalid), 409 for a state conflict.
            return HTTPException(status_code=getattr(error, "status_code", 409), detail={"code": error.code,
                                                          **({"message": message} if message != error.code else {})})
        if isinstance(error, ValueError):
            return HTTPException(status_code=409, detail={"code": str(error)})
        return HTTPException(status_code=400, detail="proposal_operation_failed")
