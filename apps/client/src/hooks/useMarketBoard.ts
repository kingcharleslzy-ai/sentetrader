import { useEffect, useRef, useState } from "react";
import { getMarketBoard, type MarketBoardSnapshot } from "../api";
import { feedViewServes, useBoardWant, type BoardWant } from "../feedView";
import { feedBoardLiveNow, feedIsLive, feedLiveNow, useFeedBoard } from "../runtimeFeed";

const BOARD_POLL_MS = 3000;
/** With the stream up, the first poll waits this long for the pushed board before reading. */
const PUSH_GRACE_MS = 1500;
const ALL: BoardWant = { all: true };

/**
 * The 报价总表 read the board view, the open 合约抽屉, the phone 报价 list and the 快讯 chips share.
 * Pushed (2026-10-12): each reader names the rows it shows (`want`, default every row) and the
 * Runtime pushes them on the session's stream — one full feed.board, then only changed fields —
 * while the page is visible. The poll stays the fallback: it reads every `pollMs` only while the push
 * does not cover this reader (stream down, an older Runtime, the board channel unavailable or silent
 * past FEED_STALE_MS). A failed read keeps the last snapshot and reports the error line instead of
 * clearing the table. Paused while the tab is hidden.
 */
export function useMarketBoard(baseUrl: string | undefined, enabled: boolean, pollMs = BOARD_POLL_MS, want: BoardWant = ALL) {
  const key = useBoardWant(baseUrl && enabled ? want : undefined);
  const { board: pushed, channel } = useFeedBoard();
  const serves = feedViewServes(baseUrl);
  const covered = Boolean(enabled && key && serves && pushed?.key === key && feedIsLive({ channels: { board: channel } }, "board"));
  const keyRef = useRef(key);
  keyRef.current = key;
  const [readState, setReadState] = useState<{baseUrl: string; snapshot?: MarketBoardSnapshot; error: string}>();
  useEffect(() => {
    if (!baseUrl || !enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let first = true;
    const read = async () => {
      // A background tab skips the read and checks again on the next tick; the first read
      // always runs, so a board opened in a background tab is filled when it comes forward.
      // While the push covers this reader nothing is read.
      if ((!first && document.visibilityState === "hidden") || (feedViewServes(baseUrl) && feedBoardLiveNow(keyRef.current))) {
        timer = setTimeout(() => void read(), pollMs);
        return;
      }
      first = false;
      try {
        const result = await getMarketBoard(baseUrl);
        if (stopped) return;
        setReadState({baseUrl, snapshot: result, error: ""});
      } catch (cause) {
        if (stopped) return;
        const error = cause instanceof Error && cause.message ? `报价总表读取失败：${cause.message.split("\n")[0].slice(0, 80)}` : "报价总表暂时不可用，稍后自动重试";
        setReadState(current => ({baseUrl, snapshot: current?.baseUrl === baseUrl ? current.snapshot : undefined, error}));
      } finally {
        if (!stopped) timer = setTimeout(() => void read(), pollMs);
      }
    };
    // A live stream brings the board within moments of the view request: give it that long first.
    if (feedViewServes(baseUrl) && feedLiveNow()) timer = setTimeout(() => void read(), Math.min(PUSH_GRACE_MS, pollMs));
    else void read();
    return () => { stopped = true; clearTimeout(timer); };
  }, [baseUrl, enabled, pollMs]);
  if (covered && pushed) return { snapshot: pushed as MarketBoardSnapshot, error: "" };
  const own = readState?.baseUrl === baseUrl ? readState : undefined;
  // Between the push stopping and the next poll answer — or while a changed row set (another
  // exchange chip) is on its way — whichever of the two is newer stays on screen, never a blank table.
  const last = enabled && serves && pushed ? pushed as MarketBoardSnapshot : undefined;
  const newer = last && (!own?.snapshot || Date.parse(last.capturedAt) > Date.parse(own.snapshot.capturedAt)) ? last : own?.snapshot;
  return { snapshot: newer, error: own?.error ?? "" };
}
