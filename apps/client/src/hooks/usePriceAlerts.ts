import { useCallback, useEffect, useState } from "react";
import { requestJson } from "../api";
import { onFeedNudge, onPriceAlert } from "../runtimeFeed";

/** 到价提醒 (2026-10-02): the user's own alerts on the Runtime (`GET/POST/DELETE
 * /api/market/alerts` — the price-alerts skill's catalog routes, not a private
 * surface). One handle per workspace: the lists load with the connection, refresh
 * when the Runtime pushes a crossing (`feed.price-alert` reaches every subscribed
 * window of the user's) and after each create or cancel. A guest never enables it,
 * so the bell and the chart lines simply do not exist there. A replay window
 * (2026-10-03) keeps both — its Runtime URL is the replay child's, so the alerts
 * watch the replay market and vanish with the replay; alerts only ever remind,
 * they never place an order. */
export interface PriceAlert {
  id: string;
  symbol: string;
  name: string;
  direction: "above" | "below";
  /** The Runtime's price text — already on the tick grid, no trailing .0. */
  price: string;
  note: string;
  status: string;
  via: string;
  sessionId: string | null;
  createdAt: string;
  triggeredAt: string | null;
  triggerPrice: string | null;
  /** 碰到这条线就提醒 (改版 2026-10-31): the user's drawing this alert watches (its price then is where the line was
   *  when set; a sloped line is watched where it is). Absent on an older Runtime. */
  drawingId?: string | null;
}

interface AlertsDocument { active: PriceAlert[]; triggered: PriceAlert[] }

/** The refusal the Runtime sent (a Chinese sentence) or the transport error. */
function alertErrorText(error: unknown): string {
  const detail = (error as { detail?: { detail?: unknown } }).detail?.detail;
  if (typeof detail === "string") return detail;
  return error instanceof Error && error.message ? error.message : "提醒设置未保存";
}

export function alertLine(alert: PriceAlert): string {
  if (alert.drawingId) return `${alert.name} 碰到画的线 提醒${alert.note ? `，备注：${alert.note}` : ""}`;
  const verb = alert.direction === "above" ? "涨到" : "跌到";
  return `${alert.name} ${verb} ${alert.price} 提醒${alert.note ? `，备注：${alert.note}` : ""}`;
}

export interface PriceAlertsHandle {
  /** False while disconnected or for a guest; true inside a replay (its own child Runtime). */
  available: boolean;
  active: PriceAlert[];
  /** Newest crossing first (the Runtime caps the history at 20). */
  triggered: PriceAlert[];
  /** The last create's refusal, shown inline under the popover's form. */
  createError: string;
  /** The last create/cancel receipt, shown by the market bar for ~6 s. */
  notice: string;
  /** `direction` is deliberately not sent: the Runtime judges it against the live quote. With `drawingId` the alert
   *  watches that drawing (碰到这条线就提醒): the Runtime takes the price from the line. */
  create: (symbol: string, price: number, note?: string, drawingId?: string) => Promise<boolean>;
  cancel: (id: string) => void;
  refresh: () => void;
}

export function usePriceAlerts(baseUrl: string | undefined, enabled: boolean): PriceAlertsHandle {
  const [doc, setDoc] = useState<AlertsDocument>();
  const [createError, setCreateError] = useState("");
  const [notice, setNotice] = useState("");
  const [readKey, setReadKey] = useState(0);

  useEffect(() => {
    if (!baseUrl || !enabled) { setDoc(undefined); return; }
    let cancelled = false;
    requestJson<AlertsDocument>(`${baseUrl}/api/market/alerts`)
      .then(result => { if (!cancelled) setDoc(result); })
      .catch(() => undefined); // keep the last lists; the next feed event or action re-reads
    return () => { cancelled = true; };
  }, [baseUrl, enabled, readKey]);

  const refresh = useCallback(() => setReadKey(key => key + 1), []);
  useEffect(() => (!baseUrl || !enabled) ? undefined : onPriceAlert(() => refresh()), [baseUrl, enabled, refresh]);
  // A drawing deleted or moved (here, by the AI or on another device) takes or re-arms its line alert.
  useEffect(() => (!baseUrl || !enabled) ? undefined : onFeedNudge(reason => { if (reason === "drawings") refresh(); }),
    [baseUrl, enabled, refresh]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const create = useCallback(async (symbol: string, price: number, note?: string, drawingId?: string) => {
    if (!baseUrl) { setCreateError("连接后可设置"); return false; }
    try {
      const result = await requestJson<{ alert: PriceAlert }>(`${baseUrl}/api/market/alerts`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ symbol, via: "ui", ...(drawingId ? { drawingId } : { price }), ...(note ? { note } : {}) })
      });
      setCreateError("");
      setNotice(`已设${alertLine(result.alert)}`);
      refresh();
      return true;
    } catch (error) {
      // The market bar says it too: a chart right-click or a line's 提醒 has no form to show the reason in.
      setCreateError(alertErrorText(error));
      setNotice(alertErrorText(error));
      return false;
    }
  }, [baseUrl, refresh]);

  const cancel = useCallback((id: string) => {
    if (!baseUrl) return;
    requestJson<{ alert: PriceAlert }>(`${baseUrl}/api/market/alerts/${id}`, { method: "DELETE" })
      .then(result => { setNotice(`已删除${alertLine(result.alert)}`); refresh(); })
      .catch(error => setNotice(alertErrorText(error)));
  }, [baseUrl, refresh]);

  return {
    available: Boolean(baseUrl) && enabled,
    active: doc?.active ?? [],
    triggered: doc?.triggered ?? [],
    createError, notice, create, cancel, refresh
  };
}
