import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { usePushToTalk } from "../futures/PushToTalk";
import { PTT_NOTICE_MS, pushToTalkStore } from "../../pushToTalk";
import { resetShortcutStore } from "../../shortcuts";
import { FakeWebSocket, installPttFakes, removePttFakes, speak } from "../../../tests/support/pttFakes";
import { useAiCommand } from "../futures/useAiCommand";
import { HoldToTalk, HoldToTalkLive, PTT_SLID_AWAY, PTT_TOO_SHORT } from "./HoldToTalk";
import { MobileWorkspace, type MobileTab } from "./MobileWorkspace";

vi.mock("../../frontendLog", () => ({ logDesktop: vi.fn() }));

function Harness({ blockedReason = "", onSend = vi.fn() }: { blockedReason?: string; onSend?: (text: string) => void }) {
  const ptt = usePushToTalk({ baseUrl: "http://runtime", blockedReason, sendOnRelease: true, onSend, onFill: vi.fn() });
  const [slide, setSlide] = useState(false);
  return <div>
    <HoldToTalkLive status={ptt.status} slideCancel={slide} />
    {ptt.status.notice ? <p data-testid="notice">{ptt.status.notice}</p> : null}
    <HoldToTalk ptt={ptt} onSlideCancelChange={setSlide} />
  </div>;
}

async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
function advance(ms: number) { act(() => { vi.advanceTimersByTime(ms); }); }
const button = () => screen.getByRole("button", { name: "按住说话，松开发送" });

// jsdom has no PointerEvent: without it the pointer events lose their coordinates.
class FakePointerEvent extends MouseEvent {
  pointerId: number;
  constructor(type: string, init: PointerEventInit = {}) { super(type, init); this.pointerId = init.pointerId ?? 0; }
}

beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); resetShortcutStore(); installPttFakes(); vi.stubGlobal("PointerEvent", FakePointerEvent); pushToTalkStore.reset(); });
afterEach(() => { act(() => pushToTalkStore.reset()); vi.useRealTimers(); removePttFakes(); });

test("手机按住说话：按下开始录音并显示声波提示，松开转写后作为一条消息发送", async () => {
  const onSend = vi.fn();
  render(<Harness onSend={onSend} />);
  expect(button()).toHaveClass("fdm-hold");
  fireEvent.pointerDown(button(), { button: 0, pointerId: 1, clientY: 700 });
  expect(pushToTalkStore.snapshot().state).toBe("recording");
  expect(screen.getByRole("status")).toHaveTextContent("正在听… 松开发送，上滑取消");
  expect(button()).toHaveAttribute("aria-pressed", "true");
  await flush();
  const socket = FakeWebSocket.latest!;
  act(() => { socket.open(); speak(); });
  advance(600);
  fireEvent.pointerUp(button(), { pointerId: 1, clientY: 690 });
  advance(200);
  expect(socket.commands()).toEqual(["end"]);
  act(() => socket.receive({ type: "final", text: "螺纹现在怎么看" }));
  expect(onSend).toHaveBeenCalledWith("螺纹现在怎么看");
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  expect(screen.queryByRole("status")).toBeNull();
});

test("按住时手指上滑超过阈值，松开即取消，不发送并提示已取消", async () => {
  const onSend = vi.fn();
  render(<Harness onSend={onSend} />);
  fireEvent.pointerDown(button(), { button: 0, pointerId: 1, clientY: 700 });
  await flush();
  const socket = FakeWebSocket.latest!;
  act(() => { socket.open(); speak(); });
  advance(600);
  fireEvent.pointerMove(button(), { pointerId: 1, clientY: 650 });
  expect(screen.getByRole("status")).toHaveTextContent("正在听");
  fireEvent.pointerMove(button(), { pointerId: 1, clientY: 620 });
  expect(screen.getByRole("status")).toHaveTextContent("松开手指，取消发送");
  expect(button()).toHaveClass("cancel");
  fireEvent.pointerUp(button(), { pointerId: 1, clientY: 620 });
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  expect(socket.commands()).toEqual(["cancel"]);
  expect(screen.getByTestId("notice")).toHaveTextContent(PTT_SLID_AWAY);
  advance(10_000);
  expect(onSend).not.toHaveBeenCalled();
});

