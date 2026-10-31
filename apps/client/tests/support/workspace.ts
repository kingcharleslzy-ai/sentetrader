import { expect, type Page } from "@playwright/test";

export const responsiveViewports = [
  { name: "4K-125%", width: 3072, height: 1728 },
  { name: "4K-150%-or-2K-100%", width: 2560, height: 1440 },
  { name: "2K-125%", width: 2048, height: 1152 },
  { name: "2K-150%", width: 1707, height: 960 },
  { name: "1080p-100%", width: 1920, height: 1080 },
  { name: "1080p-125%", width: 1536, height: 864 },
  { name: "desktop-default", width: 1440, height: 960 },
  { name: "compact-laptop", width: 1366, height: 768 },
  { name: "compact-desktop", width: 960, height: 720 },
  { name: "tablet-portrait", width: 768, height: 900 },
  { name: "mobile-wide", width: 414, height: 896 },
  { name: "mobile", width: 375, height: 812 },
  { name: "mobile-min", width: 320, height: 720 }
] as const;

export async function waitForWorkspace(page: Page) {
  await expect(page.getByTestId("futures-decision-workspace")).toBeVisible();
  await expect(page.getByTestId("market-workspace")).toContainText("CFFEX.IF2609");
  await expect(page.getByLabel("K 线图")).toBeVisible();
}

export async function openSettings(page: Page) {
  await page.getByRole("button", { name: "设置", exact: true }).click();
  return page.getByRole("dialog", { name: "设置" });
}
