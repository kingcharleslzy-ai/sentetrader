import { useSyncExternalStore } from "react";
import type { StrategyHealthGroup } from "@ai-trader/ui-events";
import { requestJson, type StrategyFunds } from "./api";
import { LONG_READ_TIMEOUT_MS } from "./requestTimeout";
import { activeTradingAccount, onTradingAccountChange } from "./tradingAccountScope";
import { backtestError, type StrategyBacktestResult } from "./strategyBacktests";
import { prewarmEngine, ruleBacktest, StaleEngine } from "./strategyEngine/browserEngine";
import { designerPlanProblem, previewStrategyFunds, requestStatus, strategiesChanged, strategyError, strategyQuestions, strategyScope, type Clarification, type SavedStrategy, type StrategyPlan } from "./strategyApi";
import { advancedRules, applyPrefill, changedFields, DEFAULT_NAME, emptyForm, FIELD_NAMES, fieldDiffers, formFromPlan, hitsOf, maxPositionOf, missingParts,
  planFromForm, DEFAULTS, REMINDER_NEW, rowLabel, rowModifiers, type EditorForm, type ReminderOptions, type SignalChoice, type FieldName, type GroupForm, type PrefillResponse } from "./strategyEditor";
import { FOCUS_TARGETS, strategyFlow, type FocusTarget, type StrategyFlowModel } from "./strategyFlow";

/**
 * 策略编辑卡's state for the window (2026-10-08): one draft at a time, kept when the card is only hidden
 * (‹ 我的策略) and dropped by 放弃 / close or a save. The card, the `strategyEditor` UI command and the
 * workbench share this copy, so a chart contract switch (which remounts the strategy panel) keeps the
 * draft. The automatic backtest, the AI 补一句 job, the prefill and the template load run from here;
 * every one of them carries the draft's generation and a late answer for an older draft is dropped.
 */
export type EditorStart = "blank" | "habits" | `template:${string}`;
type Diagnosis = StrategyHealthGroup & { join?: "all" | "any"; anyPass?: number };
export type PreviewResult = StrategyBacktestResult & { planSummary?: string; diagnosis?: Partial<Record<"entryLong" | "entryShort", Diagnosis>> };
export type BacktestFigures = { trades: number; winRate: number | null; netProfit: number; maxDrawdown: number };
export interface EditorBacktest {
  status: "idle" | "waiting" | "running" | "done" | "error";
  result?: PreviewResult; key?: string; error?: string; at?: number;
  /** The result shown before the last change, for 上次 ↑↓. */
  previous: BacktestFigures | null;
}
/** The basis of visible cached figures, shared by the full block and fixed summaries. */
export function backtestProgress(backtest: EditorBacktest): string {
  const old = Boolean(backtest.result);
  if (backtest.status === "error") return old ? "重算失败，以下为上次结果" : "回测没算出来";
  if (backtest.status === "running") return old ? "上次结果，正在重算…" : "回测中…";
  if (backtest.status === "waiting") return old ? "上次结果，等待重算…" : "改动后自动重算…";
  return "";
}

/**
 * 按资金比例's figures for this form (the Runtime's own calculation); null = not asked / not available. `key` names the
 * draft, account, Runtime, strategy, contract, percentage and cap they were asked for; `at` is when the Runtime answered
 * (an estimate of that moment: an actual open is worked out again when it is sent).
 */
export interface EditorFunds { status: "loading" | "done" | "error"; key: string; data?: StrategyFunds; error?: string; at?: number }
export interface EditorAsk { text: string; status: "running" | "done" | "failed" | "questions"; stage?: string; error?: string }
export interface EditorState {
  /** Changes with every new draft: the card remounts, so its folds start closed. */
  draftId: number;
  open: boolean;
  /** A draft exists (shown or hidden). */
  draft: boolean;
  mode: "new" | "edit";
  strategyId: string | null;
  item: SavedStrategy | null;
  start: EditorStart;
  /** What is loading right now (成交 / 模板), shown instead of the form's 起点 hint. */
  loading: string | null;
  base: StrategyPlan | null;
  /** The form as it was loaded: an edit with no change needs no save before 上线. */
  initial: EditorForm;
  form: EditorForm;
  templateId: string | null;
  /** 按我的习惯: the Runtime's answer (null until asked or when it could not be read). */
  prefill: PrefillResponse | null;
  basis: Partial<Record<FieldName, string>>;
  symbolLabel: string | null;
  aiFields: FieldName[];
  /** Fields the last 补一句 changed: highlighted once, cleared by the next edit. */
  changed: FieldName[];
  ask: EditorAsk | null;
  questions: Clarification[];
  answers: Record<string, string>;
  history: { question: string; answer: string }[];
  backtest: EditorBacktest;
  funds: EditorFunds | null;
  saving: boolean;
  savedStrategyId: string | null;
  error: string | null;
  /** The last 流程图 block or `strategyEditor {focus}`: the form scrolls there and lights it briefly (`seq` re-runs it). */
  focus: { target: FocusTarget; seq: number } | null;
  /** 提醒模式 (2026-10-25 第 6 批): a 我的提醒 entry — only conditions and 时段; saved with `plan.reminderOnly`. */
  reminder: boolean;
}
export interface EditorContext { rootUrl: string; chartSymbol: string; chartTimeframe: number }

export const BACKTEST_DEBOUNCE_MS = 1500;
export const DESIGN_POLL_MS = 800;
export const PREVIEW_TRADING_DAYS = 60;

