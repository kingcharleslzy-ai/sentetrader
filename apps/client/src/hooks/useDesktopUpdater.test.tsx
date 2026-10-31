import { act, render, screen } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import { DesktopUpdateProgress } from "../components/DesktopUpdateProgress";
import { useDesktopUpdater, type DesktopUpdateProgressInfo } from "./useDesktopUpdater";

const mocks = vi.hoisted(() => ({ invoke: vi.fn(), listen: vi.fn() }));
let progressHandler: ((event: { payload: DesktopUpdateProgressInfo }) => void) | undefined;

vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: mocks.listen }));

beforeEach(() => {
  progressHandler = undefined;
  mocks.listen.mockReset();
  mocks.listen.mockImplementation((_event, handler) => {
    progressHandler = handler;
    return Promise.resolve(vi.fn());
  });
});

function Harness() {
  const updater = useDesktopUpdater(true);
  return (
    <div>
      <span>{updater.phase}</span>
      <span>{updater.info?.version}</span>
      <span>{updater.message}</span>
      <DesktopUpdateProgress updater={updater} />
      <span data-testid="progress">{updater.progress?.percent}</span>
      <button type="button" onClick={() => void updater.install()}>install</button>
    </div>
  );
}

test("checks once after startup and delegates signed installation to Tauri", async () => {
  vi.useFakeTimers();
  mocks.invoke.mockReset();
  mocks.invoke.mockImplementation((command: string) => {
    if (command === "check_desktop_update") return Promise.resolve({
      currentVersion: "0.8.0",
      available: true,
      version: "0.8.1",
      notes: "update"
    });
    return Promise.resolve();
  });
  render(<Harness />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_200);
    await Promise.resolve();
  });
  expect(screen.getByText("available")).toBeVisible();
  expect(screen.getByText("0.8.1")).toBeVisible();
  expect(mocks.invoke).toHaveBeenCalledWith("check_desktop_update");

  await act(async () => {
    screen.getByRole("button", { name: "install" }).click();
    await Promise.resolve();
  });
  expect(mocks.invoke).toHaveBeenCalledWith("install_desktop_update", { expectedVersion: "0.8.1" });
  vi.useRealTimers();
});

test("reports download percentage and the installing stage from desktop events", async () => {
  vi.useFakeTimers();
  let finishInstall: (() => void) | undefined;
  mocks.invoke.mockReset();
  mocks.invoke.mockImplementation((command: string) => {
    if (command === "check_desktop_update") return Promise.resolve({
      currentVersion: "0.8.0",
      available: true,
      version: "0.8.1"
    });
    return new Promise<void>((resolve) => { finishInstall = resolve; });
  });
  render(<Harness />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_200);
    await Promise.resolve();
  });
  await act(async () => {
    screen.getByRole("button", { name: "install" }).click();
    await Promise.resolve();
  });
  act(() => progressHandler?.({ payload: { stage: "downloading", downloadedBytes: 50, totalBytes: 100, percent: 50 } }));
  expect(screen.getByText("downloading")).toBeVisible();
  expect(screen.getByTestId("progress")).toHaveTextContent("50");

  act(() => progressHandler?.({ payload: { stage: "installing", downloadedBytes: 100, totalBytes: 100, percent: 100 } }));
  expect(screen.getByText("installing")).toBeVisible();
  act(() => finishInstall?.());
  vi.useRealTimers();
});

test("requires a fresh confirmation when the update version changes", async () => {
  vi.useFakeTimers();
  mocks.invoke.mockReset();
  mocks.invoke.mockImplementation((command: string) => {
    if (command === "check_desktop_update") return Promise.resolve({
      currentVersion: "0.8.2",
      available: true,
      version: "0.8.5",
      notes: "update"
    });
    return Promise.reject("desktop_update_version_changed");
  });
  render(<Harness />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_200);
    await Promise.resolve();
  });
  await act(async () => {
    screen.getByRole("button", { name: "install" }).click();
    await Promise.resolve();
  });
  expect(screen.getByText("更新版本已变化，请重新检查并确认")).toBeVisible();
  expect(screen.getByText("error")).toBeVisible();
  vi.useRealTimers();
});


