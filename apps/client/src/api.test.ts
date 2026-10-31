import { afterEach, vi } from "vitest";
import {
  resumeAiExecution,
  authenticateCloud,
  authenticateLocal,
  authenticateTqkq,
  createSession,
  fetchAccountDirectory,
  getAccountOverview,
  getMarketWorkbench,
  listInstruments,
  openEventStream,
  runtimeFetch,
  setActiveTradingAccount,
  setSessionTradingAccount,
  requestJson,
} from "./api";
import { DEFAULT_READ_TIMEOUT_MS, LONG_READ_TIMEOUT_MS, isRequestTimeout, isTransientFailure } from "./requestTimeout";
import { guestRequest } from "./guestApi";
import { clientHeaderValue, readClientCompat, resetClientCompat } from "./clientBuild";

afterEach(() => { vi.unstubAllGlobals(); resetClientCompat(); setActiveTradingAccount(null); });

test("cloud session probe returns a local auth requirement without requesting a protected route", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ authenticated: false, authMode: "cloud-jwt", authProvider: "oidc" }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  ));
  vi.stubGlobal("fetch", fetchMock);

  await expect(authenticateCloud("https://runtime.example.test", ""))
    .rejects.toThrow("cloud_auth_required");
  expect(fetchMock).toHaveBeenCalledWith(
    "https://runtime.example.test/api/auth/cloud/status",
    expect.objectContaining({ credentials: "include" }),
  );
});

test("local IPC token stays in memory and authenticates protected requests", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ status: "authenticated" }), {
      status: 200, headers: { "Content-Type": "application/json" }
    }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ sessionId: "session-1" }), {
      status: 201, headers: { "Content-Type": "application/json" }
    }));
  vi.stubGlobal("fetch", fetchMock);

  await authenticateLocal("http://127.0.0.1:43123", "memory-only-token");
  await createSession("http://127.0.0.1:43123", "local");

  const secondRequest = fetchMock.mock.calls[1]?.[1] as RequestInit;
  expect(new Headers(secondRequest.headers).get("Authorization"))
    .toBe("Bearer memory-only-token");
});

test("owner login sends credentials once and relies on the HttpOnly session cookie", async () => {
  const response = {
    status: "authenticated",
    authenticated: true,
    authMode: "owner-password",
    authProvider: "owner-password",
    bindings: {
      profileId: "charles-demo",
      tqkqConfigured: true,
      tqkqMode: "simulation",
      modelProvider: "deepseek",
      modelCredentialConfigured: true,
      liveTrading: false,
    }
  };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), {
    status: 200, headers: { "Content-Type": "application/json" }
  }));
  vi.stubGlobal("fetch", fetchMock);

  await authenticateTqkq("https://runtime.example.test", "charles", "one-time-password");

  const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
  expect(request.credentials).toBe("include");
  expect(new Headers(request.headers).has("Authorization")).toBe(false);
  expect(JSON.parse(String(request.body))).toEqual({
    username: "charles", password: "one-time-password", inviteCode: ""
  });
});

test("instrument catalog uses the authenticated read-only runtime route", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    schemaVersion: 1,
    capturedAt: "2026-08-31T04:00:00Z",
    dataStatus: "normal",
    isCached: false,
    count: 1,
    items: [{ symbol: "DCE.i2609" }]
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);

  const result = await listInstruments("http://127.0.0.1:43123");

  expect(result.items[0]?.symbol).toBe("DCE.i2609");
  expect(fetchMock).toHaveBeenCalledWith(
    "http://127.0.0.1:43123/api/instruments",
    expect.objectContaining({ credentials: "include" })
  );
});

