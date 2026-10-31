import { useCallback, useEffect, useMemo, useState } from "react";
import { requestJson } from "../api";
import type { ChartDrawing, DrawingKind, DrawingPoints, DrawingStyle } from "../chartDrawingModel";
import { onFeedNudge } from "../runtimeFeed";
import { useIdentityFence, type FenceContext } from "./useIdentityFence";

export type { ChartDrawing } from "../chartDrawingModel";

/** 用户画线 (2026-10-02; 画线第二版 2026-10-09): the drawings the user made on the Runtime
 * (`GET/POST/PUT/DELETE /api/market/drawings` — the chart-drawings skill's catalog routes, not a
 * private surface). One handle per workspace: everything the user has on every contract loads with
 * the connection (≤200 per contract) and re-reads after each change — its own, and any the AI or
 * another device makes (the Runtime's `feed.ui` nudge with reason "drawings"). A move or a style
 * change shows at once; writes run in submission order, and a refusal removes only that edit while
 * keeping later edits over the last confirmed list. A guest never enables it, so the toolbar and
 * the chart layer simply do not exist there. A replay window (2026-10-03) keeps them — its Runtime
 * URL is the replay child's, so the lines live in that replay and vanish with it. Drawing is a
 * visual aid: it never places an order and never creates an alert. */

/** The refusal the Runtime sent (a Chinese sentence) or the transport error. */
function drawingErrorText(error: unknown): string {
  const detail = (error as { detail?: { detail?: unknown } }).detail?.detail;
  if (typeof detail === "string") return detail;
  return error instanceof Error && error.message ? error.message : "画线未保存";
}

export interface DrawingPatch { points?: DrawingPoints; label?: string; style?: Partial<Record<keyof DrawingStyle, unknown>> }

export interface ChartDrawingsHandle {
  /** False while disconnected or for a guest; true inside a replay (its own child Runtime). */
  available: boolean;
  /** Every contract's drawings; the chart layers filter by the displayed contract. */
  items: ChartDrawing[];
  /** The last create's refusal, shown beside the toolbar. */
  createError: string;
  /** The last receipt or refusal (cleared, refused move…), shown by the market bar for ~6 s. */
  notice: string;
  /** Resolves to the new drawing, or null when refused (createError says why). */
  create: (symbol: string, kind: DrawingKind, points: DrawingPoints, label?: string, style?: DrawingStyle) => Promise<ChartDrawing | null>;
  /** Points, label and/or a style patch (null returns a key to the default); applied at once. */
  update: (id: string, patch: DrawingPatch) => Promise<boolean>;
  remove: (id: string) => void;
  /** 全部删除: deletes only this contract's own drawings. */
  clear: (symbol: string) => void;
  refresh: () => void;
}

function patched(drawing: ChartDrawing, patch: DrawingPatch): ChartDrawing {
  const style: Record<string, unknown> = { ...(drawing.style ?? {}) };
  for (const [key, value] of Object.entries(patch.style ?? {})) {
    if (value === null) delete style[key]; else style[key] = value;
  }
  return { ...drawing, ...(patch.points ? { points: patch.points } : {}), ...(patch.label !== undefined ? { label: patch.label } : {}),
    style: style as DrawingStyle };
}

type PendingEdit = { id: string; patch: DrawingPatch } | { remove: string } | { unchanged: true };
interface DrawingView { context: FenceContext; items: ChartDrawing[]; createError: string; notice: string }

