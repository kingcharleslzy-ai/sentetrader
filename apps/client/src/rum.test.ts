import { describe, expect, it, vi } from "vitest";
import { RumCollector, bucketOf, errorKind, RUM_BUCKET_BOUNDS } from "./rum";

const MINUTE = 1_800_000_000_000 - (1_800_000_000_000 % 60_000);

function collector(options: { fetch?: typeof fetch; beacon?: (url: string, body: Blob) => boolean } = {}) {
  let now = MINUTE + 5_000;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetchImpl = options.fetch ?? (vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ enabled: true, accepted: true, serverNow: now + 2_000 }), { status: 200 });
  }) as unknown as typeof fetch);
  const rum = new RumCollector({ now: () => now, fetch: fetchImpl, beacon: options.beacon, random: () => 0.5 });
  return { rum, calls, advance: (ms: number) => { now += ms; }, at: () => now };
}

const target = (headers: Record<string, string> = {}) => ({ baseUrl: "https://runtime.test", headers: () => new Headers(headers) });
const sent = (init?: RequestInit) => JSON.parse(String(init?.body));

describe("RUM collector", () => {
  it("buckets values on the Runtime's bounds and names errors by kind only", () => {
    expect(bucketOf(5)).toBe(0);
    expect(bucketOf(120)).toBe(5);
    expect(bucketOf(1e9)).toBe(RUM_BUCKET_BOUNDS.length);
    expect(errorKind(new TypeError("Failed to fetch"), true)).toBe("network");
    expect(errorKind(new TypeError("x is undefined"), false)).toBe("type");
    expect(errorKind(new Error("Failed to fetch dynamically imported module: /a.js"), false)).toBe("chunk");
    expect(errorKind(new DOMException("aborted", "AbortError"), true)).toBeUndefined();
    expect(errorKind("plain reason", true)).toBe("rejection");
  });

  it("sends one minute as counts, sums and bucket counts — no raw values or text", async () => {
    const { rum, calls } = collector();
    rum.record("lcp", 1200);
    rum.record("lcp", 1300);
    rum.error(new TypeError("secret symbol SHFE.au2612 in message"), false);
    rum.noteStreamDown();
    rum.noteStreamUp();
    rum.setTarget(target());
    await rum.flush();
    const post = calls.find(call => call.init?.method === "POST")!;
    expect(post.url).toBe("https://runtime.test/api/telemetry/rum");
    expect(new Headers(post.init?.headers).get("Content-Type")).toBe("text/plain");
    const body = sent(post.init);
    expect(body).toMatchObject({ v: 1, client: "web", minutes: [{ t: MINUTE / 1000 }] });
    expect(body.session).toMatch(/^[0-9a-f]{32}$/);
    expect(body.minutes[0].metrics).toEqual({
      lcp: { n: 2, sum: 2500, buckets: { "11": 2 } },
      "error.type": { n: 1, sum: 0 },
      sse_down: { n: 1, sum: 0, buckets: { "0": 1 } },
      sse_reconnect: { n: 1, sum: 0 },
    });
    expect(String(post.init?.body)).not.toContain("SHFE");
    // Sent minutes are not sent again.
    const before = calls.length;
    await rum.flush();
    expect(calls.length).toBe(before);
  });

  it("corrects the price latency with the server clock, including samples taken before it was known", async () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const { rum, calls, advance, at } = collector();
    // Server clock 2 s ahead of this page: a quote captured "now" on the server is painted 300 ms later here.
    const captured = new Date(at() + 2_000 - 300).toISOString();
    rum.notePriceCapture(captured, callback => callback());
    rum.setTarget(target());
    await vi.waitFor(() => expect(calls.length).toBe(1));   // the handshake GET carried serverNow
    await new Promise(resolve => setTimeout(resolve, 0));    // …and its answer was read
    advance(1_000);
    rum.notePriceCapture(new Date(at() + 2_000 - 150).toISOString(), callback => callback());
    await rum.flush();
    const metrics = sent(calls[1].init).minutes[0].metrics;
    expect(metrics.price_latency.n).toBe(2);
    expect(metrics.price_latency.sum).toBe(450);
  });

  it("counts the chart's first paint only for a page that stayed visible until it drew", async () => {
    const shown = collector();
    shown.rum.noteVisibility(true);
    shown.rum.noteChartPainted(900);
    shown.rum.noteChartPainted(5000);    // once per page
    shown.rum.setTarget(target());
    await shown.rum.flush();
    const sentShown = sent(shown.calls.find(call => call.init?.method === "POST")!.init).minutes[0].metrics;
    expect(sentShown.kline_first_paint).toEqual({ n: 1, sum: 900, buckets: { "10": 1 } });
    const background = collector();
    background.rum.noteVisibility(false);   // opened in a background tab
    background.rum.noteChartPainted(98_000);
    background.rum.record("ttfb", 40);
    background.rum.setTarget(target());
    await background.rum.flush();
    const sentBackground = sent(background.calls.find(call => call.init?.method === "POST")!.init).minutes[0].metrics;
    expect(sentBackground.kline_first_paint).toBeUndefined();
  });

  it("dates a silent stream drop from when it really went quiet", () => {
    const { rum, advance, at } = collector();
    const quietSince = at() - 40_000;   // the idle watchdog only fired 40 s after the last frame
    rum.noteStreamDown(quietSince);
    rum.noteStreamDown(at());           // a later failed attempt does not move the start
    advance(5_000);
    rum.noteStreamUp();
    const metrics = JSON.parse(rum.takeBatch()!).minutes[0].metrics;
    expect(metrics.sse_down).toEqual({ n: 1, sum: 45_000, buckets: { "19": 1 } });
  });

  it("measures the newest capture when several frames land before one paint", () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const { rum, advance, at } = collector();
    rum.noteServerClock(at(), at(), at());   // no clock offset: the raw difference is the latency
    const paints: Array<() => void> = [];
    const schedule = (callback: () => void) => { paints.push(callback); };
    rum.notePriceCapture(new Date(at() - 900).toISOString(), schedule);
    advance(100);
    rum.notePriceCapture(new Date(at() - 150).toISOString(), schedule);   // same paint window, newer
    expect(paints).toHaveLength(1);
    paints[0]();
    const metrics = JSON.parse(rum.takeBatch()!).minutes[0].metrics;
    expect(metrics.price_latency).toMatchObject({ n: 1, sum: 150 });
  });

  it("splits visible time on minute boundaries, also across a gap with nowhere to send", async () => {
    const { rum, calls, advance } = collector();
    rum.noteVisibility(true);           // visible from :05 of minute 0
    advance(150_000);                   // …to :35 of minute 2
    await rum.flush();                  // no target yet: the timer still books the interval
    rum.setTarget(target());
    await rum.flush();
    const post = calls.find(call => call.init?.method === "POST")!;
    const minutes = sent(post.init).minutes as Array<{ t: number; metrics: { visible: { sum: number } } }>;
    expect(minutes.map(minute => [minute.t, minute.metrics.visible.sum])).toEqual([
      [MINUTE / 1000, 55_000], [MINUTE / 1000 + 60, 60_000], [MINUTE / 1000 + 120, 35_000]]);
  });

  it("books the visible time of a late timer and of a hide in the minutes they belong to", () => {
    const { rum, advance } = collector();
    rum.noteVisibility(true);
    advance(100_000);                   // a throttled timer wakes up 100 s later: :05 → 1:45
    rum.noteVisibility(false);
    const minutes = JSON.parse(rum.takeBatch()!).minutes as Array<{ t: number; metrics: { visible: { sum: number } } }>;
    expect(minutes.map(minute => minute.metrics.visible.sum)).toEqual([55_000, 45_000]);
  });

  it("starts a new page session and leaves the old pending minutes when the identity changes", async () => {
    const { rum, calls } = collector();
    rum.setTarget({ ...target(), identity: "guest" });
    const guestSession = rum.session;
    rum.record("lcp", 900);
    rum.setTarget(undefined);           // the guest view goes away: its minutes are sent under its own session
    await vi.waitFor(() => expect(calls.filter(call => call.init?.method === "POST")).toHaveLength(1));
    expect(sent(calls.find(call => call.init?.method === "POST")!.init).session).toBe(guestSession);
    rum.record("inp", 80);              // gathered in the gap, belonging to nobody the next target knows
    rum.setTarget({ ...target(), identity: "member" });
    expect(rum.session).not.toBe(guestSession);
    rum.record("fcp", 300);
    await rum.flush();
    const posts = calls.filter(call => call.init?.method === "POST");
    const member = sent(posts[1].init);
    expect(member.session).toBe(rum.session);
    expect(Object.keys(member.minutes[0].metrics)).toEqual(["fcp"]);
  });

  it("keeps the session when the same identity reconnects", () => {
    const { rum } = collector();
    rum.setTarget({ ...target(), identity: "member" });
    const session = rum.session;
    rum.setTarget(undefined);
    rum.setTarget({ ...target(), identity: "member" });
    expect(rum.session).toBe(session);
  });

  it("stops for good when the Runtime says it is switched off", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ enabled: false, serverNow: 1 }), { status: 200 })) as unknown as typeof fetch;
    const { rum } = collector({ fetch: fetchImpl });
    rum.setTarget(target());
    await vi.waitFor(() => expect(rum.active).toBe(false));
    rum.record("lcp", 100);
    await rum.flush();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("uses a beacon for a closing page with a cookie login, the keepalive request for a bearer login", async () => {
    const beacon = vi.fn(() => true);
    const { rum, calls } = collector({ beacon });
    rum.setTarget(target());
    rum.record("inp", 80);
    await rum.flush(true);
    expect(beacon).toHaveBeenCalledTimes(1);
    expect(calls.filter(call => call.init?.method === "POST")).toHaveLength(0);
    rum.setTarget(target({ Authorization: "Bearer local" }));
    rum.record("inp", 90);
    await rum.flush(true);
    expect(beacon).toHaveBeenCalledTimes(1);
    const post = calls.find(call => call.init?.method === "POST")!;
    expect(post.init?.keepalive).toBe(true);
  });
});
