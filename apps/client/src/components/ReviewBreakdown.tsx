import type { BreakdownRow, ReviewBreakdown } from "../accountPnl";
import { SectionCard } from "./Blocks";
import { formatSigned, signClass } from "./futures/support";
import "./ReviewBreakdown.css";

const HINT = "近 366 天已平仓的交易按三种方式分组：交易笔记看下单时选的或复盘里补标的笔记（没标的归“未标笔记”）；时段按开仓时间落在哪个交易小节（只算国内合约，夜盘每小时一段）；星期按平仓的交易日（夜盘算到下一个交易日）。盈亏用扣费后的净值，没有逐笔手续费时用毛值。差得明显的分组会记进 AI 的经验。";

function Table({ title, rows }: { title: string; rows: BreakdownRow[] }) {
  return <table className="review-breakdown-table" aria-label={title}>
    <thead><tr><th scope="col">{title}</th><th scope="col">笔数</th><th scope="col">胜率</th><th scope="col">平均每笔</th></tr></thead>
    <tbody>{rows.map(row => <tr key={row.label}>
      <th scope="row">{row.label}</th><td>{row.trades}</td><td>{row.winRate}%</td>
      <td className={signClass(row.average)}>{formatSigned(row.average, 0)}</td>
    </tr>)}</tbody>
  </table>;
}

/** 复盘页“按打法、时段、星期”: three small tables from the Runtime's review breakdown. */
export function ReviewBreakdownBlock({ data }: { data: ReviewBreakdown | null | undefined }) {
  if (!data || !data.byPlaybook.length) return null;
  return <SectionCard title="按交易笔记、时段、星期" hint={HINT} helpKey="review-breakdown" className="review-breakdown">
    <div className="review-breakdown-tables">
      <Table title="交易笔记" rows={data.byPlaybook} />
      {data.byTimeBlock.length ? <Table title="时段" rows={data.byTimeBlock} /> : null}
      {data.byWeekday.length ? <Table title="星期" rows={data.byWeekday} /> : null}
    </div>
  </SectionCard>;
}
