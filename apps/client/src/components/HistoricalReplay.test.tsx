import { useEffect } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { requestJson } from "../api";
import { disclosureStates, uiSurface } from "../uiSurfaces";
import { defaultFees, type SavedStrategy, type StrategyPlan } from "../strategyApi";
import { HistoricalReplay, type ReplayWorkspaceProps } from "./HistoricalReplay";

vi.mock("../api", () => ({ requestJson: vi.fn() }));

function ConnectedWorkspace({ onRuntimeReady, onOpenReplay, replayControls, onChartTimeframeChange }: ReplayWorkspaceProps) {
  useEffect(() => onRuntimeReady("http://runtime", "replay-test"), [onRuntimeReady]);
  return <><button onClick={() => onChartTimeframeChange?.(180)}>选择三分钟图表</button>{onOpenReplay ? <button onClick={onOpenReplay}>回放</button> : <span>云端工作台</span>}{replayControls}</>;
}

beforeEach(() => {
  localStorage.clear();
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

test("cloud workspace does not advertise or request local historical replay", async () => {
  render(<HistoricalReplay enabled={false}>{(props) => <ConnectedWorkspace {...props} />}</HistoricalReplay>);
  expect(screen.getByText("云端工作台")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "回放" })).toBeNull();
  expect(requestJson).not.toHaveBeenCalled();
});

test("local workspace restores its isolated replay session", async () => {
  vi.mocked(requestJson).mockResolvedValue({ items: [] });
  render(<HistoricalReplay enabled>{(props) => <ConnectedWorkspace {...props} />}</HistoricalReplay>);
  expect(screen.getByRole("button", { name: "回放" })).toBeTruthy();
  await waitFor(() => expect(requestJson).toHaveBeenCalledWith("http://runtime/api/replays"));
});

test("switching environment retires a delayed replay list without remounting the workspace", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(requestJson).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  let mounts = 0;
  function Workspace(props: ReplayWorkspaceProps) {
    useEffect(() => { mounts++; props.onRuntimeReady("http://old", "old"); }, []);
    return <><span>{props.replayPath || "live"}</span>{props.replayControls}</>;
  }
  const view = render(<HistoricalReplay environment="local" enabled>{props => <Workspace {...props}/>}</HistoricalReplay>);
  await waitFor(() => expect(requestJson).toHaveBeenCalledWith("http://old/api/replays"));
  view.rerender(<HistoricalReplay environment="cloud" enabled>{props => <Workspace {...props}/>}</HistoricalReplay>);
  await act(async () => finish({items:[{replayId:"old",runtimePath:"/old-replay",status:"ready"}]}));
  expect(screen.getByText("live")).toBeVisible();
  expect(screen.queryByText("/old-replay")).toBeNull();
  expect(mounts).toBe(1);
  expect(requestJson).toHaveBeenCalledTimes(1);
});

test("#755 C 回放状态读一次失败显示重试提示，下一次读到就消失；退出回放后不留横幅", async () => {
  const replay = {replayId:"r1",runtimePath:"/replays/r1",status:"ready",symbol:"SHFE.rb2701",tradingDay:"2026-10-09",playing:false,speed:1};
  let polls = 0;
  vi.mocked(requestJson).mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "http://runtime/api/replays") return {items:[replay]};
    if (url.endsWith("/control") && init?.method === "POST") return {...replay, status:"closed"};
    if (url === "http://runtime/api/replays/r1") { polls += 1; if (polls === 1) throw new Error("offline"); return replay; }
    throw new Error(`Unexpected request: ${url}`);
  });
  render(<HistoricalReplay enabled>{props => <ConnectedWorkspace {...props}/>}</HistoricalReplay>);
  expect(await screen.findByText(/回放连接暂时不可用，正在重试/)).toBeVisible();
  await waitFor(() => expect(screen.queryByText(/回放连接暂时不可用/)).toBeNull(), {timeout: 2000});
  // Another failure, then 退出回放: nothing stays behind.
  polls = 0;
  await waitFor(() => expect(screen.getByText(/回放连接暂时不可用/)).toBeVisible(), {timeout: 2000});
  fireEvent.click(screen.getByRole("button", {name:"退出回放"}));
  await waitFor(() => expect(screen.queryByRole("region", {name:"历史回放控制"})).toBeNull());
  expect(screen.queryByText(/回放连接/)).toBeNull();
});

