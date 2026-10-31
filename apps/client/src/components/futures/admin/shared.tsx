import type { ReactNode } from "react";
import { HelpButton } from "../../AppHelp";
import { HelpTip } from "../../Blocks";
import { Button } from "../../settings/controls";

/** The owner-only 后台管理 console's sections (the standalone /trader/admin page). */
export type AdminSection = "overview" | "users" | "plans" | "skills" | "invites" | "accounts" | "market-sources" | "servers" | "experience";

/** The left navigation, grouped (owner-approved sketch 2026-10-04): 运营 · 能力 · 交易 · 基础设施.
 *  Each entry carries its 17px line icon. */
export const ADMIN_NAV_GROUPS: Array<{ label: string; items: Array<{ id: AdminSection; label: string; icon: ReactNode }> }> = [
  { label: "运营", items: [
    { id: "overview", label: "概览", icon: <><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></> },
    { id: "users", label: "用户", icon: <><circle cx="9" cy="8" r="3.5" /><path d="M3 20c.8-3.4 3.2-5 6-5s5.2 1.6 6 5" /><path d="M16 4.5a3.5 3.5 0 010 7M18 15c1.6.6 2.6 2.2 3 5" /></> },
    { id: "plans", label: "档位", icon: <path d="M4 7h16M4 12h16M4 17h10" /> },
    { id: "invites", label: "邀请码", icon: <><path d="M4 8h16v4a2 2 0 000 4v4H4v-4a2 2 0 000-4z" /><path d="M14 8v12" strokeDasharray="2 2" /></> },
  ] },
  { label: "能力", items: [
    { id: "skills", label: "技能", icon: <path d="M12 3l1.8 4.6L18 9.4l-4.2 1.8L12 16l-1.8-4.8L6 9.4l4.2-1.8z" /> },
  ] },
  { label: "交易", items: [
    { id: "accounts", label: "交易账户", icon: <><rect x="3" y="6" width="18" height="13" rx="2" /><path d="M3 10h18M7 15h4" /></> },
    { id: "market-sources", label: "行情源", icon: <path d="M4 19V5M4 19h16M8 15l3-4 3 2 5-6" /> },
  ] },
  { label: "基础设施", items: [
    { id: "servers", label: "服务器", icon: <><rect x="3" y="4" width="18" height="7" rx="1.5" /><rect x="3" y="13" width="18" height="7" rx="1.5" /><path d="M7 7.5h.01M7 16.5h.01" /></> },
    // 真实用户监测 (2026-10-13): the pages' anonymous technical metrics.
    { id: "experience", label: "用户体验", icon: <><path d="M3 12h4l3-7 4 14 3-7h4" /></> },
  ] },
];

/** A load figure's tone: ≥60% warning, ≥80% danger (server gauges, usage bars). */
export type LoadLevel = "normal" | "warn" | "danger";
export const loadLevel = (percent: number | null | undefined): LoadLevel =>
  percent == null ? "normal" : percent >= 80 ? "danger" : percent >= 60 ? "warn" : "normal";

/** A thin horizontal meter (usage, CPU/内存 小条, 档位人数); the fill takes its level's color. */
export function Meter({ percent, level = "normal", label }: { percent: number; level?: LoadLevel; label?: string }) {
  const width = Math.max(0, Math.min(100, percent));
  return <span className="fdw-admin-meter" data-level={level} role={label ? "img" : undefined} aria-label={label}>
    <span style={{ width: `${width}%` }} />
  </span>;
}

/** The big-number tiles at the top of a section: label, a monospace figure, a gray note. */
export function AdminTiles({ items, label }: {
  items: Array<{ label: string; value: ReactNode; unit?: ReactNode; note?: ReactNode }>; label: string;
}) {
  return <dl className="fdw-admin-tiles" aria-label={label}>
    {items.map(item => <div key={item.label} className="fdw-admin-tile">
      <dt>{item.label}</dt>
      <dd>{item.value}{item.unit ? <span className="fdw-admin-tile-unit">{item.unit}</span> : null}</dd>
      {item.note ? <dd className="fdw-admin-tile-note">{item.note}</dd> : null}
    </div>)}
  </dl>;
}

// TqKq simulation accounts say how they are connected (the owner's resident
// sidecar is always on, a member's pool connection starts on demand); a paper
// account matches on the site engine (2026-10-03).
const PROVIDER_LABELS: Record<string, string> = {
  "tqkq-sidecar": "快期模拟 · 常驻连接",
  "tqkq-pool": "快期模拟 · 按需连接",
  "paper": "本站模拟 · 站内撮合",
};
// A grant role on ONE trading account — "owner" is that account's owner, not the owner login.
const GRANT_ROLE_LABELS: Record<string, string> = { owner: "所有者", operator: "操作员", viewer: "只读" };
export const providerLabel = (kind: string) => PROVIDER_LABELS[kind] ?? kind;
export const grantRoleLabel = (role: string) => GRANT_ROLE_LABELS[role] ?? role;

