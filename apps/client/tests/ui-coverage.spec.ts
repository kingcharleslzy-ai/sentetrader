import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";
import { waitForWorkspace } from "./support/workspace";
import { classify, staleKnownGaps, type Control } from "./support/uiCoverage";

/**
 * AI operability contract, step three — coverage audit. Opens every surface through UI commands,
 * lists its interactive controls (the a11y tree the agent's snapshot is built from) and classifies
 * each against tests/support/uiCoverage.ts. Fails on a control nobody accounted for, or on a control
 * the agent can reach but the snapshot cannot see; prints the known-gap burn-down list.
 */
type Raw = Omit<Control, "surface" | "observed"> & { inDialog: boolean };
const collectControls = (page: Page) => page.evaluate((): Raw[] => {
  // What the user can act on right now: rendered, not aria-hidden, and inside the viewport (the snapshot
  // additionally lists scrolled-out content of scroll regions, so anything counted here must be in it).
  const visible = (el: Element) => {
    if (el.closest("[aria-hidden=true]")) return false;
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0"
      && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
  };
  // Same derivation as uiObservation.ts so labels compare 1:1 with the snapshot's controls.
  const label = (el: Element) => el.getAttribute("aria-label")
    || (el.getAttribute("aria-labelledby") || "").split(" ").map(id => document.getElementById(id)?.textContent || "").join(" ").trim()
    || (el instanceof HTMLInputElement ? el.placeholder || el.closest("label")?.textContent?.trim() || "" : "")
    || el.textContent?.trim().slice(0, 160) || el.tagName.toLowerCase();
  return [...document.querySelectorAll("button, a[href], [role=tab], [role=menuitem], [role=switch], [role=checkbox], [role=radio], [role=separator], input, select, textarea, summary")]
    .filter(visible).map(el => ({
      role: el.getAttribute("role") || (el instanceof HTMLInputElement ? `input:${el.type}` : el.tagName.toLowerCase()),
      label: label(el), disabled: el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true",
      inDialog: Boolean(el.closest("dialog[open], [role=dialog]")),
      // Where it sits: its settings category, else the nearest agent target (e.g. the quick-skills bar).
      group: el.closest(".app-settings-section")?.getAttribute("aria-label") || el.closest("[data-agent-target]")?.getAttribute("data-agent-target") || "",
    }));
});

// AI 简报订阅 (2026-10-05): a Runtime before the subscriptions answers neither the list nor the
// multi-product briefs; the audit then injects one of each so the 订阅品种 block, the 添加品种 picker
// and 通知's product chips render. A Runtime that serves them is passed through untouched.
// 我的流程 (2026-10-06): an account with no routine yet gets one injected so 设置 → 记忆's rows (运行 / 改 / 删 and
// the 删 confirmation) render; once the chat turn below saves a real one, the Runtime's list passes through.
async function routineRoutes(target: Page | BrowserContext) {
  await target.route("**/api/routines", async route => {
    if (route.request().method() !== "GET") return route.fallback();
    const response = await route.fetch().catch(() => undefined);
    const data = response?.ok() ? await response.json() : null;
    if (data?.items?.length) return route.fulfill({ response: response! });
    await route.fulfill({ json: { items: [{ id: "rt-audit", name: "收盘检查", stepCount: 2, createdAt: "2026-10-06T01:00:00Z",
      updatedAt: "2026-10-06T01:00:00Z", lastRunAt: null, steps: [{ kind: "read", kindLabel: "读", text: "看持仓" },
        { kind: "alert", kindLabel: "提醒", text: "设夜盘提醒" }] }], limits: { routines: 5, steps: 10, nameChars: 12 } } });
  });
}

async function briefRoutes(target: Page | BrowserContext) {
  const fallback = (json: () => unknown) => async (route: Route) => {
    const response = await route.fetch().catch(() => undefined);
    if (response?.ok()) return route.fulfill({ response });
    await route.fulfill({ json: json() });
  };
  const brief = (id: string, product: string, productName: string, text: string) => ({ id, kind: "news", text, source: "audit", product, productName,
    symbol: `${product}2601`, publishedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + 3_600_000).toISOString() });
  await target.route("**/api/app/brief-subscriptions", fallback(() => ({ products: [{ product: "SHFE.rb", name: "螺纹钢", symbol: "SHFE.rb2601" }], limit: 3, source: "default" })));
  await target.route("**/api/app/briefs", fallback(() => ({ items: [brief("audit-rb", "SHFE.rb", "螺纹钢", "审计用螺纹简报"), brief("audit-au", "SHFE.au", "黄金", "审计用黄金简报")] })));
  // 锁屏提醒 (2026-10-06): a configured Runtime with one other device, so the device list shows its 移除.
  await target.route("**/api/app/push", async route => route.fulfill({ json: { configured: true, publicKey: "BAEC_w",
    devices: [{ deviceId: "audit-other-device", platform: "Mac · Safari", createdAt: new Date().toISOString(), lastOkAt: null, lastError: null }] } }));
}

// 统一提醒 (#932, 2026-10-15): a Runtime before the reminders record (PR-1) answers neither route; the audit then
// serves one trading day — unread and read rows, a 保本 question waiting with its two answers, a row with its result
// state, a night-session row — and one earlier day, so the bell's popover, the phone's 提醒 page and 最新提醒 render.
async function reminderRoutes(target: Page | BrowserContext) {
  const day = new Date().toISOString().slice(0, 10);
  const at = (clock: string, offsetDays = 0) => new Date(new Date(`${day}T${clock}:00+08:00`).getTime() - offsetDays * 86_400_000).toISOString();
  const item = (id: string, kind: string, clock: string, name: string, body: string, extra: Record<string, unknown> = {}) =>
    ({ id, kind, tradingDay: day, createdAt: at(clock), name, title: name, body, read: false, target: { type: "chat", cardId: `audit-${id}` }, ...extra });
  const today = [
    item("r-close", "market_close", "14:50", "螺纹2701 · 沪金2612", "收盘前 5 分钟，两笔持仓要过夜"),
    item("r-be", "breakeven", "14:31", "沪金2612", "已赚到 1 倍风险，把止损移到成本？", { actions: [
      { kind: "breakeven", label: "移到成本", breakevenId: "audit-be", choice: "move" }, { kind: "breakeven", label: "不用", breakevenId: "audit-be", choice: "decline" }] }),
    item("r-stop", "stop_triggered", "10:05", "沪银2612", "跌破止损，平多 1 手", { state: { text: "平仓单 已成交 ×1", tone: "ok" }, read: true }),
    item("r-unfilled", "order_unfilled", "09:20", "沪金2612", "买开 1 手挂了 15 分钟没成交", { state: { text: "仍在排队 0/1", tone: "wait" }, target: { type: "orders" } }),
    item("r-morning", "morning_desk", "08:45", "沪金 · 螺纹", "沪金、螺纹钢", { target: { type: "research", page: "sheet" }, read: true }),
    item("r-brief", "brief", "22:31", "贵金属", "美国 CPI 高于预期", { createdAt: at("22:31", 1), target: { type: "news" }, read: true }),
  ];
  await target.route("**/api/reminders/read", async route => {
    const response = await route.fetch().catch(() => undefined);
    if (response?.ok()) return route.fulfill({ response });
    await route.fulfill({ json: { unread: 0 } });
  });
  await target.route("**/api/reminders?*", async route => {
    const response = await route.fetch().catch(() => undefined);
    if (response?.ok()) return route.fulfill({ response });
    const earlier = new URL(route.request().url()).searchParams.get("scope") === "earlier";
    await route.fulfill({ json: earlier
      ? { items: [{ ...item("r-review", "daily_review", "15:15", "复盘", "当日 +1,240，计划内 3 笔"), tradingDay: "2026-10-14", createdAt: at("15:15", 1), read: true,
        target: { type: "review", tradingDay: "2026-10-14" } }], unread: 3, tradingDay: day, nextCursor: null }
      : { items: today, unread: 3, tradingDay: day, nextCursor: "audit-earlier" } });
  });
}

