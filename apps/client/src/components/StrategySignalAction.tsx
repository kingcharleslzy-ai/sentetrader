import { useEffect, useRef, useState } from "react";
import { ChevronDown, Play, X } from "lucide-react";
import type { InstrumentCatalog } from "../api";
import { allowsSymbol, CONTRACT, spreadLegsWords, spreadWords, strategyScope, strategyText, type SavedStrategy } from "../strategyApi";
import { useDialogSurface, useDisclosureSurface, useUiSurface } from "../uiSurfaces";
import { HelpButton } from "./AppHelp";

/** 执行方式 (2026-09-25): what a running instance does with its signal. */
export type SignalAction = "alert" | "confirm" | "auto";
export const SIGNAL_ACTIONS: { value: SignalAction; label: string; hint: string }[] = [
  { value: "alert", label: "只提醒", hint: "信号来了在对话里提醒一句，不出卡、不下单" },
  { value: "confirm", label: "出卡确认", hint: "信号来了出一张确认卡，你确认后才下单" },
  { value: "auto", label: "自动下单", hint: "信号通过检查后直接下模拟单" },
];
export const AUTO_BLOCKED = "AI 自主执行已关闭（设置 → 交易），打开后才能选自动下单";
export const signalActionLabel = (value: string | null | undefined) => SIGNAL_ACTIONS.find(item => item.value === value)?.label ?? "自动下单";

/** A strategy bound to one concrete contract launches only there. */
export function lockedSymbol(item: SavedStrategy): string | null {
  if (!item.definition.instrumentScope) return item.symbol;
  const scope = strategyScope(item);
  return scope.mode === "selected" && scope.symbols.length === 1 && CONTRACT.test(scope.symbols[0]) ? scope.symbols[0] : null;
}

export function launchDefaults(item: SavedStrategy, chartSymbol: string): { symbol: string; symbolLocked: boolean; signalAction: SignalAction } {
  const spread = item.definition.plan?.spread;
  if (spread) return { symbol: spread.legs[0].symbol, symbolLocked: true, signalAction: "confirm" };
  const locked = lockedSymbol(item);
  return { symbol: locked ?? (allowsSymbol(item, chartSymbol) && CONTRACT.test(chartSymbol) ? chartSymbol : ""), symbolLocked: Boolean(locked), signalAction: "confirm" };
}

/**
 * 上线运行 (2026-09-25): choose the contract (the chart's by default; a strategy bound to one
 * contract is locked to it) and the 执行方式 (出卡确认 by default; 自动下单 is greyed with its reason
 * while AI 自主执行 is off), then 验证并上线. A failed validation shows its reason here as is.
 * 价差 (2026-09-29 第二期): the two legs stand in for the contract field (监控按 A 腿运行) and all
 * three 执行方式 are offered — a confirmed card or an auto signal both dispatch the two legs together.
 * `dialog: strategy-launch` opens it; `strategyLaunch: {symbol?, signalAction?}` fills it.
 */
