"""Wire the push feed to the per-account providers, store and autonomy reads."""
from __future__ import annotations

import asyncio
import logging
import re
from time import monotonic
from typing import Any

from .. import account_reminders
from ..data_providers import provider_read_snapshot
from ..instruments import foreign_trade_name
from ..paper_accounts import PaperProvider
from ..risk import decimal_value, risk_budget_summary
from ..runtime_feed import FeedReadError, RuntimeFeed
from ..simulation_orders import order_is_active
from .accounts import AccountNotAssembled, AccountServices, user_may_use_account
from .context import RuntimeContext
from .services import wire_orders_on_change

log = logging.getLogger("uvicorn.error.agent")


REASON_CODE = re.compile(r"^[a-z0-9_]+$")


def _read_error(error: Exception, fallback: str) -> FeedReadError:
    """The provider's own condition code (market_quote_not_ready, sidecar_reconnecting…)
    is what the client shows. Only a code shape travels: exception prose can carry paths,
    symbols or credentials, so anything else becomes the channel's fallback code and the
    original text stays in the log."""
    detail = str(error.args[0]).strip() if error.args and isinstance(error.args[0], str) else ""
    if REASON_CODE.fullmatch(detail):
        return FeedReadError(detail)
    log.info("feed_read_failed channel=%s detail=%s", fallback, detail or type(error).__name__)
    return FeedReadError(fallback)


