import { useEffect, useRef, useState } from "react";
import { getAccountOverview, type AccountOverviewSnapshot } from "../api";
import { feedIsLive, type RuntimeFeedState } from "../runtimeFeed";
import type { WorkbenchDataStatus } from "./useWorkbenchStatus";
import type { WorkspaceHandle } from "./workspaceHandle";

interface AccountOverviewInput {
  handle: WorkspaceHandle;
  /** The trading account the Runtime reports (null: it reports none; undefined: its status has not arrived yet, and
   *  nothing is read until it has); a switch drops the previous account's snapshot and replies. */
  accountId?: string | null;
  accountStatus: WorkbenchDataStatus;
  feed: RuntimeFeedState;
  /** An active AI execution polls faster while the feed is not covering the account. */
  aiExecutionActive: boolean;
}

/** Account summary, positions, activity and risk budget: feed first, polling as fallback. */
export function useAccountOverview({ handle, accountId, accountStatus, feed, aiExecutionActive }: AccountOverviewInput) {
  const { baseUrl, connection, replayPath } = handle;
  const [accountOverview, setAccountOverview] = useState<AccountOverviewSnapshot>();
  const [, setAccountOverviewLoading] = useState(false);
  const [, setAccountOverviewError] = useState<string>();
  const [accountOverviewKey, setAccountOverviewKey] = useState(0);

  const feedActive = connection === "connected" && !replayPath;
  const feedCovers = useRef(false);
  // Only the next poll's delay reads it: turning AI execution on or off does not read the account again.
  const fastPoll = useRef(aiExecutionActive);
  fastPoll.current = aiExecutionActive;
  // The account status matters only to a replay (whose account may not be ready); elsewhere its changes read nothing.
  const replayNotReady = Boolean(replayPath) && accountStatus !== "normal";
  feedCovers.current = feedActive && feedIsLive(feed, "account") && feed.account !== undefined;
  useEffect(() => {
    setAccountOverview(undefined);
  }, [accountId]);

  useEffect(() => {
    if (!feedActive || !feed.account) return;
    setAccountOverview(feed.account);
    setAccountOverviewError(undefined);
  }, [feed.account, feedActive]);

  useEffect(() => {
    if (connection !== "connected" || replayNotReady) {
      setAccountOverview(undefined);
      setAccountOverviewLoading(false);
      return;
    }
    if (accountId === undefined) return;
    const controller = new AbortController();
    let cancelled = false;
    let timer: number | undefined;
    let inFlight = false;
    const load = async (showLoading = false) => {
      if (cancelled || inFlight) return;
      if (document.visibilityState === "hidden" || feedCovers.current) {
        timer = window.setTimeout(() => void load(), 5_000);
        return;
      }
      inFlight = true;
      if (showLoading) setAccountOverviewLoading(true);
      setAccountOverviewError(undefined);
      try {
        const snapshot = await getAccountOverview(baseUrl, controller.signal);
        if (!cancelled) setAccountOverview(snapshot);
      } catch (error) {
        if (!cancelled && !(error instanceof DOMException && error.name === "AbortError")) {
          setAccountOverviewError("账户明细暂时不可用");
        }
      } finally {
        inFlight = false;
        if (!cancelled) {
          setAccountOverviewLoading(false);
          timer = window.setTimeout(() => void load(), replayPath ? 750 : fastPoll.current ? 3_000 : 15_000);
        }
      }
    };
    void load(true);
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [accountOverviewKey, accountId, baseUrl, connection, replayNotReady, replayPath]);

  return { accountOverview, refreshAccount: () => setAccountOverviewKey((value) => value + 1) };
}
