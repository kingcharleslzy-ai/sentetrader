import { AccountAccessContext } from "../../hooks/useAccountAccess";
import { Mic, MicOff, Phone, PhoneOff, RotateCcw, X } from "lucide-react";
import { useContext, useEffect, useRef, useState } from "react";
import { CLIENT_BUILD } from "../../clientBuild";
import { callDuration, type VoiceCallControls } from "../../voiceCallControls";
import { orbLabel, voiceCallStore, type VoiceCallStatus } from "../../voiceRealtime";
import { HelpTip } from "../Blocks";

const WAVE_BARS = 16;
type Speaker = "me" | "ai" | "idle";
const SPEAKER_TEXT: Record<Speaker, string> = { me: "你在说", ai: "AI 在说", idle: "在听" };
/** Bars taper towards both ends, like the site's call strip. */
const envelope = (index: number) => Math.sin(Math.PI * (index + .5) / WAVE_BARS) ** .7;
const reducedMotion = () => typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);

/** RMS of an analyser's current window, scaled so speech (≈0.04–0.2) fills most of the bar. */
function analyserLevel(node: AnalyserNode | undefined, buffer: Float32Array<ArrayBuffer>): number {
  if (!node) return 0;
  node.getFloatTimeDomainData(buffer);
  let sum = 0;
  for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
  return Math.min(1, Math.sqrt(Math.sqrt(sum / buffer.length) / 0.25));
}

/**
 * The live call's waveform strip: bars scroll with the real loudness — the microphone's
 * (--wave-me) while you talk, the AI's playback (--wave-ai) while it answers — read from two
 * AnalyserNodes on the call's own AudioContext. Visual only (aria-hidden); the call state stays
 * the status line beside it. Reduced motion: a still level from the status' microphone level.
 */
