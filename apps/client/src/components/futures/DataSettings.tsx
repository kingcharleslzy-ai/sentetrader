import { useState } from "react";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { Button, Stepper } from "../settings/controls";
import type { FuturesDecisionWorkspaceProps } from "./props";
import type { PreferencesHandle } from "../../userPreferences";
import { requestJson } from "../../api";
import { LONG_READ_TIMEOUT_MS } from "../../requestTimeout";
import type { TradeReviewData } from "../../hooks/useTradeReview";
import { REVIEW_EXPORT_DAYS, clearLocalCache, exportConversation, exportDiagnostics, exportReview } from "../../dataExport";

/** 设置 → 数据与隐私: retention, exports and the local-cache reset. Exports are browser downloads. */
export function DataSettings({ workspace, connectedRuntimeUrl, preferences, desktopVersion }: {
  workspace: FuturesDecisionWorkspaceProps; connectedRuntimeUrl: string | undefined; preferences: PreferencesHandle; desktopVersion: string | undefined;
}) {
  const prefs = preferences.preferences;
  const offline = !connectedRuntimeUrl;
  const [cacheStatus, setCacheStatus] = useState("");
  const [reviewStatus, setReviewStatus] = useState("");
  const [diagnosticsStatus, setDiagnosticsStatus] = useState("");
  const exportTheReview = async (format: "json" | "markdown") => {
    if (!connectedRuntimeUrl) return;
    setReviewStatus("读取中");
    try { exportReview(await requestJson<TradeReviewData>(`${connectedRuntimeUrl}/api/trade-review?days=${REVIEW_EXPORT_DAYS}`, { timeoutMs: LONG_READ_TIMEOUT_MS }), format, REVIEW_EXPORT_DAYS); setReviewStatus("已导出"); }
    catch { setReviewStatus("复盘读取失败"); }
  };
  return <>
    <SettingsSection title="数据">
      <SettingRow label="对话与复盘" helpKey="data-storage" help="历史对话在“AI 对话 → 历史对话”里打开"><strong>{workspace.mode === "cloud" ? "保存在云端账号" : "保存在本机 Runtime"}</strong></SettingRow>
      <SettingRow label="会话保留天数" helpKey="session-retention" help="超过天数的对话由 Runtime 清理（7–365）" hint={preferences.status ? <span role="status">{preferences.status}</span> : undefined}>
        <Stepper label="会话保留天数" value={prefs.data.sessionRetentionDays} min={7} max={365} step={7} unit="天" disabled={offline} onChange={value => { if (value != null) void preferences.set("data.sessionRetentionDays", Math.round(value)).catch(() => undefined); }} />
      </SettingRow>
      <SettingRow label="本地缓存" helpKey="local-cache" help="清除本机记住的布局、图表设置和缓存，不影响账号与登录">
        <Button danger onClick={() => setCacheStatus(`已清除 ${clearLocalCache()} 项，重新打开后生效`)} status={cacheStatus || undefined}>清除本地缓存</Button>
      </SettingRow>
      <SettingRow label="凭据" helpKey="credentials" help="快期账号、模型密钥由软件保管，不进入模型参数"><strong>本机 / 服务端保管</strong></SettingRow>
    </SettingsSection>
    <SettingsSection title="导出">
      <SettingRow label="导出对话" helpKey="export-conversation" help="当前对话的全部回合">
        <Button disabled={!workspace.state.turns.length} onClick={() => exportConversation(workspace.state.turns, workspace.chatSessionId, "json")}>对话 JSON</Button>
        <Button disabled={!workspace.state.turns.length} onClick={() => exportConversation(workspace.state.turns, workspace.chatSessionId, "markdown")}>对话 Markdown</Button>
      </SettingRow>
      <SettingRow label="导出复盘" helpKey="export-review" help={`账户近 ${REVIEW_EXPORT_DAYS} 天内平仓的记录`}>
        <Button disabled={offline} onClick={() => void exportTheReview("json")}>复盘 JSON</Button>
        <Button disabled={offline} onClick={() => void exportTheReview("markdown")} status={reviewStatus || undefined}>复盘 Markdown</Button>
      </SettingRow>
      <SettingRow label="导出诊断日志" helpKey="export-diagnostics" help="环境信息、最近一次语音通话时间线；桌面版另含本机 frontend.log 尾部，不含凭据">
        <Button status={diagnosticsStatus || undefined} onClick={() => { setDiagnosticsStatus("导出中"); void exportDiagnostics({ mode: workspace.mode, connection: workspace.connection, runtime: connectedRuntimeUrl ?? null, desktopVersion: desktopVersion ?? null, runtimeVersion: workspace.updater.info?.currentVersion ?? null }).then(setDiagnosticsStatus); }}>诊断 JSON</Button>
      </SettingRow>
    </SettingsSection>
  </>;
}
