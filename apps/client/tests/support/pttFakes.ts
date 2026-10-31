/** Microphone and WebSocket stand-ins for the push-to-talk tests (vitest globals). */

type ProcessorCallback = (event: { inputBuffer: { getChannelData: () => Float32Array; sampleRate: number } }) => void;

type FakeNode = { kind: string; targets: unknown[]; connect: (target: unknown) => void; disconnect: () => void };
const node = <T extends object>(kind: string, extra: T): FakeNode & T => {
  const made: FakeNode = { kind, targets: [], connect: target => { made.targets.push(target); }, disconnect: () => { made.targets = []; } };
  return Object.assign(made, extra);
};
const param = (value = 0) => ({ value });

/** WebAudio for the voice tests: the microphone's ScriptProcessor (the test drives its callback),
 *  and enough output nodes to follow what playback is connected to. Starts suspended like a phone. */
export class FakeAudioContext {
  static latest: FakeAudioContext | undefined;
  static created = 0;
  destination = node("destination", {});
  closed = false;
  state: "suspended" | "running" | "closed" = "suspended";
  resumeCalls = 0;
  currentTime = 0;
  processors: (FakeNode & { onaudioprocess: ProcessorCallback | null })[] = [];
  sources: (FakeNode & { buffer: unknown; start: () => void; stop: () => void; onended: null })[] = [];
  constructor() { FakeAudioContext.latest = this; FakeAudioContext.created += 1; }
  async resume() { this.resumeCalls += 1; if (this.state !== "closed") this.state = "running"; }
  async close() { this.closed = true; this.state = "closed"; }
  createMediaStreamSource() { return node("microphone", {}); }
  createGain() { return node("gain", { gain: param(1) }); }
  createAnalyser() { return node("analyser", { fftSize: 2048 }); }
  createDynamicsCompressor() { return node("compressor", { threshold: param(), knee: param(), ratio: param(), attack: param(), release: param() }); }
  createBuffer(_channels: number, length: number, sampleRate: number) {
    const data = new Float32Array(length);
    return { duration: length / sampleRate, getChannelData: () => data };
  }
  createBufferSource() {
    const source = node("buffer", { buffer: null as unknown, start: () => undefined, stop: () => undefined, onended: null });
    this.sources.push(source);
    return source;
  }
  createScriptProcessor() {
    const processor = node("processor", { onaudioprocess: null as ProcessorCallback | null });
    this.processors.push(processor);
    return processor;
  }
}

export class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static latest: FakeWebSocket | undefined;
  binaryType = "";
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  sent: (ArrayBuffer | string)[] = [];
  closed = false;
  constructor(readonly url: string, readonly protocols?: string[]) { FakeWebSocket.latest = this; }
  send(item: ArrayBuffer | string) { this.sent.push(item); }
  close() { this.closed = true; this.readyState = 3; }
  open() { this.readyState = 1; this.onopen?.(); }
  receive(payload: object) { this.onmessage?.({ data: JSON.stringify(payload) }); }
  commands() { return this.sent.filter((item): item is string => typeof item === "string").map(item => JSON.parse(item).type as string); }
  frames() { return this.sent.filter(item => typeof item !== "string").length; }
}

/** 20 ms of sound at 16 kHz through the microphone's current processor. */
export function speak(amplitude = 0.1) {
  const samples = new Float32Array(320).fill(amplitude);
  FakeAudioContext.latest!.processors.at(-1)!.onaudioprocess!({ inputBuffer: { getChannelData: () => samples, sampleRate: 16000 } });
}

/** Every microphone track handed out, so a test can see that each one was stopped. */
export const microphoneTracks: { stopped: boolean }[] = [];
const grantedMicrophone = () => vi.fn(async () => {
  const track = { stopped: false, enabled: true, stop() { track.stopped = true; } };
  microphoneTracks.push(track);
  return { getTracks: () => [track], getAudioTracks: () => [track] };
});

export function installPttFakes(getUserMedia: (...args: unknown[]) => Promise<unknown> = grantedMicrophone()) {
  FakeAudioContext.latest = undefined;
  FakeAudioContext.created = 0;
  FakeWebSocket.latest = undefined;
  microphoneTracks.length = 0;
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("WebSocket", FakeWebSocket);
  return getUserMedia;
}

export function removePttFakes() {
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, "mediaDevices", { value: undefined, configurable: true });
}
