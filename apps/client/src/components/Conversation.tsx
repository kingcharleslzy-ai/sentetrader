import { Fragment, useEffect, useState } from "react";
import { Volume2 } from "lucide-react";
import type { FlowItem } from "./futures/aiFlow";
import type { AgentTask } from "@ai-trader/ui-events";
import type { AccountSnapshot } from "../api";
import type { ConversationCard, ConversationTurn } from "../state/conversation";
import { AccountCard } from "./cards/AccountCard";
import { BrandMark } from "./BrandMark";
import { BacktestCard } from "./cards/BacktestCard";
import { StrategyBacktestConversationCard } from "./StrategyBacktestView";
import { CloseBatchProposalCard } from "./cards/CloseBatchProposalCard";
import { SpreadProposalCard } from "./cards/SpreadProposalCard";
import { AgentDiffCard } from "./cards/AgentDiffCard";
import { AgentToolCard } from "./cards/AgentToolCard";
import { MarketCard } from "./cards/MarketCard";
import { RiskRuleProposalCard } from "./cards/RiskRuleProposalCard";
import { StrategyCard } from "./cards/StrategyCard";
import { useDisclosureSurface } from "../uiSurfaces";
import { useSmoothReveal } from "../hooks/useSmoothReveal";
import { TaskErrorCard } from "./cards/TaskErrorCard";
import { TradeProposalCard } from "./cards/TradeProposalCard";
import { BriefCard } from "./cards/BriefCard";
import { ExecutionCard } from "./cards/ExecutionCard";
import { OrderQuestionCard } from "./cards/OrderQuestionCard";
import { ReminderCard } from "./cards/ReminderCard";
import { AnswerCard, DataCard } from "./cards/AnswerCard";
import { ResearchJudgmentCard } from "./cards/ResearchJudgmentCard";
import { RoutineCard, RoutineRunCard } from "./cards/RoutineCards";
import { confirmBlocked, otherAccountNote } from "./cards/confirmGate";

export interface ProposalActions {
  busyProposalId?: string;
  strategyRefreshKey?: string;
  /** The live simulation orders of a card's own account (#1005: its `accountId`, never just the current
   *  account's), so a confirmed card shows its orders' real state; `undefined` while not read. */
  ordersFor?: (accountId?: string) => import("../api").SimulationOrder[] | undefined;
  /** Whether a card of this account is on the session's current account (a card without one is). */
  onCurrentAccount?: (accountId?: string) => boolean;
  /** Per-order 继续发送 / 放弃 / 查询: always on the current account, so only a current-account card offers them. */
  orderAction?: import("./futures/PositionPanels").OrderActionHandler;
  adjustRisk: (proposalId: string, proposalVersion: number) => void;
  /** The card's own 确认: submits it at once (no second dialog, owner 2026-10-19). */
  confirm: (card: ConversationCard) => void;
  /** Why the account or the simulation gateway cannot take a confirmation now; the card shows it by 确认. */
  confirmGate?: string;
  /** An account's display name from the account directory: a card for another account than the session's
   *  names its own under its title (#1011). */
  accountName?: (accountId: string) => string | undefined;
  /** The card whose 确认 is being submitted: every other card's 确认 waits for it. */
  confirming?: string;
  reject: (proposalId: string, proposalVersion: number) => void;
  cancelRun: (runId: string) => void;
  changeStrategyMonitoring: (
    strategyId: string,
    action: "start" | "pause"
  ) => Promise<import("../api").StrategyMonitorSnapshot>;
  loadStrategyMonitor: (strategyId: string) => Promise<import("../api").StrategyMonitorSnapshot>;
  /** Structured cards' buttons: send a prompt as the user's own message (the composer's path). */
  askAi?: (text: string) => void;
  /** Structured cards' buttons: apply a UI command in this window (keys whitelisted by the card). */
  applyUi?: (changes: Record<string, unknown>) => void;
  /** 保本提醒's 移到成本 / 不用 (its business route; the user's own click). */
  breakeven?: (breakevenId: string, choice: "move" | "decline") => void;
  /** 流程卡's 保存: the user's own POST /api/routines {draftId} (rejects with the refusal's sentence). */
  saveRoutine?: (draftId: string) => Promise<void>;
  /** 流程卡's 改一下: put text in the composer for the user to finish (never sends). */
  fillPrompt?: (text: string) => void;
}

