import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { Debate, DebateFact } from "../researchDebate";
import { applyFeedEvent, resetFeed } from "../runtimeFeed";
import { applyUiSurface, resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { ToolsRail } from "./futures/ToolsRail";
import type { RightDock } from "./futures/support";
import { EventRow, type NewsEvent } from "./NewsEvents";
import type { NewsTab } from "./NewsPanel";
import { DebateTab, DebateView, factDay, useDebateHost } from "./ResearchDebate";

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); resetUiSurfaces(); });

const fact = (id: string, n: number, extra: Record<string, unknown> = {}) => ({ id, n, sheet: "technical", section: "market", kind: "data", label: `${id} 标签`,
  value: `${id} 值`, source: "快期行情", asOf: "2026-10-11", ...extra }) as DebateFact;
const said = (id: string, side: "bull" | "bear", text: string, factIds: string[], extra: Record<string, unknown> = {}) =>
  ({ id, side, text, factIds, quote: "", verified: true, ...extra });
const DONE: Debate = {
  id: "db0123456789ab", status: "done", symbol: "SHFE.rb2701", productKey: "SHFE.rb", name: "螺纹钢", researchDay: "2026-10-11",
  createdAt: "2026-10-11T03:00:00+00:00", finishedAt: "2026-10-11T03:00:41+00:00", seconds: 41,
  topic: { kind: "event", eventId: "e0123456789ab", title: "多地优化住房公积金政策", question: "这条消息对 螺纹钢2701 意味着什么？", tier: "major", publishedAt: "2026-10-11T11:00:00+08:00" },
  sheets: [{ key: "position", title: "持仓", status: "complete", factIds: ["D.position_rank"], missing: [] },
    { key: "technical", title: "技术面", status: "partial", factIds: ["M.price"], missing: ["关键价位"] }],
  facts: [fact("M.price", 1), fact("D.position_rank", 2, { sheet: "position", asOf: "20261009" }), fact("T.event", 3, { section: "flash", kind: "report", sheet: "news" })],
  price: 3100, steps: [{ key: "collect", status: "done", seconds: 2 }, ...(["turn1", "turn2", "turn3", "turn4"] as const).map(key => ({ key, status: "done" as const, seconds: 8 })),
    { key: "judge", status: "done", seconds: 14 }],
  turns: [
    { n: 1, side: "bull", status: "done", text: "", sentences: [said("多1", "bull", "公积金放松直接作用在成交[F3]。", ["T.event"]), said("多2", "bull", "超卖了[F1]。", ["M.price"])] },
    { n: 2, side: "bear", status: "done", text: "", sentences: [said("空1", "bear", "前20净多是换月移仓[F2]。", ["D.position_rank"]),
      said("空2", "bear", "去年同期跌了 8%[F1]。", ["M.price"], { verified: false, problem: "这个数在所引资料里找不到" })] },
    { n: 3, side: "bull", status: "done", text: "", sentences: [said("多3", "bull", "你说换月，我认。但期货先交易预期[F1]。", ["M.price"])] },
    { n: 4, side: "bear", status: "done", text: "", sentences: [said("空3", "bear", "预期也要成交来兑现。", [])] },
  ],
  leans: { bull: "偏多", bear: "偏空" },
  verdict: { stance: "略偏多", winner: "bull", margin: "略", insufficient: false, headline: "短线情绪利多", reason: "多方的政策传导站得住，空方换月的说法也成立，但不够把价格压下去。",
    adopted: [{ id: "多3", side: "bull", reason: "盘面已先反应" }],
    rejected: [{ id: "空1", side: "bear", reason: "换月移仓成立" }], swing: "政策传导要多久", changeMind: "仓单继续增加", invalidation: 3060, watch: ["10-12 社融"],
    confidence: "中", confidenceBasis: { evidenceShare: 1, refuted: 0, missingSheets: 0, staleSheets: 0, cap: "中", score: 2 } },
  review: false, error: null, models: { bull: "deepseek-flash", bear: "glm-5.3-flash", judge: "gpt-6-astra" }, mapping: { A: "bear", B: "bull" },
  calls: [{ role: "judge", stage: "verdict", model: "gpt-6-astra", tokens: 900, seconds: 14, output: { winner: "B" } }], tokens: 4200,
  settlement: { tradingDay: "2026-10-12", due: "2026-10-18", entryPrice: 3100, invalidation: 3060, stance: "略偏多", deskStance: "震荡", leans: {}, status: "pending", closes: [], outcomes: {} },
  adopted: null, compare: { stance: "震荡" }, checks: { rounds: 2, arguments: 6, unverified: 1, cited: 3 },
};
const EARLIER = { items: [
  { id: "db0123456789ab", status: "done", symbol: "SHFE.rb2701", productKey: "SHFE.rb", name: "螺纹钢", researchDay: "2026-10-11", createdAt: "", finishedAt: "", topic: { kind: "event" }, stance: "略偏多", confidence: "中" },
  { id: "db00000000000a", status: "done", symbol: "SHFE.rb2701", productKey: "SHFE.rb", name: "螺纹钢", researchDay: "2026-10-03", createdAt: "", finishedAt: "", topic: { kind: "today" },
    stance: "略偏空", confidence: "中", winner: "bear", margin: "略", settlement: { status: "settled", outcome: "right" } },
  { id: "db00000000000b", status: "done", symbol: "SHFE.rb2701", productKey: "SHFE.rb", name: "螺纹钢", researchDay: "2026-10-10", createdAt: "", finishedAt: "", topic: { kind: "today" },
    stance: "偏多", confidence: "高", winner: "bull", margin: "明显", settlement: { status: "pending" } },
  { id: "db00000000000c", status: "done", symbol: "SHFE.rb2701", productKey: "SHFE.rb", name: "螺纹钢", researchDay: "2026-10-09", createdAt: "", finishedAt: "", topic: { kind: "today" },
    stance: "看不清", confidence: "低", winner: null, insufficient: true, settlement: { status: "void" } },
], stats: { settled: 1, pending: 1, minSamples: 30, rates: null }, day: "2026-10-11" };

