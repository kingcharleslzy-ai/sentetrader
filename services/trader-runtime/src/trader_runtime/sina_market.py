"""新浪期货备用行情 (2026-10-07): when every TqKq public source is down, the shared
market reads quotes, the board and chart K-lines from Sina instead.

The backup is for watching only — charts, the board, AI research and chat reads.
Every packet it builds carries `dataStatus: "backup"` and `source: "新浪（备用）"`;
every order, stop, alert, strategy and verification gate accepts only `normal`
(or `simulated` in fixtures), so a backup price can never reach them.

Sina endpoints (checked 2026-10-07 from the Tencent Shanghai host and this Mac):
- quotes: `https://hq.sinajs.cn/list=nf_AU2612,nf_IF2610` with the Referer
  `https://finance.sina.com.cn`, GBK text, one `var hq_str_nf_X="…";` line per code.
  Commodity fields (SHFE/INE/DCE/CZCE/GFEX): 0 name, 1 time HHMMSS, 2 open, 3 high,
  4 low, 5 close, 6 bid1, 7 ask1, 8 last, 9 settlement, 10 pre-settlement,
  11 bid volume 1, 12 ask volume 1, 13 open interest, 14 volume, 15 exchange short
  name, 16 product name, 17 date, 27 average price, 28–35 bid 2–5 (price, volume),
  36–43 ask 2–5 (price, volume; zeros where the exchange gives no depth).
  CFFEX fields: 0 open, 1 high, 2 low, 3 last, 4 volume, 5 turnover, 6 open
  interest, 9 upper limit, 10 lower limit, 13 pre-close, 14 pre-settlement,
  16/17 bid1 price/volume, 26/27 ask1 price/volume, 36 date, 37 time, last field
  the name (AKShare `futures_zh_spot`, checked against live IF2610: the limits are
  pre-settlement ×1.1/×0.9 on the tick).
- minute K-lines: `stock2.finance.sina.com.cn/futures/api/jsonp.php/var t=/
  InnerFuturesNewService.getFewMinLine?symbol=AU2612&type=5` (type 1/5/15/30/60)
  → `[{d,o,h,l,c,v,p}]`, `d` the bar's END time, up to 1023 bars.
- daily K-lines: `…/InnerFuturesNewService.getDailyKLine?symbol=AU2612` →
  `[{d,o,h,l,c,v,p,s}]` since the contract listed.
3-minute bars aggregate the 1-minute ones; weekly bars aggregate the daily ones.
Requests take turns on the existing gentle host throttle (one at a time per host,
≥ 0.5 s apart); one quote request carries every wanted contract."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .collection import AsyncCollector, SourceFailed
from .host_throttle import GENTLE, HostThrottle
from .last_good import LastGood
from .trading_calendar import CST

log = logging.getLogger(__name__)

BACKUP_STATUS = "backup"
BACKUP_SOURCE = "新浪（备用）"
BACKUP_NOTICE = "快期行情暂不可用，这是新浪的备用行情，可能延迟几秒，只供看盘和调研，不能作为下单依据。"
QUOTE_URL = "https://hq.sinajs.cn/list="
MINUTE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
              "InnerFuturesNewService.getFewMinLine?symbol={code}&type={kind}")
DAILY_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
             "InnerFuturesNewService.getDailyKLine?symbol={code}")
HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
QUOTE_TTL = 2.0
# K-lines move slower than the price; the header quote keeps refreshing every QUOTE_TTL.
BAR_TTL = {60: 10.0, 180: 15.0, 300: 15.0, 900: 30.0, 1800: 30.0, 3600: 60.0, 86400: 120.0, 604800: 300.0}
MINUTE_KINDS = {60: "1", 300: "5", 900: "15", 1800: "30", 3600: "60"}
BAR_RETRY = 5.0  # a failed K-line read is not repeated sooner (the header quote retries every QUOTE_TTL)
IDLE_SECONDS = 600.0  # a contract nobody has read for this long leaves the store
MAX_BARS = {60: 600}  # 分时 needs a whole trading day; other periods carry what the sidecar does
DEFAULT_BARS = 256
NS = 1_000_000_000
# A quote older than this inside a scheduled session reads as closed (holidays keep the schedule).
SESSION_QUOTE_SECONDS = 30 * 60

Fetch = Callable[[str], Awaitable[bytes]]


class SinaUnavailable(RuntimeError):
    """Sina gave nothing usable for this read."""


def _decade_year(digit: int, now_year: int) -> int:
    """CZCE's one-digit year → the four-digit year in [now − 1, now + 8]."""
    return next(year for year in range(now_year - 1, now_year + 9) if year % 10 == digit)


