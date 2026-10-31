"""到价提醒 (2026-10-02): one alert per user per price crossing — a market quote has no trading
account, so neither does an alert. This module owns the creation rules (real contract from the
catalog, price on the tick grid, direction inferred from or checked against the live quote, the
per-user active cap) and the background monitor: every second the active alerts' symbols are read
once each, and a crossing is delivered through the one reminder entry (reminders.deliver, #932) — a
persistent message in the conversation the user is looking at (reminder_target_session: an alert has no
account, so an online session of the user wins over the newest one), the 提醒中心 row, feed.reminder, the voice
on a live call and the push, all behind the one 到价提醒 switch — plus a feed.price-alert data event (the
chart line and the lists drop the alert). An alert only ever reminds; it never places an order, and it fires exactly once
(Active → triggered is a conditional UPDATE, so loop timing and a restart cannot double-fire).
回放子 Runtime 里同样可用 (2026-10-03)：目录、参考价与盯价都只读回放行情（只有本次回放的
合约可设），时间戳用回放时钟，投递只落在回放子进程自己的会话上，提醒随回放一起消失。"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .account_reminders import _preferences
from .chart_drawings import ALERTABLE_KINDS, SLOPED_KINDS, line_price_at
from .conversation_cards import ask_action, reminder_card, ui_action
from .instruments import cme_hours_of, foreign_product_of, foreign_tradable, foreign_trade_name, foreign_trade_symbol
from .order_boundaries import quote_is_fresh
from .reminder_kinds import reminder_enabled
from .trading_calendar import NS, cme_schedule, trading_seconds

log = logging.getLogger("uvicorn.error.pricealerts")

MAX_ACTIVE_ALERTS = 20
MONITOR_POLL_SECONDS = 1.0
DIRECTION_WORDS = {"above": "涨到", "below": "跌到"}


def price_text(price: Any) -> str:
    """A tick-grid price as humans write it: no trailing .0, no scientific notation."""
    try:
        text = format(Decimal(str(price)).normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        return str(price)
    return text


def _parse_price(value: Any) -> Decimal:
    """The creation price as a finite positive Decimal, or a Chinese refusal."""
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("价格看不懂，填一个数字") from error
    if not price.is_finite() or price <= 0:
        raise ValueError("价格要是不小于 0 的数")
    return price


def _align_to_tick(price: Decimal, tick: Any, *, name: str) -> Decimal:
    """The price must sit on the contract's tick grid; warn with the grid's step."""
    try:
        step = Decimal(str(tick))
    except (InvalidOperation, ValueError, TypeError):
        return price
    if step <= 0:
        return price
    steps = price / step
    if steps != steps.to_integral_value():
        raise ValueError(
            f"{price_text(price)} 不在 {name} 的最小变动价位上（每跳 {price_text(step)}）")
    return price


async def _replay_entry(replay: Any, symbol: str) -> tuple[dict[str, Any], Any]:
    """回放子 Runtime (2026-10-03) 的目录条目与参考价：目录里只有本次回放的合约——别的合约
    提醒不了回放价，回放价也提醒不了实盘。参考价读回放行情的最新价（拿不到回落昨结算），
    方向判定与实盘同一套。"""
    try:
        catalog: Any = await asyncio.to_thread(replay.provider.instrument_catalog)
    except Exception as error:
        raise ValueError(f"现在读不到回放的行情，稍后再试（{error}）") from error
    item = next((entry for entry in catalog.get("items", [])
                 if str(entry.get("symbol", "")).upper() == symbol.upper()), None)
    if item is None:
        raise ValueError("回放里只能给本次回放的合约设提醒；要给其他合约设，回主窗口")
    try:
        market = await asyncio.to_thread(replay.provider.market_snapshot, "", str(item["symbol"]))
    except Exception as error:
        raise ValueError(f"现在读不到回放行情，稍后再试（{getattr(error, 'detail', error)}）") from error
    return item, market.get("price") or market.get("lastPrice") or market.get("preSettlement")


def _foreign_market(app: Any) -> Any:
    return getattr(getattr(getattr(app.state, "ctx", None), "services", None), "foreign_market", None)


async def _foreign_entry(app: Any, symbol: str) -> tuple[dict[str, Any], Any]:
    """外盘到价提醒（外盘代码统一，2026-10-21）：提醒挂在 MT5 的具体月份合约上，与美元子账户撮合同一份价格。
    外盘图表的快期外盘主连（KQD.m@COMEX.GC）换成此刻的 MT5 主力月份；只接了参考行情的品种（伦铜、美棉…）没有
    月份合约，拒绝。返回 (外盘可下单目录里的条目, 参考价)。"""
    product = foreign_product_of(symbol)
    market = _foreign_market(app)
    if product is None or market is None:
        raise ValueError("外盘行情服务还没就绪，稍后再试")
    if not foreign_tradable(product):
        raise ValueError(f"{product.name}只接了参考行情，没有 MT5 月份合约，暂不能设到价提醒")
    if symbol.upper().startswith("KQD."):
        board = await market.board()
        row = next((item for item in board.get("rows") or [] if item.get("key") == product.key), None)
        if not row or row.get("source") != "mt5" or not row.get("contract") or row.get("stale"):
            raise ValueError(f"现在取不到{product.name}的 MT5 主力月份（行情桥不通或休市），稍后再试")
        symbol = foreign_trade_symbol(str(row["contract"]))
    try:
        catalog = await asyncio.to_thread(market.trade_catalog)
        quotes = await asyncio.to_thread(market.month_quotes, [symbol])
    except Exception as error:
        raise ValueError(f"现在读不到 {foreign_trade_name(symbol) or symbol} 的 MT5 行情，稍后再试（{error}）") from error
    item = next((entry for entry in catalog.get("items") or []
                 if str(entry.get("symbol", "")).upper() == symbol.upper()), None)
    if item is None:
        raise ValueError(f"外盘目录里没有合约 {symbol}（已到期或 MT5 没列出）；换一个月份，或在外盘图表上设")
    row = quotes.get(symbol.upper()) or {}
    return item, row.get("last") if row.get("last") is not None else row.get("preSettlement")


def _now_seconds(app: Any) -> int | None:
    """此刻（秒）：回放子 Runtime 里是回放时钟（读不到为 None），否则墙钟。"""
    replay = getattr(app.state, "replay_context", None)
    if replay is None:
        return int(time.time())
    try:
        return int(replay.provider.clock().timestamp())
    except Exception:  # noqa: BLE001 - 回放快照暂不可读
        return None


async def _line_clock(app: Any, symbols: set[str]) -> tuple[dict[str, Any], list[str]]:
    """What a sloped line's trading time needs: each contract's sessions (the catalog's, a foreign month's CME hours)
    and the known trading days (none in a replay: the weekday rule)."""
    from .account_reminders import _catalog_items, normalize_schedule
    wanted = {symbol.upper() for symbol in symbols}
    schedules: dict[str, Any] = {}
    for item in await _catalog_items(app):
        symbol = str(item.get("symbol") or "").upper()
        if symbol in wanted:
            schedules[symbol] = normalize_schedule(item.get("tradingSchedule"))
    for symbol in symbols:
        if foreign_trade_name(symbol):
            schedules[symbol.upper()] = cme_schedule(hours=cme_hours_of(symbol))
    shared: Any = getattr(app.state, "shared_market", None)
    if shared is None or getattr(app.state, "replay_context", None) is not None:
        return schedules, []
    return schedules, await shared.trading_days()


def line_target(drawing: dict[str, Any], now: int, schedules: dict[str, Any], days: list[str]) -> Decimal | None:
    """The price a drawing's alert watches at `now` (seconds), or None where the line is not on the chart."""
    schedule = schedules.get(str(drawing.get("symbol") or "").upper())
    price = line_price_at(drawing, now, lambda start, end: trading_seconds(start * NS, end * NS, schedule, days))
    return None if price is None else Decimal(str(round(price, 6)))


