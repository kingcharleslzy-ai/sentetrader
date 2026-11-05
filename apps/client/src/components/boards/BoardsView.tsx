import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, ChevronLeft, GripVertical, LayoutDashboard, MoreHorizontal, Plus, Trash2, X } from "lucide-react";
import GridLayout, { getCompactor, type Layout, type LayoutItem } from "react-grid-layout";
import { GridBackground } from "react-grid-layout/extras";
import "react-grid-layout/css/styles.css";
import { applyUiSurface, useDisclosureSurface, useUiSurface } from "../../uiSurfaces";
import { fitClasses, rowContentOverflows, useFitLevel } from "../futures/useHeaderFit";
import { HelpTip } from "../Blocks";
import { Presence } from "../motion";
import { CELL, MODULES, SIZE_CELLS, measuredGrid, moduleInfo, shownBoard, type BoardModule, type BoardSize, type BoardsChange, type BoardsHandle } from "./boards";
import "./BoardsView.css";

const GAP = 8;
const DROPPING = "__dropping-elem__";
/** The size a module takes when dragged in from the library (the owner's design drags 图表 in as 大). */
const DROP_SIZE: Record<BoardModule, BoardSize> = { chart: "large", positions: "medium", order: "small", watch: "medium" };
/** Free placement on the 12 × 8 cells: nothing floats up, nothing is pushed aside, a tile never covers another. */
const COMPACTOR = getCompactor(null, false, true);
const MODULE_ICONS: Record<BoardModule, string> = {
  chart: "M4 19V5M4 19h16M8 15l3-4 3 2 4-6",
  positions: "M4 7h16v12H4zM9 7V5h6v2M4 12h16",
  order: "M5 8h14M5 16h14M8 5L5 8l3 3M16 13l3 3-3 3",
  watch: "M12 4l2.4 5 5.6.8-4 3.9 1 5.5-5-2.7-5 2.7 1-5.5-4-3.9 5.6-.8z",
};
/** The board bar gives up space one way (一套自适应): tighter, no 编辑中 tag, 回到交易 as an icon, then only the
 *  board on screen with the rest in 更多看板 (the header's board list). */
const BAR_FIT = ["board-fit-tight", "board-fit-hide-tag", "board-fit-icon-back", "board-fit-current-tab"];
const barOverflows = (bar: HTMLElement) => rowContentOverflows(bar, [".board-tabs"], ".board-grow");
const ModuleIcon = ({ module }: { module: BoardModule }) => <svg viewBox="0 0 24 24" aria-hidden="true"><path d={MODULE_ICONS[module]} /></svg>;

export interface BoardsControl {
  handle: BoardsHandle;
  editing: boolean;
  setEditing: (editing: boolean) => void;
  selected: BoardModule | null;
  setSelected: (module: BoardModule | null) => void;
  /** Put the 看板 on screen (the view). */
  show: () => void;
}

/**
 * The `boards` UI command and the snapshot's `state.boards` (editing, the selected tile); the list itself is the
 * account preference ui.boards. Lives with the workspace so a command lands while another view is on screen.
 */
