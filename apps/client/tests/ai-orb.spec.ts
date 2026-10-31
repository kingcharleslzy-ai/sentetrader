import { expect, test } from "@playwright/test";
import { waitForWorkspace } from "./support/workspace";

// Chromium's fake microphone stands in for a device; the call itself goes to the e2e Runtime's voice upstream.
test.use({ launchOptions: { args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] } });
test("the AI orb is the voice entry and the activity ball: menu, call, mute, hang-up and the activity card are commandable and observable", async ({ page }) => {
  const runtime = `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_RUNTIME_PORT || "8003"}`;
  const headers = { Authorization: "Bearer ai-trader-execution-e2e-token", Origin: `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_CLIENT_PORT || "4176"}` };
  const voice = await (await page.request.get(`${runtime}/api/voice/realtime/status`, { headers })).json();
  test.skip(!(voice.enabled && voice.keyConfigured), "the e2e Runtime has no voice key: the orb is absent by design");
  const listWindows = async (): Promise<{ windowId: string; online: boolean }[]> =>
    (await (await page.request.get(`${runtime}/api/app/ui/windows`, { headers })).json()).windows;
  const before = new Set((await listWindows()).map(item => item.windowId));
  // Same preamble as the main suite: a fresh disposable conversation, which also warms a cold Runtime
  // before the first paint is timed.
  const restored = await page.request.post(`${runtime}/api/sessions/restore`, { headers, data: { mode: "local", agentId: "native" } });
  expect(restored.ok()).toBeTruthy();
  await page.request.delete(`${runtime}/api/sessions/${(await restored.json()).sessionId}`, { headers });
  await page.goto("/");
  await waitForWorkspace(page);
  const orb = page.getByTestId("ai-orb");
  await expect(orb).toHaveAttribute("data-state", "standby");
  await expect(orb).toHaveAccessibleName(/开始语音通话/);
  await expect(page.getByRole("button", { name: /^(开始通话|语音输入)$/ })).toHaveCount(0);
  // One orb: the old "AI 动态" ball is gone, its card lives behind this orb's menu / command.
  await expect(page.locator(".ai-floating-button")).toHaveCount(0);

  // The agent's view: this window's snapshot and the same UI commands an external agent sends.
  // This page's window is the newest one that appeared after it loaded (a reused local Runtime may still list
  // older ones, and a window re-registers under a new id when its heartbeat restarts), so resolve it per call.
  const currentWindow = async () => (await listWindows()).filter(item => item.online && !before.has(item.windowId)).at(-1)?.windowId ?? "";
  await expect.poll(currentWindow).not.toBe("");
  // Let the window settle (a cold Runtime still moves the state for a moment after the first paint):
  // two consecutive reads with the same revision, as an agent would do before commanding it.
  const revision = async () => (await (await page.request.get(`${runtime}/api/app/ui/windows/${await currentWindow()}`, { headers })).json()).revision as number;
  await expect.poll(async () => { const first = await revision(); await page.waitForTimeout(1000); return first === await revision(); }, { timeout: 20_000 }).toBe(true);

  // A command carries the revision it saw; the window may have reported a newer one in between
  // (a UI click right before), which the Runtime rejects — an agent then re-reads and resends once.
  const disclosure = async (name: string, open: boolean) => {
    let receipt: { status: string; state: { disclosures: Record<string, boolean> } | null } | undefined;
    for (let attempt = 0; attempt < 3 && receipt?.status !== "applied"; attempt += 1) {
      const windowId = await currentWindow();
      const current = await (await page.request.get(`${runtime}/api/app/ui/windows/${windowId}`, { headers })).json();
      const response = await page.request.post(`${runtime}/api/app/ui/commands?waitMs=5000`, { headers, data: {
        commandId: crypto.randomUUID(), windowId, expectedRevision: current.revision, changes: { disclosure: { name, open } }
      } });
      receipt = await response.json();
    }
    expect(receipt?.status, JSON.stringify(receipt)).toBe("applied");
    return receipt!.state!.disclosures;
  };

  expect((await disclosure("voice-menu", true))["voice-menu"]).toBe(true);
  await expect(page.getByRole("menuitem", { name: "切文字" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: /AI 动态/ })).toBeVisible();
  expect((await disclosure("voice-menu", false))["voice-menu"]).toBe(false);
  // The activity card through its own command, on the same surface.
  const setCard = async (value: "open" | "closed") => {
    const windowId = await currentWindow();
    const current = await (await page.request.get(`${runtime}/api/app/ui/windows/${windowId}`, { headers })).json();
    const receipt = await (await page.request.post(`${runtime}/api/app/ui/commands?waitMs=5000`, { headers, data: {
      commandId: crypto.randomUUID(), windowId, expectedRevision: current.revision, changes: { aiActivityCard: value } } })).json();
    expect(receipt.status, JSON.stringify(receipt)).toBe("applied");
    return receipt.state.aiActivityCard as string;
  };
  expect(await setCard("open")).toBe("open");
  await expect(page.getByRole("region", { name: "AI 动态卡片" })).toBeVisible();
  expect(await setCard("closed")).toBe("closed");

  expect((await disclosure("voice-call", true))["voice-call"]).toBe(true);
  await expect(orb).toHaveAttribute("data-state", /busy|listening/);
  await expect(orb).toHaveAttribute("data-state", "listening", { timeout: 20_000 });
  await expect(page.getByRole("button", { name: /结束语音通话（听你说）/ })).toBeVisible();
  // 1.6.6: the 对话面板 call bar shows the same call and shares the orb's `voice-call` surface.
  await page.getByRole("button", { name: "打开对话" }).click();
  const bar = page.locator(".fdw-call-bar");
  await expect(bar).toHaveAttribute("data-state", "live");
  await expect(bar.getByRole("status")).toHaveText(/听你说/);
  await expect(bar.getByRole("button", { name: "结束语音通话" })).toBeVisible();

  expect((await disclosure("voice-mute", true))["voice-mute"]).toBe(true);
  await expect(orb).toHaveAttribute("data-muted", "true");
  await expect(page.getByRole("button", { name: /结束语音通话（已静音）/ })).toBeVisible();
  await expect(bar.getByRole("button", { name: "取消静音" })).toBeVisible();

  // Ending the call from the bar is the same action as the orb's: one call, one `voice-call` state.
  await bar.getByRole("button", { name: "结束语音通话" }).click();
  await expect(orb).toHaveAttribute("data-state", "standby");
  await expect(bar.getByRole("button", { name: "开始语音通话" })).toBeVisible();
  const after = await disclosure("voice-call", false);
  expect(after["voice-call"]).toBe(false);
  expect(after["voice-mute"]).toBeUndefined();
});
