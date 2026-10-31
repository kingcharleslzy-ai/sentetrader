"""回测并发配置的唯一解析入口 (2026-10-12)。

规则回测（策略详情、模板预览、AI 引导/工具、CLI、实验室云端兜底，``strategy_backtest.BacktestQueue``
与计算进程池）和 AI 官方回测（``backtest_coordinator.BacktestCoordinator``）各取所需，都从这里读环境变量：

=============================================  =======  ==============================================
变量                                            默认     含义
=============================================  =======  ==============================================
``AI_TRADER_BACKTEST_GLOBAL_LIMIT``             2        同时运行的回测数（所有用户合计，每条路径各自计）
``AI_TRADER_BACKTEST_USER_LIMIT``               1        每人同时运行的回测数
``AI_TRADER_BACKTEST_QUEUE_WAIT_SECONDS``       60       规则回测在队列里最长等待秒数，超时 429
``AI_TRADER_BACKTEST_WORKERS``                  全局并发  规则回测的常驻计算进程数
``AI_TRADER_BACKTEST_GLOBAL_QUEUE_LIMIT``       32       AI 官方回测排队总数
``AI_TRADER_BACKTEST_USER_QUEUE_LIMIT``         4        AI 官方回测每人排队数
``AI_TRADER_BACKTEST_TIMEOUT_SECONDS``          30       AI 官方回测单次超时
=============================================  =======  ==============================================

已知限制：断开的请求不会取消已启动的规则回测（等计算实际完成才归还名额）；规则回测没有运行超时
（单次最长约 1.7 s，上限 1 万根），只有 AI 官方回测有 ``TIMEOUT_SECONDS``。

空串、非数字、非有限数、0 或负数一律回退默认值，不会把上限变成 0（#765）。
"""
from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_GLOBAL_LIMIT = 2
DEFAULT_USER_LIMIT = 1
DEFAULT_QUEUE_WAIT_SECONDS = 60.0
DEFAULT_GLOBAL_QUEUE_LIMIT = 32
DEFAULT_USER_QUEUE_LIMIT = 4
DEFAULT_TIMEOUT_SECONDS = 30.0


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


@dataclass(frozen=True)
class BacktestSettings:
    global_limit: int
    user_limit: int
    queue_wait_seconds: float
    workers: int
    global_queue_limit: int
    user_queue_limit: int
    timeout_seconds: float


def load_backtest_settings(env: Mapping[str, str] | None = None) -> BacktestSettings:
    env = os.environ if env is None else env
    global_limit = _positive_int(env, "AI_TRADER_BACKTEST_GLOBAL_LIMIT", DEFAULT_GLOBAL_LIMIT)
    return BacktestSettings(
        global_limit=global_limit,
        user_limit=_positive_int(env, "AI_TRADER_BACKTEST_USER_LIMIT", DEFAULT_USER_LIMIT),
        queue_wait_seconds=_positive_float(env, "AI_TRADER_BACKTEST_QUEUE_WAIT_SECONDS", DEFAULT_QUEUE_WAIT_SECONDS),
        workers=_positive_int(env, "AI_TRADER_BACKTEST_WORKERS", global_limit),
        global_queue_limit=_positive_int(env, "AI_TRADER_BACKTEST_GLOBAL_QUEUE_LIMIT", DEFAULT_GLOBAL_QUEUE_LIMIT),
        user_queue_limit=_positive_int(env, "AI_TRADER_BACKTEST_USER_QUEUE_LIMIT", DEFAULT_USER_QUEUE_LIMIT),
        timeout_seconds=_positive_float(env, "AI_TRADER_BACKTEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
    )
