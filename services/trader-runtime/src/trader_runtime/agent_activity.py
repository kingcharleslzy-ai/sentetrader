"""Read-only, fill-backed activity shared by the chart and the agent.

2026-10-13 第二轮省 CPU：夜盘 60 读者档里活动帧占 Runtime 17%（另有策略草稿全文解析 9.6%）——
同一账户每个关注合约每秒各读一遍该账户全部 AI 委托链接、逐条解析提案 JSON，再整份解析 50 份
策略定义（活动帧只用名称、状态、合约）。现在：

- 与合约无关、只取自数据库的部分（AI 委托、策略摘要、监控状态与最近尝试、保护单）是一份
  `ActivityBase`，按 (用户, 交易账户) 建。推送通路每一轮每个账户只建一份，各合约的帧共用
  （`wiring/feed.py`）；HTTP、AI 工具等其他调用照旧每次现建。
- 只有自动来源（ai_autonomous / deterministic_stop）的提案才取出并解析（SQL 先筛）。
- 策略只读摘要列（`list_strategy_draft_heads`），不解析定义。

#518 账户隔离不变：AI 委托链接经调用方自己账户的委托服务读取，保护单按调用方账户读取；
`ActivityBase` 记着建它的用户与账户，拿另一账户的底稿来拼帧会直接报错。"""
from . import fast_json
from .simulation_orders import SimulationOrderService, order_display_status
from .strategy_monitor import StrategyMonitorService

AUTOMATION_SOURCES = ("ai_autonomous", "deterministic_stop")


class ActivityBase:
    """One user's activity facts on one trading account as the database holds them right now —
    everything `activity_snapshot` reads except the provider frame and the monitor's in-memory
    decisions. Built for one moment (one feed round); never mutated by a snapshot."""

    def __init__(self, store, user: str, orders_service: SimulationOrderService,
                 trading_account_id: str) -> None:
        self.store = store
        self.user = user
        self.trading_account_id = trading_account_id
        # 2026-10-04 isolation (#518): the AI order links are read through the caller's own account
        # like the protections below — another account's order is not just filtered out, it must
        # never even reach this account's order service.
        rows = store.connection.execute("""SELECT l.client_order_id, p.payload_json
            FROM tqkq_sim_order_links l JOIN proposals p ON p.id = l.proposal_id
            WHERE l.user_id = ? AND l.trading_account_id = ?
              AND json_extract(p.payload_json, '$.automationSource') IN (?, ?)
            ORDER BY l.created_at DESC""", (user, trading_account_id, *AUTOMATION_SOURCES)).fetchall()
        self.ai_orders: list[tuple[str, dict, str, str]] = []
        for row in rows:
            # orjson (fast_json): a proposal payload holds this Runtime's own validated fields — no
            # integer beyond 64 bits, the one input orjson reads differently from the stdlib.
            payload = fast_json.loads(row["payload_json"])
            if payload.get("automationSource") not in AUTOMATION_SOURCES:
                continue
            self.ai_orders.append((
                row["client_order_id"], orders_service.get(row["client_order_id"], user_id=user),
                payload.get("reason", ""),
                "stop" if payload.get("automationSource") == "deterministic_stop" else "ai"))
        self.strategies = store.list_strategy_draft_heads(user_id=user, limit=50)
        self._monitor_states: dict[str, dict | None] = {}
        self._attempts: dict[str, list[dict]] = {}
        # 2026-10-02 isolation: protections are read through the caller's own account, never every
        # account's book.
        self._stops = store.connection.execute(
            "SELECT * FROM ai_stop_orders WHERE user_id = ? AND trading_account_id = ?",
            (user, trading_account_id)).fetchall()
        self._protections: dict[int, dict] = {}

    def monitor_state(self, strategy_id: str) -> dict | None:
        if strategy_id not in self._monitor_states:
            self._monitor_states[strategy_id] = self.store.strategy_monitor_state(
                strategy_id=strategy_id, user_id=self.user)
        return self._monitor_states[strategy_id]

    def protections(self, symbol: str) -> list[dict]:
        """The account's protections on `symbol` (all of them for no symbol), each parsed once."""
        from .autonomy import AutonomousSimulationService
        chosen = []
        for index, row in enumerate(self._stops):
            if symbol and row["symbol"] != symbol:
                continue
            if index not in self._protections:
                self._protections[index] = AutonomousSimulationService._stop_public(row)
            chosen.append(self._protections[index])
        return chosen

    def recent_attempts(self, strategy_id: str) -> list[dict]:
        if strategy_id not in self._attempts:
            self._attempts[strategy_id] = self.store.list_strategy_execution_attempts(
                strategy_id=strategy_id, user_id=self.user, limit=10)
        return self._attempts[strategy_id]


