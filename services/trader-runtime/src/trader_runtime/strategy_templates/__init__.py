"""策略模板库 (策略积木·第二期 A, 2026-09-25): classic strategies as data, not code.

Each ``*.json`` beside this file is one template: a complete ``StrategyPlan`` with its default
parameters, a table of tunable parameters (each mapped to the plan fields it sets), a one-line use
case and the verified source. Instantiating a template only fills in numbers and produces an
ordinary ``StrategyPlan`` — it goes through the same validation, backtest, 上线 and protection as a
plan the user designed; there is no template-specific execution path.

Every template is validated with ``StrategyPlan`` (and ``check_executable``) when the library loads;
a template that does not validate is an error at startup, never silently skipped.

说人话 (2026-09-25): each template also carries ``plainSummary`` (one sentence without jargon — no
indicator names or parameter letters), ``level`` and ``holding``, and each parameter a ``help``
{what, up, down} (what it is, what raising / lowering it does). The load check requires them all and
keeps ``holding`` true to the plan: 日内 exactly when the plan closes before the session end.
``level`` 新手 = 顺势, the signal watches one thing (a line or a channel), at most two signal
parameters, one period, no extra gate and no multi-step state (复位 / 观察价) — 双均线、SuperTrend、
唐奇安、Aberration、肯特纳通道、菲阿里四价、连涨连跌; everything else is 进阶 (逆势, two periods, a volatility or
gap gate, 挤压 / 观察价 state, or three signal parameters).
"""
from __future__ import annotations

import copy
import json
import math
import re
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..instruments import STRATEGY_CONTRACT_PATTERN
from ..strategy_rules import StrategyPlan

TEMPLATE_DIR = Path(__file__).resolve().parent
CATEGORIES: dict[str, str] = {"trend_breakout": "趋势突破", "ma_trend": "均线趋势",
                              "channel_reversion": "通道回归", "reversal": "反转", "intraday_range": "日内区间"}
Category = Literal["trend_breakout", "ma_trend", "channel_reversion", "reversal", "intraday_range"]
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# A concrete contract code, the same shape a spread leg's symbol takes (strategy_rules.SpreadLeg).
CONTRACT = re.compile(rf"^{STRATEGY_CONTRACT_PATTERN}$")
# plainSummary is for someone who knows no indicator: no Latin letters (ATR, RSI, K1…) besides “K线”.
JARGON = re.compile(r"[A-Za-z]")
# Gallery order inside each category (the baseline first); a template not listed goes last.
ORDER = ("donchian_turtle", "turtle_pyramid", "boll_channel", "supertrend", "squeeze", "king_keltner", "consecutive_updown", "thermostat",
         "double_ma", "multi_timeframe", "atr_rsi", "aberration", "spread_boll_reversion", "rsi_reversal", "williams_r",
         "pivot_reversal", "z_score", "dual_thrust", "fairy_four_price", "nr7_breakout", "sky_garden", "r_breaker")


class TemplateError(ValueError):
    """A parameter or period the template cannot take; the message is the Chinese reason (422)."""


class ParamTarget(BaseModel):
    """plan field = value × scale + add (a dotted path; list items by index)."""
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    scale: float = 1
    add: float = 0


class ParamHelp(BaseModel):
    """The “?” beside a parameter, in plain words: what it is, what a larger / smaller value does."""
    model_config = ConfigDict(extra="forbid")
    what: str = Field(min_length=1, max_length=120)
    up: str = Field(min_length=1, max_length=80)
    down: str = Field(min_length=1, max_length=80)


