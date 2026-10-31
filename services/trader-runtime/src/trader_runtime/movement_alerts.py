"""盘中异动 (2026-10-06): the Runtime watches, once a minute, the contracts a user holds and the main
contracts of their AI 简报 products, with the same judgment the briefs use
(`market_briefs.market_movement`: 5 minutes moving at least 8 ticks — or three times the usual
range — on 1.5× the volume).

Series: a held contract's 1-minute channel is opened and held by this watch for as long as someone
holds it (released once nobody does), so a movement is seen while the user is away — at most
HELD_CHANNEL_CAP of them, and a new one only while the public market keeps more than
FREE_SLOT_RESERVE channels free (the sources cap kline series and TqSdk never unsubscribes one). A
brief product nobody holds is judged only when its series is already being served; the rest are
skipped and counted in the log.

Who: every signed-in user who owns an assembled account (or has a lock-screen subscription) with
通知哪些事 · 盘中异动 on. A movement is delivered like the 收盘前 reminder — a message plus a reminder
card in the conversation the user is looking at (reminder_target_session: a movement has no
account, so an online session wins over the newest one; 平一半 asks the AI for a close card to confirm, 问 AI 怎么办),
the feed.reminder event (the desktop notification), the voice on a live call, and the lock-screen
push to the devices that have it. Once per movement (`market:{symbol}:{5-minute bucket}:{direction}`
in push_notices, committed with the message) and at most one per user per contract every 15
minutes. It only reminds; it never trades."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .account_reminders import DIRECTION_WORDS, _money, _number
from .conversation_cards import ask_action, reminder_card, ui_action
from .instruments import foreign_trade_name
from .market_briefs import market_movement
from .web_push import kind_enabled, movement_message, owns_account

log = logging.getLogger("uvicorn.error.movement")

MOVEMENT_LOOP_SECONDS = 60.0
SPACING_SECONDS = 15 * 60
HELD_CHANNEL_CAP = 8
FREE_SLOT_RESERVE = 8


def position_words(positions: list[dict[str, Any]], unit: str = "元") -> tuple[str, str, str]:
    """(“6 手空单”, “浮盈 +1,240”, the sentence) for the user's positions in one contract (``unit`` 美元 for a
    USD sub-account's foreign contract)."""
    lots = "、".join(f"{int(_number(item.get('volume')) or 0)} 手{DIRECTION_WORDS.get(str(item.get('direction')), '')}单"
                     for item in positions)
    pnl = sum(_number(item.get("unrealizedPnl")) or 0 for item in positions)
    word = "浮盈" if pnl >= 0 else "浮亏"
    return lots, f"{word} {_money(pnl)}", f"你有 {lots}，{word} {_money(abs(pnl)).lstrip('+')} {unit}。"


