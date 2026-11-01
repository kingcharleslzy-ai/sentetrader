import { useState } from "react";
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
 * it; `available` says which one holds the `ai-command-result` disclosure in this window.
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
  const working = Boolean(asked) && (!reply || reply.completed === false || task?.status === "running");
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

/** The result drop-down's body (desktop under the bar, phone above the tabs). */
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
