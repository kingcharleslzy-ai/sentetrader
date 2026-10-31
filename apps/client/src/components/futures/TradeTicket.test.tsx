import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import type { OrderEstimate } from "@ai-trader/ui-events";
import { rememberStrategyScopes, type AccountPosition, type MarketWorkbenchSnapshot } from "../../api";
import { TradeTicket } from "./TradeTicket";
import type { ManualOrderOutcome } from "../../hooks/useSimulationOrders";

/** A confirmed submission whose order is working (what the ticket clears its protection on). */
const ACCEPTED: ManualOrderOutcome = { confirmed: true, accepted: true, orders: [], skipped: [] };

vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => ({ label: "main" }) }));

const estimateMock = vi.fn();
vi.mock("../../api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../api")>();
  return { ...original, getOrderEstimate: vi.fn((...args: unknown[]) => estimateMock(...args)) };
});

const snapshot: MarketWorkbenchSnapshot = {
  schemaVersion: 1,
  durationSeconds: 300,
  capturedAt: "2026-10-02T06:59:52Z",
  ageSeconds: 0.3,
  dataStatus: "normal",
  isCached: false,
  liveTrading: "disabled",
  quote: {
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
  },
  bars: []
};

function estimate(overrides: Partial<OrderEstimate> = {}): OrderEstimate {
  return {
    schemaVersion: 1, symbol: "SHFE.au2612", side: "buy", offset: "open", volume: 1,
    price: 824, marginPerLot: 82_350, commissionPerLot: 24.71, margin: 82_350,
    fee: 24.71, available: 835_570, maxOpenVolume: 10, insufficient: false,
    releasedMargin: null, basis: "保证金按交易所标准估算，实际以成交后为准。", missing: [],
    ...overrides
  };
}

function ticket(runtimeUrl?: string, selectedPosition?: AccountPosition) {
  return render(
    <TradeTicket
      snapshot={snapshot}
      runtimeUrl={runtimeUrl}
      selectedPosition={selectedPosition}
      volume={1}
      setVolume={() => undefined}
      simulationEnabled
      busy={false}
      currentPositions={[]}
      onSubmit={() => Promise.resolve()}
      onTargetPosition={() => Promise.resolve(true)}
    />
  );
}

