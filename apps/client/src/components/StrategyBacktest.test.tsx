import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { StrategyWorkbench } from "./StrategyWorkbench";
import { Conversation } from "./Conversation";
import { resetUiSurfaces, uiSurface } from "../uiSurfaces";
import { currentBacktestMarks, resetAiAnnotations } from "../aiAnnotations";
import { resetStrategyBacktests } from "../strategyBacktests";
import { conversationReducer, initialConversationState } from "../state/conversation";
import type { StrategyPlan } from "../strategyApi";
import type { StrategyBacktestResult } from "@ai-trader/ui-events";
import type { MarketWorkbenchSnapshot } from "../api";

const request = vi.hoisted(() => vi.fn());
// 结果页 (2026-10-08) also reads the stored result, the last comparison and the template catalog on opening.
vi.mock("../api", () => ({ getStrategyMonitor: vi.fn(), stopStrategyMonitoring: vi.fn(),
  requestJson: (url: string, init?: RequestInit) => /strategy-backtests\?strategyId=/.test(url) ? Promise.resolve({items:[]})
    : /\/compare$/.test(url) && !init?.method ? Promise.resolve({status:"none", labId:null, rows:[]})
    : /strategy-templates$/.test(url) ? Promise.resolve({categories:[], items:[]}) : request(url, init) }));

const plan: StrategyPlan = {name:"双均线",description:"MA5 上穿 MA20 开多",direction:"long",timeframeSeconds:300,maxPosition:1,maxLoss:1000,stopLossTicks:20,takeProfitTicks:null,cooldownBars:0,maxOrdersPerDay:20,questions:[],clarifications:[],
  rules:{entryLong:{join:"all",sequence:false,conditions:[{label:"MA5上穿MA20",left:{indicator:"ma",period:5,timeframe:300,value:0,barsAgo:0},operator:"cross_up",right:{indicator:"ma",period:20,timeframe:300,value:0,barsAgo:0},withinBars:1,consecutive:1,minCount:1}]},exitLong:null,entryShort:null,exitShort:null}};
const saved = {strategyId:"library",name:"双均线",symbol:"*",version:2,status:"draft",definition:{schemaVersion:2,plan,instrumentScope:{mode:"any",symbols:[]}}};
const T0 = 1_788_192_000_000_000_000, BAR = 300_000_000_000;

function result(overrides: Partial<StrategyBacktestResult> = {}): StrategyBacktestResult {
  return {backtestId:"bt-1",kind:"rule",strategyId:"library",strategyName:"双均线",symbol:"SHFE.au2612",timeframeSeconds:300,
    params:{tradingDays:null,slippageTicks:0,initialBalance:1_000_000},
    summary:{netProfit:-10,returnPercent:0,maxDrawdown:150,maxDrawdownPercent:0.15,winRate:50,payoffRatio:0.88,trades:2,averageBarsHeld:4,fees:10},
    exitReasons:[{reason:"stop_loss",label:"止损",count:1,net:-85},{reason:"rule",label:"规则出场",count:1,net:75}],
    equityCurve:[{time:T0,equity:1_000_000},{time:T0 + BAR,equity:1_000_075},{time:T0 + 2 * BAR,equity:999_990}],
    trades:[{id:1,direction:"long",volume:1,openTime:T0 + BAR,openPrice:104,closeTime:T0 + 3 * BAR,closePrice:112,exitReason:"rule",exitLabel:"规则出场",barsHeld:2,gross:80,fees:5,net:75},
      {id:2,direction:"long",volume:1,openTime:T0 - 100 * BAR,openPrice:104,closeTime:T0 - 90 * BAR,closePrice:96,exitReason:"stop_loss",exitLabel:"止损",barsHeld:10,gross:-80,fees:5,net:-85}],
    coverage:{firstBarTime:T0 - 200 * BAR,lastBarTime:T0 + 5 * BAR,bars:205,tradingDays:3,firstTradingDay:"2026-08-28",lastTradingDay:"2026-09-01",requestedTradingDays:null,limited:false},
    notes:["手续费按交易所标准估算（openctp 汇总）。"],assumptions:["按收盘确认、下一根开盘成交。"],...overrides};
}

