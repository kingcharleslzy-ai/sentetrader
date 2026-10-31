import { useEffect, useState, type ReactNode } from "react";
import { ChevronRight, Lock } from "lucide-react";
import type { AgentSkill, OwnerAuthStatus } from "../../api";
import { CLIENT_BUILD } from "../../clientBuild";
import { notificationPermission, playNotificationSound, refreshNotificationPermission, requestNotificationPermission, type NotificationPermissionState } from "../../notifications";
import { isTauriRuntime } from "../../runtimeConnection";
import { useDisclosureSurface } from "../../uiSurfaces";
import type { PreferencesHandle } from "../../userPreferences";
import { usePush, type PushController } from "../../webPush";
import type { useAppWorkbench } from "../AiActivity";
import { BreakEvenSelect, BreakEvenUsdRow } from "../BreakEvenThreshold";
import { BriefSubscriptionEditor, useBriefSubscriptions } from "../BriefSubscriptions";
import { BREAK_EVEN_KEY, INFO_REMINDERS, LOCK_SCREEN_AMOUNTS, TRADE_REMINDERS, openedOn, type NotifyKind } from "../notifyKinds";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { Button, Switch } from "../settings/controls";
import type { FuturesDecisionWorkspaceProps } from "../futures/props";
import { skillBlockedLine } from "../futures/skillAccess";
import { normalizeWatchlist } from "../futures/support";
import { KindIcon } from "./ReminderCenter";

const PERMISSION_LABELS: Record<NotificationPermissionState, string> = { granted: "已允许", denied: "已被拒绝，请在系统或浏览器的通知设置里允许", default: "尚未允许", unsupported: "当前环境不支持桌面通知" };

/**
 * 通用提醒 → 锁屏推送 (2026-10-06, web only): this computer's switch — 打开锁屏提醒 runs the browser's
 * permission prompt inside the click — with 发一条试试, the devices that have it on (`disclosure:
 * push-devices`, each removable), then — last — 显示金额和手数, which applies to every device's lock screen.
 * Chrome goes through Google's push service, unreachable here, and says so instead of offering the button.
 */
export function LockScreenSection({ push, offline, devicesOpen, setDevicesOpen, amounts, onAmounts }: {
  push?: PushController; offline: boolean; devicesOpen: boolean; setDevicesOpen: (value: boolean | ((open: boolean) => boolean)) => void;
  amounts: boolean; onAmounts: (value: boolean) => void;
}) {
  const amountsRow = (hint?: string) => <SettingRow label={LOCK_SCREEN_AMOUNTS.label} hint={hint} helpKey="lock-screen-amounts" help={LOCK_SCREEN_AMOUNTS.help}>
    <Switch label={LOCK_SCREEN_AMOUNTS.label} checked={amounts} disabled={offline} onChange={onAmounts} />
  </SettingRow>;
  if (!push) return <SettingsSection title="锁屏推送">{amountsRow()}</SettingsSection>;
  const devices = push.status?.devices ?? [];
  const configured = push.status?.configured === true;
  const browser = push.support.label.split(" · ")[1] ?? "浏览器";
  const unsupported = push.support.support !== "supported" || push.fcm;
  return <SettingsSection title="锁屏推送">
    <SettingRow label="这台电脑收锁屏提醒" hint={unsupported
      ? push.support.reason === "chrome-desktop" || push.fcm ? "Chrome 走谷歌推送，国内收不到；请换 Edge 或 Safari 打开" : "这个浏览器收不到锁屏提醒，请换 Edge 或 Safari"
      : push.status && !configured ? "服务器还没开通锁屏提醒"
      : push.thisDevice ? `已开 · ${openedOn(push.thisDevice.createdAt)} · ${browser}` : `${browser} · 浏览器关着也能收到`}>
      {unsupported ? <strong>收不到</strong> : push.thisDevice ? <>
        <Button disabled={push.busy || offline} onClick={() => void push.test(push.deviceId)}>发一条试试</Button>
        <Button disabled={push.busy || offline} onClick={() => void push.disable()}>关闭锁屏提醒</Button>
      </> : <Button primary disabled={push.busy || offline || !configured} onClick={() => void push.enable()}>打开锁屏提醒</Button>}
    </SettingRow>
    {push.notice ? <SettingRow label="结果"><span className="app-setting-status" role="status">{push.notice}</span></SettingRow> : null}
    <SettingRow label="已开的设备" hint={devices.length
      ? devices.map(item => `${item.platform}${item.deviceId === push.deviceId ? "（这台）" : ""}`).join("、") : "还没有设备开启"}>
      <button type="button" className="app-button" aria-expanded={devicesOpen} disabled={!devices.length} onClick={() => setDevicesOpen(open => !open)}>{devices.length} 台</button>
    </SettingRow>
    {devicesOpen ? devices.map(device => <SettingRow key={device.deviceId} label={device.platform}
      hint={device.deviceId === push.deviceId ? "这台" : `${openedOn(device.createdAt)}${device.lastError ? " · 上次没送达" : ""}`}>
      {device.deviceId === push.deviceId ? <span className="app-setting-status">{device.lastError ? "上次没送达" : "正常"}</span>
        : <Button disabled={push.busy || offline} onClick={() => void push.remove(device.deviceId)}>移除</Button>}
    </SettingRow>) : null}
    {/* A browser that cannot take push (Chrome) has nothing to show on a lock screen: the switch only
        stays when another of your devices has it on, and then says that is where it applies. */}
    {!unsupported ? amountsRow() : devices.length ? amountsRow("对你已开锁屏提醒的设备生效") : null}
  </SettingsSection>;
}

