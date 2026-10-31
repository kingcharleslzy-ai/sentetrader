"""Research data (AKShare), news and the research desk: read-only, never on the market path."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..battle_sheet import BattleSheet
from ..research_data import DATASETS
from ..research_debate import DebateUnavailable, EventProductError
from ..research_live import FLASH_INCLUDE_MAX, FLASH_OFFICIAL_HOURS_MAX
from ..skill_registry import skill_allowed, skill_by_id
from ..wiring.context import RuntimeContext


class DebateStart(BaseModel):
    """发起多 AI 研讨: the product (any contract of it, e.g. SHFE.rb2701), or a news event's id — its product comes
    from the event (a `symbol` with it picks among the event's products and must be one of them)."""
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, min_length=3, max_length=40)
    eventId: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9:_.-]+$")
    force: bool = False


DEBATE_MESSAGES = {
    "research_debate_unknown": (404, "没有这次研讨"),
    "research_debate_event_unknown": (404, "近 24 小时的快讯事件里没有这一条，刷新快讯后再试"),
    "research_debate_event_product": (422, "这条快讯不是关于这个品种的"),
    "research_debate_event_ambiguous": (409, "这条快讯涉及几个品种，请指明研讨哪一个"),
    "research_debate_not_done": (409, "这次研讨还没有结论，不能写进今日判断"),
    "research_debate_not_today": (409, "只能把今天的研讨写进今日判断"),
    "research_debate_unanchored": (422, "研讨没有采纳带引用的论点，不能写进今日判断"),
    "judgment_write_conflict": (409, "同一时刻另一条判断刚写入，请再点一次"),
    "research_debate_forbidden": (403, "当前档位未开通「联网研究」，不能发起研讨"),
    "research_debate_not_yours": (403, "只有发起这场研讨的人可以取消"),
    "research_symbol_invalid": (422, "合约代码不对"),
    "research_symbol_unknown": (422, "合约目录里没有这个品种"),
}


