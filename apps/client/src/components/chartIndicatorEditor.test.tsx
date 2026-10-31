import { fireEvent, render, screen } from "@testing-library/react";
import { MA_LINES_MAX, SUB_PANES_MAX, normalizeChartIndicators, type ChartIndicatorConfig } from "./chartIndicators";
import { AddPaneChooser, ChartPopover, MaLinesEditor, SubPaneEditor, type ChartIndicatorPatch } from "./chartIndicatorEditor";

function editor(config: Partial<ReturnType<typeof normalizeChartIndicators>> = {}) {
  const indicators = { ...normalizeChartIndicators({}), ...config } as ChartIndicatorConfig;
  const patches: ChartIndicatorPatch[] = [];
  const view = render(<MaLinesEditor indicators={indicators} onChange={patch => patches.push(patch)} status="已保存" />);
  return { indicators, patches, ...view };
}

test("the MA editor hands back the complete lines on every edit", () => {
  const { patches } = editor();
  expect(screen.getByText("均线 5")).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("已保存");
  fireEvent.click(screen.getByLabelText("第 1 条均线周期增加"));
  expect(patches.at(-1)).toEqual({ maLines: [
    { period: 6, enabled: true }, { period: 10, enabled: true }, { period: 25, enabled: true }, { period: 60, enabled: true }, { period: 200, enabled: true },
  ] });
  fireEvent.click(screen.getByRole("switch", { name: "显示第 2 条均线" }));
  expect(patches.at(-1)).toEqual({ maLines: [
    { period: 5, enabled: true }, { period: 10, enabled: false }, { period: 25, enabled: true }, { period: 60, enabled: true }, { period: 200, enabled: true },
  ] });
  fireEvent.click(screen.getAllByRole("button", { name: "删除" })[4]);
  expect(patches.at(-1)?.maLines).toHaveLength(4);
});

test("at least one MA line stays and the eighth is the cap", () => {
  const single = editor({ maLines: [{ period: 5, enabled: false }] });
  const onlyDelete = screen.getByRole("button", { name: "删除" });
  expect(onlyDelete).toBeDisabled();
  expect(onlyDelete).toHaveAttribute("title", "至少保留一条均线；不想看可以关掉显示");
  single.unmount();
  const seven = editor({ maLines: Array.from({ length: MA_LINES_MAX - 1 }, (_, index) => ({ period: (index + 1) * 2, enabled: true })) });
  fireEvent.click(screen.getByRole("button", { name: /加一条均线/ }));
  const added = seven.patches.at(-1)?.maLines ?? [];
  expect(added).toHaveLength(MA_LINES_MAX);
  expect(added.at(-1)).toEqual({ period: 120, enabled: true });
  seven.unmount();
  editor({ maLines: Array.from({ length: MA_LINES_MAX }, (_, index) => ({ period: (index + 1) * 2, enabled: true })) });
  const capped = screen.getByRole("button", { name: `＋ 加一条均线（最多 ${MA_LINES_MAX} 条）` });
  expect(capped).toBeDisabled();
  expect(capped).toHaveAttribute("title", `最多 ${MA_LINES_MAX} 条均线`);
});

test("the BOLL row gates its parameters behind the switch and edits the whole boll object", () => {
  // The editor reads the saved configuration, so the round trip feeds each patch back in.
  const { patches, rerender, indicators } = editor({ boll: { enabled: false, period: 20, width: 2 } });
  expect(screen.getByLabelText("布林周期")).toBeDisabled();
  expect(screen.getByLabelText("布林宽度")).toBeDisabled();
  fireEvent.click(screen.getByRole("switch", { name: "布林带" }));
  expect(patches.at(-1)).toEqual({ boll: { enabled: true, period: 20, width: 2 } });
  rerender(<MaLinesEditor indicators={{ ...indicators, boll: { enabled: true, period: 20, width: 2 } }} onChange={patch => patches.push(patch)} />);
  fireEvent.click(screen.getByLabelText("布林周期增加"));
  expect(patches.at(-1)).toEqual({ boll: { enabled: true, period: 21, width: 2 } });
  rerender(<MaLinesEditor indicators={{ ...indicators, boll: { enabled: true, period: 21, width: 2 } }} onChange={patch => patches.push(patch)} />);
  fireEvent.click(screen.getByLabelText("布林宽度增加"));
  expect(patches.at(-1)).toEqual({ boll: { enabled: true, period: 21, width: 2.5 } });
});

