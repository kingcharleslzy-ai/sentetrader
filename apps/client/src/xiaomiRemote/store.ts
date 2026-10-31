import { useSyncExternalStore } from "react";
import type { PttCaptureFactory } from "../pushToTalk";
import { AtvvDecoder, GET_CAPS } from "./protocol";
import { browserTransport, nativeTransport, remotePlatform, type Packet, type RemotePlatform, type RemoteTransport } from "./transport";

export const REMOTE_KEYS = [
  { id: "up", label: "上" }, { id: "down", label: "下" }, { id: "left", label: "左" }, { id: "right", label: "右" },
  { id: "ok", label: "确认" }, { id: "back", label: "返回" }, { id: "home", label: "主页" }, { id: "menu", label: "菜单" },
  { id: "tv", label: "TV" }, { id: "volumeUp", label: "音量＋" }, { id: "volumeDown", label: "音量－" },
  { id: "voice", label: "语音" }, { id: "power", label: "电源" },
] as const;
export type RemoteKey = typeof REMOTE_KEYS[number]["id"];
export const REMOTE_ACTIONS = [
  { id: "none", label: "停用" }, { id: "ptt", label: "按住说话" }, { id: "cancel", label: "取消本次语音" },
  { id: "chat", label: "打开对话" }, { id: "settings", label: "打开设置" },
  { id: "scrollUp", label: "向上滚动" }, { id: "scrollDown", label: "向下滚动" },
  { id: "focusPrevious", label: "上一个控件" }, { id: "focusNext", label: "下一个控件" },
  { id: "activate", label: "操作已聚焦的安全控件" },
] as const;
export type RemoteAction = typeof REMOTE_ACTIONS[number]["id"];
const DEFAULTS: Record<RemoteKey, RemoteAction> = {
  up: "scrollUp", down: "scrollDown", left: "focusPrevious", right: "focusNext", ok: "none", back: "cancel",
  home: "chat", menu: "settings", tv: "chat", volumeUp: "none", volumeDown: "none", voice: "ptt", power: "none",
};
export interface RemoteStatus {
  enabled: boolean;
  phase: "idle" | "connecting" | "ready" | "reconnecting" | "error";
  supported: boolean;
  platform: RemotePlatform;
  deviceName?: string;
  error?: string;
  keyError?: string;
  battery?: number;
  bindings: Record<RemoteKey, RemoteAction>;
  bindingsDefault: boolean;
  availableKeys: RemoteKey[];
  diagnostic: boolean;
  lastKey?: { key: RemoteKey; pressed: boolean };
  frames: number;
  level: number;
}
export type RemoteInput = { type: "start"; input: PttCaptureFactory } | { type: "stop" } | { type: "cancel" }
  | { type: "action"; action: RemoteAction };
const STORAGE = "ai-trader.xiaomi-remote.v1";
const validBinding = (key: string, action: string): key is RemoteKey => REMOTE_KEYS.some(k => k.id === key)
  && REMOTE_ACTIONS.some(a => a.id === action) && (action !== "ptt" || key === "voice");

export class XiaomiRemoteStore {
  private readonly listeners = new Set<() => void>();
  private readonly inputs = new Set<(value: RemoteInput) => void>();
  private transport?: RemoteTransport;
  private decoder = new AtvvDecoder();
  private generation = 0;
  private retry = 0;
  private reconnect?: ReturnType<typeof setTimeout>;
  private heartbeat?: ReturnType<typeof setInterval>;
  private heldTimeout?: ReturnType<typeof setTimeout>;
  private ready?: () => void;
  private failed?: (error: Error) => void;
  private sink?: (pcm: Int16Array) => void;
  private readonly pageClosed = () => this.disconnect();
  private readonly reserveVoiceKey = (event: KeyboardEvent) => {
    // Web Bluetooth cannot identify the keyboard device. Explicitly reserve F5 in this connected
    // page, rather than pretending it is isolated HID; F5 itself never starts any microphone.
    if (event.code !== "F5" || this.status.platform !== "web" || !this.status.enabled
      || !["connecting", "ready", "reconnecting"].includes(this.status.phase)) return;
    event.preventDefault(); event.stopImmediatePropagation();
  };
  private status: RemoteStatus;

