"""Trim the expired-contract table every live TqApi loads at construction (2026-10-13 内存调查).

TqSdk 3.10.2's ``TqApi.__init__`` reads its bundled ``expired_quotes.json.lzma`` into
``api._pre20_ins_info``: 14 246 contracts that expired before 2020-09 (2 488 futures, 7 337
futures options, 3 973 combinations, 448 options), about 26 MiB of Python dicts per TqApi, held
for the API's whole life. TqSdk uses the table in one way only: a quote query for a symbol in it is
sent alone, and when the contract service answers that symbol with an error its row is taken from
the table (``symbols.py`` / ``utils._query_for_quote``). There is no constructor option that skips
it (``_stock=False`` swaps it for the old contract service that loads every contract instead).

Our live sessions only ever subscribe catalogue contracts (current futures, ``query_quotes(
ins_class="FUTURE", expired=False)``) and never options or combinations, so the non-futures rows
are never read. ``trim_expired_contract_table`` drops them right after construction and keeps
every expired FUTURE row, so even a pre-2020 futures symbol keeps resolving exactly as before.
The kept rows are rebuilt as fresh objects so the original table's memory is freed as a whole
rather than pinned by the survivors scattered through it (about 21 MiB back per TqApi).

Backtest and replay APIs (TqBacktest) keep the full table: only the live session, the
data-only market/history APIs and the paper engine's market API call this. The trim checks the
exact TqSdk version and the table's shape and silently does nothing when either differs, so an
SDK upgrade can only turn it off, never break a session. The vendored/installed TqSdk source is
not modified.
"""
from __future__ import annotations

import json
from typing import Any

# The TqSdk release whose private table layout was read (api.py `_pre20_ins_info`, symbols.py).
TRIM_TQSDK_VERSION = "3.10.2"
# Expired rows of these classes stay: a futures symbol must resolve as it always did.
KEPT_INS_CLASSES = frozenset({"FUTURE"})


def _installed_version() -> str | None:
    try:
        import tqsdk  # type: ignore[import-not-found]
    except Exception:
        return None
    return str(getattr(tqsdk, "__version__", "") or "") or None


def trim_expired_contract_table(api: Any, *, version: str | None = None) -> int:
    """Drop the expired non-futures rows from ``api._pre20_ins_info``; the number dropped.

    0 (and the table untouched) when the TqSdk version is not the one this was written for, the
    attribute is missing or not a dict, or any row does not look like an expired contract row."""
    if (version if version is not None else _installed_version()) != TRIM_TQSDK_VERSION:
        return 0
    table = getattr(api, "_pre20_ins_info", None)
    if not isinstance(table, dict) or not table:
        return 0
    kept: dict[str, Any] = {}
    for symbol, row in table.items():
        if not isinstance(row, dict) or not isinstance(row.get("ins_class"), str) or row.get("expired") is not True:
            return 0  # not the layout we know: leave the SDK alone
        if row["ins_class"] in KEPT_INS_CLASSES:
            kept[symbol] = row
    dropped = len(table) - len(kept)
    if dropped == 0:
        return 0
    # A JSON round trip gives the survivors fresh objects (NaN included), so no allocator block of
    # the original table stays pinned by them once it is released below.
    api._pre20_ins_info = json.loads(json.dumps(kept))
    return dropped
