import { ChartSpline } from "lucide-react";
import type { BacktestComparison } from "@ai-trader/ui-events";
import { CardShell } from "../CardShell";

function chartPoints(values: number[]) {
  const points = values.length === 1 ? [values[0], values[0]] : values;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  return points
    .map((value, index) => {
      const x = (index / (points.length - 1)) * 680;
      const y = 118 - ((value - min) / range) * 105;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

interface BacktestCardProps {
  backtest: BacktestComparison;
  id?: string;
}

export function BacktestCard({ backtest, id }: BacktestCardProps) {
  return (
    <CardShell
      icon={<ChartSpline />}
      title="回测对比"
      badge="已完成"
      badgeTone="positive"
      id={id}
      testId="backtest-card"
      details={
        <>
          <dl className="card-facts">
            <div><dt>范围</dt><dd>{backtest.symbol} · {backtest.oldVersion} / {backtest.newVersion} · {backtest.period}</dd></div>
            <div><dt>建议</dt><dd>{backtest.recommendation}</dd></div>
          </dl>
          <div className="chart-panel">
            <svg viewBox="0 0 680 130" preserveAspectRatio="none" role="img" aria-label="新旧策略权益曲线">
              <polyline points={chartPoints(backtest.oldCurve)} fill="none" stroke="var(--muted)" strokeWidth="2" strokeDasharray="5 5" />
              <polyline points={chartPoints(backtest.newCurve)} fill="none" stroke="var(--accent)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <div className="chart-legend">
              <span className="legend-key"><i />旧版</span>
              <span className="legend-key new"><i />新版</span>
            </div>
          </div>
          <p className="audit-note" data-testid="backtest-provenance">
            {backtest.sourceKind === "tqsdk_history"
              ? `官方 TqSdk 历史数据 ${backtest.dataPointCount ?? 0} 根 · TqSim 本地撮合 · 制品 ${backtest.artifactDigest.slice(0, 12)}`
              : `可复现制品 ${backtest.artifactDigest.slice(0, 12)} · 候选配置 ${backtest.candidateConfigDigest.slice(0, 10)} · 策略 ${backtest.strategyDigest.slice(0, 10)} · 数据 ${backtest.dataDigest.slice(0, 10)} · 引擎 ${backtest.engineDigest.slice(0, 10)}`}
          </p>
        </>
      }
    >
      <div className="comparison-grid">
        {backtest.metrics.map((metric) => (
          <div className="compare-item" key={metric.label}>
            <span>{metric.label}</span>
            <div className="compare-values">
              <em>{metric.before}</em><span>→</span>
              <strong className={metric.tone === "positive" ? "positive-text" : metric.tone === "warning" ? "warning-text" : ""}>
                {metric.after}
              </strong>
            </div>
          </div>
        ))}
      </div>
      <p className="card-line">{backtest.conclusion}</p>
    </CardShell>
  );
}
