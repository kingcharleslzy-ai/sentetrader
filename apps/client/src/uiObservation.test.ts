import { afterEach, expect, it, vi } from "vitest";
import { observeUi } from "./uiObservation";

/** jsdom has no layout: give every element a box and pretend a 800×600 viewport. */
function layout(boxes: Record<string, DOMRect>) {
  Element.prototype.getBoundingClientRect = function () {
    const key = (this as Element).getAttribute("data-box") ?? "";
    return boxes[key] ?? new DOMRect(0, 0, 100, 20);
  };
  Element.prototype.getClientRects = function () { return [this.getBoundingClientRect()] as unknown as DOMRectList; };
  Object.defineProperty(window, "innerWidth", { value: 800, configurable: true });
  Object.defineProperty(window, "innerHeight", { value: 600, configurable: true });
}

afterEach(() => { document.body.innerHTML = ""; vi.restoreAllMocks(); });

it("reads scrolled-out rows of a scrollable region, selects' current value and options, and flags what is on screen", () => {
  document.body.innerHTML = `<div class="fdw-shell">
    <select aria-label="合约筛选"><option value="">全部合约</option><option value="SHFE.rb2610" selected>SHFE.rb2610</option></select>
    <div style="overflow-y: auto" data-box="list">
      <button data-box="row-visible">9 月 14 日</button>
      <button data-box="row-below">9 月 9 日</button>
    </div>
    <div style="overflow: hidden" data-box="clip"><button data-box="clipped">被裁掉</button></div>
  </div>`;
  layout({
    list: new DOMRect(0, 100, 400, 200), "row-visible": new DOMRect(0, 120, 400, 20), "row-below": new DOMRect(0, 900, 400, 20),
    clip: new DOMRect(0, 400, 400, 40), clipped: new DOMRect(0, 480, 400, 20),
  });
  const observation = observeUi({});
  expect(observation.text.split("\n")).toEqual(["SHFE.rb2610", "9 月 14 日", "9 月 9 日"]);
  const rows = observation.controls.map(control => [control.label, control.inViewport]);
  expect(rows).toEqual([["合约筛选", true], ["9 月 14 日", true], ["9 月 9 日", false]]);
  expect(observation.controls[0]).toMatchObject({ role: "select", value: "SHFE.rb2610", options: ["全部合约", "SHFE.rb2610"] });
});

it("a scroll region inside an overflow-hidden panel still yields its scrolled-out content", () => {
  // The AI dock: .fdw-ai (overflow: hidden) wraps .fdw-ai-scroll (overflow-y: auto); the newest
  // reply sits below the panel's box and is reached by scrolling, so it is part of the observation.
  // A hidden box below the scroll container still clips its own children.
  document.body.innerHTML = `<div class="fdw-shell">
    <section style="overflow: hidden" data-box="panel">
      <div style="overflow-y: auto" data-box="scroll">
        <p data-box="reply-top">结论</p>
        <details data-box="sources-below"><summary data-box="sources-summary">来源 · 3 条</summary><p data-box="sources-url">https://www.shfe.com.cn/publicnotice/notice/</p></details>
        <div style="overflow: hidden" data-box="inner-clip"><button data-box="inner-clipped">仍被裁掉</button></div>
      </div>
    </section>
  </div>`;
  layout({
    panel: new DOMRect(400, 0, 400, 600), scroll: new DOMRect(400, 60, 400, 500),
    "reply-top": new DOMRect(400, 80, 380, 20), "sources-below": new DOMRect(400, 1400, 380, 60), "sources-summary": new DOMRect(400, 1400, 380, 20), "sources-url": new DOMRect(400, 1420, 380, 20),
    "inner-clip": new DOMRect(400, 1500, 400, 20), "inner-clipped": new DOMRect(400, 1700, 100, 20),
  });
  const observation = observeUi({});
  expect(observation.text.split("\n")).toEqual(["结论", "来源 · 3 条", "https://www.shfe.com.cn/publicnotice/notice/"]);
  expect(observation.controls.map(control => [control.label, control.inViewport])).toEqual([["来源 · 3 条", false]]);
});

it("reports a control's title when it adds to the label (a greyed quick-skill chip's reason)", () => {
  document.body.innerHTML = `<div class="fdw-shell"><div data-agent-target="quick-skills">
    <button disabled title="当前档位未开通「策略信号解读」">策略信号解读</button>
    <button title="发送：帮我复盘一下今天">复盘与日结</button>
    <button title="设置">设置</button>
  </div></div>`;
  layout({});
  const controls = observeUi({}).controls.map(({ label, disabled, title }) => ({ label, disabled, title }));
  expect(controls).toEqual([
    { label: "策略信号解读", disabled: true, title: "当前档位未开通「策略信号解读」" },
    { label: "复盘与日结", disabled: false, title: "发送：帮我复盘一下今天" },
    { label: "设置", disabled: false, title: undefined },
  ]);
});

it("a resizer reports its current width and the range this window allows (对话栏宽度随窗口)", () => {
  document.body.innerHTML = `<div class="fdw-shell">
    <button type="button" role="separator" aria-label="调整辅助区宽度" aria-valuemin="280" aria-valuemax="960" aria-valuenow="480"></button>
  </div>`;
  layout({});
  expect(observeUi({}).controls[0]).toMatchObject({ label: "调整辅助区宽度", role: "separator", value: 480, min: 280, max: 960 });
});