  constructor(private readonly makeTransport?: (packet: (value: Packet) => void) => RemoteTransport) {
    const platform = remotePlatform();
    const bindings = { ...DEFAULTS };
    try {
      const saved: unknown = JSON.parse(localStorage.getItem(STORAGE) || "null");
      if (saved && typeof saved === "object") for (const [key, action] of Object.entries(saved)) {
        if (typeof action === "string" && validBinding(key, action)) bindings[key] = action as RemoteAction;
      }
    } catch { /* Private/blocked storage: the current window still works. */ }
    this.status = { enabled: false, phase: "idle", supported: platform !== "unsupported", platform, bindings,
      bindingsDefault: REMOTE_KEYS.every(k => bindings[k.id] === DEFAULTS[k.id]),
      availableKeys: platform !== "unsupported" ? ["voice"] : [],
      diagnostic: false, frames: 0, level: 0 };
  }
  readonly subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  readonly snapshot = () => this.status;
  readonly onInput = (listener: (value: RemoteInput) => void) => { this.inputs.add(listener); return () => { this.inputs.delete(listener); }; };
  private publish(patch: Partial<RemoteStatus>) {
    const next = { ...this.status, ...patch };
    next.bindingsDefault = REMOTE_KEYS.every(k => next.bindings[k.id] === DEFAULTS[k.id]);
    this.status = next; this.listeners.forEach(f => f());
  }
  private emit(value: RemoteInput) { this.inputs.forEach(f => f(value)); }

  setBinding(key: RemoteKey, action: RemoteAction) {
    if (!validBinding(key, action)) throw new Error("未知遥控按键或功能；仅语音键支持按住说话");
    if (key === "voice") { this.emit({ type: "cancel" }); this.sink = undefined; }
    this.publish({ bindings: { ...this.status.bindings, [key]: action } }); this.save();
  }
  resetBindings() { this.publish({ bindings: { ...DEFAULTS } }); this.save(); }
  private save() { try { localStorage.setItem(STORAGE, JSON.stringify(this.status.bindings)); } catch { /* optional */ } }
  setEnabled(enabled: boolean) { if (!enabled) this.disconnect(); else this.publish({ enabled: true }); }
  setDiagnostic(diagnostic: boolean) {
    if (diagnostic && this.status.phase !== "ready") throw new Error("请先连接遥控器后开始测试");
    this.emit({ type: "cancel" }); this.sink = undefined;
    this.publish({ diagnostic, frames: 0, level: 0, lastKey: undefined });
  }