test("空方换了兜底模型：研讨记录显示实际说话的模型，顶替的那次写明换下了谁、为什么", async () => {
  const switched: Debate = { ...DONE, models: { ...DONE.models, bear: "kimi-k3" }, calls: [
    { role: "bear", stage: "turn2", model: "glm-5.3-flash", tokens: 0, seconds: 1, error: "model_rate_limited" },
    { role: "bear", stage: "turn2", model: "kimi-k3", tokens: 400, seconds: 9, fallbackFrom: "glm-5.3-flash", fallbackReason: "model_rate_limited" },
    ...DONE.calls] };
  respond({ "/research/debates/db0123456789ab": switched });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  const head = await screen.findByRole("button", { name: "展开研讨记录" });
  expect(head).toHaveTextContent("空方 Kimi K3");
  fireEvent.click(head);
  expect(screen.getByText(/空方 · 第 2 次发言 · Kimi K3（kimi-k3）· 400 token · 9s · 顶替 GLM（限流）/)).toBeVisible();
  expect(screen.getByText(/GLM（glm-5.3-flash）· 0 token · 1s · 失败：model_rate_limited/)).toBeVisible();
});

test("空方第二次发言才换兜底：第一次是 GLM 说的，记录摘要写 GLM→Kimi K3，不把整场算给 Kimi", async () => {
  const late: Debate = { ...DONE, models: { ...DONE.models, bear: "kimi-k3" },
    fallbacks: [{ role: "bear", stage: "turn4", from: "glm-5.3-flash", to: "kimi-k3", reason: "TimeoutError" }],
    calls: [{ role: "bear", stage: "turn2", model: "glm-5.3-flash", tokens: 300, seconds: 8 },
      { role: "bear", stage: "turn4", model: "glm-5.3-flash", tokens: 0, seconds: 60, error: "TimeoutError" },
      { role: "bear", stage: "turn4", model: "kimi-k3", tokens: 400, seconds: 9, fallbackFrom: "glm-5.3-flash", fallbackReason: "TimeoutError" }, ...DONE.calls] };
  respond({ "/research/debates/db0123456789ab": late });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  expect(await screen.findByRole("button", { name: "展开研讨记录" })).toHaveTextContent("空方 GLM→Kimi K3");
});

/** The computer's 辩论 page: the 调研面板's 辩论 tab (as the workspace wires it); the host follows the tab. */
function Host({ baseUrl = "https://test", onAsk }: { baseUrl?: string; onAsk?: (text: string) => void }) {
  const [dock, setDock] = useState<RightDock>(null);
  const [tab, setTab] = useState<NewsTab>("desk");
  const host = useDebateHost({ baseUrl, symbol: "SHFE.rb2701", enabled: true, onAsk,
    view: [dock === "news" && tab === "debate", open => { if (open) { setDock("news"); setTab("debate"); } else setTab(current => current === "debate" ? "desk" : current); }] });
  // The panel's own tabs (NewsPanel) without its other pages, which read their own routes.
  return <>
    {dock === "news" ? <div role="tablist">{(["desk", "debate"] as const).map(value =>
      <button type="button" role="tab" key={value} aria-selected={tab === value} onClick={() => setTab(value)}>{value === "desk" ? "品种" : "辩论"}</button>)}</div> : null}
    {dock === "news" && tab === "debate" ? <div role="tabpanel" aria-label="辩论"><DebateTab host={host} /></div> : <p>closed</p>}
    <ToolsRail rightDock={dock} setRightDock={setDock} onTrade={() => undefined} onSettings={() => undefined} />
  </>;
}

const respond = (routes: Record<string, unknown>) => {
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    const key = Object.keys(routes).find(path => path.startsWith("POST ") ? init?.method === "POST" && String(url).includes(path.slice(5)) : String(url).includes(path));
    const body = key ? routes[key] : { items: [], stats: { settled: 0, pending: 1, minSamples: 30, rates: null }, day: "2026-10-11" };
    return typeof body === "number" ? new Response(JSON.stringify({ detail: { code: "x", message: "空方模型 glm-5.3-flash 不可用" } }), { status: body })
      : new Response(JSON.stringify(body));
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
};
const verdictBlock = () => screen.findByRole("region", { name: /^裁判 · / });

