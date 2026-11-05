import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MutableRefObject, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { Check, ChevronDown, GripVertical, LayoutDashboard, MoreHorizontal, Pencil, Plus, Sparkles, Trash2, Undo2, X } from "lucide-react";
import { DndProvider, useDrag } from "react-dnd";
import { HTML5Backend } from "react-dnd-html5-backend";
import { MosaicDragType, MosaicWindow, MosaicWithoutDragDropContext, type MosaicDropData, type MosaicPath } from "react-mosaic-component";
import "react-mosaic-component/react-mosaic-component.css";
import { useDisclosureSurface, useUiSurface } from "../../uiSurfaces";
import { HelpTip } from "../Blocks";
import { Presence } from "../motion";
import { arrangeBoard } from "../../api";
import { CHART_DURATIONS, LINK_COLORS, MODULES, SIZE_LABELS, blockLabel, insertAt, isLinkBlock, leavesOf, linkOf, mainLink, moduleInfo, moduleOf, nextChartBlock,
  normalizeBoards, pickSize, removeLeaf, shareOf, type Board, type BoardBlock, type BoardLock, type BoardModule, type BoardNode, type BoardPreview, type BoardSize,
  type BoardsChange, type BoardsHandle, type LinkColor, type LinkSetting } from "./boards";
import { durationLabel } from "../mobile/mobileMarketPrefs";
import "./BoardsView.css";

/** External drags (the 模块库) must name the Mosaic they drop into. */
const MOSAIC_ID = "sente-board";
const MODULE_ICONS: Record<BoardModule, string> = {
  chart: "M4 19V5M4 19h16M8 15l3-4 3 2 4-6",
  positions: "M4 7h16v12H4zM9 7V5h6v2M4 12h16",
  order: "M5 8h14M5 16h14M8 5L5 8l3 3M16 13l3 3-3 3",
  watch: "M12 4l2.4 5 5.6.8-4 3.9 1 5.5-5-2.7-5 2.7 1-5.5-4-3.9 5.6-.8z",
  sheet: "M5 4h14v16H5zM8 9h8M8 13h8M8 17h5",
  alert: "M6 16v-5a6 6 0 0 1 12 0v5l2 2H4zM10 21h4",
  chat: "M4 5h16v11H9l-5 4z",
};
const ModuleIcon = ({ module }: { module: BoardModule }) => <svg viewBox="0 0 24 24" aria-hidden="true"><path d={MODULE_ICONS[module]} /></svg>;

type TileReport = { block: BoardBlock; size: BoardSize; locked: boolean } | { block: BoardBlock; gone: true };
/** A raw arrangement (the AI's, or a `boards: {preview}` command) as the screen can show it; null when nothing is left. */
function previewOf(raw: { layout: unknown; locks?: unknown; charts?: unknown }, note: string): BoardPreview | null {
  const board = normalizeBoards({ current: "x", items: [{ id: "x", name: "x", layout: raw.layout as BoardNode, locks: (raw.locks ?? {}) as Board["locks"], links: {},
    charts: (raw.charts ?? {}) as Board["charts"] }] }).items[0];
  return board.layout ? { layout: board.layout, locks: board.locks, charts: board.charts, note: note.slice(0, 60) } : null;
}
export interface BoardsControl {
  handle: BoardsHandle;
  editing: boolean;
  setEditing: (editing: boolean) => void;
  /** The 模块库 card (only while editing). */
  library: boolean;
  setLibrary: (open: boolean) => void;
  /** Put the 看板 on screen (the view). */
  show: () => void;
  /** A refusal shown as one line above the canvas for a few seconds (never over a control). */
  notice: string;
  notify: (message: string) => void;
  /** The size each block on screen picked (from its room, or its lock). */
  sizes: Partial<Record<BoardBlock, BoardSize>>;
  reportTile: (tile: TileReport) => void;
  /** 色点联动: the contract a colour other than the main one follows on this board (undefined: the app's own). */
  linkSymbol: (color: LinkColor) => string | undefined;
  followLink: (color: LinkColor, symbol: string) => void;
  /** 让 AI 帮我摆: the arrangement on screen before 用这套, the request card and whether the AI is still arranging. */
  preview: BoardPreview | null;
  adopt: () => void;
  discard: () => void;
  asking: boolean;
  setAsking: (open: boolean) => void;
  arranging: boolean;
  ask: (sentence: string) => Promise<void>;
  /** The canvas's measured size, which the AI arranges for. */
  canvas: MutableRefObject<{ w: number; h: number }>;
}

