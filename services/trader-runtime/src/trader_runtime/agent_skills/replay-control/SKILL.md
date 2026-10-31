---
name: replay-control
description: 控制正在进行的行情回放：播放、暂停、单步（一步一根 1 分钟线）、1/2/5/10 步/秒调速与退出回放。当用户在回放中说"暂停""继续播放""加速到5倍""走一步""退出回放"时使用。
metadata:
  ai-trader-label: 回放控制
  ai-trader-parent: "market-replay"
  ai-trader-summary: "AI 帮你播放、暂停、单步、调速回放"
  ai-trader-category: 复盘
  ai-trader-tools: control_replay
  ai-trader-routes: POST /api/replays/{identifier}/control
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: replay:controls
  ai-trader-quick-action: ""
  ai-trader-default-tiers: basic pro test
  ai-trader-mount-words: 回放
---

# 回放控制（replay-control）

回放中的播放操作：与回放窗口按钮同一条控制路由，语音说"暂停/继续/快一点"直接生效。

## 何时用

- 回放进行中："暂停""继续播放""加速到 5 倍""走一步""退出回放"。
- 相对调速："快一点""慢一点"（在 1/2/5/10 步/秒档间移动；用户说"5 倍"即 5 步/秒档）。

## 何时不用

- 开新回放 → 行情回放（market-replay）。
- 不在回放中说的"暂停"（比如暂停策略监控）→ 按上下文走对应技能。
- 回放里要下单 → 走下单出卡（order-proposal），卡片在回放账户内成交。

## 步骤

1. 确认在回放中（不在则如实说"现在不在回放里"）。
2. 解析动作与速度；相对说法解析到相邻档位。
3. 走 `control_replay` / POST /api/replays/{id}/control，播报结果。

## 安全边界

- 只控制回放，不碰真实模拟账户。
- 按时间跳转（"快进到下午"）不支持，如实拒绝。
- 退出（close）永远可用。

## 典型说法

- 暂停回放
- 加速到5倍
- 退出回放

## 反例（不该触发）

- 回放一下昨天的行情（开新回放，走 market-replay）
- 暂停策略监控（不在回放中，走 strategy-management）
- 回放里买多一手（下单，走 order-proposal）
