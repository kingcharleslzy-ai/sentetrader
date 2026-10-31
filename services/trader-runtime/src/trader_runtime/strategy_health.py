"""策略体检 (2026-09-26): which entry condition keeps a rule strategy from ever opening, and what
structural change would let it.

The rule backtester hands every judged bar's evaluation to an ``EntryHealth`` observer. For each
entry group whose conditions must all hold on the same bar (join "all", no 先后顺序, no 方向分段确认,
no 回调结构) it counts, per condition, how often it holds alone, how often all of them hold, how
often all the others hold ("without" it), and — for 上穿 / 下穿 — how often the group would hold if
each cross may have happened within the last 3 bars. Every count is of occurrences, not bars: a run
of consecutive bars on which a set holds is one signal. A plain 任一满足 group (join "any") is counted
too — each condition alone and the group (`anyPass`) — for the 策略编辑卡's per-row counts (2026-10-08);
the advice and the 体检 lines only ever read the "all" groups.

Advice is structural only — drop one condition, or let the crosses happen within 3 bars — and is
ranked and described by how many times the entry group would hold, never by profit: the guide
must not tune parameters on history ("不得根据历史行情优化参数"). Pure; no I/O.
"""
from __future__ import annotations

import copy
from typing import Any

from .strategy_rules import StrategyPlan, group_conditions

CROSS_OPERATORS = frozenset({"cross_up", "cross_down"})
RELAX_BARS = 3
ENTRY_GROUPS = ("entryLong", "entryShort")
GROUP_WORDS = {"entryLong": "开多", "entryShort": "开空"}
MAX_ADVICE = 2
ADVICE_NOTE = "以下建议只按开仓次数给，不按盈亏挑参数。"
_MASK = (1 << RELAX_BARS) - 1


def diagnosable_groups(plan: StrategyPlan) -> list[str]:
    """Entry groups of plain conditions on one bar (no 先后顺序, 方向确认 or 条件组合), 全部满足 or 任一满足;
    none when the plan uses 回调结构."""
    if plan.pullback is not None:
        return []
    names = []
    for name in ENTRY_GROUPS:
        group = getattr(plan.rules, name)
        if group is None or group.sequence or group.alignmentConditions or group.branches:
            continue
        names.append(name)
    return names


