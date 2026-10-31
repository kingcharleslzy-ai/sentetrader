import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { RuntimeStatus } from "@ai-trader/ui-events";
import { fetchAccountDirectory, setActiveTradingAccount, setSessionTradingAccount, type TradingAccountDirectory } from "../api";
import { admitRuntimeStatus, resetAccountRevisions } from "../accountRevision";
import { TradingAccountSelect } from "../components/futures/TradingAccountSelect";
import { ACCOUNT_STATE_LOADING, useTradingAccounts } from "./useTradingAccounts";

vi.mock("../api", () => ({
  requestJson: vi.fn(),
  fetchAccountDirectory: vi.fn(),
  setActiveTradingAccount: vi.fn(),
  setSessionTradingAccount: vi.fn(),
}));

const directory: TradingAccountDirectory = {
  current: "tqkq-sim:default",
  accounts: [
    { id: "tqkq-sim:default", displayName: "默认模拟账户", role: "owner", environment: "simulation", executionOwner: true },
    { id: "tqkq-sim:abc123def456", displayName: "第二模拟账户", role: "operator", environment: "simulation", executionOwner: false },
  ],
};

function Harness({ sessionId, runtime }: { sessionId?: string; runtime?: RuntimeStatus }) {
  const view = useTradingAccounts("https://runtime.test", sessionId, runtime);
  return view.directory
    ? <>
        <TradingAccountSelect directory={view.directory} currentId={view.currentId} onSwitch={accountId => view.requestSwitch(accountId).then(outcome => "error" in outcome ? outcome.error : undefined)} />
        <span data-testid="pool-status">{view.currentPoolStatus ?? ""}</span>
      </>
    : null;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(fetchAccountDirectory).mockResolvedValue(directory);
});

test("a session without runtime.account yet still fetches the directory and renders the switcher", async () => {
  // A resumed connection before its first SSE runtime.status: currentId is null,
  // the directory loads anyway and the select falls back to directory.current.
  render(<Harness sessionId="session-1" runtime={undefined} />);
  const select = await screen.findByRole("combobox", { name: "切换交易账户" });
  expect(fetchAccountDirectory).toHaveBeenCalledTimes(1);
  expect(fetchAccountDirectory).toHaveBeenCalledWith("https://runtime.test");
  expect(select).toHaveValue("tqkq-sim:default");
  // Both accounts under 模拟盘, plus 实盘's unselectable 暂无 entry.
  expect(screen.getAllByRole("option").filter(option => !(option as HTMLOptionElement).disabled)).toHaveLength(2);
});

test("refreshes the directory every 5 s while the current pool account is queued", async () => {
  vi.useFakeTimers();
  try {
    const runtime = { account: { id: "tqkq-sim:default", status: "queued" } } as unknown as RuntimeStatus;
    const { unmount } = render(<Harness sessionId="session-1" runtime={runtime} />);
    await act(async () => { await Promise.resolve(); });
    expect(fetchAccountDirectory).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("pool-status").textContent).toBe("queued");
    await act(async () => { vi.advanceTimersByTime(5000); });
    expect(fetchAccountDirectory).toHaveBeenCalledTimes(2);
    await act(async () => { vi.advanceTimersByTime(5000); });
    expect(fetchAccountDirectory).toHaveBeenCalledTimes(3);
    unmount();
  } finally {
    vi.useRealTimers();
  }
});

test("pool status falls back to the directory entry before the first runtime.status", async () => {
  const poolDirectory: TradingAccountDirectory = {
    current: "tqkq-sim:default",
    accounts: [{ id: "tqkq-sim:default", displayName: "默认模拟账户", role: "owner", environment: "simulation", executionOwner: true, poolStatus: "connecting" }],
  };
  vi.mocked(fetchAccountDirectory).mockResolvedValue(poolDirectory);
  // runtime without account: currentId falls to directory.current; the single
  // entry renders as a label, so wait on the status span itself.
  render(<Harness sessionId="session-1" runtime={{ } as RuntimeStatus} />);
  expect((await screen.findByTestId("pool-status")).textContent).toBe("connecting");
  // A connected account stops refreshing: one fetch only, no interval keeps running.
  expect(fetchAccountDirectory).toHaveBeenCalledTimes(1);
});

