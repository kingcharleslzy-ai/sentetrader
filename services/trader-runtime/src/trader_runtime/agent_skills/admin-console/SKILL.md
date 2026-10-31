---
name: admin-console
description: 后台管理（仅主账号）：成员与档位管理、技能启停与档位开放矩阵、公共行情源、模型与用量监督；邀请码是主人在后台网页手动发的。当主账号说"看看成员""把friend调到pro档""停用某技能"时使用。
metadata:
  ai-trader-label: 后台管理
  ai-trader-parent: ""
  ai-trader-summary: "后台管理（仅主账号）"
  ai-trader-category: 系统
  ai-trader-tools: ""
  ai-trader-routes: PUT /api/admin/users/{username} POST /api/auth/owner/invite-codes POST /api/auth/owner/invite-codes/{id}/disable POST /api/admin/plans PUT /api/admin/plans/{name} DELETE /api/admin/plans/{name} PUT /api/admin/market-sources POST /api/admin/market-sources/tqkq DELETE /api/admin/market-sources/{account_id} POST /api/admin/memory/trace DELETE /api/admin/memory/trace
  # GET/PUT /api/admin/skills（与 PUT /api/admin/skills/{skill_id}）刻意不归属本技能：它们是恢复入口，停用 admin-console 不得让主账号无法再启用技能。
  # 两条邀请码写路由只为归属与技能闸门登记在这里（skill_registry.HANDLER_GATED_WRITE_ROUTES）；它们不在能力目录（529-A2b），AI 不调用。
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "true"
  ai-trader-ui-entries: ""
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
---

# 后台管理（admin-console）

主账号专属：成员（档位、停用）、档位（模型/额度/技能勾选）、邀请码（自定义码、一次/不限次数、档位、长期有效、停用）、技能全局启停、公共行情源清单、项目主机状态与用量监督。非主账号完全不可见（404）。后台界面是独立网页 qilinshuzhi.com/trader/admin，不在交易工作区窗口里：agent 只用下面的管理员接口操作并回读结果，没有能打开后台的界面命令；需要主人看界面时，告诉主人用浏览器打开该网址并点左侧导航的对应分区（运营：概览/用户/档位/邀请码 · 能力：技能 · 交易：交易账户/行情源 · 基础设施：服务器/用户体验）。

## 何时用

- "看看成员/用量""把 friend 调到 pro 档""停用这个成员"。
- "新建一个档位""基础档加语音时长"。
- "停用「联网研究」技能""哪些档位开了策略回测"。
- "看看行情源""行情源加一个账号""把二号行情源排到第一位"。
- "用户那边卡不卡""价格延迟多少""手机端报错多吗"（用户体验汇总）。

## 何时不用

- 自己的偏好 → 偏好设置（preferences）。
- 普通成员的任何请求 → 不是这个技能的对象。
- 认证本身（登录/登出）→ 认证接口，不属于管理面。
- 邀请码（“发个邀请码”“停用那个邀请码”）：生成、列表、停用是主人在后台网页的人工动作，不在能力目录（529-A2b）——不要调用 `/api/auth/owner/invite-codes`，告诉主人打开 qilinshuzhi.com/trader/admin 左侧「邀请码」分区操作（可选自定义码、一次/不限次数、档位、长期有效，停用在同一列表）。

## 步骤

1. 读现状：GET /api/admin/users、/api/admin/plans、/api/admin/skills；项目主机资源用 GET /api/admin/servers?range=1h|24h|7d（运维台每 5 分钟采集）；各端页面的真实使用体验（页面打开、操作响应、价格延迟、K 线首屏、断线重连、报错类别，按网页/桌面/手机分组的分位数与时序）用 GET /api/admin/rum?range=1h|24h|7d。
2. 改动：PUT /api/admin/users/{username}、PUT /api/admin/plans/{name}（skills/features 均按键合并）、PUT /api/admin/skills/{id}（全局启停）、PUT /api/admin/market-sources（公共行情源整份有序清单，只收库中 active 的快期池/sidecar 账户，立即生效）。用快期账号与口令新加一个“行情专用”账号、或移除它（删除托管口令），是主人在后台「行情源」页的人工操作，不在能力目录里：主人说“行情源加一个账号”时，库里已有的快期账户可以用 PUT 加到末尾；要新输口令的，请主人打开 qilinshuzhi.com/trader/admin → 行情源，在“添加快期行情账号”里输入账号与口令点“验证并加入”。
3. 回读确认改动生效（档位是每请求读库的事实）。
4. 内存诊断（查 Runtime 内存泄漏时临时用，默认关闭）：POST /api/admin/memory/trace 打开追踪并以此刻为起点，等一段时间后 GET /api/admin/memory（可带 types=true）看起点之后一直没释放的内存按调用栈排行，查完 DELETE /api/admin/memory/trace 关闭。追踪期间多占内存，不要一直开着。

## 安全边界

- 仅主账号；全局停用一个技能对所有人生效（包括主账号自己）。
- 档位勾选只影响成员；主账号不受档位约束。

## 典型说法

- 看看成员用量
- 把friend调到pro档
- 停用联网研究技能

## 反例（不该触发）

- 默认手数改成两手（个人偏好，走 preferences）
- 帮我开多一手（交易，走 order-proposal）
- 我要注册个账号（成员自己注册，走认证入口）
