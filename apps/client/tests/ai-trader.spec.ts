import { expect, test, type Page } from "@playwright/test";
import { openSettings, responsiveViewports, waitForWorkspace } from "./support/workspace";

async function send(page: Page, text: string) {
  if (await page.getByLabel("向 AI 决策流提问").count() === 0) {
    await page.getByRole("button", { name: "打开对话" }).click();
  }
  const composer = page.getByLabel("向 AI 决策流提问");
  await composer.fill(text);
  await composer.press("Enter");
}

async function runSyntheticBacktest(page: Page) {
  await send(page, "修改策略并回测");
  await expect(page.getByTestId("backtest-card").last()).toBeVisible();
}

async function layoutContract(page: Page) {
  return page.evaluate(() => {
    const selectorFor = (element: Element) => {
      const className = element.getAttribute("class")?.trim().split(/\s+/).filter(Boolean).join(".");
      return `${element.tagName.toLowerCase()}${className ? `.${className}` : ""}`;
    };
    const scrollRegions = Array.from(document.querySelectorAll<HTMLElement>("body *"))
      .filter((element) => {
        const style = getComputedStyle(element);
        return element.getClientRects().length > 0 && style.visibility !== "hidden"
          && [style.overflowX, style.overflowY].some((value) => value === "auto" || value === "scroll");
      })
      // A scroll policy without overflowing content does not create a user scroll region.
      .map(element => ({ selector: selectorFor(element), clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
        clientHeight: element.clientHeight, scrollHeight: element.scrollHeight }))
      .filter(region => region.scrollWidth > region.clientWidth + 1 || region.scrollHeight > region.clientHeight + 1);
    const measure = (selector: string) => {
      const element = document.querySelector<HTMLElement>(selector);
      return element ? {
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight
      } : null;
    };
    const criticalBounds = [
      ".fdw-header",
      ".fdw-body",
      ".fdw-market",
      ".fdw-side-workbench",
      ".fdw-order-panel",
      ".fdw-position-dock",
      ".fdw-tools-rail"
    ].flatMap((selector) => Array.from(document.querySelectorAll<HTMLElement>(selector)).map((element) => {
      const rect = element.getBoundingClientRect();
      return { selector, left: rect.left, right: rect.right, width: rect.width };
    }));
    return {
      root: measure("html"),
      body: measure("body"),
      shell: measure(".fdw-shell"),
      market: measure(".fdw-market"),
      ticket: measure(".fdw-order-panel"),
      positionDock: measure(".fdw-position-dock"),
      aiContext: measure(".fdw-ai-context"),
      chartTools: measure(".fdw-chart-tools"),
      criticalBounds,
      scrollRegions,
      pageOverflow: {
        htmlX: getComputedStyle(document.documentElement).overflowX,
        htmlY: getComputedStyle(document.documentElement).overflowY,
        bodyX: getComputedStyle(document.body).overflowX,
        bodyY: getComputedStyle(document.body).overflowY
      },
      viewport: { width: window.innerWidth, height: window.innerHeight }
    };
  });
}

test.beforeEach(async ({ page, request }) => {
  // Each test starts a fresh disposable conversation; reloads within it retain history.
  const runtime = `http://127.0.0.1:${process.env.AI_TRADER_E2E_RUNTIME_PORT || "8000"}`;
  const headers = {Authorization: "Bearer ai-trader-e2e-local-token", Origin: `http://127.0.0.1:${process.env.AI_TRADER_E2E_CLIENT_PORT || "4173"}`};
  const restored = await request.post(`${runtime}/api/sessions/restore`, {headers, data: {mode: "local", agentId: "native"}});
  expect(restored.ok()).toBeTruthy();
  await request.delete(`${runtime}/api/sessions/${(await restored.json()).sessionId}`, {headers});
  await page.goto("/");
  await waitForWorkspace(page);
});

