import { Mic } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { currentPttTarget, pushToTalkStore, usePushToTalkStatus, type PttStatus, type PttCaptureFactory } from "../../pushToTalk";
import { xiaomiRemoteStore } from "../../xiaomiRemote/store";
import { runRemoteAction } from "../../xiaomiRemote/actions";
import { holdKeyLabel, isModifierCode, useShortcuts } from "../../shortcuts";

interface Options {
  guest?: boolean;
  /** The connected Runtime; undefined disables push-to-talk. */
  baseUrl: string | undefined;
  /** Why push-to-talk cannot start right now (a call is running, voice is off, the model is not ready); "" when it can. */
  blockedReason: string;
  /** `voice.pttSendOnRelease`: send the text as the user's message, or put it into the composer. */
  sendOnRelease: boolean;
  onSend: (text: string) => void;
  onFill: (text: string) => void;
  /** The hold key went down: the workspace opens the 对话面板 (recording does not wait for it). */
  onKeyPress?: () => void;
  /** The chat shown at the press. Words recognised after a switch to another chat are put into
   *  the input box instead of being sent: never to a chat the user has left or did not speak to. */
  chatSessionId?: string;
}

export interface PushToTalk {
  status: PttStatus;
  blockedReason: string;
  keyLabel: string;
  sendOnRelease: boolean;
  press: () => void;
  /** A hold whose words go to `target` instead of the chat (the phone's 策略编辑卡 补一句, 2026-10-08). */
  pressTo: (target: (text: string) => void) => void;
}

/**
 * 按住说话, owned once by the workspace: the hold key (设置 → 快捷键, default right Alt / right
 * Option) works while this window has focus whether or not the 对话面板 is open — it opens the
 * panel and starts recording in the same moment, so the first words are kept. The composer's
 * button drives the same store. A lone modifier also works while typing, an ordinary key only
 * outside text fields. A blocked key (call running, not connected, voice off, model not ready)
 * records nothing and leaves the reason in the panel's input row. Esc or the window losing focus
 * cancels a hold; a call starting cancels it too.
 */
export function usePushToTalk({ baseUrl, blockedReason, sendOnRelease, onSend, onFill, onKeyPress, guest, chatSessionId }: Options): PushToTalk {
  const status = usePushToTalkStatus();
  const binding = useShortcuts().ptt;
  const latest = useRef({ baseUrl, blockedReason, sendOnRelease, onSend, onFill, onKeyPress, guest, chatSessionId });
  latest.current = { baseUrl, blockedReason, sendOnRelease, onSend, onFill, onKeyPress, guest, chatSessionId };
  const start = useCallback((fromKey: boolean, explicit?: (text: string) => void, input?: PttCaptureFactory) => {
    if (input && pushToTalkStore.snapshot().state !== "idle") return false;
    const { baseUrl: url, blockedReason: blocked, chatSessionId: pressedChat } = latest.current;
    // Only the hold key answers a surface that asks (the guide card): a hold on the composer's own
    // microphone speaks to the chat it sits under (10-08: it answered an open guide card instead).
    // A button that names its own target (the phone editor's 补一句 microphone) gets the words.
    const target = explicit ?? (fromKey && !latest.current.guest ? currentPttTarget() : undefined);
    if (!url || blocked) {
      if (fromKey) pushToTalkStore.showNotice(blocked || "连接后可用", target);
    } else {
      const final = (text: string) => {
        if (target) return target(text);
        const { sendOnRelease: send, onSend: sendText, onFill: fill, chatSessionId: shownChat } = latest.current;
        (send && shownChat === pressedChat ? sendText : fill)(text);
      };
      void pushToTalkStore.press(url, final, latest.current.guest, input, target);
    }
    // After the store has started (its microphone is already opening): the panel renders next.
    // A registered target (the guide card) keeps the words where they are asked for.
    if (fromKey && !target) latest.current.onKeyPress?.();
    return Boolean(url && !blocked && pushToTalkStore.snapshot().state === "recording");
  }, []);
  const press = useCallback(() => start(false), [start]);
  const pressTo = useCallback((target: (text: string) => void) => start(false, target), [start]);
  useEffect(() => {
    if (guest) { xiaomiRemoteStore.disconnect(); return; }
    let owned: number | undefined;
    const owns = () => owned !== undefined && pushToTalkStore.captureId() === owned;
    const off = xiaomiRemoteStore.onInput(event => {
      if (event.type === "cancel") { if (owns()) pushToTalkStore.cancel(); owned = undefined; return; }
      if (event.type === "stop") { if (owns()) pushToTalkStore.release(); owned = undefined; return; }
      if (!document.hasFocus() || document.visibilityState === "hidden") return;
      if (event.type === "start") {
        if (start(true, undefined, event.input)) owned = pushToTalkStore.captureId();
      } else if (event.action === "cancel") pushToTalkStore.cancel();
      else runRemoteAction(event.action);
    });
    const blur = () => { if (owns()) pushToTalkStore.cancel(); owned = undefined; };
    window.addEventListener("blur", blur);
    window.addEventListener("pagehide", blur);
    const hidden = () => { if (document.visibilityState === "hidden") blur(); };
    document.addEventListener("visibilitychange", hidden);
    return () => { off(); blur(); window.removeEventListener("blur", blur); window.removeEventListener("pagehide", blur); document.removeEventListener("visibilitychange", hidden); };
  }, [start, guest, baseUrl]);
  useEffect(() => {
    const editing = (target: EventTarget | null) => target instanceof HTMLElement
      && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
    const onKeyDown = (event: KeyboardEvent) => {
      // Esc cancels the hold and also the transcription still on its way (#523): nothing is sent or filled.
      if (event.key === "Escape" && pushToTalkStore.snapshot().state !== "idle") { pushToTalkStore.cancel(); return; }
      if (event.code !== binding || (!isModifierCode(binding) && editing(event.target))) return;
      if (event.target instanceof HTMLElement && event.target.closest("dialog[open]")) return;
      // Windows focuses the menu bar on a lone Alt; the hold key is ours inside the window.
      event.preventDefault();
      if (!event.repeat) start(true);
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code !== binding) return;
      event.preventDefault();
      pushToTalkStore.release();
    };
    const onBlur = () => pushToTalkStore.cancel();
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
  }, [binding, start]);
  // A call that starts (or voice going away) mid-hold ends the hold; so does leaving the workspace.
  useEffect(() => { if (blockedReason) pushToTalkStore.cancel(); }, [blockedReason]);
  useEffect(() => () => pushToTalkStore.cancel(), []);
  return { status, blockedReason, keyLabel: holdKeyLabel(binding), sendOnRelease, press, pressTo };
}

