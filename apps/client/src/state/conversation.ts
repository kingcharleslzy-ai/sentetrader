import type {
  AccountSummary,
  AgentTask,
  AnswerCard,
  DataCard,
  BacktestComparison,
  CloseBatchProposal,
  ExecutionCard,
  OrderQuestionCard,
  ReminderCard,
  ResearchJudgmentCard,
  RoutineCard,
  RoutineRunCard,
  SpreadProposal,
  AgentDiffArtifact,
  AgentToolActivity,
  MarketSnapshot,
  RiskRuleProposal,
  RiskWarning,
  RuntimeStatus,
  RuntimeError,
  StrategyBacktestCard,
  StrategyExplanation,
  TradeProposal,
  TurnRole,
  UiEvent
} from "@ai-trader/ui-events";
import { admitRuntimeStatus } from "../accountRevision";

export type ConversationCard =
  | { id: string; type: "account"; payload: AccountSummary }
  | { id: string; type: "market"; payload: MarketSnapshot }
  | { id: string; type: "strategy"; payload: StrategyExplanation }
  | { id: string; type: "task"; payload: AgentTask }
  | { id: string; type: "backtest"; payload: BacktestComparison }
  | { id: string; type: "strategy-backtest"; payload: StrategyBacktestCard }
  | { id: string; type: "trade"; payload: TradeProposal }
  | { id: string; type: "close-batch"; payload: CloseBatchProposal }
  | { id: string; type: "spread"; payload: SpreadProposal }
  | { id: string; type: "risk-rule"; payload: RiskRuleProposal }
  | { id: string; type: "risk"; payload: RiskWarning }
  | { id: string; type: "agent-tool"; payload: AgentToolActivity }
  | { id: string; type: "agent-diff"; payload: AgentDiffArtifact }
  | { id: string; type: "task-error"; payload: RuntimeError }
  | { id: string; type: "order-question"; payload: OrderQuestionCard }
  | { id: string; type: "research-judgment"; payload: ResearchJudgmentCard }
  | { id: string; type: "reminder"; payload: ReminderCard }
  | { id: string; type: "execution"; payload: ExecutionCard }
  | { id: string; type: "routine"; payload: RoutineCard }
  | { id: string; type: "routine-run"; payload: RoutineRunCard }
  | { id: string; type: "answer"; payload: AnswerCard }
  | { id: string; type: "data"; payload: DataCard };

export interface ConversationTurn {
  id: string;
  role: TurnRole;
  text: string;
  agentId?: string;
  timestamp: string;
  completed?: boolean;
  /** "voice": spoken through the real-time call; the turn shows a speaker mark. */
  origin?: "voice";
  /** Voice turns: what the call read out for this answer. */
  spoken?: string | null;
  /** 对象级 AI: the object a user turn was asked about (shown as "@ label" above it). */
  focus?: { kind: string; label: string };
  /** A streamed answer that did not finish ("已停止。"/"未完成。" stubs). */
  ended?: "cancelled" | "failed";
  /** 续句合并：这半句 user 消息被并进下一句；该轮渲染时隐藏（数据保留）。 */
  superseded?: boolean;
  hidden?: boolean;
  cards: ConversationCard[];
}

export interface ConversationState {
  turns: ConversationTurn[];
  runtime?: RuntimeStatus;
  runtimeUpdatedAt?: string;
  latestAccount?: AccountSummary;
  connectionError?: string;
  lastWarning?: string;
  /** message.superseded 点名的 user turnId：即使事件先于该 turn 到达也能补标。 */
  supersededTurnIds: string[];
}

export type ConversationAction =
  | { type: "event"; event: UiEvent }
  | { type: "local-backtest"; card: BacktestComparison }
  | { type: "reset" }
  | { type: "connection-restored" }
  | { type: "local-error"; message: string }
  | { type: "local-warning"; message: string };

export const initialConversationState: ConversationState = { turns: [], supersededTurnIds: [] };

/** The Runtime's confirm window for a card (approval_expires_at = 600 s), plus a minute for its expiry sweep. */
export const PROPOSAL_SETTLE_AFTER_MS = 11 * 60_000;

