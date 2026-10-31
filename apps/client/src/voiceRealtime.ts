/**
 * Real-time voice call: microphone PCM16/16 kHz → Runtime WebSocket → Doubao;
 * Doubao float32 PCM/24 kHz → Runtime → this player. The Runtime owns the model key and
 * the tools; this file only moves audio and shows state. Availability comes from
 * `GET /api/voice/realtime/status` (the key lives with the Runtime); there is no client switch.
 */
import { localAuthTokenFor, requestJson } from "./api";
import { logDesktop } from "./frontendLog";
import { MicCapture, handsFreeSuggestion, rootMeanSquare, setAudioSession, type MicDeviceInfo } from "./micCapture";
import { activeAgentWindow } from "./nativeAgent";
import { loginIdentity, onLoginChange } from "./runtimeClient";

/** Subprotocol pair that carries the session token: browsers cannot set Authorization on a WebSocket,
 *  and the token must stay out of the query string (the Runtime access log prints request targets). */
export const VOICE_AUTH_PROTOCOL = "ai-trader-token";
export const VOICE_AUTH_TOKEN_PREFIX = "bearer.";

export interface VoiceRealtimeStatus {
  enabled: boolean;
  keyConfigured: boolean;
  reason: string | null;
  activeCalls?: number;
}
export function getVoiceRealtimeStatus(baseUrl: string) {
  return requestJson<VoiceRealtimeStatus>(`${baseUrl}/api/voice/realtime/status`);
}

/** The voices the Runtime offers and this account's choice; a change applies to the next call. */
export interface VoiceRealtimeVoices {
  current: string;
  voices: { id: string; label: string }[];
}
export function getVoiceRealtimeVoices(baseUrl: string) {
  return requestJson<VoiceRealtimeVoices>(`${baseUrl}/api/voice/realtime/voices`);
}

export type VoiceCallState = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "tool" | "error";
export interface VoiceCallStatus {
  state: VoiceCallState;
  /** The call was opened (or asked) for the guided review (复盘页“语音复盘”); cleared when it ends. */
  review?: boolean;
  tools?: string[];
  /** Seconds the agent has been working on the current request (`state: tool`), from the Runtime's ticks. */
  elapsedSeconds?: number;
  /** Microphone loudness 0–1 while listening, ~10 updates a second; drives the orb's halo. */
  level?: number;
  muted?: boolean;
  transcript?: string;
  reply?: string;
  lastLatencySeconds?: number;
  turns?: number;
  totalTokens?: number;
  error?: string;
  announcement?: string;
  /** Epoch ms the call went live (past 接通中); the call bar's mm:ss. Undefined when not in a call. */
  startedAt?: number;
  /** Listening, not muted, and the microphone has delivered no sound (or no audio at all) for
   *  MIC_SILENT_MS: the call shows 没有收到麦克风声音. Cleared the moment sound comes back. */
  micSilent?: boolean;
  /** With micSilent: the microphone the browser actually opened (its label, owner name dropped). */
  micLabel?: string;
  /** With micSilent: the same headset's Hands-Free / 免提 / Headset input, when the current one is not it. */
  micSuggestion?: string;
}
export type VoiceCallEvent =
  | { type: "status"; state: string; tools?: string[]; ttsType?: string; elapsedSeconds?: number }
  | { type: "transcript"; text: string; final: boolean }
  | { type: "text"; delta: string }
  | { type: "audio"; data: string; encoding?: "f32le" | "s16le" }
  | { type: "audio_end" }
  | { type: "interrupt" }
  | { type: "tool_result"; name: string; ok: boolean; summary: unknown; elapsedSeconds: number }
  | { type: "latency"; latencySeconds: number }
  | { type: "usage"; usage: { total_tokens?: number }; turns: number }
  | { type: "announce"; kind: string; text: string }
  | { type: "error"; error: unknown }
  // Client-side diagnostics kept in the call's events for 导出诊断日志 (never audio):
  | ({ type: "microphone" } & MicDeviceInfo)
  | { type: "mic_silent"; silent: boolean; blocks: number; context: string; trackMuted?: boolean; readyState?: string }
  | { type: "mic_suggestion"; label: string }
  | { type: "closed"; durationSeconds: number; turns: number; usage: { total_tokens: number }; latency: unknown[] };

