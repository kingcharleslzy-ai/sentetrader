import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ACCOUNT_NAVIGATION_WAIT_MS, useAccountNavigation, type AccountNavigationDeps, type SwitchOutcome } from "./useAccountNavigation";

afterEach(() => { vi.useRealTimers(); });

// Status timestamps as the Runtime writes them (ISO, one clock): t(5) is newer than t(4).
const t = (second: number) => `2026-10-19T01:00:${String(second).padStart(2, "0")}.000000+00:00`;

/** A PUT the test answers when it wants: ok(statusAt) = taken with the status it published, fail("…") = refused. */
function slowSwitch() {
  const calls: { accountId: string; ok: (statusAt: string | null) => void; fail: (error: string) => void }[] = [];
  const requestSwitch = vi.fn((accountId: string) => new Promise<SwitchOutcome>(resolve => {
    calls.push({ accountId, ok: statusAt => resolve({ statusAt }), fail: error => resolve({ error }) });
  }));
  return { calls, requestSwitch };
}

function setup(initial: Partial<AccountNavigationDeps> & Pick<AccountNavigationDeps, "requestSwitch">) {
  const onNotice = vi.fn();
  const base: AccountNavigationDeps = { currentId: "A", statusAt: t(1), sessionId: "s1", runtimeUrl: "http://runtime", known: () => true, onNotice, ...initial };
  const view = renderHook((deps: AccountNavigationDeps) => useAccountNavigation(deps), { initialProps: base });
  let props = base;
  return { ...view, onNotice, show: (over: Partial<AccountNavigationDeps>) => { props = { ...props, ...over }; view.rerender(props); } };
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

it("a reminder without an account opens at once; an account reminder opens only on a status published for its switch (slow PUT)", async () => {
  const runtime = slowSwitch();
  const view = setup({ requestSwitch: runtime.requestSwitch });
  const anywhere = vi.fn();
  act(() => view.result.current.navigate(null, anywhere));
  expect(anywhere).toHaveBeenCalledTimes(1);
  expect(runtime.requestSwitch).not.toHaveBeenCalled();

  const there = vi.fn();
  act(() => view.result.current.navigate("B", there));
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B"]);
  view.show({ currentId: "B", statusAt: t(2) });   // B's status arrives before the PUT answers
  expect(there).not.toHaveBeenCalled();             // the PUT is still on its way
  await act(async () => { runtime.calls[0].ok(t(2)); });
  expect(there).toHaveBeenCalledTimes(1);           // the shown status is the one published for this switch
  view.show({ currentId: "B", statusAt: t(3) });
  expect(there).toHaveBeenCalledTimes(1);

  // The shown account itself still asks the Runtime: it answers with a fresh status, and that one opens it.
  const same = vi.fn();
  act(() => view.result.current.navigate("B", same));
  await flush();
  await act(async () => { runtime.calls[1].ok(t(4)); });
  expect(same).not.toHaveBeenCalled();              // B shown, but from a status older than this answer
  view.show({ statusAt: t(4) });
  expect(same).toHaveBeenCalledTimes(1);
});

it("a refused switch says why and opens nothing; an account outside the directory never switches", async () => {
  const runtime = slowSwitch();
  const view = setup({ requestSwitch: runtime.requestSwitch, known: accountId => accountId !== "X" });
  const there = vi.fn();
  act(() => view.result.current.navigate("B", there));
  await flush();
  await act(async () => { runtime.calls[0].fail("该账户正在排队，稍后再试"); });
  expect(view.onNotice).toHaveBeenCalledWith("没有切到这条提醒的账户，仍停在当前账户：该账户正在排队，稍后再试");
  view.show({ currentId: "B", statusAt: t(9) });   // a later switch by other means does not open the old target
  expect(there).not.toHaveBeenCalled();

  const unknown = vi.fn();
  act(() => view.result.current.navigate("X", unknown));
  expect(view.onNotice).toHaveBeenLastCalledWith("这条提醒的交易账户不在目录里或未授权，仍停在当前账户");
  expect(runtime.requestSwitch).toHaveBeenCalledTimes(1);
  expect(unknown).not.toHaveBeenCalled();
});

it("A→B→A: switches go one after another, and a late status of B or the A shown from before never opens the A target (Codex r4307948000)", async () => {
  const runtime = slowSwitch();
  const view = setup({ requestSwitch: runtime.requestSwitch });
  const toB = vi.fn();
  const backToA = vi.fn();
  act(() => view.result.current.navigate("B", toB));
  await flush();
  await act(async () => { runtime.calls[0].ok(t(2)); });   // B taken; the window still shows A from t(1)
  act(() => view.result.current.navigate("A", backToA));
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B", "A"]);
  await act(async () => { runtime.calls[1].ok(t(3)); });   // A answered before any status arrived
  expect(backToA).not.toHaveBeenCalled();                  // A is shown, but from t(1): not this switch's status
  view.show({ currentId: "B", statusAt: t(2) });          // B's late status
  expect(backToA).not.toHaveBeenCalled();
  view.show({ currentId: "A", statusAt: t(3) });
  expect(backToA).toHaveBeenCalledTimes(1);
  expect(toB).not.toHaveBeenCalled();

  // A navigation replaced before its turn sends nothing; the newer one goes once the write before it answered.
  const toC = vi.fn();
  act(() => view.result.current.navigate("D", vi.fn()));
  act(() => view.result.current.navigate("C", toC));
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B", "A", "C"]);
  await act(async () => { runtime.calls[2].ok(t(5)); });
  view.show({ currentId: "C", statusAt: t(4) });
  expect(toC).not.toHaveBeenCalled();
  view.show({ currentId: "C", statusAt: t(5) });
  expect(toC).toHaveBeenCalledTimes(1);
});

it("an older Runtime answers without a status mark: the shown account decides, as before", async () => {
  const runtime = slowSwitch();
  const view = setup({ requestSwitch: runtime.requestSwitch });
  const there = vi.fn();
  act(() => view.result.current.navigate("B", there));
  await flush();
  await act(async () => { runtime.calls[0].ok(null); });
  view.show({ currentId: "B", statusAt: t(2) });
  expect(there).toHaveBeenCalledTimes(1);
});

it("a PUT that does not answer: the navigation gives up visibly; the write is never cut off or sent twice (fake clock, Codex r4307841907 / r4307899290)", async () => {
  vi.useFakeTimers();
  const runtime = slowSwitch();
  const view = setup({ requestSwitch: runtime.requestSwitch });
  const stalled = vi.fn();
  act(() => view.result.current.navigate("B", stalled));
  await flush();
  await act(async () => { vi.advanceTimersByTime(ACCOUNT_NAVIGATION_WAIT_MS + 1); });
  expect(view.onNotice).toHaveBeenCalledWith("切换账户还没有回应，没有打开这条提醒；稍后再点一次");
  const next = vi.fn();
  act(() => view.result.current.navigate("C", next));
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B"]);   // waits for B's outcome
  await act(async () => { runtime.calls[0].ok(t(2)); });
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B", "C"]);
  await act(async () => { runtime.calls[1].ok(t(3)); });
  view.show({ currentId: "C", statusAt: t(3) });
  expect(next).toHaveBeenCalledTimes(1);
  expect(stalled).not.toHaveBeenCalled();

  // A status that never comes gives up too.
  const late = vi.fn();
  act(() => view.result.current.navigate("D", late));
  await flush();
  await act(async () => { runtime.calls[2].ok(t(4)); });
  await act(async () => { vi.advanceTimersByTime(ACCOUNT_NAVIGATION_WAIT_MS + 1); });
  expect(view.onNotice).toHaveBeenLastCalledWith("账户切换还没完成，仍停在当前账户；稍后再点这条提醒");
  view.show({ currentId: "D", statusAt: t(4) });
  expect(late).not.toHaveBeenCalled();
});

it("B pending → the user chooses C: the reminder navigation is abandoned and C is written after B, so the session ends on C (Codex r4308164454 / r4308219648)", async () => {
  const runtime = slowSwitch();
  const view = setup({ requestSwitch: runtime.requestSwitch });
  const toB = vi.fn();
  act(() => view.result.current.navigate("B", toB));
  await flush();
  let chosen: Promise<string | undefined> | undefined;
  act(() => { chosen = view.result.current.switchAccount("C"); });   // the selector / tradingAccount command
  expect(view.onNotice).toHaveBeenCalledWith("账户已被切换，未打开提醒目标");
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B"]);   // C waits for B's write to settle
  await act(async () => { runtime.calls[0].ok(t(2)); });
  view.show({ currentId: "B", statusAt: t(2) });
  expect(toB).not.toHaveBeenCalled();
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B", "C"]);   // the user's choice is the last write
  await act(async () => { runtime.calls[1].ok(t(3)); });
  expect(await chosen).toBeUndefined();
  view.show({ currentId: "C", statusAt: t(3) });
  expect(toB).not.toHaveBeenCalled();
  // A refused choice resolves its reason for the selector; nothing waiting means no extra notice.
  act(() => { chosen = view.result.current.switchAccount("D"); });
  await flush();
  await act(async () => { runtime.calls[2].fail("该账户正在排队"); });
  expect(await chosen).toBe("该账户正在排队");
  expect(view.onNotice).toHaveBeenCalledTimes(1);
});

it("B pending → C chosen → the chat changes before C's turn: C is never sent to the new session (Codex r4308267633)", async () => {
  const runtime = slowSwitch();
  const view = setup({ requestSwitch: runtime.requestSwitch });
  act(() => view.result.current.navigate("B", vi.fn()));
  await flush();
  let chosen: Promise<string | undefined> | undefined;
  act(() => { chosen = view.result.current.switchAccount("C"); });
  view.show({ sessionId: "s2" });                       // another chat while B is still on its way
  await act(async () => { runtime.calls[0].ok(t(2)); });
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B"]);
  expect(await chosen).toBe("对话或服务已切换，这次账户切换没有发出");
  // The new chat's own switches go on its own lane at once.
  act(() => { chosen = view.result.current.switchAccount("D"); });
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B", "D"]);
});

it("another chat or a disconnect abandons the navigation; after a disconnect a dead write holds nothing", async () => {
  const runtime = slowSwitch();
  const view = setup({ requestSwitch: runtime.requestSwitch });
  const there = vi.fn();
  act(() => view.result.current.navigate("B", there));
  await flush();
  view.show({ sessionId: "s2" });
  await act(async () => { runtime.calls[0].ok(t(2)); });
  view.show({ currentId: "B", statusAt: t(2) });
  expect(there).not.toHaveBeenCalled();

  const offline = vi.fn();
  act(() => view.result.current.navigate("C", offline));
  await flush();
  expect(runtime.calls.at(-1)?.accountId).toBe("C");   // never answers: the connection drops
  view.show({ runtimeUrl: undefined });
  view.show({ runtimeUrl: "http://runtime" });
  const after = vi.fn();
  act(() => view.result.current.navigate("D", after));
  await flush();
  expect(runtime.calls.map(call => call.accountId)).toEqual(["B", "C", "D"]);
  await act(async () => { runtime.calls[2].ok(t(5)); });
  view.show({ currentId: "D", statusAt: t(5) });
  expect(after).toHaveBeenCalledTimes(1);
  expect(offline).not.toHaveBeenCalled();
});