/**
 * The `boards` UI command and the snapshot's `state.boards` (editing, the 模块库, each block's size and share); the list
 * itself is the account preference ui.boards. Lives with the workspace so a command lands while another view is on screen.
 */
export function useBoardsControl(handle: BoardsHandle, show: () => void, available: boolean, baseUrl: string | undefined, fontScale: number,
  /** The main contracts the AI may give a chart (it answers with one of these symbols). */
  contracts: readonly { symbol: string; name: string }[]): BoardsControl {
  const [editing, setEditingState] = useState(false);
  const [links, setLinks] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<BoardPreview | null>(null);
  const [asking, setAskingState] = useState(false);
  const [arranging, setArranging] = useState(false);
  const canvas = useRef({ w: 0, h: 0 });
  const [library, setLibraryState] = useState(false);
  const [sizes, setSizes] = useState<Partial<Record<BoardBlock, BoardSize>>>({});
  const { resetUndo } = handle;
  const setEditing = useCallback((value: boolean) => {
    setEditingState(value);
    setLibraryState(false);
    if (value) resetUndo();
  }, [resetUndo]);
  const setLibrary = useCallback((open: boolean) => setLibraryState(open), []);
  const [notice, setNotice] = useState("");
  const notify = useCallback((message: string) => setNotice(message), []);
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(""), 5000); return () => clearTimeout(timer); }, [notice]);
  const reportTile = useCallback((tile: TileReport) => setSizes(current => {
    if ("gone" in tile) { if (!current[tile.block]) return current; const next = { ...current }; delete next[tile.block]; return next; }
    return current[tile.block] === tile.size ? current : { ...current, [tile.block]: tile.size };
  }), []);
  const boardId = handle.board.id;
  const linkSymbol = useCallback((color: LinkColor) => links[`${boardId}:${color}`], [links, boardId]);
  const followLink = useCallback((color: LinkColor, symbol: string) => setLinks(current => ({ ...current, [`${boardId}:${color}`]: symbol })), [boardId]);
  // A preview belongs to the board it was made for; switching boards or leaving editing drops it.
  useEffect(() => { setPreview(null); setAskingState(false); }, [boardId, editing]);
  const { apply } = handle;
  const adopt = useCallback(() => {
    if (!preview) throw new Error("没有 AI 摆好的方案");
    apply({ arrange: { layout: preview.layout, locks: preview.locks, charts: preview.charts } });
    setPreview(null);
  }, [apply, preview]);
  const discard = useCallback(() => setPreview(null), []);
  const setAsking = useCallback((open: boolean) => setAskingState(open), []);
  const ask = useCallback(async (sentence: string) => {
    const text = sentence.trim();
    if (!text) throw new Error("先说一句想怎么摆，比如“摆一套盘中，主看螺纹”");
    if (!baseUrl) throw new Error("连接后可让 AI 帮你摆");
    setArranging(true);
    try {
      const result = await arrangeBoard(baseUrl, { ask: text.slice(0, 200), current: handle.board.layout,
        width: Math.round(canvas.current.w), height: Math.round(canvas.current.h), fontScale, contracts: contractList.current.slice(0, 120) });
      const next = previewOf(result, String(result.note ?? ""));
      if (!next) throw new Error("AI 这次没摆出能用的方案，换个说法再试");
      setPreview(next);
      setAskingState(false);
    } catch (error) {
      const detail = (error as { detail?: { detail?: unknown } }).detail?.detail;
      throw new Error(typeof detail === "string" ? detail : error instanceof Error && !/^\d{3} /.test(error.message) ? error.message : "AI 这次没摆好，稍后再试",
        { cause: error });
    } finally {
      setArranging(false);
    }
  }, [baseUrl, fontScale, handle.board.layout]);
  const contractList = useRef(contracts);
  contractList.current = contracts;
  const latest = useRef({ handle, show, setEditing, adopt, discard, ask, followLink });
  latest.current = { handle, show, setEditing, adopt, discard, ask, followLink };
  const { layout, locks, charts } = handle.board;
  const main = mainLink(handle.board);
  const tiles = leavesOf(layout).map(block => ({ block, module: moduleOf(block), size: sizes[block] ?? null, locked: locks[block] ?? null,
    share: shareOf(layout, block) === null ? 100 : Math.round(shareOf(layout, block)!.share),
    link: isLinkBlock(block) ? linkOf(handle.board, block) : null,
    ...(moduleOf(block) === "chart" ? { symbol: charts[block]?.symbol ?? null, duration: charts[block]?.duration ?? null } : {}) }));
  const groups = Object.fromEntries(LINK_COLORS.flatMap(({ id }) => id !== main && linkSymbol(id) ? [[id, linkSymbol(id)!]] : []));
  const shownPreview = preview ? { note: preview.note, blocks: leavesOf(preview.layout) } : null;
  useUiSurface("boards", { state: { editing, library: editing && library, tiles, main, groups, preview: shownPreview, arranging }, apply: value => {
    const change = value as BoardsChange & { editing?: boolean; undo?: boolean; linkSymbol?: { color: LinkColor; symbol: string };
      preview?: { layout: BoardNode; locks?: BoardPreview["locks"]; charts?: BoardPreview["charts"]; note?: string }; adopt?: boolean; discard?: boolean; ask?: string };
    const { handle: boards, show: open, setEditing: edit } = latest.current;
    if (change.linkSymbol !== undefined) {
      const { color, symbol } = change.linkSymbol;
      if (color === mainLink(boards.board)) throw new Error("这个颜色跟着顶栏的当前合约，用 symbol 切换");
      latest.current.followLink(color, symbol);
      return;
    }
    if (change.preview !== undefined) {
      if (!editing) throw new Error("先进入编辑（boards: {editing: true}）再给出方案");
      const next = previewOf(change.preview, change.preview.note ?? "");
      if (!next) throw new Error("这个方案里没有能放的模块");
      setPreview(next);
      return;
    }
    if (change.adopt) { latest.current.adopt(); return; }
    if (change.discard) { latest.current.discard(); return; }
    if (change.ask !== undefined) {
      if (!editing) throw new Error("先进入编辑（boards: {editing: true}）");
      void latest.current.ask(change.ask).catch(error => setNotice(error instanceof Error ? error.message : "AI 这次没摆好"));
      return;
    }
    if (change.undo !== undefined) {
      if (!boards.undo) throw new Error("没有可撤销的改动（只撤销这次编辑里的放、换、拿掉、调比例、锁档）");
      boards.undo();
      return;
    }
    if (change.editing !== undefined) {
      if (Object.keys(change).length !== 1) throw new Error("editing 单独发");
      edit(change.editing);
      if (change.editing) open();
      return;
    }
    boards.apply(change);
    if (change.select !== undefined || change.create !== undefined) open();
  } }, [editing, library, JSON.stringify(tiles), main, JSON.stringify(groups), JSON.stringify(shownPreview), arranging], available);
  return { handle, editing, setEditing, library, setLibrary, show, notice, notify, sizes, reportTile, linkSymbol, followLink,
    preview, adopt, discard, asking, setAsking, arranging, ask, canvas };
}

