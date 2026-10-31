import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VoiceSettings } from "./VoiceSettings";
import { DEFAULT_PREFERENCES, type PreferencesHandle } from "../../userPreferences";

const voices = { current: "zh_female_vv_jupiter_bigtts", voices: [
  { id: "zh_female_vv_jupiter_bigtts", label: "Vivi（女声，活泼）" }, { id: "zh_male_xiaotian_jupiter_bigtts", label: "小天（男声，磁性）" }
] };

function handle(overrides: Partial<PreferencesHandle> = {}): PreferencesHandle {
  const set = vi.fn(async () => ({ version: 2, preferences: DEFAULT_PREFERENCES }));
  return { preferences: DEFAULT_PREFERENCES, document: { version: 1, preferences: DEFAULT_PREFERENCES }, status: "", update: vi.fn(), set, refresh: vi.fn(), ...overrides };
}

test("the voice row reads the Runtime's voices and writes the choice into preferences.voice.voice", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(voices), { headers: { "Content-Type": "application/json" } })));
  const prefs = handle();
  render(<VoiceSettings baseUrl="http://runtime" available preferences={prefs} />);
  // No account choice yet: the Runtime's current voice is shown.
  const select = await screen.findByRole("combobox", { name: "语音音色" });
  await waitFor(() => expect(select).toHaveValue("zh_female_vv_jupiter_bigtts"));
  expect(screen.getByRole("option", { name: "Vivi · 女声，活泼" })).toBeInTheDocument();
  fireEvent.change(select, { target: { value: "zh_male_xiaotian_jupiter_bigtts" } });
  await waitFor(() => expect(prefs.set).toHaveBeenCalledWith("voice.voice", "zh_male_xiaotian_jupiter_bigtts"));
  expect(fetch).toHaveBeenCalledTimes(1);
  vi.unstubAllGlobals();
});

test("speed, stall, confirm window and auto-annotate write their dotted keys; the announcements moved to 设置 → 提醒", () => {
  vi.stubGlobal("fetch", vi.fn());
  const prefs = handle();
  const view = render(<VoiceSettings baseUrl="http://runtime" available={false} preferences={prefs} />);
  expect(screen.getByText("服务器未配置语音")).toBeVisible();
  expect(screen.queryByRole("combobox", { name: "语音音色" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("radio", { name: "快" }));
  expect(prefs.set).toHaveBeenCalledWith("voice.speed", "fast");
  fireEvent.click(screen.getByRole("switch", { name: "通话中自动画图" }));
  expect(prefs.set).toHaveBeenCalledWith("voice.autoAnnotate", false);
  // 统一提醒 (#932): each reminder kind's switch on 设置 → 提醒 covers its spoken line too.
  expect(screen.queryByRole("switch", { name: "成交播报" })).toBeNull();
  expect(screen.queryByRole("switch", { name: "简讯播报" })).toBeNull();
  // 判停与确认时限 moved to the voice-advanced second-level page (设置拆页 2026-09-30).
  view.rerender(<VoiceSettings baseUrl="http://runtime" available={false} preferences={prefs} advanced />);
  fireEvent.click(screen.getByRole("button", { name: "说完判停增加" }));
  expect(prefs.set).toHaveBeenCalledWith("voice.endpointStallSeconds", 3.5);
  fireEvent.click(screen.getByRole("button", { name: "口头确认时限减少" }));
  expect(prefs.set).toHaveBeenCalledWith("voice.confirmWindowSeconds", 10);
  expect(fetch).not.toHaveBeenCalled();
  vi.unstubAllGlobals();
});

test("disconnected: every voice row is disabled and nothing is written", () => {
  const prefs = handle();
  render(<VoiceSettings baseUrl={undefined} available={false} preferences={prefs} />);
  expect(screen.getByText("连接后可用")).toBeVisible();
  expect(screen.getByRole("switch", { name: "通话中自动画图" })).toBeDisabled();
  expect(screen.getByRole("radio", { name: "慢" })).toBeDisabled();
  expect(prefs.set).not.toHaveBeenCalled();
});