/** The microphone button beside 发送: press and hold (mouse, touch or Space), release to finish. */
export function PttButton({ ptt, releaseLabel, label = "按住说话" }: { ptt: PushToTalk; /** What releasing does, when not the composer's own. */ releaseLabel?: string;
  /** Its name where another 按住说话 button shows at the same time (the AI 指令栏's). */ label?: string }) {
  const { status, blockedReason, keyLabel, sendOnRelease, press } = ptt;
  const active = status.state !== "idle";
  const title = blockedReason || `按住说话（或按住${keyLabel}），松开${releaseLabel ?? (sendOnRelease ? "直接发送" : "填入输入框")}；Esc 取消`;
  const onKey = (event: ReactKeyboardEvent<HTMLButtonElement>, down: boolean) => {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    if (down && !event.repeat) press();
    else if (!down) pushToTalkStore.release();
  };
  return (
    <button type="button" className={`fdw-ptt-button${active ? " active" : ""}`} aria-label={label} title={title}
      aria-pressed={status.state === "recording"} disabled={Boolean(blockedReason) && !active}
      onPointerDown={event => { if (event.button > 0) return; event.preventDefault(); event.currentTarget.setPointerCapture?.(event.pointerId); press(); }}
      onPointerUp={() => pushToTalkStore.release()} onPointerCancel={() => pushToTalkStore.cancel()}
      onKeyDown={event => onKey(event, true)} onKeyUp={event => onKey(event, false)} onContextMenu={event => event.preventDefault()}>
      <Mic />
    </button>
  );
}

const BARS = [0.45, 0.75, 1, 0.75, 0.45];

/** In place of the input while holding / transcribing: level bars, the hold's timer, the words so far.
 *  Recording: the bars follow the microphone from a visible floor (the button beats beside them);
 *  transcribing: they ripple in turn until the words come back (CSS; still under reduced motion). */
export function PttLive({ status }: { status: PttStatus }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (status.state !== "recording") return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [status.state]);
  const seconds = status.startedAt ? Math.max(0, Math.floor((now - status.startedAt) / 1000)) : 0;
  const recording = status.state === "recording";
  return (
    <div className={`fdw-ptt-live ${status.state}`} role="status" aria-live="polite" data-agent-target="ptt-status" data-state={status.state}>
      <span className="fdw-ptt-meter" aria-hidden="true">
        {BARS.map((weight, index) => <i key={index} style={recording
          ? { transform: `scaleY(${Math.max(0.15, Math.min(1, (0.25 + status.level * 1.6) * weight)).toFixed(3)})` }
          : { animationDelay: `${index * 120}ms` }} />)}
      </span>
      <span className="fdw-ptt-timer">{`${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`}</span>
      <span className="fdw-ptt-text">{status.partial || (recording ? status.notice || "正在听，松开结束" : "识别中…")}</span>
    </div>
  );
}
