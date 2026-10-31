import { useRef, useState, type ComponentProps } from "react";
import { X } from "lucide-react";
import type { SimulationOrder } from "../../api";
import { TradeTicket } from "../futures/TradeTicket";
import { formatNumber, orderStatusText, type ManualSimulationRequest } from "../futures/support";
import { orderLegLabel, type ManualOrderOutcome } from "../../hooks/useSimulationOrders";

type TicketProps = ComponentProps<typeof TradeTicket>;

/**
 * 下单页 (手机版第二期, 2026-10-04): the desktop's 下单票 as a bottom sheet over 看盘 — the same
 * ticket, checks and confirm-then-send path (`onSubmit` is the workspace's manual order, which
 * creates the proposal and confirms it), laid out for a phone. After a confirm the sheet shows the
 * order's own status words (已受理 / 部分成交 / 已成交 / 结果未知 …), or 未发送 when nothing went out.
 */
export function MobileOrder({ name, accountLabel, available, orders, onClose, ticket }: {
  name: string;
  accountLabel?: string;
  available?: number;
  /** The window's simulation orders, newest first: the result line follows the order this sheet sent. */
  orders: SimulationOrder[];
  onClose: () => void;
  ticket: Omit<TicketProps, "variant">;
}) {
  const [sent, setSent] = useState<{ before: Set<string>; symbol: string; outcome?: "unsent" | ManualOrderOutcome }>();
  const pending = useRef(false);
  const submit = async (request: ManualSimulationRequest) => {
    if (pending.current) return undefined;
    pending.current = true;
    const symbol = ticket.snapshot.quote.symbol;
    const before = new Set(orders.map(order => order.clientOrderId));
    setSent({ before, symbol });
    try {
      const outcome = await ticket.onSubmit(request) || undefined;
      setSent(current => current && current.before === before ? { ...current, outcome: outcome?.confirmed ? outcome : "unsent" } : current);
      return outcome;
    } finally { pending.current = false; }
  };
  // Every order this confirmation placed — a 今+昨 close is two (#932 A26) — read live from the window's
  // list so fills and cancels show; before the answer arrives, the new rows on this contract.
  const outcome = typeof sent?.outcome === "object" ? sent.outcome : undefined;
  const placed = !sent ? [] : outcome
    ? outcome.orders.map(order => orders.find(row => row.clientOrderId === order.clientOrderId) ?? order)
    : orders.filter(order => !sent.before.has(order.clientOrderId) && order.request.symbol.toUpperCase() === sent.symbol.toUpperCase());
  const statuses = [...new Set(placed.map(order => order.displayStatus))];
  const status = placed.length === 1 ? orderStatusText(placed[0])
    : placed.length ? `共 ${placed.reduce((sum, order) => sum + order.request.volume, 0)} 手 · ${placed.length} 笔`
      : sent?.outcome === "unsent" ? "未发送" : sent ? "提交中…" : undefined;
  const leg = (order: SimulationOrder) => `${orderLegLabel(order)} ${order.request.volume} 手 @ ${order.request.limitPrice}`;
  return (
    <div className="fdm-sheet-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="fdm-sheet fdm-order" role="dialog" aria-label="下单" data-testid="mobile-order" data-agent-target="mobile-order">
        <i className="fdm-sheet-grip" aria-hidden="true" />
        <header className="fdm-sheet-head">
          <span><strong>{name} 下单</strong><small>{[accountLabel, available !== undefined ? `可用 ${formatNumber(available, 0)}` : ""].filter(Boolean).join(" · ")}</small></span>
          <button type="button" aria-label="关闭下单" onClick={onClose}><X aria-hidden="true" /></button>
        </header>
        <div className="fdm-sheet-body">
          <TradeTicket {...ticket} onSubmit={submit} variant="mobile" />
          {status ? <p className="fdm-order-result" role="status" data-testid="mobile-order-result"
            data-status={statuses.length === 1 ? statuses[0] : statuses.length ? "mixed" : typeof sent?.outcome === "string" ? sent.outcome : undefined}>
            委托结果：<strong>{status}</strong>{placed.length === 1 ? ` · ${leg(placed[0])}` : ""}
            {placed.length > 1 ? placed.map(order => <span key={order.clientOrderId} className="fdm-order-leg">{leg(order)}：{orderStatusText(order)}</span>) : null}
            {outcome?.skipped.map((item, index) => <span key={`skip-${index}`} className="fdm-order-leg">另 {item.volume} 手未纳入：{item.label}</span>)}
          </p> : null}
        </div>
      </section>
    </div>
  );
}
