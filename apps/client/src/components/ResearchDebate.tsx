import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, ChevronLeft, RefreshCw } from "lucide-react";
import { readFlashFeed } from "../flashCache";
import { feedLiveNow, onDebateDelta, onFeedNudge } from "../runtimeFeed";
import {
  adoptDebate, bumpDebates, cancelDebate, DEBATE_RUNNING, debateError, fallbackReason, getDebate, roleModels, listDebates, modelName, setDebateViewOpen, setShownDebate, startDebate,
  type AnalystKey, type Debate, type DebateFact, type DebateReport, type DebateSide, type DebateStats, type DebateSummary, type DebateTurn, type ReportLine,
} from "../researchDebate";
import { useDisclosureSurface, useUiSurface } from "../uiSurfaces";
import { HelpTip, SectionCard, StatTiles } from "./Blocks";
import type { NewsEvent } from "./NewsEvents";
import "./ResearchDebate.css";

/**
 * 多 AI 辩论 (owner-approved sample 2026-10-11; its own page, spoken turns and streaming 2026-10-24; analysts 2026-10-24,
 * sample ④): the page the right rail's 辩论 opens on a computer (half the window) — and that a 快讯 row's 研讨,
 * 今日判断's 让 AI 们辩一轮 and the `researchDebate` command open — and a full-screen page over 调研 on a phone. While it
 * runs: the stage bar, the four analysts' reports as they are written (side by side in time), 上次复盘, then the four
 * turns one after another (feed.debate; a slow poll only when the feed is not live), each replaced by its checked
 * version when it ends; then the verdict (the one primary button is 写进今日判断), the folded reports, 交锋回看,
 * 以前的辩论, and at the bottom the folded 裁判怎么取舍, 底稿, sources, track record and record.
 */
export type DebateOrigin = "flash" | "desk" | "rail";
type DebateRequest = { kind: "start" | "rerun"; status: "sending" | "started" | "reused" | "refused" | "error"; eventId?: string; message?: string };
type DebateStream = { id: string; turn: number; side: DebateSide; text: string };
export type DebateHost = {
  open: boolean; origin: DebateOrigin; debate?: Debate; request?: DebateRequest; placeholder?: { title: string; kind: "event" | "today" };
  adopting: boolean; adoptMessage?: string; adoptError?: string; symbol?: string;
  /** The running turn's text as it streams in (feed.debate), newer than the last read. */
  stream?: DebateStream;
  /** Each writing analyst's report text as it streams in (feed.debate with `analyst`). */
  reports?: Partial<Record<AnalystKey, string>>;
  /** 研讨战绩: read once a finished debate is open. */
  stats?: DebateStats;
  /** 以前的辩论: the product's recent debates (the open one left out). */
  recent?: DebateSummary[];
  start: (target: { eventId?: string; symbol?: string; title?: string; origin?: DebateOrigin }) => void;
  openDebate: (id: string, origin?: DebateOrigin) => void; rerun: () => void; close: () => void; adopt: () => void; cancel: () => void;
  ask?: () => void;
};

const POLL_MS = 4000;
const SIDE_NAMES: Record<DebateSide, string> = { bull: "多方", bear: "空方" };
const SHEET_MARK = { complete: "✓", partial: "部分", missing: "缺失", stale: "过期" } as const;
const CLOCK = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const DAY = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit" });
const clock = (value?: string | null) => { const when = value ? new Date(value) : undefined; return when && !Number.isNaN(when.getTime()) ? CLOCK.format(when) : ""; };
const day = (value?: string | null) => { const when = value ? new Date(`${value}T12:00:00+08:00`) : undefined; return when && !Number.isNaN(when.getTime()) ? DAY.format(when).replace("/", "-") : value ?? ""; };
/** A fact's date as a chip shows it: "10-23" from 20261023, 2026-10-23 or an instant. */
/** A fact's day as Beijing reads it: a market date stays as written; an instant (a searched page's
 *  `2026-10-19T16:00:00+00:00`, Beijing midnight of 10-20) is shown in Beijing time, never by its UTC date. */
export const factDay = (value?: string | null) => {
  if (value && value.length > 10) {
    const when = new Date(value);
    if (!Number.isNaN(when.getTime())) return DAY.format(when).replace("/", "-");
  }
  const digits = /^(\d{4})-?(\d{2})-?(\d{2})/.exec(value ?? "");
  return digits ? `${digits[2]}-${digits[3]}` : "";
};
/** A price as the facts carry it (they are on the contract's tick grid; 国债 has three decimals): never rounded. */
const price = (value: number | null | undefined) => typeof value === "number" ? value.toLocaleString("zh-CN", { maximumFractionDigits: 6 }) : "—";
const winnerLine = (verdict: { winner?: DebateSide | null; margin?: string | null; insufficient?: boolean | null } | null | undefined) => {
  if (!verdict) return "";
  if (verdict.insufficient) return "证据不足";
  if (!verdict.winner) return "双方相当";
  return `${SIDE_NAMES[verdict.winner]}${verdict.margin === "明显" ? "明显胜出" : "略胜"}`;
};
/** 多 : 空 on the meter: a clear win 7 : 3, a narrow one 6 : 4, a draw 5 : 5. */
const split = (debate: Debate) => {
  const verdict = debate.verdict;
  const strong = verdict?.margin === "明显" ? 7 : 6;
  return !verdict?.winner ? 5 : verdict.winner === "bull" ? strong : 10 - strong;
};
const stepOf = (debate: Debate | undefined) => debate?.steps.find(step => step.status === "run")?.key ?? null;
// The model's citation markers ([F12], full-width brackets too), as the Runtime reads them.
const MARKER = /[[【［]\s*[Ff]\s*(\d{1,3})\s*[\]】］]/g;
/** 第一轮 · 先各自表态 before turn 1, 第二轮 · 互相反驳 before turn 3. */
const ROUND_OF: Record<number, string> = { 1: "第一轮 · 先各自表态", 3: "第二轮 · 互相反驳" };
const TURN_LABEL: Record<number, string> = { 1: "", 2: "回应多方", 3: "回应空方", 4: "回应多方" };

