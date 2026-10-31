/** Request time limits for the client's JSON transports (#712 B1): `requestJson`, `runtimeFetch` (the
 *  generated workbench client) and `guestRequest` all run through `withRequestTimeout`. Also the one transient-failure classification
 *  the recovery paths share (`isTransientFailure`). Kept apart from api.ts so callers can name a limit
 *  without depending on the whole API module. */

/** A read gives up after this long, so a hung connection ends as an error the caller already shows
 *  instead of a spinner that never stops. Above the Runtime's own 20 s market-read limit. */
export const DEFAULT_READ_TIMEOUT_MS = 30_000;
/** Reads the Runtime may legitimately spend longer on: web research and the desk judgment (may start a
 *  model call), the trade review and its 按我的习惯 prefill (rebuild fill rounds), the strategy engine bundle. */
export const LONG_READ_TIMEOUT_MS = 90_000;

export const REQUEST_TIMEOUT_CODE = "request_timeout";

/** `timeoutMs`: undefined = the default (GET/HEAD get DEFAULT_READ_TIMEOUT_MS; writes have none, because
 *  aborting a write does not cancel it on the server and would report a false failure); null = no limit. */
export interface RequestTimeoutOption { timeoutMs?: number | null }

export function isRequestTimeout(error: unknown): boolean {
  return (error as { code?: unknown } | undefined)?.code === REQUEST_TIMEOUT_CODE;
}

/**
 * The one answer to "is this failure worth trying the same read again after a pause?" (#710 P2), shared
 * by every recovery path: the transport failed before any answer (fetch's TypeError), the service answered
 * 5xx, or a read (GET/HEAD) ran past its time limit. Everything else stays final: a 4xx (401 belongs to
 * the login, not a retry), the caller's own abort, a write that timed out (its outcome is unknown and must
 * never be sent again blindly), and any other status-0 error. A caller retries only reads or writes that
 * are safe to repeat.
 */
export function isTransientFailure(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  const { status, code, method } = (error ?? {}) as { status?: unknown; code?: unknown; method?: unknown };
  if (code === REQUEST_TIMEOUT_CODE) return method === "GET" || method === "HEAD";
  return typeof status === "number" && status >= 500;
}

/**
 * Runs one request under its time limit. `run` receives the signal to hand to fetch: the caller's own
 * signal still aborts it as before (AbortError); when the limit passes the fetch is aborted and the
 * call rejects with a status-0 `request_timeout` error that the callers' generic failure paths show.
 * The limit covers whatever `run` awaits (the body too for JSON reads, the headers for a raw Response).
 */
export async function withRequestTimeout<T>(
  request: { method?: string; signal?: AbortSignal | null; timeoutMs?: number | null },
  run: (signal: AbortSignal | undefined) => Promise<T>,
): Promise<T> {
  const method = (request.method ?? "GET").toUpperCase();
  const limit = request.timeoutMs === undefined ? (method === "GET" || method === "HEAD" ? DEFAULT_READ_TIMEOUT_MS : null) : request.timeoutMs;
  const caller = request.signal ?? undefined;
  if (limit == null || limit <= 0) return run(caller);
  const controller = new AbortController();
  const forward = () => controller.abort(caller?.reason);
  if (caller?.aborted) forward(); else caller?.addEventListener("abort", forward, { once: true });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const expired = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(Object.assign(new Error(`应用服务 ${Math.round(limit / 1000)} 秒内没有响应，请稍后重试`), { status: 0, code: REQUEST_TIMEOUT_CODE, method }));
      controller.abort();
    }, limit);
  });
  try {
    return await Promise.race([run(controller.signal), expired]);
  } finally {
    clearTimeout(timer);
    caller?.removeEventListener("abort", forward);
  }
}
