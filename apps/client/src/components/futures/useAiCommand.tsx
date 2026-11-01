import { useEffect, useState } from "react";
import { Check, Undo2, X } from "lucide-react";
import type { AgentTask } from "@ai-trader/ui-events";
import { requestJson } from "../../api";
import type { ConversationTurn } from "../../state/conversation";
import { useDisclosureSurface } from "../../uiSurfaces";

const ORDER_CARDS = new Set(["trade", "close-batch", "spread"]);
const ANSWER_CHARS = 140;

/**
 * The AI 指令栏's result (改版第二步): the line sent, the first assistant turn after it, what the AI did — each step
 * the Runtime marked as an action (`step.action`) with 撤销 where it can be taken back through the same business call
 * the UI uses — and the answer's first lines. The desktop header bar and the phone's 回执条 (方案 B, 2026-11-01) share
 * it (the desktop shows it as `AiCommandBubble` beside the AI 球); `available` says whether this window holds the
 * `ai-command-result` disclosure.
 */
export function useAiCommand({ baseUrl, turns, busy, blockedReason, onSend, available = true }: {
  baseUrl: string | undefined;
  turns: readonly ConversationTurn[];
  /** The chat is answering or sending: a new line waits. */
  busy: boolean;
  /** Why the AI cannot be asked now; null = usable. */
  blockedReason: string | null;
  /** `spoken`: held words, answered out loud like every 按住说话. */
  onSend: (text: string, spoken?: boolean) => Promise<void>;
  available?: boolean;
}) {
  const [asked, setAsked] = useState<{ text: string; after: number } | null>(null);
  const [undone, setUndone] = useState<Record<string, "busy" | "done" | string>>({});
  const [open, setOpen] = useDisclosureSurface("ai-command-result", { available: available && asked !== null });

  // The answer to the line sent: the first assistant turn after the turns there were when it went out.
  const reply = asked ? turns.slice(asked.after).find(turn => turn.role === "assistant") : undefined;
  const task = reply?.cards.find(card => card.type === "task")?.payload as AgentTask | undefined;
  const actions = (task?.steps ?? []).filter(step => step.status === "done" && step.action);
  // A reply with a task is done when its task is (11-02: a quote answer's message.completed never came, so the
  // 指令结果 said "AI 正在处理…" for good while the task and the answer were both there); without one, by the turn.
  const working = Boolean(asked) && (!reply || (task ? task.status === "running" : reply.completed === false));
  const hasCard = Boolean(reply?.cards.some(card => ORDER_CARDS.has(card.type)));
  const answer = reply?.text.trim() ?? "";

  /** Sends the line and opens the result; false (nothing sent) while busy or blocked. */
  const send = (line: string, spoken = false) => {
    if (!line || busy || blockedReason) return false;
    setAsked({ text: line, after: turns.length });
    setUndone({});
    setOpen(true);
    void onSend(line, spoken);
    return true;
  };
  const undo = async (id: string, path: string) => {
    if (!baseUrl) return;
    setUndone(previous => ({ ...previous, [id]: "busy" }));
    try {
      await requestJson(`${baseUrl}${path}`, { method: "DELETE" });
      setUndone(previous => ({ ...previous, [id]: "done" }));
    } catch (cause) {
      setUndone(previous => ({ ...previous, [id]: cause instanceof Error ? cause.message : "没撤成，请重试" }));
    }
  };
  const clear = () => { setOpen(false); setAsked(null); };
  return { asked, open, setOpen, actions, working, hasCard, answer, undone, send, undo, clear };
}

export type AiCommand = ReturnType<typeof useAiCommand>;

/** The phone's 回执条 body (above the tabs). */
export function AiCommandResult({ command, className, target, onOpenChat }: { command: AiCommand; className: string; target?: string; onOpenChat: () => void }) {
  const { asked, actions, working, hasCard, answer, undone, undo, setOpen } = command;
  if (!asked) return null;
  return <section className={className} aria-label="AI 指令结果" aria-live="polite" data-agent-target={target}>
    <header>
      <span className="ai-bar-asked" title={asked.text}>{asked.text}</span>
      <button type="button" aria-label="收起 AI 指令结果" onClick={() => setOpen(false)}><X /></button>
    </header>
    {working && !actions.length ? <p className="ai-bar-status" role="status">AI 正在处理…</p> : null}
    {actions.length ? <>
      <p className="ai-bar-status">AI 做了 {actions.length} 件事{working ? "，还在继续…" : ""}</p>
      <ul className="ai-bar-actions">
        {actions.map(step => {
          const action = step.action!;
          const state = undone[step.id];
          return <li key={step.id} data-undone={state === "done" || undefined}>
            <Check aria-hidden="true" />
            <span>{action.text}</span>
            {action.undo ? state === "done" ? <em>已撤销</em>
              : <button type="button" disabled={state === "busy"} onClick={() => void undo(step.id, action.undo!.path)}><Undo2 aria-hidden="true" />撤销</button> : null}
            {state && state !== "busy" && state !== "done" ? <small role="alert">{state}</small> : null}
          </li>;
        })}
      </ul>
    </> : null}
    {answer && !working ? <p className="ai-bar-answer">{answer.length > ANSWER_CHARS ? `${answer.slice(0, ANSWER_CHARS)}…` : answer}</p> : null}
    {hasCard ? <p className="ai-bar-card">有一张确认卡，确认后才会下单。</p> : null}
    <footer>
      <button type="button" className="ai-bar-chat" onClick={() => { setOpen(false); onOpenChat(); }}>{hasCard ? "到对话里确认" : "在对话里看全文"}</button>
    </footer>
  </section>;
}

