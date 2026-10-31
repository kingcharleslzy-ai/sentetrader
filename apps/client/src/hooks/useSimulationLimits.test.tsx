import { act, renderHook } from "@testing-library/react";
import { getAiExecution, requestJson, updateSimulationLimits, type AiExecutionControls } from "../api";
import { useSimulationLimits } from "./useSimulationLimits";

vi.mock("../api", () => ({getAiExecution: vi.fn(), requestJson: vi.fn(), updateSimulationLimits: vi.fn()}));
vi.mock("../runtimeClient", () => ({readIdentityGeneration: () => 0}));
const key = "ai-trader-simulation-limits-v2:owner:cloud";
const context = {identity: 0};
const fence = { context, isCurrent: () => true, channel: () => ({ beginRead: () => () => true, noteWrite: () => undefined }) };
let remote: AiExecutionControls;
const limits = {enabled: true, maxOrderVolume: 50, maxOrders: 100};
function mount(connected = true) {
  return renderHook(({online}) => useSimulationLimits({baseUrl: "https://runtime.test", rootBaseUrl: "https://runtime.test",
    scope: "owner:cloud", fence, connected: online}), {initialProps: {online: connected}});
}
beforeEach(() => {
  localStorage.clear(); vi.resetAllMocks();
  remote = {revision: 1, generation: 4, stopStatus: "EMERGENCY_STOP", maxOrderVolume: 10, maxOrders: 100};
  vi.mocked(requestJson).mockResolvedValue({scope: "owner"});
  vi.mocked(getAiExecution).mockImplementation(async () => ({controls: {...remote}} as Awaited<ReturnType<typeof getAiExecution>>));
  vi.mocked(updateSimulationLimits).mockImplementation(async (_url, body) => {
    if (body.revision !== remote.revision) throw new Error("simulation_limits_conflict");
    remote = {...remote, ...body, revision: remote.revision + 1}; return {...remote};
  });
});
test.each([false, true])("offline edit preserves its base across reload, remote changed=%s", async changed => {
  const first = mount(); await act(async () => {});
  first.rerender({online: false});
  act(() => first.result.current.change(limits)); first.unmount();
  if (changed) remote = {...remote, revision: 2, maxOrderVolume: 1};
  const restored = mount(); await act(async () => {});
  expect(remote.maxOrderVolume).toBe(changed ? 1 : 50);
  expect(updateSimulationLimits).toHaveBeenCalledTimes(changed ? 0 : 1);
  expect(remote.stopStatus).toBe("EMERGENCY_STOP"); expect(remote.generation).toBe(4);
  if (changed) {
    expect(restored.result.current.status).toContain("草稿已保留");
    expect(restored.result.current.limits).toEqual(limits);
    act(() => restored.result.current.change({...limits, maxOrderVolume: 2})); await act(async () => {});
    expect(remote.maxOrderVolume).toBe(2);
  }
  restored.unmount();
});
test("lost response is reconciled without repeating a successful write", async () => {
  const write = vi.mocked(updateSimulationLimits).getMockImplementation()!;
  vi.mocked(updateSimulationLimits).mockImplementation(async (...args) => {await write(...args); throw new Error("response lost");});
  const first = mount(); await act(async () => {});
  act(() => first.result.current.change(limits)); await act(async () => {}); first.unmount();
  const restored = mount(); await act(async () => {});
  expect(updateSimulationLimits).toHaveBeenCalledTimes(1);
  expect(remote.revision).toBe(2); expect(restored.result.current.status).toBe("已生效");
  restored.unmount();
});
test("legacy pending draft without a base cannot overwrite server limits", async () => {
  localStorage.setItem(key, JSON.stringify({limits, pending: true}));
  const hook = mount(); await act(async () => {});
  expect(updateSimulationLimits).not.toHaveBeenCalled();
  expect(hook.result.current.status).toContain("编辑基准缺失"); hook.unmount();
});
