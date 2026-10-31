import type { ActivitySnapshot } from "./runtimeClient";
import { orderStatusText } from "./components/futures/support";
export type ReviewedTrade = {id:string;symbol:string;direction:string;volume:number;openTime:number;closeTime:number;openPrice:number;closePrice:number;gross:number;net:number|null;fees:number|null};
export type TradeReviewSnapshot = {trades:ReviewedTrade[];costSource:string};
export type ActivityTrade = {id:string;revision:string;time:number|string;direction:string;offset:string;volume:number;price:number|null;status?:string;closes:Pick<ReviewedTrade,"id"|"closeTime"|"closePrice"|"volume">[];pnl?:number;net:boolean};
const millis=(time:number|string)=>typeof time === "number" ? time/1e6 : Date.parse(time);
export function activityTradeRows(activity:ActivitySnapshot|undefined, review?:TradeReviewSnapshot):ActivityTrade[] {
  if(!activity)return [];
  const fills=[...new Map(activity.fills.map(f=>[`${f.clientOrderId}:${f.tradeId}`,f])).values()];
  const consumed=new Map<string,number>();
  const paired=new Map<string,(ReviewedTrade & {closeTradeId:string})[]>();
  const allocated=new Map<string,number>();
  // The business API owns FIFO/trading-day/fee rules. Accept only a unique
  // opening fill reference with matching facts, never a nearby time or price.
  for(const round of new Map((review?.trades||[]).map(r=>[r.id,r])).values()) {
    const candidates=fills.filter(f=>f.offset==="OPEN"&&round.id.startsWith(f.tradeId+":")&&round.symbol===f.symbol&&round.openTime===f.tradeDateTime&&round.openPrice===f.price&&round.direction===(f.direction==="BUY"?"long":"short"));
    if(candidates.length!==1||!Number.isFinite(round.gross)||!Number.isFinite(round.closePrice)||!Number.isFinite(round.closeTime)||!Number.isFinite(round.volume)||round.volume<=0)continue;
    const open=candidates[0], used=allocated.get(open.tradeId)||0;
    if(used+round.volume>open.volume||round.closeTime<round.openTime)continue;
    const closeId=round.id.slice(open.tradeId.length+1);
    if(!closeId)continue;
    const closing=fills.filter(f=>f.tradeId===closeId);
    if(closing.length && (closing.length!==1||closing[0].symbol!==round.symbol||closing[0].offset==="OPEN"||closing[0].direction===open.direction||closing[0].price!==round.closePrice||closing[0].tradeDateTime!==round.closeTime||(consumed.get(closeId)||0)+round.volume>closing[0].volume))continue;
    allocated.set(open.tradeId,used+round.volume);
    consumed.set(closeId,(consumed.get(closeId)||0)+round.volume);
    paired.set(open.clientOrderId,[...(paired.get(open.clientOrderId)||[]),{...round,closeTradeId:closeId}]);
  }
  const rows:ActivityTrade[]=[];
  for(const order of activity.orders) {
    const actual=fills.filter(f=>f.clientOrderId===order.clientOrderId);
    const remaining=actual.map(f=>({...f,volume:f.volume-(f.offset==="OPEN"?0:consumed.get(f.tradeId)||0)})).filter(f=>f.volume>0);
    if(actual.length&&!remaining.length)continue;
    const quantity=remaining.reduce((n,f)=>n+f.volume,0);
    const allPriced=remaining.every(f=>f.price!==null&&Number.isFinite(f.price));
    const price=quantity ? allPriced ? remaining.reduce((n,f)=>n+f.price!*f.volume,0)/quantity : null : order.request.limitPrice;
    const closes=(paired.get(order.clientOrderId)||[]).sort((a,b)=>a.closeTime-b.closeTime);
    const closeRows=new Map<string,ActivityTrade["closes"][number]>();
    for(const close of closes) {
      const key=JSON.stringify([close.closeTradeId,close.closeTime,close.closePrice]);
      const row=closeRows.get(key);
      if(row)row.volume+=close.volume;
      else closeRows.set(key,{id:key,closeTime:close.closeTime,closePrice:close.closePrice,volume:close.volume});
    }
    const net=review?.costSource!=="estimate"&&closes.every(c=>c.net!==null&&Number.isFinite(c.net));
    const revision=JSON.stringify([order.displayStatus,order.filledVolume,order.cancelRequestedAt]);
    rows.push({id:order.clientOrderId,revision:closes.length?JSON.stringify([revision,closes]):revision,
      time:remaining[0]?.tradeDateTime??order.createdAt,direction:order.request.direction,offset:order.request.offset,
      volume:quantity||order.request.volume,price,status:quantity?undefined:order.submitPhase==="PREPARED"?order.cancelRequestedAt?"未发送 · 已请求取消":"尚未发送":orderStatusText(order),
      closes:[...closeRows.values()],net,pnl:closes.length?closes.reduce((n,c)=>n+(net?c.net!:c.gross),0):undefined});
  }
  return rows.sort((a,b)=>millis(b.closes.at(-1)?.closeTime??b.time)-millis(a.closes.at(-1)?.closeTime??a.time));
}
export const activityTime=(time:number|string)=>{
  const d=new Date(millis(time));
  return Number.isNaN(d.valueOf())?"时间待确认":new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).format(d);
};
