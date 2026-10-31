import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { emptyRow, operandFor, type OperandForm, type RowForm } from "../strategyEditor";
import { ModifierChips, PickerPanel } from "./IndicatorPicker";

/**
 * The picker's line buttons (肯特纳上/中/下轨, shared by the desktop panel and the phone's sheet):
 * switching a line is a patch of the current operand, never a rebuild from defaults — a rebuild
 * dropped the chosen 中轨 algorithm (basis), silently changing MA to the engine's default EMA
 * (#659). Re-picking the line already on changes nothing at all.
 */
afterEach(cleanup);

const start = () => operandFor("keltner_up", { period: 30, k: 2, basis: "ma", transform: "average", transformN: 6, barsAgo: 3 });

function sheet(initial: OperandForm, onChange: (value: OperandForm) => void) {
  return render(<PickerPanel side="left" value={initial} op="gt" planTimeframe={300} spread={false}
    layout="mobile" title="左边" onChange={onChange} />);
}

it("switching and re-picking keltner lines keeps the basis, average and barsAgo the user set", () => {
  const changes: OperandForm[] = [];
  const onChange = vi.fn((value: OperandForm) => changes.push(value));
  function Host() {
    const [value, setValue] = useState(start());
    return <PickerPanel side="left" value={value} op="gt" planTimeframe={300} spread={false}
      layout="mobile" title="左边" onChange={next => { onChange(next); setValue(next); }} />;
  }
  render(<Host />);
  const line = (label: string) => screen.getByRole("button", { name: label });
  // 上 → 下 → 上: only the indicator changes; everything the user set survives both switches.
  for (const [label, indicator] of [["下轨", "keltner_down"], ["上轨", "keltner_up"]] as const) {
    fireEvent.click(line(label));
    expect(changes.at(-1)).toMatchObject({ indicator, period: 30, k: 2, basis: "ma", transform: "average", transformN: 6, barsAgo: 3 });
  }
  // Re-picking the line already on is a no-op: no further change, the operand stays field for field.
  const before = changes.length;
  fireEvent.click(line("上轨"));
  expect(changes.length).toBe(before);
  expect(changes.at(-1)).toEqual(start());
  // A plain k edit goes through the same patch: only k moves, basis/average/barsAgo survive.
  fireEvent.change(screen.getByLabelText("倍数"), { target: { value: "2.5" } });
  expect(changes.at(-1)).toMatchObject({ indicator: "keltner_up", k: 2.5, basis: "ma", transform: "average", transformN: 6, barsAgo: 3, period: 30 });
  // The mid line has no k of its own; its switch still keeps basis/average/barsAgo.
  fireEvent.click(line("中轨"));
  expect(changes.at(-1)).toMatchObject({ indicator: "keltner_mid", period: 30, k: null, basis: "ma", transform: "average", transformN: 6, barsAgo: 3 });
});

it("an EMA-basis keltner stays EMA across a line switch (the default is not turned into ma)", () => {
  const changes: OperandForm[] = [];
  sheet(operandFor("keltner_up", { k: 1.5 }), value => changes.push(value));
  fireEvent.click(screen.getByRole("button", { name: "下轨" }));
  expect(changes.at(-1)).toMatchObject({ indicator: "keltner_down", basis: null, k: 1.5 });
});

it("a 修饰's number can be typed through a leading 1 (12, 10): the 修饰 stays on and the row gets the finished number", () => {
  let row: RowForm = { ...emptyRow(), within: 3, consecutive: 2 };
  function Host() {
    const [value, setValue] = useState(row);
    return <ModifierChips row={value} name="第 1 条" onChange={next => { row = next; setValue(next); }} />;
  }
  render(<Host />);
  const within = screen.getByRole("spinbutton", { name: "第 1 条 · N 根内" });
  fireEvent.change(within, { target: { value: "1" } });
  expect(screen.getByRole("button", { name: "去掉第 1 条的 N 根内发生" })).toBeInTheDocument();
  expect(row.within).toBe(3);
  fireEvent.change(within, { target: { value: "12" } });
  expect(row.within).toBe(12);
  const streak = screen.getByRole("spinbutton", { name: "第 1 条 · 连续 N 根" });
  fireEvent.change(streak, { target: { value: "1" } });
  fireEvent.change(streak, { target: { value: "10" } });
  expect(row.consecutive).toBe(10);
  // Left half-typed (or out of range), the box shows the number in force again.
  fireEvent.change(streak, { target: { value: "" } });
  fireEvent.blur(streak);
  expect(streak).toHaveValue(10);
  expect(row.consecutive).toBe(10);
});
