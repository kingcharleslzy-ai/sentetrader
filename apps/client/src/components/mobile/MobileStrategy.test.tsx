import { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { MobileStrategy, useMobileStrategies, type MobileStrategyRun } from "./MobileStrategy";

const request = vi.hoisted(() => vi.fn());
vi.mock("../../api", () => ({
  requestJson: request,
  getStrategyMonitor: (base: string, id: string) => request(`${base}/api/strategies/${id}/monitor`),
  setStrategyMonitoring: (base: string, id: string, action: string) => request(`${base}/api/strategies/${id}/monitor/${action}`, { method: "POST", body: "{}" }),
  stopStrategyMonitoring: (base: string, id: string, closePositions: boolean) => request(`${base}/api/strategies/${id}/monitor/stop`, { method: "POST", body: JSON.stringify({ closePositions }) }),
}));

const plan = { timeframeSeconds: 86400, rules: {}, questions: [] };
const items = [
  { strategyId: "lib-1", name: "均线金叉 10/30", symbol: "*", version: 1, status: "draft", planSummary: "短均线上穿长均线就做多。",
    latestBacktest: { backtestId: "b1", netProfit: 12040, maxDrawdown: 3100, winRate: 55, trades: 128 },
    definition: { schemaVersion: 2, plan, instrumentScope: { mode: "any", symbols: [] } } },
  { strategyId: "lib-2", name: "甲醇突破", symbol: "*", version: 1, status: "draft", planSummary: null, latestBacktest: null,
    definition: { schemaVersion: 2, plan, instrumentScope: { mode: "any", symbols: [] } } },
  { strategyId: "lib-3", name: "苹果区间", symbol: "*", version: 1, status: "draft", planSummary: null, latestBacktest: null,
    definition: { schemaVersion: 2, plan, instrumentScope: { mode: "any", symbols: [] } } },
];
const executions = [
  { strategyId: "run-1", name: "均线金叉 10/30", symbol: "SHFE.rb2701", version: 1, status: "monitoring", signalAction: "confirm", definition: { schemaVersion: 2, plan, libraryStrategyId: "lib-1" } },
  { strategyId: "run-2", name: "甲醇突破", symbol: "CZCE.MA701", version: 1, status: "paused", signalAction: "alert", definition: { schemaVersion: 2, plan: { ...plan, timeframeSeconds: 900 }, libraryStrategyId: "lib-2" } },
];
const monitor = (id: string) => id === "run-1" ? { strategyId: id, status: "monitoring", effectiveSignalAction: "confirm", today: { tradingDate: "2026-10-06", pnl: "860", consecutiveLosses: 0 },
  position: { direction: "long", volume: 2 }, researchClash: { stance: "偏空", direction: "long", text: "今日判断偏空，这个策略今天在做多" },
  recentAttempts: [{ attemptId: "a1", action: "open_long", volume: 2, status: "SKIPPED", cardProposalIds: ["c1"], updatedAt: "2026-10-06T01:58:00Z" }] }
  : { strategyId: id, status: "paused", signalAction: "alert", today: null, recentAttempts: [] };

function Page({ onChart = vi.fn(), onAskAi = vi.fn() }: { onChart?: (run: MobileStrategyRun) => void; onAskAi?: (text: string, focus?: unknown) => void }) {
  const strategies = useMobileStrategies({ baseUrl: "http://runtime", rootUrl: "http://root", enabled: true, detailed: true });
  const [tab, setTab] = useState<"running" | "mine">("running");
  return <MobileStrategy strategies={strategies} tab={tab} onTabChange={setTab} onChart={onChart} onAskAi={onAskAi} />;
}

let startFails = false;
beforeEach(() => {
  startFails = false;
  request.mockReset();
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/strategy-plans")) return { items, executions };
    if (url.endsWith("/monitor/stop")) return { strategyId: "run-1", status: "ready", notice: "已停止：不再判断信号。已出平仓卡，在对话里确认后才平仓。", recentAttempts: [] };
    if (url.includes("/api/strategy-labs")) return { items: [{ id: "lab-1", symbol: "SHFE.rb2701", status: "done", source: "server", createdAt: "2026-10-06T05:30:00Z",
      updatedAt: "2026-10-06T05:30:34Z", tried: 4, best: "v2", name: "均线金叉", timeframeSeconds: 86400, bestOutNet: 1240, noProfit: false }] };
    if (url.endsWith("/api/ai-execution")) return { controls: { stopStatus: null } };
    if (url.endsWith("/monitor/start") && init?.method === "POST" && startFails) {
      throw Object.assign(new Error("409"), { status: 409, detail: { detail: "strategy_transition_invalid" } });
    }
    if (init?.method === "POST") return {};
    const id = url.match(/strategies\/([^/]+)\/monitor$/)?.[1];
    return id ? monitor(id) : {};
  });
});

const posts = () => request.mock.calls.filter(([, init]) => init?.method === "POST").map(([url]) => url);

