import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

const sessionState = vi.hoisted(() => ({ authRequired: false }));
vi.mock("./hooks/useRuntimeSession", () => ({
  useRuntimeSession: () => ({
    authRequired: sessionState.authRequired,
    state: {
      turns: [{
        id: "turn-trade",
        role: "assistant",
        text: "模拟交易候选",
        timestamp: "2026-08-29T12:00:00Z",
        cards: [{
          id: "proposal-stale",
          type: "trade",
          payload: {
            turnId: "turn-trade", proposalId: "proposal-stale", version: 1,
            symbol: "SHFE.au2612", name: "沪金", side: "buy_open", volume: 1,
            limitPrice: 823.5, stopPrice: 819, contractMultiplier: 1000,
            updatedAt: "2026-08-29T12:00:00Z", environment: "simulation",
            status: "approval_required", approvalId: "approval-stale",
            executionEligibleAtCreation: true,
            riskCheck: {
              passed: true, limit: 2000, estimatedLoss: 1000, reason: "通过",
              contractMultiplier: 1000, priceTick: "0.5"
            }
          }
        }]
      }],
      runtime: {
        mode: "cloud",
        adapter: "fake-agent",
        accountMode: "fake",
        runtimeConnected: true,
        agentConnected: true,
        marketDataStatus: "stale",
        accountStatus: "normal",
        tradingGatewayStatus: "read_only",
        simulationTrading: true,
        riskStatus: "normal",
        connected: true,
        version: "0.8.0",
        agentNotice: "云端 AI 尚未配置隔离执行环境。"
      }
    },
    connection: "connected",
    runtimeActivityAt: Date.now(),
    baseUrl: "https://runtime.example.cn",
    rootBaseUrl: "https://runtime.example.cn",
    submitMessage: vi.fn(),
    showBacktest: vi.fn()
  })
}));

const chartHarness = vi.hoisted(() => ({enabled:false}));
vi.mock("./components/FuturesDecisionWorkspace", async original => {
  const actual = await original<typeof import("./components/FuturesDecisionWorkspace")>();
  return {...actual, FuturesDecisionWorkspace: (props: Parameters<typeof actual.FuturesDecisionWorkspace>[0]) => chartHarness.enabled ? <div>{[60,180,300].map(period => <button key={period} className={props.duration === period ? "active" : ""} onClick={() => props.onDurationChange(period as 60 | 180 | 300)}>{period/60}分</button>)}</div> : <actual.FuturesDecisionWorkspace {...props}/>};
});
vi.mock("./strategyApi", async (original) => ({...await original<typeof import("./strategyApi")>(), setChartPeriod: vi.fn().mockResolvedValue({})}));
import { setChartPeriod } from "./strategyApi";
import { App } from "./App";

beforeEach(() => {
  vi.stubEnv("VITE_CLOUD_RUNTIME_URL", "https://runtime.example.cn");
  vi.stubEnv("VITE_DEFAULT_RUNTIME_MODE", "cloud");
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline cloud fixture")));
});

afterEach(() => {
  sessionState.authRequired = false;
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  chartHarness.enabled = false;
});

test("cloud fake mode explicitly states that the isolated agent is unavailable", async () => {
  render(<App />);
  expect(await screen.findByTestId("cloud-agent-notice")).toHaveTextContent(
    "云端 AI 尚未配置隔离执行环境。"
  );
});

test("owner-password cloud mode opens the single account login as the first screen", async () => {
  // Runtimes without guest support retain the required login fallback.
  sessionState.authRequired = true;
  render(<App />);
  expect(await screen.findByRole("dialog", { name: "登录 Sente" })).toBeVisible();
  expect(screen.queryByText(/短期访问令牌/)).not.toBeInTheDocument();
});

test("stale market data cannot promise a simulation order submission", async () => {
  render(<App />);
  // The whole App boots (identity check, guest gate) and connects first; the shared Linux CI host,
  // running every test file in parallel, has needed more than 5 s here.
  fireEvent.click(await screen.findByRole("button", { name: "打开对话" }, { timeout: 12_000 }));
  // The card's own 确认 (no second dialog, 2026-10-19): disabled, with the reason beneath it.
  expect(screen.getByTestId("confirm-trade")).toBeDisabled();
  expect(screen.getByText(/模拟下单暂时不可用/)).toBeVisible();
}, 15_000);


test("chart selection switches at once and a failed strategy rebind only warns", async () => {
  chartHarness.enabled = true;
  vi.mocked(setChartPeriod).mockClear();
  localStorage.setItem("ai-trader:selected-instrument", "SHFE.au2612");
  render(<App/>);
  await waitFor(() => expect(setChartPeriod).toHaveBeenCalled(), { timeout: 12_000 });
  let finish: () => void = () => {};
  vi.mocked(setChartPeriod).mockImplementationOnce(() => new Promise(resolve => { finish = () => resolve({}); }));
  fireEvent.click(screen.getByRole("button", {name:"3分"}));
  // Viewing a period never waits on the strategies that follow the chart (2026-09-25 holiday close).
  await waitFor(() => expect(screen.getByRole("button", {name:"3分"})).toHaveClass("active"));
  await waitFor(() => expect(vi.mocked(setChartPeriod).mock.calls.at(-1)?.[2]).toBe(180));
  await act(async () => finish());
  expect(screen.getByRole("button", {name:"3分"})).toHaveClass("active");
  vi.mocked(setChartPeriod).mockRejectedValueOnce(Object.assign(new Error("no bars"), {detail:"所需行情不足，周期尚未切换"}));
  fireEvent.click(screen.getByRole("button", {name:"5分"}));
  await screen.findByText(/图表已切换；跟随图表周期的策略暂未切换：所需行情不足/);
  expect(screen.getByRole("button", {name:"5分"})).toHaveClass("active");
}, 15_000);