// The 2026-10-08 product contract removed the sample backtest button. The official
// TqSdk API/tool remains covered by test_official_backtest.py; this page never substitutes engines.
test("review offers account review without the removed sample backtest", async ({ page }) => {
  let tradingMutations = 0;
  page.on("request", outbound => {
    if (outbound.method() === "POST" && new URL(outbound.url()).pathname.startsWith("/api/simulation/orders/")) tradingMutations += 1;
  });
  await page.getByRole("button", { name: "复盘", exact: true }).click();
  await expect(page.getByRole("heading", { name: "账户复盘", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /示例趋势策略回测|回测近 30 日/, exact: true })).toHaveCount(0);
  expect(tradingMutations).toBe(0);
});

test("positions and remote activity are available without an AI prompt", async ({ page }) => {
  // The positions bar is mounted under the chart from the start, collapsed to the account overview line.
  const dock = page.getByRole("region", { name: "持仓与委托" });
  await expect(dock).toBeVisible();
  await expect(dock).toHaveAttribute("data-collapsed", "true");
  await expect(dock).toContainText("委托 0");
  await dock.getByRole("button", { name: "展开持仓与委托" }).click();
  await expect(dock).toHaveAttribute("data-collapsed", "false");
  // 1.6.6: "查看全部" opens the 持仓面板 on the right (the 持仓抽屉 is gone).
  await dock.getByRole("button", { name: "查看全部" }).click();
  const panel = page.getByRole("region", { name: "持仓面板" });
  await expect(page.getByTestId("futures-decision-workspace")).toHaveAttribute("data-right-dock", "positions");
  await expect(panel.getByTestId("positions-kpi")).toContainText("权益");
  await expect(panel.getByRole("list", { name: "当前持仓" }).locator("li")).not.toHaveCount(0);
  await panel.getByRole("button", { name: "刷新账户" }).click();
  await expect(panel.getByRole("list", { name: "当前持仓" }).locator("li")).not.toHaveCount(0);
  // The protection section moved here from the AI monitor page.
  const protection = panel.locator(".ai-protection-summary");
  await expect(protection).toContainText("保护");
  await protection.getByRole("button", { name: "展开" }).click();
  await expect(protection.locator(".ai-protection-lines")).toBeVisible();
});

test("AI synthetic backtest is a research-only card", async ({ page }) => {
  await runSyntheticBacktest(page);
  const backtest = page.getByTestId("backtest-card").last();
  await expect(backtest).toContainText("优于基线");
  await expect(backtest.getByRole("button", { name: /部署/ })).toHaveCount(0);
});

test("AI trade remains a candidate until the owner confirms", async ({ page }) => {
  await send(page, "准备模拟交易方案");
  const proposal = page.getByTestId("trade-proposal-card").last();
  await expect(proposal).toContainText("等待确认");
  await expect(proposal.getByTestId("trade-risk-result")).toHaveCount(0);
  // No simulation gateway here: the card's own 确认 (no second dialog since 2026-10-19) stays disabled with the reason —
  // a card is never recorded as a mere candidate (PR #1004 review).
  await expect(proposal.getByTestId("confirm-trade")).toBeDisabled();
  await expect(proposal).toContainText("模拟下单暂时不可用");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("natural-language risk rules require owner confirmation", async ({ page }) => {
  await send(page, "黄金和螺纹钢每笔最多亏一千元，每天亏到三千元，晚上十点以后不再开仓");
  const proposal = page.getByTestId("risk-rule-proposal-card").last();
  await expect(proposal).toContainText("沪金、螺纹钢");
  await proposal.getByTestId("confirm-risk-rule").click();
  await expect(proposal).toContainText("已生效");
  await expect(proposal).toContainText("AI 无法跳过");
});

test("the settings drawer offers no runtime mode switch any more", async ({ page }) => {
  const settings = await openSettings(page);
  await expect(page.getByTestId("futures-decision-workspace")).toHaveAttribute("data-connection", "connected");
  await expect(settings.getByRole("button", { name: /切换到/ })).toHaveCount(0);
  await settings.getByRole("button", { name: "关于", exact: true }).click();
  await expect(settings.getByText("软件版本")).toBeVisible();
});

test("font size and interface density persist independently without page zoom", async ({ page }) => {
  const settings = await openSettings(page);
  await settings.getByRole("button", { name: "界面" }).click();
  await settings.getByRole("radio", { name: "特大 120%" }).click();
  await settings.getByRole("radio", { name: "紧凑" }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("ai-trader:font-scale"))).toBe("extra-large");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("ai-trader:interface-density"))).toBe("compact");
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.fontScale)).toBe("extra-large");
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.interfaceDensity)).toBe("compact");
  expect(await page.evaluate(() => document.documentElement.style.zoom)).toBe("");

  await page.keyboard.press("Control+-");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("ai-trader:font-scale"))).toBe("large");
  expect(await page.evaluate(() => localStorage.getItem("ai-trader:interface-density"))).toBe("compact");
  await page.keyboard.press("Control+0");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("ai-trader:font-scale"))).toBe("large");

  await page.reload();
  await waitForWorkspace(page);
  const reopened = await openSettings(page);
  await reopened.getByRole("button", { name: "界面" }).click();
  await expect(reopened.getByRole("radio", { name: "大字 110%" })).toHaveAttribute("aria-checked", "true");
  await expect(reopened.getByRole("radio", { name: "紧凑" })).toHaveAttribute("aria-checked", "true");
});

