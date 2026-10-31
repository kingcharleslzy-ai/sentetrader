"""私人记忆 3b (2026-09-30): 程序统计（trade_stats 纯函数）、按 statKey 就地更新
（apply_stats，用户删掉的不加回）、从用户原话提炼（extract_from_words 逐条校验）、
每天 06:00 的整理器（认领表去重、关记忆跳过、单用户失败不连坐）和手动接口。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from test_ai_annotations import HEADERS
from trader_runtime import user_memory
from trader_runtime.main import create_app
from trader_runtime.memory_consolidation import (
    CHANGED_MIND,
    MemoryConsolidator,
    MemoryOp,
    apply_stats,
    extract_from_words,
)
from trader_runtime.memory_stats import INVALID_RECOMPUTE_REASON, trade_stats
from trader_runtime.store import RuntimeStore
from trader_runtime.trading_costs import FeeSchedule, review_account_trades

USER = "local-user"
CHINA = ZoneInfo("Asia/Shanghai")
RB, AU = "SHFE.rb2701", "SHFE.au2612"


def _app(tmp_path: Path, name: str):
    return create_app(tmp_path / name, runtime_state_root=tmp_path, agent_mode="fake",
                      local_token="annotation-test-token")


def at(day: str, hour: int) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+08:00").timestamp() * 1e9)


def round_trip(index: int, *, symbol: str = RB, direction: str = "long", volume: int = 2,
               minutes: int = 60, pnl: float = 1000.0, source: str = "manual",
               day: str = "2026-09-20", hour: int = 10, stance: str | None = None) -> dict:
    opened = at(day, hour)
    return {"id": f"t{index}", "symbol": symbol, "direction": direction, "volume": volume,
            "openTime": opened, "closeTime": opened + minutes * 60 * 10**9, "openPrice": 100.0,
            "closePrice": 100.0, "gross": pnl, "net": pnl, "fees": 0.0, "source": source,
            "holdingMinutes": minutes, "deskView": {"stance": stance} if stance else None}


# 手动单 6 笔（5 赢 1 亏）、AI 建议的黄金 5 笔全亏且逆着当时的偏空判断做多、夜盘 AI 自主 3 笔全亏。
def _dataset() -> list[dict]:
    return [
        round_trip(1, pnl=2000, day="2026-09-20"),
        round_trip(2, pnl=1000, day="2026-09-21"),
        round_trip(3, pnl=-500, day="2026-09-22"),
        round_trip(4, pnl=3000, minutes=90, day="2026-09-23"),
        round_trip(5, pnl=1000, day="2026-09-24"),
        round_trip(6, pnl=500, day="2026-09-25"),
        *[round_trip(index, symbol=AU, direction="long", volume=1, minutes=30, pnl=pnl, source="card",
                     day=day, hour=10, stance="偏空")
          for index, pnl, day in ((7, -2000.0, "2026-09-22"), (8, -1500.0, "2026-09-23"),
                                  (9, -3000.0, "2026-09-24"), (10, -800.0, "2026-09-25"), (11, -1200.0, "2026-09-26"))],
        *[round_trip(index, pnl=pnl, source="ai", minutes=30, day=day, hour=22)
          for index, pnl, day in ((12, -800.0, "2026-09-26"), (13, -1200.0, "2026-09-26"), (14, -300.0, "2026-09-27"))],
    ]


def test_trade_stats_renders_every_expected_text():
    stats = {entry["key"]: entry for entry in trade_stats(_dataset(), names={RB: "螺纹钢", AU: "黄金"},
                                                          now_ns=at("2026-09-28", 10))}
    assert stats["stat:products"]["text"] == "常做：螺纹钢（占 64%）、黄金（占 36%）"
    assert stats["stat:holding"]["text"] == "多为日内短线（持仓中位 30 分钟）"
    assert stats["stat:size"]["text"] == "一般每笔 2 手"
    assert stats["stat:source:manual"]["text"] == "近 90 天手动单 6 笔，赢 5 笔（胜率 83%），平均每笔 +1,167 元"
    assert stats["stat:source:card"]["text"] == "近 90 天AI 建议单 5 笔，赢 0 笔（胜率 0%），平均每笔 -1,700 元"
    assert stats["stat:desk:against"]["text"] == "近 90 天逆着调研判断做的 5 笔，赢 0 笔（胜率 0%），平均每笔 -1,700 元"
    assert stats["stat:product:黄金"]["text"] == "近 90 天黄金 5 笔，赢 0 笔（胜率 0%），平均每笔 -1,700 元"
    assert stats["stat:product:黄金"]["tags"] == ["黄金"]
    assert stats["stat:products"]["n"] == 14 and stats["stat:products"]["wins"] == 5
    assert len(stats["stat:products"]["roundIds"]) == 14
    # 不显著（螺纹钢 56%、日盘 45%）或 n<5（AI 自主、夜盘、顺着调研）的分组不产出。
    assert set(stats) == {"stat:products", "stat:holding", "stat:size", "stat:source:manual",
                          "stat:source:card", "stat:desk:against", "stat:product:黄金"}
    # 目录里没有的合约（已到期）按去掉交易所与月份的代码归到同一品种，不按合约拆开。
    unnamed = {entry["key"]: entry for entry in trade_stats(_dataset(), names={}, now_ns=at("2026-09-28", 10))}
    assert unnamed["stat:products"]["text"] == "常做：rb（占 64%）、au（占 36%）"


def test_trade_stats_skips_profiles_below_ten_trades_and_reads_the_review_pipeline():
    few = trade_stats(_dataset()[:9], names={RB: "螺纹钢", AU: "黄金"}, now_ns=at("2026-09-28", 10))
    assert "stat:products" not in {entry["key"] for entry in few}
    assert "stat:source:manual" in {entry["key"] for entry in few}  # 分组经验不受画像门槛影响

    # 复盘同一条链路喂进来的字段：FIFO 配对算出持仓分钟，来源带着走。
    hour_ago = int((datetime.now(UTC) - timedelta(hours=2)).timestamp() * 1e9)
    trading_day = datetime.fromtimestamp(hour_ago / 1e9, CHINA).date().isoformat()
    fills = [{"tradeId": "open-1", "clientOrderId": "order-open-1", "symbol": RB, "direction": "BUY",
              "offset": "OPEN", "price": 3200.0, "volume": 1, "tradeDateTime": hour_ago,
              "tradingDay": trading_day},
             {"tradeId": "close-1", "clientOrderId": "order-close-1", "symbol": RB, "direction": "SELL",
              "offset": "CLOSETODAY", "price": 3210.0, "volume": 1, "tradeDateTime": hour_ago + 10 * 60 * 10**9,
              "tradingDay": trading_day}]
    rounds = review_account_trades(fills, FeeSchedule().model_dump(), {RB: 100}, days=90,
                                   sources={"order-open-1": {"source": "card"}})["trades"]
    from trader_runtime.strategy_workbench import journal_fields
    journal_fields(rounds, {}, None)
    assert rounds[0]["holdingMinutes"] == 10 and rounds[0]["source"] == "card"
    assert trade_stats(rounds, names={RB: "螺纹钢"}, now_ns=hour_ago + 10 * 60 * 10**9) == []


def test_trade_stats_flags_losers_held_far_longer_than_gainers():
    trades = [round_trip(index, pnl=-1000.0, minutes=500, day="2026-09-10") for index in range(1, 4)]
    trades += [round_trip(index, pnl=1000.0, minutes=60, day="2026-09-12") for index in range(4, 7)]
    stats = {entry["key"]: entry for entry in trade_stats(trades, names={}, now_ns=at("2026-09-28", 10))}
    assert stats["stat:hold:losers"]["text"] == "近 90 天亏损单持仓中位 500 分钟，是盈利单（60 分钟）的 8.3 倍"
    assert stats["stat:hold:losers"]["n"] == 3 and stats["stat:hold:losers"]["wins"] == 0


def test_trade_stats_compares_busy_days_with_quiet_days():
    """忙闲日对比（改写自 Vibe-Trading 的 _overtrading）：一天平仓 4 笔的三天每笔都亏，一天 1 笔的五天每笔都赚。"""
    trades, index = [], 0
    for day in ("2026-09-14", "2026-09-15", "2026-09-16"):
        for hour in (9, 10, 13, 14):
            index += 1
            trades.append(round_trip(index, pnl=-300.0, day=day, hour=hour))
    for day in ("2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"):
        index += 1
        trades.append(round_trip(index, pnl=500.0, day=day))
    stats = {entry["key"]: entry for entry in trade_stats(trades, names={}, now_ns=at("2026-09-28", 10))}
    busy = stats["stat:busy-days"]
    assert busy["text"] == "近 90 天平仓多的日子（一天 ≥4 笔）每笔平均 -300 元，少的日子（≤1 笔）每笔平均 +500 元"
    assert busy["layer"] == "lesson" and busy["n"] == 12 and busy["wins"] == 0
    # 忙日反而更好、或差距不到 30%，都不产出。
    flipped = [{**trade, "net": -trade["net"], "gross": -trade["gross"]} for trade in trades]
    assert "stat:busy-days" not in {entry["key"] for entry in trade_stats(flipped, names={}, now_ns=at("2026-09-28", 10))}
    close = [{**trade, "net": 450.0, "gross": 450.0} if trade["net"] > 0 else {**trade, "net": 400.0, "gross": 400.0}
             for trade in trades]
    assert "stat:busy-days" not in {entry["key"] for entry in trade_stats(close, names={}, now_ns=at("2026-09-28", 10))}
    # 每天笔数一样，分不出忙闲。
    even = [round_trip(n, pnl=-100.0 if n % 2 else 200.0, day=f"2026-09-{10 + n:02d}") for n in range(1, 9)]
    assert "stat:busy-days" not in {entry["key"] for entry in trade_stats(even, names={}, now_ns=at("2026-09-28", 10))}


def _stat(key: str, text: str, *, layer: str = "lesson", n: int = 6, tags: list[str] | None = None) -> dict:
    return {"key": key, "layer": layer, "text": text, "n": n, "wins": 5, "roundIds": ["t1"], "tags": tags or []}


def test_apply_stats_updates_invalidates_and_never_revives_user_deletions(tmp_path):
    store = RuntimeStore(tmp_path / "apply.sqlite3")
    try:
        text_a = "常做：螺纹钢（占 64%）"
        first = apply_stats(store, USER, [_stat("stat:products", text_a, layer="profile", n=14),
                                          _stat("stat:source:manual", "近 90 天手动单 6 笔，赢 5 笔（胜率 83%）")])
        assert first == {"added": 2, "updated": 0, "invalidated": 0, "skipped": 0}
        items = {item["evidence"]["statKey"]: item for item in store.list_memories(USER)}
        assert set(items) == {"stat:products", "stat:source:manual"}
        assert items["stat:products"]["source"] == "stats" and items["stat:products"]["tags"] == []
        assert items["stat:products"]["evidence"]["roundIds"] == ["t1"]
        assert [change["actor"] for change in store.list_memory_changes(USER)] == ["consolidation", "consolidation"]

        # 同样的数据再跑一遍：全 skipped，审计不新增。
        again = apply_stats(store, USER, [_stat("stat:products", text_a, layer="profile", n=14),
                                          _stat("stat:source:manual", "近 90 天手动单 6 笔，赢 5 笔（胜率 83%）")])
        assert again == {"added": 0, "updated": 0, "invalidated": 0, "skipped": 2}
        assert len(store.list_memory_changes(USER)) == 2

        # 数据变了：products 更新（evidence 的 n 跟着变），manual 的规律消失 → 失效，冒出一条新的。
        moved = apply_stats(store, USER, [_stat("stat:products", "常做：黄金（占 71%）", layer="profile", n=15),
                                          _stat("stat:source:ai", "近 90 天AI 自主单 5 笔，赢 0 笔（胜率 0%）")])
        assert moved == {"added": 1, "updated": 1, "invalidated": 1, "skipped": 0}
        rows = {item["evidence"]["statKey"]: item for item in store.list_memories(USER, include_invalid=True)}
        assert rows["stat:products"]["text"] == "常做：黄金（占 71%）" and rows["stat:products"]["evidence"]["n"] == 15
        assert rows["stat:source:manual"]["status"] == "invalid"
        assert rows["stat:source:manual"]["invalidReason"] == INVALID_RECOMPUTE_REASON

        # 上次被重算失效的规律回来了 → 重新记下；顺带把本轮没出现的 AI 自主条目失效掉。
        revived = apply_stats(store, USER, [_stat("stat:products", "常做：黄金（占 71%）", layer="profile", n=15),
                                            _stat("stat:source:manual", "近 90 天手动单 6 笔，赢 5 笔（胜率 83%）")])
        assert revived == {"added": 1, "updated": 0, "invalidated": 1, "skipped": 1}
        products_id = next(item["id"] for item in store.list_memories(USER)
                           if item["evidence"].get("statKey") == "stat:products")

        # 用户亲手删掉的统计条目，之后不再自动加回来。
        user_memory.invalidate(store, USER, products_id, "用户删除")
        after_delete = apply_stats(store, USER, [_stat("stat:products", "常做：黄金（占 71%）", layer="profile", n=15),
                                                 _stat("stat:source:manual", "近 90 天手动单 6 笔，赢 5 笔（胜率 83%）")])
        assert after_delete == {"added": 0, "updated": 0, "invalidated": 0, "skipped": 2}
        rows = store.list_memories(USER, include_invalid=True)
        assert {item["evidence"]["statKey"] for item in rows if item["status"] == "active"} == {"stat:source:manual"}
        assert len([item for item in rows if item["invalidReason"] == "用户删除"]) == 1
    finally:
        store.close()


def test_apply_stats_leaves_user_edited_entries_alone(tmp_path):
    """#524-2: a stats entry the user rewrote in settings carries userEdited; the daily
    consolidation neither updates nor invalidates it, nor adds a parallel entry."""
    store = RuntimeStore(tmp_path / "edited.sqlite3")
    try:
        apply_stats(store, USER, [_stat("stat:source:manual", "近 90 天手动单 6 笔，赢 5 笔（胜率 83%）")])
        item = next(memory for memory in store.list_memories(USER) if memory["source"] == "stats")
        edited = user_memory.update(store, USER, item["id"], "手动单我看得懂，改成自己的话")
        assert edited["source"] == "stats" and edited["evidence"]["userEdited"] is True
        assert edited["evidence"]["statKey"] == "stat:source:manual"
        change = store.list_memory_changes(USER)[0]   # 最新在前
        assert change["op"] == "update" and change["actor"] == "user"

        # 数据变了：userEdited 条目跳过——不更新、不并排再记一条。
        moved = apply_stats(store, USER, [_stat("stat:source:manual", "近 90 天手动单 8 笔，赢 6 笔（胜率 75%）")])
        assert moved == {"added": 0, "updated": 0, "invalidated": 0, "skipped": 1}
        assert [memory["text"] for memory in store.list_memories(USER)] == ["手动单我看得懂，改成自己的话"]

        # 本次没有产出该统计：也不因“规律不再成立”把用户改过的条目失效。
        gone = apply_stats(store, USER, [_stat("stat:holding", "多为日内短线（持仓中位 30 分钟）")])
        assert gone == {"added": 1, "updated": 0, "invalidated": 0, "skipped": 0}
        rows = store.list_memories(USER, include_invalid=True)
        assert [memory["status"] for memory in rows if (memory["evidence"] or {}).get("userEdited")] == ["active"]
    finally:
        store.close()


async def test_stats_fall_back_to_gross_when_the_fee_source_is_the_account(tmp_path):
    """#514-4: the account-default fee source carries no per-lot rates — even with custom rates left
    over from before the switch. The consolidation shares the review's fee mask (mask_account_fees):
    the stats sit on gross and say so in evidence (pnlBasis), not on the leftover per-lot rates that
    would turn every winning day session into a loser."""
    app = _app(tmp_path, "gross.sqlite3")
    store = app.state.store
    consolidator: MemoryConsolidator = app.state.memory_consolidator
    store.ensure_trading_account("tqkq-sim:fee-mask", execution_owner_user_id=USER,
                                 provider_kind="tqkq-sim", display_name="费率遮罩", sidecar_url=None)
    # 残留的高额自定义费率：开/平今各 2000 元每手，一手一开的回合净亏 3,000 元、毛赚 1,000 元。
    app.state.fee_settings.save(USER, {"source": "account", "default": {
        "mode": "per_lot", "open": 2000.0, "closeToday": 2000.0, "closeYesterday": 2000.0}, "overrides": []})
    yesterday = datetime.now(CHINA) - timedelta(days=1)
    day_open = int(yesterday.replace(hour=9, minute=30).timestamp() * 1e9)          # 日盘 5 笔全赢
    night_open = int(yesterday.replace(hour=21, minute=30).timestamp() * 1e9)       # 夜盘 5 笔全亏
    for index in range(5):
        when = day_open + index * 60 * 10**9
        store.upsert_account_trades(
            [fill_at(f"d{index}-open", RB, "BUY", "OPEN", 3200.0, when),
             fill_at(f"d{index}-close", RB, "SELL", "CLOSETODAY", 3210.0, when + 10 * 60 * 10**9)],
            {RB: 100}, trading_account_id="tqkq-sim:fee-mask")
    for index in range(5):
        when = night_open + index * 60 * 10**9
        store.upsert_account_trades(
            [fill_at(f"n{index}-open", RB, "SELL", "OPEN", 3210.0, when),
             fill_at(f"n{index}-close", RB, "BUY", "CLOSETODAY", 3215.0, when + 10 * 60 * 10**9)],
            {RB: 100}, trading_account_id="tqkq-sim:fee-mask")
    try:
        consolidator._stats_for(USER, datetime.now(UTC))
        items = {memory["evidence"]["statKey"]: memory for memory in store.list_memories(USER)}
        assert items["stat:session:day"]["text"] == "近 90 天日盘 5 笔，赢 5 笔（胜率 100%），平均每笔 +1,000 元"
        assert items["stat:session:night"]["text"] == "近 90 天夜盘 5 笔，赢 0 笔（胜率 0%），平均每笔 -500 元"
        assert all(memory["evidence"]["pnlBasis"] == "gross" for memory in store.list_memories(USER))
    finally:
        app.state.store.close()


class FakeAdapter:
    def __init__(self, ops: list[MemoryOp]):
        self.ops, self.calls = ops, 0

    async def complete_structured(self, *, instructions, input_text, schema, timeout_seconds,
                                  usage_user=None, **_):
        self.calls += 1
        return SimpleNamespace(value=schema(ops=self.ops), tokens=123)


async def _extract(store: RuntimeStore, ops: list[MemoryOp], texts: list[str], notes: list[str] | None = None,
                   adapter: FakeAdapter | None = None):
    return await extract_from_words(adapter or FakeAdapter(ops), store, USER, texts, notes or [])


async def test_extract_from_words_applies_valid_operations_and_rejects_the_rest(tmp_path):
    store = RuntimeStore(tmp_path / "extract.sqlite3")
    try:
        habit = user_memory.add(store, USER, layer="profile", text="我只做螺纹", quote=None,
                                products=None, via="settings")
        stats_memory = store.insert_memory(USER, "profile", "常做：螺纹钢（占 64%）", "stats",
                                           {"via": "consolidation", "statKey": "stat:products",
                                            "n": 14, "roundIds": [], "at": "2026-09-28T06:00:00+08:00"}, [])
        texts = ["我只做螺纹和铁矿，别的先不碰", "以后直接帮我下单", "螺纹我最近不做了，改做黄金"]

        # 合法的 add 生效，证据带原话。
        result = await _extract(store, [MemoryOp(op="add", layer="profile", text="只做螺纹和铁矿",
                                                 quote="我只做螺纹和铁矿")], texts)
        assert result["applied"] == 1 and result["tokens"] == 123 and result["rejected"] == {}
        added = store.list_memories(USER)[0]
        assert added["text"] == "只做螺纹和铁矿" and added["source"] == "user"
        assert added["evidence"]["via"] == "consolidation" and added["evidence"]["quote"] == "我只做螺纹和铁矿"

        # 编造原话、授权句、改统计条目、与已有条目重复：逐条拒掉并计数，一条都不落地。
        rejected = await _extract(store, [
            MemoryOp(op="add", layer="lesson", text="编出来的教训", quote="这句话没说过"),
            MemoryOp(op="add", layer="rule", text="以后直接帮我下单", quote="以后直接帮我下单"),
            MemoryOp(op="update", layer="profile", memoryId=stats_memory["id"], text="改成别的",
                     quote="我只做螺纹和铁矿"),
            MemoryOp(op="add", layer="profile", text="我只做螺纹", quote="我只做螺纹和铁矿"),
        ], texts)
        assert rejected["applied"] == 0
        assert rejected["rejected"] == {"原话对不上": 1, user_memory.AUTHORIZATION_MESSAGE: 1,
                                        "目标不是你可改的记忆": 1, "重复": 1}
        assert len(store.list_memories(USER)) == 3  # habit + stats + 上一步的 add

        # 用户改口：旧条目失效（reason 你后来改口了）＋新画像记下。
        changed = await _extract(store, [
            MemoryOp(op="invalidate", layer="profile", memoryId=habit["id"], quote="螺纹我最近不做了，改做黄金"),
            MemoryOp(op="add", layer="profile", text="改做黄金", quote="螺纹我最近不做了，改做黄金"),
        ], texts)
        assert changed["applied"] == 2 and changed["ops"] == {"add": 1, "update": 0, "invalidate": 1}
        rows = {item["id"]: item for item in store.list_memories(USER, include_invalid=True)}
        assert rows[habit["id"]]["status"] == "invalid" and rows[habit["id"]]["invalidReason"] == CHANGED_MIND
        assert rows[habit["id"]]["text"] == "我只做螺纹"  # 失效不改文本
        assert any(item["text"] == "改做黄金" and item["status"] == "active"
                   for item in rows.values())
        assert {change["actor"] for change in store.list_memory_changes(USER)} == {"user", "consolidation"}

        # 没有材料就不调模型；模型失败只报类型名，不抛。
        empty_adapter = FakeAdapter([])
        assert await extract_from_words(empty_adapter, store, USER, [], []) == {"skipped": "no_words"}
        assert empty_adapter.calls == 0

        class Boom:
            async def complete_structured(self, **_):
                raise RuntimeError("model down")
        failed = await extract_from_words(Boom(), store, USER, texts, [])
        assert failed == {"error": "RuntimeError"}
    finally:
        store.close()


def fill_at(trade_id: str, symbol: str, direction: str, offset: str, price: float, when: int,
            volume: int = 1) -> dict:
    return {"tradeId": trade_id, "clientOrderId": f"order-{trade_id}", "symbol": symbol,
            "direction": direction, "offset": offset, "price": price, "volume": volume,
            "tradeDateTime": when, "tradingDay": datetime.fromtimestamp(when / 1e9, CHINA).date().isoformat()}


async def _say(app, text: str, user: str) -> None:
    session = app.state.broker.create_session("local", "fake", user)
    await app.state.broker.publish(session.session_id, kind="message", name="message.completed",
                                   payload={"turnId": "t-user", "role": "user", "text": text})


def _runs(store: RuntimeStore) -> dict[tuple[str, str], str]:
    return {(row["user_id"], row["day"]): row["status"] for row in store.connection.execute(
        "SELECT user_id, day, status FROM user_memory_runs")}


async def test_consolidator_run_claims_each_user_once_and_skips_disabled_users(tmp_path):
    app = _app(tmp_path, "consolidator.sqlite3")
    store = app.state.store
    consolidator: MemoryConsolidator = app.state.memory_consolidator
    adapter = FakeAdapter([])
    consolidator._adapter_for = lambda user_id: adapter
    store.ensure_trading_account("tqkq-sim:trader-2", execution_owner_user_id="trader-2",
                                 provider_kind="tqkq-sim", display_name="二号", sidecar_url=None)
    hour_ago = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1e9)
    store.upsert_account_trades(
        [fill_at("t2-open", RB, "BUY", "OPEN", 3200.0, hour_ago),
         fill_at("t2-close", RB, "SELL", "CLOSE", 3210.0, hour_ago + 600 * 10**9)],
        {RB: 100}, trading_account_id="tqkq-sim:trader-2")
    await _say(app, "我只做螺纹和铁矿", USER)          # 有原话 → 会调一次模型
    await _say(app, "关掉记忆的人说的话", "sleeper")   # chat.memory 关了 → 整个跳过
    store.update_preferences("sleeper", {"chat": {"memory": False}})
    try:
        await consolidator.run()
        day = datetime.now(UTC).astimezone(CHINA).date().isoformat()
        assert _runs(store) == {(USER, day): "done", ("trader-2", day): "done"}
        assert adapter.calls == 1  # 只有说过话的 local-user 走了模型；trader-2 没有原话
        # 同一天再跑：认领表拦下，谁也不重复整理。
        await consolidator.run()
        assert adapter.calls == 1 and len(_runs(store)) == 2
    finally:
        app.state.store.close()


async def test_consolidator_run_survives_one_user_failing(tmp_path):
    app = _app(tmp_path, "failing.sqlite3")
    store = app.state.store
    consolidator: MemoryConsolidator = app.state.memory_consolidator
    consolidator._adapter_for = lambda user_id: FakeAdapter([])
    store.ensure_trading_account("tqkq-sim:trader-2", execution_owner_user_id="trader-2",
                                 provider_kind="tqkq-sim", display_name="二号", sidecar_url=None)
    hour_ago = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1e9)
    store.upsert_account_trades(
        [fill_at("t2-open", RB, "BUY", "OPEN", 3200.0, hour_ago),
         fill_at("t2-close", RB, "SELL", "CLOSE", 3210.0, hour_ago + 600 * 10**9)],
        {RB: 100}, trading_account_id="tqkq-sim:trader-2")
    await _say(app, "我只做螺纹和铁矿", USER)
    original = consolidator._compute_stats

    def flaky(user_id: str, now: datetime):
        if user_id == USER:
            raise RuntimeError("boom")
        return original(user_id, now)
    consolidator._compute_stats = flaky
    try:
        await consolidator.run()
        day = datetime.now(UTC).astimezone(CHINA).date().isoformat()
        assert _runs(store) == {(USER, day): "error", ("trader-2", day): "done"}
    finally:
        app.state.store.close()


async def test_manual_consolidate_route_throttles_and_respects_the_switch(tmp_path):
    from httpx import ASGITransport, AsyncClient
    app = _app(tmp_path, "route.sqlite3")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://runtime",
                               headers=HEADERS) as client:
            first = await client.post("/api/memory/consolidate")
            assert first.status_code == 200, first.text
            body = first.json()
            assert set(body) == {"stats", "words"} and body["words"] == {"skipped": "no_words"}
            again = await client.post("/api/memory/consolidate")
            assert again.status_code == 429 and again.json()["detail"] == "刚整理过，10 分钟后再试"
            assert (await client.put("/api/preferences", json={"chat": {"memory": False}})).status_code == 200
            off = await client.post("/api/memory/consolidate")
            assert off.status_code == 409 and off.json()["detail"] == "记忆已关闭，可在设置 → 记忆打开"
    app.state.store.close()


def test_apply_stats_skips_when_text_and_sample_both_hold(tmp_path):
    """#514-5: 只比文案时，文案没变但 evidence 旧了的条目每天被白改写一遍、审计多一行；
    改成文案和样本数都没变才跳过——文案没变而 n 变了仍刷新一次依据。"""
    store = RuntimeStore(tmp_path / "noop.sqlite3")
    try:
        apply_stats(store, USER, [_stat("stat:products", "常做：螺纹钢（占 64%）", layer="profile", n=14)])
        grown = apply_stats(store, USER, [_stat("stat:products", "常做：螺纹钢（占 64%）", layer="profile", n=15)])
        assert grown == {"added": 0, "updated": 1, "invalidated": 0, "skipped": 0}
        item = next(memory for memory in store.list_memories(USER)
                    if memory["evidence"].get("statKey") == "stat:products")
        assert item["evidence"]["n"] == 15
        audits = len(store.list_memory_changes(USER))
        steady = apply_stats(store, USER, [_stat("stat:products", "常做：螺纹钢（占 64%）", layer="profile", n=15)])
        assert steady == {"added": 0, "updated": 0, "invalidated": 0, "skipped": 1}
        assert len(store.list_memory_changes(USER)) == audits   # 原样不动：不再白写审计
    finally:
        store.close()


class RecordingAdapter:
    """FakeAdapter that also captures the prompt and lets the test act while the model call is
    'in flight' — exactly the window #524-3 is about."""

    def __init__(self, ops: list[MemoryOp], during=None):
        self.ops, self.calls, self.prompts, self.during = ops, 0, [], during

    async def complete_structured(self, *, instructions, input_text, schema, timeout_seconds,
                                  usage_user=None, **_):
        import json
        self.calls += 1
        self.prompts.append(json.loads(input_text))
        if self.during is not None:
            self.during()
        return SimpleNamespace(value=schema(ops=self.ops), tokens=123)