/** The read cards an answer card folds into its 依据 (去重, 2026-10-14 跟进): the ids it names that this turn has. */
function foldedIds(cards: ConversationCard[]): Set<string> {
  const present = new Set(cards.map(card => card.id));
  return new Set(cards.flatMap(card => card.type === "answer" ? (card.payload.folded ?? []).filter(id => present.has(id)) : []));
}

/** One conversation card in its own component (also the phone's AI 首页 pins the unconfirmed ones with it).
 *  `turnCards` lets an answer card show the turn's read cards it folded inside its 依据. */
export function CardRenderer({ card, actions, turnCards }: { card: ConversationCard; actions: ProposalActions; turnCards?: ConversationCard[] }) {
  switch (card.type) {
    case "account":
      return <AccountCard id={card.id} account={card.payload} />;
    case "market":
      return <MarketCard id={card.id} market={card.payload} />;
    case "strategy":
      return <StrategyCard
        id={card.id}
        strategy={card.payload}
        busy={actions.busyProposalId === card.payload.strategyId}
        refreshKey={actions.strategyRefreshKey}
        onMonitorAction={actions.changeStrategyMonitoring}
        onLoadMonitor={actions.loadStrategyMonitor}
      />;
    case "task":
    case "risk":
      // Rendered by the turn itself: a status line / 过程 fold and a footnote, never a card.
      return null;
    case "backtest":
      return <BacktestCard id={card.id} backtest={card.payload} />;
    case "strategy-backtest":
      return <StrategyBacktestConversationCard id={card.id} result={card.payload} />;
    case "trade":
      return (
        <TradeProposalCard
          id={card.id}
          proposal={card.payload}
          orders={actions.ordersFor?.(card.payload.accountId)}
          accountNote={otherAccountNote(card.payload.accountId, actions, card.payload.accountLabel)}
          busy={actions.busyProposalId === card.payload.proposalId}
          onAdjustRisk={() => actions.adjustRisk(card.payload.proposalId, card.payload.version)}
          onConfirm={() => actions.confirm(card)}
          blockedReason={confirmBlocked(card, actions.confirmGate, actions.onCurrentAccount) ?? (actions.confirming && actions.confirming !== card.payload.proposalId ? "另一张卡正在提交，稍候再确认" : undefined)}
          onReject={() => actions.reject(card.payload.proposalId, card.payload.version)}
          onAsk={actions.askAi}
        />
      );
    case "close-batch":
      return (
        <CloseBatchProposalCard
          id={card.id}
          proposal={card.payload}
          orders={actions.ordersFor?.(card.payload.accountId)}
          accountNote={otherAccountNote(card.payload.accountId, actions, card.payload.accountLabel)}
          // Another account's card (#1005) shows its orders but acts on none of them from here.
          onOrderAction={actions.onCurrentAccount?.(card.payload.accountId) === false ? undefined : actions.orderAction}
          otherAccount={actions.onCurrentAccount?.(card.payload.accountId) === false}
          // A per-order continue/abandon is busy under its own client order id (AIT…).
          busy={actions.busyProposalId === card.payload.proposalId || Boolean(actions.busyProposalId?.startsWith("AIT"))}
          onConfirm={() => actions.confirm(card)}
          blockedReason={confirmBlocked(card, actions.confirmGate, actions.onCurrentAccount) ?? (actions.confirming && actions.confirming !== card.payload.proposalId ? "另一张卡正在提交，稍候再确认" : undefined)}
          onReject={() => actions.reject(card.payload.proposalId, card.payload.version)}
          onAsk={actions.askAi}
        />
      );
    case "spread":
      return (
        <SpreadProposalCard
          id={card.id}
          proposal={card.payload}
          orders={actions.ordersFor?.(card.payload.accountId)}
          accountNote={otherAccountNote(card.payload.accountId, actions, card.payload.accountLabel)}
          busy={actions.busyProposalId === card.payload.proposalId}
          onConfirm={() => actions.confirm(card)}
          blockedReason={confirmBlocked(card, actions.confirmGate, actions.onCurrentAccount) ?? (actions.confirming && actions.confirming !== card.payload.proposalId ? "另一张卡正在提交，稍候再确认" : undefined)}
          onReject={() => actions.reject(card.payload.proposalId, card.payload.version)}
        />
      );
    case "risk-rule":
      return (
        <RiskRuleProposalCard
          id={card.id}
          proposal={card.payload}
          busy={actions.busyProposalId === card.payload.proposalId}
          onConfirm={() => actions.confirm(card)}
          blockedReason={confirmBlocked(card, actions.confirmGate, actions.onCurrentAccount) ?? (actions.confirming && actions.confirming !== card.payload.proposalId ? "另一张卡正在提交，稍候再确认" : undefined)}
          onReject={() => actions.reject(card.payload.proposalId, card.payload.version)}
        />
      );
    case "agent-tool":
      return <AgentToolCard id={card.id} activity={card.payload} />;
    case "agent-diff":
      return <AgentDiffCard id={card.id} artifact={card.payload} />;
    case "task-error":
      return <TaskErrorCard id={card.id} error={card.payload} />;
    case "order-question":
      return <OrderQuestionCard id={card.id} card={card.payload} handlers={actions} />;
    case "research-judgment":
      return <ResearchJudgmentCard id={card.id} card={card.payload} handlers={actions} />;
    case "reminder":
      return <ReminderCard id={card.id} card={card.payload} handlers={actions} />;
    case "execution":
      return <ExecutionCard id={card.id} card={card.payload} />;
    case "routine":
      return <RoutineCard id={card.id} card={card.payload} handlers={actions} />;
    case "routine-run":
      return <RoutineRunCard id={card.id} card={card.payload} />;
    case "answer": {
      const folded = (card.payload.folded ?? []).flatMap(id => turnCards?.filter(item => item.id === id && item.type !== "answer") ?? []);
      return <AnswerCard id={card.id} card={card.payload} handlers={actions} folded={folded.length ? folded.map(item => (
        <div className="ac-folded-card" data-card-id={item.id} key={item.id}><CardRenderer card={item} actions={actions} /></div>
      )) : undefined} />;
    }
    case "data":
      return <DataCard id={card.id} card={card.payload} handlers={actions} />;
  }
}

