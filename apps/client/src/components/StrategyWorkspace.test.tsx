// @vitest-environment jsdom
// Stays on jsdom: its assertions fail under the default happy-dom (accessible names / text / location differ).
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { FeeEditor, StrategyDesigner, StrategyWorkbench } from "./StrategyWorkbench";
import { usePersistentDraft } from "../hooks/usePersistentDraft";
import { UiNavigationContext } from "../uiNavigation";
import { disclosureStates, resetUiSurfaces, uiSurface } from "../uiSurfaces";
import type { StrategyPlan } from "../strategyApi";
import { resetStrategyBacktests } from "../strategyBacktests";
import { resetStrategyEditor } from "../strategyEditorStore";
import { resetReadIdentity } from "../runtimeClient";

const request = vi.hoisted(() => vi.fn());
const estimate = vi.hoisted(() => vi.fn());
vi.mock("../api", async () => ({ requestJson: request, getStrategyMonitor: vi.fn(), stopStrategyMonitoring: vi.fn(), getOrderEstimate: estimate,
  sameContract: (await vi.importActual<typeof import("../api")>("../api")).sameContract }));
const plan: StrategyPlan = {name:"我的策略",description:"均线和 KD 的交叉",direction:"both",timeframeSeconds:300,maxPosition:1,maxLoss:1000,stopLossTicks:20,takeProfitTicks:40,cooldownBars:1,maxOrdersPerDay:20,rules:{entryLong:null,exitLong:null,entryShort:null,exitShort:null},questions:["哪个周期？"],clarifications:[{id:"period",question:"你想在哪个周期看交叉？",recommended:0,options:[{label:"5 分钟",description:"适合盘中观察"},{label:"日线",description:"较慢的趋势信号"}]}]};
beforeEach(() => {localStorage.clear(); vi.clearAllMocks(); resetReadIdentity(); resetUiSurfaces(); resetStrategyBacktests(); resetStrategyEditor(); HTMLDialogElement.prototype.showModal = function () {this.setAttribute("open", "");}; HTMLDialogElement.prototype.close = function () {this.removeAttribute("open");};});

function Input({scope}: {scope:string}) {
  const [text,setText,error] = usePersistentDraft(`draft:${scope}`, "");
  return <><input aria-label="草稿" value={text} onChange={e=>setText(e.target.value)}/>{error && <p>未保存</p>}</>;
}
test("draft survives remount and isolates users; failed storage never erases typing", () => {
  const first=render(<Input scope="one"/>); fireEvent.change(screen.getByLabelText("草稿"),{target:{value:"未发送的内容"}}); first.unmount();
  const second=render(<Input scope="one"/>); expect(screen.getByLabelText("草稿")).toHaveValue("未发送的内容"); second.rerender(<Input scope="two"/>); expect(screen.getByLabelText("草稿")).toHaveValue("");
  const broken=vi.spyOn(Storage.prototype,"setItem").mockImplementation(()=>{throw new Error("quota");});
  fireEvent.change(screen.getByLabelText("草稿"),{target:{value:"即使保存失败也保留"}}); expect(screen.getByLabelText("草稿")).toHaveValue("即使保存失败也保留"); expect(screen.getByText("未保存")).toBeVisible(); broken.mockRestore();
});

test("strategy input and generated result survive navigation; clarification answers continue the same original idea", async () => {
  let finish: (value: unknown)=>void = ()=>{}; let job: Record<string,unknown>={};
  request.mockImplementation(async (url:string,init?:RequestInit) => {
    if (init?.method==="POST") {
      const body=JSON.parse(String(init.body));
      job={id:body.id,status:"running",stage:"模型正在分析",startedAt:new Date().toISOString()};
      if(body.constraints.clarificationRound===1) {job={...job,status:"completed",result:{...plan,clarifications:[],questions:[]}};return job;}
      return new Promise(resolve=>{finish=resolve;});
    }
    return job;
  });
  const props={rootUrl:"http://runtime",workspaceScope:"user-one",symbol:"SHFE.au2612",onSaved:vi.fn()};
  const first=render(<StrategyDesigner {...props}/>);
  fireEvent.change(screen.getByLabelText("描述你的交易想法"),{target:{value:"均线和 KD 的交叉"}});
  fireEvent.click(screen.getByRole("button",{name:"设计策略"}));
  await waitFor(()=>expect(request).toHaveBeenCalled()); first.unmount();
  job={...job,status:"completed",result:plan};
  await act(async()=>finish(job));
  render(<StrategyDesigner {...props} symbol="SHFE.rb2610"/>);
  await screen.findByText("你想在哪个周期看交叉？");
  expect(screen.getByLabelText("描述你的交易想法")).toHaveValue("均线和 KD 的交叉");
  expect(screen.getByRole("combobox",{name:"适用范围"})).toHaveValue("any");
  expect(screen.getByRole("button",{name:"回答并完善策略"})).toBeDisabled();
  fireEvent.click(screen.getByRole("radio",{name:/5 分钟/}));
  fireEvent.click(screen.getByRole("button",{name:"回答并完善策略"}));
  await waitFor(()=>expect(request.mock.calls.filter(([,init])=>init?.method==="POST")).toHaveLength(2));
  const body=JSON.parse(String(request.mock.calls.filter(([,init])=>init?.method==="POST")[1][1].body));
  expect(body.description).toBe("均线和 KD 的交叉");
  expect(body.constraints.clarificationAnswers).toEqual([{question:"你想在哪个周期看交叉？",answer:"5 分钟：适合盘中观察"}]);
  expect(body.constraints.clarificationRound).toBe(1);
});

test("legacy questions remain answerable in Chinese and keep original rules", async () => {
  const legacy = {...plan, questions:["止盈 takeProfitTicks=20，请确认"], clarifications:[]};
  request.mockImplementation(async (_url:string, init?:RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      return {id:body.id,status:"completed",result:{...legacy,questions:[]}};
    }
    return {};
  });
  render(<StrategyDesigner rootUrl="http://runtime" workspaceScope="legacy" symbol="SHFE.au2612" initialPlan={legacy} onSaved={vi.fn()}/>);
  expect(screen.getByText("止盈 止盈跳数=20，请确认")).toBeVisible();
  expect(screen.queryByText("草稿自动保存在本机")).not.toBeInTheDocument();
  expect(screen.queryByRole("radio")).toBeNull();
  fireEvent.change(screen.getByLabelText("自己补充"), {target:{value:"止盈设为20跳"}});
  fireEvent.click(screen.getByRole("button", {name:"回答并完善策略"}));
  await waitFor(() => expect(request.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
  const body = JSON.parse(String(request.mock.calls.find(([,init]) => init?.method === "POST")![1].body));
  expect(body.constraints.previousPlan).toEqual(legacy);
  expect(body.constraints.clarificationAnswers).toEqual([{question:legacy.questions[0],answer:"止盈设为20跳"}]);
});

test("failed generation gives an actionable retry and preserves the idea", async () => {
  let job: Record<string, unknown> = {};
  request.mockImplementation(async (_url:string, init?:RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      job = {id:body.id,status:"failed",stage:"等待模型生成策略超时，可以重试。",error:{code:"model_timeout",phase:"等待模型生成策略",action:"retry"}};
    }
    return job;
  });
  render(<StrategyDesigner rootUrl="http://runtime" workspaceScope="failed" symbol="SHFE.au2612" onSaved={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText("描述你的交易想法"),{target:{value:"只按照原来的均线条件"}});
  fireEvent.click(screen.getByRole("button",{name:"设计策略"}));
  await screen.findByText("等待模型生成策略超时，可以重试。");
  fireEvent.click(screen.getByRole("button",{name:"重新生成策略"}));
  await waitFor(() => expect(request.mock.calls.filter(([,init])=>init?.method==="POST")).toHaveLength(2));
  const bodies = request.mock.calls.filter(([,init])=>init?.method==="POST").map(([,init])=>JSON.parse(String(init.body)));
  expect(bodies[0].id).not.toBe(bodies[1].id);
  expect(bodies[1].description).toBe("只按照原来的均线条件");
});


test("fee card starts compact, cancels unsaved edits and shows saved values after a successful save", async () => {
  let fees = {source:"custom",default:{mode:"per_lot",open:10,closeToday:20,closeYesterday:30},overrides:[]};
  request.mockImplementation(async (_url: string, init?: RequestInit) => { if (init?.method === "POST") fees = JSON.parse(String(init.body)); return fees; });
  render(<FeeEditor rootUrl="http://runtime" symbol="SHFE.au2612"/>);
  const summary = await screen.findByRole("button", {name:/手续费.*开仓 10.*平今 20.*平昨 30/});
  expect(screen.queryByRole("spinbutton")).toBeNull();
  fireEvent.click(summary); fireEvent.change(screen.getByLabelText("开仓"), {target:{value:"99"}});
  fireEvent.click(screen.getByRole("button", {name:"取消"}));
  expect(summary).toHaveTextContent("开仓10");
  fireEvent.click(summary); fireEvent.change(screen.getByLabelText("开仓"), {target:{value:"12"}});
  fireEvent.click(screen.getByRole("button", {name:"保存费率"}));
  await waitFor(() => expect(screen.queryByRole("spinbutton")).toBeNull());
  expect(summary).toHaveTextContent("开仓12");
});

test("fee summary respects exact contract override before product or default rates", async () => {
  const rate = {mode:"notional",open:1,closeToday:2,closeYesterday:3};
  request.mockResolvedValue({source:"custom",default:{...rate,open:0},overrides:[{...rate,symbol:"SHFE.au"},{...rate,symbol:"SHFE.au2612",open:4.5}]});
  render(<FeeEditor rootUrl="http://runtime" symbol="shfe.au2612"/>);
  expect(await screen.findByRole("button", {name:/手续费.*万分之.*开仓 4.5/})).toHaveTextContent("SHFE.au2612 专用费率");
});

test("外盘合约的手续费写每手每边美元（取下单估算），国内费率与专用费率对它不适用", async () => {
  const rate = {mode:"per_lot",open:7,closeToday:8,closeYesterday:9};
  // CME.GCEZ26 strips to “cme.gcez” — a lookalike override must never be shown as the foreign contract's rate.
  request.mockResolvedValue({source:"custom",default:{...rate,open:1},overrides:[{...rate,symbol:"CME.GCEZ26"},{...rate,symbol:"cme.gcez"}]});
  estimate.mockResolvedValue({symbol:"CME.GCEZ26",commissionPerLot:2.5});
  render(<FeeEditor rootUrl="http://runtime" symbol="CME.GCEZ26"/>);
  expect(await screen.findByTestId("fee-foreign")).toHaveTextContent("外盘 · 每手每边 2.5 美元");
  expect(estimate).toHaveBeenCalledWith("http://runtime", {symbol:"CME.GCEZ26",side:"buy",offset:"open",volume:1}, expect.anything());
  expect(await screen.findByRole("button", {name:/手续费.*开仓 1/})).not.toHaveTextContent("专用费率");
});

test.each(["click", "command"])("saved strategy deletion requires menu selection and confirmation: %s", async via => {
  const item = {strategyId:"delete-me",name:"可删除策略",symbol:"*",version:1,status:"draft",definition:{plan:{...plan,questions:[],clarifications:[]},instrumentScope:{mode:"any",symbols:[]}}};
  let deleted = false;
  request.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/strategy-plans/delete-me") && init?.method === "DELETE") { deleted = true; return {status:"deleted"}; }
    return {items:deleted ? [] : [item],executions:[]};
  });
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.au2612" replayActive={false} workspaceScope="user"/>);
  const more = await screen.findByRole("button", {name:"可删除策略 v1 的更多操作"});
  if (via === "click") {
    fireEvent.click(more);
    fireEvent.click(screen.getByRole("menuitem", {name:"删除策略"}));
  } else {
    // The row's disclosures register in a passive effect after the row is painted; findByRole can see the
    // row first (the list arrives outside act), so wait for the registration an agent's command needs.
    await waitFor(() => expect(uiSurface("disclosure:strategy-actions:library:delete-me")).toBeDefined());
    act(() => uiSurface("disclosure:strategy-actions:library:delete-me")!.apply!(true));
    expect(screen.getByRole("menuitem", {name:"删除策略"})).toBeVisible();
    expect(disclosureStates()["strategy-actions:library:delete-me"]).toBe(true);
    act(() => uiSurface("disclosure:strategy-delete-confirm:library:delete-me")!.apply!(true));
    expect(disclosureStates()["strategy-delete-confirm:library:delete-me"]).toBe(true);
    act(() => uiSurface("disclosure:strategy-delete-confirm:library:delete-me")!.apply!(false));
    expect(deleted).toBe(false);
    expect(screen.queryByRole("button", {name:"确认删除"})).toBeNull();
    act(() => uiSurface("disclosure:strategy-delete-confirm:library:delete-me")!.apply!(true));
  }
  expect(deleted).toBe(false);
  fireEvent.click(screen.getByRole("button", {name:"确认删除"}));
  await waitFor(() => expect(screen.queryByRole("button", {name:"可删除策略 v1 的更多操作"})).toBeNull());
});