test("#932 一步中途失败的回放（incomplete）显示提示，单步与播放可用来走完这一分钟", async () => {
  const notice = "这一分钟只推进了一部分，已暂停；点单步或播放会先走完这一分钟，不重发委托。";
  const replay = {replayId:"r2",runtimePath:"/replays/r2",status:"incomplete",symbol:"SHFE.rb2701",tradingDay:"2026-10-09",playing:false,speed:1,notice};
  const controls: unknown[] = [];
  vi.mocked(requestJson).mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === "http://runtime/api/replays") return {items:[replay]};
    if (url === "http://runtime/api/replays/r2") return replay;
    if (url.endsWith("/control") && init?.method === "POST") { controls.push(JSON.parse(String(init.body))); return {...replay, status:"ready", notice:null}; }
    throw new Error(`Unexpected request: ${url}`);
  });
  render(<HistoricalReplay enabled>{props => <ConnectedWorkspace {...props}/>}</HistoricalReplay>);
  expect(await screen.findByText(notice)).toBeVisible();
  const step = screen.getByRole("button", {name:"单步"});
  expect(step).not.toBeDisabled();
  expect(screen.getByRole("button", {name:"播放"})).not.toBeDisabled();
  // 平仓并停盘 waits until the minute is finished (the server refuses it too).
  expect(screen.getByRole("button", {name:"平仓并停盘"})).toBeDisabled();
  fireEvent.click(step);
  await waitFor(() => expect(controls).toContainEqual({action:"step"}));
});

const draft: StrategyPlan ={name:"回放策略",description:"按均线开仓",direction:"long",timeframeSeconds:60,maxPosition:1,maxLoss:1000,stopLossTicks:20,takeProfitTicks:40,cooldownBars:1,maxOrdersPerDay:10,rules:{entryLong:null,exitLong:null,entryShort:null,exitShort:null},questions:["止损 20 跳，请确认"],clarifications:[]};
const saved: SavedStrategy = {strategyId:"pending",name:draft.name,symbol:"*",version:1,status:"draft",definition:{schemaVersion:2,plan:draft,instrumentScope:{mode:"any",symbols:[]}}};

function mockSetup(extra?: (url: string, init?: RequestInit) => unknown) {
  vi.mocked(requestJson).mockImplementation(async (url, init) => {
    const value = extra?.(url, init);
    if (value !== undefined) return value;
    if (url.endsWith("/api/strategy-plans")) return {items:[saved]};
    if (url.endsWith("/api/trading-costs")) return defaultFees;
    if (url.endsWith("/api/instruments") || url.endsWith("/api/replays")) return {items:[]};
    throw new Error(`Unexpected request: ${url}`);
  });
}
async function openAiReplay() {
  render(<HistoricalReplay enabled>{props => <ConnectedWorkspace {...props}/>}</HistoricalReplay>);
  fireEvent.click(screen.getByRole("button", {name:"回放"}));
  fireEvent.click(screen.getByRole("button", {name:/AI 自动策略/}));
  await screen.findByRole("option", {name:/回放策略/});
  fireEvent.change(screen.getByLabelText("选择已保存策略"), {target:{value:"pending"}});
}