function DirectoryHarness({ baseUrl, sessionId, runtime, onView }: {
  baseUrl?: string; sessionId?: string; runtime?: RuntimeStatus; onView?: (view: ReturnType<typeof useTradingAccounts>) => void;
}) {
  const view = useTradingAccounts(baseUrl, sessionId, runtime);
  onView?.(view);
  return <span data-testid="directory">{view.directory ? view.directory.accounts.map(entry => `${entry.id}:${entry.poolStatus ?? ""}`).join(",") : "none"}</span>;
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

const pool = (poolStatus: string): TradingAccountDirectory => ({
  current: "tqkq-sim:default",
  accounts: [{ id: "tqkq-sim:default", displayName: "默认模拟账户", role: "owner", environment: "simulation", executionOwner: true, poolStatus } as TradingAccountDirectory["accounts"][number]],
});

test("#526 首读还在途时断开，同一会话重连会重新读目录，迟到的旧回包不落地", async () => {
  const first = deferred<TradingAccountDirectory>();
  vi.mocked(fetchAccountDirectory).mockReturnValueOnce(first.promise).mockResolvedValue(directory);
  const view = render(<DirectoryHarness baseUrl="https://runtime.test" sessionId="session-1" />);
  view.rerender(<DirectoryHarness baseUrl={undefined} sessionId="session-1" />);
  view.rerender(<DirectoryHarness baseUrl="https://runtime.test" sessionId="session-1" />);
  expect(fetchAccountDirectory).toHaveBeenCalledTimes(2);
  expect(await screen.findByText(/tqkq-sim:abc123def456/)).toBeInTheDocument();
  await act(async () => { first.resolve(pool("connecting")); await first.promise; });
  expect(screen.getByTestId("directory").textContent).toContain("tqkq-sim:abc123def456");
});

test("#526 手动刷新与轮询：先发的慢回包不能盖掉后发的新目录", async () => {
  let handle: ReturnType<typeof useTradingAccounts> | undefined;
  const slow = deferred<TradingAccountDirectory>();
  vi.mocked(fetchAccountDirectory).mockResolvedValueOnce(pool("queued")).mockReturnValueOnce(slow.promise).mockResolvedValueOnce(pool("connecting"));
  render(<DirectoryHarness baseUrl="https://runtime.test" sessionId="session-1" onView={view => { handle = view; }} />);
  await screen.findByText("tqkq-sim:default:queued");
  let older: Promise<void> | undefined;
  act(() => { older = handle!.refresh(); });
  await act(async () => { await handle!.refresh(); });
  expect(screen.getByTestId("directory").textContent).toBe("tqkq-sim:default:connecting");
  await act(async () => { slow.resolve(pool("queued")); await older; });
  expect(screen.getByTestId("directory").textContent).toBe("tqkq-sim:default:connecting");
});

test("#526 当前池账户从 connecting 变成 connected 后补读一次目录，不再停在 connecting", async () => {
  vi.mocked(fetchAccountDirectory).mockResolvedValueOnce(pool("connecting")).mockResolvedValue(pool("connected"));
  const connecting = { account: { id: "tqkq-sim:default", status: "connecting" } } as unknown as RuntimeStatus;
  const connected = { account: { id: "tqkq-sim:default", status: "connected" } } as unknown as RuntimeStatus;
  const view = render(<DirectoryHarness baseUrl="https://runtime.test" sessionId="session-1" runtime={connecting} />);
  await screen.findByText("tqkq-sim:default:connecting");
  view.rerender(<DirectoryHarness baseUrl="https://runtime.test" sessionId="session-1" runtime={connected} />);
  expect(await screen.findByText("tqkq-sim:default:connected")).toBeInTheDocument();
  expect(fetchAccountDirectory).toHaveBeenCalledTimes(2);
});

test("a switch names the session's revision; a newer switch landed first (409) is not retried; a late 409 teaches nothing (#932 follow-up)", async () => {
  resetAccountRevisions();
  const runtime = { simulationTrading: true, account: { id: "tqkq-sim:default" } } as unknown as RuntimeStatus;
  const switches = vi.mocked(setSessionTradingAccount);
  const view = renderHook(() => useTradingAccounts("https://runtime.test", "session-1", runtime));
  expect(admitRuntimeStatus("session-1", { accountGeneration: 3 })).toBe(true);
  switches.mockResolvedValueOnce({ sessionId: "session-1", accountId: "tqkq-sim:abc123def456", statusAt: "t", accountGeneration: 4 });
  await act(async () => { expect(await view.result.current.requestSwitch("tqkq-sim:abc123def456")).toEqual({ statusAt: "t" }); });
  expect(switches).toHaveBeenLastCalledWith("https://runtime.test", "session-1", "tqkq-sim:abc123def456", 3);
  // A late status of the older revision is dropped whole and never lowers it (Codex r4309822257).
  expect(admitRuntimeStatus("session-1", { accountGeneration: 3 })).toBe(false);
  switches.mockRejectedValueOnce(Object.assign(new Error("409"), { status: 409,
    detail: { detail: { code: "account_switch_superseded", accountGeneration: 5, accountId: "tqkq-sim:abc123def456" } } }));
  await act(async () => {
    expect(await view.result.current.requestSwitch("tqkq-sim:default")).toEqual({ error: "这个对话的账户刚在别处切换过，这次没有切换；请看当前账户后再选" });
  });
  expect(switches).toHaveBeenCalledTimes(2);                 // never retried by itself
  expect(switches).toHaveBeenLastCalledWith("https://runtime.test", "session-1", "tqkq-sim:default", 4);
  switches.mockResolvedValueOnce({ sessionId: "session-1", accountId: "tqkq-sim:default", statusAt: "u", accountGeneration: 6 });
  await act(async () => { await view.result.current.requestSwitch("tqkq-sim:default"); });
  expect(switches).toHaveBeenLastCalledWith("https://runtime.test", "session-1", "tqkq-sim:default", 5);   // the Runtime's number
  // A late 409 to a revision we no longer hold (6 then) teaches nothing (Codex r4309683185).
  let release: (reason: unknown) => void = () => undefined;
  switches.mockImplementationOnce(() => new Promise((_, reject) => { release = reject; }));
  let late: Promise<unknown> | undefined;
  act(() => { late = view.result.current.requestSwitch("tqkq-sim:abc123def456"); });   // sent with 6
  admitRuntimeStatus("session-1", { accountGeneration: 8 });                             // meanwhile two more switches landed
  await act(async () => {
    release(Object.assign(new Error("409"), { status: 409, detail: { detail: { code: "account_switch_superseded", accountGeneration: 7 } } }));
    await late;
  });
  switches.mockResolvedValueOnce({ sessionId: "session-1", accountId: "tqkq-sim:default", statusAt: "v", accountGeneration: 9 });
  await act(async () => { await view.result.current.requestSwitch("tqkq-sim:default"); });
  expect(switches).toHaveBeenLastCalledWith("https://runtime.test", "session-1", "tqkq-sim:default", 8);
  // An older Runtime reports no revision: its statuses are accepted and the switch names none, as before.
  expect(admitRuntimeStatus("session-2", {})).toBe(true);
  const legacy = renderHook(() => useTradingAccounts("https://runtime.test", "session-2", runtime));
  switches.mockResolvedValueOnce({ sessionId: "session-2", accountId: "tqkq-sim:default" });
  await act(async () => { await legacy.result.current.requestSwitch("tqkq-sim:default"); });
  expect(switches).toHaveBeenLastCalledWith("https://runtime.test", "session-2", "tqkq-sim:default", undefined);
});

test("a 409 puts the window and the request header on the account the Runtime reports, until a newer status (Codex r4309888894)", async () => {
  resetAccountRevisions();
  const status = (account: string, accountGeneration: number) => ({ simulationTrading: true, account: { id: account }, accountGeneration }) as unknown as RuntimeStatus;
  const switches = vi.mocked(setSessionTradingAccount);
  admitRuntimeStatus("session-1", { accountGeneration: 3 });
  const view = renderHook(({ runtime }: { runtime: RuntimeStatus }) => useTradingAccounts("https://runtime.test", "session-1", runtime),
    { initialProps: { runtime: status("tqkq-sim:default", 3) } });
  switches.mockRejectedValueOnce(Object.assign(new Error("409"), { status: 409,
    detail: { detail: { code: "account_switch_superseded", accountGeneration: 4, accountId: "tqkq-sim:abc123def456" } } }));
  await act(async () => { await view.result.current.requestSwitch("tqkq-sim:default"); });
  expect(view.result.current.currentId).toBe("tqkq-sim:abc123def456");                       // no runtime.status yet
  expect(vi.mocked(setActiveTradingAccount)).toHaveBeenLastCalledWith("tqkq-sim:abc123def456");   // the header too
  // A newer status takes over again.
  view.rerender({ runtime: status("tqkq-sim:default", 5) });
  expect(view.result.current.currentId).toBe("tqkq-sim:default");
  // A 409 reporting an older state than the window took changes nothing.
  switches.mockRejectedValueOnce(Object.assign(new Error("409"), { status: 409,
    detail: { detail: { code: "account_switch_superseded", accountGeneration: 4, accountId: "tqkq-sim:abc123def456" } } }));
  admitRuntimeStatus("session-1", { accountGeneration: 5 });
  await act(async () => { await view.result.current.requestSwitch("tqkq-sim:abc123def456"); });
  expect(view.result.current.currentId).toBe("tqkq-sim:default");
});

test("before the session's first status the selector is disabled and nothing is sent; after it the switch names the revision (Codex r4309945263)", async () => {
  resetAccountRevisions();
  const switches = vi.mocked(setSessionTradingAccount);
  switches.mockClear();
  function Gated({ runtime }: { runtime?: RuntimeStatus }) {
    const view = useTradingAccounts("https://runtime.test", "session-gated", runtime);
    return view.directory ? <TradingAccountSelect directory={view.directory} currentId={view.currentId}
      onSwitch={accountId => view.requestSwitch(accountId).then(outcome => "error" in outcome ? outcome.error : undefined)}
      disabledReason={view.accountReady ? undefined : ACCOUNT_STATE_LOADING} /> : null;
  }
  const page = render(<Gated />);
  const select = await screen.findByRole("combobox", { name: "切换交易账户" });
  expect(select).toBeDisabled();
  expect(screen.getByText("账户状态加载中")).toBeInTheDocument();
  const early = renderHook(() => useTradingAccounts("https://runtime.test", "session-gated", undefined));
  await act(async () => { expect(await early.result.current.requestSwitch("tqkq-sim:abc123def456")).toEqual({ error: "账户状态加载中" }); });
  expect(switches).not.toHaveBeenCalled();
  // The first status arrives (accepted, its revision noted): the switch goes out with it.
  const first = { simulationTrading: true, account: { id: "tqkq-sim:default" }, accountGeneration: 2 } as unknown as RuntimeStatus;
  admitRuntimeStatus("session-gated", first);
  page.rerender(<Gated runtime={first} />);
  expect(screen.getByRole("combobox", { name: "切换交易账户" })).toBeEnabled();
  switches.mockResolvedValueOnce({ sessionId: "session-gated", accountId: "tqkq-sim:abc123def456", statusAt: "t", accountGeneration: 3 });
  await act(async () => { fireEvent.change(screen.getByRole("combobox", { name: "切换交易账户" }), { target: { value: "tqkq-sim:abc123def456" } }); });
  expect(switches).toHaveBeenLastCalledWith("https://runtime.test", "session-gated", "tqkq-sim:abc123def456", 2);
});

test("revisions are kept per chat: a late answer for chat A never replaces chat B's (Codex r4309683202)", async () => {
  resetAccountRevisions();
  const runtime = { simulationTrading: true, account: { id: "tqkq-sim:default" } } as unknown as RuntimeStatus;
  const switches = vi.mocked(setSessionTradingAccount);
  admitRuntimeStatus("chat-a", { accountGeneration: 7 });
  admitRuntimeStatus("chat-b", { accountGeneration: 2 });
  const view = renderHook(({ sessionId }: { sessionId: string }) => useTradingAccounts("https://runtime.test", sessionId, runtime),
    { initialProps: { sessionId: "chat-a" } });
  let answerA: (value: unknown) => void = () => undefined;
  switches.mockImplementationOnce(() => new Promise(resolve => { answerA = resolve as (value: unknown) => void; }));
  let pending: Promise<unknown> | undefined;
  act(() => { pending = view.result.current.requestSwitch("tqkq-sim:abc123def456"); });   // chat A, sent with 7
  view.rerender({ sessionId: "chat-b" });                                                 // the user moves to chat B
  await act(async () => { answerA({ sessionId: "chat-a", accountId: "tqkq-sim:abc123def456", statusAt: "a", accountGeneration: 8 }); await pending; });
  switches.mockResolvedValueOnce({ sessionId: "chat-b", accountId: "tqkq-sim:default", statusAt: "b", accountGeneration: 3 });
  await act(async () => { await view.result.current.requestSwitch("tqkq-sim:default"); });
  expect(switches).toHaveBeenLastCalledWith("https://runtime.test", "chat-b", "tqkq-sim:default", 2);
  view.rerender({ sessionId: "chat-a" });
  switches.mockResolvedValueOnce({ sessionId: "chat-a", accountId: "tqkq-sim:default", statusAt: "c", accountGeneration: 8 });
  await act(async () => { await view.result.current.requestSwitch("tqkq-sim:default"); });
  expect(switches).toHaveBeenLastCalledWith("https://runtime.test", "chat-a", "tqkq-sim:default", 7);
});
