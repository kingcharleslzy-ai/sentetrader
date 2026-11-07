import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { resolveHighlightTarget } from "../aiAnnotations";
import { nextTourStep, prevTourStep, relightTourStep, stopTour, useTour } from "../uiTour";
import "./TourIndicator.css";

interface Spot { left: number; bottom: number }

/**
 * Where the indicator sits so it covers neither the step's target nor the panel the step opened (11-07): centred over
 * the room the side panel leaves, at its usual bottom spot, or just above the target when that spot would cover it
 * (the expanded bottom bar, the AI ball).
 */
function placeIndicator(indicator: HTMLElement, target: Element | null): Spot {
  const width = window.innerWidth, height = window.innerHeight, gap = 12;
  const own = indicator.getBoundingClientRect();
  let from = 0, to = width;
  const side = document.querySelector(".fdw-side-workbench")?.getBoundingClientRect();
  if (side?.width && side.width < width * 0.6) { if (side.left > width / 2) to = side.left; else from = side.right; }
  const half = own.width / 2 + gap;
  const left = to - from >= 2 * half ? Math.min(Math.max((from + to) / 2, from + half), to - half) : width / 2;
  const base = parseFloat(getComputedStyle(indicator).getPropertyValue("--ai-tour-bottom")) || 56;
  const box = target?.getBoundingClientRect();
  const top = height - base - own.height;
  const covers = box && box.width && box.left < left + half && box.right > left - half && box.top < top + own.height + gap && box.bottom > top - gap;
  return { left, bottom: covers ? Math.min(height - box.top + gap, height - own.height - gap) : base };
}

/**
 * The guided tour's step indicator: 第 n/N 步 · the step's label, with 上一步 / 下一步 (or 结束 on the
 * last step) and 结束. Clicking elsewhere only clears the highlight — the tour goes on until 结束.
 */
export function TourIndicator() {
  const tour = useTour();
  const ref = useRef<HTMLDivElement>(null);
  const [spot, setSpot] = useState<Spot | null>(null);
  const target = tour.active ? tour.current?.target : undefined;
  // Followed every frame like the highlight ring: the step's panel opens and the target moves after the step starts.
  useEffect(() => {
    if (!target) return;
    let frame = 0, previous = "";
    const measure = () => {
      if (ref.current) {
        const next = placeIndicator(ref.current, resolveHighlightTarget(target));
        const key = `${Math.round(next.left)},${Math.round(next.bottom)}`;
        if (key !== previous) { previous = key; setSpot(next); }
      }
      frame = requestAnimationFrame(measure);
    };
    frame = requestAnimationFrame(measure);
    return () => cancelAnimationFrame(frame);
  }, [target]);
  if (!tour.active || !tour.current) return null;
  const last = tour.step >= tour.total;
  return (
    <div ref={ref} className="ai-tour-indicator" role="group" aria-label="新手引导" data-agent-target="tour" data-step={tour.step} data-total={tour.total}
      style={spot ? { left: spot.left, bottom: spot.bottom } : undefined}>
      <button type="button" className="ai-tour-step" aria-label="重新高亮当前步骤" data-agent-target="tour:relight" onClick={relightTourStep}>
        <strong>第 {tour.step}/{tour.total} 步</strong><span>{tour.current.label}</span>
      </button>
      <button type="button" aria-label="上一步" data-agent-target="tour:prev" disabled={tour.step <= 1} onClick={() => prevTourStep()}><ChevronLeft /></button>
      <button type="button" aria-label={last ? "完成引导" : "下一步"} data-agent-target="tour:next" onClick={() => nextTourStep()}>{last ? "完成" : <ChevronRight />}</button>
      <button type="button" aria-label="结束引导" data-agent-target="tour:stop" onClick={() => stopTour()}><X /></button>
    </div>
  );
}
