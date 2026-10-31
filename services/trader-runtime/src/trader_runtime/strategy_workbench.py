"""User-facing strategy library, fee settings and bounded rule validation."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import ValidationError

from .browser_backtests import (
    SERVER_FIELDS,
    BrowserChecks,
    BrowserTickets,
    Ticket,
    browser_backtests_enabled,
    inputs_digest,
)
from .instruments import account_currency, foreign_product_of, foreign_tradable, strategy_scope_symbol
from .memory_stats import review_breakdown
from .order_backfill import backfill_order_fills
from .store import utc_now
from .strategy_backtest import (
    BacktestDataError,
    BacktestQueue,
    BacktestQueueTimeout,
    card_payload,
    execute,
    history_error_message,
    plan_for_backtest,
    prepare_setup,
    setup_inputs,
)
from .strategy_design_errors import design_failure
from .strategy_health import apply_advice
from .strategy_plain import definition_currency, plain_rows, strategy_summary
from .strategy_requests import (
    ApplyAdviceRequest,
    BacktestOptions,
    BrowserInputsRequest,
    BrowserResultRequest,
    ChartPeriodRequest,
    DesignJobRequest,
    DesignRequest,
    InstrumentScope,
    PlanBacktestRequest,
    SavePlanRequest,
    TemplateInstantiateRequest,
    TemplatePreviewRequest,
    ValidatePlanRequest,
)
from .strategy_rules import StrategyPlan, StrategyRuleError, evaluate_plan, require_close_schedule
from .strategy_scope import execution_strategy
from .strategy_verdict import model_verdict, tuned_on_same_period, verdict_facts
from .trading_costs import (
    FeeSchedule,
    FeeSettings,
    account_fills,
    contract_multiplier,
    foreign_backtest_schedule,
    foreign_review_schedules,
    mask_account_fees,
    persist_account_fills,
    review_account_trades,
)


def definition_for(plan: StrategyPlan) -> dict:
    return {"schemaVersion": 2, "template": "combined", "plan": plan.model_dump(),
            "timeframeSeconds": plan.timeframeSeconds, "direction": plan.direction,
            "riskLimits": {"maxPosition": plan.maxPosition, "maxLoss": plan.maxLoss,
                "maxOrdersPerDay": plan.maxOrdersPerDay, "stopLossTicks": plan.stopLossTicks,
                "takeProfitTicks": plan.takeProfitTicks, "cooldownBars": plan.cooldownBars,
                "perTradeRisk": plan.perTradeRisk},
            "signalInput": "completed_bars_only", "executionMode": "simulation_only", "liveTrading": "disabled"}



PERIOD_NAMES = {60: "1分钟", 180: "3分钟", 300: "5分钟", 900: "15分钟", 3600: "1小时", 86400: "日线"}


def period_name(seconds: int) -> str:
    return PERIOD_NAMES.get(seconds, f"{seconds // 60}分钟")


def contract_label(provider, symbol: str) -> str:
    """螺纹2701-style name from the instrument catalog (as the client's contractLabel): the exact
    contract's name, else a same-product sibling's name with this contract's digits, else the code."""
    try:
        items = list((provider.instrument_catalog() or {}).get("items") or [])
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        items = []
    return catalog_contract_label(items, symbol)


def catalog_contract_label(items: list[dict], symbol: str) -> str:
    """contract_label over catalog `items` already read (the voice call reads them over the API)."""
    code = symbol.split(".")[-1]
    exact = next((item for item in items if str(item.get("symbol", "")).upper() == symbol.upper()), None)
    if exact and exact.get("instrumentName"):
        return str(exact["instrumentName"])
    product = re.sub(r"\d+$", "", code)
    digits = code[len(product):]
    sibling = next((item for item in items if str(item.get("productId", "")).lower() == product.lower()
                    and item.get("instrumentName")), None)
    name = re.sub(r"\d+$", "", str(sibling["instrumentName"])) if sibling else ""
    return f"{name}{digits}" if name and digits else code


def unique_strategy_name(store, user: str, name: str) -> str:
    """`name`, or `name (2)`, `name (3)` … when the user already has a strategy of that name."""
    with store.lock:
        taken = {row["name"] for row in store.connection.execute(
            """SELECT name FROM strategy_drafts WHERE user_id = ? AND status != 'archived'
            AND json_extract(definition_json, '$.libraryStrategyId') IS NULL""", (user,))}
    if name not in taken:
        return name
    number = 2
    while f"{name} ({number})" in taken:
        number += 1
    return f"{name} ({number})"


def exchange_schedule_for(root, symbols: list[str]) -> tuple[dict | None, list[str]]:
    """One exchange-standard fee schedule covering every contract a backtest trades (#506 的
    多腿口径保留)：每个合约各读一次交易所标准——第一个合约的费率做 default（单合约形状），
    其余合约做精确 override，价差两腿各按自己的费率计。任一合约拿不到就整体不计费
    （None, missing），缺的合约在结果里点名。"""
    from .exchange_fees import exchange_fee_schedule
    schedules = [(symbol, exchange_fee_schedule(symbol, root=root)) for symbol in symbols]
    missing = [symbol for symbol, item in schedules if item is None]
    if missing:
        return None, missing
    resolved = [(symbol, item) for symbol, item in schedules if item is not None]
    first = resolved[0][1]
    if len(resolved) == 1:
        return first, []
    legs = [{"symbol": symbol, "rate": item["default"], "reference": item.get("reference")}
            for symbol, item in resolved]
    overrides = [{**item["default"], "symbol": symbol} for symbol, item in resolved[1:]]
    return {**first, "overrides": overrides, "legs": legs}, []


# 本站模拟盘强平 (#1204): the paper engine's own close orders carry this clientOrderId prefix; no app row names them.
FORCED_LIQUIDATION_PREFIX = "paper-force-"


class OrderSources(dict):
    """`order_sources`' map: an id it has no row for that the paper engine sent as a forced close reads
    as 强平 through the same `.get` every caller already uses."""

    def get(self, key: Any, default: Any = None) -> Any:
        if key in self:
            return self[key]
        return {"source": "liquidation"} if str(key or "").startswith(FORCED_LIQUIDATION_PREFIX) else default


def order_sources(store, user: str) -> dict[str, dict]:
    """clientOrderId → who placed the order, for the trade review's 来源: the AI's own orders and
    deterministic stops from the order's proposal, strategy orders (with the strategy's name) from
    the execution attempts and their legs, a confirmed strategy card (出卡确认) from the card's
    strategySource, and a card the conversation's AI proposed and the user confirmed (「AI 建议」,
    origin "ai") — the AI's orders carry the card's reason. Anything else — the ticket, a card from the
    REST route, an order placed outside the app — is left out and reads as manual. The name is the
    library strategy's own name: the fill row already shows the contract, so an instance's
    "名称 · 合约" would repeat it. The paper engine's forced closes read as 强平 (`OrderSources`)."""
    sources: dict[str, dict] = OrderSources()
    for row in store.connection.execute("""SELECT l.client_order_id, p.payload_json, pb.id AS playbook_id, pb.name AS playbook_name
            FROM tqkq_sim_order_links l JOIN proposals p ON p.id = l.proposal_id
            LEFT JOIN proposal_playbooks pp ON pp.proposal_id = l.proposal_id
            LEFT JOIN playbooks pb ON pb.id = pp.playbook_id AND pb.user_id = l.user_id
            WHERE l.user_id = ?""", (user,)):
        payload = json.loads(row["payload_json"] or "{}")
        automation = payload.get("automationSource")
        strategy_source = payload.get("strategySource")
        reason = str(payload.get("reason") or "").strip()
        # 打法本 (2026-10-25): the setup the card was placed under (the ticket's 打法); a deleted setup names none.
        playbook = {"playbookId": row["playbook_id"], "playbookName": row["playbook_name"]} if row["playbook_name"] else {}
        if automation == "deterministic_stop":
            sources[row["client_order_id"]] = {"source": "stop", **({"reason": reason} if reason else {})}
        elif automation == "ai_autonomous":
            sources[row["client_order_id"]] = {"source": "ai", **({"reason": reason} if reason else {}), **playbook}
        elif isinstance(strategy_source, dict) and strategy_source.get("name"):
            sources[row["client_order_id"]] = {"source": "strategy", "strategyName": str(strategy_source["name"])}
        elif payload.get("origin") == "ai":
            sources[row["client_order_id"]] = {"source": "card", **({"reason": reason} if reason else {}), **playbook}
        elif playbook:
            sources[row["client_order_id"]] = {"source": "manual", **playbook}
    for row in store.connection.execute("""SELECT COALESCE(g.client_order_id, a.client_order_id) AS client_order_id,
            COALESCE(lib.name, d.name) AS name
            FROM strategy_execution_attempts a JOIN strategy_drafts d ON d.id = a.strategy_id
            LEFT JOIN strategy_drafts lib ON lib.id = json_extract(d.definition_json, '$.libraryStrategyId')
                AND lib.user_id = d.user_id
            LEFT JOIN strategy_execution_legs g ON g.attempt_id = a.id
            WHERE a.user_id = ?""", (user,)):
        # An attempt without legs may list several orders, comma-separated.
        for identifier in (row["client_order_id"] or "").split(","):
            if identifier and sources.get(identifier, {}).get("source") != "stop":
                sources[identifier] = {"source": "strategy", "strategyName": row["name"]}
    return sources


def apply_round_playbooks(store, rounds: list[dict], *, trading_account_id: str | None, user: str) -> None:
    """打法本 (2026-10-25): a round trip tagged afterwards in the review overrides the setup its card carried —
    the review, the 分组 table and the memory statistics all read it through here. No account id = the default
    one, as the 补充 read."""
    tagged = store.list_round_playbooks(trading_account_id=trading_account_id or store.default_trading_account_id,
                                        user_id=user)
    for trade in rounds:
        if trade["id"] in tagged:
            trade.update(tagged[trade["id"]])


def source_label(source: dict | None) -> str | None:
    """An order's 来源 in a few words, for the lines that name a fill outside its own card (the 成交
    reminder, the 委托与成交 answer card; #932): 策略 · 名称 / AI 自主 / 止损 / 强平 — the 成交 tab's own
    labels. A confirmed card (AI 建议) and a manual order read as the user's own and stay unlabelled."""
    kind = (source or {}).get("source")
    if kind == "strategy":
        name = str((source or {}).get("strategyName") or "").strip()
        return f"策略 · {name}" if name else "策略"
    return {"ai": "AI 自主", "stop": "止损", "liquidation": "强平"}.get(str(kind))


def journal_fields(rounds: list[dict], notes: dict[str, dict], desk: Any = None) -> None:
    """交易日志 (2026-09-29) on each round trip, in place: how long it was held, the user's 补充
    (`note`) and what the research desk said about the product when it was opened (`deskView`,
    only for trades opened after the judgment history began)."""
    for trade in rounds:
        trade["holdingMinutes"] = max(0, round((int(trade["closeTime"]) - int(trade["openTime"])) / 60e9))
        note = notes.get(trade["id"])
        if note is not None and note.get("volumeAtWrite") is not None \
                and int(note["volumeAtWrite"]) != int(trade["volume"]):
            # 同一回合 ID 的手数变了（#514-2，迟到的更早开仓重排 FIFO）：补充说的已
            # 不是现在这几手，标“范围已变”，原文照读。
            note = {**note, "scopeChanged": True}
        trade["note"] = note
        trade["deskView"] = (desk.view_at(trade["symbol"], datetime.fromtimestamp(int(trade["openTime"]) / 1e9, UTC))
                             if desk is not None else None)


async def account_trade_review(store, provider, fees, user: str, *, symbol: str | None, days: int,
                               trading_account_id: str | None = None, desk: Any = None,
                               product: Any = None) -> dict:
    """Round trips over the account's fills: the provider's current-day fills plus
    every fill the store kept, each with its 交易日志 fields (`journal_fields`). Raises
    KeyError/ValueError/RuntimeError when the account or a requested contract's
    parameters are unavailable. `product` (a ForeignProduct, with `symbol` None) keeps
    every month contract of that 外盘 product (the 美元子账户 half of a 外盘 chart)."""
    account = await asyncio.to_thread(provider.account_summary, "review")
    activity = await asyncio.to_thread(provider.account_activity)
    schedule = getattr(provider, "fee_schedule", None) or fees.get(user)
    def multiplier_for(name: str) -> float | None:
        return contract_multiplier(provider, name)
    today = await asyncio.to_thread(
        persist_account_fills, store, activity, multiplier_for, trading_account_id=trading_account_id,
    )
    await asyncio.to_thread(backfill_order_fills, store, provider, trading_account_id=trading_account_id)
    fills = account_fills(store, today, symbol, trading_account_id=trading_account_id)
    if product is not None:
        fills = [fill for fill in fills if foreign_product_of(str(fill.get("symbol") or "")) is product]
    multipliers, skipped = {}, []
    for name in sorted({fill["symbol"] for fill in fills}, key=str.upper):
        # The stored multiple wins: it outlives the contract in the provider.
        stored = next((f.get("volumeMultiple") for f in fills if f["symbol"] == name and f.get("volumeMultiple")), None)
        multiplier = stored or await asyncio.to_thread(multiplier_for, name)
        if multiplier:
            multipliers[name] = multiplier
        elif symbol:
            raise KeyError(name)
        else:
            # A contract the provider no longer serves (expired, unsubscribed)
            # must not hide every other symbol's round trips.
            skipped.append(name)
    sources = await asyncio.to_thread(order_sources, store, user)
    from .replay import ReplayProvider
    now = provider.clock() if isinstance(provider, ReplayProvider) else None
    # 外盘合约（美元子账户）按外盘引擎的每手美元手续费配对，不套用户为国内设的费率表（外盘盘点 3A，2026-10-21）。
    foreign = await asyncio.to_thread(foreign_review_schedules, provider, list(multipliers))
    result = review_account_trades(fills, schedule, multipliers, days=days, sources=sources, now=now, schedules=foreign)
    # 今日复盘 (2026-10-06): each round trip's 开 / 平 leg tagged 计划内 / 提醒后 / 临时, as the day's review is.
    from .daily_review import annotate_round_tags
    await asyncio.to_thread(annotate_round_tags, store, user_id=user, account_id=trading_account_id,
                            rounds=result["trades"], sources=sources)
    notes = await asyncio.to_thread(store.list_trade_notes, trading_account_id=trading_account_id)
    await asyncio.to_thread(journal_fields, result["trades"], notes, desk)
    await asyncio.to_thread(apply_round_playbooks, store, result["trades"], trading_account_id=trading_account_id, user=user)
    # 补充 keyed on a backfilled fill the real fills replaced (#514): listed read-only —
    # 原文、via、更新时间 — never rewritten, so the user can still read and redo them.
    result["unlinkedNotes"] = await asyncio.to_thread(
        store.list_unlinked_trade_notes, trading_account_id=trading_account_id)
    if skipped:
        result["skippedSymbols"] = skipped
    net = mask_account_fees(result, schedule, charged=foreign) == "net"
    # After the mask: grouped like the per-trade figures (net, or gross once the rates are hidden).
    result["breakdown"] = review_breakdown(result["trades"])
    # 全是按外盘引擎每手费用算的外盘合约：逐笔手续费与净收益就是引擎实际扣的口径（"foreign"）。
    foreign_only = bool(multipliers) and set(multipliers) <= set(foreign)
    return {**result, "account": account, "feeSchedule": schedule, "currency": account_currency(trading_account_id),
            "costSource": "foreign" if foreign_only and net else
            "custom_simulation" if getattr(provider, "fee_schedule", None) and net else "estimate" if net else "account",
            "notice": "账户权益与实际扣费按账户回报；未提供逐笔手续费时不推算逐笔净收益。"}


async def with_foreign_review(review: dict, store, fees, user: str, *, accounts, account_id: str, symbol: str,
                              days: int, desk: Any = None) -> dict:
    """外盘图表上人民币本站模拟账户的复盘带上美元子账户同一品种的回合（F2 2026-10-21，操作记录卡的外盘成交据此
    显示盈亏）：子账户与操作记录同一个（``agent_activity.foreign_half``），回合按子账户自己的复盘算（外盘引擎每手
    美元手续费，``costSource`` foreign），每条写明 ``accountId`` 与 ``currency``（USD）。人民币账户在外盘合约上本没有
    回合，这时金额汇总、``currency``、``costSource`` 都取子账户的；``foreignAccount.status`` 是 ok / unavailable。"""
    from .agent_activity import foreign_half

    found = await foreign_half(accounts, account_id=account_id, user=user, symbol=symbol, assemble=True)
    if found is None or found[2] is None:
        return review
    foreign_id, services, product = found
    half = {"accountId": foreign_id, "currency": "USD"}
    if services is None:
        return {**review, "foreignAccount": {**half, "status": "unavailable"}}
    try:
        other = await account_trade_review(store, services.provider, fees, user, symbol=None, days=days,
                                           trading_account_id=foreign_id, desk=desk, product=product)
    except (KeyError, ValueError, RuntimeError, OSError):
        return {**review, "foreignAccount": {**half, "status": "unavailable"}}
    trades = [{**trade, **half} for trade in other["trades"]]
    merged = {**review, "trades": sorted(review["trades"] + trades, key=lambda item: (item["closeTime"], item["id"])),
              "foreignAccount": {**half, "status": "ok"}}
    if not review["trades"]:
        merged.update({key: other[key] for key in ("estimatedCommission", "estimatedNet", "unmatched", "currency",
                                                   "costSource") if key in other})
    return merged


def fixed_verdict_model(application) -> Any:
    """Browser tests only (AI_TRADER_FAKE_RESEARCH_LIVE=1 on the local fake runtime): the recorded
    fake model that also designs a strategy from one sentence and writes the 回测 verdict sentence."""
    return getattr(application.state, "fixed_strategy_model", None)


async def concrete_symbol(provider, symbol: str) -> str:
    """外盘三期：策略要的是具体合约。外盘图表的快期外盘主连代码（KQD.m@COMEX.GC）换成美元子账户此刻的主力月份
    （CME.GCEZ26，与下单同一选法）；其余原样。取不到主力时 503，不拿主连去建策略。"""
    product = foreign_product_of(symbol)
    if product is None or not str(symbol).upper().startswith("KQD."):
        return symbol
    if not foreign_tradable(product):
        # 伦铜、美棉这类只接了行情的外盘品种：MT5 里没有它的月份合约，不是 MT5 不通。
        raise HTTPException(422, f"{product.name}只接了行情，没有可交易的月份合约，不能用于策略")
    from .trader_tools import foreign_main_symbol
    main = await asyncio.to_thread(foreign_main_symbol, provider, product)
    if main is None:
        raise HTTPException(503, "外盘这个品种现在取不到主力月份合约（MT5 行情不通），暂时不能用于策略")
    return main


def register_workbench(application, current_user, root, adapter, store, broker, provider, proposals, ctx=None):
    from .ui_workspace import UiWorkspace
    workspace = UiWorkspace(root)
    application.state.ui_workspace = workspace
    fees = FeeSettings(root)
    application.state.fee_settings = fees
    # AI 引导设计 retired 2026-10-08 (the 策略编辑卡 replaced it); its endpoints answer 410 for old clients.
    from .strategy_editor import register_retired_guide_routes
    register_retired_guide_routes(application)

    @application.get("/api/trading-costs", response_model=FeeSchedule)
    async def get_costs(request: Request):
        return fees.get(await current_user(request))

    @application.post("/api/trading-costs", response_model=FeeSchedule)
    async def save_costs(request: Request, body: FeeSchedule):
        user = await current_user(request)
        try:
            return fees.save(user, body.model_dump())
        except (ValueError, ValidationError) as error:
            raise HTTPException(422, "手续费配置无效，请检查费率与品种") from error

    @application.get("/api/workspace")
    async def workspace_identity(request: Request):
        return {"scope": workspace.scope(await current_user(request))}

    @application.post("/api/strategy-plans/design-jobs", status_code=202)
    async def start_design(request: Request, body: DesignJobRequest):
        user = await current_user(request)
        selected = fixed_verdict_model(application) or (
            adapter.adapter_for_user(user) if hasattr(adapter, "adapter_for_user") else adapter)
        return workspace.start(user, body.model_dump(exclude_none=True), selected)

    @application.get("/api/strategy-plans/design-jobs/{identifier}")
    async def design_status(identifier: str, request: Request):
        return workspace.get(await current_user(request), identifier)

    @application.delete("/api/strategy-plans/design-jobs/{identifier}")
    async def stop_design(identifier: str, request: Request):
        return await workspace.cancel(await current_user(request), identifier)

    @application.post("/api/strategy-plans/design", deprecated=True, description="兼容同步入口；交互式客户端请使用design-jobs创建可恢复的生成任务。", response_model=StrategyPlan)
    async def design(request: Request, body: DesignRequest):
        user = await current_user(request)
        from .strategy_designer import design_plan
        selected = adapter.adapter_for_user(user) if hasattr(adapter, "adapter_for_user") else adapter
        try:
            return await design_plan(selected, body.description, body.constraints, user_id=user)
        except (ValueError, RuntimeError, TimeoutError) as error:
            raise HTTPException(422, design_failure(error, "设计策略")) from error

    @application.get("/api/strategy-plans")
    async def library(request: Request):
        user = await current_user(request)
        # planSummary (说人话): the one sentence 详情 shows above the five sections; planRows (策略页
        # 改版第八批) the 开仓 / 平仓 / 止损 / 风险 rows with their “?” terms; latestBacktest the card's
        # 最近回测 (the newest stored result's summary, null before the first one).
        latest = store.latest_backtest_summaries(user_id=user)
        return {"items": [{**item, "planSummary": strategy_summary(item.get("definition") or {}),
                           "planRows": rows_for(item.get("definition") or {}),
                           "latestBacktest": latest.get(item["strategyId"])}
                          for item in store.list_strategy_library(user_id=user)],
                "executions": [item for item in store.list_strategy_drafts(user_id=user, limit=50)
                               if item["definition"].get("libraryStrategyId")]}

    def rows_for(definition: dict) -> list[dict] | None:
        try:
            return plain_rows(StrategyPlan.model_validate(definition.get("plan") or {}), currency=definition_currency(definition))
        except (ValidationError, ValueError):
            return None

    async def save_library_plan(request: Request, user: str, plan: StrategyPlan, scope: dict,
                                extra: dict | None = None) -> dict:
        """One library strategy (symbol "*") from a plan; saving the same definition again returns
        the existing one. `extra` adds definition fields (a template's origin)."""
        definition = {**definition_for(plan), "instrumentScope": scope, **(extra or {})}
        digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        # Saving the same definition is idempotent; execution state is never edited.
        existing = store.find_library_strategy(user_id=user, digest=digest)
        if existing is not None:
            return existing
        # 2026-09-23 isolation: the draft (and its source session) land on the
        # caller's own account, never the default account.
        account_id = ((await ctx.account_context(request, user)).account.account_id
                      if ctx is not None else None)
        session = broker.create_session("local", "native", user, trading_account_id=account_id)
        return store.save_strategy_draft(strategy_id=uuid4().hex, user_id=user,
            source_session_id=session.session_id, name=plan.name, symbol="*",
            version=store.next_strategy_draft_version(user_id=user, name=plan.name),
            status="draft", definition=definition, definition_digest=digest,
            trading_account_id=account_id)

    @application.post("/api/strategy-plans", status_code=201)
    async def save_plan(request: Request, body: SavePlanRequest):
        """保存一条策略库版本。策略编辑卡 (2026-10-08): baseStrategyId = 修改的是哪条——存为它的新版本（同名时
        版本号加一，原版本保留），它定义里卡片以外的字段（模板来源等）原样带上；templateId = 新建时的模板起点，
        记为模板来源（和模板默认不同时标为已调整），与已有策略重名时加“(2)”等序号。都不带时照旧：同名存为
        下一个版本，同样的定义再存返回原来那条。"""
        user = await current_user(request)
        extra: dict = {}
        plan = body.plan
        scope = body.instrumentScope.model_dump()
        if body.baseStrategyId is not None:
            try:
                base = store.get_strategy_draft(body.baseStrategyId, user_id=user)
                if base["definition"].get("libraryStrategyId"):
                    # A running instance's id (the AI's “改一下这条策略” names the one it reads): its library strategy.
                    base = store.get_strategy_draft(str(base["definition"]["libraryStrategyId"]), user_id=user)
            except KeyError:
                raise HTTPException(404, "策略不存在") from None
            definition = base["definition"]
            if definition.get("schemaVersion") != 2 or definition.get("libraryStrategyId") or not definition.get("plan"):
                raise HTTPException(422, "只能修改策略库里的策略")
            extra = {key: value for key, value in definition.items()
                     if key not in {*definition_for(plan), "instrumentScope"}}
            if "instrumentScope" not in body.model_fields_set and isinstance(definition.get("instrumentScope"), dict):
                scope = definition["instrumentScope"]  # a new version that does not say its scope keeps the base's (#1034 review)
            source = extra.get("templateSource")
            if isinstance(source, dict) and plan.model_dump() != definition["plan"]:
                extra["templateSource"] = {**source, "defaults": False, "modified": True,
                                           "changes": [*(source.get("changes") or []), "在策略编辑卡里修改"][-20:]}
        elif body.templateId is not None:
            template = library_templates.templates().get(body.templateId)
            if template is None:
                raise HTTPException(404, "模板不存在")
            default, values = library_templates.build_plan(template, {}, timeframe=plan.timeframeSeconds or None)
            origin = library_templates.template_source(template, values)
            ignored = {"name", "description"}
            same = ({k: v for k, v in plan.model_dump().items() if k not in ignored}
                    == {k: v for k, v in default.model_dump().items() if k not in ignored})
            extra = {"templateSource": origin if same else {**origin, "defaults": False, "modified": True,
                                                              "changes": ["在策略编辑卡里调整"]}}
            plan = plan.model_copy(update={"name": unique_strategy_name(store, user, plan.name)})
        try:
            return await save_library_plan(request, user, plan, scope, extra)
        except (KeyError, ValueError, ValidationError) as error:
            raise HTTPException(422, "策略格式无效，请检查名称、合约和规则") from error

    @application.get("/api/strategy-plans/prefill")
    async def habit_prefill_route(request: Request):
        """策略编辑卡“按我的习惯”(2026-10-08): 从本账户近 20 个交易日的已平仓成交（与复盘同一条成交轮次）
        按统计算出能确定的字段——合约（最常做的品种的主力）、方向（一侧占比 ≥70%）、止损跳数（该品种亏损单
        出场跳数的中位数，至少 3 笔亏损单）、收盘前平仓（都没留隔夜）、开仓时段（≥70% 在同一连续时段）、
        每次手数（众数）；每项带一句依据和成交 id。不到 5 笔不预填（eligible: false 与原因）。开仓条件
        不预填。只读这次请求所在的账户，不调模型、不下单。"""
        from .strategy_editor import habit_prefill
        user = await current_user(request)
        account = ((await ctx.account_context(request, user)).account if ctx is not None else None)
        source = account.provider if account is not None else provider
        try:
            review = await account_trade_review(
                store, source, fees, user, symbol=None, days=45,
                trading_account_id=account.account_id if account is not None else None)
            catalog = await asyncio.to_thread(source.instrument_catalog)
        except (KeyError, ValueError, RuntimeError, AttributeError) as error:
            raise HTTPException(409, "成交暂时读不到，先从空白或模板开始") from error
        return habit_prefill(review["trades"], catalog_items=list((catalog or {}).get("items") or []),
                             now_ns=time.time_ns())

    @application.delete("/api/strategy-plans/{identifier}")
    async def delete_plan(identifier: str, request: Request):
        user = await current_user(request)
        try:
            # Check every contract, not the paginated list shown in the panel.
            # Archive associated idle instances atomically so they cannot restart.
            with store.transaction() as db:
                item = store.get_strategy_draft(identifier, user_id=user)
                # A running instance is removed with its library strategy. An old-format draft (schemaVersion 1, no
                # plan: what the AI's create_strategy_draft saved before 2026-10-20) is not in 我的策略 — deleted here,
                # under the same monitoring / open-order checks, so the user (through the AI) can still clear it (#932 F5).
                if item["definition"].get("libraryStrategyId"):
                    raise ValueError("请在我的策略中删除策略库版本")
                if item["status"] == "archived":
                    return {"status": "deleted", "strategyId": identifier}
                related = db.execute("""SELECT id, symbol, status FROM strategy_drafts
                    WHERE user_id = ? AND (id = ? OR
                    json_extract(definition_json, '$.libraryStrategyId') = ?)""",
                    (user, identifier, identifier)).fetchall()
                for row in related:
                    if row["status"] == "monitoring":
                        raise ValueError(f"{row['symbol']} 仍在监控，请先暂停后再删除")
                    pending = db.execute("""SELECT 1 FROM strategy_execution_attempts a
                        LEFT JOIN strategy_execution_legs l ON l.attempt_id = a.id
                        LEFT JOIN tqkq_sim_order_links o ON o.client_order_id = COALESCE(l.client_order_id, a.client_order_id) AND o.user_id = a.user_id
                        WHERE a.user_id = ? AND a.strategy_id = ? AND
                        (a.status IN ('RESERVED', 'UNKNOWN') OR
                         (COALESCE(l.status, a.status) IN ('RESERVED', 'SUBMITTED', 'PARTIAL', 'UNKNOWN') AND
                          (o.client_order_id IS NULL OR o.cancel_phase = 'UNKNOWN' OR
                           (COALESCE(o.last_remote_status, '') != 'FINISHED' AND o.submit_phase != 'ABANDONED'))))
                        LIMIT 1""", (user, row["id"])).fetchone()
                    if pending:
                        raise ValueError(f"{row['symbol']} 还有未完成或结果未知的委托，请先在持仓与订单中处理")
                    protected = db.execute("""SELECT 1 FROM ai_stop_orders WHERE user_id = ?
                        AND json_extract(strategy_metadata_json, '$.strategyId') = ?
                        AND COALESCE(remaining_volume, volume) > 0 LIMIT 1""", (user, row["id"])).fetchone()
                    if protected:
                        raise ValueError(f"{row['symbol']} 仍有关联持仓，请先处理持仓并刷新状态后再删除")
                for row in related:
                    db.execute("UPDATE strategy_drafts SET status = 'archived', updated_at = ? WHERE id = ? AND user_id = ?",
                               (utc_now(), row["id"], user))
            return {"status": "deleted", "strategyId": identifier}
        except KeyError:
            raise HTTPException(404, "策略不存在") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @application.post("/api/strategy-plans/{identifier}/scope")
    async def update_scope(identifier: str, request: Request, body: InstrumentScope):
        user = await current_user(request)
        try:
            item = store.get_strategy_draft(identifier, user_id=user)
            if item["definition"].get("schemaVersion") != 2 or item["definition"].get("libraryStrategyId"):
                raise ValueError("只能修改策略库的适用范围")
            scope = body.model_dump()
            definition = {**item["definition"], "instrumentScope": scope}
            digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
            if "instrumentScope" not in item["definition"]:
                # Same isolation rule as save_plan: the caller's own account.
                account_id = ((await ctx.account_context(request, user)).account.account_id
                              if ctx is not None else None)
                session = broker.create_session("local", "native", user, trading_account_id=account_id)
                return store.save_strategy_draft(strategy_id=uuid4().hex, user_id=user,
                    source_session_id=session.session_id, name=item["name"], symbol="*",
                    version=store.next_strategy_draft_version(user_id=user, name=item["name"]),
                    status="draft", definition=definition, definition_digest=digest,
                    trading_account_id=account_id)
            return store.update_strategy_scope(identifier, user_id=user, definition=definition, digest=digest)
        except KeyError:
            raise HTTPException(404, "策略不存在") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @application.post("/api/strategy-plans/{identifier}/apply-advice", status_code=201)
    async def apply_health_advice(identifier: str, request: Request, body: ApplyAdviceRequest):
        """按这个改 (策略体检, 2026-09-26): one structural suggestion from this strategy's rule backtest
        (drop one entry condition / let the crosses happen within 3 bars) applied to its saved plan and
        saved as the next version of the same name, like 改一版 — the original version stays. Returns
        the saved strategy. Never trades, validates or starts monitoring."""
        user = await current_user(request)
        try:
            item = store.get_strategy_draft(identifier, user_id=user)
        except KeyError:
            raise HTTPException(404, "策略不存在") from None
        definition = item["definition"]
        if definition.get("schemaVersion") != 2 or definition.get("libraryStrategyId") or not definition.get("plan"):
            raise HTTPException(422, "只能修改策略库里的策略")
        advice = body.advice.model_dump(exclude_none=True)
        change = advice.get("text") or ("去掉一条开仓条件" if advice["kind"] == "drop" else "交叉放宽到最近 3 根K线内")
        try:
            changed = apply_advice(definition["plan"], advice)
            changed["description"] = f"{changed['description']}（按体检建议改：{change}）"[:20000]
            plan = StrategyPlan.model_validate({**changed, "name": item["name"]})
        except ValidationError as error:
            raise HTTPException(422, "按这条建议改后策略格式无效") from error
        except (KeyError, ValueError) as error:
            raise HTTPException(422, f"这条建议改不了这条策略：{error}") from error
        extra = {key: value for key, value in definition.items()
                 if key not in {*definition_for(plan), "instrumentScope"}}
        if extra.get("templateSource"):
            source = extra["templateSource"]
            extra["templateSource"] = {**source, "defaults": False, "modified": True,
                                       "changes": [*(source.get("changes") or []), change][-20:]}
        scope = definition.get("instrumentScope") or InstrumentScope().model_dump()
        return await save_library_plan(request, user, plan, scope, extra)

    @application.post("/api/strategy-plans/chart-period")
    async def chart_period(request: Request, body: ChartPeriodRequest):
        from .strategy_chart_period import rebind_items
        user = await current_user(request)
        timeframe, symbol = body.timeframeSeconds, body.symbol.strip()
        if type(timeframe) is not int or timeframe not in (60, 180, 300, 900, 3600, 86400) or not symbol:
            raise HTTPException(422, "请选择有效合约与图表周期")
        # The caller's own account's loop is the one that re-reads the rebound drafts.
        account = ((await ctx.account_for_symbol(request, user, symbol)).account if ctx is not None else None)
        monitor = account.strategy_monitor if account is not None else application.state.strategy_monitor
        if account is not None:
            symbol = await concrete_symbol(account.provider, symbol)
        try:
            async with monitor.period_lock:
                with store.lock:
                    rows = store.connection.execute("""SELECT id FROM strategy_drafts WHERE user_id=?
                        AND trading_account_id=? AND lower(symbol)=lower(?) AND status!='archived'
                        AND json_extract(definition_json, '$.chartPlan') IS NOT NULL""",
                        (user, account.account_id if account is not None else store.default_trading_account_id, symbol)).fetchall()
                items = [store.get_strategy_draft(row["id"], user_id=user) for row in rows]
                changed = await rebind_items(items, timeframe, store=store,
                                             provider=account.provider if account is not None else provider)
            monitor.wake()
            return {"symbol":symbol, "timeframeSeconds":timeframe, "strategyIds":changed}
        except (ValueError, RuntimeError) as error:
            raise HTTPException(409, f"周期尚未切换：{error}") from error

    @application.post("/api/strategy-plans/{identifier}/validate")
    async def validate_plan(identifier: str, request: Request, body: ValidatePlanRequest):
        user = await current_user(request)
        # 2026-09-23 isolation: the execution copy this creates is bound to the
        # caller's own account, and that account's loop is the one paused here.
        try:
            requested = str(body.symbol or store.get_strategy_draft(identifier, user_id=user)["symbol"]).strip()
        except KeyError as error:
            raise HTTPException(404, "strategy_not_found") from error
        # 外盘三期：外盘合约的执行副本在用户的美元子账户上线（撮合与记账都在那里）。
        account = ((await ctx.account_for_symbol(request, user, requested)).account if ctx is not None else None)
        monitor = account.strategy_monitor if account is not None else application.state.strategy_monitor
        source = account.provider if account is not None else provider
        try:
            async with monitor.period_lock:
                item = store.get_strategy_draft(identifier, user_id=user)
                symbol = await concrete_symbol(source, requested)
                item = execution_strategy(item, symbol, store=store, broker=broker, user=user,
                    chart_timeframe=body.chartTimeframeSeconds,
                    trading_account_id=account.account_id if account is not None else None)
                from .strategy_chart_period import rebind_items
                await rebind_items([item], body.chartTimeframeSeconds, store=store, provider=source)
                item = store.get_strategy_draft(item["strategyId"], user_id=user)
                identifier = item["strategyId"]
                evaluation = await asyncio.to_thread(evaluate_plan, source, item["definition"], item["symbol"])
                await asyncio.to_thread(require_close_schedule, source, item["definition"], item["symbol"])
                checks = [{"id": "rules", "label": "规则及参数完整", "status": "passed"},
                          {"id": "closed_bars", "label": "各周期已收盘行情可计算", "status": "passed"}]
                report = store.save_strategy_validation_report(report_id=uuid4().hex, strategy_id=identifier,
                    user_id=user, strategy_digest=item["definitionDigest"], status="passed", checks=checks,
                    evidence={"kind": "rule_validation", "performanceTest": "not_run",
                              "barTime": evaluation["evaluatedBarTime"], "futureData": False})
                if item["status"] == "draft":
                    proposals.transition_strategy_status(strategy_id=identifier, user_id=user, target="validating")
                if item["status"] in {"draft", "validating"}:
                    proposals.transition_strategy_status(strategy_id=identifier, user_id=user, target="ready",
                                                         validation_report_id=report["reportId"])
                return {"status": "passed", "strategyId": identifier, "checks": checks, "evaluation": evaluation,
                        "notice": "规则验证通过，尚未评价盈利表现；开始后使用独立模拟资金执行。"}
        except KeyError as error:
            raise HTTPException(404, "策略或所需周期行情不存在") from error
        except (ValueError, RuntimeError) as error:
            raise HTTPException(422, f"策略尚不可运行：{error}") from error

    @application.get("/api/strategy-plans/{identifier}/conditions")
    async def conditions(identifier: str, request: Request):
        user = await current_user(request)
        try:
            item = store.get_strategy_draft(identifier, user_id=user)
            source = provider
            if ctx is not None and foreign_product_of(item["symbol"]) is not None:
                # 外盘三期：外盘策略的条件读数与它的监控同一个账户（美元子账户）的行情。
                source = (await ctx.account_for_symbol(request, user, item["symbol"])).account.provider
            return await asyncio.to_thread(evaluate_plan, source, item["definition"], item["symbol"])
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(409, f"条件暂不可用：{error}") from error

    # 规则回测 (策略积木·第一期 B, 2026-09-25): the user's own rules over history, read-only. Since
    # 策略页改版第八批 (2026-10-08) every result is kept in SQLite (store/backtests.py, newest 5 per
    # strategy), so a restart loses none and the strategy cards show the latest one.
    # 2026-09-25: every rule backtest (detail page, template preview, guide, AI tool, CLI) waits here for
    # one of the worker slots (default two, one per user; AI_TRADER_BACKTEST_*); the live monitor's judgement never does.
    queue = BacktestQueue()
    application.state.strategy_backtest_queue = queue

    # 规则回测本机优先 (2026-10-12): the inputs handed to browsers and their results' cross-check.
    tickets = BrowserTickets()
    checks = BrowserChecks()

    async def resolve_backtest(request: Request, user: str, *, raw_plan: dict, options: BacktestOptions,
                               symbol: str | None) -> tuple[StrategyPlan, str, Any, dict]:
        """(executable plan, contract, data provider, fee schedule) of one rule backtest — the server's
        run and the browser's inputs resolve them the same way."""
        if options.sessionId:
            if ctx is None or broker.get_session(options.sessionId, user) is None:
                raise HTTPException(404, "session_not_found")
        try:
            plan = plan_for_backtest(raw_plan, options.timeframe)
        except (StrategyRuleError, ValidationError, ValueError) as error:
            raise HTTPException(422, f"无法回测：{error}") from error
        if plan.spread is not None:
            symbol = plan.spread.legs[0].symbol    # 价差: the legs define what is read; A drives the schedule
        if not symbol:
            raise HTTPException(422, "请指定回测合约")
        account = ((await ctx.account_for_symbol(request, user, symbol, session_id=options.sessionId)).account
                   if ctx is not None else None)
        source = account.provider if account is not None else provider
        symbol = await concrete_symbol(source, symbol)
        schedule = getattr(source, "fee_schedule", None) or fees.get(user)
        if foreign_product_of(symbol) is not None:
            # 外盘三期：美元子账户的合约按引擎的每手美元手续费回测（与实扣同一口径）。
            legs = [leg.symbol for leg in plan.spread.legs] if plan.spread is not None else [symbol]
            schedule = await asyncio.to_thread(foreign_backtest_schedule, source, schedule, legs)
        elif schedule.get("source") == "account":
            # 未自定义费率 → 交易所标准（openctp 汇总，2026-10-08）：不再读已失效的
            # AKShare fees_margin 参考档。价差各腿各读各的；任一腿拿不到就整体不扣
            # 并点名缺哪个合约。
            symbols = ([leg.symbol for leg in plan.spread.legs] if plan.spread is not None else [symbol])
            resolved, missing = await asyncio.to_thread(exchange_schedule_for, root, symbols)
            if resolved is not None:
                schedule = resolved
            elif len(symbols) > 1:
                schedule = {**schedule, "missingReference": missing}
        return plan, symbol, source, schedule

    def history_errors(error: Exception) -> HTTPException:
        """A history read or plan refusal as the HTTP error the 回测 card shows."""
        if isinstance(error, KeyError):
            return HTTPException(404, "symbol_not_found")
        if isinstance(error, (StrategyRuleError, BacktestDataError, ValidationError, ValueError)):
            return HTTPException(422, f"无法回测：{error}")
        return HTTPException(503, history_error_message(str(error)))

    async def finish_result(user: str, result: dict, *, plan_name: str, options: BacktestOptions, identity: dict,
                            persist: bool) -> dict:
        """The stored, card-ready result around a report (the server's or the browser's): its id,
        identity, parameters and the AI 怎么看 facts; stored unless a preview; the conversation card."""
        result = {"backtestId": uuid4().hex, "kind": "rule", "createdAt": utc_now(), **identity,
                  "params": {"tradingDays": options.tradingDays, "slippageTicks": options.slippageTicks,
                             "initialBalance": result["initialBalance"]}, **result}
        # AI 怎么看 (2026-10-08): the deterministic facts now, from the whole trade list; the model's
        # plain sentence comes later through POST /api/strategy-backtests/{id}/verdict.
        result["verdict"] = {**verdict_facts(result, tuned=tuned_on_same_period(identity.get("definition"))),
                             "text": None, "source": None}
        result.pop("definition", None)
        if persist:
            await asyncio.to_thread(store.save_backtest_result, user_id=user, result=result)
        if options.sessionId:
            turn_id = uuid4().hex
            await broker.publish(options.sessionId, kind="message", name="message.completed", payload={
                "turnId": turn_id, "role": "assistant", "agentId": "native",
                "text": f"「{identity.get('strategyName') or plan_name}」规则回测已完成；按收盘确认、下一根开盘成交，结果只用于研究，不会下单。"})
            await broker.publish(options.sessionId, kind="chart", name="card.strategy_backtest",
                                 payload=card_payload(result, turn_id=turn_id, task_id=result["backtestId"]))
        return result

    async def run_rule_backtest(request: Request, user: str, *, raw_plan: dict, options: BacktestOptions,
                                symbol: str | None, identity: dict, persist: bool = True) -> dict:
        plan, symbol, source, schedule = await resolve_backtest(request, user, raw_plan=raw_plan, options=options,
                                                                symbol=symbol)
        queued = time.monotonic()
        try:
            async with queue.slot(user, request_id=options.requestId, label={
                    "strategyId": identity.get("strategyId"), "strategyName": identity.get("strategyName") or plan.name,
                    "symbol": symbol}) as ticket:
                waited = time.monotonic() - queued
                try:
                    ticket.work = asyncio.create_task(asyncio.to_thread(
                        execute, source, plan=plan, symbol=symbol, fee_schedule=schedule,
                        trading_days=options.tradingDays, slippage_ticks=options.slippageTicks,
                        initial_balance=options.initialBalance, digest=inputs_digest))
                    result = await asyncio.shield(ticket.work)
                except (KeyError, StrategyRuleError, BacktestDataError, ValidationError, ValueError,
                        RuntimeError) as error:
                    raise history_errors(error) from error
        except BacktestQueueTimeout as error:
            raise HTTPException(429, str(error)) from error
        result["timings"] = {"queueMs": round(waited * 1000), **result.get("timings", {})}
        # 本机优先: where it was computed and, when the interface could have computed it, why it did not.
        result["source"] = "server"
        if options.fallbackReason:
            result["fallbackReason"] = options.fallbackReason
        check = checks.check(user, result)
        if check is not None:
            result["browserCheck"] = check
        return await finish_result(user, result, plan_name=plan.name, options=options, identity=identity,
                                   persist=persist)

    def json_text(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str)

    def json_bytes(payload: dict) -> Response:
        """A backtest result (up to 2 000 trades) as JSON in one C-level dump: FastAPI's field-by-field
        jsonable_encoder would walk it on the event loop."""
        return Response(json_text(payload), media_type="application/json")

    def stored_strategy_backtest(user: str, strategy_id: str,
                                 body: BacktestOptions) -> tuple[dict, BacktestOptions, str | None, dict]:
        """(plan, options, contract, identity) of a saved strategy's backtest: its own contract unless
        named, its chart period when it follows the chart, the library strategy's identity."""
        try:
            item = store.get_strategy_draft(strategy_id, user_id=user)
        except KeyError:
            raise HTTPException(404, "策略不存在") from None
        raw = item["definition"].get("plan")
        if not raw:
            raise HTTPException(422, "这条策略没有规则定义，无法回测")
        symbol = body.symbol or (item["symbol"] if item["symbol"] not in {"", "*"} else None)
        options = body if body.timeframe or not item["definition"].get("chartTimeframeSeconds") else \
            body.model_copy(update={"timeframe": item["definition"]["chartTimeframeSeconds"]})
        library_id = item["definition"].get("libraryStrategyId")
        definition = item["definition"]
        if library_id:
            try:
                definition = store.get_strategy_draft(str(library_id), user_id=user)["definition"]
            except KeyError:
                pass
        return raw, options, symbol, {"strategyId": library_id or strategy_id, "strategyName": item["name"],
                                      "version": item["version"], "definition": definition}

    @application.post("/api/strategies/{strategy_id}/backtest")
    async def backtest_strategy(strategy_id: str, request: Request, body: BacktestOptions):
        """用这条策略自己的规则回测历史（与上线运行同一套判定）：信号按收盘确认、下一根开盘成交，
        止损止盈按K线高低价判断，计入“设置 → 交易”的手续费。只读取历史行情、只返回结果，
        不下单；结果存库（每条策略最近 5 次，GET /api/strategy-backtests），在服务端算（source: server）。"""
        user = await current_user(request)
        raw, options, symbol, identity = stored_strategy_backtest(user, strategy_id, body)
        return json_bytes(await run_rule_backtest(request, user, raw_plan=raw, options=options, symbol=symbol,
                                                  identity=identity))

    @application.post("/api/strategy-backtests")
    async def backtest_plan(request: Request, body: PlanBacktestRequest):
        """不保存也能回测：body 带完整策略定义与合约（AI 比较“改前 / 改后”用）。口径同
        POST /api/strategies/{id}/backtest；只读，不保存策略。preview（策略编辑卡的实时回测）只算不存，
        结果另带 planSummary（这份计划的一句话）；diagnosis 里每个开仓组有各条件单独满足次数与组的
        同时满足（allPass）/ 任一满足（anyPass）次数。"""
        user = await current_user(request)
        result = await run_rule_backtest(request, user, raw_plan=body.plan.model_dump(), options=body,
                                         symbol=body.symbol, persist=not body.preview,
                                         identity={"strategyId": body.strategyId, "strategyName": body.plan.name,
                                                   "version": None})
        if body.preview:
            result = {**result, "planSummary": strategy_summary(definition_for(body.plan), symbol=body.symbol)}
        return json_bytes(result)

    @application.post("/api/strategy-backtests/inputs")
    async def browser_inputs(request: Request, body: BrowserInputsRequest) -> Response:
        """本机回测的输入（界面默认在本机浏览器里算，2026-10-12）：这次回测要读的全部——K线、合约乘数与最小
        变动、手续费（同服务端口径）、交易时段、初始资金、滑点——一次给齐（gzip），带 inputDigest 与
        engineDigest。给 plan 按这份定义（同 POST /api/strategy-backtests），只给 strategyId 按已存策略（同
        POST /api/strategies/{id}/backtest）。只读历史、不计算，但读历史占一个回测队列名额（同服务端回测的并发上限，排队超时 429）；
        服务端记下这次输入 15 分钟（返回 ticketId），算完用 POST /api/strategy-backtests/browser-result 交回。AI 与 CLI 直接用服务端回测接口，不用这一对。"""
        from .strategy_lab import engine_bundle
        user = await current_user(request)
        if not browser_backtests_enabled():
            raise HTTPException(503, "browser_backtests_disabled")
        options: BacktestOptions = body
        if body.plan is not None:
            raw, symbol = body.plan.model_dump(), body.symbol
            identity = {"strategyId": body.strategyId, "strategyName": body.plan.name, "version": None}
        else:
            raw, options, symbol, identity = stored_strategy_backtest(user, str(body.strategyId), body)
        plan, symbol, source, schedule = await resolve_backtest(request, user, raw_plan=raw, options=options,
                                                                symbol=symbol)
        started = time.monotonic()

        def read_inputs():
            setup = prepare_setup(source, plan=plan, symbol=symbol, fee_schedule=schedule,
                                  trading_days=options.tradingDays, slippage_ticks=options.slippageTicks,
                                  initial_balance=options.initialBalance)
            inputs = setup_inputs(setup)
            return setup, inputs, inputs_digest(inputs)

        # Reading the history is a backtest-sized job: it takes a queue slot like the server's own run.
        try:
            async with queue.slot(user, request_id=options.requestId, label={
                    "strategyId": identity.get("strategyId"), "strategyName": identity.get("strategyName") or plan.name,
                    "symbol": symbol}) as slot:
                try:
                    slot.work = asyncio.create_task(asyncio.to_thread(read_inputs))
                    setup, inputs, digest = await asyncio.shield(slot.work)
                except (KeyError, StrategyRuleError, BacktestDataError, ValidationError, ValueError,
                        RuntimeError) as error:
                    raise history_errors(error) from error
        except BacktestQueueTimeout as error:
            raise HTTPException(429, str(error)) from error
        ticket = Ticket(user=user, digest=digest, plan=raw, symbol=setup.symbol, identity=identity,
                        options=options.model_dump(include=set(BacktestOptions.model_fields)),
                        persist=not body.preview, preview=body.preview,
                        fee_source=str(schedule.get("source", "account")),
                        load_ms=round((time.monotonic() - started) * 1000))
        tickets.issue(ticket)
        payload = {"engineDigest": engine_bundle()["digest"], "inputDigest": digest, "ticketId": ticket.id,
                   "inputs": inputs}
        text = await asyncio.to_thread(lambda: json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                                          allow_nan=False, default=str).encode())
        headers = {"Cache-Control": "no-store"}
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(await asyncio.to_thread(gzip.compress, text, 6), media_type="application/json",
                            headers={**headers, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(text, media_type="application/json", headers=headers)

    @application.post("/api/strategy-backtests/browser-result")
    async def browser_result(request: Request, body: BrowserResultRequest):
        """交回本机算完的回测报告：服务端按发出输入时记下的策略、合约与参数包装成与服务端回测同样的结果
        （backtestId、AI 怎么看的事实句；非 preview 的存库），带 source: browser 与两个摘要。引擎已更新返回
        409 engine_changed（刷新页面后重跑），这次输入服务端已不记得（重启、超过 15 分钟）返回 409
        inputs_expired（改走服务端回测）。每次 inputs 发一张带 ticketId 的票据，同一张票据重复交回（回包丢了
        的重试）返回第一次的同一份结果，不会再存一条、再发一张卡。本机结果只用于展示：存为策略后的回测由
        服务端重跑。"""
        from .strategy_lab import engine_bundle
        user = await current_user(request)
        if body.engineDigest != engine_bundle()["digest"]:
            raise HTTPException(409, "engine_changed")
        ticket = tickets.get(user, body.ticketId)
        if ticket is None or ticket.digest != body.inputDigest:
            raise HTTPException(409, "inputs_expired")

        async def settle() -> str:
            options = BacktestOptions.model_validate(ticket.options)
            plan = plan_for_backtest(ticket.plan, options.timeframe)
            report = {key: value for key, value in body.result.items() if key not in SERVER_FIELDS}
            result = {**report, "symbol": ticket.symbol, "timeframeSeconds": plan.timeframeSeconds,
                      "planName": plan.name, "feeSource": ticket.fee_source,
                      "timings": {"loadMs": ticket.load_ms, "runMs": body.runMs},
                      "source": "browser", "inputDigest": ticket.digest, "engineDigest": body.engineDigest}
            checks.remember(user, ticket.digest, result.get("summary") or {})
            result = await finish_result(user, result, plan_name=plan.name, options=options,
                                         identity=ticket.identity, persist=ticket.persist)
            if ticket.preview:
                result = {**result, "planSummary": strategy_summary(
                    definition_for(StrategyPlan.model_validate(ticket.plan)), symbol=ticket.symbol)}
            return json_text(result)      # the reply's text is what a retry gets back, not the whole dict

        # One outcome per ticket: a retry after a lost reply (or two at once) gets the first result back
        # instead of a second stored backtestId and conversation card. A failed attempt may be retried.
        if ticket.outcome is None:
            ticket.outcome = asyncio.ensure_future(settle())

            ticket.outcome.add_done_callback(tickets.settled(ticket))
        return Response(await asyncio.shield(ticket.outcome), media_type="application/json")

    @application.get("/api/strategy-backtests")
    async def recent_backtests(request: Request, strategyId: str | None = Query(default=None, max_length=100),
                               limit: int = Query(default=5, ge=1, le=5)):
        """本人最近的规则回测结果（存库：每条策略留最近 5 次，未保存的方案与模板预览共留 5 次；新的在前，
        重启不丢）。strategyId 只看这条策略的；存下的逐笔清单只留最近 300 笔（摘要按全部成交算）。"""
        user = await current_user(request)
        return json_bytes({"items": await asyncio.to_thread(
            store.list_backtest_results, user_id=user, strategy_id=strategyId, limit=limit)})

    @application.post("/api/strategy-backtests/{backtest_id}/verdict")
    async def backtest_verdict(backtest_id: str, request: Request):
        """回测结果的“AI 怎么看”：程序先从这次结果算出事实句（最赚钱那个月占多少、去掉它还剩多少、笔数
        太少、没扣手续费、参数是不是在同一段行情里挑的），再请模型用一两句大白话说出来；模型的话里每个
        数字都必须是事实句里的数字，不符、没有模型或出错时只给事实句（source: facts）。写好后存回这次
        结果，再调直接返回存下的那句。只读回测，不改策略、不下单。"""
        user = await current_user(request)
        try:
            result = await asyncio.to_thread(store.get_backtest_result, backtest_id, user_id=user)
        except KeyError:
            raise HTTPException(404, "没有这次回测") from None
        verdict = result.get("verdict") or {"facts": []}
        if verdict.get("text"):
            return verdict
        generator = fixed_verdict_model(application) or (
            adapter.adapter_for_user(user) if hasattr(adapter, "adapter_for_user") else adapter)
        sentence = await model_verdict(generator, verdict, user_id=user)
        verdict = {**verdict, **sentence}
        await asyncio.to_thread(store.update_backtest_result, backtest_id, user_id=user, changes={"verdict": verdict})
        return verdict

    @application.get("/api/strategy-backtests/queue")
    async def backtest_queue(request: Request, requestId: str | None = Query(default=None, max_length=64)):
        """本人正在排队或运行的规则回测：state queued / running，ahead = 前面还有几个（含正在运行的）。
        默认全局同时最多 2 个、每人 1 个（部署可用 AI_TRADER_BACKTEST_* 调整，当前值见 limits），先到先跑；
        排队超过等待时限（默认 60 秒）放弃并返回 429“回测排队中，稍后再试”。"""
        user = await current_user(request)
        return {"items": queue.items(user, requestId), "limits": queue.limits()}

    # 策略模板库 (策略积木·第二期 A, 2026-09-25): classic strategies as ordinary plans. Loading
    # validates every template now — an invalid one stops the Runtime from starting.
    from . import strategy_templates as library_templates
    library_templates.templates()

    def template_or_404(template_id: str):
        template = library_templates.templates().get(template_id)
        if template is None:
            raise HTTPException(404, "模板不存在")
        return template

    @application.get("/api/strategy-templates")
    async def list_templates(request: Request):
        """模板库：分类、一句话场景、大白话 plainSummary、level（新手 / 进阶）、holding（日内 / 会隔夜）、来源、推荐执行周期、
        可调参数表（范围、步长、映射到 plan 的字段、help = {what, up, down} 大白话说明）。"""
        await current_user(request)
        return {"categories": [{"id": key, "label": label} for key, label in library_templates.CATEGORIES.items()],
                "items": [library_templates.summary(item) for item in library_templates.templates().values()]}

    @application.get("/api/strategy-templates/{template_id}")
    async def get_template(template_id: str, request: Request):
        """一个模板的完整定义：列表字段 + 默认参数 + 按默认参数实例化的完整策略定义（plan）。"""
        await current_user(request)
        return library_templates.detail(template_or_404(template_id))

    @application.post("/api/strategy-templates/{template_id}/preview")
    async def preview_template(template_id: str, request: Request, body: TemplatePreviewRequest):
        """按参数实例化（不保存），并在 symbol 上跑一次规则回测（口径同 POST /api/strategy-backtests）。
        参数越界返回 422 与中文原因。只读，不下单、不保存策略。"""
        user = await current_user(request)
        template = template_or_404(template_id)
        try:
            plan, values = library_templates.build_plan(template, body.params, timeframe=body.timeframe)
        except library_templates.TemplateError as error:
            raise HTTPException(422, str(error)) from error
        result = await run_rule_backtest(request, user, raw_plan=plan.model_dump(), options=body, symbol=body.symbol,
                                         identity={"strategyId": None, "strategyName": plan.name, "version": None,
                                                   "templateId": template.id})
        return json_bytes({"templateId": template.id, "templateName": template.name, "params": values,
                           "plan": plan.model_dump(), "backtest": result})

    async def instantiate_strategy(request: Request, user: str, template, body: TemplateInstantiateRequest, *,
                                   plan_override: StrategyPlan | None = None, changes: list[str] | None = None) -> dict:
        """One library draft from a template (the instantiate route; 恢复模板默认 with baseStrategyId).
        `plan_override` is a plan changed beyond the parameter table: it is saved as that ordinary
        definition, and templateSource records the template, parameters and the changes."""
        base_name = None
        if body.baseStrategyId:
            try:
                base = store.get_strategy_draft(body.baseStrategyId, user_id=user)
            except KeyError:
                raise HTTPException(404, "策略不存在") from None
            if (base["definition"].get("templateSource") or {}).get("templateId") != template.id:
                raise HTTPException(422, "这条策略不是从这个模板创建的")
            base_name = base["name"]
        try:
            plan, values = library_templates.build_plan(template, body.params, timeframe=body.timeframe)
            scope = (body.instrumentScope or (InstrumentScope(mode="selected", symbols=[strategy_scope_symbol(body.symbol)])
                                              if body.symbol else InstrumentScope()))
        except library_templates.TemplateError as error:
            raise HTTPException(422, str(error)) from error
        except (ValueError, ValidationError) as error:
            raise HTTPException(422, "适用品种无效，例如 SHFE.rb 或 SHFE.rb2610") from error
        origin = library_templates.template_source(template, values)
        if plan_override is not None:
            plan = plan_override
            origin = {**origin, "defaults": False, "modified": True, "changes": list(changes or [])[:20]}
        elif changes:
            origin = {**origin, "changes": list(changes)[:20]}
        extra = {"templateSource": origin}
        try:
            if base_name is not None:
                # 恢复模板默认: the next version of that strategy, under its own name.
                return await save_library_plan(request, user, plan.model_copy(update={"name": base_name}),
                                               scope.model_dump(), extra)
            name = body.name
            if name is None:
                named = body.chartSymbol or (body.symbol if body.symbol and re.search(r"\d", body.symbol) else None)
                source = ((await ctx.account_context(request, user)).account.provider if ctx is not None else provider)
                contract = await asyncio.to_thread(contract_label, source, named) if named else None
                title = template.name + ("（已调整）" if plan_override is not None else "")
                name = " · ".join(part for part in (title, contract, period_name(plan.timeframeSeconds)) if part)
            named_plan = plan.model_copy(update={"name": name})
            definition = {**definition_for(named_plan), "instrumentScope": scope.model_dump(), **extra}
            digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
            existing = store.find_library_strategy(user_id=user, digest=digest)
            if existing is not None:
                return existing          # the same template, parameters and name: that strategy, not a copy
            unique = unique_strategy_name(store, user, name)
            return await save_library_plan(request, user, plan.model_copy(update={"name": unique}),
                                           scope.model_dump(), extra)
        except (KeyError, ValueError, ValidationError) as error:
            raise HTTPException(422, "策略格式无效，请检查名称、合约和规则") from error

    @application.post("/api/strategy-templates/{template_id}/instantiate", status_code=201)
    async def instantiate_template(template_id: str, request: Request, body: TemplateInstantiateRequest):
        """按参数创建一条策略库草稿（与 POST /api/strategy-plans 同一流程），definition.templateSource
        记录来源模板与参数；同一定义重复创建返回已有的那条。返回保存的策略（含 strategyId）。"""
        user = await current_user(request)
        return await instantiate_strategy(request, user, template_or_404(template_id), body)

    # 策略实验室 (2026-09-30) reuses these exact paths: the same queue and fee rule for its
    # authoritative choose-backtest, the same save flow for 存为我的策略.
    application.state.run_rule_backtest = run_rule_backtest
    application.state.save_library_plan = save_library_plan
    application.state.instantiate_strategy = instantiate_strategy

    @application.get("/api/trade-review")
    async def trade_review(request: Request, symbol: str | None = Query(default=None),
                           days: int = Query(default=30, ge=1, le=366)):
        user = await current_user(request)
        # 4.3: the review follows the request's account (header/session), so it
        # never reports the default account's trades under another account.
        # 2026-09-23 isolation: the fills persist and read under that same
        # account id, not the store default.
        account = ((await ctx.account_context(request, user)).account
                   if ctx is not None else None)
        desk = ctx.services.research_desk if ctx is not None else None
        try:
            review = await account_trade_review(
                store, account.provider if account is not None else provider, fees, user,
                symbol=symbol, days=days,
                trading_account_id=account.account_id if account is not None else None,
                desk=desk)
        except (KeyError, ValueError, RuntimeError) as error:
            raise HTTPException(409, "成交或合约参数尚不可用") from error
        if ctx is None or account is None or not symbol:
            return review
        return await with_foreign_review(review, store, fees, user, accounts=ctx.services.accounts,
                                         account_id=account.account_id, symbol=symbol, days=days, desk=desk)