test("account overview combines account positions and remote activity", async () => {
  const responses = [
    { accountName: "TqKq 模拟账户", dataStatus: "normal", equity: 100_000 },
    { items: [{ symbol: "SHFE.au2612", direction: "long", volume: 1 }] },
    { schemaVersion: 1, ordersInitialized: true, orders: [], trades: [], liveTrading: "disabled" },
    { configured: true, dataStatus: "normal", status: "normal", message: "预算内", dailyLossUsed: 0 }
  ];
  const fetchMock = vi.fn().mockImplementation(async () => new Response(
    JSON.stringify(responses.shift()),
    { status: 200, headers: { "Content-Type": "application/json" } },
  ));
  vi.stubGlobal("fetch", fetchMock);

  const snapshot = await getAccountOverview("http://127.0.0.1:43123");

  expect(snapshot.account.accountName).toBe("TqKq 模拟账户");
  expect(snapshot.positions[0]?.symbol).toBe("SHFE.au2612");
  expect(snapshot.activity.ordersInitialized).toBe(true);
  expect(snapshot.risk.status).toBe("normal");
  expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
    "http://127.0.0.1:43123/api/account/summary",
    "http://127.0.0.1:43123/api/positions",
    "http://127.0.0.1:43123/api/account/activity",
    "http://127.0.0.1:43123/api/risk/summary",
  ]);
});

test("market workbench requests one supported K-line duration for the selected symbol", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    schemaVersion: 1,
    durationSeconds: 300,
    capturedAt: "2026-08-31T04:00:00Z",
    ageSeconds: 0,
    dataStatus: "normal",
    isCached: false,
    liveTrading: "disabled",
    quote: { symbol: "DCE.i2609", lastPrice: 815 },
    bars: []
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);

  const result = await getMarketWorkbench("http://127.0.0.1:43123", "DCE.i2609", 300);

  expect(result.quote.symbol).toBe("DCE.i2609");
  expect(fetchMock).toHaveBeenCalledWith(
    "http://127.0.0.1:43123/api/markets/DCE.i2609/workbench?duration=300",
    expect.objectContaining({ credentials: "include" })
  );
});

test("manual recovery posts the user's own resume once and rejects failures", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:"ai_execution_owner_required"}),{status:409}));
  vi.stubGlobal("fetch",fetchMock);
  await expect(resumeAiExecution("https://runtime.example.test","s1","SHFE.rb2610")).rejects.toThrow();
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0][0]).toBe("https://runtime.example.test/api/ai-execution/resume");
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({sessionId:"s1",symbol:"SHFE.rb2610"});
});

test("every runtime request reports the client build and records the compat answer", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ items: [] }), {
    status: 200, headers: { "Content-Type": "application/json",
      "X-AI-Trader-Client-Compat": "outdated", "X-AI-Trader-Min-Client": "1.6.6", "X-AI-Trader-Runtime": "1.6.6+deadbee" }
  }));
  vi.stubGlobal("fetch", fetchMock);

  await listInstruments("http://127.0.0.1:43123");

  const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
  expect(new Headers(request.headers).get("X-AI-Trader-Client")).toBe(clientHeaderValue());
  expect(readClientCompat()).toEqual({ state: "outdated", minVersion: "1.6.6", runtime: "1.6.6+deadbee" });
});

test("a caller-provided client header is not overwritten and errors still record the answer", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ detail: "nope" }), {
    status: 409, headers: { "X-AI-Trader-Client-Compat": "ok", "X-AI-Trader-Runtime": "1.6.6+deadbee" }
  }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(listInstruments("http://127.0.0.1:43123")).rejects.toThrow();
  expect(readClientCompat()).toEqual({ state: "ok", minVersion: null, runtime: "1.6.6+deadbee" });

  // An agent-side caller may state its own build; runtimeFetch must keep it.
  await runtimeFetch("http://127.0.0.1:43123/api/agent/call", { headers: { "X-AI-Trader-Client": "agent/9.9.9" } });
  const sent = fetchMock.mock.calls[1]?.[0] as Request;
  expect(sent.headers.get("X-AI-Trader-Client")).toBe("agent/9.9.9");
});

