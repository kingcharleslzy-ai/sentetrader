import { ChevronRight, CircleHelp, Mic, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { groupRounds, realized, type AccountPnlData, type PnlMonth, type PnlRound } from "../accountPnl";
import { chartSymbolOf, requestJson, sameContract, type AccountSnapshot } from "../api";
import { useAccountPnl } from "../hooks/useAccountPnl";
import type { TradeNote } from "../hooks/useTradeReview";
import { useDialogSurface, useDisclosureSurface, useUiSurface } from "../uiSurfaces";
import type { FeeSchedule } from "../strategyApi";
import { ForeignFeeNote, openFeeSettings } from "./StrategyWorkbench";
import { AccountEquityCurve, dayLabel, equityCurveSummary } from "./AccountEquityCurve";
import { SectionCard, SideBySide, StatTiles } from "./Blocks";
import { Segmented } from "./settings/controls";
import { formatNumber, formatSigned, isForeignSymbol, signClass } from "./futures/support";
import { saveTradeNote, TradeJournalCard, UnlinkedNotes } from "./TradeJournal";
import { useDailyReview } from "../dailyReview";
import { DailyReviewBlock, RoundTags } from "./DailyReview";
import { HabitCompareBlock, habitState, useHabitCompare } from "./HabitCompare";
import { ReviewBreakdownBlock } from "./ReviewBreakdown";
import "./AccountPnl.css";

const time = (value: number) => new Date(value / 1_000_000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
const monthLabel = (month: string) => `${Number(month.slice(0, 4))} 年 ${Number(month.slice(5, 7))} 月`;
const sign = signClass;
const money = (value: number | null | undefined) => value === null || value === undefined ? "—" : formatSigned(value, 0);
type Currency = "CNY" | "USD";
const CURRENCY_LABELS: Record<Currency, string> = { CNY: "人民币", USD: "美元" };

/**
 * The 复盘 view as three blocks: 账户 (total P&L against the principal — equity-based, so it
 * includes open positions and fees — beside today and this month's closed trades), 权益曲线
 * (one point per stored trading day), and 平仓记录 by month → trading day → trade. A contract
 * filter keeps the old per-contract review; clicking a trade focuses the chart on it. The 总盈亏
 * figure opens the account detail card; the fee icon explains the current fee basis.
 */
export function AccountPnl({ baseUrl, rootUrl, symbol, chartSymbols, chartAvailable = true, accountId, foreignAccountId, snapshots, onFocus, onSelectSymbol, initialBalance, onInitialBalanceChange, onVoiceReview, voiceReviewing = false, compact = false }: {
  baseUrl: string; rootUrl: string; symbol: string; accountId?: string | null;
  /** History belongs to the ledger, even when its contract has no subscribable chart. */
  chartSymbols?: string[]; chartAvailable?: boolean;
  /** 本站模拟合成一个账户 (owner 2026-10-21): the 外盘 USD sub-account behind the session's RMB 本站模拟 account.
   *  With it the panel offers 人民币 / 美元: the two 总盈亏 always side by side, the rest of the panel on the chosen ledger. */
  foreignAccountId?: string | null;
  /** The two ledgers' live snapshots (可用 / 保证金 in 账户明细). */
  snapshots?: { CNY?: AccountSnapshot; USD?: AccountSnapshot };
  /** The phone's 复盘摘要: the 今日复盘 tiles two a row. */
  compact?: boolean;
  /** 语音复盘 (2026-09-29): the voice call walks through the day's notable trades; absent without voice. */
  onVoiceReview?: () => void; voiceReviewing?: boolean;
  onFocus: (range: { openTime: number; closeTime: number; direction: string } | undefined) => void;
  onSelectSymbol: (symbol: string) => void;
  initialBalance: number | null; onInitialBalanceChange: (value: number | null) => void;
}) {
  const hasUsd = Boolean(foreignAccountId);
  const [chosen, setChosen] = useState<Currency>("CNY");
  const currency: Currency = hasUsd ? chosen : "CNY";
  const activeId = currency === "USD" ? foreignAccountId : accountId;
  const { pnl, error } = useAccountPnl({ baseUrl, accountId: activeId });
  // The other ledger only for its 总盈亏 beside this one (and its column in 账户明细).
  const other = useAccountPnl({ baseUrl, accountId: currency === "USD" ? accountId : foreignAccountId, enabled: hasUsd });
  const ledgers: Record<Currency, AccountPnlData | undefined> = currency === "USD" ? { CNY: other.pnl, USD: pnl } : { CNY: pnl, USD: other.pnl };
  // 今日复盘 (2026-10-06): the block on top; only a Runtime that answers the route reports it.
  const daily = useDailyReview({ baseUrl, accountId: activeId });
  const dailyReview = daily.data?.review;
  // 习惯对照 (2026-10-25): the last 90 days of this ledger against only its 计划内 trades.
  const habits = useHabitCompare({ baseUrl, accountId: activeId });
  const [filter, setFilter] = useState("");
  const [habitNotice, setHabitNotice] = useState("");
  const [selected, setSelected] = useState("");
  const [openMonth, setOpenMonth] = useState<string>();
  const [openDay, setOpenDay] = useState<string>();
  const detailDialog = useRef<HTMLDialogElement>(null);
  const feeDialog = useRef<HTMLDialogElement>(null);
  // 账户明细 can be asked for before the P&L has loaded (顶栏权益 opens 复盘 and asks at once): the card mounts with
  // the data, so the request waits until it is there.
  const [detailWanted, setDetailWanted] = useState(false);
  useDialogSurface("account-detail", detailDialog, { open: () => setDetailWanted(true) });
  useDialogSurface("fee-hint", feeDialog);
  useDisclosureSurface("voice-review", { available: Boolean(onVoiceReview), state: voiceReviewing,
    apply: open => { if (open && !voiceReviewing) onVoiceReview?.(); } });
  // A Runtime/account change drops the selection; callback identity is not a reason to clear it.
  useEffect(() => { setSelected(""); onFocus(undefined); }, [baseUrl, activeId]); // eslint-disable-line react-hooks/exhaustive-deps
  const chooseCurrency = (next: Currency) => {
    if (next === "USD" && !hasUsd) throw new Error("没有外盘美元账");
    setChosen(next); setFilter(""); setOpenMonth(undefined); setOpenDay(undefined);
  };
  useEffect(() => {
    if (!detailWanted || !detailDialog.current) return;
    if (!detailDialog.current.open) detailDialog.current.showModal();
    setDetailWanted(false);
  }, [detailWanted, pnl]);
  const symbols = useMemo(() => [...new Set((pnl?.trades ?? []).map(t => t.symbol))].sort(), [pnl]);
  const trades = useMemo(() => (pnl?.trades ?? []).filter(t => !filter || t.symbol === filter), [pnl, filter]);
  const months = useMemo(() => groupRounds(trades), [trades]);
  const currentMonth = openMonth ?? months[0]?.key;
  const month = months.find(m => m.key === currentMonth);
  const currentDay = openDay && month?.days.some(d => d.key === openDay) ? openDay : month?.days[0]?.key;
  const wins = trades.filter(t => realized(t) > 0).length;
  const select = (trade: PnlRound) => {
    setSelected(trade.id);
    const target = chartSymbolOf(trade.symbol);
    const available = chartSymbols === undefined || chartSymbols.some(item => item.toUpperCase() === target.toUpperCase());
    if (available && !sameContract(trade.symbol, symbol)) onSelectSymbol(target);
    onFocus(available ? trade : undefined);
  };
  /** Opens the trade's month and day in 平仓记录 and selects it (the review command and 习惯对照's picks). */
  const selectById = (id: string) => {
    const trade = (pnl?.trades ?? []).find(t => t.id === id);
    if (!trade) return false;
    if (filter && trade.symbol !== filter) setFilter("");  // a contract filter must not hide the chosen trade
    setOpenMonth(trade.tradingDay.slice(0, 7)); setOpenDay(trade.tradingDay);
    select(trade);
    return true;
  };
  const detail = trades.find(t => t.id === selected);
  const detailChart = detail && chartSymbolOf(detail.symbol);
  const chartNotice = !detail ? undefined
    : chartSymbols && !chartSymbols.some(item => item.toUpperCase() === detailChart?.toUpperCase()) ? "该历史合约暂无可用图表，仍可查看和补充记录。"
    : !chartAvailable ? "图表行情暂不可用，仍可查看和补充记录。"
    : detailChart !== detail.symbol ? "图表使用对应品种的主连，逐笔价格以这笔记录为准。" : undefined;
  // A saved 补充 shows at once; the 5 s poll brings the Runtime's copy, which then wins.
  const [savedNotes, setSavedNotes] = useState<Record<string, TradeNote | null>>({});
  useEffect(() => setSavedNotes({}), [pnl]);
  const detailNote = detail ? (detail.id in savedNotes ? savedNotes[detail.id] : detail.note ?? null) : null;
  // 交易验尸 (2026-10-25): the review's questions are answered into the round trips' 补充, the same notes as 这笔交易.
  const noteTexts = useMemo(() => {
    const texts: Record<string, string> = {};
    for (const trade of pnl?.trades ?? []) if (trade.note?.text) texts[trade.id] = trade.note.text;
    for (const [id, note] of Object.entries(savedNotes)) texts[id] = note?.text ?? "";
    return texts;
  }, [pnl, savedNotes]);
  const answerQuestion = async (roundId: string, text: string) => {
    if (!baseUrl) return false;
    try {
      const saved = await saveTradeNote(baseUrl, activeId, roundId, text);
      setSavedNotes(current => ({ ...current, [roundId]: saved }));
      return true;
    } catch { return false; }
  };
  const detailQuestion = detail ? dailyReview?.legs.find(leg => leg.roundId === detail.id && leg.question)?.question ?? undefined : undefined;
  const monthClosed = pnl?.thisMonthTradeCount !== 0;
  const basis = pnl?.costSource === "account" ? "毛收益" : "净收益";
  const winRate = `${trades.length ? `${Math.round(wins / trades.length * 100)}%` : "—"} / ${trades.length}`;
  const tiles = pnl ? [
    { label: "总盈亏", value: money(pnl.totalPnl), note: "权益 − 本金" },
    { label: "今日", value: money(pnl.today), note: "账户当日净盈亏" },
    { label: "本月平仓", value: monthClosed ? money(pnl.thisMonth) : "—", note: monthClosed ? `已平仓${basis}` : "本月还没有平仓" },
    { label: "胜率 / 笔数", value: winRate, note: "近 366 天完整配对" },
  ] : [];
  const curve = pnl?.equityCurve;
  // Until the P&L has loaded there is nothing to expand or select: a command sent
  // meanwhile stays pending in the registry and lands once `apply` is registered.
  // The 账户 tiles and the curve summary go out only from a Runtime that sends the curve, the 未关联的补充
  // count only from one that sends the list (an older Runtime's state schema forbids the fields).
  useUiSurface("review", { state: { currency, currencies: hasUsd ? ["CNY", "USD"] : ["CNY"],
    month: currentMonth ?? null, day: currentDay ?? null, filter, selectedTradeId: selected || null,
    selectedNote: detailNote?.text ?? null, ...(curve && pnl ? { tiles, equityCurve: equityCurveSummary(curve, pnl.initialBalance) } : {}),
    ...(pnl?.unlinkedNotes ? { unlinkedNotes: pnl.unlinkedNotes.length } : {}),
    ...(habits ? { habits: habitState(habits) } : {}),
    ...(daily.data ? { dailyReview: dailyReview ? { tradingDay: dailyReview.tradingDay, legs: dailyReview.stats.legs,
      planned: dailyReview.stats.planned, checks: dailyReview.legs.filter(leg => leg.checks?.length).length,
      observation: dailyReview.observation?.status ?? null } : null } : {}) },
  apply: !pnl ? undefined : value => {
    const command = value as { expand?: { month?: string; day?: string }; filter?: string; select?: string | null; reviewDay?: string; currency?: Currency };
    if (command.currency) { chooseCurrency(command.currency); return; }
    if (command.reviewDay) {
      // Before the first read lands the day is simply asked for; once read, only a day that has a review.
      if (daily.data && !daily.data.days.includes(command.reviewDay)) throw new Error("这个交易日没有今日复盘");
      daily.setDay(command.reviewDay);
    } else if (command.expand?.month) {
      if (!months.some(m => m.key === command.expand!.month)) throw new Error("该月没有平仓记录");
      setOpenMonth(command.expand.month); setOpenDay(undefined);
    } else if (command.expand?.day) {
      const owner = months.find(m => m.days.some(d => d.key === command.expand!.day));
      if (!owner) throw new Error("该交易日没有平仓记录");
      setOpenMonth(owner.key); setOpenDay(command.expand.day);
    } else if (command.filter !== undefined) {
      if (command.filter && !symbols.includes(command.filter)) throw new Error("该合约没有平仓记录");
      setFilter(command.filter); setOpenMonth(undefined); setOpenDay(undefined);
    } else if ("select" in command) {
      if (command.select === null) { setSelected(""); onFocus(undefined); return; }
      if (!command.select || !selectById(command.select)) throw new Error("没有这笔平仓记录");
    }
  } }, [currentMonth, currentDay, filter, selected, months, trades, symbols, pnl, detailNote, winRate, daily.data, dailyReview, currency, hasUsd, habits]);
  return <section className="strategy-trade-review account-pnl" aria-label="账户复盘">
    <header><h3>账户复盘</h3>
      <select aria-label="合约筛选" value={filter} onChange={event => { setFilter(event.target.value); setOpenMonth(undefined); setOpenDay(undefined); }}>
        <option value="">全部合约</option>{symbols.map(item => <option key={item} value={item}>{item}</option>)}
      </select>
      {onVoiceReview && <button type="button" className="account-pnl-voice-review" aria-pressed={voiceReviewing}
        data-agent-target="voice-review" onClick={onVoiceReview} disabled={voiceReviewing}>
        <Mic size={14} aria-hidden="true" />{voiceReviewing ? "复盘中" : "语音复盘"}</button>}
      {hasUsd && <Segmented label="复盘币种" value={currency} options={[{ value: "CNY", label: "人民币" }, { value: "USD", label: "美元" }] as const}
        onChange={chooseCurrency} />}</header>
    <DailyReviewBlock data={daily.data} onDay={daily.setDay} onRegenerate={() => void daily.regenerate()}
      onAnswer={verdict => void daily.answer(verdict)} busy={daily.busy} notice={daily.notice} compact={compact}
      notes={noteTexts} onQuestion={answerQuestion} />
    <HabitCompareBlock data={habits} onSelect={id => setHabitNotice(selectById(id) ? "" : "这笔已不在近 366 天的平仓记录里")} />
    <ReviewBreakdownBlock data={pnl?.breakdown} />
    {habitNotice && <p className="account-pnl-status" role="status">{habitNotice}</p>}
    {pnl && <>
      <SectionCard title="账户" className="account-pnl-account" note={`总盈亏按账户权益算，含持仓浮动与手续费；本月平仓和按月、按日只算已平仓的交易${hasUsd ? "。人民币账记国内合约，美元账记外盘合约，两边不换汇、不相加" : ""}`}>
        {hasUsd && <SideBySide>{(["CNY", "USD"] as const).map(key => <button type="button" key={key} className="account-pnl-total account-pnl-ledger"
          aria-haspopup="dialog" aria-label={`总盈亏 · ${CURRENCY_LABELS[key]}，查看账户明细`} onClick={() => detailDialog.current?.showModal()}>
          <small>总盈亏 · {CURRENCY_LABELS[key]}</small><span className={sign(ledgers[key]?.totalPnl)}>{money(ledgers[key]?.totalPnl)}</span></button>)}</SideBySide>}
        <StatTiles columns={2} items={[
          hasUsd ? { label: `权益 · ${CURRENCY_LABELS[currency]}`, value: pnl.equity === null ? "—" : formatNumber(pnl.equity, 0), note: currency === "USD" ? "外盘美元账" : "国内人民币账" }
            : { label: "总盈亏", value: <button type="button" className="account-pnl-total" aria-haspopup="dialog" aria-label="总盈亏，查看账户明细"
            onClick={() => detailDialog.current?.showModal()}><span className={sign(pnl.totalPnl)}>{money(pnl.totalPnl)}</span><ChevronRight size={14} aria-hidden="true" /></button>,
          note: tiles[0].note },
          { label: "今日", value: <span className={sign(pnl.today)}>{money(pnl.today)}</span>, note: tiles[1].note },
          { label: "本月平仓", value: <span className={monthClosed ? sign(pnl.thisMonth) : ""}>{tiles[2].value}</span>,
            note: <>{tiles[2].note}<button type="button" className="account-pnl-fee-hint" aria-haspopup="dialog" aria-label="手续费口径" title="手续费口径" onClick={() => feeDialog.current?.showModal()}><CircleHelp size={13} /></button></> },
          { label: "胜率 / 笔数", value: winRate, note: tiles[3].note },
        ]} />
        {pnl.dataStatus !== "normal" && <p className="account-pnl-status" role="status">账户快照{pnl.dataStatus === "stale" ? "已陈旧" : "暂不可用"}，权益取最近记录。</p>}
      </SectionCard>
      <SectionCard title="权益曲线" className="account-pnl-curve" note="每个交易日收盘后的账户权益；虚线是本金">
        <AccountEquityCurve points={curve ?? []} principal={pnl.initialBalance} />
      </SectionCard>
      <SectionCard title="平仓记录" className="account-pnl-closed" note="按平仓的交易日归类，夜盘成交记在下一个交易日">
        <ul className="account-pnl-months" aria-label="按月">
          {months.map(item => <li key={item.key}>
            <button type="button" className={item.key === currentMonth ? "active" : ""} aria-expanded={item.key === currentMonth} onClick={() => { setOpenMonth(item.key); setOpenDay(undefined); }}>
              <strong>{monthLabel(item.key)}</strong><span>{item.tradeCount} 笔 · 胜率 {item.tradeCount ? Math.round(item.wins / item.tradeCount * 100) : 0}%</span><b className={sign(item.realizedNet)}><small>平仓</small>{money(item.realizedNet)}</b>
            </button>
            {item.key === currentMonth && <MonthDays month={item} currentDay={currentDay} onOpenDay={setOpenDay} selected={selected} onSelect={select} />}
          </li>)}
          {!months.length && <li className="account-pnl-empty">还没有完整的开平仓记录。</li>}
        </ul>
      </SectionCard>
      {detail && <TradeJournalCard key={`${baseUrl}:${activeId}:${detail.id}`} trade={detail} baseUrl={baseUrl} accountId={activeId} note={detailNote}
        question={detailQuestion}
        onNoteSaved={(id, note) => setSavedNotes(current => ({ ...current, [id]: note }))}
        onShowAll={() => { setSelected(""); onFocus(undefined); }} />}
      {chartNotice && <p className="account-pnl-status" role="status">{chartNotice}</p>}
      <UnlinkedNotes key={`${baseUrl}:${activeId}`} notes={pnl.unlinkedNotes ?? []} baseUrl={baseUrl} accountId={activeId} />
      {pnl.skippedSymbols.length > 0 && <p>{pnl.skippedSymbols.join("、")} 的合约参数已不可用，未计入。</p>}
      <AccountDetailCard dialog={detailDialog} ledgers={hasUsd ? ledgers : { CNY: pnl }} snapshots={snapshots}
        initialBalance={initialBalance} onInitialBalanceChange={onInitialBalanceChange} />
      <FeeHintCard dialog={feeDialog} rootUrl={rootUrl} symbol={symbol} costSource={pnl.costSource} />
    </>}
    {!pnl && !error && <p>正在读取账户盈亏</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

function MonthDays({ month, currentDay, onOpenDay, selected, onSelect }: {
  month: PnlMonth; currentDay: string | undefined; onOpenDay: (day: string) => void; selected: string; onSelect: (trade: PnlRound) => void;
}) {
  return <ul className="account-pnl-days" aria-label={`${monthLabel(month.key)}按日`}>
    {month.days.map(day => <li key={day.key}>
      <button type="button" className={day.key === currentDay ? "active" : ""} aria-expanded={day.key === currentDay} onClick={() => onOpenDay(day.key)}>
        <strong>{dayLabel(day.key)}</strong><span>{day.tradeCount} 笔</span><b className={sign(day.realizedNet)}>{money(day.realizedNet)}</b>
      </button>
      {day.key === currentDay && <ul className="account-pnl-trades" aria-label={`${dayLabel(day.key)}成交`}>
        {day.trades.map(trade => <li key={trade.id}>
          <button type="button" className={`strategy-review-trade ${selected === trade.id ? "selected" : ""}`} onClick={() => onSelect(trade)}>
            <span><strong>{trade.symbol} · {trade.direction === "long" ? "多" : "空"} {trade.volume} 手</strong><b className={sign(realized(trade))}>{money(realized(trade))}</b></span>
            <small>{formatNumber(trade.openPrice, 2)} → {formatNumber(trade.closePrice, 2)} · {time(trade.openTime)} — {time(trade.closeTime)}</small>
            <RoundTags open={trade.openTag} close={trade.closeTag} />
          </button>
        </li>)}
      </ul>}
    </li>)}
  </ul>;
}

function closeOnBackdrop(event: React.MouseEvent<HTMLDialogElement>) {
  if (event.target !== event.currentTarget) return;
  const box = event.currentTarget.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) event.currentTarget.close();
}

/** Account, principal (editable), total P&L, cumulative fees and current account value; with the 外盘 USD ledger
 *  (本站模拟合成一个账户, 2026-10-21) the two ledgers side by side, each with its 可用 / 保证金 / 今日. */
function AccountDetailCard({ dialog, ledgers, snapshots, initialBalance, onInitialBalanceChange }: {
  dialog: React.RefObject<HTMLDialogElement | null>; ledgers: { CNY?: AccountPnlData; USD?: AccountPnlData };
  snapshots?: { CNY?: AccountSnapshot; USD?: AccountSnapshot };
  initialBalance: number | null; onInitialBalanceChange: (value: number | null) => void;
}) {
  const two = "USD" in ledgers;
  const day = ledgers.CNY?.tradingDay ?? ledgers.USD?.tradingDay;
  const column = (key: Currency) => {
    const pnl = ledgers[key];
    const live = two ? snapshots?.[key] : undefined;
    return <dl className="account-detail-rows" key={key} aria-label={two ? `${CURRENCY_LABELS[key]}账` : undefined}>
      {two && <div className="account-detail-ledger"><dt>{key === "USD" ? "美元 · 外盘" : "人民币 · 国内"}</dt><dd /></div>}
      <div><dt>账户</dt><dd>{pnl?.accountName || "模拟账户"}</dd></div>
      <div><dt>本金</dt><dd>{key === "CNY"
        ? <><input aria-label="本金" type="number" min="0" step="10000" placeholder="1000000" value={initialBalance ?? ""} onChange={event => { const value = Number(event.target.value); onInitialBalanceChange(event.target.value === "" || !Number.isFinite(value) || value < 0 ? null : value); }} /><small>初始资金 + 后续入金，盈利不计</small></>
        : <>{pnl ? formatNumber(pnl.initialBalance, 0) : "—"}<small>开户资金</small></>}</dd></div>
      <div><dt>总盈亏</dt><dd className={sign(pnl?.totalPnl)}>{money(pnl?.totalPnl)}</dd></div>
      {live && <>
        <div><dt>今日盈亏</dt><dd className={sign(live.todayNetPnl)}>{money(live.todayNetPnl)}</dd></div>
        <div><dt>可用</dt><dd>{formatNumber(live.available, 0)}</dd></div>
        <div><dt>保证金</dt><dd>{formatNumber(live.margin, 0)}</dd></div>
      </>}
      <div><dt>累计手续费</dt><dd>{pnl?.cumulativeCommission === null || pnl?.cumulativeCommission === undefined ? "—" : formatNumber(pnl.cumulativeCommission, 2)}<small>{pnl?.commissionSince ? `自 ${pnl.commissionSince} 起累计` : "尚无账户日结记录"}</small></dd></div>
      <div><dt>当前账户总值</dt><dd>{pnl?.equity === null || pnl?.equity === undefined ? "—" : formatNumber(pnl.equity, 2)}</dd></div>
    </dl>;
  };
  return createPortal(<dialog ref={dialog} className={`market-session-card account-detail-card${two ? " two-ledgers" : ""}`} aria-label="账户明细" onClick={closeOnBackdrop}>
    <header><div><strong>账户明细</strong><small>{day ? `交易日 ${day}` : "账户快照暂不可用"}</small></div><button type="button" aria-label="关闭账户明细" onClick={() => dialog.current?.close()}><X size={18} /></button></header>
    {two ? <div className="account-detail-ledgers">{column("CNY")}{column("USD")}</div> : column("CNY")}
  </dialog>, document.body);
}

/** Which fee basis the realized figures use, with the way to the fee settings. */
function FeeHintCard({ dialog, rootUrl, symbol, costSource }: { dialog: React.RefObject<HTMLDialogElement | null>; rootUrl: string; symbol: string; costSource: string }) {
  const [schedule, setSchedule] = useState<FeeSchedule>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false; setSchedule(undefined); setFailed(false);
    void requestJson<FeeSchedule>(`${rootUrl}/api/trading-costs`).then(value => { if (!cancelled) setSchedule(value); }).catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [rootUrl, costSource]);
  const key = symbol.trim().toLowerCase();
  // 外盘合约的费用是外盘撮合引擎的每手美元手续费，国内费率表（含专用费率）对它不适用。
  const foreign = isForeignSymbol(symbol);
  const override = foreign ? undefined : schedule?.overrides.find(rate => rate.symbol.toLowerCase() === key) || schedule?.overrides.find(rate => rate.symbol.toLowerCase() === key.replace(/\d+$/, ""));
  const custom = schedule?.source === "custom";
  const rate = override ?? schedule?.default;
  return createPortal(<dialog ref={dialog} className="market-session-card account-fee-card" aria-label="手续费口径" onClick={closeOnBackdrop}>
    <header><div><strong>手续费口径</strong><small>复盘与模拟测算共用</small></div><button type="button" aria-label="关闭手续费口径" onClick={() => dialog.current?.close()}><X size={18} /></button></header>
    {foreign && <ForeignFeeNote url={rootUrl} symbol={symbol} />}
    {!schedule && !failed && <p>正在读取费率</p>}
    {failed && <p role="alert">费率读取失败</p>}
    {schedule && !custom && <p>{foreign ? "国内合约" : ""}默认手续费为 0，按毛收益显示；账户实际扣费以权益为准。</p>}
    {schedule && custom && rate && <>
      <p>自定义费率：{rate.mode === "per_lot" ? "元 / 手" : "成交金额 · 万分之"}，开仓 {rate.open} · 平今 {rate.closeToday} · 平昨 {rate.closeYesterday}。</p>
      <p>生效范围：{override ? `${override.symbol} 专用费率` : `${foreign ? "国内" : "全部"}合约默认费率${schedule.overrides.length ? `，另有 ${schedule.overrides.length} 条品种 / 合约专用费率` : ""}`}。</p>
    </>}
    <button type="button" className="strategy-primary account-fee-settings" onClick={() => { dialog.current?.close(); openFeeSettings(); }}>设置手续费</button>
  </dialog>, document.body);
}
