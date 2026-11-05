import { useCallback, useMemo, useRef, useState } from "react";
import type { PreferencesDocument } from "../../userPreferences";

/**
 * 看板 (owner 2026-11-05, 第二批): a blank screen of cells the user fills with modules, each at one of its sizes (each
 * size has its own layout, the small ones keep only what matters most). A cell keeps one size (scaled by the font
 * preference), so the canvas has as many columns and rows as its measured space holds — about 12 × 8 on a laptop,
 * more on a big or ultra-wide screen — and a module keeps its designed look everywhere. Each board remembers the
 * grid it was laid out on: on a larger canvas the extra cells are empty, on a smaller one the modules that do not
 * fit move down and the page scrolls, never squeezed. The boards live in the account preference `ui.boards` (mirrors
 * the Runtime's BoardsPreferences): 盘前 / 盘中 / 复盘 first, then the user's own; `current` is the one shown. Every
 * module follows the current contract (颜色联动 comes with 第三批).
 */
export const DEFAULT_COLS = 12;
export const DEFAULT_ROWS = 8;
export const MAX_COLS = 40;
export const MAX_ROWS = 30;
/** One cell at 100 % type (标准字号); the 大字 default makes a laptop's canvas about 12 × 8. */
export const CELL = { w: 92, h: 78 };
export const BOARDS_MAX = 11;
const NAME_MAX = 12;

export type BoardModule = "chart" | "positions" | "order" | "watch";
export type BoardSize = "small" | "medium" | "large" | "xlarge" | "wide" | "full";
export interface BoardTile { module: BoardModule; size: BoardSize; x: number; y: number }
/** `cols` × `rows`: the grid the board was laid out on. */
export interface Board { id: string; name: string; cols: number; rows: number; tiles: BoardTile[] }
export interface Boards { current: string; items: Board[] }

export const SIZE_CELLS: Record<BoardSize, { w: number; h: number; label: string }> = {
  small: { w: 3, h: 2, label: "小" }, medium: { w: 6, h: 2, label: "中" }, large: { w: 6, h: 4, label: "大" },
  xlarge: { w: 9, h: 6, label: "加大" }, wide: { w: 12, h: 4, label: "通栏" }, full: { w: 12, h: 8, label: "整版" },
};
export const MODULES: { id: BoardModule; label: string; sizes: BoardSize[] }[] = [
  { id: "chart", label: "图表", sizes: ["small", "medium", "large", "xlarge", "full"] },
  { id: "positions", label: "持仓", sizes: ["small", "medium", "large"] },
  { id: "order", label: "下单", sizes: ["small", "medium", "large"] },
  { id: "watch", label: "自选报价", sizes: ["small", "medium", "large", "wide"] },
];
const MODULE_IDS = new Set(MODULES.map(item => item.id));
export const moduleInfo = (module: BoardModule) => MODULES.find(item => item.id === module)!;

const tile = (module: BoardModule, size: BoardSize, x: number, y: number): BoardTile => ({ module, size, x, y });
/** The three the owner's design starts with, filled with the modules this batch has. */
const grid = { cols: DEFAULT_COLS, rows: DEFAULT_ROWS };
export const DEFAULT_BOARDS: Boards = { current: "live", items: [
  { id: "pre", name: "盘前", ...grid, tiles: [tile("chart", "large", 0, 0), tile("watch", "large", 6, 0), tile("positions", "medium", 0, 4)] },
  { id: "live", name: "盘中", ...grid, tiles: [tile("chart", "xlarge", 0, 0), tile("order", "small", 9, 0), tile("watch", "small", 9, 2), tile("positions", "medium", 0, 6)] },
  { id: "review", name: "复盘", ...grid, tiles: [tile("positions", "large", 0, 0), tile("chart", "large", 6, 0)] },
] };

const overlaps = (a: BoardTile, b: BoardTile) => {
  const sa = SIZE_CELLS[a.size], sb = SIZE_CELLS[b.size];
  return a.x < b.x + sb.w && b.x < a.x + sa.w && a.y < b.y + sb.h && b.y < a.y + sa.h;
};
interface Grid { cols: number; rows: number }
const fits = (item: BoardTile, area: Grid) => item.x >= 0 && item.y >= 0
  && item.x + SIZE_CELLS[item.size].w <= area.cols && item.y + SIZE_CELLS[item.size].h <= area.rows;
const validTile = (item: BoardTile, area: Grid) => MODULE_IDS.has(item.module) && moduleInfo(item.module).sizes.includes(item.size)
  && Number.isInteger(item.x) && Number.isInteger(item.y) && fits(item, area);
const clampInt = (value: unknown, low: number, high: number, fallback: number) =>
  typeof value === "number" && Number.isInteger(value) ? Math.min(high, Math.max(low, value)) : fallback;

