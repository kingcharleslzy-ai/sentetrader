import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { InstrumentCatalogItem } from "../api";
import { applyUiSurface, resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { FLASH_REFRESH_MS, flashTime, type CalendarEvent, type FlashItem } from "./FlashFeed";
import { NewsPanel } from "./NewsPanel";

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); resetUiSurfaces(); });

const item = (id: string, products: string[], tags: string[] = [], extra: Partial<FlashItem> = {}): FlashItem => ({
  id, source: "财联社", sources: ["财联社"], publishedAt: new Date().toISOString(), title: `${id}…`, text: `${id} 正文`, link: null, products, tags, ...extra });
const ITEMS = [item("沪金走高", ["au"], [], { sources: ["东方财富", "新浪"], link: "https://example.com/au" }), item("美联储讲话", [], ["macro"]),
  item("铁矿发运", ["i", "rb"]), item("股指升水", ["if"])];
const catalog = [
  { symbol: "SHFE.au2612", exchangeId: "SHFE", productId: "au", instrumentName: "沪金2612", isMain: true, deliveryYear: 2026, deliveryMonth: 12 },
  { symbol: "SHFE.au2702", exchangeId: "SHFE", productId: "au", instrumentName: "沪金2702", isMain: false, deliveryYear: 2027, deliveryMonth: 2 },
] as InstrumentCatalogItem[];
const flashFetch = (respond: (url: string) => unknown, calendar: unknown = { status: "ok", items: [] }) => {
  const fetcher = vi.fn(async (url: string) => new Response(JSON.stringify(url.includes("/calendar") ? calendar : respond(url))));
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
};
const flashCalls = (fetcher: ReturnType<typeof flashFetch>) => fetcher.mock.calls.map(([url]) => String(url)).filter(url => url.includes("/research/flash"));
const openFlash = () => fireEvent.click(screen.getByRole("tab", { name: "快讯" }));

test("今天的快讯只显示时分，更早的带月日（北京时间）", () => {
  const now = new Date("2026-10-03T12:00:00+08:00");
  expect(flashTime("2026-10-03T09:04:00+08:00", now)).toBe("09:04");
  expect(flashTime("2026-10-02T23:10:00+08:00", now)).toBe("10-02 23:10");
  expect(flashTime("2026-10-02T16:10:00Z", now)).toBe("00:10");
});

test("四种筛选都走服务端：当前品种与持仓与自选是 AI 筛过的视图，宏观与全部原样列出", async () => {
  const fetcher = flashFetch(url => ({ status: "ok", items: url.includes("products=") ? [ITEMS[0], ITEMS[2]] : url.includes("product=") ? [ITEMS[3]] : url.includes("tag=macro") ? [ITEMS[1]] : ITEMS,
    ...(url.includes("product") ? { screen: { mode: "ai", pending: 0, dropped: { count: 0, groups: [], items: [] } } } : {}) }));
  render(<NewsPanel baseUrl="https://test" symbol="CFFEX.IF2609" watchProducts={["SHFE.au", "DCE.i"]} onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText(/股指升水 正文/)).toBeVisible();
  expect(flashCalls(fetcher).at(-1)).toContain("product=CFFEX.IF&");
  // 当前品种 reads only its own week of data; the other views read the whole calendar.
  expect(fetcher.mock.calls.map(([url]) => String(url)).filter(url => url.includes("/calendar")).at(-1)).toContain("product=CFFEX.IF");
  fireEvent.click(screen.getByRole("button", { name: "宏观" }));
  expect(await screen.findByText(/美联储讲话 正文/)).toBeVisible();
  expect(flashCalls(fetcher).at(-1)).toContain("tag=macro");
  expect(screen.getByText(/原样列出，未经 AI 筛选/)).toBeVisible();
  expect(screen.queryByRole("group", { name: "快讯排序" })).toBeNull();  // sorting by impact needs the readings
  fireEvent.click(screen.getByRole("button", { name: "持仓与自选" }));
  expect(await screen.findByText(/沪金走高 正文/)).toBeVisible();
  expect(screen.getByText(/铁矿发运 正文/)).toBeVisible();
  expect(flashCalls(fetcher).at(-1)).toContain(`products=${encodeURIComponent("au,i")}&`);
  fireEvent.click(screen.getByRole("button", { name: "全部" }));
  await waitFor(() => expect(screen.getAllByRole("listitem").filter(row => row.classList.contains("flash-row"))).toHaveLength(4));
  expect(screen.getByRole("button", { name: "全部" })).toHaveAttribute("aria-pressed", "true");
  // Several sources of one story are joined with "／"; a link shows 原文.
  expect(screen.getByText("东方财富／新浪")).toBeVisible();
  expect(screen.getByRole("link", { name: "原文" })).toHaveAttribute("href", "https://example.com/au");
});

