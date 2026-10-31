"""Typed, closed-bar strategy rules shared by design, validation and execution.

Building blocks (策略积木, 2026-09-25) extend the operand vocabulary additively: definitions saved
before them validate unchanged (schemaVersion stays 2, every new field is optional). The second batch
(第三期 A) adds CCI, ADX / ±DI, SAR, 当日最高/最低, 枢轴 (R-Breaker), open-interest change / quadrant,
`consecutive`, `rising` / `falling`, the entry groups' `rearm` and the plan's `breakeven` /
`timeTakeProfit`. The third batch (2026-09-27) adds ROC, KDJ's J line, TRIX, the bar's own high / low /
open, and `barsAgo` on any operand (its value N closed bars back). The fourth batch (2026-09-27) adds
条件组合 (a group's `branches`: small AND / OR groups joined with its own conditions, so (A且B)或(C且D)),
N 根内至少 M 根 (`minCount`, 通达信 COUNT), 区间最高 / 最低 of any operand (`highest` / `lowest`, 通达信
HHV / LLV), 威廉 %R, 乖离率, MFI, BBI (TqSdk ta formulas) and the bar's range / body / shadows. The fifth
(2026-09-28) adds 分时均价 (`vwap`, the trading day's volume-weighted typical price) and the entry groups'
盘中触价 (`trigger`: arm a stop / limit level at the signal bar's close, filled inside the next bar). The
sixth (2026-09-28) adds the plan-level 加仓 (`pyramiding`: a same-direction add only in profit, never
averaging down) and 分批止盈 (`scaleOut`: partial closes of the peak volume at rising profit targets).
价差 (2026-09-29 第一期) adds `spread`: two legs of one account traded as one unit whose rules judge
the legs' closed K-lines joined into a spread series (see strategy_spread).
Every new indicator's value at a bar is the same in the live 255-bar window and in a
backtest's whole series (fixed lookbacks are sliced; SAR reuses the previous window's path).
"""
from __future__ import annotations

import json
import math
import threading
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import accumulate
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .instruments import STRATEGY_CONTRACT_PATTERN, foreign_product_of
from .trading_calendar import (
    NS,
    bar_trading_day,
    daily_bar_day,
    daily_index,
    first_bars_of_day,
    parse_schedule,
    sessions_of_day,
)

Timeframe = Literal[0, 60, 180, 300, 900, 3600, 86400]
Indicator = Literal[
    "constant", "close", "ma", "macd_dif", "macd_dea", "macd_hist", "kd_k", "kd_d", "volume", "volume_ratio",
    "ema", "atr", "donchian_high", "donchian_low", "boll_mid", "boll_up", "boll_down",
    "keltner_mid", "keltner_up", "keltner_down", "rsi", "supertrend",
    "prev_high", "prev_low", "prev_close", "today_open", "range_nday", "gap_percent",
    "opening_range_high", "opening_range_low",
    "cci", "adx", "plus_di", "minus_di", "sar", "session_high", "session_low", "pivot", "oi_change", "oi_quadrant",
    "roc", "kd_j", "trix", "high", "low", "open",
    "williams_r", "bias", "mfi", "bbi", "cmi", "bar_range", "body", "upper_shadow", "lower_shadow",
    "vwap",
]
# Read from the 86400 series by trading day (夜盘归下一交易日), whatever the operand's own clock.
DAILY_ANCHORS = frozenset({"prev_high", "prev_low", "prev_close", "today_open", "range_nday", "gap_percent", "pivot"})
# The period an operand gets when the caller leaves it out; `ma` keeps its historical 5.
DEFAULT_PERIODS = {"ema": 20, "atr": 14, "donchian_high": 20, "donchian_low": 20, "boll_mid": 20, "boll_up": 20,
                   "boll_down": 20, "keltner_mid": 20, "keltner_up": 20, "keltner_down": 20, "rsi": 14,
                   "supertrend": 10, "range_nday": 5, "opening_range_high": 1, "opening_range_low": 1,
                   "cci": 20, "adx": 14, "plus_di": 14, "minus_di": 14, "oi_change": 1, "oi_quadrant": 1,
                   "roc": 12, "trix": 12, "williams_r": 14, "bias": 6, "mfi": 14, "cmi": 30}
WINDOWED = frozenset({"ma", "ema", "atr", "donchian_high", "donchian_low", "boll_mid", "boll_up", "boll_down",
                      "keltner_mid", "keltner_up", "keltner_down", "rsi", "supertrend", "cci", "adx", "plus_di", "minus_di", "trix",
                      "williams_r", "bias", "mfi", "cmi"})
DEFAULT_K = {"boll": 2.0, "keltner": 1.5, "supertrend": 3.0}
# +DI / −DI / ADX: Wilder smoothing over a fixed history of DI_HISTORY bars behind each value (ADX: DI_HISTORY
# DX values), so a value depends on its own lookback only — the live 255-bar window and a whole-series backtest
# compute it identically. DI_MAX_PERIOD keeps the seed a small part of that history.
DI_HISTORY = 100
DI_MAX_PERIOD = 50
# TRIX warms up over 3 × (N − 1) + 2 bars of the 255-bar live window; 60 leaves room for its average.
TRIX_MAX_PERIOD = 60
# 前 N 根 (`barsAgo`): an operand's value N closed bars of its own clock back — never a later bar.
MAX_BARS_AGO = 50
BAR_FIELDS = frozenset({"high", "low", "open"})
# Read from the trading day's first bar (夜盘归下一交易日): a window that starts inside the day is
# completed from a longer series (live: the read-only history; a backtest: its whole series).
SESSION_INDICATORS = frozenset({"session_high", "session_low", "vwap"})
# K线形态 (第四批): read the bar itself only — 振幅 high − low, 实体 close − open (阳线为正), 上 / 下影线.
CANDLE_FIELDS = frozenset({"bar_range", "body", "upper_shadow", "lower_shadow"})
# 价差 (2026-09-29 第一期): the indicators a spread plan may read. A synthesized spread bar only
# has its own open and close (its high / low are their max / min — two legs have no common intrabar
# path), so every indicator that reads high / low, volume or open interest, and the daily anchors /
# session series (they read one contract's own day), is not computable on a spread.
SPREAD_INDICATORS = frozenset({"constant", "close", "open", "ma", "ema", "macd_dif", "macd_dea", "macd_hist",
                               "boll_mid", "boll_up", "boll_down", "rsi", "roc", "bias", "trix", "bbi"})
BBI_PERIODS = (3, 6, 12, 24)                 # TqSdk ta.BBI(df, 3, 6, 12, 24)
# 条件组合: at most this many conditions in one group, its own and its branches' together.
MAX_GROUP_CONDITIONS = 16
# 枢轴位 levels, carried in `value`: P, R1–R3, S1–S3. R-Breaker's six prices are the same lines (TqSdk
# demo/example/rbreaker.py): 突破买入=R3, 观察卖出=R2, 反转卖出=R1, 反转买入=S1, 观察买入=S2, 突破卖出=S3.
PIVOT_LEVELS = {0: "枢轴P", 1: "枢轴R1（反转卖出价）", 2: "枢轴R2（观察卖出价）", 3: "枢轴R3（突破买入价）",
                -1: "枢轴S1（反转买入价）", -2: "枢轴S2（观察买入价）", -3: "枢轴S3（突破卖出价）"}
# 持仓四象限 codes (oi_quadrant eq value): price and open interest both against N bars ago.
OI_QUADRANTS = {1: "多头增仓", 2: "空头增仓", 3: "空头减仓", 4: "多头减仓"}
SAR_STEP, SAR_MAX = 0.02, 0.2
# 保本 / 分段止盈 in ATR: the signal bar's ATR of this period, frozen at the entry like the ATR stop.
EXIT_ATR_PERIOD = 14
STATE_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})


class StrategyRuleError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


def workbench_read_error(error: Exception, message: str) -> StrategyRuleError:
    """A failed workbench read keeps the provider's own code (market_series_not_ready,
    market_subscription_limit, market_quote_not_ready …) beside the sentence for people, so the
    strategy monitor can tell data that is not ready yet from a real failure (2026-10-12)."""
    code = str(error) if isinstance(error, RuntimeError) else ""
    if not (code.isascii() and "_" in code and code.replace("_", "").isalnum() and len(code) <= 100):
        code = "market_symbol_unknown" if isinstance(error, KeyError) else "market_workbench_unavailable"
    return StrategyRuleError(code, message)


class OperandBase(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    # The operand vocabulary is explained once in the designer instructions: this model is inlined
    # many times into the structured-output schema, so it carries no per-field descriptions.
    indicator: Indicator
    timeframe: Timeframe = 0                                         # 0 跟随图表周期（说明在设计指令里）
    period: int = Field(default=5, ge=1, le=200)
    value: float = 0
    k: float | None = Field(default=None, gt=0, le=10)                 # 布林/肯特纳/超级趋势倍数，默认 2/1.5/3
    basis: Literal["ema", "ma"] | None = None                         # 肯特纳中轨，默认 ema
    average: int | None = Field(default=None, ge=2, le=200)           # 再取该指标的 N 根均线
    barsAgo: int = Field(default=0, ge=0, le=MAX_BARS_AGO)            # 取 N 根前（已收盘）的值，0 为当前根
    highest: int | None = Field(default=None, ge=2, le=200)           # 该指标最近 N 根（含当前根）的最高值
    lowest: int | None = Field(default=None, ge=2, le=200)            # 该指标最近 N 根（含当前根）的最低值

    @model_validator(mode="before")
    @classmethod
    def indicator_default_period(cls, data: Any) -> Any:
        if isinstance(data, dict) and "period" not in data and data.get("indicator") in DEFAULT_PERIODS:
            return {**data, "period": DEFAULT_PERIODS[data["indicator"]]}
        return data

    @model_validator(mode="after")
    def window_period(self):
        if self.indicator in WINDOWED and self.period < 2:
            raise ValueError("指标周期至少 2 根")
        if self.indicator in {"adx", "plus_di", "minus_di"} and self.period > DI_MAX_PERIOD:
            raise ValueError(f"ADX / DI 周期最多 {DI_MAX_PERIOD} 根")
        if self.indicator == "trix" and self.period > TRIX_MAX_PERIOD:
            raise ValueError(f"TRIX 周期最多 {TRIX_MAX_PERIOD} 根")
        if self.indicator == "constant" and self.barsAgo:
            raise ValueError("常数没有“前 N 根”，前 N 根只用于行情指标")
        if sum(item is not None for item in (self.average, self.highest, self.lowest)) > 1:
            raise ValueError("均线、区间最高、区间最低三者只能选一种")
        if self.indicator == "constant" and (self.highest or self.lowest):
            raise ValueError("常数没有区间最高 / 最低，只用于行情指标")
        if self.indicator == "pivot" and self.value not in PIVOT_LEVELS:
            raise ValueError("枢轴位的 value 取 0（P）、1–3（R1–R3）或 −1–−3（S1–S3）")
        return self


class Offset(BaseModel):
    # 本指标 + factor × 波动尺度（ATR(N) 或 N日振幅，同周期），例如 今开 + 0.2 × 5日振幅、MA + 1.5 × ATR。
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    factor: float = Field(ge=-100, le=100)
    indicator: Literal["atr", "range_nday"]
    period: int = Field(default=14, ge=1, le=200)

    @model_validator(mode="before")
    @classmethod
    def indicator_default_period(cls, data: Any) -> Any:
        if isinstance(data, dict) and "period" not in data and data.get("indicator") in DEFAULT_PERIODS:
            return {**data, "period": DEFAULT_PERIODS[data["indicator"]]}
        return data

    @model_validator(mode="after")
    def usable(self):
        if self.factor == 0:
            raise ValueError("系数不能为 0")
        if self.indicator == "atr" and self.period < 2:
            raise ValueError("指标周期至少 2 根")
        return self

    def term(self, timeframe: int) -> OperandBase:
        return OperandBase(indicator=self.indicator, period=self.period, timeframe=timeframe)


class Operand(OperandBase):
    offset: Offset | None = None

    @model_validator(mode="after")
    def offset_on_market_value(self):
        if self.offset is not None and self.indicator == "constant":
            raise ValueError("系数偏移须作用在行情指标上")
        return self


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=160)
    left: Operand
    operator: Literal["gt", "gte", "lt", "lte", "cross_up", "cross_down", "rising", "falling", "eq"]
    right: Operand
    withinBars: int = Field(default=1, ge=1, le=20)
    consecutive: int = Field(default=1, ge=1, le=20)
    minCount: int = Field(default=1, ge=1, le=20)     # withinBars 根内至少 minCount 根成立（通达信 COUNT）

    @model_validator(mode="after")
    def same_clock(self):
        if self.right.indicator != "constant" and self.left.timeframe != self.right.timeframe:
            raise ValueError("单个比较条件的两条指标须使用同一周期；跨周期条件请分行组合")
        if self.left.indicator == "constant":
            raise ValueError("左侧必须是行情指标")
        if self.operator in {"rising", "falling"} and (
                self.right.indicator != "constant" or self.right.value not in range(1, 21)):
            raise ValueError("上升 / 下降比较的右侧写常数：与几根K线前比（1–20）")
        if (self.operator == "eq") != (self.left.indicator == "oi_quadrant") or (
                self.operator == "eq" and (self.right.indicator != "constant" or self.right.value not in OI_QUADRANTS)):
            raise ValueError("持仓四象限只用“等于”比较常数：1 多头增仓、2 空头增仓、3 空头减仓、4 多头减仓")
        if self.consecutive > 1 and self.operator in {"cross_up", "cross_down"}:
            raise ValueError("交叉是一次性事件，不能要求连续 N 根；连续 N 根请用高于 / 低于等状态比较")
        if self.minCount > self.withinBars:
            raise ValueError("“N 根内至少 M 根”的 M 不能大于 N（withinBars）")
        return self


class Branch(BaseModel):
    """条件组合的一个分支：一小组条件，组内按 join 合并，整体当作本组的一条条件。"""
    model_config = ConfigDict(extra="forbid")
    join: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(min_length=1, max_length=8)


class RuleGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    join: Literal["all", "any"] = "all"
    sequence: bool = False
    conditions: list[Condition] = Field(default_factory=list, max_length=12)
    branches: list[Branch] = Field(default_factory=list, max_length=4,
        description="条件组合：每个分支先在组内按自己的join合并，再与conditions里的单条条件一起按本组join合并；"
                    "例如 join=any、两个 all 分支 = (A且B)或(C且D)。")
    alignmentConditions: list[Condition] = Field(default_factory=list, max_length=12,
        description="入场方向状态，全部同向后等待conditions确认；方向被打断才开始新的一段，每段最多一次开仓尝试。")

    @model_validator(mode="after")
    def ordered_events(self):
        if not self.conditions and not self.branches:
            raise ValueError("条件组至少要有一条条件")
        if len(signal_conditions(self)) > MAX_GROUP_CONDITIONS:
            raise ValueError(f"一个条件组连同组合分支最多 {MAX_GROUP_CONDITIONS} 条条件")
        if self.branches and (self.sequence or self.alignmentConditions):
            raise ValueError("条件组合不与先后顺序、方向确认同用")
        if self.alignmentConditions and (self.join != "all" or self.sequence or any(
                c.operator not in STATE_OPERATORS or c.withinBars != 1 or c.consecutive != 1
                for c in self.alignmentConditions + self.conditions)):
            raise ValueError("方向确认使用全部满足的当前状态比较，不使用交叉窗口或固定先后顺序")
        if self.sequence and (self.join != "all" or any(c.operator not in {"cross_up", "cross_down"} for c in self.conditions)):
            raise ValueError("先后顺序只适用于全部满足的交叉事件条件")
        return self


class EntryTrigger(BaseModel):
    """盘中触价入场：本组条件在收盘成立时，按这根K线收盘时 price 的值挂一个触价单，只在下一根执行周期
    K线内有效——stop：多单价格涨到它（空单跌到它）就开仓，limit：多单价格回落到它（空单反弹到它）就开仓；
    没触及就作废，下一根收盘条件仍成立再按新价位挂。"""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["stop", "limit"] = "stop"
    price: Operand


class EntryGroup(RuleGroup):
    rearm: list[Condition] = Field(default_factory=list, max_length=4,
        description="复位：本方向开仓后须等这些条件（执行周期、全部满足）成立一次才可再开。")
    trigger: EntryTrigger | None = Field(default=None,
        description="盘中触价入场：条件在收盘成立后挂价（price 取这根收盘时的值），下一根K线内价格触及才开仓。")


class StrategyRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entryLong: EntryGroup | None = None
    exitLong: RuleGroup | None = None
    entryShort: EntryGroup | None = None
    exitShort: RuleGroup | None = None


def signal_conditions(group: RuleGroup | None) -> list[Condition]:
    """The group's own conditions, then each branch's in order: what `group_passes` combines."""
    return group.conditions + [c for branch in group.branches for c in branch.conditions] if group else []


def group_conditions(group: RuleGroup | None) -> list[Condition]:
    """The conditions deciding the group's signal (its rearm conditions are not among them)."""
    return group.alignmentConditions + signal_conditions(group) if group else []


def group_passes(group: RuleGroup, passed: list[bool]) -> bool:
    """The group's signal from one flag per `signal_conditions(group)`: each branch is joined by its own
    `join` into one flag, then the group's conditions and branch flags by the group's `join`."""
    own = len(group.conditions)
    flags, at = list(passed[:own]), own
    for branch in group.branches:
        part = passed[at:at + len(branch.conditions)]
        at += len(branch.conditions)
        flags.append(all(part) if branch.join == "all" else any(part))
    return all(flags) if group.join == "all" else any(flags)


def trigger_of(group: RuleGroup | None) -> EntryTrigger | None:
    return getattr(group, "trigger", None)


def rearm_conditions(group: RuleGroup | None) -> list[Condition]:
    return list(getattr(group, "rearm", None) or [])


def all_conditions(group: RuleGroup | None) -> list[Condition]:
    """Every condition a group reads, rearm included: for clocks, series and operands."""
    return group_conditions(group) + rearm_conditions(group)


class ClarificationOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=300)


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=500)
    options: list[ClarificationOption] = Field(min_length=2, max_length=3)
    recommended: int = Field(default=0, ge=0, le=2)

    @model_validator(mode="after")
    def recommended_exists(self):
        if self.recommended >= len(self.options):
            raise ValueError("推荐选项不存在")
        return self


class PullbackSetup(BaseModel):
    """A bounded, single-clock trend pullback; levels freeze on the signal bar."""
    model_config = ConfigDict(extra="forbid")
    trendPeriod: int = Field(default=200, ge=2, le=200)
    touchPeriod: int = Field(default=25, ge=2, le=200)
    guardPeriod: int = Field(default=60, ge=2, le=200)
    maxBars: int = Field(default=10, ge=1, le=20)


class AtrStop(BaseModel):
    """入场止损距离 = 信号K线 ATR × multiple；成交后按成交价换算成固定止损价。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    multiple: float = Field(gt=0, le=20)
    period: int = Field(default=14, ge=2, le=200)


class TrailingStop(BaseModel):
    """实盘按最新价只收紧的追踪止损；value 与 activateAfter 同单位（percent=%、ticks=跳、atr_chandelier=ATR倍数）。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["percent", "ticks", "atr_chandelier"]
    value: float = Field(gt=0, le=10000)
    activateAfter: float = Field(default=0, ge=0, le=10000, description="浮盈达到多少才开始追踪；0 立即追踪。")
    atrPeriod: int = Field(default=14, ge=2, le=200)


class ProtectionTrailing(TrailingStop):
    """手动止损的追踪方式（2026-10-25 第 4 批）：策略的追踪止损（按最新价只收紧、不放松），ATR 吊灯在挂上时按
    atrTimeframe 周期最近收完的 K 线算一次 ATR（atrPeriod 根）并固定。bar_extreme（只限手动止损）：多单止损跟到
    atrTimeframe 周期最近 value 根已收完 K 线的最低价、空单跟到最高价，每收完一根重算，同样只紧不松；activateAfter 不用。"""
    mode: Literal["percent", "ticks", "atr_chandelier", "bar_extreme"]
    atrTimeframe: Literal[60, 180, 300, 900, 1800, 3600, 86400] = Field(
        default=300, description="ATR 吊灯 / 前 N 根 K 线按哪个周期算（秒）")

    @model_validator(mode="after")
    def bar_count(self):
        if self.mode == "bar_extreme" and (self.value != int(self.value) or not 1 <= self.value <= 200):
            raise ValueError("按前 N 根 K 线追踪时 N 要是 1–200 的整数")
        return self


class Sizing(BaseModel):
    """atr_risk：手数 = floor(权益 × riskPercent% ÷ (合约乘数 × atrMultiple × ATR))，上限 maxPosition。
    equity_percent (2026-10-09)：本策略保证金预算 = 权益 × equityPercent%，先扣本策略持仓与未成交
    开仓挂单占用的保证金再除以每手保证金；可选 maxLots 限制本策略总手数（持仓+挂单），不填不限。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["fixed", "atr_risk", "equity_percent"] = "fixed"
    riskPercent: float | None = Field(default=None, gt=0, le=10)
    atrMultiple: float = Field(default=2, gt=0, le=20)
    atrPeriod: int = Field(default=14, ge=2, le=200)
    equityPercent: float | None = Field(default=None, gt=0, le=100,
        description="按资金比例定手数：本策略保证金占用上限占账户权益的百分比。")
    maxLots: int | None = Field(default=None, ge=1,
        description="按资金比例定手数时本策略总手数上限（持仓+未成交开仓挂单）；不填不限。")

    @model_validator(mode="after")
    def risk_percent_required(self):
        if self.mode == "atr_risk" and self.riskPercent is None:
            raise ValueError("按 ATR 定手数须设置单笔风险占权益的百分比")
        if self.mode == "equity_percent" and self.equityPercent is None:
            raise ValueError("按资金比例定手数须设置资金占用上限占权益的百分比")
        return self


class EntryWindow(BaseModel):
    """允许开仓的北京时间段（含两端，start>end 表示跨午夜）；出场不受限制。"""
    model_config = ConfigDict(extra="forbid")
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class Breakeven(BaseModel):
    """保本：浮盈（持仓后最高/最低价）达 triggerTicks 跳或 triggerAtr×ATR14 后，止损移到开仓价±offsetTicks 跳，只收紧。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    triggerTicks: int | None = Field(default=None, ge=1, le=10000)
    triggerAtr: float | None = Field(default=None, gt=0, le=20)
    offsetTicks: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def one_trigger(self):
        if (self.triggerTicks is None) == (self.triggerAtr is None):
            raise ValueError("保本触发写 triggerTicks（跳）或 triggerAtr（ATR 倍数）其中一个")
        if self.triggerTicks is not None and self.offsetTicks >= self.triggerTicks:
            raise ValueError("保本后的止损须在触发价以内（offsetTicks 小于 triggerTicks）")
        return self


class TimeTarget(BaseModel):
    """分段止盈的一档：持仓满 afterBars 根起，收盘浮盈达 profitTicks 跳或 profitAtr×ATR14 即平仓。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    afterBars: int = Field(ge=0, le=500)
    profitTicks: int | None = Field(default=None, ge=0, le=10000)
    profitAtr: float | None = Field(default=None, ge=0, le=50)

    @model_validator(mode="after")
    def one_target(self):
        if (self.profitTicks is None) == (self.profitAtr is None):
            raise ValueError("分段止盈每档写 profitTicks（跳）或 profitAtr（ATR 倍数）其中一个")
        return self


class Pyramiding(BaseModel):
    """加仓：浮盈方向顺势加（不摊平）。trigger 决定何时加：profit_atr / profit_ticks 按上次成交价
    与决策K线收盘的距离，signal 按同向入场组再成立且收盘更优；step 是距离（ATR 倍数或跳数）。
    volume 是每次加仓手数（缺省用首次开仓手数）；moveStop 为加仓后按最近一次成交重算止损/止盈。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    maxAdds: int = Field(ge=1, le=5)
    trigger: Literal["profit_atr", "profit_ticks", "signal"] = "profit_atr"
    step: float = Field(default=0.5, gt=0, le=10000)
    atrPeriod: int = Field(default=20, ge=2, le=200)
    volume: int | None = Field(default=None, ge=1, le=20)
    moveStop: bool = True


class ScaleOutTier(BaseModel):
    """分批止盈的一档：K线收盘时对持仓均价的浮盈达 profitTicks 跳或 profitAtr×ATR14（入场时冻结在
    exit_atr），平掉峰值手数的 percent%；只剩 1 手时该档作废（仍记为已触发）。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    profitTicks: int | None = Field(default=None, ge=1, le=10000)
    profitAtr: float | None = Field(default=None, gt=0, le=50)
    percent: int = Field(ge=1, le=99)

    @model_validator(mode="after")
    def one_target(self):
        if (self.profitTicks is None) == (self.profitAtr is None):
            raise ValueError("分批止盈每档写 profitTicks（跳）或 profitAtr（ATR 倍数）其中一个")
        return self


class SpreadLeg(BaseModel):
    """价差的一条腿：一个具体合约，及这份价差里它占的手数（lots）与权重（weight，仅差价用）。"""
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=rf"^{STRATEGY_CONTRACT_PATTERN}$")     # 与上线实例同一具体合约格式
    weight: float = Field(default=1, gt=0, le=100)
    lots: int = Field(default=1, ge=1, le=20)


class SpreadSpec(BaseModel):
    """价差 (2026-09-29 第一期): two legs of one account traded as one unit — 做多价差 = 买 A 卖 B,
    做空价差 = 卖 A 买 B, one unit = A lotsA 手 + B lotsB 手. The rules judge the spread series
    (``difference`` A×wA − B×wB, the weights; ``ratio`` A ÷ B, the weights ignored) on the two legs'
    same-period closed K-lines inner-joined by time. ``maxPosition`` counts spread units; the plan's
    stopLossTicks / takeProfitTicks do not apply (use stopLoss / takeProfit in spread points here)."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    kind: Literal["difference", "ratio"] = "difference"
    legs: list[SpreadLeg] = Field(min_length=2, max_length=2)
    stopLoss: float | None = Field(default=None, gt=0, le=1000000, description="价差止损点数：实盘每秒按两腿最新价判断，回测按收盘价差判断。")
    takeProfit: float | None = Field(default=None, gt=0, le=1000000, description="价差止盈点数：实盘每秒按两腿最新价判断，回测按收盘价差判断。")

    @model_validator(mode="after")
    def one_account(self) -> SpreadSpec:
        # 两腿在同一个账户里成交：外盘合约只在美元子账户撮合，一腿外盘一腿国内的价差下不成（外盘三期）。
        if len({foreign_product_of(leg.symbol) is not None for leg in self.legs}) > 1:
            raise ValueError("价差的两条腿必须同是国内合约或同是外盘合约：外盘只在美元子账户成交，跨账户的价差下不成")
        return self


class ReminderOptions(BaseModel):
    """提醒页的提醒怎么响 (改版第一步 2026-10-31)，只用于 reminderOnly 的计划。"""
    model_config = ConfigDict(extra="forbid")
    repeat: Literal["once", "rearm", "bar"] = Field(default="rearm",
        description="响几次：once 只响一次（响过就自动停）；rearm 同一个信号只响一次，条件不再满足后再满足才再响（原来的做法）；"
                    "bar 条件满足的每根 K 线最多响一次。")
    until: Literal["today", "week", "forever"] = Field(default="forever",
        description="到期：today 开始提醒那个交易日收盘、week 本周最后一个交易日收盘、forever 一直；到期自动停并说一声。")
    watchlist: bool = Field(default=False,
        description="整张自选表：开始提醒时按自选表每个品种的主力合约各上线一个实例，每个品种分别判断（上线后自选表再变不跟）。")


class StrategyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=20000)
    direction: Literal["long", "short", "both"] = "both"
    timeframeSeconds: Timeframe = Field(default=0, description="0 表示按条件所用周期动态解析；用户明确固定执行周期时填写秒数。")
    maxPosition: int = Field(default=1, ge=1, le=20)
    maxLoss: float | None = Field(default=None, gt=0, le=100000000,
        description="当日净亏损上限（元），含已实现、持仓盈亏及手续费；触及后仅禁止新开仓，退出条件和保护仍运行。"
                    "null=不设：只有用户明确说了当日亏损上限才填（2026-10-08 用户决定）。")
    maxDrawdownPercent: float | None = Field(default=None, gt=0, le=100,
        description="回撤停机 (2026-10-09)：本策略净值从本轮最高点回撤达到该百分比时，撤销本策略未成交开仓单、"
                    "平掉本策略持仓并保持停止，手动恢复。null=不启用；只按本策略归属的净值计算。")
    stopLossTicks: int = Field(default=20, ge=1, le=5000)
    takeProfitTicks: int | None = Field(default=40, ge=1, le=10000)
    pullback: PullbackSetup | None = None
    stopLookbackBars: int | None = Field(default=None, ge=2, le=200,
        description="入场时取最近N根已收盘K线（含信号K线）最低/最高价外一跳作为固定止损；与pullback互斥。")
    perTradeRisk: float | None = Field(default=None, gt=0, le=100000000)
    cooldownBars: int = Field(default=1, ge=0, le=100,
        description="平仓后等待的执行周期K线根数；退出不受冷却限制。平仓确认后才允许下一根信号开仓，不能同次反手。")
    maxOrdersPerDay: int = Field(default=20, ge=1, le=50)
    atrStop: AtrStop | None = Field(default=None, description="ATR 止损；与 stopLookbackBars、pullback 只能选一种，设置后不用 stopLossTicks。")
    trailingStop: TrailingStop | None = None
    closeBeforeEndMinutes: int | None = Field(default=None, ge=1, le=120,
        description="收盘前 N 分钟平掉持仓；同一交易日同一持仓只触发一次。")
    closeBeforeEndScope: Literal["day", "session"] = Field(default="day",
        description="day 只在当日最后一个时段收盘前；session 每个交易时段收盘前都平。")
    maxHoldBars: int | None = Field(default=None, ge=1, le=200, description="持仓达到 N 根执行周期K线即平仓。")
    sizing: Sizing | None = None
    entryWindows: list[EntryWindow] = Field(default_factory=list, max_length=6,
        description="只在这些时段开仓（按信号K线收盘时刻）；空表示不限。")
    allowNightEntries: bool = Field(default=True, description="false 时夜盘不开新仓，出场照常。")
    maxEntriesPerDirection: int | None = Field(default=None, ge=1, le=50, description="每个交易日每个方向最多开仓次数。")
    breakeven: Breakeven | None = None
    timeTakeProfit: list[TimeTarget] = Field(default_factory=list, max_length=6,
        description="分段止盈：收盘时取 afterBars 不超过已持仓根数的最大一档，浮盈达标即平；每笔持仓一次。")
    pyramiding: Pyramiding | None = Field(default=None,
        description="加仓：浮盈方向顺势加（不摊平），总手数不超过 maxPosition；分批止盈触发后不再加仓。")
    scaleOut: list[ScaleOutTier] = Field(default_factory=list, max_length=3,
        description="分批止盈：最多 3 档、目标由近到远，对持仓均价判断；每档平掉峰值手数的 percent%，每笔持仓每档一次。")
    spread: SpreadSpec | None = Field(default=None,
        description="价差：两条腿按一个整体交易（做多价差 = 买 A 卖 B）；规则判定两腿收盘价合成的价差序列。"
                    "maxPosition 为价差份数，stopLossTicks/takeProfitTicks 不生效。")
    rules: StrategyRules
    reminderOnly: bool = Field(default=False,
        description="只提醒 (2026-10-25 第 6 批)：提醒面板“新建提醒”存的策略——只填条件和时段，手数止损不用；"
                    "上线只能“信号来了只提醒”，不出卡、不下单，列在“我的提醒”、不出现在“我的策略”。")
    reminder: ReminderOptions | None = Field(default=None,
        description="提醒页的提醒的响几次、到期与自选表（只用于 reminderOnly）；null＝原来的做法（条件再满足才再响、一直提醒）。")
    signalAction: Literal["alert", "confirm", "auto"] | None = Field(default=None,
        description="信号来了怎么办：上线运行对话框默认选中的执行方式（只是默认，上线仍由用户点；自动下单仍要 AI 自主执行开着）。"
                    "null＝出卡确认。")
    questions: list[str] = Field(default_factory=list, max_length=20)

    clarifications: list[Clarification] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def fixed_conditions_use_fixed_clock(self):
        # 提醒的选项只属于提醒，执行方式的默认只属于策略：放错地方的不报错，直接去掉。
        if not self.reminderOnly and self.reminder is not None:
            self.reminder = None
        if self.reminderOnly and self.signalAction is not None:
            self.signalAction = None
        if len({target.afterBars for target in self.timeTakeProfit}) != len(self.timeTakeProfit):
            raise ValueError("分段止盈各档的持仓根数不能重复")
        periods = [c.left.timeframe for group in (self.rules.entryLong, self.rules.entryShort,
                   self.rules.exitLong, self.rules.exitShort) for c in all_conditions(group)]
        if self.timeframeSeconds == 0 and periods and all(periods):
            self.timeframeSeconds = min(periods)
        return self

    def check_executable(self):
        if self.questions or self.clarifications:
            raise ValueError("策略仍有待澄清问题，请补充后重新设计")
        if self.direction in {"long", "both"} and self.rules.entryLong is None:
            raise ValueError("缺少开多条件")
        if self.direction in {"short", "both"} and self.rules.entryShort is None:
            raise ValueError("缺少开空条件")
        if self.direction == "long" and self.rules.entryShort is not None:
            raise ValueError("只做多策略不能包含开空条件")
        if self.direction == "short" and self.rules.entryLong is not None:
            raise ValueError("只做空策略不能包含开多条件")
        for group in (self.rules.entryLong, self.rules.entryShort, self.rules.exitLong, self.rules.exitShort):
            if group and any(0 < c.left.timeframe < self.timeframeSeconds for c in signal_conditions(group)):
                raise ValueError("执行周期不能慢于条件周期，请选择最小的条件周期")
            if group and group.alignmentConditions and any(
                    c.left.timeframe != self.timeframeSeconds for c in group_conditions(group)):
                raise ValueError("方向确认的全部条件须使用执行周期")
        if any(group and group.alignmentConditions for group in (self.rules.exitLong, self.rules.exitShort)):
            raise ValueError("方向分段确认仅用于入场，退出请使用普通状态条件")
        for group in (self.rules.entryLong, self.rules.entryShort):
            if any(c.left.timeframe != self.timeframeSeconds for c in rearm_conditions(group)):
                raise ValueError("复位条件须使用执行周期")
            trigger = trigger_of(group)
            if trigger is not None and trigger.price.indicator != "constant" and trigger.price.timeframe != self.timeframeSeconds:
                raise ValueError("触价价位须使用执行周期（在执行周期K线收盘时取值）")
            if trigger is not None and self.pullback is not None:
                raise ValueError("回调结构入场不与盘中触价同用")
        if sum(item is not None for item in (self.atrStop, self.stopLookbackBars, self.pullback)) > 1:
            raise ValueError("ATR 止损、最近K线结构止损与回调结构止损只能选一种")
        if self.pyramiding is not None:
            if self.breakeven or self.trailingStop or self.timeTakeProfit:
                raise ValueError("加仓暂不和保本/追踪止损/分段止盈一起用")
            if self.maxPosition < 2:
                raise ValueError("加仓须最大持仓至少 2 手")
        if self.scaleOut:
            unit = "ticks" if self.scaleOut[0].profitTicks is not None else "atr"
            if any((tier.profitTicks is not None) != (unit == "ticks") for tier in self.scaleOut):
                raise ValueError("分批止盈各档须同用跳数或同用 ATR 倍数")
            targets = [float(tier.profitTicks if unit == "ticks" else tier.profitAtr) for tier in self.scaleOut]
            if any(later <= earlier for earlier, later in zip(targets, targets[1:], strict=False)):
                raise ValueError("分批止盈各档目标须由近到远递增")
        if self.stopLookbackBars is not None and (self.pullback or self.perTradeRisk is None):
            raise ValueError("最近K线结构止损须设置单笔预算，且不能同时使用MA回调止损")
        if self.pullback:
            if self.perTradeRisk is None:
                raise ValueError("结构止损需要明确单笔风险预算")
            if not self.pullback.touchPeriod < self.pullback.guardPeriod < self.pullback.trendPeriod:
                raise ValueError("回调均线周期须小于保护均线，保护均线须小于趋势均线")
            for group in (self.rules.entryLong, self.rules.entryShort):
                if group and (group.join != "all" or group.sequence or group.alignmentConditions or group.branches or any(
                        c.left.timeframe != self.timeframeSeconds for c in group.conditions)):
                    raise ValueError("回调入场条件须在执行周期内全部满足，不使用先后排序或条件组合")
        if self.spread is not None:
            spread = self.spread
            if spread.legs[0].symbol.upper() == spread.legs[1].symbol.upper():
                raise ValueError("价差两条腿不能是同一个合约")
            for name, present in (("ATR 止损", self.atrStop), ("最近K线结构止损", self.stopLookbackBars),
                                  ("回调结构入场", self.pullback), ("追踪止损", self.trailingStop),
                                  ("保本", self.breakeven), ("分段止盈", self.timeTakeProfit),
                                  ("加仓", self.pyramiding), ("分批止盈", self.scaleOut),
                                  ("收盘前平仓", self.closeBeforeEndMinutes), ("单笔预算", self.perTradeRisk)):
                if present:
                    raise ValueError(f"价差策略不使用{name}")
            if self.sizing is not None and self.sizing.mode == "atr_risk":
                raise ValueError("价差策略不按 ATR 定手数：一份价差里各腿的手数写在价差定义的 legs 里")
            if self.sizing is not None and self.sizing.mode == "equity_percent":
                raise ValueError("价差策略暂不支持按资金比例定手数：两腿保证金归属还不足以可靠算出这一份价差占用的预算")
            if self.maxDrawdownPercent is not None:
                raise ValueError("价差策略暂不支持回撤停机：两腿成交归属还不足以可靠算出本策略净值")
            for group in (self.rules.entryLong, self.rules.entryShort):
                if trigger_of(group) is not None:
                    raise ValueError("价差策略不使用盘中触价")
            for operand in _plan_operands(self):
                if operand.offset is not None:
                    raise ValueError(f"价差策略的指标不带系数偏移：「{describe_operand(operand)}」的偏移依赖最高最低价")
                if operand.indicator not in SPREAD_INDICATORS:
                    raise ValueError(f"价差策略的指标只能用只看开/收盘价的：「{describe_operand(operand)}」不可用")



def uses_chart_timeframe(plan: StrategyPlan) -> bool:
    return plan.timeframeSeconds == 0 or any(
        c.left.timeframe == 0 or (c.right.indicator != "constant" and c.right.timeframe == 0)
        for group in (plan.rules.entryLong, plan.rules.entryShort, plan.rules.exitLong, plan.rules.exitShort)
        for c in all_conditions(group)) or any(
        (trigger := trigger_of(group)) is not None and trigger.price.indicator != "constant" and trigger.price.timeframe == 0
        for group in (plan.rules.entryLong, plan.rules.entryShort))


def resolve_chart_timeframe(plan: StrategyPlan, timeframe: int) -> StrategyPlan:
    if type(timeframe) is not int or timeframe not in (60, 180, 300, 900, 3600, 86400):
        raise StrategyRuleError("chart_timeframe_required", "请先选择有效的图表周期")
    resolved = plan.model_dump()
    periods = []
    for group in resolved["rules"].values():
        if not group:
            continue
        for condition in (group["conditions"] + group.get("alignmentConditions", []) + group.get("rearm", [])
                          + [c for branch in group.get("branches", []) for c in branch["conditions"]]):
            for side in ("left", "right"):
                if condition[side]["timeframe"] == 0:
                    condition[side]["timeframe"] = timeframe
            periods.append(condition["left"]["timeframe"])
    if resolved["timeframeSeconds"] == 0:
        resolved["timeframeSeconds"] = min(periods) if periods else timeframe
    for group in resolved["rules"].values():
        if group and group.get("trigger") and group["trigger"]["price"]["timeframe"] == 0:
            group["trigger"]["price"]["timeframe"] = resolved["timeframeSeconds"]
    result = StrategyPlan.model_validate(resolved)
    result.check_executable()
    return result


def _ema(values, period):
    result = []
    for value in values:
        result.append(value if not result else value * 2 / (period + 1) + result[-1] * (1 - 2 / (period + 1)))
    return result


@dataclass(frozen=True)
class SeriesContext:
    """What an operand may read besides its own bars: the 86400 series by trading day (the current,
    still-forming day may be included — only its open is ever read) and the session schedule.
    `memo` keeps what one evaluation derives from them (the daily index, trading-day labels) so
    several operands reading the same series derive it once; the series are read as given and must
    not change while the context is in use."""
    daily: list[dict] | None = None
    schedule: dict | None = None
    memo: dict = field(default_factory=dict, compare=False, repr=False)
    # 当日最高 / 最低: per clock, a longer completed series ending with (or after) the window, so a day
    # that began before the window's first bar is still read from its own first bar (live: the history
    # read stitched before the workbench; a backtest: its whole series).
    extended: dict[int, list[dict]] | None = None


