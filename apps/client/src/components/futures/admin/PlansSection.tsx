import { useEffect, useMemo, useState } from "react";
import type { AdminPlan, AdminPlansSnapshot } from "../../../api";
import { updateAdminPlan } from "../../../api";
import { SectionCard } from "../../Blocks";
import { Button, Select, Stepper, Switch, TextInput } from "../../settings/controls";
import { DangerButton, GUEST_VOICE_OPTIONS, SectionHeader, Tag, TOKEN_OPTIONS, VOICE_OPTIONS, fmtTokens, tokenPreset, voicePreset } from "./shared";

export type PlanChange = Parameters<typeof updateAdminPlan>[2];

/** The 档位 section, redesigned (owner-approved sketch): tiers on the left,
 * the selected tier's editor on the right. 新建档位 lives in the section
 * header and opens an inline card on top of the left column. */
export function PlansSection({ plans, selected, onSelect, busyTier, status, onSavePlan, onRemovePlan, onAddPlan, onGoSkills }: {
  plans: AdminPlansSnapshot | undefined;
  selected: string;
  onSelect: (name: string) => void;
  busyTier: string | undefined;
  status: string;
  onSavePlan: (plan: AdminPlan, change: PlanChange, message?: string) => void;
  onRemovePlan: (plan: AdminPlan) => void;
  onAddPlan: (label: string, copyFrom: string) => void;
  onGoSkills: () => void;
}) {
  const [creating, setCreating] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [newCopyFrom, setNewCopyFrom] = useState("");
  const [labelDraft, setLabelDraft] = useState<Record<string, string>>({});
  // "自定义…" selected for a tier whose stored value is still 不限 or a preset: the
  // number box shows until the first typed value lands (then the stored value is custom).
  const [tokenCustom, setTokenCustom] = useState<Record<string, boolean>>({});
  const [voiceCustom, setVoiceCustom] = useState<Record<string, boolean>>({});

  const tierOptions = useMemo(() =>
    (plans?.items ?? []).filter(tier => tier.name !== "guest").map(tier => ({ value: tier.name, label: tier.label })),
    [plans]);

  useEffect(() => {
    // The copy source defaults to the default tier once the list arrives.
    if (plans && !newCopyFrom) setNewCopyFrom(plans.items.find(tier => tier.isDefault)?.name ?? plans.items[0]?.name ?? "");
  }, [plans, newCopyFrom]);

  if (!plans) return <>
    <SectionHeader title="档位" helpKey="plans" help="档位约束成员；主账号不受档位限制。" />
    <div className="fdw-admin-section-body"><p className="fdw-admin-empty">读取中</p></div>
  </>;

  const plan = plans.items.find(tier => tier.name === selected) ?? plans.items[0];
  const addTier = () => {
    onAddPlan(newLabel.trim(), newCopyFrom);
    setNewLabel("");
    setCreating(false);
  };

  return <>
    <SectionHeader title="档位" helpKey="plans" help="档位约束成员；主账号不受档位限制；改动即时保存。"
      action={<Button primary onClick={() => setCreating(true)}>新建档位</Button>} />
    <div className="fdw-admin-section-body">
      <div className="fdw-admin-plans-layout">
        <div className="fdw-admin-plan-list" role="group" aria-label="档位列表">
          {creating ? <div className="fdw-admin-plan-create">
            <TextInput label="新档位显示名称" value={newLabel} maxLength={40} placeholder="例如 内测档"
              onChange={setNewLabel} />
            <Select label="新档位复制来源" value={newCopyFrom}
              options={tierOptions.length ? tierOptions : [{ value: "", label: "暂无档位" }]}
              onChange={setNewCopyFrom} />
            <div className="fdw-admin-plan-create-actions">
              <Button primary disabled={!newLabel.trim() || !newCopyFrom} onClick={addTier}>创建</Button>
              <Button onClick={() => { setCreating(false); setNewLabel(""); }}>取消</Button>
            </div>
          </div> : null}
          {plans.items.map(tier => (
            <button type="button" key={tier.name}
              className={`fdw-admin-plan-card${tier.name === plan?.name ? " selected" : ""}`}
              aria-pressed={tier.name === plan?.name}
              onClick={() => onSelect(tier.name)}>
              <span className="fdw-admin-plan-card-head">
                <strong>{tier.label}</strong>
                {tier.isDefault ? <Tag tone="primary">默认</Tag> : null}
                {tier.name === "guest" ? <Tag>未登录访客</Tag> : null}
                <span className="fdw-admin-plan-card-count">{tier.userCount} 人</span>
              </span>
              <span className="fdw-admin-plan-card-meta">{tier.name} · {tier.name === "guest" ? "不消耗 AI 额度"
                : `每日 ${tier.dailyTokens == null ? "不限" : `${fmtTokens(tier.dailyTokens)} token`}`}</span>
            </button>
          ))}
        </div>
        {plan ? <div className="fdw-admin-plan-editor">
          <span role="status" className="fdw-admin-plan-status">{status}</span>
          <SectionCard title="基本" note={plan.name === "guest" ? "游客是保留身份档，不能删除，也不能设为默认" : undefined}>
            <div className="section-card-body">
              <div className="fdw-admin-form-row">
                <span className="fdw-admin-form-label">名称</span>
                <span className="fdw-admin-form-field">
                  <TextInput label={`${plan.label} 显示名称`} value={labelDraft[plan.name] ?? plan.label} maxLength={40}
                    disabled={busyTier === plan.name}
                    onChange={value => setLabelDraft(current => ({ ...current, [plan.name]: value }))} />
                  <Button disabled={busyTier === plan.name || !(labelDraft[plan.name] ?? plan.label).trim()
                    || (labelDraft[plan.name] ?? plan.label).trim() === plan.label}
                    onClick={() => onSavePlan(plan, { label: (labelDraft[plan.name] ?? plan.label).trim() })}>保存名称</Button>
                </span>
              </div>
              <div className="fdw-admin-form-row">
                <span className="fdw-admin-form-label">默认档</span>
                <span className="fdw-admin-form-field">
                  {plan.isDefault ? <span className="fdw-admin-inline-note">当前默认</span> : (
                    <Button disabled={busyTier === plan.name || plan.name === "guest"}
                      onClick={() => onSavePlan(plan, { isDefault: true }, "已设为默认档")}>设为默认</Button>
                  )}
                </span>
              </div>
            </div>
          </SectionCard>
          <SectionCard title="可用模型" note="功能开关在「技能」分区设置"
            actions={<Button onClick={onGoSkills}>前往「技能」分区</Button>}>
            <div className="section-card-body">
              <div className="fdw-admin-form-rows fdw-admin-model-pills">
                {plans.modelCatalog.map(model => {
                  const effective = plan.models ?? plans.modelCatalog.map(entry => entry.slug);
                  return <div className="fdw-admin-form-row" key={model.slug}>
                    <span className="fdw-admin-form-label fdw-admin-form-label-nowrap">{model.label}</span>
                    <span className="fdw-admin-form-field">
                      <Switch label={`${plan.label} ${model.label}`} checked={plan.name !== "guest" && effective.includes(model.slug)}
                        disabled={busyTier === plan.name || plan.name === "guest"}
                        onChange={value => {
                          const slugs = plans.modelCatalog.map(entry => entry.slug);
                          const next = new Set(plan.models ?? slugs);
                          if (value) next.add(model.slug); else next.delete(model.slug);
                          onSavePlan(plan, { models: [...next].sort() });
                        }} />
                    </span>
                  </div>;
                })}
              </div>
            </div>
          </SectionCard>
          <SectionCard title="每日额度" note="每天北京时间 8:00 重置；不限＝没有上限">
            <div className="section-card-body">
              <div className="fdw-admin-form-rows fdw-admin-form-inline">
                <div className="fdw-admin-form-row">
                  <span className="fdw-admin-form-label">行情历史范围</span>
                  <span className="fdw-admin-form-field">
                    {plan.name === "guest" ? <Select label={`${plan.label} 行情交易日`} value={String(plan.marketHistoryTradingDays ?? 3)}
                      disabled={busyTier === plan.name} options={[1, 3, 5].map(days => ({ value: String(days), label: `最近 ${days} 个交易日` }))}
                      onChange={value => onSavePlan(plan, { marketHistoryTradingDays: Number(value) })} />
                      : <span className="fdw-admin-inline-note">沿用现有行情范围</span>}
                  </span>
                </div>
                <div className="fdw-admin-form-row">
                  <span className="fdw-admin-form-label">每日 token</span>
                  <span className="fdw-admin-form-field">
                    {plan.name === "guest" ? <span className="fdw-admin-inline-note">0（游客不消耗 AI 额度）</span> : <>
                      <Select label={`${plan.label} 每日 token`} value={tokenCustom[plan.name] ? "custom" : plan.dailyTokens == null ? "unlimited" : tokenPreset(plan.dailyTokens) ?? "custom"}
                        disabled={busyTier === plan.name} options={TOKEN_OPTIONS}
                        onChange={value => {
                          setTokenCustom(current => ({ ...current, [plan.name]: value === "custom" }));
                          if (value === "unlimited") onSavePlan(plan, { dailyTokens: null });
                          else if (value !== "custom") onSavePlan(plan, { dailyTokens: Number(value) });
                        }} />
                      {(tokenCustom[plan.name] ? true : (plan.dailyTokens != null && !tokenPreset(plan.dailyTokens))) ? <Stepper label={`${plan.label} 自定义每日 token`} value={plan.dailyTokens}
                        min={0} max={1_000_000_000} step={10_000} unit="token" placeholder="自定义" disabled={busyTier === plan.name}
                        onChange={value => onSavePlan(plan, { dailyTokens: value ?? 0 })} /> : null}
                    </>}
                  </span>
                </div>
                <div className="fdw-admin-form-row">
                  <span className="fdw-admin-form-label">每日语音</span>
                  <span className="fdw-admin-form-field">
                    {plan.name === "guest"
                      ? <Select label={`${plan.label} 每日语音`} value={String(plan.voiceSecondsPerDay ?? 60)} disabled={busyTier === plan.name}
                        options={GUEST_VOICE_OPTIONS.some(option => option.value === String(plan.voiceSecondsPerDay ?? 60))
                          ? GUEST_VOICE_OPTIONS
                          : [...GUEST_VOICE_OPTIONS, { value: String(plan.voiceSecondsPerDay), label: `${plan.voiceSecondsPerDay} 秒` }]}
                        onChange={value => onSavePlan(plan, { voiceSecondsPerDay: Number(value) })} />
                      : <>
                        <Select label={`${plan.label} 每日语音`} value={voiceCustom[plan.name] ? "custom" : plan.voiceSecondsPerDay == null ? "unlimited" : voicePreset(plan.voiceSecondsPerDay) ?? "custom"}
                          disabled={busyTier === plan.name} options={VOICE_OPTIONS}
                          onChange={value => {
                            setVoiceCustom(current => ({ ...current, [plan.name]: value === "custom" }));
                            if (value === "unlimited") onSavePlan(plan, { voiceSecondsPerDay: null });
                            else if (value !== "custom") onSavePlan(plan, { voiceSecondsPerDay: Number(value) });
                          }} />
                        {(voiceCustom[plan.name] ? true : (plan.voiceSecondsPerDay != null && plan.voiceSecondsPerDay !== 0 && !voicePreset(plan.voiceSecondsPerDay))) ? <Stepper label={`${plan.label} 自定义每日语音分钟`}
                          value={plan.voiceSecondsPerDay == null ? null : Math.round(plan.voiceSecondsPerDay / 60)}
                          min={0} max={166_666} step={5} unit="分钟" placeholder="自定义" disabled={busyTier === plan.name}
                          onChange={value => onSavePlan(plan, { voiceSecondsPerDay: (value ?? 0) * 60 })} /> : null}
                      </>}
                  </span>
                </div>
                <div className="fdw-admin-form-row">
                  <span className="fdw-admin-form-label">AI 简报品种上限</span>
                  <span className="fdw-admin-form-field">
                    {plan.name === "guest" ? <span className="fdw-admin-inline-note">游客不订阅 AI 简报</span> : <>
                      <Stepper label={`${plan.label} AI 简报品种上限`} value={plan.briefProducts ?? null} min={1} max={100} unit="个" placeholder="默认"
                        disabled={busyTier === plan.name} onChange={value => { if (value != null) onSavePlan(plan, { briefProducts: value }); }} />
                      <span className="fdw-admin-inline-note">管理员不限；调低后超出的只取前 N 个</span>
                    </>}
                  </span>
                </div>
              </div>
            </div>
          </SectionCard>
          <div className="fdw-admin-plan-danger">
            {plan.name === "guest" || plan.isDefault || plan.userCount > 0 ? (
              <DangerButton disabled title={plan.name === "guest" ? "游客是保留身份档，不能删除"
                : plan.isDefault ? "默认档不能删除，先把默认切到其他档位"
                  : `有 ${plan.userCount} 人在用，先把这些用户移到其他档位`}>删除档位</DangerButton>
            ) : <DangerButton disabled={busyTier === plan.name} title="删除后不可恢复，正在使用该档位名称的用户会落到默认档"
              onClick={() => onRemovePlan(plan)}>删除档位</DangerButton>}
          </div>
        </div> : null}
      </div>
    </div>
  </>;
}
