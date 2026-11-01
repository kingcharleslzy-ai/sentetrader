import { useEffect, useRef, useState, type FormEvent } from "react";
import { Sparkles } from "lucide-react";
import type { ConversationTurn } from "../../state/conversation";
import { pushToTalkStore, setPttTarget } from "../../pushToTalk";
import { PttButton, PttLive, type PushToTalk } from "./PushToTalk";
import { AiCommandResult, useAiCommand } from "./useAiCommand";
import "./AiCommandBar.css";

/**
 * AI 指令栏 (改版第二步, owner-approved mockup 2026-10-31): one line in the header to ask the AI or tell it what to
 * do, without opening the 对话面板. Enter sends the same message the chat sends (⌘K / Ctrl+K focuses it); the
 * result drops down under it (`disclosure: ai-command-result`, `useAiCommand`). Orders only ever come as cards: a card
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
  const input = useRef<HTMLInputElement>(null);
  const root = useRef<HTMLDivElement>(null);
  const command = useAiCommand({ baseUrl, turns, busy, blockedReason, onSend });
  const { asked, open, setOpen } = command;

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

  const send = (line: string, spoken = false) => { if (command.send(line, spoken)) setText(""); };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    send(text.trim());
  };
  // Held here, the words go out from the bar (or wait in its input while the AI is still answering).
  // 松开即发送 off (设置 → 语音): the words wait in the bar's input like a typed line.
  const latest = useRef({ busy, send, fill: ptt?.sendOnRelease === false });
  latest.current = { busy, send, fill: ptt?.sendOnRelease === false };
  const heard = (words: string) => {
    if (latest.current.busy || latest.current.fill) setText(words); else latest.current.send(words.trim(), true);
  };
  const hasPtt = Boolean(ptt);
  useEffect(() => hasPtt ? setPttTarget(heard) : undefined, [hasPtt]);
  // Any hold shows here: the bar's microphone and the hold key both send from the bar.
  const holding = Boolean(ptt && ptt.status.state !== "idle");
  // Why a hold did nothing (not connected, too short): said here, where the words would have gone.
  const notice = ptt?.status.state === "idle" ? ptt.status.notice : undefined;
  const barPtt: PushToTalk | undefined = ptt && { ...ptt, press: () => ptt.pressTo(heard) };
  return <div className="ai-bar" ref={root} data-agent-target="ai-command-bar">
    <form className="ai-bar-input" role="search" aria-label="AI 指令栏" title={blockedReason ?? "问 AI（⌘K）"}
      onClick={event => { if (!(event.target as Element).closest("button")) input.current?.focus(); }} onSubmit={submit}>
      <Sparkles aria-hidden="true" />
      {holding && ptt ? <PttLive status={ptt.status} /> : null}
      {notice ? <small role="status" className="fdw-ptt-notice" title="点一下关闭" onClick={() => pushToTalkStore.dismiss()}>{notice}</small> : null}
      <input hidden={holding || Boolean(notice)} ref={input} aria-label="问 AI，或说一句要做的事" value={text} maxLength={500} disabled={Boolean(blockedReason)}
        placeholder={blockedReason ?? "问 AI，或说一句要做的事"} onChange={event => setText(event.target.value)}
        onFocus={() => { if (asked) setOpen(true); }} onKeyDown={event => { if (event.key === "Escape") { setOpen(false); input.current?.blur(); } }} />
      <kbd aria-hidden="true">⌘K</kbd>
      {barPtt ? <PttButton ptt={barPtt} releaseLabel="发给 AI" label="按住对 AI 说" /> : null}
    </form>
    {open && asked ? <AiCommandResult command={command} className="ai-bar-result" onOpenChat={onOpenChat} /> : null}
  </div>;
}
