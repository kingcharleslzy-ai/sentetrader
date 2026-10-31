import { renderHook } from "@testing-library/react";
import { requestJson } from "../api";
import { agentConnectionInfo, captureNativeWindow } from "../nativeAgent";
import { matches, refused, useUiControl, useUiControlHealth, type UiState } from "./useUiControl";
import { logDesktop } from "../frontendLog";
import * as runtimeFeed from "../runtimeFeed";

vi.mock("../api", () => ({ requestJson: vi.fn() }));
vi.mock("../frontendLog", () => ({ logDesktop: vi.fn() }));
vi.mock("../nativeAgent", () => ({
  agentConnectionInfo: vi.fn(async () => ({ deviceId: "d", platform: "macos", version: "test", nativeCapture: true })),
  captureNativeWindow: vi.fn(),
  registerAgentWindow: vi.fn(),
  forgetAgentWindow: vi.fn(),
}));
vi.mock("../uiObservation", () => ({
  createUiObserver: () => ({ read: () => ({ capturedAt: "now", viewport: { width: 1, height: 1 }, text: "", truncated: false, regions: [], controls: [], context: {} }), close: () => undefined }),
}));

const state = { symbol: "SHFE.rb2610", duration: 60, view: "trade" } as unknown as UiState;

beforeEach(() => { vi.useFakeTimers(); vi.mocked(requestJson).mockReset(); vi.mocked(captureNativeWindow).mockReset(); });
afterEach(() => { vi.useRealTimers(); });

test.each(["f59f39ecf7aabbf111600f2cd669049f1facab7e", null, undefined])(
  "the window heartbeat reports its build without MCP configuration (%s)", async build => {
    vi.mocked(agentConnectionInfo).mockResolvedValueOnce({ deviceId: "local-device", platform: "macos",
      version: "test", build, nativeCapture: true, mcp: { command: "runtime", args: ["mcp"] } });
    vi.mocked(requestJson).mockResolvedValue({ command: null });
    const view = renderHook(() => useUiControl("http://runtime.test", "owner", state, () => undefined, {}));
    await vi.advanceTimersByTimeAsync(50);
    const device = vi.mocked(requestJson).mock.calls.map(([, init]) => JSON.parse(String(init?.body ?? "{}")))
      .find(body => body.device)?.device;
    expect(device).toBeDefined();
    expect(device.build).toBe(build);
    expect(device).not.toHaveProperty("mcp");
    if (build === undefined) expect(device).not.toHaveProperty("build");
    view.unmount();
  },
);

test("the heartbeat keeps beating while a native capture is stuck", async () => {
  let pulses = 0;
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    pulses += 1;
    // The first pulse dispatches a capture; the client must not wait for it.
    return pulses === 1 ? { command: null, capture: { requestId: "cap", expectedRevision: 0 } } : { command: null };
  });
  vi.mocked(captureNativeWindow).mockImplementation(() => new Promise(() => undefined)); // never resolves
  const view = renderHook(() => useUiControl("http://runtime.test", "owner", state, () => undefined, {}));
  await vi.advanceTimersByTimeAsync(50);
  expect(pulses).toBe(1);
  await vi.advanceTimersByTimeAsync(3000);
  expect(pulses).toBeGreaterThanOrEqual(5);
  expect(captureNativeWindow).toHaveBeenCalledTimes(1);
  view.unmount();
});

test("a finished capture receipt rides on the next heartbeat", async () => {
  const bodies: Array<Record<string, unknown>> = [];
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    bodies.push(body);
    return bodies.length === 1 ? { command: null, capture: { requestId: "cap", expectedRevision: 0 } } : { command: null };
  });
  vi.mocked(captureNativeWindow).mockResolvedValue({ requestId: "cap", observedRevision: 0, capturedAt: "now", image: { mimeType: "image/png", data: "px" } });
  const view = renderHook(() => useUiControl("http://runtime.test", "owner", state, () => undefined, {}));
  await vi.advanceTimersByTimeAsync(1200);
  expect(bodies.some(body => (body.captureReceipt as { requestId?: string } | undefined)?.requestId === "cap")).toBe(true);
  view.unmount();
});

