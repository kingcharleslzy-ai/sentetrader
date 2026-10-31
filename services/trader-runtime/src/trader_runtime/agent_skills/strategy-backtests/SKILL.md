---
name: strategy-backtests
description: 用历史数据回测用户自己的规则策略（规则回测：与上线运行同一套判定，结果存库、带“AI 怎么看”的事实），比较改前 / 改后；试几组参数比一比 / 策略实验室对比多组参数（前 70% 挑、后 30% 单独检验）；另有可复现回测与示例趋势策略的 TqSdk 历史回测（界面上已不提供，只给 AI 与旧客户端）。当用户说"回测一下这个策略""近两个月表现怎么样""改完再跑一次对比""这几组参数哪个好"时使用。
metadata:
  ai-trader-label: 策略验证与回测
  ai-trader-parent: "strategy-management"
  ai-trader-summary: "对策略做规则回测和体检"
  ai-trader-category: 策略
  ai-trader-tools: run_strategy_backtest preview_strategy_template create_strategy_lab get_strategy_lab choose_strategy_lab_variant run_reproducible_backtest run_tqsdk_historical_backtest
  ai-trader-routes: POST /api/strategies/{strategy_id}/backtest POST /api/strategy-backtests POST /api/strategy-templates/{template_id}/preview POST /api/backtests POST /api/backtests/reproducible POST /api/backtests/tqsdk POST /api/strategy-labs POST /api/strategy-labs/{lab_id}/results POST /api/strategy-labs/{lab_id}/run-server POST /api/strategy-labs/{lab_id}/choose GET /api/strategy-backtests POST /api/strategy-backtests/{backtest_id}/verdict POST /api/strategy-plans/{strategy_id}/compare GET /api/strategy-plans/{strategy_id}/compare POST /api/strategy-backtests/inputs POST /api/strategy-backtests/browser-result
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: strategy:backtest
  ai-trader-quick-action: ""
  ai-trader-default-tiers: basic pro test
  ai-trader-mount-words: 回测 实验室 比一比 模板
---

# 策略验证与回测（strategy-backtests）

用户自己的策略用**规则回测**（`run_strategy_backtest`）：逐根K线调用与上线运行完全相同的规则求值与策略引擎。
`run_tqsdk_historical_backtest` 跑的是内置的“示例趋势策略”（只比较两个成交量阈值），不是用户的策略，
只在用户明确要看示例时用；界面上的“示例趋势策略回测”按钮已去掉（2026-10-08）。

界面口径（策略页改版第八批，2026-10-08）：界面只有一种回测——规则回测，默认最近 6 个月（`tradingDays: 120`）、滑点 0、扣手续费，
结果页大字写“一共赚了 / 最多一次亏到 / 100 次里赢几次”。每次结果都存库（每条策略最近 5 次，`GET /api/strategy-backtests?strategyId=`，重启不丢），
并带 `verdict.facts`：程序算出的事实句（最赚钱那个月占比、去掉它还剩多少、笔数太少、没扣手续费、参数是否在同一段行情里挑的）；
`POST /api/strategy-backtests/{backtestId}/verdict` 请模型把事实说成一两句，模型句里的数字必须都出自事实，否则只回事实句（`source: facts`）。
转述回测时优先用这些事实，不要自己另算“占比”之类的数字。

在哪算（本机优先，2026-10-12）：用户在界面上点的回测（编辑卡自动回测、结果页回测、比一比）默认在用户本机浏览器里算
（Pyodide 跑服务端同一份引擎，`POST /api/strategy-backtests/inputs` 取输入和 ticketId、`POST /api/strategy-backtests/browser-result` 带 ticketId 交回存档，重复交回同一票据只返回第一次的结果），
算不了自动转服务端。结果的 `source`（browser / server）、`fallbackReason`（为何转服务端）与 `browserCheck`（服务端按同一份输入复核本机结果是否一致）
只供排障，不用向用户转述。你（AI）调用的回测工具与接口一律在服务端算，不要调用 inputs / browser-result 这一对；
本机结果不作上线依据：存为策略后那次回测由服务端重跑。

## 何时用

- "回测一下这个策略""这条策略最近两个月表现怎么样"。
- 修改策略时：改前跑一次、改后跑一次，用“改前 → 改后”对比收益、回撤、胜率、次数（策略编辑卡自己每次改动后用 preview 自动重算并和上次对比）。
- "这几组参数哪个好""把止损和均线各试几档比一比"：策略实验室，一次并行对比多组变体。
- 用户要绩效数字（净收益、最大回撤、胜率、盈亏比、出场原因）来决定要不要上线。

## 何时不用

- 模拟盘实时跑 → 策略管理与监控（strategy-management）的上线运行。
- "按策略现在该怎么做" → 策略信号解读（strategy-signals）。
- 看行情/看图 → 行情类技能。

## 步骤

1. 确定策略与合约：已保存的传 `strategyId`（策略库版本没有绑定合约时必须给 `symbol`）；未保存的改后版本传完整 `plan` + `symbol`。
   跟随图表周期的策略给 `timeframe`。区间省略时按周期取可取到的最近历史（1 分钟线约 28 个交易日，5 分钟约 145 天，日线数年），
   或给 `tradingDays`；滑点默认 0 跳，可设 0–5 跳。
