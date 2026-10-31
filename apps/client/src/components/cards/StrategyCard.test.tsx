import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { StrategyCard } from "./StrategyCard";

test("renders a saved strategy as a non-running draft", () => {
  render(<StrategyCard strategy={{
    turnId: "turn-1",
    strategyId: "strategy-1",
    name: "沪金短线均线策略",
    symbol: "SHFE.au2612",
    version: "草案 v1",
    status: "draft",
    summary: "AI 已整理为确定性策略草案；完成回测并确认前只保存，不执行。",
    conditions: [
      { label: "均线交叉", status: "waiting", detail: "5 根均线与 20 根均线交叉" },
      { label: "风险边界", status: "passed", detail: "最多 1 手、每日最多 3 单" }
    ],
    template: "ma_cross",
    timeframeSeconds: 180,
    direction: "both",
    riskLimits: {
      maxPosition: 1,
      maxOrdersPerDay: 3,
      stopLossTicks: 20,
      takeProfitTicks: 40,
      cooldownBars: 2
    },
    definitionDigest: "a".repeat(64),
    backtestStatus: "not_run",
    executionMode: "simulation_only"
  }} />);

  expect(screen.getByTestId("strategy-card")).toHaveTextContent("草案待回测");
  expect(screen.getByTestId("strategy-card")).toHaveTextContent("3 分钟");
  expect(screen.getByTestId("strategy-card")).toHaveTextContent("未启动交易");
  expect(screen.getByTestId("strategy-card")).not.toHaveTextContent("运行中");
});

test("renders an incomplete validation report without implying the strategy is ready", () => {
  render(<StrategyCard strategy={{
    turnId: "turn-2",
    strategyId: "strategy-2",
    name: "沪金验证草案",
    symbol: "SHFE.au2612",
    version: "草案 v1",
    status: "validating",
    summary: "基础检查已通过，仍需历史回放、样本外和成本验证；当前不能启动策略。",
    conditions: [
      { label: "策略定义完整", status: "passed", detail: "参数已固化" },
      { label: "样本外验证", status: "waiting", detail: "等待历史回放" }
    ],
    definitionDigest: "b".repeat(64),
    backtestStatus: "not_run",
    executionMode: "simulation_only",
    validationReportId: "report-12345678",
    validationStatus: "incomplete"
  }} />);

  expect(screen.getByTestId("strategy-card")).toHaveTextContent("验证中");
  expect(screen.getByTestId("strategy-card")).toHaveTextContent("证据尚未完整");
  expect(screen.getByTestId("strategy-card")).toHaveTextContent("未启动交易");
  expect(screen.getByTestId("strategy-card")).not.toHaveTextContent("验证通过·未启动");
  expect(screen.getByTestId("strategy-card")).not.toHaveTextContent("模拟运行中");
});

test("shows a failed validation check as a failure instead of a waiting item", () => {
  render(<StrategyCard strategy={{
    turnId: "turn-3",
    strategyId: "strategy-3",
    name: "未通过的策略",
    symbol: "SHFE.au2612",
    version: "草案 v1",
    status: "validating",
    summary: "历史验证未通过；策略保持验证中，不能启动。",
    conditions: [
      { label: "样本外验证", status: "failed", detail: "扣成本后没有通过" }
    ],
    validationReportId: "report-failed",
    validationStatus: "failed"
  }} />);

  expect(screen.getByTestId("strategy-card")).toHaveTextContent("×");
  expect(screen.getByTestId("strategy-card")).toHaveTextContent("未通过");
  expect(screen.getByTestId("strategy-card")).not.toHaveTextContent("验证通过·未启动");
});

test("renders signal, position and risk intent vertically without implying an order", () => {
  render(<StrategyCard strategy={{
    turnId: "turn-4",
    strategyId: "strategy-4",
    name: "螺纹钢趋势策略",
    symbol: "SHFE.rb2610",
    version: "草案 v1",
    status: "ready",
    summary: "准备开多 1 手；这里只保存风险裁剪后的意图，不会创建委托。",
    conditions: [],
    intentId: "intent-12345678",
    signalSnapshot: {
      signal: "buy",
      barTime: "1767225600000000000",
      barState: "completed",
      dataStatus: "simulated",
      definitionDigest: "d".repeat(64)
    },
    positionIntent: {
      action: "open_long",
      requestedVolume: 1,
      currentLong: 0,
      currentShort: 0,
      targetLong: 1,
      targetShort: 0,
      reason: "买入信号出现"
    },
    riskAdjustedIntent: {
      status: "allowed",
      action: "open_long",
      volume: 1,
      stopPrice: 3480,
      takeProfitPrice: 3540,
      checks: [],
      riskCheck: null,
      reason: "风险规则允许 1 手；这里只生成意图，尚未提交模拟委托。",
      executionEligible: false,
      liveTrading: "disabled"
    }
  }} />);

  const flow = screen.getByTestId("strategy-intent-flow");
  expect(flow).toHaveTextContent("当前信号");
  expect(flow).toHaveTextContent("仓位意图");
  expect(flow).toHaveTextContent("风险裁剪");
  expect(flow).toHaveTextContent("风险检查通过");
  expect(screen.getByTestId("strategy-card")).toHaveTextContent("未创建委托");
});