export function StrategyLaunchDialog({ open, item, chartSymbol, instruments, autonomy, busy, stage, error, onRequestOpen, onClose, onSubmit }: {
  open: boolean; item: SavedStrategy | undefined; chartSymbol: string; instruments?: InstrumentCatalog; autonomy?: boolean;
  busy: boolean; stage: string; error: string; onRequestOpen: () => void; onClose: () => void;
  onSubmit: (symbol: string, signalAction: SignalAction) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const defaults = item ? launchDefaults(item, chartSymbol) : { symbol: "", symbolLocked: false, signalAction: "confirm" as SignalAction };
  const spread = item?.definition.plan?.spread ?? null;
  const [symbol, setSymbol] = useState(defaults.symbol);
  const [signalAction, setSignalAction] = useState<SignalAction>(defaults.signalAction);
  const autoBlocked = autonomy === false ? AUTO_BLOCKED : null;
  // A fresh dialog each time it opens (or the strategy changes).
  useEffect(() => { if (open && item) { const next = launchDefaults(item, chartSymbol); setSymbol(next.symbol); setSignalAction(next.signalAction); } },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [open, item?.strategyId]);
  useEffect(() => { if (open) dialog.current?.showModal(); else dialog.current?.close(); }, [open]);
  useDialogSurface("strategy-launch", dialog, { open: onRequestOpen, close: onClose });
  const valid = Boolean(item && (spread ? symbol === spread.legs[0].symbol : CONTRACT.test(symbol) && allowsSymbol(item, symbol)));
  const symbolHint = !symbol ? "请选择一个具体合约，例如 SHFE.rb2701 或 CME.GCEZ26" : !CONTRACT.test(symbol) ? "请填写具体合约代码，例如 SHFE.rb2701 或 CME.GCEZ26"
    : item && !allowsSymbol(item, symbol) ? "这个合约不在策略的适用范围内" : null;
  useUiSurface("strategyLaunch", {
    state: { open, strategyId: item?.strategyId ?? null, symbol, symbolLocked: defaults.symbolLocked, signalAction, autoBlocked, error: error || null },
    apply: value => {
      const change = value as { symbol?: string; signalAction?: SignalAction };
      if (!open || !item) throw new Error("先打开上线对话框（dialog: strategy-launch）");
      if (change.symbol !== undefined) {
        if (spread && change.symbol !== defaults.symbol) throw new Error("价差的两条腿写在策略定义里，上线时不能换合约");
        if (defaults.symbolLocked && change.symbol !== defaults.symbol) throw new Error("这条策略绑定了合约，不能换");
        if (!allowsSymbol(item, change.symbol)) throw new Error("这个合约不在策略的适用范围内");
        setSymbol(change.symbol);
      }
      if (change.signalAction !== undefined) {
        if (change.signalAction === "auto" && autoBlocked) throw new Error(autoBlocked);
        setSignalAction(change.signalAction);
      }
    },
  }, [open, item?.strategyId, symbol, signalAction, autoBlocked, error, defaults.symbolLocked]);
  const options = (instruments?.items ?? []).filter(entry => item && CONTRACT.test(entry.symbol) && allowsSymbol(item, entry.symbol)).slice(0, 300);
  return <dialog ref={dialog} className="replay-setup strategy-launch-dialog" aria-label="上线运行" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <header><div><h2>上线运行</h2><HelpButton topic="strategy-launch" /></div>
      <button type="button" aria-label="关闭上线运行" disabled={busy} onClick={onClose}><X size={18}/></button></header>
    {item ? <form className="replay-setup-body strategy-launch-body" onSubmit={event => { event.preventDefault(); if (valid && !busy) onSubmit(symbol, signalAction); }}>
      <p className="strategy-launch-name"><strong>{strategyText(item.name)}</strong> <small>v{item.version}</small></p>
      {spread ? <label>价差
        <input aria-label="价差的两条腿" value={spreadWords(spread)} readOnly disabled={busy}/>
        <small>做多价差 = {spreadLegsWords(spread, "open_long")}；监控按 A 腿 {spread.legs[0].symbol} 的行情与时段运行</small>
      </label> : <label>合约
        <input aria-label="合约" value={symbol} list="strategy-launch-symbols" disabled={busy || defaults.symbolLocked} spellCheck={false}
          onChange={event => setSymbol(event.target.value.trim())} aria-describedby="strategy-launch-symbol-hint"/>
        <datalist id="strategy-launch-symbols">{options.map(entry => <option key={entry.symbol} value={entry.symbol}>{entry.instrumentName || entry.symbol}</option>)}</datalist>
        <small id="strategy-launch-symbol-hint">{defaults.symbolLocked ? "这条策略绑定了这个合约" : symbolHint ?? (symbol === chartSymbol ? "当前图表合约" : "监控按这个合约自己的行情运行")}</small>
      </label>}
      <fieldset className="strategy-signal-options">
        <legend>执行方式</legend>
        {SIGNAL_ACTIONS.map(option => {
          const blocked = option.value === "auto" ? autoBlocked : null;
          return <label key={option.value} className="strategy-choice" data-disabled={blocked ? "true" : undefined}>
            <input type="radio" name="strategy-signal-action" value={option.value} checked={signalAction === option.value}
              disabled={busy || Boolean(blocked)} onChange={() => setSignalAction(option.value)}/>
            <span><strong>{option.label}</strong><small>{blocked ?? option.hint}</small></span>
          </label>;
        })}
      </fieldset>
      {error ? <p role="alert" className="strategy-launch-error">{error}</p> : null}
      <div className="strategy-launch-actions">
        <button type="button" disabled={busy} onClick={onClose}>取消</button>
        <button type="submit" className="strategy-primary" disabled={busy || !valid}><Play size={15}/>{busy ? stage || "处理中…" : "验证并上线"}</button>
      </div>
    </form> : null}
  </dialog>;
}

/**
 * The running card's 执行方式: the current mode, and (click) the three to switch between. The
 * switch is the same `POST …/monitor/start {signalAction}` the AI's start_strategy_monitoring sends (never a UI
 * command, #932).
 */
export function SignalActionMenu({ strategyId, value, effective, autonomy, busy, onChange }: {
  strategyId: string; value: string | null | undefined; effective?: string | null; autonomy?: boolean; busy: boolean;
  onChange: (action: SignalAction) => void;
}) {
  const [open, setOpen] = useDisclosureSurface("strategy-signal-action", { id: strategyId });
  const root = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open, setOpen]);
  const degraded = (value ?? "auto") === "auto" && effective === "confirm";
  return <span ref={root} className="strategy-signal-action">
    <button type="button" className="strategy-signal-trigger" aria-haspopup="menu" aria-expanded={open} disabled={busy}
      onClick={() => setOpen(current => !current)}>{signalActionLabel(value)}{degraded ? "（AI 自主执行已关闭，按出卡确认）" : ""}<ChevronDown size={12}/></button>
    {open ? <span className="ai-menu-list strategy-signal-list" role="menu" aria-label="切换执行方式">
      {SIGNAL_ACTIONS.map(option => {
        const blocked = option.value === "auto" && autonomy === false ? AUTO_BLOCKED : null;
        return <button type="button" role="menuitemradio" aria-checked={(value ?? "auto") === option.value} key={option.value}
          disabled={Boolean(blocked) || busy} title={blocked ?? option.hint}
          onClick={() => { setOpen(false); if ((value ?? "auto") !== option.value) onChange(option.value); }}>
          <strong>{option.label}</strong><small>{blocked ?? option.hint}</small></button>;
      })}
    </span> : null}
  </span>;
}
