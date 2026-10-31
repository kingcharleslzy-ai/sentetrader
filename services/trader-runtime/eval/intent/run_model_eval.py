"""Offline model pass over the order-intent corpus (docs/ORDER_INTENT_V2.md section 3, level 3).

Not part of CI: it costs model calls and its answers vary. Run it before a release and when the trading model
changes. It feeds the sentences the fast parser leaves Uncertain (the ones the typed router hands to the trading
AI, 意图重构第 3 步) to the trading model with the real `propose_order` tool definition, `--repeat` times each,
validates every call with the Runtime's own schema (ProposeOrderArguments → OrderIntent) and grounding
(order_planner.ground: every key slot must rest on the words or the context), offers the tool only on rows whose
typed route does (classify_turn: interface, market and no-card turns never get it), compares only the
*structure* with the row's `expect` (never the wording of a reply), and reports:

  * intent accuracy  rows where every run was right / rows scored
  * false-card rate  rows that expect no card (question, reported, negated, conditional, retracted, uncertain,
                     act none, 一句多单 — propose_order refuses legs) where any run produced a card-making intent
  * missed-card rate rows that expect a card where no run produced one
  * consistency      rows whose runs disagree (the model is not stable on this sentence)

    uv run --project . python eval/intent/run_model_eval.py --model deepseek-flash \
        [--keychain com.kingcharles.ai-trader/deepseek-api-key | DEEPSEEK_API_KEY in the env] \
        [--repeat 3] [--limit 30] [--only c101,c102] [--out eval/intent/results.json]
    uv run --project . python eval/intent/run_model_eval.py --model fake   # plumbing check, no network

Rules: scores use `corpus.intent_mismatch(expect, got, allow_unsure=False)`, the same helper as the CI tests, so one
reading of "right" is shared. A row marked `uncertain` is right when the model asks (no call, or a call with the
unsure slot left empty — the planner asks) and never when it cards a guess. A conditional call (`cond` filled) is no
card: the planner refuses it. Results are written next to this file and not committed.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PY = ROOT / "tests" / "intent_corpus" / "corpus.py"
CARD_ACTS = {"open", "close", "close_all", "target", "reverse"}
# Fields the model's raw call carries alongside the validated view; the consistency check
# compares behavior, so two runs whose grounded intent matches but whose raw JSON differs
# (field order, unfilled slots) must not count as inconsistent.
DIAGNOSTIC_KEYS = {"raw"}
INSTRUCTIONS = (
    "你是 Sente 的交易助手，正在模拟账户里为唯一用户出待确认的下单卡。用户要下单（开仓、平仓、全平、调到目标持仓、反手）"
    "时只调用 propose_order，只填用户说了的：没说的方向、手数、合约留空（Runtime 会追问），不要猜，也不要先读数。"
    "一句里有几单时写进 legs（Runtime 不出卡，请用户一单一单说）。问句、转述已做过的事、否定、条件单（到价/到时间才下）、用户自己收回的话不是下单：不调用工具，"
    "用一句话回答。合约：说了月份写原话或代码，只说品种写品种名，没说不填（用窗口合约）。"
)


def load_corpus_module():
    spec = importlib.util.spec_from_file_location("intent_corpus_helpers", CORPUS_PY)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = module  # type: ignore[union-attr]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def card_tool() -> dict[str, Any]:
    """The propose_order definition exactly as the trading AI's request carries it."""
    from trader_runtime.native_agent import wire_schema
    from trader_runtime.trader_tools import TraderReadOnlyTools
    tools = TraderReadOnlyTools(SimpleNamespace(mode="paper"), lambda mode: {}, proposals=SimpleNamespace())
    definition = next(item for item in tools.definitions() if item["name"] == "propose_order")
    return {"type": "function", "name": "propose_order", "description": definition["description"],
            "parameters": wire_schema(definition["inputSchema"]), "strict": False}


