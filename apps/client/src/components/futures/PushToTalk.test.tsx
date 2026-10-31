import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { AiDock } from "./AiDock";
import { PttButton, PttLive, usePushToTalk } from "./PushToTalk";
import { PTT_TOO_SHORT, pushToTalkStore, setPttTarget } from "../../pushToTalk";
import { readQueryClient } from "../../runtimeClient";
import { resetShortcutStore } from "../../shortcuts";
import { resetUiSurfaces } from "../../uiSurfaces";
import { FakeWebSocket, installPttFakes, removePttFakes, speak } from "../../../tests/support/pttFakes";
import { xiaomiRemoteStore, type RemoteInput } from "../../xiaomiRemote/store";

vi.mock("../../frontendLog", () => ({ logDesktop: vi.fn() }));

function Harness({ blockedReason = "", sendOnRelease = true, onSend = vi.fn(), onFill = vi.fn(), onKeyPress, chatSessionId }: {
  blockedReason?: string; sendOnRelease?: boolean; onSend?: (text: string) => void; onFill?: (text: string) => void; onKeyPress?: () => void; chatSessionId?: string }) {
  const ptt = usePushToTalk({ baseUrl: "http://runtime", blockedReason, sendOnRelease, onSend, onFill, onKeyPress, chatSessionId });
  return <form>{ptt.status.state !== "idle" ? <PttLive status={ptt.status} /> : <input aria-label="输入" />}<PttButton ptt={ptt} /></form>;
}

/** Keyboard events on the window, as the page receives them; returns whether the default was prevented. */
function key(type: "keydown" | "keyup", code: string, keyName = code.startsWith("Alt") ? "Alt" : code, repeat = false) {
  const event = new KeyboardEvent(type, { code, key: keyName, repeat, bubbles: true, cancelable: true });
  act(() => { window.dispatchEvent(event); });
  return event.defaultPrevented;
}

async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
function advance(ms: number) { act(() => { vi.advanceTimersByTime(ms); }); }

/** Hold → speak → release → end → the Runtime's final. */
async function holdAndSay(text: string, release: () => void) {
  await flush();
  const socket = FakeWebSocket.latest!;
  act(() => { socket.open(); speak(); });
  advance(600);
  release();
  advance(200);
  expect(socket.commands()).toEqual(["end"]);
  act(() => socket.receive({ type: "final", text }));
  return socket;
}

beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); resetShortcutStore(); installPttFakes(); pushToTalkStore.reset(); resetUiSurfaces(); });
afterEach(() => { act(() => pushToTalkStore.reset()); vi.useRealTimers(); removePttFakes(); });

test("遥控 PCM 经正式按住说话发送；Esc 后迟到松键不结束新的键盘录音", async () => {
  let receive!: (event: RemoteInput) => void;
  let frame!: (pcm: Int16Array) => void;
  const focus = vi.spyOn(document, "hasFocus").mockReturnValue(true);
  const subscribe = vi.spyOn(xiaomiRemoteStore, "onInput").mockImplementation(callback => { receive = callback; return () => {}; });
  const source = { type: "start", input: (send: (pcm: Int16Array) => void) => ({ start: async () => { frame = send; }, stop: () => {} }) } as const;
  const onSend = vi.fn(); const view = render(<Harness onSend={onSend} />);
  act(() => receive(source)); await flush();
  const first = FakeWebSocket.latest!;
  act(() => { first.open(); frame(new Int16Array([500, -500])); });
  advance(600); act(() => receive({ type: "stop" })); advance(200);
  expect(first.frames()).toBe(1); expect(first.commands()).toEqual(["end"]);
  act(() => first.receive({ type: "final", text: "遥控器说的话" })); expect(onSend).toHaveBeenCalledWith("遥控器说的话");
  act(() => receive(source)); await flush();
  key("keydown", "Escape", "Escape");
  key("keydown", "AltRight"); await flush();
  act(() => receive({ type: "stop" }));
  expect(pushToTalkStore.snapshot().state).toBe("recording");
  view.unmount(); subscribe.mockRestore(); focus.mockRestore();
});

