"""停止 / 急停 AI 模拟执行的唯一入口（网页 POST /api/ai-execution/stop 与 AI 的 stop_simulation_execution 共用）。

外盘三期起，人民币本站模拟账户的外盘策略与外盘单跑在它的美元子账户（paperusd:同号）上——用户眼里是同一个
账户的外盘半边。停在人民币本站模拟账户上时，美元子账户一并停（停自动执行、暂停监控中的策略），不然从人民币
对话里启动的外盘策略在急停后照跑（2026-10-21 发现）。其余账户（快期模拟等）只停自己。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from .instruments import is_foreign_paper

log = logging.getLogger("uvicorn.error.execution_stop")


async def companion_autonomy(accounts: Any, user_id: str, account_id: str) -> Any | None:
    """这个人民币本站模拟账户的美元子账户的 autonomy（按需装配）；不是本站模拟账户、没有子账户或装配不上为 None。"""
    if not str(account_id).startswith("paper:") or is_foreign_paper(account_id):
        return None
    resolver = getattr(accounts, "foreign_account", None)
    if resolver is None:
        return None
    try:
        primary = accounts.get(account_id) if hasattr(accounts, "get") else None
        store = getattr(getattr(primary, "autonomy", None), "store", None)
        record = store.get_trading_account(account_id) if store is not None else None
        owner_id = record["execution_owner_user_id"] if record is not None else user_id
        foreign_id = await resolver(owner_id)
    except Exception:  # noqa: BLE001 — an unreachable sub-account must not block stopping the main one
        log.exception("execution_stop_companion_unavailable account=%s", account_id)
        return None
    if foreign_id is None or str(foreign_id).removeprefix("paperusd:") != str(account_id).removeprefix("paper:"):
        return None
    companion = accounts.get(foreign_id) if hasattr(accounts, "get") else None
    return getattr(companion, "autonomy", None)


def _wake(autonomy: Any) -> None:
    monitor = getattr(autonomy, "strategy_monitor", None)
    if monitor is not None:
        monitor.wake()


async def stop_execution(*, autonomy: Any, proposals: Any, accounts: Any, user_id: str,
                         emergency: bool, scope: Literal["all", "opens"]) -> dict[str, Any]:
    """停当前账户（不变的语义与返回），再停它的美元子账户；子账户停不下来只记在 companion 里，不影响主账户的结果。"""
    result = await autonomy.deactivate(user_id=user_id, emergency=emergency, scope=scope)
    account_id = getattr(autonomy, "account_id", None)
    # scope=opens leaves strategies monitoring: only the full stop pauses them.
    paused = (proposals.pause_monitoring_strategies(user_id=None, trading_account_id=account_id)
              if scope == "all" and proposals is not None and account_id else [])
    _wake(autonomy)
    companion = await companion_autonomy(accounts, user_id, account_id) if account_id else None
    if companion is not None:
        try:
            extra = await companion.deactivate(user_id=user_id, emergency=emergency, scope=scope, parent_account_id=account_id)
            if scope == "all" and proposals is not None:
                paused = [*paused, *proposals.pause_monitoring_strategies(
                    user_id=None, trading_account_id=companion.account_id)]
            _wake(companion)
            result["companion"] = {"accountId": companion.account_id,
                                   "cancelledOrders": extra.get("cancelledOrders") or []}
        except Exception as error:  # noqa: BLE001 — reported, never undoes the main account's stop
            code = getattr(error, "code", None) or type(error).__name__
            log.warning("execution_stop_companion_failed account=%s error=%s", companion.account_id, code)
            result["companion"] = {"accountId": companion.account_id, "error": str(code)}
    result["pausedStrategyIds"] = paused
    return result


async def lift_companion_stop(*, accounts: Any, user_id: str, account_id: str) -> None:
    """用户在人民币本站模拟账户上明确恢复 AI 执行（POST /api/ai-execution/start 或界面“恢复执行”的 /resume）时，美元子账户被一并停下的
    停止状态也解除（只解除停止，不替它开新的授权）；AI 自己发起的恢复不走这里。"""
    companion = await companion_autonomy(accounts, user_id, account_id)
    if companion is not None:
        companion.lift_stop(user_id=user_id)
        _wake(companion)
