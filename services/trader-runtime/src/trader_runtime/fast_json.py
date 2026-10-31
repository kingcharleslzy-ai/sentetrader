"""JSON on the Runtime's hot paths (feed frames, SSE envelopes, sidecar and pool responses), via orjson.

2026-10-13 扩容①: at 30 readers × 40 charts the stdlib encoder/decoder was a fifth of the event loop
(decoding every K-line workbench, encoding every feed frame). orjson (3.12, Apache-2.0 OR MIT, with
MPL-2.0 vendored parts) does the same work several times faster. Its output is what the stdlib call
with `ensure_ascii=False, separators=(",", ":")` produced, with these differences only:

- Chinese and other non-ASCII text: raw UTF-8 in both (the stdlib calls here already passed
  `ensure_ascii=False`; `shared_market.event` used to escape it — same text after parsing).
- Floats: the same shortest round-trip digits; an exponent is written `1e-7`, not `1e-07` (same value).
- NaN / ±Infinity: `null` instead of the bare `NaN` token no browser's JSON.parse accepts.
- What the stdlib refuses (sets, Decimal, datetime, dataclasses) stays refused — the same TypeError, so
  the SSE stream's "drop one unserializable transient" rule still applies. Enum and UUID values encode.
- Anything orjson itself refuses where the stdlib does not — non-string dict keys, integers beyond 64
  bits, lone surrogates — falls back to the stdlib call, so that output is byte-identical to before.

Decoding: orjson first; what it refuses (NaN tokens, a BOM, UTF-16, lone surrogates, 1e400) goes to
the stdlib, so a document decodes to the same value — except an integer outside the signed/unsigned
64-bit range, which orjson reads as a float without refusing (2026-10-13 第二轮核对). Use `loads` only
for documents that cannot hold one (sidecar/pool payloads, this Runtime's own stored columns), never
for free-form user JSON. Both raise `ValueError` subclasses.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import orjson

_OPTIONS = orjson.OPT_PASSTHROUGH_DATETIME | orjson.OPT_PASSTHROUGH_DATACLASS


def dumps(value: Any, *, sort_keys: bool = False, default: Callable[[Any], Any] | None = None) -> str:
    """Compact JSON text (no spaces, non-ASCII kept), keys in insertion order unless `sort_keys`."""
    try:
        return orjson.dumps(value, default=default,
                            option=(_OPTIONS | orjson.OPT_SORT_KEYS) if sort_keys else _OPTIONS).decode()
    except orjson.JSONEncodeError:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys, default=default)


def loads(data: bytes | bytearray | str) -> Any:
    try:
        return orjson.loads(data)
    except orjson.JSONDecodeError:
        return json.loads(data)
