"""后台内存诊断 (2026-10-26): find what keeps growing in a live Runtime without a restart or a debugger.

Off by default. The owner starts it (Python's own tracemalloc plus a baseline snapshot), reads which source lines
allocated the memory still alive since that baseline, and stops it. Nothing is stored; tracing costs memory and a
little CPU only while it is on, so it is meant for an hour's hunt, not to be left running.
"""
from __future__ import annotations

import gc
import linecache
import resource
import threading
import tracemalloc
from collections import Counter
from datetime import UTC, datetime
from typing import Any

MAX_FRAMES = 25
_lock = threading.Lock()
_state: dict[str, Any] = {"baseline": None, "startedAt": None, "frames": 0}


def _rss_kb() -> int | None:
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def _status() -> dict[str, Any]:
    traced, peak = tracemalloc.get_traced_memory() if tracemalloc.is_tracing() else (0, 0)
    return {"tracing": tracemalloc.is_tracing(), "startedAt": _state["startedAt"], "frames": _state["frames"],
            "rssKb": _rss_kb(), "maxRssKb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "tracedKb": traced // 1024, "tracedPeakKb": peak // 1024}


def start(frames: int) -> dict[str, Any]:
    """Begin tracing (or move the baseline to now when already tracing)."""
    with _lock:
        if not tracemalloc.is_tracing():
            tracemalloc.start(frames)
            _state["frames"] = frames
        _state["baseline"] = tracemalloc.take_snapshot()
        _state["startedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
        return _status()


def stop() -> dict[str, Any]:
    with _lock:
        tracemalloc.stop()
        _state.update(baseline=None, startedAt=None, frames=0)
        return _status()


def report(limit: int, *, types: bool) -> dict[str, Any]:
    """The allocations alive now that were not alive at the baseline, grouped by their call stack, largest first;
    with `types`, the live object count per type (a full gc walk: seconds on a big heap)."""
    with _lock:
        baseline = _state["baseline"]
        payload = _status()
        if baseline is not None:
            snapshot = tracemalloc.take_snapshot().filter_traces((
                tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, linecache.__file__)))
            stats = snapshot.compare_to(baseline, "traceback")
            payload["growthKb"] = sum(stat.size_diff for stat in stats) // 1024
            payload["top"] = [{"sizeKb": stat.size_diff // 1024, "count": stat.count_diff,
                               "stack": [f"{frame.filename.rsplit('site-packages/', 1)[-1]}:{frame.lineno}"
                                         for frame in reversed(stat.traceback)]}
                              for stat in stats[:limit] if stat.size_diff > 0]
    if types:
        counts = Counter(type(item).__name__ for item in gc.get_objects())
        payload["types"] = dict(counts.most_common(limit))
    return payload
