import { PTT_MIC_SILENT, PTT_NOT_HEARD, PTT_NOTICE_MS, PTT_TOO_SHORT, pushToTalkStore } from "./pushToTalk";
import { FakeAudioContext, FakeWebSocket, installPttFakes, microphoneTracks, removePttFakes, speak } from "../tests/support/pttFakes";

vi.mock("./frontendLog", () => ({ logDesktop: vi.fn() }));
vi.mock("./api", () => ({ localAuthTokenFor: () => "local-token", requestJson: vi.fn() }));

beforeEach(() => { vi.useFakeTimers(); installPttFakes(); pushToTalkStore.reset(); });
afterEach(() => {
  pushToTalkStore.reset();
  vi.useRealTimers();
  removePttFakes();
});

test("遥控器 PCM 走既有认证转写与尾音；不打开电脑麦克风，取消后可再次收音", async () => {
  let frame: ((pcm: Int16Array) => void) | undefined;
  const stop = vi.fn();
  const input = (onFrame: (pcm: Int16Array) => void) => ({ start: async () => { frame = onFrame; }, stop });
  const finals = vi.fn();
  await pushToTalkStore.press("http://runtime", finals, false, input);
  expect(microphoneTracks).toHaveLength(0);
  const id = pushToTalkStore.captureId();
  frame!(new Int16Array([100, -100]));
  const first = FakeWebSocket.latest!; first.open(); expect(first.frames()).toBe(1);
  expect(first.protocols).toEqual(["ai-trader-token", "bearer.local-token"]);
  pushToTalkStore.cancel(); first.receive({ type: "final", text: "取消的文字" });
  expect(finals).not.toHaveBeenCalled(); expect(stop).toHaveBeenCalled();
  await pushToTalkStore.press("http://runtime", finals, false, input);
  expect(pushToTalkStore.captureId()).not.toBe(id);
  const second = FakeWebSocket.latest!; second.open(); frame!(new Int16Array([200, -200]));
  vi.advanceTimersByTime(600); pushToTalkStore.release(); vi.advanceTimersByTime(200);
  expect(second.commands()).toEqual(["end"]);
  second.receive({ type: "final", text: "第二段" }); expect(finals).toHaveBeenCalledWith("第二段");
  expect(microphoneTracks).toHaveLength(0);
});

test("按住→录音（帧先排队、连上后按序发出、partial 实时显示）→松开后再送 0.2 s→end→final 交给发送", async () => {
  const finals: string[] = [];
  await pushToTalkStore.press("http://runtime", text => finals.push(text));
  expect(pushToTalkStore.snapshot().state).toBe("recording");
  const socket = FakeWebSocket.latest!;
  expect(socket.url).toBe("ws://runtime/api/voice/transcribe");
  expect(socket.protocols).toEqual(["ai-trader-token", "bearer.local-token"]);
  speak(); speak(); // captured before the socket opened: queued, not lost
  expect(socket.frames()).toBe(0);
  socket.open();
  expect(socket.frames()).toBe(2);
  speak();
  expect(socket.frames()).toBe(3);
  socket.receive({ type: "partial", text: "螺纹钢" });
  expect(pushToTalkStore.snapshot().partial).toBe("螺纹钢");

  vi.advanceTimersByTime(800);
  pushToTalkStore.release();
  expect(pushToTalkStore.snapshot().state).toBe("transcribing");
  speak(); // the 0.2 s tail still goes up
  expect(socket.commands()).toEqual([]);
  vi.advanceTimersByTime(200);
  expect(socket.frames()).toBe(4);
  expect(socket.commands()).toEqual(["end"]);
  // After `end` the microphone is closed; the AudioContext stays for the next hold.
  expect(microphoneTracks.map(track => track.stopped)).toEqual([true]);
  expect(FakeAudioContext.latest!.processors[0].targets).toEqual([]);
  expect(FakeAudioContext.latest!.closed).toBe(false);

  socket.receive({ type: "final", text: "螺纹钢买两手。" });
  expect(finals).toEqual(["螺纹钢买两手。"]);
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", partial: "", notice: undefined });
  expect(socket.closed).toBe(true);
});

