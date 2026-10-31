---
name: strategy-signals
description: 解读策略当前信号：按用户策略规则讲"现在该怎么做"，并把进出场位置以编号标注画到图上。当用户说"按我的策略现在该怎么做""现在能开多吗""我的策略出信号了吗"时使用。
metadata:
  ai-trader-label: 策略信号解读
  ai-trader-parent: "strategy-management"
  ai-trader-summary: "解读运行中策略发出的开平仓信号"
  ai-trader-category: 策略
  ai-trader-tools: explain_strategy_signal
  ai-trader-routes: GET /api/strategies/signal
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: strategy:signal
  ai-trader-quick-action: prompt:按我的策略现在该怎么做
  ai-trader-default-tiers: basic pro test
  ai-trader-mount-words: 按我的策略 策略信号
---

# 策略信号解读（strategy-signals）

只读解读：拿用户已有策略的规则对照当前行情，给出"现在该怎么做"（开/平/观望）与依据，并把位置画上图。

## 何时用

- "按我的策略现在该怎么做""我的策略现在什么信号"。
- "现在能开多吗"（用户上下文里挂着某个策略时）。
- 监控开着，用户想口头问一句当前判定。

## 何时不用

- 没有策略时的"现在怎么看" → 行情与合约查询 + 图表与指标。
- 要启动/暂停监控 → 策略管理与监控（strategy-management）。
- 要直接下单 → 解读之后按用户指令走下单出卡；解读本身不下单。
- 要回测历史表现 → 策略验证与回测（strategy-backtests）。

## 步骤

1. 确认解读哪个策略（用户当前策略/点名的策略）。
2. `explain_strategy_signal`：得到当前判定（动作、触发条件、失效条件）与图上标注。
3. 用"结论—为什么—现在怎么做"一句话说清；标注已画到 AI 标注层，提醒用户看图。

## 安全边界

- 只读：解读不等于下单，不替用户确认任何卡。
- 判定必须引用策略自己的规则语义，不混入即兴观点。
- 策略未监控时说明"这是规则解读，不是正在执行的监控"。

## 典型说法

- 按我的策略现在该怎么做
- 我的策略出信号了吗
- 现在能开多吗

## 反例（不该触发）

- 帮我开多一手（直接下单，走 order-proposal）
- 回测一下这个策略（走 strategy-backtests）
- 现在行情怎么样（无策略上下文，走 market-lookup）
