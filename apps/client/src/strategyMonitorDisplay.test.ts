import { monitorDisplay } from "./strategyMonitorDisplay";

test("stale evaluation cannot claim the monitor is still watching", () => {
  expect(monitorDisplay({status:"monitoring",decision:{status:"waiting",detail:"等待信号",checkedAt:new Date(Date.now()-60_000).toISOString()}}).label).toBe("运行回报已过期");
  expect(monitorDisplay({status:"paused",decision:{status:"waiting",detail:"等待信号"}}).label).toBe("已暂停");
  expect(monitorDisplay({status:"monitoring",decision:{status:"paused",detail:"策略已暂停"}}).detail).not.toBe("策略已暂停");
});

test("a paused monitor preserves the execution failure reason", () => {
  expect(monitorDisplay({status:"paused",monitorState:{pauseReason:null},decision:{status:"failed",detail:"ai_execution_stopped"}}).detail).toContain("AI 模拟执行已停止或急停");
});

test("normal cooldown is waiting rather than a failed trade", () => {
  expect(monitorDisplay({status:"monitoring",decision:{status:"blocked",detail:"策略仍在冷却周期。"}}).label).toBe("冷却等待");
  expect(monitorDisplay({status:"monitoring",decision:{status:"blocked",detail:"同合约仍有活动委托。"}}).label).toBe("动作待确认 · 已拦截");
  expect(monitorDisplay({status:"monitoring",decision:{status:"blocked",action:"open_long",detail:"同合约仍有活动委托。"}}).label).toBe("开多 · 已拦截");
  expect(monitorDisplay({status:"monitoring",decision:{status:"blocked",action:"close_short",detail:"没有足够可平持仓。"}}).label).toBe("平空 · 已拦截");
});

test("every runtime strategy status maps onto one of the four user-facing states", async () => {
  const { strategyState, STRATEGY_STATE_ORDER } = await import("./strategyMonitorDisplay");
  expect(strategyState("monitoring")).toEqual({ label: "运行中" });
  expect(strategyState("paused")).toEqual({ label: "已暂停" });
  expect(strategyState("ready")).toEqual({ label: "已验证" });
  expect(strategyState("validating")).toEqual({ label: "草稿", detail: "验证中" });
  expect(strategyState("draft").label).toBe("草稿");
  expect(strategyState(undefined).label).toBe("草稿");
  expect(STRATEGY_STATE_ORDER).toEqual(["运行中", "已暂停", "已验证", "草稿"]);
});

test("data not ready yet reads as waiting for quotes, turning warning only when long", () => {
  const waiting = monitorDisplay({status:"monitoring",decision:{status:"waiting",waitReason:"market_series_not_ready",stalled:false,detail:"等待行情：所需周期 K 线正在载入。"}});
  expect(waiting).toEqual({label:"等待行情",detail:"等待行情：所需周期 K 线正在载入。",tone:"neutral"});
  expect(monitorDisplay({status:"monitoring",decision:{status:"waiting",waitReason:"market_subscription_limit",stalled:true,detail:"等待行情已 12 分钟：行情订阅名额已满，等名额腾出。请检查行情连接或合约是否在交易。"}}).tone).toBe("warning");
  expect(monitorDisplay({status:"monitoring",decision:{status:"waiting",waitReason:"market_session_closed",detail:"market_session_closed"}}).label).toBe("休市等待");
});
