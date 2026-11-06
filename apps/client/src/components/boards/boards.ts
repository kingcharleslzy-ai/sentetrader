import { useCallback, useMemo, useRef, useState } from "react";
import { createRemoveUpdate, getLeaves, getNodeAtPath, getParentPath, isSplitNode, updateTree, type MosaicNode, type MosaicPath } from "react-mosaic-component";
import type { PreferencesDocument } from "../../userPreferences";
import { SUB_PANES_MAX, type SubPaneType } from "../chartIndicators";

/**
 * 看板 · 铺满版 (owner 2026-11-05, “按新稿做”): the canvas is always filled. A board is a split tree (react-mosaic's
 * n-ary tree: a leaf is a block, a split shares its room among its children by percentages), so what is stored are
 * proportions, never pixels — any window shows the whole board. Each block picks its size from the room it measures
 * (小 / 中 / 大, the chart also 加大); a block can be locked to 小 or 中. The boards live in the account preference
 * `ui.boards` (mirrors the Runtime's BoardsPreferences): 盘前 / 盘中 / 复盘 first, then the user's own; `current` is the one
 * shown. 色点联动: 图表 / 下单 / 自选 / 作战表 carry a colour dot; blocks of one colour follow one contract.
 *
 * A block is named after its module (`chart`, `order`…); 图表 may be placed several times (owner 2026-11-06, 大屏要放很多
 * 行情): `chart`, then `chart-2` … `chart-9`, each with its own contract and period (`charts`) unless its colour says
 * otherwise. Every other module stays one block per board.
 */
export const BOARDS_MAX = 11;
const NAME_MAX = 12;
export const CHARTS_MAX = 9;

export type BoardModule = "chart" | "positions" | "order" | "watch" | "sheet" | "alert" | "chat" | "account" | "orders" | "strategies" | "flash" | "pnl" | "debate" | "notes" | "briefs";
/** A block on a board: its module's name, or `chart-2` … `chart-9` for the further charts. */
export type BoardBlock = string;
export type BoardSize = "small" | "medium" | "large" | "xlarge" | "bar";
export type BoardLock = "small" | "medium";
export type BoardNode = MosaicNode<BoardBlock>;
/** The modules that show a contract and so carry a 联动 dot; 持仓 / 提醒 / 对话 cover every contract. */
export type LinkModule = "chart" | "order" | "watch" | "sheet";
// Not red or green: those are the market direction colours.
export type LinkColor = "blue" | "purple" | "orange";
/** A chart may also follow nothing but its own contract (不联动). */
export type LinkSetting = LinkColor | "none";
export const LINK_MODULES: readonly LinkModule[] = ["chart", "order", "watch", "sheet"];
export const LINK_COLORS: { id: LinkColor; label: string }[] = [{ id: "blue", label: "蓝" }, { id: "purple", label: "紫" }, { id: "orange", label: "橙" }];
/** A chart block's own contract, period and 副图 (absent: the app's own; 副图 default to the trade chart's). */
export interface ChartChoice { symbol?: string; duration?: number; panes?: SubPaneType[] }
const PANE_TYPES: readonly SubPaneType[] = ["kd", "macd", "rsi", "oi", "cci", "volume"];
const cleanPanes = (panes: unknown): SubPaneType[] | undefined => Array.isArray(panes)
  ? [...new Set(panes.filter((pane): pane is SubPaneType => PANE_TYPES.includes(pane as SubPaneType)))].slice(0, SUB_PANES_MAX) : undefined;
export const CHART_DURATIONS = [60, 180, 300, 900, 1800, 3600, 86400, 604800] as const;
export interface Board { id: string; name: string; layout: BoardNode | null; locks: Partial<Record<BoardBlock, BoardLock>>;
  /** 联动色 per block; a block not named here is blue, except a further chart, which follows its own contract. */
  links: Partial<Record<BoardBlock, LinkSetting>>;
  charts: Partial<Record<BoardBlock, ChartChoice>> }
export interface Boards { current: string; items: Board[] }
export type BoardSide = "left" | "right" | "top" | "bottom";

