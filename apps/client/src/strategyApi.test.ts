import { describe, expect, test } from "vitest";
import { allowsSymbol, CONTRACT, conditionText, designerPlanProblem, isAdvancedCondition, operandLabel, planBlockRows, strategyError, strategyStatusText, type Operand, type RuleCondition,
  type StrategyPlan } from "./strategyApi";
import { rememberStrategyScopes } from "./api";

describe("strategyError account-role refusals", () => {
  test("403 with a bare 4.3 code explains the role boundary instead of a lost connection", () => {
    const error = { status: 403, detail: "not_execution_owner" };
    expect(strategyError(error, "启动失败")).toContain("执行负责人");
    expect(strategyError({ status: 403, detail: "account_read_only" }, "启动失败"))
      .toContain("查看权限");
    expect(strategyError({ status: 403, detail: "account_not_granted" }, "启动失败"))
      .toContain("访问授权");
  });

  test("403 with an unrecognized detail keeps the reconnect guidance", () => {
    expect(strategyError({ status: 403, detail: "other_code" }, "启动失败"))
      .toBe("连接已失效，请在设置中重新连接后重试。");
    expect(strategyError({ status: 403 }, "启动失败"))
      .toBe("连接已失效，请在设置中重新连接后重试。");
  });

  test("401 and other statuses are unchanged", () => {
    expect(strategyError({ status: 401 }, "启动失败"))
      .toBe("连接已失效，请在设置中重新连接后重试。");
    expect(strategyError({ status: 409, detail: "strategy_transition_invalid" }, "启动失败"))
      .toContain("策略状态已变化");
  });

  test("status text translates the ownership code in monitor detail", () => {
    expect(strategyStatusText("not_execution_owner：这个账户的策略只监控不下单。"))
      .toContain("方案卡");
  });
});

