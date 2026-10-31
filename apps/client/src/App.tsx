import { GuestEntry, ReturnToGuestContext } from "./components/GuestEntry";
import { StrategyWorkbench } from "./components/StrategyWorkbench";
import { useContext, useEffect, useMemo, useRef, useState } from "react";
import type { RuntimeMode } from "@ai-trader/ui-events";
import { runtimeHeaders, signOutOwner, withForeignHalf, type FuturesWorkspacePromptContext, type MessageExtras, type SimulationOrder } from "./api";
import { DesktopUpdateDialog } from "./components/DesktopUpdateDialog";
import { HistoricalReplay, type ReplayWorkspaceProps } from "./components/HistoricalReplay";
import { FuturesDecisionWorkspace } from "./components/FuturesDecisionWorkspace";
import { OwnerLogin } from "./components/OwnerLogin";
import { useRuntimeSession } from "./hooks/useRuntimeSession";
import { useDesktopUpdater } from "./hooks/useDesktopUpdater";
import { useWorkbenchStatus } from "./hooks/useWorkbenchStatus";
import { useIdentityFence } from "./hooks/useIdentityFence";
import { useAccountOverview } from "./hooks/useAccountOverview";
import { useForeignHalf } from "./hooks/useForeignHalf";
import { useAiExecution } from "./hooks/useAiExecution";
import { useAgentAccount } from "./hooks/useAgentAccount";
import { useMarketWorkbench } from "./hooks/useMarketWorkbench";
import { useProposalBusy, useProposals } from "./hooks/useProposals";
import { useSimulationLimits } from "./hooks/useSimulationLimits";
import { useOrdersFollowAccount, useSimulationOrders, type ManualTicketContext } from "./hooks/useSimulationOrders";
import type { ToastKind, WorkspaceHandle } from "./hooks/workspaceHandle";
import { useRuntimeFeed } from "./runtimeFeed";
import { setRumTarget } from "./rum";
import { watchWebBuild } from "./clientBuild";
import { cloudRuntimeAuthMode, defaultRuntimeMode, isTauriRuntime } from "./runtimeConnection";
import { chartQuoteBlockedReason } from "./marketSession";
import { useAppearance } from "./hooks/useAppearance";
import { GuestAppearanceContext } from "./guestAppearance";

export function App() {
  const [mode] = useState<RuntimeMode>(defaultRuntimeMode);
  // Web builds compare themselves against the deployed version.json; watchWebBuild no-ops on desktop.
  useEffect(() => watchWebBuild(), []);
  return <GuestEntry mode={mode}><HistoricalReplay environment={mode} enabled={mode === "local" || cloudRuntimeAuthMode() === "owner-password"}>{(replayProps) => <TradingApp key={replayProps.replayPath || "live"} {...replayProps} mode={mode} />}</HistoricalReplay></GuestEntry>;
}


