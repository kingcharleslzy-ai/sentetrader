import { render } from "@testing-library/react";
import { useRef } from "react";
import { expect, test } from "vitest";
import { HEADER_FIT_MAX_LEVEL, headerContentOverflows, headerFitClasses, resolveHeaderFitLevel, useHeaderFit } from "./useHeaderFit";

/** Stubbed header: `overflowAt` decides from the applied level whether the header overflows. */
function Harness({ overflowAt, contentKey }: { overflowAt: (level: number) => boolean; contentKey: string }) {
  const header = useRef<HTMLElement | null>(null);
  const level = useHeaderFit(header, contentKey);
  const stub = useRef(overflowAt);
  stub.current = overflowAt;
  const attach = (node: HTMLElement | null) => {
    if (!node || node.dataset.stubbed) return;
    node.dataset.stubbed = "true";
    Object.defineProperty(node, "scrollWidth", { configurable: true, get() { return stub.current(Number(node.dataset.fit)) ? 200 : 100; } });
    Object.defineProperty(node, "clientWidth", { configurable: true, get() { return 100; } });
  };
  return <header ref={node => { header.current = node; attach(node); }} data-fit={level} className={headerFitClasses(level)}><span>cell</span></header>;
}

/** Layout stubs: clientWidth/scrollWidth per cell plus a fake rect (jsdom has no layout). */
interface Geometry { clientWidth?: number; scrollWidth?: number; left?: number; width?: number; displayed?: boolean; }

function stubCell(element: HTMLElement, geometry: Geometry = {}) {
  const { clientWidth = 100, scrollWidth = clientWidth, left = 0, width = clientWidth, displayed = true } = geometry;
  Object.defineProperty(element, "clientWidth", { configurable: true, get: () => clientWidth });
  Object.defineProperty(element, "scrollWidth", { configurable: true, get: () => scrollWidth });
  element.getClientRects = () => (displayed ? [{}] as unknown as DOMRectList : [] as unknown as DOMRectList);
  element.getBoundingClientRect = () => ({ left, right: left + width, width, top: 0, height: 10 } as DOMRect);
}

/**
 * A header skeleton with every probed cell stubbable. Defaults describe a roomy level-0
 * header: brand 120, nav 700 (tabs 400 + divider + icon button), status 150, and the
 * picker hidden the way it is below level 3.
 */
type CellName = "header" | "brand" | "nav" | "tabs" | "add" | "addIcon" | "picker" | "divider" | "view" | "viewIcon" | "status" | "warningSpan" | "warningText" | "equity";

function probeHeader(overrides: Partial<Record<CellName, Geometry>> = {}) {
  const root = document.createElement("div");
  root.innerHTML = `
    <header class="fdw-header">
      <div class="fdw-brand"></div>
      <nav>
        <div class="fdw-contract-tabs"><button class="fdw-contract-add"><svg></svg></button></div>
        <div class="fdw-contract-picker"></div>
        <i class="fdw-nav-divider"></i>
        <button class="fdw-nav-view"><svg></svg></button>
      </nav>
      <div class="fdw-header-status"><span class="warning"><span class="fdw-warning-text"></span></span><button class="fdw-account-chip"></button></div>
    </header>`;
  const header = root.querySelector("header") as HTMLElement;
  const cells = {
    header,
    brand: header.querySelector(".fdw-brand") as HTMLElement,
    nav: header.querySelector("nav") as HTMLElement,
    tabs: header.querySelector(".fdw-contract-tabs") as HTMLElement,
    add: header.querySelector(".fdw-contract-add") as HTMLElement,
    addIcon: header.querySelector(".fdw-contract-add > svg") as HTMLElement,
    picker: header.querySelector(".fdw-contract-picker") as HTMLElement,
    divider: header.querySelector(".fdw-nav-divider") as HTMLElement,
    view: header.querySelector(".fdw-nav-view") as HTMLElement,
    viewIcon: header.querySelector(".fdw-nav-view > svg") as HTMLElement,
    status: header.querySelector(".fdw-header-status") as HTMLElement,
    warningSpan: header.querySelector(".fdw-header-status > .warning") as HTMLElement,
    warningText: header.querySelector(".fdw-warning-text") as HTMLElement,
    equity: header.querySelector(".fdw-header-status > .fdw-account-chip") as HTMLElement,
  };
  const defaults: Record<keyof typeof cells, Geometry> = {
    header: { clientWidth: 1000 },
    brand: { clientWidth: 120, left: 0 },
    nav: { clientWidth: 700, left: 140 },
    tabs: { clientWidth: 400, left: 140 },
    add: { clientWidth: 30, left: 510 },
    addIcon: { clientWidth: 14 },
    picker: { displayed: false },
    divider: { clientWidth: 1, left: 560, width: 1 },
    view: { clientWidth: 60, left: 580 },
    viewIcon: { clientWidth: 14 },
    status: { clientWidth: 150, left: 850 },
    warningSpan: { clientWidth: 80, left: 850 },
    warningText: { clientWidth: 60 },
    equity: { clientWidth: 60, left: 940 },
  };
  (Object.keys(defaults) as (keyof typeof defaults)[]).forEach(key => stubCell(cells[key], overrides[key] ?? defaults[key]));
  return cells;
}

