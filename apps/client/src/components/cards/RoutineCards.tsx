import { useState } from "react";
import { Check, ListChecks, Workflow } from "lucide-react";
import type { RoutineCard as Routine, RoutineRunCard as RoutineRun, RoutineRunStep, RoutineStepManner } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { cardDateTime } from "./cardActions";
import "./RoutineCards.css";

/** What the 流程卡's buttons can do in this window: 保存 (the user's own POST /api/routines) and 改一下
 *  (fill the composer with “改一下『名称』：”). Missing (tests, guests) renders them disabled. */
export interface RoutineCardHandlers {
  saveRoutine?: (draftId: string) => Promise<void>;
  fillPrompt?: (text: string) => void;
}

export const routineEditPrompt = (name: string) => `改一下「${name}」：`;

/** 出卡 steps carry the accent chip: they only make a card, the order still waits for 确认. */
function KindChip({ kind, label }: { kind: string; label: string }) {
  return <span className="rt-kind" data-kind={kind}>{label}</span>;
}

/** How a 提醒/画线/盯异动 step runs by the user's own words: 只给建议 or 需要时执行 (none: it just does it). */
export const ROUTINE_MANNER_WORDS: Record<RoutineStepManner, string> = { advisory: "只给建议", conditional: "需要时执行" };
export function MannerChip({ manner }: { manner?: RoutineStepManner | null }) {
  return manner ? <span className="rt-manner" data-manner={manner}>{ROUTINE_MANNER_WORDS[manner]}</span> : null;
}

/**
 * 流程卡 (我的流程, 2026-10-06): the steps the AI drafted from the user's words, each numbered and typed.
 * 保存 is the only thing that writes the routine; 改一下 hands the composer back to the user. A saved
 * card says so in place of its buttons; a refused one (免确认下单 and the like) shows why, with no 保存.
 */
export function RoutineCard({ card, id, handlers }: { card: Routine; id?: string; handlers: RoutineCardHandlers }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const refused = card.status === "refused";
  const saved = card.status === "saved";
  const save = async () => {
    if (!handlers.saveRoutine || saving) return;
    setSaving(true);
    setError("");
    try { await handlers.saveRoutine(card.draftId); }
    catch (cause) { setError(cause instanceof Error && cause.message ? cause.message : "没有保存，请重试"); }
    finally { setSaving(false); }
  };
  return (
    <CardShell
      icon={<Workflow />}
      title={`流程 ·「${card.name}」`}
      badge={refused ? "不能保存" : saved ? "已保存" : `${card.steps.length} 步`}
      badgeTone={refused ? "danger" : saved ? "positive" : "neutral"}
      phase={saved ? "done" : refused ? "dropped" : "open"}
      id={id}
      testId="routine-card"
      className={`sc-card rt-card${refused ? " sc-tone-danger" : ""}`}
      details={
        <dl className="card-facts">
          <div><dt>类型</dt><dd>{card.mode === "update" ? "改已有流程" : "新流程"}</dd></div>
          {card.savedAt ? <div><dt>保存时间</dt><dd>{cardDateTime(card.savedAt)}</dd></div> : null}
          <div><dt>卡片</dt><dd>{card.cardId}</dd></div>
        </dl>
      }
      actions={saved ? <span className="card-outcome" role="status"><Check aria-hidden="true" />已存进我的流程</span>
        : refused ? null : <>
          <button type="button" className="primary-button accent" disabled={!handlers.saveRoutine || saving}
            data-testid="save-routine" onClick={() => void save()}>{saving ? "保存中" : "保存"}</button>
          <button type="button" className="ghost-button" disabled={!handlers.fillPrompt || saving}
            title={`在输入框填入：${routineEditPrompt(card.name)}`}
            onClick={() => handlers.fillPrompt?.(routineEditPrompt(card.name))}>改一下</button>
        </>}
    >
      <ol className="rt-steps">
        {card.steps.map((step, index) => (
          <li key={`${index}-${step.text}`}>
            <span className="rt-n" aria-hidden="true">{index + 1}</span>
            <span className="rt-text">{step.text}<KindChip kind={step.kind} label={step.kindLabel} /><MannerChip manner={step.manner} /></span>
          </li>
        ))}
      </ol>
      {refused && card.refusal ? <p className="rt-refusal" role="note">{card.refusal}</p> : null}
      {card.note && !refused ? <p className="sc-sub">{card.note}</p> : null}
      {error ? <p className="rt-error" role="alert">{error}</p> : null}
    </CardShell>
  );
}

const STEP_MARK: Record<RoutineRunStep["status"], string> = {
  waiting: "", active: "", done: "✓", confirm: "!", failed: "✕", skipped: "–",
};
const STEP_WORD: Record<RoutineRunStep["status"], string> = {
  waiting: "等待", active: "进行中", done: "完成", confirm: "需你确认", failed: "失败", skipped: "没做",
};

function elapsedText(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${Math.round(seconds - minutes * 60)} 秒`;
}

/**
 * 进度卡 (我的流程, 2026-10-06): a running routine ticked off step by step — each line its state and one
 * result; a 需你确认 step made a card that waits below; a failed step stops the run (the rest 没做).
 */
export function RoutineRunCard({ card, id }: { card: RoutineRun; id?: string }) {
  const running = card.status === "running";
  const badge = running ? `第 ${Math.min(card.steps.length, card.steps.filter(step => step.status !== "waiting").length)} / ${card.steps.length} 步`
    : card.status === "completed" ? `${card.steps.length} 件事 · ${elapsedText(card.elapsedSeconds)}`
    : card.status === "cancelled" ? "已停止" : "停在半路";
  return (
    <CardShell
      icon={<ListChecks />}
      title={card.name}
      badge={badge}
      badgeTone={running ? "neutral" : card.status === "completed" ? "positive" : "warning"}
      phase={running ? "busy" : "done"}
      id={id}
      testId="routine-run-card"
      className="sc-card rt-card rt-run"
      details={
        <dl className="card-facts">
          <div><dt>开始</dt><dd>{cardDateTime(card.startedAt)}</dd></div>
          {card.finishedAt ? <div><dt>结束</dt><dd>{cardDateTime(card.finishedAt)}</dd></div> : null}
          <div><dt>用时</dt><dd>{elapsedText(card.elapsedSeconds)}</dd></div>
          <div><dt>卡片</dt><dd>{card.cardId}</dd></div>
        </dl>
      }
    >
      <ol className="rt-progress" aria-live="polite">
        {card.steps.map((step, index) => (
          <li key={`${index}-${step.text}`} data-status={step.status}>
            <span className="rt-tick" aria-label={STEP_WORD[step.status]} title={STEP_WORD[step.status]}>
              {STEP_MARK[step.status] || (step.status === "waiting" ? index + 1 : "")}
            </span>
            <span className="rt-text">
              {step.status === "done" || step.status === "confirm" || step.status === "failed" ? step.result || step.text : step.text}
              {step.status === "confirm" ? <small className="rt-confirm">需你确认 · 卡片在下面</small>
                : step.status === "failed" ? <small className="rt-fail">失败 · 后面的步骤没有做</small>
                : step.status === "active" ? <small>进行中</small>
                : step.status === "done" && step.result ? <small>{step.text}</small> : null}
            </span>
          </li>
        ))}
      </ol>
      {card.confirmCount ? <p className="sc-sub">{card.confirmCount} 步出了卡，在下面的卡上点确认才下单</p> : null}
    </CardShell>
  );
}
