import { act, cleanup, render, screen } from "@testing-library/react";
import { useState } from "react";
import { disclosureStates, latestCardDisclosure, resetUiSurfaces, uiSurface, useDisclosureSurface } from "./uiSurfaces";

afterEach(() => { cleanup(); resetUiSurfaces(); });

function Details({ name, id, available = true }: { name: string; id?: string; available?: boolean }) {
  const [open, setOpen] = useDisclosureSurface(name, { id, available });
  return <details open={open} onToggle={event => setOpen(event.currentTarget.open)} data-testid={id ? `${name}:${id}` : name}><summary>{name}</summary>body</details>;
}

test("a disclosure registers while available, reports its state and opens on command", () => {
  const view = render(<Details name="protection-status" />);
  expect(disclosureStates()).toEqual({ "protection-status": false });
  act(() => { uiSurface("disclosure:protection-status")!.apply!(true); });
  expect(screen.getByTestId("protection-status")).toHaveAttribute("open");
  expect(disclosureStates()).toEqual({ "protection-status": true });
  view.rerender(<Details name="protection-status" available={false} />);
  expect(uiSurface("disclosure:protection-status")).toBeUndefined();
  expect(disclosureStates()).toEqual({});
});

test("card disclosures apply to the most recent card unless an id is given", () => {
  render(<><Details name="tool-card" id="c1" /><Details name="tool-card" id="c2" /></>);
  expect(latestCardDisclosure("tool-card")).toBe("disclosure:tool-card:c2");
  act(() => { uiSurface(latestCardDisclosure("tool-card")!)!.apply!(true); });
  expect(disclosureStates()).toEqual({ "tool-card": true, "tool-card:c1": false, "tool-card:c2": true });
  // Re-registering after a state change keeps c2 the most recent card.
  act(() => { uiSurface("disclosure:tool-card:c1")!.apply!(true); });
  expect(latestCardDisclosure("tool-card")).toBe("disclosure:tool-card:c2");
  expect(disclosureStates()["tool-card:c1"]).toBe(true);
  expect(latestCardDisclosure("diff-card")).toBeUndefined();
});

test("a component that owns the state reports it and handles the command in its own apply", () => {
  function Custom() {
    const [design, setDesign] = useState(false);
    useDisclosureSurface("replay-design", { state: design, apply: wanted => setDesign(wanted) });
    return <span data-testid="design">{String(design)}</span>;
  }
  render(<Custom />);
  expect(disclosureStates()).toEqual({ "replay-design": false });
  act(() => { uiSurface("disclosure:replay-design")!.apply!(true); });
  expect(screen.getByTestId("design")).toHaveTextContent("true");
  expect(disclosureStates()).toEqual({ "replay-design": true });
  act(() => { uiSurface("disclosure:replay-design")!.apply!(false); });
  expect(disclosureStates()).toEqual({ "replay-design": false });
});

test("a card's 详情 fold registers as card-details by card id and opens on command", async () => {
  const { CardShell } = await import("./components/CardShell");
  render(<CardShell icon={null} title="买入开仓 螺纹钢2701 2 手 @ 3126 限价" id="trade-1" testId="card" details={<p>SHFE.rb2701</p>} />);
  expect(disclosureStates()).toEqual({ "card-details": false, "card-details:trade-1": false });
  act(() => { uiSurface(latestCardDisclosure("card-details")!)!.apply!(true); });
  expect(screen.getByTestId("card-details")).toHaveAttribute("open");
  expect(disclosureStates()["card-details:trade-1"]).toBe(true);
});


test("一屏超过 20 条翻译过的行：每条 flash-orig / news-orig 折叠都进快照（不截前 20）", () => {
  render(<>{Array.from({ length: 25 }, (_, index) => <Details key={index} name="flash-orig" id={`en-${index}`} />)}</>);
  for (const index of [0, 20, 24]) {
    expect(disclosureStates()[`flash-orig:en-${index}`]).toBe(false);
    act(() => { uiSurface(`disclosure:flash-orig:en-${index}`)!.apply!(true); });
    expect(disclosureStates()[`flash-orig:en-${index}`]).toBe(true);
  }
  expect(Object.keys(disclosureStates()).filter(key => key.startsWith("flash-orig:")).length).toBe(25);
});

test.each(["library", "replay"])("50条策略仍能控制并观察首条菜单和删除确认：%s", async prefix => {
  const { StrategyItemActions } = await import("./components/StrategyItemActions");
  let deleted = 0;
  render(<>{Array.from({ length: 50 }, (_, index) => <StrategyItemActions key={index} id={`${prefix}:${index}`} name={`策略${index}`} disabled={false}
    onDelete={async () => { deleted += 1; }}><span>策略{index}</span></StrategyItemActions>)}</>);
  for (const index of [0, 49]) {
    for (const name of ["strategy-actions", "strategy-delete-confirm"]) {
      const key = `${name}:${prefix}:${index}`;
      expect(disclosureStates()[key]).toBe(false);
      act(() => { uiSurface(`disclosure:${key}`)!.apply!(true); });
      expect(disclosureStates()[key]).toBe(true);
      expect(screen.getByRole("menu", { name: `策略${index} 的操作` })).toBeVisible();
      if (name === "strategy-delete-confirm") expect(screen.getByRole("button", { name: "确认删除" })).toBeVisible();
      act(() => { uiSurface(`disclosure:${key}`)!.apply!(false); });
      expect(disclosureStates()[key]).toBe(false);
    }
  }
  expect(deleted).toBe(0);
});
