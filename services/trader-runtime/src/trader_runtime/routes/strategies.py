"""Strategy drafts, monitoring and backtests (the strategy workbench registers its own routes here)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, status

from ..proposals import ProposalConflict
from ..skill_registry import human_request, require_skill
from ..wiring.accounts import require_account_write, require_execution_owner
from ..wiring.context import RuntimeContext
from .requests import (
    BacktestRequest,
    ReproducibleBacktestRequest,
    StrategyMonitorStartRequest,
    StrategyMonitorStopRequest,
    TqSdkBacktestRequest,
)


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    services = ctx.services
    agent = ctx.agent
    settings = ctx.settings
    store = services.store
    broker = services.broker
    adapter = agent.adapter
    proposals = services.proposals
    backtests = services.backtests
    backtest_router = agent.backtest_router
    backtest_coordinator = services.backtest_coordinator
    runs = agent.runs
    current_user = ctx.current_user
    require_session = ctx.require_session
    proposal_error = ctx.proposal_error
    state_root = settings.state_root
    _replay_provider = ctx.replay_provider

    def strategy_account(strategy: dict[str, object]):
        """The account a strategy row belongs to; the default account without wiring."""
        account_id = strategy.get("tradingAccountId")
        account = (services.accounts.get(str(account_id)) if account_id
                   else services.accounts.default)
        if account is None:
            # The strategy's account is disabled in this boot: reading its
            # monitor or orders through the default account would mix books.
            raise HTTPException(status_code=409, detail={"code": "account_not_assembled"})
        return account

    @application.get("/api/strategies")
    async def list_strategies(
        request: Request, limit: int = Query(default=20, ge=1, le=50),
    ) -> dict[str, object]:
        user_id = await current_user(request)
        return {
            "items": store.list_strategy_drafts(user_id=user_id, limit=limit),
            "simulationOnly": True,
            "liveTrading": "disabled",
        }

    @application.get("/api/strategies/signal")
    async def explain_strategy_signal(
        request: Request,
        strategyId: str | None = Query(default=None, max_length=100),
        symbol: str | None = Query(default=None, max_length=80),
        chartTimeframeSeconds: int | None = Query(default=None),
    ) -> dict[str, object]:
        """按当前数据（回放中按回放时钟）解读一张策略的信号：结论（开多/开空/平多/平空/观望）、
        每条条件的满足情况、参考入场价、策略止损/止盈，以及可画到图上的标注。只读：不创建
        候选、不授权、不下单。strategyId 省略时取该合约上监控中的策略（多张时返回候选）；
        symbol 省略时用策略自己的合约；chartTimeframeSeconds 为当前图表周期，跟随图表周期的
        策略（如“图表周期均线交叉”）需要它。未验证的草案也能解读，结果带 validated=false。"""
        user_id = await current_user(request)
        # A read: the app-level write gate never sees it, so the strategy
        # skill check stays here (the catalog attributes the route to
        # strategy-signals; availability follows strategy-management).
        require_skill(store, user_id, "strategy-management", human=human_request(request))
        if chartTimeframeSeconds is not None and chartTimeframeSeconds not in (60, 180, 300, 900, 3600, 86400):
            raise HTTPException(status_code=422, detail="chart_timeframe_invalid")
        try:
            return proposals.explain_strategy_signal(
                user_id=user_id, turn_id=f"signal:{uuid4()}",
                strategy_id=strategyId, symbol=symbol,
                chart_timeframe_seconds=chartTimeframeSeconds,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy_not_found") from error
        except ProposalConflict as error:
            raise proposal_error(error) from error

    @application.get("/api/strategies/funds-preview")
    async def strategy_funds_preview(
        request: Request,
        symbol: str = Query(max_length=80),
        equityPercent: float = Query(gt=0, le=100),
        maxLots: int | None = Query(default=None, ge=1),
        strategyId: str | None = Query(default=None, max_length=100),
    ) -> dict[str, object]:
        """按资金比例定手数 · 编辑器实时预览 (2026-10-09)：未保存的定义也能看预算——同一份
        live_budget 核心实盘发送前核对用的，只读，不建候选、不占额度。strategyId 是执行实例时
        按这个实例已占用的持仓/挂单和它绑定的账户算；是库策略（或不带）时按当前请求账户估
        一个全新实例。"""
        user_id = await current_user(request)
        require_skill(store, user_id, "strategy-management", human=human_request(request))
        try:
            if strategyId:
                strategy = store.get_strategy_draft(strategyId, user_id=user_id)
            else:
                strategy = {}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy_not_found") from error
        # 预览走与正式发送同一套账户归属解析（`ctx.account_context`），并区分库策略与执行
        # 实例 (2026-10-10 审查)：执行实例（definition 带 libraryStrategyId）绑定自身账户，
        # 按它的持仓/挂单占用算；库策略只是模板，可以在任何账户上线——按当前请求账户估
        # 一个全新实例，不扣这份记录（或别的实例）的占用，也不被它的 tradingAccountId
        # 覆盖请求头。绝不默认落到全局 owner 账户上。
        instance = bool((strategy.get("definition") or {}).get("libraryStrategyId"))
        symbol = symbol.strip()
        if instance and strategy.get("tradingAccountId"):
            context = await ctx.account_context(request, user_id, account_id=str(strategy.get("tradingAccountId")))
        else:
            # 外盘三期：外盘合约按美元子账户估（与上线同一入口 account_for_symbol），图表的主连代码换成主力月份。
            context = await ctx.account_for_symbol(request, user_id, symbol)
            from ..strategy_workbench import concrete_symbol
            symbol = await concrete_symbol(context.account.provider, symbol)
        account = context.account
        from ..strategy_funds import live_budget
        try:
            budget = live_budget(
                store, account.provider, user_id=user_id,
                strategy_id=str(strategyId or "") if instance else "", symbol=symbol,
                account=None, market=None,
                fee_settings=getattr(account.simulation_orders, "fee_settings", None),
                gateway=account.simulation_orders,
                equity_percent=equityPercent, max_lots=maxLots)
        except (KeyError, RuntimeError, ValueError) as error:
            raise HTTPException(status_code=409, detail={"code": "funds_preview_unavailable",
                                                         "message": str(error)}) from error
        budget["availableLots"] = int(budget.pop("lots") or 0)
        return {"funds": budget, "simulationOnly": True, "liveTrading": "disabled",
                "accountId": account.account_id}

    from ..strategy_workbench import register_workbench
    register_workbench(application, current_user, state_root, adapter, store, broker,
                       services.accounts.default.provider, proposals, ctx=ctx)

    @application.get("/api/strategies/{strategy_id}/status")
    async def get_strategy_status(request: Request, strategy_id: str) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            return store.get_strategy_draft(strategy_id, user_id=user_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy_not_found") from error

    def current_trading_day(account) -> str | None:
        """The account's own trading day (the monitor's clock for "今日"); None when unreadable."""
        try:
            day = str(account.provider.account_summary("strategy-monitor-read").get("tradingDay") or "")
            datetime.strptime(day, "%Y-%m-%d")
        except Exception:
            return None
        return day

    def strategy_monitor_payload(strategy_id: str, user_id: str) -> dict[str, object]:
        strategy = store.get_strategy_draft(strategy_id, user_id=user_id)
        account = strategy_account(strategy)
        attempts = store.list_strategy_execution_attempts(strategy_id=strategy_id, user_id=user_id, limit=10)
        for attempt in attempts:
            readbacks = []
            for identifier in (attempt.get("clientOrderId") or "").split(","):
                if identifier and account.simulation_orders is not None:
                    try:
                        readbacks.append(account.simulation_orders.get(
                            identifier, user_id=user_id)["displayStatus"])
                    except KeyError:
                        pass
            if readbacks:
                attempt["submissionDetail"] = attempt.get("detail")
                attempt["detail"] = "最新委托回读：" + " / ".join(readbacks)
        monitor_state = store.strategy_monitor_state(strategy_id=strategy_id, user_id=user_id)
        # 今日 (2026-09-25 fix): the state keeps the last trading day the monitor ran on. Its day
        # P&L, loss streak and open count only describe *today* when that day is the account's
        # current trading day; otherwise (a new session before the first pass, a paused instance)
        # they are an earlier day's figures and are not reported as today's.
        trading_day = current_trading_day(account)
        fresh = monitor_state is not None and trading_day is not None and monitor_state["tradingDate"] == trading_day
        if monitor_state is not None and not fresh:
            monitor_state = {**monitor_state, "lastTodayPnl": None, "consecutiveLosses": 0}
        monitor = account.strategy_monitor
        # 策略卡 (2026-10-08): the instance's own position, today's signals, and whether it goes
        # against the product's 今日判断 (偏多 / 偏空 only; 震荡 / 不明 / none say nothing).
        position: dict[str, Any] = (monitor.held_position(strategy) if monitor is not None
                                    else {"direction": None, "volume": 0})
        signals: dict[str, Any] = (store.strategy_signals_today(strategy_id=strategy_id, user_id=user_id,
                                                                trading_date=trading_day)
                                   if trading_day is not None else {"count": None, "lastOpen": None})
        direction: str | None = position["direction"] or signals["lastOpen"]
        desk = services.research_desk
        clash = None
        if direction and desk is not None and strategy["status"] in {"monitoring", "paused"}:
            from ..research_desk import stance_clash
            view = desk.view_at(str(strategy["symbol"]), datetime.now(UTC))
            clash = stance_clash((view or {}).get("stance"), direction, holding=bool(position["direction"]))
        # 按资金比例定手数 / 回撤停机 (2026-10-09): the same shared cores the monitor and the
        # send-time recheck judge with — the payload only reads, it never reserves anything.
        # 读数块与内置工具的 status 共用 monitor 的同一入口 (2026-10-10 #692)。
        funds: dict[str, Any] | None = None
        drawdown: dict[str, Any] | None = None
        if monitor is not None:
            funds, drawdown = monitor.funds_and_drawdown(strategy)
        return {
            "strategyId": strategy_id,
            "position": {"direction": position["direction"], "volume": position["volume"]},
            "signalsToday": signals["count"],
            "researchClash": clash,
            "status": strategy["status"],
            # 执行方式: what the instance is set to, and what it does now (auto runs as confirm
            # while 设置 → 交易 → AI 自主执行 is off).
            "signalAction": strategy["signalAction"],
            "effectiveSignalAction": (monitor.signal_mode(strategy) if monitor is not None
                                      else strategy["signalAction"]),
            "signalSessionId": strategy["signalSessionId"],
            "monitorState": monitor_state,
            "tradingDay": trading_day,
            "today": ({"tradingDate": trading_day, "pnl": monitor_state["lastTodayPnl"],
                       "consecutiveLosses": monitor_state["consecutiveLosses"]}
                      if fresh and monitor_state is not None else None),
            # Opens counted toward maxOrdersPerDay: the same count the monitor checks the cap
            # with, on the current trading day (null before the day's first monitor pass).
            # An unconfirmed strategy card is not an open; a confirmed, dispatched one is.
            "opensToday": store.count_strategy_attempts_today(
                strategy_id=strategy_id, user_id=user_id, trading_date=trading_day,
            ) if fresh and trading_day is not None else None,
            "recentAttempts": attempts,
            "decision": (monitor.latest_results.get(strategy_id) if monitor is not None else None),
            "funds": funds,
            "drawdown": drawdown,
            "simulationOnly": True,
            "liveTrading": "disabled",
        }

    @application.get("/api/strategies/{strategy_id}/monitor")
    async def get_strategy_monitor(
        request: Request, strategy_id: str,
    ) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            return strategy_monitor_payload(strategy_id, user_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy_not_found") from error

    @application.post("/api/strategies/{strategy_id}/monitor/start")
    async def start_strategy_monitor(
        request: Request, strategy_id: str, body: StrategyMonitorStartRequest | None = None,
    ) -> dict[str, object]:
        """启动模拟监控，或在监控中修改执行方式：signalAction alert 只提醒 / confirm 出卡确认 / auto
        自动下单（需要 设置 → 交易 → AI 自主执行 开着，否则 409 ai_autonomy_disabled），sessionId 是
        提醒与确认卡所在的对话（必须是本人的会话）。省略的字段保持实例原值。"""
        user_id = await current_user(request)
        body = body or StrategyMonitorStartRequest()
        if _replay_provider is not None and (_replay_provider.stopped or _replay_provider.snapshot.get("status") == "finished"):
            raise HTTPException(409, "回放已结束，请新建回放后再启动策略")
        try:
            current = store.get_strategy_draft(strategy_id, user_id=user_id)
            # 启动策略监控是 AI 自主下单的入口：只属于策略所属账户的执行负责人。
            require_execution_owner(
                await ctx.account_context(request, user_id, account_id=current.get("tradingAccountId")))
            monitor = strategy_account(current).strategy_monitor
            if monitor is None or not monitor.enabled:
                raise HTTPException(status_code=503, detail={"code": "strategy_monitor_unavailable"})
            if body.signalAction == "auto" and not monitor.autonomy.autonomy_enabled(user_id):
                raise HTTPException(status_code=409, detail={"code": "ai_autonomy_disabled"})
            if body.sessionId is not None:
                require_session(body.sessionId, user_id)
            # 启动 / 显式重置链路与内置工具共用同一入口 (2026-10-10 #692)：回撤重置的前提核对、
            # 排期核对、状态迁移、执行方式更新与“全部成功后才删停机状态”都收敛在 monitor 里，
            # 两个入口不再各写一套迁移。
            monitor.start_monitoring(
                current, signal_action=body.signalAction, session_id=body.sessionId,
                reset_drawdown=bool(body.resetDrawdown))
            return strategy_monitor_payload(strategy_id, user_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy_not_found") from error
        except (ProposalConflict, RuntimeError, ValueError) as error:
            raise proposal_error(error) from error

    @application.post("/api/strategies/{strategy_id}/monitor/stop")
    async def stop_strategy_monitor(
        request: Request, strategy_id: str, body: StrategyMonitorStopRequest | None = None,
    ) -> dict[str, object]:
        """停止一个运行中或已暂停的实例：回到 ready，不再判断信号；执行记录保留。默认已有持仓不会平掉；
        body.closePositions=true（停止并平仓）时，停止后若它还有持仓，在它的对话里出平仓卡（平今 / 平昨分开）
        等你确认才平——从不直接下单，自动下单的实例也一样。要再运行，重新上线即可。"""
        user_id = await current_user(request)
        body = body or StrategyMonitorStopRequest()
        try:
            if body.sessionId is not None:
                require_session(body.sessionId, user_id)
            current = store.get_strategy_draft(strategy_id, user_id=user_id)
            require_account_write(
                await ctx.account_context(request, user_id, account_id=current.get("tradingAccountId")))
            if current["status"] in {"monitoring", "paused"}:
                proposals.transition_strategy_status(
                    strategy_id=strategy_id, user_id=user_id, target="ready"
                )
            elif current["status"] != "ready":
                raise ProposalConflict("strategy_transition_invalid")
            monitor = strategy_account(current).strategy_monitor
            if monitor is not None:
                monitor.latest_results.pop(strategy_id, None)
                monitor.forget_offer(strategy_id, user_id)
            if body.closePositions and monitor is not None:
                closing = await monitor.propose_close_after_stop(
                    store.get_strategy_draft(strategy_id, user_id=user_id), session_id=body.sessionId)
                return {**strategy_monitor_payload(strategy_id, user_id), "positionsKept": not closing["cards"],
                        "closeCards": [{"proposalId": card["proposalId"], "symbol": card.get("symbol"),
                                        "side": card.get("side"), "volume": card.get("volume")} for card in closing["cards"]],
                        "closeSessionId": closing.get("sessionId"),
                        "notice": f"已停止：不再判断信号。{closing['detail']}"}
            return {**strategy_monitor_payload(strategy_id, user_id), "positionsKept": True,
                    "notice": "已停止：不再判断信号，执行记录保留；已有持仓不会平掉，需要平仓请在持仓里处理。"}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy_not_found") from error
        except (ProposalConflict, RuntimeError, ValueError) as error:
            raise proposal_error(error) from error

    @application.post("/api/strategies/{strategy_id}/monitor/pause")
    async def pause_strategy_monitor(
        request: Request, strategy_id: str,
    ) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            current = store.get_strategy_draft(strategy_id, user_id=user_id)
            require_account_write(
                await ctx.account_context(request, user_id, account_id=current.get("tradingAccountId")))
            if current["status"] != "paused":
                proposals.transition_strategy_status(
                    strategy_id=strategy_id, user_id=user_id, target="paused"
                )
            return strategy_monitor_payload(strategy_id, user_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="strategy_not_found") from error
        except (ProposalConflict, RuntimeError, ValueError) as error:
            raise proposal_error(error) from error

    @application.post("/api/backtests", status_code=status.HTTP_202_ACCEPTED)
    async def start_backtest(request: Request, body: BacktestRequest) -> dict[str, str]:
        user_id = await current_user(request)
        require_session(body.sessionId, user_id)
        application.state.app_capabilities.bind_session(body.sessionId, user_id, request.headers)
        try:
            run = runs.enqueue(session_id=body.sessionId, user_id=user_id, instruction=body.instruction)
        except ValueError as error:
            # #515 F-d: the session is being cleaned up under the request.
            if str(error) == "session_releasing":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="session_releasing") from error
            raise
        return {"status": "accepted", "runId": str(run["runId"])}

    @application.post("/api/backtests/reproducible", status_code=status.HTTP_201_CREATED)
    async def run_reproducible_backtest(
        request: Request, body: ReproducibleBacktestRequest,
    ) -> dict[str, object]:
        user_id = await current_user(request)
        require_session(body.sessionId, user_id)
        try:
            service = (
                backtest_router.for_session(body.sessionId, user_id=user_id)
                if backtest_router is not None else backtests
            )
            return await asyncio.to_thread(
                service.run,
                session_id=body.sessionId,
                user_id=user_id,
                strategy_path=body.strategyPath,
                data_path=body.dataPath,
                baseline_threshold=body.baselineThreshold,
                candidate_threshold=body.candidateThreshold,
                request_id=body.requestId,
            )
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.post("/api/backtests/reproducible/{request_id}/cancel")
    async def cancel_reproducible_backtest(
        request: Request, request_id: str,
    ) -> dict[str, object]:
        user_id = await current_user(request)
        cancelled = backtest_coordinator.cancel(request_id, user_id=user_id)
        if not cancelled:
            raise HTTPException(status_code=404, detail="backtest_request_not_found")
        return {"requestId": request_id, "status": "cancelled"}

    @application.post("/api/backtests/tqsdk", status_code=status.HTTP_201_CREATED)
    async def run_tqsdk_backtest(
        request: Request, body: TqSdkBacktestRequest,
    ) -> dict[str, object]:
        user_id = await current_user(request)
        require_session(body.sessionId, user_id)
        context = await ctx.account_context(request, user_id, session_id=body.sessionId)
        runner = getattr(context.account.provider, "historical_backtest", None)
        if not callable(runner):
            raise HTTPException(status_code=503, detail="tqsdk_backtest_unavailable")
        request_id = body.requestId or str(uuid4())
        try:
            comparison = await asyncio.to_thread(
                backtests.run_tqsdk,
                session_id=body.sessionId,
                user_id=user_id,
                symbol=body.symbol,
                start_date=body.startDate,
                end_date=body.endDate,
                duration_seconds=body.durationSeconds,
                baseline_threshold=body.baselineThreshold,
                candidate_threshold=body.candidateThreshold,
                initial_balance=body.initialBalance,
                runner=runner,
                request_id=request_id,
            )
            turn_id = str(uuid4())
            await broker.publish(
                body.sessionId,
                kind="message",
                name="message.completed",
                payload={
                    "turnId": turn_id,
                    "role": "assistant",
                    "text": "官方 TqSdk 历史回测已完成；结果只用于研究，不会部署或下单。",
                    "agentId": "native",
                },
            )
            payload = {**comparison, "turnId": turn_id, "taskId": request_id}
            await broker.publish(
                body.sessionId,
                kind="chart",
                name="card.backtest",
                payload=payload,
            )
            return payload
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.get("/api/backtests/{backtest_id}")
    async def get_backtest(request: Request, backtest_id: str) -> dict[str, object]:
        user_id = await current_user(request)
        try:
            record = store.get_backtest_run(backtest_id, user_id=user_id)
            return {
                key: value for key, value in record.items()
                if key not in {"artifactPath", "userId"}
            }
        except KeyError as error:
            raise HTTPException(status_code=404, detail="backtest_not_found") from error

