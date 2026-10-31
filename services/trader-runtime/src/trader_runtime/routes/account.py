"""Account, positions, activity, risk budget, instrument catalog and market reads."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..account_pnl import account_pnl, ordered_review_trades
from ..account_pool import PoolCapacityFull, PoolStartRefused
from ..account_reminders import check_account as check_account_reminders
from ..account_reminders import check_positions as check_close_reminders
from ..guest import guest_request
from ..habit_compare import habit_compare
from ..instruments import account_currency, foreign_trade_name
from ..paper_accounts import PAPER_FOREIGN_INITIAL_BALANCE, PaperEngineError, PaperProvider
from ..review_picks import review_picks
from ..risk import decimal_value, risk_budget_summary
from ..strategy_workbench import account_trade_review
from ..wiring.accounts import ACCOUNT_HEADER, account_status_payload, effective_account_role
from ..wiring.context import RuntimeContext

log = logging.getLogger("uvicorn.error.account")

CHINA = ZoneInfo("Asia/Shanghai")


class PriceAlertCreateRequest(BaseModel):
    """到价提醒 creation body: `direction` may be omitted — the Runtime infers it from the live
    quote (above the last price → 涨到, below → 跌到); `via` records which surface set it."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str = Field(min_length=1, max_length=60)
    price: Decimal
    direction: Literal["above", "below"] | None = None
    note: str = Field(default="", max_length=60)
    via: Literal["ui", "chat", "voice"] = "ui"
    session_id: str | None = Field(default=None, max_length=80, alias="sessionId")


class PlaybookRequest(BaseModel):
    """打法本 (2026-10-25): one setup — its name and the user's own entry / exit / stop rules in words; `strategyId`
    optionally links one of the user's strategies (the rules a program can check)."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=20)
    entryRules: str = Field(default="", max_length=300)
    exitRules: str = Field(default="", max_length=300)
    stopRules: str = Field(default="", max_length=200)
    strategyId: str | None = Field(default=None, max_length=80)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("交易笔记要有名字")
        return value


PLAYBOOK_MAX = 20


class TradePlaybookRequest(BaseModel):
    """A closed round trip's afterwards 打法 (GET /api/playbooks 的 id)."""
    model_config = ConfigDict(extra="forbid")

    playbookId: str = Field(min_length=1, max_length=64)


