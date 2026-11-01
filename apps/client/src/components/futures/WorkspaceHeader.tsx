import { contractLabel } from "./support";
import { ChevronDown, History, MoreHorizontal, Play, Plus, Table2, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { sameContract, type InstrumentCatalog } from "../../api";
import type { TradingAccountsView } from "../../hooks/useTradingAccounts";
import { isTauriRuntime } from "../../runtimeConnection";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { productIcon } from "./productIcons";
import { formatNumber, formatSigned, signClass, type FuturesWorkspaceView } from "./support";
import { BrandMark } from "../BrandMark";
import { applyUiSurface } from "../../uiSurfaces";
import "./WorkspaceHeader.css";
import { headerFitClasses, useHeaderFit } from "./useHeaderFit";

/** Tabs shown in full before the rest fold into the "…" menu. */
const VISIBLE_TABS = 6;

interface Props {
  guestActions?: ReactNode;
  /** AI 指令栏 (改版第二步): sits after the contract tabs and shrinks before anything else does. */
  aiBar?: ReactNode;
  workspace: FuturesDecisionWorkspaceProps;
  view: FuturesWorkspaceView;
  onSelectView: (view: FuturesWorkspaceView) => void;
  /** Shown while AI execution is stopped; null otherwise. */
  recoveryButton: ReactNode;
  /** The session's account directory; hidden without a second granted account. */
  tradingAccounts: TradingAccountsView;
  /** Pinned contracts: every one is a trade-view tab. */
  pinnedSymbols: string[];
  onSelectContract: (symbol: string) => void;
  onPinContract: () => void;
  onUnpinContract: (symbol: string) => void;
  /** The settled header layout's visible current label, not membership in the pinned list. */
  onVisibleCurrentContractChange?: (symbol: string | undefined) => void;
  /** M2: why the 行情回放 skill is unavailable (skill registry reason line); null = usable.
   *  Starting one is refused, exiting stays possible. */
  replayBlocked?: string | null;
}

/**
 * Brand, the contract tabs (each pinned contract is a trade workbench tab),
 * review / replay, connection state and equity; drags the desktop window.
 * Contract tabs are role=tab with aria-selected; review and replay stay buttons.
 */
export function WorkspaceHeader({ workspace, view, onSelectView, recoveryButton, tradingAccounts, pinnedSymbols, onSelectContract, onPinContract, onUnpinContract, onVisibleCurrentContractChange, replayBlocked, guestActions, aiBar }: Props) {
  const headerRef = useRef<HTMLElement | null>(null);
  const [menu, setMenu] = useState<"more" | "pick" | null>(null);
  const [blocked, setBlocked] = useState("");
  const allowed = (key: string) => workspace.access?.require(key) ?? true;
  const replay = Boolean(workspace.replayActive);
  const current = workspace.selectedSymbol;
  const nameOf = (symbol: string) => nameFor(symbol, workspace.instruments);
  const Icon = (symbol: string) => {
    const item = workspace.instruments?.items.find(entry => entry.symbol.toUpperCase() === symbol.toUpperCase());
    return productIcon(item?.productId ?? symbol.split(".").pop()?.replace(/\d+$/, ""), item?.exchangeId ?? symbol.split(".")[0]);
  };
  const label = (symbol: string) => { const Glyph = Icon(symbol); return <><Glyph aria-hidden="true" /><span>{nameOf(symbol)}</span></>; };
  const isCurrent = (symbol: string) => view === "trade" && !replay && sameContract(symbol, current);
  // Everything that changes the header's content width restarts the fit pass.
  const poolWarning = tradingAccounts.currentPoolStatus === "connecting" || tradingAccounts.currentPoolStatus === "queued" ? tradingAccounts.currentPoolStatus : "";
  // 本站模拟合成一个账户 (2026-10-21): the 外盘 USD ledger follows the RMB one in the same chip, never summed.
  const usd = workspace.account?.foreign?.account;
  const equityText = `权益 ${formatNumber(workspace.account?.account.equity, 0)}${usd ? `，美元 ${formatNumber(usd.equity, 0)}` : ""}`;
  const todayNet = workspace.account?.account.todayNetPnl;
  const usdToday = usd?.todayNetPnl;
  const fitKey = [
    pinnedSymbols.map(symbol => nameFor(symbol, workspace.instruments)).join(","),
    current ?? "",
    replay ? "replay" : "",
    workspace.onOpenReplay ? "replay-entry" : "",
    poolWarning,
    workspace.connection,
    workspace.status.runtimeUsable ? "" : "runtime-warning",
    recoveryButton ? "recovery" : "",
    equityText.length, // the digit count, not every tick's value: equity updates must not re-run the pass
    workspace.fontScale ?? "",
    workspace.interfaceDensity ?? "",
    isTauriRuntime() ? "" : "web",
  ].join("|");
  const fit = useHeaderFit(headerRef, fitKey);
  const contractMenu = fit >= 5;
  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => { if (!(event.target as Element).closest?.(".fdw-tab-menu, .fdw-tab-menu-trigger")) setMenu(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenu(null); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [menu]);
  useEffect(() => { setMenu(null); }, [contractMenu, current, view]);
  const pick = (symbol: string) => { setMenu(null); onSelectContract(symbol); };
  const tab = (symbol: string) => (
    <span key={symbol} className={`fdw-contract-tab ${isCurrent(symbol) ? "active" : ""}`} data-symbol={symbol}>
      <button type="button" role="tab" aria-selected={isCurrent(symbol)} onClick={() => pick(symbol)}>{label(symbol)}</button>
      {!replay && pinnedSymbols.length > 1 ? <button type="button" aria-label={`移除标签 ${symbol}`} onClick={() => onUnpinContract(symbol)}><X /></button> : null}
    </span>
  );
  const visible = pinnedSymbols.slice(0, VISIBLE_TABS);
  const folded = pinnedSymbols.slice(VISIBLE_TABS);
  const visibleCurrent = contractMenu || visible.some(isCurrent) ? current : undefined;
  useLayoutEffect(() => {
    onVisibleCurrentContractChange?.(visibleCurrent);
  }, [visibleCurrent, onVisibleCurrentContractChange]);
  return (
    <header ref={headerRef} className={`fdw-header ${headerFitClasses(fit)}`} data-fit={fit} data-shell={isTauriRuntime() ? "desktop" : "web"} data-region="header" data-agent-target="header" onMouseDown={(event) => {
      if (event.button !== 0 || !isTauriRuntime() || (event.target as HTMLElement).closest("button, input, select, a, [role='dialog'], [role='menu']")) return;
      event.preventDefault();
      void getCurrentWindow().startDragging().catch(() => undefined);
    }}>
      <div className="fdw-brand"><BrandMark /><strong>Sente</strong><span>{workspace.guest ? "游客浏览" : replay ? "历史回放" : "模拟盘"}</span></div>
      <nav aria-label="主要工作区">
        {/* Both subtrees stay mounted: useHeaderFit flips the level class on the DOM to
            measure each level, and CSS (fdw-fit-contract-menu) is what switches between
            them; aria-hidden only mirrors the settled level for assistive tech. */}
        <div className="fdw-contract-tabs" role="tablist" aria-label="钉住的合约" data-agent-target="header:contracts" aria-hidden={contractMenu}>
          {visible.map(tab)}
          {folded.length ? (
            <span className="fdw-contract-tab fdw-contract-more">
              <button type="button" className="fdw-tab-menu-trigger" aria-label={`更多合约 ${folded.length}`} aria-expanded={menu === "more"} aria-haspopup="menu" onClick={() => setMenu(menu === "more" ? null : "more")}><MoreHorizontal /></button>
              {menu === "more" ? (
                <div className="fdw-tab-menu" role="menu" aria-label="更多合约">
                  {folded.map(symbol => (
                    <div key={symbol} role="none">
                      <button type="button" role="menuitem" aria-current={isCurrent(symbol) || undefined} onClick={() => pick(symbol)}>{label(symbol)}</button>
                      {!replay ? <button type="button" aria-label={`移除标签 ${symbol}`} onClick={() => onUnpinContract(symbol)}><X /></button> : null}
                    </div>
                  ))}
                </div>
              ) : null}
            </span>
          ) : null}
          {!replay && pinnedSymbols.length < 8 ? <button type="button" className="fdw-contract-add" aria-label="钉住合约" title="钉住合约" onClick={onPinContract}><Plus /></button> : null}
        </div>
        {/* Fit level 3: the current contract as a dropdown of the pinned ones. */}
        <div className="fdw-contract-picker" role="tablist" aria-label="钉住的合约" aria-hidden={!contractMenu}>
          <button type="button" className="fdw-tab-menu-trigger" role="tab" aria-selected={view === "trade" && !replay} aria-expanded={menu === "pick"} aria-haspopup="menu" onClick={() => setMenu(menu === "pick" ? null : "pick")}>
            {current ? label(current) : <span>选择合约</span>}<ChevronDown />
          </button>
          {menu === "pick" ? (
            <div className="fdw-tab-menu" role="menu" aria-label="钉住的合约">
              {pinnedSymbols.map(symbol => (
                <div key={symbol} role="none">
                  <button type="button" role="menuitem" aria-current={isCurrent(symbol) || undefined} onClick={() => pick(symbol)}>{label(symbol)}</button>
                  {!replay && pinnedSymbols.length > 1 ? <button type="button" aria-label={`移除标签 ${symbol}`} onClick={() => onUnpinContract(symbol)}><X /></button> : null}
                </div>
              ))}
              {!replay && pinnedSymbols.length < 8 ? <button type="button" role="menuitem" className="fdw-tab-menu-add" onClick={() => { setMenu(null); onPinContract(); }}><Plus />钉住合约…</button> : null}
            </div>
          ) : null}
        </div>
        <i className="fdw-nav-divider" aria-hidden="true" />
        <span className="fdw-view-group">
        {!workspace.guest ? <button type="button" className={`fdw-nav-view ${view === "board" ? "active" : ""}`} aria-label="报价" onClick={() => onSelectView("board")}><Table2 /><span>报价</span></button> : null}
        <button type="button" className={`fdw-nav-view ${view === "review" ? "active" : ""}`} aria-label="复盘" onClick={() => { if (allowed("review-summary")) onSelectView("review"); }}><History /><span>复盘</span></button>
        {workspace.onOpenReplay && <button type="button" className={`fdw-nav-view ${replay && view === "trade" ? "active" : ""}`} aria-label="回放" title={replayBlocked && !replay ? replayBlocked : undefined} onClick={() => { if (replay) onSelectView("trade"); else if (!workspace.access && replayBlocked) setBlocked(replayBlocked); else if (allowed("market-replay")) workspace.onOpenReplay?.(); }}><Play /><span>回放</span></button>}
        </span>
      </nav>
      {aiBar}
      <div className="fdw-header-status">
        {!workspace.guest && (tradingAccounts.currentPoolStatus === "connecting" || tradingAccounts.currentPoolStatus === "queued")
          ? <span data-testid="pool-connection" className="warning"><i /><span className="fdw-warning-text">{tradingAccounts.currentPoolStatus === "connecting" ? "正在连接模拟盘…" : "服务器在线人数已满，排队中，稍后自动重试"}</span></span>
          : null}
        {!workspace.status.runtimeUsable || workspace.connection !== "connected" ? <span data-testid="runtime-connection" className="warning"><i /><span className="fdw-warning-text">{workspace.connection === "connecting" ? "正在连接" : workspace.connection === "disconnected" ? "连接已断开" : "数据恢复中"}</span></span> : null}
        {!workspace.guest && recoveryButton}
        {/* 点权益打开账户明细（2026-10-22，本站模拟两本账样稿）：切到复盘、明细卡里人民币 / 美元两栏；回放中没有账户明细。 */}
        {!workspace.guest && <button type="button" className="fdw-account-chip" data-testid="account-equity" aria-label={`${equityText}，查看账户明细`} title={equityText}
          aria-haspopup="dialog" disabled={replay} onClick={() => {
            if (!allowed("review-summary")) return;
            onSelectView("review");
            applyUiSurface("dialog:account-detail", { open: true });
          }}>
          <span><small>权益</small>{formatNumber(workspace.account?.account.equity, 0)}</span>
          {todayNet !== undefined ? <span data-testid="today-net" className={signClass(todayNet)}><small>今日</small>{formatSigned(todayNet, 0)}</span> : null}
          {usd ? <span data-testid="account-equity-usd" className="fdw-account-chip-usd"><small>美元</small>{formatNumber(usd.equity, 0)}</span> : null}
          {usd && usdToday !== undefined ? <span data-testid="today-net-usd" className={signClass(usdToday)}><small>今日</small>{formatSigned(usdToday, 0)}</span> : null}
        </button>}
        {guestActions}
        {workspace.guest && <button type="button" onClick={workspace.onOpenCloudLogin}>登录</button>}
        {blocked && <span role="status">{blocked}</span>}
      </div>
    </header>
  );
}

function nameFor(symbol: string, instruments: InstrumentCatalog | undefined): string {
  return contractLabel(symbol, instruments);
}
