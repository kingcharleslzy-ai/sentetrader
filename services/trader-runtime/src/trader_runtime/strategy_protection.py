"""Fill-backed strategy exits. The stop and take-profit share one close identity."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import weakref
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .risk import ceil_to_tick, floor_to_tick
from .store import RuntimeStore, utc_now
from .trading_clock import fill_trading_day


def breakeven_at_r(entry: Decimal, stop: Decimal, tick: Decimal) -> dict[str, int]:
    """浮盈 1R 后移到成本 (2026-10-25)：1R = 开仓价到止损的距离（按跳）；浮盈到这么多跳时止损移到开仓价，用策略的保本
    (strategy_engine.breakeven_stop)。"""
    return {"triggerTicks": max(1, int((abs(entry - stop) / tick).to_integral_value())), "offsetTicks": 0}


def require_current_activity(activity: dict[str, Any]) -> None:
    age = Decimal(str(activity.get("ageSeconds", "NaN")))
    if (activity.get("dataStatus") != "normal" or activity.get("isCached")
            or not age.is_finite() or not 0 <= age <= 15
            or activity.get("ordersInitialized") is not True
            or not isinstance(activity.get("trades"), list)
            or not isinstance(activity.get("orders"), list)):
        raise RuntimeError("strategy_activity_not_current")


def account_protection_day(summary: dict[str, Any]) -> str:
    """The fill Owner's existing account-day validation, shared with read-only previews."""
    day = str(summary.get("tradingDay", ""))
    datetime.strptime(day, "%Y-%m-%d")
    if summary.get("dataStatus") != "normal" or summary.get("isCached"):
        raise RuntimeError("strategy_account_not_current")
    return day


def fill_protection_bucket(row: Any, metadata: dict[str, Any], *, day: str) -> str:
    """The existing fill-backed bucket rule; entry day, never the protection row's creation day."""
    return ("today" if metadata["entryTradingDay"] == day else "historical") if row["position_bucket"] != "all" else "all"


# 只处理新变化 (2026-10-22): the autonomy loop syncs every 0.5 s per account. The inputs a sync acts on are
# today's fills, the finished orders, the trading day and this account's protection links, stops and recorded
# fill events; when none of them changed since the last sync that ran clean, there is nothing to do. Keyed by
# store, then account; holds the inputs as they were read before that clean sync.
_CLEAN_SYNC: weakref.WeakKeyDictionary[Any, dict[str, str]] = weakref.WeakKeyDictionary()


def _sync_inputs(store: RuntimeStore, account: str, activity: dict[str, Any], day: str) -> str:
    """A digest of everything one account's sync reads. The links' `fact_revision` moves with every change of
    an order's phases, remote status or report (store trigger); stops and protection configs are read whole."""
    digest = hashlib.sha256(day.encode())
    for trade in sorted((str(t.get("clientOrderId")), str(t.get("tradeId"))) for t in activity["trades"]):
        digest.update(f"t{trade}".encode())
    for order in sorted(str(o.get("clientOrderId")) for o in activity["orders"] if o.get("status") == "FINISHED"):
        digest.update(f"f{order}".encode())
    with store.lock:
        row = store.connection.execute(
            """SELECT
                (SELECT count(*) || ':' || coalesce(max(id), '') || ':' || coalesce(sum(fact_revision), 0)
                 FROM tqkq_sim_order_links WHERE trading_account_id = ?),
                (SELECT group_concat(client_order_id || '=' || protection_json, '|')
                 FROM tqkq_sim_order_links WHERE trading_account_id = ? AND protection_json IS NOT NULL),
                (SELECT group_concat(id || '=' || status || ':' || coalesce(remaining_volume, '') || ':'
                     || coalesce(close_client_order_id, '') || ':' || coalesce(position_bucket, '') || ':'
                     || coalesce(last_error, '') || ':' || coalesce(strategy_metadata_json, ''), '|')
                 FROM ai_stop_orders WHERE trading_account_id = ?),
                (SELECT count(*) || ':' || coalesce(max(settled), 0)
                 FROM strategy_fill_events WHERE trading_account_id = ?)""",
            (account, account, account, account)).fetchone()
    for part in row:
        digest.update(b"\x00" + str(part).encode())
    return digest.hexdigest()