async def create_alert(app: Any, *, user_id: str, symbol: str, price: Any,
                       direction: str | None, note: str, via: str,
                       session_id: str | None, drawing_id: str | None = None) -> dict[str, Any]:
    """The creation rules, shared by the HTTP route and the AI tools (the tools go through the
    route). Every refusal is a Chinese sentence; the caller maps ValueError → 422. In a replay
    child the catalog and the reference price come from the replay market only.
    `drawing_id` (碰到这条线就提醒, 改版 2026-10-31): the alert watches one of the user's drawings — its contract and
    price are the line's (a sloped line's price where it is now, on the tick grid), and it goes with the line."""
    drawing: dict[str, Any] | None = None
    if drawing_id:
        drawing = app.state.store.get_chart_drawing(user_id=user_id, drawing_id=drawing_id)
        if drawing is None:
            raise ValueError("这条线已经不在了")
        if str(drawing.get("kind")) not in ALERTABLE_KINDS:
            raise ValueError("形状和标注不是价位，不能设提醒")
        symbol = str(drawing["symbol"])
    symbol = str(symbol or "").strip()
    if not symbol:
        raise ValueError("要先选一个合约")
    if symbol.upper().startswith("KQ.M@") or ".MAIN" in symbol.upper() or symbol.upper().endswith("@MAIN"):
        raise ValueError("主连不能直接设提醒；说“螺纹主力”会换成当前主力合约，或自己选一个具体合约")
    replay = getattr(app.state, "replay_context", None)
    shared: Any = getattr(app.state, "shared_market", None)
    reference: Any = None
    item: Any = None
    if replay is not None:
        item, reference = await _replay_entry(replay, symbol)
    elif foreign_product_of(symbol) is not None:
        item, reference = await _foreign_entry(app, symbol)
        if reference is None:
            raise ValueError(f"现在读不到 {item.get('instrumentName') or item['symbol']} 的 MT5 最新价，稍后再试")
    else:
        if shared is None:
            raise ValueError("行情服务还没就绪，稍后再试")
        try:
            catalog = await shared.instruments()
        except Exception as error:
            raise ValueError(f"现在读不到合约目录（{error}），稍后再试") from error
        item = next((entry for entry in catalog.get("items", [])
                     if str(entry.get("symbol", "")).upper() == symbol.upper()), None)
        if item is None:
            raise ValueError(f"目录里没有合约 {symbol}；换一个实际合约，或说品种名让 AI 找主力")
    symbol, name = str(item["symbol"]), str(item.get("instrumentName") or item["symbol"])
    if direction is not None and direction not in DIRECTION_WORDS:
        raise ValueError("方向只能不填（按最新价自动判定）、above（涨到）或 below（跌到）")
    if len(note) > 60:
        raise ValueError("备注最多 60 个字")
    if drawing is not None:
        price = await _drawing_price(app, drawing, symbol, item.get("priceTick"))
    target = _align_to_tick(_parse_price(price), item.get("priceTick"), name=name)
    # Only the trigger needs a fresh quote. Setting tomorrow's alert after the close or on a
    # holiday is the normal case, so the direction is judged against the last traded price —
    # read through the quote-only extras channel (2026-10-03), never a chart series. A 盘前
    # row with no trade yet falls back to 昨结算, the reference the exchange itself uses.
    # In a replay child the reference came from the replay market above.
    if reference is None and replay is None:
        try:
            payload = await shared.quotes([symbol])
        except Exception as error:
            raise ValueError(f"现在读不到 {name} 的最新价，稍后再试（{getattr(error, 'detail', error)}）") from error
        row = next((entry for entry in payload.get("rows", [])
                    if str(entry.get("symbol", "")).upper() == symbol.upper()), None)
        reference = (row or {}).get("last") if (row or {}).get("errorCode") is None else None
        if reference is None:
            reference = (row or {}).get("preSettlement")
    if reference is None:
        raise ValueError(f"现在读不到 {name} 的最新价，稍后再试")
    last = _parse_price(reference)
    if direction is None:
        if target == last:
            raise ValueError(f"{name} 现价就是 {price_text(last)}，不用设提醒；改一个不同的价")
        direction = "above" if target > last else "below"
    elif direction == "above" and last >= target:
        raise ValueError(f"{name} 现价 {price_text(last)} 已在 {price_text(target)} 之上；要等涨到，设一个更高的价")
    elif direction == "below" and last <= target:
        raise ValueError(f"{name} 现价 {price_text(last)} 已在 {price_text(target)} 之下；要等跌到，设一个更低的价")
    # 3050.0 (a float from an AI tool) and 3050 are the same alert price; store it as written.
    target = Decimal(price_text(target))
    created = app.state.store.create_price_alert(
        user_id=user_id, symbol=symbol, name=name, direction=direction, price=target,
        note=note.strip(), via=via, session_id=session_id, max_active=MAX_ACTIVE_ALERTS,
        drawing_id=drawing["id"] if drawing is not None else None)
    if isinstance(created, str):
        raise ValueError(created)
    return created