test("按下不足 0.3 秒：发 cancel、不发送，提示说话太短", async () => {
  const finals: string[] = [];
  await pushToTalkStore.press("http://runtime", text => finals.push(text));
  const socket = FakeWebSocket.latest!;
  socket.open();
  vi.advanceTimersByTime(200);
  pushToTalkStore.release();
  expect(socket.commands()).toEqual(["cancel"]);
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", notice: PTT_TOO_SHORT });
  vi.advanceTimersByTime(10_000);
  expect(finals).toEqual([]);
  expect(socket.closed).toBe(true);
});

test("final 为空（超时或没听清）只提示“没听清，再说一次”，不发送", async () => {
  const finals: string[] = [];
  await pushToTalkStore.press("http://runtime", text => finals.push(text));
  const socket = FakeWebSocket.latest!;
  socket.open();
  vi.advanceTimersByTime(600);
  pushToTalkStore.release();
  vi.advanceTimersByTime(200);
  socket.receive({ type: "final", text: "", reason: "timeout" });
  expect(finals).toEqual([]);
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", notice: PTT_NOT_HEARD });
  // The Runtime never answering at all ends the same way (the client's own backstop).
  await pushToTalkStore.press("http://runtime", text => finals.push(text));
  expect(pushToTalkStore.snapshot().notice).toBeUndefined();
  FakeWebSocket.latest!.open();
  vi.advanceTimersByTime(600);
  pushToTalkStore.release();
  vi.advanceTimersByTime(10_000);
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", notice: PTT_NOT_HEARD });
  expect(finals).toEqual([]);
  // A silent hold (09-25): the Runtime answers `no_speech` shortly after the release — same notice.
  await pushToTalkStore.press("http://runtime", text => finals.push(text));
  const silent = FakeWebSocket.latest!;
  silent.open();
  vi.advanceTimersByTime(600);
  pushToTalkStore.release();
  vi.advanceTimersByTime(200);
  silent.receive({ type: "final", text: "", reason: "no_speech" });
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", notice: PTT_NOT_HEARD });
  expect(finals).toEqual([]);
});

test("麦克风权限失败复用通话的中文说明，不开语音通道", async () => {
  installPttFakes(vi.fn(async () => { throw new DOMException("denied", "NotAllowedError"); }));
  await pushToTalkStore.press("http://runtime", vi.fn());
  expect(FakeWebSocket.latest).toBeUndefined();
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", notice: expect.stringContaining("麦克风权限被拒绝") });
});

test("Runtime 拒绝（通话进行中 4432）时在输入行说明原因", async () => {
  await pushToTalkStore.press("http://runtime", vi.fn());
  FakeWebSocket.latest!.onclose?.({ code: 4432 });
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", notice: "通话中不能按住说话，先结束通话" });
});

test("游客转写走独立cookie路由，不复用普通账户bearer", async () => {
  await pushToTalkStore.press("https://runtime", vi.fn(), true);
  const socket = FakeWebSocket.latest!;
  expect(socket.url).toBe("wss://runtime/api/guest/voice/transcribe");
  expect(socket.protocols).toBeUndefined();
});

test("桌面游客转写仅带guest bearer子协议，不带owner bearer", async () => {
  const { setGuestAccessToken } = await import("./guestApi");
  setGuestAccessToken("https://runtime", "guest.test-opaque");
  try {
    await pushToTalkStore.press("https://runtime", vi.fn(), true);
    expect(FakeWebSocket.latest!.protocols).toEqual(["ai-trader-token", "bearer.guest.test-opaque"]);
  } finally { setGuestAccessToken("https://runtime"); }
});

test("连续三轮按住（手机第二轮起曾卡住）：AudioContext 只建一次、每轮在按下的同步段内 resume，每轮都有帧、音量和实时文字", async () => {
  const resumedAtPermission: number[] = [];
  installPttFakes(vi.fn(async () => {
    resumedAtPermission.push(FakeAudioContext.latest?.resumeCalls ?? 0);
    return { getTracks: () => [{ stop: () => undefined }], getAudioTracks: () => [] };
  }));
  const finals: string[] = [];
  for (const round of [1, 2, 3]) {
    const pressed = pushToTalkStore.press("http://runtime", text => finals.push(text));
    // Still the synchronous part of the press, i.e. inside the gesture.
    expect(FakeAudioContext.created).toBe(1);
    expect(FakeAudioContext.latest!.resumeCalls).toBe(round);
    await pressed;
    const socket = FakeWebSocket.latest!;
    socket.open();
    vi.advanceTimersByTime(150);
    speak();
    expect(socket.frames()).toBe(1);
    expect(pushToTalkStore.snapshot().level).toBeGreaterThan(0);
    socket.receive({ type: "partial", text: `第${round}句` });
    expect(pushToTalkStore.snapshot()).toMatchObject({ state: "recording", partial: `第${round}句`, notice: undefined });
    vi.advanceTimersByTime(800);
    pushToTalkStore.release();
    vi.advanceTimersByTime(200);
    expect(socket.commands()).toEqual(["end"]);
    socket.receive({ type: "final", text: `第${round}句。` });
    FakeAudioContext.latest!.state = "suspended"; // a phone parks the idle context between holds
  }
  expect(finals).toEqual(["第1句。", "第2句。", "第3句。"]);
  // resume() was already called before the first await (the permission request) of every hold.
  expect(resumedAtPermission).toEqual([1, 2, 3]);
  expect(FakeAudioContext.created).toBe(1);
  expect(FakeAudioContext.latest!.closed).toBe(false);
});

