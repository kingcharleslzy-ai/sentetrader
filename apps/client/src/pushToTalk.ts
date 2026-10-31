/**
 * 按住说话 (push-to-talk): hold the composer's microphone button or the hold key, speak, release.
 * The audio goes to the Runtime's `/api/voice/transcribe` (transcription only — no reply, no tools)
 * and the final text comes back to the composer, which sends it as an ordinary typed message or
 * puts it into the input (`voice.pttSendOnRelease`). Capture is the call's (`MicCapture`).
 *
 * idle → recording (level, timer, partial text) → transcribing → idle (sent / filled / a notice).
 * A hold under 0.3 s is a tap: cancelled, "说话太短". Esc cancels. After the release ~0.2 s more
 * audio is sent so the last syllable is not clipped, then `end`.
 *
 * Every hold reuses one AudioContext, created and resumed in the press gesture before anything is
 * awaited: mobile browsers only let a context started in a gesture run, and the old per-hold context
 * made after the permission await stayed suspended from the second hold on (no frames, no words, a
 * frozen wave; 10-05). A hold that still hears nothing for a second says so instead of looking alive.
 *
 * A notice (cancelled, too short, not heard, refused) clears itself PTT_NOTICE_MS after the hold is
 * over, on a tap, or on the next hold: it used to stay until the page was reloaded (10-05). Lasting
 * reasons (a call running, the model not ready) are the button's own blocked state, not the notice.
 */
import { useSyncExternalStore } from "react";
import { guestAccessTokenFor } from "./guestApi";
import { localAuthTokenFor } from "./api";
import { logDesktop } from "./frontendLog";
import { MicCapture, rootMeanSquare, setAudioSession } from "./micCapture";
import { VOICE_AUTH_PROTOCOL, VOICE_AUTH_TOKEN_PREFIX, microphoneError } from "./voiceRealtime";

export type PttState = "idle" | "recording" | "transcribing";
export interface PttStatus {
  state: PttState;
  /** Microphone loudness 0–1 while recording (~10 updates a second). */
  level: number;
  /** Epoch ms the hold began; the composer's timer. */
  startedAt?: number;
  /** The recognizer's text so far. */
  partial: string;
  /** One line under the composer after a hold that produced nothing ("没听清，再说一次", a refusal);
   *  gone PTT_NOTICE_MS later. */
  notice?: string;
}

export const PTT_MIN_HOLD_MS = 300;
export const PTT_TAIL_MS = 200;
/** The Runtime answers within 6 s of `end`; this is the client's own backstop. */
const FINAL_BACKSTOP_MS = 9000;
export const PTT_NOT_HEARD = "没听清，再说一次";
/** A release under PTT_MIN_HOLD_MS (a tap): nothing is sent, the line says how it works. */
export const PTT_TOO_SHORT = "说话太短，按住说完再松开";
export const PTT_MIC_SILENT = "麦克风没有声音，松开重试";
/** How long a ready microphone may deliver nothing before the hold says so. */
const MIC_SILENT_MS = 1000;
/** How long a notice stays once the hold is over. */
export const PTT_NOTICE_MS = 3000;

const CLOSE_REASONS: Record<number, string> = {
  4401: "登录已失效，请重新登录后再试",
  4409: "服务器未配置语音密钥，暂不能按住说话",
  4429: "今天的用量已达上限，明天再试",
  4430: "今天的语音时长已用完，明天再试",
  4431: "当前档位未开通语音，联系管理员",
  4432: "通话中不能按住说话，先结束通话",
};

type FinalHandler = (text: string) => void;
/** A device supplies the same PCM16/16 kHz frames as MicCapture. No second ASR or session owner. */
export interface PttCapture {
  start(context?: AudioContext): Promise<unknown>;
  stop(): void;
}
export type PttCaptureFactory = (frame: (pcm: Int16Array) => void, meter: (samples: Float32Array) => void) => PttCapture;

class PushToTalkStore {
  private status: PttStatus = { state: "idle", level: 0, partial: "" };
  private readonly listeners = new Set<() => void>();
  private capture?: PttCapture;
  private socket?: WebSocket;
  /** Frames and commands produced before the socket opened, sent in order once it does. */
  private outbox: (ArrayBuffer | string)[] = [];
  private onFinal?: FinalHandler;
  private session = 0;
  private micReady = false;
  private backstop?: number;
  private levelAt = 0;
  /** The microphone's AudioContext, shared by every hold (see the header). */
  private context?: AudioContext;
  private heard = false;
  private silence?: number;
  private noticeTimer?: number;

