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

for (const viewport of [{ width: 1280, height: 695 }, { width: 1440, height: 900 }]) {
  for (const fontScale of ["standard", "extra-large"] as const) {
    for (const theme of ["light", "dark"] as const) {
      test(`workspace orb ${viewport.width}x${viewport.height} ${fontScale} ${theme}`, async ({ page }, testInfo) => {
        test.setTimeout(120_000);
        await page.setViewportSize(viewport);
        const runtime = `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_RUNTIME_PORT || "8003"}`;
        const headers = { Authorization: "Bearer ai-trader-execution-e2e-token", Origin: `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_CLIENT_PORT || "4176"}` };
        const windows = async () => (await (await page.request.get(`${runtime}/api/app/ui/windows`, { headers })).json()).windows as { windowId: string; online: boolean }[];
        const before = new Set((await windows()).map(item => item.windowId));
        await page.goto("/");
        await waitForWorkspace(page);
        const windowId = async () => (await windows()).filter(item => item.online && !before.has(item.windowId)).at(-1)?.windowId;
        await expect.poll(windowId).toBeTruthy();
        const command = async (changes: Record<string, unknown>) => {
          let receipt;
          for (const [key, value] of Object.entries(changes)) {
            for (let attempt = 0; attempt < 3; attempt++) {
              const id = await windowId();
              const state = await (await page.request.get(`${runtime}/api/app/ui/windows/${id}`, { headers })).json();
              receipt = await (await page.request.post(`${runtime}/api/app/ui/commands?waitMs=5000`, { headers,
                data: { commandId: crypto.randomUUID(), windowId: id, expectedRevision: state.revision, changes: { [key]: value } } })).json();
              if (receipt.status === "applied") break;
            }
            expect(receipt.status, JSON.stringify(receipt)).toBe("applied");
          }
          return receipt;
        };
        await command({ fontScale, theme, panel: null, preference: { key: "ui.autoHidePositionsBar", value: false } });
        await expect(page.locator("html")).toHaveAttribute("data-font-scale", fontScale);
        await expect(page.locator("html")).toHaveAttribute("data-app-theme", theme);
        const orb = page.getByTestId("ai-orb");
        const menu = page.getByRole("menu", { name: "AI 球菜单" });
        const dragTo = async (x: number, y: number) => {
          const box = (await orb.boundingBox())!;
          await page.mouse.move(box.x + 28, box.y + 28);
          await page.mouse.down();
          await page.mouse.move(x, y, { steps: 8 });
          await page.mouse.up();
          await expect(menu).not.toBeVisible();
          await expect(page.getByRole("region", { name: "AI 动态卡片" })).not.toBeVisible();
        };
        const bounded = async () => {
          await expect.poll(() => orb.evaluate(el => {
            const r = el.getBoundingClientRect(), body = el.closest(".fdw-body")!.getBoundingClientRect();
            const bar = document.querySelector('[data-testid="positions-bar"]')?.getBoundingClientRect();
            const rail = document.querySelector(".fdw-tools-rail")!.getBoundingClientRect();
            return r.left >= body.left + 7 && r.right <= body.right - 7 && r.top >= body.top + 7
              && r.bottom <= Math.min(body.bottom - 7, bar?.height ? bar.top - 7 : Infinity)
              && (r.right <= rail.left - 7 || r.left >= rail.right + 7);
          })).toBe(true);
        };
        for (const view of ["trade", "board", "dashboard", "review"]) {
          await command({ view });
          await expect(orb).toHaveCount(1);
          expect(await orb.evaluate(el => el.parentElement?.parentElement?.className)).toBe("fdw-body");
          for (const [x, y] of [[0, 0], [viewport.width, 0], [0, viewport.height], [viewport.width, viewport.height]]) {
            await dragTo(x, y);
            await bounded();
            await orb.click({ button: "right" });
            await expect(menu).toBeVisible();
            const m = (await menu.boundingBox())!;
            expect(m.x).toBeGreaterThanOrEqual(0);
            expect(m.y).toBeGreaterThanOrEqual(0);
            expect(m.x + m.width).toBeLessThanOrEqual(viewport.width);
            expect(m.y + m.height).toBeLessThanOrEqual(viewport.height);
            await page.keyboard.press("Escape");
            await expect(orb).toBeFocused();
          }
          await orb.press("ArrowUp");
          const saved = await page.evaluate(() => localStorage.getItem("ai-trader-ai-float-position-v2"));
          await page.locator(".fdw-tools-rail").getByRole("button", { name: "打开持仓面板", exact: true }).click();
          const expand = page.getByRole("button", { name: "展开持仓与委托", exact: true });
          if (await expand.isVisible()) await expand.click();
          await bounded();
          expect(await page.evaluate(() => localStorage.getItem("ai-trader-ai-float-position-v2"))).toBe(saved);
          // Park clear of the fixture's quote rows / price plot after exercising the four edges.
          const pane = page.locator(".fdw-market .fdw-chart-pane").first();
          const park = await (view === "board" ? page.locator(".fdw-market-core") : pane).boundingBox();
          expect(park).toBeTruthy();
          await dragTo(park!.x + 170, view === "board" ? park!.y + park!.height - 80 : park!.y + 60);
          await page.screenshot({ animations: "disabled", path: testInfo.outputPath(`${view}-panel.png`) });
          const collapse = page.getByRole("button", { name: "收起持仓与委托", exact: true });
          if (await collapse.isVisible()) await collapse.click();
          await page.locator(".fdw-tools-rail").getByRole("button", { name: "收起持仓面板", exact: true }).click();
          await bounded();
          await orb.press("Shift+F10");
          await expect(menu).toBeVisible();
          await page.screenshot({ animations: "disabled", path: testInfo.outputPath(`${view}-menu.png`) });
          await page.keyboard.press("Escape");
        }
        const saved = await page.evaluate(() => localStorage.getItem("ai-trader-ai-float-position-v2"));
        await page.setViewportSize({ width: 960, height: 600 });
        await bounded();
        await page.screenshot({ animations: "disabled", path: testInfo.outputPath("shrink.png") });
        await page.reload();
        await waitForWorkspace(page);
        await bounded();
        expect(await page.evaluate(() => localStorage.getItem("ai-trader-ai-float-position-v2"))).toBe(saved);
      });
    }
  }
}


test("the phone keeps its own voice entry without a floating orb", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1" });
  try {
    const page = await context.newPage();
    await page.goto(baseURL!);
    await expect(page.locator('[data-agent-target="mobile-hold-to-talk"]')).toBeVisible();
    await expect(page.getByTestId("ai-orb")).toHaveCount(0);
  } finally { await context.close(); }
});
