"""One authenticated capability surface for model tools, CLI and UI.

Calls traverse the actual FastAPI routes, including their validation and guards.
No HTTP target, credential, filesystem path or arbitrary code is model supplied.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import HTTPException, Query, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .app_help import HELP, SKILL_TOPICS, topic_text
from .auth import LOCAL_USER_ID, OWNER_CALL_FACTS_SCOPE, LocalAuth, OwnerAuth
from .broker import session_is_live
from .market_briefs import MarketBriefs
from .message_context import PENDING_CARD_REPLY
from .native_agent import PROCESSING_PLACEHOLDER, NativeAgentAdapter
from .operations import owned_operation
from .skill_registry import (
    AI_FEATURE_SKILLS,
    all_skills,
    availability_map,
    hidden_tool_names,
    read_skill_resource,
    require_skill,
    route_owner_map,
    skill_block_reason,
    skill_by_id,
    skill_content_block,
    tool_owner_map,
)
from .strategy_editor import RETIRED_GUIDE_ROUTES
from .strategy_rules import StrategyPlan
from .tenant_adapters import SessionAgentRouter
from .trader_tools import card_events
from .trading_costs import FeeSchedule
from .wiring.accounts import ACCOUNT_HEADER
from .workbench_api import register_workbench_api
from .workbench_extensions import WorkbenchConfig, WorkbenchExtensions

# What an external caller's request carries into the operations it calls: its credentials and the
# account it named. Dropping the account header made every app_call (chat:send's new session, cards,
# reads) land on the user's default account whatever the caller chose (2026-10-09).
CALLER_HEADERS = ("authorization", "cookie", ACCOUNT_HEADER.lower())

# Explicit product surfaces. Authentication, secrets, core deployment and
# generic code runners are never callable through the catalog.
PRODUCT_PATHS = (
    "/api/runtime/status", "/api/accounts", "/api/account/", "/api/positions", "/api/risk/summary",
    "/api/instruments", "/api/market/", "/api/markets/", "/api/strategies",
    "/api/strategy-plans", "/api/strategy-backtests", "/api/strategy-templates", "/api/strategy-labs",
    "/api/strategy-engine", "/api/trading-costs", "/api/trade-review",
    "/api/replays", "/api/simulation/", "/api/ai-execution", "/api/app/",
    "/api/research/", "/api/voice/", "/api/preferences", "/api/memory", "/api/playbooks",
    # 我的流程 (2026-10-06): saving and editing are the user's own UI actions (the routes refuse
    # anything else); the AI drafts a 流程卡, lists, deletes on request and runs on request.
    "/api/routines",
    # 提醒中心 (#932): the reminder record and its read marks.
    "/api/reminders",
    # The skill registry's own progressive-disclosure surface (2026-09-24): the
    # per-user directory, tier-2 activation and tier-3 resource reads. Shared
    # surface — no single skill owns them, and each route re-checks availability.
    "/api/agent/skills",
    # The owner's user/plan administration (M2): listed for the owner alone,
    # and every route re-checks ownership before doing anything. /api/admin/accounts
    # is the read-only isolation check of accounts and their grants;
    # /api/admin/servers reads the project hosts' resource gauges from the
    # infra console (2026-10-04).
    "/api/admin/plans", "/api/admin/users", "/api/admin/accounts", "/api/admin/skills",
    # 公共行情多账号接力 (2026-10-04): the ordered public market source list and
    # each source's live load — owner-maintained online, no restart needed.
    "/api/admin/market-sources",
    "/api/admin/servers",
    # 真实用户监测 (2026-10-13): the owner's read of the pages' anonymous technical metrics. The
    # beacon itself (/api/telemetry/rum) is the page's own plumbing and stays out of the catalog.
    "/api/admin/rum",
    # 后台内存诊断 (2026-10-26): the owner's tracemalloc switch and read, off by default.
    "/api/admin/memory",
    # 重置本站模拟账户 (#1204): owner-only; the body repeats the account id as its confirmation.
    "/api/admin/paper-accounts",
)
# The conversation itself, for external agents (CLI / MCP): open a chat, send a
# message, pick the model, point the chat at another granted account. The
# embedded model may not call these from inside its own turn — that would be
# the agent driving itself (see CHAT_OPERATIONS guard).
CHAT_OPERATIONS = frozenset({
    "POST /api/sessions", "POST /api/sessions/{session_id}/messages",
    "PUT /api/sessions/{session_id}/account",
    "GET /api/models", "PUT /api/models/preference",
})
# 526-A3: account lifecycle the user performs themselves — opening the site's
# paper account. It changes which accounts the user holds, so it stays a UI
# action in settings: never listed in the catalog and never reachable through
# app_call, while the account reads stay agent-operable. bind-/unbind-tqkq only
# answer 410 since 快期模拟停用交易 (#1205) and stay out of the catalog too.
HUMAN_ONLY_ROUTES = frozenset({
    # 重置本站模拟账户 (#1204): wipes a user's positions and equity — only the owner's own click, never an agent turn.
    "POST /api/admin/paper-accounts/{account_id}/reset",
    "POST /api/accounts/open-paper",
    "POST /api/accounts/bind-tqkq",
    "POST /api/accounts/unbind-tqkq",
    # 锁屏提醒 (2026-10-06): a subscription comes only from the browser after the user's own click and
    # the system's permission prompt — no agent turns a device's lock-screen push on.
    "PUT /api/app/push/subscription",
    # 行情专用快期账号 (2026-10-12): the owner types a TqKq password into 后台「行情源」 to add one,
    # and removing one deletes that sealed password. Hosted credentials stay a human action; the
    # agent reads the list (GET) and may reorder it (PUT) as before.
    "POST /api/admin/market-sources/tqkq",
    "DELETE /api/admin/market-sources/{account_id}",
    # 重置成员密码 (2026-10-21): a password is typed by the administrator, never by an agent. The owner's own
    # change (POST /api/auth/owner/password) is outside PRODUCT_PATHS and so never in the catalog either.
    "POST /api/admin/users/{username}/password",
})
# 作废待确认卡 (2026-10-12, #756): the card's 取消 button route, listed so any AI can do what the click
# does (the embedded model through `tool:reject_proposal`, external agents through either). Only
# rejecting — the confirm route stays out of the catalog: a card is confirmed by the user's own click
# or spoken "确认", never by an agent.
CARD_REJECT_ROUTES = frozenset({"POST /api/proposals/{proposal_id}/reject"})
CHAT_SEND = "chat:send"
# The catalog's operating notes (full and compact directory alike).
CATALOG_WORKFLOW = (
    "先读目录，再用operation读取参数定义；通过app_call调用同一业务接口。",
    "连接慢先GET /api/runtime/status，读取dataConnectionStage、dataStartupTimings各已完成阶段秒数和dataStartupTotalSeconds首份快照总耗时；loading_sdk是本地加载，不代表快期认证。合约目录catalogStatus=loading时仅有首批合约，应稍后重读，不能认定其他合约不存在。",
    "生成策略使用POST /api/strategy-plans/design-jobs，保存任务id后查询进度；同步design接口仅供旧客户端兼容。保存与修改前读取接口schema，修改另存，不覆盖正在运行的策略。",
    "诊断先读/api/app/activity和策略monitor，检查未启动、活动委托、条件、每日限制与连续亏损；不能从盈亏推断软件故障。",
    "回放控制body为{action:play|pause|step|speed|pause_entries|resume_entries|finish|close,speed?:1|2|5|10}。",
    "回放数据通过replayId明确选取；当前回放会话默认只影响自己，不能退回当前模拟账户。",
    "图表扩展先GET配置，按revision提交完整config；与策略规则独立，不执行任意代码。",
    "只在用户要求操作时写入；结果未知先查询，不自动重复提交。不得声称本版本不存在的真盘能力。",
)
HELP_OUTLINE_NOTE = ("内置 AI 经 app_call 读本接口且不带 query.topic 时返回目录而不是下面的 HelpSnapshot："
                     "{groups, topics: {主题id: {title, group, summary}}, next}；带 query.search=<关键词> 时返回 "
                     "{search, matches: [{topic, title, text}], next}（提到该词的句子）；带 topic 时返回该主题的完整 HelpSnapshot。")
CATALOG_PROTECTED_CORE = ("认证与密钥", "交易执行", "风险权限", "更新与部署", "任意源码执行")
# Proposal cards the embedded agent must create through its own card tool (propose_order), never by calling the
# route: a route call carries no turn, so the card would not belong to the conversation and the voice call could
# never confirm it.
PROPOSAL_ROUTES = frozenset({"POST /api/simulation/proposals", "POST /api/simulation/close-batch-proposals",
                             "POST /api/simulation/target-position/proposals", "tool:propose_order"})
CHAT_SEND_TIMEOUT_SECONDS = 120.0
CHAT_SEND_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["text"],
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 4000, "description": "发给 AI 的一句话"},
        "sessionId": {"type": "string", "description": "已有聊天会话；省略则新建一个 chat 会话并返回其 id"},
        "mode": {"type": "string", "enum": ["local", "cloud"], "default": "local", "description": "新建会话时的运行模式"},
        "timeoutSeconds": {"type": "number", "minimum": 5, "maximum": 600, "default": CHAT_SEND_TIMEOUT_SECONDS},
        "uiWindowId": {"type": "string", "description": "这轮默认操作的软件窗口（GET /api/app/ui/windows 的 windowId）"},
        "origin": {"type": "string", "enum": ["voice"], "description": "voice：实时语音委托的一轮，回复第一段口语直答（spoken），逐句播报"},
        "followUp": {"type": "boolean", "description": "语音：用户在上一轮播报未完时插话，这轮优先答最新一句"},
    },
}


def _proposal_summary(card: dict) -> str:
    """One spoken line for a proposal card: what the user is being asked to confirm."""
    side = {"buy_open": "买入开仓", "sell_open": "卖出开仓", "buy_close": "买入平仓", "sell_close": "卖出平仓"}.get(str(card.get("side")), str(card.get("side") or ""))
    if isinstance(card.get("orders"), list) and card.get("totalOrders"):
        parts = [f"{ {'sell_close': '卖平', 'buy_close': '买平'}.get(str(o.get('side')), '平') }{o.get('name') or o.get('symbol')} {o.get('volume')} 手" for o in card["orders"][:6]]
        more = f" 等 {card['totalOrders']} 张" if card["totalOrders"] > 6 else ""
        return f"批量平仓 {card['totalOrders']} 张：" + "，".join(parts) + more
    if card.get("symbol") and card.get("volume"):
        price = card.get("limitPrice")
        return f"{side} {card.get('name') or card['symbol']} {card['volume']} 手" + (f"，限价 {price:g}" if isinstance(price, (int, float)) else "")
    return str(card.get("summary") or card.get("title") or card.get("reason") or "这个方案")


def operation_is_read_only(operation: str) -> bool:
    # 规则回测 posts its parameters but only reads history and computes: nothing is stored or ordered.
    if operation in {"POST /api/app/strategy-explanations", "POST /api/strategies/{strategy_id}/backtest",
                     "POST /api/strategy-backtests", "POST /api/strategy-backtests/inputs", "tool:run_strategy_backtest",
                     "POST /api/strategy-templates/{template_id}/preview"}:
        return True
    if operation.startswith("POST /api/app/ui/windows/") and operation.endswith("/captures"):
        return True
    # Order listing also reconciles original orders and persisted cancel intent.
    # 529-A2c: reading the research desk can start today's judgment — a model
    # call (booked to the platform marker platform:research-desk, not the reader's
    # quota) — so it is not a free read either.
    if operation.startswith("GET "):
        return operation not in {"GET /api/simulation/orders", "GET /api/research/desk"}
    return operation.startswith(("tool:get_", "tool:list_", "tool:search_", "tool:evaluate_", "tool:explain_", "tool:preview_"))


class AppCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(min_length=1, max_length=200)
    path: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str | int | float | bool] = Field(default_factory=dict)
    body: dict | None = None
    replayId: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class CallIdentity:
    bridge: object = field(repr=False)
    user: str
    generation: int
    headers: dict[str, str] = field(repr=False)
    session_id: str | None = None
    binding: object = field(default=None, repr=False)

    async def verify(self):
        await self.bridge.verify_identity(self)


class AppCapabilities:
    def __init__(self, app, origin, current_user, ctx=None):
        self.app, self.origin = app, origin
        self.current_user = current_user
        # The runtime context (account resolution) for the tool bridge; None
        # only in unit tests that never take the native-tool branch.
        self.ctx = ctx
        self.sessions = {}
        # One bridge session per (user, account): a caller naming another account must not reuse the
        # session (and so the cards) of the account it used before.
        self._account_sessions: dict[tuple[str, str], str] = {}
        # Host-only credentials from an authenticated message request. Never
        # persist them or include them in capability schemas or model arguments.
        self._session_headers: dict[str, tuple[str, dict[str, str]]] = {}
        self._user_generations: dict[str, int] = {}

    def bind_session(self, session_id, user, headers):
        if self.app.state.broker.get_session(session_id, user) is None:
            raise HTTPException(404, "session_not_found")
        for identifier, (owner, _) in list(self._session_headers.items()):
            if self.app.state.broker.get_session(identifier, owner) is None:
                self.forget_session(identifier)
        binding = (user, {
            key: headers[key] for key in ("authorization", "cookie") if key in headers
        })
        if self._session_headers.get(session_id) != binding:
            self._session_headers[session_id] = binding

    def forget_session(self, session_id):
        self._session_headers.pop(session_id, None)

    def forget_user(self, user):
        self._user_generations[user] = self._user_generations.get(user, 0) + 1
        for identifier, (owner, _) in list(self._session_headers.items()):
            if owner == user:
                self.forget_session(identifier)

    def capture_identity(self, user, headers=None, session_id=None):
        binding = None
        if headers is None:
            if not session_id or self.app.state.broker.get_session(session_id, user) is None:
                raise HTTPException(401, "session_auth_required")
            binding = self._session_headers.get(session_id)
            if binding is not None and binding[0] == user:
                headers = binding[1]
            elif isinstance(self.app.state.auth, LocalAuth) and user == LOCAL_USER_ID:
                headers = {"authorization": "Bearer " + self.app.state.auth.token}
                self.bind_session(session_id, user, headers)
                binding = self._session_headers[session_id]
            else:
                raise HTTPException(401, "会话身份已失效，请重新登录并发送消息")
        headers = {key: headers[key] for key in CALLER_HEADERS if key in headers}
        return CallIdentity(self, user, self._user_generations.get(user, 0), headers, session_id, binding)

    async def verify_identity(self, identity):
        def require_current(session_live=None):
            if identity.generation != self._user_generations.get(identity.user, 0):
                raise HTTPException(401, "交互式操作身份已撤销，请重新登录并发送消息")
            if identity.session_id:
                if session_live is None:
                    session_live = self.app.state.broker.get_session(identity.session_id, identity.user) is not None
                if not session_live:
                    raise HTTPException(401, "session_auth_required")
                if identity.binding is not None and self._session_headers.get(identity.session_id) is not identity.binding:
                    raise HTTPException(401, "session_auth_revoked")
        # Revalidate through the existing auth provider on every call, including
        # cookie expiry/password changes; a stored chat owner alone is not auth.
        request = Request({"type": "http", "method": "POST", "path": "/api/agent/call",
            "headers": [(key.encode(), value.encode()) for key, value in {**identity.headers, "origin": self.origin}.items()]})
        auth = self.app.state.auth
        session_live = None
        # The merged read looks the session up in the auth's own store. A replay child inherits
        # the parent's owner-password auth but keeps its sessions in the replay database, so
        # there the session is checked through this app's broker as before (2026-10-04: the
        # merged read found no replay session and cut every replay event stream at once).
        if identity.session_id and isinstance(auth, OwnerAuth) and auth.session_store is self.app.state.store:
            # Owner-password streams verify every event: the session row, the token
            # revocation and the user record come from one store read (2026-10-03,
            # four before) and the same checks run on it in the same order. The
            # owner-password auth call is synchronous, so the session cannot change
            # between the two checks below; generation and binding are re-read.
            facts = auth.call_facts(request, identity.session_id, identity.user)
            if facts is not None:
                request.scope[OWNER_CALL_FACTS_SCOPE] = facts
                session_live = facts.session is not None and session_is_live(facts.session)
        require_current(session_live)
        if await self.current_user(request) != identity.user:
            raise HTTPException(403, "session_owner_mismatch")
        require_current(session_live)

    def trader_tools(self, user):
        adapter = self.app.state.adapter
        if isinstance(adapter, SessionAgentRouter):
            adapter = adapter.adapter_for_user(user)
        return getattr(adapter, "trader_tools", None)

    def _account_request(self, headers):
        # The bridge holds no Request of its own; account resolution needs one
        # for the owner-role check, so the same headers verify_identity reuses
        # stand in for it.
        return Request({"type": "http", "method": "POST", "path": "/api/agent/call",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})

    async def tool_session(self, user, headers):
        """The bridge's own session for a tool caller that named none. 2026-09-23
        isolation: it is bound to the account the same caller's /api/sessions
        request would resolve (the named account, the user's chosen default,
        else their grants), pool-assembled on demand — never to the default
        account just because the broker accepts a bare create_session. A member
        with no account is refused with the route's own state
        (tqkq_account_required / account_not_granted). Reused while it lives;
        one per account (2026-10-09)."""
        if getattr(self.app.state, "replay_context", None) is not None:
            # A replay child owns exactly one synthetic account: the replay book.
            account_id = self.app.state.accounts.default.account_id
        else:
            account_id = (await self.ctx.account_context(self._account_request(headers), user)).account.account_id
        session_id = self._account_sessions.get((user, account_id))
        if session_id and self.app.state.broker.get_session(session_id, user) is not None:
            self.sessions[user] = session_id
            return session_id
        return self._new_tool_session(account_id, user)

    async def session_tools(self, session_id, user, headers):
        """The tools one bridge call runs with: the set bound to the session's own
        account (the native adapter builds one per account). A session whose user
        no longer holds that account — an operator revoked after the session was
        made — is refused through the same account resolution, not switched to
        the default account's tools."""
        state = self.app.state
        if getattr(state, "replay_context", None) is not None:
            return self.trader_tools(user)
        context = await self.ctx.account_context(
            self._account_request(headers), user, session_id=session_id)
        adapter = state.adapter
        if isinstance(adapter, SessionAgentRouter):
            adapter = adapter.adapter_for_user(user)
        if isinstance(adapter, NativeAgentAdapter):
            return adapter.tools_for_account(context.account.account_id)
        tools = getattr(adapter, "trader_tools", None)
        if tools is None:
            raise HTTPException(404, "能力不存在，请重新读取目录")
        return tools

    def _new_tool_session(self, account_id, user):
        session_id = self.app.state.broker.create_session(
            "local", "native", user, trading_account_id=account_id).session_id
        self.sessions[user] = session_id
        self._account_sessions[(user, account_id)] = session_id
        return session_id

    def tools(self, user):
        tools = self.trader_tools(user)
        if not tools:
            return {}
        # The catalog offers the same toolset the user's turns get: a skill the
        # user lacks (globally disabled, tier-withheld or owner-only) lists —
        # and accepts — none of its tools.
        hidden = hidden_tool_names(self.app.state.store, user)
        return {"tool:" + item["name"]: item for item in tools.definitions()
                if item["name"] not in {"app_capabilities", "app_call"} and item["name"] not in hidden}

    def _admin_allowed(self, user: str) -> bool:
        """Whether the catalog may list the admin operations: the owner alone.
        The routes re-check ownership on every call (non-owners get 404 there);
        this only keeps the admin surface unlisted for members."""
        if not isinstance(self.app.state.auth, OwnerAuth):
            return False
        # A cloud user always has a row; the owner does not.
        return self.app.state.store.get_user_plan(user) is None

    def routes(self, user=None):
        """The catalog's HTTP operations, optionally filtered to what `user`
        may use: an operation owned by a skill (the registry's route map) is
        listed only when that skill is available to them."""
        unavailable = (frozenset() if user is None else
                       {skill_id for skill_id, reason
                        in availability_map(self.app.state.store, user).items()
                        if reason is not None})
        owned = route_owner_map()
        return {f"{method} {route.path}": route
                for route in self.app.routes if isinstance(route, APIRoute)
                for method in sorted(route.methods or []) if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                and f"{method} {route.path}" not in HUMAN_ONLY_ROUTES
                and f"{method} {route.path}" not in RETIRED_GUIDE_ROUTES
                and (f"{method} {route.path}" in CHAT_OPERATIONS
                     or f"{method} {route.path}" in CARD_REJECT_ROUTES
                     or any(route.path == prefix or route.path.startswith(prefix if prefix.endswith('/') else prefix + '/')
                            for prefix in PRODUCT_PATHS))
                and owned.get(f"{method} {route.path}") not in unavailable}

    def manifest(self, operation=None, *, user, compact=False, query=None):
        """The capability catalog. `operation` reads one entry's full definition (`ui:<field>` reads one
        interface-command field). `compact` (the embedded AI's own app_capabilities tool, 2026-10-14 token
        瘦身) answers the directory as a short index — operations grouped by skill, the interface commands
        as one line per field, no repeated tool descriptions or external-agent guide; with `query` only the
        operations, tools and interface fields that match it."""
        schema = self.app.openapi()
        routes = self.routes(user)
        if not self._admin_allowed(user):
            routes = {key: route for key, route in routes.items()
                      if not key.split(" ", 1)[1].startswith("/api/admin")}
        native = self.tools(user)
        if operation and operation.startswith("ui:"):
            from .ui_control import ui_field_detail
            detail = ui_field_detail(operation.removeprefix("ui:"))
            if detail is None:
                raise HTTPException(404, "界面命令没有这个字段，请看目录 uiControl 的字段索引")
            return {"operation": operation, "commandOperation": "POST /api/app/ui/commands", **detail}
        if compact and not operation:
            return self._compact_manifest(routes, native, query=query)
        if operation in native:
            return {"operation": operation, "description": native[operation]["description"],
                    "bodySchema": native[operation]["inputSchema"], "transport": "app_call.body"}
        if operation == CHAT_SEND:
            return {"operation": CHAT_SEND, "description": self._chat_send_entry()["description"],
                    "bodySchema": CHAT_SEND_SCHEMA, "transport": "app_call.body"}
        if operation and operation not in routes:
            raise HTTPException(404, "能力不存在，请重新读取目录")
        selected = {operation: routes[operation]} if operation else routes
        # Registry attribution: which skill owns each offered operation (null =
        # the shared surfaces — auth, session carrying, card confirmation —
        # which belong to no skill by design).
        route_owners = route_owner_map()
        tool_owners = tool_owner_map()
        components = schema.get("components", {}).get("schemas", {})
        used = {}
        def include_refs(value):
            if isinstance(value, dict):
                ref = value.get("$ref", "")
                name = ref.removeprefix("#/components/schemas/")
                if name in components and name not in used:
                    used[name] = components[name]
                    include_refs(components[name])
                for child in value.values():
                    include_refs(child)
            elif isinstance(value, list):
                for child in value:
                    include_refs(child)
        if operation:
            route = routes[operation]
            include_refs(schema["paths"][route.path][operation.split()[0].lower()])
        result = {"version": 1, "environment": "replay" if getattr(self.app.state.data_provider, "identifier", None) else "simulation",
            "serviceBuild": os.getenv("AI_TRADER_BUILD_SHA", "development"),
            "operations": [{"operation": key, "name": route.name, "readOnly": operation_is_read_only(key), "deprecated": bool(route.deprecated),
                            "skill": route_owners.get(key),
                            **({"schema": schema["paths"][route.path][key.split()[0].lower()]} if operation else {})}
                for key, route in selected.items()] + ([] if operation else [
                    {"operation": name, "name": item["name"], "description": item["description"],
                     "readOnly": operation_is_read_only(name), "deprecated": False,
                     "skill": tool_owners.get(name.removeprefix("tool:"))}
                    for name, item in native.items()] + [self._chat_send_entry()]),
            **({"schemas": used} if operation else {}),
            **({"strategyPlanSchema": StrategyPlan.model_json_schema()} if operation and "/strategy-plans" in operation else {}),
            **({"workbenchConfigSchema": WorkbenchConfig.model_json_schema()} if operation and "/app/config" in operation else {}),
            **({"feeScheduleSchema": FeeSchedule.model_json_schema()} if operation and "/trading-costs" in operation else {}),
            # The developer topic in reading order, then what the embedded AI does for the user and how
            # the conversation behaves (the rest of the manual: GET /api/app/help).
            "guide": [*topic_text("agent"), *HELP["ai-actions"]["detail"], *HELP["chat"]["detail"]],
            "workflow": list(CATALOG_WORKFLOW),
            "protectedCore": list(CATALOG_PROTECTED_CORE)}
        if not operation:
            from .ui_control import UiCommand
            result["uiControl"] = {
                "listOperation": "GET /api/app/ui/windows", "listQuery": {"deviceId": "明确跨设备才指定；* 表示本人全部设备"},
                "commandOperation": "POST /api/app/ui/commands", "commandSchema": UiCommand.model_json_schema(),
                "commandQuery": {"waitMs": "0到5000，建议3000"},
                "receipt": "applied.state 已核对所改字段，observedRevision 可连续使用；页面内容未变时无需重复读取。queued/dispatched 必须按原编号查询，不能重发。",
            }
        if operation:
            # The directory supplies the shared guide once. An operation lookup
            # returns its full schema without repeating unrelated instructions.
            for key in ("guide", "workflow", "protectedCore"):
                result.pop(key)
            if compact and operation == "GET /api/app/help":
                # The embedded AI's own reads of this route (trader_tools, 2026-10-14 瘦身): without a topic
                # it gets the table of contents, not the HelpSnapshot below.
                result["embeddedResponse"] = HELP_OUTLINE_NOTE
        return result

    def _compact_manifest(self, routes, native, *, query=None):
        from .ui_control import UI_COMMAND_OPERATION, ui_command_index
        route_owners = route_owner_map()
        tool_owners = tool_owner_map()
        base = {"version": 1,
                "environment": "replay" if getattr(self.app.state.data_provider, "identifier", None) else "simulation",
                "serviceBuild": os.getenv("AI_TRADER_BUILD_SHA", "development")}
        if query:
            needle = str(query).strip().lower()
            operations = [{"operation": key, "name": route.name, "readOnly": operation_is_read_only(key),
                           "skill": route_owners.get(key)}
                          for key, route in routes.items()
                          if needle in key.lower() or needle in str(route.name).lower()
                          or needle in str(route_owners.get(key) or "")]
            tools = [{"operation": name, "skill": tool_owners.get(name.removeprefix("tool:")),
                      "description": item["description"][:120]}
                     for name, item in native.items()
                     if needle in name.lower() or needle in item["description"].lower()
                     or needle in str(tool_owners.get(name.removeprefix("tool:")) or "")]
            from .ui_control import UiChanges
            fields = [name for name in UiChanges.model_fields if needle in name.lower()]
            return {**base, "query": query, "operations": operations, "tools": tools, "uiFields": fields,
                    "next": "operation=<原文> 读完整参数；界面字段用 operation=\"ui:<字段>\"。"}
        grouped: dict[str, list[str]] = {}
        for key in routes:
            grouped.setdefault(route_owners.get(key) or "shared", []).append(key)
        # The skills' own tools by name (no descriptions — the model carries the mounted ones' already),
        # so a tool-only skill and every deferred tool stay discoverable from the directory.
        for name in native:
            grouped.setdefault(tool_owners.get(name.removeprefix("tool:")) or "shared", []).append(name)
        return {**base,
                "operations": {skill: "；".join(keys) for skill, keys in sorted(grouped.items())},
                "uiControl": {"listOperation": "GET /api/app/ui/windows", "commandOperation": UI_COMMAND_OPERATION,
                              "index": ui_command_index()},
                "workflow": list(CATALOG_WORKFLOW), "protectedCore": list(CATALOG_PROTECTED_CORE),
                "next": ("operation=<目录里的原文> 读该操作的完整参数；operation=\"ui:<字段>\" 读界面命令字段的完整定义；"
                         "query=<关键词> 只列匹配的操作、工具与界面字段。tool: 开头的操作就是你自己的工具，"
                         "没挂上的先 activate_skill 挂上它的技能。")}

    @staticmethod
    def _chat_send_entry():
        return {"operation": CHAT_SEND, "name": "chat_send", "readOnly": False, "deprecated": False,
                "description": ("向内置 AI 发一句话并等待本轮结束（默认 120 秒），返回该轮摘要：最终回复正文、"
                                "工具步骤、警告与运行状态。省略 sessionId 时新建 chat 会话。内置 AI 自己的"
                                "工具调用不能使用，避免自我循环。")}

    async def _chat_send(self, args: AppCall, *, user: str, headers, pending_card_reply: bool = False):
        body = dict(args.body or {})
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(422, "text 不能为空")
        # The message route caps a turn at 4000 characters (MessageRequest) — the same limit the
        # advertised schema carries. Checked before the session is created, so an over-long
        # chat:send is refused without leaving an empty session behind (#529-A2).
        if len(text) > 4000:
            raise HTTPException(422, "text 超过 4000 字上限")
        requested = body.get("timeoutSeconds") or CHAT_SEND_TIMEOUT_SECONDS
        try:
            timeout = float(requested) if not isinstance(requested, bool) else math.nan
        except (TypeError, ValueError):
            timeout = math.nan
        if not math.isfinite(timeout):
            raise HTTPException(422, "timeoutSeconds 必须是 5 到 600 之间的数字")
        timeout = max(5.0, min(600.0, timeout))
        store, broker = self.app.state.store, self.app.state.broker
        target_session = body.get("sessionId")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://ai-trader.local",
                                     trust_env=False, headers={**headers, "origin": self.origin}) as client:
            if not target_session:
                created = await client.post("/api/sessions", params={"chat": "true"},
                                            json={"mode": body.get("mode") or "local"})
                if not created.is_success:
                    raise HTTPException(created.status_code, created.json().get("detail", "session_create_failed"))
                target_session = created.json()["sessionId"]
            elif broker.get_session(target_session, user) is None:
                raise HTTPException(404, "session_not_found")
            events = store.list_events(target_session)
            start_sequence = events[-1][0] if events else 0
            message = {"text": text, **{key: body[key] for key in ("uiWindowId", "origin", "followUp") if body.get(key)}}
            token = PENDING_CARD_REPLY.set((user, str(target_session)) if pending_card_reply else None)
            try:
                accepted = await client.post(f"/api/sessions/{target_session}/messages", json=message)
            finally:
                PENDING_CARD_REPLY.reset(token)
            if not accepted.is_success:
                raise HTTPException(accepted.status_code, accepted.json().get("detail", "message_rejected"))
            run_id = accepted.json()["runId"]
        deadline = asyncio.get_running_loop().time() + timeout
        run = store.get_run(run_id)
        while run["status"] in {"queued", "running"} and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            run = store.get_run(run_id)
        collected = [event for _, event in store.list_events(target_session, after_sequence=start_sequence)]
        # This run's own turn ids: its task events name the run (taskId) and carry the model turn's
        # turnId, and the deterministic routed lines carry the runId directly. A reminder, a
        # price-alert notice or another run's answer that lands between the request and the reply is
        # not this turn's answer and must not be returned as it (#523 R3a). With no match at all
        # (an adapter publishing neither marker) the old whole-window collection stands.
        turn_ids = {str(event["payload"].get("turnId")) for event in collected
                    if event["name"] in {"task.started", "task.progress", "task.completed"}
                    and str(event["payload"].get("taskId")) == str(run_id) and event["payload"].get("turnId")}
        turn_ids |= {str(event["payload"].get("turnId")) for event in collected
                     if event["name"] == "message.completed" and str(event["payload"].get("runId")) == str(run_id)
                     and event["payload"].get("turnId")}
        def this_turn(payload: dict) -> bool:
            return not turn_ids or payload.get("turnId") in turn_ids
        answers = [event["payload"] for event in collected
                   if event["name"] == "message.completed" and event["payload"].get("role") == "assistant"
                   and event["payload"].get("turnId") and event["payload"].get("text") not in {"", None, PROCESSING_PLACEHOLDER}
                   and this_turn(event["payload"])]
        replies = [item["text"] for item in answers]
        # Proposal cards this turn left waiting for the user (the voice call confirms or drops them).
        pending_proposals = [{"proposalId": card["proposalId"], "approvalId": card.get("approvalId"), "version": card.get("version", 1),
                              "type": event["name"].split(".", 1)[1], "summary": _proposal_summary(card),
                              "intentSource": card.get("intentSource")}
                             for event in collected for card in [event["payload"]]
                             if event["name"].startswith("proposal.") and card.get("status") == "approval_required" and card.get("proposalId")
                             and this_turn(card)]
        # The native loop reports its work as task steps (task.progress / task.completed payload.steps).
        task_events = [event for event in collected if event["name"] in {"task.progress", "task.completed"}
                       and (not turn_ids or str(event["payload"].get("taskId")) == str(run_id))]
        tool_steps = [{"name": step.get("label"), "tool": step.get("tool"), "status": step.get("status"), "detail": step.get("detail")}
                      for step in (task_events[-1]["payload"].get("steps") or [])] if task_events else []
        model = task_events[-1]["payload"].get("model") if task_events else None
        warnings = [event["payload"].get("message") or event["payload"].get("title") or ""
                    for event in collected if event["kind"] in {"warning", "error"} and this_turn(event["payload"])]
        # 我的流程 (2026-10-06): a routine ran this turn. Its cards wait for the user's own 确认 — the
        # voice call never confirms them on the user's behalf, whatever 语音下单免口头确认 says.
        routine = any(event["name"] == "card.routine_run" and this_turn(event["payload"]) for event in collected)
        timed_out = run["status"] in {"queued", "running"}
        # The account the conversation acts on (its session's, which a named account set when the
        # session was made here): the caller sees where the cards of this turn will trade.
        bound = broker.get_session(target_session, user)
        return {"ok": run["status"] == "completed", "status": 200 if run["status"] == "completed" else 409 if not timed_out else 202,
                "operation": CHAT_SEND, "environment": "simulation",
                "data": {"sessionId": target_session, "accountId": bound.trading_account_id if bound else None,
                         "runId": run_id, "runStatus": run["status"], "timedOut": timed_out,
                         "reply": replies[-1] if replies else "", "spoken": answers[-1].get("spoken") if answers else None,
                         "pendingProposals": pending_proposals, "toolSteps": tool_steps, "model": model, "routine": routine,
                         "warnings": [item for item in warnings if item], "eventCount": len(collected)}}

    async def call(self, args: AppCall, *, user: str, headers=None, session_id=None, pending_card_reply: bool = False):
        identity = self.capture_identity(user, headers, session_id)
        await identity.verify()
        headers = identity.headers
        target = self.app
        if session_id is not None and args.operation in PROPOSAL_ROUTES:
            raise HTTPException(409, "proposal_route_forbidden_use_tool: 方案卡请直接调用 propose_order 工具生成，不要经 app_call")
        if args.operation == CHAT_SEND or args.operation in CHAT_OPERATIONS:
            if session_id is not None:
                # A call carrying the caller's chat session comes from inside an agent
                # turn: the model must not drive a conversation (its own or another).
                raise HTTPException(409, "chat_recursion_forbidden")
            if args.replayId:
                raise HTTPException(422, "对话接口不作用于回放")
            if args.operation == CHAT_SEND:
                return await self._chat_send(args, user=user, headers=headers, pending_card_reply=pending_card_reply)
        context = getattr(target.state, "replay_context", None)
        if args.replayId:
            router = getattr(target.state, "replay_router", None)
            if router is None:
                if getattr(target.state.data_provider, "identifier", None) != args.replayId:
                    raise HTTPException(404, "回放不属于当前环境")
            else:
                context = await router.resolve(args.replayId, user)
                target = context.child
        if context is not None:
            context.require_open(user)
        bridge = target.state.app_capabilities
        if args.operation in bridge.tools(user):
            if args.path or args.query:
                raise HTTPException(422, "工具参数只放在body中")
            tools = bridge.trader_tools(user)
            tool_name = args.operation.removeprefix("tool:")
            if tools.shared_market is not None and tool_name in tools.SHARED_MARKET_TOOLS:
                # Public reads require a valid identity, never a connected private account.
                response, card = await owned_operation(
                    lambda: tools.shared_market_tool(tool_name, args.body or {},
                        turn_id="app-call:" + uuid4().hex, user_id=user, session_id=session_id, headers=headers),
                    authorize=identity.verify)
                if card is not None and session_id is not None:
                    await target.state.broker.publish(session_id, kind="artifact", name=card[0], payload=card[1])
                return {"ok": response["success"], "status": 200 if response["success"] else 409,
                        "operation": args.operation, "environment": "simulation",
                        "data": json.loads(response["contentItems"][0]["text"])}
            # Native tools are shared with the embedded model, preserving the
            # existing simulation/risk/recovery checks and actual session owner.
            if not session_id or target is not self.app:
                session_id = await bridge.tool_session(user, headers)
            bridge.bind_session(session_id, user, headers)
            response, card = await (await bridge.session_tools(session_id, user, headers)).call_async(
                tool=args.operation.removeprefix("tool:"), arguments=args.body or {},
                turn_id="app-call:" + uuid4().hex, mode="local", session_id=session_id, user_id=user,
                host_identity=identity)
            for event, payload in card_events(card):
                # A card the tool produced (a proposal, a backtest) belongs to the conversation it was
                # created in; an external caller's card was previously stored but never shown. Several cards of one
                # sentence (propose_order legs) are each their own card.
                kind = "approval" if event.startswith("proposal.") else "chart" if event in {"card.backtest", "card.strategy_backtest"} else "artifact"
                await target.state.broker.publish(session_id, kind=kind, name=event, payload=payload)
            data = json.loads(response["contentItems"][0]["text"])
            return {"ok": response["success"], "status": 200 if response["success"] else 409,
                "operation": args.operation, "environment": "replay" if getattr(target.state.data_provider, "identifier", None) else "simulation", "data": data}
        route = bridge.routes().get(args.operation)
        if route is None:
            raise HTTPException(404, "能力不存在，请重新读取目录")
        names = set(re.findall(r"\{([^}]+)\}", route.path))
        if names != set(args.path):
            raise HTTPException(422, {"message": "路径参数必须放在 path 对象的同名键中，operation 保留占位符",
                "expectedPathKeys": sorted(names), "operation": args.operation})
        if args.operation == "POST /api/app/ui/commands":
            from .ui_control import UiCommand
            try:
                UiCommand.model_validate(args.body)
            except ValidationError as error:
                # The route uses this same model before enqueue. No HTTP dispatch or command
                # has happened; unrelated 422s and missing receipts carry no such guarantee.
                return {"ok": False, "status": 422, "operation": args.operation,
                        "environment": "replay" if context is not None else "simulation",
                        "uiCommandAttempted": False,
                        "data": {"detail": error.errors(include_url=False, include_context=False)}}
        path = route.path
        for name, value in args.path.items():
            if not value or any(char in value for char in ('/', '\\', '%', '?', '#')) or value in {'.', '..'}:
                raise HTTPException(422, "无效路径参数")
            path = path.replace('{' + name + '}', quote(value, safe=''))
        # httpx ASGITransport is in-process; it cannot reach arbitrary hosts.
        if session_id is not None and context is None and not str(headers.get(ACCOUNT_HEADER.lower(), "")).strip():
            # A nested tool call (trader_tools → bridge.call(session_id=…)) carries the
            # session's credentials but not its account: capture_identity restores only
            # the login headers, so the business request resolved the caller's default
            # account instead of the account the tool session — or the chat session the
            # turn runs in — is bound to (#674). The session row is re-read on every
            # call (a session rebound by PUT /api/sessions/{id}/account is followed on
            # the next read), an explicit non-empty account header still wins (a blank
            # one means unspecified, the same reading resolve_account gives it), and
            # the value never enters the stored credential binding. resolve_account
            # keeps authorization and assembly: an account the user no longer holds is
            # refused there, never silently re-defaulted. Replay keeps its own
            # single-account scope.
            bound = target.state.broker.get_session(session_id, user)
            if bound is not None and bound.trading_account_id:
                headers = {**headers, ACCOUNT_HEADER.lower(): bound.trading_account_id}
        async def transport_app(scope, receive, send):
            scope["ai_trader.call_identity"] = identity
            if context is not None:
                await context.dispatch(scope, receive, send, authorize=identity.verify)
            elif route.path == "/api/replays/{identifier}/control":
                # The control route owns its queue/commit boundary. Preserve
                # the original revocable identity without owning its wait here.
                await identity.verify()
                await target(scope, receive, send)
            else:
                await owned_operation(lambda: target(scope, receive, send), authorize=identity.verify)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=transport_app), base_url="http://ai-trader.local", trust_env=False) as client:
            response = await client.request(args.operation.split()[0], path, params=args.query,
                json=args.body, headers={**headers, "origin": self.origin})
        try:
            data = response.json()
        except ValueError:
            data = {"detail": "接口未返回JSON结果"}
        result = {"ok": response.is_success, "status": response.status_code, "operation": args.operation,
                  "environment": "replay" if getattr(target.state.data_provider, "identifier", None) else "simulation", "data": data}
        from .ui_control import UI_COMMAND_ATTEMPTED_HEADER
        if not response.is_success and response.headers.get(UI_COMMAND_ATTEMPTED_HEADER) == "false":
            # The UI command route refused before queueing (#759): nothing reached the window.
            result["uiCommandAttempted"] = False
        return result


