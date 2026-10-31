"""Order-intent corpus (docs/ORDER_INTENT_V2.md, section 3): tests/intent_corpus/{cases.jsonl,contexts.json}.

The corpus file itself is valid (fields, contexts, canonical product names, coverage of the classes the design
lists) and the comparison helpers behave; the parser (step 2, order_intent_parse.parse) is right or Uncertain on
every row, never wrong; the planner (step 1) matches every row that pins it. The model-extraction pass is offline
only: eval/intent/run_model_eval.py.
"""
from __future__ import annotations

import collections
import dataclasses
import re
from typing import Any

from intent_corpus import corpus

CASES = corpus.load_cases()
CONTEXTS = corpus.load_contexts()
NAMED = corpus.named_contexts(CONTEXTS)


def _walk(expect: dict[str, Any]):
    yield expect
    yield from expect.get("legs") or []


def test_every_row_is_well_formed():
    catalog = CONTEXTS["_catalog"]
    problems = [problem for case in CASES for problem in corpus.validate_case(case, set(NAMED), catalog)]
    assert not problems, "\n".join(problems[:40])


def test_ids_and_sentences_are_unique():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)), [i for i, n in collections.Counter(ids).items() if n > 1]
    pairs = [(case["say"], case["ctx"]) for case in CASES]
    assert len(pairs) == len(set(pairs)), "the same sentence in the same context twice"
    assert ids == sorted(ids), "ids stay in file order; new rows are appended with the next number"


def test_contexts_are_consistent_and_all_used():
    assert not corpus.context_problems(CONTEXTS)
    unused = set(NAMED) - {case["ctx"] for case in CASES}
    assert not unused, f"contexts no row uses (delete them or add a row): {sorted(unused)}"


def test_corpus_covers_the_classes_the_design_lists():
    expects = [item for case in CASES for item in _walk(case["expect"])]
    count = collections.Counter()
    for item in expects:
        count[f"act:{item.get('act')}"] += 1
        count[f"stance:{item.get('stance')}"] += 1
        count[f"bucket:{item.get('bucket')}"] += 1
        price = str(item.get("price") or "")
        count[f"price:{price.split(':')[0]}"] += 1
        count["frac"] += "frac" in item
        count["ask"] += "ask" in item
        count["uncertain"] += bool(item.get("uncertain"))
        count["topic"] += "topic" in item
    count["legs"] = sum("legs" in case["expect"] for case in CASES)
    count["voice"] = sum(case["origin"] == "voice" for case in CASES)
    count["typed"] = sum(case["origin"] == "typed" for case in CASES)
    count["pending_card_ctx"] = sum(bool(NAMED[case["ctx"]].get("pending_card")) for case in CASES)
    count["fixed"] = sum(bool(item.get("fixed")) for item in expects)
    minimum = {
        "act:open": 40, "act:close": 15, "act:close_all": 15, "act:target": 20, "act:reverse": 3, "act:cancel_order": 8,
        "act:retract": 5, "act:amend": 5, "act:confirm": 3, "act:none": 60,
        "stance:order": 100, "stance:question": 15, "stance:conditional": 5, "stance:reported": 5,
        "stance:retracted": 8, "stance:advice": 5, "stance:negated": 10,
        "bucket:today": 3, "bucket:yesterday": 3, "price:counter": 5, "price:queue": 5, "price:limit": 5, "price:last": 1,
        "frac": 3, "ask": 8, "uncertain": 15, "topic": 10, "legs": 5, "fixed": 5,
        "voice": 70, "typed": 100, "pending_card_ctx": 15,
    }
    short = {key: (count[key], need) for key, need in minimum.items() if count[key] < need}
    assert not short, f"class coverage below the first-version floor (have, need): {short}"
    assert len(CASES) >= 250


def test_no_owner_conversations_or_identifiers():
    """Only test-account sentences and repo/issue wording go in (owner 2026-10-16); no ids, phones or accounts."""
    for case in CASES:
        assert not case["src"].startswith(("prod-owner", "owner")), case["id"]
        assert not re.search(r"\d{8,}|owner-[0-9a-f]{8}|@[a-z0-9.-]+\.[a-z]{2,}|paper:[0-9a-f]{6}", case["say"] + str(case.get("notes", "")), re.I), case["id"]
    assert all(c["src"].startswith("prod-test") for c in CASES if "prod" in c["src"])