const market = {durationSeconds:300,quote:{symbol:"SHFE.au2612"},bars:Array.from({length:10},(_, i) => ({datetime:T0 + i * BAR,open:1,high:1,low:1,close:1,volume:1}))} as unknown as MarketWorkbenchSnapshot;

function routes(backtest: () => StrategyBacktestResult = () => result()) {
  request.mockImplementation(async (url: string) => url.endsWith("/api/strategy-plans") ? {items:[saved],executions:[]}
    : url.includes("backtest") ? backtest() : {conditions:[]});
}

beforeEach(() => { localStorage.clear(); vi.clearAllMocks(); resetUiSurfaces(); resetStrategyBacktests(); resetAiAnnotations(); });

async function openDetail(props: Partial<Parameters<typeof StrategyWorkbench>[0]> = {}) {
  render(<StrategyWorkbench rootUrl="http://main" baseUrl="http://runtime" symbol="SHFE.au2612" chartTimeframe={300} replayActive={false}
    workspaceScope="backtest" market={market} onAskAi={vi.fn()} backtestBlocked={null} {...props}/>);
  fireEvent.click(await screen.findByRole("button", {name:/^双均线 · /}));
}
/** 结果页的“回测”, with the 更多 fold (回测明细) open so the result card is visible. */
async function backtestOnDetail() {
  fireEvent.click(await screen.findByRole("button", {name:"回测"}));
  act(() => uiSurface("strategyDetails")!.apply!({section:"rules", open:true}));
}
/** POSTs with their bodies; every run carries a fresh requestId (its place in the Runtime's queue), checked apart. */
const posts = () => request.mock.calls.filter(([, init]) => init?.method === "POST").map(([url, init]) => {
  const { requestId, ...body } = JSON.parse(String(init.body)) as Record<string, unknown>;
  expect(requestId).toMatch(/^[A-Za-z0-9_-]+$/);
  return [url, body];
});

test("详情的回测直接调规则回测接口，结果卡显示核心数字与出场原因", async () => {
  routes();
  await openDetail();
  await backtestOnDetail();
  const card = await screen.findByTestId("strategy-backtest-card");
  expect(posts()).toEqual([["http://main/api/strategies/library/backtest", {symbol:"SHFE.au2612", slippageTicks:0, tradingDays:120, fallbackReason:"unsupported"}]]);
  expect(within(card).getByText("规则回测 · SHFE.au2612 · 5 分钟")).toBeVisible();
  const metrics = within(card).getAllByRole("definition").map(item => item.textContent);
  expect(metrics).toEqual(["-10", "1500.2%", "50.0%", "0.88", "2"]);   // 回撤 150（0.2%）
  expect(within(card).getByRole("list", {name:"出场原因"})).toHaveTextContent("规则出场1 笔+75止损1 笔-85");
  expect(within(card).getByRole("img", {name:/资金曲线/})).toBeInTheDocument();
  expect(within(card).getByText(/2026-08-28 至 2026-09-01 · 3 个交易日/)).toBeVisible();
  expect(within(card).getByText(/交易所标准估算/)).toBeVisible();
  // 逐笔清单 starts folded and is commandable through the card's surface.
  expect(within(card).getByText(/逐笔清单（2 笔）/).closest("details")).not.toHaveAttribute("open");
  act(() => uiSurface("strategyBacktest")!.apply!({trades: true}));
  expect(within(card).getByText(/逐笔清单（2 笔）/).closest("details")).toHaveAttribute("open");
  expect(uiSurface("strategyBacktest")!.state).toMatchObject({strategyId:"library", running:false, tradesOpen:true,
    summary:{netProfit:-10, maxDrawdown:150, winRate:50, payoffRatio:0.88, trades:2}});
});