async def _drawing_price(app: Any, drawing: dict[str, Any], symbol: str, tick: Any) -> Decimal:
    """Where the line is now, rounded onto the contract's tick grid (a drawing is not snapped server-side)."""
    now = _now_seconds(app)
    sloped = str(drawing.get("kind")) in SLOPED_KINDS
    schedules, days = await _line_clock(app, {symbol}) if sloped else ({}, [])
    if sloped and not schedules.get(symbol.upper()):
        raise ValueError("读不到这个合约的交易时段，现在设不了斜线提醒；水平线可以")
    found = None if now is None else line_target({**drawing, "symbol": symbol}, now, schedules, days)
    if found is None:
        extended = {**drawing, "symbol": symbol, "style": {**(drawing.get("style") or {}), "extendLeft": True, "extendRight": True}}
        if now is not None and line_target(extended, now, schedules, days) is not None:
            raise ValueError("这条线现在不在图上（线段已经画到头）；在样式里向右延长后再设提醒")
        raise ValueError("这条线的两个点不在交易时段里，算不出它此刻的位置；把点放到有 K 线的地方再设")
    try:
        step = Decimal(str(tick))
    except (InvalidOperation, ValueError, TypeError):
        return found
    return (found / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step if step > 0 else found


def rows_to_quotes(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Quote-only rows (报价总表 or the 只订报价的 extras channel) → market dicts
    `quote_is_fresh` judges. A row carrying an errorCode (unknown symbol, cap) is
    skipped, never mapped; a row without a live trade (盘前 / 当日无成交 / 不可用)
    is not current."""
    try:
        captured = datetime.fromisoformat(str(payload.get("capturedAt")).replace("Z", "+00:00"))
        age = max(0.0, (datetime.now(UTC) - captured).total_seconds())
    except (TypeError, ValueError):
        age = None
    quotes: dict[str, dict[str, Any]] = {}
    for row in payload.get("rows", []):
        if row.get("errorCode"):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        quotes[symbol] = {
            "symbol": row.get("symbol"), "lastPrice": row.get("last"), "price": row.get("last"),
            "dataStatus": payload.get("dataStatus") if row.get("status") == "normal" else "stale",
            "isCached": bool(payload.get("isCached")), "ageSeconds": age,
            "quoteDatetime": row.get("quoteTime"),
        }
    return quotes


def alert_line(alert: dict[str, Any], *, last_price: Any = None) -> str:
    """The one-line receipt the UI, the tools and the trigger message all read."""
    name = str(alert.get("name") or alert.get("symbol") or "")
    line = (f"{name} 碰到画的线 提醒（设时线在 {price_text(alert.get('price'))}）" if alert.get("drawingId")
            else f"{name} {DIRECTION_WORDS.get(str(alert.get('direction')), '')} {price_text(alert.get('price'))} 提醒")
    if last_price is not None:
        line += f"，现价 {price_text(last_price)}"
    if alert.get("note"):
        line += f"，备注：{alert['note']}"
    return line


class PriceAlertMonitor:
    """One background loop for every active alert of every user of this boot."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._task: asyncio.Task | None = None
        self._clock: tuple[frozenset[str], float, tuple[dict[str, Any], list[str]]] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(MONITOR_POLL_SECONDS)
            try:
                await self._tick()
            except Exception as error:  # one bad second never stops the loop
                log.debug("price alerts tick failed: %s", error)

    async def _tick(self) -> None:
        alerts = self.app.state.store.active_price_alerts()
        if not alerts:
            return
        replay = getattr(self.app.state, "replay_context", None)
        if replay is not None:
            await self._tick_replay(replay, alerts)
            return
        shared = getattr(self.app.state, "shared_market", None)
        if shared is None:
            return
        symbols = sorted({str(alert["symbol"]) for alert in alerts})
        # 主力合约 (2026-10-02): the quote-only board — one shared read for every user,
        # no kline series. 非主力合约 (2026-10-03): the quote-only extras channel, 20
        # symbols per read — watching one no longer holds one of the public market's
        # 48 shared kline series. A stale or unreadable quote skips this round only.
        # 外盘月份合约（CME.GCEZ26）按它自己的 MT5 报价判（与美元子账户撮合同一份价格），不走快期。
        foreign = await self._foreign_quotes([symbol for symbol in symbols if foreign_trade_name(symbol)])
        symbols = [symbol for symbol in symbols if not foreign_trade_name(symbol)]
        board = await self._board_quotes(shared) if symbols else {}
        extras = [symbol for symbol in symbols if symbol.upper() not in board]
        extra_quotes: dict[str, dict[str, Any]] = {}
        for start in range(0, len(extras), 20):
            try:
                extra_quotes.update(rows_to_quotes(await shared.quotes(extras[start:start + 20])))
            except Exception:
                continue
        quotes: dict[str, dict[str, Any] | None] = {}
        for symbol in symbols:
            read = board.get(symbol.upper()) or extra_quotes.get(symbol.upper())
            if read is not None:
                try:
                    quote_is_fresh(read, allow_fixture=True)  # a stale quote skips this round
                except Exception:
                    read = None
            quotes[symbol] = read
        quotes.update(foreign)
        lines = await self._sloped_targets(alerts)
        for alert in alerts:
            quote = quotes.get(str(alert["symbol"]))
            if quote is None:
                continue
            try:
                target = Decimal(str(alert["price"]))
                last = Decimal(str(quote.get("lastPrice", quote.get("price"))))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if str(alert["id"]) in lines:
                line = lines[str(alert["id"])]
                if line is None:
                    continue  # the line is not on the chart now (a segment that ended) or its sessions are unknown
                target = line
            crossed = (str(alert["direction"]) == "above" and last >= target) or (
                str(alert["direction"]) == "below" and last <= target)
            if not crossed:
                continue
            await self._trigger_and_deliver(
                alert, quote=quote, last=last, user_id=str(alert["user_id"]), line=target)

    async def _sloped_targets(self, alerts: list[dict[str, Any]]) -> dict[str, Decimal | None]:
        """碰到这条线就提醒 on a sloped line (改版 2026-10-31): alert id → where its line is this round, read from the
        drawing as it stands (a level's alert keeps its stored price: moving the line re-arms it). The quote is
        judged fresh first, as for every alert, so a closed market or a stale feed never fires one."""
        store = self.app.state.store
        drawings: dict[str, dict[str, Any]] = {}
        for alert in alerts:
            if not alert.get("drawing_id"):
                continue
            drawing = store.get_chart_drawing(user_id=str(alert["user_id"]), drawing_id=str(alert["drawing_id"]))
            if drawing is not None and str(drawing.get("kind")) in SLOPED_KINDS:
                drawings[str(alert["id"])] = {**drawing, "symbol": str(alert["symbol"])}
        if not drawings:
            return {}
        now = _now_seconds(self.app)
        symbols = frozenset(drawing["symbol"] for drawing in drawings.values())
        cached = self._clock
        if cached is None or cached[0] != symbols or cached[1] < time.monotonic():
            # The sessions and the trading days change once a day: read them once a minute, not every second.
            self._clock = cached = (symbols, time.monotonic() + 60, await _line_clock(self.app, set(symbols)))
        schedules, days = cached[2]
        return {key: None if now is None else line_target(drawing, now, schedules, days)
                for key, drawing in drawings.items()}

    async def _foreign_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any] | None]:
        """外盘月份合约 → 一个 `quote_is_fresh` 判定过的 MT5 报价（最近一笔超过 15 秒的这一轮跳过）。一次批量读。"""
        market = _foreign_market(self.app)
        if not symbols or market is None:
            return {}
        try:
            rows = await asyncio.to_thread(market.month_quotes, symbols)
        except Exception:  # noqa: BLE001 — MT5 down: these alerts wait for the next round
            return {}
        quotes: dict[str, dict[str, Any] | None] = {}
        for symbol in symbols:
            row = rows.get(symbol.upper())
            read: dict[str, Any] | None = None
            if row is not None and row.get("last") is not None:
                read = {"symbol": symbol, "lastPrice": row["last"], "price": row["last"], "dataStatus": "normal",
                        "isCached": False, "ageSeconds": 0.0, "quoteDatetime": row.get("quoteTime")}
                try:
                    quote_is_fresh(read, allow_fixture=False)
                except Exception:
                    read = None
            quotes[symbol] = read
        return quotes

    async def _board_quotes(self, shared: Any) -> dict[str, dict[str, Any]]:
        """Main-contract quotes from the 报价总表 read (quote-only subscriptions, 1 s shared cache):
        symbol → a market dict `quote_is_fresh` judges. Empty when the board is unreadable, so
        every alert falls back to its own read rather than going blind."""
        try:
            return rows_to_quotes(await shared.board())
        except Exception:
            return {}

    async def _tick_replay(self, replay: Any, alerts: list[dict[str, Any]]) -> None:
        """回放子 Runtime 的盯价轮 (2026-10-03)：每轮把回放合约的最新价读一遍（回放快照过期
        就整轮跳过，与实盘的“过期行情不判定”同义），判定、条件触发与投递和实盘同一套；
        时间戳用回放时钟，落库也用它（triggered_at 是回放里的“现在”）。"""
        provider = replay.provider
        try:
            at = provider.clock().isoformat()
        except Exception:  # 快照暂不可读：这一轮跳过，提醒不丢
            return
        markets: dict[str, dict[str, Any] | None] = {}
        for symbol in sorted({str(alert["symbol"]) for alert in alerts}):
            try:
                markets[symbol] = await asyncio.to_thread(provider.market_snapshot, "", symbol)
            except Exception:
                markets[symbol] = None
        lines = await self._sloped_targets(alerts)
        for alert in alerts:
            market = markets.get(str(alert["symbol"]))
            if market is None or market.get("marketSessionStatus") == "closed":
                continue  # 读不到或回放已走完：跳过这一轮
            try:
                target = Decimal(str(alert["price"]))
                last = Decimal(str(market.get("price") or market.get("lastPrice")))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if str(alert["id"]) in lines:
                line = lines[str(alert["id"])]
                if line is None:
                    continue
                target = line
            crossed = (str(alert["direction"]) == "above" and last >= target) or (
                str(alert["direction"]) == "below" and last <= target)
            if not crossed:
                continue
            await self._trigger_and_deliver(
                alert, quote={**market, "lastPrice": last}, last=last,
                user_id=str(alert["user_id"]), at=at, line=target)

    async def _trigger_and_deliver(self, alert: dict[str, Any], *, quote: dict[str, Any],
                                   last: Decimal, user_id: str, at: str | None = None,
                                   line: Decimal | None = None) -> None:
        """Claim the crossing and deliver it through the one reminder entry (#521, #932): the alert's own
        claim (active → triggered, the once-only guard), the 提醒中心 row and the persistent message + card
        commit in ONE store transaction, so a crash or a failure between them retries next round instead of
        leaving a triggered alert whose message never landed. With 到价提醒 switched off the alert still
        triggers (it is spent, as before) and nothing is announced. The feed.price-alert data event (the
        chart line and the list drop the alert) follows either way. `at` is the trigger moment's clock — the
        replay clock in a replay child, wall clock otherwise."""
        from .reminders import ReminderSpec, deliver
        from .web_push import price_alert_message
        app = self.app
        store = app.state.store
        text, turn_id, moment, card = _notice_parts(alert, quote=quote, at=at, line=line)
        triggered: dict[str, Any] = {}
        enabled = reminder_enabled(_preferences(app, user_id), "price_alert")

        def claim(connection: Any) -> bool:
            row = store.trigger_price_alert(alert_id=str(alert["id"]), trigger_price=last, now=at,
                                            connection=connection)
            if row is not None:
                triggered["row"] = row
            return row is not None

        if enabled:
            name = str(alert["name"] or alert["symbol"])
            await deliver(app, ReminderSpec(
                kind="price_alert", user_id=user_id, dedupe_key=str(alert["id"]), title=f"{card['title']} · {name}",
                body=str(card["summary"]), symbol=str(alert["symbol"]), name=str(alert["name"] or ""), at=moment,
                message=text, turn_id=turn_id, cards=[("card.reminder", card)], speech=text,
                # The kind carries the alert id: a second alert is never swallowed by the first one's line.
                voice_kind=f"price-alert-{alert['id']}",
                push=price_alert_message(name=name, symbol=str(alert["symbol"]), text=text, alert_id=str(alert["id"])),
                legacy=claim))
        else:
            try:
                with store.transaction() as connection:
                    claim(connection)
            except Exception as error:
                log.debug("price alert %s trigger failed: %s", alert["id"], error)
                return
        if "row" not in triggered:
            return
        await self._announce(triggered["row"], quote=quote, text=text, user_id=user_id)

    async def _announce(self, alert: dict[str, Any], *, quote: dict[str, Any], text: str,
                        user_id: str) -> None:
        """The feed.price-alert data event to every subscribed window of the user: the chart line and the
        lists drop the triggered alert (the notification itself is the reminder's feed.reminder)."""
        app = self.app
        store = app.state.store
        last_price = quote.get("lastPrice", quote.get("price"))
        broker = getattr(app.state, "broker", None)
        if broker is None:
            return
        # 回放路径算价用 Decimal：文本照旧（price_text 吃它），但 JSON 事件里必须是
        # float——一条带 Decimal 的瞬时事件会在流上序列化失败，当场剪断整条 SSE。
        feed_last = (float(last_price)
                     if isinstance(last_price, (int, float, str, Decimal)) else None)
        payload = {"id": alert["id"], "symbol": alert["symbol"], "name": alert["name"],
                   "direction": alert["direction"], "price": alert["price"],
                   "lastPrice": feed_last, "note": alert["note"], "text": text,
                   "triggeredAt": alert.get("triggeredAt")}
        for session_id in broker.subscribed_session_ids():
            try:
                if str(store.get_session(session_id).get("userId") or "") != user_id:
                    continue
            except KeyError:
                continue
            try:
                await broker.publish_transient(
                    session_id, kind="result", name="feed.price-alert", payload=payload)
            except Exception as error:
                log.debug("price alert feed not published: %s", error)


