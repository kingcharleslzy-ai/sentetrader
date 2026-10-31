import { useEffect, useRef, useState } from "react";
import type { DesktopUpdaterController } from "../hooks/useDesktopUpdater";
import { DesktopUpdateProgress } from "./DesktopUpdateProgress";
import { useDialogSurface } from "../uiSurfaces";

/** ISO pubDate → 2026-09-23; anything unparsable stays hidden instead of lying. */
function publishedDate(pubDate: string | undefined): string | undefined {
  if (!pubDate) return undefined;
  const date = new Date(pubDate);
  if (Number.isNaN(date.getTime())) return undefined;
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

/**
 * The new-version popup (desktop only): opens by itself when the updater offers a
 * version this run has not snoozed. 稍后 (or Esc) silences that version until restart
 * or a newer one. Downloading/installing stays the protected flow the settings row
 * already has — closing the popup mid-download leaves the progress visible there.
 */
export function DesktopUpdateDialog({ updater }: { updater: DesktopUpdaterController }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  // One-run memory: the version the user snoozed, and the one this dialog already
  // opened for (pausing a download flips the phase back to available — no re-pop).
  const snoozedVersion = useRef<string | undefined>(undefined);
  const openedForVersion = useRef<string | undefined>(undefined);
  const version = updater.info?.version;

  useEffect(() => {
    if (updater.phase !== "available" || !version) return;
    if (version === snoozedVersion.current || version === openedForVersion.current) return;
    openedForVersion.current = version;
    setOpen(true);
  }, [updater.phase, version]);

  useEffect(() => { if (open) ref.current?.showModal(); else ref.current?.close(); }, [open]);

  const later = () => {
    if (version) snoozedVersion.current = version;
    setOpen(false);
  };

  // `dialog: update-dialog` opens and closes only the window; the download and the
  // installation remain user actions an agent command cannot reach.
  useDialogSurface("update-dialog", ref, {
    open: () => {
      if (updater.phase !== "available" || !updater.info?.version) throw new Error("当前没有可用的新版本");
      openedForVersion.current = updater.info.version;
      setOpen(true);
    },
    close: later,
  });

  const busy = updater.phase === "checking" || updater.phase === "downloading" || updater.phase === "installing";
  const canInstall = Boolean(updater.info?.available) && (updater.phase === "available" || updater.phase === "error");
  const published = publishedDate(updater.info?.pubDate);
  return (
    <dialog ref={ref} className="replay-setup desktop-update-dialog" aria-labelledby="desktop-update-title" data-testid="desktop-update-dialog"
      onCancel={(event) => { event.preventDefault(); later(); }}>
      <div className="replay-setup-body">
        <h2 id="desktop-update-title">发现新版本 {version}</h2>
        <p className="desktop-update-meta">当前版本 {updater.info?.currentVersion}{published ? ` · 发布于 ${published}` : ""}</p>
        {updater.info?.notes ? <div className="desktop-update-notes">{updater.info.notes}</div> : null}
        {updater.phase === "available" && updater.message ? <p role="status">{updater.message}</p> : null}
        {busy ? <DesktopUpdateProgress updater={updater} /> : null}
        {updater.phase === "error" ? <p role="alert">{updater.message ?? "更新失败"}</p> : null}
        <div className="confirm-grid">
          <button type="button" onClick={later}>稍后</button>
          {updater.phase === "error" ? (
            <button type="button" onClick={() => void updater.install()}>重试下载</button>
          ) : canInstall ? (
            <button type="button" disabled={busy} onClick={() => void updater.install()}>立即更新 {version}</button>
          ) : null}
        </div>
      </div>
    </dialog>
  );
}
