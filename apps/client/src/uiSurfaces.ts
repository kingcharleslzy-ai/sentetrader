import { useEffect, useRef, useState, useSyncExternalStore, type Dispatch, type RefObject, type SetStateAction } from "react";

/**
 * Registry of user-operable surfaces (dialogs, cards, collapsibles, tabs) so the
 * agent's UI commands can open what a click opens and the observation can report
 * their state. Components register while mounted; the workspace reads the registry
 * to apply `dialog`-style commands and to build `UiState`.
 */
export interface UiSurface<S = unknown> {
  /** Current state for the observation (`open`, `expanded`, selected id …). */
  state: S;
  /** Apply a command; throw a user-readable error when the surface cannot comply. */
  apply?: (value: unknown) => void;
}

const surfaces = new Map<string, UiSurface>();
const pending = new Map<string, unknown>();
const listeners = new Set<() => void>();
// Mount order by name, kept across re-registrations: "the most recent card" is the
// disclosure registered last for the first time.
const firstSeen = new Map<string, number>();
let sequence = 0;
let version = 0;

function notify() {
  version += 1;
  listeners.forEach(listener => listener());
}

export function registerUiSurface(name: string, surface: UiSurface): () => void {
  surfaces.set(name, surface);
  if (!firstSeen.has(name)) firstSeen.set(name, ++sequence);
  // A command that arrived while the surface was not mounted (e.g. the review
  // page opened by the same command) lands as soon as it mounts.
  if (pending.has(name) && surface.apply) {
    const value = pending.get(name);
    pending.delete(name);
    queueMicrotask(() => { try { surface.apply?.(value); } catch { /* The receipt already reported the navigation. */ } });
  }
  notify();
  return () => { if (surfaces.get(name) === surface) { surfaces.delete(name); notify(); } };
}

export function uiSurface(name: string): UiSurface | undefined {
  return surfaces.get(name);
}

/** Apply now when the surface is mounted, otherwise when it next mounts. */
export function applyUiSurface(name: string, value: unknown) {
  const surface = surfaces.get(name);
  if (surface?.apply) surface.apply(value);
  else pending.set(name, value);
}

export function uiSurfaceStates(): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  surfaces.forEach((surface, name) => { result[name] = surface.state; });
  return result;
}

/**
 * Register a surface for the component's lifetime; re-registers when its state
 * changes. `available: false` (the component renders nothing) leaves it out of
 * the registry so the observation does not report a surface nobody can see.
 */
export function useUiSurface(name: string, surface: UiSurface, deps: unknown[], available = true) {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => available ? registerUiSurface(name, surface) : undefined, [name, available, ...deps]);
}

/** Re-render the caller whenever any surface registers, unregisters or changes state. */
export function useUiSurfaceVersion(): number {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => version, () => version);
}

/** Test helper. */
export function resetUiSurfaces() { surfaces.clear(); pending.clear(); firstSeen.clear(); notify(); }

/** Disclosures that live inside conversation cards; a command without `id` applies to the most recent card. */
export const CARD_DISCLOSURES = new Set(["progress-steps", "advice-sources", "tool-card", "diff-card", "card-details",
  // 2026-09-25: per object — a running card's 运行日志 / 执行方式 menu / 停止 confirmation (instance id) and "AI ▾" menus.
  "strategy-run-log", "strategy-signal-action", "strategy-stop-confirm", "ai-menu",
  "strategy-actions", "strategy-delete-confirm",
  // 规则回测 (2026-09-25): the 逐笔清单 fold of a conversation's rule-backtest card.
  "strategy-backtest-trades",
  // 说人话 (2026-09-25): a parameter's “?” note (id = parameter key) and a template card's 详情 (id = template id).
  "strategy-param-help", "strategy-template-details",
  // 图表指标可配置 (2026-10-02): a chart sub-pane's label popover (id = pane type).
  "chart-pane-editor",
  // 下单顺带保护 (2026-10-02): a position row's 保护 form (`id` = position:<symbol>:<long|short>).
  "position-protection",
  // 我的流程 (2026-10-06): a routine row's 删 confirmation in 设置 → 记忆 (`id` = routine id).
  "routine-delete",
  // 新闻改版 (2026-10-07): a merged news event's “N 家报道 · 展开” on the 快讯 / 资讯 tabs (`id` = event id).
  "flash-event", "news-event",
  // 英文原文折叠 (2026-10-11): a translated headline's 原文 fold — a raw feed row, a screened event's
  // headline or one of its reports (`id` = the item's / report's — on the phone, the event's — id).
  "flash-orig", "news-orig",
  // 手机编辑卡 (2026-10-08): one of the four sections or the 策略流程 (`id` = basics / entry / exit / sizing / flow; one open
  // at a time) and a condition's bottom sheet (`id` = <entryLong|entryShort|exitLong|exitShort>:<第几条, from 1>).
  "strategy-editor-section", "strategy-editor-condition",
  // 策略编辑卡第二版 (2026-10-08): a condition side's 指标选择器 (`id` = <group>:<第几条>:<left|right>) and every "?" (`id` = its key).
  "strategy-editor-picker", "help-tip",
  // 手机看盘 (2026-10-09): one 多图 cell's contract / period sheet (`id` = the cell's index 0–5).
  "mobile-chart-cell"]);
