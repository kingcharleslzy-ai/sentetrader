import { MIC_SILENT_MS, VoiceCall, decodeOutputAudio, orbLabel, voiceCallStore } from "./voiceRealtime";
import { authenticateTqkq, signOutOwner } from "./api";
import { exportDiagnostics } from "./dataExport";
import { logDesktop } from "./frontendLog";

vi.mock("./frontendLog", () => ({ logDesktop: vi.fn() }));
vi.mock("./nativeAgent", async importOriginal => ({ ...await importOriginal<typeof import("./nativeAgent")>(), activeAgentWindow: () => "window-1" }));

test("Doubao output frames are float32 little-endian; int16 stays available when the Runtime says so", () => {
  const f32 = new Float32Array([0, 0.25, -0.5, 1.5]);
  const bytes = new Uint8Array(f32.buffer);
  expect(Array.from(decodeOutputAudio(bytes))).toEqual([0, 0.25, -0.5, 1]);
  // A trailing partial sample is dropped rather than shifting every later sample.
  expect(decodeOutputAudio(bytes.subarray(0, 14))).toHaveLength(3);
  const s16 = new Int16Array([0, 16384, -32768]);
  expect(Array.from(decodeOutputAudio(new Uint8Array(s16.buffer), "s16le"))).toEqual([0, 0.5, -1]);
  // The bug: 4-byte floats read as int16 give two noise samples per real one.
  expect(decodeOutputAudio(bytes, "s16le")).toHaveLength(8);
});

/** A WebAudio stand-in: currentTime is advanced by hand, sources record their start time. */
class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  static latest: FakeAudioContext | undefined;
  currentTime = 0;
  destination = {} as AudioDestinationNode;
  sources: { buffer: { duration: number; sampleRate: number } | null; start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn>; onended: (() => void) | null }[] = [];
  gains: { gain: { value: number }; connect: ReturnType<typeof vi.fn> }[] = [];
  processors: { connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn>; onaudioprocess: ((event: { inputBuffer: { getChannelData: () => Float32Array; sampleRate: number } }) => void) | null }[] = [];
  constructor() { FakeAudioContext.instances.push(this); FakeAudioContext.latest = this; }
  async resume() { return undefined; }
  async close() { return undefined; }
  createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }
  createGain() { const gain = { gain: { value: 1 }, connect: vi.fn() }; this.gains.push(gain); return gain; }
  createAnalyser() { return { fftSize: 2048, connect: vi.fn(), disconnect: vi.fn() }; }
  createScriptProcessor() {
    const processor = { connect: vi.fn(), disconnect: vi.fn(), onaudioprocess: null as ((event: { inputBuffer: { getChannelData: () => Float32Array; sampleRate: number } }) => void) | null };
    this.processors.push(processor);
    return processor;
  }
  createBuffer(_channels: number, length: number, sampleRate: number) {
    return { duration: length / sampleRate, sampleRate, getChannelData: () => new Float32Array(length) };
  }
  createBufferSource() {
    const source = { buffer: null as { duration: number; sampleRate: number } | null,
      connect: vi.fn(), start: vi.fn(), stop: vi.fn(), onended: null as (() => void) | null };
    this.sources.push(source);
    return source;
  }
}

class FakeWebSocket {
  static OPEN = 1;
  static latest: FakeWebSocket | undefined;
  binaryType = "";
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn();
  addEventListener() { /* only the close-reject watcher registers; the fake never closes early */ }
  constructor() { FakeWebSocket.latest = this; }
  close() { this.onclose?.(); }
}

/** `count` silent samples as an f32le audio frame the socket would deliver. */
function audioFrame(count: number) {
  const bytes = new Uint8Array(new Float32Array(count).buffer);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return JSON.stringify({ type: "audio", data: btoa(binary) });
}

async function connectedCall() {
  const call = new VoiceCall("http://runtime");
  const starting = call.start();
  await vi.waitFor(() => expect(FakeWebSocket.latest).toBeDefined());
  FakeWebSocket.latest!.onopen?.();
  await starting;
  return call;
}

function receive(payload: string) { FakeWebSocket.latest!.onmessage!({ data: payload }); }

beforeEach(() => {
  vi.mocked(logDesktop).mockClear();
  FakeAudioContext.instances = [];
  FakeAudioContext.latest = undefined;
  FakeWebSocket.latest = undefined;
  Object.defineProperty(navigator, "mediaDevices",
    { value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: vi.fn() }], getAudioTracks: () => [{ enabled: true }] })) }, configurable: true });
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("WebSocket", FakeWebSocket);
});
afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, "mediaDevices", { value: undefined, configurable: true });
});