const reading = (strength: string, direction = "偏空", transmission = "冷库出库慢，现货压力传到期货") =>
  ({ event: "库存", direction, strength, reason: "冷库出库慢于去年", transmission, product: "ap" });
const report = (id: string, sources: string[], minutesAgo: number, link: string | null = `https://example.com/${id}`) => ({
  id, source: sources[0], sources, publishedAt: new Date(Date.now() - minutesAgo * 60_000).toISOString(), title: `${id}…`, text: `${id} 正文`, link, tags: [] });
const ev = (id: string, tier: "major" | "other" | "minor" | "unscreened", ai: ReturnType<typeof reading> | null, reports: ReturnType<typeof report>[],
  eventLabel: string | null = id) => ({
  id, eventLabel, researchDay: "2026-10-07", publishedAt: reports[0].publishedAt, firstAt: reports.at(-1)!.publishedAt, title: reports[0].title,
  text: reports[0].text, ai, tier, reports: new Set(reports.flatMap(entry => entry.sources)).size, tags: [],
  sources: [...new Set(reports.flatMap(entry => entry.sources))], direct: true, items: reports });
const DROP = ev("金价下跌", "major", reading("强", "偏空", "外盘下跌直接带动沪金"),
  [report("新浪快讯", ["新浪", "东方财富"], 5), report("金十快讯", ["金十"], 80), report("见闻快讯", ["华尔街见闻"], 90, null)], "现货黄金日内下跌");
const GOLD_VIEW = {
  status: "ok", sources: ["财联社", "东方财富", "新浪", "金十"], items: [],
  events: [
    DROP,
    ev("美债新高", "major", reading("中", "偏空", "收益率越高，不付利息的黄金越吃亏"), [report("美债快讯", ["财联社"], 20)], "美债收益率创新高"),
    ev("加息降温", "other", reading("弱", "偏多"), [report("加息快讯", ["富途"], 30), report("加息二", ["新浪"], 31)], "美国加息概率下降"),
    ev("欧央行", "minor", reading("弱", "中性"), [report("欧央行一", ["新浪"], 10), report("欧央行二", ["新浪"], 12)], "欧央行官员表态"),
    ev("纪要前瞻", "minor", reading("弱", "中性"), [report("纪要一", ["金十"], 40)], "美联储纪要前瞻"),
  ],
  layers: { major: 2, other: 1, minor: 2, unscreened: 0, minorReports: 3, minorGroups: [{ label: "欧央行官员表态", count: 2 }, { label: "美联储纪要前瞻", count: 1 }] },
  brief: { status: "ready", net: "偏空", take: "美债收益率创新高、美元走强，现货黄金跌破 4110 美元。", basedOn: 31, generatedAt: "2026-10-07T10:35:00+00:00",
    points: [{ text: "现货黄金跌 1.3%：沪金夜盘大概率跟跌", direction: "偏空", eventLabel: "现货黄金日内下跌", eventId: "金价下跌", reports: 4 },
      { text: "多国央行增持黄金：长期支撑没变", direction: "偏多", eventLabel: "多国央行增持黄金", eventId: "央行", reports: 3 }],
    watch: ["周四 02:00 美联储会议纪要"] },
  tonight: [{ date: "2026-10-07", time: "20:30", datetime: "2026-10-07T20:30+08:00", region: "美国", event: "9月非农就业人数", importance: 3 }],
  screen: { mode: "ai", pending: 2, dropped: { count: 3, groups: [{ reason: "无关国际新闻", count: 2 }, { reason: "黄金周等同名说法", count: 1 }], items: [
    { ...item("胡塞武装", [], ["macro"]), dropReason: "无关国际新闻", droppedBy: "ai" },
    { ...item("加拿大经济", [], ["macro"]), dropReason: "无关国际新闻", droppedBy: "ai" },
    { ...item("黄金周出游", ["au"]), dropReason: "黄金周等同名说法", droppedBy: "keyword" },
  ] } },
};
const sectionRows = (name: string) => within(screen.getByRole("region", { name })).getAllByRole("listitem").map(row => row.textContent ?? "");

