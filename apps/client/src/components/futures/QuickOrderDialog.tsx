import { useEffect, useRef } from "react";
import { formatNumber, type ManualSimulationRequest } from "./support";
import type { PendingQuickOrder } from "./useQuickOrders";
import { useDialogSurface } from "../../uiSurfaces";

const sideLabel: Record<ManualSimulationRequest["side"], string> = { buy_open: "买入开仓", sell_open: "卖出开仓", buy_close: "买入平仓", sell_close: "卖出平仓" };

function priceLabel(request: ManualSimulationRequest, decimals: number): string {
  if (request.limitPrice !== undefined) return `限价 ${formatNumber(request.limitPrice, decimals)}`;
  return request.priceIntent === "aggressive" ? "对手价" : request.priceIntent === "last" ? "最新价" : "排队价";
}

/** 上期所 / 能源中心的平仓：今仓、昨仓由 Runtime 按“平今平昨”设置分配（跨今昨时一次确认分两笔发出）。 */
const splitByRuntime = (symbol: string, request: ManualSimulationRequest) => !request.positionBucket && request.side.endsWith("_close")
  && ["SHFE", "INE"].includes(symbol.split(".")[0]?.toUpperCase() ?? "");

/** One confirmation for a quick order before it is submitted; off via "快捷下单免确认". */
export function QuickOrderDialog({ pending, decimals, busy, onClose, onConfirm }: {
  pending: PendingQuickOrder | undefined; decimals: number; busy: boolean; onClose: () => void; onConfirm: (order: PendingQuickOrder) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (pending) ref.current?.showModal(); else ref.current?.close(); }, [pending]);
  useDialogSurface("quick-order-confirm", ref, { closeOnly: true, close: onClose });
  return (
    <dialog ref={ref} className="replay-setup fdw-quick-order-dialog" aria-labelledby="quick-order-title" data-testid="quick-order-dialog"
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
      <div className="replay-setup-body">
      <h2 id="quick-order-title">确认快捷下单</h2>
      {pending?.kind === "orders" ? <ul aria-label="待提交的模拟委托">
        {pending.requests.map((request, index) => <li key={index}>
          <strong>{sideLabel[request.side]}{request.positionBucket === "today" ? "（平今）" : splitByRuntime(pending.symbol, request) ? "（今昨按设置分配）" : ""}</strong>
          <span>{pending.symbol} · {request.volume} 手 · {priceLabel(request, decimals)}</span>
        </li>)}
      </ul> : pending?.kind === "cancel" ? <p>撤销 {pending.symbol} 的 {pending.orders.length} 笔可撤委托</p>
        : pending?.kind === "reverse" ? <p>反手：{pending.symbol} {pending.summary} · 对手价</p> : null}
      <div className="confirm-grid">
        <button type="button" disabled={busy} onClick={onClose}>取消</button>
        <button type="button" className="app-button primary" disabled={busy || !pending} onKeyDown={event => { if (event.key === "Enter" && (event.repeat || event.nativeEvent.isComposing)) event.preventDefault(); }} onClick={() => pending && onConfirm(pending)}>{busy ? "提交中…" : "确认提交"}</button>
      </div>
      </div>
    </dialog>
  );
}