test("通话只用一个 AudioContext：捕获与播放共用，输出帧仍按 24 kHz 标注", async () => {
  const call = await connectedCall();
  receive(audioFrame(2880)); // ≥120 ms alone: the preroll bound is met at once.
  const context = FakeAudioContext.latest!;
  expect(FakeAudioContext.instances).toHaveLength(1);
  expect(context.sources).toHaveLength(1);
  expect(context.sources[0].buffer!.sampleRate).toBe(24000);
  expect(context.sources[0].start).toHaveBeenCalledWith(context.currentTime + 0.05);
  call.stop();
});

test("起播缓冲：前 120 ms 只累积不起播，满后按 playhead 连续调度不留缝", async () => {
  const call = await connectedCall();
  const context = FakeAudioContext.latest!;
  // 5 × 20 ms = 100 ms（< 120 ms 也 < 6 帧）：全部进缓冲，一个 source 都不创建。
  for (let i = 0; i < 5; i += 1) receive(audioFrame(480));
  expect(context.sources).toHaveLength(0);
  // 第 6 帧达到 6 帧边界：整段从 currentTime+0.05 连续调度。
  receive(audioFrame(480));
  expect(context.sources).toHaveLength(6);
  const starts = context.sources.map(source => source.start.mock.calls[0][0] as number);
  expect(starts[0]).toBe(0.05);
  // 第二帧起播 == 第一帧起播 + 第一帧时长：帧与帧无缝。
  expect(starts[1]).toBeCloseTo(starts[0] + context.sources[0].buffer!.duration, 10);
  expect(starts[5]).toBeCloseTo(starts[0] + 5 * context.sources[0].buffer!.duration, 10);
  // 之后的帧接在 playhead 上，不再各自从 currentTime+0.02 起播。
  receive(audioFrame(480));
  expect(context.sources[5 + 1].start.mock.calls[0][0]).toBeCloseTo(starts[0] + 6 * 0.02, 10);
  call.stop();
});

test("欠载：playhead 落后 currentTime 时重对齐到 +0.1s，每个回复最多记一次 underrun", async () => {
  const call = await connectedCall();
  const context = FakeAudioContext.latest!;
  receive(audioFrame(2880)); // playhead = 0.05 + 0.12 = 0.17
  // 网络抖动：时间已经走过了 playhead。
  context.currentTime = 1;
  receive(audioFrame(480));
  expect(context.sources[1].start.mock.calls[0][0]).toBeCloseTo(1.1, 10);
  const underruns = () => vi.mocked(logDesktop).mock.calls.filter(([kind, message]) => kind === "audio" && message.startsWith("underrun"));
  expect(underruns()).toHaveLength(1);
  // 再次落后：照样重对齐，但不再重复记。
  context.currentTime = 2;
  receive(audioFrame(480));
  expect(context.sources[2].start.mock.calls[0][0]).toBeCloseTo(2.1, 10);
  expect(underruns()).toHaveLength(1);
  call.stop();
});

test("打断清空起播缓冲并归零 playhead，下一回复重新走起播缓冲", async () => {
  const call = await connectedCall();
  const context = FakeAudioContext.latest!;
  receive(audioFrame(2880));
  call.interrupt();
  expect(context.sources[0].stop).toHaveBeenCalled();
  // 打断后到达的帧重新累积，不立刻起播。
  receive(audioFrame(480));
  expect(context.sources).toHaveLength(1);
  call.stop();
});

/** Three loud mic frames (RMS > 0.04) while audio is queued — what a knock on the desk looks like. */
function loudMicBurst(context: FakeAudioContext) {
  const loud = new Float32Array(1024).fill(0.3);
  for (let i = 0; i < 3; i += 1)
    context.processors[0].onaudioprocess!({ inputBuffer: { getChannelData: () => loud, sampleRate: 16000 } });
}

test("杂音不打断：本地音量只把增益压到 0.15，700 ms 服务端未确认就恢复，队列未清、不发 cancel", async () => {
  const call = await connectedCall();
  const context = FakeAudioContext.latest!;
  receive(audioFrame(2880)); // 起播：队列里有一个 source
  const gain = context.gains[0];
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  loudMicBurst(context);
  expect(gain.gain.value).toBeCloseTo(0.15, 10);
  expect(context.sources[0].stop).not.toHaveBeenCalled(); // 队列未清，仍在播
  expect(FakeWebSocket.latest!.send).not.toHaveBeenCalledWith('{"type":"cancel"}');
  vi.advanceTimersByTime(700); // 服务端始终没有确认：恢复音量继续播
  expect(gain.gain.value).toBe(1);
  expect(context.sources).toHaveLength(1);
  vi.useRealTimers();
  call.stop();
});