  async connect() {
    if (!this.status.supported && !this.makeTransport) {
      this.publish({ phase: "error", error: "请使用 Mac 客户端，或支持蓝牙的 Chrome / Edge 安全网页" }); return;
    }
    this.disconnect(); this.retry = 0;
    this.publish({ enabled: true, phase: "connecting", error: undefined, keyError: undefined });
    window.addEventListener("keydown", this.reserveVoiceKey, true);
    window.addEventListener("keyup", this.reserveVoiceKey, true);
    window.addEventListener("pagehide", this.pageClosed);
    this.transport = this.makeTransport?.(p => this.packet(p))
      ?? (this.status.platform === "macos" ? nativeTransport(p => this.packet(p)) : browserTransport(p => this.packet(p)));
    await this.attempt();
  }
  disconnect() {
    this.generation++;
    window.removeEventListener("keydown", this.reserveVoiceKey, true);
    window.removeEventListener("keyup", this.reserveVoiceKey, true);
    window.removeEventListener("pagehide", this.pageClosed);
    if (this.reconnect) clearTimeout(this.reconnect); this.reconnect = undefined;
    this.failed?.(new Error("连接已取消")); this.failed = this.ready = undefined;
    this.stopAudio(); this.emit({ type: "cancel" });
    this.transport?.close(); this.transport = undefined;
    this.publish({ enabled: false, phase: "idle", diagnostic: false, deviceName: undefined, level: 0 });
  }
  private async attempt() {
    const transport = this.transport;
    if (!transport) return;
    const generation = ++this.generation;
    this.decoder = new AtvvDecoder();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const handshake = new Promise<void>((resolve, reject) => {
        this.ready = resolve; this.failed = reject;
        timer = setTimeout(() => reject(new Error("遥控器连接或语音握手超时，请唤醒遥控器后重试")), 20000);
      });
      // Start before awaiting: Web Bluetooth's chooser still sees the original user activation.
      const link = transport.connect().then(name => {
        if (generation !== this.generation) return;
        this.publish({ deviceName: name });
        return transport.write(GET_CAPS);
      });
      await Promise.all([link, handshake]);
      if (generation !== this.generation) return;
      this.retry = 0; this.publish({ phase: "ready", error: undefined });
    } catch (error) {
      if (generation !== this.generation) return;
      transport.close(); this.stopAudio(); this.emit({ type: "cancel" });
      this.publish({ phase: "error", error: error instanceof Error ? error.message : "遥控器连接失败" });
    } finally {
      if (timer) clearTimeout(timer);
      if (generation === this.generation) this.ready = this.failed = undefined;
    }
  }
  private recover(message: string) {
    if (!this.transport || !this.status.enabled || this.reconnect) return;
    if (this.failed && !this.decoder.ready) { this.failed(new Error(message)); return; }
    this.generation++;
    this.failed?.(new Error(message)); this.failed = this.ready = undefined;
    this.stopAudio(); this.emit({ type: "cancel" }); this.transport.close();
    this.decoder = new AtvvDecoder();
    this.publish({ phase: "reconnecting", level: 0, error: message });
    const delay = Math.min(16000, 1000 * 2 ** this.retry++);
    if (this.retry > 5) { this.publish({ phase: "error", error: "遥控器重连失败，请点击连接重试" }); return; }
    this.reconnect = setTimeout(() => {
      this.reconnect = undefined;
      void this.attempt().then(() => { if (this.status.phase === "error") this.recover(this.status.error || message); });
    }, delay);
  }
  private stopAudio() {
    if (this.heartbeat) clearInterval(this.heartbeat); this.heartbeat = undefined;
    if (this.heldTimeout) clearTimeout(this.heldTimeout); this.heldTimeout = undefined;
    this.sink = undefined;
  }
  private write(bytes: number[]) { void this.transport?.write(new Uint8Array(bytes)).catch(() => this.recover("遥控器控制通道写入失败")); }
  private packet(packet: Packet) {
    if (!this.status.enabled) return;
    if (packet.kind === "disconnected") { this.recover("遥控器已断开，正在重连"); return; }
    if (packet.kind === "error") { this.recover(packet.message); return; }
    if (packet.kind === "keyError") { this.publish({ keyError: packet.message, availableKeys: ["voice"] }); return; }
    if (packet.kind === "keysReady") { this.publish({ keyError: undefined, availableKeys: REMOTE_KEYS.map(k => k.id) }); return; }
    if (packet.kind === "key") {
      if (!REMOTE_KEYS.some(k => k.id === packet.key)) return;
      const key = packet.key as RemoteKey;
      if (key === "voice") return; // ATVV owns voice start/end, never a second HID hold.
      this.key(key, packet.pressed); return;
    }
    try {
      const bytes = new Uint8Array(packet.bytes);
      if (this.status.phase !== "ready" && !(packet.kind === "control" && bytes[0] === 11 && this.ready)) return;
      if (packet.kind === "audio") {
        const pcm = this.decoder.decode(bytes);
        if (!pcm) return;
        let squares = 0; for (const sample of pcm) squares += sample * sample;
        this.publish({ frames: this.status.frames + 1, level: Math.min(1, Math.sqrt(squares / pcm.length) / 6553.6) });
        if (!this.status.diagnostic) this.sink?.(pcm);
        return;
      }
      const event = this.decoder.control(bytes);
      if (event === "ready") this.ready?.();
      if (event === "search") this.write([12, 0]); // Only in response to the remote, never an idle MIC_OPEN.
      if (event === "start") {
        this.publish({ frames: 0, level: 0 }); this.key("voice", true);
        if (!this.status.diagnostic && this.status.bindings.voice === "ptt") {
          const generation = this.generation;
          this.emit({ type: "start", input: (frame, meter) => {
            const sink = (pcm: Int16Array) => {
              frame(pcm);
              meter(Float32Array.from(pcm, value => value / 32768));
            };
            return { start: async () => { if (generation === this.generation) this.sink = sink; },
              stop: () => { if (this.sink === sink) this.sink = undefined; } };
          } });
        }
        this.heartbeat = setInterval(() => this.write([14, this.decoder.streamId]), 2000);
        this.heldTimeout = setTimeout(() => this.recover("本次语音超过30秒，已取消"), 30000);
      }
      if (event === "stop") { this.stopAudio(); this.key("voice", false); if (!this.status.diagnostic) this.emit({ type: "stop" }); this.publish({ level: 0 }); }
    } catch (error) { this.recover(error instanceof Error ? error.message : "遥控器音频无效"); }
  }
  private key(key: RemoteKey, pressed: boolean) {
    this.publish({ lastKey: { key, pressed } });
    const action = this.status.bindings[key];
    if (pressed && !this.status.diagnostic && action !== "ptt" && action !== "none") this.emit({ type: "action", action });
  }
}

export const xiaomiRemoteStore = new XiaomiRemoteStore();
export function useXiaomiRemote() { return useSyncExternalStore(xiaomiRemoteStore.subscribe, xiaomiRemoteStore.snapshot, xiaomiRemoteStore.snapshot); }
