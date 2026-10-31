import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("./engineWorker.ts?worker&url", () => ({ default: "assets/engineWorker-test.js" }));

import { browserSupport, computeLab, engineStatus, pyodideUrl, resetBrowserEngine, ruleBacktest, StaleEngine, workerUrl } from "./browserEngine";

/** The engine's own report text: floats stay floats (3500.0), which a JavaScript round trip would lose. */
const REPORT = '{"summary":{"netProfit":3500.0,"trades":2},"coverage":{"tradingDays":3},"trades":[],"initialBalance":1000000.0}';
let workerMode: "ok" | "init-fails" | "crash" | "python-error" = "ok";
let workers: FakeWorker[] = [];
class FakeWorker {
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: ((event: { message: string; preventDefault?: () => void }) => void) | null = null;
  onmessageerror: (() => void) | null = null;
  terminated = false;
  received: Record<string, unknown>[] = [];
  constructor(readonly url: URL, readonly options: WorkerOptions) { workers.push(this); }
  postMessage(message: Record<string, unknown>) {
    this.received.push(message);
    queueMicrotask(() => {
      const id = message.id;
      if (message.type === "init") {
        this.onmessage?.({ data: workerMode === "init-fails" ? { id, ok: false, error: "TypeError: Failed to fetch pyodide.mjs" } : { id, ok: true, ms: 5 } });
      } else if (workerMode === "crash") this.onerror?.({ message: "worker died" });
      else if (workerMode === "python-error") this.onmessage?.({ data: { id, ok: false, error: "PythonError: BacktestDataError: 执行周期的历史K线不足" } });
      else this.onmessage?.({ data: { id, ok: true, ms: 42,
        text: message.type === "rule" ? REPORT : `{"variantId":${JSON.stringify((message.variant as { id: string }).id)},"inSample":{"net":1.0}}` } });
    });
  }
  terminate() { this.terminated = true; }
}

const posted: { url: string; body: string }[] = [];
let resultStatus = 200;
let resultNetworkFailures = 0;
let resultDetail = "";
let engineDigest = "digest-1";
function stubFetch() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(String(input), init);
    const url = request.url;
    const body = request.method === "GET" ? "" : await request.text();
    if (request.method !== "GET") posted.push({ url, body });
    const json = (value: unknown, status = 200) => new Response(typeof value === "string" ? value : JSON.stringify(value),
      { status, headers: { "Content-Type": "application/json" } });
    // The Runtime parses only a JSON body: a string body sent as text/plain is a 422 there.
    if (request.method !== "GET" && !request.headers.get("Content-Type")?.startsWith("application/json")) return json({ detail: "not json" }, 422);
    if (url.endsWith("/api/strategy-engine/bundle")) return json({ digest: engineDigest, modules: { "trader_runtime/__init__.py": "" }, entry: "trader_runtime.strategy_lab_engine", pyodide: { version: "314.0.7" } });
    if (url.endsWith("/api/strategy-backtests/inputs")) return json(`{"engineDigest":"digest-1","inputDigest":"in-1","ticketId":"t-1","inputs":{"priceTick":1.0}}`);
    if (url.endsWith("/api/strategy-backtests/browser-result") && resultNetworkFailures > 0) { resultNetworkFailures--; throw new TypeError("Failed to fetch"); }
    if (url.endsWith("/api/strategy-backtests/browser-result"))
      return resultStatus === 200 ? json({ backtestId: "b1", source: "browser", summary: { netProfit: 3500 } }) : json({ detail: resultDetail }, resultStatus);
    if (url.endsWith("/inputs")) return json(`{"engineDigest":"digest-1","variants":[{"id":"v1","label":"a"},{"id":"v2","label":"b"},{"id":"v3","label":"c"}],"series":{}}`);
    if (url.endsWith("/results")) return json({ id: "lab-1", status: "done" });
    return json({ detail: "not found" }, 404);
  }));
}

const settle = async () => { for (let index = 0; index < 20; index++) await Promise.resolve(); await new Promise(resolve => setTimeout(resolve, 0)); };
const server = vi.fn(async (reason: string) => ({ source: "server", fallbackReason: reason }));