describe("TradeTicket 下单前的“钱”估算行", () => {
  beforeEach(() => {
    estimateMock.mockReset();
  });

  test("开仓模式显示保证金约 · 手续费约 · 可开手数", async () => {
    estimateMock.mockResolvedValue(estimate());
    ticket("https://runtime.test");
    const line = screen.getByTestId("ticket-estimate");
    await waitFor(() => expect(line).toHaveTextContent("保证金约 82,350 · 手续费约 24.71 · 可开 10 手"));
    expect(estimateMock).toHaveBeenCalledWith("https://runtime.test", {
      symbol: "SHFE.au2612", side: "buy", offset: "open", volume: 1, price: 824
    }, expect.anything());
    expect(line).not.toHaveAttribute("data-tone", "warning");
  });

  test("资金不够开 N 手时警示但不禁用下单按钮", async () => {
    estimateMock.mockResolvedValue(estimate({ insufficient: true, maxOpenVolume: 0 }));
    ticket("https://runtime.test");
    const line = screen.getByTestId("ticket-estimate");
    await waitFor(() => expect(line).toHaveTextContent("可用资金不够开 1 手"));
    expect(line).toHaveAttribute("data-tone", "warning");
    expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeEnabled();
  });

  test("平仓模式显示释放保证金约", async () => {
    estimateMock.mockResolvedValue(estimate({ offset: "close", side: "sell", margin: 0, releasedMargin: 82_350, maxOpenVolume: null, insufficient: false }));
    ticket("https://runtime.test");
    fireEvent.click(screen.getByRole("button", { name: "平仓" }));
    await waitFor(() => expect(screen.getByTestId("ticket-estimate")).toHaveTextContent("释放保证金约 82,350"));
  });

  test("平今/平昨分开报的合约按所选桶估算释放保证金", async () => {
    estimateMock.mockResolvedValue(estimate({ offset: "close", side: "sell", margin: 0, releasedMargin: 82_350, maxOpenVolume: null, insufficient: false }));
    const position: AccountPosition = {
      symbol: "SHFE.au2612", direction: "long", volume: 3, todayVolume: 2, historicalVolume: 1,
      frozenToday: 0, frozenHistorical: 0, openPrice: 823.5, unrealizedPnl: 1500
    };
    ticket("https://runtime.test", position);
    fireEvent.click(screen.getByRole("button", { name: "平仓" }));
    // 自动 (the default): no bucket — the Runtime estimates with the same 平今平昨 allocation it plans with.
    await waitFor(() => expect(estimateMock).toHaveBeenLastCalledWith("https://runtime.test",
      expect.objectContaining({ offset: "close" }), expect.anything()));
    expect(estimateMock.mock.lastCall?.[1]).not.toHaveProperty("positionBucket");
    fireEvent.click(screen.getByRole("button", { name: "平昨 1" }));
    await waitFor(() => expect(estimateMock).toHaveBeenLastCalledWith("https://runtime.test",
      expect.objectContaining({ offset: "close", positionBucket: "historical" }), expect.anything()));
  });

  test("平仓页默认自动：只发用户选的内容，不在客户端定平今平昨；点了平今/平昨原样发出（意图重构第 4 步）", async () => {
    const onSubmit = vi.fn<(request: unknown) => Promise<ManualOrderOutcome>>(() => Promise.resolve(ACCEPTED));
    const position: AccountPosition = {
      symbol: "SHFE.au2612", direction: "long", volume: 3, todayVolume: 2, historicalVolume: 1,
      frozenToday: 0, frozenHistorical: 0, openPrice: 823.5, unrealizedPnl: 1500
    };
    const view = (volume: number) => <TradeTicket snapshot={snapshot} selectedPosition={position} volume={volume} setVolume={() => undefined}
      simulationEnabled busy={false} currentPositions={[position]} onSubmit={onSubmit}
      onTargetPosition={() => Promise.resolve(true)} />;
    const { rerender } = render(view(3));
    fireEvent.click(screen.getByRole("button", { name: "平仓" }));
    expect(screen.getByRole("button", { name: "自动" })).toHaveAttribute("aria-pressed", "true");
    // 3 手跨今昨：自动下可平 3 手（以前默认平今，只能平 2 手，按钮是灰的），一次发出、不带今昨。
    const close = () => screen.getByRole("button", { name: /(卖出平仓|买入平仓)/ });
    expect(close()).toHaveTextContent("可平 3");
    fireEvent.click(close());
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.lastCall?.[0]).toEqual(expect.objectContaining({ side: "sell_close", volume: 3, positionBucket: undefined }));
    fireEvent.click(screen.getByRole("button", { name: "平昨 1" }));
    expect(close()).toBeDisabled(); // 3 手 > 昨仓 1 手
    rerender(view(1));
    fireEvent.click(close());
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(onSubmit.mock.lastCall?.[0]).toEqual(expect.objectContaining({ side: "sell_close", volume: 1, positionBucket: "historical" }));
  });

  test("缺数据显示 —，没有 Runtime 连接时不请求", () => {
    estimateMock.mockResolvedValue(estimate({ margin: null, fee: null, maxOpenVolume: null }));
    ticket();
    expect(screen.getByTestId("ticket-estimate").textContent).toBe("—");
    expect(estimateMock).not.toHaveBeenCalled();
  });
});

