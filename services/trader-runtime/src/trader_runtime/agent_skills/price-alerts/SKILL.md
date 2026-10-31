---
name: price-alerts
description: 到价提醒：合约涨到/跌到目标价时提醒用户（对话消息＋桌面通知＋通话口播），只提醒不下单。当用户说"涨到3150提醒我""跌破3100告诉我""我有哪些提醒""删掉螺纹的提醒"时使用。
metadata:
  ai-trader-label: 到价提醒
  ai-trader-parent: ""
  ai-trader-summary: "设、查、删合约到价提醒"
  ai-trader-category: 行情
  ai-trader-tools: create_price_alert list_price_alerts cancel_price_alert
  ai-trader-routes: GET /api/market/alerts POST /api/market/alerts DELETE /api/market/alerts/{alert_id}
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: chart:alerts
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
  ai-trader-mount-words: 提醒 预警 通知我 叫我 喊我 告诉我一声 到价
---

# 到价提醒（price-alerts）

用户点一下、说一句就能盯价：合约涨到或跌到目标价时，软件在对话里发消息、桌面弹通知（可关）、通话中直接口播。按账号记，跟行情不跟交易账户；只提醒，从不下单。

历史回放里同样可用（2026-10-03）：只限本次回放的合约（别的合约被拒，中文原因原话转告），按回放最新价判方向、按回放时间触发；提醒记在回放自己的账上，随回放一起消失。

画线提醒（2026-10-31）：用户在图上自己画的线可以“碰到就提醒”（线的右键或样式条“这条线被碰到时提醒我”，接口 `POST /api/market/alerts {drawingId}`）。`list_price_alerts` 的这类提醒带 drawingId，回执写“碰到画的线”；斜线跟着线走，挪线提醒跟着改，删线提醒一起删。用户要给某条线设提醒时请他在线上右键点一下；你自己只按价位设普通到价提醒。

## 何时用

- 用户要盯一个价："螺纹涨到 3150 提醒我""跌破 3100 告诉我""到 3200 说一声"。
- 用户查自己的提醒："我有哪些提醒""设了几个""螺纹那条还在吗"。
- 用户删提醒："删掉螺纹的提醒""那条不要了"。

## 何时不用

- 问现在的价格 → 行情与合约查询（market-lookup）。
- 到价自动下单（条件单）→ 没有这个功能，如实说明只能提醒；要交易走 下单出卡（order-proposal）。
- 成交播报、桌面通知开关 → 偏好设置（preferences）。
- 策略信号提醒（盯策略而不是盯价）→ 策略信号解读（strategy-signals）。

## 步骤

1. `create_price_alert`：symbol 可传合约代码或品种名（品种名自动取当前主力合约，对不上时先 `search_instruments`）；price 必须在该合约的最小变动价位上；direction 可省（按最新价自动判定涨到/跌到）。
2. 回执里有已设内容和现价，向用户复述"已设：螺纹2701 涨到 3150 提醒，现价 3112"。
3. 查与删：`list_price_alerts` 列出生效中和最近已触发的；`cancel_price_alert` 删生效中的（id 从列表拿）。
4. 被拒绝（价位不在跳上、方向与现价矛盾、超过 20 条上限、主连代码）时把中文原因原话转告，不要换个参数重试。

## 安全边界

- 只提醒不下单：本技能的任何工具都不创建委托、不改持仓、不碰风控。
- 上限 20 条生效提醒是数量上限，不是风险规则；到达上限照实转告。
- 用户没说目标价就先问，不要按现价猜一个。

## 典型说法

- 螺纹涨到 3150 提醒我
- 跌破 3100 告诉我
- 我有哪些到价提醒
- 删掉螺纹那条涨到 3150 的提醒

## 反例（不该触发）

- 螺纹现在多少钱（查行情，走 market-lookup）
- 涨到 3150 就帮我开一手（下单，走 order-proposal）
- 提醒我每一笔成交（成交播报偏好，走 preferences）
