import { act, fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { OperationTimeline } from "./OperationTimeline";
import type { ActivitySnapshot } from "../runtimeClient";
import { operationEvents } from "../operationEvents";

const call=vi.hoisted(()=>vi.fn());
vi.mock("../api",()=>({requestJson:call}));
const fill=(id:string)=>({tradeId:id,clientOrderId:"same-order",symbol:"SHFE.rb2610",direction:"BUY",offset:"OPEN",price:3100,volume:1,tradeDateTime:1788934800000000000,source:"ai" as const,strategyId:"s"});
const snapshot=(ids:string[]):ActivitySnapshot=>({environment:"simulation",replayId:null,symbol:"SHFE.rb2610",capturedAt:"2026-09-09T06:20:00Z",dataStatus:"normal",ageSeconds:0,orders:[],fills:ids.map(fill),strategies:[],protections:[]});
const props={baseUrl:"http://runtime",scope:"test",strategyId:"s",unavailable:false,busy:false,onAsk:vi.fn(),onLocate:vi.fn(),onCapability:vi.fn()};
beforeEach(()=>{vi.useFakeTimers();localStorage.clear();call.mockReset();call.mockImplementation(async(url:string)=>url.endsWith("capabilities")?{operations:[{operation:"POST /api/app/strategy-explanations"}]}:{id:"e",status:"completed",summary:"已经成交。",evidence:{},environment:"simulation"});});
afterEach(()=>vi.useRealTimers());

test("initial history is quiet, new partial fills merge, repeated snapshots do not repeat AI",async()=>{
  const view=render(<OperationTimeline {...props} activity={snapshot(["old"])}/>);
  await act(async()=>{await vi.advanceTimersByTimeAsync(4000);});
  expect(call.mock.calls.filter(c=>c[1]?.method==="POST")).toHaveLength(0);
  view.rerender(<OperationTimeline {...props} activity={snapshot(["old","a"])}/>);
  await act(async()=>{await vi.advanceTimersByTimeAsync(1000);});
  view.rerender(<OperationTimeline {...props} activity={snapshot(["old","a","b"])}/>);
  await act(async()=>{await vi.advanceTimersByTimeAsync(3500);});
  const posts=call.mock.calls.filter(c=>c[1]?.method==="POST");
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0][1].body).eventId).toBe("b");
  view.rerender(<OperationTimeline {...props} activity={snapshot(["old","a","b"])}/>);
  await act(async()=>{await vi.advanceTimersByTimeAsync(4000);});
  expect(call.mock.calls.filter(c=>c[1]?.method==="POST")).toHaveLength(1);
});

test("clear is display-only; changing execution status makes the event visible again",async()=>{
  const data=snapshot([]);data.strategies=[{strategyId:"s",name:"策略",status:"monitoring",reminderOnly:false,decision:null,monitorState:null,recentAttempts:[{attemptId:"a",action:"open_long",volume:1,status:"FAILED",detail:"ai_execution_stopped",updatedAt:"2026-09-09T06:20:00Z"}]}];
  const view=render(<OperationTimeline {...props} activity={data}/>);
  await act(async()=>{await Promise.resolve();});
  fireEvent.click(screen.getByRole("button",{name:"清空显示"}));
  expect(screen.getByText("暂无新操作")).toBeVisible();
  expect(data.strategies[0].recentAttempts).toHaveLength(1);
  const changed=structuredClone(data);changed.strategies[0].recentAttempts[0].status="REJECTED";
  view.rerender(<OperationTimeline {...props} activity={changed}/>);
  expect(screen.getByText(/开多 · 1 手 · 已拒绝/)).toBeVisible();
});