export function useChartDrawings(baseUrl: string | undefined, enabled: boolean): ChartDrawingsHandle {
  const fence = useIdentityFence([baseUrl, enabled]);
  // This hook owns one user's drawings across trading accounts. Login, Runtime, disconnect and
  // unmount retire its callbacks; a trading-account switch alone does not split the drawings.
  const owner = useMemo(() => ({ confirmed: [] as ChartDrawing[], pending: [] as PendingEdit[],
    queue: Promise.resolve(), channel: fence.channel("drawings") }), [fence]);
  const [view, setView] = useState<DrawingView>({ context: fence.context, items: [], createError: "", notice: "" });
  const currentView = view.context === fence.context ? view : { context: fence.context, items: [], createError: "", notice: "" };
  const { items, createError, notice } = currentView;
  const [readKey, setReadKey] = useState(0);

  const publish = useCallback((messages: Partial<Pick<DrawingView, "createError" | "notice">> = {}) => {
    if (!fence.isCurrent()) return;
    const projected = owner.pending.reduce((list, edit) => {
      if ("patch" in edit) return list.map(item => item.id === edit.id ? patched(item, edit.patch) : item);
      if ("remove" in edit) return list.filter(item => item.id !== edit.remove);
      return list;
    }, owner.confirmed);
    setView(previous => ({ context: fence.context, items: projected,
      createError: previous.context === fence.context ? previous.createError : "",
      notice: previous.context === fence.context ? previous.notice : "", ...messages }));
  }, [fence, owner]);

  const refresh = useCallback(() => { if (fence.isCurrent()) setReadKey(key => key + 1); }, [fence]);
  // Reuse the preferences' Promise-tail pattern. One queue also orders clear/delete against edits;
  // no GET lands during a write, and the final write re-reads changes made by another window.
  const enqueue = useCallback((edit: PendingEdit, write: () => Promise<void>) => {
    owner.pending.push(edit);
    owner.channel.noteWrite();
    publish();
    const run = owner.queue.then(async () => { if (fence.isCurrent()) await write(); }).finally(() => {
      owner.pending = owner.pending.filter(item => item !== edit);
      owner.channel.noteWrite();
      publish();
      if (!owner.pending.length) refresh();
    });
    owner.queue = run.catch(() => undefined);
    return run;
  }, [fence, owner, publish, refresh]);

  useEffect(() => {
    if (!baseUrl || !enabled || owner.pending.length) return;
    let cancelled = false;
    const current = owner.channel.beginRead();
    requestJson<{ drawings?: ChartDrawing[] }>(`${baseUrl}/api/market/drawings`)
      .then(result => {
        if (cancelled || !current() || owner.pending.length) return;
        if (result.drawings) owner.confirmed = result.drawings;
        publish();
      })
      .catch(() => undefined); // keep the last lines; the next action re-reads
    return () => { cancelled = true; };
  }, [baseUrl, enabled, readKey, owner, publish]);

  // The AI or another window changed a drawing: the Runtime nudges every window of the user.
  useEffect(() => (!baseUrl || !enabled) ? undefined : onFeedNudge(reason => { if (reason === "drawings") refresh(); }),
    [baseUrl, enabled, refresh]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => publish({ notice: "" }), 6000);
    return () => window.clearTimeout(timer);
  }, [notice, publish]);

  const create = useCallback(async (symbol: string, kind: DrawingKind, points: DrawingPoints, label?: string, style?: DrawingStyle) => {
    if (!baseUrl || !enabled || !fence.isCurrent()) { publish({ createError: "连接后可画线" }); return null; }
    let made: ChartDrawing | null = null;
    await enqueue({ unchanged: true }, async () => {
      try {
        const result = await requestJson<{ drawing: ChartDrawing }>(`${baseUrl}/api/market/drawings`, {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ symbol, kind, points, ...(label ? { label } : {}), ...(style && Object.keys(style).length ? { style } : {}) })
        });
        if (!fence.isCurrent()) return;
        const drawing = result.drawing ?? null;
        made = drawing;
        if (drawing) owner.confirmed = [...owner.confirmed.filter(item => item.id !== drawing.id), drawing];
        publish({ createError: "" });
      } catch (error) { publish({ createError: drawingErrorText(error) }); }
    });
    return made;
  }, [baseUrl, enabled, fence, owner, enqueue, publish]);

  const update = useCallback(async (id: string, patch: DrawingPatch) => {
    if (!baseUrl || !enabled || !fence.isCurrent()) return false;
    let saved = false;
    await enqueue({ id, patch }, async () => {
      try {
        const result = await requestJson<{ drawing: ChartDrawing; alertNotice?: string }>(`${baseUrl}/api/market/drawings/${id}`, {
          method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(patch)
        });
        if (!fence.isCurrent()) return;
        if (result.drawing) owner.confirmed = owner.confirmed.map(item => item.id === id ? result.drawing : item);
        // A moved line whose 碰到就提醒 could not follow it says why (the alert was cancelled).
        if (result.alertNotice) publish({ notice: result.alertNotice });
        saved = true;
      } catch (error) { publish({ notice: drawingErrorText(error) }); }
    });
    return saved;
  }, [baseUrl, enabled, fence, owner, enqueue, publish]);

  const remove = useCallback((id: string) => {
    if (!baseUrl || !enabled || !fence.isCurrent()) return;
    void enqueue({ remove: id }, async () => {
      try {
        await requestJson(`${baseUrl}/api/market/drawings/${id}`, { method: "DELETE" });
        if (fence.isCurrent()) owner.confirmed = owner.confirmed.filter(item => item.id !== id);
      } catch (error) { publish({ notice: drawingErrorText(error) }); }
    });
  }, [baseUrl, enabled, fence, owner, enqueue, publish]);

  const clear = useCallback((symbol: string) => {
    if (!baseUrl || !enabled || !fence.isCurrent()) return;
    void enqueue({ unchanged: true }, async () => {
      try {
        const result = await requestJson<{ cleared: number }>(`${baseUrl}/api/market/drawings?symbol=${encodeURIComponent(symbol)}`, { method: "DELETE" });
        if (!fence.isCurrent()) return;
        owner.confirmed = owner.confirmed.filter(item => item.symbol.toUpperCase() !== symbol.toUpperCase());
        publish({ notice: result.cleared > 0 ? `已删除本合约 ${result.cleared} 条画线` : "本合约没有画过线" });
      } catch (error) { publish({ notice: drawingErrorText(error) }); }
    });
  }, [baseUrl, enabled, fence, owner, enqueue, publish]);

  return {
    available: Boolean(baseUrl) && enabled,
    items, createError, notice, create, update, remove, clear, refresh
  };
}