test("dark, light and system themes cover the whole workspace and persist", async ({ page }) => {
  const workspace = page.getByTestId("futures-decision-workspace");
  const settings = await openSettings(page);
  await settings.getByRole("button", { name: "界面" }).click();
  await expect(settings.getByRole("radio", { name: "深色" })).toHaveAttribute("aria-checked", "true");

  await settings.getByRole("radio", { name: "浅色" }).click();
  await expect(workspace).toHaveClass(/app-theme-light/);
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.appTheme)).toBe("light");

  await settings.getByRole("radio", { name: "跟随系统" }).click();
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(workspace).toHaveAttribute("data-theme-preference", "system");
  await expect(workspace).toHaveClass(/app-theme-dark/);
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.appTheme)).toBe("dark");

  await page.reload();
  await waitForWorkspace(page);
  await expect(workspace).toHaveAttribute("data-theme-preference", "system");
  await expect(workspace).toHaveClass(/app-theme-dark/);
});

test("appearance combinations keep working surfaces fitted within the viewport", async ({ page }) => {
  const combinations = [
    { font: "standard", density: "comfortable", width: 1920, height: 1080 },
    { font: "large", density: "standard", width: 1440, height: 960 },
    { font: "extra-large", density: "compact", width: 1366, height: 768 },
    { font: "extra-large", density: "compact", width: 960, height: 720 }
  ] as const;

  for (const combination of combinations) {
    await page.setViewportSize({ width: combination.width, height: combination.height });
    await page.evaluate(({ font, density }) => {
      localStorage.setItem("ai-trader:font-scale", font);
      localStorage.setItem("ai-trader:interface-density", density);
    }, combination);
    await page.reload();
    await waitForWorkspace(page);
    const layout = await layoutContract(page);
    expect(layout.root?.scrollWidth, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.root?.clientWidth ?? 0);
    expect(layout.root?.scrollHeight, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.root?.clientHeight ?? 0);
    expect(layout.body?.scrollWidth, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.viewport.width);
    expect(layout.body?.scrollHeight, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.viewport.height);
    for (const bound of layout.criticalBounds) {
      expect(bound.left, JSON.stringify({ combination, layout, bound })).toBeGreaterThanOrEqual(-1);
      expect(bound.right, JSON.stringify({ combination, layout, bound })).toBeLessThanOrEqual(layout.viewport.width + 1);
      expect(bound.width, JSON.stringify({ combination, layout, bound })).toBeGreaterThan(0);
    }
    expect(layout.shell?.clientWidth, JSON.stringify({ combination, layout })).toBe(layout.viewport.width);
    expect(layout.shell?.clientHeight, JSON.stringify({ combination, layout })).toBe(layout.viewport.height);
    expect(layout.pageOverflow, JSON.stringify({ combination, layout })).toEqual({
      htmlX: "hidden",
      htmlY: "hidden",
      bodyX: "hidden",
      bodyY: "hidden"
    });
    expect(layout.market?.scrollHeight, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.market?.clientHeight ?? 0);
    if (layout.ticket) {
      expect(layout.ticket.scrollHeight, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.ticket.clientHeight + 1);
    }
    if (layout.positionDock) {
      expect(layout.positionDock.scrollHeight, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.positionDock.clientHeight + 1);
    }
    if (layout.aiContext) {
      expect(layout.aiContext.scrollWidth, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.aiContext.clientWidth);
    }
    expect(layout.chartTools?.scrollWidth, JSON.stringify({ combination, layout })).toBeLessThanOrEqual(layout.chartTools?.clientWidth ?? 0);
    expect(layout.scrollRegions.filter((region) => region.selector !== "div.fdw-ai-scroll"), JSON.stringify(layout.scrollRegions)).toEqual([]);
    if (combination.width === 960) {
      const settings = await openSettings(page);
      const drawer = await settings.locator(".app-settings-content").evaluate((element) => ({
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
        overflowY: getComputedStyle(element).overflowY
      }));
      if (drawer.scrollHeight > drawer.clientHeight + 1) {
        expect(drawer.overflowY, JSON.stringify({ combination, drawer })).toMatch(/auto|scroll/);
      }
      await settings.getByRole("button", { name: "关于", exact: true }).click();
      await settings.getByText("软件版本").scrollIntoViewIfNeeded();
      await expect(settings.getByText("软件版本")).toBeVisible();
      await settings.getByRole("button", { name: "关闭系统设置" }).click();
    }
  }
});

