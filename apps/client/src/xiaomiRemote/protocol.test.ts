import { AtvvDecoder } from "./protocol";
const caps = new Uint8Array([11,1,0,2,3,0,120]);
const start = new Uint8Array([4,3,2,7]);
const stop = new Uint8Array([0,0]);
const audio = () => new Uint8Array(120);
function ready() { const decoder = new AtvvDecoder(); decoder.control(caps); return decoder; }

test("firmware's standard and swapped capabilities require 16kHz and a bounded frame", () => {
  expect(ready().ready).toBe(true);
  const decoder = new AtvvDecoder();
  expect(decoder.control(new Uint8Array([11,1,0,0,3,0,120]))).toBe("ready");
  expect(() => decoder.control(new Uint8Array([11,1,0,1,0,0,120]))).toThrow("16 kHz");
  expect(() => decoder.control(new Uint8Array([11,1,0,2,0,0,0]))).toThrow("帧大小");
});

test("direct AUDIO_START decodes high nibble first and resets between actual holds", () => {
  const decoder = ready(); const frame = audio(); frame.set([0x77,0x80]);
  expect(decoder.decode(frame)).toBeUndefined();
  decoder.control(start);
  expect(Array.from(decoder.decode(frame)!.slice(0,4))).toEqual([11,41,37,40]);
  decoder.control(stop); decoder.control(start);
  expect(Array.from(decoder.decode(frame)!.slice(0,4))).toEqual([11,41,37,40]);
});

test("sync before start survives; sync during a hold never leaks into the next hold", () => {
  const decoder = ready(); const sync = new Uint8Array([10,2,0,0,0xfc,0x18,5]);
  decoder.control(sync); decoder.control(start);
  expect(Array.from(decoder.decode(audio())!.slice(0,2))).toEqual([-999,-998]);
  decoder.control(sync); decoder.control(stop); decoder.control(start);
  expect(decoder.decode(audio())![0]).toBe(0);
});

test("malformed sync/frame/start is refused, saturation stays within PCM16", () => {
  const decoder = ready(); decoder.control(start);
  expect(() => decoder.decode(new Uint8Array(3))).toThrow("不完整");
  expect(() => decoder.control(start)).toThrow();
  expect(() => decoder.control(new Uint8Array([10,2,0,0,0,0,89]))).toThrow();
  expect(decoder.decode(new Uint8Array(120).fill(0x77))![239]).toBe(32767);
});
