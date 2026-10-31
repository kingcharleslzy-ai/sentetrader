import { useEffect, useRef, useState, type ReactNode } from "react";
import { MessagesSquare, RefreshCw } from "lucide-react";
import { requestJson } from "../api";
import { useDebateVersion } from "../researchDebate";
import { LONG_READ_TIMEOUT_MS } from "../requestTimeout";
import { guestRequest } from "../guestApi";
import { useDisclosureSurface } from "../uiSurfaces";
import { SectionCard, SideBySide, StatTiles } from "./Blocks";
import { tone } from "./NewsEvents";
import "./ResearchDesk.css";

export type DeskFact = { id: string; section: string; kind: "data" | "report"; label: string; value: string; source: string; asOf: string | null; url?: string;
  /** 机构观点: the passage about this product and the note's own title (notes often cover several products). */
  detail?: string; title?: string;
  /** The source's read time; stale = an old value kept because the source did not refresh. */
  status?: "stale"; fetchedAt?: string; error?: string };
type DeskSection = { key: string; title: string; kind: "data" | "report"; status: "ok" | "stale" | "loading" | "unavailable"; error?: string; count: number; source?: string | null };
type Direction = "偏多" | "偏空" | "中性";
type DeskPoint = { text: string; basis: string[]; kind: "推断" | "推测"; grounds: "data" | "report" | "mixed";
  /** Words copied from a cited fact; the Runtime checked they are there. */
  quote?: string;
  /** 第七批: the price direction of a reason (points and risks; an older Runtime leaves it out). */
  direction?: Direction };
/** 关键数据 (第七批): one figure picked from the facts; `ref` is the fact it comes from. */
export type DeskTile = { label: string; value: string; color: "up" | "down" | "none"; note: string; ref: string };
export type DeskJudgment = {
  status: "ready" | "pending" | "disabled" | "insufficient" | "failed" | "budget_exhausted" | "signin_required";
  refreshing?: boolean; stance?: string; confidence?: string; headline?: string; headlineBasis?: string[];
  /** Why the confidence is this level (computed by the Runtime since 2026-10-12; older judgments have none). */
  confidenceBasis?: string;
  points?: DeskPoint[]; risks?: DeskPoint[]; watch?: DeskPoint[]; basis?: Record<string, Omit<DeskFact, "id">>;
  /** Cited ids whose current fact reads differently from the snapshot in `basis`. */
  changedBasis?: string[];
  generatedAt?: string; model?: string; error?: string;
  /** One judgment per research day (turns 08:30 Beijing); `today` false means an earlier day's. */
  researchDay?: string; today?: boolean; reason?: "daily" | "swan" | "manual" | "night" | "news" | "debate"; movedSincePercent?: number;
  /** 今日判断自动更新 (2026-10-24): why a news-triggered run happened, the research day's runs (newest first) and
   *  when the next automatic one may come. */
  trigger?: string;
  updates?: Array<{ generatedAt?: string; stance?: string; reason?: string; trigger?: string }>;
  nextAuto?: { nightAt: string | null; news: boolean };
  manualAvailableAt?: string;
  /** The last re-run failed (the judgment shown is the earlier one); `retryAt` is when a refresh may try again. */
  refreshError?: string; retryAt?: string;
  /** A re-run that waited for its facts and then could not run (too few facts, the day's cap…): the receipt. */
  rejudgeSkipped?: DeskRejudge;
  /** While one is being written: which step, since when, and the usual duration. */
  stage?: "facts" | "model"; startedAt?: string; expectedSeconds?: number;
  /** 第七批: the AI's one-sentence conclusion, the comparison with the previous research day, and the
   *  possibly-stale hint (重要 news since, or a newer news brief pointing the other way). */
  conclusion?: string;
  change?: { previousDay: string; previousStance: string; same: boolean; text: string };
  stale?: { count: number; newsNet: string | null };
};
/** 重新判断's receipt from the manual refresh: what happened, or why it could not run and from when. */
export type DeskRejudge = { status: "started" | "running" | "limited" | "unavailable" | "current"; message: string; availableAt?: string };
export type DeskReport = { symbol: string; productName: string; mainSymbol: string; generatedAt: string; sections: DeskSection[]; facts: DeskFact[]; judgment: DeskJudgment; notice: string;
  tiles?: DeskTile[];
  /** 新闻面: today's news brief (net direction and sentence); absent when there is none. */
  news?: { net: string; take: string; generatedAt?: string };
  rejudge?: DeskRejudge;
  /** The facts shown are being re-read (old, or not collected yet — sections 加载中): read again shortly (2026-10-22). */
  factsRefreshing?: boolean };