const OUTPUT_RATE = 24000;
// A reply's first frames are held back until either bound, then scheduled as one contiguous run:
// starting each frame at `currentTime + 0.02` left a gap whenever one arrived late (mobile
// jitter) — the crackle users heard. 2 ms fade-in samples at 24 kHz.
const PREROLL_SAMPLES = OUTPUT_RATE * 0.12;
const PREROLL_FRAMES = 6;
const START_DELAY = 0.05;
const REALIGN_DELAY = 0.1;
const FADE_SAMPLES = OUTPUT_RATE * 0.002;
// Barge-in is confirmed by the server: a local loud burst only ducks the playback this quiet for this
// long; the queue is dropped solely when the server says the user's first word was recognised
// (its `interrupt`). A knock or a chair scrape never becomes an interruption.
const DUCK_GAIN = 0.15;
const DUCK_CONFIRM_MS = 700;
// 没有收到麦克风声音 (10-14: a Windows web call where Doubao heard nothing while the phone worked).
// A block quieter than this (≈ −74 dBFS) is no sound: a muted or wrong input device delivers digital
// silence, a real room never does. While listening and not muted, this long without one sounding
// block — or without any block at all (a stalled AudioContext) — shows the hint.
const MIC_SILENT_RMS = 0.0002;
export const MIC_SILENT_MS = 4000;
const MIC_CHECK_MS = 500;

/** One call: opens the socket, streams the microphone, plays what comes back. */
export class VoiceCall {
  private socket?: WebSocket;
  /** One context at the device rate for both directions: two contexts at forced 16 k/24 k made
   *  iOS/Android WebKit resample crackle, and iOS reverts to the hardware rate with a mic anyway. */
  private audioContext?: AudioContext;
  /** Everything played goes through this gain so a suspected barge-in can duck before it is confirmed. */
  private gain?: GainNode;
  /** Tap after the playback gain for the call bar's waveform (created on first use). */
  private playbackMeter?: AnalyserNode;
  /** Pending duck: set while waiting for the server's `interrupt` to confirm a barge-in. */
  private duckTimer?: number;
  /** Microphone capture (shared with push-to-talk); its AudioContext is the one above. */
  private capture?: MicCapture;
  private playhead = 0;
  private scheduled: AudioBufferSourceNode[] = [];
  private preroll: Float32Array[] = [];
  private prerollSamples = 0;
  private underrunLogged = false;
  private status: VoiceCallStatus = { state: "idle" };
  /** Set by stop(): a start still awaiting the permission prompt or the socket gives up when it resumes. */
  private stopped = false;
  /** What the user asked for, applied again once the microphone and the socket are ready. */
  private desiredMuted = false;
  private replyText = "";
  readonly events: VoiceCallEvent[] = [];
  onStatus: (status: VoiceCallStatus) => void = () => undefined;
  /** The open call ended without stop(): the Runtime closed it (a spoken hang-up, the daily limit, the voice
   *  channel given up) or the socket dropped. The page's store lets go of it then (#523 / #875). */
  onEnded: () => void = () => undefined;
  private opened = false;

  constructor(private readonly baseUrl: string) {}

  current(): VoiceCallStatus { return this.status; }

  private update(patch: Partial<VoiceCallStatus>) {
    this.status = { ...this.status, ...patch };
    this.onStatus(this.status);
  }

