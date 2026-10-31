import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, ChevronDown, ChevronLeft, ChevronRight, Sparkles } from "lucide-react";
import type { InstrumentCatalog } from "../../api";
import { loadStrategies, needsAnswersOf, reminderContracts, strategyError, strategySymbol, validateAndStart, type SavedStrategy } from "../../strategyApi";
import { EDITOR_SECTIONS, OPERATORS, SIDE_WORDS, advancedRules, missingParts, planFromForm, rowParts, sectionOfGroup, sectionSummary, usedEntryGroups,
  withLeft, withOp, findRow, listKey, rowContext, rowProblem, LIST_WORDS, type EditorSection, type GroupForm, type GroupKey, type RowContext, type RowForm } from "../../strategyEditor";
import { isTrend, operatorProblem, usesPeriod } from "../../strategyIndicators";
import { focusPlace } from "../../strategyFlow";
import { backtestProgress, applyEditorChange, askEditor, discardDraft, editDraft, editFields, editorFlow, editorSnapshot, editorState, focusEditor, groupHits, hideDraft, needsSave,
  newDraft, openDraft, pendingEdit, regenerate, saveDraft, setEditorAnswer, setEditorContext, setEditorError, setStart, useStrategyEditor, PREVIEW_TRADING_DAYS,
  type EditorChange, type EditorStart, type EditorState } from "../../strategyEditorStore";
import { loadTemplates } from "../../strategyTemplates";
import { useDisclosureSurface, useUiSurface } from "../../uiSurfaces";
import { AiTag, BacktestDetail, BasicsFields, EDITOR_HELP, EntryFields, ExitFields, RepeatFields, SizingFields, StartChoice, WindowFields, backtestDetailHint, useFocusFollow } from "../StrategyEditorCard";
import { HelpTip } from "../Blocks";
import { ModifierChips, PickerPanel, QuadrantRight, TrendRight } from "../IndicatorPicker";
import { StrategyFlow } from "../StrategyFlow";
import { StrategyQuestions } from "../StrategyQuestions";
import { contractLabel, formatNumber, formatSigned, signClass } from "../futures/support";
import type { PushToTalk } from "../futures/PushToTalk";
import { HoldToTalk, HoldToTalkLive } from "./HoldToTalk";
import type { MobileStrategies } from "./MobileStrategy";
import "../StrategyEditorCard.css";

const CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫";
type Step = "left" | "op" | "right";
/** The page's foldable blocks: the four sections and the 策略流程 after them (one open at a time). */
type Block = EditorSection | "flow";
const SECTION_HELP: Record<EditorSection, string> = { basics: EDITOR_HELP.basics, entry: EDITOR_HELP.entry, exit: EDITOR_HELP.exit, sizing: EDITOR_HELP.sizing };

/** The groups whose rows the phone shows (and whose sheets a command may open): the entry sides the direction
 *  uses and the exit sides it can hold; a 条件组合 group is read-only. */
function sheetGroups(state: EditorState): GroupKey[] {
  const form = state.form;
  const exits: GroupKey[] = state.reminder ? []
    : [...(form.direction !== "short" ? ["exitLong" as const] : []), ...(form.direction !== "long" ? ["exitShort" as const] : [])];
  return [...usedEntryGroups(form), ...exits].filter(key => !form[key].locked);
}

/** A disclosure registered for one id (a section, a condition's sheet) while the page is open; renders nothing. */
function Surface({ name, id, open, onApply }: { name: string; id: string; open: boolean; onApply: (open: boolean) => void }) {
  useDisclosureSurface(name, { id, state: open, apply: onApply });
  return null;
}

/**
 * 改条件 (the approved mockup's third screen; 第二版 picker.html): the row's 左边 / 比较 / 右边 in three steps — a side is
 * chosen in the full indicator picker (the desktop's categories as sliding chips, search, the parameters under it), the
 * comparison from the list (disabled with the reason where the engine refuses it), and the row's 修饰 under them; 好
 * writes the row into the draft (the store), 取消 keeps it as it was.
 */