/** How long the desktop bubble stays (owner 2026-11-01): a plain result fades on its own; one with a card to confirm
 *  or something to undo waits longer. A pointer or focus on it holds it. */
const LINGER_MS = 8_000;
const LINGER_LONG_MS = 20_000;
const FADE_MS = 300;
const BUBBLE_CHARS = 60;

/**
 * The AI 指令栏's result on the desktop, a small bubble beside the AI 球 (owner 2026-11-01, replacing the drop-down
 * under the bar): one line — what the AI did, joined, with one 撤销 for all that can be taken back; a waiting order
 * card; or the answer's first words — and a button into the chat for the rest. Orders are confirmed in the chat.
 * Fades after LINGER_MS (LINGER_LONG_MS with a card or an undo) unless the pointer or focus is on it. Keyed per line
 * sent, so its timer and hold start over.
 */
export function AiCommandBubble({ command, failed, onOpenChat }: {
  command: AiCommand;
  /** The send itself failed (the chat's own line, e.g. 发送未确认): said here instead of waiting forever. */
  failed?: string;
  onOpenChat: () => void;
}) {
  const { asked, open, setOpen, actions, hasCard, answer, undone, undo } = command;
  const [held, setHeld] = useState(false);
  const [fading, setFading] = useState(false);
  const lost = Boolean(failed) && command.working && !answer && !actions.length;
  const working = command.working && !lost;
  const undoable = actions.filter(step => step.action!.undo);
  const linger = hasCard || undoable.length ? LINGER_LONG_MS : LINGER_MS;
  useEffect(() => {
    if (!open || working || held) return;
    const fade = window.setTimeout(() => setFading(true), linger);
    const close = window.setTimeout(() => setOpen(false), linger + FADE_MS);
    return () => { window.clearTimeout(fade); window.clearTimeout(close); setFading(false); };
  }, [open, working, held, linger, setOpen]);
  if (!open || !asked) return null;

  const states = undoable.map(step => undone[step.id]);
  const allUndone = undoable.length > 0 && states.every(state => state === "done");
  const busy = states.includes("busy");
  const error = states.find(state => state && state !== "busy" && state !== "done");
  const undoAll = async () => { for (const step of undoable) if (undone[step.id] !== "done") await undo(step.id, step.action!.undo!.path); };
  const did = actions.map(step => step.action!.text).join("；");
  const line = actions.length ? `${allUndone ? "已撤销：" : ""}${did}${working ? "，还在继续…" : ""}`
    : lost ? failed!
    : working ? "AI 正在处理…"
    : hasCard ? "有一张下单确认卡，确认后才会下单"
    : answer.length > BUBBLE_CHARS ? `${answer.slice(0, BUBBLE_CHARS)}…` : answer;
  const tone = allUndone ? "undone" : working ? "working" : hasCard || lost ? "card" : actions.length ? "done" : "answer";
  const more = hasCard ? "在对话里确认" : actions.length > 1 ? "在对话里看明细" : !actions.length && !working && !lost ? "看全文" : "";
  const hold = () => setHeld(true);
  const release = () => setHeld(false);

  return <section className="ai-orb-result" aria-label="AI 指令结果" aria-live="polite" data-agent-target="ai-command-result"
    data-tone={tone} data-fading={fading || undefined} title={asked.text}
    onPointerEnter={hold} onPointerLeave={release} onFocus={hold} onBlur={release}>
    <p>
      <i aria-hidden="true" />
      <span role={working ? "status" : undefined}>{line}</span>
      {undoable.length && !allUndone && !working ? <button type="button" className="app-button" disabled={busy} onClick={() => void undoAll()}><Undo2 aria-hidden="true" />撤销</button> : null}
    </p>
    {actions.length && hasCard ? <p className="ai-orb-result-card">有一张下单确认卡，确认后才会下单</p> : null}
    {error ? <small role="alert">{error}</small> : null}
    {more ? <footer><button type="button" className={hasCard ? "app-button primary" : "app-button"} onClick={() => { setOpen(false); onOpenChat(); }}>{more}</button></footer> : null}
  </section>;
}