export const SIZE_LABELS: Record<BoardSize, string> = { small: "小", medium: "中", large: "大", xlarge: "加大", bar: "一条" };
export const MODULES: { id: BoardModule; label: string; sizes: BoardSize[]; many?: boolean }[] = [
  { id: "chart", label: "图表", sizes: ["small", "medium", "large", "xlarge"], many: true },
  { id: "positions", label: "持仓", sizes: ["small", "medium", "large"] },
  { id: "order", label: "下单", sizes: ["small", "medium", "large"] },
  { id: "watch", label: "自选报价", sizes: ["small", "medium", "large"] },
  { id: "sheet", label: "作战表", sizes: ["small", "medium", "large"] },
  // 提醒: no large size (the design's 砍: the list is short; 新建 and editing open the 提醒 panel).
  { id: "alert", label: "提醒", sizes: ["small", "medium"] },
  // 对话: 一条 when the block is a thin strip.
  { id: "chat", label: "对话", sizes: ["bar", "medium", "large"] },
  // 模块补全 (owner 2026-11-06): every surface can sit on a board.
  { id: "account", label: "账户权益", sizes: ["small", "medium", "large"] },
  { id: "orders", label: "委托与成交", sizes: ["small", "medium", "large"] },
  { id: "strategies", label: "运行中的策略", sizes: ["small", "medium", "large"] },
  { id: "flash", label: "快讯", sizes: ["small", "medium", "large"] },
  { id: "pnl", label: "复盘盈亏", sizes: ["small", "medium", "large"] },
  // 所有者 11-06 再加三样: 多 AI 辩论、交易笔记、AI 简报订阅.
  { id: "debate", label: "多 AI 辩论", sizes: ["small", "medium", "large"] },
  { id: "notes", label: "交易笔记", sizes: ["small", "medium", "large"] },
  { id: "briefs", label: "AI 简报", sizes: ["small", "medium", "large"] },
];
const MODULE_IDS = new Set(MODULES.map(item => item.id));
const CHART_BLOCK = /^chart-[2-9]$/;
export const isBlock = (block: unknown): block is BoardBlock => typeof block === "string" && (MODULE_IDS.has(block as BoardModule) || CHART_BLOCK.test(block));
/** The module a block shows. */
export const moduleOf = (block: BoardBlock): BoardModule => (CHART_BLOCK.test(block) ? "chart" : block) as BoardModule;
export const moduleInfo = (module: BoardModule) => MODULES.find(item => item.id === module)!;
/** How a block is called on screen: 图表 2 for a further chart. */
export const blockLabel = (block: BoardBlock) => CHART_BLOCK.test(block) ? `图表 ${block.slice(6)}` : moduleInfo(moduleOf(block))?.label ?? block;
export const isLinkModule = (module: BoardModule): module is LinkModule => (LINK_MODULES as readonly string[]).includes(module);
export const isLinkBlock = (block: BoardBlock) => isLinkModule(moduleOf(block));
export const linkOf = (board: Board, block: BoardBlock): LinkSetting => board.links[block] ?? (CHART_BLOCK.test(block) ? "none" : "blue");
/**
 * The colour that follows the app's own contract (the one 下单 trades, the chart tabs and the AI follow): 下单's, else
 * blue. Every other colour follows a contract of its own on this board.
 */
export const mainLink = (board: Board): LinkColor => {
  const order = leavesOf(board.layout).includes("order") ? linkOf(board, "order") : "blue";
  return order === "none" ? "blue" : order;
};
/** An AI arrangement on screen before 用这套 (never saved until then). */
export interface BoardPreview { layout: BoardNode; locks: Partial<Record<BoardBlock, BoardLock>>; charts: Partial<Record<BoardBlock, ChartChoice>>; note: string }
/** The block a new chart gets: `chart` while there is none, then the first free `chart-n`. */
export function nextChartBlock(layout: BoardNode | null): BoardBlock | null {
  const taken = new Set(leavesOf(layout));
  if (!taken.has("chart")) return "chart";
  for (let n = 2; n <= CHARTS_MAX; n += 1) if (!taken.has(`chart-${n}`)) return `chart-${n}`;
  return null;
}

/**
 * 由大往小试 (owner 2026-11-06, after a wide 780 × 200 chart came out as a line): every size of a module says the least
 * room it needs, at 100 % type (growing half as fast as the font). A block takes the fullest size its room holds; the
 * smallest is what is left. A lock caps it at 小 or 中. The chart draws its K 线 (with as many 副图 as fit) from 160 × 120
 * — narrow or flat, it shows fewer bars, never a line (owner 2026-11-06); only a really small block gets the line. A `small` entry is that size's own least room, where it is more than every
 * block's minimum (下单's buttons; see blockMin).
 */
