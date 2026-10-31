import { BriefcaseBusiness, PanelRightClose, RefreshCw, ShieldCheck } from "lucide-react";
import { CardShell } from "../CardShell";
import type { AccountOverviewSnapshot, AccountPosition, AccountSnapshot, AiStopLoss, InstrumentCatalog, MarketQuoteSnapshot, SimulationOrder } from "../../api";
import type { TradeReviewData } from "../../hooks/useTradeReview";
import type { useAppWorkbench } from "../AiActivity";
import { protectionStatus } from "../../operationEvents";
import { useDisclosureSurface } from "../../uiSurfaces";
import { FILL_SOURCES, FillRows, OrderRows, PositionRows, orderIsActive, orderIsUnknown, useAbandonOrder,
  type FillsSource, type OrderActionHandler, type PositionStopRequest, type PositionsBarTab } from "./PositionPanels";
import { contractLabel, formatBoardPrice, formatNumber, formatSigned, ownPositions, signClass } from "./support";
import type { AskAi } from "../AiMenu";

interface Props {
  account?: AccountOverviewSnapshot;
  /** Contract catalog and the current quote, for the rows' 品种名 and 开仓均价 → 现价. */
  instruments?: InstrumentCatalog;
  quote?: MarketQuoteSnapshot;
  simulationOrders: SimulationOrder[];
  review?: TradeReviewData;
  /** The 外盘 USD ledger's round trips (本站模拟合成一个账户). */
  foreignReview?: TradeReviewData;
  reviewError?: string;
  busy: boolean;
  tab: PositionsBarTab;
  onTabChange: (tab: PositionsBarTab) => void;
  history: boolean;
  onHistoryChange: (history: boolean) => void;
  /** 成交 · 来源 filter (`fillsSource` command). */
  fillsSource: FillsSource;
  onFillsSourceChange: (source: FillsSource) => void;
  onOrderAction: OrderActionHandler;
  onClosePosition: (position: AccountPosition) => void;
  onRefresh: () => void;
  onClose: () => void;
  activeProtections: NonNullable<ReturnType<typeof useAppWorkbench>["activity"]>["protections"] | undefined;
  protectionWarning: boolean | undefined;
  /** 下单顺带保护 (2026-10-02): the account's stop rows (armed line + 撤销) and the per-row form. */
  stops: AiStopLoss[] | undefined;
  protectionBusy: boolean | undefined;
  onCreateStop: (position: AccountPosition, request: PositionStopRequest) => Promise<boolean | void>;
  onCancelStop: (stopId: string) => Promise<boolean | void>;
  /** 对象级 AI: each position and order row's "AI ▾" (the question goes out with the row as focus). */
  onAskAi?: AskAi;
}

/** One ledger's six cells: 权益 / 今日 / 浮动盈亏 / 可用 / 保证金占用 / 风险度, all from that ledger's own snapshot and
 *  positions (风险度 = 保证金占用 ÷ 权益, reported by the account; marginRatio is a percentage and is not used). */
function LedgerKpis({ snapshot, positions, testId }: { snapshot?: AccountSnapshot; positions: AccountPosition[]; testId: string }) {
  const floating = positions.reduce((total, position) => total + position.unrealizedPnl, 0);
  const riskRatio = snapshot?.riskRatio;
  const riskTone = riskRatio === undefined ? "" : riskRatio >= 100 ? "danger" : riskRatio >= 80 ? "warning" : "";
  const today = snapshot?.todayNetPnl ?? snapshot?.todayPnl;
  return <dl className="fdw-positions-kpi" data-testid={testId}>
    <div><dt>权益</dt><dd title={formatNumber(snapshot?.equity, 0)}>{formatNumber(snapshot?.equity, 0)}</dd></div>
    <div><dt>今日</dt><dd className={signClass(today)}>{today === undefined ? "—" : formatSigned(today, 0)}</dd></div>
    <div><dt>浮动盈亏</dt><dd className={signClass(snapshot ? floating : undefined)}>{snapshot ? formatSigned(floating, 0) : "—"}</dd></div>
    <div data-testid={testId === "positions-kpi" ? "kpi-available" : "kpi-usd-available"}><dt>可用</dt><dd title={formatNumber(snapshot?.available, 0)}>{formatNumber(snapshot?.available, 0)}</dd></div>
    <div><dt>保证金占用</dt><dd title={formatNumber(snapshot?.margin, 0)}>{formatNumber(snapshot?.margin, 0)}</dd></div>
    <div data-testid={testId === "positions-kpi" ? "kpi-risk-ratio" : "kpi-usd-risk-ratio"}><dt>风险度</dt><dd className={riskTone} title="保证金占用 ÷ 权益">{riskRatio === undefined ? "—" : `${Math.round(riskRatio * 10) / 10}%`}</dd></div>
  </dl>;
}