describe("strategy building-block labels", () => {
  test("operands read like the Runtime's describe_operand", () => {
    const label = (operand: Partial<Operand>) => operandLabel({ timeframe: 300, value: 0, ...operand } as Operand);
    expect(label({ indicator: "today_open", offset: { factor: 0.2, indicator: "range_nday", period: 5 } })).toBe("今开 + 0.2×5日振幅");
    expect(label({ indicator: "ma", period: 20, offset: { factor: -1.5, indicator: "atr", period: 14 } })).toBe("MA20 − 1.5×ATR14");
    expect(label({ indicator: "atr", period: 14, average: 20 })).toBe("ATR14的20均线");
    expect(label({ indicator: "boll_up", period: 20 })).toBe("布林上轨(20,2)");
    expect(label({ indicator: "donchian_low", period: 10 })).toBe("前10根最低");
  });

  test("plan-level blocks split into exit and entry rows", () => {
    const plan = { name: "积木", description: "积木", direction: "long", timeframeSeconds: 300, maxPosition: 3,
      rules: { entryLong: null, exitLong: null, entryShort: null, exitShort: null }, questions: [],
      trailingStop: { mode: "percent", value: 0.8, activateAfter: 0.5 }, closeBeforeEndMinutes: 5, maxHoldBars: 30,
      sizing: { mode: "atr_risk", riskPercent: 1 }, entryWindows: [{ start: "09:05", end: "14:30" }],
      allowNightEntries: false, maxEntriesPerDirection: 2 } as unknown as StrategyPlan;
    expect(planBlockRows(plan, "exit").map(([label]) => label)).toEqual(["追踪止损", "收盘前平仓", "最长持仓"]);
    expect(planBlockRows(plan, "exit")[0][1]).toContain("回撤 0.8%");
    expect(planBlockRows(plan, "entry").map(([label]) => label)).toEqual(["按 ATR 定手数", "开仓时段", "夜盘", "每方向开仓"]);
  });

  test("third-batch blocks and 前 N 根 read like the Runtime (2026-09-27)", () => {
    const label = (operand: Partial<Operand>) => operandLabel({ timeframe: 300, value: 0, ...operand } as Operand);
    expect(label({ indicator: "roc", period: 12 })).toBe("12根涨跌幅%");
    expect(label({ indicator: "kd_j" })).toBe("KDJ J");
    expect(label({ indicator: "trix", period: 12, average: 9 })).toBe("TRIX12的9均线");
    expect(label({ indicator: "high", barsAgo: 1 })).toBe("1根前的最高价");
    expect(label({ indicator: "close", barsAgo: 0 })).toBe("收盘价");
    const close = { indicator: "close", timeframe: 300, value: 0 } as Operand;
    expect(conditionText({ label: "x", left: close, operator: "gt", right: { ...close, barsAgo: 2 }, withinBars: 1, consecutive: 1 } as RuleCondition))
      .toBe("收盘价 高于 2根前的收盘价");
    // The new blocks sit in the designer's 高级 fold, like the second batch.
    const plain = { label: "x", left: close, operator: "gt", right: { ...close, indicator: "ma", period: 20 }, withinBars: 1, consecutive: 1 } as RuleCondition;
    expect(isAdvancedCondition(plain)).toBe(false);
    expect(isAdvancedCondition({ ...plain, right: { ...close, barsAgo: 1 } })).toBe(true);
    expect(isAdvancedCondition({ ...plain, left: { ...close, indicator: "trix", period: 12 } })).toBe(true);
  });

  test("second-batch blocks read like the Runtime (2026-09-25)", () => {
    const label = (operand: Partial<Operand>) => operandLabel({ timeframe: 300, value: 0, ...operand } as Operand);
    expect(label({ indicator: "adx", period: 14 })).toBe("ADX14");
    expect(label({ indicator: "pivot", value: 3 })).toBe("枢轴R3（突破买入价）");
    expect(label({ indicator: "pivot", value: -1 })).toBe("枢轴S1（反转买入价）");
    expect(label({ indicator: "session_high" })).toBe("当日最高");
    const close = { indicator: "close", timeframe: 300, value: 0 } as Operand;
    const constant = (value: number) => ({ indicator: "constant", timeframe: 300, value }) as Operand;
    const condition = (operator: RuleCondition["operator"], right: Operand, extra: Partial<RuleCondition> = {}) =>
      ({ label: "x", left: close, operator, right, withinBars: 1, consecutive: 1, ...extra }) as RuleCondition;
    expect(conditionText(condition("rising", constant(2), { consecutive: 3 }))).toBe("收盘价 较2根前上升（连续 3 根）");
    expect(conditionText(condition("eq", constant(1), { left: { ...close, indicator: "oi_quadrant" } }))).toBe("持仓四象限 = 多头增仓");
    expect(conditionText({ ...condition("gt", constant(100)), consecutive: undefined } as unknown as RuleCondition)).toBe("收盘价 高于 100");
    const plan = { name: "积木", description: "积木", direction: "long", timeframeSeconds: 300, maxPosition: 1, questions: [],
      breakeven: { triggerAtr: 1, offsetTicks: 1 }, timeTakeProfit: [{ afterBars: 20, profitTicks: 5 }, { afterBars: 0, profitAtr: 3 }],
      rules: { entryLong: { join: "all", sequence: false, conditions: [], rearm: [condition("gt", constant(35))] },
        exitLong: null, entryShort: null, exitShort: null } } as unknown as StrategyPlan;
    expect(planBlockRows(plan, "exit")).toEqual([["保本", "浮盈达 1×ATR14 后止损移到开仓价盈利一侧 1 跳，只收紧不放松"],
      ["分段止盈", "K线收盘时判断：持仓满 0 根起浮盈 ≥ 3×ATR14；持仓满 20 根起浮盈 ≥ 5 跳，达到就平仓"]]);
    expect(planBlockRows(plan, "entry")).toEqual([["复位", "开多后须等「收盘价 高于 35」成立一次才再开多"]]);
    // 第五批 (2026-09-28): 盘中触价 rows for both sides (a sell limit waits for the price to rise), 分时均价's label.
    const armed = { ...plan, rules: { ...plan.rules,
      entryLong: { join: "all", sequence: false, conditions: [], trigger: { kind: "stop", price: { ...close, indicator: "prev_high" } } },
      entryShort: { join: "all", sequence: false, conditions: [], trigger: { kind: "limit", price: { ...close, indicator: "vwap" } } } } } as unknown as StrategyPlan;
    expect(planBlockRows(armed, "entry")).toEqual([
      ["盘中触价", "开多条件收盘成立后挂价「昨高」，下一根K线里价格涨到它就开多，没触及就作废"],
      ["盘中触价", "开空条件收盘成立后挂价「分时均价」，下一根K线里价格涨到它就开空，没触及就作废"]]);
  });

  test("sixth-batch rows read like the Runtime, and the designer's checks match the server's (2026-09-28)", () => {
    const plan = { name: "加仓与分批", description: "第六批", direction: "long", timeframeSeconds: 300, maxPosition: 4, questions: [],
      pyramiding: { maxAdds: 3, trigger: "profit_ticks", step: 5, atrPeriod: 20, volume: 1, moveStop: true },
      scaleOut: [{ profitTicks: 20, profitAtr: null, percent: 50 }, { profitTicks: 40, profitAtr: null, percent: 25 }],
      rules: { entryLong: null, exitLong: null, entryShort: null, exitShort: null } } as unknown as StrategyPlan;
    // The 加仓 row says 收紧 (not 上移) and spells out the first-entry reserve, like plan_feature_lines.
    expect(planBlockRows(plan, "entry")).toEqual([["加仓", "每盈利 5 跳加 1 手，最多加 3 次，止损随最近一次加仓收紧；"
      + "首笔开 1 手给加仓留空间（最大持仓 4 − 每次加仓手数 × 最多加仓次数，最少 1 手）"]]);
    expect(planBlockRows(plan, "exit")).toEqual([["分批止盈", "盈利 20 跳平 50%，盈利 40 跳平 25%；按持仓均价判断，每档一次，剩下的照原止损止盈走"]]);
    // The designer refuses an invalid edit with the server's own Chinese lines before any request.
    expect(designerPlanProblem(plan)).toBeNull();
    expect(designerPlanProblem({ ...plan, pyramiding: undefined, scaleOut: undefined })).toBeNull();
    expect(designerPlanProblem({ ...plan, breakeven: { triggerAtr: 1, offsetTicks: 0 } })).toBe("加仓暂不和保本/追踪止损/分段止盈一起用");
    expect(designerPlanProblem({ ...plan, trailingStop: { mode: "percent", value: 0.8, activateAfter: 0.5, atrPeriod: 14 } })).toBe("加仓暂不和保本/追踪止损/分段止盈一起用");
    expect(designerPlanProblem({ ...plan, timeTakeProfit: [{ afterBars: 20, profitAtr: 2 }] })).toBe("加仓暂不和保本/追踪止损/分段止盈一起用");
    expect(designerPlanProblem({ ...plan, maxPosition: 1 })).toBe("加仓须最大持仓至少 2 手");
    expect(designerPlanProblem({ ...plan, pyramiding: { ...plan.pyramiding!, maxAdds: 6 } })).toBe("加仓最多加仓次数须在 1–5 次");
    expect(designerPlanProblem({ ...plan, pyramiding: { ...plan.pyramiding!, step: 0 } })).toBe("加仓步长须大于 0");
    expect(designerPlanProblem({ ...plan, pyramiding: { ...plan.pyramiding!, trigger: "profit_atr", atrPeriod: 1 } })).toBe("加仓 ATR 周期须在 2–200");
    expect(designerPlanProblem({ ...plan, pyramiding: { ...plan.pyramiding!, volume: 21 } })).toBe("加仓每次手数须在 1–20 手（留空跟首笔）");
    expect(designerPlanProblem({ ...plan, scaleOut: [{ profitTicks: 20, percent: 50 }, { profitAtr: 2, profitTicks: null, percent: 25 }] }))
      .toBe("分批止盈各档须同用跳数或同用 ATR 倍数");
    expect(designerPlanProblem({ ...plan, scaleOut: [{ profitTicks: 40, percent: 50 }, { profitTicks: 20, percent: 25 }] }))
      .toBe("分批止盈各档目标须由近到远递增");
    expect(designerPlanProblem({ ...plan, scaleOut: [{ profitTicks: 20, percent: 50 }, { profitTicks: 40, percent: 25 }, { profitTicks: 60, percent: 25 }, { profitTicks: 80, percent: 25 }] }))
      .toBe("分批止盈最多 3 档");
    expect(designerPlanProblem({ ...plan, scaleOut: [{ profitTicks: 20, profitAtr: 1, percent: 50 }] })).toBe("分批止盈第 1 档须写跳数或 ATR 倍数其中一个");
    expect(designerPlanProblem({ ...plan, scaleOut: [{ profitTicks: 20, percent: 0 }] })).toBe("分批止盈第 1 档平掉百分比须在 1–99%");
  });
});