beforeEach(() => {
  workerMode = "ok"; workers = []; posted.length = 0; resultStatus = 200; resultNetworkFailures = 0; resultDetail = ""; engineDigest = "digest-1";
  vi.stubGlobal("Worker", FakeWorker);
  Object.defineProperty(window.navigator, "hardwareConcurrency", { value: 8, configurable: true });
  stubFetch();
});
afterEach(() => {
  resetBrowserEngine(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); server.mockClear();
  delete (window.navigator as Partial<Navigator> & { hardwareConcurrency?: number }).hardwareConcurrency;
  delete (window.navigator as Partial<Navigator> & { userAgent?: string }).userAgent;
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
});

test("能不能本机算：没有 Worker 或 WebAssembly、核数不足、手机平板都走云端", () => {
  expect(browserSupport()).toBeNull();
  vi.stubGlobal("Worker", undefined);
  expect(browserSupport()).toBe("unsupported");
  vi.stubGlobal("Worker", FakeWorker);
  vi.stubGlobal("WebAssembly", undefined);
  expect(browserSupport()).toBe("unsupported");
  vi.unstubAllGlobals(); vi.stubGlobal("Worker", FakeWorker);
  Object.defineProperty(window.navigator, "hardwareConcurrency", { value: 2, configurable: true });
  expect(browserSupport()).toBe("device");
  Object.defineProperty(window.navigator, "hardwareConcurrency", { value: 8, configurable: true });
  Object.defineProperty(window.navigator, "userAgent", { value: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)", configurable: true });
  expect(browserSupport()).toBe("device");
});

test("Pyodide 与 worker 的地址：网页同源（自托管 v 前缀目录），桌面版取官网，构建变量优先；网页版 worker 按文件名走同源", () => {
  expect(pyodideUrl()).toBe(`${location.origin}/trader/pyodide/v314.0.7/`);
  (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
  expect(pyodideUrl()).toBe("https://qilinshuzhi.com/trader/pyodide/v314.0.7/");
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  vi.stubEnv("VITE_PYODIDE_URL", "https://mirror.example/py");
  expect(pyodideUrl()).toBe("https://mirror.example/py/");
  vi.stubEnv("VITE_ASSET_ORIGIN", "https://oss.example/web/");
  expect(workerUrl().href).toBe(`${location.origin}/assets/engineWorker-test.js`);
});

test("第一次引擎还在加载就走云端（warming），加载好后本机算：结果原样交回，不经 JS 转换", async () => {
  const first = await ruleBacktest("http://rt", { strategyId: "s1", symbol: "SHFE.rb2701" }, server);
  expect(first).toEqual({ source: "server", fallbackReason: "warming" });
  await settle();
  expect(engineStatus().state).toBe("ready");
  expect(workers[0].options).toEqual({ type: "module" });
  const local = await ruleBacktest<{ source: string }>("http://rt", { strategyId: "s1", symbol: "SHFE.rb2701" }, server);
  expect(local.source).toBe("browser");
  expect(server).toHaveBeenCalledTimes(1);
  const rule = workers[0].received.find(message => message.type === "rule")!;
  expect(rule.inputsJson).toBe(`{"engineDigest":"digest-1","inputDigest":"in-1","ticketId":"t-1","inputs":{"priceTick":1.0}}`);
  const result = posted.find(item => item.url.endsWith("/browser-result"))!;
  expect(result.body).toBe(`{"inputDigest":"in-1","ticketId":"t-1","engineDigest":"digest-1","runMs":42,"result":${REPORT}}`);
  expect(result.body).toContain('"netProfit":3500.0');
});

test("交回报告时回包丢了：同一张票据把同一份报告再交一次（Runtime 返回第一次的结果），仍失败才转云端", async () => {
  await ruleBacktest("http://rt", { strategyId: "s1" }, server); await settle();
  resultNetworkFailures = 1;
  const retried = await ruleBacktest<{ source: string }>("http://rt", { strategyId: "s1" }, server);
  expect(retried.source).toBe("browser");
  const handIns = posted.filter(item => item.url.endsWith("/browser-result"));
  expect(handIns).toHaveLength(2);
  expect(handIns[1].body).toBe(handIns[0].body);
  expect(server).toHaveBeenCalledTimes(1);           // only the warming run
  resultNetworkFailures = 2;
  expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server)).toEqual({ source: "server", fallbackReason: "inputs_failed" });
});

