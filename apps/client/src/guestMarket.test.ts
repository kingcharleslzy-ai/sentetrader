// @vitest-environment node
import { mergeGuestMarket } from "./guestMarket";
import type { MarketKlineBar, MarketWorkbenchSnapshot } from "./api";
const bar = (datetime: number, close = 10): MarketKlineBar => ({ datetime, open: 10, high: 12, low: 8, close, volume: 3, openInterest: 1 });
const original = { durationSeconds:300,quote:{symbol:"SHFE.rb2610",lastPrice:10},bars:[bar(1),bar(2),bar(3)] } as MarketWorkbenchSnapshot;
it("尾K变更按时间替换/新增，并裁掉服务端已移出的头部，不重复追加", () => {
  const frame = { quote:{...original.quote,lastPrice:13},workbench:{ barsPatch:{bars:[bar(3,12),bar(4,13)],trimBefore:2} } };
  const first = mergeGuestMarket(original, frame, false)!;
  expect(first.bars.map(item => [item.datetime,item.close])).toEqual([[2,10],[3,12],[4,13]]);
  expect(first.quote.lastPrice).toBe(13);
  expect(mergeGuestMarket(first, frame, false)?.bars).toEqual(first.bars);
  expect(original.bars).toHaveLength(3);
});
it("重连snapshot替换旧行情，增量不能凭空创建不完整图表", () => {
  const reconnected = {...original,bars:[bar(20),bar(21)]};
  expect(mergeGuestMarket(original,{workbench:reconnected},true)?.bars).toEqual(reconnected.bars);
  expect(mergeGuestMarket(undefined,{workbench:{barsPatch:{bars:[bar(21)]}}},false)).toBeUndefined();
});
it("帧里的历史范围随管理员改动更新：快照和增量都带到图表（#734）", () => {
  const first = mergeGuestMarket(undefined, { workbench: { ...original, historyTradingDays: 3 } }, true)!;
  expect(first.historyTradingDays).toBe(3);
  const tightened = mergeGuestMarket(first, { workbench: { ...original, bars: [bar(3)], historyTradingDays: 1 } }, true)!;
  expect(tightened.historyTradingDays).toBe(1);
  expect(mergeGuestMarket(tightened, { workbench: { historyTradingDays: 5 } }, false)?.historyTradingDays).toBe(5);
});