test("空间充足时等级保持 0，所有内容完整显示", () => {
  expect(resolveHeaderFitLevel(() => true)).toBe(0);
  const view = render(<Harness overflowAt={() => false} contentKey="roomy" />);
  expect(view.container.firstChild).toHaveAttribute("data-fit", "0");
  expect(view.container.firstChild).not.toHaveClass("fdw-fit-hide-text");
});

test("逐步变窄：等级从 1 升到 6，类名按等级累加", () => {
  for (let needed = 1; needed <= HEADER_FIT_MAX_LEVEL; needed += 1) {
    expect(resolveHeaderFitLevel(level => level >= needed)).toBe(needed);
  }
  // Cumulative: a level-5 header also carries the level-1 to level-4 rules; the AI 指令栏 and 美元 give way first.
  expect(headerFitClasses(1)).toBe("fdw-fit-compact-ai");
  expect(headerFitClasses(5)).toBe("fdw-fit-compact-ai fdw-fit-hide-usd fdw-fit-hide-text fdw-fit-hide-brand fdw-fit-contract-menu");
  expect(headerFitClasses(HEADER_FIT_MAX_LEVEL)).toContain("fdw-fit-hide-equity");
});

test("连最高级也放不下时停在 6；警告与恢复按钮不在任何等级的隐藏范围里", () => {
  expect(resolveHeaderFitLevel(() => false)).toBe(HEADER_FIT_MAX_LEVEL);
  for (let level = 0; level <= HEADER_FIT_MAX_LEVEL; level += 1) {
    expect(headerFitClasses(level)).not.toMatch(/warning|connection|recovery|stop/);
  }
});

test("宽度回升时等级回落：每轮都从 0 重新判定", () => {
  // 上一轮 4 才放得下；空间恢复后 1 就够，新的一轮直接回到 1。
  expect(resolveHeaderFitLevel(level => level >= 4)).toBe(4);
  expect(resolveHeaderFitLevel(level => level >= 1)).toBe(1);
});

test("hook 一轮同步测量收敛到最终等级，宽度回升后回落", () => {
  const view = render(<Harness overflowAt={level => level < 5} contentKey="mid" />);
  expect(view.container.firstChild).toHaveAttribute("data-fit", "5");
  expect(view.container.firstChild).toHaveClass("fdw-fit-contract-menu");
  // Space comes back: the next pass restarts from 0 and settles on 1.
  view.rerender(<Harness overflowAt={level => level < 1} contentKey="wide" />);
  expect(view.container.firstChild).toHaveAttribute("data-fit", "1");
  expect(view.container.firstChild).not.toHaveClass("fdw-fit-contract-menu");
  // Content grows without new space: the pass climbs again, capping at 6.
  view.rerender(<Harness overflowAt={() => true} contentKey="grown" />);
  expect(view.container.firstChild).toHaveAttribute("data-fit", "6");
});

test("内容放得下：header、子容器、相邻盒子与宽度都正常时不判溢出", () => {
  expect(headerContentOverflows(probeHeader().header)).toBe(false);
});

test("header 本身不溢出但子容器溢出也判溢出：合约标签被压缩后内容超出盒子", () => {
  // 实测缺陷：header 三列各自 scrollWidth==clientWidth，但 .fdw-contract-tabs 261/354。
  expect(headerContentOverflows(probeHeader({ tabs: { clientWidth: 261, scrollWidth: 354, left: 140 } }).header)).toBe(true);
});

test("相邻盒子重叠判溢出：标签盒压到复盘/回放按钮上", () => {
  // tabs 右缘 540，分隔线却画在 500：prev.right > next.left + 1。
  expect(headerContentOverflows(probeHeader({ divider: { clientWidth: 1, left: 500, width: 1 } }).header)).toBe(true);
});

test("可见子元素被挤成 0 宽判溢出：钉住入口与导航按钮都被压扁", () => {
  expect(headerContentOverflows(probeHeader({ add: { clientWidth: 30, width: 0 } }).header)).toBe(true);
  expect(headerContentOverflows(probeHeader({ view: { clientWidth: 60, width: 0 } }).header)).toBe(true);
  // 等级 5 收起权益与下载后，status 没有可见内容，塌成 0 宽不算缺陷。
  expect(headerContentOverflows(probeHeader({
    status: { clientWidth: 150, left: 850, width: 0 },
    warningSpan: { displayed: false },
    warningText: { displayed: false },
    equity: { displayed: false },
  }).header)).toBe(false);
});

test("警告文字省略号截断不算溢出，等级隐藏的容器也不算", () => {
  // 警告文字自己滚动了（截断），但各容器与盒子都放得下。
  expect(headerContentOverflows(probeHeader({ warningText: { clientWidth: 60, scrollWidth: 120 } }).header)).toBe(false);
  // 等级 5 以下隐藏的下拉即便“溢出”也不参与判定（display:none）。
  expect(headerContentOverflows(probeHeader({ picker: { clientWidth: 90, scrollWidth: 400, displayed: false } }).header)).toBe(false);
});