test("要点在最上面：新闻面方向、一句话、带方向点与报道家数的要点、接下来关注、更新时间与依据；今晚一行只列相关数据", async () => {
  flashFetch(() => GOLD_VIEW);
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  const brief = await screen.findByRole("region", { name: "今日新闻要点" });
  expect(within(brief).getByText("新闻面偏空")).toHaveAttribute("data-tone", "down");
  expect(within(brief).getByText(/美债收益率创新高、美元走强/)).toBeVisible();
  expect(within(brief).getByText(/AI 新闻分析师 · \d\d:\d\d 更新 · 依据 31 条/)).toBeVisible();
  const points = within(brief).getAllByRole("listitem");
  expect(points[0]).toHaveTextContent("现货黄金跌 1.3%：沪金夜盘大概率跟跌 · 4 家报道");
  expect(within(points[1]).getByLabelText("偏多")).toHaveAttribute("data-tone", "up");
  expect(within(brief).getByText("接下来关注")).toBeVisible();
  expect(screen.getByLabelText("今晚的相关数据")).toHaveTextContent("今晚20:30 美国9月非农就业人数");
  // Its place: above the filter row.
  expect(brief.compareDocumentPosition(screen.getByRole("group", { name: "快讯筛选" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ flash: { brief: { status: "ready", net: "偏空", basedOn: 31, points: 2 }, tonight: 1 } }));
});

test("同一件事合成一条：重要带原因与“N 家报道”，展开看各家原文；其他一行一条；次要默认收起、按原因计数；没判完的不显示", async () => {
  flashFetch(() => GOLD_VIEW);
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  const major = await screen.findByRole("region", { name: "重要" });
  expect(sectionRows("重要")).toHaveLength(2);
  const drop = within(major).getAllByRole("listitem")[0];
  expect(drop).toHaveTextContent("新浪快讯 正文");
  expect(drop).toHaveTextContent("外盘下跌直接带动沪金");
  expect(drop).toHaveTextContent(/起 4 家报道：新浪、东方财富、金十、华尔街见闻/);
  expect(within(drop).getByLabelText("偏空，强度强")).toHaveAttribute("data-tone", "down");
  expect(within(drop).queryByRole("link")).toBeNull();
  fireEvent.click(within(drop).getByRole("button", { name: "展开 4 家报道" }));
  expect(within(drop).getAllByRole("link", { name: "原文" }).map(link => link.getAttribute("href"))).toEqual(["新浪快讯", "金十快讯"].map(id => new URL(`https://example.com/${id}`).href));
  expect(within(drop).getByText(/华尔街见闻：见闻快讯 正文/)).toBeVisible();
  expect(uiSurface("disclosure:flash-event:金价下跌")?.state).toBe(true);
  act(() => applyUiSurface("disclosure:flash-event:金价下跌", false));
  expect(within(drop).queryByText(/华尔街见闻：/)).toBeNull();
  // A single report: its source and 原文 on the source line.
  expect(within(major).getAllByRole("listitem")[1]).toHaveTextContent("财联社 · 原文");

  expect(sectionRows("其他")).toHaveLength(1);
  expect(sectionRows("其他")[0]).toContain("加息快讯 正文");
  expect(screen.getByRole("button", { name: "展开 2 家报道" })).toHaveTextContent("2 家");

  // 次要: folded with counts by event name; open by click or command.
  expect(screen.getByText(/收起了/)).toHaveTextContent("收起了 3 条次要消息：欧央行官员表态 2 条、美联储纪要前瞻 1 条（影响弱且方向不明）");
  expect(screen.queryByText(/欧央行一 正文/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "展开次要消息" }));
  expect(screen.getByText(/欧央行一 正文/)).toBeVisible();
  expect(uiSurface("disclosure:flash-minor")?.state).toBe(true);
  act(() => applyUiSurface("disclosure:flash-minor", false));
  expect(screen.queryByText(/欧央行一 正文/)).toBeNull();

  // Not judged yet: not listed, one line says how many the AI is still reading.
  expect(screen.getByText("AI 正在看新到的 2 条，看完再列出")).toBeVisible();
  expect(screen.getByText(/AI 筛掉了/)).toHaveTextContent("AI 筛掉了 3 条无关新闻：无关国际新闻 2 条，黄金周等同名说法 1 条");
  fireEvent.click(screen.getByRole("button", { name: "看筛掉了什么" }));
  expect(within(screen.getByRole("region", { name: "无关国际新闻 2 条" })).getByText(/胡塞武装 正文/)).toBeVisible();
  expect(document.querySelector(".news-sources")).toHaveTextContent("来源 4 个：财联社 · 东方财富 · 新浪 · 金十");
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ flash: { screen: "ai", pending: 2, dropped: 3, events: 5, major: 2, other: 1, minor: 2, minorReports: 3 } }));

  // 按影响: within 重要 the stronger first (here already); the switch is still reported.
  fireEvent.click(screen.getByRole("button", { name: "按影响" }));
  expect(sectionRows("重要")[0]).toContain("新浪快讯 正文");
  expect(uiSurface("news")?.state).toMatchObject({ flashSort: "impact" });
});

