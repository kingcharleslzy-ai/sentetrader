import { useEffect, useState } from "react";
import { foreignQuoteOnly, sameContract, type AccountPosition, type MarketQuoteSnapshot, type SimulationOrder } from "../../api";
import { shortcutAction, shortcutLabel, useShortcuts, type ShortcutAction } from "../../shortcuts";
import { useUiSurface } from "../../uiSurfaces";
import { formatNumber, decimalsForTick, type ManualSimulationRequest } from "./support";
import type { useQuickOrders } from "./useQuickOrders";

export const QUICK_VOLUME_KEY = "ai-trader:quick-volume";
const readVolume = () => { try { const n = Number(localStorage.getItem(QUICK_VOLUME_KEY)); return n === 2 || n === 5 ? n : 1; } catch { return 1; } };
/** The same close request as the manual ticket: the Runtime owns today's/yesterday's allocation. */
export function quickCloseRequests(positions: AccountPosition[], symbol: string, mode: "all" | "today" | "half"): ManualSimulationRequest[] {
  return positions.filter(p => sameContract(p.symbol, symbol)).flatMap(p => {
    const available = Math.max(0, p.volume - (p.frozenToday ?? 0) - (p.frozenHistorical ?? 0));
    const volume = mode === "today" ? Math.max(0, (p.todayVolume ?? 0) - (p.frozenToday ?? 0))
      : mode === "half" ? Math.min(available, Math.max(1, Math.floor(p.volume / 2))) : available;
    return volume > 0 ? [{ side: p.direction === "long" ? "sell_close" : "buy_close", volume,
      priceIntent: "aggressive", source: "three-key", ...(mode === "today" ? { positionBucket: "today" as const } : {}) }] : [];
  });
}

/** 反手 on the chart's contract: target −net (null when flat or long = short), and both steps in words — the same
 *  reading as the Runtime's plan summary (close the gross holding, then open the net on the other side). */
export function quickReverse(positions: AccountPosition[], symbol: string): { targetNet: number; summary: string } | null {
  const rows = positions.filter(p => sameContract(p.symbol, symbol));
  const long = rows.filter(p => p.direction === "long").reduce((sum, p) => sum + p.volume, 0);
  const short = rows.filter(p => p.direction === "short").reduce((sum, p) => sum + p.volume, 0);
  const net = long - short;
  if (!net) return null;
  const held = !short ? "平多" : !long ? "平空" : "平掉全部";
  return { targetNet: -net, summary: `${held} ${long + short} 手，成交后自动${net > 0 ? "开空" : "开多"} ${Math.abs(net)} 手（确认一次）` };
}