def test_comparison_helpers():
    unsure = corpus.UNSURE
    want = {"act": "open", "stance": "order", "product": "螺纹", "dir": "long", "vol": 2, "price": "queue", "ask": ["dir"], "x_protect": 1}
    ok = {"act": "open", "stance": "order", "product": "螺纹", "dir": "long", "vol": 2.0, "price": "passive", "ask": ["dir"]}
    assert corpus.intent_mismatch(want, ok) == []  # passive == queue, 2.0 == 2, x_ key the parser lacks is skipped
    assert corpus.intent_mismatch(want, unsure) == []  # declining is always allowed for the fast rules ...
    assert corpus.intent_mismatch(want, unsure, allow_unsure=False)  # ... but not for the model pass
    assert corpus.intent_mismatch(want, {**ok, "vol": 1})  # a wrong structure is never allowed
    assert corpus.intent_mismatch({"uncertain": True}, ok)  # an `uncertain` row demands UNSURE
    assert corpus.intent_mismatch({"uncertain": True}, unsure) == []
    assert corpus.intent_mismatch({"price": None}, {"price": "counter"})  # explicit null = must be empty
    legs = {"legs": [{"act": "open", "vol": 1}, {"act": "cancel_order"}]}
    assert corpus.intent_mismatch(legs, {"legs": [{"act": "open", "vol": 1}, {"act": "cancel_order"}]}) == []
    assert corpus.intent_mismatch(legs, {"legs": [{"act": "open", "vol": 1}]})

    plan = {"outcome": "cards", "stepwise": True, "cards": [{"side": "sell_close", "vol": 1}, {"side": "sell_open", "vol": 2, "price": "queue"}]}
    got = {"outcome": "cards", "stepwise": True, "cards": [{"side": "sell_close", "vol": 1, "price": "queue"}, {"side": "sell_open", "vol": 2, "price": "passive"}]}
    assert corpus.plan_mismatch(plan, got) == []
    assert corpus.plan_mismatch(plan, {**got, "stepwise": False})
    assert corpus.plan_mismatch(plan, {**got, "cards": list(reversed(got["cards"]))})  # stepwise order matters
    unordered = {**plan, "stepwise": False}
    assert corpus.plan_mismatch(unordered, {**got, "stepwise": False, "cards": list(reversed(got["cards"]))}) == []
    assert corpus.plan_mismatch({"outcome": "reject", "reason": "conditional"}, {"outcome": "reject", "reason": "other"})
    assert corpus.expected_plan({"act": "none", "stance": "question"}) == {"outcome": "none"}
    assert corpus.expected_plan({"act": "open", "stance": "order", "ask": ["dir"]}) == {"outcome": "ask", "slots": ["dir"]}
    assert corpus.expected_plan({"act": "open", "stance": "order", "vol": 1}) is None


# -- Step 2: the parser (docs/ORDER_INTENT_V2.md 2.2): every row right, or Uncertain — never a wrong structure. --

def parse_context(ctx: dict[str, Any]):
    """The named account context as the parser reads it: the corpus catalog's names (the shared spoken aliases, as
    in production), the chart and its product, a waiting card, and the directions held per product."""
    from trader_runtime.order_intent_parse import ParseContext
    by_symbol = {entry["symbol"]: name for name, entry in CONTEXTS["_catalog"].items()}
    held: dict[str, set[str]] = {}
    for row in ctx.get("positions") or []:
        held.setdefault(by_symbol[row["sym"]], set()).add(row["dir"])
    return ParseContext(products=tuple(CONTEXTS["_catalog"]), chart=ctx["chart"], chart_product=by_symbol.get(ctx["chart"]),
                        pending_card=bool(ctx.get("pending_card")), held={k: frozenset(v) for k, v in held.items()})