test("要点上次没写成时保留上一份并写明上次更新时间；分析师在写时不占位成空块", async () => {
  flashFetch(() => ({ ...GOLD_VIEW, brief: { ...GOLD_VIEW.brief, status: "stale", failedAt: "2026-10-07T11:10:00+00:00" } }));
  const { unmount } = render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  const brief = await screen.findByRole("region", { name: "今日新闻要点" });
  expect(within(brief).getByText(/AI 新闻分析师 · 上次更新 \d\d:\d\d · 依据 31 条/)).toBeVisible();
  expect(within(brief).getByText(/美债收益率创新高、美元走强/)).toBeVisible();
  unmount();
  resetUiSurfaces();
  flashFetch(() => ({ ...GOLD_VIEW, brief: { status: "writing" } }));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText("AI 新闻分析师正在读今天筛过的新闻…")).toBeVisible();
  resetUiSurfaces();
});

test("未经 AI 筛选时有一句可见说明，事件照常按关键词列出、不分层", async () => {
  flashFetch(() => ({ status: "ok", items: [item("栖霞冷库", ["ap"], [], { ai: null, direct: true })],
    events: [ev("栖霞冷库", "unscreened", null, [report("栖霞冷库", ["财联社"], 3)], null)],
    layers: { major: 0, other: 0, minor: 0, unscreened: 1, minorReports: 0, minorGroups: [] },
    screen: { mode: "keyword", pending: 0, note: "今天的 AI 筛选额度已用完；新条目按关键词列出，未经 AI 筛选。",
      dropped: { count: 1, groups: [{ reason: "苹果公司新品与财报", count: 1 }], items: [{ ...item("苹果新品", ["ap"]), dropReason: "苹果公司新品与财报", droppedBy: "keyword" }] } } }));
  render(<NewsPanel baseUrl="https://test" symbol="CZCE.AP701" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText(/栖霞冷库 正文/)).toBeVisible();
  expect(screen.getByText("今天的 AI 筛选额度已用完；新条目按关键词列出，未经 AI 筛选。")).toBeVisible();
  expect(screen.getByRole("region", { name: "按关键词列出" })).toBeVisible();
  expect(screen.queryByRole("region", { name: "重要" })).toBeNull();
  expect(screen.getByText(/按关键词筛掉了/)).toHaveTextContent("按关键词筛掉了 1 条无关新闻：苹果公司新品与财报 1 条");
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ flash: { screen: "keyword", dropped: 1 } }));
});

test("AI 筛选中有条目两次判断失败：一句可见说明写明条数，那几条单列“未经 AI 筛选”，快照报条数", async () => {
  flashFetch(() => ({ status: "ok", items: [],
    events: [ev("栖霞冷库", "major", reading("中"), [report("栖霞冷库", ["财联社"], 3)]), ev("郑商所保证金", "unscreened", null, [report("郑商所保证金", ["金十"], 5)], null)],
    layers: { major: 1, other: 0, minor: 0, unscreened: 1, minorReports: 0, minorGroups: [] },
    screen: { mode: "ai", pending: 0, unscreened: 1, note: "1 条 AI 判断失败，按关键词显示，未经 AI 筛选。", dropped: { count: 0, groups: [], items: [] } } }));
  render(<NewsPanel baseUrl="https://test" symbol="CZCE.AP701" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText(/郑商所保证金 正文/)).toBeVisible();
  expect(screen.getByText("1 条 AI 判断失败，按关键词显示，未经 AI 筛选。")).toHaveAttribute("data-mode", "unscreened");
  expect(sectionRows("未经 AI 筛选")[0]).toContain("郑商所保证金");
  expect(within(screen.getByRole("region", { name: "未经 AI 筛选" })).queryByLabelText(/强度/)).toBeNull();
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ flash: { screen: "ai", unscreened: 1, major: 1 } }));
});

