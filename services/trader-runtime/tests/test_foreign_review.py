"""外盘盘点 3A (2026-10-21): the USD sub-account (``paperusd:``) is reviewed and counted as what it is — a CME
account in 美元 — never as a domestic CNY one. Its 今日复盘 comes after CME's 17:00 New York close (not at the
Beijing 15:15 mid-session), its round trips carry the foreign engine's own per-lot USD fee (never the user's
domestic schedule), questions, statistics and the 保本 threshold say or compare 美元 only against 美元, and a
reminder on the 外盘主连 counts for an order on its month. The domestic account's results stay as they were."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from test_daily_review import Model, client
from test_simulation_orders import USER, Gateway, application
from test_strategy_protection import Provider
from trader_runtime import breakeven
from trader_runtime.answer_cards import positions_card
from trader_runtime.breakeven import account_rule
from trader_runtime.daily_review import DailyReviewService, current_trading_day, due_review_day
from trader_runtime.memory_stats import trade_stats
from trader_runtime.review_picks import review_picks
from trader_runtime.review_tags import Reminder, product_key, tag_for
from trader_runtime.strategy_funds import compute_budget
from trader_runtime.strategy_workbench import account_trade_review
from trader_runtime.trading_costs import FeeSettings
from trader_runtime.user_preferences import BreakEvenRule

NS = 1_000_000_000
USD = "paperusd:f1990d30ada5"
GC, GC_NEXT, MAIN = "CME.GCEZ26", "CME.GCEG27", "KQD.m@COMEX.GC"
CME_DAY = "2026-10-21"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def ns(moment: datetime) -> int:
    return int(moment.timestamp() * NS)


class ForeignProvider(Provider):
    """The USD sub-account's provider as the review reads it: the foreign engine's quote carries the contract's
    multiplier and its per-lot USD fee; the catalog is the foreign trade catalog."""

    def market_snapshot(self, turn_id: str, symbol: str) -> dict[str, Any]:
        if not symbol.upper().startswith("CME."):
            return super().market_snapshot(turn_id, symbol)
        return {"symbol": symbol, "volumeMultiple": 100, "commissionPerLot": 2.5, "price": 4185.0, "priceTick": 0.1,
                "dataStatus": "normal", "isCached": False, "ageSeconds": 0}

    def instrument_catalog(self) -> dict[str, Any]:
        return {"items": [{"symbol": GC, "instrumentName": "COMEX黄金2612"}]}


def cme_fill(trade_id: str, direction: str, offset: str, price: float, moment: datetime, *, day: str = CME_DAY) -> dict:
    return {"tradeId": trade_id, "clientOrderId": f"o-{trade_id}", "symbol": GC, "direction": direction, "offset": offset,
            "price": price, "volume": 1, "tradeDateTime": ns(moment), "tradingDay": day}


# -- 1. the review's time ---------------------------------------------------------------------------

def test_the_usd_review_waits_for_the_cme_close_and_names_the_cme_day():
    # Beijing 15:16 on the CME day is 03:16 New York: CME is mid-session, nothing is due.
    assert due_review_day(USD, at("2026-10-21T15:16:00+08:00")) is None
    # 17:15 New York (05:15 Beijing the next morning in US summer time) is due for that CME day …
    assert due_review_day(USD, at("2026-10-21T17:14:00-04:00")) is None
    assert due_review_day(USD, at("2026-10-21T17:15:00-04:00")) == CME_DAY
    # … still that day's after 18:00 rolls trading to the next CME day; and in winter time the same 17:15 local.
    assert due_review_day(USD, at("2026-10-21T20:00:00-04:00")) == CME_DAY
    assert current_trading_day(USD, at("2026-10-21T20:00:00-04:00")) == "2026-10-22"
    assert due_review_day(USD, at("2026-12-01T17:20:00-05:00")) == "2026-12-01"
    # The domestic account is unchanged: 15:15 Beijing, the Beijing date.
    assert due_review_day("paper:f1990d30ada5", at("2026-10-21T15:14:00+08:00")) is None
    assert due_review_day("paper:f1990d30ada5", at("2026-10-21T15:15:00+08:00")) == "2026-10-21"
    assert current_trading_day("paper:f1990d30ada5", at("2026-10-22T02:00:00+08:00")) == "2026-10-22"


async def test_the_usd_review_is_generated_after_the_cme_close_in_dollars_with_the_engine_fee(tmp_path):
    provider = ForeignProvider()
    provider.activity["trades"] = [
        cme_fill("g1", "BUY", "OPEN", 4183.8, at("2026-10-21T09:30:00-04:00")),
        cme_fill("g2", "SELL", "CLOSE", 4182.9, at("2026-10-21T09:31:00-04:00")),
    ]
    app = application(tmp_path / "foreign.sqlite3", Gateway(), ForeignProvider())
    store = app.state.store
    default = app.state.accounts.default
    usd = SimpleNamespace(account_id=USD, provider=provider)
    owners = store.account_owner_ids
    store.account_owner_ids = lambda account_id: [USER] if account_id == USD else owners(account_id)
    app.state.accounts = SimpleNamespace(all=lambda: [default, usd], default=default, get=lambda _: None)
    # A custom domestic schedule must not price the USD contract.
    app.state.fee_settings.save(USER, {"source": "custom", "default": {"mode": "per_lot", "open": 7, "closeToday": 7,
                                                                       "closeYesterday": 7}, "overrides": []})
    model = Model()
    service: DailyReviewService = app.state.daily_reviews
    clock = {"now": at("2026-10-21T15:20:00+08:00")}
    service.now = lambda: clock["now"]
    service._adapter_for = lambda user: model
    http = await client(app)
    try:
        session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
        # The Beijing 15:15 round reviews the domestic account only; CME is mid-session.
        await service.run_once()
        assert store.get_daily_review(user_id=USER, account_id=USD, trading_day=CME_DAY) is None
        clock["now"] = at("2026-10-21T17:16:00-04:00")
        assert await service.run_once() == 1
        review = store.get_daily_review(user_id=USER, account_id=USD, trading_day=CME_DAY)
        assert review["pushedAt"]
        legs = review["payload"]["legs"]
        # Net of the engine's 2.5 USD a lot a side (−90 gross, −5 fees), not the user's 7 元.
        assert legs[1]["pnl"] == -95.0 and legs[1]["pnlBasis"] == "net"
        assert legs[0]["action"] == "买开 COMEX黄金 GCEZ26 × 1 @ 4,183.8"
        # The model is told the amounts are dollars.
        assert model.calls[0]["input"]["currency"] == "USD"
        cards = [event["payload"] for _, event in store.list_events(session_id)
                 if event["name"] == "card.reminder" and event["payload"].get("kind") == "daily_review"]
        assert cards[-1]["title"] == "外盘复盘 · 10-21"
    finally:
        await http.aclose()
        store.close()


# -- 2. fees in the trade review and the journal ----------------------------------------------------

async def test_usd_round_trips_carry_the_engine_fee_not_the_domestic_schedule(tmp_path):
    app = application(tmp_path / "fees.sqlite3", Gateway(), Provider())
    store = app.state.store
    fees = FeeSettings(tmp_path)
    provider = ForeignProvider()
    provider.activity["trades"] = [
        cme_fill("g1", "BUY", "OPEN", 4183.8, at("2026-10-21T09:30:00-04:00")),
        cme_fill("g2", "SELL", "CLOSE", 4182.9, at("2026-10-21T09:31:00-04:00")),
    ]
    try:
        for schedule in ({"source": "account", "default": {}, "overrides": []},
                         {"source": "custom", "default": {"mode": "per_lot", "open": 7, "closeToday": 7, "closeYesterday": 7},
                          "overrides": []}):
            fees.save(USER, schedule)
            review = await account_trade_review(store, provider, fees, USER, symbol=None, days=30, trading_account_id=USD)
            [trade] = review["trades"]
            assert (trade["gross"], trade["fees"], trade["net"]) == (-90.0, 5.0, -95.0)
            assert review["costSource"] == "foreign" and review["currency"] == "USD"
            assert review["estimatedCommission"] == 5.0 and review["estimatedNet"] == -95.0
        # The engine's fee unreadable (an expired contract): gross only, never the domestic 7 元.
        provider.market_snapshot = lambda turn_id, symbol: {"volumeMultiple": 100}
        review = await account_trade_review(store, provider, fees, USER, symbol=None, days=30, trading_account_id=USD)
        assert review["trades"][0]["fees"] is None and review["trades"][0]["net"] is None
        assert review["costSource"] == "account"
    finally:
        store.close()


# -- 3. money in 美元, foreign products by product ---------------------------------------------------

def test_the_review_question_says_dollars_for_a_foreign_trade():
    now = ns(at("2026-10-21T18:00:00-04:00"))
    trade = {"id": "r1", "symbol": GC, "direction": "long", "gross": -90.0, "net": -95.0, "tradingDay": CME_DAY,
             "openTime": now - 3600 * NS, "closeTime": now - 3000 * NS, "holdingMinutes": 10, "note": None, "deskView": None}
    [pick] = review_picks([trade], scope="day", now_ns=now)["picks"]
    assert pick["name"] == "COMEX黄金2612" and pick["question"].startswith("COMEX黄金2612这笔做多，亏了 95 美元")


def test_the_breakeven_amount_threshold_in_yuan_is_never_compared_with_dollars():
    rule = BreakEvenRule(mode="amount", amount=1000, multiple=1.5)
    assert account_rule(rule, USD).mode == "multiple" and account_rule(rule, USD).multiple == 1.5
    assert account_rule(rule, "paper:f1990d30ada5") == rule


def test_a_dollar_threshold_judges_the_usd_sub_account_in_dollars_and_leaves_the_yuan_account_alone():
    """所有者 2026-10-21: the USD sub-account has its own 美元 threshold; without it, the multiple as before."""
    rule = BreakEvenRule(mode="amount", amount=1000, amountUsd=300, multiple=1.5)
    usd = account_rule(rule, USD)
    assert (usd.mode, usd.amount) == ("amount", 300)
    assert account_rule(rule, "paper:f1990d30ada5") == rule  # 人民币账户照旧按 1000 元
    position = {"symbol": GC, "direction": "long", "volume": 1, "openPrice": 4000.0, "unrealizedPnl": 250.0}
    rows = [{"id": "s1", "status": "ARMED", "trigger_price": "3990", "take_profit_price": None}]
    market = {"priceTick": "0.1", "price": "4002.5"}
    assert breakeven.judge(position, rows, market, usd) is None  # 250 美元 < 300 美元
    question = breakeven.judge({**position, "unrealizedPnl": 320.0}, rows, market, usd)
    assert question is not None
    question["unit"] = "美元"
    _, text = breakeven.card_for(notice_id="breakeven-x", symbol=GC, name="COMEX黄金2612", question=question,
                                 at="2026-10-21T10:00:00+00:00")
    assert "浮盈已到 320 美元（你设的 300 美元）" in text


def round_trip(index: int, symbol: str, pnl: float, *, source: str = "manual") -> dict:
    opened = ns(at(f"2026-10-{index + 1:02d}T22:00:00+08:00"))   # all at night, Beijing time
    return {"id": f"t{index}", "symbol": symbol, "direction": "long", "volume": 1, "openTime": opened,
            "closeTime": opened + 600 * NS, "gross": pnl, "net": pnl, "fees": 0.0, "source": source,
            "holdingMinutes": 10, "deskView": None}


def test_memory_stats_group_foreign_months_by_product_and_never_mix_dollars_into_yuan():
    # Five losing COMEX gold card trades over two months; rebar: five manual winners and two losing card trades.
    rounds = ([round_trip(i, GC if i % 2 else GC_NEXT, -100.0, source="card") for i in range(5)]
              + [round_trip(i + 5, "SHFE.rb2701", 400.0) for i in range(5)]
              + [round_trip(i + 10, "SHFE.rb2701", -200.0, source="card") for i in range(2)])
    stats = {entry["key"]: entry for entry in trade_stats(rounds, names={}, now_ns=ns(at("2026-10-20T00:00:00+08:00")))}
    # One gold product across months (not GCEZ / GCEG), its average in 美元.
    assert stats["stat:product:COMEX黄金"]["n"] == 5 and stats["stat:product:COMEX黄金"]["text"].endswith("平均每笔 -100 美元")
    assert stats["stat:product:rb"]["text"].endswith("平均每笔 +229 元")
    # A group with both currencies states each average apart, never one sum of yuan and dollars.
    assert stats["stat:source:card"]["text"].endswith("平均每笔 -200 元、-100 美元")
    # The domestic night session counts domestic trades only (CME has one session a day).
    assert stats["stat:session:night"]["n"] == 7


def test_a_reminder_on_the_foreign_main_counts_for_an_order_on_its_month():
    assert product_key(GC) == product_key(GC_NEXT) == product_key(MAIN) == "COMEX.GC"
    assert product_key("SHFE.rb2701") == "SHFE.RB"
    reminder = Reminder(ns(at("2026-10-21T09:25:00-04:00")), "price_alert", "到价", frozenset({product_key(MAIN)}))
    tag, _ = tag_for(source="manual", placed_ns=ns(at("2026-10-21T09:30:00-04:00")), symbol=GC, reminders=[reminder])
    assert tag == "remind"


def test_other_amount_lines_of_a_usd_account_say_dollars():
    funds = compute_budget(equity=1000.0, equity_percent=10, margin_per_lot=500.0, used_lots=0, pending_lots=0,
                           available=1000.0, fee_per_lot=2.5, max_lots=None, unit="美元")
    assert funds["unavailableReason"] == "剩余额度 100.00 美元 ÷ 每手保证金 500.00 美元，不足 1 手，这次不开。"
    card = positions_card({"positions": [{"symbol": GC, "instrumentId": "GCEZ26", "direction": "long", "volume": 1,
                                          "unrealizedPnl": 120.0}]}, account_word="本站模拟 · 美元")
    assert card["figures"][0]["unit"] == "美元"


def test_a_foreign_review_push_opens_on_its_usd_sub_account():
    """2026-10-21：外盘复盘的推送点开要看到美元子账户那天的复盘，不是当前（人民币）账户的；链接带上账户。"""
    from trader_runtime.web_push import review_message
    foreign = review_message(trading_day="2026-10-21", legs=1, planned=0, pnl="-95", account_id="paperusd:f1990d30ada5")
    assert foreign.url == "?open=review:2026-10-21:paperusd:f1990d30ada5"
    domestic = review_message(trading_day="2026-10-21", legs=1, planned=0, pnl="-95", account_id="paper:f1990d30ada5")
    assert domestic.url == "?open=review:2026-10-21"