function ConditionSheet({ row, side, index, step, context, onStep, onDone, onCancel }: { row: RowForm; side: string; index: number; step: Step; context: RowContext;
  onStep: (step: Step) => void; onDone: (row: RowForm) => void; onCancel: () => void }) {
  const [draft, setDraft] = useState<RowForm>(row);
  const parts = rowParts(draft);
  const name = `${side}第 ${index + 1} 条`;
  const problem = rowProblem(draft, context);
  const list = step === "op" ? OPERATORS.map(item => {
    const reason = draft.left.indicator || context.stateOnly ? operatorProblem(item.value, draft.left.indicator, draft.consecutive, context.stateOnly) : null;
    return <div key={item.value} className="fdm-se-opt" data-on={draft.op === item.value || undefined}>
      <button type="button" aria-pressed={draft.op === item.value} disabled={Boolean(reason)} title={reason ?? undefined}
        onClick={() => { setDraft(withOp(draft, item.value)); onStep("right"); }}>{item.label}{reason ? <small>{reason}</small> : null}{draft.op === item.value ? <Check aria-hidden="true" /> : null}</button></div>;
  }) : step === "right" && isTrend(draft.op) ? <div className="fdm-se-opt"><TrendRight label="右边 · 和几根前比" value={draft.right.value}
    onChange={value => setDraft({ ...draft, right: { ...draft.right, indicator: "constant", value } })} /></div>
    : step === "right" && draft.op === "eq" ? <div className="fdm-se-opt"><QuadrantRight label="右边 · 哪一种" value={draft.right.value}
      onChange={value => setDraft({ ...draft, right: { ...draft.right, indicator: "constant", value } })} /></div>
      : <PickerPanel key={step} side={step} layout="mobile" title={`改${name} · ${step === "left" ? "左边" : "右边"}`} value={draft[step]} op={draft.op}
        planTimeframe={context.planTimeframe ?? 0} spread={Boolean(context.spread)} ownClock={context.ownClock} onChange={value => {
          if (step === "left") {
            setDraft(withLeft(draft, value));
            // A pick that takes no N goes straight on to 比较.
            if (value.indicator !== draft.left.indicator && !usesPeriod(value.indicator)) onStep("op");
          } else setDraft({ ...draft, right: value });
        }} />;
  return <div className="fdm-se-backdrop" onClick={event => { if (event.target === event.currentTarget) onCancel(); }}>
    <section className="fdm-se-sheet" role="dialog" aria-label={`改${name}`} data-agent-target="strategy-editor-condition" data-testid="mobile-strategy-editor-sheet">
      <i className="fdm-se-grip" aria-hidden="true" />
      <h4>改{side}第 {CIRCLED[index] ?? index + 1} 条</h4>
      <div className="se-seg fdm-se-steps" role="group" aria-label="三步">
        <button type="button" aria-pressed={step === "left"} onClick={() => onStep("left")}>左边：{parts.left || "未选"}</button>
        <button type="button" aria-pressed={step === "op"} onClick={() => onStep("op")}>比较：{parts.op || "未选"}</button>
        <button type="button" aria-pressed={step === "right"} onClick={() => onStep("right")}>右边：{parts.right || (isTrend(draft.op) ? "—" : "未选")}</button>
      </div>
      <div className="fdm-se-list">{list}</div>
      <ModifierChips row={draft} name={name} onChange={setDraft} disabled={context.stateOnly ? "有方向确认时条件只看当前状态，不加修饰" : undefined} />
      <div className="fdm-se-sheet-actions">
        <button type="button" className="se-btn" onClick={onCancel}>取消</button>
        <button type="button" className="se-btn primary" disabled={Boolean(problem)} title={problem ?? undefined} onClick={() => onDone(draft)}>好</button>
      </div>
    </section>
  </div>;
}

