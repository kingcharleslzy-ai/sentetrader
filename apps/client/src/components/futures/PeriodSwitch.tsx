import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { ChevronDown } from "lucide-react";
import type { MarketWorkbenchSnapshot } from "../../api";
import { ChartPopover } from "../chartIndicatorEditor";
import { useDisclosureSurface } from "../../uiSurfaces";
import { periods } from "./support";

type Duration = MarketWorkbenchSnapshot["durationSeconds"];

/** 周期收起 (2026-10-03): 分时 plus these stay on the row at every width; the others fold into
 *  “更多” when the full row does not fit the bar. */
const PINNED_DURATIONS = new Set<number>([60, 300, 900, 86400]);
export const morePeriods = periods.filter(period => !PINNED_DURATIONS.has(period.value));

/** The 更多 button's face: the selected period when it is a folded one, so the choice stays visible. */
export function moreButtonFace(chartView: "kline" | "timeshare", duration: number): { label: string; active: boolean } {
  const folded = chartView === "kline" ? morePeriods.find(period => period.value === duration) : undefined;
  return folded ? { label: folded.label, active: true } : { label: "更多", active: false };
}

const COLLAPSED = "fdw-periods-collapsed";

/** One pass decides from the full row: the fold class comes off (every period shown, 更多 hidden),
 *  the group is measured, and the class goes back on only if the row overflows — so the result
 *  never depends on the previous state and cannot oscillate. */
export function measurePeriodFold(group: HTMLElement): boolean {
  group.classList.remove(COLLAPSED);
  const collapsed = group.scrollWidth - group.clientWidth > 1;
  group.classList.toggle(COLLAPSED, collapsed);
  return collapsed;
}

/** Re-measures when the bar or the group resizes (a side panel, the window, font preferences)
 *  and when the active period changes (its label is bold). */
function usePeriodFold(group: RefObject<HTMLElement | null>, contentKey: string): boolean {
  const [collapsed, setCollapsed] = useState(false);
  const measure = useCallback(() => {
    if (group.current) setCollapsed(measurePeriodFold(group.current));
  }, [group]);
  useLayoutEffect(() => { measure(); }, [contentKey, measure]);
  useLayoutEffect(() => {
    const node = group.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      if (frame) return;
      frame = requestAnimationFrame(() => { frame = 0; measure(); });
    });
    observer.observe(node);
    const bar = node.closest(".fdw-market-command");
    if (bar) observer.observe(bar);
    return () => { observer.disconnect(); if (frame) cancelAnimationFrame(frame); };
  }, [group, measure]);
  return collapsed;
}

interface Props {
  chartView: "kline" | "timeshare";
  duration: number;
  onSelectDuration: (duration: Duration) => void;
  onSelectChartView: (view: "kline" | "timeshare") => void;
}

/** The K 线周期 row: 分时 at the head, then the periods; folded ones open from “更多”. */
export function PeriodSwitch({ chartView, duration, onSelectDuration, onSelectChartView }: Props) {
  const groupRef = useRef<HTMLDivElement>(null);
  const moreRef = useRef<HTMLButtonElement>(null);
  const collapsed = usePeriodFold(groupRef, `${chartView}:${duration}`);
  const [popover, setPopover] = useState<{ rect: DOMRect }>();
  const openMore = () => setPopover({ rect: moreRef.current?.getBoundingClientRect() ?? document.body.getBoundingClientRect() });
  useDisclosureSurface("chart-periods-more", {
    state: popover !== undefined, available: collapsed,
    apply: open => (open ? openMore() : setPopover(undefined))
  });
  // A bar that widens again shows every period: the popover has nothing left to offer.
  useEffect(() => { if (!collapsed) setPopover(undefined); }, [collapsed]);
  const face = moreButtonFace(chartView, duration);
  const choose = (value: Duration) => { setPopover(undefined); onSelectDuration(value); };
  const isActive = (value: number) => chartView === "kline" && duration === value;
  return (
    <div role="group" aria-label="K线周期" data-agent-target="market:periods" ref={groupRef} className={collapsed ? COLLAPSED : undefined}>
      <button type="button" className={chartView === "timeshare" ? "active" : ""} onClick={() => onSelectChartView("timeshare")}>分时</button>
      {periods.map(period => (
        <button type="button" key={period.value} onClick={() => onSelectDuration(period.value)}
          className={[isActive(period.value) ? "active" : "", PINNED_DURATIONS.has(period.value) ? "" : "fdw-period-extra"].filter(Boolean).join(" ")}>{period.label}</button>
      ))}
      {collapsed ? (
        <button type="button" ref={moreRef} className={`fdw-period-more${face.active ? " active" : ""}`} aria-haspopup="dialog" aria-expanded={popover !== undefined}
          aria-label={face.active ? `更多周期（当前 ${face.label}）` : "更多周期"} onClick={openMore}>{face.label}<ChevronDown aria-hidden="true" /></button>
      ) : null}
      {/* Portaled out of `.fdw-chart-tools` (overflow: hidden), like the alerts popover. */}
      {popover && collapsed ? createPortal(
        <ChartPopover title="更多周期" anchorRect={popover.rect} onClose={() => setPopover(undefined)} width={220}>
          <div className="fdw-period-more-list">
            {morePeriods.map(period => (
              <button type="button" key={period.value} className={isActive(period.value) ? "active" : ""} aria-pressed={isActive(period.value)}
                onClick={() => choose(period.value)}>{period.label}</button>
            ))}
          </div>
        </ChartPopover>, document.body) : null}
    </div>
  );
}