test("调研面板的“辩论”页签：还没有辩论时可以辩今日议题，下面是以前的辩论；收起调研面板就关了", async () => {
  const fetcher = respond({ "/research/debates?": EARLIER, "POST /api/research/debates": { ...DONE, status: "queued", verdict: null, turns: [] } });
  render(<Host />);
  fireEvent.click(screen.getByRole("button", { name: "打开调研面板" }));
  fireEvent.click(screen.getByRole("tab", { name: "辩论" }));
  const page = screen.getByRole("tabpanel", { name: "辩论" });
  const recent = await within(page).findByRole("region", { name: "以前的辩论" });
  // An older Runtime's summary has no winner: its tier stands in.
  expect([...recent.querySelectorAll(".debate-recent-item")].map(item => item.textContent)).toEqual([
    "10-11 · 螺纹钢 · 快讯略偏多", "10-03 · 螺纹钢空方略胜5 日后：说对了", "10-10 · 螺纹钢多方明显胜出5 日后：待揭晓"]);
  expect(fetcher.mock.calls.some(([url]) => String(url).includes("product=SHFE.rb2701"))).toBe(true);
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ open: true, status: "empty", id: null }));
  const start = within(page).getByRole("button", { name: "辩今日议题（rb2701）" });
  expect(start).toHaveClass("primary-button");
  fireEvent.click(start);
  await waitFor(() => expect(fetcher.mock.calls.some(([url, init]) => String(url).endsWith("/api/research/debates") && init?.method === "POST")).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "收起调研面板" }));
  expect(screen.getByText("closed")).toBeVisible();
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ open: false }));
});

test("辩论结果：裁判一行、胜方与幅度、档位和信心、多空条、理由、什么情况下不成立；唯一主按钮是写进今日判断，次按钮再辩一轮", async () => {
  vi.setSystemTime(new Date("2026-10-11T04:00:00Z"));
  const fetcher = respond({ "/adopt": { debate: { ...DONE, adopted: { at: "2026-10-11T04:00:00+00:00", stance: "偏多" } }, judgment: { stance: "偏多", generatedAt: "x" } },
    "/research/debates/db0123456789ab": DONE, "/research/debates?": EARLIER });
  const asked = vi.fn();
  render(<Host onAsk={asked} />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  const verdict = await verdictBlock();
  expect(verdict).toHaveAccessibleName("裁判 · GPT-6 Astra");
  expect(within(verdict).getByText("11:00")).toBeVisible();
  expect(within(verdict).getByText("多方略胜")).toHaveAttribute("data-side", "bull");
  expect(within(verdict).getByText("略偏多 · 信心中")).toBeVisible();
  expect(within(verdict).getByRole("img", { name: "多 6 : 空 4" })).toBeVisible();
  expect(within(verdict).getByText(DONE.verdict!.reason!)).toBeVisible();
  expect(within(verdict).getByText("什么情况下这个结论就不成立：收盘跌破 3,060，或仓单继续增加")).toBeVisible();
  const buttons = within(verdict).getAllByRole("button").filter(button => !button.closest(".help-tip"));
  expect(buttons.filter(button => button.classList.contains("primary-button")).map(button => button.textContent)).toEqual(["写进今日判断"]);
  expect(within(verdict).getByRole("button", { name: "再辩一轮" })).toHaveClass("app-button");
  // 交锋回看: one row per turn, the 未核对 sentence yellow with why, the citations as chips.
  const replay = screen.getByRole("region", { name: "交锋回看" });
  expect(within(replay).getAllByRole("listitem")).toHaveLength(4);
  expect(within(replay).getByText("（一处未核对）")).toBeVisible();
  expect(within(replay).getByText(/去年同期跌了 8%/).closest("mark")).toHaveAttribute("title", "这个数在所引资料里找不到");
  expect(replay).toHaveTextContent("6 句 · 引用 3 处资料 · 1 句未核对");
  // 以前的辩论 leaves the open one out; the folds stay at the bottom.
  expect((await screen.findByRole("region", { name: "以前的辩论" })).querySelectorAll(".debate-recent-item")).toHaveLength(3);
  fireEvent.click(screen.getByRole("button", { name: "展开裁判怎么取舍" }));
  expect(document.querySelector(".debate-picks")).toHaveTextContent("多方 · 多3 · 你说换月，我认。但期货先交易预期。（盘面已先反应）");
  fireEvent.click(within(verdict).getByRole("button", { name: "在对话里接着问" }));
  // #768B: the question goes into the chat input, worded as the page shows the debate — no internal id.
  expect(asked.mock.calls[0][0]).not.toContain("db0123456789ab");
  expect(asked.mock.calls[0][0]).toContain("快讯“多地优化住房公积金政策”");
  fireEvent.click(within(verdict).getByRole("button", { name: "写进今日判断" }));
  expect(await within(verdict).findByRole("button", { name: "已写进今日判断（偏多）" })).toBeDisabled();
  expect(fetcher.mock.calls.some(([url, init]) => String(url).endsWith("/research/debates/db0123456789ab/adopt") && init?.method === "POST")).toBe(true);
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ open: true, id: DONE.id, status: "done", stance: "略偏多", adopted: true, canAdopt: false }));
});

test("引用标记变成“名称 · 日期”标签，点开定位到资料来源那一条", async () => {
  respond({ "/research/debates/db0123456789ab": DONE });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  const replay = await screen.findByRole("region", { name: "交锋回看" });
  const chip = within(replay).getByRole("button", { name: "依据 2，D.position_rank 标签" });
  expect(chip).toHaveTextContent("D.position_rank 标签 · 10-09");
  expect(within(replay).getByRole("button", { name: "依据 3，T.event 标签" })).toHaveTextContent("快讯 · 10-11");
  expect(replay.textContent).not.toContain("[F");
  fireEvent.click(chip);
  expect(screen.getByRole("button", { name: "收起资料来源" })).toBeVisible();
  expect(document.getElementById("debate-fact-D.position_rank")).toHaveAttribute("data-highlight", "true");
});

