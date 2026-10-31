import { useState } from "react";
import type { TradingAccountDirectory, TradingAccountDirectoryEntry } from "../../api";
import { Select } from "../settings/controls";

const ROLE_LABELS: Record<string, string> = { operator: "操作员", viewer: "只读" };

/** What the account is, in words a user reads: 本站模拟 (its 外盘 sub-account 本站模拟 · 美元,
 *  2026-10-21 — both read 本站模拟 before), or the display name (the owner's 快期 main account);
 *  a role is added only when it is not the user's own account (操作员 / 只读). Members hold no
 *  快期 trading account since #1205. */
export function tradingAccountLabel(account: Pick<TradingAccountDirectoryEntry, "displayName" | "providerKind" | "role" | "currency">): string {
  const name = account.providerKind === "paper" ? (account.currency === "USD" ? "本站模拟 · 美元" : "本站模拟") : account.displayName;
  return account.role === "owner" ? name : `${name}（${ROLE_LABELS[account.role] ?? account.role}）`;
}

/** 本站模拟合成一个账户 (owner 2026-10-21): the 外盘 USD sub-account is part of 本站模拟 — its 权益, 复盘 and 成交 show
 *  beside the RMB ones — so it is not offered as an account of its own, unless the session already sits on it
 *  (so it can be left). Foreign orders reach it without switching. */
export function switchableAccounts(directory: TradingAccountDirectory, currentId: string | null): TradingAccountDirectoryEntry[] {
  return directory.accounts.filter(account => !(account.providerKind === "paper" && account.currency === "USD")
    || account.id === (currentId ?? directory.current));
}

/** 实盘 is not connected to any futures company: its group holds this one unselectable entry. */
export const NO_LIVE_ACCOUNT = "暂无（未接入期货公司）";
const NO_LIVE_ACCOUNT_VALUE = "live:none";

/** The current account as read-only text (one account, no switcher): it is always a 模拟盘 account. */
export function simulationAccountText(account: Pick<TradingAccountDirectoryEntry, "displayName" | "providerKind" | "role" | "currency">): string {
  return `模拟盘 · ${tradingAccountLabel(account)}`;
}

interface Props {
  directory: TradingAccountDirectory;
  /** The session's account id (runtime.status is the sole source); the directory's
   *  `current` covers the moment before the first status arrives. */
  currentId: string | null;
  onSwitch: (accountId: string) => Promise<string | undefined>;
  /** Why it cannot switch right now (账户状态加载中 before the session's first status, #932 follow-up). */
  disabledReason?: string;
}

/**
 * The trading-account switcher, in 设置 → 账户 only (the header shows no account since
 * 2026-10-05). Renders only when the user is granted two accounts or more; one granted
 * account needs no switcher. The options are grouped 模拟盘 / 实盘 (2026-10-08): every
 * account the Runtime runs is a simulation account, and 实盘 holds only the unselectable
 * “暂无（未接入期货公司）” until a futures company is connected. Selecting goes through the session
 * route — the runtime.status that follows updates the selection, so a failed
 * switch leaves the select on the current account with the message beside it.
 */
export function TradingAccountSelect({ directory, currentId, onSwitch, disabledReason }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const accounts = switchableAccounts(directory, currentId);
  if (accounts.length < 2) return null;
  return <span className="fdw-account-select" data-testid="trading-account-select">
    <Select label="切换交易账户" value={currentId ?? directory.current} disabled={busy || Boolean(disabledReason)}
      groups={[
        { label: "模拟盘", options: accounts.map(account => ({ value: account.id, label: tradingAccountLabel(account) })) },
        { label: "实盘", options: [{ value: NO_LIVE_ACCOUNT_VALUE, label: NO_LIVE_ACCOUNT, disabled: true }] },
      ]}
      onChange={value => {
        setBusy(true);
        void onSwitch(value).then(message => { setError(message); setBusy(false); });
      }} />
    {disabledReason ? <span role="status" className="app-model-switch-error">{disabledReason}</span>
      : error ? <span role="status" className="app-model-switch-error" title={error}>{error}</span> : null}
  </span>;
}