/**
 * 历史卡的结局 (#932 P2): order cards still waiting in the replayed history although their confirm window ran out
 * long ago — their proposal.result was trimmed from the session before it was kept with the cards (77 cards read
 * 等待确认). Their ids, so the window reads each one's real state once (GET /api/proposals/{id}).
 */
export function staleOpenProposals(state: ConversationState, now: number): string[] {
  const ids: string[] = [];
  for (const turn of state.turns) {
    for (const card of turn.cards) {
      if (card.type !== "trade" && card.type !== "close-batch" && card.type !== "spread") continue;
      const payload = card.payload as { status?: string; updatedAt?: string };
      const at = Date.parse(payload.updatedAt ?? "");
      if (payload.status === "approval_required" && Number.isFinite(at) && now - at > PROPOSAL_SETTLE_AFTER_MS) ids.push(card.id);
    }
  }
  return ids;
}

/** A proposal record read back (GET /api/proposals/{id}) as the proposal.result it would have sent; none while it
 *  still waits. */
export function settledProposalEvent(sessionId: string, record: { id?: string; status?: string; payload?: Record<string, unknown> }): UiEvent | undefined {
  if (!record.id || (record.status !== "candidate_recorded" && record.status !== "rejected")) return undefined;
  const payload = record.payload ?? {};
  const reason = payload.expired ? "expired" : payload.superseded ? "superseded" : null;
  return { schemaVersion: 1, id: `settled-${record.id}`, sessionId, kind: "result", name: "proposal.result",
    timestamp: new Date().toISOString(),
    payload: { proposalId: record.id, status: record.status, auditId: String(payload.auditId ?? "settled"), message: "", reason } } as UiEvent;
}

function attachCard(
  state: ConversationState,
  turnId: string,
  card: ConversationCard
): ConversationState {
  return {
    ...state,
    turns: state.turns.map((turn) =>
      turn.id === turnId
        ? { ...turn, cards: [...turn.cards.filter((item) => item.id !== card.id), card] }
        : turn
    )
  };
}

/** A card whose turn has not streamed in (yet): like a task's fallback turn, open an empty assistant
 *  turn for it instead of dropping the card (a 目标持仓 second-leg card under its own turnId was lost
 *  this way). `completed` stays unset, so the turn never blocks sending; a later message under the
 *  same turnId merges into it through upsertMessage — live and when history replays the events. */
function attachCardOrOpenTurn(
  state: ConversationState,
  turnId: string,
  timestamp: string,
  card: ConversationCard
): ConversationState {
  if (state.turns.some((turn) => turn.id === turnId)) return attachCard(state, turnId, card);
  const opened: ConversationTurn = { id: turnId, role: "assistant", text: "", timestamp, cards: [] };
  return attachCard({ ...state, turns: hideSupersededRounds([...state.turns, opened]) }, turnId, card);
}

/** Same id again = the card changed in place (a 判断卡 pending → ready, an 执行回报 submitted → filled). */
function upsertCard(state: ConversationState, turnId: string, timestamp: string, card: ConversationCard): ConversationState {
  const exists = state.turns.some((turn) => turn.cards.some((item) => item.id === card.id));
  return exists ? updateCard(state, card.id, () => card) : attachCardOrOpenTurn(state, turnId, timestamp, card);
}

/** A new user turn supersedes everything before it: an assistant turn still marked open (a lost
 *  message.completed, a Runtime from before 1.6.6) cannot still be running once the user spoke
 *  again, so close it here — live and when history replays the same events. */
function closeSupersededTurns(turns: ConversationTurn[]): ConversationTurn[] {
  return turns.some((turn) => turn.role === "assistant" && turn.completed === false)
    ? turns.map((turn) => (turn.role === "assistant" && turn.completed === false ? { ...turn, completed: true } : turn))
    : turns;
}

/** 续句合并的去噪规则：被标记 superseded 的半句 user 消息，连同其后直到下一句 user 消息之前的
 *  assistant 轮（流式占位、已停止/未完成收尾、任务卡）一起隐藏。取消在服务端先于合并句入队，
 *  所以位置规则对事件顺序是稳的；每次 turns 变化后重算。 */
