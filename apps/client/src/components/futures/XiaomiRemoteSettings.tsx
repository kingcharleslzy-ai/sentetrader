import { useEffect } from "react";
import { SettingRow, SettingsLink, SettingsSection } from "../SettingsPanel";
import { Button, Select, Switch } from "../settings/controls";
import { useUiSurface } from "../../uiSurfaces";
import { REMOTE_ACTIONS, REMOTE_KEYS, useXiaomiRemote, xiaomiRemoteStore, type RemoteAction, type RemoteKey } from "../../xiaomiRemote/store";

/** Commands and clicks share the local device store; pairing always needs a user's gesture. */
export function applyRemoteSettings(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("遥控器设置必须是对象");
  const command = value as Record<string, unknown>;
  if (Object.keys(command).some(key => !["enabled", "bindings", "resetBindings", "diagnostic", "disconnect"].includes(key))) throw new Error("未知遥控器操作；连接请在页面点击连接并选择设备");
  for (const key of ["enabled", "resetBindings", "diagnostic", "disconnect"]) if (key in command && typeof command[key] !== "boolean") throw new Error("遥控器开关必须是布尔值");
  if (!Object.keys(command).length) throw new Error("遥控器操作不能为空");
  if (command.diagnostic === true && (command.enabled === false || command.disconnect === true)) throw new Error("开始诊断不能同时停用或断开遥控器");
  if (command.disconnect === true && command.enabled === true) throw new Error("断开不能同时启用遥控器");
  if (command.resetBindings === true && "bindings" in command) throw new Error("恢复默认不能同时修改按键");
  if (command.diagnostic === true && xiaomiRemoteStore.snapshot().phase !== "ready") throw new Error("请先连接遥控器再开始诊断");
  const bindings = command.bindings;
  if (bindings !== undefined) {
    if (!bindings || typeof bindings !== "object" || Array.isArray(bindings)) throw new Error("按键设置必须是对象");
    for (const [key, action] of Object.entries(bindings)) {
      if (!REMOTE_KEYS.some(item => item.id === key) || !REMOTE_ACTIONS.some(item => item.id === action) || (action === "ptt" && key !== "voice")) throw new Error("未知按键或动作，按住说话仅用于语音键");
    }
  }
  if (typeof command.enabled === "boolean") xiaomiRemoteStore.setEnabled(command.enabled);
  if (command.resetBindings) xiaomiRemoteStore.resetBindings();
  if (bindings) for (const [key, action] of Object.entries(bindings)) xiaomiRemoteStore.setBinding(key as RemoteKey, action as RemoteAction);
  if (typeof command.diagnostic === "boolean") xiaomiRemoteStore.setDiagnostic(command.diagnostic);
  if (command.disconnect) xiaomiRemoteStore.disconnect();
}

