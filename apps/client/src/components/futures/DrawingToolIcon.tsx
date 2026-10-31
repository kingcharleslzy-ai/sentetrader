import type { DrawingKind } from "../../chartDrawingModel";

/** 画线第二版: one small line icon per tool (18 px, currentColor), drawn to look like the tool's result. */
const PATHS: Record<DrawingKind, string> = {
  trendline: "M3 14 15 4 M3 14h0 M15 4h0",
  ray: "M3 14 16 3.5 M3 14h0",
  extended: "M1 15.5 17 2.5",
  hline: "M2 9h14",
  hray: "M4 9h12 M4 9h0",
  vline: "M9 2v14",
  channel: "M2 12 14 4 M4 15 16 7",
  polyline: "M2 14 6 7l4 5 6-9",
  zone: "M2 6h14 M2 12h14",
  rect_zone: "M3 5h12v8H3z",
  fib: "M2 3h14 M2 7h14 M2 10h14 M2 15h14",
  percent: "M2 3h14 M2 6.5h14 M2 10h14 M2 13.5h14",
  gann: "M2 3h14 M2 5h14 M2 7h14 M2 9h14 M2 11h14 M2 13h14 M2 15h14",
  rect: "M3 4h12v10H3z",
  ellipse: "M9 4c3.9 0 7 2.2 7 5s-3.1 5-7 5-7-2.2-7-5 3.1-5 7-5z",
  triangle: "M9 3 16 15H2z",
  text: "M4 4h10 M9 4v11",
  arrow: "M3 15 14 4 M8 4h6v6",
  price_label: "M2 9l4-4h10v8H6z",
  measure: "M9 2v14 M6 5l3-3 3 3 M6 13l3 3 3-3",
};

export function DrawingToolIcon({ kind }: { kind: DrawingKind }) {
  const dots = kind === "trendline" ? [[3, 14], [15, 4]] : kind === "ray" ? [[3, 14]] : kind === "hray" ? [[4, 9]] : [];
  return (
    <svg className="fdw-draw-icon" width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor"
      strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[kind]} />
      {dots.map(([cx, cy]) => <circle key={`${cx}:${cy}`} cx={cx} cy={cy} r="1.6" fill="currentColor" stroke="none" />)}
    </svg>
  );
}