test("a failed refinement preserves prior rules and another draft cannot inherit its failed job", async () => {
  const old = {...plan,clarifications:[]};
  request.mockImplementation(async (_url:string, init?:RequestInit) => {
    if (init?.method === "POST") return {id:JSON.parse(String(init.body)).id,status:"failed",stage:"本次超时"};
    return {status:"failed",stage:"本次超时"};
  });
  const props={rootUrl:"http://runtime",workspaceScope:"isolation",symbol:"SHFE.au2612",onSaved:vi.fn()};
  const view=render(<StrategyDesigner {...props} draftKey="one" initialPlan={old}/>);
  fireEvent.click(screen.getByRole("button", {name:"生成可选建议"}));
  await screen.findByText(/下面保留上次草稿/);
  expect(screen.getByLabelText("策略名称")).toHaveValue(old.name);
  view.unmount();
  render(<StrategyDesigner {...props} draftKey="two" initialPlan={{...old,name:"另一条策略"}}/>);
  expect(screen.getByLabelText("策略名称")).toHaveValue("另一条策略");
  expect(screen.queryByText("本次超时")).toBeNull();
});

test("a strategy with open questions is 要补条件 in 我的策略 and its 结果页 offers 完善策略 instead of 回测 / 上线", async () => {
  localStorage.setItem("ai-trader-strategy-panel:old-navigation", JSON.stringify({value:{tab:"design"}}));
  request.mockResolvedValue({items:[{strategyId:"kept",name:plan.name,symbol:"*",version:1,status:"draft",definition:{plan,instrumentScope:{mode:"any",symbols:[]}}}],executions:[]});
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.au2612" replayActive={false} workspaceScope="old-navigation"/>);
  fireEvent.click(await screen.findByRole("button", {name:"我的策略 · 要补条件"}));
  expect(screen.getByRole("button", {name:"返回我的策略"})).toBeVisible();
  expect(screen.queryByRole("button", {name:/上线运行/})).toBeNull();
  expect(screen.queryByRole("button", {name:"回测"})).toBeNull();
  // 完善策略 opens the 策略编辑卡 on it, the questions in the card.
  fireEvent.click(screen.getByRole("button", {name:"完善策略"}));
  const card = await screen.findByTestId("strategy-editor");
  expect(within(card).getByText("你想在哪个周期看交叉？")).toBeVisible();
  expect(uiSurface("strategyEditor")!.state).toMatchObject({open: true, mode: "edit", strategyId: "kept", questions: [{id: "period", options: ["5 分钟", "日线"]}]});
});

test("two tabs, 我的策略 and 正在运行 (no 策略库 / 详情 / 实验室); strategy: {tab: fees} still hands over to settings", async () => {
  localStorage.setItem("ai-trader-strategy-panel:fees-moved", JSON.stringify({value:{tab:"fees"}}));
  request.mockResolvedValue({items:[],executions:[]});
  const opened = vi.fn();
  window.addEventListener("ai-trader-open-settings", opened);
  const handler = {current: undefined as undefined | ((changes: Record<string, unknown>) => void)};
  render(<UiNavigationContext.Provider value={{publish: vi.fn(), handler: handler as any}}>
    <StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.au2612" replayActive={false} workspaceScope="fees-moved"/>
  </UiNavigationContext.Provider>);
  expect(await screen.findByRole("button", {name:"我的策略"})).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", {name:"正在运行"})).toBeVisible();
  for (const name of ["策略库", "详情", "运行中", "实验室", "手续费设置", "设计"]) expect(screen.queryByRole("button", {name})).toBeNull();
  act(() => handler.current?.({tab: "fees"}));
  expect((opened.mock.calls[0][0] as CustomEvent).detail).toBe("fees");
  window.removeEventListener("ai-trader-open-settings", opened);
});

test("我的策略: 新建策略 on top, one card per strategy with its state, contract, sentence and latest backtest; the old strategy-new disclosure opens the 编辑卡", async () => {
  const ready = {strategyId:"lib-ready",name:"棉纱日内反转",symbol:"*",version:2,status:"draft",planSummary:"跌多了就买回来。",
    latestBacktest:{backtestId:"b1",netProfit:12040,maxDrawdown:3100,winRate:55.2,trades:128,symbol:"SHFE.rb2701"},
    definition:{schemaVersion:2,plan:{...plan,name:"棉纱日内反转",questions:[],clarifications:[]},instrumentScope:{mode:"any",symbols:[]}}};
  const running = {...ready,strategyId:"lib-run",name:"双均线",latestBacktest:null,planSummary:null};
  const paused = {...ready,strategyId:"lib-paused",name:"沪金突破"};
  const draft = {...ready,strategyId:"lib-draft",name:"待完善",definition:{...ready.definition,plan}};
  const run = {...running,strategyId:"run-1",symbol:"SHFE.rb2701",status:"monitoring",definition:{...running.definition,libraryStrategyId:"lib-run"}};
  const pausedRun = {...paused,strategyId:"run-2",symbol:"SHFE.au2612",status:"paused",definition:{...paused.definition,libraryStrategyId:"lib-paused"}};
  request.mockImplementation(async (url: string) => {
    if (url.endsWith("/prefill")) return {eligible:false, reason:"成交不够", fields:{}, trades:[]};
    if (url.endsWith("/api/strategy-plans")) return {items:[ready,running,paused,draft],executions:[run,pausedRun]};
    return {conditions:[]};
  });
  const instruments = {items:[{symbol:"SHFE.rb2701",instrumentName:"螺纹2701",instrumentId:"rb2701",productId:"rb"}]} as any;
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.au2612" replayActive={false} workspaceScope="library-states" instruments={instruments}/>);
  expect(await screen.findByRole("button", {name:"双均线 · 运行中"})).toBeVisible();
  expect(screen.getByRole("button", {name:"沪金突破 · 已暂停"})).toBeVisible();
  const idle = screen.getByRole("button", {name:"棉纱日内反转 · 没上线"}).closest("article")!;
  expect(idle).toHaveTextContent("跌多了就买回来。");
  expect(idle).toHaveTextContent("最近回测 +1.2 万最多亏 -3,100胜率 55%");
  expect(idle).toHaveTextContent("螺纹2701");
  expect(screen.getByRole("button", {name:"待完善 · 要补条件"})).toBeVisible();
  expect(screen.getByRole("button", {name:"双均线 · 运行中"}).closest("article")).toHaveTextContent("还没回测");
  expect(screen.getByRole("button", {name:"正在运行 · 2"})).toBeVisible();
  // 新建策略 replaced the ask box and the template chips; the old chooser's disclosure opens the card.
  expect(screen.getByRole("button", {name:"新建策略"})).toBeVisible();
  for (const name of ["突破追涨", "均线趋势", "区间高抛低吸", "让 AI 做"]) expect(screen.queryByRole("button", {name})).toBeNull();
  expect(screen.queryByRole("textbox", {name:"说说你想怎么做"})).toBeNull();
  act(() => uiSurface("disclosure:strategy-new")!.apply!(true));
  expect(await screen.findByTestId("strategy-editor")).toBeVisible();
  expect(disclosureStates()["strategy-new"]).toBe(true);
  // ‹ 我的策略 without a change leaves no draft (a look is not a saved draft).
  fireEvent.click(screen.getByRole("button", {name:"返回我的策略"}));
  expect(screen.queryByTestId("strategy-editor")).toBeNull();
  expect(screen.queryByRole("button", {name:/继续编辑/})).toBeNull();
  expect(disclosureStates()["strategy-new"]).toBe(false);
  // After a change it hides the card and keeps the draft; 继续编辑 brings it back.
  act(() => uiSurface("disclosure:strategy-new")!.apply!(true));
  fireEvent.change(await screen.findByRole("textbox", {name:"策略名称"}), {target:{value:"午盘突破"}});
  fireEvent.click(screen.getByRole("button", {name:"返回我的策略"}));
  expect(screen.queryByTestId("strategy-editor")).toBeNull();
  fireEvent.click(screen.getByRole("button", {name:"继续编辑：午盘突破"}));
  expect(screen.getByTestId("strategy-editor")).toBeVisible();
  act(() => uiSurface("disclosure:strategy-new")!.apply!(false));
  expect(screen.queryByTestId("strategy-editor")).toBeNull();
  expect(screen.queryByRole("button", {name:/继续编辑/})).toBeNull();
});

