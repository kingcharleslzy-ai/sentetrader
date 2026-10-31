import { useEffect, useState } from "react";
import { clearHighlights, resolveHighlightTarget, useHighlights, type Highlight } from "../aiAnnotations";
import "./HighlightLayer.css";

interface Box { x: number; y: number; width: number; height: number }

/**
 * The AI's pointer: a pulse or outline over the surface a `highlight` command named,
 * with its label in a bubble. Follows the target while it moves, never intercepts the
 * pointer, and every highlight goes away on the user's next click or when it times out.
 */
export function HighlightLayer() {
  const highlights = useHighlights();
  const [boxes, setBoxes] = useState<Record<string, Box>>({});
  useEffect(() => {
    if (!highlights.length) { setBoxes({}); return; }
    let frame = 0;
    let previous = "";
    const measure = () => {
      const next: Record<string, Box> = {};
      for (const item of highlights) {
        const rect = resolveHighlightTarget(item.target)?.getBoundingClientRect();
        if (rect && rect.width && rect.height) next[item.target] = { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
      }
      const key = JSON.stringify(next);
      if (key !== previous) { previous = key; setBoxes(next); }
      frame = requestAnimationFrame(measure);
    };
    frame = requestAnimationFrame(measure);
    const dismiss = () => clearHighlights();
    document.addEventListener("pointerdown", dismiss, true);
    return () => { cancelAnimationFrame(frame); document.removeEventListener("pointerdown", dismiss, true); };
  }, [highlights]);
  if (!highlights.length) return null;
  return (
    <div className="ai-highlight-layer" aria-hidden="true" data-count={highlights.length}>
      {highlights.map(item => {
        const box = boxes[item.target];
        if (!box) return null;
        return <HighlightBox key={item.target} item={item} box={box} />;
      })}
    </div>
  );
}

/** Bubble width budget: the CSS max-width (320px or 70vw) plus its gap. */
const bubbleRoom = () => Math.min(320, window.innerWidth * 0.7) + 12;

function HighlightBox({ item, box }: { item: Highlight; box: Box }) {
  // The bubble stays inside the viewport: beside a narrow target that hugs a side edge (left of a
  // right-edge target, right of a left-edge one), otherwise above it, or below near the top edge.
  const width = window.innerWidth, room = bubbleRoom();
  const narrow = box.width < 160;
  const side = narrow && box.x + box.width + room > width && box.x > room ? "left"
    : narrow && box.x < room && box.x + box.width + room <= width ? "right"
    : box.y < 48 ? "below" : "above";
  return (
    <div className={`ai-highlight ${item.style}`} data-target={item.target}
      style={{ left: box.x - 4, top: box.y - 4, width: box.width + 8, height: box.height + 8 }}>
      {item.label ? <span className={`ai-highlight-bubble ${side} ${box.x + room > width ? "end" : "start"}`}>{item.label}</span> : null}
    </div>
  );
}