  /** `sessionId` names the chat session voice turns join, so spoken and typed turns share one conversation. */
  async start(sessionId?: string, intent?: "review"): Promise<void> {
    this.update({ state: "connecting", error: undefined });
    setAudioSession("play-and-record");
    // Microphone first: a machine without one (or without permission) fails here without opening a
    // server call; frames captured before the socket opens are dropped by send().
    try { await this.startMicrophone(); }
    catch (error) {
      this.stopAudio();
      if (this.stopped) return;
      this.update({ state: "error" });
      logDesktop("microphone", microphoneError(error), error instanceof DOMException ? `${error.name}: ${error.message}` : String(error));
      throw new Error(microphoneError(error), { cause: error });
    }
    // Ended while the permission prompt was open: the microphone is already released, no call opens.
    if (this.stopped) return;
    const token = localAuthTokenFor(this.baseUrl);
    const url = this.baseUrl.replace(/^http/, "ws") + "/api/voice/realtime";
    this.socket = token ? new WebSocket(url, [VOICE_AUTH_PROTOCOL, VOICE_AUTH_TOKEN_PREFIX + token]) : new WebSocket(url);
    this.socket.binaryType = "arraybuffer";
    this.socket.onmessage = event => this.handle(event.data);
    this.socket.onerror = () => this.update({ state: "error", error: "语音通道连接失败" });
    this.socket.onclose = () => {
      if (this.status.state !== "idle") this.stopAudio();
      if (this.status.state !== "error") this.update({ state: "idle" });
      if (this.opened && !this.stopped) this.onEnded();
    };
    try {
      await new Promise<void>((resolve, reject) => {
        this.socket!.onopen = () => resolve();
        // 4430 (M2): the tier's daily voice minutes are spent; the orb shows the line it heard.
        this.socket!.addEventListener("close", event => reject(new Error(event.code === 4430 ? "今天的语音时长已用完，明天再试" : "voice_socket_closed")), { once: true });
      });
    } catch (error) {
      if (this.stopped) return;
      this.stop(); throw error;
    }
    if (this.stopped) return;
    this.opened = true;
    // Name this device's UI window so the call's tools act here, not on another signed-in device.
    const windowId = activeAgentWindow(this.baseUrl);
    if (windowId) this.send({ type: "hello", windowId, ...(sessionId ? { sessionId } : {}), ...(intent ? { intent } : {}) });
    if (this.desiredMuted) this.send({ type: "mute" });
  }

  /** 语音复盘 during a call: the Runtime starts the guided review with its first question. */
  review() {
    const windowId = activeAgentWindow(this.baseUrl);
    if (windowId) this.send({ type: "hello", windowId, intent: "review" });
  }

  private async startMicrophone() {
    const capture = new MicCapture(frame => { if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(frame.buffer); },
      samples => this.detectSpeech(samples));
    this.capture = capture;
    const context = await capture.start();
    if (this.stopped) { capture.stop(); return; }
    capture.setEnabled(!this.desiredMuted);
    this.audioContext = context;
    this.gain = this.audioContext.createGain();
    this.gain.connect(this.audioContext.destination);
    const device = capture.deviceInfo();
    if (device) {
      this.events.push({ type: "microphone", ...device });
      logDesktop("microphone", "device", JSON.stringify(device));
    }
    this.micTimer = window.setInterval(() => this.checkMicrophone(), MIC_CHECK_MS);
  }

  private micTimer?: number;
  /** Date.now() of the last sounding microphone block, of entering listening (0 = not listening). */
  private lastSoundAt = 0;
  private listeningSince = 0;
  /** Microphone blocks seen since listening began: 0 in the hint's log line = no audio at all. */
  private micBlocks = 0;
  private checkMicrophone() {
    const now = Date.now();
    if (this.status.state !== "listening" || this.status.muted) {
      this.listeningSince = 0;
      if (this.status.micSilent) {
        this.events.push({ type: "mic_silent", silent: false, blocks: this.micBlocks, context: this.audioContext?.state ?? "none" });
        this.update({ micSilent: false });
      }
      return;
    }
    if (!this.listeningSince) { this.listeningSince = now; this.micBlocks = 0; }
    const silent = now - Math.max(this.lastSoundAt, this.listeningSince) >= MIC_SILENT_MS;
    if (silent === Boolean(this.status.micSilent)) return;
    if (silent) {
      const context = this.audioContext?.state ?? "none";
      // The track as it is now (muted by the OS, ended by an unplug), not as it was at call start.
      const device = this.capture?.deviceInfo();
      this.events.push({ type: "mic_silent", silent: true, blocks: this.micBlocks, context, trackMuted: device?.trackMuted, readyState: device?.readyState });
      logDesktop("microphone", `silent ${MIC_SILENT_MS / 1000}s`, `blocks=${this.micBlocks} context=${context} trackMuted=${device?.trackMuted} readyState=${device?.readyState}`);
      const label = device?.label ?? "";
      this.update({ micSilent: true, micLabel: label || undefined, micSuggestion: undefined });
      void this.suggestMicrophone(label);
      return;
    }
    this.update({ micSilent: false });
  }