function hideSupersededRounds(turns: ConversationTurn[]): ConversationTurn[] {
  if (!turns.some((turn) => turn.superseded)) return turns;
  let hiding = false;
  return turns.map((turn) => {
    if (turn.role === "user") hiding = Boolean(turn.superseded);
    return hiding ? { ...turn, hidden: true } : turn;
  });
}

function upsertMessage(state: ConversationState, event: Extract<UiEvent, { name: "message.delta" | "message.completed" }>): ConversationState {
  const existing = state.turns.some((turn) => turn.id === event.payload.turnId);
  if (!existing) {
    const preceding = event.payload.role === "user" ? closeSupersededTurns(state.turns) : state.turns;
    return {
      ...state,
      turns: hideSupersededRounds([...preceding, {
        id: event.payload.turnId,
        role: event.payload.role,
        text: event.payload.text,
        agentId: event.payload.agentId,
        timestamp: event.timestamp,
        completed: event.name === "message.completed",
        origin: event.payload.origin,
        spoken: event.payload.spoken,
        ended: event.payload.ended,
        focus: event.payload.focus,
        // A superseded event may name a turn that has not streamed in yet.
        superseded: state.supersededTurnIds.includes(event.payload.turnId) || undefined,
        cards: []
      }])
    };
  }
  return {
    ...state,
    turns: hideSupersededRounds(state.turns.map((turn) => turn.id === event.payload.turnId
      ? {
        ...turn,
        role: event.payload.role,
        text: event.payload.text,
        agentId: event.payload.agentId,
        completed: event.name === "message.completed",
        origin: event.payload.origin ?? turn.origin,
        spoken: event.payload.spoken ?? turn.spoken,
        ended: event.payload.ended ?? turn.ended,
        focus: event.payload.focus ?? turn.focus
      }
      : turn))
  };
}

function updateCard(
  state: ConversationState,
  cardId: string,
  updater: (card: ConversationCard) => ConversationCard
): ConversationState {
  return {
    ...state,
    turns: state.turns.map((turn) => ({
      ...turn,
      cards: turn.cards.map((card) => (card.id === cardId ? updater(card) : card))
    }))
  };
}

