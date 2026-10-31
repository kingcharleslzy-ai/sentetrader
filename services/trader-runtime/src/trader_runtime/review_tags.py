"""今日复盘的标签规则 (所有者 2026-10-06)：计划内 / 提醒后 / 临时，按成交来源和提醒时间判定，不靠模型。
纯函数，没有库、没有时钟；复盘、平仓记录、帮助和“怎么算的”折叠共用这里的规则与原话。"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .instruments import foreign_product_of

NS = 1_000_000_000
REMIND_WINDOW_SECONDS = 15 * 60
# 强平 (#1204) is the paper engine closing for the user, not an impulse of theirs — never 临时.
PLANNED_SOURCES = frozenset({"strategy", "stop", "ai", "liquidation"})
TAG_LABELS = {"plan": "计划内", "remind": "提醒后", "adhoc": "临时"}

# The fold under the block (怎么算的) and the help say the rule in these same words.
TAG_RULES = (
    "计划内：成交来自策略信号、止损止盈或条件单、自主执行、本站模拟盘强平；AI 出的开仓 / 平仓卡经你确认、下单前 15 分钟内没有同品种提醒的也算",
    "提醒后：同一品种在你下单前 15 分钟内出现过 Sente 提醒（盘中异动、到价、收盘前持仓、风险度；风险度对账户里所有品种都算），"
    "这笔是你自己下的——包括你一句话让 AI 出的卡、在提醒卡上点“平一半”出的卡",
    "临时：其余你手动下的单",
    "夜盘成交算到下一个交易日；数字取自账户实际成交、平仓配对和账户当日盈亏",
)


def product_key(symbol: str) -> str:
    """SHFE.rb2701 / KQ.m@SHFE.rb → SHFE.RB: the product a reminder and an order are compared on. A foreign
    contract — an MT5 month (CME.GCEZ26) or the 快期外盘主连 (KQD.m@COMEX.GC) — is its catalog product
    (COMEX.GC, instruments.foreign_product_of), so a reminder on the chart's 主连 matches an order on the month."""
    product = foreign_product_of(str(symbol or ""))
    if product is not None:
        return product.key.upper()
    text = re.sub(r"^KQD?\.[A-Z]@", "", str(symbol or "").upper())
    exchange, _, instrument = text.partition(".")
    match = re.match(r"[A-Z]+", instrument)
    return f"{exchange}.{match.group(0)}" if exchange and match else text


@dataclass(frozen=True)
class Reminder:
    """One Sente reminder as the tag rule reads it: when, what kind, what it said, and the products it was
    about (None = the whole account: 风险度, or a 收盘前 row written before the products were kept)."""
    at_ns: int
    kind: str
    text: str
    products: frozenset[str] | None


def tag_for(*, source: str, placed_ns: int, symbol: str, reminders: Iterable[Reminder]) -> tuple[str, Reminder | None]:
    """The owner's rule (2026-10-06), in priority order: ① strategy / 止损止盈 / 自主执行 → plan; ② a reminder
    about the same product at most 15 minutes before the order was placed, for an order the user placed
    (their ticket or an AI card they confirmed) → remind; ③ an AI card they confirmed → plan; ④ adhoc.
    A reminder after the order does not count. Returns the tag and the reminder that made it 提醒后."""
    if source in PLANNED_SOURCES:
        return "plan", None
    product = product_key(symbol)
    hits = [item for item in reminders
            if 0 <= placed_ns - item.at_ns <= REMIND_WINDOW_SECONDS * NS
            and (item.products is None or product in item.products)]
    if hits:
        return "remind", max(hits, key=lambda item: item.at_ns)
    if source == "card":
        return "plan", None
    return "adhoc", None
