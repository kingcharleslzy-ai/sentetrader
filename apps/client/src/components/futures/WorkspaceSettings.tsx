import { XiaomiRemoteSettings } from "./XiaomiRemoteSettings";
import { useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { FONT_SCALE_OPTIONS, INTERFACE_DENSITY_OPTIONS } from "../../appearance";
import { changeOwnerPassword, type AgentSkill, type OwnerAuthStatus } from "../../api";
import type { useModelCatalog } from "../../hooks/useModelCatalog";
import type { TradingAccountsView } from "../../hooks/useTradingAccounts";
import { DesktopUpdateActions } from "../DesktopUpdateActions";
import { desktopUpdateStatusLabel } from "../DesktopUpdateProgress";
import { SettingRow, SettingsLink, SettingsPanel, SettingsSection } from "../SettingsPanel";
import { Button, Segmented, Select, Stepper, Switch } from "../settings/controls";
import { useWebUpdateCheck } from "../WebUpdate";
import { BoardColumnsEditor } from "./BoardColumnsEditor";
import type { BoardColumn } from "./support";
import { AccountSection } from "./AccountSection";
import { VoiceSettings } from "./VoiceSettings";
import { openAppHelp } from "../AppHelp";
import { PasswordDialog } from "../PasswordDialog";
import { GUEST_SETTINGS_PAGES } from "../../guestApi";
import { currentSurfaceIds, surfaceAliases, type CurrentSurfaceId } from "../../uiSurfaces";
import { FeeEditor } from "../StrategyWorkbench";
import type { TradingChartSettings } from "../TradingCharts";
import { AddPaneChooser, MaLinesEditor, SubPaneEditor, type ChartIndicatorPatch } from "../chartIndicatorEditor";
import { SUB_PANE_NAMES, type ChartIndicatorConfig } from "../chartIndicators";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { AI_AVATAR_OPTIONS, type DockSide } from "./support";
import { AiAvatar } from "../AiAvatar";
import type { PreferencesHandle } from "../../userPreferences";
import { DEFAULT_QUICK_SKILLS } from "../../userPreferences";
import { quickActionSkills, skillBlockedLine } from "./skillAccess";
import { periods } from "./support";
import { ShortcutsSettings } from "./ShortcutsSettings";
import { DataSettings } from "./DataSettings";
import { MemoryAdvancedSettings, MemorySettings } from "./MemorySettings";
import { RoutineSettings } from "./RoutineSettings";
import { FeedbackSettings } from "./FeedbackSettings";
import { CLIENT_BUILD, shortCommit, useClientCompat } from "../../clientBuild";
import { isTauriRuntime } from "../../runtimeConnection";
import type { RoutineRef } from "../../routines";

/** 设置 → 提醒 moved to the 提醒 panel's 通用提醒 tab (2026-10-25): callers turn this old page id into that tab
 *  before normalizing (a command, the open-settings event, an older client's saved page). */
export const REMINDER_SETTINGS_PAGE = "notify";
export type SettingsPage = Exclude<CurrentSurfaceId<"settingsPages">, typeof REMINDER_SETTINGS_PAGE>;
/** The settings pages (设置重做 step 1; 设置拆页 2026-09-30: 账户与模型拆开、AI/记忆/语音各成一页，详细与高级设置进
 *  *-advanced 二级页; 关于 → 问题与建议 is `feedback`, 2026-10-06): the interface surface table's settingsPages, less
 *  the one that became a 提醒 tab. */
export const SETTINGS_PAGES = currentSurfaceIds("settingsPages").filter((page): page is SettingsPage => page !== REMINDER_SETTINGS_PAGE);
/** Pages of 1.6.5 and earlier (commands, saved state, the fee-editor event) map onto the new categories. */
const LEGACY_SETTINGS_PAGES = surfaceAliases("settingsPages") as Record<string, SettingsPage>;
export function normalizeSettingsPage(page: string | null | undefined): SettingsPage {
  if (page && (SETTINGS_PAGES as readonly string[]).includes(page)) return page as SettingsPage;
  return LEGACY_SETTINGS_PAGES[page ?? ""] ?? "account";
}

interface Props {
  workspace: FuturesDecisionWorkspaceProps;
  page: SettingsPage;
  onPageChange: (page: SettingsPage) => void;
  onClose: () => void;
  model: ReturnType<typeof useModelCatalog>;
  /** The session's account directory; one granted account shows a read-only row. */
  tradingAccounts: TradingAccountsView;
  connectedRuntimeUrl: string | undefined;
  desktopVersion: string | undefined;
  agentModelLabel: string;
  apiSetupRequired: boolean;
  apiProviderName: string;
  preferenceStatus: string;
  chartSettings: TradingChartSettings;
  setChartSettings: Dispatch<SetStateAction<TradingChartSettings>>;
  /** 图表指标可配置 (2026-10-02): the normalized indicator configuration (the chart labels' and
   *  this drawer's editors are the same components). The edit path is undefined while a member
   *  window is disconnected; guests edit locally. */
  chartIndicators: ChartIndicatorConfig;
  onChartIndicatorsChange?: (patch: ChartIndicatorPatch) => void;
  /** The indicator editors' shared inline status (已保存 / 越界原因). */
  chartIndicatorStatus?: string;
  /** 报价总表列可自定义 (2026-10-02): the normalized column list (the board's 列 popover and this
   *  page render the same editor). The edit path is undefined while a member window is
   *  disconnected; guests edit locally. */
  boardColumns: BoardColumn[];
  onBoardColumnsChange?: (columns: BoardColumn[]) => void;
  /** The column editor's shared inline status (已保存 / 错误原因). */
  boardColumnStatus?: string;
  dockSide: DockSide;
  setDockSide: (side: DockSide) => void;
  resetLayout: () => void;
  closed: boolean;
  sessionLabel: string;
  aiExecutionActive: boolean;
  executionStopped: string | null | undefined | false;
  recoveryButton: ReactNode;
  armedStopCount: number;
  onEmergencyStop: () => void;
  /** 设置 → 执行限制 → 只停新开仓: reversible light action, no confirmation dialog. */
  onHaltOpens: () => void;
  /** "快捷下单免确认" lives on this machine only (localStorage, keyed per user), never in the account preferences. */
  quickSkipConfirm: boolean;
  setQuickSkipConfirm: (value: boolean) => void;
  /** The switch is keyed per signed-in user: until the user is known (connecting / reconnecting) it cannot be changed (#782). */
  quickSkipConfirmScoped: boolean;
  /** Account preference: starting equity behind 总盈亏 on the P&L page; null = Runtime default. */
  initialBalance: number | null;
  setInitialBalance: (value: number | null) => void;
  /** Runtime offers real-time voice (key configured); shows the voice row. */
  voiceAvailable: boolean;
  /** M2: the signed-in cloud user's tier (`/api/auth/owner/status`); undefined on a local Runtime. */
  ownerStatus?: OwnerAuthStatus;
  /** Account preferences (设置重做第 2 步): every new row reads and writes this document. */
  preferences: PreferencesHandle;
  /** The skill registry as the current user sees it (null on a pre-registry
   *  Runtime): the skill-gated rows read it, `ownerStatus.features` is the fallback. */
  skills?: AgentSkill[] | null;
  /** 我的流程 (2026-10-06): 运行 sends “跑一下<名称>” in the chat with the routine's id (#522), 改 opens the chat with “改一下『名称』：”. */
  routines?: { run: (routine: RoutineRef) => void; edit: (name: string) => void };
  /** 手机版 (第二轮 2026-11-01): 交易 and 语音 keep only the everyday rows (下单与停止: 默认手数, 默认价格方式, 停止; 语音: see VoiceSettings). */
  mobile?: boolean;
}

/** 设置 → 账户 → 登录 → 登录密码 (2026-10-21): a site-password account changes it here (this device stays
 *  signed in, the others are signed out); a TqKq login is told its password lives with TqKq. */
function PasswordRow({ passwordLogin, username, baseUrl }: { passwordLogin: boolean; username?: string; baseUrl: string | undefined }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false);
  if (!passwordLogin) return <SettingRow label="登录密码" hint="用快期账号登录，密码请在快期修改">{null}</SettingRow>;
  return <SettingRow label="登录密码" hint={done ? <span role="status">已修改，其他设备需用新密码重新登录</span> : undefined}>
    <Button testId="settings:change-password" disabled={!baseUrl} onClick={() => { setDone(false); setOpen(true); }}>修改密码</Button>
    <PasswordDialog surface="change-password" open={open} onOpenChange={setOpen} title="修改登录密码" subject={username ?? ""}
      needCurrent note="改好后这台设备保持登录，其他设备需要用新密码重新登录。" submitLabel="修改密码"
      onSubmit={async (current, next) => { await changeOwnerPassword(baseUrl!, current, next); setDone(true); }} />
  </SettingRow>;
}

