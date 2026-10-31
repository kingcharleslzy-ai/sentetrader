---
name: ui-control
description: 操作与观察软件界面：切换面板/页面/主题/字号/周期/合约、高亮界面元素、读屏快照、新手引导、扩展与界面配置管理。当用户说"打开设置""切到复盘""深色模式""字大一点""X在哪里""带我走一遍"时使用。
metadata:
  ai-trader-label: 界面控制与读屏
  ai-trader-parent: ""
  ai-trader-summary: "AI 读取当前界面，帮你打开页面、切换设置"
  ai-trader-category: 界面
  ai-trader-tools: highlight_ui start_tour
  ai-trader-routes: POST /api/app/ui/commands POST /api/ui/pulse POST /api/app/ui/windows/{window_id}/captures PUT /api/app/config POST /api/app/config/rollback
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: ""
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
  ai-trader-mount-words: 引导 教程 新手 带我看 带我走 教我用
---

# 界面控制与读屏（ui-control）

界面操作的统一入口：UI 命令（面板/主题/字号/周期/合约/对话框/披露卡）、元素高亮、读屏观察快照、新手引导，以及扩展与界面配置的读写。

## 何时用

- "打开设置""切到复盘页""看持仓面板""看五档"（五档在下单面板顶部：`panel: order`）。
- "深色模式""字大一点""看 15 分钟线""切到螺纹"。
- "下单按钮在哪""指给我看"→ 打开面板 + 高亮。
- "带我走一遍软件"→ 新手引导（start_tour）。
- 手机布局的窗口（快照 `mobileLayout` 为 true）："打开调研""回到看盘""打开策略"切底部标签（`mobileTab`：ai AI 首页 / research / market / strategy 策略 / me，旧值 notify＝AI 首页），"打开提醒 / 看看通知 / 未读的提醒"打开提醒中心（`reminderCenter: {open: true, filter?}`，电脑是 K 线工具条铃铛的浮层，手机是整屏提醒页，快照 `reminderCenter`），"打开时间线"是 AI 首页的时间线（`mobileNotifyFilter: all`）；"打开报价列表""看多图""多周期同列""单图""副图换成 MACD""打开副图设置"是 `mobileMarket`（view / layout / subPane / subPanes）；"打开持仓""去下单""看复盘""订阅简报"打开整屏子页（`mobilePage: order|positions|review|briefs|install|push|lab|reminders|accounts|memory|appearance|help`；lab 是回测结果页，看指定一次用 `mobileLab: {labId}`（labId 来自 `GET /api/strategy-labs`），手机上线策略固定出卡确认；"教我装到桌面"打开 install 安装引导，快照 `installEnvironment` 是识别出的浏览器环境；"打开锁屏提醒"打开 push 锁屏提醒页（"提醒设置 / 通知设置"两边都是 `settingsPage: notify`），快照 `pushSupport` / `pushSubscribed`，电脑窗口是 `settingsPage: notify`，已开设备列表 `disclosure: push-devices`；null 关闭），"只看成交"用 `mobileNotifyFilter`，"只看螺纹的简报"用 `mobileNotifyProduct: "SHFE.rb"`（须是订阅品种或时间线里出现的品种）；电脑窗口说"订阅简报 / 简报品种"打开设置 → 提醒 AI 简讯行的订阅品种（`settingsPage: notify` 再 `disclosure: brief-subscriptions`）；看某合约先 `symbol` 再 `mobileTab: market`；"新建策略 / 改这条策略"用 `strategyEditor`（手机上是整屏页，四段与“策略流程”块是 `strategy-editor-section`〔id 含 flow〕，条件面板是 `strategy-editor-condition`，条件一侧的指标选择器是 `strategy-editor-picker`〔id 如 entryLong:1:left〕；“看止损那块”用 `strategyEditor: {focus: "stop"}` 定位到流程图的一块；“?”说明是 `help-tip`〔id＝说明 key〕）。侧边面板与视图在手机上没有。

## 何时不用

- 在图上画标注 → 图上标注（chart-annotations）。
- 改音色/默认手数等个人偏好 → 偏好设置（preferences）。
- 查行情数据本身 → 行情与合约查询（market-lookup）。

## 步骤

1. 固定表能识别的界面话直接出 UI 命令（app_call POST /api/app/ui/commands，等 applied 回执）。
2. "X 在哪"先开面板再 `highlight_ui` 高亮目标。
3. 分不清用户指哪里时读屏（窗口观察快照）再决定。
4. 只据实际回执确认生效步骤，不凭模型文字称整句完成；无回执给固定澄清，拒绝/未决不重复提交。

## 安全边界

- 界面命令只作用于用户自己的窗口（多设备不串扰）。
- 不改认证、凭据、执行与更新机制；扩展配置走版本号检查与回滚。
- 语音/打字固定表命中的界面命令不过模型，直接执行；其余纯界面语音等定稿后整句共用本 Owner 与正式命令定义，不另作动作映射。

命令词汇表（panel/view/theme/字号/周期/对话框/披露卡全集）见 [references/commands.md](references/commands.md)。

## 典型说法

- 打开设置
- 切到复盘
- 只看自选
- 看快讯
- 打开调研
- 字大一点
- 让 AI 们讨论一下这条

## 反例（不该触发）

- 把止损位画到图上（画标注，走 chart-annotations）
- 默认手数改成两手（偏好，走 preferences）
- 螺纹现在多少钱（数据查询，走 market-lookup）