/**
 * 持仓面板 (右侧栏重设计 v2, replaces the 持仓抽屉): account KPIs, the same 持仓 / 委托 / 成交
 * tabs the bottom bar shows (one `positionsTab`, one 历史 switch), and the protection list. The
 * 成交 tab here groups by trading day and filters by 来源.
 */
export function PositionsDock({ account, instruments, quote, simulationOrders, review, foreignReview, reviewError, busy, tab, onTabChange, history, onHistoryChange,
  fillsSource, onFillsSourceChange, onOrderAction, onClosePosition, onRefresh, onClose, activeProtections, protectionWarning, stops, protectionBusy, onCreateStop, onCancelStop, onAskAi }: Props) {
  const abandon = useAbandonOrder(busy, onOrderAction);
  const [protectionOpen, setProtectionOpen] = useDisclosureSurface("protection-status");
  const positions = account?.positions ?? [];
  const activeOrders = simulationOrders.filter(orderIsActive);
  const unknownCount = activeOrders.filter(orderIsUnknown).length;
  const orderRows = history ? simulationOrders : activeOrders;
  // 本站模拟两本账 (2026-10-21/22): the USD sub-account's own six cells under the RMB ones — never summed.
  const usd = account?.foreign?.account;
  const missingSnapshot = account?.positionsStatus?.snapshotAvailable === false
    || account?.foreign?.positionsStatus?.snapshotAvailable === false;
  // 本站模拟挂起中的到期结算 (#527): 待最后交易日结算价 / 暂估待核 — an actual account state, shown as is.
  const expiries = account?.account.expirySettlements ?? [];
  const contractName = (symbol: string) => contractLabel(symbol, instruments);
  return (
    <section className="fdw-ai open fdw-positions-dock" aria-label="持仓面板" data-agent-target="panel:positions">
      <header>
        <div><BriefcaseBusiness /><span><strong>持仓</strong><small>{account?.account.accountName ?? "账户读取中"}</small></span></div>
        <div className="fdw-ai-head-actions">
          <button type="button" aria-label="刷新账户" title="刷新账户" onClick={onRefresh}><RefreshCw /></button>
          <button type="button" aria-label="收起持仓" onClick={() => onClose()}><PanelRightClose /></button>
        </div>
      </header>
      {usd ? <p className="fdw-positions-ledger">人民币 · 国内</p> : null}
      {account?.positionsStatus?.isCached ? <p role="status">{account.positionsStatus.snapshotAvailable
        ? "账户数据中断，价格与盈亏沿用最近快照" : "持仓快照尚未就绪，正在等待更新"}</p> : null}
      <LedgerKpis snapshot={account?.positionsStatus?.snapshotAvailable === false ? undefined : account?.account} positions={ownPositions(positions)} testId="positions-kpi" />
      {usd ? <>
        <p className="fdw-positions-ledger">美元 · 外盘</p>
        {account?.foreign?.positionsStatus?.isCached ? <p role="status">{account.foreign.positionsStatus.snapshotAvailable
          ? "外盘行情或账户数据中断，价格与盈亏沿用最近快照" : "外盘持仓快照尚未就绪，正在等待更新"}</p> : null}
        <LedgerKpis snapshot={account?.foreign?.positionsStatus?.snapshotAvailable === false ? undefined : usd} positions={positions.filter(position => position.accountId)} testId="positions-kpi-usd" />
      </> : null}
      {expiries.length ? (
        <ul className="fdw-expiry-settlements" aria-label="到期结算" data-testid="expiry-settlements">
          {expiries.map(item => (
            <li key={item.symbol}>{contractName(item.symbol)} 到期结算 · {item.statusLabel}
              {item.estimate ? ` · 暂估价 ${formatBoardPrice(item.estimate.price)}` : ""}</li>
          ))}
        </ul>
      ) : null}
      <div className="fdw-ai-scroll">
        <nav className="fdw-positions-tabs" aria-label="持仓数据分类" data-agent-target="positions-tabs">
          <button type="button" className={tab === "positions" ? "active" : ""} onClick={() => onTabChange("positions")}>持仓 ({missingSnapshot ? "—" : positions.length})</button>
          <button type="button" className={tab === "orders" ? "active" : ""} data-unknown={unknownCount > 0 || undefined} onClick={() => onTabChange("orders")}>委托 ({activeOrders.length}{unknownCount ? `，未知 ${unknownCount}` : ""})</button>
          <button type="button" className={tab === "fills" ? "active" : ""} onClick={() => onTabChange("fills")}>成交{review ? ` (${review.trades.length + (foreignReview?.trades.length ?? 0)})` : ""}</button>
          {tab === "orders" ? <button type="button" className="fdw-positions-history" aria-pressed={history} onClick={() => onHistoryChange(!history)}>历史</button> : null}
        </nav>
        {tab === "positions" ? (missingSnapshot && !positions.length ? null : <PositionRows positions={positions} instruments={instruments} quote={quote} busy={busy} limit={50} onClosePosition={onClosePosition} onAskAi={onAskAi}
          stops={stops} protectionBusy={protectionBusy} onCreateStop={onCreateStop} onCancelStop={onCancelStop} />)
          : tab === "orders" ? <OrderRows instruments={instruments} orders={orderRows} history={history} busy={busy} limit={50} onOrderAction={onOrderAction} onAbandon={abandon.request} onAskAi={onAskAi} />
          : <>
            <nav className="fdw-fill-sources" aria-label="成交来源" data-agent-target="fills-source">
              {FILL_SOURCES.map(item => <button type="button" key={item.value} aria-pressed={fillsSource === item.value}
                onClick={() => onFillsSourceChange(item.value)}>{item.label}</button>)}
            </nav>
            <FillRows review={review} foreignReview={foreignReview} reviewError={reviewError} instruments={instruments} limit={100} grouped source={fillsSource} />
          </>}
        <CardShell icon={<ShieldCheck size={18} />} title={protectionWarning ? "保护异常" : "保护"} className={protectionWarning ? "ai-protection-summary warning" : "ai-protection-summary"}
          meta={activeProtections == null ? "待确认" : activeProtections.length ? `${activeProtections.length} 条活动保护` : "当前合约无活动保护记录"}
          headAction={<button type="button" aria-expanded={protectionOpen} onClick={() => setProtectionOpen(value => !value)}>{protectionOpen ? "收起" : "展开"}</button>}>
          {protectionOpen ? <ul className="ai-protection-lines">{activeProtections == null ? <li>保护状态待确认</li> : activeProtections.length ? activeProtections.map(p => <li key={String(p.stopId)}>{protectionStatus(String(p.status))} · {[
            p.triggerPrice != null ? `止损 ${String(p.triggerPrice)}` : null,
            p.takeProfitPrice != null ? `止盈 ${String(p.takeProfitPrice)}` : null
          ].filter(Boolean).join(" / ")}</li>) : <li>当前合约无活动保护记录</li>}</ul> : null}
        </CardShell>
      </div>
      {abandon.dialog}
    </section>
  );
}
