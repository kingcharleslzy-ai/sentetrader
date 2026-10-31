import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { readQueryClient } from "./runtimeClient";
import { App } from "./App";
import { configureServiceWorker, serviceWorkerRegistrar } from "./serviceWorkerPolicy";
import { installExternalLinkHandler } from "./externalLinks";
import { isTauriRuntime } from "./runtimeConnection";
import { isAdminPath } from "./adminRoute";
import "./styles.css";
import { applySavedTheme } from "./theme";
import { CLIENT_BUILD } from "./clientBuild";
import { listenForInstallPrompt } from "./components/mobile/installEnvironment";
import { startRum } from "./rum";

applySavedTheme();
installExternalLinkHandler();
// Android's install offer can fire before the app renders; the 安装到桌面 guide reads it later.
listenForInstallPrompt();
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applySavedTheme);

async function startApplication() {
  // The standalone 后台管理 page (/trader/admin, web only): a separate app, no service
  // worker, no UI-control window — dynamically imported so the trading entry chunk
  // stays free of the console.
  if (!isTauriRuntime() && isAdminPath(window.location.pathname)) {
    const { AdminApp } = await import("./AdminApp");
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <QueryClientProvider client={readQueryClient}><AdminApp /></QueryClientProvider>
    );
    return;
  }
  // 真实用户监测: anonymous minute metrics; they are sent once the workspace is signed in.
  startRum();
  // The web app's network-only worker (sw.js, scope = the app's base path); never in dev or the desktop app.
  const base = import.meta.env.BASE_URL || "/";
  // Optional enhancement: it never throws, and a call that never settles cannot hold the first render.
  await Promise.race([
    configureServiceWorker(serviceWorkerRegistrar(`${base}sw.js`, base)),
    new Promise(resolve => setTimeout(resolve, 3000)),
  ]);
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <QueryClientProvider client={readQueryClient}><App /></QueryClientProvider>
  );
  // The desktop shell watches for this: a window whose frontend never runs updates itself.
  if ("__TAURI_INTERNALS__" in window) {
    import("@tauri-apps/api/core").then(({ invoke }) => {
      void invoke("desktop_frontend_ready", { build: CLIENT_BUILD.commit });
      // Environment facts for the desktop log: whether WebKit treats this page as a secure context
      // (getUserMedia needs it) and which WebKit this is.
      void invoke("desktop_frontend_error", { error: { kind: "info", message: `isSecureContext=${window.isSecureContext} mediaDevices=${Boolean(navigator.mediaDevices?.getUserMedia)} origin=${window.location.origin}`, detail: navigator.userAgent } });
    }).catch(() => undefined);
  }
}

void startApplication();
