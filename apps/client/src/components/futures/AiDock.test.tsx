import { AccountAccessContext } from "../../hooks/useAccountAccess";
import { act, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { AiDock } from "./AiDock";
import { useWorkspaceChat } from "./useWorkspaceChat";
import { AiOrb } from "../AiActivity";
import { buildFlowItems } from "./aiFlow";
import { disclosureStates, resetUiSurfaces } from "../../uiSurfaces";
import { readQueryClient } from "../../runtimeClient";
import { voiceCallStore } from "../../voiceRealtime";
import { CLIENT_BUILD } from "../../clientBuild";
import { useVoiceCall } from "../../voiceCallControls";
import { CallBar } from "./CallBar";

const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={readQueryClient}>{children}</QueryClientProvider>;

const activity = (overrides: Record<string, unknown> = {}) => ({
  environment: "simulation", replayId: null, symbol: "SHFE.rb2610", capturedAt: null, dataStatus: "normal", ageSeconds: 1,
  orders: [], fills: [], strategies: [], protections: [], ...overrides,
});

/** The dock with a stub call, so every call-bar state is reachable without a WebSocket. */
function Dock({ callState = "idle", muted = false, level = 0, available = true, reason = "", ...rest }: Record<string, any> = {}) {
  const call = {
    status: { state: callState, muted, level, startedAt: callState === "listening" ? Date.now() - 65_000 : undefined, error: callState === "error" ? "语音通道连接失败" : undefined },
    inCall: callState !== "idle" && callState !== "error",
    available, start: rest.onStart ?? vi.fn(), stop: rest.onStop ?? vi.fn(), mute: rest.onMute ?? vi.fn(), retry: rest.onRetry ?? vi.fn(),
    dismiss: rest.onDismiss ?? vi.fn(),
  };
  const workspace = {
    connection: "connected", proposalActions: {}, account: undefined, chatSessionId: "s-1",
    onChangeChat: rest.onChangeChat, strategyPanel: <div>策略面板</div>, ...rest.workspace,
  };
  const chat = {
    prompt: "", setPrompt: vi.fn(), promptError: "", promptSaveError: false, sendingPrompt: false,
    chatHistory: rest.chatHistory, chatError: "", chatBlocked: false, selectChat: vi.fn(), toggleHistory: rest.toggleHistory ?? vi.fn(),
    submitPrompt: vi.fn(), ...rest.chat,
  };
  return <AiDock workspace={workspace as any} chat={chat as any} focus={{ activeStrategy: "" } as any}
    appWorkbench={{ activity: rest.activity, config: {}, error: "", updateConfig: vi.fn() } as any}
    call={call as any} voiceUnavailableReason={reason}
    onClose={vi.fn()} onPauseStrategy={rest.onPauseStrategy ?? (async () => undefined)}
    briefs={rest.briefs ?? []} quote={rest.quote ?? { symbol: "SHFE.rb2610" } as any} decimals={0} applyUi={rest.applyUi}
    selectedPrice={undefined} currentPositions={rest.currentPositions ?? []} decisionTurns={rest.turns ?? []}
    aiExecutionActive={false} apiSetupRequired={false} apiConnectionFailed={false} apiProviderName="DeepSeek" view="trade" sendKey="enter"
    quickSkills={rest.quickSkills ?? []} onQuickPrompt={rest.onQuickPrompt ?? vi.fn()}
    suggestions={rest.suggestions ?? { label: "K 线图", items: [] }} />;
}

beforeEach(() => { resetUiSurfaces(); voiceCallStore.reset(); });

test("通话栏四种状态：未通话一键接通、接通中、通话中（音量·时长·静音·结束）、出错重试", () => {
  const onStart = vi.fn(), onStop = vi.fn(), onMute = vi.fn(), onRetry = vi.fn();
  const view = render(<Dock onStart={onStart} />, { wrapper });
  // Idle: one full-width green pill, a single click connects (never press-and-hold).
  const start = screen.getByRole("button", { name: "开始语音通话" });
  expect(start).toBeEnabled();
  fireEvent.click(start);
  expect(onStart).toHaveBeenCalledOnce();

  view.rerender(<Dock callState="connecting" />);
  expect(screen.getByRole("status")).toHaveTextContent("接通中…");
  expect(screen.queryByRole("button", { name: "开始语音通话" })).toBeNull();

  view.rerender(<Dock callState="listening" muted level={0.4} onStop={onStop} onMute={onMute} />);
  expect(screen.getByText("01:05")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "取消静音" }));
  expect(onMute).toHaveBeenCalledWith(false);
  fireEvent.click(screen.getByRole("button", { name: "结束语音通话" }));
  expect(onStop).toHaveBeenCalledOnce();

  const onDismiss = vi.fn();
  view.rerender(<Dock callState="error" onRetry={onRetry} onDismiss={onDismiss} />);
  expect(screen.getByRole("alert")).toHaveTextContent("语音通道连接失败");
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(onRetry).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(onDismiss).toHaveBeenCalledOnce();
});

