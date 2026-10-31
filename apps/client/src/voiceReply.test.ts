import { pushToTalkStore } from "./pushToTalk";
import { voiceReply } from "./voiceReply";
import { FakeAudioContext, FakeWebSocket, installPttFakes, removePttFakes, speak } from "../tests/support/pttFakes";

vi.mock("./api", () => ({ localAuthTokenFor: () => "local-token", requestJson: vi.fn() }));
vi.mock("./frontendLog", () => ({ logDesktop: vi.fn() }));

beforeEach(() => { installPttFakes(); pushToTalkStore.reset(); });
afterEach(() => { voiceReply.stop(); pushToTalkStore.reset(); vi.useRealTimers(); removePttFakes(); });

const audioFrame = () => btoa(String.fromCharCode(...new Uint8Array(new Float32Array([0.1, 0.2, 0.1, 0]).buffer)));

/** One hold up to the final text, the way the workspace sends it: `expect()` runs in `onFinal`,
 *  long after the press gesture. Returns the transcribe socket. */
async function holdUntilFinal(onFinal: (text: string) => void) {
  vi.useFakeTimers();
  await pushToTalkStore.press("http://runtime", onFinal);
  const socket = FakeWebSocket.latest!;
  socket.open(); speak();
  vi.advanceTimersByTime(800);
  pushToTalkStore.release();
  vi.advanceTimersByTime(200);
  vi.advanceTimersByTime(1500); // the final comes back seconds later, outside the gesture
  FakeAudioContext.latest!.state = "suspended";
  socket.receive({ type: "final", text: "螺纹现在怎么样" });
  vi.useRealTimers();
}

test("按住说话的回复用按下手势里已解锁的 AudioContext：final 晚到再 expect() 也不新建，收到音频帧就播放", async () => {
  await holdUntilFinal(() => voiceReply.expect("http://runtime", []));
  const context = FakeAudioContext.latest!;
  expect(FakeAudioContext.created).toBe(1); // no context made outside the gesture
  expect(context.resumeCalls).toBe(2); // the press, then expect() waking it from Safari's park
  expect(context.state).toBe("running");
  const socket = FakeWebSocket.latest!;
  expect(socket.url).toBe("ws://runtime/api/voice/speak");
  socket.open();
  voiceReply.offer([{ id: "t1", role: "assistant", origin: "voice", spoken: "螺纹 3100。" }]);
  const start = vi.fn();
  const createSource = vi.spyOn(context, "createBufferSource").mockImplementation(() => Object.assign(FakeAudioContext.prototype.createBufferSource.call(context), { start }));
  socket.receive({ type: "audio", data: audioFrame(), encoding: "f32le" });
  socket.receive({ type: "audio", data: audioFrame(), encoding: "f32le" });
  expect(start).toHaveBeenCalledTimes(2);
  createSource.mockRestore();
});

test("按住说话的回复：发出时先连上朗读通道，第一条新的语音回答念它的口语摘要，只念一次", () => {
  voiceReply.expect("http://runtime", ["old-answer"]);
  const socket = FakeWebSocket.latest!;
  expect(socket.url).toBe("ws://runtime/api/voice/speak");
  expect(socket.protocols).toEqual(["ai-trader-token", "bearer.local-token"]);
  const turns = [
    { id: "old-answer", role: "assistant", origin: "voice", spoken: "上一句。" },
    { id: "t1-user", role: "user", origin: "voice", spoken: null },
    { id: "t1", role: "assistant", origin: "voice", spoken: "" }
  ];
  voiceReply.offer(turns); // the answer is still streaming: nothing to read yet
  expect(socket.sent).toEqual([]);
  voiceReply.offer([...turns.slice(0, 2), { id: "t1", role: "assistant", origin: "voice", spoken: "螺纹现在 3100。" }]);
  expect(socket.sent).toEqual([]); // queued until the socket is open
  socket.open();
  expect(socket.sent.map(item => JSON.parse(item as string))).toEqual([{ type: "speak", text: "螺纹现在 3100。" }]);
  expect(voiceReply.waiting).toBe(false);
  voiceReply.offer([{ id: "t2", role: "assistant", origin: "voice", spoken: "又一句。" }]);
  expect(socket.sent).toHaveLength(1);
});

test("新的按住或通话开始时停止朗读并取消", () => {
  voiceReply.expect("http://runtime", []);
  const socket = FakeWebSocket.latest!;
  socket.open();
  voiceReply.stop();
  expect(socket.commands()).toEqual(["cancel"]);
  expect(socket.closed).toBe(true);
  expect(voiceReply.waiting).toBe(false);
});

test("读回复经过增益（1.6）和限幅再到扬声器，并把音频会话切回播放", async () => {
  const types: string[] = [];
  Object.defineProperty(navigator, "audioSession", { configurable: true, value: { set type(value: string) { types.push(value); } } });
  const createSource = vi.spyOn(FakeAudioContext.prototype, "createBufferSource");
  try {
    await holdUntilFinal(() => undefined);
    types.length = 0;
    voiceReply.expect("http://runtime", []);
    expect(types).toEqual(["playback"]);
    const socket = FakeWebSocket.latest!;
    socket.open();
    voiceReply.offer([{ id: "t1", role: "assistant", origin: "voice", spoken: "好的。" }]);
    const samples = new Uint8Array(new Float32Array([0.1, 0.2, 0.1, 0]).buffer);
    socket.receive({ type: "audio", data: btoa(String.fromCharCode(...samples)), encoding: "f32le" });
    const source = createSource.mock.results[0].value as { targets: { kind: string; gain?: { value: number }; targets: { kind: string; targets: { kind: string }[] }[] }[] };
    const [gain] = source.targets;
    expect(gain).toMatchObject({ kind: "gain", gain: { value: 1.6 } });
    expect(gain.targets.map(node => node.kind)).toEqual(["compressor"]);
    expect(gain.targets[0].targets.map(node => node.kind)).toEqual(["destination"]);
  } finally { createSource.mockRestore(); Reflect.deleteProperty(navigator, "audioSession"); }
});
