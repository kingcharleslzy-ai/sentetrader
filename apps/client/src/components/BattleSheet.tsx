import { useEffect, useState } from "react";
import { requestJson } from "../api";
import { trimPriceZeros } from "./chartPriceFormat";
import { SectionCard } from "./Blocks";
import "./BattleSheet.css";

type Priority = "高" | "中" | "低";
type SheetLevel = { key: string; label: string; price: number; distancePercent?: number };
export type SheetRow = { product: string; name: string; symbol: string | null; price: number | null; priority: Priority; score: number;
  levels: SheetLevel[]; nearest: { label: string; distancePercent: number } | null;
  news: { strength: "强" | "中" | "无"; items: Array<{ id: string; title: string; at: string | null; reports: number }>;
    /** 次要 events (weak, no direction): not news strength, but the row says they are there. */
    minor?: number };
  playbooks: Array<{ id: string; name: string; why: string }>; error?: string };
export type SheetReport = { generatedAt: string; rows: SheetRow[]; foreign: string[]; over: number; playbookCount: number };
/** What the news panel's observation snapshot reports about the 今日作战表 tab. */
export type SheetState = { status: "loading" | "ready" | "error"; rows: number; high: number; mid: number; low: number;
  top?: string | null; playbookCount?: number; error?: string };

const REFRESH_MS = 60_000;
const BEIJING = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const price = (value: number) => trimPriceZeros(value.toFixed(2));
const clock = (value: string | null) => { const at = value ? new Date(value) : undefined; return at && !Number.isNaN(at.getTime()) ? BEIJING.format(at) : ""; };
/** 开盘区间 shows as one range; the other levels one by one. */
function levelText(levels: SheetLevel[]) {
  const low = levels.find(level => level.key === "opening_low");
  const distance = (level: SheetLevel) => level.distancePercent === undefined ? "" : ` ${level.distancePercent}%`;
  return levels.filter(level => level.key !== "opening_low").map(level => level.key === "opening_high" && low
    ? { key: "opening", text: `开盘区间 ${price(low.price)}–${price(level.price)}`, far: `${distance(level)}${low.distancePercent === undefined ? "" : ` / ${low.distancePercent}%`}` }
    : { key: level.key, text: `${level.label} ${price(level.price)}`, far: distance(level) });
}

/**
 * 今日作战表 (侧栏重排第 5 批, owner 2026-10-25): the 调研 panel's first page — one row per 自选品种, ranked
 * 高 / 中 / 低 by the Runtime (news strength + distance to the nearest key level), each with its key levels,
 * the 调研 · 快讯 events it cites and the user's 打法 that may show up there. No 偏多 / 偏空 anywhere.
 * `onOpen` switches the chart to the row's contract and opens 品种调研.
 */