def activity_base(app, user: str, simulation_orders: SimulationOrderService | None = None,
                  trading_account_id: str | None = None) -> ActivityBase:
    store = app.state.store
    return ActivityBase(store, user, simulation_orders or app.state.simulation_orders,
                        trading_account_id or store.default_trading_account_id)


def activity_snapshot(
    app, user: str, symbol: str, activity: dict,
    simulation_orders: SimulationOrderService | None = None,
    strategy_monitor: StrategyMonitorService | None = None,
    trading_account_id: str | None = None,
    base: ActivityBase | None = None,
) -> dict:
    state = app.state
    monitor_service = strategy_monitor or state.strategy_monitor
    account_filter = trading_account_id or state.store.default_trading_account_id
    if base is None:
        base = activity_base(app, user, simulation_orders, account_filter)
    elif base.user != user or base.trading_account_id != account_filter:
        raise ValueError("activity_base_mismatch")  # #518: never another user's or account's book
    ai_orders = {}
    for identifier, order, reason, source in base.ai_orders:
        if symbol and order["request"]["symbol"] != symbol:
            continue
        ai_orders[identifier] = {**order, "reason": reason, "source": source}
    fills = [{**trade, "source": ai_orders[trade["clientOrderId"]]["source"]}
             for trade in activity.get("trades", []) if trade.get("clientOrderId") in ai_orders]
    reports = {report.get("clientOrderId"): report for report in activity.get("orders", [])}
    for identifier, order in ai_orders.items():
        report = reports.get(identifier)
        request = order["request"]
        if not report or any(report.get(key) != request.get(key) for key in ("symbol", "direction", "offset", "volume")):
            continue
        volume = int(request["volume"])
        filled = max(int(order["filledVolume"]),
                     sum(fill["volume"] for fill in fills if fill["clientOrderId"] == identifier))
        order["filledVolume"] = filled
        # The order service owns the status (#483): terminal, unknown and anomalous orders keep
        # it. Only an order it confirmed live may advance on this newer frame, and through the
        # same projection, so a FINISHED frame is never guessed into a cancellation.
        if order["displayStatus"] in {"已受理", "部分成交"} and report.get("status") in {"ALIVE", "FINISHED"}:
            order["displayStatus"] = order_display_status(
                remote_status=order["remoteStatus"], submit_phase=order["submitPhase"],
                cancel_phase=order["cancelPhase"], volume=volume,
                order={**report, "volumeLeft": volume - filled})
    strategies = []
    for strategy in base.strategies:
        if strategy["symbol"] == "*" or symbol and strategy["symbol"] != symbol:
            continue
        monitor = base.monitor_state(strategy["strategyId"])
        if strategy["status"] != "monitoring" and monitor is None:
            continue
        strategies.append({"strategyId": strategy["strategyId"], "name": strategy["name"],
            "status": strategy["status"], "reminderOnly": bool(strategy.get("reminderOnly")), "decision": monitor_service.latest_results.get(strategy["strategyId"]),
            "monitorState": monitor,
            "recentAttempts": base.recent_attempts(strategy["strategyId"])})
    # Attribute only through persisted execution links, never through symbol/name.
    for strategy in strategies:
        for attempt in strategy["recentAttempts"]:
            for identifier in [attempt.get("clientOrderId"), *(leg.get("clientOrderId") for leg in attempt.get("legs", []))]:
                if identifier in ai_orders:
                    ai_orders[identifier]["strategyId"] = strategy["strategyId"]
    protections = base.protections(symbol)
    for protection in protections:
        if protection["closeClientOrderId"] in ai_orders:
            ai_orders[protection["closeClientOrderId"]]["strategyId"] = protection["strategyId"]
    for fill in fills:
        fill["strategyId"] = ai_orders[fill["clientOrderId"]].get("strategyId")
    return {"environment": "replay" if getattr(state.data_provider, "identifier", None) else "simulation",
        "replayId": getattr(state.data_provider, "identifier", None), "symbol": symbol,
        "capturedAt": activity.get("capturedAt"), "dataStatus": activity.get("dataStatus"),
        "ageSeconds": activity.get("ageSeconds"), "orders": list(ai_orders.values())[:100],
        "fills": sorted(fills, key=lambda item: item["tradeDateTime"]), "strategies": strategies, "protections": protections}


