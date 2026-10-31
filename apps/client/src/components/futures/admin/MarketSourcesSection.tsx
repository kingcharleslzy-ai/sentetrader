import { useEffect, useMemo, useRef, useState } from "react";
import {
  addAdminMarketSourceAccount, getAdminMarketSources, removeAdminMarketSourceAccount, updateAdminMarketSources,
  type AdminAccount, type AdminMarketSource, type AdminMarketSourcesSnapshot,
} from "../../../api";
import { HelpTip } from "../../Blocks";
import { TextInput } from "../../settings/controls";
import { AdminTiles, Button, Meter, SectionHeader, fmtDateTime } from "./shared";

const SOURCE_KINDS = new Set(["tqkq-pool", "tqkq-sidecar"]);
const KIND_LABEL: Record<string, string> = { "tqkq-sidecar": "常驻", "tqkq-pool": "接入池" };
const HEALTH_LABEL: Record<AdminMarketSource["health"], string> = { healthy: "健康", full: "已挂满", unavailable: "不可用" };

export const sourceName = (item: AdminMarketSource) => item.displayName || item.accountId;
const kindLabel = (item: AdminMarketSource) => item.marketOnly ? "仅行情" : KIND_LABEL[item.providerKind] ?? item.providerKind;

type Refusal = { status?: number; detail?: { detail?: { code?: string; reason?: string; max?: number; running?: number; poolSources?: number } | string } };

/** Why 添加快期行情账号 did not go through, in the owner's words. The password is never part of it. */
function addRefusal(failure: unknown): string {
  const { status, detail } = failure as Refusal;
  const body = typeof detail?.detail === "object" ? detail.detail : undefined;
  switch (body?.code) {
    case "tqkq_bind_failed":
      return body.reason === "auth_failed" ? "快期账号或口令不对，未添加"
        : body.reason === "permission_denied" ? "该快期账号没有行情权限，未添加"
          : body.reason === "timeout" ? "快期校验超时，未添加，请稍后重试"
            : `快期校验未通过（${body.reason ?? "未知原因"}），未添加`;
    case "market_source_duplicate": return "该账号已在清单里";
    case "market_source_account_in_use": return "该快期账号已是某位用户的交易账户：在下方“已有快期账户”里选它加入";
    case "market_source_main_account": return "这是主账号，已是常驻行情源，不用再加";
    case "market_source_list_full": return `清单最多 ${body.max ?? 16} 个账号`;
    case "market_source_pool_full":
      return `号池上限不够：最多 ${body.max} 个快期连接，当前在用 ${body.running} 个、清单里接入池账号 ${body.poolSources} 个。`
        + "调大服务器的 AI_TRADER_TQKQ_POOL_MAX（每个连接约 200 MB 内存）并重建号池后再加";
    case "invalid_username": return "快期账号格式不对：3–64 位字母、数字或 _ . @ + -";
    case "invalid_password": return "口令为空或过长";
  }
  if (status === 503) return "号池暂时不可用，请稍后重试";
  return "添加未生效，请重试";
}

/** Health as the row shows it: the account's own state wins over the relay's view. */
function sourceHealth(item: AdminMarketSource): { label: string; tone: "ok" | "warn" | "danger" | "idle" } {
  if (item.accountStatus === "missing") return { label: "账户不存在", tone: "danger" };
  if (item.accountStatus !== "active") return { label: "账户已停用", tone: "idle" };
  return { label: HEALTH_LABEL[item.health] ?? item.health, tone: item.health === "healthy" ? "ok" : item.health === "full" ? "warn" : "danger" };
}

/** The 行情源 section: the public market relay's ordered TqKq accounts. Totals on top,
 *  one row per source (K-line channel matrix, quote share, last error), 上移/下移/停用
 *  save the whole order at once — optimistic, rolled back when the write fails. The add
 *  box takes a new TqKq login (account + password, verified by the pool, kept as a
 *  行情专用 account that 移除 deletes again) or an active TqKq account not yet listed. */