/**
 * 顶栏“看板” (owner 2026-11-05): one click shows the board used last; the arrow lists them all and 新建空白看板 and, on the
 * board, 编辑 / 撤销 / 模块库 / 改名 / 删除 — the board has no bar of its own. While editing, ＋ 模块 / 撤销 / 完成 sit beside it.
 */
export function BoardsMenu({ control, active, onError }: { control: BoardsControl; active: boolean; onError: (message: string) => void }) {
  const [open, setOpen] = useDisclosureSurface("boards-menu");
  const ref = useRef<HTMLSpanElement>(null);
  const { boards, board } = control.handle;
  const [name, setName] = useState(board.name);
  const [confirming, setConfirming] = useState(false);
  useEffect(() => { setName(board.name); setConfirming(false); }, [board.id, board.name, open]);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("pointerdown", close); window.removeEventListener("keydown", escape); };
  }, [open, setOpen]);
  const fail = (error: unknown) => {
    const message = error instanceof Error ? error.message : "看板暂时不能修改";
    if (active) control.notify(message); else onError(message);
  };
  const run = (change: BoardsChange, keepOpen = false) => {
    try { control.handle.apply(change); control.show(); if (!keepOpen) setOpen(false); return true; } catch (error) { fail(error); return false; }
  };
  const createBlank = () => {
    const used = new Set(boards.items.map(item => item.name));
    let n = 1;
    while (used.has(`看板 ${n}`)) n += 1;
    if (run({ create: `看板 ${n}` })) { control.setEditing(true); control.setLibrary(true); }
  };
  const editing = active && control.editing;
  return <span className={`board-entry${active ? " active" : ""}`} ref={ref}>
    <button type="button" className="fdw-nav-view board-entry-main" aria-label="看板" aria-pressed={active} onClick={() => { control.show(); setOpen(false); }}>
      <LayoutDashboard /><span>{active ? board.name : "看板"}</span>
    </button>
    <button type="button" className="board-entry-arrow" aria-label="选择看板" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}><ChevronDown /></button>
    {editing ? <span className="board-entry-tools">
      <button type="button" className="board-entry-tool" aria-pressed={control.asking} disabled={control.arranging} onClick={() => control.setAsking(!control.asking)}>
        <Sparkles /><span>{control.arranging ? "AI 在摆…" : "让 AI 帮我摆"}</span></button>
      <button type="button" className="board-entry-tool" aria-pressed={control.library} onClick={() => control.setLibrary(!control.library)}><Plus /><span>模块</span></button>
      <button type="button" className="board-entry-tool" aria-label="撤销" disabled={!control.handle.undo} onClick={() => control.handle.undo?.()}><Undo2 /></button>
      <button type="button" className="app-button primary board-entry-done" onClick={() => control.setEditing(false)}><Check /><span>完成</span></button>
    </span> : null}
    <Presence>{open ? <div className="fdw-tab-menu board-entry-menu" role="menu" aria-label="选择看板" data-agent-target="boards-menu">
      {boards.items.map(item => <div key={item.id} role="none">
        <button type="button" role="menuitem" aria-current={item.id === boards.current || undefined} onClick={() => run({ select: item.id })}>
          <span>{item.name}</span>{item.id === boards.current ? <small>{active ? "正在看" : "上次用的"}</small> : null}
        </button>
      </div>)}
      <div role="none"><button type="button" role="menuitem" className="fdw-tab-menu-add" onClick={createBlank}><Plus />新建空白看板</button></div>
      {active ? <>
        <i className="board-menu-divider" aria-hidden="true" />
        <div role="none"><button type="button" role="menuitem" onClick={() => { control.setEditing(!control.editing); setOpen(false); }}>
          {control.editing ? <><Check />完成编辑</> : <><Pencil />编辑“{board.name}”</>}</button></div>
        {editing ? <>
          <div role="none"><button type="button" role="menuitem" disabled={!control.handle.undo} onClick={() => { control.handle.undo?.(); setOpen(false); }}><Undo2 />撤销</button></div>
          <div role="none"><button type="button" role="menuitem" onClick={() => { control.setLibrary(true); setOpen(false); }}><Plus />模块库</button></div>
          <div role="none"><button type="button" role="menuitem" onClick={() => { control.setAsking(true); setOpen(false); }}><Sparkles />让 AI 帮我摆</button></div>
        </> : null}
        <form className="board-menu-rename" onSubmit={event => { event.preventDefault(); run({ rename: { id: board.id, name } }, true); }}>
          <label>名字<input value={name} maxLength={12} onChange={event => setName(event.target.value)} /></label>
          <button type="submit" className="app-button" disabled={!name.trim() || name.trim() === board.name}>改名</button>
        </form>
        <div role="none"><button type="button" role="menuitem" className="board-danger" disabled={boards.items.length === 1}
          title={boards.items.length === 1 ? "至少留一个看板" : undefined}
          onClick={() => confirming ? run({ remove: board.id }) : setConfirming(true)}>
          <Trash2 />{confirming ? `确认删除“${board.name}”` : "删除这套看板"}
        </button></div>
      </> : null}
    </div> : null}</Presence>
  </span>;
}

