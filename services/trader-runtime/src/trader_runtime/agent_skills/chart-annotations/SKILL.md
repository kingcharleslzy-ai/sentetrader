---
name: chart-annotations
description: 在图表的 AI 标注层画线、标记关键位与编号说明（支撑阻力、进出场点、策略信号位置）。当用户说"把止损位画到图上""标一下关键位""在图上注明进场点"时使用。
metadata:
  ai-trader-label: 图上标注
  ai-trader-parent: "chart-analysis"
  ai-trader-summary: "AI 在图上画价位、区间等标注"
  ai-trader-category: 行情
  ai-trader-tools: annotate_chart
  ai-trader-routes: ""
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: chart:annotations
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
---

# 图上标注（chart-annotations）

把分析结论落到图上：AI 标注层画水平线/箭头/编号标记，配合文字说明，用户看图即懂。

## 何时用

- "把支撑位/止损位画到图上""标一下关键位"。
- 策略信号解读后把进出场位置画出来。
- "在图上给我注明这个位置为什么重要"。

## 何时不用

- 只是问位置在哪（不用画）→ 图表与指标（chart-analysis）口头/文字回答。
- 高亮界面某个按钮/面板 → 界面控制与读屏（ui-control）的 highlight_ui。
- 改图表周期/打开面板 → 界面控制与读屏（ui-control）。

## 步骤

1. 明确要标的内容与位置（价格位或K线位置）。
2. `annotate_chart` 画到 AI 标注层（编号标记 + 线条），可聚焦。副图标注的 pane 取 main/kd/macd/rsi/oi，只画在当前显示的副图上；pane 没显示时该条不画并写进回执 notDrawnPanes，先让用户开那格副图（preferences 改 chart.subPaneList）再画。
3. 用一句话把编号与含义对上（"① 是建议进场点，② 是止损"）。

## 安全边界

- 标注是说明性图层，不触发任何交易；撤清"画了止损位"≠"挂了止损单"（挂单走 order-ops）。
- 用户当前看的合约与标注合约不一致时先说明。

## 典型说法

- 把止损位画到图上
- 标一下关键位
- 在图上注明进场点

## 反例（不该触发）

- 给这个持仓挂个止损（真实止损单，走 order-ops）
- 高亮下单按钮（界面高亮，走 ui-control）
- 切到日线（界面命令，走 ui-control）