export function MarketSourcesSection({ baseUrl, accounts }: { baseUrl: string; accounts: AdminAccount[] | undefined }) {
  const [snapshot, setSnapshot] = useState<AdminMarketSourcesSnapshot>();
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{ text: string; error?: boolean }>();
  const [addStatus, setAddStatus] = useState<{ text: string; error?: boolean }>();
  const [candidate, setCandidate] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const accountInput = useRef<HTMLInputElement>(null);
  // 行情专用 accounts removed here: disabled now, though the accounts list loaded with the page still says active.
  const [removed, setRemoved] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    getAdminMarketSources(baseUrl)
      .then(result => { if (!cancelled) setSnapshot(result); })
      .catch(() => { if (!cancelled) setLoadFailed(true); });
    return () => { cancelled = true; };
  }, [baseUrl]);

  const items = useMemo(() => snapshot?.items ?? [], [snapshot]);
  const candidates = useMemo(() => {
    const listed = new Set([...items.map(item => item.accountId), ...removed]);
    return (accounts ?? []).filter(account => account.status === "active" && SOURCE_KINDS.has(account.providerKind) && !listed.has(account.id));
  }, [accounts, items, removed]);
  const chosen = candidates.some(account => account.id === candidate) ? candidate : candidates[0]?.id ?? "";

  /** PUT the whole order; the list shows the new order at once and goes back on failure. */
  const save = async (next: AdminMarketSource[], done: string) => {
    if (!snapshot) return;
    const previous = snapshot;
    setSnapshot({ ...snapshot, items: next.map((item, position) => ({ ...item, position })) });
    setSaving(true);
    setStatus({ text: "保存中…" });
    try {
      setSnapshot(await updateAdminMarketSources(baseUrl, next.map(item => item.accountId)));
      setStatus({ text: done });
    } catch (failure) {
      setSnapshot(previous);
      const code = (failure as { detail?: { detail?: { code?: string } } }).detail?.detail?.code;
      setStatus({ text: code === "market_source_invalid" ? "保存未生效：该账号不是启用中的快期账户，已恢复原顺序" : "保存未生效，已恢复原顺序", error: true });
    } finally {
      setSaving(false);
    }
  };

  const move = (index: number, delta: -1 | 1) => {
    const next = [...items];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    void save(next, `已把「${sourceName(items[index])}」${delta < 0 ? "上移" : "下移"}`);
  };
  const remove = (index: number) => void save(items.filter((_, position) => position !== index), `已停用「${sourceName(items[index])}」`);

  /** 移除 a 行情专用 account: it leaves the list, is disabled and its saved password deleted. */
  const removeMarketOnly = async (item: AdminMarketSource) => {
    const name = sourceName(item);
    if (!window.confirm(`移除「${name}」？它会离开清单、停止连接，保存的口令一并删除；以后要用需重新输入口令。`)) return;
    setSaving(true);
    setStatus({ text: "移除中…" });
    try {
      setSnapshot(await removeAdminMarketSourceAccount(baseUrl, item.accountId));
      setRemoved(current => [...current, item.accountId]);
      setStatus({ text: `已移除「${name}」并删除保存的口令` });
    } catch {
      setStatus({ text: "移除未生效，请重试", error: true });
    } finally {
      setSaving(false);
    }
  };

  /** 验证并加入: the pool checks the login first (this takes a few seconds); the password
   *  field is cleared after every attempt and never shown back. */
  const addLogin = async () => {
    const account = username.trim();
    if (!snapshot || !account || !password) return;
    setSaving(true);
    setAddStatus({ text: "正在向快期校验账号与口令…" });
    try {
      const result = await addAdminMarketSourceAccount(baseUrl, account, password);
      setSnapshot({ items: result.items, seriesPerSource: result.seriesPerSource });
      setUsername("");
      setAddStatus({ text: `已验证并把「${account}」加到清单末尾，立即生效` });
    } catch (failure) {
      setAddStatus({ text: addRefusal(failure), error: true });
    } finally {
      setPassword("");
      setSaving(false);
    }
  };

  const add = async () => {
    const account = candidates.find(item => item.id === chosen);
    if (!snapshot || !account) return;
    setSaving(true);
    setAddStatus({ text: "加入中…" });
    try {
      setSnapshot(await updateAdminMarketSources(baseUrl, [...items.map(item => item.accountId), account.id]));
      setAddStatus({ text: `已把「${account.displayName}」加到清单末尾` });
    } catch {
      setAddStatus({ text: "加入未生效，请重试", error: true });
    } finally {
      setSaving(false);
    }
  };

  const cap = snapshot?.seriesPerSource ?? 48;
  const header = <SectionHeader title="行情源" helpKey="market-sources" help={`按顺序接力：前一个挂满 ${cap} 组 K 线或断线，新的行情交给下一个`}
    action={<Button disabled={!snapshot || saving} onClick={() => accountInput.current?.focus()}>添加快期账号</Button>} />;
  if (!snapshot) return <>
    {header}
    <div className="fdw-admin-section-body"><p className="fdw-admin-empty">{loadFailed ? "行情源读取失败，请切换分区重试" : "读取中"}</p></div>
  </>;

  const used = items.reduce((total, item) => total + item.channels, 0);
  const capacity = items.reduce((total, item) => total + item.channelLimit, 0);
  const quotes = items.reduce((total, item) => total + item.quoteSymbols, 0);
  const unhealthy = items.filter(item => sourceHealth(item).tone !== "ok").length;
  return <>
    {header}
    <div className="fdw-admin-section-body">
      <AdminTiles label="行情源总量" items={[
        { label: "K 线通道", value: used, unit: ` / ${capacity}` },
        { label: "报价订阅", value: quotes, unit: " 个合约" },
        { label: "账号", value: items.length, unit: ` 个 · ${items.length === 0 ? "未配置" : unhealthy ? `${unhealthy} 个异常` : "全部健康"}` },
      ]} />
      {status ? <p className={status.error ? "fdw-admin-load-error" : "fdw-admin-inline-note"} role={status.error ? "alert" : "status"}>{status.text}</p> : null}
      {items.length === 0 ? <p className="fdw-admin-empty">清单为空：游客行情没有来源，先加一个快期账号</p> : (
        <ol className="fdw-admin-sources" aria-label="行情源清单">
          {items.map((item, index) => {
            const health = sourceHealth(item);
            const name = sourceName(item);
            const share = quotes ? Math.round(item.quoteSymbols / quotes * 100) : 0;
            return <li key={item.accountId} className="fdw-admin-source" data-first={index === 0 ? "" : undefined} aria-label={`${index + 1} ${name}`}>
              <div className="fdw-admin-source-id">
                <span className="fdw-admin-source-rank">{index + 1}</span>
                <div className="fdw-admin-source-name">
                  <span><strong>{name}</strong><span className="fdw-admin-kind">{kindLabel(item)}</span></span>
                  <span className="fdw-admin-mono-note">{item.accountId}</span>
                  <span className="fdw-admin-source-health"><span className="fdw-admin-dot" data-tone={health.tone} />{health.label}{index === 0 && health.tone === "ok" ? " · 首选" : ""}</span>
                </div>
              </div>
              <div className="fdw-admin-source-channels">
                <span className="fdw-admin-source-caption">K 线 {item.channels} / {item.channelLimit}</span>
                <span className="fdw-admin-cells" role="img" aria-label={`K 线占用 ${item.channels} / ${item.channelLimit}`}>
                  {Array.from({ length: item.channelLimit }, (_, cell) => <i key={cell} data-on={cell < item.channels ? "" : undefined} />)}
                </span>
              </div>
              <div className="fdw-admin-source-quotes">
                <span className="fdw-admin-source-caption">报价 {item.quoteSymbols} 个 · 占 {share}%</span>
                <Meter percent={share} />
                {item.lastError
                  ? <span className="fdw-admin-source-error">最近错误：{item.lastError}{item.lastErrorAt ? ` · ${fmtDateTime(item.lastErrorAt)}` : ""}</span>
                  : <span className="fdw-admin-source-caption">没有错误</span>}
              </div>
              <div className="fdw-admin-source-actions">
                <button type="button" className="fdw-admin-icon-button" aria-label={`上移 ${name}`} title="上移"
                  disabled={saving || index === 0} onClick={() => move(index, -1)}>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 15l6-6 6 6" /></svg>
                </button>
                <button type="button" className="fdw-admin-icon-button" aria-label={`下移 ${name}`} title="下移"
                  disabled={saving || index === items.length - 1} onClick={() => move(index, 1)}>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
                </button>
                {item.marketOnly
                  ? <button type="button" className="fdw-admin-outline-button danger" aria-label={`移除 ${name}`}
                    disabled={saving} onClick={() => void removeMarketOnly(item)}>移除</button>
                  : <button type="button" className="fdw-admin-outline-button" aria-label={`停用 ${name}`}
                    disabled={saving} onClick={() => remove(index)}>停用</button>}
              </div>
            </li>;
          })}
        </ol>
      )}
      <section className="fdw-admin-source-add" aria-label="添加快期账号">
        <header className="fdw-admin-source-add-head">
          <h3>添加快期行情账号</h3>
          <HelpTip helpKey="admin:market-sources-add" label="添加快期行情账号">
            输入一个快期账号与口令：先向快期校验，通过后口令加密保存，账号作为“仅行情”源加到清单末尾、立即生效。
            它不属于任何用户，不出现在交易账户里，也不能下单；移除会停止连接并删除保存的口令。
          </HelpTip>
        </header>
        <form className="fdw-admin-source-add-row" autoComplete="off"
          onSubmit={event => { event.preventDefault(); void addLogin(); }}>
          <label className="fdw-admin-field">
            <span>快期账号</span>
            <TextInput label="快期账号" value={username} inputRef={accountInput} autoComplete="off" maxLength={64}
              placeholder="手机号、邮箱或用户名" disabled={saving} onChange={setUsername} />
          </label>
          <label className="fdw-admin-field">
            <span>口令</span>
            <TextInput label="快期口令" value={password} password autoComplete="new-password" maxLength={400}
              disabled={saving} onChange={setPassword} />
          </label>
          <button type="submit" className="app-button primary" disabled={saving || !username.trim() || !password}>验证并加入</button>
        </form>
        {addStatus ? <p className={addStatus.error ? "fdw-admin-load-error" : "fdw-admin-inline-note"} role={addStatus.error ? "alert" : "status"}>{addStatus.text}</p> : null}
        {candidates.length > 0 ? (
          <div className="fdw-admin-source-add-row">
            <label className="fdw-admin-field">
              <span>或选已有快期账户</span>
              <select className="app-select" aria-label="选择快期账号" value={chosen}
                disabled={saving} onChange={event => setCandidate(event.target.value)}>
                {candidates.map(account => <option key={account.id} value={account.id}>{account.displayName} · {account.id}</option>)}
              </select>
            </label>
            <Button disabled={saving || !chosen} onClick={() => void add()}>加入清单末尾</Button>
          </div>
        ) : null}
      </section>
    </div>
  </>;
}
