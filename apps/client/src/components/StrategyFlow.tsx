import type { ReactNode } from "react";
import type { FlowBox, FlowTarget, StrategyFlowModel } from "../strategyFlow";
import { formatNumber } from "./futures/support";
import "./StrategyFlow.css";

/**
 * 策略流程图 (策略编辑卡第二版, 2026-10-08; wide.html's right column, the phone's foldable block): the model of
 * strategyFlow.ts drawn top to bottom as plain DOM boxes with CSS connectors — 触发 → 开多 / 开空 → 开仓 → each way out →
 * the notes line. Each box is a button that moves the form to the field it stands for (`onFocus`); a box the form still
 * misses is dashed with 待填. Counts appear only when the model has them (the current form's preview).
 */
export function StrategyFlow({ flow, onFocus, layout = "desktop" }: { flow: StrategyFlowModel; onFocus: (target: FlowTarget) => void; layout?: "desktop" | "mobile" }) {
  const count = (box: FlowBox, unit: string) => box.count == null ? null
    : <span className="sf-count" data-sign={box.net == null ? undefined : box.net > 0 ? "up" : box.net < 0 ? "down" : undefined}>{formatNumber(box.count, 0)} {unit}</span>;
  const button = (box: FlowBox, children: ReactNode, unit = "次") => <button type="button" key={box.id + box.title} className="sf-box" data-side={box.side}
    data-pending={box.pending || undefined} aria-label={`${box.title}${box.pending ? "（待填）" : ""}${box.count != null ? `，${box.count} ${unit}` : ""}：在表单里看`}
    onClick={() => onFocus(box.id)}>
    {children}{count(box, unit)}</button>;
  return <div className="sf" data-layout={layout} role="group" aria-label="策略流程图" data-testid="strategy-flow">
    <div className="sf-stage">{button(flow.trigger, <><b>{flow.trigger.title}</b>{flow.trigger.lines.map(line => <small key={line}>{line}</small>)}</>)}</div>
    <div className="sf-stage sf-entries" data-count={flow.entries.length}>{flow.entries.map(box => button(box, <>
      <b>{box.title}</b>
      {box.pending ? <small>待填</small> : <>{box.lines.map(line => <small key={line}>{line}</small>)}
        {box.rows?.map(row => <span className="sf-row" key={row.text}><span>{row.text}</span>
          {row.count != null ? <span className="sf-row-count">{formatNumber(row.count, 0)} 次</span> : null}</span>)}</>}
    </>))}</div>
    <div className="sf-stage">{button(flow.action, <><b>{flow.action.title}</b>{flow.action.lines.map(line => <small key={line}>{line}</small>)}</>, "笔")}</div>
    <div className="sf-stage sf-exits">{flow.exits.map(box => button(box, <><b>{box.title}</b>{box.pending ? <small>待填</small> : box.lines.map(line => <small key={line}>{line}</small>)}</>))}</div>
    {flow.notes.length ? <button type="button" className="sf-notes" aria-label={`${flow.notes.join("，")}：在表单里看`} onClick={() => onFocus("limits")}>{flow.notes.join(" · ")}</button> : null}
  </div>;
}
