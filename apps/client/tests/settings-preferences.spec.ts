import { expect, test } from "@playwright/test";
import { waitForWorkspace } from "./support/workspace";

// 设置重做第 2 步: one `preference` receipt per category page, judged from the document the client
// reads back, and the page shows the same value. Execution config (fake Runtime, fake bars).
test("preference commands land on every settings page and the snapshot reports the document", async ({ page }) => {
  test.setTimeout(120_000);
  const runtime = `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_RUNTIME_PORT || "8003"}`;
  const headers = { Authorization: "Bearer ai-trader-execution-e2e-token", Origin: `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_CLIENT_PORT || "4176"}` };
  const deviceId = crypto.randomUUID();
  await page.addInitScript(id => localStorage.setItem("ai-trader:web-device", id), deviceId);
  const listWindows = async (): Promise<{ windowId: string; online: boolean }[]> =>
    (await (await page.request.get(`${runtime}/api/app/ui/windows?deviceId=${deviceId}`, { headers })).json()).windows;
  // Start from the Runtime defaults whatever an earlier run left behind.
  await page.request.put(`${runtime}/api/preferences`, { headers, data: { voice: { speed: "normal" }, chart: { upDownColors: "cn", candleStyle: "candle", kdParams: { k: 9 }, subPaneList: ["kd", "macd"] },
    trade: { defaultVolume: 1, quickOrder: false }, chat: { sendKey: "enter" }, notify: { desktop: { fills: true } }, data: { sessionRetentionDays: 365 } } });
  await page.goto("/");
  await waitForWorkspace(page);
  await expect(page.getByTestId("futures-decision-workspace")).toHaveAttribute("data-connection", "connected", { timeout: 30_000 });
  const currentWindow = async () => (await listWindows()).filter(item => item.online).at(-1)?.windowId ?? "";
  await expect.poll(currentWindow).not.toBe("");
  const detail = async () => { const response = await page.request.get(`${runtime}/api/app/ui/windows/${await currentWindow()}`, { headers }); return response.json(); };
  // A command carries the revision it was decided on: wait until the window has reported its latest state.
  const settled = () => expect.poll(async () => { const first = (await detail()).revision; await page.waitForTimeout(1000); return first === (await detail()).revision; }, { timeout: 20_000 }).toBe(true);
  await settled();
  const command = async (changes: Record<string, unknown>) => {
    let receipt: { status: string; state: Record<string, unknown> | null } | undefined;
    for (let attempt = 0; attempt < 3 && receipt?.status !== "applied" && receipt?.status !== "rejected"; attempt += 1) {
      const windowId = await currentWindow();
      const current = await detail();
      receipt = await (await page.request.post(`${runtime}/api/app/ui/commands?waitMs=5000`, { headers, data: {
        commandId: crypto.randomUUID(), windowId, expectedRevision: current.revision, changes } })).json();
    }
    return receipt!;
  };
  const preferences = async () => (await detail()).state.preferences as Record<string, Record<string, unknown>>;
  await expect.poll(async () => (await preferences())?.voice?.speed, { timeout: 15_000 }).toBe("normal");

  const settings = page.getByRole("dialog", { name: "设置" });
  // 语音
  expect(await command({ settingsPage: "voice" })).toMatchObject({ status: "applied" });
  expect(await command({ preference: { key: "voice.speed", value: "fast" } })).toMatchObject({ status: "applied" });
  await expect(settings.getByRole("radio", { name: "快" })).toHaveAttribute("aria-checked", "true");
  // 图表: the colour pair flips the document attribute the chart and panels share.
  expect(await command({ settingsPage: "chart" })).toMatchObject({ status: "applied" });
  expect(await command({ preference: { key: "chart.upDownColors", value: "intl" } })).toMatchObject({ status: "applied" });
  await expect(settings.getByRole("radio", { name: "绿涨红跌" })).toHaveAttribute("aria-checked", "true");
  await expect(page.locator("html")).toHaveAttribute("data-updown", "intl");
  expect(await command({ preference: { key: "chart.kdParams.k", value: 14 } })).toMatchObject({ status: "applied" });
  // KD 参数 left the settings page (#448): the chart's KD pane and its own editor read the same document back.
  await page.keyboard.press("Escape");
  await expect(settings).toHaveCount(0);
  await expect(page.getByRole("button", { name: "KD 14,3,3" })).toBeVisible();
  await settled();
  expect(await command({ disclosure: { name: "chart-pane-editor", id: "kd", open: true } })).toMatchObject({ status: "applied" });
  await expect(page.getByRole("spinbutton", { name: "KD 周期" })).toHaveValue("14");
  expect(await command({ disclosure: { name: "chart-pane-editor", id: "kd", open: false } })).toMatchObject({ status: "applied" });
  // 交易 (下单默认)
  expect(await command({ settingsPage: "trade" })).toMatchObject({ status: "applied" });
  expect(await command({ preference: { key: "trade.defaultVolume", value: 4 } })).toMatchObject({ status: "applied" });
  await expect(settings.getByRole("spinbutton", { name: "默认手数" })).toHaveValue("4");
  // AI 与对话 → 对话（sendKey 等对话行随设置拆页移到这里）
  expect(await command({ settingsPage: "ai" })).toMatchObject({ status: "applied" });
  expect(await command({ preference: { key: "chat.sendKey", value: "ctrlEnter" } })).toMatchObject({ status: "applied" });
  await expect(settings.getByRole("radio", { name: "Ctrl / Cmd + Enter" })).toHaveAttribute("aria-checked", "true");
  // 通知（notify.desktop.fills 是“成交与撤单”一行，notifyKinds.ts）
  expect(await command({ settingsPage: "notify" })).toMatchObject({ status: "applied" });
  expect(await command({ preference: { key: "notify.desktop.fills", value: false } })).toMatchObject({ status: "applied" });
  await expect(settings.getByRole("switch", { name: "成交与撤单" })).toHaveAttribute("aria-checked", "false");
  // 数据与隐私
  expect(await command({ settingsPage: "data" })).toMatchObject({ status: "applied" });
  expect(await command({ preference: { key: "data.sessionRetentionDays", value: 30 } })).toMatchObject({ status: "applied" });
  await expect(settings.getByRole("spinbutton", { name: "会话保留天数" })).toHaveValue("30");
  // Out-of-range values never reach the window: the Runtime's schema refuses the command (422).
  const refused = await page.request.post(`${runtime}/api/app/ui/commands`, { headers, data: {
    commandId: crypto.randomUUID(), windowId: await currentWindow(), expectedRevision: (await detail()).revision, changes: { preference: { key: "data.sessionRetentionDays", value: 3 } } } });
  expect(refused.status()).toBe(422);
  expect((await refused.json()).detail[0].msg).toContain("data.sessionRetentionDays");
  // A user click on a row writes the same document the command reads back from.
  await settings.getByRole("button", { name: "会话保留天数增加" }).click();
  await expect.poll(async () => (await preferences()).data.sessionRetentionDays, { timeout: 15_000 }).toBe(37);
  const stored = await (await page.request.get(`${runtime}/api/preferences`, { headers })).json();
  expect(stored.preferences).toMatchObject({ voice: { speed: "fast" }, chart: { upDownColors: "intl", kdParams: { k: 14 } }, trade: { defaultVolume: 4 }, chat: { sendKey: "ctrlEnter" }, notify: { desktop: { fills: false } }, data: { sessionRetentionDays: 37 } });
  // 快捷键 and 界面 keep their local rows; the snapshot shows the shortcut map.
  expect(await command({ settingsPage: "shortcuts" })).toMatchObject({ status: "applied" });
  await expect(settings.getByRole("button", { name: "录制买多草稿" })).toBeVisible();
  await expect.poll(async () => ((await detail()).observation?.context as { settings?: { shortcuts?: Record<string, string> } } | undefined)?.settings?.shortcuts?.buy, { timeout: 15_000 }).toBe("Alt+B");
  // Restore the defaults for the next suite.
  await page.request.put(`${runtime}/api/preferences`, { headers, data: { voice: { speed: "normal" }, chart: { upDownColors: "cn", kdParams: { k: 9 } }, trade: { defaultVolume: 1 }, chat: { sendKey: "enter" }, notify: { desktop: { fills: true } }, data: { sessionRetentionDays: 365 } } });
});
