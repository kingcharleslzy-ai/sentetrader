import { isTauriRuntime } from "./runtimeConnection";

/**
 * The desktop WebView cannot open tabs itself, so on Tauri every external link
 * is handed to the shell command that asks the system browser; the web build
 * keeps the ordinary new-tab behavior for web pages. Mail drafts use the
 * current browsing context so the user's click reaches their mail handler.
 */
export async function openExternal(url: string): Promise<void> {
  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_external_url", { url });
    return;
  }
  if (/^mailto:/i.test(url)) window.location.assign(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}

/** The absolute http(s) URL an anchor should leave the app with, or null when
 *  the click must keep its default in-app navigation. */
function externalAnchorUrl(anchor: HTMLAnchorElement): string | null {
  let resolved: URL;
  try {
    resolved = new URL(anchor.getAttribute("href") ?? "", window.location.href);
  } catch {
    return null;
  }
  if (resolved.protocol !== "https:" && resolved.protocol !== "http:") return null;
  if (anchor.target !== "_blank" && resolved.origin === window.location.origin) return null;
  return resolved.href;
}

/**
 * Desktop-only capture-phase click handler: plain `<a target="_blank">` and
 * cross-origin anchors (news sources, AI reply links, registration pages) open
 * in the system browser. A failed open only logs — the WebView has no fallback.
 */
export function installExternalLinkHandler(): void {
  if (!isTauriRuntime()) return;
  document.addEventListener("click", event => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const anchor = target.closest("a[href]");
    if (!(anchor instanceof HTMLAnchorElement)) return;
    const url = externalAnchorUrl(anchor);
    if (!url) return;
    event.preventDefault();
    openExternal(url).catch(error => console.warn("external link not opened", url, error));
  }, true);
}
