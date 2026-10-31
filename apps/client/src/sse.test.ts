// @vitest-environment node
import { openSseStream, parseSseBuffer, type SseFrame } from "./sse";
import { rumStreamDown } from "./rum";

vi.mock("./rum", () => ({ rumStreamDown: vi.fn(), rumStreamUp: vi.fn() }));

test("parses complete frames and keeps the partial tail buffered", () => {
  const { frames, rest } = parseSseBuffer("id: 7\nevent: transient\ndata: {\"a\":1}\ndata: more\n\n: keep-alive\n\nevent: heartbeat\r\ndata: x\r\n\r\ndata: partial");
  expect(frames).toEqual<SseFrame[]>([
    { event: "transient", data: ["{\"a\":1}", "more"], id: "7", comment: false },
    { event: "message", data: [], comment: true },
    { event: "heartbeat", data: ["x"], comment: false },
  ]);
  expect(rest).toBe("data: partial");
});

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) { for (const chunk of chunks) controller.enqueue(encoder.encode(chunk)); controller.close(); },
  });
}

test("reconnects after the stream closes and resumes from the last event id", async () => {
  vi.useFakeTimers();
  try {
    const seen: string[] = [];
    const lastEventIds: Array<string | null> = [];
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      lastEventIds.push(new Headers(init?.headers).get("Last-Event-ID"));
      const body = fetchImpl.mock.calls.length === 1
        ? streamOf(["id: 1\ndata: first\n\n", "id: 2\ndata: sec", "ond\n\n"])
        : streamOf(["id: 3\ndata: third\n\n"]);
      return new Response(body, { status: 200 });
    });
    const opened = vi.fn();
    const closed = vi.fn();
    const stop = openSseStream({ url: "http://runtime.test/api/events", headers: { Authorization: "Bearer t" },
      onFrame: frame => seen.push(frame.data.join("\n")), onOpen: opened, onClose: closed, retryMs: 1000, fetchImpl: fetchImpl as typeof fetch });
    await vi.advanceTimersByTimeAsync(10);
    expect(seen).toEqual(["first", "second"]);
    expect(closed).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(seen).toEqual(["first", "second", "third"]);
    expect(lastEventIds).toEqual([null, "2"]);
    expect(opened).toHaveBeenCalledTimes(2);
    expect(new Headers(fetchImpl.mock.calls[0][1]?.headers).get("Authorization")).toBe("Bearer t");
    stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

test("a non-2xx response is treated as a closed stream and retried", async () => {
  vi.useFakeTimers();
  try {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 503 }));
    const closed = vi.fn();
    const stop = openSseStream({ url: "http://runtime.test/api/events", onFrame: () => undefined, onClose: closed, retryMs: 500, fetchImpl: fetchImpl as typeof fetch });
    await vi.advanceTimersByTimeAsync(10);
    expect(closed).toHaveBeenCalledTimes(1);
    expect(rumStreamDown).toHaveBeenLastCalledWith(undefined);   // a refused connection is down from now
    await vi.advanceTimersByTimeAsync(500);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    stop();
  } finally {
    vi.useRealTimers();
  }
});

test("a stream that goes silent without closing is reopened after the idle limit; keep-alives hold it open (#745 P3-D)", async () => {
  vi.useFakeTimers();
  try {
    const encoder = new TextEncoder();
    const bodies: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchImpl = vi.fn(async () => new Response(new ReadableStream<Uint8Array>({ start(controller) { bodies.push(controller); } }), { status: 200 }));
    const opened = vi.fn();
    const closed = vi.fn();
    const stop = openSseStream({ url: "http://runtime.test/api/events", onFrame: () => undefined, onOpen: opened, onClose: closed,
      retryMs: 1000, idleMs: 20_000, fetchImpl: fetchImpl as typeof fetch });
    await vi.advanceTimersByTimeAsync(10);
    expect(opened).toHaveBeenCalledTimes(1);
    // A keep-alive every 15 s: never idle for 20 s, no reconnect.
    for (let i = 0; i < 4; i++) {
      await vi.advanceTimersByTimeAsync(15_000);
      bodies[0].enqueue(encoder.encode(": keep-alive\n\n"));
    }
    expect(closed).not.toHaveBeenCalled();
    // Then silence: the open-but-dead stream is given up and a new one opened.
    const lastFrameAt = Date.now();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(closed).toHaveBeenCalledTimes(1);
    // The outage is dated from the last frame, not from the moment the watchdog noticed (RUM sse_down).
    expect(rumStreamDown).toHaveBeenLastCalledWith(lastFrameAt);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(opened).toHaveBeenCalledTimes(2);
    stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(closed).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});

test("a request that never gets its headers is also abandoned after the idle limit", async () => {
  vi.useFakeTimers();
  try {
    const fetchImpl = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    }));
    const closed = vi.fn();
    const startedAt = Date.now();
    const stop = openSseStream({ url: "http://runtime.test/api/events", onFrame: () => undefined, onClose: closed, retryMs: 500, fetchImpl: fetchImpl as typeof fetch });
    await vi.advanceTimersByTimeAsync(45_000);
    expect(closed).toHaveBeenCalledTimes(1);
    expect(rumStreamDown).toHaveBeenLastCalledWith(startedAt);   // dated from the attempt's start
    await vi.advanceTimersByTimeAsync(500);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    stop();
  } finally {
    vi.useRealTimers();
  }
});
