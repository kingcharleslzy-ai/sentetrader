import { useCallback, useEffect, useRef, useState } from "react";
import type { RuntimeStatus } from "@ai-trader/ui-events";
import { fetchAccountDirectory, setActiveTradingAccount, setSessionTradingAccount, type TradingAccountDirectory } from "../api";
import { strategyError } from "../strategyApi";
import type { SwitchOutcome } from "./useAccountNavigation";
import { admitAccountRevision, knownAccountRevision, noteAccountRevision } from "../accountRevision";

/** The session's trading-account view. The account id comes from the server's
 *  runtime.status alone; every account-scoped request follows it through the
 *  `X-AI-Trader-Account` header, and switching goes through the session route —
 *  the SSE status that follows updates the header and refetches the directory.
 *  The directory itself loads as soon as a session exists: before the first
 *  status arrives the switcher shows the directory's `current` account.
 *  M1b: while the current pool account is connecting or queued for capacity,
 *  the directory refreshes every 5 s until the account reaches the user —
 *  the SSE runtime.status pushes cover the transitions in between. */
/** Why the account cannot be switched yet: the session's first runtime.status has not arrived. */
export const ACCOUNT_STATE_LOADING = "账户状态加载中";

export function useTradingAccounts(baseUrl: string | undefined, sessionId: string | undefined, runtime: RuntimeStatus | undefined) {
  const [directory, setDirectory] = useState<TradingAccountDirectory>();
  // The account a 409 reported (#932 follow-up, Codex r4309888894): the session's account and revision as the Runtime
  // read them together. Taken through accountRevision (never older than what this window took) and shown — the
  // current account and the request header — until a runtime.status at least that new arrives.
  const [reported, setReported] = useState<{ sessionId: string; accountId: string; revision: number } | null>(null);
  const statusRevision = runtime?.accountGeneration;
  const override = reported && reported.sessionId === sessionId
    && (typeof statusRevision !== "number" || statusRevision < reported.revision) ? reported : null;
  const currentId = override ? override.accountId : runtime?.account?.id ?? null;
  const currentAccount = override && runtime?.account?.id !== override.accountId ? undefined : runtime?.account;
  // The session's first runtime.status arrived (#932 follow-up): only then may its account be switched.
  const accountReady = runtime !== undefined;
  const fetchedFor = useRef<{ sessionId: string; accountId: string | null } | null>(null);

  useEffect(() => { setActiveTradingAccount(currentId); }, [currentId]);

  // Every directory read (first read, pool polling, terminal re-read, manual refresh) goes through
  // `read`: only the newest read of the current Runtime/session lands, so a slower older answer
  // can never replace a newer directory (#526).
  const live = useRef({ baseUrl, sessionId });
  live.current = { baseUrl, sessionId };
  const generation = useRef(0);
  const read = useCallback(async (url: string, session: string | undefined): Promise<boolean> => {
    const ticket = ++generation.current;
    const result = await fetchAccountDirectory(url);
    const current = ticket === generation.current && live.current.baseUrl === url && live.current.sessionId === session;
    if (current) setDirectory(result);
    return current;
  }, []);

  useEffect(() => {
    if (!baseUrl || !sessionId) return;
    const wanted = { sessionId, accountId: currentId };
    if (fetchedFor.current && fetchedFor.current.sessionId === wanted.sessionId
      && fetchedFor.current.accountId === wanted.accountId) return;
    fetchedFor.current = wanted;
    let settled = false;
    read(baseUrl, sessionId)
      .then(landed => { settled = landed; if (!landed && fetchedFor.current === wanted) fetchedFor.current = null; })
      .catch(() => { if (fetchedFor.current === wanted) fetchedFor.current = null; });
    // Torn down before the read landed (disconnect, Runtime change): the same session/account must read again.
    return () => { if (!settled && fetchedFor.current === wanted) fetchedFor.current = null; };
  }, [baseUrl, sessionId, currentId, read]);

  const currentPoolStatus = currentAccount?.status
    ?? directory?.accounts.find(entry => entry.id === (currentId ?? directory.current))?.poolStatus
    ?? undefined;

  // While the current pool account is connecting or queued the directory is re-read every 5 s; when it
  // leaves that state (connected, failed…) it is read once more so the directory does not keep the old status.
  const pollingStatus = useRef<string | undefined>(undefined);
  useEffect(() => {
    const waiting = currentPoolStatus === "connecting" || currentPoolStatus === "queued";
    const wasWaiting = pollingStatus.current === "connecting" || pollingStatus.current === "queued";
    pollingStatus.current = currentPoolStatus;
    if (!baseUrl || !sessionId) return;
    if (!waiting) {
      if (wasWaiting) read(baseUrl, sessionId).catch(() => undefined);
      return;
    }
    const timer = window.setInterval(() => { read(baseUrl, sessionId).catch(() => undefined); }, 5000);
    return () => window.clearInterval(timer);
  }, [baseUrl, sessionId, currentPoolStatus, read]);

  /** The switch itself (PUT /api/sessions/{id}/account), sent even when the shown account already is the target
   *  (#932 R3: a reminder's navigation waits for the status the Runtime publishes for it — `statusAt`). Never timed
   *  out: a write's outcome is unknown until it answers. On success nothing changes here — the runtime.status that
   *  follows drives the header, the reported state and the directory refetch. Callers do not send it directly: every
   *  account write of the window goes through `useAccountNavigation`'s one lane (its `switchAccount`), one after
   *  another (Codex r4308219648). */
  const requestSwitch = async (accountId: string): Promise<SwitchOutcome> => {
    if (!baseUrl || !sessionId) return { error: "应用服务尚未连接" };
    // Before the session's first runtime.status the window cannot tell whether this Runtime orders switches: nothing
    // is sent (Codex r4309945263). After it, a Runtime that reports a revision gets it on every switch; only one that
    // never reports it (older) takes the unconditional path.
    if (!accountReady) return { error: ACCOUNT_STATE_LOADING };
    // The revision the accepted statuses and answers taught for this session (accountRevision: the one place a status
    // is accepted). An answer counts only for the Runtime and session still shown.
    const sent = knownAccountRevision(sessionId);
    const current = () => live.current.baseUrl === baseUrl && live.current.sessionId === sessionId;
    try {
      const answer = await setSessionTradingAccount(baseUrl, sessionId, accountId, sent);
      if (current() && typeof answer?.accountGeneration === "number") noteAccountRevision(sessionId, answer.accountGeneration);
      return { statusAt: typeof answer?.statusAt === "string" ? answer.statusAt : null };
    } catch (error) {
      const detail = (error as { status?: number; detail?: { detail?: { code?: string; accountGeneration?: number; accountId?: string } } });
      const superseded = detail.status === 409 && detail.detail?.detail?.code === "account_switch_superseded";
      if (!superseded) return { error: strategyError(error, "切换交易账户未完成，请重试。") };
      // A newer switch of this session landed first: this one changed nothing and is not retried. The account the 409
      // reports becomes the shown account and the request header at once — unless this window already took a newer
      // state (a late 409 teaches nothing).
      const { accountGeneration: revision, accountId: serverAccount } = detail.detail?.detail ?? {};
      if (current() && typeof revision === "number" && typeof serverAccount === "string" && admitAccountRevision(sessionId, revision)) {
        setReported({ sessionId, accountId: serverAccount, revision });
      }
      return { error: "这个对话的账户刚在别处切换过，这次没有切换；请看当前账户后再选" };
    }
  };

  // An account just opened (开通本站模拟账户): reload the directory now.
  const refresh = useCallback(async () => {
    if (!baseUrl) return;
    await read(baseUrl, sessionId);
  }, [baseUrl, sessionId, read]);
  return { directory, currentId, currentAccount, currentPoolStatus, accountReady, requestSwitch, refresh };
}

/** What the workspace hands its account surfaces: the directory plus `switchAccount`, the one account-write lane (#932). */
export type TradingAccountsView = ReturnType<typeof useTradingAccounts> & { switchAccount: (accountId: string) => Promise<string | undefined> };
