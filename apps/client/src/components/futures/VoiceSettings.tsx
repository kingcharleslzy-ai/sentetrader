import { useEffect, useState } from "react";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { Segmented, Select, Stepper, Switch } from "../settings/controls";
import { getVoiceRealtimeVoices, type VoiceRealtimeVoices } from "../../voiceRealtime";
import type { PreferencesHandle } from "../../userPreferences";

/**
 * 设置 → 语音: the account's voice preferences (`preferences.voice`, applied to the next call). The
 * voice list comes from the Runtime; the choice and every other row are written through
 * PUT /api/preferences, so the `preference` command and `update_preferences` land in the same place.
 * `advanced` (设置拆页 2026-09-30) renders the voice-advanced page's rows only: 判停与确认时限
 * moved off the main page into that second-level page.
 */
export function VoiceSettings({ baseUrl, available, preferences, advanced }: { baseUrl: string | undefined; available: boolean; preferences: PreferencesHandle; advanced?: boolean }) {
  const [voices, setVoices] = useState<VoiceRealtimeVoices>();
  const [listStatus, setListStatus] = useState("");
  useEffect(() => {
    if (!baseUrl || !available) { setVoices(undefined); return; }
    let cancelled = false;
    getVoiceRealtimeVoices(baseUrl)
      .then(result => { if (!cancelled) { setVoices(result); setListStatus(""); } })
      .catch(() => { if (!cancelled) setListStatus("音色列表读取失败"); });
    return () => { cancelled = true; };
  }, [baseUrl, available]);
  const voice = preferences.preferences.voice;
  const set = (key: string, value: unknown) => void preferences.set(`voice.${key}`, value).catch(() => undefined);
  const disabled = !baseUrl;
  const status = preferences.status ? <span role="status">{preferences.status}</span> : undefined;
  if (advanced) return (
    <SettingsSection title="判停与确认时限" helpKey="voice-safety-net" help="安全网参数：判停主要由服务端动态判停决定">
      <SettingRow label="说完判停" helpKey="voice-end-of-speech" help="没有新字多久才强制判停，安全网（2–5 秒）；判停主要由服务端动态判停决定">
        <Stepper label="说完判停" value={voice.endpointStallSeconds} min={2} max={5} step={0.5} unit="秒" disabled={disabled} onChange={value => { if (value != null) set("endpointStallSeconds", Math.round(value * 10) / 10); }} />
      </SettingRow>
      <SettingRow label="口头确认时限" helpKey="voice-confirm-window" help="复述播完后等“确认”的时间（5–60 秒），超时方案卡自动放弃">
        <Stepper label="口头确认时限" value={voice.confirmWindowSeconds} min={5} max={60} step={5} unit="秒" disabled={disabled} onChange={value => { if (value != null) set("confirmWindowSeconds", Math.round(value)); }} />
      </SettingRow>
    </SettingsSection>
  );
  return (
    <>
      <SettingsSection title="实时语音">
        <SettingRow label="语音状态"><strong>{!baseUrl ? "连接后可用" : available ? "可用" : "服务器未配置语音"}</strong></SettingRow>
        {available && baseUrl ? (
          <SettingRow label="语音音色" hint={listStatus ? <span role="status">{listStatus}</span> : status ?? "按账号保存，下一次通话生效"}>
            <Select label="语音音色" value={voice.voice ?? voices?.current ?? ""} disabled={!voices} onChange={value => set("voice", value)}
              options={(voices?.voices ?? []).map(item => ({ value: item.id, label: item.label.replace(/（(.+?)）$/, " · $1") }))} />
          </SettingRow>
        ) : null}
        <SettingRow label="语速" hint={status}>
          <Segmented label="语速" value={voice.speed} disabled={disabled} options={[{ value: "slow", label: "慢" }, { value: "normal", label: "正常" }, { value: "fast", label: "快" }] as const} onChange={value => set("speed", value)} />
        </SettingRow>
      </SettingsSection>
      {/* 成交播报 / 简讯播报 moved to 设置 → 提醒 (2026-10-15): each reminder kind's one switch covers its voice line too. */}
      <SettingsSection title="通话中">
        <SettingRow label="通话中自动画图" helpKey="voice-auto-annotate" help="AI 讲到价位或区间时自动在图上标注"><Switch label="通话中自动画图" checked={voice.autoAnnotate} disabled={disabled} onChange={value => set("autoAnnotate", value)} /></SettingRow>
      </SettingsSection>
      <SettingsSection title="按住说话">
        <SettingRow label="松开后直接发送" helpKey="ptt-send-on-release" help="关闭后识别的文字填入输入框，改完再发；按住键在“快捷键”里改">
          <Switch label="松开后直接发送" checked={voice.pttSendOnRelease !== false} disabled={disabled} onChange={value => set("pttSendOnRelease", value)} />
        </SettingRow>
      </SettingsSection>
    </>
  );
}