def to_sina_code(symbol: str, now_year: int) -> str | None:
    """`SHFE.au2612` → `AU2612`; `CZCE.AP701` → `AP2701` (the decade inferred from
    `now_year`); `CFFEX.IF2610` → `IF2610`. Anything that is not a dated contract
    (主连、指数、组合) → None."""
    exchange, _, instrument = str(symbol or "").strip().partition(".")
    exchange = exchange.upper()
    letters = instrument.rstrip("0123456789")
    digits = instrument[len(letters):]
    if not letters.isalpha() or exchange not in {"SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX"}:
        return None
    if exchange == "CZCE":
        if len(digits) != 3:
            return None
        digits = str(_decade_year(int(digits[0]), now_year))[2] + digits
    elif len(digits) != 4:
        return None
    return letters.upper() + digits


def from_sina_code(code: str, exchange: str) -> str:
    """The reverse mapping, in the exchange's own spelling (lower-case products on
    SHFE/INE/DCE/GFEX, upper-case on CZCE/CFFEX, CZCE's three-digit month)."""
    letters = code.rstrip("0123456789")
    digits = code[len(letters):]
    exchange = exchange.upper()
    if exchange == "CZCE":
        return f"CZCE.{letters.upper()}{digits[-3:]}"
    if exchange == "CFFEX":
        return f"CFFEX.{letters.upper()}{digits}"
    return f"{exchange}.{letters.lower()}{digits}"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price(value: Any) -> float | None:
    """Sina writes 0 for an empty book level or a missing price."""
    number = _number(value)
    return number if number is not None and number > 0 else None


def _stamp(day: str, clock: str, now: datetime) -> datetime | None:
    clock = clock.replace(":", "")
    if len(clock) != 6 or not clock.isdigit():
        return None
    try:
        moment = datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H%M%S").replace(tzinfo=CST)
    except ValueError:
        return None
    if moment > now + timedelta(minutes=5):
        # A night quote may carry its trading day's date: put it back on the calendar.
        local_now = now.astimezone(CST)
        moment = moment.replace(year=local_now.year, month=local_now.month, day=local_now.day)
        if moment > now + timedelta(minutes=5):
            moment -= timedelta(days=1)
    return moment


def parse_quote_fields(code: str, fields: list[str], *, cffex: bool, now: datetime) -> dict[str, Any] | None:
    """One `hq_str_nf_X` line's fields → the quote's numbers, or None when Sina has
    nothing for the code (an unknown or expired contract answers an empty string)."""
    if not fields or fields == [""]:
        return None
    if cffex:
        if len(fields) < 38:
            return None
        quoted = _stamp(fields[36], fields[37], now)
        result: dict[str, Any] = {
            "name": fields[-1].strip() or code, "open": _price(fields[0]), "highest": _price(fields[1]),
            "lowest": _price(fields[2]), "lastPrice": _price(fields[3]), "volume": _number(fields[4]),
            "openInterest": _number(fields[6]), "upperLimit": _price(fields[9]), "lowerLimit": _price(fields[10]),
            "preSettlement": _price(fields[14]),
            "bidPrice1": _price(fields[16]), "bidVolume1": _number(fields[17]),
            "askPrice1": _price(fields[26]), "askVolume1": _number(fields[27]),
        }
    else:
        if len(fields) < 18:
            return None
        quoted = _stamp(fields[17], fields[1], now)
        result = {
            "name": fields[0].strip() or code, "open": _price(fields[2]), "highest": _price(fields[3]),
            "lowest": _price(fields[4]), "bidPrice1": _price(fields[6]), "askPrice1": _price(fields[7]),
            "lastPrice": _price(fields[8]), "preSettlement": _price(fields[10]),
            "bidVolume1": _number(fields[11]), "askVolume1": _number(fields[12]),
            "openInterest": _number(fields[13]), "volume": _number(fields[14]),
            "upperLimit": None, "lowerLimit": None,
        }
        for level in range(2, 6):
            bid = 28 + (level - 2) * 2
            ask = 36 + (level - 2) * 2
            if len(fields) > ask + 1 and _price(fields[bid]) and _price(fields[ask]):
                result.update({f"bidPrice{level}": _price(fields[bid]), f"bidVolume{level}": _number(fields[bid + 1]),
                               f"askPrice{level}": _price(fields[ask]), f"askVolume{level}": _number(fields[ask + 1])})
    if result["lastPrice"] is None or quoted is None:
        return None
    result["quoteDatetime"] = quoted.isoformat()
    return result


