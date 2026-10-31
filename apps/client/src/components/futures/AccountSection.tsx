import { useState } from "react";
import { ChevronDown } from "lucide-react";
import type { RuntimeStatus } from "@ai-trader/ui-events";
import type { ModelCatalog } from "../../api";
import { ACCOUNT_STATE_LOADING, type TradingAccountsView } from "../../hooks/useTradingAccounts";
import { useDisclosureSurface, useUiSurface } from "../../uiSurfaces";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { Switch } from "../settings/controls";
import { OpenPaperAccountButton } from "./OpenPaperAccountButton";
import { TradingAccountSelect, simulationAccountText, switchableAccounts } from "./TradingAccountSelect";

/** 设置 → 账户 → 实盘 (2026-10-08): no futures company is connected; the switch stays off and says so. */
export const LIVE_TRADING_UNAVAILABLE = "暂无期货公司接入，实盘暂不可开启";

const ACCOUNT_STATUS_LABELS: Record<string, string> = {
  normal: "已连接", simulated: "演示账户", connecting: "连接中", stale: "账户数据已过期", unavailable: "连接不可用",
};

/** 模拟盘: whether the session's account can place simulation orders now. */
function simulationAvailability(runtime: RuntimeStatus | undefined): string {
  if (!runtime) return "连接中";
  if (runtime.simulationTrading !== true) return "不可用";
  return runtime.accountStatus === "normal" || runtime.accountStatus === "simulated" ? "可用" : "等待账户连接";
}

const minutes = (seconds: number) => `${Math.round(seconds / 60)} 分钟`;

/**
 * 设置 → 账户 → 账户 (2026-10-08): 模拟盘 (可用 / 不可用) and 实盘 (a switch that stays off — no futures
 * company is connected: clicking or `disclosure: live-trading` with open says so and changes nothing;
 * snapshot state.liveTrading), the connection, the 交易账户 grouped 模拟盘 / 实盘 (开通本站模拟 beside it for
 * whoever has no paper account), and 档位 whose daily token / voice quotas fold under the tier name
 * (`disclosure: plan-quota`). Mounted only while the page shows, so its surfaces register with it.
 */
export function AccountSection({ runtime, tradingAccounts, quota, canOpenPaper, baseUrl }: {
  runtime: RuntimeStatus | undefined;
  tradingAccounts: TradingAccountsView;
  quota: ModelCatalog["quota"] | undefined;
  canOpenPaper: boolean;
  baseUrl: string | undefined;
}) {
  const [planOpen, setPlanOpen] = useDisclosureSurface("plan-quota", { available: Boolean(quota) });
  const [liveRefused, setLiveRefused] = useState(false);
  useDisclosureSurface("live-trading", { state: false, apply: open => {
    if (!open) return;
    setLiveRefused(true);
    throw new Error(LIVE_TRADING_UNAVAILABLE);
  } });
  useUiSurface("liveTrading", { state: { state: "off", disabledReason: LIVE_TRADING_UNAVAILABLE } }, []);
  const { directory } = tradingAccounts;
  const current = directory?.accounts.find(entry => entry.id === (tradingAccounts.currentId ?? directory.current));
  const currentText = current ? simulationAccountText(current)
    : tradingAccounts.currentAccount ? `模拟盘 · ${tradingAccounts.currentAccount.displayName}` : "—";
  return <SettingsSection title="账户">
    <SettingRow label="模拟盘"><strong>{simulationAvailability(runtime)}</strong></SettingRow>
    <SettingRow label="实盘" hint={liveRefused ? <span role="status">{LIVE_TRADING_UNAVAILABLE}</span> : undefined}>
      <Switch label="实盘" checked={false} off="关闭" onChange={() => setLiveRefused(true)} />
    </SettingRow>
    <SettingRow label="账户连接"><strong>{ACCOUNT_STATUS_LABELS[runtime?.accountStatus ?? "connecting"]}</strong></SettingRow>
    <SettingRow label="交易账户" hint={canOpenPaper
      ? "会话当前作用的账户；本站模拟开通即有 100 万模拟资金，与快期账户互不影响"
      : "会话当前作用的账户；切换只影响本会话，方案卡和策略跟随创建时的账户"}>
      {directory && switchableAccounts(directory, tradingAccounts.currentId).length >= 2
        ? <TradingAccountSelect directory={directory} currentId={tradingAccounts.currentId} onSwitch={tradingAccounts.switchAccount}
          disabledReason={tradingAccounts.accountReady ? undefined : ACCOUNT_STATE_LOADING} />
        : <strong>{currentText}</strong>}
      {canOpenPaper ? <OpenPaperAccountButton baseUrl={baseUrl} onOpened={tradingAccounts.refresh} /> : null}
    </SettingRow>
    {directory?.accounts.some(entry => entry.providerKind === "paper" && entry.currency === "USD") ? <SettingRow label="本站模拟"
      helpKey="paper-ledgers" help="一个账户两本账：国内合约记人民币，外盘合约在美元账撮合记账，下单不用切账户；顶栏权益、复盘、成交与账户明细都分人民币 / 美元两栏，不换汇、不相加">
      <strong>人民币 · 国内合约；美元 · 外盘合约</strong>
    </SettingRow> : null}
    {quota ? <>
      <SettingRow label="档位" helpKey="plan" help="模型、额度与功能开关跟随档位">
        <button type="button" className="app-button app-disclosure-button" aria-expanded={planOpen}
          aria-label={`${quota.planLabel}：${planOpen ? "收起" : "展开"}每日额度`} onClick={() => setPlanOpen(open => !open)}>
          {quota.planLabel}<ChevronDown aria-hidden="true" />
        </button>
      </SettingRow>
      {planOpen ? <>
        <SettingRow label="今日 token" helpKey="quota-tokens" help="超额当日停止 AI 回复" hint={`每日 ${quota.dailyTokenLimit == null ? "不限" : quota.dailyTokenLimit.toLocaleString("zh-CN")}`}>
          <strong>{quota.usedTokensToday.toLocaleString("zh-CN")}{quota.dailyTokenLimit == null ? "" : ` / ${quota.dailyTokenLimit.toLocaleString("zh-CN")}`}</strong>
        </SettingRow>
        <SettingRow label="今日语音" helpKey="quota-voice" help="用完当日不能再通话" hint={`每日 ${quota.voiceSecondsPerDay == null ? "不限" : minutes(quota.voiceSecondsPerDay)}`}>
          <strong>{minutes(quota.voiceSecondsToday)}{quota.voiceSecondsPerDay == null ? "" : ` / ${minutes(quota.voiceSecondsPerDay)}`}</strong>
        </SettingRow>
      </> : null}
    </> : null}
  </SettingsSection>;
}
