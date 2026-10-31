import type { DeliveryNotice, MarketQuoteSnapshot } from "../../api";

/**
 * 交割月提醒 (2026-10-02) 的界面短句。`tradingDaysLeft` 含今天（Runtime 按交易所日历扣节假日，
 * 日历缺失时按工作日估算），所以“N 天后”= N − 1；这些文字只提醒，不新增任何拒绝下单条件。
 */
export function deliveryBadgeText(notice: DeliveryNotice): string {
  const after = notice.tradingDaysLeft - 1;
  if (notice.kind === "expiry") return after <= 0 ? "今天到期" : `${after} 天后到期`;
  if (notice.tradingDaysLeft === 0) return "交割月";
  return after <= 0 ? "今天需平仓" : `${after} 天后需平仓`;
}

/** 开仓模式下单台的一行提示（收盘前平仓的口径与 Runtime 提醒文字一致）。 */
export function deliveryTicketHint(notice: DeliveryNotice): string {
  if (notice.kind === "expiry") {
    const after = notice.tradingDaysLeft - 1;
    return after <= 0 ? "这个合约今天到期（现金交割），注意换月" : `这个合约 ${after} 天后到期（现金交割），注意换月`;
  }
  if (notice.tradingDaysLeft === 0) return "这个合约已进入交割月，个人账户不能持仓，请改看其他月份";
  const after = notice.tradingDaysLeft - 1;
  return after <= 0 ? "这个合约今天需平仓，之后个人账户不能持仓进交割月" : `这个合约 ${after} 天后需平仓，建议换下一个主力`;
}

/** "09:00–10:15 / 10:30–11:30"；跨零点的夜盘段（结束时间小于开始）标“次日”。 */
export function formatSessionPairs(pairs: [string, string][] | undefined | null): string {
  if (!pairs?.length) return "";
  return pairs.map(([start, end]) => {
    const short = (value: string) => value.slice(0, 5) || value;
    const crossesMidnight = short(end) < short(start);
    return `${short(start)}–${crossesMidnight ? "次日" : ""}${short(end)}`;
  }).join(" / ");
}

/** 时间块的两行：日盘与夜盘（没有夜盘写明，时段缺失时两个都是空串）。 */
export function scheduleLines(schedule: MarketQuoteSnapshot["tradingSchedule"]): { day: string; night: string } {
  if (!schedule) return { day: "", night: "" };
  return { day: formatSessionPairs(schedule.day), night: formatSessionPairs(schedule.night) };
}