/** A stored document as the screen can show it: unknown modules, sizes that do not fit and overlaps drop out. */
export function normalizeBoards(raw: Partial<Boards> | undefined | null): Boards {
  const items = (Array.isArray(raw?.items) ? raw.items : []).flatMap(item => {
    if (!item || typeof item.id !== "string" || typeof item.name !== "string") return [];
    const area = { cols: clampInt(item.cols, 3, MAX_COLS, DEFAULT_COLS), rows: clampInt(item.rows, 2, MAX_ROWS, DEFAULT_ROWS) };
    const tiles: BoardTile[] = [];
    for (const one of Array.isArray(item.tiles) ? item.tiles : []) {
      if (one && validTile(one, area) && !tiles.some(other => other.module === one.module || overlaps(other, one))) tiles.push({ ...one });
    }
    return [{ id: item.id, name: item.name, ...area, tiles }];
  }).slice(0, BOARDS_MAX);
  if (!items.length) return DEFAULT_BOARDS;
  return { current: items.some(item => item.id === raw?.current) ? raw!.current! : items[0].id, items };
}

/** The first top-left spot where `size` fits beside the other tiles, or null. */
export function freeSpot(tiles: BoardTile[], module: BoardModule, size: BoardSize, area: Grid): { x: number; y: number } | null {
  const others = tiles.filter(item => item.module !== module);
  for (let y = 0; y <= area.rows - SIZE_CELLS[size].h; y += 1) {
    for (let x = 0; x <= area.cols - SIZE_CELLS[size].w; x += 1) {
      const probe = { module, size, x, y };
      if (!others.some(other => overlaps(other, probe))) return { x, y };
    }
  }
  return null;
}

/** How many cells a measured canvas holds at this type scale (never fewer than one small module). */
export function measuredGrid(width: number, height: number, scale: number, gap: number): Grid {
  const w = CELL.w * scale, h = CELL.h * scale;
  return { cols: Math.min(MAX_COLS, Math.max(3, Math.floor((width + gap) / (w + gap)))),
    rows: Math.min(MAX_ROWS, Math.max(2, Math.floor((height + gap) / (h + gap)))) };
}

/**
 * The board as a canvas of `screen` cells shows it: a board laid out on more columns keeps every tile that still fits
 * where it is and moves the others down (never narrower — a module wider than the canvas widens it and the canvas
 * scrolls sideways); the grid grows downward for what moved, and a larger canvas only adds empty cells.
 */
export function shownBoard(board: Board, screen: Grid | null): Grid & { tiles: BoardTile[] } {
  if (!screen) return { cols: board.cols, rows: board.rows, tiles: board.tiles };
  const cols = Math.max(screen.cols, ...board.tiles.map(item => SIZE_CELLS[item.size].w));
  const placed: BoardTile[] = [];
  for (const item of [...board.tiles].sort((a, b) => a.y - b.y || a.x - b.x)) {
    const size = SIZE_CELLS[item.size];
    const next = { ...item, x: Math.min(item.x, cols - size.w) };
    while (placed.some(other => overlaps(other, next))) next.y += 1;
    placed.push(next);
  }
  const used = Math.max(0, ...placed.map(item => item.y + SIZE_CELLS[item.size].h));
  return { cols, rows: Math.min(MAX_ROWS, Math.max(screen.rows, used)), tiles: board.tiles.map(item => placed.find(one => one.module === item.module)!) };
}

export interface BoardsChange {
  select?: string; create?: string; rename?: { id: string; name: string }; remove?: string;
  /** Put a module on the current board, or move / resize the one there (absent x, y: where it is, else the first free spot). */
  place?: { module: BoardModule; size?: BoardSize; x?: number; y?: number };
  /** Take a module off the current board. */
  take?: BoardModule;
}

/** What one change does to the list (pure; throws a readable refusal). Placing or taking a module lays the board out
 *  on the grid on screen (`screen`, absent when no window shows it: the board's own grid). */