test("按住右 Alt 说话、松开发送：按下时阻止默认（Windows 不去聚焦菜单栏），重复按键不重开", async () => {
  const onSend = vi.fn(), onFill = vi.fn();
  render(<Harness onSend={onSend} onFill={onFill} />);
  expect(key("keydown", "AltRight")).toBe(true);
  expect(pushToTalkStore.snapshot().state).toBe("recording");
  key("keydown", "AltRight", "Alt", true);
  await flush();
  expect(screen.getByRole("status")).toHaveTextContent("正在听");
  const socket = FakeWebSocket.latest!;
  act(() => socket.receive({ type: "partial", text: "螺纹钢" }));
  expect(screen.getByRole("status")).toHaveTextContent("螺纹钢");
  await holdAndSay("螺纹钢买两手。", () => { expect(key("keyup", "AltRight")).toBe(true); });
  expect(onSend).toHaveBeenCalledWith("螺纹钢买两手。");
  expect(onFill).not.toHaveBeenCalled();
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  // The left Alt is not the hold key.
  key("keydown", "AltLeft");
  expect(pushToTalkStore.snapshot().state).toBe("idle");
});

test("策略引导卡在提问时，按住快捷键说的话作为回答发到卡片，不打开对话面板", async () => {
  const onSend = vi.fn(), onKeyPress = vi.fn(), answer = vi.fn();
  render(<Harness onSend={onSend} onKeyPress={onKeyPress} />);
  const release = setPttTarget(answer);
  key("keydown", "AltRight");
  await holdAndSay("五分钟均线金叉就开多", () => key("keyup", "AltRight"));
  expect(answer).toHaveBeenCalledWith("五分钟均线金叉就开多");
  expect(onSend).not.toHaveBeenCalled();
  expect(onKeyPress).not.toHaveBeenCalled();
  release();
  key("keydown", "AltRight");
  expect(onKeyPress).toHaveBeenCalledTimes(1);
});

test("策略引导卡在提问时，按住对话面板输入行的麦克风说的话仍发到对话，不当作卡片的回答（10-08）", async () => {
  const onSend = vi.fn(), answer = vi.fn();
  render(<Harness onSend={onSend} />);
  const release = setPttTarget(answer);
  const button = screen.getByRole("button", { name: "按住说话" });
  fireEvent.pointerDown(button, { button: 0, pointerId: 1 });
  await holdAndSay("帮我看看螺纹", () => fireEvent.pointerUp(button, { pointerId: 1 }));
  expect(onSend).toHaveBeenCalledWith("帮我看看螺纹");
  expect(answer).not.toHaveBeenCalled();
  release();
});

test("松开后识别期间：音量条换成依次起伏的等待动效（不再随音量）", async () => {
  render(<Harness />);
  key("keydown", "AltRight");
  await flush();
  act(() => { FakeWebSocket.latest!.open(); speak(0.2); });
  advance(600);
  const bars = () => Array.from(document.querySelectorAll<HTMLElement>(".fdw-ptt-meter i"));
  expect(bars().some(bar => bar.style.transform !== "scaleY(0.150)")).toBe(true); // following the voice
  key("keyup", "AltRight");
  expect(screen.getByRole("status")).toHaveAttribute("data-state", "transcribing");
  expect(bars().map(bar => [bar.style.transform, bar.style.animationDelay])).toEqual([0, 120, 240, 360, 480].map(ms => ["", `${ms}ms`]));
});

test("识别期间切换了对话：说的话回填输入框，不发到任何一个对话", async () => {
  const onSend = vi.fn(), onFill = vi.fn();
  const view = render(<Harness onSend={onSend} onFill={onFill} chatSessionId="chat-a" />);
  key("keydown", "AltRight");
  await holdAndSay("螺纹钢买两手。", () => {
    key("keyup", "AltRight");
    view.rerender(<Harness onSend={onSend} onFill={onFill} chatSessionId="chat-b" />);
  });
  expect(onSend).not.toHaveBeenCalled();
  expect(onFill).toHaveBeenCalledWith("螺纹钢买两手。");
});

test("松开后直接发送关闭时回填输入框", async () => {
  const onSend = vi.fn(), onFill = vi.fn();
  render(<Harness sendOnRelease={false} onSend={onSend} onFill={onFill} />);
  key("keydown", "AltRight");
  await holdAndSay("沪金多少钱", () => key("keyup", "AltRight"));
  expect(onFill).toHaveBeenCalledWith("沪金多少钱");
  expect(onSend).not.toHaveBeenCalled();
});