test("结果页: 还没回测 offers 回测; the plain rows, 跟 AI 说怎么改 / 编辑 / 存着先不上线 / 上线运行; the full rules and the original idea folded under 更多", async () => {
  const detailed: StrategyPlan = {...plan, questions:[], clarifications:[], maxOrdersPerDay:20, rules:{...plan.rules,
    entryLong:{join:"all",sequence:false,conditions:[{label:"MA5上穿MA20",left:{indicator:"ma",period:5,timeframe:0,value:0,barsAgo:0},operator:"cross_up",right:{indicator:"ma",period:20,timeframe:0,value:0,barsAgo:0},withinBars:1,consecutive:1,minCount:1}]},
    exitLong:{join:"any",sequence:false,conditions:[{label:"MA5下穿MA20",left:{indicator:"ma",period:5,timeframe:0,value:0,barsAgo:0},operator:"cross_down",right:{indicator:"ma",period:20,timeframe:0,value:0,barsAgo:0},withinBars:1,consecutive:1,minCount:1}]}}};
  const rows = [{key:"entry",label:"开仓",parts:[{text:"5 根均线上穿 20 根均线就开多。"}]},{key:"exit",label:"平仓",parts:[{text:"下穿就平。"}]},
    {key:"stop",label:"止损",parts:[{text:"止损约 2 倍"},{text:"平均波动",term:"ATR14",help:"ATR（平均真实波幅）：最近 14 根K线平均每根波动多大。"},{text:"。"}]},
    {key:"risk",label:"风险",parts:[{text:"每次 1 手。"}]}];
  const saved = {strategyId:"library",name:"1分钟双均线策略",symbol:"*",version:3,status:"draft",planRows:rows,definition:{schemaVersion:2,plan:detailed,instrumentScope:{mode:"any",symbols:[]}}};
  request.mockImplementation(async (url: string) => url.endsWith("/api/strategy-plans") ? {items:[saved],executions:[]}
    : url.endsWith("/backtest") ? {backtestId:"b1",kind:"rule",strategyName:"1分钟双均线策略",strategyId:"library",symbol:"SHFE.rb2701",timeframeSeconds:300,
      summary:{netProfit:12040,returnPercent:12,maxDrawdown:3100,maxDrawdownPercent:3,winRate:55.4,payoffRatio:1.2,trades:128,averageBarsHeld:3,fees:10},
      exitReasons:[],equityCurve:[{time:1,equity:1},{time:2,equity:3}],trades:[],coverage:{firstBarTime:0,lastBarTime:0,bars:10,tradingDays:120,firstTradingDay:"2026-04-08",lastTradingDay:"2026-10-07",requestedTradingDays:120,limited:false},assumptions:[],
      verdict:{facts:[{id:"concentration",text:"能赚钱，但赚的钱有 52% 来自 9 月那一段；去掉那个月只赚 5,900。",tone:"warning",numbers:["52","9","5,900"]},
        {id:"tuned",text:"提醒：参数是在同一段行情里挑出来的，实盘可能没这么好。",tone:"warning",numbers:[]}],text:null,source:null}}
    : url.endsWith("/verdict") ? {facts:[{id:"tuned",text:"提醒：参数是在同一段行情里挑出来的，实盘可能没这么好。",tone:"warning",numbers:[]}],text:"能赚钱，但一半靠 9 月那一段。",source:"model"}
    : {conditions:[]});
  const onAskAi = vi.fn();
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.rb2701" replayActive={false} workspaceScope="detail-blocks" onAskAi={onAskAi} backtestBlocked={null} detailFooter={<p>研究数据</p>}/>);
  fireEvent.click(await screen.findByRole("button", {name:"1分钟双均线策略 · 没上线"}));
  expect(screen.getByRole("heading", {name:"还没回测"})).toBeVisible();
  const rules = screen.getByLabelText("规则");
  expect(within(rules).getAllByText(/^(开仓|平仓|止损|风险)$/).map(node => node.textContent)).toEqual(["开仓", "平仓", "止损", "风险"]);
  expect(rules).toHaveTextContent("止损约 2 倍平均波动?。");
  fireEvent.click(within(rules).getByRole("button", {name:"平均波动是什么"}));
  expect(screen.getByRole("group", {name:"平均波动的说明"})).toHaveTextContent("ATR（平均真实波幅）");
  for (const name of ["跟 AI 说怎么改", "编辑", "存着先不上线", "上线运行"]) expect(screen.getByRole("button", {name})).toBeEnabled();
  // 回测 on the page: the last 6 months, then the three figures, the curve and AI 怎么看.
  fireEvent.click(screen.getByRole("button", {name:"回测"}));
  await screen.findByText("过去 6 个月按这个做");
  expect(request).toHaveBeenCalledWith("http://runtime/api/strategies/library/backtest", expect.objectContaining({method:"POST"}));
  const big = screen.getByLabelText("回测结果");
  expect(big).toHaveTextContent("一共赚了+12,040收益 12%最多一次亏到-3,100从最高点回落100 次里赢55 次共 128 笔");
  const verdict = screen.getByRole("region", {name:"AI 怎么看"});
  // The model's sentence once it arrives; the tuned warning stays in yellow.
  await waitFor(() => expect(verdict).toHaveTextContent("能赚钱，但一半靠 9 月那一段。"));
  expect(within(verdict).getByText("提醒：参数是在同一段行情里挑出来的，实盘可能没这么好。")).toHaveClass("warn");
  // 跟 AI 说怎么改: the user's own words, with the strategy as the turn's focus.
  fireEvent.click(screen.getByRole("button", {name:"跟 AI 说怎么改"}));
  expect(disclosureStates()["strategy-ask-ai"]).toBe(true);
  fireEvent.change(screen.getByRole("textbox", {name:"跟 AI 说怎么改"}), {target:{value:"把止损放宽一点再测一次"}});
  fireEvent.click(screen.getByRole("button", {name:"发给 AI"}));
  expect(onAskAi).toHaveBeenCalledWith("把止损放宽一点再测一次", {kind:"strategy_edit", id:"library", label:"1分钟双均线策略"});
  // 更多: the full rules as titled blocks, the original idea, the research footer.
  act(() => uiSurface("strategyDetails")!.apply!({section:"rules", open:true}));
  const entry = screen.getByRole("region", {name:"什么时候开仓"});
  expect(within(entry).getByTitle("MA5上穿MA20")).toHaveTextContent("跟图MA5 上穿 MA20");
  expect(screen.getByText("原始想法（你当时说的话）").closest("details")).not.toHaveAttribute("open");
  expect(screen.getByText("研究数据")).toBeVisible();
  // 编辑 opens the 策略编辑卡 on this strategy; 放弃 comes back to the 结果页.
  fireEvent.click(screen.getByRole("button", {name:"编辑"}));
  const card = await screen.findByTestId("strategy-editor");
  expect(within(card).getByRole("textbox", {name:"策略名称"})).toHaveValue(plan.name);
  expect(uiSurface("strategyEditor")!.state).toMatchObject({mode: "edit", strategyId: "library", fields: {entryLong: {conditions: ["MA5上穿MA20"]}}});
  fireEvent.click(within(card).getByRole("button", {name:"放弃"}));
  // 存着先不上线 goes back to 我的策略.
  fireEvent.click(screen.getByRole("button", {name:"存着先不上线"}));
  expect(await screen.findByRole("button", {name:"新建策略"})).toBeVisible();
});