def sync_protection(store: RuntimeStore, provider: Any, *, user_id: str | None = None,
                    symbol_filter: str | None = None,
                    trading_account_id: str | None = None) -> dict[tuple[str, str], str]:
    # 4.x 多账户: every fill-cursor, dedup, close allocation and roll below reads and writes
    # this account's own book only — one account's close must never consume another's protection.
    account = trading_account_id or store.default_trading_account_id
    sources = store.connection.execute(
        """SELECT * FROM tqkq_sim_order_links
        WHERE protection_json IS NOT NULL AND trading_account_id = ?""",
        (account,),
    ).fetchall()
    if not sources:
        return {}
    activity = provider.account_activity()
    require_current_activity(activity)
    day = account_protection_day(provider.account_summary(""))
    clean = _CLEAN_SYNC.setdefault(store, {})
    inputs = _sync_inputs(store, account, activity, day)
    if clean.get(account) == inputs:
        return {}  # nothing new since a sync that ran clean
    clean.pop(account, None)
    failures: dict[tuple[str, str], str] = {}
    # A contract's history initializes independently. Old manual opens are facts
    # to deduplicate, but do not advance the strategy-position ordering cursor.
    for user in sorted({str(row["user_id"]) for row in sources}):
        owned = {str(row["client_order_id"]): row for row in sources if row["user_id"] == user}
        symbols = {json.loads(row["request_json"])["symbol"] for row in owned.values()}
        settlements = []
        for symbol in sorted(symbols):
            entries = {identifier: row for identifier, row in owned.items()
                       if json.loads(row["request_json"])["symbol"] == symbol}
            try:
                symbol_settlements = []
                trades = sorted((t for t in activity["trades"] if t.get("symbol") == symbol),
                                key=lambda t: (int(t["tradeDateTime"]), str(t["tradeId"])))
                with store.transaction() as db:
                    latest = db.execute("""SELECT MAX(e.trade_time) FROM strategy_fill_events e
                        WHERE e.user_id = ? AND e.trading_account_id = ?
                        AND json_extract(e.payload_json, '$.trade.symbol') = ?
                        AND (json_extract(e.payload_json, '$.trade.offset') != 'OPEN' OR EXISTS (
                            SELECT 1 FROM tqkq_sim_order_links l WHERE l.user_id = e.user_id
                            AND l.trading_account_id = e.trading_account_id
                            AND l.client_order_id = e.client_order_id AND l.protection_json IS NOT NULL))""",
                        (user, account, symbol)).fetchone()[0]
                    for trade in trades:
                        order_id = str(trade["clientOrderId"])
                        event_id = f"{order_id}:{trade['tradeId']}"
                        if db.execute("""SELECT 1 FROM strategy_fill_events
                            WHERE user_id = ? AND trading_account_id = ? AND event_id = ?""",
                            (user, account, event_id)).fetchone():
                            continue
                        timestamp = int(trade["tradeDateTime"])
                        try:
                            trade_day = fill_trading_day(trade)
                        except ValueError as error:
                            raise RuntimeError("strategy_fill_trading_day_unavailable") from error
                        if trade_day > day:
                            raise RuntimeError("strategy_fill_trading_day_in_future")
                        affects_position = trade["offset"] != "OPEN" or order_id in entries
                        if affects_position and latest is not None and timestamp < latest:
                            # Do not silently assign a late historical close to a new position.
                            raise RuntimeError("strategy_fill_history_out_of_order")
                        if affects_position:
                            latest = timestamp
                        price, volume = Decimal(str(trade["price"])), int(trade["volume"])
                        if not price.is_finite() or price <= 0 or volume <= 0:
                            raise ValueError("strategy_fill_invalid")
                        pnl: dict[str, str] = {}
                        close_lots: dict[str, dict[str, int]] = {}
                        entry_strategy = None
                        unattributed: set[str] = set()
                        if trade["offset"] == "OPEN" and order_id in entries:
                            entry_strategy = _entry(db, store, entries[order_id], trade, event_id, trade_day, price, volume)
                        elif trade["offset"] in {"CLOSE", "CLOSETODAY"}:
                            pnl, close_lots, unattributed = _close(
                                db, user, account, trade, trade_day, price, volume)
                        event_payload: dict[str, Any] = {"trade": trade, "realizedGrossPnl": pnl}
                        if entry_strategy is not None:
                            # 回撤停机 (2026-10-09): the strategy this entry opened, for its fee attribution.
                            event_payload["strategyEntry"] = entry_strategy
                        if close_lots:
                            # 回撤停机 · 费用口径 (2026-10-10): each strategy's real 平今/平昨
                            # allocation from this close, for its fee estimate.
                            event_payload["strategyCloseLots"] = close_lots
                        if unattributed:
                            # A manual close consumed these strategies' rows without an attributable
                            # result: their net value is unknowable, and the drawdown must say so.
                            event_payload["unattributedStrategyIds"] = sorted(unattributed)
                        db.execute("""INSERT INTO strategy_fill_events
                            (user_id, event_id, trade_time, trading_day, client_order_id, payload_json, trading_account_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (user, event_id, timestamp, trade_day, order_id, json.dumps(event_payload), account))
                    terminal = {str(o["clientOrderId"]): o for o in activity["orders"] if o.get("status") == "FINISHED"}
                    pending = db.execute("""SELECT client_order_id, MAX(trade_time) AS time FROM strategy_fill_events
                        WHERE user_id = ? AND trading_account_id = ? AND settled = 0
                        AND json_extract(payload_json, '$.trade.symbol') = ?
                        GROUP BY client_order_id ORDER BY time, client_order_id""", (user, account, symbol)).fetchall()
                    for order in pending:
                        if order["client_order_id"] in terminal:
                            remote = terminal[order["client_order_id"]]
                            expected = int(remote["volume"]) - int(remote["volumeLeft"])
                            fills = db.execute("""SELECT payload_json FROM strategy_fill_events
                                WHERE user_id = ? AND trading_account_id = ? AND client_order_id = ?""",
                                (user, account, order["client_order_id"])).fetchall()
                            if sum(int(json.loads(fill[0])["trade"]["volume"]) for fill in fills) != expected:
                                continue
                            symbol_settlements.append((order["time"], order["client_order_id"]))
                    _roll_positions(db, user, account, symbol, day, store)
                    db.execute("""UPDATE ai_stop_orders SET last_error = NULL
                        WHERE user_id = ? AND trading_account_id = ? AND symbol = ?
                        AND last_error LIKE 'strategy_protection_sync:%'""",
                        (user, account, symbol))
                settlements.extend(symbol_settlements)
            except (ValueError, KeyError, TypeError, ArithmeticError, RuntimeError, sqlite3.Error) as error:
                # A database failure (constraint, corruption) rolls the contract's
                # transaction back above and must land in the protection's own
                # lastError like every other sync failure — not escape the loop
                # and leave the other contracts unsynced.
                code = ("strategy_fill_database_error" if isinstance(error, sqlite3.Error)
                        else str(error) if str(error).startswith("strategy_") else "strategy_fill_invalid")
                failures[user, symbol] = code
                # Roll back only this contract; keep other contracts protecting.
                with store.transaction() as db:
                    db.execute("""UPDATE ai_stop_orders SET last_error = ?
                        WHERE user_id = ? AND trading_account_id = ? AND symbol = ? AND status = 'ARMED'""",
                        ("strategy_protection_sync:" + code[:120], user, account, symbol))
        # Keep completion observations ordered for the audit trail. Risk counting
        # uses the fill's business time, not this append-only confirmation order.
        with store.transaction() as db:
            for _, order_id in sorted(settlements):
                sequence = db.execute("""SELECT COALESCE(MAX(settled), 0) + 1 FROM strategy_fill_events
                    WHERE user_id = ? AND trading_account_id = ?""", (user, account)).fetchone()[0]
                db.execute("""UPDATE strategy_fill_events SET settled = ?, settled_day = ?
                    WHERE user_id = ? AND trading_account_id = ? AND client_order_id = ?""",
                    (sequence, day, user, account, order_id))
    if not failures:
        # The inputs as read before this run: its own writes change them, so the next tick runs once more and
        # then rests; a change that landed while it ran is never mistaken for one it saw.
        clean[account] = inputs
    if user_id is not None and symbol_filter is not None:
        error = next((value for (owner, symbol), value in failures.items()
                      if owner == user_id and symbol.casefold() == symbol_filter.casefold()), None)
        if error:
            raise RuntimeError(error)
    return failures


def _entry(db, store, entry, trade, event_id, day, price, volume):
    """Arm this fill's protection; returns the strategyId whose entry it was (None = manual)."""
    config = json.loads(entry["protection_json"])
    request = json.loads(entry["request_json"])
    if trade["direction"] != request["direction"] or trade["symbol"] != request["symbol"]:
        raise ValueError("strategy_fill_identity_mismatch")
    consumed = db.execute("""SELECT payload_json FROM strategy_fill_events
        WHERE user_id = ? AND trading_account_id = ? AND client_order_id = ?""",
        (entry["user_id"], entry["trading_account_id"], entry["client_order_id"])).fetchall()
    if volume + sum(int(json.loads(r[0])["trade"]["volume"]) for r in consumed) > request["volume"]:
        raise ValueError("strategy_fill_volume_exceeded")
    tick = Decimal(config["priceTick"])
    long = request["direction"] == "BUY"
    if not config.get("strategyId"):
        # 手动保护 (2026-10-02): an order card's own stop / take prices, fixed at card time and
        # armed on the fill exactly as given — either may be absent (take-only stores '' in
        # trigger_price; stop-only leaves take_profit_price NULL).
        stop = None if config.get("fixedStopPrice") is None else Decimal(str(config["fixedStopPrice"]))
        take = None if config.get("fixedTakeProfitPrice") is None else Decimal(str(config["fixedTakeProfitPrice"]))
    else:
        stop = price - tick * config["stopLossTicks"] if long else price + tick * config["stopLossTicks"]
        if config.get("stopDistance") is not None:
            # ATR 止损: the signal bar's ATR × multiple, re-anchored on this fill's own price.
            distance = Decimal(config["stopDistance"])
            stop = price - distance if long else price + distance
        if config.get("fixedStopPrice") is not None:
            stop = Decimal(config["fixedStopPrice"])
        take = None if config["takeProfitTicks"] is None else (price + tick * config["takeProfitTicks"] if long else price - tick * config["takeProfitTicks"])
    if stop is not None:
        stop = floor_to_tick(stop, tick) if long else ceil_to_tick(stop, tick)
    if take is not None:
        take = ceil_to_tick(take, tick) if long else floor_to_tick(take, tick)
    if (stop is not None and stop <= 0) or (take is not None and take <= 0):
        raise ValueError("strategy_protection_price_invalid")
    # Every row of one position carries the first row's identity (positionKey / positionEntryTime):
    # an 加仓 fill and a later partial fill of the first order both copy it from the row already
    # protecting, so the key the engine's exit keys hang on survives FIFO closes of earlier rows.
    stop_id = str(uuid5(NAMESPACE_URL, f"ai-trader-protection:{entry['user_id']}:{event_id}"))
    peers = db.execute("""SELECT * FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?
        AND symbol = ? AND position_direction = ? AND remaining_volume > 0
        AND json_extract(strategy_metadata_json, '$.strategyId') = ?
        ORDER BY created_at DESC, id DESC LIMIT 1""",
        (entry["user_id"], entry["trading_account_id"], request["symbol"], "long" if long else "short",
         config.get("strategyId"))).fetchall() if config.get("strategyId") else []
    peer_meta = json.loads(peers[0]["strategy_metadata_json"]) if peers else {}
    if peers and int(config.get("addIndex") or 0) >= 1:
        # 加仓 fill: this row joins the position's other protection rows. moveStop keeps the whole
        # position's stop the tighter of the current one and the one this fill's own rule gives
        # (the engine's open_position); without it the position's levels are simply unchanged.
        held_stop = Decimal(peers[0]["trigger_price"])
        held_take = Decimal(peers[0]["take_profit_price"]) if peers[0]["take_profit_price"] else None
        if config.get("moveStop"):
            stop = max(held_stop, stop) if long else min(held_stop, stop)
        else:
            stop, take = held_stop, held_take
    position_key = str(peer_meta.get("positionKey") or (peers[0]["id"] if peers else stop_id))
    held_time = peer_meta.get("positionEntryTime") or peer_meta.get("entryTime")
    if held_time is None and peers:
        held_time = int(datetime.fromisoformat(str(peers[0]["created_at"])).timestamp() * 1_000_000_000)
    if config.get("breakevenAtR") and stop is not None:
        # 浮盈 1R 后移到成本 (2026-10-25): 1R is this fill's own distance to its stop.
        config = {**config, "breakeven": breakeven_at_r(price, stop, tick)}
    metadata = {**config, "entryPrice": str(price), "entryVolume": volume,
                "entryClientOrderId": entry["client_order_id"], "entryTradingDay": day,
                # 最长持仓 counts bars from the fill; 追踪止损 starts its extreme at the fill price.
                "entryTime": int(trade["tradeDateTime"]), "trailExtreme": str(price),
                "positionKey": position_key, "positionEntryTime": int(held_time or trade["tradeDateTime"]),
                # The initial stop's distance from its own fill: a structural 加仓 re-anchors it.
                "initialStopDistance": str(abs(price - stop)) if stop is not None else None}
    fence = db.execute("SELECT status FROM ai_execution_authorizations WHERE id = ?", (config["authorizationId"],)).fetchone()
    # The authorization read follows the entry's own account: another account's
    # newer stop row must not emergency-cancel this account's protective stop.
    current = db.execute("""SELECT status FROM ai_execution_authorizations
        WHERE user_id = ? AND trading_account_id = ? ORDER BY rowid DESC LIMIT 1""",
        (entry["user_id"], entry["trading_account_id"])).fetchone()
    emergency = any(r and r["status"] == "EMERGENCY_STOP" for r in (fence, current))
    bucket = "today" if request["symbol"].split(".")[0] in {"SHFE", "INE"} else "all"
    now = utc_now()
    db.execute("""INSERT INTO ai_stop_orders
        (id, user_id, authorization_id, session_id, symbol, position_direction, position_bucket,
         volume, trigger_price, take_profit_price, price_intent, status, last_error,
         strategy_metadata_json, remaining_volume, created_at, updated_at, trading_account_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'aggressive', ?, ?, ?, ?, ?, ?, ?)""",
        (stop_id, entry["user_id"], config["authorizationId"], config["sessionId"], request["symbol"],
         "long" if long else "short", bucket, volume, str(stop) if stop is not None else "",
         str(take) if take is not None else None,
         "CANCELLED" if emergency else "ARMED", "emergency_stop" if emergency else None,
         json.dumps(metadata), volume, now, now, entry["trading_account_id"]))
    if peers and config.get("moveStop"):
        # The rest of the position follows the add: stops only tighten, the take moves to this fill's.
        moved = db.execute("""SELECT id, trigger_price, take_profit_price FROM ai_stop_orders
            WHERE user_id = ? AND trading_account_id = ? AND symbol = ?
            AND position_direction = ? AND remaining_volume > 0
            AND id != ? AND json_extract(strategy_metadata_json, '$.strategyId') = ?""",
            (entry["user_id"], entry["trading_account_id"], request["symbol"],
             "long" if long else "short", stop_id, config["strategyId"])).fetchall()
        for row in moved:
            held = Decimal(row["trigger_price"])
            tighter = max(held, stop) if long else min(held, stop)
            new_take = take if take is not None else (Decimal(row["take_profit_price"]) if row["take_profit_price"] else None)
            db.execute("UPDATE ai_stop_orders SET trigger_price = ?, take_profit_price = ?, changed_by = 'strategy', updated_at = ? WHERE id = ?",
                       (str(tighter), str(new_take) if new_take is not None else None, utc_now(), row["id"]))
        store.audit(entry["proposal_id"], "strategy-protection-pyramid", {
            "stopId": stop_id, "addIndex": int(config.get("addIndex") or 0), "stopPrice": str(stop),
            "takeProfitPrice": str(take) if take is not None else None, "tightenedRows": len(moved),
        }, connection=db)
    return config.get("strategyId")


def _close(db, user, account, trade, day, price, volume):
    """Spend the protection rows this close reaches (its own strategy's first) and return
    (per-strategy realized gross PnL, per-strategy 平今/平昨 lot allocation for fee
    estimation, the strategyIds it consumed without an attributable result — a manual close
    the net value must not silently read as zero)."""
    direction = "long" if trade["direction"] == "SELL" else "short"
    rows = db.execute("""SELECT * FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?
        AND symbol = ?
        AND position_direction = ? AND strategy_metadata_json IS NOT NULL AND remaining_volume > 0
        ORDER BY (close_client_order_id = ?) DESC, created_at, id""",
        (user, account, trade["symbol"], direction, trade["clientOrderId"])).fetchall()
    strategy = db.execute("""SELECT a.strategy_id FROM strategy_execution_legs l
        JOIN strategy_execution_attempts a ON a.id = l.attempt_id
        WHERE a.user_id = ? AND a.trading_account_id = ? AND l.client_order_id = ?""",
        (user, account, trade["clientOrderId"])).fetchone()
    if strategy is None:
        # 出卡确认: a confirmed strategy card's close order is not an execution leg — the attempt
        # row carries it — so the fill still reads as this strategy's close and spends its own
        # protection rows first, never the oldest (possibly manual) row on the contract.
        strategy = db.execute("""SELECT strategy_id FROM strategy_execution_attempts
            WHERE user_id = ? AND trading_account_id = ?
            AND (',' || client_order_id || ',') LIKE ('%,' || ? || ',%')
            LIMIT 1""", (user, account, trade["clientOrderId"])).fetchone()
    rows = sorted(rows, key=lambda row: (
        row["close_client_order_id"] != trade["clientOrderId"],
        not (strategy and json.loads(row["strategy_metadata_json"]).get("strategyId") == strategy[0]),
        row["created_at"], row["id"],
    ))
    pnl: dict[str, Decimal] = {}
    close_lots: dict[str, dict[str, int]] = {}
    unattributed: set[str] = set()
    for row in rows:
        metadata = json.loads(row["strategy_metadata_json"])
        if (strategy is not None
                and row["close_client_order_id"] != trade["clientOrderId"]
                and metadata.get("strategyId") != strategy[0]):
            # 归属明确 (#654: 停止并平仓 / 出卡确认): this close spends only its own strategy's
            # rows — the account's other lots (manual or another strategy's) keep their
            # protection untouched, even when the strategy's own rows fall short.
            continue
        bucket = "today" if metadata.get("entryTradingDay") == day else "historical"
        if trade["symbol"].split(".")[0] in {"SHFE", "INE"}:
            if (trade["offset"] == "CLOSETODAY") != (bucket == "today"):
                continue
        used = min(volume, int(row["remaining_volume"]))
        if used <= 0:
            break
        remaining = int(row["remaining_volume"]) - used
        db.execute("""UPDATE ai_stop_orders SET remaining_volume = ?,
            volume = CASE WHEN ? > 0 THEN ? ELSE volume END,
            status = CASE WHEN ? = 0 AND status = 'ARMED' THEN 'CANCELLED' ELSE status END,
            updated_at = ? WHERE id = ?""", (remaining, remaining, remaining, remaining, utc_now(), row["id"]))
        # Manual/unattributed closes reduce protection conservatively, but never
        # fabricate a strategy's realized PnL from account-level equity changes.
        strategy_id = metadata.get("strategyId")
        if strategy_id is not None:
            attributable = (row["close_client_order_id"] == trade["clientOrderId"]
                            or (strategy is not None and strategy[0] == strategy_id))
            if not attributable:
                unattributed.add(str(strategy_id))
            width = int(metadata["timeframeSeconds"]) * 1_000_000_000
            exit_bar = int(trade["tradeDateTime"]) // width * width
            state = db.execute("SELECT last_executed_bar FROM strategy_monitor_states WHERE strategy_id = ? AND user_id = ?",
                               (strategy_id, user)).fetchone()
            if state is not None:
                previous = str(state[0] or "0")
                if not previous.isdigit() or exit_bar > int(previous):
                    db.execute("UPDATE strategy_monitor_states SET last_executed_bar = ? WHERE strategy_id = ? AND user_id = ?",
                               (str(exit_bar), strategy_id, user))
            if attributable:
                gross = (price - Decimal(metadata["entryPrice"])) * used * Decimal(metadata["contractMultiplier"])
                pnl[strategy_id] = pnl.get(strategy_id, Decimal(0)) + (gross if direction == "long" else -gross)
                # 费用口径 (2026-10-10 审查)：CLOSE 不等于所有交易所的平昨——只有 SHFE/INE 用
                # CLOSETODAY 区分，其他交易所按开仓交易日与成交交易日区分。这笔平仓实际花掉的
                # 今/昨桶手数记在成交事件里，回撤净值用它估费，不再把整笔 CLOSE 当平昨。
                lots = close_lots.setdefault(str(strategy_id), {"closeToday": 0, "closeYesterday": 0})
                lots["closeToday" if bucket == "today" else "closeYesterday"] += used
        volume -= used
    return ({key: str(value) for key, value in pnl.items()}, close_lots, unattributed)


def _roll_positions(db, user, account, symbol, day, store):
    rows = db.execute("""SELECT * FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?
        AND symbol = ? AND strategy_metadata_json IS NOT NULL AND remaining_volume > 0""",
        (user, account, symbol)).fetchall()
    for row in rows:
        metadata = json.loads(row["strategy_metadata_json"])
        bucket = fill_protection_bucket(row, metadata, day=day)
        linked = db.execute("SELECT * FROM tqkq_sim_order_links WHERE client_order_id = ?", (row["close_client_order_id"],)).fetchone()
        resume = row["status"] == "PARTIAL" and linked and linked["last_remote_status"] == "FINISHED"
        if resume:
            report = json.loads(linked["last_report_json"] or "{}").get("order", {})
            request = json.loads(linked["request_json"])
            filled = int(request["volume"]) - int(report.get("volumeLeft", request["volume"]))
            events = db.execute("""SELECT payload_json FROM strategy_fill_events
                WHERE user_id = ? AND trading_account_id = ? AND client_order_id = ?""",
                (user, account, linked["client_order_id"])).fetchall()
            observed = sum(int(json.loads(event[0])["trade"]["volume"]) for event in events)
            resume = filled > 0 and observed == filled
        replan = (row["status"] == "ARMED" and linked and linked["submit_phase"] == "PREPARED"
                  and (bucket != row["position_bucket"] or json.loads(linked["request_json"])["volume"] != row["remaining_volume"]))
        if (resume or replan) and not metadata.get("disarmed"):
            if replan:
                db.execute("UPDATE tqkq_sim_order_links SET submit_phase = 'ABANDONED', updated_at = ? WHERE id = ?",
                           (utc_now(), linked["id"]))
            store.audit(linked["proposal_id"], "strategy-protection-continue", {
                "stopId": row["id"], "previousClientOrderId": row["close_client_order_id"],
                "remainingVolume": row["remaining_volume"], "reason": "position_changed" if replan else "partial_fill",
            }, connection=db)
            fence = db.execute("SELECT status FROM ai_execution_authorizations WHERE id = ?", (row["authorization_id"],)).fetchone()
            db.execute("""UPDATE ai_stop_orders SET close_proposal_id = NULL, close_client_order_id = NULL,
                status = ?, updated_at = ? WHERE id = ?""",
                ("CANCELLED" if fence and fence[0] == "EMERGENCY_STOP" else "ARMED", utc_now(), row["id"]))
        db.execute("UPDATE ai_stop_orders SET position_bucket = ? WHERE id = ?", (bucket, row["id"]))


def live_position_state(store: RuntimeStore, provider: Any, *, strategy: dict[str, Any], symbol: str,
                        account: dict[str, Any]) -> Any:
    """The strategy engine's PositionState from live facts: the strategy's own armed protection
    (entry price / time, trail extreme, stop, take, frozen ATRs), today's opens per direction, the
    time exits already acted on, the 复位 latches and — for 最长持仓 / 分段止盈 / 分批止盈 — the
    completed execution bars since the entry fill. The direction and volume are filled from the
    account's positions by the caller. A plan with 加仓 or 分批止盈 reads the position's whole row
    set — every row carries the first row's positionKey / positionEntryTime, so the identity, the
    average price and the peak survive the FIFO closes a partial exit makes of earlier rows; any
    other plan keeps reading its single latest row exactly as before."""
    from .strategy_engine import PositionState, bars_held
    definition = strategy.get("definition") or {}
    plan = definition.get("plan") or {}
    strategy_id, user_id = str(strategy["strategyId"]), str(strategy["userId"])
    day = str(account.get("tradingDay") or "")
    counts = (store.count_strategy_opens_by_direction(strategy_id=strategy_id, user_id=user_id, trading_date=day)
              if day else {"long": 0, "short": 0})
    fields: dict[str, Any] = {"entries_long": counts["long"], "entries_short": counts["short"],
                              "fired_exits": store.strategy_exit_keys(strategy_id=strategy_id, user_id=user_id)}
    rules = plan.get("rules") or {}
    if any((rules.get(name) or {}).get("rearm") for name in ("entryLong", "entryShort")):
        # 复位: the latest dispatched open per direction is the latch, read from the attempts (restart-safe).
        latest = store.strategy_last_open_bars(strategy_id=strategy_id, user_id=user_id)
        fields.update(last_open_long=latest["long"], last_open_short=latest["short"])
    rows = store.connection.execute(
        """SELECT * FROM ai_stop_orders WHERE user_id = ? AND symbol = ? AND remaining_volume > 0
        AND json_extract(strategy_metadata_json, '$.strategyId') = ?
        ORDER BY created_at DESC, id DESC""", (user_id, symbol, strategy_id)).fetchall()
    if not rows:
        return PositionState(**fields)
    metas = [(row, json.loads(row["strategy_metadata_json"])) for row in rows]
    row, metadata = metas[0]                 # the latest fill: its protection levels stand for the position
    long = row["position_direction"] == "long"
    entry = Decimal(metadata["entryPrice"])
    entry_time = metadata.get("entryTime")
    key = str(row["id"])
    adds = initial_volume = peak_volume = 0
    last_entry_price: float | None = None
    aggregate = bool(plan.get("pyramiding") or plan.get("scaleOut"))
    if aggregate:

        def identity(item: tuple[Any, dict[str, Any]]) -> str:
            return str(item[1].get("positionKey") or item[0]["id"])

        # The position's every row — the ones a FIFO close already drained included: the first
        # row's identity keeps keying the fired tiers after it is closed, and the peak the tiers
        # size from stays the whole position's, not what is left of it.
        every = sorted(((candidate, json.loads(candidate["strategy_metadata_json"]))
                        for candidate in store.connection.execute(
            """SELECT * FROM ai_stop_orders WHERE user_id = ? AND symbol = ?
            AND json_extract(strategy_metadata_json, '$.strategyId') = ?""",
            (user_id, symbol, strategy_id)).fetchall()), key=lambda item: (item[0]["created_at"], item[0]["id"]))
        every = [item for item in every if identity(item) in {identity(held) for held in metas}]
        if any(item[1].get("positionKey") for item in every):
            key = str(every[0][1].get("positionKey") or every[0][0]["id"])
            entry_time = every[0][1].get("positionEntryTime") or every[0][1].get("entryTime")
            entry = (sum(Decimal(m["entryPrice"]) * int(m["entryVolume"]) for _, m in every)
                     / sum(int(m["entryVolume"]) for _, m in every))
            adds = max((int(m.get("addIndex") or 0) for _, m in every), default=0)
            initial_volume = sum(int(m["entryVolume"]) for _, m in every if not int(m.get("addIndex") or 0))
            peak_volume = sum(int(m["entryVolume"]) for _, m in every)
            last_entry_price = float(metadata["entryPrice"])
        else:
            # Rows written before the identity fields existed: main's own reading (the latest row)
            # and whatever counters the surviving rows can still give.
            indexed = [(r, m, int(m.get("addIndex") or 0)) for r, m in metas]
            pyramid = any(index >= 1 for _, _, index in indexed)
            first, first_meta = next(((r, m) for r, m, index in indexed if index == 0), metas[-1])
            key = str(first["id"])
            entry_time = first_meta.get("entryTime")
            if pyramid:
                entry = (sum(Decimal(m["entryPrice"]) * int(r["remaining_volume"]) for r, m in metas)
                         / sum(int(r["remaining_volume"]) for r, m in metas))
                adds = max((index for _, _, index in indexed), default=0)
                initial_volume = sum(int(m["entryVolume"]) for r, m, index in indexed if index == 0)
                peak_volume = sum(int(m["entryVolume"]) for _, m in metas)
                last_entry_price = float(metadata["entryPrice"])
    if entry_time is None:
        entry_time = int(datetime.fromisoformat(str(row["created_at"])).timestamp() * 1_000_000_000)
    extreme = float(metadata.get("trailExtreme") or float(entry))
    held = 0
    bars_pending = False
    timeframe = int(plan.get("timeframeSeconds") or definition.get("timeframeSeconds") or 0)
    needs_bars = plan.get("maxHoldBars") or plan.get("timeTakeProfit") or plan.get("scaleOut")
    if needs_bars and timeframe:
        bars = [bar for bar in (provider.market_workbench(symbol, timeframe).get("bars") or [])[:-1]
                if isinstance(bar, dict)]
        held = bars_held(bars, int(entry_time), timeframe)
        required = max([int(plan.get("maxHoldBars") or 0)]
                       + [int(tier["afterBars"]) for tier in plan.get("timeTakeProfit") or []])
        if held < required and (not bars or int(bars[0]["datetime"]) > int(entry_time)):
            # The subscribed workbench has only 255 completed bars. Read the existing cached
            # history without waiting on the monitor event loop; retry this bar when it arrives.
            try:
                reader = getattr(provider, "market_history_nowait")
                history = [bar for bar in (reader(symbol, timeframe, 10_000).get("bars") or [])[:-1]
                           if isinstance(bar, dict)]
                # The five-minute cache may trail the live window by a few bars.
                # Require overlap at the live window's first completed bar,
                # then take the cache prefix and the entire newer live tail.
                first_live = int(bars[0]["datetime"]) if bars else None
                overlaps = first_live is not None and any(int(bar["datetime"]) == first_live for bar in history)
                stitched = ([bar for bar in history if int(bar["datetime"]) < first_live] + bars
                            if overlaps else [])
                count = bars_held(stitched, int(entry_time), timeframe)
                if stitched and (int(stitched[0]["datetime"]) <= int(entry_time) or count >= required):
                    held = count
                else:
                    bars_pending = True
            except (AttributeError, RuntimeError, KeyError, ValueError):
                bars_pending = True
    return PositionState(
        **fields, entry_price=float(entry), entry_time=int(entry_time), bars_held=held, bars_pending=bars_pending,
        highest=extreme if long else float(entry), lowest=float(entry) if long else extreme,
        stop_price=float(row["trigger_price"]),
        take_profit_price=float(row["take_profit_price"]) if row["take_profit_price"] else None,
        entry_atr=float(metadata["entryAtr"]) if metadata.get("entryAtr") is not None else None,
        exit_atr=float(metadata["exitAtr"]) if metadata.get("exitAtr") is not None else None,
        trailing_active=bool(metadata.get("trailActive")), breakeven_active=bool(metadata.get("breakevenActive")),
        position_key=key, adds=adds, initial_volume=initial_volume, peak_volume=peak_volume,
        last_entry_price=last_entry_price,
        scaled_out=any(exit_key.startswith("scale_out:") and exit_key.endswith(f":{key}")
                       for exit_key in fields["fired_exits"]) if aggregate else False)


def closed_results(store: RuntimeStore, user_id: str, strategy_id: str, day: str) -> list[tuple[str, Decimal]]:
    # A terminal order can arrive after its fills, including on the next trading
    # day. Read every fill of candidate orders so their whole result belongs to
    # the last fill's persisted trading day, independent of confirmation batches.
    rows = store.connection.execute("""SELECT * FROM strategy_fill_events
        WHERE user_id = ? AND settled > 0 AND client_order_id IN (
            SELECT client_order_id FROM strategy_fill_events
            WHERE user_id = ? AND trading_day = ? AND settled > 0)
        ORDER BY trade_time, event_id""", (user_id, user_id, day)).fetchall()
    totals: dict[str, Decimal] = {}
    completed: dict[str, tuple[int, str]] = {}
    for row in rows:
        key = f"close:{row['client_order_id']}"
        completed[key] = (int(row["trade_time"]), str(row["trading_day"]))
        pnl = json.loads(row["payload_json"])["realizedGrossPnl"].get(strategy_id)
        if pnl is not None:
            totals[key] = totals.get(key, Decimal(0)) + Decimal(pnl)
    return [(key, totals[key]) for key in sorted(totals, key=lambda key: (completed[key][0], key))
            if completed[key][1] == day]
