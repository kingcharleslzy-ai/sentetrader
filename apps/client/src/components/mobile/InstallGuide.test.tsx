// Protects the install guide's own timing (#522): opening it by hand settles this visit, so it never
// reopens by itself right after a manual close and that close is never counted as 以后再说; an
// automatic open closed by any route counts once, then a 7-day pause, and three closes end it.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { INSTALL_COOLDOWN_MS, INSTALL_RECORD_KEY, type InstallDetection } from "./installEnvironment";

const detection: InstallDetection = { environment: "ios-safari-26" };
const record = () => JSON.parse(localStorage.getItem(INSTALL_RECORD_KEY) ?? "null");
const store = (value: object) => localStorage.setItem(INSTALL_RECORD_KEY, JSON.stringify(value));

/** One page load: a fresh module, so this visit's "already shown" starts over. */
async function visit(engaged = false) {
  vi.resetModules();
  const { InstallGuide, useInstallAutoOpen } = await import("./InstallGuide");
  let setEngaged!: (value: boolean) => void;
  function Workspace() {
    const [open, setOpen] = useState(false);
    const [isEngaged, engage] = useState(engaged);
    setEngaged = engage;
    useInstallAutoOpen({ enabled: true, detection, signedIn: true, engaged: isEngaged, holdingToTalk: false, pageOpen: false,
      pendingOrder: false, installPageOpen: open, open: () => setOpen(true) });
    return <>
      <button type="button" onClick={() => setOpen(true)}>添加到手机桌面</button>
      {/* A drag down from the sheet's head and `mobilePage: null` both clear the page the same way. */}
      <button type="button" onClick={() => setOpen(false)}>清掉整屏页</button>
      {open ? <InstallGuide detection={detection} onClose={() => setOpen(false)} /> : null}
    </>;
  }
  render(<Workspace />);
  return { engage: (value: boolean) => act(() => setEngaged(value)) };
}

const closes: Record<string, () => void> = {
  "关闭按钮": () => fireEvent.click(screen.getByRole("button", { name: "关闭安装引导" })),
  "以后再说": () => fireEvent.click(screen.getByRole("button", { name: "以后再说" })),
  "遮罩": () => fireEvent.click(document.querySelector(".fdm-sheet-backdrop")!),
  "下拖 / mobilePage:null": () => fireEvent.click(screen.getByRole("button", { name: "清掉整屏页" })),
};
const guide = () => screen.queryByTestId("mobile-install");

beforeEach(() => localStorage.clear());
afterEach(() => cleanup());

it.each(Object.keys(closes))("a guide opened by hand and closed by %s stays closed and is not counted", async (route) => {
  const page = await visit();
  expect(guide()).toBeNull();
  expect(record()).toMatchObject({ opens: 1, dismissals: 0 });
  fireEvent.click(screen.getByRole("button", { name: "添加到手机桌面" }));
  expect(guide()).not.toBeNull();
  page.engage(true);  // an AI reply finished while the hand-opened guide was showing
  expect(record()).toMatchObject({ engaged: true, dismissals: 0 });
  closes[route]();
  expect(guide()).toBeNull();
  expect(record()).toMatchObject({ dismissals: 0 });
  expect(record().lastDismissedAt).toBeUndefined();
  page.engage(false);
  page.engage(true);
  expect(guide()).toBeNull();
});

it.each(Object.keys(closes))("an automatic guide closed by %s counts once and pauses for 7 days", async (route) => {
  store({ opens: 1, dismissals: 0 });
  await visit();
  expect(guide()).not.toBeNull();
  closes[route]();
  expect(guide()).toBeNull();
  expect(record()).toMatchObject({ opens: 2, dismissals: 1 });
  expect(Date.now() - record().lastDismissedAt).toBeLessThan(1000);
  // Reopened by hand in the same visit and closed again: still one.
  fireEvent.click(screen.getByRole("button", { name: "添加到手机桌面" }));
  closes[route]();
  expect(record().dismissals).toBe(1);
  cleanup();
  await visit();
  expect(guide()).toBeNull();  // within the 7 days
});

it("three automatic closes end it, even after the pause", async () => {
  for (let round = 1; round <= 3; round += 1) {
    store({ ...(record() ?? { opens: 1 }), lastDismissedAt: Date.now() - INSTALL_COOLDOWN_MS - 1, dismissals: round - 1 });
    await visit();
    expect(guide()).not.toBeNull();
    closes["以后再说"]();
    expect(record().dismissals).toBe(round);
    cleanup();
  }
  store({ ...record(), lastDismissedAt: Date.now() - INSTALL_COOLDOWN_MS - 1 });
  await visit();
  expect(guide()).toBeNull();
});
