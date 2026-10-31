"""回答卡测试集 (结构化第 1 步, 2026-10-17): tests/answer_corpus/{cases.jsonl,contexts.json}, one card per row.

Each row's reads go through TurnCards.record_read as the tools returned them, its card through answer_card; the
rendered figures and compare columns, the rejection codes and the error must be what the row says. Rows marked
`pending` belong to a later step of the redesign and are skipped until it lands; `conclusion` / `spoken` / `reasons` /
`risk` / `symbol` / `title` are compared when a row states them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trader_runtime.answer_cards import TurnCards
from trader_runtime.contracts import validate_event

ROOT = Path(__file__).parent / "answer_corpus"
CONTEXTS: dict[str, Any] = json.loads((ROOT / "contexts.json").read_text(encoding="utf-8"))
CASES = [json.loads(line) for line in (ROOT / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
FIELDS = {"id", "ctx", "src", "card", "expect", "notes", "pending"}
EXPECT = {"figures", "compare", "dropped", "error", "conclusion", "spoken", "reasons", "risk", "symbol", "title"}


def test_every_row_is_well_formed_and_every_context_used():
    ids = [case["id"] for case in CASES]
    assert ids == sorted(set(ids)), "ids are unique and stay in file order"
    for case in CASES:
        assert set(case) <= FIELDS and {"id", "ctx", "src", "card", "expect"} <= set(case), case["id"]
        assert case["ctx"] in CONTEXTS and set(case["expect"]) <= EXPECT, case["id"]
        assert case["src"].split(":", 1)[0] in {"commit", "repo", "prod-test", "gap"}, case["id"]
    assert set(CONTEXTS) - {"_doc"} == {case["ctx"] for case in CASES}


def _run(case: dict[str, Any]) -> tuple[dict[str, Any] | None, TurnCards, str | None]:
    cards = TurnCards("t")
    for read in CONTEXTS[case["ctx"]]:
        cards.record_read(read["tool"], read.get("args") or {}, json.dumps({"data": read["data"]}, ensure_ascii=False))
    card, _, error = cards.answer_card({"title": "测试卡", "conclusion": "看卡片。", **case["card"]})
    return card, cards, error


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_answer_card_rows(case: dict[str, Any]):
    if case.get("pending"):
        pytest.skip(f"{case['pending']}: 后续步骤启用")
    card, cards, error = _run(case)
    expect = case["expect"]
    assert ("answer_card_invalid" if error else None) == expect.get("error")
    assert cards.rejections == expect.get("dropped", [])
    if card is None:
        return
    assert card["figures"] == expect.get("figures", [])
    compare = card["compare"]
    assert ({side: compare[side]["lines"] for side in ("left", "right")} if compare else None) == expect.get("compare")
    cards.accept(card)
    for key, shown in (("conclusion", card["conclusion"]), ("spoken", cards.spoken), ("reasons", card["reasons"]),
                       ("risk", card["risk"]), ("symbol", card["symbol"]), ("title", card["title"])):
        if key in expect:
            assert shown == expect[key], key
    validate_event({"schemaVersion": 1, "sessionId": "s", "id": "e", "kind": "artifact", "name": "card.answer",
                    "timestamp": "2026-10-17T05:00:00Z", "payload": card})
