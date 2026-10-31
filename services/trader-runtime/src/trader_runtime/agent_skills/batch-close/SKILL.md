---
name: batch-close
description: 一次性平掉全部持仓或某方向/某品种的所有持仓（全平、清仓、所有空单平掉、不带手数的“平掉螺纹”），生成一张批量平仓确认卡。当用户说"全平""清仓""把所有空单都平了""多单全部平掉""平掉螺纹""把沪金平了"时使用。
metadata:
  ai-trader-label: 批量平仓
  ai-trader-parent: "order-proposal"
  ai-trader-summary: "一句话把多个持仓一起平掉，先出确认卡"
  ai-trader-category: 交易
  ai-trader-tools: ""
  ai-trader-routes: POST /api/simulation/close-batch-proposals
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: ""
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
---

# 批量平仓（batch-close）

按范围（全部/某方向/某品种）把持仓一次性平掉：Runtime 汇总每条持仓生成一张批量卡，用户确认后逐条发送。

## 何时用

- "全平""清仓""都平了"。
- "所有空单平掉""把螺纹的仓都平了"。
- 不带手数点名品种："平掉螺纹""把沪金平了""把螺纹和沪金都平了"——Runtime 按持有的月份平；没有该品种持仓时如实说明。

## 何时不用

- 只平某一笔指定持仓（"平掉刚才那张"）→ 下单出卡（order-proposal）。
- 调到目标手数（留一部分）→ 目标持仓（target-position）。
- 撤掉挂单（没成交的单）→ 撤单/追单/改价（order-ops）。

## 步骤

1. 范围就是用户说的：全部 / 仅多单 / 仅空单 / 某几个品种。简单的说法 Runtime 直接出卡；其余由 `propose_order`（act=close_all，dir 写多单/空单，symbols 写点名的品种或合约，不写=全部持仓）出卡，不先读持仓、不逐个平。
2. Runtime 读整账户持仓，按今/昨、对手价拆成一张批量卡（列出每条合约、方向、手数）。
3. 复述范围与合计，等用户确认；确认后按实际回报逐条播报。

## 安全边界

- 只出卡，确认后才执行；批量卡同样走确认时效。
- 当前没有符合范围的持仓时如实说"没有可平的持仓"，不出空卡。
- 模拟账户专属。

## 典型说法

- 全平
- 清仓
- 把所有空单都平掉

## 反例（不该触发）

- 平掉一手（单笔平仓，走 order-proposal）
- 撤单（撤销挂单，走 order-ops）
- 螺纹留两手其他全平（部分保留，走 target-position 或逐笔确认）
