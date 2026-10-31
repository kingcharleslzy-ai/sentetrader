import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import type { OrderEstimate } from "@ai-trader/ui-events";
import type { DeliveryNotice, InstrumentCatalogItem, MarketQuoteSnapshot, MarketWorkbenchSnapshot } from "../../api";
import { ContractInfoControl } from "./ContractInfoControl";
import { PositionRows } from "./PositionPanels";
import { TradeTicket } from "./TradeTicket";
import { deliveryBadgeText, deliveryTicketHint, formatSessionPairs, scheduleLines } from "./deliveryInfo";

const estimateMock = vi.fn();
vi.mock("../../api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../api")>();
  return { ...original, getOrderEstimate: vi.fn((...args: unknown[]) => estimateMock(...args)) };
});

const notice = (over: Partial<DeliveryNotice> = {}): DeliveryNotice => ({
  kind: "delivery_month", level: "warning", lastHoldDay: "2026-09-30", tradingDaysLeft: 5,
  text: "SHFE.rb2610 最迟 2026-09-30 收盘前须平仓（个人账户不能持仓进 10 月交割月）", ...over,
});

const item = (over: Partial<InstrumentCatalogItem> = {}): InstrumentCatalogItem => ({
  symbol: "SHFE.rb2610", exchangeId: "SHFE", instrumentId: "rb2610", instrumentName: "螺纹钢2610",
  productId: "rb", priceTick: 1, volumeMultiple: 10, deliveryYear: 2026, deliveryMonth: 10,
  expireRestDays: 28, hasNight: true, isMain: true, ...over,
});

const quote = (over: Partial<MarketQuoteSnapshot> = {}): MarketQuoteSnapshot => ({
  symbol: "SHFE.rb2610", name: "螺纹钢2610", quoteDatetime: "2026-10-02T14:59:52+08:00",
  lastPrice: 3500, changePercent: -0.68, bidPrice1: 3499, bidVolume1: 42, askPrice1: 3501,
  askVolume1: 36, open: 3510, highest: 3520, lowest: 3490, volume: 12_856, openInterest: 1_827,
  priceTick: 1, volumeMultiple: 10, upperLimit: 3675, lowerLimit: 3325,
  marginPerLot: 43_125, commissionPerLot: 4.02,
  tradingSchedule: { day: [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]], night: [["21:00:00", "23:00:00"]] },
  ...over,
});

function estimate(overrides: Partial<OrderEstimate> = {}): OrderEstimate {
  return {
    schemaVersion: 1, symbol: "SHFE.rb2610", side: "buy", offset: "open", volume: 1,
    price: null, marginPerLot: 43_125, commissionPerLot: 4.02, margin: 45_678, fee: 5.6,
    available: 835_570, maxOpenVolume: 18, insufficient: false, releasedMargin: null,
    basis: "保证金按交易所标准估算，实际以成交后为准。", missing: [], ...overrides,
  };
}

describe("交割提醒短句（deliveryInfo）", () => {
  test("徽标口径：含今天，N 天后 = N − 1；进入交割月写明", () => {
    expect(deliveryBadgeText(notice({ tradingDaysLeft: 5 }))).toBe("4 天后需平仓");
    expect(deliveryBadgeText(notice({ tradingDaysLeft: 1 }))).toBe("今天需平仓");
    expect(deliveryBadgeText(notice({ level: "danger", tradingDaysLeft: 0 }))).toBe("交割月");
    expect(deliveryBadgeText(notice({ kind: "expiry", tradingDaysLeft: 6 }))).toBe("5 天后到期");
    expect(deliveryBadgeText(notice({ kind: "expiry", tradingDaysLeft: 1 }))).toBe("今天到期");
  });

  test("开仓台提示：交割月与到期是两套话，级别不改变文字", () => {
    expect(deliveryTicketHint(notice({ tradingDaysLeft: 2 }))).toBe("这个合约 1 天后需平仓，建议换下一个主力");
    expect(deliveryTicketHint(notice({ tradingDaysLeft: 1 }))).toBe("这个合约今天需平仓，之后个人账户不能持仓进交割月");
    expect(deliveryTicketHint(notice({ level: "danger", tradingDaysLeft: 0 }))).toBe("这个合约已进入交割月，个人账户不能持仓，请改看其他月份");
    expect(deliveryTicketHint(notice({ kind: "expiry", tradingDaysLeft: 3 }))).toBe("这个合约 2 天后到期（现金交割），注意换月");
  });

  test("交易时段：跨零点标“次日”，没有时段或没有夜盘各自写明", () => {
    expect(formatSessionPairs([["21:00:00", "02:30:00"]])).toBe("21:00–次日02:30");
    expect(formatSessionPairs([["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"]])).toBe("09:00–10:15 / 10:30–11:30");
    expect(formatSessionPairs(undefined)).toBe("");
    expect(scheduleLines(undefined)).toEqual({ day: "", night: "" });
    expect(scheduleLines({ day: [["09:00:00", "10:15:00"]], night: [] })).toEqual({ day: "09:00–10:15", night: "" });
  });
});