function CallWave({ status }: { status: VoiceCallStatus }) {
  const bars = useRef<HTMLSpanElement>(null);
  const latest = useRef(status);
  latest.current = status;
  const still = reducedMotion();
  const fallback: Speaker = status.state === "speaking" ? "ai" : (status.level ?? 0) > 0.2 ? "me" : "idle";
  const [speaker, setSpeaker] = useState<Speaker>(fallback);
  useEffect(() => {
    if (still) return;
    const root = bars.current;
    if (!root || typeof requestAnimationFrame !== "function") return;
    const items = Array.from(root.children) as HTMLElement[];
    const history: number[] = new Array(WAVE_BARS).fill(0);
    const buffer = new Float32Array(512);
    let frame = 0, last = 0, shown: Speaker | "" = "";
    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      if (now - last < 50) return; // ~20 steps a second
      last = now;
      const current = latest.current;
      const { input, output } = voiceCallStore.analysers();
      const ai = output ? analyserLevel(output, buffer) : current.state === "speaking" ? .5 : 0;
      const me = current.muted ? 0 : input ? analyserLevel(input, buffer) : current.level ?? 0;
      const who: Speaker = ai > .12 || current.state === "speaking" ? "ai" : me > .2 ? "me" : "idle";
      if (who !== shown) { shown = who; setSpeaker(who); }
      history.shift();
      history.push(who === "ai" ? ai : me);
      items.forEach((item, index) => { item.style.transform = `scaleY(${Math.max(.08, history[index] * envelope(index)).toFixed(3)})`; });
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [still]);
  const who = still ? fallback : speaker;
  const level = status.state === "speaking" ? .5 : status.muted ? 0 : status.level ?? 0;
  const talking = (status.state === "listening" && !status.muted) || status.state === "speaking";
  return <>
    {talking ? <span className="fdw-call-who" aria-hidden="true" data-who={who}>{SPEAKER_TEXT[who]}</span> : null}
    <span className="fdw-call-wave" aria-hidden="true" data-who={who} ref={bars}>
      {Array.from({ length: WAVE_BARS }, (_, index) => <i key={index}
        style={still ? { transform: `scaleY(${Math.max(.08, level * envelope(index)).toFixed(3)})` } : undefined} />)}
    </span>
  </>;
}

/**
 * The 对话面板's call controls. Idle: one green 通话 pill in the header (`slot="header"`) — a
 * single click connects, no press-and-hold — with the reason line under the header when voice is
 * off. In a call (or after an error) the full-width bar under the header (`slot="bar"`) shows the
 * live waveform (who is talking), mm:ss, mute and a red 结束通话 pill; an error shows 重试 and ✕
 * (关闭, back to idle). It drives the same `voiceCallStore` as
 * the floating orb and shows the same state text (orbLabel); the orb owns the `voice-call` /
 * `voice-mute` surfaces.
 */
export function CallBar({ call, unavailableReason, slot }: { call: VoiceCallControls; unavailableReason?: string; slot: "header" | "bar" }) {
  const access = useContext(AccountAccessContext);
  const blocked = access?.reason("voice-call");
  const { status, inCall, available, start, stop, mute, retry, dismiss } = call;
  const connecting = status.state === "connecting";
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!status.startedAt) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [status.startedAt]);
  const idle = status.state !== "error" && !inCall;
  if (slot === "header") {
    return idle ? <div className="fdw-call-bar" data-agent-target="call-bar" data-state="idle">
      <button type="button" data-agent-target="call-start" className="fdw-call-start" disabled={!available && !blocked} aria-label="开始语音通话"
        title={blocked ?? (available ? "点一下接通语音通话（与 AI 球同一路通话）" : unavailableReason || "当前不可用")} onClick={() => { if (access?.require("voice-call") ?? true) start(); }}>
        <Phone />通话
      </button>
    </div> : null;
  }
  if (idle) return !available && unavailableReason ? <small className="fdw-call-hint">{unavailableReason}</small> : null;
  if (status.state === "error") {
    return (
      <div className="fdw-call-bar" data-agent-target="call-bar" data-state="error">
        <p role="alert">{status.error || "语音通话出错"}</p>
        <button type="button" className="fdw-call-retry" onClick={() => { if (access?.require("voice-call") ?? true) retry(); }}><RotateCcw />重试</button>
        {/* A machine without a microphone can never retry its way out: closing goes back to idle. */}
        <button type="button" className="fdw-call-dismiss" aria-label="关闭" title="关闭" onClick={dismiss}><X /></button>
      </div>
    );
  }
  return (
    <div className="fdw-call-bar" data-agent-target="call-bar" data-state={connecting ? "connecting" : "live"}>
      <div className="fdw-call-live">
        <CallWave status={status} />
        {/* The orb's sentence stays the bar's status; while someone talks the speaker label shows instead. */}
        <span className="fdw-call-state" role="status" data-shadowed={(status.state === "listening" && !status.muted) || status.state === "speaking" ? "" : undefined}>{orbLabel(status)}</span>
        <time className="fdw-call-time">{connecting ? "00:00" : callDuration(status.startedAt, now)}</time>
        <button type="button" className="fdw-call-mute" aria-pressed={Boolean(status.muted)}
          aria-label={status.muted ? "取消静音" : "静音"} onClick={() => mute(!status.muted)}>
          {status.muted ? <MicOff /> : <Mic />}
        </button>
      </div>
      <button type="button" data-agent-target="call-end" className="fdw-call-end" aria-label="结束语音通话" onClick={stop}><PhoneOff />结束通话</button>
      {status.micSilent && status.state === "listening" && !status.muted ? <MicSilentHint label={status.micLabel} suggestion={status.micSuggestion} /> : null}
    </div>
  );
}

/** The live bar's 没有收到麦克风声音 line: the microphone in use, what to do now, the how-to behind its "?". The
 *  desktop app has no address bar: its microphone is the system's input device (#875 review). */
function MicSilentHint({ label, suggestion }: { label?: string; suggestion?: string }) {
  const desktop = CLIENT_BUILD.platform === "desktop";
  const where = desktop ? "到系统声音设置的输入设备里" : "到地址栏左侧的网站设置里";
  return <p className="fdw-call-mic" role="status" data-agent-target="call-mic-silent">
    <span>没有收到麦克风声音{label ? `（当前麦克风：${label}）` : ""}</span>
    <span>{suggestion ? `${where}换成「${suggestion}」` : `${where}换麦克风，蓝牙耳机选带“免提 / Hands-Free / Headset”的那一项`}</span>
    <HelpTip helpKey="voice-mic-silent" label="没有收到麦克风声音">
      通话中连续约 4 秒没收到任何声音时出现，声音恢复后自动消失；你自己点了静音不会提示。
      最常见的原因是选错了麦克风：Windows 会把一副蓝牙耳机拆成两项，“耳机 / Headphones（立体声 / Stereo）”只能听，
      “耳麦 / Headset（免提 / Hands-Free）”才带麦克风。{desktop
        ? "打开系统声音设置 → 输入，把输入设备换成带“免提 / Hands-Free / Headset”的那一项，"
        : "点地址栏左侧的图标 → 网站设置 → 麦克风，换成带“免提 / Hands-Free / Headset”的那一项，"}
      再挂断重拨。还不行就看系统声音设置里的输入音量，以及耳机或电脑上的麦克风静音键。
    </HelpTip>
  </p>;
}
