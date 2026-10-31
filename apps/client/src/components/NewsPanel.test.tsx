import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { formatPublishDate, NewsPanel } from "./NewsPanel";
import { applyUiSurface, resetUiSurfaces, uiSurface } from "../uiSurfaces";
const payload = (symbol: string) => ({symbol, status: "ok", results: [{ title: symbol + "新闻", summary: "来源摘要", media: "交易所", publishDate: "2026-09-26", link: "https://example.com/news" }]});
afterEach(() => {vi.unstubAllGlobals(); resetUiSurfaces();});
test("引用编号不冒充媒体，首页不冒充原文，非法链接不可点击", async () => {
  const news = payload("SHFE.au2612");
  news.results = [
    {...news.results[0], title: "首页摘要", media: "ref_1", publishDate: "", link: "https://example.com"},
    {...news.results[0], title: "无链接摘要", media: "ref-2", link: "javascript:alert(1)"},
    {...news.results[0], title: "原文摘要"},
  ];
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(news))));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", {name: "相关报道 ›"}));
  expect(await screen.findByText("发布时间未提供 · example.com")).toBeVisible();
  expect(screen.queryByText(/ref[_-]\d/)).toBeNull();
  expect(screen.getByRole("link", {name: "查看来源网站"})).toHaveAttribute("href", "https://example.com/");
  expect(screen.getByRole("link", {name: "阅读原文"})).toHaveAttribute("href", "https://example.com/news");
  expect(screen.getAllByRole("link")).toHaveLength(2);
});
test("资讯：相关报道与快讯同样分层合并——重要带原因与几家报道、可展开看各家原文，次要收起；筛掉的折叠在下面可展开", async () => {
  const at = (minutes: number) => new Date(Date.UTC(2026, 9, 7, 0, minutes)).toISOString();
  const reports = [{ id: "n1", source: "期货日报", sources: ["期货日报"], publishedAt: at(10), title: "栖霞冷库苹果出库偏慢", text: "", link: "https://example.com/1", tags: [] },
    { id: "n2", source: "东方财富", sources: ["东方财富"], publishedAt: at(5), title: "冷库苹果走货慢", text: "", link: "https://example.com/3", tags: [] }];
  const ai = { event: "库存", direction: "偏空", strength: "中", reason: "去化慢于去年", transmission: "冷库出库慢，现货压力传到期货" };
  const news = { ...payload("CZCE.AP701"), results: [{ title: "栖霞冷库苹果出库偏慢", summary: "", media: "期货日报", publishDate: at(10),
    link: "https://example.com/1", source: "eastmoney", ai }],
    events: [{ id: "e1", eventLabel: "栖霞冷库出库偏慢", researchDay: "2026-10-07", publishedAt: at(10), firstAt: at(5), title: "栖霞冷库苹果出库偏慢", text: "摘要不显示",
      ai, tier: "major", reports: 2, sources: ["期货日报", "东方财富"], direct: true, tags: [], items: reports },
      { id: "e2", eventLabel: "苹果消费平淡", researchDay: "2026-10-07", publishedAt: at(3), firstAt: at(3), title: "节后苹果消费平淡", text: "",
        ai: { ...ai, direction: "中性", strength: "弱" }, tier: "minor", reports: 1, sources: ["新浪"], direct: true, tags: [],
        items: [{ id: "n3", source: "新浪", sources: ["新浪"], publishedAt: at(3), title: "节后苹果消费平淡", text: "", link: null, tags: [] }] }],
    layers: { major: 1, other: 0, minor: 1, unscreened: 0, minorReports: 1, minorGroups: [{ label: "苹果消费平淡", count: 1 }] },
    screen: { mode: "ai", pending: 0, dropped: { count: 1, groups: [{ reason: "苹果公司新品与财报", count: 1 }], items: [
      { title: "苹果公司三季度财报超预期", summary: "", media: "东方财富", publishDate: "2026-10-07T09:00:00+08:00", link: "https://example.com/2", source: "eastmoney",
        dropReason: "苹果公司新品与财报", droppedBy: "keyword" }] } } };
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(news))));
  render(<NewsPanel baseUrl="https://test" symbol="CZCE.AP701" onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", {name: "相关报道 ›"}));
  const major = await screen.findByRole("region", { name: "重要" });
  const row = within(major).getByRole("listitem");
  expect(row).toHaveTextContent("栖霞冷库苹果出库偏慢");
  expect(row).toHaveTextContent("冷库出库慢，现货压力传到期货");
  expect(row).toHaveTextContent("2 家报道：期货日报、东方财富");
  expect(row).not.toHaveTextContent("摘要不显示");
  fireEvent.click(within(row).getByRole("button", { name: "展开 2 家报道" }));
  expect(within(row).getAllByRole("link", { name: "原文" })).toHaveLength(2);
  expect(uiSurface("disclosure:news-event:e1")?.state).toBe(true);
  expect(screen.getByText(/收起了/)).toHaveTextContent("收起了 1 条次要消息：苹果消费平淡 1 条");
  act(() => applyUiSurface("disclosure:news-minor", true));
  expect(screen.getByText("节后苹果消费平淡")).toBeVisible();
  expect(screen.getByText(/AI 筛掉了/)).toHaveTextContent("AI 筛掉了 1 条无关新闻：苹果公司新品与财报 1 条");
  expect(screen.queryByText("苹果公司三季度财报超预期")).toBeNull();
  act(() => applyUiSurface("disclosure:news-dropped", true));
  expect(screen.getByText("苹果公司三季度财报超预期")).toBeVisible();
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({ tab: "news", news: { count: 1, screen: "ai", dropped: 1, events: 2, major: 1, minor: 1 } }));
});