2. `run_strategy_backtest`：结果卡自动出现在对话里（注明“规则回测”），工具返回摘要、出场原因分布、覆盖区间与说明。
3. 汇报：净收益（已扣手续费）/ 最大回撤 / 胜率 / 盈亏比 / 交易次数，最主要的出场原因，实际覆盖了多少个交易日；
   对比时逐项写“改前 → 改后”。数据不足等 `notes` 与手续费口径 `feeEstimate.text` 如实转述；`feeEstimate.source` 为 none 时明确说“未扣手续费，结果偏乐观”。
   策略体检：一笔没成交或开仓条件一次都没同时满足时，摘要带 `health = {headline, advice}`——先原样说 `headline`（卡在哪一条），
   再列建议（去掉那一条 / 把交叉放宽到最近 3 根内，各带改后大约开几次；连续几根都满足只算一次）；建议只按开仓次数给，不要按盈亏另挑参数。
   已存的策略由用户在详情点“按这个改”（或界面命令 `strategyBacktest: {applyAdvice: "<建议 id>"}`，接口 `POST /api/strategy-plans/{id}/apply-advice`）
   存为新版本并自动重跑回测；未存的改后版本用改后的 `plan` 再回测一次对比，存为新草稿仍按策略管理技能由用户决定。
4. 模板回测：`preview_strategy_template {templateId, symbol, params?, timeframe?}` 按参数实例化模板（不保存）并规则回测，
   结果卡同样发进对话；换参数就再跑一次对比。参数越界返回中文原因，照实转告。存成策略走策略管理的 `create_strategy_from_template`。
5. 策略实验室（2026-09-30，对比多组）：`create_strategy_lab {symbol, variants: 1–12 组, tradingDays?, slippageTicks?, uiWindowId?}`
   建一次对比，在服务端算（`uiWindowId` 是旧字段，接受但不再使用）。`get_strategy_lab {labId, waitSeconds?}`
   等结果（默认 60 秒，每 2 秒查一次）：历史按时间切前 70%（样本内）/后 30%（样本外），跨过切点的那笔平仓两段都不计；
   每组带样本内 / 样本外的笔数、胜率、净利、回撤与衰减，标记 best 样本内最佳 /
   overfit 过拟合风险 / negative_oos 样本外亏损 / too_few 样本太少。汇报以样本外为准，别只报样本内第一名。
   结果带整次比较共用的 `feeEstimate`（同规则回测）：`source` 为 none 时明确说“未扣手续费，结果偏乐观”。
   用户选好后 `choose_strategy_lab_variant {labId, variantId, name?}` 存为“我的策略”（服务端先按规则回测同口径重跑权威回测），
   返回 strategyId；上线仍由用户决定。两组以内的对比仍直接用 `run_strategy_backtest` 跑两次更省事。

6. 试几组参数比一比（结果页，2026-10-08）：`POST /api/strategy-plans/{strategyId}/compare {symbol, timeframe?}` 对这条策略最关键的一个参数
   （模板策略取模板的 keyParam，同值同范围的对称参数一起变；其他策略取止损距离）跑当前值和上下各一组，最近 6 个月；不带 `runOn` 时在服务端算（界面传 `runOn: "browser"` 由用户本机算，算不了再 `run-server`）；
   `GET` 同一路径读最近一次（`rows` 每组赚 / 最多亏 / 胜率，`current` 标当前那组，`line` 是“后 X 个月单独检验：……”）。界面命令 `strategyLab: {run: true}`。

## 口径

- 按收盘确认、下一根开盘成交；规则 / 收盘前 / 最长持仓 / 分段止盈出场也在下一根开盘成交。
- 止损、止盈、追踪止损、保本按K线高低价判断触及，按止损 / 止盈价成交；跳空越过按开盘价；同一根同时碰到止损和止盈按止损。
- 冷却、每日开仓次数、当日亏损上限、单笔预算、“每段方向只开一次”与复位条件与实盘相同。
- 手续费：“设置 → 交易”自定义费率时按自定义；否则按交易所标准估算（openctp 每日汇总，已还原期货公司加收；开仓 / 平今 / 平昨分别计，按金额和按手的费率都算）；价差回测两腿各查自己合约的费率、各按各的计，任一腿拿不到就不扣，并标“未扣手续费，结果偏乐观”、写明缺哪个合约。
- 只按K线回放，不含盘中排队与部分成交。
- 并发：默认全局同时 2 个、每人 1 个回测（部署可调，当前值读 `GET /api/strategy-backtests/queue` 的 limits），其余先到先跑地排队；排队超过等待时限（默认 60 秒）返回“回测排队中，稍后再试”，照实转告并建议稍后再跑，不要连续重试。

## 安全边界

- 回测只读取历史行情计算，不下单、不部署、不写数据库；结论里明确“历史表现不代表未来”。
- 未开通时接口 403、工具不出现。

## 典型说法

- 回测一下这个策略
- 这条策略最近两个月表现怎么样
- 止损改成两倍 ATR 再回测一次，跟原来比比
- 快慢均线各试几档，看哪组搭配好（实验室）

## 反例（不该触发）

- 按我的策略现在该怎么做（当前信号，走 strategy-signals）
- 开始监控这个策略（实时监控，走 strategy-management）
- 帮我写个策略（创建草稿，走 strategy-management）
