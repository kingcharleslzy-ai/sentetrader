from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

ENGINE_SPEC = {
    "implementationRevision": 2,
    "engine": "tqsdk-3.10.2-tqsim-tqbacktest",
    "signal": "close>ma5>ma20-and-volume-ratio",
    "position": "target-volume-0-or-1",
    "fill": "TargetPosTask-ACTIVE",
    "variants": "volume-threshold-baseline-vs-candidate",
}
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


def _engine_digest() -> str:
    return hashlib.sha256(json.dumps(
        ENGINE_SPEC, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _curve(trade_log: dict[str, Any], initial_balance: float) -> list[float]:
    values = [initial_balance]
    for trading_day in sorted(trade_log):
        account = trade_log[trading_day].get("account", {})
        values.append(round(_finite(account.get("balance"), values[-1]), 4))
    return values


def _variant_result(
    sim: Any, initial_balance: float, signals: int, quote: Any,
) -> dict[str, Any]:
    trade_log = dict(getattr(sim, "trade_log", {}) or {})
    metrics = dict(getattr(sim, "tqsdk_stat", {}) or {})
    trades = sum(len(day.get("trades", [])) for day in trade_log.values())
    curve = _curve(trade_log, initial_balance)
    ending = curve[-1]
    return {
        "returnPercent": round((ending / initial_balance - 1) * 100, 4),
        "maxDrawdownPercent": round(_finite(metrics.get("max_drawdown")) * 100, 4),
        "signalCount": signals,
        "winRatePercent": round(_finite(metrics.get("winning_rate")) * 100, 2),
        "tradeCount": trades,
        "commission": round(_finite(metrics.get("commission")), 4),
        "contractMultiplier": _finite(getattr(quote, "volume_multiple", 0)),
        "priceTick": _finite(getattr(quote, "price_tick", 0)),
        "curve": curve,
    }


def _bar(row: Any) -> dict[str, float | int]:
    return {
        "datetime": int(_finite(row.get("datetime"))),
        "open": _finite(row.get("open")),
        "high": _finite(row.get("high")),
        "low": _finite(row.get("low")),
        "close": _finite(row.get("close")),
        "volume": int(_finite(row.get("volume"))),
        "openInterest": int(_finite(row.get("close_oi"))),
    }


def _trading_dates(api: Any, start_date: date, end_date: date) -> set[str]:
    calendar = api.get_trading_calendar(
        start_date - timedelta(days=7), end_date + timedelta(days=21),
    )
    return {
        value.date().isoformat()
        for value, trading in zip(calendar["date"], calendar["trading"], strict=True)
        if bool(trading)
    }


def _trading_day(timestamp: int, trading_dates: set[str]) -> str:
    local = datetime.fromtimestamp(
        timestamp / 1_000_000_000, tz=SHANGHAI_TIMEZONE,
    )
    current_date = local.date()
    candidates = sorted(date.fromisoformat(value) for value in trading_dates)
    if local.hour < 18 and current_date in candidates:
        return current_date.isoformat()
    target = next((value for value in candidates if value > current_date), None)
    if target is None:
        raise RuntimeError("trading_calendar_unavailable")
    return target.isoformat()


def _annotate_trading_day(
    bar: dict[str, float | int], trading_dates: set[str],
) -> dict[str, float | int | str]:
    return {
        **bar,
        "tradingDay": _trading_day(int(bar["datetime"]), trading_dates),
    }


def _in_requested_window(timestamp: int, start_date: date, end_date: date) -> bool:
    trading_date = datetime.fromtimestamp(
        timestamp / 1_000_000_000, tz=SHANGHAI_TIMEZONE,
    ).date()
    return start_date <= trading_date <= end_date


def _completed_signal(
    klines: Any, volume_threshold: float, ma: Any,
) -> tuple[dict[str, float | int], bool]:
    completed = _bar(klines.iloc[-2])
    short_average = _finite(ma(klines, 5).iloc[-2]["ma"], float("nan"))
    medium_average = _finite(ma(klines, 20).iloc[-2]["ma"], float("nan"))
    volume_average = _finite(klines.volume.iloc[-21:-1].mean(), float("nan"))
    current_volume = _finite(klines.volume.iloc[-2], float("nan"))
    ready = all(math.isfinite(value) for value in (
        short_average, medium_average, volume_average, current_volume,
    )) and volume_average > 0 and (
        completed["close"] > short_average
        and short_average > medium_average
        and current_volume / volume_average >= volume_threshold
    )
    return completed, ready


def _run_variant(
    *, username: str, password: str, symbol: str, start_date: date, end_date: date,
    duration_seconds: int, volume_threshold: float, initial_balance: float,
) -> tuple[dict[str, Any], list[dict[str, float | int | str]]]:
    from tqsdk import (  # type: ignore[import-not-found]
        BacktestFinished,
        TargetPosTask,
        TqApi,
        TqAuth,
        TqBacktest,
        TqSim,
    )
    from tqsdk.ta import MA  # type: ignore[import-not-found]

    sim = TqSim(init_balance=initial_balance)
    api = TqApi(
        account=sim,
        backtest=TqBacktest(start_dt=start_date, end_dt=end_date),
        auth=TqAuth(username, password),
        debug=False,
        disable_print=True,
    )
    klines = api.get_kline_serial(symbol, duration_seconds, data_length=21)
    quote = api.get_quote(symbol)
    trading_dates = _trading_dates(api, start_date, end_date)
    target = TargetPosTask(api, symbol, price="ACTIVE", account=sim)
    bars: dict[int, dict[str, float | int | str]] = {}
    signals = 0
    previous_target = 0
    try:
        while True:
            api.wait_update()
            if not api.is_serial_ready(klines):
                continue
            if api.is_changing(klines.iloc[-1], [
                "datetime", "open", "high", "low", "close", "volume", "close_oi",
            ]):
                latest = _annotate_trading_day(
                    _bar(klines.iloc[-1]), trading_dates,
                )
                timestamp = int(latest["datetime"])
                if timestamp > 0 and _in_requested_window(
                    timestamp, start_date, end_date,
                ):
                    bars[timestamp] = latest
            if not api.is_changing(klines.iloc[-1], "datetime"):
                continue
            completed_raw, ready = _completed_signal(klines, volume_threshold, MA)
            completed = _annotate_trading_day(completed_raw, trading_dates)
            completed_timestamp = int(completed["datetime"])
            if completed_timestamp <= 0 or not _in_requested_window(
                completed_timestamp, start_date, end_date,
            ):
                continue
            bars[completed_timestamp] = completed
            next_target = 1 if ready else 0
            if next_target != previous_target:
                signals += 1
                previous_target = next_target
                target.set_target_volume(next_target)
    except BacktestFinished:
        pass
    finally:
        api.close()
    return (
        _variant_result(sim, initial_balance, signals, quote),
        [bars[timestamp] for timestamp in sorted(bars)],
    )


def run_tqsdk_backtest(
    request: dict[str, Any], username: str, password: str,
) -> dict[str, Any]:
    """Run two isolated official TqSim variants; never use a remote trade account."""
    common = {
        "username": username,
        "password": password,
        "symbol": str(request["symbol"]),
        "start_date": date.fromisoformat(str(request["startDate"])),
        "end_date": date.fromisoformat(str(request["endDate"])),
        "duration_seconds": int(request["durationSeconds"]),
        "initial_balance": float(request["initialBalance"]),
    }
    baseline, baseline_bars = _run_variant(
        **common, volume_threshold=float(request["baselineThreshold"]),
    )
    if float(request["baselineThreshold"]) == float(request["candidateThreshold"]):
        candidate, candidate_bars = dict(baseline), list(baseline_bars)
    else:
        candidate, candidate_bars = _run_variant(
            **common, volume_threshold=float(request["candidateThreshold"]),
        )
    canonical = lambda bars: json.dumps(
        bars, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    baseline_digest = hashlib.sha256(canonical(baseline_bars)).hexdigest()
    if baseline_digest != hashlib.sha256(canonical(candidate_bars)).hexdigest():
        raise RuntimeError("tqsdk_backtest_data_mismatch")
    if (
        baseline["contractMultiplier"] != candidate["contractMultiplier"]
        or baseline["priceTick"] != candidate["priceTick"]
    ):
        raise RuntimeError("tqsdk_backtest_contract_mismatch")
    return {
        "engineVersion": "tqsdk-3.10.2-tqsim-tqbacktest",
        "engineDigest": _engine_digest(),
        "dataSource": (
            f"tqsdk:{common['symbol']}:{request['startDate']}:{request['endDate']}:"
            f"{common['duration_seconds']}"
        ),
        "dataDigest": baseline_digest,
        "dataPointCount": len(baseline_bars),
        "contractMultiplier": baseline["contractMultiplier"],
        "priceTick": baseline["priceTick"],
        "fillMode": "ACTIVE",
        "commissionModel": "tqsdk_default",
        "bars": baseline_bars,
        "baseline": baseline,
        "candidate": candidate,
        "executionEnabled": False,
        "orderCapabilities": [],
        "accountType": "TqSim",
    }