async def foreign_half(accounts, *, account_id: str, user: str, symbol: str | None, assemble: bool = False):
    """人民币本站模拟账户在外盘图表上（``symbol`` 是外盘主连或月份合约，或不限合约）的美元子账户那一半：
    (子账户编号, 它的账户服务或 None——此刻读不到, 外盘品种或 None——不限合约)；国内合约、没有子账户的账户为 None。
    子账户只认 ``foreign_account_of`` 已开好的（与持仓面板同一个）；``assemble`` 时按需装配。操作记录
    （``with_foreign_half``）与它读的复盘（``/api/trade-review``）共用这一处。"""
    from .instruments import foreign_product_of

    foreign_of = getattr(accounts, "foreign_account_of", None)
    product = foreign_product_of(symbol) if symbol else None
    if foreign_of is None or not str(account_id).startswith("paper:") or (symbol and product is None):
        return None
    foreign_id = foreign_of(str(account_id))
    if foreign_id is None:
        return None
    services = accounts.get(foreign_id)
    resolver = getattr(accounts, "foreign_account", None)
    if services is None and assemble and resolver is not None:
        await resolver(user)
        services = accounts.get(foreign_id)
    return foreign_id, services, product


async def with_foreign_half(app, snapshot: dict, *, accounts, account_id: str, user: str, symbol: str,
                            assemble: bool = False) -> dict:
    """外盘仓合并显示 (外盘盘点 D 2026-10-21)：人民币本站模拟账户的操作记录在外盘图表上（``symbol`` 是外盘主连或月份
    合约，或不限合约）带上它的美元子账户那一半——同一品种的 AI 委托、成交与保护单，每条写明 ``accountId`` 与
    ``currency``（USD），与持仓面板、AI 读数（``foreign_account_of`` / ``merge_paper_account_status`` 一路）同一个
    子账户。国内合约、没有子账户的账户原样返回；子账户此刻读不到时 ``foreignAccount.status`` 写 unavailable。
    HTTP 读取可按需装配子账户（``assemble``），推送只读已装配的。策略列表是按用户的，不重复列。"""
    from .data_providers import provider_read_snapshot
    from .instruments import foreign_product_of

    found = await foreign_half(accounts, account_id=account_id, user=user, symbol=symbol, assemble=assemble)
    if found is None:
        return snapshot
    foreign_id, services, product = found
    half = {"accountId": foreign_id, "currency": "USD"}
    if services is None:
        return {**snapshot, "foreignAccount": {**half, "status": "unavailable"}}
    try:
        async with provider_read_snapshot(services.provider):
            activity = services.provider.account_activity()
    except (RuntimeError, OSError, ValueError):
        return {**snapshot, "foreignAccount": {**half, "status": "unavailable"}}
    foreign = activity_snapshot(app, user, "", activity, simulation_orders=services.simulation_orders,
                                strategy_monitor=services.strategy_monitor, trading_account_id=foreign_id)

    def same(item_symbol: object) -> bool:
        return product is None or foreign_product_of(str(item_symbol or "")) is product

    orders = [{**order, **half} for order in foreign["orders"] if same(order["request"]["symbol"])]
    fills = [{**fill, **half} for fill in foreign["fills"] if same(fill.get("symbol"))]
    protections = [{**item, **half} for item in foreign["protections"] or [] if same(item.get("symbol"))]
    return {**snapshot, "orders": (snapshot["orders"] + orders)[:100],
            "fills": sorted(snapshot["fills"] + fills, key=lambda item: item["tradeDateTime"]),
            "protections": (snapshot.get("protections") or []) + protections,
            "foreignAccount": {**half, "status": "ok"}}
