---
name: target-position
description: 把某个品种调到目标持仓（多三手、清到两手、空一手等），Runtime 计算差额后出一张增减仓确认卡。当用户说"把螺纹调到三手多单""沪金清到只剩两手""这个品种我想持有5手空单"时使用。
metadata:
  ai-trader-label: 目标持仓
  ai-trader-parent: "order-proposal"
  ai-trader-summary: "说“调到三手多单”，AI 算出要下的单并出卡确认"
  ai-trader-category: 交易
  ai-trader-tools: ""
  ai-trader-routes: POST /api/simulation/target-position/proposals
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: ""
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
---

# 目标持仓（target-position）

用户说出"目标状态"，Runtime 对比当前持仓算出需要开的仓/平的仓，生成一张差额卡等待确认。

## 何时用

- "把螺纹调到三手多单""螺纹要 3 手多""螺纹做到多 2 手，挂买一价"（做到/加到/减到 N 手是目标，不是再下 N 手）。
- "沪金清到只剩两手""这个品种留两手空单"。
- 用户关心的是"最后持有多少"，不是"这一笔下多少"。

## 何时不用

- 开新仓或平指定一单 → 下单出卡（order-proposal）。
- 全部平掉/清仓 → 批量平仓（batch-close）。
- 只是问现在持有多少 → 复盘与日结（review-summary）。

## 步骤

1. 简单的说法（"螺纹调到多三手""把螺纹仓位清零"）Runtime 直接出卡；其余用 `propose_order`：act=target，target 写目标净持仓（多为正、空为负、清零为 0），不要自己算差额；反手写 act=reverse。用户说了价格方式就填 price（挂单/排队 queue、对手价/市价 counter、最新价 last），没说不填。
2. Runtime 读当前持仓与挂单，算差额（开/平/不动），出第一段的卡；价格方式用于计划的每一步（含反手的全平与第 2 步开仓）。
3. 复述差额内容（"你现在 1 手多，调到 3 手要多开 2 手"），等用户确认。

## 安全边界

- 与下单出卡同一条铁律：只出卡，确认后才执行。
- 反手两段只确认一次（2026-10-24）：用户确认第一段的卡，也就确认了第二段；第一段全部成交后 Runtime 自动发第二段（发单时照常复查），你不要再出第二段的卡或替用户确认。复述时说清两步，如“平多 3 手，成交后自动开空 3 手，确认一次”。上一段还在场上（含部分成交）就继续等；已结束但没全部成交就取消计划、不开仓，并说明第几段成交了几手，先在委托里确认原单状态再重新发起；对话切到别的账户后计划自动取消。
- 目标与当前一致时如实说"已经是目标持仓"，不出空卡。
- 该合约还有没成交的挂单（开仓或平仓）时不出卡，转述 Runtime 的原因（几手什么挂单、先撤单或等成交），不自己把挂单算进差额。
- 模拟账户专属。

## 典型说法

- 把螺纹调到三手多单
- 沪金清到只剩两手
- 这个品种我想持有一手空单

## 反例（不该触发）

- 再加开一手（增量下单，走 order-proposal）
- 全平（走 batch-close）
- 现在手里有什么持仓（查询，走 review-summary）
