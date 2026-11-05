import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ChevronDown } from "lucide-react";
import type { MarketWorkbenchSnapshot } from "../../api";
import { ChartPopover } from "../chartIndicatorEditor";
import { useDisclosureSurface } from "../../uiSurfaces";
import { periods } from "./support";
import { Presence } from "../motion";

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

interface Props {
  chartView: "kline" | "timeshare";
  duration: number;
  onSelectDuration: (duration: Duration) => void;
  onSelectChartView: (view: "kline" | "timeshare") => void;
  /** The bar's fit level folded the uncommon periods (MarketCommand's useFitLevel; the class on the bar hides them). */
  folded: boolean;
  /** 多图 / 铃铛 the bar folded too: shown in the same popover under the periods. */
  tools?: ReactNode;
}

/** The K 线周期 row: 分时 at the head, then the periods; folded ones open from “更多”. */
export function PeriodSwitch({ chartView, duration, onSelectDuration, onSelectChartView, folded: collapsed, tools }: Props) {
  const moreRef = useRef<HTMLButtonElement>(null);
  const [popover, setPopover] = useState<{ rect: DOMRect }>();
  const openMore = () => setPopover({ rect: moreRef.current?.getBoundingClientRect() ?? document.body.getBoundingClientRect() });
  // Folded, the disclosure is the popover; when the full row fits every period is already on it, so
  // open / close just toggle the hook's own state and the command lands either way (owner 2026-11-02).
  const [, setUnfoldedOpen] = useDisclosureSurface("chart-periods-more", collapsed ? {
    state: popover !== undefined,
    apply: open => (open ? openMore() : setPopover(undefined))
  } : {});
  // A bar that widens again shows every period: the popover has nothing left to offer.
  useEffect(() => { if (!collapsed) setPopover(undefined); setUnfoldedOpen(false); }, [collapsed, setUnfoldedOpen]);
  const face = moreButtonFace(chartView, duration);
  const choose = (value: Duration) => { setPopover(undefined); onSelectDuration(value); };
  const isActive = (value: number) => chartView === "kline" && duration === value;
  return (
    <div role="group" aria-label="K线周期" data-agent-target="market:periods">
      <button type="button" className={chartView === "timeshare" ? "active" : ""} onClick={() => onSelectChartView("timeshare")}>分时</button>
      {periods.map(period => (
        <button type="button" key={period.value} onClick={() => onSelectDuration(period.value)}
          className={[isActive(period.value) ? "active" : "", PINNED_DURATIONS.has(period.value) ? "" : "fdw-period-extra"].filter(Boolean).join(" ")}>{period.label}</button>
      ))}
      {/* Always mounted so the bar's fit pass measures it; the fold class shows it, aria-hidden mirrors the settled level. */}
      <button type="button" ref={moreRef} className={`fdw-period-more${face.active ? " active" : ""}`} aria-haspopup="dialog" aria-expanded={popover !== undefined}
        aria-hidden={!collapsed} tabIndex={collapsed ? undefined : -1}
        aria-label={face.active ? `更多周期（当前 ${face.label}）` : "更多周期"} onClick={openMore}>{face.label}<ChevronDown aria-hidden="true" /></button>
      {/* Portaled out of `.fdw-chart-tools` (overflow: hidden), like the alerts popover. */}
      <Presence>{popover && collapsed ? createPortal(
        <ChartPopover title={tools ? "更多" : "更多周期"} anchorRect={popover.rect} onClose={() => setPopover(undefined)} width={220}>
          <div className="fdw-period-more-list">
            {morePeriods.map(period => (
              <button type="button" key={period.value} className={isActive(period.value) ? "active" : ""} aria-pressed={isActive(period.value)}
                onClick={() => choose(period.value)}>{period.label}</button>
            ))}
          </div>
          {/* A tool that was used (a layout picked, the 提醒 panel opened) closes the menu, like a period does. */}
          {tools ? <div className="fdw-chart-tools fdw-period-more-tools" onClick={event => { if ((event.target as Element).closest("button")) setPopover(undefined); }}>{tools}</div> : null}
        </ChartPopover>, document.body) : null}</Presence>
    </div>
  );
}
