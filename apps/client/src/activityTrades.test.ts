// @vitest-environment node
import { activityTradeRows, activityTime, type TradeReviewSnapshot } from "./activityTrades";
import type { ActivitySnapshot } from "./runtimeClient";
const openTime=Date.parse("2026-09-09T06:20:00Z")*1e6,closeTime=openTime+840e9;
const fill=(id:string,offset="OPEN",volume=3)=>({tradeId:id,clientOrderId:id,symbol:"SHFE.rb2610",direction:offset==="OPEN"?"BUY":"SELL",offset,price:offset==="OPEN"?3099:3100,volume,tradeDateTime:offset==="OPEN"?openTime:closeTime,source:"ai" as const});
const data=(fills=[fill("o"),fill("c","CLOSETODAY")])=>({symbol:"SHFE.rb2610",environment:"simulation",dataStatus:"normal",ageSeconds:0,capturedAt:"",fills,orders:fills.map(f=>({clientOrderId:f.clientOrderId,request:{...f,limitPrice:f.price},displayStatus:"已成交",filledVolume:f.volume,createdAt:"2026-09-09T06:20:00Z"})),strategies:[]} as unknown as ActivitySnapshot);
const review:TradeReviewSnapshot={costSource:"account",trades:[{id:"o:c",symbol:"SHFE.rb2610",direction:"long",volume:3,openTime,closeTime,openPrice:3099,closePrice:3100,gross:30,fees:null,net:null}]};
it("merges a business-matched close into its opening, without inventing net fees",()=>{
 const rows=activityTradeRows(data(),review);expect(rows).toHaveLength(1);expect(rows[0]).toMatchObject({volume:3,price:3099,pnl:30,net:false});expect(rows[0].closes).toHaveLength(1);
 expect(activityTime(openTime)).toBe("09/09 14:20");
});
it("partial closes do not consume another unmatched closing quantity or overallocate an opening",()=>{
 const r={...review,trades:[{...review.trades[0],volume:1,gross:10}]};
 const rows=activityTradeRows(data(),r);expect(rows).toHaveLength(2);expect(rows.find(r=>r.id==="c")?.volume).toBe(2);expect(rows.find(r=>r.id==="o")?.pnl).toBe(10);
 expect(activityTradeRows(data(),{...review,trades:[{...review.trades[0],volume:4}]}).every(r=>r.pnl===undefined)).toBe(true);
});
it("rejects a wrong symbol, altered entry facts or ambiguous fill reference, and never pairs by time alone",()=>{
 for(const patch of [{symbol:"SHFE.au2612"},{openPrice:3101},{openTime:openTime-1e9},{id:"unknown:c"}]) {
  const rows=activityTradeRows(data(),{...review,trades:[{...review.trades[0],...patch}]});expect(rows).toHaveLength(2);expect(rows.every(r=>r.pnl===undefined)).toBe(true);
 }
});
it("deduplicates repeated fills and rounds and uses net only for actual replay costs",()=>{
 const d=data();d.fills.push(d.fills[0]);const r={...review,trades:[review.trades[0],review.trades[0]]};expect(activityTradeRows(d,r)[0].pnl).toBe(30);
 const custom={costSource:"custom_simulation",trades:[{...review.trades[0],net:24,fees:6}]};expect(activityTradeRows(data(),custom)[0]).toMatchObject({pnl:24,net:true});
 expect(activityTradeRows(data(),{...custom,costSource:"estimate"})[0]).toMatchObject({pnl:30,net:false});
});
it("a later close changes the opening display revision, and missing review leaves facts intact",()=>{
 const first=activityTradeRows(data());const closed=activityTradeRows(data(),review);expect(first).toHaveLength(2);expect(first[0].revision).not.toBe(closed[0].revision);
 const shortData=data();shortData.fills.forEach(f=>f.direction=f.direction==="BUY"?"SELL":"BUY");shortData.orders.forEach(o=>o.request.direction=o.request.direction==="BUY"?"SELL":"BUY");
 expect(activityTradeRows(shortData,{...review,trades:[{...review.trades[0],direction:"short",gross:-30} ]})[0].pnl).toBe(-30);
});

it("combines split opening fills closed by one execution into one closing line",()=>{
 const d=data();d.fills[0].volume=1;
 d.fills.push({...d.fills[0],tradeId:"o2",volume:2});
 const r={...review,trades:[{...review.trades[0],volume:1,gross:10},{...review.trades[0],id:"o2:c",volume:2,gross:20}]};
 const rows=activityTradeRows(d,r);expect(rows).toHaveLength(1);expect(rows[0].closes).toHaveLength(1);expect(rows[0].closes[0].volume).toBe(3);expect(rows[0].pnl).toBe(30);
});
