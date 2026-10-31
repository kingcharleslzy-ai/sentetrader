import { useMemo, useState } from "react";
import type { AdminAccount, AdminPlan, AdminUser } from "../../../api";
import { Button, Select, TextInput } from "../../settings/controls";
import { PasswordDialog } from "../../PasswordDialog";
import { Meter, SectionHeader, fmtDateTime, fmtTokens, loadLevel, sourceLabel } from "./shared";

type UserSortKey = "username" | "plan" | "status" | "tokensToday" | "voiceToday" | "cost" | "lastSeenAt" | "createdAt";

/** The 用户 section: the full member table. 档位/状态 stay dropdowns that save
 *  immediately; every edit reports its result inside its own row. */
export function UsersSection({ users, plans, accounts, accountsFailed, rowStatus, onChangeUser, onResetPassword }: {
  users: AdminUser[] | undefined;
  plans: AdminPlan[] | undefined;
  accounts: AdminAccount[] | undefined;
  /** The accounts read failed: the 交易账户 column says so instead of "—" (no grant). */
  accountsFailed?: boolean;
  rowStatus: Record<string, string>;
  onChangeUser: (user: AdminUser, change: { plan?: string; disabled?: boolean }) => void;
  /** 重置密码 (2026-10-21): site accounts only — the administrator changes their own in 设置, a TqKq login's lives with TqKq. */
  onResetPassword: (user: AdminUser, newPassword: string) => Promise<void>;
}) {
  const [resetting, setResetting] = useState<AdminUser>();
  const [query, setQuery] = useState("");
  const [tierFilter, setTierFilter] = useState("all");
  const [sort, setSort] = useState<{ key: UserSortKey; dir: 1 | -1 }>({ key: "username", dir: 1 });

  const tierOptions = useMemo(() =>
    (plans ?? []).filter(tier => tier.name !== "guest").map(tier => ({ value: tier.name, label: tier.label })),
    [plans]);

  const visibleUsers = useMemo(() => {
    if (!users) return undefined;
    const accessor: Record<UserSortKey, (user: AdminUser) => string | number | boolean> = {
      username: user => user.username.toLowerCase(),
      plan: user => user.planLabel,
      status: user => user.disabled,
      tokensToday: user => user.tokensToday,
      voiceToday: user => user.voiceSecondsToday,
      cost: user => user.estimatedCostToday ?? 0,
      lastSeenAt: user => user.lastSeenAt ?? "",
      createdAt: user => user.createdAt,
    };
    const sorted = [...users].sort((a, b) => {
      const left = accessor[sort.key](a), right = accessor[sort.key](b);
      if (typeof left === "boolean" || typeof right === "boolean") {
        return (Number(left) - Number(right)) * sort.dir;
      }
      return (left < right ? -1 : left > right ? 1 : 0) * sort.dir;
    });
    const text = query.trim().toLowerCase();
    return sorted.filter(user =>
      (!text || user.username.toLowerCase().includes(text))
      && (tierFilter === "all" || user.plan === tierFilter));
  }, [users, query, tierFilter, sort]);

  // The 交易账户 column: grants inverted into username → display names.
  const accountsByUser = useMemo(() => {
    const byUser = new Map<string, string[]>();
    for (const account of accounts ?? []) {
      for (const grant of account.grants) {
        byUser.set(grant.user, [...(byUser.get(grant.user) ?? []), account.displayName]);
      }
    }
    return byUser;
  }, [accounts]);

  // 今日 token against the member's tier quota (null quota = 不限, the owner has none).
  const quotaOf = (user: AdminUser) => user.role === "owner" ? null : plans?.find(tier => tier.name === user.plan)?.dailyTokens ?? null;

  const sortButton = (key: UserSortKey, label: string, numeric = false) => (
    <button type="button" className={numeric ? "fdw-admin-sort num" : "fdw-admin-sort"} aria-label={`按${label}排序`}
      onClick={() => setSort(current => current.key === key
        ? { key, dir: current.dir === 1 ? -1 : 1 }
        : { key, dir: 1 })}>
      {label}{sort.key === key ? (sort.dir === 1 ? " ↑" : " ↓") : ""}
    </button>
  );

  return <>
    <SectionHeader title="用户" helpKey="users" help="档位与状态改动即时保存；停用立即结束该用户的会话，其后登录被拒。重置密码后该用户所有设备退出登录，需用新密码重新登录。" />
    <div className="fdw-admin-section-body">
      {visibleUsers === undefined ? <p className="fdw-admin-empty">读取中</p> : users?.length === 0 ? (
        <p className="fdw-admin-empty">还没有云用户</p>
      ) : <>
        {/* The filters stay while nothing matches, so the search can be cleared. */}
        <div className="fdw-admin-toolbar">
          <TextInput label="按用户名搜索" value={query} placeholder="搜索用户名" onChange={setQuery} />
          <Select label="档位筛选" value={tierFilter}
            options={[{ value: "all", label: "全部档位" }, ...tierOptions]}
            onChange={setTierFilter} />
        </div>
        {visibleUsers.length === 0 ? <div className="fdw-admin-empty fdw-admin-empty-action">
          <span>没有匹配的用户</span><Button onClick={() => { setQuery(""); setTierFilter("all"); }}>清空筛选</Button>
        </div> : <div className="fdw-admin-table-card">
        <table className="fdw-admin-table" aria-label="用户列表">
          <thead><tr>
            <th>{sortButton("username", "用户名")}</th>
            <th>{sortButton("plan", "档位")}</th>
            <th>{sortButton("status", "状态")}</th>
            <th>交易账户</th>
            <th className="num">{sortButton("tokensToday", "今日 token", true)}</th>
            <th className="num">{sortButton("voiceToday", "今日语音", true)}</th>
            <th className="num">{sortButton("cost", "估算成本", true)}</th>
            <th>{sortButton("lastSeenAt", "最后在线")}</th>
            <th>{sortButton("createdAt", "来源")}</th>
            <th>密码</th>
          </tr></thead>
          <tbody>
            {visibleUsers.map(user => <tr key={user.username}>
              <td>
                <span className="fdw-admin-user">
                  <span className="fdw-admin-avatar" aria-hidden="true">{user.username.slice(0, 1).toUpperCase()}</span>
                  <span><strong>{user.username}</strong>{rowStatus[user.username] ? <span role="status" className="fdw-admin-cell-status">{rowStatus[user.username]}</span> : null}</span>
                </span>
              </td>
              <td>{user.role === "owner" ? <strong>管理员</strong> : <>
                <Select label={`${user.username} 档位`} value={user.plan} disabled={rowStatus[user.username] === "保存中…"}
                  options={tierOptions.some(option => option.value === user.plan)
                    ? tierOptions
                    : [{ value: user.plan, label: `${user.plan}（已删除）` }, ...tierOptions]}
                  onChange={value => { if (value !== user.plan) onChangeUser(user, { plan: value }); }} />
              </>}</td>
              <td>{user.role === "owner" ? <strong>主账号</strong> : <>
                <Select label={`${user.username} 状态`} value={user.disabled ? "disabled" : "active"}
                  options={[{ value: "active", label: "正常" }, { value: "disabled", label: "停用" }]}
                  onChange={value => { const disabled = value === "disabled"; if (disabled !== user.disabled) onChangeUser(user, { disabled }); }} />
              </>}</td>
              <td className="fdw-admin-wrap">{accounts === undefined ? (accountsFailed ? "未读到" : "读取中")
                : accountsByUser.get(user.username)?.join("、") || "—"}</td>
              <td className="num">{(() => {
                const quota = quotaOf(user);
                const percent = quota ? Math.round(user.tokensToday / quota * 100) : null;
                return <span className="fdw-admin-usage">
                  <span>{fmtTokens(user.tokensToday)}</span>
                  {percent == null ? <small>不限额度</small> : <>
                    <Meter percent={percent} level={loadLevel(percent)} />
                    <small>{percent}% 日额度</small>
                  </>}
                </span>;
              })()}</td>
              <td className="num">{Math.round(user.voiceSecondsToday / 60)} 分钟</td>
              <td className="num">{user.estimatedCostToday == null ? "—" : `${user.estimatedCostToday.toFixed(2)} 元`}</td>
              <td>{!user.lastSeenAt ? "从未在线" : fmtDateTime(user.lastSeenAt)}</td>
              <td title={`注册于 ${user.createdAt.slice(0, 10)}`}>{sourceLabel(user.invitedBy)}</td>
              <td>{user.role === "owner" ? "在设置里修改" : user.invitedBy === "tqkq-login" ? "在快期修改"
                : <Button testId={`admin:reset-password:${user.username}`} onClick={() => setResetting(user)}>重置密码</Button>}</td>
            </tr>)}
          </tbody>
        </table>
        </div>}
      </>}
    </div>
    <PasswordDialog surface="admin-reset-password" closeOnly open={resetting !== undefined}
      onOpenChange={open => { if (!open) setResetting(undefined); }} title="重置用户密码" subject={resetting?.username ?? ""}
      needCurrent={false} note="重置后该用户所有设备退出登录，需用新密码重新登录；请把新密码当面或私下告诉对方。" submitLabel="重置密码"
      onSubmit={async (_current, next) => { if (resetting) await onResetPassword(resetting, next); }} />
  </>;
}