test("changing all indicators to chart periods saves a new plan without altering risk or the prior version", async () => {
  const previous: StrategyPlan = {...plan, questions:[], clarifications:[], rules:{...plan.rules,entryLong:{join:"all",sequence:false,conditions:[{label:"5日均线上穿10日均线",left:{indicator:"ma",period:5,timeframe:86400,value:0,barsAgo:0},operator:"cross_up",right:{indicator:"ma",period:10,timeframe:86400,value:0,barsAgo:0},withinBars:2,consecutive:1,minCount:1}]}}};
  request.mockResolvedValue({strategyId:"new-version"});
  render(<StrategyDesigner rootUrl="http://runtime" workspaceScope="chart-conversion" symbol="SHFE.au2612" initialPlan={previous} onSaved={vi.fn()}/>);
  fireEvent.click(screen.getByRole("button", {name:"所有指标改为跟随图表"}));
  fireEvent.click(screen.getByRole("button", {name:"保存到我的策略"}));
  await waitFor(() => expect(request).toHaveBeenCalled());
  const saved = JSON.parse(String(request.mock.calls[0][1].body)).plan;
  expect(saved.timeframeSeconds).toBe(0);
  expect(saved.rules.entryLong.conditions![0]).toEqual({...previous.rules.entryLong!.conditions![0],label:"MA5上穿MA10",left:{...previous.rules.entryLong!.conditions![0].left,timeframe:0},right:{...previous.rules.entryLong!.conditions![0].right,timeframe:0}});
  for (const key of ["maxPosition", "maxLoss", "maxOrdersPerDay", "cooldownBars", "stopLossTicks", "takeProfitTicks"] as const) expect(saved[key]).toBe(previous[key]);
  expect(previous.rules.entryLong!.conditions![0].left.timeframe).toBe(86400);
});

test("the designer's 高级 fold edits 加仓 and 分批止盈 into the saved plan, and refuses the 互斥 edit (第六批)", async () => {
  const editable: StrategyPlan = {...plan, questions:[], clarifications:[], maxPosition:4};
  request.mockResolvedValue({strategyId:"pyramid-saved"});
  const view = render(<StrategyDesigner rootUrl="http://runtime" workspaceScope="pyramid-edit" symbol="SHFE.au2612" initialPlan={editable} onSaved={vi.fn()}/>);
  act(() => uiSurface("disclosure:strategy-advanced")!.apply!(true));
  const fold = screen.getByText("高级").closest("details")!;   // 已用 0 项: the summary carries no count yet
  // 加仓: the toggle creates the block, then every field edits the same plan.
  fireEvent.click(within(fold).getByLabelText("浮盈时顺势加仓"));
  fireEvent.change(within(fold).getByLabelText("触发方式"), {target:{value:"profit_ticks"}});
  fireEvent.change(within(fold).getByLabelText("步长 · 跳"), {target:{value:"5"}});
  fireEvent.change(within(fold).getByLabelText("每次加仓手数"), {target:{value:"1"}});
  expect(within(fold).getByText(/首笔开 1 手给加仓留空间/)).toBeVisible();
  // 分批止盈: 加一档 starts at 1×ATR 平 50%; editing the tier keeps one unit per tier.
  fireEvent.click(within(fold).getByRole("button", {name:"加一档"}));
  fireEvent.change(within(fold).getByLabelText("第 1 档 · 目标值"), {target:{value:"2"}});
  fireEvent.change(within(fold).getByLabelText("第 1 档 · 平掉 %"), {target:{value:"50"}});
  // The editable blocks replace their read-only rows: the fold shows no 加仓 / 分批止盈 kv row.
  expect(within(fold).queryByText("加仓", {selector:"dt"})).toBeNull();
  expect(within(fold).queryByText("分批止盈", {selector:"dt"})).toBeNull();
  fireEvent.click(screen.getByRole("button", {name:"保存到我的策略"}));
  await waitFor(() => expect(request).toHaveBeenCalled());
  const saved = JSON.parse(String(request.mock.calls[0][1].body)).plan;
  expect(saved.pyramiding).toEqual({maxAdds:3, trigger:"profit_ticks", step:5, atrPeriod:20, volume:1, moveStop:true});
  expect(saved.scaleOut).toEqual([{profitTicks:null, profitAtr:2, percent:50}]);
  expect(saved.maxPosition).toBe(4);

  // 互斥: a plan that already has 保本 refuses the add in the form, with the server's own line, and never posts.
  view.unmount();
  request.mockClear();
  const withBreakeven = {...editable, breakeven:{triggerTicks:20, triggerAtr:null, offsetTicks:0}};
  render(<StrategyDesigner rootUrl="http://runtime" workspaceScope="pyramid-clash" symbol="SHFE.au2612" initialPlan={withBreakeven} onSaved={vi.fn()}/>);
  act(() => uiSurface("disclosure:strategy-advanced")!.apply!(true));
  fireEvent.click(screen.getByLabelText("浮盈时顺势加仓"));
  expect(screen.getAllByRole("alert").map(node => node.textContent)).toEqual(["加仓暂不和保本/追踪止损/分段止盈一起用"]);
  fireEvent.click(screen.getByRole("button", {name:"保存到我的策略"}));
  expect(request).not.toHaveBeenCalled();
});

test.each([false, true])("a delayed paused read cannot replace the confirmed start; lost response: %s", async (lostResponse) => {
  const {getStrategyMonitor} = await import("../api");
  const saved = {strategyId:"library",name:"运行策略",symbol:"*",version:1,status:"ready",definition:{schemaVersion:2,instrumentScope:{mode:"any",symbols:[]},plan:{...plan,questions:[],clarifications:[]}}};
  const execution = {...saved,strategyId:"execution",symbol:"SHFE.rb2610",status:"paused",definition:{...saved.definition,libraryStrategyId:"library"}};
  let finishOld!: (value: any) => void;
  let finishValidation!: (value: any) => void;
  let started = false;
  vi.mocked(getStrategyMonitor).mockImplementationOnce(() => new Promise(resolve => {finishOld = resolve;})).mockImplementation(async () => ({strategyId:"execution",status:started ? "monitoring" : "paused",recentAttempts:[],simulationOnly:true,liveTrading:"disabled"}));
  request.mockImplementation(async (url:string, _init?: RequestInit) => {
    if (url.endsWith("/ai-execution")) return {controls:{stopStatus:null}};
    if (url.endsWith("/validate")) return new Promise(resolve => {finishValidation = resolve;});
    if (url.endsWith("/monitor/start")) {started = true; if (lostResponse) throw new TypeError("lost response"); return {};}
    if (url.endsWith("/conditions")) return {conditions:[]};
    return {items:[saved],executions:[{...execution,status:started ? "monitoring" : "paused"}]};
  });
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="start-race" symbol="SHFE.rb2610" replayActive={false}/>);
  fireEvent.click(await screen.findByRole("button",{name:/^运行策略 · /}));
  fireEvent.click(await screen.findByRole("button",{name:"上线运行"}));
  const submit = within(screen.getByRole("dialog", {name:"上线运行"})).getByRole("button", {name:"验证并上线"});
  fireEvent.click(submit); fireEvent.click(submit);
  expect(within(screen.getByRole("dialog", {name:"上线运行"})).getByRole("button", {name:/验证规则与所需周期行情/})).toBeDisabled();
  await waitFor(() => expect(finishValidation).toBeDefined());
  await act(async () => finishValidation({strategyId:"execution"}));
  // Started: the panel moves to 运行中, where the instance reads its own monitor state.
  const card = await screen.findByTestId("strategy-run-card");
  await act(async () => finishOld({strategyId:"execution",status:"paused",recentAttempts:[]}));
  expect(await within(card).findByRole("button",{name:"暂停"})).toBeEnabled();
  expect(screen.getByRole("button", {name:"正在运行 · 1"})).toHaveAttribute("aria-pressed", "true");
  expect(request.mock.calls.filter(([url]) => url.endsWith("/monitor/start"))).toHaveLength(1);
});

test("emergency stop is explained before any strategy validation or start", async () => {
  const saved = {strategyId:"library",name:"运行策略",symbol:"*",version:1,status:"ready",definition:{instrumentScope:{mode:"any",symbols:[]},plan:{...plan,questions:[],clarifications:[]}}};
  request.mockImplementation(async (url:string) => url.endsWith("/ai-execution") ? {controls:{stopStatus:"EMERGENCY_STOP"}} : {items:[saved],executions:[]});
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="stopped" symbol="SHFE.rb2610" replayActive={false}/>);
  fireEvent.click(await screen.findByRole("button",{name:/^运行策略 · /}));
  fireEvent.click(screen.getByRole("button",{name:"上线运行"}));
  fireEvent.click(screen.getByRole("button",{name:"验证并上线"}));
  expect(await screen.findByText(/AI 模拟执行已停止或急停/)).toBeVisible();
  expect(request.mock.calls.some(([,init]) => init?.method === "POST")).toBe(false);
});

test("AI 审计 2026-10-13：用实例 id 选中正在运行的实例，发布的选中就是这个实例（回执对得上）", async () => {
  const {getStrategyMonitor} = await import("../api");
  const saved = {strategyId:"library",name:"双均线",symbol:"*",version:1,status:"ready",definition:{instrumentScope:{mode:"any",symbols:[]},plan:{...plan,questions:[],clarifications:[]}}};
  const execution = {...saved,strategyId:"execution",status:"monitoring",symbol:"SHFE.rb2610",definition:{...saved.definition,libraryStrategyId:"library"}};
  vi.mocked(getStrategyMonitor).mockResolvedValue({strategyId:"execution",status:"monitoring",simulationOnly:true,liveTrading:"disabled",recentAttempts:[]} as any);
  request.mockImplementation(async () => ({items:[saved],executions:[execution]}));
  const publish = vi.fn();
  const handler = {current: undefined as undefined | ((changes: Record<string, unknown>) => void)};
  render(<UiNavigationContext.Provider value={{publish, handler} as any}>
    <StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.rb2610" replayActive={false} workspaceScope="select-instance"/>
  </UiNavigationContext.Provider>);
  await waitFor(() => expect((publish.mock.calls.at(-1)?.[0] as {items?: unknown[]} | null)?.items).toHaveLength(1));
  act(() => handler.current!({selectedStrategyId: "execution"}));
  await waitFor(() => expect(publish.mock.calls.at(-1)![0]).toMatchObject({selectedStrategyId: "execution"}));
  act(() => handler.current!({selectedStrategyId: "library"}));
  await waitFor(() => expect(publish.mock.calls.at(-1)![0]).toMatchObject({selectedStrategyId: "library"}));
});

