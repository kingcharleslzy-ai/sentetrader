"""Loader, schema check and comparison helpers for the order-intent corpus.

Test data only: no product code is imported here. `tests/test_intent_corpus.py` uses these helpers,
and `eval/intent/run_model_eval.py` loads this file by path. Field meanings are in README.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).parent
UNSURE: dict[str, Any] = {"uncertain": True}  # what a fast parser returns for "I can't tell"

ACTS = {"open", "close", "close_all", "target", "reverse", "cancel_order", "retract", "amend", "confirm", "none"}
# `negated` is a corpus extension of the design's six stances (README explains why).
STANCES = {"order", "question", "conditional", "reported", "retracted", "advice", "negated"}
DIRS = {"long", "short"}
BUCKETS = {"today", "yesterday", "auto"}
ORIGINS = {"typed", "voice"}
TOPICS = {"market", "account", "ui", "chat", "pref", "routine", "protection", "reminder", "risk_rule", "control"}
SCOPES = {"pending_card", "last_order", "working_order", "all_orders"}
SIDES = {"buy_open", "sell_open", "buy_close", "sell_close"}
OUTCOMES = {"cards", "ask", "reject", "none"}
COND_OPS = {"<=", ">=", "<", ">", "reach"}
SRC_PREFIXES = ("issue#", "prod-test", "voice-sim", "repo:", "eval/fuzzy", "artifact:", "gap:")
PRICE_RE = re.compile(r"^(counter|queue|passive|last|limit:\d+(\.\d+)?)$")
# design 2.1 lists both `queue` and `passive`; today's code has one passive meaning (挂买一/卖一). The
# corpus writes `queue`; comparison treats the two as the same until the owner separates them.
PRICE_ALIASES = {"passive": "queue"}
INTENT_KEYS = {
    "act", "stance", "product", "symbol", "dir", "vol", "target", "frac", "price", "bucket", "cond", "legs",
    "ask", "scope", "fixed", "topic", "plan", "uncertain",
}


def load_cases() -> list[dict[str, Any]]:
    cases = []
    for number, line in enumerate((CORPUS_DIR / "cases.jsonl").read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"cases.jsonl line {number}: {error}") from error
    return cases


def load_contexts() -> dict[str, Any]:
    return json.loads((CORPUS_DIR / "contexts.json").read_text(encoding="utf-8"))


def named_contexts(contexts: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in contexts.items() if not name.startswith("_")}


def _intent_problems(expect: dict[str, Any], catalog: dict[str, Any], where: str, *, leg: bool = False) -> list[str]:
    problems: list[str] = []
    if expect.get("uncertain"):
        extra = set(expect) - {"uncertain"}
        return [f"{where}: uncertain rows carry no other key (got {sorted(extra)})"] if extra else []
    for key in expect:
        if key not in INTENT_KEYS and not key.startswith("x_"):
            problems.append(f"{where}: unknown expect key {key!r}")
    act, stance = expect.get("act"), expect.get("stance")
    if "legs" in expect:
        legs = expect["legs"]
        if leg or not isinstance(legs, list) or len(legs) < 2:
            problems.append(f"{where}: legs must be a list of 2+ intents at the top level only")
        else:
            for index, item in enumerate(legs):
                problems += _intent_problems(item, catalog, f"{where}.legs[{index}]", leg=True)
        if set(expect) - {"legs", "plan"}:
            problems.append(f"{where}: a legs row carries only legs (and plan)")
        return problems
    if act not in ACTS:
        problems.append(f"{where}: act {act!r} not in {sorted(ACTS)}")
    if stance is not None and stance not in STANCES:
        problems.append(f"{where}: stance {stance!r} not in {sorted(STANCES)}")
    if act == "none" and stance == "order":
        problems.append(f"{where}: act none cannot be stance order")
    elif act != "none" and stance is None:
        problems.append(f"{where}: an actionable intent needs a stance")
    if stance in {"question", "advice", "reported", "retracted", "negated"} and act != "none":
        problems.append(f"{where}: stance {stance} must be act none")
    if stance == "conditional" and not expect.get("cond"):
        problems.append(f"{where}: conditional needs cond")
    if expect.get("cond") is not None:
        cond = expect["cond"]
        if cond.get("type") == "price":
            if cond.get("op") not in COND_OPS or not isinstance(cond.get("value"), (int, float)):
                problems.append(f"{where}: bad price cond {cond}")
        elif cond.get("type") == "time":
            if not cond.get("text"):
                problems.append(f"{where}: time cond needs text")
        else:
            problems.append(f"{where}: cond.type must be price|time")
    product = expect.get("product")
    if product is not None and product not in {"@chart", "@all"} and product not in catalog:
        problems.append(f"{where}: product {product!r} is not a canonical catalog name")
    symbol = expect.get("symbol")
    if symbol is not None and (product is None or not str(symbol).startswith(str(product))):
        problems.append(f"{where}: symbol {symbol!r} must extend product {product!r}")
    if expect.get("dir") not in (None, *DIRS):
        problems.append(f"{where}: dir must be long|short|null")
    for key in ("vol", "target"):
        if key in expect and expect[key] is not None and (isinstance(expect[key], bool) or not isinstance(expect[key], int)):
            problems.append(f"{where}: {key} must be an integer")
    if "frac" in expect and not (isinstance(expect["frac"], (int, float)) and 0 < expect["frac"] < 1):
        problems.append(f"{where}: frac must be a number between 0 and 1")
    if sum(key in expect and expect[key] is not None for key in ("vol", "target", "frac")) > 1:
        problems.append(f"{where}: vol / target / frac are mutually exclusive")
    if "price" in expect and not PRICE_RE.match(str(expect["price"])):
        problems.append(f"{where}: price {expect['price']!r} not counter|queue|passive|last|limit:N")
    if "bucket" in expect and expect["bucket"] not in BUCKETS:
        problems.append(f"{where}: bucket must be one of {sorted(BUCKETS)}")
    if "scope" in expect and expect["scope"] not in SCOPES:
        problems.append(f"{where}: scope must be one of {sorted(SCOPES)}")
    if "topic" in expect and expect["topic"] not in TOPICS:
        problems.append(f"{where}: topic must be one of {sorted(TOPICS)}")
    if "ask" in expect and (not isinstance(expect["ask"], list) or not expect["ask"]):
        problems.append(f"{where}: ask must be a non-empty list of slot names")
    if act == "open" and stance == "order" and not expect.get("legs"):
        if product is None and "product" not in (expect.get("ask") or []):
            problems.append(f"{where}: open needs product (or ask it)")
        if expect.get("dir") is None and "dir" not in (expect.get("ask") or []):
            problems.append(f"{where}: open needs dir (or ask it)")
        if expect.get("vol") is None and "vol" not in (expect.get("ask") or []):
            problems.append(f"{where}: open needs vol (or ask it)")
    if act == "target" and not isinstance(expect.get("target"), int):
        problems.append(f"{where}: target act needs an integer target")
    if act == "close" and expect.get("vol") is None and expect.get("frac") is None:
        problems.append(f"{where}: close needs vol or frac (otherwise it is close_all)")
    if act == "reverse" and (expect.get("vol") is not None or expect.get("target") is not None):
        problems.append(f"{where}: reverse takes no vol/target (the held lots are used)")
    if act == "amend" and expect.get("scope") not in {"pending_card", None}:
        problems.append(f"{where}: amend works on the pending card")
    return problems


def _plan_problems(plan: dict[str, Any], catalog: dict[str, Any], where: str) -> list[str]:
    problems: list[str] = []
    if plan.get("outcome") not in OUTCOMES:
        problems.append(f"{where}: plan.outcome must be one of {sorted(OUTCOMES)}")
    if plan.get("outcome") == "cards":
        cards = plan.get("cards")
        if not isinstance(cards, list) or not cards:
            problems.append(f"{where}: cards plan needs cards")
        for card in cards or []:
            if card.get("side") not in SIDES or not isinstance(card.get("vol"), int) or card["vol"] < 1:
                problems.append(f"{where}: bad card {card}")
            if "price" in card and not PRICE_RE.match(str(card["price"])):
                problems.append(f"{where}: bad card price {card['price']!r}")
            if "bucket" in card and card["bucket"] not in BUCKETS:
                problems.append(f"{where}: bad card bucket {card['bucket']!r}")
            if "product" in card and card["product"] not in catalog:
                problems.append(f"{where}: card product {card['product']!r} not in catalog")
    if plan.get("outcome") == "reject" and not plan.get("reason"):
        problems.append(f"{where}: reject needs a reason")
    if plan.get("outcome") == "ask" and not plan.get("slots"):
        problems.append(f"{where}: ask needs slots")
    return problems


def validate_case(case: dict[str, Any], names: set[str], catalog: dict[str, Any]) -> list[str]:
    where = str(case.get("id"))
    problems: list[str] = []
    if not re.fullmatch(r"c\d{3,}", where):
        problems.append(f"{where}: id must look like c001")
    if not isinstance(case.get("say"), str) or not case["say"].strip():
        problems.append(f"{where}: say must be a non-empty string")
    if case.get("origin") not in ORIGINS:
        problems.append(f"{where}: origin must be typed|voice")
    if case.get("ctx") not in names:
        problems.append(f"{where}: ctx {case.get('ctx')!r} is not defined in contexts.json")
    if not str(case.get("src", "")).startswith(SRC_PREFIXES):
        problems.append(f"{where}: src {case.get('src')!r} must start with one of {SRC_PREFIXES}")
    unknown = set(case) - {"id", "say", "origin", "ctx", "src", "expect", "notes", "xfail", "model"}
    if unknown:
        problems.append(f"{where}: unknown row keys {sorted(unknown)}")
    expect = case.get("expect")
    if not isinstance(expect, dict) or not expect:
        return [*problems, f"{where}: expect must be a non-empty object"]
    problems += _intent_problems({k: v for k, v in expect.items() if k != "plan"}, catalog, where)
    if "plan" in expect:
        problems += _plan_problems(expect["plan"], catalog, where)
    if expect.get("uncertain") and not case.get("notes"):
        problems.append(f"{where}: an uncertain row must say why in notes")
    readings = case.get("model", [])
    if not isinstance(readings, list):
        return [*problems, f"{where}: model must be a list of readings"]
    for index, reading in enumerate(readings):
        # The trading AI's readings of this sentence (offline eval / issue) and what propose_order must make of each.
        spot = f"{where} model[{index}]"
        if not isinstance(reading, dict) or set(reading) != {"intent", "plan"} or not isinstance(reading["intent"], dict):
            problems.append(f"{spot}: a model reading is an object with intent and plan")
            continue
        problems += [f"{spot}: unknown intent key {key!r}" for key in reading["intent"]
                     if key not in INTENT_KEYS and not key.startswith("x_")]
        problems += _plan_problems(reading["plan"], catalog, spot)
    return problems


def context_problems(contexts: dict[str, Any]) -> list[str]:
    catalog = contexts["_catalog"]
    symbols = {entry["symbol"] for entry in catalog.values()}
    quotes = contexts.get("_quotes", {})
    problems: list[str] = []
    for symbol in quotes:
        if symbol not in symbols:
            problems.append(f"_quotes: {symbol} is not a catalog symbol")
    for name, ctx in named_contexts(contexts).items():
        if ctx.get("chart") is not None and ctx["chart"] not in symbols:
            problems.append(f"{name}: chart {ctx['chart']} not in catalog")
        for position in ctx.get("positions", []):
            if position["sym"] not in symbols:
                problems.append(f"{name}: position {position['sym']} not in catalog")
            if position["vol"] != position["today"] + position["yd"]:
                problems.append(f"{name}: {position['sym']} vol != today + yd")
        for order in ctx.get("orders", []):
            if order["sym"] not in symbols or not 0 < order["left"] <= order["vol"]:
                problems.append(f"{name}: bad working order {order}")
        card = ctx.get("pending_card")
        if card and card["type"] == "trade" and card["sym"] not in symbols:
            problems.append(f"{name}: card sym {card['sym']} not in catalog")
    return problems


def _norm_price(value: Any) -> Any:
    return PRICE_ALIASES.get(value, value)


def _same_number(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return left == right


def is_unsure(got: dict[str, Any] | None) -> bool:
    return got is None or bool(got.get("uncertain"))


def intent_mismatch(expect: dict[str, Any], got: dict[str, Any] | None, *, allow_unsure: bool = True) -> list[str]:
    """Differences between an expected structure and what a parser produced; [] means acceptable.

    Only keys written in `expect` are compared (a missing key means "not checked"; an explicit null
    means "must be empty"). `plan` belongs to the planner and is skipped here, `x_*` keys are skipped
    when the parser does not output them. A parser answer of UNSURE is acceptable for the fast rules
    (`allow_unsure=True`) and never for an `uncertain` row's opposite: the row demands UNSURE.
    """
    if expect.get("uncertain"):
        return [] if is_unsure(got) else [f"must be UNSURE, got {got}"]
    if is_unsure(got):
        return [] if allow_unsure else ["parser was unsure"]
    assert got is not None
    problems: list[str] = []
    for key, want in expect.items():
        if key in {"plan", "uncertain"}:
            continue
        if key.startswith("x_") and key not in got:
            continue
        have = got.get(key)
        if key == "legs":
            have_legs = got.get("legs") or []
            if len(have_legs) != len(want):
                problems.append(f"legs: expected {len(want)}, got {len(have_legs)}")
            else:
                for index, (leg_want, leg_have) in enumerate(zip(want, have_legs, strict=True)):
                    problems += [f"legs[{index}] {item}" for item in intent_mismatch(leg_want, leg_have, allow_unsure=False)]
        elif key == "ask":
            if set(want) != set(have or []):
                problems.append(f"ask: expected {sorted(want)}, got {have}")
        elif key == "price":
            if _norm_price(want) != _norm_price(have):
                problems.append(f"price: expected {want}, got {have}")
        elif key == "cond":
            if want is None:
                if have is not None:
                    problems.append(f"cond: expected none, got {have}")
            elif not isinstance(have, dict) or any(not _same_number(have.get(k), v) for k, v in want.items()):
                problems.append(f"cond: expected {want}, got {have}")
        elif key in {"vol", "target", "frac"}:
            if want is None and have is not None or want is not None and not _same_number(have, want):
                problems.append(f"{key}: expected {want}, got {have}")
        elif have != want:
            problems.append(f"{key}: expected {want!r}, got {have!r}")
    return problems


def expected_plan(expect: dict[str, Any]) -> dict[str, Any] | None:
    """What the planner must do for a row, or None when the row does not pin the planner down."""
    if expect.get("plan"):
        return expect["plan"]
    if expect.get("uncertain") or expect.get("legs"):
        return None
    if expect.get("act") == "none" or expect.get("stance") not in {None, "order"}:
        return {"outcome": "none"}
    if expect.get("ask"):
        return {"outcome": "ask", "slots": expect["ask"]}
    return None


def _card_key(card: dict[str, Any]) -> tuple[Any, ...]:
    return (card.get("product", ""), card["side"], card["vol"], card.get("bucket", ""), _norm_price(card.get("price", "")))


def plan_mismatch(want: dict[str, Any], got: dict[str, Any] | None) -> list[str]:
    """Differences between an expected planner outcome and the planner's; [] means it matches."""
    if got is None:
        return ["planner returned nothing"]
    problems: list[str] = []
    if want["outcome"] != got.get("outcome"):
        return [f"outcome: expected {want['outcome']}, got {got.get('outcome')}"]
    for key in ("reason", "suggest"):
        if key in want and want[key] != got.get(key):
            problems.append(f"{key}: expected {want[key]!r}, got {got.get(key)!r}")
    if want["outcome"] == "ask" and "slots" in want and set(want["slots"]) != set(got.get("slots") or []):
        problems.append(f"slots: expected {want['slots']}, got {got.get('slots')}")
    if want["outcome"] == "cards":
        want_cards, got_cards = want["cards"], got.get("cards") or []
        if len(want_cards) != len(got_cards):
            return [*problems, f"cards: expected {len(want_cards)}, got {len(got_cards)}"]
        if want.get("stepwise"):
            if not got.get("stepwise"):
                problems.append("stepwise: the cards must come one at a time, each confirmed first")
            pairs = list(zip(want_cards, got_cards, strict=True))
        else:
            pairs = list(zip(sorted(want_cards, key=_card_key), sorted(got_cards, key=_card_key), strict=True))
        for index, (card_want, card_have) in enumerate(pairs):
            for key, value in card_want.items():
                have = card_have.get(key)
                if (_norm_price(value) if key == "price" else value) != (_norm_price(have) if key == "price" else have):
                    problems.append(f"cards[{index}].{key}: expected {value!r}, got {have!r}")
    return problems
