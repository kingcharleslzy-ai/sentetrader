import { useEffect, useState, type MouseEvent as ReactMouseEvent } from "react";
import { ChevronRight } from "lucide-react";
import { requestJson } from "../api";
import { LONG_READ_TIMEOUT_MS } from "../requestTimeout";
import { requestStatus } from "../strategyApi";
import type { PrefillResponse } from "../strategyEditor";
import type { EditorStart } from "../strategyEditorStore";
import { COMMON_PREVIEW_DAYS, COMMON_TEMPLATE_IDS, loadTemplates, templateFigures, useTemplateCatalog, type TemplateFigures } from "../strategyTemplates";
import { HelpTip } from "./Blocks";
import "./StrategyStarts.css";

/** “从模板开始 ▾” beside 新建策略: picking a template opens the 策略编辑卡 from it (strategyEditor {new, start: template:<id>}). */
export function TemplateStartSelect({ className, onStart }: { className?: string; onStart: (start: EditorStart) => void }) {
  const catalog = useTemplateCatalog();
  const templates = (catalog?.items ?? []).filter(item => !(item.params ?? []).some(param => param.type === "contract"));
  return <select className={className} aria-label="从模板开始" value="" onChange={event => { if (event.target.value) onStart(`template:${event.target.value}`); }}>
    <option value="">从模板开始 ▾</option>
    {templates.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
  </select>;
}

/** A template's description up to its first 。 or ；(the empty state shows one line; the row's title has it all). */
export const firstSentence = (text: string) => text.match(/^[^。；;]*[。；;]?/)?.[0].replace(/[；;]$/, "。") || text;

type Habits = { status: "loading" } | { status: "ready"; eligible: boolean; trades: number; minTrades: number; reason?: string };

/**
 * 我的策略's empty state (the owner approved the mockup 2026-10-08): 还没有策略 with what a strategy is and the
 * four steps (desktop only), the three ways to start — 从空白开始, 从模板开始, 按我的习惯 (its progress to the
 * prefill's minimum from `GET /api/strategy-plans/prefill`'s sample; clickable once there are enough trades) —
 * and four common templates with their 近 60 个交易日 backtest on the chart's contract (the template preview,
 * computed while this shows, kept per contract for the window; a failed one shows only its sentence).
 * Every entry opens the 策略编辑卡 through `onStart` (= strategyEditor {new: true, start}).
 */
export function StrategyStarts({ rootUrl, symbol, symbolLabel, compact, onStart }: {
  rootUrl: string; symbol: string; symbolLabel: string;
  /** The phone: no explanation block, the starts and the templates stacked. */
  compact?: boolean;
  onStart: (start: EditorStart) => void;
}) {
  const catalog = useTemplateCatalog();
  useEffect(() => { void loadTemplates(rootUrl).catch(() => undefined); }, [rootUrl]);
  const [habits, setHabits] = useState<Habits>({ status: "loading" });
  useEffect(() => {
    let alive = true;
    setHabits({ status: "loading" });
    void requestJson<PrefillResponse>(`${rootUrl}/api/strategy-plans/prefill`, { timeoutMs: LONG_READ_TIMEOUT_MS }).then(reply => {
      if (alive) setHabits({ status: "ready", eligible: reply.eligible, trades: reply.sample?.trades ?? reply.trades.length, minTrades: reply.sample?.minTrades ?? 5, reason: reply.reason });
    }).catch(error => {
      if (alive) setHabits({ status: "ready", eligible: false, trades: 0, minTrades: 5,
        reason: requestStatus(error) === 409 ? "成交记录暂时读不到，先从空白或模板开始。" : "成交记录没读到，稍后再试。" });
    });
    return () => { alive = false; };
  }, [rootUrl]);
  const common = COMMON_TEMPLATE_IDS.map(id => catalog?.items.find(item => item.id === id)).filter(item => item !== undefined);
  const [figures, setFigures] = useState<Record<string, TemplateFigures | null | "loading">>({});
  const commonKey = common.map(item => item.id).join(",");
  useEffect(() => {
    if (!symbol || !commonKey) return;
    let alive = true;
    const ids = commonKey.split(",");
    setFigures(Object.fromEntries(ids.map(id => [id, "loading" as const])));
    for (const id of ids) void templateFigures(rootUrl, id, symbol).then(value => { if (alive) setFigures(current => ({ ...current, [id]: value })); });
    return () => { alive = false; };
  }, [rootUrl, symbol, commonKey]);
  const missing = habits.status === "ready" ? Math.max(0, habits.minTrades - habits.trades) : 0;
  const ready = habits.status === "ready" && habits.eligible;
  const scrollToTemplates = (event: ReactMouseEvent<HTMLButtonElement>) =>
    event.currentTarget.closest(".ss-starts-root")?.querySelector<HTMLElement>(".ss-templates")?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  return <div className={`ss-starts-root${compact ? " ss-compact" : ""}`} data-agent-target="strategy-starts" data-testid="strategy-starts">
    <div className="ss-head">
      <h4 className="ss-title">还没有策略，选一种开始</h4>
      <HelpTip helpKey="strategy-starts" label="策略是什么">
        <p>策略就是把你的做法写成规则：什么时候开仓、怎么平仓、每次几手。写好先看回测，上线后按规则提醒你、出卡等你确认，或自动下模拟单。</p>
        <ol className="ss-flow">
          <li><b>① 写规则</b> 编辑卡里选条件</li>
          <li><b>② 看回测</b> 边改边算近 60 天</li>
          <li><b>③ 上线</b> 只提醒 / 出卡 / 自动</li>
          <li><b>④ 复盘</b> 每笔都能看到原因</li>
        </ol>
        <p>从空白开始：自己一条条选条件，适合已经有明确做法。从模板开始：经典做法改一改：双均线、突破、反转……按我的习惯：成交满 {habits.status === "ready" ? habits.minTrades : 5} 笔后，AI 按你的成交预填合约、止损、手数。</p>
      </HelpTip>
    </div>
    <div className="ss-starts">
      <button type="button" className="ss-start" aria-label="从空白开始" onClick={() => onStart("blank")}>
        <b>从空白开始</b><em>新建<ChevronRight aria-hidden="true" /></em></button>
      <button type="button" className="ss-start" aria-label="从模板开始，看下面的模板" onClick={scrollToTemplates}>
        <b>从模板开始</b><em>看下面的模板<ChevronRight aria-hidden="true" /></em></button>
      <button type="button" className="ss-start" aria-label="按我的习惯" disabled={!ready} data-testid="strategy-starts-habits" onClick={() => onStart("habits")}>
        <b>按我的习惯</b>
        {habits.status === "loading" ? <span className="ss-note">正在读你的成交…</span> : ready ? <em>按成交预填<ChevronRight aria-hidden="true" /></em> : <>
          <span className="ss-progress" role="progressbar" aria-label="成交笔数" aria-valuemin={0} aria-valuemax={habits.minTrades} aria-valuenow={Math.min(habits.trades, habits.minTrades)}>
            <i style={{ width: `${Math.min(100, habits.minTrades ? habits.trades / habits.minTrades * 100 : 0)}%` }} /></span>
          <span className="ss-note">{missing > 0 ? `已有 ${habits.trades} 笔，还差 ${missing} 笔` : habits.reason ?? "成交记录暂时读不到"}</span></>}
      </button>
    </div>
    {common.length ? <>
      <h4 className="ss-title">常用模板 <small>· 当前合约{symbolLabel}、近 {COMMON_PREVIEW_DAYS} 个交易日回测</small></h4>
      <ul className="ss-templates" aria-label="常用模板">
        {common.map(item => {
          const value = figures[item.id];
          const summary = item.plainSummary ?? item.scenario;
          return <li key={item.id} title={summary}>
            <span className="ss-tpl-text"><b>{item.name}</b><span>{firstSentence(summary)}</span></span>
            <span className="ss-tpl-fig">{value === "loading" ? "计算中…" : value ? `${value.trades} 笔 · 胜率 ${value.winRate == null ? "—" : `${Math.round(value.winRate)}%`}` : ""}</span>
            <button type="button" aria-label={`用这个：${item.name}`} onClick={() => onStart(`template:${item.id}`)}>用这个</button>
          </li>;
        })}
      </ul>
    </> : null}
  </div>;
}