class SeriesFacts:
    """Per-bar facts of one whole series, each computed once (a backtest): closes, true ranges,
    trading-day labels and the indicators whose value at a bar only reads that bar and the bars
    before it. `SeriesWindow` slices them; the arithmetic is the plain-list code run once over the
    whole series, so every sliced value is the value the window would compute itself."""

    def __init__(self, bars: list[dict]):
        self.bars = list(bars)
        self._cache: dict[Any, Any] = {}

    def _once(self, key: Any, compute):
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]

    @property
    def ordered(self) -> bool:
        """Bar times never go back (a window is then a time prefix of everything after its start)."""
        return self._once("ordered", lambda: all(
            int(a["datetime"]) <= int(b["datetime"]) for a, b in zip(self.bars, self.bars[1:], strict=False)))

    def times(self) -> list[int]:
        return self._once("times", lambda: [int(bar["datetime"]) for bar in self.bars])

    def closes(self) -> list[float]:
        return self._once("closes", lambda: [float(bar["close"]) for bar in self.bars])

    def ranges(self) -> list[float]:
        return self._once("ranges", lambda: _true_ranges(self.bars))

    def column(self, name: str) -> list[float | None]:
        return self._once(("column", name), lambda: _column(self.bars, name))

    def session_prefix(self, days: list[str], schedule: dict | None, start: int, name: str) -> tuple[bool, float | None]:
        """当日最高 / 最低 before `start`: (whether the day's first bar is known, the extreme of that
        trading day's bars before `start` — None when `start` is its first bar). Each bar's running
        extreme within its day is computed once per `days` list (a backtest's changes once a day)."""
        pick = max if name == "high" else min
        values = self.column(name)
        known_days, runs = self._cache.get(("session", name), (None, None))
        if known_days is not days or runs is None:
            runs = {}
            self._cache[("session", name)] = (days, runs)

        def label(i: int) -> str:
            return self.day_labels(days, i, i + 1)[0]

        def opens_series(i: int) -> bool:
            sessions = sessions_of_day(label(i), schedule, days)
            return bool(sessions) and int(self.bars[i]["datetime"]) == sessions[0].start_ns

        if start == 0:
            return opens_series(0), None
        if label(start - 1) != label(start):
            return True, None
        pending, j = [], start - 1
        while j not in runs:
            if j == 0 or label(j - 1) != label(j):
                runs[j] = (opens_series(0) if j == 0 else True, values[j])
                break
            pending.append(j)
            j -= 1
        for k in reversed(pending):
            known, extreme = runs[k - 1]
            runs[k] = (known, pick(extreme, values[k]))  # type: ignore[type-var]
        return runs[start - 1]

    def session_sums(self, days: list[str], schedule: dict | None, start: int) -> tuple[bool, float, float]:
        """分时均价 before `start`: (whether the day's first bar is known, Σ典型价×量 and Σ量 of that trading
        day's bars before `start`), from running sums within each day computed once per `days` list."""
        cached = self._cache.get("session_sums")
        if cached is None or cached[0] is not days:
            labels = self.day_labels(days, 0, len(self.bars))
            weighted: list[float] = []
            volumes: list[float] = []
            for i, bar in enumerate(self.bars):
                amount = float(bar.get("volume") or 0)
                fresh = i == 0 or labels[i] != labels[i - 1]
                weighted.append(_typical(bar) * amount + (0.0 if fresh else weighted[-1]))
                volumes.append(amount + (0.0 if fresh else volumes[-1]))
            sessions = sessions_of_day(labels[0], schedule, days) if labels else []
            opens = bool(sessions) and int(self.bars[0]["datetime"]) == sessions[0].start_ns
            cached = (days, labels, weighted, volumes, opens)
            self._cache["session_sums"] = cached
        _, labels, weighted, volumes, opens = cached
        if start == 0:
            return opens, 0.0, 0.0
        if labels[start - 1] != labels[start]:
            return True, 0.0, 0.0
        # Only the series' first day can begin before the series (its opening bell is known or not).
        return (opens if labels[start - 1] == labels[0] else True), weighted[start - 1], volumes[start - 1]

    def sar(self, start: int, end: int) -> list[float | None]:
        """The SAR of bars[start:end] as that window computes it (a path started at `start`). The
        path of the previous window is kept: once the new path's state equals it at some bar, every
        later state is the same (same transition, same bars), so the rest is copied, not recomputed."""
        highs, lows, closes = self.column("high"), self.column("low"), self.closes()
        known = self._cache.get("sar")
        states: list[tuple] = []
        state: tuple | None = None
        i = start + 1
        while i < end:
            state = _sar_advance(i, start, state, highs, lows, closes)
            states.append(state)
            if known is not None:
                known_start, known_states = known
                offset = i - known_start - 1
                if 0 <= offset < len(known_states) and known_states[offset] == state:
                    copied = known_states[offset + 1:offset + 1 + (end - i - 1)]
                    states += copied
                    i += len(copied)
                    state = states[-1]
                    known = None
            i += 1
        self._cache["sar"] = (start, states)
        return [None] * min(1, end - start) + [item[1] for item in states]

    def labels(self) -> list[str] | None:
        """Every bar's own tradingDay annotation, or None when a bar has none (then the label depends
        on the known trading days and is derived per window)."""
        def annotated():
            labels = [bar.get("tradingDay") for bar in self.bars]
            return labels if all(isinstance(label, str) and label for label in labels) else None
        return self._once("labels", annotated)

    def day_labels(self, days: list[str], start: int, end: int) -> list[str]:
        """``bar_trading_day(bar, days)`` of bars[start:end]: the annotations, else each bar's label
        computed once per `days` list (the latest one is kept — a backtest's daily index changes
        once per trading day)."""
        annotated = self.labels()
        if annotated is not None:
            return annotated[start:end]
        known_days, known = self._cache.get("day_labels", (None, None))
        if known_days is not days or known is None:
            known = {}
            self._cache["day_labels"] = (days, known)
        for i in range(start, end):
            if i not in known:
                known[i] = bar_trading_day(self.bars[i], days)
        return [known[i] for i in range(start, end)]

    def indicator(self, operand: OperandBase) -> list[float | None]:
        key = ("indicator", operand.indicator, operand.period, operand.k)
        return self._once(key, lambda: _base_values(self.bars, operand, None))


class SeriesWindow(list):
    """``facts.bars[start:end]`` as a list (a backtest's live-sized window) that remembers where it
    sits in the whole series, so per-bar facts are sliced instead of recomputed. `memo` keeps what
    this window derives (a daily window's index and anchor values by day)."""
    __slots__ = ("facts", "start", "memo")

    def __init__(self, facts: SeriesFacts, start: int, end: int):
        super().__init__(facts.bars[start:end])
        self.facts, self.start, self.memo = facts, start, {}


# Indicators whose value at a bar only reads that bar and the bars before it, by the window index from
# which a window has it (earlier indices are None): the whole-series value sliced is the window's own.
def _window_free_start(operand: OperandBase) -> int | None:
    name, period = operand.indicator, operand.period
    if name in {"close", "volume"} or name in BAR_FIELDS or name in CANDLE_FIELDS:
        return 0
    if name == "ma" or name.startswith("boll"):
        return period - 1
    if name in {"donchian_high", "donchian_low", "oi_change", "oi_quadrant"}:
        return period
    if name == "volume_ratio":
        return 20
    if name == "cci":
        return period - 1
    if name == "roc":
        return period
    if name in {"williams_r", "bias", "cmi"}:
        return period - 1
    if name == "mfi":
        return period
    if name == "bbi":
        return BBI_PERIODS[-1] - 1
    if name in {"plus_di", "minus_di"}:
        return DI_HISTORY
    if name == "adx":
        return 2 * DI_HISTORY - 1
    return None


def _remember(memo: dict, key: tuple, owners: tuple, compute):
    """memo[key] for the objects `owners` (key holds their ids; the entry keeps them alive, so an id
    is never reused while its entry exists)."""
    entry = memo.get(key)
    if entry is not None and all(a is b for a, b in zip(entry[0], owners, strict=True)):
        return entry[1]
    value = compute()
    memo[key] = (owners, value)
    return value


def _closes(bars: list[dict]) -> list[float]:
    if isinstance(bars, SeriesWindow):
        return bars.facts.closes()[bars.start:bars.start + len(bars)]
    return [float(bar["close"]) for bar in bars]


_NO_DAYS: list[str] = []   # never mutated


def _daily_facts(context: SeriesContext | None) -> tuple[list[str], dict[str, dict], dict]:
    """(sorted trading days, daily bar by day, anchor values by day) of the context's 86400 series,
    derived once per series object: per evaluation, or per backtest daily window."""
    daily = context.daily if context else None
    if context is None or not daily:
        return _NO_DAYS, {}, {}   # one shared object, so per-days caches (labels, 当日最高) survive between bars
    memo = daily.memo if isinstance(daily, SeriesWindow) else context.memo
    return _remember(memo, ("daily", id(daily)), (daily,), lambda: (*daily_index(daily), {}))


def _day_runs(bars: list[dict], days: list[str], context: SeriesContext | None) -> tuple[list[str], list[tuple[int, int]]]:
    """Each bar's trading day and the runs [start, end) of consecutive bars on the same day."""
    def compute():
        if isinstance(bars, SeriesWindow):
            labels = bars.facts.day_labels(days, bars.start, bars.start + len(bars))
        else:
            labels = [bar_trading_day(bar, days) for bar in bars]
        cuts = [0, *(i for i in range(1, len(labels)) if labels[i] != labels[i - 1]), len(labels)]
        return labels, [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)] if labels else []
    if context is None:
        return compute()
    memo = bars.memo if isinstance(bars, SeriesWindow) else context.memo
    return _remember(memo, ("days", id(bars), id(days)), (bars, days), compute)


def _sma(values: list[float | None], period: int) -> list[float | None]:
    """Mean of the last `period` values; None while the window is short or holds a None."""
    result: list[float | None] = []
    last_none = -1
    for i, value in enumerate(values):
        if value is None:
            last_none = i
        result.append(sum(values[i + 1 - period:i + 1]) / period  # type: ignore[arg-type]
                      if i + 1 >= period and last_none <= i - period else None)
    return result


def _warm(values: list[float], period: int) -> list[float | None]:
    return [value if i >= period - 1 else None for i, value in enumerate(values)]


def _wilder(values: list[float], period: int, first: int) -> list[float | None]:
    """Wilder smoothing: the first value is the plain mean of `period` inputs from `first`, then
    avg = (avg × (period − 1) + x) / period."""
    result: list[float | None] = [None] * len(values)
    if len(values) - first < period:
        return result
    average = sum(values[first:first + period]) / period
    result[first + period - 1] = average
    for i in range(first + period, len(values)):
        average = (average * (period - 1) + values[i]) / period
        result[i] = average
    return result


def _true_ranges(bars: list[dict]) -> list[float]:
    """High − low for the first bar (no previous close), the true range after it."""
    if isinstance(bars, SeriesWindow):
        if not bars:
            return []
        start = bars.start
        return [float(bars[0]["high"]) - float(bars[0]["low"]), *bars.facts.ranges()[start + 1:start + len(bars)]]
    ranges = []
    for i, bar in enumerate(bars):
        high, low = float(bar["high"]), float(bar["low"])
        previous = float(bars[i - 1]["close"]) if i else None
        ranges.append(high - low if previous is None else max(high - low, abs(high - previous), abs(low - previous)))
    return ranges


def _atr(bars: list[dict], period: int) -> list[float | None]:
    return _wilder(_true_ranges(bars), period, 0)


def _optional_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _column(bars: list[dict], name: str) -> list[float | None]:
    """One bar field as numbers (None where a bar lacks it, e.g. openInterest on an old series)."""
    if isinstance(bars, SeriesWindow):
        return bars.facts.column(name)[bars.start:bars.start + len(bars)]
    return [_optional_number(bar.get(name)) for bar in bars]


@lru_cache(maxsize=64)
def _wilder_weights(period: int, history: int) -> tuple[float, ...]:
    """Wilder smoothing of `history` inputs (oldest first) as one weighted sum: the seed is the mean
    of the first `period`, then avg = avg × (period − 1)/period + x/period for each later input."""
    beta = (period - 1) / period
    return tuple([beta ** (history - period) / period] * period
                 + [beta ** (history - 1 - j) / period for j in range(period, history)])


def _fixed_wilder(values: list[float | None], period: int, first: int) -> list[float | None]:
    """At each index i ≥ first + DI_HISTORY − 1: the Wilder smoothing of values[i − DI_HISTORY + 1 : i + 1]
    (None when one of them is None). A value only reads its own DI_HISTORY inputs."""
    weights = _wilder_weights(period, DI_HISTORY)
    result: list[float | None] = [None] * len(values)
    for i in range(first + DI_HISTORY - 1, len(values)):
        sample = values[i + 1 - DI_HISTORY:i + 1]
        if None not in sample:
            result[i] = math.sumprod(weights, sample)  # type: ignore[arg-type]
    return result


