import type { ProtectionBucketLevels, RemainingProtection, RemainingProtectionLevel } from "@ai-trader/ui-events";
import "./StructuredCards.css";

/** "81,420" — a protection price as the card says it (no trailing zeros, thousands separated). */
export function priceWords(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN", { maximumFractionDigits: 4 }) : String(value);
}

export interface CardStep {
  title: string;
  detail?: string;
  /** The step's own outcome on the right (已成交 / 已改 / 未改); absent while it waits. */
  outcome?: { label: string; tone: "ok" | "fail" | "wait" };
  /** The stop step is drawn in the chart's stop colour (样稿: 剩下 1 手止损 → …). */
  stop?: boolean;
}

/** 合并卡 / 保本提醒: the numbered steps of one card (样稿 steps2). */
export function CardSteps({ steps, label }: { steps: CardStep[]; label: string }) {
  return (
    <ol className="sc-steps" aria-label={label}>
      {steps.map((step, index) => (
        <li key={index} data-testid="card-step">
          <span className="sc-step-n" aria-hidden="true">{index + 1}</span>
          <span className="sc-step-text">
            <b className={step.stop ? "sc-step-stop" : undefined}>{step.title}</b>
            {step.detail ? <small>{step.detail}</small> : null}
          </span>
          {step.outcome ? <span className="sc-step-outcome" data-tone={step.outcome.tone}>{step.outcome.label}</span> : <span />}
        </li>
      ))}
    </ol>
  );
}

const levelText = (word: string, level: RemainingProtectionLevel) =>
  level.action === "set" ? `${word} → ${priceWords(level.price)}${level.atCost ? "（成本）" : ""}`
    : level.action === "remove" ? `撤掉${word}` : "";

type LevelKey = "stopLoss" | "takeProfit";
const BUCKET_WORDS: Record<ProtectionBucketLevels["positionBucket"], string> = { today: "今仓", historical: "昨仓", all: "持仓" };
const BUCKET_ORDER = ["today", "historical", "all"] as const;

/** "今仓 825、昨仓 830" — one level bucket by bucket, only when the buckets' prices differ (the scalar is
 *  then null). It never borrows another bucket's price. A kept level's null is proven none (未挂); for a
 *  set / remove level a null bucket price only means it can't be stated, so `setOnly` leaves it out. */
function bucketWords<Row extends ProtectionBucketLevels>(rows: Row[] | null | undefined, field: (row: Row) => string | null,
  { lots = false, setOnly = false } = {}): string {
  const sorted = [...(rows ?? [])].filter(row => !lots || row.volume > 0)
    .sort((a, b) => BUCKET_ORDER.indexOf(a.positionBucket) - BUCKET_ORDER.indexOf(b.positionBucket));
  if (sorted.length < 2 || !sorted.some(field) || new Set(sorted.map(field)).size < 2) return "";
  return sorted.filter(row => !setOnly || field(row)).map(row => {
    const head = `${BUCKET_WORDS[row.positionBucket]}${lots ? ` ${row.volume} 手` : ""}`;
    return field(row) ? `${head} ${priceWords(field(row))}` : `${head}未挂`;
  }).join("、");
}