test("切品种不接收迟到的旧结果，固定品种和命令切页共用状态", async () => {  let finishOld!: (value: Response) => void;
  const fetcher = vi.fn((url: string) => url.includes("rb2701") ? new Promise<Response>(resolve => {finishOld = resolve;}) : Promise.resolve(new Response(JSON.stringify(payload("DCE.i2701")))));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", {name: "相关报道 ›"}));
  view.rerender(<NewsPanel baseUrl="https://test" symbol="DCE.i2701" onClose={() => {}} />);
  expect(await screen.findByText("DCE.i2701新闻")).toBeVisible();
  await act(async () => finishOld(new Response(JSON.stringify(payload("SHFE.rb2701")))));
  expect(screen.queryByText("SHFE.rb2701新闻")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "固定阅读品种" }));
  view.rerender(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2705" onClose={() => {}} />);
  expect(screen.getByText("DCE.i2701新闻")).toBeVisible();
  act(() => applyUiSurface("news", {tab: "data"}));
  // 研究数据 is a page under 品种 (改版第一步): the 品种 tab stays selected and the page shows its back link.
  expect(screen.getByRole("tab", {name: "品种"})).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("button", {name: "‹ 品种 · 研究数据"})).toBeVisible();
  expect(uiSurface("news")?.state).toMatchObject({tab:"data", pinnedSymbol:"DCE.i2701"});
});
test("游客新闻与研究数据只走游客cookie接口，失败显示真实错误", async () => {
  const fetcher = vi.fn(async (url: string) => new Response(JSON.stringify(url.includes("/summary") ? {symbol:"SHFE.rb2701", product:"rb", datasets:{}} : {...payload("SHFE.rb2701"), status:"stale", error:"upstream_timeout", searchedAt:"2026-09-25"})));
  vi.stubGlobal("fetch", fetcher);
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" guest onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", {name: "相关报道 ›"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("新闻暂不可用 · 显示旧值 2026-09-25");
  expect(screen.queryByText("upstream_timeout")).toBeNull();
  expect(screen.getByRole("link", {name:"阅读原文"})).toHaveAttribute("rel", "noopener noreferrer");
  act(() => applyUiSurface("news", {tab: "data"}));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).includes("/summary"))).toBe(true));
  expect(fetcher.mock.calls.map(([url]) => String(url).split("?")[0].replace("https://test", ""))).toEqual(
    expect.arrayContaining(["/api/guest/research/desk", "/api/guest/research/news", "/api/guest/research/summary"]));
  for (const [url, init] of fetcher.mock.calls as unknown as [string, RequestInit][]) {
    expect(url).toContain("/api/guest/research/"); expect(init.credentials).toBe("include");
    expect(init.headers).not.toHaveProperty("Authorization");
  }
});
test("回放中不取也不显示今天的新闻和研究数据", async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify(payload("SHFE.rb2701"))));
  vi.stubGlobal("fetch", fetcher);
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" replay onClose={() => {}} />);
  expect(screen.getByRole("status")).toHaveTextContent("回放中不显示新闻和研究数据");
  act(() => applyUiSurface("news", {tab: "data"}));
  expect(screen.getByRole("status")).toHaveTextContent("回放中不显示新闻和研究数据");
  await act(async () => {});
  expect(fetcher).not.toHaveBeenCalled();
  expect(uiSurface("news")?.state).toMatchObject({replay: true});
});
test("游客会话过期时新闻与研究数据交给工作区显示重连提示，不写“暂不可用”", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
  const expired = vi.fn();
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" guest onGuestExpired={expired} onClose={() => {}} />);
  await waitFor(() => expect(expired).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("button", {name: "研究数据 ›"})).toBeNull();
  act(() => applyUiSurface("news", {tab: "data"}));
  await waitFor(() => expect(expired).toHaveBeenCalledTimes(2));
});

const fact = (id: string, section: string, label: string, value: string, extra: Record<string, unknown> = {}) =>
  ({id, section, kind: section === "report" ? "report" : "data", label, value, source: section === "report" ? "eastmoney" : "快期行情", asOf: "2026-09-28T10:00+08:00", ...extra});
