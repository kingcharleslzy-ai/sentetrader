import { CandlestickChart } from "lucide-react";
import type { MarketSnapshot } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";
import { Sparkline } from "../Sparkline";
import { signedPercent } from "./cardActions";
import "./AnswerCard.css";

/** Direction of a change as shown (two decimals): a change that rounds to 0.00% has none. */
const peerDir = (change: number) => { const shown = Number(change.toFixed(2)); return shown > 0 ? "up" : shown < 0 ? "down" : undefined; };

export function MarketCard({ market, id }: { market: MarketSnapshot; id?: string }) {
  const dataStatus = market.dataStatus ?? "simulated";
  const stale = dataStatus === "stale";
  const backup = dataStatus === "backup";
  const source = dataStatus === "simulated" ? "演示行情" : backup ? (market.source ?? "新浪（备用）") : "快期行情";
  return (
    <CardShell
      icon={<CandlestickChart />}
      title={`${market.name} ${market.symbol}`}
      badge={stale ? "数据陈旧" : backup ? "备用行情" : market.trend === "up" ? "短线上行" : market.trend === "down" ? "短线回落" : "横盘"}
      badgeTone={stale || backup ? "warning" : market.trend === "up" ? "positive" : market.trend === "down" ? "danger" : "neutral"}
      id={id}
      testId="market-card"
      details={
        <dl className="card-facts">
          <div><dt>来源</dt><dd>{source} · {market.updatedAt}{stale ? " · 数据陈旧" : ""}</dd></div>
          {market.summary ? <div><dt>说明</dt><dd>{market.summary}</dd></div> : null}
        </dl>
      }
    >
      <div className="market-row">
        <div>
          <span className="eyebrow">最新价</span>
          <strong className="market-price">{market.price.toFixed(2)}</strong>
          {/* 涨跌用市场方向色（涨跌色偏好生效），不用成功/危险状态色。 */}
          <span className="market-change mc-change" data-dir={peerDir(market.changePercent)}>
            {signedPercent(market.changePercent)}
          </span>
        </div>
        <Sparkline values={market.priceCurve} className="market-chart" label="短线价格曲线" />
      </div>
      {market.peers?.length ? (
        // 回答卡片化 (2026-10-14): other contracts read in the same turn ride on the turn's one quote card.
        <ul className="mc-peers" aria-label="同时读取的合约" data-testid="market-peers">
          {market.peers.map(peer => (
            <li key={peer.symbol}>
              <span className="mc-peer-name">{peer.name}<small>{peer.symbol}</small>
                {peer.dataStatus ? <span className="mc-peer-warning">{peer.dataStatus === "stale" ? "数据陈旧" : "备用行情"}</span> : null}</span>
              <span className="mc-peer-price">{peer.price.toFixed(2)}</span>
              <span className="mc-peer-change" data-dir={peerDir(peer.changePercent)}>
                {signedPercent(peer.changePercent)}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </CardShell>
  );
}
