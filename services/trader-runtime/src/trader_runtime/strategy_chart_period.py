"""Bind chart-relative library rules to concrete execution periods without resetting risk state."""
from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

from .store import utc_now
from .strategy_rules import StrategyPlan, evaluate_plan, resolve_chart_timeframe, uses_chart_timeframe


def bind_definition(definition: dict, timeframe: int | None) -> dict:
    from .strategy_workbench import definition_for
    raw = definition.get("chartPlan") or definition.get("plan")
    if not raw:
        return definition
    plan = StrategyPlan.model_validate(raw)
    if not uses_chart_timeframe(plan):
        return definition
    resolved = resolve_chart_timeframe(plan, timeframe)
    return {**definition, **definition_for(resolved), "chartPlan": plan.model_dump(), "chartTimeframeSeconds": timeframe}


async def rebind_items(items, timeframe, *, store, provider):
    """Caller holds the monitor period lock. Validate all changes before one transaction."""
    from .strategy_funds import round_identity_digest

    changes = []
    for item in items:
        definition = bind_definition(item["definition"], timeframe)
        if definition == item["definition"]:
            continue
        evaluation = None
        if item["status"] in {"monitoring", "ready"}:
            # Only computability on the closed bars: a period change after the close (holiday, night
            # break) must not be refused for quiet quotes; the monitor checks freshness before trading.
            evaluation = await asyncio.to_thread(evaluate_plan, provider, definition, item["symbol"], require_fresh=False)
        digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        # 旧回撤行的摘要迁移 (2026-10-10 #692 复审)：部署前写的回撤状态行存完整定义摘要，切周期后
        # 既不等于新完整摘要、也不等于身份摘要——同轮判定会失配开新轮（stopped 解锁）。身份摘要
        # 前后一致才迁（真改了定义不迁）；账号或摘要不匹配的行不动；只改摘要本身，轮次/峰值/净值/
        # 费用/状态全部原样，且与改周期同一个事务。停机实例本来不轮询，只有这里能覆盖“部署后第一次
        # 轮询之前就切周期”的场景。
        identity = round_identity_digest(definition)
        migrates = identity == round_identity_digest(item["definition"])
        changes.append((item, definition, digest, evaluation, identity, migrates))
    if not changes:
        return []
    with store.transaction() as db:
        for item, definition, digest, evaluation, identity, migrates in changes:
            changed = db.execute("""UPDATE strategy_drafts SET definition_json=?, definition_digest=?, updated_at=?
                WHERE id=? AND user_id=? AND trading_account_id=? AND definition_digest=? AND status=?""",
                (json.dumps(definition, sort_keys=True, ensure_ascii=False), digest, utc_now(),
                 item["strategyId"], item["userId"], item["tradingAccountId"],
                 item["definitionDigest"], item["status"]))
            if changed.rowcount != 1:
                raise ValueError("策略状态已变化，请重试切换周期")
            if migrates:
                db.execute("""UPDATE strategy_drawdown_states SET strategy_digest=?, updated_at=?
                    WHERE strategy_id=? AND user_id=? AND trading_account_id=? AND strategy_digest=?""",
                    (identity, utc_now(), item["strategyId"], item["userId"],
                     item["tradingAccountId"], item["definitionDigest"]))
            if evaluation is not None:
                store.save_strategy_validation_report(report_id=uuid4().hex,
                    strategy_id=item["strategyId"], user_id=item["userId"], strategy_digest=digest,
                    status="passed", checks=[{"id":"closed_bars", "label":"切换周期后的规则与已收盘行情可计算", "status":"passed"}],
                    evidence={"kind":"chart_period_change", "chartTimeframeSeconds":timeframe,
                              "barTime":evaluation["evaluatedBarTime"], "futureData":False}, connection=db)
    return [change[0]["strategyId"] for change in changes]
