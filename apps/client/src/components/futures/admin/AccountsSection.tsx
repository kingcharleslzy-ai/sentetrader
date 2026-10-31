import type { AdminAccount } from "../../../api";
import { AdminTiles, SectionHeader, grantRoleLabel, providerLabel } from "./shared";

/** The 交易账户 section: the read-only isolation check — who may use which
 *  trading account. Grants change in the accounts file, not here. */
export function AccountsSection({ accounts, failed }: { accounts: AdminAccount[] | undefined; failed?: boolean }) {
  const paper = accounts?.filter(account => account.providerKind === "paper").length ?? 0;
  const tqkq = accounts?.filter(account => account.providerKind.startsWith("tqkq")).length ?? 0;
  const grantees = new Set(accounts?.flatMap(account => account.grants.map(grant => grant.user)) ?? []).size;
  return <>
    <SectionHeader title="交易账户" helpKey="accounts" help="只读核对：谁能用哪个交易账户；授权在账户配置文件里改。" />
    <div className="fdw-admin-section-body">
      {accounts === undefined ? <p className="fdw-admin-empty">{failed ? "交易账户没有读到" : "读取中"}</p> : accounts.length === 0 ? (
        <p className="fdw-admin-empty">还没有交易账户</p>
      ) : <>
        <AdminTiles label="交易账户汇总" items={[
          { label: "本站模拟", value: paper, note: "站内撮合" },
          { label: "快期模拟", value: tqkq, note: "常驻或按需连接" },
          { label: "授权用户", value: grantees, note: "出现在授权里的账号" },
        ]} />
        <div className="fdw-admin-table-card">
          <table className="fdw-admin-table" aria-label="交易账户与授权">
            <thead><tr><th>交易账户</th><th>类型</th><th>执行负责人</th><th>授权用户</th><th className="fdw-admin-col-actions">状态</th></tr></thead>
            <tbody>
              {accounts.map(account => <tr key={account.id}>
                <td><span className="fdw-admin-stacked"><strong>{account.displayName}</strong><span className="fdw-admin-mono-note">{account.id}</span></span></td>
                <td><span className="fdw-admin-kind" data-kind={account.providerKind === "paper" ? "paper" : "tqkq"}>{providerLabel(account.providerKind)}</span></td>
                <td>{account.executionOwner}</td>
                <td className="fdw-admin-wrap">{account.grants.length ? account.grants.map(grant => `${grant.user}（${grantRoleLabel(grant.role)}）`).join("、") : "—"}</td>
                <td className="fdw-admin-col-actions"><span className="fdw-admin-state-text">
                  <span className="fdw-admin-dot" data-tone={account.status === "active" ? "ok" : "idle"} />
                  {account.status === "active" ? "正常" : account.status}
                </span></td>
              </tr>)}
            </tbody>
          </table>
        </div>
      </>}
    </div>
  </>;
}