def debate_error(code: str, message: str | None = None, status: int | None = None, **extra: Any) -> HTTPException:
    known = DEBATE_MESSAGES.get(code, (422, code))
    return HTTPException(status_code=status or known[0], detail={"code": code, "message": message or known[1], **extra})


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    research = ctx.services.research
    current_user = ctx.current_user

    @application.get("/api/research/summary")
    async def research_summary(request: Request, symbol: str = Query(min_length=3, max_length=40)) -> dict[str, object]:
        await current_user(request)
        try:
            return await asyncio.to_thread(research.summary, symbol)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.get("/api/research/news")
    async def research_news(request: Request, symbol: str = Query(min_length=3, max_length=40)) -> dict[str, object]:
        await current_user(request)
        try:
            return await ctx.services.research_brief.brief(symbol, "news")
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    def desk_generator(user_id: str) -> object | None:
        """A reader whose tier includes web research may start a judgment with their own
        model adapter; everyone else reads the shared judgment that already exists."""
        skill = skill_by_id("web-research")
        if skill is None or not skill_allowed(ctx.services.store, user_id, skill):
            return None
        if ctx.services.research_desk.fixed_generator is not None:
            return ctx.services.research_desk.fixed_generator  # browser tests: the recorded fake model
        adapter = application.state.adapter
        return adapter.adapter_for_user(user_id) if hasattr(adapter, "adapter_for_user") else adapter

    ctx.services.research_desk.generator_for = desk_generator
    # News screening and 今日新闻要点 are shared platform work: the Runtime's own adapter, booked to the
    # platform markers (unless the browser-test runtime already gave them its recorded fake model).
    for worker in (ctx.services.research_live.screener, ctx.services.research_live.analyst,
                   ctx.services.research_live.translator):
        if worker is not None and worker.generator is None:
            worker.generator = lambda: application.state.adapter

    @application.get("/api/research/desk")
    async def research_desk(request: Request, symbol: str = Query(min_length=3, max_length=40)) -> dict[str, object]:
        """调研汇总读取。注意（529-A2c，能力目录标为非只读）：当天判断还没有时，
        这次读取会启动一次模型生成，用量记在平台账 platform:research-desk，不占读者额度；
        已有现成判断才是纯读取。"""
        user_id = await current_user(request)
        try:
            # The panel's read never waits for a collection (2026-10-22): see research_desk.FIRST_READ_WAIT_SECONDS.
            return await ctx.services.research_desk.report(symbol, generator=desk_generator(user_id), user_id=user_id,
                                                            fast=True)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    sheet = BattleSheet(ctx.services.research_desk)

    @application.get("/api/research/sheet")
    async def research_sheet(request: Request) -> dict[str, object]:
        """今日作战表：自选品种每个一行，按优先级排好（关键价位、消息强弱、可能出现的打法）。
        纯读取：程序从行情和已采集的快讯算，不调模型、不读账户。"""
        user_id = await current_user(request)
        store = ctx.services.store
        products = list(store.user_preferences(user_id).watchlist.products)
        return await sheet.sheet(products, store.list_playbooks(user_id=user_id))

    @application.post("/api/research/desk/refresh")
    async def research_desk_refresh(request: Request, symbol: str = Query(min_length=3, max_length=40)) -> dict[str, object]:
        """重新判断 / 按最新消息重新判断: re-reads the facts now (at most once a minute); the judgment is
        regenerated only when it is at least an hour old (or none exists today), within the day's cap.
        `rejudge` says what happened: started / running / limited (with `availableAt` and the reason) /
        unavailable / current."""
        user_id = await current_user(request)
        try:
            return await ctx.services.research_desk.report(symbol, generator=desk_generator(user_id), force=True,
                                                            user_id=user_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    # 多 AI 研讨 (2026-10-11): the model adapter is resolved per reader exactly like the desk's (`desk_generator`:
    # the recorded fake in browser tests, else `adapter_for_user` — in cloud mode application.state.adapter is a
    # SessionAgentRouter, not a model adapter); tokens still book to platform:research-debate. Who may start one
    # is the same rule (a tier with web research). Progress reaches the user's windows as feed.ui nudges
    # (reason research-debate), a turn's text while it is written as feed.debate, and the end as one reminder (#932: 研讨完成 is an 信息 kind — the 提醒中心 row,
    # feed.reminder, the push; never a conversation message; its own switch notify.researchDebate).
    debates = ctx.services.research_debate

    async def debate_notify(user_id: str, event: dict[str, Any]) -> None:
        broker = application.state.broker
        for session_id in broker.subscribed_session_ids():
            try:
                if str(ctx.services.store.get_session(session_id).get("userId") or "") != user_id:
                    continue
            except KeyError:
                continue
            if event.get("kind") == "delta":
                # A turn's or an analyst's text so far, as it is written (like chat's message.delta; the full text each time).
                who = {"analyst": str(event["analyst"])} if event.get("analyst") else {"turn": int(event["turn"]), "side": str(event["side"])}
                await broker.publish_transient(session_id, kind="result", name="feed.debate", payload={
                    "id": str(event["id"]), **who, "text": str(event["text"])})
            else:
                await broker.publish_transient(session_id, kind="result", name="feed.ui", payload={"reason": "research-debate"})
        if event.get("kind") == "done":
            from ..reminders import ReminderSpec, deliver
            from ..web_push import PushMessage, deep_link
            symbol = str(event.get("symbol") or "")
            await deliver(application, ReminderSpec(
                kind="research_debate", user_id=user_id, dedupe_key=str(event["id"]), title=str(event["title"]),
                body=str(event["text"]), symbol=symbol, name=str(event.get("name") or ""),
                at=datetime.now(UTC).isoformat(timespec="seconds"),
                target={"type": "research", "debateId": str(event["id"]), **({"symbol": symbol} if symbol else {})},
                feed_extra={"debateId": str(event["id"])},
                push=PushMessage(kind="research_debate", title=str(event["title"]), body=str(event["text"]),
                                 short=str(event["title"]), tag=f"debate-{event['id']}", url=deep_link("research"))))
    debates.notify = debate_notify

    def debate_allowed(user_id: str) -> object:
        generator = desk_generator(user_id)
        if generator is None:
            raise debate_error("research_debate_forbidden")
        return generator

    @application.get("/api/research/debates")
    async def research_debates(request: Request, product: str | None = Query(default=None, min_length=3, max_length=40),
                               events: str | None = Query(default=None, max_length=1200),
                               day: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                               limit: int = Query(default=20, ge=1, le=100)) -> dict[str, object]:
        """多 AI 研讨 list (read-only): summaries newest first — of one product (SHFE.rb), of news events
        (`events`, comma-separated event ids: the 快讯 rows' 已研讨 tags) or of a research day — with `stats`
        (研讨战绩: settled count; per-participant hits only once 30 are settled)."""
        await current_user(request)
        wanted = [item.strip() for item in events.split(",") if item.strip()][:60] if events else None
        try:
            return await asyncio.to_thread(debates.listing, product=product, events=wanted, day=day, limit=limit)
        except ValueError as error:
            raise debate_error(str(error)) from error

    @application.post("/api/research/debates")
    async def research_debate_start(request: Request, body: DebateStart) -> dict[str, object]:
        """发起多 AI 研讨 (or reuse one): on the product's 今日议题, or on a 快讯 event (`eventId`). A cached debate
        (same topic, product and facts; an event once per product and research day) comes back with `reused`
        true; `force` (再辩一轮) always runs again. Three or five model calls (five when the sides disagree after
        round one) in about 40 seconds: progress in the
        debate's `steps` (GET /api/research/debates/{id}). Refused with the role and model when a role's model
        is unavailable (503), when the day's runs are used up (429), for a tier without web research (403)."""
        user_id = await current_user(request)
        generator = debate_allowed(user_id)
        if not body.symbol and not body.eventId:
            raise debate_error("research_symbol_invalid")
        try:
            return await debates.start(body.symbol, event_id=body.eventId, force=body.force, user_id=user_id, generator=generator)
        except EventProductError as error:
            names = [ctx.services.research_live.product_name(product) for product in error.products]
            known = DEBATE_MESSAGES[error.code][1]
            raise debate_error(error.code, f"{known}（涉及：{'、'.join(names)}）", products=error.products) from error
        except DebateUnavailable as error:
            raise debate_error(error.code, error.message, 429 if error.code == "research_debate_budget" else 503,
                               role=error.role, model=error.model) from error
        except LookupError as error:
            raise debate_error(str(error.args[0] if error.args else error)) from error
        except ValueError as error:
            raise debate_error(str(error)) from error

    @application.get("/api/research/debates/{debate_id}")
    async def research_debate(request: Request, debate_id: str) -> dict[str, object]:
        """One debate (read-only, never starts anything): topic, the five 底稿 and their state, the numbered
        facts, both rounds (arguments that failed the check marked with why), the verdict with its computed
        confidence, the frozen reckoning, and the record (models, the judge's A/B mapping, tokens, raw output)."""
        await current_user(request)
        found = await asyncio.to_thread(debates.get, debate_id)
        if found is None:
            raise debate_error("research_debate_unknown")
        return found

    @application.post("/api/research/debates/{debate_id}/adopt")
    async def research_debate_adopt(request: Request, debate_id: str) -> dict[str, object]:
        """写进今日判断: the debate's conclusion becomes the product's 今日判断 through the desk's own check and
        store (its history included); the tier folds to the desk's four (略偏多 → 偏多, 看不清 → 不明)."""
        user_id = await current_user(request)
        debate_allowed(user_id)
        try:
            return debates.adopt(debate_id)  # on the loop, like the desk's own judgment writes
        except LookupError as error:
            raise debate_error("research_debate_unknown") from error
        except ValueError as error:
            raise debate_error(str(error)) from error

    @application.post("/api/research/debates/{debate_id}/cancel")
    async def research_debate_cancel(request: Request, debate_id: str) -> dict[str, object]:
        """取消 a running debate (the calls already made stay booked); a finished one is returned as it is."""
        user_id = await current_user(request)
        debate_allowed(user_id)
        try:
            return await debates.cancel(debate_id, user_id)
        except PermissionError as error:
            raise debate_error("research_debate_not_yours") from error
        except LookupError as error:
            raise debate_error("research_debate_unknown") from error

    @application.get("/api/research/flash")
    async def research_flash(request: Request, product: str | None = Query(default=None, min_length=4, max_length=24),
                             products: str | None = Query(default=None, max_length=600),
                             tag: str | None = Query(default=None, max_length=16),
                             hours: int = Query(default=24, ge=1, le=FLASH_OFFICIAL_HOURS_MAX),
                             limit: int = Query(default=50, ge=1, le=200),
                             since: str | None = Query(default=None, max_length=64),
                             include: str | None = Query(default=None, max_length=6500),
                             official: bool = Query(default=False)) -> dict[str, object]:
        """The cross-source flash feed from the shared table the background poller fills (new → old).
        `product` (or `products`, comma-separated, ≤ 30 — 持仓与自选) answers the AI-screened view:
        kept items with their `ai` reading and `screen` (mode, pending, dropped groups and items), the
        same items merged into `events` (one per event and research day, tiered major / other / minor) with
        `layers` counts; a single `product` also answers its `brief` (今日新闻要点) and `tonight` (related
        calendar events until 08:30).

        `since` (快讯游标增量, 2026-10-12; "0" the first time) asks for the compact feed: items reduced to
        their view fields with `id` + `rev`, the content of the rows changed after that cursor once in
        `rows`, the new `cursor`; `include` (comma-separated ids) adds rows the reader found missing
        (research_live.compact_flash). Without `since` the feed is the full one.

        `official=true` lists only the official sources' rows (exchanges and ministries, kept 30 days), never
        filtered by `product` / `products`: `hours` may then reach back up to a year (bounded by the retention);
        otherwise it is read as at most 72."""
        await current_user(request)
        wanted = [value.strip() for value in products.split(",") if value.strip()][:30] if products else None
        live = ctx.services.research_live
        if since is None:
            return await asyncio.to_thread(live.flash_feed, product, tag, hours, limit, wanted, official)
        named = frozenset([value.strip() for value in (include or "").split(",") if value.strip()][:FLASH_INCLUDE_MAX])
        return await asyncio.to_thread(lambda: live.compact_flash(live.flash_feed(product, tag, hours, limit, wanted, official),
                                                                  since, named))

    @application.get("/api/research/calendar")
    async def research_calendar(request: Request, days: int = Query(default=7, ge=1, le=7),
                                region: str | None = Query(default=None, max_length=12),
                                product: str | None = Query(default=None, min_length=1, max_length=24)) -> dict[str, object]:
        """The economic calendar (next 1–7 days, China / US / Eurozone, importance ≥ medium), from cache;
        status ok / stale (missingDays, staleDays) / loading / unavailable. `product` (SHFE.au or au)
        keeps only the events related to that product's category (`otherCount`: the rest)."""
        await current_user(request)
        return await asyncio.to_thread(ctx.services.research_live.calendar_feed, days, region, product)

    @application.get("/api/research/{dataset}")
    async def research_dataset(request: Request, dataset: str, symbol: str = Query(min_length=3, max_length=40)) -> dict[str, object]:
        await current_user(request)
        if dataset not in DATASETS:
            raise HTTPException(status_code=404, detail="research_dataset_unknown")
        try:
            return await asyncio.to_thread(research.dataset, dataset, symbol)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