function TradingApp(replayProps: ReplayWorkspaceProps & { mode: RuntimeMode }) {
  const { mode } = replayProps;
  const returnToGuest = useContext(ReturnToGuestContext);
  const [reconnectKey, setReconnectKey] = useState(0);
  const { state, changeChat, listChats, chatBusy, workspaceScope, rootWorkspaceScope, sessionId, connection, authRequired, runtimeActivityAt, baseUrl, rootBaseUrl, submitMessage } = useRuntimeSession(mode, reconnectKey, replayProps.replayPath);
  useEffect(() => {
    if (connection === "connected") replayProps.onRuntimeReady(rootBaseUrl, rootWorkspaceScope);
  }, [connection, rootBaseUrl, rootWorkspaceScope, replayProps.onRuntimeReady]);
  // 真实用户监测: the minute beacons go to the Runtime this workspace is signed in to.
  useEffect(() => {
    if (connection !== "connected" || !rootBaseUrl) return;
    setRumTarget({ baseUrl: rootBaseUrl, headers: () => runtimeHeaders(rootBaseUrl), identity: "member" });
    return () => setRumTarget(undefined);
  }, [connection, rootBaseUrl]);
  const workbenchStatus = useWorkbenchStatus({ connection, runtime: state.runtime, runtimeUpdatedAt: state.runtimeUpdatedAt, runtimeActivityAt });
  // Retire old requests and operations before rendering another authenticated workspace.
  const fence = useIdentityFence([mode, baseUrl, sessionId, connection, replayProps.replayPath, state.runtime?.account?.id, state.runtime?.accountGeneration]);
  const orderContext = fence.context;
  const [toastState, setToastState] = useState<{context: typeof orderContext; text: string; kind: ToastKind}>();
  const toast = toastState?.context === orderContext ? toastState : undefined;
  const setToast = (text?: string, kind: ToastKind = "info") => {
    if (fence.isCurrent()) setToastState(text ? {context: orderContext, text, kind} : undefined);
  };
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(undefined), 3200);
    return () => window.clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (state.lastWarning) setToast(state.lastWarning, "warning");
  }, [state.lastWarning]);
  // A login from the guest workspace mounts this workspace fresh: say its result once connected.
  const guestAppearance = useContext(GuestAppearanceContext);
  const loginNotice = useRef(guestAppearance?.notice);
  useEffect(() => {
    if (connection !== "connected" || !loginNotice.current) return;
    setToast(loginNotice.current, "success");
    loginNotice.current = undefined;
  }, [connection]);
  const handle: WorkspaceHandle = { baseUrl, connection, sessionId, replayPath: replayProps.replayPath, fence, orderContext, isCurrentOrderContext: fence.isCurrent, setToast };

  const appearance = useAppearance(setToast);
  const [cloudLoginOpen, setCloudLoginOpen] = useState(false);
  useEffect(() => {
    if (mode === "cloud" && authRequired) setCloudLoginOpen(true);
  }, [authRequired, mode]);
  const desktop = isTauriRuntime();
  const desktopUpdater = useDesktopUpdater(desktop);
  const { agentAccount, requireModel } = useAgentAccount({ handle });

  // Runtime feed: pushed snapshots replace the pollers inside these hooks while the
  // session's stream delivers them; each poller keeps its old interval as a fallback.
  const feed = useRuntimeFeed(sessionId);
  const workbench = useMarketWorkbench({ handle, marketStatus: workbenchStatus.marketStatus, feed,
    replaySymbol: replayProps.replaySymbol, chartTimeframe: replayProps.chartTimeframe, onChartTimeframeChange: replayProps.onChartTimeframeChange });
  const busy = useProposalBusy(handle);
  const ticket = useRef<ManualTicketContext>({ market: undefined, enabled: false, onAccountChanged: () => undefined });
  // The orders list is the session's account's (#930): unknown until runtime.status names it.
  const orders = useSimulationOrders({ handle, busy, ticket, accountId: state.runtime ? (state.runtime.account?.id ?? null) : undefined,
    cardAccounts: state.turns.flatMap(turn => turn.cards).flatMap(card =>
      (card.type === "trade" || card.type === "close-batch" || card.type === "spread")
        && card.payload.status === "candidate_recorded" && card.payload.accountId ? [card.payload.accountId] : []) });
  const execution = useAiExecution({ handle, accountStatus: workbenchStatus.accountStatus, feed, symbol: workbench.symbol, refreshSimulationOrders: orders.refreshSimulationOrders });
  const { aiExecution } = execution;
  const aiExecutionActive = aiExecution?.authorization?.status === "ACTIVE";
  const { accountOverview, refreshAccount } = useAccountOverview({ handle, accountId: state.runtime ? (state.runtime.account?.id ?? null) : undefined, accountStatus: workbenchStatus.accountStatus, feed, aiExecutionActive });
  useOrdersFollowAccount(orders.simulationOrders, accountOverview?.activity?.orders, orders.refreshSimulationOrders);
  // 外盘仓合并显示 (2026-10-21): the USD sub-account's positions and orders join the workspace's lists.
  const { foreignHalf, refreshForeignHalf } = useForeignHalf({ handle, feed, foreignAccountId: state.runtime?.account?.foreignAccountId });
  const workspaceAccount = useMemo(() => withForeignHalf(accountOverview, foreignHalf), [accountOverview, foreignHalf]);
  const workspaceOrders = useMemo(() => foreignHalf?.orders.length ? [...orders.simulationOrders, ...foreignHalf.orders] : orders.simulationOrders,
    [orders.simulationOrders, foreignHalf]);
  const orderAction = (order: SimulationOrder, action: "continue" | "abandon" | "query" | "cancel") =>
    orders.simulationOrderAction(order, action).finally(() => { if (order.accountId && order.accountId === state.runtime?.account?.foreignAccountId) refreshForeignHalf(); });
  const simulationLimitState = useSimulationLimits({baseUrl, rootBaseUrl, scope: workspaceScope, fence, connected: connection === "connected", controls: aiExecution?.controls});

  const simulationGatewayAvailable = Boolean(
    state.runtime?.simulationTrading
    && workbenchStatus.runtimeUsable
    && state.runtime.tradingGatewayStatus !== "unavailable"
  );
  // A conversation card carries its own contract, so its confirmation depends on the account and the
  // gateway only — never on the contract the chart happens to show (#932 B1: a 螺纹 card could not be
  // confirmed while the chart sat on a closed 中证 contract). The card's own quote freshness, price
  // limits and position are re-checked by the Runtime's confirmation, which this does not relax.
  const approvalBlockedReason = ((): string | undefined => {
    if (!simulationGatewayAvailable) return "模拟网关未连接";
    if (workbenchStatus.accountStatus !== "normal") return "账户数据未就绪";
    if (accountOverview?.account.dataStatus !== "normal") return "账户快照不是实时数据";
    if (accountOverview.risk.status === "blocked") return "风险预算已触发，停止开仓";
    if (accountOverview.risk.status === "unavailable") return "风险预算不可用";
    return undefined;
  })();
  const proposals = useProposals({ handle, busy, recordConfirmedOrders: orders.recordConfirmedOrders,
    refreshSimulationOrders: orders.refreshOrdersFor, aiExecution, confirmGate: approvalBlockedReason,
    onCurrentAccount: orders.onCurrentAccount });
  // The ticket orders the chart's contract: the card's conditions plus that contract's live quote.
  // The first unmet condition is what the ticket shows under its button; every one is re-checked by the Runtime.
  const simulationBlockedReason = ((): string | undefined => {
    const market = workbench.market;
    if (!simulationGatewayAvailable) return approvalBlockedReason;
    // 新浪备用 (2026-10-07): TqKq is down; the chart shows Sina's quotes for watching only.
    if (workbenchStatus.marketStatus === "backup" || market?.dataStatus === "backup") return "备用行情（新浪）只供看盘，暂不可下单";
    if (workbenchStatus.marketStatus !== "normal") return "行情数据未就绪";
    if (workbenchStatus.accountStatus !== "normal") return "账户数据未就绪";
    if (!market) return "当前合约行情未加载";
    return chartQuoteBlockedReason(market, Boolean(replayProps.replayPath)) ?? approvalBlockedReason;
  })();
  const simulationTicketEnabled = simulationBlockedReason === undefined;
  ticket.current = { market: workbench.market, enabled: simulationTicketEnabled, onAccountChanged: refreshAccount };

  const run = async (text: string, workspaceContext?: FuturesWorkspacePromptContext, extras?: MessageExtras) => {
    if (agentAccount && !agentAccount.authenticated) {
      requireModel("当前模型未就绪");
      throw new Error("模型尚未连接");
    }
    try {
      await (extras ? submitMessage(text, workspaceContext, extras) : submitMessage(text, workspaceContext));
    } catch (error) {
      setToast(error instanceof Error ? error.message : "请求失败", "error");
      throw error;
    }
  };

  const ownerLogin = (
    <OwnerLogin
      baseUrl={baseUrl}
      open={cloudLoginOpen || (mode === "cloud" && authRequired)}
      required={mode === "cloud" && authRequired}
      onClose={() => setCloudLoginOpen(false)}
      onAuthenticated={(message) => {
        setCloudLoginOpen(false);
        setReconnectKey((value) => value + 1);
        setToast(message);
      }}
    />
  );

  if (mode === "cloud" && authRequired) {
    return <main className="owner-login-gate">{ownerLogin}</main>;
  }

  return (
    <>
      <FuturesDecisionWorkspace
        workspaceScope={workspaceScope}
        preferencesScope={rootWorkspaceScope}
        preferencesBaseUrl={mode === "cloud" ? rootBaseUrl : undefined}
        runtimeBaseUrl={baseUrl}
        rootBaseUrl={rootBaseUrl}
        strategyPanel={<StrategyWorkbench market={workbench.market || undefined} workspaceScope={rootWorkspaceScope} chartTimeframe={workbench.duration} account={accountOverview} rootUrl={rootBaseUrl} baseUrl={baseUrl} symbol={workbench.symbol || replayProps.replaySymbol || "SHFE.au2612"} replayActive={Boolean(replayProps.replayPath)}/>}
        replayControls={replayProps.replayControls}
        replayActive={Boolean(replayProps.replayPath)}
        replayEnded={replayProps.replayEnded}
        onOpenReplay={replayProps.onOpenReplay}
        state={state}
        chatSessionId={sessionId}
        chatBusy={chatBusy}
        onChangeChat={changeChat}
        onListChats={listChats}
        mode={mode}
        connection={connection}
        status={workbenchStatus}
        instruments={workbench.instrumentCatalog}
        instrumentsLoading={workbench.instrumentCatalogLoading}
        instrumentsError={workbench.instrumentCatalogError}
        account={workspaceAccount}
        market={workbench.market}
        marketLoading={workbench.marketLoading}
        marketError={workbench.marketError}
        duration={workbench.duration}
        selectedSymbol={workbench.symbol}
        simulationOrders={workspaceOrders}
        aiExecution={aiExecution}
        aiExecutionBusy={execution.aiExecutionBusy}
        pausedStrategyIds={execution.pausedStrategyIds}
        agentAccount={agentAccount}
        simulationGatewayAvailable={simulationGatewayAvailable}
        simulationEnabled={simulationTicketEnabled}
        simulationBlockedReason={simulationBlockedReason}
        tradeBusy={busy.id === "manual-ticket" || Boolean(busy.id?.startsWith("AIT")) || Boolean(proposals.confirming)}
        fontScale={appearance.fontScale}
        interfaceDensity={appearance.interfaceDensity}
        onFontScaleChange={appearance.setFontScale}
        onInterfaceDensityChange={appearance.setInterfaceDensity}
        simulationLimits={simulationLimitState.limits}
        onSimulationLimitsChange={simulationLimitState.change}
        simulationLimitsStatus={simulationLimitState.status}
        proposalActions={{
          ...proposals.proposalActions,
          ordersFor: orders.ordersFor,
          onCurrentAccount: orders.onCurrentAccount,
          orderAction
        }}
        updater={desktopUpdater}
        onSelectSymbol={workbench.selectSymbol}
        onDurationChange={(duration) => void workbench.changeChartPeriod(duration)}
        onRefreshMarket={workbench.refreshMarket}
        onRefreshAccount={() => { refreshAccount(); refreshForeignHalf(); void orders.refreshSimulationOrders(false).catch(() => undefined); }}
        onRetryInstruments={workbench.retryInstruments}
        onManualSubmit={orders.submitManualSimulationOrder}
        onTargetPosition={orders.submitTargetPosition}
        onOrderAction={orderAction}
        onResumeAiExecution={execution.reactivateAiExecution}
        onStopAiExecution={(emergency, scope) => void execution.deactivateAiExecution(emergency, scope)}
        onCreatePositionStop={execution.createPositionStop}
        onCancelPositionStop={execution.cancelPositionStop}
        onRepricePositionStop={execution.repricePositionStop}
        onPrompt={run}
        onOpenCloudLogin={() => setCloudLoginOpen(true)}
        onCloudLogout={mode === "cloud" ? async () => {
          await signOutOwner(baseUrl);
          if (returnToGuest) { returnToGuest(); return; }
          setCloudLoginOpen(true);
          setReconnectKey((value) => value + 1);
        } : undefined}
      />
      {toast ? <div className="toast" data-kind={toast.kind} role={toast.kind === "error" ? "alert" : "status"}><i aria-hidden="true">{{success: "✓", info: "i", warning: "!", error: "!"}[toast.kind]}</i><span>{toast.text}</span></div> : null}
      {desktop ? <DesktopUpdateDialog updater={desktopUpdater} /> : null}
      {ownerLogin}
    </>
  );
}