test("进行中：一次发言一边写一边出现（光标、正在说…），说完换成核对后的版本；结束后切到结论", async () => {
  const opening = DONE.turns[0];
  const running: Debate = { ...DONE, status: "running", verdict: null, steps: DONE.steps.map(step => ({ ...step, status: step.key === "turn2" ? "run" : ["turn3", "turn4", "judge"].includes(step.key) ? "wait" : "done" })),
    turns: [opening, { n: 2, side: "bear", status: "run", text: "前20净多", sentences: [] }] };
  let current: Debate = running;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => new Response(JSON.stringify(
    init?.method === "POST" ? running : String(url).includes("/research/debates/db") ? current : { items: [], stats: null, day: "2026-10-11" }))));
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { start: "e0123456789ab" }));
  const exchange = await screen.findByLabelText("多空交锋");
  expect(within(exchange).getByText("第一轮 · 先各自表态")).toBeVisible();
  expect(within(exchange).getByText("空方 · 回应多方 · 正在说…")).toBeVisible();
  expect(exchange.querySelector(".debate-turn[data-live] .debate-caret")).not.toBeNull();
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ open: true, status: "running", step: "turn2", speaking: "bear",
    request: { kind: "start", status: "started", eventId: "e0123456789ab" } }));
  // The turn streams in over the feed: the whole text so far each time, the citation already a chip.
  const delta = (text: string) => applyFeedEvent("s1", { schemaVersion: 1, id: "d1", sessionId: "s1", kind: "result", name: "feed.debate", timestamp: "",
    payload: { id: DONE.id, turn: 2, side: "bear", text } } as never);
  act(() => { delta("前20净多是换月移仓[F2]。去年同期"); });
  // The words appear at a steady pace (a few frames), not in the burst they arrived in.
  expect(await within(exchange).findByText(/去年同期/)).toBeVisible();
  expect(within(exchange).getByRole("button", { name: "依据 2，D.position_rank 标签" })).toBeVisible();
  expect(screen.getByText(/裁判 GPT-6 Astra 给出结论/)).toBeVisible();
  // The turn ends: the nudge brings its checked version — the unchecked figure marked, no caret any more.
  current = { ...running, turns: [opening, DONE.turns[1], { n: 3, side: "bull", status: "run", text: "", sentences: [] }] };
  act(() => { applyFeedEvent("s1", { schemaVersion: 1, id: "n1", sessionId: "s1", kind: "result", name: "feed.ui", timestamp: "", payload: { reason: "research-debate" } } as never); });
  await waitFor(() => expect(exchange.querySelector("mark.debate-unchecked")).toHaveTextContent("去年同期跌了 8%"));
  expect(within(exchange).getByText(/这个数在所引资料里找不到 · 未核对/)).toBeVisible();
  expect(within(exchange).getByText("多方 · 回应空方 · 正在说…")).toBeVisible();
  expect(within(exchange).getByText("第二轮 · 互相反驳")).toBeVisible();
  // Done: the verdict replaces the transcript.
  current = DONE;
  act(() => { applyFeedEvent("s1", { schemaVersion: 1, id: "n2", sessionId: "s1", kind: "result", name: "feed.ui", timestamp: "", payload: { reason: "research-debate" } } as never); });
  expect(await verdictBlock()).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "品种" }));
  expect(screen.getByText("closed")).toBeVisible();
});

// 分析师 (sample ④, 2026-10-24): four reports before the turns.
const line = (text: string, factIds: string[] = [], extra: Record<string, unknown> = {}) => ({ text, factIds, verified: true, ...extra });
const REPORTS: NonNullable<Debate["analysts"]> = [
  { key: "technical", title: "技术面", status: "done", text: "结论：短线超卖、中期空头未改\n超卖了[F1]。\n要点：\n趋势：空头排列", model: "deepseek-flash",
    conclusion: line("短线超卖、中期空头未改"), sentences: [said("技1", "bull", "超卖了[F1]。", ["M.price"])],
    points: [line("空头排列", [], { label: "趋势" }), line("压力 3118[F1]", ["M.price"], { label: "关键位" })], found: [], searches: [], gaps: [] },
  { key: "supply", title: "供需基本面", status: "run", text: "结论：开工", conclusion: null, sentences: [], points: [], model: "deepseek-flash",
    found: ["W.0a1b2c3d"], searches: [{ gap: "开工率", query: "螺纹钢 高炉开工率 after:2026-09-27", found: 1 }], gaps: [{ label: "开工率", state: "missing" }] },
  { key: "flow", title: "资金持仓", status: "failed", text: "", conclusion: null, sentences: [], points: [], found: [], searches: [], gaps: [], error: "model_rate_limited" },
  { key: "news", title: "新闻宏观", status: "wait", text: "", conclusion: null, sentences: [], points: [], found: [], searches: [], gaps: [] },
];
const WEB = fact("W.0a1b2c3d", 4, { section: "web", kind: "report", sheet: "basis", label: "247家钢厂高炉开工率 82.1%", value: "247 家钢厂高炉开工率 82.1%",
  source: "我的钢铁网", asOf: "2026-10-08", found: { analyst: "supply", gap: "开工率" } });
const LESSON = { id: "db00000000000a", researchDay: "2026-10-03", outcome: "right" as const,
  text: "上次（10-03）裁判判空方略胜（略偏空），5 个交易日后说对了：空方看对了，多方当时说“超卖要反弹”，没有兑现。" };

