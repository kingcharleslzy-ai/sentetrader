import { useEffect, useMemo, useState } from "react";
import { ChevronLeft } from "lucide-react";
import type { InstrumentCatalog } from "../../api";
import { loadStrategies, launchBlockedReason, periodLabel, strategiesChanged, strategyError, validateAndStart } from "../../strategyApi";
import { chooseLabVariant, getLab, listLabs, rankedResults, type Lab, type LabResult } from "../../strategyLab/labApi";
import { launchDefaults } from "../StrategySignalAction";
import { contractLabel, formatNumber, formatSigned, signClass } from "../futures/support";

/** The equity curve with the 样本内 / 样本外 split as a dashed line (time is the engine's bar clock). */
function EquityCurve({ result }: { result: LabResult }) {
  const points = result.equityCurve ?? [];
  if (points.length < 2) return null;
  const width = 280, height = 90;
  const first = points[0].time, last = points[points.length - 1].time;
  const values = points.map(point => point.equity);
  const low = Math.min(...values), high = Math.max(...values);
  const x = (time: number) => last === first ? 0 : (time - first) / (last - first) * width;
  const y = (value: number) => high === low ? height / 2 : height - 6 - (value - low) / (high - low) * (height - 12);
  const split = result.splitTime != null && result.splitTime > first && result.splitTime < last ? x(result.splitTime) : null;
  return <div className="fdm-curve" role="img" aria-label={`${result.label || result.variantId} 的净值曲线，虚线右边是样本外`}>
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      {split !== null ? <><line x1={split} y1={0} x2={split} y2={height} className="fdm-curve-split" />
        <text x={Math.min(split + 4, width - 34)} y={12} className="fdm-curve-label">样本外</text></> : null}
      <polyline points={points.map(point => `${x(point.time).toFixed(1)},${y(point.equity).toFixed(1)}`).join(" ")} className="fdm-curve-line" />
    </svg>
  </div>;
}

/** A variant's label without the template name every row repeats (「双均线 · 短均线 10 / 长均线 60」). */
const variantLabel = (result: LabResult) => { const label = result.label || result.variantId; return label.includes(" · ") ? label.split(" · ").slice(1).join(" · ") : label; };

/** The lab's 选用 and the launch result this page reports (the `mobileLab` snapshot). */
export interface MobileLabReport { labId: string; chosen: string | null }

/**
 * 回测结果页 (手机版新结构, 2026-10-06; `mobilePage: lab`, `mobileLab: {labId}`): one comparison of
 * 策略 → 最近的回测 (the latest without an id) — 合约 · 周期, the picked variant's equity curve with the
 * 样本内 / 样本外 split, every variant's 净值 / 样本内 / 样本外 (过拟合风险 is the lab's own `overfit`
 * flag), 选用这一组 (`POST …/choose`, the server re-runs it before saving) and 上线运行: the desktop
 * dialog's own checks (AI 模拟执行 stopped refuses, then validate + start), the backtest's contract,
 * and 执行方式 fixed to 出卡确认 — the phone never offers 自动下单.
 */
