"""Strategy request shapes; business methods retain execution and risk checks."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .instruments import CONTEXT_SYMBOL_PATTERN
from .strategy_rules import StrategyPlan
from .strategy_scope import normalize_scope

ChartTimeframe = Literal[60, 180, 300, 900, 3600, 86400]
# 规则回测本机优先 (2026-10-12): why a backtest the browser could have computed ran on the server
# (browser_backtests.FALLBACK_REASONS); diagnostic only, never shown on the result.
FallbackReason = Literal["unsupported", "device", "warming", "init_failed", "init_timeout", "memory", "crashed",
                         "engine_error", "hidden", "slow", "inputs_failed", "inputs_expired", "rerun"]


class InstrumentScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["any", "selected"] = "any"
    symbols: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def normalized(self):
        self.symbols = normalize_scope(self.model_dump())["symbols"]
        return self


class SavePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: StrategyPlan
    instrumentScope: InstrumentScope = Field(default_factory=InstrumentScope)
    # 策略编辑卡 (2026-10-08): where the edited plan came from. Both optional; an older client sends neither.
    baseStrategyId: str | None = Field(default=None, min_length=1, max_length=100,
        description="修改的是哪条策略（策略库版本的 strategyId；运行实例的 id 取它的策略库版本）：存为它的新版本，它定义里卡片以外的字段（模板来源等）原样带上")
    templateId: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$", max_length=60,
        description="从哪个模板开始（新建时）：记为模板来源，和模板默认不同时标为已调整")

    @model_validator(mode="after")
    def one_origin(self):
        if self.baseStrategyId is not None and self.templateId is not None:
            raise ValueError("baseStrategyId 与 templateId 只能给一个")
        return self


class DesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("description")
    @classmethod
    def description_length(cls, value):
        if not 1 <= len(value.strip()) <= 20000:
            raise ValueError("请填写策略描述，最多 20000 字")
        return value


class DesignJobRequest(DesignRequest):
    id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{32,36}$")


class ChartPeriodRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=40)
    timeframeSeconds: ChartTimeframe


class ValidatePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, max_length=40)
    chartTimeframeSeconds: ChartTimeframe | None = None


class HealthAdvice(BaseModel):
    """One 策略体检 suggestion from a rule backtest result (`advice[]`), sent back as it came. The change
    is decided by kind, groups and index; `text` becomes the new version's change note; extra fields
    (label, passes) are ignored."""
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=40)
    kind: Literal["drop", "relaxCross"]
    groups: list[Literal["entryLong", "entryShort"]] = Field(min_length=1, max_length=2)
    index: int | None = Field(default=None, ge=0, le=40)
    withinBars: int | None = Field(default=None, ge=1, le=20)
    text: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def drop_names_a_condition(self):
        if self.kind == "drop" and self.index is None:
            raise ValueError("去掉条件的建议须写明序号")
        return self


class ApplyAdviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    advice: HealthAdvice


# 合约或品种代码；外盘图表的快期外盘主连（KQD.m@COMEX.GC）也收，路由换成美元子账户的主力月份（外盘三期）。
SYMBOL_PATTERN = CONTEXT_SYMBOL_PATTERN


class BacktestOptions(BaseModel):
    """规则回测的参数；都可省略：合约默认策略自己的合约，区间默认按周期取可取到的最近 N 根。"""
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40,
                               description="回测合约；省略时用策略绑定的合约，策略库版本须指定")
    timeframe: ChartTimeframe | None = Field(default=None, description="跟随图表周期的策略用哪个周期回测")
    tradingDays: int | None = Field(default=None, ge=1, le=2000,
                                    description="只回测最近 N 个交易日；超过数据上限时用全部可取到的历史")
    slippageTicks: int = Field(default=0, ge=0, le=5, description="开平各算几跳滑点，默认 0")
    initialBalance: float | None = Field(default=None, gt=0, le=1e12,
                                         description="初始资金；省略时取账户权益，读不到用 100 万")
    sessionId: str | None = Field(default=None, max_length=100, description="把结果卡发到这个对话")
    requestId: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
                                  description="调用方自取的请求号；排队时用 GET /api/strategy-backtests/queue?requestId= 查看位置")
    fallbackReason: FallbackReason | None = Field(
        default=None, description="界面本机优先时为什么改在服务端算（排障用，随结果存下）；AI 与 CLI 不用传")


TemplateParams = dict[str, float | str | None]  # 合约参数（价差模板的两条腿）是合约代码；可不填的参数（当日亏损上限）给 null 即不设


class TemplatePreviewRequest(BacktestOptions):
    """模板预览：按参数实例化出的策略定义，外加一次规则回测（口径同 POST /api/strategy-backtests）。"""
    symbol: str = Field(pattern=SYMBOL_PATTERN, max_length=40, description="回测合约，例如 SHFE.rb2610")
    params: TemplateParams = Field(default_factory=dict, description="要改的参数（键见模板的 params）；省略的取默认值")
    timeframe: ChartTimeframe | None = Field(default=None, description="执行周期；省略用模板推荐周期")


class TemplateInstantiateRequest(BaseModel):
    """从模板创建一条策略库草稿（与自己设计的策略完全等价）。"""
    model_config = ConfigDict(extra="forbid")
    params: TemplateParams = Field(default_factory=dict, description="要改的参数；省略的取默认值")
    symbol: str | None = Field(default=None, max_length=40,
                               description="只用于这个品种或合约（适用范围），例如 SHFE.rb、CME.GCE；外盘图表的快期外盘主连"
                                           "（KQD.m@COMEX.GC）按该外盘品种的全部月份；省略为全部品种")
    name: str | None = Field(default=None, min_length=1, max_length=80,
                             description="策略名称；省略为“模板名 · 合约中文名 · 周期”。与已有策略重名时追加“(2)”等序号")
    chartSymbol: str | None = Field(default=None, pattern=SYMBOL_PATTERN, max_length=40,
                                    description="默认名里用的合约（通常是当前图表合约）；只用于命名，不限制适用范围")
    timeframe: ChartTimeframe | None = Field(default=None, description="执行周期；省略用模板推荐周期")
    instrumentScope: InstrumentScope | None = Field(default=None, description="完整适用范围（给了就不看 symbol），恢复模板默认时沿用原策略的范围")
    baseStrategyId: str | None = Field(default=None, max_length=100,
                                       description="恢复模板默认：存成这条策略（同名）的新版本，不另起名字")


class PlanBacktestRequest(BacktestOptions):
    """不保存也能回测：带完整策略定义（给 AI 比较改前 / 改后，策略编辑卡的实时回测）。"""
    plan: StrategyPlan
    strategyId: str | None = Field(default=None, max_length=100, description="对比的来源策略（只作标识）")
    preview: bool = Field(default=False,
        description="策略编辑卡的实时回测：只算不存（不进最近回测、不写 AI 怎么看），结果多带一句 planSummary")


class BrowserInputsRequest(BacktestOptions):
    """本机回测要的输入：给 plan 按这份定义（同 POST /api/strategy-backtests），不给 plan 给 strategyId 按这条
    已存策略（同 POST /api/strategies/{id}/backtest）。"""
    plan: StrategyPlan | None = None
    strategyId: str | None = Field(default=None, max_length=100, description="已存策略；给了 plan 时只作标识")
    preview: bool = Field(default=False, description="策略编辑卡的实时回测：只算不存")

    @model_validator(mode="after")
    def plan_or_strategy(self):
        if self.plan is None and not self.strategyId:
            raise ValueError("给 plan 或 strategyId")
        return self


class BrowserResultRequest(BaseModel):
    """浏览器按 inputs 算完的回测报告；服务端按自己记下的那次输入包装、存档。"""
    model_config = ConfigDict(extra="forbid")
    inputDigest: str = Field(min_length=1, max_length=64, description="POST /api/strategy-backtests/inputs 给的 inputDigest")
    ticketId: str = Field(min_length=1, max_length=64, description="POST /api/strategy-backtests/inputs 给的 ticketId（这次输入的票据）")
    engineDigest: str = Field(min_length=1, max_length=64, description="算这次结果的引擎摘要")
    result: dict[str, Any] = Field(description="run_rule 的报告（summary、coverage、trades 等）")
    runMs: int | None = Field(default=None, ge=0, description="浏览器里计算用了多少毫秒")

    @field_validator("result")
    @classmethod
    def report_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("summary"), dict) or not isinstance(value.get("coverage"), dict) \
                or not isinstance(value.get("trades"), list) or len(value["trades"]) > 2000:
            raise ValueError("回测报告不完整")
        return value


class LabVariant(BaseModel):
    """策略实验室的一组变体：要么 templateId（+params，+timeframe），要么完整 plan。"""
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=60,
                              description="这组的名字；省略为“模板名 · 参数摘要”或策略名")
    templateId: str | None = Field(default=None, max_length=60, description="来源模板")
    params: TemplateParams = Field(default_factory=dict, description="模板参数（键见模板的 params）")
    timeframe: ChartTimeframe | None = Field(default=None, description="执行周期；省略用模板推荐周期")
    plan: StrategyPlan | None = Field(default=None, description="完整策略定义（不用模板时给这个）")

    @model_validator(mode="after")
    def one_source(self):
        if (self.templateId is None) == (self.plan is None):
            raise ValueError("每组变体要么给 templateId，要么给 plan")
        return self


class CompareRequest(BaseModel):
    """试几组参数比一比：在哪个合约上比（跟随图表周期的策略再给周期）。"""
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=SYMBOL_PATTERN, max_length=40, description="比较的合约，例如 SHFE.rb2701")
    timeframe: ChartTimeframe | None = Field(default=None, description="跟随图表周期的策略用哪个周期")
    runOn: Literal["server", "browser"] = Field(
        default="server", description="server 立即在服务端排队算（默认，AI 与 CLI 用）；browser 只建好比较，由界面在本机算"
        "（GET /api/strategy-labs/{labId}/inputs → POST …/results），本机算不了再 POST …/run-server")


class LabRunServerRequest(BaseModel):
    """云端兜底：界面本机算不了时带上原因（排障用）。"""
    model_config = ConfigDict(extra="forbid")
    fallbackReason: FallbackReason | None = None


class LabCreateRequest(BaseModel):
    """创建一次策略实验室对比：一个合约上最多 12 组变体。"""
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=SYMBOL_PATTERN, max_length=40, description="对比的合约，例如 SHFE.rb2610")
    variants: list[LabVariant] = Field(min_length=1, max_length=12)
    tradingDays: int | None = Field(default=None, ge=1, le=2000, description="只回测最近 N 个交易日")
    slippageTicks: int = Field(default=1, ge=0, le=5, description="开平各算几跳滑点，默认 1 跳")
    skipInvalid: bool = Field(default=False, description="跳过建不起来的组（模板页的网格用）；默认整次拒绝")


class LabSegment(BaseModel):
    """样本内 / 样本外一侧的统计（浏览器与服务端各自算出，服务端按收到的数字重排名）。"""
    model_config = ConfigDict(extra="ignore")
    trades: int = Field(ge=0)
    wins: int = Field(ge=0)
    winRate: float | None = None
    net: float
    avgNet: float | None = None
    maxDrawdown: float = Field(default=0, ge=0)


class LabVariantResult(BaseModel):
    """一组变体的结果：要么带 error（这组没算成），要么带完整的样本内 / 样本外统计。"""
    model_config = ConfigDict(extra="ignore")
    variantId: str = Field(min_length=1, max_length=40)
    label: str = Field(default="", max_length=120)
    error: str | None = Field(default=None, max_length=500)
    summary: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    inSample: LabSegment | None = None
    outSample: LabSegment | None = None
    splitTime: int | None = None
    # 回测结果页 (2026-10-06): the equity curve, thinned by the engine (absent on older engines).
    equityCurve: list[dict[str, Any]] | None = Field(default=None, max_length=80)
    tradesUsed: int | None = Field(default=None, ge=0)
    straddling: int | None = Field(default=None, ge=0)
    truncated: bool = False
    feeEstimate: dict[str, Any] | None = None

    @model_validator(mode="after")
    def segments_or_error(self):
        if self.error is None and (self.inSample is None or self.outSample is None):
            raise ValueError("没有 error 的结果必须带样本内与样本外统计")
        return self


class LabResultsRequest(BaseModel):
    """浏览器（Pyodide）算完的各组结果；排名由服务端重算，不信任客户端。"""
    model_config = ConfigDict(extra="forbid")
    engineDigest: str = Field(min_length=1, max_length=64, description="算这批结果的引擎摘要")
    results: list[LabVariantResult] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def variants_unique(self):
        identifiers = [item.variantId for item in self.results]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("结果里有重复的变体")
        return self


class LabChooseRequest(BaseModel):
    """把这组变体存为我的策略：服务端先重跑一遍权威回测再保存。"""
    model_config = ConfigDict(extra="forbid")
    variantId: str = Field(min_length=1, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=80, description="策略名称；省略用这组的名字")
