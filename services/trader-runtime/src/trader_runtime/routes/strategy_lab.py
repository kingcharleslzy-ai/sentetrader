"""策略实验室 (2026-09-30): compare up to 12 strategy variants on one contract.

The comparison runs in the user's browser (Pyodide, the same engine sources this server ships);
these routes hand it the inputs, take its results back (re-ranked here), provide the cloud
fallback and save the chosen variant after the server's own authoritative backtest. The lab rows
are per user — another user's lab is 没有这次比较, never a hint that it exists.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from ..browser_backtests import browser_backtests_enabled
from ..store import utc_now
from ..strategy_backtest import BacktestDataError, history_error_message
from ..strategy_lab import (
    COMPARE_DAYS,
    PYODIDE,
    LabError,
    build_inputs,
    build_variants,
    compare_line,
    compare_variants,
    complete_results,
    engine_bundle,
    lab_summary,
    run_lab_on_server,
)
from ..strategy_lab_engine import rank
from ..strategy_requests import (
    BacktestOptions,
    CompareRequest,
    InstrumentScope,
    LabChooseRequest,
    LabCreateRequest,
    LabResultsRequest,
    LabRunServerRequest,
    LabVariant,
    TemplateInstantiateRequest,
)
from ..strategy_rules import StrategyPlan, StrategyRuleError
from ..strategy_templates import templates
from ..strategy_workbench import concrete_symbol
from ..trading_costs import FeeSettings, backtest_fee_schedule
from ..wiring.context import RuntimeContext

log = logging.getLogger("uvicorn.error.strategy-lab")

# The cloud fallback's tasks by lab id, held so a running lab is never garbage-collected mid-run.
# The entry is a pending claim until its task exists: taking the slot before the route's first
# await makes a double start (two tabs racing) impossible; a “running” row without a live entry
# here is a restart leftover and may rerun instead of 409ing forever.
_RUNNING: dict[str, asyncio.Future[None] | asyncio.Task[None]] = {}


def _forget(lab_id: str, task: asyncio.Task[None]) -> None:
    """A finished cloud run leaves the registry (#530-C: it only grew) — only when the entry is
    still this run, so an old run's late callback never drops a newer one; its exception is read so
    a failed run is not reported as never retrieved."""
    if not task.cancelled():
        task.exception()
    if _RUNNING.get(lab_id) is task:
        del _RUNNING[lab_id]


def cloud_running(lab_id: str) -> bool:
    live = _RUNNING.get(lab_id)
    return live is not None and not live.done()


def settled(lab: dict) -> dict:
    """A “running” row this process holds no task for was cut off (a restart or a release mid-run):
    it reads as an error the page can rerun from, not a run that never ends."""
    if lab.get("status") != "running" or cloud_running(lab["id"]):
        return lab
    return {**lab, "status": "error", "ranking": {**(lab.get("ranking") or {}), "error": "这次比较被中断了，请重新比一比"}}


def register(application: FastAPI, ctx: RuntimeContext) -> None:
    store = ctx.services.store
    current_user = ctx.current_user
    fees = FeeSettings(ctx.settings.state_root)

    def lab_or_404(user: str, lab_id: str) -> dict:
        try:
            return store.get_lab(lab_id, user_id=user)
        except KeyError:
            raise HTTPException(404, "没有这次比较") from None

    def fee_schedule_for(provider, user: str, symbol: str) -> dict:
        """The backtest fee rule (run_rule_backtest): the user's own rates, 未自定义时换成
        交易所标准（openctp 汇总）。Blocking — call it off the event loop."""
        return backtest_fee_schedule(provider, getattr(provider, "fee_schedule", None) or fees.get(user), symbol,
                                     root=ctx.settings.state_root)

    @application.get("/api/strategy-engine/bundle")
    async def engine_bundle_route(request: Request) -> JSONResponse:
        """浏览器实验室跑的引擎源码（与服务端同一份文件，按 digest 缓存；随版本变化）。"""
        await current_user(request)
        bundle = engine_bundle()
        return JSONResponse({**bundle, "entry": "trader_runtime.strategy_lab_engine", "pyodide": PYODIDE},
                            headers={"Cache-Control": "no-cache"})

    async def lab_symbol(request: Request, user: str, symbol: str) -> str:
        """外盘三期：比较存的是具体合约——外盘图表的快期外盘主连换成美元子账户此刻的主力月份，
        与普通回测同一入口（``concrete_symbol``），历史、手续费和之后存为策略都用这一个月份。"""
        account = await ctx.account_for_symbol(request, user, symbol)
        return await concrete_symbol(account.account.provider, symbol)

    @application.post("/api/strategy-labs", status_code=201)
    async def create_lab(request: Request, body: LabCreateRequest) -> dict:
        user = await current_user(request)
        try:
            variants, skipped = build_variants(body.variants, skip_invalid=body.skipInvalid)
        except LabError as error:
            raise HTTPException(422, str(error)) from error
        symbol = await lab_symbol(request, user, body.symbol)
        return store.save_lab(lab_id=uuid4().hex, user_id=user, symbol=symbol,
                              request={"symbol": symbol, "tradingDays": body.tradingDays,
                                       "slippageTicks": body.slippageTicks},
                              variants=variants, skipped=skipped)

    @application.get("/api/strategy-labs")
    async def list_labs(request: Request, limit: int = Query(default=10, ge=1, le=50)) -> dict:
        user = await current_user(request)
        return {"items": [lab_summary(settled(lab)) for lab in store.list_labs(user_id=user, limit=limit)]}

    @application.get("/api/strategy-labs/{lab_id}")
    async def get_lab(lab_id: str, request: Request) -> dict:
        user = await current_user(request)
        return settled(lab_or_404(user, lab_id))

    @application.get("/api/strategy-labs/{lab_id}/inputs")
    async def lab_inputs(lab_id: str, request: Request) -> Response:
        """浏览器跑对比需要的全部输入：合约参数、手续费、初始资金、滑点与各周期的历史K线。
        几 MB 的历史按需 gzip（不走全局中间件）。"""
        user = await current_user(request)
        lab = lab_or_404(user, lab_id)
        provider = (await ctx.account_for_symbol(request, user, lab["symbol"])).account.provider
        schedule = await asyncio.to_thread(fee_schedule_for, provider, user, lab["symbol"])
        try:
            inputs = await asyncio.to_thread(build_inputs, provider, lab, schedule)
        except (BacktestDataError, StrategyRuleError) as error:
            raise HTTPException(422, str(error)) from error
        except KeyError:
            raise HTTPException(404, "symbol_not_found") from None
        except RuntimeError as error:
            raise HTTPException(503, history_error_message(str(error))) from error
        payload = {**inputs, "variants": lab["variants"], "engineDigest": engine_bundle()["digest"]}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                          default=str).encode()
        headers = {"Cache-Control": "no-cache"}
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(gzip.compress(body), media_type="application/json",
                            headers={**headers, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(body, media_type="application/json", headers=headers)

    @application.post("/api/strategy-labs/{lab_id}/results")
    async def post_results(lab_id: str, request: Request, body: LabResultsRequest) -> dict:
        """收浏览器（Pyodide）算完的各组结果。排名由服务端用同一引擎重算——客户端传来的
        flags / 排名一概不信任；引擎版本变了让浏览器带上新源码重跑。``tried`` 是这次比较
        的组数，不是送来的结果数：lab 里有而结果里没有的变体补一条“没有算完”。"""
        user = await current_user(request)
        lab = lab_or_404(user, lab_id)
        # #530-C: the cloud's own run is authoritative — a browser upload arriving while it runs or
        # after it finished never replaces it (a late upload used to overwrite a finished server run).
        if cloud_running(lab_id):
            raise HTTPException(409, "这次比较正在云端运行，结果以云端为准")
        if lab["source"] == "server" and lab["status"] == "done":
            raise HTTPException(409, "云端已经算完这次比较，不再接收浏览器的结果")
        if body.engineDigest != engine_bundle()["digest"]:
            raise HTTPException(409, "回测引擎已更新，请刷新页面后重跑")
        known = {variant["id"] for variant in lab["variants"]}
        for item in body.results:
            if item.variantId not in known:
                raise HTTPException(422, f"结果里有不属于这次比较的变体：{item.variantId}")
        results = complete_results(lab, [item.model_dump() for item in body.results])
        return store.update_lab(lab_id, user_id=user, status="done", source="browser",
                                results=results, ranking=rank(results, tried=len(lab["variants"])))

    @application.post("/api/strategy-labs/{lab_id}/run-server")
    async def run_server(lab_id: str, request: Request, body: LabRunServerRequest | None = None) -> dict:
        """云端兜底：浏览器跑不了（手机、加载失败）时在服务端排队跑同一份输入。每组完成
        都写库，GET 这次比较能看到进度；本进程已在跑（或正要起）这次比较时再调返回 409。
        行状态“运行中”但本进程没有它的任务（部署重启的残留）时允许重跑。fallbackReason（界面本机
        算不了的原因，排障用）记在这次比较上。"""
        user = await current_user(request)
        lab = lab_or_404(user, lab_id)
        return await start_cloud_run(request, user, lab, fallback=body.fallbackReason if body else None)

    async def start_cloud_run(request: Request, user: str, lab: dict, *, fallback: str | None = None) -> dict:
        lab_id = lab["id"]
        if cloud_running(lab_id):
            raise HTTPException(409, "这次比较正在云端运行，稍后再看结果")
        claim: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        _RUNNING[lab_id] = claim                   # the slot is taken before the first await
        try:
            provider = (await ctx.account_for_symbol(request, user, lab["symbol"])).account.provider
            schedule = await asyncio.to_thread(fee_schedule_for, provider, user, lab["symbol"])
            started = store.update_lab(lab_id, user_id=user, status="running", results=[], ranking={},
                                       reset_source=True,
                                       request={**(lab.get("request") or {}), "fallbackReason": fallback})
            task = asyncio.create_task(run_lab_on_server(application, store, provider, schedule, user, lab_id))
        except BaseException:
            if _RUNNING.get(lab_id) is claim:      # the run never started: free the slot
                del _RUNNING[lab_id]
            raise
        _RUNNING[lab_id] = task
        task.add_done_callback(lambda done: _forget(lab_id, done))
        return started

    def compare_view(lab: dict) -> dict:
        request = lab.get("request") or {}
        return {"labId": lab["id"], "strategyId": request.get("strategyId"), "symbol": lab["symbol"],
                "status": lab["status"], "source": lab.get("source"), "fallbackReason": request.get("fallbackReason"),
                "paramLabel": request.get("paramLabel"), "paramHelp": request.get("paramHelp"),
                "currentVariant": request.get("currentVariant"), "createdAt": lab["createdAt"],
                "rows": [{"variantId": variant["id"], "label": variant["label"],
                          "current": variant["id"] == request.get("currentVariant"),
                          **({"summary": result.get("summary"), "inSample": result.get("inSample"),
                              "outSample": result.get("outSample"), "error": result.get("error")}
                             if (result := next((item for item in lab.get("results") or []
                                                 if item.get("variantId") == variant["id"]), None)) else {})}
                         for variant in lab["variants"]],
                "best": (lab.get("ranking") or {}).get("best"),
                "line": compare_line(lab), "error": (lab.get("ranking") or {}).get("error")}

    @application.post("/api/strategy-plans/{strategy_id}/compare", status_code=201)
    async def compare_strategy(strategy_id: str, request: Request, body: CompareRequest):
        """试几组参数比一比（策略结果页，2026-10-08）：对这条策略最关键的一个参数（模板策略取模板的关键
        参数，其余取止损距离）跑当前值和上下各一组，共 3 组，最近 6 个月；前 70% 挑、后 30% 单独
        检验，和实验室同一套。只读回测，不改策略、不下单。GET 同一路径读这条策略最近一次比较。
        runOn 默认 server（立即在服务端排队算，AI 与 CLI 用）；界面传 browser 在本机算（2026-10-12），
        本机算不了再 POST /api/strategy-labs/{labId}/run-server。"""
        user = await current_user(request)
        try:
            item = store.get_strategy_draft(strategy_id, user_id=user)
        except KeyError:
            raise HTTPException(404, "策略不存在") from None
        timeframe = body.timeframe or item["definition"].get("chartTimeframeSeconds")
        try:
            raw, described = compare_variants(item, timeframe=timeframe)
            variants, _ = build_variants([LabVariant.model_validate({key: value for key, value in variant.items()
                                                                     if key != "value"}) for variant in raw])
        except LabError as error:
            raise HTTPException(422, str(error)) from error
        except (StrategyRuleError, ValueError) as error:
            raise HTTPException(422, f"比不了：{error}") from error
        current = next((variant["id"] for variant, source in zip(variants, raw, strict=True)
                        if source["label"].endswith("（当前）")), None)
        symbol = await lab_symbol(request, user, body.symbol)
        lab = store.save_lab(lab_id=uuid4().hex, user_id=user, symbol=symbol,
                             request={"symbol": symbol, "tradingDays": COMPARE_DAYS, "slippageTicks": 0,
                                      "strategyId": strategy_id, "currentVariant": current, **described},
                             variants=variants)
        if body.runOn == "server" or not browser_backtests_enabled():
            await start_cloud_run(request, user, lab)
        return compare_view(lab_or_404(user, lab["id"]))

    @application.get("/api/strategy-plans/{strategy_id}/compare")
    async def latest_compare(strategy_id: str, request: Request) -> dict:
        """这条策略最近一次“试几组参数比一比”：每组的赚、最多亏、胜率与前后两段，当前那组标 current，
        line 是“后 X 个月单独检验：…”；还没比过时 status 为 none、labId 为 null。"""
        user = await current_user(request)
        lab = store.latest_lab_for_strategy(user_id=user, strategy_id=strategy_id)
        if lab is None:
            return {"labId": None, "strategyId": strategy_id, "status": "none", "rows": [], "line": None,
                    "paramLabel": None, "paramHelp": None, "currentVariant": None, "best": None}
        return compare_view(settled(lab))

    @application.post("/api/strategy-labs/{lab_id}/choose")
    async def choose_variant(lab_id: str, request: Request, body: LabChooseRequest) -> dict:
        """把这组变体存为我的策略：服务端先用与规则回测完全相同的队列与口径重跑一遍权威
        回测（区间、滑点与这次比较相同）再保存。保存的是比较时已复核的那份方案（变体里的
        plan），不按当前模板目录重新生成——比较之后模板变了也不受影响。浏览器结果与服务端
        不一致不报错，返回里带 serverSummary 供界面显示。上线运行仍走原有流程。"""
        user = await current_user(request)
        lab = lab_or_404(user, lab_id)
        variant = next((item for item in lab["variants"] if item["id"] == body.variantId), None)
        if variant is None:
            raise HTTPException(404, "这次比较里没有这组变体")
        template = None
        if variant.get("templateId"):
            template = templates().get(str(variant["templateId"]))
            if template is None:      # checked before the rerun: nothing is spent on a save that cannot happen
                raise HTTPException(409, "这组变体用的策略模板已下线，不能按模板保存；"
                                         "请在策略库用同样的规则新建策略")
        result = await application.state.run_rule_backtest(
            request, user, raw_plan=variant["plan"],
            options=BacktestOptions(tradingDays=lab["request"].get("tradingDays"),
                                    slippageTicks=lab["request"].get("slippageTicks", 1)),
            symbol=lab["symbol"], identity={"strategyId": None, "strategyName": variant["label"],
                                            "version": None})
        name = body.name or variant["label"]
        if template is not None:
            saved = await application.state.instantiate_strategy(
                request, user, template,
                TemplateInstantiateRequest(params=variant.get("params") or {},
                                           timeframe=variant.get("timeframe"),
                                           symbol=lab["symbol"], name=name),
                plan_override=StrategyPlan.model_validate(variant["plan"]))
        else:
            plan = StrategyPlan.model_validate(variant["plan"]).model_copy(update={"name": name})
            scope = InstrumentScope(mode="selected", symbols=[str(lab["symbol"])])
            saved = await application.state.save_library_plan(request, user, plan, scope.model_dump())
        store.update_lab(lab_id, user_id=user, chosen={
            "variantId": variant["id"], "strategyId": saved["strategyId"],
            "backtestSummary": result["summary"], "at": utc_now()})
        browser = next((item for item in (lab["results"] or [])
                        if item.get("variantId") == variant["id"]), None)
        payload: dict = {"strategyId": saved["strategyId"], "backtest": result["summary"]}
        if browser is not None and browser.get("summary") != result["summary"]:
            # 权威复核以服务端为准；与本机结果不同只记日志（区间末端新K线、引擎差异都会造成）。
            if lab.get("source") == "browser":
                log.warning("strategy lab %s variant %s: browser summary %s, server rerun %s",
                            lab_id, variant["id"], browser.get("summary"), result["summary"])
            payload["serverSummary"] = result["summary"]
        return payload