test("服务端首字确认的打断：interrupt 消息立即清空队列并恢复增益", async () => {
  const call = await connectedCall();
  const context = FakeAudioContext.latest!;
  receive(audioFrame(2880));
  const gain = context.gains[0];
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  loudMicBurst(context);
  expect(gain.gain.value).toBeCloseTo(0.15, 10);
  receive(JSON.stringify({ type: "interrupt" })); // 服务端识别出了人声首字
  expect(context.sources[0].stop).toHaveBeenCalled();
  expect(gain.gain.value).toBe(1);
  expect(FakeWebSocket.latest!.send).not.toHaveBeenCalledWith('{"type":"cancel"}'); // 客户端不再代发 cancel
  vi.useRealTimers();
  call.stop();
});

test("多轮通话：回复播放、通话栏波形旁听、服务端打断之后，麦克风帧照常上行，第 N 句与第一句一样", async () => {
  const call = await connectedCall();
  const context = FakeAudioContext.latest!;
  const socket = FakeWebSocket.latest!;
  const frames = () => socket.send.mock.calls.filter(([item]) => typeof item !== "string").length;
  const say = () => context.processors[0].onaudioprocess!({ inputBuffer: { getChannelData: () => new Float32Array(960).fill(0.1), sampleRate: 16000 } });
  for (let turn = 1; turn <= 3; turn += 1) {
    const before = frames();
    say();
    expect(frames() - before).toBe(3); // 60 ms → three 20 ms frames, every turn
    receive(JSON.stringify({ type: "transcript", text: `第${turn}句`, final: true }));
    receive(JSON.stringify({ type: "status", state: "speaking" }));
    receive(audioFrame(2880));
    expect(call.analysers().input).toBeDefined(); // the call bar's taps only listen
    say(); // talking over the answer still goes up
    receive(JSON.stringify({ type: turn === 2 ? "interrupt" : "audio_end" }));
    context.sources.forEach(source => source.onended?.());
    receive(JSON.stringify({ type: "status", state: "listening" }));
  }
  expect(FakeAudioContext.instances).toHaveLength(1);
  expect(context.processors).toHaveLength(1);
  expect(context.processors[0].disconnect).not.toHaveBeenCalled();
  call.stop();
  expect(context.processors[0].disconnect).toHaveBeenCalled();
});

test("短回复：audio_end 到达时起播缓冲里不足 120 ms 的帧立即起播，不留到下一句（#523）", async () => {
  const call = await connectedCall();
  const context = FakeAudioContext.latest!;
  for (let i = 0; i < 3; i += 1) receive(audioFrame(480));
  expect(context.sources).toHaveLength(0);
  receive(JSON.stringify({ type: "audio_end" }));
  expect(context.sources).toHaveLength(3);
  expect(context.sources[0].start.mock.calls[0][0]).toBe(0.05);
  call.stop();
});

test("通话被 Runtime 结束（closed 后断开、或报错后断开）：再点能重新接通，诊断导出是这一通（#523 / #875）", async () => {
  voiceCallStore.reset();
  const open = async () => {
    FakeWebSocket.latest = undefined;
    const starting = voiceCallStore.start("http://runtime", "chat-1");
    await vi.waitFor(() => expect(FakeWebSocket.latest).toBeDefined());
    FakeWebSocket.latest!.onopen?.();
    await starting;
    return FakeWebSocket.latest!;
  };
  const first = await open();
  receive(JSON.stringify({ type: "transcript", text: "第一通的话" }));
  receive(JSON.stringify({ type: "closed", durationSeconds: 3, turns: 1, usage: { total_tokens: 1 }, latency: [] }));
  first.close();
  expect(voiceCallStore.snapshot().state).toBe("idle");
  expect(voiceCallStore.lastTimeline().some(event => event.type === "transcript" && event.text === "第一通的话")).toBe(true);
  const second = await open();
  expect(second).not.toBe(first);
  receive(JSON.stringify({ type: "error", error: { message: "语音通道没有响应，请稍后重拨。" } }));
  second.close();
  expect(voiceCallStore.snapshot().state).toBe("error");
  expect(voiceCallStore.lastTimeline().some(event => event.type === "error")).toBe(true);
  const third = await open();
  expect(third).not.toBe(second);
  voiceCallStore.stop();
});