test("麦克风就绪后一秒没有任何声音帧：按住时就提示松开重试并再试 resume；声音来了提示消失", async () => {
  await pushToTalkStore.press("http://runtime", vi.fn());
  const context = FakeAudioContext.latest!;
  FakeWebSocket.latest!.open();
  vi.advanceTimersByTime(999);
  expect(pushToTalkStore.snapshot().notice).toBeUndefined();
  vi.advanceTimersByTime(1);
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "recording", notice: PTT_MIC_SILENT });
  expect(context.resumeCalls).toBe(2);
  speak();
  expect(pushToTalkStore.snapshot().notice).toBeUndefined();
});

// 10-05: notices stayed until the page was reloaded. They now go PTT_NOTICE_MS after the hold is over;
// a recording's own notice (麦克风没有声音) stays while the finger is still down.
test("提示在按住结束后 3 秒自动消失；录音中的“麦克风没有声音”按住期间不消失", async () => {
  expect(PTT_NOTICE_MS).toBe(3000);
  await pushToTalkStore.press("http://runtime", vi.fn());
  const socket = FakeWebSocket.latest!;
  socket.open();
  vi.advanceTimersByTime(1000 + 5000);
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "recording", notice: PTT_MIC_SILENT });
  pushToTalkStore.release();
  vi.advanceTimersByTime(200);
  socket.receive({ type: "final", text: "" });
  expect(pushToTalkStore.snapshot()).toMatchObject({ state: "idle", notice: PTT_NOT_HEARD });
  vi.advanceTimersByTime(PTT_NOTICE_MS - 1);
  expect(pushToTalkStore.snapshot().notice).toBe(PTT_NOT_HEARD);
  vi.advanceTimersByTime(1);
  expect(pushToTalkStore.snapshot().notice).toBeUndefined();

  // A newer notice restarts the count; dismiss clears at once and the old timer does nothing later.
  pushToTalkStore.showNotice("已取消，这句没有发送");
  vi.advanceTimersByTime(2000);
  pushToTalkStore.showNotice("说话太短，按住说完再松开");
  vi.advanceTimersByTime(2000);
  expect(pushToTalkStore.snapshot().notice).toBe("说话太短，按住说完再松开");
  vi.advanceTimersByTime(1000);
  expect(pushToTalkStore.snapshot().notice).toBeUndefined();
});

test("音频会话：按下切到录放，松开后（读回复前）切回播放，外放音量不被压成听筒级", async () => {
  const types: string[] = [];
  Object.defineProperty(navigator, "audioSession", { configurable: true, value: { set type(value: string) { types.push(value); } } });
  try {
    const pressed = pushToTalkStore.press("http://runtime", vi.fn());
    expect(types).toEqual(["play-and-record"]);
    await pressed;
    FakeWebSocket.latest!.open();
    vi.advanceTimersByTime(600);
    pushToTalkStore.release();
    expect(types).toEqual(["play-and-record"]); // the 0.2 s tail still records
    vi.advanceTimersByTime(200);
    expect(types).toEqual(["play-and-record", "playback"]);
    FakeWebSocket.latest!.receive({ type: "final", text: "好" });
    expect(types).toHaveLength(2); // the microphone was already given back
    // A slip or cancel also gives the speaker back.
    await pushToTalkStore.press("http://runtime", vi.fn());
    pushToTalkStore.cancel();
    expect(types.slice(2)).toEqual(["play-and-record", "playback"]);
  } finally { Reflect.deleteProperty(navigator, "audioSession"); }
});
