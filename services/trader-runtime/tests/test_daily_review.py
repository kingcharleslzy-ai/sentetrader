"""今日复盘 (2026-10-06). Protects the owner's three decisions: the 计划内 / 提醒后 / 临时 tag is decided by the
fill's source and the reminders' times in the owner's priority (never by a model); a 习惯 observation needs
two supporting fills and reaches the memory only when the user says 记住 (像授权的话不记, 不对 is never offered
again); the review is generated and delivered once per user × account × trading day after 15:15 and only when
the day has fills. Also: no unchecked number from the model is ever shown — it falls back to the deterministic
sentence — and the lock-screen line drops its figures when amounts are hidden."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from test_simulation_orders import USER, Gateway, application
from test_strategy_protection import Provider
from test_web_push import APPLE, body, configure
from trader_runtime import trade_autopsy
from trader_runtime.daily_review import DailyReviewService, build_legs, numbers_in, readable_prices
from trader_runtime.review_tags import Reminder, product_key, tag_for

NS = 1_000_000_000
DAY = "2026-10-09"
RB, MA = "SHFE.rb2701", "CZCE.MA701"


def bj(hour: int, minute: int, second: int = 0, day: str = DAY) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:{second:02d}+08:00")


def ns(moment: datetime) -> int:
    return int(moment.timestamp() * NS)


# -- the tag rule --------------------------------------------------------------------------------------

PLACED = ns(bj(10, 48))
MOVEMENT_RB = Reminder(ns(bj(10, 47)), "movement", "盘中异动：螺纹钢", frozenset({"SHFE.RB"}))


@pytest.mark.parametrize(("source", "reminders", "expected"), [
    # ① strategy / 止损止盈 / 自主执行 are 计划内 even right after a reminder about the same product.
    ("strategy", [MOVEMENT_RB], "plan"), ("stop", [MOVEMENT_RB], "plan"), ("ai", [MOVEMENT_RB], "plan"),
    # ② the user's own order within 15 minutes after a same-product reminder: the ticket, or an AI card
    # (the 平一半 card the reminder's button asks for is one of these).
    ("manual", [MOVEMENT_RB], "remind"), ("card", [MOVEMENT_RB], "remind"),
    ("manual", [Reminder(PLACED - (14 * 60 + 59) * NS, "price_alert", "到价", frozenset({"SHFE.RB"}))], "remind"),
    ("manual", [Reminder(PLACED - 15 * 60 * NS, "price_alert", "到价", frozenset({"SHFE.RB"}))], "remind"),
    ("manual", [Reminder(PLACED - (15 * 60 + 1) * NS, "price_alert", "到价", frozenset({"SHFE.RB"}))], "adhoc"),
    # A reminder about another product, or one that came after the order, does not count.
    ("manual", [Reminder(ns(bj(10, 47)), "movement", "盘中异动：甲醇", frozenset({"CZCE.MA"}))], "adhoc"),
    ("manual", [Reminder(PLACED + NS, "movement", "盘中异动：螺纹钢", frozenset({"SHFE.RB"}))], "adhoc"),
    # 风险度 is about the whole account: every product's order after it is 提醒后.
    ("manual", [Reminder(ns(bj(10, 40)), "risk", "风险度提醒", None)], "remind"),
    # ③ an AI card the user confirmed, no reminder → 计划内; ④ the rest by hand → 临时.
    ("card", [], "plan"), ("manual", [], "adhoc"),
])
def test_the_owner_tag_rule_in_priority_order(source, reminders, expected):
    tag, reminder = tag_for(source=source, placed_ns=PLACED, symbol=RB, reminders=reminders)
    assert tag == expected
    assert (reminder is not None) == (expected == "remind")


def test_products_compare_across_months_and_main_aliases():
    assert product_key("SHFE.rb2701") == product_key("SHFE.RB2610") == product_key("KQ.m@SHFE.rb") == "SHFE.RB"
    assert product_key("CZCE.MA701") != product_key("SHFE.rb2701")


# -- the day's 笔 --------------------------------------------------------------------------------------

def fill(trade_id, symbol, direction, offset, price, moment, *, order, volume=1, day=DAY):
    return {"tradeId": trade_id, "clientOrderId": order, "symbol": symbol, "direction": direction, "offset": offset,
            "price": price, "volume": volume, "tradeDateTime": ns(moment), "tradingDay": day}


def test_fills_become_one_leg_per_order_with_the_round_trip_pnl_and_the_night_on_the_next_day():
    fills = [
        # Last night's session belongs to today (the provider's own tradingDay): it is today's first 笔.
        fill("n1", RB, "BUY", "OPEN", 3140.0, bj(21, 5, day="2026-10-08"), order="o-night"),
        # One order filled in two tranches: one 笔 of 3 lots at the volume-weighted price.
        fill("p1", MA, "SELL", "OPEN", 2420.0, bj(9, 10), order="o-ma", volume=1),
        fill("p2", MA, "SELL", "OPEN", 2423.0, bj(9, 11), order="o-ma", volume=2),
        fill("c1", RB, "SELL", "CLOSE", 3188.0, bj(14, 22), order="o-close"),
        # Tonight's session is tomorrow's: not in today's review.
        fill("n2", RB, "BUY", "OPEN", 3190.0, bj(21, 3), order="o-tonight", day="2026-10-12"),
    ]
    rounds = [{"closeTradeId": "c1", "net": None, "gross": 48000.0}]
    sources = {"o-close": {"source": "stop", "reason": "保护性止盈触发：3188"}, "o-ma": {"source": "card", "reason": "回踩不破，按约定进"}}
    legs = build_legs(fills, trading_day=DAY, sources=sources, placed={}, reminders=[], rounds=rounds,
                      names={RB.upper(): "螺纹钢", MA.upper(): "甲醇"})
    assert [(leg["id"], leg["orderId"], leg["volume"]) for leg in legs] == [("L1", "o-night", 1), ("L2", "o-ma", 3), ("L3", "o-close", 1)]
    assert legs[1]["price"] == 2422.0 and legs[1]["action"] == "卖开 甲醇 MA701 × 3 @ 2,422"
    assert [leg["tag"] for leg in legs] == ["adhoc", "plan", "plan"]
    # A price in the reason reads like every other figure on the page; a contract code stays as it is.
    assert legs[0]["reason"] == "没有记下理由" and legs[2]["reason"] == "保护性止盈触发：3,188"
    assert readable_prices("回踩 RB2701 的 3150 支撑，10:47 后 2026-10-09 加到 3150.5") == "回踩 RB2701 的 3,150 支撑，10:47 后 2026-10-09 加到 3,150.5"
    assert numbers_in("止盈 3,188") == numbers_in("止盈 3188") == {3188.0}
    assert legs[2]["action"].startswith("平多 螺纹钢 RB2701") and legs[2]["pnl"] == 48000.0 and legs[2]["pnlBasis"] == "gross"
    assert legs[0]["pnl"] is None


# -- generation, delivery, the observation -------------------------------------------------------------

class Model:
    """The user's model adapter as the review reaches it: answers what the test sets, or raises."""

    agent_authenticated = True

    def __init__(self):
        self.answer: dict | Exception = {"summary": "今天的成交都按计划。"}
        self.calls: list[dict] = []

    async def complete_structured(self, *, instructions, input_text, schema, timeout_seconds, model=None, usage_user=None):
        self.calls.append({"input": json.loads(input_text), "usage_user": usage_user})
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(value=schema.model_validate(self.answer), tokens=120)


