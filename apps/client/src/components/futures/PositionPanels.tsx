import { ChevronDown, ChevronUp, History } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { sameContract, type AccountOverviewSnapshot, type AccountPosition, type AiStopLoss, type InstrumentCatalog, type MarketQuoteSnapshot, type SimulationOrder, type StopMotion } from "../../api";
import { StopMotionFields } from "./StopMotionFields";
import type { RoundTrip, TradeReviewData } from "../../hooks/useTradeReview";
import { useDialogSurface, useDisclosureSurface } from "../../uiSurfaces";
import "./PositionsStrip.css";
import { AiMenu, type AskAi } from "../AiMenu";
import { contractLabel, decimalsForTick, formatNumber, formatSigned, isProtectionActive, orderActionLabel, orderStatusText, ownPositions, positionKey, positionsMarketValue, signClass } from "./support";
import { deliveryBadgeText } from "./deliveryInfo";
import { protectionIssueText, protectionStatus } from "../../operationEvents";
import { notSentReason } from "../cards/CloseBatchProposalCard";
import { readNumber, STOP_PRICE_RULE, TAKE_PRICE_RULE } from "../../numberField";
import "./ContractInfoControl.css";

export type OrderActionHandler = (order: SimulationOrder, action: "continue" | "abandon" | "query" | "cancel") => void;

