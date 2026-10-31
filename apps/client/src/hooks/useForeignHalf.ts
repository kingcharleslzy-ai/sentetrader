import { useEffect, useRef, useState } from "react";
import { getForeignHalf, type ForeignHalf } from "../api";
import { orderIsActive } from "../components/futures/PositionPanels";
import { feedIsLive, type RuntimeFeedState } from "../runtimeFeed";
import { mergeOrderFacts } from "./useSimulationOrders";
import type { WorkspaceHandle } from "./workspaceHandle";

/** With foreign positions or working orders: their P&L and fills move while the CNY account's push may be quiet. */
const BUSY_POLL_MS = 5_000;
const IDLE_POLL_MS = 15_000;

/**
 * 外盘仓合并显示 (所有者 2026-10-21，方案 1): the USD sub-account behind the session's CNY 本站模拟 account
 * (`runtime.account.foreignAccountId`). Foreign orders fill there, so its positions and working orders are read
 * from the account feed, with account-header polling as fallback, and shown in the same 持仓 / 委托 lists, each row keeping its account and currency.
 * Nothing is read on any other account, in a replay, or before the Runtime names the sub-account.
 */
export function useForeignHalf({ handle, foreignAccountId, feed }: { handle: WorkspaceHandle; foreignAccountId?: string | null; feed: RuntimeFeedState }) {
  const { baseUrl, connection, replayPath } = handle;
  const [half, setHalf] = useState<ForeignHalf>();
  const [refreshKey, setRefreshKey] = useState(0);
  const active = connection === "connected" && !replayPath && Boolean(foreignAccountId);
  const feedCovers = useRef<() => boolean>(() => false);
  feedCovers.current = () => active && feedIsLive(feed, "account") && feed.account?.foreign?.accountId === foreignAccountId;
  const pushed = active && feedCovers.current() ? feed.account?.foreign : undefined;
  const lastRefresh = useRef(refreshKey);
  useEffect(() => {
    if (pushed) setHalf(previous => previous?.accountId === pushed.accountId
      ? { ...pushed, orders: pushed.orders.map(order => mergeOrderFacts(previous.orders.find(old => old.clientOrderId === order.clientOrderId), order)) }
      : pushed);
  }, [pushed]);
  const current = useRef(foreignAccountId);
  current.current = foreignAccountId;

  useEffect(() => {
    if (!active || !foreignAccountId) {
      setHalf(undefined);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    let timer: number | undefined;
    let held = false;
    let force = refreshKey !== lastRefresh.current;
    lastRefresh.current = refreshKey;
    const load = async () => {
      if (cancelled) return;
      if (document.visibilityState !== "hidden" && (force || !feedCovers.current())) {
        const forced = force;
        force = false;
        try {
          const next = await getForeignHalf(baseUrl, foreignAccountId, controller.signal);
          if (cancelled || current.current !== foreignAccountId) return;
          if (forced || !feedCovers.current()) {
            setHalf(next);
            held = next.positions.length > 0 || next.orders.some(orderIsActive);
          }
        } catch {
          // The last good read stays: a sub-account that cannot be read right now keeps its rows rather than
          // flickering them away; the Runtime re-checks every action on them.
        }
      }
      if (!cancelled) timer = window.setTimeout(() => void load(), feedCovers.current() || held ? BUSY_POLL_MS : IDLE_POLL_MS);
    };
    void load();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [active, baseUrl, foreignAccountId, refreshKey]);

  return { foreignHalf: active && half?.accountId === foreignAccountId ? half : undefined,
    refreshForeignHalf: () => setRefreshKey(value => value + 1) };
}