export const SIZE_NEEDS: Record<BoardModule, Partial<Record<BoardSize, { w: number; h: number }>>> = {
  chart: { xlarge: { w: 900, h: 480 }, large: { w: 480, h: 300 }, medium: { w: 160, h: 120 } },
  positions: { large: { w: 560, h: 300 }, medium: { w: 340, h: 150 } },
  order: { large: { w: 420, h: 420 }, medium: { w: 460, h: 200 }, small: { w: 200, h: 128 } },
  watch: { large: { w: 520, h: 320 }, medium: { w: 340, h: 150 } },
  sheet: { large: { w: 520, h: 360 }, medium: { w: 340, h: 170 } },
  alert: { medium: { w: 300, h: 150 } },
  // 对话 · 中 reads in any width a block can have (bubbles wrap): height alone decides it, or a narrow, tall block showed
  // only the input box in a blank column (retest 1.9.1, 11-07).
  chat: { large: { w: 480, h: 360 }, medium: { w: 160, h: 150 } },
  account: { large: { w: 480, h: 200 }, medium: { w: 300, h: 120 } },
  orders: { large: { w: 600, h: 300 }, medium: { w: 400, h: 180 } },
  strategies: { large: { w: 420, h: 400 }, medium: { w: 300, h: 150 } },
  flash: { large: { w: 520, h: 400 }, medium: { w: 380, h: 240 } },
  pnl: { large: { w: 520, h: 400 }, medium: { w: 360, h: 300 } },
  debate: { large: { w: 480, h: 400 }, medium: { w: 340, h: 200 } },
  notes: { large: { w: 460, h: 360 }, medium: { w: 300, h: 150 } },
  briefs: { large: { w: 420, h: 260 }, medium: { w: 300, h: 150 } },
};
const LADDER: BoardSize[] = ["xlarge", "large", "medium"];

/** The size a block of `w` × `h` CSS px shows: the fullest whose needs fit, else the module's smallest (对话: 一条). */
export function pickSize(module: BoardModule, w: number, h: number, fontScale: number, lock?: BoardLock): BoardSize {
  const g = 1 + (fontScale - 1) / 2;
  const smallest: BoardSize = module === "chat" ? "bar" : "small";
  if (lock === "small") return smallest;
  const needs = SIZE_NEEDS[module];
  const tried = lock === "medium" ? ["medium" as const] : LADDER;
  const fits = tried.find(size => { const need = needs[size]; return need && w >= need.w * g && h >= need.h * g; });
  return fits ?? smallest;
}

/**
 * 每块的最小地方 (owner 2026-11-06): a block never gets less room than a title and a line of its content — this wide and
 * tall at 100 % type (growing half as fast as the font, like the size thresholds), the 8 px between blocks included;
 * what reads longer scrolls inside it. A module whose smallest size has buttons (下单) declares that size's need in
 * SIZE_NEEDS and gets that instead — it does not scroll. Dragging a divider stops there (react-mosaic's
 * minimumPaneSizePx) and every layout shown — a place, an AI arrangement, an old board — is fitted to it first.
 */
export const BLOCK_MIN = { w: 168, h: 104 };
export type BlockMinimum = { w: number; h: number; of: (block: BoardBlock) => { w: number; h: number } };
export const blockMin = (fontScale: number): BlockMinimum => {
  const g = 1 + (fontScale - 1) / 2;
  const scaled = (min: { w: number; h: number }) => ({ w: Math.round(min.w * g) + 8, h: Math.round(min.h * g) + 8 });
  const base = scaled(BLOCK_MIN);
  const own = Object.fromEntries(Object.entries(SIZE_NEEDS).flatMap(([module, needs]) => needs.small
    ? [[module, scaled({ w: Math.max(BLOCK_MIN.w, needs.small.w), h: Math.max(BLOCK_MIN.h, needs.small.h) })]] : []));
  return { ...base, of: block => own[moduleOf(block)] ?? base };
};

