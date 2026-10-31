import { act, renderHook } from "@testing-library/react";
import { useAiExecution } from "./useAiExecution";

// The real api module runs against a stubbed fetch so the stop request body is asserted end to end.
const fetchMock = vi.hoisted(() => vi.fn());
const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), {status: 200, headers: {"Content-Type": "application/json"}});
const snapshot = (stopStatus: string | null) => ({controls: {stopStatus, generation: 1, revision: 1, maxOrderVolume: null, maxOrders: null}, authorization: null, stops: [], simulationOnly: true, liveTrading: "disabled" as const});
const context = {identity: 0};
const setToast = vi.fn();
const handle = {
  baseUrl: "https://runtime.test", connection: "connected" as const, sessionId: undefined, replayPath: "",
  fence: { context, isCurrent: () => true, channel: () => ({ beginRead: () => () => true, noteWrite: () => undefined }) },
  orderContext: context, isCurrentOrderContext: () => true, setToast
};
function mount() {
  return renderHook(() => useAiExecution({handle, accountStatus: "normal", feed: {channels: {}}, symbol: "SHFE.au2612", refreshSimulationOrders: async () => undefined}));
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) =>
    jsonResponse(String(input).endsWith("/api/ai-execution/stop") && init?.method === "POST"
      ? {authorization: null, emergency: false, scope: "opens", pausedStrategyIds: [], liveTrading: "disabled"}
      : snapshot("HALT_OPENS")));
});
afterEach(() => vi.unstubAllGlobals());
test("scope=opens sends the stop body verbatim and keeps strategies running", async () => {
  const hook = mount();
  await act(async () => { await hook.result.current.deactivateAiExecution(false, "opens"); });
  const stopCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith("/api/ai-execution/stop") && init?.method === "POST");
  expect(stopCall?.[1]?.body).toBe('{"emergency":false,"scope":"opens"}');
  expect(setToast).toHaveBeenCalledWith("已只停新开仓，AI 不再开新仓；平仓与保护单照常");
  expect(hook.result.current.pausedStrategyIds).toEqual([]);
  hook.unmount();
});
test("the emergency stop keeps its body and message", async () => {
  const hook = mount();
  await act(async () => { await hook.result.current.deactivateAiExecution(true); });
  const stopCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith("/api/ai-execution/stop") && init?.method === "POST");
  expect(stopCall?.[1]?.body).toBe('{"emergency":true,"scope":"all"}');
  expect(setToast).toHaveBeenCalledWith("AI 模拟执行已急停");
  hook.unmount();
});
const armedStop = {stopId: "stop-1", symbol: "SHFE.au2612", positionDirection: "long", positionBucket: "all", volume: 1, triggerPrice: 820, takeProfitPrice: null, status: "ARMED"};
test("a protection that landed is reported as landed even when the status refresh fails", async () => {
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("/api/ai-execution/stops") && init?.method === "POST") return jsonResponse(armedStop);
    return new Response("upstream", {status: 502});
  });
  const hook = renderHook(() => useAiExecution({handle: {...handle, sessionId: "session-1"}, accountStatus: "normal", feed: {channels: {}}, symbol: "SHFE.au2612", refreshSimulationOrders: async () => undefined}));
  let ok: boolean | undefined;
  await act(async () => {
    ok = await hook.result.current.createPositionStop({symbol: "SHFE.au2612", positionDirection: "long", positionBucket: "all", volume: 1, triggerPrice: 820});
  });
  // Reporting 挂保护没成功 here would make the user submit a second, overlapping protection.
  expect(ok).toBe(true);
  expect(setToast).toHaveBeenCalledWith("保护已挂上：止损 820 · 1 手；状态刷新失败，稍后自动更新", "warning");
  expect(setToast).not.toHaveBeenCalledWith(expect.stringContaining("没成功"), "error");
  hook.unmount();
});
test("cancelling a protection that already fired says so instead of a raw failure", async () => {
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) =>
    String(input).endsWith("/cancel") && init?.method === "POST"
      ? new Response(JSON.stringify({detail: {code: "stop_not_cancelable"}}), {status: 409, headers: {"Content-Type": "application/json"}})
      : jsonResponse(snapshot(null)));
  const hook = mount();
  await act(async () => { await hook.result.current.cancelPositionStop("stop-1"); });
  expect(setToast).toHaveBeenCalledWith("这条保护已经触发或撤销了，撤不了", "error");
  hook.unmount();
});