const desk = (judgment: Record<string, unknown>) => ({symbol: "SHFE.rb2701", productName: "螺纹钢", mainSymbol: "SHFE.rb2610", generatedAt: "2026-09-28T10:00:00+00:00",
  notice: "数据类事实带来源与日期", sections: [
    {key: "market", title: "行情", kind: "data", status: "ok", count: 1, source: "快期行情"},
    {key: "fundamental", title: "基本面", kind: "data", status: "loading", count: 0},
    {key: "official", title: "产业数据", kind: "data", status: "unavailable", count: 0, error: "nbs_timeout"},
    {key: "research", title: "机构观点", kind: "report", status: "ok", count: 1},
    {key: "report", title: "相关报道", kind: "report", status: "ok", count: 1}],
  facts: [fact("M.price", "market", "主力 RB2610 最新价", "3106，日涨跌 -0.42%"),
    fact("V.x", "research", "南华期货", "螺纹：成材低估值有支撑。", {url: "https://futures.eastmoney.com/a/9.html", title: "黑色系 | 双焦偏弱",
      detail: "螺纹：成材低估值有支撑。反弹缺持续驱动，节前震荡为主。"}),
    fact("N.abc", "report", "新华财经", "螺纹钢社库连续第四周减少", {url: "https://finance.eastmoney.com/a/1.html", detail: "摘要不显示"})],
  judgment});
