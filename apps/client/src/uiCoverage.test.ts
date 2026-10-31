// @vitest-environment node
import { expect, it } from "vitest";
import { classify, staleKnownGaps, type Control } from "../tests/support/uiCoverage";

const control = (label: string, extra: Partial<Control> = {}): Control => ({ surface: "trade", role: "button", label, disabled: false, observed: true, ...extra });

it("classifies controls: command, capability, exempt, known gap, and the two failure kinds", () => {
  expect(classify(control("1分")).kind).toBe("command");
  expect(classify(control("打开模拟下单")).via).toMatch(/panel: order/);
  expect(classify(control("确认下单", { surface: "order" })).kind).toBe("capability");
  expect(classify(control("向 AI 决策流提问", { role: "input:text" })).kind).toBe("exempt");
  expect(classify(control("手数减一", { surface: "order" })).kind).toBe("capability");
  expect(classify(control("恢复默认布局", { surface: "settings-appearance" })).via).toMatch(/reset/);
  expect(classify(control("清空显示", { surface: "activity-card" })).via).toMatch(/activity-clear/);
  // 画线第二版 (2026-10-09) 把“仅填入草稿价”移出右键菜单（下单在下单区）；候选价仍由 selectedPrice 命令族覆盖。
  expect(classify(control("清除候选价", { surface: "trade" })).via).toMatch(/selectedPrice/);
  // 副图右键的换成 / 添加跟随共用副图清单 (CCI、成交量 included): the same chart.subPaneList, not chartOption.
  for (const label of ["CCI", "成交量"]) expect(classify(control(label, { group: "chart:context-menu", role: "menuitem" })).via).toMatch(/chart\.subPaneList/);
  // Reachable but invisible to the snapshot is a failure; so is a control no table names.
  expect(classify(control("1分", { observed: false })).kind).toBe("unobserved");
  expect(classify(control("从未见过的按钮")).kind).toBe("gap");
  // Review-only dialogs and the help topics are covered by surface-aware rules.
  expect(classify(control("总盈亏，查看账户明细", { surface: "review" })).via).toMatch(/account-detail/);
  expect(classify(control("图表与十字线", { surface: "dialog-help" })).via).toMatch(/help/);
  expect(classify(control("沪深300股指2609", { role: "tab" })).via).toBe("symbol");
  for (const label of ["沪深300股指2609 CFFEX.IF2609", "沪金2612 SHFE.AU2612", "CFFEX.IF2609", "名称读取中"]) {
    expect(classify(control(label, { group: "market:contract" })).via).toMatch(/contract-drawer/);
  }
  expect(classify(control("沪深300股指2609 CFFEX.IF2609")).kind).toBe("gap");
  // 说人话 (2026-09-25): a parameter's “?” is a disclosure.
  expect(classify(control("向上突破幅度（K1）是什么", { surface: "ai-strategy" })).via).toMatch(/strategy-param-help/);
  // 策略编辑卡 (2026-10-08): its own buttons name the strategyEditor key; its 1分 and 更多： are the card's, not the chart's / 结果页's.
  const card = { surface: "ai-strategy-editor", group: "strategy-editor" };
  expect(classify(control("存为草稿", card)).via).toMatch(/strategyEditor: \{save: true\}/);
  expect(classify(control("1分", card)).via).toMatch(/编辑卡表单项/);
  expect(classify(control("更多：保本、分批止盈、按时间止盈、最长持仓、按条件平仓", { ...card, role: "summary" })).via).toMatch(/strategy-editor-more/);
  expect(classify(control("新建策略", { surface: "ai-strategy" })).via).toMatch(/strategyEditor: \{new: true\}/);
  // 结构化卡片 (2026-10-04): option / 问 AI buttons send a message, UI buttons name their command.
  expect(classify(control("回答：买开（做多）", { surface: "ai" })).via).toMatch(/chat:send/);
  expect(classify(control("问 AI 怎么办", { surface: "ai" })).via).toMatch(/chat:send/);
  expect(classify(control("问 AI 这条资讯", { surface: "ai" })).via).toMatch(/chat:send/);
  expect(classify(control("打开调研", { surface: "ai" })).via).toMatch(/news: \{tab: desk\}/);
  expect(classify(control("打开下单台", { surface: "ai" })).via).toMatch(/panel: order/);
  expect(classify(control("看图表", { surface: "ai" })).via).toMatch(/view: trade/);
  expect(classify(control("打开委托", { surface: "ai" })).via).toMatch(/positionsTab: orders/);
  expect(classify(control("打开持仓", { surface: "ai" })).via).toMatch(/panel: positions/);
});

it("reports known-gap entries that no longer match any control (the list is empty after the first burn-down)", () => {
  expect(staleKnownGaps([control("1分")])).toEqual([]);
});
