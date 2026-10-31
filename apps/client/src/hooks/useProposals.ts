import { useEffect, useRef, useState } from "react";
import {
  adjustProposalRisk, answerBreakeven, cancelRun, confirmProposal, getStrategyMonitor,
  rejectProposal, setStrategyMonitoring, type AiExecutionSnapshot, type SimulationOrder
} from "../api";
import type { ConversationCard } from "../state/conversation";
import { confirmBlocked, confirmable } from "../components/cards/confirmGate";
import { saveRoutineDraft } from "../routines";
import type { WorkspaceHandle } from "./workspaceHandle";

/** One busy proposal/order/strategy at a time: taking it while another holds it fails (undefined — the caller sends
 *  nothing, PR #1004 review), and only the owner token may clear it. */
export interface ProposalBusy {
  id: string | undefined;
  set(id?: string, owner?: object): object | undefined;
}

export const BUSY_LINE = "上一个操作还在提交，稍候再试";

export function useProposalBusy(handle: WorkspaceHandle): ProposalBusy {
  const { orderContext, isCurrentOrderContext } = handle;
  const [busyProposal, setBusyProposal] = useState<{ context: typeof orderContext; id?: string }>();
  const proposalOperation = useRef<object | undefined>(undefined);
  // A new order context (another Runtime, session or identity) starts unheld: the old one's late finish clears nothing.
  useEffect(() => { proposalOperation.current = undefined; setBusyProposal(undefined); }, [orderContext]);
  return {
    id: busyProposal?.context === orderContext ? busyProposal.id : undefined,
    set: (id?: string, owner?: object) => {
      if (!isCurrentOrderContext()) return undefined;
      if (id === undefined && proposalOperation.current !== owner) return undefined;
      if (id !== undefined && proposalOperation.current !== undefined) return undefined;  // held: no second write
      const token = id === undefined ? undefined : {};
      proposalOperation.current = token;
      setBusyProposal({ context: orderContext, id });
      return token;
    }
  };
}

function strategyMonitorError(error: unknown): string {
  const detail = error instanceof Error ? error.message : "";
  if (detail.includes("strategy_validation_not_passed")) return "策略还没有完成正式验证，暂时不能启动";
  if (detail.includes("strategy_transition_invalid")) return "策略当前状态不能执行这个操作，请刷新后重试";
  if (detail.includes("strategy_monitor_unavailable")) return "模拟交易通道暂时不可用，策略没有启动";
  if (detail.includes("strategy_not_found") || detail.includes("404")) return "没有找到这张策略，可能已被移除";
  return "策略状态更新失败，请稍后重试";
}

interface ProposalsInput {
  handle: WorkspaceHandle;
  busy: ProposalBusy;
  /** A confirmed card's returned orders, into its own account's list (the current one only for its own cards). */
  recordConfirmedOrders: (cardAccount: string | undefined, orders: SimulationOrder[]) => void;
  /** Re-read the orders of a card's account: a trade card confirmed but refused at send leaves its order 待发送. */
  refreshSimulationOrders: (cardAccount?: string) => Promise<unknown>;
  aiExecution: AiExecutionSnapshot | undefined;
  /** Why the account or the simulation gateway cannot take a confirmation now (undefined = it can). */
  confirmGate: string | undefined;
  /** Whether a card's account is the one the conversation is on; `confirmGate` applies only to those cards. */
  onCurrentAccount?: (accountId?: string) => boolean;
}

/** Deterministic business confirmations: a proposal card's own 确认 submits it (owner 2026-10-19: no second
 *  confirmation dialog — the card states the order; the Runtime re-checks everything on confirmation). */
