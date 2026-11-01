import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";

/**
 * The header gives content up in measured-space levels instead of viewport breakpoints:
 * 0 shows everything, 1 shrinks the AI 指令栏 to a ✦ ⌘K button and 2 drops the 美元 half of the equity
 * chip (both give way first, owner 2026-11-01: the chip's hover and 账户明细 still show it), 3 hides the
 * 复盘/回放 labels, 4 the brand sub-label 模拟盘, 5 folds the contract tabs into a single dropdown, 6 the equity.
 * Connection/queue warnings and the recovery button never hide —
 * they stay and truncate when even level 6 overflows.
 */
export const HEADER_FIT_MAX_LEVEL = 6;

const FIT_CLASSES = ["fdw-fit-compact-ai", "fdw-fit-hide-usd", "fdw-fit-hide-text", "fdw-fit-hide-brand", "fdw-fit-contract-menu", "fdw-fit-hide-equity"];

/** Cumulative level classes: a level-5 header also carries the level-1 to level-4 rules. */
export function headerFitClasses(level: number): string {
  return FIT_CLASSES.slice(0, Math.max(0, Math.min(level, HEADER_FIT_MAX_LEVEL))).join(" ");
}

/** Writes one level straight onto the DOM node: the level classes plus data-fit. */
function applyFitLevel(node: HTMLElement, level: number) {
  node.classList.remove(...FIT_CLASSES);
  node.classList.add(...headerFitClasses(level).split(" ").filter(Boolean));
  node.dataset.fit = String(level);
}

/**
 * The level decision, pure so tests can walk it: the first level whose layout fits,
 * starting from 0 on every pass so recovered width lowers the level again. Falls
 * back to the max level when even that overflows (warnings stay visible regardless).
 */
export function resolveHeaderFitLevel(fitsAtLevel: (level: number) => boolean, max: number = HEADER_FIT_MAX_LEVEL): number {
  for (let level = 0; level <= max; level += 1) {
    if (fitsAtLevel(level)) return level;
  }
  return max;
}

function cellOverflows(element: HTMLElement): boolean {
  return element.scrollWidth - element.clientWidth > 1;
}

/** Cells whose content must fit whole. The picker joins at level 5 (hidden otherwise);
 *  warning texts may ellipsize instead, so they are deliberately not probed. */
const WHOLE_CELLS = [".fdw-brand", "nav", ".fdw-contract-tabs", ".fdw-contract-picker", ".fdw-header-status"];

/** The AI 指令栏 is not probed: its result and its focused input float over the header (wider than its cell,
 *  which empties while the input floats), and its own width is the grid's to give — level 1 shrinks it. */
const FLOATING_CELL = ".ai-bar";

/** A header child the fit pass looks at. */
function probed(element: Element): boolean {
  return !element.matches(FLOATING_CELL);
}

/** Cells hidden by the fit levels themselves (or in layout-less jsdom) never count. */
function isDisplayed(element: Element): boolean {
  return element.getClientRects().length > 0;
}

/** Whether anything inside is actually shown — an emptied cell (status after level 6
 *  hid equity) is allowed to collapse to nothing. */
function hasDisplayedContent(element: HTMLElement): boolean {
  return Array.from(element.querySelectorAll("*")).some(node => isDisplayed(node));
}

/** A flex row that ran out of room: a child with content squeezed flat, or one drawn over its neighbour. */
function childrenSqueeze(parent: HTMLElement): boolean {
  const children = (Array.from(parent.children) as HTMLElement[]).filter(child => isDisplayed(child) && probed(child));
  let rightEdge = Number.NEGATIVE_INFINITY;
  for (const child of children) {
    const rect = child.getBoundingClientRect();
    if (rect.width < 1 && hasDisplayedContent(child)) return true;
    if (rect.left < rightEdge - 1) return true;
    rightEdge = Math.max(rightEdge, rect.right);
  }
  return false;
}

/**
 * Overflow means content that no longer fits, not just a clipped header: a cell that
 * scrolls (the header, its children, the tabs/picker/status cells), a child crushed to
 * zero width (the pin "+" when the tabs are squeezed) or neighbours overlapping (a tab
 * drawn over 复盘/回放 — the boxes fit while their content bleeds out).
 */
export function headerContentOverflows(header: HTMLElement): boolean {
  if (cellOverflows(header)) return true;
  const cells = [
    header,
    ...(Array.from(header.children) as HTMLElement[]).filter(probed),
    ...WHOLE_CELLS.flatMap(selector => Array.from(header.querySelectorAll<HTMLElement>(selector))),
  ];
  return cells.some(cell => isDisplayed(cell) && (cellOverflows(cell) || childrenSqueeze(cell)));
}

/** jsdom has no requestAnimationFrame; the coalescing then degrades to running inline. */
function scheduleFrame(callback: () => void): number {
  if (typeof requestAnimationFrame === "function") return requestAnimationFrame(callback);
  callback();
  return 0;
}

/**
 * Resolves the header's collapse level from the space it actually has. One synchronous
 * pass per trigger: each level is applied straight to the live DOM (the level classes
 * switch the CSS, including the level-5 tabs/picker swap, which is why both subtrees
 * stay mounted) and measured before the next is tried, so the walk settles before the
 * browser paints — no flicker, no climb state to race. The final level is written to
 * the DOM and mirrored into state in one go. Passes re-run on header resizes (coalesced
 * per frame) and on contentKey changes (pinned contracts, status content, account
 * directory, replay state); a pass itself never changes the header's box size, so it
 * cannot re-trigger itself.
 */
export function useHeaderFit(header: RefObject<HTMLElement | null>, contentKey: string): number {
  const [level, setLevel] = useState(0);
  const frame = useRef(0);

  const measure = useCallback(() => {
    const node = header.current;
    if (!node) return;
    const settled = resolveHeaderFitLevel(candidate => {
      applyFitLevel(node, candidate);
      return !headerContentOverflows(node);
    });
    applyFitLevel(node, settled);
    setLevel(settled);
  }, [header]);

  useLayoutEffect(() => {
    measure();
  }, [contentKey, measure]);

  useLayoutEffect(() => {
    const node = header.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (frame.current) return;
      frame.current = scheduleFrame(() => {
        frame.current = 0;
        measure();
      });
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
      if (frame.current && typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame.current);
      frame.current = 0;
    };
  }, [header, measure]);

  return level;
}