def _directional(bars: list[dict], period: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """(+DI, −DI, ADX) with Wilder's directional movement: +DM = up move when it beats the down move
    (else 0), −DM likewise, DI = 100 × smoothed DM ÷ smoothed true range, DX = 100 × |+DI − −DI| ÷
    (+DI + −DI) (0 when both are 0), ADX = smoothed DX. Every smoothing is over DI_HISTORY values."""
    highs, lows, closes = _column(bars, "high"), _column(bars, "low"), _closes(bars)
    count = len(bars)
    plus_dm: list[float | None] = [None] * count
    minus_dm: list[float | None] = [None] * count
    ranges: list[float | None] = [None] * count
    for i in range(1, count):
        high, low, previous_high, previous_low = highs[i], lows[i], highs[i - 1], lows[i - 1]
        if high is None or low is None or previous_high is None or previous_low is None:
            continue
        up, down = high - previous_high, previous_low - low
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        previous_close = closes[i - 1]
        ranges[i] = max(high - low, abs(high - previous_close), abs(low - previous_close))
    smooth_range = _fixed_wilder(ranges, period, 1)
    plus_di: list[float | None] = [None] * count
    minus_di: list[float | None] = [None] * count
    dx: list[float | None] = [None] * count
    for i, (plus, minus, true_range) in enumerate(zip(_fixed_wilder(plus_dm, period, 1), _fixed_wilder(minus_dm, period, 1),
                                                     smooth_range, strict=True)):
        if plus is None or minus is None or not true_range:
            continue
        plus_di[i], minus_di[i] = 100 * plus / true_range, 100 * minus / true_range
        total = plus_di[i] + minus_di[i]  # type: ignore[operator]
        dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / total if total else 0.0  # type: ignore[operator]
    return plus_di, minus_di, _fixed_wilder(dx, period, DI_HISTORY)


def _sar_advance(i: int, start: int, state: tuple | None, highs: list, lows: list, closes: list) -> tuple:
    """The parabolic SAR state (rising, sar, extreme, acceleration) after bar i of a path that began at
    bar `start` (TradingView's ta.sar, step 0.02, maximum 0.2): the first bar only seeds the trend."""
    high, low = highs[i], lows[i]
    if i == start + 1:
        rising = closes[i] > closes[i - 1]
        sar, extreme = (lows[i - 1], high) if rising else (highs[i - 1], low)
        acceleration, first = SAR_STEP, True
    else:
        rising, sar, extreme, acceleration = state  # type: ignore[misc]
        first = False
    sar = sar + acceleration * (extreme - sar)
    if rising and sar > low:
        first, rising, sar, extreme, acceleration = True, False, max(high, extreme), low, SAR_STEP
    elif not rising and sar < high:
        first, rising, sar, extreme, acceleration = True, True, min(low, extreme), high, SAR_STEP
    if not first:
        if rising and high > extreme:
            extreme, acceleration = high, min(acceleration + SAR_STEP, SAR_MAX)
        elif not rising and low < extreme:
            extreme, acceleration = low, min(acceleration + SAR_STEP, SAR_MAX)
    if rising:
        sar = min(sar, lows[i - 1], lows[i - 2]) if i - start > 1 else min(sar, lows[i - 1])
    else:
        sar = max(sar, highs[i - 1], highs[i - 2]) if i - start > 1 else max(sar, highs[i - 1])
    return rising, sar, extreme, acceleration


def _sar(bars: list[dict]) -> list[float | None]:
    if isinstance(bars, SeriesWindow):
        return bars.facts.sar(bars.start, bars.start + len(bars))
    highs, lows, closes = _column(bars, "high"), _column(bars, "low"), _closes(bars)
    result: list[float | None] = [None] * len(bars)
    state = None
    for i in range(1, len(bars)):
        state = _sar_advance(i, 0, state, highs, lows, closes)
        result[i] = state[1]
    return result


def _cci(bars: list[dict], period: int) -> list[float | None]:
    """(TP − SMA(TP)) ÷ (0.015 × mean absolute deviation), TP = (H + L + C) / 3; 0 on a flat window."""
    highs, lows, closes = _column(bars, "high"), _column(bars, "low"), _closes(bars)
    typical = [(h + lo + c) / 3 for h, lo, c in zip(highs, lows, closes, strict=True)]  # type: ignore[operator]
    result: list[float | None] = [None] * len(bars)
    for i in range(period - 1, len(bars)):
        # Offsets from the window's first value (#998): a flat window of decimals (6.602 × 14) leaves a rounding
        # residue in the plain mean that the deviation divides into ±66.7 and could fire a CCI rule; offsets are 0.
        base = typical[i + 1 - period]
        sample = [x - base for x in typical[i + 1 - period:i + 1]]
        mean = sum(sample) / period
        deviation = sum(abs(x - mean) for x in sample) / period
        result[i] = (typical[i] - base - mean) / (0.015 * deviation) if deviation else 0.0
    return result


def _open_interest(bars: list[dict], operand: OperandBase) -> list[float | None]:
    """oi_change: open interest minus N bars ago. oi_quadrant: 1 多头增仓 (price ↑ OI ↑), 2 空头增仓
    (↓ ↑), 3 空头减仓 (↑ ↓), 4 多头减仓 (↓ ↓), 0 when price or OI is unchanged — both against N bars ago."""
    interest, closes, n = _column(bars, "openInterest"), _closes(bars), operand.period
    result: list[float | None] = [None] * len(bars)
    for i in range(n, len(bars)):
        now, before = interest[i], interest[i - n]
        if now is None or before is None:
            continue
        change = now - before
        if operand.indicator == "oi_change":
            result[i] = change
            continue
        move = closes[i] - closes[i - n]
        result[i] = float(1 if move > 0 and change > 0 else 2 if move < 0 and change > 0 else
                          3 if move > 0 and change < 0 else 4 if move < 0 and change < 0 else 0)
    return result


def _rsi(closes: list[float], period: int) -> list[float | None]:
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    result: list[float | None] = []
    for gain, loss in zip(_wilder(gains, period, 1), _wilder(losses, period, 1), strict=True):
        if gain is None or loss is None:
            result.append(None)
        elif loss == 0:
            result.append(50.0 if gain == 0 else 100.0)
        else:
            result.append(100 - 100 / (1 + gain / loss))
    return result


def _supertrend(bars: list[dict], period: int, k: float) -> list[float | None]:
    """The common (TradingView) SuperTrend: bands (H+L)/2 ∓ k×ATR ratchet while the previous close
    stays inside them; the trend starts up and flips when the close crosses the previous band."""
    atr = _atr(bars, period)
    result: list[float | None] = [None] * len(bars)
    previous_up: float | None = None
    previous_down: float | None = None
    trend = 1
    for i, bar in enumerate(bars):
        value = atr[i]
        if value is None:
            continue
        middle = (float(bar["high"]) + float(bar["low"])) / 2
        up, down = middle - k * value, middle + k * value
        close = float(bar["close"])
        last_up = up if previous_up is None else previous_up
        last_down = down if previous_down is None else previous_down
        if previous_up is not None:
            last_close = float(bars[i - 1]["close"])
            if last_close > last_up:
                up = max(up, last_up)
            if last_close < last_down:
                down = min(down, last_down)
        if trend == -1 and close > last_down:
            trend = 1
        elif trend == 1 and close < last_up:
            trend = -1
        result[i] = up if trend == 1 else down
        previous_up, previous_down = up, down
    return result


def _candle(bar: dict, name: str) -> float:
    high, low, open_, close = (float(bar[key]) for key in ("high", "low", "open", "close"))
    if name == "bar_range":
        return high - low
    if name == "body":
        return close - open_
    return high - max(open_, close) if name == "upper_shadow" else min(open_, close) - low


def _mfi(bars: list[dict], period: int) -> list[float | None]:
    """资金流量指标 (TqSdk ta.MFI): typical price (H+L+C)÷3; a bar whose typical price is above / below
    the previous bar's adds typical × volume to the positive / negative flow; over the last N bars
    MFI = 100 − 100 ÷ (1 + positive ÷ negative). A bar's flow reads the bar before it, so the first
    value is at bar N (not N − 1), the same in any window."""
    typical = [(float(b["high"]) + float(b["low"]) + float(b["close"])) / 3 for b in bars]
    up = [0.0] + [typical[i] * float(bars[i]["volume"]) if typical[i] > typical[i - 1] else 0.0 for i in range(1, len(bars))]
    down = [0.0] + [typical[i] * float(bars[i]["volume"]) if typical[i] < typical[i - 1] else 0.0 for i in range(1, len(bars))]
    result: list[float | None] = []
    for i in range(len(bars)):
        if i < period:
            result.append(None)
            continue
        positive, negative = sum(up[i + 1 - period:i + 1]), sum(down[i + 1 - period:i + 1])
        result.append(None if positive == negative == 0 else 100.0 if negative == 0 else 100 - 100 / (1 + positive / negative))
    return result


def _extreme(values: list[float | None], period: int, pick) -> list[float | None]:
    """区间最高 / 最低 (通达信 HHV / LLV): the extreme of the last N values, the current one included;
    None while any of them is missing."""
    result: list[float | None] = []
    for i in range(len(values)):
        sample = values[i + 1 - period:i + 1] if i + 1 >= period else []
        result.append(pick(sample) if sample and all(v is not None for v in sample) else None)  # type: ignore[type-var]
    return result


def _trix(closes: list[float], period: int) -> list[float | None]:
    """TRIX (TqSdk ta.TRIX, 通达信): TR = EMA(EMA(EMA(C, N), N), N), TRIX = (TR ÷ TR[1] − 1) × 100 — the
    one-bar % change of the triple EMA. Each EMA (α = 2 ÷ (N + 1)) is seeded with the window's first
    value like MACD's, so a value is None until the three EMAs have run N − 1 bars each (3 × (N − 1)),
    plus the one bar the change needs. Its signal line (TqSdk TRMA = MA(TRIX, M)) is `average=M`."""
    triple = _ema(_ema(_ema(closes, period), period), period)
    first = 3 * (period - 1) + 1
    return [None if i < first or not triple[i - 1] else (triple[i] / triple[i - 1] - 1) * 100
            for i in range(len(triple))]


def _anchor_values(bars: list[dict], operand: OperandBase, context: SeriesContext | None) -> list[float | None]:
    """昨高 / 昨低 / 昨收 / 今开 / N日振幅 / 跳空: from the 86400 series by the bar's trading day.
    今开 falls back to the first bar of that trading day in this series when the day's own daily
    bar is not there yet (e.g. a replay before midnight). A day's value only depends on the daily
    series and that opening, so it is computed once per day and daily series (`_daily_facts`)."""
    days, table, by_day = _daily_facts(context)
    if not days:
        return [None] * len(bars)
    schedule = context.schedule if context else None
    trading_days, runs = _day_runs(bars, days, context)
    first_open: dict[str, float] = {}
    for start, _ in runs:
        day = trading_days[start]
        # The first bar's open is only read for a day without its own daily bar.
        if day not in table and day not in first_open:
            first = first_bars_of_day(bars, start, 1, schedule=schedule, trading_days=days)
            if first:
                first_open[day] = float(first[0]["open"])

    def value_for(day: str) -> float | None:
        before = days[:bisect_left(days, day)]
        previous = table[before[-1]] if before else None
        today = table.get(day)
        opening = float(today["open"]) if today is not None else first_open.get(day)
        name = operand.indicator
        if name == "today_open":
            return opening
        if name == "range_nday":
            if len(before) < operand.period:
                return None
            sample = [table[item] for item in before[-operand.period:]]
            highest, lowest = max(float(b["high"]) for b in sample), min(float(b["low"]) for b in sample)
            high_close, low_close = max(float(b["close"]) for b in sample), min(float(b["close"]) for b in sample)
            return max(highest - low_close, high_close - lowest)
        if previous is None:
            return None
        if name == "gap_percent":
            close = float(previous["close"])
            return None if opening is None or close == 0 else (opening - close) / close * 100
        if name == "pivot":
            high, low, close = float(previous["high"]), float(previous["low"]), float(previous["close"])
            pivot = (high + low + close) / 3
            return {0: pivot, 1: 2 * pivot - low, 2: pivot + (high - low), 3: high + 2 * (pivot - low),
                    -1: 2 * pivot - high, -2: pivot - (high - low), -3: low - 2 * (high - pivot)}[int(operand.value)]
        return float(previous[{"prev_high": "high", "prev_low": "low", "prev_close": "close"}[name]])

    result: list[float | None] = []
    for start, end in runs:
        day = trading_days[start]
        key = (operand.indicator, operand.period, operand.value, day, None if day in table else first_open.get(day))
        if key not in by_day:
            by_day[key] = value_for(day)
        result += [by_day[key]] * (end - start)
    return result


def _opening_range(bars: list[dict], operand: OperandBase, context: SeriesContext | None) -> list[float | None]:
    """The first N bars of each trading day in this series; defined from the N-th bar on."""
    days = _daily_facts(context)[0]
    schedule = context.schedule if context else None
    result: list[float | None] = [None] * len(bars)
    field, pick = ("high", max) if operand.indicator == "opening_range_high" else ("low", min)
    for start, i in _day_runs(bars, days, context)[1]:
        # bars[start:i] is one trading day.
        last = start + operand.period - 1
        if last < i:
            first = first_bars_of_day(bars, last, operand.period, schedule=schedule, trading_days=days)
            if first:
                level = pick(float(bar[field]) for bar in first)
                result[last:i] = [level] * (i - last)
    return result


def _session_extreme(bars: list[dict], operand: OperandBase, context: SeriesContext | None) -> list[float | None]:
    """当日最高 / 最低: the running high / low of the trading day up to and including each bar (夜盘归
    下一交易日). The day the series starts in is read from its own first bar through
    `context.extended` (a longer series for this clock) when the window begins inside it; without one
    the window must show the previous day or start exactly on the day's first session bell, else that
    day's values are None."""
    days = _daily_facts(context)[0]
    name = "high" if operand.indicator == "session_high" else "low"
    values = _column(bars, name)
    pick = max if name == "high" else min
    labels, runs = _day_runs(bars, days, context)
    result: list[float | None] = [None] * len(bars)
    for start, end in runs:
        prefix = None
        if start == 0:
            known, prefix = _session_prefix(bars, operand, context, labels[0], days, name)
            if not known:
                continue
        running = accumulate(values[start:end], pick, initial=prefix)  # type: ignore[type-var]
        if prefix is not None:
            next(running)
        result[start:end] = running
    return result


def _session_prefix(bars: list[dict], operand: OperandBase, context: SeriesContext | None, day: str,
                    days: list[str], name: str) -> tuple[bool, float | None]:
    """(is the first day's opening bar known, the extreme of its bars before bars[0])."""
    schedule = context.schedule if context else None
    extended = (context.extended or {}).get(operand.timeframe) if context else None
    if extended is not None and isinstance(bars, SeriesWindow) and extended is bars.facts.bars:
        return bars.facts.session_prefix(days, schedule, bars.start, name)   # a backtest: its whole series
    first = int(bars[0]["datetime"])
    if extended:
        times = [int(bar["datetime"]) for bar in extended]
        index = bisect_left(times, first)
        if index < len(times) and times[index] == first:
            pick = max if name == "high" else min
            extreme: float | None = None
            k = index - 1
            while k >= 0 and bar_trading_day(extended[k], days) == day:
                value = _optional_number(extended[k].get(name))
                extreme = value if extreme is None else pick(extreme, value)  # type: ignore[type-var]
                k -= 1
            if k >= 0:
                return True, extreme
            first, day = times[0], bar_trading_day(extended[0], days)
            sessions = sessions_of_day(day, schedule, days)
            return bool(sessions) and first == sessions[0].start_ns, extreme
    sessions = sessions_of_day(day, schedule, days)
    return bool(sessions) and first == sessions[0].start_ns, None


def _typical(bar: dict) -> float:
    return (float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3


def _session_vwap(bars: list[dict], operand: OperandBase, context: SeriesContext | None) -> list[float | None]:
    """分时均价: Σ(典型价 (H+L+C)/3 × 成交量) ÷ Σ成交量 from the trading day's first bar up to and including
    each bar (夜盘归下一交易日). Bars carry no turnover, so this is the bar estimate of the exchange's
    turnover-based average. The day the window starts in is completed like 当日最高 / 最低; unknown → None."""
    days = _daily_facts(context)[0]
    labels, runs = _day_runs(bars, days, context)
    result: list[float | None] = [None] * len(bars)
    for start, end in runs:
        weighted = volume = 0.0
        if start == 0:
            known, weighted, volume = _session_sums(bars, operand, context, labels[0], days)
            if not known:
                continue
        for i in range(start, end):
            amount = float(bars[i].get("volume") or 0)
            weighted += _typical(bars[i]) * amount
            volume += amount
            result[i] = weighted / volume if volume > 0 else None
    return result


def _session_sums(bars: list[dict], operand: OperandBase, context: SeriesContext | None, day: str,
                  days: list[str]) -> tuple[bool, float, float]:
    """(is the first day's opening bar known, Σ典型价×量 and Σ量 of its bars before bars[0]) — the
    分时均价 counterpart of `_session_prefix`."""
    schedule = context.schedule if context else None
    extended = (context.extended or {}).get(operand.timeframe) if context else None
    if extended is not None and isinstance(bars, SeriesWindow) and extended is bars.facts.bars:
        return bars.facts.session_sums(days, schedule, bars.start)   # a backtest: its whole series
    first = int(bars[0]["datetime"])
    if extended:
        times = [int(bar["datetime"]) for bar in extended]
        index = bisect_left(times, first)
        if index < len(times) and times[index] == first:
            weighted = volume = 0.0
            k = index - 1
            while k >= 0 and bar_trading_day(extended[k], days) == day:
                amount = float(extended[k].get("volume") or 0)
                weighted += _typical(extended[k]) * amount
                volume += amount
                k -= 1
            if k >= 0:
                return True, weighted, volume
            sessions = sessions_of_day(bar_trading_day(extended[0], days), schedule, days)
            return bool(sessions) and times[0] == sessions[0].start_ns, weighted, volume
    sessions = sessions_of_day(day, schedule, days)
    return bool(sessions) and first == sessions[0].start_ns, 0.0, 0.0


def _base_values(bars: list[dict], operand: OperandBase, context: SeriesContext | None) -> list[float | None]:
    name, period = operand.indicator, operand.period
    if name == "constant":
        return [operand.value] * len(bars)
    if name in DAILY_ANCHORS:
        return _anchor_values(bars, operand, context)
    if name in {"opening_range_high", "opening_range_low"}:
        return _opening_range(bars, operand, context)
    if name in {"session_high", "session_low"}:
        return _session_extreme(bars, operand, context)
    if name == "vwap":
        return _session_vwap(bars, operand, context)
    if name == "sar":
        return _sar(bars)
    if isinstance(bars, SeriesWindow) and (free := _window_free_start(operand)) is not None:
        start, count = bars.start, len(bars)
        whole = bars.facts.indicator(operand)
        return [None] * min(free, count) + whole[start + free:start + count]
    values = _closes(bars)
    if name == "close":
        return values
    if name == "ma":
        return [sum(values[i + 1 - period:i + 1]) / period if i + 1 >= period else None for i in range(len(bars))]
    if name.startswith("macd"):
        dif = [fast - slow for fast, slow in zip(_ema(values, 12), _ema(values, 26), strict=True)]
        dea = _ema(dif, 9)
        return dif if name == "macd_dif" else dea if name == "macd_dea" else [(a - b) * 2 for a, b in zip(dif, dea, strict=True)]
    if name.startswith("kd"):
        k = d = 50.0
        result = []
        for i, bar in enumerate(bars):
            sample = bars[max(0, i - 8):i + 1]
            low, high = min(float(b["low"]) for b in sample), max(float(b["high"]) for b in sample)
            rsv = 50 if high == low else (float(bar["close"]) - low) / (high - low) * 100
            k, d = (2 * k + rsv) / 3, d
            d = (2 * d + k) / 3
            # kd_j: KDJ's J = 3K − 2D (TqSdk ta.KDJ, 通达信), on the same KD(9,3,3) smoothing as K and D.
            result.append(k if name == "kd_k" else d if name == "kd_d" else 3 * k - 2 * d)
        return result
    if name in BAR_FIELDS:
        return _column(bars, name)
    if name in CANDLE_FIELDS:
        return [_candle(bar, name) for bar in bars]
    if name == "williams_r":
        # 威廉 %R = (N根最高 − 收盘) ÷ (N根最高 − N根最低) × (−100)，含当前根（TqSdk ta.WR），−100~0。
        result: list[float | None] = []
        for i, bar in enumerate(bars):
            if i + 1 < period:
                result.append(None)
                continue
            sample = bars[i + 1 - period:i + 1]
            high, low = max(float(b["high"]) for b in sample), min(float(b["low"]) for b in sample)
            result.append(None if high == low else (high - float(bar["close"])) / (high - low) * -100)
        return result
    if name == "bias":
        # 乖离率 = (收盘 − MA(N)) ÷ MA(N) × 100（TqSdk ta.BIAS）。
        average = _sma(list(values), period)
        return [None if m is None or m == 0 else (c - m) / m * 100 for c, m in zip(values, average, strict=True)]
    if name == "mfi":
        return _mfi(bars, period)
    if name == "cmi":
        # 震荡指数 CMI (Pruitt & Hill, Thermostat) = |C − C[N−1]| ÷ (N根最高 − N根最低) × 100，0~100，越大越有趋势。
        result = []
        for i in range(len(bars)):
            if i + 1 < period:
                result.append(None)
                continue
            sample = bars[i + 1 - period:i + 1]
            high, low = max(float(b["high"]) for b in sample), min(float(b["low"]) for b in sample)
            result.append(None if high == low else abs(values[i] - values[i + 1 - period]) / (high - low) * 100)
        return result
    if name == "bbi":
        # 多空指数 = (MA3 + MA6 + MA12 + MA24) ÷ 4（TqSdk ta.BBI）。
        lines = [_sma(list(values), n) for n in BBI_PERIODS]
        return [None if any(line[i] is None for line in lines) else sum(line[i] for line in lines) / len(lines)  # type: ignore[misc]
                for i in range(len(values))]
    if name == "roc":
        # ROC = (C ÷ C[N] − 1) × 100 (TqSdk ta.ROC, 通达信 ROC): the % change against N bars ago.
        return [None if i < period or not values[i - period] else (values[i] / values[i - period] - 1) * 100
                for i in range(len(values))]
    if name == "trix":
        return _trix(values, period)
    if name == "volume":
        return [float(bar["volume"]) for bar in bars]
    if name == "volume_ratio":
        volumes = [float(bar["volume"]) for bar in bars]
        return [volumes[i] / (sum(volumes[i - 20:i]) / 20) if i >= 20 and sum(volumes[i - 20:i]) > 0 else None
                for i in range(len(bars))]
    if name == "ema":
        return _warm(_ema(values, period), period)
    if name == "atr":
        return _atr(bars, period)
    if name in {"donchian_high", "donchian_low"}:
        field, pick = ("high", max) if name == "donchian_high" else ("low", min)
        return [pick(float(b[field]) for b in bars[i - period:i]) if i >= period else None for i in range(len(bars))]
    if name.startswith("boll"):
        middle = _sma(list(values), period)
        if name == "boll_mid":
            return middle
        width = operand.k or DEFAULT_K["boll"]
        sign = 1 if name == "boll_up" else -1
        bands: list[float | None] = []
        for i, center in enumerate(middle):
            if center is None:
                bands.append(None)
                continue
            deviation = math.sqrt(sum((v - center) ** 2 for v in values[i + 1 - period:i + 1]) / period)
            bands.append(center + sign * width * deviation)
        return bands
    if name.startswith("keltner"):
        middle = _sma(list(values), period) if operand.basis == "ma" else _warm(_ema(values, period), period)
        if name == "keltner_mid":
            return middle
        width = operand.k or DEFAULT_K["keltner"]
        sign = 1 if name == "keltner_up" else -1
        return [None if c is None or a is None else c + sign * width * a
                for c, a in zip(middle, _atr(bars, period), strict=True)]
    if name == "rsi":
        return _rsi(values, period)
    if name == "supertrend":
        return _supertrend(bars, period, operand.k or DEFAULT_K["supertrend"])
    if name == "cci":
        return _cci(bars, period)
    if name in {"plus_di", "minus_di", "adx"}:
        return _directional(bars, period)[("plus_di", "minus_di", "adx").index(name)]
    if name in {"oi_change", "oi_quadrant"}:
        return _open_interest(bars, operand)
    raise StrategyRuleError("unknown_indicator", name)


def indicator_values(bars: list[dict], operand: OperandBase, context: SeriesContext | None = None) -> list[float | None]:
    """The single indicator implementation. KD(9,3,3), MACD(12,26,9) exactly match the product chart;
    ATR / RSI use Wilder smoothing, Bollinger the population standard deviation, Donchian the
    previous N bars (the current bar excluded). Daily anchors need `context.daily`."""
    values = _base_values(bars, operand, context)
    if operand.average:
        values = _sma(values, operand.average)
    if operand.highest:
        values = _extreme(values, operand.highest, max)
    if operand.lowest:
        values = _extreme(values, operand.lowest, min)
    offset = getattr(operand, "offset", None)
    if offset is not None:
        other = indicator_values(bars, offset.term(operand.timeframe), context)
        values = [None if a is None or b is None else a + offset.factor * b for a, b in zip(values, other, strict=True)]
    if operand.barsAgo:
        # 前 N 根: bar i reads bar i − N of the same completed series (None until it exists), so it
        # only ever looks back — the same shift in a live window and in a backtest window.
        values = [None] * min(operand.barsAgo, len(values)) + values[:max(0, len(values) - operand.barsAgo)]
    return values


def _required_bars(name: str, period: int) -> int:
    return (35 if name.startswith("macd") else 9 if name.startswith("kd") else
            period if name in {"ma", "ema", "atr", "cci"} or name.startswith(("boll", "keltner")) else
            period + 1 if name in {"donchian_high", "donchian_low", "rsi", "supertrend", "oi_change", "oi_quadrant",
                                   "roc", "mfi"} else
            period if name in {"williams_r", "bias", "cmi"} else BBI_PERIODS[-1] if name == "bbi" else
            3 * (period - 1) + 2 if name == "trix" else
            21 if name == "volume_ratio" else DI_HISTORY + 1 if name in {"plus_di", "minus_di"} else
            2 * DI_HISTORY if name == "adx" else 2 if name == "sar" else 1)


def _required(operand: OperandBase) -> int:
    base = _required_bars(operand.indicator, operand.period)
    if operand.average:
        base += operand.average - 1
    if operand.highest or operand.lowest:
        base += (operand.highest or operand.lowest or 1) - 1
    offset = getattr(operand, "offset", None)   # its term is a plain operand of the offset's indicator and period
    if offset is not None:
        base = max(base, _required_bars(offset.indicator, offset.period))
    return base + operand.barsAgo


def _lookback(condition: Condition) -> int:
    """Bars before a judged bar the operator reads besides the previous one: rising / falling compare
    with k bars ago, consecutive N needs the N − 1 bars before."""
    back = int(condition.right.value) if condition.operator in {"rising", "falling"} else 0
    return back + condition.consecutive - 1


def required_condition_bars(condition: Condition) -> int:
    return max(_required(condition.left), _required(condition.right)) + condition.withinBars + _lookback(condition)


def _format(value: float | None) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:.4f}"


def _passes(operator: str, a: float | None, b: float | None, previous_a: float | None, previous_b: float | None) -> bool:
    if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
        return False
    if operator in {"cross_up", "cross_down"}:
        if previous_a is None or previous_b is None or not math.isfinite(previous_a) or not math.isfinite(previous_b):
            return False
        return a > b and previous_a <= previous_b if operator == "cross_up" else a < b and previous_a >= previous_b
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b, "eq": a == b}[operator]


