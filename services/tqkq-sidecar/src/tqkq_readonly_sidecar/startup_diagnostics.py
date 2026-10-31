"""Offline SDK import profiling: never construct an account or TqApi."""
from __future__ import annotations

import importlib
import sys
import time
from typing import Any


def profile_sdk_import() -> dict[str, Any]:
    started: dict[int, tuple[str, float]] = {}
    durations: dict[str, float] = {}

    def profile(frame: Any, event: str, arg: Any) -> None:
        if frame.f_code.co_name != "_find_and_load":
            return
        if event == "call":
            started[id(frame)] = (str(frame.f_locals.get("name", "")), time.perf_counter())
        elif event == "return" and id(frame) in started:
            name, at = started.pop(id(frame))
            durations[name] = max(durations.get(name, 0.0), time.perf_counter() - at)

    previous = sys.getprofile()
    at = time.perf_counter()
    try:
        sys.setprofile(profile)
        importlib.import_module("tqsdk")
    finally:
        sys.setprofile(previous)
    return {
        "schemaVersion": 1,
        "operation": "sdk_import",
        "frozen": bool(getattr(sys, "frozen", False)),
        "elapsedSeconds": round(time.perf_counter() - at, 3),
        "modules": [
            {"module": name, "inclusiveSeconds": round(seconds, 3)}
            for name, seconds in sorted(durations.items(), key=lambda item: item[1], reverse=True)[:25]
        ],
        "accountConnected": False,
    }