/** The 技能 matrix's group order: trading work first, administration last. */
export const SKILL_CATEGORY_ORDER = ["交易", "行情", "策略", "复盘", "研究", "语音", "界面", "系统"];

/** The owner's 每日 token dropdown: 不限, the common budgets, then 自定义…
 *  (a number box appears). A non-preset stored value shows as 自定义 too. */
const TOKEN_PRESETS = [50_000, 100_000, 300_000, 500_000, 1_000_000, 2_000_000, 5_000_000];
export const TOKEN_OPTIONS = [
  { value: "unlimited", label: "不限" },
  ...TOKEN_PRESETS.map(tokens => ({ value: String(tokens), label: `${tokens / 10_000}万` })),
  { value: "custom", label: "自定义…" },
];
export const tokenPreset = (value: number | null) =>
  value != null && TOKEN_PRESETS.includes(value) ? String(value) : undefined;

/** The owner's 每日语音 dropdown: 不限 / 关闭 / common minute budgets / 自定义…. */
const VOICE_SECONDS_PRESETS = [0, ...[10, 30, 60, 120, 240].map(minutes => minutes * 60)];
export const VOICE_OPTIONS = [
  { value: "unlimited", label: "不限" },
  { value: "0", label: "关闭" },
  ...[10, 30, 60, 120, 240].map(minutes => ({ value: String(minutes * 60), label: `${minutes} 分钟` })),
  { value: "custom", label: "自定义…" },
];
/** The guest tier's voice budget keeps its own supported values (≤300 s in 15 s steps). */
export const GUEST_VOICE_OPTIONS = [0, 15, 30, 45, 60, 120, 180, 240, 300]
  .map(seconds => ({ value: String(seconds), label: seconds === 0 ? "关闭" : `${seconds} 秒` }));
export const voicePreset = (value: number | null) =>
  value != null && VOICE_SECONDS_PRESETS.includes(value) ? String(value) : undefined;

export const INVITE_DAY_OPTIONS = [1, 3, 7, 14, 30, 90].map(days => ({ value: String(days), label: `${days} 天` }));

/** 注册来源 humanized: a TqKq first login always carried an invite code. */
export function sourceLabel(invitedBy: string | null): string {
  if (invitedBy === "tqkq-login") return "快期登录 · 邀请码";
  if (invitedBy === "invite-code") return "邀请码注册";
  return invitedBy ?? "—";
}

// One date format each (北京时间): a date alone is 2026-10-08, a date with time
// is 10-01 08:59 — same shape in every table.
export const fmtDateTime = (value: string | null) => {
  if (!value) return "—";
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })
    .formatToParts(new Date(value));
  const get = (type: string) => parts.find(part => part.type === type)?.value ?? "";
  return `${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
};
export const fmtDate = (value: string | null) => !value ? "—"
  : new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value));
export const fmtTokens = (value: number) => value.toLocaleString("zh-CN");
/** A time of day alone (北京时间 22:18) for "采集于" and "上次" lines. */
export const fmtTime = (value: string | null) => !value || Number.isNaN(Date.parse(value)) ? "—"
  : new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));

/** One section's opening row: the title, its help button, a one-line gray hint
 *  on the same row, and at most one primary action on the right. */
/** A section's title row: `help` (what the section is, how it behaves) sits behind a "?" keyed `admin:<helpKey>`;
 *  `hint` stays visible for a status line. */
export function SectionHeader({ title, hint, help, helpKey, action }: { title: string; hint?: ReactNode; help?: ReactNode; helpKey?: string; action?: ReactNode }) {
  return <header className="fdw-admin-section-head">
    <h2>{title}</h2>
    {help && helpKey ? <HelpTip helpKey={`admin:${helpKey}`} label={title}>{help}</HelpTip> : null}
    <HelpButton topic="admin" />
    {hint ? <span className="fdw-admin-section-hint">{hint}</span> : null}
    {action ? <div className="fdw-admin-section-action">{action}</div> : null}
  </header>;
}

/** A small status pill (邀请码状态、默认档、未登录访客). */
export function Tag({ children, tone }: { children: ReactNode; tone?: "default" | "danger" | "primary" }) {
  return <span className={`fdw-admin-tag${tone ? ` ${tone}` : ""}`}>{children}</span>;
}

/** Copy text to the clipboard; when that is refused (insecure context, user
 *  gesture rules) the caller's fallback selects the text instead. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** The destructive text button (删除档位): plain text in the danger color. */
export function DangerButton({ children, onClick, disabled, title }: {
  children: ReactNode; onClick?: () => void; disabled?: boolean; title?: string;
}) {
  return <button type="button" className="fdw-admin-danger" disabled={disabled} title={title} onClick={onClick}>{children}</button>;
}

export { Button };