test("鼠标或触摸按住按钮说话：按下即进入录音态（按钮 aria-pressed，输入行换成音量条），不足 0.3 秒发 cancel 并提示太短", async () => {
  const onSend = vi.fn();
  render(<Harness onSend={onSend} />);
  const button = screen.getByRole("button", { name: "按住说话" });
  fireEvent.pointerDown(button, { button: 0, pointerId: 1 });
  // The recording look is there before any sound: the beating button and the live strip.
  expect(button).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("status")).toHaveAttribute("data-state", "recording");
  await flush();
  act(() => FakeWebSocket.latest!.open());
  advance(150);
  fireEvent.pointerUp(button, { pointerId: 1 });
  expect(FakeWebSocket.latest!.commands()).toEqual(["cancel"]);
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", notice: PTT_TOO_SHORT });

  fireEvent.pointerDown(button, { button: 0, pointerId: 2 });
  expect(button).toHaveAttribute("aria-pressed", "true");
  await holdAndSay("确认", () => fireEvent.pointerUp(button, { pointerId: 2 }));
  expect(onSend).toHaveBeenCalledWith("确认");
});

test("按住期间按 Esc 取消：不识别、不发送", async () => {
  const onSend = vi.fn();
  render(<Harness onSend={onSend} />);
  key("keydown", "AltRight");
  await flush();
  const socket = FakeWebSocket.latest!;
  act(() => socket.open());
  advance(800);
  key("keydown", "Escape", "Escape");
  expect(socket.commands()).toEqual(["cancel"]);
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  key("keyup", "AltRight");
  advance(10_000);
  expect(onSend).not.toHaveBeenCalled();
});

test("松开后识别期间按 Esc 也取消：迟到的结果不发送、不回填（#523）", async () => {
  const onSend = vi.fn(), onFill = vi.fn();
  render(<Harness onSend={onSend} onFill={onFill} />);
  key("keydown", "AltRight");
  await flush();
  const socket = FakeWebSocket.latest!;
  act(() => { socket.open(); speak(); });
  advance(600);
  key("keyup", "AltRight");
  advance(200);
  expect(pushToTalkStore.snapshot().state).toBe("transcribing");
  key("keydown", "Escape", "Escape");
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  expect(socket.commands()).toContain("cancel");
  act(() => socket.receive({ type: "final", text: "螺纹钢买两手。" }));
  expect(onSend).not.toHaveBeenCalled();
  expect(onFill).not.toHaveBeenCalled();
});

test("通话中按钮与快捷键都禁用，title 写原因；通话开始时正在按住的也取消", async () => {
  const view = render(<Harness />);
  key("keydown", "AltRight");
  await flush();
  expect(pushToTalkStore.snapshot().state).toBe("recording");
  view.rerender(<Harness blockedReason="通话中不能按住说话，先结束通话" />);
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  const button = screen.getByRole("button", { name: "按住说话" });
  expect(button).toBeDisabled();
  expect(button).toHaveAttribute("title", "通话中不能按住说话，先结束通话");
  const opened = FakeWebSocket.latest;
  key("keyup", "AltRight");
  key("keydown", "AltRight");
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  expect(FakeWebSocket.latest).toBe(opened);
});

const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={readQueryClient}>{children}</QueryClientProvider>;