test("reminderCenter {more: true} is applied only once its read landed, rejected when it loaded nothing (#932 R5, Codex r4308006076)", () => {
  const center = (open: boolean, more: Record<string, unknown>) =>
    ({ reminderCenter: { open, filter: "all", unread: 2, today: 500, moreToday: true, moreAsked: 2, moreDone: 2, moreError: "", earlier: false, ...more } }) as unknown as UiState;
  const command = { reminderCenter: { more: true } };
  expect(matches(center(true, { moreDone: 1 }), command)).toBe(false);          // still reading
  expect(refused(center(true, { moreDone: 1 }), command)).toBe(false);
  expect(matches(center(true, {}), command)).toBe(true);
  expect(matches(center(true, {}), { reminderCenter: { more: true, filter: "unread" } })).toBe(false);
  expect(matches(center(false, {}), command)).toBe(false);
  const failed = center(true, { moreError: "当天提醒刚刷新过，再点一次接着读" });
  expect(matches(failed, command)).toBe(false);
  expect(refused(failed, command)).toBe(true);
  expect(refused(failed, { reminderCenter: { open: true } })).toBe(false);
});

test("panel: positions is receipted as applied once the window reports the 持仓面板", async () => {
  const receipts: unknown[] = [];
  let pulses = 0;
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    pulses += 1;
    if (body.receipt) receipts.push(body.receipt);
    return pulses === 1 ? { command: { commandId: "c1", expectedRevision: 0, changes: { panel: "positions" } } } : { command: null };
  });
  const applied: unknown[] = [];
  const barState = { ...state, panel: "positions", positionsBar: "collapsed" } as unknown as UiState;
  const view = renderHook(() => useUiControl("http://runtime.test", "owner", barState, changes => { applied.push(changes); }, {}));
  await vi.advanceTimersByTimeAsync(50);
  expect(applied).toEqual([{ panel: "positions" }]);
  await vi.advanceTimersByTimeAsync(100);
  expect(receipts).toEqual([{ commandId: "c1", status: "applied" }]);
  view.unmount();
});

test("a state change pulses after it settles instead of waiting for the idle heartbeat", async () => {
  const revisions: number[] = [];
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    revisions.push(body.revision);
    return { command: null };
  });
  const view = renderHook(({ current }) => useUiControl("http://runtime.test", "owner", current, () => undefined, {}),
    { initialProps: { current: { ...state, positionsBar: "expanded" } as unknown as UiState } });
  await vi.advanceTimersByTimeAsync(50);
  expect(revisions).toEqual([0]);
  view.rerender({ current: { ...state, positionsBar: "collapsed" } as unknown as UiState });
  await vi.advanceTimersByTimeAsync(300);
  expect(revisions).toEqual([0, 1]);
  view.unmount();
});

test("a drag's run of state changes collapses into one pulse", async () => {
  // While the feed is live the idle heartbeat is 10 s, so the only pulses inside the window are the ones we cause.
  const live = vi.spyOn(runtimeFeed, "feedLiveNow").mockReturnValue(true);
  const revisions: number[] = [];
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    revisions.push(body.revision);
    return { command: null };
  });
  const view = renderHook(({ current }) => useUiControl("http://runtime.test", "owner", current, () => undefined, {}),
    { initialProps: { current: { ...state, dockWidth: 340 } as unknown as UiState } });
  await vi.advanceTimersByTimeAsync(50);
  for (const dockWidth of [341, 342, 343]) {
    view.rerender({ current: { ...state, dockWidth } as unknown as UiState });
    await vi.advanceTimersByTimeAsync(100);
  }
  await vi.advanceTimersByTimeAsync(300);
  expect(revisions).toEqual([0, 3]);
  view.unmount();
  live.mockRestore();
});