export function MobileLab({ rootUrl, baseUrl, labId, instruments, chartTimeframe, sessionId, onClose, onAskAi, onReport }: {
  rootUrl?: string;
  baseUrl?: string;
  /** The comparison to show; null = the latest one. */
  labId: string | null;
  instruments?: InstrumentCatalog;
  /** A follow-chart plan's period when its variant names none (the chart's). */
  chartTimeframe: number;
  /** The conversation the launched instance sends its cards to. */
  sessionId?: string;
  onClose: () => void;
  onAskAi: (text: string) => void;
  onReport: (report: MobileLabReport | null) => void;
}) {
  const [lab, setLab] = useState<Lab | null>(null);
  const [error, setError] = useState("");
  const [pick, setPick] = useState<string | null>(null);
  const [busy, setBusy] = useState<"" | "choose" | "launch">("");
  const [notice, setNotice] = useState("");
  const [launched, setLaunched] = useState(false);
  useEffect(() => {
    let alive = true;
    setLab(null); setError(""); setPick(null); setNotice(""); setLaunched(false);
    if (!rootUrl) { setError("连接后可看回测结果。"); return; }
    void (async () => {
      try {
        const id = labId ?? (await listLabs(rootUrl, 1)).items?.[0]?.id;
        if (!id) { if (alive) setError("最近还没有回测比较。可以跟 AI 说一句让它帮你跑。"); return; }
        const next = await getLab(rootUrl, id);
        if (alive) setLab(next);
      } catch { if (alive) setError("这次回测没有读到，稍后再试。"); }
    })();
    return () => { alive = false; };
  }, [rootUrl, labId]);
  const rows = useMemo(() => rankedResults(lab), [lab]);
  const picked = rows.find(row => row.result.variantId === (pick ?? lab?.chosen?.variantId ?? lab?.ranking?.best)) ?? rows.find(row => !row.result.error);
  const chosen = lab?.chosen?.variantId ?? null;
  useEffect(() => { onReport(lab ? { labId: lab.id, chosen } : labId ? { labId, chosen: null } : null); }, [lab, labId, chosen, onReport]);
  useEffect(() => () => onReport(null), [onReport]);
  const variant = lab?.variants.find(item => item.id === picked?.result.variantId);
  const name = (lab?.variants[0]?.label ?? "").split(" · ")[0].trim() || "策略比较";
  const period = variant?.timeframe || Number((variant?.plan as { timeframeSeconds?: number } | undefined)?.timeframeSeconds) || null;
  const choose = async () => {
    if (!rootUrl || !lab || !picked || busy) return;
    setBusy("choose"); setNotice("");
    try {
      await chooseLabVariant(rootUrl, lab.id, picked.result.variantId);
      setLab(await getLab(rootUrl, lab.id));
    } catch (failure) { setNotice(strategyError(failure, "没有存下这一组，请重试")); }
    finally { setBusy(""); }
  };
  const launch = async () => {
    if (!baseUrl || !lab?.chosen || busy || launched) return;
    setBusy("launch"); setNotice("");
    try {
      const blocked = await launchBlockedReason(baseUrl);
      if (blocked) { setNotice(blocked); return; }
      const item = (await loadStrategies(baseUrl)).items.find(entry => entry.strategyId === lab.chosen!.strategyId);
      const symbol = (item ? launchDefaults(item, lab.symbol).symbol : "") || lab.symbol;
      await validateAndStart(baseUrl, lab.chosen.strategyId, symbol, period || chartTimeframe, undefined,
        { signalAction: "confirm", ...(sessionId ? { sessionId } : {}) });
      setLaunched(true);
      setNotice("已上线：出卡确认，信号来了先出卡，你点确认才下单。");
      void strategiesChanged();
    } catch (failure) { setNotice(strategyError(failure, "验证未完成，请检查规则与行情连接后重试。")); }
    finally { setBusy(""); }
  };
  const segment = (value: number | undefined | null) => value == null ? <td>—</td> : <td className={signClass(value)}>{formatSigned(value, 0)}</td>;
  const summary = picked?.result.summary as { netProfit?: number; maxDrawdown?: number; trades?: number } | null | undefined;
  return (
    <section className="fdm-overlay fdm-page fdm-lab" aria-label="回测结果" data-testid="mobile-lab" data-agent-target="mobile-lab">
      <header className="fdm-head"><button type="button" className="fdm-back" aria-label="返回策略" onClick={onClose}><ChevronLeft /></button>
        <strong>{lab ? `${name} · 回测` : "回测结果"}</strong></header>
      <main className="fdm-content">
        {error ? <p className="fdm-note" role="status">{error}</p> : !lab ? <p className="fdm-note" role="status">正在读取这次回测…</p> : <>
          <p className="fdm-lab-meta"><span>{contractLabel(lab.symbol, instruments)}{period ? ` · ${periodLabel(period)}` : ""}</span><span>前 70% 样本内 · 后 30% 样本外</span></p>
          {lab.status !== "done" ? <p className="fdm-note" role="status">{lab.status === "running" ? "云端还在算，算完再来看。" : lab.status === "error" ? "这次比较出错了，可以让 AI 再跑一次。" : "这次比较还没算。"}</p> : <>
            {picked ? <EquityCurve result={picked.result} /> : null}
            <table className="fdm-lab-table" aria-label="各组参数的净值、样本内与样本外">
              <thead><tr><th>参数</th><th>净值</th><th>样本内</th><th>样本外</th></tr></thead>
              <tbody>
                {rows.map(row => (
                  <tr key={row.result.variantId} aria-selected={row === picked} data-picked={row === picked ? "" : undefined}
                    onClick={() => !row.result.error && setPick(row.result.variantId)}>
                    <th scope="row"><button type="button" disabled={Boolean(row.result.error)} onClick={() => setPick(row.result.variantId)}>{variantLabel(row.result)}</button>
                      {row.flags.includes("overfit") ? <small className="fdm-lab-warn">过拟合风险</small> : null}
                      {row.result.error ? <small>{row.result.error}</small> : null}</th>
                    {segment((row.result.summary as { netProfit?: number } | null | undefined)?.netProfit)}
                    {segment(row.result.inSample?.net)}
                    {segment(row.result.outSample?.net)}
                  </tr>
                ))}
              </tbody>
            </table>
            {lab.ranking?.noProfit ? <p className="fdm-note">这几组在样本内都没赚钱，没有推荐的一组。</p> : null}
            {picked && !picked.result.error ? <article className="fdm-strat fdm-lab-pick">
              <div className="fdm-strat-head"><strong>{variantLabel(picked.result)}</strong>
                {chosen === picked.result.variantId ? <em className="fdm-tag" data-tone="accent">已选用</em> : null}</div>
              <p>最大回撤 {formatNumber(summary?.maxDrawdown, 0)}，交易 {summary?.trades ?? picked.result.tradesUsed ?? 0} 次，
                {(picked.result.outSample?.net ?? 0) > 0 ? "样本外仍为正。" : "样本外没有赚钱。"}</p>
              <div className="fdm-strat-actions">
                {chosen === picked.result.variantId
                  ? <button type="button" className="primary" disabled={Boolean(busy) || launched} onClick={() => void launch()}>{busy === "launch" ? "正在验证并上线…" : launched ? "已上线" : "上线运行"}</button>
                  : <button type="button" className="primary" disabled={Boolean(busy)} onClick={() => void choose()}>{busy === "choose" ? "正在复核并保存…" : "选用这一组"}</button>}
                <button type="button" onClick={() => onAskAi(`${name}这次回测（${contractLabel(lab.symbol, instruments)}，${lab.variants.length} 组参数）里「${variantLabel(picked.result)}」怎么样？`)}>问 AI</button>
              </div>
              {notice ? <p className={launched ? "fdm-note" : "fdm-strat-error"} role={launched ? "status" : "alert"}>{notice}</p> : null}
              <small className="fdm-lab-hint">手机上线固定「出卡确认」：信号来了先出卡，你点确认才下单；要自动下单回电脑选或跟 AI 说。</small>
            </article> : null}
          </>}
        </>}
      </main>
    </section>
  );
}