def corpus_view(result: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """A parser result in the corpus's terms (README `expect`): cancel → cancel_order; the contract as the catalog
    name / spoken month / @chart / @all; the price as counter|queue|last|limit:N; an opening's missing slots as `ask`."""
    from trader_runtime.order_intent_parse import Uncertain
    if isinstance(result, Uncertain):
        return dict(corpus.UNSURE)
    if result.legs:
        return {"legs": [corpus_view(leg, ctx) for leg in result.legs]}
    act = "cancel_order" if result.act == "cancel" else result.act
    view: dict[str, Any] = {"act": act, "stance": result.stance}
    if result.product:
        view["product"] = result.product
        if result.symbol:
            view["symbol"] = result.symbol
    elif ctx["chart"] and result.symbol == ctx["chart"] or act in {"open", "target", "reverse"}:
        view["product"] = "@chart"
    else:
        view["product"] = "@all" if act == "close_all" else None
    price = result.price
    view.update(dir=result.dir, vol=result.vol, target=result.target, frac=result.frac, cond=result.cond,
                price=f"limit:{price.limit:g}" if price.limit is not None else price.mode,
                bucket=result.bucket, scope=result.scope, fixed=result.fixed)
    if act == "open":
        missing = [slot for slot, gone in (("dir", result.dir is None), ("vol", result.vol is None),
                                           ("product", not result.product and not result.symbol)) if gone]
        if missing:
            view["ask"] = missing
    return view


def _parsed_rows():
    from trader_runtime.order_intent_parse import parse
    for case in CASES:
        ctx = NAMED[case["ctx"]]
        yield case, parse(case["say"], parse_context(ctx)), ctx


def test_the_parser_is_right_or_unsure_never_wrong():
    wrong = []
    for case, got, ctx in _parsed_rows():
        expect = {key: value for key, value in case["expect"].items() if key != "topic"}  # topic: statistics only
        wrong += [f"{case['id']} {case['say']}: {p}"
                  for p in corpus.intent_mismatch(expect, corpus_view(got, ctx), allow_unsure=True)]
    assert not wrong, "\n".join(wrong[:40])


# -- Step 1: the planner (docs/ORDER_INTENT_V2.md 2.3): each row's structure in its named account context. --
# The rows this pins are the planner's own (cards / ask / reject / none). Not the planner's: 撤单、撤回、改单、确认
# (their own chains: cancel lookup, pending-card ops), a "实盘" word (x_live: refused before any intent exists),
# an ask for a price the words left out (the parser's slot, not the account's), and legs (their own test below:
# independent legs are all carded at once, legs depending on one contract go to the relay).
_OTHER_CHAINS = {"cancel_order", "retract", "amend", "confirm"}
_REASONS = {  # planner refusal code -> the corpus's reason word
    "conditional_order": "conditional", "close_position_not_found": "no_position",
    "reverse_position_not_found": "no_position", "no_position_to_close": "no_position",
    "close_volume_exceeds_position": "exceeds_position", "close_bucket_empty": "no_such_bucket",
    "target_position_unchanged": "already_at_target", "target_position_orders_working": "working_order",
}
_MODES = {"aggressive": "counter", "passive": "queue", "last": "last"}
_BUCKETS = {"today": "today", "historical": "yesterday"}


class _CorpusProvider:
    """The catalog the corpus names (`_catalog`), the only provider read the planner makes itself."""

    def instrument_catalog(self) -> dict[str, Any]:
        items = []
        for entry in CONTEXTS["_catalog"].values():
            exchange, instrument = entry["symbol"].split(".", 1)
            items.append({"symbol": entry["symbol"], "exchangeId": exchange, "instrumentId": instrument,
                          "instrumentName": entry["name"], "productId": instrument.rstrip("0123456789")})
        return {"items": items}


class _CorpusStore:
    default_trading_account_id = "acct"

    def get_session(self, session_id: str) -> dict[str, Any]:
        return {"tradingAccountId": "acct"}

    def create_target_plan(self, **kwargs: Any) -> tuple[dict[str, Any], None]:
        return {"id": "plan-1"}, None

    def update_target_plan(self, *args: Any, **kwargs: Any) -> None:
        return None


class _CorpusProposals:
    """Records the cards the planner writes (the builder's own checks are covered by the service tests)."""

    def __init__(self, positions: list[dict[str, Any]]) -> None:
        self.store = _CorpusStore()
        self.positions = positions

    @staticmethod
    def _card(symbol: str, side: str, volume: int, price_intent: str, limit_price: Any, bucket: Any) -> dict[str, Any]:
        price = f"limit:{limit_price:g}" if limit_price is not None else _MODES[price_intent]
        return {"symbol": symbol, "side": side, "vol": volume, "price": price,
                **({"bucket": _BUCKETS[bucket]} if bucket in _BUCKETS else {})}

    def create_trade(self, session_id: str, turn_id: str, *, symbol: str, side: str, volume: int,
                     price_intent: str, limit_price: Any = None, position_bucket: Any = None,
                     target_position: Any = None, **kwargs: Any) -> dict[str, Any]:
        return {"proposalId": f"p-{symbol}-{side}", "orders": [self._card(
            symbol, side, volume, price_intent, limit_price, position_bucket)], "targetPosition": target_position}

    def create_close_batch(self, session_id: str, turn_id: str, *, symbols: Any = None, direction: Any = None,
                           max_volume: Any = None, price_intent: str = "aggressive", limit_price: Any = None,
                           target_position: Any = None, **kwargs: Any) -> dict[str, Any]:
        from trader_runtime.order_planner import close_legs
        from trader_runtime.proposals import CloseBatchRefused
        wanted = {symbol.casefold() for symbol in symbols or []}
        orders = []
        for row in self.positions:
            if (wanted and row["symbol"].casefold() not in wanted) or direction not in (None, row["direction"]):
                continue
            volume = max_volume or row["volume"]
            for bucket, lots in close_legs([row], symbol=row["symbol"], direction=row["direction"], volume=volume,
                                           preference="auto"):
                side = "sell_close" if row["direction"] == "long" else "buy_close"
                orders.append(self._card(row["symbol"], side, lots, price_intent, limit_price, bucket))
        if not orders:
            raise CloseBatchRefused("no_position_to_close", [])
        return {"proposalId": "batch", "orders": orders, "targetPosition": target_position}


def _corpus_symbol(product: Any, ctx: dict[str, Any]) -> str | None:
    if product in (None, "@chart"):
        return ctx.get("chart")
    if product == "@all":
        return None
    return CONTEXTS["_catalog"][product]["symbol"]


def _corpus_intent(expect: dict[str, Any], ctx: dict[str, Any]):
    from trader_runtime.order_intent import OrderIntent, Price
    from trader_runtime.order_planner import product_of
    price = str(expect.get("price") or "")
    act = expect.get("act") or "none"
    symbol = _corpus_symbol(expect.get("product"), ctx) if act != "close_all" or expect.get("product") != "@all" else None
    if act == "close_all" and expect.get("product") is None:
        symbol = None
    named_product = expect.get("product") not in (None, "@chart", "@all")
    return OrderIntent(
        act=act, stance=expect.get("stance") or "order", symbol=symbol,
        product=product_of(symbol) if symbol and named_product and act != "open" else None,
        dir=expect.get("dir"), vol=expect.get("vol"), target=expect.get("target"), frac=expect.get("frac"),
        price=Price(limit=float(price.split(":", 1)[1])) if price.startswith("limit:") else Price(mode=price or None),
        bucket=expect.get("bucket") or "auto", cond=expect.get("cond"))


def _corpus_outcome(outcome: Any) -> dict[str, Any]:
    from trader_runtime.order_planner import Ask, Cards
    if isinstance(outcome, Ask):
        return {"outcome": "ask", "slots": [outcome.missing]}
    if isinstance(outcome, Cards):
        names = {entry["symbol"]: name for name, entry in CONTEXTS["_catalog"].items()}
        cards = [{**{k: v for k, v in order.items() if k != "symbol"}, "product": names.get(order["symbol"])}
                 for card in outcome.cards for order in card.proposal["orders"]]
        plan = outcome.target_position or {}
        later = (plan.get("legs") or [])[1:]  # the relay's next steps, issued once the leg before has filled
        cards += [{"side": leg["side"], "vol": leg["volume"], "product": names.get(plan.get("symbol")),
                   **({"price": _MODES[leg.get("priceIntent") or plan["priceIntent"]]}
                      if leg.get("priceIntent") or plan.get("priceIntent") else {})} for leg in later]
        return {"outcome": "cards", "cards": cards, "stepwise": bool(later)}
    if outcome.code in {"not_an_order", "not_a_card_act"}:
        return {"outcome": "none"}
    return {"outcome": "reject", "reason": _REASONS.get(outcome.code, outcome.code),
            **({"suggest": outcome.data["suggest"]} if (outcome.data or {}).get("suggest") else {})}


def _corpus_snapshot(ctx: dict[str, Any]):
    from trader_runtime.order_planner import AccountSnapshot
    positions = [{"symbol": row["sym"], "direction": row["dir"], "volume": row["vol"], "todayVolume": row["today"],
                  "historicalVolume": row["yd"], "frozenToday": 0, "frozenHistorical": 0}
                 for row in ctx.get("positions") or []]
    orders = [{"symbol": row["sym"], "direction": "BUY" if row["side"].startswith("buy") else "SELL",
               "offset": "OPEN" if row["side"].endswith("open") else "CLOSE", "volume": row["vol"],
               "volumeLeft": row["left"], "status": row["status"]} for row in ctx.get("orders") or []]
    return AccountSnapshot.of(positions, {"orders": orders, "ordersInitialized": True, "isCached": False,
                                          "dataStatus": "normal"})


def test_planner_results_match_the_corpus():
    from trader_runtime import order_planner
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    wrong, checked = [], 0
    for case in CASES:
        expect = case["expect"]
        want = corpus.expected_plan(expect)
        if (want is None or expect.get("legs") or expect.get("act") in _OTHER_CHAINS or expect.get("x_live")
                or "price" in (expect.get("ask") or [])):
            continue
        ctx = NAMED[case["ctx"]]
        snapshot = _corpus_snapshot(ctx)
        context = order_planner.PlanContext(
            provider=_CorpusProvider(), proposals=_CorpusProposals(list(snapshot.positions)), session_id="s",
            turn_id="t", user_id="u", decision_context=lambda symbol: (ready, None))
        got = _corpus_outcome(order_planner.plan(_corpus_intent(expect, ctx), snapshot, order_planner.Prefs(), context))
        wrong += [f"{case['id']} {case['say']}: {p}" for p in corpus.plan_mismatch(want, got)]
        checked += 1
    assert checked >= 150, checked
    assert not wrong, "\n".join(wrong[:40])


def test_the_models_readings_are_grounded_as_the_corpus_says():
    """意图重构第 3 步的模型路径（#932 A11/A12/A15/A16/A17）：a row's `model` readings are what the trading AI was seen to
    give for that sentence; propose_order's one validation (`ground`, against the words, the account and the one waiting
    card an explicit correction changes) and then the planner must make of each what its `plan` says — a card, or a
    question instead of a card the words never asked for."""
    from trader_runtime import order_planner
    from trader_runtime.turn_router import catalog_product_names
    items = _CorpusProvider().instrument_catalog()["items"]
    products = tuple(catalog_product_names(items) or ())
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    modes = {mode: word for word, mode in _MODES.items()}
    wrong, checked = [], 0
    for case in CASES:
        ctx = NAMED[case["ctx"]]
        snapshot = _corpus_snapshot(ctx)
        card = ctx.get("pending_card") or {}
        cards = ([{"symbol": card["sym"], "side": card["side"], "volume": card["vol"],
                   "priceIntent": modes.get(card.get("price_mode"))}]
                 if card.get("type") == "trade" and case["expect"].get("act") == "amend" else [])
        for reading in case.get("model") or []:
            said = reading["intent"]
            intent = _corpus_intent(said, ctx)
            if "x_symbol" in said:
                intent = dataclasses.replace(intent, symbol=said["x_symbol"], product=None)
            if "product" not in said and "x_symbol" not in said:
                intent = dataclasses.replace(intent, symbol=None, product=None)  # the model named no contract
            if "x_symbols" in said:  # a 全平 over several scopes as the model listed them
                intent = dataclasses.replace(intent, symbols=tuple(said["x_symbols"]), symbol=None, product=None)
            if "x_remaining" in said:
                intent = dataclasses.replace(intent, remaining_protection=said["x_remaining"])
            grounded = order_planner.ground(intent, case["say"], products=products, chart=ctx.get("chart"),
                                            positions=snapshot.positions, cards=cards, items=items)
            if isinstance(grounded, order_planner.Ask):
                got = _corpus_outcome(grounded)
            else:
                resolved = order_planner.resolve_words(grounded, items, ctx.get("chart"))
                context = order_planner.PlanContext(
                    provider=_CorpusProvider(), proposals=_CorpusProposals(list(snapshot.positions)), session_id="s",
                    turn_id="t", user_id="u", decision_context=lambda symbol: (ready, None))
                got = _corpus_outcome(resolved if isinstance(resolved, order_planner.Refuse) else order_planner.plan(
                    resolved, snapshot, order_planner.Prefs(default_volume=said.get("x_default_volume")), context))
            wrong += [f"{case['id']} {case['say']} {said}: {p}" for p in corpus.plan_mismatch(reading["plan"], got)]
            checked += 1
    assert checked >= 10, checked
    assert not wrong, "\n".join(wrong)


def test_planner_cards_independent_legs_at_once_and_relays_dependent_ones():
    """一句两单 (owner 2026-10-16; #950 review: the planner decides, not the way a card is confirmed): legs that do not
    touch each other's contract — all their cards now, in order (corpus `stepwise` false); legs that close and touch
    one contract again — the first card now, the rest relayed after its fill (`stepwise` true)."""
    from trader_runtime import order_planner
    from trader_runtime.order_intent import OrderIntent
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    rows = [case for case in CASES if case["expect"].get("legs") and case["expect"].get("plan")]
    assert {bool(case["expect"]["plan"].get("stepwise")) for case in rows} == {True, False}
    wrong = []
    for case in rows:
        ctx, want = NAMED[case["ctx"]], case["expect"]["plan"]
        snapshot = _corpus_snapshot(ctx)
        legs = tuple(_corpus_intent(leg, ctx) for leg in case["expect"]["legs"])
        context = order_planner.PlanContext(
            provider=_CorpusProvider(), proposals=_CorpusProposals(list(snapshot.positions)), session_id="s",
            turn_id="t", user_id="u", decision_context=lambda symbol: (ready, None))
        got = _corpus_outcome(order_planner.plan(OrderIntent(act=legs[0].act, legs=legs), snapshot,
                                                 order_planner.Prefs(), context))
        wrong += [f"{case['id']} {case['say']}: {p}" for p in corpus.plan_mismatch(want, got)]
    assert not wrong, "\n".join(wrong)


# 一句多单 (owner 2026-10-16: 按顺序出卡、各自确认): the fast rules split the sentence at its connectives and read each
# clause as one plain sentence — these rows become legs, each leg's slots from its own clause; the rest stay Uncertain
# (a cancel among them, a shared “各买”, a clause with something missing or unsure, a price said apart, a condition, the
# same order twice, a bare 卖 against the position held): one order at a time.
LEGS_READ = {"c284", "c285", "c288", "c290", "c352", "c361", "c366", "c371", "c373"}
LEGS_UNSURE = {"c167", "c286", "c289", "c322", "c360", "c367", "c368", "c369", "c370", "c372"}


def test_the_fast_parser_reads_several_orders_clause_by_clause_or_not_at_all():
    from trader_runtime.order_intent_parse import Uncertain
    got = {case["id"]: parsed for case, parsed, _ in _parsed_rows()}
    assert [key for key in sorted(LEGS_READ) if isinstance(got[key], Uncertain) or len(got[key].legs) != 2] == []
    assert [key for key in sorted(LEGS_UNSURE) if not isinstance(got[key], Uncertain)] == []
    several = {case["id"] for case in CASES if case["expect"].get("legs")}
    assert several <= LEGS_READ | LEGS_UNSURE, sorted(several - LEGS_READ - LEGS_UNSURE)


def test_the_parsers_legs_plan_as_the_corpus_says():
    """The parser's own legs, read against the corpus catalog and planned: each card carries the side, lots and contract
    of its own clause (c366: 螺纹买开 1 手 and 沪金卖开 2 手, never a slot from the other clause), independent legs at once,
    one contract closed and opened again relayed, an unscoped 全平 with another leg refused (legs_entangled)."""
    from trader_runtime import order_planner
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    items = _CorpusProvider().instrument_catalog()["items"]
    wrong, checked = [], 0
    for case, parsed, ctx in _parsed_rows():
        if case["id"] not in LEGS_READ or not case["expect"].get("plan"):
            continue
        resolved = order_planner.resolve_words(parsed, items, ctx["chart"])
        assert not isinstance(resolved, order_planner.Refuse), case["id"]
        snapshot = _corpus_snapshot(ctx)
        context = order_planner.PlanContext(
            provider=_CorpusProvider(), proposals=_CorpusProposals(list(snapshot.positions)), session_id="s",
            turn_id="t", user_id="u", decision_context=lambda symbol: (ready, None), intent_source="parser")
        outcome = order_planner.plan(resolved, snapshot, order_planner.Prefs(), context)
        wrong += [f"{case['id']} {case['say']}: {p}"
                  for p in corpus.plan_mismatch(case["expect"]["plan"], _corpus_outcome(outcome))]
        if isinstance(outcome, order_planner.Cards) and not outcome.target_position:
            # Each card names the clause it came from, in the order said.
            assert [card.leg for card in outcome.cards] == list(range(len(parsed.legs))), case["id"]
            assert [card.source.symbol for card in outcome.cards] == [leg.symbol for leg in resolved.legs], case["id"]
        checked += 1
    assert checked >= 5, checked
    assert not wrong, "\n".join(wrong)


def test_a_bare_side_against_the_position_in_a_sentence_of_several_is_refused_not_asked():
    """A leg that only says 买/卖 while the opposite side is held (开新仓还是平仓？), or one the account leaves open (两个
    月份), cannot be asked about mid-sentence: no card, one order at a time. Cards of a sentence of several are marked
    `parser_legs`, so 语音免口头确认 never confirms them for the user."""
    from trader_runtime import order_planner
    from trader_runtime.order_intent import OrderIntent
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    snapshot = _corpus_snapshot(NAMED["long2_au"])
    context = order_planner.PlanContext(
        provider=_CorpusProvider(), proposals=_CorpusProposals(list(snapshot.positions)), session_id="s", turn_id="t",
        user_id="u", decision_context=lambda symbol: (ready, None), intent_source="parser")
    legs = (OrderIntent(act="open", symbol="SHFE.au2612", dir="short", vol=1, bare=True),
            OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1))
    refused = order_planner.plan(OrderIntent(act="open", legs=legs), snapshot, order_planner.Prefs(), context)
    assert isinstance(refused, order_planner.Refuse) and refused.code == "legs_unsettled"
    assert "一单一单说" in refused.issues[0]
    seen: list[Any] = []
    proposals = _CorpusProposals(list(snapshot.positions))
    original = proposals.create_trade

    def create_trade(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs.get("intent_source"))
        return original(*args, **kwargs)
    proposals.create_trade = create_trade  # type: ignore[method-assign]
    fine = (OrderIntent(act="open", symbol="SHFE.ag2612", dir="short", vol=1, bare=True), legs[1])
    got = order_planner.plan(OrderIntent(act="open", legs=fine), snapshot, order_planner.Prefs(),
                             dataclasses.replace(context, proposals=proposals))
    assert isinstance(got, order_planner.Cards) and len(got.cards) == 2 and seen == ["parser_legs", "parser_legs"]