test("按一下就松开（不到 0.3 秒）提示说话太短，不发送", async () => {
  const onSend = vi.fn();
  render(<Harness onSend={onSend} />);
  fireEvent.pointerDown(button(), { button: 0, pointerId: 1, clientY: 700 });
  await flush();
  advance(120);
  fireEvent.pointerUp(button(), { pointerId: 1, clientY: 700 });
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  expect(screen.getByTestId("notice")).toHaveTextContent(PTT_TOO_SHORT);
  expect(onSend).not.toHaveBeenCalled();
});

test("不能按住说话时（通话中）不录音并说明原因；麦克风被拒与识别失败都有提示", async () => {
  const { unmount } = render(<Harness blockedReason="通话中不能按住说话，先结束通话" />);
  expect(button()).toHaveAttribute("aria-disabled", "true");
  fireEvent.pointerDown(button(), { button: 0, pointerId: 1, clientY: 700 });
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  expect(screen.getByTestId("notice")).toHaveTextContent("通话中不能按住说话");
  // The notice goes like any other; the lasting reason stays on the button itself.
  advance(PTT_NOTICE_MS);
  expect(screen.queryByTestId("notice")).toBeNull();
  expect(button()).toHaveAttribute("aria-disabled", "true");
  expect(button()).toHaveAttribute("title", "通话中不能按住说话，先结束通话");
  fireEvent.pointerDown(button(), { button: 0, pointerId: 1, clientY: 700 });
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  unmount();
  act(() => pushToTalkStore.reset());

  removePttFakes();
  installPttFakes(vi.fn(async () => { throw new DOMException("denied", "NotAllowedError"); }));
  vi.stubGlobal("PointerEvent", FakePointerEvent);
  const denied = render(<Harness />);
  fireEvent.pointerDown(button(), { button: 0, pointerId: 1, clientY: 700 });
  await flush();
  expect(screen.getByTestId("notice")).toHaveTextContent("麦克风权限被拒绝");
  denied.unmount();
  act(() => pushToTalkStore.reset());

  removePttFakes();
  installPttFakes();
  vi.stubGlobal("PointerEvent", FakePointerEvent);
  const onSend = vi.fn();
  render(<Harness onSend={onSend} />);
  fireEvent.pointerDown(button(), { button: 0, pointerId: 1, clientY: 700 });
  await flush();
  const socket = FakeWebSocket.latest!;
  act(() => { socket.open(); speak(); });
  advance(600);
  fireEvent.pointerUp(button(), { pointerId: 1, clientY: 700 });
  advance(200);
  act(() => socket.receive({ type: "final", text: "", reason: "upstream_error" }));
  expect(screen.getByTestId("notice")).toHaveTextContent("语音识别暂时不可用");
  expect(onSend).not.toHaveBeenCalled();
});

function PhoneHarness({ onSend }: { onSend: (text: string, spoken?: boolean) => void }) {
  const [tab, setTab] = useState<MobileTab>("ai");
  const ptt = usePushToTalk({ baseUrl: "http://runtime", blockedReason: "", sendOnRelease: true, onSend: vi.fn(), onFill: vi.fn() });
  // As in the workspace (方案 B): the bar's key sends from the page on screen through the AI 指令栏's result.
  const command = useAiCommand({ baseUrl: "http://runtime", turns: [], busy: false, blockedReason: null, onSend: async (text, spoken) => onSend(text, spoken) });
  return <MobileWorkspace tab={tab} onTab={setTab} onOpenChat={() => setTab("chat")} onBell={vi.fn()} quote={undefined} decimals={0} feed={null} ptt={ptt} unread={0}
    command={command} context="今天" chat={<section className="fdm-chat">对话</section>} pages={{ research: null, market: null, strategy: null, me: null }} />;
}

const key = () => screen.getByRole("button", { name: "按住说话，点一下打字" });