class TemplateParam(BaseModel):
    """A tunable number, or (价差模板) a concrete contract code: ``type: "contract"`` has no range or
    step — the value fills its target (e.g. a spread leg's symbol) as the string it is.

    ``optional`` (2026-10-08, 当日亏损上限): a number the user may leave empty — the value is then
    None and its targets are set to null (不设). An optional parameter's default is None: the
    template does not choose it for the user."""
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9]*$")
    type: Literal["number", "contract"] = "number"
    label: str = Field(min_length=1, max_length=40)
    default: float | str | None
    optional: bool = False
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    targets: list[ParamTarget | str] = Field(min_length=1)
    help: ParamHelp

    @model_validator(mode="after")
    def consistent(self):
        self.targets = [ParamTarget(path=item) if isinstance(item, str) else item for item in self.targets]
        if self.optional and (self.type != "number" or self.default is not None):
            raise ValueError(f"{self.key} 是可不填的数字参数，默认须为 null（不设）")
        if self.default is None and not self.optional:
            raise ValueError(f"{self.key} 须有默认值")
        if self.type == "contract":
            if not CONTRACT.fullmatch(str(self.default)):
                raise ValueError(f"{self.key} 的默认值须是具体合约，例如 SHFE.rb2701")
            return self
        if self.min is None or self.max is None or not self.step or self.step <= 0:
            raise ValueError(f"{self.key} 须给出范围与步长")
        if self.default is not None and not self.min <= float(self.default) <= self.max:
            raise ValueError(f"{self.key} 的默认值不在范围内")
        return self

    @property
    def integer(self) -> bool:
        return float(self.step or 1).is_integer() and float(self.min or 0).is_integer()

    @property
    def decimals(self) -> int:
        text = f"{self.step:.10f}".rstrip("0")
        return len(text.split(".")[1]) if "." in text else 0


class TemplateConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    less: tuple[str, str]
    message: str = Field(min_length=1)


class TemplateSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")


class StrategyTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=40)
    category: Category
    scenario: str = Field(min_length=1, max_length=80)
    plainSummary: str = Field(min_length=1, max_length=120)
    level: Literal["新手", "进阶"]
    holding: Literal["日内", "会隔夜"]
    source: TemplateSource
    recommendedTimeframe: Literal[60, 180, 300, 900, 3600, 86400]
    approximations: list[str] = Field(default_factory=list)
    params: list[TemplateParam] = Field(min_length=1)
    constraints: list[TemplateConstraint] = Field(default_factory=list)
    # 试几组参数比一比 (策略页改版第八批): the one parameter the result page varies; omitted = the
    # first number parameter that is not a time / size / loss setting (``key_param``).
    keyParam: str | None = None
    plan: dict[str, Any]

    def param(self, key: str) -> TemplateParam | None:
        return next((item for item in self.params if item.key == key), None)

    def defaults(self) -> dict[str, float | str | None]:
        return {item.key: (str(item.default) if item.type == "contract" else _clean(item, item.default))
                for item in self.params}


def _clean(param: TemplateParam, value: float | str | None) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), param.decimals)
    return int(rounded) if param.integer else rounded


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def resolve_params(template: StrategyTemplate, params: dict[str, Any] | None) -> dict[str, float | str | None]:
    """The full parameter set: defaults overridden by `params`, each checked against its range and
    step (a contract parameter against its code shape), then the cross-parameter constraints. An
    optional parameter given null / "" is None (不设). Raises TemplateError with the Chinese reason."""
    values: dict[str, float | str | None] = dict(template.defaults())
    for key, raw in (params or {}).items():
        param = template.param(key)
        if param is None:
            raise TemplateError(f"模板“{template.name}”没有参数 {key}")
        if param.type == "contract":
            code = str(raw).strip() if isinstance(raw, str) else ""
            if not CONTRACT.fullmatch(code):
                raise TemplateError(f"{param.label}须是具体合约代码，例如 SHFE.rb2701")
            values[key] = code
            continue
        if param.optional and (raw is None or (isinstance(raw, str) and not raw.strip())):
            values[key] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(float(raw)):
            raise TemplateError(f"{param.label}须是数字")
        assert param.min is not None and param.max is not None and param.step is not None
        value = float(raw)
        if not param.min <= value <= param.max:
            raise TemplateError(f"{param.label}须在 {_number(param.min)}–{_number(param.max)}{param.unit} 之间")
        steps = (value - param.min) / param.step
        if abs(steps - round(steps)) > 1e-6:
            raise TemplateError(f"{param.label}按 {_number(param.step)} 递增")
        values[key] = _clean(param, value)
    for constraint in template.constraints:
        smaller, larger = constraint.less
        low, high = values[smaller], values[larger]
        if low is None or high is None:
            continue
        if not float(low) < float(high):   # constraints only link number params
            raise TemplateError(constraint.message)
    return values


