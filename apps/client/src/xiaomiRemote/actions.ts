import type { RemoteAction } from "./store";

/** These actions never synthesize an Enter key or click a trade/confirmation button. */
export function runRemoteAction(action: RemoteAction) {
  if (action === "chat") window.dispatchEvent(new CustomEvent("ai-trader-open-ai"));
  if (action === "settings") window.dispatchEvent(new CustomEvent("ai-trader-open-settings", { detail: "voice" }));
  const scope = document.querySelector("dialog[open]") ?? document;
  if (action === "focusNext" || action === "focusPrevious") {
    const controls = Array.from(scope.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex='0']"))
      .filter(element => element.getClientRects().length && !element.closest("[hidden], [inert]"));
    const at = controls.indexOf(document.activeElement as HTMLElement);
    const next = at < 0 ? (action === "focusNext" ? 0 : controls.length - 1)
      : (at + (action === "focusNext" ? 1 : controls.length - 1)) % controls.length;
    controls[next]?.focus();
  }
  if (action === "scrollUp" || action === "scrollDown") {
    let node = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    while (node && !(node.scrollHeight > node.clientHeight && /auto|scroll/.test(getComputedStyle(node).overflowY))) node = node.parentElement;
    const scroll = node ?? scope.querySelector<HTMLElement>(".app-settings-content") ?? document.scrollingElement;
    if (scroll) scroll.scrollTop += action === "scrollUp" ? -240 : 240;
  }
  if (action === "activate") {
    const active = document.activeElement;
    // Positive allowlist: no proposals, trades, destructive actions, links, forms or arbitrary buttons.
    if (active instanceof HTMLInputElement && ["checkbox", "radio"].includes(active.type)) active.click();
    else if (active instanceof HTMLButtonElement && active.getAttribute("role") === "switch" && !active.disabled) active.click();
  }
}