test("改区间与滑点后重跑，第二次请求带新参数；命令也能改参数并回测", async () => {
  let run = 0;
  routes(() => result({backtestId:`bt-${++run}`}));
  await openDetail();
  await backtestOnDetail();
  const card = await screen.findByTestId("strategy-backtest-card");
  fireEvent.change(within(card).getByLabelText("区间 · 交易日"), {target:{value:"20"}});
  fireEvent.change(within(card).getByLabelText("滑点"), {target:{value:"1"}});
  fireEvent.click(within(card).getByRole("button", {name:"重跑"}));
  await waitFor(() => expect(posts()).toHaveLength(2));
  expect(posts()[1][1]).toEqual({symbol:"SHFE.au2612", slippageTicks:1, tradingDays:20, fallbackReason:"unsupported"});
  act(() => uiSurface("strategyBacktest")!.apply!({tradingDays:5, slippageTicks:0, run:true}));
  await waitFor(() => expect(posts()).toHaveLength(3));
  expect(posts()[2][1]).toEqual({symbol:"SHFE.au2612", slippageTicks:0, tradingDays:5, fallbackReason:"unsupported"});
  await waitFor(() => expect(uiSurface("strategyBacktest")!.state).toMatchObject({params:{tradingDays:5, slippageTicks:0}, running:false}));
});

test("在图上标出买卖点：同合约同周期直接画；图表不一致先切过去", async () => {
  routes();
  const onShowChart = vi.fn();
  await openDetail({onShowChart});
  await backtestOnDetail();
  const card = await screen.findByTestId("strategy-backtest-card");
  fireEvent.click(within(card).getByRole("button", {name:"在图上标出买卖点"}));
  expect(currentBacktestMarks()).toMatchObject({backtestId:"bt-1", symbol:"SHFE.au2612", durationSeconds:300});
  expect(currentBacktestMarks()!.items.map(item => item.label)).toEqual(["开多", "平多 +75", "开多", "平多 -85"]);
  expect(onShowChart).not.toHaveBeenCalled();
  // Only the first trade falls inside the chart's loaded bars.
  expect(within(card).getByText("图上显示 1 / 2 笔；其余不在图表已加载的K线范围内。")).toBeVisible();
  expect(uiSurface("strategyBacktest")!.state).toMatchObject({marked:true});
  fireEvent.click(within(card).getByRole("button", {name:"清除买卖点"}));
  expect(currentBacktestMarks()).toBeNull();
  // A backtest on another period switches the chart first.
  routes(() => result({backtestId:"bt-daily", timeframeSeconds:86400}));
  fireEvent.click(within(card).getByRole("button", {name:"重跑"}));
  await within(card).findByText("规则回测 · SHFE.au2612 · 日线");
  act(() => uiSurface("strategyBacktest")!.apply!({mark:true}));
  expect(onShowChart).toHaveBeenCalledWith("SHFE.au2612", 86400);
  expect(currentBacktestMarks()?.durationSeconds).toBe(86400);
});

test("回放中用主 Runtime 的不保存回测入口（子 Runtime 没有历史行情）", async () => {
  routes();
  await openDetail({replayActive:true});
  await backtestOnDetail();
  await screen.findByTestId("strategy-backtest-card");
  const [[url, body]] = posts();
  expect(url).toBe("http://main/api/strategy-backtests");
  expect(body).toMatchObject({symbol:"SHFE.au2612", strategyId:"library", plan:{name:"双均线"}});
});

