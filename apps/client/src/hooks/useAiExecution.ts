import { useEffect, useRef, useState } from "react";
import { cancelAiStop, createAiStop, getAiExecution, repriceAiStop, resumeAiExecution, stopAiExecution, type AiExecutionSnapshot, type AiStopLoss, type StopMotion } from "../api";
import { feedIsLive, type RuntimeFeedState } from "../runtimeFeed";
import type { WorkbenchDataStatus } from "./useWorkbenchStatus";
import type { WorkspaceHandle } from "./workspaceHandle";

interface AiExecutionInput {
  handle: WorkspaceHandle;
  accountStatus: WorkbenchDataStatus;
  feed: RuntimeFeedState;
  /** The symbol a resume re-arms execution for. */
  symbol: string | undefined;
  /** An active authorization keeps the visible orders fresh alongside its state. */
  refreshSimulationOrders: (recoveryWait?: boolean) => Promise<unknown>;
}

/** AI simulation execution: authorization/controls/stops state, and the stop/resume operations. */
/** Codes the stop routes refuse with and no Chinese sentence of their own (the 422 price
 *  refusals carry `message`): never show the raw `409 {"detail":…}` to the user. */
const PROTECTION_REFUSALS: Record<string, string> = {
  market_session_closed: "现在休市，开盘后再挂保护",
  quote_not_current: "现在行情不是最新（休市或行情暂停），开盘后再挂保护",
  ai_execution_stopped: "模拟执行已急停，恢复后再挂保护",
  account_read_only: "这个账户只读，挂不了保护",
  not_execution_owner: "这个账户不由你执行，挂不了保护",
  stop_not_armed: "这条保护已经触发或撤销了",
  stop_not_cancelable: "这条保护已经触发或撤销了，撤不了",
  close_bucket_required: "上期所/能源中心的持仓要分今仓、昨仓分别挂保护",
};

function protectionRefusal(error: unknown, action: string): string {
  // requestJson nests FastAPI's body under `detail`; the 422 refusals carry their Chinese
  // reason one level deeper (same shape useProposals reads).
  const detail = (error as { detail?: { detail?: { message?: string; code?: string } | string } }).detail?.detail;
  if (detail && typeof detail === "object") {
    if (detail.message) return detail.message;
    if (detail.code && PROTECTION_REFUSALS[detail.code]) return PROTECTION_REFUSALS[detail.code];
  }
  if (typeof detail === "string" && PROTECTION_REFUSALS[detail]) return PROTECTION_REFUSALS[detail];
  return `${action}没成功，请稍后重试`;
}