/** The room a node needs: a block its minimum, a split its children's side by side (or stacked). */
export function needOf(node: BoardNode | null, min: BlockMinimum): { w: number; h: number } {
  if (!node) return { w: 0, h: 0 };
  if (!isSplitNode(node)) return min.of(node as BoardBlock);
  const needs = node.children.map(child => needOf(child, min));
  return node.direction === "row"
    ? { w: needs.reduce((sum, need) => sum + need.w, 0), h: Math.max(...needs.map(need => need.h)) }
    : { w: Math.max(...needs.map(need => need.w)), h: needs.reduce((sum, need) => sum + need.h, 0) };
}

/**
 * The layout as a `w` × `h` canvas can show it: each split's shares raised where a child would get less than it needs,
 * the room taken from the children that have more than they need (in proportion to what they can spare). When even the
 * minimums do not fit, each child gets room in proportion to its need. The stored proportions are untouched otherwise.
 */
export function fitLayout(layout: BoardNode | null, w: number, h: number, min: BlockMinimum): BoardNode | null {
  if (!layout || !isSplitNode(layout) || w <= 0 || h <= 0) return layout;
  const along = layout.direction === "row" ? w : h;
  const count = layout.children.length;
  const shares = layout.splitPercentages ?? layout.children.map(() => 100 / count);
  const needs = layout.children.map(child => { const need = needOf(child, min); return (layout.direction === "row" ? need.w : need.h) / along * 100; });
  const total = needs.reduce((sum, need) => sum + need, 0);
  let fitted = shares;
  if (total >= 100) fitted = needs.map(need => need / total * 100);
  else if (shares.some((share, index) => share < needs[index])) {
    const deficit = shares.reduce((sum, share, index) => sum + Math.max(0, needs[index] - share), 0);
    const spare = shares.reduce((sum, share, index) => sum + Math.max(0, share - needs[index]), 0);
    fitted = shares.map((share, index) => share < needs[index] ? needs[index] : share - (share - needs[index]) / spare * deficit);
  }
  const children = layout.children.map((child, index) => layout.direction === "row"
    ? fitLayout(child, w * fitted[index] / 100, h, min)! : fitLayout(child, w, h * fitted[index] / 100, min)!);
  return { ...layout, children, splitPercentages: fitted };
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
  { id: "pre", name: "盘前", layout: DEFAULT_LAYOUTS.pre, locks: {}, links: {}, charts: {} },
  { id: "live", name: "盘中", layout: DEFAULT_LAYOUTS.live, locks: {}, links: {}, charts: {} },
  { id: "review", name: "复盘", layout: DEFAULT_LAYOUTS.review, locks: {}, links: {}, charts: {} },
] };