  readonly subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  readonly snapshot = () => this.status;
  /** An input owner uses this to ignore a late release after another capture has started. */
  readonly captureId = () => this.session;

  private publish(patch: Partial<PttStatus>) {
    const before = this.status;
    this.status = { ...before, ...patch };
    // A notice counts down only while idle: a recording's own (麦克风没有声音) stays until the hold ends.
    if (this.status.notice !== before.notice || this.status.state !== before.state) {
      if (this.noticeTimer !== undefined) { window.clearTimeout(this.noticeTimer); this.noticeTimer = undefined; }
      if (this.status.notice && this.status.state === "idle") this.noticeTimer = window.setTimeout(() => { this.noticeTimer = undefined; this.dismiss(); }, PTT_NOTICE_MS);
    }
    this.listeners.forEach(listener => listener());
  }

  /** Press: open the microphone and the socket. `onFinal` gets a non-empty final text. */
  async press(baseUrl: string, onFinal: FinalHandler, guest = false, input?: PttCaptureFactory) {
    if (this.status.state !== "idle") return;
    // Still inside the press gesture: nothing above the first await may be moved below it.
    if (!input) setAudioSession("play-and-record");
    const context = input ? undefined : this.gestureContext();
    const session = ++this.session;
    this.onFinal = onFinal;
    this.outbox = [];
    this.micReady = false;
    this.heard = false;
    this.publish({ state: "recording", level: 0, startedAt: Date.now(), partial: "", notice: undefined });
    const frame = (pcm: Int16Array) => { if (session === this.session) this.frame(pcm); };
    const meter = (samples: Float32Array) => { if (session === this.session) this.meter(samples); };
    const capture = input ? input(frame, meter) : new MicCapture(frame, meter);
    this.capture = capture;
    try { await capture.start(context); }
    catch (error) {
      capture.stop();
      if (session !== this.session) return;
      this.capture = undefined;
      setAudioSession("playback");
      logDesktop("microphone", microphoneError(error), error instanceof DOMException ? `${error.name}: ${error.message}` : String(error));
      this.publish({ state: "idle", level: 0, startedAt: undefined, notice: microphoneError(error) });
      return;
    }
    if (session !== this.session) { capture.stop(); return; } // released or cancelled while the permission prompt was up
    this.micReady = true;
    this.silence = window.setTimeout(() => {
      if (session !== this.session || this.heard || this.status.state !== "recording") return;
      logDesktop("microphone", PTT_MIC_SILENT, `AudioContext ${this.context?.state ?? "unavailable"}`);
      void this.context?.resume().catch(() => undefined);
      this.publish({ notice: PTT_MIC_SILENT });
    }, MIC_SILENT_MS);
    const token = guest ? guestAccessTokenFor(baseUrl) : localAuthTokenFor(baseUrl);
    const url = baseUrl.replace(/^http/, "ws") + (guest ? "/api/guest/voice/transcribe" : "/api/voice/transcribe");
    const socket = token ? new WebSocket(url, [VOICE_AUTH_PROTOCOL, VOICE_AUTH_TOKEN_PREFIX + token]) : new WebSocket(url);
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.onopen = () => { const queued = this.outbox; this.outbox = []; queued.forEach(item => socket.send(item)); };
    socket.onmessage = event => { if (session === this.session) this.handle(event.data); };
    socket.onclose = event => { if (session === this.session && this.status.state !== "idle") this.finish("", CLOSE_REASONS[event.code] ?? "语音通道连接失败"); };
  }

  /** Release: a tap under 0.3 s is cancelled with PTT_TOO_SHORT, a microphone still waiting for
   *  permission silently; otherwise ~0.2 s more audio, then `end`, and the final is awaited. */
  release() {
    if (this.status.state !== "recording") return;
    const held = Date.now() - (this.status.startedAt ?? Date.now());
    if (held < PTT_MIN_HOLD_MS) { this.cancel(); this.showNotice(PTT_TOO_SHORT); return; }
    if (!this.micReady) { this.cancel(); return; }
    const session = this.session;
    this.publish({ state: "transcribing", level: 0, notice: undefined });
    window.setTimeout(() => {
      if (session !== this.session) return;
      this.capture?.stop(); this.capture = undefined;
      setAudioSession("playback");
      this.send(JSON.stringify({ type: "end" }));
      this.backstop = window.setTimeout(() => { if (session === this.session) this.finish("", PTT_NOT_HEARD); }, FINAL_BACKSTOP_MS);
    }, PTT_TAIL_MS);
  }