test("#843 选中按正在运行里显示的实例解析：旧式运行中策略选到它的运行卡，已停止的实例不能选", async () => {
  const {getStrategyMonitor} = await import("../api");
  const base = {name:"双均线",symbol:"*",version:1,status:"ready",definition:{instrumentScope:{mode:"any",symbols:[]},plan:{...plan,questions:[],clarifications:[]}}};
  const library = {...base,strategyId:"library"};
  // A legacy running strategy: kept in the library list, no instrumentScope, monitoring.
  const legacy = {...base,strategyId:"legacy",name:"旧式",status:"monitoring",symbol:"SHFE.rb2610",definition:{plan:{...plan,questions:[],clarifications:[]}}};
  const stopped = {...base,strategyId:"stopped",status:"stopped",symbol:"SHFE.rb2610",definition:{...base.definition,libraryStrategyId:"library"}};
  vi.mocked(getStrategyMonitor).mockResolvedValue({strategyId:"legacy",status:"monitoring",simulationOnly:true,liveTrading:"disabled",recentAttempts:[]} as any);
  request.mockImplementation(async () => ({items:[library, legacy],executions:[stopped]}));
  const publish = vi.fn();
  const handler = {current: undefined as undefined | ((changes: Record<string, unknown>) => void)};
  render(<UiNavigationContext.Provider value={{publish, handler} as any}>
    <StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.rb2610" replayActive={false} workspaceScope="select-843"/>
  </UiNavigationContext.Provider>);
  await waitFor(() => expect((publish.mock.calls.at(-1)?.[0] as {items?: unknown[]} | null)?.items).toHaveLength(2));
  act(() => handler.current!({selectedStrategyId: "legacy"}));
  await waitFor(() => expect(screen.getByRole("button", {name:"正在运行 · 1"})).toHaveAttribute("aria-pressed", "true"));
  expect(publish.mock.calls.at(-1)![0]).toMatchObject({selectedStrategyId: "legacy"});
  expect(() => handler.current!({selectedStrategyId: "stopped"})).toThrow("这个运行实例已停止");
  expect(publish.mock.calls.at(-1)![0]).toMatchObject({selectedStrategyId: "legacy"});
});

test("策略列表还没读回来时说正在读取，不先说还没有策略（#522 A-08）", async () => {
  let finish: (value: unknown) => void = () => undefined;
  request.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.rb2610" replayActive={false} workspaceScope="loading-522"/>);
  expect(await screen.findByText("正在读取策略…")).toBeVisible();
  expect(screen.queryByText(/还没有策略/)).toBeNull();
  await act(async () => finish({items:[],executions:[]}));
  expect(await screen.findByRole("heading", {name:"还没有策略，选一种开始"})).toBeVisible();
});

test("halt-opens still allows starting strategy monitoring", async () => {
  const {getStrategyMonitor} = await import("../api");
  const saved = {strategyId:"library",name:"运行策略",symbol:"*",version:1,status:"ready",definition:{instrumentScope:{mode:"any",symbols:[]},plan:{...plan,questions:[],clarifications:[]}}};
  vi.mocked(getStrategyMonitor).mockResolvedValue({strategyId:"execution",status:"monitoring",simulationOnly:true,liveTrading:"disabled",recentAttempts:[]} as any);
  request.mockImplementation(async (url:string) => url.endsWith("/ai-execution") ? {controls:{stopStatus:"HALT_OPENS"}} : url.endsWith("/validate") ? {strategyId:"execution"} : {items:[saved],executions:[]});
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="halt-opens" symbol="SHFE.rb2610" replayActive={false}/>);
  fireEvent.click(await screen.findByRole("button",{name:/^运行策略 · /}));
  fireEvent.click(screen.getByRole("button",{name:"上线运行"}));
  fireEvent.click(screen.getByRole("button",{name:"验证并上线"}));
  await waitFor(() => expect(request.mock.calls.some(([url,init]) => String(url).endsWith("/monitor/start") && init?.method === "POST")).toBe(true));
  expect(screen.queryByText(/AI 模拟执行已停止或急停/)).not.toBeInTheDocument();
});

test("a plain stop still blocks strategy monitoring start", async () => {
  const saved = {strategyId:"library",name:"运行策略",symbol:"*",version:1,status:"ready",definition:{instrumentScope:{mode:"any",symbols:[]},plan:{...plan,questions:[],clarifications:[]}}};
  request.mockImplementation(async (url:string) => url.endsWith("/ai-execution") ? {controls:{stopStatus:"STOPPED"}} : {items:[saved],executions:[]});
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="full-stop" symbol="SHFE.rb2610" replayActive={false}/>);
  fireEvent.click(await screen.findByRole("button",{name:/^运行策略 · /}));
  fireEvent.click(screen.getByRole("button",{name:"上线运行"}));
  fireEvent.click(screen.getByRole("button",{name:"验证并上线"}));
  expect(await screen.findByText(/AI 模拟执行已停止或急停/)).toBeVisible();
  expect(request.mock.calls.some(([,init]) => init?.method === "POST")).toBe(false);
});

test("正在运行 card: 执行方式, signal, today's figures, position, latest signals, 问 AI 它为什么这么做 (or the reason), 运行日志 expands in place and failed records can be hidden and brought back", async () => {
  const {getStrategyMonitor} = await import("../api");
  const saved = {strategyId:"library",name:"双均线",symbol:"*",version:1,status:"ready",definition:{instrumentScope:{mode:"any",symbols:[]},plan:{...plan,questions:[],clarifications:[],maxOrdersPerDay:20,
    rules:{...plan.rules,entryLong:{join:"all",sequence:false,conditions:[{label:"MA5上穿MA20",left:{indicator:"ma",period:5,timeframe:0,value:0},operator:"cross_up",right:{indicator:"ma",period:20,timeframe:0,value:0},withinBars:1,consecutive:1,minCount:1}]}}}}};
  const execution = {...saved,strategyId:"execution",status:"monitoring",symbol:"SHFE.rb2610",signalAction:"auto",definition:{...saved.definition,libraryStrategyId:"library"}};
  vi.mocked(getStrategyMonitor).mockResolvedValue({strategyId:"execution",status:"monitoring",simulationOnly:true,liveTrading:"disabled",
    signalAction:"auto", effectiveSignalAction:"confirm",
    decision:{status:"waiting",checkedAt:new Date().toISOString()},
    monitorState:{strategyId:"execution",userId:"u",tradingDate:"2026-09-09",lastTodayPnl:"140",consecutiveLosses:0,updatedAt:"2026-09-09T05:48:42Z"},
    tradingDay:"2026-09-09", today:{tradingDate:"2026-09-09",pnl:"140",consecutiveLosses:0},
    opensToday:13, position:{direction:"long", volume:1}, signalsToday:2,
    researchClash:{stance:"偏空", direction:"long", text:"今日判断偏空，这个策略今天在做多"}, recentAttempts:[{attemptId:"failed",strategyId:"execution",intentId:"i",signalBarTime:"1",tradingDay:"2026-09-09",status:"FAILED",action:"open_long",volume:3,createdAt:"2026-09-09T05:48:42Z",updatedAt:"2026-09-09T05:48:42Z",detail:"ai_execution_stopped"}]} as any);
  request.mockImplementation(async (url:string) => url.endsWith("/conditions") ? {conditions:[{label:"MA5上穿MA20",status:"passed",detail:"",group:"entryLong"}]} : {items:[saved],executions:[execution]});
  const onAskAi = vi.fn();
  const view = render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="clear-failed" symbol="SHFE.rb2610" replayActive={false}
    tab="running" onTabChange={vi.fn()} autonomy={false} onAskAi={onAskAi} explainBlocked={null}/>);
  const card = await screen.findByTestId("strategy-run-card");
  // auto while AI 自主执行 is off runs as 出卡确认, and the card says so.
  expect(await within(card).findByText(/自动下单（AI 自主执行已关闭，按出卡确认）/)).toBeVisible();
  expect(await within(card).findByText(/当前信号：开多条件满足/)).toBeVisible();
  expect(card).toHaveTextContent("今天 +140");
  expect(card).toHaveTextContent("持仓 多 1 手");
  expect(card).toHaveTextContent(/开多 3 手 · 执行失败/);
  // 今日判断联动: the yellow line when the strategy goes against the product's judgment.
  expect(within(card).getByTestId("strategy-clash")).toHaveTextContent("今日判断偏空，这个策略今天在做多");
  expect(uiSurface("strategyInstance:execution")!.state).toMatchObject({position:{direction:"long", volume:1}, signalsToday:2,
    researchClash:"今日判断偏空，这个策略今天在做多"});
  fireEvent.click(within(card).getByRole("button",{name:"问 AI 它为什么这么做"}));
  expect(onAskAi).toHaveBeenCalledWith("这个策略今天为什么这么做", {kind:"strategy_instance", id:"execution", label:"双均线 · SHFE.rb2610"});
  // 运行日志 in the panel (also a disclosure with the instance id).
  fireEvent.click(within(card).getByRole("button",{name:"运行日志"}));
  expect(disclosureStates()["strategy-run-log:execution"]).toBe(true);
  const log = within(card).getByRole("region",{name:"运行日志"});
  expect(within(log).getByText(/执行失败/)).toBeVisible();
  fireEvent.click(within(log).getByRole("button",{name:"清空失败记录"}));
  expect(within(log).queryByText(/执行失败/)).toBeNull();
  fireEvent.click(within(log).getByRole("button",{name:"查看历史记录"}));
  expect(within(log).getByText(/执行失败/)).toBeVisible();
  expect(request.mock.calls.some(([,init]) => init?.method === "POST")).toBe(false);
  // Skill unavailable: the button says why instead of asking AI.
  view.rerender(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="clear-failed" symbol="SHFE.rb2610" replayActive={false}
    tab="running" onTabChange={vi.fn()} autonomy onAskAi={onAskAi} explainBlocked="「策略信号解读」仅主账号可用"/>);
  expect(within(card).getByRole("button",{name:"问 AI 它为什么这么做"})).toHaveAttribute("title", "「策略信号解读」仅主账号可用");
});