test("调研页：结论在上——一句话（第 5 批起不显示偏多偏空与信心）、几点生成；关键数据点了展开资料来源并定位；多空两栏每条一个小编号", async () => {
  const report = {...desk({status: "ready", stance: "震荡", confidence: "中", headline: "超卖有反弹动能，但美债收益率压着", headlineBasis: ["M.price"],
    generatedAt: "2026-09-28T07:01:00+00:00", today: true, conclusion: "两边接近，所以判震荡；若美债收益率继续上行，倾向转空。",
    change: {previousDay: "2026-09-27", previousStance: "偏多", same: false, text: "上次（09-27）偏多 → 今天转震荡"},
    points: [{text: "主力日跌 0.42%，超卖", basis: ["M.price"], kind: "推断", grounds: "data", quote: "日涨跌 -0.42%", direction: "偏多"},
      {text: "据报道社库下降", basis: ["N.abc", "N.gone"], kind: "推测", grounds: "report", direction: "偏多"}],
    risks: [{text: "外盘回落", basis: ["N.abc"], kind: "推断", grounds: "report", direction: "偏空"}],
    watch: [{text: "周四美联储会议纪要", basis: ["N.abc"], kind: "推断", grounds: "report"}], basis: {"N.gone": {label: "旧报道", value: "已经刷新掉的一条"}}}),
    tiles: [{label: "主力 RB2610", value: "3106", color: "down", note: "-0.42% · 09-28 10:00", ref: "M.price"},
      {label: "现货基差", value: "+59", color: "up", note: "期货比现货高 · 09-26", ref: "D.spot_basis"}]};
  report.facts.push(fact("D.spot_basis", "fundamental", "现货与基差", "现货 3165，主力基差 +59"), fact("E.pmi", "macro", "制造业PMI（2026年08月份）", "49.8，非制造业 49"));
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(report))));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
  const judgment = await screen.findByRole("region", {name: "今日判断"});
  expect(judgment).not.toHaveTextContent("不是交易建议");
  expect(judgment).not.toHaveTextContent("信心");
  expect(judgment).not.toHaveTextContent("震荡");
  expect(judgment).toHaveTextContent("15:01 自动更新因为：今天第一次判断");  // 07:01 UTC in Beijing time
  expect(judgment).toHaveTextContent("超卖有反弹动能，但美债收益率压着");
  // 关键数据: 涨跌 colours, the date in the small print; a figure opens 资料来源 at its fact.
  const tiles = screen.getByRole("region", {name: "关键数据"});
  expect(within(tiles).getByRole("button", {name: "主力 RB2610 3106，看来源"})).toHaveAttribute("data-color", "down");
  expect(tiles).toHaveTextContent("期货比现货高 · 09-26");
  expect(document.getElementById("desk-fact-D.spot_basis")).toBeNull();  // 资料来源 is folded
  fireEvent.click(within(tiles).getByRole("button", {name: "现货基差 +59，看来源"}));
  expect(document.getElementById("desk-fact-D.spot_basis")).toHaveAttribute("data-highlight", "true");
  // 多空对照: the reasons by direction, the citation numbers are 资料来源's; a vanished fact shows 旧.
  const versus = screen.getByRole("region", {name: "多空对照"});
  const [bull, bear] = [versus.querySelector<HTMLElement>('[data-side="bull"]')!, versus.querySelector<HTMLElement>('[data-side="bear"]')!];
  expect(bull).toHaveTextContent("偏多的理由主力日跌 0.42%，超卖1据报道社库下降3旧");
  expect(bear).toHaveTextContent("偏空的理由外盘回落3");
  expect(versus).toHaveTextContent("AI 的结论：两边接近，所以判震荡；若美债收益率继续上行，倾向转空。");
  expect(within(versus).getByRole("list", {name: "接下来关注"})).toHaveTextContent("周四美联储会议纪要");
  expect(screen.getByTitle("已更新：旧报道：已经刷新掉的一条")).toHaveTextContent("旧");
  // The long provenance string is gone from the reasons: no 推断/依据/原文 tags beside them.
  expect(versus).not.toHaveTextContent("原文：");
  expect(screen.queryByText("依据报道")).toBeNull();
  fireEvent.click(within(bull).getAllByRole("button", {name: "依据 1"})[0]);
  expect(document.getElementById("desk-fact-M.price")).toHaveAttribute("data-highlight", "true");
  // 资料来源 lists every fact with its source, date and the words the judgment quoted from it.
  const source = document.getElementById("desk-fact-M.price")!;
  expect(source).toHaveTextContent("主力 RB2610 最新价3106，日涨跌 -0.42%判断引用：「日涨跌 -0.42%」快期行情 · 09-28 10:00");
  expect(document.getElementById("desk-fact-V.x")).toHaveTextContent("摘自《黑色系 | 双焦偏弱》");
  expect(screen.queryByText("摘要不显示")).toBeNull();
  expect(screen.getByText(/暂未取到：产业数据/)).toBeInTheDocument();
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({tab: "desk", desk: {status: "ready", judgment: "ready", stance: "震荡",
    factCount: 5, tiles: 2, bull: 2, bear: 1, conclusion: true, change: "上次（09-27）偏多 → 今天转震荡", stale: null, highlight: "M.price"}}));
});
test("调研页：基本面、宏观、相关报道、资料来源四个折叠——数有几项，宏观只列这个品种有关的，相关报道近 7 天新的在前；折叠与资料来源定位都能用命令打开", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-10-07T13:00:00Z") });
  try {
    const report = {...desk({status: "disabled"}), symbol: "SHFE.au2612"};
    report.facts = [fact("M.price", "market", "主力 AU2612 最新价", "910.58，日涨跌 +1.31%"),
      fact("D.warehouse_receipt", "fundamental", "仓单", "116028，较上日 -3", {asOf: "20260930"}),
      fact("R.ag", "related", "沪银 AG2612（同类贵金属）", "日涨跌 +0.5%"),
      fact("O.fred_DGS10", "macro", "美国10年期国债收益率（FRED）", "5.35%，较前值 +0.04", {asOf: "2026-10-06"}),
      fact("N.old", "report", "旧媒体", "九月底的旧文", {asOf: "2026-09-20"}),
      fact("N.new", "report", "新华财经", "金价回落", {asOf: "2026-10-04T09:00+08:00", url: "https://finance.eastmoney.com/a/2.html"}),
      fact("V.y", "research", "南华期货", "黄金高位震荡。", {asOf: "2026-10-06T08:30+08:00", title: "贵金属早评"})];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(report))));
    render(<NewsPanel baseUrl="https://test" symbol="SHFE.au2612" onClose={() => {}} />);
    const fundamental = await screen.findByRole("button", {name: "展开基本面"});
    expect(fundamental).toHaveTextContent("基本面仓单 · 关联品种展开 2 项");
    expect(screen.getByRole("button", {name: "展开宏观"})).toHaveTextContent("只列和这个品种有关的：美国10年期国债收益率展开 1 项");
    const reports = screen.getByRole("button", {name: "展开相关报道"});
    expect(reports).toHaveTextContent("近 7 天 2 篇，最新 10-06");  // the 09-20 one is older than seven days
    fireEvent.click(reports);
    const rows = [...document.querySelectorAll('.desk-row[data-kind="report"]')].map(row => row.textContent);
    expect(rows).toEqual(["南华期货贵金属早评10-06 08:30", "新华财经金价回落10-04 09:00"]);
    expect(screen.getByRole("link", {name: "金价回落"})).toHaveAttribute("href", "https://finance.eastmoney.com/a/2.html");
    expect(screen.getByRole("button", {name: "展开资料来源"})).toHaveTextContent("7 项数据与报道的出处、原文摘录和数据日期");
    // The folds and the 资料来源 jump are commands; their state rides in the snapshot.
    act(() => { applyUiSurface("disclosure:desk-macro", true); });
    expect(screen.getByRole("button", {name: "收起宏观"})).toBeVisible();
    expect(screen.getByText("5.35%，较前值 +0.04")).toBeVisible();
    act(() => { applyUiSurface("news", {deskSource: "D.warehouse_receipt"}); });
    expect(document.getElementById("desk-fact-D.warehouse_receipt")).toHaveAttribute("data-highlight", "true");
    await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({tab: "desk", desk: {highlight: "D.warehouse_receipt"}}));
    expect(uiSurface("disclosure:desk-sources")?.state).toBe(true);
    act(() => { applyUiSurface("news", {deskSource: "X.unknown"}); });  // an id the report does not list highlights nothing
    expect((uiSurface("news")?.state as {desk: {highlight: string}}).desk.highlight).toBe("D.warehouse_receipt");
  } finally { vi.useRealTimers(); }
});
test("调研页：重新判断等完了不等于成功——没生成新判断、或资料到齐后没能跑（Runtime 回执）都如实说（#516）", async () => {
  const ready = {status: "ready", stance: "震荡", confidence: "中", headline: "宽幅震荡", headlineBasis: ["M.price"],
    generatedAt: "2026-09-28T07:01:00+00:00", today: true, points: [], risks: [], watch: []};
  for (const [after, said] of [
    [ready, "没有生成新的判断，仍显示之前的判断"],
    [{...ready, rejudgeSkipped: {status: "limited", message: "今天的判断次数已用完，明天 08:30 后再判"}}, "没有重新判断：今天的判断次数已用完，明天 08:30 后再判；仍显示之前的判断"],
    [{...ready, generatedAt: "2026-09-28T08:10:00+00:00"}, "已按最新消息重新判断"],
  ] as const) {
    const started = {...desk({...ready, refreshing: true, stage: "facts", startedAt: "2026-09-28T08:09:00+00:00", expectedSeconds: 30}),
      rejudge: {status: "started", message: "已按最新消息重新判断，完成后在这里更新"}};
    let posted = false;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") { posted = true; return new Response(JSON.stringify(started)); }
      return new Response(JSON.stringify(desk(posted ? after : ready)));
    }));
    const view = render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
    const judgment = await screen.findByRole("region", {name: "今日判断"});
    act(() => { applyUiSurface("news", {deskRejudge: true}); });
    expect(await within(judgment).findByText(said)).toBeVisible();
    view.unmount(); resetUiSurfaces();
  }
});
test("调研页：判断可能过时只提示——点“按最新消息重新判断”走手动刷新，限制原因就地显示；命令同样可按；新闻面一行链到快讯页", async () => {
  const report = {...desk({status: "ready", stance: "震荡", confidence: "中", headline: "宽幅震荡", headlineBasis: ["M.price"],
    generatedAt: "2026-09-28T07:01:00+00:00", today: true, points: [{text: "超卖", basis: ["M.price"], kind: "推断", grounds: "data", direction: "偏多"}],
    risks: [], watch: [], stale: {count: 6, newsNet: "偏空"}}), news: {net: "偏空", take: "美债收益率飙升压制金价", generatedAt: "2026-09-28T13:00:00+00:00"}};
  const refreshed = {...report, rejudge: {status: "limited", availableAt: "2026-09-28T08:01:00+00:00", message: "1 小时内已重判过，16:01 后可再判"}};
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => new Response(JSON.stringify(
    url.includes("/api/research/desk") ? (init?.method === "POST" ? refreshed : report) : {status: "ok", items: [], events: []})));
  vi.stubGlobal("fetch", fetcher);
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
  const judgment = await screen.findByRole("region", {name: "今日判断"});
  expect(judgment).toHaveTextContent("判断之后又来了 6 条重要消息，新闻面已转偏空，这份判断可能过时了");
  expect(fetcher).toHaveBeenCalledTimes(1);  // a hint only: nothing re-runs by itself
  fireEvent.click(within(judgment).getByRole("button", {name: "按最新消息重新判断"}));
  expect(await within(judgment).findByText("1 小时内已重判过，16:01 后可再判")).toBeVisible();
  expect(fetcher.mock.calls.filter(([url, init]) => String(url).endsWith("/api/research/desk/refresh?symbol=SHFE.rb2701") && init?.method === "POST")).toHaveLength(1);
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({desk: {stale: {count: 6, newsNet: "偏空"}, newsNet: "偏空",
    rejudge: {status: "limited", message: "1 小时内已重判过，16:01 后可再判"}}}));
  // The same through the UI command (the header button is the same request).
  act(() => { applyUiSurface("news", {deskRejudge: true}); });
  await waitFor(() => expect(fetcher.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(2));
  // 新闻面: the brief's direction and sentence; 去快讯看 opens the 快讯 tab on the current product.
  expect(screen.getByText("新闻面偏空")).toBeVisible();
  expect(screen.getByText("美债收益率飙升压制金价")).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "去快讯看 →"}));
  expect(uiSurface("news")?.state).toMatchObject({tab: "flash", flashFilter: "product"});
});
test("调研页：旧判断没有方向时按立场归栏——偏多的论点进偏多栏、风险进偏空栏；震荡的分成“理由 / 风险”两栏，不重算", async () => {
  const reason = (text: string) => ({text, basis: ["M.price"], kind: "推断", grounds: "data"});
  const old = (stance: string) => desk({status: "ready", stance, confidence: "中", headline: "旧判断", headlineBasis: ["M.price"],
    generatedAt: "2026-09-28T02:00:00+00:00", points: [reason("论点甲")], risks: [reason("风险乙")], watch: []});
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(old("偏多")))));
  const view = render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
  const versus = await screen.findByRole("region", {name: "多空对照"});
  expect(versus.querySelector('[data-side="bull"]')).toHaveTextContent("偏多的理由论点甲");
  expect(versus.querySelector('[data-side="bear"]')).toHaveTextContent("偏空的理由风险乙");
  expect(versus).not.toHaveTextContent("AI 的结论");  // an old judgment has no conclusion
  view.unmount(); resetUiSurfaces();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(old("震荡")))));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
  const range = await screen.findByRole("region", {name: "多空对照"});
  expect(range.querySelector('[data-side="points"]')).toHaveTextContent("理由论点甲");
  expect(range.querySelector('[data-side="risks"]')).toHaveTextContent("风险风险乙");
});
test("调研页：生成后已更新的依据编号标黄、资料来源写出生成时的值；旧值事实标出采集时间（#491 #492）", async () => {
  const report = desk({status: "ready", stance: "偏多", confidence: "中", confidenceBasis: "同向直接依据只有 1 条", headline: "基差走强", headlineBasis: ["M.price"], generatedAt: "2026-09-28T02:00:00+00:00",
    points: [{text: "主力基差 +74", basis: ["M.price"], kind: "推断", grounds: "data", quote: "主力基差 +74", direction: "偏多"}], risks: [], watch: [],
    changedBasis: ["M.price"], basis: {"M.price": {label: "主力 RB2610 最新价", value: "3074，日涨跌 +0.8%", source: "快期行情", asOf: "2026-09-28T09:30+08:00"}}});
  report.facts[0] = fact("M.price", "market", "主力 RB2610 最新价", "3106，日涨跌 -0.42%", {status: "stale", fetchedAt: "2026-09-27T10:00:00+00:00"});
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(report))));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
  const versus = await screen.findByRole("region", {name: "多空对照"});
  const cite = within(versus).getByRole("button", {name: "依据 1，生成后已更新"});
  expect(cite).toHaveAttribute("data-changed", "");
  expect(cite).toHaveAttribute("title", "生成时：主力 RB2610 最新价：3074，日涨跌 +0.8%");
  fireEvent.click(cite);
  const source = document.getElementById("desk-fact-M.price")!;
  expect(source).toHaveTextContent("生成后已更新，生成时：主力 RB2610 最新价 3074，日涨跌 +0.8%");
  expect(source).toHaveTextContent("快期行情 · 09-28 10:00 · 旧值（09-27 18:00 采集），来源未刷新");
  expect(screen.getByRole("button", {name: "收起资料来源"}).closest(".desk-fold")).toHaveTextContent("生成时 AI 只能引用当时编号的事实");
  // 第 5 批: no confidence level is shown, so neither is its reason.
  expect(screen.getByRole("button", {name: "收起资料来源"}).closest(".desk-fold")).not.toHaveTextContent("信心依据");
});
test("调研页：判断未就绪时说明原因，游客只看数据与资料来源、没有重新判断与新闻面", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({...desk({status: "signin_required"}), news: {net: "偏空", take: "不该给游客"}}))));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" guest onClose={() => {}} />);
  expect(await screen.findByText("登录后可看 AI 判断；下面是数据与资料来源。")).toBeVisible();
  expect(screen.queryByRole("button", {name: "重新判断"})).toBeNull();
  expect(screen.queryByText("新闻面偏空")).toBeNull();
  expect(screen.getByRole("button", {name: "展开资料来源"})).toHaveTextContent("3 项");
});

