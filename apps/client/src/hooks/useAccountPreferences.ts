import { useContext, useEffect, useRef, useState } from "react";
import { GuestAppearanceContext } from "../guestAppearance";
import { requestJson } from "../api";
import { useIdentityFence } from "./useIdentityFence";
import { feedLiveNow } from "../runtimeFeed";
import type { components } from "../workbench.generated";

export type AccountPreferences = components["schemas"]["PresentationPreferences"];
type Snapshot = components["schemas"]["PreferencesSnapshot"];

export const defaultPreferences: AccountPreferences = {
  theme: "dark", fontScale: "large", density: "standard", dockSide: "right",
  chart: { kd: true, macd: true, volume: true, crosshair: true, lastPriceLine: false,
    ma5: true, ma10: true, ma25: true, ma60: true, ma200: true },
  chartSizes: { main: 58, kd: 19, macd: 23 },
  positionsBar: "collapsed",
  recentSymbols: [],
  pinnedSymbols: [],
  initialBalance: null
};

function validPreferences(value: unknown): value is AccountPreferences {
  if (!value || typeof value !== "object") return false;
  const item = value as AccountPreferences;
  return ["dark", "light", "system"].includes(item.theme) && ["standard", "large", "extra-large"].includes(item.fontScale)
    && ["compact", "standard", "comfortable"].includes(item.density) && ["left", "right"].includes(item.dockSide)
    && (item.positionsBar === undefined || ["expanded", "collapsed"].includes(item.positionsBar))
    && (item.recentSymbols === undefined || (Array.isArray(item.recentSymbols) && item.recentSymbols.every(symbol => typeof symbol === "string")))
    && (item.pinnedSymbols === undefined || (Array.isArray(item.pinnedSymbols) && item.pinnedSymbols.every(symbol => typeof symbol === "string")))
    && (item.initialBalance === undefined || item.initialBalance === null || (typeof item.initialBalance === "number" && Number.isFinite(item.initialBalance) && item.initialBalance >= 0))
    && Boolean(item.chart && Object.keys(defaultPreferences.chart).every(key => typeof item.chart[key as keyof typeof item.chart] === "boolean"))
    && Boolean(item.chartSizes && [["main", 34, 76], ["kd", 12, 36], ["macd", 12, 36]].every(([key, min, max]) => {
      const size = item.chartSizes[key as keyof typeof item.chartSizes];
      return typeof size === "number" && Number.isFinite(size) && size >= Number(min) && size <= Number(max);
    }));
}

