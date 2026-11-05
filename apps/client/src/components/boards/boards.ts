import { useCallback, useMemo, useRef, useState } from "react";
import { createRemoveUpdate, getLeaves, getNodeAtPath, getParentPath, isSplitNode, updateTree, type MosaicNode, type MosaicPath } from "react-mosaic-component";
import type { PreferencesDocument } from "../../userPreferences";

/**
 * 看板 · 铺满版 (owner 2026-11-05, “按新稿做”): the canvas is always filled. A board is a split tree (react-mosaic's
 * n-ary tree: a leaf is a module, a split shares its room among its children by percentages), so what is stored are
 * proportions, never pixels — any window shows the whole board. Each block picks its size from the room it measures
 * (小 / 中 / 大, the chart also 加大); a block can be locked to 小 or 中. The boards live in the account preference
 * `ui.boards` (mirrors the Runtime's BoardsPreferences): 盘前 / 盘中 / 复盘 first, then the user's own; `current` is the one
 * shown. Every module follows the current contract (颜色联动 comes with PR 二).
 */
export const BOARDS_MAX = 11;
const NAME_MAX = 12;

export type BoardModule = "chart" | "positions" | "order" | "watch" | "sheet" | "alert" | "chat";
export type BoardSize = "small" | "medium" | "large" | "xlarge" | "bar";
export type BoardLock = "small" | "medium";
export type BoardNode = MosaicNode<BoardModule>;
export interface Board { id: string; name: string; layout: BoardNode | null; locks: Partial<Record<BoardModule, BoardLock>> }
export interface Boards { current: string; items: Board[] }
export type BoardSide = "left" | "right" | "top" | "bottom";

export const SIZE_LABELS: Record<BoardSize, string> = { small: "小", medium: "中", large: "大", xlarge: "加大", bar: "一条" };
export const MODULES: { id: BoardModule; label: string; sizes: BoardSize[] }[] = [
  { id: "chart", label: "图表", sizes: ["small", "medium", "large", "xlarge"] },
  { id: "positions", label: "持仓", sizes: ["small", "medium", "large"] },
  { id: "order", label: "下单", sizes: ["small", "medium", "large"] },
  { id: "watch", label: "自选报价", sizes: ["small", "medium", "large"] },
  { id: "sheet", label: "作战表", sizes: ["small", "medium", "large"] },
  // 提醒: no large size (the design's 砍: the list is short; 新建 and editing open the 提醒 panel).
  { id: "alert", label: "提醒", sizes: ["small", "medium"] },
  // 对话: 一条 when the block is a thin strip.
  { id: "chat", label: "对话", sizes: ["bar", "medium", "large"] },
];
const MODULE_IDS = new Set(MODULES.map(item => item.id));
export const moduleInfo = (module: BoardModule) => MODULES.find(item => item.id === module)!;

/**
 * The size a block of `w` × `h` CSS px shows (the design's thresholds at 100 % type; they grow half as fast as the
 * font preference). A lock picks 小 or 中 instead.
 */
export function pickSize(module: BoardModule, w: number, h: number, fontScale: number, lock?: BoardLock): BoardSize {
  const g = 1 + (fontScale - 1) / 2;
  const small = w < 420 * g || h < 230 * g;
  if (module === "chat") {
    if (lock === "small" || h < 150 * g) return "bar";
    return lock === "medium" || !(w >= 560 * g && h >= 380 * g) ? "medium" : "large";
  }
  if (lock) return lock;
  if (module === "alert") return small ? "small" : "medium";
  if (module === "chart" && w >= 1000 * g && h >= 560 * g) return "xlarge";
  if (w >= 620 * g && h >= 380 * g) return "large";
  return small ? "small" : "medium";
}

const split = (direction: "row" | "column", children: BoardNode[], splitPercentages: number[]): BoardNode =>
  ({ type: "split", direction, children, splitPercentages });
/** The design's three, as proportions. */
export const DEFAULT_LAYOUTS: Record<"pre" | "live" | "review", BoardNode> = {
  live: split("row", [split("column", ["chart", split("row", ["positions", "chat"], [50, 50])], [74, 26]),
    split("column", ["order", "alert", "watch"], [26, 24, 50])], [72, 28]),
  pre: split("row", [split("column", ["sheet", "chart"], [56, 44]), split("column", ["watch", "alert", "chat"], [50, 25, 25])], [52, 48]),
  review: split("row", [split("column", ["positions", "chart"], [56, 44]), split("column", ["chat", "sheet", "alert"], [50, 30, 20])], [55, 45]),
};
const defaultLayout = (id: string): BoardNode | null => (DEFAULT_LAYOUTS as Record<string, BoardNode>)[id] ?? null;
export const DEFAULT_BOARDS: Boards = { current: "live", items: [
  { id: "pre", name: "盘前", layout: DEFAULT_LAYOUTS.pre, locks: {} },
  { id: "live", name: "盘中", layout: DEFAULT_LAYOUTS.live, locks: {} },
  { id: "review", name: "复盘", layout: DEFAULT_LAYOUTS.review, locks: {} },
] };