def seed_orders(app):
    """An AI card confirmed before any reminder (计划内), the 平一半 card one minute after a 盘中异动 on
    螺纹 (提醒后), and a manual ticket order (临时)."""
    store = app.state.store
    session_id = app.state.broker.create_session("local", "native", USER).session_id
    for proposal_id, order, placed in (("p-open", "cli-open", bj(9, 3)), ("p-half", "cli-half", bj(10, 48))):
        store.create(proposal_id=proposal_id, session_id=session_id, proposal_type="trade", status="executed",
                     payload={"symbol": RB, "origin": "ai", "reason": "回踩支撑，按约定分两笔进" if order == "cli-open" else "把螺纹平一半"},
                     user_id=USER)
        store.connection.execute(
            "INSERT INTO tqkq_sim_order_links (id, user_id, proposal_id, client_order_id, request_json, submit_phase,"
            " cancel_phase, created_at, updated_at) VALUES (?, ?, ?, ?, '{}', 'CONFIRMED', 'NONE', ?, ?)",
            (f"link-{proposal_id}", USER, proposal_id, order, placed.astimezone(UTC).isoformat(), placed.astimezone(UTC).isoformat()))
    store.connection.commit()
    store.claim_push_notice(user_id=USER, key=f"market:{RB}:{ns(bj(10, 45)) // NS // 300}:上涨",
                            at=bj(10, 47).astimezone(UTC).isoformat(), text="盘中异动：螺纹钢 近5分钟上涨12跳")


def day_fills():
    return [
        fill("t1", RB, "BUY", "OPEN", 3150.0, bj(9, 3, 20), order="cli-open", volume=2),
        fill("t2", RB, "SELL", "CLOSETODAY", 3170.0, bj(10, 48, 30), order="cli-half", volume=1),
        fill("t3", RB, "SELL", "CLOSETODAY", 3160.0, bj(13, 40), order="ticket-1", volume=1),
    ]


async def client(app):
    http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Origin": "http://127.0.0.1:5173"})
    assert (await http.post("/api/auth/local", headers={"Authorization": "Bearer test-local-token"})).status_code == 200
    return http


def setup(tmp_path, *, fills=True):
    provider = Provider()
    provider.day = DAY
    provider.activity["trades"] = day_fills() if fills else []
    app = application(tmp_path / "review.sqlite3", Gateway(), provider)
    store = app.state.store
    seed_orders(app)
    store.upsert_account_daily(DAY, {"equity": 1_002_100, "staticBalance": 1_000_000, "closeProfit": 2112,
                                     "positionProfit": 0, "commission": 12, "todayNetPnl": 2100},
                               trading_account_id=app.state.accounts.default.account_id)
    model = Model()
    service: DailyReviewService = app.state.daily_reviews
    clock = {"now": bj(15, 10)}
    service.now = lambda: clock["now"]
    service._adapter_for = lambda user: model
    return app, provider, model, service, clock