const launchable = {strategyId:"library",name:"双均线",symbol:"*",version:1,status:"draft",definition:{schemaVersion:2,instrumentScope:{mode:"any",symbols:[]},plan:{...plan,questions:[],clarifications:[]}}};

test("上线运行 opens a dialog: chart contract and 出卡确认 by default, 自动下单 greyed with its reason, a bound contract locked, a failed validation shown as is", async () => {
  const bound = {...launchable,strategyId:"bound",name:"螺纹专用",definition:{...launchable.definition,instrumentScope:{mode:"selected",symbols:["SHFE.rb2701"]}}};
  request.mockImplementation(async (url:string) => {
    if (url.endsWith("/ai-execution")) return {controls:{stopStatus:null}};
    if (url.endsWith("/validate")) { const error = Object.assign(new Error("422"), {status:422, detail:"策略尚不可运行：所需周期的已收盘 K 线不足"}); throw error; }
    return {items:[launchable,bound],executions:[]};
  });
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="launch-defaults" symbol="SHFE.au2612" replayActive={false} autonomy={false}/>);
  fireEvent.click(await screen.findByRole("button",{name:/^双均线 · /}));
  fireEvent.click(screen.getByRole("button",{name:"上线运行"}));
  const dialog = screen.getByRole("dialog",{name:"上线运行"});
  expect(within(dialog).getByLabelText("合约")).toHaveValue("SHFE.au2612");
  expect(within(dialog).getByRole("radio",{name:/出卡确认/})).toBeChecked();
  expect(within(dialog).getByRole("radio",{name:/自动下单/})).toBeDisabled();
  expect(dialog).toHaveTextContent("AI 自主执行已关闭（设置 → 交易），打开后才能选自动下单");
  // The dialog is commandable and observable.
  expect(uiSurface("strategyLaunch")!.state).toMatchObject({open:true, strategyId:"library", symbol:"SHFE.au2612", symbolLocked:false, signalAction:"confirm"});
  expect(() => uiSurface("strategyLaunch")!.apply!({signalAction:"auto"})).toThrow(/AI 自主执行已关闭/);
  act(() => uiSurface("strategyLaunch")!.apply!({signalAction:"alert"}));
  expect(within(dialog).getByRole("radio",{name:/只提醒/})).toBeChecked();
  fireEvent.click(within(dialog).getByRole("button",{name:"验证并上线"}));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("策略尚不可运行：所需周期的已收盘 K 线不足");
  expect(screen.getByRole("dialog",{name:"上线运行"})).toBeVisible();
  // A strategy bound to one contract is locked to it.
  fireEvent.click(within(dialog).getByRole("button",{name:"取消"}));
  fireEvent.click(screen.getByRole("button",{name:/^我的策略/}));
  fireEvent.click(screen.getByRole("button",{name:/^螺纹专用 · /}));
  fireEvent.click(screen.getByRole("button",{name:"上线运行"}));
  const locked = screen.getByRole("dialog",{name:"上线运行"});
  expect(within(locked).getByLabelText("合约")).toHaveValue("SHFE.rb2701");
  expect(within(locked).getByLabelText("合约")).toBeDisabled();
  expect(locked).toHaveTextContent("这条策略绑定了这个合约");
});

test("价差策略的上线对话框：合约换成只读的两条腿，三种执行方式都能选，验证按 A 腿合约", async () => {
  const spreadItem = {...launchable, strategyId:"spreadlib", name:"跨期价差",
    definition:{...launchable.definition, plan:{...plan, questions:[], clarifications:[],
      spread:{kind:"difference", legs:[{symbol:"SHFE.rb2701", lots:1, weight:1},{symbol:"SHFE.rb2705", lots:1, weight:1}]}}}};
  request.mockImplementation(async (url:string) => url.endsWith("/ai-execution") ? {controls:{stopStatus:null}} : url.endsWith("/validate") ? {strategyId:"execution"} : url.endsWith("/monitor/start") ? {} : {items:[spreadItem],executions:[]});
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="spread-launch" symbol="SHFE.au2612" replayActive={false} autonomy sessionId="chat-1"/>);
  fireEvent.click(await screen.findByRole("button",{name:/^跨期价差 · /}));
  fireEvent.click(screen.getByRole("button",{name:"上线运行"}));
  const dialog = screen.getByRole("dialog",{name:"上线运行"});
  // 价差 stands in for the contract field: read-only, the legs from the definition.
  expect(within(dialog).queryByLabelText("合约")).toBeNull();
  expect(within(dialog).getByLabelText("价差的两条腿")).toHaveValue("SHFE.rb2701 − SHFE.rb2705");
  expect(within(dialog).getByLabelText("价差的两条腿")).toHaveAttribute("readonly");
  expect(dialog).toHaveTextContent("监控按 A 腿 SHFE.rb2701 的行情与时段运行");
  expect(uiSurface("strategyLaunch")!.state).toMatchObject({open:true, strategyId:"spreadlib", symbol:"SHFE.rb2701", symbolLocked:true, signalAction:"confirm"});
  expect(() => uiSurface("strategyLaunch")!.apply!({symbol:"SHFE.au2612"})).toThrow(/价差的两条腿写在策略定义里/);
  // All three 执行方式 are offered (第二期: a confirmed card or an auto signal both dispatch the two legs).
  for (const label of [/只提醒/, /出卡确认/, /自动下单/]) expect(within(dialog).getByRole("radio",{name:label})).toBeEnabled();
  fireEvent.click(within(dialog).getByRole("radio",{name:/自动下单/}));
  fireEvent.click(within(dialog).getByRole("button",{name:"验证并上线"}));
  await waitFor(() => expect(request.mock.calls.some(([url]) => String(url).endsWith("/monitor/start"))).toBe(true));
  const validate = request.mock.calls.find(([url]) => String(url).endsWith("/validate"))!;
  expect(JSON.parse(String(validate[1].body))).toMatchObject({symbol:"SHFE.rb2701"});
  const start = request.mock.calls.find(([url]) => String(url).endsWith("/monitor/start"))!;
  expect(JSON.parse(String(start[1].body))).toEqual({signalAction:"auto", sessionId:"chat-1"});
});

test("验证并上线 validates on the chosen contract and starts with the 执行方式 and this conversation", async () => {
  const {getStrategyMonitor} = await import("../api");
  vi.mocked(getStrategyMonitor).mockResolvedValue({strategyId:"execution",status:"monitoring",simulationOnly:true,liveTrading:"disabled",recentAttempts:[]} as any);
  request.mockImplementation(async (url:string) => url.endsWith("/ai-execution") ? {controls:{stopStatus:null}} : url.endsWith("/validate") ? {strategyId:"execution"} : url.endsWith("/monitor/start") ? {} : {items:[launchable],executions:[]});
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="launch-submit" symbol="SHFE.au2612" replayActive={false} autonomy sessionId="chat-1"/>);
  fireEvent.click(await screen.findByRole("button",{name:/^双均线 · /}));
  fireEvent.click(screen.getByRole("button",{name:"上线运行"}));
  const dialog = screen.getByRole("dialog",{name:"上线运行"});
  fireEvent.change(within(dialog).getByLabelText("合约"), {target:{value:"SHFE.rb2701"}});
  fireEvent.click(within(dialog).getByRole("radio",{name:/自动下单/}));
  fireEvent.click(within(dialog).getByRole("button",{name:"验证并上线"}));
  await waitFor(() => expect(request.mock.calls.some(([url]) => String(url).endsWith("/monitor/start"))).toBe(true));
  const validate = request.mock.calls.find(([url]) => String(url).endsWith("/validate"))!;
  expect(JSON.parse(String(validate[1].body))).toMatchObject({symbol:"SHFE.rb2701"});
  const start = request.mock.calls.find(([url]) => String(url).endsWith("/monitor/start"))!;
  expect(JSON.parse(String(start[1].body))).toEqual({signalAction:"auto", sessionId:"chat-1"});
  await waitFor(() => expect(screen.queryByRole("dialog",{name:"上线运行"})).toBeNull());
});

