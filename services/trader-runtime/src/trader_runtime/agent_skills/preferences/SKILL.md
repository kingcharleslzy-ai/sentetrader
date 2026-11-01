---
name: preferences
description: 查看与修改个人偏好设置：音色语速、回答长短、默认手数、默认价格方式、播报与通知开关（成交通知、收盘前提醒、风险度提醒）、保留天数、模型选择、语音音色、图表均线与副图指标、报价总表的列与排序、自选品种。当用户说"默认手数改成两手""说慢一点""回答简短一点""换个音色""均线改成 5 10 20""副图换成 RSI""报价表加一列昨结""把螺纹加到自选""快收盘提醒我""风险度高了告诉我"时使用。
metadata:
  ai-trader-label: 偏好设置
  ai-trader-parent: ""
  ai-trader-summary: "AI 帮你改设置和偏好"
  ai-trader-category: 系统
  ai-trader-tools: get_preferences update_preferences remember forget recall_memory
  ai-trader-routes: PUT /api/preferences PUT /api/app/preferences PUT /api/models/preference PUT /api/voice/realtime/voice POST /api/memory POST /api/memory/consolidate PUT /api/memory/{memory_id} DELETE /api/memory/{memory_id} POST /api/memory/changes/{change_id}/undo DELETE /api/app/push/subscription POST /api/app/push/test
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: settings
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
  ai-trader-mount-words: 记住 记得 记一下 忘掉 忘了 别记 记忆 习惯 你知道我 以后 别再 保本提醒
---

# 偏好设置（preferences）

个人偏好一处改：默认手数与价格方式、回答长短、语音音色与语速、播报/通知开关（含收盘前提醒 `notify.marketCloseReminder` 默认关、风险度提醒 `notify.riskReminder` 默认开——两者都只提醒，不平仓、不拦单、不加任何限制）、数据保留天数、模型选择、图表指标（均线 1–8 条、布林带、抛物线 SAR、副图 KD/MACD/RSI/持仓量/CCI/成交量最多 3 格及各自参数）、默认周期（`chart.defaultDuration` 1 分到周线；`chart.defaultView` 写 kline 或 timeshare，选分时则打开软件直接进分时图）、报价总表（显示哪些列、按什么排、只看哪个交易所，都在 `marketBoard` 下）、自选品种（`watchlist.products`，星标的三处入口共用一份）。

## 何时用

