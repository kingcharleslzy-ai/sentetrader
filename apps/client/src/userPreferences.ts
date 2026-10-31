import { useCallback, useEffect, useRef, useState } from "react";
import { requestJson } from "./api";
import { DEFAULT_BOARD_COLUMNS, DEFAULT_BOARD_SORT } from "./components/futures/support";
import type { DrawingDefaults } from "./chartDrawingModel";

/**
 * Account preferences (设置重做第 2 步): the Runtime keeps one document per user (`GET/PUT
 * /api/preferences`, PUT is a partial update that returns the whole document with a new version).
 * The types mirror `user_preferences.py`; the defaults here only bridge the moment before the first
 * read so consumers never see `undefined`.
 */
export type DurationSeconds = 60 | 180 | 300 | 900 | 1800 | 3600 | 86400 | 604800;
/** 分时图 (2026-10-02): which chart the workbench opens on; absent on an older Runtime. */
export type ChartView = "kline" | "timeshare";

export interface UserPreferences {
  voice: { voice: string | null; speed: "slow" | "normal" | "fast"; endpointStallSeconds: number; confirmWindowSeconds: number;
    announceFills: boolean; announceBriefs: boolean; autoAnnotate: boolean;
    /** 按住说话: send the transcript on release (true) or put it into the composer. Absent on an older Runtime. */
    pttSendOnRelease?: boolean };
  chart: { defaultDuration: DurationSeconds; defaultView?: ChartView; candleStyle: "candle" | "ohlc"; upDownColors: "cn" | "intl";
    kdParams: { k: number; d: number; smooth: number }; macdParams: { fast: number; slow: number; signal: number };
    subPanes: { kd: boolean; macd: boolean }; aiAnnotationsVisible: boolean; aiAnnotationsAutoClearMinutes: number;
    /** 图表指标可配置 (2026-10-02); absent on an older Runtime (the normalizer fills the defaults). */
    maLines?: Array<{ period: number; enabled: boolean }>;
    boll?: { enabled: boolean; period: number; width: number };
    subPaneList?: Array<"kd" | "macd" | "rsi" | "oi" | "cci" | "volume">;
    rsiParams?: { period: number };
    /** 2026-10-09: CCI's period and the 抛物线 SAR overlay; absent on an older Runtime. */
    cciParams?: { period: number };
    sar?: { enabled: boolean; step: number; max: number };
    maMuted?: number[];
    /** 画线第二版 (2026-10-09): 磁吸 / 全部锁定 / 全部隐藏 and each tool's default style; absent on an older Runtime. */
    drawingMagnet?: boolean; drawingsLocked?: boolean; drawingsHidden?: boolean;
    drawingDefaults?: DrawingDefaults };
  trade: { defaultVolume: number; defaultPriceIntent: "aggressive" | "passive"; closePreference: "today" | "historical" | "auto";
    voiceOrderConfirm: boolean; quickOrder: boolean; aiAutonomy: boolean };
  chat: { replyStyle: "concise" | "detailed"; autoReadScreen: boolean;
    /** 让 AI 记住我 (记忆层 3a); absent on a Runtime before 2026-09-29. */
    memory?: boolean; sendKey: "enter" | "ctrlEnter" };
  notify: { desktop: { fills: boolean; cancels: boolean; emergencyStop: boolean; briefs: boolean;
    /** 到价提醒 (2026-10-02): the desktop notification + sound when a price alert crosses. */
    priceAlerts: boolean }; sound: boolean;
    /** 收盘前提醒 (2026-10-03): 还开着的旧开关键名，日/夜盘最后一段结束前 5 分钟提醒未平仓位。 */
    marketCloseReminder: boolean;
    /** 风险度提醒 (2026-10-03): 风险度到 80% 时每个交易日提醒一次，只提醒。 */
    riskReminder: boolean;
    strategySignalAlerts: boolean;
    /** 设置 → 提醒 (原通知哪些事, 2026-10-06): 开盘前判断 / 盘中异动, one switch per kind over every channel and the lock
     *  screen; absent on an older Runtime — 开盘前判断 reads as on, 盘中异动 as off (its default since 2026-10-14). */
    morningDesk?: boolean; movement?: boolean;
    /** 收盘复盘 (2026-10-06): the 15:15 今日复盘's message, desktop notification and push; absent = on. */
    dailyReview?: boolean;
    /** 保本提醒 (2026-10-06): ask once a day per protected position when its profit covers the threshold;
     *  absent on an older Runtime (read as on, 1 倍风险). */
    breakEven?: boolean;
    breakEvenRule?: BreakEvenRule;
    /** 锁屏上 · 显示金额和手数 (2026-10-06): off → the lock screen shows a figure-free line. */
    lockScreenAmounts?: boolean };
  data: { sessionRetentionDays: number };
  /** The quick-skill bar over the chat composer: pinned skill ids (≤8, only
   *  skills that offer a quick action; an empty list hides the bar). Optional
   *  because a pre-registry Runtime's document has no `ui` category yet. */
  ui?: { quickSkills: string[];
    /** 设置 → 界面 → AI 形象; absent on a Runtime before 2026-09-28. */
    aiAvatar?: "kline" | "eyes" | "pixel";
    /** 设置 → 界面 → 布局 (2026-09-30): hide the collapsed positions bar when the account has
     *  no positions and no working orders. Absent on an older Runtime (defaults to true). */
    autoHidePositionsBar?: boolean;
    /** 手机看盘 (2026-10-09): 报价 / 多图 / 单图 and their settings; absent on an older Runtime
     *  (`normalizeMobileMarket` in mobile/mobileMarketPrefs.ts fills the defaults). */
    mobileMarket?: Partial<MobileMarketPreferences> };
  /** 报价总表 (2026-10-02): which columns the board shows (order = display order; 品种/合约 fixed
   *  first and never in the list), the sort and the exchange filter. Absent on an older Runtime
   *  (the normalizers in support.ts fill the defaults). */
  marketBoard?: { columns: string[]; sort: { key: string; order: string }; exchange: string };
  /** 自选品种 (2026-10-02): the starred products ("<交易所>.<品种>" like "SHFE.rb"), newest last —
   *  one list the board's 自选 filter, the contract drawer and the contract head share. Absent on
   *  an older Runtime (normalizeWatchlist fills the empty default). */
  watchlist?: { products: string[] };
}

