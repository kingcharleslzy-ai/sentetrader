import { act, renderHook } from "@testing-library/react";
import { GuestAppearanceContext } from "../guestAppearance";
import { useState, type ReactNode } from "react";
import { requestJson } from "../api";
import { defaultPreferences, useAccountPreferences, type AccountPreferences } from "./useAccountPreferences";

vi.mock("../api", () => ({ requestJson: vi.fn() }));
const request = vi.mocked(requestJson);

beforeEach(() => { localStorage.clear(); vi.useFakeTimers(); request.mockReset(); });
afterEach(() => { vi.useRealTimers(); });

function useHarness(scope = "owner") {
  const [value, setValue] = useState<AccountPreferences>(defaultPreferences);
  const status = useAccountPreferences("https://runtime.test", scope, value, setValue);
  return { value, setValue, status };
}

test("cloud preference wins and offline edits cannot overwrite a newer remote revision", async () => {
  let remote = { revision: 3, preferences: { ...defaultPreferences, theme: "light" as const } };
  const writes: unknown[] = [];
  request.mockImplementation(async (_url, options) => {
    if (options?.method === "PUT") { writes.push(JSON.parse(String(options.body))); throw new TypeError("offline"); }
    return structuredClone(remote);
  });
  const hook = renderHook(() => useHarness());
  await act(async () => {});
  expect(hook.result.current.value.theme).toBe("light");
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  act(() => hook.result.current.setValue({ ...hook.result.current.value, density: "compact" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(writes).toHaveLength(1);
  remote = { revision: 4, preferences: { ...remote.preferences, fontScale: "extra-large" } };
  await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
  expect(writes.every(write => (write as { revision: number }).revision === 3)).toBe(true);
  expect(hook.result.current.value).toEqual(remote.preferences);
  hook.unmount();
});

test("AI 审计 2026-10-13：窗口有损地显示账号偏好时，之后改主题/字号照样写回账号", async () => {
  // The workspace renders the document lossily: the pane switches come from the settings document and an
  // empty pin list shows the current contract. Waiting for an exact match blocked every later save.
  const lossy = (value: AccountPreferences): AccountPreferences => ({ ...value, chart: { ...value.chart, kd: false },
    pinnedSymbols: value.pinnedSymbols?.length ? value.pinnedSymbols : ["SHFE.rb2701"] });
  function useLossyHarness() {
    const [value, setValue] = useState<AccountPreferences>(defaultPreferences);
    useAccountPreferences("https://runtime.test", "lossy", value, next => setValue(lossy(next)));
    return { value, setValue };
  }
  const remote = { revision: 7, preferences: { ...defaultPreferences, theme: "dark" as const } };
  const writes: { revision: number; preferences: AccountPreferences }[] = [];
  request.mockImplementation(async (_url, options) => {
    if (options?.method === "PUT") {
      const body = JSON.parse(String(options.body));
      writes.push(body);
      return { revision: body.revision + 1, preferences: body.preferences };
    }
    return structuredClone(remote);
  });
  const hook = renderHook(() => useLossyHarness());
  await act(async () => { await vi.advanceTimersByTimeAsync(3200); });
  expect(writes).toEqual([]);  // the lossy render itself is not a user edit
  act(() => hook.result.current.setValue({ ...hook.result.current.value, theme: "light", fontScale: "extra-large" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(100); });
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ revision: 7, preferences: { theme: "light", fontScale: "extra-large" } });
  hook.unmount();
});

test("reload preserves a pending edit only when the server revision is unchanged", async () => {
  let remote = { revision: 1, preferences: defaultPreferences };
  let writes = 0;
  request.mockImplementation(async (_url, options) => {
    if (options?.method === "PUT") {
      writes++;
      if (writes === 1) return new Promise(() => {});
      const body = JSON.parse(String(options.body));
      expect(body.revision).toBe(remote.revision);
      remote = { revision: remote.revision + 1, preferences: body.preferences };
    }
    return structuredClone(remote);
  });
  const first = renderHook(() => useHarness());
  await act(async () => {});
  await act(async () => first.result.current.setValue({ ...defaultPreferences, theme: "light" }));
  expect(writes).toBe(1);
  first.unmount();
  const restored = renderHook(() => useHarness());
  await act(async () => { await vi.advanceTimersByTimeAsync(20); });
  expect(restored.result.current.value.theme).toBe("light");
  expect(remote.preferences.theme).toBe("light");
  expect(remote.revision).toBe(2);
  restored.unmount();
});

test("late reads from a previous account cannot replace the new account", async () => {
  let release: (value: unknown) => void = () => {};
  request.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
  request.mockResolvedValue({ revision: 2, preferences: { ...defaultPreferences, density: "comfortable" } });
  const hook = renderHook(({ scope }) => useHarness(scope), { initialProps: { scope: "alice" } });
  hook.rerender({ scope: "bob" });
  await act(async () => {});
  await act(async () => { release({ revision: 99, preferences: { ...defaultPreferences, theme: "light" } }); });
  expect(hook.result.current.value.density).toBe("comfortable");
  expect(hook.result.current.value.theme).toBe("dark");
  expect(request.mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(0);
  hook.unmount();
});


test("guest login carries the viewed theme while retaining other remote preferences", async () => {
  let remote = { revision: 4, preferences: { ...defaultPreferences, theme: "dark" as const, density: "comfortable" as const } };
  request.mockImplementation(async (_url, options) => {
    if (options?.method === "PUT") {
      const body = JSON.parse(String(options.body));
      expect(body.revision).toBe(4);
      remote = { revision: 5, preferences: body.preferences };
    }
    return structuredClone(remote);
  });
  const hook = renderHook(() => useHarness(), { wrapper: ({ children }: { children: ReactNode }) => <GuestAppearanceContext.Provider value={{theme:"light",duration:900}}>{children}</GuestAppearanceContext.Provider> });
  await act(async () => { await vi.advanceTimersByTimeAsync(1600); });
  expect(hook.result.current.value.theme).toBe("light");
  expect(hook.result.current.value.density).toBe("comfortable");
  expect(remote.preferences.theme).toBe("light");
  expect(remote.revision).toBe(5);
  hook.unmount();
});