async def test_extract_rechecks_the_switch_and_the_target_after_the_model_returns(tmp_path):
    """#524-3/#524-4: 模型最长等 60 秒，落地前重读记忆开关与记忆清单——用户在这一分钟里
    关掉记忆或删掉目标条目，都不再写；删掉的条目同一批材料也不原样加回来。"""
    store = RuntimeStore(tmp_path / "recheck.sqlite3")
    texts = ["我只做螺纹和铁矿"]
    try:
        habit = user_memory.add(store, USER, layer="profile", text="我只做螺纹", quote=None,
                                products=None, via="settings")

        def turn_off():
            store.update_preferences(USER, {"chat": {"memory": False}})
        off = RecordingAdapter([MemoryOp(op="add", layer="profile", text="只做螺纹和铁矿",
                                         quote="我只做螺纹和铁矿")], during=turn_off)
        assert await extract_from_words(off, store, USER, texts, []) == {"skipped": "disabled"}
        assert [item["id"] for item in store.list_memories(USER)] == [habit["id"]]
        store.update_preferences(USER, {"chat": {"memory": True}})

        def delete_habit():
            user_memory.invalidate(store, USER, habit["id"], "用户删除")
        stale_target = RecordingAdapter([
            MemoryOp(op="update", layer="profile", memoryId=habit["id"], text="改一改",
                     quote="我只做螺纹和铁矿")], during=delete_habit)
        result = await extract_from_words(stale_target, store, USER, texts, [])
        assert result["applied"] == 0 and result["rejected"] == {"目标不是你可改的记忆": 1}

        # 用户删掉的话，同样的材料再整理也不会原样加回来（重复判定连失效条目一起算）。
        revived = await _extract(store, [MemoryOp(op="add", layer="profile", text="我只做螺纹",
                                                  quote="我只做螺纹和铁矿")], texts)
        assert revived["rejected"] == {"重复": 1} and store.list_memories(USER) == []
    finally:
        store.close()