class CardCoverageRoute(BaseModel):
    route: str
    numeric: int
    withoutCard: int


class CardCoverageGap(BaseModel):
    runId: str
    sessionId: str
    route: str
    finishedAt: str


class CardCoverage(BaseModel):
    """回答卡片化统计 (2026-10-14): finished AI turns in the window, the ones whose answer stated figures,
    and of those the ones that showed no card — the gaps to cover next."""
    days: int
    scope: str = Field(description="self = the caller's own turns; all = every user's (owner only)")
    turns: int
    numericTurns: int
    numericWithCard: int
    numericWithoutCard: int
    cardRate: float | None = None
    byRoute: list[CardCoverageRoute]
    recentGaps: list[CardCoverageGap]


def register_agent_api(app, ctx, root, origin):
    from .guest import guest_call, guest_manifest, guest_request
    extensions = WorkbenchExtensions(root)
    briefs = MarketBriefs(app, extensions)
    app.state.market_briefs = briefs
    current_user = ctx.current_user
    bridge = AppCapabilities(app, origin, current_user, ctx)
    app.state.app_capabilities = bridge

    register_workbench_api(app, ctx, extensions, briefs)

    @app.get("/api/agent/capabilities")
    async def app_capabilities(request: Request, operation: str | None = None):
        if guest_request(request):
            guest_service = app.state.guests
            guest_service.require(request)
            return guest_manifest(app, operation)
        user = await current_user(request)
        return bridge.manifest(operation, user=user)

    @app.post("/api/agent/call")
    async def app_call(request: Request, call: AppCall):
        if guest_request(request):
            app.state.guests.require(request)
            return await guest_call(app, request, call)
        user = await current_user(request)
        headers = {key: request.headers[key] for key in CALLER_HEADERS if key in request.headers}
        return await bridge.call(call, user=user, headers=headers)

    @app.get("/api/app/answer-cards/coverage", response_model=CardCoverage)
    async def answer_card_coverage(request: Request, days: int = Query(7, ge=1, le=30)):
        """回答卡片化统计：AI 回答正文带数字却没出任何卡的回合（日志 agent_card_gap 的可查询计数）。
        所有者看全部用户，其他人看自己的。"""
        user = await current_user(request)
        everyone = bridge._admin_allowed(user)
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        stats = app.state.store.card_coverage(user_id=None if everyone else user, since=since)
        return {"days": days, "scope": "all" if everyone else "self", **stats}

    # -- the skill registry's disclosure surface (M: skills registry) ------------

    @app.get("/api/agent/skills")
    async def agent_skills_list(request: Request) -> dict[str, object]:
        """The whole registry for the current user: every skill with its
        availability and, when unavailable, the machine-readable reason
        (disabled / not_in_plan / owner_only) the UI and external agents read; `helpTopics` are the help
        topics (GET /api/app/help) the skill answers for."""
        user = await current_user(request)
        return {"items": [
            {
                "id": skill.skill_id, "label": skill.label, "category": skill.category,
                "description": skill.description, "tierGated": skill.tier_gated,
                "ownerOnly": skill.owner_only, "parent": skill.parent or None,
                "available": (reason := skill_block_reason(app.state.store, user, skill)) is None,
                "unavailableReason": reason,
                "phrases": list(skill.phrases),
                "helpTopics": SKILL_TOPICS.get(skill.skill_id, []),
                "uiEntries": list(skill.ui_entries),
                "quickAction": skill.quick_action or None,
                # Switched off but not an AI feature: hidden from the AI only; the user's own screens keep it.
                "aiFeature": skill.skill_id in AI_FEATURE_SKILLS,
            }
            for skill in all_skills()
        ]}

    @app.get("/api/agent/skills/{skill_id}")
    async def agent_skill_activate(request: Request, skill_id: str) -> dict[str, object]:
        """Tier 2 for external agents (MCP / CLI): the full SKILL.md as the
        `<skill_content>` block plus its resource list. Unavailable skills refuse
        with the same codes the business entries use."""
        user = await current_user(request)
        skill = skill_by_id(skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill_unknown")
        require_skill(app.state.store, user, skill)
        return {
            "id": skill.skill_id, "label": skill.label, "category": skill.category,
            "description": skill.description, "content": skill_content_block(skill),
            "resources": list(skill.references),
        }

    @app.get("/api/agent/skills/{skill_id}/resource")
    async def agent_skill_resource(request: Request, skill_id: str, path: str) -> dict[str, object]:
        """Tier 3: one file from the skill's own directory (relative paths only)."""
        user = await current_user(request)
        skill = skill_by_id(skill_id)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill_unknown")
        require_skill(app.state.store, user, skill)
        return {"id": skill.skill_id, "path": path,
                "content": read_skill_resource(skill, path)}

    if isinstance(app.state.adapter, SessionAgentRouter):
        app.state.adapter.bind_app_capabilities(bridge)
    else:
        tools = getattr(app.state.adapter, "trader_tools", None)
        if tools is not None:
            tools.app_capabilities = bridge
    return bridge
