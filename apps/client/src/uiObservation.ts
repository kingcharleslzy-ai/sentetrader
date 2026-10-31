import { HIGHLIGHT_TARGET_SELECTOR, highlightTargetId } from "./aiAnnotations";
import type { components } from "./workbench.generated";

const excluded = "[data-agent-private], input, textarea, [contenteditable]:not([contenteditable=false]), pre, code, script, style, [aria-hidden=true]";
/**
 * Rendered and reachable: not hidden by style, not clipped away by an
 * `overflow: hidden` ancestor. Content inside a scrollable region counts even
 * when scrolled out of the viewport — the agent reads the whole list, and
 * `inViewport` on controls says what is on screen right now. `overflow: hidden`
 * ancestors above the nearest scroll container (the panel holding it) do not clip.
 */
function visible(element: Element): boolean {
  if (element.closest(excluded)) return false;
  if (element.tagName === "OPTION") {
    const select = element.closest("select");
    return Boolean(select && (element as HTMLOptionElement).selected && visible(select));
  }
  const rect = element.getBoundingClientRect();
  if (!rect.width || !rect.height) return false;
  let left = rect.left, top = rect.top, right = rect.right, bottom = rect.bottom;
  let scrollable = false;
  for (let parent: Element | null = element; parent; parent = parent.parentElement) {
    const style = getComputedStyle(parent);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    if (parent === element) continue;
    const overflow = style.overflow + style.overflowX + style.overflowY;
    if (/auto|scroll/.test(overflow)) scrollable = true;
    else if (!scrollable && /hidden|clip/.test(overflow)) {
      // Above the nearest scroll container a hidden ancestor only bounds the scroll region
      // itself; what is scrolled out of it is reachable by scrolling, not hidden.
      const bounds = parent.getBoundingClientRect();
      left = Math.max(left, bounds.left); right = Math.min(right, bounds.right);
      top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom);
    }
  }
  if (!scrollable) { left = Math.max(0, left); top = Math.max(0, top); right = Math.min(innerWidth, right); bottom = Math.min(innerHeight, bottom); }
  return right > left && bottom > top;
}
function inViewport(element: Element): boolean {
  const rect = element.getBoundingClientRect();
  return rect.right > 0 && rect.bottom > 0 && rect.left < innerWidth && rect.top < innerHeight;
}
const label = (element: Element) => element.getAttribute("aria-label")
  || (element.getAttribute("aria-labelledby") || "").split(" ").map(id => document.getElementById(id)?.textContent || "").join(" ").trim()
  || element.textContent?.trim().slice(0, 160) || element.tagName.toLowerCase();
const titleOf = (element: Element) => {
  const title = element.getAttribute("title")?.trim().slice(0, 160);
  return title && title !== label(element) ? { title } : {};
};
const bounds = (element: Element) => {
  const rect = element.getBoundingClientRect();
  return { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) };
};

/** Reuse a rendered observation only while both document and business context stay unchanged. */
/** Stable, language-independent region names for agents; labels stay for display. */
const REGION_NAMES: [string, string][] = [
  ["strategy-workbench", "strategy"], ["app-settings-section", "settings"], ["fdw-position-dock", "positions"],
  ["fdw-ticket", "ticket"], ["fdw-drawer", "drawer"],
];
function regionName(element: Element): string {
  const explicit = element.getAttribute("data-region");
  if (explicit) return explicit;
  const match = REGION_NAMES.find(([className]) => element.classList.contains(className));
  return match ? match[1] : element.getAttribute("role") === "dialog" ? "dialog" : "region";
}

export function createUiObserver() {
  let dirty = true;
  let key = "";
  let cached: ReturnType<typeof observeUi> | undefined;
  const invalidate = () => { dirty = true; };
  const mutations = new MutationObserver(invalidate);
  mutations.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true });
  const events = ["resize", "scroll", "transitionend", "animationend", "focusin", "focusout"];
  events.forEach(event => window.addEventListener(event, invalidate, true));
  return {
    read(context: Record<string, unknown>) {
      const nextKey = JSON.stringify(context);
      if (dirty || !cached || key !== nextKey) {
        cached = observeUi(context);
        key = nextKey;
        dirty = false;
      }
      return cached;
    },
    close() {
      mutations.disconnect();
      events.forEach(event => window.removeEventListener(event, invalidate, true));
    }
  };
}