/** The workspace's wiring in miniature: the hook lives above the dock and opens it on the hold key. */
function Workspace({ inCall = false, sendOnRelease = true, chat = {}, initiallyOpen = false }: {
  inCall?: boolean; sendOnRelease?: boolean; chat?: Record<string, any>; initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  const call = { status: { state: inCall ? "listening" : "idle" }, inCall, available: true, start: vi.fn(), stop: vi.fn(), mute: vi.fn(), retry: vi.fn() };
  const workspace = { connection: "connected", runtimeBaseUrl: "http://runtime", proposalActions: {}, account: undefined, chatSessionId: "s-1" };
  const chatState: Record<string, any> = { prompt: "", setPrompt: vi.fn(), promptError: "", promptSaveError: false, sendingPrompt: false, chatError: "", chatBlocked: false,
    selectChat: vi.fn(), toggleHistory: vi.fn(), submitPrompt: vi.fn(), sendQuickPrompt: vi.fn(async () => undefined), ...chat };
  const ptt = usePushToTalk({ baseUrl: "http://runtime", blockedReason: inCall ? "通话中不能按住说话，先结束通话" : "", sendOnRelease,
    onSend: text => void chatState.sendQuickPrompt(text), onFill: text => chatState.setPrompt(text), onKeyPress: () => setOpen(true) });
  // Loosely typed on purpose: the dock's other props are not what this test is about.
  const props: any = { workspace, chat: chatState, focus: { activeStrategy: "" }, appWorkbench: { activity: undefined, config: {}, error: "", updateConfig: vi.fn() },
    call, voiceUnavailableReason: "", onClose: vi.fn(), onPauseStrategy: async () => undefined,
    briefs: [], quote: { symbol: "SHFE.rb2610" }, decimals: 0, selectedPrice: undefined, currentPositions: [], decisionTurns: [],
    aiExecutionActive: false, apiSetupRequired: false, apiConnectionFailed: false, apiProviderName: "DeepSeek", view: "trade", sendKey: "enter",
    quickSkills: [], onQuickPrompt: vi.fn(), suggestions: { label: "K 线图", items: [] }, ptt };
  return open ? <AiDock {...props} /> : <div>图表</div>;
}

test("快捷键全局：面板没开时按下右 Alt，先开始录音再打开面板，按下时刻的音频不丢", async () => {
  let dockAtMicStart: boolean | undefined;
  installPttFakes(vi.fn(async () => {
    dockAtMicStart = Boolean(document.querySelector("[data-agent-target='panel:ai']"));
    return { getTracks: () => [{ stop: () => undefined }], getAudioTracks: () => [{ enabled: true }] };
  }));
  const sendQuickPrompt = vi.fn(async () => undefined);
  render(<Workspace chat={{ sendQuickPrompt }} />, { wrapper });
  expect(screen.queryByRole("button", { name: "按住说话" })).toBeNull();
  key("keydown", "AltRight");
  expect(dockAtMicStart).toBe(false); // the microphone opened in the key press, before the panel existed
  expect(screen.getByRole("status")).toHaveTextContent("正在听");
  await flush();
  act(() => { speak(); speak(); }); // captured before the socket opened: kept and sent once it does
  await holdAndSay("螺纹钢买两手。", () => key("keyup", "AltRight"));
  expect(FakeWebSocket.latest!.frames()).toBe(3);
  expect(sendQuickPrompt).toHaveBeenCalledWith("螺纹钢买两手。");
});

test("对话面板输入行：麦克风在发送旁；松开的文字走打字的同一发送入口，或回填输入框；没听清只提示；通话中禁用", async () => {
  const sendQuickPrompt = vi.fn(async () => undefined);
  const view = render(<Workspace initiallyOpen chat={{ sendQuickPrompt }} />, { wrapper });
  const mic = screen.getByRole("button", { name: "按住说话" });
  expect(mic.nextElementSibling).toBe(screen.getByRole("button", { name: "发送给AI" }));
  expect(mic.title).toContain("右");
  key("keydown", "AltRight");
  expect(screen.queryByRole("textbox", { name: "向 AI 决策流提问" })).toBeNull();
  await holdAndSay("螺纹钢买两手。", () => key("keyup", "AltRight"));
  expect(sendQuickPrompt).toHaveBeenCalledWith("螺纹钢买两手。");
  expect(screen.getByRole("textbox", { name: "向 AI 决策流提问" })).toBeInTheDocument();

  const setPrompt = vi.fn();
  view.rerender(<Workspace initiallyOpen sendOnRelease={false} chat={{ sendQuickPrompt, setPrompt }} />);
  key("keydown", "AltRight");
  await holdAndSay("沪金多少钱", () => key("keyup", "AltRight"));
  expect(sendQuickPrompt).toHaveBeenCalledTimes(1);
  expect(setPrompt).toHaveBeenCalledWith("沪金多少钱");

  // An empty final: the notice line, nothing sent.
  key("keydown", "AltRight");
  await flush();
  act(() => FakeWebSocket.latest!.open());
  advance(600);
  key("keyup", "AltRight");
  advance(200);
  act(() => FakeWebSocket.latest!.receive({ type: "final", text: "", reason: "timeout" }));
  expect(screen.getByText("没听清，再说一次")).toBeInTheDocument();
  expect(sendQuickPrompt).toHaveBeenCalledTimes(1);

  view.rerender(<Workspace initiallyOpen inCall chat={{ sendQuickPrompt }} />);
  expect(screen.getByRole("button", { name: "按住说话" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "按住说话" })).toHaveAttribute("title", "通话中不能按住说话，先结束通话");
  // The key while blocked records nothing and says why in the input row.
  key("keydown", "AltRight");
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  expect(screen.getByText("通话中不能按住说话，先结束通话", { selector: ".fdw-ptt-notice" })).toBeInTheDocument();
});
