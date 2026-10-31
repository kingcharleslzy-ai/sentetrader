import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import type { Lab } from "../../strategyLab/labApi";
import { MobileLab } from "./MobileLab";

const request = vi.hoisted(() => vi.fn());
vi.mock("../../api", () => ({ requestJson: request }));

const seg = (trades: number, net: number) => ({ trades, wins: 0, winRate: null, net, avgNet: null, maxDrawdown: 0 });
const curve = [{ time: 0, equity: 1_000_000 }, { time: 50, equity: 1_004_000 }, { time: 100, equity: 1_006_000 }];
const lab = (chosen: Lab["chosen"]): Lab => ({
  id: "lab-1", symbol: "SHFE.rb2701", status: "done", source: "server", createdAt: "2026-10-06T05:30:00Z", updatedAt: "2026-10-06T05:30:34Z",
  request: { symbol: "SHFE.rb2701", tradingDays: 60, slippageTicks: 1 }, skipped: null,
  variants: [{ id: "v1", label: "均线金叉 · 快线 5 / 慢线 20", plan: { timeframeSeconds: 86400 } }, { id: "v2", label: "均线金叉 · 快线 10 / 慢线 30", plan: { timeframeSeconds: 86400 } }],
  results: [
    { variantId: "v1", label: "均线金叉 · 快线 5 / 慢线 20", summary: { netProfit: 21000, maxDrawdown: 5200, trades: 41 }, inSample: seg(30, 25800), outSample: seg(11, -4800), splitTime: 70, equityCurve: curve },
    { variantId: "v2", label: "均线金叉 · 快线 10 / 慢线 30", summary: { netProfit: 18400, maxDrawdown: 4100, trades: 38 }, inSample: seg(27, 12000), outSample: seg(11, 6200), splitTime: 70, equityCurve: curve },
  ],
  ranking: { tried: 2, best: "v1", items: [{ variantId: "v1", isRank: 1, oosRank: 2, decay: -0.4, flags: ["best", "overfit", "negative_oos"] },
    { variantId: "v2", isRank: 2, oosRank: 1, decay: 0.8, flags: [] }] },
  chosen,
});

let current: Lab;
beforeEach(() => {
  current = lab(null);
  request.mockReset();
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/strategy-labs/lab-1/choose")) {
      current = lab({ variantId: "v2", strategyId: "s-9", backtestSummary: {}, at: "2026-10-06T05:31:00Z" });
      return { strategyId: "s-9", backtest: {} };
    }
    if (url.endsWith("/api/strategy-labs/lab-1")) return current;
    if (url.includes("/api/strategy-labs?limit=1")) return { items: [{ id: "lab-1" }] };
    if (url.endsWith("/api/ai-execution")) return { controls: { stopStatus: null } };
    if (url.endsWith("/api/strategy-plans")) return { items: [{ strategyId: "s-9", name: "均线金叉", symbol: "SHFE.rb2701", version: 1, status: "ready",
      definition: { schemaVersion: 1, instrumentScope: { mode: "selected", symbols: ["SHFE.rb2701"] }, plan: { timeframeSeconds: 86400 } } }] };
    if (url.endsWith("/api/strategy-plans/s-9/validate")) return { strategyId: "run-9" };
    if (init?.method === "POST") return {};
    return {};
  });
});

const posts = () => request.mock.calls.filter(([, init]) => init?.method === "POST").map(([url, init]) => [url, JSON.parse(String(init.body))]);

it("回测结果页：净值曲线带样本内外分界，几组参数的净值 / 样本内 / 样本外，过拟合风险标出；选用一组后上线固定出卡确认，不出现自动下单", async () => {
  const report = vi.fn();
  render(<MobileLab rootUrl="http://root" baseUrl="http://runtime" labId={null} chartTimeframe={300} sessionId="chat-1" onClose={vi.fn()} onAskAi={vi.fn()} onReport={report} />);
  const table = await screen.findByRole("table", { name: "各组参数的净值、样本内与样本外" });
  expect(screen.getByText("均线金叉 · 回测")).toBeVisible();
  expect(screen.getByText("前 70% 样本内 · 后 30% 样本外")).toBeVisible();
  expect(screen.getByRole("img", { name: /净值曲线，虚线右边是样本外/ }).querySelector(".fdm-curve-split")).not.toBeNull();
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows[0]).toHaveTextContent("快线 5 / 慢线 20过拟合风险+21,000+25,800-4,800");
  expect(rows[1]).not.toHaveTextContent("过拟合风险");
  // The report is a passive effect after the render the table appeared in: wait for it, not for the last call.
  await waitFor(() => expect(report).toHaveBeenCalledWith({ labId: "lab-1", chosen: null }));
  // Pick the other group, 选用 it (the server re-runs and saves), then 上线运行.
  fireEvent.click(within(rows[1]).getByRole("button", { name: "快线 10 / 慢线 30" }));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "选用这一组" })); });
  await waitFor(() => expect(screen.getByText("已选用")).toBeVisible());
  await waitFor(() => expect(report).toHaveBeenCalledWith({ labId: "lab-1", chosen: "v2" }));
  const reports = report.mock.calls.map(([value]) => value?.chosen);
  expect(reports.indexOf(null)).toBeLessThan(reports.indexOf("v2"));
  expect(screen.queryByRole("radio")).toBeNull();
  expect(screen.queryByRole("button", { name: /自动下单/ })).toBeNull();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "上线运行" })); });
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("已上线：出卡确认"));
  expect(posts()).toEqual([
    ["http://root/api/strategy-labs/lab-1/choose", { variantId: "v2" }],
    ["http://runtime/api/strategy-plans/s-9/validate", { symbol: "SHFE.rb2701", chartTimeframeSeconds: 86400 }],
    ["http://runtime/api/strategies/run-9/monitor/start", { signalAction: "confirm", sessionId: "chat-1" }],
  ]);
  expect(screen.getByRole("button", { name: "已上线" })).toBeDisabled();
});

it("AI 模拟执行已停止时不上线，写明原因", async () => {
  current = lab({ variantId: "v2", strategyId: "s-9", backtestSummary: {}, at: "2026-10-06T05:31:00Z" });
  request.mockImplementation(async (url: string) => url.endsWith("/api/ai-execution") ? { controls: { stopStatus: "STOPPED" } }
    : url.endsWith("/api/strategy-labs/lab-1") ? current : {});
  render(<MobileLab rootUrl="http://root" baseUrl="http://runtime" labId="lab-1" chartTimeframe={300} onClose={vi.fn()} onAskAi={vi.fn()} onReport={vi.fn()} />);
  const launch = await screen.findByRole("button", { name: "上线运行" });
  await act(async () => { fireEvent.click(launch); });
  expect(screen.getByRole("alert")).toHaveTextContent("AI 模拟执行已停止或急停");
  expect(posts()).toEqual([]);
});
