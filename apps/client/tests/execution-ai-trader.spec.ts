import { expect, test } from "@playwright/test";
import { openSettings, responsiveViewports, waitForWorkspace } from "./support/workspace";

// This suite runs against a runtime started with AI_TRADER_FAKE_SIMULATION_GATEWAY=1
// (playwright.execution.config.ts): the in-process gateway lets the real
// activate → feed → stop chain run, so nothing about execution state is mocked.
const runtime = `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_RUNTIME_PORT || "8003"}`;
const headers = {Authorization: "Bearer ai-trader-execution-e2e-token", Origin: `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_CLIENT_PORT || "4176"}`};

test.beforeEach(async ({ page, request }) => {
  const restored = await request.post(`${runtime}/api/sessions/restore`, {headers, data: {mode: "local", agentId: "native"}});
  expect(restored.ok()).toBeTruthy();
  await request.delete(`${runtime}/api/sessions/${(await restored.json()).sessionId}`, {headers});
  await page.goto("/");
  await waitForWorkspace(page);
});

test("AI simulation emergency stop remains usable across 4K, 2K, 1080p and compact viewports", async ({ page, request }) => {
  // Thirteen full reloads share this test; individual UI expectations retain the 8s limit.
  test.setTimeout(120_000);
  const activate = async () => {
    const restored = await request.post(`${runtime}/api/sessions/restore`, {headers, data: {mode: "local", agentId: "native"}});
    expect(restored.ok()).toBeTruthy();
    const started = await request.post(`${runtime}/api/ai-execution/start`, {headers, data: {
      sessionId: (await restored.json()).sessionId, symbols: ["CFFEX.IF2609"], maxOrderVolume: 1, maxOrders: 10
    }});
    expect(started.ok()).toBeTruthy();
    expect((await started.json()).status).toBe("ACTIVE");
  };

  const errors: string[] = [];
  page.on("console", (message) => message.type() === "error" && errors.push(message.text()));
  page.on("pageerror", (error) => errors.push(error.message));
  for (const { width, height } of responsiveViewports) {
    await activate();
    await page.setViewportSize({ width, height });
    await page.reload();
    await expect(page.getByTestId("market-workspace")).toContainText("CFFEX.IF2609");
    await expect(page.getByTestId("ai-execution-control")).toHaveCount(0);
    const settings = await openSettings(page);
    await settings.getByRole("button", { name: "交易" }).click();
    // 执行状态 and 全局急停 live on 交易 → 高级交易设置 (执行限制) since the settings split.
    await settings.getByRole("button", { name: "打开高级交易设置" }).click();
    await expect(settings).toContainText("执行中");
    await settings.getByRole("button", { name: "全局急停" }).click();
    await page.getByRole("button", { name: "确认全局急停" }).click();
    await expect(settings).toContainText("已急停");
    await expect(settings.getByRole("button", { name: /AI 已急停 · 恢复/ })).toBeVisible();
    await settings.getByRole("button", { name: "关闭系统设置" }).click();
    await page.getByRole("button", { name: "打开对话" }).click();
    await expect(page.getByRole("textbox", { name: "向 AI 决策流提问" })).toBeVisible();
    // The floating orb is the only voice entry and exists exactly when the Runtime has a voice key;
    // there is no push-to-talk fallback any more.
    const voice = await (await page.request.get(`${runtime}/api/voice/realtime/status`, { headers })).json();
    // The AI orb is always there; it is the voice entry exactly when the Runtime has a voice key.
    await expect(page.getByTestId("ai-orb")).toBeVisible();
    await expect(page.getByTestId("ai-orb")).toHaveAccessibleName(voice.enabled && voice.keyConfigured ? /开始语音通话/ : /^AI 动态/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
  }
  expect(errors).toEqual([]);
});
