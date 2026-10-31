import { useEffect, useRef, useState } from "react";
import { requestJson } from "../api";
import { LONG_READ_TIMEOUT_MS } from "../requestTimeout";
import { SectionCard } from "./Blocks";
import { guestRequest } from "../guestApi";
import { useUiSurface } from "../uiSurfaces";

type Status = "ok" | "stale" | "unavailable";
interface Item { dataset: string; status: Status; asOf: string | null; source: string | null; error?: string; data: Record<string, unknown> | null }
export interface ResearchSummary { symbol: string; product: string; contract: string; generatedAt: string; datasets: Record<string, Item>;
  /** 外盘品种 (2026-10-21): none of these domestic datasets exist for it; `notice` says so. */
  foreign?: boolean; notice?: string }

const REFRESH_MS = 30 * 60 * 1000;
const money = (value: unknown, digits = 0) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "—";
const signed = (value: unknown, digits = 0) => typeof value === "number" && Number.isFinite(value) ? `${value > 0 ? "+" : ""}${money(value, digits)}` : "—";

/** One line per available dataset: the number that matters, then its as-of date. */
function line(item: Item): { value: string; detail: string; tone: "ok" | "stale" } {
  const d = item.data ?? {};
  const tone = item.status === "stale" ? "stale" : "ok";
  const when = item.asOf ? `${item.asOf.length === 8 ? `${item.asOf.slice(4, 6)}-${item.asOf.slice(6, 8)}` : item.asOf.slice(5, 10)}` : "";
  switch (item.dataset) {
    case "spot_basis": return { value: `基差 ${signed(d.dominantBasis)}（${typeof d.dominantBasisRate === "number" ? `${(d.dominantBasisRate * 100).toFixed(2)}%` : "—"}）`, detail: `现货 ${money(d.spotPrice, 2)} · ${when}`, tone };
    case "warehouse_receipt": return { value: `仓单 ${money(d.total)}（${signed(d.change)}）`, detail: when, tone };
    case "position_rank": return { value: `前20净持仓 ${signed(d.netLong)}`, detail: `多 ${money(d.longTotal)}（${signed(d.longChange)}） 空 ${money(d.shortTotal)}（${signed(d.shortChange)}） · ${when}`, tone };
    case "term_structure": return { value: `${({ contango: "远月升水 contango", backwardation: "远月贴水 backwardation", flat: "近远月持平" } as Record<string, string>)[String(d.shape)] ?? "—"}`, detail: `${String(d.nearContract)} → ${String(d.farContract)} ${signed(d.spread, 1)} · ${when}`, tone };
    case "fees_margin": return { value: `保证金 ${typeof d.marginRateLong === "number" ? `${d.marginRateLong}%` : "—"} · 手续费 ${money(d.feePerLot, 1)} 元/手`, detail: `${String(d.contract)} · 参考值`, tone };
    default: return { value: "—", detail: "", tone };
  }
}

// No 库存 line: Eastmoney's "inventory" is the registered receipts again (checked 2026-09-29); the 调研 page
// shows SHFE's weekly warehouse stock instead.
const LABELS: Array<[string, string]> = [["spot_basis", "基差"], ["warehouse_receipt", "仓单"], ["position_rank", "持仓排名"], ["term_structure", "期限结构"], ["fees_margin", "保证金"]];

/** Product research in the news panel, refreshed every 30 minutes. */
export function ResearchCard({ baseUrl, symbol, replay, defaultOpen = true, guest = false, onGuestExpired }: { baseUrl: string | undefined; symbol: string | undefined; replay: boolean; defaultOpen?: boolean; guest?: boolean; onGuestExpired?: () => void }) {
  const [summary, setSummary] = useState<ResearchSummary>();
  const expired = useRef(onGuestExpired);
  expired.current = onGuestExpired;
  const [failure, setFailure] = useState<{symbol: string; message: string}>();
  const error = failure && failure.symbol === symbol ? failure.message : "";
  const [open, setOpen] = useState(defaultOpen);
  const current = summary?.symbol === symbol ? summary : undefined;
  const items = current?.datasets ?? {};
  const available = LABELS.filter(([key]) => items[key] && items[key].status !== "unavailable" && items[key].data);
  // The card exists only with data; an absent card is not commandable or reported.
  const rendered = !replay && Boolean(symbol) && available.length > 0;
  useUiSurface("monitorCard:research", { state: open ? "expanded" : "collapsed", apply: value => setOpen(Boolean(value)) }, [open], rendered);
  useEffect(() => {
    setSummary(undefined); setFailure(undefined);
    if (!baseUrl || !symbol || replay) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try { const path = `/api/${guest ? "guest/" : ""}research/summary?symbol=${encodeURIComponent(symbol)}`; const next = await (guest ? guestRequest<ResearchSummary>(baseUrl, path, { timeoutMs: LONG_READ_TIMEOUT_MS }) : requestJson<ResearchSummary>(`${baseUrl}${path}`, { timeoutMs: LONG_READ_TIMEOUT_MS })); if (!cancelled) { setSummary(next); setFailure(undefined); } }
      catch (cause) {
        if (cancelled) return;
        if (guest && (cause as { status?: number }).status === 401) { expired.current?.(); return; }
        setFailure({symbol, message: cause instanceof Error ? cause.message : "研究数据加载失败"});
      }
      finally { if (!cancelled) timer = setTimeout(load, REFRESH_MS); }
    };
    void load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl, symbol, replay, guest]);
  if (replay || !symbol) return null;
  if (current?.foreign) return <SectionCard title="研究数据"><div className="section-card-body"><p role="status">{current.notice}</p></div></SectionCard>;
  if (!rendered || !current) return <SectionCard title="研究数据"><div className="section-card-body">{error || current ? <p role="alert">研究数据暂不可用</p> : <p role="status">加载中…</p>}</div></SectionCard>;
  const staleCount = available.filter(([key]) => items[key].status === "stale").length;
  return (
    <SectionCard title="研究数据" note={`${current.product.toUpperCase()} · ${available.length} 项${staleCount ? `，${staleCount} 项为旧值` : ""}${error ? " · 刷新失败，显示旧值" : ""}`}
      testId="research-card"
      actions={<button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? "收起" : "展开"}</button>}>
      {open ? (
        <div className="section-card-body">
          <ul className="research-lines" aria-label="研究数据明细">
            {available.map(([key, label]) => { const row = line(items[key]); return (
              <li key={key} data-dataset={key} data-tone={row.tone}>
                <span>{label}</span>
                <strong>{row.value}{row.tone === "stale" ? <em title="来源暂时失败，显示上次成功值">旧值</em> : null}</strong>
                <small>{row.detail}</small>
              </li>
            ); })}
          </ul>
          <small className="research-note">来源 交易所官网、AKShare（生意社 / 东方财富 / 新浪 / 九期网），研究线索非交易授权；保证金、手续费为第三方参考值。</small>
        </div>
      ) : null}
    </SectionCard>
  );
}