it("receipts for the round-eleven surface commands come from the state they change", () => {
  const state = { pinnedSymbols: ["SHFE.rb2701"], review: { month: "2026-09", day: "2026-09-10", filter: "", selectedTradeId: "t1" },
    dialog: "account-detail", strategyDetails: { rules: true }, monitorCards: { research: "collapsed" }, selectedPosition: { symbol: "SHFE.rb2610", direction: "short" }, panel: "ai", positionsTab: "fills", strategyTab: "running" };
  expect(matches(state, { pinnedSymbols: { add: "shfe.rb2701" } })).toBe(true);
  expect(matches(state, { pinnedSymbols: { remove: "SHFE.rb2701" } })).toBe(false);
  expect(matches(state, { reviewExpand: { day: "2026-09-10" } })).toBe(true);
  expect(matches(state, { reviewExpand: { month: "2026-08" } })).toBe(false);
  expect(matches(state, { reviewFilter: "" })).toBe(true);
  expect(matches(state, { reviewSelect: "t1" })).toBe(true);
  expect(matches(state, { reviewSelect: null })).toBe(false);
  expect(matches(state, { dialog: { name: "account-detail", open: true } })).toBe(true);
  expect(matches(state, { dialog: { name: "fee-hint", open: false } })).toBe(true);
  expect(matches(state, { dialog: { name: "account-detail", open: false } })).toBe(false);
  expect(matches(state, { strategyDetails: { section: "rules", open: true } })).toBe(true);
  expect(matches(state, { monitorCard: { name: "research", expanded: false } })).toBe(true);
  expect(matches(state, { selectPosition: { symbol: "shfe.rb2610", direction: "short" } })).toBe(true);
  expect(matches(state, { selectPosition: null })).toBe(false);
  // Retired: aiPage (1.6.6) and aiFilter (2026-09-25) only open the 对话面板 now.
  expect(matches(state, { aiPage: "monitor" })).toBe(true);
  expect(matches(state, { aiFilter: "trading" })).toBe(true);
  expect(matches({ ...state, panel: "strategy" }, { aiFilter: "chat" })).toBe(false);
  // 成交: `trades` (平仓记录) is the old name of `fills`; the strategy panel's tab is reported as is.
  expect(matches(state, { positionsTab: "trades" })).toBe(true);
  expect(matches(state, { positionsTab: "fills" })).toBe(true);
  expect(matches(state, { positionsTab: "orders" })).toBe(false);
  expect(matches(state, { strategyTab: "running" })).toBe(true);
  expect(matches(state, { strategyTab: "detail" })).toBe(false);
  expect(matches({ ...state, panel: "positions" }, { dialog: { name: "positions-drawer", open: true } })).toBe(true);
  expect(matches(state, { dialog: { name: "positions-drawer", open: false } })).toBe(true);
  expect(matches({ ...state, chatSessionId: "s-2" }, { chatSession: { sessionId: "s-2" } })).toBe(true);
  expect(matches({ ...state, chatSessionId: "s-1" }, { chatSession: { sessionId: "s-2" } })).toBe(false);
  expect(matches({ ...state, chatSessionId: "s-9" }, { chatSession: { new: true } })).toBe(true);
  // A command whose target is not mounted yet (no review state) is not applied yet.
  expect(matches({ review: null }, { reviewExpand: { day: "2026-09-10" } })).toBe(false);
  // reviewDay from another view (10-25 回读): the page still loading, or its 今日复盘 not read yet, is not applied —
  // only the block showing that day is.
  expect(matches({ view: "review" }, { reviewDay: "2026-10-23" })).toBe(false);
  expect(matches({ review: { ...state.review } }, { reviewDay: "2026-10-23" })).toBe(false);
  expect(matches({ review: { ...state.review, dailyReview: { tradingDay: "2026-10-22" } } }, { reviewDay: "2026-10-23" })).toBe(false);
  expect(matches({ review: { ...state.review, dailyReview: { tradingDay: "2026-10-23" } } }, { reviewDay: "2026-10-23" })).toBe(true);
});

