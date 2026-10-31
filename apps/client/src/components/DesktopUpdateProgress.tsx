import type { DesktopUpdaterController } from "../hooks/useDesktopUpdater";

export function desktopUpdateStatusLabel(updater: DesktopUpdaterController): string {
  if (updater.phase === "checking") return "正在检查更新";
  if (updater.phase === "downloading") {
    return updater.progress?.percent == null ? "正在下载更新" : `正在下载 ${updater.progress.percent}%`;
  }
  if (updater.phase === "installing") return "下载完成，正在安装";
  if (updater.phase === "current") return `已是最新 ${updater.info?.currentVersion ?? ""}`.trim();
  if (updater.phase === "available") return `可更新 ${updater.info?.version ?? ""}`.trim();
  if (updater.phase === "error") return updater.message ?? "更新失败";
  return "当前环境不支持自动更新";
}

export function DesktopUpdateProgress({ updater }: { updater: DesktopUpdaterController }) {
  if (updater.phase !== "checking" && updater.phase !== "downloading" && updater.phase !== "installing") return null;
  const indeterminate = updater.phase === "checking" || (updater.phase === "downloading" && updater.progress?.percent == null);
  const value = updater.phase === "installing" ? 100 : updater.progress?.percent;
  const label = desktopUpdateStatusLabel(updater);
  return (
    <div className="desktop-update-progress" aria-live="polite">
      <div className="desktop-update-progress-label"><span>{label}</span>{value != null ? <strong>{value}%</strong> : null}</div>
      {updater.phase === "downloading" ? <div className="desktop-update-progress-label">
        <span>{updater.progress?.incremental ? "增量更新 · " : ""}{updater.progress?.source ? `${updater.progress.source}线路 · ` : ""}
          {updater.progress?.bytesPerSecond ? `${(updater.progress.bytesPerSecond / 1024 / 1024).toFixed(2)} MB/s` : "正在选择下载线路…"}
          {updater.progress?.remainingSeconds != null ? ` · 约剩 ${Math.max(1, Math.ceil(updater.progress.remainingSeconds))} 秒` : ""}
        </span>
        {updater.cancel ? <button type="button" onClick={() => void updater.cancel?.()}>暂停下载</button> : null}
      </div> : null}
      <div
        className={`desktop-update-progress-track${indeterminate ? " indeterminate" : ""}`}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : value}
      >
        <span style={indeterminate ? undefined : { width: `${value ?? 0}%` }} />
      </div>
    </div>
  );
}
