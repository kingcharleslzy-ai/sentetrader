import { AccountAccessContext } from "../hooks/useAccountAccess";
import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Bell, Bot, Brain, ChartCandlestick, ChevronRight, Info, Keyboard, Mic, Palette, ReceiptText, ShieldCheck, UserRound, X, type LucideIcon } from "lucide-react";
import { HelpButton, openAppHelp } from "./AppHelp";
import { HelpTip } from "./Blocks";
import "./SettingsPanel.css";

type SettingsPage = { id: string; label: string; content: ReactNode; blockedReason?: string; onAttempt?: () => void; parent?: string };

/** The secondary nav: known categories get an icon and a group; any other page joins 其他.
 *  `parent` marks a second-level detail page (设置拆页, 2026-09-30): it has no nav entry —
 *  the parent page links to it — and the nav keeps highlighting its parent. */
const PAGE_META: Record<string, { icon: LucideIcon; group: string }> = {
  account: { icon: UserRound, group: "账户与交易" }, trade: { icon: ReceiptText, group: "账户与交易" },
  ai: { icon: Bot, group: "AI 助手" }, memory: { icon: Brain, group: "AI 助手" }, voice: { icon: Mic, group: "AI 助手" },
  chart: { icon: ChartCandlestick, group: "界面与提醒" }, ui: { icon: Palette, group: "界面与提醒" },
  notify: { icon: Bell, group: "界面与提醒" }, shortcuts: { icon: Keyboard, group: "界面与提醒" },
  data: { icon: ShieldCheck, group: "其他" }, about: { icon: Info, group: "其他" },
};
const GROUP_ORDER = ["账户与交易", "AI 助手", "界面与提醒", "其他"];

/** Settings use one scroll region, a category nav and section cards of aligned rows. */
export function SettingsPanel({ pages, onClose, selectedPage, onPageChange, onHelp }: { pages: SettingsPage[]; onClose: () => void; selectedPage?: string; onPageChange?: (page: string) => void; onHelp?: () => void }) {
  const access = useContext(AccountAccessContext);
  const dialog = useRef<HTMLDialogElement>(null);
  const [selected, setSelected] = useState(pages[0].id);
  const page = pages.find(item => item.id === (selectedPage ?? selected) && !item.blockedReason) ?? pages.find(item => !item.blockedReason) ?? pages[0];
  // A second-level page highlights its parent in the nav.
  const navActive = page.parent ? (pages.find(item => item.id === page.parent)?.id ?? page.id) : page.id;
  const parentLabel = page.parent ? pages.find(item => item.id === page.parent)?.label : undefined;
  const help = onHelp ?? (() => openAppHelp("settings"));
  // Closing unmounts the dialog rather than calling close(), so the browser's own focus return never
  // runs (#484): hand focus back to the control that opened the panel while it is still on the page.
  const [opener] = useState(() => document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => {
      const active = document.activeElement;
      if (opener?.isConnected && (!active || active === document.body || element?.contains(active))) opener.focus();
    };
  }, [opener]);
  return <dialog ref={dialog} className="app-settings-dialog" aria-labelledby="settings-title" data-agent-target="dialog:settings" onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="app-settings-frame">
      <header className="app-settings-header"><h2 id="settings-title">设置</h2><HelpButton topic="settings" onClick={help} /><button type="button" aria-label="关闭系统设置" onClick={onClose}><X /></button></header>
      <div className="app-settings-body">
        <nav className="app-settings-nav" aria-label="设置分类">
          {GROUP_ORDER.map(group => {
            // Within a group the nav follows PAGE_META's order (界面与提醒: 图表 / 界面 / 提醒 / 快捷键), not the pages' order.
            const rank = (id: string) => { const at = Object.keys(PAGE_META).indexOf(id); return at < 0 ? Number.MAX_SAFE_INTEGER : at; };
            const items = pages.filter(item => !item.parent && (PAGE_META[item.id]?.group ?? "其他") === group).sort((a, b) => rank(a.id) - rank(b.id));
            return items.length ? <div key={group} className="app-settings-nav-group" role="group" aria-label={group}>
              <span aria-hidden="true">{group}</span>
              {items.map(item => { const Icon = PAGE_META[item.id]?.icon; return <button type="button" key={item.id} aria-pressed={navActive === item.id} title={item.blockedReason} data-blocked={item.blockedReason ? true : undefined} onClick={() => { if (item.blockedReason) { item.onAttempt?.(); return; } setSelected(item.id); onPageChange?.(item.id); }}>{Icon ? <Icon aria-hidden="true" /> : null}{item.label}</button>; })}
            </div> : null;
          })}
        </nav>
        <div className="app-settings-content" key={page.id} aria-label={page.label} onContextMenu={event => { event.preventDefault(); help(); }}>{parentLabel ? <button type="button" className="app-settings-back" aria-label={`返回${parentLabel}`} onClick={() => { setSelected(page.parent!); onPageChange?.(page.parent!); }}>‹ {parentLabel}</button> : null}<h3 className="app-settings-page-title">{page.label}</h3>{access?.message && <p role="status">{access.message}</p>}{page.blockedReason ? null : page.content}</div>
      </div>
    </div>
  </dialog>;
}