test("正文默认折叠两行，点开看全文；品种标签把图表切到该品种主力合约", async () => {
  flashFetch(() => ({ status: "ok", items: [ITEMS[0]] }));
  const select = vi.fn();
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2702" instruments={catalog} onSelectSymbol={select} onClose={() => {}} />);
  openFlash();
  const text = await screen.findByRole("button", { name: /沪金走高 正文/ });
  expect(text).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(text);
  expect(text).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(text);
  expect(text).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(screen.getByRole("button", { name: "切到 沪金 主力合约" }));
  expect(select).toHaveBeenCalledWith("SHFE.au2612");
});

test("翻译过的英文快讯：中文标题加原文折叠，正文（=原文）不重复显示；行按钮就是折叠（flash-orig 面）", async () => {
  flashFetch(() => ({ status: "ok", items: [item("en-copper", [], ["macro"], {
    title: "伦交所铜库存下降", text: "Copper inventories fall in LME warehouses", origTitle: "Copper inventories fall in LME warehouses" })] }));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  const row = await screen.findByRole("button", { name: /伦交所铜库存下降/ });
  expect(row).not.toHaveTextContent(/Copper inventories/);  // 正文（=原文）不重复显示，原文折叠着
  fireEvent.click(row);
  expect(within(row).getByText("原文：Copper inventories fall in LME warehouses")).toBeVisible();
  expect(uiSurface("disclosure:flash-orig:en-copper")?.state).toBe(true);
  act(() => applyUiSurface("disclosure:flash-orig:en-copper", false));
  expect(within(row).queryByText(/Copper inventories/)).toBeNull();
});

test("默认的筛选视图（事件行）也显示原始来源“据…”标签", async () => {
  const tagged = { ...ev("统计局数据", "major", reading("强", "偏多", "需求回暖"), [report("新浪快讯", ["新浪"], 5)], "国家统计局发布数据"),
    tags: ["据:国家统计局"] };
  flashFetch(() => ({ ...GOLD_VIEW, events: [tagged],
    layers: { ...GOLD_VIEW.layers, major: 1, other: 0, minor: 0, minorReports: 0, minorGroups: [] } }));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText("据国家统计局")).toBeVisible();
});

test("旧版 Runtime 的 events 没有 tags 字段：照常渲染、不亮交易所徽标（缺字段不崩）", async () => {
  // 灰度/回滚时新客户端连旧 Runtime：events 载荷没有 tags，读取按无标签处理。
  const { tags: _omit, ...legacy } = ev("金价下跌", "major", reading("强", "偏空", "外盘下跌直接带动沪金"),
    [report("新浪快讯", ["新浪"], 5)], "现货黄金日内下跌");
  flashFetch(() => ({ ...GOLD_VIEW, events: [legacy],
    layers: { ...GOLD_VIEW.layers, major: 1, other: 0, minor: 0, minorReports: 0, minorGroups: [] } }));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText(/新浪快讯 正文/)).toBeVisible();
  expect(screen.queryByText("交易所")).toBeNull();
});

test("页签显示时每 60 秒刷新、新到的条目短暂高亮，切走后不再请求", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  let round = 0;
  const fetcher = flashFetch(url => url.includes("/research/flash") ? { status: "ok", items: round++ === 0 ? [ITEMS[0]] : [item("新到一条", ["au"]), ITEMS[0]] } : {});
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  expect(flashCalls(fetcher)).toHaveLength(0);  // the tab has not been shown yet
  openFlash();
  expect(await screen.findByText(/沪金走高 正文/)).toBeVisible();
  expect(screen.getByText(/沪金走高 正文/).closest("li")).not.toHaveAttribute("data-fresh");
  await act(async () => { vi.advanceTimersByTime(FLASH_REFRESH_MS); });
  expect(await screen.findByText(/新到一条 正文/)).toBeVisible();
  expect(flashCalls(fetcher)).toHaveLength(2);
  expect(screen.getByText(/新到一条 正文/).closest("li")).toHaveAttribute("data-fresh");
  expect(screen.getByText(/沪金走高 正文/).closest("li")).not.toHaveAttribute("data-fresh");
  fireEvent.click(screen.getByRole("tab", { name: "资讯" }));
  const before = flashCalls(fetcher).length;
  await act(async () => { vi.advanceTimersByTime(FLASH_REFRESH_MS * 3); });
  expect(flashCalls(fetcher)).toHaveLength(before);
});

