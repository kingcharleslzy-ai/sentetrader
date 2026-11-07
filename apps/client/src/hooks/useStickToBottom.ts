import { useCallback, useEffect, useRef } from "react";

/** How close to the bottom (px) still counts as "following the newest message". */
const FOLLOW_SLACK = 48;

/**
 * Keeps a scroll region on its newest content (the AI dock's conversation, 2026-10-01: replies,
 * voice transcripts and cards used to land below the fold until the owner scrolled by hand).
 * While the reader is at the bottom, any growth — a new turn, a streaming reply, a card — scrolls
 * it into view; once they scroll up to read, nothing moves until they come back down. A change of
 * `followKey` (the newest message the user sent or spoke) always jumps to the bottom. `onFollow` hears
 * whether the reader is on the newest content (the AI orb's new-message dot, 2026-11-07).
 */
export function useStickToBottom<T extends HTMLElement>(followKey: string | undefined, onFollow?: (following: boolean) => void) {
  const ref = useRef<T>(null);
  const following = useRef(true);
  const heard = useRef(onFollow);
  heard.current = onFollow;
  const setFollowing = useCallback((value: boolean) => { if (following.current !== value) { following.current = value; heard.current?.(value); } }, []);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const toBottom = () => { element.scrollTop = element.scrollHeight; };
    const onScroll = () => setFollowing(element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_SLACK);
    const follow = () => { if (following.current) toBottom(); };
    const observer = new MutationObserver(follow);
    observer.observe(element, { childList: true, subtree: true, characterData: true });
    // Re-showing a hidden dock or resizing it changes the box, not the content.
    const resize = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(follow);
    resize?.observe(element);
    element.addEventListener("scroll", onScroll, { passive: true });
    toBottom();
    heard.current?.(true);
    return () => { observer.disconnect(); resize?.disconnect(); element.removeEventListener("scroll", onScroll); };
  }, [setFollowing]);

  useEffect(() => {
    const element = ref.current;
    if (!element || followKey === undefined) return;
    setFollowing(true);
    element.scrollTop = element.scrollHeight;
  }, [followKey, setFollowing]);

  return ref;
}
