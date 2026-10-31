import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useDialogSurface } from "../uiSurfaces";
import { Clock3, X } from "lucide-react";
import type { MarketWorkbenchSnapshot } from "../api";
import { CME_HOURS_HELP, QUOTE_STALE_SECONDS, cmeHours, marketClosedNow, marketSessionDisplay, quoteAgeSeconds, sessionSeconds, sessionTime } from "../marketSession";
import { HelpTip } from "./Blocks";
import "./MarketSessionCard.css";

export function MarketSessionCard({ market, replayActive }: { market: MarketWorkbenchSnapshot; replayActive?: boolean }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useDialogSurface("market-session", dialog);
  const display = marketSessionDisplay(market, replayActive);
  const schedule = market.quote.tradingSchedule;
  // 外盘 (CME): the snapshot carries CME's hours and, when shut, why (sessionClosedFor).
  const cme = replayActive ? undefined : cmeHours(schedule);
  const closedFor = market.quote.sessionClosedFor;
  const closed = marketClosedNow(market);
  const live = market.dataStatus === "normal";
  // 新浪备用 (2026-10-07): the pill says where the numbers come from before anything else.
  const backup = market.dataStatus === "backup";
  const preOpen = market.quote.dataErrorCode === "market_quote_pre_open";
  const noTrade = market.quote.dataErrorCode === "market_quote_no_trade";
  // "X 秒无新报价" counts by the wall clock and moves once a second on its own — the snapshot's
  // age only moves when a new snapshot arrives, and this label must not wait for one.
  const [now, setNow] = useState(() => Date.now());
  // A replay's quotes carry historical stamps: against the wall clock they are always "days old"
  // (405024 秒无新报价 on 10-04). The replay shows its own session state instead.
  const watching = live && !closed && !replayActive;
  useEffect(() => {
    if (!watching) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [watching]);
  const quoteAge = watching ? quoteAgeSeconds(market.quote.quoteDatetime, now) : undefined;
  const noNewQuote = quoteAge !== undefined && quoteAge > QUOTE_STALE_SECONDS;
  // 集合竞价（AUCTIONORDERING）是交易所报的交易状态，比按时刻表推的"开盘前"更准，优先展示。
  const auction = market.quote.tradingStatus === "AUCTIONORDERING";
  const label = backup ? "备用行情 · 新浪" : auction ? "集合竞价" : preOpen ? "开盘前" : noTrade ? "无成交" : !closed && !live ? "行情待更新" : noNewQuote ? `${Math.round(quoteAge)} 秒无新报价` : display.label;
  return <>
    <button type="button" className={`market-session-trigger ${backup ? "backup" : closed ? "closed" : live && display.label === "交易中" ? "live" : "stale"}`} aria-haspopup="dialog" aria-label={`${label}，查看交易时间`} title="查看当前品种交易时间" onClick={() => dialog.current?.showModal()}>{label}</button>
    {createPortal(<dialog ref={dialog} className="market-session-card" aria-label="当前品种交易时间" onClick={event => { if (event.target === event.currentTarget) { const box = event.currentTarget.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.current?.close(); } }}>
      <header><div><strong>{market.quote.name} · 交易时间{cme ? <HelpTip helpKey="market-session:source" label="交易时间">{CME_HOURS_HELP}</HelpTip>
        : schedule ? <HelpTip helpKey="market-session:source" label="交易时间">常规交易时段来自当前合约行情资料；节假日及临时调整以交易所公告为准。</HelpTip> : null}</strong><small>{market.quote.symbol} · 北京时间{replayActive ? " · 回放" : ""}</small></div><button type="button" aria-label="关闭交易时间" onClick={() => dialog.current?.close()}><X size={18}/></button></header>
      <div className="market-session-current"><Clock3 size={18}/><strong>{display.detail}</strong></div>
      {cme ? <section data-testid="market-session-cme"><h3>CME 交易时段</h3>
        {cme.periods.map((period, index) => <div key={`${period.start}-${period.end}`}>
          {index > 0 && <div className={`market-session-row break ${closed && closedFor === "intraday" ? "current" : ""}`}><span>盘中休息</span><span>{cme.periods[index - 1].end}–{period.start}</span></div>}
          <div className="market-session-row"><span>交易</span><span>{period.start}–{period.end}</span></div>
        </div>)}
        <div className={`market-session-row break ${closed && closedFor === "break" ? "current" : ""}`}><span>每日停盘</span><span>{cme.closes}–{cme.opens}</span></div>
        <div className={`market-session-row break ${closed && closedFor === "weekend" ? "current" : ""}`}><span>周末休市</span><span>周六 {cme.closes}–周一 {cme.opens}</span></div>
      </section> : schedule ? <>
        {(["day", "night"] as const).map(group => <section key={group}><h3>{group === "day" ? "日盘" : "夜盘"}</h3>{schedule[group].length ? schedule[group].map(([start, end], index, periods) => {
          const previous = periods[index - 1]?.[1];
          const crossesMidnight = sessionSeconds(end) < sessionSeconds(start);
          return <div key={`${start}-${end}`}>
            {previous && <div className={`market-session-row break ${display.breakStart === previous && display.breakEnd === start ? "current" : ""}`}><span>盘中休息</span><span>{sessionTime(previous)}–{sessionTime(start)}</span></div>}
            <div className="market-session-row"><span>交易</span><span>{sessionTime(start)}–{crossesMidnight ? "次日 " : ""}{sessionTime(end)}</span></div>
          </div>;
        }) : <p>无{group === "day" ? "日盘" : "夜盘"}交易时段</p>}</section>)}
      </> : <p>当前行情服务尚未提供该合约的交易时间。</p>}
    </dialog>, document.body)}
  </>;
}