const CHART_OPTIONS = [["volume", "成交量"], ["crosshair", "同步十字线"], ["lastPriceLine", "K 线最新价线"]] as const;

/** The settings drawer: 账户 / AI 与对话 / 记忆 / 语音 / 图表 / 交易 / 界面 / 快捷键 /
 *  数据与隐私 / 关于，详细与高级设置在各自的 *-advanced 二级页. */
export function WorkspaceSettings({ workspace, page, onPageChange, onClose, model, tradingAccounts, connectedRuntimeUrl, desktopVersion,
  agentModelLabel, apiSetupRequired, apiProviderName, preferenceStatus, chartSettings, setChartSettings, chartIndicators, onChartIndicatorsChange, chartIndicatorStatus, boardColumns, onBoardColumnsChange, boardColumnStatus, dockSide, setDockSide,
  resetLayout, closed, sessionLabel, aiExecutionActive, executionStopped, recoveryButton, armedStopCount, onEmergencyStop,
  onHaltOpens, quickSkipConfirm, setQuickSkipConfirm, quickSkipConfirmScoped, initialBalance, setInitialBalance, voiceAvailable, ownerStatus, preferences, skills, routines, mobile = false }: Props) {
  const prefs = preferences.preferences;
  const prefStatus = preferences.status ? <span role="status">{preferences.status}</span> : undefined;
  const offline = !connectedRuntimeUrl && !workspace.guest;
  // #712 B5: account-stored settings are greyed out while the app service is unreachable; say why.
  const offlineReason = offline ? "应用服务未连接，连接后可修改" : undefined;
  const setPref = (key: string, value: unknown) => { if (workspace.access?.require("preferences") ?? true) void preferences.set(key, value).catch(() => undefined); };
  const { catalog: modelCatalog, busy: modelBusy, error: modelCatalogError, select: modelSelect } = model;
  // M2: the tier's model list (only what the tier allows is offered) and quota rows.
  const selectableModels = modelCatalog?.models.filter(entry => entry.selectable) ?? [];
  const quota = modelCatalog?.quota;
  // The registry's unified reason lines (null = usable) — the same wordings the
  // Runtime's unavailable_line gives; the legacy features view is the fallback.
  const aiExecutionBlocked = skillBlockedLine("ai-execution", skills, ownerStatus?.features, "AI 自主执行");
  // 快捷技能: the pin list (设置 → AI 与对话) — one row per registry skill with a quick action.
  const pinnedQuickSkills = prefs.ui?.quickSkills ?? DEFAULT_QUICK_SKILLS;
  const quickSkillOptions = quickActionSkills(skills);
  const currentModel = modelCatalog?.models.find(entry => entry.slug === modelCatalog.current);
  const limits = workspace.simulationLimits;
  const stopStatus = workspace.aiExecution?.controls?.stopStatus;
  const stopActionsDisabled = workspace.aiExecutionBusy || workspace.connection !== "connected" || !workspace.simulationGatewayAvailable;
  const compat = useClientCompat();
  const webUpdate = useWebUpdateCheck();
  // 开通本站模拟 sits on the 交易账户 row for whoever has no paper account yet (the owner).
  const canOpenPaper = workspace.mode === "cloud" && workspace.connection === "connected" && Boolean(ownerStatus?.bindings?.paperConfigured)
    && Boolean(tradingAccounts.directory) && !tradingAccounts.directory?.accounts.some(entry => entry.providerKind === "paper" && entry.executionOwner);
  // Web builds have no desktop updater: the build itself knows its version.
  const versionText = desktopVersion ?? workspace.updater.info?.currentVersion ?? CLIENT_BUILD.version;
  const defaultVolumeRow = <SettingRow label="默认手数" hint={prefStatus ?? "打开下单台和快捷下单的起始手数"}><Stepper label="默认手数" value={prefs.trade.defaultVolume} min={1} max={100} unit="手" disabled={offline} onChange={value => { if (value != null) setPref("trade.defaultVolume", Math.round(value)); }} /></SettingRow>;
  const priceIntentRow = <SettingRow label="默认价格方式" helpKey="default-price-intent" help="语音或 AI 下单没说价格时用的挂单价"><Segmented label="默认价格方式" value={prefs.trade.defaultPriceIntent} disabled={offline} options={[{ value: "aggressive", label: "对手价" }, { value: "passive", label: "挂单价" }] as const} onChange={value => setPref("trade.defaultPriceIntent", value)} /></SettingRow>;
  const stopRow = <SettingRow label="执行状态" hint={stopStatus === "EMERGENCY_STOP" ? "已急停" : stopStatus === "HALT_OPENS" ? "已只停新开仓" : stopStatus === "STOPPED" ? "已停止" : aiExecutionActive ? "执行中" : "尚未启动"}>
    {stopStatus === "HALT_OPENS"
      // The fence only strengthens: from halt-opens the user can still upgrade to a full emergency stop.
      ? <>{recoveryButton}<Button danger disabled={stopActionsDisabled} onClick={() => onEmergencyStop()}>全局急停</Button></>
      : executionStopped ? recoveryButton
      : !workspace.replayActive && <><Button disabled={stopActionsDisabled} onClick={() => onHaltOpens()}>只停新开仓</Button><Button danger disabled={stopActionsDisabled} onClick={() => onEmergencyStop()}>全局急停</Button></>}
  </SettingRow>;
  const compatHint = compat.state === "outdated"
    ? <span role="status">Runtime 要求客户端至少 {compat.minVersion ?? "较新版本"}，请更新客户端</span>
    : compat.state === "refresh" ? <span role="status">网页有新版本，点检查更新或刷新页面即可更新</span> : undefined;
  return (
    <SettingsPanel onHelp={() => { if (workspace.access?.require("help") ?? true) openAppHelp("settings"); }} selectedPage={page} onPageChange={next => onPageChange(normalizeSettingsPage(next))} onClose={onClose} pages={[
      { id: "account", label: "账户", content: <>
        <AccountSection runtime={workspace.state.runtime} tradingAccounts={tradingAccounts} quota={quota}
          canOpenPaper={canOpenPaper} baseUrl={connectedRuntimeUrl} />
        {workspace.mode === "cloud" ? <SettingsSection title="登录">
          <SettingRow label="云端账户" hint={workspace.connection === "connected" ? "已登录" : "未登录"}>
            {workspace.connection !== "connected" ? <Button onClick={() => { onClose(); workspace.onOpenCloudLogin(); }}>登录</Button> : null}
            {workspace.connection === "connected" && workspace.onCloudLogout ? <Button onClick={workspace.onCloudLogout}>退出账户</Button> : null}
          </SettingRow>
          {workspace.connection === "connected" && ownerStatus?.passwordLogin !== undefined
            ? <PasswordRow passwordLogin={ownerStatus.passwordLogin} username={ownerStatus.username} baseUrl={connectedRuntimeUrl} />
            : null}
        </SettingsSection> : null}
      </> },
      { id: "ai", label: "AI 与对话", content: <>
        <SettingsSection title="AI 模型">
          <SettingRow label="AI 模型" hint={modelCatalogError ? <span role="status" className="app-model-switch-error">{modelCatalogError}</span> : currentModel ? `${currentModel.displayName} · 已连接` : undefined}>
            {selectableModels.length > 0
              ? <Select label="切换模型" value={modelCatalog!.current ?? ""} disabled={modelBusy} onChange={value => void modelSelect(value)}
                  options={selectableModels.map(entry => ({ value: entry.slug, label: `${entry.displayName}${entry.memberOnly ? "（仅会员）" : ""}` }))} />
              : <strong>{workspace.agentAccount?.authenticated ? `${agentModelLabel} · 已连接` : apiSetupRequired ? `${apiProviderName} · 待配置` : "未就绪"}</strong>}
          </SettingRow>
        </SettingsSection>
        <SettingsSection title="对话">
          <SettingRow label="回复风格" hint={prefStatus ?? (prefs.chat.replyStyle === "concise" ? "第一段口语直答，细节放后面" : "完整展开分析与依据")}>
            <Segmented label="回复风格" value={prefs.chat.replyStyle} disabled={offline} options={[{ value: "concise", label: "简洁" }, { value: "detailed", label: "详细" }] as const} onChange={value => setPref("chat.replyStyle", value)} />
          </SettingRow>
          <SettingRow label="自动读屏" helpKey="auto-read-screen" help="提问时把当前界面快照交给 AI"><Switch label="自动读屏" checked={prefs.chat.autoReadScreen} disabled={offline} onChange={value => setPref("chat.autoReadScreen", value)} /></SettingRow>
          <SettingRow label="发送键"><Segmented label="发送键" value={prefs.chat.sendKey} disabled={offline} options={[{ value: "enter", label: "Enter" }, { value: "ctrlEnter", label: "Ctrl / Cmd + Enter" }] as const} onChange={value => setPref("chat.sendKey", value)} /></SettingRow>
        </SettingsSection>
        {quickSkillOptions.length ? <SettingsSection title="快捷技能">
          {quickSkillOptions.map(skill => (
            <SettingRow key={skill.id} label={skill.label} helpKey={`quick-skill:${skill.id}`} help={`对话输入框上方的按钮，点一下发送：${skill.quickAction?.slice("prompt:".length)}`}>
              <Switch label={skill.label} checked={pinnedQuickSkills.includes(skill.id)}
                disabled={offline || (!pinnedQuickSkills.includes(skill.id) && pinnedQuickSkills.length >= 8)}
                onChange={value => setPref("ui.quickSkills", value ? [...pinnedQuickSkills, skill.id] : pinnedQuickSkills.filter(id => id !== skill.id))} />
            </SettingRow>
          ))}
        </SettingsSection> : null}
      </> },
      { id: "memory", label: "记忆", content: <>
        <MemorySettings baseUrl={connectedRuntimeUrl} preferences={preferences} setPref={setPref} />
        <SettingsLink label="约定、经验与最近变动" helpKey="memory-advanced" help="其余两层记忆、添加一条与整理" onOpen={() => onPageChange("memory-advanced")} />
        {connectedRuntimeUrl && !workspace.guest && routines ? <RoutineSettings baseUrl={connectedRuntimeUrl} onRun={routines.run} onEdit={routines.edit} /> : null}
      </> },
      { id: "memory-advanced", label: "约定、经验与最近变动", parent: "memory", content: <MemoryAdvancedSettings baseUrl={connectedRuntimeUrl} preferences={preferences} /> },
      { id: "voice", label: "语音", content: mobile ? <VoiceSettings compact baseUrl={connectedRuntimeUrl} available={voiceAvailable} preferences={preferences} /> : <>
        <XiaomiRemoteSettings onPageChange={onPageChange} />
        <VoiceSettings baseUrl={connectedRuntimeUrl} available={voiceAvailable} preferences={preferences} />
        <SettingsLink label="判停与确认时限" helpKey="voice-advanced" help="说完判停与口头确认的安全网参数" onOpen={() => onPageChange("voice-advanced")} />
      </> },
      { id: "remote-keys", label: "按键设置", parent: "voice", content: <XiaomiRemoteSettings page="remote-keys" /> },
      { id: "remote-diagnostics", label: "测试与诊断", parent: "voice", content: <XiaomiRemoteSettings page="remote-diagnostics" /> },
      { id: "voice-advanced", label: "判停与确认时限", parent: "voice", content: <VoiceSettings advanced baseUrl={connectedRuntimeUrl} available={voiceAvailable} preferences={preferences} /> },
      { id: "chart", label: "图表", content: <>
        <SettingsSection title="图表显示">
          <SettingRow label="默认周期" hint={offlineReason ?? prefStatus ?? "打开软件时的图表；选分时则直接进分时图"}>
            <Select label="默认周期" value={prefs.chart.defaultView === "timeshare" ? "timeshare" : String(prefs.chart.defaultDuration)} disabled={offline}
              options={[...periods.map(option => ({ value: String(option.value), label: option.label })), { value: "timeshare", label: "分时" }]}
              onChange={value => {
                // 分时 is `chart.defaultView`（数据周期随之钉在 60 秒）; a period pick returns to the K 线 view.
                if (value === "timeshare") setPref("chart.defaultView", "timeshare");
                else { setPref("chart.defaultView", "kline"); setPref("chart.defaultDuration", Number(value)); }
              }} />
          </SettingRow>
          <SettingRow label="K 线样式" hint={offlineReason}><Segmented label="K 线样式" value={prefs.chart.candleStyle} disabled={offline} options={[{ value: "candle", label: "蜡烛" }, { value: "ohlc", label: "美国线" }] as const} onChange={value => setPref("chart.candleStyle", value)} /></SettingRow>
          <SettingRow label="涨跌颜色" hint={offlineReason} helpKey="up-down-colors" help="同时作用于 K 线与行情面板"><Segmented label="涨跌颜色" value={prefs.chart.upDownColors} disabled={offline} options={[{ value: "cn", label: "红涨绿跌" }, { value: "intl", label: "绿涨红跌" }] as const} onChange={value => setPref("chart.upDownColors", value)} /></SettingRow>
          {CHART_OPTIONS.map(([key, label]) => (
            <SettingRow label={label} key={key}>
              <Switch label={label} checked={Boolean(chartSettings[key])} onChange={value => setChartSettings(current => ({ ...current, [key]: value }))} />
            </SettingRow>
          ))}
        </SettingsSection>
        <SettingsSection title="AI 标注" hint={offlineReason}>
          <SettingRow label="默认显示 AI 标注" helpKey="ai-annotations-visible" help="关闭后 AI 画的价位、区间不在图上显示，命令仍会记录"><Switch label="默认显示 AI 标注" checked={prefs.chart.aiAnnotationsVisible} disabled={offline} onChange={value => setPref("chart.aiAnnotationsVisible", value)} /></SettingRow>
          <SettingRow label="标注自动清除" hint="0 = 不自动清除"><Stepper label="标注自动清除" value={prefs.chart.aiAnnotationsAutoClearMinutes} min={0} max={1440} step={5} unit="分钟" disabled={offline} onChange={value => { if (value != null) setPref("chart.aiAnnotationsAutoClearMinutes", Math.round(value)); }} /></SettingRow>
        </SettingsSection>
        <SettingsLink label="指标参数与均线" helpKey="chart-advanced" help="均线、布林带与副图指标的增删改" onOpen={() => onPageChange("chart-advanced")} />
      </> },
      { id: "chart-advanced", label: "指标参数与均线", parent: "chart", content: onChartIndicatorsChange ? <>
        <SettingsSection title="均线与布林带">
          <MaLinesEditor indicators={chartIndicators} onChange={onChartIndicatorsChange} status={chartIndicatorStatus} />
        </SettingsSection>
        <SettingsSection title="副图指标">
          {chartIndicators.subPaneList.map((type, index) => <div className="fdw-ind-pane-slot" key={type}>
            <strong>{`第 ${index + 1} 格 · ${SUB_PANE_NAMES[type]}`}</strong>
            <SubPaneEditor index={index} indicators={chartIndicators} onChange={onChartIndicatorsChange} />
          </div>)}
          <AddPaneChooser indicators={chartIndicators} status={chartIndicatorStatus}
            onAdd={type => onChartIndicatorsChange({ subPaneList: [...chartIndicators.subPaneList, type] })} />
        </SettingsSection>
      </> : <>
        <SettingsSection title="指标参数与均线">
          <SettingRow label="指标参数" hint="连接后可修改均线、布林带与副图指标"><strong>未连接</strong></SettingRow>
        </SettingsSection>
      </> },
      { id: "trade", label: mobile ? "下单与停止" : "交易", content: mobile ? <>
        <SettingsSection title="下单默认">{defaultVolumeRow}{priceIntentRow}</SettingsSection>
        <SettingsSection title="停止">{stopRow}</SettingsSection>
      </> : <>
        <SettingsSection title="交易状态">
          <SettingRow label="模拟网关"><strong>{workspace.simulationGatewayAvailable ? "已连接" : "不可用"}</strong></SettingRow>
          <SettingRow label="当前提交"><strong>{workspace.simulationEnabled ? "可用" : closed ? `${sessionLabel}，暂不可提交` : "暂不可用"}</strong></SettingRow>
          <SettingRow label="实盘交易"><strong>永久关闭</strong></SettingRow>
        </SettingsSection>
        <SettingsSection title="下单默认">
          {defaultVolumeRow}
          {priceIntentRow}
          <SettingRow label="快捷下单免确认" hint={quickSkipConfirmScoped ? quickSkipConfirm ? "下单台快捷行直接提交" : "提交前弹出确认" : offlineReason ?? "正在连接，连上后可修改"}>
            <Switch label="快捷下单免确认" checked={quickSkipConfirm} disabled={!quickSkipConfirmScoped} onChange={setQuickSkipConfirm} />
          </SettingRow>
          <SettingRow label="AI 自主执行" hint={aiExecutionBlocked ?? (prefs.trade.aiAutonomy ? "开：自动下单的策略可直接下模拟单、AI 可挂止损；对话里说的单仍出卡确认" : "关：AI 只能出方案卡由你确认，也不替你挂止损；你自己的操作不受影响")}>
            <Switch label="AI 自主执行" checked={prefs.trade.aiAutonomy} disabled={offline || (!workspace.access && Boolean(aiExecutionBlocked))} onChange={value => { if (workspace.access?.require("ai-execution") ?? !aiExecutionBlocked) setPref("trade.aiAutonomy", value); }} />
          </SettingRow>
        </SettingsSection>
        <SettingsLink label="高级交易设置" helpKey="trade-advanced" help="平今平昨、本金、执行限制与费率" onOpen={() => onPageChange("trade-advanced")} />
      </> },
      { id: "trade-advanced", label: "高级交易设置", parent: "trade", content: <>
        <SettingsSection title="下单细节">
          <SettingRow label="平今平昨" helpKey="close-preference" help="上期所 / 能源中心平仓时的默认顺序"><Segmented label="平今平昨" value={prefs.trade.closePreference} disabled={offline} options={[{ value: "auto", label: "自动" }, { value: "today", label: "优先平今" }, { value: "historical", label: "优先平昨" }] as const} onChange={value => setPref("trade.closePreference", value)} /></SettingRow>
          <SettingRow label="语音下单免口头确认" hint="仅模拟盘：复述后不等“确认”直接提交；实盘永不适用">
            <Switch label="语音下单免口头确认" checked={!prefs.trade.voiceOrderConfirm} disabled={offline} onChange={value => setPref("trade.voiceOrderConfirm", !value)} />
          </SettingRow>
          <SettingRow label="本金" helpKey="initial-balance" help="初始资金 + 后续入金，盈利不计；复盘页总盈亏 = 账户总值 − 本金">
            <Stepper label="本金" value={initialBalance} min={0} step={10000} unit="元" placeholder="1000000" onChange={setInitialBalance} />
          </SettingRow>
        </SettingsSection>
        <SettingsSection title="执行限制">
          <SettingRow label="平台上限" helpKey="simulation-limits" help="模拟盘默认无上限；启用后按下面两项限制 AI 的委托">
            <Switch label="平台上限" checked={limits.enabled} on="已启用" off="未启用" onChange={value => workspace.onSimulationLimitsChange({ ...limits, enabled: value })} />
          </SettingRow>
          {limits.enabled ? <>
            <SettingRow label="单笔最多手数"><Stepper label="单笔最多手数" value={limits.maxOrderVolume} min={1} unit="手" onChange={value => workspace.onSimulationLimitsChange({ ...limits, maxOrderVolume: Math.max(1, Math.trunc(value ?? 1)) })} /></SettingRow>
            <SettingRow label="累计最多委托"><Stepper label="累计最多委托" value={limits.maxOrders} min={1} unit="次" onChange={value => workspace.onSimulationLimitsChange({ ...limits, maxOrders: Math.max(1, Math.trunc(value ?? 1)) })} /></SettingRow>
          </> : null}
          <SettingRow label="限额保存">
            <Button disabled={workspace.simulationLimitsStatus === "连接后可设置"} onClick={() => workspace.onSimulationLimitsChange(limits)} status={workspace.simulationLimitsStatus || "待读取"}>保存</Button>
          </SettingRow>
          {stopRow}
          <SettingRow label="止损保护"><strong>{armedStopCount} 个待触发保护单</strong></SettingRow>
          <SettingRow label="已用委托次数"><strong>{workspace.aiExecution ? workspace.aiExecution.authorization?.usedOrders ?? 0 : "—"}</strong></SettingRow>
        </SettingsSection>
        <SettingsSection title="费率">
          {workspace.rootBaseUrl ?? workspace.runtimeBaseUrl
            ? <FeeEditor rootUrl={(workspace.rootBaseUrl ?? workspace.runtimeBaseUrl)!} symbol={workspace.selectedSymbol ?? ""} />
            : <SettingRow label="费率"><strong>连接后可设置</strong></SettingRow>}
        </SettingsSection>
      </> },
      { id: "ui", label: "界面", content: <>
        <SettingsSection title="外观">
          {preferenceStatus && <SettingRow label="账号偏好"><span role="status">{preferenceStatus}</span></SettingRow>}
          <SettingRow label="主题"><Segmented label="界面主题" value={chartSettings.theme} options={[{ value: "dark", label: "深色" }, { value: "light", label: "浅色" }, { value: "system", label: "跟随系统" }] as const} onChange={value => setChartSettings(current => ({ ...current, theme: value }))} /></SettingRow>
          <SettingRow label="字体大小"><Segmented label="字体大小" value={workspace.fontScale} options={FONT_SCALE_OPTIONS.map(option => ({ value: option.value, label: `${option.label} ${option.percentage}` }))} onChange={value => workspace.onFontScaleChange(value)} /></SettingRow>
          <SettingRow label="界面密度"><Segmented label="界面密度" value={workspace.interfaceDensity} options={INTERFACE_DENSITY_OPTIONS.map(option => ({ value: option.value, label: option.label }))} onChange={value => workspace.onInterfaceDensityChange(value)} /></SettingRow>
        </SettingsSection>
        <SettingsSection title="报价总表" helpKey="board-columns" help="勾选要显示的列，上移/下移调顺序；报价总表顶部的“列”也是这份设置">
          {onBoardColumnsChange
            ? <BoardColumnsEditor columns={boardColumns} onChange={onBoardColumnsChange} status={boardColumnStatus} />
            : <SettingRow label="列" hint="连接后可修改报价总表显示哪些列"><strong>未连接</strong></SettingRow>}
        </SettingsSection>
        {!workspace.guest ? <SettingsSection title="AI 形象">
          <div className="ai-avatar-picker" role="radiogroup" aria-label="AI 形象">
            {AI_AVATAR_OPTIONS.map(option => <button key={option.value} type="button" role="radio" aria-checked={(prefs.ui?.aiAvatar ?? "kline") === option.value} disabled={offline} onClick={() => setPref("ui.aiAvatar", option.value)}>
              <AiAvatar kind={option.value} state="standby" size={44} /><span>{option.label}</span>
            </button>)}
          </div>
        </SettingsSection> : null}
        <SettingsSection title="布局">
          <SettingRow label="辅助区位置"><Segmented label="辅助区位置" value={dockSide} options={[{ value: "left", label: "左侧" }, { value: "right", label: "右侧" }] as const} onChange={setDockSide} /></SettingRow>
          <SettingRow label="底栏自动隐藏" helpKey="auto-hide-positions-bar" help="没有持仓和挂单时隐藏底部持仓条"><Switch label="底栏自动隐藏" checked={prefs.ui?.autoHidePositionsBar ?? true} disabled={offline} onChange={value => setPref("ui.autoHidePositionsBar", value)} /></SettingRow>
          <SettingRow label="辅助区布局"><Button onClick={resetLayout}>恢复默认布局</Button></SettingRow>
        </SettingsSection>
      </> },
      { id: "shortcuts", label: "快捷键", content: <>
        <ShortcutsSettings />
        <SettingsSection title="字体大小">
          <SettingRow label="放大"><kbd>Ctrl / Cmd</kbd><kbd>+</kbd></SettingRow>
          <SettingRow label="缩小"><kbd>Ctrl / Cmd</kbd><kbd>−</kbd></SettingRow>
          <SettingRow label="恢复默认大小"><kbd>Ctrl / Cmd</kbd><kbd>0</kbd></SettingRow>
        </SettingsSection>
      </> },
      { id: "data", label: "数据与隐私", content: <DataSettings workspace={workspace} connectedRuntimeUrl={connectedRuntimeUrl} preferences={preferences} desktopVersion={desktopVersion} /> },
      { id: "about", label: "关于", content: <><SettingsSection title="版本与更新">
          <SettingRow label="软件" helpKey="about-software" help="弈先 · AI 私人交易助理"><strong>Sente</strong></SettingRow>
          <SettingRow label="软件版本" hint={compatHint}><strong>{versionText ? `${versionText}（${shortCommit()}）` : "正在读取"}</strong>
            {/* The web build has no updater: 检查更新 reads the deployed version.json and reloads onto
                a newer build; the desktop keeps its full update row below. */}
            {isTauriRuntime() ? null : <Button testId="settings:web-update" disabled={webUpdate.busy} onClick={webUpdate.check} status={webUpdate.notice || undefined}>{webUpdate.label}</Button>}
          </SettingRow>
          {isTauriRuntime()
            ? <SettingRow label="更新状态"><strong>{workspace.updater.phase === "current" ? "已是最新" : desktopUpdateStatusLabel(workspace.updater)}</strong><DesktopUpdateActions updater={workspace.updater} className="app-setting-button" /></SettingRow>
            : null}
          <SettingRow label="新手引导"><Button testId="settings:tour" onClick={() => { if (workspace.access?.require("help") ?? true) { onClose(); openAppHelp("tour"); } }}>打开新手引导</Button></SettingRow>
        </SettingsSection>
        <SettingsLink label="问题与建议" helpKey="feedback" help="描述遇到的问题或想法，整理成一封反馈邮件" onOpen={() => onPageChange("feedback")} />
      </> },
      { id: "feedback", label: "问题与建议", parent: "about", content: <FeedbackSettings environment={{
        version: versionText ?? null, build: shortCommit() || null, platform: CLIENT_BUILD.platform, runtimeVersion: compat.runtime,
        mode: workspace.mode, connection: workspace.connection }} /> },
      // Guests and a disconnected window still reach 关于 and 问题与建议 (no account data there).
    ].map(item => ({ ...item, blockedReason: GUEST_SETTINGS_PAGES.includes(item.id) ? undefined : workspace.access?.reason("preferences"), onAttempt: () => { workspace.access?.require("preferences"); } }))} />
  );
}