async def test_conflicting_ops_in_one_batch_revalidate_against_the_fresh_list(tmp_path):
    """#524-5: 同一批里失效过的条目不再是后续操作的目标——对着过期快照给失效条目改字的
    操作被拒掉，而不是照写。"""
    store = RuntimeStore(tmp_path / "batch.sqlite3")
    try:
        habit = user_memory.add(store, USER, layer="profile", text="我只做螺纹", quote=None,
                                products=None, via="settings")
        result = await _extract(store, [
            MemoryOp(op="invalidate", layer="profile", memoryId=habit["id"], quote="我只做螺纹和铁矿"),
            MemoryOp(op="update", layer="profile", memoryId=habit["id"], text="失效以后还想改我",
                     quote="我只做螺纹和铁矿"),
        ], ["我只做螺纹和铁矿"])
        assert result["applied"] == 1 and result["rejected"] == {"目标不是你可改的记忆": 1}
        row = store.list_memories(USER, include_invalid=True)[0]
        assert row["status"] == "invalid" and row["text"] == "我只做螺纹"   # 失效后一个字没动
    finally:
        store.close()


async def test_run_records_a_model_failure_retries_it_and_never_leaves_two_runs_overlapping(tmp_path):
    """#524-5/#524-7: 模型提炼失败记 error 而且可以重试（同一天有界次数），成功才记 done、done
    之后不再重复；卡在 running 的旧认领超过半小时可以重抢；每日整理与“现在整理一次”按用户
    串行，同一时刻只有一个在写。"""
    import asyncio
    app = _app(tmp_path, "run-state.sqlite3")
    store = app.state.store
    consolidator: MemoryConsolidator = app.state.memory_consolidator

    class Down:
        async def complete_structured(self, **_):
            raise RuntimeError("model down")
    consolidator._adapter_for = lambda user_id: Down()
    await _say(app, "我只做螺纹和铁矿", USER)
    try:
        await consolidator.run()
        day = datetime.now(UTC).astimezone(CHINA).date().isoformat()
        assert _runs(store) == {(USER, day): "error"}   # 统计照跑、提炼失败如实记下

        # error 不再堵死当天：再跑一次就重试，模型恢复后记 done；done 之后同一天不再整理。
        recovered = FakeAdapter([])
        consolidator._adapter_for = lambda user_id: recovered
        await consolidator.run()
        assert recovered.calls == 1 and _runs(store) == {(USER, day): "done"}
        await consolidator.run()
        assert recovered.calls == 1
        window = datetime.now(UTC).isoformat()
        assert store.claim_memory_run(USER, day, window_start=window) is None

        # 卡在 running 的认领：新鲜的排他，拨回两小时前可以重抢，并换一个新令牌。
        store.connection.execute("UPDATE user_memory_runs SET status = 'running', at = ?"
                                 " WHERE user_id = ? AND day = ?",
                                 ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), USER, day))
        store.connection.commit()
        old = store.connection.execute("SELECT attempt FROM user_memory_runs").fetchone()[0]
        claim = store.claim_memory_run(USER, day, window_start=window)
        assert claim is not None and claim["attempt"] != old
        assert store.claim_memory_run(USER, day, window_start=window) is None

        # 两次手动整理没有交叠：一个进来另一个在门外等。
        events: list[tuple[str, str]] = []
        original = consolidator._consolidate_user

        async def traced(user_id, start, end, now, attempt=None):
            events.append(("enter", user_id))
            await asyncio.sleep(0.02)
            events.append(("exit", user_id))
            return await original(user_id, start, end, now, attempt=attempt)
        consolidator._consolidate_user = traced
        consolidator._adapter_for = lambda user_id: FakeAdapter([])
        await asyncio.gather(consolidator.run_manual(USER), consolidator.run_manual(USER))
        assert events == [("enter", USER), ("exit", USER), ("enter", USER), ("exit", USER)]
    finally:
        app.state.store.close()


