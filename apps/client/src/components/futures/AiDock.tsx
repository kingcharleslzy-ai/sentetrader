import { AccountAccessContext } from "../../hooks/useAccountAccess";
import { Bot, ChevronLeft, History, MessageSquareText, PanelRightClose, Pause, Plus, Send } from "lucide-react";
import { useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { MarketQuoteSnapshot, AccountPosition } from "../../api";
import { useDisclosureSurface } from "../../uiSurfaces";
import { Conversation } from "../Conversation";
import type { useAppWorkbench } from "../AiActivity";
import type { Brief } from "../../runtimeClient";
import type { ConversationState } from "../../state/conversation";
import type { VoiceCallControls } from "../../voiceCallControls";
import type { FuturesDecisionWorkspaceProps } from "./props";
import { buildFlowItems } from "./aiFlow";
import { CallBar } from "./CallBar";
import { PttButton, PttLive, type PushToTalk } from "./PushToTalk";
import { pushToTalkStore } from "../../pushToTalk";
import { formatNumber, formatSigned, signClass } from "./support";
import type { WorkspaceChat } from "./useWorkspaceChat";
import { AiAvatar } from "../AiAvatar";
import { useStickToBottom } from "../../hooks/useStickToBottom";
import { useFitHeight } from "../../hooks/useFitHeight";
import type { AiAvatar as AiAvatarKind } from "./support";
import type { ChartFocus } from "./useChartFocus";

interface Props {
  workspace: FuturesDecisionWorkspaceProps;
  chat: WorkspaceChat;
  focus: ChartFocus;
  appWorkbench: ReturnType<typeof useAppWorkbench>;
  call: VoiceCallControls;
  voiceUnavailableReason: string;
  onClose: () => void;
  /** 暂停 on the running-strategy banner: the same monitor pause the strategy card runs. */
  onPauseStrategy: (strategyId: string) => Promise<void>;
  briefs: Brief[];
  quote: MarketQuoteSnapshot | undefined;
  decimals: number;
  selectedPrice: number | undefined;
  currentPositions: AccountPosition[];
  decisionTurns: ConversationState["turns"];
  aiExecutionActive: boolean;
  apiSetupRequired: boolean;
  apiConnectionFailed: boolean;
  apiProviderName: string;
  view: string;
  /** 设置 → AI 与对话 → 发送键: Enter sends, or only Ctrl/Cmd+Enter does. */
  sendKey: "enter" | "ctrlEnter";
  /** The pinned quick skills (设置 → 界面 → 快捷技能); empty hides the whole bar. */
  quickSkills: QuickSkillChip[];
  /** A chip click: the same composer path as a typed prompt. */
  onQuickPrompt: (text: string) => void;
  /** 建议问题 (useChatSuggestions): the AI's 0–2 questions for this moment and the place they are
   *  asked from. A click fills the input, never sends; none shows nothing. */
  suggestions: { label: string; items: string[] };
  /** 按住说话 (the workspace owns it and its hold key): the button, the live line and the notice live in the input row. */
  ptt?: PushToTalk;
  /** 设置 → 界面 → AI 形象: the header shows the same figure as the floating orb. */
  avatar?: AiAvatarKind;
  /** A structured card's UI button: the workspace's own UI-command path (throws when refused). */
  applyUi?: (changes: Record<string, unknown>) => void;
  /** The shown contract's display name (instrument list), for the brief cards; else the quote's. */
  symbolName?: string;
  /** 手机版 (2026-10-04): the page form — a back arrow to AI 首页 instead of 收起, the account in the
   *  head, and the big hold-to-talk button in the composer (the send button replaces it while typing). */
  mobile?: { onBack: () => void; accountLabel?: string; holdButton?: ReactNode };
}

/** One chip of the 快捷技能栏: the pinned skill's label, the fixed prompt it
 *  sends as the user's own turn, and the line that greys it out (null = usable). */
export interface QuickSkillChip { id: string; label: string; prompt: string; blockedLine: string | null }

/** "4 分钟" / "1 小时 12 分" since the monitor's first check of this run. */
function runningFor(since: string | undefined, now: number): string {
  const started = since ? Date.parse(since) : Number.NaN;
  if (!Number.isFinite(started)) return "刚开始";
  const minutes = Math.max(0, Math.floor((now - started) / 60_000));
  return minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

/**
 * 对话面板 (右侧栏重设计 v2): header with the contract/position tags and the 新对话 · 历史 · 收起
 * actions, the call bar, a pinned banner while a strategy is monitoring, the conversation (with the
 * AI's briefs merged on time) and the composer. Since 2026-09-25 it holds the conversation only:
 * strategy judgements and attempts live in the 策略 panel's 运行中 tab, fills and P&L in the
 * 持仓 panel's 成交 tab.
 */
export function AiDock({ workspace, chat, focus, appWorkbench, call, voiceUnavailableReason, onClose,
  onPauseStrategy, briefs, quote, decimals, selectedPrice, currentPositions, decisionTurns,
  aiExecutionActive, apiSetupRequired, apiConnectionFailed, apiProviderName, view, sendKey, quickSkills, onQuickPrompt,
  suggestions, ptt, avatar = "kline", applyUi, symbolName, mobile }: Props) {
  const access = useContext(AccountAccessContext);
  const [accessNotice, setAccessNotice] = useState("");
  const { prompt, setPrompt, promptError, promptSaveError, sendingPrompt, chatHistory, chatError, chatBlocked, selectChat, toggleHistory,
    submitPrompt } = chat;
  const { activeStrategy } = focus;
  // 历史对话 list as a commandable fold: `disclosure: chat-history`; the list itself rides in the snapshot as `chatHistory`.
  useDisclosureSurface("chat-history", { available: Boolean(workspace.onChangeChat), state: Boolean(chatHistory),
    apply: open => { if (open !== Boolean(chatHistory)) void toggleHistory(); } });
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 30_000); return () => window.clearInterval(timer); }, []);
  const [pausing, setPausing] = useState(false);
  const activity = appWorkbench.activity;
  const running = activity?.strategies.find(item => item.status === "monitoring" && (!activeStrategy || item.strategyId === activeStrategy))
    ?? activity?.strategies.find(item => item.status === "monitoring");
  const decision = running?.decision as (Record<string, unknown> & { since?: string; checks?: number }) | null | undefined;
  const monitorState = running?.monitorState as (Record<string, unknown> & { lastTodayPnl?: string | null }) | null | undefined;
  const todayPnl = Number(monitorState?.lastTodayPnl);
  const briefName = symbolName || quote?.name;
  const flow = useMemo(() => buildFlowItems({ briefs, symbol: quote?.symbol, symbolName: briefName }), [briefs, quote?.symbol, briefName]);
  // Structured cards ask through the composer's path and open panels through the UI-command path;
  // a refused command (no access, replay ended) says why under the conversation.
  // The phone keeps its one-line input (its page is restyled apart); the desktop composer grows with its text.
  const input = useRef<HTMLInputElement>(null);
  const area = useFitHeight(prompt, !mobile);
  const cardActions = useMemo(() => ({
    ...workspace.proposalActions,
    askAi: onQuickPrompt,
    applyUi: applyUi ? (changes: Record<string, unknown>) => {
      try { setAccessNotice(""); applyUi(changes); }
      catch (error) { setAccessNotice(error instanceof Error ? error.message : "这个界面现在打不开"); }
    } : undefined,
    // 流程卡's 改一下: the user finishes the sentence; nothing is sent.
    fillPrompt: (text: string) => { pushToTalkStore.dismiss(); setPrompt(text); (input.current ?? area.current)?.focus(); },
  }), [workspace.proposalActions, onQuickPrompt, applyUi, setPrompt, area]);
  const turns = decisionTurns;
  // Follow the newest message; whatever the user just sent or said always brings it back down.
  const lastUserTurnId = useMemo(() => [...turns].reverse().find(turn => turn.role === "user")?.id, [turns]);
  const scrollRef = useStickToBottom<HTMLDivElement>(lastUserTurnId);
  const empty = !turns.length && !flow.length;
  // 建议问题: in the empty conversation, else left of the 快捷技能 chips. A click only fills the
  // input (the user edits and sends), so it never runs a skill on its own. A new set rises in once.
  const exampleChips = suggestions.items.length ? (
    <div className="fdw-chat-examples" data-agent-target="chat-examples" aria-label="建议问题" key={suggestions.items.join("\n")}>
      {suggestions.items.map(text => (
        <button type="button" key={text} title={`填入输入框：${text}`}
          onClick={() => { pushToTalkStore.dismiss(); setPrompt(text); (input.current ?? area.current)?.focus(); }}>{text}</button>
      ))}
    </div>
  ) : null;
  const positionTag = currentPositions.length
    ? currentPositions.map(position => `${position.direction === "long" ? "多" : "空"} ${position.volume} 手 ${formatSigned(position.unrealizedPnl, 0)}`).join(" · ")
    : workspace.account ? "空仓" : "持仓读取中";
  const composerProps = {
    "aria-label": "向 AI 决策流提问", value: prompt,
    placeholder: workspace.agentAccount && !workspace.agentAccount?.authenticated ? "模型尚未就绪，请在设置里选择模型" : mobile ? "打字问 AI" : view === "review" ? "问 AI：这次交易哪里还能改进…" : "向 AI 提问，或输入你的交易思路…",
    disabled: workspace.connection !== "connected" || (workspace.agentAccount && !workspace.agentAccount?.authenticated),
  };
  const composerInput = mobile ? <input ref={input} {...composerProps} onChange={(event) => { pushToTalkStore.dismiss(); setPrompt(event.target.value); }}
    onKeyDown={event => {
      if (event.key !== "Enter" || sendKey !== "ctrlEnter") return;
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) event.currentTarget.form?.requestSubmit();
    }} />
    // 发送键 Enter: Enter sends and Shift+Enter breaks the line; Ctrl / Cmd + Enter: that sends and Enter breaks the line.
    // Never on the Enter that picks an input method's candidate (keyCode 229 in Safari).
    : <textarea ref={area} rows={1} {...composerProps} onChange={(event) => { pushToTalkStore.dismiss(); setPrompt(event.target.value); }}
      onKeyDown={event => {
        if (event.key !== "Enter" || event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (sendKey === "ctrlEnter" ? !(event.ctrlKey || event.metaKey) : event.shiftKey) return;
        event.preventDefault();
        event.currentTarget.form?.requestSubmit();
      }} />;
  const sendButton = <button type="submit" aria-label="发送给AI" disabled={sendingPrompt || workspace.chatBusy || !prompt.trim() || Boolean(workspace.agentAccount && !workspace.agentAccount?.authenticated)}><Send /></button>;
  return (
    <section className={`fdw-ai open${mobile ? " fdm-chat" : ""}`} aria-label="AI 对话" data-agent-target="panel:ai">
      {mobile ? (
        <header>
          <button type="button" className="fdm-back" aria-label="返回今天" onClick={mobile.onBack}><ChevronLeft /></button>
          <span className="fdm-chat-title"><strong>AI 交易助理</strong>{mobile.accountLabel ? <small>{mobile.accountLabel}</small> : null}</span>
          {workspace.onChangeChat ? <div className="fdw-ai-head-actions">
            <button type="button" aria-label="新对话" title="新对话" disabled={chatBlocked || workspace.connection !== "connected"} onClick={() => void selectChat()}><Plus /></button>
            <button type="button" aria-label="历史对话" title="历史对话" aria-expanded={Boolean(chatHistory)} disabled={chatBlocked} onClick={() => void toggleHistory()}><History /></button>
          </div> : null}
        </header>
      ) : <header>
        <div><AiAvatar kind={avatar} state={call.inCall ? "listening" : chat.sendingPrompt || workspace.chatBusy ? "busy" : "standby"} size={30} /><span><strong>AI</strong><small>{quote?.symbol ?? "未选择合约"} · {positionTag}{selectedPrice !== undefined ? ` · 候选价 ${formatNumber(selectedPrice, decimals)}` : ""}</small></span></div>
        <div className="fdw-ai-head-actions">
          <CallBar call={call} unavailableReason={voiceUnavailableReason} slot="header" />
          {workspace.onChangeChat ? <>
            <button type="button" aria-label="新对话" title="新对话" disabled={chatBlocked || workspace.connection !== "connected"} onClick={() => void selectChat()}><Plus /></button>
            <button type="button" aria-label="历史对话" title="历史对话" aria-expanded={Boolean(chatHistory)} disabled={chatBlocked} onClick={() => void toggleHistory()}><History /></button>
          </> : null}
          <button type="button" aria-label="收起AI" onClick={() => onClose()}><PanelRightClose /></button>
        </div>
      </header>}
      <CallBar call={call} unavailableReason={voiceUnavailableReason} slot="bar" />
      {running ? (
        <div className="fdw-strategy-banner" data-testid="strategy-banner">
          <div>
            <strong>{running.name}</strong>
            <span>{`运行 ${runningFor(typeof decision?.since === "string" ? decision.since : undefined, now)} · 判断 ${Number(decision?.checks) || 0} 次`}{Number.isFinite(todayPnl) ? <> · 今日 <b className={signClass(todayPnl)}>{formatSigned(todayPnl, 0)}</b></> : null}</span>
          </div>
          <button type="button" disabled={pausing} onClick={() => { setPausing(true); void onPauseStrategy(running.strategyId).finally(() => setPausing(false)); }}>
            <Pause />{pausing ? "暂停中…" : "暂停"}
          </button>
        </div>
      ) : null}
      {chatError ? <p role="alert" className="fdw-ai-error">{chatError}</p> : null}
      {chatHistory ? <div className="fdw-chat-history" data-testid="chat-history" aria-label="历史对话列表">{chatHistory.length ? chatHistory.map(item => <button type="button" disabled={chatBlocked || item.sessionId === workspace.chatSessionId} key={item.sessionId} onClick={() => void selectChat(item.sessionId)}>{new Date(item.createdAt).toLocaleString("zh-CN")}{item.sessionId === workspace.chatSessionId ? " · 当前" : ""}</button>) : <p>暂无历史对话</p>}</div> : null}
      {workspace.agentAccount && !workspace.agentAccount?.authenticated ? (
        <section className="fdw-agent-login" data-testid="agent-login">
          <Bot />
          <div>
            <strong>{apiConnectionFailed ? `检查 ${apiProviderName}` : apiSetupRequired ? `配置 ${apiProviderName}` : "模型尚未就绪"}</strong>
            <p>{apiConnectionFailed
              ? "模型密钥已在服务器配置，但上游模型尚未连接；请稍后重试或在设置里换一个模型。"
              : apiSetupRequired
                ? `服务器尚未配置 ${apiProviderName}，请在设置里换一个可用模型。`
              : "请先在设置里选择当前模型，再回到这里。"}</p>
          </div>
        </section>
      ) : null}
      <div className="fdw-ai-scroll" ref={scrollRef}>
        <Conversation turns={turns} actions={cardActions} accountOverride={workspace.account?.account} systemItems={flow} />
        {empty ? <div className="fdw-ai-empty"><MessageSquareText />{exampleChips ? <strong>在{suggestions.label}可以这样问</strong> : null}{aiExecutionActive ? <span>AI 模拟执行可用，实盘始终关闭。</span> : null}{exampleChips}</div> : null}
      </div>
      {(!empty && exampleChips) || quickSkills.length ? (
        <div className="fdw-ai-chips">
          {empty ? null : exampleChips}
          {quickSkills.length ? (
            <div className="fdw-quick-skills" data-agent-target="quick-skills" aria-label="快捷技能">
              {quickSkills.map(chip => (
                <button type="button" key={chip.id} disabled={chatBlocked}
                  title={chip.blockedLine ?? `发送：${chip.prompt}`}
                  onClick={() => { if (chip.blockedLine) { if (access?.deny) access.deny(chip.blockedLine); else setAccessNotice(chip.blockedLine); return; } onQuickPrompt(chip.prompt); }}>
                  {chip.label}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
      {accessNotice && <small role="status">{accessNotice}</small>}
      <form className="fdw-ai-composer" onSubmit={(event) => void submitPrompt(event)}>
        {(promptError || promptSaveError) && <small role="alert">{promptError || "草稿暂未保存，请勿关闭页面"}</small>}
        {ptt?.status.notice && ptt.status.state === "idle" && !ptt.status.target ? <small role="status" className="fdw-ptt-notice" title="点一下关闭" onClick={() => pushToTalkStore.dismiss()}>{ptt.status.notice}</small> : null}
        {ptt && ptt.status.state !== "idle" && !ptt.status.target && !mobile ? <PttLive status={ptt.status} /> : composerInput}
        {mobile ? prompt.trim() || !mobile.holdButton ? sendButton : mobile.holdButton : <>{ptt ? <PttButton ptt={ptt} /> : null}{sendButton}</>}
      </form>
    </section>
  );
}