class ChartDrawingCreateRequest(BaseModel):
    """用户画线 creation body: `kind` picks the shape (画线第二版: twenty kinds, see
    chart_drawings.DRAWING_SPECS), `points` carries its coordinates (hline {price}; trendline
    {t1,p1,t2,p2}; zone {low,high}; every other kind {anchors: [{t,p},…]}), `label` is optional
    (≤12 chars, a 文字's words ≤60), `style` optional (colour, width, dash, extend, lock, …). Prices
    are visual (the client rounds to the tick) but must be finite positive numbers."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str = Field(min_length=1, max_length=60)
    kind: Literal["hline", "trendline", "zone", "ray", "extended", "hray", "vline", "channel", "polyline",
                  "rect_zone", "fib", "percent", "gann", "rect", "ellipse", "triangle", "text", "arrow",
                  "price_label", "measure"]
    points: dict[str, Any]
    label: str = Field(default="", max_length=60)
    style: dict[str, Any] | None = None


class ChartDrawingUpdateRequest(BaseModel):
    """用户画线 edit body: points, label and/or style; an omitted key stays as it was (label 空串清除;
    style is a patch — the keys it names change, a null value returns that key to the default)."""
    model_config = ConfigDict(extra="forbid")

    points: dict[str, Any] | None = None
    label: str | None = Field(default=None, max_length=60)
    style: dict[str, Any] | None = None

    @field_validator("points")
    @classmethod
    def _nonempty(cls, value):
        if value is not None and not value:
            raise ValueError("points 不能为空")
        return value


class TradeNoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    via: Literal["typed", "voice", "chat"] = "typed"

    @field_validator("text")
    @classmethod
    def _stripped(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("补充不能为空")
        return value


SOURCE_LABELS = {"manual": "手动", "card": "AI 建议", "ai": "AI 自主", "strategy": "策略", "stop": "止损"}


def journal_entry(trade: dict[str, Any]) -> dict[str, Any]:
    """One round trip of the 交易日志 in the shape the AI and the CLI read: times in Beijing time,
    the realized result on the review's basis, 来源 with its label, the reason, the note, the desk's view."""
    def local(ns: int) -> str:
        return datetime.fromtimestamp(ns / 1e9, UTC).astimezone(CHINA).isoformat(timespec="minutes")
    source = str(trade.get("source") or "manual")
    net = trade.get("net")
    note = trade.get("note") or {}
    return {"id": trade["id"], "symbol": trade["symbol"], "direction": trade["direction"], "volume": trade["volume"],
            "openTime": local(int(trade["openTime"])), "closeTime": local(int(trade["closeTime"])),
            "holdingMinutes": trade.get("holdingMinutes"), "openPrice": trade["openPrice"], "closePrice": trade["closePrice"],
            "pnl": net if net is not None else trade["gross"], "pnlBasis": "net" if net is not None else "gross",
            "source": source, "sourceLabel": trade.get("strategyName") or SOURCE_LABELS.get(source, source),
            "reason": trade.get("reason"), "note": note.get("text"),
            # The note's scope as the review and the P&L read it (#514): the lots it was written for, and
            # whether the round's lots have changed since (then the note speaks of other lots).
            "noteVolumeAtWrite": note.get("volumeAtWrite"), "noteScopeChanged": bool(note.get("scopeChanged")),
            "deskView": trade.get("deskView")}


def tqkq_account_id(username: str) -> str:
    """The stable pool-account id for one TqKq username (the 行情专用 account's key in 后台「行情源」)."""
    return f"tqkq-sim:{hashlib.sha256(username.encode()).hexdigest()[:12]}"


def store_tqkq_pool_account(
    store: Any, pool: Any, *, owner_user_id: str, username: str, password: str,
) -> str:
    """The storage half of a binding: seal the password under its owner, create
    (or refresh and reactivate) the `tqkq-pool` row with that owner and save the
    sealed credentials. A TqKq username already held by another owner is 409.
    Shared by a member's own binding and the owner's 行情专用 account (后台
    「行情源」, 2026-10-12). The password is never logged or echoed."""
    account_id = tqkq_account_id(username)
    existing = store.get_trading_account(account_id)
    if existing is not None and existing["execution_owner_user_id"] != owner_user_id:
        raise HTTPException(status_code=409, detail={"code": "account_already_bound"})
    ciphertext, nonce = pool.credential_box.seal(user_id=owner_user_id, password=password)
    store.ensure_trading_account(
        account_id, execution_owner_user_id=owner_user_id, provider_kind="tqkq-pool",
        display_name=username, sidecar_url=None, sidecar_token_file=None,
        grants=((owner_user_id, "owner"),),
    )
    store.save_tqkq_credentials(
        user_id=owner_user_id, account_id=account_id, kq_username=username,
        ciphertext=ciphertext, nonce=nonce,
    )
    return account_id


async def bind_tqkq_account(
    store: Any, pool: Any, *, user_id: str, username: str, password: str,
    kq_username: str,
) -> dict[str, object]:
    """The TqKq login on a deployment without the paper engine (M1b): store the
    sealed password on the account row with this user as owner, make it their
    default account and assemble it right away — the user just showed up. With
    the paper engine configured the login never binds (快期模拟停用交易, #1205).
    The password is never logged or echoed."""
    account_id = store_tqkq_pool_account(
        store, pool, owner_user_id=user_id, username=username, password=password)
    # The user's new sessions act on the account they just bound.
    store.set_user_default_account(user_id=user_id, account_id=account_id)
    pool.presence.touch(user_id)
    assembly = pool.account_state(account_id)
    try:
        await pool.ensure(account_id, reason="login")
        assembly = pool.account_state(account_id)
    except PoolCapacityFull:
        assembly = "queued"  # bound; the next request retries the assembly
    except PoolStartRefused as error:
        raise HTTPException(status_code=409, detail={
            "code": "tqkq_bind_failed", "reason": error.reason,
        }) from error
    return {
        "accountId": account_id,
        "displayName": username,
        "assembly": assembly,
        "kqUsername": kq_username,
    }


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    services = ctx.services
    store = services.store
    registry = services.accounts
    current_user = ctx.current_user

    @application.get("/api/accounts")
    async def account_directory(request: Request) -> dict[str, object]:
        """The accounts this user may act on — default first, then by id — and
        the account this very request resolved to. Assembled accounts are listed
        from the registry; a bound pool account is listed from its store row
        even while its child is down (its status says where it is). A disabled
        account keeps its rows but disappears from the directory."""
        user_id = await current_user(request)
        try:
            context = await ctx.account_context(request, user_id)
            current = context.account.account_id
        except HTTPException as error:
            # The directory must stay readable while the user's account is being
            # assembled or queued for capacity — the listing itself is store-based.
            if error.status_code != 503:
                raise
            # The header's explicit target stays `current` through the fallback too
            # (526-B2): while B is queued the directory must not report the default A.
            header = request.headers.get(ACCOUNT_HEADER, "").strip()
            if header:
                current = header
            else:
                fallback = store.get_user_default_account(user_id=user_id)
                current = fallback if fallback is not None else registry.default.account_id
        pool = ctx.account_pool
        paper = ctx.paper_accounts

        def manager_state(kind: str, account_id: str) -> str | None:
            """Where an on-demand account's services are (connecting / queued /
            connected / offline); static accounts carry no such field."""
            if kind == "tqkq-pool":
                return pool.account_state(account_id) if pool is not None else "offline"
            if kind == "paper":
                return paper.account_state(account_id) if paper is not None else "offline"
            return None

        items = []
        for services_for_account in registry.all():
            account_id = services_for_account.account_id
            # The effective verdict (grant row or the single-tenant IdP fallback
            # the request above already resolved through): a cloud-jwt user on
            # the shared default account sees it in the directory as operator,
            # exactly as before the 2026-09-23 isolation removed the store-level
            # fallback from account_role.
            role = effective_account_role(store, account_id=account_id, user_id=user_id)
            if role is None:
                continue
            record = store.get_trading_account(account_id)
            if record is None:
                continue
            provider_kind = str(record["provider_kind"])
            if services_for_account.spec is not None and provider_kind not in ("tqkq-pool", "paper"):
                # An on-demand account's spec is an assembly detail (a sidecar
                # URL); what the directory names is how the account is managed.
                provider_kind = services_for_account.spec.provider_kind
            if paper is not None and provider_kind == "tqkq-sidecar" and account_id != current:
                # 快期只管行情 (owner 2026-10-31): with 本站模拟 on, the static 快期 main account is a
                # market source, not a trading account; a session still sitting on it lists it as current.
                continue
            items.append({
                **account_status_payload(
                    account_id=account_id, record=record, role=role,
                    execution_owner=store.is_execution_owner(account_id=account_id, user_id=user_id),
                    pool_status=manager_state(provider_kind, account_id),
                ),
                "providerKind": provider_kind,
                "isDefault": account_id == registry.default.account_id,
                "status": record["status"],
            })
        assembled = {services_for_account.account_id for services_for_account in registry.all()}
        for record in store.list_trading_accounts():
            account_id = str(record["id"])
            if (account_id in assembled or record["status"] != "active"
                    or record["provider_kind"] not in ("tqkq-pool", "paper")):
                continue
            role = effective_account_role(store, account_id=account_id, user_id=user_id)
            if role is None:
                continue
            items.append({
                **account_status_payload(
                    account_id=account_id, record=record, role=role,
                    execution_owner=store.is_execution_owner(account_id=account_id, user_id=user_id),
                    pool_status=manager_state(str(record["provider_kind"]), account_id),
                ),
                "providerKind": str(record["provider_kind"]),
                "isDefault": False,
                "status": record["status"],
            })
        return {"accounts": items, "current": current}

    @application.post("/api/accounts/open-paper")
    async def open_paper_account(request: Request) -> dict[str, object]:
        """开通本站模拟账户 (2026-10-03): any signed-in user — the owner included —
        opens their one site-hosted paper account (initial 1,000,000), or gets the
        existing one back. The default account changes only when they had none; the
        account selector switches to it."""
        user_id = await current_user(request)
        manager = ctx.paper_accounts
        if manager is None:
            raise HTTPException(status_code=503, detail="paper_engine_unavailable")
        try:
            record = await manager.ensure_paper_account(user_id, make_default=False)
        except PaperEngineError as error:
            raise HTTPException(status_code=503, detail="paper_engine_unavailable") from error
        return {"accountId": str(record["id"]), "displayName": str(record["display_name"]),
                "providerKind": "paper",
                "isDefault": store.get_user_default_account(user_id=user_id) == str(record["id"])}

    async def tqkq_trading_retired(body: dict[str, Any] | None = Body(default=None)) -> None:
        raise HTTPException(status_code=410, detail={"code": "tqkq_trading_retired"})

    # 快期模拟停用交易（所有者 2026-10-30，#1205）：绑定与解绑快期交易账户已下线，账户由启动时的
    # tqkq_retirement 一次收尾。路由只为已装的旧桌面客户端回 410，不进能力目录（agent_api）；
    # shortcut: kept only for the old-client contract baseline — delete in the next version prep, which regenerates it.
    for path in ("/api/accounts/bind-tqkq", "/api/accounts/unbind-tqkq"):
        application.add_api_route(path, tqkq_trading_retired, methods=["POST"], deprecated=True,
                                  name=f"retired_{path.rsplit('/', 1)[-1].replace('-', '_')}")

    @application.get("/api/account/summary")
    async def get_account_summary(request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        try:
            summary = context.account.provider.account_summary("")
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        # 风险度提醒 (2026-10-03)：读账户的顺风车，绝不影响这次读取的返回。
        await check_account_reminders(application, user_id=user_id,
                                      account_id=context.account.account_id, summary=summary)
        return summary

    async def review_context(request: Request, user_id: str, currency: str | None):
        """A review ledger is independent of the account the conversation trades on."""
        context = await ctx.account_context(request, user_id)
        if currency is None or account_currency(context.account.account_id) == currency:
            return context
        foreign_of = ctx.services.accounts.foreign_account_of
        foreign_id = foreign_of(context.account.account_id) if currency == "USD" and foreign_of else None
        if foreign_id is None:
            raise HTTPException(409, "review_currency_unavailable")
        # Reuse the declared relationship and re-check grants. Never guess an id
        # from a currency or switch the conversation's execution account.
        return await ctx.account_context(request, user_id, account_id=foreign_id)

    @application.get("/api/account/pnl")
    async def get_account_pnl(request: Request, months: int = Query(default=12, ge=1, le=24),
                              currency: Literal["CNY", "USD"] | None = Query(default=None)) -> dict[str, object]:
        """账户复盘的近366天历史与权益；currency可选人民币/美元账本，不切交易账户。

        trades按平仓时间倒序、同时间id升序，与复盘页一致；选择记录用其真实id。
        当日activity为空不能据此断言本历史为空。
        """
        user_id = await current_user(request)
        context = await review_context(request, user_id, currency)
        try:
            review = await account_trade_review(
                store, context.account.provider, application.state.fee_settings, user_id,
                symbol=None, days=366, trading_account_id=context.account.account_id,
                desk=ctx.services.research_desk)
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail="成交或合约参数尚不可用") from error
        preferences = application.state.preference_store.get(user_id)["preferences"] or {}
        # 本金：设置里的“本金”是人民币账户的；外盘美元子账户按开户的 10 万美元算（2026-10-21：以前把人民币本金
        # 套在美元账上，总盈亏成了约 -90 万）。
        ledger_currency = account_currency(context.account.account_id)
        principal = PAPER_FOREIGN_INITIAL_BALANCE if ledger_currency == "USD" else preferences.get("initialBalance")
        return {**account_pnl(review, daily=store.list_account_daily(
            trading_account_id=context.account.account_id), initial_balance=principal,
            months=months), "currency": ledger_currency, "breakdown": review.get("breakdown")}

    async def _review(request: Request, user_id: str, *, days: int, symbol: str | None = None,
                      currency: str | None = None) -> tuple[Any, dict]:
        context = await review_context(request, user_id, currency)
        try:
            return context, await account_trade_review(
                store, context.account.provider, application.state.fee_settings, user_id,
                symbol=symbol, days=days, trading_account_id=context.account.account_id,
                desk=ctx.services.research_desk)
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail="成交或合约参数尚不可用") from error

    @application.get("/api/account/journal")
    async def get_trade_journal(request: Request, days: int = Query(default=30, ge=1, le=366),
                                symbol: str | None = Query(default=None, max_length=40),
                                limit: int = Query(default=50, ge=1, le=200),
                                currency: Literal["CNY", "USD"] | None = Query(default=None)) -> dict[str, object]:
        """交易日志: the account's closed round trips, newest first, each with who placed it (来源), why
        (the AI card's or AI order's reason, the strategy's name), the user's 补充 and what the research
        desk said about the product when it was opened. currency selects this account's CNY/USD
        review ledger without changing the chat's trading account; omitted preserves the request's
        account. days bounds history (366 matches the review page), not today's activity. Same-time
        trades use id ascending before limit is applied. Read-only."""
        user_id = await current_user(request)
        context, review = await _review(request, user_id, days=days, symbol=symbol, currency=currency)
        trades = ordered_review_trades(review["trades"])
        return {"trades": [journal_entry(trade) for trade in trades[:limit]], "total": len(trades),
                "currency": account_currency(context.account.account_id), "days": days,
                # 补充 whose round no longer exists (replaced by real fills), as the review and the P&L list them.
                "unlinkedNotes": review.get("unlinkedNotes") or [],
                "notice": "盈亏按账户回报口径；“当时的调研判断”只覆盖判断留档（2026-09-29）之后开仓的交易。"}

    @application.get("/api/account/review-picks")
    async def get_review_picks(request: Request, scope: Literal["day", "week"] = Query(default="day")) -> dict[str, object]:
        """引导复盘: up to three closed trades worth one question each (the biggest loss, trades against the
        research desk's stance that day, losers held clearly longer than the user's winners), trades that
        already carry a 补充 skipped, each with the question to ask. `day` is the latest trading day with
        closes in the last three days, `week` the last seven days. Read-only."""
        user_id = await current_user(request)
        context, review = await _review(request, user_id, days=30)

        def names() -> dict[str, str]:
            try:
                items = context.account.provider.instrument_catalog().get("items") or []
            except (RuntimeError, KeyError, TypeError, ValueError):
                return {}
            wanted = {trade["symbol"] for trade in review["trades"]}
            return {str(item["symbol"]): re.sub(r"\d+$", "", str(item.get("instrumentName") or "")) or str(item["symbol"])
                    for item in items if item.get("symbol") in wanted}
        return review_picks(review["trades"], scope=scope, now_ns=time.time_ns(), names=await asyncio.to_thread(names))

    @application.get("/api/account/habits")
    async def get_habit_compare(request: Request,
                                currency: Literal["CNY", "USD"] | None = Query(default=None)) -> dict[str, object]:
        """习惯对照: the last 90 days' closed round trips against only the 计划内 ones (opened by a strategy, a stop,
        the AI's own order, or a confirmed AI card with no reminder before it). The gap is split exactly by the
        计划外 trades' open tag (提醒后 / 临时); `holding` holds two estimates not added to it (losers held past the
        longest 计划内 loser, winners closed before the shortest 计划内 winner); `picks` the five trades that move
        the comparison most. Fewer than 5 计划内 trades: status insufficient. currency picks the CNY/USD review
        ledger without switching the chat's trading account. Read-only."""
        user_id = await current_user(request)
        context, review = await _review(request, user_id, days=90, currency=currency)
        result = habit_compare(review["trades"], basis="gross" if review.get("costSource") == "account" else "net")
        return {**result, "days": 90, "currency": account_currency(context.account.account_id)}

    @application.put("/api/account/trade-notes/{round_id}")
    async def put_trade_note(request: Request, round_id: str, body: TradeNoteRequest) -> dict[str, object]:
        """复盘补写: one line on a closed round trip (id from the journal or the review), replacing any
        earlier one. `via` records whether it was typed, spoken in the voice review, or written by the AI
        in chat for the user."""
        user_id = await current_user(request)
        context, review = await _review(request, user_id, days=366)
        round_trip = next((trade for trade in review["trades"] if trade["id"] == round_id), None)
        if round_trip is None:
            raise HTTPException(status_code=404, detail="没有这笔平仓记录")
        # 记下写入时的手数（#514-2）：之后同一回合 ID 的手数变了，复盘读回时标“范围已变”。
        note = await asyncio.to_thread(store.set_trade_note, round_id, body.text, via=body.via, user_id=user_id,
                                       volume_at_write=int(round_trip["volume"]),
                                       trading_account_id=context.account.account_id)
        return {"roundId": round_id, "note": note}

    @application.delete("/api/account/trade-notes/{round_id}")
    async def delete_trade_note(request: Request, round_id: str) -> dict[str, object]:
        """Remove the 补充 of a round trip."""
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        deleted = await asyncio.to_thread(store.delete_trade_note, round_id,
                                          trading_account_id=context.account.account_id)
        return {"roundId": round_id, "deleted": deleted}

    @application.put("/api/account/trade-playbooks/{round_id}")
    async def put_trade_playbook(request: Request, round_id: str, body: TradePlaybookRequest) -> dict[str, object]:
        """打法本 (2026-10-25): tag a closed round trip with one of the user's setups afterwards; it overrides the
        setup the opening card carried, in the review, its 分组 table and the memory statistics."""
        user_id = await current_user(request)
        playbook = store.get_playbook(body.playbookId, user_id=user_id)
        if playbook is None:
            raise HTTPException(status_code=422, detail="playbook_not_found")
        context, review = await _review(request, user_id, days=366)
        if not any(trade["id"] == round_id for trade in review["trades"]):
            raise HTTPException(status_code=404, detail="没有这笔平仓记录")
        await asyncio.to_thread(store.set_round_playbook, round_id, body.playbookId, user_id=user_id,
                                trading_account_id=context.account.account_id)
        return {"roundId": round_id, "playbookId": playbook["id"], "playbookName": playbook["name"]}

    @application.delete("/api/account/trade-playbooks/{round_id}")
    async def delete_trade_playbook(request: Request, round_id: str) -> dict[str, object]:
        """Remove a round trip's afterwards tag (the card's own setup, if any, shows again)."""
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        cleared = await asyncio.to_thread(store.clear_round_playbook, round_id,
                                          trading_account_id=context.account.account_id)
        return {"roundId": round_id, "cleared": cleared}

    @application.get("/api/positions")
    async def get_positions(request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        projection: dict[str, Any] = {}
        if isinstance(context.account.provider, PaperProvider):
            projection = await context.account.provider.positions_view()
            items = projection.pop("items")
        else:
            try:
                items = context.account.provider.positions()
            except RuntimeError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
        # 交割提醒 (#932) is judged by the Runtime's own 30 s account loop (AccountReminderLoop), not here.
        # 收盘前提醒 (2026-10-03)：同一处顺风车，按各合约自己的交易时段只提醒一次。#458：
        # 新鲜与否看同一账户的账户快照——这里单读一次；读失败只意味着不提醒，不影响本接口。
        try:
            reminder_summary = context.account.provider.account_summary("")
        except Exception:
            reminder_summary = None
        await check_close_reminders(application, user_id=user_id,
                                    account_id=context.account.account_id, positions=items,
                                    summary=reminder_summary)
        # 外盘月份合约带中文名（CME.GCEZ26 → COMEX黄金2612，与卡片同一处 foreign_trade_name）：人民币账户的
        # 持仓面板并进美元子账户的行时，它自己的合约目录里没有这些月份（2026-10-21）。
        named = [{**item, "instrumentName": name}
                 if isinstance(item, dict) and (name := foreign_trade_name(str(item.get("symbol") or ""))) else item
                 for item in items]
        return {"mode": context.account.provider.mode, "items": named, **projection}

    @application.get("/api/account/activity")
    async def get_account_activity(request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        try:
            return await asyncio.to_thread(context.account.provider.account_activity)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/api/risk/summary")
    async def get_risk_summary(request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        try:
            account = context.account.provider.account_summary("")
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        data_status = str(account.get("dataStatus") or "unavailable")
        today_pnl = (
            decimal_value(account["todayNetPnl"])
            if data_status in {"normal", "simulated"}
            else None
        )
        try:
            return risk_budget_summary(
                store.active_risk_policy(user_id=user_id),
                today_pnl=today_pnl,
                data_status=data_status,
                now=(context.account.simulation_orders.trading_now()
                     if context.account.simulation_orders is not None else None),
            )
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=500, detail="invalid_risk_policy") from error

    @application.get("/api/instruments")
    async def get_instruments(
        request: Request,
        exchange: str | None = None,
        product: str | None = None,
        main_only: bool = Query(default=False, alias="mainOnly"),
        search: str | None = Query(default=None, max_length=40),
    ) -> dict[str, object]:
        await current_user(request)
        try:
            result = (await asyncio.to_thread(registry.default.provider.instrument_catalog)
                      if getattr(application.state, "replay_context", None) is not None
                      else await application.state.shared_market.instruments())
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        items = result.get("items", [])
        exchange_filter = (exchange or "").strip().upper()
        product_filter = (product or "").strip().lower()
        search_filter = (search or "").strip().lower()

        def included(item: dict[str, Any]) -> bool:
            if exchange_filter and str(item.get("exchangeId", "")).upper() != exchange_filter:
                return False
            if product_filter and str(item.get("productId", "")).lower() != product_filter:
                return False
            if main_only and item.get("isMain") is not True:
                return False
            if search_filter:
                searchable = " ".join(str(item.get(key, "")) for key in (
                    "symbol", "instrumentId", "instrumentName", "productId",
                )).lower()
                if search_filter not in searchable:
                    return False
            return True

        filtered = [item for item in items if isinstance(item, dict) and included(item)]
        return {**result, "items": filtered, "count": len(filtered)}

    @application.get("/api/market/board")
    async def get_market_board(request: Request) -> dict[str, object]:
        # 报价总表 is a member surface; the guest page shows no entry for it.
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_board_unavailable")
        await current_user(request)
        if getattr(application.state, "replay_context", None) is not None:
            raise HTTPException(status_code=503, detail="market_board_unavailable")
        try:
            return await application.state.shared_market.board()
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/api/market/foreign")
    async def get_market_foreign(request: Request) -> dict[str, object]:
        # 外盘 (2026-10-18, #976): the board's 外盘 group — every foreign product's main series, each row whole
        # from one source (`source`), read from the one foreign entry. A member surface like the board; for
        # watching and research only, never an order basis.
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_board_unavailable")
        await current_user(request)
        if getattr(application.state, "replay_context", None) is not None:
            raise HTTPException(status_code=503, detail="market_foreign_unavailable")
        return await services.foreign_market.board()

    @application.get("/api/market/quotes")
    async def get_market_quotes(request: Request, symbols: str = Query(min_length=1, max_length=900)) -> dict[str, object]:
        # 只订报价的补充行 (2026-10-09, 手机报价列表): quote-only rows for contracts the board does not
        # carry (a held non-main month), ≤ 20 per read. Each read renews a 30 s per-symbol lease on the
        # shared extras channel (cap MAX_EXTRA_QUOTES); a symbol no longer asked for lapses by itself,
        # and a capped one answers its own row with errorCode market_subscription_limit.
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_board_unavailable")
        await current_user(request)
        if getattr(application.state, "replay_context", None) is not None:
            raise HTTPException(status_code=503, detail="market_quotes_unavailable")
        wanted = [item.strip() for item in symbols.split(",") if item.strip()]
        if not wanted or len(wanted) > 20:
            raise HTTPException(status_code=422, detail="market_quotes_symbols_1_to_20")
        # 美元子账户的外盘月份（CME.GCEZ26）不在快期通道里：读 MT5 月份报价（与撮合同一份价格），按行并回。
        # MT5 不通时这些行本轮缺席，客户端照常标“本次未取到报价”。
        months = [symbol for symbol in wanted if symbol.upper().startswith("CME.")]
        domestic = [symbol for symbol in wanted if symbol not in months]
        try:
            answer = await application.state.shared_market.quotes(domestic)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if months:
            try:
                foreign = await asyncio.to_thread(services.foreign_market.month_quotes, months)
            except Exception:  # noqa: BLE001 — MT5 down or not configured
                foreign = {}
            answer = {**answer, "rows": [*answer.get("rows", []),
                                         *({**row, "symbol": symbol} for symbol, row in foreign.items())]}
        return answer

    @application.get("/api/market/calendar")
    async def get_market_calendar(request: Request) -> dict[str, object]:
        # 交易日历 (2026-10-03): the replay dialog's default day and the 休市 refusal.
        # The calendar itself is public — unlike the board there is no guest refusal;
        # a replay window has no live calendar, same as the alerts.
        await current_user(request)
        if getattr(application.state, "replay_context", None) is not None:
            raise HTTPException(status_code=503, detail="market_calendar_replay_unavailable")
        try:
            return await application.state.shared_market.calendar()
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/api/market/alerts")
    async def list_price_alerts(request: Request) -> dict[str, object]:
        # 到价提醒 is a member surface with no guest entry: the quote is public, the alerts are the
        # user's own. A replay window lists its own alerts — they live in the replay child's store
        # and vanish with the replay (2026-10-03).
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_alerts_unavailable")
        user_id = await current_user(request)
        return store.list_price_alerts(user_id=user_id)

    @application.post("/api/market/alerts", status_code=201)
    async def create_price_alert(payload: PriceAlertCreateRequest, request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_alerts_unavailable")
        user_id = await current_user(request)
        from ..price_alerts import create_alert
        try:
            alert = await create_alert(application, user_id=user_id, symbol=payload.symbol,
                                       price=payload.price, direction=payload.direction,
                                       note=payload.note, via=payload.via, session_id=payload.session_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"alert": alert}

    @application.delete("/api/market/alerts/{alert_id}")
    async def cancel_price_alert(alert_id: str, request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_alerts_unavailable")
        user_id = await current_user(request)
        cancelled = store.cancel_price_alert(user_id=user_id, alert_id=alert_id)
        if cancelled is None:
            raise HTTPException(status_code=404, detail="price_alert_not_found")
        if isinstance(cancelled, str):  # triggered: archived history, said in Chinese (#932)
            raise HTTPException(status_code=409, detail=cancelled)
        return {"alert": cancelled}

    # 打法本 (2026-10-25): the user's own setups. An order placed under one (the ticket's 打法, a card's
    # playbookId) carries it into the trade review's round trips. Members only.
    def playbook_fields(payload: PlaybookRequest, user_id: str) -> dict[str, Any]:
        fields = payload.model_dump()
        for key in ("entryRules", "exitRules", "stopRules"):
            fields[key] = fields[key].strip()
        if fields.get("strategyId"):
            try:
                store.get_strategy_draft(fields["strategyId"], user_id=user_id)
            except KeyError:
                raise HTTPException(status_code=422, detail="关联的策略不存在或不是你的") from None
        return fields

    @application.get("/api/playbooks")
    async def list_playbooks(request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_playbooks_unavailable")
        user_id = await current_user(request)
        return {"playbooks": store.list_playbooks(user_id=user_id), "max": PLAYBOOK_MAX}

    @application.post("/api/playbooks", status_code=201)
    async def create_playbook(payload: PlaybookRequest, request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_playbooks_unavailable")
        user_id = await current_user(request)
        created = store.create_playbook(user_id=user_id, fields=playbook_fields(payload, user_id), max_count=PLAYBOOK_MAX)
        if isinstance(created, str):
            raise HTTPException(status_code=409, detail=created)
        return {"playbook": created}

    @application.put("/api/playbooks/{playbook_id}")
    async def update_playbook(playbook_id: str, payload: PlaybookRequest, request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_playbooks_unavailable")
        user_id = await current_user(request)
        updated = store.update_playbook(playbook_id, user_id=user_id, fields=playbook_fields(payload, user_id))
        if updated is None:
            raise HTTPException(status_code=404, detail="playbook_not_found")
        if isinstance(updated, str):
            raise HTTPException(status_code=409, detail=updated)
        return {"playbook": updated}

    @application.delete("/api/playbooks/{playbook_id}")
    async def delete_playbook(playbook_id: str, request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_playbooks_unavailable")
        user_id = await current_user(request)
        if not store.delete_playbook(playbook_id, user_id=user_id):
            raise HTTPException(status_code=404, detail="playbook_not_found")
        return {"deleted": playbook_id}

    # 用户画线 (2026-10-02): the user's own chart lines, stored per user × contract so they
    # survive a refresh and follow the user across devices. Like the alerts, guests have none;
    # a replay window keeps its own lines in the replay child's store (2026-10-03) — drawing a
    # horizontal line and turning it into an alert works on the replay chart too. Registered
    # before the /api/market/{symbol} catch-all so the path is not swallowed by it.
    @application.get("/api/market/drawings")
    async def list_chart_drawings(
        request: Request, symbol: str | None = Query(default=None, max_length=60),
    ) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_drawings_unavailable")
        user_id = await current_user(request)
        return {"drawings": store.list_chart_drawings(user_id=user_id, symbol=symbol)}

    async def drawings_changed(user_id: str) -> None:
        """画线第二版 (2026-10-09): every attached window of the user re-reads its lines — a line the AI
        or another device drew shows at once (the `feed.ui` nudge with reason "drawings"; an older client
        only pulses)."""
        feed = getattr(application.state, "runtime_feed", None)
        if feed is None:
            return
        try:
            await feed.publish_ui_nudge(user_id, "drawings")
        except Exception as error:  # the write stands; the next read shows it
            logging.getLogger(__name__).debug("drawings nudge not published: %s", error)

    @application.post("/api/market/drawings", status_code=201)
    async def create_chart_drawing(payload: ChartDrawingCreateRequest, request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_drawings_unavailable")
        user_id = await current_user(request)
        from ..chart_drawings import create_drawing
        try:
            drawing = await create_drawing(application, user_id=user_id, symbol=payload.symbol,
                                           kind=payload.kind, points=payload.points,
                                           label=payload.label, style=payload.style)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        await drawings_changed(user_id)
        return {"drawing": drawing}

    @application.put("/api/market/drawings/{drawing_id}")
    async def update_chart_drawing(drawing_id: str, payload: ChartDrawingUpdateRequest, request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_drawings_unavailable")
        user_id = await current_user(request)
        from ..chart_drawings import check_label, parse_points, parse_style
        existing = store.get_chart_drawing(user_id=user_id, drawing_id=drawing_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="chart_drawing_not_found")
        kind = str(existing["kind"])
        try:
            points = parse_points(kind, payload.points) if payload.points is not None else None
            label = check_label(payload.label, kind) if payload.label is not None else None
            style = parse_style(payload.style, base=existing.get("style") or {})
        except ValueError as error:
            # 与创建同一条 422：改一条线也不让无效参数变成 500（#521 R4）。
            raise HTTPException(status_code=422, detail=str(error)) from error
        # 画线第二版: a locked line keeps its place — moving it (points) is refused until it is
        # unlocked; style and label still change (the lock itself is a style key).
        unlocking = (payload.style or {}).get("locked") is False
        if points is not None and (existing.get("style") or {}).get("locked") and not unlocking:
            raise HTTPException(status_code=409, detail="这条线已锁定，先解锁再移动")
        updated = store.update_chart_drawing(
            user_id=user_id, drawing_id=drawing_id, points=points, label=label,
            style=style if payload.style is not None else None)
        if updated is None:
            raise HTTPException(status_code=404, detail="chart_drawing_not_found")
        await drawings_changed(user_id)
        return {"drawing": updated}

    @application.delete("/api/market/drawings/{drawing_id}")
    async def delete_chart_drawing(drawing_id: str, request: Request) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_drawings_unavailable")
        user_id = await current_user(request)
        deleted = store.delete_chart_drawing(user_id=user_id, drawing_id=drawing_id)
        if deleted is None:
            raise HTTPException(status_code=404, detail="chart_drawing_not_found")
        await drawings_changed(user_id)
        return {"drawing": deleted}

    @application.delete("/api/market/drawings")
    async def clear_chart_drawings(request: Request, symbol: str = Query(max_length=60)) -> dict[str, object]:
        if guest_request(request):
            raise HTTPException(status_code=403, detail="guest_drawings_unavailable")
        user_id = await current_user(request)
        cleared = store.clear_chart_drawings(user_id=user_id, symbol=symbol)
        if cleared:
            await drawings_changed(user_id)
        return {"cleared": cleared}

    @application.get("/api/market/{symbol}")
    async def get_market(request: Request, symbol: str) -> dict[str, object]:
        await current_user(request)
        try:
            if getattr(application.state, "replay_context", None) is not None:
                return await asyncio.to_thread(registry.default.provider.market_snapshot, "", symbol)
            return await application.state.shared_market.snapshot(symbol)
        except KeyError as error:
            raise HTTPException(404, "symbol_not_found") from error
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        except HTTPException as error:
            if error.detail == "guest_unknown_symbol":
                raise HTTPException(404, "symbol_not_found") from error
            raise

    @application.get("/api/markets/{symbol}/workbench")
    async def get_market_workbench(
        request: Request,
        symbol: str,
        duration: int = Query(default=300),
        since: int | None = Query(default=None, ge=0),
    ) -> dict[str, object]:
        """`since` (本地 K 线缓存, 2026-10-12): the datetime of the last closed bar the reader already holds;
        the answer then carries only the bars after it with `barsAfter` and `barsWindow` (see
        shared_market.workbench_since). A replay always answers the whole workbench."""
        await current_user(request)
        if duration not in {60, 180, 300, 900, 1800, 3600, 86400, 604800}:
            raise HTTPException(status_code=422, detail="invalid_market_duration")
        try:
            if getattr(application.state, "replay_context", None) is not None:
                return await asyncio.to_thread(registry.default.provider.market_workbench, symbol, duration)
            if since is not None:
                return await application.state.shared_market.workbench_since(symbol, duration, since)
            return (await application.state.shared_market.market(symbol, duration))["workbench"]
        except KeyError as error:
            raise HTTPException(404, "symbol_not_found") from error
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        except HTTPException as error:
            if error.detail == "guest_unknown_symbol":
                raise HTTPException(404, "symbol_not_found") from error
            raise