test("正在运行 card: 执行方式 switches through start, 停止并平仓 asks first (只停止 keeps positions, the close path makes cards), a paused instance on another contract can 继续, an older day's figures show —", async () => {
  const {getStrategyMonitor} = await import("../api");
  const run = {...launchable,strategyId:"execution",status:"paused",symbol:"SHFE.rb2610",signalAction:"confirm",definition:{...launchable.definition,libraryStrategyId:"library",chartTimeframeSeconds:300}};
  vi.mocked(getStrategyMonitor).mockResolvedValue({strategyId:"execution",status:"paused",simulationOnly:true,liveTrading:"disabled",signalAction:"confirm",effectiveSignalAction:"confirm",
    monitorState:{strategyId:"execution",userId:"u",tradingDate:"2026-09-24",lastTodayPnl:null,consecutiveLosses:0,updatedAt:"2026-09-24T05:48:42Z"},
    tradingDay:"2026-09-25", today:null, opensToday:null, recentAttempts:[]} as any);
  request.mockImplementation(async (url:string) => {
    if (url.endsWith("/ai-execution")) return {controls:{stopStatus:null}};
    // The library validates into another execution (its scope changed since): 继续 must never go there (#520).
    if (url.endsWith("/validate")) return {strategyId:url.includes("/strategy-plans/library/") ? "execution-new" : "execution"};
    if (url.endsWith("/monitor/stop")) return {strategyId:"execution",status:"ready",positionsKept:true,notice:"已停止：不再判断信号，执行记录保留；已有持仓不会平掉，需要平仓请在持仓里处理。",recentAttempts:[]};
    if (url.endsWith("/monitor/start")) return {strategyId:"execution",status:"monitoring",recentAttempts:[]};
    if (url.endsWith("/conditions")) return {conditions:[]};
    return {items:[launchable],executions:[run]};
  });
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="card-actions" symbol="SHFE.au2612" replayActive={false} tab="running" onTabChange={vi.fn()} autonomy={false}/>);
  const card = await screen.findByTestId("strategy-run-card");
  // (a) the state is from 2026-09-24, today is 2026-09-25: no figures pass as today's.
  await waitFor(() => expect(card).toHaveTextContent("今天 —"));
  // 执行方式: the menu offers three, 自动下单 greyed; choosing one is the same start call.
  fireEvent.click(within(card).getByRole("button",{name:/^出卡确认/}));
  expect(within(card).getByRole("menuitemradio",{name:/自动下单/})).toBeDisabled();
  fireEvent.click(within(card).getByRole("menuitemradio",{name:/只提醒/}));
  await waitFor(() => expect(request.mock.calls.some(([url, init]) => String(url).endsWith("/execution/monitor/start") && init?.body === JSON.stringify({signalAction:"alert"}))).toBe(true));
  expect(uiSurface("strategyInstance:execution")!.state).toMatchObject({strategyId:"execution", symbol:"SHFE.rb2610", signalAction:"confirm"});
  // (b) 继续 is not tied to the chart's contract: it validates on the instance's own contract and period.
  const resume = within(card).getByRole("button",{name:"继续"});
  expect(resume).toBeEnabled();
  fireEvent.click(resume);
  await waitFor(() => expect(request.mock.calls.some(([url]) => String(url).endsWith("/validate"))).toBe(true));
  const validate = request.mock.calls.find(([url]) => String(url).endsWith("/validate"))!;
  expect(JSON.parse(String(validate[1].body))).toEqual({symbol:"SHFE.rb2610", chartTimeframeSeconds:300});
  // …on the paused instance itself, whose start carries no 执行方式: it keeps its own (#520).
  expect(String(validate[0])).toBe("http://runtime/api/strategy-plans/execution/validate");
  const startedAfterValidate = () => request.mock.calls.slice(request.mock.calls.indexOf(validate))
    .filter(([url]) => String(url).endsWith("/monitor/start")).map(([url, init]) => [String(url), init?.body]);
  await waitFor(() => expect(startedAfterValidate()).toEqual([["http://runtime/api/strategies/execution/monitor/start", "{}"]]));
  await waitFor(() => expect(within(card).getByRole("button",{name:"停止并平仓"})).toBeEnabled());
  // 停止并平仓 asks first; nothing is stopped or sent before a choice.
  const { stopStrategyMonitoring } = await import("../api");
  vi.mocked(stopStrategyMonitoring).mockResolvedValue({strategyId:"execution",status:"ready",positionsKept:true,notice:"已停止：不再判断信号，执行记录保留；已有持仓不会平掉，需要平仓请在持仓里处理。",recentAttempts:[]} as any);
  fireEvent.click(within(card).getByRole("button",{name:"停止并平仓"}));
  expect(disclosureStates()["strategy-stop-confirm:execution"]).toBe(true);
  expect(card).toHaveTextContent("停止后不再判断信号。它还有持仓的话，会在对话里出平仓卡，你确认了才平。");
  expect(stopStrategyMonitoring).not.toHaveBeenCalled();
  fireEvent.click(within(card).getByRole("button",{name:"只停止，不平仓"}));
  await waitFor(() => expect(stopStrategyMonitoring).toHaveBeenCalledWith("http://runtime", "execution", false));
  expect(await screen.findByText(/已有持仓不会平掉/)).toBeVisible();
});

test("回撤停机与资金额度（运行中卡）：平仓处理中不当成已停、不能继续也不能恢复；已停止只有 canResume 才能“恢复运行”且只发 resetDrawdown；没开就不显示；额度原样显示 Runtime 的数或原因", async () => {
  // Protects: a triggered strategy is never restarted by the plain 继续 (which would keep the old peak) or shown as flat
  // while its own position is still being closed; the budget lines are the Runtime's figures, not the card's arithmetic.
  const {getStrategyMonitor} = await import("../api");
  const run = {...launchable,strategyId:"execution",status:"paused",symbol:"SHFE.rb2610",signalAction:"confirm",definition:{...launchable.definition,libraryStrategyId:"library"}};
  const funds = {equity:"1000000",equityPercent:10,usedMargin:"30000",pendingMargin:"10000",remainingBudget:"60000",marginPerLot:"20000",availableLots:3,unavailableReason:null};
  const drawdown = {initialEquity:"1000000",peakEquity:"1050000",currentEquity:"996000",drawdownPercent:5.14,limitPercent:5,
    curve:[{time:1,equity:"1000000"},{time:2,equity:"1050000"},{time:3,equity:"996000"}]};
  request.mockImplementation(async (url:string) => {
    if (url.endsWith("/ai-execution")) return {controls:{stopStatus:null}};
    if (url.endsWith("/monitor/start")) return {strategyId:"execution",status:"monitoring",recentAttempts:[]};
    if (url.endsWith("/conditions")) return {conditions:[]};
    return {items:[launchable],executions:[run]};
  });
  const show = async (extra: Record<string, unknown>) => {
    vi.mocked(getStrategyMonitor).mockResolvedValue({strategyId:"execution",status:"paused",simulationOnly:true,liveTrading:"disabled",signalAction:"confirm",
      effectiveSignalAction:"confirm",recentAttempts:[],...extra} as any);
    const view = render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="drawdown" symbol="SHFE.au2612" replayActive={false} tab="running" onTabChange={vi.fn()} autonomy/>);
    const card = await screen.findByTestId("strategy-run-card");
    await waitFor(() => expect(uiSurface("strategyInstance:execution")!.state).toMatchObject({status:"paused"}));
    return { card, view };
  };
  // 平仓处理中: the reason stays visible, neither 继续 nor 恢复运行 is offered.
  let { card, view } = await show({funds, drawdown:{...drawdown,status:"liquidating",reason:"已撤挂单 1 笔 · 剩余持仓 2 手待平",canResume:false}});
  const block = await within(card).findByTestId("strategy-drawdown");
  expect(block).toHaveTextContent("已停新开仓 · 平仓处理中");
  expect(block).toHaveTextContent("剩余持仓 2 手待平");
  expect(block).toHaveTextContent("起始权益100.00 万最高105.00 万当前99.60 万回撤（线 5%）5.14%");
  expect(within(card).queryByRole("button",{name:"继续"})).toBeNull();
  expect(within(card).queryByRole("button",{name:"恢复运行"})).toBeNull();
  expect(card).toHaveTextContent("剩余额度 = 权益 100.00 万 × 10% − 本策略已占 4.00 万（含挂单 1.00 万） = 6.00 万");
  expect(card).toHaveTextContent("剩余额度 6.00 万 ÷ 每手保证金 2.00 万 → 本次最多 3 手");
  expect(uiSurface("strategyInstance:execution")!.state).toMatchObject({funds:{availableLots:3},drawdown:{status:"liquidating",canResume:false}});
  view.unmount();
  // 已停止 but the Runtime does not allow it yet: 恢复运行 is there, disabled; nothing is sent.
  ({ card, view } = await show({funds:{unavailableReason:"行情已过期，额度暂时算不出"}, drawdown:{...drawdown,status:"stopped",reason:"回撤 5.14% 触到 5% 停机线",resumeBlockedReason:"本策略还有 1 笔委托结果未确认",canResume:false}}));
  await waitFor(() => expect(within(card).getByRole("button",{name:"恢复运行"})).toBeDisabled());
  expect(card).toHaveTextContent("已停止 · 手动恢复");
  // The disabled button explains with the Runtime's blocking reason, not the trigger note.
  expect(within(card).getByTestId("strategy-drawdown")).toHaveTextContent("本策略还有 1 笔委托结果未确认");
  expect(within(card).getByTestId("strategy-drawdown")).not.toHaveTextContent("回撤 5.14% 触到 5% 停机线");
  expect(uiSurface("strategyInstance:execution")!.state).toMatchObject({drawdown:{canResume:false,resumeBlockedReason:"本策略还有 1 笔委托结果未确认"}});
  expect(card).toHaveTextContent("行情已过期，额度暂时算不出");
  expect(card).not.toHaveTextContent("剩余额度");
  view.unmount();
  // canResume: 恢复运行 is the instance's own start with resetDrawdown only.
  ({ card, view } = await show({drawdown:{...drawdown,status:"stopped",reason:"已撤挂单 1 笔 · 已平 2 手",canResume:true}}));
  await waitFor(() => expect(within(card).getByRole("button",{name:"恢复运行"})).toBeEnabled());
  fireEvent.click(within(card).getByRole("button",{name:"恢复运行"}));
  await waitFor(() => expect(request.mock.calls.some(([url]) => String(url).endsWith("/monitor/start"))).toBe(true));
  expect(request.mock.calls.filter(([url]) => String(url).endsWith("/monitor/start")).map(([url, init]) => [url, init?.body]))
    .toEqual([["http://runtime/api/strategies/execution/monitor/start", JSON.stringify({resetDrawdown:true})]]);
  expect(request.mock.calls.some(([url]) => String(url).endsWith("/validate"))).toBe(false);
  view.unmount();
  // The Runtime refuses (still settling): its Chinese reason shows and the card keeps the stopped state it reported.
  request.mockImplementation(async (url:string) => {
    if (url.endsWith("/ai-execution")) return {controls:{stopStatus:null}};
    if (url.endsWith("/monitor/start")) throw Object.assign(new Error("409"), {status:409, detail:{detail:{code:"strategy_drawdown_liquidating", message:"本策略还有 1 手待平，处理完才能恢复运行"}}});
    if (url.endsWith("/conditions")) return {conditions:[]};
    return {items:[launchable],executions:[run]};
  });
  ({ card, view } = await show({drawdown:{...drawdown,status:"stopped",reason:null,canResume:true,feeSource:"estimated"}}));
  expect(await within(card).findByTestId("drawdown-fee-basis")).toHaveTextContent("手续费按费率估算");
  fireEvent.click(await within(card).findByRole("button",{name:"恢复运行"}));
  expect(await screen.findByText("本策略还有 1 手待平，处理完才能恢复运行")).toBeInTheDocument();
  expect(within(card).getByTestId("strategy-drawdown")).toHaveTextContent("已停止 · 手动恢复");
  view.unmount();
  // 回撤暂时算不出: the same 恢复运行 (a new round), still only when the Runtime says canResume; its blocking reason first.
  ({ card, view } = await show({drawdown:{...drawdown,status:"unavailable",reason:"手续费暂缺，回撤暂时算不出",resumeBlockedReason:"本策略还有 1 手持仓未平",canResume:false}}));
  await waitFor(() => expect(within(card).getByRole("button",{name:"恢复运行"})).toBeDisabled());
  expect(within(card).getByTestId("strategy-drawdown")).toHaveTextContent("回撤暂时算不出本策略还有 1 手持仓未平");
  expect(within(card).getByTestId("strategy-drawdown")).not.toHaveTextContent("手续费暂缺，回撤暂时算不出");
  view.unmount();
  // canResume: it sends resetDrawdown only; refused, the card keeps the unavailable state with the Runtime's reason.
  const sent = request.mock.calls.length;
  ({ card, view } = await show({drawdown:{...drawdown,status:"unavailable",reason:"历史成交归属无法恢复",canResume:true}}));
  fireEvent.click(await within(card).findByRole("button",{name:"恢复运行"}));
  expect(await screen.findByText("本策略还有 1 手待平，处理完才能恢复运行")).toBeInTheDocument();
  expect(request.mock.calls.slice(sent).filter(([url]) => String(url).endsWith("/monitor/start")).map(([url, init]) => [url, init?.body]))
    .toEqual([["http://runtime/api/strategies/execution/monitor/start", JSON.stringify({resetDrawdown:true})]]);
  expect(within(card).getByTestId("strategy-drawdown")).toHaveTextContent("回撤暂时算不出历史成交归属无法恢复");
  view.unmount();
  // A payload missing one part (挂单 not known): no 已占 total made from a 0; the second line still reads.
  ({ card, view } = await show({funds:{...funds, pendingMargin:null}}));
  await waitFor(() => expect(card).toHaveTextContent("剩余额度 6.00 万 ÷ 每手保证金 2.00 万 → 本次最多 3 手"));
  expect(card).not.toHaveTextContent("本策略已占");
  view.unmount();
  // Not used (absent or inactive): no block, the plain 继续 as before.
  ({ card } = await show({drawdown:{status:"inactive"}, funds:{...funds, remainingBudget:"15000", availableLots:0}}));
  await waitFor(() => expect(within(card).getByRole("button",{name:"继续"})).toBeEnabled());
  expect(within(card).queryByTestId("strategy-drawdown")).toBeNull();
  expect(card).toHaveTextContent("剩余额度 1.50 万 ÷ 每手保证金 2.00 万 → 不足 1 手，这次不开");
});

