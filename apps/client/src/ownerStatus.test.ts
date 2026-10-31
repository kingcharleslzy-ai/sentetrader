import { afterEach, expect, test, vi } from "vitest";
import { authenticateTqkq, getOwnerAuthStatus, setOwnerSessionToken } from "./api";

afterEach(() => { vi.unstubAllGlobals(); setOwnerSessionToken("https://runtime.test", ""); });

test("opening the page asks the owner status once; a reload or a sign-in asks again", async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ authenticated: true }), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetcher);
  await getOwnerAuthStatus("https://runtime.test", { maxAgeMs: 5000 });
  await getOwnerAuthStatus("https://runtime.test", { maxAgeMs: 5000 });
  expect(fetcher).toHaveBeenCalledTimes(1);
  await getOwnerAuthStatus("https://runtime.test");
  expect(fetcher).toHaveBeenCalledTimes(2);
  setOwnerSessionToken("https://runtime.test", "token");
  await getOwnerAuthStatus("https://runtime.test", { maxAgeMs: 5000 });
  expect(fetcher).toHaveBeenCalledTimes(3);
});

test("a web TqKq login drops the anonymous status read so the session sees the new login", async () => {
  let authenticated = false;
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).endsWith("/api/auth/tqkq")) {
      authenticated = true;
      return new Response(JSON.stringify({ status: "logged_in", userId: "u1", username: "kq", role: "user", accountId: "a1", assembly: "connected" }), { headers: { "Content-Type": "application/json" } });
    }
    return new Response(JSON.stringify({ authenticated }), { headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetcher);
  expect((await getOwnerAuthStatus("https://runtime.test", { maxAgeMs: 5000 })).authenticated).toBe(false);
  await authenticateTqkq("https://runtime.test", "kq", "kq-password");
  expect((await getOwnerAuthStatus("https://runtime.test", { maxAgeMs: 5000 })).authenticated).toBe(true);
});