test("通话出错（没有麦克风）点 ✕ 关闭：错误条消失、通话栏与 AI 球一起回到空闲，不重试", () => {
  const start = vi.spyOn(voiceCallStore, "start").mockImplementation(async () => undefined);
  function Both() {
    const bar = useVoiceCall({ baseUrl: "http://runtime", sessionId: "s-1", voiceAvailable: true });
    return <>
      <AiOrb baseUrl="http://runtime" symbol="RB" replay={false} error="" voiceAvailable sessionId="s-1"
        config={{ showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] }}
        onConfigChange={vi.fn()} onOpenChat={vi.fn()} activity={activity() as any} />
      <CallBar call={bar} slot="header" /><CallBar call={bar} slot="bar" />
    </>;
  }
  render(<Both />, { wrapper });
  act(() => { voiceCallStore["publish"]({ state: "error", error: "未检测到麦克风（NotFoundError）" } as never); });
  expect(screen.getByRole("alert")).toHaveTextContent("未检测到麦克风（NotFoundError）");
  expect(screen.getByTestId("ai-orb")).toHaveAttribute("title", "AI 语音：点一下重试 · 右键更多");
  expect(screen.queryByRole("button", { name: "开始语音通话" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(voiceCallStore.snapshot().state).toBe("idle");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(document.querySelector(".ai-orb-label")).toBeNull();
  expect(screen.getByTestId("ai-orb")).toHaveAttribute("title", "AI 语音：点一下通话 · 右键更多");
  expect(screen.getByRole("button", { name: "开始语音通话" })).toHaveClass("fdw-call-start");
  expect(start).not.toHaveBeenCalled();
  start.mockRestore();
});

test("语音不可用时开始通话禁用并写出原因", () => {
  render(<Dock available={false} reason="服务器未配置语音密钥，暂不能通话" />, { wrapper });
  expect(screen.getByRole("button", { name: "开始语音通话" })).toBeDisabled();
  expect(screen.getByText("服务器未配置语音密钥，暂不能通话")).toBeVisible();
});

test("快捷技能栏：点击以用户身份发送 quickAction 文本；不可用可点并解释原因；无钉选不渲染", () => {
  const onQuickPrompt = vi.fn();
  const chips = [
    { id: "strategy-backtests", label: "回测与体检", prompt: "回测我选中的策略", blockedLine: null },
    { id: "help", label: "使用说明", prompt: "帮助", blockedLine: "「使用说明」已停用" },
  ];
  const view = render(<Dock quickSkills={chips} onQuickPrompt={onQuickPrompt} />, { wrapper });
  const bar = screen.getByLabelText("快捷技能");
  const guided = within(bar).getByRole("button", { name: "回测与体检" });
  expect(guided).toBeEnabled();
  expect(guided).toHaveAttribute("title", "发送：回测我选中的策略");
  fireEvent.click(guided);
  expect(onQuickPrompt).toHaveBeenCalledWith("回测我选中的策略");
  const help = within(bar).getByRole("button", { name: "使用说明" });
  expect(help).toBeEnabled();
  expect(help).toHaveAttribute("title", "「使用说明」已停用");
  fireEvent.click(help);
  expect(screen.getByRole("status")).toHaveTextContent("「使用说明」已停用");
  expect(onQuickPrompt).toHaveBeenCalledTimes(1);  // the denied chip never sends
  view.rerender(<Dock quickSkills={[]} onQuickPrompt={onQuickPrompt} />);
  expect(screen.queryByLabelText("快捷技能")).toBeNull();
});

test("建议问题：空对话时在空态里（有建议才有“可以这样问”），有对话时排在快捷技能左侧；点一下只填进输入框并聚焦，不发送；没有建议什么都不显示", () => {
  const setPrompt = vi.fn(), submitPrompt = vi.fn(), onQuickPrompt = vi.fn();
  const suggestions = { label: "报价总表", items: ["螺纹今天放量了吗", "黄金夜盘怎么看"] };
  const chips = [{ id: "review-summary", label: "复盘", prompt: "帮我复盘一下今天", blockedLine: null }];
  const view = render(<Dock suggestions={suggestions} chat={{ setPrompt, submitPrompt }} quickSkills={chips} onQuickPrompt={onQuickPrompt} />, { wrapper });
  expect(screen.getByText("在报价总表可以这样问")).toBeVisible();
  const group = screen.getByLabelText("建议问题");
  expect(group).toHaveAttribute("data-agent-target", "chat-examples");
  expect(within(group).getAllByRole("button").map(button => button.textContent)).toEqual(suggestions.items);
  fireEvent.click(within(group).getByRole("button", { name: "黄金夜盘怎么看" }));
  expect(setPrompt).toHaveBeenCalledWith("黄金夜盘怎么看");
  expect(screen.getByRole("textbox", { name: "向 AI 决策流提问" })).toHaveFocus();
  expect(submitPrompt).not.toHaveBeenCalled();
  expect(onQuickPrompt).not.toHaveBeenCalled();

  const turns = [{ id: "t1", role: "user", text: "现在怎么看？", timestamp: "2026-09-19T02:05:00Z", cards: [] }];
  view.rerender(<Dock suggestions={suggestions} chat={{ setPrompt, submitPrompt }} quickSkills={chips} onQuickPrompt={onQuickPrompt} turns={turns} />);
  expect(screen.queryByText("在报价总表可以这样问")).toBeNull();
  const row = document.querySelector<HTMLElement>(".fdw-ai-chips")!;
  expect(within(row).getAllByRole("button").map(button => button.textContent)).toEqual([...suggestions.items, "复盘"]);
  fireEvent.click(within(row).getByRole("button", { name: "螺纹今天放量了吗" }));
  expect(setPrompt).toHaveBeenLastCalledWith("螺纹今天放量了吗");
  expect(submitPrompt).not.toHaveBeenCalled();
  // Nothing pinned and nothing suggested: no chip row at all.
  view.rerender(<Dock turns={turns} />);
  expect(document.querySelector(".fdw-ai-chips")).toBeNull();
  // An empty conversation without suggestions: no heading, no group.
  view.rerender(<Dock suggestions={{ label: "报价总表", items: [] }} />);
  expect(screen.queryByText(/可以这样问/)).toBeNull();
  expect(screen.queryByLabelText("建议问题")).toBeNull();
});

test("策略运行横幅只在有监控中的策略时出现，暂停走同一个监控暂停动作", () => {
  const onPauseStrategy = vi.fn(async () => undefined);
  const view = render(<Dock activity={activity()} />, { wrapper });
  expect(screen.queryByTestId("strategy-banner")).toBeNull();
  const running = activity({ strategies: [{ strategyId: "st-1", name: "均线回踩", status: "monitoring",
    decision: { status: "waiting", detail: "等待信号", checkedAt: "2026-09-19T02:00:00Z", since: new Date(Date.now() - 25 * 60_000 - 30_000).toISOString(), checks: 42 },
    monitorState: { pauseReason: null, consecutiveLosses: 0, lastTodayPnl: "-320" }, recentAttempts: [] }] });
  view.rerender(<Dock activity={running} onPauseStrategy={onPauseStrategy} />);
  const banner = screen.getByTestId("strategy-banner");
  expect(banner).toHaveTextContent("均线回踩");
  expect(banner).toHaveTextContent("运行 25 分钟 · 判断 42 次");
  expect(banner).toHaveTextContent("-320");
  fireEvent.click(within(banner).getByRole("button", { name: /暂停/ }));
  expect(onPauseStrategy).toHaveBeenCalledWith("st-1");
});

test("对话面板只放对话：没有筛选片，策略判断、成交、保护、平仓盈亏不进对话，简讯按时间留在对话里", () => {
  const turns = [{ id: "t1", role: "user", text: "现在怎么看？", timestamp: "2026-09-19T02:05:00Z", cards: [] }];
  const trading = activity({
    strategies: [{ strategyId: "st-1", name: "均线回踩", status: "ready",
      decision: { status: "waiting", detail: "等待信号", checkedAt: "2026-09-19T02:01:00Z" }, monitorState: null, recentAttempts: [] }],
    fills: [{ tradeId: "f1", clientOrderId: "o1", symbol: "SHFE.rb2610", direction: "BUY", offset: "OPEN", price: 3100, volume: 1, tradeDateTime: Date.parse("2026-09-19T02:00:00Z") * 1e6, source: "ai" }],
    protections: [{ stopId: "p1", symbol: "SHFE.rb2610", status: "ARMED", updatedAt: "2026-09-19T02:02:00Z", triggerPrice: 3050, volume: 1 }],
  });
  render(<Dock activity={trading} turns={turns}
    briefs={[{ id: "b1", kind: "news", text: "交易所提高保证金", source: "SHFE", publishedAt: "2026-09-19T02:03:00Z", expiresAt: "2026-09-20T00:00:00Z" }]} />, { wrapper });
  for (const name of ["全部", "对话", "策略与交易"]) expect(screen.queryByRole("button", { name })).toBeNull();
  const flow = screen.getByTestId("conversation");
  const lines = Array.from(flow.children).map(node => node.textContent ?? "");
  expect(lines).toHaveLength(2);
  expect(lines[0]).toContain("交易所提高保证金"); // 02:03 简讯
  expect(lines[1]).toContain("现在怎么看？");   // 02:05
  expect(flow).not.toHaveTextContent("策略判断");
  expect(flow).not.toHaveTextContent("3050");
  expect(buildFlowItems({ briefs: [] })).toEqual([]);
  // AI 简报订阅: a brief names its own product; only a Runtime without subscriptions falls back to the dock's quote.
  const brief = { id: "b", kind: "news" as const, text: "港口到货增加", source: "fixture", publishedAt: "2026-10-05T01:00:00Z", expiresAt: "2026-10-06T00:00:00Z" };
  expect(buildFlowItems({ briefs: [{ ...brief, product: "DCE.i", productName: "铁矿石", symbol: "DCE.i2601" }], symbol: "SHFE.rb2610", symbolName: "螺纹钢" })[0])
    .toMatchObject({ symbol: "DCE.i2601", symbolName: "铁矿石", product: "DCE.i" });
  expect(buildFlowItems({ briefs: [brief], symbol: "SHFE.rb2610", symbolName: "螺纹钢" })[0]).toMatchObject({ symbol: "SHFE.rb2610", symbolName: "螺纹钢" });
});

test("结构化卡片：快讯卡的问 AI 走快捷提问并带合约名，界面按钮被拒时说明原因", () => {
  const onQuickPrompt = vi.fn();
  const applyUi = vi.fn(() => { throw new Error("回放已结束"); });
  const turns = [{ id: "j", role: "assistant", text: "", timestamp: "2026-10-04T02:06:00Z", cards: [{ id: "judgment-1", type: "research-judgment",
    payload: { turnId: "j", cardId: "judgment-1", symbol: "SHFE.rb2610", productName: "螺纹钢", status: "ready", stance: "偏多",
      actions: [{ kind: "ui", label: "打开调研", changes: { news: { tab: "desk" } } }] } }] }];
  render(<Dock onQuickPrompt={onQuickPrompt} applyUi={applyUi} turns={turns} quote={{ symbol: "SHFE.rb2610", name: "螺纹钢2610" }}
    briefs={[{ id: "b1", kind: "news", text: "交易所提高保证金", source: "交易所", publishedAt: "2026-10-04T02:03:00Z", expiresAt: "2026-10-05T02:03:00Z" }]} />, { wrapper });
  fireEvent.click(screen.getByRole("button", { name: "问 AI 这条资讯" }));
  expect(onQuickPrompt).toHaveBeenCalledWith("这条资讯对螺纹钢2610有什么影响：交易所提高保证金");
  fireEvent.click(screen.getByRole("button", { name: "打开调研" }));
  expect(applyUi).toHaveBeenCalledWith({ news: { tab: "desk" } });
  expect(screen.getByRole("status")).toHaveTextContent("回放已结束");
});

test("历史对话按钮开合锚定列表，仍是 disclosure chat-history", () => {
  const toggleHistory = vi.fn();
  const view = render(<Dock onChangeChat={vi.fn()} toggleHistory={toggleHistory} />, { wrapper });
  expect(disclosureStates()["chat-history"]).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "历史对话" }));
  expect(toggleHistory).toHaveBeenCalledOnce();
  view.rerender(<Dock onChangeChat={vi.fn()} toggleHistory={toggleHistory}
    chatHistory={[{ sessionId: "s-1", createdAt: "2026-09-19T01:00:00Z" }, { sessionId: "s-2", createdAt: "2026-09-19T02:00:00Z" }]} />);
  expect(disclosureStates()["chat-history"]).toBe(true);
  const list = screen.getByTestId("chat-history");
  expect(within(list).getAllByRole("button")).toHaveLength(2);
  expect(within(list).getByRole("button", { name: /当前/ })).toBeDisabled();
});