test("语音复盘：未通话时接通并在 hello 里带 intent=review；通话中再按只发一条 hello 让 Runtime 开始；挂断后清掉", async () => {
  voiceCallStore.reset();
  const opening = voiceCallStore.review("http://runtime", "chat-1");
  await vi.waitFor(() => expect(FakeWebSocket.latest).toBeDefined());
  FakeWebSocket.latest!.onopen?.();
  await opening;
  const sent = () => FakeWebSocket.latest!.send.mock.calls.map(([payload]) => typeof payload === "string" ? JSON.parse(payload) : null).filter(Boolean);
  expect(sent()).toContainEqual({ type: "hello", windowId: "window-1", sessionId: "chat-1", intent: "review" });
  receive(JSON.stringify({ type: "status", state: "listening" }));
  expect(voiceCallStore.snapshot().review).toBe(true);
  await voiceCallStore.review("http://runtime", "chat-1");
  expect(sent().filter(item => item.intent === "review")).toHaveLength(2);
  voiceCallStore.stop();
  expect(voiceCallStore.snapshot().review).toBe(false);
});

/** A microphone whose permission prompt is still open until `grant()`. */
function pendingMicrophone() {
  const track = { enabled: true, stop: vi.fn() };
  let grant: () => void = () => undefined;
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: vi.fn(() => new Promise(resolve => { grant = () => resolve(stream); })) }, configurable: true });
  return { track, grant: () => grant() };
}

test("授权弹窗等待时点了结束：授权后释放麦克风、不建立通话、状态回到空闲", async () => {
  const mic = pendingMicrophone();
  voiceCallStore.reset();
  const starting = voiceCallStore.start("http://runtime", "chat-1");
  await vi.waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalled());
  voiceCallStore.stop();
  mic.grant();
  await starting;
  expect(mic.track.stop).toHaveBeenCalled();
  expect(FakeWebSocket.latest).toBeUndefined();
  expect(voiceCallStore.snapshot().state).toBe("idle");
  expect(voiceCallStore.inCall()).toBe(false);
});

test("接通中点静音：麦克风就绪即关轨道，通道打开后告诉 Runtime 静音，界面显示已静音", async () => {
  const mic = pendingMicrophone();
  const call = new VoiceCall("http://runtime");
  const starting = call.start();
  await vi.waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalled());
  call.mute(true);
  mic.grant();
  await vi.waitFor(() => expect(FakeWebSocket.latest).toBeDefined());
  expect(mic.track.enabled).toBe(false);
  FakeWebSocket.latest!.onopen?.();
  await starting;
  const sent = FakeWebSocket.latest!.send.mock.calls.map(([payload]) => JSON.parse(String(payload)));
  expect(sent).toContainEqual({ type: "mute" });
  expect(call.current().muted).toBe(true);
  call.stop();
});

/** One 1024-sample microphone block at a constant level, as the ScriptProcessor delivers it. */
function micBlock(context: FakeAudioContext, level: number) {
  context.processors[0].onaudioprocess!({ inputBuffer: { getChannelData: () => new Float32Array(1024).fill(level), sampleRate: 16000 } });
}

/** `ms` of call time in 500 ms steps, feeding one block per step unless `level` is undefined (no audio at all). */
function elapse(context: FakeAudioContext, ms: number, level?: number) {
  for (let t = 0; t < ms; t += 500) {
    if (level !== undefined) micBlock(context, level);
    vi.advanceTimersByTime(500);
  }
}

test("没有收到麦克风声音：听你说时麦克风约 4 秒全零才提示，声音一回来就消失，诊断事件记下转换", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  try {
    const call = await connectedCall();
    const context = FakeAudioContext.latest!;
    receive(JSON.stringify({ type: "status", state: "listening" }));
    elapse(context, MIC_SILENT_MS - 1000, 0);
    expect(call.current().micSilent).toBeFalsy();
    elapse(context, 1500, 0);
    expect(call.current().micSilent).toBe(true);
    expect(orbLabel(call.current())).toBe("没收到麦克风声音");
    expect(call.events).toContainEqual(expect.objectContaining({ type: "mic_silent", silent: true }));
    micBlock(context, 0.05); // the user speaks: gone at once, not after the next check
    expect(call.current().micSilent).toBe(false);
    expect(orbLabel(call.current())).toBe("听你说");
    // Thinking / speaking is not listening: no hint while the AI works or talks.
    receive(JSON.stringify({ type: "status", state: "speaking" }));
    elapse(context, MIC_SILENT_MS + 1000, 0);
    expect(call.current().micSilent).toBeFalsy();
    call.stop();
  } finally { vi.useRealTimers(); }
});