def test_each_card_of_a_sentence_carries_its_own_leg_and_an_unscoped_close_all_never_mixes():
    """#950 review: a refused earlier leg shifts the later cards forward — each card names the leg it came from, so the
    narration follows the card. An unscoped 全平 with any other leg is refused, never narrowed to one contract."""
    from trader_runtime import order_planner
    from trader_runtime.order_intent import OrderIntent
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "openLimit": {},
             "source": "corpus", "market": {}}
    snapshot = _corpus_snapshot(NAMED["long2_au"])
    context = order_planner.PlanContext(
        provider=_CorpusProvider(), proposals=_CorpusProposals(list(snapshot.positions)), session_id="s", turn_id="t",
        user_id="u", decision_context=lambda symbol: (ready, None))
    legs = (OrderIntent(act="open", symbol="SHFE.zz2701", dir="long", vol=1),  # not in the catalog: refused
            OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1))
    got = order_planner.plan(OrderIntent(act="open", legs=legs), snapshot, order_planner.Prefs(), context)
    assert isinstance(got, order_planner.Cards) and len(got.cards) == 1 and len(got.notes) == 1
    assert got.cards[0].leg == 1 and got.cards[0].source.symbol == "SHFE.rb2701"
    mixed = (OrderIntent(act="close_all"), OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=1))
    refused = order_planner.plan(OrderIntent(act="close_all", legs=mixed), snapshot, order_planner.Prefs(), context)
    assert isinstance(refused, order_planner.Refuse) and refused.code == "legs_entangled"