it("两个分页：正在运行（合约周期、今日盈亏、持仓、今日判断相反的黄色提示、最近信号）与我的策略（状态、一句话、最近回测）", async () => {
  render(<Page />);
  expect(await screen.findByRole("button", { name: /正在运行 2/ })).toHaveAttribute("aria-pressed", "true");
  const running = await screen.findByRole("article", { name: "均线金叉 10/30" });
  expect(running).toHaveTextContent("SHFE.rb2701 · 日线");
  await waitFor(() => expect(running).toHaveTextContent("今天 +860"));
  expect(running).toHaveTextContent("多 2 手");
  expect(within(running).getByRole("note")).toHaveTextContent("今日判断偏空，这个策略今天在做多");
  expect(running).toHaveTextContent("开多 2 手 · 出卡待确认");
  const paused = screen.getByRole("article", { name: "甲醇突破" });
  expect(paused).toHaveTextContent("15 分钟");
  expect(paused).toHaveTextContent("已暂停");
  expect(screen.queryByText("最近的回测")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /我的策略 3/ }));
  const mine = screen.getByRole("article", { name: "均线金叉 10/30" });
  expect(mine).toHaveTextContent("运行中短均线上穿长均线就做多。最近回测 +1.2 万最多亏 -3,100胜率 55%");
  expect(screen.getByRole("article", { name: "苹果区间" })).toHaveTextContent("没上线还没回测");
});

it("停止并平仓先确认，确认后请 Runtime 停止并出平仓卡（不直接下单），结果写在卡下", async () => {
  render(<Page />);
  const running = await screen.findByRole("article", { name: "均线金叉 10/30" });
  fireEvent.click(within(running).getByRole("button", { name: "停止并平仓" }));
  await waitFor(() => expect(within(running).getByRole("alertdialog")).toHaveTextContent("它还有 多 2 手，会在对话里出平仓卡，你确认了才平。"));
  expect(posts()).toEqual([]);
  await act(async () => { fireEvent.click(within(running).getByRole("button", { name: "确认停止并平仓" })); });
  const stop = request.mock.calls.find(([url]) => url === "http://runtime/api/strategies/run-1/monitor/stop")!;
  expect(JSON.parse(String(stop[1].body))).toEqual({ closePositions: true });
  expect(posts().filter(url => /confirm|trade|order/.test(String(url)))).toEqual([]);
  expect(await screen.findByText("已停止：不再判断信号。已出平仓卡，在对话里确认后才平仓。")).toBeVisible();
});

it("回撤停机（手机卡）：已停止只有“恢复运行”（发 resetDrawdown，不走普通继续）；平仓处理中两者都没有", async () => {
  const stopped = { status: "stopped", initialEquity: 1_000_000, peakEquity: 1_050_000, currentEquity: 996_000, drawdownPercent: 5.14, limitPercent: 5, canResume: true };
  let state: Record<string, unknown> = { ...stopped, status: "liquidating", canResume: false, reason: "剩余持仓 2 手待平" };
  const plain = request.getMockImplementation()!;
  request.mockImplementation(async (url: string, init?: RequestInit) =>
    url.endsWith("/strategies/run-2/monitor") ? { ...monitor("run-2"), drawdown: state } : plain(url, init));
  const view = render(<Page />);
  let paused = await screen.findByRole("article", { name: "甲醇突破" });
  await waitFor(() => expect(paused).toHaveTextContent("已停新开仓 · 平仓处理中"));
  expect(within(paused).queryByRole("button", { name: "继续运行" })).toBeNull();
  expect(within(paused).queryByRole("button", { name: "恢复运行" })).toBeNull();
  view.unmount();
  state = stopped;
  render(<Page />);
  paused = await screen.findByRole("article", { name: "甲醇突破" });
  await waitFor(() => expect(within(paused).getByRole("button", { name: "恢复运行" })).toBeEnabled());
  expect(within(paused).queryByRole("button", { name: "继续运行" })).toBeNull();
  await act(async () => { fireEvent.click(within(paused).getByRole("button", { name: "恢复运行" })); });
  await waitFor(() => expect(posts()).toContain("http://runtime/api/strategies/run-2/monitor/start"));
  const start = request.mock.calls.find(([url, init]) => url === "http://runtime/api/strategies/run-2/monitor/start" && init?.method === "POST")!;
  expect(start[1].body).toBe(JSON.stringify({ resetDrawdown: true }));
});

