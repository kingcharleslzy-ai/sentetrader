---
name: market-briefs
description: 行情简讯：订阅品种的自动要点推送与手动刷新（价格异动、板块要闻的一句话简讯）。当用户说"刷新一下简讯""简讯怎么没了""今天有什么简讯"时使用。
metadata:
  ai-trader-label: 行情简讯
  ai-trader-parent: ""
  ai-trader-summary: "盘中自动推送行情简讯"
  ai-trader-category: 研究
  ai-trader-tools: ""
  ai-trader-routes: GET /api/app/brief-subscriptions PUT /api/app/brief-subscriptions POST /api/app/briefs/refresh
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: briefs
  ai-trader-quick-action: ""
  ai-trader-default-tiers: basic pro test
---

# 行情简讯（market-briefs）

订阅品种的简讯流：系统自动生成 + 用户手动刷新；语音通话中可播报新简讯（按偏好开关）。

## 何时用

- "刷新一下简讯""现在有什么简讯"。
- 用户问"刚才那条异动是什么"。
- "简报订阅螺纹和铁矿""简报别订这个品种了"。
- 偏好里开了简讯播报时，语音主动提醒。

## 何时不用

- 深度新闻/归因研究 → 联网研究（web-research）；简讯是一句话要点，不是研究报告。
- 问实时报价 → 行情与合约查询（market-lookup）。

## 步骤

1. 读取简讯列表：`GET /api/app/briefs`（不带 symbol，合并全部订阅品种，按时间新到旧，每条带 product/productName/symbol；带 symbol 只读该合约，是旧客户端契约）。
2. 读订阅：`GET /api/app/brief-subscriptions`（品种列表、档位上限 limit——null=不限、来源 source；overLimit=true 表示档位调低后超出上限，读取与生成只覆盖前 limit 个）。
3. 改订阅：`PUT /api/app/brief-subscriptions`，body `{"products":["SHFE.rb","DCE.i"]}`（有序去重；空列表=不订阅；超过档位上限 422 `brief_limit_exceeded`，但 overLimit 时只删减已存品种的整表照存，可逐个删到上限内）。默认订阅跟随自选品种前 N 个，没有自选时取当前主力合约所在品种 1 个；用户设置过就以设置为准。
4. 手动刷新走 `POST /api/app/briefs/refresh`：body `{}` 在订阅品种间轮转挑一个到期的发起检索（返回 product）；带 symbol 只刷该合约。
5. 复述要点，一句话一条，标注时间。

## 安全边界

- 简讯只描述已发生的事实，不给买卖指令。
- 未开通时读取返回"已停用"状态，如实告知，不发起模型调用。

## 典型说法

- 刷新一下简讯
- 今天有什么简讯
- 刚才那条异动简讯说的是什么

## 反例（不该触发）

- 为什么会这样，帮我查查原因（深度归因，走 web-research）
- 螺纹现在多少钱（实时报价，走 market-lookup）
- 关掉简讯推送（偏好设置，走 preferences）
