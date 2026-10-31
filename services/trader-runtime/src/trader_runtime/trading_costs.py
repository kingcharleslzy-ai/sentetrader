"""One versioned fee schedule for estimates and isolated simulation accounting.

Connected-account balances are never rewritten by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import weakref
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .trading_clock import fill_trading_day


class FeeRate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["per_lot", "notional"] = "per_lot"
    open: float = Field(default=0, ge=0, le=100000)
    closeToday: float = Field(default=0, ge=0, le=100000)
    closeYesterday: float = Field(default=0, ge=0, le=100000)
    # 交易所标准里同一栏可以“按金额 + 每手”两部分并存（openctp 减加收后）：
    # notional 费率之外的每手加收，默认 0（旧的自定义费率 JSON 照读）。
    openPerLotExtra: float = Field(default=0, ge=0, le=100000)
    closeTodayPerLotExtra: float = Field(default=0, ge=0, le=100000)
    closeYesterdayPerLotExtra: float = Field(default=0, ge=0, le=100000)


class FeeOverride(FeeRate):
    symbol: str = Field(pattern=r"^[A-Za-z]+\.[A-Za-z]+[0-9]*$", max_length=40)


class FeeSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["account", "custom"] = "account"
    default: FeeRate = Field(default_factory=FeeRate)
    overrides: list[FeeOverride] = Field(default_factory=list, max_length=100)


class FeeSettings:
    def __init__(self, root: Path):
        self.root = root / "trading-costs"

    def path(self, user_id: str) -> Path:
        return self.root / (hashlib.sha256(user_id.encode()).hexdigest() + ".json")

    def get(self, user_id: str) -> dict:
        path = self.path(user_id)
        return FeeSchedule.model_validate_json(path.read_text()).model_dump() if path.exists() else FeeSchedule().model_dump()

    def save(self, user_id: str, value: dict) -> dict:
        result = FeeSchedule.model_validate(value).model_dump()
        keys = [item["symbol"].casefold() for item in result["overrides"]]
        if len(set(keys)) != len(keys):
            raise ValueError("同一品种或合约不能重复设置手续费")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(user_id)
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(result, ensure_ascii=False))
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return result


def rate_for(schedule: dict, symbol: str) -> dict:
    overrides = {item["symbol"].casefold(): item for item in schedule["overrides"]}
    return overrides.get(symbol.casefold(), overrides.get(re.sub(r"\d+$", "", symbol).casefold(), schedule["default"]))


def trade_fee(schedule: dict, *, symbol: str, price: float, volume: int,
              multiplier: float, offset: str, today: bool = True) -> float:
    rate = rate_for(schedule, symbol)
    key = "open" if offset == "OPEN" else "closeToday" if today or offset == "CLOSETODAY" else "closeYesterday"
    amount = Decimal(str(rate[key])) * Decimal(str(volume))
    if rate["mode"] == "notional":
        # UI expresses the rate in ten-thousandths of traded notional.
        amount *= Decimal(str(price)) * Decimal(str(multiplier)) / Decimal(10000)
    # 同栏的每手加收（交易所标准的“金额 + 每手”两并存）不乘成交额。
    amount += Decimal(str(rate.get(f"{key}PerLotExtra") or 0)) * Decimal(str(volume))
    return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def effective_fee_schedule(user_schedule: dict | None, symbol: str,
                           *, root: Path | None = None) -> dict | None:
    """该合约实际生效的费率表（所有者 2026-10-08）：用户自定义了就用自定义（整套替换），
    否则交易所标准；两边都没有返回 None，调用方退回自己的兜底口径。外盘合约（外盘二期，美元子账户）一律
    None：费率表是国内人民币口径，外盘按外盘引擎行情里的每手美元手续费。"""
    from .instruments import foreign_product_of
    if foreign_product_of(symbol) is not None:
        return None
    if user_schedule is not None and user_schedule.get("source") == "custom":
        return user_schedule
    from .exchange_fees import exchange_fee_schedule
    return exchange_fee_schedule(symbol, root=root)


def effective_fee_rate(user_schedule: dict | None, symbol: str,
                       *, root: Path | None = None) -> dict | None:
    """`effective_fee_schedule` 落到该合约的那一条费率（下单卡估算与本站模拟扣费共用）。"""
    schedule = effective_fee_schedule(user_schedule, symbol, root=root)
    return rate_for(schedule, symbol) if schedule is not None else None


def round_source(sources: dict[str, dict], open_order: str | None, close_order: str | None) -> dict:
    """Who a round trip belongs to (the 成交 list's 来源): a stop-loss close is 止损, a forced close 强平; otherwise
    the side that opened it (AI / 策略 with its name), then the closing side; an order the
    app never placed for the AI or a strategy is 手动."""
    close = sources.get(close_order or "")
    if close and close["source"] in {"stop", "liquidation"}:
        return dict(close)
    return dict(sources.get(open_order or "") or close or {"source": "manual"})


def foreign_fee_schedule(per_lot: float) -> dict:
    """外盘合约（美元子账户）的费率表：每手固定手续费，开平同价——外盘撮合引擎按行情里的每手美元手续费扣，
    回测、策略对比与它同一口径（外盘三期，2026-10-20）。"""
    rate = {"mode": "per_lot", "open": per_lot, "closeToday": per_lot, "closeYesterday": per_lot}
    return {"source": "foreign", "default": rate, "overrides": []}


def foreign_backtest_schedule(provider: Any, user_schedule: dict, symbols: Sequence[str]) -> dict:
    """外盘回测（含两腿都是外盘的价差）的费率表：每个合约各按引擎行情里自己的每手美元手续费——第一个做
    default，其余做精确 override（与国内价差同一形状）；任一合约取不到就整体不扣并点名。阻塞读取，放在线程里调。"""
    schedules = [(symbol, backtest_fee_schedule(provider, user_schedule, symbol)) for symbol in symbols]
    missing = [(symbol, item) for symbol, item in schedules if item.get("foreign")]
    if missing:  # the uncharged (zero-rate) schedule, every missing contract named
        return {**missing[0][1], "missingReference": [symbol for symbol, _ in missing]}
    first = schedules[0][1]
    return {**first, "overrides": [{**item["default"], "symbol": symbol} for symbol, item in schedules[1:]]}


def backtest_fee_schedule(provider: Any, user_schedule: dict, symbol: str, *, root: Path | None = None) -> dict:
    """规则回测与策略对比的费率表（一个入口）：外盘合约按引擎行情的每手美元手续费；国内用户自定义了用自定义，
    否则交易所标准，都没有时原样（账户默认，不扣）。阻塞读取，放在线程里调。"""
    from .instruments import foreign_product_of
    if foreign_product_of(symbol) is not None:
        try:
            per_lot = provider.market_snapshot("", symbol).get("commissionPerLot")
        except Exception:
            per_lot = None
        if isinstance(per_lot, (int, float)) and per_lot >= 0:
            return foreign_fee_schedule(float(per_lot))
        # 取不到外盘每手费用：不扣（零费率），也不拿用户为国内设的费率去扣美元合约；结果卡如实写“未扣”。
        return {**foreign_fee_schedule(0.0), "source": "account", "missingReference": [symbol], "foreign": True}
    if user_schedule.get("source") == "account":
        from .exchange_fees import exchange_fee_schedule
        return exchange_fee_schedule(symbol, root=root) or user_schedule
    return user_schedule


def foreign_review_schedules(provider: Any, symbols: Collection[str]) -> dict[str, dict]:
    """复盘、交易日志与统计里外盘合约（美元子账户）的费率：每个合约按外盘引擎行情里自己的每手美元手续费
    （`backtest_fee_schedule` 同一取法）；取不到的合约不在结果里——它的逐笔手续费与净收益照旧遮成未知，
    不拿用户为国内设的费率去扣美元合约。国内合约不在结果里。阻塞读取，放在线程里调。"""
    from .instruments import foreign_product_of
    found: dict[str, dict] = {}
    for symbol in symbols:
        if foreign_product_of(symbol) is None or provider is None:
            continue
        schedule = backtest_fee_schedule(provider, {"source": "account"}, symbol)
        if not schedule.get("missingReference"):
            found[symbol] = schedule
    return found


def mask_account_fees(result: dict, schedule: dict, charged: Collection[str] = ()) -> str:
    """The account-default fee source carries no per-lot rates (#514-4): per-trade fees/net and the
    estimates read as unknown (None) rather than charging rates left over from a custom schedule the
    user has since switched away from. Returns the P&L basis the numbers are on — "net" when the
    rates stayed, "gross" when they were hidden (the consumers fall back to gross then).

    `charged` are the foreign contracts priced at the foreign engine's own per-lot USD fee
    (`foreign_review_schedules`): their fees stay whatever the user's schedule is. A foreign contract
    outside it never keeps a fee — the user's schedule is the domestic CNY one."""
    from .instruments import foreign_product_of
    custom = schedule.get("source") == "custom"
    trades = result.get("trades") or []
    kept = 0
    for trade in trades:
        symbol = str(trade.get("symbol") or "")
        if symbol in charged or (custom and foreign_product_of(symbol) is None):
            kept += 1
            continue
        trade["fees"], trade["net"] = None, None
    if custom and kept == len(trades):
        return "net"
    if trades and kept == len(trades):
        result["estimatedCommission"] = round(sum(float(item["fees"]) for item in trades), 2)
        result["estimatedNet"] = round(sum(float(item["net"]) for item in trades), 2)
        return "net"
    result["estimatedNet"] = None
    result["estimatedCommission"] = None
    return "gross"


def review_trades(trades: list[dict], schedule: dict, multiplier: float,
                  sources: dict[str, dict] | None = None) -> dict:
    """FIFO round trips; unmatched closes stay explicit, never invent entry prices.

    Input order is the account's stable chronological trade order. Repeated reads
    recompute from unique fills, so no fee or realized profit is accumulated twice.
    `sources` (clientOrderId → {source, strategyName?}) adds each round trip's 来源.
    """
    lots: dict[tuple[str, str], list[dict]] = {}
    rounds, unmatched, seen = [], [], set()
    fees = 0.0
    for trade in sorted(trades, key=lambda item: item["tradeDateTime"]):
        identifier = trade["tradeId"]
        identity = (trade.get("clientOrderId", ""), identifier)
        if identity in seen:
            continue
        seen.add(identity)
        day = fill_trading_day(trade)
        symbol, offset = trade["symbol"], trade["offset"]
        volume, price = int(trade["volume"]), float(trade["price"])
        direction = "long" if (trade["direction"] == "BUY") == (offset == "OPEN") else "short"
        queue = lots.setdefault((symbol, direction), [])
        if offset == "OPEN":
            fee = trade_fee(schedule, symbol=symbol, price=price, volume=volume, multiplier=multiplier, offset=offset)
            fees += fee
            queue.append({**trade, "remaining": volume, "entryFeeRemaining": Decimal(str(fee)), "entryFeeTotal": Decimal(str(fee)),
                          "tradingDay": day})
            continue
        remaining = volume
        allocations = []
        for lot in queue:
            if not remaining or not lot["remaining"]:
                continue
            today = lot["tradingDay"] == day
            exchange = symbol.split(".")[0].upper()
            if (offset == "CLOSETODAY" and not today) or (exchange in {"SHFE", "INE"} and offset == "CLOSE" and today):
                continue
            used = min(remaining, lot["remaining"])
            lot["remaining"] -= used
            remaining -= used
            allocations.append((lot, used, today))
        bucket_volumes = {today: sum(used for _, used, bucket in allocations if bucket == today) for today in (True, False)}
        bucket_fees = {today: Decimal(str(trade_fee(schedule, symbol=symbol, price=price, volume=quantity,
            multiplier=multiplier, offset=offset, today=today))) for today, quantity in bucket_volumes.items()}
        fees += float(sum(bucket_fees.values()))
        for lot, used, today in allocations:
            entry_cost = lot["entryFeeRemaining"] if not lot["remaining"] else (
                lot["entryFeeTotal"] * used / int(lot["volume"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            entry_cost = min(entry_cost, lot["entryFeeRemaining"])
            lot["entryFeeRemaining"] -= entry_cost
            closing_cost = bucket_fees[today] if used == bucket_volumes[today] else (
                bucket_fees[today] * used / bucket_volumes[today]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            closing_cost = min(closing_cost, bucket_fees[today])
            bucket_fees[today] -= closing_cost
            bucket_volumes[today] -= used
            cost = entry_cost + closing_cost
            gross = (Decimal(str(price)) - Decimal(str(lot["price"]))) * Decimal(str(multiplier)) * used * (1 if direction == "long" else -1)
            rounds.append({"id": f"{lot['tradeId']}:{identifier}", "symbol": symbol, "direction": direction,
                           "volume": used, "openTime": lot["tradeDateTime"], "closeTime": trade["tradeDateTime"],
                           "openPrice": lot["price"], "closePrice": price,
                           "gross": float(round(gross, 2)), "fees": float(cost), "net": float(round(gross - cost, 2)),
                           "approximate": bool(lot.get("approximate")) or bool(trade.get("approximate")),
                           "tradingDay": day,
                           # Each leg's own fill and order (今日复盘 2026-10-06 tags the two legs apart).
                           "openTradeId": lot["tradeId"], "closeTradeId": identifier,
                           "openOrderId": str(lot.get("clientOrderId") or ""),
                           "closeOrderId": str(trade.get("clientOrderId") or "")})
            if sources is not None:
                rounds[-1].update(round_source(sources, lot.get("clientOrderId"), trade.get("clientOrderId")))
        if remaining:
            unmatched.append({"tradeId": identifier, "volume": remaining})
    return {"trades": rounds, "estimatedCommission": round(fees, 2),
            "estimatedNet": round(sum(item["net"] for item in rounds), 2), "unmatched": unmatched}


# Each provider's contract multiples, read once (see contract_multiplier).
_CONTRACT_MULTIPLIERS: weakref.WeakKeyDictionary[Any, dict[str, float]] = weakref.WeakKeyDictionary()


def contract_multiplier(provider: Any, symbol: str) -> float | None:
    """A contract's volume multiple (每手乘数). It is a constant of the contract, so the provider's quote
    is read for it once per provider and kept; the review, the account ledger and the order backfill all
    ask here (2026-10-22: each 成交复盘 / 盈亏 read spent ~0.35 s on sidecar quotes just for this). None
    when the provider does not serve the contract (expired, not loaded) — not kept, so it is asked again."""
    try:
        known = _CONTRACT_MULTIPLIERS.setdefault(provider, {})
    except TypeError:  # a provider that cannot be weakly referenced: the same answer, unkept
        known = {}
    key = symbol.upper()
    if key in known:
        return known[key]
    try:
        value = float(provider.market_snapshot("multiplier", symbol)["volumeMultiple"]) or None
    except (KeyError, TypeError, ValueError, RuntimeError):
        return None
    if value:
        known[key] = value
    return value


def persist_account_fills(store, activity: dict, multiplier_for: Callable[[str], float | None],
                          trading_account_id: str | None = None) -> list[dict]:
    """Keep the provider's current-day fills in the store (the sidecar serves only
    the current trading day) and return them. Fixture data is never persisted;
    the fill's volume multiple rides along so the review survives contract expiry."""
    today = [t for t in activity.get("trades") or [] if t.get("tradeId")]
    if activity.get("dataStatus") == "simulated":
        return today
    multipliers = {}
    for symbol in {str(t.get("symbol") or "") for t in today}:
        if symbol:
            multipliers[symbol] = multiplier_for(symbol)
    store.upsert_account_trades(
        today, {k: v for k, v in multipliers.items() if v}, trading_account_id=trading_account_id,
    )
    return today


def normalized_trading_day(value: Any) -> str | None:
    """A tradingDay normalized to its calendar date (YYYY-MM-DD); None when absent or not a
    real calendar date (成交先到、交易日晚补的缺日, malformed / 2026-99-99). An unreadable day is
    never guessed to be another trading day — the stop-close checks count it as the current
    one (#700 review)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def account_fills(store, today: list[dict], symbol: str | None,
                  trading_account_id: str | None = None, *,
                  close_direction: str | None = None,
                  offsets: Collection[str] | None = None,
                  trading_day: str | None = None) -> list[dict]:
    """Today's fills from the provider plus every earlier fill the store kept. With
    `close_direction` / `offsets` / `trading_day` (the stop-close chain's own read,
    #700 review) only that leg's own close fills come back — the filtering runs in the
    store query, not by reading the whole history first. A fill's day is judged by the
    same strict normalization on both sides: SQLite `date()` rolls impossible dates
    (2026-02-30) forward instead of rejecting them, so the SQL keeps any value it
    cannot prove is a normalized valid date, and this same predicate (`normalized_
    trading_day`) re-judges persisted rows and today's fills alike — a close provably
    dated to another trading day is out, while a missing or unreadable day stays in
    (unknown is never guessed safe)."""
    day = normalized_trading_day(trading_day) if trading_day is not None else None
    scoped = close_direction is not None and offsets is not None and trading_day is not None
    if offsets is not None and not offsets:
        return []
    persisted = store.list_account_trades(
        symbol=symbol, trading_account_id=trading_account_id,
        close_direction=close_direction if scoped else None,
        offsets=offsets if scoped else None,
        trading_day=day if scoped else None)
    if symbol:
        today = [t for t in today if str(t.get("symbol") or "").upper() == symbol.upper()]
    if scoped:
        scoped_direction = str(close_direction)
        scoped_offsets = set(offsets or ())

        def relevant(fill: dict) -> bool:
            return (str(fill.get("direction") or "") == scoped_direction
                    and str(fill.get("offset") or "") in scoped_offsets
                    and normalized_trading_day(fill.get("tradingDay")) in (None, day))

        # SQL conservatively retains ambiguous dates; apply the same final predicate
        # to persisted and provider fills so both produce the same baseline.
        persisted = [t for t in persisted if relevant(t)]
        today = [t for t in today if relevant(t)]
    seen = {t["tradeId"] for t in persisted}
    return persisted + [t for t in today if t["tradeId"] not in seen]


def review_account_trades(fills: list[dict], schedule: dict, multipliers: dict[str, float], *, days: int,
                          sources: dict[str, dict] | None = None, now: datetime | None = None,
                          schedules: dict[str, dict] | None = None) -> dict:
    """Pair every symbol over all known fills, then keep the round trips closed
    within the last `days` before `now` (wall clock by default; a replay passes
    its own clock, or a replay of a day older than the window reviews nothing).
    Pairing always sees the whole history, so an open lot older than the window
    still matches its close. `schedules` (symbol → fee schedule) prices a contract on its own schedule
    instead of `schedule` — the foreign contracts' per-lot USD fee (`foreign_review_schedules`)."""
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).timestamp() * 1_000_000_000
    rounds: list[dict] = []
    unmatched: list[dict] = []
    for symbol, multiplier in multipliers.items():
        result = review_trades([t for t in fills if t["symbol"] == symbol], (schedules or {}).get(symbol, schedule),
                               multiplier, sources)
        rounds.extend(t for t in result["trades"] if t["closeTime"] >= cutoff)
        unmatched.extend(result["unmatched"])
    rounds.sort(key=lambda item: (item["closeTime"], item["id"]))
    return {"trades": rounds, "estimatedCommission": round(sum(item["fees"] for item in rounds), 2),
            "estimatedNet": round(sum(item["net"] for item in rounds), 2), "unmatched": unmatched}
