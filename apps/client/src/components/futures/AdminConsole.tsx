import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  createAdminPlan, createInviteCode, deleteAdminPlan, disableInviteCode, getAdminAccounts, getAdminPlans, getAdminSkills, getAdminUsers,
  listInviteCodes, resetAdminUserPassword, updateAdminPlan, updateAdminSkill, updateAdminUser,
  type AdminAccount, type AdminInviteCode, type AdminPlan, type AdminPlansSnapshot, type AdminServerRange, type AdminSkill, type AdminUser,
} from "../../api";
import { BrandMark } from "../BrandMark";
import type { PlanChange } from "./admin/PlansSection";
import { ADMIN_NAV_GROUPS, type AdminSection } from "./admin/shared";
import { AccountsSection } from "./admin/AccountsSection";
import { InvitesSection } from "./admin/InvitesSection";
import { MarketSourcesSection } from "./admin/MarketSourcesSection";
import { OverviewSection } from "./admin/Overview";
import { PlansSection } from "./admin/PlansSection";
import { ExperienceSection } from "./admin/ExperienceSection";
import { ServersSection } from "./admin/ServersSection";
import { SkillsSection } from "./admin/SkillsSection";
import { UsersSection } from "./admin/UsersSection";
import "./AdminConsole.css";

/** Which datasets a section reads; switching sections re-reads exactly these. */
const SECTION_DATA: Record<AdminSection, Array<"users" | "plans" | "skills" | "invites" | "accounts">> = {
  overview: ["users", "plans", "invites"],
  users: ["users", "plans", "accounts"],
  plans: ["plans"],
  skills: ["skills", "plans"],
  invites: ["invites", "plans"],
  accounts: ["accounts"],
  // 行情源 offers active TqKq accounts to add; its own list and 服务器 read in their sections.
  "market-sources": ["accounts"],
  servers: [],
  experience: [],
};

/**
 * The owner-only 后台管理 console (the standalone /trader/admin page, owner-approved
 * sketch 2026-10-04): a left sidebar — the Sente mark, the grouped navigation with live
 * counts (运营 概览/用户/档位/邀请码 · 能力 技能 · 交易 交易账户/行情源 · 基础设施
 * 服务器/用户体验), the signed-in account at the bottom — and one scrollable content column.
 * Every edit saves immediately and reports its result inline; the 交易账户 section is
 * the read-only isolation check. Under 900px the sidebar becomes a top bar.
 */
