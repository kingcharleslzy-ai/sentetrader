import { useCallback, useEffect, useRef, useState } from "react";

/** How long a navigation waits — for its switch to answer (or an earlier one to), then for the status that confirms
 *  it — before it gives up with a notice and opens nothing. */
export const ACCOUNT_NAVIGATION_WAIT_MS = 15_000;

/** PUT /api/sessions/{id}/account's outcome: the user-facing error, or the timestamp of the runtime.status the Runtime
 *  published for it (null from a Runtime before #932 R3, which sends none). */
export type SwitchOutcome = { error: string } | { statusAt: string | null };

export interface AccountNavigationDeps {
  /** The account the window shows, and the timestamp of the runtime.status it came from. */
  currentId: string | null;
  statusAt: string | undefined;
  sessionId: string | undefined;
  runtimeUrl: string | undefined;
  /** Whether the directory lists the account (the same check the `tradingAccount` command makes). */
  known: (accountId: string) => boolean;
  requestSwitch: (accountId: string) => Promise<SwitchOutcome>;
  onNotice: (text: string) => void;
}

interface Pending {
  accountId: string;
  open: () => void;
  sessionId: string | undefined;
  runtimeUrl: string | undefined;
  /** Set once the Runtime took the switch: the status timestamp that confirms it ("" = any, an older Runtime). */
  confirmedAt?: string;
  timer?: ReturnType<typeof setTimeout>;
}

/** The switch writes of one session on one connection: one after another, never two with an unknown outcome at once. */
interface Lane { key: string; queue: Promise<unknown> }

/**
 * 点提醒去它自己的账户 (#932 R3): an account's reminder (成交 / 撤单 / 挂单 / 复盘) opens its target only on a runtime.status
 * the Runtime published for this very switch or later. One rule decides (`confirmed`): the window shows the reminder's
 * account AND the status it shows is at least as new as the one the switch answered with — so an earlier switch's
 * status still on its way, or the account shown from before, never passes for it. Every account navigation asks the
 * Runtime (a repeat of the shown account changes nothing there but still answers with a fresh status). Switch writes go
 * one after another and are never cut off or sent twice while their outcome is unknown; a navigation that waits longer
 * than ACCOUNT_NAVIGATION_WAIT_MS gives up with a notice. A newer navigation, another chat, another Runtime or a
 * disconnect abandons the waiting one; a disconnect also leaves a dead write behind with its own lane.
 */
export function useAccountNavigation(deps: AccountNavigationDeps) {
  const live = useRef(deps);
  live.current = deps;
  const pending = useRef<Pending | null>(null);
  const lanes = useRef<Lane>({ key: "", queue: Promise.resolve() });
  const [answered, setAnswered] = useState(0);

  const drop = useCallback((entry: Pending | null) => {
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    if (pending.current === entry) pending.current = null;
  }, []);

  // The one place a navigation opens: a status published for its switch (or later) shows its account.
  useEffect(() => {
    const entry = pending.current;
    const confirmed = entry?.confirmedAt !== undefined && deps.currentId === entry.accountId
      && (deps.statusAt ?? "") >= entry.confirmedAt;
    if (!entry || !confirmed) return;
    drop(entry);
    entry.open();
  }, [deps.currentId, deps.statusAt, answered, drop]);
  // Another chat or Runtime, or disconnected: the waiting navigation is abandoned; after a disconnect the writes start
  // a new lane (one still unanswered died with the connection).
  useEffect(() => {
    const entry = pending.current;
    if (entry && (entry.sessionId !== deps.sessionId || entry.runtimeUrl !== deps.runtimeUrl || !deps.runtimeUrl)) drop(entry);
    if (!deps.runtimeUrl) lanes.current = { key: "", queue: Promise.resolve() };
  }, [deps.sessionId, deps.runtimeUrl, drop]);
  useEffect(() => () => drop(pending.current), [drop]);

  /** The lane of the session's account writes on this connection (a new one for another chat or Runtime). */
  const laneNow = useCallback((): Lane => {
    const now = live.current;
    const key = `${now.runtimeUrl ?? ""}|${now.sessionId ?? ""}`;
    if (lanes.current.key !== key) lanes.current = { key, queue: Promise.resolve() };
    return lanes.current;
  }, []);

  /**
   * The one way an account write is sent: queued on the current session's lane behind the write before it, with that
   * session's own request captured now. When its turn comes it is dropped unsent — null — if the chat or the Runtime
   * changed meanwhile (Codex r4308267633: the old choice never lands on the new session), or if `wanted` says it was
   * given up.
   */
  const enqueue = useCallback((accountId: string, wanted: () => boolean = () => true): Promise<SwitchOutcome | null> => {
    const lane = laneNow();
    const send = live.current.requestSwitch;
    const run = lane.queue.then(async () => {
      const now = live.current;
      if (`${now.runtimeUrl ?? ""}|${now.sessionId ?? ""}` !== lane.key || !wanted()) return null;
      return send(accountId);
    });
    lane.queue = run.catch(() => undefined);
    return run;
  }, [laneNow]);

  /**
   * Every other switch of the session's account (the account selector, the `tradingAccount` command) — the window's
   * only other account write, and it goes through the same lane (Codex r4308219648): a reminder navigation still
   * waiting is abandoned, visibly (r4308164454), and this write is sent only after the one before it settled, so the
   * user's explicit choice is the last write and the account it lands on. Resolves the user-facing error, if any.
   */
  const switchAccount = useCallback(async (accountId: string): Promise<string | undefined> => {
    const entry = pending.current;
    if (entry) {
      drop(entry);
      live.current.onNotice("账户已被切换，未打开提醒目标");
    }
    if (!live.current.runtimeUrl || !live.current.sessionId) return undefined;
    const outcome = await enqueue(accountId);
    if (!outcome) return "对话或服务已切换，这次账户切换没有发出";
    return "error" in outcome ? outcome.error : undefined;
  }, [drop, enqueue]);

  /** Open `open` on `accountId` (null = wherever the window is). Replaces any navigation still waiting. */
  const navigate = useCallback((accountId: string | null | undefined, open: () => void) => {
    drop(pending.current);
    const now = live.current;
    if (!accountId) {
      open();
      return;
    }
    if (!now.runtimeUrl || !now.sessionId) {
      now.onNotice("连接后才能切到这条提醒的账户");
      return;
    }
    if (!now.known(accountId)) {
      now.onNotice("这条提醒的交易账户不在目录里或未授权，仍停在当前账户");
      return;
    }
    const entry: Pending = { accountId, open, sessionId: now.sessionId, runtimeUrl: now.runtimeUrl };
    pending.current = entry;
    entry.timer = setTimeout(() => {
      if (pending.current !== entry) return;
      drop(entry);
      live.current.onNotice(entry.confirmedAt !== undefined ? "账户切换还没完成，仍停在当前账户；稍后再点这条提醒"
        : "切换账户还没有回应，没有打开这条提醒；稍后再点一次");
    }, ACCOUNT_NAVIGATION_WAIT_MS);
    // Replaced or given up before its turn: no switch at all.
    void enqueue(accountId, () => pending.current === entry).then(outcome => {
      if (!outcome || pending.current !== entry) return;
      if ("error" in outcome) {
        drop(entry);
        live.current.onNotice(`没有切到这条提醒的账户，仍停在当前账户：${outcome.error}`);
        return;
      }
      entry.confirmedAt = outcome.statusAt ?? "";
      setAnswered(value => value + 1);
    });
  }, [drop, enqueue]);
  return { navigate, switchAccount };
}