  /** The device list (labels are readable once the microphone is granted): the same headset's
   *  Hands-Free input, if the current one is its listen-only half. Best effort, never throws. */
  private async suggestMicrophone(current: string) {
    try {
      const devices = await navigator.mediaDevices?.enumerateDevices?.();
      const suggestion = devices ? handsFreeSuggestion(current, devices) : undefined;
      if (suggestion && this.status.micSilent) {
        this.events.push({ type: "mic_suggestion", label: suggestion });
        this.update({ micSuggestion: suggestion });
      }
    } catch { /* the hint stands without a suggestion */ }
  }

  private loudFrames = 0;
  private levelReportedAt = 0;
  /** A burst of voice while audio is queued only DUCKS the playback and starts the confirmation timer;
   *  the server's `interrupt` (its ASR recognised the first word) is what actually drops the queue, so
   *  background noise never interrupts. Echo cancellation keeps the speaker out of this.
   *  The same loudness feeds the orb's listening halo (throttled to ~10 Hz). */
  private detectSpeech(samples: Float32Array) {
    const rms = rootMeanSquare(samples);
    this.micBlocks += 1;
    if (rms >= MIC_SILENT_RMS) {
      this.lastSoundAt = Date.now();
      if (this.status.micSilent) {
        this.events.push({ type: "mic_silent", silent: false, blocks: this.micBlocks, context: this.audioContext?.state ?? "none" });
        this.update({ micSilent: false });
      }
    }
    const now = performance.now();
    if (now - this.levelReportedAt >= 100) {
      this.levelReportedAt = now;
      const level = Math.min(1, rms / 0.2);
      if (Math.abs(level - (this.status.level ?? 0)) >= 0.02) this.update({ level });
    }
    if (!this.scheduled.length) { this.loudFrames = 0; return; }
    this.loudFrames = rms > 0.04 ? this.loudFrames + 1 : 0;
    if (this.loudFrames >= 3) { this.loudFrames = 0; this.duckForBargeIn(); }
  }

  /** Quiet the playback while the server decides whether this was speech; nothing is dropped yet. */
  private duckForBargeIn() {
    if (!this.gain || this.duckTimer !== undefined) return;
    this.gain.gain.value = DUCK_GAIN;
    this.duckTimer = window.setTimeout(() => {
      this.duckTimer = undefined;
      this.restoreGain(); // not speech after all (or the server stayed silent): back to full volume
    }, DUCK_CONFIRM_MS);
  }

  private restoreGain() {
    if (this.duckTimer !== undefined) { clearTimeout(this.duckTimer); this.duckTimer = undefined; }
    if (this.gain) this.gain.gain.value = 1;
  }

  private handle(raw: unknown) {
    if (typeof raw !== "string") return;
    let event: VoiceCallEvent;
    try { event = JSON.parse(raw) as VoiceCallEvent; } catch { return; }
    if (event.type !== "audio") this.events.push(event);
    switch (event.type) {
      case "status": {
        const state = (["connecting", "listening", "thinking", "speaking", "tool"].includes(event.state) ? event.state : "listening") as VoiceCallState;
        if (state === "thinking" || state === "listening") this.replyText = state === "listening" ? this.replyText : "";
        this.update({ state, tools: event.tools, elapsedSeconds: state === "tool" ? event.elapsedSeconds ?? this.status.elapsedSeconds ?? 0 : undefined });
        return;
      }
      case "transcript": this.update({ transcript: event.text }); return;
      case "text": this.replyText += event.delta ?? ""; this.update({ reply: this.replyText }); return;
      case "audio": this.play(event.data, event.encoding); return;
      case "audio_end": this.startPreroll(); return; // a reply shorter than the preroll bound plays now (#523)
      case "interrupt": this.flushPlayback(); return; // drops the queue and restores the ducked gain
      case "latency": this.update({ lastLatencySeconds: event.latencySeconds }); return;
      case "usage": this.update({ turns: event.turns, totalTokens: (this.status.totalTokens ?? 0) + (event.usage.total_tokens ?? 0) }); return;
      case "announce": this.update({ announcement: event.text }); return;
      case "error": this.update({ state: "error", error: describe(event.error) }); return;
      case "closed": this.stopAudio(); this.update({ state: "idle", turns: event.turns, totalTokens: event.usage.total_tokens }); return;
      default: return;
    }
  }

