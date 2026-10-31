import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronUp } from "lucide-react";
import { getBriefSubscriptions, saveBriefSubscriptions, type BriefProduct, type BriefSubscriptions, type InstrumentCatalogItem } from "../api";
import { useIdentityFence } from "../hooks/useIdentityFence";
import { readNetworkMode, readQueryClient } from "../runtimeClient";
import { useDisclosureSurface } from "../uiSurfaces";
import { SettingRow, SettingsSection } from "./SettingsPanel";
import { Button, TextInput } from "./settings/controls";
import { catalogProducts, type ProductGroup } from "./futures/WorkspaceDrawers";
import "./BriefSubscriptions.css";

const PICKER_SHOWN = 12;

function saveError(failure: unknown) {
  const detail = (failure as { detail?: { detail?: { code?: string; limit?: number; product?: string } } } | undefined)?.detail?.detail;
  if (detail?.code === "brief_limit_exceeded") return `当前档位最多 ${detail.limit} 个，未保存`;
  if (detail?.code === "brief_product_unknown") return `没有找到品种 ${detail.product ?? ""}，未保存`;
  return "订阅未保存，请重试";
}

/**
 * AI 简报订阅 (2026-10-05): the products the briefs are searched for, in push order. Shared by
 * 我的 → AI 简报推送 and 设置 → 提醒 → AI 简讯的订阅品种 (one cached read). A change saves at once: the list
 * shows it immediately and goes back to the saved one, with the reason, when the save fails.
 */
export function useBriefSubscriptions(baseUrl: string | undefined, enabled = true) {
  const fence = useIdentityFence([baseUrl]);
  const identity = fence.context.identity;
  const key = ["workbench", identity, baseUrl, "brief-subscriptions"] as const;
  const query = useQuery({ queryKey: key, enabled: baseUrl !== undefined && enabled, networkMode: readNetworkMode(baseUrl),
    queryFn: async ({ signal }) => {
      const value = await getBriefSubscriptions(baseUrl!, signal);
      // A Runtime before subscriptions answers something else: the rows say the read failed.
      if (!Array.isArray(value?.products)) throw new Error("简报订阅读取失败");
      return value;
    } }, readQueryClient);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const save = async (products: BriefProduct[]) => {
    const previous = query.data;
    if (baseUrl === undefined || !previous) return;
    setSaving(true); setError("");
    await readQueryClient.cancelQueries({ queryKey: key, exact: true });
    readQueryClient.setQueryData<BriefSubscriptions>(key, { ...previous, products, source: "user",
      overLimit: previous.limit !== null && products.length > previous.limit });
    try {
      const saved = await saveBriefSubscriptions(baseUrl, products.map(item => item.product));
      if (!fence.isCurrent()) return;
      readQueryClient.setQueryData(key, saved);
      // The merged briefs follow the new list on their next read.
      void readQueryClient.invalidateQueries({ queryKey: ["workbench", identity, baseUrl, "briefs"], exact: true });
    } catch (failure) {
      if (!fence.isCurrent()) return;
      readQueryClient.setQueryData(key, previous);
      setError(saveError(failure));
    } finally { setSaving(false); }
  };
  return { data: query.data, failed: query.isError, saving, error, save, reload: query.refetch };
}

/**
 * 订阅品种 and its 添加品种 picker as settings blocks: one row per product (上移 / 下移 / 取消), the
 * count against the tier's cap with 添加品种 (disabled at the cap), then — while `disclosure:
 * brief-add` is open — the catalog's products to add, the starred ones first, searchable by name or code.
 */