test("停止并平仓：确认后停止并请 Runtime 出平仓卡，卡在对话里等确认，界面从不下单", async () => {
  const {getStrategyMonitor, stopStrategyMonitoring} = await import("../api");
  const run = {...launchable,strategyId:"execution",status:"monitoring",symbol:"SHFE.rb2610",signalAction:"auto",definition:{...launchable.definition,libraryStrategyId:"library"}};
  vi.mocked(getStrategyMonitor).mockResolvedValue({strategyId:"execution",status:"monitoring",simulationOnly:true,liveTrading:"disabled",signalAction:"auto",
    position:{direction:"short", volume:2}, recentAttempts:[]} as any);
  vi.mocked(stopStrategyMonitoring).mockResolvedValue({strategyId:"execution",status:"ready",positionsKept:false,recentAttempts:[],
    closeCards:[{proposalId:"card-1",symbol:"SHFE.rb2610",side:"buy_close",volume:2}],notice:"已停止：不再判断信号。已出平仓卡，在对话里确认后才平仓。"} as any);
  request.mockImplementation(async (url:string) => url.endsWith("/conditions") ? {conditions:[]} : {items:[launchable],executions:[run]});
  render(<StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" workspaceScope="stop-close" symbol="SHFE.au2612" replayActive={false} tab="running" onTabChange={vi.fn()} autonomy/>);
  const card = await screen.findByTestId("strategy-run-card");
  await waitFor(() => expect(card).toHaveTextContent("持仓 空 2 手"));
  fireEvent.click(within(card).getByRole("button",{name:"停止并平仓"}));
  expect(card).toHaveTextContent("它还有 空 2 手，会在对话里出平仓卡，你确认了才平。");
  fireEvent.click(within(card).getByRole("button",{name:"确认停止并平仓"}));
  await waitFor(() => expect(stopStrategyMonitoring).toHaveBeenCalledWith("http://runtime", "execution", true));
  expect(await screen.findByText(/已出平仓卡，在对话里确认后才平仓/)).toBeVisible();
  // Nothing on this page sends an order: no confirm, trade or order route was called.
  expect(request.mock.calls.filter(([url]) => /confirm|trade|order/.test(String(url)))).toEqual([]);
});


test("#507 编辑卡：保存还没回来就放弃并开了新卡，迟到的结果只刷新列表，不关新卡、不改选中；当前卡自己的失败才显示", async () => {
  const ready: StrategyPlan = {...plan, questions:[], clarifications:[], rules:{...plan.rules, entryLong:{join:"all",sequence:false,conditions:[{label:"MA5上穿MA20",
    left:{indicator:"ma",period:5,timeframe:0,value:0,barsAgo:0},operator:"cross_up",right:{indicator:"ma",period:20,timeframe:0,value:0,barsAgo:0},withinBars:1,consecutive:1,minCount:1}]}}};
  const library = (id: string, name: string, version = 1) => ({strategyId:id,name,symbol:"*",version,status:"draft",definition:{schemaVersion:2,plan:{...ready,name},instrumentScope:{mode:"any",symbols:[]}}});
  let items = [library("a","策略甲")];
  const saves: {resolve: (value: unknown) => void; reject: (error: Error) => void}[] = [];
  request.mockImplementation((url: string, init?: RequestInit) => url.endsWith("/api/strategy-plans") && init?.method === "POST"
    ? new Promise((resolve, reject) => { saves.push({resolve, reject}); })
    : url.endsWith("/api/strategy-backtests") ? new Promise(() => undefined)
    : Promise.resolve(url.endsWith("/api/strategy-plans") ? {items, executions:[]} : url.endsWith("/prefill") ? {eligible:false, reason:"成交不够", fields:{}, trades:[]} : {conditions:[]}));
  const publish = vi.fn();
  render(<UiNavigationContext.Provider value={{publish, handler: {current: undefined} as any}}>
    <StrategyWorkbench rootUrl="http://runtime" baseUrl="http://runtime" symbol="SHFE.au2612" replayActive={false} workspaceScope="late-save-owner"/>
  </UiNavigationContext.Provider>);
  const state = () => publish.mock.calls.at(-1)![0] as {selectedStrategyId: string; designerOpen: boolean};
  fireEvent.click(await screen.findByRole("button", {name:/^策略甲 · /}));
  fireEvent.click(screen.getByRole("button", {name:"编辑"}));
  fireEvent.click(within(await screen.findByTestId("strategy-editor")).getByRole("button", {name:"存为草稿"}));
  await waitFor(() => expect(saves).toHaveLength(1));
  // Thrown away mid-save, a new card opened: the late answer neither closes it nor selects the version.
  fireEvent.click(screen.getByRole("button", {name:"放弃"}));
  fireEvent.click(screen.getByRole("button", {name:"返回我的策略"}));
  fireEvent.click(screen.getByRole("button", {name:"新建策略"}));
  await screen.findByText("成交不够");
  items = [library("a2","策略甲",2), ...items];
  await act(async () => saves[0].resolve({strategyId:"a2", name:"策略甲"}));
  await waitFor(() => expect((state() as unknown as {items: {strategyId: string}[]}).items.map(item => item.strategyId)).toContain("a2"));
  expect(screen.getByTestId("strategy-editor")).toBeVisible();
  expect(uiSurface("strategyEditor")!.state).toMatchObject({open: true, mode: "new", savedStrategyId: null});
  expect(state()).toMatchObject({selectedStrategyId: "a", designerOpen: true});
  // The current card's own failure is shown and keeps the draft.
  fireEvent.click(screen.getByRole("button", {name:"放弃"}));
  fireEvent.click((await screen.findAllByRole("button", {name:/^策略甲 · /}))[0]);
  fireEvent.click(screen.getByRole("button", {name:"编辑"}));
  fireEvent.click(within(await screen.findByTestId("strategy-editor")).getByRole("button", {name:"存为草稿"}));
  await waitFor(() => expect(saves).toHaveLength(2));
  await act(async () => saves[1].reject(new Error("bad")));
  expect(within(screen.getByTestId("strategy-editor")).getByRole("alert")).toHaveTextContent("无法连接应用服务");
  expect(screen.getByRole("textbox", {name:"策略名称"})).toHaveValue("策略甲");
});