/**
 * 画线第二版 (2026-10-09): disclosures with one entry per drawing, tool, tool group or right-click menu (and 手机画线's
 * style row: color / width / dash).
 * Each is one registered family (`family:<name>`) whose state is the list of open ids; the snapshot
 * reports only the open ones (`name:id`), so a missing entry is closed — the Runtime judges it so too.
 */
export const SPARSE_DISCLOSURES = new Set(["chart-drawing-group", "chart-drawing-tool", "chart-drawing-style", "chart-drawing-settings", "chart-context-menu",
  "mobile-drawing-style-row"]);
/** Card disclosures a command must name by `id` (there is no "most recent" one to fall back on). */
export const ID_REQUIRED_DISCLOSURES: Record<string, string> = {
  "chart-drawing-group": "要带 id：lines / levels / fib / shapes / notes / measure",
  "chart-drawing-tool": "要带 id：工具名，如 trendline、hline、fib（state.chartDrawings.tool 是当前工具）",
  "chart-drawing-style": "要带 id：线的 id（state.chartDrawings.items）",
  "chart-drawing-settings": "要带 id：线的 id（state.chartDrawings.items）",
  "chart-context-menu": "要带 id：main / drawing:<线 id> / pane:<kd|macd|rsi|oi>，可加 /draw 等子菜单",
  "strategy-editor-section": "要带 id：basics / entry / exit / sizing / flow",
  "strategy-editor-condition": "要带 id：<entryLong|entryShort|exitLong|exitShort>:<第几条>",
  "strategy-editor-picker": "要带 id：<entryLong|entryShort|exitLong|exitShort>:<第几条>:<left|right>",
  "help-tip": "要带 id：说明的 key（state.disclosures 里 help-tip:<key>）",
  "mobile-chart-cell": "要带 id：多图格子序号 0–5",
  "mobile-drawing-style-row": "要带 id：color / width / dash",
};
/** Disclosures with many instances on one screen (every "?", every condition side, every translated
 * row's / report's 原文 fold — a 24-title batch already exceeds the cap): all reported, not only the last 20. */
const UNCAPPED_DISCLOSURES = new Set(["help-tip", "strategy-editor-picker", "strategy-actions", "strategy-delete-confirm",
  "flash-orig", "news-orig"]);
/** Old disclosure names a command may still use: the designer's 查看完整规则 became its 高级 fold. */
export const DISCLOSURE_ALIASES: Record<string, string> = { "strategy-full-rules": "strategy-advanced",
  // 画线第二版 (2026-10-09): the first version's 画线 popover is the left toolbar now.
  "chart-drawing-tools": "chart-drawing-toolbar" };
const CARD_STATES_PER_NAME = 20;

/** Registry key of the most recently mounted card disclosure with this name, if any. */
export function latestCardDisclosure(name: string): string | undefined {
  let best: string | undefined;
  surfaces.forEach((_, key) => {
    if (key.startsWith(`disclosure:${name}:`) && (best === undefined || (firstSeen.get(key) ?? 0) > (firstSeen.get(best) ?? 0))) best = key;
  });
  return best;
}

/**
 * `UiState.disclosures`: every mounted disclosure by name; card disclosures report the
 * most recent card under the bare name and the last few cards as `name:id`.
 */