test("悬浮球与通话栏共用同一路通话，voice-call / voice-mute 只由球登记一次", () => {
  const start = vi.spyOn(voiceCallStore, "start").mockImplementation(async () => undefined);
  function Both() {
    // The call bar's hook must not register the surfaces; only the always-mounted orb does.
    const bar = useVoiceCall({ baseUrl: "http://runtime", sessionId: "s-1", voiceAvailable: true });
    return <>
      <AiOrb baseUrl="http://runtime" symbol="RB" replay={false} error="" voiceAvailable sessionId="s-1"
        config={{ showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] }}
        onConfigChange={vi.fn()} onOpenChat={vi.fn()} activity={activity() as any} />
      <button type="button" onClick={bar.start}>bar-start</button>
    </>;
  }
  const view = render(<Both />, { wrapper });
  expect(disclosureStates()["voice-call"]).toBe(false);
  fireEvent.click(screen.getByText("bar-start"));
  expect(start).toHaveBeenCalledWith("http://runtime", "s-1");
  // The orb's registration survives: unmounting the whole tree is what removes it.
  expect(disclosureStates()["voice-call"]).toBe(false);
  act(() => { view.unmount(); });
  expect(disclosureStates()["voice-call"]).toBeUndefined();
  start.mockRestore();
});

