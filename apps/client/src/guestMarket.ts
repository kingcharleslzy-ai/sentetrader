import type { MarketKlineBar, MarketWorkbenchSnapshot } from "./api";
export interface GuestMarketFrame {
  quote?: MarketWorkbenchSnapshot["quote"];
  workbench?: Partial<MarketWorkbenchSnapshot> & { barsPatch?: { bars: MarketKlineBar[]; trimBefore?: number } };
}
/** A reconnect snapshot replaces all bars. Updates replace matching timestamps and append new tail bars. */
export function mergeGuestMarket(previous: MarketWorkbenchSnapshot | undefined, frame: GuestMarketFrame, fullSnapshot: boolean): MarketWorkbenchSnapshot | undefined {
  const workbench = frame.workbench;
  if (fullSnapshot) {
    if (!workbench?.bars || !workbench.quote) return undefined;
    return { ...workbench, quote: frame.quote ?? workbench.quote } as MarketWorkbenchSnapshot;
  }
  if (!previous) return previous;
  const { barsPatch, ...metadata } = workbench ?? {};
  let bars = metadata.bars ?? previous.bars;
  if (barsPatch) {
    const byTime = new Map(bars.map(bar => [bar.datetime, bar]));
    barsPatch.bars.forEach(bar => byTime.set(bar.datetime, bar));
    bars = [...byTime.values()].filter(bar => barsPatch.trimBefore === undefined || bar.datetime >= barsPatch.trimBefore).sort((a, b) => a.datetime - b.datetime);
  }
  return { ...previous, ...metadata, bars, quote: frame.quote ?? workbench?.quote ?? previous.quote };
}