export function useBoardsControl(handle: BoardsHandle, show: () => void, available: boolean): BoardsControl {
  const [editing, setEditingState] = useState(false);
  const [selected, setSelected] = useState<BoardModule | null>(null);
  const { resetUndo } = handle;
  const setEditing = useCallback((value: boolean) => {
    setEditingState(value);
    setSelected(null);
    if (value) resetUndo();
  }, [resetUndo]);
  const latest = useRef({ handle, show, setEditing });
  latest.current = { handle, show, setEditing };
  useUiSurface("boards", { state: { editing, selected }, apply: value => {
    const change = value as BoardsChange & { editing?: boolean; undo?: boolean };
    const { handle: boards, show: open, setEditing: edit } = latest.current;
    if (change.undo !== undefined) {
      if (!boards.undo) throw new Error("没有可撤销的改动（只撤销这次编辑里的放、挪、换档、删）");
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
    if (change.take !== undefined) setSelected(current => current === change.take ? null : current);
  } }, [editing, selected], available);
  // A tile taken off the board is no longer selected.
  useEffect(() => {
    if (selected && !handle.board.tiles.some(tile => tile.module === selected)) setSelected(null);
  }, [handle.board, selected]);
  // Switching boards ends nothing but the selection.
  useEffect(() => { setSelected(null); }, [handle.board.id]);
  return { handle, editing, setEditing, selected, setSelected, show };
}

/** 顶栏“看板” (owner 2026-11-05): one click shows the board used last; the arrow lists them all and 新建空白看板. */
export function BoardsMenu({ control, active, onError }: { control: BoardsControl; active: boolean; onError: (message: string) => void }) {
  const [open, setOpen] = useDisclosureSurface("boards-menu");
  const ref = useRef<HTMLSpanElement>(null);
  const { boards } = control.handle;
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("pointerdown", close); window.removeEventListener("keydown", escape); };
  }, [open, setOpen]);
  const run = (change: BoardsChange) => {
    try { control.handle.apply(change); control.show(); setOpen(false); } catch (error) { onError(error instanceof Error ? error.message : "看板暂时不能切换"); }
  };
  const createBlank = () => {
    const used = new Set(boards.items.map(item => item.name));
    let n = 1;
    while (used.has(`看板 ${n}`)) n += 1;
    run({ create: `看板 ${n}` });
    control.setEditing(true);
  };
  return <span className={`board-entry${active ? " active" : ""}`} ref={ref}>
    <button type="button" className="fdw-nav-view board-entry-main" aria-label="看板" aria-pressed={active} onClick={() => { control.show(); setOpen(false); }}>
      <LayoutDashboard /><span>看板</span>
    </button>
    <button type="button" className="board-entry-arrow" aria-label="选择看板" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}><ChevronDown /></button>
    <Presence>{open ? <div className="fdw-tab-menu board-entry-menu" role="menu" aria-label="选择看板" data-agent-target="boards-menu">
      {boards.items.map(item => <div key={item.id} role="none">
        <button type="button" role="menuitem" aria-current={item.id === boards.current || undefined} onClick={() => run({ select: item.id })}>
          <span>{item.name}</span>{item.id === boards.current ? <small>上次用的</small> : null}
        </button>
      </div>)}
      <div role="none"><button type="button" role="menuitem" className="fdw-tab-menu-add" onClick={createBlank}><Plus />新建空白看板</button></div>
    </div> : null}</Presence>
  </span>;
}

function useBox<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [box, setBox] = useState({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const read = () => setBox(old => old.width === node.clientWidth && old.height === node.clientHeight ? old : { width: node.clientWidth, height: node.clientHeight });
    read();
    const observer = new ResizeObserver(read);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return [ref, box] as const;
}

/** The ⋯ menu beside the tabs: 改名 and 删除 for the board on screen. */
function BoardMoreMenu({ control, onError }: { control: BoardsControl; onError: (message: string) => void }) {
  const { board, boards } = control.handle;
  const [open, setOpen] = useDisclosureSurface("board-more");
  const [name, setName] = useState(board.name);
  const [confirming, setConfirming] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { setName(board.name); setConfirming(false); }, [board.id, board.name, open]);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [open, setOpen]);
  const run = (change: BoardsChange) => { try { control.handle.apply(change); setOpen(false); } catch (error) { onError(error instanceof Error ? error.message : "看板暂时不能修改"); } };
  return <div className="board-more" ref={ref}>
    <button type="button" className="board-icon-button" aria-label="改名或删除这套看板" aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen(!open)}><MoreHorizontal /></button>
    {open ? <div className="fdw-tab-menu board-more-menu" role="menu" aria-label={`${board.name} 看板`} data-agent-target="board-more">
      <form onSubmit={event => { event.preventDefault(); run({ rename: { id: board.id, name } }); }}>
        <label>名字<input value={name} maxLength={12} onChange={event => setName(event.target.value)} /></label>
        <button type="submit" className="app-button" disabled={!name.trim() || name.trim() === board.name}>改名</button>
      </form>
      <button type="button" role="menuitem" className="board-danger" disabled={boards.items.length === 1}
        title={boards.items.length === 1 ? "至少留一个看板" : undefined}
        onClick={() => confirming ? run({ remove: board.id }) : setConfirming(true)}>
        <Trash2 />{confirming ? `确认删除“${board.name}”` : "删除这套看板"}
      </button>
    </div> : null}
  </div>;
}

/**
 * 看板 (owner 2026-11-05): the board on screen. Viewing, every tile is its module at its size; 编辑 shows the cells,
 * the 模块库 (drag a module in, or click it to drop it in the first free spot) and, on the selected tile, its sizes
 * and 删除. Every change is saved into the board at once.
 */
