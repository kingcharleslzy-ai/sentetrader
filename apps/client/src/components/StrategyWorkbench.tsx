import { AccountAccessContext } from "../hooks/useAccountAccess";
import { HelpTip, RuleRow, SectionCard, SideBySide, StatTiles, type StatTile } from "./Blocks";
import { feedLiveNow } from "../runtimeFeed";
import { actionText, monitorDisplay, monitorTime, attemptStatusLabel } from "../strategyMonitorDisplay";
import { UiNavigationContext } from "../uiNavigation";
import { useDisclosureSurface, useUiSurface } from "../uiSurfaces";
import { HelpButton } from "./AppHelp";
import { StrategyQuestions } from "./StrategyQuestions";
import { StrategyScopeEditor, StrategyScopeFields } from "./StrategyScopeEditor";
import { usePersistentDraft } from "../hooks/usePersistentDraft";
import { useWorkspaceScope } from "../hooks/useWorkspaceScope";
import { AiProgressCard, type AiProgress } from "./AiProgressCard";
import { useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { BookmarkPlus, Bot, Play, Plus } from "lucide-react";
import { requestJson, getOrderEstimate, getStrategyMonitor, stopStrategyMonitoring, type StrategyMonitorSnapshot, type AccountOverviewSnapshot, type InstrumentCatalog, type MarketWorkbenchSnapshot } from "../api";
import { marketClosedNow, marketSessionDisplay } from "../marketSession";
import { launchBlockedReason, resumeAfterDrawdown, requestStatus, strategyError, strategyQuestions, strategyText, strategyStatusText, allowsSymbol, scopeLabel, spreadLegsWords, spreadWords, strategyScope, strategySymbol, needsAnswersOf, CONTRACT, type InstrumentScope, defaultFees, designerPlanProblem, designerRows, directions, followChart, loadStrategies, conditionParts, operandLabel, periodLabel, planBlockRows, ruleNames, triggerRises, validateAndStart,
  type FeeRate, type FeeSchedule, type RuleCondition, type RuleGroup, type SavedStrategy, type StrategyPlan } from "../strategyApi";
import { contractLabel, formatFee, formatSigned, isForeignSymbol, moneyUnit, signClass, type StrategyTab } from "./futures/support";
import "./StrategyWorkbench.css";
import { StrategyItemActions } from "./StrategyItemActions";
import type { AskAi } from "./AiMenu";
import { SIGNAL_ACTIONS, SignalActionMenu, StrategyLaunchDialog, type SignalAction } from "./StrategySignalAction";
import { prewarmEngine, useEngineStatus } from "../strategyEngine/browserEngine";
import { applyStrategyAdvice, backtestSelection, loadStoredBacktest, requestVerdict, runStrategyBacktest, selectBacktestStrategy, setBacktestParams, setBacktestTradesOpen, useBacktestCard, type StrategyBacktestResult, type StrategyHealthAdvice } from "../strategyBacktests";
import { backtestMarksOf, setBacktestMarks, useBacktestMarks } from "../aiAnnotations";
import { StrategyBacktestPanel } from "./StrategyBacktestView";
import { instantiateTemplate, loadTemplates, templateById, templateValueText, useTemplateCatalog } from "../strategyTemplates";
import { ParamHelp } from "./ParamHelp";
import { chooseLabVariant, getLab } from "../strategyLab/labApi";
import { loadCompare, startCompare, useCompare } from "../strategyCompare";
import { DrawdownStatus, FundsLines } from "./StrategyRiskBlocks";
import { PlaybookPage } from "./playbooks/PlaybookPage";
import { AskAiInput, ClashLine, CompareBlock, ResultHead, ResultSummary, RuleRows, SignalRows, StrategyCard,
  useInstanceMonitor, type CardState } from "./StrategyHome";
import { ScaleOutFields } from "./ScaleOutFields";
import { StrategyEditorCard } from "./StrategyEditorCard";
import { StrategyStarts, TemplateStartSelect } from "./StrategyStarts";
import { applyEditorChange, askEditor, discardDraft, editDraft, editorSnapshot, editorState, hideDraft, needsSave, newDraft, openDraft, pendingEdit, saveDraft, setEditorContext,
  setEditorError, setStart, useStrategyEditor, type EditorChange } from "../strategyEditorStore";

const backtestMarks = (result: StrategyBacktestResult) => backtestMarksOf(result);

function FeeFields({ value, onChange }: { value: FeeRate; onChange: (value: FeeRate) => void }) {
  return <div className="strategy-fee-fields"><label>计费方式<select value={value.mode} onChange={e => onChange({ ...value, mode: e.target.value as FeeRate["mode"] })}><option value="per_lot">元 / 手</option><option value="notional">成交金额 · 万分之</option></select></label>
    {(["open", "closeToday", "closeYesterday"] as const).map((key, i) => <label key={key}>{["开仓", "平今", "平昨"][i]}<input type="number" min="0" max="100000" step="any" required value={value[key]} onChange={e => onChange({ ...value, [key]: Number(e.target.value) })}/></label>)}</div>;
}

/** 外盘合约的手续费（外盘盘点 3B 2026-10-21）：美元子账户按外盘撮合引擎的每手固定美元手续费扣，开平同价，回测也按它
 *  （Runtime trading_costs.backtest_fee_schedule）；这里的费率设置只用于国内合约。每手的数取自开仓 1 手的下单估算。 */
export function ForeignFeeNote({ url, symbol }: { url: string; symbol: string }) {
  const [fee, setFee] = useState<number | null>();
  useEffect(() => {
    const controller = new AbortController();
    setFee(undefined);
    getOrderEstimate(url, { symbol, side: "buy", offset: "open", volume: 1 }, controller.signal)
      .then(value => setFee(value.commissionPerLot ?? null)).catch(() => { if (!controller.signal.aborted) setFee(null); });
    return () => controller.abort();
  }, [url, symbol]);
  return <p className="fee-foreign" data-testid="fee-foreign">
    <span>外盘 · 每手每边 <strong>{fee === undefined ? "…" : fee === null ? "—" : formatFee(fee)}</strong> 美元</span>
    <HelpTip helpKey="fees:foreign" label="外盘手续费">外盘合约在美元子账户按外盘撮合引擎的每手固定手续费扣，开仓、平仓同价，回测同一口径；下面的费率设置只用于国内合约。</HelpTip>
  </p>;
}

export function FeeEditor({ rootUrl, symbol = "" }: { rootUrl: string; symbol?: string }) {
  const [fees, setFees] = useState<FeeSchedule>(defaultFees);
  const [saved, setSaved] = useState<FeeSchedule>(defaultFees);
  const [loaded, setLoaded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [overridesOpen, setOverridesOpen] = useDisclosureSurface("fee-overrides", { available: editing && fees.source === "custom" });
  useEffect(() => { let cancelled = false; setLoaded(false); setEditing(false); setMessage(""); void requestJson<FeeSchedule>(`${rootUrl}/api/trading-costs`).then(value => { if (!cancelled) { setFees(value); setSaved(value); setLoaded(true); } }).catch(() => !cancelled && setMessage("手续费配置读取失败，请重新打开")); return () => { cancelled = true; }; }, [rootUrl]);
  const key = symbol.trim().toLowerCase();
  // The schedule is the domestic contracts' only: a foreign contract never matches an override (its fee is the engine's).
  const foreign = isForeignSymbol(symbol);
  const override = foreign ? undefined : saved.overrides.find(rate => rate.symbol.toLowerCase() === key) || saved.overrides.find(rate => rate.symbol.toLowerCase() === key.replace(/\d+$/, ""));
  const rate = override || saved.default;
  const unit = saved.source === "account" ? "交易所标准（默认）" : rate.mode === "per_lot" ? "元 / 手" : "万分之";
  return <section className="strategy-fees" aria-label="手续费">
    {foreign ? <ForeignFeeNote url={rootUrl} symbol={symbol} /> : null}
    <button type="button" className="fee-summary" disabled={!loaded || busy} aria-expanded={editing} onClick={() => { setFees(saved); setEditing(!editing); setMessage(""); }}>
      <span className="fee-summary-heading"><strong>手续费 <small>{loaded ? unit : "读取中…"}</small></strong><span>{editing ? "收起" : "编辑"}</span></span>
      <span className="fee-summary-values">{(["open", "closeToday", "closeYesterday"] as const).map((field, i) => <span key={field}><small>{["开仓", "平今", "平昨"][i]}</small><strong>{!loaded ? "—" : saved.source === "account" ? "默认" : rate[field].toLocaleString("zh-CN", {maximumFractionDigits: 8})}</strong></span>)}</span>
      {override && saved.source === "custom" && <small className="fee-applied-scope">{override.symbol} 专用费率</small>}
    </button>
    {editing && <form className="strategy-editor fee-editor" onSubmit={async e => { e.preventDefault(); setBusy(true); setMessage(""); try { const value = await requestJson<FeeSchedule>(`${rootUrl}/api/trading-costs`, { method: "POST", body: JSON.stringify(fees) }); setFees(value); setSaved(value); setEditing(false); setMessage("已保存，新回放生效。"); } catch (error) { setMessage(strategyError(error, "保存失败，请检查费率和重复品种")); } finally { setBusy(false); } }}>
      <label>费用来源<select disabled={busy} value={fees.source} onChange={e => setFees({ ...fees, source: e.target.value as FeeSchedule["source"] })}><option value="account">交易所标准（默认）</option><option value="custom">自定义费率</option></select></label>
      {fees.source === "custom" && <><FeeFields value={fees.default} onChange={value => setFees({ ...fees, default: value })}/>
        <details className="fee-overrides" open={overridesOpen} onToggle={e => setOverridesOpen(e.currentTarget.open)}><summary>品种 / 合约专用费率{fees.overrides.length ? `（${fees.overrides.length}）` : ""}</summary>
          {fees.overrides.map((value, index) => <fieldset key={index}><label>品种或合约<input required placeholder="SHFE.au 或 SHFE.au2612" value={value.symbol} onChange={e => setFees({ ...fees, overrides: fees.overrides.map((v, i) => i === index ? { ...v, symbol: e.target.value } : v) })}/></label><FeeFields value={value} onChange={rate => setFees({ ...fees, overrides: fees.overrides.map((v, i) => i === index ? { ...rate, symbol: v.symbol } : v) })}/><button type="button" className="app-button danger" onClick={() => setFees({ ...fees, overrides: fees.overrides.filter((_, i) => i !== index) })}>删除专用费率</button></fieldset>)}
          <button type="button" onClick={() => setFees({ ...fees, overrides: [...fees.overrides, { ...fees.default, symbol: "" }] })}>添加品种 / 合约</button>
        </details></>}
      <div className="fee-editor-actions"><HelpTip helpKey="fees:scope" label="费率">与模拟盘测算共用；账户扣费以回报为准，正在运行的回放保持原费率。</HelpTip><button type="button" disabled={busy} onClick={() => { setFees(saved); setEditing(false); setMessage(""); }}>取消</button><button disabled={busy} className="strategy-primary">{busy ? "保存中…" : "保存费率"}</button></div>
    </form>}
    {message && <p className="fee-message" role="status">{message}</p>}
  </section>;
}

/** The fee schedule is edited in settings → 手续费; panels that cannot reach the drawer ask for it by event. */
export function openFeeSettings() {
  window.dispatchEvent(new CustomEvent("ai-trader-open-settings", { detail: "fees" }));
}

function protectionLabel(plan: StrategyPlan, unit: string) {
  if (plan.spread) {
    // 价差: stop / take are spread points judged on the close spread; the tick-based ones do not apply.
    const stop = plan.spread.stopLoss == null ? "无价差止损" : `价差止损 ${plan.spread.stopLoss} 点`;
    const take = plan.spread.takeProfit == null ? "无价差止盈" : `价差止盈 ${plan.spread.takeProfit} 点`;
    return `${stop} · ${take}（实盘每秒判断，回测按收盘）`;
  }
  const stop = plan.pullback ? "止损固定在回调低点 / 高点外一跳" : plan.stopLookbackBars != null
    ? `止损固定在入场前最近 ${plan.stopLookbackBars} 根已收盘 K 线（含信号K线）最低 / 最高价外一跳`
    : plan.atrStop ? `止损 ${plan.atrStop.multiple}×ATR${plan.atrStop.period ?? 14}（信号K线的 ATR，成交后换算成固定价）`
    : `止损 ${plan.stopLossTicks} 跳`;
  const take = plan.takeProfitTicks == null ? "无固定止盈" : `止盈 ${plan.takeProfitTicks} 跳`;
  return `${stop} · ${take}${plan.perTradeRisk != null ? ` · 单笔预算 ${plan.perTradeRisk} ${unit}，预算不足一手时跳过` : ""}`;
}

type ConditionReading = { label: string; status: string; detail: string; group: keyof typeof ruleNames };

// 过滤 conditions: a slower period's trend, or a gate on volatility / volume / the opening gap.
const FILTER_INDICATORS = new Set(["gap_percent", "atr", "volume", "volume_ratio"]);
/** An entry group's conditions split into 信号 (when to open) and 过滤 (when not to); a group made only
 *  of gates shows them as its signal. */
function entryParts(plan: StrategyPlan, key: keyof typeof ruleNames): { signal: RuleCondition[]; filter: RuleCondition[] } {
  const conditions = plan.rules[key]?.conditions || [];
  const isFilter = (condition: RuleCondition) => FILTER_INDICATORS.has(condition.left.indicator)
    || Boolean(plan.timeframeSeconds && (condition.left.timeframe ?? 0) > plan.timeframeSeconds);
  const filter = conditions.filter(isFilter);
  return filter.length === conditions.length ? { signal: conditions, filter: [] } : { signal: conditions.filter(c => !isFilter(c)), filter };
}

/**
 * 填表单 (说人话, 2026-09-25): the generated plan's 常用 blocks in the open and its 高级 blocks in the
 * “高级” fold (`disclosure: strategy-advanced`; the old name `strategy-full-rules` still works). A plan
 * that uses any 高级 block opens the fold by itself, so no value it sets is hidden.
 * 第六批 (2026-09-28): 加仓 and 分批止盈 are the fold's two editable blocks — every other 高级 block
 * stays a read-only row (edit those by asking the AI for a new version). The same checks the server
 * runs refuse an invalid edit before the save request (`designerPlanProblem`).
 */
type Pyramid = NonNullable<StrategyPlan["pyramiding"]>;
const DEFAULT_PYRAMID: Pyramid = { maxAdds: 3, trigger: "profit_atr", step: 0.5, atrPeriod: 20, volume: null, moveStop: true };

function PyramidFields({ plan, disabled, onChange }: { plan: StrategyPlan; disabled: boolean; onChange: (next: StrategyPlan) => void }) {
  const pyramid = plan.pyramiding;
  const patch = (values: Partial<Pyramid>) => onChange({ ...plan, pyramiding: { ...(pyramid ?? DEFAULT_PYRAMID), ...values } });
  return <fieldset className="strategy-block-edit" aria-label="加仓">
    <legend>加仓</legend>
    <label className="strategy-block-toggle"><input type="checkbox" checked={Boolean(pyramid)} disabled={disabled}
      onChange={event => onChange({ ...plan, pyramiding: event.target.checked ? DEFAULT_PYRAMID : null })}/>浮盈时顺势加仓</label>
    {pyramid && <>
      <label>触发方式<select value={pyramid.trigger} disabled={disabled}
        onChange={event => patch({ trigger: event.target.value as Pyramid["trigger"] })}>
        <option value="profit_atr">每盈利 ATR 倍数</option>
        <option value="profit_ticks">每盈利跳数</option>
        <option value="signal">同向开仓信号再成立</option>
      </select></label>
      {pyramid.trigger !== "signal" && <label>{pyramid.trigger === "profit_ticks" ? "步长 · 跳" : "步长 · ATR 倍数"}
        <input type="number" min="0.1" step="0.1" value={pyramid.step} disabled={disabled} onChange={event => patch({ step: Number(event.target.value) })}/></label>}
      {pyramid.trigger === "profit_atr" && <label>ATR 周期
        <input type="number" min="2" max="200" value={pyramid.atrPeriod} disabled={disabled} onChange={event => patch({ atrPeriod: Number(event.target.value) })}/></label>}
      <label>最多加仓次数<input type="number" min="1" max="5" value={pyramid.maxAdds} disabled={disabled} onChange={event => patch({ maxAdds: Number(event.target.value) })}/></label>
      <label>每次加仓手数<input type="number" min="1" max="20" value={pyramid.volume ?? ""} placeholder="留空跟首笔" disabled={disabled}
        onChange={event => patch({ volume: event.target.value === "" ? null : Number(event.target.value) })}/></label>
      <label className="strategy-block-toggle"><input type="checkbox" checked={pyramid.moveStop} disabled={disabled}
        onChange={event => patch({ moveStop: event.target.checked })}/>止损随最近一次加仓收紧</label>
      <small>首笔开 {Math.max(1, plan.maxPosition - (pyramid.volume || 1) * pyramid.maxAdds)} 手给加仓留空间；只顺势加，亏了不摊平。</small>
    </>}
  </fieldset>;
}

function DesignerRules({ plan, unit, active, busy, onChange }: { plan: StrategyPlan; unit: string; active: boolean; busy: boolean; onChange: (next: StrategyPlan) => void }) {
  const { common, advanced } = designerRows(plan);
  const used = advanced.length > 0;
  const [open, setOpen] = useDisclosureSurface("strategy-advanced", { initial: used, available: active });
  useEffect(() => { if (used) setOpen(true); }, [used, setOpen]);
  // The two editable blocks replace their own read-only rows; the fold still counts them as 已用.
  const shown = advanced.filter(([label]) => label !== "加仓" && label !== "分批止盈");
  const problem = designerPlanProblem(plan);
  const rows = (items: [string, string][]) => <dl className="strategy-kv">{items.map(([label, value], index) => <div key={`${label}-${index}`}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
  return <div className="strategy-designer-rules">
    <section className="strategy-block" aria-label="常用"><h4>常用</h4>
      {rows([...common, ["止损 / 止盈", protectionLabel(plan, unit)], ["最多持仓", plan.spread ? `${plan.maxPosition} 份价差` : `${plan.maxPosition} 手`],
        ["当日亏损上限", plan.maxLoss != null ? `${plan.maxLoss} ${unit}` : "不设"], ["平仓后等待", `${plan.cooldownBars} 根`]])}</section>
    <details className="strategy-details strategy-advanced" open={open} onToggle={event => { const next = event.currentTarget.open; if (next !== open) setOpen(next); }}>
      <summary>高级{used ? ` · 已用 ${advanced.length} 项` : ""}</summary>
      <div className="strategy-advanced-body">
        <PyramidFields plan={plan} disabled={busy} onChange={onChange}/>
        <ScaleOutFields tiers={plan.scaleOut ?? []} disabled={busy} onChange={tiers => onChange({ ...plan, scaleOut: tiers.length ? tiers : undefined })}/>
        {shown.length > 0 && rows(shown)}
        {!used && <p>其余高级积木（复位、连续 N 根、升降、持仓量、枢轴、分段止盈、保本、开仓时段、按 ATR 定手数等）没有用到。</p>}
        {problem && <p role="alert">{problem}</p>}
      </div>
    </details>
  </div>;
}

function PullbackRule({ plan }: { plan: StrategyPlan }) {
  const pullback = plan.pullback!;
  return <p>MA{pullback.trendPeriod} 同向倾斜，价格在趋势侧；首次触及 MA{pullback.touchPeriod} 后记录高低点，收盘守住 MA{pullback.guardPeriod}，最多等待 {pullback.maxBars} 根。交叉按各自的有效窗口判断，可以早于首次触及；每次回调只触发一次。</p>;
}

const shortPeriod = (seconds: number | undefined) => !seconds ? "跟图" : seconds === 86400 ? "日线"
  : seconds % 3600 === 0 ? `${seconds / 3600}时` : `${seconds / 60}分`;

/** 止损 as a tile: the value, and what it is measured from when that is not obvious. 价差 (2026-09-29):
 *  spread points on the close spread — the tick-based stop / take of a single contract do not apply. */
function stopTile(plan: StrategyPlan): StatTile {
  if (plan.spread) return { label: "价差止损", value: plan.spread.stopLoss == null ? "不设" : `${plan.spread.stopLoss} 点`, note: "实盘每秒判断，回测按收盘" };
  if (plan.pullback) return { label: "止损", value: "回调低点 / 高点外一跳" };
  if (plan.stopLookbackBars != null) return { label: "止损", value: `入场前 ${plan.stopLookbackBars} 根低点 / 高点外一跳`, note: "含信号K线" };
  if (plan.atrStop) return { label: "止损", value: `${plan.atrStop.multiple}×ATR${plan.atrStop.period ?? 14}`, note: "信号K线的 ATR，成交后换算成固定价" };
  return { label: "止损", value: `${plan.stopLossTicks} 跳` };
}

/** 止盈 as a tile, the same split as `stopTile`. */
function takeTile(plan: StrategyPlan): StatTile {
  if (plan.spread) return { label: "价差止盈", value: plan.spread.takeProfit == null ? "不设" : `${plan.spread.takeProfit} 点`, note: "实盘每秒判断，回测按收盘" };
  return { label: "止盈", value: plan.takeProfitTicks == null ? "不设" : `${plan.takeProfitTicks} 跳` };
}

function Rule({ condition, kind, reading }: { condition: RuleCondition; kind?: "filter" | "flag"; reading?: ConditionReading }) {
  const parts = conditionParts(condition);
  return <RuleRow period={shortPeriod(condition.left.timeframe)} left={parts.left} op={parts.op} right={parts.right} note={parts.note}
    kind={kind} title={strategyText(condition.label)}
    reading={reading && { passed: reading.status === "passed", detail: strategyStatusText(reading.detail, reading.detail || "等待条件更新") }}/>;
}

const SIDES = { entryLong: ["做多", "long"], entryShort: ["做空", "short"], exitLong: ["平多", "long"], exitShort: ["平空", "short"] } as const;
const joinLabel = (group: RuleGroup) => group.sequence ? "按顺序全部满足" : group.join === "all" ? "全部满足" : "任一满足";

/** 条件组合 (第四批): each branch as its own titled run of rule rows, after the group's single conditions. */
function Branches({ group, reading }: { group: RuleGroup; reading: (condition: RuleCondition) => ConditionReading | undefined }) {
  return <>{(group.branches ?? []).map((branch, index) => <div key={`b${index}`} className="strategy-branch">
    <span className="strategy-side-part">{`组合 ${index + 1} · ${branch.join === "all" ? "全部满足" : "任一满足"}`}</span>
    {branch.conditions.map((condition, row) => <Rule key={row} condition={condition} reading={reading(condition)}/>)}
  </div>)}</>;
}

/** One side of 什么时候开仓: 信号, then 过滤 (a slower period or a volatility / volume / gap gate) and 方向确认. */
function EntrySide({ plan, group: key, readings, flag }: { plan: StrategyPlan; group: "entryLong" | "entryShort"; readings?: ConditionReading[];
  /** 策略体检: the index (in `conditions`) of the condition the backtest advises dropping. */
  flag?: number }) {
  const group = plan.rules[key];
  const [name, side] = SIDES[key];
  const own = readings?.filter(row => row.group === key) ?? [];
  const reading = (condition: RuleCondition) => own.find(row => row.label === condition.label);
  if (!group) return <div className="strategy-side" data-side={side}><h4>{name}</h4><p>未设置开仓条件</p></div>;
  const { signal, filter } = entryParts(plan, key);
  const flagged = flag === undefined ? undefined : group.conditions?.[flag];
  const alignment = group.alignmentConditions ?? [];
  const labels = new Set([...alignment, ...(group.conditions ?? []), ...(group.branches ?? []).flatMap(branch => branch.conditions)]
    .map(condition => condition.label));
  return <div className="strategy-side" data-side={side}>
    <h4>{name}<small>{alignment.length ? "方向同向后，入场确认" : ""}{joinLabel(group)}</small></h4>
    {signal.length > 0 && <span className="strategy-side-part">信号</span>}
    {signal.map((condition, index) => <Rule key={index} condition={condition} kind={condition === flagged ? "flag" : undefined} reading={reading(condition)}/>)}
    <Branches group={group} reading={reading}/>
    {group.trigger && <>
      <span className="strategy-side-part">盘中触价 · 收盘成立后挂价，下一根K线内有效</span>
      <RuleRow period={shortPeriod(group.trigger.price.timeframe)} left="价格"
        op={triggerRises(group.trigger, key === "entryLong") ? "涨到" : "跌到"} right={operandLabel(group.trigger.price)}
        note={`就${name === "做多" ? "开多" : "开空"}`}/>
    </>}
    {(filter.length > 0 || alignment.length > 0) && <span className="strategy-side-part">过滤{alignment.length > 0
      ? <HelpTip helpKey={`strategy:alignment-${key}`} label={`${name}方向条件`}>方向条件先后不限，同向后等待入场确认；每段最多开一次，止损后不重复开仓。</HelpTip> : null}</span>}
    {alignment.map((condition, index) => <Rule key={`a${index}`} condition={condition} kind="filter" reading={reading(condition)}/>)}
    {filter.map((condition, index) => <Rule key={`f${index}`} condition={condition} kind={condition === flagged ? "flag" : "filter"} reading={reading(condition)}/>)}
    {own.filter(row => !labels.has(row.label)).map((row, index) => <p key={`r${index}`} className="strategy-side-note">
      {row.status === "passed" ? "✓" : "·"} {strategyText(row.label)} · {strategyStatusText(row.detail, "等待条件更新")}</p>)}
  </div>;
}

function ExitSide({ plan, group: key, readings }: { plan: StrategyPlan; group: "exitLong" | "exitShort"; readings?: ConditionReading[] }) {
  const group = plan.rules[key];
  const [name, side] = SIDES[key];
  return <div className="strategy-side" data-side={side}>
    <h4>{name}{group ? <small>{joinLabel(group)}</small> : null}{group ? null
      : <HelpTip helpKey={`strategy:exit-none-${key}`} label={`${name}规则`}>不按规则平仓，只靠下面的止损、止盈和收盘前平仓。</HelpTip>}</h4>
    {group ? <>{(group.conditions ?? []).map((condition, index) => <Rule key={index} condition={condition}
      reading={readings?.find(row => row.group === key && row.label === condition.label)}/>)}
      <Branches group={group} reading={condition => readings?.find(row => row.group === key && row.label === condition.label)}/></>
      : null}
  </div>;
}

const HEALTH_GROUPS = { entryLong: "开多", entryShort: "开空" } as const;

/** The condition each entry group is advised to drop (策略体检's first drop suggestion per group). */
function droppedConditions(result: StrategyBacktestResult): Partial<Record<"entryLong" | "entryShort", number>> {
  const flags: Partial<Record<"entryLong" | "entryShort", number>> = {};
  for (const item of result.advice ?? []) {
    if (item.kind !== "drop" || item.index === undefined) continue;
    for (const group of item.groups) if (flags[group] === undefined) flags[group] = item.index;
  }
  return flags;
}

/**
 * 策略体检 on 详情 (2026-09-26): the last backtest opened nothing. The headline, per entry group how
 * often each condition held alone and how often the others held without it (the advised one marked),
 * and the structural suggestions — each 按这个改 saves a new version and reruns the backtest. Counted
 * in entries, never chosen by profit.
 */
function HealthCard({ result, applying, onApply }: { result: StrategyBacktestResult; applying: boolean; onApply: (advice: StrategyHealthAdvice) => void }) {
  const flags = droppedConditions(result);
  const advice = result.advice ?? [];
  // 任一满足的组也有体检数（编辑卡用），这张卡只列全部满足的组。
  const groups = (["entryLong", "entryShort"] as const).filter(group => result.diagnosis?.[group] && (result.diagnosis[group] as { join?: string }).join !== "any");
  return <SectionCard tone="warning" title="体检：这条策略几乎不会开仓" label="体检" testId="strategy-health"
    hint={advice.length > 0 ? "改完存为新版本并自动重跑回测；原版本保留。" : undefined} helpKey="strategy:health"
    note={`${result.symbol} · ${periodLabel(result.timeframeSeconds)} · 近 ${result.coverage.tradingDays} 个交易日`}>
    <div className="section-card-body strategy-health" data-agent-target="strategy-health">
      <p>{result.headline}</p>
      {groups.map(group => <table key={group} className="strategy-health-table" aria-label={`${HEALTH_GROUPS[group]}条件体检`}>
        <thead><tr><th scope="col">{HEALTH_GROUPS[group]}条件</th><th scope="col">单独满足</th><th scope="col">去掉它，其余满足</th></tr></thead>
        <tbody>{result.diagnosis![group]!.conditions.map((row, index) => <tr key={index} data-flag={flags[group] === index || undefined}>
          <td>{strategyText(row.label)}</td><td>{row.alone} 次</td><td>{row.without} 次{flags[group] === index ? " ← 卡在这" : ""}</td>
        </tr>)}</tbody>
      </table>)}
      {advice.length > 0 && <ol className="strategy-health-advice" aria-label="体检建议">{advice.map(item => <li key={item.id}>
        <span>{item.text}</span>
        <button type="button" disabled={applying} onClick={() => onApply(item)}>{applying ? "修改中…" : "按这个改"}</button>
      </li>)}</ol>}
    </div>
  </SectionCard>;
}

/**
 * 详情 (版面重做, 2026-09-26; the owner approved the design as the pattern for every module): the
 * strategy as four titled blocks — 什么时候开仓（信号 + 过滤，做多 | 做空并排）/ 什么时候平仓（平多 | 平空 +
 * 止损止盈格子）/ 怎么下单、下多少 — each condition one line (周期 · 指标 · 动作 · 对象). The five
 * sections (信号 / 过滤 / 入场 / 出场与风控 / 仓位) are the same content: 信号 and 过滤 are the parts of
 * the first block, 入场 and 仓位 share the third.
 */
function StrategyBlocks({ plan, unit, readings, chartTimeframe, health, flagsFrom }: { plan: StrategyPlan; unit: string; readings?: ConditionReading[]; chartTimeframe: number;
  /** 策略体检 of this strategy's last backtest: shown above ①, and the advised condition is flagged in ①. */
  health?: { result: StrategyBacktestResult; applying: boolean; onApply: (advice: StrategyHealthAdvice) => void };
  /** 结果页 (2026-10-08): the health card sits above the plain rows; ① only flags the advised condition. */
  flagsFrom?: StrategyBacktestResult }) {
  const entryRows = planBlockRows(plan, "entry");
  const sizing = entryRows.find(([label]) => label === "按 ATR 定手数");
  const gates = entryRows.filter(([label]) => label !== "按 ATR 定手数").map(([label, value]) => ({ label, value }));
  const long = plan.direction !== "short", short = plan.direction !== "long";
  const flags = health ? droppedConditions(health.result) : flagsFrom ? droppedConditions(flagsFrom) : {};
  return <div className="strategy-blocks">
    {health && <HealthCard {...health}/>}
    <SectionCard title="① 什么时候开仓" label="什么时候开仓" note="都按已收盘的 K 线判断">
      {plan.spread && <div className="strategy-block-note"><p>价差 {spreadWords(plan.spread)}：做多 = {spreadLegsWords(plan.spread, "open_long")}，做空 = {spreadLegsWords(plan.spread, "open_short")}；两腿行情按同一根K线时间对齐，一腿缺失的那根不判定。</p></div>}
      {plan.pullback && <div className="strategy-block-note"><PullbackRule plan={plan}/></div>}
      <SideBySide>
        {long && <EntrySide plan={plan} group="entryLong" readings={readings} flag={flags.entryLong}/>}
        {short && <EntrySide plan={plan} group="entryShort" readings={readings} flag={flags.entryShort}/>}
      </SideBySide>
      <StatTiles columns={2} label="开仓限制" items={gates}/>
    </SectionCard>
    <SectionCard title="② 什么时候平仓" label="什么时候平仓" note="哪条先到按哪条走">
      <SideBySide>
        {long && <ExitSide plan={plan} group="exitLong" readings={readings}/>}
        {short && <ExitSide plan={plan} group="exitShort" readings={readings}/>}
      </SideBySide>
      <StatTiles label="止损与止盈" items={[stopTile(plan), takeTile(plan),
        ...planBlockRows(plan, "exit").map(([label, value]) => ({ label, value })),
        // 10-08 用户决定：没设当日亏损上限就不提它。
        ...(plan.maxLoss != null ? [{ label: "当日亏损上限", value: `${plan.maxLoss} ${unit}`, note: "到了停止开新仓" }] : [])]}/>
    </SectionCard>
    <SectionCard title="③ 怎么下单、下多少" label="怎么下单">
      <StatTiles columns={3} label="下单与仓位" items={[
        { label: "成交方式", value: plan.spread ? "收盘确认，两腿各自按下一根开盘价成交" : "收盘确认，下一根开盘价成交",
          note: plan.spread ? "实盘两腿同时下单，瘸腿自动补一次" : plan.pyramiding ? "浮盈时分步加仓" : "持仓时不加仓" },
        { label: "方向", value: directions[plan.direction] },
        { label: "执行周期", value: `${periodLabel(plan.timeframeSeconds)}${plan.timeframeSeconds === 0 ? `（当前 ${periodLabel(chartTimeframe)}）` : ""}` },
        sizing ? { label: "按 ATR 定手数", value: sizing[1], note: plan.pyramiding ? "首笔给加仓留空间" : undefined }
          : { label: "每次开仓", value: plan.spread ? `${plan.maxPosition} 份价差` : `${plan.maxPosition} 手`, note: plan.pyramiding ? "首笔给加仓留空间" : undefined },
        ...(plan.perTradeRisk != null ? [{ label: "单笔预算", value: `${plan.perTradeRisk} ${unit}`, note: "不足一手时跳过" }] : []),
        { label: "每天最多开仓", value: `${plan.maxOrdersPerDay} 次` },
        { label: "平仓后等待", value: `${plan.cooldownBars} 根` },
      ]}/>
    </SectionCard>
  </div>;
}

type DesignJob = AiProgress & { id: string; result?: StrategyPlan; confirmed?: boolean; error?: {code: string; message: string; phase: string; action: string} };
type StrategyEditorDraft = { instrumentScope?: InstrumentScope; answers?: Record<string, string>; clarificationHistory?: {question: string; answer: string}[][]; description: string; direction: StrategyPlan["direction"]; volume: number; /** 当日亏损上限；null = 不设（10-08 用户决定：用户没填就不带）。 */ maxLoss: number | null; plan?: StrategyPlan; previousPlan?: StrategyPlan; jobId?: string; job?: DesignJob };

export function StrategyDesigner({ rootUrl, symbol, initialPlan, initialScope, workspaceScope, draftKey, editSession, onSaved, onOpenSettings, active = true }: { rootUrl: string; symbol: string; initialPlan?: StrategyPlan; initialScope?: InstrumentScope; workspaceScope?: string; draftKey?: string; /** The host's current edit session; a save reports the session it started in. */ editSession?: () => number; onSaved: (item: SavedStrategy, session?: number) => void; onOpenSettings?: () => void; /** False while the hosting dialog is closed: the disclosures are then not commandable. */ active?: boolean }) {
  const scope = useWorkspaceScope(rootUrl, workspaceScope);
  const [draft, setDraft, saveError] = usePersistentDraft<StrategyEditorDraft>(scope ? `ai-trader-strategy-draft:${scope}:${draftKey || "new"}` : undefined,
    { description: initialPlan?.description || "", direction: initialPlan?.direction || "both", volume: initialPlan?.maxPosition || 1, maxLoss: initialPlan?.maxLoss ?? null, plan: initialPlan, instrumentScope: initialScope || {mode: "any", symbols: []} });
  const { description, direction, volume, maxLoss, plan, job } = draft;
  const history = draft.clarificationHistory || [];
  const answers = draft.answers || {};
  const questions = plan ? strategyQuestions(plan) : [];
  const binding = draft.instrumentScope || {mode: "any" as const, symbols: []};
  // 外盘策略（外盘三期）按美元记钱：适用范围里的合约优先，否则看当前图表。
  const unit = moneyUnit(binding.symbols[0] ?? symbol);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [pollEpoch, setPollEpoch] = useState(0);
  const [disconnected, setDisconnected] = useState(false);
  const sending = useRef(false);
  const busy = saving || job?.status === "running";
  // The idea section starts open and folds once a plan exists; the agent can reopen it (`strategy-idea`).
  const [ideaOpen, setIdeaOpen] = useDisclosureSurface("strategy-idea", { initial: !plan, available: active });
  const hasPlan = Boolean(plan);
  useEffect(() => setIdeaOpen(!hasPlan), [hasPlan, setIdeaOpen]);
  const change = (values: Partial<StrategyEditorDraft>) => { setDraft(d => ({ ...d, ...values, previousPlan: d.plan || d.previousPlan, plan: undefined, jobId: undefined, job: undefined })); setError(""); };
  useEffect(() => {
    const id = draft.jobId;
    if (!id || !scope) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>; let failures = 0;
    setDisconnected(false);
    const poll = async () => {
      try {
        const next = await requestJson<DesignJob>(`${rootUrl}/api/strategy-plans/design-jobs/${id}`);
        if (cancelled) return;
        failures = 0;
        setDraft(d => d.jobId === id ? { ...d, job: { ...next, confirmed: true }, plan: next.result || d.plan } : d);
        setError("");
        if (next.status === "running") timer = setTimeout(poll, 800);
      } catch (error) {
        if (cancelled) return;
        const status = requestStatus(error);
        if ([401, 403, 404].includes(status || 0)) {
          const message = status === 404 ? "生成任务已不存在，原文已保留，可以重新生成。" : "连接已失效，请重新连接后再恢复任务。";
          setDraft(d => d.jobId === id ? {...d, job: {...d.job!, id, confirmed: status === 404, status: "failed", stage: message}} : d);
          setError(message);
        } else {
          setError("暂时无法读取生成状态；原文已保留，请重新连接任务。");
          if (++failures < 3) timer = setTimeout(poll, 2000);
          else setDisconnected(true);
        }
      }
    };
    void poll(); return () => { cancelled = true; clearTimeout(timer); };
  }, [draft.jobId, rootUrl, scope, pollEpoch]);
  const generate = async (nextHistory = history) => {
    if (sending.current) return;
    sending.current = true;
    const id = draft.jobId && !job?.confirmed ? draft.jobId : crypto.randomUUID();
    const previousPlan = plan || draft.previousPlan;
    setError(""); setDisconnected(false);
    setDraft(d => ({ ...d, previousPlan, clarificationHistory: nextHistory, answers: {}, jobId: id, job: { id, status: "running", stage: "正在发送请求", startedAt: new Date().toISOString() } }));
    try {
      const next = await requestJson<DesignJob>(`${rootUrl}/api/strategy-plans/design-jobs`, { method: "POST", body: JSON.stringify({ id, description, constraints: { direction, maxPosition: volume, maxLoss, clarificationRound: nextHistory.length, clarificationAnswers: nextHistory.flat(), ...(previousPlan ? {previousPlan} : {}) } }) });
      setDraft(d => d.jobId === id ? { ...d, job: { ...next, confirmed: true }, plan: next.result || d.plan } : d);
      setPollEpoch(value => value + 1);
    } catch (error) {
      const status = requestStatus(error);
      const message = status ? strategyError(error, "请求未被接受，请检查输入后重试。") : "请求尚未确认，原文已保留；重试会先恢复同一任务。";
      setDraft(d => d.jobId === id && !d.job?.confirmed ? { ...d, job: { ...d.job!, confirmed: Boolean(status && status >= 400 && status < 500), status: "failed", stage: message } } : d);
      setError(message);
    } finally { sending.current = false; }
  };
  return <div className="strategy-editor">
    <details className="strategy-idea-fields" open={ideaOpen} onToggle={e => setIdeaOpen(e.currentTarget.open)}><summary>交易想法与限制{plan ? ` · ${periodLabel(plan.timeframeSeconds)} · 最多 ${volume} 手` : ""}</summary>
    <div className="strategy-idea-body"><label>描述你的交易想法<textarea rows={5} maxLength={20000} value={description} disabled={busy || !scope} onChange={e => change({description: e.target.value})} placeholder="描述何时开仓、何时平仓，以及需要遵守的限制。"/></label>
    <HelpButton topic="strategy" />
    <StrategyScopeFields value={binding} onChange={instrumentScope => setDraft(d => ({...d, instrumentScope}))} disabled={busy || !scope}/>
    {(!scope || saveError) && <small role="status">{!scope ? "正在恢复草稿…" : "草稿暂未保存，请勿关闭页面"}</small>}
    <div className="strategy-risk-fields"><label>方向<select value={direction} disabled={busy || !scope} onChange={e => change({direction: e.target.value as StrategyPlan["direction"]})}>{Object.entries(directions).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>最大仓位 · 手<input type="number" min="1" max="20" value={volume} disabled={busy || !scope} onChange={e => change({volume: Number(e.target.value)})}/></label><label>当日亏损上限 · {unit}<input type="number" min="1" placeholder="不设" value={maxLoss ?? ""} disabled={busy || !scope} onChange={e => change({maxLoss: e.target.value.trim() === "" ? null : Number(e.target.value)})}/></label></div>
    </div></details>
    {(!plan || job?.status === "failed" || job?.status === "cancelled" || Boolean(plan.questions.length && !plan.clarifications?.length)) && <button type="button" className="strategy-primary" disabled={busy || !scope || !description.trim() || !Number.isFinite(volume) || volume < 1 || volume > 20 || (maxLoss !== null && (!Number.isFinite(maxLoss) || maxLoss <= 0))} onClick={() => void generate()}><Bot size={16}/>{busy ? "正在设计策略…" : job?.status === "failed" || job?.status === "cancelled" ? "重新生成策略" : plan?.questions.length ? "生成可选建议" : "设计策略"}</button>}
    {job && <AiProgressCard id={job.id} title={job.status === "failed" ? "本次生成未完成" : "正在设计策略"} task={job} onCancel={job.confirmed ? () => requestJson<DesignJob>(`${rootUrl}/api/strategy-plans/design-jobs/${job.id}`, {method: "DELETE"}).then(next => setDraft(d => d.jobId === job.id ? {...d, job: {...next, confirmed: true}} : d)).catch(error => setError(strategyError(error, "停止尚未确认，请重新连接任务"))) : undefined}/>}
    {job?.status === "failed" && plan && <p role="status">本次生成未完成，下面保留上次草稿，可重试继续完善。</p>}
    {job?.status === "failed" && <div className="strategy-failure-actions">{onOpenSettings && <button type="button" onClick={onOpenSettings}>检查模型连接</button>}<details><summary>任务详情</summary><p>任务编号：{job.id}</p><p>{job.error?.phase ? `失败阶段：${job.error.phase}` : "旧任务未记录具体失败阶段"}</p></details></div>}
    {disconnected && <button type="button" onClick={() => setPollEpoch(value => value + 1)}>重新连接任务</button>}
    {questions.length > 0 && <StrategyQuestions questions={questions} answers={answers} round={history.length} busy={busy} onChange={(id, answer) => setDraft(d => ({...d, answers: {...d.answers, [id]: answer}}))} onContinue={() => void generate([...history, questions.map(q => ({question: q.question, answer: (() => { const answer = answers[q.id].trim(); const option = q.options.find(o => o.label === answer); return option ? `${option.label}：${option.description}` : answer; })()}))])}/>}
    {plan && <><label>策略名称<input disabled={busy} value={plan.name} maxLength={80} onChange={e => setDraft(d => ({ ...d, plan: d.plan ? { ...d.plan, name: e.target.value } : undefined }))}/></label>
      <p>指标周期：{periodLabel(plan.timeframeSeconds)}</p>
      {Object.values(plan.rules).some(group => (group?.conditions ?? []).some(c => c.left.timeframe !== 0)) && <button type="button" disabled={busy} onClick={() => { const next = followChart(plan); setDraft(d => ({...d, plan: next, description: next.description, previousPlan: next, job: undefined, jobId: undefined})); }}>所有指标改为跟随图表</button>}
      <DesignerRules plan={plan} unit={unit} active={active} busy={busy} onChange={next => setDraft(d => d.plan ? { ...d, plan: next } : d)}/>
      <div className="strategy-actions"><button type="button" className="strategy-primary" disabled={busy || !plan.name.trim()} onClick={async () => { const problem = designerPlanProblem(plan); if (problem) { setError(problem); return; } const session = editSession?.(); setSaving(true); try { const saved = await requestJson<SavedStrategy>(`${rootUrl}/api/strategy-plans`, { method: "POST", body: JSON.stringify({ plan, instrumentScope: binding }) }); onSaved(saved, session); } catch (error) { if (session === editSession?.()) setError(strategyError(error, "策略保存失败，请检查规则后重试")); } finally { setSaving(false); } }}><BookmarkPlus size={16}/>{questions.length ? "保存待完善草稿" : "保存到我的策略"}</button></div></>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

export function StrategyWorkbench(props: WorkbenchProps) {
  return <StrategyWorkbenchContent key={`${props.baseUrl}|${props.workspaceScope}|${props.symbol}`} {...props}/>;
}

type WorkbenchProps = {
  workspaceScope?: string; rootUrl: string; baseUrl: string; symbol: string; chartTimeframe?: number; replayActive: boolean;
  account?: AccountOverviewSnapshot; market?: MarketWorkbenchSnapshot; onOpenSettings?: () => void;
  /** 策略面板 wiring (the dock clones these in); a standalone mount keeps its own tab and chooser. */
  tab?: StrategyTab; onTabChange?: (tab: StrategyTab) => void;
  onSelectionChange?: (id: string) => void; onStarted?: () => void;
  /** 提醒模式 (第 6 批): a 提醒 saved and started from the editor card; the workspace shows 我的提醒. */
  onShowReminders?: () => void;
  /** Contract catalog, for 螺纹2701-style names on the cards. */
  instruments?: InstrumentCatalog;
  /** 设置 → 交易 → AI 自主执行: 自动下单 is only offered (上线 dialog, running cards) while it is on. */
  autonomy?: boolean;
  /** 对象级 AI "AI ▾" on 详情 and on each running card: the question goes out as the user's turn
   *  with the object as focus. explainBlocked explains denied signal items (strategy-signals),
   *  backtestBlocked 回测与体检 (strategy-backtests). */
  onAskAi?: AskAi; explainBlocked?: string | null; backtestBlocked?: string | null;
  /** The chat an instance started here sends its reminders and strategy cards to. */
  sessionId?: string;
  /** Folded at the bottom of 详情 (the research data card). */
  detailFooter?: ReactNode;
  /** 在图上标出买卖点 on another contract / period: switch the chart there first. */
  onShowChart?: (symbol: string, durationSeconds: number) => void;
};

// 打法 (2026-10-25) comes first: the user's own setups, the ones the trade review groups and checks by.
const TABS: { value: "playbooks" | "library" | "running"; label: string }[] = [{ value: "playbooks", label: "打法" },
  { value: "library", label: "我的策略" }, { value: "running", label: "正在运行" }];
const FAILED_ATTEMPTS = ["FAILED", "REJECTED", "NOT_DISPATCHED", "SKIPPED", "BLOCKED"];
const isRunning = (item: SavedStrategy) => item.status === "monitoring" || item.status === "paused";
const ORDER = ["running", "paused", "idle", "draft"];

/**
 * 策略面板 (策略页改版第八批, 2026-10-08; the owner approved the mockup): one main line and two tabs.
 * 我的策略 has the ask box on top (one sentence → the AI designs it → it is saved → backtested → its
 * 结果页 opens; a template chip does the same with the template's defaults, 全部 22 个 opens the
 * gallery) and one card per strategy (state, its one sentence, the latest backtest, today's signals,
 * the 今日判断 it goes against). A card opens its 结果页 (`strategyTab: detail`): the backtest in plain
 * words, AI 怎么看, the four rule rows, 试几组参数比一比 (the old 实验室 tab, `strategyLab`) and 跟 AI 说
 * 怎么改 / 存着先不上线 / 上线运行 (the unchanged launch dialog). 正在运行 lists the monitoring / paused
 * instances with today's signals, 暂停 / 继续 and 停止并平仓 (a close card to confirm, never an order).
 */
function StrategyWorkbenchContent({ rootUrl, baseUrl, symbol, chartTimeframe = 60, replayActive, market, workspaceScope, onStarted, onShowReminders, onSelectionChange,
  instruments, autonomy, onAskAi, explainBlocked, backtestBlocked, sessionId, detailFooter, onShowChart, ...wiring }: WorkbenchProps) {
  const access = useContext(AccountAccessContext);
  const scope = useWorkspaceScope(rootUrl, workspaceScope);
  const marketClosed = market?.quote.symbol.toLowerCase() === symbol.toLowerCase() && marketClosedNow(market);
  const closedMessage = marketClosed ? replayActive ? "回放已停止，当前不再更新行情" : `${marketSessionDisplay(market).detail}，等待交易时段行情更新` : undefined;
  const [ownTab, setOwnTab] = useState<StrategyTab>("library");
  const tab = wiring.tab ?? ownTab;
  const changeTab = (next: StrategyTab) => { if (wiring.onTabChange) wiring.onTabChange(next); else setOwnTab(next); };
  // 策略编辑卡 (2026-10-08): new and edited strategies open the one card in this panel; its draft lives in
  // strategyEditorStore (a contract switch remounts the panel, the draft stays). `strategy-new` is its old name.
  const editor = useStrategyEditor();
  setEditorContext({ rootUrl, chartSymbol: symbol, chartTimeframe });
  useDisclosureSurface("strategy-new", { state: editor.open && editor.mode === "new", apply: open => {
    if (open) newDraft(); else if (editor.mode === "new") discardDraft();
  } });
  // Folds of the selected strategy; commandable and reported as `strategyDetails`.
  const [details, setDetails] = useState<{ scope: boolean; idea: boolean; rules: boolean }>({ scope: false, idea: false, rules: false });
  const [items, setItems] = useState<SavedStrategy[]>([]);
  const [executions, setExecutions] = useState<SavedStrategy[]>([]);
  const [selected, setSelected] = useState("");
  // A running instance picked by `selectedStrategyId` (its own id): the panel shows its library strategy on
  // 正在运行, and the published selection is the instance the command named (AI 审计 2026-10-13).
  const [selectedRun, setSelectedRun] = useState("");
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [pendingExecution, setPendingExecution] = useState<{library: string; id: string}>();
  const [monitor, setMonitor] = useState<StrategyMonitorSnapshot>();
  const [monitorUnavailable, setMonitorUnavailable] = useState(false);
  // 运行日志 per running instance (`disclosure: strategy-run-log` with the instance id).
  const [logs, setLogs] = useState<Record<string, boolean>>({});
  const monitorEpoch = useRef(0);
  const context = `${baseUrl}|${symbol}|${selected}`;
  const activeContext = useRef(context);
  activeContext.current = context;
  const [conditions, setConditions] = useState<{ conditions: ConditionReading[] }>();
  const [conditionError, setConditionError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const operation = useRef(false);
  const navigation = useContext(UiNavigationContext);
  const current = items.find(item => item.strategyId === selected);
  const runsOf = (item: SavedStrategy) => [...executions.filter(run => run.definition.libraryStrategyId === item.strategyId && run.status !== "archived"),
    ...(item.definition.instrumentScope ? [] : [item])];
  // 提醒页的提醒 (第 6 批, `plan.reminderOnly`) are strategies too, but listed under 我的提醒, not here.
  const instances = [...executions.filter(isRunning), ...items.filter(item => !item.definition.instrumentScope && isRunning(item))].filter(item => !isReminder(item));
  const libraryOf = (run: SavedStrategy) => items.find(item => item.strategyId === run.definition.libraryStrategyId) ?? run;
  const execution = executions.find(item => item.definition.libraryStrategyId === selected && item.symbol.toLowerCase() === symbol.toLowerCase() && item.status !== "archived");
  const executionId = execution?.strategyId || (pendingExecution?.library === selected ? pendingExecution.id : "") || (current && !current.definition.instrumentScope ? selected : "");
  const needsAnswers = needsAnswersOf(current);
  const currentMonitor = monitor?.strategyId === executionId ? monitor : undefined;
  const status = currentMonitor?.status || execution?.status || current?.status;
  const display = monitorDisplay({ status: status || "draft", decision: currentMonitor?.decision, monitorState: currentMonitor?.monitorState }, monitorUnavailable);
  const select = (id: string) => { setSelected(id); setError(""); changeTab("detail"); };
  useLayoutEffect(() => {
    if (!navigation) return;
    navigation.handler.current = changes => {
      if (changes.selectedStrategyId !== undefined) {
        // A running instance resolves against the cards 正在运行 shows (#843): a legacy instance kept in the
        // library list selects its running card too, and a stopped/archived run is not a card to select.
        const run = instances.find(item => item.strategyId === changes.selectedStrategyId);
        if (run) { setSelected(libraryOf(run).strategyId); setSelectedRun(run.strategyId); changeTab("running"); }
        else if (items.some(item => item.strategyId === changes.selectedStrategyId)) select(changes.selectedStrategyId);
        else if (executions.some(item => item.strategyId === changes.selectedStrategyId)) throw new Error("这个运行实例已停止，不在正在运行里");
        else throw new Error("策略不存在");
      }
      // `tab` beside a selection (the resume dialog's 打开策略) keeps the tab the selection chose.
      if (changes.tab === "fees") openFeeSettings();
      else if (changes.tab !== undefined && changes.selectedStrategyId === undefined) changeTab("library");
      // 策略设计窗 retired 2026-10-08: designerOpen opens the 策略编辑卡 on the selected strategy (none: new) or hides it.
      if (changes.designerOpen === false) hideDraft(); else if (changes.designerOpen) editOrNew();
    };
  });
  const runSelection = selectedRun && tab === "running"
    && instances.some(run => run.strategyId === selectedRun && libraryOf(run).strategyId === selected) ? selectedRun : "";
  useEffect(() => {
    navigation?.publish({ selectedStrategyId: runSelection || selected, tab: "library", designerOpen: editor.open,
      items: items.slice(0, 200).map(item => ({ strategyId: item.strategyId, name: strategyText(item.name), version: item.version })) });
  }, [navigation, selected, runSelection, editor.open, items]);
  useEffect(() => () => { if (navigation) { navigation.handler.current = undefined; navigation.publish(null); } }, [navigation]);
  useUiSurface("strategyDetails", { state: { rules: tab === "detail" && Boolean(current) && details.rules, scope: details.scope, executions: Boolean(executionId && logs[executionId]), idea: details.idea },
    apply: value => {
      const { section, open } = value as { section: "rules" | "scope" | "executions" | "idea"; open: boolean };
      if (!current) throw new Error("先选一个策略");
      if (section === "executions") {
        if (!executionId) throw new Error("该策略在当前合约没有运行实例");
        setLogs(previous => ({ ...previous, [executionId]: open }));
        if (open) changeTab("running");
        return;
      }
      changeTab("detail");
      setDetails(previous => ({ ...previous, [section]: open }));
    } }, [details, current, tab, executionId, logs]);
  // 说人话: the strategy's one sentence as its own snapshot field `strategySummary` (strategyDetails stays booleans).
  const planSummary = tab === "detail" && current ? current.planSummary ?? null : null;
  useUiSurface("strategySummary", { state: planSummary }, [planSummary]);
  useEffect(() => { onSelectionChange?.(executionId); }, [executionId, onSelectionChange]);
  const loadSequence = useRef(0);
  const loadContext = useRef({ baseUrl, active: true });
  loadContext.current.baseUrl = baseUrl;
  const reload = async () => {
    const sequence = ++loadSequence.current;
    const active = () => loadContext.current.active && loadContext.current.baseUrl === baseUrl && sequence === loadSequence.current;
    try {
      const result = await loadStrategies(baseUrl);
      if (!active()) return;
      const values = result.items.filter(item => item.definition.plan);
      setListLoaded(true);
      setItems(values); setExecutions(result.executions || []);
      if (!values.length) selectBacktestStrategy(null);
      setSelected(previous => values.some(item => item.strategyId === previous) || previous === pendingSelect.current ? previous : values[0]?.strategyId || "");
    } catch (error) { if (active()) setError(strategyError(error, "策略列表读取失败")); }
  };
  // A strategy just saved by the main line may not be in the list yet: keep it selected until it is.
  const pendingSelect = useRef<string | null>(null);
  // State, not a ref: the empty state must re-render once the first read lands (#522 A-08).
  const [listLoaded, setListLoaded] = useState(false);
  useEffect(() => {
    // The panel remembers its selection for the window.
    loadContext.current.active = true; setSelected(backtestSelection()?.strategyId ?? ""); setError(""); void reload();
    const refresh = () => { void reload(); };
    window.addEventListener("ai-trader-strategies-changed", refresh);
    // Strategy changes arrive through the conversation stream; the timer only catches drift.
    const timer = setInterval(() => { if (!feedLiveNow() || Date.now() % 15000 < 5000) refresh(); }, 5000);
    return () => { loadContext.current.active = false; ++loadSequence.current; clearInterval(timer); window.removeEventListener("ai-trader-strategies-changed", refresh); };
  }, [baseUrl]);
  useEffect(() => {
    setConditions(undefined); setConditionError(""); setMonitorUnavailable(false);
    if (!executionId || tab !== "detail") return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const epoch = monitorEpoch.current;
      const active = () => !cancelled && !operation.current && epoch === monitorEpoch.current;
      try {
        const state = await getStrategyMonitor(baseUrl, executionId);
        if (active()) { setMonitor(state); setMonitorUnavailable(false); }
        if (!needsAnswers && ["monitoring", "ready", "paused"].includes(state.status)) {
          try {
            const detail = await requestJson<typeof conditions>(`${baseUrl}/api/strategy-plans/${executionId}/conditions`);
            if (active()) { setConditions(detail); setConditionError(""); }
          } catch (error) { if (active()) { setConditions(undefined); setConditionError(strategyError(error, "暂时无法读取条件，请刷新行情后重试。", closedMessage)); } }
        }
      } catch (error) { if (active()) { setMonitorUnavailable(true); setConditionError(strategyError(error, "运行状态暂不可用")); } }
      finally { if (!cancelled) timer = setTimeout(poll, 3000); }
    };
    void poll(); return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl, executionId, tab, needsAnswers, closedMessage]);
  const edit = () => { if (current) editDraft(current, symbolFor(current)); };
  const editOrNew = () => { if (current?.definition.plan) editDraft(current, symbolFor(current)); else newDraft(); };
  const announce = () => window.dispatchEvent(new Event("ai-trader-strategies-changed"));
  // 上线运行 (2026-09-25): opens the launch dialog (contract + 执行方式); `dialog: strategy-launch`.
  const [launchOpen, setLaunchOpen] = useState(false);
  const [launchError, setLaunchError] = useState("");
  const openLaunch = () => { if (!current?.definition.plan || needsAnswers) throw new Error("先在我的策略选一个可上线的策略"); setLaunchError(""); changeTab("detail"); setLaunchOpen(true); };
  // 编辑卡的“上线运行…”存好之后：刷新列表、选中这条策略并打开它的上线对话框（执行方式默认出卡确认；
  // “验证并上线”仍由用户点）。The list may not hold it yet, so the dialog opens once it does.
  const [launchFor, setLaunchFor] = useState<string | null>(null);
  useEffect(() => {
    const item = launchFor ? items.find(entry => entry.strategyId === launchFor) : undefined;
    if (!item) return;
    setLaunchFor(null); select(item.strategyId);
    if (!needsAnswersOf(item)) { setLaunchError(""); setLaunchOpen(true); }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [launchFor, items]);
  // 规则回测: the one 回测 the page has (last 6 months, fees counted); the result is stored by the
  // Runtime, read back on opening a 结果页, and a run with a session also posts its card to the chat.
  const currentId = current?.strategyId;
  const currentName = current ? strategyText(current.name) : "";
  useEffect(() => { if (currentId) selectBacktestStrategy({ strategyId: currentId, name: currentName }); }, [currentId, currentName]);
  const backtest = useBacktestCard(currentId);
  const marks = useBacktestMarks();
  const backtestResult = backtest.result;
  const marked = Boolean(backtestResult && marks?.backtestId === backtestResult.backtestId);
  const backtestPlan = current?.definition.plan;
  const symbolFor = (item: SavedStrategy) => strategySymbol(item, symbol);
  const backtestSymbol = current ? symbolFor(current) : symbol;
  const runBacktestFor = (item: SavedStrategy, cardSession?: string, where?: "auto" | "server") => {
    if (backtestBlocked) { if (access?.deny) access.deny(backtestBlocked); else setError(backtestBlocked); return; }
    const plan = item.definition.plan;
    if (!plan) throw new Error("先在我的策略选一个策略");
    if (needsAnswersOf(item)) throw new Error("策略还有待确认的条件，完善后才能回测");
    return runStrategyBacktest(rootUrl, { strategyId: item.strategyId, symbol: symbolFor(item),
      ...(plan.timeframeSeconds ? {} : { timeframe: chartTimeframe }),
      // A replay's strategies live in its child Runtime, which has no history: send the plan to the main one.
      ...(replayActive ? { plan } : {}), ...(cardSession ? { sessionId: cardSession } : {}), ...(where ? { where } : {}) });
  };
  const runBacktest = (cardSession?: string, where?: "auto" | "server") => {
    if (!current) throw new Error("先在我的策略选一个策略");
    changeTab("detail");
    return runBacktestFor(current, cardSession, where);
  };
  // The 结果页 opened: its stored result (until a run replaces it), the model's verdict sentence, the last comparison.
  useEffect(() => { if (currentId && tab === "detail") { void loadStoredBacktest(rootUrl, currentId); void loadCompare(rootUrl, currentId); prewarmEngine(rootUrl); } }, [currentId, tab, rootUrl]);
  const verdictPending = Boolean(backtestResult?.verdict && backtestResult.verdict.text == null && backtestResult.verdict.facts.length);
  const [asking, setAsking] = useState(false);
  useEffect(() => {
    if (!currentId || !backtestResult || !verdictPending || tab !== "detail") { setAsking(false); return; }
    let live = true; setAsking(true);
    void requestVerdict(rootUrl, currentId, backtestResult.backtestId).finally(() => { if (live) setAsking(false); });
    return () => { live = false; setAsking(false); };
  }, [currentId, backtestResult?.backtestId, verdictPending, tab, rootUrl]);   // eslint-disable-line react-hooks/exhaustive-deps
  const markOnChart = (mark: boolean) => {
    if (!mark) { if (marked) setBacktestMarks(null); return; }
    if (!backtestResult) throw new Error("先回测，再标买卖点");
    setBacktestMarks(backtestMarks(backtestResult));
    if (backtestResult.symbol.toLowerCase() !== symbol.toLowerCase() || backtestResult.timeframeSeconds !== chartTimeframe) onShowChart?.(backtestResult.symbol, backtestResult.timeframeSeconds);
  };
  // What the chart can show: trades inside the loaded K-lines of the backtest's contract and period.
  let markNote: string | undefined;
  if (marked && backtestResult) {
    const sameChart = market && market.quote.symbol.toLowerCase() === backtestResult.symbol.toLowerCase() && market.durationSeconds === backtestResult.timeframeSeconds;
    if (!sameChart) markNote = onShowChart ? `正在把图表切到 ${backtestResult.symbol} · ${periodLabel(backtestResult.timeframeSeconds)}…`
      : `买卖点画在 ${backtestResult.symbol} · ${periodLabel(backtestResult.timeframeSeconds)} 的图上，请切换过去查看。`;
    else if (market.bars.length) {
      const first = market.bars[0].datetime, end = market.bars.at(-1)!.datetime + market.durationSeconds * 1e9;
      const shown = backtestResult.trades.filter(trade => (trade.openTime >= first && trade.openTime < end) || (trade.closeTime >= first && trade.closeTime < end)).length;
      markNote = shown < backtestResult.trades.length ? `图上显示 ${shown} / ${backtestResult.trades.length} 笔；其余不在图表已加载的K线范围内。` : `已在图上标出 ${shown} 笔的开平仓点。`;
    }
  }
  // 一条主线 (2026-10-08): a strategy the 编辑卡 saved is selected, its 结果页 opens and it is backtested
  // once the list holds it.
  const [rerunFor, setRerunFor] = useState<string | null>(null);
  const created = (item: SavedStrategy) => {
    if (!loadContext.current.active) return;
    pendingSelect.current = item.strategyId;
    setSelected(item.strategyId); setError(""); changeTab("detail");
    setRerunFor(item.strategyId);
    void reload();
  };
  // 策略体检 (2026-09-26): 按这个改 saves the advised change as a new version, selects it and reruns the backtest.
  const [applying, setApplying] = useState(false);
  const [appliedAdvice, setAppliedAdvice] = useState<string | null>(null);
  const applyAdvice = async (advice: StrategyHealthAdvice) => {
    if (!currentId) throw new Error("先在我的策略选一个策略");
    if (applying) return;
    const from = currentId;
    setApplying(true); setError("");
    try {
      const item = await applyStrategyAdvice(rootUrl, currentId, advice);
      setAppliedAdvice(advice.id);
      await reload();
      // #520-D: a late answer only selects the new version when the user still looks at the strategy it came from.
      if (selectedRef.current === from) { select(item.strategyId); setRerunFor(item.strategyId); setError(`已按建议改好，存为 v${item.version}，正在重新回测；原版本保留`); }
    } catch (error) { if (selectedRef.current === from) setError(strategyError(error, "按建议修改未完成，请重试")); }
    finally { setApplying(false); }
  };
  useEffect(() => {
    if (!rerunFor || currentId !== rerunFor || !backtestPlan) return;
    setRerunFor(null);
    pendingSelect.current = null;
    // A saved (or re-saved) strategy's backtest is the Runtime's own run (本机优先 never stands for a saved strategy).
    try { void runBacktest(undefined, "server"); } catch (error) { setError(error instanceof Error ? error.message : "回测未完成，请重试"); }
  }, [rerunFor, currentId, backtestPlan]);   // eslint-disable-line react-hooks/exhaustive-deps
  // 本机优先: the browser engine's state (off with why / idle / loading / ready) for AI and diagnosis.
  const engine = useEngineStatus();
  const healthResult = backtestResult?.headline && backtestResult.strategyId === currentId ? backtestResult : undefined;
  useUiSurface("strategyBacktest", {
    state: { strategyId: currentId ?? null, running: backtest.running, queueAhead: backtest.queue?.ahead ?? null, params: backtest.params,
      health: healthResult ? { headline: healthResult.headline, advice: (healthResult.advice ?? []).map(item => ({ id: item.id, text: item.text })) } : null,
      applying, appliedAdvice,
      symbol: backtestResult?.symbol ?? backtestSymbol, timeframeSeconds: backtestResult?.timeframeSeconds ?? (backtestPlan?.timeframeSeconds || chartTimeframe),
      summary: backtestResult ? { netProfit: backtestResult.summary.netProfit, maxDrawdown: backtestResult.summary.maxDrawdown,
        winRate: backtestResult.summary.winRate, payoffRatio: backtestResult.summary.payoffRatio, trades: backtestResult.summary.trades,
        tradingDays: backtestResult.coverage.tradingDays, firstTradingDay: backtestResult.coverage.firstTradingDay,
        lastTradingDay: backtestResult.coverage.lastTradingDay } : null,
      // AI 怎么看: the computed facts and the model's sentence (null until it comes; source facts = no model sentence).
      verdict: backtestResult?.verdict ? { facts: backtestResult.verdict.facts.map(fact => fact.text), text: backtestResult.verdict.text,
        source: backtestResult.verdict.source } : null,
      error: backtest.error ?? null, marked, tradesOpen: backtest.tradesOpen,
      // 本机优先 (2026-10-12): where the shown result was computed, why the Runtime did it, and the Runtime's
      // rerun checked against the browser's numbers over the same inputs (diagnostic only, never on screen).
      source: backtestResult?.source ?? null, fallbackReason: backtestResult?.fallbackReason ?? null,
      browserCheck: backtestResult?.browserCheck ? { match: backtestResult.browserCheck.match } : null,
      engine },
    apply: value => {
      const change = value as { tradingDays?: number; slippageTicks?: number; run?: boolean; mark?: boolean; trades?: boolean; applyAdvice?: string };
      if (!currentId) throw new Error("先在我的策略选一个策略");
      const advice = change.applyAdvice === undefined ? undefined : healthResult?.advice?.find(item => item.id === change.applyAdvice);
      if (change.applyAdvice !== undefined && !advice) throw new Error("当前回测结果里没有这条体检建议");
      changeTab("detail");
      if (advice) { void applyAdvice(advice); return; }
      if (change.tradingDays !== undefined || change.slippageTicks !== undefined)
        setBacktestParams(currentId, { ...(change.tradingDays !== undefined ? { tradingDays: change.tradingDays } : {}), ...(change.slippageTicks !== undefined ? { slippageTicks: change.slippageTicks } : {}) });
      if (change.trades !== undefined) setBacktestTradesOpen(currentId, change.trades);
      // 价差: a trade's prices are spread values, which have no place on a contract's price chart.
      if (change.mark && backtestResult?.spread) throw new Error("价差回测的成交价是价差数值，标不到合约价格图上；两腿各自的成交价在逐笔清单的详情里");
      if (change.mark !== undefined) markOnChart(change.mark);
      if (change.run) void runBacktest();
    } }, [currentId, backtest, backtestResult, marked, backtestSymbol, chartTimeframe, symbol, replayActive, rootUrl, healthResult, applying, appliedAdvice, engine], Boolean(currentId && backtestPlan));
  // 恢复模板默认 saves the template's defaults as a new version of the same name (a template is otherwise
  // only the 编辑卡's 起点).
  const templateCatalog = useTemplateCatalog();
  const templateSource = current?.definition.templateSource;
  const sourceTemplate = templateCatalog ? templateById(templateSource?.templateId) : undefined;
  const sourceTemplateId = templateSource?.templateId;
  useEffect(() => { void loadTemplates(rootUrl).catch(() => undefined); }, [sourceTemplateId, rootUrl]);
  const [restoring, setRestoring] = useState(false);
  const restoreTemplate = async () => {
    if (!current || !templateSource) throw new Error("这条策略不是从模板创建的");
    if (templateSource.defaults || restoring) return;
    const from = current.strategyId;
    setRestoring(true); setError("");
    try {
      const item = await instantiateTemplate(rootUrl, templateSource.templateId, { baseStrategyId: current.strategyId, instrumentScope: strategyScope(current) });
      await reload();
      if (selectedRef.current === from) { select(item.strategyId); setRerunFor(item.strategyId); setError("已恢复模板默认参数，另存为新版本，正在重新回测"); }
    } catch (error) { if (selectedRef.current === from) setError(strategyError(error, "恢复模板默认未完成，请重试")); }
    finally { setRestoring(false); }
  };
  useUiSurface("strategyTemplate", {
    state: { detail: current && templateSource && tab === "detail" ? { strategyId: current.strategyId, ...templateSource } : null },
    apply: value => {
      const change = value as { restore?: boolean };
      if (!change.restore) throw new Error("模板画廊已并入策略编辑卡：用 strategyEditor {new: true, start: template:<模板id>} 从模板新建");
      if (!templateSource) throw new Error("结果页里的策略不是从模板创建的");
      changeTab("detail"); void restoreTemplate();
    } }, [current, templateSource, tab, rootUrl, restoring]);
  // 试几组参数比一比 (`strategyLab`, the retired 实验室 tab's command): the 结果页's comparison.
  const compare = useCompare(currentId);
  const startCompareNow = () => {
    if (!current) throw new Error("先选一个策略");
    if (current.definition.plan?.spread) throw new Error("价差策略暂不支持试几组参数比一比");
    void startCompare(rootUrl, current.strategyId, backtestSymbol, current.definition.plan?.timeframeSeconds ? undefined : chartTimeframe);
  };
  useUiSurface("strategyLab", {
    state: { labId: compare.view?.labId ?? null, strategyId: currentId ?? null,
      // 本机优先: browser / server once computed (null before), and why the Runtime computed it.
      where: compare.view?.source ?? null, fallbackReason: compare.view?.fallbackReason ?? null,
      status: compare.running ? "running" : compare.view?.status ?? "none", paramLabel: compare.view?.paramLabel ?? null,
      best: compare.view?.best ?? null, line: compare.view?.line ?? null,
      rows: (compare.view?.rows ?? []).map(row => ({ variantId: row.variantId, label: row.label, current: row.current,
        netProfit: row.summary?.netProfit ?? null, maxDrawdown: row.summary?.maxDrawdown ?? null, winRate: row.summary?.winRate ?? null,
        flags: row.variantId === compare.view?.best ? ["best"] : [] })) },
    apply: value => {
      const change = value as { open?: boolean; labId?: string; run?: boolean; useServer?: boolean; choose?: string; name?: string };
      if (change.labId !== undefined && change.labId !== compare.view?.labId) {
        void getLab(rootUrl, change.labId).then(lab => {
          const owner = (lab.request as { strategyId?: string } | undefined)?.strategyId;
          if (owner && items.some(item => item.strategyId === owner)) select(owner);
          else setError("那次比较不属于我的策略里的任何一条（旧实验室的比较）");
        }).catch(() => setError("没有这次比较"));
        return;
      }
      if (change.open || change.run || change.choose) { if (!current) throw new Error("先在我的策略选一个策略"); changeTab("detail"); }
      if (change.run) startCompareNow();
      if (change.choose !== undefined) {
        const lab = compare.view;
        if (!lab) throw new Error("先比一次");
        if (!lab.rows.some(row => row.variantId === change.choose)) throw new Error("这次比较里没有这组");
        const from = currentId;
        void chooseLabVariant(rootUrl, lab.labId, change.choose, change.name).then(async saved => {
          await reload();
          if (selectedRef.current === from) { select(saved.strategyId); setRerunFor(saved.strategyId); }
        }).catch(error => setError(strategyError(error, "这组没存上，请重试")));
      }
    } }, [compare, currentId, current, items, rootUrl, backtestSymbol, chartTimeframe]);
  // 跟 AI 说怎么改 (`disclosure: strategy-ask-ai`): an input on the 结果页; sent as the user's turn.
  const [askAiOpen, setAskAiOpen] = useDisclosureSurface("strategy-ask-ai", { available: tab === "detail" && Boolean(current) });
  const sendToAi = (text: string) => {
    if (!current || !onAskAi) return;
    onAskAi(text, { kind: "strategy_edit", id: current.strategyId, label: strategyText(current.name) });
  };
  // 验证并上线 (from the dialog) and 继续 on a paused instance: validate on the chosen contract, then
  // start simulation monitoring there. 继续 uses the instance's own contract and bound period and
  // keeps its 执行方式 — the monitor runs on that contract whatever the chart shows.
  const start = async (item: SavedStrategy | undefined, launch: { symbol: string; chartTimeframe?: number; signalAction?: SignalAction; fromDialog?: boolean; reminder?: boolean }) => {
    if (!item || operation.current) return;
    const startedContext = context;
    // A 提醒 starts from the editor card, not from the selected strategy: only a change of Runtime makes it stale.
    const active = () => (launch.reminder ? loadContext.current.baseUrl === baseUrl : activeContext.current === startedContext) && loadContext.current.active;
    const fail = (message: string) => { if (launch.fromDialog) setLaunchError(message); else setError(message); };
    let validatedId = "";
    operation.current = true; ++monitorEpoch.current; setBusy(true); setError(""); setLaunchError(""); setStage("验证规则与所需周期行情…");
    try {
      if (!replayActive) {
        const blocked = await launchBlockedReason(baseUrl);
        if (!active()) return;
        if (blocked) { fail(blocked); return; }
      }
      const id = await validateAndStart(baseUrl, item.strategyId, launch.symbol, launch.chartTimeframe ?? chartTimeframe,
        id => { validatedId = id; if (active()) { setPendingExecution({library: item.definition.libraryStrategyId ?? item.strategyId, id}); setStage("正在启动模拟监控…"); } },
        launch.signalAction ? { signalAction: launch.signalAction, ...(sessionId ? { sessionId } : {}) } : {});
      const state = await getStrategyMonitor(baseUrl, id);
      if (active() && launch.reminder) { setMonitorUnavailable(false); await reload(); onShowReminders?.(); }
      else if (active()) { setMonitor(state); setMonitorUnavailable(false); setLaunchOpen(false); changeTab("running"); onStarted?.(); await reload(); announce(); }
    } catch (error) {
      // A lost POST response is not permission to repeat a trading control action.
      let recovered = false;
      if (validatedId) {
        try {
          const state = await getStrategyMonitor(baseUrl, validatedId);
          if (active()) { setMonitor(state); await reload(); announce(); }
          recovered = state.status === "monitoring";
          if (active() && recovered) { setLaunchOpen(false); changeTab("running"); onStarted?.(); setMonitorUnavailable(false); }
        } catch { if (active()) setMonitorUnavailable(true); }
      }
      if (active() && !recovered) fail(strategyError(error, validatedId ? "启动结果尚未确认，正在回读状态，请勿重复启动。" : "验证未完成，请检查规则与行情连接后重试。"));
    } finally { operation.current = false; ++monitorEpoch.current; setBusy(false); setStage(""); }
  };
  // 继续 restarts the paused instance itself (#520): the library strategy would validate into another execution
  // (a changed scope or another account) and start that one with the default 执行方式 instead of this one's.
  const resume = (run: SavedStrategy) => start(run, { symbol: run.symbol, chartTimeframe: run.definition.chartTimeframeSeconds });
  // 回撤停机 · 恢复运行: the instance's own start with resetDrawdown (a new 起始 and peak); the Runtime only allows it once
  // the strategy's orders and position are settled, and keeps its own permission / 急停 checks.
  const resumeDrawdown = async (run: SavedStrategy) => {
    if (operation.current) return;
    operation.current = true; ++monitorEpoch.current; setBusy(true); setStage("正在恢复运行…"); setError("");
    try {
      const blocked = replayActive ? null : await launchBlockedReason(baseUrl);
      if (blocked) { setError(blocked); return; }
      await resumeAfterDrawdown(baseUrl, run.strategyId); await reload(); announce();
    } catch (error) { setError(strategyError(error, "没有恢复运行，请刷新后重试")); }
    finally { operation.current = false; ++monitorEpoch.current; setBusy(false); setStage(""); }
  };
  // 运行中卡 · 执行方式: the same start call, carrying only the new mode.
  const changeSignalAction = async (run: SavedStrategy, signalAction: SignalAction) => {
    if (operation.current) return;
    operation.current = true; setBusy(true); setStage("正在切换执行方式…"); setError("");
    try { await requestJson<StrategyMonitorSnapshot>(`${baseUrl}/api/strategies/${run.strategyId}/monitor/start`, { method: "POST", body: JSON.stringify({ signalAction }) }); await reload(); announce(); }
    catch (error) { setError(strategyError(error, "执行方式没有切换，请刷新后重试")); }
    finally { operation.current = false; setBusy(false); setStage(""); }
  };
  // 停止 / 停止并平仓: back to 没上线; history stays. With close, the position the strategy still holds
  // gets a close card in its conversation — confirmed by the user, never sent from here.
  const stop = async (run: SavedStrategy, closePositions: boolean) => {
    if (operation.current) return;
    operation.current = true; ++monitorEpoch.current; setBusy(true); setStage("正在停止…"); setError("");
    try {
      const state = await stopStrategyMonitoring(baseUrl, run.strategyId, closePositions);
      setError(state.notice || "已停止：不再判断信号，已有持仓不会平掉。"); await reload(); announce();
    } catch (error) { setError(strategyError(error, "停止尚未确认，请刷新后重试")); }
    finally { operation.current = false; ++monitorEpoch.current; setBusy(false); setStage(""); }
  };
  const pause = async (id: string) => {
    if (operation.current || !id) return;
    const startedContext = context;
    operation.current = true; ++monitorEpoch.current; setBusy(true); setStage("正在暂停监控…"); setError("");
    try {
      const state = await requestJson<StrategyMonitorSnapshot>(`${baseUrl}/api/strategies/${id}/monitor/pause`, {method:"POST",body:"{}"});
      if (activeContext.current === startedContext && loadContext.current.active) { setMonitor(state); setMonitorUnavailable(false); announce(); await reload(); }
    } catch (error) { if (activeContext.current === startedContext) { setMonitorUnavailable(true); setError(strategyError(error, "暂停尚未确认，正在回读状态")); } }
    finally { operation.current = false; ++monitorEpoch.current; setBusy(false); setStage(""); }
  };
  const remove = async (item: SavedStrategy) => {
    if (operation.current) return;
    operation.current = true; setBusy(true); setError("");
    try { await requestJson(`${baseUrl}/api/strategy-plans/${item.strategyId}`, {method: "DELETE"}); await reload(); }
    catch (error) { setError(strategyError(error, "策略删除失败，请刷新后重试")); }
    finally { operation.current = false; setBusy(false); }
  };
  // 策略编辑卡: 存为草稿 → the new version's 结果页 with an automatic backtest; 上线运行… → also its launch
  // dialog (验证并上线 stays the user's click). A new strategy avoids the names in 我的策略 itself.
  const saveEditor = (launch = false) => {
    const reminder = editorState().reminder, reminderSymbol = editorState().form.symbol;
    const saving = saveDraft(items.map(item => item.name));
    void saving.then(({ item, current }) => {
      if (!current) { void reload(); return; }
      discardDraft();
      if (reminder) { if (launch) void startReminder(item, reminderSymbol); else { void reload(); onShowReminders?.(); } return; }
      created(item); if (launch) setLaunchFor(item.strategyId);
    }).catch(() => undefined);
  };
  // 开始提醒: a 提醒 has one 执行方式 (只提醒) and the card's contract, so it starts without the launch dialog.
  const startReminder = (item: SavedStrategy, contract: string) => start(item, { symbol: contract || symbolFor(item), signalAction: "alert", reminder: true });
  const launchEditor = () => {
    const item = editorState().item;
    if (editorState().reminder && item && editorState().mode === "edit" && !needsSave()) { const contract = editorState().form.symbol; discardDraft(); void startReminder(item, contract); return; }
    if (editorState().mode === "edit" && item && !needsSave()) { discardDraft(); setLaunchFor(item.strategyId); void reload(); return; }
    saveEditor(true);
  };
  const editById = (id: string) => {
    const item = items.find(entry => entry.strategyId === id);
    if (item) { editDraft(item, symbolFor(item)); return; }
    // Not in the list yet (made a moment ago by the API, CLI or AI): read the list once; the receipt waits for the card.
    const current = pendingEdit();
    return loadStrategies(baseUrl).then(result => {
      if (!current()) return false;
      const found = result.items.find(entry => entry.strategyId === id);
      if (found?.definition.plan) { editDraft(found, symbolFor(found)); return true; }
      setError("策略不存在"); return false;
    }, error => { if (current()) setError(strategyError(error, "策略列表读取失败")); return false; });
  };
  // `strategyEditor` (AI 可操作性): every control of the card is reachable by these keys; the card is the snapshot.
  useUiSurface("strategyEditor", { state: { ...editorSnapshot(editor), ...(editor.open ? { engine } : {}) },
    apply: value => applyEditorChange(value as EditorChange, { edit: editById, save: () => saveEditor() }) }, [editor, items, symbol, baseUrl, engine]);
  // A tab chosen while the card is open (strategyTab, a card's 结果页) hides the card; the draft stays.
  const shownTab = useRef(tab);
  useEffect(() => { if (shownTab.current === tab) return; shownTab.current = tab; if (editorState().open) hideDraft(); }, [tab]);
  const stateOf = (item: SavedStrategy): CardState => {
    const runs = runsOf(item);
    if (runs.some(run => run.status === "monitoring")) return { key: "running", label: "运行中" };
    if (runs.some(run => run.status === "paused")) return { key: "paused", label: "已暂停" };
    if (needsAnswersOf(item) || item.status === "validating") return { key: "draft", label: item.status === "validating" ? "验证中" : "要补条件" };
    return { key: "idle", label: "没上线" };
  };
  const liveRunOf = (item: SavedStrategy) => runsOf(item).find(run => run.status === "monitoring") ?? runsOf(item).find(run => run.status === "paused");
  const contractOf = (item: SavedStrategy) => {
    const run = liveRunOf(item);
    if (run && run.symbol !== "*") return contractLabel(run.symbol, instruments);
    const bound = strategyScope(item).symbols.find(value => CONTRACT.test(value));
    if (bound) return contractLabel(bound, instruments);
    return item.latestBacktest?.symbol ? contractLabel(item.latestBacktest.symbol, instruments) : scopeLabel(item);
  };
  const library = items.filter(item => !isReminder(item)).sort((a, b) => ORDER.indexOf(stateOf(a).key) - ORDER.indexOf(stateOf(b).key));
  const home = <div className="sh-home">
    <div className="sh-home-actions">
      <button type="button" className="sh-btn primary" onClick={() => newDraft()}><Plus size={14}/>新建策略</button>
      <TemplateStartSelect className="sh-btn sh-select" onStart={start => newDraft(start)}/>
      {editor.draft && !editor.open ? <button type="button" className="sh-btn" onClick={() => openDraft()}>继续编辑：{editor.form.name.trim() || "未命名策略"}</button> : null}
    </div>
    {library.length ? <div className="sh-list">{library.map(item => <StrategyItemActions key={item.strategyId} id={`library:${item.strategyId}`} name={`${item.name} v${item.version}`} disabled={busy} onDelete={() => remove(item)}>
      <LibraryCard item={item} state={stateOf(item)} contract={contractOf(item)} run={liveRunOf(item)} baseUrl={baseUrl}
        disabled={busy} onOpen={() => select(item.strategyId)} onBacktest={() => { setSelected(item.strategyId); try { void runBacktestFor(item); } catch (error) { setError(error instanceof Error ? error.message : "回测未完成"); } }}/>
    </StrategyItemActions>)}</div> : !listLoaded ? error ? null : <p className="strategy-empty" role="status">正在读取策略…</p>
      : !replayActive ? <StrategyStarts rootUrl={rootUrl} symbol={symbol} symbolLabel={contractLabel(symbol, instruments)}
      onStart={start => newDraft(start)}/> : <p className="strategy-empty">还没有策略。点“新建策略”开始。</p>}
  </div>;
  const runState = status === "monitoring" && execution ? "running" : "";
  const plan = current?.definition.plan;
  const planRows = current?.planRows;
  const detailTab = !current || !plan ? <><p className="strategy-empty">{items.length ? "先在我的策略选一个策略。" : listLoaded ? "还没有策略。" : "正在读取策略…"}</p>{detailFooter}</> : <article className="sh-result" data-agent-target="strategy-result">
    <ResultHead name={strategyText(current.name)} sub={`${backtestResult?.symbol && backtestResult.strategyId === current.strategyId ? contractLabel(backtestResult.symbol, instruments) : contractOf(current)} · ${periodLabel(plan.timeframeSeconds || chartTimeframe)}`}
      state={busy && stage ? { key: stateOf(current).key, label: stage } : stateOf(current)} onBack={() => changeTab("library")}/>
    {needsAnswers ? <section className="sh-result-empty" aria-label="要补条件">
      <h3>还有条件要你确认</h3>
      <span className="sh-hint">AI 做这条策略时有几处拿不准，确认后才能回测和上线。</span>
      <button type="button" className="sh-btn primary" disabled={busy} onClick={edit}>完善策略</button>
    </section> : <ResultSummary result={backtestResult} lots={plan.maxPosition} running={backtest.running} error={backtest.error} asking={asking}
      blocked={backtestBlocked} onRun={() => { try { void runBacktest(); } catch (error) { setError(error instanceof Error ? error.message : "回测未完成"); } }}/>}
    {healthResult && <HealthCard result={healthResult} applying={applying} onApply={advice => void applyAdvice(advice)}/>}
    {planRows?.length ? <RuleRows rows={planRows}/> : current.planSummary ? <p className="sh-plain">{current.planSummary}</p> : null}
    {!needsAnswers && !plan.spread ? <CompareBlock card={compare} disabled={busy || !backtestResult} onRun={() => { try { startCompareNow(); } catch (error) { setError(error instanceof Error ? error.message : "比不了"); } }}/> : null}
    {!allowsSymbol(current, symbol) && <p role="status">当前图表合约不在适用范围内；上线时请选择范围内的合约，或在“更多”里修改适用范围。</p>}
    {currentMonitor && status !== "monitoring" && display.tone === "warning" && <p role="status">{display.detail}</p>}
    {error && <p role="status" className="strategy-feedback">{error}</p>}
    {!needsAnswers && <div className="sh-actions">
      <button type="button" className="sh-btn" aria-expanded={askAiOpen} disabled={!onAskAi} onClick={() => setAskAiOpen(open => !open)}>跟 AI 说怎么改</button>
      <button type="button" className="sh-btn" disabled={busy} onClick={edit}>编辑</button>
      <button type="button" className="sh-btn" onClick={() => { changeTab("library"); setError("已存在我的策略里，想上线时再点开它"); }}>存着先不上线</button>
      {runState ? <button type="button" className="sh-btn primary" onClick={() => changeTab("running")}>查看运行中</button>
        : <button type="button" className="sh-btn primary" disabled={busy} onClick={openLaunch}><Play size={14}/>{busy ? stage || "处理中…" : "上线运行"}</button>}
    </div>}
    <AskAiInput open={askAiOpen} onOpenChange={setAskAiOpen} disabled={!onAskAi} onSend={sendToAi}/>
    <details className="sh-more" open={details.rules} onToggle={event => { const open = event.currentTarget.open; setDetails(previous => previous.rules === open ? previous : { ...previous, rules: open }); }}>
      <summary>更多：回测明细、完整规则、适用范围、原始想法</summary>
      <div className="sh-more-body">
        {(backtest.running || backtestResult || backtest.error) && currentId && <StrategyBacktestPanel params={backtest.params} running={backtest.running} queue={backtest.queue}
          result={backtestResult} error={backtest.error} tradesOpen={backtest.tradesOpen} marked={marked} markNote={markNote}
          onParams={params => setBacktestParams(currentId, params)} onRun={() => void runBacktest()} onMark={markOnChart}
          onTradesToggle={open => setBacktestTradesOpen(currentId, open)}/>}
        {templateSource && <p className="strategy-template-origin" data-agent-target="strategy-template-origin"><span>来自模板：{templateSource.templateName}</span>
          {templateSource.defaults ? <small>模板默认参数</small>
            : <button type="button" disabled={busy || restoring} onClick={() => void restoreTemplate()}>{restoring ? "恢复中…" : "恢复模板默认"}</button>}</p>}
        {templateSource && sourceTemplate && <dl className="strategy-template-values" aria-label="模板参数">{sourceTemplate.params.map(param =>
          <div key={param.key}><dt>{param.label}<ParamHelp paramKey={param.key} label={param.label} help={param.help}/></dt>
            <dd>{templateValueText(param.key in templateSource.params ? templateSource.params[param.key] : param.default, param.unit)}</dd></div>)}</dl>}
        {conditionError && <p role="status">{conditionError}</p>}
        <StrategyBlocks plan={plan} unit={moneyUnit(current ? strategySymbol(current, symbol) : symbol)} readings={conditions?.conditions} chartTimeframe={chartTimeframe} flagsFrom={healthResult}/>
        <details className="strategy-details" open={details.scope} onToggle={event => { const open = event.currentTarget.open; setDetails(previous => previous.scope === open ? previous : { ...previous, scope: open }); }}><summary>适用范围：{scopeLabel(current)}</summary><StrategyScopeEditor key={current.strategyId} item={current} baseUrl={rootUrl} onSaved={item => { if (!replayActive) { const from = current.strategyId; void reload(); if (selectedRef.current === from) setSelected(item.strategyId); } else setError("新范围已保存，下次回放使用。"); }}/></details>
        <details className="strategy-details" open={details.idea} onToggle={event => { const open = event.currentTarget.open; setDetails(previous => previous.idea === open ? previous : { ...previous, idea: open }); }}><summary>原始想法（你当时说的话）</summary><p className="strategy-original">{plan.description}</p></details>
        {detailFooter}
      </div>
    </details>
  </article>;
  const runningTab = instances.length ? <div className="sh-list strategy-running">{instances.map(run => <RunningCard key={run.strategyId} run={run} library={libraryOf(run)} baseUrl={baseUrl} scope={scope}
    instruments={instruments} autonomy={autonomy} busy={busy} stage={stage}
    logOpen={Boolean(logs[run.strategyId])} onLogChange={open => setLogs(previous => ({ ...previous, [run.strategyId]: open }))}
    onPause={() => void pause(run.strategyId)} onResume={() => void resume(run)} onResumeDrawdown={() => void resumeDrawdown(run)} onStop={close => void stop(run, close)}
    onSignalAction={action => void changeSignalAction(run, action)}
    onAskAi={onAskAi} explainBlocked={explainBlocked}/>)}
    {error && <p role="status" className="strategy-feedback">{error}</p>}
  </div> : <><p className="strategy-empty">没有正在运行的策略。在我的策略里点开一个，按“上线运行”。</p>
    {error && <p role="status" className="strategy-feedback">{error}</p>}</>;
  const launchDialog = <StrategyLaunchDialog open={launchOpen} item={current} chartSymbol={symbol} instruments={instruments} autonomy={autonomy}
    busy={busy} stage={stage} error={launchError} onRequestOpen={openLaunch} onClose={() => { if (!operation.current) setLaunchOpen(false); }}
    onSubmit={(launchSymbol, signalAction) => void start(current, { symbol: launchSymbol, signalAction, fromDialog: true })}/>;
  const count = (value: "playbooks" | "library" | "running") => value === "running" ? instances.length : value === "library" ? library.length : 0;
  if (editor.open) return <section data-help="strategy" data-popover-bounds className="strategy-workbench strategy-panel strategy-panel-editing">{launchDialog}
    <StrategyEditorCard key={editor.draftId} state={editor} chartTimeframe={chartTimeframe} instruments={instruments} onBack={() => { const reminder = editor.reminder; hideDraft(); if (reminder) onShowReminders?.(); }}
      onDiscard={() => { const reminder = editor.reminder; discardDraft(); if (reminder) onShowReminders?.(); }}
      onStart={start => setStart(start)} onAsk={text => { try { askEditor(text); } catch (error) { setEditorError(error); } }}
      onSave={() => { try { saveEditor(); } catch (error) { setEditorError(error); } }}
      onLaunch={() => { try { launchEditor(); } catch (error) { setEditorError(error); } }}/>
  </section>;
  return <section data-help="strategy" data-popover-bounds className="strategy-workbench strategy-panel">{launchDialog}
    <nav className="strategy-tabs" aria-label="策略面板分页" data-agent-target="strategy-tabs">
      {TABS.map(item => {
        const on = item.value === "library" ? tab === "library" || tab === "detail" : tab === item.value;
        return <button type="button" key={item.value} aria-pressed={on} className={on ? "active" : ""}
          onClick={() => changeTab(item.value)}>{item.label}{count(item.value) ? ` · ${count(item.value)}` : ""}</button>;
      })}
    </nav>
    <div className="strategy-scroll">
      {tab === "playbooks" ? <PlaybookPage baseUrl={baseUrl} strategies={library.map(item => ({ strategyId: item.strategyId, name: item.name }))} />
        : tab === "library" ? home : tab === "detail" ? detailTab : runningTab}
      {tab === "library" && error && <p role="status" className="strategy-feedback">{error}</p>}
    </div>
  </section>;
}

/** 我的策略的一张卡: a running strategy reads its instance's monitor (today's signals, the clash). */
function LibraryCard({ item, state, contract, run, baseUrl, disabled, onOpen, onBacktest }: {
  item: SavedStrategy; state: CardState; contract: string; run?: SavedStrategy; baseUrl: string; disabled: boolean;
  onOpen: () => void; onBacktest: () => void;
}) {
  const { monitor } = useInstanceMonitor(baseUrl, run?.strategyId, 10_000);
  const backtest = useBacktestCard(item.strategyId);
  const shown = backtest.result && backtest.result.strategyId === item.strategyId
    ? { ...item, latestBacktest: { backtestId: backtest.result.backtestId, netProfit: backtest.result.summary.netProfit,
      maxDrawdown: backtest.result.summary.maxDrawdown, winRate: backtest.result.summary.winRate, trades: backtest.result.summary.trades } }
    : item;
  return <StrategyCard item={shown} state={state} contract={contract} monitor={monitor} backtesting={backtest.running} disabled={disabled}
    onOpen={onOpen} onBacktest={onBacktest}/>;
}

const SPREAD_RULE_NAMES: Record<string, string> = { 开多: "做多价差", 平多: "平掉做多价差", 开空: "做空价差", 平空: "平掉做空价差" };

/** The sentence under 当前信号: which rule group is satisfied now, or how far each one is. */
function signalSentence(plan: StrategyPlan | undefined, readings: ConditionReading[] | undefined): string | null {
  if (!plan || !readings?.length) return null;
  const parts: string[] = [];
  const met: string[] = [];
  for (const key of ["entryLong", "entryShort", "exitLong", "exitShort"] as const) {
    const rows = readings.filter(row => row.group === key);
    if (!rows.length) continue;
    const passed = rows.filter(row => row.status === "passed").length;
    const group = plan.rules[key];
    if (group && !group.sequence && group.join !== "all" && !group.alignmentConditions?.length ? passed > 0 : passed === rows.length) met.push(ruleNames[key]);
    else parts.push(`${ruleNames[key]} ${passed}/${rows.length}`);
  }
  if (met.length) return `${met.map(name => plan.spread ? SPREAD_RULE_NAMES[name] ?? name : name).join("、")}条件满足`;
  return parts.length ? `等待（${parts.join(" · ")}）` : null;
}

const HOLDING = { long: "多", short: "空" } as const;
const isReminder = (item: SavedStrategy) => Boolean(item.definition.plan?.reminderOnly);
const MODE_WORDS = { alert: "信号来了只提醒", confirm: "信号来了先出卡等你确认", auto: "信号来了自动下单" } as const;

/** One 正在运行 instance: today's figures, the 今日判断 clash, its latest signals and actions. */
function RunningCard({ run, library, baseUrl, scope, instruments, autonomy, busy, stage, logOpen, onLogChange, onPause, onResume, onResumeDrawdown, onStop, onSignalAction, onAskAi, explainBlocked }: {
  run: SavedStrategy; library: SavedStrategy; baseUrl: string; scope?: string; instruments?: InstrumentCatalog; autonomy?: boolean;
  busy: boolean; stage: string; logOpen: boolean; onLogChange: (open: boolean) => void; onPause: () => void; onResume: () => void; onResumeDrawdown: () => void;
  onStop: (close: boolean) => void; onSignalAction: (action: SignalAction) => void; onAskAi?: AskAi; explainBlocked?: string | null;
}) {
  const [monitor, setMonitor] = useState<StrategyMonitorSnapshot>();
  const [unavailable, setUnavailable] = useState(false);
  const [readings, setReadings] = useState<ConditionReading[]>();
  useDisclosureSurface("strategy-run-log", { id: run.strategyId, state: logOpen, apply: onLogChange });
  // 停止并平仓 asks first (`disclosure: strategy-stop-confirm` with the instance id).
  const [stopping, setStopping] = useDisclosureSurface("strategy-stop-confirm", { id: run.strategyId });
  useEffect(() => {
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const state = await getStrategyMonitor(baseUrl, run.strategyId);
        if (cancelled) return;
        setMonitor(state); setUnavailable(false);
        try {
          const detail = await requestJson<{ conditions: ConditionReading[] }>(`${baseUrl}/api/strategy-plans/${run.strategyId}/conditions`);
          if (!cancelled) setReadings(detail?.conditions);
        } catch { if (!cancelled) setReadings(undefined); }
      } catch { if (!cancelled) setUnavailable(true); }
      finally { if (!cancelled) timer = setTimeout(poll, 3000); }
    };
    void poll(); return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl, run.strategyId, run.signalAction]);
  const status = monitor?.status || run.status;
  const display = monitorDisplay({ status, decision: monitor?.decision, monitorState: monitor?.monitorState }, unavailable);
  const plan = library.definition.plan ?? run.definition.plan;
  // 今日 (2026-09-25 fix): only the current trading day's figures; an older day's state shows —.
  const today = monitor?.today ?? null;
  const todayPnl = Number(today?.pnl);
  const hasPnl = today != null && today.pnl != null && Number.isFinite(todayPnl);
  const attempts = monitor?.recentAttempts ?? [];
  const signal = status === "monitoring" ? signalSentence(plan, readings) : null;
  const contract = contractLabel(run.symbol, instruments);
  const name = `${strategyText(library.name)} · ${contract}`;
  const running = status === "monitoring";
  const signalAction = monitor?.signalAction ?? run.signalAction ?? "auto";
  const effective = monitor?.effectiveSignalAction ?? (signalAction === "auto" && autonomy === false ? "confirm" : signalAction);
  const position = monitor?.position;
  // 回撤停机 triggered (平仓处理中 / 已停止): only 恢复运行 (with a new 起始) brings it back, never the plain 继续.
  const drawdown = monitor?.drawdown;
  const halted = drawdown?.status === "liquidating" || drawdown?.status === "stopped";
  // The snapshot lists the instance. Its 执行方式 is a trading control: switched by the menu or the AI's
  // start_strategy_monitoring, never by a UI command (#932).
  useUiSurface(`strategyInstance:${run.strategyId}`, {
    state: { strategyId: run.strategyId, name, symbol: run.symbol, status, signalAction, effectiveSignalAction: effective,
      position: position ?? null, signalsToday: monitor?.signalsToday ?? null, researchClash: monitor?.researchClash?.text ?? null,
      // 按资金比例 / 回撤停机 as the Runtime reports them (absent = not used).
      funds: monitor?.funds ?? null,
      drawdown: drawdown && drawdown.status !== "inactive" ? { status: drawdown.status, drawdownPercent: drawdown.drawdownPercent ?? null,
        limitPercent: drawdown.limitPercent ?? null, canResume: Boolean(drawdown.canResume), reason: drawdown.reason ?? null,
        resumeBlockedReason: drawdown.resumeBlockedReason ?? null, feeSource: drawdown.feeSource ?? null } : null },
  }, [run.strategyId, name, status, signalAction, effective, position?.direction, position?.volume, monitor?.signalsToday, monitor?.researchClash?.text, monitor?.funds, drawdown]);
  const focus = { kind: "strategy_instance" as const, id: run.strategyId, label: name };
  const rows = attempts.slice(0, 3).map(attempt => ({ time: monitorTime(attempt.updatedAt).slice(-8, -3),
    text: `${actionText(attempt.action, attempt.volume, Boolean(plan?.spread))} · ${attemptStatusLabel(attempt.status, attempt.cardProposalIds?.length, attempt.signalAction)}` }));
  return <article className="sh-card strategy-run-card" data-state={running ? "running" : "paused"} data-testid="strategy-run-card" aria-label={name}>
    <span className="sh-card-top"><b>{strategyText(library.name)}</b><em className="sh-state" data-state={unavailable ? "draft" : running ? "running" : "paused"}>
      {unavailable ? "待确认" : running ? "运行中" : "已暂停"}</em><small className="sh-sym">{contract}</small></span>
    <div className="sh-score">
      <span>今天 <b className={hasPnl ? signClass(todayPnl) : ""}>{hasPnl ? formatSigned(todayPnl, 0) : "—"}</b></span>
      <span>持仓 <b>{position?.direction ? `${HOLDING[position.direction]} ${position.volume} 手` : "没有"}</b></span>
      <span title={SIGNAL_ACTIONS.find(item => item.value === effective)?.hint}>{MODE_WORDS[effective as keyof typeof MODE_WORDS] ?? "信号来了"}</span>
      <SignalActionMenu strategyId={run.strategyId} value={signalAction} effective={effective} autonomy={autonomy} busy={busy} onChange={onSignalAction}/>
    </div>
    <ClashLine clash={monitor?.researchClash}/>
    <FundsLines funds={monitor?.funds} unit={moneyUnit(run.symbol)}/>
    <DrawdownStatus drawdown={drawdown} unit={moneyUnit(run.symbol)} busy={busy} buttonClass="sh-btn primary" onResume={onResumeDrawdown}/>
    <p className="sh-hint" aria-label="当前信号">当前信号：{signal ?? display.detail}</p>
    <SignalRows rows={rows}/>
    <div className="sh-run-actions">
      <button type="button" className="sh-btn" disabled={!onAskAi || Boolean(explainBlocked)} title={explainBlocked ?? undefined}
        onClick={() => onAskAi?.("这个策略今天为什么这么做", focus)}>问 AI 它为什么这么做</button>
      <button type="button" className="sh-btn" aria-expanded={logOpen} onClick={() => onLogChange(!logOpen)}>运行日志</button>
      {halted ? null : running
        ? <button type="button" className="sh-btn" disabled={busy} onClick={onPause}>{busy && stage ? stage : "暂停"}</button>
        : <button type="button" className="sh-btn" disabled={busy} title={`在 ${contract} 上重新验证后继续`} onClick={onResume}>{busy && stage ? stage : "继续"}</button>}
      <button type="button" className="sh-btn danger" disabled={busy} aria-expanded={stopping} onClick={() => setStopping(value => !value)}>停止并平仓</button>
    </div>
    {stopping ? <div className="sh-stop-confirm" role="alertdialog" aria-label={`停止 ${name}`}>
      <p>停止后不再判断信号。{position?.direction ? `它还有 ${HOLDING[position.direction]} ${position.volume} 手，会在对话里出平仓卡，你确认了才平。` : "它还有持仓的话，会在对话里出平仓卡，你确认了才平。"}</p>
      <div><button type="button" className="sh-btn" disabled={busy} onClick={() => setStopping(false)}>取消</button>
        <button type="button" className="sh-btn" disabled={busy} onClick={() => { setStopping(false); onStop(false); }}>只停止，不平仓</button>
        <button type="button" className="sh-btn danger" disabled={busy} onClick={() => { setStopping(false); onStop(true); }}>确认停止并平仓</button></div>
    </div> : null}
    {logOpen && <AttemptLog attempts={attempts} spread={Boolean(plan?.spread)} storageKey={scope ? `ai-trader-attempts-hidden:${scope}:${baseUrl}:${run.strategyId}` : undefined}/>}
  </article>;
}

/** 运行日志: the instance's recent execution attempts; failed ones can be hidden (display only) and brought back. */
function AttemptLog({ attempts, storageKey, spread = false }: { attempts: StrategyMonitorSnapshot["recentAttempts"]; storageKey?: string; spread?: boolean }) {
  const [hidden, setHidden] = usePersistentDraft<Record<string, string>>(storageKey, {});
  const [history, setHistory] = useDisclosureSurface("attempt-history", { available: Object.keys(hidden).length > 0 });
  // A strategy card still waiting for confirmation (SKIPPED with cards) is not a failure.
  const failed = (attempt: typeof attempts[number]) => FAILED_ATTEMPTS.includes(attempt.status) && !(attempt.status === "SKIPPED" && (attempt.cardProposalIds?.length || attempt.signalAction === "alert"));
  const revision = (attempt: typeof attempts[number]) => `${attempt.status}:${attempt.updatedAt}`;
  const visible = attempts.filter(attempt => history || !failed(attempt) || hidden[attempt.attemptId] !== revision(attempt));
  return <section className="strategy-run-log" aria-label="运行日志">
    <div className="strategy-actions-inline">
      <button type="button" className="app-button danger" disabled={!visible.some(failed)} onClick={() => { setHidden(previous => ({...previous, ...Object.fromEntries(attempts.filter(failed).map(attempt => [attempt.attemptId, revision(attempt)]))})); setHistory(false); }}>清空失败记录</button>
      {Object.keys(hidden).length > 0 && <button type="button" onClick={() => setHistory(value => !value)}>{history ? "返回当前记录" : "查看历史记录"}</button>}
    </div>
    {visible.length ? visible.map(attempt => <p key={attempt.attemptId}><time>{monitorTime(attempt.updatedAt)}</time><br/>{actionText(attempt.action, attempt.volume, spread, " · ")} · {attemptStatusLabel(attempt.status, attempt.cardProposalIds?.length, attempt.signalAction)} · {attempt.detail ? strategyStatusText(attempt.detail) : "等待执行回报"}</p>) : <p>暂无执行记录</p>}
  </section>;
}