it("回撤停机（手机卡）：暂时算不出也用“恢复运行”，只看 canResume，先显示 Runtime 的阻塞原因", async () => {
  let state: Record<string, unknown> = { status: "unavailable", reason: "手续费暂缺", resumeBlockedReason: "本策略还有 1 笔委托结果未确认", canResume: false };
  const plain = request.getMockImplementation()!;
  request.mockImplementation(async (url: string, init?: RequestInit) =>
    url.endsWith("/strategies/run-2/monitor") ? { ...monitor("run-2"), drawdown: state } : plain(url, init));
  const view = render(<Page />);
  let card = await screen.findByRole("article", { name: "甲醇突破" });
  await waitFor(() => expect(within(card).getByRole("button", { name: "恢复运行" })).toBeDisabled());
  expect(within(card).getByTestId("strategy-drawdown")).toHaveTextContent("回撤暂时算不出本策略还有 1 笔委托结果未确认");
  view.unmount();
  state = { status: "unavailable", reason: "历史成交归属无法恢复", canResume: true };
  render(<Page />);
  card = await screen.findByRole("article", { name: "甲醇突破" });
  await waitFor(() => expect(within(card).getByRole("button", { name: "恢复运行" })).toBeEnabled());
  await act(async () => { fireEvent.click(within(card).getByRole("button", { name: "恢复运行" })); });
  const start = await waitFor(() => request.mock.calls.find(([url, init]) => url === "http://runtime/api/strategies/run-2/monitor/start" && init?.method === "POST")!);
  expect(start[1].body).toBe(JSON.stringify({ resetDrawdown: true }));
});

it("跟 AI 说……：发到对话，带上选中的策略作为上下文（默认第一条正在运行的）", async () => {
  const onAskAi = vi.fn();
  render(<Page onAskAi={onAskAi} />);
  await screen.findByRole("article", { name: "均线金叉 10/30" });
  expect(screen.getByText("关于：均线金叉 10/30")).toBeVisible();
  fireEvent.change(screen.getByRole("textbox", { name: "跟 AI 说" }), { target: { value: "把止损放宽一点再测一次" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(onAskAi).toHaveBeenCalledWith("把止损放宽一点再测一次", { kind: "strategy_edit", id: "lib-1", label: "均线金叉 10/30" });
  // Tapping another card makes it the context.
  fireEvent.click(screen.getByRole("button", { name: /^甲醇突破/ }));
  expect(screen.getByText("关于：甲醇突破")).toBeVisible();
});

it("暂停与继续运行走运行中卡同一个接口（继续先查 AI 模拟执行）；没成功时原因写在那张卡里", async () => {
  const onChart = vi.fn();
  render(<Page onChart={onChart} />);
  const running = await screen.findByRole("article", { name: "均线金叉 10/30" });
  await act(async () => { fireEvent.click(within(running).getByRole("button", { name: "暂停" })); });
  expect(posts()).toEqual(["http://runtime/api/strategies/run-1/monitor/pause"]);
  const paused = screen.getByRole("article", { name: "甲醇突破" });
  await act(async () => { fireEvent.click(within(paused).getByRole("button", { name: "继续运行" })); });
  expect(request.mock.calls.some(([url]) => url === "http://runtime/api/ai-execution")).toBe(true);
  expect(posts().at(-1)).toBe("http://runtime/api/strategies/run-2/monitor/start");
  startFails = true;
  await act(async () => { fireEvent.click(within(screen.getByRole("article", { name: "甲醇突破" })).getByRole("button", { name: "继续运行" })); });
  await waitFor(() => expect(within(screen.getByRole("article", { name: "甲醇突破" })).getByRole("alert")).toHaveTextContent("策略状态已变化，请刷新列表后重试"));
  fireEvent.click(within(screen.getByRole("article", { name: "均线金叉 10/30" })).getByRole("button", { name: "看图表" }));
  expect(onChart).toHaveBeenCalledWith(expect.objectContaining({ symbol: "SHFE.rb2701", timeframeSeconds: 86400 }));
});

it("AI 模拟执行已停止时继续运行不发启动请求，卡里说明原因", async () => {
  render(<Page />);
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/ai-execution")) return { controls: { stopStatus: "EMERGENCY_STOP" } };
    if (url.endsWith("/api/strategy-plans")) return { items: [], executions };
    if (init?.method === "POST") return {};
    const id = url.match(/strategies\/([^/]+)\/monitor$/)?.[1];
    return id ? monitor(id) : { items: [] };
  });
  const paused = await screen.findByRole("article", { name: "甲醇突破" });
  await act(async () => { fireEvent.click(within(paused).getByRole("button", { name: "继续运行" })); });
  expect(posts()).toEqual([]);
  expect(within(screen.getByRole("article", { name: "甲醇突破" })).getByRole("alert")).toHaveTextContent("AI 模拟执行已停止或急停");
});

it("档位不含策略时是有说明的空状态：这一页做什么、为什么空、去哪看档位（10-06 实测整页空白）", () => {
  const strategies = { runs: [], mine: [], loaded: true, failed: false, busy: null, errors: {}, notices: {}, reload: vi.fn(), pause: vi.fn(), resume: vi.fn(), stop: vi.fn() };
  render(<MobileStrategy strategies={strategies as never} blocked="当前档位未开通「策略管理与监控」" tab="running" onTabChange={vi.fn()} onChart={vi.fn()} onAskAi={vi.fn()} />);
  const empty = screen.getByTestId("mobile-strategy-blocked");
  expect(empty).toHaveTextContent("策略这一页看运行中的策略和我的策略");
  expect(empty).toHaveTextContent("当前档位未开通「策略管理与监控」");
  expect(empty).toHaveTextContent("档位与用量在电脑上的“设置 → 账户”里查看。");
  expect(screen.queryByText("最近的回测")).toBeNull();
});