describe("TradeTicket 下单顺带保护", () => {
  function protectionTicket(overrides: Partial<Parameters<typeof TradeTicket>[0]> = {}) {
    return render(
      <TradeTicket
        snapshot={snapshot}
        volume={1}
        setVolume={() => undefined}
        simulationEnabled
        busy={false}
        currentPositions={[]}
        onSubmit={() => Promise.resolve(ACCEPTED)}
        onTargetPosition={() => Promise.resolve(true)}
        {...overrides}
      />
    );
  }

  test("填了止损/止盈显示约亏/约赚，随单提交后清空", async () => {
    const onSubmit = vi.fn(() => Promise.resolve(ACCEPTED));
    protectionTicket({ onSubmit });
    fireEvent.change(screen.getByTestId("stop-loss-input"), { target: { value: "823.98" } });
    fireEvent.change(screen.getByTestId("take-profit-input"), { target: { value: "824.6" } });
    // (委托价 824 − 823.98) × 1000 × 1 手，(824.6 − 824) × 1000 × 1 手
    expect(screen.getByTestId("ticket-protection-estimate")).toHaveTextContent("止损约亏 20 元 · 止盈约赚 600 元");
    fireEvent.click(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      protection: { stopLossPrice: 823.98, takeProfitPrice: 824.6 }
    })));
    // 提交成功即清空（number 输入为空时 jest-dom 读到 null，直接看 .value）
    await waitFor(() => expect((screen.getByTestId("stop-loss-input") as HTMLInputElement).value).toBe(""));
    expect(screen.queryByTestId("ticket-protection-estimate")).toBeNull();
  });

  test("止损方式：填了止损才出现，追踪与浮盈 1R 后移到成本随单提交，提交后回到固定", async () => {
    const onSubmit = vi.fn(() => Promise.resolve(ACCEPTED));
    protectionTicket({ onSubmit });
    expect(screen.queryByRole("combobox", { name: "止损方式" })).toBeNull();
    fireEvent.change(screen.getByTestId("stop-loss-input"), { target: { value: "823.9" } });
    fireEvent.change(screen.getByRole("combobox", { name: "止损方式" }), { target: { value: "ticks" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "追踪距离" }), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "浮盈 1R 后移到成本" }));
    fireEvent.click(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      protection: { stopLossPrice: 823.9, takeProfitPrice: undefined, trailing: expect.objectContaining({ mode: "ticks", value: 5 }), breakevenAtR: true }
    })));
    await waitFor(() => expect(screen.queryByRole("combobox", { name: "止损方式" })).toBeNull());
  });

  test("方向不对显示中文原因并禁用确认；换边重新校验", () => {
    protectionTicket();
    fireEvent.change(screen.getByTestId("stop-loss-input"), { target: { value: "825" } });
    const line = screen.getByTestId("ticket-protection-estimate");
    expect(line).toHaveTextContent("止损要低于委托价 824");
    expect(line).toHaveAttribute("data-tone", "warning");
    expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
    // 空头的止损在委托价上方：825 对卖空合法，换边即重新校验
    fireEvent.click(screen.getByRole("button", { name: "卖空" }));
    expect(screen.getByTestId("ticket-protection-estimate")).toHaveTextContent("止损约亏 1,000 元");
    expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeEnabled();
  });

  test("不在最小变动价位上的价格显示原因并禁用确认", () => {
    protectionTicket();
    fireEvent.change(screen.getByTestId("stop-loss-input"), { target: { value: "823.99" } });
    expect(screen.getByTestId("ticket-protection-estimate")).toHaveTextContent("止损 823.99 不在最小变动价位上");
    expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  });

  test("不填就没有 protection；图表草稿的止损候选一键填入", () => {
    const onSubmit = vi.fn(() => Promise.resolve(ACCEPTED));
    protectionTicket({
      onSubmit,
      draft: { id: 1, side: "buy_open", volume: 1, limitPrice: 824, stopPrice: 823.9, source: "context-menu" }
    });
    fireEvent.click(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ protection: undefined }));
    fireEvent.click(screen.getByTestId("fill-stop-from-draft"));
    expect(screen.getByTestId("stop-loss-input")).toHaveValue(823.9);
  });
});

