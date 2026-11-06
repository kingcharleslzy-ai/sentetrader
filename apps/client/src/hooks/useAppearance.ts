import { useEffect, useRef, useState } from "react";
import {
  DEFAULT_FONT_SCALE, applyAppearance, persistAppearance, readFontScale, readInterfaceDensity, stepFontScale,
  type FontScale, type InterfaceDensity
} from "../appearance";

/** Font scale and density: persisted, applied to the document, stepped with Ctrl/Cmd +/-/0. */
export function useAppearance(setToast: (text?: string, kind?: "info" | "error") => void) {
  const toast = useRef(setToast);
  toast.current = setToast;
  const [fontScale, setFontScale] = useState<FontScale>(() => readFontScale());
  const [interfaceDensity, setInterfaceDensity] = useState<InterfaceDensity>(() => readInterfaceDensity());
  // Written while rendering, not only in the effect below: the toolbars fit themselves (useFitLevel) in layout effects,
  // which run before any effect — they measured the old type size and stayed overlapped until the window was resized.
  if (typeof document !== "undefined") Object.assign(document.documentElement.dataset, { fontScale, interfaceDensity });
  useEffect(() => {
    persistAppearance(fontScale, interfaceDensity);
    void applyAppearance(fontScale, interfaceDensity)
      .catch(() => toast.current("显示设置应用失败，请重新打开应用", "error"));
  }, [fontScale, interfaceDensity]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        setFontScale((scale) => stepFontScale(scale, 1));
      } else if (event.key === "-") {
        event.preventDefault();
        setFontScale((scale) => stepFontScale(scale, -1));
      } else if (event.key === "0") {
        event.preventDefault();
        setFontScale(DEFAULT_FONT_SCALE);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  return { fontScale, setFontScale, interfaceDensity, setInterfaceDensity };
}