def test_same_contract_opening_legs_share_the_intraday_open_limit():
    """#950 review: two openings of one contract in one sentence are carded at once and the confirm does not rerun
    the gate — so the second leg's gate counts the lots the first leg already carded."""
    from trader_runtime import order_planner
    from trader_runtime.order_intent import OrderIntent
    ready = {"userFacing": {}, "proposalReady": True, "proposalReadinessReasons": [], "source": "corpus",
             "market": {}, "openLimit": {"limit": 5, "remaining": 3, "openedToday": 2}}
    snapshot = _corpus_snapshot(NAMED["flat_chart_rb"])
    context = order_planner.PlanContext(
        provider=_CorpusProvider(), proposals=_CorpusProposals([]), session_id="s", turn_id="t", user_id="u",
        decision_context=lambda symbol: (ready, None))
    legs = (OrderIntent(act="open", symbol="SHFE.rb2701", dir="long", vol=2),
            OrderIntent(act="open", symbol="SHFE.rb2701", dir="short", vol=2))
    got = order_planner.plan(OrderIntent(act="open", legs=legs), snapshot, order_planner.Prefs(), context)
    assert isinstance(got, order_planner.Cards) and len(got.cards) == 1
    assert got.cards[0].proposal["orders"][0]["side"] == "buy_open"
    assert len(got.notes) == 1 and "今日还能开 3 手" in got.notes[0] and "这句话前面已出卡 2 手" in got.notes[0]
    # Each leg alone fits: only together are they over the remainder.
    assert isinstance(order_planner.plan(legs[1], snapshot, order_planner.Prefs(), context), order_planner.Cards)