describe("TradeTicket 手数 / 委托价 / 目标持仓的输入校验（#789）", () => {
  function StatefulTicket({ quote }: {
    quote?: Partial<MarketWorkbenchSnapshot["quote"]>;
  }) {
    const [volume, setVolume] = useState(1);
    return <TradeTicket snapshot={{ ...snapshot, quote: { ...snapshot.quote, ...quote } }} volume={volume} setVolume={setVolume}
      simulationEnabled busy={false} currentPositions={[]}
      onSubmit={() => Promise.resolve(ACCEPTED)} onTargetPosition={() => Promise.resolve(true)} />;
  }
  const volumeBox = () => screen.getByLabelText("模拟委托手数") as HTMLInputElement;
  const confirm = () => screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ });

  test("删空手数不弹回 1：再输 5 得 5，确认按钮跟着 5 手", () => {
    render(<StatefulTicket />);
    fireEvent.change(volumeBox(), { target: { value: "" } });
    expect(volumeBox().value).toBe("");
    expect(screen.getByTestId("ticket-estimate")).toHaveTextContent("请输入手数");
    expect(confirm()).toBeDisabled();
    fireEvent.change(volumeBox(), { target: { value: "5" } });
    expect(volumeBox().value).toBe("5");
    expect(confirm()).toBeEnabled();
    expect(confirm()).toHaveTextContent("买多开仓 5 手");
  });

  test.each(["0", "-1", "1.5", "1001"])("手数 %s 原样留着、就地说明原因并禁用确认", typed => {
    render(<StatefulTicket />);
    fireEvent.change(volumeBox(), { target: { value: typed } });
    expect(volumeBox().value).toBe(typed);
    expect(volumeBox()).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("ticket-estimate")).toHaveTextContent("手数须是 1–1000 手的整数");
    expect(confirm()).toBeDisabled();
    // 加一从最后一个合法手数走，框里换成新值
    fireEvent.click(screen.getByRole("button", { name: "手数加一" }));
    expect(volumeBox().value).toBe("2");
  });

  test("确认文字跟随方向、手数和价格类型", () => {
    render(<StatefulTicket />);
    expect(confirm()).toHaveTextContent("买多开仓 1 手 · 824.00");
    fireEvent.click(screen.getByRole("button", {name: "卖空"}));
    fireEvent.change(volumeBox(), {target:{value:"2"}});
    fireEvent.click(screen.getByRole("button", {name:"对手价"}));
    expect(confirm()).toHaveTextContent("卖空开仓 2 手 · 对手价");
    fireEvent.click(screen.getByRole("button", {name:"最新价"}));
    expect(confirm()).toHaveTextContent("卖空开仓 2 手 · 最新价");
  });

  test("交易所日内开仓限额压低开仓手数上限", () => {
    render(<StatefulTicket quote={{ openLimit: 50 }} />);
    fireEvent.change(volumeBox(), { target: { value: "60" } });
    expect(screen.getByTestId("ticket-estimate")).toHaveTextContent("手数须是 1–50 手的整数");
    expect(confirm()).toBeDisabled();
  });

  test("规则变宽让框里的数合规时，提交的手数跟着框里的数（开仓超限额后切到平仓）", () => {
    render(<StatefulTicket quote={{ openLimit: 50 }} />);
    fireEvent.change(volumeBox(), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: "平仓" }));
    expect(volumeBox().value).toBe("60");
    expect(volumeBox()).not.toHaveAttribute("aria-invalid");
    // 加一从 60 走（父组件的手数已同步成 60），不是从开仓时最后合规的 1 走
    fireEvent.click(screen.getByRole("button", { name: "手数加一" }));
    expect(volumeBox().value).toBe("61");
  });

  test("委托价超出涨跌停时说明原因并禁用确认", () => {
    render(<StatefulTicket />);
    const price = screen.getByLabelText("模拟委托价") as HTMLInputElement;
    fireEvent.change(price, { target: { value: "99999" } });
    expect(screen.getByTestId("ticket-estimate")).toHaveTextContent("委托价须在跌停 700.00 到涨停 900.00 之间");
    expect(confirm()).toBeDisabled();
    fireEvent.change(price, { target: { value: "824.02" } });
    expect(confirm()).toBeEnabled();
  });

  test("止损填 -1 不当作不设：说明原因并禁用确认", () => {
    render(<StatefulTicket />);
    fireEvent.change(screen.getByTestId("stop-loss-input"), { target: { value: "-1" } });
    expect(screen.getByTestId("ticket-protection-estimate")).toHaveTextContent("止损须大于 0");
    expect(confirm()).toBeDisabled();
  });

  test("目标净持仓删空不变成 0，越界说明原因并禁用生成", () => {
    render(<StatefulTicket />);
    fireEvent.click(screen.getByRole("button", { name: "目标持仓" }));
    const target = screen.getByTestId("target-net-input") as HTMLInputElement;
    fireEvent.change(target, { target: { value: "" } });
    expect(target.value).toBe("");
    expect(screen.getByTestId("target-plan-summary")).toHaveTextContent("请输入目标净持仓");
    expect(screen.getByTestId("generate-target-card")).toBeDisabled();
    fireEvent.change(target, { target: { value: "1000" } });
    expect(screen.getByTestId("target-plan-summary")).toHaveTextContent("目标净持仓须是 -999–999 手的整数");
    fireEvent.change(target, { target: { value: "-2" } });
    expect(screen.getByTestId("target-plan-summary")).toHaveTextContent("目标 -2 手");
    expect(screen.getByTestId("generate-target-card")).toBeEnabled();
  });
});

