from __future__ import annotations

import csv
import importlib.util
import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

MAX_DATASET_ROWS = 100_000


def validate_dataset_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        raise RuntimeError("backtest_dataset_empty")
    if len(rows) > MAX_DATASET_ROWS:
        raise RuntimeError("backtest_dataset_too_large")


def load_strategy(path: Path):
    spec = importlib.util.spec_from_file_location("immutable_strategy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("strategy_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name in ("TrendInputs", "TrendParameters", "signal_ready"):
        if not hasattr(module, name):
            raise RuntimeError(f"strategy_contract_missing:{name}")
    return module


def max_drawdown(curve: list[float]) -> float:
    peak = curve[0]
    drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak * 100)
    return drawdown


def validate_result(result: dict[str, Any]) -> None:
    for field in ("returnPercent", "maxDrawdownPercent", "winRatePercent"):
        value = result.get(field)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuntimeError("backtest_non_finite_result")
    if not isinstance(result.get("signalCount"), int) or result["signalCount"] < 0:
        raise RuntimeError("backtest_result_invalid")
    curve = result.get("curve")
    if (
        not isinstance(curve, list)
        or not curve
        or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in curve)
    ):
        raise RuntimeError("backtest_non_finite_curve")


def evaluate(module: Any, rows: list[dict[str, str]], threshold: Decimal) -> dict[str, Any]:
    equity = 100.0
    curve = [equity]
    returns: list[float] = []
    for row in rows:
        inputs = module.TrendInputs(
            row["priceAboveShortAverage"] == "1",
            row["mediumTrendAligned"] == "1",
            Decimal(row["volumeRatio"]),
        )
        ready = bool(module.signal_ready(inputs, module.TrendParameters(volume_threshold=threshold)))
        period_return = float(row["returnPercent"]) if ready else 0.0
        if not math.isfinite(period_return):
            raise RuntimeError("dataset_non_finite_return")
        if period_return < -100:
            raise RuntimeError("dataset_return_below_minus_100")
        if ready:
            returns.append(period_return)
        equity *= 1 + period_return / 100
        if not math.isfinite(equity):
            raise RuntimeError("backtest_non_finite_equity")
        if equity < 0:
            raise RuntimeError("backtest_negative_equity")
        curve.append(round(equity, 4))
    wins = sum(1 for value in returns if value > 0)
    result = {
        "returnPercent": round(equity - 100, 3),
        "maxDrawdownPercent": round(max_drawdown(curve), 3),
        "signalCount": len(returns),
        "winRatePercent": round((wins / len(returns) * 100) if returns else 0.0, 2),
        "curve": curve,
    }
    validate_result(result)
    return result


def main() -> int:
    config = json.loads(sys.stdin.readline(64 * 1024))
    strategy = load_strategy(Path(config["strategyPath"]))
    with Path(config["dataPath"]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    validate_dataset_rows(rows)
    baseline = evaluate(strategy, rows, Decimal(str(config["baselineThreshold"])))
    candidate = evaluate(strategy, rows, Decimal(str(config["candidateThreshold"])))
    print(json.dumps(
        {"baseline": baseline, "candidate": candidate},
        separators=(",", ":"),
        allow_nan=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