const HELP = {
  verdict: <>裁判读完四份分析师报告和四次发言后裁决，并写出双方的关键分歧和几种“如果……那么……”的情景。双方有分歧不是给“震荡”的理由，要判断哪边更站得住；证据都太薄时才给“看不清”（和“震荡”不是一回事）。立场按胜方和胜出幅度定档：明显胜出给“偏多 / 偏空”，略胜给“略偏多 / 略偏空”，基本平衡给“震荡”。</>,
  confidence: <>信心不由模型自报，由程序计算：被采纳的句子所引的资料里，核对过的数据（不是报道、也不是过期旧值）占比越高越高；胜方被驳回的句子每条扣分；明显胜出加分。底稿有缺失或过期时最高“中”，缺两份以上为“低”；“震荡”最高“中”，“看不清”为“低”。</>,
  exchange: <>多方先开场，空方回应多方，多方再反驳，空方最后反驳；每一方都看得到四份分析师报告和之前的全部交锋，每次发言 400 到 600 字、上限相同，要讲因果链。双方只能引用底稿里的资料（含分析师从网上补的），句子里的标签就是所引的那一条（点开看来源）。每次发言说完，程序逐句核对：引用的资料存在、句子里的数字在所引资料里、没有交易建议；没通过的标黄色“未核对”，裁判看不到。</>,
  analysts: <>辩论前，技术面、供需基本面、资金持仓、新闻宏观四位分析师同时各写一份报告：先用调研页的资料；自己这块缺的或只有旧值的，才上网补（每位最多 2 次、每场最多 6 次搜索），找到的每条都进资料来源、带来源和日期，数字同样要核对。写完的报告一句一句核对，只把核对过的句子交给多空双方和裁判。某位没写成就标出来，辩论照常进行。</>,
  lesson: <>同一品种最近一次已对账（5 个交易日后按收盘价）的辩论：谁说对了、说错的一方当时说了什么。多空双方和裁判都会看到，当作教训。</>,
  stages: <>先是四位分析师同时写报告，再是多空双方四次发言，最后裁判给结论；一场大约 2 到 3 分钟。</>,
  judge: <>裁判只看到 A 方、B 方（哪方是多方随机分配，记在研讨记录里），不知道是哪家模型；每方字数上限相同。没写进采纳或驳回的句子不影响结论。</>,
  sheets: <>底稿复用调研员的事实，数字由程序算好，不调模型：持仓（前 20 会员、持仓变化、CFTC）、基差与仓单（现货、仓单、库存）、期限结构、新闻（快讯、报道、机构观点、宏观）、技术面（行情、走势、指标、近 20 日关键价位、关联品种）。完整＝该有的都有；部分＝缺一部分；缺失＝没有；过期＝有来源没刷新的旧值。</>,
  progress: <>可以先做别的，好了会在 AI 动态里提醒你。先整理资料（复用调研员的事实，加上引发辩论的快讯，分成 5 份底稿，数字由程序算好）；四位分析师同时写报告；然后多方开场、空方回应、多方反驳、空方反驳，一句一句写出来；最后裁判只看到 A 方、B 方、四份报告和核对过的句子，给出结论。</>,
  line: <>“什么情况下这个结论就不成立”先写失效位：胜方逻辑不再成立的价格，偏多时收盘跌破、偏空时收盘涨破就不成立。只能取底稿里的价格（主力价、涨跌停、近 20 日收盘高低与均价），而且离发起时价格不超过 8%；对不上的不显示。后面是裁判写的改判条件。</>,
  reckoning: <>出结论时就定好对账口径：5 个交易日后按收盘价判对错。偏多 / 略偏多：期间收盘从未跌破失效位、且第 5 日收盘高于发起时的价格算对；偏空 / 略偏空反过来；震荡：第 5 日涨跌幅不超过 1.5% 算对；看不清、需复核不计。多方、空方模型按它们自己的总体看法，单模型今日判断按发起时的那份，同一规则对账。满 30 次后分开显示命中率；战绩不进提示词，也不给模型加权。</>,
  recent: <>同一品种最近几次辩论和它们 5 个交易日后的对账：说对了 / 说错了；还没到期是“待揭晓”；需复核、证据不足、外盘或没有发起价的不计入战绩。点一格打开那次辩论。</>,
};

/** The debate's state machine for one host (the computer's 辩论 page or the phone's 调研 page) and its `researchDebate`
 *  surface. `view`: whether the page shows and how to show / close it — the computer's right dock (open = the rail's
 *  辩论); without it the host keeps its own (the phone's sub-page). */
