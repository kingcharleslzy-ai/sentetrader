import { useEffect, useMemo, useState } from "react";
import {
  getAdminMarketSources, getAdminServers,
  type AdminInviteCode, type AdminMarketSourcesSnapshot, type AdminPlan, type AdminServersSnapshot, type AdminUser,
} from "../../../api";
import { hostFigures } from "./ServersSection";
import { sourceName } from "./MarketSourcesSection";
import { AdminTiles, Button, Meter, SectionHeader, Tag, fmtTime, fmtTokens, loadLevel } from "./shared";

const WEEK_MS = 7 * 86_400_000;

/** The 概览 section: today's figures in tiles, the per-tier population, the invite-code
 *  posture, and a 系统 block (each host's CPU/内存 bars, the market relay's load). Tier
 *  rows and the cards' links jump into their sections; the 系统 block's two halves read
 *  their own interfaces and fail on their own. */
export function OverviewSection({ baseUrl, users, plans, invites, onOpenTier, onManageInvites, onOpenServers, onOpenMarketSources }: {
  baseUrl: string;
  users: AdminUser[] | undefined;
  plans: AdminPlan[] | undefined;
  invites: AdminInviteCode[] | undefined;
  onOpenTier: (name: string) => void;
  onManageInvites: () => void;
  onOpenServers: () => void;
  onOpenMarketSources: () => void;
}) {
  const overview = useMemo(() => {
    if (!users || !plans || !invites) return undefined;
    const today = new Date().toDateString();
    const weekAgo = Date.now() - WEEK_MS;
    return {
      members: users.length,
      disabledUsers: users.filter(user => user.disabled).length,
      activeToday: users.filter(user =>
        user.tokensToday > 0 || user.voiceSecondsToday > 0
        || (user.lastSeenAt != null && new Date(user.lastSeenAt).toDateString() === today)).length,
      tokensToday: users.reduce((total, user) => total + user.tokensToday, 0),
      voiceMinutesToday: Math.round(users.reduce((total, user) => total + user.voiceSecondsToday, 0) / 60),
      // A model without a listed price has no estimate (null): it is left out of the sum and
      // counted, never added as 0 — unless the member used nothing today.
      costToday: users.reduce((total, user) => total + (user.estimatedCostToday ?? 0), 0),
      costKnown: users.some(user => user.estimatedCostToday != null),
      costMissing: users.filter(user => user.estimatedCostToday == null
        && (user.tokensToday > 0 || user.voiceSecondsToday > 0)).length,
      availableInvites: invites.filter(item => item.status === "active").length,
      unlimitedInvites: invites.filter(item => item.status === "active" && item.uses === "unlimited").length,
      usedThisWeek: invites.reduce((total, item) =>
        total + item.usedBy.filter(use => use.usedAt != null && Date.parse(use.usedAt) >= weekAgo).length, 0),
      expiredInvites: invites.filter(item => item.status === "expired").length,
    };
  }, [users, plans, invites]);

  const header = <SectionHeader title="概览" hint="数字每天北京时间 8:00 重置；成员不含主账号；估算成本供排程参考，不是账单。" />;
  if (!overview || !plans) return <>{header}<p className="fdw-admin-empty">读取中</p></>;
  const largestTier = Math.max(1, ...plans.map(plan => plan.userCount));
  return <>
    {header}
    <div className="fdw-admin-section-body">
      <AdminTiles label="今日" items={[
        { label: "成员", value: overview.members, note: "另有主账号" },
        { label: "今日活跃", value: overview.activeToday },
        { label: "已停用成员", value: overview.disabledUsers },
        { label: "今日 token", value: fmtTokens(overview.tokensToday) },
        { label: "今日语音（分钟）", value: fmtTokens(overview.voiceMinutesToday) },
        { label: "今日估算成本", value: overview.costKnown ? `${overview.costToday.toFixed(2)} 元` : "—",
          note: overview.costMissing ? `另有 ${overview.costMissing} 人的模型没有价格，未计入` : undefined },
      ]} />
      <div className="fdw-admin-overview-grid">
        <div className="fdw-admin-overview-column">
          <section className="fdw-admin-panel" aria-label="档位人数">
            <header className="fdw-admin-panel-head"><h3>档位人数</h3><span className="fdw-admin-panel-hint">点一行去档位分区编辑</span></header>
            <div className="fdw-admin-tier-rows">
              {plans.map(plan => (
                <button type="button" key={plan.name} className="fdw-admin-tier-row" onClick={() => onOpenTier(plan.name)}>
                  <span className="fdw-admin-tier-name">
                    {plan.label}
                    {plan.isDefault ? <Tag>默认</Tag> : null}
                    {plan.name === "guest" ? <Tag>未登录访客</Tag> : null}
                  </span>
                  <Meter percent={plan.userCount / largestTier * 100} />
                  <span className="fdw-admin-tier-count">{plan.userCount} 人</span>
                </button>
              ))}
              <div className="fdw-admin-tier-row fdw-admin-tier-row-static">
                <span className="fdw-admin-tier-name">主账号</span>
                <span />
                <span className="fdw-admin-tier-count">1 人</span>
              </div>
            </div>
          </section>
          <section className="fdw-admin-panel" aria-label="邀请码">
            <header className="fdw-admin-panel-head">
              <h3>邀请码</h3><span className="fdw-admin-panel-hint">可用＝未用完、未过期、未停用</span>
              <div className="fdw-admin-panel-actions"><Button onClick={onManageInvites}>管理邀请码</Button></div>
            </header>
            <dl className="fdw-admin-figures">
              <div><dt>可用</dt><dd>{overview.availableInvites}</dd><dd className="fdw-admin-figure-note">不限次数 {overview.unlimitedInvites} 个</dd></div>
              <div><dt>近 7 天使用</dt><dd>{overview.usedThisWeek}</dd></div>
              <div data-muted=""><dt>已过期</dt><dd>{overview.expiredInvites}</dd></div>
            </dl>
          </section>
        </div>
        <section className="fdw-admin-panel fdw-admin-overview-system" aria-label="系统">
          <SystemHosts baseUrl={baseUrl} onOpen={onOpenServers} />
          <SystemMarket baseUrl={baseUrl} onOpen={onOpenMarketSources} />
        </section>
      </div>
    </div>
  </>;
}