test("the active trading account rides along on every request until the session resets", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => new Response(
    JSON.stringify({ accounts: [], current: "tqkq-sim:default" }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  ));
  vi.stubGlobal("fetch", fetchMock);

  setActiveTradingAccount("tqkq-sim:abc123def456");
  await fetchAccountDirectory("http://127.0.0.1:43123");

  const scoped = fetchMock.mock.calls[0]?.[1] as RequestInit;
  expect(new Headers(scoped.headers).get("X-AI-Trader-Account")).toBe("tqkq-sim:abc123def456");

  // The switch itself names its target in the body; a session reset clears the header.
  await setSessionTradingAccount("http://127.0.0.1:43123", "session-1", "tqkq-sim:default");
  const put = fetchMock.mock.calls[1]?.[1] as RequestInit;
  expect(put.method).toBe("PUT");
  expect(JSON.parse(String(put.body))).toEqual({ accountId: "tqkq-sim:default" });

  setActiveTradingAccount(null);
  await fetchAccountDirectory("http://127.0.0.1:43123");
  const unscoped = fetchMock.mock.calls[2]?.[1] as RequestInit;
  expect(new Headers(unscoped.headers).has("X-AI-Trader-Account")).toBe(false);
});

test("the web event stream reconnects after the 502 of a deploy switch (10-08)", async () => {
  // A native EventSource closed for good on one 502; every open web page stayed offline.
  vi.useFakeTimers();
  try {
    const calls: { credentials?: RequestCredentials; auth: string | null; lastId: string | null }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      calls.push({ credentials: init?.credentials, auth: headers.get("Authorization"), lastId: headers.get("Last-Event-ID") });
      if (calls.length === 1) return new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode("id: 7\ndata: {}\n\n")); c.close(); } }), { status: 200 });
      if (calls.length === 2) return new Response("bad gateway", { status: 502 });
      return new Response(new ReadableStream({ start() { /* stays open */ } }), { status: 200 });
    }));
    const connection: boolean[] = [];
    const stop = openEventStream("https://runtime.test", "s1", () => undefined, () => undefined, () => undefined, ok => connection.push(ok));
    await vi.advanceTimersByTimeAsync(10);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls.length).toBe(3);
    expect(calls.every(call => call.credentials === "include" && call.auth === null)).toBe(true);
    expect(calls[2].lastId).toBe("7");
    expect(connection.at(-1)).toBe(true);
    stop();
  } finally {
    vi.useRealTimers();
  }
});

/** A fetch that never answers on its own and rejects like the browser once its signal aborts. */
function hangingFetch() {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
    signal?.addEventListener("abort", () => reject(signal.reason ?? new DOMException("Aborted", "AbortError")));
  }));
}

test("a hung read ends with a timeout error after the default limit instead of waiting forever (#712 B1)", async () => {
  vi.useFakeTimers();
  try {
    const fetchMock = hangingFetch();
    vi.stubGlobal("fetch", fetchMock);
    const outcome = requestJson("https://runtime.example.test/api/app/help").then(() => "resolved", (error: unknown) => error);
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS - 1);
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const error = await outcome;
    expect(isRequestTimeout(error)).toBe(true);
    expect(error).toMatchObject({ status: 0, message: "应用服务 30 秒内没有响应，请稍后重试" });
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
  } finally {
    vi.useRealTimers();
  }
});

test("a caller's own abort still rejects as AbortError, a write has no default limit, and timeoutMs overrides", async () => {
  vi.useFakeTimers();
  try {
    vi.stubGlobal("fetch", hangingFetch());
    const controller = new AbortController();
    const aborted = requestJson("https://runtime.example.test/api/market/board", { signal: controller.signal }).catch((error: unknown) => error);
    controller.abort();
    const abortError = await aborted;
    expect((abortError as DOMException).name).toBe("AbortError");
    expect(isRequestTimeout(abortError)).toBe(false);

    let writeSettled = false;
    void requestJson("https://runtime.example.test/api/strategy-backtests", { method: "POST", body: "{}" })
      .catch(() => undefined).finally(() => { writeSettled = true; });
    let researchSettled = false;
    const research = requestJson("https://runtime.example.test/api/research/desk?symbol=SHFE.rb2701", { timeoutMs: LONG_READ_TIMEOUT_MS })
      .catch((error: unknown) => error).finally(() => { researchSettled = true; });
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS + 1);
    expect(researchSettled).toBe(false);
    await vi.advanceTimersByTimeAsync(LONG_READ_TIMEOUT_MS);
    expect(isRequestTimeout(await research)).toBe(true);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(writeSettled).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});

