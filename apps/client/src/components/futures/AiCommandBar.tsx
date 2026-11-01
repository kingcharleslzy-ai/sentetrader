import { useEffect, useRef, useState, type FormEvent } from "react";
import { Check, Sparkles, Undo2, X } from "lucide-react";
import type { AgentTask } from "@ai-trader/ui-events";
import { requestJson } from "../../api";
import type { ConversationTurn } from "../../state/conversation";
import { useDisclosureSurface } from "../../uiSurfaces";
import { pushToTalkStore, setPttTarget } from "../../pushToTalk";
import { PttButton, PttLive, type PushToTalk } from "./PushToTalk";
import "./AiCommandBar.css";

const ORDER_CARDS = new Set(["trade", "close-batch", "spread"]);
const ANSWER_CHARS = 140;

/**
 * AI 指令栏 (改版第二步, owner-approved mockup 2026-10-31): one line in the header to ask the AI or tell it what to
 * do, without opening the 对话面板. Enter sends the same message the chat sends (⌘K / Ctrl+K focuses it); the
 * result drops down under it (`disclosure: ai-command-result`): what the AI did — each step the Runtime marked as
 * an action (`step.action`: a price alert, a drawing, a screen change) with 撤销 where it can be taken back through
 * the same business call the UI uses — then the answer's first lines. Orders only ever come as cards: a card
 * points to the chat, where it is confirmed. 按住说话 sits in the bar too: the workspace's one hold (`ptt.pressTo`), its
 * words sent from here like a typed line. The hold key speaks here as well (owner 2026-11-01): it no longer swaps the
 * right panel for the chat, so the 策略编辑卡 or the tour the words are about stays on screen.
 */
export function AiCommandBar({ baseUrl, turns, busy, blockedReason, onSend, onOpenChat, ptt }: {
  baseUrl: string | undefined;
  turns: readonly ConversationTurn[];
  /** The chat is answering or sending: a new line waits (the input keeps it). */
  busy: boolean;
  /** Why the AI cannot be asked now (not connected, model not ready); null = usable. */
  blockedReason: string | null;
  /** `spoken`: held words, answered out loud like every 按住说话. */
  onSend: (text: string, spoken?: boolean) => Promise<void>;
  onOpenChat: () => void;
  /** The workspace's 按住说话; absent hides the bar's microphone. */
  ptt?: PushToTalk;
}) {
  const [text, setText] = useState("");
  const [asked, setAsked] = useState<{ text: string; after: number } | null>(null);
  const [undone, setUndone] = useState<Record<string, "busy" | "done" | string>>({});
  const input = useRef<HTMLInputElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useDisclosureSurface("ai-command-result", { available: asked !== null });

  // The answer to the line sent: the first assistant turn after the turns there were when it went out.
  const reply = asked ? turns.slice(asked.after).find(turn => turn.role === "assistant") : undefined;
  const task = reply?.cards.find(card => card.type === "task")?.payload as AgentTask | undefined;
  const actions = (task?.steps ?? []).filter(step => step.status === "done" && step.action);
  const working = Boolean(asked) && (!reply || reply.completed === false || task?.status === "running");
  const hasCard = Boolean(reply?.cards.some(card => ORDER_CARDS.has(card.type)));
  const answer = reply?.text.trim() ?? "";

  useEffect(() => {
    const focus = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", focus);
    return () => window.removeEventListener("keydown", focus);
  }, []);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open, setOpen]);

  const send = async (line: string, spoken = false) => {
    if (!line || busy || blockedReason) return;
    setAsked({ text: line, after: turns.length });
    setUndone({});
    setOpen(true);
    setText("");
    await onSend(line, spoken);
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    await send(text.trim());
  };
  // Held here, the words go out from the bar (or wait in its input while the AI is still answering).
  // 松开即发送 off (设置 → 语音): the words wait in the bar's input like a typed line.
  const latest = useRef({ busy, send, fill: ptt?.sendOnRelease === false });
  latest.current = { busy, send, fill: ptt?.sendOnRelease === false };
  const heard = (words: string) => {
    if (latest.current.busy || latest.current.fill) setText(words); else void latest.current.send(words.trim(), true);
  };
  const hasPtt = Boolean(ptt);
  useEffect(() => hasPtt ? setPttTarget(heard) : undefined, [hasPtt]);
  // Any hold shows here: the bar's microphone and the hold key both send from the bar.
  const holding = Boolean(ptt && ptt.status.state !== "idle");
  // Why a hold did nothing (not connected, too short): said here, where the words would have gone.
  const notice = ptt?.status.state === "idle" ? ptt.status.notice : undefined;
  const barPtt: PushToTalk | undefined = ptt && { ...ptt, press: () => ptt.pressTo(heard) };
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

  return <div className="ai-bar" ref={root} data-agent-target="ai-command-bar">
    <form className="ai-bar-input" role="search" aria-label="AI 指令栏" title={blockedReason ?? "问 AI（⌘K）"}
      onClick={event => { if (!(event.target as Element).closest("button")) input.current?.focus(); }} onSubmit={event => void submit(event)}>
      <Sparkles aria-hidden="true" />
      {holding && ptt ? <PttLive status={ptt.status} /> : null}
      {notice ? <small role="status" className="fdw-ptt-notice" title="点一下关闭" onClick={() => pushToTalkStore.dismiss()}>{notice}</small> : null}
      <input hidden={holding || Boolean(notice)} ref={input} aria-label="问 AI，或说一句要做的事" value={text} maxLength={500} disabled={Boolean(blockedReason)}
        placeholder={blockedReason ?? "问 AI，或说一句要做的事"} onChange={event => setText(event.target.value)}
        onFocus={() => { if (asked) setOpen(true); }} onKeyDown={event => { if (event.key === "Escape") { setOpen(false); input.current?.blur(); } }} />
      <kbd aria-hidden="true">⌘K</kbd>
      {barPtt ? <PttButton ptt={barPtt} releaseLabel="发给 AI" label="按住对 AI 说" /> : null}
    </form>
    {open && asked ? <section className="ai-bar-result" aria-label="AI 指令结果" aria-live="polite">
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
    </section> : null}
  </div>;
}