it("a disclosure receipt reads the named entry, or the card entry when an id is given", () => {
  const state = { disclosures: { "protection-status": true, "tool-card": false, "tool-card:c7": true } };
  expect(matches(state, { disclosure: { name: "protection-status", open: true } })).toBe(true);
  expect(matches(state, { disclosure: { name: "protection-status", open: false } })).toBe(false);
  expect(matches(state, { disclosure: { name: "tool-card", open: false } })).toBe(true);
  expect(matches(state, { disclosure: { name: "tool-card", open: true, id: "c7" } })).toBe(true);
  expect(matches(state, { disclosure: { name: "tool-card", open: true, id: "c8" } })).toBe(false);
  expect(matches(state, { disclosure: { name: "replay-fees", open: true } })).toBe(false); // not mounted
  // AI 审计 2026-10-13: 策略的“手续费” opens 设置 → 高级交易设置; the strategy panel's own tab stays library.
  expect(matches({ strategy: { tab: "library" }, settingsOpen: true, settingsPage: "trade-advanced" }, { strategy: { tab: "fees" } })).toBe(true);
  expect(matches({ strategy: { tab: "library" }, settingsOpen: false, settingsPage: "trade-advanced" }, { strategy: { tab: "fees" } })).toBe(false);
  // AI 审计 2026-10-13: an old name is judged by the surface the client opens for it.
  expect(matches({ disclosures: { "strategy-advanced": true } }, { disclosure: { name: "strategy-full-rules", open: true } })).toBe(true);
  expect(matches({ disclosures: { "strategy-advanced": false } }, { disclosure: { name: "strategy-full-rules", open: true } })).toBe(false);
});

it("the annotation layer's receipts read the highlights, drawings and focus the state reports", () => {
  const state = { symbol: "SHFE.rb2610", highlights: [{ target: "rail:ai", label: "这里", style: "pulse", until: "2026-09-18T00:00:00Z" }],
    chartAnnotations: [{ id: "s1", kind: "hline", price: 3100 }, { id: "z1", kind: "zone", price: 3050, price2: 3080 }], chartFocus: { from: 100, to: 200 } };
  expect(matches(state, { highlight: { target: "rail:ai", durationMs: 6000 } })).toBe(true);
  expect(matches(state, { highlight: { target: "rail:chat", durationMs: 6000 } })).toBe(false);
  expect(matches(state, { highlight: { target: "rail:ai", durationMs: 0 } })).toBe(false);
  expect(matches(state, { highlight: { clear: true } })).toBe(false);
  expect(matches({ ...state, highlights: [] }, { highlight: { clear: true } })).toBe(true);
  expect(matches(state, { chartAnnotations: { op: "add", items: [{ id: "z1", kind: "zone", price: 1, price2: 2 }] } })).toBe(true);
  expect(matches(state, { chartAnnotations: { op: "set", items: [{ id: "z1", kind: "zone", price: 1, price2: 2 }] } })).toBe(false);
  expect(matches(state, { chartAnnotations: { op: "set", items: [{ id: "s1", kind: "hline", price: 1 }, { id: "z1", kind: "zone", price: 1, price2: 2 }] } })).toBe(true);
  expect(matches(state, { chartAnnotations: { op: "clear" } })).toBe(false);
  expect(matches({ ...state, chartAnnotations: [] }, { chartAnnotations: { op: "clear" } })).toBe(true);
  // Drawn on another contract: the snapshot cannot contradict it.
  expect(matches(state, { chartAnnotations: { op: "clear", symbol: "SHFE.au2612" } })).toBe(true);
  expect(matches(state, { chartFocus: { from: 100, to: 200 } })).toBe(true);
  expect(matches(state, { chartFocus: { from: 100, to: 300 } })).toBe(false);
  expect(matches(state, { chartFocus: null })).toBe(false);
  expect(matches({ ...state, chartFocus: null }, { chartFocus: null })).toBe(true);
  // Candidate price and the layout reset (judged by the layout fields it puts back).
  expect(matches({ selectedPrice: 3100, ticketPrice: 3100, ticketPriceMode: "limit" }, { selectedPrice: 3100 })).toBe(true);
  expect(matches({ selectedPrice: 3100 }, { selectedPrice: null })).toBe(false);
  expect(matches({ selectedPrice: 3100, ticketPrice: 3100, ticketPriceMode: "opponent" }, { selectedPrice: 3100 })).toBe(false);
  expect(matches({ selectedPrice: null }, { selectedPrice: null })).toBe(true);
  expect(matches({ dockSide: "right", dockWidth: 340, positionsBar: "collapsed" }, { presentation: { reset: true } })).toBe(true);
  expect(matches({ dockSide: "left", dockWidth: 340, positionsBar: "collapsed" }, { presentation: { reset: true } })).toBe(false);
  // A wide panel (策略面板 opens at half the window) reports its own width after the reset: still applied.
  expect(matches({ dockSide: "right", dockWidth: 590, positionsBar: "collapsed" }, { presentation: { reset: true } })).toBe(true);
  expect(matches({ dockSide: "right", dockWidth: 340, positionsBar: "collapsed", theme: "dark" }, { presentation: { reset: true, theme: "dark" } })).toBe(true);
  // A tour receipt: start = active at step 1 of N; stop = inactive; next/prev = the client's word.
  const steps = [{ target: "a", label: "b" }, { target: "c", label: "d" }];
  expect(matches({ tour: { active: true, step: 1, total: 2 } }, { tour: { op: "start", steps } })).toBe(true);
  expect(matches({ tour: { active: true, step: 2, total: 2 } }, { tour: { op: "start", steps } })).toBe(false);
  expect(matches({ tour: { active: false, step: 0, total: 0 } }, { tour: { op: "stop" } })).toBe(true);
  expect(matches({ tour: { active: true, step: 2, total: 2 } }, { tour: { op: "next" } })).toBe(true);
  expect(matches({ dialog: null }, { dialog: { name: "proposal-confirm", open: false } })).toBe(true);
});