export function ActivityPanel({
  account,
  simulationOrders,
  busy,
  onOrderAction
}: {
  account?: AccountOverviewSnapshot;
  simulationOrders: SimulationOrder[];
  busy: boolean;
  onOrderAction: OrderActionHandler;
}) {
  const rows = simulationOrders.length ? simulationOrders : [];
  return (
    <section className="fdw-review-panel">
      <header><History />当日交易轨迹</header>
      <div className="fdw-review-summary">
        <span>账户权益<strong>{formatNumber(account?.account.equity, 0)}</strong></span>
        <span>今日盈亏<strong className={signClass(account?.account.todayPnl ?? 0)}>{formatNumber(account?.account.todayPnl, 0)}</strong></span>
        <span>成交回读<strong>{account?.activity.trades.length ?? 0} 笔</strong></span>
      </div>
      <div className="fdw-review-list">
        {account?.activity.trades.map((trade) => (
          <div key={trade.tradeId}>
            <time>{new Date(trade.tradeDateTime / 1_000_000).toLocaleTimeString("zh-CN", { hour12: false })}</time>
            <strong>{trade.symbol}</strong>
            <span>{trade.direction} · {trade.offset}</span>
            <b>{formatNumber(trade.price, 2)} × {trade.volume}</b>
          </div>
        ))}
        {!account?.activity.trades.length ? <p>当前没有可回读的成交记录。历史挂单簿未留存，因此不会伪造复盘盘口。</p> : null}
      </div>
      {rows.length ? (
        <div className="fdw-live-orders">
          <strong>本地委托链</strong>
          {rows.slice(0, 5).map((order) => {
            const unknown = order.displayStatus === "结果未知" || order.cancelPhase === "UNKNOWN";
            const alive = order.remoteStatus === "ALIVE" && order.cancelPhase === "NONE";
            return (
              <div key={order.clientOrderId}>
                <span>{orderActionLabel(order)}<small>{orderStatusText(order)}</small></span>
                <b>{order.request.limitPrice} × {order.request.volume}</b>
                {unknown ? <button type="button" disabled={busy} onClick={() => onOrderAction(order, "query")}>查询</button>
                  : alive ? <button type="button" disabled={busy} onClick={() => onOrderAction(order, "cancel")}>撤单</button> : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

/** 持仓 / 委托 / 成交; `trades` (平仓记录, before 2026-09-25) is still accepted as a command and maps to `fills`. */
export type PositionsBarTab = "positions" | "orders" | "fills";

export function orderIsUnknown(order: SimulationOrder): boolean {
  return order.displayStatus === "结果未知" || order.cancelPhase === "UNKNOWN";
}

/** Mirrors the Runtime's `active=true` filter: working at the exchange, not yet confirmed, or outcome unknown. */
export function orderIsActive(order: SimulationOrder): boolean {
  if (["PREPARED", "CALL_STARTED", "UNKNOWN"].includes(order.submitPhase) || order.cancelPhase === "UNKNOWN") return true;
  return order.remoteStatus === "ALIVE";
}

/** Dropping a prepared (never sent) order is final; ask once. */
function AbandonOrderDialog({ order, busy, onClose, onConfirm }: { order?: SimulationOrder; busy: boolean; onClose: () => void; onConfirm: (order: SimulationOrder) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (order) ref.current?.showModal(); else ref.current?.close(); }, [order]);
  useDialogSurface("abandon-order", ref, { closeOnly: true, close: onClose });
  return (
    <dialog ref={ref} className="replay-setup fdw-quick-order-dialog" aria-labelledby="abandon-order-title" data-testid="abandon-order-dialog"
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
      <div className="replay-setup-body">
        <h2 id="abandon-order-title">放弃这张待发送委托？</h2>
        {order ? <p>{order.request.symbol} · {orderActionLabel(order)} · {order.request.limitPrice} × {order.request.volume}。放弃后不会发送到模拟网关，也不能恢复。</p> : null}
        <div className="confirm-grid">
          <button type="button" disabled={busy} onClick={onClose}>取消</button>
          <button type="button" className="app-button danger" disabled={busy || !order} onClick={() => order && onConfirm(order)}>确认放弃</button>
        </div>
      </div>
    </dialog>
  );
}

const closeTime = (value: number) => new Date(value / 1_000_000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

const TRAILING_UNITS = { ticks: "跳", percent: "%", atr_chandelier: "倍 ATR", bar_extreme: "根 K 线" } as const;

/** “ · 追踪 10 跳 · 保本” after an armed stop's levels: how it moves (2026-10-25); nothing for a fixed one. */
export function stopMotionText(stop: Pick<AiStopLoss, "trailing" | "breakeven">): string {
  const parts = [stop.trailing ? `追踪${stop.trailing.mode === "bar_extreme" ? " 前" : ""} ${stop.trailing.value} ${TRAILING_UNITS[stop.trailing.mode]}` : "", stop.breakeven ? "保本" : ""];
  return parts.filter(Boolean).map(part => ` · ${part}`).join("");
}

/** What the inline 保护 form asks the Runtime for (POST /api/ai-execution/stops). */
export type PositionStopRequest = {
  positionBucket: "today" | "historical" | "all";
  volume: number;
  triggerPrice?: number | null;
  takeProfitPrice?: number | null;
} & StopMotion;

/**
 * 下单顺带保护 · 持仓补挂 (2026-10-02): one position row's 保护 area — the armed lines (strategy
 * ones read-only, manual ones with 撤销) and the inline form (disclosure `position-protection`,
 * id = position:<symbol>:<direction>). 止损/止盈 至少一个；方向与最小变动的检查和 Runtime 同一套
 * 中文口径，余下的（非本合约行情、仓量已变）由 Runtime 用 422 中文拒绝并转述。
 */
/** 现价 of a position row: the chart's live quote for its own contract, else the position's own
 *  latest price from the account read (#803) — every row shows 开仓均价 → 现价, not only the chart's. */
function positionLastPrice(position: AccountPosition, quote?: MarketQuoteSnapshot): number | undefined {
  if (quote && quote.symbol.toUpperCase() === position.symbol.toUpperCase()) return quote.lastPrice;
  return position.lastPrice ?? undefined;
}

function PositionProtection({ position, quote, priceTick, stops, busy, onCreate, onCancel }: {
  position: AccountPosition;
  quote?: MarketQuoteSnapshot;
  priceTick: number | null | undefined;
  stops: AiStopLoss[];
  busy: boolean;
  onCreate: (position: AccountPosition, request: PositionStopRequest) => Promise<boolean | void>;
  onCancel: (stopId: string) => Promise<boolean | void>;
}) {
  const [open, setOpen] = useDisclosureSurface("position-protection", { id: `position:${positionKey(position)}` });
  const decimals = decimalsForTick(priceTick);
  const tick = priceTick ?? 1;
  const exchange = position.symbol.split(".")[0]?.toUpperCase();
  const separate = exchange === "SHFE" || exchange === "INE";
  const today = Math.max(0, (position.todayVolume ?? 0) - (position.frozenToday ?? 0));
  const historical = Math.max(0, (position.historicalVolume ?? 0) - (position.frozenHistorical ?? 0));
  const whole = Math.max(0, position.volume - (position.frozenToday ?? 0) - (position.frozenHistorical ?? 0));
  // SHFE/INE close today and yesterday separately, so a stop protects one bucket (the Runtime refuses
  // "all" with close_bucket_required); with both held it starts on 昨仓, the position carried overnight.
  const [bucket, setBucket] = useState<"today" | "historical" | "all">(separate ? historical > 0 || today <= 0 ? "historical" : "today" : "all");
  const volume = bucket === "today" ? today : bucket === "historical" ? historical : whole;
  const [stopInput, setStopInput] = useState("");
  const [takeInput, setTakeInput] = useState("");
  const [stopMotion, setStopMotion] = useState<StopMotion>({});
  // shortcut: a position row has no chart period of its own, so its ATR chandelier measures on 5-minute bars; pass the
  // chart's period down if users ask for another.
  const atrTimeframe = 300;
  // Read with the ticket's rule (#789): a typed 0 / -1 is refused with its reason, never taken as 不设.
  const stopRead = readNumber(stopInput, STOP_PRICE_RULE);
  const takeRead = readNumber(takeInput, TAKE_PRICE_RULE);
  const stopPrice = stopRead.problem === null ? stopRead.value ?? undefined : undefined;
  const takePrice = takeRead.problem === null ? takeRead.value ?? undefined : undefined;
  const offTick = (value: number) => Math.abs(value / tick - Math.round(value / tick)) > 1e-6;
  const last = positionLastPrice(position, quote);
  const long = position.direction === "long";
  const issue = (() => {
    const typed = stopRead.problem ?? takeRead.problem;
    if (typed) return typed;
    if (stopPrice !== undefined) {
      if (offTick(stopPrice)) return `止损 ${stopPrice} 不在最小变动价位上`;
      if (last !== undefined && (long ? stopPrice >= last : stopPrice <= last))
        return long ? `止损要低于现价 ${formatNumber(last, decimals)}` : `止损要高于现价 ${formatNumber(last, decimals)}`;
    }
    if (takePrice !== undefined) {
      if (offTick(takePrice)) return `止盈 ${takePrice} 不在最小变动价位上`;
      if (last !== undefined && (long ? takePrice <= last : takePrice >= last))
        return long ? `止盈要高于现价 ${formatNumber(last, decimals)}` : `止盈要低于现价 ${formatNumber(last, decimals)}`;
    }
    return undefined;
  })();
  const submit = async () => {
    if (issue || (stopPrice === undefined && takePrice === undefined) || volume <= 0) return;
    const ok = await onCreate(position, {
      positionBucket: bucket, volume,
      triggerPrice: stopPrice ?? null, takeProfitPrice: takePrice ?? null,
      ...(stopPrice !== undefined ? stopMotion : {})
    });
    if (ok !== false) {
      setOpen(false);
      setStopInput("");
      setTakeInput("");
      setStopMotion({});
    }
  };
  return (
    <div className="fdw-row-protect">
      {stops.map(stop => (
        <span key={stop.stopId} className="fdw-protect-line" data-testid="position-protect-line">
          {stop.source === "strategy" ? "策略保护 " : ""}
          {[
            stop.triggerPrice !== null ? `止损 ${formatNumber(stop.triggerPrice, decimals)}` : null,
            stop.takeProfitPrice !== null ? `止盈 ${formatNumber(stop.takeProfitPrice, decimals)}` : null
          ].filter(Boolean).join(" / ")}{stopMotionText(stop)} · {stop.status === "ARMED" ? `已挂 ${stop.volume} 手` : `${stop.volume} 手 · ${protectionStatus(stop.status)}`}
          {stop.source === "manual" && stop.status === "ARMED"
            ? <button type="button" data-testid="cancel-stop" disabled={busy} onClick={() => void onCancel(stop.stopId)}>撤销</button>
            : null}
          {stop.status === "ARMED" && protectionIssueText(stop.lastError)
            ? <small className="fdw-protect-issue" data-testid="position-protect-blocked">{protectionIssueText(stop.lastError)}</small>
            : null}
        </span>
      ))}
      {open ? (
        <div className="fdw-protect-form" data-testid="position-protect-form">
          <label>止损<input aria-label="止损价" data-testid="row-stop-input" type="number" inputMode="decimal" min="0" step={tick} placeholder="不设" value={stopInput} onChange={(event) => setStopInput(event.target.value)} /></label>
          <label>止盈<input aria-label="止盈价" data-testid="row-take-input" type="number" inputMode="decimal" min="0" step={tick} placeholder="不设" value={takeInput} onChange={(event) => setTakeInput(event.target.value)} /></label>
          {stopPrice !== undefined ? <StopMotionFields value={stopMotion} onChange={setStopMotion} atrTimeframe={atrTimeframe} /> : null}
          {separate && today > 0 && historical > 0 ? (
            <span className="fdw-protect-buckets" role="group" aria-label="保护哪部分仓量">
              <button type="button" className={bucket === "today" ? "active" : ""} onClick={() => setBucket("today")}>今 {today}</button>
              <button type="button" className={bucket === "historical" ? "active" : ""} onClick={() => setBucket("historical")}>昨 {historical}</button>
            </span>
          ) : null}
          <small>保护 {volume} 手</small>
          <button type="button" className="fdw-protect-submit" data-testid="row-protect-submit"
            disabled={busy || volume <= 0 || stopPrice === undefined && takePrice === undefined || issue !== undefined}
            onClick={() => void submit()}>提交</button>
          {issue ? <small className="fdw-protect-issue" role="alert">{issue}</small> : null}
        </div>
      ) : (
        <button type="button" data-testid="row-protect-toggle" aria-expanded={open}
          aria-label={`给 ${position.symbol} ${long ? "多" : "空"}仓挂保护`} onClick={() => setOpen(true)}>保护</button>
      )}
    </div>
  );
}

/**
 * The three record lists behind 持仓 / 委托 / 成交. The bottom bar and the 持仓面板 render the
 * same rows from the same `positionsTab`; only the row limit differs (the bar keeps three lines).
 */
export function PositionRows({ positions, instruments, quote, busy, limit, onClosePosition, onAskAi, stops, onCreateStop, onCancelStop, protectionBusy }: {
  positions: AccountPosition[];
  /** Contract catalog: the row leads with the Chinese product name, the code rides as its `small`. */
  instruments?: InstrumentCatalog;
  /** The current contract's quote; only that contract's row can show a live price. */
  quote?: MarketQuoteSnapshot;
  busy: boolean;
  limit: number;
  onClosePosition: (position: AccountPosition) => void;
  /** 持仓面板 only: each row's "AI ▾" (这笔持仓的风险 / 平仓建议，出卡). */
  onAskAi?: AskAi;
  /** 持仓面板 only (2026-10-02): armed protection rows and the per-row inline 保护 form. */
  stops?: AiStopLoss[];
  onCreateStop?: (position: AccountPosition, request: PositionStopRequest) => Promise<boolean | void>;
  onCancelStop?: (stopId: string) => Promise<boolean | void>;
  protectionBusy?: boolean;
}) {
  // 外盘仓合并显示 (2026-10-21): the USD sub-account's rows follow the account's own under one 外盘 · 美元 head;
  // their money is dollars, and their protection belongs to that account (no inline 保护 form here).
  const shown = positions.slice(0, limit);
  const own = shown.filter(position => !position.accountId);
  const foreign = shown.filter(position => position.accountId);
  const foreignFloating = positions.filter(position => position.accountId).reduce((sum, position) => sum + position.unrealizedPnl, 0);
  const row = (position: AccountPosition) => {
    const available = Math.max(0, position.volume - (position.frozenToday ?? 0) - (position.frozenHistorical ?? 0));
    const item = instruments?.items.find(entry => entry.symbol.toUpperCase() === position.symbol.toUpperCase());
    const name = position.instrumentName || contractLabel(position.symbol, instruments);
    const decimals = decimalsForTick(item?.priceTick ?? (position.accountId && !sameContract(position.symbol, quote?.symbol) ? undefined : quote?.priceTick));
    const last = positionLastPrice(position, quote);
    const openPrice = position.openPrice ?? undefined;
    const usd = position.currency === "USD";
    const rowStops = stops?.filter(stop => sameContract(stop.symbol, position.symbol)
      && stop.positionDirection === position.direction && isProtectionActive(stop.status)) ?? [];
    return (
      <li key={positionKey(position)} data-position={positionKey(position)} data-currency={usd ? "USD" : undefined}>
        <strong><span>{name}</span>{name === position.symbol ? null : <small>{position.symbol.split(".").pop()}</small>}</strong>
        <span className="fdw-row-meta">
          <span className={position.direction === "long" ? "positive" : "negative"}>{position.direction === "long" ? "买多" : "卖空"} {position.volume} 手</span>
          {openPrice === undefined ? null
            : <span className="fdw-row-price">{formatNumber(openPrice, decimals)}{last === undefined ? null : <> → {formatNumber(last, decimals)}</>}</span>}
          <span>可用 {available}</span>
          {/* 交割月提醒 (2026-10-02): a text chip beside the row's figures — the head badge's
              wording; plain text, the card's ⓘ stays its only commandable entry. */}
          {item?.deliveryNotice ? <span className="fdw-delivery-chip" data-level={item.deliveryNotice.level}>{deliveryBadgeText(item.deliveryNotice)}</span> : null}
        </span>
        <b className={signClass(position.unrealizedPnl)}>浮盈 {formatSigned(position.unrealizedPnl, 0)}{usd ? " 美元" : ""}</b>
        <span className="fdw-row-actions">
        <button type="button" className="fdw-row-close" disabled={busy || !available} aria-label={`平仓 ${position.symbol} ${position.direction === "long" ? "多" : "空"}仓`} onClick={() => onClosePosition(position)}>平仓</button>
        <AiMenu compact onAsk={onAskAi} focus={{ kind: "position", id: positionKey(position), label: `${name} ${position.direction === "long" ? "多" : "空"} ${position.volume} 手` }} items={[
          { label: "这笔持仓的风险", prompt: "这笔持仓现在的风险怎么样" },
          { label: "平仓建议（出卡）", prompt: "给这笔持仓一个平仓建议，要平就出确认卡" },
        ]}/>
        </span>
        {/* 两本账（2026-10-22）：外盘行也能挂保护——Runtime 按合约把它挂在美元子账户上（account_for_symbol）。 */}
        {onCreateStop && onCancelStop ? (
          <PositionProtection position={position} quote={quote} priceTick={item?.priceTick ?? quote?.priceTick}
            stops={rowStops} busy={busy || (protectionBusy ?? false)} onCreate={onCreateStop} onCancel={onCancelStop} />
        ) : null}
      </li>
    );
  };
  return (
    <ul className="fdw-dock-rows fdw-position-rows" aria-label="当前持仓">
      {own.map(row)}
      {foreign.length ? (
        <li className="fdw-dock-group" data-testid="foreign-positions-head">
          <span>外盘 · 美元</span>
          <b className={signClass(foreignFloating)}>浮盈 {formatSigned(foreignFloating, 0)} 美元</b>
        </li>
      ) : null}
      {foreign.map(row)}
      {!positions.length ? <li className="fdw-dock-empty">当前没有持仓</li> : null}
    </ul>
  );
}

export function OrderRows({ orders, history, busy, limit, onOrderAction, onAbandon, onAskAi, instruments, showShortCode = false }: {
  instruments?: InstrumentCatalog;
  showShortCode?: boolean;
  orders: SimulationOrder[]; history: boolean; busy: boolean; limit: number; onOrderAction: OrderActionHandler; onAbandon: (order: SimulationOrder) => void;
  /** 持仓面板 only: each order's "AI ▾" (为什么没成交 / 追单建议，出卡). */
  onAskAi?: AskAi;
}) {
  return (
    <ul className="fdw-dock-rows" aria-label={history ? "全部委托" : "在途委托"}>
      {orders.slice(0, limit).map((order) => {
        const unknown = orderIsUnknown(order);
        const alive = order.remoteStatus === "ALIVE" && order.cancelPhase === "NONE";
        // A prepared order was never sent: the user decides to send it now or drop it (dropping is confirmed).
        const prepared = order.submitPhase === "PREPARED" && !history;
        const name = contractLabel(order.request.symbol, instruments);
        return (
          <li key={order.clientOrderId} data-order-id={order.clientOrderId} data-unknown={unknown || undefined}>
            <strong title={order.request.symbol}>{showShortCode ? <span>{name}</span> : name}{!showShortCode || name === order.request.symbol ? null : <small>{order.request.symbol.split(".").pop()}</small>}{order.accountId?.startsWith("paperusd:") ? <small className="fdw-currency-tag">美元</small> : null}</strong>
            <span>{orderActionLabel(order)}</span>
            <span>{order.request.limitPrice} × {order.request.volume}</span>
            <em>{orderStatusText(order)}{prepared && notSentReason(order.lastReadError) ? ` · ${notSentReason(order.lastReadError)}` : ""}</em>
            {prepared ? <span className="fdw-row-actions"><button type="button" disabled={busy} onClick={() => onOrderAction(order, "continue")}>继续</button><button type="button" className="app-button danger" disabled={busy} onClick={() => onAbandon(order)}>放弃</button></span>
              : unknown ? <button type="button" disabled={busy} onClick={() => onOrderAction(order, "query")}>查询</button>
              : alive ? <button type="button" disabled={busy} onClick={() => onOrderAction(order, "cancel")}>撤单</button> : null}
            <AiMenu compact onAsk={onAskAi} focus={{ kind: "order", id: order.clientOrderId, label: `${order.request.symbol} ${orderActionLabel(order)} ${order.request.volume} 手` }} items={[
              { label: "为什么没成交", prompt: "这笔委托为什么没成交" },
              { label: "追单建议（出卡）", prompt: "这笔委托要不要追单，要追就出确认卡" },
            ]}/>
          </li>
        );
      })}
      {!orders.length ? <li className="fdw-dock-empty">{history ? "当前账户没有委托记录" : "当前没有在途委托"}</li> : null}
    </ul>
  );
}

/** 成交 · 来源 filter values; `all` shows every round trip (including ones without a source). */
export type FillsSource = "all" | "manual" | "card" | "ai" | "strategy" | "stop" | "liquidation";
export const FILL_SOURCES: { value: FillsSource; label: string }[] = [
  { value: "all", label: "全部" }, { value: "manual", label: "手动" }, { value: "card", label: "AI 建议" }, { value: "ai", label: "AI 自主" },
  { value: "strategy", label: "策略" }, { value: "stop", label: "止损" }, { value: "liquidation", label: "强平" },
];

/** One 成交 line: round trips closed in the same minute on the same contract, direction and
 *  prices (and from the same source) add up into one row. */
export interface FillLine {
  key: string; symbol: string; direction: string; volume: number; closeTime: number; tradingDay: string;
  openPrice: number; closePrice: number; pnl: number; netKnown: boolean; approximate: boolean;
  source?: RoundTrip["source"]; strategyName?: string;
}

const shanghaiDay = (nanos: number) => new Date(nanos / 1_000_000).toLocaleDateString("en-CA", { timeZone: "Asia/Shanghai" });
const minuteTime = (nanos: number) => new Date(nanos / 1_000_000).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, hour: "2-digit", minute: "2-digit" });
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
/** "2026-09-24" → "9月24日 周四" (the trading day, so a night session sits under the next day). */
export function tradingDayLabel(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  if (!year || !month || !date) return day;
  return `${month}月${date}日 ${WEEKDAYS[new Date(Date.UTC(year, month - 1, date)).getUTCDay()]}`;
}
export function fillSourceLabel(line: Pick<FillLine, "source" | "strategyName">): string | null {
  if (!line.source) return null;
  // The strategy's own name: the row already leads with the contract, so an instance name
  // "名称 · SHFE.rb2610" (an older Runtime) loses its code suffix here.
  if (line.source === "strategy") return line.strategyName?.replace(/\s*·\s*[A-Za-z]+\.[A-Za-z]+\d{3,4}$/, "") || "策略";
  return FILL_SOURCES.find(item => item.value === line.source)?.label ?? null;
}

/** Round trips → 成交 lines, newest first, merged per minute / contract / direction / prices / source. */
export function fillLines(rounds: RoundTrip[], source: FillsSource = "all"): FillLine[] {
  const lines = new Map<string, FillLine>();
  for (const trade of rounds) {
    if (source !== "all" && trade.source !== source) continue;
    const minute = Math.floor(trade.closeTime / 60e9);
    const key = [minute, trade.symbol, trade.direction, trade.openPrice, trade.closePrice, trade.source ?? "", trade.strategyName ?? ""].join("|");
    const value = trade.net ?? trade.gross;
    const line = lines.get(key);
    if (line) {
      line.volume += trade.volume;
      line.pnl += value;
      line.netKnown &&= trade.net !== null;
      line.approximate ||= Boolean(trade.approximate);
      line.closeTime = Math.max(line.closeTime, trade.closeTime);
    } else {
      lines.set(key, { key, symbol: trade.symbol, direction: trade.direction, volume: trade.volume, closeTime: trade.closeTime,
        tradingDay: trade.tradingDay || shanghaiDay(trade.closeTime), openPrice: trade.openPrice, closePrice: trade.closePrice,
        pnl: value, netKnown: trade.net !== null, approximate: Boolean(trade.approximate), source: trade.source, strategyName: trade.strategyName });
    }
  }
  return [...lines.values()].sort((a, b) => b.closeTime - a.closeTime || a.key.localeCompare(b.key));
}

function FillLineRow({ line, instruments, dated }: { line: FillLine; instruments?: InstrumentCatalog; dated: boolean }) {
  const label = fillSourceLabel(line);
  // An expired contract is not in the catalog any more: its product's tick is the same.
  const product = (line.symbol.split(".").pop() ?? "").replace(/\d+$/, "").toLowerCase();
  const listed = instruments?.items.find(item => item.symbol.toUpperCase() === line.symbol.toUpperCase())
    ?? instruments?.items.find(item => item.productId.toLowerCase() === product);
  const decimals = decimalsForTick(listed?.priceTick);
  return (
    <li data-fill={line.key}>
      <em>{dated ? closeTime(line.closeTime) : minuteTime(line.closeTime)}</em>
      <strong title={line.symbol}>{contractLabel(line.symbol, instruments)}</strong>
      <span>{line.direction === "long" ? "平多" : "平空"} {line.volume} 手</span>
      <span className="fdw-row-price">{formatNumber(line.openPrice, decimals)} → {formatNumber(line.closePrice, decimals)}{line.approximate ? <em title="按委托补录：委托价、报单时间估算">估</em> : null}</span>
      {label ? <i className="fdw-fill-source" data-source={line.source}>{label}</i> : null}
      <b className={signClass(line.pnl)}>{line.netKnown ? "" : "毛 "}{formatSigned(line.pnl, 0)}</b>
    </li>
  );
}

/**
 * 成交 (2026-09-25, replaces 平仓记录 and the 对话面板's 策略与交易 flow): closed round trips with
 * the contract's Chinese name, a 来源 label and same-minute duplicates merged. `grouped` (the 持仓
 * 面板) groups by trading day with the day's closed P&L in the head; the bottom bar shows the
 * newest lines with their date instead.
 */
export function FillRows({ review, reviewError, instruments, limit, grouped, source = "all", foreignReview }: {
  review?: TradeReviewData; reviewError?: string; instruments?: InstrumentCatalog; limit: number; grouped: boolean; source?: FillsSource;
  /** 本站模拟合成一个账户 (2026-10-21): the 外盘 USD ledger's round trips, listed after the account's own under one
   *  外盘 · 美元 head (like 持仓) — amounts in 美元, never added to the RMB day totals. */
  foreignReview?: TradeReviewData;
}) {
  const lines = fillLines(review?.trades ?? [], source).slice(0, limit);
  const foreignLines = fillLines(foreignReview?.trades ?? [], source).slice(0, limit);
  const foreignTotal = foreignLines.reduce((sum, line) => sum + line.pnl, 0);
  const foreignGroup = foreignLines.length ? <>
    <li className="fdw-dock-group" data-testid="foreign-fills-head"><span>外盘 · 美元</span>
      <b className={signClass(foreignTotal)}>平仓盈亏 {formatSigned(foreignTotal, 0)} 美元</b></li>
    {foreignLines.map(line => <FillLineRow key={`usd:${line.key}`} line={line} instruments={instruments} dated />)}
  </> : null;
  const days = grouped ? lines.reduce<{ day: string; lines: FillLine[] }[]>((groups, line) => {
    const last = groups.at(-1);
    if (last?.day === line.tradingDay) last.lines.push(line); else groups.push({ day: line.tradingDay, lines: [line] });
    return groups;
  }, []) : [];
  const empty = review && !lines.length && !foreignLines.length
    ? <li className="fdw-dock-empty">{source === "all" ? "近 30 天没有平仓成交" : `近 30 天没有来源为「${FILL_SOURCES.find(item => item.value === source)?.label}」的成交`}</li> : null;
  const loading = !review ? <li className="fdw-dock-empty">{reviewError || "正在读取成交"}</li> : null;
  if (!grouped) {
    return (
      <ul className="fdw-dock-rows fdw-fill-rows" aria-label="成交">
        {lines.map(line => <FillLineRow key={line.key} line={line} instruments={instruments} dated />)}
        {foreignGroup}
        {empty}{loading}
      </ul>
    );
  }
  return (
    <div className="fdw-fill-days" aria-label="成交">
      {days.map(group => {
        const total = group.lines.reduce((sum, line) => sum + line.pnl, 0);
        const net = group.lines.every(line => line.netKnown);
        return (
          <section key={group.day} className="fdw-fill-day" data-day={group.day}>
            <h4><span>{tradingDayLabel(group.day)}</span><span>平仓盈亏{net ? "" : "（毛）"} <b className={signClass(total)}>{formatSigned(total, 0)}</b></span></h4>
            <ul className="fdw-dock-rows fdw-fill-rows">
              {group.lines.map(line => <FillLineRow key={line.key} line={line} instruments={instruments} dated={false} />)}
            </ul>
          </section>
        );
      })}
      {foreignGroup ? <ul className="fdw-dock-rows fdw-fill-rows">{foreignGroup}</ul> : null}
      {empty || loading ? <ul className="fdw-dock-rows">{empty}{loading}</ul> : null}
    </div>
  );
}

/** The confirmation for dropping a prepared order, shared by the bar and the 持仓面板. */
export function useAbandonOrder(busy: boolean, onOrderAction: OrderActionHandler) {
  const [abandoning, setAbandoning] = useState<SimulationOrder>();
  const dialog = <AbandonOrderDialog order={abandoning} busy={busy} onClose={() => setAbandoning(undefined)}
    onConfirm={(order) => { setAbandoning(undefined); onOrderAction(order, "abandon"); }} />;
  return { dialog, request: setAbandoning };
}

/**
 * The positions / working orders / round-trips bar under the chart. Always mounted;
 * collapsed it is one line of counts, expanded it shows the three tabs. Round trips
 * come from the account-wide trade review, the same pairing the review view shows.
 * With `autoHide` (设置 → 界面 → 布局, 2026-09-30) the collapsed bar is not rendered
 * at all while the account has no positions and no working orders.
 */
export function PositionsBar({
  account,
  instruments,
  quote,
  simulationOrders,
  review,
  foreignReview,
  reviewError,
  busy,
  collapsed,
  autoHide,
  tab,
  onTabChange,
  onToggle,
  onOrderAction,
  onClosePosition,
  onOpenAll,
  history,
  onHistoryChange
}: {
  account?: AccountOverviewSnapshot;
  instruments?: InstrumentCatalog;
  /** The current contract's quote, for the position rows' 开仓均价 → 现价. */
  quote?: MarketQuoteSnapshot;
  simulationOrders: SimulationOrder[];
  review?: TradeReviewData;
  /** The 外盘 USD ledger's round trips (本站模拟合成一个账户). */
  foreignReview?: TradeReviewData;
  reviewError?: string;
  busy: boolean;
  collapsed: boolean;
  /** 底栏自动隐藏: with no positions and no working orders the collapsed bar is not rendered. */
  autoHide?: boolean;
  tab: PositionsBarTab;
  onTabChange: (tab: PositionsBarTab) => void;
  onToggle: () => void;
  onOrderAction: OrderActionHandler;
  /** "平仓" on a position row: open the ticket's close page with that position selected. */
  onClosePosition: (position: AccountPosition) => void;
  /** "查看全部": the 持仓面板 on the right (1.6.6; it replaced the 持仓抽屉). */
  onOpenAll: () => void;
  /** 委托"历史" — owned by the workspace so the bar and the 持仓面板 share one switch. */
  history: boolean;
  onHistoryChange: (history: boolean) => void;
}) {
  const abandon = useAbandonOrder(busy, onOrderAction);
  const activeOrders = simulationOrders.filter(orderIsActive);
  const unknownCount = activeOrders.filter(orderIsUnknown).length;
  const orderRows = history ? simulationOrders : activeOrders;
  const positions = account?.positions ?? [];
  const rounds = review?.trades ?? [];
  if (collapsed && (autoHide ?? true) && !positions.length && !activeOrders.length) return null;
  if (collapsed) {
    // One line: a chip per position (name, side and lots, floating P&L), the working orders and the
    // positions' market value. 今日 lives in the header's account chip.
    // 持仓市值 is the account's own (yuan); the USD sub-account's chips say dollars on themselves.
    const marketValue = positionsMarketValue(ownPositions(positions), instruments);
    return (
      <section className="fdw-position-dock collapsed" aria-label="持仓与委托" data-testid="positions-bar" data-collapsed="true">
        <span className="fdw-strip-label">持仓</span>
        <ul className="fdw-strip-chips" aria-label="持仓概览">
          {positions.slice(0, 6).map(position => {
            const name = position.instrumentName || contractLabel(position.symbol, instruments);
            return <li key={positionKey(position)}><button type="button" data-direction={position.direction} aria-label={`查看持仓 ${name} ${position.direction === "long" ? "多" : "空"} ${position.volume} 手`} onClick={onOpenAll}>
              <i aria-hidden="true" />{name}<span>{position.direction === "long" ? "多" : "空"} {position.volume}</span>
              <b className={signClass(position.unrealizedPnl)}>{formatSigned(position.unrealizedPnl, 0)}{position.currency === "USD" ? " 美元" : ""}</b>
            </button></li>;
          })}
          {positions.length > 6 ? <li className="fdw-strip-more">+{positions.length - 6}</li> : null}
          {!positions.length ? <li className="fdw-strip-empty">空仓</li> : null}
        </ul>
        <i className="fdw-strip-sep" aria-hidden="true" />
        <span className="fdw-strip-stat" data-unknown={unknownCount > 0 || undefined}>委托 <b>{activeOrders.length}</b>{unknownCount ? `（未知 ${unknownCount}）` : ""}</span>
        {marketValue !== undefined && ownPositions(positions).length ? <span className="fdw-strip-stat" data-testid="positions-market-value">持仓市值 <b>{formatNumber(marketValue, 0)}</b></span> : null}
        <button type="button" className="fdw-positions-summary" aria-expanded={false} aria-label="展开持仓与委托" onClick={onToggle}><ChevronUp /></button>
      </section>
    );
  }
  return (
    <section className="fdw-position-dock" aria-label="持仓与委托" data-testid="positions-bar" data-collapsed="false">
      <header>
        <nav aria-label="持仓数据分类">
          <button type="button" className={tab === "positions" ? "active" : ""} onClick={() => onTabChange("positions")}>持仓 ({positions.length})</button>
          <button type="button" className={tab === "orders" ? "active" : ""} data-unknown={unknownCount > 0 || undefined} onClick={() => onTabChange("orders")}>委托 ({activeOrders.length}{unknownCount ? `，未知 ${unknownCount}` : ""})</button>
          <button type="button" className={tab === "fills" ? "active" : ""} onClick={() => onTabChange("fills")}>成交{review ? ` (${rounds.length})` : ""}</button>
        </nav>
        <div>
          {tab === "orders" ? <button type="button" aria-pressed={history} onClick={() => onHistoryChange(!history)}>历史</button> : null}
          <button type="button" title="持仓面板" onClick={onOpenAll}>查看全部</button>
          <button type="button" aria-expanded={true} aria-label="收起持仓与委托" onClick={onToggle}><ChevronDown /></button>
        </div>
      </header>
      {/* Every tab is a list of one-line records with inline labels: no header row, the same grid template, wrapping when narrow. */}
      {tab === "positions" ? <PositionRows positions={positions} instruments={instruments} quote={quote} busy={busy} limit={3} onClosePosition={onClosePosition} />
        : tab === "orders" ? <OrderRows instruments={instruments} orders={orderRows} history={history} busy={busy} limit={history ? 20 : 3} onOrderAction={onOrderAction} onAbandon={abandon.request} />
        : <FillRows review={review} foreignReview={foreignReview} reviewError={reviewError} instruments={instruments} limit={3} grouped={false} />}
      {abandon.dialog}
    </section>
  );
}