/** A stored node as the screen can draw it: known blocks once each, splits of two or more, percentages summing to 100. */
function cleanNode(raw: unknown, seen: Set<BoardBlock>): BoardNode | null {
  if (typeof raw === "string") {
    if (!isBlock(raw) || seen.has(raw)) return null;
    seen.add(raw);
    return raw;
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
export const cleanLayout = (raw: unknown): BoardNode | null => cleanNode(raw, new Set());

const SYMBOL = /^[A-Za-z0-9_.@-]{3,40}$/;
/** The per-block settings of the blocks on the layout, as the screen can use them. */
function cleanSettings(layout: BoardNode | null, raw: { locks?: unknown; links?: unknown; charts?: unknown }): Pick<Board, "locks" | "links" | "charts"> {
  const blocks = new Set(leavesOf(layout));
  const entries = (value: unknown) => Object.entries(value && typeof value === "object" ? value as Record<string, unknown> : {}).filter(([block]) => blocks.has(block));
  const locks: Board["locks"] = {};
  entries(raw.locks).forEach(([block, lock]) => { if (lock === "small" || lock === "medium") locks[block] = lock; });
  const links: Board["links"] = {};
  entries(raw.links).forEach(([block, color]) => {
    if (isLinkBlock(block) && (LINK_COLORS.some(option => option.id === color) || (color === "none" && moduleOf(block) === "chart"))) links[block] = color as LinkSetting;
  });
  const charts: Board["charts"] = {};
  entries(raw.charts).forEach(([block, choice]) => {
    if (moduleOf(block) !== "chart" || !choice || typeof choice !== "object") return;
    const { symbol, duration, panes } = choice as ChartChoice;
    const clean: ChartChoice = {};
    if (typeof symbol === "string" && SYMBOL.test(symbol)) clean.symbol = symbol;
    if ((CHART_DURATIONS as readonly number[]).includes(duration as number)) clean.duration = duration;
    const ownPanes = cleanPanes(panes);
    if (ownPanes) clean.panes = ownPanes;
    if (clean.symbol || clean.duration || clean.panes) charts[block] = clean;
  });
  return { locks, links, charts };
}

/**
 * A stored document as the screen can show it. A board from the grid version (2026-11-05 morning, `tiles` / `cols` /
 * `rows`, only test accounts) reads as the default arrangement of its id (a user board: empty).
 */
export function normalizeBoards(raw: Partial<Boards> | undefined | null): Boards {
  const items = (Array.isArray(raw?.items) ? raw.items : []).flatMap(item => {
    if (!item || typeof item.id !== "string" || typeof item.name !== "string") return [];
    const legacy = !("layout" in item) || "tiles" in item;
    const layout = legacy ? defaultLayout(item.id) : cleanLayout(item.layout);
    return [{ id: item.id, name: item.name, layout, ...cleanSettings(layout, item) }];
  }).slice(0, BOARDS_MAX);
  if (!items.length) return DEFAULT_BOARDS;
  return { current: items.some(item => item.id === raw?.current) ? raw!.current! : items[0].id, items };
}

export const leavesOf = (layout: BoardNode | null): BoardBlock[] => layout === null ? [] : getLeaves(layout);
export function pathOf(layout: BoardNode | null, block: BoardBlock, path: MosaicPath = []): MosaicPath | null {
  if (layout === null) return null;
  if (layout === block) return path;
  if (isSplitNode(layout)) {
    for (let index = 0; index < layout.children.length; index += 1) {
      const found = pathOf(layout.children[index], block, [...path, index]);
      if (found) return found;
    }
  }
  return null;
}

/** Split the node at `path` so `block` takes the `side` part with `share` % (the rest stays the node). */
export function insertAt(layout: BoardNode | null, path: MosaicPath, side: BoardSide, block: BoardBlock, share = 50): BoardNode {
  if (layout === null) return block;
  const node = getNodeAtPath(layout, path);
  if (node === null) throw new Error("那一块已经不在看板上了");
  const first = side === "left" || side === "top";
  const s = Math.min(90, Math.max(10, share));
  const next = split(side === "left" || side === "right" ? "row" : "column", first ? [block, node] : [node, block], first ? [s, 100 - s] : [100 - s, s]);
  return path.length ? updateTree(layout, [{ path, spec: { $set: next } }]) : next;
}

export function removeLeaf(layout: BoardNode | null, block: BoardBlock): BoardNode | null {
  const path = pathOf(layout, block);
  if (layout === null || path === null) return layout;
  return path.length ? updateTree(layout, [createRemoveUpdate(layout, path)]) : null;
}

/** The share (%) of `block` in the split that holds it, and that split's direction. */
export function shareOf(layout: BoardNode | null, block: BoardBlock): { share: number; direction: "row" | "column" } | null {
  const path = pathOf(layout, block);
  if (layout === null || !path?.length) return null;
  const parent = getNodeAtPath(layout, getParentPath(path));
  if (!parent || !isSplitNode(parent)) return null;
  const index = path[path.length - 1];
  return { share: parent.splitPercentages?.[index] ?? 100 / parent.children.length, direction: parent.direction };
}

export interface BoardsChange {
  select?: string; create?: string; rename?: { id: string; name: string }; remove?: string;
  /** Put a module beside the block `target` on its `side` taking `ratio` (0.1–0.9) of it; without a target beside the
   *  whole board (a third). A block already on the board (`block`, else the module's own) moves there; 图表 without
   *  `block` adds another chart once one is there. A chart may get its contract and period at once. */
  place?: { module: BoardModule; block?: BoardBlock; target?: BoardBlock; side?: BoardSide; ratio?: number; symbol?: string; duration?: number };
  /** 换成别的模块: the block `module` shows `with` instead. */
  replace?: { module: BoardBlock; with: BoardModule };
  /** Take a block off; its neighbours fill the room. */
  take?: BoardBlock;
  /** 拖分隔线: the block gets `value` (0.05–0.95) of the split holding it, the others share the rest. */
  ratio?: { module: BoardBlock; value: number };
  /** 档位: auto (follow the room) or lock to 小 / 中. */
  lock?: { module: BoardBlock; size: "auto" | BoardLock };
  /** A whole new layout (a finished drag, resize or drop): what react-mosaic handed back. */
  layout?: BoardNode | null;
  /** 联动色: the block follows the contract of that colour (a chart also 不联动, its own contract). */
  link?: { module: BoardBlock; color: LinkSetting };
  /** A chart block's own contract, period and 副图 (null: back to the app's own / the trade chart's 副图). */
  chart?: { block: BoardBlock; symbol?: string | null; duration?: number | null; panes?: SubPaneType[] | null };
  /** 用这套: an AI arrangement's layout, locks and charts in one step. */
  arrange?: { layout: BoardNode; locks: Partial<Record<BoardBlock, BoardLock>>; charts?: Partial<Record<BoardBlock, ChartChoice>> };
}

const KEYS = ["select", "create", "rename", "remove", "place", "replace", "take", "ratio", "lock", "layout", "link", "chart", "arrange"] as const;

/** What one change does to the list (pure; throws a readable refusal). */
export function planBoardsChange(change: BoardsChange, boards: Boards): Boards {
  const keys = KEYS.filter(key => change[key] !== undefined);
  if (keys.length !== 1) throw new Error("boards 一次只做一件：select / create / rename / remove / place / replace / take / ratio / lock / link / chart");
  const named = (name: string) => {
    const clean = name.trim();
    if (!clean || clean.length > NAME_MAX) throw new Error(`看板名 1–${NAME_MAX} 个字`);
    return clean;
  };
  const current = boards.items.find(item => item.id === boards.current) ?? boards.items[0];
  // Settings follow the blocks: what a removed block had goes with it.
  const withCurrent = (patch: Partial<Board>) => ({ ...boards, items: boards.items.map(item => {
    if (item.id !== current.id) return item;
    const next = { ...item, ...patch };
    return { ...next, ...cleanSettings(next.layout, next) };
  }) });
  const known = (module: BoardModule) => { if (!MODULE_IDS.has(module)) throw new Error(`没有“${module}”这个模块`); };
  const onBoard = (block: BoardBlock) => { if (!leavesOf(current.layout).includes(block)) throw new Error(`这个看板上没有${isBlock(block) ? blockLabel(block) : `“${block}”`}`); };
  const chartChoice = (block: BoardBlock, symbol?: string | null, duration?: number | null, panes?: SubPaneType[] | null) => {
    const old = current.charts[block] ?? {};
    const next: ChartChoice = { ...old };
    if (symbol !== undefined) { if (symbol === null) delete next.symbol; else if (SYMBOL.test(symbol)) next.symbol = symbol; else throw new Error("合约代码不对，比如 SHFE.rb2601"); }
    if (duration !== undefined) {
      if (duration === null) delete next.duration;
      else if ((CHART_DURATIONS as readonly number[]).includes(duration)) next.duration = duration;
      else throw new Error("周期是 60 / 180 / 300 / 900 / 1800 / 3600 / 86400 / 604800 秒");
    }
    if (panes !== undefined) {
      if (panes === null) delete next.panes;
      else { const clean = cleanPanes(panes); if (!clean || clean.length !== new Set(panes).size) throw new Error(`副图最多 ${SUB_PANES_MAX} 个，可选 kd / macd / rsi / oi / cci / volume`); next.panes = clean; }
    }
    const charts = { ...current.charts };
    if (next.symbol || next.duration || next.panes) charts[block] = next; else delete charts[block];
    return charts;
  };
  // Picking a chart's own contract makes it 不联动 unless it was given a colour on purpose.
  const ownLink = (block: BoardBlock, symbol?: string | null) => symbol && current.links[block] === undefined ? { ...current.links, [block]: "none" as const } : current.links;
  if (change.select !== undefined) {
    if (!boards.items.some(item => item.id === change.select)) throw new Error("没有这个看板");
    return { ...boards, current: change.select };
  }
  if (change.create !== undefined) {
    const name = named(change.create);
    if (boards.items.length >= BOARDS_MAX) throw new Error(`最多存 ${BOARDS_MAX} 个看板，先删一个`);
    if (boards.items.some(item => item.name === name)) throw new Error(`已经有叫“${name}”的看板`);
    const id = `b${Date.now().toString(36)}`;
    return { current: id, items: [...boards.items, { id, name, layout: null, locks: {}, links: {}, charts: {} }] };
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
  if (change.layout !== undefined) return withCurrent({ layout: change.layout === null ? null : cleanLayout(change.layout) });
  if (change.arrange !== undefined) {
    const layout = cleanLayout(change.arrange.layout);
    return withCurrent({ layout, locks: change.arrange.locks, links: {}, charts: change.arrange.charts ?? {} });
  }
  if (change.link !== undefined) {
    const { module: block, color } = change.link;
    onBoard(block);
    if (!isLinkBlock(block)) throw new Error(`${blockLabel(block)}不跟单个合约，没有联动色`);
    if (!(LINK_COLORS.some(option => option.id === color) || (color === "none" && moduleOf(block) === "chart"))) {
      throw new Error(moduleOf(block) === "chart" ? "联动色是 blue / purple / orange，或 none 不联动" : "联动色是 blue / purple / orange");
    }
    return withCurrent({ links: { ...current.links, [block]: color } });
  }
  if (change.chart !== undefined) {
    const { block, symbol, duration, panes } = change.chart;
    onBoard(block);
    if (moduleOf(block) !== "chart") throw new Error(`${blockLabel(block)}不是图表`);
    const links = symbol && linkOf(current, block) !== "none" ? { ...current.links, [block]: "none" as const } : current.links;
    return withCurrent({ charts: chartChoice(block, symbol, duration, panes), links });
  }
  if (change.take !== undefined) {
    onBoard(change.take);
    return withCurrent({ layout: removeLeaf(current.layout, change.take) });
  }
  if (change.replace !== undefined) {
    const { module: block, with: next } = change.replace;
    known(next);
    onBoard(block);
    if (moduleOf(block) === next) return boards;
    const nextBlock = next === "chart" ? nextChartBlock(current.layout) : next;
    if (!nextBlock) throw new Error(`一个看板最多放 ${CHARTS_MAX} 块图表`);
    if (leavesOf(current.layout).includes(nextBlock)) throw new Error(`${moduleInfo(next).label}已经在这个看板上了，拿掉它或换成别的`);
    const path = pathOf(current.layout, block)!;
    const layout = path.length ? updateTree(current.layout!, [{ path, spec: { $set: nextBlock } }]) : nextBlock;
    return withCurrent({ layout });
  }
  if (change.ratio !== undefined) {
    const { module: block, value } = change.ratio;
    onBoard(block);
    const path = pathOf(current.layout, block)!;
    if (!path.length) throw new Error(`${blockLabel(block)}占着整块看板，没有可调的分隔线`);
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
    const { module: block, size } = change.lock;
    onBoard(block);
    const locks = { ...current.locks };
    if (size === "auto") delete locks[block]; else locks[block] = size;
    return withCurrent({ locks });
  }
  const place = change.place!;
  known(place.module);
  const placed = leavesOf(current.layout);
  let block: BoardBlock | null = place.block ?? place.module;
  if (place.block !== undefined && (!isBlock(place.block) || moduleOf(place.block) !== place.module)) throw new Error(`“${place.block}”不是${moduleInfo(place.module).label}的一块`);
  if (place.module === "chart" && place.block === undefined && placed.includes("chart")) block = nextChartBlock(current.layout);
  if (!block) throw new Error(`一个看板最多放 ${CHARTS_MAX} 块图表`);
  if ((place.symbol !== undefined || place.duration !== undefined) && place.module !== "chart") throw new Error("只有图表能设合约和周期");
  if (place.target === block) throw new Error("不能放在它自己旁边");
  const ratio = place.ratio ?? (place.target ? 0.5 : 0.33);
  if (ratio < 0.1 || ratio > 0.9) throw new Error("ratio 在 0.1 到 0.9 之间");
  const without = removeLeaf(current.layout, block);
  const charts = place.module === "chart" && (place.symbol !== undefined || place.duration !== undefined) ? chartChoice(block, place.symbol, place.duration) : current.charts;
  if (place.target !== undefined) {
    const path = pathOf(without, place.target);
    if (path === null) throw new Error(`这个看板上没有${isBlock(place.target) ? blockLabel(place.target) : `“${place.target}”`}，先放它，或放在别的模块旁边`);
    return withCurrent({ layout: insertAt(without, path, place.side ?? "right", block, ratio * 100), charts, links: ownLink(block, place.symbol) });
  }
  return withCurrent({ layout: insertAt(without, [], place.side ?? "right", block, ratio * 100), charts, links: ownLink(block, place.symbol) });
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
