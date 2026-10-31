import { useEffect, useState } from "react";
import { getForeignBoard, type ForeignBoardSnapshot } from "../api";

const FOREIGN_POLL_MS = 3000;

/**
 * 外盘 (2026-10-18, #976): the board's 外盘 group, read from GET /api/market/foreign (the Runtime's one
 * foreign entry, a few seconds' shared cache) every `pollMs` while `enabled` and the tab is visible.
 * A failed read keeps the last rows and reports the error line instead of clearing the table.
 */
export function useForeignBoard(baseUrl: string | undefined, enabled: boolean, pollMs = FOREIGN_POLL_MS) {
  const [state, setState] = useState<{ baseUrl: string; snapshot?: ForeignBoardSnapshot; error: string }>();
  useEffect(() => {
    if (!baseUrl || !enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let first = true;
    const read = async () => {
      if (!first && document.visibilityState === "hidden") {
        timer = setTimeout(() => void read(), pollMs);
        return;
      }
      first = false;
      try {
        const result = await getForeignBoard(baseUrl);
        if (stopped) return;
        setState({ baseUrl, snapshot: result, error: "" });
      } catch (cause) {
        if (stopped) return;
        const error = cause instanceof Error && cause.message ? `外盘读取失败：${cause.message.split("\n")[0].slice(0, 80)}` : "外盘暂时不可用，稍后自动重试";
        setState(current => ({ baseUrl, snapshot: current?.baseUrl === baseUrl ? current.snapshot : undefined, error }));
      } finally {
        if (!stopped) timer = setTimeout(() => void read(), pollMs);
      }
    };
    void read();
    return () => { stopped = true; clearTimeout(timer); };
  }, [baseUrl, enabled, pollMs]);
  const own = state?.baseUrl === baseUrl ? state : undefined;
  const snapshot = own?.snapshot;
  // The Runtime answers 200 with status unavailable when every source failed and nothing was kept.
  const error = own?.error || (snapshot?.status === "unavailable" ? "外盘暂时取不到，稍后自动重试" : "");
  return { snapshot, error };
}
