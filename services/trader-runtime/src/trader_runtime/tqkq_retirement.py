"""快期模拟停用交易（所有者 2026-10-30，#1205）：本站模拟盘接替快期模拟做交易账户。

配置了本站模拟盘的部署每次启动跑一遍 `retire_tqkq_trading`（幂等）：每个仍在用的成员快期
账户（`tqkq-pool`，行情专用的 `market-source:` 账户除外）——
- 在行情源列表里：账户行与托管口令保留（行情接力还用它登录），只删交易授权；
- 不在：整套解绑——停子进程、删托管口令、删授权、停用账户行（以前「解绑」按钮做的事）。
两种都把持有人的默认账户和停在这个账户上的会话挪到他自己的本站模拟账户（没有就先开）。
还有监控中的策略、已挂的止损或未完成委托的账户跳过并记日志，下次启动再看。
历史（委托、成交、会话）一律保留。主人的静态主账号不在此列：它在 wiring.services 里失去下单网关。
"""
from __future__ import annotations

import logging
from typing import Any

from .store.trading_accounts import is_market_source_owner

log = logging.getLogger("uvicorn.error.tqkq_retirement")


async def retire_tqkq_account(ctx: Any, account_id: str, *, keep_credentials: bool) -> None:
    """Take one 快期 pool account out of trading. Every grantee's own paper account is
    opened (or found) first, so a paper-engine failure raises before anything changes.
    `keep_credentials` (a market source) keeps the row, the sealed password and the
    child the market relay uses; otherwise the child stops, the password is deleted
    and the row disabled. Grants go either way; defaults and sessions move to paper."""
    store = ctx.services.store
    record = store.get_trading_account(account_id)
    owner = str(record["execution_owner_user_id"])
    papers = {user_id: str((await ctx.paper_accounts.ensure_paper_account(user_id, make_default=False))["id"])
              for user_id in store.list_account_grantees(account_id=account_id)}
    pool = ctx.account_pool
    if pool is not None:
        if keep_credentials:
            await pool.detach(account_id, reason="tqkq_retired")
        else:
            await pool.release(account_id, reason="tqkq_retired")
    if not keep_credentials:
        credentials = store.load_tqkq_credentials(user_id=owner)
        if credentials is not None and str(credentials["account_id"]) == account_id:
            store.delete_tqkq_credentials(user_id=owner)
    store.delete_account_grants(account_id=account_id)
    if not keep_credentials:
        store.disable_trading_account(account_id)
    for user_id, paper_id in papers.items():
        if store.get_user_default_account(user_id=user_id) in (None, account_id):
            store.set_user_default_account(user_id=user_id, account_id=paper_id)
        sessions = store.user_sessions_on_account(user_id=user_id, trading_account_id=account_id)
        # All sessions rebind first (526-C2): a failing push must not leave the rest of them
        # pointed at the retired account. A real move advances the session's account revision.
        for session_id in sessions:
            store.set_session_trading_account(session_id, paper_id, user_id=user_id)
        for session_id in sessions:
            try:
                ctx.runtime_feed.session_account_changed(session_id, paper_id)
                await ctx.services.broker.publish(
                    session_id, kind="result", name="runtime.status",
                    payload=ctx.status_for_session(session_id))
            except Exception:  # noqa: BLE001 — best-effort: the client picks the rebind up on its next poll
                log.warning("tqkq_retire_rebind_push_failed session=%s", session_id, exc_info=True)


async def retire_tqkq_trading(ctx: Any, *, market_sources: list[str]) -> list[str]:
    """The boot step: retire every member 快期 trading account still in use; the ids retired."""
    if ctx.paper_accounts is None:
        return []
    store = ctx.services.store
    sources = set(market_sources)
    busy = set(store.list_busy_paper_accounts(provider_kind="tqkq-pool"))
    retired = []
    for record in store.list_trading_accounts():
        account_id = str(record["id"])
        if (record["provider_kind"] != "tqkq-pool" or record["status"] != "active"
                or is_market_source_owner(record["execution_owner_user_id"])):
            continue
        market_source = account_id in sources
        if market_source and not store.list_account_grantees(account_id=account_id):
            continue  # already retired: a market source nobody trades on
        if account_id in busy:
            log.warning("tqkq_retire_skipped account=%s reason=busy", account_id)
            continue
        try:
            await retire_tqkq_account(ctx, account_id, keep_credentials=market_source)
        except Exception:
            log.exception("tqkq_retire_failed account=%s", account_id)
            continue
        log.info("tqkq_retired account=%s kept_for_market=%s", account_id, market_source)
        retired.append(account_id)
    return retired
