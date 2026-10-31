import { X } from "lucide-react";
import { useState } from "react";
import { checkWebUpdateNow, dismissWebUpdateBanner, useWebUpdate, webPage } from "../clientBuild";
import "./WebUpdate.css";

/**
 * 新版本已就绪 · 刷新 (web, 2026-10-05): shown while a newer build is deployed and the page could
 * not reload itself (something was in progress). 刷新 reloads now; 关闭 hides it for this build —
 * the next return to the foreground while idle still reloads. Phone: atop the content; desktop: a
 * row under the header.
 */
export function WebUpdateBanner() {
  const update = useWebUpdate();
  if (!update.banner) return null;
  return <div className="web-update-bar" role="status" data-agent-target="web-update-banner">
    <span>新版本已就绪</span><span aria-hidden="true">·</span>
    <button type="button" className="web-update-reload" onClick={() => webPage.reload()}>刷新</button>
    <button type="button" className="web-update-close" aria-label="关闭新版本提示" onClick={dismissWebUpdateBanner}><X aria-hidden="true" /></button>
  </div>;
}

export type WebUpdateCheckStatus = "" | "checking" | "current" | "failed";
const CHECK_NOTICES: Partial<Record<WebUpdateCheckStatus, string>> = { current: "已是最新", failed: "检查失败，请稍后再试" };

/** 检查更新: reads version.json now — a newer build reloads the page, otherwise 已是最新. */
export function useWebUpdateCheck(): { label: string; busy: boolean; notice: string; check: () => void } {
  const [status, setStatus] = useState<WebUpdateCheckStatus>("");
  const check = () => {
    setStatus("checking");
    void checkWebUpdateNow().then(result => setStatus(result === "reloading" ? "checking" : result));
  };
  return { label: status === "checking" ? "检查中…" : "检查更新", busy: status === "checking", notice: CHECK_NOTICES[status] ?? "", check };
}
