import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BacktestCard } from "./BacktestCard";

const historical = {
  turnId: "turn-1",
  taskId: "task-1",
  backtestId: "backtest-1",
  artifactDigest: "a".repeat(64),
  strategyDigest: "b".repeat(64),
  dataDigest: "c".repeat(64),
  engineDigest: "d".repeat(64),
  strategyReleaseId: "release-1",
  candidateVariantId: `sha256-${"e".repeat(64)}`,
  candidateConfigDigest: "f".repeat(64),
  candidateParameters: { baselineThreshold: "1.00", candidateThreshold: "0.90" },
  engineVersion: "tqsdk-3.10.2-tqsim-tqbacktest",
  dataSource: "tqsdk:SHFE.rb2610:2026-01-01:2026-03-31:3600",
  generatedAt: "2026-08-31T00:00:00Z",
  reproducible: true as const,
  sourceKind: "tqsdk_history" as const,
  dataPointCount: 480,
  strategyName: "量价趋势研究",
  symbol: "SHFE.rb2610",
  oldVersion: "阈值 100%",
  newVersion: "阈值 90%",
  period: "2026-01-01 至 2026-03-31",
  metrics: [{ label: "累计收益", before: "1.0%", after: "1.5%", tone: "positive" as const }],
  oldCurve: [1_000_000, 1_010_000],
  newCurve: [1_000_000, 1_015_000],
  conclusion: "候选参数优于基线",
  recommendation: "官方 TqSdk 历史回放，仅供研究；不会部署策略或提交账户委托。",
};

describe("BacktestCard", () => {
  it("labels official history and offers no action button", () => {
    render(<BacktestCard backtest={historical} />);
    expect(screen.getByText(/官方 TqSdk 历史数据 480 根/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText(/不会部署策略或提交账户委托/)).toBeInTheDocument();
  });

  it("renders a finite flat chart when there is only one equity point", () => {
    const { container } = render(
      <BacktestCard
        backtest={{ ...historical, oldCurve: [1_000_000], newCurve: [1_000_000] }}
      />
    );
    for (const polyline of container.querySelectorAll("polyline")) {
      expect(polyline.getAttribute("points")).toBe("0.0,118.0 680.0,118.0");
    }
  });
});