it("a preference command's receipt comes from the reported document (none reported: the receipt stands)", () => {
  const state = { preferences: { voice: { speed: "fast" }, notify: { desktop: { fills: false } } } };
  expect(matches(state, { preference: { key: "voice.speed", value: "fast" } })).toBe(true);
  expect(matches(state, { preference: { key: "voice.speed", value: "slow" } })).toBe(false);
  expect(matches(state, { preference: { key: "notify.desktop.fills", value: false } })).toBe(true);
  expect(matches(state, { preference: { key: "chart.missing", value: 1 } })).toBe(false);
  expect(matches({ preferences: null }, { preference: { key: "voice.speed", value: "slow" } })).toBe(true);
});

test("three consecutive 422 heartbeats surface as an unregistered window (and recover on the next 200)", async () => {
  let pulses = 0;
  vi.mocked(requestJson).mockImplementation(async (_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.closing) return {};
    pulses += 1;
    if (pulses <= 3) throw Object.assign(new Error("422 ..."), { status: 422, detail: { detail: [{ loc: ["body", "state", "chartAnnotations", 0, "label"], msg: "String should have at most 8 characters" }] } });
    return { command: null };
  });
  const view = renderHook(() => ({ control: useUiControl("http://runtime.test", "owner", state, () => undefined, {}), health: useUiControlHealth() }));
  await vi.advanceTimersByTimeAsync(50);
  expect(view.result.current.health.registered).toBe(true);
  await vi.advanceTimersByTimeAsync(1200);
  expect(pulses).toBeGreaterThanOrEqual(3);
  expect(view.result.current.health.registered).toBe(false);
  expect(view.result.current.health.detail).toContain("chartAnnotations");
  expect(logDesktop).toHaveBeenCalledWith("ui-pulse", expect.stringContaining("422"), expect.stringContaining("chartAnnotations"));
  await vi.advanceTimersByTimeAsync(1500);
  expect(view.result.current.health.registered).toBe(true);
  view.unmount();
});