def uncertain_rows(corpus, cases: list[dict[str, Any]], contexts: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows the fast parser leaves Uncertain in their named context — the trading AI's sentences."""
    from trader_runtime.order_intent_parse import ParseContext, Uncertain, parse
    catalog = contexts["_catalog"]
    by_symbol = {entry["symbol"]: name for name, entry in catalog.items()}
    rows = []
    for case in cases:
        ctx = contexts[case["ctx"]]
        held: dict[str, set[str]] = {}
        for row in ctx.get("positions") or []:
            held.setdefault(by_symbol[row["sym"]], set()).add(row["dir"])
        parsed = parse(case["say"], ParseContext(
            products=tuple(catalog), chart=ctx["chart"], chart_product=by_symbol.get(ctx["chart"]),
            pending_card=bool(ctx.get("pending_card")), held={k: frozenset(v) for k, v in held.items()}))
        if isinstance(parsed, Uncertain) and not ctx.get("pending_card"):
            rows.append(case)
    return rows


def context_line(ctx: dict[str, Any], catalog: dict[str, Any]) -> str:
    """The facts a typed turn carries: the window's contract, the positions, the catalog's names."""
    names = {entry["symbol"]: name for name, entry in catalog.items()}
    held = "；".join(f"{names.get(row['sym'], row['sym'])} {row['sym']} {'多' if row['dir'] == 'long' else '空'} {row['vol']} 手"
                    for row in ctx.get("positions") or []) or "无"
    products = "、".join(f"{name}={entry['symbol']}" for name, entry in catalog.items())
    return f"当前窗口合约：{ctx.get('chart') or '无'}。持仓：{held}。合约目录：{products}。\n"


def view(intent: Any, catalog: dict[str, Any]) -> dict[str, Any]:
    """An intent (the model's, after the Runtime's validation and grounding) in the corpus's terms (README `expect`)."""
    by_symbol = {entry["symbol"].lower(): name for name, entry in catalog.items()}
    aliases = {alias.lower(): name for name, entry in catalog.items() for alias in [name, *entry.get("aliases", [])]}

    def one(leg) -> dict[str, Any]:
        out: dict[str, Any] = {"act": leg.act, "stance": leg.stance, "dir": leg.dir, "vol": leg.vol, "target": leg.target,
                               "frac": leg.frac, "cond": leg.cond, "bucket": leg.bucket,
                               "price": f"limit:{leg.price.limit:g}" if leg.price.limit is not None else leg.price.mode}
        word = str(leg.symbol or (leg.symbols[0] if len(leg.symbols) == 1 else ""))
        codes = {entry["symbol"].split(".", 1)[1].lower(): entry["symbol"].lower() for entry in catalog.values()}
        code = by_symbol.get(word.lower()) and word.lower() or codes.get(word.lower())
        if not word:
            out["product"] = "@all" if leg.act == "close_all" and not leg.symbols else "@chart"
        elif code:
            out["product"] = by_symbol[code]
            out["symbol"] = out["product"] + re.sub(r"^\D*", "", code.split(".", 1)[1])
        else:
            bare = re.sub(r"\d+$", "", word)
            out["product"] = aliases.get(bare.lower(), bare)
            if bare != word:
                out["symbol"] = f"{out['product']}{word[len(bare):]}"
        if leg.act == "open":
            missing = [slot for slot, gone in (("dir", leg.dir is None), ("vol", leg.vol is None)) if gone]
            if missing:
                out["ask"] = missing
        return out
    if intent.legs:
        return {"legs": [one(leg) for leg in intent.legs]}
    return one(intent)


def makes_card(got: dict[str, Any] | None) -> bool:
    if not got or got.get("uncertain") or got.get("invalid"):
        return False
    legs = got.get("legs") or [got]
    return any(leg.get("act") in CARD_ACTS and not leg.get("cond") and not leg.get("ask")
               and not (leg.get("act") == "close" and leg.get("vol") is None and leg.get("frac") is None)
               for leg in legs)


def expects_card(expect: dict[str, Any]) -> bool:
    if expect.get("uncertain"):
        return False
    if expect.get("legs"):
        return False  # 一句多单：propose_order refuses it (legs_entangled) — one order at a time, no card
    return (expect.get("act") in CARD_ACTS and expect.get("stance") in {None, "order"} and not expect.get("ask"))


async def extract_intent(case: dict[str, Any], *, context: dict[str, Any], catalog: dict[str, Any], model: str,
                         client: Any, tool: dict[str, Any]) -> dict[str, Any]:
    """The sentence the way a typed turn meets it: the router decides whether the turn offers propose_order at all
    (an interface round, a market read or a no-card turn never does: {"uncertain": True, "route": kind}); then one
    model call, the Runtime's validation and its grounding (order_planner.ground: an unvouched slot becomes a
    question, {"uncertain": True, "asked": slot}); else the grounded intent in corpus terms. No call = uncertain."""
    from pydantic import ValidationError

    from trader_runtime.order_planner import Ask, ground
    from trader_runtime.trader_tools import ProposeOrderArguments, proposed_intent
    from trader_runtime.turn_router import CARD_TOOL, classify_turn
    route = classify_turn(case["say"], has_pending_card=False, ui_available=True, catalog=list(catalog))
    offered = route.kind == "order_intent" or (not route.no_card and (route.tool_names is None
                                                                       or CARD_TOOL in route.tool_names))
    if not offered:
        return {"uncertain": True, "route": route.kind}
    if model == "fake":  # plumbing check: an oracle that answers every row with its own expectation
        expect = case["expect"]
        return {"uncertain": True} if expect.get("uncertain") or not expects_card(expect) else dict(expect)
    request = {"model": model, "instructions": INSTRUCTIONS, "tools": [tool], "temperature": 0.0,
               "input": [{"role": "user", "content": context_line(context, catalog) + case["say"]}]}
    if model.startswith("deepseek"):
        request["reasoning"] = {"effort": "none"}
    response = await client.responses.create(**request)
    calls = [item for item in response.output if getattr(item, "type", "") == "function_call"
             and getattr(item, "name", "") == "propose_order"]
    if not calls:
        return {"uncertain": True}
    try:
        args = json.loads(calls[0].arguments or "{}")
        intent = proposed_intent(ProposeOrderArguments.model_validate(args))
    except (ValueError, ValidationError):
        return {"invalid": True}
    if intent.legs:
        return {"uncertain": True, "refused": "legs_entangled", "raw": args}  # the Runtime's refusal, like propose_order
    positions = [{"symbol": row["sym"], "direction": row["dir"], "volume": row["vol"]}
                 for row in context.get("positions") or []]
    items = [{"symbol": entry["symbol"], "exchangeId": entry["symbol"].split(".")[0],
              "instrumentId": entry["symbol"].split(".")[1], "instrumentName": entry["name"],
              "productId": re.sub(r"\d+$", "", entry["symbol"].split(".")[1]), "isMain": True}
             for entry in catalog.values()]
    grounded = ground(intent, case["say"], products=tuple(catalog), chart=context.get("chart"), positions=positions,
                      items=items)
    if isinstance(grounded, Ask):
        return {"uncertain": True, "asked": grounded.missing, "raw": args}
    return {**view(grounded, catalog), "raw": args}


def right(expect: dict[str, Any], got: dict[str, Any], corpus) -> bool:
    if expect.get("uncertain"):
        return not makes_card(got)
    if not expects_card(expect):
        if expect.get("ask"):
            return not makes_card(got) and not corpus.intent_mismatch(
                {key: value for key, value in expect.items() if key in {"act", "product", "ask"}}, got, allow_unsure=True)
        return not makes_card(got)
    if got.get("uncertain") or got.get("invalid"):
        return False
    compared = {key: value for key, value in expect.items() if key not in {"topic", "plan"} and not key.startswith("x_")}
    return not corpus.intent_mismatch(compared, got, allow_unsure=False)


def score(rows: list[dict[str, Any]], runs: dict[str, list[dict[str, Any]]], corpus) -> dict[str, Any]:
    """The metrics above from {case id: [run result, ...]}."""
    detail = []
    accurate = false_cards = missed = unstable = 0
    no_card_rows = card_rows = 0
    for case in rows:
        results = runs[case["id"]]
        expect = case["expect"]
        oks = [right(expect, got, corpus) for got in results]
        carded = [makes_card(got) for got in results]
        accurate += all(oks)
        if expects_card(expect):
            card_rows += 1
            missed += not any(carded)
        else:
            no_card_rows += 1
            false_cards += any(carded)
        stable = len({json.dumps({k: v for k, v in got.items() if k not in DIAGNOSTIC_KEYS},
                                 sort_keys=True, ensure_ascii=False) for got in results}) == 1
        unstable += not stable
        detail.append({"id": case["id"], "say": case["say"], "ok": oks, "carded": carded, "stable": stable,
                       "expect": expect, "got": results})

    def rate(part: int, whole: int) -> float | None:
        return round(part / whole, 3) if whole else None
    return {"rows": len(rows), "runsPerRow": len(next(iter(runs.values()))) if runs else 0,
            "intentAccuracy": rate(accurate, len(rows)), "falseCardRate": rate(false_cards, no_card_rows),
            "missedCardRate": rate(missed, card_rows), "inconsistentRows": unstable,
            "noCardRows": no_card_rows, "cardRows": card_rows, "detail": detail}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    corpus = load_corpus_module()
    contexts = corpus.load_contexts()
    named = corpus.named_contexts(contexts)
    wanted = {item for item in args.only.split(",") if item}
    rows = [case for case in uncertain_rows(corpus, corpus.load_cases(), contexts) if not wanted or case["id"] in wanted]
    rows = rows[:args.limit] if args.limit else rows
    client = None
    request_model = args.model
    if args.model != "fake":
        from openai import AsyncOpenAI

        from trader_runtime.native_agent import build_model_endpoints
        key = os.getenv("DEEPSEEK_API_KEY")
        if args.keychain:  # macOS Keychain "service/account": the key never touches the shell or a file
            import subprocess
            service, _, account = args.keychain.partition("/")
            key = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                                 check=True, capture_output=True, text=True).stdout.strip()
        endpoints = build_model_endpoints(default_provider="deepseek", deepseek_api_key=key)
        endpoint = endpoints.get(args.model)
        if endpoint is None:
            raise SystemExit(f"model {args.model} has no key in this environment (see trader_runtime.model_provider)")
        client = AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key, timeout=60)
        # The request carries the provider's own model id when it differs from the catalog slug
        # (Kimi Code calls K3 `k3`); `args.model` stays the slug in the report.
        request_model = endpoint.upstream or endpoint.slug
    tool = card_tool()
    runs: dict[str, list[dict[str, Any]]] = {}
    for case in rows:
        runs[case["id"]] = [await extract_intent(case, context=named[case["ctx"]], catalog=contexts["_catalog"],
                                                 model=request_model, client=client, tool=tool)
                            for _ in range(args.repeat)]
    result = score(rows, runs, corpus)
    result["model"] = args.model
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="a catalog model slug with its key in the env, or fake")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="score only the first N uncertain rows (0 = all)")
    parser.add_argument("--only", default="", help="comma separated case ids")
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.json")))
    parser.add_argument("--keychain", default="", help="read the DeepSeek key from the macOS Keychain: service/account")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {key: value for key, value in result.items() if key != "detail"}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