test("本机没有快讯源时一行说明；筛选后为空时说近 24 小时没有相关快讯", async () => {
  flashFetch(() => ({ status: "unavailable", error: "research_dependency_missing", items: [] }), { status: "unavailable", items: [] });
  const view = render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText("本机版不提供快讯，云端可用")).toBeVisible();
  expect(screen.queryByText("本周重要数据")).toBeNull();
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ flash: { status: "unavailable", count: 0 } }));
  view.unmount(); resetUiSurfaces();
  flashFetch(() => ({ status: "ok", items: [], lastPoll: { at: "2026-10-03T12:00:00+08:00", failedSources: [] } }), { status: "loading", items: [] });
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText("近 24 小时没有相关快讯")).toBeVisible();
  expect(within(screen.getByRole("region", { name: "本周重要数据" })).getByText("正在获取")).toBeVisible();
});

test("云端这一轮全部来源失败：说是抓取失败而不是本机版，本周重要数据照常显示；首轮没跑完不说“没有快讯”（#516）", async () => {
  const week = [{ date: "2026-10-05", time: "20:30", datetime: "2026-10-05T20:30:00+08:00", region: "美国", event: "非农", importance: 3,
    actual: null, forecast: 1.5, previous: 1.2 }];
  flashFetch(() => ({ status: "unavailable", error: "research_flash_unavailable", items: [],
    lastPoll: { at: "2026-10-03T12:00:00+08:00", failedSources: ["财联社"] } }), { status: "ok", items: week });
  const view = render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText(/快讯来源这一轮都没读到/)).toBeVisible();
  expect(screen.queryByText("本机版不提供快讯，云端可用")).toBeNull();
  expect(await screen.findByText("非农")).toBeVisible();
  view.unmount(); resetUiSurfaces();
  flashFetch(() => ({ status: "ok", items: [] }), { status: "ok", items: week });
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  expect(await screen.findByText("快讯还没完成首轮采集，稍后自动刷新")).toBeVisible();
  expect(screen.queryByText("近 24 小时没有相关快讯")).toBeNull();
});

test("本周重要数据按日分组，最高一档强调、已过去变淡；折叠时只列接下来的 5 条，展开看全部（命令同样可开）", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-03T12:00:00+08:00"));
  const event = (date: string, time: string, name: string, importance = 2, actual: number | null = null): CalendarEvent => ({
    date, time, datetime: `${date}T${time}+08:00`, region: "美国", event: name, importance, actual, forecast: 1.5, previous: 1.2 });
  const events = [event("2026-10-03", "09:30", "已公布", 2, 1.7), event("2026-10-03", "20:30", "非农", 3), event("2026-10-04", "17:00", "零售"),
    event("2026-10-05", "22:00", "PMI"), event("2026-10-06", "09:30", "CPI", 3), event("2026-10-07", "20:30", "PPI"), event("2026-10-08", "02:00", "纪要", 3)];
  flashFetch(() => ({ status: "ok", items: [] }), { status: "ok", items: events });
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  const block = await screen.findByRole("region", { name: "本周重要数据" });
  await within(block).findByText("非农");
  expect(within(block).getByRole("region", { name: "今天 10-03 周六" })).toBeVisible();
  expect(within(block).getByRole("region", { name: "明天 10-04 周日" })).toBeVisible();
  expect(within(block).getByRole("region", { name: "10-05 周一" })).toBeVisible();
  // Folded: only what is still to come — the morning's published figure waits behind 展开全部.
  expect(within(block).queryByText("已公布")).toBeNull();
  expect(within(block).getByText("非农").closest("li")).toHaveAttribute("data-high");
  expect(within(block).getByText("非农").closest("li")).not.toHaveAttribute("data-past");
  expect(within(block).getByText("PPI")).toBeVisible();
  expect(within(block).queryByText("纪要")).toBeNull();
  fireEvent.click(within(block).getByRole("button", { name: "展开全部 7 条" }));
  expect(within(block).getByText("纪要")).toBeVisible();
  const published = within(block).getByText("已公布").closest("li")!;
  expect(published).toHaveAttribute("data-past");
  expect(published).toHaveTextContent("公布 1.7预期 1.5 / 前值 1.2");
  expect(uiSurface("disclosure:flash-calendar")?.state).toBe(true);
  act(() => applyUiSurface("disclosure:flash-calendar", false));
  expect(within(block).queryByText("纪要")).toBeNull();
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ flash: { calendarStatus: "ok", calendarCount: 7 } }));
});

