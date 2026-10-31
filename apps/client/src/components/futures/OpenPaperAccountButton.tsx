import { useState } from "react";
import { openPaperAccount } from "../../api";
import { Button } from "../settings/controls";

/**
 * 开通本站模拟 (2026-10-03; 2026-10-08 moved into 设置 → 账户 → 交易账户's 模拟盘 side): opens the
 * user's one site-hosted paper account (1,000,000 to start) — only someone without one sees it
 * (the owner, who is never auto-provisioned). Opening never changes the default account; the
 * refreshed directory then offers it in the 交易账户 select.
 */
export function OpenPaperAccountButton({ baseUrl, onOpened }: {
  baseUrl: string | undefined; onOpened: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const open = async () => {
    if (busy || !baseUrl) return;
    setBusy(true);
    setError(undefined);
    try {
      await openPaperAccount(baseUrl);
      await onOpened();
    } catch {
      setError("开通未完成，请稍后重试");
    } finally {
      setBusy(false);
    }
  };
  return <Button testId="settings:open-paper" disabled={busy || !baseUrl} status={error} onClick={() => void open()}>
    {busy ? "正在开通…" : "开通本站模拟"}
  </Button>;
}
