---
name: strategy-management
description: 创建、查看、评估、验证与管理个人交易策略草稿（新建或修改策略都在策略面板的策略编辑卡里，按成交习惯预填、一句话补改字段），并启动/暂停/停止策略监控、设置执行方式（只提醒/出卡确认/自动下单）。当用户说"帮我设计一个策略""根据我的交易生成策略""止损改成 20 跳""评估一下这个策略""跑一下验证""开始监控这个策略""改成出卡确认""暂停监控""停掉这个策略"时使用。
metadata:
  ai-trader-label: 策略管理与监控
  ai-trader-parent: ""
  ai-trader-summary: "创建、保存、上线和监控策略"
  ai-trader-category: 策略
  ai-trader-tools: create_strategy_draft list_strategy_templates create_strategy_from_template get_strategy_draft list_strategy_drafts evaluate_strategy_draft prepare_strategy_validation run_strategy_validation preview_strategy_intent get_strategy_monitor_status start_strategy_monitoring pause_strategy_monitoring
  ai-trader-routes: POST /api/strategies/{strategy_id}/monitor/start POST /api/strategies/{strategy_id}/monitor/pause POST /api/strategies/{strategy_id}/monitor/stop GET /api/strategy-plans/prefill GET /api/strategies/{strategy_id}/monitor GET /api/strategies/funds-preview GET /api/playbooks POST /api/playbooks PUT /api/playbooks/{playbook_id} DELETE /api/playbooks/{playbook_id}
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: strategy
  ai-trader-quick-action: ""
  ai-trader-default-tiers: basic pro test
  ai-trader-mount-words: 策略
---

# 策略管理与监控（strategy-management）

策略全生命周期：草稿创建与保存、评估、验证（模拟跑）、以及监控的启动、暂停与停止。监控中的策略按实例的执行方式处理信号：只提醒（对话里提醒一句）、出卡确认（出一张确认卡，用户确认后才下单）、自动下单（AI 自主执行开着时直接下模拟单，关着时按出卡确认处理）。开平仓判断的持仓口径（2026-10-08）：出卡确认与自动下单只数策略自己的成交（它挂的策略保护覆盖的手数，按账户封顶），同一合约上用户或 AI 手动开的仓不数、不平、也不挡策略开仓；只提醒仍按账户该合约持仓判断（手动开仓后还能收到平仓提醒）；价差策略按两腿账户持仓判断，同一价差不要再手动交易。

按资金比例定手数与回撤停机（2026-10-09）：sizing 选 equity_percent 的策略，手数由实时预算定（权益×比例的保证金预算÷每手保证金，扣本策略持仓与开仓挂单，受可用资金与 maxLots 约束，发送前再复核）；设了 maxDrawdownPercent 的策略，本轮净值从高点回撤超限即停机（撤它的开仓挂单、平它的持仓，转 paused，跨日/重启不自启）。`GET /api/strategies/{id}/monitor` 带 `funds`/`drawdown` 两块读数；`monitor/start {resetDrawdown: true}` 在平仓/撤单确认完成后重置回撤、以新基准恢复（liquidating 中 409）；`GET /api/strategies/funds-preview?symbol=&equityPercent=&maxLots=&strategyId?` 给编辑器实时预算（只读）。价差策略两者都不支持。

## 何时用

- "把我的开盘突破写成一条交易笔记""我的交易笔记有哪些""删掉那条笔记"（交易笔记，2026-10-25 叫打法本，2026-10-31 搬到复盘页：`strategyTab: playbooks` 打开复盘页；增删改走 `/api/playbooks`，字段 name / entryRules / exitRules / stopRules / strategyId；规则用用户自己的话，不替用户编）。
- "帮我设计一个策略""新建策略""根据我的交易生成策略"（打开策略编辑卡）、"止损改成 20 跳"（编辑卡开着时的补一句）、"把这个想法存成策略""我有哪些策略"。
- "放量突破开盘区间高点就提醒我""5 分钟 KD 金叉提醒一下"（条件提醒，2026-10-25 第 6 批）：打开编辑卡的提醒模式 `strategyEditor: {new: true, reminder: true, ask: 用户原话}`，只有条件和提醒时段，开始提醒由用户点；它存成 `plan.reminderOnly` 的策略，只能“只提醒”（confirm / auto 回 409 strategy_reminder_only），列在提醒面板“我的提醒”，不在我的策略。只问到价的（“到 3200 提醒我”）仍用到价提醒。
- "评估一下这个策略回测表现""验证跑得怎么样"。
- "开始监控这个策略""改成只提醒/出卡确认/自动下单""暂停监控""停掉这个策略"。

