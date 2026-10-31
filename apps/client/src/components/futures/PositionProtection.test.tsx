import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { AccountPosition, AiStopLoss, MarketQuoteSnapshot } from "../../api";
import { PositionRows } from "./PositionPanels";

const position: AccountPosition = {
  symbol: "SHFE.au2612",
  direction: "long",
  volume: 3,
  todayVolume: 2,
  historicalVolume: 1,
  frozenToday: 0,
  frozenHistorical: 0,
  openPrice: 823.5,
  unrealizedPnl: 1500
};

const quote: MarketQuoteSnapshot = {
  symbol: "SHFE.au2612",
  name: "沪金2612",
  quoteDatetime: "2026-10-02T14:59:52+08:00",
  lastPrice: 824,
  changePercent: -0.68,
  bidPrice1: 823.98,
  bidVolume1: 42,
  askPrice1: 824.02,
  askVolume1: 36,
  open: 829.8,
  highest: 831.6,
  lowest: 822.4,
  volume: 128_564,
  openInterest: 182_746,
  priceTick: 0.02,
  volumeMultiple: 1000,
  upperLimit: 900,
  lowerLimit: 700
};

function stop(overrides: Partial<AiStopLoss> = {}): AiStopLoss {
  return {
    stopId: "stop-1",
    authorizationId: "auth-1",
    sessionId: "session-1",
    symbol: "SHFE.au2612",
    positionDirection: "long",
    positionBucket: "all",
    volume: 1,
    triggerPrice: 823.98,
    takeProfitPrice: 824.6,
    remainingVolume: 1,
    strategyId: null,
    source: "manual",
    priceIntent: "aggressive",
    status: "ARMED",
    createdAt: "2026-10-02T06:59:00Z",
    updatedAt: "2026-10-02T06:59:00Z",
    liveTrading: "disabled",
    ...overrides
  };
}

function rows(stops: AiStopLoss[] = [], handlers: Partial<Parameters<typeof PositionRows>[0]> = {}) {
  return render(
    <PositionRows
      positions={[position]}
      quote={quote}
      busy={false}
      limit={10}
      onClosePosition={() => undefined}
      stops={stops}
      onCreateStop={() => Promise.resolve(true)}
      onCancelStop={() => Promise.resolve(true)}
      protectionBusy={false}
      {...handlers}
    />
  );
}

describe("持仓行的保护（下单顺带保护 · 持仓补挂）", () => {
  test("已挂保护显示一行；手动来源可撤销，策略来源只读", () => {
    const onCancelStop = vi.fn(() => Promise.resolve(true));
    rows([
      stop({ stopId: "stop-manual", source: "manual", volume: 1 }),
      stop({ stopId: "stop-strategy", source: "strategy", strategyId: "strategy-1", volume: 2 })
    ], { onCancelStop });
    const lines = screen.getAllByTestId("position-protect-line");
    expect(lines[0]).toHaveTextContent("止损 823.98 / 止盈 824.60 · 已挂 1 手");
    expect(lines[1]).toHaveTextContent("策略保护 止损 823.98 / 止盈 824.60 · 已挂 2 手");
    // 策略自己的保护只能随策略走，行上不提供撤销
    expect(lines[1].querySelector('[data-testid="cancel-stop"]')).toBeNull();
    fireEvent.click(within(lines[0]).getByTestId("cancel-stop"));
    expect(onCancelStop).toHaveBeenCalledWith("stop-manual");
  });


  test("已触发、正在平仓的保护显示真实状态，不再显示“已挂”和撤销", () => {
    rows([stop({ stopId: "stop-firing", source: "manual", status: "TRIGGERING", takeProfitPrice: null })]);
    const line = screen.getByTestId("position-protect-line");
    expect(line).toHaveTextContent("止损 823.98 · 1 手 · 保护已触发，正在平仓");
    expect(line).not.toHaveTextContent("已挂");
    expect(within(line).queryByTestId("cancel-stop")).toBeNull();
  });

  test("直接保护本轮核对不了原仓时，行上说明暂不触发的原因 (#528)", () => {
    rows([stop({ stopId: "stop-blocked", source: "manual", lastError: "direct_stop_blocked:activity_unavailable" })]);
    expect(screen.getByTestId("position-protect-blocked")).toHaveTextContent("成交记录暂时读不到，暂不触发；恢复后自动核对");
  });

  test("已触发但引擎在等结算价时，行上说明在等结算价 (#750)", () => {
    rows([stop({ stopId: "stop-pending", source: "manual", lastError: "paper_settlement_pending" })]);
    expect(screen.getByTestId("position-protect-blocked")).toHaveTextContent("已触发，正在等交易所公布结算价；公布后按当时价格平仓");
  });

  test("表单按现价校验方向，提交带桶与手数，成功后收起", async () => {
    const onCreateStop = vi.fn(() => Promise.resolve(true));
    rows([], { onCreateStop });
    fireEvent.click(screen.getByTestId("row-protect-toggle"));
    // 多仓止损要低于现价 824
    fireEvent.change(screen.getByTestId("row-stop-input"), { target: { value: "825" } });
    expect(screen.getByRole("alert")).toHaveTextContent("止损要低于现价 824");
    expect(screen.getByTestId("row-protect-submit")).toBeDisabled();
    fireEvent.change(screen.getByTestId("row-stop-input"), { target: { value: "823.5" } });
    expect(screen.queryByRole("alert")).toBeNull();
    // 今 2 / 昨 1 都有：分开保护，点昨仓
    fireEvent.click(screen.getByRole("button", { name: "昨 1" }));
    fireEvent.click(screen.getByTestId("row-protect-submit"));
    await waitFor(() => expect(onCreateStop).toHaveBeenCalledWith(position, {
      positionBucket: "historical", volume: 1, triggerPrice: 823.5, takeProfitPrice: null
    }));
    await waitFor(() => expect(screen.queryByTestId("position-protect-form")).toBeNull());
  });

  test("上期所/能源中心今昨仓都有时不提供“全部”（Runtime 拒绝 close_bucket_required），默认昨仓", async () => {
    const onCreateStop = vi.fn(() => Promise.resolve(true));
    rows([], { onCreateStop });
    fireEvent.click(screen.getByTestId("row-protect-toggle"));
    const buckets = screen.getByRole("group", { name: "保护哪部分仓量" });
    expect(within(buckets).queryByRole("button", { name: /全部/ })).toBeNull();
    expect(within(buckets).getByRole("button", { name: "昨 1" })).toHaveClass("active");
    expect(screen.getByTestId("position-protect-form")).toHaveTextContent("保护 1 手");
    fireEvent.change(screen.getByTestId("row-stop-input"), { target: { value: "823.5" } });
    fireEvent.click(screen.getByTestId("row-protect-submit"));
    await waitFor(() => expect(onCreateStop).toHaveBeenCalledWith(position, {
      positionBucket: "historical", volume: 1, triggerPrice: 823.5, takeProfitPrice: null
    }));
  });

  test("止损止盈都不填时不能提交", () => {
    rows([]);
    fireEvent.click(screen.getByTestId("row-protect-toggle"));
    expect(screen.getByTestId("row-protect-submit")).toBeDisabled();
  });
});
