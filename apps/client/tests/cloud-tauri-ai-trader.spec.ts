import { expect, test } from "@playwright/test";

const cloudPort = process.env.AI_TRADER_CLOUD_E2E_PORT || "8443";
const cloudRuntimeUrl = `https://127.0.0.1:${cloudPort}`;

test("Tauri origin authenticates, resumes and receives SSE in the new workspace", async ({
  page, context
}) => {
  // Cloud is the only mode now: the page connects to the cloud Runtime on load.
  const restoreResponse = page.waitForResponse(response => response.url() === `${cloudRuntimeUrl}/api/sessions/restore` && response.request().method() === "POST");
  await page.goto("/");
  await expect(page.getByTestId("futures-decision-workspace")).toBeVisible();
  await page.getByRole("button", { name: "设置", exact: true }).click();
  const settings = page.getByRole("dialog", { name: "设置" });
  await expect(page.getByTestId("futures-decision-workspace")).toHaveAttribute("data-connection", "connected");
  await expect(settings.getByRole("button", { name: /切换到/ })).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "登录 Sente" })).not.toBeVisible();

  const browserErrors: string[] = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });

  const cookie = (await context.cookies(cloudRuntimeUrl))
    .find((item) => item.name === "ai_trader_cloud_auth");
  expect(cookie).toMatchObject({ httpOnly: true, secure: true, sameSite: "None" });

  const before = (await (await restoreResponse).json()).sessionId;
  expect(before).toBeTruthy();
  const resumed = await page.evaluate(async ({ sessionId, runtimeUrl }) => {
    const response = await fetch(
      `${runtimeUrl}/api/sessions/${encodeURIComponent(sessionId || "")}`,
      { credentials: "include" }
    );
    return { status: response.status, body: await response.json() };
  }, { sessionId: before, runtimeUrl: cloudRuntimeUrl });
  expect(resumed.status).toBe(200);
  expect(resumed.body.sessionId).toBe(before);

  await settings.getByRole("button", { name: "关闭系统设置" }).click();
  await page.getByRole("button", { name: "打开对话" }).click();
  const composer = page.getByLabel("向 AI 决策流提问");
  const marketCards = page.getByTestId("market-card");
  const restoredCardCount = await marketCards.count();
  const messageResponse = page.waitForResponse(response =>
    response.url() === `${cloudRuntimeUrl}/api/sessions/${before}/messages` && response.request().method() === "POST"
  );
  await composer.fill("查看主力行情");
  await composer.press("Enter");
  expect((await messageResponse).status()).toBe(202);
  await expect(marketCards).toHaveCount(restoredCardCount + 1);
  await expect(marketCards.last()).toBeVisible();
  expect(browserErrors).toEqual([]);
});