# -- Step 3: the typed router reads order words with the parser (docs/ORDER_INTENT_V2.md §4 第 3 步). --

# The rows whose typed turn main (10-16) routed to a card while the row expects none or the model (问句、条件、转述、
# 撤回、否定、听不清): their `xfail` notes were removed once the typed router read them with the parser.
TYPED_ROUTE_FIXED = {
    "c020", "c028", "c029", "c042", "c048", "c054", "c055", "c061", "c204", "c207", "c208", "c210", "c212", "c214",
    "c215", "c216", "c217", "c218", "c221", "c222", "c242", "c243", "c244", "c245", "c254", "c259", "c262", "c265",
    "c266", "c267", "c268", "c271", "c275", "c276", "c277", "c282", "c287", "c291", "c292",
}


def _may_card(route: Any) -> bool:
    """Whether a typed turn on this route can make a card: the planner's fast path, or a model turn offered propose_order."""
    from trader_runtime.turn_router import CARD_TOOL
    if route.kind == "order_intent":
        return True
    if route.no_card:
        return False
    return route.tool_names is None or CARD_TOOL in route.tool_names


def test_the_typed_router_takes_the_parsers_reading_on_every_row():
    """A sentence the parser is sure of as an order goes to the planner with exactly that intent (no model); one it
    reads as no order now (问句、否定、转述、整句收回、建议、条件单) can never make a card; one it is not sure of goes to
    the model path (the trading AI's propose_order round or the general turn). A waiting card keeps the general turn."""
    from trader_runtime.order_intent import CARD_ACTS
    from trader_runtime.order_intent_parse import ParseContext, Uncertain, parse
    from trader_runtime.turn_router import classify_turn
    catalog = list(CONTEXTS["_catalog"])
    wrong = []
    for case in CASES:
        pending = bool(NAMED[case["ctx"]].get("pending_card"))
        route = classify_turn(case["say"], has_pending_card=pending, ui_available=True, catalog=catalog)
        if pending:
            ok = route.kind in {"general", "ui_intent"} and route.order is None
        else:
            parsed = parse(case["say"], ParseContext(products=tuple(catalog)))
            if isinstance(parsed, Uncertain):
                ok = route.kind != "order_intent"
            elif parsed.stance == "order" and (parsed.legs or parsed.act in CARD_ACTS):
                ok = route.kind == "order_intent" and route.order == parsed
            elif parsed.stance != "order":
                ok = not _may_card(route)
            else:
                ok = route.kind != "order_intent"  # 撤单、改单、确认 without a card: their own chains
        if not ok:
            wrong.append(f"{case['id']} {case['say']}: {route.kind} no_card={route.no_card}")
    assert not wrong, "\n".join(wrong[:40])


