import type { ProductBrief } from "../../api";

/**
 * One system entry in the 对话面板 (2026-09-25: the panel holds the conversation only). A brief
 * is a message the AI sends the user, so it stays in the conversation, merged on time with the
 * turns. Strategy judgements, orders, fills, P&L and protection changes live in the 策略 panel's
 * 运行中 tab and the 持仓 panel's 成交 tab instead. Since 2026-10-04 each brief renders as a compact
 * 快讯/异动 card with its source, the contract it was searched for and a 问 AI button.
 */
export interface FlowItem {
  id: string;
  /** news = 重要资讯, market = 行情异动. */
  kind: "news" | "market";
  /** Epoch milliseconds; the flow is merged with the conversation turns on this. */
  time: number;
  title: string;
  /** The brief's one-sentence summary. */
  detail: string;
  source: string;
  /** The contract the brief was searched for (its subscribed product's main contract; a Runtime
   *  without subscriptions sends none and the dock's current quote stands in). */
  symbol?: string;
  /** Its display name (the product, 螺纹钢); falls back to the code. */
  symbolName?: string;
  /** The subscribed product ("SHFE.rb"): 通知's per-product filter. */
  product?: string;
  tone?: "warning" | "positive" | "negative";
}

const FLOW_LIMIT = 60;

/** The briefs as conversation entries, newest last (the conversation reads top-down). */
type BriefInput = Omit<ProductBrief, "product" | "productName" | "symbol"> & { product?: string | null; productName?: string | null; symbol?: string | null };

export function buildFlowItems({ briefs, symbol, symbolName }: { briefs?: BriefInput[]; symbol?: string; symbolName?: string }): FlowItem[] {
  return (briefs ?? [])
    .map((brief): FlowItem => {
      const own = Boolean(brief.product || brief.symbol);
      return {
        id: `brief:${brief.id}`, kind: brief.kind === "news" ? "news" : "market", time: Date.parse(brief.publishedAt),
        title: brief.kind === "news" ? "重要资讯" : "行情异动", detail: brief.text, source: brief.source,
        ...(own ? { symbol: brief.symbol ?? undefined, symbolName: brief.productName || brief.symbol || brief.product || undefined, product: brief.product ?? undefined }
          : { symbol, symbolName: symbolName || symbol }),
      };
    })
    .filter(item => Number.isFinite(item.time))
    .sort((a, b) => a.time - b.time)
    .slice(-FLOW_LIMIT);
}

/** The 问 AI question a brief card sends as the user's own message. */
export function briefQuestion(item: FlowItem): string {
  const name = item.symbolName || item.symbol || "这个品种";
  return item.kind === "news" ? `这条资讯对${name}有什么影响：${item.detail}` : `${name}${item.detail}现在怎么看？`;
}
