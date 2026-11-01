import { useEffect, useRef, useState, type FormEvent } from "react";
import { Sparkles } from "lucide-react";
import { useFitHeight } from "../../hooks/useFitHeight";
import { pushToTalkStore, setPttTarget } from "../../pushToTalk";
import { PttButton, PttLive, type PushToTalk } from "./PushToTalk";
import "./AiCommandBar.css";

/**
 * AI 指令栏 (改版第二步, owner-approved mockup 2026-10-31): one line in the header to ask the AI or tell it what to
 * do, without opening the 对话面板. Enter sends the same message the chat sends, Shift+Enter breaks the line (⌘K /
 * Ctrl+K focuses it); focused, the input grows down over the page (up to six lines) and the header keeps its height.
 * What came of the line shows beside the AI 球 (`AiCommandBubble`, the workspace's `useAiCommand`), not here.
 * 按住说话 sits in the bar too: the workspace's one hold (`ptt.pressTo`), its words sent from here like a typed line.
 * The hold key speaks here while the 对话面板 is closed (so the 策略编辑卡 or the tour the words are about stays on
 * screen) and into the panel's input while it is open (owner 2026-11-01); the hold shows only where its words go.
 */
export function AiCommandBar({ busy, blockedReason, send: sendLine, chatOpen, ptt }: {
  /** The chat is answering or sending: a new line waits (the input keeps it). */
  busy: boolean;
  /** Why the AI cannot be asked now (not connected, model not ready); null = usable. */
  blockedReason: string | null;
  /** `useAiCommand().send`: false when nothing went out. `spoken`: held words, answered out loud. */
  send: (text: string, spoken?: boolean) => boolean;
  /** The 对话面板 is open: the hold key speaks there instead of here. */
  chatOpen: boolean;
  /** The workspace's 按住说话; absent hides the bar's microphone. */
  ptt?: PushToTalk;
}) {
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const input = useFitHeight(text, focused);

  useEffect(() => {
    const focus = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", focus);
    return () => window.removeEventListener("keydown", focus);
  }, [input]);

  const send = (line: string, spoken = false) => { if (sendLine(line, spoken)) setText(""); };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    send(text.trim());
  };
  // Held here, the words go out from the bar (or wait in its input while the AI is still answering).
  // 松开即发送 off (设置 → 语音): the words wait in the bar's input like a typed line.
  const latest = useRef({ busy, send, fill: ptt?.sendOnRelease === false });
  latest.current = { busy, send, fill: ptt?.sendOnRelease === false };
  // One function for the bar's life: the hold's `target` names it, so the bar knows a hold is its own.
  const heard = useRef((words: string) => {
    if (latest.current.busy || latest.current.fill) setText(words); else latest.current.send(words.trim(), true);
  }).current;
  const hasPtt = Boolean(ptt);
  useEffect(() => hasPtt && !chatOpen ? setPttTarget(heard) : undefined, [hasPtt, chatOpen, heard]);
  const mine = ptt?.status.target === heard;
  const holding = Boolean(ptt && mine && ptt.status.state !== "idle");
  // Why a hold did nothing (not connected, too short): said here, where the words would have gone.
  const notice = mine && ptt?.status.state === "idle" ? ptt.status.notice : undefined;
  const barPtt: PushToTalk | undefined = ptt && { ...ptt, press: () => ptt.pressTo(heard) };

  return <div className="ai-bar" data-agent-target="ai-command-bar">
    <form className="ai-bar-input" role="search" aria-label="AI 指令栏" title={blockedReason ?? "问 AI（⌘K）"}
      onClick={event => { if (!(event.target as Element).closest("button")) input.current?.focus(); }} onSubmit={submit}>
      <Sparkles aria-hidden="true" />
      {holding && ptt ? <PttLive status={ptt.status} /> : null}
      {notice ? <small role="status" className="fdw-ptt-notice" title="点一下关闭" onClick={() => pushToTalkStore.dismiss()}>{notice}</small> : null}
      <textarea hidden={holding || Boolean(notice)} ref={input} rows={1} aria-label="问 AI，或说一句要做的事" value={text} maxLength={500} disabled={Boolean(blockedReason)}
        placeholder={blockedReason ?? "问 AI，或说一句要做的事"} onChange={event => setText(event.target.value)}
        onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
        onKeyDown={event => {
          if (event.key === "Escape") { input.current?.blur(); return; }
          // Not the Enter that picks an input method's candidate (keyCode 229 in Safari).
          if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229) return;
          event.preventDefault();
          event.currentTarget.form?.requestSubmit();
        }} />
      <kbd aria-hidden="true">⌘K</kbd>
      {barPtt ? <PttButton ptt={barPtt} releaseLabel="发给 AI" label="按住对 AI 说" /> : null}
    </form>
  </div>;
}