def checked_groups(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """The 全部满足 groups: the only ones the advice and the 体检 lines judge."""
    return {name: item for name, item in diagnosis.items() if item.get("join", "all") == "all"}


class _GroupTally:
    """Counts OCCURRENCES (rising edges): a run of consecutive judged bars on which a set of
    conditions holds counts once, like one signal. `judged` counts bars."""
    __slots__ = ("join", "labels", "cross", "relax", "relaxable", "judged", "all_pass", "any_pass", "alone", "without",
                 "relaxed", "recent", "was_alone", "was_all", "was_any", "was_without", "was_relaxed")

    def __init__(self, conditions: list[Any], join: str = "all") -> None:
        self.join = join
        self.labels = [condition.label for condition in conditions]
        self.cross = [condition.operator in CROSS_OPERATORS for condition in conditions]
        # A cross already allowed RELAX_BARS or more bars is not relaxed any further.
        self.relax = [i for i, condition in enumerate(conditions)
                      if condition.operator in CROSS_OPERATORS and condition.withinBars < RELAX_BARS]
        size = len(conditions)
        self.relaxable = [i in self.relax for i in range(size)]
        self.judged = self.all_pass = self.any_pass = self.relaxed = 0
        self.alone = [0] * size
        self.without = [0] * size       # occurrences where every other condition holds
        self.recent = [0] * size        # bitmask of this condition's passes on the last RELAX_BARS judged bars
        self.was_alone = [False] * size
        self.was_without = [False] * size
        self.was_all = self.was_any = self.was_relaxed = False

    def add(self, passes: list[bool]) -> None:
        size = len(self.labels)
        if len(passes) != size:
            return
        self.judged += 1
        failed, failing = 0, -1
        alone, was_alone = self.alone, self.was_alone
        for i, ok in enumerate(passes):
            if ok:
                if not was_alone[i]:
                    alone[i] += 1
            else:
                failed += 1
                failing = i
            was_alone[i] = ok
        now_all = failed == 0
        if now_all and not self.was_all:
            self.all_pass += 1
        self.was_all = now_all
        now_any = failed < size
        if now_any and not self.was_any:
            self.any_pass += 1
        self.was_any = now_any
        without, was_without = self.without, self.was_without
        for i in range(size):
            others = now_all or (failed == 1 and failing == i)
            if others and not was_without[i]:
                without[i] += 1
            was_without[i] = others
        if self.relax:
            recent = self.recent
            for i in self.relax:
                recent[i] = ((recent[i] << 1) | passes[i]) & _MASK
            now = all(recent[i] if relaxed else ok
                      for i, (ok, relaxed) in enumerate(zip(passes, self.relaxable, strict=True)))
            if now and not self.was_relaxed:
                self.relaxed += 1
            self.was_relaxed = now

    def report(self) -> dict[str, Any]:
        return {"join": self.join, "judgedBars": self.judged, "allPass": self.all_pass, "anyPass": self.any_pass,
                "relaxedCrossPass": self.relaxed if self.relax else None,
                "conditions": [{"label": label, "alone": alone, "without": without, "cross": cross}
                               for label, alone, without, cross in zip(self.labels, self.alone, self.without, self.cross,
                                                                       strict=True)]}


class EntryHealth:
    """The observer ``strategy_engine.step`` calls with each judged bar's evaluation."""

    def __init__(self, plan: StrategyPlan) -> None:
        self.tallies = {name: _GroupTally(group_conditions(getattr(plan.rules, name)), getattr(plan.rules, name).join)
                        for name in diagnosable_groups(plan)}

    def __bool__(self) -> bool:
        return bool(self.tallies)

    def __call__(self, evaluation: dict[str, Any]) -> None:
        buckets: dict[str, list[bool]] = {name: [] for name in self.tallies}
        for detail in evaluation["conditions"]:
            bucket = buckets.get(detail.get("group"))
            if bucket is not None:
                bucket.append(bool(detail.get("hits")))
        for name, passes in buckets.items():
            self.tallies[name].add(passes)

    def diagnosis(self) -> dict[str, Any]:
        return {name: tally.report() for name, tally in self.tallies.items()}


def needs_advice(diagnosis: dict[str, Any], trades: int) -> bool:
    checked = checked_groups(diagnosis)
    return bool(checked) and (trades == 0 or any(item["allPass"] == 0 for item in checked.values()))


def _counts(passes: dict[str, int]) -> str:
    return "、".join(f"{GROUP_WORDS[name]}约 {count} 次" for name, count in passes.items())


def _labels(diagnosis: dict[str, Any], groups: list[str], index: int) -> list[str]:
    labels: list[str] = []
    for name in groups:
        label = diagnosis[name]["conditions"][index]["label"]
        if label not in labels:
            labels.append(label)
    return labels


def _target(labels: list[str]) -> str:
    return "和".join(f"「{label}」" for label in labels)


def build_advice(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
    """At most MAX_ADVICE structural suggestions, drops first. Ranked by entry occurrences, never by profit."""
    diagnosis = checked_groups(diagnosis)
    drops: dict[str, int] = {}
    for name, item in diagnosis.items():
        conditions = item["conditions"]
        if len(conditions) < 2:    # dropping the only condition would empty the group
            continue
        best = max(range(len(conditions)), key=lambda i: conditions[i]["without"])
        if conditions[best]["without"] > item["allPass"]:
            drops[name] = best
    advice: list[dict[str, Any]] = []
    indices = set(drops.values())
    batches = [list(drops)] if len(indices) == 1 else [[name] for name in drops]
    for groups in batches:
        index = drops[groups[0]]
        labels = _labels(diagnosis, groups, index)
        passes = {name: diagnosis[name]["conditions"][index]["without"] for name in groups}
        advice.append({"id": f"drop-{index}",
                       "kind": "drop", "groups": groups, "index": index, "label": "／".join(labels),
                       "passes": passes, "text": f"去掉{_target(labels)}：{_counts(passes)}"})
    relax = {name: item["relaxedCrossPass"] for name, item in diagnosis.items()
             if item["relaxedCrossPass"] is not None and len(item["conditions"]) >= 2
             and item["relaxedCrossPass"] > item["allPass"]}
    if relax:
        advice.append({"id": "relax-cross", "kind": "relaxCross", "groups": list(relax), "withinBars": RELAX_BARS,
                       "passes": relax,
                       "text": f"“上穿/下穿”放宽到最近 {RELAX_BARS} 根K线内发生（不必同一根）：{_counts(relax)}"})
    return advice[:MAX_ADVICE]


def headline(diagnosis: dict[str, Any], advice: list[dict[str, Any]], *, trading_days: int | None, trades: int) -> str:
    """One line: how often each entry group held over the backtest and where it is stuck."""
    diagnosis = checked_groups(diagnosis)
    counts = "、".join(f"{GROUP_WORDS[name]}{'条件同时满足 ' if index == 0 else ' '}{item['allPass']} 次"
                       for index, (name, item) in enumerate(diagnosis.items()))
    span = f"近 {trading_days} 个交易日" if trading_days else "回测区间内"
    no_trades = "，回测没有成交" if trades == 0 and all(item["allPass"] > 0 for item in diagnosis.values()) else ""
    drops = [item for item in advice if item["kind"] == "drop"]
    if drops:
        stuck = "卡在" + "、".join(_target(_labels(diagnosis, item["groups"], item["index"])) for item in drops)
    elif advice:
        stuck = "卡在几个“上穿/下穿”要落在同一根K线上"
    else:
        stuck = "单去掉一个条件或放宽交叉都不够，建议少叠几个条件"
    return f"按现在的规则，{span}{counts}{no_trades}，{stuck}。" + (ADVICE_NOTE if advice else "")


def passed_line(diagnosis: dict[str, Any], *, trading_days: int | None, trades: int) -> str:
    """回测与体检 found nothing stuck: say so, with how often each entry group held."""
    diagnosis = checked_groups(diagnosis)
    span = f"近 {trading_days} 个交易日" if trading_days else "回测区间内"
    if not diagnosis:
        return (f"体检：开仓条件有先后顺序、任一满足、条件组合或方向确认，不做逐条检查；{span}回测开仓 {trades} 笔。"
                if trades else f"体检：{span}回测没有开仓；这类开仓条件（有先后顺序、任一满足、条件组合或方向确认）不做逐条检查，"
                               "请看看条件是不是太严。")
    counts = "、".join(f"{GROUP_WORDS[name]}{'条件同时满足 ' if index == 0 else ' '}{item['allPass']} 次"
                       for index, (name, item) in enumerate(diagnosis.items()))
    return f"体检通过：{span}{counts}，没有条件互相卡住。"


def health_report(plan: StrategyPlan, observer: EntryHealth | None, *, trading_days: int | None,
                  trades: int) -> dict[str, Any]:
    """The backtest result's `diagnosis`, `advice`, `headline` (only when stuck) and `checkLine` — the
    回测与体检 verdict every run carries: the headline when stuck, otherwise the pass line (plain JSON)."""
    diagnosis = observer.diagnosis() if observer else {}
    if not needs_advice(diagnosis, trades):
        return {"diagnosis": diagnosis, "advice": [], "headline": None,
                "checkLine": passed_line(diagnosis, trading_days=trading_days, trades=trades)}
    advice = build_advice(diagnosis)
    line = headline(diagnosis, advice, trading_days=trading_days, trades=trades)
    return {"diagnosis": diagnosis, "advice": advice, "headline": line, "checkLine": line}


def apply_advice(plan: dict[str, Any], advice: dict[str, Any]) -> dict[str, Any]:
    """The plan with one advice applied: drop removes that condition from each named group;
    relaxCross lets each named group's crosses happen within RELAX_BARS bars. Validated; raises
    ValueError when the advice does not fit the plan (a group would be emptied, index out of range)."""
    result = copy.deepcopy(plan)
    rules = result.get("rules") or {}
    for name in advice.get("groups") or []:
        group = rules.get(name)
        if not group or group.get("alignmentConditions"):
            raise ValueError(f"{GROUP_WORDS.get(name, name)}条件不适用这条建议")
        conditions = group["conditions"]
        if advice["kind"] == "drop":
            index = int(advice["index"])
            if len(conditions) < 2 or not 0 <= index < len(conditions):
                raise ValueError("去掉后条件组会变空，不能这样改")
            del conditions[index]
        elif advice["kind"] == "relaxCross":
            for condition in conditions:
                if condition.get("operator") in CROSS_OPERATORS and int(condition.get("withinBars") or 1) < RELAX_BARS:
                    condition["withinBars"] = RELAX_BARS
        else:
            raise ValueError("未知的建议")
    StrategyPlan.model_validate(result)
    return result
