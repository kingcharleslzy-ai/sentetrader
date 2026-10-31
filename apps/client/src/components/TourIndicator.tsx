import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { nextTourStep, prevTourStep, relightTourStep, stopTour, useTour } from "../uiTour";
import "./TourIndicator.css";

/**
 * The guided tour's step indicator: 第 n/N 步 · the step's label, with 上一步 / 下一步 (or 结束 on the
 * last step) and 结束. Clicking elsewhere only closes the bubble — the tour goes on until 结束.
 */
export function TourIndicator() {
  const tour = useTour();
  if (!tour.active || !tour.current) return null;
  const last = tour.step >= tour.total;
  return (
    <div className="ai-tour-indicator" role="group" aria-label="新手引导" data-agent-target="tour" data-step={tour.step} data-total={tour.total}>
      <button type="button" className="ai-tour-step" aria-label="重新高亮当前步骤" data-agent-target="tour:relight" onClick={relightTourStep}>
        <strong>第 {tour.step}/{tour.total} 步</strong><span>{tour.current.label}</span>
      </button>
      <button type="button" aria-label="上一步" data-agent-target="tour:prev" disabled={tour.step <= 1} onClick={() => prevTourStep()}><ChevronLeft /></button>
      <button type="button" aria-label={last ? "完成引导" : "下一步"} data-agent-target="tour:next" onClick={() => nextTourStep()}>{last ? "完成" : <ChevronRight />}</button>
      <button type="button" aria-label="结束引导" data-agent-target="tour:stop" onClick={() => stopTour()}><X /></button>
    </div>
  );
}