export interface ReminderSettingsProps {
  workspace: FuturesDecisionWorkspaceProps;
  connectedRuntimeUrl: string | undefined;
  preferences: PreferencesHandle;
  appWorkbench: ReturnType<typeof useAppWorkbench>;
  brief: { status: string; saving: boolean; error: string; setSaving: (value: boolean) => void; setError: (value: string) => void };
  skills?: AgentSkill[] | null;
  ownerStatus?: OwnerAuthStatus;
  /** Shown now (the 提醒 panel's or page's 通用提醒 tab): its folds and the lock-screen controls register only then. */
  shown: boolean;
}

/**
 * 通用提醒 (统一提醒 #932, 2026-10-15; moved out of 设置 → 提醒 into the 提醒 panel 2026-10-25): one switch per
 * reminder kind, governing every channel of it (对话、桌面通知、语音播报、锁屏推送 and the 提醒记录); 急停 always
 * reminds. Then 桌面通知 (permission, 提示音) and 锁屏推送.
 */
export function ReminderSettings({ workspace, connectedRuntimeUrl, preferences, appWorkbench, brief, skills, ownerStatus, shown }: ReminderSettingsProps) {
  const prefs = preferences.preferences;
  const prefStatus = preferences.status ? <span role="status">{preferences.status}</span> : undefined;
  const offline = !connectedRuntimeUrl && !workspace.guest;
  const setPref = (key: string, value: unknown) => { if (workspace.access?.require("preferences") ?? true) void preferences.set(key, value).catch(() => undefined); };
  const updatePref = (patch: Record<string, unknown>) => { if (workspace.access?.require("preferences") ?? true) void preferences.update(patch).catch(() => undefined); };
  const [permission, setPermission] = useState<NotificationPermissionState>(notificationPermission);
  useEffect(() => { if (workspace.guest || !shown) return; setPermission(notificationPermission()); void refreshNotificationPermission().then(setPermission); }, [shown, workspace.guest]);
  const { status: briefStatus, saving: briefSaving, error: briefError, setSaving: setBriefSaving, setError: setBriefError } = brief;
  const marketBriefsBlocked = skillBlockedLine("market-briefs", skills, ownerStatus?.features, "行情简讯");
  // AI 简报订阅 (2026-10-05): the 订阅品种 link on the AI 简讯 row opens the products block under the section.
  const briefProductsAvailable = shown && !workspace.guest;
  const briefProducts = useBriefSubscriptions(connectedRuntimeUrl, briefProductsAvailable).data?.products;
  const briefWords = briefProducts === undefined ? "订阅品种" : !briefProducts.length ? "还没订阅"
    : briefProducts.length <= 3 ? briefProducts.map(item => item.name || item.product).join("、") : `${briefProducts.length} 个品种`;
  const [briefProductsOpen, setBriefProductsOpen] = useDisclosureSurface("brief-subscriptions", { available: briefProductsAvailable });
  // 锁屏提醒 (2026-10-06): the web page only — the desktop app keeps its system notifications.
  const pushAvailable = briefProductsAvailable && CLIENT_BUILD.platform === "web" && !isTauriRuntime();
  const push = usePush(connectedRuntimeUrl, pushAvailable);
  const [pushDevicesOpen, setPushDevicesOpen] = useDisclosureSurface("push-devices", { available: pushAvailable });
  // One reminder kind's row — its tag icon, the "?" saying what it is, an extra control, its switch.
  const reminderRow = (kind: NotifyKind, extra: ReactNode) => <SettingRow key={kind.key} label={kind.label} icon={<KindIcon kind={kind.kind} />}
    helpKey={`reminder-${kind.kind}`} help={kind.help}>
    {extra}<Switch label={kind.label} checked={kind.read(prefs.notify)} disabled={offline} onChange={value => updatePref(kind.patch(value))} />
  </SettingRow>;
  return <div className="reminder-settings">
    <SettingsSection title="交易与账户 · 进对话" helpKey="reminders-trade" help="这些事发生时在对话里出一张卡（成交与撤单只进提醒记录），同时在提醒记录里留一条。每类一个开关，同时管对话、桌面通知、语音播报和锁屏推送。">
      {/* 保本提醒 (2026-10-06): when to ask — 浮盈达到止损距离的几倍（默认 1 倍风险）或固定金额 — is its row's dropdown. */}
      {/* In the amount mode the 外盘（美元）row follows it: the USD sub-account's own 美元 threshold (2026-10-21). */}
      {TRADE_REMINDERS.flatMap(kind => kind.key === BREAK_EVEN_KEY
        ? [reminderRow(kind, <BreakEvenSelect notify={prefs.notify} disabled={offline || !kind.read(prefs.notify)} onPatch={updatePref} />),
          <BreakEvenUsdRow key={`${kind.key}.usd`} notify={prefs.notify} disabled={offline || !kind.read(prefs.notify)} onPatch={updatePref} />]
        : [reminderRow(kind, null)])}
    </SettingsSection>
    <SettingsSection title="信息 · 只进提醒记录" helpKey="reminders-info" help="不进对话，只在提醒记录和锁屏推送里出现，不打断你和 AI 的对话。">
      {INFO_REMINDERS.map(kind => reminderRow(kind, kind.kind === "brief" && !workspace.guest
        ? <button type="button" className="app-setting-link-inline" aria-expanded={briefProductsOpen}
          aria-label={`AI 简讯的订阅品种：${briefWords}`} onClick={() => setBriefProductsOpen(value => !value)}>{briefWords}<ChevronRight aria-hidden="true" /></button> : null))}
    </SettingsSection>
    {briefProductsAvailable && briefProductsOpen ? <>
      <SettingsSection title="AI 简讯" helpKey="market-briefs" help="关掉后不再为订阅品种生成简讯，调研面板里的 AI 简讯也不显示；上面的开关只管提不提醒。">
        <SettingRow label="生成简讯" hint={marketBriefsBlocked ?? (briefError || briefStatus)}>
          <Switch label="生成简讯" checked={appWorkbench.config.showMarketBriefs} disabled={briefSaving || !connectedRuntimeUrl || (!workspace.access && Boolean(marketBriefsBlocked))}
            onChange={async enabled => { if (!(workspace.access?.require("market-briefs") ?? !marketBriefsBlocked)) return; setBriefSaving(true); setBriefError(""); try { await appWorkbench.updateConfig({ showMarketBriefs: enabled }); } catch { setBriefError("简讯设置未保存，请重试"); } finally { setBriefSaving(false); } }} />
        </SettingRow>
      </SettingsSection>
      <BriefSubscriptionEditor baseUrl={connectedRuntimeUrl} instruments={workspace.instruments?.items ?? []}
        watchlist={normalizeWatchlist(prefs.watchlist?.products)} disabled={offline || Boolean(marketBriefsBlocked)} />
    </> : null}
    <SettingsSection title="总是提醒">
      <SettingRow label="急停" icon={<KindIcon kind="emergency_stop" />} helpKey="reminder-emergency_stop" help="急停时桌面通知、语音、锁屏推送和提醒记录都会提醒（不写进对话），这一类不能关。">
        <span className="app-setting-locked"><Lock aria-hidden="true" />总是提醒，不能关</span>
      </SettingRow>
    </SettingsSection>
    <SettingsSection title="桌面通知" hint={`系统通知${PERMISSION_LABELS[permission]}`}>
      {permission === "default" ? <SettingRow label="系统通知" hint={prefStatus}>
        <Button onClick={() => void requestNotificationPermission().then(setPermission)}>允许桌面通知</Button>
      </SettingRow> : null}
      <SettingRow label="提示音" helpKey="notify-sound" help="提醒到达时响一声"><Switch label="提示音" checked={prefs.notify.sound} disabled={offline} onChange={value => { setPref("notify.sound", value); if (value) playNotificationSound(true); }} /></SettingRow>
    </SettingsSection>
    <LockScreenSection push={pushAvailable ? push : undefined} offline={offline} devicesOpen={pushDevicesOpen} setDevicesOpen={setPushDevicesOpen}
      amounts={LOCK_SCREEN_AMOUNTS.read(prefs.notify)} onAmounts={value => updatePref(LOCK_SCREEN_AMOUNTS.patch(value))} />
  </div>;
}
