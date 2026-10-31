import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { AiOrb, savedOrbPosition } from "./AiActivity";
import { applyUiSurface, disclosureStates, resetUiSurfaces, uiSurfaceStates } from "../uiSurfaces";
import { readQueryClient } from "../runtimeClient";
import { voiceCallStore } from "../voiceRealtime";

const wrapper = ({ children }: { children: React.ReactNode }) => <QueryClientProvider client={readQueryClient}>{children}</QueryClientProvider>;
const config = { showAiActivity: true, showTradeMarkers: true, showMarketBriefs: false, extensions: [] };
const activity = (orders: unknown[] = []) => ({ environment: "simulation", replayId: null, dataStatus: "normal", ageSeconds: 1, orders, fills: [], strategies: [] });
const props = { baseUrl: "http://runtime", symbol: "RB", replay: false, error: "", config, onConfigChange: vi.fn(), onOpenChat: vi.fn(), onBriefStatusChange: vi.fn() };

beforeEach(() => { resetUiSurfaces(); voiceCallStore.reset(); vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [] }), { headers: { "Content-Type": "application/json" } }))); });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test("with voice: click starts the call, the same surface carries aiActivityCard and the voice disclosures, the menu opens the card", async () => {
  const start = vi.spyOn(voiceCallStore, "start").mockImplementation(async () => undefined);
  const stop = vi.spyOn(voiceCallStore, "stop").mockImplementation(() => undefined);
  render(<AiOrb {...props} activity={activity() as any} voiceAvailable sessionId="chat-1" />, { wrapper });
  const orb = screen.getByTestId("ai-orb");
  expect(orb).toHaveAttribute("data-state", "standby");
  expect(orb).toHaveAccessibleName(/开始语音通话；AI 待机；可拖动/);
  expect(disclosureStates()["voice-call"]).toBe(false);
  expect(uiSurfaceStates().aiActivityCard).toBe("closed");
  fireEvent.click(orb);
  expect(start).toHaveBeenCalledWith("http://runtime", "chat-1");
  act(() => applyUiSurface("disclosure:voice-call", true));
  expect(start).toHaveBeenCalledTimes(2);
  // Right-click menu: 切文字 and AI 动态; the card opens through the same surface and reports open.
  fireEvent.contextMenu(orb);
  expect(screen.getByRole("menuitem", { name: "切文字" })).toBeVisible();
  fireEvent.click(screen.getByRole("menuitem", { name: /AI 动态/ }));
  expect(screen.getByRole("region", { name: "AI 动态卡片" })).toBeVisible();
  expect(uiSurfaceStates().aiActivityCard).toBe("open");
  await waitFor(() => expect(disclosureStates()["voice-menu"]).toBe(false));
  act(() => applyUiSurface("aiActivityCard", "closed"));
  await waitFor(() => expect(uiSurfaceStates().aiActivityCard).toBe("closed"));
  expect(stop).not.toHaveBeenCalled();
  start.mockRestore(); stop.mockRestore();
});

test("without voice the orb is the activity ball: click toggles the card", () => {
  render(<AiOrb {...props} activity={activity() as any} voiceAvailable={false} />, { wrapper });
  const orb = screen.getByRole("button", { name: /AI 动态/ });
  expect(disclosureStates()["voice-call"]).toBeUndefined();
  fireEvent.click(orb);
  expect(screen.getByRole("region", { name: "AI 动态卡片" })).toBeVisible();
  fireEvent.click(orb);
  expect(screen.queryByRole("region", { name: "AI 动态卡片" })).not.toBeInTheDocument();
});