/** What the news panel's observation snapshot reports about this tab. */
export type DeskState = { status: "loading" | "ready" | "error"; judgment?: DeskJudgment["status"]; refreshing?: boolean; stance?: string; generatedAt?: string; factCount?: number;
  today?: boolean; stage?: DeskJudgment["stage"]; refreshError?: string; retryAt?: string;
  /** The last read or refresh request failed (e.g. HTTP 502); what is shown is the last report read. */
  readError?: string;
  /** 第七批: the possibly-stale hint, the change line, the figures and columns shown, the news line's
   *  direction, the last 重新判断 receipt and the 资料来源 entry highlighted. */
  stale?: DeskJudgment["stale"] | null; change?: string | null; tiles?: number; bull?: number; bear?: number; conclusion?: boolean;
  newsNet?: string | null; rejudge?: { status: DeskRejudge["status"] | "sending" | "error" | "done"; message: string } | null; highlight?: string | null };
/** A command from the news panel's surface (`news: {deskRejudge}` / `news: {deskSource}`); `nonce` makes a repeat count. */
export type DeskCommand = { kind: "rejudge" } | { kind: "source"; id: string };

const REFRESH_MS = 10 * 60 * 1000;
const WAIT_MS = 4000;
const WAIT_TRIES = 25;
/** A failed read retries after 4, 8, 16, 32, 64 seconds, then at the normal ten minutes. */
const READ_RETRIES = 5;
const REPORT_DAYS = 7;
const PHONE_TILES = 4;
const WAITING: Partial<Record<DeskJudgment["status"], string>> = {
  disabled: "当前档位未开通「联网研究」，还没有可看的 AI 判断；下面的数据照常可看。",
  insufficient: "行情缺失或事实不足三类，不生成 AI 判断。",
  failed: "这次没能生成 AI 判断，几分钟后刷新可重试。",
  budget_exhausted: "今天的 AI 判断次数已用完，明天恢复；下面的数据照常更新。",
  signin_required: "登录后可看 AI 判断；下面是数据与资料来源。",
};

const BEIJING = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
/** Why a judgment was written, as the card's 因为 line says it (今日判断自动更新, 2026-10-24). */
const REASONS: Record<string, string> = { daily: "今天第一次判断", swan: "行情剧烈波动", manual: "手动重判",
  night: "夜盘开盘前例行", news: "重要新闻", debate: "写入了 AI 辩论的结论" };