test("进行中：进度条、四位分析师同时写（正在写的流式出现、全网补充几条、等待中、没写成），上次复盘带进辩论", async () => {
  const running: Debate = { ...DONE, status: "running", verdict: null, turns: [], analysts: REPORTS, lesson: LESSON, searches: 1, facts: [...DONE.facts, WEB],
    steps: [{ key: "collect", status: "done" }, { key: "analysts", status: "run" }, ...DONE.steps.slice(1).map(step => ({ ...step, status: "wait" as const }))] };
  respond({ "/research/debates/db0123456789ab": running });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  const stages = await screen.findByRole("status", { name: "辩论进度" });
  expect(stages).toHaveTextContent("① 分析师写报告 2/4");
  expect(within(stages).getByText(/① 分析师写报告/)).toHaveAttribute("aria-current", "step");
  expect(stages).toHaveTextContent("约 2–3 分钟");
  const supply = screen.getByRole("article", { name: "供需基本面分析师" });
  expect(supply).toHaveTextContent("正在写…");
  expect(within(supply).getByText("全网补充 1 条")).toBeVisible();
  expect(within(supply).getByText("调研页缺开工率，从全网补了：")).toBeVisible();
  expect(within(supply).getByRole("list", { name: "供需基本面分析师全网补充的资料" })).toHaveTextContent("我的钢铁网 · 10-08247家钢厂高炉开工率 82.1%");
  // The report streams in over the feed (the whole text so far), its citation already a chip, with a caret.
  act(() => { applyFeedEvent("s1", { schemaVersion: 1, id: "d1", sessionId: "s1", kind: "result", name: "feed.debate", timestamp: "",
    payload: { id: DONE.id, analyst: "supply", text: "结论：开工回落\n据报道高炉开工率 82.1%[F4]。" } } as never); });
  expect(await within(supply).findByText(/据报道高炉开工率/)).toBeVisible();
  expect(await within(supply).findByRole("button", { name: "依据 4，247家钢厂高炉开工率 82.1%" })).toBeVisible();
  expect(supply.querySelector(".debate-caret")).not.toBeNull();
  expect(screen.getByRole("article", { name: "新闻宏观分析师" })).toHaveTextContent("等待中");
  expect(screen.getByRole("article", { name: "资金持仓分析师" })).toHaveTextContent("没写成");
  // A finished card folds to its conclusion; opening it (a click or `disclosure: debate-analyst-technical`) shows the
  // reasoning and the key points.
  const technical = screen.getByRole("article", { name: "技术面分析师" });
  expect(technical).toHaveTextContent("结论：短线超卖、中期空头未改");
  expect(within(technical).queryByRole("group", { name: "技术面要点" })).toBeNull();
  fireEvent.click(within(technical).getByRole("button", { name: "展开技术面分析师的报告" }));
  expect(technical.querySelector(".stat-tiles")).toHaveTextContent("趋势空头排列关键位压力 3118");
  expect(uiSurface("disclosure:debate-analyst-technical")?.state).toBe(true);
  act(() => applyUiSurface("disclosure:debate-analyst-technical", false));
  expect(technical.querySelector(".stat-tiles")).toBeNull();
  expect(screen.getByText(`带入辩论的复盘：${LESSON.text}`)).toBeVisible();
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ step: "analysts", analysts: 2, lesson: LESSON.text }));
});

test("结论里有裁判写的关键分歧与“如果……那么……”；分析师报告折叠在下面；以前的记录没有这些也照常显示", async () => {
  const finished: Debate = { ...DONE, analysts: REPORTS.map(report => ({ ...report, status: "done" as const, conclusion: line(`${report.title}结论`) })),
    lesson: LESSON, verdict: { ...DONE.verdict!, disagreements: ["资金在进场还是离场"], scenarios: ["如果仓单继续减少，那么现货偏紧的逻辑会加强"] } };
  respond({ "/research/debates/db0123456789ab": finished });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  const verdict = await verdictBlock();
  expect(within(verdict).getByText("关键分歧")).toBeVisible();
  expect(within(verdict).getByText("资金在进场还是离场")).toBeVisible();
  expect(within(verdict).getByText("如果仓单继续减少，那么现货偏紧的逻辑会加强")).toBeVisible();
  const reports = screen.getByRole("region", { name: "分析师报告" });
  expect(within(reports).getAllByRole("article")).toHaveLength(4);
  expect(within(reports).getAllByRole("button", { name: /^展开.+分析师的报告$/ })).toHaveLength(4);
  expect(screen.queryByRole("status", { name: "辩论进度" })).toBeNull();
  expect(screen.getByText(`带入辩论的复盘：${LESSON.text}`)).toBeVisible();
  cleanup();
  respond({ "/research/debates/db0123456789ab": DONE });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  expect(await verdictBlock()).toBeVisible();
  expect(screen.queryByRole("region", { name: "分析师报告" })).toBeNull();
  expect(screen.queryByText("关键分歧")).toBeNull();
});

test("流式只在推送不通时才退回轮询", async () => {
  vi.useFakeTimers();
  resetFeed("s1");
  const running: Debate = { ...DONE, status: "running", verdict: null, turns: [] };
  const fetcher = vi.fn(async (_url: string) => new Response(JSON.stringify(running)));
  vi.stubGlobal("fetch", fetcher);
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
  const reads = () => fetcher.mock.calls.filter(([url]) => String(url).includes("/research/debates/db")).length;
  const before = reads();
  // The feed is live (a channel frame just arrived): no poll.
  act(() => { applyFeedEvent("s1", { schemaVersion: 1, id: "a1", sessionId: "s1", kind: "result", name: "feed.activity", timestamp: "",
    payload: { environment: "simulation", symbol: "SHFE.rb2701", orders: [], fills: [], strategies: [] } } as never); });
  await act(async () => { await vi.advanceTimersByTimeAsync(4100); });
  expect(reads()).toBe(before);
  // Silent for longer than the feed's stale window: the slow poll takes over.
  await act(async () => { await vi.advanceTimersByTimeAsync(12_000); });
  expect(reads()).toBeGreaterThan(before);
});

