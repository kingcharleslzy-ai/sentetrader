"""回测结果的“AI 怎么看” (策略页改版第八批, 2026-10-08).

Two steps, so a number on the page can only ever come from the backtest itself:

1. ``verdict_facts`` (deterministic, no model) reads the finished result and states the facts a
   reader should know before trusting it — how much of the profit one month made and what is left
   without that month, too few trades, fees not counted, no trade at all, and whether the
   parameters were picked on this same stretch of history (``tuned_on_same_period``: a template
   strategy whose parameters are no longer the template's defaults — someone changed them after
   looking at recent backtests, or picked them from a comparison). Every fact is a finished plain
   sentence and carries the numbers it quotes.
2. ``model_verdict`` asks the user's model to say those facts in one or two plain sentences. The
   reply is accepted only when every number in it is one of the facts' numbers (Arabic digits only;
   a Chinese-numeral amount or a figure the facts do not contain rejects it) and it stays short;
   otherwise — no model, a failure, a timeout, an invented number — the page shows the fact
   sentences themselves (``source: facts``).

Nothing here changes a strategy, a backtest or an order.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

log = logging.getLogger("uvicorn.error.strategy-verdict")
CHINA = ZoneInfo("Asia/Shanghai")
FEW_TRADES = 10            # fewer closed trades than this: too few to say much
CONCENTRATED = 0.4         # one month making this share of the profit is worth saying
MAX_CHARS = 120
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# “五千九”“一万二”“三十” — an amount written in Chinese numerals cannot be checked, so it is refused.
CHINESE_AMOUNT = re.compile(r"[零一二两三四五六七八九十]+[十百千万亿]|百分之")
INSTRUCTIONS = (
    "你是期货策略回测的讲解员。下面是程序从一次回测里算出的事实句（facts），每句都已核对过数字。"
    "请用一到两句大白话把最要紧的意思说给不懂术语的人听：先说能不能赚钱，再说最该当心的一点。"
    "只能用 facts 里出现过的数字，数字用阿拉伯数字照抄，不要自己计算新数字、不要用中文数字、不要百分号以外的符号，"
    "不要提指标名和英文缩写，不要给交易建议。"
)


def money(value: float) -> str:
    """12040 → “12,040” (whole yuan; the sign is said in words)."""
    return f"{abs(round(float(value))):,}"


def tuned_on_same_period(definition: dict[str, Any] | None) -> bool:
    """A template strategy whose parameters are not the template's defaults any more: they were
    changed (by hand, by the AI, by 按建议改 or chosen from a comparison) after looking at backtests
    of the same recent history this backtest reads."""
    source = (definition or {}).get("templateSource") or {}
    return bool(source.get("templateId")) and source.get("defaults") is False


def _months(trades: list[dict[str, Any]]) -> dict[str, float]:
    by_month: dict[str, float] = {}
    for trade in trades:
        moment = datetime.fromtimestamp(int(trade["closeTime"]) / 1e9, UTC).astimezone(CHINA)
        key = f"{moment.year}-{moment.month:02d}"
        by_month[key] = by_month.get(key, 0.0) + float(trade["net"])
    return by_month


def verdict_facts(result: dict[str, Any], *, tuned: bool = False) -> dict[str, Any]:
    """{facts: [{id, text, tone, numbers}]} for one finished backtest result (numbers as written)."""
    summary = result.get("summary") or {}
    trades = result.get("trades") or []
    count = int(summary.get("trades") or 0)
    net = float(summary.get("netProfit") or 0)
    facts: list[dict[str, Any]] = []

    def add(identifier: str, text: str, numbers: list[str], tone: str = "info") -> None:
        facts.append({"id": identifier, "text": text, "tone": tone, "numbers": numbers})

    if count == 0:
        add("no_trades", "这段时间一笔都没开出来，规则可能太严，或者这段行情碰不到它的条件。", [], "warning")
    else:
        months = _months(trades) if not result.get("tradesTruncated") else {}
        if net > 0 and len(months) >= 2:
            best_key = max(months, key=lambda key: months[key])
            best = months[best_key]
            month = str(int(best_key.split("-")[1]))
            share = round(best / net * 100) if best > 0 else 0
            rest = net - best
            if share >= CONCENTRATED * 100:
                tail = f"去掉那个月只赚 {money(rest)}" if rest > 0 else f"去掉那个月反而亏 {money(rest)}"
                add("concentration", f"能赚钱，但赚的钱有 {share}% 来自 {month} 月那一段；{tail}。",
                    [str(share), month, money(rest)], "warning" if share >= 50 or rest <= 0 else "info")
            else:
                add("spread_out", f"能赚钱，而且是分几个月慢慢赚的：最赚的 {month} 月也只占 {share}%。",
                    [month, str(share)])
        elif net > 0:
            add("profit", f"能赚钱：一共赚了 {money(net)}。", [money(net)])
        else:
            worst_key = min(months, key=lambda key: months[key]) if months else None
            if worst_key is not None and months[worst_key] < 0:
                month = str(int(worst_key.split("-")[1]))
                add("loss", f"这段时间按这个做是亏的，一共亏了 {money(net)}，亏得最多的是 {month} 月。",
                    [money(net), month], "warning")
            else:
                add("loss", f"这段时间按这个做是亏的，一共亏了 {money(net)}。", [money(net)], "warning")
        if count < FEW_TRADES:
            add("few_trades", f"只做了 {count} 笔，次数太少，说明不了太多。", [str(count)], "warning")
    if (result.get("feeEstimate") or {}).get("source") == "none" and count:
        add("no_fees", "这次没扣手续费，实际结果会差一些。", [], "warning")
    if tuned:
        add("tuned", "提醒：参数是在同一段行情里挑出来的，实盘可能没这么好。", [], "warning")
    return {"facts": facts}


def fact_text(verdict: dict[str, Any]) -> str:
    """The fact sentences as they stand (what the page shows without a model sentence)."""
    return "".join(item["text"] for item in verdict.get("facts") or [])


def check_sentence(text: str, verdict: dict[str, Any]) -> str | None:
    """None when `text` may stand for the facts; otherwise why not (for the log)."""
    text = text.strip()
    if not text:
        return "empty"
    if len(text) > MAX_CHARS:
        return "too_long"
    if len(re.findall(r"[。！？!?]", text)) > 2:
        return "too_many_sentences"
    if re.search(r"[A-Za-z]", text):
        return "jargon"
    if CHINESE_AMOUNT.search(text):
        return "chinese_numeral"
    allowed = {number.replace(",", "") for item in verdict.get("facts") or [] for number in item.get("numbers") or []}
    for number in NUMBER.findall(text):
        if number.replace(",", "") not in allowed:
            return f"number_not_in_facts:{number}"
    return None


class VerdictSentence(BaseModel):
    text: str = Field(description="一到两句大白话，只用 facts 里的数字")


async def model_verdict(generator: Any, verdict: dict[str, Any], *, user_id: str | None = None,
                        timeout_seconds: float = 30) -> dict[str, Any]:
    """{text, source: model|facts}: the model's plain sentence when it passes ``check_sentence``,
    else the fact sentences."""
    fallback = {"text": fact_text(verdict), "source": "facts"}
    facts = verdict.get("facts") or []
    if not facts or generator is None or not hasattr(generator, "complete_structured") \
            or not getattr(generator, "agent_authenticated", True):
        return fallback
    try:
        result = await generator.complete_structured(
            instructions=INSTRUCTIONS, input_text=json.dumps({"facts": [item["text"] for item in facts]}, ensure_ascii=False),
            schema=VerdictSentence, timeout_seconds=timeout_seconds, usage_user=user_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 — any model failure falls back to the facts
        log.warning("strategy_verdict_model_failed error=%s", type(error).__name__)
        return fallback
    text = str(getattr(result.value, "text", "") or "").strip()
    problem = check_sentence(text, verdict)
    if problem is not None:
        log.info("strategy_verdict_rejected reason=%s", problem)
        return fallback
    return {"text": text, "source": "model"}