test("starts simulation monitoring in place and exposes pause without authorization", async () => {
  const change = vi.fn().mockResolvedValue({
    strategyId: "strategy-ready",
    status: "monitoring",
    monitorState: null,
    recentAttempts: [],
    simulationOnly: true,
    liveTrading: "disabled"
  });
  render(<StrategyCard
    strategy={{
      turnId: "turn-ready",
      strategyId: "strategy-ready",
      name: "沪金已验证策略",
      symbol: "SHFE.au2612",
      version: "草案 v1",
      status: "ready",
      summary: "验证通过，尚未启动。",
      conditions: []
    }}
    onMonitorAction={change}
    onLoadMonitor={vi.fn()}
  />);

  fireEvent.click(screen.getByRole("button", { name: "启动模拟监控" }));

  await waitFor(() => expect(change).toHaveBeenCalledWith("strategy-ready", "start"));
  expect(screen.getByTestId("strategy-card")).toHaveTextContent("监控中");
  expect(screen.getByTestId("strategy-monitor-detail")).toHaveTextContent("正常监控已完成 K 线");
  expect(screen.getByRole("button", { name: "暂停监控" })).toBeInTheDocument();
  expect(screen.queryByText(/授权/)).not.toBeInTheDocument();
});

test("refreshes a monitoring card after the global AI execution state changes", async () => {
  const snapshot = (status: "monitoring" | "paused") => ({
    strategyId: "strategy-monitoring", status, monitorState: null, recentAttempts: [],
    simulationOnly: true as const, liveTrading: "disabled" as const
  });
  const load = vi.fn()
    .mockResolvedValueOnce(snapshot("monitoring"))
    .mockResolvedValueOnce(snapshot("paused"));
  const strategy = {
    turnId: "turn-monitoring",
    strategyId: "strategy-monitoring",
    name: "沪金监控策略",
    symbol: "SHFE.au2612",
    version: "草案 v1",
    status: "monitoring" as const,
    summary: "正在监控。",
    conditions: []
  };
  const { rerender } = render(<StrategyCard
    strategy={strategy}
    refreshKey="ACTIVE:1"
    onLoadMonitor={load}
  />);
  await waitFor(() => expect(load).toHaveBeenCalledTimes(1));

  rerender(<StrategyCard
    strategy={strategy}
    refreshKey="EMERGENCY_STOP:2"
    onLoadMonitor={load}
  />);

  await waitFor(() => expect(screen.getByTestId("strategy-card")).toHaveTextContent("已暂停"));
  expect(load).toHaveBeenCalledTimes(2);
});

test("an add reads 准备加多 and a 分批止盈 close reads 分批平多 (第六批, 2026-09-28)", () => {
  const intent = (action: "open_long" | "close_long", snapshot: { add?: boolean; exitReason?: string }) => ({
    turnId: `turn-${action}`,
    strategyId: "strategy-pyramid",
    name: "沪金加仓策略",
    symbol: "SHFE.au2612",
    version: "草案 v1",
    status: "monitoring" as const,
    summary: "持仓中。",
    conditions: [],
    intentId: "intent-pyramid",
    signalSnapshot: { signal: "buy" as const, barTime: "1767225600000000000", barState: "completed" as const, dataStatus: "normal" as const,
      definitionDigest: "e".repeat(64), ...snapshot },
    positionIntent: { action, requestedVolume: 1, currentLong: 1, currentShort: 0, targetLong: 2, targetShort: 0, reason: "加仓第 1 次" },
    riskAdjustedIntent: { status: "allowed" as const, action, volume: 1, stopPrice: 820, takeProfitPrice: null, checks: [], riskCheck: null,
      reason: "允许。", executionEligible: false as const, liveTrading: "disabled" as const }
  });
  const { unmount } = render(<StrategyCard strategy={intent("open_long", { add: true })}/>);
  const flow = screen.getByTestId("strategy-intent-flow");
  expect(flow).toHaveTextContent("准备加多 1 手");
  expect(flow).not.toHaveTextContent("准备开多");
  unmount();
  render(<StrategyCard strategy={intent("close_long", { exitReason: "scale_out" })}/>);
  expect(screen.getByTestId("strategy-intent-flow")).toHaveTextContent("分批平多 1 手");
  expect(screen.getByTestId("strategy-intent-flow")).not.toHaveTextContent("优先平多");
});
