"""Confirm a direction once, retaining its identity until direction is broken."""
from __future__ import annotations

import math

from .strategy_rules import group_conditions, indicator_values, required_condition_bars


def evaluate_alignment(group, bars, name, context=None):
    conditions = group_conditions(group)
    series = [(condition, indicator_values(bars, condition.left, context), indicator_values(bars, condition.right, context))
              for condition in conditions]
    split = len(group.alignmentConditions)
    start = confirmed = None
    boundary_seen = False

    def passes(item, index):
        condition, left, right = item
        a, b = left[index], right[index]
        if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[condition.operator]

    # A truncated series starting inside an old trend cannot invent a new entry.
    for i in range(max(required_condition_bars(c) for c in conditions) - 1, len(bars)):
        aligned = all(passes(item, i) for item in series[:split])
        if not aligned:
            start = confirmed = None
            boundary_seen = True
            continue
        if not boundary_seen:
            continue
        if start is None:
            start = i
        if confirmed is None and all(passes(item, i) for item in series[split:]):
            confirmed = i

    # Confirmation survives a temporary filter reversal, but entry still needs
    # the filter now. Execution persists this segment's identity across bars,
    # exits and restarts, preventing a second entry after a stop.
    ready = confirmed is not None and all(passes(item, len(bars) - 1) for item in series[split:])
    alignment = ({"id": f"{name}:{bars[start]['datetime']}",
                  "startTime": str(bars[start]["datetime"]),
                  "confirmedTime": str(bars[confirmed]["datetime"])} if confirmed is not None else None)
    detail = ("方向已确认；本段最多一次开仓尝试，已有持仓时不加仓" if ready else
              "方向保持，等待辅助条件确认" if start is not None else "等待方向重新形成")
    return ready, alignment, {"label": "方向分段确认", "status": "passed" if ready else "waiting",
        "detail": detail, "hits": [int(bars[-1]["datetime"])] if ready else [],
        "time": int(bars[-1]["datetime"]) if ready else None}