test("new activity pops the card for about 8 s with an unread dot, even during a call", () => {
  vi.useFakeTimers();
  const first = activity([{ clientOrderId: "a", displayStatus: "已受理", createdAt: "2026-09-18T01:00:00Z", source: "ai", request: { direction: "BUY", offset: "OPEN", volume: 1, limitPrice: 3100 } }]);
  const { rerender } = render(<AiOrb {...props} activity={first as any} voiceAvailable />, { wrapper });
  expect(screen.queryByRole("region", { name: "AI 动态卡片" })).not.toBeInTheDocument();
  act(() => { voiceCallStore["publish"]({ state: "listening" } as never); });
  expect(screen.getByTestId("ai-orb")).toHaveAttribute("data-state", "listening");
  const second = activity([{ clientOrderId: "a", displayStatus: "已成交", createdAt: "2026-09-18T01:00:00Z", source: "ai", request: { direction: "BUY", offset: "OPEN", volume: 1, limitPrice: 3100 } }]);
  rerender(<AiOrb {...props} activity={second as any} voiceAvailable />);
  const card = screen.getByRole("region", { name: "AI 动态卡片" });
  expect(card).toHaveAttribute("data-auto", "true");
  expect(screen.getByTestId("ai-orb")).toHaveAttribute("data-unread", "true");
  expect(screen.getByTestId("ai-orb")).toHaveAttribute("data-state", "listening");
  act(() => { vi.advanceTimersByTime(8_100); });
  expect(document.getElementById("ai-floating-card")).toHaveAttribute("data-closing", "true");
  act(() => { vi.advanceTimersByTime(200); });
  expect(document.getElementById("ai-floating-card")).toBeNull();
  // The dot stays until the user really looks: a pinned open clears it.
  expect(screen.getByTestId("ai-orb")).toHaveAttribute("data-unread", "true");
  fireEvent.contextMenu(screen.getByTestId("ai-orb"));
  fireEvent.click(screen.getByRole("menuitem", { name: /AI 动态/ }));
  expect(screen.getByTestId("ai-orb")).not.toHaveAttribute("data-unread");
});

test("默认位置在图表主图安全区左下（时间轴上方、价格轴内侧）；只在拖动后记住，旧版默认角不算用户位置，越界夹回", () => {
  // The safe area is the orb's container (AiActivity.css / FuturesDecisionWorkspace.css keep it off the
  // axes); a 800×500 area puts the bottom-left default at left 0, top 500 - 56.
  class Sized { constructor(private callback: ResizeObserverCallback) {} observe() { this.callback([{ contentRect: { width: 800, height: 500 } }] as never, this as never); } unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", Sized);
  localStorage.clear();
  expect(savedOrbPosition()).toEqual({ x: 0, y: 1 });
  const view = render(<AiOrb {...props} activity={activity() as any} voiceAvailable={false} />, { wrapper });
  const orb = screen.getByTestId("ai-orb");
  expect(orb.style.left).toBe("0px");
  expect(orb.style.top).toBe("444px");
  // Merely showing the orb remembers nothing; moving it does (keyboard moves are the same path as a drag).
  expect(localStorage.getItem("ai-trader-ai-float-position-v2")).toBeNull();
  fireEvent.keyDown(orb, { key: "ArrowRight" });
  fireEvent.keyDown(orb, { key: "ArrowUp" });
  expect(orb.style.left).toBe("16px");
  expect(orb.style.top).toBe("428px");
  expect(JSON.parse(localStorage.getItem("ai-trader-ai-float-position-v2")!)).toEqual({ x: 16 / 744, y: 428 / 444 });
  view.unmount();
  // 1.6.x wrote its bottom-right default on every mount: that corner is not a user choice.
  localStorage.clear();
  localStorage.setItem("ai-trader-ai-float-position-v1", JSON.stringify({ x: 1, y: 1 }));
  expect(savedOrbPosition()).toEqual({ x: 0, y: 1 });
  localStorage.setItem("ai-trader-ai-float-position-v1", JSON.stringify({ x: 0.4, y: 0.25 }));
  expect(savedOrbPosition()).toEqual({ x: 0.4, y: 0.25 });
  localStorage.setItem("ai-trader-ai-float-position-v2", JSON.stringify({ x: 1, y: 1 }));
  expect(savedOrbPosition()).toEqual({ x: 1, y: 1 });
  // A stored place is a fraction of the safe area, clamped into it: any window size keeps it visible.
  localStorage.setItem("ai-trader-ai-float-position-v2", JSON.stringify({ x: 3, y: -2 }));
  expect(savedOrbPosition()).toEqual({ x: 1, y: 0 });
  localStorage.clear();
});