/** 系统 · hosts: one line per host — online dot, name, CPU and 内存 bars. */
function SystemHosts({ baseUrl, onOpen }: { baseUrl: string; onOpen: () => void }) {
  const [snapshot, setSnapshot] = useState<AdminServersSnapshot>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    getAdminServers(baseUrl, "24h")
      .then(result => { if (!cancelled) setSnapshot(result); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [baseUrl]);
  const ready = snapshot?.status === "ok" ? snapshot : undefined;
  const hosts = ready && Array.isArray(ready.hosts) ? ready.hosts : undefined;
  return <div className="fdw-admin-system-part" aria-label="服务器状态" role="group">
    <header className="fdw-admin-panel-head">
      <h3>系统</h3>
      <button type="button" className="fdw-admin-link" onClick={onOpen}>服务器 →</button>
    </header>
    {/* The console was unreachable: the last good copy, said to be old (服务器 does the same). */}
    {hosts?.length && ready?.stale ? <p className="fdw-admin-inline-note" role="status">
      <strong>数据已过期（上次 {fmtTime(ready.fetchedAt)}）</strong>
    </p> : null}
    {failed ? <p className="fdw-admin-inline-note">服务器状态读取失败</p>
      : snapshot?.status === "unconfigured" ? <p className="fdw-admin-inline-note">运维台令牌未配置</p>
        : !snapshot ? <p className="fdw-admin-inline-note">读取中</p>
          : !hosts?.length ? <p className="fdw-admin-inline-note">运维台没有返回主机</p>
            : <ul className="fdw-admin-system-hosts">
              {hosts.map(host => {
                const { cpu, mem } = hostFigures(host);
                const tone = host.reachability === "online" ? "ok" : !host.reachability || host.reachability === "unknown" ? "idle" : "danger";
                return <li key={host.id}>
                  <span className="fdw-admin-dot" data-tone={tone} />
                  <span className="fdw-admin-system-name">{host.displayName}</span>
                  <span className="fdw-admin-system-bar"><small>CPU {cpu == null ? "—" : `${cpu}%`}</small><Meter percent={cpu ?? 0} level={loadLevel(cpu)} /></span>
                  <span className="fdw-admin-system-bar"><small>内存 {mem == null ? "—" : `${mem}%`}</small><Meter percent={mem ?? 0} level={loadLevel(mem)} /></span>
                </li>;
              })}
            </ul>}
  </div>;
}

/** 系统 · 行情源: channels used of the relay's capacity, one bar segment per source. */
function SystemMarket({ baseUrl, onOpen }: { baseUrl: string; onOpen: () => void }) {
  const [snapshot, setSnapshot] = useState<AdminMarketSourcesSnapshot>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    getAdminMarketSources(baseUrl)
      .then(result => { if (!cancelled) setSnapshot(result); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [baseUrl]);
  const items = Array.isArray(snapshot?.items) ? snapshot.items : [];
  const used = items.reduce((total, item) => total + item.channels, 0);
  const capacity = items.reduce((total, item) => total + item.channelLimit, 0);
  return <div className="fdw-admin-system-part fdw-admin-system-market" aria-label="行情源占用" role="group">
    <header className="fdw-admin-panel-head">
      <h3>行情源</h3>
      <button type="button" className="fdw-admin-link" onClick={onOpen}>{snapshot && capacity ? `${used} / ${capacity} 组 →` : "行情源 →"}</button>
    </header>
    {failed ? <p className="fdw-admin-inline-note">行情源读取失败</p>
      : !snapshot ? <p className="fdw-admin-inline-note">读取中</p>
        : items.length === 0 ? <p className="fdw-admin-inline-note">还没有行情源</p>
          : <>
            <span className="fdw-admin-stack" role="img" aria-label={`K 线通道已用 ${used} / ${capacity}`}>
              {items.map((item, index) => item.channels > 0
                ? <i key={item.accountId} data-rank={Math.min(index, 2)} style={{ flexGrow: item.channels }} /> : null)}
              {capacity > used ? <i data-rest="" style={{ flexGrow: capacity - used }} /> : null}
            </span>
            <p className="fdw-admin-inline-note">{items.map(item => `${sourceName(item)} ${item.channels}`).join(" · ")}</p>
          </>}
  </div>;
}
