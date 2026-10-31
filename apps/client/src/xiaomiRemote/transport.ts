import { isTauriRuntime } from "../runtimeConnection";
import { ATVV_SERVICE, ATVV_TX, ATVV_AUDIO, ATVV_CONTROL } from "./protocol";

// A small local type boundary keeps Web Bluetooth optional in Safari/WebKit and avoids a global shim.
interface Characteristic extends EventTarget {
  value?: DataView;
  startNotifications(): Promise<Characteristic>;
  stopNotifications(): Promise<Characteristic>;
  writeValueWithResponse(value: Uint8Array<ArrayBuffer>): Promise<void>;
}
interface BluetoothDevice extends EventTarget {
  name?: string;
  gatt?: {
    connected: boolean;
    connect(): Promise<{ getPrimaryService(id: string): Promise<{ getCharacteristic(id: string): Promise<Characteristic> }> }>;
    disconnect(): void;
  };
}
interface Bluetooth {
  requestDevice(options: unknown): Promise<BluetoothDevice>;
}
export type Packet = { kind: "audio" | "control"; bytes: number[] }
  | { kind: "keysReady" }
  | { kind: "key"; key: string; pressed: boolean }
  | { kind: "keyError"; message: string } | { kind: "error"; message: string }
  | { kind: "disconnected" };
export interface RemoteTransport {
  connect(): Promise<string>;
  write(bytes: Uint8Array<ArrayBuffer>): Promise<void>;
  close(): void;
}
export type RemotePlatform = "macos" | "web" | "unsupported";
const bluetooth = () => (navigator as Navigator & { bluetooth?: Bluetooth }).bluetooth;
export const remotePlatform = (): RemotePlatform => isTauriRuntime()
  ? (/Mac/.test(navigator.platform) ? "macos" : "unsupported")
  : (typeof navigator !== "undefined" && bluetooth() && globalThis.isSecureContext ? "web" : "unsupported");

export function browserTransport(packet: (value: Packet) => void): RemoteTransport {
  let device: BluetoothDevice | undefined;
  let tx: Characteristic | undefined;
  let audio: Characteristic | undefined;
  let control: Characteristic | undefined;
  let generation = 0;
  let pending: Promise<unknown> = Promise.resolve();
  let intentional = false;
  const notifications: [Characteristic, EventListener][] = [];
  const lost = () => { if (!intentional) packet({ kind: "disconnected" }); };
  return {
    async connect() {
      const turn = ++generation;
      intentional = false;
      // requestDevice is called before the first await: it must stay in the Connect button gesture.
      const selected = device ?? await bluetooth()!.requestDevice({
        filters: [{ namePrefix: "小米蓝牙语音遥控器" }, { namePrefix: "Mi RC" }], optionalServices: [ATVV_SERVICE],
      });
      if (turn !== generation) throw new Error("连接已取消");
      device = selected;
      // A canceled GATT call can resolve after close(). Finish it before a successor starts,
      // otherwise its late disconnect/characteristics could damage the new connection.
      const previous = pending;
      const current = async () => {
        await previous.catch(() => undefined);
        const active = () => { if (turn !== generation) throw new Error("连接已取消"); };
        active();
        selected.removeEventListener("gattserverdisconnected", lost);
        selected.addEventListener("gattserverdisconnected", lost);
        const server = await selected.gatt!.connect();
        if (turn !== generation) { selected.gatt!.disconnect(); throw new Error("连接已取消"); }
        const service = await server.getPrimaryService(ATVV_SERVICE);
        active();
        tx = await service.getCharacteristic(ATVV_TX);
        active();
        audio = await service.getCharacteristic(ATVV_AUDIO);
        active();
        control = await service.getCharacteristic(ATVV_CONTROL);
        active();
        for (const [characteristic, kind] of [[audio, "audio"], [control, "control"]] as const) {
          const listener: EventListener = () => {
            if (turn !== generation || !characteristic.value) return;
            const value = characteristic.value;
            packet({ kind, bytes: Array.from(new Uint8Array(value.buffer, value.byteOffset, value.byteLength)) });
          };
          characteristic.addEventListener("characteristicvaluechanged", listener);
          notifications.push([characteristic, listener]);
          await characteristic.startNotifications();
          active();
        }
        return selected.name || "米家遥控器 2 Pro";
      };
      const connecting = current();
      pending = connecting;
      return connecting;
    },
    async write(bytes) {
      if (!tx || !device?.gatt?.connected) throw new Error("遥控器已断开");
      await tx.writeValueWithResponse(bytes);
    },
    close() {
      generation++;
      intentional = true;
      for (const [characteristic, listener] of notifications.splice(0)) {
        characteristic.removeEventListener("characteristicvaluechanged", listener);
        // A disconnected server rejects CCCD writes; local listeners are always removed first.
        if (device?.gatt?.connected) void characteristic.stopNotifications().catch(() => undefined);
      }
      device?.removeEventListener("gattserverdisconnected", lost);
      device?.gatt?.disconnect();
      tx = audio = control = undefined;
    },
  };
}

export function nativeTransport(packet: (value: Packet) => void): RemoteTransport {
  let unlisten: (() => void) | undefined;
  let rejectReady: ((reason: Error) => void) | undefined;
  let turn = 0;
  return {
    async connect() {
      const current = ++turn;
      const { listen } = await import("@tauri-apps/api/event");
      const { invoke } = await import("@tauri-apps/api/core");
      const ready = new Promise<string>((resolve, reject) => {
        rejectReady = reject;
        void listen<Packet | { kind: "connected"; name: string }>("xiaomi-remote", event => {
          if (current !== turn) return;
          if (event.payload.kind === "connected") resolve(event.payload.name);
          else { packet(event.payload); if (event.payload.kind === "error") reject(new Error(event.payload.message)); }
        }).then(off => {
          if (current !== turn) { off(); reject(new Error("连接已取消")); return; }
          unlisten = off;
          return invoke("xiaomi_remote_connect");
        }).catch(reject);
      });
      return ready;
    },
    async write(bytes) {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("xiaomi_remote_write", { bytes: Array.from(bytes) });
    },
    close() {
      turn++;
      rejectReady?.(new Error("连接已取消")); rejectReady = undefined;
      unlisten?.(); unlisten = undefined;
      void import("@tauri-apps/api/core").then(({ invoke }) => invoke("xiaomi_remote_disconnect")).catch(() => undefined);
    },
  };
}
