import type { GuestAccountController } from "../../hooks/useGuestAccount";
import type { WorkspaceAccess } from "../../hooks/useAccountAccess";
import type { ReactNode } from "react";
import type { RuntimeMode } from "@ai-trader/ui-events";
import type {
  AccountOverviewSnapshot, AgentAccountStatus, AiExecutionSnapshot, FuturesWorkspacePromptContext, InstrumentCatalog,
  MarketWorkbenchSnapshot, MessageExtras, SimulationOrder, StopMotion
} from "../../api";
import type { DesktopUpdaterController } from "../../hooks/useDesktopUpdater";
import type { ManualOrderOutcome } from "../../hooks/useSimulationOrders";
import type { WorkbenchStatus } from "../../hooks/useWorkbenchStatus";
import type { ConversationState } from "../../state/conversation";
import type { ProposalActions } from "../Conversation";
import type { FontScale, InterfaceDensity } from "../../appearance";
import type { ManualSimulationRequest, SimulationLimitSettings } from "./support";

export interface FuturesDecisionWorkspaceProps {
  guest?: GuestAccountController;
  access?: WorkspaceAccess;
  workspaceScope?: string;
  preferencesScope?: string;
  preferencesBaseUrl?: string;
  replayControls?: ReactNode;
  strategyPanel?: ReactNode;
  runtimeBaseUrl?: string;
  /** Root (owner-level) Runtime URL for account-wide settings such as the fee schedule. */
  rootBaseUrl?: string;
  replayActive?: boolean;
  replayEnded?: boolean;
  onOpenReplay?: () => void;
  state: ConversationState;
  chatSessionId?: string;
  chatBusy?: boolean;
  onChangeChat?: (id?: string) => Promise<void>;
  onListChats?: () => Promise<{items: {sessionId: string; createdAt: string}[]}>;
  mode: RuntimeMode;
  connection: "connecting" | "connected" | "disconnected";
  status: WorkbenchStatus;
  instruments?: InstrumentCatalog;
  instrumentsLoading: boolean;
  instrumentsError?: string;
  account?: AccountOverviewSnapshot;
  market?: MarketWorkbenchSnapshot;
  marketLoading: boolean;
  marketError?: string;
  duration: MarketWorkbenchSnapshot["durationSeconds"];
  selectedSymbol?: string;
  simulationOrders: SimulationOrder[];
  aiExecution?: AiExecutionSnapshot;
  aiExecutionBusy: boolean;
  /** Strategies paused by the last stop, for the resume dialog. */
  pausedStrategyIds?: string[];
  agentAccount?: AgentAccountStatus;
  simulationGatewayAvailable: boolean;
  simulationEnabled: boolean;
  /** The first unmet condition behind `simulationEnabled === false`, in the user's words. */
  simulationBlockedReason?: string;
  tradeBusy: boolean;
  fontScale: FontScale;
  interfaceDensity: InterfaceDensity;
  onFontScaleChange: (scale: FontScale) => void;
  onInterfaceDensityChange: (density: InterfaceDensity) => void;
  simulationLimits: SimulationLimitSettings;
  simulationLimitsStatus?: string;
  onSimulationLimitsChange: (limits: SimulationLimitSettings) => void;
  proposalActions: ProposalActions;
  updater: DesktopUpdaterController;
  onSelectSymbol: (symbol: string) => void;
  onDurationChange: (duration: MarketWorkbenchSnapshot["durationSeconds"]) => void;
  onRefreshMarket: () => void;
  onRefreshAccount: () => void;
  onRetryInstruments: () => void;
  /** What the submission did: undefined when nothing was confirmed (see ManualOrderOutcome). */
  onManualSubmit: (request: ManualSimulationRequest) => Promise<ManualOrderOutcome | undefined>;
  /** 目标持仓：Runtime 算差额并把第一段的确认卡发进会话；`confirm`（快捷区反手）时当场确认这张卡。 */
  onTargetPosition: (request: { symbol: string; targetNet: number; priceIntent?: "passive" | "aggressive" | "last"; confirm?: boolean }) => Promise<boolean>;
  onOrderAction: (order: SimulationOrder, action: "continue" | "abandon" | "query" | "cancel") => void | Promise<void>;
  onResumeAiExecution?: () => Promise<boolean>;
  onStopAiExecution: (emergency: boolean, scope?: "all" | "opens") => void;
  /** 持仓面板的保护表单 (2026-10-02): give an existing position its stop / take, or drop one. */
  onCreatePositionStop?: (request: {
    symbol: string; positionDirection: "long" | "short";
    positionBucket: "today" | "historical" | "all"; volume: number;
    triggerPrice?: number | null; takeProfitPrice?: number | null;
  } & StopMotion) => Promise<boolean>;
  onCancelPositionStop?: (stopId: string) => Promise<boolean>;
  /** 图上拖线改价 (2026-10-24): the chart's confirmed drag of the user's own stop / take line. */
  onRepricePositionStop?: (stopId: string, request: { level: "stop" | "take"; previous: number; price: number }) => Promise<boolean>;
  /** `extras`: a push-to-talk message (`origin: "ptt"`) or 我的流程's 运行 (`routineId`), see MessageExtras. */
  onPrompt: (prompt: string, context?: FuturesWorkspacePromptContext, extras?: MessageExtras) => Promise<void>;
  onOpenCloudLogin: () => void;
  onCloudLogout?: () => void;
}