/** One block: measures the room it has and picks its size from it (or its lock). */
function TileBody({ block, lock, link, scale, editing, reportTile, renderTile }: { block: BoardBlock; lock?: BoardLock; scale: number; editing: boolean;
  /** 色点联动: shown when the board uses more than one colour (while editing the dot is in the name bar). */
  link?: LinkSetting;
  reportTile: (tile: TileReport) => void; renderTile: (block: BoardBlock, size: BoardSize) => ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const read = () => setBox(old => old.w === node.clientWidth && old.h === node.clientHeight ? old : { w: node.clientWidth, h: node.clientHeight });
    read();
    const observer = new ResizeObserver(read);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const size = box.w && box.h ? pickSize(moduleOf(block), box.w, box.h, scale, lock) : null;
  useEffect(() => { if (size) reportTile({ block, size, locked: Boolean(lock) }); }, [reportTile, block, size, lock]);
  useEffect(() => () => reportTile({ block, gone: true }), [reportTile, block]);
  return <div ref={ref} className={`board-tile size-${size ?? "pending"}`} data-agent-target={`board-tile:${block}`} data-state={size ?? undefined}
    role="group" aria-label={`${blockLabel(block)}${size ? ` · ${SIZE_LABELS[size]}` : ""}`}>
    <div className="board-tile-content" inert={editing || undefined}>{size ? renderTile(block, size) : null}</div>
    {link && link !== "none" ? <span className="board-link-dot" data-link={link} role="img" aria-label={`联动：${LINK_COLORS.find(item => item.id === link)?.label}`} /> : null}
  </div>;
}

/** The block's ⋯ menu: 换成别的模块 / 档位 / 联动色 / 再分一块 / 拿掉 (one at a time, a disclosure per block). */
function TileMenu({ block, size, run, layout, locked, link }: { block: BoardBlock; size: BoardSize | undefined; layout: BoardNode | null; locked?: BoardLock;
  link?: LinkSetting; run: (change: BoardsChange) => boolean }) {
  const [open, setOpen] = useDisclosureSurface("board-tile-menu", { id: block });
  const [split, setSplit] = useState<"right" | "bottom" | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const [up, setUp] = useState(false);
  useEffect(() => { if (!open) setSplit(null); }, [open]);
  // A block low on the screen opens its menu upwards.
  useLayoutEffect(() => { if (open && ref.current) setUp(ref.current.getBoundingClientRect().bottom + 320 > window.innerHeight); }, [open]);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [open, setOpen]);
  const label = blockLabel(block);
  const module = moduleOf(block);
  // 图表 can always come once more (up to nine); the others while they are not on the board.
  const free = MODULES.filter(item => item.many ? nextChartBlock(layout) !== null : !leavesOf(layout).includes(item.id));
  const act = (change: BoardsChange) => { if (run(change)) setOpen(false); };
  return <span className="board-tile-menu" ref={ref} onMouseDown={event => event.stopPropagation()}>
    <button type="button" className="board-tile-menu-button" aria-label={`${label}的菜单`} aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen(!open)}><MoreHorizontal /></button>
    {open ? <div className={`fdw-tab-menu board-tile-menu-list${up ? " up" : ""}`} role="menu" aria-label={`${label}的菜单`} data-agent-target={`board-tile-menu:${block}`}>
      {split ? <>
        <small className="board-menu-caption">{split === "right" ? "左右分：右边放" : "上下分：下边放"}</small>
        {free.map(item => <div role="none" key={item.id}><button type="button" role="menuitem" onClick={() => act({ place: { module: item.id, target: block, side: split } })}>
          {item.many ? `再放一块${item.label}` : item.label}</button></div>)}
      </> : <>
        {free.some(item => item.id !== module) ? <><small className="board-menu-caption">换成别的模块</small>
          {free.filter(item => item.id !== module).map(item => <div role="none" key={item.id}><button type="button" role="menuitem"
            onClick={() => act({ replace: { module: block, with: item.id } })}>换成{item.label}</button></div>)}</> : null}
        <small className="board-menu-caption">档位{size ? ` · 现在${SIZE_LABELS[size]}` : ""}</small>
        <div className="board-segment" role="radiogroup" aria-label={`${label}的档位`}>
          <button type="button" role="radio" aria-checked={!locked} onClick={() => act({ lock: { module: block, size: "auto" } })}>自动</button>
          <button type="button" role="radio" aria-checked={locked === "small"} onClick={() => act({ lock: { module: block, size: "small" } })}>{module === "chat" ? "锁一条" : "锁小"}</button>
          <button type="button" role="radio" aria-checked={locked === "medium"} onClick={() => act({ lock: { module: block, size: "medium" } })}>锁中</button>
        </div>
        {link ? <>
          <small className="board-menu-caption">联动色（同色的跟同一个合约）</small>
          <div className="board-segment" role="radiogroup" aria-label={`${label}的联动色`}>
            {LINK_COLORS.map(item => <button key={item.id} type="button" role="radio" aria-checked={link === item.id} aria-label={`联动${item.label}色`}
              onClick={() => act({ link: { module: block, color: item.id } })}><span className="board-link-dot inline" data-link={item.id} aria-hidden="true" />{item.label}</button>)}
            {module === "chart" ? <button type="button" role="radio" aria-checked={link === "none"} aria-label="不联动"
              onClick={() => act({ link: { module: block, color: "none" } })}>不联动</button> : null}
          </div>
        </> : null}
        {free.length ? <div className="board-menu-row">
          <button type="button" className="app-button" onClick={() => setSplit("right")}>左右分</button>
          <button type="button" className="app-button" onClick={() => setSplit("bottom")}>上下分</button>
        </div> : null}
        <i className="board-menu-divider" aria-hidden="true" />
        <div role="none"><button type="button" role="menuitem" className="board-danger" onClick={() => act({ take: block })}><Trash2 />拿掉（旁边的补满）</button></div>
      </>}
    </div> : null}
  </span>;
}