def test_no_typed_sentence_the_corpus_expects_no_card_from_is_carded_by_the_fast_path():
    """The 39 rows main's typed router carded (TYPED_ROUTE_FIXED) and every other row expecting no card or the model:
    the fast path never cards them — they are answered without a card tool, or the trading AI reads them. A bare
    买/卖 with the opposite side held (c020 卖一手) is checked against the account before the planner runs — in a
    sentence of several (c372), by the planner for that leg (legs_unsettled, no card)."""
    from trader_runtime.turn_router import classify_turn
    catalog = list(CONTEXTS["_catalog"])
    assert not [case["id"] for case in CASES if case.get("xfail")], "typed-route xfail notes are history now"
    assert TYPED_ROUTE_FIXED <= {case["id"] for case in CASES}
    wrong = []
    for case in CASES:
        expect = case["expect"]
        no_card = expect.get("act") == "none" or expect.get("stance") not in {None, "order"}
        if not (expect.get("uncertain") or no_card or case["id"] in TYPED_ROUTE_FIXED):
            continue
        if NAMED[case["ctx"]].get("pending_card"):
            continue
        route = classify_turn(case["say"], has_pending_card=False, ui_available=True, catalog=catalog)
        bare = route.order is not None and any(order.bare for order in (route.order, *route.order.legs))
        if route.kind == "order_intent" and not (bare and expect.get("uncertain")):
            wrong.append(f"{case['id']} {case['say']}: carded by the fast path")
    assert not wrong, "\n".join(wrong)


