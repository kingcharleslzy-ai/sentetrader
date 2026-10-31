import { expect, test } from "vitest";
import { dailyReviewTiles } from "./DailyReview";

test("当日净盈亏在结算前写明按当时价格、结算后更正；结算后不写（2026-10-22 按结算价）", () => {
  const review = { dayPnl: 17.5, stats: { legs: 1, planned: 0, remind: 0, adhoc: 1, closes: 0, wins: 0 } } as never;
  expect(dailyReviewTiles({ ...(review as object), dayPnlBasis: "live" } as never)[0]).toMatchObject({
    label: "当日净盈亏", note: "结算前，按当时价格；结算后更正" });
  expect(dailyReviewTiles({ ...(review as object), dayPnlBasis: "settlement", dayPnl: 60.5 } as never)[0]).not.toHaveProperty("note");
});