def review_messages(app, session_id):
    return [event for _, event in app.state.store.list_events(session_id)
            if str(event.get("payload", {}).get("turnId", "")).startswith("daily-review-")]


async def test_generated_once_after_1515_tagged_by_rule_and_pushed_once(tmp_path):
    app, provider, model, service, clock = setup(tmp_path)
    sender = configure(app)
    http = await client(app)
    session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
    assert (await http.put("/api/app/push/subscription", json=body("device-a-0001", APPLE))).status_code == 200
    account = app.state.accounts.default.account_id
    assert app.state.store.account_owner_ids(account) == [USER]
    try:
        # Before 15:15 nothing is generated.
        assert await service.run_once() == 0
        assert (await http.get("/api/account/daily-review")).json()["review"] is None
        clock["now"] = bj(15, 16)
        assert await service.run_once() == 1
        review = (await http.get("/api/account/daily-review")).json()["review"]
        assert [(leg["clock"], leg["tag"]) for leg in review["legs"]] == [("09:03", "plan"), ("10:48", "remind"), ("13:40", "adhoc")]
        assert review["legs"][1]["reason"].startswith("10:47 盘中异动后") and review["legs"][1]["reminder"]["text"].startswith("盘中异动：螺纹钢")
        assert review["stats"] == {"legs": 3, "planned": 1, "remind": 1, "adhoc": 1, "closes": 2, "wins": 2}
        assert review["dayPnl"] == 2100 and review["summary"] == {
            "text": "今天的成交都按计划。", "source": "model",
            "basis": "依据这 3 笔成交、当时的提醒和你的约定；数字都可在下面的成交里核对"}
        assert review["rules"][0].startswith("计划内：") and model.calls[0]["usage_user"] == USER
        # The model saw the day's facts only: legs, the reminders, the counts — never an account number.
        facts = model.calls[0]["input"]
        assert [leg["id"] for leg in facts["legs"]] == ["L1", "L2", "L3"] and "accountId" not in facts
        # One message + card in the latest conversation, the card's 看复盘 opens the review on that day.
        messages = review_messages(app, session_id)
        cards = [event["payload"] for event in messages if event["name"] == "card.reminder"]
        assert len(messages) == 2 and cards[0]["kind"] == "daily_review" and cards[0]["title"] == "今日复盘 · 10-09"
        assert cards[0]["actions"] == [{"kind": "ui", "label": "看复盘", "changes": {"reviewDay": DAY}}]
        assert [(tag["tag"], tag["count"]) for tag in cards[0]["tags"]] == [("plan", 1), ("remind", 1), ("adhoc", 1)]
        await app.state.push.drain()
        assert [payload for _, payload in sender.sent] == [{
            "title": "今天的复盘好了", "body": "3 笔成交，1 笔计划内，当日 +2,100。点开看每一笔。",
            "tag": f"review-{account}-{DAY}", "url": f"?open=review:{DAY}", "kind": "review"}]
        # A second round and a restarted service (a blue-green sibling) never deliver again.
        assert await service.run_once() == 0
        fresh = DailyReviewService(app, now=lambda: bj(15, 30), adapter_for=lambda user: model)
        assert await fresh.run_once() == 0
        await app.state.push.drain()
        assert len(sender.sent) == 1 and len(review_messages(app, session_id)) == 2
        # 重新生成 overwrites and never pushes.
        model.answer = {"summary": "今天三笔里一笔在计划内。"}
        regenerated = await http.post("/api/account/daily-review/regenerate", json={"tradingDay": DAY})
        assert regenerated.status_code == 200 and regenerated.json()["review"]["summary"]["text"] == "今天三笔里一笔在计划内。"
        assert regenerated.json()["days"] == [DAY]
        await app.state.push.drain()
        assert len(sender.sent) == 1 and len(review_messages(app, session_id)) == 2
        assert (await http.post("/api/account/daily-review/regenerate", json={})).status_code == 429
        # The round trips carry each leg's tag too (平仓记录).
        trades = (await http.get("/api/trade-review")).json()["trades"]
        assert {(trade["openTag"], trade["closeTag"]) for trade in trades} == {("plan", "remind"), ("plan", "adhoc")}
    finally:
        await http.aclose()
        app.state.store.close()


async def test_a_day_without_fills_gets_no_review_and_no_push(tmp_path):
    app, provider, model, service, clock = setup(tmp_path, fills=False)
    sender = configure(app)
    http = await client(app)
    try:
        clock["now"] = bj(15, 20)
        assert await service.run_once() == 0
        assert (await http.get("/api/account/daily-review")).json() == {
            "review": None, "days": [], "generatedAt": None, "generateAt": "15:15", "currency": "CNY", "rules": (await http.get("/api/account/daily-review")).json()["rules"]}
        refused = await http.post("/api/account/daily-review/regenerate", json={"tradingDay": DAY})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "no_fills"
        await app.state.push.drain()
        assert sender.sent == [] and model.calls == []
    finally:
        await http.aclose()
        app.state.store.close()