export function useProposals({ handle, busy, recordConfirmedOrders, refreshSimulationOrders, aiExecution, confirmGate, onCurrentAccount }: ProposalsInput) {
  const { baseUrl, connection, orderContext, isCurrentOrderContext, setToast } = handle;

  const adjustRisk = async (proposalId: string, proposalVersion: number) => {
    if (connection !== "connected" || !isCurrentOrderContext()) return;
    const busyToken = busy.set(proposalId);
    if (!busyToken) { setToast(BUSY_LINE, "error"); return; }
    try {
      await adjustProposalRisk(baseUrl, proposalId, proposalVersion);
      if (isCurrentOrderContext()) setToast("候选已按单笔风险上限调整");
    } catch (error) {
      if (isCurrentOrderContext()) setToast(error instanceof Error ? error.message : "风险调整失败", "error");
    } finally {
      busy.set(undefined, busyToken);
    }
  };

  const reject = async (proposalId: string, proposalVersion: number) => {
    if (connection !== "connected" || !isCurrentOrderContext()) return;
    const busyToken = busy.set(proposalId);
    if (!busyToken) { setToast(BUSY_LINE, "error"); return; }
    try {
      await rejectProposal(baseUrl, proposalId, proposalVersion);
      if (isCurrentOrderContext()) setToast("候选已取消，未执行任何操作", "success");
    } catch (error) {
      if (isCurrentOrderContext()) setToast(error instanceof Error ? error.message : "取消失败", "error");
    } finally {
      busy.set(undefined, busyToken);
    }
  };

  // The card being confirmed (no dialog stands for it since 2026-10-19): while it is set every other card's 确认 and
  // the ticket wait — one protected submission at a time, as the old modal dialog made it (PR #1004 review).
  const [confirming, setConfirming] = useState<{ context: unknown; id: string }>();
  // Scoped to the order context it started in: another session / Runtime / identity starts with nothing in flight.
  const confirmingId = confirming?.context === orderContext ? confirming.id : undefined;

  const confirm = async (card: ConversationCard) => {
    if (connection !== "connected" || !isCurrentOrderContext() || !confirmable(card)) return;
    const blocked = confirmBlocked(card, confirmGate, onCurrentAccount);
    if (blocked) { setToast(blocked, "error"); return; }  // the card's button is disabled with the same reason
    const pendingApproval = card;
    const proposalId = pendingApproval.payload.proposalId;
    const busyToken = busy.set(proposalId);
    if (!busyToken) { setToast(BUSY_LINE, "error"); return; }  // another write holds the lock: nothing is sent
    const confirmingNow = { context: orderContext, id: proposalId };
    setConfirming(confirmingNow);
    try {
      if (!pendingApproval.payload.approvalId) throw new Error("审批凭证不存在或已过期");
      const result = await confirmProposal(baseUrl, proposalId, pendingApproval.payload.approvalId, pendingApproval.payload.version);
      if (!isCurrentOrderContext()) return;
      // The returned orders belong to the card's own account (#1011 review): its list, not the current one's.
      const cardAccount = "accountId" in pendingApproval.payload ? pendingApproval.payload.accountId : undefined;
      if (result.simulationOrder) recordConfirmedOrders(cardAccount, [result.simulationOrder]);
      const children = (result.simulationOrders ?? []).filter((order): order is SimulationOrder => Boolean(order.id));
      recordConfirmedOrders(cardAccount, children);
      setToast(
        pendingApproval.type === "close-batch"
          // The Runtime counts this from the persisted orders; never from what we asked for.
          ? `批量平仓${result.execution?.text?.replace(/^已确认，?/, "：") ?? "已确认"}，逐笔见委托栏`
        : pendingApproval.type === "spread"
          // Both legs went out together; fills, 补单 and 回退 are followed by the Runtime and land on the card.
          ? `价差两腿${result.execution?.text?.replace(/^已确认，?/, "：") ?? "已确认"}，结果见卡片与委托栏`
        : pendingApproval.type === "trade"
          ? result.simulationOrder
            ? `模拟委托：${result.simulationOrder.displayStatus}`
            : "模拟候选已记录；当前未连接委托网关"
          : "风险规则已生效，应用会严格执行"
      );
    } catch (error) {
      // A refusal that carries its own sentence (a strategy card under a stop, an expired card) shows just that.
      const detail = (error as { detail?: { detail?: { code?: string; message?: string } } })?.detail?.detail;
      if (isCurrentOrderContext()) setToast(detail?.message ?? (error instanceof Error ? `服务端复核失败：${error.message}` : "确认失败"), "error");
      // The confirmation may have committed before the send was refused (stale quote, gateway down):
      // read the order back so the card says 未发送 instead of 已发送. Nothing is re-sent.
      if (isCurrentOrderContext() && pendingApproval.type === "trade") void refreshSimulationOrders(pendingApproval.payload.accountId).catch(() => undefined);
    } finally {
      busy.set(undefined, busyToken);
      setConfirming(current => current === confirmingNow ? undefined : current);  // never a newer context's
    }
  };

  const proposalActions = {
    busyProposalId: busy.id,
    strategyRefreshKey: `${aiExecution?.authorization?.status ?? "none"}:${aiExecution?.authorization?.updatedAt ?? "none"}`,
    adjustRisk,
    confirmGate,
    confirming: confirmingId,
    confirm: (card: ConversationCard) => void confirm(card),
    reject,
    // 保本提醒: the card's 移到成本 / 不用 — the card itself comes back republished with the outcome.
    breakeven: (breakevenId: string, choice: "move" | "decline") => {
      if (connection !== "connected") return;
      void answerBreakeven(baseUrl, breakevenId, choice)
        .then(result => {
          if (!isCurrentOrderContext()) return;
          if (result.status === "moved") setToast(result.result ?? "止损已移到成本", "success");
          else if (result.status === "failed") setToast(result.result ?? "止损没移", "error");
          else if (result.status === "declined") setToast("不移了，这笔今天不再问");
        })
        .catch((error: unknown) => {
          const detail = (error as { detail?: { detail?: { message?: string } } })?.detail?.detail;
          if (isCurrentOrderContext()) setToast(detail?.message ? `没移：${detail.message}` : "保本提醒没办成，请稍后再点", "error");
        });
    },
    cancelRun: (runId: string) => {
      void cancelRun(baseUrl, runId)
        .then(() => setToast("任务停止请求已提交"))
        .catch((error: unknown) => setToast(error instanceof Error ? error.message : "停止任务失败", "error"));
    },
    changeStrategyMonitoring: async (strategyId: string, action: "start" | "pause") => {
      const busyToken = busy.set(strategyId);
      if (!busyToken) throw new Error(BUSY_LINE);
      try {
        const result = await setStrategyMonitoring(baseUrl, strategyId, action);
        if (isCurrentOrderContext()) setToast(action === "start" ? "策略已开始模拟监控" : "策略已暂停");
        return result;
      } catch (error) {
        const message = strategyMonitorError(error);
        if (isCurrentOrderContext()) setToast(message);
        throw new Error(message, { cause: error });
      } finally {
        busy.set(undefined, busyToken);
      }
    },
    // 我的流程 (2026-10-06): the 流程卡's 保存 — the user's own write; the card turns 已保存 when the
    // Runtime republishes it, a refusal comes back as the route's sentence on the card.
    saveRoutine: (draftId: string) => saveRoutineDraft(baseUrl, draftId),
    loadStrategyMonitor: async (strategyId: string) => {
      try {
        return await getStrategyMonitor(baseUrl, strategyId);
      } catch (error) {
        throw new Error(strategyMonitorError(error), { cause: error });
      }
    }
  };

  return { proposalActions, confirming: confirmingId };
}