test("默认全屏图表，AI 与下单抽屉互斥（五档在下单面板里），持仓底栏常驻", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.reload();
  await waitForWorkspace(page);

  const workspace = page.getByTestId("futures-decision-workspace");
  await expect(workspace).toHaveAttribute("data-right-dock", "closed");
  await expect(page.locator(".fdw-side-workbench")).toHaveCount(0);
  expect(await page.locator(".fdw-chart-stage").evaluate((element) => element.getBoundingClientRect().width / window.innerWidth)).toBeGreaterThanOrEqual(0.9);

  await page.getByRole("button", { name: "打开对话" }).click();
  await expect(workspace).toHaveAttribute("data-right-dock", "ai");
  await expect(page.locator(".fdw-ai")).toHaveCount(1);
  await expect(page.getByLabel("固定下单面板")).toHaveCount(0);
  const advice = page.getByTestId("ai-advice").first();
  await expect(advice.getByRole("heading", { name: "结论" })).toBeVisible();
  const adviceFlow = await advice.evaluate((element) => {
    const heading = element.querySelector("h4")?.getBoundingClientRect();
    const content = element.querySelector("p")?.getBoundingClientRect();
    return { headingBottom: heading?.bottom, contentTop: content?.top };
  });
  expect(adviceFlow.headingBottom).toBeLessThanOrEqual(adviceFlow.contentTop ?? 0);
  expect(await page.locator(".fdw-chart-stage").evaluate((element) => element.getBoundingClientRect().width / window.innerWidth)).toBeGreaterThanOrEqual(0.6);

  await expect(page.getByRole("button", { name: "打开价格阶梯" })).toHaveCount(0);
  await page.getByRole("button", { name: "打开模拟下单" }).click();
  await expect(workspace).toHaveAttribute("data-right-dock", "order");
  await expect(page.locator(".fdw-ai")).toHaveCount(0);
  await expect(page.getByLabel("固定下单面板")).toBeVisible();
  // 五档盘口（2026-10-07 起价格阶梯并入下单面板）：假数据源带满五档，渲染卖五到买五 11 行。
  const depth = page.getByLabel("固定下单面板").getByRole("region", { name: "五档" });
  await expect(depth).toContainText("卖五");
  await expect(depth).toContainText("买五");
  expect(await depth.locator(".fdw-ladder-rows button").count()).toBe(11);

  // 1.6.6: the rail's 持仓 opens the 持仓面板 (mutually exclusive with 下单); the bar stays under the chart.
  const bar = page.getByRole("region", { name: "持仓与委托" });
  await expect(bar).toBeVisible();
  await page.getByRole("button", { name: "打开持仓面板" }).click();
  await expect(workspace).toHaveAttribute("data-right-dock", "positions");
  await expect(page.getByRole("region", { name: "持仓面板" })).toBeVisible();
  await expect(page.getByLabel("固定下单面板")).toHaveCount(0);
  await expect(bar).toHaveAttribute("data-collapsed", "true");
  await page.getByRole("button", { name: "收起持仓面板" }).click();
  await expect(workspace).toHaveAttribute("data-right-dock", "closed");
  await expect(bar).toContainText("委托 0");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)).toBe(false);
});

