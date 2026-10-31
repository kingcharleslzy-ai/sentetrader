import { productOfSymbol } from "../FlashFeed";
import { formatSigned } from "../futures/support";
import type { MobileFeedItem } from "./feedItems";

/**
 * 手机版 AI 首页的时间线 (原通知页 2026-10-04; 并进 AI 首页 2026-10-06): the stream as one-line rows —
 * 成交回报, 今日判断 and 简报, plus (under 全部 only) the 确认卡 once handled or expired and the
 * conversation's 回测. Under AI 简报 the rows can be narrowed to one product (2026-10-05: briefs come
 * for every subscribed product). 提醒 and what is unread moved to the Runtime's 提醒中心 (2026-10-15,
 * `reminders.ts`): the read marks this browser kept in localStorage are gone with them.
 */
export type NotifyFilter = "all" | "fill" | "brief";
export const NOTIFY_FILTERS: [NotifyFilter, string][] = [["all", "全部"], ["fill", "成交"], ["brief", "AI 简报"]];

export type NotifyTone = "warning" | "danger" | "positive" | "accent" | "neutral";
export interface NotifyRow {
  id: string;
  time: number;
  /** The filter that lists the row besides 全部; null = 全部 only (确认卡, 回测). */
  filter: Exclude<NotifyFilter, "all"> | null;
  tag: string;
  tone: NotifyTone;
  title: string;
  summary: string;
  /** 简报 / 今日判断: the product ("SHFE.rb") and its name, shown as a tag and filtered on. */
  product?: string;
  productName?: string;
  item: MobileFeedItem;
}

/** "SHFE.rb" from a contract or a main-contract alias (KQ.m@SHFE.rb); a 外盘主连 (KQD.m@COMEX.GC) → its product key "COMEX.GC". */
export const productKey = (symbol: string) => productOfSymbol(symbol.replace(/^KQD?\.[a-z]@/i, ""));

export function notifyRow(item: MobileFeedItem): NotifyRow {
  const base = { id: item.id, time: item.time, item };
  if (item.kind === "execution") {
    const card = item.card;
    const price = card.price ? ` @ ${card.price}` : "";
    return { ...base, filter: "fill", tag: "成交", tone: card.status === "filled" || card.status === "partial" ? "positive" : "neutral",
      title: `${card.sideLabel} ${card.name || card.symbol} ${card.volume} 手${price}`,
      summary: [card.sourceLabel, card.statusLabel, card.detail].filter(Boolean).join(" · ") };
  }
  if (item.kind === "proposal") {
    const { card } = item;
    const status = card.payload.status;
    const [tag, tone]: [string, NotifyTone] = status === "candidate_recorded" ? ["已确认", "positive"] : status === "rejected" ? ["已放弃", "neutral"]
      : status === "risk_blocked" ? ["未通过", "danger"] : status === "approval_required" ? ["已过期", "neutral"] : ["方案", "neutral"];
    if (card.type === "trade") {
      const proposal = card.payload;
      const side = { buy_open: "买入开仓", sell_open: "卖出开仓", buy_close: "买入平仓", sell_close: "卖出平仓" }[proposal.side];
      return { ...base, filter: null, tag, tone, title: `${side} ${proposal.name || proposal.symbol} ${proposal.volume} 手 @ ${proposal.limitPrice}`,
        summary: [proposal.strategySource ? `策略 ${proposal.strategySource.name}` : "AI 方案卡", proposal.accountLabel].filter(Boolean).join(" · ") };
    }
    if (card.type === "close-batch") {
      return { ...base, filter: null, tag, tone, title: `批量平仓 ${card.payload.totalOrders} 笔，共 ${card.payload.totalVolume} 手`, summary: card.payload.reason };
    }
    return { ...base, filter: null, tag, tone, title: `价差 ${card.payload.spreadWords} · ${card.payload.legsWords}`, summary: card.payload.reason };
  }
  if (item.kind === "backtest") {
    const { card } = item;
    const figures = card.summary;
    return { ...base, filter: null, tag: "回测", tone: "accent", title: `回测跑完了 · ${card.strategyName}`,
      summary: [card.symbol, `净利 ${formatSigned(figures.netProfit, 0)}`, `${figures.trades} 笔`, figures.winRate === null ? "" : `胜率 ${figures.winRate}%`].filter(Boolean).join(" · ") };
  }
  if (item.kind === "judgment") {
    const card = item.card;
    const ready = card.status === "ready";
    return { ...base, filter: "brief", tag: "AI", tone: "accent", product: card.symbol ? productKey(card.symbol) : undefined, productName: card.productName,
      title: ready ? `今日判断已更新：${card.productName}${card.stance ?? ""}` : `今日判断：${card.productName}`,
      summary: ready ? [card.headline, card.confidence ? `置信 ${card.confidence}` : ""].filter(Boolean).join(" · ") : card.note || "暂时没有判断" };
  }
  const brief = item.item;
  return { ...base, filter: "brief", tag: "简报", tone: "accent", title: brief.title, summary: brief.detail,
    product: brief.product ?? (brief.symbol ? productKey(brief.symbol) : undefined), productName: brief.symbolName };
}

