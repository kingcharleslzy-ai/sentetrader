import { fireEvent, render, screen } from "@testing-library/react";
import type { RuntimeStatus } from "@ai-trader/ui-events";
import type { TradingAccountsView } from "../../hooks/useTradingAccounts";
import { disclosureStates, resetUiSurfaces, uiSurface, uiSurfaceStates } from "../../uiSurfaces";
import { AccountSection, LIVE_TRADING_UNAVAILABLE } from "./AccountSection";

const runtime = { simulationTrading: true, accountStatus: "normal" } as RuntimeStatus;
const paper = { id: "paper:1", displayName: "本站模拟", role: "owner", environment: "simulation", executionOwner: true, providerKind: "paper" };
const accounts = (list: typeof paper[]): TradingAccountsView => ({
  directory: { accounts: list, current: list[0].id }, currentId: list[0].id, currentAccount: undefined, currentPoolStatus: undefined,
  switchAccount: vi.fn(), requestSwitch: vi.fn(), refresh: vi.fn(), accountReady: true,
});
const quota = { dailyTokenLimit: 200000, usedTokensToday: 1234, day: "2026-10-08", plan: "basic", planLabel: "基础档", voiceSecondsToday: 120, voiceSecondsPerDay: 1800 };

beforeEach(() => resetUiSurfaces());

test("only a paper account: 模拟盘 可用, the account shown as a 模拟盘 account, no 快期 anywhere", () => {
  render(<AccountSection runtime={runtime} tradingAccounts={accounts([paper])} quota={quota} canOpenPaper={false} baseUrl="https://runtime.test" />);
  expect(screen.getByText("可用")).toBeVisible();
  expect(screen.getByText("模拟盘 · 本站模拟")).toBeVisible();
  expect(screen.queryByText(/快期/)).toBeNull();
  expect(screen.queryByRole("button", { name: "开通本站模拟" })).toBeNull();
});

test("实盘 stays off: a click and the live-trading command both refuse with the reason and change nothing", () => {
  render(<AccountSection runtime={runtime} tradingAccounts={accounts([paper])} quota={quota} canOpenPaper={false} baseUrl={undefined} />);
  const toggle = screen.getByRole("switch", { name: "实盘" });
  expect(toggle).toHaveAttribute("aria-checked", "false");
  expect(screen.queryByText(LIVE_TRADING_UNAVAILABLE)).toBeNull();
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-checked", "false");
  expect(screen.getByRole("status")).toHaveTextContent(LIVE_TRADING_UNAVAILABLE);
  expect(() => uiSurface("disclosure:live-trading")?.apply?.(true)).toThrow(LIVE_TRADING_UNAVAILABLE);
  expect(disclosureStates()["live-trading"]).toBe(false);
  expect(uiSurfaceStates().liveTrading).toEqual({ state: "off", disabledReason: LIVE_TRADING_UNAVAILABLE });
});

test("档位 shows the tier name; its quotas fold open under it (by click and by the plan-quota disclosure)", () => {
  render(<AccountSection runtime={runtime} tradingAccounts={accounts([paper])} quota={quota} canOpenPaper={false} baseUrl={undefined} />);
  const tier = screen.getByRole("button", { name: "基础档：展开每日额度" });
  expect(tier).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText("今日 token")).toBeNull();
  fireEvent.click(tier);
  expect(screen.getByText("1,234 / 200,000")).toBeVisible();
  expect(screen.getByText("2 分钟 / 30 分钟")).toBeVisible();
  expect(disclosureStates()["plan-quota"]).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "基础档：收起每日额度" }));
  expect(screen.queryByText("今日 token")).toBeNull();
});

test("without a paper account the 开通本站模拟 button sits on the 交易账户 row", () => {
  const main = { id: "tqkq-sim:default", displayName: "主账户", role: "owner", environment: "simulation", executionOwner: true, providerKind: "tqkq-sidecar" };
  render(<AccountSection runtime={runtime} tradingAccounts={accounts([main])} quota={undefined} canOpenPaper baseUrl="https://runtime.test" />);
  expect(screen.getByText("模拟盘 · 主账户")).toBeVisible();
  expect(screen.getByRole("button", { name: "开通本站模拟" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: /每日额度/ })).toBeNull();
});