/** Market dates as sources give them (YYYY-MM-DD, YYYYMMDD) stay dates; instants show in Beijing time. */
const time = (value: string | null | undefined) => {
  if (!value) return "日期未知";
  const date = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(value);
  if (date) return `${date[2]}-${date[3]}`;
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return value;
  const parts = Object.fromEntries(BEIJING.formatToParts(instant).map(part => [part.type, part.value]));
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
};
/** The day a fact is about, for the 相关报道 window (unknown dates sort last and stay listed). */
const dayOf = (value: string | null | undefined) => {
  if (!value) return Number.NaN;
  const date = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(value);
  return date ? Date.parse(`${date[1]}-${date[2]}-${date[3]}T00:00:00+08:00`) : Date.parse(value);
};
const failureCode = (cause: unknown) => {
  const status = (cause as { status?: number }).status;
  return status ? `HTTP ${status}` : "网络错误";
};
const after = (seconds: number) => seconds >= 60 ? `${Math.round(seconds / 60)} 分钟` : `${seconds} 秒`;
const safeUrl = (value?: string) => {
  try { const url = new URL(value ?? ""); return ["https:", "http:"].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
};
const OPPOSITE: Record<string, Direction> = { 偏多: "偏空", 偏空: "偏多" };
/** A reason's direction; an older Runtime gives none: points go with the stance, risks against it. */
const directionOf = (point: DeskPoint, group: "points" | "risks", stance: string | undefined): Direction =>
  point.direction ?? (stance && OPPOSITE[stance] ? (group === "points" ? stance as Direction : OPPOSITE[stance]) : "中性");
/** A label without its bracketed qualifier or source tag, for a fold's one-line summary. */
const shortLabel = (label: string) => label.replace(/（[^）]*）/g, "").replace(/\s+[A-Z]{1,3}\d{3,4}$/, "").trim();
const uniq = (items: string[]) => [...new Set(items.filter(Boolean))];

/** One of the four folds at the bottom: a counted row that opens its rows (commandable as `disclosure: desk-…`). */
function DeskFold({ name, title, summary, count, children }: { name: string; title: string; summary: string; count: number; children: ReactNode }) {
  const [open, setOpen] = useDisclosureSurface(name, { available: count > 0 });
  return <DeskFoldView title={title} summary={summary} count={count} open={open} onToggle={() => setOpen(!open)}>{children}</DeskFoldView>;
}

function DeskFoldView({ title, summary, count, open, onToggle, children }: { title: string; summary: string; count: number; open: boolean; onToggle: () => void; children: ReactNode }) {
  if (!count) return null;
  return <div className="desk-fold" data-open={open || undefined}>
    <button type="button" className="desk-fold-head" aria-expanded={open} aria-label={`${open ? "收起" : "展开"}${title}`} onClick={onToggle}>
      <b>{title}</b><span>{summary}</span><i>{open ? "收起" : `展开 ${count} 项`}</i>
    </button>
    {open ? <div className="desk-fold-rows">{children}</div> : null}
  </div>;
}

/**
 * 调研 (第七批 2026-10-07, approved sample; 第 5 批 2026-10-25 drops the stance / confidence label): the judgment
 * first — one sentence, and a hint when news since says it may be out of date (only a
 * hint: 按最新消息重新判断 re-runs it through the manual refresh and its hourly limit) — then up to eight
 * key figures, the reasons split 偏多 | 偏空 with one small number each, the AI's conclusion and what to
 * watch, one 新闻面 line, and four folds: 基本面, 宏观 (the product category's only), 相关报道 (7 days) and
 * 资料来源 (every fact and report with its source, quoted text and date). `compact`: the phone's page —
 * four figures, the columns stacked, no numbers on the reasons; the rest is folded.
 */
export function ResearchDesk({ baseUrl, symbol, guest = false, compact = false, onGuestExpired, onState, onJudgment, onOpenFlash, command, reloadSignal = 0, onDebate }: {
  baseUrl: string | undefined; symbol: string; guest?: boolean; compact?: boolean; onGuestExpired?: () => void; onState?: (state: DeskState) => void;
  /** The judgment on screen as it changes (the phone's 就这份判断问 AI names it). */
  onJudgment?: (judgment: DeskJudgment | undefined) => void;
  /** The 新闻面 line's “去快讯看”: the 快讯 tab (desktop) or the page's 快讯 section (phone). */
  onOpenFlash?: () => void;
  command?: DeskCommand & { nonce: number };
  /** 下拉刷新 on the phone: read again now. */
  reloadSignal?: number;
  /** 多 AI 研讨: 让 AI 们辩一轮 on today's topic (absent for guests). */
  onDebate?: () => void;
}) {
  const [report, setReport] = useState<DeskReport>();
  /** The last request that failed: a read retried automatically in `retryIn` seconds, or a manual refresh. */
  const [failure, setFailure] = useState<{ symbol: string; code: string; retryIn?: number }>();
  const [refreshing, setRefreshing] = useState(false);
  const [rejudge, setRejudge] = useState<DeskState["rejudge"]>(null);
  // The judgment a 重新判断 started from: only a different one is a new judgment (#516).
  const rejudgeFrom = useRef<string | null>(null);
  const [highlight, setHighlight] = useState<string>();
  const [reload, setReload] = useState(0);
  const [tick, setTick] = useState(() => Date.now());
  // A debate written into 今日判断 (or one that ended) re-reads the judgment.
  const debates = useDebateVersion();
  const expired = useRef(onGuestExpired);
  expired.current = onGuestExpired;
  const current = report?.symbol === symbol && Array.isArray(report.facts) ? report : undefined;
  const failed = failure?.symbol === symbol ? failure : undefined;
  const error = !failed ? "" : failed.retryIn === undefined ? `刷新失败（${failed.code}），下面是上次读到的内容，可再点重新判断。`
    : `${current ? "调研读取失败" : "调研暂不可用"}（${failed.code}），约 ${after(failed.retryIn)}后自动重试${current ? "；下面是上次读到的内容" : ""}。`;
  const sourceRows = current?.facts.length ?? 0;
  const [sourcesOpen, setSourcesOpen] = useDisclosureSurface("desk-sources", { available: sourceRows > 0 });

  useEffect(() => {
    if (!baseUrl) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    let tries = 0;
    let misses = 0;
    const load = async () => {
      let delay = REFRESH_MS;
      try {
        const path = `/api/${guest ? "guest/" : ""}research/desk?symbol=${encodeURIComponent(symbol)}`;
        const next = await (guest ? guestRequest<DeskReport>(baseUrl, path, { timeoutMs: LONG_READ_TIMEOUT_MS }) : requestJson<DeskReport>(`${baseUrl}${path}`, { timeoutMs: LONG_READ_TIMEOUT_MS }));
        if (cancelled) return;
        setReport(next); setFailure(undefined); misses = 0;
        // A judgment being written, or facts being re-read: look again shortly, a bounded number of times.
        if ((next.judgment?.status === "pending" || next.judgment?.refreshing || next.factsRefreshing) && tries++ < WAIT_TRIES) delay = WAIT_MS;
      } catch (cause) {
        if (cancelled) return;
        if (guest && (cause as { status?: number }).status === 401) expired.current?.();
        // A failed read says nothing about the judgment: try again soon with a growing gap, a bounded number of times.
        else {
          if (misses < READ_RETRIES) delay = WAIT_MS * 2 ** misses++;
          setFailure({ symbol, code: failureCode(cause), retryIn: delay / 1000 });
        }
      }
      timer = setTimeout(load, delay);
    };
    void load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl, symbol, guest, reload, reloadSignal, debates]);

  const judgment = current?.judgment;
  const ready = judgment?.status === "ready";
  useEffect(() => { onJudgment?.(judgment); }, [judgment, onJudgment]);
  const reasons = ready ? [...(judgment.points ?? []).map(point => ({ point, group: "points" as const })),
    ...(judgment.risks ?? []).map(point => ({ point, group: "risks" as const }))]
    .map(item => ({ ...item, direction: directionOf(item.point, item.group, judgment.stance) })) : [];
  const bull = reasons.filter(item => item.direction === "偏多");
  const bear = reasons.filter(item => item.direction === "偏空");
  // An old 震荡 judgment (no directions) keeps its own two lists: 理由 | 风险.
  const undirected = !bull.length && !bear.length;
  const neutral = undirected ? [] : reasons.filter(item => item.direction === "中性");
  const tiles = current?.tiles ?? [];
  const left = undirected ? reasons.filter(item => item.group === "points").length : bull.length;
  const right = undirected ? reasons.filter(item => item.group === "risks").length : bear.length;
  const tileCount = tiles.length;
  useEffect(() => {
    onState?.(current ? { status: "ready", judgment: judgment?.status, refreshing: Boolean(judgment?.refreshing), stance: judgment?.stance,
      generatedAt: judgment?.generatedAt, factCount: current.facts.length, today: judgment?.today, stage: judgment?.stage,
      refreshError: judgment?.refreshError, retryAt: judgment?.retryAt, readError: failed?.code,
      stale: judgment?.stale ?? null, change: judgment?.change?.text ?? null, tiles: tileCount, bull: left, bear: right,
      conclusion: Boolean(judgment?.conclusion), newsNet: current.news?.net ?? null, rejudge, highlight: highlight ?? null }
      : { status: failed ? "error" : "loading", readError: failed?.code, rejudge, highlight: highlight ?? null });
  }, [current, judgment, failed, onState, rejudge, highlight, tileCount, left, right]);

  const refresh = async () => {
    if (!baseUrl || guest) return;
    setRefreshing(true);
    setRejudge({ status: "sending", message: "正在按最新消息重新判断…" });
    try {
      const next = await requestJson<DeskReport>(`${baseUrl}/api/research/desk/refresh?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
      rejudgeFrom.current = next.judgment?.generatedAt ?? null;
      setReport(next);
      setRejudge(next.rejudge ?? { status: "current", message: "已重读事实" });
      setReload(value => value + 1);
    } catch (cause) {
      setFailure({ symbol, code: failureCode(cause) });
      setRejudge({ status: "error", message: `重新判断没能发出（${failureCode(cause)}），可再点一次` });
    } finally { setRefreshing(false); }
  };
  const openSource = (id: string) => {
    setSourcesOpen(true);
    // An id the report does not list highlights nothing (a command for it then gets no receipt).
    if (current?.facts.some(fact => fact.id === id)) setHighlight(id);
  };
  // 资料来源 opens before the entry exists in the page: scroll once it renders.
  useEffect(() => {
    if (!highlight || !sourcesOpen) return;
    document.getElementById(`desk-fact-${highlight}`)?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [highlight, sourcesOpen]);
  const latest = useRef({ refresh, openSource });
  latest.current = { refresh, openSource };
  // Only a command that arrives while mounted runs (a remount for another product must not repeat the last one).
  const handled = useRef(command?.nonce ?? 0);
  useEffect(() => {
    if (!command || command.nonce === handled.current) return;
    handled.current = command.nonce;
    if (command.kind === "rejudge") void latest.current.refresh();
    else latest.current.openSource(command.id);
  }, [command]);

  const waiting = judgment && (judgment.status === "pending" || judgment.refreshing);
  // A re-run that started is over once the judgment no longer says it is being written.
  const failedRun = judgment?.status === "ready" ? judgment.refreshError : judgment?.status === "failed" ? judgment.error : undefined;
  useEffect(() => {
    if ((rejudge?.status === "started" || rejudge?.status === "running") && judgment && !waiting) {
      // No longer waiting is not success: the run may have failed, been skipped once its facts landed, or left the
      // same judgment in place (#516).
      const skipped = judgment.rejudgeSkipped;
      setRejudge(failedRun ? { status: "done", message: `重判没能完成（${failedRun}），仍显示之前的判断` }
        : skipped ? { status: skipped.status === "started" || skipped.status === "running" ? "unavailable" : skipped.status,
          message: `没有重新判断：${skipped.message}；仍显示之前的判断` }
        : (judgment.generatedAt ?? null) === rejudgeFrom.current ? { status: "unavailable", message: "没有生成新的判断，仍显示之前的判断" }
        : { status: "done", message: "已按最新消息重新判断" });
    }
  }, [rejudge, judgment, waiting, failedRun]);
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [waiting]);
  const elapsed = waiting && judgment?.startedAt ? Math.max(0, Math.round((tick - Date.parse(judgment.startedAt)) / 1000)) : 0;
  const expected = judgment?.expectedSeconds ?? 0;
  // While the last request failed, the judgment's state is not known: say the failure instead.
  const waitingText = !waiting || error ? "" : `${judgment?.stage === "facts" ? "正在补齐基本面数据，随后 AI 综合" : `AI 正在综合 ${current?.facts.length ?? 0} 条事实`}`
    + `，预计约 ${expected} 秒${elapsed ? `（已等 ${elapsed} 秒${elapsed > expected + 5 ? "，比平时久" : ""}）` : ""}`
    + (judgment?.status === "ready" ? "；下面先显示上一份判断。" : "。");
  const retryText = judgment?.retryAt && Date.parse(judgment.retryAt) > tick ? `${time(judgment.retryAt).slice(-5)} 后可再重判` : "可再点重新判断";
  if (!current) return error ? <p role="alert">{error}</p> : <p role="status">正在收集事实…</p>;

  const numbers = new Map(current.facts.map((fact, index) => [fact.id, index + 1]));
  const changed = new Set(judgment?.changedBasis ?? []);
  const cite = (ids: string[] | undefined) => (ids ?? []).map(id => {
    const number = numbers.get(id);
    const snapshot = judgment?.basis?.[id];
    return number ? <button type="button" key={id} className="desk-ref" data-changed={changed.has(id) ? "" : undefined}
      aria-label={`依据 ${number}${changed.has(id) ? "，生成后已更新" : ""}`}
      title={snapshot ? `${changed.has(id) ? "生成时：" : ""}${snapshot.label}：${snapshot.value}` : undefined}
      onClick={() => openSource(id)}>{number}</button>
      : <span key={id} className="desk-ref" data-stale="" title={snapshot ? `已更新：${snapshot.label}：${snapshot.value}` : "依据已更新"}>旧</span>;
  });
  const disabled = !baseUrl || guest || refreshing;
  const stale = ready && !waiting ? judgment.stale : undefined;
  const staleText = !stale ? "" : compact
    ? `${stale.count ? `之后又来了 ${stale.count} 条重要消息` : `新闻面转${stale.newsNet}`}，可能过时`
    : `判断之后${stale.count ? `又来了 ${stale.count} 条重要消息${stale.newsNet && OPPOSITE[stale.newsNet] && stale.newsNet !== judgment?.stance ? `，新闻面已转${stale.newsNet}` : ""}`
      : `新闻面转${stale.newsNet}，和判断不一致`}，这份判断可能过时了`;
  // 今日判断自动更新: when the next automatic run may come.
  const nextAuto = [judgment?.nextAuto?.nightAt ? "夜盘开盘前 20:45" : null, judgment?.nextAuto?.news ? "再有重要新闻时" : null]
    .filter(Boolean).join("，或");
  // 第 5 批 (owner 2026-10-25): the judgment is its sentence — no 偏多 / 偏空 + 信心 label as the conclusion (the
  // debate keeps its direction); the reasons below still split by the direction each one points.
  const verdict = judgment?.status === "ready" ? <>
    <p className="desk-headline">{judgment.headline}{compact ? <small className="desk-confidence"> · {time(judgment.generatedAt).slice(-5)}</small> : null}</p>
    {compact ? null : <div className="desk-auto">
      <p><span className="desk-auto-when">{judgment.today === false ? `${time(judgment.researchDay)} 的判断，今天还没更新`
        : `${time(judgment.generatedAt).slice(-5)} ${judgment.reason === "manual" || judgment.reason === "debate" ? "更新" : "自动更新"}`}</span>
        {judgment.today === false ? null : <span>因为：{judgment.trigger ?? REASONS[judgment.reason ?? "daily"] ?? "今天第一次判断"}</span>}</p>
      {nextAuto ? <p className="desk-auto-next"><span>下次自动更新</span><span>{nextAuto}</span></p> : null}
    </div>}
    {!compact && (judgment.updates?.length ?? 0) > 1 ? <div className="desk-updates" aria-label="今天的更新记录">
      <strong>今天的更新记录</strong>
      {judgment.updates?.map((update, index) => <p key={update.generatedAt ?? index}><span>{time(update.generatedAt).slice(-5)}</span>
        <span>{update.trigger ?? REASONS[update.reason ?? "daily"]}</span></p>)}
    </div> : null}
    {Math.abs(judgment.movedSincePercent ?? 0) >= 0.5
      ? <p className="desk-moved">生成后主力已{(judgment.movedSincePercent ?? 0) > 0 ? "涨" : "跌"} {Math.abs(judgment.movedSincePercent ?? 0).toFixed(1)}%，判断可能滞后；关键数据是最新的。</p> : null}
  </> : null;
  const staleBar = stale && !guest ? <div className="desk-stale" role="status">
    <span>{staleText}</span>
    <button type="button" onClick={() => void refresh()} disabled={disabled}>{compact ? "重新判断" : "按最新消息重新判断"}</button>
  </div> : null;
  // A request that failed is said by the read-error line above; the receipt line carries the Runtime's answers.
  const receipt = rejudge && rejudge.status !== "error" ? <p className="desk-rejudge" role="status" data-status={rejudge.status}>{rejudge.message}</p> : null;

  const shownTiles = compact ? [...tiles].sort((a, b) => phoneRank(a.ref) - phoneRank(b.ref)).slice(0, PHONE_TILES) : tiles;
  const tileBlock = shownTiles.length ? <StatTiles columns={compact ? 2 : 4} label="关键数据" items={shownTiles.map(tile => ({ label: tile.label,
    value: <button type="button" className="desk-tile" data-color={tile.color} aria-label={`${tile.label} ${tile.value}，看来源`} onClick={() => openSource(tile.ref)}>{tile.value}</button>,
    note: tile.note }))} /> : null;

  const item = ({ point, direction }: (typeof reasons)[number], index: number) => <li key={index} data-direction={direction}>
    <span>{point.text}</span>{compact ? null : cite(point.basis)}
  </li>;
  const column = (title: string, side: "bull" | "bear" | "points" | "risks", list: typeof reasons) => <div className="desk-side" data-side={side}>
    <h4>{title}</h4>
    {list.length ? <ul>{list.map(item)}</ul> : <p className="desk-none">没有</p>}
  </div>;
  const versus = ready && reasons.length ? <>
    <SideBySide>
      {undirected ? column("理由", "points", reasons.filter(entry => entry.group === "points")) : column(compact ? "偏多" : "偏多的理由", "bull", bull)}
      {undirected ? column("风险", "risks", reasons.filter(entry => entry.group === "risks")) : column(compact ? "偏空" : "偏空的理由", "bear", bear)}
    </SideBySide>
    {neutral.length && !compact ? <p className="desk-neutral"><b>中性：</b>{neutral.map((entry, index) => <span key={index}>{index ? "；" : ""}{entry.point.text}{cite(entry.point.basis)}</span>)}</p> : null}
    {judgment.conclusion ? <p className="desk-conclusion"><b>AI 的结论：</b>{judgment.conclusion}</p> : null}
    {!compact && judgment.watch?.length ? <ul className="desk-watch" aria-label="接下来关注">{judgment.watch.map((point, index) => <li key={index}>{point.text}{cite(point.basis)}</li>)}</ul> : null}
  </> : null;
  const newsline = current.news && !guest ? <div className="desk-newsline">
    <span className="news-net" data-tone={tone(current.news.net)}>新闻面{current.news.net}</span>
    {compact ? null : <span className="desk-take">{current.news.take}</span>}
    {onOpenFlash ? <button type="button" className="desk-go" onClick={onOpenFlash}>{compact ? "快讯 →" : "去快讯看 →"}</button> : null}
  </div> : null;

  const factRow = (fact: DeskFact) => {
    const url = safeUrl(fact.url);
    return <div key={fact.id} className="desk-row">
      <span className="desk-row-k">{fact.kind === "report" ? fact.label : shortLabel(fact.label) || fact.label}</span>
      <span className="desk-row-v">{fact.value}</span>
      <span className="desk-row-d">{time(fact.asOf)}{url ? <> · <a href={url} target="_blank" rel="noopener noreferrer">原文</a></> : null}</span>
    </div>;
  };
  const inSections = (...keys: string[]) => current.facts.filter(fact => keys.includes(fact.section));
  const fundamentals = inSections("fundamental", "official", "related");
  const macro = inSections("macro");
  const since = tick - REPORT_DAYS * 86_400_000;
  const reports = inSections("research", "report").filter(fact => !(dayOf(fact.asOf) < since))
    .sort((a, b) => (dayOf(b.asOf) || 0) - (dayOf(a.asOf) || 0));
  const quotesOf = (id: string) => uniq([...(judgment?.points ?? []), ...(judgment?.risks ?? []), ...(judgment?.watch ?? [])]
    .filter(point => point.basis.includes(id) && point.quote).map(point => point.quote ?? ""));
  const loading = current.sections.filter(section => section.status === "loading" && !section.count);
  const missing = current.sections.filter(section => !section.count && section.status !== "loading");
  const newest = reports[0]?.asOf;

  return <div className="research-desk" data-testid="research-desk" data-layout={compact ? "phone" : "panel"}>
    {compact ? <section className="desk-phone-verdict" aria-label="今日判断">
      {waitingText ? <p role="status" className="desk-waiting">{waitingText}</p> : null}
      {error ? <p role="alert" className="desk-moved">{error}</p> : null}
      {verdict ?? (waitingText || error ? null : <p role="status" className="desk-waiting">{waitingMessage(judgment, retryText)}</p>)}
      {staleBar}{receipt}
      {onDebate && !guest ? <button type="button" className="debate-secondary desk-phone-debate" onClick={onDebate} disabled={!baseUrl}>让 AI 们辩一轮</button> : null}
    </section> : <SectionCard title="今日判断" className="desk-judgment"
      actions={guest ? null : <>
        {onDebate ? <UltraButton onClick={onDebate} disabled={!baseUrl}>让 AI 们辩一轮</UltraButton> : null}
        <button type="button" className="desk-refresh" onClick={() => void refresh()} disabled={disabled} aria-label="重新判断"
          data-busy={refreshing ? "" : undefined} title={refreshing ? "判断中" : "重读事实；判断距上次满 1 小时才重算"}><RefreshCw /></button>
      </>}>
      <div className="section-card-body desk-verdict">
        {waitingText ? <p role="status" className="desk-waiting">{waitingText}</p> : null}
        {error ? <p role="alert" className="desk-moved">{error}</p> : null}
        {judgment?.status === "ready" && judgment.refreshError
          ? <p role="status" className="desk-moved">上次重判失败（{judgment.refreshError}），仍显示之前的判断；{retryText}。</p> : null}
        {verdict ?? (waitingText || error ? null : <p role="status">{waitingMessage(judgment, retryText)}</p>)}
        {staleBar}{receipt}
      </div>
    </SectionCard>}
    {tileBlock ? compact ? tileBlock : <SectionCard title="关键数据" className="desk-tiles" note="点数字看来源；灰字是数据日期">{tileBlock}</SectionCard> : null}
    {versus ? compact ? <section className="desk-phone-versus" aria-label="多空对照">{versus}</section>
      : <SectionCard title="多空对照" className="desk-versus" note="每条后面的编号是依据的数据或报道"><div className="section-card-body">{versus}</div></SectionCard> : null}
    {newsline}
    {loading.length ? <p role="status" className="desk-waiting">{loading.map(section => section.title).join("、")}加载中…</p> : null}
    <DeskFold name="desk-fundamental" title="基本面" count={fundamentals.length}
      summary={uniq(fundamentals.map(fact => fact.section === "related" ? "关联品种" : shortLabel(fact.label))).slice(0, 4).join(" · ")}>
      {fundamentals.map(factRow)}
    </DeskFold>
    <DeskFold name="desk-macro" title="宏观" count={macro.length} summary={`只列和这个品种有关的：${uniq(macro.map(fact => shortLabel(fact.label))).slice(0, 3).join(" · ")}`}>
      {macro.map(factRow)}
    </DeskFold>
    <DeskFold name="desk-reports" title="相关报道" count={reports.length} summary={`近 ${REPORT_DAYS} 天 ${reports.length} 篇${newest ? `，最新 ${time(newest).slice(0, 5)}` : ""}`}>
      {reports.map(fact => {
        const url = safeUrl(fact.url);
        return <div key={fact.id} className="desk-row" data-kind="report">
          <span className="desk-row-k">{fact.label}</span>
          <span className="desk-row-v">{url ? <a href={url} target="_blank" rel="noopener noreferrer">{fact.section === "research" ? fact.title || fact.value : fact.value}</a>
            : fact.section === "research" ? fact.title || fact.value : fact.value}</span>
          <span className="desk-row-d">{time(fact.asOf)}</span>
        </div>;
      })}
    </DeskFold>
    <DeskFoldView title="资料来源" count={sourceRows} open={sourcesOpen} onToggle={() => { setSourcesOpen(!sourcesOpen); if (sourcesOpen) setHighlight(undefined); }}
      summary={`${sourceRows} 项数据与报道的出处、原文摘录和数据日期`}>
      <ol className="desk-sources">{current.facts.map(fact => {
        const url = safeUrl(fact.url);
        const quotes = quotesOf(fact.id);
        const snapshot = changed.has(fact.id) ? judgment?.basis?.[fact.id] : undefined;
        return <li key={fact.id} id={`desk-fact-${fact.id}`} data-kind={fact.kind} data-highlight={highlight === fact.id || undefined}>
          <span className="desk-number">{numbers.get(fact.id)}</span>
          <span className="desk-source-text">
            <span className="desk-source-main"><span>{fact.label}</span><strong>{fact.value}</strong></span>
            {/* 机构观点 show their passage; media reports only their headline and link (owner decision 2026-09-28). */}
            {fact.section === "research" && fact.detail && fact.detail !== fact.value ? <span className="desk-passage">{fact.detail}</span> : null}
            {quotes.map(quote => <small key={quote} className="desk-quote">判断引用：「{quote}」</small>)}
            {snapshot ? <small className="desk-quote" data-changed="">生成后已更新，生成时：{snapshot.label} {snapshot.value}</small> : null}
            <small>{fact.source} · {time(fact.asOf)}{fact.title ? ` · 摘自《${fact.title}》` : ""}{fact.status === "stale" ? ` · 旧值${fact.fetchedAt ? `（${time(fact.fetchedAt)} 采集）` : ""}，来源未刷新` : ""}
              {url ? <> · <a href={url} target="_blank" rel="noopener noreferrer">阅读原文</a></> : null}</small>
          </span>
        </li>;
      })}</ol>
      <div className="desk-provenance">
        <p>{current.notice}</p>
        <p>事实整理于 {time(current.generatedAt)}{current.mainSymbol ? ` · 主力 ${current.mainSymbol}` : ""}{judgment?.model ? ` · 判断模型 ${judgment.model}` : ""}；生成时 AI 只能引用当时编号的事实，数字对不上的论点已被去掉，这不是交易建议。</p>
        {missing.length ? <p>暂未取到：{missing.map(section => section.title).join("、")}</p> : null}
      </div>
    </DeskFoldView>
  </div>;
}

/** The phone's four figures: the price, the foreign counterpart, RSI and the macro one first (the sample's choice). */
function phoneRank(ref: string) {
  const order = ["M.price", "M.foreign", "M.indicators"];
  const index = order.indexOf(ref);
  if (index >= 0) return index;
  return /^(E\.|O\.(fred|eia))/.test(ref) ? 3 : 4 + ["M.trend", "D.spot_basis", "D.position_rank", "M.position"].indexOf(ref);
}

function waitingMessage(judgment: DeskJudgment | undefined, retryText: string) {
  return judgment?.status === "failed" && judgment.retryAt
    ? `这次没能生成 AI 判断（${judgment.error ?? "未知错误"}），${retryText}。` : WAITING[judgment?.status ?? "pending"] ?? "AI 判断暂不可用。";
}

/** 让 AI 们辩一轮 (owner 2026-10-24, modelled on Codex's Ultra): a blue → violet pill with twinkling white stars. */
const STARS = [[10, 30, 0], [22, 70, 0.6], [38, 22, 1.1], [52, 64, 0.3], [66, 34, 1.4], [78, 72, 0.9], [90, 40, 0.2]];
function UltraButton({ onClick, disabled, children }: { onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return <button type="button" className="ultra-button" onClick={onClick} disabled={disabled}>
    {STARS.map(([left, top, delay]) => <i key={left} aria-hidden="true" style={{ left: `${left}%`, top: `${top}%`, animationDelay: `${delay}s` }} />)}
    <MessagesSquare aria-hidden="true" /><span>{children}</span>
  </button>;
}