export interface MobileChartCell { symbol: string | null; duration: DurationSeconds }
export interface MobileMarketPreferences {
  view: "quotes" | "multi" | "single";
  board: "watchlist" | "main" | "SHFE" | "DCE" | "CZCE" | "CFFEX" | "GFEX" | "INE" | "FOREIGN";
  columns: Array<"last" | "changePercent" | "change" | "openInterest" | "bid" | "ask" | "volume" | "openInterestChange">;
  layout: "four" | "periods" | "six";
  cells: MobileChartCell[];
  periods: DurationSeconds[];
  subPane: "kd" | "macd" | "rsi" | "oi" | "cci" | "volume" | null;
}

/** 保本提醒的阈值: 浮盈达到“成本到止损的距离”的 multiple 倍（0.5–5），或 mode amount 时浮盈达到 amount 元；
 *  外盘美元子账户在 amount 时按 amountUsd 美元（1–10,000,000 整数，null = 外盘按 multiple）。Runtime 的
 *  user_preferences.BreakEvenRule 是权威，这里只镜像。 */
export interface BreakEvenRule { mode: "multiple" | "amount"; multiple: number; amount: number; amountUsd?: number | null }
export const DEFAULT_BREAK_EVEN_RULE: BreakEvenRule = { mode: "multiple", multiple: 1, amount: 1000, amountUsd: null };
export const BREAK_EVEN_AMOUNT_MAX = 10_000_000;

export interface PreferencesDocument { version: number; preferences: UserPreferences }

/** The quick-skill bar's pins before the account has saved anything (mirrors the Runtime's
 *  UiPreferences default): none since 2026-10-05 — the AI's own 建议问题 took their place over the
 *  composer, and 设置 → AI 与对话 still pins skills. The Runtime migrates documents that still hold
 *  the former default pins once; change both together. */
export const DEFAULT_QUICK_SKILLS: string[] = [];