test("加仓与分批止盈的逐笔行：含加仓次数的持仓一行，分批平仓的档写分批止盈（第六批）", async () => {
  routes(() => result({trades:[
    {id:1,direction:"long",volume:4,adds:3,openTime:T0 + BAR,openPrice:104,closeTime:T0 + 3 * BAR,closePrice:112,exitReason:"rule",exitLabel:"规则出场",barsHeld:2,gross:320,fees:20,net:300},
    {id:2,direction:"long",volume:2,partial:true,openTime:T0 + 10 * BAR,openPrice:104,closeTime:T0 + 12 * BAR,closePrice:116,exitReason:"scale_out",exitLabel:"分批止盈",barsHeld:2,gross:240,fees:10,net:230}],
    exitReasons:[{reason:"scale_out",label:"分批止盈",count:1,net:230},{reason:"rule",label:"规则出场",count:1,net:300}]}));
  await openDetail();
  await backtestOnDetail();
  const card = await screen.findByTestId("strategy-backtest-card");
  expect(within(card).getByRole("list", {name:"出场原因"})).toHaveTextContent("分批止盈1 笔+230");
  act(() => uiSurface("strategyBacktest")!.apply!({trades: true}));   // jsdom does not toggle <details> on summary clicks
  expect(within(card).getByText(/逐笔清单（2 笔）/).closest("details")).toHaveAttribute("open");
  expect(within(card).getByText(/#1 多 4 手（含加仓 3 次）/)).toBeVisible();
  expect(within(card).getByText(/#2 多 2 手/)).toBeVisible();
  expect(within(card).getAllByText(/分批止盈 · 2 根/)).toHaveLength(1);
});

test("回测失败如实显示原因", async () => {
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("/api/strategy-plans")) return {items:[saved],executions:[]};
    if (url.includes("backtest")) throw Object.assign(new Error("503"), {status:503, detail:{detail:"strategy_backtest_history_unavailable"}});
    return {conditions:[]};
  });
  await openDetail();
  await backtestOnDetail();
  // On the 结果页 and in its 回测明细 alike.
  expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent("当前数据源拿不到历史K线，无法回测");
});

test("排队时结果卡显示“排队中（前面还有 N 个）”，开始后显示“回测中…”；排队超时如实显示", async () => {
  let finish: (value: StrategyBacktestResult) => void = () => undefined;
  let queueState = {state:"queued", ahead:2};
  let requestId = "";
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/strategy-plans")) return {items:[saved],executions:[]};
    if (url.includes("/api/strategy-backtests/queue")) {
      expect(url).toBe(`http://main/api/strategy-backtests/queue?requestId=${requestId}`);
      return {items:[{requestId, ...queueState}], limits:{global:2, perUser:1, waitSeconds:60}};
    }
    if (url.endsWith("/backtest")) {
      requestId = JSON.parse(String(init!.body)).requestId;
      return new Promise<StrategyBacktestResult>(resolve => { finish = resolve; });
    }
    return {conditions:[]};
  });
  await openDetail();
  await backtestOnDetail();
  const card = await screen.findByTestId("strategy-backtest-card");
  expect(within(card).getAllByText("回测中…")).toHaveLength(2);   // badge and button
  expect(await within(card).findByText("排队中（前面还有 2 个）", undefined, {timeout:3000})).toBeVisible();
  expect(within(card).getByRole("button", {name:"排队中…"})).toBeDisabled();
  expect(screen.getAllByRole("button", {name:"排队中…"})).toHaveLength(1);   // 结果页只剩回测明细里的这一个
  expect(uiSurface("strategyBacktest")!.state).toMatchObject({running:true, queueAhead:2});
  queueState = {state:"running", ahead:0};
  await waitFor(() => expect(within(card).getAllByText("回测中…")).toHaveLength(2), {timeout:3000});
  expect(within(card).queryByText(/排队中/)).toBeNull();
  expect(uiSurface("strategyBacktest")!.state).toMatchObject({running:true, queueAhead:null});
  await act(async () => finish(result()));
  expect(await within(card).findByText("规则回测 · SHFE.au2612 · 5 分钟")).toBeVisible();
  expect(within(card).queryByText(/排队中|回测中/)).toBeNull();
  expect(uiSurface("strategyBacktest")!.state).toMatchObject({running:false, queueAhead:null});
  // Waiting longer than the Runtime's queue allows: its reason, as it is.
  request.mockImplementation(async (url: string) => {
    if (url.includes("/api/strategy-backtests/queue")) return {items:[]};
    if (url.endsWith("/backtest")) throw Object.assign(new Error("429"), {status:429, detail:{detail:"回测排队中，稍后再试"}});
    return {conditions:[]};
  });
  fireEvent.click(within(card).getByRole("button", {name:"重跑"}));
  expect(await within(card).findByRole("alert")).toHaveTextContent("回测排队中，稍后再试");
});