def condition_test(condition: Condition, left: list[float | None], right: list[float | None]):
    """Whether the condition holds on bar i (i ≥ 1) of the series `left` / `right` were computed on:
    the comparison, rising / falling against `right.value` bars ago, and `consecutive` bars in a row.
    withinBars is not applied here (callers widen the window)."""
    operator = condition.operator
    if operator in {"rising", "falling"}:
        back = int(condition.right.value)

        def one(i: int) -> bool:
            if i < back:
                return False
            a, b = left[i], left[i - back]
            if a is None or b is None or not math.isfinite(a) or not math.isfinite(b):
                return False
            return a > b if operator == "rising" else a < b
    else:
        def one(i: int) -> bool:
            return _passes(operator, left[i], right[i], left[i - 1], right[i - 1])
    count = condition.consecutive
    if count == 1:
        return one
    return lambda i: i >= count and all(one(j) for j in range(i - count + 1, i + 1))


def _operand_key(operand: OperandBase) -> tuple:
    offset = getattr(operand, "offset", None)
    return (operand.indicator, operand.timeframe, operand.period, operand.value, operand.k, operand.basis,
            operand.average, None if offset is None else (offset.factor, offset.indicator, offset.period),
            operand.barsAgo, operand.highest, operand.lowest)


def _condition(condition: Condition, bars: list[dict], context: SeriesContext | None = None,
               memo: dict[tuple, list[float | None]] | None = None) -> dict:
    """`memo` (one evaluation's cache): an operand read twice on the same window — MA10 in both the
    entry and the opposite exit — is computed once. Same values, only fewer passes."""
    required = required_condition_bars(condition)
    if len(bars) < required:
        raise StrategyRuleError("strategy_bars_insufficient")

    def values(operand: OperandBase) -> list[float | None]:
        if memo is None:
            return indicator_values(bars, operand, context)
        key = (condition.left.timeframe, _operand_key(operand))
        if key not in memo:
            memo[key] = indicator_values(bars, operand, context)
        return memo[key]
    left, right = values(condition.left), values(condition.right)
    holds = condition_test(condition, left, right)
    hits = [int(bars[i]["datetime"]) for i in range(max(1, len(bars) - condition.withinBars), len(bars)) if holds(i)]
    if len(hits) < condition.minCount:     # N 根内至少 M 根: fewer than M holding bars is not a pass
        hits = []
    return {"label": condition.label, "status": "passed" if hits else "waiting", "hits": hits,
            "detail": f"{condition.left.timeframe // 60} 分钟 · {_format(left[-1])} / {_format(right[-1])}",
            "time": hits[-1] if hits else None}


def _last_hit(conditions: list[Condition], bars: list[dict], context: SeriesContext | None,
              memo: dict[tuple, list[float | None]]) -> int | None:
    """复位: the time of the latest bar in `bars` on which every condition holds (each with its own
    withinBars window); None when none does or the history is too short to tell."""
    if not conditions or any(len(bars) < required_condition_bars(c) for c in conditions):
        return None
    tests = []
    for condition in conditions:
        sides = []
        for operand in (condition.left, condition.right):
            key = (condition.left.timeframe, _operand_key(operand))
            if key not in memo:
                memo[key] = indicator_values(bars, operand, context)
            sides.append(memo[key])
        tests.append((condition_test(condition, sides[0], sides[1]), condition.withinBars, condition.minCount))
    for i in range(len(bars) - 1, 0, -1):
        if all(sum(test(j) for j in range(max(1, i - within + 1), i + 1)) >= least for test, within, least in tests):
            return int(bars[i]["datetime"])
    return None


def _schedule_of(workbenches: dict[int, dict]) -> dict | None:
    for item in workbenches.values():
        quote = item.get("quote") if isinstance(item, dict) else None
        if isinstance(quote, dict) and isinstance(quote.get("tradingSchedule"), dict):
            return quote["tradingSchedule"]
    return None


