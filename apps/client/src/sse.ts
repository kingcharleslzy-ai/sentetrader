/** Hand-written SSE over fetch: EventSource cannot send a bearer token. */
import { rumStreamDown, rumStreamUp } from "./rum";

export interface SseFrame {
  event: string;
  data: string[];
  id?: string;
  /** True when the block was only a comment line (`:` keep-alive). */
  comment: boolean;
}

/** Split a growing text buffer into complete SSE frames; the tail stays buffered. */
export function parseSseBuffer(buffer: string): { frames: SseFrame[]; rest: string } {
  const blocks = buffer.split(/\r?\n\r?\n/);
  const rest = blocks.pop() ?? "";
  const frames: SseFrame[] = [];
  for (const block of blocks) {
    const frame: SseFrame = { event: "message", data: [], comment: false };
    let sawField = false;
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith(":")) { frame.comment = true; continue; }
      sawField = true;
      if (line.startsWith("id:")) frame.id = line.slice(3).trim();
      else if (line.startsWith("event:")) frame.event = line.slice(6).trim();
      else if (line.startsWith("data:")) frame.data.push(line.slice(5).trimStart());
    }
    if (sawField) frame.comment = false;
    if (sawField || frame.comment) frames.push(frame);
  }
  return { frames, rest };
}

export interface SseStreamOptions {
  url: string;
  headers?: Record<string, string>;
  /** Fired for every complete frame, including comment keep-alives. */
  onFrame: (frame: SseFrame) => void;
  onOpen?: () => void;
  onClose?: () => void;
  retryMs?: number;
  /** Reconnect when nothing at all — not even a keep-alive — arrived for this long (#745 P3-D). A stream
   *  can go silent without ever closing (a dropped proxy/NAT mapping, a sleeping laptop, a frozen tab):
   *  the read then waits forever while the page looks connected. Must exceed the server's heartbeat gap;
   *  0 turns it off. */
  idleMs?: number;
  fetchImpl?: typeof fetch;
}

/** Both SSE endpoints send a keep-alive at least every 15 s (the guest stream's longest gap). */
export const DEFAULT_SSE_IDLE_MS = 45_000;

/**
 * Connect, read frames, reconnect after `retryMs` on any failure, resuming
 * with `Last-Event-ID` from the last frame that carried an id. Returns stop().
 */
export function openSseStream(options: SseStreamOptions): () => void {
  const { url, headers = {}, onFrame, onOpen, onClose, retryMs = 1000, idleMs = DEFAULT_SSE_IDLE_MS, fetchImpl = fetch } = options;
  let stopped = false;
  let abort: AbortController | undefined;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  let lastEventId = "";

  const connect = async () => {
    const attempt = abort = new AbortController();
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    // The last sign of life of this attempt (its start, then every chunk). A silent drop is noticed only when
    // the idle watchdog fires, so an outage the watchdog ended is dated from here, not from the moment it
    // was noticed; a closed or failed connection is down from the moment it ended.
    let lastAlive = Date.now();
    let silent = false;
    // Armed from the request on and re-armed by every chunk: silence ends this attempt like a closed stream.
    const alive = () => {
      lastAlive = Date.now();
      if (idleTimer !== undefined) clearTimeout(idleTimer);
      if (idleMs > 0 && !stopped) idleTimer = setTimeout(() => { silent = true; attempt.abort(); void reader?.cancel().catch(() => undefined); }, idleMs);
    };
    alive();
    try {
      const requestHeaders = new Headers({ Accept: "text/event-stream", ...headers });
      if (lastEventId) requestHeaders.set("Last-Event-ID", lastEventId);
      const response = await fetchImpl(url, { credentials: "include", headers: requestHeaders, signal: abort.signal });
      if (!response.ok || !response.body) throw new Error(`sse_${response.status}`);
      rumStreamUp();
      onOpen?.();
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (!stopped) {
        const { done, value } = await reader.read();
        if (!silent) alive();   // the watchdog's own cancel ends the read: that is not a sign of life
        buffer += decoder.decode(value, { stream: !done });
        const parsed = parseSseBuffer(buffer);
        buffer = parsed.rest;
        for (const frame of parsed.frames) {
          if (frame.id !== undefined) lastEventId = frame.id;
          onFrame(frame);
        }
        if (done) break;
      }
      if (!stopped) throw new Error("sse_closed");
    } catch {
      if (idleTimer !== undefined) clearTimeout(idleTimer);
      if (stopped) return;
      rumStreamDown(silent ? lastAlive : undefined);
      onClose?.();
      retryTimer = setTimeout(() => void connect(), retryMs);
    }
  };

  void connect();
  return () => {
    stopped = true;
    if (retryTimer !== undefined) clearTimeout(retryTimer);
    if (idleTimer !== undefined) clearTimeout(idleTimer);
    abort?.abort();
  };
}