- "默认手数改成两手""以后都按对手价挂"。
- "默认打开 30 分钟图""以后默认看周线""打开就是分时图"（改 `chart.defaultDuration` / `chart.defaultView`）。
- "说慢一点""换个音色""回答简短一点"。
- "关掉成交播报""保留多少天"。
- "快收盘提醒我""收盘前提醒我一下"（打开 `notify.marketCloseReminder`，默认关；日盘/夜盘最后一段结束前 5 分钟还有持仓就每个半场每个交易日提醒一条，只列即将收盘的合约）。
- "风险度高了告诉我"（`notify.riskReminder` 默认已开：风险度到 80% 当天提醒一次；用户嫌吵才关）。
- "保本提醒赚 2 倍再问""浮盈 3000 块就问我""外盘赚 500 美元就问"（`notify.breakEvenRule`：`mode` multiple 用 `multiple` 倍风险，amount 用 `amount` 元；外盘美元子账户按 `amountUsd` 美元，只在 mode 为 amount 时生效——当前是倍数模式时先说明改成金额模式国内也会按 `amount` 元判断；`amountUsd: null` 或没设＝外盘按倍数，用户没提外盘就不动它）。
- 提醒面板 → 通用提醒（2026-10-15 起每类一个开关，同时管对话、桌面通知、语音与锁屏推送；原“通知哪些事”）："别推盘中异动""盘中异动提醒打开"（默认关）"开盘前判断别发了""成交别通知"（`notify.movement` / `notify.morningDesk` / `notify.desktop.fills` 与 `cancels` 一起改）；"锁屏上别显示金额"（`notify.lockScreenAmounts: false`）；新增 `notify.stopTriggered` / `protectionCancelled` / `orderUnfilled` / `delivery` / `researchDebate`；急停总是提醒，没有开关。
- "我哪些设备开了锁屏提醒""给我手机发一条试试"：`app_call` 读 `GET /api/app/push`、发 `POST /api/app/push/test`（body `{deviceId}` 只发一台）；"把那台 Mac 的锁屏提醒去掉"：`DELETE /api/app/push/subscription?deviceId=`。不能替用户打开锁屏提醒——那要用户在设备上点“打开锁屏提醒”并在系统询问里允许（手机：我的 → 通知设置 → 锁屏提醒；电脑：设置 → 通知）。
- "均线改成 5 10 20""加一条 120 均线""打开布林带""打开 SAR""副图换成 RSI""加个 CCI 副图""副图要成交量""去掉 MACD"（都改 `chart` 下的 maLines / boll / sar / subPaneList，连参数一起写整段；副图 kd / macd / rsi / oi / cci / volume 最多 3 个，手机单图一次显示其中一个——手机窗口上“副图换成 X”走界面命令 `mobileMarket: {subPane}` 更直接）。
- 手机看盘记在 `ui.mobileMarket`（view 报价/多图/单图、board 板块、columns 报价列、layout 多图布局、cells 每格合约与周期、periods 同合约 4 周期的周期、subPane 单图下的副图）；界面上用 `mobileMarket` 命令改，这里只在用户要“以后默认……”时整段写。
- "报价表加一列昨结""总表去掉最高最低""报价表按成交量排""报价表只看郑商所"（改 `marketBoard` 下的 columns / sort / exchange；columns 是完整列序，品种/合约固定在前，先 get_preferences 读当前列再增删）。
- "把螺纹加到自选""自选里删掉沪金""我的自选有哪些"（`watchlist.products` 是"交易所.品种"（如 SHFE.rb）的整段清单，最多 50 个、后加在后；增删先 get_preferences 读整段、改这一项再整段写回，按品种不按月份，换月自动跟主力）。
- "记住我只做螺纹""以后给建议先说止损位"→ remember；"你记得我什么"→ recall_memory；"忘掉那条"→ 先 recall_memory 再 forget。

## 何时不用

- 界面外观（主题、字号）→ 界面控制与读屏（ui-control）。
- 账户/档位/成员管理 → 后台管理（admin-console，仅主账号）。
- 只是问"我设了什么" → `get_preferences` 读后复述。

## 步骤

1. `get_preferences` 看当前值（能口头回答就不用改）。
2. `update_preferences` 一次改清；已是要改的值就说明"已经是了"。
3. 一句话确认改了什么、何时生效（音色/语速下次通话生效）。

## 安全边界

- 偏好只属于当前账号，改完即生效，不需要确认。
- 记忆分画像/约定/经验三层，只记用户亲口说的话；remember 必须带用户原话 quote（服务端核对），
  直接调 `POST /api/memory` 也一样——经能力目录转发的请求一律按对话来源核对原话并受"记忆已关闭"
  约束，body 里写 via=settings 不豁免。改已有记忆 `PUT /api/memory/{memory_id}` 必须带
  `{text, quote}`：quote 是用户这次要求改成这样的原话（同样核对，没带或对不上 422）。记忆关闭
  时新增、修改、撤销都 409，只能 forget；让用户去设置里打开或自己改。任何让 AI 自动下单、
  免确认的话都不记——记忆不是授权，让他去设置里改。
- 记忆的最近变动撤销可沿原状态连续进行；重复请求不重复写入，已有后续修改或旧依据不足时返回409，重读后由用户决定，不盲重发旧撤销。原话整理只采纳模型实际看过且未被人工后来决定改变的目标版本。
- 模型选择受档位限制（不在档位内的模型拒绝并说明）。
- 不改别人的偏好；多设备各自读取同一份。

## 典型说法

- 默认手数改成两手
- 说慢一点
- 关掉成交播报
- 快收盘提醒我
- 风险度高了告诉我
- 均线改成 5 10 20
- 加一条 120 均线
- 打开布林带
- 副图换成 RSI
- 去掉 MACD
- 报价表加一列昨结
- 总表去掉最高最低
- 报价表按成交量排
- 报价表只看郑商所
- 把螺纹加到自选
- 自选里删掉沪金
- 我的自选有哪些

## 反例（不该触发）

- 深色模式（界面主题，走 ui-control）
- 把螺纹调到三手（交易动作，走 target-position）
- 邀请一个成员（后台管理，走 admin-console）