async def test_an_empty_activity_that_is_not_current_does_not_cost_the_day_its_review(tmp_path):
    """2026-10-12: a connecting/stale activity read that shows no trades is not "no fills" — and a current
    read without fills is not final either: the review comes once the fills are readable."""
    app, provider, model, service, clock = setup(tmp_path, fills=False)
    try:
        clock["now"] = bj(15, 20)
        provider.activity.update(dataStatus="connecting", ordersInitialized=False)
        assert await service.run_once() == 0
        provider.activity.update(dataStatus="normal", ordersInitialized=True)  # current, still empty
        assert await service.run_once() == 0
        assert model.calls == []
        provider.activity["trades"] = day_fills()  # the fills were only late
        clock["now"] = bj(15, 21)
        assert await service.run_once() == 1
        assert app.state.store.get_daily_review(user_id=USER, account_id=app.state.accounts.default.account_id,
                                                trading_day=DAY)["pushedAt"]
    finally:
        app.state.store.close()


async def test_the_review_is_generated_once_and_the_push_carries_the_archived_text(tmp_path):
    """2026-10-12: the 15:15 run claims the review before the model call. While another process (the other
    slot) holds the claim, this one neither calls the model nor pushes; once that process saved the review,
    this one delivers exactly the saved text instead of generating its own."""
    app, provider, model, service, clock = setup(tmp_path)
    http = await client(app)
    store, account = app.state.store, app.state.accounts.default
    try:
        session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
        clock["now"] = bj(15, 16)
        stale_before = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        assert store.claim_daily_review_generation(user_id=USER, account_id=account.account_id, trading_day=DAY,
                                                   stale_before=stale_before)  # the other slot is generating
        assert await service.run_once() == 0 and model.calls == []
        model.answer = {"summary": "另一槽生成的复盘。"}
        other = DailyReviewService(app, now=lambda: clock["now"], adapter_for=lambda user: model)
        saved = await other.generate(USER, account, DAY, reason="auto")  # it saved, then lost the lease
        model.answer = {"summary": "这一槽不该再生成。"}
        assert await service.run_once() == 1
        assert len(model.calls) == 1  # generated once, by the claimer
        archived = store.get_daily_review(user_id=USER, account_id=account.account_id, trading_day=DAY)
        assert archived["pushedAt"] and archived["payload"]["summary"]["text"] == saved["summary"]["text"] == "另一槽生成的复盘。"
        cards = [event["payload"] for event in review_messages(app, session_id) if event["name"] == "card.reminder"]
        assert [card["summary"] for card in cards] == ["另一槽生成的复盘。"]
        # A claim whose process died mid-call is taken over after GENERATION_CLAIM_SECONDS.
        assert store.claim_daily_review_generation(user_id=USER, account_id="acct-x", trading_day=DAY,
                                                   stale_before=stale_before)
        assert not store.claim_daily_review_generation(user_id=USER, account_id="acct-x", trading_day=DAY,
                                                       stale_before=stale_before)
        assert store.claim_daily_review_generation(user_id=USER, account_id="acct-x", trading_day=DAY,
                                                   stale_before=(datetime.now(UTC) + timedelta(seconds=1)).isoformat())
    finally:
        await http.aclose()
        store.close()


@pytest.mark.parametrize("answer", [
    {"summary": "今天赚了 9,999，三笔都很好。"},                      # a number the facts do not hold
    {"summary": "今天三笔，明天建议继续买入螺纹。"},                    # trading advice
    {"summary": "今天三笔，螺纹预计还会涨。"},                          # a forecast
    RuntimeError("model_request_failed"),                                # no model answer at all
])
async def test_an_unchecked_model_answer_falls_back_to_the_deterministic_sentence(tmp_path, answer):
    app, provider, model, service, clock = setup(tmp_path)
    model.answer = answer
    try:
        payload = await service.generate(USER, app.state.accounts.default, DAY, reason="manual")
        assert payload["summary"]["text"] == "今天 3 笔里 1 笔在计划内，1 笔是提醒后下的，1 笔临时。"
        assert payload["summary"]["source"] == "template" and payload["observation"] is None
    finally:
        app.state.store.close()


async def test_without_a_model_or_quota_the_review_still_comes_with_the_template(tmp_path):
    app, provider, model, service, clock = setup(tmp_path)
    service._adapter_for = lambda user: None
    try:
        payload = await service.generate(USER, app.state.accounts.default, DAY, reason="auto")
        assert payload["summary"]["source"] == "template" and payload["summary"]["basis"] == "按成交来源和提醒时间统计，没有用模型"
    finally:
        app.state.store.close()


# -- 交易验尸 (2026-10-25): the program's checks, the question, 本周最该改的一件事 -------------------------

def leg(id_, side, price, clock, *, pnl=None, source="manual", trades=None, order=None):
    return {"id": id_, "side": side, "price": price, "time": f"{DAY}T{clock}+08:00", "symbol": RB, "pnl": pnl,
            "source": source, "tradeIds": trades or [f"t-{id_}"], "orderId": order or f"o-{id_}"}


def stop(trigger, *, take=None, created="09:00", updated="15:00", armed=True, close_order=""):
    return {"symbol": RB.upper(), "direction": "long", "trigger": trigger, "take": take, "armed": armed,
            "closeOrderId": close_order, "created": ns(bj(*map(int, created.split(":")))),
            "updated": ns(bj(*map(int, updated.split(":"))))}