export function BoardsView({ control, renderTile, onExit, onError, scale }: {
  control: BoardsControl;
  /** The font preference's factor (标准 1, 大字 1.1, 特大 1.2): a cell grows with the type. */
  scale: number;
  renderTile: (module: BoardModule, size: BoardSize) => ReactNode;
  onExit: () => void;
  onError: (message: string) => void;
}) {
  const { handle, editing, setEditing, selected, setSelected } = control;
  const { boards, board } = handle;
  const [library, setLibrary] = useDisclosureSurface("board-library", { initial: true, available: editing });
  const [canvasRef, box] = useBox<HTMLDivElement>();
  const dragging = useRef<BoardModule | null>(null);
  const run = useCallback((change: BoardsChange) => {
    try { handle.apply(change); return true; } catch (error) { onError(error instanceof Error ? error.message : "看板暂时不能修改"); return false; }
  }, [handle, onError]);
  // The grid this canvas holds (每格大小固定, the count follows the measured space) and the board laid out on it.
  const screen = box.width > 0 && box.height > 0 ? measuredGrid(box.width, box.height, scale, GAP) : null;
  const { setScreen } = handle;
  useEffect(() => { setScreen(screen); }, [setScreen, screen?.cols, screen?.rows]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => setScreen(null), [setScreen]);
  const shown = shownBoard(board, screen);
  const cellW = CELL.w * scale, cellH = CELL.h * scale;
  const gridWidth = shown.cols * cellW + (shown.cols - 1) * GAP;
  const gridHeight = shown.rows * cellH + (shown.rows - 1) * GAP;
  const layout: Layout = shown.tiles.map(tile => ({ i: tile.module, x: tile.x, y: tile.y, w: SIZE_CELLS[tile.size].w, h: SIZE_CELLS[tile.size].h }));
  const moved = (item: LayoutItem | null) => {
    if (!item || item.i === DROPPING) return;
    const tile = shown.tiles.find(one => one.module === item.i);
    if (tile && (tile.x !== item.x || tile.y !== item.y)) run({ place: { module: tile.module, x: item.x, y: item.y } });
  };
  const dropSize = (module: BoardModule | null) => module ? SIZE_CELLS[board.tiles.find(tile => tile.module === module)?.size ?? DROP_SIZE[module]] : SIZE_CELLS.small;
  const placed = new Set(board.tiles.map(tile => tile.module));
  const barRef = useRef<HTMLElement>(null);
  const fit = useFitLevel(barRef, `${boards.items.map(item => item.name).join("|")}:${board.id}:${editing}:${library}:${Boolean(handle.undo)}`, BAR_FIT, barOverflows);
  return <section className={`board-view${editing ? " editing" : ""}`} aria-label={`看板 ${board.name}`} data-agent-target="board-view" data-state={editing ? "editing" : "viewing"}>
    <header className={`board-bar ${fitClasses(BAR_FIT, fit)}`} ref={barRef} data-fit={fit}>
      <button type="button" className="board-back" aria-label="回到交易" onClick={onExit}><ChevronLeft /><span>回到交易</span></button>
      <i className="fdw-nav-divider" aria-hidden="true" />
      <div className="board-tabs" role="tablist" aria-label="看板">
        {boards.items.map(item => <button type="button" role="tab" key={item.id} aria-selected={item.id === boards.current}
          onClick={() => run({ select: item.id })}>{item.name}</button>)}
      </div>
      <button type="button" className="board-icon-button board-more-boards" aria-label="更多看板" aria-hidden={fit < BAR_FIT.length || undefined}
        tabIndex={fit < BAR_FIT.length ? -1 : undefined} onClick={() => applyUiSurface("disclosure:boards-menu", true)}><ChevronDown /></button>
      <button type="button" className="board-icon-button" aria-label="新建空白看板" onClick={() => {
        const used = new Set(boards.items.map(item => item.name));
        let n = 1;
        while (used.has(`看板 ${n}`)) n += 1;
        if (run({ create: `看板 ${n}` })) setEditing(true);
      }}><Plus /></button>
      <BoardMoreMenu control={control} onError={onError} />
      <span className="board-grow" />
      {editing ? <>
        <span className="board-editing-tag">编辑中</span>
        <button type="button" className="ghost-button" disabled={!handle.undo} onClick={() => handle.undo?.()}>撤销</button>
        {!library ? <button type="button" className="app-button" onClick={() => setLibrary(true)}>模块库</button> : null}
        <button type="button" className="app-button primary" onClick={() => setEditing(false)}>完成</button>
      </> : <button type="button" className="app-button" onClick={() => setEditing(true)}>编辑</button>}
    </header>
    <div className="board-body">
      <div className="board-canvas" ref={canvasRef} data-agent-target="board-canvas">
        {box.width > 0 && box.height > 0 ? <>
          {editing ? <GridBackground className="board-cells" width={gridWidth} cols={shown.cols} rowHeight={cellH} margin={[GAP, GAP]}
            containerPadding={[0, 0]} rows={shown.rows} color="currentColor" borderRadius={6} style={{ width: gridWidth, height: gridHeight }} /> : null}
          <GridLayout width={gridWidth} layout={layout} compactor={COMPACTOR} autoSize={false} style={{ width: gridWidth, height: gridHeight }}
            gridConfig={{ cols: shown.cols, rowHeight: cellH, margin: [GAP, GAP], containerPadding: [0, 0], maxRows: shown.rows }}
            dragConfig={{ enabled: editing, bounded: true, handle: ".board-tile-grip" }}
            resizeConfig={{ enabled: false }}
            dropConfig={{ enabled: editing, defaultItem: { w: 3, h: 2 }, onDragOver: () => dragging.current ? dropSize(dragging.current) : false }}
            droppingItem={{ i: DROPPING, x: 0, y: 0, ...dropSize(dragging.current) }}
            onDragStop={(_layout, _old, item) => moved(item)}
            onDrop={(_layout, item) => {
              const module = dragging.current;
              dragging.current = null;
              if (module && item) run({ place: { module, size: board.tiles.find(tile => tile.module === module)?.size ?? DROP_SIZE[module], x: item.x, y: item.y } });
            }}>
            {board.tiles.map(tile => {
              const info = moduleInfo(tile.module);
              const isSelected = editing && selected === tile.module;
              return <div key={tile.module} className={`board-tile size-${tile.size}${isSelected ? " selected" : ""}`}
                data-agent-target={`board-tile:${tile.module}`} data-state={tile.size} aria-label={`${info.label} · ${SIZE_CELLS[tile.size].label}`} role="group">
                <div className="board-tile-content" inert={editing || undefined}>{renderTile(tile.module, tile.size)}</div>
                {editing ? <button type="button" className="board-tile-grip" aria-pressed={isSelected}
                  aria-label={`选中${info.label}（${SIZE_CELLS[tile.size].label}），拖动可挪位置`} onClick={() => setSelected(isSelected ? null : tile.module)}>
                  <GripVertical /><span>{info.label} · {SIZE_CELLS[tile.size].label}</span>
                </button> : null}
                {isSelected ? <div className="board-tile-tools" role="toolbar" aria-label={`${info.label}的档位`}>
                  <div className="board-segment" role="radiogroup" aria-label="档位">
                    {info.sizes.map(size => <button type="button" role="radio" key={size} aria-checked={tile.size === size}
                      onClick={() => run({ place: { module: tile.module, size } })}>{SIZE_CELLS[size].label}</button>)}
                  </div>
                  <button type="button" className="board-icon-button board-danger" aria-label={`从看板删除${info.label}`} onClick={() => run({ take: tile.module })}><Trash2 /></button>
                </div> : null}
              </div>;
            })}
          </GridLayout>
        </> : null}
        {!board.tiles.length ? <p className="board-empty-prompt">{editing ? "这一套还空着：从右边拖模块进来，会自动对齐格子" : "这一套还空着：点“编辑”放模块进来"}</p> : null}
      </div>
      {editing && library ? <aside className="board-library" aria-label="模块库" data-agent-target="board-library">
        <header>
          <strong>模块库</strong>
          <HelpTip helpKey="board-library" label="模块库">拖一个模块到左边的格子里，松手就吸附对齐；也可以点一下，放进第一个空位。每个模块在一个看板上只放一次，放好后点它换档或删除。这一批的模块都跟着当前合约走。</HelpTip>
          <span className="board-grow" />
          <button type="button" className="board-icon-button" aria-label="收起模块库" onClick={() => setLibrary(false)}><X /></button>
        </header>
        <ul>
          {MODULES.map(item => <li key={item.id}>
            <button type="button" className="board-library-item" draggable={!placed.has(item.id)} unselectable="on"
              aria-label={placed.has(item.id) ? `${item.label}已在看板上，选中它` : `把${item.label}放进看板`}
              onDragStart={event => { dragging.current = item.id; event.dataTransfer.setData("text/plain", item.id); event.dataTransfer.effectAllowed = "copy"; }}
              onDragEnd={() => { dragging.current = null; }}
              onClick={() => placed.has(item.id) ? setSelected(item.id) : run({ place: { module: item.id, size: DROP_SIZE[item.id] } })}>
              <span className="board-library-icon"><ModuleIcon module={item.id} /></span>
              <span className="board-library-text"><strong>{item.label}</strong>
                <span>{item.sizes.map(size => <small key={size}>{SIZE_CELLS[size].label}</small>)}</span></span>
              {placed.has(item.id) ? <small className="board-library-placed">已放</small> : <GripVertical aria-hidden="true" />}
            </button>
          </li>)}
        </ul>
      </aside> : null}
    </div>
  </section>;
}
