import type { TradeReviewData } from "./hooks/useTradeReview";
import { REVIEW_EXPORT_DAYS, reviewMarkdown } from "./dataExport";

const review = (over: Partial<TradeReviewData> = {}): TradeReviewData => ({
  trades: [], account: { equity: 1_000_000, todayNetPnl: 0, commission: 0, positionCount: 0 }, costSource: "account",
  unmatched: [], ...over });

test("#932 F6: the review export says its window and what it could not count, also when no round trip is in it", () => {
  const empty = reviewMarkdown(review({ skippedSymbols: ["DCE.m2501"], unmatched: [{}, {}] }), REVIEW_EXPORT_DAYS);
  expect(empty).toContain(`时间窗：近 ${REVIEW_EXPORT_DAYS} 天内平仓的记录`);
  expect(empty).toContain("DCE.m2501 的合约参数已不可用，其成交未计入（不限时间窗）");
  expect(empty).toContain("2 笔平仓缺少对应开仓记录（未配对），未推算其收益（不限时间窗）");
  expect(empty).toContain(`近 ${REVIEW_EXPORT_DAYS} 天暂无已平仓记录`);
  const trade = { id: "r1", symbol: "SHFE.rb2701", direction: "多", volume: 1, openTime: 1.79e18, closeTime: 1.7901e18,
    openPrice: 3000, closePrice: 3010, gross: 100, fees: null, net: null, approximate: true };
  const priced = reviewMarkdown(review({ trades: [trade] }), REVIEW_EXPORT_DAYS);
  expect(priced).toContain("| SHFE.rb2701（估算） |");
  expect(priced).toContain("按委托价推算");
  expect(priced).not.toContain("暂无已平仓记录");
});