describe("合约信息卡", () => {
  beforeEach(() => { estimateMock.mockReset(); });

  test("无提醒时只有 ⓘ；交割块各值显示 —，没有夜盘写明", () => {
    render(<ContractInfoControl quote={quote({ tradingSchedule: { day: [["09:00:00", "10:15:00"], ["10:30:00", "11:30:00"], ["13:30:00", "15:00:00"]], night: [] } })} />);
    expect(screen.queryByRole("button", { name: /^交割月提醒：/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "合约信息" }));
    const time = screen.getByRole("region", { name: "时间" });
    expect(time).toHaveTextContent("09:00–10:15 / 10:30–11:30 / 13:30–15:00");
    expect(screen.getByText("无夜盘")).toBeVisible();
    const delivery = screen.getByRole("region", { name: "交割" });
    expect(delivery).toBeVisible();
    expect(delivery).toHaveTextContent("交割月—");
    expect(delivery).toHaveTextContent("最后可持仓日—");
    expect(delivery).toHaveTextContent("剩余交易日—");
  });

  test("徽标带级别与完整提醒句，点击 ⓘ 打开四块信息卡", () => {
    const danger = notice({ level: "danger", tradingDaysLeft: 0, text: "SHFE.rb2610 已进入交割月（2026-10 交割），个人账户不能再持仓" });
    render(<ContractInfoControl quote={quote()} item={item({ deliveryNotice: danger })} />);
    const badge = screen.getByRole("button", { name: "交割月提醒：SHFE.rb2610 已进入交割月（2026-10 交割），个人账户不能再持仓" });
    expect(badge).toHaveAttribute("data-level", "danger");
    expect(badge).toHaveTextContent("交割月");
    fireEvent.click(screen.getByRole("button", { name: "合约信息" }));
    const dialog = screen.getByRole("dialog", { name: "合约信息" });
    expect(dialog).toBeVisible();
    // 四块都在：一手（乘数、每跳、涨跌停）、时间（日盘三段）、交割（年月、最后可持仓日、剩余、提醒句）
    const one = screen.getByRole("region", { name: "一手" });
    expect(one).toHaveTextContent("合约乘数");
    expect(one).toHaveTextContent("10");
    expect(one).toHaveTextContent("跳一下盈亏");
    expect(one).toHaveTextContent("涨停价3,675");
    expect(one).toHaveTextContent("跌停价3,325");
    const time = screen.getByRole("region", { name: "时间" });
    expect(time).toHaveTextContent("09:00–10:15 / 10:30–11:30 / 13:30–15:00");
    expect(time).toHaveTextContent("21:00–23:00");
    const delivery = screen.getByRole("region", { name: "交割" });
    expect(delivery).toHaveAttribute("data-tone", "warning");
    expect(delivery).toHaveTextContent("2026-10");
    expect(delivery).toHaveTextContent("2026-09-30");
    expect(delivery).toHaveTextContent("剩余交易日");
    expect(delivery).toHaveTextContent("个人账户不能再持仓");
  });

  test("外盘卡：没有涨跌停、日盘夜盘与国内交割规则，写快照带的 CME 时段（北京时间）与下单月份", async () => {
    estimateMock.mockResolvedValue(estimate({ symbol: "CME.ENQZ26", margin: 46_372, fee: 2.5, commissionPerLot: 2.5 }));
    render(<ContractInfoControl runtimeUrl="https://runtime.test" quote={quote({ symbol: "KQD.m@CME.NQ", name: "迷你纳指主连",
      priceTick: 0.25, volumeMultiple: 20, upperLimit: null, lowerLimit: null, tradingSchedule: { day: [["06:00", "29:00"]], night: [], dayRule: "cme" } })}
      item={item({ symbol: "KQD.m@CME.NQ", exchangeId: "KQD", deliveryYear: 0, deliveryMonth: 0, deliveryNotice: null })} />);
    fireEvent.click(screen.getByRole("button", { name: "合约信息" }));
    const one = screen.getByRole("region", { name: "一手" });
    expect(one).toHaveTextContent("美元 / 点");
    expect(one).not.toHaveTextContent("涨停");
    const time = screen.getByRole("region", { name: "时间" });
    expect(time).toHaveTextContent("交易06:00–次日 05:00");
    expect(time).toHaveTextContent("每日停盘05:00–06:00");
    expect(time).toHaveTextContent("周末休市周六 05:00–周一 06:00");
    expect(time).not.toHaveTextContent("夜盘");
    expect(screen.queryByRole("region", { name: "交割" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "合约信息" })).not.toHaveTextContent("个人账户");
    await waitFor(() => expect(screen.getByRole("region", { name: "到期" })).toHaveTextContent("下单月份CME.ENQZ26"));
    expect(screen.getByRole("region", { name: "资金" })).toHaveTextContent("每手手续费约 2.5美元 / 手 · 每边");
  });

  test("外盘谷物卡（F2）：时间块写两段交易与中间的盘中休息", () => {
    render(<ContractInfoControl quote={quote({ symbol: "KQD.m@CBOT.ZS", name: "美豆主连", upperLimit: null, lowerLimit: null,
      tradingSchedule: { day: [["08:00", "20:45"], ["21:30", "26:20"]], night: [], dayRule: "cme", cmeHours: "grains" } })}
      item={item({ symbol: "KQD.m@CBOT.ZS", exchangeId: "KQD", deliveryYear: 0, deliveryMonth: 0, deliveryNotice: null })} />);
    fireEvent.click(screen.getByRole("button", { name: "合约信息" }));
    expect(screen.getByRole("region", { name: "时间" })).toHaveTextContent(
      "交易08:00–20:45盘中休息20:45–21:30交易21:30–次日 02:20每日停盘02:20–08:00周末休市周六 02:20–周一 08:00");
  });

  test("资金块优先开仓 1 手估算，没有 Runtime 回退行情参考值", async () => {
    estimateMock.mockResolvedValue(estimate());
    const connected = render(<ContractInfoControl quote={quote()} runtimeUrl="https://runtime.test" />);
    fireEvent.click(screen.getByRole("button", { name: "合约信息" }));
    const money = screen.getByRole("region", { name: "资金" });
    await waitFor(() => expect(money).toHaveTextContent("每手保证金约 45,678"));
    expect(money).toHaveTextContent("每手手续费约 5.6");
    expect(estimateMock).toHaveBeenCalledWith("https://runtime.test",
      { symbol: "SHFE.rb2610", side: "buy", offset: "open", volume: 1 }, expect.anything());
    connected.unmount();

    estimateMock.mockClear();
    render(<ContractInfoControl quote={quote()} />);
    fireEvent.click(screen.getByRole("button", { name: "合约信息" }));
    expect(screen.getByRole("region", { name: "资金" })).toHaveTextContent("约 43,125");
    expect(estimateMock).not.toHaveBeenCalled();
  });

  test("徽标点击打开同一张卡；股指到期显示第三个周五口径", () => {
    const expiry = notice({ kind: "expiry", level: "warning", lastHoldDay: "2026-10-16", tradingDaysLeft: 11,
      text: "CFFEX.IF2610 2026-10-16（第三个周五）到期，现金交割前注意换月" });
    render(<ContractInfoControl quote={quote({ symbol: "CFFEX.IF2610", priceTick: 0.2, volumeMultiple: 300 })} item={item({ symbol: "CFFEX.IF2610", deliveryNotice: expiry, volumeMultiple: 300 })} />);
    fireEvent.click(screen.getByRole("button", { name: /^交割月提醒：/ }));
    expect(screen.getByRole("button", { name: /^交割月提醒：/ })).toHaveTextContent("10 天后到期");
    const delivery = screen.getByRole("region", { name: "交割" });
    expect(delivery).toHaveTextContent("2026-10-16");
    expect(delivery).toHaveTextContent("第三个周五 · 现金交割");
  });
});

