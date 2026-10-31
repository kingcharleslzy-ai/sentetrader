import { useEffect, useState } from "react";
import { requestJson } from "../api";
import type { AccountPnlData } from "../accountPnl";

/** Account-wide P&L from `/api/account/pnl`, polled every 5 s while `enabled`. */
export function useAccountPnl({ baseUrl, accountId, enabled = true }: { baseUrl: string | undefined; accountId?: string | null; enabled?: boolean }) {
  const [result, setResult] = useState<{ baseUrl: string; accountId?: string | null; pnl?: AccountPnlData; error: string }>();
  useEffect(() => {
    setResult(undefined);
    // null waits for the live account; omitted keeps existing replay/single-account reads.
    if (!baseUrl || !enabled || accountId === null) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await requestJson<AccountPnlData>(`${baseUrl}/api/account/pnl`, {
          headers: accountId === undefined ? undefined : { "X-AI-Trader-Account": accountId }
        });
        if (!cancelled) setResult({ baseUrl, accountId, pnl: next, error: "" });
      }
      catch { if (!cancelled) setResult(previous => ({ baseUrl, accountId, pnl: previous?.pnl, error: "账户盈亏暂不可用" })); }
      finally { if (!cancelled) timer = setTimeout(poll, 5000); }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [baseUrl, accountId, enabled]);
  const current = enabled && accountId !== null && result?.baseUrl === baseUrl
    && result?.accountId === accountId ? result : undefined;
  return { pnl: current?.pnl, error: current?.error ?? "" };
}