test("球和对话面板显示同一个通话状态：接通中、通话中、静音一致，在任一处挂断两处同时回到未通话", () => {
  const stop = vi.spyOn(voiceCallStore, "stop");
  function Both() {
    const bar = useVoiceCall({ baseUrl: "http://runtime", sessionId: "s-1", voiceAvailable: true });
    return <>
      <AiOrb baseUrl="http://runtime" symbol="RB" replay={false} error="" voiceAvailable sessionId="s-1"
        config={{ showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] }}
        onConfigChange={vi.fn()} onOpenChat={vi.fn()} activity={activity() as any} />
      <CallBar call={bar} slot="header" /><CallBar call={bar} slot="bar" />
    </>;
  }
  render(<Both />, { wrapper });
  const orb = screen.getByTestId("ai-orb");
  expect(orb).toHaveAttribute("title", "AI 语音：点一下通话 · 右键更多");
  expect(orb).toHaveAccessibleName(/^开始语音通话；/);
  expect(screen.getByRole("button", { name: "开始语音通话" })).toHaveClass("fdw-call-start");
  const barState = () => document.querySelector(".fdw-call-state")?.textContent;
  const orbState = () => document.querySelector(".ai-orb-label")?.textContent;
  act(() => { voiceCallStore["publish"]({ state: "connecting" } as never); });
  expect(barState()).toBe("接通中…");
  expect(orbState()).toBe("接通中…");
  act(() => { voiceCallStore["publish"]({ state: "listening", muted: true, startedAt: Date.now() } as never); });
  expect(barState()).toBe("已静音");
  expect(orbState()).toBe("已静音");
  expect(orb).toHaveAttribute("data-muted", "true");
  expect(orb).toHaveAttribute("title", "AI 语音：点一下挂断 · 右键更多");
  expect(disclosureStates()["voice-call"]).toBe(true);
  // Hang up from the 对话面板: the orb follows; the header 通话 pill comes back.
  fireEvent.click(screen.getByRole("button", { name: "结束语音通话" }));
  expect(stop).toHaveBeenCalledOnce();
  expect(orb).not.toHaveAttribute("data-call");
  expect(disclosureStates()["voice-call"]).toBe(false);
  expect(screen.getByRole("button", { name: "开始语音通话" })).toHaveClass("fdw-call-start");
  expect(orb).toHaveAccessibleName(/^开始语音通话；/);
  // And from the orb: a second call, ended by clicking the orb, also clears the call bar.
  act(() => { voiceCallStore["publish"]({ state: "speaking", startedAt: Date.now() } as never); });
  expect(barState()).toBe("AI 在说");
  fireEvent.click(orb);
  expect(stop).toHaveBeenCalledTimes(2);
  expect(document.querySelector(".fdw-call-state")).toBeNull();
  stop.mockRestore();
});

