import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";

/**
 * 一套自适应 (owner 2026-11-05): every desktop toolbar row — the header here, the market bar above the chart
 * (MarketCommand) — fits itself with useFitLevel below: measured space, cumulative level classes, never a
 * viewport breakpoint or a wrapped second line.
 *
 * The header gives content up in measured-space levels instead of viewport breakpoints:
 * 0 shows everything, 1 shrinks the AI 指令栏 to a ✦ ⌘K button and 2 drops the 美元 half of the equity
 * chip (both give way first, owner 2026-11-01: the chip's hover and 账户明细 still show it), 3 hides the
 * 复盘/回放 labels, 4 the brand sub-label 模拟盘, 5 folds the contract tabs into a single dropdown, 6 the equity.
 * Connection/queue warnings and the recovery button never hide —
 * they stay and truncate when even level 6 overflows.
 */
export const HEADER_FIT_MAX_LEVEL = 6;

const FIT_CLASSES = ["fdw-fit-compact-ai", "fdw-fit-hide-usd", "fdw-fit-hide-text", "fdw-fit-hide-brand", "fdw-fit-contract-menu", "fdw-fit-hide-equity"];

/** Cumulative level classes: level n carries the first n classes of the row's list. */
export function fitClasses(classes: readonly string[], level: number): string {
  return classes.slice(0, Math.max(0, Math.min(level, classes.length))).join(" ");
}

/** The header's: a level-5 header also carries the level-1 to level-4 rules. */
export function headerFitClasses(level: number): string {
  return fitClasses(FIT_CLASSES, level);
}

/** Writes one level straight onto the DOM node: the level classes plus data-fit. */
function applyFitLevel(node: HTMLElement, classes: readonly string[], level: number) {
  node.classList.remove(...classes);
  node.classList.add(...fitClasses(classes, level).split(" ").filter(Boolean));
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
function childrenSqueeze(parent: HTMLElement, floating: string): boolean {
  const children = (Array.from(parent.children) as HTMLElement[]).filter(child => isDisplayed(child) && !child.matches(floating));
  const placed: DOMRect[] = [];
  for (const child of children) {
    const rect = child.getBoundingClientRect();
    if (rect.width < 1 && hasDisplayedContent(child)) return true;
    // Only neighbours on the same line can overlap sideways: a status line or a second row below starts at the left again.
    if (placed.some(prev => rect.left < prev.right - 1 && rect.top < prev.top + prev.height - 1 && prev.top < rect.top + rect.height - 1)) return true;
    placed.push(rect);
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
  return rowContentOverflows(header, WHOLE_CELLS, FLOATING_CELL);
}

/** The same test for any fit row: the row, its direct children (bar `floating`) and its `wholeCells`. */
export function rowContentOverflows(row: HTMLElement, wholeCells: readonly string[], floating = ":not(*)"): boolean {
  if (cellOverflows(row)) return true;
  const cells = [
    row,
    ...(Array.from(row.children) as HTMLElement[]).filter(child => !child.matches(floating)),
    ...wholeCells.flatMap(selector => Array.from(row.querySelectorAll<HTMLElement>(selector))),
  ];
  return cells.some(cell => isDisplayed(cell) && (cellOverflows(cell) || childrenSqueeze(cell, floating)));
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
  return useFitLevel(header, contentKey, FIT_CLASSES, headerContentOverflows);
}

/** The shared walk: `classes` are the row's levels in give-up order, `overflows` its measurement. */
export function useFitLevel(row: RefObject<HTMLElement | null>, contentKey: string, classes: readonly string[],
  overflows: (row: HTMLElement) => boolean): number {
  const [level, setLevel] = useState(0);
  const frame = useRef(0);

  const measure = useCallback(() => {
    const node = row.current;
    if (!node) return;
    const settled = resolveHeaderFitLevel(candidate => {
      applyFitLevel(node, classes, candidate);
      return !overflows(node);
    }, classes.length);
    applyFitLevel(node, classes, settled);
    setLevel(settled);
  }, [row, classes, overflows]);

  useLayoutEffect(() => {
    measure();
  }, [contentKey, measure]);

  useLayoutEffect(() => {
    const node = row.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (frame.current) return;
      frame.current = scheduleFrame(() => {
        frame.current = 0;
        measure();
      });
    });
    observer.observe(node);
    // A row sized by its content (a folded control is narrower than its room) must hear its container grow back — and
    // the one above it, as a wrapper that sizes with the control does not grow either (the strategy editor's .se-mark
    // round a 分段选择, 11-07).
    const parent = node.parentElement;
    if (parent) observer.observe(parent);
    if (parent?.parentElement) observer.observe(parent.parentElement);
    return () => {
      observer.disconnect();
      if (frame.current && typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame.current);
      frame.current = 0;
    };
  }, [row, measure]);

  return level;
}