test("没有收到麦克风声音：根本没有音频块（AudioContext 停住）也提示；主动静音期间不提示，取消静音后重新计时", async () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  try {
    const call = await connectedCall();
    const context = FakeAudioContext.latest!;
    receive(JSON.stringify({ type: "status", state: "listening" }));
    call.mute(true);
    elapse(context, MIC_SILENT_MS * 2, 0); // a muted track delivers zeros on purpose
    expect(call.current().micSilent).toBeFalsy();
    call.mute(false);
    elapse(context, MIC_SILENT_MS - 1000); // grace restarts at unmute
    expect(call.current().micSilent).toBeFalsy();
    elapse(context, 1500); // no block at all
    expect(call.current().micSilent).toBe(true);
    expect(call.events).toContainEqual(expect.objectContaining({ type: "mic_silent", silent: true, blocks: 0 }));
    call.mute(true);
    elapse(context, 500);
    expect(call.current().micSilent).toBe(false);
    expect(call.events.filter(event => event.type === "mic_silent").map(event => event.type === "mic_silent" && event.silent)).toEqual([true, false]);
    call.stop();
  } finally { vi.useRealTimers(); }
});

test("同一副蓝牙耳机拆成立体声与免提两项时点名建议免提那一项；当前已是免提、没有同族免提项或只有默认别名时不建议", async () => {
  const { handsFreeSuggestion } = await import("./micCapture");
  const input = (label: string) => ({ kind: "audioinput", label });
  const windows = [input("默认 - 耳机 (WH-1000XM4 Stereo)"), input("耳机 (WH-1000XM4 Stereo)"), input("耳麦 (WH-1000XM4 Hands-Free AG Audio)"),
    input("麦克风 (Realtek(R) Audio)"), { kind: "audiooutput", label: "耳机 (WH-1000XM4 Hands-Free AG Audio)" }];
  expect(handsFreeSuggestion("耳机 (WH-1000XM4 Stereo)", windows)).toBe("耳麦 (WH-1000XM4 Hands-Free AG Audio)");
  expect(handsFreeSuggestion("Headphones (WH-1000XM4 Stereo)", [input("Headset (WH-1000XM4 Hands-Free AG Audio)")])).toBe("Headset (WH-1000XM4 Hands-Free AG Audio)");
  expect(handsFreeSuggestion("耳机 (立体声)", [input("耳机 (免提)")])).toBe("耳机 (免提)");
  // The current label was shown without its owner name; the candidates are compared the same way.
  expect(handsFreeSuggestion("耳机 (WH Stereo)", [input("张三的耳麦 (WH Hands-Free AG Audio)")])).toBe("耳麦 (WH Hands-Free AG Audio)");
  // Owner names inside the brackets go too (the shown label and the suggestion alike).
  expect(handsFreeSuggestion("Headphones (AirPods Stereo)", [input("Headset (Ann's AirPods Hands-Free AG Audio)")])).toBe("Headset (AirPods Hands-Free AG Audio)");
  expect(handsFreeSuggestion("耳机 (WH-1000XM4 Stereo)", [input("耳麦 (WH-1000XM4 Hands-Free AG Audio)")])).toBe("耳麦 (WH-1000XM4 Hands-Free AG Audio)");
  expect(handsFreeSuggestion("耳麦 (WH-1000XM4 Hands-Free AG Audio)", windows)).toBeUndefined();
  expect(handsFreeSuggestion("耳机 (WH-1000XM4 Stereo)", [input("耳麦 (AirPods Hands-Free AG Audio)")])).toBeUndefined();
  expect(handsFreeSuggestion("耳机 (WH-1000XM4 Stereo)", [input("默认 - 耳麦 (WH-1000XM4 Hands-Free AG Audio)")])).toBeUndefined();
});