const idle = (): EditorBacktest => ({ status: "idle", previous: null });
const closed = (): EditorState => ({ draftId: 0, open: false, draft: false, mode: "new", strategyId: null, item: null, start: "blank", loading: null,
  base: null, initial: emptyForm("", 300), form: emptyForm("", 300), templateId: null, prefill: null, basis: {}, symbolLabel: null,
  aiFields: [], changed: [], ask: null, questions: [], answers: {}, history: [], backtest: idle(), funds: null, saving: false, savedStrategyId: null, error: null, focus: null, reminder: false });

let state: EditorState = closed();
let context: EditorContext = { rootUrl: "", chartSymbol: "", chartTimeframe: 300 };
/** Every new draft, 起点 or discard: async answers for an older one are dropped. */
let generation = 0;
let formRevision = 0;
let contextRevision = 0;
let fundsSequence = 0;
let askGeneration = 0;
/** The latest `edit` waiting for the host's strategy list (a newer one drops the older). */
let editTicket = 0;
let askJob: string | null = null;
let timer: ReturnType<typeof setTimeout> | undefined;
let fundsTimer: ReturnType<typeof setTimeout> | undefined;
let inFlight = false;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
function set(change: Partial<EditorState>) {
  if ((change.form && change.form !== state.form) || (change.base && change.base !== state.base)
      || (change.answers && change.answers !== state.answers)) formRevision += 1;
  state = { ...state, ...change }; notify();
}

export function editorState(): EditorState { return state; }
export function useStrategyEditor(): EditorState { return useSyncExternalStore(subscribe, editorState, editorState); }
/** The workbench tells the store where to send requests and what the chart shows. */
export function setEditorContext(next: EditorContext) {
  const moved = next.rootUrl !== context.rootUrl;
  context = next;
  // Called while the host renders: the estimate for the other Runtime is dropped right after.
  if (moved) {
    contextRevision += 1;
    fundsSequence += 1; // Retire the old Runtime even if it is selected again before the microtask.
    queueMicrotask(() => { scheduleFunds(true); schedule(); });
  }
}
/** 网页新版本 waits while a card is open or saving (a reload would drop the draft). */
export function editorBusy() { return state.open || state.saving; }