def test_place_verb_rewrite_keeps_negation_and_remaining():
    """所有者 2026-10-19：“下多单”的“下”读成开仓，只在它真是下单动词时（PR #1004 审查）：否定（不下 / 别下 / 不要下）
    留给否定规则判成不下单，“剩下空单”不是开仓。"""
    from trader_runtime.order_intent import OrderIntent
    from trader_runtime.order_intent_parse import Uncertain, mentions, parse
    for said in ("不下一手多单", "我不下多单", "别下多单", "不要下多单", "不打算下多单", "不能下多单", "不许下多单"):
        parsed = parse(said)
        assert isinstance(parsed, Uncertain) and parsed.gap == "negation", (said, parsed)
    for said in ("下一手多单", "我要下多单", "马上下一手多单", "请下多单", "麻烦下一手空单", "现在下多单"):
        parsed = parse(said)
        assert isinstance(parsed, OrderIntent) and parsed.act == "open", (said, parsed)
    assert mentions("把剩下空单平了").acts == frozenset({"close"})
    assert "open" not in mentions("分析一下多单的风险").acts  # “一下” is a filler, not a placement
    for said in ("不再下多单", "没再下一手空单", "别再下多单", "没有再下多单", "未再下多单", "申请下多单权限"):
        assert "open" not in mentions(said).acts and not isinstance(parse(said), OrderIntent), said
    for said in ("我下一手多单了", "我下多单了"):  # telling what was done is no new order
        assert not isinstance(parse(said), OrderIntent), said
        assert "open" not in mentions(said).acts, said


def test_a_bare_sell_against_a_held_long_asks_with_whole_order_answers():
    """#1006 / PR #1013: 持沪银多（非主力 2610）说“卖一手白银”、模型给主力 2612 也要追问；两个答案各是一整单——
    平的那个是实际持有的月份，开的那个是原样读出的单，合约与手数都跟着答案走。"""
    from trader_runtime import order_planner as op
    from trader_runtime.order_intent import OrderIntent
    from trader_runtime.turn_router import catalog_product_names
    items = _CorpusProvider().instrument_catalog()["items"]
    intent = OrderIntent(act="open", stance="order", symbol="SHFE.ag2612", dir="short", vol=1)
    ask = op.ground(intent, "卖一手白银", products=tuple(catalog_product_names(items) or ()), chart="SHFE.ag2612",
                    positions=[{"symbol": "SHFE.ag2610", "direction": "long"}], items=items)
    assert isinstance(ask, op.Ask) and ask.missing == "act" and ask.question == "平多还是开空？"
    choices = op.ask_choices(intent, ask, lambda symbol: symbol)
    assert [choice["label"] for choice in choices] == ["平多", "开空"]
    assert "SHFE.ag2610" in choices[0]["prompt"] and "1" in choices[0]["prompt"]
    assert "SHFE.ag2612" in choices[1]["prompt"] and "卖开" in choices[1]["prompt"]


def test_the_bare_side_question_keeps_month_scope_several_months_protection_and_corrections():
    """PR #1013 review: a named month asks only about that month; several held months → one 平 answer each; a stop
    riding the order → no quick answers; a correction of a waiting opening card keeps opening (no question)."""
    from trader_runtime import order_planner as op
    from trader_runtime.order_intent import OrderIntent
    from trader_runtime.turn_router import catalog_product_names
    items = _CorpusProvider().instrument_catalog()["items"]
    products = tuple(catalog_product_names(items) or ())
    rows = [{"symbol": "SHFE.ag2610", "direction": "long"}, {"symbol": "SHFE.ag2612", "direction": "long"}]

    def run(said, intent, positions=rows, cards=()):
        return op.ground(intent, said, products=products, chart="SHFE.ag2612", positions=positions, cards=cards,
                         items=items)
    sell = OrderIntent(act="open", stance="order", symbol="SHFE.ag2612", dir="short", vol=1)
    ask = run("卖一手白银", sell)
    assert isinstance(ask, op.Ask) and [o for o in ask.options if o.startswith("close:")] == [
        "close:SHFE.ag2610", "close:SHFE.ag2612"]
    labels = [choice["label"] for choice in op.ask_choices(sell, ask, lambda symbol: symbol)]
    assert labels == ["平多 SHFE.ag2610", "平多 SHFE.ag2612", "开空"]
    named = run("卖一手沪银2612", sell, positions=[{"symbol": "SHFE.ag2610", "direction": "long"}])
    assert not isinstance(named, op.Ask)  # the month said is not the month held: no 锁仓 question
    model = OrderIntent(act="open", stance="order", symbol="SHFE.ag2612", dir="short", vol=5)
    unsaid = run("卖白银", model)  # the model's 5 lots no word said: dropped before the question is built
    assert isinstance(unsaid, op.Ask) and all("5" not in choice["prompt"]
                                               for choice in op.ask_choices(model, unsaid, lambda symbol: symbol))
