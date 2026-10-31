import type { RuntimeStatus } from "@ai-trader/ui-events";



const stageLabels: Record<NonNullable<RuntimeStatus["dataConnectionStage"]>, string> = {
  starting: "正在启动数据连接",
  loading_sdk: "正在加载行情组件",
  discovering_instruments: "正在读取合约目录",
  authenticating: "正在验证快期账号",
  subscribing: "账号已验证，正在读取账户与行情",
  waiting_snapshot: "已连接，正在读取数据",
  waiting_account: "已连接，正在读取账户",
  waiting_market: "已连接，正在读取行情",
  reconnecting: "网络连接中断，正在安全重连",
  ready: "账户与行情已连接",
  failed: "数据连接失败"
};

const errorLabels: Record<string, string> = {
  tqkq_authentication_failed: "快期账号或密码验证失败",
  tqkq_permission_denied: "快期账户没有所需的期货行情权限",
  tqkq_connection_timeout: "连接快期服务超时",
  tqkq_daily_maintenance: "快期每日 19:00–19:30 例行维护，结束后自动恢复",
  tqkq_initial_snapshot_timeout: "未及时收到完整账户或行情数据",
  market_quote_not_ready: "部分合约报价暂缺，等待行情更新",
  market_session_stalled: "行情会话卡住，正在自动重连",
  sidecar_reconnecting: "行情重连中",
  tqkq_sidecar_importerror: "交易数据组件未能启动，请检查应用更新",
  tqkq_sidecar_modulenotfounderror: "交易数据组件不完整，请检查应用更新"
};

export function tqkqRecovery(runtime: RuntimeStatus | undefined, runtimeUsable: boolean) {
  if (!runtimeUsable) return { action: "retry", label: "重新连接", detail: "应用服务暂不可用，可重试连接；无需更换快期账号。" } as const;
  const code = runtime?.dataErrorCode;
  const paper = runtime?.accountMode === "paper";
  if (code === "tqkq_authentication_failed") return { action: "credentials", label: "核对账号密码", detail: "快期拒绝了账号验证，请核对个人快期账号及当前密码。" } as const;
  if (code === "tqkq_permission_denied") return { action: "help", label: "查看权限说明", detail: "当前账号缺少所需权限，请在快期官方账户中心核对。" } as const;
  if (code === "tqkq_sidecar_importerror" || code === "tqkq_sidecar_modulenotfounderror") return { action: "help", label: "查看组件修复说明", detail: "行情组件未正常加载，请在设置中检查应用更新。" } as const;
  if (runtime?.marketDataStatus === "backup") return { action: "wait", label: "", detail: "快期行情暂不可用，报价与图表暂用新浪备用行情（可能延迟几秒），只供看盘，不能下单；快期恢复后自动切回。" } as const;
  if (runtime?.dataConnectionStage === "failed") return { action: "retry", label: "重新连接", detail: code === "tqkq_initial_snapshot_timeout" ? "未在规定时间内收到完整账户或行情，可稍后重新连接。" : tqkqConnectionDetail(runtime) || "连接失败，可稍后重试；具体原因尚未确认。" } as const;
  if ((runtime?.dataConnectionStage === "ready" || runtime?.dataConnectionStage === "waiting_market") && runtime.marketSessionStatus === "closed" && runtime.accountStatus !== "unavailable" && runtime.marketDataStatus !== "unavailable") {
    return { action: "none", label: "", detail: runtime.dataConnectionStage === "ready" ? (paper ? "本站模拟已连接，当前休市。" : "快期已连接，当前休市。") : paper ? "本站模拟已连接，当前休市；开盘后自动收到行情。" : "快期已连接，当前休市；开盘后自动收到行情。" } as const;
  }
  if (runtime?.dataConnectionStage === "reconnecting") return { action: "wait", label: "", detail: code === "tqkq_daily_maintenance"
    ? "快期每日 19:00–19:30 例行维护，期间行情和快期账户暂停更新，结束后自动恢复。" : "网络中断，正在自动重连。" } as const;
  if (runtime?.dataConnectionStage && runtime.dataConnectionStage !== "ready") return { action: "wait", label: "", detail: tqkqConnectionDetail(runtime) || (paper ? "正在读取本站账户状态。" : "正在读取快期连接状态。") } as const;
  if (runtime?.accountStatus === "stale" || runtime?.marketDataStatus === "stale" || runtime?.accountStatus === "unavailable" || runtime?.marketDataStatus === "unavailable") return { action: "retry", label: "重新连接", detail: "账户或行情数据未及时更新，可重新连接。" } as const;
  if (runtime?.dataConnectionStage !== "ready") return { action: "wait", label: "", detail: tqkqConnectionDetail(runtime) || (paper ? "正在读取本站账户状态。" : "正在读取快期连接状态。") } as const;
  return { action: "none", label: "", detail: "账户与行情已连接。" } as const;
}


export function tqkqConnectionDetail(runtime?: RuntimeStatus): string | undefined {
  if (runtime?.accountMode !== "tqkq-readonly") return undefined;
  // Logged in after the day close: no quote exists until the next bell, which is not a data gap.
  if (runtime.dataConnectionStage === "waiting_market" && runtime.marketSessionStatus === "closed" && runtime.dataErrorCode === "market_quote_not_ready") {
    return "已连接快期 · 当前休市，开盘后自动收到行情";
  }
  const error = runtime.dataErrorCode ? errorLabels[runtime.dataErrorCode] : undefined;
  if (error) return error;
  const stage = runtime.dataConnectionStage ? stageLabels[runtime.dataConnectionStage] : undefined;
  if (!stage) return undefined;
  if (runtime.dataConnectionStage === "ready" && runtime.marketSessionStatus === "closed") {
    return `${stage} · 当前休市，显示最新一笔行情`;
  }
  const seconds = runtime.dataInitializationSeconds;
  return typeof seconds === "number" && seconds >= 1 && runtime.dataConnectionStage !== "ready"
    ? `${stage} · ${Math.floor(seconds)} 秒`
    : stage;
}