def engine_atr_periods(plan: StrategyPlan) -> list[int]:
    periods = {plan.atrStop.period if plan.atrStop else None,
               plan.trailingStop.atrPeriod if plan.trailingStop and plan.trailingStop.mode == "atr_chandelier" else None,
               plan.sizing.atrPeriod if plan.sizing and plan.sizing.mode == "atr_risk" else None,
               plan.pyramiding.atrPeriod if plan.pyramiding and plan.pyramiding.trigger == "profit_atr" else None,
               EXIT_ATR_PERIOD if uses_exit_atr(plan) else None}
    return sorted(p for p in periods if p)


def uses_exit_atr(plan: StrategyPlan | dict) -> bool:
    """保本 / 分段止盈 / 分批止盈 measured in ATR: the signal bar's ATR14 is frozen with the entry."""
    if isinstance(plan, StrategyPlan):
        return (plan.breakeven is not None and plan.breakeven.triggerAtr is not None) or any(
            target.profitAtr is not None for target in plan.timeTakeProfit) or any(
            tier.profitAtr is not None for tier in plan.scaleOut)
    breakeven = plan.get("breakeven") or {}
    return (breakeven.get("triggerAtr") is not None
            or any(target.get("profitAtr") is not None for target in plan.get("timeTakeProfit") or [])
            or any(tier.get("profitAtr") is not None for tier in plan.get("scaleOut") or []))


def evaluate_rules(plan: StrategyPlan, workbenches: dict[int, dict], *,
                   extended: dict[int, list[dict]] | None = None) -> dict:
    """Provider workbenches: every series ends with the still-forming bar, never a signal input.
    `extended`: longer completed series for 当日最高 / 最低 (see `session_history`)."""
    if uses_chart_timeframe(plan):
        raise StrategyRuleError("chart_timeframe_required", "请先选择图表周期，再验证策略")
    completed = {timeframe: item["bars"][:-1] for timeframe, item in workbenches.items()}
    daily = workbenches[86400]["bars"] if 86400 in workbenches else None
    return evaluate_completed(plan, completed, daily=daily, schedule=_schedule_of(workbenches), extended=extended)


def evaluate_completed(plan: StrategyPlan, bars_by_timeframe: dict[int, list[dict]], *,
                       daily: list[dict] | None = None, schedule: dict | None = None,
                       extended: dict[int, list[dict]] | None = None) -> dict:
    """Evaluate on completed bars only; the last bar of the execution timeframe is the decision
    bar. `daily` (defaults to the 86400 series) may also hold the current trading day's bar — only
    its open is read, by 今开. A slower series only contributes bars completed by the decision
    time; for 86400 that means trading days before the decision bar's trading day."""
    if uses_chart_timeframe(plan):
        raise StrategyRuleError("chart_timeframe_required", "请先选择图表周期，再验证策略")
    base = bars_by_timeframe[plan.timeframeSeconds]
    if not base:
        raise StrategyRuleError("strategy_bars_insufficient")
    context = SeriesContext(daily=daily if daily is not None else bars_by_timeframe.get(86400), schedule=schedule,
                            extended=extended)
    known_days = _daily_facts(context)[0]
    decision_day = bar_trading_day(base[-1], known_days)
    cutoff = int(base[-1]["datetime"]) + plan.timeframeSeconds * NS

    windows: dict[int, list[dict]] = {}
    memo: dict[tuple, list[float | None]] = {}

    def window(timeframe: int) -> list[dict]:
        if timeframe not in windows:
            series = bars_by_timeframe[timeframe]
            if timeframe == 86400 and plan.timeframeSeconds != 86400:
                windows[timeframe] = [b for b in series if daily_bar_day(b) < decision_day]
            elif (isinstance(series, SeriesWindow) and series.facts.ordered
                  and (not series or int(series[-1]["datetime"]) + timeframe * NS <= cutoff)):
                windows[timeframe] = series   # time-ordered and its last bar is done: the filter keeps it all
            else:
                windows[timeframe] = [b for b in series if int(b["datetime"]) + timeframe * NS <= cutoff]
        return windows[timeframe]

    results, details, entry_alignments = {}, [], {}
    for name in ("entryLong", "exitLong", "entryShort", "exitShort"):
        group = getattr(plan.rules, name)
        if group is None:
            results[name] = False
            continue
        checks = []
        for condition in group_conditions(group):
            timeframe = condition.left.timeframe
            try:
                check = _condition(condition, window(timeframe), context, memo)
            except StrategyRuleError as error:
                period = "日线" if timeframe == 86400 else f"{timeframe // 60} 分钟"
                raise StrategyRuleError(error.code, f"{period}：{error}") from error
            checks.append(check)
            details.append({**check, "group": name})
        passed = [bool(c["hits"]) for c in checks]
        result = group_passes(group, passed[len(group.alignmentConditions):])
        if group.sequence and result:
            previous = -1
            for check in checks:
                following = next((t for t in check["hits"] if t > previous), None)
                if following is None:
                    result = False
                    break
                previous = following
        if group.alignmentConditions:
            from .strategy_alignment import evaluate_alignment
            result, alignment, detail = evaluate_alignment(group, base, name, context)
            if alignment:
                entry_alignments[name] = alignment
            details.append({**detail, "group": name})
        results[name] = result
    structures = {}
    if plan.pullback:
        from .strategy_pullback import evaluate_pullback
        entries, structures, setup_details = evaluate_pullback(plan, base, context)
        results.update(entries)
        details.extend(setup_details)
    if plan.stopLookbackBars is not None:
        if len(base) < plan.stopLookbackBars:
            raise StrategyRuleError("strategy_bars_insufficient", "最近K线结构止损所需历史K线不足")
        sample = base[-plan.stopLookbackBars:]
        for name in ("entryLong", "entryShort"):
            if results[name]:
                structures[name] = {"startTime": str(sample[0]["datetime"]),
                    "low": min(float(b["low"]) for b in sample), "high": max(float(b["high"]) for b in sample),
                    "signalTime": str(base[-1]["datetime"])}
    # 盘中触价: the level each armed entry waits for, the trigger operand's value on the decision bar.
    triggers: dict[str, float | None] = {}
    for name in ("entryLong", "entryShort"):
        trigger = trigger_of(getattr(plan.rules, name))
        if trigger is not None and results[name]:
            key = (plan.timeframeSeconds, _operand_key(trigger.price))
            if key not in memo:
                memo[key] = indicator_values(window(plan.timeframeSeconds), trigger.price, context)
            level = memo[key][-1] if memo[key] else None
            triggers[name] = level if level is not None and math.isfinite(level) else None
    # Bar-derived facts strategy_engine.decide needs about the decision bar.
    def engine_atr(period: int) -> float | None:
        operand = Operand(indicator="atr", period=period, timeframe=plan.timeframeSeconds)
        key = (plan.timeframeSeconds, _operand_key(operand))
        if key not in memo:
            memo[key] = indicator_values(base, operand)
        return memo[key][-1]
    engine = {"barTime": str(base[-1]["datetime"]), "barClose": cutoff, "tradingDay": decision_day,
              "close": float(base[-1]["close"]), "schedule": schedule,
              "atr": {str(p): engine_atr(p) for p in engine_atr_periods(plan)}}
    rearm = {name: _last_hit(rearm_conditions(getattr(plan.rules, name)), window(plan.timeframeSeconds), context, memo)
             for name in ("entryLong", "entryShort") if rearm_conditions(getattr(plan.rules, name))}
    if rearm:
        engine["rearm"] = rearm
    return {"rules": results, "conditions": details, "structures": structures,
            "entryAlignments": entry_alignments, "triggers": triggers,
            "evaluatedBarTime": str(base[-1]["datetime"]), "engine": engine}


def _plan_operands(plan: StrategyPlan):
    for group in (plan.rules.entryLong, plan.rules.exitLong, plan.rules.entryShort, plan.rules.exitShort):
        trigger = trigger_of(group)
        for condition in all_conditions(group):
            for operand in (condition.left, condition.right):
                yield operand
                if operand.offset is not None:
                    yield operand.offset.term(operand.timeframe)
        if trigger is not None:
            yield trigger.price
            if trigger.price.offset is not None:
                yield trigger.price.offset.term(trigger.price.timeframe)


def uses_daily_anchor(plan: StrategyPlan) -> bool:
    return any(operand.indicator in DAILY_ANCHORS for operand in _plan_operands(plan))


def plan_timeframes(plan: StrategyPlan) -> set[int]:
    """Every series an evaluation reads; daily anchors add the 86400 series."""
    return {plan.timeframeSeconds} | {condition.left.timeframe
        for group in (plan.rules.entryLong, plan.rules.exitLong, plan.rules.entryShort, plan.rules.exitShort)
        for condition in all_conditions(group)} | ({86400} if uses_daily_anchor(plan) else set())


def uses_structural_stop(definition: dict) -> bool:
    plan = definition.get("plan", {})
    return bool(plan.get("pullback") or plan.get("stopLookbackBars"))


def requires_entry_recheck(definition: dict) -> bool:
    """Structural stops and direction segments depend on the signal bar staying current, so the
    order is held to that bar and re-read when it is sent. The ATR stop / chandelier ATR does not:
    it is frozen on the signal bar and travels with the order (signalSnapshot.protectionLevels)."""
    rules = definition.get("plan", {}).get("rules", {})
    return uses_structural_stop(definition) or any(
        (rules.get(name) or {}).get("alignmentConditions") for name in ("entryLong", "entryShort"))


def rule_position_intent(results: dict, direction: str, long: int, short: int, maximum: int) -> dict:
    action, volume = "hold", 0
    if long and results["exitLong"]:
        action, volume = "close_long", long
    elif short and results["exitShort"]:
        action, volume = "close_short", short
    elif not long and not short:
        buy = direction in {"long", "both"} and results["entryLong"]
        sell = direction in {"short", "both"} and results["entryShort"]
        if buy != sell:
            action, volume = ("open_long" if buy else "open_short"), maximum
    return {"action": action, "requestedVolume": volume, "currentLong": long, "currentShort": short,
            "targetLong": maximum if action == "open_long" else 0 if action == "close_long" else long,
            "targetShort": maximum if action == "open_short" else 0 if action == "close_short" else short,
            "reason": "条件已满足" if volume else "继续等待；多空信号冲突时不开仓"}


# -- Chinese labels for the building blocks (detail page, plan card, 策略编辑卡) -----------------------

OPERATOR_TEXT = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "cross_up": "上穿", "cross_down": "下穿",
                 "rising": "较{k}根前上升", "falling": "较{k}根前下降", "eq": "="}


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def describe_operand(operand: OperandBase) -> str:
    n, k = operand.period, operand.k
    labels = {
        "constant": _number(operand.value), "close": "收盘价", "ma": f"MA{n}", "ema": f"EMA{n}",
        "macd_dif": "MACD DIF", "macd_dea": "MACD DEA", "macd_hist": "MACD 柱", "kd_k": "KD K", "kd_d": "KD D",
        "volume": "成交量", "volume_ratio": "放量倍数", "atr": f"ATR{n}",
        "donchian_high": f"前{n}根最高", "donchian_low": f"前{n}根最低",
        "boll_mid": f"布林中轨({n})", "boll_up": f"布林上轨({n},{_number(k or DEFAULT_K['boll'])})",
        "boll_down": f"布林下轨({n},{_number(k or DEFAULT_K['boll'])})",
        "keltner_mid": f"肯特纳中轨({n})", "keltner_up": f"肯特纳上轨({n},{_number(k or DEFAULT_K['keltner'])})",
        "keltner_down": f"肯特纳下轨({n},{_number(k or DEFAULT_K['keltner'])})",
        "rsi": f"RSI{n}", "supertrend": f"超级趋势({n},{_number(k or DEFAULT_K['supertrend'])})",
        "prev_high": "昨高", "prev_low": "昨低", "prev_close": "昨收", "today_open": "今开",
        "range_nday": f"{n}日振幅", "gap_percent": "开盘跳空%",
        "opening_range_high": f"开盘{n}根最高", "opening_range_low": f"开盘{n}根最低",
        "cci": f"CCI{n}", "adx": f"ADX{n}", "plus_di": f"+DI{n}", "minus_di": f"−DI{n}", "sar": "抛物线SAR",
        "session_high": "当日最高", "session_low": "当日最低",
        "pivot": PIVOT_LEVELS.get(int(operand.value), "枢轴") if float(operand.value).is_integer() else "枢轴",
        "oi_change": f"持仓量较{n}根前变化", "oi_quadrant": "持仓四象限",
        "roc": f"{n}根涨跌幅%", "kd_j": "KDJ J", "trix": f"TRIX{n}", "high": "最高价", "low": "最低价", "open": "开盘价",
        "williams_r": f"威廉%R{n}", "bias": f"乖离率{n}", "mfi": f"MFI{n}", "bbi": "BBI", "cmi": f"震荡指数CMI{n}", "bar_range": "K线振幅",
        "body": "K线实体", "upper_shadow": "上影线", "lower_shadow": "下影线", "vwap": "分时均价",
    }
    text = labels[operand.indicator]
    if operand.average:
        text = f"{text}的{operand.average}均线"
    if operand.highest or operand.lowest:
        text = f"{text}的{operand.highest or operand.lowest}根{'最高' if operand.highest else '最低'}"
    offset = getattr(operand, "offset", None)
    if offset is not None:
        term = describe_operand(offset.term(operand.timeframe))
        text = f"{text} {'+' if offset.factor > 0 else '−'} {_number(abs(offset.factor))}×{term}"
    if operand.barsAgo:
        text = f"{operand.barsAgo}根前的{text}"
    return text


def describe_condition(condition: Condition) -> str:
    left, operator = describe_operand(condition.left), condition.operator
    if operator in {"rising", "falling"}:
        text = f"{left} {OPERATOR_TEXT[operator].format(k=_number(condition.right.value))}"
    elif operator == "eq":
        text = f"{left} = {OI_QUADRANTS.get(int(condition.right.value), _number(condition.right.value))}"
    else:
        text = f"{left} {OPERATOR_TEXT[operator]} {describe_operand(condition.right)}"
    if condition.consecutive > 1:
        text = f"{text}（连续 {condition.consecutive} 根）"
    return f"{text}（{condition.withinBars}根内至少{condition.minCount}根）" if condition.minCount > 1 else text