export function BattleSheet({ baseUrl, visible, compact = false, onOpen, onState }: { baseUrl: string | undefined; visible: boolean;
  compact?: boolean; onOpen?: (symbol: string) => void; onState?: (state: SheetState) => void }) {
  const [report, setReport] = useState<SheetReport>();
  const [error, setError] = useState("");
  useEffect(() => {
    if (!visible || !baseUrl) return;
    let cancelled = false;
    const load = async () => {
      try {
        const next = await requestJson<SheetReport>(`${baseUrl}/api/research/sheet`);
        // An older Runtime (or a proxy page) answers something else: say so instead of drawing it.
        if (!Array.isArray(next?.rows)) throw new Error("Runtime 还不支持作战表");
        if (!cancelled) { setReport({ ...next, foreign: next.foreign ?? [] }); setError(""); }
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "作战表读取失败");
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [visible, baseUrl]);
  const rows = report?.rows ?? [];
  useEffect(() => {
    const count = (value: Priority) => rows.filter(row => row.priority === value).length;
    onState?.({ status: report ? "ready" : error ? "error" : "loading", rows: rows.length, high: count("高"), mid: count("中"), low: count("低"),
      top: rows[0]?.product ?? null, playbookCount: report?.playbookCount, ...(error ? { error } : {}) });
  }, [report, error, onState]); // eslint-disable-line react-hooks/exhaustive-deps
  const notes = [report?.over ? `只排前 12 个自选，另有 ${report.over} 个没排` : "", report?.foreign.length ? `外盘 ${report.foreign.length} 个不排` : ""].filter(Boolean);
  return <SectionCard title="今日作战表" className="battle-sheet" testId="battle-sheet"
    note={report ? `${clock(report.generatedAt)} 更新${notes.length ? ` · ${notes.join("，")}` : ""}` : undefined}
    hint={<>每个自选品种一行（最多 12 个，外盘不排），按优先级排好。关键价位由程序从主力合约行情算：昨高、昨低取上一交易日日线；开盘区间是本交易日开盘后 15 分钟（夜盘品种从夜盘开盘算）；均价是本交易日分时均价；百分比是现价离它多远。消息取调研 · 快讯近 24 小时的事件：有重要事件为强，只有其他事件为中。优先级：消息强 2 分、中 1 分；离最近的关键价位 0.3% 以内 2 分、1% 以内 1 分；3 分以上为高、2 分为中、其余为低。交易笔记：你交易笔记里名字或进场规则提到某个价位（昨高低、开盘区间、均价，或突破、回踩）且现价离它 1% 以内，或提到消息且今天有消息。这张表不给多空方向。</>}>
    <div className="section-card-body">
      {error ? <p role="alert" className="sheet-note">作战表读取失败（{error}）{report ? "，下面是上次的内容" : ""}</p> : null}
      {!report && !error ? <p role="status" className="sheet-note">正在排作战表…</p> : null}
      {report && !rows.length ? <p className="sheet-note">还没有自选品种：在报价表或合约抽屉里点星标加入，这里就会每个品种排一行。</p> : null}
      {rows.length ? <ol className="sheet-rows" data-layout={compact ? "phone" : "panel"}>{rows.map(row => <li key={row.product} className="sheet-row" data-priority={row.priority}>
        <div className="sheet-head">
          <span className="sheet-priority" data-priority={row.priority} aria-label={`优先级${row.priority}`}>{row.priority}</span>
          <strong>{row.name}</strong>{row.symbol ? <small>{row.symbol.split(".").pop()}</small> : null}
          {row.price ? <b className="sheet-price">{price(row.price)}</b> : null}
          {row.symbol && onOpen ? <button type="button" className="sheet-open" onClick={() => onOpen(row.symbol as string)}>看调研</button> : null}
        </div>
        <dl className="sheet-fields">
          <dt>关键价位</dt>
          <dd>{row.levels.length ? levelText(row.levels).map(item => <span key={item.key}>{item.text}<small>{item.far}</small></span>)
            : <span className="sheet-none">{row.error ?? "还没有可算的价位"}</span>}</dd>
          <dt>消息</dt>
          <dd>{row.news.items.length ? <><span className="sheet-strength" data-strength={row.news.strength}>{row.news.strength}</span>
            {row.news.items.map(item => <span key={item.id}>{item.title}<small>{clock(item.at)}{item.reports > 1 ? ` · ${item.reports} 家` : ""}</small></span>)}</>
            : <span className="sheet-none">{row.news.minor ? `近 24 小时只有 ${row.news.minor} 条次要消息` : "近 24 小时没有相关快讯"}</span>}</dd>
          <dt>交易笔记</dt>
          <dd>{row.playbooks.length ? row.playbooks.map(playbook => <span key={playbook.id}>{playbook.name}<small>{playbook.why}</small></span>)
            : <span className="sheet-none">{report?.playbookCount ? "价位附近没有对得上的交易笔记" : "还没有交易笔记"}</span>}</dd>
        </dl>
      </li>)}</ol> : null}
    </div>
  </SectionCard>;
}