test("拉宽单一辅助抽屉后图表与抽屉都不被裁切", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.reload();
  await waitForWorkspace(page);

  await page.getByRole("button", { name: "打开对话" }).click();
  const resizer = page.getByRole("separator", { name: "调整辅助区宽度" });
  for (let index = 0; index < 8; index += 1) await resizer.press("ArrowLeft");
  expect(Number(await resizer.getAttribute("aria-valuenow"))).toBe(468);

  const geometry = await page.evaluate(() => {
    const rect = (selector: string) => {
      const value = document.querySelector(selector)?.getBoundingClientRect();
      return value ? { left: value.left, right: value.right, top: value.top, bottom: value.bottom, width: value.width } : null;
    };
    return {
      root: { clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth },
      chart: rect(".fdw-chart-stage"),
      workbench: rect(".fdw-side-workbench"),
      ai: rect(".fdw-ai")
    };
  });
  expect(geometry.root.scrollWidth).toBeLessThanOrEqual(geometry.root.clientWidth);
  expect(geometry.chart?.width).toBeGreaterThanOrEqual(700);
  expect(geometry.ai?.left).toBeGreaterThanOrEqual((geometry.workbench?.left ?? 0) - 1);
  expect(geometry.ai?.right).toBeLessThanOrEqual((geometry.workbench?.right ?? 0) + 1);
});

test("布局入口可以交换辅助区并在安全范围内调整模块大小", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1024 });
  await page.reload();
  await waitForWorkspace(page);

  const workspace = page.getByTestId("futures-decision-workspace");
  const settings = await openSettings(page);
  await settings.getByRole("button", { name: "界面" }).click();
  await settings.getByRole("radio", { name: "左侧", exact: true }).click();
  await settings.getByRole("button", { name: "关闭系统设置" }).click();
  await expect(workspace).toHaveAttribute("data-dock-side", "left");

  await page.getByRole("button", { name: "打开对话" }).click();
  const sideResizer = page.getByRole("separator", { name: "调整辅助区宽度" });
  const initialWidth = Number(await sideResizer.getAttribute("aria-valuenow"));
  await sideResizer.press("ArrowRight");
  await expect(sideResizer).toHaveAttribute("aria-valuenow", String(initialWidth + 16));
  const geometry = await page.evaluate(() => ({
    aiRight: document.querySelector(".fdw-ai")?.getBoundingClientRect().right,
    chartLeft: document.querySelector(".fdw-market")?.getBoundingClientRect().left
  }));
  expect(geometry.aiRight).toBeLessThanOrEqual(geometry.chartLeft ?? 0);

  await page.reload();
  await expect(workspace).toHaveAttribute("data-dock-side", "left");
  const reopened = await openSettings(page);
  await reopened.getByRole("button", { name: "界面" }).click();
  await reopened.getByRole("button", { name: "恢复默认布局" }).click();
  await expect(workspace).toHaveAttribute("data-dock-side", "right");
});

