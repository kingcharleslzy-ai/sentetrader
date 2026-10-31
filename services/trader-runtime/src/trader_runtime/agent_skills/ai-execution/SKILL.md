---
name: ai-execution
description: 开启/调整 AI 自主执行授权（限定品种、手数、笔数、时长与权限范围的模拟自动交易会话）。当用户说"开始AI执行""让AI自动盯螺纹""自主执行限额改成5笔"时使用。
metadata:
  ai-trader-label: AI 自主执行
  ai-trader-parent: ""
  ai-trader-summary: "让 AI 在你设定的品种、手数、笔数和时长内自己下模拟单"
  ai-trader-category: 交易
  ai-trader-tools: ""
  ai-trader-routes: POST /api/ai-execution/start POST /api/ai-execution/limits
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: execution
  ai-trader-quick-action: ""
  ai-trader-default-tiers: pro test
---

# AI 自主执行（ai-execution）

授权 AI 在限定范围内自动执行模拟交易：品种范围、单笔手数上限、总笔数上限、时长与允许的动作。开启后策略自动下单等 AI 自己发起的操作在授权内直接执行；对话里用户说的单一律出卡确认，不直接成交（所有者 2026-10-16；会话内操作见撤单/追单/改价技能）。

## 何时用

- "开始 AI 执行""让 AI 盯着螺纹自动做"。
- "把自主执行的笔数上限改成 5 笔、手数上限 2 手"。

## 何时不用

- 单笔下单（自己确认）→ 下单出卡（order-proposal）。
- 查询/停止正在跑的执行 → 撤单/追单/改价（order-ops，停止永远可用，不随本技能停用受限）。
- 策略监控（按策略规则自动决策）→ 策略管理与监控（strategy-management）。

## 步骤

1. 和用户确认授权要素：品种、单笔手数上限、总笔数上限、时长、允许动作（开/平/撤/停止）。
2. `POST /api/ai-execution/start` 开启；`POST /api/ai-execution/limits` 调整仍在跑的会话限额。
3. 开启后告知用户如何一句话停止（"停止 AI 执行"）。

## 安全边界

- 授权范围外的一律拒绝；授权过期自动失效，不自动续期。
- 仅模拟账户；确定性检查（报价新鲜、涨跌停、持仓）在每笔执行内仍然生效。
- 停止路径永远开放：停用本技能不影响停止与状态查询。

## 典型说法

- 开始 AI 执行，就盯螺纹，最多一手
- 自主执行限额改成 5 笔
- 让 AI 自动盯盘做模拟单

## 反例（不该触发）

- 买多一手螺纹（单笔下单走卡片确认，不需要授权）
- 停止 AI 执行（停止操作，走 order-ops，永远可用）
- 把我的策略跑起来（策略监控，走 strategy-management）