/** Read-only rendered snapshot. No DOM execution, input values, or credential forms. */
export function observeUi(context: Record<string, unknown>): components["schemas"]["UiObservation"] {
  const dialogs = [...document.querySelectorAll("dialog[open], [role=dialog], [role=alertdialog]")]
    .filter(element => element.getClientRects().length && getComputedStyle(element).visibility !== "hidden");
  // Native modal dialogs render in the browser top layer, regardless of DOM
  // order. A later credential sheet in the DOM must not hide that observation.
  const modalDialogs = dialogs.filter(element => {
    try { return element.matches(":modal"); } catch { return false; }
  });
  const candidates = modalDialogs.length ? modalDialogs : dialogs;
  const dialog = candidates.find(element => {
    const rect = element.getBoundingClientRect();
    const front = document.elementFromPoint(Math.max(0, Math.min(innerWidth - 1, rect.x + rect.width / 2)),
      Math.max(0, Math.min(innerHeight - 1, rect.y + rect.height / 2)));
    return front !== null && element.contains(front);
  }) ?? candidates.at(-1);
  const root = dialog ?? document.querySelector(".fdw-shell") ?? document.body;
  const privateDialog = Boolean(root.closest("[data-agent-private]"));
  const text: string[] = [];
  let visited = 0;
  if (!privateDialog) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node: Node | null;
    while ((node = walker.nextNode()) && visited++ < 1200) {
      const value = node.textContent?.trim();
      if (value && node.parentElement && visible(node.parentElement)) text.push(value);
    }
  }
  const controls = privateDialog ? [] : [...root.querySelectorAll("button, select, summary, [role=tab], [role=separator]")]
    .filter(visible).slice(0, 150).map(element => ({
      label: label(element), role: element.getAttribute("role") || element.tagName.toLowerCase(),
      disabled: element.hasAttribute("disabled") || element.getAttribute("aria-disabled") === "true",
      pressed: element.getAttribute("aria-pressed"), expanded: element.getAttribute("aria-expanded"),
      // Settings switches (role=switch) and segmented options (role=radio) report their state here.
      checked: element.getAttribute("aria-checked"),
      selected: element.getAttribute("aria-selected"),
      // A title the label does not repeat (e.g. a greyed quick-skill chip's reason line).
      ...titleOf(element),
      inViewport: inViewport(element),
      ...(element instanceof HTMLSelectElement ? { value: element.selectedOptions[0]?.text ?? "", options: [...element.options].map(option => option.text).slice(0, 50) } : {}),
      // A resizer (e.g. 调整辅助区宽度) reports its width now and the range this window allows.
      ...(element.getAttribute("role") === "separator" && element.hasAttribute("aria-valuenow")
        ? { value: Number(element.getAttribute("aria-valuenow")), min: Number(element.getAttribute("aria-valuemin")), max: Number(element.getAttribute("aria-valuemax")) } : {}),
      bounds: bounds(element), foreground: getComputedStyle(element).color, background: getComputedStyle(element).backgroundColor
    }));
  const regions = privateDialog ? [] : [...root.querySelectorAll(".fdw-chart-pane, .strategy-workbench, .app-settings-section, .fdw-position-dock, .fdw-ticket, .fdw-drawer, .fdw-header, [role=dialog]")]
    .filter(visible).slice(0, 30).map(element => ({ name: regionName(element),
      label: element.getAttribute("aria-label") || element.querySelector("h2,h3,header,strong")?.textContent?.slice(0, 160) || element.className,
      bounds: bounds(element), foreground: getComputedStyle(element).color, background: getComputedStyle(element).backgroundColor }));
  const joined = text.join("\n");
  const chart = document.querySelector<HTMLElement>("[data-agent-chart-range]");
  let chartViewport: unknown = null;
  try { chartViewport = JSON.parse(chart?.dataset.agentChartObservation ?? "null"); } catch { /* A partial render is not a confirmed viewport. */ }
  // Ids a `highlight` command may point at: every rendered agent target, test id and chart region.
  const highlightTargets = privateDialog ? [] : [...new Set([...root.querySelectorAll(HIGHLIGHT_TARGET_SELECTOR)]
    .filter(visible).map(highlightTargetId).filter((id): id is string => Boolean(id)))].slice(0, 80);
  return { capturedAt: new Date().toISOString(), viewport: { width: innerWidth, height: innerHeight },
    activeDialog: dialog ? (privateDialog ? "凭据或登录窗口（内容不向Agent提供）" : label(dialog).slice(0, 200)) : null,
    text: privateDialog ? "此窗口需要用户直接输入凭据。" : joined.slice(0, 16000),
    truncated: visited >= 1200 || joined.length > 16000, controls, regions,
    context: { ...context, chartVisibleRange: chart?.dataset.agentChartRange ?? null,
      chartViewport, highlightTargets,
      inputValuesOmitted: true, snapshotKind: "rendered-text-and-layout" } };
}
