import { XiaomiRemoteStore } from "./store";
import { ATVV_AUDIO, ATVV_CONTROL, ATVV_TX } from "./protocol";
import { browserTransport } from "./transport";

class Characteristic extends EventTarget {
  value?: DataView;
  startNotifications = vi.fn(async () => this);
  stopNotifications = vi.fn(async () => this);
  writeValueWithResponse = vi.fn(async (bytes: Uint8Array) => {
    if (bytes[0] === 10) this.control?.notify([11,1,0,2,3,0,120]);
  });
  control?: Characteristic;
  notify(bytes: number[]) { this.value = new DataView(new Uint8Array(bytes).buffer); this.dispatchEvent(new Event("characteristicvaluechanged")); }
}
let store: XiaomiRemoteStore;
const original = Object.getOwnPropertyDescriptor(navigator, "bluetooth");
function fixture() {
  const device = new EventTarget() as EventTarget & { name: string; gatt: { connected: boolean; connect: ReturnType<typeof vi.fn<() => Promise<unknown>>>; disconnect: ReturnType<typeof vi.fn> } };
  const sessions: { audio: Characteristic; control: Characteristic; tx: Characteristic }[] = [];
  device.name = "Mi RC";
  device.gatt = { connected: false, disconnect: vi.fn(() => { device.gatt.connected = false; }), connect: vi.fn(async () => {
    device.gatt.connected = true;
    const audio = new Characteristic(), control = new Characteristic(), tx = new Characteristic(); tx.control = control;
    sessions.push({ audio, control, tx });
    const chars: Record<string, Characteristic> = { [ATVV_AUDIO]: audio, [ATVV_CONTROL]: control, [ATVV_TX]: tx };
    return { getPrimaryService: async () => ({ getCharacteristic: async (id: string) => chars[id] }) };
  }) };
  const requestDevice = vi.fn(async () => device);
  Object.defineProperty(navigator, "bluetooth", { configurable: true, value: { requestDevice } });
  vi.stubGlobal("isSecureContext", true);
  store = new XiaomiRemoteStore();
  return { device, sessions, requestDevice };
}
beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); });
afterEach(() => {
  store?.disconnect();
  if (original) Object.defineProperty(navigator, "bluetooth", original); else Reflect.deleteProperty(navigator, "bluetooth");
  vi.unstubAllGlobals(); vi.useRealTimers();
});

test("browser chooses once, subscribes both channels, reserves F5 without recording keyboard audio, and restores it", async () => {
  const { sessions, requestDevice, device } = fixture(); const source = vi.fn(), frames = vi.fn();
  store.onInput(event => { if (event.type === "start") { source(); void event.input(frames, () => {}).start(); } });
  await store.connect();
  expect(store.snapshot()).toMatchObject({ phase: "ready", platform: "web", availableKeys: ["voice"] });
  const first = sessions[0];
  expect(first.audio.startNotifications).toHaveBeenCalledOnce(); expect(first.control.startNotifications).toHaveBeenCalledOnce();
  const f5 = new KeyboardEvent("keydown", { code: "F5", cancelable: true }); window.dispatchEvent(f5);
  expect(f5.defaultPrevented).toBe(true); expect(source).not.toHaveBeenCalled();
  const keyboard = new KeyboardEvent("keydown", { code: "ArrowUp", cancelable: true }); window.dispatchEvent(keyboard);
  expect(keyboard.defaultPrevented).toBe(false);
  first.control.notify([4,3,2,1]); first.audio.notify(Array(120).fill(0x77)); first.control.notify([0,0]);
  expect(source).toHaveBeenCalledOnce(); expect(frames).toHaveBeenCalledOnce();
  device.gatt.connected = false; device.dispatchEvent(new Event("gattserverdisconnected"));
  first.control.notify([4,3,2,1]); // removed old characteristic listener
  await vi.advanceTimersByTimeAsync(1000);
  expect(store.snapshot().phase).toBe("ready"); expect(requestDevice).toHaveBeenCalledOnce();
  sessions[1].control.notify([4,3,2,2]); sessions[1].audio.notify(Array(120).fill(0)); sessions[1].control.notify([0,0]);
  expect(source).toHaveBeenCalledTimes(2); expect(frames).toHaveBeenCalledTimes(2);
  window.dispatchEvent(new Event("pagehide"));
  expect(store.snapshot().enabled).toBe(false);
  expect(sessions[1].audio.stopNotifications).toHaveBeenCalledOnce();
  const after = new KeyboardEvent("keydown", { code: "F5", cancelable: true }); window.dispatchEvent(after); expect(after.defaultPrevented).toBe(false);
});

test("closing while the chooser resolves never attaches notifications or starts a stale link", async () => {
  const { device, sessions } = fixture();
  const connecting = store.connect(); store.disconnect(); await connecting;
  expect(store.snapshot()).toMatchObject({ phase: "idle", enabled: false });
  expect(device.gatt.connect).not.toHaveBeenCalled(); expect(sessions).toHaveLength(0);
});

test("a canceled pending GATT connection finishes before its replacement subscribes", async () => {
  const { device, sessions } = fixture();
  const realConnect = device.gatt.connect.getMockImplementation()!;
  let resume!: () => void;
  const held = new Promise<void>(resolve => { resume = resolve; });
  device.gatt.connect.mockImplementationOnce(async () => { await held; return realConnect(); });
  const packets = vi.fn();
  const transport = browserTransport(packets);
  const first = transport.connect().catch(error => error);
  await vi.advanceTimersByTimeAsync(0);
  expect(device.gatt.connect).toHaveBeenCalledOnce();
  transport.close();
  const second = transport.connect();
  await vi.advanceTimersByTimeAsync(0);
  expect(device.gatt.connect).toHaveBeenCalledOnce();
  resume();
  expect(await first).toBeInstanceOf(Error);
  expect(await second).toBe("Mi RC");
  expect(sessions[0].audio.startNotifications).not.toHaveBeenCalled();
  expect(sessions[1].audio.startNotifications).toHaveBeenCalledOnce();
  sessions[0].control.notify([4,3,2,1]);
  expect(packets).not.toHaveBeenCalled();
  transport.close();
  expect(sessions[1].audio.stopNotifications).toHaveBeenCalledOnce();
});