export function useDebateHost({ baseUrl, symbol, enabled, onAsk, view }: { baseUrl: string | undefined; symbol: string | undefined; enabled: boolean;
  /** 在对话里接着问: put a question about this debate into the chat's input for the user to edit and send (never sends). */
  onAsk?: (text: string) => void; view?: [boolean, (open: boolean) => void] }): DebateHost {
  const own = useState(false);
  const [open, setView] = view ?? own;
  const show = useRef(setView);
  show.current = setView;
  const [origin, setOrigin] = useState<DebateOrigin>("rail");
  const [debate, setDebate] = useState<Debate>();
  const [request, setRequest] = useState<DebateRequest>();
  const [placeholder, setPlaceholder] = useState<DebateHost["placeholder"]>();
  const [adopting, setAdopting] = useState(false);
  const [adoptMessage, setAdoptMessage] = useState<string>();
  const [adoptError, setAdoptError] = useState<string>();
  const [stats, setStats] = useState<DebateStats>();
  const [recent, setRecent] = useState<DebateSummary[]>();
  const [stream, setStream] = useState<DebateStream>();
  const [reports, setReports] = useState<{ id?: string; text: Partial<Record<AnalystKey, string>> }>({ text: {} });
  const sequence = useRef(0);
  const running = Boolean(debate && DEBATE_RUNNING.has(debate.status));
  const shown = useRef<string | undefined>(undefined);
  shown.current = debate?.id;

  useEffect(() => { if (enabled) setDebateViewOpen(open); }, [open, enabled]);
  useEffect(() => { if (enabled) setShownDebate(open ? debate?.id : undefined); }, [open, debate?.id, enabled]);
  // Unmounted: an answer still on its way belongs to a view that is gone — a late “讨论这条” lookup must not start a
  // debate (#749).
  useEffect(() => () => { sequence.current += 1; if (enabled) setDebateViewOpen(false); }, [enabled]);
  // Hidden from outside (the 调研面板 switched to another tab or closed, 2026-10-25): the same as 关闭 — an answer
  // still on its way must not reopen the page.
  const wasOpen = useRef(open);
  useEffect(() => {
    if (wasOpen.current && !open) { sequence.current += 1; setRequest(undefined); setPlaceholder(undefined); }
    wasOpen.current = open;
  }, [open]);

  const reset = () => { setAdoptMessage(undefined); setAdoptError(undefined); setAdopting(false); setStream(undefined); };
  const start = useCallback((target: { eventId?: string; symbol?: string; title?: string; origin?: DebateOrigin }, kind: "start" | "rerun" = "start") => {
    // An event's product is the event's (resolved by the Runtime); the reading contract only for 今日议题.
    const subject = target.eventId ? target.symbol : target.symbol ?? symbol;
    if (!baseUrl || (!subject && !target.eventId)) throw new Error("请先选择合约");
    const mine = ++sequence.current;
    show.current(true); setOrigin(target.origin ?? (target.eventId ? "flash" : "desk")); reset();
    setDebate(undefined);
    setPlaceholder({ title: target.title ?? (target.eventId ? "这条快讯" : "今日议题"), kind: target.eventId ? "event" : "today" });
    setRequest({ kind, status: "sending", eventId: target.eventId });
    // Always the Runtime's start: it reuses a result only when the topic, product and facts are unchanged today.
    void startDebate(baseUrl, { symbol: subject, eventId: target.eventId, force: kind === "rerun" }).then(result => {
      if (mine !== sequence.current) return;
      setDebate(result);
      setRequest({ kind, status: result.reused ? "reused" : "started", eventId: target.eventId });
      bumpDebates();
    }).catch(cause => {
      if (mine !== sequence.current) return;
      setRequest({ kind, status: "refused", eventId: target.eventId, message: debateError(cause) });
    });
  }, [baseUrl, symbol]);
  const openDebate = useCallback((id: string, from?: DebateOrigin) => {
    if (!baseUrl) throw new Error("应用服务未连接");
    const mine = ++sequence.current;
    show.current(true); if (from) setOrigin(from); reset(); setRequest(undefined); setPlaceholder(undefined);
    if (debate?.id !== id) setDebate(undefined);
    void getDebate(baseUrl, id).then(result => { if (mine === sequence.current) setDebate(result); })
      .catch(cause => { if (mine === sequence.current) setRequest({ kind: "start", status: "error", message: debateError(cause) }); });
  }, [baseUrl, debate?.id]);
  const rerun = useCallback(() => {
    // A closed page keeps its last debate in memory; 再辩一轮 is for the one on screen only (it spends a run).
    if (!open || !debate) throw new Error("没有打开的辩论");
    start({ eventId: debate.topic.kind === "event" ? debate.topic.eventId : undefined, symbol: debate.productKey, title: debate.topic.title, origin }, "rerun");
  }, [open, debate, origin, start]);
  const close = useCallback(() => { sequence.current += 1; show.current(false); setRequest(undefined); setPlaceholder(undefined); reset(); }, []);
  const adopt = useCallback(() => {
    if (!baseUrl || !debate) return;
    // A rerun or another debate opened meanwhile: this answer belongs to a view that is gone.
    const mine = sequence.current, id = debate.id;
    const current = () => mine === sequence.current && id === shown.current;
    setAdopting(true); setAdoptError(undefined);
    void adoptDebate(baseUrl, id).then(result => {
      bumpDebates();
      if (!current()) return;
      setDebate(result.debate); setAdoptMessage(`已写进今日判断：${result.judgment.stance}`);
    }).catch(cause => { if (current()) setAdoptError(debateError(cause)); }).finally(() => { if (current()) setAdopting(false); });
  }, [baseUrl, debate]);
  const cancel = useCallback(() => {
    if (!baseUrl || !debate) return;
    const mine = sequence.current, id = debate.id;
    void cancelDebate(baseUrl, id).then(result => {
      bumpDebates();
      if (mine === sequence.current && id === shown.current) setDebate(result);
    }).catch(() => undefined);
  }, [baseUrl, debate]);

  // Progress: each turn streams in over the feed (feed.debate) and every step's end nudges a re-read (feed.ui,
  // reason research-debate); the slow poll runs only while the feed is not live.
  const id = debate?.id;
  useEffect(() => {
    if (!baseUrl || !id || !running) return;
    let stopped = false;
    const read = () => { void getDebate(baseUrl, id).then(result => { if (!stopped) { setDebate(result); if (!DEBATE_RUNNING.has(result.status)) bumpDebates(); } }).catch(() => undefined); };
    const timer = setInterval(() => { if (!feedLiveNow()) read(); }, POLL_MS);
    const stop = onFeedNudge(reason => { if (reason === "research-debate") read(); });
    const stopStream = onDebateDelta(payload => {
      if (stopped || payload.id !== id) return;
      const { analyst, turn, side } = payload;
      if (analyst) setReports(current => ({ id, text: { ...(current.id === id ? current.text : {}), [analyst]: payload.text } }));
      else if (turn && side) setStream({ id, turn, side, text: payload.text });
    });
    return () => { stopped = true; clearInterval(timer); stop(); stopStream(); };
  }, [baseUrl, id, running]);

  const finished = open && Boolean(id) && !running;
  useEffect(() => {
    if (!baseUrl || !finished) return;
    let active = true;
    void listDebates(baseUrl, { limit: 1 }).then(result => { if (active) setStats(result.stats); }).catch(() => undefined);
    return () => { active = false; };
  }, [baseUrl, finished, id]);
  // 以前的辩论: the open debate's product, else the product being read (the page opened from the rail).
  const product = debate?.productKey ?? symbol;
  const listing = open && (finished || (!debate && !request));
  useEffect(() => {
    if (!baseUrl || !listing || !product) return;
    let active = true;
    void listDebates(baseUrl, { product, limit: 4 }).then(result => { if (active) setRecent(result.items.filter(item => item.id !== id).slice(0, 3)); }).catch(() => undefined);
    return () => { active = false; };
  }, [baseUrl, listing, product, id]);

  const latest = useRef({ start, openDebate, rerun, close, symbol, baseUrl });
  latest.current = { start, openDebate, rerun, close, symbol, baseUrl };
  const verdict = debate?.verdict;
  const canAdopt = Boolean(debate?.status === "done" && verdict && !debate.adopted && debate.today !== false);
  const speaking = debate?.turns.find(turn => turn.status === "run")?.side ?? (running && stream ? stream.side : null);
  useUiSurface("researchDebate", {
    state: open ? { open: true, id: debate?.id ?? null, status: debate?.status ?? (request?.status === "refused" ? "refused" : request ? "starting" : "empty"),
      step: stepOf(debate), speaking: speaking ?? null, turns: debate?.turns.filter(turn => turn.status === "done").length ?? 0,
      // 分析师 (2026-10-24): how many of the four reports are written (没写成 counts as finished); null before them.
      analysts: debate?.analysts ? debate.analysts.filter(report => report.status === "done" || report.status === "failed").length : null,
      lesson: debate?.lesson?.text ?? null,
      symbol: debate?.symbol ?? symbol ?? null, origin,
      topic: debate ? { kind: debate.topic.kind, eventId: debate.topic.eventId ?? null, title: debate.topic.title } : placeholder ?? null,
      stance: verdict?.stance ?? null, confidence: verdict?.confidence ?? null, review: debate?.review ?? false,
      canAdopt, adopted: Boolean(debate?.adopted), request: request ?? null, error: debate?.error ?? null,
      recent: recent?.map(item => item.id) ?? [] } : { open: false },
    apply: value => {
      const change = value as { start?: string; open?: string; rerun?: boolean; close?: boolean };
      const host = latest.current;
      if (change.close) { host.close(); return; }
      if (change.open) { host.openDebate(change.open); return; }
      if (change.rerun) { host.rerun(); return; }
      if (change.start === "desk") { host.start({ origin: "desk" }); return; }
      if (change.start === "event") {
        // “讨论一下这条”: the newest 重要 event of the product being read.
        const product = host.symbol?.replace(/\d+$/, "");
        if (!host.baseUrl || !product) throw new Error("请先选择合约");
        // Closing the page or opening another debate while the lookup runs makes its answer stale.
        const mine = ++sequence.current;
        show.current(true); setOrigin("flash"); setDebate(undefined); setPlaceholder({ title: "最新一条重要快讯", kind: "event" });
        setRequest({ kind: "start", status: "sending" });
        const base = host.baseUrl;
        void readFlashFeed<{ events?: NewsEvent[] }>(base, `hours=24&limit=20&product=${encodeURIComponent(product)}`).then(feed => {
          if (mine !== sequence.current) return;
          const event = (feed.events ?? []).find(item => item.tier === "major");
          if (!event) { setRequest({ kind: "start", status: "refused", message: "近 24 小时没有这个品种的重要快讯可以辩" }); return; }
          latest.current.start({ eventId: event.id, symbol: host.symbol, title: event.title || event.text, origin: "flash" });
        }).catch(cause => { if (mine === sequence.current) setRequest({ kind: "start", status: "error", message: debateError(cause) }); });
        return;
      }
      // An event id alone: the Runtime finds the event and its product (never the chart's product as a guess).
      if (change.start) { host.start({ eventId: change.start, origin: "flash" }); return; }
      throw new Error("researchDebate 需要 start、open、rerun 或 close");
    },
  }, [open, debate, request, placeholder, origin, symbol, canAdopt, speaking, recent], enabled);

  // Said the way the page says it (topic, finish time, verdict) — no internal id; the AI finds the debate by product
  // and time (#768B).
  const topicLine = debate?.topic.kind === "event" ? `快讯“${debate.topic.title.slice(0, 30)}”` : "今日议题";
  const ask = onAsk && debate?.verdict ? () => onAsk(`接着刚才${debate.name}的多 AI 辩论（${topicLine}，${clock(debate.finishedAt)} 完成，`
    + `结论${debate.verdict?.stance}，信心${debate.verdict?.confidence}）：裁判为什么这么判？决定胜负的那条依据可靠吗？`) : undefined;
  return { open, origin, debate, request, placeholder, adopting, adoptMessage, adoptError, symbol, stats, recent,
    stream: running && stream?.id === id ? stream : undefined, reports: running && reports.id === id ? reports.text : undefined,
    start: target => start(target), openDebate, rerun, close, adopt, cancel, ask };
}