/** "正在读取行情…" for the step in flight; Runtime labels are already in the user's words. */
function stageText(task: AgentTask): string {
  const active = [...task.steps].reverse().find(step => step.status === "active");
  const label = task.stage || active?.label || "等待模型响应";
  return `${label.includes("正在") || label.includes("等待") ? label : `正在${label}`}…`;
}

/**
 * The AI's work on a turn, as the user should see it: while it runs, one status line under the
 * streaming bubble (with 停止); afterwards nothing but a folded 过程 list — `disclosure: progress-steps`.
 */
function TurnProgress({ task, id, onCancel }: { task: AgentTask; id: string; onCancel: () => void }) {
  const [cancelling, setCancelling] = useState(false);
  const running = task.status === "running";
  const [open, setOpen] = useDisclosureSurface("progress-steps", { id, available: !running && (Boolean(task.summary) || task.steps.length > 0) });
  useEffect(() => { if (running) setCancelling(false); }, [running]);
  if (running) {
    return <p className="reply-status" role="status" data-testid="turn-progress">
      <i aria-hidden="true" />{stageText(task)}
      <button type="button" disabled={cancelling} onClick={async () => { setCancelling(true); try { await onCancel(); } finally { setCancelling(false); } }}>{cancelling ? "正在停止…" : "停止"}</button>
    </p>;
  }
  if (task.status === "failed" || task.status === "cancelled") {
    return <p className="reply-footnote danger" data-testid="turn-progress">{task.status === "cancelled" ? "已停止" : task.stage || "未完成"}</p>;
  }
  if (!task.summary && !task.steps.length) return null;
  return <details className="reply-process" open={open} onToggle={e => setOpen(e.currentTarget.open)} data-testid="turn-process">
    <summary>过程 · {task.steps.length} 步</summary>
    <div>
      {task.summary ? <p className="reply-process-summary">{task.summary}</p> : null}
      {task.steps.length ? <ul>{task.steps.map(step => <li key={step.id}><span>{step.status === "done" ? "✓" : step.status === "failed" ? "!" : "·"}</span>{step.label}<small>{step.detail}</small></li>)}</ul> : null}
    </div>
  </details>;
}

