"""Library applicability is independent of the concrete execution contract."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid5

from .instruments import CONTRACT_MONTH_PATTERN, CONTRACT_PRODUCT_PATTERN, STRATEGY_CONTRACT_PATTERN


def normalize_scope(value: dict | None) -> dict:
    value = {"mode": "any", "symbols": []} if value is None else value
    if not isinstance(value, dict):
        raise ValueError("适用范围格式无效")
    if value.get("mode") not in {"any", "selected"}:
        raise ValueError("请选择全部品种或指定品种")
    raw = value.get("symbols", [])
    if not isinstance(raw, list) or len(raw) > 30:
        raise ValueError("最多指定 30 个品种或合约")
    symbols = list(dict.fromkeys(str(s).strip() for s in raw if str(s).strip()))
    if value["mode"] == "selected" and (not symbols or any(not re.fullmatch(rf"{CONTRACT_PRODUCT_PATTERN}{CONTRACT_MONTH_PATTERN}?", s) for s in symbols)):
        raise ValueError("请填写有效品种或合约代码，例如 SHFE.au、SHFE.rb2610、CME.GCEZ26")
    return {"mode": value["mode"], "symbols": symbols if value["mode"] == "selected" else []}


def instrument_scope(strategy: dict) -> dict:
    return strategy["definition"].get("instrumentScope", {"mode": "selected", "symbols": [strategy["symbol"]]})


def allows_symbol(strategy: dict, symbol: str) -> bool:
    scope = instrument_scope(strategy)
    if scope["mode"] == "any":
        return True
    return any(symbol.casefold() == s.casefold() or
               (not re.fullmatch(STRATEGY_CONTRACT_PATTERN, s) and re.fullmatch(re.escape(s) + CONTRACT_MONTH_PATTERN, symbol, re.I))
               for s in scope["symbols"])


def execution_strategy(strategy: dict, symbol: str, *, store, broker, user: str,
                       chart_timeframe: int | None = None, trading_account_id: str | None = None) -> dict:
    """Instantiate one execution of a library strategy. `trading_account_id`
    binds the new draft (and its source session) to the caller's account; the
    store default is only a last resort for callers without accounts."""
    if strategy["status"] == "archived":
        raise ValueError("策略已删除，请从策略库选择其他策略")
    plan = strategy["definition"].get("plan") or {}
    if plan.get("spread"):
        # 价差: the execution copy is bound to the A leg — it drives the schedule and the quote;
        # the legs themselves define what is read and (next phase) traded.
        symbol = str(plan["spread"]["legs"][0]["symbol"])
    if not re.fullmatch(STRATEGY_CONTRACT_PATTERN, symbol) or not allows_symbol(strategy, symbol):
        raise ValueError("请选择适用范围内的具体合约")
    account_id = trading_account_id or store.default_trading_account_id
    if "instrumentScope" not in strategy["definition"] or strategy["definition"].get("libraryStrategyId"):
        if strategy["symbol"].casefold() != symbol.casefold():
            raise ValueError("合约不在适用范围内")
        if strategy["tradingAccountId"] != account_id:
            raise ValueError("执行策略属于其他交易账户，请从策略库重新选择")
        return strategy
    definition = {**strategy["definition"], "libraryStrategyId": strategy["strategyId"]}
    from .strategy_chart_period import bind_definition
    definition = bind_definition(definition, chart_timeframe)
    identity = f'{user}:{strategy["strategyId"]}:{strategy["definitionDigest"]}:{symbol.casefold()}'
    # Keep existing executions and their monitor state on their original account.
    # Older ids omitted the account; they must never be reused by another account.
    try:
        legacy = store.get_strategy_draft(str(uuid5(NAMESPACE_URL, identity)), user_id=user)
        if legacy["tradingAccountId"] == account_id:
            return legacy
    except KeyError:
        pass
    identifier = str(uuid5(NAMESPACE_URL, f"{identity}:{account_id}"))
    try:
        return store.get_strategy_draft(identifier, user_id=user)
    except KeyError:
        session = broker.create_session("local", "native", user, trading_account_id=account_id)
        execution_name = f'{strategy["name"]} · {symbol}'
        return store.save_strategy_draft(strategy_id=identifier, user_id=user,
            source_session_id=session.session_id, name=execution_name, symbol=symbol,
            version=store.next_strategy_draft_version(user_id=user, name=execution_name), status="draft", definition=definition,
            definition_digest=hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest(),
            trading_account_id=account_id)