test("saved conversation and selected contract resume after reload", async ({ page }) => {
  await send(page, "查看主力行情");
  await expect(page.getByTestId("market-card")).toBeVisible();
  await page.reload();
  await waitForWorkspace(page);
  await page.getByRole("button", {name: "打开对话"}).click();
  await expect(page.getByText("查看主力行情", {exact: true})).toBeVisible();
  await expect(page.getByTestId("market-card")).toBeVisible();
});

test("disconnect is visible and reconnect restores conversation", async ({ page, context }) => {
  await send(page, "查看主力行情");
  await expect(page.getByTestId("market-card")).toBeVisible();
  await context.setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await expect(page.getByTestId("runtime-connection")).toContainText("连接已断开");
  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(page.getByTestId("futures-decision-workspace")).toHaveAttribute("data-connection", "connected");
  await expect(page.getByText("查看主力行情", {exact: true})).toBeVisible();
  await expect(page.getByTestId("market-card")).toBeVisible();
});

test("trade and review keep controls isolated", async ({ page }) => {
  const workspace = page.getByTestId("futures-decision-workspace");
  await expect(workspace).toHaveAttribute("data-right-dock", "closed");
  await page.getByRole("button", { name: "打开模拟下单" }).click();
  await expect(page.getByLabel("固定下单面板")).toBeVisible();
  await expect(page.getByRole("button", { name: "确认下单" })).toBeDisabled();
  await page.getByRole("button", { name: "复盘", exact: true }).click();
  await expect(page.getByLabel("固定下单面板")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "账户复盘", exact: true })).toBeVisible();
  await page.getByRole("navigation", { name: "主要工作区" }).getByRole("tab").first().click();
  await expect(workspace).toHaveAttribute("data-right-dock", "closed");
});

test("contract selection persists across 4K, 2K, 1080p and compact responsive viewports", async ({ page }) => {
  await page.evaluate(() => localStorage.setItem("ai-trader:font-scale", "large"));
  await page.reload();
  await waitForWorkspace(page);
  await page.getByRole("button", { name: "打开调研面板" }).click();
  await page.getByRole("button", { name: "固定阅读品种" }).click();
  const newsSymbol = await page.locator(".news-panel-toolbar > strong").innerText();
  await page.locator(".fdw-instrument-button").click();
  const contracts = page.getByRole("dialog", { name: "合约列表" });
  await contracts.getByPlaceholder("搜索合约代码或名称").fill("铁矿石");
  await contracts.getByRole("button", { name: "铁矿石 DCE.i2609", exact: true }).click();
  await expect(page.getByTestId("market-workspace")).toContainText("DCE.I2609");
  expect(await page.evaluate(() => localStorage.getItem("ai-trader:selected-instrument"))).toBe("DCE.i2609");
  // Unpinned current contract + a narrow chart + news pinned to another contract:
  // neither the unrelated header tab nor the news reading symbol can name this chart.
  await page.setViewportSize({ width: 1050, height: 800 });
  await expect(page.locator(".news-panel-toolbar > strong")).toHaveText(newsSymbol);
  await expect(page.locator(".fdw-contract-tabs [role=tab][aria-selected=true]")).toHaveCount(0);
  await expect(page.locator(".fdw-instrument-button > span")).toBeVisible();
  await expect(page.locator(".fdw-instrument-button strong")).toHaveText("DCE.I2609");
  const contractBounds = await page.locator(".fdw-instrument-button").evaluate(button => ({
    nameWidth: button.querySelector("strong")!.getBoundingClientRect().width,
    nameRight: button.querySelector("strong")!.getBoundingClientRect().right,
    buttonRight: button.getBoundingClientRect().right
  }));
  expect(contractBounds.nameWidth).toBeGreaterThan(0);
  expect(contractBounds.nameRight).toBeLessThanOrEqual(contractBounds.buttonRight);
  await page.getByRole("button", { name: "更多周期", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "更多周期" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "收起调研面板" }).click();
  await page.getByRole("button", { name: "15分", exact: true }).click();
  await expect(page.getByRole("button", { name: "15分", exact: true })).toHaveClass(/active/);

  const errors: string[] = [];
  page.on("console", (message) => message.type() === "error" && errors.push(message.text()));
  page.on("pageerror", (error) => errors.push(error.message));
  for (const { name, width, height } of responsiveViewports) {
    await page.setViewportSize({ width, height });
    await page.reload();
    await expect(page.getByTestId("market-workspace")).toContainText("DCE.I2609");
    const overflow = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      offenders: Array.from(document.querySelectorAll<HTMLElement>("body *"))
        .map((element) => ({
          selector: `${element.tagName.toLowerCase()}.${element.className}`,
          left: Math.round(element.getBoundingClientRect().left),
          right: Math.round(element.getBoundingClientRect().right),
          width: Math.round(element.getBoundingClientRect().width)
        }))
        .filter((element) => element.left < -1 || element.right > window.innerWidth + 1)
        .slice(0, 8)
    }));
    expect(overflow.scrollWidth, `${name}: ${JSON.stringify(overflow)}`).toBeLessThanOrEqual(overflow.clientWidth);
  }
  expect(errors).toEqual([]);
});