def parse_quotes(text: str, *, cffex_codes: Iterable[str] = (), now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """The whole `hq.sinajs.cn` answer → {Sina code: parsed quote}; codes Sina knows
    nothing about are simply absent."""
    now = now or datetime.now(UTC)
    cffex = set(cffex_codes)
    result: dict[str, dict[str, Any]] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("var hq_str_nf_") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        code = name.removeprefix("var hq_str_nf_").strip()
        fields = value.strip().strip('"').split(",")
        parsed = parse_quote_fields(code, fields, cffex=code in cffex, now=now)
        if parsed is not None:
            result[code] = parsed
    return result


def _jsonp_rows(text: str) -> list[dict[str, Any]]:
    start, end = text.find("(["), text.rfind("])")
    if start < 0 or end <= start:
        return []
    try:
        rows = json.loads(text[start + 1:end + 1])
    except ValueError:
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _bar(start: datetime, row: dict[str, Any]) -> dict[str, Any] | None:
    values = [_number(row.get(key)) for key in ("o", "h", "l", "c")]
    if any(value is None or value <= 0 for value in values):
        return None
    return {"datetime": int(start.timestamp()) * NS, "open": values[0], "high": values[1], "low": values[2],
            "close": values[3], "volume": _number(row.get("v")) or 0, "openInterest": _number(row.get("p"))}


def parse_minute_bars(text: str, minutes: int) -> list[dict[str, Any]]:
    """Sina labels a minute bar by its END time; TqSdk (and the chart) by its start."""
    bars: dict[int, dict[str, Any]] = {}
    for row in _jsonp_rows(text):
        try:
            end = datetime.strptime(str(row.get("d")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        except ValueError:
            continue
        bar = _bar(end - timedelta(minutes=minutes), row)
        if bar is not None:
            bars[bar["datetime"]] = bar
    return [bars[key] for key in sorted(bars)]


def parse_daily_bars(text: str) -> list[dict[str, Any]]:
    """A daily bar sits at 00:00 CST of its trading day, as TqSdk stamps it."""
    bars: dict[int, dict[str, Any]] = {}
    for row in _jsonp_rows(text):
        try:
            day = datetime.strptime(str(row.get("d")), "%Y-%m-%d").replace(tzinfo=CST)
        except ValueError:
            continue
        bar = _bar(day, row)
        if bar is not None:
            bar["tradingDay"] = day.date().isoformat()
            bars[bar["datetime"]] = bar
    return [bars[key] for key in sorted(bars)]


def aggregate_bars(bars: list[dict[str, Any]], bucket: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
    """Consecutive bars sharing a bucket → one bar (first open, extreme high/low, last
    close and open interest, summed volume), stamped at the first bar's start."""
    result: list[dict[str, Any]] = []
    current_key: Any = None
    for bar in bars:
        key = bucket(bar)
        if result and key == current_key:
            merged = result[-1]
            merged.update(high=max(merged["high"], bar["high"]), low=min(merged["low"], bar["low"]),
                          close=bar["close"], volume=merged["volume"] + bar["volume"],
                          openInterest=bar["openInterest"])
        else:
            result.append({name: value for name, value in bar.items() if name != "tradingDay"})
            current_key = key
    return result


def three_minute_bucket(bar: dict[str, Any]) -> int:
    return int(bar["datetime"]) // (180 * NS)


def week_bucket(bar: dict[str, Any]) -> tuple[int, int]:
    year, week, _ = datetime.fromtimestamp(bar["datetime"] / NS, tz=CST).date().isocalendar()
    return year, week


async def http_fetch(url: str) -> bytes:
    import httpx  # the runtime's existing HTTP client

    async with httpx.AsyncClient(timeout=8.0, headers=HEADERS) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


class SinaBackup:
    """Batched, cached Sina reads on the unified collector (#976 step 7): one quote request serves every
    contract due in the same moment (``AsyncCollector.get_many``), K-lines are kept per contract and period
    (``AsyncCollector.get``), in memory only. A failed refresh never serves an old Sina price: the read
    answers ``SinaUnavailable`` (the shared market then keeps its own answer) and the request is not
    repeated before the retry interval. `fetch`, the throttle and the clocks are injectable so tests never
    touch the network or sleep."""

    def __init__(self, fetch: Fetch = http_fetch, *, throttle: HostThrottle = GENTLE,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.fetch, self.throttle, self.clock, self.now = fetch, throttle, clock, now
        self.kept = LastGood(clock=clock)
        self._collect = AsyncCollector(self.kept, "sina-backup")
        self._used: dict[str, float] = {}  # key → when a reader last asked for it

    async def _get(self, url: str) -> bytes:
        host = url.split("://", 1)[-1].split("/", 1)[0]
        async with self.throttle.async_turn(host):
            return await self.fetch(url)

    def _touch(self, keys: Iterable[str]) -> None:
        """Contracts nobody has asked about for IDLE_SECONDS drop out of the store."""
        now = self.clock()
        for key in keys:
            self._used[key] = now
        for old in [key for key, at in self._used.items() if at + IDLE_SECONDS < now]:
            self._used.pop(old, None)
            self.kept.forget(old)

    async def quotes(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        """{symbol: parsed quote} for the contracts Sina knows; the rest are absent."""
        wanted = sorted({str(s).strip() for s in symbols if str(s).strip()})
        year = self.now().astimezone(CST).year
        keyed = {f"quote:{symbol}": (symbol, code) for symbol in wanted
                 if (code := to_sina_code(symbol, year)) is not None}

        async def fetch(keys: list[str]) -> dict[str, Any]:
            by_code = {keyed[key][1]: key for key in keys}
            cffex = {code for code, key in by_code.items() if keyed[key][0].upper().startswith("CFFEX.")}
            try:
                raw = await self._get(QUOTE_URL + ",".join("nf_" + code for code in by_code))
                parsed = parse_quotes(raw.decode("gbk", errors="replace"), cffex_codes=cffex, now=self.now())
            except Exception as error:  # noqa: BLE001 — any transport or parse failure is “Sina unavailable”
                raise SourceFailed("sina_quote_unavailable") from error
            return {key: parsed.get(code) for code, key in by_code.items()}  # None: Sina has no such contract

        self._touch(keyed)  # before waiting: a reader cancelled mid-request still leaves its keys evictable
        kept = await self._collect.get_many(list(keyed), fetch, ttl=QUOTE_TTL, retry=QUOTE_TTL, timeout=None,
                                            timeout_error="sina_quote_unavailable")
        if any(entry is None or entry.stale for entry in kept.values()):
            raise SinaUnavailable("sina_quote_unavailable")
        return {symbol: dict(entry.value) for key, (symbol, _) in keyed.items()
                if (entry := kept[key]) is not None and entry.value is not None}

    async def bars(self, symbol: str, duration: int) -> list[dict[str, Any]]:
        code = to_sina_code(symbol, self.now().astimezone(CST).year)
        if code is None or duration not in BAR_TTL:
            raise SinaUnavailable("sina_symbol_unsupported")

        async def fetch() -> list[dict[str, Any]]:
            try:
                if duration in (86400, 604800):
                    bars = parse_daily_bars((await self._get(DAILY_URL.format(code=code))).decode("utf-8", "replace"))
                    if duration == 604800:
                        bars = aggregate_bars(bars, week_bucket)
                else:
                    kind = MINUTE_KINDS.get(duration, "1")
                    text = (await self._get(MINUTE_URL.format(code=code, kind=kind))).decode("utf-8", "replace")
                    bars = parse_minute_bars(text, int(kind))
                    if duration == 180:
                        bars = aggregate_bars(bars, three_minute_bucket)
            except Exception as error:  # noqa: BLE001
                raise SourceFailed("sina_bars_unavailable") from error
            return bars[-MAX_BARS.get(duration, DEFAULT_BARS):]

        key = f"bars:{symbol}:{duration}"
        self._touch([key])
        try:
            kept = await self._collect.get(key, fetch, ttl=BAR_TTL[duration], retry=BAR_RETRY, timeout=None,
                                           timeout_error="sina_bars_unavailable", unavailable="sina_bars_unavailable")
        except SourceFailed as failure:
            raise SinaUnavailable("sina_bars_unavailable") from failure
        if kept.stale:
            raise SinaUnavailable("sina_bars_unavailable")
        return [dict(bar) for bar in kept.value]

    async def close(self) -> None:
        await self._collect.close()


def session_status(schedule: Any, quote_datetime: str | None, now: datetime) -> str:
    """trading only inside a scheduled session AND with a recent quote (a holiday keeps
    the weekday schedule but brings no trades); closed otherwise; unknown without a schedule."""
    from .chat_suggestions import session_phase  # lazy: chat_suggestions pulls in the proposal stack

    phase = session_phase(schedule, now.astimezone(CST))
    if phase == "未知":
        return "unknown"
    if phase not in {"盘中", "夜盘"}:
        return "closed"
    try:
        quoted = datetime.fromisoformat(str(quote_datetime))
    except (TypeError, ValueError):
        return "closed"
    return "trading" if (now - quoted).total_seconds() <= SESSION_QUOTE_SECONDS else "closed"


def quote_payload(symbol: str, raw: dict[str, Any], item: dict[str, Any], now: datetime) -> dict[str, Any]:
    """A parsed Sina quote + the contract's catalog entry → the shared market's quote
    shape (the fields the sidecar fills), marked backup."""
    last, pre = raw["lastPrice"], raw.get("preSettlement")
    change = round((last - pre) / pre * 100, 2) if pre else 0.0
    age = max(0.0, (now - datetime.fromisoformat(raw["quoteDatetime"])).total_seconds())
    schedule = item.get("tradingSchedule")
    return {
        **{key: value for key, value in raw.items() if key != "name"},
        "symbol": symbol, "name": item.get("instrumentName") or raw.get("name") or symbol,
        "price": last, "changePercent": change, "trend": "up" if change > 0 else "down" if change < 0 else "flat",
        "priceTick": item.get("priceTick"), "volumeMultiple": item.get("volumeMultiple"),
        "capturedAt": now.isoformat(), "ageSeconds": round(age, 3),
        "marketSessionStatus": session_status(schedule, raw["quoteDatetime"], now),
        "tradingSchedule": schedule, "dataStatus": BACKUP_STATUS, "source": BACKUP_SOURCE,
    }


_INCONSISTENT: set[str] = set()  # symbols whose backup row is withheld (logged once per episode)


def board_row(symbol: str, raw: dict[str, Any] | None, item: dict[str, Any]) -> dict[str, Any]:
    """One 报价总表 / extras row from a Sina quote (the sidecar's row shape)."""
    name = str(item.get("instrumentName") or symbol)
    exchange, _, instrument = symbol.partition(".")
    row: dict[str, Any] = {
        "symbol": symbol, "exchangeId": str(item.get("exchangeId") or exchange),
        "productId": str(item.get("productId") or ""), "instrumentId": str(item.get("instrumentId") or instrument),
        "instrumentName": name, "productName": name.rstrip("0123456789") or name,
        "last": None, "preSettlement": None, "change": None, "changePercent": None, "volume": None,
        "openInterest": None, "openInterestChange": None, "high": None, "low": None,
        "upperLimit": None, "lowerLimit": None, "bidPrice1": None, "askPrice1": None, "quoteTime": None,
        "status": "market_quote_unavailable",
    }
    if raw is None:
        return row
    # 最新价须落在当日最高/最低之间 (#806): otherwise the row stays unavailable — never another price — and
    # the first sighting is logged (the sidecar's board keeps the same guard). An inverted range (最高 below
    # 最低) is an impossible quote too (#823).
    last, high, low = raw["lastPrice"], raw.get("highest"), raw.get("lowest")
    if last is not None and high is not None and low is not None and (high < low or not (
            low - 1e-6 * max(1.0, abs(last)) <= last <= high + 1e-6 * max(1.0, abs(last)))):
        if symbol not in _INCONSISTENT:
            _INCONSISTENT.add(symbol)
            log.warning("sina_board_quote_inconsistent symbol=%s last=%s high=%s low=%s", symbol, last, high, low)
        return row
    _INCONSISTENT.discard(symbol)
    pre = raw.get("preSettlement")
    row.update(last=raw["lastPrice"], preSettlement=pre, volume=raw.get("volume"), openInterest=raw.get("openInterest"),
               high=raw.get("highest"), low=raw.get("lowest"), upperLimit=raw.get("upperLimit"),
               lowerLimit=raw.get("lowerLimit"), bidPrice1=raw.get("bidPrice1"), askPrice1=raw.get("askPrice1"),
               quoteTime=raw["quoteDatetime"], status="normal")
    if pre:
        row["change"] = round(raw["lastPrice"] - pre, 2)
        row["changePercent"] = round(row["change"] / pre * 100, 2)
    return row


def revision(*parts: Any) -> str:
    digest = hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return "sina:" + digest