  /** Doubao's "pcm" output is 32-bit float little-endian at 24 kHz (the Runtime says so per frame);
   *  reading it as int16 produced full-scale noise with a ghost of the voice (1.5.1–1.5.5). */
  private play(base64: string, encoding: "f32le" | "s16le" = "f32le") {
    if (!this.audioContext) return;
    const samples = decodeOutputAudio(Uint8Array.from(atob(base64), c => c.charCodeAt(0)), encoding);
    if (!samples.length) return;
    if (this.playhead === 0 && !this.preroll.length) {
      // First frame of a reply: one log line with the decoded peak (speech ≈ 0.2–0.6; a wrong
      // sample format reads as ≈1.0 every time) so a noisy machine can be diagnosed from its log.
      let peak = 0;
      for (let i = 0; i < samples.length; i += 1) peak = Math.max(peak, Math.abs(samples[i]));
      logDesktop("audio", `encoding=${encoding} frames=${samples.length} peak=${peak.toFixed(3)}`);
      this.underrunLogged = false;
    }
    if (this.preroll.length || this.playhead === 0) {
      // Hold the start of a reply until the preroll bound, then schedule the whole run contiguously
      // from one base time; a late frame no longer punches a hole the next one must fill.
      this.preroll.push(samples);
      this.prerollSamples += samples.length;
      if (this.prerollSamples >= PREROLL_SAMPLES || this.preroll.length >= PREROLL_FRAMES) this.startPreroll();
      return;
    }
    this.schedule(samples);
  }

  /** The held-back start of a reply, scheduled as one contiguous run from one base time. */
  private startPreroll() {
    if (!this.audioContext || !this.preroll.length) return;
    const [first, ...rest] = this.preroll;
    this.schedule(first, this.audioContext.currentTime + START_DELAY);
    for (const frame of rest) this.schedule(frame); // from the playhead: one seamless run
    this.preroll = []; this.prerollSamples = 0;
  }

  /** One frame at `startAt` (the preroll base) or at the playhead; a playhead left behind by a
   *  network stall is realigned just ahead of now with a 2 ms fade instead of stacking gaps. */
  private schedule(samples: Float32Array, startAt?: number) {
    const context = this.audioContext;
    if (!context) return;
    let at = startAt ?? this.playhead;
    if (at < context.currentTime) {
      at = context.currentTime + REALIGN_DELAY;
      if (!this.underrunLogged) {
        this.underrunLogged = true;
        logDesktop("audio", `underrun: playhead ${this.playhead.toFixed(3)}s behind ${context.currentTime.toFixed(3)}s, realigned +${REALIGN_DELAY}s`);
      }
      const fade = Math.min(FADE_SAMPLES, samples.length);
      for (let i = 0; i < fade; i += 1) samples[i] *= i / fade;
    }
    const buffer = context.createBuffer(1, samples.length, OUTPUT_RATE);
    buffer.getChannelData(0).set(samples);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain ?? context.destination);
    source.start(at);
    this.playhead = at + buffer.duration;
    this.scheduled.push(source);
    source.onended = () => {
      this.scheduled = this.scheduled.filter(item => item !== source);
      if (!this.scheduled.length) this.playhead = 0; // the reply is over; the next one re-prerolls
    };
  }

  /** Barge-in confirmed by the server (or a manual stop): drop everything queued so the assistant
   *  stops within one buffer, and bring the ducked gain back. */
  private flushPlayback() {
    for (const source of this.scheduled) { try { source.stop(); } catch { /* already ended */ } }
    this.scheduled = [];
    this.preroll = []; this.prerollSamples = 0;
    this.playhead = 0;
    this.restoreGain();
  }

  /** The call's two level taps on its one AudioContext: microphone in, AI playback out. Both only
   *  listen (no output connected), so recording and playback are unchanged. */
  analysers(): { input?: AnalyserNode; output?: AnalyserNode } {
    if (!this.playbackMeter && this.audioContext && this.gain) {
      this.playbackMeter = this.audioContext.createAnalyser();
      this.playbackMeter.fftSize = 512;
      this.gain.connect(this.playbackMeter);
    }
    return { input: this.capture?.analyser(), output: this.playbackMeter };
  }

  interrupt() { this.flushPlayback(); this.send({ type: "cancel" }); }
  mute(muted: boolean) {
    this.desiredMuted = muted;
    this.send({ type: muted ? "mute" : "unmute" });
    this.capture?.setEnabled(!muted);
    this.update({ muted, level: 0 });
  }

  private send(payload: object) { if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(payload)); }

  private stopAudio() {
    if (this.micTimer !== undefined) { clearInterval(this.micTimer); this.micTimer = undefined; }
    this.listeningSince = 0;
    if (this.status.micSilent) this.status = { ...this.status, micSilent: false };
    this.flushPlayback();
    this.capture?.stop(); this.capture = undefined; // closes the shared AudioContext too
    setAudioSession("playback");
    this.playbackMeter = undefined;
    this.gain = undefined;
    this.audioContext = undefined;
  }

  stop() {
    this.stopped = true;
    this.send({ type: "stop" });
    this.stopAudio();
    try { this.socket?.close(); } catch { /* closing anyway */ }
    this.update({ state: "idle" });
  }
}