export function BriefSubscriptionEditor({ baseUrl, instruments, watchlist, disabled }: {
  baseUrl: string | undefined;
  instruments: readonly InstrumentCatalogItem[];
  /** The account's starred products ("SHFE.rb"): offered first. */
  watchlist: readonly string[];
  /** Offline / the tier has no briefs: the list shows, nothing saves. */
  disabled?: boolean;
}) {
  const subs = useBriefSubscriptions(baseUrl);
  const [adding, setAdding] = useDisclosureSurface("brief-add", { available: Boolean(subs.data) });
  const [query, setQuery] = useState("");
  const groups = useMemo(() => catalogProducts(instruments), [instruments]);
  if (!subs.data) {
    return <SettingsSection title="订阅品种">
      <SettingRow label="订阅品种"><strong role="status">{subs.failed ? "订阅读取失败，稍后重试" : baseUrl === undefined ? "连接后可设置" : "读取中…"}</strong></SettingRow>
    </SettingsSection>;
  }
  const { products, limit, source, overLimit } = subs.data;
  const full = limit !== null && products.length >= limit;
  const busy = Boolean(disabled) || subs.saving;
  // Over a lowered cap only the first N are pushed; the order changes once the list fits again.
  const locked = limit !== null && products.length > limit;
  const move = (index: number, delta: -1 | 1) => {
    const next = [...products];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    void subs.save(next);
  };
  const subscribed = new Set(products.map(item => item.product));
  const starred = new Set(watchlist);
  const needle = query.trim().toLowerCase();
  const open = groups.filter(group => !subscribed.has(group.key));
  const candidates: ProductGroup[] = needle
    ? open.filter(group => `${group.productName}${group.productId}${group.key}`.toLowerCase().includes(needle))
      .sort((a, b) => Number(starred.has(b.key)) - Number(starred.has(a.key))).slice(0, PICKER_SHOWN)
    : watchlist.flatMap(key => open.filter(group => group.key === key));
  const add = (group: ProductGroup) => {
    void subs.save([...products, { product: group.key, name: group.productName, symbol: group.main.symbol }]);
    setQuery("");
  };
  return <div className="brief-subs">
    <SettingsSection title="订阅品种" hint={source === "default" ? "默认使用你的自选品种" : undefined}>
      {overLimit ? <p className="brief-subs-note" role="status">超出当前档位上限，只推送前 {limit} 个；取消多余的后可调整顺序</p> : null}
      {products.map((item, index) => {
        const unpushed = limit !== null && index >= limit;
        return <SettingRow key={item.product} label={item.name} hint={unpushed ? `${item.symbol} · 超出上限，未推送` : item.symbol}>
          <button type="button" className="app-button brief-subs-move" aria-label={`上移${item.name}`} disabled={busy || locked || index === 0}
            onClick={() => move(index, -1)}><ChevronUp aria-hidden="true" /></button>
          <button type="button" className="app-button brief-subs-move" aria-label={`下移${item.name}`} disabled={busy || locked || index === products.length - 1}
            onClick={() => move(index, 1)}><ChevronDown aria-hidden="true" /></button>
          <button type="button" className="app-button" aria-label={`取消订阅${item.name}`} disabled={busy}
            onClick={() => void subs.save(products.filter(entry => entry.product !== item.product))}>取消</button>
        </SettingRow>;
      })}
      {!products.length ? <p className="brief-subs-note">还没有订阅品种；添加后 AI 会为它检索简报。</p> : null}
      <SettingRow label={limit === null ? `已选 ${products.length}（不限）` : `已选 ${products.length} / ${limit}`}
        hint={full ? `当前档位最多 ${limit} 个` : undefined}>
        <Button disabled={busy || (full && !adding)} status={subs.error || undefined}
          onClick={() => setAdding(value => !value)}>{adding ? "收起" : "添加品种"}</Button>
      </SettingRow>
    </SettingsSection>
    {adding ? <SettingsSection title="添加品种" hint="从合约目录按品种选，先列自选">
      <SettingRow label="搜索品种">
        <TextInput label="搜索品种" value={query} placeholder="名称或代码，如 螺纹 / rb" onChange={setQuery} />
      </SettingRow>
      {candidates.map(group => (
        <SettingRow key={group.key} label={group.productName} hint={`${starred.has(group.key) ? "自选 · " : ""}${group.main.symbol}`}>
          <button type="button" className="app-button" aria-label={`添加${group.productName}`} disabled={busy || full} onClick={() => add(group)}>添加</button>
        </SettingRow>
      ))}
      {!candidates.length ? <p className="brief-subs-note">{!groups.length ? "合约目录读取中…"
        : needle ? "没有匹配的品种" : "自选品种都已订阅；输入名称或代码搜索全部品种"}</p> : null}
    </SettingsSection> : null}
  </div>;
}
