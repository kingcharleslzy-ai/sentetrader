import { afterEach, describe, expect, test } from "vitest";
import {
  DEFAULT_FONT_SCALE,
  DEFAULT_INTERFACE_DENSITY,
  applyAppearance,
  persistAppearance,
  readFontScale,
  readInterfaceDensity,
  stepFontScale
} from "./appearance";

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-font-scale");
  document.documentElement.removeAttribute("data-interface-density");
  document.documentElement.style.zoom = "";
});

describe("appearance preferences", () => {
  test("uses readable type and balanced density on every platform", () => {
    expect(DEFAULT_FONT_SCALE).toBe("large");
    expect(DEFAULT_INTERFACE_DENSITY).toBe("standard");
    expect(readFontScale()).toBe("large");
    expect(readInterfaceDensity()).toBe("standard");
  });

  test("persists type and density independently and removes legacy page zoom", () => {
    window.localStorage.setItem("ai-trader:display-scale", "1.25");
    persistAppearance("extra-large", "compact");
    expect(readFontScale()).toBe("extra-large");
    expect(readInterfaceDensity()).toBe("compact");
    expect(window.localStorage.getItem("ai-trader:display-scale")).toBeNull();
  });

  test("invalid stored values fall back safely", () => {
    window.localStorage.setItem("ai-trader:font-scale", "huge");
    window.localStorage.setItem("ai-trader:interface-density", "tiny");
    expect(readFontScale()).toBe("large");
    expect(readInterfaceDensity()).toBe("standard");
  });

  test("keyboard stepping changes only the three font choices", () => {
    expect(stepFontScale("standard", -1)).toBe("standard");
    expect(stepFontScale("standard", 1)).toBe("large");
    expect(stepFontScale("large", 1)).toBe("extra-large");
    expect(stepFontScale("extra-large", 1)).toBe("extra-large");
  });

  test("applies CSS preferences without browser page zoom", async () => {
    document.documentElement.style.zoom = "1.25";
    document.documentElement.dataset.displayScale = "1.25";
    await applyAppearance("extra-large", "compact");
    expect(document.documentElement.dataset.fontScale).toBe("extra-large");
    expect(document.documentElement.dataset.interfaceDensity).toBe("compact");
    expect(document.documentElement.dataset.displayScale).toBeUndefined();
    expect(document.documentElement.style.zoom).toBe("");
  });
});