test("提示带上浏览器实际在用的麦克风名，枚举到同一副耳机的免提输入时点名建议", async () => {
  const track = { enabled: true, stop: vi.fn(), label: "耳机 (WH-1000XM4 Stereo)", muted: false, readyState: "live", getSettings: () => ({ deviceId: "default" }) };
  Object.defineProperty(navigator, "mediaDevices", { value: {
    getUserMedia: vi.fn(async () => ({ getTracks: () => [track], getAudioTracks: () => [track] })),
    enumerateDevices: vi.fn(async () => [{ kind: "audioinput", label: "耳机 (WH-1000XM4 Stereo)" }, { kind: "audioinput", label: "耳麦 (WH-1000XM4 Hands-Free AG Audio)" }]),
  }, configurable: true });
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  try {
    const call = await connectedCall();
    const context = FakeAudioContext.latest!;
    receive(JSON.stringify({ type: "status", state: "listening" }));
    elapse(context, MIC_SILENT_MS + 500, 0);
    expect(call.current()).toMatchObject({ micSilent: true, micLabel: "耳机 (WH-1000XM4 Stereo)" });
    expect(call.events).toContainEqual(expect.objectContaining({ type: "mic_silent", silent: true, trackMuted: false, readyState: "live" }));
    await vi.waitFor(() => expect(call.current().micSuggestion).toBe("耳麦 (WH-1000XM4 Hands-Free AG Audio)"));
    expect(call.events).toContainEqual({ type: "mic_suggestion", label: "耳麦 (WH-1000XM4 Hands-Free AG Audio)" });
    call.stop();
  } finally { vi.useRealTimers(); }
});

test("诊断里记下浏览器实际打开的麦克风：去掉设备名里的人名，deviceId 只留前缀，带轨道设置与静音状态", async () => {
  const track = { enabled: true, stop: vi.fn(), label: "张三的 AirPods Pro", muted: false, readyState: "live",
    getSettings: () => ({ deviceId: "abcdef1234567890", sampleRate: 48000, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }) };
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [track], getAudioTracks: () => [track] })) }, configurable: true });
  const call = await connectedCall();
  const device = call.events.find(event => event.type === "microphone");
  expect(device).toMatchObject({ type: "microphone", label: "AirPods Pro", deviceId: "abcdef…", sampleRate: 48000, channelCount: 1,
    echoCancellation: true, trackMuted: false, readyState: "live" });
  expect(JSON.stringify(device)).not.toContain("张三");
  expect(JSON.stringify(device)).not.toContain("1234567890");
  expect(logDesktop).toHaveBeenCalledWith("microphone", "device", expect.stringContaining("AirPods Pro"));
  call.stop();
});

test("换人登录后诊断导出不含上一人的通话（#523）：A 自己导出仍有，退出后迟到的旧通话回调不写回，导出不发网络请求", async () => {
  const fetchMock = vi.fn(async (url: string) => new Response(JSON.stringify(String(url).endsWith("/logout") ? { status: "signed_out" }
    : { status: "authenticated", userId: "user-b", username: "b", role: "user", bindings: {} }), { status: 200, headers: { "content-type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  const blobs: Blob[] = [];
  vi.stubGlobal("URL", Object.assign(Object.create(URL), { createObjectURL: (blob: Blob) => { blobs.push(blob); return "blob:x"; }, revokeObjectURL: vi.fn() }));
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  const exported = async () => {
    const calls = fetchMock.mock.calls.length;
    expect(await exportDiagnostics({})).toBe("已导出");
    expect(fetchMock.mock.calls.length).toBe(calls);
    const blob = blobs.at(-1)!;
    return new Promise<string>(resolve => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(blob); });
  };
  const call = async () => {
    const starting = voiceCallStore.start("http://runtime", "chat-1");
    await vi.waitFor(() => expect(FakeWebSocket.latest).toBeDefined());
    FakeWebSocket.latest!.onopen?.();
    await starting;
    return FakeWebSocket.latest!;
  };
  voiceCallStore.reset();
  FakeWebSocket.latest = undefined;
  await call();
  receive(JSON.stringify({ type: "transcript", text: "仅 A 的测试句" }));
  receive(JSON.stringify({ type: "text", delta: "仅 A 的测试答" }));
  voiceCallStore.stop();
  expect(await exported()).toContain("仅 A 的测试句");
  // A starts another call, signs out mid-call; the old socket's late events and a late 结束 write nothing.
  FakeWebSocket.latest = undefined;
  const late = await call();
  await signOutOwner("http://runtime");
  expect(voiceCallStore.snapshot().state).toBe("idle");
  late.onmessage?.({ data: JSON.stringify({ type: "transcript", text: "仅 A 的迟到句" }) });
  voiceCallStore.stop();
  await authenticateTqkq("http://runtime", "b", "pw");
  const text = await exported();
  expect(text).not.toContain("仅 A");
  expect(JSON.parse(text).lastVoiceTimeline).toEqual([]);
  click.mockRestore();
});