// 手机版新结构 (2026-10-06): the fake Runtime runs no strategy and has no comparison; the phone audit
// injects one running instance and one finished comparison so the 策略 cards and 回测结果页 render.
async function strategyRoutes(target: BrowserContext) {
  const plan = { timeframeSeconds: 86400, rules: {}, questions: [] };
  await target.route("**/api/strategy-plans", route => route.request().method() === "GET"
    ? route.fulfill({ json: { items: [{ strategyId: "audit-lib", name: "审计用均线", symbol: "*", version: 1, status: "draft", planSummary: "短均线上穿长均线就做多。",
      latestBacktest: { backtestId: "audit-bt", netProfit: 900, maxDrawdown: 300, winRate: 50, trades: 16 },
      definition: { schemaVersion: 2, plan, instrumentScope: { mode: "any", symbols: [] } } }],
      executions: [{ strategyId: "audit-run", name: "审计用均线", symbol: "SHFE.rb2601", version: 1, status: "monitoring",
      signalAction: "confirm", definition: { schemaVersion: 2, plan, libraryStrategyId: "audit-lib" } }] } }) : route.fallback());
  await target.route("**/api/strategies/audit-run/monitor", route => route.fulfill({ json: { strategyId: "audit-run", status: "monitoring",
    effectiveSignalAction: "confirm", today: null, recentAttempts: [], simulationOnly: true, liveTrading: "disabled",
    position: { direction: "long", volume: 1 }, signalsToday: 1, researchClash: { stance: "偏空", direction: "long", text: "今日判断偏空，这个策略今天在做多" } } }));
  const segment = (net: number) => ({ trades: 8, wins: 4, winRate: 50, net, avgNet: net / 8, maxDrawdown: 300 });
  const lab = { id: "audit-lab", symbol: "SHFE.rb2601", status: "done", source: "server", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
    request: { symbol: "SHFE.rb2601", tradingDays: 60, slippageTicks: 1 }, skipped: null, chosen: null,
    variants: [{ id: "v1", label: "审计 · 快线 5", plan }, { id: "v2", label: "审计 · 快线 10", plan }],
    results: [{ variantId: "v1", label: "审计 · 快线 5", summary: { netProfit: 900, maxDrawdown: 300, trades: 16 }, inSample: segment(1200), outSample: segment(-300), splitTime: 70,
      equityCurve: [{ time: 0, equity: 1_000_000 }, { time: 100, equity: 1_000_900 }] },
    { variantId: "v2", label: "审计 · 快线 10", summary: { netProfit: 700, maxDrawdown: 200, trades: 16 }, inSample: segment(500), outSample: segment(200), splitTime: 70 }],
    ranking: { tried: 2, best: "v1", items: [{ variantId: "v1", isRank: 1, oosRank: 2, decay: null, flags: ["best", "overfit", "negative_oos"] }, { variantId: "v2", isRank: 2, oosRank: 1, decay: 0.8, flags: [] }] } };
  await target.route("**/api/strategy-labs?*", route => route.fulfill({ json: { items: [{ id: lab.id, symbol: lab.symbol, status: "done", source: "server",
    createdAt: lab.createdAt, updatedAt: lab.updatedAt, tried: 2, best: "v1", name: "审计", timeframeSeconds: 86400, bestOutNet: -300, noProfit: false }] } }));
  await target.route("**/api/strategy-labs/audit-lab", route => route.fulfill({ json: lab }));
}

/** 习惯对照 needs five 计划内 trades; the fake account has none, so the read carries one comparison. */
function auditHabits() {
  return { status: "ok", basis: "gross", days: 90, currency: "CNY", rules: ["只做计划内：只留开仓标为计划内的单"],
    actual: { count: 9, pnl: -720 }, planned: { count: 6, pnl: 1420 }, gap: 2140,
    split: [{ tag: "remind", label: "提醒后", count: 1, pnl: -960 }, { tag: "adhoc", label: "临时", count: 2, pnl: -1180 }],
    holding: { lateLosers: { count: 1, extra: 746.67, limitMinutes: 40 }, earlyWinners: null },
    picks: [{ id: "audit:habit", tradingDay: "2026-10-09", symbol: "SHFE.rb2701", direction: "long", volume: 1, tag: "adhoc",
      pnl: -1380, reason: "计划外亏损" }] };
}

function auditDailyReview() {
  const leg = (id: string, clock: string, action: string, tag: string, tagLabel: string, pnl: number | null) => ({ id, orderId: id, tradeIds: [id],
    symbol: "SHFE.rb2701", name: "螺纹钢", time: `2026-10-09T${clock}+08:00`, clock, side: "", volume: 1, price: 3150, action, source: "manual",
    sourceLabel: "手动", tag, tagLabel, reason: "没有记下理由", reminder: null, pnl, pnlBasis: pnl === null ? null : "gross", approximate: false });
  const review = (day: string) => ({ version: 1, tradingDay: day, accountId: "audit", generatedAt: `${day}T07:15:00+00:00`, reason: "auto",
    stats: { legs: 2, planned: 1, remind: 1, adhoc: 0, closes: 1, wins: 1 }, dayPnl: 860, commission: 0,
    legs: [leg("L1", "09:03", "买开 螺纹钢 RB2701 × 1 @ 3,150", "plan", "计划内", null), leg("L2", "10:48", "平多 螺纹钢 RB2701 × 1 @ 3,188", "remind", "提醒后", 860)],
    summary: { text: "审计用复盘：两笔里一笔在计划内。", source: "template", basis: "按成交来源和提醒时间统计，没有用模型" },
    observation: { text: "你看到提醒后会先平一半", legs: ["L1", "L2"], tradeIds: ["L1", "L2"], key: "audit", status: "pending" }, rules: ["计划内：…"] });
  return { review: review("2026-10-09"), days: ["2026-10-09", "2026-10-08"], generatedAt: null, generateAt: "15:15", rules: ["计划内：…"] };
}

