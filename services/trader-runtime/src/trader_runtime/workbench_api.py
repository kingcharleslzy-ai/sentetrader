"""Typed workbench routes shared by Runtime and offline OpenAPI generation."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .agent_activity import activity_snapshot, with_foreign_half
from .app_help import help_snapshot, resolve_topic
from .data_providers import provider_read_snapshot
from .instruments import SYMBOL_PATTERN
from .preferences import register_preferences
from .skill_registry import skill_block_reason, skill_by_id
from .ui_control import TourStep, register_ui_control
from .workbench_extensions import WorkbenchConfig


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    config: WorkbenchConfig


class ConfigRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)


class ConfigSnapshot(BaseModel):
    revision: int
    config: WorkbenchConfig
    canRollback: bool


class BriefRefresh(BaseModel):
    # AI 简报多品种订阅 (2026-10-05): symbol omitted = rotate through the user's
    # subscriptions; the 1.6.x clients that always send it keep their contract.
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40)


class Brief(BaseModel):
    id: str
    kind: Literal["market", "news"]
    text: str
    source: str
    url: str | None = None
    publishedAt: str
    expiresAt: str
    eventAt: str | None = None
    # The merged multi-product read (no symbol) tags every item; the
    # single-symbol contract leaves them absent.
    product: str | None = None
    productName: str | None = None
    symbol: str | None = None


class BriefsSnapshot(BaseModel):
    items: list[Brief]
    # "disabled" = the tier or the user's own setting has briefs off; absent
    # means briefs run and the list is simply empty right now.
    status: str | None = None


class BriefRefreshResult(BaseModel):
    status: Literal["disabled", "unavailable", "idle", "pending"]
    # The product the rotation picked; present only on "pending".
    product: str | None = None


class BriefSubscriptionItem(BaseModel):
    """One subscribed product with its Chinese name and the product's current
    main contract (null when the board read could not resolve it)."""
    product: str
    name: str
    symbol: str | None = None


class BriefSubscriptionsSnapshot(BaseModel):
    products: list[BriefSubscriptionItem]
    # The tier cap; null = unlimited.
    limit: int | None
    # default = derived (watchlist / current main contract); user = the stored choice.
    source: Literal["default", "user"]
    # A tier cut left stored choices beyond the cap: reads and generation cover
    # only the first `limit`, the stored list is never auto-trimmed.
    overLimit: bool = False


class BriefSubscriptionsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    products: list[str] = Field(max_length=50)


class HelpFaq(BaseModel):
    q: str
    a: str


class HelpTerm(BaseModel):
    term: str
    plain: str
    when: str


class HelpTopic(BaseModel):
    """Fields in reading order: the plain layer (summary, steps, faq, glossary terms), then detail."""
    title: str
    group: str
    summary: str
    steps: list[str]
    faq: list[HelpFaq]
    terms: list[HelpTerm] | None = None
    detail: list[str]
    # The whole topic as ordered text (summary → steps → faq → detail): what agents quote and what
    # clients before the two-layer card render.
    paragraphs: list[str]
    # The guided-tour topic carries its default course so the client never hardcodes the copy.
    tour: list[TourStep] | None = None


class HelpGroup(BaseModel):
    id: str
    title: str
    ownerOnly: bool = False
    collapsed: bool = False


class HelpSnapshot(BaseModel):
    groups: list[HelpGroup]
    aliases: dict[str, str]
    topics: dict[str, HelpTopic]


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    symbol: str
    direction: Literal["BUY", "SELL"]
    offset: Literal["OPEN", "CLOSE", "CLOSETODAY"]
    volume: int
    priceType: Literal["LIMIT"]
    limitPrice: float


class ActivityOrder(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    proposalId: str
    clientOrderId: str
    request: OrderRequest
    submitPhase: Literal["PREPARED", "CALL_STARTED", "UNKNOWN", "CONFIRMED", "ABANDONED"]
    cancelPhase: Literal["NONE", "CALL_STARTED", "UNKNOWN", "CONFIRMED"]
    displayStatus: str
    filledVolume: int
    reason: str
    source: Literal["ai", "stop"]
    createdAt: str
    updatedAt: str
    strategyId: str | None = None


class ActivityFill(BaseModel):
    model_config = ConfigDict(extra="allow")
    tradeId: str
    clientOrderId: str
    symbol: str
    direction: str
    offset: str
    price: float | None
    volume: int
    tradeDateTime: int
    source: Literal["ai", "stop"]
    strategyId: str | None = None


class StrategyDecision(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str
    detail: str | None = None
    # When this judgement was made, when the current run started and how many judgements it has
    # made (1.6.6; the 对话面板's running-strategy banner reads them).
    checkedAt: str | None = None
    since: str | None = None
    checks: int | None = None
    # 等待行情 (2026-10-12): on status "waiting", the source code of what is not ready yet
    # (strategy_monitor.MARKET_WAIT_REASONS), when this uninterrupted wait began, and whether it has
    # lasted 10 minutes outside a closed session.
    waitReason: str | None = None
    waitingSince: str | None = None
    stalled: bool | None = None


class MonitorState(BaseModel):
    model_config = ConfigDict(extra="allow")
    pauseReason: str | None = None
    consecutiveLosses: int


class ActivityStrategy(BaseModel):
    strategyId: str
    name: str
    status: str
    decision: StrategyDecision | None
    monitorState: MonitorState | None
    recentAttempts: list[dict[str, Any]]


class ActivitySnapshot(BaseModel):
    environment: Literal["replay", "simulation"]
    replayId: str | None
    symbol: str
    capturedAt: str | None
    dataStatus: str | None
    ageSeconds: float | None
    orders: list[ActivityOrder]
    fills: list[ActivityFill]
    strategies: list[ActivityStrategy]
    protections: list[dict[str, Any]] | None = None
    # 人民币本站模拟账户在外盘图表上：美元子账户那一半的编号、币种与读取状态（ok / unavailable，外盘盘点 D）。
    foreignAccount: dict[str, Any] | None = None


def register_workbench_api(app, ctx, extensions, briefs):
    current_user = ctx.current_user
    from .strategy_explanations import register_explanations
    register_explanations(app, ctx)
    register_ui_control(app, current_user)
    from .web_push import register_push
    register_push(app, current_user)
    register_preferences(app, current_user, extensions.root.parent if extensions is not None else None)
    @app.get("/api/app/help", response_model=HelpSnapshot, response_model_exclude_none=True)
    async def app_help(request: Request, topic: str | None = Query(default=None, max_length=80)):
        """使用说明：分组、旧 id 别名与主题；topic（可用旧 id）只返回该主题。"""
        await current_user(request)
        if topic is None:
            return help_snapshot()
        resolved = resolve_topic(topic)
        if resolved is None:
            raise HTTPException(404, "没有这个帮助主题")
        return help_snapshot(resolved)

    @app.get("/api/app/activity", response_model=ActivitySnapshot)
    async def app_activity(request: Request, symbol: str = ""):
        user = await current_user(request)
        # 2026-09-23 isolation: the caller's own account (header / session /
        # grants), never the default account's book.
        account = (await ctx.account_context(request, user)).account
        try:
            async with provider_read_snapshot(account.provider):
                activity = account.provider.account_activity()
        except (RuntimeError, OSError, ValueError) as error:
            raise HTTPException(503, "操作记录暂不可用，请等待账户连接恢复") from error
        snapshot = activity_snapshot(
            app, user, symbol, activity,
            simulation_orders=account.simulation_orders,
            strategy_monitor=account.strategy_monitor,
            trading_account_id=account.account_id,
        )
        # 外盘图表：人民币本站模拟账户带上美元子账户那一半（外盘盘点 D）。
        return await with_foreign_half(app, snapshot, accounts=ctx.services.accounts, account_id=account.account_id,
                                       user=user, symbol=symbol, assemble=True)

    @app.get("/api/app/config", response_model=ConfigSnapshot)
    async def app_config(request: Request):
        return extensions.get(await current_user(request))

    @app.get("/api/app/briefs", response_model=BriefsSnapshot, response_model_exclude_none=True)
    async def app_briefs(request: Request, symbol: str | None = Query(default=None, pattern=SYMBOL_PATTERN, max_length=40)):
        """AI 简报：带 symbol 只读该合约（1.6.x 契约）；不带 symbol 合并读取全部订阅品种
        （新闻 + 异动，按时间新到旧，每条带 product/productName/symbol）。"""
        return await briefs.read(await current_user(request), symbol)

    @app.post("/api/app/briefs/refresh", response_model=BriefRefreshResult, response_model_exclude_none=True)
    async def refresh_app_briefs(request: Request, body: BriefRefresh):
        # The app-level write gate skips this route (1.6.8 client contract): a
        # tier without the skill gets the gentle `status: disabled`; only the
        # owner's global switch is the hard refusal.
        user = await current_user(request)
        skill = skill_by_id("market-briefs")
        if skill is not None and skill_block_reason(app.state.store, user, skill) == "disabled":
            raise HTTPException(403, "skill_disabled")
        return await briefs.refresh(user, body.symbol)

    @app.get("/api/app/brief-subscriptions", response_model=BriefSubscriptionsSnapshot)
    async def app_brief_subscriptions(request: Request, symbol: str | None = Query(default=None, pattern=SYMBOL_PATTERN, max_length=40)):
        """AI 简报的品种订阅：生效品种（含中文名与当前主力合约）、档位上限（null=不限）与来源
        （default=默认跟随自选/主力；user=用户自选）。可选 symbol（当前主力合约）只在既无自选、
        也未设置过订阅时用于推导默认品种。"""
        return await briefs.subscriptions(await current_user(request), symbol)

    @app.put("/api/app/brief-subscriptions", response_model=BriefSubscriptionsSnapshot)
    async def save_app_brief_subscriptions(request: Request, body: BriefSubscriptionsUpdate):
        """设置 AI 简报订阅品种（有序、去重；空列表=不订阅）。未知品种 422 brief_product_unknown，
        超出档位上限 422 brief_limit_exceeded，均整体拒绝不落库；档位调低不删除已存设置。"""
        return await briefs.save_subscriptions(await current_user(request), body.products)

    @app.put("/api/app/config", response_model=ConfigSnapshot)
    async def save_app_config(request: Request, update: ConfigUpdate):
        try:
            user = await current_user(request)
            return extensions.save(user, update.config.model_dump(), update.revision)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/app/config/rollback", response_model=ConfigSnapshot)
    async def rollback_app_config(request: Request, update: ConfigRollback):
        try:
            user = await current_user(request)
            return extensions.save(user, None, update.revision, rollback=True)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