describe("交割提醒的三个落点", () => {
  beforeEach(() => { estimateMock.mockReset(); });

  test("下单台：开仓模式显示一行提示，平仓与目标持仓不显示", () => {
    const snapshot: MarketWorkbenchSnapshot = { schemaVersion: 1, durationSeconds: 300, capturedAt: "t",
      ageSeconds: 0, dataStatus: "normal", isCached: false, liveTrading: "disabled", quote: quote(), bars: [] };
    render(<TradeTicket snapshot={snapshot} volume={1} setVolume={() => undefined} simulationEnabled
      busy={false} currentPositions={[]}
      onSubmit={() => Promise.resolve()} onTargetPosition={() => Promise.resolve(true)}
      deliveryNotice={notice({ tradingDaysLeft: 5 })} />);
    expect(screen.getByTestId("ticket-delivery-hint")).toHaveTextContent("这个合约 4 天后需平仓，建议换下一个主力");
    fireEvent.click(screen.getByRole("button", { name: "平仓" }));
    expect(screen.queryByTestId("ticket-delivery-hint")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "开仓" }));
    expect(screen.getByTestId("ticket-delivery-hint")).toBeVisible();
  });

  test("持仓行：目录条目带提醒时同一句徽标文字作文字胶囊", () => {
    render(<PositionRows positions={[{ symbol: "SHFE.rb2610", direction: "long", volume: 2, unrealizedPnl: -120 }]}
      instruments={{ schemaVersion: 1, capturedAt: "t", dataStatus: "normal", isCached: false, count: 1,
        items: [item({ deliveryNotice: notice({ level: "danger", tradingDaysLeft: 0 }) })] }}
      busy={false} limit={3} onClosePosition={() => undefined} />);
    const chip = document.querySelector<HTMLElement>(".fdw-delivery-chip")!;
    expect(chip).toBeVisible();
    expect(chip.getAttribute("data-level")).toBe("danger");
    expect(chip.textContent).toBe("交割月");
  });
});