test("发起被拒：说清是哪个角色的模型不可用", async () => {
  respond({ "POST /api/research/debates": 503 });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { start: "desk" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("空方模型 glm-5.3-flash 不可用");
  // The surface re-registers in an effect after the render that shows the alert.
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ open: true, status: "refused", request: { kind: "start", status: "refused" } }));
});

const EVENT: NewsEvent = { id: "e0123456789ab", eventLabel: "公积金", researchDay: "2026-10-11", publishedAt: "2026-10-11T03:00:00Z", firstAt: "2026-10-11T03:00:00Z",
  title: "多地优化住房公积金政策", text: "多地积极优化调整住房公积金政策", ai: { event: "政策", direction: "偏多", strength: "中", reason: "成交回暖" },
  tier: "major", reports: 3, sources: ["财联社"], direct: true, items: [{ id: "i1", source: "财联社", sources: ["财联社"], publishedAt: "2026-10-11T03:00:00Z", title: "t", text: "x", link: null, tags: [] }] } as NewsEvent;

test("快讯事件行：重要事件有“研讨”，已研讨的挂标签；品种标签带实时涨跌、点了切过去", () => {
  const start = vi.fn(); const chip = vi.fn(); const openDebate = vi.fn();
  const { rerender } = render(<ol><EventRow event={EVENT} surface="flash" timeOf={() => "11:00"}
    extras={{ chips: [{ id: "rb", name: "螺纹", symbol: "SHFE.rb2701", changePercent: 0.55 }, { id: "i", name: "铁矿", changePercent: -0.82 }], onChip: chip, onDebate: start }} /></ol>);
  fireEvent.click(screen.getByRole("button", { name: "研讨：多地优化住房公积金政策" }));
  expect(start).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "切到 螺纹 主力合约" }));
  expect(chip).toHaveBeenCalledWith("SHFE.rb2701");
  expect(screen.getByText("+0.55%")).toHaveAttribute("data-tone", "up");
  expect(screen.getByText("−0.82%")).toHaveAttribute("data-tone", "down");
  rerender(<ol><EventRow event={EVENT} surface="flash" timeOf={() => "11:00"}
    extras={{ onDebate: start, debated: { id: "db0123456789ab", status: "done", stance: "略偏多", at: "2026-10-11T01:41:00Z" }, onOpenDebate: openDebate }} /></ol>);
  expect(screen.queryByRole("button", { name: /^研讨：/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "打开这条快讯的研讨结果" }));
  expect(openDebate).toHaveBeenCalledWith("db0123456789ab");
  expect(screen.getByRole("button", { name: "打开这条快讯的研讨结果" })).toHaveTextContent("已研讨 · 略偏多 · 09:41");
  rerender(<ol><EventRow event={{ ...EVENT, tier: "other" }} surface="flash" timeOf={() => "11:00"} extras={{ onDebate: start }} /></ol>);
  expect(screen.queryByRole("button", { name: /^研讨：/ })).toBeNull();  // only 重要 events
});

test("调研面板的快讯行“研讨”与已研讨标签交给辩论（快讯页里不内嵌辩论）", async () => {
  const { NewsPanel } = await import("./NewsPanel");
  respond({ "/api/research/flash": { status: "ok", items: [], events: [EVENT], screen: { mode: "ai", pending: 0, unscreened: 0, dropped: { count: 0 } } } });
  const debate = { start: vi.fn(), openDebate: vi.fn() };
  const instruments = [{ symbol: "SHFE.rb2701", productId: "rb", instrumentName: "螺纹钢2701", isMain: true, deliveryYear: 2027, deliveryMonth: 1 }] as never;
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => undefined} instruments={instruments} debate={debate as never} />);
  act(() => applyUiSurface("news", { tab: "flash" }));
  fireEvent.click(await screen.findByRole("button", { name: "研讨：多地优化住房公积金政策" }));
  expect(debate.start).toHaveBeenCalledWith(expect.objectContaining({ eventId: EVENT.id, origin: "flash" }));
  expect(screen.queryByTestId("research-debate")).toBeNull();
});

test("写进今日判断的回包晚到：已切到别的辩论就丢掉，不改新画面", async () => {
  vi.setSystemTime(new Date("2026-10-11T04:00:00Z"));
  let release: (value: Response) => void = () => {};
  const other = { ...DONE, id: "db99999999999a", adopted: null };
  vi.stubGlobal("fetch", vi.fn((url: string) => String(url).endsWith("/adopt")
    ? new Promise<Response>(resolve => { release = resolve; })
    : Promise.resolve(new Response(JSON.stringify(String(url).includes("db99999999999a") ? other : String(url).includes("/research/debates/db") ? DONE
      : { items: [], stats: null, day: "2026-10-11" })))));
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  fireEvent.click(await screen.findByRole("button", { name: "写进今日判断" }));
  act(() => applyUiSurface("researchDebate", { open: other.id }));
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ id: other.id }));
  await act(async () => { release(new Response(JSON.stringify({ debate: { ...DONE, adopted: { at: "x", stance: "偏多" } }, judgment: { stance: "偏多", generatedAt: "x" } }))); });
  expect(uiSurface("researchDebate")?.state).toMatchObject({ id: other.id, adopted: false, canAdopt: true });
  expect(screen.queryByText(/已写进今日判断/)).toBeNull();
});

