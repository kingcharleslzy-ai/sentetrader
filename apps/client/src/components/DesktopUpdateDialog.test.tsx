import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { DesktopUpdateDialog } from "./DesktopUpdateDialog";
import type { DesktopUpdaterController, DesktopUpdateInfo, DesktopUpdatePhase } from "../hooks/useDesktopUpdater";
import { resetUiSurfaces, uiSurface } from "../uiSurfaces";

function updater(info: DesktopUpdateInfo, phase: DesktopUpdatePhase, message?: string): DesktopUpdaterController {
  return { phase, info, message, check: vi.fn(), install: vi.fn(), cancel: vi.fn() };
}

const available = (version: string): DesktopUpdateInfo => ({ currentVersion: "1.6.6", available: true, version, notes: "修复若干问题", pubDate: "2026-09-23T08:00:00Z" });

beforeEach(() => resetUiSurfaces());

function dialogElement(): HTMLDialogElement {
  return screen.getByTestId("desktop-update-dialog") as HTMLDialogElement;
}

test("stays closed while the app is current or unsupported", () => {
  const { rerender } = render(<DesktopUpdateDialog updater={updater({ currentVersion: "1.6.6", available: false }, "current")} />);
  expect(dialogElement().hasAttribute("open")).toBe(false);
  rerender(<DesktopUpdateDialog updater={updater({ currentVersion: "1.6.6", available: false }, "unsupported")} />);
  expect(dialogElement().hasAttribute("open")).toBe(false);
});

test("opens on an available version; 稍后 silences it for this run, a newer version pops again", () => {
  const { rerender } = render(<DesktopUpdateDialog updater={updater(available("1.6.7"), "available")} />);
  expect(dialogElement().hasAttribute("open")).toBe(true);
  expect(screen.getByText("发现新版本 1.6.7")).toBeVisible();
  expect(screen.getByText(/当前版本 1\.6\.6/)).toBeVisible();
  expect(screen.getByText(/发布于 2026-09-23/)).toBeVisible();
  expect(screen.getByText("修复若干问题")).toBeVisible();

  act(() => screen.getByRole("button", { name: "稍后" }).click());
  expect(dialogElement().hasAttribute("open")).toBe(false);
  // The same version re-reported (e.g. the periodic check) must not re-pop it.
  rerender(<DesktopUpdateDialog updater={updater(available("1.6.7"), "available")} />);
  expect(dialogElement().hasAttribute("open")).toBe(false);
  rerender(<DesktopUpdateDialog updater={updater(available("1.6.8"), "available")} />);
  expect(dialogElement().hasAttribute("open")).toBe(true);
  expect(screen.getByText("发现新版本 1.6.8")).toBeVisible();
});

test("Esc works like 稍后", () => {
  const { rerender } = render(<DesktopUpdateDialog updater={updater(available("1.6.7"), "available")} />);
  expect(dialogElement().hasAttribute("open")).toBe(true);
  act(() => dialogElement().dispatchEvent(new Event("cancel", { bubbles: true, cancelable: true })));
  expect(dialogElement().hasAttribute("open")).toBe(false);
  rerender(<DesktopUpdateDialog updater={updater(available("1.6.7"), "available")} />);
  expect(dialogElement().hasAttribute("open")).toBe(false);
});

test("立即更新 installs and the window follows the download progress", () => {
  const install = vi.fn();
  const start = { phase: "available" as DesktopUpdatePhase, info: available("1.6.7"), check: vi.fn(), install, cancel: vi.fn() };
  const { rerender } = render(<DesktopUpdateDialog updater={start} />);
  act(() => screen.getByRole("button", { name: "立即更新 1.6.7" }).click());
  expect(install).toHaveBeenCalledOnce();
  rerender(<DesktopUpdateDialog updater={{ ...start, phase: "downloading", progress: { stage: "downloading", downloadedBytes: 40, totalBytes: 80, percent: 50 } }} />);
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  expect(screen.getByRole("button", { name: "暂停下载" })).toBeEnabled();
  // Closing mid-download only minimizes: the progress stays visible in settings.
  act(() => screen.getByRole("button", { name: "稍后" }).click());
  expect(dialogElement().hasAttribute("open")).toBe(false);
});

test("an install failure shows the message with a retry", () => {
  const install = vi.fn();
  const start = { phase: "available" as DesktopUpdatePhase, info: available("1.6.7"), check: vi.fn(), install, cancel: vi.fn() };
  const { rerender } = render(<DesktopUpdateDialog updater={start} />);
  act(() => screen.getByRole("button", { name: "立即更新 1.6.7" }).click());
  rerender(<DesktopUpdateDialog updater={{ ...start, phase: "error", message: "更新包下载失败，请稍后重试" }} />);
  expect(screen.getByRole("alert")).toHaveTextContent("更新包下载失败");
  act(() => screen.getByRole("button", { name: "重试下载" }).click());
  expect(install).toHaveBeenCalledTimes(2);  // 立即更新 + 重试下载
});

test("the dialog command opens only when an update is actually available", () => {
  const { rerender } = render(<DesktopUpdateDialog updater={updater({ currentVersion: "1.6.6", available: false }, "current")} />);
  const surface = uiSurface("dialog:update-dialog");
  expect(surface).toBeDefined();
  expect(() => surface!.apply!({ open: true })).toThrow(/当前没有可用的新版本/);
  expect(dialogElement().hasAttribute("open")).toBe(false);
  rerender(<DesktopUpdateDialog updater={updater(available("1.6.7"), "available")} />);
  act(() => surface!.apply!({ open: true }));
  expect(dialogElement().hasAttribute("open")).toBe(true);
  act(() => surface!.apply!({ open: false }));
  expect(dialogElement().hasAttribute("open")).toBe(false);
});
