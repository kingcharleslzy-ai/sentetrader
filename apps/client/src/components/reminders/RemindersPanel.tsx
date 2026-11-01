import type { ReactNode } from "react";
import { Bell, PanelRightClose } from "lucide-react";
import { FilterBar, ReminderCenterBody, type ReminderCenterProps } from "./ReminderCenter";
import { MyReminders, type MyRemindersProps } from "./MyReminders";
import { currentSurfaceIds, surfaceLabel, type CurrentSurfaceId } from "../../uiSurfaces";

/** 提醒 (2026-10-25): 我的提醒 (the ones you set, one list since 改版 2026-10-31: 到某个价, 碰到我画的线 and 按条件), 通用提醒
 *  (the switches that were 设置 → 提醒) and 提醒记录 (the 提醒中心 the bell opened as a popover). */
export type RemindersTab = CurrentSurfaceId<"reminderTabs">;
export const REMINDER_TABS: [RemindersTab, string][] = currentSurfaceIds("reminderTabs").map(tab => [tab, surfaceLabel("reminderTabs", tab)]);

function Tabs({ tab, onTab }: { tab: RemindersTab; onTab: (tab: RemindersTab) => void }) {
  return <div role="tablist" aria-label="提醒内容">{REMINDER_TABS.map(([value, label]) =>
    <button type="button" role="tab" key={value} aria-selected={tab === value} onClick={() => onTab(value)}>{label}</button>)}</div>;
}

/** One tab's content, the same on the right rail and the phone page. */
export function RemindersTabBody({ tab, center, settings, mine, mobile = false }: { tab: RemindersTab; center: ReminderCenterProps; settings: ReactNode;
  /** 我的提醒's own wiring (the Runtime, the chart's contract and period, 按条件 / 编辑 into the editor card). */
  mine?: MyRemindersProps; mobile?: boolean }) {
  if (tab === "general") return <>{settings}</>;
  if (tab === "mine") {
    return center.alerts?.available ? <div className={mobile ? "rc-mobile-alerts rc-card" : undefined}>
      <MyReminders chartTimeframe={300} {...mine} alerts={center.alerts} alertContract={center.contract} onSwitchContract={center.onSwitchContract} />
    </div> : <p className="rc-quiet">登录后可以设提醒</p>;
  }
  return <>
    <FilterBar reminders={center.reminders} filter={center.filter} onFilter={center.onFilter} mobile={mobile} />
    <ReminderCenterBody {...center} mobile={mobile} />
  </>;
}

/** The right rail's 提醒 panel. */
export function RemindersPanel({ tab, onTab, onClose, center, settings, mine }: { tab: RemindersTab; onTab: (tab: RemindersTab) => void; onClose: () => void;
  center: ReminderCenterProps; settings: ReactNode; mine?: MyRemindersProps }) {
  return <section className="fdw-ai open reminders-panel" aria-label="提醒面板" data-agent-target="panel:reminders">
    <header><div><Bell /><span><strong>提醒</strong></span></div><button type="button" aria-label="关闭提醒面板" onClick={onClose}><PanelRightClose /></button></header>
    <Tabs tab={tab} onTab={onTab} />
    <div className="reminders-panel-content" role="tabpanel" aria-label={REMINDER_TABS.find(([value]) => value === tab)?.[1]}>
      <RemindersTabBody tab={tab} center={center} settings={settings} mine={mine} />
    </div>
  </section>;
}

export function RemindersTabs({ tab, onTab }: { tab: RemindersTab; onTab: (tab: RemindersTab) => void }) {
  return <Tabs tab={tab} onTab={onTab} />;
}