test("pending replay strategy has a usable completion path and only creates replay after confirmed rules are saved", async () => {
  const ready = {...saved, strategyId:"ready", version:2, definition:{...saved.definition,plan:{...draft,questions:[]}}};
  mockSetup((url, init) => {
    if (url.endsWith("/design-jobs") && init?.method === "POST") return {id:JSON.parse(String(init.body)).id,status:"completed",result:ready.definition.plan};
    if (url.endsWith("/api/strategy-plans") && init?.method === "POST") return ready;
    if (url.endsWith("/api/replays") && init?.method === "POST") return {replayId:"isolated",status:"loading",runtimePath:"/api/replays/isolated/runtime",symbol:"SHFE.au2612",tradingDay:"2026-09-04",playing:false,speed:1};
    if (url.endsWith("/api/replays/isolated")) return {replayId:"isolated",status:"loading",runtimePath:"/api/replays/isolated/runtime",symbol:"SHFE.au2612",tradingDay:"2026-09-04",playing:false,speed:1};
  });
  await openAiReplay();
  expect(screen.getByText(/还有待确认的买卖条件/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name:"完善策略后回放"}));
  expect(screen.getByLabelText("描述你的交易想法")).toHaveValue(draft.description);
  expect(vi.mocked(requestJson).mock.calls.some(([url, init]) => url.endsWith("/api/replays") && init?.method === "POST")).toBe(false);
  fireEvent.change(screen.getByLabelText("自己补充"), {target:{value:"确认止损20跳"}});
  fireEvent.click(screen.getByRole("button", {name:"回答并完善策略"}));
  await screen.findByRole("button", {name:"保存到我的策略"});
  fireEvent.click(screen.getByRole("button", {name:"保存到我的策略"}));
  await waitFor(() => expect(screen.getByLabelText("选择已保存策略")).toHaveValue("ready"));
  fireEvent.click(screen.getByRole("button", {name:"验证并开始策略回放"}));
  await waitFor(() => expect(vi.mocked(requestJson).mock.calls.filter(([url, init]) => url.endsWith("/api/replays") && init?.method === "POST")).toHaveLength(1));
});

test("replay picker keeps a blocked strategy visible and removes it only after successful deletion", async () => {
  let blocked = true;
  mockSetup((url, init) => {
    if (url.endsWith("/api/strategy-plans/pending") && init?.method === "DELETE") {
      if (blocked) throw Object.assign(new Error("blocked"), {detail:"SHFE.au2612 仍在监控，请先暂停后再删除"});
      return {status:"deleted"};
    }
  });
  await openAiReplay();
  fireEvent.click(screen.getByRole("button", {name:"回放策略 的更多操作"}));
  fireEvent.click(screen.getByRole("menuitem", {name:"删除策略"}));
  fireEvent.click(screen.getByRole("button", {name:"确认删除"}));
  await screen.findByText("SHFE.au2612 仍在监控，请先暂停后再删除");
  expect(screen.getByLabelText("选择已保存策略")).toHaveValue("pending");
  blocked = false;
  act(() => uiSurface("disclosure:strategy-actions:replay:pending")!.apply!(true));
  expect(screen.getByRole("menuitem", {name:"删除策略"})).toBeVisible();
  act(() => uiSurface("disclosure:strategy-delete-confirm:replay:pending")!.apply!(true));
  expect(disclosureStates()["strategy-delete-confirm:replay:pending"]).toBe(true);
  fireEvent.click(screen.getByRole("button", {name:"确认删除"}));
  await waitFor(() => expect(screen.queryByRole("option", {name:/回放策略/})).toBeNull());
  expect(screen.getByText("请选择一条策略，或先设计新策略。")).toBeVisible();
});


test("the replay dialog defaults to the calendar's previous trading day and never overwrites a hand-picked day", async () => {
  let resolveCalendar!: (value: unknown) => void;
  mockSetup(url => {
    if (url.endsWith("/api/market/calendar"))
      return new Promise(resolve => { resolveCalendar = resolve; });
  });
  render(<HistoricalReplay enabled>{props => <ConnectedWorkspace {...props}/>}</HistoricalReplay>);
  fireEvent.click(screen.getByRole("button", {name:"回放"}));
  const day = await screen.findByLabelText("交易日");
  const fallback = String(day.getAttribute("value"));  // before the calendar answers: the weekend guess
  await act(async () => resolveCalendar({tradingDay:"2026-10-05", previousTradingDay:"2026-09-30", recentTradingDays:["2026-09-30"]}));
  expect(day).toHaveValue("2026-09-30");
  expect(day).toHaveAttribute("max", "2026-09-30");  // the guard follows the calendar too
  fireEvent.change(day, {target:{value:"2026-09-18"}});
  fireEvent.click(screen.getByRole("button", {name:"关闭回放设置"}));
  fireEvent.click(screen.getByRole("button", {name:"回放"}));  // a reopen reads the calendar again
  await act(async () => resolveCalendar({tradingDay:"2026-10-05", previousTradingDay:"2026-09-30", recentTradingDays:[]}));
  expect(day).toHaveValue("2026-09-18");  // a day the user picked is never overwritten
  // The fallback itself is always a weekday (Mon–Fri) at or before yesterday, no calendar asked.
  const [year, month, date] = fallback.split("-").map(Number);
  expect([1, 2, 3, 4, 5]).toContain(new Date(Date.UTC(year, month - 1, date)).getUTCDay());
});

