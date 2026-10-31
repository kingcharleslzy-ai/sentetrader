import { act, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { ConnectionProgress } from "./ConnectionProgress";
import type { RuntimeStatus } from "@ai-trader/ui-events";

const ready: RuntimeStatus = {
  mode: "local", adapter: "native-agent", accountMode: "tqkq-readonly",
  runtimeConnected: true, agentConnected: true, connected: true, version: "fixture",
  accountStatus: "normal", marketDataStatus: "normal", tradingGatewayStatus: "disabled",
  riskStatus: "normal", dataConnectionStage: "ready"
};

test("已等待从连接出问题算起：重试时各阶段在连接中与需要处理之间切换不清零，真正连上后才重新计时（#712 B7）", () => {
  vi.useFakeTimers();
  try {
    const agent = { authenticated: true, available: true, loginRequired: false };
    const failed: RuntimeStatus = { ...ready, accountStatus: "unavailable", marketDataStatus: "unavailable", dataConnectionStage: "failed" };
    const retrying = (failedAttempt: boolean) => failedAttempt
      ? <ConnectionProgress connection="disconnected" runtime={failed} hasMarket onSettings={vi.fn()} />
      : <ConnectionProgress connection="connecting" hasMarket onSettings={vi.fn()} />;
    const view = render(retrying(false));
    act(() => { vi.advanceTimersByTime(12_000); });
    expect(screen.getByText(/已等待 12 秒/)).toBeInTheDocument();
    // A failed attempt: every stage reads 需要处理, nothing is pending, so the counter hides…
    view.rerender(retrying(true));
    act(() => { vi.advanceTimersByTime(8_000); });
    expect(screen.queryByText(/已等待/)).toBeNull();
    // …and the next attempt continues from the real start, not from 0.
    view.rerender(retrying(false));
    expect(screen.getByText(/已等待 20 秒/)).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(screen.getByText(/已等待 30 秒/)).toBeInTheDocument();
    // A real connection clears it; a later problem starts a new count.
    view.rerender(<ConnectionProgress connection="connected" runtime={ready} hasMarket agent={agent} onSettings={vi.fn()} />);
    expect(screen.queryByLabelText("连接进度")).toBeNull();
    act(() => { vi.advanceTimersByTime(60_000); });
    view.rerender(retrying(false));
    expect(screen.getByText(/已等待 0 秒/)).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(3_000); });
    expect(screen.getByText(/已等待 3 秒/)).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

test("切换交易账户重新计时；同一账户重试、中途短暂没有账户信息都不清零（#712 B7）", () => {
  vi.useFakeTimers();
  try {
    const account = (id: string) => ({ id, displayName: id, role: "owner", environment: "simulation", executionOwner: true });
    const connecting = (id?: string): RuntimeStatus => ({ ...ready, accountStatus: "connecting", marketDataStatus: "connecting",
      dataConnectionStage: "authenticating", ...(id ? { account: account(id) } : {}) });
    const view = render(<ConnectionProgress connection="connected" runtime={connecting("acc-a")} hasMarket runtimeUsable onSettings={vi.fn()} />);
    act(() => { vi.advanceTimersByTime(40_000); });
    expect(screen.getByText(/已等待 40 秒/)).toBeInTheDocument();
    view.rerender(<ConnectionProgress connection="connecting" hasMarket onSettings={vi.fn()} />);
    view.rerender(<ConnectionProgress connection="connected" runtime={connecting("acc-a")} hasMarket runtimeUsable onSettings={vi.fn()} />);
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(screen.getByText(/已等待 45 秒/)).toBeInTheDocument();
    view.rerender(<ConnectionProgress connection="connected" runtime={connecting("acc-b")} hasMarket runtimeUsable onSettings={vi.fn()} />);
    expect(screen.getByText(/已等待 0 秒/)).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(2_000); });
    expect(screen.getByText(/已等待 2 秒/)).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

test("连接分阶段显示，失败可处理，全部就绪后退出", () => {
  const onSettings = vi.fn();
  const view = render(<ConnectionProgress connection="connecting" hasMarket={false} onSettings={onSettings} />);
  expect(screen.getByText("应用服务 · 连接中")).toBeVisible();
  expect(view.container.querySelectorAll(".connection-spinner")).toHaveLength(4);
  view.rerender(<ConnectionProgress connection="connected" runtime={ready} hasMarket={true} agent={{ authenticated: false, available: false, loginRequired: true }} onSettings={onSettings} />);
  expect(screen.getByText("快期账户 · 已就绪")).toBeVisible();
  expect(screen.getByText("行情 · 已就绪")).toBeVisible();
  expect(screen.getByText("AI 模型 · 需要处理")).toBeVisible();
  expect(view.container.querySelectorAll(".connection-spinner")).toHaveLength(0);
  screen.getByRole("button", { name: "查看连接设置" }).click();
  expect(onSettings).toHaveBeenCalledOnce();
  view.rerender(<ConnectionProgress connection="connected" runtime={ready} hasMarket={true} agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={onSettings} />);
  expect(screen.queryByLabelText("连接进度")).toBeNull();
  view.rerender(<ConnectionProgress connection="disconnected" runtime={{ ...ready, accountStatus: "unavailable", marketDataStatus: "unavailable", dataConnectionStage: "failed" }} hasMarket={false} onSettings={onSettings} />);
  expect(view.container.querySelectorAll(".connection-spinner")).toHaveLength(0);
  expect(screen.getByRole("button", { name: "查看连接设置" })).toBeVisible();
});

test("快期冷启动按三段显示，已完成段带用时，并按上次启动给出预计剩余", () => {
  window.localStorage.setItem("ai-trader:cold-start-seconds:tqkq-readonly", "85");
  const starting: RuntimeStatus = { ...ready, accountStatus: "connecting", marketDataStatus: "connecting",
    dataConnectionStage: "discovering_instruments", dataInitializationSeconds: 30,
    dataStartupTimings: { starting: 2, loading_sdk: 9, authenticating: 6.4, subscribing: 3 } };
  const onSettings = vi.fn();
  const view = render(<ConnectionProgress connection="connected" runtime={starting} hasMarket={false} runtimeUsable onSettings={onSettings} />);
  const phases = screen.getByRole("list", { name: "行情连接阶段" });
  expect(phases).toHaveTextContent("登录快期 · 17 秒");
  expect(phases).toHaveTextContent("合约目录 · 进行中");
  expect(phases).toHaveTextContent("首批行情");
  expect(phases.querySelectorAll('[data-state="done"]')).toHaveLength(1);
  expect(screen.getByText(/预计还需约 55 秒/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看连接设置" })).toBeNull();
  // Twice the previous duration: offer the settings entry.
  view.rerender(<ConnectionProgress connection="connected" runtime={{ ...starting, dataInitializationSeconds: 180 }} hasMarket={false} runtimeUsable onSettings={onSettings} />);
  expect(screen.getByText(/已超过上次启动用时的两倍/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看连接设置" })).toBeVisible();
  // Reaching ready records this start for the next estimate.
  view.rerender(<ConnectionProgress connection="connected" runtime={{ ...ready, dataStartupTotalSeconds: 72 }} hasMarket agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={onSettings} />);
  expect(window.localStorage.getItem("ai-trader:cold-start-seconds:tqkq-readonly")).toBe("72");
  window.localStorage.clear();
});

test("没有上次记录时给出首次启动的提示，演示账户不显示阶段", () => {
  window.localStorage.clear();
  const starting: RuntimeStatus = { ...ready, accountStatus: "connecting", marketDataStatus: "connecting", dataConnectionStage: "authenticating", dataInitializationSeconds: 5 };
  const view = render(<ConnectionProgress connection="connected" runtime={starting} hasMarket={false} runtimeUsable onSettings={vi.fn()} />);
  expect(screen.getByText(/首次启动需要 1–2 分钟/)).toBeInTheDocument();
  view.rerender(<ConnectionProgress connection="connected" runtime={{ ...starting, accountMode: "fake" }} hasMarket={false} runtimeUsable onSettings={vi.fn()} />);
  expect(screen.queryByRole("list", { name: "行情连接阶段" })).toBeNull();
});

test("行情阶段只看 Runtime 的行情状态：单个合约没有快照时不显示行情连接中", () => {
  const view = render(<ConnectionProgress connection="connected" runtime={ready} hasMarket={false} runtimeUsable agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={vi.fn()} />);
  expect(view.container).toBeEmptyDOMElement();
  view.rerender(<ConnectionProgress connection="connected" runtime={{ ...ready, marketDataStatus: "connecting", dataConnectionStage: "subscribing" }} hasMarket={false} runtimeUsable agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={vi.fn()} />);
  expect(screen.getByRole("status")).toHaveTextContent("行情 · 连接中");
});

test("休市时登录的会话不显示“连接中”：行情为休市、无冷启动阶段列表、全部就绪即收起", () => {
  const afterClose: RuntimeStatus = { ...ready, marketDataStatus: "stale", marketSessionStatus: "closed",
    dataConnectionStage: "waiting_market", dataErrorCode: "market_quote_not_ready",
    dataStartupTimings: { starting: 0.01, loading_sdk: 5.5, authenticating: 0.7, subscribing: 0.3, waiting_snapshot: 0.25, ready: 0.06 } };
  const view = render(<ConnectionProgress connection="connected" runtime={afterClose} hasMarket={true} agent={{ authenticated: false, available: false, loginRequired: true }} onSettings={vi.fn()} />);
  expect(screen.getByText("快期账户 · 已就绪")).toBeVisible();
  expect(screen.getByText("行情 · 休市")).toBeVisible();
  expect(screen.queryByLabelText("行情连接阶段")).toBeNull();
  expect(view.container.querySelectorAll(".connection-spinner")).toHaveLength(0);
  view.rerender(<ConnectionProgress connection="connected" runtime={afterClose} hasMarket={true} agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={vi.fn()} />);
  expect(screen.queryByLabelText("连接进度")).toBeNull();
});

test("快期全不可用、行情改由新浪备用时，行情一格是提示而不是转圈，说明只供看盘、恢复后自动切回", () => {
  const backup: RuntimeStatus = { ...ready, marketDataStatus: "backup", marketDataSource: "新浪（备用）", dataConnectionStage: "reconnecting" };
  const view = render(<ConnectionProgress connection="connected" runtime={backup} hasMarket={true} runtimeUsable
    agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={vi.fn()} />);
  const stage = screen.getByText("行情 · 备用（新浪，可能延迟几秒）");
  expect(stage).toHaveAttribute("data-state", "backup");
  expect(stage.querySelector(".connection-spinner")).toBeNull();
  expect(screen.getByText(/新浪备用行情.*只供看盘，不能下单；快期恢复后自动切回/)).toBeVisible();
  view.rerender(<ConnectionProgress connection="connected" runtime={ready} hasMarket={true} runtimeUsable
    agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={vi.fn()} />);
  expect(screen.queryByLabelText("连接进度")).toBeNull();
});

test("开盘时快期数据没在更新（如备用刚结束、能连的源满额或冷启动给的是旧包）：行情一格如实提示而不是转圈或已就绪", () => {
  const stale: RuntimeStatus = { ...ready, marketDataStatus: "stale" };
  const view = render(<ConnectionProgress connection="connected" runtime={stale} hasMarket={true} runtimeUsable
    agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={vi.fn()} />);
  const stage = screen.getByText("行情 · 快期数据暂时没更新");
  expect(stage).toHaveAttribute("data-state", "stale");
  expect(view.container.querySelectorAll(".connection-spinner")).toHaveLength(0);
  expect(screen.queryByText(/已等待/)).toBeNull();
  view.rerender(<ConnectionProgress connection="connected" runtime={ready} hasMarket={true} runtimeUsable
    agent={{ authenticated: true, available: true, loginRequired: false }} onSettings={vi.fn()} />);
  expect(screen.queryByLabelText("连接进度")).toBeNull();
});
