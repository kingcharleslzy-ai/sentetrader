import { act, renderHook, waitFor } from "@testing-library/react";
import type { AccountOverviewSnapshot } from "../api";
import { useAccountOverview } from "./useAccountOverview";

const getAccountOverview = vi.hoisted(() => vi.fn());
vi.mock("../api", () => ({ getAccountOverview }));

const overview = (accountName: string) => ({ account: { accountName } }) as unknown as AccountOverviewSnapshot;
const context = { identity: 0 };
const handle = {
  baseUrl: "https://runtime.test", connection: "connected" as const, sessionId: undefined, replayPath: "",
  fence: { context, isCurrent: () => true, channel: () => ({ beginRead: () => () => true, noteWrite: () => undefined }) },
  orderContext: context, isCurrentOrderContext: () => true, setToast: vi.fn(),
};

test("a late reply for the previous account never overwrites the account switched to", async () => {
  let resolveA: (value: AccountOverviewSnapshot) => void = () => undefined;
  getAccountOverview
    .mockImplementationOnce(() => new Promise<AccountOverviewSnapshot>(resolve => { resolveA = resolve; }))
    .mockImplementationOnce(async () => overview("B"));
  const hook = renderHook(({ accountId }) => useAccountOverview({
    handle, accountId, accountStatus: "normal", feed: { channels: {} }, aiExecutionActive: false,
  }), { initialProps: { accountId: "A" } });
  expect(getAccountOverview).toHaveBeenCalledTimes(1);
  hook.rerender({ accountId: "B" });
  await waitFor(() => expect(hook.result.current.accountOverview?.account.accountName).toBe("B"));
  await act(async () => { resolveA(overview("A")); });
  expect(hook.result.current.accountOverview?.account.accountName).toBe("B");
  hook.unmount();
});

test("the page reads the account once its id is known; AI execution and status changes do not read it again", async () => {
  getAccountOverview.mockReset().mockImplementation(async () => overview("A"));
  type Props = { accountId?: string | null; accountStatus: "normal" | "stale"; aiExecutionActive: boolean };
  const hook = renderHook((props: Props) => useAccountOverview({ handle, feed: { channels: {} }, ...props }),
    { initialProps: { accountId: undefined, accountStatus: "stale", aiExecutionActive: false } as Props });
  expect(getAccountOverview).not.toHaveBeenCalled();      // the Runtime status has not named the account yet
  hook.rerender({ accountId: "A", accountStatus: "stale", aiExecutionActive: false });
  await waitFor(() => expect(hook.result.current.accountOverview?.account.accountName).toBe("A"));
  hook.rerender({ accountId: "A", accountStatus: "normal", aiExecutionActive: false });
  hook.rerender({ accountId: "A", accountStatus: "normal", aiExecutionActive: true });
  await act(async () => { await Promise.resolve(); });
  expect(getAccountOverview).toHaveBeenCalledTimes(1);
  hook.unmount();
});