test("old server reports unavailable without blocking facts, asking and chart selection",async()=>{
  call.mockResolvedValue({operations:[]});
  render(<OperationTimeline {...props} activity={snapshot(["f"])} selectedTrades={["f"]}/>);
  await act(async()=>{await Promise.resolve();});
  expect(screen.getByText("自动解释未接入")).toBeVisible();
  fireEvent.click(screen.getByRole("button",{name:"定位图表"}));
  expect(props.onLocate).toHaveBeenCalledWith(expect.objectContaining({eventId:"f"}));
  fireEvent.click(screen.getByRole("button",{name:"问 AI"}));
  expect(props.onAsk).toHaveBeenCalledWith(expect.objectContaining({eventId:"f"}));
});

test("protection heartbeat alone is not a new event, and no fill is invented",()=>{
  const data=snapshot([]);data.protections=[{stopId:"p",strategyId:"s",symbol:"SHFE.rb2610",status:"ARMED",triggerPrice:3090,volume:1,updatedAt:"2026-09-09T06:20:00Z"}];
  const old=operationEvents(data);
  data.protections[0].updatedAt="2026-09-09T06:21:00Z";
  expect(operationEvents(data)[0].version).toBe(old[0].version);
  expect(operationEvents(data).some(e=>e.eventType==="fill")).toBe(false);
});

test("late explanation cannot enter a different account workspace",async()=>{
  let finish:((value:unknown)=>void)|undefined;
  call.mockImplementation((url:string)=>url.endsWith("capabilities")?Promise.resolve({operations:[{operation:"POST /api/app/strategy-explanations"}]}):new Promise(resolve=>{finish=resolve;}));
  const view=render(<OperationTimeline key="owner" {...props} activity={snapshot(["old-owner"])}/>);
  await act(async()=>{await Promise.resolve();});
  fireEvent.click(screen.getByRole("button",{name:"解释原因"}));
  view.rerender(<OperationTimeline key="other" {...props} scope="other" activity={snapshot(["new-owner"])}/>);
  await act(async()=>{finish?.({id:"old",status:"completed",summary:"旧账户的解释",evidence:{},environment:"simulation"});await Promise.resolve();});
  expect(screen.queryByText("旧账户的解释")).not.toBeInTheDocument();
});

test("order status readback updates a cleared fill without calling the model again",async()=>{
  const data=snapshot(["a"]);data.orders=[{id:"link",proposalId:"proposal",clientOrderId:"same-order",request:{symbol:"SHFE.rb2610",direction:"BUY",offset:"OPEN",volume:3,priceType:"LIMIT",limitPrice:3100},filledVolume:1,submitPhase:"CONFIRMED",cancelPhase:"NONE",displayStatus:"部分成交",createdAt:"2026-09-09T06:20:00Z",updatedAt:"2026-09-09T06:20:00Z",source:"ai",strategyId:"s",reason:""}];
  const view=render(<OperationTimeline {...props} activity={data}/>);
  await act(async()=>{await Promise.resolve();});
  fireEvent.click(screen.getByRole("button",{name:"清空显示"}));
  const next=structuredClone(data);next.orders[0].displayStatus="已撤销";
  view.rerender(<OperationTimeline {...props} activity={next}/>);
  expect(screen.getByText(/委托回读 1\/3 手 · 已撤销/)).toBeVisible();
  await act(async()=>{await vi.advanceTimersByTimeAsync(4000);});
  expect(call.mock.calls.filter(c=>c[1]?.method==="POST")).toHaveLength(0);
});


test("clearing history exits history and releases chart selection without deleting records", async () => {
  const clear=vi.fn();const data=snapshot(["f"]);
  const view=render(<OperationTimeline {...props} activity={data} onClear={clear}/>);
  await act(async()=>{await Promise.resolve();});
  fireEvent.click(screen.getByRole("button",{name:"查看历史"}));
  fireEvent.click(screen.getByRole("button",{name:"清空显示"}));
  expect(clear).toHaveBeenCalledOnce();
  expect(screen.getByText("暂无新操作")).toBeVisible();
  expect(data.fills).toHaveLength(1);
  view.unmount();
});
