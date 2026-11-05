import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { moreButtonFace, morePeriods, PeriodSwitch } from "./PeriodSwitch";

function renderSwitch(chartView: "kline" | "timeshare", duration: number, folded = false) {
  const onSelectDuration = vi.fn(), onSelectChartView = vi.fn();
  render(<PeriodSwitch chartView={chartView} duration={duration} onSelectDuration={onSelectDuration} onSelectChartView={onSelectChartView} folded={folded} />);
  return { group: screen.getByRole("group", { name: "K线周期" }), onSelectDuration, onSelectChartView };
}

test("收起集合：分时、1分、5分、15分、日线常驻，3分、30分、1小时、周线进更多", () => {
  expect(morePeriods.map(period => period.label)).toEqual(["3分", "30分", "1小时", "周线"]);
});

test("更多按钮的字：选中的是收起的周期就显示它（并点亮），否则是“更多”；分时视图不算", () => {
  expect(moreButtonFace("kline", 3600)).toEqual({ label: "1小时", active: true });
  expect(moreButtonFace("kline", 604800)).toEqual({ label: "周线", active: true });
  expect(moreButtonFace("kline", 300)).toEqual({ label: "更多", active: false });
  expect(moreButtonFace("timeshare", 3600)).toEqual({ label: "更多", active: false });
});

test("放得下时没有更多按钮，九个周期都在行内", () => {
  const { group } = renderSwitch("kline", 300);
  expect(within(group).queryByRole("button", { name: /^更多周期/ })).toBeNull();
});

test("放不下时收起：收起的周期带隐藏类，更多按钮显示当前选中的 1小时，下拉里选周线走 onSelectDuration 并关闭", () => {
  const { group, onSelectDuration } = renderSwitch("kline", 3600, true);
  for (const label of ["3分", "30分", "1小时", "周线"]) expect(within(group).getByRole("button", { name: label })).toHaveClass("fdw-period-extra");
  for (const label of ["分时", "1分", "5分", "15分", "日线"]) expect(within(group).getByRole("button", { name: label })).not.toHaveClass("fdw-period-extra");
  const more = within(group).getByRole("button", { name: "更多周期（当前 1小时）" });
  expect(more).toHaveTextContent("1小时");
  expect(more).toHaveClass("active");
  fireEvent.click(more);
  const popover = screen.getByRole("dialog", { name: "更多周期" });
  expect(within(popover).getAllByRole("button").filter(button => button.textContent !== "×").map(button => button.textContent)).toEqual(["3分", "30分", "1小时", "周线"]);
  expect(within(popover).getByRole("button", { name: "1小时" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(within(popover).getByRole("button", { name: "周线" }));
  expect(onSelectDuration).toHaveBeenCalledWith(604800);
  expect(screen.queryByRole("dialog", { name: "更多周期" })).toBeNull();
});

test("收起时选中常驻周期或分时：更多按钮写“更多”、不点亮", () => {
  const { group } = renderSwitch("timeshare", 3600, true);
  const more = within(group).getByRole("button", { name: "更多周期" });
  expect(more).toHaveTextContent("更多");
  expect(more).not.toHaveClass("active");
  expect(within(group).getByRole("button", { name: "分时" })).toHaveClass("active");
});