/** Presentation only. Pending edits resume only against the same confirmed remote revision. */
export function useAccountPreferences(baseUrl: string | undefined, scope: string | undefined,
  value: AccountPreferences, apply: (value: AccountPreferences) => void) {
  const guestTheme = useContext(GuestAppearanceContext)?.theme;
  const carryTheme = useRef(guestTheme);
  const fence = useIdentityFence([baseUrl, scope]);
  const latest = useRef({ value, apply });
  latest.current = { value, apply };
  // Counts renders, so an applied document is judged only once the window has rendered it.
  const renders = useRef(0);
  renders.current += 1;
  const [status, setStatus] = useState("");
  const changed = useRef<() => void>(() => {});
  const valueKey = JSON.stringify(value);
  useEffect(() => {
    if (!baseUrl || !scope) return;
    const cacheKey = `ai-trader:preferences:${encodeURIComponent(baseUrl)}:${scope}`;
    const importKey = "ai-trader:preferences-legacy-owner";
    let seed = latest.current.value;
    let pending: { revision: number; preferences: AccountPreferences } | undefined;
    try {
      const cached = localStorage.getItem(cacheKey);
      if (cached) {
        const parsed = JSON.parse(cached);
        seed = parsed.preferences ?? parsed;
        if (Number.isInteger(parsed.pending?.revision) && parsed.pending.revision >= 0 && validPreferences(parsed.pending.preferences)) pending = parsed.pending;
      }
      else if (localStorage.getItem(importKey) && localStorage.getItem(importKey) !== cacheKey) seed = defaultPreferences;
    } catch { seed = defaultPreferences; }
    if (!validPreferences(seed)) seed = defaultPreferences;
    if (carryTheme.current) {
      seed = { ...seed, theme: carryTheme.current };
      if (pending) pending = { ...pending, preferences: { ...pending.preferences, theme: carryTheme.current } };
    }
    let stopped = false, refreshing = true, busy = false;
    let known: Snapshot | undefined;
    // After the remote document is applied, the window renders it — lossily: the pane and MA switches,
    // an empty pin list and the recent symbols come from elsewhere, so the rendered value need not equal
    // the document. The first render after an install is the synced baseline (a carried guest theme still
    // differs, so it is saved); after a pending edit is re-applied, that render is compared as usual.
    // Waiting for an exact match instead blocked every later save for good (AI 审计 2026-10-13:
    // theme and font changes reverted on reload).
    let accepted = JSON.stringify(seed), awaitingRender: "baseline" | "resume" | undefined, baselineTheme: AccountPreferences["theme"] | undefined;
    let appliedAt = 0, idleTicks = 0;
    const awaitRender = (mode: "baseline" | "resume") => { awaitingRender = mode; appliedAt = renders.current; idleTicks = 0; };
    // Settled once a render followed the apply — or, when the apply changed nothing React re-renders for,
    // after the next idle pass (the window already shows all it will show of that document).
    const settleRender = (fromRender: boolean) => {
      if (!awaitingRender || (!fromRender && renders.current === appliedAt && idleTicks++ < 1)) return;
      if (awaitingRender === "baseline") accepted = JSON.stringify({ ...latest.current.value, ...(baselineTheme ? { theme: baselineTheme } : {}) });
      awaitingRender = undefined;
    };
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | undefined;
    const current = () => !stopped && fence.isCurrent();
    const cache = () => {
      try { localStorage.setItem(cacheKey, JSON.stringify({ preferences: known?.preferences ?? seed, pending })); } catch { /* Optional cache. */ }
    };
    const install = (snapshot: Snapshot) => {
      known = snapshot;
      if (!snapshot.preferences) return;
      accepted = JSON.stringify(snapshot.preferences);
      pending = undefined;
      const visible = carryTheme.current ? { ...snapshot.preferences, theme: carryTheme.current } : snapshot.preferences;
      carryTheme.current = undefined;
      baselineTheme = snapshot.preferences.theme;
      awaitRender("baseline");
      latest.current.apply(visible);
      try {
        cache();
        if (!localStorage.getItem(importKey)) localStorage.setItem(importKey, cacheKey);
      } catch { /* Private/restricted storage does not prevent account sync. */ }
    };
    latest.current.apply(seed);
    setStatus("正在读取账号偏好");
    const tick = async () => {
      if (!current() || busy) return;
      busy = true;
      clearTimeout(timer);
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 5000);
      try {
        settleRender(false);
        const rendered = JSON.stringify(latest.current.value);
        if (known && !refreshing && !awaitingRender && rendered !== accepted) {
          pending = { revision: known.revision, preferences: latest.current.value };
          cache();
          const saved = await requestJson<Snapshot>(`${baseUrl}/api/app/preferences`, {
            method: "PUT", signal: controller.signal, keepalive: true,
            body: JSON.stringify({ revision: known.revision, preferences: latest.current.value })
          });
          if (!current()) return;
          // Preserve an edit made while this write was in flight. The next pass
          // submits that edit against the newly confirmed revision.
          known = saved;
          accepted = JSON.stringify(saved.preferences);
          pending = JSON.stringify(latest.current.value) !== accepted
            ? { revision: saved.revision, preferences: latest.current.value } : undefined;
          cache();
          setStatus("已同步到账号");
        } else {
          const remote = await requestJson<Snapshot>(`${baseUrl}/api/app/preferences`, { signal: controller.signal });
          if (!current()) return;
          if (!remote.preferences) {
            const saved = await requestJson<Snapshot>(`${baseUrl}/api/app/preferences`, {
              method: "PUT", signal: controller.signal,
              body: JSON.stringify({ revision: remote.revision, preferences: seed })
            });
            if (!current()) return;
            install(saved);
          } else if (refreshing || !known || remote.revision !== known.revision) {
            if (pending && remote.revision === pending.revision) {
              known = remote;
              accepted = JSON.stringify(remote.preferences);
              awaitRender("resume");
              latest.current.apply(pending.preferences);
              carryTheme.current = undefined;
            } else install(remote);
          }
          refreshing = false;
          setStatus("已同步到账号");
        }
      } catch (error) {
        if (!current()) return;
        // Refresh first after any uncertain write. A changed revision discards
        // the pending edit instead of overwriting another device's settings.
        refreshing = true;
        const code = (error as { status?: number }).status;
        setStatus(code === 409 ? "偏好已在其他设备修改，正在读取最新设置" : "偏好同步暂不可用，恢复连接后读取账号设置");
        if (code === 401 || code === 403 || code === 404) stopped = true;
      } finally {
        clearTimeout(timeout);
        busy = false;
        // Local edits wake the loop through `changed`; the idle read only catches other devices.
        if (current()) timer = setTimeout(() => void tick(), pending && !refreshing ? 16 : feedLiveNow() ? 60_000 : 1500);
      }
    };
    changed.current = () => {
      if (!current() || !known || refreshing) return;
      if (renders.current !== appliedAt) settleRender(true);
      if (!awaitingRender && JSON.stringify(latest.current.value) !== accepted) {
        pending = { revision: known.revision, preferences: latest.current.value };
        cache();
        void tick();
      }
    };
    void tick();
    return () => { stopped = true; changed.current = () => {}; clearTimeout(timer); controller?.abort(); };
  }, [baseUrl, scope, fence]);
  useEffect(() => { changed.current(); }, [valueKey]);
  return status;
}
