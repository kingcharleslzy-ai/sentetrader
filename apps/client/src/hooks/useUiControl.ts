import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from "react";
import { createUiObserver } from "../uiObservation";
import { requestJson } from "../api";
import { logDesktop } from "../frontendLog";
import { feedLiveNow, onFeedNudge } from "../runtimeFeed";
import { useIdentityFence } from "./useIdentityFence";
import type { components } from "../workbench.generated";
import { agentConnectionInfo, captureNativeWindow, registerAgentWindow, forgetAgentWindow, type AgentDevice } from "../nativeAgent";

export type UiState = components["schemas"]["UiState"];
export type UiChanges = components["schemas"]["UiChanges"];
/** Server treats a window as offline after 75 s without a pulse; 30 s keeps it online with slack for one
 *  failed request. Commands and captures do not wait for it: feed.ui makes the window pulse at once. */
const IDLE_PULSE_MS = 30_000;
/** A drag (dock width, chart pane sizes) changes state on every pointer move; one pulse after it settles. */
const STATE_CHANGE_DEBOUNCE_MS = 250;
type Command = { commandId: string; expectedRevision: number; changes: UiChanges };
/** `reason`: why the window could not run it (the error it threw), for the AI and the readback to say. */
type Receipt = { commandId: string; status: "applied" | "rejected"; reason?: string };
/** Whether this window's heartbeat is accepted; false after three consecutive 422s (shown in the workspace). */
export interface UiControlHealth { registered: boolean; detail: string }
let health: UiControlHealth = { registered: true, detail: "" };
const healthListeners = new Set<() => void>();
function setUiControlHealth(next: UiControlHealth) { health = next; healthListeners.forEach(listener => listener()); }
export function useUiControlHealth(): UiControlHealth {
  return useSyncExternalStore(listener => { healthListeners.add(listener); return () => { healthListeners.delete(listener); }; }, () => health, () => health);
}

