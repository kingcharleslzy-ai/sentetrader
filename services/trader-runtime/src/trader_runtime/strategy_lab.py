"""策略实验室 (2026-09-30) 的服务层: variants, browser inputs, the engine bundle and the
cloud fallback.

一次在一个合约上对比最多 12 组策略变体（同一模板不同参数，或不同模板 / 不同 plan）。回测在
用户的浏览器里用 Pyodide 跑（``GET /api/strategy-labs/{id}/inputs`` 给它同一份历史与参数，
``GET /api/strategy-engine/bundle`` 给它与服务端逐字节相同的引擎源码），浏览器算不了的走
``run-server`` 云端兜底。浏览器结果只用于展示与对比：排名由服务端重算（strategy_lab_engine.
rank），用户“存为我的策略”时服务端再用与规则回测完全相同的队列与口径重跑一遍才保存。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .strategy_backtest import (
    BacktestDataError,
    BacktestQueueTimeout,
    account_equity,
    equity_sized,
    load_series,
    margin_per_lot_for,
    plan_for_backtest,
    run_callable_in_worker,
)
from .strategy_lab_engine import rank, run_variant
from .strategy_rules import DEFAULT_PERIODS, StrategyPlan, StrategyRuleError
from .strategy_templates import TemplateError, build_plan, templates

log = logging.getLogger("uvicorn.error.strategy-lab")

# The modules the browser bundle ships: the rule backtest engine's whole import closure plus the
# shared lab engine itself. strategy_lab_engine.py may only import these (plus the standard
# library and pydantic) — anything added here must keep that true.
BUNDLE_MODULES = (
    "strategy_backtest", "strategy_engine", "strategy_health", "strategy_rules",
    "strategy_alignment", "strategy_pullback", "strategy_spread", "trading_calendar",
    "trading_costs", "trading_clock", "risk", "strategy_lab_engine", "backtest_settings",
    "strategy_funds",       # imported lazily by the engine's 按资金比例 sizing
    "instruments",          # contract formats and the 外盘 product table (外盘三期)
)
BUNDLE_INIT = '"""Browser bundle of the AI Trader rule backtest engine."""\n'
PYODIDE = {"version": "314.0.7", "packages": ["pydantic", "tzdata"]}

_bundle: dict[str, Any] | None = None


def engine_bundle() -> dict[str, Any]:
    """{digest, modules}: the engine sources the browser runs, keyed by their in-package path.
    ``digest`` (first 16 hex of the chained sha256 over sorted name + source) changes with any
    engine edit — a stale browser bundle is refused by POST /results (409)."""
    global _bundle
    if _bundle is None:
        package = Path(__file__).resolve().parent
        modules = {"trader_runtime/__init__.py": BUNDLE_INIT}
        for name in BUNDLE_MODULES:
            modules[f"trader_runtime/{name}.py"] = (package / f"{name}.py").read_text(encoding="utf-8")
        digest = hashlib.sha256()
        for filename in sorted(modules):
            digest.update(filename.encode())
            digest.update(modules[filename].encode())
        _bundle = {"digest": digest.hexdigest()[:16], "modules": modules}
    return _bundle


class LabError(ValueError):
    """A lab request that cannot be built; the message is the Chinese reason (422)."""


def _value_text(value: float | str | None) -> str:
    if value is None:
        return "不设"
    if isinstance(value, str):
        return value
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def default_label(template, values: dict[str, float | str | None]) -> str:
    """「双均线（基准） · 短均线 10 / 长均线 60」: the template's name and up to three parameters
    that differ from its defaults (each named by its label before “（”); all defaults is 默认参数.
    A group's name has to stay short — the results table shows twelve of them."""
    defaults = template.defaults()
    changed = [param for param in template.params if values[param.key] != defaults[param.key]]
    if not changed:
        return f"{template.name} · 默认参数"
    return template.name + " · " + " / ".join(
        f"{param.label.split('（')[0]} {_value_text(values[param.key])}" for param in changed[:3])


