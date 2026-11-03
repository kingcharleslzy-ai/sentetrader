import { useEffect, useState } from "react";
import { requestJson } from "../api";
import type { AccountPnlData } from "../accountPnl";
import { feedReportServes, REPORT_RECHECK_MS, reportKey, useFeedReport } from "../feedView";

/** Account-wide P&L from `/api/account/pnl`, every 5 s while `enabled`. The session's own account's comes over the push
 *  (通用报告推送 2026-11-03); the read runs only while the push does not serve it (the USD sub-account, an older Runtime). */
export function useAccountPnl({ baseUrl, accountId, enabled = true }: { baseUrl: string | undefined; accountId?: string | null; enabled?: boolean }) {
  const [result, setResult] = useState<{ baseUrl: string; accountId?: string | null; pnl?: AccountPnlData; error: string }>();
  const report = enabled && accountId ? reportKey("account-pnl", { account: accountId }) : undefined;
  const pushed = useFeedReport<AccountPnlData>(baseUrl, report);
  useEffect(() => {
    setResult(undefined);
    // null waits for the live account; omitted keeps existing replay/single-account reads.
    if (!baseUrl || !enabled || accountId === null) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (feedReportServes(baseUrl, report)) { timer = setTimeout(poll, REPORT_RECHECK_MS); return; }
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
  }, [baseUrl, accountId, enabled]); // eslint-disable-line react-hooks/exhaustive-deps
  // After the reset above, so a report already pushed for the new account shows at once.
  useEffect(() => { if (pushed && baseUrl) setResult({ baseUrl, accountId, pnl: pushed, error: "" }); }, [pushed, report]); // eslint-disable-line react-hooks/exhaustive-deps
  const current = enabled && accountId !== null && result?.baseUrl === baseUrl
    && result?.accountId === accountId ? result : undefined;
  return { pnl: current?.pnl, error: current?.error ?? "" };
}