test("pause retains the offered version and exposes incremental transfer progress", async () => {
  vi.useFakeTimers();
  let rejectDownload: ((reason: string) => void) | undefined;
  mocks.invoke.mockReset();
  mocks.invoke.mockImplementation((command: string) => {
    if (command === "check_desktop_update") return Promise.resolve({ currentVersion: "1.3.1", available: true, version: "1.4.0" });
    if (command === "cancel_desktop_update") { rejectDownload?.("desktop_update_cancelled"); return Promise.resolve(); }
    return new Promise<void>((_resolve, reject) => { rejectDownload = reject; });
  });
  render(<Harness />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
  await act(async () => { screen.getByRole("button", { name: "install" }).click(); });
  act(() => progressHandler?.({ payload: { stage: "downloading", downloadedBytes: 50, totalBytes: 100, percent: 50,
    source: "OSS", incremental: true, bytesPerSecond: 1048576, remainingSeconds: 8 } }));
  expect(screen.getByText(/增量更新 · OSS线路/)).toHaveTextContent("1.00 MB/s");
  await act(async () => { screen.getByRole("button", { name: "暂停下载" }).click(); });
  expect(screen.getByText("available")).toBeVisible();
  expect(screen.getByText("1.4.0")).toBeVisible();
  expect(screen.getByText("下载已暂停，再次下载可继续")).toBeVisible();
  vi.useRealTimers();
});


const SIX_HOURS = 6 * 60 * 60 * 1000;

test("re-checks silently every 6 hours but never while a download runs", async () => {
  vi.useFakeTimers();
  let version = "1.6.7";
  mocks.invoke.mockReset();
  const checks = vi.fn();
  mocks.invoke.mockImplementation((command: string) => {
    if (command === "check_desktop_update") {
      checks();
      return Promise.resolve({ currentVersion: "1.6.6", available: true, version });
    }
    // install never settles → the updater stays in the downloading phase
    return new Promise<void>(() => undefined);
  });
  render(<Harness />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1_200); });
  expect(checks).toHaveBeenCalledOnce();

  await act(async () => { await vi.advanceTimersByTimeAsync(SIX_HOURS); });
  expect(checks).toHaveBeenCalledTimes(2);
  // A newer version from the background check replaces the offered one.
  version = "1.6.8";
  await act(async () => { await vi.advanceTimersByTimeAsync(SIX_HOURS); });
  expect(checks).toHaveBeenCalledTimes(3);
  expect(screen.getByText("1.6.8")).toBeVisible();

  await act(async () => { screen.getByRole("button", { name: "install" }).click(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(SIX_HOURS); });
  expect(screen.getByText("downloading")).toBeVisible();
  expect(checks).toHaveBeenCalledTimes(3);
  vi.useRealTimers();
});

test("a failed silent re-check keeps the offered update on screen", async () => {
  vi.useFakeTimers();
  let calls = 0;
  mocks.invoke.mockReset();
  mocks.invoke.mockImplementation((command: string) => {
    if (command === "check_desktop_update") {
      calls += 1;
      if (calls === 1) return Promise.resolve({ currentVersion: "1.6.6", available: true, version: "1.6.7", notes: "update" });
      return Promise.reject("desktop_update_download_failed");
    }
    return Promise.resolve();
  });
  render(<Harness />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1_200); });
  await act(async () => { await vi.advanceTimersByTimeAsync(SIX_HOURS); });
  expect(calls).toBe(2);
  expect(screen.getByText("available")).toBeVisible();
  expect(screen.getByText("1.6.7")).toBeVisible();
  expect(screen.queryByText("error")).not.toBeInTheDocument();
  expect(screen.queryByText("暂时无法连接更新服务")).not.toBeInTheDocument();
  vi.useRealTimers();
});
