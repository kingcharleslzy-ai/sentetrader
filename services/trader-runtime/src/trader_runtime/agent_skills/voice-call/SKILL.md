---
name: voice-call
description: 实时语音通话：与 AI 打电话查行情、下单出卡、口头确认/取消、操作界面与主动播报。当用户说"开始通话""打电话给AI""接通语音"时使用。
metadata:
  ai-trader-label: 语音通话
  ai-trader-parent: ""
  ai-trader-summary: "和 AI 实时语音通话"
  ai-trader-category: 语音
  ai-trader-tools: ""
  ai-trader-routes: POST /api/voice/realtime/announce
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: chat:voice-call
  ai-trader-quick-action: ""
  ai-trader-default-tiers: basic pro test
---

# 语音通话（voice-call）

实时语音入口：语音前台负责快查与界面操作，业务请求转给同一个交易 AI；下单卡口头念出后"确认/取消"生效。

## 何时用

- "开始通话""打电话给 AI""接通"。
- 通话中的播报接口（announce）触达主动提醒。
- 对话面板输入行（和手机版）的“按住说话”（`/api/voice/transcribe` 转写，松开后作为一条消息发给 AI，AI 回答的要点经 `/api/voice/speak` 用通话音色读出来——不是只转文字）也受这项技能管：档位没开通语音时按住说话同样不可用。

## 何时不用

- 不想通话、只打字 → 正常对话即可，能力相同。
- 换音色/语速 → 偏好设置（preferences）。
- 通话里说“帮我设计一个策略 / 根据我的交易生成策略”会直接打开右边的策略编辑卡；卡开着时说“止损改成 20 跳”这类话送进卡里的“补一句”（策略管理技能，strategy-management）。

## 步骤

1. 用户点通话或说"开始通话"，客户端建 WebSocket 通话。
2. 通话中：行情/持仓/账户快查由语音前台直接答；业务请求转交易 AI；卡片念出后等"确认/取消"。
   开了前台直接查询（Runtime 开关 `AI_TRADER_VOICE_FRONT_QUERY`）的账号：价格涨跌、持仓、资金、活动委托、快讯标题、简讯、日线走势都在前台查完念出（`query` 工具，正式接口取数），止损/保证金这类概念、寒暄、半句话的反问也在前台答，不转交易 AI；这些轮次照样记进同一对话。“能不能买”这类判断、下单、策略、设置、界面仍转交易 AI。
   另开了下单类直接出卡（Runtime 开关 `AI_TRADER_VOICE_FRONT_ACTION`）的账号：一句话说全了的开仓、平几手、全平/平某品种、调到目标持仓由 Runtime 按原话直接用同一套出卡工具出卡并念出（合约、方向、手数、价格、确认吗），撤单只撤唯一一笔挂单；没说方向、多个品种、条件、随单止损止盈、价差、策略、判断仍转交易 AI。这些卡和交易 AI 的卡一样等“确认/算了”，也记进同一对话——在打字回合里看到语音出的卡，按普通待确认卡处理即可。
3. "关闭语音/挂断"结束通话。

## 安全边界

- 口头"确认"只作用于刚念出的待确认卡；没有卡时"确认"不做事。
- 有卡等待时，"确认/取消"永远优先于其他判定（包括策略编辑卡开着时）。
- 通话有档位日时长限制；回放命名空间中的通话全部作用于该回放。
- 档位未开通或全局停用时，状态接口说明原因，socket 拒绝。

## 典型说法

- 开始通话
- 打电话给AI
- 接通语音

## 反例（不该触发）

- 语音说慢一点（音色/语速偏好，走 preferences）
- 帮我设计一个策略（打开策略编辑卡，走 strategy-management）
- 关闭语音（挂断，由通话路由直接处理）
