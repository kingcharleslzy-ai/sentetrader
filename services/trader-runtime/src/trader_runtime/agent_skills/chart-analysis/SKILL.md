---
name: chart-analysis
description: 读取K线历史数据做技术指标与形态分析（均线、MACD、KD、支撑阻力、突破回踩等）。当用户说"看看KD和MACD""均线现在什么状态""最近30根K线什么形态""哪里是支撑位"时使用。
metadata:
  ai-trader-label: 图表与指标
  ai-trader-parent: ""
  ai-trader-summary: "AI 读 K 线和均线、MACD、KD 等指标给出分析"
  ai-trader-category: 行情
  ai-trader-tools: get_chart_bars
  ai-trader-routes: ""
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: chart
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
---

# 图表与指标（chart-analysis）

按需读取K线序列，计算与解读技术指标和K线形态，给出图上依据。

## 何时用

- 用户问指标状态："KD金叉了吗""MACD什么情况""均线多头排列没有"。
- 用户要形态或关键位："最近什么形态""上方压力在哪""突破了吗"。
- 需要用具体K线数据支撑结论时。

## 何时不用

- 只是要切换图表周期/打开图表（"看日线"）→ 界面控制与读屏（ui-control）。
- 要把关键位画到图上 → 图上标注（chart-annotations）。
- 问现价/涨跌（不需要K线序列）→ 行情与合约查询（market-lookup）。
- 新闻或消息面归因 → 联网研究（web-research）。

## 步骤

1. 明确合约与周期；未指定时用用户当前看的合约与常用周期。
2. `get_chart_bars` 取所需K线（够用即可，不取超量数据）。
3. 基于数据描述指标/形态，区分"数据事实"与"走势判断"。
4. 结论落到"现在怎么看/怎么做"，需要画出来时配合图上标注。

## 安全边界

- 只读数据，不下单、不改图上状态（标注走 chart-annotations）。
- 指标是工具不是信号源：不承诺指标必然生效。
- 数据不足（K线太少、合约无数据）时直说，不硬算。

## 典型说法

- 帮我看看KD和MACD
- 均线现在是什么排列
- 日线上最近是什么形态，支撑在哪

## 反例（不该触发）

- 把止损位画到图上（画图动作，走 chart-annotations）
- 切到1小时K线（界面命令，走 ui-control）
- 螺纹现在多少钱（实时报价，走 market-lookup）