def _word(param: TemplateParam, value: float | str | None) -> str:
    if value is None:
        return "不设"
    return str(value) if param.type == "contract" else _number(float(value))


def _set_path(root: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node: Any = root
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    if isinstance(node, list):
        node[int(parts[-1])] = value
    else:
        node[parts[-1]] = value


def _get_path(root: dict[str, Any], path: str) -> Any:
    node: Any = root
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _fill(value: Any, words: dict[str, str]) -> Any:
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda match: words[match.group(1)], value)
    if isinstance(value, list):
        return [_fill(item, words) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, words) for key, item in value.items()}
    return value


def rebind_timeframe(raw: dict[str, Any], timeframe: int) -> dict[str, Any]:
    """Move the execution period: every condition side on the template's execution clock goes to
    `timeframe` (slower filter periods stay). check_executable then refuses a period slower than
    a filter's."""
    old = raw["timeframeSeconds"]
    if timeframe == old:
        return raw
    for group in (raw.get("rules") or {}).values():
        for condition in ((group or {}).get("conditions", []) + (group or {}).get("alignmentConditions", [])
                          + (group or {}).get("rearm", [])
                          + [c for branch in (group or {}).get("branches", []) for c in branch["conditions"]]
                          + ([{"left": group["trigger"]["price"], "right": {}}] if (group or {}).get("trigger") else [])):
            for side in ("left", "right"):
                if condition[side].get("timeframe") == old:
                    condition[side]["timeframe"] = timeframe
    raw["timeframeSeconds"] = timeframe
    return raw


def build_plan(template: StrategyTemplate, params: dict[str, Any] | None = None, *,
               timeframe: int | None = None, name: str | None = None) -> tuple[StrategyPlan, dict[str, float | str | None]]:
    """(the ordinary StrategyPlan, the resolved parameters). Raises TemplateError."""
    values = resolve_params(template, params)
    raw = copy.deepcopy(template.plan)
    for param in template.params:
        for target in param.targets:
            assert isinstance(target, ParamTarget)
            value = values[param.key]
            if param.type == "contract" or value is None:
                _set_path(raw, target.path, value)
                continue
            result = float(value) * target.scale + target.add
            _set_path(raw, target.path, _clean(param, result) if param.integer else round(result, 10))
    raw = _fill(raw, {item.key: _word(item, values[item.key]) for item in template.params})
    if name:
        raw["name"] = name
    if timeframe is not None:
        raw = rebind_timeframe(raw, timeframe)
    try:
        plan = StrategyPlan.model_validate(raw)
        plan.check_executable()
    except ValidationError as error:
        first = error.errors()[0]
        raise TemplateError(f"参数组合无效：{first.get('msg', '')}".replace("Value error, ", "")) from error
    except ValueError as error:
        raise TemplateError(str(error)) from error
    return plan, values