function paneEditor(index: number, config: Partial<ChartIndicatorConfig> = {}) {
  const indicators = { ...normalizeChartIndicators({}), ...config } as ChartIndicatorConfig;
  const patches: ChartIndicatorPatch[] = [];
  const view = render(<SubPaneEditor index={index} indicators={indicators} onChange={patch => patches.push(patch)} />);
  return { indicators, patches, ...view };
}

test("a pane slot swaps its indicator, refuses types used elsewhere and removes itself", () => {
  const { patches } = paneEditor(0);
  const select = screen.getByLabelText("第 1 格指标") as HTMLSelectElement;
  const options = [...select.options].map(option => ({ value: option.value, disabled: option.disabled, label: option.textContent }));
  expect(options).toContainEqual({ value: "macd", disabled: true, label: "MACD（已在第 2 格）" });
  expect(options).toContainEqual({ value: "rsi", disabled: false, label: "RSI" });
  fireEvent.change(select, { target: { value: "rsi" } });
  expect(patches.at(-1)).toEqual({ subPaneList: ["rsi", "macd"] });
  fireEvent.click(screen.getByRole("button", { name: "移除这一格" }));
  expect(patches.at(-1)).toEqual({ subPaneList: ["macd"] });
});

test("each pane type edits its own parameters", () => {
  const kd = paneEditor(0);
  fireEvent.click(screen.getByLabelText("KD 周期增加"));
  expect(kd.patches.at(-1)).toEqual({ kdParams: { k: 10, d: 3, smooth: 3 } });
  kd.unmount();
  const macd = paneEditor(1);
  fireEvent.click(screen.getByLabelText("MACD 快线增加"));
  expect(macd.patches.at(-1)).toEqual({ macdParams: { fast: 13, slow: 26, signal: 9 } });
  macd.unmount();
  const rsi = paneEditor(0, { subPaneList: ["rsi"] });
  fireEvent.click(screen.getByLabelText("RSI 周期增加"));
  expect(rsi.patches.at(-1)).toEqual({ rsiParams: { period: 15 } });
});

test("the add chooser lists every type, says where a used one lives and stops at three panes", () => {
  const added: string[] = [];
  const indicators = normalizeChartIndicators({});
  const { rerender } = render(<AddPaneChooser indicators={indicators} onAdd={type => added.push(type)} />);
  expect(screen.getByRole("button", { name: "KD副图（已在第 1 格）" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "MACD副图（已在第 2 格）" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "RSI副图" }));
  fireEvent.click(screen.getByRole("button", { name: "持仓量副图" }));
  expect(added).toEqual(["rsi", "oi"]);
  const full = normalizeChartIndicators({ subPaneList: ["kd", "macd", "rsi"] });
  rerender(<AddPaneChooser indicators={full} onAdd={type => added.push(type)} />);
  for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
  expect(screen.getAllByTitle(`最多 ${SUB_PANES_MAX} 格副图`)).toHaveLength(3);
});

test("the popover sits at a fixed position and closes on Esc or an outside pointer press", () => {
  const onClose = vi.fn();
  const anchorRect = { left: 100, top: 40, bottom: 60 } as DOMRect;
  const { container } = render(<ChartPopover title="均线与布林带" anchorRect={anchorRect} onClose={onClose}>
    <button type="button" onClick={() => undefined}>一条均线</button>
  </ChartPopover>);
  const popover = screen.getByRole("dialog", { name: "均线与布林带" });
  expect(popover).toHaveStyle({ position: "fixed" });
  expect(container.querySelector(".fdw-ind-popover-title")).toHaveTextContent("均线与布林带");
  fireEvent.pointerDown(screen.getByRole("button", { name: "一条均线" }));
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.pointerDown(document.body);
  expect(onClose).toHaveBeenCalledOnce();
  onClose.mockClear();
  fireEvent.keyDown(popover, { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce();
});