/** A chart block's own contract and period, in its name bar (the 多图 cell's choice: 跟顶栏 = the app's own). */
function ChartPick({ block, choice, link, contracts, run }: { block: BoardBlock; choice: { symbol?: string; duration?: number }; link?: LinkSetting;
  contracts: readonly { symbol: string; name: string }[]; run: (change: BoardsChange) => boolean }) {
  const label = blockLabel(block);
  const own = link === "none";
  const listed = contracts.some(item => item.symbol === choice.symbol);
  return <span className="board-chart-pick" onMouseDown={event => event.stopPropagation()}>
    {/* Picking a contract makes the chart 不联动; 跟联动色 / 跟顶栏 puts it back on its colour or the app's contract. */}
    <select aria-label={`${label}的合约`} value={own ? choice.symbol ?? "" : ""}
      onChange={event => run(event.target.value ? { chart: { block, symbol: event.target.value } } : own && block === "chart" ? { link: { module: block, color: "blue" } } : { chart: { block, symbol: null } })}>
      <option value="">{own ? "跟顶栏合约" : "跟联动色"}</option>
      {choice.symbol && !listed ? <option value={choice.symbol}>{choice.symbol}</option> : null}
      {contracts.map(item => <option key={item.symbol} value={item.symbol}>{item.name}</option>)}
    </select>
    <select aria-label={`${label}的周期`} value={choice.duration ?? ""} onChange={event => run({ chart: { block, duration: event.target.value ? Number(event.target.value) : null } })}>
      <option value="">跟顶栏周期</option>
      {CHART_DURATIONS.map(duration => <option key={duration} value={duration}>{durationLabel(duration)}</option>)}
    </select>
  </span>;
}

