export const CHART_SETTINGS_STORAGE_KEY = "ai-trader-chart-settings-v1";
export type ThemePreference = "dark" | "light" | "system";

export function applyTheme(preference: ThemePreference) {
  const theme = preference === "system"
    ? window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"
    : preference;
  document.documentElement.dataset.appTheme = theme;
  document.documentElement.style.colorScheme = theme;
  // The installed web app's title bar (and a phone's status bar) takes the header's colour.
  const surface = getComputedStyle(document.documentElement).getPropertyValue("--fdw-surface").trim();
  if (surface) document.querySelector('meta[name="theme-color"]')?.setAttribute("content", surface);
}

export function applySavedTheme() {
  let preference: ThemePreference = "dark";
  try {
    const saved = JSON.parse(localStorage.getItem(CHART_SETTINGS_STORAGE_KEY) || "null")?.theme;
    if (saved === "light" || saved === "system") preference = saved;
  } catch { /* A damaged preference must not prevent opening the app. */ }
  applyTheme(preference);
}
