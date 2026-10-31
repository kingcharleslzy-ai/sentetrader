import { useEffect, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from "react";
import {
  confirmProposal, createSimulationProposal, createTargetPositionProposal, fetchAccountDirectory, listSimulationOrders, mutateSimulationOrder,
  type MarketWorkbenchSnapshot, type RemoteOrderSnapshot, type SimulationOrder
} from "../api";
import { knownAccountRevision } from "../accountRevision";
import { activeTradingAccount } from "../tradingAccountScope";
import { onOrderFact } from "../runtimeFeed";
import type { ManualSimulationRequest } from "../components/FuturesDecisionWorkspace";
import { SENT_STATUSES, UNKNOWN_STATUSES, skipLabel } from "../components/cards/CloseBatchProposalCard";
import { orderStatusText } from "../components/futures/support";
import { BUSY_LINE, type ProposalBusy } from "./useProposals";
import type { WorkspaceHandle } from "./workspaceHandle";

/** An order that went out and is working or (partly) filled. */
const ACCEPTED_STATUSES = new Set(["已受理", "部分成交", "部分成交后已结束", "已成交"]);

/**
 * What one manual ticket submission actually did (#932 A26). A 上期所/能源中心 close spanning 今仓 + 昨仓 is
 * one confirmation of several child orders, so "confirmed" and "every child is working" are separate
 * facts: a confirmed request is never offered for sending again, whatever its children's statuses are.
 */
export interface ManualOrderOutcome {
  /** The Runtime recorded the confirmation (or the original confirmation's orders were read back). */
  confirmed: boolean;
  /** Every order the card carried was placed and is working or filled (the ticket may clear its draft). */
  accepted: boolean;
  /** The placed orders as the Runtime read them back — one, or each child of a batch. */
  orders: SimulationOrder[];
  /** Position buckets the planner could not turn into an order (never sent): lots and the reason. */
  skipped: { volume: number; label: string }[];
}

/** 卖平今 / 卖平昨 / 买开 …: one order's side and offset, short. */
export function orderLegLabel(order: SimulationOrder): string {
  const { direction, offset, symbol } = order.request;
  const side = direction === "BUY" ? "买" : "卖";
  if (offset === "OPEN") return `${side}开`;
  if (offset === "CLOSETODAY") return `${side}平今`;
  return ["SHFE", "INE"].includes(symbol.split(".")[0].toUpperCase()) ? `${side}平昨` : `${side}平`;
}

/**
 * The ticket's receipt, counted from the orders actually placed (never from the request): each child's
 * side, lots, price and own status, and buckets the planner skipped. Once confirmed it never suggests
 * submitting again — an unknown result is queried, an unsent child is sent or abandoned from 委托.
 */
export function manualOrderReceipt(outcome: ManualOrderOutcome | undefined): string {
  if (!outcome?.confirmed) return "本次提交未确认，请查看委托回报，不会自动重提。";
  const volume = outcome.orders.reduce((sum, order) => sum + order.request.volume, 0);
  const skipped = outcome.skipped.length
    ? `；另 ${outcome.skipped.reduce((sum, item) => sum + item.volume, 0)} 手未纳入（${outcome.skipped.map(item => item.label).join("；")}）` : "";
  const detail = outcome.orders
    .map(order => `${orderLegLabel(order)} ${order.request.volume} 手 @ ${order.request.limitPrice} ${orderStatusText(order)}`).join("、");
  if (outcome.accepted) return `模拟委托已受理，共 ${volume} 手：${detail}${skipped}；成交状态见委托分页。`;
  const hints = [
    outcome.orders.some(order => UNKNOWN_STATUSES.has(order.displayStatus)) ? "结果未知的先在委托分页查询，不要重复下单" : "",
    outcome.orders.some(order => !SENT_STATUSES.has(order.displayStatus) && !UNKNOWN_STATUSES.has(order.displayStatus))
      ? "未发送的在委托分页继续发送或放弃" : "",
  ].filter(Boolean);
  return `已确认，共 ${volume} 手：${detail}${skipped}。`
    + (hints.length ? `${hints.join("；")}，系统不会自动重发。` : "系统不会自动重提，逐笔见委托分页。");
}

/** A confirmed card's outcome from its placed orders: accepted only when every order of the card is working. */
function confirmedOutcome(orders: SimulationOrder[], expected: number, skipped: ManualOrderOutcome["skipped"] = []): ManualOrderOutcome {
  return { confirmed: true, orders, skipped,
    accepted: orders.length === expected && orders.every(order => ACCEPTED_STATUSES.has(order.displayStatus)) };
}

export function mergeOrderFacts(previous: SimulationOrder | undefined, next: SimulationOrder): SimulationOrder {
  if (!previous) return next;
  if (previous.factRevision !== undefined && next.factRevision !== undefined) return previous.factRevision > next.factRevision ? previous : next;
  if ((previous.filledVolume || 0) > (next.filledVolume || 0) || (previous.remoteStatus === "FINISHED" && next.remoteStatus !== "FINISHED")) return previous;
  return next;
}

/**
 * What a manual ticket needs at submit time. The App refreshes the ref on every
 * render because these values derive from hooks that run after this one; read
 * `ticket.current` only inside an event callback, never during render.
 */
export interface ManualTicketContext {
  market: MarketWorkbenchSnapshot | undefined;
  /** Market, account and gateway all allow a manual ticket right now. */
  enabled: boolean;
  /** A confirmed manual order changes the account; the overview is re-read. */
  onAccountChanged: () => void;
}

interface SimulationOrdersInput {
  handle: WorkspaceHandle;
  busy: ProposalBusy;
  ticket: RefObject<ManualTicketContext>;
  /**
   * The trading account the session acts on, as runtime.status names it (#930): `undefined` until
   * the first status arrives, `null` when the Runtime reports no account block. The list is that
   * account's: it is read only once the account is known (a read without it lands on the user's
   * default account), carries it explicitly, and is read again when the account changes.
   */
  accountId: string | null | undefined;
  /** The accounts the conversation's confirmed order cards belong to (#1005, the card's `accountId`). */
  cardAccounts?: string[];
}

/** The visible simulation orders, their refresh, per-order actions and the manual ticket. */
export function useSimulationOrders({ handle, busy, ticket, accountId, cardAccounts = [] }: SimulationOrdersInput) {
  const { baseUrl, connection, sessionId, replayPath, fence, orderContext, isCurrentOrderContext, setToast } = handle;
  const orderChannel = fence.channel("orders");
  const account = useRef(accountId);
  account.current = accountId;
  const [simulationOrderState, setSimulationOrderState] = useState<{ context: typeof orderContext; account: string | null | undefined; items: SimulationOrder[] }>();
  const simulationOrders = accountId !== undefined && simulationOrderState?.context === orderContext
    && simulationOrderState.account === accountId ? simulationOrderState.items : [];
  const setSimulationOrders: Dispatch<SetStateAction<SimulationOrder[]>> = (update) => {
    if (!isCurrentOrderContext()) return;
    orderChannel.noteWrite();
    const target = account.current;
    setSimulationOrderState(previous => {
      if (!isCurrentOrderContext() || account.current !== target) return previous;
      const items = previous?.context === orderContext && previous.account === target ? previous.items : [];
      const incoming = typeof update === "function" ? update(items) : update;
      return { context: orderContext, account: target, items: incoming.map(item => mergeOrderFacts(items.find(old => old.clientOrderId === item.clientOrderId), item)) };
    });
  };
  const refreshSimulationOrders = async (recoveryWait = false) => {
    const target = account.current;
    const read = orderChannel.beginRead();
    const result = await listSimulationOrders(baseUrl, recoveryWait, target);
    // A read for an account the session has since left never lands on the new one.
    const fresh = () => read() && account.current === target;
    if (fresh()) {
      setSimulationOrderState(previous => fresh()
        ? {context: orderContext, account: target, items: result.items.map(item => mergeOrderFacts(previous?.context === orderContext && previous.account === target ? previous.items.find(old => old.clientOrderId === item.clientOrderId) : undefined, item))} : previous);
    }
    return result;
  };
  useEffect(() => { setSimulationOrderState(undefined); }, [orderContext]);
  // #1005: a confirmed card reads its order status from its own account, never from the one the
  // session is on now. Another account's cards read that account's list (the same read, naming it),
  // kept apart from the current list (the 委托 tab and every order action stay on the current account).
  const [cardAccountOrders, setCardAccountOrders] = useState<{ context: typeof orderContext; account: string; lists: Record<string, SimulationOrder[]> }>();
  const otherLists = cardAccountOrders?.context === orderContext && cardAccountOrders.account === accountId ? cardAccountOrders.lists : {};
  /** The orders a card of `cardAccount` reads: the current list for the session's account (or a card
   *  without an account); that account's own list otherwise, `undefined` until it is read. */
  const onCurrentAccount = (cardAccount?: string) => !cardAccount || typeof accountId !== "string" || cardAccount === accountId;
  const ordersFor = (cardAccount?: string): SimulationOrder[] | undefined =>
    onCurrentAccount(cardAccount) ? simulationOrders : otherLists[cardAccount!];
  /** A confirmed card's returned orders land in its own account's list (#1011 review): the current list only
   *  for a card on the session's account; another account's card adds them to that account's list — never to
   *  the current account's 委托 / 持仓 view, whose feed and order actions belong to the current account. */
  const recordConfirmedOrders = (cardAccount: string | undefined, confirmed: SimulationOrder[]) => {
    if (!confirmed.length) return;
    const merge = (items: SimulationOrder[]) => [...confirmed.map(order =>
      mergeOrderFacts(items.find(old => old.clientOrderId === order.clientOrderId), order)),
      ...items.filter(old => !confirmed.some(order => order.clientOrderId === old.clientOrderId))];
    if (onCurrentAccount(cardAccount)) { setSimulationOrders(merge); return; }
    if (!isCurrentOrderContext() || typeof accountId !== "string") return;
    const other = cardAccount!, current = accountId, context = orderContext;
    setCardAccountOrders(previous => {
      const lists = previous?.context === context && previous.account === current ? previous.lists : {};
      return { context, account: current, lists: { ...lists, [other]: merge(lists[other] ?? []) } };
    });
  };
  const otherAccounts = typeof accountId === "string"
    ? [...new Set(cardAccounts)].filter(id => id !== accountId).sort().join(",") : "";
  /** Read another account's list for its cards (#1005); a read that lands after the session moved is dropped. */
  const readCardAccount = async (other: string) => {
    if (typeof accountId !== "string") return;
    const current = accountId, context = orderContext;
    const { items } = await listSimulationOrders(baseUrl, false, other);
    if (!isCurrentOrderContext() || account.current !== current) return;
    // Merged by fact like the current list (mergeOrderFacts never regresses a row), so an older read that lands
    // after a newer one — the background read and a confirmation's recovery read — cannot undo it.
    setCardAccountOrders(previous => {
      const lists = previous?.context === context && previous.account === current ? previous.lists : {};
      return { context, account: current, lists: { ...lists,
        [other]: items.map(item => mergeOrderFacts(lists[other]?.find(old => old.clientOrderId === item.clientOrderId), item)) } };
    });
  };
  /** Re-read the orders a card of `cardAccount` shows: the current list, or that account's own (#932 F1 review:
   *  another account's card confirmed but refused at send must not stay 结果未知 on a stale list). */
  const refreshOrdersFor = (cardAccount?: string) =>
    onCurrentAccount(cardAccount) ? refreshSimulationOrders() : readCardAccount(cardAccount!);
  useEffect(() => {
    if (connection !== "connected" || replayPath || typeof accountId !== "string" || !otherAccounts) return;
    // Only accounts the user may still act on (#1205: a retired 快期模拟 account's old cards would read 403 each load).
    void fetchAccountDirectory(baseUrl).then(directory => {
      const granted = new Set(directory.accounts.map(entry => entry.id));
      for (const other of otherAccounts.split(",").filter(id => granted.has(id))) {
        void readCardAccount(other).catch(() => undefined);  // unreadable (not assembled): the card stays 已确认
      }
    }).catch(() => undefined);
  }, [otherAccounts, orderContext, baseUrl, connection, replayPath, accountId]);
  useEffect(() => {
    if (connection !== "connected" || accountId === undefined) return;
    void refreshSimulationOrders(!replayPath).catch(() => undefined);
  }, [orderContext, baseUrl, sessionId, connection, replayPath, accountId]);
  /** Actions and pushed facts share the account routing and monotonic merge for every list. */
  const mergeOrderFact = (order: SimulationOrder, target = order.accountId ?? account.current) => {
    if (!isCurrentOrderContext() || account.current !== accountId) return;
    const merge = (items: SimulationOrder[]) => items.some(item => item.clientOrderId === order.clientOrderId)
      ? items.map(item => item.clientOrderId === order.clientOrderId ? mergeOrderFacts(item, order) : item)
      : [order, ...items];
    if (!target || target === account.current) setSimulationOrders(merge);
    if (!target || typeof accountId !== "string") return;
    setCardAccountOrders(previous => {
      if (!isCurrentOrderContext() || account.current !== accountId
          || previous?.context !== orderContext || previous.account !== accountId || !previous.lists[target]) return previous;
      return { ...previous, lists: { ...previous.lists, [target]: merge(previous.lists[target]) } };
    });
  };
  // A watched order's read-back updates the same lists as an explicit action, without waiting for polling.
  useEffect(() => onOrderFact((eventSessionId, fact) => {
    if (connection !== "connected" || replayPath || eventSessionId !== sessionId || !fact.clientOrderId) return;
    mergeOrderFact(fact as unknown as SimulationOrder);
  }), [sessionId, connection, replayPath, orderContext, accountId]);

  const simulationOrderAction = async (order: SimulationOrder, action: "continue" | "abandon" | "query" | "cancel") => {
    if (connection !== "connected" || !isCurrentOrderContext()) return;
    const busyToken = busy.set(order.clientOrderId);
    if (!busyToken) { setToast(BUSY_LINE, "warning"); return; }
    // A card or merged foreign row acts on its own account; App also re-reads the foreign half afterwards.
    const target = order.accountId ?? account.current;
    try {
      const updated = await mutateSimulationOrder(baseUrl, order.clientOrderId, action, target);
      if (!isCurrentOrderContext()) return;
      mergeOrderFact(updated, updated.accountId ?? target);
      setToast(`模拟委托：${updated.displayStatus}`);
    } catch (error) {
      if (!isCurrentOrderContext()) return;
      if (action !== "query" && (error as { status?: number })?.status === 409) {
        try {
          const updated = await mutateSimulationOrder(baseUrl, order.clientOrderId, "query", target);
          if (!isCurrentOrderContext()) return;
          mergeOrderFact(updated, updated.accountId ?? target);
          setToast(updated.remoteStatus === "FINISHED"
            ? `这笔委托已经结束（${updated.displayStatus}），${action === "cancel" ? "不用再撤" : "无需重复操作"}`
            : `已回读这笔委托：${updated.displayStatus}，请按最新状态操作`, "warning");
          return;
        } catch {
          if (isCurrentOrderContext()) setToast("委托状态已变化，但回读失败，请查询原委托核对", "error");
          return;
        }
      }
      setToast(error instanceof Error ? error.message : "模拟委托操作失败", "error");
    } finally {
      busy.set(undefined, busyToken);
    }
  };

  const manualSubmitInFlight = useRef<typeof orderContext | undefined>(undefined);
  /** 目标持仓：Runtime 算差额并把第一段的确认卡发进会话，用户在会话里确认。`confirm`（快捷区反手，2026-10-24）：
   *  快捷区自己的确认框（或用户开了免确认）就是这次确认，这张卡当场走普通确认路由；平仓全部成交后由 Runtime 接力
   *  自动发开仓，客户端不做接力。 */
  const submitTargetPosition = async (request: { symbol: string; targetNet: number; priceIntent?: "passive" | "aggressive" | "last"; confirm?: boolean }) => {
    if (connection !== "connected" || !isCurrentOrderContext()) return false;
    const { market, enabled } = ticket.current;
    if (!sessionId || !market) {
      setToast("请先打开一个可交易合约的实时行情", "warning");
      return false;
    }
    if (!enabled) {
      setToast("当前行情、账户或模拟网关状态不允许提交", "warning");
      return false;
    }
    const busyToken = busy.set("target-ticket");
    if (!busyToken) { setToast(BUSY_LINE, "warning"); return false; }
    try {
      const answer = await createTargetPositionProposal(baseUrl, {
        sessionId,
        symbol: request.symbol,
        targetNet: request.targetNet,
        priceIntent: request.priceIntent,
        reason: request.confirm ? `用户在快捷区对 ${request.symbol} 反手（目标净持仓 ${request.targetNet} 手）。`
          : `用户在下单台把 ${request.symbol} 的目标净持仓设为 ${request.targetNet} 手。`
      });
      if (!isCurrentOrderContext()) return false;
      if (answer.cardIssued && request.confirm) {
        if (!answer.proposalId || !answer.approvalId || answer.proposalVersion == null) throw new Error("方案卡缺少确认信息，请在会话中确认");
        const result = await confirmProposal(baseUrl, answer.proposalId, answer.approvalId, answer.proposalVersion);
        if (!isCurrentOrderContext()) return false;
        const placed = result.simulationOrder ? [result.simulationOrder] : (result.simulationOrders ?? []).filter(order => Boolean(order.id));
        setSimulationOrders(orders => [...placed, ...orders.filter(order => !placed.some(item => item.clientOrderId === order.clientOrderId))]);
        ticket.current.onAccountChanged();
        setToast(`反手已确认：${answer.targetPosition?.summary ?? request.symbol}`);
        return true;
      }
      if (answer.cardIssued) {
        setToast(`方案卡已生成：${answer.targetPosition?.summary ?? request.symbol}，请在会话中确认`);
        return true;
      }
      const line = answer.userFacing?.candidateStatus
        ?? answer.userFacing?.candidateIssues?.[0]
        ?? "当前不需要下单";
      setToast(line);
      return false;
    } catch (error) {
      if (isCurrentOrderContext()) setToast(error instanceof Error ? error.message : "目标持仓方案生成失败", "error");
      return false;
    } finally {
      busy.set(undefined, busyToken);
    }
  };
  const submitManualSimulationOrder = async (request: ManualSimulationRequest): Promise<ManualOrderOutcome | undefined> => {
    if (connection !== "connected" || !isCurrentOrderContext() || manualSubmitInFlight.current === orderContext) return undefined;
    const target = account.current;
    const revision = sessionId ? knownAccountRevision(sessionId) : undefined;
    const current = () => isCurrentOrderContext() && account.current === target
      && (!activeTradingAccount() || activeTradingAccount() === target)
      && (!sessionId || knownAccountRevision(sessionId) === revision);
    if (!current()) {
      if (isCurrentOrderContext()) setToast("交易账户状态已变化，请等待最新状态后再提交。", "warning");
      return undefined;
    }
    const { market, enabled, onAccountChanged } = ticket.current;
    if (!sessionId || !market) {
      setToast("请先打开一个可交易合约的实时行情", "warning");
      return undefined;
    }
    if (!enabled) {
      setToast("当前行情、账户或模拟网关状态不允许提交", "warning");
      return undefined;
    }
    const busyToken = busy.set("manual-ticket");
    if (!busyToken) { setToast(BUSY_LINE, "warning"); return undefined; }
    manualSubmitInFlight.current = orderContext;
    let createdProposalId: string | undefined;
    let confirmationStarted = false;
    // What the card carries, for a confirmation whose answer is lost and must be read back.
    let planned = { orders: 1, volume: request.volume, skipped: [] as ManualOrderOutcome["skipped"] };
    try {
      const proposal = await createSimulationProposal(baseUrl, {
        sessionId,
        symbol: request.symbol ?? market.quote.symbol,
        side: request.side,
        positionBucket: request.positionBucket,
        volume: request.volume,
        priceIntent: request.priceIntent,
        limitPrice: request.limitPrice,
        // 下单顺带保护只随开仓走（平仓卡不携带）；未填就是没有。
        protection: request.side === "buy_open" || request.side === "sell_open" ? request.protection : undefined,
        playbookId: request.side === "buy_open" || request.side === "sell_open" ? request.playbookId : undefined,
        reason: request.source === "context-menu" ? "用户在图表右键菜单明确授权一张模拟限价委托。" : "用户在三键交易台明确授权一张手工模拟委托。"
      }, target);
      if (!current()) return undefined;
      createdProposalId = proposal.proposalId;
      // A 上期所/能源中心 close spanning 今仓 + 昨仓 comes back as one batch card (the planner's split); the
      // ticket's click confirms it exactly like the single card — one confirmation, one send per child order.
      const batch = "orders" in proposal;
      if (proposal.status === "risk_blocked") {
        throw new Error(String(proposal.riskCheck.reason || "确定性风控未通过"));
      }
      // The batch's own sends are gated by the Runtime per child order (dispatch refuses a candidate made
      // without a gateway); the single card says it on itself.
      if (!proposal.approvalId || (!batch && proposal.executionEligibleAtCreation !== true)) {
        throw new Error("候选创建时模拟网关不可执行，请刷新后重试");
      }
      if (batch) {
        planned = { orders: proposal.totalOrders, volume: proposal.totalVolume,
          skipped: (proposal.skipped ?? []).map(item => ({ volume: Number(item.volume) || 0, label: skipLabel(item) })) };
      }
      confirmationStarted = true;
      const result = await confirmProposal(baseUrl, proposal.proposalId, proposal.approvalId, proposal.version, target);
      if (!current()) return undefined;
      // A batch answers with one fact per card order, in the card's order; a child without an order
      // record was never prepared (never sent) and is reported with the card's own lots.
      const children = batch ? result.simulationOrders ?? [] : [];
      const placed = batch
        ? children.filter((order): order is SimulationOrder => Boolean(order.id))
        : result.simulationOrder ? [result.simulationOrder] : [];
      if (!placed.length) {
        throw new Error("模拟委托未返回可核对的订单记录");
      }
      const skipped = batch ? [
        ...planned.skipped,
        ...proposal.orders.filter((_, index) => !children[index]?.id)
          .map(order => ({ volume: order.volume, label: `${order.symbol.split(".").at(-1)} 委托未生成，未发送` })),
      ] : [];
      setSimulationOrders((orders) => [
        ...placed,
        ...orders.filter((order) => !placed.some((item) => item.clientOrderId === order.clientOrderId))
      ]);
      setToast(batch
        // The Runtime counts this from the persisted orders; never from what we asked for.
        ? `手工模拟平仓${result.execution?.text?.replace(/^已确认，?/, "：") ?? "已确认"}，逐笔见委托栏`
        : `手工模拟委托：${placed[0].displayStatus}`);
      onAccountChanged();
      return confirmedOutcome(placed, planned.orders, skipped);
    } catch (error) {
      if (!current()) return undefined;
      const detail = error instanceof Error ? error.message : "未知错误";
      if (confirmationStarted && createdProposalId) {
        try {
          const { items } = await refreshSimulationOrders(true);
          if (!current()) return undefined;
          // Every order read back for this card — never just the first — against what the card carried:
          // a card is accepted only when all its orders are back and working, and lots not read back
          // are named (queried, never resent).
          const recovered = items.filter((order) => order.proposalId === createdProposalId);
          if (recovered.length) {
            setToast(`已回读模拟委托：${recovered.map((order) => order.displayStatus).join("、")}`);
            // The original confirmation went through after all: its orders are the outcome, never a retry,
            // and the account is re-read exactly as after a normal confirmation.
            onAccountChanged();
            const missing = planned.orders - recovered.length;
            const missingVolume = planned.volume - recovered.reduce((sum, order) => sum + order.request.volume, 0);
            return confirmedOutcome(recovered, planned.orders, [...planned.skipped,
              ...(missing > 0 ? [{ volume: Math.max(0, missingVolume), label: `${missing} 笔未回读到，先在委托分页查询` }] : [])]);
          } else {
            setToast(`确认未完成：${detail}；候选已保留，系统不会自动重提`);
          }
        } catch {
          if (current()) setToast(`提交结果未确认：${detail}；系统不会自动重提，请先查询原候选`, "error");
        }
      } else {
        setToast(`提交失败：${detail}`, "error");
      }
      return undefined;
    } finally {
      if (manualSubmitInFlight.current === orderContext) manualSubmitInFlight.current = undefined;
      busy.set(undefined, busyToken);
    }
  };

  return { simulationOrders, ordersFor, onCurrentAccount, recordConfirmedOrders, setSimulationOrders, refreshSimulationOrders, refreshOrdersFor, simulationOrderAction, submitManualSimulationOrder, submitTargetPosition };
}

/**
 * The account's own order read-back (account overview → activity.orders, by clientOrderId) that the
 * list does not show yet: a working order missing from it, or a row whose state or fill differs.
 * Empty when the list is current. The key changes only when the account's orders do.
 */
export function ordersBehindAccount(orders: SimulationOrder[], remote: RemoteOrderSnapshot[] | undefined): string {
  if (!remote?.length) return "";
  const listed = new Map(orders.map(order => [order.clientOrderId, order]));
  return remote.filter(item => {
    const row = listed.get(item.clientOrderId);
    if (!row) return item.status === "ALIVE";
    return row.remoteStatus !== item.status || (row.filledVolume || 0) !== (item.filledVolume || 0);
  }).map(item => `${item.clientOrderId}:${item.status}:${item.filledVolume}`).sort().join("|");
}

/**
 * #930: an order placed on this account from any other entry — another chat, the CLI or MCP, a
 * strategy, another window — reaches this window only through the account's read-back, never
 * through this session's feed.order. When that read-back shows orders the list does not, the list
 * is read again, so the 委托 tab and the bottom bar show (and can cancel) every working order.
 */
export function useOrdersFollowAccount(
  orders: SimulationOrder[], remote: RemoteOrderSnapshot[] | undefined, refresh: (recoveryWait?: boolean) => Promise<unknown>,
) {
  const behind = ordersBehindAccount(orders, remote);
  const latest = useRef(refresh);
  latest.current = refresh;
  useEffect(() => {
    if (behind) void latest.current(false).catch(() => undefined);
  }, [behind]);
}