const newJobId = () => (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function" ? crypto.randomUUID()
  : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`).replace(/-/g, "").padEnd(32, "0").slice(0, 32);
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

function cancelAsk() {
  askGeneration += 1;
  if (askJob && context.rootUrl) void requestJson(`${context.rootUrl}/api/strategy-plans/design-jobs/${askJob}`, { method: "DELETE" }).catch(() => undefined);
  askJob = null;
}

function fresh(mode: "new" | "edit", form: EditorForm, extra: Partial<EditorState> = {}): EditorState {
  return { ...closed(), draftId: generation, open: true, draft: true, mode, form, initial: form, ...extra };
}

// -- opening ---------------------------------------------------------------------------------------

/** 新建: blank with the chart's contract and period; then 按我的习惯 when the trades allow it. `reminder`: 新建提醒
 *  (提醒模式) — blank, named after a 打法 when one is given, its entry rules sent as the first 补一句. */
export function newDraft(start?: EditorStart, reminder?: { name?: string; ask?: string }) {
  cancelAsk(); clearTimeout(timer); clearTimeout(fundsTimer);
  generation += 1;
  const blank = emptyForm(context.chartSymbol, context.chartTimeframe);
  // A 提醒 asks no stop: the plan keeps the default ticks, which an alert-only run never uses. It asks no 方向 either
  // (改版第一步): one condition group, kept on the long side, and 常用 offers the down forms too.
  state = fresh("new", reminder ? { ...blank, name: reminder.name ?? "", direction: "long", stop: { ...blank.stop, value: DEFAULTS.stopLossTicks },
    reminderOptions: { ...REMINDER_NEW } } : blank, { reminder: Boolean(reminder) });
  notify();
  if (!reminder) { void chooseStart(start ?? "default"); return; }
  schedule();
  if (reminder.ask?.trim()) askEditor(reminder.ask.trim());
}

/** 编辑: the strategy's plan in the card; what the card cannot show is kept as it is. */
export function editDraft(item: SavedStrategy, symbol: string) {
  const plan = item.definition.plan;
  if (!plan) throw new Error("这条策略没有可编辑的规则");
  cancelAsk(); clearTimeout(timer); clearTimeout(fundsTimer);
  generation += 1;
  const form = formFromPlan(plan, symbol);
  const questions = plan.questions.length || plan.clarifications?.length ? strategyQuestions(plan) : [];
  state = fresh("edit", form, { strategyId: item.strategyId, item, base: plan, questions, reminder: Boolean(plan.reminderOnly),
    templateId: item.definition.templateSource?.templateId ?? null });
  notify();
  schedule();
}

/** 打开: the hidden draft again, or a new one. */
export function openDraft() {
  editTicket += 1;
  if (state.draft) { set({ open: true }); schedule(); return; }
  newDraft();
}
/** A refusal from a button (the command path throws it to the receipt instead). */
export function setEditorError(error: unknown) { if (state.draft) set({ error: error instanceof Error ? error.message : String(error) }); }
/** ‹ 我的策略: hide the card, keep the draft. */
export function hideDraft() {
  editTicket += 1;
  if (!state.open) return;
  if (!draftEdited()) { discardDraft(); return; }
  set({ open: false });
}
/**
 * Whether leaving keeps a draft (2026-10-08, the owner: a look without a change is not a saved draft):
 * a field the user changed since the form was loaded (起点 / 按我的习惯's prefill are the loaded form, not
 * changes), or a 补一句 sent or still running.
 */
export function draftEdited(current: EditorState = state) {
  return changedFields(current.initial, current.form).length > 0 || current.ask !== null || (current === state && askJob !== null);
}
/** 放弃 / close: drop the draft and anything still running for it. */
export function discardDraft() {
  cancelAsk(); clearTimeout(timer); clearTimeout(fundsTimer);
  generation += 1;
  // The strategy a save just stored stays in the closed card's snapshot: the hosts close the card right after a
  // save, and `strategyEditor {save}`'s receipt is judged by it (a fast save used to leave no trace: unknown).
  state = { ...closed(), savedStrategyId: state.savedStrategyId };
  notify();
}

async function chooseStart(start: EditorStart | "default") {
  const mine = generation;
  const blank = () => emptyForm(state.form.symbol || context.chartSymbol, state.form.timeframe || context.chartTimeframe);
  if (start === "blank") {
    set({ start: "blank", form: blank(), initial: blank(), base: null, templateId: null, aiFields: [], basis: {}, symbolLabel: null, changed: [], loading: null });
    schedule(); return;
  }
  if (start.startsWith("template:")) {
    const id = start.slice("template:".length);
    set({ loading: "正在载入模板…", error: null });
    try {
      const template = await requestJson<{ plan: StrategyPlan; name?: string }>(`${context.rootUrl}/api/strategy-templates/${encodeURIComponent(id)}`);
      if (mine !== generation) return;
      const form = formFromPlan(template.plan, state.form.symbol || context.chartSymbol);
      set({ start: start as EditorStart, form, initial: form, base: template.plan, templateId: id, aiFields: [], basis: {}, symbolLabel: null, changed: [], loading: null });
      schedule();
    } catch (error) {
      if (mine === generation) set({ loading: null, error: requestStatus(error) === 404 ? "没有这个模板" : strategyError(error, "模板没载入，请再试一次") });
    }
    return;
  }
  // habits, or the default of a new draft: read the trades; too few (or unreadable) opens blank.
  set({ loading: "正在读你的成交…" });
  let prefill: PrefillResponse | null;
  try { prefill = await requestJson<PrefillResponse>(`${context.rootUrl}/api/strategy-plans/prefill`, { timeoutMs: LONG_READ_TIMEOUT_MS }); }
  catch (error) {
    prefill = requestStatus(error) === 409 ? { eligible: false, reason: "成交记录暂时读不到，先从空白或模板开始。", fields: {}, trades: [] } : null;
  }
  if (mine !== generation) return;
  if (state.ask) {
    // 补一句 already took the form (new + ask in one command): keep it, only record what the trades allow.
    set({ prefill: prefill ?? { eligible: false, reason: "成交记录暂时读不到，先从空白或模板开始。", fields: {}, trades: [] },
      start: prefill?.eligible ? "habits" : "blank", loading: null });
    schedule(); return;
  }
  if (!prefill?.eligible) {
    const form = blank();
    set({ prefill: prefill ?? { eligible: false, reason: "成交记录暂时读不到，先从空白或模板开始。", fields: {}, trades: [] },
      start: "blank", form, initial: form, base: null, templateId: null, aiFields: [], basis: {}, symbolLabel: null, loading: null });
    schedule(); return;
  }
  const filled = applyPrefill(blank(), prefill);
  set({ prefill, start: "habits", form: filled.form, initial: filled.form, base: null, templateId: null, aiFields: filled.aiFields,
    basis: filled.basis, symbolLabel: filled.symbolLabel ?? null, changed: [], loading: null });
  schedule();
}

/** 起点: replaces the form (空白 / 从模板 / 按我的习惯). */
export function setStart(start: EditorStart) {
  if (!state.draft) throw new Error("先打开编辑卡");
  cancelAsk(); clearTimeout(fundsTimer);
  generation += 1;
  set({ ask: null, questions: [], answers: {}, history: [], error: null, funds: null });
  void chooseStart(start);
}

/** The `strategyEditor` UI command's keys. */
export type EditorChoice = Partial<ReminderOptions> & { signalAction?: SignalChoice };
export interface EditorChange { open?: boolean; new?: boolean; reminder?: boolean; edit?: string; start?: EditorStart; answers?: Record<string, string>; ask?: string;
  focus?: FocusTarget; choose?: EditorChoice; save?: boolean; close?: boolean }

/** 点选 (改版第一步): 提醒的响几次 / 到期 / 整张自选表, 策略的信号来了怎么办 — the card's own buttons and `choose`.
 *  `autoBlocked`: why 自动下单 cannot be the default now (设置 → 交易 → AI 自主执行 off). */
export function chooseEditor(choice: EditorChoice, autoBlocked?: string | null) {
  if (!state.open) throw new Error("先打开编辑卡");
  const { signalAction, ...reminder } = choice;
  const reminderKeys = Object.keys(reminder).filter(key => reminder[key as keyof typeof reminder] != null);
  if (reminderKeys.length && !state.reminder) throw new Error("响几次、到期和自选表只用于提醒");
  if (signalAction != null && state.reminder) throw new Error("提醒只提醒，不能选执行方式");
  if (signalAction === "auto" && autoBlocked) throw new Error(autoBlocked);
  const fields: FieldName[] = [...(reminderKeys.length ? ["reminderOptions" as const] : []), ...(signalAction != null ? ["signalAction" as const] : [])];
  editFields(fields, form => ({ ...form,
    ...(reminderKeys.length ? { reminderOptions: { ...form.reminderOptions, ...Object.fromEntries(reminderKeys.map(key => [key, reminder[key as keyof typeof reminder]])) } } : {}),
    ...(signalAction != null ? { signalAction } : {}) }));
}

/**
 * The `strategyEditor` UI command, the same for the desktop panel's card and the phone's full-screen page
 * (2026-10-08): close → open / new / edit → start → answers → ask → focus → save. `edit` (it needs the host's
 * strategy list) and `save` (what follows a save differs) are the host's.
 */
export function applyEditorChange(change: EditorChange, host: { edit: (strategyId: string) => void | Promise<boolean>; save: () => void; autoBlocked?: string | null }) {
  if (change.close) { discardDraft(); return; }
  if (change.focus !== undefined && !FOCUS_TARGETS.includes(change.focus)) throw new Error(`没有这一块：${change.focus}`);
  const rest = () => {
    if (change.start !== undefined && !change.new) { if (state.draft) setStart(change.start); else newDraft(change.start); }
    if (change.answers) answerEditor(change.answers);
    if (change.ask !== undefined) askEditor(change.ask);
    if (change.focus !== undefined) focusEditor(change.focus);
    if (change.choose) chooseEditor(change.choose, host.autoBlocked);
    if (change.save) host.save();
  };
  if (change.edit !== undefined) {
    const loading = host.edit(change.edit);
    // A strategy the host is still reading: the rest of the command waits for its card, and is dropped if it never opens.
    if (loading) { void loading.then(opened => { if (opened) { try { rest(); } catch (error) { setEditorError(error); } } }); return; }
  } else if (change.new) newDraft(change.start, change.reminder ? {} : undefined);
  else if (change.open) openDraft();
  rest();
}

/**
 * For an `edit` whose strategy the host must read first: whether its answer may still open the card. A newer
 * `edit`, a new / other draft, 放弃, or the card opened / hidden meanwhile makes it late, and a late answer is dropped.
 */
export function pendingEdit(): () => boolean {
  const ticket = ++editTicket, mine = generation, open = state.open;
  return () => ticket === editTicket && mine === generation && open === state.open;
}

/** 流程图 → 表单: scroll the form to the field a block stands for and light it briefly (the card and the phone page follow `focus`). */
export function focusEditor(target: FocusTarget) {
  if (!state.open) throw new Error("先打开编辑卡");
  set({ focus: { target, seq: (state.focus?.seq ?? 0) + 1 } });
}

// -- editing ---------------------------------------------------------------------------------------

/** A user edit of the named fields: their AI mark goes, the 补一句 highlight clears, the backtest waits. */
export function editFields(fields: FieldName[], change: (form: EditorForm) => EditorForm) {
  if (!state.draft) return;
  set({ form: change(state.form), aiFields: state.aiFields.filter(field => !fields.includes(field)), changed: [], error: null });
  schedule();
}

// -- the automatic backtest ------------------------------------------------------------------------

/** The plan as it would be saved now (the description aside). */
export function currentPlan(description?: string): StrategyPlan {
  const plan = planFromForm(state.form, state.base, description);
  return state.reminder ? { ...plan, reminderOnly: true, reminder: { ...state.form.reminderOptions } } : plan;
}

function backtestRequest() {
  const plan = currentPlan();
  // The Runtime binds every follow-the-chart clock to `timeframe`: always send the period the card shows.
  const timeframe = plan.timeframeSeconds || context.chartTimeframe;
  const keyPlan: Partial<StrategyPlan> = { ...plan, description: "" };
  return { plan, timeframe, key: JSON.stringify([context.rootUrl, activeTradingAccount(), keyPlan, state.form.symbol, timeframe]) };
}
const figures = (result: PreviewResult): BacktestFigures => ({ trades: result.summary.trades, winRate: result.summary.winRate,
  netProfit: result.summary.netProfit, maxDrawdown: result.summary.maxDrawdown });

/** Whether the result on the card is this form's (the key is written only with a finished result). */
function shownIsCurrent(key = backtestRequest().key) { return Boolean(state.backtest.result) && state.backtest.key === key; }

function schedule() {
  clearTimeout(timer);
  timer = undefined;
  if (!state.open || !state.draft || state.loading) return;
  scheduleFunds();
  prewarmEngine(context.rootUrl);   // 本机优先: the engine loads while the form is being filled
  if (missingParts(state.form).length) {
    if (state.backtest.status !== "idle" && !inFlight) set({ backtest: { ...state.backtest, status: "idle" } });
    return;
  }
  if (shownIsCurrent()) {
    // Back to the plan whose result is on the card (a draft reopened, 20 → 21 → 20): no new request, no 上次 flicker.
    if (!inFlight && state.backtest.status !== "done") set({ backtest: { ...state.backtest, status: "done", error: undefined } });
    return;
  }
  if (!inFlight) set({ backtest: { ...state.backtest, status: "waiting", error: undefined } });
  timer = setTimeout(() => { timer = undefined; void runBacktest(); }, BACKTEST_DEBOUNCE_MS);
}

/** One request in flight at a time; edits while it runs wait for it and then run the latest. */
async function runBacktest() {
  if (inFlight || !state.open || !state.draft || state.loading || missingParts(state.form).length) return;
  const mine = generation;
  const scope = contextRevision;
  const request = backtestRequest();
  if (shownIsCurrent(request.key)) { if (state.backtest.status !== "done") set({ backtest: { ...state.backtest, status: "done", error: undefined } }); return; }
  const problem = designerPlanProblem(request.plan);
  if (problem) { set({ backtest: { ...state.backtest, status: "error", error: problem } }); return; }
  inFlight = true;
  set({ backtest: { ...state.backtest, status: "running", error: undefined } });
  let result: PreviewResult | undefined;
  let failure: string | undefined;
  const rootUrl = context.rootUrl;
  const body = { plan: request.plan, symbol: state.form.symbol, timeframe: request.timeframe, tradingDays: PREVIEW_TRADING_DAYS, preview: true };
  try {
    // 本机优先 (2026-10-12): computed in this browser when it can, else the Runtime's queue (with the reason).
    result = await ruleBacktest<PreviewResult>(rootUrl, body, fallbackReason => requestJson<PreviewResult>(`${rootUrl}/api/strategy-backtests`,
      { method: "POST", body: JSON.stringify({ ...body, requestId: newJobId(), fallbackReason }) }));
  } catch (error) {
    const status = requestStatus(error);
    failure = error instanceof StaleEngine ? error.message : status === 429 ? "回测排队的人多，稍后自动再试" : backtestError(error);
  } finally { inFlight = false; }
  if (mine !== generation || scope !== contextRevision || !state.draft) {
    // The old request owns the single slot until it finishes; hand it to the current draft.
    if (!timer) void runBacktest();
    return;
  }
  const latest = backtestRequest().key;
  if (latest !== request.key) {
    // The form changed while this ran: its result is not the form's any more.
    if (!timer && !missingParts(state.form).length) void runBacktest();
    return;
  }
  if (failure !== undefined) { set({ backtest: { ...state.backtest, status: "error", error: failure } }); return; }
  const shown = state.backtest.result;
  set({ backtest: { status: "done", result, key: request.key, at: Date.now(),
    previous: shown && state.backtest.key !== request.key ? figures(shown) : state.backtest.previous } });
}

// -- 按资金比例: the Runtime's figures for this form ---------------------------------------------------

/** What the estimate is asked for, and its key; null = not 按资金比例 or the percentage / cap is not valid yet. */
function fundsRequest(current: EditorState = state) {
  const { sizing, symbol } = current.form;
  const percent = sizing.equityPercent, maxLots = sizing.maxLots;
  if (!current.draft || sizing.mode !== "equity_percent" || !symbol || !context.rootUrl || percent == null || !(percent > 0 && percent <= 100)
    || (maxLots != null && !(Number.isInteger(maxLots) && maxLots >= 1))) return null;
  const query = { symbol, equityPercent: percent, maxLots, strategyId: current.strategyId };
  return { query, key: JSON.stringify([generation, activeTradingAccount(), context.rootUrl, current.strategyId, symbol, percent, maxLots]) };
}

/**
 * Ask once the percentage (cap, contract) settles. A changed form, draft, account or Runtime drops the old figures at
 * once (they never stand for the new ones); the timer and the answer both check the key is still this form's.
 */
function scheduleFunds(force = false) {
  const request = fundsRequest();
  if (!force && request && state.funds?.key === request.key) return;
  const sequence = ++fundsSequence;
  clearTimeout(fundsTimer);
  fundsTimer = undefined;
  if (!request) {
    if (state.funds) set({ funds: null });
    return;
  }
  const { key, query } = request;
  set({ funds: { status: "loading", key } });
  const current = () => sequence === fundsSequence && fundsRequest()?.key === key && state.funds?.key === key;
  fundsTimer = setTimeout(() => {
    fundsTimer = undefined;
    if (!current()) return;
    previewStrategyFunds(context.rootUrl, query).then(
      data => { if (current()) set({ funds: { status: "done", key, data, at: Date.now() } }); },
      error => { if (current()) set({ funds: { status: "error", key, error: strategyError(error, "按当前账户的额度暂时算不出") } }); });
  }, BACKTEST_DEBOUNCE_MS);
}
// Another account: its estimate replaces the previous account's (the draft itself stays).
onTradingAccountChange(() => {
  contextRevision += 1;
  fundsSequence += 1;
  if (state.draft) { scheduleFunds(true); schedule(); }
});

// -- 补一句 and its questions ------------------------------------------------------------------------

function formIsEmpty(form: EditorForm) {
  return changedFields(emptyForm(form.symbol, form.timeframe || context.chartTimeframe), form).every(field => field === "symbol" || field === "timeframe" || field === "name");
}

/** 补一句: an empty form gets a whole plan from the sentence, a filled one only the related fields. */
export function askEditor(text: string) {
  const sentence = text.trim();
  if (!state.draft) throw new Error("先打开编辑卡");
  if (!sentence) throw new Error("先说一句要改什么");
  void design(sentence, [], 0, formIsEmpty(state.form) ? undefined : currentPlan());
}

/** Answers to the questions; when every question has one, the plan is designed again with them. */
export function answerEditor(answers: Record<string, string>) {
  if (!state.questions.length) throw new Error("现在没有要回答的问题");
  for (const id of Object.keys(answers)) if (!state.questions.some(question => question.id === id)) throw new Error(`没有这个问题：${id}`);
  const merged = { ...state.answers, ...answers };
  set({ answers: merged, ask: state.ask ?? { text: state.base?.description ?? "", status: "questions" } });
  if (state.questions.every(question => merged[question.id]?.trim())) regenerate();
}
export function setEditorAnswer(id: string, answer: string) { set({ answers: { ...state.answers, [id]: answer } }); }

/** 回答并完善策略: the answered round goes to a new job with the plan so far. */
export function regenerate() {
  const missing = state.questions.find(question => !state.answers[question.id]?.trim());
  if (missing) throw new Error(`还没回答：${missing.question}`);
  const round = state.questions.map(question => {
    const answer = state.answers[question.id].trim();
    const option = question.options.find(item => item.label === answer);
    return { question: question.question, answer: option ? `${option.label}：${option.description}` : answer };
  });
  const description = state.ask?.text || state.base?.description || "";
  void design(description, [...state.history, ...round], rounds + 1, currentPlan());
}

let rounds = 0;
async function design(description: string, history: { question: string; answer: string }[], round: number, previousPlan?: StrategyPlan) {
  cancelAsk();
  formRevision += 1; // Sending a new request is an edit even before its AI result arrives.
  rounds = round;
  const mine = ++askGeneration;
  const draft = generation;
  const live = () => mine === askGeneration && draft === generation;
  const id = newJobId();
  askJob = id;
  set({ ask: { text: description, status: "running", stage: "正在发送" }, questions: [], history, error: null });
  type Job = { id: string; status: string; stage?: string; result?: StrategyPlan; error?: { message?: string } };
  try {
    // No direction / maxPosition / maxLoss here: the server would treat them as limits the user set and
    // let them override the plan; the sentence and the form decide.
    let reply = await requestJson<Job>(`${context.rootUrl}/api/strategy-plans/design-jobs`, { method: "POST", body: JSON.stringify({
      id, description, constraints: { clarificationRound: round, clarificationAnswers: history,
        ...(previousPlan ? { previousPlan, editorRevision: true } : {}) } }) });
    while (reply.status === "running") {
      if (!live()) return;
      set({ ask: { text: description, status: "running", stage: reply.stage ?? "AI 正在改" } });
      await delay(DESIGN_POLL_MS);
      if (!live()) return;
      reply = await requestJson<Job>(`${context.rootUrl}/api/strategy-plans/design-jobs/${id}`);
    }
    if (!live()) return;
    askJob = null;
    if (reply.status !== "completed" || !reply.result) {
      set({ ask: { text: description, status: "failed", error: reply.error?.message ?? reply.stage ?? "这次没有改成，换个说法再试一次" } });
      return;
    }
    const plan = reply.result;
    const questions = plan.questions.length || plan.clarifications?.length ? strategyQuestions(plan) : [];
    applyDesigned(plan, previousPlan);
    if (questions.length) set({ ask: { text: description, status: "questions" }, questions, answers: {} });
  } catch (error) {
    if (live()) { askJob = null; set({ ask: { text: description, status: "failed", error: strategyError(error, "这次没有改成，请检查连接后再试") } }); }
  }
}

/** The designed plan into the form (not saved): what it changed is highlighted once. */
function applyDesigned(plan: StrategyPlan, sent?: StrategyPlan) {
  const proposed = formFromPlan(plan, state.form.symbol);
  // 响几次 / 到期 / 自选表 and 信号来了怎么办 are the user's picks: the designer never writes them.
  proposed.reminderOptions = state.form.reminderOptions; proposed.signalAction = state.form.signalAction;
  if (!plan.timeframeSeconds && state.form.timeframe) proposed.timeframe = state.form.timeframe;
  // 补一句 on a filled form: only what the designer actually changed against the plan it was sent lands on
  // the form — a field it merely echoed (an empty field's default, a value it kept) stays as the user left it.
  const next = sent ? revisedForm(proposed, formFromPlan(sent, state.form.symbol)) : proposed;
  const changed: FieldName[] = changedFields(state.form, next).filter(field => field !== "symbol");
  set({ base: plan, form: next, changed, aiFields: state.aiFields.filter(field => !changed.includes(field)),
    ask: state.ask && { ...state.ask, status: "done", stage: undefined } });
  schedule();
}

/** The form after a revision: the designer's changes against what it was sent, the rest as it was. A new
 * name is taken only while the strategy still has the default one. */
function revisedForm(proposed: EditorForm, sent: EditorForm): EditorForm {
  const next = { ...state.form } as Record<FieldName, unknown>;
  for (const name of FIELD_NAMES) {
    if (name === "name" && state.form.name.trim() && state.form.name !== DEFAULT_NAME) continue;
    if (fieldDiffers(name, proposed, sent)) next[name] = proposed[name];
  }
  return next as EditorForm;
}

// -- saving ----------------------------------------------------------------------------------------

/** Whether 上线运行 needs a save first: a new strategy always, an edit once something changed. */
export function needsSave() { return state.mode === "new" || changedFields(state.initial, state.form).length > 0 || Boolean(state.base !== state.item?.definition.plan); }

/** A new strategy's name that no saved one has: “名字 (2)”, “名字 (3)” … (the server's unique_strategy_name). */
export function uniqueName(name: string, taken: string[]): string {
  const names = new Set(taken);
  if (!names.has(name)) return name;
  for (let index = 2; ; index += 1) if (!names.has(`${name} (${index})`)) return `${name} (${index})`;
}

/**
 * 存为草稿: refuses (throws) what the server would refuse; a new version when editing. A new strategy
 * avoids the names in 我的策略 itself (`takenNames`): without a template the server saves a same-named
 * plan as that strategy's next version. `current` is false when the draft was thrown away or replaced
 * or edited/hidden before the answer came (the caller then only refreshes the list).
 */
export function saveDraft(takenNames: string[] = []): Promise<{ item: SavedStrategy; current: boolean }> {
  const missing = missingParts(state.form);
  if (missing.length) throw new Error(`还没填好：${missing.join("、")}`);
  const summary = state.backtest.status === "done" && state.backtest.key === backtestRequest().key ? state.backtest.result?.planSummary : undefined;
  const edited = state.mode === "edit" && !needsSave();
  const description = edited ? undefined : summary ? `策略编辑卡：${summary}` : state.mode === "edit" ? state.base?.description
    : `策略编辑卡：${state.form.name.trim() || "未命名策略"}`;
  const plan = currentPlan(description);
  const fromTemplate = state.mode === "new" && Boolean(state.templateId) && state.start.startsWith("template:");
  if (state.mode === "new" && !fromTemplate) plan.name = uniqueName(plan.name, takenNames);
  const problem = designerPlanProblem(plan);
  if (problem) throw new Error(problem);
  if (state.saving) throw new Error("正在保存");
  const mine = generation;
  const revision = formRevision, scope = contextRevision, ticket = editTicket;
  const item = state.item, sentForm = state.form, sentBase = state.base, rootUrl = context.rootUrl;
  set({ saving: true, error: null });
  return (async () => {
    try {
      const saved = await requestJson<SavedStrategy>(`${rootUrl}/api/strategy-plans`, { method: "POST", body: JSON.stringify({
        plan, instrumentScope: item ? strategyScope(item) : { mode: "any", symbols: [] },
        ...(item ? { baseStrategyId: item.strategyId } : {}),
        ...(fromTemplate ? { templateId: state.templateId } : {}) }) });
      void strategiesChanged();
      // #507: a save that answers after the draft was thrown away or replaced only reports the strategy.
      if (mine !== generation) return { item: saved, current: false };
      if (rootUrl !== context.rootUrl) { set({ saving: false }); return { item: saved, current: false }; }
      const current = revision === formRevision && scope === contextRevision && ticket === editTicket;
      // Keep newer form/AI work, but the next save belongs to this saved strategy, including a
      // template's first save. Its saved form is now the baseline for deciding what changed.
      // The server may make a template name unique. Accept that name only if the user has not renamed it meanwhile.
      const initial = { ...sentForm, name: saved.name };
      const form = state.form.name === sentForm.name ? { ...state.form, name: saved.name } : state.form;
      set({ saving: false, savedStrategyId: saved.strategyId, mode: "edit", strategyId: saved.strategyId,
        item: saved, initial, form, base: state.base === sentBase ? saved.definition.plan ?? null : state.base });
      schedule();
      return { item: saved, current };
    } catch (error) {
      if (mine === generation) set({ saving: false, error: strategyError(error, "策略没存上，请检查规则后再试") });
      throw error;
    }
  })();
}

// -- the snapshot ----------------------------------------------------------------------------------

const ENTRY = ["entryLong", "entryShort"] as const;
function groupSnapshot(state: EditorState, key: "entryLong" | "entryShort" | "exitLong" | "exitShort") {
  const group: GroupForm = state.form[key];
  if (!group.rows.length && !group.locked) return null;
  const labels = group.rows.map(row => rowLabel(row, state.form.timeframe));
  // 修饰 (连续 N 根 / N 根内 …) the label does not always say: per row, only where a row has some.
  const modifiers = group.rows.map(rowModifiers);
  const extra = { ...(modifiers.some(words => words.length) ? { modifiers } : {}),
    // 方向确认 / 复位 (entry groups), only where the group has some.
    ...(group.alignment.length ? { alignment: group.alignment.map(row => rowLabel(row, state.form.timeframe)) } : {}),
    ...(group.rearm.length ? { rearm: group.rearm.map(row => rowLabel(row, state.form.timeframe)) } : {}) };
  if (!ENTRY.includes(key as "entryLong")) return { join: group.join, conditions: labels, ...extra };
  const { hits, together } = groupHits(state, key as "entryLong" | "entryShort");
  return { join: group.join, conditions: labels, ...extra, hits, together };
}

/** 近 60 个交易日满足次数: per row (by label) and the group (同时 / 任一), from the last preview. */
export function groupHits(state: EditorState, key: "entryLong" | "entryShort"): { hits: (number | null)[]; together: number | null } {
  return hitsOf(state.form, key, state.backtest.result);
}

/** The 流程图 of the card (the drawing and the snapshot's `flow` are this one model). */
export function editorFlow(current: EditorState, chartTimeframe = context.chartTimeframe): StrategyFlowModel {
  const plan = planFromForm(current.form, current.base);
  return strategyFlow(current.form, plan, current.backtest.result, current.backtest.status === "done", chartTimeframe);
}

export function editorSnapshot(current: EditorState = state): Record<string, unknown> {
  if (!current.open) return current.savedStrategyId ? { open: false, savedStrategyId: current.savedStrategyId } : { open: false };
  const { form, backtest } = current;
  const missing = missingParts(form);
  const plan = planFromForm(form, current.base);
  const result = backtest.result;
  const flow = editorFlow(current);
  const box = (item: StrategyFlowModel["trigger"]) => ({ id: item.id, title: item.title, ...(item.lines.length ? { lines: item.lines } : {}),
    ...(item.rows ? { rows: item.rows } : {}), count: item.count, ...(item.pending ? { pending: true } : {}) });
  return {
    open: true, mode: current.mode, strategyId: current.strategyId, start: current.start, name: form.name.trim() || (current.reminder ? "未命名提醒" : "未命名策略"),
    ...(current.reminder ? { reminder: true } : {}),
    // 正在读成交 / 正在载入模板: the form is about to be replaced; new / edit / start are not done before it is null.
    loading: current.loading,
    fields: {
      symbol: form.symbol, timeframe: form.timeframe, direction: form.direction,
      entryLong: groupSnapshot(current, "entryLong"), entryShort: groupSnapshot(current, "entryShort"),
      stop: { mode: form.stop.mode, value: form.stop.value, ...(form.stop.mode === "lookback" ? { risk: form.stop.risk } : {}) },
      take: { mode: form.take.mode, value: form.take.value },
      trailing: { on: form.trailing.on, mode: form.trailing.mode, value: form.trailing.value, activateAfter: form.trailing.activateAfter, atrPeriod: form.trailing.atrPeriod },
      closeBeforeEnd: { on: form.closeBeforeEnd.on, minutes: form.closeBeforeEnd.minutes, everySession: form.closeBeforeEnd.everySession },
      // 按资金比例 does not use maxPosition (its cap is sizingMore.sizing.maxLots).
      lots: form.lots, maxPosition: form.sizing.mode === "equity_percent" ? null : maxPositionOf(form), maxOrdersPerDay: form.maxOrdersPerDay,
      entryWindows: form.entryWindows, allowNightEntries: form.allowNightEntries, pyramiding: form.pyramiding.on,
      more: { breakeven: form.breakeven.on ? { unit: form.breakeven.unit, trigger: form.breakeven.trigger, offsetTicks: form.breakeven.offsetTicks } : null,
        scaleOut: form.scaleOut, timeTakeProfit: form.timeTakeProfit, maxHoldBars: form.maxHoldBars,
        exitLong: groupSnapshot(current, "exitLong"), exitShort: groupSnapshot(current, "exitShort") },
      sizingMore: { perTradeRisk: form.stop.mode === "lookback" ? null : form.stop.risk, sizing: sizingSnapshot(form.sizing),
        maxDrawdown: form.maxDrawdown.on ? { percent: form.maxDrawdown.percent } : null, pyramiding: form.pyramiding.on ? form.pyramiding : null,
        cooldownBars: form.cooldownBars, maxEntriesPerDirection: form.maxEntriesPerDirection, maxLoss: form.maxLoss },
      ...(current.reminder ? { reminder: form.reminderOptions } : { signalAction: form.signalAction ?? "confirm" }),
    },
    // 策略流程图: the form as the engine runs it, with the preview's counts (only while the result is this form's).
    flow: { basis: flow.basis, trigger: box(flow.trigger), entries: flow.entries.map(box), action: box(flow.action), exits: flow.exits.map(box), notes: flow.notes },
    focus: current.focus?.target ?? null,
    aiFields: current.aiFields, changed: current.changed,
    prefill: current.prefill ? { eligible: current.prefill.eligible, reason: current.prefill.reason ?? null, count: current.aiFields.length,
      summary: current.prefill.summary ?? null, trades: current.prefill.sample?.trades ?? current.prefill.trades.length } : null,
    advanced: advancedRules(plan), missing,
    backtest: backtest.status === "idle" && !result ? null : {
      status: backtest.status, trades: result?.summary.trades ?? null, winRate: result?.summary.winRate ?? null,
      netProfit: result?.summary.netProfit ?? null, maxDrawdown: result?.summary.maxDrawdown ?? null, error: backtest.error ?? null,
      previous: backtest.previous,
      // 本机优先: where this result was computed and, for the Runtime, why (diagnostic, not on screen).
      source: result?.source ?? null, fallbackReason: result?.fallbackReason ?? null },
    // 按资金比例's live figures (the Runtime's); null = not used or not available.
    funds: current.funds ? { status: current.funds.status, ...(current.funds.data ?? {}), ...(current.funds.error ? { error: current.funds.error } : {}),
      estimatedAt: current.funds.at ? new Date(current.funds.at).toISOString() : null } : null,
    canSave: !missing.length && !current.saving && !designerPlanProblem(plan),
    ask: current.ask ? { text: current.ask.text, status: current.ask.status } : null,
    questions: current.questions.map(question => ({ id: question.id, question: question.question, options: question.options.map(option => option.label) })),
    saving: current.saving, savedStrategyId: current.savedStrategyId, error: current.error,
  };
}

/** 开仓手数 in the snapshot: null = 固定手数; otherwise the chosen mode with its own numbers. */
function sizingSnapshot(sizing: EditorForm["sizing"]) {
  if (sizing.mode === "atr_risk") return { mode: sizing.mode, riskPercent: sizing.riskPercent, atrMultiple: sizing.atrMultiple, atrPeriod: sizing.atrPeriod };
  if (sizing.mode === "equity_percent") return { mode: sizing.mode, equityPercent: sizing.equityPercent, maxLots: sizing.maxLots };
  return null;
}

/** Test helper. */
export function resetStrategyEditor() { clearTimeout(timer); clearTimeout(fundsTimer); inFlight = false; askJob = null; generation += 1; askGeneration += 1; state = closed(); notify(); }