/** A stored node as the screen can draw it: known modules once each, splits of two or more, percentages summing to 100. */
function cleanNode(raw: unknown, seen: Set<BoardModule>): BoardNode | null {
  if (typeof raw === "string") {
    const module = raw as BoardModule;
    if (!MODULE_IDS.has(module) || seen.has(module)) return null;
    seen.add(module);
    return module;
  }
  if (!raw || typeof raw !== "object" || (raw as { type?: unknown }).type !== "split") return null;
  const node = raw as { direction?: unknown; children?: unknown; splitPercentages?: unknown };
  if (node.direction !== "row" && node.direction !== "column") return null;
  const given = Array.isArray(node.splitPercentages) ? node.splitPercentages : [];
  const kept: { child: BoardNode; share: number }[] = [];
  (Array.isArray(node.children) ? node.children : []).forEach((child, index) => {
    const clean = cleanNode(child, seen);
    const share = typeof given[index] === "number" && given[index] > 0 ? given[index] as number : 0;
    if (clean !== null) kept.push({ child: clean, share });
  });
  if (!kept.length) return null;
  if (kept.length === 1) return kept[0].child;
  const total = kept.reduce((sum, item) => sum + item.share, 0);
  const shares = total > 0 && kept.every(item => item.share > 0) ? kept.map(item => item.share / total * 100) : kept.map(() => 100 / kept.length);
  return split(node.direction, kept.map(item => item.child), shares);
}

/**
 * A stored document as the screen can show it. A board from the grid version (2026-11-05 morning, `tiles` / `cols` /
 * `rows`, only test accounts) reads as the default arrangement of its id (a user board: empty).
 */
export function normalizeBoards(raw: Partial<Boards> | undefined | null): Boards {
  const items = (Array.isArray(raw?.items) ? raw.items : []).flatMap(item => {
    if (!item || typeof item.id !== "string" || typeof item.name !== "string") return [];
    const legacy = !("layout" in item) || "tiles" in item;
    const layout = legacy ? defaultLayout(item.id) : cleanNode(item.layout, new Set());
    const locks: Board["locks"] = {};
    Object.entries(item.locks ?? {}).forEach(([module, lock]) => {
      if (MODULE_IDS.has(module as BoardModule) && (lock === "small" || lock === "medium")) locks[module as BoardModule] = lock;
    });
    return [{ id: item.id, name: item.name, layout, locks }];
  }).slice(0, BOARDS_MAX);
  if (!items.length) return DEFAULT_BOARDS;
  return { current: items.some(item => item.id === raw?.current) ? raw!.current! : items[0].id, items };
}

export const leavesOf = (layout: BoardNode | null): BoardModule[] => layout === null ? [] : getLeaves(layout);
export function pathOf(layout: BoardNode | null, module: BoardModule, path: MosaicPath = []): MosaicPath | null {
  if (layout === null) return null;
  if (layout === module) return path;
  if (isSplitNode(layout)) {
    for (let index = 0; index < layout.children.length; index += 1) {
      const found = pathOf(layout.children[index], module, [...path, index]);
      if (found) return found;
    }
  }
  return null;
}

/** Split the node at `path` so `module` takes the `side` part with `share` % (the rest stays the node). */
export function insertAt(layout: BoardNode | null, path: MosaicPath, side: BoardSide, module: BoardModule, share = 50): BoardNode {
  if (layout === null) return module;
  const node = getNodeAtPath(layout, path);
  if (node === null) throw new Error("那一块已经不在看板上了");
  const first = side === "left" || side === "top";
  const s = Math.min(90, Math.max(10, share));
  const next = split(side === "left" || side === "right" ? "row" : "column", first ? [module, node] : [node, module], first ? [s, 100 - s] : [100 - s, s]);
  return path.length ? updateTree(layout, [{ path, spec: { $set: next } }]) : next;
}

export function removeLeaf(layout: BoardNode | null, module: BoardModule): BoardNode | null {
  const path = pathOf(layout, module);
  if (layout === null || path === null) return layout;
  return path.length ? updateTree(layout, [createRemoveUpdate(layout, path)]) : null;
}

