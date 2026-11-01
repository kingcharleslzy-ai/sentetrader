import { useEffect, useRef, useState, useSyncExternalStore, type Dispatch, type RefObject, type SetStateAction } from "react";
import surfaceTable from "../../../services/trader-runtime/src/trader_runtime/ui_surfaces.json";

/**
 * 界面表面一张表 (owner 2026-11-01): every view, panel, page, tab, dialog and disclosure id a UI command can name,
 * with its old aliases, labels and guest flag, is the Runtime's ui_surfaces.json (its "about" says what each field
 * means) — read here and by the Runtime, so a surface is added in that one file.
 */
type SurfaceTable = typeof surfaceTable;
export type SurfaceFamily = Exclude<keyof SurfaceTable, "about" | "guest">;
interface SurfaceEntry { label?: string; aliasOf?: string; stateOnly?: boolean; guest?: boolean; closeOnly?: boolean;
  card?: boolean; sparse?: boolean; idRequired?: string; uncapped?: boolean }
const families = surfaceTable as unknown as Record<SurfaceFamily, Record<string, SurfaceEntry>>;
/** A family's ids a command may name now (no old alias, nothing only an older client reports), as a type. */
export type CurrentSurfaceId<F extends SurfaceFamily> = { [K in keyof SurfaceTable[F]]:
  SurfaceTable[F][K] extends { aliasOf: string } | { stateOnly: boolean } ? never : K }[keyof SurfaceTable[F]] & string;
export const currentSurfaceIds = <F extends SurfaceFamily>(family: F) =>
  Object.keys(families[family]).filter(name => !families[family][name].aliasOf && !families[family][name].stateOnly) as CurrentSurfaceId<F>[];
/** The ids whose entry sets `flag` (guest, card, sparse …). */
export const surfacesWith = (family: SurfaceFamily, flag: keyof SurfaceEntry) =>
  new Set(Object.keys(families[family]).filter(name => families[family][name][flag]));
/** Old id → the id it stands for now. */
export const surfaceAliases = (family: SurfaceFamily): Record<string, string> =>
  Object.fromEntries(Object.entries(families[family]).flatMap(([name, entry]) => entry.aliasOf ? [[name, entry.aliasOf]] : []));
/** The id an old alias stands for now (any other value as it is). */
export const currentSurface = <F extends SurfaceFamily, T extends string>(family: F, value: T) =>
  (families[family][value]?.aliasOf ?? value) as Extract<T, CurrentSurfaceId<F>>;
export const surfaceLabel = (family: SurfaceFamily, id: string) => families[family][id]?.label ?? id;
/** What a visitor's window may be told to change (the Runtime's guest.py checks the same). */
export const GUEST_SURFACES = surfaceTable.guest;

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
export const CARD_DISCLOSURES = surfacesWith("disclosures", "card");
/**
 * Disclosures with one entry per drawing, tool, tool group or right-click menu: each is one registered family
 * (`family:<name>`) whose state is the list of open ids; the snapshot reports only the open ones (`name:id`), so a
 * missing entry is closed — the Runtime judges it so too.
 */
export const SPARSE_DISCLOSURES = surfacesWith("disclosures", "sparse");
/** Card disclosures a command must name by `id` (there is no "most recent" one to fall back on), with the hint. */
export const ID_REQUIRED_DISCLOSURES: Record<string, string> = Object.fromEntries(Object.entries(families.disclosures)
  .flatMap(([name, entry]) => entry.idRequired ? [[name, entry.idRequired]] : []));
/** Disclosures with many instances on one screen (every "?", every condition side, every translated row's 原文
 * fold): all reported, not only the last 20. */
const UNCAPPED_DISCLOSURES = surfacesWith("disclosures", "uncapped");
/** Old disclosure names a command may still use, and the surface the client opens for them. */
export const DISCLOSURE_ALIASES = surfaceAliases("disclosures");
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
