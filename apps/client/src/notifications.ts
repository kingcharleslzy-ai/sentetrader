import type { UserPreferences } from "./userPreferences";

/**
 * Desktop notifications and the notification sound (设置 → 提醒). The web client uses the browser's
 * Notification API. Which events notify is the Runtime's decision since #932 (2026-10-15): every
 * reminder arrives as feed.reminder already gated on its kind's switch, so this only posts it.
 * `configureNotifications` runs whenever the document changes (the shared sound switch).
 */
let settings: UserPreferences["notify"] | undefined;
export function configureNotifications(next: UserPreferences["notify"] | undefined) { settings = next; }

export type NotificationPermissionState = "granted" | "denied" | "default" | "unsupported";
const desktop = () => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
// The desktop shell answers asynchronously; the last answer is what the synchronous readers show.
let desktopPermission: NotificationPermissionState = "default";
const invokeDesktop = async <T,>(command: string, args?: Record<string, unknown>): Promise<T> => {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
};
async function probeDesktopPermission(request: boolean): Promise<NotificationPermissionState> {
  try { desktopPermission = await invokeDesktop<NotificationPermissionState>("desktop_notification_permission", { request }); }
  catch { desktopPermission = "unsupported"; }
  return desktopPermission;
}
/** Web: the browser's answer. Desktop: the OS answer from the last probe (`refreshNotificationPermission`). */
export function notificationPermission(): NotificationPermissionState {
  if (desktop()) return desktopPermission;
  return typeof Notification === "undefined" ? "unsupported" : Notification.permission;
}
export async function refreshNotificationPermission(): Promise<NotificationPermissionState> {
  return desktop() ? probeDesktopPermission(false) : notificationPermission();
}
export async function requestNotificationPermission(): Promise<NotificationPermissionState> {
  if (desktop()) return probeDesktopPermission(true);
  if (typeof Notification === "undefined") return "unsupported";
  try { return await Notification.requestPermission(); } catch { return Notification.permission; }
}

let audio: AudioContext | undefined;
/** A short two-tone chime; silent where WebAudio is unavailable or the sound is off. */
export function playNotificationSound(force = false) {
  if (!force && !settings?.sound) return;
  const Context = (globalThis as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext }).AudioContext
    ?? (globalThis as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Context) return;
  try {
    audio ??= new Context();
    const now = audio.currentTime;
    for (const [frequency, at] of [[880, 0], [1175, 0.12]] as const) {
      const oscillator = audio.createOscillator(); const gain = audio.createGain();
      oscillator.frequency.value = frequency; oscillator.connect(gain); gain.connect(audio.destination);
      gain.gain.setValueAtTime(0.0001, now + at); gain.gain.exponentialRampToValueAtTime(0.2, now + at + 0.01); gain.gain.exponentialRampToValueAtTime(0.0001, now + at + 0.18);
      oscillator.start(now + at); oscillator.stop(now + at + 0.2);
    }
  } catch { /* No audio device. */ }
}

/** One reminder (feed.reminder): the Runtime already gated it on its kind's switch (急停 always), so
 * only the permission and the shared sound setting apply here. */
export function announceReminder(title: string, body?: string) {
  playNotificationSound();
  if (desktop()) {
    // The WebView has no Notification API; the shell posts to the OS notification centre.
    void invokeDesktop("desktop_notify", { title, body: body ?? null }).catch(() => undefined);
    return;
  }
  if (notificationPermission() !== "granted") return;
  try { new Notification(title, { body, tag: `ai-trader:reminder:${title}` }); } catch { /* Blocked by the platform. */ }
}
