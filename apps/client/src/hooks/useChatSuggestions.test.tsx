import { act, renderHook, waitFor } from "@testing-library/react";
import { SUGGESTION_DEBOUNCE_MS, useChatSuggestions } from "./useChatSuggestions";

type Props = Parameters<typeof useChatSuggestions>[0];
const base: Props = { baseUrl: "http://runtime", enabled: true, sessionId: "s-1", symbol: "SHFE.rb2610", view: "trade", busy: false };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const bodies = (fetchMock: ReturnType<typeof vi.fn>) => fetchMock.mock.calls.map(([, init]) => JSON.parse(String((init as RequestInit).body)));

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test("面板打开时请求一次，最多显示 2 条", async () => {
  const fetchMock = vi.fn(async (..._args: unknown[]) => json({ items: ["螺纹放量了吗", "要不要减仓", "第三条不显示"] }));
  vi.stubGlobal("fetch", fetchMock);
  const { result } = renderHook(props => useChatSuggestions(props), { initialProps: base });
  await waitFor(() => expect(result.current).toEqual(["螺纹放量了吗", "要不要减仓"]));
  expect(String(fetchMock.mock.calls[0][0])).toBe("http://runtime/api/agent/suggestions");
  expect(bodies(fetchMock)).toEqual([{ sessionId: "s-1", symbol: "SHFE.rb2610", view: "trade", trigger: "open" }]);
});

test("接口 404 或报错时静默为没有建议；不可用（面板未开等）时不请求", async () => {
  const fetchMock = vi.fn(async () => json({ detail: "Not Found" }, 404));
  vi.stubGlobal("fetch", fetchMock);
  const { result, rerender } = renderHook(props => useChatSuggestions(props), { initialProps: { ...base, enabled: false } });
  expect(fetchMock).not.toHaveBeenCalled();
  rerender({ ...base });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  await act(async () => { await Promise.resolve(); });
  expect(result.current).toEqual([]);
  fetchMock.mockImplementation(async () => { throw new TypeError("network down"); });
  rerender({ ...base, busy: true });
  rerender({ ...base, busy: false, lastAnswerId: "a-1" });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  await act(async () => { await Promise.resolve(); });
  expect(result.current).toEqual([]);
});

test("一轮回答结束请求 turn；换合约先清掉旧建议，停 1.5 秒后请求 symbol", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let reply = ["第一条"];
  const fetchMock = vi.fn(async () => json({ items: reply }));
  vi.stubGlobal("fetch", fetchMock);
  const { result, rerender } = renderHook(props => useChatSuggestions(props), { initialProps: base });
  await waitFor(() => expect(result.current).toEqual(["第一条"]));
  reply = ["回答后的建议"];
  rerender({ ...base, busy: true });
  rerender({ ...base, busy: false, lastAnswerId: "a-1" });
  await waitFor(() => expect(result.current).toEqual(["回答后的建议"]));
  reply = ["黄金的建议"];
  rerender({ ...base, lastAnswerId: "a-1", symbol: "SHFE.au2612" });
  expect(result.current).toEqual([]);
  expect(fetchMock).toHaveBeenCalledTimes(2);
  await act(async () => { vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS); });
  await waitFor(() => expect(result.current).toEqual(["黄金的建议"]));
  expect(bodies(fetchMock).map(body => body.trigger)).toEqual(["open", "turn", "symbol"]);
  expect(bodies(fetchMock)[2]).toMatchObject({ symbol: "SHFE.au2612", view: "trade" });
});

test("切交易账户：旧账户的建议立即退役，在途的旧回包不落到新账户", async () => {
  let land: (items: string[]) => void = () => undefined;
  const fetchMock = vi.fn(() => new Promise<Response>(resolve => { land = items => resolve(json({ items })); }));
  vi.stubGlobal("fetch", fetchMock);
  const { result, rerender } = renderHook(props => useChatSuggestions(props), { initialProps: { ...base, accountId: "acc-a" } });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  const landA = land;
  rerender({ ...base, accountId: "acc-b" });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  await act(async () => { landA(["A 账户的持仓要不要减"]); });
  expect(result.current).toEqual([]);
  await act(async () => { land(["B 账户的建议"]); });
  await waitFor(() => expect(result.current).toEqual(["B 账户的建议"]));
});

test("发送被接纳、失败或载入历史都不算一轮回答结束；真正的新回答完成才请求 turn", async () => {
  const fetchMock = vi.fn(async () => json({ items: ["建议"] }));
  vi.stubGlobal("fetch", fetchMock);
  const { rerender } = renderHook(props => useChatSuggestions(props), { initialProps: base });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  // 202 accepted before any answer bubble: busy flips back without an answer.
  rerender({ ...base, busy: true });
  rerender({ ...base, busy: false });
  // Another session's history loads: its last answer id appears without a turn in progress.
  rerender({ ...base, sessionId: "s-2" });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  rerender({ ...base, sessionId: "s-2", lastAnswerId: "old-answer" });
  await act(async () => { await Promise.resolve(); });
  expect(bodies(fetchMock).map(body => body.trigger)).toEqual(["open", "open"]);
  // A real answer: in progress, then completed.
  rerender({ ...base, sessionId: "s-2", lastAnswerId: "old-answer", busy: true });
  rerender({ ...base, sessionId: "s-2", lastAnswerId: "new-answer", busy: false });
  await waitFor(() => expect(bodies(fetchMock).map(body => body.trigger)).toEqual(["open", "open", "turn"]));
});

test("换合约的防抖在途时换会话：旧防抖退役，不再对新会话多发一次 symbol", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const fetchMock = vi.fn(async () => json({ items: ["建议"] }));
  vi.stubGlobal("fetch", fetchMock);
  const { rerender } = renderHook(props => useChatSuggestions(props), { initialProps: base });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  rerender({ ...base, symbol: "SHFE.au2612" });
  rerender({ ...base, symbol: "SHFE.au2612", sessionId: "s-2" });
  await act(async () => { vi.advanceTimersByTime(SUGGESTION_DEBOUNCE_MS * 2); });
  expect(bodies(fetchMock).map(body => body.trigger)).toEqual(["open", "open"]);
  expect(bodies(fetchMock)[1]).toMatchObject({ sessionId: "s-2", symbol: "SHFE.au2612" });
});
