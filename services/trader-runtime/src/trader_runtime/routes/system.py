"""Health, authentication, model catalog, agent account and runtime status."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response, status

from .. import __version__, memory_diagnostics
from ..account_pool import PoolError
from ..auth import (
    COOKIE_NAME,
    LOCAL_USER_ID,
    TQKQ_PASSWORD_MAX_BYTES,
    TQKQ_REGISTER_URL,
    CloudAuth,
    LocalAuth,
    OwnerAuth,
)
from ..client_compat import MIN_CLIENT_VERSION
from ..infra_console import RANGE_KEYS, InfraConsoleUnavailable, infra_console_from_env
from ..model_provider import USER_MODEL_CATALOG
from ..native_agent import NativeAgentAdapter
from ..paper_accounts import PAPER_ACCOUNT_PREFIX, PAPER_KIND, PaperEngineError
from ..plans import (
    FEATURE_BY_CAMEL,
    FEATURE_TO_SKILLS,
    FEATURES,
    daily_token_limit,
    default_allowed_model,
    estimated_daily_cost_cny,
    plan_for_user,
    plan_from_config,
)
from ..rum import RANGES as RUM_RANGES
from ..rum import enabled_from_env as rum_enabled_from_env
from ..rum import sample_rate_from_env as rum_sample_rate_from_env
from ..runtime_presentation import describe_runtime
from ..skill_registry import (
    all_skills,
    migrate_config_features,
    skill_by_id,
    tier_gated_skills,
)
from ..skill_registry import (
    status_payload as skill_status_payload,
)
from ..store.trading_accounts import is_market_source_owner, market_source_owner_id, scoped_owner_user_id
from ..wiring.accounts import context_account_status_payload, session_account_status_payload
from ..wiring.context import RuntimeContext
from .account import bind_tqkq_account, store_tqkq_pool_account, tqkq_account_id
from .requests import (
    MARKET_SOURCES_MAX,
    AdminMarketSourceAccountRequest,
    AdminMarketSourcesRequest,
    AdminPaperResetRequest,
    AdminPasswordResetRequest,
    AdminPlanCreateRequest,
    AdminPlanUpdateRequest,
    AdminSkillsUpdateRequest,
    AdminSkillUpdateRequest,
    AdminUserUpdateRequest,
    InviteCodeRequest,
    ModelPreferenceRequest,
    OwnerLoginRequest,
    OwnerLogoutRequest,
    OwnerPasswordChangeRequest,
    OwnerRegisterRequest,
    TqkqLoginRequest,
)


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    log = logging.getLogger("uvicorn.error.auth")
    services = ctx.services
    agent = ctx.agent
    settings = ctx.settings
    store = services.store
    adapter = agent.adapter
    default_account = services.accounts.default
    autonomy = default_account.autonomy
    simulation_orders = default_account.simulation_orders
    auth = ctx.auth
    current_user = ctx.current_user
    status_for_user = ctx.status_for_user
    native_endpoints = agent.native_endpoints
    resolved_agent_mode = settings.agent_mode
    resolved_agent_provider = settings.agent_provider
    resolved_data_mode = settings.data_mode
    runtime_instance_id = ctx.runtime_instance_id
    agent_credential_configured = ctx.agent_credential_configured

    def present(status: dict[str, object]) -> dict[str, object]:
        """新浪备用 (2026-10-07): marketDataStatus reads `backup` while the shared market serves Sina."""
        shared = getattr(application.state, "shared_market", None)
        return shared.present_status(status) if shared is not None else status

    @application.get("/healthz")
    async def healthz() -> dict[str, str | bool | None | dict[str, str | None]]:
        runtime = present(adapter.runtime_status("local"))
        # The lease says leader/standby; a replay child never runs the loops.
        lease = ctx.instance_lease
        instance = lease.snapshot() if lease is not None else {"role": "replay"}
        return {"status": "ok", "version": __version__,
                "commit": os.getenv("AI_TRADER_BUILD_SHA"),
                "minClientVersion": MIN_CLIENT_VERSION,
                "mode": resolved_agent_mode,
                "adapter": adapter.adapter_id, "agentConnected": runtime["agentConnected"],
                "marketDataStatus": runtime["marketDataStatus"], "liveTrading": "disabled",
                "simulationTrading": simulation_orders.enabled,
                "aiExecutionEnabled": autonomy.enabled,
                "authMode": auth.auth_mode,
                "authProvider": getattr(auth, "provider", "local-token"),
                "runtimeInstanceId": runtime_instance_id,
                "instance": instance}

    @application.post("/api/auth/local")
    async def local_login(request: Request, response: Response) -> dict[str, str]:
        if not isinstance(auth, LocalAuth):
            raise HTTPException(status_code=404, detail="local_auth_disabled")
        authorization = request.headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
        if not auth.valid_token(supplied):
            raise HTTPException(status_code=401, detail="invalid_local_token")
        response.set_cookie(COOKIE_NAME, auth.token, httponly=True, samesite="strict", secure=False, path="/")
        return {"status": "authenticated", "userId": LOCAL_USER_ID, "authMode": "local-token"}

    @application.post("/api/auth/cloud")
    async def cloud_login(request: Request, response: Response) -> dict[str, str]:
        if not isinstance(auth, CloudAuth):
            raise HTTPException(status_code=404, detail="cloud_auth_disabled")
        # OIDC key discovery/rotation may perform bounded network I/O. Keep it
        # away from the Runtime event loop; authenticated requests then reuse
        # the verifier's cached signing key.
        return await asyncio.to_thread(auth.login, request, response)

    @application.get("/api/auth/cloud/status")
    async def cloud_auth_status(request: Request) -> dict[str, str | bool]:
        if not isinstance(auth, CloudAuth):
            raise HTTPException(status_code=404, detail="cloud_auth_disabled")
        return await asyncio.to_thread(auth.session_status, request)

    def owner_binding_status() -> dict[str, str | bool]:
        return {
            "profileId": os.getenv("AI_TRADER_OWNER_PROFILE_ID", "owner-default"),
            "tqkqConfigured": resolved_data_mode in {"tqkq-readonly", "tqkq-sidecar"},
            "tqkqMode": "simulation" if simulation_orders.enabled else "read-only",
            # 本站模拟账户 (2026-10-03): registered users get a site-hosted paper
            # account; the client's register copy stays honest about it.
            "paperConfigured": ctx.paper_accounts is not None,
            "modelProvider": resolved_agent_provider,
            "modelCredentialConfigured": (
                any(endpoint.provider == resolved_agent_provider for endpoint in native_endpoints.values())
                or agent_credential_configured
            ),
            "liveTrading": False,
        }

    async def provision_paper_account(user_id: str) -> None:
        """Register- and password-login-time provisioning, failure-tolerant: the account exists the
        moment the user does and is their default. 快期只管行情 (owner 2026-10-31): the administrator
        and share-code accounts land on their own 本站模拟 at every login too, like a 快期 login. An
        engine hiccup must not fail a registration or login — the 403 backfill in the account context
        (members) or the next login (the administrator) provisions instead."""
        if ctx.paper_accounts is None:
            return
        try:
            record = await ctx.paper_accounts.ensure_paper_account(user_id)
            store.set_user_default_account(user_id=user_id, account_id=str(record["id"]))
        except PaperEngineError as error:
            log.warning("paper_account_provision_failed user=%s error=%s", user_id, error)

    @application.post("/api/auth/owner")
    async def owner_login(
        request: Request, response: Response, body: OwnerLoginRequest,
    ) -> dict[str, object]:
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        result = await asyncio.to_thread(
            auth.login, request, response,
            username=body.username, password=body.password,
        )
        await provision_paper_account(str(result["userId"]))
        return {**result, "bindings": owner_binding_status()}

    @application.get("/api/auth/owner/status")
    async def owner_auth_status(request: Request) -> dict[str, object]:
        if not isinstance(auth, OwnerAuth):
            # A Runtime without owner-password auth (local token, cloud JWT) has
            # no owner session, no tier and no TqKq login. Answering the status
            # question is the honest reply; 404 made the browser log a console
            # error on every cloud page load (`useOwnerPlan` asks unconditionally).
            return {"authenticated": False, "ownerAuth": False}
        status_payload = auth.session_status(request)
        tqkq_login = {
            "enabled": ctx.account_pool is not None,
            "inviteRequired": auth.tqkq_invite_required,
            "registerUrl": TQKQ_REGISTER_URL,
        }
        if status_payload["authenticated"]:
            # M2: the caller's tier and skills. The registry derives both the
            # available skill ids and the legacy camel-case `features` map for
            # 1.6.x clients — global switches included, which the plan alone
            # cannot see.
            plan = plan_for_user(store, str(status_payload["userId"]))
            return {
                **status_payload,
                "plan": plan.name,
                "planLabel": plan.label,
                "marketHistoryTradingDays": plan.market_history_trading_days,
                **skill_status_payload(store, str(status_payload["userId"])),
                "bindings": owner_binding_status(),
                "tqkqLogin": tqkq_login,
            }
        # 本站模拟 (2026-10-03): the register form lives on the anonymous status —
        # paperConfigured is deployment info like tqkqLogin.enabled, so the pre-login
        # copy can say 注册即开通本站模拟账户 honestly (post-login it rides in bindings).
        return {**status_payload, "tqkqLogin": tqkq_login, "paperConfigured": ctx.paper_accounts is not None}

    @application.post("/api/auth/tqkq")
    async def tqkq_login(
        request: Request, response: Response, body: TqkqLoginRequest,
    ) -> dict[str, object]:
        """Log in with the user's own TqKq account (M1b): the pool verifies the
        credentials, the first login consumes an invite code, and the sealed
        password lands on the pool account the session then acts on — except
        that a paper-configured deployment binds no TqKq trading account at all
        and lands every user on the site simulation account (see `bind` below). All auth
        semantics (rate limits, the main-account guard, user creation, the
        session) live in `OwnerAuth.login_tqkq`."""
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        pool = ctx.account_pool

        async def verify(username: str, password: str) -> dict[str, object]:
            assert pool is not None
            return await pool.client.verify(username, password)

        async def bind(user_id: str, password: str) -> dict[str, object]:
            assert pool is not None
            username = body.username.strip()
            # 快期模拟停用交易（所有者 2026-10-30，#1205）：部署配置了本站模拟盘时，快期账号
            # 只用来核验身份——每个快期登录（新用户、回头用户、已有或已停用快期交易账户的老用户）
            # 都落在自己的本站模拟账户上并把它设为默认，不再建、绑定或重新启用 tqkq 交易账户，
            # 也不装配 sidecar；同名的行情专用账户（后台「行情源」）不受影响。
            if ctx.paper_accounts is not None:
                try:
                    record = await ctx.paper_accounts.ensure_paper_account(user_id)
                    store.set_user_default_account(user_id=user_id, account_id=str(record["id"]))
                    return {"accountId": str(record["id"]), "displayName": str(record["display_name"]),
                            "assembly": "connected", "kqUsername": username}
                except PaperEngineError as error:
                    # 引擎失败不挡登录（与注册同口径）；首次登录后的补开户路径会再试。
                    log.warning("paper_login_provision_failed user=%s error=%s", user_id, error)
                    return {"accountId": "", "displayName": username,
                            "assembly": "offline", "kqUsername": username}
            return await bind_tqkq_account(
                store, pool, user_id=user_id, username=username,
                password=password, kq_username=username,
            )

        result = await auth.login_tqkq(
            request, response,
            username=body.username, password=body.password, invite_code=body.inviteCode,
            pool_available=pool is not None, verify=verify, bind=bind,
        )
        # The administrator (or a share-code account) logged in through the same form.
        if result.get("status") != "authenticated":
            return result
        await provision_paper_account(str(result["userId"]))
        return {**result, "bindings": owner_binding_status()}

    @application.post("/api/auth/owner/logout")
    async def owner_logout(request: Request, response: Response,
                           body: OwnerLogoutRequest | None = None) -> dict[str, str]:
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        user: str | None = None
        if body is not None and body.deviceId:
            try:
                user = await current_user(request)
            except HTTPException:
                user = None
        # Revoke this device's session. Other devices owned by the same user
        # retain their identities; queued tools and SSE revalidate the token.
        result = auth.logout(request, response)
        # 锁屏提醒 (#521): this device stops receiving the signed-out user's push; their other devices keep it.
        if user is not None and body is not None and body.deviceId:
            store.delete_push_subscriptions(user_id=user, device_id=body.deviceId)
        return result

    @application.post("/api/auth/owner/password")
    async def owner_change_password(request: Request, response: Response,
                                    body: OwnerPasswordChangeRequest) -> dict[str, object]:
        """本人改登录密码（设置 → 账户，2026-10-21）。受保护的人工操作：AI 与外部 Agent 不经能力目录代办。"""
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        identity = await asyncio.to_thread(auth.validate, auth.session_token(request))
        return await asyncio.to_thread(
            auth.change_password, request, response, username=identity.username,
            current_password=body.currentPassword, new_password=body.newPassword)

    @application.post("/api/auth/owner/register")
    async def owner_register(
        request: Request, response: Response, body: OwnerRegisterRequest,
    ) -> dict[str, object]:
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        result = await asyncio.to_thread(
            auth.register, request, response,
            username=body.username, password=body.password, invite_code=body.inviteCode,
        )
        # 注册即开户：一份本站模拟账户（初始资金 100 万）随之而来；engine 失败
        # 不挡注册，老用户补开户路径会在之后的请求里补上。
        if result.get("status") == "registered":
            await provision_paper_account(str(result["userId"]))
        return {**result, "bindings": owner_binding_status()}

    @application.post("/api/auth/owner/invite-codes")
    async def create_invite_code(request: Request, body: InviteCodeRequest | None = None) -> dict[str, object]:
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        identity = await asyncio.to_thread(auth.validate, auth.session_token(request))
        if identity.role != "owner":
            raise HTTPException(status_code=403, detail="owner_only")
        # A tier-named invite lands its first login on that tier; `None` (and
        # the body-less M1b callers) keep the default tier.
        plan = body.plan if body is not None else None
        if plan == "guest":
            raise HTTPException(409, "guest_plan_reserved")
        if plan and store.get_plan_tier(plan) is None:
            raise HTTPException(status_code=400, detail="plan_unknown")
        # The M1b callers post without a body; they keep the 14-day default and
        # a random one-shot code.
        return auth.create_invite_code(
            request, created_by=identity.username,
            days_valid=14 if body is None else body.daysValid, plan=plan or None,
            code=None if body is None else body.code,
            uses="once" if body is None else body.uses,
            never_expires=False if body is None else body.neverExpires)

    @application.get("/api/auth/owner/invite-codes")
    async def list_invite_codes(request: Request) -> list[dict[str, object]]:
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        identity = await asyncio.to_thread(auth.validate, auth.session_token(request))
        if identity.role != "owner":
            raise HTTPException(status_code=403, detail="owner_only")
        return auth.list_invite_codes(request)

    @application.post("/api/auth/owner/invite-codes/{id}/disable")
    async def disable_invite_code(request: Request, id: str) -> dict[str, object]:
        """Owner-only kill switch for one code; disabling twice returns the
        same row, an unknown id 404s."""
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        identity = await asyncio.to_thread(auth.validate, auth.session_token(request))
        if identity.role != "owner":
            raise HTTPException(status_code=403, detail="owner_only")
        disabled = await asyncio.to_thread(auth.disable_invite_code, request, id)
        if disabled is None:
            raise HTTPException(status_code=404, detail="invite_code_unknown")
        return disabled

    # -- M2: the owner's user table ----------------------------------------

    async def require_owner(request: Request):
        """The admin surface answers non-owners with 404, not 403: a cloud user
        must not learn the deployment even has user administration."""
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        identity = await asyncio.to_thread(auth.validate, auth.session_token(request))
        if identity.role != "owner":
            raise HTTPException(status_code=404, detail="owner_only")
        return identity

    def _admin_user_item(
        record: dict[str, object], *,
        tokens_today: int, tokens_month: int, voice_today: int, voice_month: int,
    ) -> dict[str, object]:
        user_id = scoped_owner_user_id(str(record["username"]))
        plan = plan_for_user(store, user_id)
        configured_slugs = configured_model_slugs()
        model = default_allowed_model(
            plan, configured_slugs, store.get_model_pref(user_id))
        seen_epoch = application.state.presence.last_seen_epoch(user_id)
        last_seen = (
            datetime.fromtimestamp(seen_epoch, tz=UTC).isoformat()
            if seen_epoch is not None else store.last_session_activity(user_id)
        )
        return {
            "username": record["username"],
            "role": record["role"],
            "plan": record["plan"],
            "planLabel": plan.label,
            "marketHistoryTradingDays": plan.market_history_trading_days,
            "invitedBy": record["invitedBy"],
            "createdAt": record["createdAt"],
            "lastSeenAt": last_seen,
            "tokensToday": tokens_today,
            "tokensMonth": tokens_month,
            "voiceSecondsToday": voice_today,
            "voiceSecondsMonth": voice_month,
            "estimatedCostToday": estimated_daily_cost_cny(
                tokens_today=tokens_today, model_slug=model, voice_seconds_today=voice_today,
            ),
            "disabled": bool(record["disabled"]),
            "tqkqBound": store.load_tqkq_credentials(user_id=user_id) is not None,
        }

    @application.get("/api/admin/users")
    async def admin_list_users(request: Request) -> dict[str, object]:
        await require_owner(request)
        today = datetime.now(UTC).date().isoformat()
        month = today[:7]
        tokens_today = store.token_usage_totals(today)
        tokens_month = store.token_usage_totals(month)
        voice_today = store.voice_seconds_totals(today)
        voice_month = store.voice_seconds_totals(month)
        items = [
            _admin_user_item(
                record,
                tokens_today=tokens_today.get(scoped_owner_user_id(str(record["username"])), 0),
                tokens_month=tokens_month.get(scoped_owner_user_id(str(record["username"])), 0),
                voice_today=voice_today.get(scoped_owner_user_id(str(record["username"])), 0),
                voice_month=voice_month.get(scoped_owner_user_id(str(record["username"])), 0),
            )
            for record in store.list_cloud_users()
        ]
        return {"items": items, "day": today}

    @application.post("/api/admin/users/{username}/password")
    async def admin_reset_password(request: Request, username: str,
                                   body: AdminPasswordResetRequest) -> dict[str, object]:
        """管理员重置一个本站账号的登录密码（后台 → 成员，2026-10-21）；那个成员所有设备随即退出登录。"""
        await require_owner(request)
        if not isinstance(auth, OwnerAuth):
            raise HTTPException(status_code=404, detail="owner_auth_disabled")
        return await asyncio.to_thread(auth.reset_password, username=username, new_password=body.newPassword)

    @application.put("/api/admin/users/{username}")
    async def admin_update_user(
        request: Request, username: str, body: AdminUserUpdateRequest,
    ) -> dict[str, object]:
        await require_owner(request)
        record = store.get_cloud_user(username)
        if record is None:
            raise HTTPException(status_code=404, detail="user_not_found")
        if body.plan == "guest":
            raise HTTPException(409, "guest_plan_reserved")
        if body.plan is not None and body.plan != record["plan"]:
            if store.get_plan_tier(body.plan) is None:
                raise HTTPException(status_code=400, detail="plan_unknown")
            store.set_user_plan(username, body.plan)
            record["plan"] = body.plan
        if body.disabled is not None and body.disabled != bool(record["disabled"]):
            store.set_user_disabled(username, body.disabled)
            record["disabled"] = body.disabled
            if body.disabled:
                # A disabled user's on-demand accounts stop running right away;
                # the next login of theirs is already refused at the door.
                user_id = scoped_owner_user_id(username)
                for grant in store.list_account_grants(user_id=user_id):
                    account = store.get_trading_account(grant["account_id"])
                    if account is None:
                        continue
                    if (account.get("provider_kind") == "tqkq-pool"
                            and ctx.account_pool is not None):
                        await ctx.account_pool.release(grant["account_id"], reason="user_disabled")
                    if (account.get("provider_kind") == "paper"
                            and ctx.paper_accounts is not None):
                        await ctx.paper_accounts.release(grant["account_id"], reason="user_disabled")
        user_id = scoped_owner_user_id(username)
        today = datetime.now(UTC).date().isoformat()
        return _admin_user_item(
            record,
            tokens_today=store.token_usage(user_id, today),
            tokens_month=store.token_usage_totals(today[:7]).get(user_id, 0),
            voice_today=store.voice_seconds(user_id, today),
            voice_month=store.voice_seconds_totals(today[:7]).get(user_id, 0),
        )

    @application.get("/api/admin/accounts")
    async def admin_list_accounts(request: Request) -> dict[str, object]:
        """The owner's read-only isolation check: every trading account with its
        grants. Read-only by design — grants change through the accounts file,
        binding and unbinding, never through this surface."""
        await require_owner(request)
        names = {
            scoped_owner_user_id(str(record["username"])): str(record["username"])
            for record in store.list_cloud_users()
        }
        # The configured owner is not a `cloud_users` row; without this their
        # grant and the execution-owner column would show the opaque scoped id.
        # (require_owner already refused non-OwnerAuth; this line narrows for mypy.)
        owner_name = auth.username if isinstance(auth, OwnerAuth) else ""
        names.setdefault(scoped_owner_user_id(owner_name), owner_name)
        for account in store.list_trading_accounts():
            if is_market_source_owner(account["execution_owner_user_id"]):
                names[str(account["execution_owner_user_id"])] = "行情专用"
        grants = store.list_all_account_grants()
        by_account: dict[str, list[dict[str, object]]] = {}
        for grant in grants:
            by_account.setdefault(str(grant["account_id"]), []).append({
                "user": names.get(str(grant["user_id"]), str(grant["user_id"])),
                "role": grant["role"],
            })
        items = [
            {
                "id": str(account["id"]),
                "displayName": account["display_name"],
                "providerKind": account["provider_kind"],
                "status": account["status"],
                "executionOwner": names.get(
                    str(account["execution_owner_user_id"]),
                    str(account["execution_owner_user_id"]),
                ),
                "grants": by_account.get(str(account["id"]), []),
            }
            for account in store.list_trading_accounts()
        ]
        return {"items": items}

    # -- 公共行情多账号接力 (2026-10-04) -------------------------------------

    def _market_source_items() -> list[dict[str, object]]:
        shared = getattr(application.state, "shared_market", None)
        if shared is None:
            return []
        accounts = {str(account["id"]): account for account in store.list_trading_accounts()}
        items: list[dict[str, object]] = []
        for item in shared.market_source_status():
            account = accounts.get(str(item["accountId"]))
            items.append({**item, "displayName": account["display_name"] if account else None,
                          "accountStatus": account["status"] if account else "missing",
                          # 行情专用：后台用快期口令加的账号，只作行情源（移除＝停用并删口令）。
                          "marketOnly": bool(account and is_market_source_owner(account["execution_owner_user_id"]))})
        return items

    def _market_source_snapshot() -> dict[str, object]:
        shared = getattr(application.state, "shared_market", None)
        return {
            "items": _market_source_items(),
            "seriesPerSource": shared.market_capacity if shared is not None else 48,
        }

    def _current_market_sources() -> list[str]:
        """The authoritative order an append/remove builds on: the table itself once
        seeded (another Runtime slot may have just changed it — the relay's 5-second
        read cache must not be the write basis), else the relay's first read, which
        seeds it from the environment."""
        if store.market_sources_seeded():
            return store.list_market_sources()
        shared = getattr(application.state, "shared_market", None)
        return shared.ordered_sources() if shared is not None else []

    async def _apply_market_sources(account_ids: list[str]) -> None:
        """Write the ordered list and make it effective now: the public relay rereads
        it on the next read; the paper engine gets the new list before this returns
        when this process holds the lease (a standby slot leaves it to the holder's
        next check, at most one sync interval later)."""
        store.set_market_sources(account_ids)
        shared = getattr(application.state, "shared_market", None)
        if shared is not None:
            shared.reload_sources()
        sync = ctx.paper_accounts.market_sync if ctx.paper_accounts is not None else None
        if sync is not None:
            sync.poke()
            try:
                await sync.sync_once()
            except Exception:  # noqa: BLE001 — the sync loop retries; the list is already saved
                log.warning("market_source_engine_sync_failed", exc_info=True)

    @application.get("/api/admin/market-sources")
    async def admin_list_market_sources(request: Request) -> dict[str, object]:
        """公共行情源清单：the ordered relay accounts with each one's live load
        (channels against the 48 cap, the quote demand it last served, health,
        last error and its time). Read-only; the order changes through the PUT."""
        await require_owner(request)
        shared = getattr(application.state, "shared_market", None)
        return {
            "items": _market_source_items(),
            "seriesPerSource": shared.market_capacity if shared is not None else 48,
            "paperEngine": await _paper_engine_source(),
        }

    async def _paper_engine_source() -> dict[str, object] | None:
        """本站模拟撮合引擎当前用哪个行情账号（账号编号与序号、切换原因，无口令）；
        没配引擎为 null。"""
        sync = ctx.paper_accounts.market_sync if ctx.paper_accounts is not None else None
        return await sync.engine_status() if sync is not None else None

    @application.put("/api/admin/market-sources")
    async def admin_update_market_sources(request: Request, body: AdminMarketSourcesRequest) -> dict[str, object]:
        """Replace the whole ordered source list in one call — the list position
        is the fill order. Only existing active `tqkq-pool` or `tqkq-sidecar`
        accounts qualify (anything else is 422 naming the account); duplicates
        are refused. Takes effect on the next read without a restart: already
        pinned channels stay where they are, a removed source's channels move."""
        await require_owner(request)
        account_ids: list[str] = []
        for raw in body.accountIds:
            account_id = str(raw).strip()
            if not account_id or len(account_id) > 120:
                raise HTTPException(status_code=422, detail={"code": "market_source_invalid", "accountId": raw})
            if account_id in account_ids:
                raise HTTPException(status_code=422, detail={"code": "market_source_duplicate", "accountId": account_id})
            account = store.get_trading_account(account_id)
            if (account is None or str(account["status"]) != "active"
                    or str(account["provider_kind"]) not in {"tqkq-pool", "tqkq-sidecar"}):
                raise HTTPException(status_code=422, detail={"code": "market_source_invalid", "accountId": account_id})
            account_ids.append(account_id)
        # A 行情专用 account leaves only through its DELETE, which also disables it and
        # deletes its sealed password; a reorder must keep it.
        for listed in _current_market_sources():
            record = store.get_trading_account(listed)
            if (listed not in account_ids and record is not None and str(record["status"]) == "active"
                    and is_market_source_owner(record["execution_owner_user_id"])):
                raise HTTPException(status_code=409, detail={"code": "market_source_remove_required", "accountId": listed})
        await _apply_market_sources(account_ids)  # the engine follows the new order right away
        return _market_source_snapshot()

    @application.post("/api/admin/market-sources/tqkq")
    async def admin_add_market_source_account(
        request: Request, body: AdminMarketSourceAccountRequest,
    ) -> dict[str, object]:
        """添加快期行情账号 (2026-10-12): the owner types a TqKq login that is not
        anyone's trading account; the pool verifies it (the TqKq login's check), the
        password is sealed through the same storage as a binding, under a system
        owner of its own (`market-source:<account id>` — in nobody's account
        directory, no session can act or order on it), and the account joins the
        end of the source list, effective at once. Refused before anything is
        written: a wrong password (409 tqkq_bind_failed), a login that already is
        a source or someone's account, or a pool whose child limit cannot carry
        one more source (409 market_source_pool_full with max/running — raise
        AI_TRADER_TQKQ_POOL_MAX). Human-only: never in the agent catalog. The
        password reaches no log line, error or response."""
        await require_owner(request)
        pool = ctx.account_pool
        if pool is None:
            raise HTTPException(status_code=503, detail="account_pool_unavailable")
        username = body.username.strip()
        password = body.password.get_secret_value()
        if not OwnerAuth._TQKQ_USERNAME_PATTERN.fullmatch(username):
            raise HTTPException(status_code=422, detail={"code": "invalid_username"})
        if not password or len(password.encode()) > TQKQ_PASSWORD_MAX_BYTES:
            raise HTTPException(status_code=422, detail={"code": "invalid_password"})
        static_username = auth.static_tqkq_username if isinstance(auth, OwnerAuth) else None
        if static_username and username.casefold() == static_username.casefold():
            # The main account already is the resident sidecar source.
            raise HTTPException(status_code=409, detail={"code": "market_source_main_account"})
        account_id = tqkq_account_id(username)
        owner_id = market_source_owner_id(account_id)
        sources = _current_market_sources()
        if account_id in sources:
            raise HTTPException(status_code=409, detail={"code": "market_source_duplicate", "accountId": account_id})
        existing = store.get_trading_account(account_id)
        if existing is not None and existing["execution_owner_user_id"] != owner_id:
            # A member's own trading account (or one bound earlier): it joins through
            # the picker below, its password stays theirs.
            raise HTTPException(status_code=409, detail={"code": "market_source_account_in_use", "accountId": account_id})
        if len(sources) >= MARKET_SOURCES_MAX:
            raise HTTPException(status_code=409, detail={"code": "market_source_list_full", "max": MARKET_SOURCES_MAX})
        try:
            entries = await pool.client.entries()
        except PoolError as error:
            raise HTTPException(status_code=503, detail="account_pool_unavailable") from error
        raw_capacity = entries.get("capacity")
        capacity: dict[str, Any] = raw_capacity if isinstance(raw_capacity, dict) else {}
        limit = int(capacity.get("max") or 0)
        running = int(capacity.get("running") or 0)
        pool_sources = sum(
            1 for source in sources
            if (record := store.get_trading_account(source)) is not None
            and str(record["provider_kind"]) == "tqkq-pool")
        if limit and (pool_sources + 1 > limit or running >= limit):
            raise HTTPException(status_code=409, detail={
                "code": "market_source_pool_full", "max": limit, "running": running,
                "poolSources": pool_sources})
        try:
            verified = await pool.client.verify(username, password)
        except PoolError as error:
            raise HTTPException(status_code=503, detail="account_pool_unavailable") from error
        if not verified.get("ok"):
            raise HTTPException(status_code=409, detail={
                "code": "tqkq_bind_failed", "reason": str(verified.get("reason") or "auth_failed")})
        store_tqkq_pool_account(store, pool, owner_user_id=owner_id, username=username, password=password)
        await _apply_market_sources([*sources, account_id])
        log.info("market_source_added account=%s position=%d", account_id, len(sources))
        return {"accountId": account_id, **_market_source_snapshot()}

    @application.delete("/api/admin/market-sources/{account_id}")
    async def admin_remove_market_source_account(request: Request, account_id: str) -> dict[str, object]:
        """移除行情专用账号 (2026-10-12): only an account added above (system owner).
        It leaves the source list (channels move to the other sources, the paper
        engine follows), the row is disabled and its grants and sealed password
        deleted — so nothing can restart it — and then its pool child stops.
        Members' accounts leave the list through the PUT and keep their binding."""
        await require_owner(request)
        record = store.get_trading_account(account_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "market_source_unknown", "accountId": account_id})
        owner_id = str(record["execution_owner_user_id"])
        if not is_market_source_owner(owner_id):
            raise HTTPException(status_code=409, detail={"code": "market_source_not_market_only", "accountId": account_id})
        await _apply_market_sources([source for source in _current_market_sources() if source != account_id])
        store.disable_trading_account(account_id)
        store.delete_account_grants(account_id=account_id)
        store.delete_tqkq_credentials(user_id=owner_id)
        pool = ctx.account_pool
        if pool is not None:
            await pool.release(account_id, reason="market_source_removed")
        log.info("market_source_removed account=%s", account_id)
        return _market_source_snapshot()

    @application.get("/api/admin/servers")
    async def admin_servers(request: Request, range: str = "24h") -> dict[str, object]:
        """后台「服务器」的数据面：项目主机的资源仪表与曲线，全部来自运维台
        每 5 分钟的只读采集（Runtime 不另存主机清单）。无令牌时如实回答
        unconfigured；运维台不可达时返回 60 秒内的上次成功结果并标 stale。"""
        await require_owner(request)
        if range not in RANGE_KEYS:
            raise HTTPException(status_code=400, detail="invalid_range")
        metrics = getattr(application.state, "infra_console_metrics", None)
        if metrics is None:
            metrics = infra_console_from_env()
            application.state.infra_console_metrics = metrics
        if metrics is None:
            return {"status": "unconfigured"}
        try:
            return await metrics.servers_payload(range)
        except InfraConsoleUnavailable:
            raise HTTPException(status_code=502, detail="infra_console_unavailable") from None

    @application.get("/api/admin/rum")
    async def admin_rum(request: Request, range: str = "24h") -> dict[str, object]:
        """后台「用户体验」（真实用户监测，2026-10-13）：各端页面按分钟上报的匿名技术指标汇总——
        LCP/INP/CLS/FCP/TTFB、价格端到端延迟、K 线首屏、会话流断线重连、页面报错分类计数、
        驻留与内存；每项给 p50/p75/p95、按客户端（web/desktop/mobile）分组与时序。保留 14 天。
        AI_TRADER_RUM_ENABLED=0 时 `enabled` 为 false（已存的数据照常可读）。"""
        await require_owner(request)
        if range not in RUM_RANGES:
            raise HTTPException(status_code=400, detail="invalid_range")
        rum_store = getattr(application.state, "rum_store", None)
        enabled = rum_enabled_from_env()
        if rum_store is None:
            return {"status": "unavailable", "enabled": enabled}
        summary = await asyncio.to_thread(rum_store.summary, range)
        return {**summary, "enabled": enabled, "sampleRate": rum_sample_rate_from_env()}

    @application.get("/api/admin/memory")
    async def admin_memory(request: Request, limit: int = 25, types: bool = False) -> dict[str, object]:
        """后台内存诊断 (2026-10-26): off by default. While tracing, the allocations alive now that were not at the
        baseline, grouped by call stack, largest first; `types=true` adds the live object count per type."""
        await require_owner(request)
        return await asyncio.to_thread(memory_diagnostics.report, max(1, min(limit, 100)), types=types)

    @application.post("/api/admin/memory/trace")
    async def admin_memory_trace(request: Request, frames: int = 10) -> dict[str, object]:
        """Start tracing with a baseline at now (again while tracing: move the baseline to now)."""
        await require_owner(request)
        return await asyncio.to_thread(memory_diagnostics.start, max(1, min(frames, memory_diagnostics.MAX_FRAMES)))

    @application.delete("/api/admin/memory/trace")
    async def admin_memory_untrace(request: Request) -> dict[str, object]:
        await require_owner(request)
        return await asyncio.to_thread(memory_diagnostics.stop)

    @application.post("/api/admin/paper-accounts/{account_id}/reset")
    async def admin_reset_paper_account(request: Request, account_id: str, body: AdminPaperResetRequest) -> dict[str, object]:
        """重置本站模拟账户 (#1204, owner decision 2026-10-30): owner-only, not offered to users. The engine
        cancels the account's live orders, clears its positions and sets equity back to the initial balance;
        trades stay in the history. The body repeats the id (409 confirm_mismatch otherwise)."""
        await require_owner(request)
        if body.confirmAccountId != account_id:
            raise HTTPException(status_code=409, detail={"code": "confirm_mismatch"})
        record = store.get_trading_account(account_id)
        if record is None or str(record["provider_kind"]) != PAPER_KIND or not account_id.startswith(PAPER_ACCOUNT_PREFIX):
            raise HTTPException(status_code=404, detail={"code": "paper_account_not_found"})
        if ctx.paper_accounts is None:
            raise HTTPException(status_code=503, detail="paper_engine_unavailable")
        try:
            return await ctx.paper_accounts.client.reset(account_id)
        except PaperEngineError as error:
            code = str(error)
            status_code = {"account_not_found": 404, "confirm_mismatch": 409}.get(code, 503)
            raise HTTPException(status_code=status_code, detail={"code": code}) from error

    # -- M2: the owner's tier administration --------------------------------

    CATALOG_SLUGS = {spec.slug for spec in USER_MODEL_CATALOG}

    def _plan_item(tier: dict[str, Any], user_counts: dict[str, int]) -> dict[str, object]:
        plan = plan_from_config(str(tier["name"]), str(tier["label"]), dict(tier["config"]))
        return {
            "name": tier["name"],
            "label": tier["label"],
            "isDefault": tier["isDefault"],
            "models": None if plan.models is None else sorted(plan.models),
            "dailyTokens": plan.daily_tokens,
            "voiceSecondsPerDay": plan.voice_seconds_per_day,
            "marketHistoryTradingDays": plan.market_history_trading_days,
            "briefProducts": plan.brief_products,
            # The tier's open (tier-gated) skills; `features` is the 1.6.x
            # editor's spelling of the same facts, derived, never stored —
            # exactly the feature catalog's keys, without the status payload's
            # strategyTools aggregate.
            "skills": sorted(plan.skill_ids),
            "features": {
                feature.camel_key: plan.has_skill_in_group(feature.key)
                for feature in FEATURES
            },
            "userCount": user_counts.get(str(tier["name"]), 0),
        }

    @application.get("/api/admin/plans")
    async def admin_list_plans(request: Request) -> dict[str, object]:
        await require_owner(request)
        counts = store.count_users_by_plan()
        return {
            "items": [_plan_item(tier, counts) for tier in store.list_plan_tiers()],
            # The editor checks skills; the 1.6.x client still reads
            # featureCatalog, so both catalogs ride along.
            "skillCatalog": [
                {"id": skill.skill_id, "label": skill.label, "category": skill.category,
                 "description": skill.description}
                for skill in tier_gated_skills()
            ],
            "featureCatalog": [
                {"key": feature.camel_key, "label": feature.label,
                 "description": feature.description}
                for feature in FEATURES
            ],
            "modelCatalog": [
                {"slug": spec.slug, "label": spec.display_name} for spec in USER_MODEL_CATALOG
            ],
        }

    @application.post("/api/admin/plans", status_code=status.HTTP_201_CREATED)
    async def admin_create_plan(request: Request, body: AdminPlanCreateRequest) -> dict[str, object]:
        await require_owner(request)
        source = store.get_plan_tier(body.copyFrom) if body.copyFrom else store.get_default_plan_tier()
        if source is None:
            raise HTTPException(status_code=400, detail="plan_unknown")
        config = dict(source["config"])
        if source["name"] == "guest":
            config["marketHistoryTradingDays"] = None
        plan_from_config("new-member-tier", body.label, config)
        tier = store.create_plan_tier(label=body.label.strip(), copy_of=config)
        return _plan_item(tier, store.count_users_by_plan())

    @application.put("/api/admin/plans/{name}")
    async def admin_update_plan(
        request: Request, name: str, body: AdminPlanUpdateRequest,
    ) -> dict[str, object]:
        await require_owner(request)
        tier = store.get_plan_tier(name)
        if tier is None:
            raise HTTPException(status_code=404, detail="plan_not_found")
        provided = body.model_fields_set
        config = dict(tier["config"])
        if "marketHistoryTradingDays" in provided:
            days = body.marketHistoryTradingDays
            if name == "guest" and days not in (1, 3, 5):
                raise HTTPException(422, "guest_market_history_limit_required")
            if name != "guest" and days is not None:
                raise HTTPException(422, "plan_market_history_limit_unsupported")
            config["marketHistoryTradingDays"] = days
        if "models" in provided:
            if body.models is not None and any(
                    slug not in CATALOG_SLUGS for slug in body.models):
                raise HTTPException(status_code=400, detail="model_unknown")
            config["models"] = body.models
        if "dailyTokens" in provided:
            config["dailyTokens"] = body.dailyTokens
        if "voiceSecondsPerDay" in provided:
            config["voiceSecondsPerDay"] = body.voiceSecondsPerDay
        if "briefProducts" in provided:
            config["briefProducts"] = body.briefProducts
        if body.skills is not None:
            unknown = [key for key in body.skills if skill_by_id(key) is None]
            not_gated = [key for key in body.skills
                         if (skill := skill_by_id(key)) is not None and not skill.tier_gated]
            if unknown:
                raise HTTPException(status_code=400, detail="skill_unknown")
            if not_gated:
                # A non-gated skill is everyone's by definition; a tier cannot
                # own it. Catch the typo instead of silently storing a no-op.
                raise HTTPException(status_code=400, detail="skill_not_tier_gated")
            # Merge semantics: ids the body mentions flip to their value, ids
            # it omits keep the tier's current set — a one-id CLI/AI call must
            # not silently turn the other skills off.
            previous = set(config.get("skills") or [])
            for skill_id, on in body.skills.items():
                if on:
                    previous.add(skill_id)
                else:
                    previous.discard(skill_id)
            config["skills"] = sorted(previous)
        if body.features is not None:
            # The 1.6.x admin client still PUTs camel feature keys. Translate
            # each to its skill group with the same merge semantics, then drop
            # the legacy spelling from the row.
            unknown = [key for key in body.features if key not in FEATURE_BY_CAMEL]
            if unknown:
                raise HTTPException(status_code=400, detail="feature_unknown")
            migrated = migrate_config_features(config)
            if migrated is not None:
                config = migrated
            skills = set(config.get("skills") or [])
            for camel_key, on in body.features.items():
                group = FEATURE_TO_SKILLS[FEATURE_BY_CAMEL[camel_key].key]
                if on:
                    skills |= set(group)
                else:
                    skills -= set(group)
            config["skills"] = sorted(skills)
        if name == "guest" and body.isDefault:
            raise HTTPException(409, "guest_plan_reserved")
        updated = store.update_plan_tier(
            name, label=body.label, config=config, is_default=body.isDefault)
        assert updated is not None
        return _plan_item(updated, store.count_users_by_plan())

    @application.delete("/api/admin/plans/{name}")
    async def admin_delete_plan(request: Request, name: str) -> dict[str, object]:
        await require_owner(request)
        tier = store.get_plan_tier(name)
        if tier is None:
            raise HTTPException(status_code=404, detail="plan_not_found")
        if name == "guest":
            raise HTTPException(409, "guest_plan_reserved")
        if tier["isDefault"]:
            raise HTTPException(status_code=409, detail="plan_is_default")
        if store.count_users_by_plan().get(name, 0) > 0 or not store.delete_plan_tier(name):
            raise HTTPException(status_code=409, detail="plan_in_use")
        return {"status": "deleted", "name": name}

    # -- the skill registry's global switches (2026-09-24) --------------------

    def _skill_admin_items() -> list[dict[str, object]]:
        """The whole registry for the admin page: every skill with its global
        switch state and which tiers open it — the matrix view of `skill_settings`
        and `plan_tiers.skills` in one read."""
        tier_skills = {
            str(tier["name"]): set(dict(tier["config"]).get("skills") or [])
            for tier in store.list_plan_tiers()
        }
        return [
            {
                "id": skill.skill_id,
                "label": skill.label,
                "category": skill.category,
                "description": skill.description,
                "parent": skill.parent,
                "summary": skill.summary,
                "tierGated": skill.tier_gated,
                "ownerOnly": skill.owner_only,
                "enabled": store.skill_enabled(skill.skill_id),
                "tiers": {
                    name: skill.skill_id in skills
                    for name, skills in sorted(tier_skills.items())
                },
            }
            for skill in all_skills()
        ]

    @application.get("/api/admin/skills")
    async def admin_list_skills(request: Request) -> dict[str, object]:
        await require_owner(request)
        return {"items": _skill_admin_items()}

    @application.put("/api/admin/skills")
    async def admin_update_skills(
        request: Request, body: AdminSkillsUpdateRequest,
    ) -> dict[str, object]:
        """Flip several global switches at once (a partial map; ids absent keep
        their state). The switch binds everyone — the owner included."""
        await require_owner(request)
        unknown = [skill_id for skill_id in body.skills if skill_by_id(skill_id) is None]
        if unknown:
            raise HTTPException(status_code=400, detail="skill_unknown")
        for skill_id, enabled in body.skills.items():
            store.set_skill_enabled(skill_id, enabled)
        return {"items": _skill_admin_items()}

    @application.put("/api/admin/skills/{skill_id}")
    async def admin_update_skill(
        request: Request, skill_id: str, body: AdminSkillUpdateRequest,
    ) -> dict[str, object]:
        await require_owner(request)
        skill = skill_by_id(skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill_unknown")
        store.set_skill_enabled(skill_id, body.enabled)
        return {"items": _skill_admin_items()}

    def configured_model_slugs() -> set[str]:
        return set(native_endpoints)

    def _model_catalog(user_id: str) -> dict[str, object]:
        today = datetime.now(UTC).date().isoformat()
        plan = plan_for_user(store, user_id)
        daily_limit = daily_token_limit(
            plan, env_limit=int(os.getenv("AI_TRADER_DAILY_TOKEN_LIMIT", "200000000")))
        preferred = store.get_model_pref(user_id)
        configured_slugs = configured_model_slugs()
        models = []
        for spec in USER_MODEL_CATALOG:
            if spec.slug not in configured_slugs:
                continue
            selectable = plan.allows_model(spec.slug)
            models.append({
                "slug": spec.slug,
                "displayName": spec.display_name,
                "provider": spec.provider,
                "tier": spec.tier,
                "configured": True,
                "selectable": selectable,
                "memberOnly": spec.tier == "member",
                "inPlan": plan.allows_model(spec.slug),
            })
        allowed = [entry["slug"] for entry in models if entry["selectable"]]
        # A preference the tier no longer allows falls back to the first allowed
        # model without being rewritten — an upgrade brings it right back.
        current = preferred if preferred in allowed else (allowed[0] if allowed else None)
        return {
            "current": current,
            "models": models,
            "quota": {
                "dailyTokenLimit": daily_limit,
                "usedTokensToday": store.token_usage(user_id, today),
                "day": today,
                "plan": plan.name,
                "planLabel": plan.label,
                "marketHistoryTradingDays": plan.market_history_trading_days,
                "voiceSecondsToday": store.voice_seconds(user_id, today),
                "voiceSecondsPerDay": plan.voice_seconds_per_day,
            },
        }

    @application.get("/api/models")
    async def list_models(request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        return _model_catalog(user_id)

    @application.put("/api/models/preference")
    async def set_model_preference(request: Request, body: ModelPreferenceRequest) -> dict[str, object]:
        user_id = await current_user(request)
        spec = next((entry for entry in USER_MODEL_CATALOG if entry.slug == body.model), None)
        if spec is None or spec.slug not in configured_model_slugs():
            raise HTTPException(status_code=404, detail="model_unavailable")
        if not plan_for_user(store, user_id).allows_model(spec.slug):
            raise HTTPException(status_code=403, detail="model_not_in_plan")
        store.set_model_pref(user_id, body.model)
        return _model_catalog(user_id)

    @application.get("/api/agent/account")
    async def agent_account(request: Request) -> dict[str, object]:
        await current_user(request)
        if isinstance(adapter, NativeAgentAdapter):
            return await adapter.refresh_account()
        if resolved_agent_mode == "fake":
            return {
                "available": True,
                "authenticated": True,
                "loginRequired": False,
                "authMode": "fake",
                "planType": None,
            }
        return {
            "available": False,
            "authenticated": False,
            "loginRequired": False,
            "authMode": None,
            "planType": None,
            "error": "agent_login_not_available",
        }

    @application.get("/api/runtime/status")
    async def runtime_status(request: Request, mode: Literal["local", "cloud"] = "local") -> dict[str, object]:
        user_id = await current_user(request)
        try:
            context = await ctx.account_context(request, user_id)
        except HTTPException as error:
            # The status itself stays readable while the user's pool account is
            # queued for capacity: the account block says where it is (M1a's
            # directory contract, extended to the status endpoint in M1b).
            if error.status_code != 503:
                raise
            fallback = store.get_user_default_account(user_id=user_id)
            account_id = fallback if fallback is not None else services.accounts.default.account_id
            pool_status = (ctx.account_pool.account_state(account_id)
                           if ctx.account_pool is not None else "queued")
            queued_status: dict[str, object] = {
                **status_for_user(user_id, mode),
                "liveTrading": False,
                "simulationTrading": False,
                "aiExecution": None,
                "authMode": auth.auth_mode,
                "authProvider": getattr(auth, "provider", "local-token"),
            }
            account_payload = session_account_status_payload(
                store, account_id=account_id, user_id=user_id, pool_status=pool_status,
            )
            if account_payload is not None:
                queued_status["account"] = account_payload
            return present(describe_runtime(queued_status))
        account = context.account
        return present(describe_runtime(ctx.merge_paper_account_status({
            **status_for_user(user_id, mode),
            "account": context_account_status_payload(context),
            "liveTrading": False,
            "simulationTrading": account.simulation_orders.enabled and account.autonomy.supports_mode(mode),
            "aiExecution": (account.autonomy.current(user_id=user_id)
                            if account.autonomy.supports_mode(mode) else None),
            "authMode": auth.auth_mode,
            "authProvider": getattr(auth, "provider", "local-token"),
        }, account.account_id)))