test("every user-operable control is commandable or reachable, and visible to the snapshot", async ({ page, browser }, testInfo) => {
  test.setTimeout(1_200_000);  // 20 min (was 12): the run measured 11.6 min locally (多 AI 研讨 2026-10-11) and grows with each surface — 12 min left no headroom under CI load (runs under playwright.execution.config.ts, workers: 1)
  const runtime = `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_RUNTIME_PORT || "8003"}`;
  const headers = { Authorization: "Bearer ai-trader-execution-e2e-token", Origin: `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_CLIENT_PORT || "4176"}` };
  // Keep one real library item: an empty list hides its 更多操作 and delete-confirm surfaces.
  const menuPlanResponse = await page.request.post(`${runtime}/api/strategy-plans`, { headers, data: {
    plan: { name: "审计策略操作菜单", description: "界面覆盖用，不上线", direction: "long", timeframeSeconds: 300,
      maxPosition: 1, stopLossTicks: 20, rules: { entryLong: { conditions: [{ label: "收盘价高于零",
        left: { indicator: "close", timeframe: 300 }, operator: "gt", right: { indicator: "constant", value: 0 } }] } } },
    instrumentScope: { mode: "any", symbols: [] }
  } });
  expect(menuPlanResponse.ok()).toBe(true);
  const menuPlanId = (await menuPlanResponse.json()).strategyId as string;
  const deviceId = crypto.randomUUID();
  await page.addInitScript(id => localStorage.setItem("ai-trader:web-device", id), deviceId);
  // 未关联的补充 only exist after a real fill replaced a backfilled one; the fake account never has
  // one, so the review's read carries one injected 补充 for the audit to see the fold and its 删除.
  await page.route("**/api/account/pnl*", async route => {
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({ response });
    await route.fulfill({ response, json: { ...(await response.json()), unlinkedNotes: [
      { roundId: "link:AITaudit:close", text: "拆单前的补充", via: "voice", updatedAt: "2026-10-02T06:30:00+00:00" }] } });
  });
  // 今日复盘 (2026-10-06) exists only after a 15:15 on a trading day with fills; the fake account has none,
  // so the review read carries one injected review (with a pending observation) for its controls.
  await page.route("**/api/account/daily-review*", async route => {
    const response = await route.fetch().catch(() => undefined);
    const data = response?.ok() ? await response.json() : null;
    if (data?.review) return route.fulfill({ response: response! });
    await route.fulfill({ json: auditDailyReview() });
  });
  await page.route("**/api/account/habits*", async route => {
    const response = await route.fetch().catch(() => undefined);
    const data = response?.ok() ? await response.json() : null;
    if (data?.status === "ok") return route.fulfill({ response: response! });
    await route.fulfill({ json: auditHabits() });
  });
  await briefRoutes(page);
  await routineRoutes(page);
  await reminderRoutes(page);
  await page.goto("/");
  await waitForWorkspace(page);
  await expect(page.getByTestId("futures-decision-workspace")).toHaveAttribute("data-connection", "connected", { timeout: 30_000 });
  // Every helper addresses one browser device's window (the phone layout runs in a second device, below).
  const listWindows = async (device = deviceId): Promise<{ windowId: string; online: boolean }[]> =>
    (await (await page.request.get(`${runtime}/api/app/ui/windows?deviceId=${device}`, { headers })).json()).windows;
  const currentWindow = async (device = deviceId) => (await listWindows(device)).filter(item => item.online).at(-1)?.windowId ?? "";
  await expect.poll(() => currentWindow()).not.toBe("");
  const detail = async (device = deviceId) => { const response = await page.request.get(`${runtime}/api/app/ui/windows/${await currentWindow(device)}`, { headers }); return response.json(); };
  const settled = (device = deviceId) => expect.poll(async () => { const first = (await detail(device)).revision; await page.waitForTimeout(800); return first === (await detail(device)).revision; }, { timeout: 20_000 }).toBe(true);
  await settled();
  // 新闻改版 (2026-10-07 第六批): the execution Runtime's recorded fake model screens 沪金's samples and writes its
  // 今日新闻要点. Read the view once (that queues the items) and wait for the judged events and the brief, so the
  // panel below shows the tiers, a merged event's 展开 and the 次要 fold.
  await expect.poll(async () => {
    const view = await (await page.request.get(`${runtime}/api/research/flash?hours=24&product=SHFE.au&limit=50`, { headers })).json();
    return view.screen?.pending === 0 && view.events?.some((event: { items: unknown[] }) => event.items.length > 1) && view.brief?.status === "ready";
  }, { timeout: 45_000, intervals: [1000] }).toBe(true);
  // 调研页改版 (2026-10-07 第七批): the recorded fake model also writes 沪金's 今日判断 (the recorded 10-07 figures stand in
  // for AKShare), so the 调研 tab below shows its verdict, key figures, both columns and the four folds.
  await expect.poll(async () => (await (await page.request.get(`${runtime}/api/research/desk?symbol=SHFE.au2612`, { headers })).json()).judgment?.status,
    { timeout: 60_000, intervals: [2000] }).toBe("ready");
  // 多 AI 研讨 (2026-10-11): one debate on 沪金's 今日议题 and one on its first 重要 快讯 event (the recorded fake model
  // speaks both sides' turns and judges), so the 快讯 rows show 研讨 and 已研讨 and the 辩论 page below opens on a result.
  const flashView = await (await page.request.get(`${runtime}/api/research/flash?hours=24&product=SHFE.au&limit=50`, { headers })).json();
  const majorEvent = (flashView.events as { id: string; tier: string }[]).find(event => event.tier === "major")!;
  const deskDebate = await (await page.request.post(`${runtime}/api/research/debates`, { headers, data: { symbol: "SHFE.au2612" } })).json() as { id: string };
  const eventDebate = await (await page.request.post(`${runtime}/api/research/debates`, { headers, data: { symbol: "SHFE.au2612", eventId: majorEvent.id } })).json() as { id: string };
  for (const id of [deskDebate.id, eventDebate.id]) {
    await expect.poll(async () => (await (await page.request.get(`${runtime}/api/research/debates/${id}`, { headers })).json()).status,
      { timeout: 30_000, intervals: [500] }).toBe("done");
  }
  const command = async (changes: Record<string, unknown>, device = deviceId) => {
    // "rejected" is retried too: boardSort/boardExchange write the preference back ~500 ms after they
    // apply, and the state that write produces carries a newer revision, so a command enqueued in that
    // window is refused as stale (the board's 3 s quote polling does not change the state) — a race,
    // not a verdict. A genuinely broken command stays rejected after the attempts.
    let status = "timeout";
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const windowId = await currentWindow(device);
      const current = await detail(device);
      const receipt = await (await page.request.post(`${runtime}/api/app/ui/commands?waitMs=5000`, { headers, data: {
        commandId: crypto.randomUUID(), windowId, expectedRevision: current.revision, changes } })).json();
      status = receipt.status as string;
      if (status === "applied") return status;
      await page.waitForTimeout(400);
    }
    return status;
  };

  // 画线第二版 (2026-10-09): one trendline on the chart's contract, so the style bar, 精确设置, the drawing's own
  // right-click menu and 全部删除's confirmation have something to act on.
  const chartSymbol = (await detail()).state.symbol as string;
  const workbench = await (await page.request.get(`${runtime}/api/markets/${encodeURIComponent(chartSymbol)}/workbench?duration=300`, { headers })).json();
  const barTimes = (workbench.bars as { datetime: number; close: number }[]).map(bar => ({ t: Math.floor(bar.datetime / 1e9), p: bar.close }));
  const auditLine = await (await page.request.post(`${runtime}/api/market/drawings`, { headers, data: {
    symbol: chartSymbol, kind: "trendline", points: { anchors: [barTimes.at(-30), barTimes.at(-5)] }, style: { color: "amber" } } })).json();
  const lineId = auditLine.drawing.id as string;
  // Surfaces and the commands that open them. Review-page dialogs need the review view first.
  const surfaces: Array<[string, Record<string, unknown>[], boolean]> = [
    ["trade", [], true],
    ["quick-volume", [{ panel: "order" }, { quickVolume: 5 }], true],
    ["", [{ quickVolume: 1 }], false],
    // 图表指标就地编辑 (2026-10-02): the chart labels' popovers, opened as disclosures.
    ["chart-ma-editor", [{ disclosure: { name: "chart-ma-editor", open: true } }], true],
    ["", [{ disclosure: { name: "chart-ma-editor", open: false } }], false],
    ["chart-pane-editor", [{ disclosure: { name: "chart-pane-editor", id: "kd", open: true } }], true],
    ["", [{ disclosure: { name: "chart-pane-editor", id: "kd", open: false } }], false],
    ["chart-pane-add", [{ disclosure: { name: "chart-pane-add", open: true } }], true],
    ["", [{ disclosure: { name: "chart-pane-add", open: false } }], false],
    ["ai-chat", [{ panel: "ai" }], true],
    ["chat-history", [{ disclosure: { name: "chat-history", open: true } }], true],
    // 价格阶梯 removed 2026-10-07: the legacy `panel: depth` (from the 对话 panel) must open the 下单面板.
    ["", [{ panel: "depth" }], false],
    ["order", [{ panel: "order" }], true],
    ["order-price-fill", [{ selectedPrice: 823.5 }], true], ["order-close", [{ ticketMode: "close" }], true],
    // 意图重构第 4 步 (2026-10-16): a SHFE position's close page shows 自动 / 平今 / 平昨 and its "?" (the chart is
    // CFFEX.IF2609 above, which has no 今昨 choice); then back to the chart contract the rest of the audit expects.
    ["order-close-shfe", [{ selectPosition: { symbol: "SHFE.au2612", direction: "long" } }], true],
    ["", [{ symbol: "CFFEX.IF2609" }], false],
    ["ai-strategy", [{ panel: "strategy" }, { strategyTab: "library" }], true],
    ["ai-strategy-actions", [{ disclosure: { name: "strategy-actions", id: `library:${menuPlanId}`, open: true } }], true],
    ["ai-strategy-delete-confirm", [{ disclosure: { name: "strategy-delete-confirm", id: `library:${menuPlanId}`, open: true } }], true],
    ["", [{ disclosure: { name: "strategy-delete-confirm", id: `library:${menuPlanId}`, open: false } }], false],
    // 策略编辑卡 (2026-10-08): a new card (the fake account has too few trades: blank), its 更多 and the old
    // strategy-new name; then a template's card with the preview backtest and its 回测明细.
    ["ai-strategy-editor", [{ strategyEditor: { new: true } }, { disclosure: { name: "strategy-editor-more", open: true } }], true],
    ["", [{ strategyEditor: { close: true } }, { disclosure: { name: "strategy-new", open: true } }, { disclosure: { name: "strategy-new", open: false } }], false],
    ["ai-strategy-editor-template", [{ strategyEditor: { new: true, start: "template:double_ma" } }, { disclosure: { name: "strategy-editor-backtest", open: true } }], true],
    // 第二版 (2026-10-08): a condition side's 指标选择器, ④'s 更多 reached from the 流程图 (focus), and one "?" open.
    ["ai-strategy-editor-picker", [{ disclosure: { name: "strategy-editor-picker", id: "entryLong:1:right", open: true } }], true],
    ["ai-strategy-editor-sizing", [{ disclosure: { name: "strategy-editor-picker", id: "entryLong:1:right", open: false } },
      { disclosure: { name: "strategy-editor-sizing-more", open: true } }, { strategyEditor: { focus: "limits" } }], true],
    ["ai-strategy-editor-help", [{ disclosure: { name: "help-tip", id: "strategy-editor:flow", open: true } }], true],
    ["", [{ disclosure: { name: "help-tip", id: "strategy-editor:flow", open: false } }], false],
    // 存为草稿 → its 结果页 (saved, backtested on the fake history).
    ["ai-strategy-result", [{ strategyEditor: { save: true } }, { disclosure: { name: "strategy-ask-ai", open: true } }], true],
    ["ai-strategy-detail", [{ strategyTab: "detail" }], true], ["ai-strategy-running", [{ strategyTab: "running" }], true],
    // 打法本 (2026-10-25): the 打法 tab and its 新建 form.
    ["ai-strategy-playbooks", [{ strategyTab: "playbooks" }], true],
    ["ai-strategy-playbook-editor", [{ disclosure: { name: "playbook-editor", open: true } }], true],
    ["", [{ disclosure: { name: "playbook-editor", open: false } }], false],
    ["", [{ strategyTab: "library" }], false],
    ["ai-positions", [{ panel: "positions" }], true], ["ai-positions-fills", [{ fillsSource: "all" }], true],
    ["positions-bar", [{ panel: null }, { positionsBar: "expanded" }], true], ["orders-tab", [{ positionsTab: "orders" }], true],
    ["orders-history", [{ ordersHistory: true }], true], ["fills-tab", [{ positionsTab: "fills" }], true],
    ["review", [{ positionsBar: "collapsed" }, { view: "review" }], true],
    // 今日复盘 (2026-10-06): the block's 怎么算的 fold (the review itself is injected, see the route above).
    ["review-daily-how", [{ disclosure: { name: "review-how", open: true } }], true],
    ["", [{ disclosure: { name: "review-how", open: false } }], false],
    // 习惯对照 (2026-10-25): its 怎么算的 fold (the comparison is injected, see the route above).
    ["review-habits-how", [{ disclosure: { name: "habits-how", open: true } }], true],
    ["", [{ disclosure: { name: "habits-how", open: false } }], false],
    // 未关联的补充 (2026-10-04): the fold appears only with such 补充; the audit injects one (see the route below).
    ["review-unlinked-notes", [{ disclosure: { name: "unlinked-notes", open: true } }], true],
    ["", [{ disclosure: { name: "unlinked-notes", open: false } }], false],
    ["dialog-account-detail", [{ dialog: { name: "account-detail", open: true } }], true], ["", [{ dialog: { name: "account-detail", open: false } }], false],
    ["dialog-fee-hint", [{ dialog: { name: "fee-hint", open: true } }], true], ["", [{ dialog: { name: "fee-hint", open: false } }], false],
    ["", [{ view: "trade" }], false],
    ...(["account", "ai", "memory", "memory-advanced", "voice", "voice-advanced", "remote-keys", "remote-diagnostics", "chart", "chart-advanced",
           "trade", "trade-advanced", "ui", "shortcuts", "data", "about", "feedback"] as const)
      .map(p => [`settings-${p}`, [{ settingsPage: p }], true] as [string, Record<string, unknown>[], boolean]),
    // 设置 → 账户 (2026-10-08): the 实盘 switch only accepts staying off (open: true is a 422 with the reason).
    // 档位's plan-quota fold needs a cloud model catalog, which this local Runtime has not (unit-tested instead).
    ["", [{ settingsPage: "account" }, { disclosure: { name: "live-trading", open: false } }], false],
    // 我的流程 (2026-10-06): a routine row's 删 confirmation (the injected routine, see routineRoutes).
    ["settings-memory-routine-delete", [{ settingsPage: "memory" }, { disclosure: { name: "routine-delete", id: "rt-audit", open: true } }], true],
    ["", [{ disclosure: { name: "routine-delete", id: "rt-audit", open: false } }], false],
    // 问题与建议 (2026-10-06): the diagnostics preview opened by its own disclosure.
    ["settings-feedback-diagnostics", [{ settingsPage: "feedback" }, { disclosure: { name: "feedback-diagnostics", open: true } }], true],
    ["", [{ disclosure: { name: "feedback-diagnostics", open: false } }], false],
    // 1.6.5 page names still open their new home (fees → the 高级交易设置 second-level page).
    ["", [{ settingsPage: "fees" }], false],
    ["", [{ settingsOpen: false }], false],
    // 通用提醒 (2026-10-25: was 设置 → 提醒): the 提醒 panel's tab, its AI 简讯 订阅品种 block with the 添加品种 picker
    // (2026-10-05) and 锁屏推送's device list (2026-10-06).
    ["reminders-general", [{ reminders: { tab: "general" } }], true],
    ["reminders-general-briefs", [{ disclosure: { name: "brief-subscriptions", open: true } }, { disclosure: { name: "brief-add", open: true } }], true],
    ["", [{ disclosure: { name: "brief-subscriptions", open: false } }], false],
    ["reminders-general-push", [{ disclosure: { name: "push-devices", open: true } }], true],
    ["", [{ disclosure: { name: "push-devices", open: false } }, { panel: null }], false],
    ...["market-session", "help", "contract-drawer", "replay-setup", "emergency-stop", "resume-execution"].flatMap(name => [
      [`dialog-${name}`, [{ dialog: { name, open: true } }], true], ["", [{ dialog: { name, open: false } }], false]] as Array<[string, Record<string, unknown>[], boolean]>),
    // 报价总表 (2026-10-01): the view itself, an exchange chip and a column sort, then back to trade.
    ["board", [{ view: "board" }], true],
    ["board-filter", [{ boardExchange: "SHFE" }, { boardSort: { key: "volume", order: "asc" } }], true],
    // 外盘 (2026-10-18, #976): the foreign group's quote-only rows and their source line.
    ["board-foreign", [{ boardExchange: "FOREIGN" }], true],
    ["", [{ view: "trade" }], false],
    // 列可自定义 (2026-10-02): the popover opens by its own disclosure. The board re-entry comes
    // first because boardSort/boardExchange debounce-write the preference ~500 ms later and the
    // revision bump that follows would reject a command enqueued in between.
    ["board-columns", [{ view: "board" }, { disclosure: { name: "board-columns", open: true } }], true],
    ["", [{ disclosure: { name: "board-columns", open: false } }, { view: "trade" }], false],
    ["", [{ view: "trade" }], false],
    // 提醒 (#932, 2026-10-15; the right rail's panel since 2026-10-25; the bell itself is on trade): 我的提醒 with 到价提醒 ·
    // 生效中's 新建 form open (the fake account starts with no alerts), then 提醒记录 and its 未读. The phone's 设提醒 keeps
    // disclosure price-alerts. 继续加载当天提醒 (#932 R5, reminderCenter: {more: true}) shows only past 500 reminders a day;
    // the audit's Runtime holds none, so its rule in support/uiCoverage.ts and the command's receipt are checked by unit tests.
    ["reminders-mine", [{ reminders: { tab: "mine" } }, { disclosure: { name: "reminder-alert-new", open: true } }], true],
    ["reminder-center", [{ disclosure: { name: "reminder-alert-new", open: false } }, { reminderCenter: { open: true } }], true],
    ["reminder-center-unread", [{ reminderCenter: { filter: "unread" } }], true],
    ["", [{ reminderCenter: { filter: "all" } }, { reminderCenter: { open: false } }], false],
    // 下单顺带保护 (2026-10-02): a position row's inline 保护 form, opened by its own disclosure
    // (the fake account holds a SHFE.au2612 long so a row renders; the ticket's stop/take inputs
    // ride the order body and are audited with the order panel).
    ["position-protection", [{ panel: "positions" }, { positionsTab: "positions" }, { disclosure: { name: "position-protection", id: "position:SHFE.au2612:long", open: true } }], true],
    ["", [{ disclosure: { name: "position-protection", id: "position:SHFE.au2612:long", open: false } }], false],
    // 合约信息卡 (2026-10-02): the ⓘ by the contract button opens it by its own disclosure. The
    // fake account's first position is a delivery-month contract, so the badge rides along too.
    ["contract-info", [{ disclosure: { name: "contract-info", open: true } }], true],
    ["", [{ disclosure: { name: "contract-info", open: false } }], false],
    // 画线第二版 (2026-10-09): the toolbar is hidden on the trade surface above (it shows only while drawing, owner
    // 2026-10-14); here it with its group lists, a tool armed (the hint), the line's style bar, its 精确设置, 全部删除's
    // confirmation, the three right-click menus and their submenus — each opened by its own command. The first
    // version's chart-drawing-tools still opens the toolbar.
    ["", [{ disclosure: { name: "chart-drawing-tools", open: false } }, { disclosure: { name: "chart-drawing-tools", open: true } }], false],
    ["chart-drawing-group-lines", [{ panel: null }, { disclosure: { name: "chart-drawing-group", id: "lines", open: true } }], true],
    ["chart-drawing-group-fib", [{ disclosure: { name: "chart-drawing-group", id: "fib", open: true } }], true],
    ["", [{ disclosure: { name: "chart-drawing-group", id: "fib", open: false } }], false],
    ["chart-drawing-tool", [{ disclosure: { name: "chart-drawing-tool", id: "channel", open: true } }], true],
    ["", [{ disclosure: { name: "chart-drawing-tool", id: "channel", open: false } }], false],
    ["chart-drawing-style", [{ disclosure: { name: "chart-drawing-style", id: lineId, open: true } }], true],
    ["chart-drawing-settings", [{ disclosure: { name: "chart-drawing-settings", id: lineId, open: true } }], true],
    ["", [{ disclosure: { name: "chart-drawing-settings", id: lineId, open: false } }], false],
    ["chart-menu-main-draw", [{ disclosure: { name: "chart-context-menu", id: "main/draw", open: true } }], true],
    ["chart-menu-main-alert", [{ disclosure: { name: "chart-context-menu", id: "main/alert", open: true } }], true],
    ["chart-menu-main-period", [{ disclosure: { name: "chart-context-menu", id: "main/period", open: true } }], true],
    ["chart-menu-main-indicator", [{ disclosure: { name: "chart-context-menu", id: "main/indicator", open: true } }], true],
    ["chart-menu-main-add-pane", [{ disclosure: { name: "chart-context-menu", id: "main/add-pane", open: true } }], true],
    ["chart-menu-drawing", [{ disclosure: { name: "chart-context-menu", id: `drawing:${lineId}/style`, open: true } }], true],
    ["chart-menu-pane", [{ disclosure: { name: "chart-context-menu", id: "pane:kd/swap", open: true } }], true],
    ["chart-menu-pane-add", [{ disclosure: { name: "chart-context-menu", id: "pane:kd/add-pane", open: true } }], true],
    ["", [{ disclosure: { name: "chart-context-menu", id: "main", open: false } }, { disclosure: { name: "chart-drawing-style", id: lineId, open: false } }], false],
    ["chart-drawing-clear", [{ disclosure: { name: "chart-drawing-clear", open: true } }], true],
    ["", [{ disclosure: { name: "chart-drawing-clear", open: false } }, { disclosure: { name: "chart-drawing-magnet", open: true } },
      { disclosure: { name: "chart-drawing-lock-all", open: true } }, { disclosure: { name: "chart-drawings-hidden", open: true } }], false],
    ["chart-drawing-switches", [{ disclosure: { name: "chart-drawing-toolbar", open: true } }], true],
    ["", [{ disclosure: { name: "chart-drawing-toolbar", open: false } }, { disclosure: { name: "chart-drawings-hidden", open: false } },
      { disclosure: { name: "chart-drawing-lock-all", open: false } }, { disclosure: { name: "chart-drawing-magnet", open: false } }], false],
    // 合约抽屉两级 (2026-10-01): the drawer's product fold, audited with one product's months expanded.
    ["dialog-contract-product", [{ dialog: { name: "contract-drawer", open: true } }, { contractProduct: "SHFE.rb" }], true],
    ["", [{ contractProduct: null }, { dialog: { name: "contract-drawer", open: false } }], false],
    // The retired 策略设计窗's name opens the 策略编辑卡 on the selected strategy (audited above as ai-strategy-editor).
    ["", [{ panel: "strategy" }, { dialog: { name: "strategy-designer", open: true } }, { strategyEditor: { close: true } }], false],
    // 1.6.6 alias: the 持仓抽屉 name opens the 持仓面板 (already audited as ai-positions).
    ["", [{ dialog: { name: "positions-drawer", open: true } }, { dialog: { name: "positions-drawer", open: false } }], false],
    ["tour", [{ tour: { op: "start", steps: [{ target: "rail:order", label: "下单区", focus: { panel: "order" } }, { target: "positions-bar", label: "底栏" }] } }], true],
    ["", [{ tour: { op: "stop" } }, { panel: null }], false],
    ["voice-menu", [{ disclosure: { name: "voice-menu", open: true } }], true], ["", [{ disclosure: { name: "voice-menu", open: false } }], false],
    ["activity-card", [{ aiActivityCard: "open" }], true], ["", [{ aiActivityCard: "closed" }], false],
    // 快讯页签 (2026-10-03): the execution Runtime serves recorded 快讯/日历 samples (AI_TRADER_FAKE_RESEARCH_LIVE),
    // audited with every filter's rows (全部) and with the week's calendar expanded.
    ["news-flash", [{ news: { tab: "flash", flashFilter: "all" } }], true],
    ["news-flash-calendar", [{ disclosure: { name: "flash-calendar", open: true } }], true],
    ["", [{ disclosure: { name: "flash-calendar", open: false } }], false],
    // 新闻 AI 筛选 (2026-10-07): 沪金's screened view (pinned: the chart is on 股指 here) sorted by impact, then
    // its dropped fold opened — the fake samples carry a 黄金周 story the 沪金 排除词 drop and the recorded fake
    // model drops the unrelated ones. 第六批: the 要点 on top, the merged 现货黄金 event's reports and the 次要 fold.
    ["news-flash-screened", [{ news: { flashFilter: "product", flashSort: "impact", pinnedSymbol: "SHFE.au2612" } }], true],
    ["news-flash-dropped", [{ disclosure: { name: "flash-dropped", open: true } }], true],
    ["news-flash-minor", [{ disclosure: { name: "flash-dropped", open: false } }, { disclosure: { name: "flash-minor", open: true } }], true],
    ["news-flash-event", [{ disclosure: { name: "flash-minor", open: false } }, { disclosure: { name: "flash-event", open: true } }], true],
    ["", [{ disclosure: { name: "flash-event", open: false } }, { news: { flashSort: "time", flashFilter: "all", pinnedSymbol: null } }], false],
    // 今日作战表 (2026-10-25 第 5 批): the 调研 panel's first page (the audit account's 自选 ranked, or its empty prompt).
    ["news-sheet", [{ news: { tab: "sheet" } }], true],
    // 调研页改版 (2026-10-07 第七批): 沪金's 调研 (pinned), its folds opened, the 资料来源 jump and 按最新消息重新判断 (the
    // judgment was just written, so the Runtime answers with the hourly limit and its time).
    ["news-desk", [{ news: { tab: "desk", pinnedSymbol: "SHFE.au2612" } }], true],
    ["news-desk-folds", [{ disclosure: { name: "desk-fundamental", open: true } }, { disclosure: { name: "desk-macro", open: true } }], true],
    ["news-desk-sources", [{ disclosure: { name: "desk-fundamental", open: false } }, { disclosure: { name: "desk-macro", open: false } },
      { news: { deskSource: "M.price" } }], true],
    ["", [{ news: { deskRejudge: true } }, { disclosure: { name: "desk-sources", open: false } }, { news: { tab: "flash", pinnedSymbol: null } }], false],
    // 多 AI 辩论 (2026-10-24): the right rail's 辩论 page (half the window) — empty first (辩今日议题 and 以前的辩论), then
    // 沪金's finished debate with its five folds, then closed.
    ["debate-empty", [{ panel: "debate" }], true],
    ["debate-page", [{ researchDebate: { open: deskDebate.id } }, { disclosure: { name: "debate-judge", open: true } }, { disclosure: { name: "debate-sheets", open: true } },
      { disclosure: { name: "debate-sources", open: true } }, { disclosure: { name: "debate-stats", open: true } }, { disclosure: { name: "debate-log", open: true } },
      // 分析师 (2026-10-24): the four written reports, each opened.
      ...(["technical", "supply", "flow", "news"] as const).map(key => ({ disclosure: { name: `debate-analyst-${key}`, open: true } }))], true],
    ["", [{ researchDebate: { close: true } }, { panel: "news" }], false],
    // 周期收起 (2026-10-03): with the news panel open the bar is too narrow for the full period row,
    // so the uncommon periods fold into 更多; its popover opens by its own disclosure.
    ["chart-periods-more", [{ disclosure: { name: "chart-periods-more", open: true } }], true],
    ["", [{ disclosure: { name: "chart-periods-more", open: false } }, { panel: null }], false],
  ];
  const controls: Control[] = [];
  const rejected: string[] = [];
  const capReached: string[] = [];
  const auditSurfaces = async (target: Page, list: typeof surfaces, device = deviceId) => { for (const [surface, commands, audit] of list) {
    for (const changes of commands) {
      const status = await command(changes, device);
      if (status !== "applied") rejected.push(`${surface || "reset"}: ${JSON.stringify(changes)} → ${status}`);
    }
    if (!audit) continue;
    await target.waitForTimeout(600);
    const raw = await collectControls(target);
    // The observation rides on the heartbeat: content that does not change UiState (a new card) reaches
    // the Runtime with the next idle pulse, up to 10 s later. Wait for a snapshot that has caught up.
    // Inputs and plain anchors never appear in observation.controls (its selector lists buttons, selects,
    // summary, tabs and separators only) — waiting for them cannot succeed; the gap assertion below still
    // flags a non-exempt anchor the snapshot cannot see.
    let snapshot = await detail(device);
    for (let waited = 0; waited < 11_000; waited += 1000) {
      const labels = new Set<string>((snapshot.observation.controls as { label: string }[]).map(item => item.label));
      if (raw.every(item => item.role.startsWith("input:") || item.role === "a" || labels.has(item.label))) break;
      await target.waitForTimeout(1000);
      snapshot = await detail(device);
    }
    const seen = snapshot.observation.controls as { label: string }[];
    const observed = new Set<string>(seen.map(item => item.label));
    // The snapshot lists at most 150 controls (uiObservation.ts); past that cap absence proves nothing.
    const capped = seen.length >= 150;
    if (capped) capReached.push(surface);
    // With a dialog open the snapshot is rooted at the dialog (by design): audit the dialog's controls only.
    // The settings drawer and the chart labels' popovers (disclosures, 2026-10-02) both root the observation.
    const dialogOpen = Boolean(snapshot.state.dialog) || surface.startsWith("settings-") || /^(chart-(ma-editor|pane-editor|pane-add|drawing-tools|drawing-settings|drawing-clear|periods-more)|board-columns|price-alerts|reminder-center|reminder-center-unread|contract-info|mobile-order|mobile-install|mobile-strategy-editor-sheet|mobile-strategy-editor-picker|mobile-strategy-editor-backtest|mobile-quote-columns|mobile-chart-cell|mobile-subpanes|mobile-drawing-toolbar)$/.test(surface);
    for (const { inDialog, ...item } of raw) if (!dialogOpen || inDialog) controls.push({ ...item, surface, observed: capped || observed.has(item.label) });
  } };
  // The first surface opens right after the page loads, and the observation rides on the heartbeat (an
  // idle pulse up to 10 s apart): once, wait for the snapshot to list the page's own controls before
  // opening anything, so the first surface (chart-ma-editor) is not judged against a pre-load snapshot.
  // Never fails here — a control the snapshot truly lacks is still caught by the gap assertion below.
  {
    const base = await collectControls(page);
    for (let waited = 0; waited < 12_000; waited += 1000) {
      const seen = (await detail()).observation.controls as { label: string }[];
      const labels = new Set<string>(seen.map(item => item.label));
      if (seen.length >= 150 || base.every(item => item.role.startsWith("input:") || item.role === "a" || labels.has(item.label))) break;
      await page.waitForTimeout(1000);
    }
  }
  await auditSurfaces(page, surfaces);
  // AI 审计 2026-10-13: 设置 → AI 与记忆 reports 我的流程 and 记忆 in the structured state, not only as visible text.
  expect(await command({ settingsPage: "memory" })).toBe("applied");
  await expect.poll(async () => (await detail()).state.routines?.count ?? -1, { timeout: 12_000 }).toBeGreaterThanOrEqual(1);
  expect(typeof (await detail()).state.memory?.enabled).toBe("boolean");
  expect(await command({ settingsOpen: false })).toBe("applied");
  // A real (fake-agent) turn: a conversation card and its folded 详情 (the proposal card and its
  // confirmation dialog need the plain fake Runtime — the execution Runtime's gateway refuses fixture
  // quotes for order preparation — and stay a known gap here).
  {
    const audit = async (surface: string, filter: (item: Raw) => boolean) => {
      const raw = (await collectControls(page)).filter(filter);
      let snapshot = await detail();
      for (let waited = 0; waited < 11_000; waited += 1000) {
        const labels = new Set<string>((snapshot.observation.controls as { label: string }[]).map(item => item.label));
        if (raw.every(item => labels.has(item.label))) break;
        await page.waitForTimeout(1000);
        snapshot = await detail();
      }
      const observed = new Set<string>((snapshot.observation.controls as { label: string }[]).map(item => item.label));
      for (const { inDialog: _ignored, ...item } of raw) controls.push({ ...item, surface, observed: observed.has(item.label) });
    };
    expect(await command({ panel: "ai" })).toBe("applied");
    const composer = page.getByLabel("向 AI 决策流提问");
    await composer.fill("看看账户");
    await composer.press("Enter");
    await expect(page.getByTestId("conversation").locator("summary", { hasText: "详情" }).last()).toBeVisible({ timeout: 20_000 });
    await audit("ai-chat-card", item => item.role === "summary");
    // 回答卡片化 (2026-10-14): "沪金咋样" answers with a 回答卡 (看图表 / 问 AI 怎么办); the turn's 走势数据卡 is
    // folded into its 依据 (去重跟进). Open 依据 and audit both cards' own controls — their "?", the 依据 folds and
    // the buttons.
    await composer.fill("沪金咋样");
    await composer.press("Enter");
    const answerCard = page.getByTestId("answer-card").last();
    await expect(answerCard).toBeVisible({ timeout: 20_000 });
    await expect(answerCard.getByTestId("data-card")).toBeHidden();
    await answerCard.getByTestId("answer-card-details").locator("summary").first().click();
    await expect(answerCard.getByTestId("answer-folded").getByTestId("data-card")).toBeVisible({ timeout: 20_000 });
    // A card's "?" is named after its title (`<标题>的说明`).
    const cardHelps = new Set((await page.locator('[data-testid="answer-card"], [data-testid="data-card"]')
      .evaluateAll(cards => cards.map(card => card.getAttribute("aria-label") ?? ""))).map(title => `${title}的说明`));
    await audit("ai-chat-answer-card", item => item.label === "依据" || cardHelps.has(item.label) || ["看图表", "问 AI 怎么办"].includes(item.label));
    // 我的流程 (2026-10-06): the fake agent drafts a real 流程卡 from a definition sentence; its 保存 writes it,
    // and the routine's name then shows the run's 进度卡 (one step waiting for 确认).
    await composer.fill("以后我说「老规矩」，就帮我看判断、出开仓卡、设到价提醒");
    await composer.press("Enter");
    const routineCard = page.getByTestId("routine-card").last();
    await expect(routineCard).toBeVisible({ timeout: 20_000 });
    await audit("ai-chat-routine-card", item => item.label === "保存" || item.label === "改一下");
    await routineCard.getByRole("button", { name: "保存" }).click();
    await expect(routineCard).toContainText("已存进我的流程", { timeout: 10_000 });
    await composer.fill("老规矩");
    await composer.press("Enter");
    await expect(page.getByTestId("routine-run-card").last()).toContainText("需你确认", { timeout: 20_000 });
    await audit("ai-chat-routine-run", item => item.role === "summary");
    await command({ panel: null });  // a reset, not an audited surface
  }
  // The chart's right-click menu (画线第二版: also a command, audited above): a real right-click on the price pane
  // opens the same main menu — chart items only, no order items.
  await page.getByLabel("K 线图").click({ button: "right", position: { x: 300, y: 150 } });
  await page.waitForTimeout(500);
  {
    const raw = (await collectControls(page)).filter(item => item.role.startsWith("menuitem"));
    expect(raw.some(item => /开仓|智能平|仅填入草稿价|图表与下单说明/.test(item.label))).toBe(false);
    const observed = new Set<string>(((await detail()).observation.controls as { label: string }[]).map(item => item.label));
    for (const { inDialog: _ignored, ...item } of raw) controls.push({ ...item, surface: "chart-context-menu", observed: observed.has(item.label) });
    await page.keyboard.press("Escape");
  }
  // 手机版 (2026-10-04): a narrow touch screen gets the phone layout — its tabs, the AI 首页 page,
  // the chat page and the four tab pages, each opened by `mobileTab` in a window of its own device.
  {
    const phoneDevice = crypto.randomUUID();
    const phoneContext = await browser.newContext({ baseURL: testInfo.project.use.baseURL, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    await phoneContext.addInitScript(id => localStorage.setItem("ai-trader:web-device", id), phoneDevice);
    await briefRoutes(phoneContext);
    await reminderRoutes(phoneContext);
    await strategyRoutes(phoneContext);
    await routineRoutes(phoneContext);
    const phone = await phoneContext.newPage();
    await phone.goto("/");
    await expect(phone.getByTestId("mobile-workspace")).toHaveAttribute("data-connection", "connected", { timeout: 30_000 });
    await expect.poll(() => currentWindow(phoneDevice)).not.toBe("");
    await settled(phoneDevice);
    expect((await detail(phoneDevice)).state).toMatchObject({ mobileLayout: true, mobileTab: "ai" });
    const phoneSymbol = (await detail(phoneDevice)).state.symbol as string;
    // 手机画线: one line on the phone's own contract for its bar and style rows.
    const phoneBars = (await (await phone.request.get(`${runtime}/api/markets/${encodeURIComponent(phoneSymbol)}/workbench?duration=300`, { headers })).json()).bars as { datetime: number; close: number }[];
    const phoneLineId = (await (await phone.request.post(`${runtime}/api/market/drawings`, { headers, data: { symbol: phoneSymbol, kind: "trendline",
      points: { anchors: [-30, -5].map(at => ({ t: Math.floor(phoneBars.at(at)!.datetime / 1e9), p: phoneBars.at(at)!.close })) } } })).json()).drawing.id as string;
    await auditSurfaces(phone, [
      ["mobile-ai", [{ mobileTab: "ai" }], true], ["mobile-chat", [{ mobileTab: "chat" }], true],
      ["mobile-research", [{ mobileTab: "research" }], true],
      // 第六批: 沪金's compact 要点 and 重要 events, then the one fold for 其他 / 次要 / 无关 opened.
      ["mobile-research-news", [{ symbol: "SHFE.au2612" }, { disclosure: { name: "flash-minor", open: true } }], true],
      // 第七批: the phone's 调研 is the desk in its phone form; its 资料来源 fold opened.
      ["mobile-research-desk", [{ disclosure: { name: "flash-minor", open: false } }, { disclosure: { name: "desk-sources", open: true } }], true],
      // 多 AI 研讨 (2026-10-11): the phone's full-screen debate page (bubbles), its 裁判怎么取舍 / 底稿与来源 / 战绩 folds, then back.
      ["mobile-research-debate", [{ disclosure: { name: "desk-sources", open: false } }, { researchDebate: { open: deskDebate.id } },
        { disclosure: { name: "debate-judge", open: true } }, { disclosure: { name: "debate-sheets", open: true } }, { disclosure: { name: "debate-stats", open: true } }], true],
      ["", [{ researchDebate: { close: true } }, { symbol: phoneSymbol }], false],
      ["mobile-market", [{ mobileTab: "market" }], true],
      // 手机看盘第二批 (2026-10-09): 报价 (main contracts, its 列 sheet), 多图 (6 图, one cell's sheet), the 副图设置 sheet
      // with its overlay parameters, then back to 单图 for the pages below.
      ["mobile-market-quotes", [{ mobileMarket: { view: "quotes" } }, { mobileMarket: { board: "main" } }], true],
      ["mobile-market-foreign", [{ mobileMarket: { board: "FOREIGN" } }], true],
      ["mobile-quote-columns", [{ disclosure: { name: "mobile-quote-columns", open: true } }], true],
      ["mobile-market-multi", [{ disclosure: { name: "mobile-quote-columns", open: false } }, { mobileMarket: { layout: "six" } }], true],
      ["mobile-chart-cell", [{ disclosure: { name: "mobile-chart-cell", id: "1", open: true } }], true],
      ["mobile-subpanes", [{ disclosure: { name: "mobile-chart-cell", id: "1", open: false } }, { mobileMarket: { subPanes: true } },
        { disclosure: { name: "mobile-subpanes-overlays", open: true } }], true],
      ["", [{ mobileMarket: { subPanes: false } }, { mobileMarket: { layout: "four" } }, { mobileMarket: { board: "watchlist" } }, { mobileMarket: { view: "single" } }], false],
      // 手机画线 (2026-10-09): ✎'s tool sheet, a tool armed (the bar's hint), then a selected line's bar and its three style rows.
      ["mobile-drawing-toolbar", [{ disclosure: { name: "chart-drawing-toolbar", open: true } }], true],
      ["mobile-drawing-hint", [{ disclosure: { name: "chart-drawing-toolbar", open: false } }, { disclosure: { name: "chart-drawing-tool", id: "channel", open: true } }], true],
      ["mobile-drawing-style", [{ disclosure: { name: "chart-drawing-tool", id: "channel", open: false } }, { disclosure: { name: "chart-drawing-style", id: phoneLineId, open: true } }], true],
      ["mobile-drawing-style-row-color", [{ disclosure: { name: "mobile-drawing-style-row", id: "color", open: true } }], true],
      ["mobile-drawing-style-row-width", [{ disclosure: { name: "mobile-drawing-style-row", id: "width", open: true } }], true],
      ["mobile-drawing-style-row-dash", [{ disclosure: { name: "mobile-drawing-style-row", id: "dash", open: true } }], true],
      ["", [{ disclosure: { name: "mobile-drawing-style-row", id: "dash", open: false } }, { disclosure: { name: "chart-drawing-style", id: phoneLineId, open: false } }], false],
      // 新结构 (2026-10-06): the timeline (原通知) is AI 首页's; the old notify tab command still lands there and the retired
      // 提醒 filter opens the 提醒 page (2026-10-15).
      ["mobile-timeline", [{ mobileTab: "notify" }, { mobileNotifyFilter: "reminder" }, { mobileNotifyFilter: "all" }], true], ["mobile-me", [{ mobileTab: "me" }], true],
      // 提醒 (#932, 2026-10-15; three tabs since 2026-10-25): the phone's 提醒 page on 我的提醒 with the 新建 form open,
      // then 提醒记录's 未读, then 通用提醒.
      ["mobile-reminders", [{ mobileTab: "me" }, { reminders: { tab: "mine" } }, { disclosure: { name: "reminder-alert-new", open: true } }], true],
      ["mobile-reminders-unread", [{ disclosure: { name: "reminder-alert-new", open: false } }, { reminderCenter: { open: true } }, { reminderCenter: { filter: "unread" } }], true],
      ["mobile-reminders-general", [{ reminderCenter: { filter: "all" } }, { reminders: { tab: "general" } }], true],
      ["", [{ mobilePage: null }], false],
      ["mobile-strategy", [{ mobileTab: "strategy" }], true],
      ["mobile-strategy-mine", [{ mobileStrategyTab: "mine" }], true],
      // 手机编辑卡 (2026-10-08): the full-screen page (blank: the fake account has too few trades), its ③ 平仓 with 更多,
      // then a template's page with ② 开仓's rows and one condition's bottom sheet.
      ["mobile-strategy-editor", [{ strategyEditor: { new: true } }], true],
      ["mobile-strategy-editor-exit", [{ disclosure: { name: "strategy-editor-section", id: "exit", open: true } }, { disclosure: { name: "strategy-editor-more", open: true } }], true],
      ["mobile-strategy-editor-entry", [{ strategyEditor: { start: "template:donchian_turtle" } }, { disclosure: { name: "strategy-editor-section", id: "entry", open: true } }], true],
      ["mobile-strategy-editor-sheet", [{ disclosure: { name: "strategy-editor-condition", id: "entryLong:1", open: true } }], true],
      // 第二版: the same sheet on its right side's 指标选择器, then the page's 策略流程 block.
      ["mobile-strategy-editor-picker", [{ disclosure: { name: "strategy-editor-picker", id: "entryLong:1:right", open: true } }], true],
      ["mobile-strategy-editor-flow", [{ disclosure: { name: "strategy-editor-condition", id: "entryLong:1", open: false } },
        { disclosure: { name: "strategy-editor-section", id: "flow", open: true } }], true],
      // 回测明细: the four figures' sheet (the desktop's block).
      ["mobile-strategy-editor-backtest", [{ disclosure: { name: "strategy-editor-condition", id: "entryLong:1", open: false } }, { disclosure: { name: "strategy-editor-backtest", open: true } }], true],
      ["", [{ disclosure: { name: "strategy-editor-backtest", open: false } }], false],
      ["", [{ disclosure: { name: "strategy-editor-condition", id: "entryLong:1", open: false } }, { strategyEditor: { close: true } }, { mobileStrategyTab: "running" }], false],
      ["mobile-lab", [{ mobileTab: "strategy" }, { mobileLab: { labId: "audit-lab" } }], true],
      ["", [{ mobilePage: null }, { mobilePage: "lab" }, { mobilePage: null }, { mobileTab: "ai" }], false],
      // 第二期: the full-screen pages over the tabs (the 持仓与委托 page is the 持仓面板 itself).
      ["mobile-order", [{ mobileTab: "market" }, { mobilePage: "order" }, { disclosure: { name: "ticket-protection", open: true } }], true],
      ["ai-positions-phone", [{ mobilePage: "positions" }, { positionsTab: "fills" }], true],
      ["mobile-review", [{ mobilePage: "review" }], true],
      // AI 简报订阅 (2026-10-05): 我的 → AI 简报推送 with its picker, and 通知 · AI 简报's product chips
      // (a subscribed product is a chip whether or not 通知 has its rows yet).
      ["mobile-briefs", [{ mobileTab: "me" }, { mobilePage: "briefs" }, { disclosure: { name: "brief-add", open: true } }], true],
      // 安装到桌面 (2026-10-06): the install guide (this browser's environment decides its form).
      ["mobile-install", [{ mobileTab: "me" }, { mobilePage: "install" }], true],
      // 锁屏提醒 (2026-10-06): this browser's form of the page (the test browser is desktop Chrome → 收不到).
      ["mobile-push", [{ mobileTab: "me" }, { mobilePage: "push" }], true],
      ["mobile-notify-products", [{ mobileTab: "ai" }, { mobileNotifyFilter: "brief" }, { mobileNotifyProduct: "SHFE.rb" }], true],
      ["", [{ mobileNotifyProduct: null }, { mobileNotifyFilter: "all" }], false],
      // 我的 → AI 与记忆 (2026-10-06): the same page as 设置 → 记忆, with 我的流程 on it.
      ["settings-mobile-memory", [{ mobileTab: "me" }, { mobilePage: "memory" }], true],
      ["", [{ mobilePage: "accounts" }, { mobilePage: "appearance" }, { mobilePage: "memory" }, { mobilePage: null }, { mobileTab: "ai" }], false],
    ], phoneDevice);
    await phoneContext.close();
  }
  // Every surface the table lists must open by command.
  expect(rejected, "surfaces that did not open by command").toEqual([]);

  const unique = new Map<string, Control>();
  for (const control of controls) unique.set(`${control.surface}|${control.role}|${control.label}`, control);
  const verdicts = [...unique.values()].map(control => ({ control, verdict: classify(control) }));
  const gaps = verdicts.filter(({ verdict }) => verdict.kind === "gap" || verdict.kind === "unobserved");
  const known = verdicts.filter(({ verdict }) => verdict.kind === "known-gap");
  const stale = staleKnownGaps([...unique.values()]);
  const line = ({ control, verdict }: (typeof verdicts)[number]) => `[${control.surface}] ${control.role} “${control.label}”${control.disabled ? "（禁用）" : ""}${control.observed ? "" : "（快照不可见）"}${verdict.via ? ` — ${verdict.via}` : ""}`;
  const report = [
    `controls: ${unique.size}; command ${verdicts.filter(v => v.verdict.kind === "command").length}, capability ${verdicts.filter(v => v.verdict.kind === "capability").length}, exempt ${verdicts.filter(v => v.verdict.kind === "exempt").length}, known gaps ${known.length}, new gaps ${gaps.length}`,
    "", "## Known gaps (burn-down)", ...known.map(line),
    "", "## New gaps", ...gaps.map(line),
    "", "## Stale known-gap entries", ...stale.map(gap => gap.gap),
    "", `## Surfaces at the 150-control snapshot cap (observation not checked): ${capReached.join(", ") || "none"}`,
  ].join("\n");
  await testInfo.attach("ui-coverage-report.md", { body: report, contentType: "text/markdown" });
  console.log(report);
  expect(gaps.map(line), "controls no rule accounts for, or reachable controls the snapshot cannot see").toEqual([]);
  expect(stale.map(gap => gap.gap), "known-gap entries that no longer match a control — remove them").toEqual([]);
});
