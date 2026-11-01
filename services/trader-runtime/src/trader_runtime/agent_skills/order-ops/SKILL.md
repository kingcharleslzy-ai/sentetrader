---
name: order-ops
description: 查询、撤销、追改已发出的模拟委托（撤单、改对手价、查挂单结果）与止损单设置，以及已授权 AI 执行会话内的直接执行、状态查询与停止。当用户说"撤单""这张单改成对手价""挂单成交了吗""给这个持仓挂个止损""停掉 AI 执行"时使用。
metadata:
  ai-trader-label: 撤单/追单/改价
  ai-trader-parent: ""
  ai-trader-summary: "让 AI 撤单、追单、改委托价"
  ai-trader-category: 交易
  ai-trader-tools: query_simulation_order cancel_simulation_order set_simulation_stop_loss cancel_simulation_stop_loss execute_simulation_trade get_ai_execution_status resume_simulation_execution stop_simulation_execution
  ai-trader-routes: POST /api/simulation/orders/{client_order_id}/{action} POST /api/ai-execution/resume
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: ""
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
  ai-trader-mount-words: 自己下单 自动下单 自动执行 自主执行 恢复执行 继续执行 查单 委托状态
---

# 撤单/追单/改价（order-ops）

已发委托与已授权执行会话的操作面：查单、撤单、改价重发、止损单管理，以及 AI 执行会话的直接执行/状态/停止。

## 何时用

- "撤单""把挂着的单撤了""这张单成交了没"。
- "改成对手价""追一下这张单"（先撤旧单再按对手价出卡）。
- "给螺纹多单挂个止损""给我的螺纹多单设个止损 3100 止盈 3200""把止损撤了"。
- "把止损挪到成本""螺纹多单保本"：止损价取 `get_positions` 里这笔持仓的开仓均价（对齐最小变动价位），用 `set_simulation_stop_loss` 挂上并复述；保本提醒卡上的“移到成本”由用户自己点。
- 已授权的 AI 执行会话内："AI 执行状态怎么样""停掉 AI 执行"。

## 何时不用

- 新开仓/平仓出卡 → 下单出卡（order-proposal）。
- 开仓时顺带保护（"开多一手螺纹，止损 3100 止盈 3200"）→ 下单出卡（order-proposal），保护随卡的 `protection` 走。
- 开启新的 AI 自主执行授权 → AI 自主执行（ai-execution）。
- 查持仓/盈亏（不是查某张单）→ 复盘与日结（review-summary）。

## 步骤

1. 查单：`query_simulation_order`（结果未知类回报只查不重发）。
2. 撤单：`cancel_simulation_order`；撤回报未知不重复撤。
3. 改价/追单：先撤旧单、确认已撤销后再走下单出卡重新出卡（新卡仍需确认）。
4. 挂保护：`set_simulation_stop_loss`（止损 `triggerPrice` / 止盈 `takeProfitPrice` 至少传一个；挂上后复述两个价位与手数）/ 撤保护：`cancel_simulation_stop_loss`。策略自己挂的保护（来源 strategy）只读，撤销前先向用户说明并确认。
5. 说剩余：撤单、挂/撤保护和停止/急停（`stop_simulation_execution`）的回报里 `accountAfter` 是写完之后现场重读的持仓、仍挂着的委托和仍生效的保护（已触发、已撤的以及急停撤掉的都不在里面）；向用户说“还剩什么”只按它，不按之前对话里说过的；`available: false` 时不陈述现状，请用户看持仓面板。
6. 执行会话：`get_ai_execution_status` 查看、`stop_simulation_execution` 停止（停止永远可用）。

7. 执行前（2026-10-14 从系统提示移入）：AI 自主下单前现场刷新运行状态、行情、账户、持仓与委托；平台没有默认手数或次数上限，策略与风控设置照常生效。普通停止冻结 AI 一切新动作，但已挂止损止盈照常执行、不占租约额度；只停新开仓（scope opens）只拦开仓（AI 与策略都拦），策略继续监控；急停另撤 AI 的挂单并作废其保护。恢复只在用户明确要恢复/继续时调，恢复不下单、不恢复已撤的单或保护、不重启已暂停的策略；明确的停止请求不必先看新闻或行情。

## 安全边界

- 委托结果"未知/回报异常"时绝不重发，只查询等待回报。
- 改价重发必须先确认旧单已撤销，避免 duplication。
- 直接执行类工具只在已授权的执行会话内有效，无授权时自然拒绝；对话里（用户消息引起的回合）不提供 `execute_simulation_trade`，用户说的单（不管怎么说，含“你自己下单”）一律用 `propose_order` 出卡（所有者 2026-10-16）；授权的开启走 ai-execution。挂保护是用户明确指令，不需要自主执行授权；工具被环境拒绝时不要绕路，指引用户在持仓面板该行点"保护"自己填。
- 模拟账户专属。

## 典型说法

- 撤单
- 这张单改成对手价
- 挂单成交了吗
- 给这个持仓挂个止损
- 给我的螺纹多单设个止损 3100 止盈 3200
- 把螺纹多单的保护撤了

## 反例（不该触发）

- 买多一手（新下单，走 order-proposal）
- 全平（走 batch-close）
- 开始 AI 自主执行（新授权，走 ai-execution）
