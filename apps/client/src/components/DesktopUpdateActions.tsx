import type { DesktopUpdaterController } from "../hooks/useDesktopUpdater";
import { DesktopUpdateProgress } from "./DesktopUpdateProgress";

export function DesktopUpdateActions({ updater, className }: { updater: DesktopUpdaterController; className: string }) {
  const busy = ["checking", "downloading", "installing"].includes(updater.phase);
  const canInstall = updater.info?.available && ["available", "error", "downloading", "installing"].includes(updater.phase);
  return <>
    {canInstall ? <button type="button" className={className} disabled={busy} onClick={() => void updater.install()}>
      {updater.phase === "downloading" ? "正在下载更新…" : updater.phase === "installing" ? "正在安装更新…"
        : `${updater.phase === "error" ? "重试下载" : "下载并安装"} ${updater.info?.version ?? "新版本"}`}
    </button> : null}
    <button type="button" className={`${className} secondary`} disabled={busy} onClick={() => void updater.check()}>
      {updater.phase === "checking" ? "正在检查更新…" : "重新检查更新"}
    </button>
    {updater.message ? <p role="status">{updater.message}</p> : null}
    <DesktopUpdateProgress updater={updater} />
  </>;
}