function describe(error: unknown): string {
  if (error && typeof error === "object") {
    const record = error as Record<string, unknown>;
    return String(record.message ?? record.error ?? JSON.stringify(record)).slice(0, 160);
  }
  return String(error);
}

/** Server audio bytes → mono samples in [-1, 1]. Trailing bytes short of one sample are dropped. */
export function decodeOutputAudio(bytes: Uint8Array, encoding: "f32le" | "s16le" = "f32le"): Float32Array {
  const width = encoding === "s16le" ? 2 : 4;
  const frames = Math.floor(bytes.byteLength / width);
  const out = new Float32Array(frames);
  const view = new DataView(bytes.buffer, bytes.byteOffset, frames * width);
  for (let i = 0; i < frames; i += 1) {
    out[i] = encoding === "s16le" ? view.getInt16(i * 2, true) / 32768 : Math.max(-1, Math.min(1, view.getFloat32(i * 4, true)));
  }
  return out;
}

/** getUserMedia failures in the words the status line shows; the raw name stays in parentheses for support. */
export function microphoneError(error: unknown): string {
  const name = error instanceof DOMException ? error.name : error instanceof Error ? error.name : "";
  switch (name) {
    case "NotFoundError": case "OverconstrainedError": return "未检测到麦克风（NotFoundError）";
    case "NotAllowedError": case "PermissionDeniedError": return "麦克风权限被拒绝，请在系统设置里允许 Sente 使用麦克风（NotAllowedError）";
    case "NotReadableError": return "麦克风被其他程序占用（NotReadableError）";
    case "SecurityError": return "当前页面环境不允许使用麦克风（SecurityError）";
    default: return error instanceof Error && error.message ? error.message : "无法开始通话";
  }
}

