import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { chartSymbolOf, rememberStrategyScopes, sameContract, withForeignHalf, type SimulationOrder } from "../../api";
import { PositionsDock } from "./PositionsDock";

function dock(activeProtections: Parameters<typeof PositionsDock>[0]["activeProtections"]) {
  return render(
    <PositionsDock simulationOrders={[]} busy={false} tab="positions" onTabChange={() => undefined}
      history={false} onHistoryChange={() => undefined} fillsSource="all" onFillsSourceChange={() => undefined}
      onOrderAction={() => undefined} onClosePosition={() => undefined} onRefresh={() => undefined} onClose={() => undefined}
      activeProtections={activeProtections} protectionWarning={false} stops={[]} protectionBusy={false}
      onCreateStop={() => Promise.resolve(true)} onCancelStop={() => Promise.resolve(true)} />
  );
}

describe("持仓面板的保护列表", () => {
  test("只设止盈的保护显示止盈价，不显示 null", () => {
    dock([
      { stopId: "take-only", symbol: "SHFE.au2612", status: "ARMED", triggerPrice: null, takeProfitPrice: 830 },
      { stopId: "both", symbol: "SHFE.au2612", status: "ARMED", triggerPrice: 818, takeProfitPrice: 830 }
    ]);
    fireEvent.click(screen.getByRole("button", { name: "展开" }));
    const lines = screen.getAllByRole("listitem").filter(item => item.closest(".ai-protection-lines"));
    expect(lines[0]).toHaveTextContent("保护已挂起，等待触发 · 止盈 830");
    expect(lines[1]).toHaveTextContent("保护已挂起，等待触发 · 止损 818 / 止盈 830");
    expect(lines[0].textContent).not.toContain("null");
  });
});

describe("持仓面板的账户读数", () => {
  const account = (extra: { expirySettlements?: unknown[]; lastPrice?: number }) => ({
    account: {
      accountName: "本站模拟账户", capturedAt: "2026-11-05T01:00:00Z", ageSeconds: 0, dataStatus: "normal" as const,
      equity: 1_000_000, todayPnl: 0, marginRatio: 8, positionCount: 1,
      expirySettlements: extra.expirySettlements as never,
    },
    positions: [{ symbol: "SHFE.au2612", direction: "long" as const, volume: 1, openPrice: 906.13, lastPrice: extra.lastPrice, unrealizedPnl: -610 }],
    activity: { schemaVersion: 1 as const, capturedAt: "", ageSeconds: 0, dataStatus: "normal" as const, ordersInitialized: true,
      orders: [], trades: [], liveTrading: "disabled" as const },
    risk: { configured: false, dataStatus: "normal" as const, status: "unconfigured" as const, message: "" },
  });
  const show = (snapshot: Parameters<typeof PositionsDock>[0]["account"]) => render(
    <PositionsDock account={snapshot} simulationOrders={[]} busy={false} tab="positions" onTabChange={() => undefined}
      history={false} onHistoryChange={() => undefined} fillsSource="all" onFillsSourceChange={() => undefined}
      onOrderAction={() => undefined} onClosePosition={() => undefined} onRefresh={() => undefined} onClose={() => undefined}
      activeProtections={[]} protectionWarning={false} stops={[]} protectionBusy={false}
      onCreateStop={() => Promise.resolve(true)} onCancelStop={() => Promise.resolve(true)} />
  );

  test("降级持仓保留账本手数并标明价格与盈亏来自最近快照", () => {
    show({ ...account({ lastPrice: 897 }), positionsStatus: {
      dataStatus: "unavailable", snapshotAvailable: true, isCached: true, ageSeconds: 20,
    } });
    expect(screen.getByRole("status")).toHaveTextContent("账户数据中断，价格与盈亏沿用最近快照");
    expect(document.querySelector(".fdw-row-price")).toHaveTextContent("906.13 → 897.00");
  });

  test("首次快照尚未到达时不显示空仓结论或零持仓计数", () => {
    show({ ...account({}), positions: [], positionsStatus: {
      dataStatus: "unavailable", snapshotAvailable: false, isCached: true, ageSeconds: 0,
    } });
    expect(screen.getByRole("status")).toHaveTextContent("持仓快照尚未就绪，正在等待更新");
    expect(screen.getByRole("button", { name: "持仓 (—)" })).toBeTruthy();
    expect(screen.queryByText("当前没有持仓")).toBeNull();
    expect(within(screen.getByTestId("positions-kpi")).getByText("权益").nextElementSibling).toHaveTextContent("—");
    expect(within(screen.getByTestId("positions-kpi")).getByText("浮动盈亏").nextElementSibling).toHaveTextContent("—");
  });

  test("不是当前图表合约的持仓行也显示开仓均价 → 现价（#803）", () => {
    show(account({ lastPrice: 897 }));
    expect(document.querySelector(".fdw-row-price")).toHaveTextContent("906.13 → 897.00");
  });

  test("挂起中的到期结算逐个列出（#527）", () => {
    show(account({ expirySettlements: [{ symbol: "SHFE.au2610", finalDay: "2026-10-15", status: "estimated",
      statusLabel: "暂估，待核", estimate: { price: 822.5 } }] }));
    expect(screen.getByTestId("expiry-settlements")).toHaveTextContent("SHFE.au2610 到期结算 · 暂估，待核 · 暂估价 822.5");
  });
});

