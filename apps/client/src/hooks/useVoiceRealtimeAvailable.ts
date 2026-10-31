import { useEffect, useState, useSyncExternalStore } from "react";
import { getVoiceRealtimeStatus, voiceCallStore } from "../voiceRealtime";

const STATUS_REFRESH_MS = 60_000;

/** Last answer per Runtime: a remount (page switch, symbol change) must not start from "unknown". */
const knownAvailability = new Map<string, boolean>();
/** Why voice is off, in the user's words; shown on the disabled 开始通话 button. */
const knownReason = new Map<string, string>();
const reasonListeners = new Set<() => void>();
const REASONS: Record<string, string> = {
  voice_key_missing: "服务器未配置语音密钥，暂不能通话",
  cloud_call_limit: "该账号已有一路通话，结束后再试",
  voice_daily_limit: "今天的语音时长已用完，明天再试",
  feature_not_in_plan: "当前档位未开通，联系管理员",
};

/**
 * Whether the connected Runtime offers real-time voice (`GET /api/voice/realtime/status`).
 * Refreshed every minute so a key added on the server shows up without a restart, but a refresh
 * never takes the orb away: a failed poll keeps the last known value and, while a call is
 * running, availability is simply true (1.5.6 dropped calls at the first poll because the
 * Runtime counted the caller's own call against the per-user limit).
 */
export function useVoiceRealtimeAvailable(baseUrl: string | undefined): boolean | undefined {
  const [available, setAvailable] = useState<boolean | undefined>(() => (baseUrl ? knownAvailability.get(baseUrl) : false));
  const inCall = useSyncExternalStore(voiceCallStore.subscribe, () => voiceCallStore.inCall(), () => false);
  useEffect(() => {
    if (!baseUrl) { setAvailable(false); return; }
    let cancelled = false;
    setAvailable(knownAvailability.get(baseUrl));
    const read = async () => {
      try {
        const status = await getVoiceRealtimeStatus(baseUrl);
        // M2: the tier's daily voice minutes being spent — or voice being switched off for the
        // tier — keeps the orb off with a reason; the per-user call limit (cloud_call_limit)
        // still does not — the running call's own poll must not read itself as unavailable.
        const next = status.enabled && status.keyConfigured
          && status.reason !== "voice_daily_limit" && status.reason !== "feature_not_in_plan";
        knownAvailability.set(baseUrl, next);
        const reason = next ? "" : REASONS[status.reason ?? ""] ?? (status.reason ? `语音暂不可用（${status.reason}）` : "语音暂不可用");
        if (knownReason.get(baseUrl) !== reason) { knownReason.set(baseUrl, reason); reasonListeners.forEach(listener => listener()); }
        if (!cancelled) setAvailable(next);
      } catch { /* keep the last known answer; the Runtime being briefly unreachable is not "no voice" */ }
    };
    void read();
    const timer = window.setInterval(() => void read(), STATUS_REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [baseUrl]);
  return inCall ? true : available;
}

/** The last `reason` this Runtime gave for voice being unavailable ("" when it is available). */
export function useVoiceUnavailableReason(baseUrl: string | undefined): string {
  return useSyncExternalStore(
    listener => { reasonListeners.add(listener); return () => { reasonListeners.delete(listener); }; },
    () => (baseUrl ? knownReason.get(baseUrl) ?? "" : "连接后可用"),
    () => "",
  );
}
