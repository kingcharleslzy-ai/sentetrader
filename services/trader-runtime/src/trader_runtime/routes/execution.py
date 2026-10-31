"""Simulation orders, AI execution controls and stops, trade proposals."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, status

from ..autonomy import AutonomyError
from ..execution_stop import lift_companion_stop, stop_execution
from ..instruments import foreign_product_of, is_foreign_paper
from ..order_intent import from_close_batch as intent_from_close_batch
from ..order_intent import from_target_position as intent_from_target_position
from ..order_intent import from_trade_order as intent_from_trade_order
from ..order_planner import Ask, Cards, Refuse
from ..simulation_orders import SimulationOrderError, order_is_active
from ..strategy_monitor import link_confirmed_strategy_card
from ..trader_tools import TraderReadOnlyTools, account_card_tools, foreign_main_symbol
from ..wiring.accounts import ACCOUNT_HEADER, require_account_write
from ..wiring.context import RuntimeContext
from .requests import (
    AiExecutionLimitsRequest,
    AiExecutionResumeRequest,
    AiExecutionStartRequest,
    AiExecutionStopRequest,
    AiStopLossRequest,
    AiStopRepriceRequest,
    CloseBatchProposalRequest,
    ConfirmRequest,
    MutationRequest,
    TargetPositionProposalRequest,
    TradeProposalRequest,
)

log = logging.getLogger("uvicorn.error.execution")

# The order-recovery poll cadence (production 1 s). A module constant, not an env
# knob: tests monkeypatch it (like voice_realtime's bounds) instead of growing the
# deployed configuration surface.
ORDER_RECOVERY_INTERVAL_SECONDS = 1.0


def proposal_account(proposal: dict[str, object]) -> str:
    """The account word a candidate card's message names: 本站模拟 for a paper
    account's card, 快期模拟 otherwise (a card from before the field existed)."""
    return "本站模拟" if proposal.get("accountLabel") == "本站模拟账户" else "快期模拟"


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    services = ctx.services
    agent = ctx.agent
    store = services.store
    broker = services.broker
    proposals = services.proposals
    runs = agent.runs
    current_user = ctx.current_user
    require_session = ctx.require_session
    proposal_error = ctx.proposal_error
    _replay_provider = ctx.replay_provider

    def simulation_error(error: Exception) -> HTTPException:
        if isinstance(error, KeyError):
            return HTTPException(status_code=404, detail="simulation_order_not_found")
        if isinstance(error, SimulationOrderError):
            return HTTPException(status_code=409, detail={"code": str(error)})
        return HTTPException(
            status_code=503, detail={"code": "simulation_gateway_unavailable"}
        )

    @application.get("/api/simulation/orders")
    async def list_simulation_orders(
        request: Request, recoveryWait: bool = Query(default=True), active: bool = Query(default=False),
    ) -> dict[str, object]:
        user_id = await current_user(request)
        simulation_orders = (await ctx.account_context(request, user_id)).account.simulation_orders
        context = getattr(application.state, "replay_context", None)
        run_round = None
        items = None
        if context is not None:
            authorize = request.scope.get("ai_trader.replay_authorize")
            if authorize is None:
                identity = application.state.app_capabilities.capture_identity(user_id, request.headers)
                authorize = identity.verify

            async def run_round(call):
                async def recover_and_snapshot():
                    nonlocal items
                    completed = await call()
                    items = simulation_orders.list(user_id=user_id)
                    return completed
                return await context.call_tool(user_id, recover_and_snapshot, authorize=authorize)

        await simulation_orders.recover_once(
            user_id=user_id,
            attempts=180 if recoveryWait else 1,
            # The multi-round wait paces itself at the production cadence; tests
            # monkeypatch the constant instead of sleeping the real interval.
            interval=(ORDER_RECOVERY_INTERVAL_SECONDS if recoveryWait else 0),
            run_round=run_round,
        )
        if items is None:
            # Disabled gateways perform no recovery rounds. Even that final
            # read must finish before a replay close can dispose of its store.
            async def read_orders():
                return simulation_orders.list(user_id=user_id)
            items = (await context.call_tool(user_id, read_orders, authorize=authorize)
                     if context is not None else await read_orders())
        if active:
            items = [item for item in items if order_is_active(item)]
        return {"items": items}

    def autonomy_error(error: Exception) -> HTTPException:
        if isinstance(error, KeyError):
            return HTTPException(status_code=404, detail={"code": "autonomy_record_not_found"})
        if isinstance(error, (AutonomyError, SimulationOrderError)):
            code = str(error)
            # 4.3 执行归属与角色：权限边界是请求被拒，不是执行冲突。
            if code in {"not_execution_owner", "account_read_only", "account_not_granted"}:
                return HTTPException(status_code=403, detail=code)
            # A validation refusal carries its Chinese reason ("止损要低于现价 3,112"): 422, not a
            # conflict — the request was well-formed, the numbers were wrong.
            message = getattr(error, "message", None)
            if message is not None:
                return HTTPException(status_code=422, detail={"code": code, "message": message})
            return HTTPException(status_code=409, detail={"code": code})
        return HTTPException(status_code=503, detail={"code": "ai_execution_unavailable"})

    @application.get("/api/simulation/order-estimate")
    async def get_simulation_order_estimate(
        request: Request,
        symbol: str = Query(min_length=1, max_length=40),
        side: str = Query(pattern="^(buy|sell)$"),
        offset: str = Query(pattern="^(open|close)$"),
        volume: int = Query(ge=1, le=1000),
        price: float | None = Query(default=None, gt=0),
        position_bucket_camel: str | None = Query(
            default=None, pattern="^(today|historical|all)$", alias="positionBucket"),
        position_bucket: str | None = Query(default=None, pattern="^(today|historical|all)$"),
    ) -> dict[str, object]:
        """下单前的“钱”估算（只读，不做校验或拦截）：开仓给保证金约/手续费约/可开手数，
        平仓给释放保证金约与今昨拆分的手续费（`positionBucket` 省略 = all，按账户平今平昨
        偏好拆；`position_bucket` 下划线写法兼容旧调用方，两写法同值只算一个，冲突返回 422）。
        缺数据的字段为 null 并带 `missing` 原因码。"""
        # The wire name is positionBucket (the tool and the docs say so); the
        # snake_case spelling stays accepted for older callers. Divergent values
        # are an explicit 422 — never a silent pick of one of them.
        if (position_bucket_camel is not None and position_bucket is not None
                and position_bucket_camel != position_bucket):
            raise HTTPException(status_code=422, detail={"code": "position_bucket_conflict"})
        position_bucket = position_bucket_camel if position_bucket_camel is not None else position_bucket
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        provider = context.account.provider
        foreign = foreign_product_of(symbol)
        if foreign is not None and foreign.mt5 and is_foreign_paper(context.account.account_id) \
                and symbol.upper().startswith("KQD."):
            # 已在美元子账户、窗口开着外盘图表：估的是该品种主力月份那一单（与出卡同一换法）。
            main_month = await asyncio.to_thread(foreign_main_symbol, provider, foreign)
            symbol = main_month or symbol
        elif foreign is not None and foreign.mt5 and not is_foreign_paper(context.account.account_id):
            # 外盘二期：外盘图表的下单票估的是美元子账户上主力月份那一单（出卡也落在那里）。
            resolver = services.accounts.foreign_account
            foreign_id = await resolver(user_id) if resolver is not None else None
            account = services.accounts.get(foreign_id) if foreign_id else None
            if account is None:
                raise HTTPException(status_code=503, detail={
                    "code": "estimate_unavailable", "reason": "foreign_account_unavailable"})
            provider = account.provider
            if symbol.upper().startswith("KQD."):
                main = await asyncio.to_thread(foreign_main_symbol, provider, foreign)
                if main is None:
                    raise HTTPException(status_code=503, detail={
                        "code": "estimate_unavailable", "reason": "foreign_main_unavailable"})
                symbol = main
        try:
            return proposals.order_estimate(
                user_id=user_id, symbol=symbol, side=side, offset=offset,
                volume=volume, price=price, provider=provider,
                position_bucket=position_bucket,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="symbol_not_found") from error
        except RuntimeError as error:
            raise HTTPException(
                status_code=503, detail={"code": "estimate_unavailable", "reason": str(error)},
            ) from error

    @application.get("/api/ai-execution")
    async def get_ai_execution(request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        autonomy = context.account.autonomy
        return {
            "authorization": autonomy.current(user_id=user_id),
            "controls": autonomy.controls(user_id=user_id),
            "stops": await _ledger_stops(request, user_id, context),
            "simulationOnly": True,
            "liveTrading": "disabled",
        }

    @application.post("/api/ai-execution/limits")
    async def update_ai_execution_limits(request: Request, body: AiExecutionLimitsRequest) -> dict[str, object]:
        user_id = await current_user(request)
        autonomy = (await ctx.account_context(request, user_id)).account.autonomy
        try:
            return autonomy.update_limits(user_id=user_id, revision=body.revision,
                max_order_volume=body.maxOrderVolume, max_orders=body.maxOrders)
        except Exception as error:
            raise autonomy_error(error) from error

    @application.post("/api/ai-execution/start")
    async def start_ai_execution(
        request: Request, body: AiExecutionStartRequest,
    ) -> dict[str, object]:
        user_id = await current_user(request)
        # A tier without autonomous execution cannot arm it (the app-level
        # write gate); stopping stays open to everyone so a left-over
        # authorization can always be ended.
        require_session(body.sessionId, user_id)
        autonomy = (await ctx.account_context(request, user_id, session_id=body.sessionId)).account.autonomy
        try:
            result = autonomy.activate(
                user_id=user_id,
                session_id=body.sessionId,
                symbols=body.symbols,
                permissions=list(body.permissions),
                max_order_volume=body.maxOrderVolume,
                max_orders=body.maxOrders,
                duration_minutes=body.durationMinutes,
            )
            # 停的时候美元子账户一并停了：用户明确恢复时一并解除（只解除停止，不替它开授权）。
            await lift_companion_stop(accounts=ctx.services.accounts, user_id=user_id, account_id=autonomy.account_id)
            return result
        except Exception as error:
            raise autonomy_error(error) from error

    @application.post("/api/ai-execution/resume")
    async def resume_ai_execution(request: Request, body: AiExecutionResumeRequest) -> dict[str, object]:
        """界面“恢复执行”按钮（用户本人点的）：同 resume_simulation_execution 的恢复，再解除美元子账户被一并停下的
        停止（#932，所有者 10-30）。AI 自己发起的恢复仍走工具，不替美元子账户解除。"""
        user_id = await current_user(request)
        require_session(body.sessionId, user_id)
        autonomy = (await ctx.account_context(request, user_id, session_id=body.sessionId)).account.autonomy
        try:
            result = autonomy.resume_runtime_access(user_id=user_id, session_id=body.sessionId, symbol=body.symbol)
            await lift_companion_stop(accounts=ctx.services.accounts, user_id=user_id, account_id=autonomy.account_id)
            return result
        except Exception as error:
            raise autonomy_error(error) from error

    @application.post("/api/ai-execution/stop")
    async def stop_ai_execution(
        request: Request, body: AiExecutionStopRequest,
    ) -> dict[str, object]:
        user_id = await current_user(request)
        account = (await ctx.account_context(request, user_id)).account
        try:
            # 同一入口（execution_stop）：人民币本站模拟账户连同它的美元子账户一并停。
            return await stop_execution(autonomy=account.autonomy, proposals=proposals, accounts=ctx.services.accounts,
                                        user_id=user_id, emergency=body.emergency, scope=body.scope)
        except Exception as error:
            raise autonomy_error(error) from error

    @application.get("/api/ai-execution/stops")
    async def list_ai_stops(request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        return {"items": await _ledger_stops(request, user_id, await ctx.account_context(request, user_id))}

    async def _ledger_stops(request: Request, user_id: str, context: Any) -> list[dict[str, Any]]:
        """本站模拟两本账（2026-10-22）：持仓面板的保护列表连同美元子账户（外盘）的一起，美元那几条带 accountId——
        外盘仓的止损止盈在人民币会话里也看得到、撤得掉。子账户此刻读不到时只列本账户的。"""
        stops = context.account.autonomy.list_stops(user_id=user_id)
        account_id = str(context.account.account_id)
        foreign_of = getattr(application.state.accounts, "foreign_account_of", None)
        foreign = foreign_of(account_id) if foreign_of is not None and account_id.startswith("paper:") else None
        if foreign:
            try:
                usd = await ctx.account_context(request, user_id, account_id=foreign)
                stops = [*stops, *({**stop, "accountId": foreign} for stop in usd.account.autonomy.list_stops(user_id=user_id))]
            except Exception:
                log.warning("foreign_ledger_stops_unreadable account=%s", foreign, exc_info=True)
        return stops

    @application.post(
        "/api/ai-execution/stops", status_code=status.HTTP_201_CREATED,
    )
    async def create_ai_stop(
        request: Request, body: AiStopLossRequest,
    ) -> dict[str, object]:
        user_id = await current_user(request)
        require_session(body.sessionId, user_id)
        # 两本账：外盘仓的保护挂在美元子账户上（与外盘下单同一规则 account_for_symbol）。
        autonomy = (await ctx.account_for_symbol(request, user_id, body.symbol, session_id=body.sessionId)).account.autonomy
        try:
            return await autonomy.create_stop(
                user_id=user_id,
                session_id=body.sessionId,
                symbol=body.symbol,
                position_direction=body.positionDirection,
                position_bucket=body.positionBucket,
                volume=body.volume,
                trigger_price=body.triggerPrice,
                take_profit_price=body.takeProfitPrice,
                price_intent=body.priceIntent,
                trailing=body.trailing.model_dump() if body.trailing is not None else None,
                breakeven_after_r=bool(body.breakevenAtR),
            )
        except Exception as error:
            raise autonomy_error(error) from error

    async def stop_owner_autonomy(request: Request, user_id: str, stop_id: str) -> Any:
        # 两本账：动的是哪本账的保护就在那本账上动（列表里美元那几条带 accountId）。
        row = store.connection.execute("SELECT trading_account_id FROM ai_stop_orders WHERE id = ? AND user_id = ?",
                                       (stop_id, user_id)).fetchone()
        owner = str(row[0]) if row is not None and row[0] else None
        return (await ctx.account_context(request, user_id, account_id=owner)).account.autonomy

    @application.post("/api/ai-execution/stops/{stop_id}/cancel")
    async def cancel_ai_stop(request: Request, stop_id: str) -> dict[str, object]:
        user_id = await current_user(request)
        autonomy = await stop_owner_autonomy(request, user_id, stop_id)
        try:
            return autonomy.cancel_stop(user_id=user_id, stop_id=stop_id)
        except Exception as error:
            raise autonomy_error(error) from error

    @application.post("/api/ai-execution/stops/{stop_id}/reprice")
    async def reprice_ai_stop(request: Request, stop_id: str, body: AiStopRepriceRequest) -> dict[str, object]:
        """图上拖线改价 (2026-10-24): the user's own stop / take moved in place after the chart's confirm card."""
        user_id = await current_user(request)
        autonomy = await stop_owner_autonomy(request, user_id, stop_id)
        try:
            return await autonomy.reprice_stop(user_id=user_id, stop_id=stop_id, level=body.level,
                                               previous=str(body.previous), price=body.price)
        except Exception as error:
            raise autonomy_error(error) from error

    # 保本提醒 (2026-10-06): the open questions and their answers. 移到成本 moves the position's own
    # stops to the cost (止盈不动) after a fresh price check — the user's own action, never an order.
    @application.get("/api/ai-execution/breakeven")
    async def list_breakeven(request: Request) -> dict[str, object]:
        user_id = await current_user(request)
        from ..breakeven import notices
        return {"items": notices(application, user_id=user_id)}

    async def _answer_breakeven(request: Request, notice_id: str, choice: str) -> dict[str, object]:
        user_id = await current_user(request)
        from ..breakeven import BreakEvenAnswerError, answer
        try:
            return await answer(application, user_id=user_id, notice_id=notice_id, choice=choice)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="breakeven_not_found") from error
        except BreakEvenAnswerError as error:
            raise HTTPException(status_code=409, detail={"code": error.code, "message": error.message}) from error

    @application.post("/api/ai-execution/breakeven/{notice_id}/move")
    async def move_breakeven(request: Request, notice_id: str) -> dict[str, object]:
        """保本提醒 · 移到成本 (the card's button, or a spoken 移)."""
        return await _answer_breakeven(request, notice_id, "move")

    @application.post("/api/ai-execution/breakeven/{notice_id}/decline")
    async def decline_breakeven(request: Request, notice_id: str) -> dict[str, object]:
        """保本提醒 · 不用: this position is not asked again today."""
        return await _answer_breakeven(request, notice_id, "decline")

    async def _account_tools(
        request: Request, user_id: str, session_id: str,
    ) -> tuple[TraderReadOnlyTools, str, str | None]:
        """The card environment of the account the card acts on (provider, status, proposal service, autonomy),
        the same for the three card routes: the account the caller named (账户头 / CLI `--account`), once the
        user is granted it and may write; else the session's own account. A named account that differs from the
        session's wins — it is the caller's intent — and the card says so (`accountOverride`). The third value is
        the account the caller named, also when it is the session's own (#932: a 目标持仓 plan records it as
        accountExplicit, so its next leg stays on that account if the conversation switches); None when nothing
        was named and the card follows the session. A viewer account makes no card."""
        session = require_session(session_id, user_id)
        session_account = str(session.trading_account_id or "") or None
        named = bool((request.headers.get(ACCOUNT_HEADER) or "").strip())
        # The header goes through the same grant check as every other route (resolve_account); no header →
        # the session's account.
        context = await ctx.account_context(request, user_id, session_id=session_id,
                                            account_id=None if named else session_account)
        require_account_write(context)
        override = context.account.account_id if named and context.account.account_id != session_account else None
        if override is not None:
            def status(market_mode: str) -> dict[str, object]:
                return ctx.merge_paper_account_status(ctx.status_for_user(user_id, str(session.mode)), override)
            tools = account_card_tools(application, user_id, context.account, status)
            return tools, str(session.mode), override
        # The trading AI's own tools for this account (the same gate status as a typed order); an adapter
        # without per-account tools gets the account's provider and autonomy (without the autonomy a cloud
        # session's card path read "no simulation gateway", audit 10-13) and the session's own status.
        tools = account_card_tools(application, user_id, context.account,
                                   lambda market_mode: ctx.status_for_session(session_id))
        return tools, str(session.mode), context.account.account_id if named else None

    def _refusal_error(outcome: Ask | Refuse) -> HTTPException:
        """A card route's refusal: the builder's own exception maps exactly as before (code / status); the
        planner's own refusals (就绪门禁、没有持仓、可平量、挂单、读不到账户) are 409 with the user's line."""
        if isinstance(outcome, Ask):
            return HTTPException(status_code=422, detail={"code": f"order_{outcome.missing}_missing",
                                                          "message": outcome.question})
        if outcome.error is not None:
            if isinstance(outcome.error, KeyError):
                return HTTPException(status_code=404, detail="symbol_not_found")
            return proposal_error(outcome.error)
        facing = outcome.facing() or {}
        issues = [str(issue) for issue in facing.get("candidateIssues") or []]
        return HTTPException(status_code=409, detail={
            "code": outcome.code,
            **({"message": "；".join(issues)} if issues else {}),
            **({"reasons": list(outcome.reasons), "userFacing": facing} if outcome.form == "gate" else {}),
        })

    async def _publish_cards(session_id: str, turn_id: str, outcome: Cards, *, text: str | None = None) -> None:
        for card in outcome.cards:
            proposal = card.proposal
            if text is not None:
                line = text
            elif card.event == "proposal.close_batch":
                line = f"请核对这 {proposal['totalOrders']} 张{proposal_account(proposal)}平仓候选。确认前不会发送委托。"
            elif _replay_provider is not None:
                line = "请核对这张历史回放交易候选。确认后仅发送到独立 TqSim。"
            else:
                line = f"请核对这张{proposal_account(proposal)}交易候选。确认前不会发送委托。"
            override = proposal.get("accountOverride") or {}
            if override.get("accountId"):
                line = f"{line}（按指定账户 {override['accountId']} 出卡，不是这个对话的账户）"
            await broker.publish(session_id, kind="message", name="message.completed", payload={
                "turnId": turn_id, "role": "assistant", "text": line, "agentId": "native"})
            await broker.publish(session_id, kind="approval", name=card.event, payload=proposal)

    @application.post(
        "/api/simulation/proposals", status_code=status.HTTP_201_CREATED,
    )
    async def create_simulation_proposal(
        request: Request, body: TradeProposalRequest,
    ) -> dict[str, object]:
        """单笔出卡（下单票、外部 AI/CLI）：与内置 AI 的出卡工具同一个规划器——开仓过就绪门禁，上期所/能源中心
        平仓没给 positionBucket 时按“平今平昨”偏好自动分今昨（跨两侧返回一张批量平仓卡），没给价格时平仓对价、
        开仓按账号默认价格方式。"""
        user_id = await current_user(request)
        # 打法本 (2026-10-25): the setup this order is placed under must be the user's own.
        if body.playbookId is not None and store.get_playbook(body.playbookId, user_id=user_id) is None:
            raise HTTPException(status_code=422, detail="playbook_not_found")
        tools, mode, account_id = await _account_tools(request, user_id, body.sessionId)
        turn_id = str(uuid4())
        intent = intent_from_trade_order(
            symbol=body.symbol, side=body.side, volume=body.volume, price_intent=body.priceIntent,
            limit_price=body.limitPrice, bucket=body.positionBucket, reason=body.reason,
            protection=body.protection.model_dump() if body.protection is not None else None,
            remaining_protection=(body.remainingProtection.model_dump(exclude_unset=True)
                                  if body.remainingProtection is not None else None))
        outcome = await tools.plan_order(intent, mode=mode, session_id=body.sessionId, turn_id=turn_id,
                                         user_id=user_id, account_id=account_id,
                                         # 网页/桌面下单票与直接调本路由的外部调用方用同一种登录令牌，分不出来：只做
                                         # 下单安全检查；AI 决策就绪留给 AI 出卡工具（内置、语音、外部经能力目录）。
                                         gate="order_safety")
        if not isinstance(outcome, Cards):
            raise _refusal_error(outcome)
        if body.playbookId is not None:
            for card in outcome.cards:
                store.tag_proposal_playbook(str(card.proposal["proposalId"]), body.playbookId, user_id=user_id)
        await _publish_cards(body.sessionId, turn_id, outcome)
        return outcome.cards[0].proposal

    @application.post("/api/simulation/close-batch-proposals", status_code=status.HTTP_201_CREATED)
    async def create_close_batch_proposal(request: Request, body: CloseBatchProposalRequest) -> dict[str, object]:
        """一张卡平掉全部（或指定合约 / 方向的）持仓：一次确认，逐张发送、逐张回报。"""
        user_id = await current_user(request)
        tools, mode, account_id = await _account_tools(request, user_id, body.sessionId)
        turn_id = str(uuid4())
        outcome = await tools.plan_order(
            intent_from_close_batch(symbols=body.symbols, direction=body.direction, reason=body.reason),
            mode=mode, session_id=body.sessionId, turn_id=turn_id, user_id=user_id, account_id=account_id)
        if not isinstance(outcome, Cards):
            raise _refusal_error(outcome)
        await _publish_cards(body.sessionId, turn_id, outcome)
        return outcome.cards[0].proposal

    @application.post("/api/simulation/target-position/proposals", status_code=status.HTTP_201_CREATED)
    async def create_target_position_proposal(
        request: Request, body: TargetPositionProposalRequest,
    ) -> dict[str, object]:
        """目标持仓（下单台第三模式、快捷区反手）：按当前持仓算差额，只出第一段的确认卡；反向目标（反手）
        确认一次——第一段全部成交后接力用这次确认自动发第二段（发单时照常复查行情、跳价、涨跌停与持仓），
        部分成交或失败就不开仓。走与工具同一个规划器（就绪门禁/挂单/平今昨）。"""
        user_id = await current_user(request)
        tools, mode, account_id = await _account_tools(request, user_id, body.sessionId)
        if not tools._simulation_mode_allowed(mode):
            return {"cardIssued": False, "errorCode": "proposal_service_unavailable", "userFacing": None}
        turn_id = str(uuid4())
        outcome = await tools.plan_order(
            intent_from_target_position(symbol=body.symbol, target_net=body.targetNet,
                                        price_intent=body.priceIntent, reason=body.reason),
            # The opening gate stays the full one here (this route had it before the planner, #942).
            mode=mode, session_id=body.sessionId, turn_id=turn_id, user_id=user_id, account_id=account_id)
        if not isinstance(outcome, Cards):
            # Already at the target, or an honest refusal (no position, stale quote, open limit, working
            # orders): no card — the one user-facing line is the answer.
            refused = outcome if isinstance(outcome, Refuse) else Refuse(
                f"order_{outcome.missing}_missing", (outcome.question,))
            return {"cardIssued": False,
                    "errorCode": None if refused.form == "unchanged" else refused.code,
                    "userFacing": refused.facing()}
        card = outcome.cards[0]
        annotation = card.proposal.get("targetPosition") or {}
        step_note = (f"（第 {annotation.get('step', 1)} 步 / 共 {annotation.get('totalSteps', 1)} 步）"
                     if int(annotation.get("totalSteps") or 1) > 1 else "")
        await _publish_cards(body.sessionId, turn_id, outcome, text=(
            f"{annotation.get('summary', body.symbol)}{step_note}。"
            f"请核对这张{proposal_account(card.proposal)}交易候选，确认前不会发送委托。"))
        # approvalId / proposalVersion: the quick area's 反手 confirms this card right away (its own dialog was the
        # user's confirmation) through the ordinary confirm route.
        return {"cardIssued": True, "proposalType": card.event, "targetPosition": annotation,
                "proposalId": card.proposal.get("proposalId"), "approvalId": card.proposal.get("approvalId"),
                "proposalVersion": card.proposal.get("version")}

    @application.post("/api/simulation/orders/{client_order_id}/{action}")
    async def mutate_simulation_order(
        request: Request, client_order_id: str,
        action: Literal["continue", "abandon", "query", "cancel"],
    ) -> dict[str, object]:
        user_id = await current_user(request)
        context = await ctx.account_context(request, user_id)
        simulation_orders = context.account.simulation_orders
        if action != "query":
            # continue/abandon/cancel change the account's orders; query stays readable.
            require_account_write(context)
        try:
            if action == "continue":
                result = await simulation_orders.dispatch(client_order_id, user_id=user_id)
            elif action == "abandon":
                result = simulation_orders.abandon(client_order_id, user_id=user_id)
            elif action == "cancel":
                result = await simulation_orders.cancel(client_order_id, user_id=user_id)
            else:
                result = await simulation_orders.query(client_order_id, user_id=user_id)
            store.audit(
                result["proposalId"], f"simulation-{action}",
                {"clientOrderId": client_order_id, "displayStatus": result["displayStatus"]},
            )
            return result
        except Exception as error:
            raise simulation_error(error) from error

    @application.get("/api/tasks/{run_id}")
    async def get_task(request: Request, run_id: str) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            return store.get_run(run_id, user_id=user_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="task_not_found") from error

    @application.post("/api/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: str) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            return await runs.cancel(run_id, user_id=user_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="task_not_found") from error

    async def _proposal_account_context(request: Request, user_id: str, proposal_id: str):
        """The account a stored proposal belongs to: confirming or amending it acts
        on that account, whatever the request header says."""
        try:
            record = store.get(proposal_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="proposal_not_found") from error
        if record["userId"] != user_id:
            raise HTTPException(status_code=404, detail="proposal_not_found")
        return await ctx.account_context(request, user_id, account_id=record.get("tradingAccountId"))

    @application.post("/api/proposals/{proposal_id}/adjust-risk")
    async def adjust_trade_risk(request: Request, proposal_id: str, body: MutationRequest) -> dict[str, object]:
        user_id = await current_user(request)
        require_account_write(await _proposal_account_context(request, user_id, proposal_id))
        try:
            return await proposals.adjust_trade_risk(proposal_id, user_id=user_id,
                                                     proposal_version=body.proposalVersion,
                                                     idempotency_key=body.idempotencyKey)
        except Exception as error:
            raise proposal_error(error) from error

    @application.post("/api/proposals/{proposal_id}/confirm")
    async def confirm_proposal(request: Request, proposal_id: str, body: ConfirmRequest) -> dict[str, object]:
        received = time.monotonic()
        user_id = await current_user(request)
        require_account_write(await _proposal_account_context(request, user_id, proposal_id))
        try:
            result = await _confirm_one(proposal_id, user_id=user_id, approval_id=body.approvalId,
                                        proposal_version=body.proposalVersion, idempotency_key=body.idempotencyKey)
        except Exception as error:
            raise proposal_error(error) from error
        # 本站模拟两本账的全平（2026-10-21）：一起确认——关联的另一本账的平仓卡走同一条确认链（它自己的账户、
        # 版本与授权），各自回报；另一张出错只写在回报里，不撤回这一张已经发出的单。
        linked = await _each_linked(request, proposal_id, user_id=user_id, action="confirm", key=body.idempotencyKey)
        if linked:
            result = {**result, "linkedResults": linked}
        # The whole confirm on the server (auth, proposal checks, dispatch); order_timing splits dispatch.
        log.info("order_confirm_timing proposal=%s server_ms=%d", proposal_id, round((time.monotonic() - received) * 1000))
        return result

    async def _confirm_one(proposal_id: str, *, user_id: str, approval_id: str, proposal_version: int,
                           idempotency_key: str) -> dict[str, object]:
        result = await proposals.confirm(proposal_id, user_id=user_id, approval_id=approval_id,
                                         proposal_version=proposal_version, idempotency_key=idempotency_key)
        # 出卡确认 (2026-09-25): a strategy's card carries its order back onto the strategy's
        # attempt; confirming, risk and dispatch above are the ordinary card path, unchanged.
        try:
            link_confirmed_strategy_card(store, store.get(proposal_id), result, user_id=user_id)
        except (KeyError, RuntimeError, ValueError):
            log.warning("strategy_card_link_failed proposal=%s", proposal_id, exc_info=True)
        await _watch_confirmed_order(result, user_id=user_id, proposal_id=proposal_id)
        return result

    async def _each_linked(request: Request, proposal_id: str, *, user_id: str, action: str,
                           key: str) -> list[dict[str, object]]:
        """The cards linked to this one (全平两本账), confirmed or rejected with it: each still awaiting confirmation
        goes through its own account's check and the ordinary path, at its own version and approval."""
        try:
            linked_ids = list((store.get(proposal_id).get("payload") or {}).get("linkedProposalIds") or [])
        except KeyError:
            return []
        results: list[dict[str, object]] = []
        for other in linked_ids:
            try:
                record = store.get(str(other))
                if record.get("userId") != user_id or record.get("status") != "approval_required":
                    continue
                require_account_write(await _proposal_account_context(request, user_id, str(other)))
                if action == "confirm":
                    outcome = await _confirm_one(str(other), user_id=user_id, approval_id=str(record.get("approvalId")),
                                                 proposal_version=int(record["version"]),
                                                 idempotency_key=f"{key}:linked:{other}")
                else:
                    outcome = await proposals.reject(str(other), user_id=user_id, proposal_version=int(record["version"]),
                                                     idempotency_key=f"{key}:linked:{other}")
                results.append({"proposalId": other, "status": "ok", "result": outcome})
            except Exception as error:  # the other ledger's own refusal, reported beside this card's result
                log.warning("linked_proposal_%s_failed proposal=%s linked=%s", action, proposal_id, other, exc_info=True)
                detail = getattr(error, "detail", None) or str(error) or type(error).__name__
                results.append({"proposalId": other, "status": "failed", "error": str(detail)})
        return results

    async def _watch_confirmed_order(result: dict[str, object], *, user_id: str, proposal_id: str) -> None:
        """A confirmed simulation limit order that rests on the book gets the unfilled-order watchdog
        (09-21), whoever confirmed it — voice, a click, an external agent. Only the single-order trade
        path is reminded; a close batch's child orders join for their fact pushes alone (2026-10-08),
        so the card's per-order rows follow fills and cancels live. A 目标持仓 plan's confirmed card
        also links to its plan: the leg's orders (batch children included) join the watch so the
        relay sees the fill."""
        order = result.get("simulationOrder") if isinstance(result, dict) else None
        children = result.get("simulationOrders")
        resting = [item for item in ([order] if isinstance(order, dict) else [])
                   + (list(children) if isinstance(children, list) else [])
                   if isinstance(item, dict) and item.get("displayStatus") in {"已受理", "部分成交"}]
        if resting:
            try:
                record = store.get(proposal_id)
            except KeyError:
                record = None
            if record is not None:
                account_id = str(record.get("tradingAccountId") or application.state.accounts.default.account_id)
                session_id = str(record.get("sessionId") or "")
                name = str((record.get("payload") or {}).get("name") or "")
                single_id = str(order.get("clientOrderId") or "") if isinstance(order, dict) else ""
                for item in resting:
                    application.state.order_watch.register(
                        user_id=user_id, account_id=account_id, session_id=session_id, order=item,
                        name=name, remind=str(item.get("clientOrderId") or "") == single_id)
        order_watch = getattr(application.state, "order_watch", None)
        if order_watch is not None:
            await order_watch.link_confirmed_plan(result, user_id=user_id, proposal_id=proposal_id)

    @application.post("/api/proposals/{proposal_id}/reject")
    async def reject_proposal(request: Request, proposal_id: str, body: MutationRequest) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            result = await proposals.reject(proposal_id, user_id=user_id,
                                            proposal_version=body.proposalVersion,
                                            idempotency_key=body.idempotencyKey)
        except Exception as error:
            raise proposal_error(error) from error
        linked = await _each_linked(request, proposal_id, user_id=user_id, action="reject", key=body.idempotencyKey)
        return {**result, "linkedResults": linked} if linked else result

    def proposal_view(record: dict[str, Any]) -> dict[str, Any]:
        if isinstance((record.get("payload") or {}).get("remainingProtection"), dict):
            # 合并卡: the second step's current state rides on the card it belongs to.
            from ..card_protection import remaining_protection_view
            return {**record, "payload": remaining_protection_view(record, store.card_protection_step(record["id"]))}
        return record

    @application.get("/api/proposals")
    async def get_proposals(request: Request, ids: list[str] = Query(max_length=100)) -> dict[str, object]:
        """Several cards' records in one read (历史卡的结局: the conversation's old waiting cards settle from one
        request instead of one each). Only the caller's own; an id not listed is not theirs or does not exist."""
        user_id = await current_user(request)
        return {"items": [proposal_view(record) for record in store.get_many(list(dict.fromkeys(ids)), user_id=user_id)]}

    @application.get("/api/proposals/{proposal_id}")
    async def get_proposal(request: Request, proposal_id: str) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            record = store.get(proposal_id)
            if record["userId"] != user_id:
                raise KeyError(proposal_id)
            return proposal_view(record)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="proposal_not_found") from error