/** A sentence with its markers as chips (名称 · 日期; a tap opens the cited fact in 资料来源). An older record's
 *  argument carries no markers: its cited facts follow it. */
function Said({ text, factIds, facts, pick }: { text: string; factIds?: string[]; facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  const parts: ReactNode[] = [];
  let at = 0;
  let marked = false;
  const chip = (fact: DebateFact, key: string) => <button type="button" key={key} className="debate-chip" data-news={fact.section === "flash" || undefined}
    aria-label={`依据 ${fact.n}，${fact.label}`} title={`${fact.label}：${fact.value}`} onClick={() => pick(fact.id)}>
    <span>{fact.section === "flash" ? "快讯" : fact.label}</span>{factDay(fact.asOf) ? <> · {factDay(fact.asOf)}</> : null}</button>;
  for (const found of text.matchAll(MARKER)) {
    parts.push(text.slice(at, found.index));
    const fact = facts.get(Number(found[1]));
    if (fact) { parts.push(chip(fact, `${found.index}`)); marked = true; }
    at = (found.index ?? 0) + found[0].length;
  }
  parts.push(text.slice(at));
  if (!marked) for (const id of factIds ?? []) {
    const fact = [...facts.values()].find(item => item.id === id);
    if (fact) parts.push(chip(fact, id));
  }
  return <>{parts}</>;
}

/** Checked sentences (a turn's, a report's): 未核对 in yellow with why. */
function Sentences({ items, facts, pick }: { items: { id?: string; text: string; factIds: string[]; verified: boolean; problem?: string }[];
  facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  return <>{items.map((item, index) => item.verified
    ? <Fragment key={item.id ?? index}><Said text={item.text} factIds={item.factIds} facts={facts} pick={pick} /></Fragment>
    : <mark key={item.id ?? index} className="debate-unchecked" title={item.problem}><Said text={item.text} factIds={item.factIds} facts={facts} pick={pick} /></mark>)}</>;
}

/** One turn's words: its checked sentences, or while it runs the text so far. */
function TurnText({ turn, text, facts, pick }: { turn: DebateTurn; text?: string; facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  if (turn.status !== "done") return <Said text={text ?? turn.text} facts={facts} pick={pick} />;
  return <Sentences items={turn.sentences} facts={facts} pick={pick} />;
}

function Checks({ turn }: { turn: DebateTurn }) {
  if (turn.status !== "done") return null;
  const failed = turn.sentences.filter(item => !item.verified);
  return <span className="debate-checks" data-unchecked={failed.length || undefined}>
    {failed.length ? failed.map(item => <span key={item.id}>“{item.text.replace(MARKER, "").slice(0, 14)}”{item.problem ? `${item.problem}` : ""} · 未核对</span>)
      : turn.sentences.some(item => item.factIds.length) ? "✓ 数字已核对" : null}
  </span>;
}

/** A streamed turn reveals its text at a steady pace instead of in the bursts the model sends (owner 2026-10-24: the
 *  words came in jerks): each frame shows a share of what is still hidden, never cutting a [F…] marker in half. */
function useSmoothText(target: string, live: boolean): string {
  const [shown, setShown] = useState(target.length);
  const goal = useRef(target.length);
  goal.current = target.length;
  useEffect(() => {
    if (!live) return;
    let frame = 0;
    const step = () => {
      setShown(count => count >= goal.current ? goal.current : count + Math.max(1, Math.ceil((goal.current - count) / 16)));
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [live]);
  if (!live) return target;
  const cut = target.slice(0, Math.min(shown, target.length));
  return cut.lastIndexOf("[") > cut.lastIndexOf("]") ? cut.slice(0, cut.lastIndexOf("[")) : cut;
}

function LiveTurnText({ turn, text, facts, pick }: { turn: DebateTurn; text: string; facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  return <TurnText turn={turn} text={useSmoothText(text, true)} facts={facts} pick={pick} />;
}

/** The running page follows the newest words — the analysts' reports, then the turns — while the reader stays at the
 *  bottom; scrolling up stops the following until they come back down. */
function Follow({ children }: { children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  useEffect(() => {
    const element = box.current;
    const scroller = element?.closest(".debate-body");
    if (!element || !scroller || typeof ResizeObserver === "undefined") return;
    const onScroll = () => { follow.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80; };
    // The page grows as words arrive: keep the newest in view unless the reader scrolled up.
    const observer = new ResizeObserver(() => { if (follow.current) scroller.scrollTop = scroller.scrollHeight; });
    observer.observe(element);
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => { observer.disconnect(); scroller.removeEventListener("scroll", onScroll); };
  }, []);
  return <div className="debate-follow" ref={box}>{children}</div>;
}

/** The exchange while it runs: the four turns as they come (the running one streams with a caret). */
function Transcript({ host, facts, pick }: { host: DebateHost; facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  const { debate, stream } = host;
  const turns: DebateTurn[] = [...(debate?.turns ?? [])];
  // A turn whose start the last read did not see yet: its streamed text alone.
  if (stream && !turns.some(turn => turn.n === stream.turn)) turns.push({ n: stream.turn, side: stream.side, status: "run", text: stream.text, sentences: [] });
  // While the analysts write, their cards are the progress.
  if (!turns.length) return debate?.analysts?.length ? null : <p className="debate-note" role="status">{debate ? "正在整理资料…" : "正在发起…"}</p>;
  return <div className="debate-transcript" aria-label="多空交锋" aria-live="polite">
    {turns.map(turn => {
      const live = turn.status === "run";
      const text = live && stream?.turn === turn.n && stream.text.length >= turn.text.length ? stream.text : turn.text;
      return <Fragment key={turn.n}>
        {ROUND_OF[turn.n] ? <div className="debate-round">{ROUND_OF[turn.n]}</div> : null}
        <div className="debate-turn" data-side={turn.side} data-live={live || undefined}>
          <span className="debate-who">{SIDE_NAMES[turn.side]}{TURN_LABEL[turn.n] ? ` · ${TURN_LABEL[turn.n]}` : ""}{live ? " · 正在说…" : ""}</span>
          <div className="debate-bubble-text">{live ? <LiveTurnText turn={turn} text={text} facts={facts} pick={pick} />
            : <TurnText turn={turn} text={text} facts={facts} pick={pick} />}{live ? <i className="debate-caret" aria-hidden="true" /> : null}</div>
          <Checks turn={turn} />
        </div>
      </Fragment>;
    })}
  </div>;
}

const ANALYST_STATE = { wait: "等待中", run: "正在写…", done: "✓ 完成", failed: "没写成" } as const;

/** ① 分析师写报告 n/4 → ② 多空辩论 → ③ 裁判结论, and about how long is left. */
function Stages({ debate }: { debate: Debate }) {
  const reports = debate.analysts ?? [];
  const written = reports.filter(report => report.status === "done" || report.status === "failed").length;
  const step = stepOf(debate);
  const stage = step === "judge" ? 3 : step?.startsWith("turn") ? 2 : 1;
  const names = [`① 分析师写报告 ${written}/${reports.length}`, "② 多空辩论", "③ 裁判结论"];
  return <div className="debate-stages" role="status" aria-label="辩论进度">
    <ol>{names.map((name, index) => <li key={index} data-state={index + 1 < stage ? "done" : index + 1 === stage ? "now" : undefined}
      aria-current={index + 1 === stage ? "step" : undefined}>{name}</li>)}</ol>
    <span className="debate-eta">{stage === 1 ? "约 2–3 分钟" : stage === 2 ? "还要约 1–2 分钟" : "快好了"}</span>
    <HelpTip helpKey="debate:stages" label="辩论分几步">{HELP.stages}</HelpTip>
  </div>;
}

/** The four analysts' cards: while the debate runs they fill in as they are written; after, each folds to its
 *  conclusion (the fold is `disclosure: debate-analyst-<key>`). */
function Analysts({ host, debate, facts, pick }: { host: DebateHost; debate: Debate; facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  const reports = debate.analysts ?? [];
  const byId = new Map(debate.facts.map(fact => [fact.id, fact]));
  const written = reports.filter(report => report.status === "done").length;
  return <SectionCard title="分析师报告" className="debate-analysts" hint={HELP.analysts} helpKey="debate:analysts"
    note={`${written}/${reports.length} 份${debate.searches ? ` · 全网补充 ${debate.searches} 次搜索` : ""}`}>
    <div className="debate-analyst-list">{reports.map(report => <AnalystCard key={report.key} report={report} stream={host.reports?.[report.key]}
      found={report.found.map(id => byId.get(id)).filter((fact): fact is DebateFact => Boolean(fact))} facts={facts} pick={pick} />)}</div>
  </SectionCard>;
}

function Line({ line, facts, pick }: { line: ReportLine; facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  return <Sentences items={[line]} facts={facts} pick={pick} />;
}

function LiveReport({ text, facts, pick }: { text: string; facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  return <Said text={useSmoothText(text, true)} facts={facts} pick={pick} />;
}

function AnalystCard({ report, stream, found, facts, pick }: { report: DebateReport; stream?: string; found: DebateFact[];
  facts: Map<number, DebateFact>; pick: (id: string) => void }) {
  const done = report.status === "done";
  const [open, setOpen] = useDisclosureSurface(`debate-analyst-${report.key}`, { available: done });
  const live = report.status === "run";
  const text = live && stream && stream.length >= report.text.length ? stream : report.text;
  const length = text.replace(MARKER, "").replace(/\s/g, "").length;
  const gaps = report.gaps.map(gap => gap.label);
  // What it lacked and what it found on the web: shown while it writes, and in the opened card.
  const sources = gaps.length > 0 ? <div className="debate-found">
    <p className="debate-analyst-note">调研页缺{gaps.join("、")}{found.length ? "，从全网补了：" : ""}</p>
    {found.length ? <ul aria-label={`${report.title}分析师全网补充的资料`}>{found.map(fact => <li key={fact.id}>
      <small>{fact.source}{factDay(fact.asOf) ? ` · ${factDay(fact.asOf)}` : ""}</small><span>{fact.label}</span></li>)}</ul> : null}
  </div> : null;
  const head = <>
    <b>{report.title}分析师</b>
    {report.status !== "wait" ? <small>{[report.model ? modelName(report.model) : "", length ? `${length} 字` : ""].filter(Boolean).join(" · ")}</small> : null}
    {found.length ? <span className="debate-found-chip">全网补充 {found.length} 条</span> : null}
    <span className="debate-analyst-state" data-status={report.status} title={report.error}>{ANALYST_STATE[report.status]}</span>
  </>;
  return <article className="debate-analyst" data-status={report.status} data-open={open || undefined} aria-label={`${report.title}分析师`}>
    {done ? <button type="button" className="debate-analyst-head" aria-expanded={open} aria-label={`${open ? "收起" : "展开"}${report.title}分析师的报告`}
      onClick={() => setOpen(!open)}>{head}<ChevronDown aria-hidden="true" /></button> : <div className="debate-analyst-head">{head}</div>}
    {done && report.conclusion ? <p className="debate-analyst-conclusion">结论：<strong><Line line={report.conclusion} facts={facts} pick={pick} /></strong></p> : null}
    {live ? <div className="debate-analyst-body">{sources}
      {text ? <p className="debate-analyst-text" data-live=""><LiveReport text={text} facts={facts} pick={pick} /><i className="debate-caret" aria-hidden="true" /></p> : null}
    </div> : null}
    {done && open ? <div className="debate-analyst-body">
      <p className="debate-analyst-text"><Sentences items={report.sentences} facts={facts} pick={pick} /></p>
      {report.points.length ? <StatTiles columns={report.points.length >= 4 ? 4 : report.points.length === 3 ? 3 : 2} label={`${report.title}要点`}
        items={report.points.map(point => ({ label: point.label ?? "", value: <Line line={point} facts={facts} pick={pick} /> }))} /> : null}
      {sources}
    </div> : null}
  </article>;
}

/** 上次复盘: the product's last settled debate, given to both sides and the judge. */
function Lesson({ debate }: { debate: Debate }) {
  if (!debate.lesson?.text) return null;
  return <p className="debate-lesson" data-outcome={debate.lesson.outcome}>
    <span>带入辩论的复盘：{debate.lesson.text}</span><HelpTip helpKey="debate:lesson" label="上次复盘">{HELP.lesson}</HelpTip>
  </p>;
}

/** The computer's 辩论 page: a tab of the 调研 panel since 2026-10-25 (it had its own rail button). One line names
 *  the debate and holds 停止 while it runs; the page body follows. */
export function DebateTab({ host }: { host: DebateHost }) {
  const debate = host.debate;
  const running = Boolean(debate && DEBATE_RUNNING.has(debate.status)) || (!debate && host.request?.status === "sending");
  const name = debate ? `${debate.name}${debate.symbol.split(".").pop()?.replace(/^[a-zA-Z]+/, "") ?? ""}` : host.symbol?.split(".").pop() ?? "";
  const topic = debate?.topic.kind ?? host.placeholder?.kind;
  // The panel's toolbar already names the contract: this line shows only once a debate is on (its topic and 停止).
  return <div className="debate-panel" data-agent-target="panel:debate">
    {debate || running ? <div className="debate-tab-head"><small>{name}{topic ? ` · ${topic === "event" ? "快讯" : "今日议题"}` : ""}</small>
      {running && debate ? <button type="button" className="app-button danger" onClick={host.cancel}>停止</button> : null}</div> : null}
    <DebateView host={host} bare />
  </div>;
}

/** The page body: the empty page (opened from the rail), progress while it runs, the result after. `compact`: the
 *  phone page (with its own back bar); `bare`: inside the computer's dock (its title bar is the dock's). */
export function DebateView({ host, compact = false, bare = false }: { host: DebateHost; compact?: boolean; bare?: boolean }) {
  const { debate, request } = host;
  const running = Boolean(debate && DEBATE_RUNNING.has(debate.status)) || (!debate && Boolean(request) && request?.status !== "refused" && request?.status !== "error");
  const refused = request?.status === "refused" || request?.status === "error";
  const status = debate?.status;
  const back = host.origin === "desk" ? "调研" : "快讯";
  const facts = new Map((debate?.facts ?? []).map(fact => [fact.n, fact]));
  const sourcesFold = useDisclosureSurface("debate-sources", { available: !compact && Boolean(debate) && !running });
  const sheetsFold = useDisclosureSurface("debate-sheets", { available: Boolean(debate) && !running });
  const [highlight, setHighlight] = useState<string>();
  const pick = (id: string) => { setHighlight(id); (compact ? sheetsFold[1] : sourcesFold[1])(true); };
  useEffect(() => {
    if (highlight) document.getElementById(`debate-fact-${highlight}`)?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [highlight, sheetsFold[0], sourcesFold[0]]); // eslint-disable-line react-hooks/exhaustive-deps
  const header = bare ? null : <header className="debate-head" data-compact="">
    <button type="button" className="debate-back" aria-label={`返回${back}`} onClick={host.close}><ChevronLeft aria-hidden="true" /></button>
    <strong>辩论{debate ? ` · ${debate.name}` : ""}</strong>
    <span className="debate-head-note">{!running && debate?.seconds ? `${Math.round(debate.seconds)} 秒` : ""}</span>
    {running && debate ? <button type="button" className="app-button danger" onClick={host.cancel}>停止</button> : null}
  </header>;
  const layout = compact ? "phone" : "panel";
  if (!debate && !request) return <section className="debate" data-layout={layout} aria-label="多 AI 辩论" data-testid="research-debate" data-status="empty" data-agent-target="research-debate">
    {header}
    <div className="debate-body"><Empty host={host} /></div>
  </section>;
  return <section className="debate" data-layout={layout} aria-label="多 AI 辩论" data-testid="research-debate" data-status={status ?? (refused ? "refused" : "starting")}
    data-agent-target="research-debate">
    {header}
    {debate ? <Roles debate={debate} streaming={host.stream?.turn} /> : null}
    <div className="debate-body">
      {refused && !debate ? <p className="debate-alert" role="alert">{request?.message}</p> : null}
      {request?.status === "reused" ? <p className="debate-note" role="status">{debate && DEBATE_RUNNING.has(debate.status) ? "这个议题正在辩，打开的是进行中的那一场"
        : debate?.topic.kind === "event" ? `这条快讯今天已辩过（${clock(debate.finishedAt ?? debate.createdAt)}），打开的是那次的结果`
        : "资料没变，打开的是已有结果"}</p> : null}
      {running ? <Follow>
        {debate?.analysts?.length ? <><Stages debate={debate} /><Analysts host={host} debate={debate} facts={facts} pick={pick} /><Lesson debate={debate} /></> : null}
        <Transcript host={host} facts={facts} pick={pick} />
        {debate ? <p className="debate-judge-note"><HelpTip helpKey="debate:progress" label="辩论怎么进行">{HELP.progress}</HelpTip>
          {debate.steps.find(step => step.key === "judge")?.status === "run" ? `裁判 ${roleModels(debate, "judge")} 正在判…`
            : `双方说完后，裁判 ${roleModels(debate, "judge")} 给出结论`}</p> : null}
      </Follow> : debate ? <Result host={host} debate={debate} compact={compact} facts={facts} pick={pick} highlight={highlight}
        folds={{ sheets: sheetsFold, sources: sourcesFold }} /> : null}
    </div>
  </section>;
}

/** Who speaks for whom (the model that actually spoke, a stand-in included), and where the debate is. */
function Roles({ debate, streaming }: { debate: Debate; streaming?: number }) {
  const live = debate.turns.find(turn => turn.status === "run")?.n ?? streaming;
  const judging = debate.steps.find(step => step.key === "judge")?.status === "run";
  const where = DEBATE_RUNNING.has(debate.status) ? judging ? "裁判在判" : live ? live <= 2 ? "第一轮 · 先各自表态" : "第二轮 · 互相反驳"
    : stepOf(debate) === "analysts" ? "分析师在写报告" : "整理资料"
    : debate.status === "done" || debate.status === "review" ? `${clock(debate.finishedAt)} 结束 · 用时 ${Math.round(debate.seconds ?? 0)} 秒`
    : debate.status === "cancelled" ? "已停止" : "没有完成";
  return <div className="debate-roles">
    {(["bull", "bear", "judge"] as const).map(role => <span key={role} className="debate-role" data-role={role}><i aria-hidden="true" />
      {role === "judge" ? "裁判" : SIDE_NAMES[role]} · {roleModels(debate, role)}</span>)}
    <span className="debate-where">{where}</span>
  </div>;
}

/** The page opened from the rail with no debate yet: start one on today's topic, or open an earlier one. */
function Empty({ host }: { host: DebateHost }) {
  const name = host.symbol?.split(".").pop() ?? "";
  return <>
    <SectionCard title="辩一轮" className="debate-start" hint={HELP.progress} helpKey="debate:start">
      <div className="section-card-body">
        <p className="debate-note">{host.symbol ? `让多方、空方两个 AI 就${name}的今日议题当面辩一轮，裁判给结论。` : "先在图表上选一个合约。"}</p>
        <div className="debate-actions" data-bare="">
          <button type="button" className="primary-button debate-primary" disabled={!host.symbol} onClick={() => host.start({ origin: "rail" })}>辩今日议题{name ? `（${name}）` : ""}</button>
        </div>
      </div>
    </SectionCard>
    <Recent host={host} />
  </>;
}

function Recent({ host }: { host: DebateHost }) {
  if (!host.recent?.length) return null;
  return <SectionCard title="以前的辩论" className="debate-recent" hint={HELP.recent} helpKey="debate:recent">
    <div className="debate-recent-grid">{host.recent.map(item => {
      const settled = item.settlement;
      // An older Runtime's summary carries no winner or reckoning: its tier stands in, and no outcome line.
      const outcome = settled === undefined ? "" : settled?.status === "settled" ? settled.outcome === "right" ? "5 日后：说对了" : settled.outcome === "wrong" ? "5 日后：说错了" : "不计入战绩"
        : settled?.status === "pending" ? "5 日后：待揭晓" : "不计入战绩";
      const result = item.status === "review" ? "需复核" : item.status === "done" ? (item.winner === undefined ? item.stance : winnerLine(item)) || "完成"
        : DEBATE_RUNNING.has(item.status) ? "辩论中" : item.status === "cancelled" ? "已停止" : "没有完成";
      return <button type="button" key={item.id} className="debate-recent-item" aria-label={`打开 ${day(item.researchDay)} ${item.name}的辩论（${result}${clock(item.finishedAt ?? item.createdAt) ? ` · ${clock(item.finishedAt ?? item.createdAt)}` : ""}）`} onClick={() => host.openDebate(item.id)}>
        <span className="debate-recent-day">{day(item.researchDay)} · {item.name}{item.topic.kind === "event" ? " · 快讯" : ""}</span>
        <span>{result}</span>
        <span className="debate-recent-outcome" data-outcome={settled?.status === "settled" ? settled.outcome ?? undefined : settled?.status}>{outcome}</span>
      </button>;
    })}</div>
  </SectionCard>;
}

function Fold({ name, title, summary, children, help, state }: { name: string; title: string; summary: ReactNode; children: ReactNode; help?: ReactNode;
  /** A fold the view opens itself (资料来源 from a citation chip) passes its own disclosure. */
  state?: [boolean, (open: boolean) => void] }) {
  const own = useDisclosureSurface(name, { available: !state });
  const [open, setOpen] = state ?? own;
  return <div className="debate-fold" data-open={open || undefined}>
    <div className="debate-fold-row">
      <button type="button" className="debate-fold-head" aria-expanded={open} aria-label={`${open ? "收起" : "展开"}${title}`} onClick={() => setOpen(!open)}>
        <b>{title}</b><span>{summary}</span><i>{open ? "收起" : "展开"}</i>
      </button>
      {help ? <HelpTip helpKey={`debate:${name}`} label={title}>{help}</HelpTip> : null}
    </div>
    {open ? <div className="debate-fold-body">{children}</div> : null}
  </div>;
}

/** 什么情况下这个结论就不成立: the frozen 失效位 on the winner's side, then the judge's own condition. */
function invalidationLine(debate: Debate): string {
  const verdict = debate.verdict;
  if (!verdict) return "";
  const parts = [verdict.invalidation !== null && verdict.winner ? `收盘${verdict.winner === "bull" ? "跌破" : "站上"} ${price(verdict.invalidation)}` : "", verdict.changeMind]
    .filter(Boolean);
  return parts.join("，或");
}

function Result({ host, debate, compact, facts, pick, highlight, folds }: { host: DebateHost; debate: Debate; compact: boolean; facts: Map<number, DebateFact>;
  pick: (id: string) => void; highlight?: string; folds: { sheets: [boolean, (open: boolean) => void]; sources: [boolean, (open: boolean) => void] } }) {
  const byId = new Map(debate.facts.map(fact => [fact.id, fact]));
  const verdict = debate.verdict;
  const sentences = new Map(debate.turns.flatMap(turn => turn.sentences).map(item => [item.id, item]));
  const ratio = split(debate);
  const adoptReason = debate.adopted ? null : debate.status !== "done" ? "需复核的辩论不能写进今日判断"
    : debate.today === false ? "只能把今天的辩论写进今日判断" : null;
  const ended = debate.status === "done" || debate.status === "review";
  const invalid = invalidationLine(debate);
  const conclusion = <SectionCard title={`裁判 · ${roleModels(debate, "judge")}`} className="debate-verdict" helpKey="debate:verdict"
    hint={<><p>{HELP.verdict}</p><p>{HELP.line}</p><p>{HELP.reckoning}</p></>} note={clock(debate.finishedAt)}>
    {verdict ? <div className="debate-verdict-body">
      <div className="debate-verdict-top">
        <strong className="debate-winner" data-side={verdict.winner ?? undefined}>{winnerLine(verdict)}</strong>
        <span className="debate-stance" data-stance={verdict.stance}>{verdict.stance} · 信心{verdict.confidence}</span>
        <HelpTip helpKey="debate:confidence" label="信心">{HELP.confidence}</HelpTip>
      </div>
      <div className="debate-meter" role="img" aria-label={`多 ${ratio} : 空 ${10 - ratio}`}>
        <span className="debate-bar"><i style={{ width: `${ratio * 10}%` }} /></span>
        <span className="debate-meter-legend"><span className="debate-up">多 {ratio}</span><span className="debate-down">空 {10 - ratio}</span></span>
      </div>
      <p className="debate-reason-text">{verdict.reason || verdict.headline}</p>
      {invalid ? <p className="debate-invalid">什么情况下这个结论就不成立：{invalid}</p> : null}
      {verdict.watch.length ? <p className="debate-invalid">接下来看：{verdict.watch.join(" · ")}</p> : null}
      {verdict.disagreements?.length ? <div className="debate-verdict-list"><b>关键分歧</b>
        <ul>{verdict.disagreements.map(item => <li key={item}>{item}</li>)}</ul></div> : null}
      {verdict.scenarios?.length ? <div className="debate-verdict-list"><b>如果……那么……</b>
        <ul>{verdict.scenarios.map(item => <li key={item}>{item}</li>)}</ul></div> : null}
    </div> : <div className="section-card-body"><p className="debate-alert" role="status">{debate.unsupportedVerdict
      // The judge did answer, but no adopted sentence with its own evidence backs the winner (#744).
      ? `需复核：裁判判${debate.unsupportedVerdict.winner ? `${SIDE_NAMES[debate.unsupportedVerdict.winner]}胜出` : "双方相当"}，但采纳的句子里没有带引用的依据，不给档位、不计入对账`
      : "需复核：裁判的回答没能读出，不给档位、不计入对账"}</p></div>}
    <div className="debate-actions">
      <button type="button" className="primary-button debate-primary" disabled={!verdict || Boolean(adoptReason) || host.adopting || Boolean(debate.adopted)}
        onClick={host.adopt}>{debate.adopted ? `已写进今日判断（${debate.adopted.stance}）` : host.adopting ? "正在写入…" : "写进今日判断"}</button>
      <button type="button" className="app-button debate-again" onClick={() => host.rerun()}><RefreshCw aria-hidden="true" />再辩一轮</button>
      {!compact && host.ask ? <button type="button" className="debate-ghost" onClick={host.ask}>在对话里接着问</button> : null}
      {adoptReason && !debate.adopted && verdict ? <span className="debate-reason">{adoptReason}</span> : null}
      {host.adoptMessage ? <span className="debate-reason" role="status">{host.adoptMessage}</span> : null}
      {host.adoptError ? <span className="debate-reason" role="alert" data-error="">{host.adoptError}</span> : null}
    </div>
  </SectionCard>;
  const replay = debate.turns.length ? <SectionCard title="交锋回看" className="debate-replay" hint={HELP.exchange} helpKey="debate:exchange"
    note={`${debate.checks?.arguments ?? sentences.size} 句 · 引用 ${debate.checks?.cited ?? 0} 处资料${debate.checks?.unverified ? ` · ${debate.checks.unverified} 句未核对` : ""}`}>
    <ol className="debate-replay-list">{debate.turns.map(turn => {
      const failed = turn.sentences.filter(item => !item.verified).length;
      return <li key={turn.n} data-side={turn.side}>
        <span className="debate-replay-who">{SIDE_NAMES[turn.side]}</span>
        <span className="debate-replay-text"><TurnText turn={turn} facts={facts} pick={pick} />
          {failed ? <small className="debate-replay-flag">（{failed === 1 ? "一处" : `${failed} 处`}未核对）</small> : null}
          {!turn.sentences.length ? <small>（没有说出能核对的句子）</small> : null}</span>
      </li>;
    })}</ol>
  </SectionCard> : null;
  const picks = verdict ? [...verdict.adopted.map(item => ({ ...item, adopted: true })), ...verdict.rejected.map(item => ({ ...item, adopted: false }))] : [];
  const judge = verdict && picks.length ? <Fold name="debate-judge" title="裁判怎么取舍" summary={`采纳 ${verdict.adopted.length} · 驳回 ${verdict.rejected.length}`} help={HELP.judge}>
    <div className="debate-picks">{picks.map(item => <Pick key={item.id} adopted={item.adopted} who={`${SIDE_NAMES[sentences.get(item.id)?.side ?? item.side]} · ${item.id}`}
      text={(sentences.get(item.id)?.text ?? "").replace(MARKER, "")} reason={item.reason} />)}</div>
  </Fold> : null;
  const sheetSummary = debate.sheets.map(sheet => `${sheet.title} ${SHEET_MARK[sheet.status]}${sheet.status === "partial" && sheet.missing.length ? `（缺${sheet.missing.join("、")}）` : ""}`).join(" · ");
  const sources = <ol className="debate-sources">{debate.facts.map(fact => <li key={fact.id} id={`debate-fact-${fact.id}`} data-highlight={highlight === fact.id || undefined} data-kind={fact.kind}>
    <span className="debate-number">{fact.section === "flash" ? "讯" : fact.n}</span>
    <span className="debate-source-text"><span><span className="debate-source-label">{fact.label}</span> <b>{fact.value}</b></span>
      <small>{fact.source}{fact.asOf ? ` · ${fact.asOf.length > 10 ? `${factDay(fact.asOf)} ${clock(fact.asOf)}` : fact.asOf}` : ""}{fact.status === "stale" ? " · 旧值，来源未刷新" : ""}
        {safe(fact.url) ? <> · <a href={safe(fact.url)} target="_blank" rel="noopener noreferrer">原文</a></> : null}</small></span>
  </li>)}</ol>;
  const mapping = debate.mapping ? `裁判看到的 A 方是${SIDE_NAMES[debate.mapping.A]}、B 方是${SIDE_NAMES[debate.mapping.B]}` : "裁判看到的是 A/B 方";
  const log = <div className="debate-log">
    <p>{mapping}；这次共 {debate.calls.length} 次调用，实际 {debate.tokens.toLocaleString("zh-CN")} token。</p>
    <ol>{debate.calls.map((call, index) => <li key={index}>
      <span className="debate-log-head">{call.role === "judge" ? "裁判" : call.role === "analyst" ? "分析师" : SIDE_NAMES[call.role]} · {stageName(call.stage)} · {modelName(call.model)}（{call.model}）· {call.tokens.toLocaleString("zh-CN")} token · {call.seconds}s{call.fallbackFrom ? ` · 顶替 ${modelName(call.fallbackFrom)}（${fallbackReason(call.fallbackReason)}）` : ""}{call.error ? ` · 失败：${call.error}` : ""}</span>
      {call.output ? <pre>{typeof call.output === "string" ? call.output : JSON.stringify(call.output, null, 1)}</pre> : null}
    </li>)}</ol>
  </div>;
  return <>
    {debate.status === "failed" || debate.status === "cancelled" ? <>
      <p className="debate-alert" role="alert">{debate.status === "cancelled" ? "这次辩论已停止。" : `这次辩论没能完成：${debate.error ?? "未知错误"}`}可以再辩一轮。</p>
      <div className="debate-actions" data-bare=""><button type="button" className="app-button debate-again" onClick={() => host.rerun()}><RefreshCw aria-hidden="true" />再辩一轮</button></div>
    </> : null}
    {ended ? conclusion : null}
    {debate.analysts?.length ? <Analysts host={host} debate={debate} facts={facts} pick={pick} /> : null}
    <Lesson debate={debate} />
    {replay}
    {ended ? <Recent host={host} /> : null}
    {judge}
    {compact ? <Fold name="debate-sheets" title="底稿与来源" summary={`${debate.facts.length} 条`} help={HELP.sheets} state={folds.sheets}>
      <p className="debate-fold-line">{sheetSummary}</p>{sources}
    </Fold> : <>
      <Fold name="debate-sheets" title="五份底稿" summary={sheetSummary} help={HELP.sheets} state={folds.sheets}>
        <ul className="debate-sheet-list">{debate.sheets.map(sheet => <li key={sheet.key} data-status={sheet.status}>
          <b>{sheet.title}</b><span>{SHEET_MARK[sheet.status]}{sheet.missing.length ? `（缺${sheet.missing.join("、")}）` : ""}</span>
          <small>{sheet.factIds.map(id => byId.get(id)).filter(Boolean).map(fact => fact!.section === "flash" ? "快讯" : `${fact!.n}`).join("、") || "—"}</small>
        </li>)}</ul>
      </Fold>
      <Fold name="debate-sources" title="资料来源" summary={`${debate.facts.length} 条 · 原文与时间`} state={folds.sources}>{sources}</Fold>
    </>}
    <Fold name="debate-stats" title="研讨战绩" summary={host.stats ? `已对账 ${host.stats.settled} 次` : "读取中"} help={HELP.reckoning}>
      <StatsBody stats={host.stats} debate={debate} />
    </Fold>
    <Fold name="debate-log" title="研讨记录" summary={`多方 ${roleModels(debate, "bull")} · 空方 ${roleModels(debate, "bear")} · 裁判 ${roleModels(debate, "judge")}（裁判看到的是 A/B 方）· ${debate.calls.length} 次调用 · 实际 ${debate.tokens.toLocaleString("zh-CN")} token`}>{log}</Fold>
  </>;
}

/** One of the judge's picks: the sentence in full (two lines, then 展开 when it is longer) and the reason. */
function Pick({ adopted, who, text, reason }: { adopted: boolean; who: string; text: string; reason: string }) {
  const [open, setOpen] = useState(false);
  const [long, setLong] = useState(false);
  const body = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const element = body.current;
    if (!element || open) return;
    const measure = () => setLong(element.scrollHeight > element.clientHeight + 1);
    measure();
    // A narrower panel wraps more lines: measure again when the row's width changes.
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(measure);
    observer?.observe(element);
    return () => observer?.disconnect();
  }, [open, text, reason]);
  return <div className="rule-row debate-pick" data-kind={adopted ? undefined : "filter"}>
    <span className="rule-row-period">{adopted ? "采纳" : "驳回"}</span>
    <span className="rule-row-text">
      <span className="debate-pick-text" ref={body} data-open={open || undefined}>{who} · <strong>{text}</strong>{reason ? <small>（{reason}）</small> : null}</span>
      {long || open ? <button type="button" className="debate-pick-more" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "收起" : "展开"}</button> : null}
    </span>
  </div>;
}

const ANALYST_TITLES: Record<string, string> = { technical: "技术面", supply: "供需基本面", flow: "资金持仓", news: "新闻宏观" };

function stageName(stage: string) {
  const turn = /^turn(\d)$/.exec(stage);
  const analyst = /^(analyst|search):(\w+)$/.exec(stage);
  if (analyst) return `${ANALYST_TITLES[analyst[2]] ?? analyst[2]}${analyst[1] === "search" ? " · 定搜索词" : "报告"}`;
  return turn ? `第 ${turn[1]} 次发言` : stage === "round1" ? "第一轮" : stage === "round2" ? "第二轮" : "裁决";
}

const safe = (value?: string) => { try { const url = new URL(value ?? ""); return ["https:", "http:"].includes(url.protocol) ? url.href : undefined; } catch { return undefined; } };

function StatsBody({ stats, debate }: { stats?: DebateStats; debate: Debate }) {
  const settlement = debate.settlement;
  const mine = settlement ? <p className="debate-fold-line">这次：{settlement.status === "settled"
    ? `已对账（${Object.entries(settlement.outcomes).map(([who, outcome]) => `${WHO[who as keyof typeof WHO]}${outcome === "right" ? "对" : "错"}`).join("、")}）`
    : settlement.status === "void" ? `不计${settlement.reason ? `（${settlement.reason}）` : ""}`
    : `${day(settlement.due)} 收盘对账 · 发起时价格 ${price(settlement.entryPrice)}${settlement.entrySource === "close" ? `（休市，${day(settlement.tradingDay)} 收盘价）` : settlement.entrySource === "last" ? "（休市，休市前最新价）" : ""}${settlement.invalidation !== null ? ` · 失效位 ${price(settlement.invalidation)}` : ""}${settlement.deskStance ? ` · 当时的今日判断 ${settlement.deskStance}` : settlement.deskMissing ? " · 当时还没有今天的今日判断，单模型这场不计" : ""}`}</p> : null;
  if (!stats) return <>{mine}<p className="debate-fold-line">读取中…</p></>;
  return <>
    {mine}
    {stats.rates ? <StatTiles columns={4} label="命中率" items={(["bull", "bear", "judge", "desk"] as const).map(who => ({ label: WHO[who],
      value: stats.rates![who].total ? `${Math.round(stats.rates![who].right / stats.rates![who].total * 100)}%` : "—",
      note: `${stats.rates![who].right} / ${stats.rates![who].total}` }))} />
      : <p className="debate-fold-line">已对账 {stats.settled} 次 · 待对账 {stats.pending} 次</p>}
  </>;
}
const WHO = { bull: "多方模型", bear: "空方模型", judge: "裁判", desk: "单模型今日判断" } as const;
