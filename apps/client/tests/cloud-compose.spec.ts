import { expect, test } from "@playwright/test";

const token = process.env.AI_TRADER_CLOUD_SMOKE_TOKEN;

test("production cloud image authenticates and renders the new workspace without overflow", async ({ page }) => {
  if (!token) throw new Error("cloud_compose_test_token_required");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.addInitScript((accessToken) => {
    window.__AI_TRADER_CLOUD_ACCESS_TOKEN__ = accessToken;
  }, token);

  await page.goto("/");
  await expect(page.getByTestId("futures-decision-workspace")).toBeVisible();
  await page.getByRole("button", { name: "设置", exact: true }).click();
  const settings = page.getByRole("dialog", { name: "设置" });
  await expect(settings).toContainText("云端账户");
  await expect(settings.getByRole("button", { name: /切换到/ })).toHaveCount(0);
  await settings.getByRole("button", { name: "关闭系统设置" }).click();
  await expect(settings).toBeHidden();
  await expect(page.getByTestId("cloud-agent-notice")).toContainText(
    "云端 AI 尚未配置隔离执行环境。",
  );
  await expect(page.getByTestId("market-workspace")).toContainText("CFFEX.IF2609");
  await expect(page.getByLabel("K 线图")).toBeVisible();

  const status = await page.evaluate(async () => {
    const response = await fetch("/api/runtime/status?mode=cloud", { credentials: "include" });
    return { code: response.status, body: await response.json() };
  });
  expect(status.code).toBe(200);
  expect(status.body).toMatchObject({
    authMode: "cloud-jwt",
    authProvider: "development-hs256",
    liveTrading: false,
    accountMode: "fake",
  });
  const version = await page.evaluate(async () => (await fetch("/version.json")).json());
  expect(version).toMatchObject({ liveTrading: "disabled" });
  expect(version.commit).not.toBe("development");
  const layout = await page.evaluate(() => {
    const selectors = [
      ".fdw-header",
      ".fdw-body",
      ".fdw-market",
      ".fdw-side-workbench",
      ".fdw-order-panel",
      ".fdw-position-dock",
      ".fdw-tools-rail",
    ];
    return {
      viewportWidth: window.innerWidth,
      pageScrollWidth: document.documentElement.scrollWidth,
      bounds: selectors.flatMap((selector) => Array.from(document.querySelectorAll<HTMLElement>(selector)).map((element) => {
        const rect = element.getBoundingClientRect();
        return { selector, left: rect.left, right: rect.right, width: rect.width };
      })),
    };
  });
  expect(layout.pageScrollWidth).toBeLessThanOrEqual(layout.viewportWidth);
  for (const bound of layout.bounds) {
    expect(bound.left, JSON.stringify({ layout, bound })).toBeGreaterThanOrEqual(-1);
    expect(bound.right, JSON.stringify({ layout, bound })).toBeLessThanOrEqual(layout.viewportWidth + 1);
    expect(bound.width, JSON.stringify({ layout, bound })).toBeGreaterThan(0);
  }
  expect(errors).toEqual([]);
});
