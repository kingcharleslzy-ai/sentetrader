import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import type { InstrumentCatalog } from "../../api";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { ContractDrawer, contractCatalogRetryRefusal, contractCatalogState } from "./WorkspaceDrawers";

const item = (symbol: string, name: string) => ({ symbol, instrumentId: symbol.split(".")[1], instrumentName: name, exchangeId: "SHFE",
  productId: symbol.split(".")[1].replace(/\d+$/, ""), priceTick: 1, volumeMultiple: 10, deliveryYear: 2027, deliveryMonth: 1,
  expireRestDays: 90, hasNight: true, isMain: true });
const catalog = { schemaVersion: 1, capturedAt: "2026-10-11T00:00:00Z", dataStatus: "normal", isCached: false, count: 1,
  items: [item("SHFE.rb2701", "螺纹2701")] } as InstrumentCatalog;

function drawer(workspace: Partial<FuturesDecisionWorkspaceProps>, query = "") {
  const props = { connection: "connected", instrumentsLoading: false, selectedSymbol: "SHFE.rb2701", onRetryInstruments: vi.fn(), ...workspace } as FuturesDecisionWorkspaceProps;
  render(<ContractDrawer workspace={props} query={query} onQueryChange={vi.fn()} onSelect={vi.fn()} onClose={vi.fn()}
    board={undefined} product={null} onProductChange={vi.fn()} watchlist={[]} />);
  return props;
}

test("没连上应用服务时说读不到，不说没有匹配；空搜索也有空状态（#712 B2）", () => {
  drawer({ connection: "disconnected", instruments: undefined }, "rb");
  expect(screen.getByRole("status")).toHaveTextContent("合约列表暂时读不到（应用服务未连接）");
  expect(screen.queryByText("没有匹配的品种或合约")).toBeNull();
  cleanup();
  drawer({ connection: "disconnected", instruments: undefined });
  expect(screen.getByRole("status")).toHaveTextContent("合约列表暂时读不到（应用服务未连接）");
});

test("读失败后应用服务又断开：按未连接显示，不再给重试，快照与命令判断一致", () => {
  const offline = { connection: "disconnected" as const, instruments: undefined, instrumentsError: "合约目录暂时不可用" };
  drawer(offline);
  expect(contractCatalogState({ ...offline, instrumentsLoading: false })).toEqual({ status: "offline", canRetry: false });
  expect(contractCatalogRetryRefusal(contractCatalogState({ ...offline, instrumentsLoading: false }))).toBe("应用服务未连接，连上后会自动读取合约目录");
  expect(screen.getByRole("status")).toHaveTextContent("合约列表暂时读不到（应用服务未连接）");
  expect(screen.queryByText("合约目录暂时不可用")).toBeNull();
  expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
});

test("游客会话失效：不给目录重试，命令被拒并指向“重新连接”", () => {
  const expired = { guest: { expired: true } as unknown as FuturesDecisionWorkspaceProps["guest"], connection: "disconnected" as const,
    instruments: undefined, instrumentsError: "游客会话已失效，请重新连接" };
  drawer(expired);
  const state = contractCatalogState({ ...expired, instrumentsLoading: false });
  expect(state).toEqual({ status: "expired", canRetry: false });
  expect(contractCatalogRetryRefusal(state)).toBe("游客会话已失效，请用“重新连接”恢复");
  expect(screen.getByRole("status")).toHaveTextContent("合约列表暂时读不到（游客会话已失效，请重新连接）");
  expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
});

test("连上了但目录是空的：读不到并可重试", () => {
  const props = drawer({ instruments: { ...catalog, items: [], count: 0 } });
  expect(screen.getByRole("status")).toHaveTextContent("合约列表暂时读不到");
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(props.onRetryInstruments).toHaveBeenCalledOnce();
});

test("游客的目录已应答但为空：说公共行情不可用并可重试，不说应用服务未连接", () => {
  const props = drawer({ guest: {} as FuturesDecisionWorkspaceProps["guest"], connection: "connecting",
    instruments: { ...catalog, items: [], count: 0, catalogStatus: "unavailable" } });
  expect(screen.getByRole("status")).toHaveTextContent("合约列表暂时读不到（公共行情暂不可用）");
  expect(screen.queryByText(/应用服务未连接/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(props.onRetryInstruments).toHaveBeenCalledOnce();
});

test("目录在读或读失败时只显示各自的状态", () => {
  drawer({ instruments: undefined, instrumentsLoading: true }, "rb");
  expect(screen.getByText("正在读取合约目录…")).toBeVisible();
  expect(screen.queryByText(/合约列表暂时读不到|没有匹配/)).toBeNull();
  cleanup();
  // Cold start: the Runtime answers an empty catalog marked loading and the workbench polls again.
  drawer({ instruments: { ...catalog, items: [], count: 0, catalogStatus: "loading" }, instrumentsLoading: false });
  expect(screen.getByText("正在读取合约目录…")).toBeVisible();
  expect(screen.queryByText(/合约列表暂时读不到|没有匹配/)).toBeNull();
  expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
  cleanup();
  // A cold-start catalog that later failed or timed out: the error wins over its stale loading marker.
  const stale = { instruments: { ...catalog, items: [], count: 0, catalogStatus: "loading" as const }, instrumentsError: "应用服务 30 秒内没有响应，请稍后重试" };
  drawer(stale);
  expect(contractCatalogState({ ...stale, connection: "connected", instrumentsLoading: false })).toEqual({ status: "error", canRetry: true });
  expect(screen.getByText("应用服务 30 秒内没有响应，请稍后重试")).toBeVisible();
  expect(screen.getByRole("button", { name: "重试" })).toBeVisible();
  expect(screen.queryByText("正在读取合约目录…")).toBeNull();
  cleanup();
  drawer({ instruments: undefined, instrumentsError: "合约目录暂时不可用" }, "rb");
  expect(screen.getByText("合约目录暂时不可用")).toBeVisible();
  expect(screen.queryByText(/合约列表暂时读不到|没有匹配/)).toBeNull();
});

test("目录已读到：搜不到才说没有匹配，空搜索列出全部", () => {
  drawer({ instruments: catalog }, "zzz999");
  expect(screen.getByText("没有匹配的品种或合约")).toBeVisible();
  expect(screen.queryByText(/合约列表暂时读不到/)).toBeNull();
});

test("目录已读到且未搜索时列出品种", () => {
  drawer({ instruments: catalog });
  expect(screen.getByRole("button", { name: "螺纹 SHFE.rb2701" })).toBeVisible();
  expect(screen.queryByRole("status")).toBeNull();
});

test("游客列表就是它的全部目录：不再在“最近”下重复列出螺纹2701；成员照旧有“最近”（#734）", () => {
  const show = (guest: boolean) => render(<ContractDrawer query="" onQueryChange={vi.fn()} onSelect={vi.fn()} onClose={vi.fn()}
    workspace={{ connection: "connected", instrumentsLoading: false, selectedSymbol: "SHFE.rb2701", instruments: catalog, onRetryInstruments: vi.fn(),
      ...(guest ? { guest: {} } : {}) } as unknown as FuturesDecisionWorkspaceProps}
    recentSymbols={["SHFE.rb2701"]} board={undefined} product={null} onProductChange={vi.fn()} watchlist={[]} />);
  show(true);
  expect(screen.getAllByText("螺纹2701")).toHaveLength(1);
  expect(screen.queryByRole("heading", { name: "最近" })).toBeNull();
  cleanup();
  show(false);
  expect(screen.getByRole("heading", { name: "最近" })).toBeVisible();
});