/** A fresh consumer per authenticated workspace; receipt uses committed React state. */
export function useUiControl(baseUrl: string | undefined, workspace: string | undefined,
  state: UiState, apply: (changes: UiChanges) => void, context: Record<string, unknown>) {
  const fence = useIdentityFence([baseUrl, workspace]);
  const latest = useRef({ state, apply, revision: 0, baseUrl, workspace, context });
  // A state change (user action or applied command) pulses shortly after it
  // settles, so the Runtime's window registry never lags behind by the 10 s
  // idle heartbeat; a drag collapses into one pulse.
  const wakeRef = useRef<() => void>(() => undefined);
  const settleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useLayoutEffect(() => {
    const previous = latest.current;
    const changed = JSON.stringify(previous.state) !== JSON.stringify(state);
    latest.current = { state, apply, baseUrl, workspace, context, revision: previous.revision + (changed ? 1 : 0) };
    if (changed) {
      clearTimeout(settleTimer.current);
      settleTimer.current = setTimeout(() => wakeRef.current(), STATE_CHANGE_DEBOUNCE_MS);
    }
  });
  useEffect(() => {
    if (baseUrl === undefined || !workspace) return;
    let windowId = crypto.randomUUID();
    const observation = createUiObserver();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let device: AgentDevice | undefined;
    let captureReceipt: components["schemas"]["CaptureReceipt"] | undefined;
    let capturing: Promise<void> | undefined;
    let pending: { command: Command; rejected: boolean; reason?: string } | undefined;
    const current = () => !stopped && fence.isCurrent();
    let inFlight = false;
    let rejected = 0;
    const pulse = async () => {
      if (!current() || inFlight) return;
      inFlight = true;
      clearTimeout(timer);
      // The window says only whether it ran the command; the Runtime judges from the state this heartbeat
      // and the next ones carry whether it took effect (ui_control.changes_match).
      const receipt: Receipt | undefined = pending && { commandId: pending.command.commandId, status: pending.rejected ? "rejected" : "applied",
        ...(pending.rejected && pending.reason ? { reason: pending.reason.slice(0, 300) } : {}) };
      controller = new AbortController();
      let succeeded = false;
      try {
        const result = await requestJson<{ command: Command | null }>(`${baseUrl}/api/ui/pulse`, {
          method: "POST", signal: controller.signal, timeoutMs: 3000,
          // visible (锁屏推送, 2026-10-06): a device whose page is in front gets no lock-screen push.
          body: JSON.stringify({ windowId, revision: latest.current.revision, state: latest.current.state, receipt,
            device, captureReceipt, observation: observation.read(latest.current.context),
            visible: typeof document === "undefined" || document.visibilityState === "visible" })
        });
        if (!current()) return;
        succeeded = true;
        if (rejected >= 3) setUiControlHealth({ registered: true, detail: "" });
        rejected = 0;
        registerAgentWindow(baseUrl, windowId);
        captureReceipt = undefined;
        if (receipt) pending = undefined;
        if (result.command && !pending) {
          const command = result.command;
          pending = { command, rejected: false };
          if (command.expectedRevision !== latest.current.revision) pending.rejected = true;
          else {
            try { latest.current.apply(command.changes); }
            catch (error) { pending.rejected = true; if (error instanceof Error && error.message) pending.reason = error.message; }
          }
        }
        const capture = (result as components["schemas"]["UiPulseResult"]).capture;
        if (capture && !pending && !capturing && current()) {
          // The capture runs beside the heartbeat, never inside it: an occluded
          // WebView may not paint for a long time, and the window must stay
          // registered meanwhile. The receipt rides on a later pulse.
          const captureWindowId = windowId;
          capturing = captureNativeWindow(capture, () => latest.current.revision, () => latest.current.context)
            .then(receiptValue => { if (captureWindowId === windowId) captureReceipt = receiptValue; }, () => undefined)
            .finally(() => { capturing = undefined; wake(); });
        }
      } catch (error) {
        const status = (error as { status?: number }).status;
        if (status === 401 || status === 403) stopped = true;
        if (status === 410) {
          // The registry retired this document; register afresh without replaying its old command or capture.
          forgetAgentWindow(windowId);
          windowId = crypto.randomUUID();
          pending = undefined;
          captureReceipt = undefined;
        }
        // A Runtime that does not know a receipt field rejects the whole heartbeat; drop the
        // receipt rather than repeat the same 422 (the capture then expires server-side).
        if (status === 422) {
          captureReceipt = undefined;
          // Three in a row means the window is not registered at all: say so on screen and in the
          // desktop log instead of failing silently (2026-09-18: a client with v1 drawings did this for 40 min).
          rejected += 1;
          if (rejected === 3) {
            const detail = String((error as { detail?: unknown }).detail !== undefined ? JSON.stringify((error as { detail?: unknown }).detail) : (error as Error).message).slice(0, 2000);
            setUiControlHealth({ registered: false, detail });
            logDesktop("ui-pulse", "界面命令登记失败：/api/ui/pulse 连续 422", detail);
          }
        } else rejected = 0;
      } finally {
        inFlight = false;
        // While the Runtime feed is live it nudges us (feed.ui) as soon as a command or
        // capture waits, so the idle heartbeat only has to keep the window registered.
        const soon = pending || captureReceipt || dirty;
        dirty = false;
        if (current()) timer = setTimeout(() => void pulse(), soon ? 16 : !succeeded || !feedLiveNow() ? 500 : IDLE_PULSE_MS);
      }
    };
    let dirty = false;
    const wake = () => {
      if (!current()) return;
      if (inFlight) { dirty = true; return; }  // carried by the pulse right after the current one
      void pulse();
    };
    wakeRef.current = wake;
    const stopNudge = onFeedNudge(wake);
    void agentConnectionInfo().then(info => {
      if (!current()) return;
      const { deviceId, platform, version, build, nativeCapture } = info;
      device = { deviceId, platform, version, build, nativeCapture };
    }).catch(() => {}).finally(() => { if (current()) void pulse(); });
    const stop = () => {
      observation.close();
      forgetAgentWindow(windowId);
      if (stopped) return;
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      // Keep the authenticated close request alive across reload/navigation.
      // Omit document content to stay below the browser keepalive payload limit.
      void requestJson(`${baseUrl}/api/ui/pulse`, {
        method: "POST", keepalive: true,
        body: JSON.stringify({ windowId, revision: latest.current.revision, state: latest.current.state,
          closing: true, observation: { capturedAt: new Date().toISOString(),
            viewport: { width: window.innerWidth, height: window.innerHeight }, text: "",
            truncated: false, regions: [], controls: [], context: {} } })
      }).catch(() => undefined);
    };
    const pagehide = (event: PageTransitionEvent) => { if (!event.persisted) stop(); };
    window.addEventListener("pagehide", pagehide);
    // The page going to the background or coming back is reported at once, so a push is not held
    // back by a stale "visible" for the 30-second idle heartbeat.
    const visibility = () => wake();
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("focus", wake);
    window.addEventListener("online", wake);
    window.addEventListener("pageshow", wake);
    return () => { window.removeEventListener("pagehide", pagehide); document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("focus", wake); window.removeEventListener("online", wake); window.removeEventListener("pageshow", wake);
      clearTimeout(settleTimer.current); wakeRef.current = () => undefined; stopNudge(); stop(); };
  }, [baseUrl, workspace, fence]);
}