describe("外盘仓合并显示（所有者 2026-10-21：迷你纳指下单成功，持仓里却没有）", () => {
  const own = {
    account: { accountName: "本站模拟账户", capturedAt: "2026-10-21T01:00:00Z", ageSeconds: 0, dataStatus: "normal" as const,
      equity: 1_000_000, todayPnl: 0, marginRatio: 8, positionCount: 1 },
    positions: [{ symbol: "SHFE.au2612", direction: "long" as const, volume: 1, openPrice: 906.13, unrealizedPnl: -610 }],
    activity: { schemaVersion: 1 as const, capturedAt: "", ageSeconds: 0, dataStatus: "normal" as const, ordersInitialized: true,
      orders: [], trades: [], liveTrading: "disabled" as const },
    risk: { configured: false, dataStatus: "normal" as const, status: "unconfigured" as const, message: "" },
  };
  const usd = "paperusd:abc";
  const foreignOrder = { accountId: usd, clientOrderId: "AIT-usd", request: { symbol: "CME.ENQZ26", direction: "BUY", offset: "OPEN", volume: 1, limitPrice: 29900 },
    remoteStatus: "ALIVE", cancelPhase: "NONE", submitPhase: "SUBMITTED", displayStatus: "已受理" } as unknown as SimulationOrder;
  const merged = withForeignHalf(own, { accountId: usd, account: { ...own.account, accountName: "本站模拟 · 美元" },
    positions: [{ symbol: "CME.ENQZ26", direction: "long", volume: 1, openPrice: 29936.5, lastPrice: 29936, unrealizedPnl: -10,
      accountId: usd, currency: "USD", instrumentName: "迷你纳指1002612" }],
    orders: [foreignOrder] })!;
  const show = (tab: "positions" | "orders", onClosePosition = () => undefined) => render(
    <PositionsDock account={merged} simulationOrders={[foreignOrder]} busy={false} tab={tab} onTabChange={() => undefined}
      history={false} onHistoryChange={() => undefined} fillsSource="all" onFillsSourceChange={() => undefined}
      onOrderAction={() => undefined} onClosePosition={onClosePosition} onRefresh={() => undefined} onClose={() => undefined}
      activeProtections={[]} protectionWarning={false} stops={[]} protectionBusy={false}
      onCreateStop={() => Promise.resolve(true)} onCancelStop={() => Promise.resolve(true)} />
  );

  test("美元子账户的仓在人民币账户的持仓面板里单独一组、按美元写，人民币浮动盈亏不掺美元", () => {
    const onClose = vi.fn();
    show("positions", onClose);
    expect(screen.getByRole("button", { name: "持仓 (2)" })).toBeInTheDocument();
    expect(screen.getByTestId("foreign-positions-head")).toHaveTextContent("外盘 · 美元浮盈 -10 美元");
    const row = document.querySelector('[data-currency="USD"]')!;
    expect(row).toHaveTextContent("迷你纳指1002612");
    expect(row).toHaveTextContent("浮盈 -10 美元");
    // 两本账（2026-10-22）：外盘行也能挂保护（Runtime 把它挂在美元子账户上）
    expect(screen.getByRole("button", { name: "给 CME.ENQZ26 多仓挂保护" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "给 SHFE.au2612 多仓挂保护" })).toBeInTheDocument();
    // 两行六格：人民币一行、美元一行，各算各的，不相加
    expect(screen.getAllByText(/^(人民币 · 国内|美元 · 外盘)$/).map(node => node.textContent)).toEqual(["人民币 · 国内", "美元 · 外盘"]);
    expect(within(screen.getByTestId("positions-kpi")).getByText("浮动盈亏").nextElementSibling).toHaveTextContent("-610");
    expect(within(screen.getByTestId("positions-kpi-usd")).getByText("浮动盈亏").nextElementSibling).toHaveTextContent("-10");
    fireEvent.click(screen.getByRole("button", { name: "平仓 CME.ENQZ26 多仓" }));
    expect(onClose).toHaveBeenCalledWith(expect.objectContaining({ symbol: "CME.ENQZ26", accountId: usd }));
  });

  test("美元子账户的在途委托在委托页标“美元”", () => {
    show("orders");
    expect(document.querySelector('[data-order-id="AIT-usd"]')).toHaveTextContent("CME.ENQZ26美元");
  });

  test("外盘月份合约的持仓认外盘主连图表；平仓切到它品种的主连图表", () => {
    rememberStrategyScopes([{ symbol: "KQD.m@CME.NQ", strategyScope: "CME.ENQ" }, { symbol: "KQD.m@CME.MNQ", strategyScope: "CME.MNQ" }]);
    expect(sameContract("CME.ENQZ26", "KQD.m@CME.NQ")).toBe(true);
    expect(sameContract("CME.MNQZ26", "KQD.m@CME.NQ")).toBe(false);
    expect(sameContract("SHFE.au2612", "SHFE.au2612")).toBe(true);
    expect(chartSymbolOf("CME.ENQZ26")).toBe("KQD.m@CME.NQ");
    expect(chartSymbolOf("SHFE.au2612")).toBe("SHFE.au2612");
  });
});
