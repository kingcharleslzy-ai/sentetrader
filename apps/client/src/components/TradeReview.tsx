import { useEffect, useState } from "react";
import { useTradeReview } from "../hooks/useTradeReview";
import { signClass } from "./futures/support";

const money = (value: number) => value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const time = (value: number) => new Date(value / 1_000_000).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });

export function TradeReview({ baseUrl, symbol, onFocus }: { baseUrl: string; symbol: string; onFocus: (range: { openTime: number; closeTime: number; direction: string } | undefined) => void }) {
  const { review, error } = useTradeReview({ baseUrl, symbol });
  const [selected, setSelected] = useState("");
  useEffect(() => { setSelected(""); onFocus(undefined); }, [baseUrl, symbol]);
  const detail = review?.trades.find(trade => trade.id === selected);
  return <section className="strategy-trade-review"><header><h3>交易复盘</h3><span>{symbol}</span></header>
    {review && <><div className="strategy-review-total"><span>账户净盈亏</span><strong className={signClass(review.account.todayNetPnl)}>{money(review.account.todayNetPnl)}<small> 元</small></strong></div>
      <dl className="strategy-review-metrics"><div><dt>权益</dt><dd>{money(review.account.equity)}</dd></div><div><dt>已计手续费</dt><dd>{money(review.account.commission)}</dd></div><div><dt>完整交易配对</dt><dd>{review.trades.length}</dd></div><div><dt>剩余持仓</dt><dd>{review.account.positionCount}</dd></div></dl>
      <p>{review.costSource === "custom_simulation" ? "本次回放自定义费率" : review.costSource === "estimate" ? "逐笔为自定义费用测算；账户汇总保留实际扣费"
        : review.costSource === "foreign" ? "外盘按撮合引擎的每手美元手续费扣（美元）" : "账户实际扣费；逐笔手续费未提供时显示毛收益"}</p>
      <h4>开平仓记录</h4>{review.trades.map(trade => <button className={`strategy-review-trade ${selected === trade.id ? "selected" : ""}`} key={trade.id} onClick={() => { setSelected(trade.id); onFocus(trade); }}><span><strong>{trade.direction === "long" ? "开多 → 平多" : "开空 → 平空"} · {trade.volume} 手</strong><b className={signClass(trade.net ?? trade.gross)}>{money(trade.net ?? trade.gross)}</b></span><small>{time(trade.openTime)} — {time(trade.closeTime)} · {trade.net === null ? "毛收益" : "净收益"}</small></button>)}
      {!review.trades.length && <p>还没有完整的开平仓记录。</p>}
      {detail && <div className="strategy-trade-detail"><p>开仓 {detail.openPrice} → 平仓 {detail.closePrice}</p><p>毛收益 {money(detail.gross)}{detail.fees !== null ? ` − 手续费 ${money(detail.fees)} = 净收益 ${money(detail.net!)}` : "；逐笔手续费未提供"}</p><button onClick={() => { setSelected(""); onFocus(undefined); }}>显示全图</button></div>}
      {review.unmatched.length > 0 && <p>部分平仓缺少对应开仓记录，未推算其收益。</p>}
    </>}{error && <p role="alert">{error}</p>}
  </section>;
}