test("对话里的规则回测卡注明“规则回测”，逐笔清单可折叠", () => {
  const card = {...result(), turnId:"turn-1", taskId:"task-1", tradesTotal:2};
  let state = conversationReducer(initialConversationState, {type:"event", event:{schemaVersion:1,id:"m",sessionId:"s",kind:"message",name:"message.completed",timestamp:"t",payload:{turnId:"turn-1",role:"assistant",text:"规则回测已完成"}}} as never);
  state = conversationReducer(state, {type:"event", event:{schemaVersion:1,id:"c",sessionId:"s",kind:"chart",name:"card.strategy_backtest",timestamp:"t",payload:card}} as never);
  expect(state.turns[0].cards).toEqual([{id:"strategy-backtest-bt-1", type:"strategy-backtest", payload:card}]);
  render(<Conversation turns={state.turns} actions={{} as never} />);
  const shown = screen.getByTestId("strategy-backtest-result");
  expect(within(shown).getByText("规则回测")).toBeVisible();
  expect(within(shown).getByText("双均线 · 规则回测 · SHFE.au2612 · 5 分钟")).toBeVisible();
  expect(uiSurface("disclosure:strategy-backtest-trades:strategy-backtest-bt-1")!.state).toBe(false);
});

test("价差回测：标题与两条曲线各有名字，买卖点标在价差图上，逐笔按份价差并带两腿详情", () => {
  const card = {...result({
    symbol: "SHFE.rb2701", summary:{...result().summary, trades:1},
    spread: {kind:"difference", legs:[{symbol:"SHFE.rb2701", lots:1, weight:1, priceTick:1, volumeMultiple:10},
      {symbol:"SHFE.rb2705", lots:1, weight:1, priceTick:1, volumeMultiple:10}]},
    spreadCurve: [{time:T0, value:4}, {time:T0 + BAR, value:20}, {time:T0 + 2 * BAR, value:12}, {time:T0 + 3 * BAR, value:6}],
    trades:[{id:1, direction:"long", volume:1, openTime:T0 + BAR, openPrice:20, closeTime:T0 + 3 * BAR, closePrice:6,
      exitReason:"spread_stop", exitLabel:"价差止损", barsHeld:2, gross:140, fees:10, net:130,
      legs:[{symbol:"SHFE.rb2701", direction:"buy", volume:1, openTime:T0 + BAR, openPrice:104, closeTime:T0 + 3 * BAR, closePrice:112, gross:80, fees:5},
        {symbol:"SHFE.rb2705", direction:"sell", volume:1, openTime:T0 + BAR, openPrice:84, closeTime:T0 + 3 * BAR, closePrice:106, gross:60, fees:5}]}],
    exitReasons:[{reason:"spread_stop", label:"价差止损", count:1, net:130}]}), turnId:"turn-1", taskId:"task-1"};
  let state = conversationReducer(initialConversationState, {type:"event", event:{schemaVersion:1,id:"m",sessionId:"s",kind:"message",name:"message.completed",timestamp:"t",payload:{turnId:"turn-1",role:"assistant",text:"价差回测已完成"}}} as never);
  state = conversationReducer(state, {type:"event", event:{schemaVersion:1,id:"c",sessionId:"s",kind:"chart",name:"card.strategy_backtest",timestamp:"t",payload:card}} as never);
  render(<Conversation turns={state.turns} actions={{} as never} />);
  const shown = screen.getByTestId("strategy-backtest-result");
  expect(within(shown).getByText("双均线 · 规则回测 · SHFE.rb2701 − SHFE.rb2705 价差 · 5 分钟")).toBeVisible();
  // Two curves, each named: the equity line and the spread's own 走势.
  expect(within(shown).getByText("资金曲线")).toBeVisible();
  expect(within(shown).getByText("价差走势")).toBeVisible();
  expect(within(shown).getByRole("img", {name:"价差：4.00 → 6.00"})).toBeInTheDocument();
  expect(within(shown).getByRole("list", {name:"出场原因"})).toHaveTextContent("价差止损1 笔+130");
  // The trade's prices are spread values, so its 开仓/平仓 marks live on the spread chart itself.
  const marks = within(shown).getByRole("img", {name:"价差：4.00 → 6.00"}).querySelectorAll("polygon title");
  expect([...marks].map(node => node.textContent)).toEqual(["开仓 20.00", "平仓 6.00"]);
  expect(within(shown).getByText(/逐笔清单（1 笔）/)).toBeVisible();
  expect(within(shown).getByText(/#1 多 1 份价差/)).toBeInTheDocument();
  // The legs' own fills fold into the trade's 详情 (a closed <details> still holds them).
  const legs = shown.querySelector(".strategy-backtest-legs")!.textContent;
  expect(legs).toContain("SHFE.rb2701 · 买 1 手");
  expect(legs).toContain("SHFE.rb2705 · 卖 1 手");
});

// 策略体检 (2026-09-26): the backtest opened nothing — 详情 says where it is stuck and 按这个改 saves a new version.
const blocked = {...plan, rules:{...plan.rules, entryLong:{...plan.rules.entryLong!, conditions:[plan.rules.entryLong!.conditions![0],
  {label:"收盘低于0",left:{indicator:"close",timeframe:300,value:0},operator:"lt",right:{indicator:"constant",timeframe:300,value:0},withinBars:1,consecutive:1,minCount:1}]}}} as StrategyPlan;
const DROP = {id:"drop-1", kind:"drop" as const, groups:["entryLong" as const], index:1, label:"收盘低于0", passes:{entryLong:12}, text:"去掉「收盘低于0」：开多约 12 次"};
const HEADLINE = "按现在的规则，近 3 个交易日开多条件同时满足 0 次，卡在「收盘低于0」。以下建议只按开仓次数给，不按盈亏挑参数。";
const stuck = (overrides: Partial<StrategyBacktestResult> = {}) => result({summary:{...result().summary, trades:0, netProfit:0, winRate:null, payoffRatio:null},
  trades:[], exitReasons:[], headline:HEADLINE, advice:[DROP],
  diagnosis:{entryLong:{judgedBars:200, allPass:0, relaxedCrossPass:0, conditions:[
    {label:"MA5上穿MA20", alone:12, without:0, cross:true}, {label:"收盘低于0", alone:0, without:12, cross:false}]}}, ...overrides});
const blockedSaved = {...saved, definition:{...saved.definition, plan:blocked}};

test("策略体检：详情顶部警示块列出各条件次数、标出卡住的条件；按这个改存新版本并重跑回测", async () => {
  const v3 = {...saved, strategyId:"library-v3", version:3};
  let applied = false;
  request.mockImplementation(async (url: string) => url.endsWith("/api/strategy-plans")
    ? {items: applied ? [v3, blockedSaved] : [blockedSaved], executions:[]}
    : url.endsWith("/apply-advice") ? (applied = true, v3)
    : url.includes("/library-v3/backtest") ? result({strategyId:"library-v3", backtestId:"bt-2"})
    : url.includes("backtest") ? stuck() : {conditions:[]});
  await openDetail();
  await backtestOnDetail();
  const health = await screen.findByTestId("strategy-health");
  expect(within(health).getByRole("heading", {name:"体检：这条策略几乎不会开仓"})).toBeVisible();
  expect(health).toHaveTextContent("SHFE.au2612 · 5 分钟 · 近 3 个交易日");
  expect(health).toHaveTextContent(HEADLINE);
  const rows = within(within(health).getByRole("table", {name:"开多条件体检"})).getAllByRole("row").map(row => row.textContent);
  expect(rows).toEqual(["开多条件单独满足去掉它，其余满足", "MA5上穿MA2012 次0 次", "收盘低于00 次12 次 ← 卡在这"]);
  // The advised condition is flagged in ① 什么时候开仓, and it is the only flagged row.
  const entry = screen.getByRole("region", {name:"什么时候开仓"});
  const flagged = entry.querySelectorAll('.rule-row[data-kind="flag"]');
  expect(flagged).toHaveLength(1);
  expect(flagged[0]).toHaveAttribute("title", "收盘低于0");
  expect(uiSurface("strategyBacktest")!.state).toMatchObject({health:{headline:HEADLINE, advice:[{id:"drop-1", text:DROP.text}]}, applying:false});
  fireEvent.click(within(health).getByRole("button", {name:"体检的说明"}));
  expect(screen.getByRole("tooltip")).toHaveTextContent("改完存为新版本并自动重跑回测；原版本保留。");

  fireEvent.click(within(health).getByRole("button", {name:"按这个改"}));
  await waitFor(() => expect(request.mock.calls.filter(([, init]) => init?.method === "POST").map(([url]) => url)).toEqual([
    "http://main/api/strategies/library/backtest", "http://main/api/strategy-plans/library/apply-advice",
    "http://main/api/strategies/library-v3/backtest"]));
  const applyCall = request.mock.calls.find(([url]) => String(url).endsWith("/apply-advice"))!;
  expect(JSON.parse(String(applyCall[1].body))).toEqual({advice: DROP});
  await waitFor(() => expect(screen.queryByTestId("strategy-health")).toBeNull());   // the new version opens trades
  expect(uiSurface("strategyBacktest")!.state).toMatchObject({strategyId:"library-v3", appliedAdvice:"drop-1", health:null});
});

test("策略体检：命令 applyAdvice 与按钮同一路径；结果里没有的建议被拒绝", async () => {
  request.mockImplementation(async (url: string) => url.endsWith("/api/strategy-plans") ? {items:[blockedSaved], executions:[]}
    : url.endsWith("/apply-advice") ? {...saved, strategyId:"library-v3", version:3} : url.includes("backtest") ? stuck() : {conditions:[]});
  await openDetail();
  await backtestOnDetail();
  await screen.findByTestId("strategy-health");
  expect(() => uiSurface("strategyBacktest")!.apply!({applyAdvice:"relax-cross"})).toThrow("当前回测结果里没有这条体检建议");
  act(() => uiSurface("strategyBacktest")!.apply!({applyAdvice:"drop-1"}));
  await waitFor(() => expect(request.mock.calls.some(([url]) => String(url).endsWith("/library/apply-advice"))).toBe(true));
});

test("对话里的规则回测卡：一笔没开时显示体检警示与建议文字（没有修改按钮）", () => {
  const card = {...stuck(), turnId:"turn-1", taskId:"task-1"};
  let state = conversationReducer(initialConversationState, {type:"event", event:{schemaVersion:1,id:"m",sessionId:"s",kind:"message",name:"message.completed",timestamp:"t",payload:{turnId:"turn-1",role:"assistant",text:"规则回测已完成"}}} as never);
  state = conversationReducer(state, {type:"event", event:{schemaVersion:1,id:"c",sessionId:"s",kind:"chart",name:"card.strategy_backtest",timestamp:"t",payload:card}} as never);
  render(<Conversation turns={state.turns} actions={{} as never} />);
  const warning = screen.getByTestId("strategy-backtest-health");
  expect(within(warning).getByRole("heading", {name:"体检：这条策略几乎不会开仓"})).toBeVisible();
  expect(warning).toHaveTextContent(HEADLINE);
  expect(within(warning).getByRole("list", {name:"体检建议"})).toHaveTextContent(DROP.text);
  expect(within(warning).queryByRole("button")).toBeNull();
});


test("off-plan backtest entries explain denial and never request a backtest", async () => {
  routes();
  await openDetail({ backtestBlocked: "当前档位未开通回测" });
  const before = request.mock.calls.length;
  for (const label of ["回测"]) {
    const entry = screen.getByRole("button", { name: label });
    expect(entry).toBeEnabled();
    fireEvent.click(entry);
  }
  expect(screen.getByText("当前档位未开通回测", { selector: ".strategy-feedback" })).toBeVisible();
  expect(request.mock.calls.slice(before).filter(([url]) => String(url).includes("backtest"))).toEqual([]);
});
