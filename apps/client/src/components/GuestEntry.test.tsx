import { act, fireEvent, render, screen, waitFor, cleanup, within } from "@testing-library/react";
import { vi } from "vitest";
import { GuestEntry } from "./GuestEntry";
import { setGuestAccessToken, guestAccessTokenFor, type GuestSession } from "../guestApi";
import { applyUiSurface, registerUiSurface, resetUiSurfaces } from "../uiSurfaces";
import { resetShortcutStore } from "../shortcuts";
import { setOwnerSessionToken } from "../api";
import { MOBILE_LAYOUT_QUERY } from "../hooks/useMobileLayout";
const mocks = vi.hoisted(() => ({ frames: undefined as any, owner: vi.fn(), press: vi.fn(), stream: vi.fn() }));
vi.mock("lightweight-charts", () => ({
  ColorType: { Solid: "solid" },
  CandlestickSeries: {},
  HistogramSeries: {},
  LineSeries: {},
  createSeriesMarkers: () => ({ detach: vi.fn() }),
  createChart: () => {
    const series = {
      setData: vi.fn(),
      applyOptions: vi.fn(),
      priceScale: () => ({ applyOptions: vi.fn() }),
      createPriceLine: vi.fn(() => ({})),
      removePriceLine: vi.fn(),
      attachPrimitive: vi.fn(),
      detachPrimitive: vi.fn(),
      coordinateToPrice: vi.fn(() => 821.5)
    };
    return {
      addSeries: () => series,
      paneSize: () => ({height:300,width:600}),
      applyOptions: vi.fn(),
      timeScale: () => ({
        fitContent: vi.fn(),
        getVisibleRange: vi.fn(() => null),
        getVisibleLogicalRange: vi.fn(() => ({ from: 0, to: 10 })),
        setVisibleLogicalRange: vi.fn(),
        subscribeVisibleLogicalRangeChange: vi.fn(),
        unsubscribeVisibleLogicalRangeChange: vi.fn()
      }),
      subscribeClick: vi.fn(),
      unsubscribeClick: vi.fn(),
      remove: vi.fn()
    };
  }
}));
vi.mock("./OwnerLogin", () => ({ OwnerLogin: ({ open, onClose, onAuthenticated }: any) => open ? <div role="dialog" aria-label="登录"><button onClick={onClose}>取消登录</button><button onClick={() => onAuthenticated("ok")}>登录成功</button></div> : null }));
vi.mock("../runtimeConnection", async importOriginal => ({ ...await importOriginal<any>(), resolveRuntimeConnection: async () => ({baseUrl:"https://guest.test",source:"web-env",authMode:"owner-password"}), cloudRuntimeAuthMode: () => "owner-password" }));
vi.mock("../api", async importOriginal => ({ ...await importOriginal<any>(), getOwnerAuthStatus: mocks.owner }));
vi.mock("../sse", () => ({ openSseStream: (options: any) => { mocks.frames = options; mocks.stream(options.url); return vi.fn(); } }));
vi.mock("../pushToTalk", async importOriginal => ({ ...await importOriginal<any>(), pushToTalkStore: { press: mocks.press, release: vi.fn(), cancel: vi.fn(), snapshot: () => ({state:"idle"}), showNotice: vi.fn() }, usePushToTalkStatus: () => ({state:"idle",partial:""}) }));
const base = "https://guest.test";
const session: GuestSession = { role:"guest", guestId:"g1", expiresAt:"2099-01-01", readOnly:true, voiceEnabled:true, windowId:"w1", accessToken:"guest.first", marketHistoryTradingDays:3 };
const item = (symbol:string, name:string) => ({symbol,instrumentId:symbol.split(".")[1],instrumentName:name,exchangeId:"SHFE",productId:symbol.split(".")[1].slice(0,2),priceTick:1,volumeMultiple:10,isMain:true});
const catalog = {schemaVersion:1,items:[item("SHFE.rb2610","螺纹2610"),item("SHFE.au2612","沪金2612")],count:2,dataStatus:"normal"};
let requests: string[];
let nextSession: GuestSession;
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); resetShortcutStore(); resetUiSurfaces(); requests=[]; mocks.frames=undefined; nextSession=session; setGuestAccessToken(base);
  vi.stubGlobal("matchMedia", vi.fn(() => ({matches:false,addEventListener:vi.fn(),removeEventListener:vi.fn()})));
  mocks.owner.mockResolvedValue({authenticated:false});
  vi.stubGlobal("fetch", vi.fn(async (input:any, init:any) => {
    const url=String(input); requests.push(url);
    if(url.endsWith("/api/auth/guest")) return Response.json(nextSession);
    if(url.endsWith("/api/guest/instruments")) return Response.json(catalog);
    if(url.endsWith("/api/guest/ui")) return Response.json({revision:0,changes:{}});
    if(url.endsWith("/api/guest/voice/intent")) return Response.json({accepted:false,say:"支持切换合约、周期、新闻和主题"});
    if(url.includes("/api/guest/research/") && url.includes("/summary")) return Response.json({symbol:"SHFE.rb2610",product:"rb",datasets:{}});
    if(url.includes("/api/guest/research/")) return Response.json({symbol:"SHFE.rb2610",status:"ok",results:[{title:"公共行情新闻",summary:"来源摘要",media:"交易所",publishDate:"2026-09-26",link:"https://example.com/news"}]});
    throw new Error(`unexpected private request ${url} ${init?.method}`);
  }));
});
afterEach(() => { cleanup(); setOwnerSessionToken(base, ""); resetUiSurfaces(); vi.unstubAllGlobals(); });
const posts = () => vi.mocked(fetch).mock.calls.filter(([url,init]) => String(url).endsWith("/api/guest/ui") && init?.method==="POST");
const latestSnapshot = () => JSON.parse(String(posts().at(-1)?.[1]?.body)).snapshot;
const frame = (event:string, data:unknown) => act(() => mocks.frames.onFrame({event,data:[JSON.stringify(data)]}));
const market = {
  schemaVersion: 1,
  durationSeconds: 300,
  capturedAt: "2026-09-01T06:59:52Z",
  ageSeconds: 0.3,
  dataStatus: "normal",
  marketSessionStatus: "trading",
  isCached: false,
  liveTrading: "disabled",
  quote: {
    symbol: "SHFE.rb2610",
    name: "螺纹2610",
    quoteDatetime: "2026-09-01T14:59:52+08:00",
    lastPrice: 824,
    changePercent: -0.68,
    bidPrice1: 823.98,
    bidVolume1: 42,
    askPrice1: 824.02,
    askVolume1: 36,
    open: 829.8,
    highest: 831.6,
    lowest: 822.4,
    volume: 128_564,
    openInterest: 182_746,
    priceTick: 0.02,
    volumeMultiple: 1000,
    upperLimit: 900,
    lowerLimit: 700
  },
  bars: [{
    datetime: 1_788_192_000_000_000_000,
    open: 820,
    high: 825,
    low: 818,
    close: 824,
    volume: 30,
    openInterest: 182_746
  }]
};
async function enter() {
  const view=render(<GuestEntry mode="cloud"><div>私人工作区</div></GuestEntry>);
  await waitFor(() => expect(mocks.frames).toBeDefined());
  frame("snapshot",{workbench:market});
  return view;
}
function assertPublicOnly() {
  expect(requests.every(url=>url.includes("/api/guest/") || url.endsWith("/api/auth/guest"))).toBe(true);
  for(const [,init] of vi.mocked(fetch).mock.calls) expect(new Headers(init?.headers).has("X-AI-Trader-Account")).toBe(false);
}
it("未登录预检创建游客身份并挂载真实工作区，不读取私人数据", async()=>{
  await enter();
  expect(screen.getByRole("navigation",{name:"主要工作区"})).toBeVisible();
  expect(screen.getByRole("navigation",{name:"辅助工具"})).toBeVisible();
  expect(screen.queryByText("私人工作区")).toBeNull();
  expect(vi.mocked(fetch).mock.calls.find(([url])=>String(url).endsWith("/api/auth/guest"))?.[1]?.method).toBe("POST");
  assertPublicOnly();
});
it("已有成员和旧服务端保留成员入口",async()=>{
  mocks.owner.mockResolvedValueOnce({authenticated:true});
  const view=render(<GuestEntry mode="cloud"><div>成员入口</div></GuestEntry>);
  await screen.findByText("成员入口"); expect(requests).toEqual([]); view.unmount();
  vi.mocked(fetch).mockResolvedValueOnce(new Response("",{status:404}));
  render(<GuestEntry mode="cloud"><div>旧版入口</div></GuestEntry>);
  await screen.findByText("旧版入口");
});
it("正在连接行情超过 10 秒提示仍在连接，“刷新重试”整页重新载入，不再并发第二次身份检查（#710 B）",async()=>{
  const reload=vi.fn();
  vi.stubGlobal("location",{...window.location,reload});
  vi.useFakeTimers();
  try {
    mocks.owner.mockReturnValueOnce(new Promise(()=>undefined));
    render(<GuestEntry mode="cloud"><div>成员入口</div></GuestEntry>);
    await act(async()=>{ await vi.advanceTimersByTimeAsync(9_999); });
    expect(screen.getByText("正在连接行情…")).toBeVisible();
    expect(screen.queryByRole("button",{name:"刷新重试"})).toBeNull();
    await act(async()=>{ await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText(/仍在连接，网络较慢或应用服务暂时连不上/)).toBeVisible();
    fireEvent.click(screen.getByRole("button",{name:"刷新重试"}));
    expect(reload).toHaveBeenCalledOnce();
    expect(mocks.owner).toHaveBeenCalledTimes(1);
  } finally { vi.useRealTimers(); }
});
it("身份读取超时留在启动页显示原因和刷新重试，不落到成员登录；其他失败照旧进入登录（#710 B）",async()=>{
  mocks.owner.mockRejectedValueOnce(Object.assign(new Error("应用服务 30 秒内没有响应，请稍后重试"),{status:0,code:"request_timeout"}));
  const view=render(<GuestEntry mode="cloud"><div>成员入口</div></GuestEntry>);
  expect(await screen.findByText("应用服务 30 秒内没有响应，请稍后重试")).toBeVisible();
  expect(screen.getByRole("button",{name:"刷新重试"})).toBeVisible();
  expect(screen.queryByText("成员入口")).toBeNull();
  expect(requests).toEqual([]);
  view.unmount();
  mocks.owner.mockRejectedValueOnce(Object.assign(new Error("503 unavailable"),{status:503}));
  render(<GuestEntry mode="cloud"><div>成员入口</div></GuestEntry>);
  expect(await screen.findByText("成员入口")).toBeVisible();
});
it("游客取消登录后，提示条不挡住右侧工具栏的点击（#710 A）",async()=>{
  // The app stylesheet itself (vitest blanks CSS imports), so the toast's real rule decides whether it swallows clicks.
  const { readFileSync } = await vi.importActual<{ readFileSync: (path: string, encoding: string) => string }>("node:fs");
  const cwd = (globalThis as unknown as { process: { cwd: () => string } }).process.cwd();
  const style=document.createElement("style"); style.textContent=readFileSync(`${cwd}/src/styles.css`,"utf8"); document.head.append(style);
  try {
    await enter();
    fireEvent.click(screen.getByRole("button",{name:"打开策略面板"}));
    fireEvent.click(screen.getByText("取消登录"));
    const toast=screen.getByText("登录后可使用此功能");
    expect(toast).toHaveClass("toast");
    expect(getComputedStyle(toast).pointerEvents).toBe("none");
    fireEvent.click(screen.getByRole("button",{name:"打开调研面板"}));
    expect(screen.getByRole("button",{name:"收起调研面板"})).toBeVisible();
  } finally { style.remove(); }
});
it("受限入口可点击并提示登录，不挂载私人能力或下单",async()=>{
  await enter();
  for(const name of ["打开对话","打开策略面板","打开模拟下单","打开持仓面板","复盘","回放"]) {
    const button=screen.getByRole("button",{name}); expect(button).toBeEnabled(); fireEvent.click(button);
    expect(screen.getByRole("dialog",{name:"登录"})).toBeVisible(); fireEvent.click(screen.getByText("取消登录"));
  }
  fireEvent.click(screen.getByRole("button",{name:"设置"}));
  fireEvent.click(screen.getByRole("button",{name:"账户"}));
  expect(screen.getByRole("dialog",{name:"登录"})).toBeVisible();
  expect(screen.queryByRole("dialog",{name:"设置"})).toBeNull();
  fireEvent.click(screen.getByText("取消登录"));
  expect(screen.getByRole("dialog",{name:"设置"})).toBeVisible();
  assertPublicOnly();
});
it("真实新闻和研究数据仅请求公共数据",async()=>{
  await enter(); fireEvent.click(screen.getByRole("button",{name:"打开调研面板"}));
  fireEvent.click(screen.getByRole("button",{name:"相关报道 ›"}));
  expect(await screen.findByText("公共行情新闻")).toBeVisible();
  act(()=>applyUiSurface("news",{tab:"data"}));
  await waitFor(()=>expect(requests.some(url=>url.includes("/summary"))).toBe(true));
  assertPublicOnly();
});
it("同窗口命令驱动共享控件并回执，其他窗口和私人面板不应用",async()=>{
  await enter();
  frame("ui",{windowId:"other",revision:1,changes:{panel:"news"}});
  expect(screen.queryByRole("tab",{name:"品种"})).toBeNull();
  frame("ui",{windowId:"w1",revision:1,changes:{theme:"light",fontScale:"large",dockSide:"left",dockWidth:420,chartOption:{name:"kd",visible:false},settingsPage:"ui",settingsOpen:true}});
  await waitFor(()=>expect(posts().length).toBeGreaterThan(0));
  await waitFor(()=>expect(latestSnapshot()).toMatchObject({appliedRevision:1,theme:"light",dockSide:"left",dockWidth:420,settingsPage:"ui",settingsOpen:true,chart:{kd:false}}));
  // AI 审计 2026-10-13: 图表高级设置 is a visitor's page on the Runtime and in the drawer, so a command opens it too.
  frame("ui",{windowId:"w1",revision:2,changes:{settingsPage:"chart-advanced"}});
  await waitFor(()=>expect(latestSnapshot()).toMatchObject({appliedRevision:2,settingsPage:"chart-advanced"}));
  frame("ui",{windowId:"w1",revision:3,changes:{panel:"order"}});
  await waitFor(()=>expect(latestSnapshot().notice).toBe("界面操作未完成，请重试"));
  expect(latestSnapshot().appliedRevision).toBe(2); assertPublicOnly();
});
it("游客只控制和回读诊断预览折叠，不应用或上报私人折叠", async()=>{
  await enter();
  act(() => { registerUiSurface("disclosure:tool-card:private-card", { state: true }); });
  frame("ui", {windowId:"w1", revision:1, changes:{settingsPage:"feedback"}});
  await screen.findByText("预览诊断内容");
  await waitFor(()=>expect(latestSnapshot()).toMatchObject({appliedRevision:1, settingsPage:"feedback", disclosures:{"feedback-diagnostics":false}}));
  for (const [index, opened] of [true, false].entries()) {
    frame("ui", {windowId:"w1", revision:index+2, changes:{disclosure:{name:"feedback-diagnostics",open:opened}}});
    await waitFor(()=>expect(latestSnapshot()).toMatchObject({appliedRevision:index+2, disclosures:{"feedback-diagnostics":opened}}));
    expect(screen.getByText("预览诊断内容").closest("details")?.open).toBe(opened);
    expect(Object.keys(latestSnapshot().disclosures)).toEqual(["feedback-diagnostics"]);
  }
  for (const disclosure of [null, {}, [], {name:"voice-call",open:true}, {name:"feedback-diagnostics",open:1},
    {name:"feedback-diagnostics",open:true,id:null}, {name:"feedback-diagnostics",open:true,id:"private"},
    {name:"feedback-diagnostics",open:true,extra:true}]) {
    frame("ui", {windowId:"w1", revision:4, changes:{disclosure}});
    await waitFor(()=>expect(latestSnapshot().notice).toBe("界面操作未完成，请重试"));
    expect(latestSnapshot().appliedRevision).toBe(3);
    expect(screen.getByText("预览诊断内容").closest("details")?.open).toBe(false);
  }
  assertPublicOnly();
});
it("游客语音只走游客意图接口，不继承已存成员令牌",async()=>{
  setOwnerSessionToken(base,"owner.secret"); await enter();
  fireEvent.keyDown(window,{key:"Alt",code:"AltRight"});
  expect(mocks.press).toHaveBeenCalledWith(base,expect.any(Function),true,undefined,undefined);
  await act(async()=>mocks.press.mock.calls.at(-1)![1]("买入十手黄金"));
  expect(requests.some(url=>url.endsWith("/api/guest/voice/intent"))).toBe(true);
  for(const [,init] of vi.mocked(fetch).mock.calls) expect((new Headers(init?.headers).get("Authorization") ?? "")).not.toContain("owner.secret");
  assertPublicOnly();
});

it("游客合约搜索按查询过滤：不匹配显示空态并说明游客搜索范围（#476）",async()=>{
  await enter();
  fireEvent.click(screen.getByRole("button",{name:/螺纹2610 SHFE.rb2610/}));
  const drawer=screen.getByRole("dialog",{name:"合约列表"});
  const search=within(drawer).getByPlaceholderText("搜索合约代码或名称");
  fireEvent.change(search,{target:{value:"zzzzqa"}});
  expect(within(drawer).getByText("没有匹配的品种或合约")).toBeInTheDocument();
  expect(within(drawer).queryByRole("button",{name:/SHFE\.rb2610/})).toBeNull();
  expect(within(drawer).getByText(/游客只能搜索和查看螺纹主力合约/)).toBeInTheDocument();
  fireEvent.change(search,{target:{value:"rb2610"}});
  expect(within(drawer).getByRole("button",{name:/SHFE\.rb2610/})).toBeInTheDocument();
  expect(within(drawer).queryByRole("button",{name:/SHFE\.au2612/})).toBeNull();
  expect(within(drawer).queryByText("没有匹配的品种或合约")).toBeNull();
  fireEvent.change(search,{target:{value:""}});
  expect(within(drawer).getByRole("button",{name:/SHFE\.au2612/})).toBeInTheDocument();
  expect(within(drawer).queryByText("没有匹配的品种或合约")).toBeNull();
});

it("合约抽屉、全部周期和设置使用共享入口；取消登录保留视图，成功登录卸载游客",async()=>{
  await enter();
  fireEvent.click(screen.getByRole("button",{name:/螺纹2610 SHFE.rb2610/}));
  fireEvent.change(screen.getByPlaceholderText("搜索合约代码或名称"),{target:{value:"沪金"}});
  fireEvent.click(screen.getByRole("button",{name:/沪金2612/}));
  expect(mocks.stream).toHaveBeenLastCalledWith(expect.stringContaining("symbol=SHFE.au2612"));
  frame("snapshot",{workbench:{...market,quote:{...market.quote,symbol:"SHFE.au2612",name:"沪金2612"}}});
  for(const [name,duration] of [["1分",60],["3分",180],["5分",300],["15分",900],["1小时",3600],["日线",86400]] as const) {
    fireEvent.click(within(screen.getByRole("group",{name:"K线周期"})).getByRole("button",{name}));
    expect(mocks.stream).toHaveBeenLastCalledWith(expect.stringContaining(`duration=${duration}`));
    frame("snapshot",{workbench:{...market,durationSeconds:duration,quote:{...market.quote,symbol:"SHFE.au2612",name:"沪金2612"}}});
  }
  fireEvent.click(screen.getByRole("button",{name:"设置"}));
  fireEvent.click(screen.getByRole("button",{name:"界面"}));
  fireEvent.click(screen.getByRole("radio",{name:"浅色"}));
  fireEvent.click(screen.getByRole("radio",{name:"特大 120%"}));
  expect(document.documentElement.dataset.appTheme).toBe("light");
  expect(document.documentElement.dataset.fontScale).toBe("extra-large");
  fireEvent.click(screen.getByRole("button",{name:"登录"}));
  fireEvent.click(screen.getByText("取消登录"));
  await waitFor(()=>expect(latestSnapshot()).toMatchObject({symbol:"SHFE.au2612",duration:86400,theme:"light",fontScale:"extra-large",settingsOpen:true,settingsPage:"ui"}));
  fireEvent.click(screen.getByRole("button",{name:"登录"})); fireEvent.click(screen.getByText("登录成功"));
  expect(await screen.findByText("私人工作区")).toBeVisible();
  expect(screen.queryByRole("navigation",{name:"辅助工具"})).toBeNull();
  expect(guestAccessTokenFor(base)).toBeUndefined(); assertPublicOnly();
});
it("空闲与行情tick不重复提交UI快照或HTTP轮询",async()=>{
  vi.useFakeTimers();
  try {
    render(<GuestEntry mode="cloud"><div>私人工作区</div></GuestEntry>);
    await act(async()=>vi.advanceTimersByTimeAsync(300));
    frame("snapshot",{workbench:market});
    await act(async()=>vi.advanceTimersByTimeAsync(300));
    const count=posts().length; expect(count).toBeGreaterThan(0);
    for(let n=0;n<10;n++) frame("update",{quote:{...market.quote,lastPrice:825+n}});
    await act(async()=>vi.advanceTimersByTimeAsync(10000));
    expect(posts()).toHaveLength(count);
    expect(requests.filter(url=>url.includes("/instruments"))).toHaveLength(1);
    expect(vi.mocked(fetch).mock.calls.some(([url,init])=>String(url).endsWith("/api/guest/ui") && init?.method!=="POST")).toBe(false);
    fireEvent.click(screen.getByRole("button",{name:"打开调研面板"}));
    await act(async()=>vi.advanceTimersByTimeAsync(600));
    expect(posts().length).toBeGreaterThan(count); expect(latestSnapshot().newsOpen).toBe(true); assertPublicOnly();
  } finally {cleanup();vi.useRealTimers();}
});
it("#712 游客会话创建还在途时页面卸载，迟到的游客令牌不再装进内存",async()=>{
  let answer:(response:Response)=>void=()=>undefined;
  const original=vi.mocked(fetch).getMockImplementation()!;
  let asked=false;
  vi.mocked(fetch).mockImplementation(async (input:any, init:any) => String(input).endsWith("/api/auth/guest")
    ? new Promise<Response>(resolve=>{ asked=true; answer=resolve; }) : original(input, init));
  const view=render(<GuestEntry mode="cloud"><div>私人工作区</div></GuestEntry>);
  await waitFor(()=>expect(asked).toBe(true));
  view.unmount();
  await act(async()=>{ answer(Response.json(session)); await new Promise(resolve=>setTimeout(resolve,0)); });
  expect(guestAccessTokenFor(base)).toBeUndefined();
});
it("过期重连保留周期布局、更换令牌并隔离旧SSE",async()=>{
  const view=await enter();
  fireEvent.click(within(screen.getByRole("group",{name:"K线周期"})).getByRole("button",{name:"15分"}));
  frame("snapshot",{workbench:{...market,durationSeconds:900}});
  frame("ui",{windowId:"w1",revision:1,changes:{dockSide:"left",panel:"news"}});
  fireEvent.click(await screen.findByRole("button",{name:"相关报道 ›"}));
  await screen.findByText("公共行情新闻");
  const old=mocks.frames;
  vi.mocked(fetch).mockResolvedValueOnce(new Response("",{status:401}));
  await act(async()=>old.fetchImpl(base+"/api/guest/stream",{headers:{Authorization:"Bearer owner.secret","X-AI-Trader-Account":"private"}}));
  expect(await screen.findByText("游客会话已失效，请重新连接")).toBeVisible();
  nextSession={...session,windowId:"w2",accessToken:"guest.second"};
  fireEvent.click(screen.getByRole("button",{name:"重新连接"}));
  await waitFor(()=>expect(guestAccessTokenFor(base)).toBe("guest.second"));
  expect(mocks.stream).toHaveBeenLastCalledWith(expect.stringContaining("duration=900"));
  frame("snapshot",{workbench:{...market,durationSeconds:900}});
  act(()=>old.onFrame({event:"ui",data:[JSON.stringify({windowId:"w1",revision:99,changes:{theme:"dark"}})]}));
  await waitFor(()=>expect(latestSnapshot()).toMatchObject({duration:900,dockSide:"left",panel:"news",appliedRevision:-1}));
  const call=vi.mocked(fetch).mock.calls.filter(([url])=>String(url).endsWith("/api/guest/instruments")).at(-1);
  expect(new Headers(call?.[1]?.headers).get("Authorization")).toBe("Bearer guest.second");
  assertPublicOnly(); view.unmount(); expect(guestAccessTokenFor(base)).toBeUndefined();
});
it("SSE切合约周期的revision在重建行情流后仍正确回执",async()=>{
  await enter();
  frame("ui",{windowId:"w1",revision:7,changes:{symbol:"SHFE.au2612",duration:900}});
  await waitFor(()=>expect(mocks.stream).toHaveBeenLastCalledWith(expect.stringContaining("symbol=SHFE.au2612&duration=900")));
  frame("snapshot",{workbench:{...market,durationSeconds:900,quote:{...market.quote,symbol:"SHFE.au2612",name:"沪金2612"}}});
  await waitFor(()=>expect(latestSnapshot()).toMatchObject({symbol:"SHFE.au2612",duration:900,appliedRevision:7}));
  assertPublicOnly();
});
/** 手机版游客: the window answers the phone-layout query; the 调研台 serves its public facts. */
function phone() {
  vi.stubGlobal("matchMedia", vi.fn((query:string) => ({matches:query===MOBILE_LAYOUT_QUERY,media:query,addEventListener:vi.fn(),removeEventListener:vi.fn()})));
  const shared=vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input:any, init:any) => String(input).includes("/api/guest/research/desk") ? (requests.push(String(input)), Response.json({
    symbol:"SHFE.rb2610",productName:"螺纹钢",mainSymbol:"SHFE.rb2610",generatedAt:"2026-10-04T01:00:00Z",notice:"公开事实",
    sections:[{key:"inventory",title:"库存",count:1,status:"ok",kind:"data",source:"交易所"}],
    facts:[{id:"f1",section:"inventory",kind:"data",label:"螺纹社会库存",value:"512 万吨",source:"交易所",asOf:"2026-10-03",status:"ok"}],
    judgment:{status:"signin_required"} })) : shared(input, init));
}
const closeLogin = () => { expect(screen.getByRole("dialog",{name:"登录"})).toBeVisible(); fireEvent.click(screen.getByText("取消登录")); };
it("手机竖屏的游客进入手机布局：默认看盘、底部四个标签没有说话键；今天 / 策略是登录后使用页；下单、设提醒、问 AI 打开登录框",async()=>{
  phone(); await enter();
  expect(screen.queryByRole("navigation",{name:"主要工作区"})).toBeNull();
  const nav=screen.getByRole("navigation",{name:"主导航"});
  expect(within(nav).getAllByRole("button").map(button=>button.textContent)).toEqual(["今天","调研","看盘","策略"]);
  expect(within(nav).getByRole("button",{name:"看盘"})).toHaveAttribute("aria-current","page");
  expect(screen.getByTestId("mobile-market")).toHaveTextContent("螺纹2610");
  expect(screen.queryByText(/快期账户/)).toBeNull();
  for(const name of ["下单","设提醒","问 AI 这根K线"]) { fireEvent.click(screen.getByRole("button",{name})); closeLogin(); }
  expect(screen.queryByRole("dialog",{name:"下单"})).toBeNull();
  for(const [tab,title] of [["今天","今天"],["策略","策略"]]) {
    fireEvent.click(within(nav).getByRole("button",{name:tab}));
    const page=screen.getByTestId("mobile-signin");
    expect(page).toHaveAccessibleName(title);
    expect(page).toHaveTextContent("登录后使用");
    expect(page).toHaveTextContent("新用户填邀请码即可创建");
    fireEvent.click(within(page).getByRole("button",{name:"登录"})); closeLogin();
  }
  expect(screen.queryByRole("button",{name:/按住说话/})).toBeNull();
  fireEvent.click(within(nav).getByRole("button",{name:"调研"}));
  expect(await screen.findByText("登录后可看 AI 判断；下面是数据与资料来源。")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"展开资料来源"}));
  expect(screen.getByText("螺纹社会库存")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"就这份判断问 AI"})); closeLogin();
  assertPublicOnly();
});
it("游客手机窗口的 mobileTab 命令切标签并回执，快照上报 mobileLayout / mobileTab；对话要登录，电脑布局拒绝",async()=>{
  phone(); const view=await enter();
  await waitFor(()=>expect(latestSnapshot()).toMatchObject({mobileLayout:true,mobileTab:"market"}));
  frame("ui",{windowId:"w1",revision:1,changes:{mobileTab:"me"}});
  await waitFor(()=>expect(latestSnapshot()).toMatchObject({appliedRevision:1,mobileTab:"me"}));
  expect(screen.getByTestId("mobile-signin")).toHaveAccessibleName("我的");
  frame("ui",{windowId:"w1",revision:2,changes:{mobileTab:"chat"}});
  await waitFor(()=>expect(latestSnapshot().notice).toBe("界面操作未完成，请重试"));
  expect(latestSnapshot()).toMatchObject({appliedRevision:1,mobileTab:"me"});
  assertPublicOnly(); view.unmount();
  vi.mocked(fetch).mockClear(); mocks.frames=undefined;
  vi.stubGlobal("matchMedia", vi.fn(() => ({matches:false,addEventListener:vi.fn(),removeEventListener:vi.fn()})));
  await enter();
  expect(screen.getByRole("navigation",{name:"主要工作区"})).toBeVisible();
  await waitFor(()=>expect(posts().length).toBeGreaterThan(0));
  expect(latestSnapshot()).not.toHaveProperty("mobileLayout");
  frame("ui",{windowId:"w1",revision:1,changes:{mobileTab:"market"}});
  await waitFor(()=>expect(latestSnapshot().notice).toBe("界面操作未完成，请重试"));
});