/** The page's one call: the floating orb starts, ends and shows it; the composer only reports its state. */
class VoiceCallStore {
  private call: VoiceCall | null = null;
  /** The sign-in the current call (and the remembered timeline) belongs to (#523). */
  private callLogin = -1;
  private status: VoiceCallStatus = { state: "idle" };
  private readonly listeners = new Set<() => void>();
  readonly subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  readonly snapshot = () => this.status;
  inCall() { return this.status.state !== "idle" && this.status.state !== "error"; }
  private publish(status: VoiceCallStatus) {
    // Stamped once the call is actually live (接通中 does not count yet) and carried through every
    // later status, so the call bar's mm:ss survives a re-render or a re-mount of the panel.
    const connected = !["idle", "error", "connecting"].includes(status.state);
    const live = status.state !== "idle" && status.state !== "error";
    this.status = { ...status, startedAt: connected ? this.status.startedAt ?? Date.now() : undefined, review: live && this.reviewing };
    this.listeners.forEach(listener => listener());
  }
  private reviewing = false;
  async start(baseUrl: string, sessionId?: string, intent?: "review") {
    if (this.call) return;
    const next = new VoiceCall(baseUrl);
    next.onStatus = status => { if (this.call === next) this.publish(status); };
    // Ended by the Runtime or the network: the orb can start the next call, and the diagnostics export
    // is this call's timeline, not the previous one's.
    next.onEnded = () => {
      if (this.call !== next) return;
      this.remember();
      this.call = null;
      this.reviewing = false;
      this.publish(this.status);
    };
    this.call = next;
    this.callLogin = loginIdentity();
    try { await next.start(sessionId, intent); }
    catch (error) {
      if (this.call !== next) return;
      this.remember();
      this.call = null;
      const message = error instanceof Error ? error.message : "";
      const name = error instanceof Error ? error.name : "";
      logDesktop("voice-start", `${name}: ${message}`, error instanceof Error && error.stack ? error.stack : "");
      this.publish({ state: "error", error: !message || message === "voice_socket_closed" ? "语音通道连接失败"
        : name === "SecurityError" ? `语音通道被页面安全策略拒绝（SecurityError: ${message}）` : message });
    }
  }
  stop() { this.remember(); this.reviewing = false; this.call?.stop(); this.call = null; this.publish({ ...this.status, state: "idle", muted: false, level: 0, micSilent: false }); }
  /** 复盘页“语音复盘”: in a call the Runtime starts the review now; otherwise the call opens with it. */
  async review(baseUrl: string, sessionId?: string) {
    this.reviewing = true;
    if (this.call && this.inCall()) { this.call.review(); this.publish(this.status); return; }
    await this.start(baseUrl, sessionId, "review");
  }
  /** The last call's events (audio frames excluded) for 导出诊断日志 — only to the person who made the
   *  call (#523): a sign-out or another sign-in retires it, and an export checks the sign-in again. */
  private timeline: VoiceCallEvent[] = [];
  private timelineLogin = -1;
  private remember() {
    if (!this.call || this.callLogin !== loginIdentity()) return;
    this.timeline = [...this.call.events];
    this.timelineLogin = this.callLogin;
  }
  lastTimeline(): VoiceCallEvent[] { return this.timelineLogin === loginIdentity() ? this.timeline : []; }
  /** A sign-in or sign-out: the call made as the previous person ends without being remembered, and
   *  its late callbacks find no call to write to. */
  readonly retireLogin = () => {
    this.timeline = [];
    const call = this.call;
    this.call = null;
    this.reviewing = false;
    call?.stop();
    if (call || this.status.state !== "idle") this.publish({ state: "idle" });
  };
  interrupt() { this.call?.interrupt(); }
  mute(muted: boolean) { this.call?.mute(muted); }
  /** The live call's microphone / playback level taps (empty when no call has audio). */
  analysers(): { input?: AnalyserNode; output?: AnalyserNode } { return this.call?.analysers() ?? {}; }
  /** Clear a failed start so the orb returns to standby. */
  dismissError() { if (this.status.state === "error") this.publish({ state: "idle" }); }
  /** Tests only. */
  reset() { this.call = null; this.publish({ state: "idle" }); }
}
export const voiceCallStore = new VoiceCallStore();
onLoginChange(voiceCallStore.retireLogin);

/** The orb's four looks. `connecting` and Doubao's own `thinking` count as "busy" so the user sees one thing: wait. */
export type OrbState = "standby" | "listening" | "busy" | "speaking";
export function orbState(status: VoiceCallStatus): OrbState {
  switch (status.state) {
    case "listening": return "listening";
    case "speaking": return "speaking";
    case "connecting": case "thinking": case "tool": return "busy";
    default: return "standby";
  }
}

export function orbLabel(status: VoiceCallStatus): string {
  switch (status.state) {
    case "connecting": return "接通中…";
    case "listening": return status.muted ? "已静音" : status.micSilent ? "没收到麦克风声音" : "听你说";
    case "thinking": return "AI 在想";
    case "tool": return status.elapsedSeconds ? `后台在查 ${Math.round(status.elapsedSeconds)} s` : "后台在查";
    case "speaking": return "AI 在说";
    case "error": return status.error ?? "语音出错";
    default: return "开始语音通话";
  }
}