test("日历空成功显示“没有数据”；部分日期没刷新时照常列出旧内容并加一行说明，快照报 stale（#488）", async () => {
  flashFetch(() => ({ status: "ok", items: [] }), { status: "ok", items: [], missingDays: [], staleDays: [] });
  const view = render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  expect(await within(screen.getByRole("region", { name: "本周重要数据" })).findByText("未来 7 天没有中等重要以上的数据")).toBeVisible();
  view.unmount(); resetUiSurfaces();
  const kept: CalendarEvent = { date: "2026-10-03", time: "20:30", datetime: "2026-10-03T20:30+08:00", region: "美国", event: "非农", importance: 3, actual: null, forecast: 14.2, previous: 14.7 };
  flashFetch(() => ({ status: "ok", items: [] }), { status: "stale", items: [kept], missingDays: ["2026-10-09"], staleDays: ["2026-10-03"] });
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  const block = await screen.findByRole("region", { name: "本周重要数据" });
  expect(await within(block).findByText("非农")).toBeVisible();
  expect(within(block).getByRole("status")).toHaveTextContent("10-03 未刷新，显示上次取到的内容；10-09 未取到");
  expect(within(block).queryByText("未来 7 天没有中等重要以上的数据")).toBeNull();
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ flash: { calendarStatus: "stale", calendarCount: 1 } }));
});

test("界面命令切到快讯页并切筛选，快照带筛选与条数；游客不提供快讯页", async () => {
  flashFetch(url => ({ status: "ok", items: url.includes("tag=macro") ? [ITEMS[1]] : [ITEMS[0], ITEMS[2]] }));
  const view = render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  act(() => applyUiSurface("news", { tab: "flash" }));
  expect(screen.getByRole("tab", { name: "快讯" })).toHaveAttribute("aria-selected", "true");
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ tab: "flash", flashFilter: "product", flash: { status: "ok", count: 2 } }));
  act(() => applyUiSurface("news", { tab: "news" }));
  act(() => applyUiSurface("news", { flashFilter: "macro" }));
  expect(screen.getByRole("tab", { name: "快讯" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("button", { name: "宏观" })).toHaveAttribute("aria-pressed", "true");
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ flashFilter: "macro", flash: { count: 1 } }));
  view.unmount(); resetUiSurfaces();
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" guest onClose={() => {}} />);
  expect(screen.queryByRole("tab", { name: "快讯" })).toBeNull();
  act(() => applyUiSurface("news", { tab: "flash" }));
  expect(screen.getByRole("tab", { name: "品种调研" })).toHaveAttribute("aria-selected", "true");
});


test("同一筛选的两轮读取交叠：先发后到的旧结果不覆盖新的（#516）", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  let releaseFirst: (value: Response) => void = () => {};
  let round = 0;
  vi.stubGlobal("fetch", vi.fn((url: string) => {
    if (url.includes("/calendar")) return Promise.resolve(new Response(JSON.stringify({ status: "ok", items: [] })));
    if (!url.includes("/research/flash")) return Promise.resolve(new Response("{}"));
    round += 1;
    if (round === 1) return new Promise<Response>(resolve => { releaseFirst = resolve; });
    return Promise.resolve(new Response(JSON.stringify({ status: "ok", items: [item("新一轮", ["au"])], lastPoll: { at: "x", failedSources: [] } })));
  }));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  openFlash();
  await act(async () => { vi.advanceTimersByTime(FLASH_REFRESH_MS); });
  expect(await screen.findByText(/新一轮 正文/)).toBeVisible();
  await act(async () => {
    releaseFirst(new Response(JSON.stringify({ status: "ok", items: [item("旧一轮", ["au"])], lastPoll: { at: "x", failedSources: [] } })));
    await new Promise(resolve => setTimeout(resolve, 20));
  });
  expect(screen.getByText(/新一轮 正文/)).toBeVisible();
  expect(screen.queryByText(/旧一轮 正文/)).toBeNull();
});