test("新对话 / 历史对话只被切换或发送阻塞：未完成回合与 running 任务卡不再锁死", () => {
  const turn = (id: string, role: "user" | "assistant", completed: boolean, cards: unknown[] = []) =>
    ({ id, role, text: "", timestamp: "2026-09-18T12:00:00Z", completed, cards });
  const taskCard = (status: string) => ({ id: "run-voice", type: "task",
    payload: { turnId: "turn-voice", taskId: "run-voice", title: "切换主题", subtitle: "", progress: 100, status, steps: [] } });
  const chatBlocked = (turns: unknown[], chatBusy = false) => renderHook(() => useWorkspaceChat({
    workspace: { chatBusy, workspaceScope: "scope:simulation", state: { turns }, onPrompt: vi.fn() } as any,
    promptContext: () => undefined,
  })).result.current.chatBlocked;

  // 手机上长期为真的两个旧启发式：最后一条 assistant 回合没等到 completed（后台标签页丢了
  // message.completed）、旧任务卡停在 running。现在都不再阻塞 —— 服务端会话与运行相互独立。
  const stuck = [turn("turn-u1", "user", true), turn("turn-a1", "assistant", false, [taskCard("running")])];
  expect(chatBlocked(stuck)).toBe(false);
  expect(chatBlocked(stuck, true)).toBe(true);

  const view = render(<Dock onChangeChat={vi.fn()} chat={{ chatBlocked: false }} />, { wrapper });
  expect(screen.getByRole("button", { name: "新对话" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "历史对话" })).toBeEnabled();

  // 切换进行中（chatBusy，与 sendingPrompt 走同一个 chatBlocked 布尔）：两个按钮都禁用。
  view.rerender(<Dock onChangeChat={vi.fn()} chat={{ chatBlocked: true }} />);
  expect(screen.getByRole("button", { name: "新对话" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "历史对话" })).toBeDisabled();

  // 未连接只挡要开新会话的「新对话」；历史列表是只读操作，仍可打开。
  view.rerender(<Dock onChangeChat={vi.fn()} workspace={{ connection: "reconnecting" }} />);
  expect(screen.getByRole("button", { name: "新对话" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "历史对话" })).toBeEnabled();
});


