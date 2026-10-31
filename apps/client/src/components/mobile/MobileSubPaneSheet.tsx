import { useDisclosureSurface } from "../../uiSurfaces";
import { HelpTip } from "../Blocks";
import { MaLinesEditor, type ChartIndicatorPatch } from "../chartIndicatorEditor";
import { SUB_PANE_TYPES, SUB_PANES_MAX, type ChartIndicatorConfig, type SubPaneType } from "../chartIndicators";
import { Stepper } from "../settings/controls";
import { subPaneName, toggleSubPane } from "./mobileMarketPrefs";

type MainOverlay = "ma" | "boll" | "sar";

/**
 * 副图设置 (手机看盘, 2026-10-09): opened by ⚙ beside the periods or by holding the sub pane. Chooses 0–3 sub panes
 * (the account's chart.subPaneList — the computer shows the same list, all at once), the price pane's overlays
 * (均线 / 布林 / SAR, or 无) and each chosen indicator's parameters. One configuration with the computer: every edit
 * goes through the same account preference path as the desktop chart labels and 设置 → 图表.
 */
export function MobileSubPaneSheet({ indicators, onChange, onClose, status }: {
  indicators: ChartIndicatorConfig;
  /** Undefined while a member window is disconnected: the sheet reads, it does not write. */
  onChange: ((patch: ChartIndicatorPatch) => void) | undefined;
  onClose: () => void;
  status?: string;
}) {
  const panes = indicators.subPaneList;
  const [overlayParams, setOverlayParams] = useDisclosureSurface("mobile-subpanes-overlays");
  const change = (patch: ChartIndicatorPatch) => onChange?.(patch);
  const maOn = indicators.maLines.some(line => line.enabled);
  const overlays: Record<MainOverlay, boolean> = { ma: maOn, boll: indicators.boll.enabled, sar: indicators.sar.enabled };
  // The 均线 switch remembers which lines were on (chart.maMuted) when it turns them all off, and restores exactly
  // those when it turns back on (every line when nothing was remembered).
  const maOff = () => ({ maLines: indicators.maLines.map(line => ({ ...line, enabled: false })),
    maMuted: maOn ? indicators.maLines.filter(line => line.enabled).map(line => line.period) : indicators.maMuted });
  const maOnAgain = () => {
    const kept = indicators.maMuted.filter(period => indicators.maLines.some(line => line.period === period));
    return { maLines: indicators.maLines.map(line => ({ ...line, enabled: kept.length ? kept.includes(line.period) : true })), maMuted: [] };
  };
  const setOverlay = (overlay: MainOverlay, on: boolean) => {
    if (overlay === "ma") change(on ? maOnAgain() : maOff());
    if (overlay === "boll") change({ boll: { ...indicators.boll, enabled: on } });
    if (overlay === "sar") change({ sar: { ...indicators.sar, enabled: on } });
  };
  const none = !maOn && !indicators.boll.enabled && !indicators.sar.enabled;
  const clearOverlays = () => change({ ...maOff(),
    boll: { ...indicators.boll, enabled: false }, sar: { ...indicators.sar, enabled: false } });
  const kd = indicators.kdParams, macd = indicators.macdParams;
  const step = (label: string, value: number, min: number, max: number, apply: (value: number) => void) =>
    <label className="fdm-mm-param"><span>{label}</span><Stepper label={label} value={value} min={min} max={max} disabled={!onChange}
      onChange={next => { if (next != null) apply(Math.round(next)); }} /></label>;
  return (
    <div className="fdm-sheet-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="fdm-sheet fdm-mm-sheet" role="dialog" aria-label="副图设置" data-agent-target="mobile-subpanes">
        <div className="fdm-sheet-grip" aria-hidden="true" />
        <header className="fdm-sheet-head">
          <span><strong>副图（最多 {SUB_PANES_MAX} 个）<HelpTip helpKey="mobile-subpanes" label="副图设置">
            单图下面一次显示一个副图：在副图上左右滑，在选中的几个之间切换；长按副图打开这里。电脑上同一份设置，选中的副图一起显示。
          </HelpTip></strong></span>
          <button type="button" className="fdm-sheet-done primary-button" onClick={onClose}>完成</button>
        </header>
        <div className="fdm-sheet-body">
          <div className="fdm-mm-grid" role="group" aria-label="副图指标">
            {SUB_PANE_TYPES.map(type => {
              const at = panes.indexOf(type);
              const full = at < 0 && panes.length >= SUB_PANES_MAX;
              return <button type="button" key={type} aria-pressed={at >= 0} disabled={!onChange || full}
                title={full ? `最多 ${SUB_PANES_MAX} 个副图，先关掉一个` : undefined}
                onClick={() => change({ subPaneList: toggleSubPane(panes, type as SubPaneType) })}>
                {subPaneName(type)}{at >= 0 ? <small>{at + 1}</small> : null}
              </button>;
            })}
          </div>
          <h4 className="fdm-mm-label">主图</h4>
          <div className="fdm-mm-grid fdm-mm-main" role="group" aria-label="主图指标">
            {([["ma", "均线"], ["boll", "布林"], ["sar", "SAR"]] as const).map(([overlay, label]) =>
              <button type="button" key={overlay} aria-pressed={overlays[overlay]} disabled={!onChange} onClick={() => setOverlay(overlay, !overlays[overlay])}>{label}</button>)}
            <button type="button" aria-pressed={none} disabled={!onChange || none} onClick={clearOverlays}>无</button>
          </div>
          <h4 className="fdm-mm-label">参数</h4>
          <div className="fdm-mm-params">
            {panes.includes("kd") ? <div className="fdm-mm-param-row"><b>KDJ</b>
              {step("KD 周期", kd.k, 2, 100, k => change({ kdParams: { ...kd, k } }))}
              {step("KD 平滑 K", kd.d, 1, 30, d => change({ kdParams: { ...kd, d } }))}
              {step("KD 平滑 D", kd.smooth, 1, 30, smooth => change({ kdParams: { ...kd, smooth } }))}</div> : null}
            {panes.includes("macd") ? <div className="fdm-mm-param-row"><b>MACD</b>
              {step("MACD 快线", macd.fast, 2, 100, fast => change({ macdParams: { ...macd, fast } }))}
              {step("MACD 慢线", macd.slow, 3, 200, slow => change({ macdParams: { ...macd, slow } }))}
              {step("MACD 信号", macd.signal, 1, 100, signal => change({ macdParams: { ...macd, signal } }))}</div> : null}
            {panes.includes("rsi") ? <div className="fdm-mm-param-row"><b>RSI</b>
              {step("RSI 周期", indicators.rsiParams.period, 2, 100, period => change({ rsiParams: { period } }))}</div> : null}
            {panes.includes("cci") ? <div className="fdm-mm-param-row"><b>CCI</b>
              {step("CCI 周期", indicators.cciParams.period, 2, 100, period => change({ cciParams: { period } }))}</div> : null}
            <button type="button" className="fdm-mm-fold" aria-expanded={overlayParams} onClick={() => setOverlayParams(!overlayParams)}>
              均线、布林与 SAR 参数</button>
            {overlayParams ? <MaLinesEditor indicators={indicators} onChange={change} /> : null}
          </div>
          {status ? <p className="fdm-note" role="status">{status}</p> : null}
        </div>
      </section>
    </div>
  );
}