## 何时不用

- 历史回测 → 策略验证与回测（strategy-backtests）。
- "按我的策略现在该怎么做"（读当前信号）→ 策略信号解读（strategy-signals）。
- 监控中策略自动出的单 → 走卡片确认流程（order-proposal）。

## 步骤

0. 策略编辑卡（2026-10-08，替换了语音/对话引导设计和我的策略的输入框）：新建或修改策略都打开同一张卡（电脑在策略面板里，编辑时面板约占窗口 3/4：左列表单、右列策略流程图与回测；手机是整屏页，四段与“策略流程”块 `disclosure: strategy-editor-section`（id 多了 flow）、条件面板 `strategy-editor-condition`；手机我的策略每条还有“回测”和“上线…”，上线固定出卡确认）——
   `strategyEditor {new: true}` 新建、`{edit: "<strategyId>"}` 修改、`{start: "habits" | "blank" | "template:<id>"}` 换起点、
   `{ask: "一句话"}` 补一句（表单空时生成整份计划，已有内容时只改相关字段，改到的字段高亮、不直接保存）、`{answers}` 回答追问、
   `{focus: "stop"}` 等把表单定位到流程图的一块（同点方块）、`{save: true}` 存为草稿、`{close: true}` 关闭；快照 `state.strategyEditor`
   （`flow` 是流程图：触发、开多 / 开空条件组、开仓、各平仓方式与回测里的触发次数，次数只取当前表单那次预览回测）。
   条件的左边 / 右边用指标选择器挑（`disclosure: strategy-editor-picker`，id 如 `entryLong:1:left`），覆盖规则引擎全部操作数
   （56 个行情指标 + 数值，参数范围与引擎一致，可加“N 根内发生 / 连续 N 根 / N 根内至少 M 根”修饰，引擎不收的组合界面禁选）；
   ③ 的“更多”可改保本、分批止盈、按时间止盈、最长持仓、按条件平仓；④ 的开仓手数是三选一（固定手数 / 按资金比例 / 按波动），
   回撤停机在夜盘开新仓后面，④ 的“更多”（`strategy-editor-sizing-more`，标题“浮盈加仓、冷却、每个方向次数、亏损上限”）可改
   浮盈加仓、平仓后冷却、每个方向每天次数、当日亏损上限。“按我的习惯”只用 `GET /api/strategy-plans/prefill`
   从本账户近 20 个交易日的已平仓成交确定性地算（合约、方向、止损跳数、收盘前平仓、开仓时段、每次手数，各带依据与成交 id；
   不到 5 笔不预填）；开仓条件不预填，要用户自己选——“从交易习惯归纳开仓规则”是下一期，如实说。改动后卡片约 1.5 秒自动回测
   最近 60 个交易日（`POST /api/strategy-backtests` 的 preview，只算不存，每条开仓条件带近 60 天满足次数）。上线运行仍由用户在卡里点
   （打开上线对话框）。卡片表达不了的规则（条件组合、先后顺序、盘中触价、回调结构、价差；方向确认、复位、单笔预算、追踪 ATR 周期可在卡里改）
   在“高级规则（卡片里不能改）”只读列出，没动过的字段和条件逐字节保留，存为新版本时写回。
   用户说“帮我设计一个策略 / 新建策略 / 根据我的交易生成策略”时 Runtime 直接用界面命令打开卡并回一句，不需要你再做；
   卡开着时说“止损改成 20 跳”这类话 Runtime 送进补一句。