test("调研页：判断生成中按阶段写预计时间；隔日判断与生成后大幅波动都有提示", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-09-29T01:00:10Z") });
  try {
    const waiting = desk({status: "pending", stage: "facts", startedAt: "2026-09-29T01:00:00+00:00", expectedSeconds: 20});
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(waiting))));
    const view = render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
    expect(await screen.findByText(/正在补齐基本面数据，随后 AI 综合，预计约 20 秒（已等 1[0-9] 秒）。/)).toBeVisible();
    view.unmount(); resetUiSurfaces();
    const old = desk({status: "ready", stance: "震荡", confidence: "低", headline: "等待", headlineBasis: ["M.price"], points: [], risks: [], watch: [],
      generatedAt: "2026-09-28T01:00:00+00:00", researchDay: "2026-09-28", today: false, movedSincePercent: -1.84,
      manualAvailableAt: "2026-09-29T02:00:00+00:00", reason: "swan"});
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(old))));
    render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
    const judgment = await screen.findByRole("region", {name: "今日判断"});
    expect(judgment).toHaveTextContent("09-28 的判断，今天还没更新");
    expect(judgment).toHaveTextContent("生成后主力已跌 1.8%，判断可能滞后");
    expect(screen.getByText("基本面加载中…")).toBeVisible();
  } finally { vi.useRealTimers(); }
});
test("调研页：资料还在后台重读（factsRefreshing）时 4 秒后再读一次，读到新的就停（2026-10-22 调研台一秒内打开）", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-09-29T01:00:10Z") });
  try {
    const ready = desk({status: "ready", stance: "偏多", confidence: "中", headline: "库存回落", headlineBasis: ["M.price"], points: [], risks: [], watch: [],
      generatedAt: "2026-09-29T01:00:00+00:00", today: true});
    const replies = [{...ready, factsRefreshing: true}, ready];
    const fetcher = vi.fn(async (_url: string) => new Response(JSON.stringify(replies.length > 1 ? replies.shift() : replies[0])));
    vi.stubGlobal("fetch", fetcher);
    render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
    expect(await screen.findByText("库存回落")).toBeVisible();
    const deskReads = () => fetcher.mock.calls.filter(([url]) => String(url).includes("/api/research/desk?")).length;
    expect(deskReads()).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(deskReads()).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(deskReads()).toBe(2);
  } finally { vi.useRealTimers(); }
});
test("调研页：等待判断时一次读取失败按短间隔重试并提示，不推迟到十分钟后（#495）", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-09-29T01:00:10Z") });
  try {
    const waiting = desk({status: "pending", stage: "model", startedAt: "2026-09-29T01:00:00+00:00", expectedSeconds: 20});
    const ready = desk({status: "ready", stance: "偏多", confidence: "中", headline: "库存回落", headlineBasis: ["M.price"], points: [], risks: [], watch: [],
      generatedAt: "2026-09-29T01:00:30+00:00", today: true});
    const replies = [waiting, null, ready];
    const fetcher = vi.fn(async () => {
      const next = replies.length > 1 ? replies.shift() : replies[0];
      return next ? new Response(JSON.stringify(next)) : new Response("bad gateway", {status: 502});
    });
    vi.stubGlobal("fetch", fetcher);
    render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
    expect(await screen.findByText(/AI 正在综合/)).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    // The failed read is said, not shown as the model still working.
    expect(await screen.findByText(/调研读取失败（HTTP 502），约 4 秒后自动重试/)).toBeVisible();
    expect(screen.queryByText(/AI 正在综合/)).toBeNull();
    await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({desk: {status: "ready", judgment: "pending", readError: "HTTP 502"}}));
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(await screen.findByText("库存回落")).toBeVisible();
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(screen.queryByText(/调研读取失败/)).toBeNull();
    expect(uiSurface("news")?.state).toMatchObject({desk: {judgment: "ready"}});
    expect((uiSurface("news")?.state as {desk: Record<string, unknown>}).desk.readError).toBeUndefined();
  } finally { vi.useRealTimers(); }
});
test("调研页：首次读取失败短间隔自动重试；已有判断时刷新失败与重算失败都有提示（#494 #495）", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-09-29T02:01:00Z") });
  try {
    const old = desk({status: "ready", stance: "震荡", confidence: "低", headline: "等待", headlineBasis: ["M.price"], points: [], risks: [], watch: [],
      generatedAt: "2026-09-29T01:00:00+00:00", today: true, refreshError: "model_request_failed", retryAt: "2026-09-29T02:06:00+00:00"});
    let calls = 0;
    const fetcher = vi.fn(async (_url: string, init?: RequestInit) => {
      calls += 1;
      return calls === 1 || init?.method === "POST" ? new Response("unavailable", {status: 503}) : new Response(JSON.stringify(old));
    });
    vi.stubGlobal("fetch", fetcher);
    render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
    expect(await screen.findByText("调研暂不可用（HTTP 503），约 4 秒后自动重试。")).toBeVisible();
    await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({desk: {status: "error", readError: "HTTP 503"}}));
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    const judgment = await screen.findByRole("region", {name: "今日判断"});
    // #494: the earlier judgment stays with its time, the failed re-run and when to retry beside it.
    expect(judgment).toHaveTextContent("09:00 自动更新");
    expect(judgment).toHaveTextContent("上次重判失败（model_request_failed），仍显示之前的判断；10:06 后可再重判。");
    await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({desk: {status: "ready", judgment: "ready", refreshError: "model_request_failed", retryAt: "2026-09-29T02:06:00+00:00"}}));
    fireEvent.click(screen.getByRole("button", {name: "重新判断"}));
    expect(await screen.findByText("刷新失败（HTTP 503），下面是上次读到的内容，可再点重新判断。")).toBeVisible();
    expect(judgment).toHaveTextContent("等待09:00 自动更新");
    await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({desk: {status: "ready", readError: "HTTP 503",
      rejudge: {status: "error", message: "重新判断没能发出（HTTP 503），可再点一次"}}}));
  } finally { vi.useRealTimers(); }
});
test("调研与资讯来回切换不重新加载：切回资讯直接显示已取到的新闻", async () => {
  const fetcher = vi.fn(async (url: string) => new Response(JSON.stringify(url.includes("/news") ? payload("SHFE.rb2701") : {})));
  vi.stubGlobal("fetch", fetcher);
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", {name: "相关报道 ›"}));
  expect(await screen.findByText("SHFE.rb2701新闻")).toBeVisible();
  const newsCalls = () => fetcher.mock.calls.filter(([url]) => String(url).includes("/news")).length;
  const deskCalls = () => fetcher.mock.calls.filter(([url]) => String(url).includes("/desk")).length;
  const [news, desk] = [newsCalls(), deskCalls()];
  fireEvent.click(screen.getByRole("button", {name: "‹ 品种 · 相关报道"}));
  expect(screen.getByText("SHFE.rb2701新闻")).not.toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "相关报道 ›"}));
  expect(screen.getByText("SHFE.rb2701新闻")).toBeVisible();
  expect([newsCalls(), deskCalls()]).toEqual([news, desk]);
});