/** The share (%) of `module`'s block in the split that holds it, and that split's direction. */
export function shareOf(layout: BoardNode | null, module: BoardModule): { share: number; direction: "row" | "column" } | null {
  const path = pathOf(layout, module);
  if (layout === null || !path?.length) return null;
  const parent = getNodeAtPath(layout, getParentPath(path));
  if (!parent || !isSplitNode(parent)) return null;
  const index = path[path.length - 1];
  return { share: parent.splitPercentages?.[index] ?? 100 / parent.children.length, direction: parent.direction };
}

export interface BoardsChange {
  select?: string; create?: string; rename?: { id: string; name: string }; remove?: string;
  /** Put a module beside `target` on its `side` taking `ratio` (0.1–0.9) of that block; without a target beside the whole
   *  board (a third). A module already on the board moves there. */
  place?: { module: BoardModule; target?: BoardModule; side?: BoardSide; ratio?: number };
  /** 换成别的模块: the block of `module` shows `with` instead. */
  replace?: { module: BoardModule; with: BoardModule };
  /** Take a module off; its neighbours fill the room. */
  take?: BoardModule;
  /** 拖分隔线: `module`'s block gets `value` (0.05–0.95) of the split holding it, the others share the rest. */
  ratio?: { module: BoardModule; value: number };
  /** 档位: auto (follow the room) or lock to 小 / 中. */
  lock?: { module: BoardModule; size: "auto" | BoardLock };
  /** A whole new layout (a finished drag, resize or drop): what react-mosaic handed back. */
  layout?: BoardNode | null;
}

const KEYS = ["select", "create", "rename", "remove", "place", "replace", "take", "ratio", "lock", "layout"] as const;

/** What one change does to the list (pure; throws a readable refusal). */
export function planBoardsChange(change: BoardsChange, boards: Boards): Boards {
  const keys = KEYS.filter(key => change[key] !== undefined);
  if (keys.length !== 1) throw new Error("boards 一次只做一件：select / create / rename / remove / place / replace / take / ratio / lock");
  const named = (name: string) => {
    const clean = name.trim();
    if (!clean || clean.length > NAME_MAX) throw new Error(`看板名 1–${NAME_MAX} 个字`);
    return clean;
  };
  const current = boards.items.find(item => item.id === boards.current) ?? boards.items[0];
  const withCurrent = (patch: Partial<Board>) => ({ ...boards, items: boards.items.map(item => item.id === current.id ? { ...item, ...patch } : item) });
  const label = (module: BoardModule) => moduleInfo(module)?.label ?? module;
  const known = (module: BoardModule) => { if (!MODULE_IDS.has(module)) throw new Error(`没有“${module}”这个模块`); };
  const onBoard = (module: BoardModule) => { if (!leavesOf(current.layout).includes(module)) throw new Error(`这个看板上没有${label(module)}`); };
  if (change.select !== undefined) {
    if (!boards.items.some(item => item.id === change.select)) throw new Error("没有这个看板");
    return { ...boards, current: change.select };
  }
  if (change.create !== undefined) {
    const name = named(change.create);
    if (boards.items.length >= BOARDS_MAX) throw new Error(`最多存 ${BOARDS_MAX} 个看板，先删一个`);
    if (boards.items.some(item => item.name === name)) throw new Error(`已经有叫“${name}”的看板`);
    const id = `b${Date.now().toString(36)}`;
    return { current: id, items: [...boards.items, { id, name, layout: null, locks: {} }] };
  }
  if (change.rename !== undefined) {
    const { id } = change.rename;
    const name = named(change.rename.name);
    if (!boards.items.some(item => item.id === id)) throw new Error("没有这个看板");
    if (boards.items.some(item => item.name === name && item.id !== id)) throw new Error(`已经有叫“${name}”的看板`);
    return { ...boards, items: boards.items.map(item => item.id === id ? { ...item, name } : item) };
  }
  if (change.remove !== undefined) {
    if (!boards.items.some(item => item.id === change.remove)) throw new Error("没有这个看板");
    if (boards.items.length === 1) throw new Error("至少留一个看板");
    const rest = boards.items.filter(item => item.id !== change.remove);
    return { current: boards.current === change.remove ? rest[0].id : boards.current, items: rest };
  }
  if (change.layout !== undefined) return withCurrent({ layout: change.layout === null ? null : cleanNode(change.layout, new Set()) });
  if (change.take !== undefined) {
    onBoard(change.take);
    const locks = { ...current.locks };
    delete locks[change.take];
    return withCurrent({ layout: removeLeaf(current.layout, change.take), locks });
  }
  if (change.replace !== undefined) {
    const { module, with: next } = change.replace;
    known(next);
    onBoard(module);
    if (module === next) return boards;
    if (leavesOf(current.layout).includes(next)) throw new Error(`${label(next)}已经在这个看板上了，拿掉它或换成别的`);
    const path = pathOf(current.layout, module)!;
    const layout = path.length ? updateTree(current.layout!, [{ path, spec: { $set: next } }]) : next;
    const locks = { ...current.locks };
    delete locks[module];
    return withCurrent({ layout, locks });
  }
  if (change.ratio !== undefined) {
    const { module, value } = change.ratio;
    onBoard(module);
    const path = pathOf(current.layout, module)!;
    if (!path.length) throw new Error(`${label(module)}占着整块看板，没有可调的分隔线`);
    const parentPath = getParentPath(path);
    const parent = getNodeAtPath(current.layout!, parentPath);
    if (!parent || !isSplitNode(parent)) throw new Error("那一块已经不在看板上了");
    const want = Math.min(95, Math.max(5, value * 100));
    const index = path[path.length - 1];
    const old = parent.splitPercentages ?? parent.children.map(() => 100 / parent.children.length);
    const restOld = 100 - old[index];
    const shares = old.map((share, at) => at === index ? want : restOld > 0 ? share / restOld * (100 - want) : (100 - want) / (old.length - 1));
    const next = { ...parent, splitPercentages: shares };
    return withCurrent({ layout: parentPath.length ? updateTree(current.layout!, [{ path: parentPath, spec: { $set: next } }]) : next });
  }
  if (change.lock !== undefined) {
    const { module, size } = change.lock;
    onBoard(module);
    const locks = { ...current.locks };
    if (size === "auto") delete locks[module]; else locks[module] = size;
    return withCurrent({ locks });
  }
  const place = change.place!;
  known(place.module);
  if (place.target === place.module) throw new Error("不能放在它自己旁边");
  const ratio = place.ratio ?? (place.target ? 0.5 : 0.33);
  if (ratio < 0.1 || ratio > 0.9) throw new Error("ratio 在 0.1 到 0.9 之间");
  const without = removeLeaf(current.layout, place.module);
  if (place.target !== undefined) {
    known(place.target);
    const path = pathOf(without, place.target);
    if (path === null) throw new Error(`这个看板上没有${label(place.target)}，先放它，或放在别的模块旁边`);
    return withCurrent({ layout: insertAt(without, path, place.side ?? "right", place.module, ratio * 100) });
  }
  return withCurrent({ layout: insertAt(without, [], place.side ?? "right", place.module, ratio * 100) });
}

