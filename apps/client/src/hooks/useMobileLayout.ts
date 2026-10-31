import { useEffect, useState } from "react";

/**
 * 手机版 (2026-10-04): a narrow touch screen — a phone held upright. Width alone would also flip a
 * desktop window dragged narrow (and the desktop layout's own narrow-window checks), so the coarse
 * pointer is required too: the desktop layout never changes on a computer, at any width.
 */
export const MOBILE_LAYOUT_QUERY = "(max-width: 760px) and (pointer: coarse)";

/** Whether the window matches the phone layout now; follows rotation and resizes live. */
export function useMobileLayout(): boolean {
  const [matches, setMatches] = useState(() => typeof window.matchMedia === "function" && window.matchMedia(MOBILE_LAYOUT_QUERY).matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(MOBILE_LAYOUT_QUERY);
    const update = () => setMatches(query.matches);
    update();
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);
  return matches;
}
