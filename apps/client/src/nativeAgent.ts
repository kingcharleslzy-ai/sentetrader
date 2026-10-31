import { invoke } from "@tauri-apps/api/core";
import type { components } from "./workbench.generated";
import { observeUi } from "./uiObservation";
import { CLIENT_BUILD } from "./clientBuild";

export type AgentDevice = components["schemas"]["UiDevice"];
type CaptureRequest = components["schemas"]["CaptureRequest"];
type CaptureReceipt = components["schemas"]["CaptureReceipt"];
export type AgentConnectionInfo = AgentDevice & { mcp: { command: string; args: string[] } };
let activeWindow: { baseUrl: string; windowId: string } | undefined;
export const activeAgentWindow = (baseUrl: string) => activeWindow?.baseUrl === baseUrl ? activeWindow.windowId : undefined;
export const registerAgentWindow = (baseUrl: string, windowId: string) => { activeWindow = { baseUrl, windowId }; };
export const forgetAgentWindow = (windowId: string) => { if (activeWindow?.windowId === windowId) activeWindow = undefined; };

let sessionDeviceId: string | undefined;
/** This browser's device id — the UI heartbeat's device.deviceId and the 锁屏提醒 subscription's
 *  device (kept for the page's life when storage is unavailable). */
export function webDeviceId(): string {
  try {
    const stored = localStorage.getItem("ai-trader:web-device") || sessionDeviceId || crypto.randomUUID();
    localStorage.setItem("ai-trader:web-device", stored);
    sessionDeviceId = stored;
  } catch { sessionDeviceId ??= crypto.randomUUID(); }
  return sessionDeviceId!;
}

export async function agentConnectionInfo(): Promise<AgentConnectionInfo> {
  if ("__TAURI_INTERNALS__" in window) {
    const info = await invoke<AgentConnectionInfo>("agent_connection_info");
    return { ...info, build: CLIENT_BUILD.commit };
  }
  const deviceId = webDeviceId();
  return { deviceId, platform: "web", version: CLIENT_BUILD.version, build: CLIENT_BUILD.commit,
    nativeCapture: false, mcp: { command: "", args: [] } };
}

// A WebView that is occluded, minimized or behind a native popup stops painting
// and never delivers an animation frame; a capture must not wait on it forever,
// because the same async loop carries the window's heartbeat.
export const CAPTURE_PAINT_DEADLINE_MS = 1500;
// The native side gives up after 4 s; together with two paint frames the receipt
// must still ride a heartbeat before the Runtime expires the request at 10 s.
export const CAPTURE_NATIVE_DEADLINE_MS = 5000;

/**
 * What the image actually shows. The observation's `context` (market snapshot
 * times, connection details …) changes with every feed refresh and is not part
 * of the picture, so it must not fail a capture.
 */
const VISIBLE_PARTS = ["text", "controls", "regions", "activeDialog", "viewport"] as const;
export function captureFingerprint(observation: ReturnType<typeof observeUi>): Record<string, string> {
  return Object.fromEntries(VISIBLE_PARTS.map(part => [part, JSON.stringify(observation[part] ?? null)]));
}
/** Names of the visible parts that differ between two fingerprints. */
export function changedParts(before: Record<string, string>, after: Record<string, string>): string[] {
  return VISIBLE_PARTS.filter(part => before[part] !== after[part]);
}
const frame = () => new Promise<boolean>(resolve => {
  const timer = setTimeout(() => resolve(false), CAPTURE_PAINT_DEADLINE_MS);
  requestAnimationFrame(() => { clearTimeout(timer); resolve(true); });
});
const withDeadline = <T,>(work: Promise<T>, ms: number): Promise<T> => new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error("capture_deadline")), ms);
  work.then(value => { clearTimeout(timer); resolve(value); }, error => { clearTimeout(timer); reject(error); });
});

/** Capture only the native WebView, after hiding editable/private contents. */
export async function captureNativeWindow(request: CaptureRequest, revision: () => number,
  context: () => Record<string, unknown>): Promise<CaptureReceipt> {
  const receipt: CaptureReceipt = { requestId: request.requestId, observedRevision: revision(), capturedAt: new Date().toISOString() };
  if (observeUi(context()).context.inputValuesOmitted !== true ||
      [...document.querySelectorAll("[data-agent-private]")].some(e => e.getClientRects().length &&
        getComputedStyle(e).visibility !== "hidden" && getComputedStyle(e).display !== "none")) {
    return { ...receipt, error: "private_window" };
  }
  const mask = document.createElement("style");
  mask.textContent = "input:not([type=checkbox]):not([type=radio]),textarea,[contenteditable]:not([contenteditable=false]),[data-agent-private]{visibility:hidden!important}";
  document.head.append(mask);
  try {
    if (!(await frame()) || !(await frame())) return { ...receipt, error: "capture_unavailable" };
    const before = captureFingerprint(observeUi(context()));
    if (revision() !== request.expectedRevision) return { ...receipt, error: "page_changed", detail: "revision" };
    const image = await withDeadline(invoke<components["schemas"]["CapturedImage"]>("capture_agent_window"), CAPTURE_NATIVE_DEADLINE_MS);
    const changed = changedParts(before, captureFingerprint(observeUi(context())));
    if (revision() !== request.expectedRevision) changed.unshift("revision");
    if (changed.length) return { ...receipt, error: "page_changed", detail: changed.join(",") };
    return { ...receipt, observedRevision: revision(), capturedAt: new Date().toISOString(), image };
  } catch {
    return { ...receipt, error: "capture_unavailable" };
  } finally {
    mask.remove();
  }
}
