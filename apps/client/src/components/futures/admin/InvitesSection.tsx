import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { AdminInviteCode, AdminPlan } from "../../../api";
import { SectionCard } from "../../Blocks";
import { Button, Segmented, Select, TextInput } from "../../settings/controls";
import { INVITE_DAY_OPTIONS, SectionHeader, Tag, copyText, fmtDate, fmtDateTime } from "./shared";

type InviteUses = "once" | "unlimited";

/** The 邀请码 section: a form (custom code · uses · tier · validity) above the
 *  list, the list itself with per-code copy and disable, and an availability
 *  filter. Old codes show 旧码不可见 — only their hash was ever stored. */
export function InvitesSection({ invites, plans, onCreateInvite, onDisableInvite }: {
  invites: AdminInviteCode[] | undefined;
  plans: AdminPlan[] | undefined;
  onCreateInvite: (invite: { daysValid: number; plan?: string; code?: string; uses: InviteUses; neverExpires: boolean }) => Promise<string>;
  onDisableInvite: (id: string) => Promise<void>;
}) {
  const [formOpen, setFormOpen] = useState(false);
  const [codeDraft, setCodeDraft] = useState("");
  const [uses, setUses] = useState<InviteUses>("once");
  const [plan, setPlan] = useState("");
  const [validity, setValidity] = useState("14");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [generated, setGenerated] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [filter, setFilter] = useState<"available" | "all">("available");
  const [highlight, setHighlight] = useState<string | null>(null);
  const [expandedUses, setExpandedUses] = useState<string | null>(null);
  const [disabling, setDisabling] = useState<string | null>(null);
  // A failed 停用 says so in its row; stopping is idempotent, so 再点一次 is safe.
  const [disableErrors, setDisableErrors] = useState<Record<string, string>>({});
  const highlightTimer = useRef<number | undefined>(undefined);

  const tierOptions = useMemo(() =>
    (plans ?? []).filter(tier => tier.name !== "guest").map(tier => ({ value: tier.name, label: tier.label })),
    [plans]);
  const defaultTier = plans?.find(tier => tier.isDefault && tier.name !== "guest")?.name;
  useEffect(() => { if (defaultTier && !plan) setPlan(defaultTier); }, [defaultTier, plan]);
  useEffect(() => () => window.clearTimeout(highlightTimer.current), []);

  const tierLabel = (name: string | null) => plans?.find(tier => tier.name === name)?.label ?? (name ? `${name}（已删除）` : "默认档");
  const statusLabel: Record<AdminInviteCode["status"], string> = {
    active: "可用", used_up: "已用完", expired: "已过期", disabled: "已停用",
  };

  const visibleInvites = useMemo(() =>
    invites === undefined ? undefined
      : filter === "all" ? invites : invites.filter(item => item.status === "active"),
    [invites, filter]);

  const generate = async () => {
    setBusy(true);
    setError("");
    try {
      const code = await onCreateInvite({
        daysValid: validity === "never" ? 14 : Number(validity),
        plan: plan || undefined,
        code: codeDraft.trim() || undefined,
        uses, neverExpires: validity === "never",
      });
      setFormOpen(false);
      setGenerated(code);
      setCopied(false);
      setCodeDraft("");
      // Back to the form defaults for the next one (the effect re-applies the default tier).
      setUses("once");
      setValidity("14");
      setPlan("");
      setHighlight(code);
      window.clearTimeout(highlightTimer.current);
      highlightTimer.current = window.setTimeout(() => setHighlight(null), 2000);
    } catch (failure) {
      const status = (failure as { status?: number }).status;
      setError(status === 422 ? "邀请码格式不对：4–32 位字母、数字、- _"
        : status === 409 ? "这个邀请码已经存在"
          : "生成失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const copyGenerated = async () => {
    if (!generated) return;
    if (await copyText(generated)) {
      setCopied(true);
      return;
    }
    // Clipboard refused (insecure context, no user gesture): selecting the code
    // lets ⌘C copy it.
    const selection = window.getSelection();
    const node = document.querySelector(`[data-invite-code="${CSS.escape(generated)}"]`);
    if (selection && node) {
      selection.removeAllRanges();
      const range = document.createRange();
      range.selectNodeContents(node);
      selection.addRange(range);
    }
  };

  const copyOne = async (code: string) => { void (await copyText(code)); };

  const disable = async (row: AdminInviteCode) => {
    if (!window.confirm(`停用邀请码 ${row.code ?? ""}？停用后它不能再被使用。`)) return;
    setDisabling(row.id);
    setDisableErrors(current => ({ ...current, [row.id]: "" }));
    try {
      await onDisableInvite(row.id);
    } catch {
      setDisableErrors(current => ({ ...current, [row.id]: "停用没有确认成功，可再点一次" }));
    } finally { setDisabling(null); }
  };

  return <>
    <SectionHeader title="邀请码" helpKey="invites" help="新账号首次登录时在登录框填写；升级前的旧码只存了摘要，码文不可见。"
      action={formOpen ? undefined : <Button primary onClick={() => setFormOpen(true)}>生成邀请码</Button>} />
    <div className="fdw-admin-section-body">
      {formOpen ? (
        <SectionCard title="生成邀请码" note="生成后立即显示，可复制" className="fdw-admin-invite-form">
          <div className="section-card-body">
          <div className="fdw-admin-form-rows fdw-admin-form-inline">
            <label className="fdw-admin-form-row">
              <span className="fdw-admin-form-label">邀请码
                <small className="fdw-admin-form-note">4–32 位字母、数字、- _，区分大小写</small>
              </span>
              <span className="fdw-admin-form-field">
                <TextInput label="邀请码" value={codeDraft} placeholder="留空自动生成" maxLength={32}
                  disabled={busy} onChange={setCodeDraft} />
              </span>
            </label>
            <label className="fdw-admin-form-row">
              <span className="fdw-admin-form-label">可用次数</span>
              <Segmented label="可用次数" value={uses} disabled={busy}
                options={[{ value: "once", label: "一次" }, { value: "unlimited", label: "不限次数" }]}
                onChange={value => setUses(value)} />
            </label>
            <label className="fdw-admin-form-row">
              <span className="fdw-admin-form-label">档位</span>
              <Select label="档位" value={plan} disabled={busy}
                options={[{ value: "", label: "默认档" }, ...tierOptions]} onChange={setPlan} />
            </label>
            <label className="fdw-admin-form-row">
              <span className="fdw-admin-form-label">有效期</span>
              <Select label="有效期" value={validity} disabled={busy}
                options={[...INVITE_DAY_OPTIONS, { value: "never", label: "长期有效" }]}
                onChange={setValidity} />
            </label>
          </div>
          {error ? <p className="fdw-admin-form-error" role="alert">{error}</p> : null}
          <div className="fdw-admin-form-actions">
            <Button disabled={busy} onClick={() => { setFormOpen(false); setError(""); }}>取消</Button>
            <Button primary disabled={busy} onClick={() => void generate()} status={busy ? "生成中" : undefined}>生成</Button>
          </div>
          </div>
        </SectionCard>
      ) : null}
      {generated ? (
        <p className="fdw-admin-invite-result" role="status">
          <span>刚生成</span>
          <strong data-invite-code={generated}>{generated}</strong>
          <Button onClick={() => void copyGenerated()}>{copied ? "已复制" : "复制"}</Button>
        </p>
      ) : null}
      {invites === undefined ? <p className="fdw-admin-empty">读取中</p> : invites.length === 0 ? (
        <p className="fdw-admin-empty">还没有邀请码</p>
      ) : <>
        <div className="fdw-admin-section-toolbar">
          <Segmented label="邀请码筛选" value={filter}
            options={[{ value: "available", label: "可用" }, { value: "all", label: "全部" }]}
            onChange={value => setFilter(value)} />
        </div>
        <div className="fdw-admin-table-card">
        <table className="fdw-admin-table fdw-admin-invites" aria-label="邀请码列表">
          <thead><tr>
            <th>邀请码</th><th>次数</th><th>档位</th><th>已使用</th><th>有效至</th><th>状态</th><th className="fdw-admin-col-created">创建</th><th className="fdw-admin-col-actions">操作</th>
          </tr></thead>
          <tbody>
            {(visibleInvites ?? []).map(item => <Fragment key={item.id}>
              <tr data-highlight={highlight === item.code ? "" : undefined}>
                <td>
                  {item.code == null ? <span className="fdw-admin-old-code" title="旧码只在生成时显示过，数据库只保留了摘要">旧码不可见</span> : <>
                    <span className="fdw-admin-code" data-invite-code={item.code}>{item.code}</span>
                    <button type="button" className="fdw-admin-copy" aria-label={`复制 ${item.code}`}
                      title="复制" onClick={() => void copyOne(item.code!)}>⧉</button>
                  </>}
                </td>
                <td>{item.uses === "unlimited" ? "不限" : "一次"}</td>
                <td>{item.plan ? tierLabel(item.plan) : "默认档"}</td>
                <td>{item.uses === "unlimited"
                  ? item.useCount > 0
                    ? <button type="button" className="fdw-admin-uses-toggle" aria-expanded={expandedUses === item.id}
                      onClick={() => setExpandedUses(current => current === item.id ? null : item.id)}>{item.useCount} 人</button>
                    : "—"
                  : item.usedBy[0]?.username ?? "—"}</td>
                <td>{item.expiresAt == null ? "长期" : fmtDate(item.expiresAt)}</td>
                <td><Tag tone={item.status === "active" ? undefined : item.status === "disabled" ? "danger" : undefined}>{statusLabel[item.status]}</Tag></td>
                <td className="fdw-admin-col-created" title={`创建于 ${fmtDateTime(item.createdAt)}，创建人 ${item.createdBy}`}>{fmtDate(item.createdAt)}</td>
                <td className="fdw-admin-col-actions">{item.status === "active"
                  ? <Button disabled={disabling === item.id} onClick={() => void disable(item)}>{disabling === item.id ? "停用中" : "停用"}</Button>
                  : null}
                  {disableErrors[item.id] && item.status === "active"
                    ? <span role="alert" className="fdw-admin-cell-status fdw-admin-cell-error">{disableErrors[item.id]}</span> : null}</td>
              </tr>
              {expandedUses === item.id ? (
                <tr className="fdw-admin-uses-detail">
                  <td colSpan={8}>
                    {item.usedBy.length === 0 ? "还没有人使用"
                      : item.usedBy.map(use => <span key={`${use.username}-${use.usedAt}`} className="fdw-admin-use-item">
                        {use.username} · {fmtDateTime(use.usedAt)}
                      </span>)}
                  </td>
                </tr>
              ) : null}
            </Fragment>)}
          </tbody>
        </table>
        </div>
        {visibleInvites?.length === 0 ? <p className="fdw-admin-empty">没有可用的邀请码</p> : null}
      </>}
    </div>
  </>;
}