/** Editing: the block's name bar (react-mosaic makes its native host the drag handle) — its size, share and ⋯ menu. */
function TileBar({ block, size, layout, locked, link, chart, contracts, run }: { block: BoardBlock; size: BoardSize | undefined; locked?: BoardLock;
  link?: LinkSetting; layout: BoardNode | null; chart?: { symbol?: string; duration?: number }; contracts: readonly { symbol: string; name: string }[];
  run: (change: BoardsChange) => boolean }) {
  const share = shareOf(layout, block);
  return <div className="board-tile-bar" title={`拖动${blockLabel(block)}，放到别的模块边上`}>
    <GripVertical aria-hidden="true" />{link && link !== "none" ? <span className="board-link-dot inline" data-link={link} role="img"
      aria-label={`联动：${LINK_COLORS.find(item => item.id === link)?.label}`} /> : null}<strong>{blockLabel(block)}</strong>
    <small>{size ? `${SIZE_LABELS[size]}${locked ? " · 锁定" : ""}` : ""}{share ? ` · ${Math.round(share.share)}%` : ""}</small>
    {chart ? <ChartPick block={block} choice={chart} link={link} contracts={contracts} run={run} /> : null}
    <span className="board-grow" />
    <TileMenu block={block} size={size} layout={layout} locked={locked} link={link} run={run} />
  </div>;
}

/** A 模块库 entry: drag it onto a block's edge (the block shows where it lands), or click to put it on the right. */
function LibraryItem({ module, placed, many, onDragging, onDrop, onClick }: { module: BoardModule; placed: boolean; many?: boolean; onDragging: (dragging: boolean) => void;
  onDrop: (module: BoardModule, drop: MosaicDropData) => void; onClick: () => void }) {
  const [, drag] = useDrag(() => ({
    type: MosaicDragType.WINDOW,
    // The card fades out after the browser has taken the drag image.
    item: () => { setTimeout(() => onDragging(true), 0); return { mosaicId: MOSAIC_ID }; },
    canDrag: !placed,
    end: (_item, monitor) => { onDragging(false); const drop = monitor.getDropResult() as MosaicDropData | null; if (drop?.path) onDrop(module, drop); },
  }), [module, placed, onDragging, onDrop]);
  const info = moduleInfo(module);
  return <li><button type="button" ref={node => { drag(node); }} className="board-library-item" disabled={placed}
    aria-label={placed ? `${info.label}已在看板上` : many ? `再放一块${info.label}` : `把${info.label}放进看板`} onClick={onClick}>
    <span className="board-library-icon"><ModuleIcon module={module} /></span>
    <span className="board-library-text"><strong>{info.label}</strong><small>{placed ? module === "chart" ? "已经放满九块" : "已在看板上" : many ? "再放一块：拖到某一块的边上" : "拖到某一块的边上"}</small></span>
    {placed ? null : <GripVertical aria-hidden="true" />}
  </button></li>;
}