test("事件的品种标签：先列被筛的品种，再接各条报道的品种，带主力合约和总表涨跌（电脑快讯与手机调研共用）", async () => {
  const { eventChips, quotesByProduct } = await import("./FlashFeed");
  const instruments = [{ symbol: "SHFE.rb2701", exchangeId: "SHFE", productId: "rb", instrumentName: "螺纹钢2701", isMain: true, deliveryYear: 2027, deliveryMonth: 1 },
    { symbol: "DCE.i2701", exchangeId: "DCE", productId: "i", instrumentName: "铁矿石2701", isMain: true, deliveryYear: 2027, deliveryMonth: 1 }] as never;
  const event = { ...EVENT, ai: { ...EVENT.ai!, product: "rb" } } as NewsEvent;
  const chips = eventChips(event, new Map([["i1", ["i", "rb"]]]), instruments, quotesByProduct([{ productId: "RB", changePercent: 0.55 }]));
  expect(chips).toEqual([{ id: "rb", name: "螺纹钢", symbol: "SHFE.rb2701", changePercent: 0.55 }, { id: "i", name: "铁矿石", symbol: "DCE.i2701", changePercent: null }]);
});

test("“讨论一下这条”查最新重要快讯时用户先关了辩论页：晚到的结果不再重新打开辩论", async () => {
  let release: (value: Response) => void = () => {};
  const fetcher = vi.fn((url: string) => String(url).includes("/research/flash")
    ? new Promise<Response>(resolve => { release = resolve; }) : Promise.resolve(new Response(JSON.stringify(DONE))));
  vi.stubGlobal("fetch", fetcher);
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { start: "event" }));
  fireEvent.click(await screen.findByRole("tab", { name: "品种" }));
  await act(async () => { release(new Response(JSON.stringify({ events: [{ id: "e0123456789ab", tier: "major", title: "x", text: "x" }] }))); });
  expect(screen.getByText("closed")).toBeVisible();
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/api/research/debates"))).toBe(false);
});

test("停止的回包晚到：已打开别的辩论就不覆盖；国债三位小数的失效位原样显示", async () => {
  let release: (value: Response) => void = () => {};
  const running = { ...DONE, status: "running" as const, verdict: null };
  const other = { ...DONE, id: "db99999999999a", verdict: { ...DONE.verdict!, invalidation: 103.125 } };
  vi.stubGlobal("fetch", vi.fn((url: string) => String(url).endsWith("/cancel")
    ? new Promise<Response>(resolve => { release = resolve; })
    : Promise.resolve(new Response(JSON.stringify(String(url).includes("db99999999999a") ? other : String(url).includes("/research/debates/db") ? running
      : { items: [], stats: null, day: "2026-10-11" })))));
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  const stop = await screen.findByRole("button", { name: "停止" });
  expect(stop).toHaveClass("app-button", "danger");
  fireEvent.click(stop);
  act(() => applyUiSurface("researchDebate", { open: other.id }));
  const verdict = await verdictBlock();
  await act(async () => { release(new Response(JSON.stringify({ ...running, status: "cancelled" }))); });
  expect(uiSurface("researchDebate")?.state).toMatchObject({ id: other.id, status: "done" });
  expect(within(verdict).getByText(/收盘跌破 103\.125/)).toBeVisible();
});

test("完成提示只对正在看的那场辩论不弹：看着 B 时 A 完成仍提示", async () => {
  const { debateShownNow, setDebateViewOpen, setShownDebate } = await import("../researchDebate");
  setDebateViewOpen(true); setShownDebate("dbB");
  expect(debateShownNow("dbB")).toBe(true);
  expect(debateShownNow("dbA")).toBe(false);
  expect(debateShownNow(undefined)).toBe(false);
  setDebateViewOpen(false);
  expect(debateShownNow("dbB")).toBe(false);
  setShownDebate(undefined);
});

test("同一事件涉及多个品种时，“已研讨”按品种区分", async () => {
  const { debatedKey } = await import("./FlashFeed");
  expect(debatedKey("e1", "SHFE.rb")).toBe("rb:e1");
  expect(debatedKey("e1", "rb")).toBe("rb:e1");
  expect(debatedKey("e1", "DCE.i")).not.toBe(debatedKey("e1", "SHFE.rb"));
});

test("界面命令只带事件 id：快讯没画出来也按事件自己的品种发起；点快讯行带该行品种，由 Runtime 核对", async () => {
  const gold = { ...EVENT, id: "egold0000001", title: "美联储降息预期升温", ai: { ...EVENT.ai, product: "au" } } as NewsEvent;
  const fetcher = respond({ "POST /api/research/debates": { ...DONE, symbol: "SHFE.au2612", productKey: "SHFE.au" } });
  const posts = () => fetcher.mock.calls.filter(([, init]) => init?.method === "POST").map(([, init]) => JSON.parse(String(init?.body)));
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { start: "egold0000001" }));
  await waitFor(() => expect(posts()).toHaveLength(1));
  expect(posts()[0]).toEqual({ eventId: "egold0000001", force: false });  // never the chart's contract as a guess
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ symbol: "SHFE.au2612", request: { status: "started", eventId: "egold0000001" } }));
  // A row of 持仓与自选 names its own product's main contract (a pick the Runtime checks against the event).
  const { FlashFeed } = await import("./FlashFeed");
  const instruments = [{ symbol: "SHFE.au2612", productId: "au", instrumentName: "沪金2612", isMain: true, deliveryYear: 2026, deliveryMonth: 12 }] as never;
  respond({ "/api/research/flash": { status: "ok", items: [], events: [gold], screen: { mode: "ai", pending: 0, unscreened: 0, dropped: { count: 0 } } },
    "POST /api/research/debates": { ...DONE, symbol: "SHFE.au2612", productKey: "SHFE.au" } });
  const clicked = vi.fn();
  render(<FlashFeed baseUrl="https://test" symbol="SHFE.rb2701" visible filter="watch" onFilterChange={() => undefined} watchProducts={["SHFE.au"]}
    instruments={instruments} onState={() => undefined} debate={{ onStart: clicked, onOpen: () => undefined }} />);
  fireEvent.click(await screen.findByRole("button", { name: "研讨：美联储降息预期升温" }));
  expect(clicked).toHaveBeenCalledWith(expect.objectContaining({ id: "egold0000001" }), "SHFE.au2612");
});