  /** Esc, a slip, the window losing focus: nothing is sent or shown. */
  cancel() {
    if (this.status.state === "idle") return;
    this.send(JSON.stringify({ type: "cancel" }));
    this.teardown();
    this.publish({ state: "idle", level: 0, startedAt: undefined, partial: "" });
  }

  /** A hold key pressed while push-to-talk is blocked: nothing records, the reason shows in the input row. */
  showNotice(text: string) { if (this.status.state === "idle") this.publish({ notice: text }); }

  /** Clear the notice line: a tap on it (the next hold and PTT_NOTICE_MS also clear it). */
  dismiss() { if (this.status.notice) this.publish({ notice: undefined }); }

  /** The context the holds resumed in their press gestures: the spoken reply plays through it, since
   *  one made when the final comes back (outside any gesture) stays suspended on phones (10-05). */
  playbackContext(): AudioContext | undefined { return this.context?.state === "closed" ? undefined : this.context; }

  /** Created on the first hold, resumed on every one; called synchronously in the press gesture. */
  private gestureContext(): AudioContext | undefined {
    try {
      if (!this.context || this.context.state === "closed") this.context = new AudioContext();
      if (this.context.state !== "running") void this.context.resume().catch(() => undefined);
    } catch { this.context = undefined; } // no WebAudio: the capture makes its own and reports the error
    return this.context;
  }

  private frame(frame: Int16Array) {
    if (!this.heard) {
      this.heard = true;
      if (this.status.notice === PTT_MIC_SILENT) this.publish({ notice: undefined });
    }
    this.send(frame.buffer as ArrayBuffer);
  }

  private send(item: ArrayBuffer | string) {
    const socket = this.socket;
    if (socket?.readyState === WebSocket.OPEN) socket.send(item);
    else if (!socket || socket.readyState === WebSocket.CONNECTING) this.outbox.push(item);
  }

  private meter(samples: Float32Array) {
    const now = performance.now();
    if (now - this.levelAt < 100 || this.status.state !== "recording") return;
    this.levelAt = now;
    this.publish({ level: Math.min(1, rootMeanSquare(samples) / 0.2) });
  }

  private handle(raw: unknown) {
    if (typeof raw !== "string") return;
    let event: { type?: string; text?: string; reason?: string };
    try { event = JSON.parse(raw); } catch { return; }
    if (event.type === "partial" && this.status.state !== "idle") this.publish({ partial: event.text ?? "" });
    else if (event.type === "final") this.finish((event.text ?? "").trim(), event.reason === "upstream_error" ? "语音识别暂时不可用，请稍后再试" : PTT_NOT_HEARD);
  }

  private finish(text: string, emptyNotice: string) {
    const onFinal = this.onFinal;
    this.teardown();
    this.publish({ state: "idle", level: 0, startedAt: undefined, partial: "", notice: text ? undefined : emptyNotice });
    if (text) onFinal?.(text);
  }

  private teardown() {
    this.session += 1;
    if (this.backstop !== undefined) { window.clearTimeout(this.backstop); this.backstop = undefined; }
    if (this.silence !== undefined) { window.clearTimeout(this.silence); this.silence = undefined; }
    if (this.capture) { this.capture.stop(); this.capture = undefined; setAudioSession("playback"); }
    const socket = this.socket;
    this.socket = undefined;
    this.outbox = [];
    this.onFinal = undefined;
    if (socket) {
      socket.onclose = null; socket.onmessage = null;
      if (socket.readyState === WebSocket.OPEN) { try { socket.close(); } catch { /* closing anyway */ } }
      else if (socket.readyState === WebSocket.CONNECTING) socket.onopen = () => { try { socket.close(); } catch { /* closing anyway */ } };
    }
  }

  /** Tests only. */
  reset() { this.teardown(); this.context = undefined; this.publish({ state: "idle", level: 0, startedAt: undefined, partial: "", notice: undefined }); }
}

export const pushToTalkStore = new PushToTalkStore();

export function usePushToTalkStatus(): PttStatus {
  return useSyncExternalStore(pushToTalkStore.subscribe, pushToTalkStore.snapshot, pushToTalkStore.snapshot);
}

/**
 * A surface that takes the held words instead of the 对话面板: the strategy guide card while it asks
 * (2026-09-26). While one is registered the hold key sends its text there and does not open the
 * 对话面板; the guide would take the same words from the chat anyway, this keeps them on the card.
 */
export type PttTarget = (text: string) => void;
let pttTarget: PttTarget | undefined;
export function setPttTarget(target: PttTarget): () => void {
  pttTarget = target;
  return () => { if (pttTarget === target) pttTarget = undefined; };
}
export function currentPttTarget(): PttTarget | undefined { return pttTarget; }
