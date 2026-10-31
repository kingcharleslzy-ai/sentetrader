import { expect, test } from "@playwright/test";
import { waitForWorkspace } from "./support/workspace";

// The AI annotation layer, step one: a command draws on the chart / lights a surface, the
// snapshot shows it, the user's right-click or click clears it. Fake Runtime, fake bars.
test("AI annotations and highlights are commandable, observable and clearable", async ({ page }) => {
  const runtime = `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_RUNTIME_PORT || "8003"}`;
  const headers = { Authorization: "Bearer ai-trader-execution-e2e-token", Origin: `http://127.0.0.1:${process.env.AI_TRADER_EXECUTION_E2E_CLIENT_PORT || "4176"}` };
  // A reused local Runtime may list older windows: this page carries its own web device id, so the
  // window list is filtered to the window that is really ours.
  const deviceId = crypto.randomUUID();
  await page.addInitScript(id => localStorage.setItem("ai-trader:web-device", id), deviceId);
  const listWindows = async (): Promise<{ windowId: string; online: boolean }[]> =>
    (await (await page.request.get(`${runtime}/api/app/ui/windows?deviceId=${deviceId}`, { headers })).json()).windows;
  await page.goto("/");
  await waitForWorkspace(page);
  // A cold Runtime may still be handshaking; the layer needs a connected, settled window.
  await expect(page.getByTestId("futures-decision-workspace")).toHaveAttribute("data-connection", "connected", { timeout: 30_000 });
  const currentWindow = async () => (await listWindows()).filter(item => item.online).at(-1)?.windowId ?? "";
  await expect.poll(currentWindow).not.toBe("");
  const detail = async () => { const response = await page.request.get(`${runtime}/api/app/ui/windows/${await currentWindow()}`, { headers }); return response.json(); };
  await expect.poll(async () => { const first = (await detail()).revision; await page.waitForTimeout(1000); return first === (await detail()).revision; }, { timeout: 20_000 }).toBe(true);
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

  // Times in the chart's own unit: the fake workbench starts at 1_788_192_000 s and steps by the period.
  const state = (await detail()).state as { duration: number; symbol: string };
  const start = 1_788_192_000, step = state.duration;
  // v2: entries and exits are points, levels are lines/zones, the MACD pane takes its own items.
  const items = [
    { id: "short", kind: "point", role: "sell", price: 821.5, time: start + 60 * step, label: "空 821.5" },
    { id: "stop", kind: "point", role: "close", price: 823.5, time: start + 60 * step, label: "止损" },
    { id: "target", kind: "point", role: "buy", price: 815, time: start + 75 * step, label: "止盈" },
    { id: "res", kind: "hline", price: 823, label: "压力", tone: "warn" },
    { id: "sup", kind: "zone", price: 812, price2: 815, label: "支撑区", tone: "good" },
  ];
  let receipt = await command({ chartAnnotations: { op: "set", items } });
  expect(receipt.status, JSON.stringify(receipt)).toBe("applied");
  const drawn = receipt.state!.chartAnnotations as { id: string; pane: string }[];
  expect(drawn.map(item => item.id)).toEqual(["short", "stop", "target", "res", "sup"]);
  expect(drawn.every(item => item.pane === "main")).toBe(true);
  await expect(page.locator("[data-agent-chart-annotations='5']")).toHaveCount(1);
  // A sixth item on the MACD pane; a seventh is refused, and so is a second line at a level that has one.
  receipt = await command({ chartAnnotations: { op: "add", items: [{ id: "div", kind: "point", pane: "macd", price: 1.0, time: start + 70 * step, label: "顶背离", tone: "danger" }] } });
  expect(receipt.status).toBe("applied");
  expect((receipt.state!.chartAnnotations as { id: string; pane: string }[]).find(item => item.id === "div")?.pane).toBe("macd");
  expect(await command({ chartAnnotations: { op: "add", items: [{ id: "seventh", kind: "vline", time: start + 20 * step }] } })).toMatchObject({ status: "rejected" });
  expect(await command({ chartAnnotations: { op: "add", items: [{ id: "res-again", kind: "hline", price: 823, label: "压力" }] } })).toMatchObject({ status: "rejected" });
  expect(((await detail()).state.chartAnnotations as unknown[])).toHaveLength(6);

  // Pointing at a surface: the highlight and its bubble render over the target and the snapshot names it.
  receipt = await command({ highlight: { target: "rail:order", label: "在这里下单", durationMs: 15000 } });
  expect(receipt.status, JSON.stringify(receipt)).toBe("applied");
  const highlight = page.locator(".ai-highlight[data-target='rail:order']");
  await expect(highlight).toBeVisible();
  await expect(highlight).toContainText("在这里下单");
  const snapshot = await detail();
  expect((snapshot.state.highlights as { target: string }[]).map(item => item.target)).toEqual(["rail:order"]);
  expect(snapshot.observation.context.highlightTargets).toEqual(expect.arrayContaining(["rail:order", "positions-bar", "chart.main", "ai-orb"]));
  expect(await command({ highlight: { target: "no-such-surface", durationMs: 3000 } })).toMatchObject({ status: "rejected" });

  // Scrolling the chart to a span reports the span and offers the way back.
  receipt = await command({ chartFocus: { from: start + 10 * step, to: start + 30 * step } });
  expect(receipt.status, JSON.stringify(receipt)).toBe("applied");
  expect(receipt.state!.chartFocus).toEqual({ from: start + 10 * step, to: start + 30 * step });
  await expect(page.getByRole("button", { name: "返回原图" })).toBeVisible();
  receipt = await command({ chartFocus: null });
  expect(receipt.status).toBe("applied");
  expect(receipt.state!.chartFocus).toBeNull();

  // Candidate price by command (what a chart click / the ladder / 右键 set), and back to none.
  receipt = await command({ selectedPrice: 818.4 });
  expect(receipt.status, JSON.stringify(receipt)).toBe("applied");
  expect(receipt.state!.selectedPrice).toBe(818.4);
  await expect(page.locator(".fdw-selected-price")).toContainText("候选价");
  receipt = await command({ selectedPrice: null });
  expect(receipt.status).toBe("applied");
  expect(receipt.state!.selectedPrice).toBeNull();
  await expect(page.locator(".fdw-selected-price")).toHaveCount(0);
  // 恢复默认布局 by command, judged by the layout fields it restores.
  expect((await command({ dockSide: "left" })).status).toBe("applied");
  receipt = await command({ presentation: { reset: true } });
  expect(receipt.status, JSON.stringify(receipt)).toBe("applied");
  expect(receipt.state).toMatchObject({ dockSide: "right", dockWidth: 340, positionsBar: "collapsed" });

  // A guided tour: start lights step 1 after its focus, next/prev move, a click only closes the bubble, stop ends it.
  const course = [
    { target: "rail:order", label: "下单区在这里", say: "下单区", focus: { panel: "order", ticketMode: "open" } },
    { target: "positions-bar", label: "底栏看持仓", focus: { positionsBar: "expanded" } },
    { target: "rail:settings", label: "设置在这里" },
  ];
  receipt = await command({ tour: { op: "start", steps: course } });
  expect(receipt.status, JSON.stringify(receipt)).toBe("applied");
  expect(receipt.state!.tour).toMatchObject({ active: true, step: 1, total: 3, current: { target: "rail:order" } });
  expect(receipt.state!.panel).toBe("order");
  const indicator = page.getByRole("group", { name: "新手引导" });
  await expect(indicator).toContainText("第 1/3 步");
  await expect(page.locator(".ai-highlight[data-target='rail:order']")).toContainText("下单区在这里");
  receipt = await command({ tour: { op: "next" } });
  expect(receipt.status).toBe("applied");
  expect(receipt.state!.tour).toMatchObject({ step: 2, current: { target: "positions-bar" } });
  expect(receipt.state!.positionsBar).toBe("expanded");
  await expect(indicator).toContainText("第 2/3 步");
  // The user's click closes the bubble but not the tour; the indicator re-lights the step.
  await page.getByLabel("K 线图").click({ position: { x: 200, y: 120 } });
  await expect(page.locator(".ai-highlight")).toHaveCount(0);
  await expect(indicator).toContainText("第 2/3 步");
  await page.getByRole("button", { name: "重新高亮当前步骤" }).click();
  await expect(page.locator(".ai-highlight[data-target='positions-bar']")).toBeVisible();
  await page.getByRole("button", { name: "下一步" }).click();
  await expect(indicator).toContainText("第 3/3 步");
  await expect.poll(async () => ((await detail()).state.tour as { step: number }).step).toBe(3);
  receipt = await command({ tour: { op: "prev" } });
  expect(receipt.state!.tour).toMatchObject({ step: 2 });
  receipt = await command({ tour: { op: "stop" } });
  expect(receipt.status).toBe("applied");
  expect(receipt.state!.tour).toMatchObject({ active: false, step: 0, total: 0 });
  await expect(indicator).toHaveCount(0);
  expect(await command({ tour: { op: "start", steps: [{ target: "no-such-surface", label: "x" }] } })).toMatchObject({ status: "rejected" });
  expect((await command({ panel: null })).status).toBe("applied");

  // The user's click dismisses highlights; the chart's right-click 清空图上标记 clears the AI's drawings (with the
  // backtest marks and the fill markers since e824c1d8).
  const chart = page.getByLabel("K 线图");
  await chart.click({ position: { x: 200, y: 120 } });
  await expect(highlight).toHaveCount(0);
  await expect.poll(async () => ((await detail()).state.highlights as unknown[]).length).toBe(0);
  await chart.click({ button: "right", position: { x: 240, y: 140 } });
  await page.getByRole("menuitem", { name: "清空图上标记" }).click();
  await expect(page.locator("[data-agent-chart-annotations='0']")).toHaveCount(1);
  await expect.poll(async () => ((await detail()).state.chartAnnotations as unknown[]).length).toBe(0);
  // "让 AI 讲讲这一段" hands the visible span to the conversation as a prompt.
  await chart.click({ button: "right", position: { x: 240, y: 140 } });
  await page.getByRole("menuitem", { name: "让 AI 讲讲这一段" }).click();
  await expect(page.getByLabel("向 AI 决策流提问")).toHaveValue(/先在图上画出关键位置再解释/);
});