test("一条快讯涉及几个品种：Runtime 要求指明，界面说清是哪几个", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: { code: "research_debate_event_ambiguous",
    message: "这条快讯涉及几个品种，请指明研讨哪一个（涉及：螺纹钢、热卷）", products: ["rb", "hc"] } }), { status: 409 })));
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { start: "e0123456789ab" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("涉及：螺纹钢、热卷");
  await waitFor(() => expect(uiSurface("researchDebate")?.state).toMatchObject({ request: { status: "refused" } }));
});

function Compact() {
  const host = useDebateHost({ baseUrl: "https://test", symbol: "SHFE.rb2701", enabled: true });
  return host.open ? <DebateView host={host} compact /> : <p>closed</p>;
}

test("手机整页：结论、交锋回看与再辩一轮都在；返回回到快讯", async () => {
  respond({ "/research/debates/db0123456789ab": DONE });
  render(<Compact />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  expect(await screen.findByRole("button", { name: "再辩一轮" })).toBeVisible();
  expect(screen.getByRole("region", { name: "交锋回看" })).toBeVisible();
  expect(screen.getByRole("button", { name: "展开底稿与来源" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "返回快讯" }));
  expect(screen.getByText("closed")).toBeVisible();
});

test("以前的记录（两轮并行论点，没有引用标记）也照样显示：论点后面跟所引资料的标签", async () => {
  const older: Debate = { ...DONE, turns: [{ n: 1, side: "bull", status: "done", text: "", sentences: [said("多1", "bull", "RSI 进入超卖", ["M.price"])] }] };
  respond({ "/research/debates/db0123456789ab": older });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  const replay = await screen.findByRole("region", { name: "交锋回看" });
  expect(within(replay).getByRole("button", { name: "依据 1，M.price 标签" })).toBeVisible();
});

test("需复核分两种：裁判判了胜方但没有带依据的句子，与裁判的回答读不出", async () => {
  const unsupported: Debate = { ...DONE, status: "review", review: true, verdict: null, unsupportedVerdict: DONE.verdict };
  respond({ "/research/debates/db0123456789ab": unsupported });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  const verdict = await verdictBlock();
  expect(within(verdict).getByText(/裁判判多方胜出，但采纳的句子里没有带引用的依据/)).toBeVisible();
  expect(within(verdict).queryByText(/没能读出/)).toBeNull();
});

test("关了辩论页后 rerun 命令不再发起（不白花当天额度）", async () => {
  const fetcher = respond({ "/research/debates/db0123456789ab": DONE, "POST /api/research/debates": DONE });
  render(<Host />);
  act(() => applyUiSurface("researchDebate", { open: DONE.id }));
  await verdictBlock();
  fireEvent.click(screen.getByRole("tab", { name: "品种" }));
  expect(() => applyUiSurface("researchDebate", { rerun: true })).toThrow("没有打开的辩论");
  expect(fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
});

test("辩论页整个卸载后，晚到的“讨论这条”查询不再发起辩论", async () => {
  let release: (value: Response) => void = () => {};
  const fetcher = vi.fn((url: string) => String(url).includes("/research/flash")
    ? new Promise<Response>(resolve => { release = resolve; }) : Promise.resolve(new Response(JSON.stringify(DONE))));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<Host />);
  act(() => applyUiSurface("researchDebate", { start: "event" }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes("/research/flash"))).toBe(true));
  view.unmount();
  await act(async () => {
    release(new Response(JSON.stringify({ events: [{ id: "e0123456789ab", tier: "major", title: "x", text: "x" }] })));
    await new Promise(resolve => setTimeout(resolve, 50));  // the lookup's own awaits settle
  });
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/api/research/debates"))).toBe(false);
});

test("研究日按北京 08:30 换日", async () => {
  const { researchDayNow } = await import("../researchDebate");
  expect(researchDayNow(Date.parse("2026-10-11T08:29:00+08:00"))).toBe("2026-10-10");
  expect(researchDayNow(Date.parse("2026-10-11T08:30:00+08:00"))).toBe("2026-10-11");
  expect(researchDayNow(Date.parse("2026-10-11T23:59:00+08:00"))).toBe("2026-10-11");
});

test("资料日期按北京时间显示：搜到的网页日期是北京零点换成的 UTC，不能取它的 UTC 日期（线上 10-24 早一天）", () => {
  expect(factDay("2026-10-19T16:00:00+00:00")).toBe("10-20");   // Beijing midnight of 10-20
  expect(factDay("2026-10-24T09:30:00+08:00")).toBe("10-24");
  expect(factDay("2026-10-23")).toBe("10-23");                   // a market date stays as written
  expect(factDay("20261023")).toBe("10-23");
  expect(factDay("")).toBe("");
});
