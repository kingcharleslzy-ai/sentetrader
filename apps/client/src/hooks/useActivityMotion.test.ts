import { act, renderHook, cleanup } from "@testing-library/react";
import { vi, afterEach, expect, it } from "vitest";
import type { ActivitySnapshot } from "../runtimeClient";
import { useActivityFresh, useFillFeedback } from "./useActivityMotion";
const snapshot=(volume=0)=>({dataStatus:"normal",ageSeconds:0,fills:volume?[{clientOrderId:"order",tradeId:"fill",volume}]:[]} as ActivitySnapshot);
afterEach(()=>{cleanup();vi.useRealTimers();});
it("stops the orbit when reports expire or a connection error arrives",()=>{
 vi.useFakeTimers();const data=snapshot();
 const {result,rerender}=renderHook(({error})=>useActivityFresh(data,error),{initialProps:{error:""}});
 expect(result.current).toBe(true);act(()=>vi.advanceTimersByTime(15001));expect(result.current).toBe(false);
 rerender({error:"offline"});expect(result.current).toBe(false);
});
it("highlights only new fills, without replaying initial, repeated, reconnected or switched history",()=>{
 vi.useFakeTimers();
 const {result,rerender}=renderHook(({data,scope,enabled})=>useFillFeedback(data,scope,enabled),{initialProps:{data:snapshot(1),scope:"a",enabled:true}});
 expect(result.current.size).toBe(0);
 rerender({data:snapshot(2),scope:"a",enabled:true});expect(result.current.has("order")).toBe(true);
 act(()=>vi.advanceTimersByTime(651));expect(result.current.size).toBe(0);
 rerender({data:snapshot(2),scope:"a",enabled:true});expect(result.current.size).toBe(0);
 rerender({data:snapshot(3),scope:"b",enabled:true});expect(result.current.size).toBe(0);
 rerender({data:snapshot(3),scope:"b",enabled:false});
 rerender({data:snapshot(4),scope:"b",enabled:true});expect(result.current.size).toBe(0);
});