class MovementWatch:
    def __init__(self, app: Any, *, interval: float = MOVEMENT_LOOP_SECONDS,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.app = app
        self.interval = interval
        self.now = now
        self._task: asyncio.Task[None] | None = None
        # The 1-minute channels this watch holds open for held contracts (symbol upper → channel).
        self._channels: dict[str, Any] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._release(set())

    def _release(self, keep: set[str]) -> None:
        shared = getattr(self.app.state, "shared_market", None)
        for symbol in [item for item in self._channels if item not in keep]:
            channel = self._channels.pop(symbol)
            try:
                if shared is not None:
                    shared.release_market(channel)
            except Exception:  # noqa: BLE001 — a closed market already retired it
                log.debug("movement channel release failed", exc_info=True)

    async def _hold(self, shared: Any, symbols: list[str], counts: dict[str, int]) -> None:
        """Hold one 1-minute channel per held contract (first HELD_CHANNEL_CAP in a fixed order), open a
        new one only above the free-slot reserve, and release the ones nobody holds any more."""
        # 外盘月份合约（美元子账户，CME.GCEZ26）不占共享行情的 K 线通道：每轮直接读它的 MT5 1 分钟线（_foreign_workbenches）。
        wanted = sorted({symbol.upper(): symbol for symbol in symbols if not foreign_trade_name(symbol)}.items())
        if len(wanted) > HELD_CHANNEL_CAP:
            log.info("movement_watch held=%d cap=%d skipped=%s", len(wanted), HELD_CHANNEL_CAP,
                     ",".join(symbol for _, symbol in wanted[HELD_CHANNEL_CAP:]))
        kept = dict(wanted[:HELD_CHANNEL_CAP])
        self._release(set(kept))
        for key, symbol in kept.items():
            if key in self._channels:
                continue
            free = shared.free_market_slots()
            if free <= FREE_SLOT_RESERVE:
                counts["capacity"] += 1
                log.info("movement_watch channel_skipped symbol=%s free=%d reserve=%d", symbol, free, FREE_SLOT_RESERVE)
                continue
            try:
                self._channels[key] = await shared.acquire_market(symbol, 60)
            except Exception as error:  # noqa: BLE001 — 503 and the like: next round tries again
                log.info("movement_watch channel_failed symbol=%s reason=%s", symbol, getattr(error, "detail", error))

    async def _foreign_workbenches(self, symbols: set[str]) -> dict[str, dict[str, Any] | None]:
        """Held foreign month contracts → their own MT5 1-minute workbench (the price the USD sub-account fills
        at); None when MT5 is down or the month has no quote (that contract is skipped this round)."""
        services = getattr(getattr(self.app.state, "ctx", None), "services", None)
        market = getattr(services, "foreign_market", None)
        result: dict[str, dict[str, Any] | None] = {}
        for symbol in sorted(symbol for symbol in symbols if foreign_trade_name(symbol)):
            try:
                result[symbol.upper()] = (await asyncio.to_thread(market.workbench, symbol, 60)
                                          if market is not None else None)
            except Exception as error:  # noqa: BLE001 — MT5 down: no movement read this round
                log.debug("movement_watch foreign read failed symbol=%s: %s", symbol, error)
                result[symbol.upper()] = None
        return result

    def _workbench(self, shared: Any, symbol: str) -> dict[str, Any] | None:
        channel = self._channels.get(symbol.upper())
        if channel is not None and channel.packet is not None and channel.error is None:
            return channel.packet["value"]["workbench"]
        return shared.served_workbench(symbol, 60)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 — one bad minute never ends the watch
                log.warning("movement_watch_failed", exc_info=True)

    def _users(self) -> list[str]:
        """Users who own an assembled account, and anyone with a lock-screen device (a visitor has neither)."""
        store = self.app.state.store
        users = set(store.push_user_ids())
        for services in list(self.app.state.accounts.all()):
            users.update(store.account_owner_ids(services.account_id))
        return sorted(user for user in users if user)

    def _held(self, user_id: str) -> dict[str, list[dict[str, Any]]]:
        """The user's open positions by contract, over the assembled accounts they own."""
        store = self.app.state.store
        held: dict[str, list[dict[str, Any]]] = {}
        for services in list(self.app.state.accounts.all()):
            if not owns_account(store, services.account_id, user_id):
                continue
            try:
                positions = services.provider.positions()
            except Exception:  # noqa: BLE001
                continue
            for item in positions:
                if int(_number(item.get("volume")) or 0) > 0 and item.get("symbol"):
                    held.setdefault(str(item["symbol"]), []).append(item)
        return held

    async def run_once(self) -> dict[str, int]:
        """One minute's round: counts of users watched, contracts judged, brief products skipped as not
        served, held contracts not given a channel for lack of room, and movements sent."""
        app, store = self.app, self.app.state.store
        shared = getattr(app.state, "shared_market", None)
        briefs = getattr(app.state, "market_briefs", None)
        counts = {"users": 0, "judged": 0, "unserved": 0, "capacity": 0, "fired": 0}
        if shared is None:
            return counts
        now = self.now()
        plans: list[tuple[str, dict[str, list[dict[str, Any]]], set[str]]] = []
        for user_id in self._users():
            if not kind_enabled(store.user_preferences(user_id).model_dump(), "movement"):
                continue
            held = self._held(user_id)
            symbols = set(held)
            if briefs is not None:
                try:
                    symbols.update(await briefs.subscribed_main_symbols(user_id))
                except Exception:  # noqa: BLE001
                    pass
            plans.append((user_id, held, symbols))
        counts["users"] = len(plans)
        await self._hold(shared, [symbol for _, held, _ in plans for symbol in held], counts)
        # 持仓的、以及 AI 简报订阅的外盘月份合约（2026-10-21）都读它自己的 MT5 1 分钟线，不走共享行情通道。
        foreign = await self._foreign_workbenches({symbol for _, _, symbols in plans for symbol in symbols})
        judged: dict[str, dict[str, Any] | None] = {}
        for user_id, held, symbols in plans:
            for symbol in sorted(symbols):
                if symbol.upper() not in judged:
                    workbench = (foreign.get(symbol.upper()) if symbol.upper() in foreign
                                 else self._workbench(shared, symbol))
                    if workbench is None:
                        if symbol not in held:
                            counts["unserved"] += 1
                        judged[symbol.upper()] = None
                        continue
                    counts["judged"] += 1
                    try:
                        movement = market_movement(workbench, now)
                    except (KeyError, TypeError, ValueError):
                        movement = None
                    judged[symbol.upper()] = ({**movement, "name": (workbench.get("quote") or {}).get("name") or symbol}
                                              if movement else None)
                movement = judged[symbol.upper()]
                if movement and await self._deliver(user_id, symbol, movement, held.get(symbol) or [], now):
                    counts["fired"] += 1
        if counts["unserved"] or counts["fired"] or counts["capacity"]:
            log.info("movement_watch users=%d judged=%d unserved=%d capacity=%d fired=%d", counts["users"],
                     counts["judged"], counts["unserved"], counts["capacity"], counts["fired"])
        return counts

    async def _deliver(self, user_id: str, symbol: str, movement: dict[str, Any], positions: list[dict[str, Any]],
                       now: datetime) -> bool:
        app, store = self.app, self.app.state.store
        key = str(movement["id"])
        prefix = key.rsplit(":", 2)[0] + ":"
        name = str(movement["name"])
        up = movement["direction"] == "上涨"
        bucket = key.rsplit(":", 2)[1]
        turn_id = f"movement-{symbol}-{bucket}-{'up' if up else 'down'}"
        at = now.isoformat()
        figures: list[tuple[Any, ...]] = [("5 分钟", f"{'+' if up else '−'}{movement['ticks']} 跳", "warning"),
                                          ("量能", f"{movement['volumeRatio']:.1f} 倍")]
        body = movement["text"]
        if positions:
            lots, pnl, sentence = position_words(positions, "美元" if foreign_trade_name(symbol) else "元")
            figures += [("持仓", lots), ("浮动盈亏", pnl, "positive" if not pnl.startswith("浮亏") else "danger")]
            body = f"{body}{sentence}"
            actions = [ask_action("平一半", f"把我 {name} 的持仓平一半，先出平仓卡给我确认"),
                       ask_action("问 AI 怎么办", f"{name} 刚才5分钟{movement['direction']}{movement['ticks']}跳还放量，我该怎么办？")]
        else:
            actions = [ask_action("问 AI 怎么办", f"{name} 刚才5分钟{movement['direction']}{movement['ticks']}跳还放量，怎么看？"),
                       ui_action("看图表", {"symbol": symbol, "view": "trade"})]
        text = f"盘中异动：{name} {body}"
        card = reminder_card(turn_id=turn_id, kind="market_movement", title=f"盘中异动 · {name}", summary=body,
                             at=at, figures=figures, symbol=symbol, actions=actions)
        from .reminders import ReminderSpec, deliver

        def claim(connection: Any) -> bool:
            # The per-product 15-minute spacing and the push_notices row (read by 今日复盘 and by a slot still
            # running the previous build): both on the claim's own transaction.
            last = store.last_push_notice_at(user_id=user_id, prefix=prefix, connection=connection)
            if last is not None and (now - datetime.fromisoformat(last)).total_seconds() < SPACING_SECONDS:
                return False
            return bool(store.claim_push_notice(user_id=user_id, key=key, connection=connection, at=at, text=text))

        result = await deliver(app, ReminderSpec(
            kind="market_movement", user_id=user_id, dedupe_key=key, title=f"盘中异动 · {name}", body=body,
            symbol=symbol, name=name, at=at, message=text, turn_id=turn_id, cards=[("card.reminder", card)],
            speech=f"{name}{movement['text']}", voice_kind=f"movement-{symbol}",
            push=movement_message(name=name, body=body, card_id=f"reminder-{turn_id}"), legacy=claim))
        return result.reminder is not None
