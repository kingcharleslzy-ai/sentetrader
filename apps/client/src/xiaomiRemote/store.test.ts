import { XiaomiRemoteStore, type RemoteInput } from "./store";
import type { Packet, RemoteTransport } from "./transport";
const caps: Packet = { kind: "control", bytes: [11,1,0,0,3,0,120] };
const start: Packet = { kind: "control", bytes: [4,3,2,1] };
const stop: Packet = { kind: "control", bytes: [0,0] };
const audio: Packet = { kind: "audio", bytes: Array(120).fill(0x77) };
let stores: XiaomiRemoteStore[] = [];
function fixture(answer = true) {
  let packet!: (packet: Packet) => void;
  const transport: RemoteTransport = { connect: vi.fn(async () => "Mi RC"), close: vi.fn(), write: vi.fn(async bytes => {
    if (answer && bytes[0] === 10) packet(caps);
  }) };
  const store = new XiaomiRemoteStore(callback => { packet = callback; return transport; });
  stores.push(store);
  return { store, transport, packet: (p: Packet) => packet(p) };
}
beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); });
afterEach(() => { stores.forEach(store => store.disconnect()); stores = []; vi.useRealTimers(); });

test("physical voice uses PCM source; no idle MIC_OPEN; every hold gets a new source", async () => {
  const { store, transport, packet } = fixture(); const frames = vi.fn(); const holds: RemoteInput[] = [];
  store.onInput(event => { holds.push(event); if (event.type === "start") void event.input(frames, () => {}).start(); });
  await store.connect(); expect(store.snapshot().phase).toBe("ready");
  expect(transport.write).toHaveBeenCalledTimes(1);
  packet(start); packet(audio); expect(frames).toHaveBeenCalledTimes(1);
  expect(Array.from(frames.mock.calls[0][0].slice(0,2))).toEqual([11,41]);
  packet(stop); packet(audio); expect(frames).toHaveBeenCalledTimes(1);
  packet(start); packet(audio); packet(stop);
  expect(holds.filter(event => event.type === "start")).toHaveLength(2);
  expect(frames).toHaveBeenCalledTimes(2);
});

test("diagnostics receive actual decoded frames but never publish a recording source or button actions", async () => {
  const { store, packet } = fixture(); const events: RemoteInput[] = []; await store.connect();
  store.onInput(e => events.push(e)); store.setDiagnostic(true); events.length = 0;
  packet(start); packet(audio); packet({ kind: "key", key: "home", pressed: true }); packet(stop);
  expect(store.snapshot()).toMatchObject({ diagnostic: true, frames: 1, lastKey: { key: "voice", pressed: false } });
  expect(events).toEqual([]);
  store.setDiagnostic(false); packet(start);
  expect(events.some(e => e.type === "start")).toBe(true);
});

test("disconnect cancels, detaches old audio, handshakes again, and the next hold supplies frames", async () => {
  const { store, packet, transport } = fixture(); const frames = vi.fn(); const events: RemoteInput[] = [];
  store.onInput(e => { events.push(e); if (e.type === "start") void e.input(frames, () => {}).start(); });
  await store.connect(); packet(start); packet(audio); packet({ kind: "disconnected" });
  expect(store.snapshot().phase).toBe("reconnecting"); expect(transport.close).toHaveBeenCalled();
  const count = events.filter(e => e.type === "start").length;
  packet(start); packet(audio); // A late notification from the old channel cannot reopen recording.
  expect(events.filter(e => e.type === "start")).toHaveLength(count);
  await vi.advanceTimersByTimeAsync(1000);
  expect(store.snapshot().phase).toBe("ready");
  packet(start); packet(audio); packet(stop);
  expect(frames).toHaveBeenCalledTimes(2);
  expect(transport.connect).toHaveBeenCalledTimes(2);
});

test("a linked device without caps never becomes ready; manual disconnect stops pending retry", async () => {
  const { store, transport, packet } = fixture(false);
  const connecting = store.connect(); await vi.advanceTimersByTimeAsync(20000); await connecting;
  expect(store.snapshot()).toMatchObject({ phase: "error" });
  expect(transport.close).toHaveBeenCalled();
  packet(caps); expect(store.snapshot().phase).toBe("error");
  store.disconnect(); await vi.advanceTimersByTimeAsync(60000);
  expect(transport.connect).toHaveBeenCalledTimes(1);
});

test("settings survive a new window; invalid key/voice assignments do not mutate anything", () => {
  const { store } = fixture(); store.setBinding("home", "settings");
  const restored = new XiaomiRemoteStore(); stores.push(restored);
  expect(restored.snapshot().bindings.home).toBe("settings");
  expect(() => store.setBinding("home", "ptt")).toThrow();
  expect(store.snapshot().bindings.home).toBe("settings");
  store.resetBindings(); expect(store.snapshot().bindings.home).toBe("chat");
  expect(store.snapshot().bindings.power).toBe("none");
});