def test_each_leg_is_checked_against_the_users_own_plan():
    legs = [
        leg("L1", "买开", 3150.0, "09:01"),
        # Closed by hand while the stop (3,100) and the take (3,250) were both still armed and untouched.
        leg("L2", "平多", 3170.0, "09:30", pnl=200.0),
        leg("L3", "买开", 3160.0, "10:00", source="card"),
        # A loss that went 10 past the planned stop; a stop's own close is never 提前离场.
        leg("L4", "平多", 3090.0, "10:30", pnl=-700.0, source="stop", order="o-stop"),
        leg("L5", "买开", 3100.0, "11:00"),
        # A losing round trip that never had a stop.
        leg("L6", "平多", 3080.0, "13:40", pnl=-200.0),
        # Two losses in a row, then the user opens again; the strategy's own entry is not judged.
        leg("L7", "买开", 3085.0, "14:00"),
        leg("L8", "买开", 3085.0, "14:01", source="strategy"),
    ]
    rounds = [{"id": "r1", "openTradeId": "t-L1", "closeTradeId": "t-L2", "openTime": ns(bj(9, 1))},
              {"id": "r2", "openTradeId": "t-L3", "closeTradeId": "t-L4", "openTime": ns(bj(10, 0)), "playbookName": "回踩",
               "playbookId": "pb1"},
              {"id": "r3", "openTradeId": "t-L5", "closeTradeId": "t-L6", "openTime": ns(bj(11, 0))}]
    # The first stop ended with the close it guarded (09:30); the second was the one that closed L4.
    stops = [stop(3100.0, take=3250.0, created="09:02", updated="09:30", armed=False),
             stop(3100.0, created="10:01", updated="10:30", armed=False, close_order="o-stop")]
    trade_autopsy.check_legs(legs, rounds=rounds, sources={}, stops=stops, keeps_playbooks=True)
    codes = {item["id"]: [check["code"] for check in item["checks"]] for item in legs}
    assert codes == {"L1": ["untagged"], "L2": ["early_exit"], "L3": [], "L4": ["stop_beyond"], "L5": ["untagged"],
                     "L6": ["no_stop"], "L7": ["after_losses", "untagged"], "L8": []}
    assert legs[1]["checks"][0]["text"] == "止损 3,100、止盈 3,250 都还没到，就手动平了"
    assert legs[3]["checks"][0]["text"] == "计划止损 3,100，实际 3,090 出场，多走了 10"
    # A later tag in the review counts as the setup; a question only where a round trip can take the answer.
    assert legs[2]["playbookName"] == "回踩" and legs[2]["roundId"] == "r2"
    assert legs[1]["question"] == "止损和目标都没到，当时为什么先平了？" and legs[6]["question"] is None
    # Without any setups kept, nothing is 没标打法.
    trade_autopsy.check_legs(legs, rounds=rounds, sources={}, stops=stops, keeps_playbooks=False)
    assert all(check["code"] != "untagged" for item in legs for check in item["checks"])


def test_the_weeks_lesson_comes_on_its_last_trading_day_from_the_most_frequent_check():
    assert trade_autopsy.is_week_end(DAY, "2026-10-12") and not trade_autopsy.is_week_end("2026-10-08", DAY)
    # A Friday holiday makes Thursday the week's last day; without a calendar, Friday.
    assert trade_autopsy.is_week_end("2026-10-08", "2026-10-12") and trade_autopsy.is_week_end(DAY, None)
    today = [{"id": "L1", "tradeIds": ["t1"], "checks": [{"code": "untagged"}]},
             {"id": "L2", "tradeIds": ["t2"], "checks": [{"code": "early_exit"}]}]
    history = [("2026-10-08", {"id": "L1", "tradeIds": ["a"], "checks": [{"code": "early_exit"}]}),
               ("2026-10-02", {"id": "L1", "tradeIds": ["old"], "checks": [{"code": "untagged"}] * 5})]  # last week
    lesson = trade_autopsy.week_lesson(DAY, today, history)
    assert (lesson["code"], lesson["count"], lesson["legs"], lesson["tradeIds"]) == (
        "early_exit", 2, ["L2", "2026-10-08/L1"], ["t2", "a"])
    assert trade_autopsy.week_lesson(DAY, [{"id": "L1", "checks": []}], []) is None


def seed_playbook(app, **fields):
    return app.state.store.create_playbook(user_id=USER, max_count=20, fields={
        "name": "回踩", "entryRules": "回踩不破再进", "exitRules": "", "stopRules": "", "strategyId": None, **fields})


