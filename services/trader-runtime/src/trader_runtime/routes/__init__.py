"""HTTP routes, one module per domain; registration order is the URL matching order."""
from __future__ import annotations

from fastapi import FastAPI

from ..wiring.context import RuntimeContext
from . import account, execution, memory, replays, research, sessions, strategies, strategy_lab, system


def register_all(application: FastAPI, ctx: RuntimeContext) -> None:
    replays.register(application, ctx)
    system.register(application, ctx)
    sessions.register(application, ctx)
    account.register(application, ctx)
    execution.register(application, ctx)
    strategies.register(application, ctx)
    # After strategies: the lab's choose/run-server reuse the workbench's queue and save paths,
    # which register_workbench (inside strategies) puts on application.state.
    strategy_lab.register(application, ctx)
    research.register(application, ctx)
    memory.register(application, ctx)
    # 今日复盘 (2026-10-06): GET / regenerate / answer the observation; the 15:15 loop lives on app.state.
    from ..daily_review import register as register_daily_review
    register_daily_review(application, ctx)
    # 我的流程 (2026-10-06): list / draft (流程卡) / save (the user's 保存 only) / patch / delete / run.
    from ..routines import register as register_routines
    register_routines(application, ctx)
    # 真实用户监测 (2026-10-13): the pages' minute beacons; a replay child has no pages of its own.
    if ctx.replay_provider is None:
        from ..rum import register as register_rum
        register_rum(application, ctx)