export function useTicketQuickActions({ quote, positions, orders, busy, enabled, quick }: {
  quote: MarketQuoteSnapshot | undefined; positions: AccountPosition[]; orders: SimulationOrder[];
  busy: boolean; enabled: boolean; quick: ReturnType<typeof useQuickOrders>;
}) {
  const [volume, setVolume] = useState(readVolume);
  const shortcuts = useShortcuts();
  useUiSurface("quickVolume", { state: volume, apply: value => { if (value === 1 || value === 2 || value === 5) setVolume(value); } }, [volume]);
  useEffect(() => { try { localStorage.setItem(QUICK_VOLUME_KEY, String(volume)); } catch { /* Local preference. */ } }, [volume]);
  const separate = ["SHFE", "INE"].includes((quote?.symbol ?? "").split(".")[0].toUpperCase());
  const todayVisible = separate && positions.some(p => sameContract(p.symbol, quote?.symbol ?? "") && (p.todayVolume ?? 0) > 0);
  const all = quickCloseRequests(positions, quote?.symbol ?? "", "all");
  const today = todayVisible ? quickCloseRequests(positions, quote?.symbol ?? "", "today") : [];
  const half = quickCloseRequests(positions, quote?.symbol ?? "", "half");
  const reverse = quickReverse(positions, quote?.symbol ?? "");
  const cancellable = orders.filter(o => sameContract(o.request.symbol, quote?.symbol ?? "") && o.remoteStatus === "ALIVE" && o.cancelPhase === "NONE");
  const locked = busy || quick.quickBusy || Boolean(quick.pending);
  const disabled: Record<string, boolean> = {
    quickBuy: locked || !quote || !enabled || foreignQuoteOnly(quote?.symbol ?? "") || (quote?.openLimit != null && quote?.openLimit > 0 && volume > quote?.openLimit),
    quickSell: locked || !quote || !enabled || foreignQuoteOnly(quote?.symbol ?? "") || (quote?.openLimit != null && quote?.openLimit > 0 && volume > quote?.openLimit),
    quickClose: locked || !quote || !enabled || !all.length, quickToday: locked || !quote || !enabled || !today.length,
    quickHalf: locked || !quote || !enabled || !half.length, quickCancel: locked || !cancellable.length,
    quickReverse: locked || !quote || !enabled || !reverse || foreignQuoteOnly(quote?.symbol ?? ""),
  };
  const run = (action: ShortcutAction) => {
    if (disabled[action] !== false) return;
    if (action === "quickCancel") quick.requestAction({ kind: "cancel", orders: cancellable });
    else if (action === "quickReverse") { if (reverse) quick.requestAction({ kind: "reverse", ...reverse }); }
    else quick.requestAction({ kind: "orders", requests: action === "quickBuy" || action === "quickSell"
      ? [{ side: action === "quickBuy" ? "buy_open" : "sell_open", volume, priceIntent: "aggressive", source: "three-key" }]
      : action === "quickToday" ? today : action === "quickHalf" ? half : all });
  };
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing || document.querySelector("dialog[open]")) return;
      const el = event.target instanceof HTMLElement ? event.target : null;
      if (el?.closest("input, textarea, select, [contenteditable=true]")) return;
      const action = shortcutAction(event);
      if (!action?.startsWith("quick")) return;
      event.preventDefault(); run(action);
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  });
  return { volume, setVolume, shortcuts, disabled, run, todayVisible, message: quick.quickMessage, quote };
}

export function QuickActions({ controller, onSettings }: { controller: ReturnType<typeof useTicketQuickActions>; onSettings: () => void }) {
  const { volume, setVolume, shortcuts, disabled, run, todayVisible, message, quote } = controller;
  const button = (action: ShortcutAction, label: string, style: string) => <button type="button" className={style}
    aria-label={`一键${label}`} disabled={disabled[action]} onClick={() => run(action)}>
    <span className="fdw-quick-button-copy"><span>{label}</span>{action === "quickBuy" || action === "quickSell" ? <small>{formatNumber(action === "quickBuy" ? quote?.askPrice1 : quote?.bidPrice1, decimalsForTick(quote?.priceTick))}</small> : null}</span><kbd>{shortcutLabel(shortcuts[action])}</kbd>
  </button>;
  return <section className="fdw-quick-actions" aria-label="快捷下单" data-agent-target="quick-actions" data-testid="quick-row">
    <header><strong>快捷 · 对手价 · 本合约</strong><div role="group" aria-label="快捷手数">{[1, 2, 5].map(n => <button key={n} type="button" aria-label={`快捷手数 ${n}`} aria-pressed={volume === n} onClick={() => setVolume(n)}>{n}</button>)}</div></header>
    <div className="fdw-quick-buttons">{button("quickBuy", "买多", "buy")}{button("quickSell", "卖空", "sell")}</div>
    <div className="fdw-quick-buttons">{button("quickClose", "平仓", "close")}{todayVisible ? button("quickToday", "平今", "close") : null}{button("quickHalf", "平一半", "close")}{button("quickReverse", "反手", "close")}</div>
    <footer><button type="button" disabled={disabled.quickCancel} onClick={() => run("quickCancel")}>撤本合约挂单 {shortcutLabel(shortcuts.quickCancel)} · 默认先弹确认</button><button type="button" onClick={onSettings}>设置快捷键</button></footer>
    {message ? <p role="status">{message}</p> : null}
  </section>;
}