def arm_stop(app):
    """The user's stop on the 螺纹 long, armed at 09:05 and still armed: both closes are 提前离场."""
    store, account = app.state.store, app.state.accounts.default.account_id
    at = bj(9, 5).astimezone(UTC).isoformat()
    session_id = app.state.broker.create_session("local", "native", USER).session_id
    store.connection.execute(
        "INSERT OR IGNORE INTO ai_execution_authorizations (id, user_id, session_id, status, symbols_json, permissions_json,"
        " max_order_volume, max_orders, expires_at, created_at, updated_at, trading_account_id)"
        " VALUES ('auth-review', ?, ?, 'ACTIVE', '[]', '[]', 1, 1, ?, ?, ?, ?)", (USER, session_id, at, at, at, account))
    store.connection.execute(
        "INSERT INTO ai_stop_orders (id, user_id, authorization_id, session_id, symbol, position_direction, position_bucket,"
        " volume, trigger_price, price_intent, status, created_at, updated_at, trading_account_id)"
        " VALUES ('stop-review', ?, 'auth-review', ?, ?, 'long', 'today', 2, '3100', 'passive', 'ARMED', ?, ?, ?)",
        (USER, session_id, RB, at, at, account))
    store.connection.commit()


def disarm_stop(app):
    app.state.store.connection.execute("DELETE FROM ai_stop_orders WHERE id = 'stop-review'")
    app.state.store.connection.commit()


UNTAGGED, EARLY = trade_autopsy.LESSONS["untagged"], trade_autopsy.LESSONS["early_exit"]


async def test_the_review_carries_each_legs_checks_its_question_and_the_weeks_lesson(tmp_path):
    app, provider, model, service, clock = setup(tmp_path)
    seed_playbook(app)
    arm_stop(app)
    try:
        payload = await service.generate(USER, app.state.accounts.default, DAY, reason="manual")
        legs = payload["legs"]
        assert [[check["code"] for check in item["checks"]] for item in legs] == [["untagged"], ["early_exit"], ["early_exit"]]
        assert [item["question"] for item in legs] == [
            "这笔用的是哪条交易笔记？", "止损和目标都没到，当时为什么先平了？", "止损和目标都没到，当时为什么先平了？"]
        assert all(item["roundId"] for item in legs)
        # The model is shown the checks to put them in plain words.
        assert model.calls[0]["input"]["legs"][1]["checks"] == ["止损 3,100 都还没到，就手动平了"]
        # 2026-10-09 is a Friday: the week's lesson is the check that came up most.
        observation = payload["observation"]
        assert (observation["kind"], observation["code"], observation["count"], observation["text"]) == (
            "weekly", "early_exit", 2, EARLY)
        assert observation["tradeIds"] == ["t2", "t3"] and observation["status"] == "pending"
    finally:
        app.state.store.close()


@pytest.mark.parametrize(("fits", "why", "judged"), [
    (False, "09:03 买开时没有回踩，理由写的是按约定分两笔进", True),
    (True, "回踩到 3,140 才进", False),           # a number the facts do not hold: unjudged
])
async def test_a_setup_in_words_is_judged_by_the_model_and_checked(tmp_path, fits, why, judged):
    app, provider, model, service, clock = setup(tmp_path)
    app.state.store.tag_proposal_playbook("p-open", seed_playbook(app)["id"], user_id=USER)
    model.answer = {"summary": "今天的成交都按计划。", "entryFits": [{"leg": "L1", "fits": fits, "why": why},
                                                                    {"leg": "L2", "fits": False, "why": "平仓笔不判断"}]}
    try:
        payload = await service.generate(USER, app.state.accounts.default, DAY, reason="manual")
        assert model.calls[0]["input"]["legs"][0]["playbook"] == {"name": "回踩", "entryRules": "回踩不破再进"}
        first, second = payload["legs"][0], payload["legs"][1]
        assert ("entryFit" in first) == judged and "entryFit" not in second
        if judged:
            assert first["entryFit"] == {"fits": False, "basis": "model", "text": why}
            assert [check["code"] for check in first["checks"]] == ["entry_miss"]
            assert first["question"] == "入场不合交易笔记的规则，当时看到了什么？"
    finally:
        app.state.store.close()


def seen(app, verdict: str, **extra) -> dict:
    """An answer naming the observation version the user is looking at (#524)."""
    record = app.state.store.get_daily_review(user_id=USER, account_id=app.state.accounts.default.account_id, trading_day=DAY)
    observation = record["payload"]["observation"]
    return {"tradingDay": DAY, "verdict": verdict, "observationKey": observation["key"],
            "revision": observation.get("revision") or observation["key"], **extra}


async def regenerate(service, app, *, stopped: bool):
    """The week's lesson as 没标打法 (no stop) or 提前离场 (the stop armed)."""
    (arm_stop if stopped else disarm_stop)(app)
    return await service.generate(USER, app.state.accounts.default, DAY, reason="manual")


