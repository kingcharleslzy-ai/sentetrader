import { act, renderHook } from "@testing-library/react";
import { useBoards } from "./boards";
import type { PreferencesDocument } from "../../userPreferences";

test("an undo failure after history was reset cannot restore the abandoned board's undo step", async () => {
  let fail!: (error: Error) => void;
  const update = vi.fn().mockResolvedValueOnce({} as PreferencesDocument)
    .mockImplementationOnce(() => new Promise<PreferencesDocument>((_, reject) => { fail = reject; }));
  const { result } = renderHook(() => useBoards({ document: undefined, connected: true, update }));
  await act(async () => { result.current.apply({ lock: { module: "chart", size: "small" } }); });
  expect(result.current.undo).toBeDefined();
  act(() => { result.current.undo!(); });
  // Switching boards, leaving editing, or starting a new edit session clears history.
  act(() => { result.current.resetUndo(); });
  await act(async () => { fail(new Error("save unavailable")); });
  expect(result.current.undo).toBeUndefined();
});