test("off-plan member quick skills and voice explain denial without sending or starting", () => {
  const deny = vi.fn(), onQuickPrompt = vi.fn(), onStart = vi.fn();
  const access = { reason: () => "当前档位未开通", deny, require: vi.fn(() => { deny("当前档位未开通"); return false; }) };
  render(<AccountAccessContext.Provider value={access}><Dock available={false} onStart={onStart}
    quickSkills={[{ id: "help", label: "使用说明", prompt: "帮助", blockedLine: "当前档位未开通" }]} onQuickPrompt={onQuickPrompt}/></AccountAccessContext.Provider>, { wrapper });
  fireEvent.click(screen.getByRole("button", { name: "使用说明" }));
  const start = screen.getByRole("button", { name: "开始语音通话" });
  expect(start).toBeEnabled();
  fireEvent.click(start);
  expect(deny).toHaveBeenCalledTimes(2);
  expect(onQuickPrompt).not.toHaveBeenCalled();
  expect(onStart).not.toHaveBeenCalled();
});

test("没有收到麦克风声音：通话中听你说时球和通话栏都提示，“?”里有排查说明；静音或声音恢复后不显示", () => {
  function Both() {
    const bar = useVoiceCall({ baseUrl: "http://runtime", sessionId: "s-1", voiceAvailable: true });
    return <>
      <AiOrb baseUrl="http://runtime" symbol="RB" replay={false} error="" voiceAvailable sessionId="s-1"
        config={{ showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] }}
        onConfigChange={vi.fn()} onOpenChat={vi.fn()} activity={activity() as any} />
      <CallBar call={bar} slot="bar" />
    </>;
  }
  render(<Both />, { wrapper });
  const hint = () => document.querySelector('[data-agent-target="call-mic-silent"]');
  act(() => { voiceCallStore["publish"]({ state: "listening", micSilent: true, micLabel: "耳机 (立体声)", startedAt: Date.now() } as never); });
  expect(hint()).toHaveTextContent("没有收到麦克风声音（当前麦克风：耳机 (立体声)）");
  expect(hint()).toHaveTextContent("到地址栏左侧的网站设置里换麦克风，蓝牙耳机选带“免提 / Hands-Free / Headset”的那一项");
  act(() => { voiceCallStore["publish"]({ state: "listening", micSilent: true, micLabel: "耳机 (立体声)", micSuggestion: "耳麦 (免提)", startedAt: Date.now() } as never); });
  expect(hint()).toHaveTextContent("到地址栏左侧的网站设置里换成「耳麦 (免提)」");
  // The desktop app has no address bar: the system's input device is where the microphone is chosen (#875).
  CLIENT_BUILD.platform = "desktop";
  try {
    act(() => { voiceCallStore["publish"]({ state: "listening", micSilent: true, micLabel: "耳机 (立体声)", startedAt: Date.now() } as never); });
    expect(hint()).toHaveTextContent("到系统声音设置的输入设备里换麦克风");
    expect(hint()).not.toHaveTextContent("地址栏");
  } finally {
    CLIENT_BUILD.platform = "web";
  }
  act(() => { voiceCallStore["publish"]({ state: "listening", micSilent: true, micLabel: "耳机 (立体声)", micSuggestion: "耳麦 (免提)", startedAt: Date.now() } as never); });
  expect(document.querySelector(".ai-orb-label")?.textContent).toBe("没收到麦克风声音");
  expect(within(hint() as HTMLElement).getByRole("button", { name: "没有收到麦克风声音的说明" })).toBeInTheDocument();
  act(() => { voiceCallStore["publish"]({ state: "listening", micSilent: true, muted: true, startedAt: Date.now() } as never); });
  expect(hint()).toBeNull();
  expect(document.querySelector(".ai-orb-label")?.textContent).toBe("已静音");
  act(() => { voiceCallStore["publish"]({ state: "listening", micSilent: false, startedAt: Date.now() } as never); });
  expect(hint()).toBeNull();
  expect(document.querySelector(".ai-orb-label")?.textContent).toBe("听你说");
});