/**
 * An explanation behind a "?" (说明收进问号, 2026-10-08): what a section, setting or link means or why it
 * exists. `helpKey` is the "?"'s stable id (`disclosure: help-tip`, `setting:<key>`), never the display text.
 * `hint` stays visible and is only for what the reader needs while working: a status, a unit or basis, a
 * reason a control is unavailable, a safety boundary.
 */
type SettingHelp = { help?: undefined; helpKey?: undefined } | { help: ReactNode; helpKey: string };

function SettingHelpTip({ help, helpKey, label }: SettingHelp & { label: string }) {
  return help && helpKey ? <HelpTip helpKey={`setting:${helpKey}`} label={label}>{help}</HelpTip> : null;
}

/** A section card: a title (with its "?" and an optional one-line hint beside it) and its rows. */
export function SettingsSection({ title, hint, children, ...help }: { title: string; hint?: ReactNode; children: ReactNode } & SettingHelp) {
  return <section className="app-settings-section" aria-label={title}>
    <div className="app-settings-section-head"><h3>{title}</h3><SettingHelpTip {...help} label={title} />{hint ? <small>{hint}</small> : null}</div>
    <div className="app-settings-rows">{children}</div>
  </section>;
}

/**
 * One setting: label (with its "?", + one grey line of hint) on the left, its single control on the right; an
 * action's status text sits beside the button inside `children`. `icon` (设置 → 提醒: the reminder kind's tag
 * icon) sits before the label.
 */
export function SettingRow({ label, hint, icon, children, ...help }: { label: string; hint?: ReactNode; icon?: ReactNode; children: ReactNode } & SettingHelp) {
  const access = useContext(AccountAccessContext);
  return <div className="app-setting-row">
    <div className="app-setting-label"><span className="app-setting-name">{icon}<span>{label}</span><SettingHelpTip {...help} label={label} /></span>{hint && hint !== access?.message ? <small>{hint}</small> : null}</div>
    <div className="app-setting-value">{children}</div>
  </div>;
}

/** A row-shaped link into a second-level detail page (设置拆页 2026-09-30): same layout as
 *  SettingRow with a chevron on the right; the whole row opens the page (the button covers the row,
 *  the "?" sits above it). */
export function SettingsLink({ label, hint, onOpen, ...help }: { label: string; hint?: ReactNode; onOpen: () => void } & SettingHelp) {
  return <div className="app-setting-link">
    <span className="app-setting-label"><span className="app-setting-name"><span>{label}</span><SettingHelpTip {...help} label={label} /></span>{hint ? <small>{hint}</small> : null}</span>
    <button type="button" className="app-setting-link-open" aria-label={`打开${label}`} onClick={onOpen}><ChevronRight aria-hidden="true" /></button>
  </div>;
}