/**
 * 手机版策略编辑卡 (2026-10-08; the owner approved the three-screen mockup, 第二版 the picker and the flow the same day):
 * the desktop card's draft (strategyEditorStore) and its field sections on one full-screen page — 返回 / 名称 / 放弃 on
 * top, 起点, the AI prefill line (依据 · N 笔 unfolds the trades), 补一句 typed or held to talk, the four sections and the
 * 策略流程 one open at a time (folded ones say themselves in one line), and the fixed bar with the preview backtest's
 * four figures and 存为草稿 / 上线运行…. A condition is edited in a sheet from the bottom.
 */
export function MobileStrategyEditor({ state, chartTimeframe, instruments, ptt, onBack, onDiscard, onStart, onAsk, onSave, onLaunch }: {
  state: EditorState; chartTimeframe: number; instruments?: InstrumentCatalog; ptt: PushToTalk;
  onBack: () => void; onDiscard: () => void; onStart: (start: EditorStart) => void; onAsk: (text: string) => void; onSave: () => void; onLaunch: () => void;
}) {
  const { form, backtest } = state;
  const prefill = state.prefill;
  const eligible = Boolean(prefill?.eligible);
  const [tradesOpen, setTradesOpen] = useDisclosureSurface("strategy-editor-trades", { available: eligible && Boolean(prefill?.trades.length) });
  const [section, setSection] = useState<Block | null>("basics");
  // 更多 and 高级规则 live in ③ 平仓 (④'s 更多 in ④): a command that opens them opens the section too.
  const [moreOpen, setMoreOpen] = useState(false);
  const [sizingMoreOpen, setSizingMoreOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const plan = planFromForm(form, state.base);
  const advanced = advancedRules(plan);
  useDisclosureSurface("strategy-editor-more", { state: moreOpen, apply: open => { if (open) setSection("exit"); setMoreOpen(open); } });
  useDisclosureSurface("strategy-editor-sizing-more", { state: sizingMoreOpen, apply: open => { if (open) setSection("sizing"); setSizingMoreOpen(open); } });
  useDisclosureSurface("strategy-editor-advanced", { state: advancedOpen, available: advanced.length > 0,
    apply: open => { if (open) setSection("exit"); setAdvancedOpen(open); } });
  // 回测明细 (the desktop's block): a sheet from the bottom, opened by the four figures or `strategy-editor-backtest`.
  const [detailOpen, setDetailOpen] = useDisclosureSurface("strategy-editor-backtest");
  // `added`: the row came from 加一条 and is not confirmed yet; `join` is the group's 满足方式 before 方向确认 forced 全部满足.
  type Sheet = { key: GroupKey; rowId: string; step: Step; added?: { join: GroupForm["join"] } };
  const [sheet, setSheet] = useState<Sheet | null>(null);
  // Leaving a just-added row without 好 takes that row out again — only it: whatever else changed the group meanwhile
  // stays, and the forced 全部满足 goes back only while it is still 全部满足 and no other 方向确认 is left.
  const dropAdded = (open: Sheet | null) => {
    if (!open?.added) return;
    const { key, rowId, added } = open;
    editFields([key], current => {
      const group = current[key];
      const at = findRow(group, rowId);
      if (!at) return current;
      const next = { ...group, [at.list]: group[at.list].filter(row => row.id !== rowId) };
      const join = at.list === "alignment" && group.join === "all" && !next.alignment.length ? added.join : group.join;
      return { ...current, [key]: { ...next, join } };
    });
  };
  const openRow = (key: GroupKey, rowId: string, step?: Step, added?: GroupForm) => {
    setSection(sectionOfGroup(key));
    if (key.startsWith("exit")) setMoreOpen(true);
    const row = findRow(form[key], rowId)?.row;
    // Another step of the same row (a picker or condition command) keeps it a just-added row; another row drops it.
    const same = sheet?.key === key && sheet.rowId === rowId;
    if (!same) dropAdded(sheet);
    setSheet({ key, rowId, added: added ? { join: added.join } : same ? sheet.added : undefined,
      step: step ?? (!row?.left.indicator ? "left" : !row.op ? "op" : !row.right.indicator ? "right" : "left") });
  };
  const closeSheet = () => { dropAdded(sheet); setSheet(null); };
  // The sheet's row in whichever list it sits (条件 / 方向确认 / 复位).
  const found = sheet ? findRow(form[sheet.key], sheet.rowId) : null;
  const sheetRow = found?.row;
  // A row removed by 补一句 or a new 起点 closes its sheet.
  useEffect(() => { if (sheet && !sheetRow) setSheet(null); }, [sheet, sheetRow]);
  const page = useRef<HTMLElement>(null);
  useFocusFollow(state, page, target => {
    const place = focusPlace(target);
    setSection(place.section);
    if (place.more && place.section === "exit") setMoreOpen(true);
  });
  const [askText, setAskText] = useState("");
  const [slideCancel, setSlideCancel] = useState(false);
  const asking = state.ask?.status === "running";
  const busy = state.saving || Boolean(state.loading);
  const reminder = state.reminder;
  // 提醒模式 (第 6 批; the phone's since 2026-10-25): 品种、什么时候提醒、提醒时段 — no stop, lots, exits or 回测 figures.
  const missing = missingParts(form).map(part => reminder ? part.replace("开仓条件", "提醒条件") : part);
  const remindHits = reminder ? usedEntryGroups(form).map(key => groupHits(state, key).together).filter((hits): hits is number => hits != null) : [];
  const sections = reminder ? [{ id: "basics" as const, title: "品种、周期与方向" }, { id: "entry" as const, title: "什么时候提醒" }, { id: "sizing" as const, title: "提醒时段与响几次" }]
    : EDITOR_SECTIONS;
  const result = backtest.result;
  const together = { entryLong: groupHits(state, "entryLong").together, entryShort: groupHits(state, "entryShort").together };
  const symbolText = state.symbolLabel && state.aiFields.includes("symbol") ? state.symbolLabel : contractLabel(form.symbol, instruments);
  const send = () => { const text = askText.trim(); if (!text || asking) return; onAsk(text); setAskText(""); };
  // The words belong to the draft the microphone was pressed on: recognised after another draft opened (the
  // hold released, the transcription still on its way), they never ask that one (#523).
  const editorPtt: PushToTalk = { ...ptt, press: () => {
    const pressedDraft = state.draftId;
    ptt.pressTo(text => { if (editorState().open && editorState().draftId === pressedDraft) onAsk(text); });
  } };
  const status = backtestProgress(backtest);
  const flow = editorFlow(state, chartTimeframe);
  const body = (id: EditorSection) => id === "basics" ? <BasicsFields state={state} chartTimeframe={chartTimeframe} instruments={instruments} />
    : id === "entry" ? <EntryFields state={state} onOpenRow={(key, rowId, added) => openRow(key, rowId, undefined, added)} />
      : id === "exit" ? <ExitFields state={state} plan={plan} advanced={advanced} more={[moreOpen, setMoreOpen]} advancedFold={[advancedOpen, setAdvancedOpen]} onOpenRow={(key, rowId, added) => openRow(key, rowId, undefined, added)} />
        : reminder ? <><WindowFields state={state} /><RepeatFields state={state} /></> : <SizingFields state={state} more={[sizingMoreOpen, setSizingMoreOpen]} />;
  const block = (id: Block, title: string, number: string, summary: string, content: () => ReactNode, help: ReactNode) => {
    const open = section === id;
    return <section key={id} className="fdm-se-sec" data-open={open || undefined} aria-label={title} data-section={id}>
      <div className="fdm-se-sec-top">
        <button type="button" className="fdm-se-sec-h" aria-expanded={open} aria-label={title} onClick={() => setSection(open ? null : id)}>
          <span className="se-num">{number}</span><b>{title}</b>
          {open ? <span className="fdm-se-sum" /> : <span className="fdm-se-sum">{summary}</span>}
          <ChevronDown aria-hidden="true" />
        </button>
        {help}
      </div>
      {open ? <div className="fdm-se-sec-b">{content()}</div> : null}
    </section>;
  };

  return <section className="fdm-overlay fdm-page fdm-se se-card" aria-label="策略编辑卡" data-agent-target="strategy-editor" data-testid="mobile-strategy-editor" ref={page}>
    {[...sections.map(item => item.id), ...(reminder ? [] : ["flow" as const])].map(id => <Surface key={id} name="strategy-editor-section" id={id} open={section === id}
      onApply={open => setSection(open ? id : current => current === id ? null : current)} />)}
    {sheetGroups(state).flatMap(key => (key.startsWith("entry") ? ["rows", "alignment", "rearm"] as const : ["rows"] as const).flatMap(list =>
      state.form[key][list].flatMap((row, index) => row.locked ? []
        : [<Surface key={row.id} name="strategy-editor-condition" id={`${listKey(key, list)}:${index + 1}`} open={sheet?.rowId === row.id}
          onApply={open => { if (open) openRow(key, row.id); else if (sheet?.rowId === row.id) closeSheet(); }} />,
          ...(["left", "right"] as const).filter(side => side === "left" || (!isTrend(row.op) && row.op !== "eq")).map(side =>
            <Surface key={`${row.id}:${side}`} name="strategy-editor-picker" id={`${listKey(key, list)}:${index + 1}:${side}`} open={sheet?.rowId === row.id && sheet.step === side}
              onApply={open => { if (open) openRow(key, row.id, side); else if (sheet?.rowId === row.id && sheet.step === side) closeSheet(); }} />)])))}
    <header className="fdm-head fdm-se-head">
      <button type="button" className="fdm-back" aria-label={reminder ? "返回我的提醒" : "返回我的策略"} onClick={onBack}><ChevronLeft /></button>
      <input className="fdm-se-name" aria-label={reminder ? "提醒名称" : "策略名称"} value={form.name} placeholder={reminder ? "未命名提醒" : "未命名策略"} maxLength={80}
        data-changed={state.changed.includes("name") || undefined} onChange={event => editFields(["name"], current => ({ ...current, name: event.target.value }))} />
      <button type="button" className="se-btn danger" onClick={onDiscard}>放弃</button>
    </header>

    <main className="fdm-content fdm-se-body">
      {state.mode === "new" && !reminder ? <div className="fdm-se-start">
        <StartChoice state={state} busy={busy} onStart={onStart} />
        {state.loading ? <span className="se-hint" role="status">{state.loading}</span>
          : prefill && !prefill.eligible && prefill.reason ? <span className="se-hint">{prefill.reason}</span> : null}
      </div> : null}

      {state.aiFields.length > 0 && eligible ? <div className="se-ainote fdm-se-ainote" role="note">
        <span><b>AI 预填了 {state.aiFields.length} 项</b> <AiTag /></span>
        {prefill?.trades.length ? <button type="button" className="fdm-se-basis" aria-expanded={tradesOpen} onClick={() => setTradesOpen(open => !open)}>
          依据 · {prefill.sample?.trades ?? prefill.trades.length} 笔<ChevronRight aria-hidden="true" /></button> : null}
      </div> : null}
      {tradesOpen && prefill?.trades.length ? <div className="fdm-se-trades" aria-label="预填依据的成交">
        {prefill.summary || prefill.notice ? <p className="se-hint">{prefill.summary ?? ""}{prefill.notice ? ` ${prefill.notice}` : ""}</p> : null}
        <ul>{prefill.trades.map(trade => <li key={trade.id}>
          <time>{new Date(trade.openTime / 1e6).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}</time>
          <span>{trade.symbol} {trade.direction === "long" ? "多" : "空"} {trade.volume} 手</span>
          <b className={signClass(trade.net)}>{formatSigned(trade.net, 0)}</b></li>)}</ul>
      </div> : null}

      <form className="se-ask fdm-se-ask" aria-label="补一句" onSubmit={event => { event.preventDefault(); send(); }}>
        <Sparkles size={16} aria-hidden="true" />
        <input aria-label="补一句" value={askText} maxLength={500} disabled={asking} enterKeyHint="send"
          placeholder={asking ? "AI 在改…" : reminder ? "说一句什么时候提醒…" : "说或打一句要改的…"} onChange={event => setAskText(event.target.value)} />
        {askText.trim() ? <button type="submit" className="se-btn primary fdm-se-send" disabled={asking}>改</button>
          : <HoldToTalk ptt={editorPtt} onSlideCancelChange={setSlideCancel} />}
      </form>
      {state.ask?.status === "running" ? <p className="se-status" role="status">“{state.ask.text}” · {state.ask.stage ?? "AI 正在改"}</p> : null}
      {state.ask?.status === "done" && state.changed.length ? <p className="se-status" role="status">已按“{state.ask.text}”改好，改到的地方标了出来，还没保存。</p> : null}
      {state.ask?.status === "failed" ? <p className="se-error" role="alert">{state.ask.error}</p> : null}
      {state.questions.length ? <div className="se-questions">
        <span className="se-hint">AI 还有几处要问你，答完再改一遍：</span>
        <StrategyQuestions questions={state.questions} answers={state.answers} round={state.history.length} busy={asking}
          onChange={(id, answer) => setEditorAnswer(id, answer)} onContinue={() => { try { regenerate(); } catch { /* disabled until answered */ } }} />
      </div> : null}
      {state.error ? <p className="se-error" role="alert">{state.error}</p> : null}

      {sections.map((item, index) => block(item.id, item.title, String(index + 1),
        reminder && item.id === "sizing" ? (form.entryWindows.map(window => `${window.start}–${window.end}`).join("、") || "不限") : sectionSummary(item.id, form, { symbolText, together }).replace("开仓条件", reminder ? "提醒条件" : "开仓条件"),
        () => body(item.id),
        reminder && item.id === "sizing" ? <HelpTip helpKey="strategy-editor:section-reminder" label="提醒时段">{EDITOR_HELP.reminder}</HelpTip>
          : <HelpTip helpKey={`strategy-editor:section-${item.id}`} label={item.title}>{SECTION_HELP[item.id]}</HelpTip>))}
      {reminder ? null : block("flow", "策略流程", "≡", [flow.exits.length ? `${flow.exits.length} 种出场` : "", flow.basis ?? ""].filter(Boolean).join(" · "),
        () => <StrategyFlow flow={flow} layout="mobile" onFocus={target => focusEditor(target)} />,
        <HelpTip helpKey="strategy-editor:flow" label="策略流程">{EDITOR_HELP.flow}</HelpTip>)}
    </main>

    {reminder ? <footer className="fdm-se-foot" aria-label="开始提醒">
      {missing.length ? <p className="se-blank se-missing" role="status">还差：{missing.join("、")}</p>
        : <p className="se-status" role="status">{remindHits.length ? `近 ${PREVIEW_TRADING_DAYS} 个交易日条件满足 ${formatNumber(remindHits.reduce((sum, hits) => sum + hits, 0), 0)} 次`
          : backtestProgress(backtest) || "正在数近 60 个交易日满足几次…"}</p>}
      <div className="fdm-se-actions">
        <button type="button" className="se-btn" disabled={missing.length > 0 || busy} onClick={onSave}>{state.saving ? "保存中…" : "先存着不开始"}</button>
        <button type="button" className="se-btn primary" disabled={missing.length > 0 || busy} onClick={onLaunch}>开始提醒</button>
      </div>
    </footer> : <footer className="fdm-se-foot" aria-label="回测结果">
      {missing.length ? <p className="se-blank" role="status">填好开仓条件和止损后自动回测。<span className="se-missing">还差：{missing.join("、")}</span></p>
        : <button type="button" className="fdm-se-figs-btn" aria-label="回测明细" aria-expanded={detailOpen} onClick={() => setDetailOpen(true)}>
          {result ? <dl className="fdm-se-figs" aria-label="实时回测">
            <div><dt>笔</dt><dd>{formatNumber(result.summary.trades, 0)}</dd></div>
            <div><dt>胜率</dt><dd>{result.summary.winRate == null ? "—" : `${Math.round(result.summary.winRate)}%`}</dd></div>
            <div><dt>净收益</dt><dd className={signClass(result.summary.netProfit)}>{formatSigned(result.summary.netProfit, 0)}</dd></div>
            <div><dt>回撤</dt><dd>{result.summary.maxDrawdown ? `−${formatNumber(result.summary.maxDrawdown, 0)}` : "0"}</dd></div>
          </dl> : <span className="se-blank">{backtest.status === "error" ? "回测没算出来" : "正在回测最近 60 个交易日…"}</span>}
          <ChevronRight aria-hidden="true" />
        </button>}
      {!missing.length && status && result ? <p className="se-status" role="status">{status}</p> : null}
      {!missing.length && backtest.status === "error" && backtest.error ? <p className="se-error" role="alert">{backtest.error}</p> : null}
      <div className="fdm-se-actions">
        <button type="button" className="se-btn" disabled={missing.length > 0 || busy} onClick={onSave}>{state.saving ? "保存中…" : "存为草稿"}</button>
        <button type="button" className="se-btn primary" disabled={missing.length > 0 || busy} onClick={onLaunch}>上线运行…</button>
      </div>
    </footer>}
    {sheet && sheetRow && found ? <ConditionSheet key={sheet.rowId} row={sheetRow} side={`${SIDE_WORDS[sheet.key]}${LIST_WORDS[found.list]}`} index={found.index}
      step={sheet.step} context={rowContext(form, sheet.key, found.list, Boolean(plan.spread))} onStep={step => setSheet({ ...sheet, step })}
      onCancel={closeSheet}
      onDone={row => { editFields([sheet.key], current => ({ ...current, [sheet.key]: { ...current[sheet.key], [found.list]: current[sheet.key][found.list].map(item => item.id === row.id ? row : item) } })); setSheet(null); }} />
      : null}
    {detailOpen ? <div className="fdm-se-backdrop" onClick={event => { if (event.target === event.currentTarget) setDetailOpen(false); }}>
      <section className="fdm-se-sheet fdm-se-detail" role="dialog" aria-label="回测明细" data-agent-target="strategy-editor-backtest" data-testid="mobile-strategy-editor-backtest">
        <i className="fdm-se-grip" aria-hidden="true" />
        <h4>回测明细</h4>
        {backtestDetailHint(state, instruments) ? <p className="se-hint">{backtestDetailHint(state, instruments)}</p> : null}
        <div className="fdm-se-list"><BacktestDetail state={state} /></div>
        <div className="fdm-se-sheet-actions fdm-se-one"><button type="button" className="se-btn" onClick={() => setDetailOpen(false)}>收起</button></div>
      </section>
    </div> : null}
    <HoldToTalkLive status={ptt.status} slideCancel={slideCancel} />
  </section>;
}

/**
 * The phone's host of the 策略编辑卡 (2026-10-08): the desktop panel is not mounted in the phone layout, so this
 * gives the shared draft its requests' context and registers the same `strategyEditor` UI command (and the old
 * `strategy-new` name) through the store's `applyEditorChange`. 编辑 opens a 我的策略 strategy; 存为草稿 returns
 * to 我的策略 and runs that strategy's 回测 there (the desktop opens its 结果页 and backtests); 上线运行… returns
 * there with the strategy's 上线 confirmation open.
 */
export function useMobileStrategyEditor({ enabled, rootUrl, baseUrl, chartSymbol, chartTimeframe, instruments, strategies, onShowMine, onShowReminders }: {
  enabled: boolean; rootUrl?: string; baseUrl?: string; chartSymbol: string; chartTimeframe: number;
  /** 整张自选表的提醒: each watchlist product's main contract comes from the catalog. */
  instruments?: InstrumentCatalog;
  strategies: MobileStrategies;
  /** The 策略 tab on 我的策略, any full-screen page closed. */
  onShowMine: () => void;
  /** The 提醒 page on 我的提醒: where a 提醒模式 draft returns. */
  onShowReminders: () => void;
}) {
  const editor = useStrategyEditor();
  if (enabled && rootUrl) setEditorContext({ rootUrl, chartSymbol, chartTimeframe });
  // 从模板 ▾ reads the template library (the desktop panel loads it when it mounts).
  useEffect(() => { if (enabled && rootUrl) void loadTemplates(rootUrl).catch(() => undefined); }, [enabled, rootUrl]);
  const edit = (item: SavedStrategy) => {
    if (!item.definition.plan) throw new Error("这条策略没有可编辑的规则");
    editDraft(item, strategySymbol(item, chartSymbol));
  };
  const editById = (id: string) => {
    const item = strategies.mine.find(entry => entry.strategyId === id)?.source;
    if (item) { edit(item); return; }
    if (!baseUrl) throw new Error("策略不存在");
    // Not in the list yet (made a moment ago by the API, CLI or AI): read the list once; the receipt waits for the card.
    const current = pendingEdit();
    return loadStrategies(baseUrl).then(result => {
      if (!current()) return false;
      const found = result.items.find(entry => entry.strategyId === id);
      if (found?.definition.plan) { edit(found); return true; }
      setEditorError("策略不存在"); return false;
    }, error => { if (current()) setEditorError(strategyError(error, "策略列表读取失败")); return false; });
  };
  // 开始提醒 (提醒模式): the saved 提醒 starts as 只提醒 on its contract (整张自选表: on every watchlist product's main
  // contract, one after another), then the 提醒 page; a refusal stays on the card.
  const startReminder = (item: SavedStrategy) => {
    if (!baseUrl) { setEditorError("先连接 Runtime"); return; }
    const draft = editorState().draftId;
    void reminderContracts(baseUrl, item, editorState().form.symbol || chartSymbol, instruments).then(async contracts => {
      for (const contract of contracts) await validateAndStart(baseUrl, item.strategyId, contract, chartTimeframe, undefined, { signalAction: "alert" });
    }).then(() => {
      window.dispatchEvent(new Event("ai-trader-strategies-changed"));
      if (editorState().draftId === draft) { discardDraft(); onShowReminders(); }
    }, error => { if (editorState().draftId === draft) setEditorError(strategyError(error, "提醒没有开始，请稍后再试")); });
  };
  const save = (launch = false) => {
    const reminder = editorState().reminder;
    const saving = saveDraft(strategies.mine.map(item => item.source.name));
    void saving.then(({ item, current }) => {
      if (!current) return;
      if (reminder) { if (launch) startReminder(item); else { discardDraft(); onShowReminders(); } return; }
      discardDraft();
      onShowMine();
      if (launch) { if (!needsAnswersOf(item)) strategies.askLaunch(item.strategyId); }
      else strategies.backtest(item);
    }).catch(() => undefined);
  };
  const launch = () => {
    const item = editorState().item;
    if (editorState().mode === "edit" && item && !needsSave()) {
      if (editorState().reminder) { startReminder(item); return; }
      discardDraft(); onShowMine(); strategies.askLaunch(item.strategyId); return;
    }
    save(true);
  };
  useUiSurface("strategyEditor", { state: editorSnapshot(editor),
    apply: value => applyEditorChange(value as EditorChange, { edit: editById, save: () => save() }) }, [editor, strategies, chartSymbol, baseUrl], enabled);
  useDisclosureSurface("strategy-new", { available: enabled, state: editor.open && editor.mode === "new",
    apply: open => { if (open) newDraft(); else if (editorState().mode === "new") discardDraft(); } });
  const guard = (run: () => void) => () => { try { run(); } catch (error) { setEditorError(error); } };
  return {
    editor,
    onNew: (start?: EditorStart) => newDraft(start),
    onResumeDraft: () => openDraft(),
    onEdit: (item: SavedStrategy) => { if (item.definition.plan) edit(item); },
    handlers: {
      onBack: () => { const reminder = editorState().reminder; hideDraft(); if (reminder) onShowReminders(); },
      onDiscard: () => { const reminder = editorState().reminder; discardDraft(); if (reminder) onShowReminders(); },
      onStart: (start: EditorStart) => setStart(start),
      onAsk: (text: string) => { try { askEditor(text); } catch (error) { setEditorError(error); } },
      onSave: guard(() => save()), onLaunch: guard(launch),
    },
  };
}