describe("外盘策略上线（外盘三期）", () => {
  test("上线框认 CME 月份合约与外盘图表主连，按品种范围放行", () => {
    rememberStrategyScopes([{ symbol: "KQD.m@COMEX.GC", strategyScope: "CME.GCE" }, { symbol: "KQD.m@COMEX.SI", strategyScope: "CME.SIE" }]);
    const item = { definition: { instrumentScope: { mode: "selected", symbols: ["CME.GCE"] } } } as never;
    expect(CONTRACT.test("CME.GCEZ26") && CONTRACT.test("KQD.m@COMEX.GC") && CONTRACT.test("SHFE.rb2701")).toBe(true);
    expect(CONTRACT.test("CME.GCE")).toBe(false);
    expect(allowsSymbol(item, "CME.GCEG27")).toBe(true);
    expect(allowsSymbol(item, "CME.SIEZ26")).toBe(false);
    expect(allowsSymbol(item, "KQD.m@COMEX.GC")).toBe(true);
    expect(allowsSymbol(item, "KQD.m@COMEX.SI")).toBe(false);   // 白银图表不在黄金策略的范围里
    expect(allowsSymbol(item, "KQD.m@NYMEX.CL")).toBe(false);   // 目录没给范围的主连不放行
    expect(allowsSymbol(item, "SHFE.au2612")).toBe(false);
    const domestic = { definition: { instrumentScope: { mode: "selected", symbols: ["SHFE.rb"] } } } as never;
    expect(allowsSymbol(domestic, "SHFE.rb2701")).toBe(true);
    expect(allowsSymbol(domestic, "KQD.m@COMEX.GC")).toBe(false);
  });
});