def plan_feature_lines(plan: StrategyPlan) -> list[tuple[str, str]]:
    """Plan-level blocks (exits, sizing, entry gates) as (kind, text) rows in plain Chinese."""
    lines: list[tuple[str, str]] = []
    if plan.spread is not None:
        from .strategy_spread import spread_legs_words, spread_words
        lines.append(("价差", f"{spread_words(plan.spread)}；做多价差一份 = {spread_legs_words(plan.spread, 'open_long', 1)}"
                                f"，做空价差反过来，最多 {_number(plan.maxPosition)} 份"))
        if plan.spread.stopLoss is not None:
            lines.append(("价差止损", f"价差较开仓价差浮亏达 {_number(plan.spread.stopLoss)} 点即两腿平仓（实盘每秒按两腿最新价判断，回测按收盘）"))
        if plan.spread.takeProfit is not None:
            lines.append(("价差止盈", f"价差较开仓价差浮盈达 {_number(plan.spread.takeProfit)} 点即两腿平仓（实盘每秒按两腿最新价判断，回测按收盘）"))
    if plan.atrStop:
        lines.append(("止损", f"{_number(plan.atrStop.multiple)}×ATR{plan.atrStop.period} 止损（取信号K线的ATR，成交后换算成固定价）"))
    trailing = plan.trailingStop
    if trailing:
        unit, distance = {"percent": ("%", f"回撤 {_number(trailing.value)}%"),
                          "ticks": (" 跳", f"回撤 {_number(trailing.value)} 跳"),
                          "atr_chandelier": ("×ATR", f"回撤 {_number(trailing.value)}×ATR{trailing.atrPeriod}")}[trailing.mode]
        text = f"从持仓后的最高/最低价{distance}追踪止损，只收紧不放松"
        if trailing.activateAfter:
            text += f"；浮盈达 {_number(trailing.activateAfter)}{unit} 后开始"
        lines.append(("追踪止损", text))
    if plan.breakeven:
        trigger = (f"{plan.breakeven.triggerTicks} 跳" if plan.breakeven.triggerTicks is not None
                   else f"{_number(plan.breakeven.triggerAtr or 0)}×ATR{EXIT_ATR_PERIOD}")
        offset = plan.breakeven.offsetTicks
        place = f"开仓价盈利一侧 {offset} 跳" if offset else "开仓价"
        lines.append(("保本", f"浮盈达 {trigger} 后止损移到{place}，只收紧不放松"))
    if plan.timeTakeProfit:
        tiers = "；".join(
            f"持仓满 {target.afterBars} 根起浮盈 ≥ "
            + (f"{target.profitTicks} 跳" if target.profitTicks is not None else f"{_number(target.profitAtr or 0)}×ATR{EXIT_ATR_PERIOD}")
            for target in sorted(plan.timeTakeProfit, key=lambda item: item.afterBars))
        lines.append(("分段止盈", f"K线收盘时判断：{tiers}，达到就平仓"))
    if plan.pyramiding:
        pyramid = plan.pyramiding
        trigger = {"profit_atr": f"每盈利 {_number(pyramid.step)} 倍 ATR({pyramid.atrPeriod})",
                   "profit_ticks": f"每盈利 {_number(pyramid.step)} 跳",
                   "signal": "同向开仓信号再成立且收盘更优时"}[pyramid.trigger]
        lots = f"{pyramid.volume} 手" if pyramid.volume else "首次开仓的手数"
        reserve = max(1, plan.maxPosition - (pyramid.volume or 1) * pyramid.maxAdds)
        moved = "，止损随最近一次加仓收紧" if pyramid.moveStop else ""
        lines.append(("加仓", f"{trigger}加 {lots}，最多加 {pyramid.maxAdds} 次{moved}；"
                                f"首笔开 {_number(reserve)} 手给加仓留空间（最大持仓 {_number(plan.maxPosition)} − "
                                f"每次加仓手数 × 最多加仓次数，最少 1 手；最大持仓 4、最多加 3 次时首笔 1 手、每次加 1 手）"))
    if plan.scaleOut:
        parts = [f"盈利 {_number(tier.profitTicks)} 跳" if tier.profitTicks is not None
                 else f"盈利 {_number(tier.profitAtr or 0)} 倍 ATR({EXIT_ATR_PERIOD})" for tier in plan.scaleOut]
        lines.append(("分批止盈", f"{'，'.join(f'{target}平 {tier.percent}%' for target, tier in zip(parts, plan.scaleOut, strict=True))}"
                                  "；按持仓均价判断，每档一次"))
    if plan.closeBeforeEndMinutes:
        scope = "每个交易时段" if plan.closeBeforeEndScope == "session" else "当日最后一个时段"
        lines.append(("收盘前平仓", f"{scope}收盘前 {plan.closeBeforeEndMinutes} 分钟平仓"))
    if plan.maxHoldBars:
        lines.append(("最长持仓", f"持仓满 {plan.maxHoldBars} 根K线平仓"))
    if plan.sizing and plan.sizing.mode == "atr_risk":
        lines.append(("仓位", f"按 ATR 定手数：权益×{_number(plan.sizing.riskPercent or 0)}% ÷ "
                              f"(合约乘数×{_number(plan.sizing.atrMultiple)}×ATR{plan.sizing.atrPeriod})，最多 {plan.maxPosition} 手"))
    if plan.entryWindows:
        lines.append(("开仓时段", "、".join(f"{w.start}–{w.end}" for w in plan.entryWindows)))
    if not plan.allowNightEntries:
        lines.append(("开仓时段", "夜盘不开新仓"))
    if plan.maxEntriesPerDirection:
        lines.append(("开仓次数", f"每个交易日每个方向最多 {plan.maxEntriesPerDirection} 次"))
    for name, side in (("entryLong", "开多"), ("entryShort", "开空")):
        trigger = trigger_of(getattr(plan.rules, name))
        if trigger is not None:
            rises = (name == "entryLong") == (trigger.kind == "stop")
            lines.append(("盘中触价", f"{side}条件在收盘成立后挂价「{describe_operand(trigger.price)}」，下一根K线里价格"
                                      f"{'涨到' if rises else '跌到'}它就{side}，没触及就作废"))
        rearm = rearm_conditions(getattr(plan.rules, name))
        if rearm:
            lines.append(("复位", f"{side}后须等「{'且'.join(describe_condition(c) for c in rearm)}」成立一次才再{side}"))
    return lines


CLOSE_SCHEDULE_REQUIRED = "拿不到这个合约的交易时段，无法保证收盘前平仓"


def require_close_schedule(provider, definition: dict, symbol: str) -> None:
    """上线运行: a strategy that closes before the bell needs the contract's session schedule;
    without it the close could silently never happen, so it does not go live."""
    plan = definition.get("plan") or {}
    if not plan.get("closeBeforeEndMinutes"):
        return
    try:
        quote = provider.market_workbench(symbol, int(plan.get("timeframeSeconds") or 60)).get("quote")
    except (KeyError, ValueError, RuntimeError):
        quote = None
    if parse_schedule((quote or {}).get("tradingSchedule")) is None:
        raise StrategyRuleError("trading_schedule_unavailable", CLOSE_SCHEDULE_REQUIRED)


def evaluate_plan(provider, definition: dict, symbol: str, *, require_fresh: bool = True) -> dict:
    """`require_fresh=False` only asks whether the rules compute on the closed bars at hand (a chart
    period change after the close): no order path may use it — the monitor re-checks freshness itself."""
    plan = StrategyPlan.model_validate(definition["plan"])
    plan.check_executable()
    if uses_chart_timeframe(plan):
        raise StrategyRuleError("chart_timeframe_required", "请先选择图表周期，再验证策略")
    if plan.spread is not None:
        # A spread plan reads both legs' workbenches itself (its rules judge the joined spread series).
        from .strategy_spread import spread_evaluation
        return spread_evaluation(provider, plan, require_fresh=require_fresh)
    workbenches = {}
    for timeframe in plan_timeframes(plan):
        period = "日线" if timeframe == 86400 else f"{timeframe // 60} 分钟"
        try:
            item = provider.market_workbench(symbol, timeframe)
        except (KeyError, ValueError, RuntimeError) as error:
            raise workbench_read_error(error, f"{period}行情读取失败，请刷新行情后重试") from error
        if require_fresh and (item.get("dataStatus") not in {"normal", "simulated"} or item.get("isCached") is True
                or float(item.get("ageSeconds", 999)) > 15):
            raise StrategyRuleError("strategy_market_not_fresh", f"{period}：strategy_market_not_fresh")
        workbenches[timeframe] = item
    extended, pending = session_history(provider, plan, symbol, workbenches)
    # 规则只在新 K 线收盘时重算 (2026-10-22): the rules read closed bars only, so between two closes the same
    # inputs give the same evaluation — reused instead of recomputed every second of monitoring.
    slot, inputs = (None, None) if pending else _evaluation_inputs(definition, symbol, workbenches, extended)
    if slot is not None:
        with _EVALUATIONS_LOCK:
            kept = _EVALUATIONS.get(slot)
            if kept is not None and kept[0] == inputs:
                _EVALUATIONS.move_to_end(slot)
                # A top-level copy: callers set top-level fields (dataPending) and never change the nested ones.
                return dict(kept[1])
    evaluation = evaluate_rules(plan, workbenches, extended=extended)
    if slot is not None:
        with _EVALUATIONS_LOCK:
            _EVALUATIONS[slot] = (inputs, dict(evaluation))
            _EVALUATIONS.move_to_end(slot)
            while len(_EVALUATIONS) > EVALUATIONS_KEPT:
                _EVALUATIONS.popitem(last=False)
    if pending:
        # 数据未就绪: no rule signal from a truncated day — no entry, no rule exit, no reversal. The engine
        # still runs the time exits; the armed protection runs on its own.
        evaluation["rules"] = {name: False for name in evaluation["rules"]}
        evaluation["dataPending"] = SESSION_DATA_PENDING
    return evaluation


# The last evaluation per (symbol, plan) with the exact inputs it was computed from (`_evaluation_inputs`).
EVALUATIONS_KEPT = 256
_EVALUATIONS: OrderedDict[tuple[str, str], tuple[tuple, dict]] = OrderedDict()
_EVALUATIONS_LOCK = threading.Lock()


def _evaluation_inputs(definition: dict, symbol: str, workbenches: dict[int, dict],
                       extended: dict[int, list[dict]] | None) -> tuple[tuple[str, str] | None, tuple | None]:
    """((symbol, plan), everything `evaluate_rules` reads): every closed bar of each period in full; of the still
    forming bar only the daily one counts, by its trading day and open (今开 — `daily_index`'s callers read nothing
    else of it); the trading schedule; the stitched same-day history. (None, None) when a value is not hashable."""
    try:
        parts: list[Any] = []
        for timeframe in sorted(workbenches):
            bars = workbenches[timeframe].get("bars") or []
            parts.append((timeframe, tuple(tuple(bar.items()) for bar in bars[:-1])))
            if timeframe == 86400 and bars:
                parts.append(("forming", daily_bar_day(bars[-1]), bars[-1].get("open")))
        for timeframe, bars in sorted((extended or {}).items()):
            parts.append(("extended", timeframe, tuple(tuple(bar.items()) for bar in bars)))
        parts.append(json.dumps(_schedule_of(workbenches), sort_keys=True, default=str))
        inputs = tuple(parts)
        hash(inputs)
        return (symbol, json.dumps(definition["plan"], sort_keys=True, default=str)), inputs
    except (TypeError, ValueError):
        return None, None


# Completed bars read before the live window for 当日最高 / 最低: the longest trading day at 1 minute is
# ~555 bars (夜盘到 02:30 + 日盘), far inside this.
SESSION_HISTORY_BARS = 1500


def session_timeframes(plan: StrategyPlan) -> set[int]:
    return {operand.timeframe for operand in _plan_operands(plan) if operand.indicator in SESSION_INDICATORS}


SESSION_DATA_PENDING = ("当日最高 / 最低或分时均价所需的当天更早K线还没读到，本次不判定开仓与规则出场，下一次轮询再判定；"
                        "止损、止盈、追踪、保本与时间出场照常。")


def session_history(provider, plan: StrategyPlan, symbol: str,
                    workbenches: dict[int, dict]) -> tuple[dict[int, list[dict]] | None, bool]:
    """当日最高 / 最低 live: (longer series per clock, whether the day's first bar is still unknown).

    When the workbench window (255 completed bars) lies inside the current trading day — a 1-minute
    afternoon — the day's earlier bars come from the provider's read-only history (cached per contract
    and period, `SESSION_HISTORY_BARS` deep) stitched before the window, so the indicator starts at
    the day's first bar like a backtest reading its whole series. While that history is not there
    the second value is True: the caller then judges no rule at all this time (see `evaluate_plan`)."""
    # The sidecar provider answers from its cache without waiting (a miss starts one background read);
    # providers without that read directly.
    reader = getattr(provider, "market_history_nowait", None) or getattr(provider, "market_history", None)
    days = daily_index(workbenches[86400]["bars"])[0] if 86400 in workbenches else []
    schedule = _schedule_of(workbenches)
    extended: dict[int, list[dict]] = {}
    pending = False
    for timeframe in session_timeframes(plan):
        window = (workbenches.get(timeframe) or {}).get("bars", [])[:-1]
        if not window:
            continue
        day = bar_trading_day(window[-1], days)
        probe = OperandBase(indicator="session_high", timeframe=timeframe)
        if bar_trading_day(window[0], days) != day or _session_prefix(
                window, probe, SeriesContext(schedule=schedule), day, days, "high")[0]:
            continue   # the window already shows the day's first bar
        history: list[dict] = []
        if callable(reader):
            try:
                history = list(reader(symbol, timeframe, SESSION_HISTORY_BARS).get("bars") or [])
            except (KeyError, ValueError, RuntimeError, TimeoutError):
                history = []
        first = int(window[0]["datetime"])
        # Stitched only when the history overlaps the window (it holds the window's first bar), so no
        # bar between the two is missing — an old cached read that ends earlier is not used.
        overlaps = any(int(bar["datetime"]) == first for bar in history)
        stitched = ([bar for bar in history if int(bar["datetime"]) < first] if overlaps else []) + window
        if _session_prefix(window, probe, SeriesContext(schedule=schedule, extended={timeframe: stitched}),
                           day, days, "high")[0]:
            extended[timeframe] = stitched
        else:
            pending = True
    return extended or None, pending