export function XiaomiRemoteSettings({ page = "voice", onPageChange }: { page?: "voice" | "remote-keys" | "remote-diagnostics"; onPageChange?: (page: "remote-keys" | "remote-diagnostics") => void }) {
  const state = useXiaomiRemote();
  useUiSurface("remoteSettings", { state, apply: value => {
    if (value && typeof value === "object" && "diagnostic" in value && value.diagnostic === true && page !== "remote-diagnostics") throw new Error("请先打开测试与诊断页");
    applyRemoteSettings(value);
  } }, [state, page]);
  useEffect(() => () => { if (page === "remote-diagnostics") xiaomiRemoteStore.setDiagnostic(false); }, [page]);
  if (page === "remote-keys") return <SettingsSection title="按键设置" helpKey="remote-keys" help="按键配置只保存在这台设备，启用和连接须在当前窗口明确开启。可提前配置所有按键；浏览器只能可靠区分本遥控器的 ATVV 语音键，其他键留给 Mac 客户端。连接期间 F5 暂为遥控语音保留，断开恢复刷新；其他普通键盘不受影响，普通 F5 不会录音。执行聚焦操作不会自动提交交易。">
    {REMOTE_KEYS.map(key => <SettingRow key={key.id} label={key.label} hint={!state.availableKeys.includes(key.id) ? state.platform === "web" ? "浏览器不支持，Mac客户端可用" : "连接遥控器后可用" : undefined}>
      <Select label={`${key.label}动作`} value={state.bindings[key.id]} options={REMOTE_ACTIONS.filter(action => action.id !== "ptt" || key.id === "voice").map(action => ({ value: action.id, label: action.label }))} onChange={action => xiaomiRemoteStore.setBinding(key.id, action as RemoteAction)} />
    </SettingRow>)}
    <SettingRow label="默认按键"><Button onClick={() => xiaomiRemoteStore.resetBindings()}>恢复默认按键</Button></SettingRow>
    {state.keyError && <SettingRow label="按键状态"><span role="alert">{state.keyError}</span></SettingRow>}
  </SettingsSection>;
  if (page === "remote-diagnostics") return <SettingsSection title="测试与诊断" helpKey="remote-diagnostics" help="开始后按住遥控器语音键检查本地音量与帧数，语音不会发送到识别服务，不保存或上传录音。离开此页自动停止。">
    <SettingRow label="本地测试"><span role="status">{state.diagnostic ? "测试中" : "已停止"}</span><Button primary={!state.diagnostic} danger={state.diagnostic} disabled={!state.diagnostic && state.phase !== "ready"} onClick={() => xiaomiRemoteStore.setDiagnostic(!state.diagnostic)}>{state.diagnostic ? "停止测试" : "开始测试"}</Button></SettingRow>
    <SettingRow label="本地音量"><meter aria-label="遥控器本地音量" min={0} max={1} value={state.level} />{Math.round(state.level * 100)}%</SettingRow>
    <SettingRow label="音频帧数"><output>{state.frames}</output></SettingRow>
    <SettingRow label="最后按键"><output>{state.lastKey ? `${REMOTE_KEYS.find(key => key.id === state.lastKey?.key)?.label ?? state.lastKey.key} · ${state.lastKey.pressed ? "按下" : "松开"}` : "尚无按键"}</output></SettingRow>
    <SettingRow label="错误"><span role="status">{state.error ?? state.keyError ?? "无"}</span></SettingRow>
  </SettingsSection>;
  const phase = { idle: "未连接", connecting: "连接中", ready: "已连接", reconnecting: "重连中", error: "连接失败" }[state.phase];
  return <SettingsSection title="米家遥控器 2 Pro" helpKey="xiaomi-remote" help="Mac 客户端与支持 Web Bluetooth 的网页版连接米家遥控器 2 Pro。仅按键配置按本机设备保存，不随账户同步；启用和连接须在当前窗口明确开启。Windows 客户端暂未实现。Mac 须允许输入监控，仅前台接管这只遥控器的13键，并临时静音原系统功能；失焦、断开或退出恢复原映射。已有冲突映射时拒绝接管并显示原因，请先关闭其他遥控软件后重新连接。首次连接请点击按钮并在系统或浏览器中选择设备。">
    <SettingRow label="启用遥控器"><Switch label="启用米家遥控器" checked={state.enabled} onChange={value => xiaomiRemoteStore.setEnabled(value)} /></SettingRow>
    <SettingRow label="连接状态" hint={!state.supported ? "请使用Mac客户端或Chrome/Edge的HTTPS网页" : state.error ?? state.keyError ?? (state.platform === "web" && ["connecting", "ready", "reconnecting"].includes(state.phase) ? "F5暂用于遥控语音" : undefined)}>
      <span role="status">{state.deviceName ? `${state.deviceName} · ` : ""}{phase}</span>
      {state.phase === "ready" || state.phase === "reconnecting" ? <Button onClick={() => xiaomiRemoteStore.disconnect()}>断开遥控器</Button> : <Button primary disabled={!state.supported || !state.enabled || state.phase === "connecting"} onClick={() => void xiaomiRemoteStore.connect()}>连接遥控器</Button>}
    </SettingRow>
    <SettingsLink label="按键设置" onOpen={() => onPageChange?.("remote-keys")} />
    <SettingsLink label="测试与诊断" onOpen={() => onPageChange?.("remote-diagnostics")} />
  </SettingsSection>;
}