export const DEFAULT_PREFERENCES: UserPreferences = {
  voice: { voice: null, speed: "normal", endpointStallSeconds: 3, confirmWindowSeconds: 15, announceFills: true, announceBriefs: true, autoAnnotate: true, pttSendOnRelease: true },
  chart: { defaultDuration: 300, defaultView: "kline", candleStyle: "candle", upDownColors: "cn", kdParams: { k: 9, d: 3, smooth: 3 }, macdParams: { fast: 12, slow: 26, signal: 9 },
    subPanes: { kd: true, macd: true }, aiAnnotationsVisible: true, aiAnnotationsAutoClearMinutes: 0 },
  trade: { defaultVolume: 1, defaultPriceIntent: "aggressive", closePreference: "auto", voiceOrderConfirm: true, quickOrder: false, aiAutonomy: true },
  chat: { replyStyle: "concise", autoReadScreen: true, memory: true, sendKey: "enter" },
  notify: { desktop: { fills: true, cancels: true, emergencyStop: true, briefs: false, priceAlerts: true }, sound: true, marketCloseReminder: false, riskReminder: true, strategySignalAlerts: true,
    morningDesk: true, movement: false, dailyReview: true, breakEven: true, breakEvenRule: { mode: "multiple", multiple: 1, amount: 1000, amountUsd: null },
    lockScreenAmounts: true },
  data: { sessionRetentionDays: 365 },
  ui: { quickSkills: [...DEFAULT_QUICK_SKILLS], aiAvatar: "kline", autoHidePositionsBar: true },
  marketBoard: { columns: [...DEFAULT_BOARD_COLUMNS], sort: { ...DEFAULT_BOARD_SORT }, exchange: "all" },
  watchlist: { products: [] }
};

/**
 * The chart period the workbench starts on. The account's `chart.defaultDuration` is remembered on this
 * machine so the next launch opens straight on it instead of loading 5 min and then switching.
 */
const STARTUP_DURATION_KEY = "ai-trader:default-duration";
const DURATIONS: DurationSeconds[] = [60, 180, 300, 900, 1800, 3600, 86400, 604800];
export function startupDuration(): DurationSeconds {
  try {
    const stored = Number(localStorage.getItem(STARTUP_DURATION_KEY));
    if (DURATIONS.includes(stored as DurationSeconds)) return stored as DurationSeconds;
  } catch { /* Storage unavailable. */ }
  return DEFAULT_PREFERENCES.chart.defaultDuration;
}
export function rememberStartupDuration(duration: DurationSeconds) {
  try { localStorage.setItem(STARTUP_DURATION_KEY, String(duration)); } catch { /* Local convenience only. */ }
}

/** The chart the workbench opens on (`chart.defaultView`), remembered the same way; 分时 also pins the
 * startup duration to 60 s so the first subscription is already the one the 分时图 needs. */
const STARTUP_CHART_VIEW_KEY = "ai-trader:default-chart-view";
export function startupChartView(): ChartView {
  try {
    const stored = localStorage.getItem(STARTUP_CHART_VIEW_KEY);
    if (stored === "kline" || stored === "timeshare") return stored;
  } catch { /* Storage unavailable. */ }
  return DEFAULT_PREFERENCES.chart.defaultView ?? "kline";
}
export function rememberStartupChartView(view: ChartView) {
  try { localStorage.setItem(STARTUP_CHART_VIEW_KEY, view); } catch { /* Local convenience only. */ }
}

/** `voice.speed` → {voice: {speed}}: the same partial document the `preference` UI command carries. */
export function preferencePatch(key: string, value: unknown): Record<string, unknown> {
  return key.split(".").reduceRight<unknown>((inner, part) => ({ [part]: inner }), value) as Record<string, unknown>;
}

export function preferenceValue(preferences: unknown, key: string): unknown {
  return key.split(".").reduce<unknown>((node, part) => (node && typeof node === "object" ? (node as Record<string, unknown>)[part] : undefined), preferences);
}

/** The readable line a failed PUT leaves (the Runtime's 422 detail when it has one). */
export function preferenceErrorText(error: unknown): string {
  const detail = (error as { detail?: { detail?: unknown } }).detail?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(item => (item as { msg?: string }).msg ?? "").filter(Boolean).join("；") || "设置未保存";
  return error instanceof Error && error.message ? error.message : "设置未保存";
}

