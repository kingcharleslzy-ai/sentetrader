import { useCallback, useEffect, useRef, useState } from "react";

/** After the startup check, look again every 6 h; the update dialog re-offers a newer version then. */
const RECHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

export interface DesktopUpdateInfo {
  currentVersion: string;
  available: boolean;
  version?: string;
  notes?: string;
  pubDate?: string;
}

export type DesktopUpdatePhase = "unsupported" | "checking" | "current" | "available" | "downloading" | "installing" | "error";

export interface DesktopUpdateProgressInfo {
  stage: "downloading" | "installing";
  downloadedBytes: number;
  totalBytes?: number;
  percent?: number;
  bytesPerSecond?: number;
  remainingSeconds?: number;
  source?: string;
  incremental?: boolean;
}

export interface DesktopUpdaterController {
  phase: DesktopUpdatePhase;
  info?: DesktopUpdateInfo;
  progress?: DesktopUpdateProgressInfo;
  message?: string;
  check: (options?: { silent?: boolean }) => Promise<void>;
  install: () => Promise<void>;
  cancel?: () => Promise<void>;
}

function updateErrorMessage(error: unknown): string {
  const code = typeof error === "string" ? error : error instanceof Error ? error.message : "";
  if (code.includes("desktop_update_already_running")) return "更新已经在进行中";
  if (code.includes("desktop_update_not_available")) return "当前已经是最新版本";
  if (code.includes("desktop_update_signature_invalid")) return "更新包校验失败，请重新下载";
  if (code.includes("desktop_update_cache_unavailable")) return "无法保存更新包，请检查磁盘空间";
  if (code.includes("desktop_update_download_failed")) return "更新包下载失败，请稍后重试";
  if (code.includes("desktop_update_install_failed")) return "更新包下载或安装失败，请稍后重试";
  if (code.includes("desktop_update_version_changed")) return "更新版本已变化，请重新检查并确认";
  if (code.includes("desktop_update_configuration_invalid")) return "应用更新配置无效";
  return "暂时无法连接更新服务";
}

export function useDesktopUpdater(enabled: boolean): DesktopUpdaterController {
  const [phase, setPhase] = useState<DesktopUpdatePhase>(enabled ? "checking" : "unsupported");
  const [info, setInfo] = useState<DesktopUpdateInfo>();
  const [progress, setProgress] = useState<DesktopUpdateProgressInfo>();
  const [message, setMessage] = useState<string>();
  // The periodic check reads the live phase; a stale closure would restart a check mid-download.
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  const check = useCallback(async (options?: { silent?: boolean }) => {
    if (!enabled) return;
    // A silent (periodic) check never flips the UI into "checking" and a failure keeps
    // whatever the user already sees — the offered update or the current-version state.
    const silent = options?.silent === true;
    if (!silent) {
      setPhase("checking");
      setProgress(undefined);
      setMessage(undefined);
    }
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const result = await invoke<DesktopUpdateInfo>("check_desktop_update");
      setInfo(result);
      setPhase(result.available ? "available" : "current");
      setMessage(undefined);
    } catch (error) {
      if (silent) return;
      setPhase("error");
      setMessage(updateErrorMessage(error));
    }
  }, [enabled]);

  const install = useCallback(async () => {
    if (!enabled) return;
    const expectedVersion = info?.version;
    if (!expectedVersion) {
      setPhase("error");
      setMessage("更新版本已变化，请重新检查并确认");
      return;
    }
    setPhase("downloading");
    setProgress({ stage: "downloading", downloadedBytes: 0, percent: 0 });
    setMessage(undefined);
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("install_desktop_update", { expectedVersion });
    } catch (error) {
      if (String(error).includes("desktop_update_cancelled")) {
        setPhase("available");
        setMessage("下载已暂停，再次下载可继续");
      } else {
        setPhase("error");
        setMessage(updateErrorMessage(error));
      }
    }
  }, [enabled, info?.version]);

  const cancel = useCallback(async () => {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("cancel_desktop_update");
  }, []);

  useEffect(() => {
    if (!enabled) {
      setPhase("unsupported");
      return;
    }
    // Test builds (VITE_TEST_BUILD=1) are for local verification only; the
    // released channel always outranks a -dev semver and would nag forever.
    if (import.meta.env.VITE_TEST_BUILD === "1") {
      setPhase("unsupported");
      setMessage("测试版不检查正式更新");
      return;
    }
    const timer = window.setTimeout(() => void check(), 1_200);
    // Every 6 h a silent re-check; only from a settled phase — never interrupts a
    // download or installation the user already started.
    const interval = window.setInterval(() => {
      const current = phaseRef.current;
      if (current !== "current" && current !== "available" && current !== "error") return;
      void check({ silent: true });
    }, RECHECK_INTERVAL_MS);
    return () => {
      window.clearTimeout(timer);
      window.clearInterval(interval);
    };
  }, [check, enabled]);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void import("@tauri-apps/api/event")
      .then(({ listen }) => listen<DesktopUpdateProgressInfo>("desktop-update-progress", (event) => {
        if (disposed) return;
        const payload = event.payload;
        const percent = payload.percent == null
          ? undefined
          : Math.max(0, Math.min(100, Math.round(payload.percent)));
        setProgress({
          ...payload,
          stage: payload.stage,
          downloadedBytes: Math.max(0, payload.downloadedBytes),
          totalBytes: payload.totalBytes,
          percent
        });
        setPhase(payload.stage);
      }))
      .then((cleanup) => {
        if (disposed) cleanup();
        else unlisten = cleanup;
      })
      .catch(() => undefined);
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [enabled]);

  return { phase, info, progress, message, check, install, cancel };
}