export function AdminConsole({ baseUrl, section, onSectionChange, footer }: {
  baseUrl: string | undefined;
  section: AdminSection;
  onSectionChange: (section: AdminSection) => void;
  /** The sidebar's bottom block: the signed-in owner, Runtime version and page links. */
  footer?: ReactNode;
}) {
  const [users, setUsers] = useState<AdminUser[]>();
  const [plans, setPlans] = useState<AdminPlansSnapshot>();
  const [skills, setSkills] = useState<AdminSkill[]>();
  const [invites, setInvites] = useState<AdminInviteCode[]>();
  const [accounts, setAccounts] = useState<AdminAccount[]>();
  // A failed accounts read is not "no accounts": the sections say it could not be read.
  const [accountsFailed, setAccountsFailed] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [planStatus, setPlanStatus] = useState("");
  const [busyTier, setBusyTier] = useState<string>();
  const [rowStatus, setRowStatus] = useState<Record<string, string>>({});
  const [selectedPlan, setSelectedPlan] = useState("");
  // The 技能 matrix's in-flight cells (`<skillId>@<tierName>` or `<skillId>@global`, each stays
  // disabled until its own write answers) and per-row status lines; disabling `admin-console`
  // asks for confirmation first.
  const [busySkillCells, setBusySkillCells] = useState<ReadonlySet<string>>(() => new Set());
  const markSkillCell = (cell: string, busy: boolean) => setBusySkillCells(current => {
    const next = new Set(current);
    if (busy) next.add(cell); else next.delete(cell);
    return next;
  });
  const [skillRowStatus, setSkillRowStatus] = useState<Record<string, string>>({});
  const [confirmDisableAdminConsole, setConfirmDisableAdminConsole] = useState(false);
  const [serverRange, setServerRange] = useState<AdminServerRange>("24h");
  const [experienceRange, setExperienceRange] = useState<AdminServerRange>("24h");

  const refresh = useCallback((datasets: Array<"users" | "plans" | "skills" | "invites" | "accounts">) => {
    if (!baseUrl) return;
    setLoadError("");
    if (datasets.includes("users")) getAdminUsers(baseUrl).then(result => setUsers(result.items)).catch(() => setLoadError("用户列表读取失败，请切换分区重试"));
    if (datasets.includes("plans")) getAdminPlans(baseUrl).then(setPlans).catch(() => setLoadError("档位列表读取失败，请切换分区重试"));
    if (datasets.includes("skills")) getAdminSkills(baseUrl).then(result => setSkills(result.items)).catch(() => setLoadError("技能列表读取失败，请切换分区重试"));
    if (datasets.includes("invites")) listInviteCodes(baseUrl).then(setInvites).catch(() => setLoadError("邀请码列表读取失败，请切换分区重试"));
    if (datasets.includes("accounts")) getAdminAccounts(baseUrl)
      .then(result => { setAccounts(result.items); setAccountsFailed(false); })
      .catch(() => { setAccountsFailed(true); setLoadError("交易账户读取失败，请切换分区重试"); });
  }, [baseUrl]);
  // Switching into a section re-reads what that section shows.
  useEffect(() => { refresh(SECTION_DATA[section]); }, [refresh, section]);

  const changeUser = async (user: AdminUser, change: { plan?: string; disabled?: boolean }) => {
    if (!baseUrl) return;
    setRowStatus(current => ({ ...current, [user.username]: "保存中…" }));
    try {
      const updated = await updateAdminUser(baseUrl, user.username, change);
      setUsers(current => current?.map(item => item.username === user.username ? updated : item) ?? [updated]);
      if (change.plan !== undefined) getAdminPlans(baseUrl).then(setPlans).catch(() => undefined);
      setRowStatus(current => ({ ...current, [user.username]: "已保存" }));
    } catch {
      setRowStatus(current => ({ ...current, [user.username]: "未生效，请重试" }));
    }
  };

  const savePlan = async (plan: AdminPlan, change: PlanChange, message = "已保存") => {
    if (!baseUrl) return;
    setBusyTier(plan.name);
    setPlanStatus("");
    try {
      const updated = await updateAdminPlan(baseUrl, plan.name, change);
      setPlans(current => current && ({ ...current, items: current.items.map(item => item.name === plan.name ? updated : item) }));
      if (change.isDefault || change.label !== undefined) getAdminPlans(baseUrl).then(setPlans).catch(() => undefined);
      setPlanStatus(message);
    } catch {
      setPlanStatus(`「${plan.label}」修改未生效，请重试`);
    } finally {
      setBusyTier(undefined);
    }
  };

  const removePlan = async (plan: AdminPlan) => {
    if (!baseUrl) return;
    setBusyTier(plan.name);
    setPlanStatus("");
    try {
      await deleteAdminPlan(baseUrl, plan.name);
      setPlans(current => current && ({ ...current, items: current.items.filter(item => item.name !== plan.name) }));
      setPlanStatus(`已删除「${plan.label}」`);
    } catch {
      setPlanStatus(`删除未生效：「${plan.label}」仍有用户或为默认档`);
    } finally {
      setBusyTier(undefined);
    }
  };

  const addPlan = async (label: string, copyFrom: string) => {
    if (!baseUrl || !label || !copyFrom) return;
    setBusyTier("new");
    setPlanStatus("");
    try {
      await createAdminPlan(baseUrl, label, copyFrom || undefined);
    } catch {
      setPlanStatus("新建档位未生效，请重试");
      setBusyTier(undefined);
      return;
    }
    // The tier exists now: a failed list refresh must not invite a retry that creates a second one.
    try {
      setPlans(await getAdminPlans(baseUrl));
      setPlanStatus("已新建档位");
    } catch {
      setPlanStatus("已新建档位；列表没刷新出来，切换分区再看");
    } finally {
      setBusyTier(undefined);
    }
  };

  const generateInvite = async (request: { daysValid: number; plan?: string; code?: string; uses: "once" | "unlimited"; neverExpires: boolean }) => {
    if (!baseUrl) throw new Error("runtime_offline");
    const result = await createInviteCode(baseUrl, request);
    listInviteCodes(baseUrl).then(setInvites).catch(() => undefined);
    return result.inviteCode;
  };

  const disableInvite = async (id: string) => {
    if (!baseUrl) return;
    const row = await disableInviteCode(baseUrl, id);
    setInvites(current => current?.map(item => item.id === id ? row : item) ?? current);
  };

  // -- 技能: the registry matrix. Every switch PUTs immediately; the optimistic
  // flip rolls back and puts its error line next to the row when the write fails.
  // Answers and rollbacks touch only their own cell: a quick second click on another
  // cell is still in flight and must not be overwritten by this one's whole-table reply.
  const withSkill = (ids: string[], id: string, open: boolean) => open ? [...new Set([...ids, id])] : ids.filter(item => item !== id);
  const setPlanSkill = (tierName: string, id: string, open: boolean) => setPlans(current => current && ({
    ...current,
    items: current.items.map(plan => plan.name === tierName ? { ...plan, skills: withSkill(plan.skills, id, open) } : plan),
  }));

  const saveSkillSwitch = async (skill: AdminSkill, enabled: boolean) => {
    if (!baseUrl) return;
    const previous = skill.enabled;
    const cell = `${skill.id}@global`;
    markSkillCell(cell, true);
    setSkillRowStatus(current => ({ ...current, [skill.id]: "保存中…" }));
    setSkills(current => current?.map(item => item.id === skill.id ? { ...item, enabled } : item) ?? current);
    try {
      const result = await updateAdminSkill(baseUrl, skill.id, enabled);
      const saved = result.items.find(item => item.id === skill.id);
      setSkills(current => current?.map(item => item.id === skill.id && saved ? saved : item) ?? result.items);
      setSkillRowStatus(current => ({ ...current, [skill.id]: "已保存" }));
    } catch {
      setSkills(current => current?.map(item => item.id === skill.id ? { ...item, enabled: previous } : item) ?? current);
      setSkillRowStatus(current => ({ ...current, [skill.id]: "保存未生效，已恢复" }));
    } finally {
      markSkillCell(cell, false);
    }
  };

  const saveSkillTier = async (skill: AdminSkill, tierName: string, open: boolean) => {
    if (!baseUrl) return;
    const wasOpen = skill.tiers[tierName] === true;
    const setCell = (value: boolean) => setSkills(current => current?.map(item => item.id === skill.id
      ? { ...item, tiers: { ...item.tiers, [tierName]: value } } : item) ?? current);
    const cell = `${skill.id}@${tierName}`;
    markSkillCell(cell, true);
    setSkillRowStatus(current => ({ ...current, [skill.id]: "保存中…" }));
    setCell(open);
    setPlanSkill(tierName, skill.id, open);
    try {
      const updated = await updateAdminPlan(baseUrl, tierName, { skills: { [skill.id]: open } });
      setPlanSkill(tierName, skill.id, updated.skills.includes(skill.id));
      setSkillRowStatus(current => ({ ...current, [skill.id]: "已保存" }));
    } catch {
      setCell(wasOpen);
      setPlanSkill(tierName, skill.id, wasOpen);
      setSkillRowStatus(current => ({ ...current, [skill.id]: "保存未生效，已恢复" }));
    } finally {
      markSkillCell(cell, false);
    }
  };

  /** 停用 admin-console 让 AI 不能再调用后台管理（主人在本页的操作不受影响），只有这一格先问一次。 */
  const flipSkillSwitch = (skill: AdminSkill, value: boolean) => {
    if (skill.id === "admin-console" && !value) { setConfirmDisableAdminConsole(true); return; }
    void saveSkillSwitch(skill, value);
  };

  const openTier = (name: string) => {
    setSelectedPlan(name);
    onSectionChange("plans");
  };

  const availableInvites = invites?.filter(item => item.status === "active").length;
  const navCounts: Partial<Record<AdminSection, number>> = {
    users: users?.length,
    plans: plans?.items.length,
    invites: availableInvites,
  };

  const offlineHint = <p className="fdw-admin-empty">连接后可管理</p>;

  return (
    <section className="fdw-admin-console" aria-label="后台管理">
      <div className="fdw-admin-layout">
        <aside className="fdw-admin-sidebar">
          <div className="fdw-admin-brand">
            <BrandMark size={30} />
            <span><strong>Sente</strong><small>管理后台</small></span>
          </div>
          <nav className="fdw-admin-nav" aria-label="后台管理分区导航">
            {ADMIN_NAV_GROUPS.map(group => (
              <div className="fdw-admin-nav-group" key={group.label} role="group" aria-label={group.label}>
                <span className="fdw-admin-nav-group-label" aria-hidden="true">{group.label}</span>
                {group.items.map(entry => (
                  <button type="button" key={entry.id} className={section === entry.id ? "active" : ""}
                    aria-current={section === entry.id ? "true" : undefined}
                    onClick={() => onSectionChange(entry.id)}>
                    <svg viewBox="0 0 24 24" aria-hidden="true">{entry.icon}</svg>
                    <span>{entry.label}</span>
                    {navCounts[entry.id] != null ? <span className="fdw-admin-nav-count">{navCounts[entry.id]}</span> : null}
                  </button>
                ))}
              </div>
            ))}
          </nav>
          {footer ? <div className="fdw-admin-sidebar-foot">{footer}</div> : null}
        </aside>
        <div className="fdw-admin-content">
          <div className="fdw-admin-content-inner">
            {!baseUrl ? offlineHint : section === "overview" ? (
              <OverviewSection baseUrl={baseUrl} users={users} plans={plans?.items} invites={invites}
                onOpenTier={openTier} onManageInvites={() => onSectionChange("invites")}
                onOpenServers={() => onSectionChange("servers")} onOpenMarketSources={() => onSectionChange("market-sources")} />
            ) : section === "users" ? (
              <UsersSection users={users} plans={plans?.items} accounts={accounts} accountsFailed={accountsFailed}
                rowStatus={rowStatus} onChangeUser={(user, change) => void changeUser(user, change)}
                onResetPassword={async (user, newPassword) => {
                  await resetAdminUserPassword(baseUrl, user.username, newPassword);
                  setRowStatus(current => ({ ...current, [user.username]: "密码已重置" }));
                }} />
            ) : section === "plans" ? (
              <PlansSection plans={plans} selected={selectedPlan} onSelect={setSelectedPlan}
                busyTier={busyTier} status={planStatus}
                onSavePlan={(plan, change, message) => void savePlan(plan, change, message)}
                onRemovePlan={plan => void removePlan(plan)}
                onAddPlan={(label, copyFrom) => void addPlan(label, copyFrom)}
                onGoSkills={() => onSectionChange("skills")} />
            ) : section === "skills" ? (
              <SkillsSection skills={skills} plans={plans?.items} busySkillCells={busySkillCells}
                skillRowStatus={skillRowStatus} onFlipSwitch={flipSkillSwitch}
                onTierSwitch={(item, tierName, open) => void saveSkillTier(item, tierName, open)} />
            ) : section === "invites" ? (
              <InvitesSection invites={invites} plans={plans?.items}
                onCreateInvite={generateInvite} onDisableInvite={disableInvite} />
            ) : section === "market-sources" ? (
              <MarketSourcesSection baseUrl={baseUrl} accounts={accounts} />
            ) : section === "servers" ? (
              <ServersSection baseUrl={baseUrl} range={serverRange} onRangeChange={setServerRange} />
            ) : section === "experience" ? (
              <ExperienceSection baseUrl={baseUrl} range={experienceRange} onRangeChange={setExperienceRange} />
            ) : (
              <AccountsSection accounts={accounts} failed={accountsFailed} />
            )}
            {loadError ? <p className="fdw-admin-load-error" role="alert">{loadError}</p> : null}
          </div>
        </div>
      </div>
      {confirmDisableAdminConsole ? (
        <div className="sheet-backdrop open" role="presentation"
          onMouseDown={event => event.target === event.currentTarget && setConfirmDisableAdminConsole(false)}>
          <section className="sheet" role="dialog" aria-modal="true" aria-labelledby="admin-skill-disable-title"
            onMouseDown={event => event.stopPropagation()}>
            <h3 id="admin-skill-disable-title">停用「后台管理」技能</h3>
            <p>停用后 AI（内置与外部）不能再调用后台管理接口；你在本页的操作不受影响，技能分区仍可重新启用。确定停用吗？</p>
            <div className="approval-actions">
              <button className="ghost-button" type="button" onClick={() => setConfirmDisableAdminConsole(false)}>暂不停用</button>
              <button className="primary-button accent" type="button" disabled={busySkillCells.has("admin-console@global")}
                onClick={() => {
                  setConfirmDisableAdminConsole(false);
                  const target = skills?.find(item => item.id === "admin-console");
                  if (target) void saveSkillSwitch(target, false);
                }}>
                {busySkillCells.has("admin-console@global") ? "正在停用…" : "确认停用"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