export interface BoardsHandle {
  boards: Boards;
  board: Board;
  apply: (change: BoardsChange) => void;
  /** 撤销 while editing: the board as it was before the last change. */
  undo: (() => void) | undefined;
  /** Editing starts a fresh undo history. */
  resetUndo: () => void;
  /** A resize drag in progress: shown at once, saved (and undoable) when released. */
  preview: (layout: BoardNode | null) => void;
}

/** A member's boards (account preference `ui.boards`); a change shows at once and is saved behind it. */
export function useBoards({ document, connected, update }: {
  document: Partial<Boards> | undefined;
  connected: boolean;
  update: (patch: Record<string, unknown>) => Promise<PreferencesDocument>;
}): BoardsHandle {
  const [pending, setPending] = useState<Boards>();
  const [dragging, setDragging] = useState<BoardNode | null | undefined>();
  const [history, setHistory] = useState<Boards[]>([]);
  const saved = useMemo(() => pending ?? normalizeBoards(document), [pending, document]);
  const latest = useRef(saved);
  latest.current = saved;
  const write = useCallback((next: Boards) => {
    setPending(next);
    const settle = () => setPending(shown => shown === next ? undefined : shown);
    void update({ ui: { boards: next } }).then(settle, settle);
  }, [update]);
  const apply = useCallback((change: BoardsChange) => {
    if (!connected) throw new Error("连接后可改看板");
    const before = latest.current;
    const next = planBoardsChange(change, before);
    setDragging(undefined);
    if (!(change.select ?? change.create ?? change.rename ?? change.remove)) setHistory(list => [...list.slice(-19), before]);
    write(next);
  }, [connected, write]);
  const stored = saved.items.find(item => item.id === saved.current) ?? saved.items[0];
  const board = dragging !== undefined ? { ...stored, layout: dragging } : stored;
  const boards = dragging !== undefined ? { ...saved, items: saved.items.map(item => item.id === stored.id ? board : item) } : saved;
  const undo = history.length ? () => {
    const previous = history.at(-1)!;
    setHistory(list => list.slice(0, -1));
    write({ ...latest.current, items: latest.current.items.map(item => item.id === previous.current ? previous.items.find(old => old.id === item.id) ?? item : item) });
  } : undefined;
  const resetUndo = useCallback(() => setHistory([]), []);
  const preview = useCallback((layout: BoardNode | null) => setDragging(layout), []);
  return { boards, board, apply, undo, resetUndo, preview };
}