def _check(template: StrategyTemplate, source: Path) -> None:
    if template.id != source.stem:
        raise ValueError(f"id {template.id} 与文件名不一致")
    keys = [param.key for param in template.params]
    if len(keys) != len(set(keys)):
        raise ValueError("参数名重复")
    for constraint in template.constraints:
        if not set(constraint.less) <= set(keys):
            raise ValueError(f"约束引用了不存在的参数 {constraint.less}")
    for param in template.params:
        for target in param.targets:
            assert isinstance(target, ParamTarget)
            try:
                _get_path(template.plan, target.path)
            except (KeyError, IndexError, ValueError, TypeError) as error:
                raise ValueError(f"参数 {param.key} 的目标 {target.path} 在模板里不存在") from error
    unknown = {word for word in PLACEHOLDER.findall(json.dumps(template.plan, ensure_ascii=False)) if word not in keys}
    if unknown:
        raise ValueError(f"文字里有未知参数 {sorted(unknown)}")
    if JARGON.search(template.plainSummary.replace("K线", "")):
        raise ValueError("plainSummary 须是不含术语的大白话（不写指标名、字母参数）")
    if template.keyParam is not None and ((template.param(template.keyParam) or None) is None
                                          or template.param(template.keyParam).type != "number"):  # type: ignore[union-attr]
        raise ValueError(f"keyParam {template.keyParam} 须是模板里的数字参数")
    plan, _ = build_plan(template)
    if plan.timeframeSeconds != template.recommendedTimeframe:
        raise ValueError("推荐执行周期须与模板的执行周期一致")
    if (template.holding == "日内") != (plan.closeBeforeEndMinutes is not None):
        raise ValueError("holding 须与模板一致：收盘前平仓的是日内，否则会隔夜")


@cache
def templates() -> dict[str, StrategyTemplate]:
    """Every template, in file order; any invalid template raises (RuntimeError) — never skipped."""
    library: dict[str, StrategyTemplate] = {}
    for source in sorted(TEMPLATE_DIR.glob("*.json")):
        try:
            template = StrategyTemplate.model_validate_json(source.read_text(encoding="utf-8"))
            _check(template, source)
        except (ValidationError, ValueError, KeyError) as error:
            raise RuntimeError(f"策略模板 {source.name} 无效：{error}") from error
        library[template.id] = template
    if not library:
        raise RuntimeError("策略模板库为空")
    categories = list(CATEGORIES)
    ordered = sorted(library.values(), key=lambda item: (
        categories.index(item.category), ORDER.index(item.id) if item.id in ORDER else len(ORDER)))
    return {item.id: item for item in ordered}


# Settings that are not the strategy's signal: never the parameter 比一比 varies by default.
NOT_KEY_PARAMS = frozenset({"closeBefore", "maxPosition", "maxLoss"})


def key_param(template: StrategyTemplate) -> TemplateParam | None:
    """The parameter 试几组参数比一比 varies: ``keyParam`` when given, else the first number
    parameter that is not a time / size / loss setting; None for a template without one."""
    if template.keyParam is not None:
        return template.param(template.keyParam)
    return next((param for param in template.params if param.type == "number" and param.key not in NOT_KEY_PARAMS), None)


def summary(template: StrategyTemplate) -> dict[str, Any]:
    """The list entry: what a gallery card and the AI need to pick one."""
    return {"id": template.id, "name": template.name, "category": template.category,
            "categoryLabel": CATEGORIES[template.category], "scenario": template.scenario,
            "plainSummary": template.plainSummary, "level": template.level, "holding": template.holding,
            "source": template.source.model_dump(), "recommendedTimeframe": template.recommendedTimeframe,
            "approximations": template.approximations,
            "params": [{"key": p.key, "type": p.type, "label": p.label,
                        "default": (str(p.default) if p.type == "contract" else _clean(p, p.default)), "optional": p.optional,
                        "min": p.min, "max": p.max, "step": p.step, "unit": p.unit,
                        "integer": p.integer, "help": p.help.model_dump(),
                        "targets": [t.path for t in p.targets if isinstance(t, ParamTarget)]} for p in template.params],
            "constraints": [item.model_dump() for item in template.constraints],
            "keyParam": (key.key if (key := key_param(template)) is not None else None)}


def detail(template: StrategyTemplate) -> dict[str, Any]:
    plan, values = build_plan(template)
    return {**summary(template), "defaults": values, "plan": plan.model_dump()}


def template_source(template: StrategyTemplate, values: dict[str, float | str | None]) -> dict[str, Any]:
    """What a strategy created from a template records about its origin (definition.templateSource)."""
    return {"templateId": template.id, "templateName": template.name, "params": values,
            "defaults": values == template.defaults()}
