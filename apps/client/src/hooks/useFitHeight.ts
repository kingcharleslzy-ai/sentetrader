import { useLayoutEffect, useRef } from "react";

/**
 * Grow a textarea to its content (a CSS `max-height` caps it, and it scrolls inside from there). `active` false
 * leaves the height to CSS (the AI 指令栏's one collapsed line while it is not focused).
 */
export function useFitHeight(value: string, active = true) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const fit = () => {
      const area = ref.current;
      if (!area || area.offsetParent === null) return;
      area.style.height = "";
      if (!active) return;
      area.style.height = "auto";
      area.style.height = `${area.scrollHeight + area.offsetHeight - area.clientHeight}px`;
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [value, active]);
  return ref;
}