def _notice_parts(alert: dict[str, Any], *, quote: dict[str, Any],
                  at: str | None = None, line: Decimal | None = None) -> tuple[str, str, str, dict[str, Any]]:
    """落点一的文本与卡片（纯构造，不碰库）：消息与卡片同一条 turnId。`at` 是触发时刻的
    时钟——回放子 Runtime 里是回放时钟，实盘是墙钟。一条画线提醒（drawing_id）说“碰到你画的线”，
    目标价是这一刻线所在的价（`line`，斜线随时间走）。"""
    last_price = quote.get("lastPrice", quote.get("price"))
    level = price_text(line if line is not None else alert["price"])
    drawn = bool(alert.get("drawing_id"))
    title = "画线提醒" if drawn else "到价提醒"
    verb = "碰到你画的线" if drawn else DIRECTION_WORDS.get(str(alert["direction"]), str(alert["direction"]))
    what = f"{verb}（线在 {level}）" if drawn else f"{verb} {level}"
    note = f"备注：{alert['note']}。" if alert.get("note") else ""
    text = (f"{title}：{alert['name'] or alert['symbol']} {what}"
            f"（现 {price_text(last_price)}）。{note}")
    # The text plus the reminder card under the same turn (2026-10-04): target / last price
    # and the time; its buttons ask the AI or open the chart — never an order.
    turn_id = f"pricealert-{alert['id']}"
    moment = at or datetime.now(UTC).isoformat()
    label = alert["name"] or alert["symbol"]
    card = reminder_card(
        turn_id=turn_id, kind="price_alert", title=title, at=moment, symbol=str(alert["symbol"]),
        summary=f"{label} {what}" + (f"（{alert['note']}）" if alert.get("note") else ""),
        figures=[("线价" if drawn else "目标价", level), ("最新价", price_text(last_price))],
        actions=[ask_action("问 AI 怎么办", f"{label} {what} 了，现在怎么办？"),
                 ui_action("看图表", {"symbol": str(alert["symbol"]), "view": "trade"})])
    return text, turn_id, moment, card
