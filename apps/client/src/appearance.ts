export const FONT_SCALE_OPTIONS = [
  { value: "standard", label: "标准", percentage: "100%" },
  { value: "large", label: "大字", percentage: "110%" },
  { value: "extra-large", label: "特大", percentage: "120%" }
] as const;

export const INTERFACE_DENSITY_OPTIONS = [
  { value: "compact", label: "紧凑", description: "少留白" },
  { value: "standard", label: "标准", description: "均衡" },
  { value: "comfortable", label: "宽松", description: "大间距" }
] as const;

export type FontScale = (typeof FONT_SCALE_OPTIONS)[number]["value"];
export type InterfaceDensity = (typeof INTERFACE_DENSITY_OPTIONS)[number]["value"];

const FONT_SCALE_STORAGE_KEY = "ai-trader:font-scale";
const INTERFACE_DENSITY_STORAGE_KEY = "ai-trader:interface-density";
const LEGACY_DISPLAY_SCALE_STORAGE_KEY = "ai-trader:display-scale";

export const DEFAULT_FONT_SCALE: FontScale = "large";
export const DEFAULT_INTERFACE_DENSITY: InterfaceDensity = "standard";

export function isFontScale(value: unknown): value is FontScale {
  return FONT_SCALE_OPTIONS.some((option) => option.value === value);
}

export function isInterfaceDensity(value: unknown): value is InterfaceDensity {
  return INTERFACE_DENSITY_OPTIONS.some((option) => option.value === value);
}

export function readFontScale(
  storage: Pick<Storage, "getItem"> | undefined = typeof window === "undefined" ? undefined : window.localStorage
): FontScale {
  try {
    const stored = storage?.getItem(FONT_SCALE_STORAGE_KEY);
    if (isFontScale(stored)) return stored;
  } catch {
    // A privacy-restricted webview may reject localStorage; use the readable default.
  }
  return DEFAULT_FONT_SCALE;
}

export function readInterfaceDensity(
  storage: Pick<Storage, "getItem"> | undefined = typeof window === "undefined" ? undefined : window.localStorage
): InterfaceDensity {
  try {
    const stored = storage?.getItem(INTERFACE_DENSITY_STORAGE_KEY);
    if (isInterfaceDensity(stored)) return stored;
  } catch {
    // A privacy-restricted webview may reject localStorage; use the balanced default.
  }
  return DEFAULT_INTERFACE_DENSITY;
}

export function persistAppearance(
  fontScale: FontScale,
  interfaceDensity: InterfaceDensity,
  storage: Pick<Storage, "setItem" | "removeItem"> | undefined = typeof window === "undefined" ? undefined : window.localStorage
): void {
  try {
    storage?.setItem(FONT_SCALE_STORAGE_KEY, fontScale);
    storage?.setItem(INTERFACE_DENSITY_STORAGE_KEY, interfaceDensity);
    storage?.removeItem(LEGACY_DISPLAY_SCALE_STORAGE_KEY);
  } catch {
    // The selected appearance remains active for this session when persistence is unavailable.
  }
}

export function stepFontScale(fontScale: FontScale, direction: -1 | 1): FontScale {
  const index = FONT_SCALE_OPTIONS.findIndex((option) => option.value === fontScale);
  const nextIndex = Math.min(FONT_SCALE_OPTIONS.length - 1, Math.max(0, index + direction));
  return FONT_SCALE_OPTIONS[nextIndex].value;
}

export async function applyAppearance(fontScale: FontScale, interfaceDensity: InterfaceDensity): Promise<void> {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.fontScale = fontScale;
  document.documentElement.dataset.interfaceDensity = interfaceDensity;
  document.documentElement.removeAttribute("data-display-scale");
  document.documentElement.style.removeProperty("zoom");
  document.documentElement.style.zoom = "";

  // Native page zoom magnifies spacing and fixed geometry along with text. Keep it
  // at 100% and let the two CSS preference axes control type and density separately.
  if (typeof window !== "undefined" && "__TAURI_INTERNALS__" in window) {
    const { getCurrentWebview } = await import("@tauri-apps/api/webview");
    await getCurrentWebview().setZoom(1);
  }
}