test("行情栏的次要统计与合约名在任何宽度都整项显示或整项隐藏，不露半个字", async ({ page }) => {
  // 2026-10-03 审查：对话面板打开时 OHLC 列只剩一条缝，露出半个“开”字。
  const clipped = () => page.evaluate(() => {
    const head = document.querySelector(".fdw-market-head")!.getBoundingClientRect();
    const dl = document.querySelector(".fdw-market-head dl");
    const box = dl?.getBoundingClientRect();
    const shown = (rect: DOMRect) => rect.width > 0 && rect.height > 0;
    const problems: string[] = [];
    // Every cell of the head (contract, price, OHLC) ends inside the head.
    for (const cell of Array.from(document.querySelectorAll(".fdw-market-head > *"))) {
      const rect = cell.getBoundingClientRect();
      if (shown(rect) && rect.right > head.right + 1) problems.push(`${cell.className || cell.tagName} 超出行情栏`);
    }
    // Each OHLC item is wholly inside the visible line, or wholly below it (wrapped out of view).
    if (box && shown(box)) for (const item of Array.from(dl!.children)) {
      const rect = item.getBoundingClientRect();
      const inside = rect.left >= box.left - 1 && rect.right <= box.right + 1 && rect.top >= box.top - 1 && rect.bottom <= box.bottom + 1;
      const hidden = rect.top >= box.bottom - 1;
      if (!inside && !hidden) problems.push(`${item.textContent} 被裁`);
    }
    return problems;
  });
  for (const width of [1440, 1280, 1100, 800]) {
    await page.setViewportSize({ width, height: 900 });
    for (const chat of [false, true]) {
      const toggle = page.getByRole("button", { name: chat ? "打开对话" : "收起对话" });
      if (await toggle.isVisible()) await toggle.click();
      await expect(page.getByRole("button", { name: chat ? "收起对话" : "打开对话" })).toBeVisible();
      await expect.poll(clipped, { message: `${width}px 对话${chat ? "开" : "关"}` }).toEqual([]);
    }
  }
});

test("the synced crosshair never outlives the pointer: leaving the chart, or a move seen only at document level, clears it", async ({ page }) => {
  await waitForWorkspace(page);
  const chart = page.locator(".fdw-trading-charts");
  const box = (await chart.boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.3);
  await page.mouse.move(box.x + box.width * 0.4 + 6, box.y + box.height * 0.3 + 6);
  await expect(page.locator(".fdw-synced-crosshair")).toHaveCount(1);
  await page.mouse.move(700, 20, { steps: 6 });
  await expect(page.locator(".fdw-synced-crosshair")).toHaveCount(0);
  // A native window drag or OS overlay can swallow pointerleave: a later move outside the chart still clears.
  await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.3, { steps: 3 });
  await expect(page.locator(".fdw-synced-crosshair")).toHaveCount(1);
  await page.evaluate(() => document.body.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: 5, clientY: 5 })));
  await expect(page.locator(".fdw-synced-crosshair")).toHaveCount(0);
});
