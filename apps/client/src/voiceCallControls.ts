import { useEffect, useSyncExternalStore } from "react";
import { useDisclosureSurface } from "./uiSurfaces";
import { voiceCallStore, type VoiceCallStatus } from "./voiceRealtime";

export interface VoiceCallControls {
  status: VoiceCallStatus;
  /** Connecting or connected (anything but idle / error). */
  inCall: boolean;
  /** The Runtime offers real-time voice and this window is connected to it. */
  available: boolean;
  start: () => void;
  /** 复盘页“语音复盘”: open the call with the guided review, or start it in the running call. */
  review: () => void;
  stop: () => void;
  mute: (muted: boolean) => void;
  /** A failed start: the next click retries. */
  retry: () => void;
  /** A failed start the user closes (no microphone, say): back to idle without retrying. */
  dismiss: () => void;
}

/**
 * The page's one call, shared by the floating AI orb and the 对话面板 call bar.
 * Both read the same store and run the same start/stop; only the owner (`register`,
 * the always-mounted orb) publishes `disclosure: voice-call|voice-mute`, so the two
 * surfaces never fight over one registry key.
 */
export function useVoiceCall({ baseUrl, sessionId, voiceAvailable, register = false }: {
  baseUrl?: string;
  sessionId?: string;
  voiceAvailable?: boolean;
  register?: boolean;
}): VoiceCallControls {
  const status = useSyncExternalStore(voiceCallStore.subscribe, voiceCallStore.snapshot, voiceCallStore.snapshot);
  const inCall = status.state !== "idle" && status.state !== "error";
  const available = Boolean(voiceAvailable && baseUrl);
  const start = () => { if (baseUrl) void voiceCallStore.start(baseUrl, sessionId); };
  const review = () => { if (baseUrl) void voiceCallStore.review(baseUrl, sessionId); };
  const stop = () => voiceCallStore.stop();
  const retry = () => { voiceCallStore.dismissError(); start(); };
  useDisclosureSurface("voice-call", { available: register && available, state: inCall,
    apply: wanted => { if (wanted) start(); else stop(); } });
  useDisclosureSurface("voice-mute", { available: register && available && inCall, state: Boolean(status.muted),
    apply: muted => voiceCallStore.mute(muted) });
  useEffect(() => { if (register && !baseUrl && voiceCallStore.inCall()) voiceCallStore.stop(); }, [register, baseUrl]);
  return { status, inCall, available, start, review, stop, mute: muted => voiceCallStore.mute(muted), retry,
    dismiss: () => voiceCallStore.dismissError() };
}

/** mm:ss since the call connected; "--:--" before the first tick. */
export function callDuration(startedAt: number | undefined, now: number): string {
  if (!startedAt) return "00:00";
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