1. 草稿：`create_strategy_draft`（`plan` 填完整 StrategyPlan，不降级成模板参数；与策略编辑卡同一个保存接口 `POST /api/strategy-plans`，存进“我的策略”，能看、能改、能删），返回 strategyId 后回读。个人策略（StrategyPlan，策略面板设计与保存）可用的积木（EMA/ATR/RSI、唐奇安、布林、肯特纳、超级趋势、昨高昨低今开等日线锚点、开盘区间、“锚点+系数×ATR/振幅”；第二批的 CCI、ADX/±DI、抛物线 SAR、当日最高/最低、枢轴 P/R1–R3/S1–S3（R-Breaker 六价位）、持仓量变化与四象限、“连续 N 根”“较 k 根前上升/下降”、开仓组的复位条件 rearm，以及 ATR 止损、追踪止损、保本 breakeven、分段止盈 timeTakeProfit、收盘前平仓、最长持仓、按 ATR 定手数、开仓时段、每方向次数）按 schema 写，用户没提的不要自己加（当日亏损上限 maxLoss 也一样：用户没说就是 null，不设）；距涨跌停过滤暂不支持，如实说明。
   从模板开始（策略模板库，22 个经典策略：双均线、唐奇安、布林通道、SuperTrend、挤压释放、肯特纳通道、连涨连跌、恒温器、多周期共振、ATR-RSI、Aberration、RSI 迟滞反转、威廉 %R 确认反转、枢轴反转、Z-Score 均值回归、Dual Thrust、菲阿里四价、NR7 窄幅突破、空中花园、R-Breaker、海龟交易法（含加仓）、跨期价差回归（布林））：`list_strategy_templates` 看分类、场景、参数范围；
   先用 `preview_strategy_template`（策略验证与回测技能）在用户的合约上回测给数字；用户同意后 `create_strategy_from_template`（可带 params / name / symbol；`chartSymbol` 给用户正在看的合约，默认名为“模板名 · 合约 · 周期”，重名自动加“(2)”）存成普通草稿，返回 strategyId。
   模板只是起点：存下来的策略与自己设计的完全相同，照样走验证、上线与保护；参数越界时如实转告工具给的中文原因。
2. 查看/评估：`list_strategy_drafts` / `get_strategy_draft` / `evaluate_strategy_draft`。
3. 验证：`prepare_strategy_validation` → `run_strategy_validation`（模拟盘跑一段，看成交与净值）。
4. 监控：`get_strategy_monitor_status` 查看，`start_strategy_monitoring`（可带 `signalAction`；监控中再调用＝改执行方式，提醒和确认卡发到当前对话）/ `pause_strategy_monitoring` 启停；停止实例（回到未上线、默认不平已有持仓）走 `POST /api/strategies/{strategy_id}/monitor/stop`；用户说“停掉并平仓”时带 `{closePositions: true}`：停止后这个策略自己还有持仓（按它挂的策略保护覆盖的手数，价差按两腿）就在它的对话里按平今/平昨出平仓卡，返回 `closeCards`，由用户确认才平——从不直接下单，自动下单的实例也一样。监控回读另带 `position`（策略自己的持仓）、`signalsToday`（今天的信号数）与 `researchClash`（品种今日判断偏多/偏空而策略方向相反时的一句提示，震荡或没有判断为 null）。用户没说执行方式时保持实例原设置；用户要自动下单但 AI 自主执行关着时如实说明，建议出卡确认。
5. 用户在结果页或手机策略页的“跟 AI 说……”里直接说怎么改（本轮 focus 的 kind 是 `strategy_edit`）：在 `get_strategy_draft` 读到的 `definition.plan` 上按他的话改，用 `create_strategy_draft`（`plan` = 改后的完整定义，`baseStrategyId` = 该策略，运行实例的 id 也行）另存新版本，再用 `run_strategy_backtest` 对新版本回测、对比改前改后；不上线、不改执行方式。
6. 调参（“建议调整参数”）：先用文字列出 旧值→新值 与理由，不改正在运行的版本；策略验证与回测技能可用时，用 `run_strategy_backtest` 对原策略（`strategyId`）和改后版本（`plan`）各回测一次，用“改前 → 改后”对比净收益、回撤、胜率与次数给出依据；用户同意后用 `create_strategy_draft` 带改后的 `plan` 与 `baseStrategyId` 另存为新版本，上线由用户决定。
7. 删除：`DELETE /api/strategy-plans/{id}`（在监控的先暂停）。2026-10-20 前对话里存的旧格式草稿（`list_strategy_drafts` 里定义没有 `plan` 的）不在“我的策略”里，用户要清掉时也用这个接口删。

