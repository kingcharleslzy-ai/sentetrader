/**
 * 设置 → 关于 → 问题与建议 (2026-10-06): the feedback mail draft. Everything here is pure — the page
 * composes a draft from what the user typed plus a fixed whitelist of environment fields, and only
 * the user's own click hands it to their mail program or clipboard. Nothing is stored or uploaded.
 */

/** The public contact address (官网同一个); the only recipient a draft can have. */
export const FEEDBACK_RECIPIENT = "lzy619202200@163.com";

export type FeedbackKind = "problem" | "idea";

export const FEEDBACK_KINDS: Record<FeedbackKind, { label: string; field: string; placeholder: string; expect: string; empty: string }> = {
  problem: { label: "遇到问题", field: "遇到了什么问题", placeholder: "例如：点开回放后一直显示加载中", expect: "例如：几秒内显示回放的 K 线", empty: "请写下遇到了什么问题" },
  idea: { label: "提建议", field: "想改进什么", placeholder: "例如：希望复盘页能按品种筛选", expect: "例如：一眼看到单个品种的盈亏", empty: "请写下想改进什么" },
};

export type FeedbackConnection = "connected" | "connecting" | "disconnected";

/** What the page knows about this build and its Runtime; null = not known (shown as 未知). */
export interface FeedbackEnvironment {
  version: string | null;
  build: string | null;
  platform: "web" | "desktop";
  runtimeVersion: string | null;
  mode: string;
  connection: FeedbackConnection;
}

/** Optional browser facts for the diagnostics: language, theme, time zone and window size only. */
export interface FeedbackBrowser {
  language: string | null;
  theme: string | null;
  timezone: string | null;
  viewport: string | null;
}

const CONNECTION_LABELS: Record<FeedbackConnection, string> = { connected: "已连接", connecting: "连接中", disconnected: "未连接" };
const PLATFORM_LABELS = { web: "网页版", desktop: "桌面版" } as const;

/** A short single-line value: control characters dropped, length capped. */
function clean(value: string | null | undefined, max = 64): string | null {
  const text = (value ?? "").replace(/\p{Cc}/gu, "").trim().slice(0, max);
  return text || null;
}

export function connectionLabel(connection: FeedbackConnection): string {
  return CONNECTION_LABELS[connection] ?? "未知";
}

/** The rows of 自动附带 (also the body's 基本环境 block), in display order. */
export function environmentRows(env: FeedbackEnvironment, occurredAt: Date): Array<[string, string]> {
  return [
    ["版本", clean(env.version) ?? "未知"],
    ["构建", clean(env.build) ?? "未知"],
    ["平台", PLATFORM_LABELS[env.platform]],
    ["Runtime", clean(env.runtimeVersion) ?? "未知"],
    [env.mode === "cloud" ? "云端" : "连接", connectionLabel(env.connection)],
    ["发生时间", formatFeedbackTime(occurredAt)],
  ];
}

const pad = (value: number) => String(value).padStart(2, "0");

/** `2026-10-06 11:40:43（UTC+8）` in the user's local time. */
export function formatFeedbackTime(date: Date): string {
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "−";
  const hours = Math.floor(Math.abs(offset) / 60), minutes = Math.abs(offset) % 60;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    + `（UTC${sign}${hours}${minutes ? `:${pad(minutes)}` : ""}）`;
}

/** Reads the four optional browser facts — nothing else of navigator, location or storage. */
export function readFeedbackBrowser(): FeedbackBrowser {
  let timezone: string | null = null;
  try { timezone = Intl.DateTimeFormat().resolvedOptions().timeZone ?? null; } catch { /* optional */ }
  return {
    language: clean(typeof navigator === "undefined" ? null : navigator.language, 32),
    theme: clean(document.documentElement.dataset.appTheme, 16),
    timezone: clean(timezone),
    viewport: typeof window === "undefined" ? null : `${window.innerWidth}×${window.innerHeight}`,
  };
}

/**
 * 基础诊断: environment and connection state only, field by field — no account, orders, trades,
 * conversation, voice, raw logs, URLs, workspace state or full user agent.
 */
export function feedbackDiagnostics(env: FeedbackEnvironment, browser: FeedbackBrowser, occurredAt: Date): string {
  return JSON.stringify({
    app: "Sente",
    clientVersion: clean(env.version),
    build: clean(env.build),
    platform: env.platform,
    runtimeVersion: clean(env.runtimeVersion),
    mode: clean(env.mode, 16),
    connection: env.connection,
    language: browser.language,
    theme: browser.theme,
    timezone: browser.timezone,
    viewport: browser.viewport,
    collectedAt: occurredAt.toISOString(),
  }, null, 2);
}

export interface FeedbackInput {
  kind: FeedbackKind;
  description: string;
  expectation: string;
  env: FeedbackEnvironment;
  occurredAt: Date;
  /** The diagnostics block, or null when 附上基础诊断 is off. */
  diagnostics: string | null;
}

export interface FeedbackDraft { subject: string; body: string }

/** Fixed format only: the subject never carries user input (no header injection). */
export function feedbackSubject(kind: FeedbackKind, version: string | null): string {
  return `Sente 反馈 · ${FEEDBACK_KINDS[kind].label} · ${clean(version) ?? "未知版本"}`;
}

export function composeFeedback(input: FeedbackInput): FeedbackDraft {
  const kind = FEEDBACK_KINDS[input.kind];
  const lines = [
    `类别：${kind.label}`, "",
    `${kind.field}：`, input.description.trim(), "",
    "期待的结果：", input.expectation.trim() || "（未填写）", "",
    "基本环境（自动附带）",
    ...environmentRows(input.env, input.occurredAt).map(([label, value]) => `${label}：${value}`),
  ];
  if (input.diagnostics) lines.push("", "基础诊断：", input.diagnostics);
  return { subject: feedbackSubject(input.kind, input.env.version), body: lines.join("\n") };
}

/** What 复制反馈内容 and the manual fallback carry: recipient, subject and body. */
export function feedbackPlainText(draft: FeedbackDraft): string {
  return `收件人：${FEEDBACK_RECIPIENT}\n主题：${draft.subject}\n\n${draft.body}`;
}

/** The `mailto:` draft: fixed recipient, subject and body each percent-encoded once, CRLF line breaks. */
export function feedbackMailto(draft: FeedbackDraft): string {
  const body = draft.body.replace(/\r?\n/g, "\r\n");
  return `mailto:${FEEDBACK_RECIPIENT}?subject=${encodeURIComponent(draft.subject)}&body=${encodeURIComponent(body)}`;
}