test("调研页命令的回执：deskRejudge 看页面已发出重新判断，deskSource 看资料来源高亮的那一条（与 Runtime 同一规则）", () => {
  const state = (rejudge: unknown, tab = "desk") => ({ panel: "news", news: { tab, desk: { rejudge, highlight: "M.price" } } }) as unknown as UiState;
  expect(matches(state(null), { news: { deskRejudge: true } })).toBe(false);
  expect(matches(state({ status: "sending", message: "…" }), { news: { deskRejudge: true } })).toBe(true);
  expect(matches(state(null), { news: { deskSource: "M.price" } })).toBe(true);
  expect(matches(state(null), { news: { deskSource: "D.spot_basis" } })).toBe(false);
  expect(matches(state({ status: "limited" }, "flash"), { news: { deskRejudge: true } })).toBe(false);
});

test("策略编辑卡命令的回执与 Runtime 的 _editor_matches 同一规则：close 看关、edit 看哪条、habits 不够成交也算、save 看保存中 / 已存 / 报错", () => {
  const state = (card: Record<string, unknown> | undefined) => ({ strategyEditor: card }) as unknown as UiState;
  const open = { open: true, strategyId: null, start: "blank", prefill: { eligible: false }, ask: null, saving: false, savedStrategyId: null, error: null };
  expect(matches(state({ open: false }), { strategyEditor: { close: true } })).toBe(true);
  expect(matches(state(open), { strategyEditor: { close: true } })).toBe(false);
  expect(matches(state({ open: false }), { strategyEditor: { new: true } })).toBe(false);
  expect(matches(state(open), { strategyEditor: { new: true } })).toBe(true);
  expect(matches(state({ ...open, strategyId: "s1" }), { strategyEditor: { new: true } })).toBe(false);
  expect(matches(state({ ...open, strategyId: "s1" }), { strategyEditor: { edit: "s1" } })).toBe(true);
  expect(matches(state({ ...open, strategyId: "s2" }), { strategyEditor: { edit: "s1" } })).toBe(false);
  // Too few trades: habits opens blank and the snapshot says why — that is the applied state.
  expect(matches(state(open), { strategyEditor: { new: true, start: "habits" } })).toBe(true);
  expect(matches(state({ ...open, prefill: { eligible: true } }), { strategyEditor: { start: "habits" } })).toBe(false);
  expect(matches(state(open), { strategyEditor: { start: "template:double_ma" } })).toBe(false);
  expect(matches(state({ ...open, start: "template:double_ma" }), { strategyEditor: { start: "template:double_ma" } })).toBe(true);
  expect(matches(state(open), { strategyEditor: { ask: "止损改成 20 跳" } })).toBe(false);
  expect(matches(state({ ...open, ask: { text: "止损改成 20 跳", status: "running" } }), { strategyEditor: { ask: "止损改成 20 跳" } })).toBe(true);
  expect(matches(state(open), { strategyEditor: { answers: { a: "b" } } })).toBe(false);
  expect(matches(state(open), { strategyEditor: { save: true } })).toBe(false);
  expect(matches(state({ ...open, saving: true }), { strategyEditor: { save: true } })).toBe(true);
  expect(matches(state({ ...open, error: "没存上" }), { strategyEditor: { save: true } })).toBe(true);
  expect(matches(state(undefined), { strategyEditor: { save: true } })).toBe(true);
  // A save closes the card at once on success: the closed snapshot's saved strategy is the proof (was unknown).
  expect(matches(state({ open: false, savedStrategyId: "s9" }), { strategyEditor: { save: true } })).toBe(true);
  // While the prefill / template loads, new / start are not applied yet (a following command would be stale).
  expect(matches(state({ ...open, loading: "正在读你的成交…" }), { strategyEditor: { new: true } })).toBe(false);
  expect(matches(state({ ...open, start: "template:double_ma", loading: "正在载入模板…" }), { strategyEditor: { start: "template:double_ma" } })).toBe(false);
  // The retired 策略设计窗's name is judged by the card.
  expect(matches({ ...state(open), dialog: null } as unknown as UiState, { dialog: { name: "strategy-designer", open: true } })).toBe(true);
  expect(matches({ ...state({ open: false }), dialog: null } as unknown as UiState, { dialog: { name: "strategy-designer", open: true } })).toBe(false);
});