export function disclosureStates(): Record<string, boolean> {
  const result: Record<string, boolean> = {};
  const cards = new Map<string, { key: string; order: number }[]>();
  surfaces.forEach((surface, key) => {
    if (key.startsWith("family:")) {
      const name = key.slice("family:".length);
      const open = Array.isArray(surface.state) ? surface.state as string[] : [];
      result[name] = open.length > 0;
      open.forEach(id => { result[`${name}:${id}`] = true; });
      return;
    }
    if (!key.startsWith("disclosure:")) return;
    const [, name, ...rest] = key.split(":");
    if (!rest.length) { result[name] = Boolean(surface.state); return; }
    const list = cards.get(name) ?? [];
    list.push({ key, order: firstSeen.get(key) ?? 0 });
    cards.set(name, list);
  });
  cards.forEach((list, name) => {
    list.sort((a, b) => b.order - a.order);
    result[name] = Boolean(surfaces.get(list[0].key)?.state);
    (UNCAPPED_DISCLOSURES.has(name) ? list : list.slice(0, CARD_STATES_PER_NAME))
      .forEach(({ key }) => { result[key.slice("disclosure:".length)] = Boolean(surfaces.get(key)?.state); });
  });
  return result;
}

/**
 * A `<details>` or boolean toggle as a commandable surface (`disclosure: {name, open}`).
 * Registers only while `available` (the control is rendered); Card disclosures pass
 * their card `id`. A component that already owns the state passes it as `state` and
 * handles the command in `apply`; the hook then reports that state, not its own.
 */
export function useDisclosureSurface(name: string, options: { initial?: boolean; id?: string; available?: boolean; state?: boolean; apply?: (open: boolean) => void } = {}):
  [boolean, Dispatch<SetStateAction<boolean>>] {
  const { initial = false, id, available = true } = options;
  const [internal, setOpen] = useState(initial);
  const open = options.state ?? internal;
  const latest = useRef(options.apply);
  latest.current = options.apply;
  const key = id ? `disclosure:${name}:${id}` : `disclosure:${name}`;
  useEffect(() => {
    if (!available) return;
    return registerUiSurface(key, { state: open, apply: value => { const wanted = Boolean(value); if (latest.current) latest.current(wanted); else setOpen(wanted); } });
  }, [key, open, available]);
  return [open, setOpen];
}

/**
 * 画线第二版: a disclosure family (SPARSE_DISCLOSURES) — many ids, one registration. `open` lists the
 * ids shown now; a command `{name, id, open}` reaches `apply(id, open)`, which throws a readable
 * reason for an id that does not exist.
 */
export function useDisclosureFamily(name: string, options: { open: string[]; available?: boolean; apply: (id: string, open: boolean) => void }) {
  const { open, available = true } = options;
  const latest = useRef(options.apply);
  latest.current = options.apply;
  const key = open.join("\n");
  useEffect(() => {
    if (!available) return;
    return registerUiSurface(`family:${name}`, { state: key ? key.split("\n") : [], apply: value => {
      const { id, open: wanted } = value as { id: string; open: boolean };
      latest.current(id, Boolean(wanted));
    } });
  }, [name, key, available]);
}

/**
 * A native `<dialog>` as a commandable surface: reports `open` from the element
 * and opens/closes it on command. `open` runs the component's own opener when
 * the dialog needs state (a help topic, a pending request) instead of showModal.
 */
export function useDialogSurface(name: string, ref: RefObject<HTMLDialogElement | null>,
  handlers: { open?: (topic?: string) => void; close?: () => void; closeOnly?: boolean } = {}) {
  const [open, setOpen] = useState(false);
  const latest = useRef(handlers);
  latest.current = handlers;
  const attached = useRef<{ element: HTMLDialogElement; detach: () => void }>(null);
  // The dialog element may mount later than the hook (portals behind loaded data): attach whenever it
  // changes. Runs after every render; the identity check above `setOpen` keeps it from looping.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const element = ref.current;
    if (attached.current?.element === element) return;
    attached.current?.detach();
    attached.current = null;
    if (!element) { setOpen(false); return; }
    const sync = () => setOpen(element.hasAttribute("open"));
    const observer = new MutationObserver(sync);
    observer.observe(element, { attributes: true, attributeFilter: ["open"] });
    element.addEventListener("close", sync);
    sync();
    attached.current = { element, detach: () => { observer.disconnect(); element.removeEventListener("close", sync); } };
  });
  useEffect(() => () => { attached.current?.detach(); attached.current = null; }, []);
  useUiSurface(`dialog:${name}`, { state: { open }, apply: value => {
    const { open: wanted, topic } = value as { open: boolean; topic?: string };
    const { open: opener, close: closer, closeOnly } = latest.current;
    if (wanted) {
      if (closeOnly) throw new Error("该确认框只能由用户操作打开");
      if (opener) opener(topic); else ref.current?.showModal();
    } else if (closer) closer(); else ref.current?.close();
  } }, [open]);
  return open;
}