test("发布时间：ISO 串转北京时间，只有日期的只显日月，解析失败原样", () => {
  expect(formatPublishDate("2026-09-30T16:25:01+08:00")).toBe("09-30 16:25");
  // An instant in another zone still shows Beijing time.
  expect(formatPublishDate("2026-09-30T09:25:01Z")).toBe("09-30 17:25");
  expect(formatPublishDate("2026-09-26")).toBe("09-26");
  expect(formatPublishDate("20260926")).toBe("09-26");
  expect(formatPublishDate("不是日期")).toBe("不是日期");
  expect(formatPublishDate(undefined)).toBe("");
});

test("今日判断自动更新：写明为什么更新、下次什么时候自动更新，列出今天的更新记录；辩一轮在标题旁", async () => {
  const report = desk({status: "ready", stance: "偏空", confidence: "中", headline: "趋势与表需走弱压制", generatedAt: "2026-09-28T06:12:00+00:00",
    today: true, reason: "news", trigger: "新闻面转偏空：钢联周度表需环比下滑", nextAuto: {nightAt: "2026-09-28T20:45:00+08:00", news: true},
    updates: [{generatedAt: "2026-09-28T06:12:00+00:00", stance: "偏空", reason: "news", trigger: "新闻面转偏空：钢联周度表需环比下滑"},
      {generatedAt: "2026-09-28T00:45:00+00:00", stance: "震荡", reason: "daily"}]});
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(report))));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} />);
  const judgment = await screen.findByRole("region", {name: "今日判断"});
  expect(judgment).not.toHaveTextContent("由震荡转偏空");
  expect(judgment).toHaveTextContent("14:12 自动更新因为：新闻面转偏空：钢联周度表需环比下滑");
  expect(judgment).toHaveTextContent("下次自动更新夜盘开盘前 20:45，或再有重要新闻时");
  const log = within(judgment).getByLabelText("今天的更新记录");
  expect(log).toHaveTextContent("14:12新闻面转偏空：钢联周度表需环比下滑");
  expect(log).toHaveTextContent("08:45今天第一次判断");
  // 辩一轮 sits in the title row before the refresh icon.
  const buttons = within(judgment).getAllByRole("button").map(button => button.getAttribute("aria-label") ?? button.textContent);
  expect(buttons.indexOf("让 AI 们辩一轮")).toBeLessThan(buttons.indexOf("重新判断"));
});