test("after 15:30 the replay dialog offers the trading day that just closed, as the calendar says (#754)", async () => {
  mockSetup(url => {
    if (url.endsWith("/api/market/calendar"))
      return Promise.resolve({tradingDay:"2026-10-13", previousTradingDay:"2026-10-12", latestClosedTradingDay:"2026-10-13",
                              recentTradingDays:["2026-10-12"]});
  });
  render(<HistoricalReplay enabled>{props => <ConnectedWorkspace {...props}/>}</HistoricalReplay>);
  fireEvent.click(screen.getByRole("button", {name:"回放"}));
  const day = await screen.findByLabelText("交易日");
  await waitFor(() => expect(day).toHaveValue("2026-10-13"));
  expect(day).toHaveAttribute("max", "2026-10-13");
});

test("a day the Runtime refuses shows its Chinese reason, not the generic failure", async () => {
  // 10-13 夜盘验收：默认交易日被拒时页面只说“无法确认回放创建完成”。422 是 Runtime 对这一天本身的拒绝
  // （休市、还没收盘），带说明和可选日期的中文，照原样显示，也不去认领别的回放。
  const reason = "10 月 13 日 的行情还没收盘（白盘 15:00 结束后才能回放），可以选更早的交易日";
  mockSetup((url, init) => {
    if (url.endsWith("/api/replays") && init?.method === "POST")
      throw Object.assign(new Error("422"), { status: 422, detail: { detail: reason } });
  });
  render(<HistoricalReplay enabled>{props => <ConnectedWorkspace {...props}/>}</HistoricalReplay>);
  fireEvent.click(screen.getByRole("button", {name:"回放"}));
  fireEvent.click(await screen.findByRole("button", {name:"开始手动回放"}));
  expect(await screen.findByText(reason)).toBeTruthy();
  const lists = vi.mocked(requestJson).mock.calls.filter(([url, init]) => url.endsWith("/api/replays") && !init?.method);
  expect(lists).toHaveLength(1);  // only the mount's restore read; no adopt-after-failure read
});

test("AI replay validates with the selected chart period before it starts playing", async () => {
  const ready = {...saved, definition:{...saved.definition,plan:{...draft,timeframeSeconds:0,questions:[]}}};
  const replay = {replayId:"chart",status:"ready",runtimePath:"/api/replays/chart/runtime",symbol:"SHFE.au2612",tradingDay:"2026-09-04",playing:false,speed:1};
  mockSetup((url, init) => {
    if (url.endsWith("/api/strategy-plans")) return {items:[ready]};
    if (url.endsWith("/api/replays") && init?.method === "POST") return replay;
    if (url.endsWith("/api/replays/chart") || url.endsWith("/control")) return replay;
    if (url.endsWith("/validate")) return {strategyId:"execution"};
    if (url.endsWith("/monitor/start")) return {status:"monitoring"};
  });
  await openAiReplay();
  fireEvent.click(screen.getByRole("button", {name:"选择三分钟图表"}));
  fireEvent.click(screen.getByRole("button", {name:"验证并开始策略回放"}));
  await waitFor(() => expect(vi.mocked(requestJson).mock.calls.some(([url]) => url.endsWith("/control"))).toBe(true));
  const calls = vi.mocked(requestJson).mock.calls;
  const validate = calls.findIndex(([url]) => url.endsWith("/validate"));
  expect(JSON.parse(String(calls[validate][1]?.body))).toEqual({symbol:"SHFE.au2612",chartTimeframeSeconds:180});
  expect(calls.findIndex(([url]) => url.endsWith("/monitor/start"))).toBeGreaterThan(validate);
  expect(calls.findIndex(([url]) => url.endsWith("/control"))).toBeGreaterThan(validate);
  // The displayed period is the sidecar's ready gate; the rest backfills after start.
  const create = calls.find(([url, init]) => url.endsWith("/api/replays") && init?.method === "POST");
  expect(JSON.parse(String(create?.[1]?.body))).toMatchObject({symbol:"SHFE.au2612",defaultDurationSeconds:180});
});