function clock(timestamp: string) {
  if (!timestamp) return "刚刚";
  const date = new Date(timestamp);
  return Number.isNaN(date.valueOf())
    ? "刚刚"
    : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function MessageText({ text }: { text: string }) {
  const parts = text.split(/(https?:\/\/[^\s<>"'）)\]}，。；！？]+)/g);
  return <>{parts.map((part, index) => (
    /^https?:\/\//.test(part)
      ? <a href={part} key={`${part}-${index}`} rel="noreferrer" target="_blank">{part}</a>
      : part
  ))}</>;
}

type AdviceSection = {
  key: "conclusion" | "reasons" | "risk" | "action" | "sources";
  title: "结论" | "为什么" | "风险" | "现在怎么做" | "参考依据";
  lines: string[];
};

const adviceHeadings = new Map<string, Pick<AdviceSection, "key" | "title">>([
  ["结论", { key: "conclusion", title: "结论" }],
  ["为什么", { key: "reasons", title: "为什么" }],
  ["依据", { key: "reasons", title: "为什么" }],
  ["风险", { key: "risk", title: "风险" }],
  ["风险/边界", { key: "risk", title: "风险" }],
  ["风险与边界", { key: "risk", title: "风险" }],
  ["现在怎么做", { key: "action", title: "现在怎么做" }],
  ["可执行下一步", { key: "action", title: "现在怎么做" }],
  ["来源", { key: "sources", title: "参考依据" }],
  ["参考依据", { key: "sources", title: "参考依据" }],
  ["消息来源", { key: "sources", title: "参考依据" }],
  ["信息来源", { key: "sources", title: "参考依据" }],
]);

function cleanAdviceLine(value: string): string {
  return value.replace(/^\s*(?:[-*]|\d+[.)])\s*/, "").replaceAll("**", "").trim();
}

/** `partial`: the part of a reply that has typed out so far — whatever sections it has, even none. */
function parseAdvice(text: string, partial = false): AdviceSection[] | null {
  const sections: AdviceSection[] = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const match = line.match(/^(?:#{1,6}\s*)?\*{0,2}(结论|为什么|依据|风险(?:\/边界|与边界)?|现在怎么做|可执行下一步|来源|参考依据|消息来源|信息来源)\*{0,2}\s*[：:]?\s*(.*)$/);
    const heading = match ? adviceHeadings.get(match[1]) : undefined;
    if (match && heading) {
      sections.push({ ...heading, lines: match[2] ? [cleanAdviceLine(match[2])] : [] });
      continue;
    }
    if (sections.length) sections.at(-1)?.lines.push(cleanAdviceLine(line));
  }
  if (partial) return sections;
  const keys = new Set(sections.map((section) => section.key));
  return keys.has("conclusion") && keys.size >= 3 ? sections : null;
}

/** The folded source list of an advice; commandable as `disclosure: advice-sources` by the turn id. */
function AdviceSources({ section, id }: { section: AdviceSection; id: string }) {
  const [open, setOpen] = useDisclosureSurface("advice-sources", { id });
  const sourceCount = Math.max(1, section.lines.join(" ").match(/https?:\/\//g)?.length ?? section.lines.length);
  return <details className="ai-advice-sources" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
    <summary>{section.title} · {sourceCount} 条</summary>
    <div>{section.lines.map((line, lineIndex) => (
      <p key={lineIndex}><MessageText text={line} /></p>
    ))}</div>
  </details>;
}

function AdviceText({ sections, id }: { sections: AdviceSection[]; id: string }) {
  return <div className="ai-advice" data-testid="ai-advice">
    {sections.map((section, sectionIndex) => {
      if (section.key === "sources") return <AdviceSources section={section} id={id} key={`${section.key}-${sectionIndex}`} />;
      return <section className={`ai-advice-section ${section.key}`} key={`${section.key}-${sectionIndex}`}>
          <h4>{section.title}</h4>
          <div>{section.lines.map((line, lineIndex) => (
            <p key={lineIndex}><MessageText text={line} /></p>
          ))}</div>
        </section>;
    })}
  </div>;
}

/** Markdown the model still emits despite the prompt: bold markers, heading hashes, list bullets. */
function plainLine(line: string): string {
  return line.replace(/^\s*#{1,6}\s*/, "").replace(/^\s*(?:[-*]|\d+[.)])\s+/, "").replaceAll("**", "").trim();
}

const tableRow = (line: string) => line.trim().startsWith("|");
const tableCells = (line: string) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => plainLine(cell));
const tableRule = (line: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

/**
 * A plain reply as the user reads it: blank lines separate paragraphs, single line breaks stay,
 * a `| … |` block becomes a real table (the `|---|` rule row is dropped). No markdown library —
 * this is the whole vocabulary the reply contract allows.
 */
function MessageBody({ text, id }: { text: string; id: string }) {
  const blocks: ({ kind: "table"; rows: string[][] } | { kind: "text"; lines: string[] })[] = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    if (tableRow(line)) {
      if (tableRule(line)) continue;
      const last = blocks.at(-1);
      if (last?.kind === "table") last.rows.push(tableCells(line));
      else blocks.push({ kind: "table", rows: [tableCells(line)] });
      continue;
    }
    const last = blocks.at(-1);
    if (!line.trim()) {
      // A blank line ends the paragraph; the next text line starts a new one.
      if (last?.kind === "text" && last.lines.length) blocks.push({ kind: "text", lines: [] });
      continue;
    }
    if (last?.kind === "text") last.lines.push(plainLine(line));
    else blocks.push({ kind: "text", lines: [plainLine(line)] });
  }
  const shown = blocks.filter(block => block.kind === "table" ? block.rows.length > 0 : block.lines.length > 0);
  // A trailing 来源 paragraph folds like an advice's sources.
  const last = shown.at(-1);
  const sourceMatch = last?.kind === "text" ? last.lines[0].match(/^(来源|参考依据|消息来源|信息来源)\s*[：:]?\s*(.*)$/) : null;
  const sources = sourceMatch && last?.kind === "text" ? [...(sourceMatch[2] ? [sourceMatch[2]] : []), ...last.lines.slice(1)] : null;
  if (sources?.length) shown.pop();
  if (shown.length === 1 && shown[0].kind === "text" && shown[0].lines.length === 1) return <MessageText text={shown[0].lines[0]} />;
  return <>{shown.map((block, index) => block.kind === "table" ? (
    <table className="reply-table" key={`table-${index}`}>
      <thead><tr>{block.rows[0].map((cell, cellIndex) => <th key={cellIndex}>{cell}</th>)}</tr></thead>
      <tbody>{block.rows.slice(1).map((row, rowIndex) => (
        <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex} className={/^[+-]?[\d.,%]+$/.test(cell) ? "num" : undefined}><MessageText text={cell} /></td>)}</tr>
      ))}</tbody>
    </table>
  ) : (
    <p className="reply-paragraph" key={`p-${index}`}>{block.lines.map((line, lineIndex) => (
      <span key={lineIndex}>{lineIndex ? <br /> : null}<MessageText text={line} /></span>
    ))}</p>
  ))}{sources?.length ? <AdviceSources section={{ key: "sources", title: "参考依据", lines: sources }} id={id} /> : null}</>;
}

const startupStatusMessages = new Set([
  "Codex 已连接到 AI Trader 完整工程区。TqKq 仅提供只读数据，真实下单能力保持关闭。",
  "Codex 已连接到 AI Trader 完整工程区。账户与交易仍为假数据，真实下单能力保持关闭。",
  "AI Trader Runtime 已就绪，但 Codex 暂未连接。只读数据状态可继续查看。",
  "AI Trader Runtime 已就绪，但模型暂未配置。只读数据状态可继续查看。",
  "Sente 已就绪，但模型暂未配置。只读数据状态可继续查看。",
  "当前使用内置假数据，可直接体验账户、行情、回测和风控流程。",
  "TqKq 只读账户快照已就绪；以下内容仅展示 Runtime 校验后的数据。",
  "TqKq 只读账户快照已经陈旧，不得据此确认交易。",
  "TqKq 只读账户数据当前不可用；页面不会编造账户盈亏结论。"
]);

function isStartupStatusTurn(turn: ConversationTurn): boolean {
  return turn.role === "assistant"
    && turn.cards.some((card) => card.type === "account")
    && startupStatusMessages.has(turn.text.trim());
}

/** Cards that say it all: with one of these in an answer the model's own words are not shown (the
 *  voice still reads its short summary). A quote card keeps a longer analysis written around it. */
const CARD_ONLY_TYPES = new Set(["trade", "close-batch", "spread", "order-question", "reminder", "execution", "routine-run"]);
const SHORT_WITH_QUOTE = 120;

const plain = (text: string) => text.replace(/\s+/g, "");

export function cardSpeaksForTurn(turn: ConversationTurn): boolean {
  if (turn.role !== "assistant") return false;
  // A read card folded into the answer card's 依据 no longer stands in the turn: it never hides the text (#754).
  const folded = foldedIds(turn.cards);
  return turn.cards.some((card) => folded.has(card.id) ? false : CARD_ONLY_TYPES.has(card.type)
    || (card.type === "market" && turn.text.trim().length <= SHORT_WITH_QUOTE)
    // 回答卡收尾的回合：正文就是卡上那句结论，不在气泡里再说一遍（去重, 2026-10-14 跟进）。
    || (card.type === "answer" && plain(turn.text) !== "" && plain(turn.text) === plain(card.payload.conclusion)));
}

/** What the Runtime shows in a reply before the model's first words (native_agent PROCESSING_PLACEHOLDER). */
const PROCESSING_PLACEHOLDER = "正在处理这个请求…";
const ADVICE_OPENING = /^\s*(?:#{1,6}\s*)?\*{0,2}结论/;

/** While typing out: hold back a half-arrived table row or a line that is still only markup
 *  (`-`, `#`, `1.`), and a trailing `*` of a half-arrived `**`, so nothing flashes and vanishes. */
function settledPrefix(text: string): string {
  const lastBreak = text.lastIndexOf("\n");
  const lastLine = text.slice(lastBreak + 1);
  const unsettled = /^\s*\|/.test(lastLine) || (lastLine.trim() !== "" && /^\s*(?:[-*#]+|\d+[.)]?)\s*$/.test(lastLine));
  const shown = unsettled ? text.slice(0, Math.max(0, lastBreak)) : text;
  return shown.replace(/\*+$/, "");
}

/**
 * The AI's words in the bubble. Before the first real words it shows the Runtime's placeholder as a
 * soft sweep; while a reply streams it types out smoothly (useSmoothReveal), keeping the advice layout
 * from the first heading on when the reply opens with 结论 so the bubble does not switch layouts mid-way.
 */
function TurnBubble({ turn, advice }: { turn: ConversationTurn; advice: AdviceSection[] | null }) {
  const streaming = turn.role === "assistant" && turn.completed === false;
  const placeholder = streaming && (!turn.text.trim() || turn.text.trim() === PROCESSING_PLACEHOLDER);
  const revealed = useSmoothReveal(placeholder ? "" : turn.text, streaming);
  if (placeholder || (streaming && !revealed.trim())) {
    return <div className="bubble thinking"><span className="thinking-text">{PROCESSING_PLACEHOLDER}</span></div>;
  }
  if (!streaming && revealed.length >= turn.text.length) {
    return <div className={`bubble${advice ? " advice-bubble" : ""}`}>
      {advice ? <AdviceText sections={advice} id={turn.id} /> : <MessageBody text={turn.text} id={turn.id} />}
    </div>;
  }
  const shown = settledPrefix(revealed);
  const asAdvice = advice || ADVICE_OPENING.test(turn.text) ? parseAdvice(shown, true) ?? [] : null;
  return <div className={`bubble${asAdvice ? " advice-bubble" : ""}`}>
    {asAdvice ? <AdviceText sections={asAdvice} id={turn.id} /> : <MessageBody text={shown} id={turn.id} />}
  </div>;
}

/** Cards that lead the answer: the 今日判断 sits above the bubble and the model's reply follows it. */
const LEADING_TYPES = new Set(["research-judgment"]);
/** Rendered by the turn itself (status line, footnote), never as a card. */
const INLINE_TYPES = new Set(["task", "risk"]);

function Turn({
  turn,
  actions,
  accountOverride
}: {
  turn: ConversationTurn;
  actions: ProposalActions;
  accountOverride?: AccountSnapshot;
}) {
  const user = turn.role === "user";
  const advice = user ? null : parseAdvice(turn.text);
  // 去重 (2026-10-14 跟进): with an answer card the turn's read cards sit in its 依据, not beside it.
  const folded = foldedIds(turn.cards);
  if (isStartupStatusTurn(turn)) {
    return (
      <section className="standalone-cards" aria-label="账户状态">
        {turn.cards.map((card) => (
          <div className="conversation-card-anchor" data-card-id={card.id} key={card.id} tabIndex={-1}>
            {card.type === "account" && accountOverride
              ? <AccountCard id={card.id} account={card.payload} current={accountOverride} />
              : <CardRenderer card={card} actions={actions} />}
          </div>
        ))}
      </section>
    );
  }
  return (
    <article className={`message ${user ? "user" : "assistant"}`} data-origin={turn.origin}
      data-streaming={!user && turn.completed === false ? "true" : undefined}>
      <div className="message-body">
        {/* No avatar or name per message: the AI's line carries the small Sente mark, yours sits at
            the right with the time alone. The speaker stays as visually hidden text for screen
            readers and the agent's snapshot text. */}
        <div className="speaker">{user ? null : <BrandMark size={16} />}<span className="visually-hidden">{user ? "你" : "AI"}</span>
          {turn.origin === "voice" ? <span className="voice-mark" role="img" aria-label={user ? "语音说的" : "已念出"} title={!user && turn.spoken ? `念出：${turn.spoken}` : user ? "通过实时语音说的" : "通过实时语音念出"}><Volume2 /></span> : null}
          <time>{clock(turn.timestamp)}</time></div>
        {user && turn.focus ? <span className="message-focus" data-focus-kind={turn.focus.kind} title="从这个对象的 AI 菜单发问">@ {turn.focus.label}</span> : null}
        {turn.cards.filter((card) => LEADING_TYPES.has(card.type)).map((card) => (
          <div className="conversation-card-anchor" data-card-id={card.id} key={card.id} tabIndex={-1}>
            <CardRenderer card={card} actions={actions} />
          </div>
        ))}
        {cardSpeaksForTurn(turn) || (!turn.text.trim() && turn.cards.some((card) => !INLINE_TYPES.has(card.type)))
          ? null : <TurnBubble turn={turn} advice={advice} />}
        {turn.cards.map((card) => card.type === "task"
          ? <TurnProgress task={card.payload} id={card.id} onCancel={() => actions.cancelRun(card.payload.taskId)} key={card.id} />
          : card.type === "risk"
            ? <p className={`reply-footnote ${card.payload.severity}`} title={card.payload.message} data-testid="turn-footnote" key={card.id}>{card.payload.severity === "danger" ? `已阻止 · ${card.payload.title}` : card.payload.title}</p>
            : null)}
        {turn.cards.map((card) => INLINE_TYPES.has(card.type) || LEADING_TYPES.has(card.type) || folded.has(card.id) ? null : (
          <div className="conversation-card-anchor" data-card-id={card.id} key={card.id} tabIndex={-1}>
            <CardRenderer card={card} actions={actions} turnCards={turn.cards} />
          </div>
        ))}
      </div>
    </article>
  );
}

/**
 * The conversation: turns and the AI's briefs merged on time into one stream.
 */
export function Conversation({
  turns,
  actions,
  accountOverride,
  systemItems
}: {
  turns: ConversationTurn[];
  actions: ProposalActions;
  accountOverride?: AccountSnapshot;
  systemItems?: FlowItem[];
}) {
  const rows = [
    // 续句合并中被并走的半句轮次不渲染（数据保留在 state 里）。
    ...turns.filter((turn) => !turn.hidden).map((turn, index) => ({ key: `turn:${turn.id}`, time: Date.parse(turn.timestamp) || index, order: index,
      node: <Turn turn={turn} actions={actions} accountOverride={accountOverride} key={turn.id} /> })),
    ...(systemItems ?? []).map(item => ({ key: `flow:${item.id}`, time: item.time, order: 0,
      node: <BriefCard item={item} askAi={actions.askAi} key={item.id} /> })),
  ].sort((a, b) => a.time - b.time || a.order - b.order);
  return (
    <main className="conversation" aria-live="polite" data-testid="conversation">
      {rows.map(row => <Fragment key={row.key}>{row.node}</Fragment>)}
    </main>
  );
}