# 记住绑定用户看到的那一版 (#524): 另一页重新生成后，旧页的回答不得落到新的一条上。
@pytest.mark.parametrize("second", ["another-lesson", "same-lesson", "x-y-x"])
async def test_an_answer_to_a_lesson_since_regenerated_is_refused_and_changes_nothing(tmp_path, second):
    app, provider, model, service, clock = setup(tmp_path)
    store = app.state.store
    seed_playbook(app)
    http = await client(app)
    try:
        await regenerate(service, app, stopped=False)
        shown = seen(app, "remember")                                   # 标签页 A 显示的那一版
        if second == "x-y-x":
            await regenerate(service, app, stopped=True)
            await regenerate(service, app, stopped=False)
        else:
            await regenerate(service, app, stopped=second == "another-lesson")   # 标签页 B 重新生成
        for verdict in ("remember", "dismiss"):
            stale = await http.post("/api/account/daily-review/observation", json={**shown, "verdict": verdict})
            assert stale.status_code == 409 and stale.json()["detail"]["code"] == "observation_stale"
        current = store.get_daily_review(user_id=USER, account_id=app.state.accounts.default.account_id,
                                         trading_day=DAY)["payload"]["observation"]
        assert current["status"] == "pending"
        assert not [item for item in store.list_memories(USER) if item["layer"] == "lesson"]
        assert store.observation_verdicts(USER) == []
        # 当前那一版照常可答。
        answered = await http.post("/api/account/daily-review/observation", json=seen(app, "dismiss"))
        assert answered.status_code == 200 and answered.json()["observation"]["status"] == "dismissed"
    finally:
        await http.aclose()
        app.state.store.close()


async def test_a_repeated_answer_decides_once(tmp_path):
    app, provider, model, service, clock = setup(tmp_path)
    store = app.state.store
    seed_playbook(app)
    http = await client(app)
    try:
        await regenerate(service, app, stopped=False)
        shown = seen(app, "remember")
        assert (await http.post("/api/account/daily-review/observation", json=shown)).status_code == 200
        again = await http.post("/api/account/daily-review/observation", json=shown)
        assert again.status_code == 409 and again.json()["detail"] == "这条观察已经答过了"
        assert [item["text"] for item in store.list_memories(USER) if item["layer"] == "lesson"] == [UNTAGGED]
        assert len(store.list_memory_changes(USER)) == 1
    finally:
        await http.aclose()
        app.state.store.close()


async def test_a_failed_review_write_commits_no_memory_and_no_verdict(tmp_path, monkeypatch):
    app, provider, model, service, clock = setup(tmp_path)
    store = app.state.store
    seed_playbook(app)
    http = await client(app)
    try:
        await regenerate(service, app, stopped=False)
        shown = seen(app, "remember")

        def broken(**kwargs):
            raise RuntimeError("disk full")
        monkeypatch.setattr(store, "save_daily_review", broken)
        with pytest.raises(RuntimeError):
            await http.post("/api/account/daily-review/observation", json=shown)
        monkeypatch.undo()
        assert not store.list_memories(USER) and not store.list_memory_changes(USER)
        assert store.observation_verdicts(USER) == []
        assert (await http.post("/api/account/daily-review/observation", json=shown)).status_code == 200
    finally:
        await http.aclose()
        app.state.store.close()


async def test_remember_writes_the_weeks_lesson_with_its_source_and_regenerating_keeps_the_answer(tmp_path):
    app, provider, model, service, clock = setup(tmp_path)
    store = app.state.store
    seed_playbook(app)
    http = await client(app)
    try:
        await regenerate(service, app, stopped=False)
        # The AI may answer only with the user's own words: without a quote it is refused.
        ai = await http.post("/api/agent/call", json={"operation": "POST /api/account/daily-review/observation",
                                                      "body": seen(app, "remember")})
        assert ai.json()["ok"] is False and ai.json()["status"] == 422
        answered = await http.post("/api/account/daily-review/observation", json=seen(app, "remember"))
        assert answered.status_code == 200 and answered.json()["observation"]["status"] == "remembered"
        memory = next(item for item in store.list_memories(USER) if item["id"] == answered.json()["memoryId"])
        assert (memory["layer"], memory["source"], memory["text"]) == ("lesson", "user", UNTAGGED)
        assert memory["evidence"]["via"] == "daily_review" and memory["evidence"]["tradingDay"] == DAY
        assert memory["evidence"]["tradeIds"] == ["t1"]
        assert (await http.post("/api/account/daily-review/observation", json=seen(app, "dismiss"))).status_code == 409
        # Regenerating keeps the answer of the same lesson; another lesson that week asks again.
        assert (await regenerate(service, app, stopped=False))["observation"]["status"] == "remembered"
        assert (await regenerate(service, app, stopped=True))["observation"]["status"] == "pending"
    finally:
        await http.aclose()
        app.state.store.close()


async def test_with_amounts_hidden_the_lock_screen_line_carries_no_figure(tmp_path):
    app, provider, model, service, clock = setup(tmp_path)
    sender = configure(app)
    http = await client(app)
    try:
        assert (await http.put("/api/app/push/subscription", json=body("device-a-0001", APPLE))).status_code == 200
        assert (await http.put("/api/preferences", json={"notify": {"lockScreenAmounts": False}})).status_code == 200
        clock["now"] = bj(15, 15)
        assert await service.run_once() == 1
        await app.state.push.drain()
        assert [(payload["title"], payload["body"]) for _, payload in sender.sent] == [("今天的复盘好了", "收盘复盘好了，点开看每一笔。")]
    finally:
        await http.aclose()
        app.state.store.close()


