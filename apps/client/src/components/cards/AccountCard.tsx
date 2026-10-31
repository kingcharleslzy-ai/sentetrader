import { ChartNoAxesCombined } from "lucide-react";
import type { AccountSummary } from "@ai-trader/ui-events";
import type { AccountSnapshot } from "../../api";
import { CardShell } from "../CardShell";
import { Sparkline } from "../Sparkline";
import { signClass } from "../futures/support";

const yuan = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  maximumFractionDigits: 0
});
const dollars = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0
});

function ageLabel(ageSeconds: number): string {
  const seconds = Math.max(0, Math.floor(ageSeconds));
  if (seconds < 60) return "刚刚";
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}分钟前`;
  return `${Math.floor(seconds / 3_600)}小时前`;
}

export function AccountCard({ account, current, id }: { account: AccountSummary; current?: AccountSnapshot; id?: string }) {
  const dataStatus = current?.dataStatus ?? account.dataStatus ?? "simulated";
  const stale = dataStatus === "stale";
  const unavailable = dataStatus === "unavailable";
  const currentAccount = current ?? account;
  // 外盘二期：美元子账户按美元写。
  const currency = (current?.currency ?? account.currency) === "USD" ? dollars : yuan;
  const accountName = currentAccount.accountName.startsWith("TqKq")
    ? "快期模拟账户"
    : currentAccount.accountName;
  const updatedAt = current ? ageLabel(current.ageSeconds) : account.updatedAt;
  const status = current ? (dataStatus === "normal" ? "normal" : "warning") : account.status;
  const meta = unavailable
    ? `${accountName} · 暂时无法读取`
    : `${accountName} · ${updatedAt}${!current && account.isCached ? " · 上次数据" : stale ? " · 数据未更新" : ""}`;
  const insight = unavailable
    ? "暂时无法读取账户数据，请到右侧“系统”中重试连接。"
    : stale
      ? "账户数据暂未更新，当前数值仅供参考。"
      : current ? "账户已连接，可在右侧查看持仓、委托和成交。" : account.insight;
  const equity = currentAccount.equity;
  const todayPnl = currentAccount.todayPnl;
  const marginRatio = currentAccount.marginRatio;
  const positionCount = currentAccount.positionCount;
  // 本站模拟挂起中的到期结算 (#527): an actual account state, said as is.
  const expiries = (currentAccount.expirySettlements ?? [])
    .map(item => `${item.symbol.split(".").pop()} 到期结算 · ${item.statusLabel}`).join("；");
  return (
    <CardShell
      icon={<ChartNoAxesCombined />}
      title="账户概况"
      meta={unavailable || stale ? meta : undefined}
      badge={unavailable ? "暂时不可用" : stale ? "数据未更新" : status === "normal" ? "正常" : "需要关注"}
      badgeTone={status === "normal" && !stale ? "positive" : "warning"}
      id={id}
      testId="account-card"
      details={unavailable ? null : (
        <>
          <dl className="card-facts">
            <div><dt>账户</dt><dd>{meta}</dd></div>
            <div><dt>说明</dt><dd>{insight}</dd></div>
          </dl>
          <Sparkline values={current ? [equity] : account.equityCurve} label="今日权益微型曲线" />
        </>
      )}
    >
      <div className="metric-grid">
        <div className="metric">
          <span>账户权益</span>
          <strong>{unavailable ? "—" : currency.format(equity)}</strong>
        </div>
        <div className="metric">
          <span>今日盈亏</span>
          <strong className={unavailable ? undefined : signClass(todayPnl, "positive-text", "danger-text")}>
            {unavailable ? "—" : <>{todayPnl > 0 ? "+" : ""}{currency.format(todayPnl)}</>}
          </strong>
        </div>
        <div className="metric">
          <span>保证金占用</span>
          <strong>{unavailable ? "—" : `${Math.round(marginRatio * 10) / 10}%`}</strong>
        </div>
        <div className="metric">
          <span>当前持仓</span>
          <strong>{unavailable ? "—" : `${positionCount} 个`}</strong>
        </div>
      </div>
      {unavailable || stale ? <p className="warning-copy">{insight}</p> : null}
      {expiries && !unavailable ? <p className="warning-copy" data-testid="account-expiry-settlements">{expiries}</p> : null}
    </CardShell>
  );
}