test("the generated client's transport and guest reads share the same default limit; their writes have none", async () => {
  vi.useFakeTimers();
  try {
    const fetchMock = hangingFetch();
    vi.stubGlobal("fetch", fetchMock);
    const generatedRead = runtimeFetch(new Request("https://runtime.example.test/api/app/config")).catch((error: unknown) => error);
    const guestRead = guestRequest("https://runtime.example.test", "/api/guest/instruments").catch((error: unknown) => error);
    let writesSettled = false;
    void Promise.allSettled([
      runtimeFetch(new Request("https://runtime.example.test/api/app/config", { method: "PUT", body: "{}" })),
      guestRequest("https://runtime.example.test", "/api/auth/guest", { method: "POST" }),
    ]).then(() => { writesSettled = true; });
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS);
    expect(isRequestTimeout(await generatedRead)).toBe(true);
    expect(isRequestTimeout(await guestRead)).toBe(true);
    // A read timeout from any of the three transports is one the recovery paths retry (#710 P2).
    expect(isTransientFailure(await generatedRead)).toBe(true);
    expect(isTransientFailure(await guestRead)).toBe(true);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(writesSettled).toBe(false);
    // The generated client's own abort still ends its read as an AbortError.
    const controller = new AbortController();
    const aborted = runtimeFetch(new Request("https://runtime.example.test/api/app/activity", { signal: controller.signal })).catch((error: unknown) => error);
    controller.abort();
    expect((await aborted as DOMException).name).toBe("AbortError");
  } finally {
    vi.useRealTimers();
  }
});

test("a generated-client read whose headers arrive but whose body stalls still times out; a whole body comes back intact", async () => {
  vi.useFakeTimers();
  try {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new ReadableStream({ start() { /* headers sent, body never ends */ } }), { status: 200 })));
    const stalled = runtimeFetch(new Request("https://runtime.example.test/api/app/activity")).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS);
    expect(isRequestTimeout(await stalled)).toBe(true);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ revision: 3 }, { status: 201, headers: { "X-Test": "kept" } })));
    const whole = await runtimeFetch(new Request("https://runtime.example.test/api/app/config"));
    expect([whole.status, whole.headers.get("X-Test"), await whole.json()]).toEqual([201, "kept", { revision: 3 }]);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    expect((await runtimeFetch(new Request("https://runtime.example.test/api/app/config", { method: "PUT", body: "{}" }))).status).toBe(204);
  } finally {
    vi.useRealTimers();
  }
});

test("transient failures: transport, 5xx and read timeouts retry; 4xx, caller aborts, write timeouts and other status-0 errors stay final", async () => {
  vi.useFakeTimers();
  try {
    vi.stubGlobal("fetch", hangingFetch());
    const read = requestJson("https://runtime.example.test/api/workspace").catch((error: unknown) => error);
    // A write given an explicit limit still times out, but its outcome is unknown: never a blind resend.
    const write = requestJson("https://runtime.example.test/api/sessions/restore", { method: "POST", body: "{}", timeoutMs: 5_000 }).catch((error: unknown) => error);
    const controller = new AbortController();
    const aborted = requestJson("https://runtime.example.test/api/workspace", { signal: controller.signal }).catch((error: unknown) => error);
    controller.abort();
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS);
    expect(isTransientFailure(await read)).toBe(true);
    expect(isRequestTimeout(await write)).toBe(true);
    expect(isTransientFailure(await write)).toBe(false);
    expect(isTransientFailure(await aborted)).toBe(false);
    expect(isTransientFailure(new TypeError("Failed to fetch"))).toBe(true);
    expect(isTransientFailure(Object.assign(new Error("502"), { status: 502 }))).toBe(true);
    expect(isTransientFailure(Object.assign(new Error("401"), { status: 401 }))).toBe(false);
    expect(isTransientFailure(Object.assign(new Error("409"), { status: 409 }))).toBe(false);
    expect(isTransientFailure(Object.assign(new Error("other"), { status: 0 }))).toBe(false);
    expect(isTransientFailure(undefined)).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});

test("a read that answers in time is unaffected and leaves no timer behind", async () => {
  vi.useFakeTimers();
  try {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ok: true })));
    await expect(requestJson("https://runtime.example.test/api/app/help")).resolves.toEqual({ ok: true });
    expect(vi.getTimerCount()).toBe(0);
  } finally {
    vi.useRealTimers();
  }
});
