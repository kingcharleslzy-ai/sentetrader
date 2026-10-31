import { useEffect, useRef } from "react";

/** How close to the bottom (px) still counts as "following the newest message". */
const FOLLOW_SLACK = 48;

/**
 * Keeps a scroll region on its newest content (the AI dock's conversation, 2026-10-01: replies,
 * voice transcripts and cards used to land below the fold until the owner scrolled by hand).
 * While the reader is at the bottom, any growth — a new turn, a streaming reply, a card — scrolls
 * it into view; once they scroll up to read, nothing moves until they come back down. A change of
 * `followKey` (the newest message the user sent or spoke) always jumps to the bottom.
 */
export function useStickToBottom<T extends HTMLElement>(followKey: string | undefined) {
  const ref = useRef<T>(null);
  const following = useRef(true);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const toBottom = () => { element.scrollTop = element.scrollHeight; };
    const onScroll = () => {
      following.current = element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_SLACK;
    };
    const follow = () => { if (following.current) toBottom(); };
    const observer = new MutationObserver(follow);
    observer.observe(element, { childList: true, subtree: true, characterData: true });
    // Re-showing a hidden dock or resizing it changes the box, not the content.
    const resize = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(follow);
    resize?.observe(element);
    element.addEventListener("scroll", onScroll, { passive: true });
    toBottom();
    return () => { observer.disconnect(); resize?.disconnect(); element.removeEventListener("scroll", onScroll); };
  }, []);

  useEffect(() => {
    const element = ref.current;
    if (!element || followKey === undefined) return;
    following.current = true;
    element.scrollTop = element.scrollHeight;
  }, [followKey]);

  return ref;
}