## 安全边界

- 策略级预算只能来自用户写进策略定义里的数值；不创建平台级/账户级风险规则。
- 监控只对模拟账户执行；自动下单只在用户选了且 AI 自主执行开着时发生，其余信号只提醒或出卡。
- 出卡确认的卡是普通方案卡，只能用户在卡上确认；没确认的卡不计入每日开仓次数。
- 停止默认不平仓；“停止并平仓”也只出平仓卡等用户确认，不直接下单。平仓卡（以及 confirm 的信号卡与价差卡）按实例自己的交易账户出——对话已切到别的账户也一样，卡上 `accountOverride` 注明、界面在卡标题下写“下到 X 账户”，确认只在实例账户下单（#1011）；出不了卡时 `notice` 是一句中文（如“没找到这个策略在 沪银2612（SHFE.ag2612）的持仓，请到持仓里手动平”），照着告诉用户即可。
- 用户说"暂停/停止"时立即执行，不等新闻或行情确认。
- 未开通时接口 403、工具不出现，提示"当前档位未开通「策略管理与监控」"。

监控状态字段与决策流的细节见 [references/monitoring.md](references/monitoring.md)。

## 规则（2026-10-14 从系统提示移入，说到策略时随本技能挂上）

- 个人策略用完整 StrategyPlan（app_call 读接口 schema 后保存或修改），保留用户给的参数，instrumentScope 默认 any，运行前不强求具体合约；修改存为新版本并回读，不覆盖正在运行的策略。
- 已保存的策略以 `list_strategy_drafts` / `get_strategy_draft` 为准，绝不从聊天记录重建。
- 用户要启动：先 `POST /api/strategy-plans/{identifier}/validate`（带所选合约与 chartTimeframeSeconds），用它返回的执行 strategyId 去 monitor/start；这是规则与数据就绪校验，不是盈利回测，不编造通过结果、不跳过失败项。
- 当前开平条件读执行副本的 conditions 接口；策略库里的条目不是持仓也不是在运行的策略。
- 问“验证就绪的策略现在会怎么做”：`preview_strategy_intent`，讲当前信号、它想要的持仓变化和风控后的最终手数——一次性只读快照，不启动监控、不建执行租约、不出卡、不下单；风控允许只是计算结果，不能说成已下单。
- 用户要启动已通过运行前校验的策略：直接 `start_strategy_monitoring`，不另要授权（监控与由此产生的执行都是模拟）；要暂停或停止用 `pause_strategy_monitoring`；执行历史与保护状态读 `get_strategy_monitor_status`。监控每根已收盘信号 K 线至多动作一次，挡住重复或重叠委托，遵守策略的持仓、每日次数与冷却设置，保留上期所/能源中心今昨仓，触发账户保护时暂停。
- 没有成功返回非空 strategyId 和状态，不说策略已建好。

## 典型说法

- 帮我设计一个策略
- 根据我的交易生成策略
- 帮我写一个均线策略
- 评估一下我这个策略
- 开始监控这个策略
- 暂停策略监控
- 这个策略改成出卡确认
- 停掉这个策略

## 反例（不该触发）

- 帮我回测这个策略近两年（历史回测，走 strategy-backtests）
- 按我的策略现在该怎么做（当前信号解读，走 strategy-signals）