describe("TradeTicket 仅行情的外盘品种（外盘代码统一 2026-10-21）", () => {
  test("伦铜这类只接了行情的外盘主连：下单按钮置灰并写明原因，不等提交后才被拒", () => {
    rememberStrategyScopes([{ symbol: "KQD.m@COMEX.GC", strategyScope: "CME.GCE" }, { symbol: "KQD.m@LMEOTC.CA" }]);
    const onSubmit = vi.fn(() => Promise.resolve(ACCEPTED));
    render(<TradeTicket snapshot={{ ...snapshot, quote: { ...snapshot.quote, symbol: "KQD.m@LMEOTC.CA", name: "伦铜" } }} volume={1} setVolume={() => undefined}
      simulationEnabled busy={false} currentPositions={[]}
      onSubmit={onSubmit} onTargetPosition={() => Promise.resolve(true)} />);
    expect(screen.getByTestId("ticket-estimate")).toHaveTextContent("伦铜只接了参考行情，没有可下单的月份合约");
    expect(screen.getByRole("button", { name: /^(买多开仓|卖空开仓)/ })).toBeDisabled();
  });

  test("可交易的外盘主连照常能下单", () => {
    rememberStrategyScopes([{ symbol: "KQD.m@COMEX.GC", strategyScope: "CME.GCE" }]);
    render(<TradeTicket snapshot={{ ...snapshot, quote: { ...snapshot.quote, symbol: "KQD.m@COMEX.GC", name: "COMEX黄金" } }} volume={1} setVolume={() => undefined}
      simulationEnabled busy={false} currentPositions={[]}
      onSubmit={() => Promise.resolve(ACCEPTED)} onTargetPosition={() => Promise.resolve(true)} />);
    expect(screen.getByTestId("ticket-estimate")).not.toHaveTextContent("只接了参考行情");
    expect(screen.getByRole("button", { name: /^买多开仓/ })).toBeEnabled();
  });
});

// A UI price command may open a previously unmounted phone ticket (whose normal default is opponent).
test("手机下单台首次挂载时，显式候选价也填入限价而不提交", () => {
  const submit = vi.fn();
  render(<TradeTicket snapshot={snapshot} selectedPrice={824.06} variant="mobile" volume={1} setVolume={() => undefined}
    simulationEnabled busy={false} currentPositions={[]} onSubmit={submit} onTargetPosition={() => Promise.resolve(true)} />);
  expect(screen.getByLabelText("模拟委托价")).toHaveValue(824.06);
  expect(screen.getByRole("button", {name:"限价"})).toHaveClass("active");
  expect(submit).not.toHaveBeenCalled();
});
