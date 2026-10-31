import { useEffect, useRef } from "react";
import { formatNumber, formatSigned } from "./support";
import { useDialogSurface } from "../../uiSurfaces";

/** A dragged stop / take waiting for its one confirmation (图上拖线改价, 2026-10-24). */
export interface PendingReprice {
  stopId: string;
  level: "stop" | "take";
  from: number;
  to: number;
  volume: number;
  priceIntent: "passive" | "aggressive" | "last";
  /** 触发时盈亏 at the new level against the position's open price; absent when the open price or multiple is unknown. */
  pnl?: number;
}

const INTENT_WORDS: Record<PendingReprice["priceIntent"], string> = { aggressive: "对价", last: "最新价", passive: "排队价" };

/** The chart's confirm card: nothing changes until 确认修改; until then the old level still holds. */
export function ProtectionRepriceDialog({ pending, decimals, busy, onClose, onConfirm }: {
  pending: PendingReprice | undefined; decimals: number; busy: boolean; onClose: () => void; onConfirm: (pending: PendingReprice) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (pending) ref.current?.showModal(); else ref.current?.close(); }, [pending]);
  useDialogSurface("protection-reprice-confirm", ref, { closeOnly: true, close: onClose });
  const word = pending?.level === "take" ? "止盈" : "止损";
  return (
    <dialog ref={ref} className="replay-setup fdw-quick-order-dialog" aria-labelledby="protection-reprice-title" data-testid="protection-reprice-dialog"
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
      <div className="replay-setup-body">
        <h2 id="protection-reprice-title">改{word}</h2>
        {pending ? <ul aria-label={`改${word}`}>
          <li><strong>触发价</strong><span>{formatNumber(pending.from, decimals)} → {formatNumber(pending.to, decimals)}</span></li>
          <li><strong>手数</strong><span>{pending.volume} 手</span></li>
          {pending.pnl !== undefined ? <li><strong>触发时盈亏</strong><span>约 {formatSigned(pending.pnl)} 元（按开仓均价）</span></li> : null}
          <li><strong>触发后</strong><span>按{INTENT_WORDS[pending.priceIntent]}平仓</span></li>
        </ul> : null}
        <div className="confirm-grid">
          <button type="button" disabled={busy} onClick={onClose}>取消</button>
          <button type="button" className="app-button primary" autoFocus disabled={busy || !pending}
            onKeyDown={event => { if (event.key === "Enter" && (event.repeat || event.nativeEvent.isComposing)) event.preventDefault(); }}
            onClick={() => pending && onConfirm(pending)}>{busy ? "提交中…" : "确认修改"}</button>
        </div>
      </div>
    </dialog>
  );
}
