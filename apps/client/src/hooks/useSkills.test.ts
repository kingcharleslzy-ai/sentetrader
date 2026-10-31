import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { getAgentSkills } from "../api";
import { SKILLS_REFRESH_MS, useSkills } from "./useSkills";

vi.mock("../api", () => ({ getAgentSkills: vi.fn() }));

const runtime = { version: "1.7.0", account: { id: "acc-1" } } as never;
const skill = (id: string, enabled: boolean) => ({ id, enabled }) as never;

test("a tier change made in 后台 shows when the window comes back to the front, without a reload", async () => {
  const read = vi.mocked(getAgentSkills);
  read.mockResolvedValueOnce({ items: [skill("backtests", false)] } as never);
  const { result } = renderHook(() => useSkills("https://runtime.test", runtime));
  await waitFor(() => expect(result.current.skills).toEqual([skill("backtests", false)]));
  read.mockResolvedValueOnce({ items: [skill("backtests", true)] } as never);
  act(() => { window.dispatchEvent(new Event("focus")); });
  await waitFor(() => expect(result.current.skills).toEqual([skill("backtests", true)]));
  // A refetch that fails keeps what was read instead of dropping to the legacy feature view.
  read.mockRejectedValueOnce(new Error("offline"));
  act(() => { window.dispatchEvent(new Event("focus")); });
  await waitFor(() => expect(read).toHaveBeenCalledTimes(3));
  await act(async () => { await Promise.resolve(); });
  expect(result.current.skills).toEqual([skill("backtests", true)]);
});

test("a window that stays in front reads the registry again every few minutes", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const read = vi.mocked(getAgentSkills);
    read.mockReset();
    read.mockResolvedValueOnce({ items: [skill("backtests", true)] } as never);
    const { result } = renderHook(() => useSkills("https://runtime.test", runtime));
    await waitFor(() => expect(result.current.skills).toEqual([skill("backtests", true)]));
    // The owner lowers the tier in 后台; no focus change follows.
    read.mockResolvedValueOnce({ items: [skill("backtests", false)] } as never);
    await act(async () => { vi.advanceTimersByTime(SKILLS_REFRESH_MS); });
    await waitFor(() => expect(result.current.skills).toEqual([skill("backtests", false)]));
  } finally {
    vi.useRealTimers();
  }
});