def build_variants(items: list[Any], *, skip_invalid: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every variant as {id, label, plan} plus its template origin: a templateId variant through
    ``build_plan`` (params checked against its range and constraints), a plan variant through
    ``plan_for_backtest``. 价差 plans are refused — the lab compares single-contract strategies.
    ``skip_invalid`` (模板页的参数网格，一次点出来的组合可能互相矛盾) skips the groups that cannot
    be built, recording each as {index, reason}, and refuses the whole request when fewer than two
    survive — a one-group comparison cannot compare."""
    library = templates()
    variants: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        try:
            if item.templateId is not None:
                template = library.get(item.templateId)
                if template is None:
                    raise TemplateError("模板不存在")
                plan, values = build_plan(template, item.params, timeframe=item.timeframe)
                origin = {"templateId": template.id, "params": values, "timeframe": item.timeframe}
                label = item.label or default_label(template, values)
            else:
                plan = plan_for_backtest(item.plan.model_dump(), item.timeframe)
                origin, label = {}, item.label or plan.name
            if plan.spread is not None:
                raise ValueError("价差策略暂不支持实验室对比")
        except (TemplateError, StrategyRuleError, ValidationError, ValueError) as error:
            if not skip_invalid:
                raise LabError(f"第 {index} 组：{error}") from error
            skipped.append({"index": index, "reason": str(error)})
            continue
        variants.append({"id": f"v{index}", "label": label, "plan": plan.model_dump(), **origin})
    if skip_invalid and len(variants) < 2:
        raise LabError("能比的组合不到 2 组：" + (skipped[0]["reason"] if skipped else "至少要给 2 组能比较的策略"))
    return variants, skipped


def build_inputs(provider, lab: dict[str, Any], fee_schedule: dict[str, Any]) -> dict[str, Any]:
    """The lab's shared backtest inputs — what the browser receives and what run-server computes
    with, built the same way on both sides. Blocking: call it off the event loop. The series is
    the union of every variant's ``load_series`` result per timeframe (the longer one wins);
    priceTick / volumeMultiple / schedule come from the execution period's read."""
    symbol = str(lab["symbol"])
    trading_days = lab["request"].get("tradingDays")
    series: dict[int, list[dict[str, Any]]] = {}
    first: dict[str, Any] | None = None
    equity_sizing = False
    for variant in lab["variants"]:
        plan = StrategyPlan.model_validate(variant["plan"])
        equity_sizing = equity_sizing or equity_sized(plan)
        variant_series, variant_first = load_series(provider, plan, symbol, trading_days)
        if first is None:
            first = variant_first
        for timeframe, bars in variant_series.items():
            if len(bars) > len(series.get(timeframe) or []):
                series[timeframe] = bars
    assert first is not None
    tick, multiple = first.get("priceTick"), first.get("volumeMultiple")
    if not tick or not multiple:
        snapshot = provider.market_snapshot("backtest", symbol)
        tick, multiple = snapshot.get("priceTick"), snapshot.get("volumeMultiple")
    return {"symbol": symbol, "priceTick": tick, "volumeMultiple": multiple,
            "schedule": first.get("tradingSchedule"), "feeSchedule": fee_schedule,
            "initialBalance": account_equity(provider),
            # 按资金比例的组要每手保证金，取值和拿不到时的报错与普通回测同一处。
            "marginPerLot": margin_per_lot_for(provider, symbol, first) if equity_sizing else None,
            "slippageTicks": lab["request"].get("slippageTicks", 1),
            "tradingDays": trading_days,
            "series": {str(timeframe): bars for timeframe, bars in series.items()}}


def complete_results(lab: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The results plus one ``没有算完`` entry for every variant of the lab that arrived without a
    result (a worker the browser lost mid-run): ``tried`` stays the comparison's size and the row
    shows up in the table instead of silently vanishing."""
    seen = {item.get("variantId") for item in results}
    missing = [{"variantId": variant["id"], "label": variant["label"], "error": "没有算完"}
               for variant in lab["variants"] if variant["id"] not in seen]
    return results + missing


async def run_lab_on_server(application, store, provider, fee_schedule: dict[str, Any],
                            user: str, lab_id: str) -> None:
    """云端兜底: the same inputs the browser gets, computed in this process instead — one queue
    slot per variant, each variant in the shared backtest worker pool (spawned processes: the lab
    never holds the Runtime's GIL), results written as they finish (GET shows the progress), ranked
    and closed at the end. A failure reading the history fails the whole lab (status=error); a
    per-variant failure (queue timeout included) is that variant's error result."""
    lab = store.get_lab(lab_id, user_id=user)
    queue = application.state.strategy_backtest_queue
    try:
        inputs = await asyncio.to_thread(build_inputs, provider, lab, fee_schedule)
    except (BacktestDataError, StrategyRuleError, KeyError, ValueError, RuntimeError, OSError) as error:
        log.warning("strategy lab %s inputs failed: %s", lab_id, error)
        store.update_lab(lab_id, user_id=user, status="error", ranking={"error": str(error) or "read failed"})
        return
    results: list[dict[str, Any]] = []
    for variant in lab["variants"]:
        try:
            async with queue.slot(user, request_id=lab_id, label={
                    "labId": lab_id, "strategyName": variant["label"], "symbol": lab["symbol"]}) as ticket:
                # #530-C: the slot belongs to the worker process's job, not to this coroutine — like the
                # rule backtest (#337), a cancelled run (shutdown, a dropped task) releases the slot
                # only when the process has really finished, so no second job starts beside it.
                ticket.work = asyncio.ensure_future(run_callable_in_worker(run_variant, inputs, variant))
                result = await asyncio.shield(ticket.work)
        except BacktestQueueTimeout as error:
            result = {"variantId": variant["id"], "label": variant["label"], "error": str(error)}
        except Exception as error:  # noqa: BLE001 — one variant's crash never hides the others
            log.exception("strategy lab %s variant %s failed", lab_id, variant["id"])
            result = {"variantId": variant["id"], "label": variant["label"],
                      "error": str(error) or error.__class__.__name__}
        results.append(result)
        store.update_lab(lab_id, user_id=user, results=list(results))
    complete = complete_results(lab, results)
    store.update_lab(lab_id, user_id=user, status="done", source="server",
                     results=complete, ranking=rank(complete, tried=len(lab["variants"])))


def lab_summary(lab: dict[str, Any]) -> dict[str, Any]:
    """The list entry: enough to recognise the comparison — and, for 手机版 策略 → 最近的回测
    (2026-10-06), its name (the template's, else the first variant's label), the period when every
    variant shares one, when it last changed and the recommended variant's out-of-sample net."""
    ranking = lab["ranking"] or {}
    variants = lab["variants"] or []
    template = templates().get(str(variants[0].get("templateId"))) if variants and variants[0].get("templateId") else None
    same_template = template is not None and all(item.get("templateId") == template.id for item in variants)
    periods = {(item.get("plan") or {}).get("timeframeSeconds") for item in variants}
    best = next((item for item in (lab["results"] or []) if item.get("variantId") == ranking.get("best")), None)
    return {"id": lab["id"], "symbol": lab["symbol"], "status": lab["status"], "source": lab["source"],
            "createdAt": lab["createdAt"], "updatedAt": lab.get("updatedAt"),
            "tried": ranking.get("tried", len(variants)), "best": ranking.get("best"),
            "name": template.name if same_template and template is not None else (variants[0].get("label") if variants else None),
            "timeframeSeconds": periods.pop() if len(periods) == 1 else None,
            "bestOutNet": ((best or {}).get("outSample") or {}).get("net"),
            "noProfit": bool(ranking.get("noProfit"))}


# -- 试几组参数比一比 (策略页改版第八批, 2026-10-08) ---------------------------------------------------------
#
# The result page compares the strategy with two neighbours of its most important parameter —
# three groups, the same lab mechanism (前 70% 挑、后 30% 单独检验), always computed in the cloud. A
# template strategy varies its template's key parameter (``key_param``; a parameter with the same
# value and range, like Dual Thrust's K2 beside K1, moves with it); any other plan varies how far its
# stop sits. The lab request carries strategyId / paramLabel / values / current so the page finds the
# strategy's latest comparison and says which row is the strategy as it stands.

COMPARE_DAYS = 120           # the same 最近 6 个月 the result page's backtest reads
LOW, HIGH = 0.75, 1.5         # the neighbours: about a quarter less and half again as much


def _snap(param, value: float) -> float:
    assert param.min is not None and param.max is not None and param.step is not None
    steps = round((value - param.min) / param.step)
    snapped = min(param.max, max(param.min, param.min + steps * param.step))
    rounded = round(snapped, param.decimals)
    return int(rounded) if param.integer else rounded


def _neighbours(current: float, snap) -> list[float]:
    """[lower, current, higher] — distinct after snapping to the parameter's grid, or fewer."""
    values = sorted({snap(current * LOW), current, snap(current * HIGH)})
    if len(values) < 3:
        values = sorted({*values, snap(current * 0.5), snap(current * 2)})
    lower = [value for value in values if value < current][-1:]
    higher = [value for value in values if value > current][:1]
    return [*lower, current, *higher]


def value_words(param_key: str | None, value: float, unit: str = "") -> str:
    """A compared value as the page says it: Dual Thrust's K (a fraction of the range) in 成, others as is."""
    if param_key in {"k1", "k2"} and 0 < value < 1:
        return f"{_value_text(round(value * 10, 2))} 成"
    return f"{_value_text(value)}{(' ' + unit) if unit and not unit.startswith('%') else unit}"


def _entry_lookback(plan: Any) -> tuple[str, int, str, int] | None:
    """(rules group, condition index, side, period) of the first entry operand with a real lookback."""
    for group_name in ("entryLong", "entryShort"):
        group = getattr(plan.rules, group_name)
        for index, condition in enumerate(group.conditions if group is not None else []):
            for side in ("right", "left"):
                operand = getattr(condition, side, None)
                # Only indicators that read a lookback (DEFAULT_PERIODS); a plain price or constant has none.
                if operand is not None and operand.indicator in DEFAULT_PERIODS and operand.period >= 2:
                    return group_name, index, side, int(operand.period)
    return None


def compare_variants(item: dict[str, Any], *, timeframe: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(variants for ``build_variants``, the comparison's description) for one library strategy."""
    definition = item.get("definition") or {}
    raw = definition.get("plan")
    if not raw:
        raise LabError("这条策略没有规则定义，比不了")
    plan = plan_for_backtest(raw, timeframe)
    if plan.spread is not None:
        raise LabError("价差策略暂不支持试几组参数比一比")
    source = definition.get("templateSource") or {}
    template = templates().get(str(source.get("templateId"))) if source.get("templateId") and not source.get("modified") else None
    from .strategy_templates import key_param
    key = key_param(template) if template is not None else None
    if template is not None and key is not None:
        values = {**template.defaults(), **(source.get("params") or {})}
        current = float(values[key.key])
        linked = [param.key for param in template.params if param.key != key.key and param.type == "number"
                  and values.get(param.key) is not None and float(values[param.key]) == current and (param.min, param.max, param.step) == (key.min, key.max, key.step)]
        label = key.label.split("（")[0]
        variants: list[dict[str, Any]] = []
        for value in _neighbours(current, lambda v: _snap(key, v)):
            params = {**values, key.key: value, **{other: value for other in linked}}
            variants.append({"templateId": template.id, "params": params,
                             "timeframe": plan.timeframeSeconds if plan.timeframeSeconds != template.recommendedTimeframe else None,
                             "label": value_words(key.key, value, key.unit) + ("（当前）" if value == current else ""),
                             "value": value})
        return variants, {"paramKey": key.key, "paramLabel": label, "paramHelp": key.help.model_dump(),
                          "linked": linked}
    # A plan without a fixed stop (5000 跳 is the schema ceiling the designer uses for "不设止损"; 10-08 线上
    # 比一比 then asked for 6250 跳 and failed): compare its first entry lookback instead (前 N 根).
    if plan.atrStop is None and plan.stopLossTicks >= 5000:
        found = _entry_lookback(plan)
        if found is not None:
            group_name, index, side, period = found
            variants = []
            for value in _neighbours(float(period), lambda v: min(200, max(2, round(v)))):
                raw_plan = plan.model_dump()
                group = raw_plan["rules"][group_name]
                group["conditions"][index][side]["period"] = int(value)
                changed = type(plan).model_validate(raw_plan)
                variants.append({"plan": changed, "label": f"前 {int(value)} 根" + ("（当前）" if value == period else ""),
                                 "value": value})
            return variants, {"paramKey": "entryLookback", "paramLabel": "开仓回看根数", "paramHelp": None, "linked": []}
    # Any other plan: how far the stop sits (its ATR multiple, else its ticks).
    if plan.atrStop is not None:
        current, label, field = float(plan.atrStop.multiple), "止损距离（平均波动的倍数）", "atrStop"
        snap = lambda v: max(0.5, round(v * 2) / 2)  # noqa: E731
    else:
        current, label, field = float(plan.stopLossTicks), "止损距离（跳）", "stopLossTicks"
        snap = lambda v: min(5000, max(1, round(v)))  # noqa: E731 — inside the schema's 1–5000
    variants = []
    for value in _neighbours(current, snap):
        if field == "atrStop":
            assert plan.atrStop is not None
            changed = plan.model_copy(update={"atrStop": plan.atrStop.model_copy(update={"multiple": value})})
        else:
            changed = plan.model_copy(update={"stopLossTicks": int(value)})
        variants.append({"plan": changed, "label": f"{_value_text(value)}{' 倍' if field == 'atrStop' else ' 跳'}"
                         + ("（当前）" if value == current else ""), "value": value})
    return variants, {"paramKey": field, "paramLabel": label.split("（")[0], "paramHelp": None, "linked": []}


def compare_line(lab: dict[str, Any]) -> str | None:
    """后 X 个月单独检验：… — what the held-out last 30% says about the strategy's current row."""
    request = lab.get("request") or {}
    current = request.get("currentVariant")
    result = next((item for item in lab.get("results") or [] if item.get("variantId") == current), None)
    if lab.get("status") != "done" or result is None or result.get("error"):
        return None
    coverage = result.get("coverage") or {}
    split, last = result.get("splitTime"), coverage.get("lastBarTime")
    days = (int(last) - int(split)) / (86400 * 1e9) if split and last else 0
    span = f"后 {max(1, round(days / 30))} 个月" if days >= 45 else f"后 {max(1, round(days))} 天"
    out = result.get("outSample") or {}
    name = str(result.get("label") or "").replace("（当前）", "")
    if not out.get("trades"):
        return f"{span}单独检验：{name}那组没开出单，说明不了稳不稳。"
    net = float(out.get("net") or 0)
    from .strategy_verdict import money
    if net > 0:
        return f"{span}单独检验：{name}那组仍赚 {money(net)}，比较稳。"
    return f"{span}单独检验：{name}那组亏了 {money(net)}，前面赚的可能是凑出来的，实盘要当心。"