async def test_with_the_switch_off_the_review_is_written_but_not_delivered(tmp_path):
    app, provider, model, service, clock = setup(tmp_path)
    sender = configure(app)
    http = await client(app)
    try:
        session_id = (await http.post("/api/sessions", json={"mode": "local", "agentId": "native"})).json()["sessionId"]
        assert (await http.put("/api/app/push/subscription", json=body("device-a-0001", APPLE))).status_code == 200
        assert (await http.put("/api/preferences", json={"notify": {"dailyReview": False}})).status_code == 200
        clock["now"] = bj(15, 15) + timedelta(minutes=1)
        await service.run_once()
        await app.state.push.drain()
        assert sender.sent == [] and review_messages(app, session_id) == []
        assert (await http.get("/api/account/daily-review")).json()["review"]["tradingDay"] == DAY
    finally:
        await http.aclose()
        app.state.store.close()


def test_day_figures_reach_the_model_and_the_review_rounded_to_cents(tmp_path):
    """10-08: the ledger's float -204.0507999999454 went to the model and the 复盘 summary verbatim."""
    from trader_runtime.daily_review import _day_pnl
    app, *_ = setup(tmp_path)
    account = app.state.accounts.default.account_id
    app.state.store.upsert_account_daily(DAY, {"equity": 999_775, "staticBalance": 1_000_000, "closeProfit": -80,
                                               "positionProfit": 0, "commission": 2.1812, "todayNetPnl": -204.0507999999454},
                                         trading_account_id=account)
    assert _day_pnl(app.state.store, None, account_id=account, trading_day=DAY) == (-204.05, 2.18, "live")


def test_day_pnl_is_the_settled_result_once_the_next_trading_day_row_exists(tmp_path):
    """所有者 2026-10-22：复盘的当日盈亏按结算价（国内外一起）。下一个交易日那行的静态权益就是这一天的结算结果：
    当日盈亏 = 下一交易日静态权益 − 当天静态权益；还没结算时是当天最后一次读到的（口径 live）。不是紧接的下一个
    交易日那行（中间缺了一天）不算，免得把两天算成一天。"""
    from trader_runtime.daily_review import _day_pnl, next_cme_trade_day
    app, *_ = setup(tmp_path)
    store, account = app.state.store, app.state.accounts.default.account_id
    # 10-21 外盘验证仓那天：盘中最后一次读到 17.5（现价约 4145.7），结算价 4150 → 静态权益 99905 → 99965.5
    store.upsert_account_daily("2026-10-21", {"equity": 99_922.5, "staticBalance": 99_905.0, "closeProfit": 0,
                                              "positionProfit": 20.0, "commission": 2.5, "todayNetPnl": 17.5},
                               trading_account_id=account)
    assert _day_pnl(store, None, account_id=account, trading_day="2026-10-21", next_day="2026-10-22") == (17.5, 2.5, "live")
    store.upsert_account_daily("2026-10-23", {"equity": 99_965.5, "staticBalance": 99_965.5}, trading_account_id=account)
    assert _day_pnl(store, None, account_id=account, trading_day="2026-10-21", next_day="2026-10-22")[2] == "live"
    store.upsert_account_daily("2026-10-22", {"equity": 99_965.5, "staticBalance": 99_965.5}, trading_account_id=account)
    assert _day_pnl(store, None, account_id=account, trading_day="2026-10-21", next_day="2026-10-22") == (60.5, 2.5, "settlement")
    # CME 交易日：周五之后是周一，感恩节（没有交易日）跳过
    assert next_cme_trade_day("2026-10-23") == "2026-10-26" and next_cme_trade_day("2026-11-25") == "2026-11-27"


async def test_a_pushed_review_is_corrected_quietly_once_the_day_settles(tmp_path):
    """15:15 的复盘先按当天最后一次读数推送（结算在晚上）；下一个交易日那行带着结算后的静态权益出现后，
    复盘悄悄按结算价重算当日盈亏（不再推送），口径从 live 变 settlement。"""
    app, provider, model, service, clock = setup(tmp_path)
    sender = configure(app)
    http = await client(app)
    account = app.state.accounts.default.account_id
    try:
        assert (await http.put("/api/app/push/subscription", json=body("device-a-0001", APPLE))).status_code == 200

        async def next_day(services, day):
            return "2026-10-12" if day == DAY else None
        service._next_trading_day = next_day  # type: ignore[method-assign]
        clock["now"] = bj(15, 16)
        assert await service.run_once() == 1
        review = (await http.get("/api/account/daily-review")).json()["review"]
        assert (review["dayPnl"], review["dayPnlBasis"]) == (2100, "live")
        await app.state.push.drain()
        pushed = len(sender.sent)
        # Nothing settled yet: nothing rebuilt.
        assert await service.settle_recent(USER, app.state.accounts.default) == 0
        # The evening settlement: Monday's row starts from the settled balance (marked at the settlement price).
        app.state.store.upsert_account_daily("2026-10-12", {"equity": 1_002_050, "staticBalance": 1_002_050},
                                             trading_account_id=account)
        assert await service.run_once() == 0  # nothing new delivered …
        review = (await http.get("/api/account/daily-review")).json()["review"]
        assert (review["dayPnl"], review["dayPnlBasis"], review["reason"]) == (2050, "settlement", "settled")
        await app.state.push.drain()
        assert len(sender.sent) == pushed  # … and no second push
        assert await service.settle_recent(USER, app.state.accounts.default) == 0  # settled once
    finally:
        await http.aclose()
        app.state.store.close()
