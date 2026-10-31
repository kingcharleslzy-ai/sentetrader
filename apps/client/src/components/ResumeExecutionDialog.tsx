import { useEffect, useRef } from "react";

export function ResumeExecutionDialog({open, busy, onClose, onConfirm, action = "resume", pausedStrategies = [], onOpenStrategy}: {
  action?:"resume"|"stop"; open:boolean; busy:boolean; onClose:()=>void; onConfirm:()=>void;
  /** Strategies the stop paused; shown on the resume dialog with a jump to the strategy page. */
  pausedStrategies?: {id: string; name?: string}[];
  onOpenStrategy?: (id: string) => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (open) ref.current?.showModal(); else ref.current?.close(); }, [open]);
  const stop = action === "stop";
  const titleId = `${action}-execution-title`;
  return <dialog ref={ref} className="replay-setup" aria-labelledby={titleId} onCancel={event => {event.preventDefault(); if (!busy) onClose();}}>
    <h2 id={titleId}>{stop ? "全局急停 AI 模拟执行" : "恢复 AI 模拟执行"}</h2>
    <p>{stop ? "停止所有策略及 AI 模拟执行，尝试撤销 AI 未完成委托，并取消自动止损保护。" : "解除全局停止后，AI 可以再次执行模拟交易。沿用已有模拟限额，实盘永久关闭。"}</p>
    <p>{stop ? "不会自动平仓，已有持仓仍需处理。仅暂停某条策略，请使用策略页的暂停监控。" : "已暂停的策略需要单独启动；旧委托不会补发，已取消的保护单不会恢复。"}</p>
    {!stop && pausedStrategies.length ? <ul className="paused-strategies" aria-label="急停时暂停的策略">
      {pausedStrategies.map(item => <li key={item.id}><span>{item.name ?? item.id.slice(0, 8)}</span>{onOpenStrategy ? <button type="button" disabled={busy} onClick={() => onOpenStrategy(item.id)}>去启动</button> : null}</li>)}
    </ul> : null}
    <div className="confirm-grid"><button type="button" disabled={busy} onClick={onClose}>{stop ? "取消" : "暂不恢复"}</button><button type="button" className={stop ? "app-button danger" : "app-button primary"} disabled={busy} onClick={onConfirm}>{busy ? "正在确认执行状态…" : stop ? "确认全局急停" : "确认恢复模拟执行"}</button></div>
  </dialog>;
}