export interface PreferencesHandle {
  preferences: UserPreferences;
  /** Undefined until the first successful read; the snapshot reports the document only then. */
  document: PreferencesDocument | undefined;
  status: string;
  /** Partial update; resolves with the whole document. Rejects with the Runtime's message.
   * `patch` may be a function of the latest document, computed when the queued write runs —
   * rapid toggles then stack on what the previous write actually saved instead of a stale render. */
  update: (patch: Record<string, unknown> | ((latest: PreferencesDocument | undefined) => Record<string, unknown>)) => Promise<PreferencesDocument>;
  /** One leaf by dotted path — what a settings row and the `preference` command call. */
  set: (key: string, value: unknown) => Promise<PreferencesDocument>;
  /** Re-read the document: the AI's `update_preferences` writes on the Runtime side. */
  refresh: () => void;
}

/** `baseUrl` is the connected Runtime (undefined while disconnected). */
export function useUserPreferences(baseUrl: string | undefined): PreferencesHandle {
  const [document, setDocument] = useState<PreferencesDocument>();
  const [status, setStatus] = useState("");
  // The version the next write locks on: updated by every read and successful write (not only by renders).
  const latest = useRef<PreferencesDocument>(undefined);
  const [readKey, setReadKey] = useState(0);
  useEffect(() => {
    if (!baseUrl) { latest.current = undefined; setDocument(undefined); return; }
    let cancelled = false;
    requestJson<PreferencesDocument>(`${baseUrl}/api/preferences`)
      .then(result => {
        // A save that landed while this read was in flight already holds a newer version.
        if (cancelled || result.version < (latest.current?.version ?? -1)) return;
        latest.current = result; setDocument(result); setStatus("");
      })
      .catch(() => { if (!cancelled && readKey === 0) setStatus("设置读取失败，显示默认值"); });
    return () => { cancelled = true; };
  }, [baseUrl, readKey]);
  const refresh = useCallback(() => setReadKey(key => key + 1), []);
  // Writes run one after another so each carries the version the previous one returned (a stepper
  // typed digit by digit would otherwise race itself into 409s). A queued function patch reads
  // `latest.current` only when its turn comes, so two quick toggles both land in the document.
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const pendingWrites = useRef(0);
  const update = useCallback((patch: Record<string, unknown> | ((latest: PreferencesDocument | undefined) => Record<string, unknown>)) => {
    pendingWrites.current += 1;
    const run = queue.current.then(() => put(patch).finally(() => { pendingWrites.current -= 1; }));
    queue.current = run.catch(() => undefined);
    return run;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl]);
  const put = async (patch: Record<string, unknown> | ((latest: PreferencesDocument | undefined) => Record<string, unknown>)) => {
    if (!baseUrl) throw new Error("连接后可设置");
    const body = typeof patch === "function" ? patch(latest.current) : patch;
    const version = latest.current?.version;
    try {
      const result = await requestJson<PreferencesDocument>(`${baseUrl}/api/preferences${version != null ? `?version=${version}` : ""}`,
        { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      latest.current = result; setDocument(result); setStatus("已保存");
      return result;
    } catch (error) {
      // Another device changed the document first: re-read, then let the user try again.
      const status409 = (error as { status?: number }).status === 409;
      if (status409) {
        try { const fresh = await requestJson<PreferencesDocument>(`${baseUrl}/api/preferences`); latest.current = fresh; setDocument(fresh); } catch { /* keep what we have */ }
        setStatus("设置已在别处修改，已刷新，请再改一次");
      } else setStatus(preferenceErrorText(error));
      throw error;
    }
  };
  const set = useCallback((key: string, value: unknown) => update(preferencePatch(key, value)), [update]);
  // 另一端改了偏好（AI、别的窗口）：本窗口回到前台时重新读一次，10 秒内最多一次，写入还
  // 排着队时先不读（读到旧版本会把队列里的写入顶成 409）。不加轮询、不加服务端推送。
  const lastFocusRefresh = useRef(0);
  useEffect(() => {
    if (!baseUrl) return;
    const refreshIfDue = () => {
      if (pendingWrites.current > 0) return;
      const now = Date.now();
      if (now - lastFocusRefresh.current < 10_000) return;
      lastFocusRefresh.current = now;
      refresh();
    };
    // The state above is itself named `document`, so reach the DOM one through `window`.
    const onVisibility = () => { if (window.document.visibilityState === "visible") refreshIfDue(); };
    window.addEventListener("focus", refreshIfDue);
    window.document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("focus", refreshIfDue);
      window.document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [baseUrl, refresh]);
  return { preferences: document?.preferences ?? DEFAULT_PREFERENCES, document, status, update, set, refresh };
}