export function conversationReducer(
  state: ConversationState,
  action: ConversationAction
): ConversationState {
  if (action.type === "reset") return initialConversationState;
  if (action.type === "local-backtest") {
    const { card } = action;
    const withTurn = state.turns.some((turn) => turn.id === card.turnId)
      ? state
      : {
          ...state,
          turns: [...state.turns, {
            id: card.turnId,
            role: "assistant" as const,
            text: "官方 TqSdk 历史回测已完成；结果只用于研究，不会部署或下单。",
            agentId: "native",
            timestamp: card.generatedAt,
            cards: []
          }]
        };
    return attachCard(withTurn, card.turnId, {
      id: `backtest-${card.taskId}`,
      type: "backtest",
      payload: card
    });
  }
  if (action.type === "connection-restored") return { ...state, connectionError: undefined };
  if (action.type === "local-error") return { ...state, connectionError: action.message };
  if (action.type === "local-warning") return { ...state, lastWarning: action.message };

  const event = action.event;
  switch (event.name) {
    case "message.delta":
    case "message.completed":
      return upsertMessage(state, event);
    case "message.superseded":
      return {
        ...state,
        supersededTurnIds: state.supersededTurnIds.includes(event.payload.turnId)
          ? state.supersededTurnIds
          : [...state.supersededTurnIds, event.payload.turnId],
        turns: hideSupersededRounds(state.turns.map((turn) =>
          turn.id === event.payload.turnId && turn.role === "user"
            ? { ...turn, superseded: true }
            : turn))
      };
    case "runtime.status":
      // An older status arriving late (another Runtime slot, before a newer switch) is dropped whole (#932 follow-up).
      if (!admitRuntimeStatus(event.sessionId, event.payload)) return state;
      return { ...state, runtime: event.payload, runtimeUpdatedAt: event.timestamp, connectionError: undefined };
    // 回答卡片化 (2026-10-14): a cardId names the turn's one account / quote card — a second read in the
    // turn replaces it in place. Older events without one stay one card per event.
    case "card.account": {
      const next = { ...state, latestAccount: event.payload };
      const card: ConversationCard = { id: event.payload.cardId ?? `account-${event.id}`, type: "account", payload: event.payload };
      return event.payload.cardId
        ? upsertCard(next, event.payload.turnId, event.timestamp, card)
        : attachCardOrOpenTurn(next, event.payload.turnId, event.timestamp, card);
    }
    case "card.market": {
      const card: ConversationCard = { id: event.payload.cardId ?? `market-${event.id}`, type: "market", payload: event.payload };
      return event.payload.cardId
        ? upsertCard(state, event.payload.turnId, event.timestamp, card)
        : attachCardOrOpenTurn(state, event.payload.turnId, event.timestamp, card);
    }
    case "card.answer":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.cardId, type: "answer", payload: event.payload });
    case "card.data":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.cardId, type: "data", payload: event.payload });
    case "card.strategy":
      return attachCardOrOpenTurn(state, event.payload.turnId, event.timestamp, {
        id: event.payload.strategyId,
        type: "strategy",
        payload: event.payload
      });
    case "task.started":
    case "task.progress":
    case "task.completed": {
      const previous = state.turns.flatMap(turn => turn.cards).find(card => card.type === "task" && card.id === event.payload.taskId);
      // A task's end is final: a running snapshot delivered after it (a coalesced transient the SSE
      // batch sends after the stored task.completed) must not bring back the 等待模型响应 row.
      if (previous?.type === "task" && previous.payload.status !== "running" && event.payload.status === "running") return state;
      const payload = {...event.payload, startedAt: previous?.type === "task" ? previous.payload.startedAt || event.payload.startedAt : event.payload.startedAt};
      // A task that ended without a final answer (cancelled, failed, a Runtime before 1.6.6) leaves its
      // streamed turn open; an open assistant turn blocks 新对话 / 历史对话 / sending, so close it here.
      const finished = payload.status !== "running";
      const turns = state.turns.map(turn => ({...turn, cards: turn.cards.filter(card => card.id !== payload.taskId),
        ...(finished && turn.id === payload.turnId && turn.role === "assistant" && turn.completed === false ? { completed: true } : {})}));
      if (!turns.some(turn => turn.id === payload.turnId)) turns.push({id: payload.turnId, role: "assistant", text: "", timestamp: event.timestamp, cards: []});
      return attachCard({...state, turns}, payload.turnId, {id: payload.taskId, type: "task", payload});
    }
    case "card.backtest":
      return attachCardOrOpenTurn(state, event.payload.turnId, event.timestamp, {
        id: `backtest-${event.payload.taskId}`,
        type: "backtest",
        payload: event.payload
      });
    case "card.strategy_backtest":
      return attachCardOrOpenTurn(state, event.payload.turnId, event.timestamp, {
        id: `strategy-backtest-${event.payload.backtestId}`,
        type: "strategy-backtest",
        payload: event.payload
      });
    case "proposal.trade":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.proposalId, type: "trade", payload: event.payload });
    case "proposal.close_batch":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.proposalId, type: "close-batch", payload: event.payload });
    case "proposal.spread":
      // The confirmed card is republished with each leg's real order; keyed by proposalId it
      // replaces the waiting card in place.
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.proposalId, type: "spread", payload: event.payload });
    case "card.order_question":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.cardId, type: "order-question", payload: event.payload });
    case "card.research_judgment":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.cardId, type: "research-judgment", payload: event.payload });
    case "card.reminder":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.cardId, type: "reminder", payload: event.payload });
    case "card.execution":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.cardId, type: "execution", payload: event.payload });
    // 我的流程 (2026-10-06): the 流程卡 (draft → saved under the same cardId) and the run's 进度卡.
    case "card.routine":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.cardId, type: "routine", payload: event.payload });
    case "card.routine_run":
      return upsertCard(state, event.payload.turnId, event.timestamp, { id: event.payload.cardId, type: "routine-run", payload: event.payload });
    case "proposal.risk_rule":
      return attachCardOrOpenTurn(state, event.payload.turnId, event.timestamp, {
        id: event.payload.proposalId,
        type: "risk-rule",
        payload: event.payload
      });
    case "trade.approval.required":
    case "risk_rule.approval.required":
      return updateCard(state, event.payload.proposalId, (card) => {
        if (card.type === "trade" || card.type === "close-batch" || card.type === "spread") {
          return {
            ...card,
            payload: { ...card.payload, approvalId: event.payload.approvalId }
          } as ConversationCard;
        }
        return card;
      });
    case "proposal.result":
      return updateCard(state, event.payload.proposalId, (card) => {
        if (card.type === "trade" || card.type === "close-batch" || card.type === "spread") {
          return {
            ...card,
            payload: {
              ...card.payload,
              status: event.payload.status === "candidate_recorded" ? "candidate_recorded" : "rejected",
              auditId: event.payload.auditId,
              voiceConfirmBy: null,
              // 已过期 / 已作废 instead of 已放弃 when the user did not drop it (#932 P2).
              ...(event.payload.reason ? { closedReason: event.payload.reason } : {})
            }
          } as ConversationCard;
        }
        if (card.type === "risk-rule") {
          return {
            ...card,
            payload: {
              ...card.payload,
              status: event.payload.status === "risk_rules_applied" ? "risk_rules_applied" : "rejected",
              auditId: event.payload.auditId
            }
          };
        }
        return card;
      });
    case "agent.tool.started":
    case "agent.tool.progress":
    case "agent.tool.completed": {
      const cardId = `tool-${event.payload.taskId}-${event.payload.itemId}`;
      const exists = state.turns.some((turn) => turn.cards.some((card) => card.id === cardId));
      return exists
        ? updateCard(state, cardId, () => ({ id: cardId, type: "agent-tool", payload: event.payload }))
        : attachCard(state, event.payload.turnId, { id: cardId, type: "agent-tool", payload: event.payload });
    }
    case "agent.diff": {
      const cardId = `diff-${event.payload.taskId}-${event.payload.itemId}`;
      const exists = state.turns.some((turn) => turn.cards.some((card) => card.id === cardId));
      return exists
        ? updateCard(state, cardId, () => ({ id: cardId, type: "agent-diff", payload: event.payload }))
        : attachCard(state, event.payload.turnId, { id: cardId, type: "agent-diff", payload: event.payload });
    }
    case "agent.result":
    case "feed.account":
    case "feed.market":
    case "feed.activity":
    case "feed.execution":
    case "feed.ui":
      // Feed snapshots never become conversation content; useRuntimeFeed consumes them.
      return state;
    case "warning":
      return attachCard(state, event.payload.turnId, {
        id: `risk-${event.id}`,
        type: "risk",
        payload: event.payload
      });
    case "error":
      if (event.payload.scope === "connection") {
        return { ...state, connectionError: event.payload.message };
      }
      return attachErrorCard(state, event.payload, event.id);
    default:
      return state;
  }
}

function attachErrorCard(state: ConversationState, error: RuntimeError, eventId: string): ConversationState {
  const directTurn = error.turnId && state.turns.some((turn) => turn.id === error.turnId)
    ? error.turnId
    : undefined;
  const taskTurn = error.runId
    ? state.turns.find((turn) => turn.cards.some((card) => card.type === "task" && card.payload.taskId === error.runId))?.id
    : undefined;
  const fallbackTurn = [...state.turns].reverse().find((turn) => turn.role !== "user")?.id;
  const turnId = directTurn || taskTurn || fallbackTurn;
  if (!turnId) return state;
  const stopped = error.runId ? updateCard(state, error.runId, card => card.type === "task" && card.payload.status === "running" ? {...card, payload: {...card.payload, status: "failed", stage: error.message}} : card) : state;
  const closed = { ...stopped, turns: stopped.turns.map(turn => turn.id === turnId && turn.role === "assistant" && turn.completed === false ? { ...turn, completed: true } : turn) };
  return attachCard(closed, turnId, { id: `task-error-${eventId}`, type: "task-error", payload: error });
}
