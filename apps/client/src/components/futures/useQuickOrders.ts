import { useCallback, useEffect, useRef, useState } from "react";
import { knownAccountRevision } from "../../accountRevision";
import type { MarketQuoteSnapshot, SimulationOrder } from "../../api";
import type { ManualSimulationRequest } from "./support";
import { manualOrderReceipt, type ManualOrderOutcome } from "../../hooks/useSimulationOrders";

/** A quick order waiting for the user's confirmation. */
export type PendingQuickOrder = { symbol: string; contextKey?: string } & (
  { kind: "orders"; requests: ManualSimulationRequest[] } | { kind: "cancel"; orders: SimulationOrder[] }
  /** 反手: the 目标持仓 route at −net, its card confirmed once here; the Runtime sends the opening leg after the close fills. */
  | { kind: "reverse"; targetNet: number; summary: string }
);

/**
 * "快捷下单免确认" is kept on this machine but per user: the key carries the workspace scope
 * (sha256(identity:user):mode, the same scope the chat and preferences use). Without a scope (guest,
 * signed out, not connected yet) nothing is read or written and the answer is always "confirm".
 * The old global key is never inherited by anyone; it is deleted when seen.
 */
const LEGACY_SKIP_CONFIRM_KEY = "ai-trader:quick-order-skip-confirm";
export const quickOrderSkipConfirmKey = (scope: string) => `${LEGACY_SKIP_CONFIRM_KEY}:${scope}`;

export function readQuickOrderSkipConfirm(scope: string | undefined): boolean {
  try {
    localStorage.removeItem(LEGACY_SKIP_CONFIRM_KEY);
    return scope ? localStorage.getItem(quickOrderSkipConfirmKey(scope)) === "1" : false;
  } catch { return false; }
}

/** The switch for the current user: re-read when the scope changes, persisted per scope, off without one. */
export function useQuickOrderSkipConfirm(scope: string | undefined): [boolean, (value: boolean) => void] {
  const [state, setState] = useState(() => ({ scope, value: readQuickOrderSkipConfirm(scope) }));
  let current = state;
  if (state.scope !== scope) { current = { scope, value: readQuickOrderSkipConfirm(scope) }; setState(current); }
  const set = useCallback((value: boolean) => {
    if (!scope) return;
    try { localStorage.setItem(quickOrderSkipConfirmKey(scope), value ? "1" : "0"); } catch { /* Local preference only. */ }
    setState({ scope, value });
  }, [scope]);
  return [Boolean(scope) && current.value, set];
}

interface Input {
  contextKey?: string;
  accountId?: string | null;
  accountGeneration?: number;
  sessionId?: string;
  quote: MarketQuoteSnapshot | undefined;
  runtimeBaseUrl: string | undefined;
  tradeBusy: boolean;
  simulationEnabled: boolean;
  submitManualOrder: (request: ManualSimulationRequest) => Promise<ManualOrderOutcome | undefined>;
  /** "快捷下单免确认": submit right away instead of showing the confirmation dialog. */
  skipConfirm: boolean;
  cancelOrder: (order: SimulationOrder) => void | Promise<void>;
  /** 目标持仓 with `confirm` (快捷区反手): true when the card was confirmed. */
  submitTargetPosition?: (request: { symbol: string; targetNet: number; priceIntent?: "aggressive"; confirm: true }) => Promise<boolean>;
}

/**
 * The ticket's quick row (一键买多 / 卖空 / 平仓): one quick order is one request, confirmed once and submitted
 * once; nothing is ever re-sent. A close spanning 今仓 + 昨仓 is split by the Runtime's planner into one batch
 * card (意图重构第 4 步, 2026-10-16), not here. 画线第二版 (2026-10-09): the chart's right-click menu no longer orders.
 */
export function useQuickOrders({ quote, runtimeBaseUrl, tradeBusy, simulationEnabled, submitManualOrder, skipConfirm, cancelOrder, submitTargetPosition, contextKey, accountId, accountGeneration, sessionId }: Input) {
  const scope = `${contextKey}:${runtimeBaseUrl}:${quote?.symbol}:${accountId}:${accountGeneration}`;
  // Every transition retires the old intent, including A → B → A on older runtimes.
  const epoch = useRef({ scope, generation: 0 });
  if (epoch.current.scope !== scope) epoch.current = { scope, generation: epoch.current.generation + 1 };
  const currentContext = `${scope}:${epoch.current.generation}`;
  const context = useRef(currentContext);
  context.current = currentContext;
  const current = (order: PendingQuickOrder) => order.contextKey === context.current
    && (!sessionId || accountGeneration === undefined
      || (knownAccountRevision(sessionId) ?? accountGeneration) === accountGeneration);
  const [pending, setPending] = useState<PendingQuickOrder>();
  const quickInFlight = useRef(false);
  const [quickBusy, setQuickBusy] = useState(false);
  const [quickMessage, setQuickMessage] = useState("");
  useEffect(() => {setPending(undefined); setQuickMessage("");}, [currentContext]);
  const submitQuick = async (order: PendingQuickOrder) => {
    if (!current(order)) {
      if (order.contextKey === context.current) {
        setPending(undefined); setQuickMessage("交易账户状态已变化，请等待最新状态后重新发起。");
      }
      return;
    }
    if (quickInFlight.current || tradeBusy || (order.kind !== "cancel" && !simulationEnabled) || order.symbol !== quote?.symbol) return;
    quickInFlight.current = true; setQuickBusy(true); setQuickMessage("正在提交模拟委托…"); setPending(undefined);
    try {
      // Counted from the orders the confirmation placed — a batch's children, a skipped bucket — never
      // from the request, and a confirmed request never reads as "not confirmed" (#932 A26).
      if (order.kind === "cancel") {
        for (const item of order.orders) {
          if (!current(order)) break;
          await cancelOrder(item);
        }
        if (current(order)) setQuickMessage("撤单请求已逐笔处理，请查看委托回报。");
      } else if (order.kind === "reverse") {
        const confirmed = await submitTargetPosition?.({ symbol: order.symbol, targetNet: order.targetNet, priceIntent: "aggressive", confirm: true });
        if (current(order)) setQuickMessage(confirmed ? `反手已确认：${order.summary}，平仓全部成交后自动开仓，进度见对话。` : "反手未提交，原因见提示。");
      } else {
        const receipts: string[] = [];
        for (const request of order.requests) {
          if (!current(order)) break;
          const result = await submitManualOrder(request);
          receipts.push(manualOrderReceipt(result));
          if (!result?.accepted) break;
        }
        if (current(order)) setQuickMessage(receipts.join("；"));
      }
    } catch {if (current(order)) setQuickMessage("模拟委托未确认，请查看委托回报，不会自动重提。");}
    finally {quickInFlight.current = false; setQuickBusy(false);}
  };
  /** Entry for every quick order: confirm first unless the user turned confirmation off. */
  const requestAction = (action: Omit<Extract<PendingQuickOrder, {kind: "orders"}>, "symbol" | "contextKey"> | Omit<Extract<PendingQuickOrder, {kind: "cancel"}>, "symbol" | "contextKey">
    | Omit<Extract<PendingQuickOrder, {kind: "reverse"}>, "symbol" | "contextKey">) => {
    if (!quote) return;
    if (quickInFlight.current || pending || tradeBusy) return;
    const order = { symbol: quote.symbol, contextKey: currentContext, ...action };
    if (skipConfirm) void submitQuick(order); else setPending(order);
  };
  return { pending, setPending, quickBusy, quickMessage, requestAction,
    submitQuick };
}