def build_feed(ctx: RuntimeContext) -> RuntimeFeed:
    services = ctx.services
    store, broker = services.store, services.broker
    accounts = services.accounts

    def _account(account_id: str | None) -> AccountServices:
        try:
            return accounts.require(account_id or accounts.default.account_id)
        except AccountNotAssembled:
            # Only this session's channels report it; the tick keeps serving
            # every other session instead of dying on the KeyError.
            raise FeedReadError("account_not_assembled") from None

    def _granted_account(account_id: str | None, user_id: str) -> AccountServices:
        """The account a session's user may read: a session row survives losing
        its grant (the member's operator was revoked), and the tick would keep
        streaming that account's positions, fills and leases. Anything the user
        holds no grant on reports `account_not_granted` on its own channels."""
        services_for_account = _account(account_id)
        if not user_may_use_account(store, user_id=user_id,
                                    account_id=services_for_account.account_id,
                                    idp_fallback=store.idp_fallback):
            raise FeedReadError("account_not_granted")
        return services_for_account

    def _feed_session_user(session_id: str) -> str | None:
        session = broker.get_session(session_id)
        return session.user_id if session is not None else None

    def _feed_session_account(session_id: str) -> str | None:
        session = broker.get_session(session_id)
        return (session.trading_account_id or accounts.default.account_id) if session is not None else None

    # Fills and the daily settlement row are booked by the AccountLedger loop,
    # with or without a client attached; the feed only pushes what it reads.
    async def _feed_account_activity(account_id: str | None) -> dict[str, Any] | None:
        provider = _account(account_id).provider
        try:
            async with provider_read_snapshot(provider):
                activity = provider.account_activity()
        except (RuntimeError, OSError, ValueError) as error:
            raise _read_error(error, "account_activity_unavailable") from error
        return activity

    # Share the old browser's 5/15 s read budget across sessions, even on a busy CNY feed.
    foreign_reads: dict[tuple[str, str], tuple[float, dict[str, Any] | FeedReadError]] = {}

    async def _foreign_half(account_id: str, user_id: str) -> dict[str, Any] | None:
        foreign_of = accounts.foreign_account_of
        foreign_id = foreign_of(account_id) if foreign_of is not None else None
        if foreign_id is None:
            return None
        account = _granted_account(foreign_id, user_id)
        now = monotonic()
        for key, (expires, _) in list(foreign_reads.items()):
            if expires <= now:
                del foreign_reads[key]
        key = (foreign_id, user_id)
        if key in foreign_reads:
            cached = foreign_reads[key][1]
            if isinstance(cached, FeedReadError):
                raise cached
            return cached
        provider = account.provider
        try:
            summary = provider.account_summary("")
            projection = await provider.positions_view(wait_seconds=0) if isinstance(provider, PaperProvider) else None
            positions = projection["items"] if projection is not None else provider.positions()
            await account.simulation_orders.recover_once(user_id=user_id, attempts=1, interval=0)
            orders = account.simulation_orders.list(user_id=user_id)
        except RuntimeError as error:
            failure = _read_error(error, "foreign_account_unavailable")
            foreign_reads[key] = (monotonic() + 15, failure)
            raise failure from error
        half = {"accountId": foreign_id, "account": summary,
                "positions": [{**item, "accountId": foreign_id, "currency": "USD",
                               **({"instrumentName": name} if (name := foreign_trade_name(str(item.get("symbol") or ""))) else {})}
                              for item in positions], "orders": orders,
                **({"positionsStatus": {key: value for key, value in projection.items() if key != "items"}}
                   if projection is not None else {})}
        busy = bool(positions) or any(order_is_active(order) for order in orders)
        foreign_reads[key] = (monotonic() + (5 if busy else 15), half)
        return half

    async def _feed_account_overview(account_id: str | None, user_id: str, activity: dict[str, Any]) -> dict[str, Any] | None:
        account = _granted_account(account_id, user_id)
        provider = account.provider
        try:
            summary = provider.account_summary("")
            projection = await provider.positions_view(wait_seconds=0) if isinstance(provider, PaperProvider) else None
            positions = projection["items"] if projection is not None else provider.positions()
        except RuntimeError as error:
            raise _read_error(error, "account_unavailable") from error
        data_status = str(summary.get("dataStatus") or "unavailable")
        today_pnl = decimal_value(summary["todayNetPnl"]) if data_status in {"normal", "simulated"} else None
        try:
            risk = risk_budget_summary(store.active_risk_policy(user_id=user_id), today_pnl=today_pnl,
                                       data_status=data_status, now=account.simulation_orders.trading_now())
        except (KeyError, ValueError) as error:
            raise _read_error(error, "risk_budget_unavailable") from error
        # 收盘前/风险度提醒 (2026-10-03)：流健康的客户端只走这条推送路，不再轮询 HTTP——
        # 与 /api/positions、/api/account/summary 同一套顺风车（各自幂等，互不重复发）。
        # #458：两条都依据同一次读取的这份 summary（上面同一个 provider 读的，同账户）。
        await account_reminders.check_positions(ctx.application, user_id=user_id,
                                                account_id=account.account_id, positions=positions,
                                                summary=summary)
        await account_reminders.check_account(ctx.application, user_id=user_id,
                                              account_id=account.account_id, summary=summary)
        # 交割提醒 (#932) is judged by the Runtime's own 30 s account loop (AccountReminderLoop), not here.
        foreign = await _foreign_half(account.account_id, user_id)
        return {"account": summary, "positions": positions, "activity": activity, "risk": risk,
                **({"foreign": foreign} if foreign is not None else {}),
                **({"positionsStatus": {key: value for key, value in projection.items() if key != "items"}}
                   if projection is not None else {})}

    async def _feed_market(account_id: str | None, symbol: str, duration: int) -> dict[str, Any] | None:
        provider = _account(account_id).provider
        try:
            return await asyncio.to_thread(provider.market_workbench, symbol, duration)
        except (KeyError, RuntimeError) as error:
            raise _read_error(error, "market_unavailable") from error

    # One database read of each account's activity per feed round, shared by every focused symbol
    # (2026-10-13 第二轮省 CPU). The base lives in the round's own scratch (`round_shared`) and goes
    # with it when the tick ends (#862): a reader who left keeps no account's order book alive. Within
    # the round it is reused only for the very provider frame and snapshot version it was built for —
    # an order-link change mid-round (after_commit bumps the version) reads the database again. Keyed
    # by (account, user): another account's or user's book is never reused (#518); the grant is checked
    # on every call before any base is touched. Outside a tick (no round) every call builds its own.
    async def _feed_activity(account_id: str | None, user_id: str, symbol: str,
                             activity: dict[str, Any]) -> dict[str, Any] | None:
        from ..agent_activity import activity_base, activity_snapshot, with_foreign_half
        account = _granted_account(account_id, user_id)
        shared = runtime_feed.round_shared
        key = ("activity_base", account.account_id, user_id)
        version = runtime_feed.snapshot_version
        cached = shared.get(key) if shared is not None else None
        if cached is not None and cached[0] is activity and cached[1] == version:
            base = cached[2]
        else:
            base = activity_base(ctx.application, user_id, account.simulation_orders, account.account_id)
            if shared is not None and (key in shared or len(shared) < 256):
                shared[key] = (activity, version, base)
        snapshot = activity_snapshot(
            ctx.application, user_id, symbol, activity,
            simulation_orders=account.simulation_orders,
            strategy_monitor=account.strategy_monitor,
            trading_account_id=account.account_id,
            base=base,
        )
        # 外盘图表：与 GET /api/app/activity 同一处带上美元子账户那一半（只读已装配的子账户）。
        return await with_foreign_half(ctx.application, snapshot, accounts=accounts, account_id=account.account_id,
                                       user=user_id, symbol=symbol)

    async def _feed_execution(account_id: str | None, user_id: str) -> dict[str, Any] | None:
        autonomy = _granted_account(account_id, user_id).autonomy
        return {
            "authorization": autonomy.current(user_id=user_id),
            "controls": autonomy.controls(user_id=user_id),
            "stops": autonomy.list_stops(user_id=user_id),
            "simulationOnly": True,
            "liveTrading": "disabled",
        }

    runtime_feed = RuntimeFeed(
        broker=broker, session_user=_feed_session_user, session_account=_feed_session_account,
        account_activity=_feed_account_activity,
        account_overview=_feed_account_overview, market_workbench=_feed_market, activity=_feed_activity,
        shared_market=(lambda: getattr(ctx.application.state, "shared_market", None)) if ctx.replay_provider is None else None,
        execution=_feed_execution, snapshot_gated=hasattr(accounts.default.provider, "subscribe_snapshot"),
    )
    # Order links change between provider snapshots; push the activity right away.
    # The same wiring every on-demand assembler (pool and paper accounts) applies.
    for account in accounts.all():
        wire_orders_on_change(account, store=store, runtime_feed=runtime_feed)
    return runtime_feed
