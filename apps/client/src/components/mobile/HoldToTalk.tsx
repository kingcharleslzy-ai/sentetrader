import { Mic } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { PTT_MIN_HOLD_MS, pushToTalkStore, type PttStatus } from "../../pushToTalk";
import type { PushToTalk } from "../futures/PushToTalk";

/** A finger that slides this far up while holding cancels the words on release. */
export const SLIDE_CANCEL_PX = 60;
export { PTT_TOO_SHORT } from "../../pushToTalk";
export const PTT_SLID_AWAY = "已取消，这句没有发送";

/**
 * 手机版按住说话 (2026-10-04): the workspace's push-to-talk (the same store, socket and
 * transcription as the desktop composer's microphone) on a thumb-sized round button, beside the
 * input on AI 动态 and in the chat's composer (10-05). Press records,
 * release sends, sliding the finger up past SLIDE_CANCEL_PX arms a cancel that the release then
 * takes; a hold under PTT_MIN_HOLD_MS says it was too short (the store's PTT_TOO_SHORT). A blocked hold
 * (call running, not connected, model not ready) records nothing and shows the reason.
 * 方案 B (2026-11-01): the button in the middle of the bottom tabs, on every page; with `onTap` a tap (a release
 * under PTT_MIN_HOLD_MS) opens typing instead of saying it was too short.
 */
export function HoldToTalk({ ptt, onSlideCancelChange, onTap }: {
  ptt: PushToTalk;
  /** Whether a release right now would cancel (the live strip says “松开手指，取消发送”). */
  onSlideCancelChange: (armed: boolean) => void;
  /** A tap rather than a hold: open typing. */
  onTap?: () => void;
}) {
  const { status, blockedReason, press } = ptt;
  const startY = useRef<number | null>(null);
  // With `onTap`: when the key went down, so a quick release opens typing even when the voice could not start
  // (blocked, no microphone in this browser).
  const pressedAt = useRef<number | null>(null);
  // The ref is what a release reads (it can land before the re-render a slide caused).
  const armedRef = useRef(false);
  const [armed, setArmedState] = useState(false);
  const setArmed = (value: boolean) => { armedRef.current = value; setArmedState(value); };
  const active = status.state !== "idle";
  useEffect(() => { onSlideCancelChange(armed); }, [armed, onSlideCancelChange]);
  // The hold ended some other way (a call started, the window lost focus): forget the slide.
  useEffect(() => { if (status.state !== "recording") { startY.current = null; armedRef.current = false; setArmedState(false); } }, [status.state]);
  const begin = () => {
    if (onTap) pressedAt.current = Date.now();
    if (blockedReason) { pushToTalkStore.showNotice(blockedReason); return false; }
    pushToTalkStore.dismiss();
    press();
    return true;
  };
  const finish = (cancel: boolean) => {
    const current = pushToTalkStore.snapshot();
    const tapped = Boolean(onTap) && !cancel && pressedAt.current !== null && Date.now() - pressedAt.current < PTT_MIN_HOLD_MS;
    pressedAt.current = null;
    startY.current = null;
    setArmed(false);
    if (tapped) { pushToTalkStore.cancel(); pushToTalkStore.dismiss(); onTap!(); return; }
    if (current.state !== "recording") return;
    if (cancel) { pushToTalkStore.cancel(); pushToTalkStore.showNotice(PTT_SLID_AWAY); return; }
    pushToTalkStore.release(); // a hold under PTT_MIN_HOLD_MS ends there as 说话太短
  };
  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button > 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    if (begin()) startY.current = event.clientY;
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (startY.current === null) return;
    setArmed(startY.current - event.clientY > SLIDE_CANCEL_PX);
  };
  const onKey = (event: ReactKeyboardEvent<HTMLButtonElement>, down: boolean) => {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    if (down && !event.repeat) begin();
    else if (!down) finish(false);
  };
  return (
    <button type="button" className={`fdm-hold${active ? " active" : ""}${armed ? " cancel" : ""}`}
      aria-label={onTap ? "按住说话，点一下打字" : "按住说话，松开发送"} title={blockedReason || (onTap ? "按住说话，松开发送，上滑取消；点一下打字" : "按住说话，松开发送，上滑取消")} aria-pressed={status.state === "recording"}
      aria-disabled={Boolean(blockedReason) && !active} data-agent-target="mobile-hold-to-talk"
      onPointerDown={onPointerDown} onPointerMove={onPointerMove}
      onPointerUp={() => finish(armedRef.current)} onPointerCancel={() => finish(true)}
      onKeyDown={event => onKey(event, true)} onKeyUp={event => onKey(event, false)} onContextMenu={event => event.preventDefault()}>
      <Mic aria-hidden="true" />
    </button>
  );
}

const WAVE = [0.3, 0.55, 0.85, 0.45, 1, 0.65, 0.35, 0.8, 0.95, 0.5, 0.75, 0.4, 0.6, 0.3, 0.5, 0.25];

/** Above the button while holding / transcribing: the sound wave, what releasing does, the words so far. */
export function HoldToTalkLive({ status, slideCancel, context }: { status: PttStatus; slideCancel: boolean;
  /** 按哪一页理解 (方案 B): the page the words are about. */
  context?: string }) {
  if (status.state === "idle") return null;
  const recording = status.state === "recording";
  const line = recording ? slideCancel ? "松开手指，取消发送" : status.notice || "正在听… 松开发送，上滑取消" : "识别中…";
  return (
    <div className={`fdm-live${slideCancel ? " cancel" : ""}`} role="status" aria-live="polite" data-agent-target="ptt-status" data-state={status.state}>
      <span className="fdm-wave" aria-hidden="true">
        {WAVE.map((weight, index) => <i key={index} style={{ transform: `scaleY(${recording ? Math.max(0.12, Math.min(1, (0.25 + status.level * 1.6) * weight)) : 0.12})` }} />)}
      </span>
      {context ? <span className="fdm-context">按 {context} 理解</span> : null}
      {status.partial ? <span className="fdm-live-text">{status.partial}</span> : null}
      <strong>{line}</strong>
    </div>
  );
}