/** The AI 简报 products: the pushed subscribed ones in push order, then the others the rows carry,
 *  newest first, once each. A row's product is named by the catalog (`names`, "CZCE.AP" → "苹果")
 *  before the row's own name: a brief without a product name carries the code there. */
export function notifyProducts(rows: NotifyRow[], subscribed: readonly { product: string; name: string }[] = [],
  names: Readonly<Record<string, string>> = {}): { product: string; name: string }[] {
  const seen = new Map<string, string>(subscribed.map(item => [item.product, item.name || names[item.product] || item.product]));
  for (const row of rows) if (row.filter === "brief" && row.product && !seen.has(row.product)) seen.set(row.product, names[row.product] || row.productName || row.product);
  return [...seen].map(([product, name]) => ({ product, name }));
}

/** "10:42" today, "昨天 14:50", otherwise "10-02 09:30" (the viewer's time zone). */
export function notifyTime(time: number, now = Date.now()): string {
  const date = new Date(time);
  if (!Number.isFinite(time) || time < 1e11) return "";
  const clock = date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  const day = (value: Date) => `${value.getFullYear()}-${value.getMonth()}-${value.getDate()}`;
  if (day(date) === day(new Date(now))) return clock;
  if (day(date) === day(new Date(now - 86_400_000))) return `昨天 ${clock}`;
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${clock}`;
}

/** A row's short time (AI 首页 timeline, 最近的回测): "10:42" today, "昨天", otherwise "10-02". */
export function rowTime(time: number, now = Date.now()): string {
  const full = notifyTime(time, now);
  return full.startsWith("昨天") ? "昨天" : full.length > 5 ? full.slice(0, 5) : full;
}

export type JudgmentStance = "偏多" | "偏空" | "震荡" | "暂无" | "读取中";

const sameLocalDay = (a: number, b: number) => new Date(a).toDateString() === new Date(b).toDateString();
const readyStance = (card: { status: string; stance?: string | null }): "偏多" | "偏空" | "震荡" | null =>
  card.status === "ready" && (card.stance === "偏多" || card.stance === "偏空" || card.stance === "震荡") ? card.stance : null;

/** 今日判断 chips: one per product (the AI 简报 product order — subscribed first), each with the
 *  stance of the newest ready judgment of the product's latest research day, whichever source in
 *  this window carries it (a conversation card, the desk read) and the contract it was read on.
 *  While the desk read of a product has not returned (`loading`) the chip says 读取中 — unless a
 *  card made today already gives the stance; 暂无 only once nothing ready was found. */
export function judgmentChips(products: readonly { product: string; name: string }[], items: readonly MobileFeedItem[],
  { loading = new Set<string>(), now = Date.now() }: { loading?: ReadonlySet<string>; now?: number } = {}):
  { product: string; name: string; stance: JudgmentStance; symbol?: string }[] {
  return products.map(({ product, name }) => {
    const key = product.toLowerCase();
    const mine = items.filter((item): item is Extract<MobileFeedItem, { kind: "judgment" }> =>
      item.kind === "judgment" && Boolean(item.card.symbol) && productKey(item.card.symbol).toLowerCase() === key);
    const day = mine[0]?.card.researchDay ?? null;
    const reading = loading.has(key);
    const judged = mine.find(item => readyStance(item.card) && (item.card.researchDay ?? null) === day
      && (!reading || sameLocalDay(item.time, now)));
    const stance: JudgmentStance = (judged && readyStance(judged.card)) || (reading ? "读取中" : "暂无");
    return { product, name, stance, symbol: (judged ?? mine[0])?.card.symbol };
  });
}