test("引擎已更新（409 engine_changed）不混用：提示刷新；服务端不记得这次输入（409 inputs_expired）改走云端", async () => {
  await ruleBacktest("http://rt", { strategyId: "s1" }, server); await settle();
  resultStatus = 409; resultDetail = "engine_changed";
  await expect(ruleBacktest("http://rt", { strategyId: "s1" }, server)).rejects.toBeInstanceOf(StaleEngine);
  resultDetail = "inputs_expired";
  expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server)).toEqual({ source: "server", fallbackReason: "inputs_expired" });
});

test("页面隐藏、worker 崩溃、引擎报错都自动转云端并写原因；崩溃后下次重新加载", async () => {
  await ruleBacktest("http://rt", { strategyId: "s1" }, server); await settle();
  Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
  expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server)).toEqual({ source: "server", fallbackReason: "hidden" });
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  workerMode = "python-error";
  expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server)).toEqual({ source: "server", fallbackReason: "engine_error" });
  workerMode = "crash";
  expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server)).toEqual({ source: "server", fallbackReason: "crashed" });
  expect(workers[0].terminated).toBe(true);
  workerMode = "ok";
  expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server)).toEqual({ source: "server", fallbackReason: "warming" });
  await settle();
  expect(workers).toHaveLength(2);
  expect((await ruleBacktest<{ source: string }>("http://rt", { strategyId: "s1" }, server)).source).toBe("browser");
});

test("运行中页面被隐藏：结束这次本机计算，改走云端", async () => {
  await ruleBacktest("http://rt", { strategyId: "s1" }, server); await settle();
  const original = FakeWorker.prototype.postMessage;
  FakeWorker.prototype.postMessage = function (this: FakeWorker, message: Record<string, unknown>) {
    if (message.type === "rule") {
      this.received.push(message);
      setTimeout(() => {
        Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
        document.dispatchEvent(new Event("visibilitychange"));
      }, 0);
      return;
    }
    original.call(this, message);
  };
  try {
    expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server)).toEqual({ source: "server", fallbackReason: "hidden" });
    expect(workers[0].terminated).toBe(true);
  } finally { FakeWorker.prototype.postMessage = original; }
});

test("Pyodide 加载失败：这一页之后都走云端（init_failed），不反复下载", async () => {
  workerMode = "init-fails";
  await ruleBacktest("http://rt", { strategyId: "s1" }, server); await settle();
  expect(engineStatus()).toMatchObject({ state: "off", reason: "init_failed" });
  expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server)).toEqual({ source: "server", fallbackReason: "init_failed" });
  expect(workers).toHaveLength(1);
});

test("“总在云端”的权威复核（where: server）不经过浏览器", async () => {
  expect(await ruleBacktest("http://rt", { strategyId: "s1" }, server, "server")).toEqual({ source: "server", fallbackReason: "rerun" });
  expect(workers).toHaveLength(0);
});

test("比一比：clamp(核数−2, 1, 3) 个 worker 并行算三组，结果原样一次交回", async () => {
  await ruleBacktest("http://rt", { strategyId: "s1" }, server); await settle();
  const progress: number[] = [];
  await computeLab("http://rt", "lab-1", done => progress.push(done));
  expect(workers).toHaveLength(3);                            // the kept one + 2 fresh (8 cores → 3)
  expect(workers.slice(1).every(worker => worker.terminated)).toBe(true);
  expect(workers[0].terminated).toBe(false);
  expect(progress).toEqual([1, 2, 3]);
  const results = posted.find(item => item.url.endsWith("/lab-1/results"))!;
  expect(results.body).toBe('{"engineDigest":"digest-1","results":[{"variantId":"v1","inSample":{"net":1.0}},{"variantId":"v2","inSample":{"net":1.0}},{"variantId":"v3","inSample":{"net":1.0}}]}');
});