/** The 模块库: a small card over the board, moved by its title, out of the way while a module is dragged. */
function Library({ control, layout, run }: { control: BoardsControl; layout: BoardNode | null; run: (change: BoardsChange) => boolean }) {
  useDisclosureSurface("board-library", { state: control.library, apply: control.setLibrary, available: control.editing });
  const [where, setWhere] = useState<{ x: number; y: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const card = useRef<HTMLElement>(null);
  const placed = leavesOf(layout);
  const onDrop = useCallback((module: BoardModule, drop: MosaicDropData) => {
    // 图表 dropped again is one more chart block; any other module moves if it is already there.
    const block = module === "chart" && placed.includes("chart") ? nextChartBlock(layout) : module;
    if (!block) return;
    run({ layout: insertAt(removeLeaf(layout, block), drop.path ?? [], drop.position ?? "right", block, drop.path?.length ? 50 : 33) });
  }, [layout, run, placed]);
  const move = (event: ReactPointerEvent<HTMLElement>) => {
    const node = card.current;
    if (!node || (event.target as HTMLElement).closest("button")) return;
    const start = { x: event.clientX, y: event.clientY, left: node.offsetLeft, top: node.offsetTop };
    const follow = (next: PointerEvent) => setWhere({ x: start.left + next.clientX - start.x, y: Math.max(0, start.top + next.clientY - start.y) });
    const stop = () => { window.removeEventListener("pointermove", follow); window.removeEventListener("pointerup", stop); };
    window.addEventListener("pointermove", follow);
    window.addEventListener("pointerup", stop);
  };
  if (!control.editing || !control.library) return null;
  return <aside ref={card} className={`board-library${dragging ? " dragging" : ""}`} aria-label="模块库" data-agent-target="board-library"
    style={where ? { left: where.x, top: where.y, right: "auto" } : undefined}>
    <header onPointerDown={move}>
      <GripVertical aria-hidden="true" /><strong>模块库</strong>
      <HelpTip helpKey="board-library" label="模块库">把模块拖到任意一块的边上，那一块会亮出它落在哪一半，松手就分出一块；点一下模块就放到整块看板的右边。按住这张卡片的标题可以挪开它。每个模块在一个看板上只放一次。</HelpTip>
      <span className="board-grow" />
      <button type="button" className="board-icon-button" aria-label="收起模块库" onClick={() => control.setLibrary(false)}><X /></button>
    </header>
    <ul>{MODULES.map(item => <LibraryItem key={item.id} module={item.id} placed={item.many ? nextChartBlock(layout) === null : placed.includes(item.id)}
      many={item.many && placed.includes(item.id)} onDragging={setDragging} onDrop={onDrop}
      onClick={() => { run({ place: { module: item.id } }); }} />)}</ul>
  </aside>;
}

/**
 * 看板 · 铺满版 (owner 2026-11-05): the board always fills the room — blocks share it by the stored proportions
 * (react-mosaic). Viewing, a block is its module at the size its room allows; editing, each block has a name bar (drag
 * it to another block's edge, ⋯ for 换 / 档位 / 分 / 拿掉), the dividers drag, and ＋ 模块 opens the 模块库.
 */
export function BoardsView({ control, renderTile, scale, contracts }: {
  control: BoardsControl;
  /** The font preference's factor (标准 1, 大字 1.1, 特大 1.2): the size thresholds grow half as fast. */
  scale: number;
  renderTile: (block: BoardBlock, size: BoardSize) => ReactNode;
  /** What a chart block's name bar offers as its own contract. */
  contracts: readonly { symbol: string; name: string }[];
}) {
  const { handle, editing, notify, sizes, reportTile, preview } = control;
  const { board } = handle;
  const run = useCallback((change: BoardsChange) => {
    try { handle.apply(change); return true; } catch (error) { notify(error instanceof Error ? error.message : "看板暂时不能修改"); return false; }
  }, [handle, notify]);
  const canvasRef = useRef<HTMLDivElement>(null);
  const { canvas } = control;
  useLayoutEffect(() => {
    const node = canvasRef.current;
    if (!node) return;
    const read = () => { canvas.current = { w: node.clientWidth, h: node.clientHeight }; };
    read();
    const observer = new ResizeObserver(read);
    observer.observe(node);
    return () => observer.disconnect();
  }, [canvas]);
  // An AI arrangement is shown as it would be (its locks too), not editable, until 用这套 or 不用.
  const shown: Pick<Board, "layout" | "locks"> = preview ?? board;
  const shaping = editing && !preview;
  const dotted = leavesOf(shown.layout).filter(isLinkBlock).map(block => linkOf(board, block)).filter(link => link !== "none");
  const showDots = editing || new Set(dotted).size > 1;
  const tile = (block: BoardBlock, path: MosaicPath) => <MosaicWindow<BoardBlock> path={path} title={blockLabel(block)} draggable={shaping}
    renderToolbar={() => <div className="board-tile-bar-host">{shaping ? <TileBar block={block} size={sizes[block]} locked={board.locks[block]}
      link={isLinkBlock(block) ? linkOf(board, block) : undefined} layout={board.layout} run={run} contracts={contracts}
      chart={moduleOf(block) === "chart" ? board.charts[block] ?? {} : undefined} /> : null}</div>}>
    <TileBody block={block} lock={shown.locks[block]} link={showDots && !shaping && isLinkBlock(block) ? linkOf(board, block) : undefined}
      scale={scale} editing={editing} reportTile={reportTile} renderTile={renderTile} />
  </MosaicWindow>;
  return <section className={`board-view${shaping ? " editing" : ""}${preview ? " previewing" : ""}`} aria-label={`看板 ${board.name}`} data-agent-target="board-view"
    data-state={preview ? "preview" : editing ? "editing" : "viewing"}>
    {control.notice ? <p className="board-notice" role="status">{control.notice}</p> : null}
    {preview ? <div className="board-preview-bar" role="status" data-agent-target="board-preview">
      <Sparkles aria-hidden="true" /><span><strong>AI 摆的这套</strong>{preview.note ? `：${preview.note}` : ""}</span>
      <span className="board-grow" />
      <button type="button" className="app-button" onClick={control.discard}>不用</button>
      <button type="button" className="app-button primary" onClick={() => { try { control.adopt(); } catch (error) { notify(error instanceof Error ? error.message : "没用上"); } }}>用这套</button>
    </div> : null}
    <ArrangeCard control={control} />
    <DndProvider backend={HTML5Backend}>
      <div className="board-canvas" data-agent-target="board-canvas" ref={canvasRef}>
        <MosaicWithoutDragDropContext<BoardBlock> mosaicId={MOSAIC_ID} className="board-mosaic" value={shown.layout}
          onChange={next => { if (!preview) handle.preview(next); }} onRelease={next => { if (!preview) run({ layout: next }); }}
          resize={shaping ? { minimumPaneSizePercentage: 8 } : "DISABLED"} renderTile={tile}
          zeroStateView={<p className="board-empty-prompt">{editing ? "这一套还空着：点顶栏“＋ 模块”，把模块拖进来，或“让 AI 帮我摆”" : "这一套还空着：点顶栏“看板”旁的箭头，选“编辑”放模块进来"}</p>} />
      </div>
      {preview ? null : <Library control={control} layout={board.layout} run={run} />}
    </DndProvider>
  </section>;
}

/** 让 AI 帮我摆: one sentence, the AI lays the board out and shows it before anything changes (needs editing). */
function ArrangeCard({ control }: { control: BoardsControl }) {
  useDisclosureSurface("board-arrange", { state: control.asking, apply: control.setAsking, available: control.editing });
  const [text, setText] = useState("");
  if (!control.editing || !control.asking) return null;
  const send = () => { void control.ask(text).catch(error => control.notify(error instanceof Error ? error.message : "AI 这次没摆好")); };
  return <form className="board-arrange" aria-label="让 AI 帮我摆" data-agent-target="board-arrange" onSubmit={event => { event.preventDefault(); send(); }}>
    <Sparkles aria-hidden="true" />
    <input value={text} maxLength={200} autoFocus aria-label="想怎么摆" placeholder="比如：摆一套盘中，主看螺纹" onChange={event => setText(event.target.value)} />
    <button type="submit" className="app-button primary" disabled={control.arranging || !text.trim()}>{control.arranging ? "在摆…" : "摆"}</button>
    <button type="button" className="board-icon-button" aria-label="收起" onClick={() => control.setAsking(false)}><X /></button>
  </form>;
}
