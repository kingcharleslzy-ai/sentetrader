"""交割月/到期主动提醒 (2026-10-02；#932 起由 Runtime 自己判定)：每个交易日、每个带提醒级别的持仓合约
一条——Runtime 的 30 秒账户循环（AccountReminderLoop，与收盘前/风险度/保本同一次读取）对每个已装配账户的
每位 owner 判定，客户端没开着也照常提醒，不再挂在客户端读持仓上。经统一提醒入口投递：交割提醒的开关管全部
渠道；对话落点按 reminder_target_session（在线且绑定该账户 → 绑定该账户的最近会话 → 用户最近会话），
turnId delivery-<symbol>-<交易日>；提醒中心一条、feed.reminder（桌面通知）、通话在线时口播、锁屏推送。
一天一个合约一条（认领与旧 delivery_notices 账本行同一事务，重启、蓝绿双槽都不重发）。只提醒，不平仓，
也不给下单加任何拒绝条件。回放里没有“今天”，不发交割提醒。"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from typing import Any

from .conversation_cards import ask_action, reminder_card, ui_action
from .delivery_rules import foreign_expiry_notice, trading_today
from .instruments import foreign_trade_name
from .reminder_kinds import reminder_enabled
from .trading_calendar import cme_trading_day

log = logging.getLogger("uvicorn.error.delivery")


def _notice_parts(name: str, symbol: str, notice: dict[str, Any],
                  today: str) -> tuple[str, str, str, dict[str, Any]]:
    """消息文本、turnId、时间戳与提醒卡（纯构造，不碰库）。"""
    day = str(notice.get("lastHoldDay") or "")
    text = f"交割提醒：{name}（{symbol}）{notice.get('text') or ''}"
    turn_id = f"delivery-{symbol}-{today}"
    at = datetime.now(UTC).isoformat()
    left = notice.get("tradingDaysLeft")
    # The text plus its reminder card under the same turn (2026-10-04): the last holding day and
    # the trading days left; the card asks the AI or opens the positions panel, never an order.
    card = reminder_card(
        turn_id=turn_id, kind="delivery", title="交割提醒", at=at, symbol=symbol,
        summary=f"{name}（{symbol}）{notice.get('text') or ''}",
        figures=[("最后持仓日", day or None),
                 ("剩余交易日", f"{left} 天" if isinstance(left, int) else None,
                  "danger" if notice.get("level") == "danger" else "warning")],
        actions=[ask_action("问 AI 怎么办", f"{name}快到交割了，要怎么处理这笔持仓？"),
                 ui_action("打开持仓", {"panel": "positions"})])
    return text, turn_id, at, card


async def check_positions(app: Any, *, user_id: str, positions: list[dict[str, Any]],
                          account_id: str | None = None) -> int:
    """One owner's positions on one account, judged once per round; returns how many reminders went out.
    Never raises — a reminder problem must not break the loop it rides on."""
    sent = 0
    try:
        shared = getattr(app.state, "shared_market", None)
        if shared is None or getattr(app.state, "replay_context", None) is not None:
            return 0  # 回放里没有“今天”，不发交割提醒
        held = [position for position in positions if int(position.get("volume") or 0) > 0]
        if not held or not reminder_enabled(app.state.store.user_preferences(user_id).model_dump(), "delivery"):
            return 0
        catalog = await shared.instruments()
        # 只按持仓代码建索引（2026-10-13 省 CPU：目录约 900 条）。
        wanted = {str(position.get("symbol") or "").strip().upper() for position in held}
        items = {symbol: item for item in catalog.get("items", [])
                 if (symbol := str(item.get("symbol", "")).upper()) in wanted}
        today = trading_today(trading_days=await shared.trading_days()).isoformat()
        foreign = await _foreign_notices(app, [symbol for symbol in wanted if foreign_trade_name(symbol)])
        seen: set[str] = set()
        for position in held:
            symbol = str(position.get("symbol") or "").strip()
            item = items.get(symbol.upper())
            notice = (item or {}).get("deliveryNotice")
            if symbol.upper() in foreign:
                # 美元子账户的外盘月份合约：快期目录里没有，按 MT5 合约表的最后交易日提醒，交易日按 CME 算。
                item, notice = foreign[symbol.upper()]
            if symbol in seen or not isinstance(notice, dict) or notice.get("level") not in ("warning", "danger"):
                continue
            seen.add(symbol)
            name = str((item or {}).get("instrumentName") or symbol)
            day = (cme_trading_day(int(datetime.now(UTC).timestamp() * 1e9)) if symbol.upper() in foreign else today)
            if await _deliver(app, user_id=user_id, account_id=account_id or "", symbol=symbol, name=name,
                              notice=notice, today=day):
                sent += 1
    except Exception as error:  # noqa: BLE001 - the ride-along never breaks the loop
        log.debug("delivery reminder check failed: %s", error)
    return sent


async def _foreign_notices(app: Any, symbols: list[str]) -> dict[str, tuple[dict[str, Any], dict[str, Any] | None]]:
    """外盘月份合约 → (目录条目, 到期提醒或 None)，取自外盘可下单目录（MT5 合约表）。读不到就空（这一轮不提醒）。"""
    services = getattr(getattr(app.state, "ctx", None), "services", None)
    market = getattr(services, "foreign_market", None)
    if not symbols or market is None:
        return {}
    try:
        catalog = await asyncio.to_thread(market.trade_catalog)
    except Exception as error:  # noqa: BLE001 — the MT5 table unreadable: no foreign reminder this round
        log.debug("foreign delivery catalog unavailable: %s", error)
        return {}
    today = date.fromisoformat(cme_trading_day(int(datetime.now(UTC).timestamp() * 1e9)))
    found: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    for item in catalog.get("items") or []:
        symbol = str(item.get("symbol") or "").upper()
        if symbol in symbols and item.get("expireDate"):
            found[symbol] = (item, foreign_expiry_notice(date.fromisoformat(str(item["expireDate"])), today))
    return found


async def _deliver(app: Any, *, user_id: str, account_id: str, symbol: str, name: str,
                   notice: dict[str, Any], today: str) -> bool:
    from .reminders import ReminderSpec, deliver
    from .web_push import PushMessage, deep_link
    store = app.state.store
    text, turn_id, at, card = _notice_parts(name, symbol, notice, today)

    def claim(connection: Any) -> bool:
        # The older ledger row a slot running the previous build reads, on the claim's transaction.
        return bool(store.record_delivery_notice(
            user_id=user_id, symbol=symbol, trade_day=today, kind=str(notice.get("kind")),
            level=str(notice.get("level")), text=str(notice.get("text")), connection=connection))

    result = await deliver(app, ReminderSpec(
        kind="delivery", user_id=user_id, account_id=account_id, dedupe_key=f"{symbol}:{today}",
        trading_day=today, title="交割提醒", body=str(card["summary"]), symbol=symbol, name=name, at=at,
        message=text, turn_id=turn_id, cards=[("card.reminder", card)], speech=text,
        voice_kind=f"delivery-{symbol}-{notice.get('lastHoldDay') or today}",
        push=PushMessage(kind="delivery", title=f"交割提醒 · {name}", body=str(notice.get("text") or ""),
                         short=f"{name}快到交割了", tag=f"delivery-{symbol}-{today}",
                         url=deep_link("card", str(card["cardId"]))),
        legacy=claim))
    return result.reminder is not None
