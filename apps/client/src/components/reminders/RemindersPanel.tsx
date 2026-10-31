import type { ReactNode } from "react";
import { Bell, PanelRightClose } from "lucide-react";
import { AlertsBlock, FilterBar, ReminderCenterBody, type ReminderCenterProps } from "./ReminderCenter";

/** 提醒 (2026-10-25): 我的提醒 (the ones you set: 条件提醒 since 第 6 批, and 到价提醒), 通用提醒 (the switches that were 设置 → 提醒) and
 *  提醒记录 (the 提醒中心 the bell opened as a popover). */
export type RemindersTab = "mine" | "general" | "history";
export const REMINDER_TABS: [RemindersTab, string][] = [["mine", "我的提醒"], ["general", "通用提醒"], ["history", "提醒记录"]];

function Tabs({ tab, onTab }: { tab: RemindersTab; onTab: (tab: RemindersTab) => void }) {
  return <div role="tablist" aria-label="提醒内容">{REMINDER_TABS.map(([value, label]) =>
    <button type="button" role="tab" key={value} aria-selected={tab === value} onClick={() => onTab(value)}>{label}</button>)}</div>;
}

/** One tab's content, the same on the right rail and the phone page. */
export function RemindersTabBody({ tab, center, settings, conditions, mobile = false }: { tab: RemindersTab; center: ReminderCenterProps; settings: ReactNode;
  /** 条件提醒 (第 6 批): the 只提醒 strategies, above 到价提醒 on 我的提醒. */
  conditions?: ReactNode; mobile?: boolean }) {
  if (tab === "general") return <>{settings}</>;
  if (tab === "mine") {
    return center.alerts?.available ? <div className={mobile ? "rc-mobile-alerts rc-card" : undefined}>
      {conditions}
      <AlertsBlock alerts={center.alerts} contract={center.contract} onSwitchContract={center.onSwitchContract} />
    </div> : <p className="rc-quiet">登录后可以设提醒</p>;
  }
  return <>
    <FilterBar reminders={center.reminders} filter={center.filter} onFilter={center.onFilter} mobile={mobile} />
    <ReminderCenterBody {...center} alerts={undefined} mobile={mobile} />
  </>;
}

/** The right rail's 提醒 panel. */
export function RemindersPanel({ tab, onTab, onClose, center, settings, conditions }: { tab: RemindersTab; onTab: (tab: RemindersTab) => void; onClose: () => void;
  center: ReminderCenterProps; settings: ReactNode; conditions?: ReactNode }) {
  return <section className="fdw-ai open reminders-panel" aria-label="提醒面板" data-agent-target="panel:reminders">
    <header><div><Bell /><span><strong>提醒</strong></span></div><button type="button" aria-label="关闭提醒面板" onClick={onClose}><PanelRightClose /></button></header>
    <Tabs tab={tab} onTab={onTab} />
    <div className="reminders-panel-content" role="tabpanel" aria-label={REMINDER_TABS.find(([value]) => value === tab)?.[1]}>
      <RemindersTabBody tab={tab} center={center} settings={settings} conditions={conditions} />
    </div>
  </section>;
}

export function RemindersTabs({ tab, onTab }: { tab: RemindersTab; onTab: (tab: RemindersTab) => void }) {
  return <Tabs tab={tab} onTab={onTab} />;
}
