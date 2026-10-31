import { useEffect, useRef, useState } from "react";
import { requestJson } from "../api";
import type { ActivitySnapshot } from "../runtimeClient";
import type { components } from "../workbench.generated";
import { operationEvents, type OperationEvent } from "../operationEvents";
import { monitorTime } from "../strategyMonitorDisplay";
import { usePersistentDraft } from "../hooks/usePersistentDraft";
import { useDisclosureSurface } from "../uiSurfaces";
import { History } from "lucide-react";
import { CardShell } from "./CardShell";

type Explanation = components["schemas"]["ExplanationResult"];
export function OperationTimeline({baseUrl,activity,scope,strategyId,unavailable,busy,onAsk,onLocate,selectedTrades,onCapability,onClear}: {
  baseUrl?: string; activity?: ActivitySnapshot; scope: string; strategyId?: string; unavailable: boolean; busy: boolean;
  onAsk:(event:OperationEvent)=>void; onLocate:(event:OperationEvent)=>void; selectedTrades?:string[];
  onCapability:(supported:boolean)=>void; onClear?:()=>void;
}) {
  const [supported,setSupported] = useState<boolean>();
  const [results,setResults] = useState<Record<string,Explanation>>({});
  const [hidden,setHidden] = usePersistentDraft<Record<string,string>>(`ai-workspace-events:${scope}`,{});
  const [history,setHistory] = useDisclosureSurface("timeline-history");
  const seen = useRef<Map<string,string> | undefined>(undefined);
  const pending = useRef(new Map<string, {event:OperationEvent;at:number}>());
  const working = useRef(false);
  const [analyzing,setAnalyzing] = useState(false);
  const generation = useRef(0);
  const lifecycle = useRef(new AbortController());
  const latest = useRef({busy,unavailable,supported}); latest.current={busy,unavailable,supported};
  const events=operationEvents(activity).filter(e=> !strategyId || e.strategyId === strategyId || !e.strategyId);
  const signature=JSON.stringify(events.map(e=>[e.key,e.version]));
  useEffect(()=>{
    const controller=new AbortController(); lifecycle.current=controller;
    let active=true;
    if(baseUrl) void requestJson<{operations:{operation:string}[]}>(`${baseUrl}/api/agent/capabilities`,{signal:controller.signal})
      .then(value=>{if(active){const yes=value.operations.some(o=>o.operation==="POST /api/app/strategy-explanations");setSupported(yes);onCapability(yes);}})
      .catch(()=>{if(active){setSupported(false);onCapability(false);}});
    return ()=>{active=false;controller.abort();++generation.current;pending.current.clear();};
  },[baseUrl,onCapability]);
  const explain=async(event:OperationEvent,retry=false)=>{
    if(!baseUrl || !latest.current.supported || working.current) return;
    working.current=true;setAnalyzing(true);const epoch=generation.current;
    const valid=()=>epoch===generation.current&&!lifecycle.current.signal.aborted;
    const key=`${event.key}:${event.version}`;
    try {
      let result=await requestJson<Explanation>(`${baseUrl}/api/app/strategy-explanations`,{method:"POST",signal:lifecycle.current.signal,
        body:JSON.stringify({symbol:event.symbol,strategyId:event.strategyId,eventType:event.eventType,eventId:event.eventId,retry})});
      const end=Date.now()+31_000;
      while(valid()){
        setResults(previous=>({...previous,[key]:result}));
        if(!["waiting","analyzing"].includes(result.status)) break;
        if(Date.now()>end) throw new Error("timeout");
        await new Promise(resolve=>setTimeout(resolve,1000));
        if(!valid()) break;
        result=await requestJson<Explanation>(`${baseUrl}/api/app/strategy-explanations/${result.id}`,{signal:lifecycle.current.signal});
      }
    } catch {if(valid())setResults(previous=>({...previous,[key]:{id:key,status:"unavailable",detail:"解释暂不可用，可重试",evidence:{},environment:activity?.environment||"simulation"}}));}
    finally {working.current=false;if(valid())setAnalyzing(false);}
  };
  const explainRef=useRef(explain);explainRef.current=explain;
  useEffect(()=>{
    if(unavailable||!activity){seen.current=undefined;pending.current.clear();return;}
    const current=new Map(events.map(e=>[e.key,e.triggerVersion || e.version]));
    if(seen.current && supported){
      for(const event of events) if(event.automatic&&seen.current.get(event.key)!==(event.triggerVersion || event.version)){
        pending.current.set(event.group,{event,at:Date.now()+3000});
      }
    }
    seen.current=current;
  },[signature,unavailable,Boolean(activity),supported]);
  useEffect(()=>{
    const timer=setInterval(()=>{
      if(working.current||latest.current.busy||latest.current.unavailable||!latest.current.supported)return;
      const ready=[...pending.current.entries()].find(([,value])=>value.at<=Date.now());
      if(ready){pending.current.delete(ready[0]);void explainRef.current(ready[1].event);}
    },250);
    return()=>clearInterval(timer);
  },[]);
  const visible=events.filter(e=>history||hidden[e.key]!==e.version);
  const selected=events.filter(e=>e.tradeIds?.some(id=>selectedTrades?.includes(id)));
  const [expanded,setExpanded] = useDisclosureSurface("timeline-expanded", { available: !selected.length && visible.length > 5 });
  const shown=selected.length?selected:visible.slice(0,expanded?100:5);
  const meta = selected.length ? `${selected.length} 条` : !visible.length ? (history ? "没有记录" : "暂无新操作") : `${history ? "全部" : "最近"} ${visible.length} 条`;
  return <CardShell icon={<History size={18} />} title={selected.length?"选中成交":"最近操作"} meta={meta} className="ai-operation-timeline" testId="operation-timeline"
    headAction={!selected.length ? <button type="button" onClick={()=>setHistory(v=>!v)}>{history?"返回当前":"查看历史"}</button> : undefined}>
    {supported===false&&<p className="ai-explanation-availability"><span>自动解释未接入</span>，可用“问 AI”手动提问。</p>}
    {unavailable&&<p role="status">操作记录连接中断，当前结果待确认</p>}
    {shown.map(event=>{const result=results[`${event.key}:${event.version}`];return <article className={`ai-operation-row ${event.warning ? "warning" : ""}`} key={event.key}>
      <header><strong>{event.title}</strong><time>{monitorTime(event.time)}{!event.strategyId ? " · 账户记录" : ""}{activity?.environment==="replay"?" · 历史回放":""}</time></header>
      <p className={event.warning?"ai-event-warning":""}>{event.eventType === "fill" && event.detail.endsWith(" · 已成交") ? event.detail.split(" · 委托回读")[0] : event.detail}</p>
      {result&&<div className="ai-event-explanation" role="status"><strong>AI 解释</strong><p>{result.status==="completed"?result.summary:result.status==="waiting"?"等待空闲分析":result.status==="analyzing"?"正在分析事件依据…":result.detail}</p>{result.status==="completed"&&<details><summary>查看依据</summary><p>事件回报 · {monitorTime(result.eventTime)}{result.environment==="replay"?" · 历史时间":""}</p><p>{result.evidence.recordedSignal ? "已关联事件当时保存的策略判定" : "未提供事件当时的指标判定"}</p></details>}</div>}
      <div className="ai-event-actions"><button type="button" onClick={()=>onAsk(event)}>问 AI</button>{event.tradeIds?.length&&<button type="button" onClick={()=>onLocate(event)}>定位图表</button>}{supported&&<button type="button" disabled={busy||analyzing} onClick={()=>void explain(event,true)}>{result?.status==="unavailable"?"重试解释":"解释原因"}</button>}</div>
    </article>;})}
    {!selected.length && visible.length > 0 && <div className="card-inline-actions">
      {visible.length>5&&<button type="button" onClick={()=>setExpanded(v=>!v)}>{expanded?"收起":"查看更多操作"}</button>}
      <button type="button" className="app-button danger" disabled={!visible.length} onClick={()=>{setHidden(Object.fromEntries(events.map(e=>[e.key,e.version])));setHistory(false);onClear?.();}}>清空显示</button>
    </div>}
  </CardShell>;
}