test("今日作战表（第 5 批）：第一页按优先级一行一个自选，写关键价位、消息、可能的打法，不写多空；看调研切到品种调研", async () => {
  const sheet = {generatedAt: "2026-10-26T01:00:00+00:00", over: 0, foreign: ["COMEX.GC"], playbookCount: 2, rows: [
    {product: "SHFE.rb", name: "螺纹钢", symbol: "SHFE.rb2701", price: 3215, priority: "高", score: 4,
      levels: [{key: "prev_high", label: "昨高", price: 3230, distancePercent: 0.47}, {key: "opening_high", label: "开盘区间高", price: 3222, distancePercent: 0.22},
        {key: "opening_low", label: "开盘区间低", price: 3200, distancePercent: 0.47}, {key: "vwap", label: "均价", price: 3210.456, distancePercent: 0.14}],
      nearest: {label: "均价", distancePercent: 0.14},
      news: {strength: "强", items: [{id: "e1", title: "钢厂减产", at: "2026-10-26T01:12:00+00:00", reports: 3}]},
      playbooks: [{id: "p1", name: "开盘突破", why: "离开盘区间高 0.22%"}]},
    {product: "SHFE.au", name: "黄金", symbol: "SHFE.au2612", price: 960, priority: "低", score: 0, levels: [], nearest: null,
      news: {strength: "无", items: [], minor: 2}, playbooks: [], error: "行情暂时读不到，关键价位稍后再算"}]};
  const fetcher = vi.fn(async (url: string) => new Response(JSON.stringify(String(url).includes("/research/sheet") ? sheet : {symbol: "SHFE.rb2701", facts: [], sections: [], judgment: {status: "disabled"}})));
  vi.stubGlobal("fetch", fetcher);
  const onSelectSymbol = vi.fn();
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2701" onClose={() => {}} tab="sheet" onSelectSymbol={onSelectSymbol} />);
  expect(screen.getByRole("tab", {name: "今天"})).toHaveAttribute("aria-selected", "true");
  const card = await screen.findByTestId("battle-sheet");
  const rows = await within(card).findAllByRole("listitem");
  expect(rows[0]).toHaveTextContent("高螺纹钢rb27013215看调研");
  expect(rows[0]).toHaveTextContent("昨高 3230 0.47%开盘区间 3200–3222 0.22% / 0.47%均价 3210.46 0.14%");
  expect(rows[0]).toHaveTextContent("强钢厂减产09:12 · 3 家");
  expect(rows[0]).toHaveTextContent("开盘突破离开盘区间高 0.22%");
  expect(rows[1]).toHaveTextContent("行情暂时读不到，关键价位稍后再算");
  expect(rows[1]).toHaveTextContent("近 24 小时只有 2 条次要消息");
  expect(card).toHaveTextContent("外盘 1 个不排");
  expect(card).not.toHaveTextContent(/偏多|偏空|信心/);
  await waitFor(() => expect(uiSurface("news")?.state).toMatchObject({tab: "sheet", sheet: {status: "ready", rows: 2, high: 1, low: 1, top: "SHFE.rb"}}));
  fireEvent.click(within(rows[0]).getByRole("button", {name: "看调研"}));
  expect(onSelectSymbol).toHaveBeenCalledWith("SHFE.rb2701");
});
test("游客没有今日作战表页签", () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({symbol: "SHFE.rb2610", facts: [], sections: [], judgment: {status: "signin_required"}}))));
  render(<NewsPanel baseUrl="https://test" symbol="SHFE.rb2610" guest onClose={() => {}} />);
  expect(screen.queryByRole("tab", {name: "今天"})).toBeNull();
});