/** The second step of a merged close card, as it reads before and after it ran. */
export function remainingStep(plan: RemainingProtection): CardStep {
  const result = plan.result ?? {};
  const lots = `剩下 ${result.remainingVolume ?? plan.remainingVolume} 手`;
  const changes = [levelText("止损", plan.stopLoss), levelText("止盈", plan.takeProfit)].filter(Boolean);
  const stopChanged = plan.stopLoss.action !== "keep";
  const takeChanged = plan.takeProfit.action !== "keep";
  if (plan.status === "done") {
    // 已撤 / 未动 come only from what this run replaced (result), never from the card-time snapshot.
    const applied = result.perBucketProtection;
    const replaced = (key: LevelKey, word: string, scalar: string | null | undefined) => {
      if (scalar) return `原${word} ${priceWords(scalar)}`;
      const each = bucketWords(applied, row => key === "stopLoss" ? row.previousStopLoss : row.previousTakeProfit, { setOnly: true });
      return each ? `原${word} ${each}` : "";
    };
    // A removed level says 已撤 only with an actual previous price; otherwise it states what stands now.
    const hadOld = (key: LevelKey, scalar: string | null | undefined) => Boolean(scalar)
      || Boolean(applied?.some(row => key === "stopLoss" ? row.previousStopLoss : row.previousTakeProfit));
    const now = [result.stopLoss ? `止损 ${priceWords(result.stopLoss)}` : stopChanged ? (hadOld("stopLoss", result.previousStopLoss) ? "止损已撤" : "未设止损") : "",
      takeChanged ? (result.takeProfit ? `止盈 ${priceWords(result.takeProfit)}`
        : hadOld("takeProfit", result.previousTakeProfit) ? "止盈已撤" : "未设止盈") : ""].filter(Boolean);
    const old = [stopChanged ? replaced("stopLoss", "止损", result.previousStopLoss) : "",
      takeChanged ? replaced("takeProfit", "止盈", result.previousTakeProfit) : ""].filter(Boolean);
    const untouched = [
      !stopChanged && !result.stopLoss ? bucketWords(applied, row => row.stopLoss, { lots: true }) : "",
      !takeChanged && !result.takeProfit ? bucketWords(applied, row => row.takeProfit, { lots: true }) : ""];
    const kept = [untouched[0] ? `止损未动：${untouched[0]}` : "", untouched[1] ? `止盈未动：${untouched[1]}` : ""].filter(Boolean);
    // 原来没有挂 needs proof: an old single-price card, or this run replaced no row at all.
    const replacedIds = (result as { replacedStopIds?: unknown }).replacedStopIds;
    const provenNone = !applied || (Array.isArray(replacedIds) && replacedIds.length === 0);
    const detail = [old.length ? `${old.join("、")} 已撤` : provenNone ? "原来没有挂" : "", ...kept].filter(Boolean).join("；");
    return { title: `${lots}${now.join("、")}`, detail: detail || undefined, stop: stopChanged, outcome: { label: "已改", tone: "ok" } };
  }
  // Card time: the original levels as each pre-close bucket held them (not split over the remaining lots).
  const snapshot = plan.perBucketOriginal;
  const each = (key: LevelKey) => plan.original[key] ? ""
    : bucketWords(snapshot, row => row[key], { setOnly: plan[key].action !== "keep" });
  const kept = [plan.stopLoss.action === "keep" && plan.original.stopLoss ? `止损 ${priceWords(plan.original.stopLoss)}` : "",
    plan.takeProfit.action === "keep" && plan.original.takeProfit ? `止盈 ${priceWords(plan.original.takeProfit)}` : ""].filter(Boolean);
  const keptEach = [plan.stopLoss.action === "keep" && each("stopLoss") ? `止损不变：${each("stopLoss")}` : "",
    plan.takeProfit.action === "keep" && each("takeProfit") ? `止盈不变：${each("takeProfit")}` : ""].filter(Boolean);
  const originals = [stopChanged ? (plan.original.stopLoss ? `原止损 ${priceWords(plan.original.stopLoss)}`
      : each("stopLoss") ? `原止损 ${each("stopLoss")}` : snapshot ? "" : "原来没挂止损") : "",
    takeChanged ? (plan.original.takeProfit ? `原止盈 ${priceWords(plan.original.takeProfit)}`
      : each("takeProfit") ? `原止盈 ${each("takeProfit")}` : "") : ""].filter(Boolean);
  const scalarBefore = [originals.join("，"), kept.length ? `${kept.join("、")} 不变` : ""].filter(Boolean).join("，");
  const before = [scalarBefore, ...keptEach].filter(Boolean).join("；");
  if (plan.status === "failed" || plan.status === "skipped") {
    return { title: `${lots}${changes.join("、")}`, detail: `没改成：${plan.reason ?? "条件不满足"}`, stop: stopChanged,
      outcome: { label: "未改", tone: "fail" } };
  }
  return { title: `${lots}${changes.join("、")}`, detail: before || undefined, stop: stopChanged,
    outcome: plan.status === "awaiting_close" ? { label: "等平仓", tone: "wait" } : undefined };
}

/** The card's one-line result under the steps (样稿: 两步都完成… / 平仓已完成；本次保护修改未执行). */
export function remainingResult(plan: RemainingProtection, closedVolume: number): { text: string; tone: "ok" | "part" } | null {
  const result = plan.result ?? {};
  if (plan.status === "done") {
    const where = result.stopLoss ? `止损在 ${priceWords(result.stopLoss)}${plan.stopLoss.atCost ? "（成本）" : ""}` : "保护已按卡上改好";
    return { text: `两步都完成：平掉 ${result.filled ?? closedVolume} 手，剩下 ${result.remainingVolume ?? plan.remainingVolume} 手${where}`, tone: "ok" };
  }
  if (plan.status === "failed") {
    // The close's own fill may still have legitimately shrunk / retired old rows; only the replacement didn't run.
    return { text: "平仓已完成；本次保护修改未执行", tone: "part" };
  }
  if (plan.status === "skipped") return { text: `${plan.reason ?? "平仓没有成交"}，原来的止损止盈都还在`, tone: "part" };
  return null;
}