export function planBoardsChange(change: BoardsChange, boards: Boards, screen: Grid | null = null): Boards {
  const keys = Object.keys(change).filter(key => change[key as keyof BoardsChange] !== undefined);
  if (keys.length !== 1) throw new Error("boards 一次只做一件：select / create / rename / remove / place / take");
  const named = (name: string) => {
    const clean = name.trim();
    if (!clean || clean.length > NAME_MAX) throw new Error(`看板名 1–${NAME_MAX} 个字`);
    return clean;
  };
  const stored = boards.items.find(item => item.id === boards.current) ?? boards.items[0];
  const shown = shownBoard(stored, screen);
  const current = { ...stored, cols: shown.cols, rows: shown.rows, tiles: shown.tiles };
  const withCurrent = (tiles: BoardTile[]) => ({ ...boards, items: boards.items.map(item => item.id === current.id
    ? { ...item, cols: current.cols, rows: current.rows, tiles } : item) });
  if (change.select !== undefined) {
    if (!boards.items.some(item => item.id === change.select)) throw new Error("没有这个看板");
    return { ...boards, current: change.select };
  }
  if (change.create !== undefined) {
    const name = named(change.create);
    if (boards.items.length >= BOARDS_MAX) throw new Error(`最多存 ${BOARDS_MAX} 个看板，先删一个`);
    if (boards.items.some(item => item.name === name)) throw new Error(`已经有叫“${name}”的看板`);
    const id = `b${Date.now().toString(36)}`;
    return { current: id, items: [...boards.items, { id, name, cols: screen?.cols ?? DEFAULT_COLS, rows: screen?.rows ?? DEFAULT_ROWS, tiles: [] }] };
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
  if (change.take !== undefined) {
    if (!current.tiles.some(item => item.module === change.take)) throw new Error(`这个看板上没有${moduleInfo(change.take).label}`);
    return withCurrent(current.tiles.filter(item => item.module !== change.take));
  }
  const place = change.place!;
  if (!MODULE_IDS.has(place.module)) throw new Error("没有这个模块");
  const info = moduleInfo(place.module);
  const there = current.tiles.find(item => item.module === place.module);
  const size = place.size ?? there?.size ?? info.sizes[0];
  if (!info.sizes.includes(size)) throw new Error(`${info.label}没有“${SIZE_CELLS[size]?.label ?? size}”这一档，可选：${info.sizes.map(item => SIZE_CELLS[item].label).join(" / ")}`);
  const others = current.tiles.filter(item => item.module !== place.module);
  let spot: { x: number; y: number } | null;
  if (place.x !== undefined || place.y !== undefined) spot = { x: place.x ?? there?.x ?? 0, y: place.y ?? there?.y ?? 0 };
  else if (there && fits({ ...there, size }, current) && !others.some(other => overlaps(other, { ...there, size }))) spot = { x: there.x, y: there.y };
  else spot = freeSpot(current.tiles, place.module, size, current);
  if (!spot) throw new Error(`没有空位放${info.label}（${SIZE_CELLS[size].label}），先挪开或删掉别的模块`);
  const next = { module: place.module, size, ...spot };
  if (!fits(next, current)) throw new Error(`${info.label}（${SIZE_CELLS[size].label}）放在那里会超出画布`);
  if (others.some(other => overlaps(other, next))) throw new Error(`那里已经有别的模块了`);
  return withCurrent(there ? current.tiles.map(item => item.module === place.module ? next : item) : [...current.tiles, next]);
}

export interface BoardsHandle {
  boards: Boards;
  board: Board;
  apply: (change: BoardsChange) => void;
  /** 撤销 while editing: the boards as they were before the last change. */
  undo: (() => void) | undefined;
  /** Editing starts a fresh undo history. */
  resetUndo: () => void;
  /** The grid the board view measures (null while no window shows a board): placing lays the board out on it. */
  setScreen: (screen: Grid | null) => void;
}

/** A member's boards (account preference `ui.boards`); a change shows at once and is saved behind it. */
export function useBoards({ document, connected, update }: {
  document: Partial<Boards> | undefined;
  connected: boolean;
  update: (patch: Record<string, unknown>) => Promise<PreferencesDocument>;
}): BoardsHandle {
  const [pending, setPending] = useState<Boards>();
  const [history, setHistory] = useState<Boards[]>([]);
  const boards = useMemo(() => pending ?? normalizeBoards(document), [pending, document]);
  const latest = useRef(boards);
  latest.current = boards;
  const screen = useRef<Grid | null>(null);
  const setScreen = useCallback((value: Grid | null) => { screen.current = value; }, []);
  const write = useCallback((next: Boards) => {
    setPending(next);
    const settle = () => setPending(shown => shown === next ? undefined : shown);
    void update({ ui: { boards: next } }).then(settle, settle);
  }, [update]);
  const apply = useCallback((change: BoardsChange) => {
    if (!connected) throw new Error("连接后可改看板");
    const before = latest.current;
    const next = planBoardsChange(change, before, screen.current);
    if (change.place || change.take) setHistory(list => [...list.slice(-19), before]);
    write(next);
  }, [connected, write]);
  const board = boards.items.find(item => item.id === boards.current) ?? boards.items[0];
  const undo = history.length ? () => {
    const previous = history.at(-1)!;
    setHistory(list => list.slice(0, -1));
    write({ ...latest.current, items: latest.current.items.map(item => item.id === previous.current ? previous.items.find(old => old.id === item.id) ?? item : item) });
  } : undefined;
  const resetUndo = useCallback(() => setHistory([]), []);
  return { boards, board, apply, undo, resetUndo, setScreen };
}