async def test_extract_material_keeps_the_newest_notes_and_caps_the_memories(tmp_path):
    """#524-9: 一天写很多条补充时材料取最新的（最新的纠正不能被挤掉）；#524-11: 现有
    记忆进材料封顶 50 条，最旧的先不进。"""
    store = RuntimeStore(tmp_path / "material.sqlite3")
    try:
        texts = ["我只做螺纹和铁矿"]
        for index in range(25):
            store.connection.execute(
                "INSERT INTO trade_notes (trading_account_id, round_id, text, via, user_id, updated_at)"
                " VALUES ('acct', ?, ?, 'typed', ?, ?)",
                (f"r{index}", f"补充第{index}条", USER,
                 (datetime.now(UTC) - timedelta(minutes=30 - index)).isoformat()))
        store.connection.commit()  # synthetic notes are fixture facts before formal memory writes
        adapter = RecordingAdapter([])
        await extract_from_words(adapter, store, USER, texts,
                                 store.notes_between(USER, (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                                                     datetime.now(UTC).isoformat()))
        prompt = adapter.prompts[0]
        assert len(prompt["tradeNotes"]) == 20
        assert prompt["tradeNotes"][0]["text"] == "补充第5条"    # 最旧的几条被截掉
        assert prompt["tradeNotes"][-1]["text"] == "补充第24条"  # 最新的一定在
        assert prompt["memories"] == []

        for index in range(60):
            user_memory.add(store, USER, layer="rule", text=f"约定{index:02d}：" + "细" * 100,
                            quote=None, products=None, via="settings")
        # 同一秒内写入的 updated_at 会撞车：按列表顺序显式排开，先后才是确定的。
        for index, item in enumerate(store.list_memories(USER)):
            store.connection.execute("UPDATE user_memories SET updated_at = ? WHERE id = ?",
                                     ((datetime.now(UTC) - timedelta(minutes=index)).isoformat(), item["id"]))
        store.connection.commit()
        busy = RecordingAdapter([])
        await extract_from_words(busy, store, USER, texts, [])
        memories = busy.prompts[0]["memories"]
        assert len(memories) == 50
        assert memories[0]["text"].startswith("约定59")    # 最新的在前
        assert not any(item["text"].startswith("约定0") for item in memories)   # 最旧的 10 条不进材料
    finally:
        store.close()