test("中间的说话键连按四轮：留在今天页不跳对话，每轮都有声波、按哪一页理解和随识别更新的实时文字", async () => {
  const onSend = vi.fn();
  const { container } = render(<PhoneHarness onSend={onSend} />);
  for (const round of [1, 2, 3, 4]) {
    fireEvent.pointerDown(key(), { button: 0, pointerId: 1, clientY: 700 });
    await flush();
    const socket = FakeWebSocket.latest!;
    act(() => { socket.open(); speak(); });
    const live = () => container.querySelector<HTMLElement>('[data-agent-target="ptt-status"]');
    expect(live()).not.toBeNull();
    expect(live()!.closest("[hidden]")).toBeNull();
    expect(live()!.querySelector(".fdm-wave")!.children).toHaveLength(16);
    expect(live()).toHaveTextContent("按 今天 理解");
    act(() => socket.receive({ type: "partial", text: `第${round}轮螺纹` }));
    expect(live()!.querySelector(".fdm-live-text")).toHaveTextContent(`第${round}轮螺纹`);
    advance(600);
    fireEvent.pointerUp(key(), { pointerId: 1, clientY: 700 });
    advance(200);
    act(() => socket.receive({ type: "final", text: `第${round}轮螺纹怎么样` }));
    expect(live()).toBeNull();
    expect(container.querySelector(".fdm-body")).toHaveAttribute("data-mobile-tab", "ai");
  }
  expect(onSend.mock.calls).toEqual([1, 2, 3, 4].map(round => [`第${round}轮螺纹怎么样`, true]));
  // What came back shows above the bar; the open result names the line sent.
  expect(screen.getByRole("region", { name: "AI 指令结果" })).toHaveTextContent("第4轮螺纹怎么样");
});

test("点一下说话键：弹出打字框（写着按哪一页理解）且输入框获得焦点，发送后收起、不跳页", async () => {
  const onSend = vi.fn();
  const { container } = render(<PhoneHarness onSend={onSend} />);
  fireEvent.pointerDown(key(), { button: 0, pointerId: 1, clientY: 700 });
  await flush();
  advance(120);
  fireEvent.pointerUp(key(), { pointerId: 1, clientY: 700 });
  expect(pushToTalkStore.snapshot().state).toBe("idle");
  expect(container.querySelector(".fdm-talk-notice")).toBeNull();
  const input = screen.getByRole("textbox", { name: "按 今天 理解" });
  expect(input).toHaveFocus();
  fireEvent.change(input, { target: { value: "螺纹今天怎么看" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(onSend).toHaveBeenCalledWith("螺纹今天怎么看", false);
  expect(screen.queryByRole("textbox", { name: "按 今天 理解" })).toBeNull();
  expect(container.querySelector(".fdm-body")).toHaveAttribute("data-mobile-tab", "ai");
  // The result collapses to the 回执条; a tap on it opens the result again.
  fireEvent.click(screen.getByRole("button", { name: "收起 AI 指令结果" }));
  fireEvent.click(screen.getByRole("button", { name: /^AI 指令结果：/ }));
  expect(screen.getByRole("region", { name: "AI 指令结果" })).toBeVisible();
});

test("上滑取消的提示点一下立即消失，不点 3 秒后也消失", async () => {
  const { container } = render(<PhoneHarness onSend={vi.fn()} />);
  const notice = () => container.querySelector(".fdm-talk-notice");
  fireEvent.pointerDown(key(), { button: 0, pointerId: 1, clientY: 700 });
  await flush();
  fireEvent.pointerMove(key(), { pointerId: 1, clientY: 600 });
  fireEvent.pointerUp(key(), { pointerId: 1, clientY: 600 });
  expect(notice()).toHaveTextContent(PTT_SLID_AWAY);
  fireEvent.click(notice()!);
  expect(notice()).toBeNull();

  fireEvent.pointerDown(key(), { button: 0, pointerId: 1, clientY: 700 });
  await flush();
  fireEvent.pointerMove(key(), { pointerId: 1, clientY: 600 });
  fireEvent.pointerUp(key(), { pointerId: 1, clientY: 600 });
  expect(notice()).toHaveTextContent(PTT_SLID_AWAY);
  advance(PTT_NOTICE_MS);
  expect(notice()).toBeNull();
});