export function useAiExecution({ handle, accountStatus, feed, symbol, refreshSimulationOrders }: AiExecutionInput) {
  const { baseUrl, connection, sessionId, replayPath, fence, orderContext, isCurrentOrderContext, setToast } = handle;
  const executionChannel = fence.channel("execution");
  const [executionState, setExecutionState] = useState<{context: typeof orderContext; value: AiExecutionSnapshot}>();
  const aiExecution = executionState?.context === orderContext ? executionState.value : undefined;
  const [executionBusy, setExecutionBusy] = useState<{context: typeof orderContext; token: object}>();
  // Strategies the last stop paused; the resume dialog offers them for restarting one by one.
  const [pausedStrategyIds, setPausedStrategyIds] = useState<string[]>([]);
  const executionOperation = useRef<{context: typeof orderContext; token: object} | undefined>(undefined);
  const aiExecutionBusy = executionBusy?.context === orderContext;
  const refreshAiExecution = async () => {
    const fresh = executionChannel.beginRead();
    const value = await getAiExecution(baseUrl);
    if (fresh()) {
      setExecutionState(previous => fresh() ? {context: orderContext, value} : previous);
    }
    return value;
  };

  const feedActive = connection === "connected" && !replayPath;
  const feedCovers = useRef(false);
  feedCovers.current = feedActive && feedIsLive(feed, "execution") && feed.execution !== undefined;
  useEffect(() => {
    // A stop/resume in flight owns the execution state; its final refresh reads the truth.
    if (!feedActive || !feed.execution || executionOperation.current !== undefined) return;
    setExecutionState({context: orderContext, value: feed.execution});
  }, [feed.execution, feedActive]);

  useEffect(() => {
    if (connection !== "connected" || (replayPath && accountStatus !== "normal")) {
      if (isCurrentOrderContext()) setExecutionState(undefined);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const load = async () => {
      if (feedCovers.current) {
        timer = window.setTimeout(() => void load(), 3_000);
        return;
      }
      try {
        const snapshot = await refreshAiExecution();
        if (!cancelled && (snapshot.authorization?.status === "ACTIVE" || replayPath)) {
          await refreshSimulationOrders(false);
        }
      } catch {
        // Runtime connection state already owns the visible error; this poll is supplemental.
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void load(), replayPath ? 750 : 3_000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [orderContext, baseUrl, sessionId, connection, replayPath ? accountStatus : undefined]);

  const reactivateAiExecution = async () => {
    if (!isCurrentOrderContext() || connection !== "connected" || replayPath || !symbol || !sessionId || executionOperation.current?.context === orderContext) return false;
    const operation = {context: orderContext, token: {}};
    executionOperation.current = operation; setExecutionBusy(operation); executionChannel.noteWrite();
    try {
      const before = await getAiExecution(baseUrl);
      if (!isCurrentOrderContext()) return false;
      if (!before.controls) throw new Error("执行状态尚未确认，请稍后重试。");
      if (before.controls.stopStatus) await resumeAiExecution(baseUrl, sessionId, symbol);
      if (!isCurrentOrderContext()) return false;
      executionChannel.noteWrite();
      const after = await refreshAiExecution();
      if (!isCurrentOrderContext()) return false;
      if (!after.controls || after.controls.stopStatus) throw new Error("恢复尚未确认，请重新读取执行状态。");
      setToast("AI 模拟执行已恢复；需要运行的策略请单独启动。");
      return true;
    } catch {
      if (isCurrentOrderContext()) {
        executionChannel.noteWrite();
        try { await refreshAiExecution(); } catch { /* Never repeat the recovery write. */ }
        if (isCurrentOrderContext()) setToast("恢复结果尚未确认，请查看执行状态；不会自动重试。", "error");
      }
      return false;
    } finally {
      if (executionOperation.current === operation) { executionOperation.current = undefined; if (isCurrentOrderContext()) setExecutionBusy(undefined); }
    }
  };

  const deactivateAiExecution = async (emergency: boolean, scope: "all" | "opens" = "all") => {
    if (!isCurrentOrderContext() || connection !== "connected" || executionOperation.current?.context === orderContext) return;
    const operation = {context: orderContext, token: {}};
    executionOperation.current = operation;
    setExecutionBusy(operation);
    executionChannel.noteWrite();
    try {
      const result = await stopAiExecution(baseUrl, emergency, scope);
      if (!isCurrentOrderContext()) return;
      executionChannel.noteWrite();
      await refreshAiExecution();
      if (!isCurrentOrderContext() || executionOperation.current !== operation) return;
      setPausedStrategyIds(result.pausedStrategyIds);
      const strategyNote = result.pausedStrategyIds.length
        ? `，已暂停 ${result.pausedStrategyIds.length} 个策略` : "";
      setToast(scope === "opens" ? "已只停新开仓，AI 不再开新仓；平仓与保护单照常"
        : emergency ? `AI 模拟执行已急停${strategyNote}` : `AI 模拟执行已停止${strategyNote}`);
    } catch (error) {
      if (isCurrentOrderContext() && executionOperation.current === operation) setToast(error instanceof Error ? error.message : "停止 AI 自动交易失败", "error");
    } finally {
      if (executionOperation.current === operation) {
        executionOperation.current = undefined;
        if (isCurrentOrderContext()) setExecutionBusy(undefined);
      }
    }
  };

  // 持仓面板的保护表单 (2026-10-02): give an existing position its own stop / take (至少一个), or
  // drop one. Same single-operation guard as stop/resume; the Runtime refuses a wrong direction or
  // an off-tick price with a Chinese sentence, which the form shows as-is.
  const protectionLine = (stop: { triggerPrice: number | null; takeProfitPrice: number | null; volume: number }) =>
    [stop.triggerPrice !== null ? `止损 ${stop.triggerPrice}` : null, stop.takeProfitPrice !== null ? `止盈 ${stop.takeProfitPrice}` : null]
      .filter(Boolean).join(" / ") + ` · ${stop.volume} 手`;
  const createPositionStop = async (request: {
    symbol: string; positionDirection: "long" | "short";
    positionBucket: "today" | "historical" | "all"; volume: number;
    triggerPrice?: number | null; takeProfitPrice?: number | null;
  } & StopMotion) => {
    if (!isCurrentOrderContext() || connection !== "connected" || executionOperation.current?.context === orderContext) return false;
    if (!sessionId) { setToast("会话未连接，挂不了保护", "warning"); return false; }
    const operation = {context: orderContext, token: {}};
    executionOperation.current = operation;
    setExecutionBusy(operation);
    executionChannel.noteWrite();
    try {
      const stop = await createAiStop(baseUrl, { sessionId, ...request });
      if (!isCurrentOrderContext()) return false;
      executionChannel.noteWrite();
      // The write landed: a failed status refresh must not read as a refusal (a retry would
      // stack a second protection), so it only says the list will catch up.
      try {
        await refreshAiExecution();
      } catch {
        if (isCurrentOrderContext()) setToast(`保护已挂上：${protectionLine(stop)}；状态刷新失败，稍后自动更新`, "warning");
        return true;
      }
      setToast(`保护已挂上：${protectionLine(stop)}`);
      return true;
    } catch (error) {
      if (isCurrentOrderContext()) {
        executionChannel.noteWrite();
        try { await refreshAiExecution(); } catch { /* Never repeat the recovery write. */ }
        // requestJson nests FastAPI's body under `detail`; the 422 refusals carry their Chinese
        // reason one level deeper (same shape useProposals reads).
        if (isCurrentOrderContext()) setToast(protectionRefusal(error, "挂保护"), "error");
      }
      return false;
    } finally {
      if (executionOperation.current === operation) { executionOperation.current = undefined; if (isCurrentOrderContext()) setExecutionBusy(undefined); }
    }
  };
  /** One write on an existing protection (撤销 / 图上拖线改价): the same busy guard, refresh and refusal wording. */
  const writePositionStop = async (word: string, write: () => Promise<AiStopLoss>) => {
    if (!isCurrentOrderContext() || connection !== "connected" || executionOperation.current?.context === orderContext) return false;
    const operation = {context: orderContext, token: {}};
    executionOperation.current = operation;
    setExecutionBusy(operation);
    executionChannel.noteWrite();
    try {
      const stop = await write();
      if (!isCurrentOrderContext()) return false;
      executionChannel.noteWrite();
      // The write landed: a failed status refresh must not read as a refusal (a retry would
      // stack a second protection), so it only says the list will catch up.
      try {
        await refreshAiExecution();
      } catch {
        if (isCurrentOrderContext()) setToast(`保护已${word}：${protectionLine(stop)}；状态刷新失败，稍后自动更新`, "warning");
        return true;
      }
      setToast(`保护已${word}：${protectionLine(stop)}`);
      return true;
    } catch (error) {
      if (isCurrentOrderContext()) {
        executionChannel.noteWrite();
        try { await refreshAiExecution(); } catch { /* Never repeat the recovery write. */ }
        if (isCurrentOrderContext()) setToast(protectionRefusal(error, `${word}保护`), "error");
      }
      return false;
    } finally {
      if (executionOperation.current === operation) { executionOperation.current = undefined; if (isCurrentOrderContext()) setExecutionBusy(undefined); }
    }
  };
  const cancelPositionStop = (stopId: string) => writePositionStop("撤销", () => cancelAiStop(baseUrl, stopId));
  const repricePositionStop = (stopId: string, request: { level: "stop" | "take"; previous: number; price: number }) =>
    writePositionStop("改价", () => repriceAiStop(baseUrl, stopId, request));

  return { aiExecution, aiExecutionBusy, pausedStrategyIds, reactivateAiExecution, deactivateAiExecution, createPositionStop, cancelPositionStop, repricePositionStop };
}
