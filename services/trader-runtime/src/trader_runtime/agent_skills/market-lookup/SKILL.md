---
name: market-lookup
description: 查询期货实时行情、涨跌、买卖盘、K线形态解读、合约搜索与结算价/主力合约历史/成交量排名等只读数据。当用户问"现在多少钱""涨还是跌""螺纹主力是哪个合约""帮我找个手续费低的品种""结算价多少""今天哪些品种涨得最多""一手多少钱""跳一下多少钱""这个合约什么时候要平仓"时使用。
metadata:
  ai-trader-label: 行情与合约查询
  ai-trader-parent: ""
  ai-trader-summary: "查行情、涨跌、持仓量、报价总表和合约信息"
  ai-trader-category: 行情
  ai-trader-tools: get_market_snapshot get_market_analysis get_futures_decision_context get_market_board search_instruments get_runtime_data_status get_settlement_prices get_main_contract_history get_symbol_ranking
  ai-trader-routes: ""
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: ""
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
  ai-trader-mount-words: 报价表 总表 结算价 排名 排行 涨幅榜 跌幅榜 主力换月 席位
---

# 行情与合约查询（market-lookup）

只读行情与合约信息查询：价格、涨跌、买卖盘、量仓、K线走势概览、全品种报价总表、合约检索，以及结算价、主力合约变迁、成交量/持仓量排名三类 TqSdk 数据（数据源可用时）。

## 何时用

- 用户问某个品种/合约的现价、涨跌幅、买卖一价、成交量："螺纹现在多少钱""沪金涨了没"。
- 用户要全市场概览："今天哪些品种涨得最多""持仓量最大的几个""行情表看一下"。
- 用户想找合约："螺纹主力是哪个""帮我搜下 au""有没有手续费便宜的活跃品种"。
- 用户要行情概览或走势判断素材："看下今天铁矿的走势""量大不大"。
- 用户问合约本身："一手螺纹多少钱""跳一下多少钱""保证金手续费多少""这个合约什么时候要平仓"。
- 用户要结算价、主连换月历史、品种活跃度排名。

## 何时不用

- 新闻、政策、消息面、涨跌原因 → 联网研究（web-research）。
- 让 AI 帮忙开仓/平仓/下单 → 下单出卡（order-proposal）等交易技能，本技能只读。
- 界面操作（"打开图表""切到 15 分钟线""打开报价总表"）→ 界面控制与读屏（ui-control）。
- 只是想看图上的支撑位/画标注 → 图上标注（chart-annotations）配合图表与指标（chart-analysis）。

## 步骤

1. 口语品种名先用 `search_instruments` 解析成合约代码；没说品种时先看用户当前看的合约。
2. 现价/涨跌/买卖盘用 `get_market_snapshot`；走势与多空概览用 `get_market_analysis`。合约本身的问题也用 `get_market_snapshot`：`contract` 带乘数/每跳/每手保证金手续费/交易时段，`deliveryNotice` 带个人账户交割月最后可持仓日、剩余交易日与提醒级别——直接用它的原文回答，不自己换算月份。`contract.commissionPerLot` 是开仓一手的手续费（元/手），与下单票“手续费约”、`estimate_order` 同一个估算；`commissionSource` 为 `market_source` 时说明是行情参考值、实际以成交为准。
3. 跨品种比较用 `get_market_board`（全部主力一张表，可按涨跌幅/成交量/持仓量/增仓排序，可按交易所过滤，limit 最多 30 行）。
4. 要完整决策背景（行情+持仓+账户+就绪检查）用 `get_futures_decision_context`。
5. 数据异常时用 `get_runtime_data_status` 核对数据源状态，如实说"数据过时/不可用"，不编数字。
6. 回答卡片化（2026-10-14）：行情快照、走势分析、报价总表（以及持仓、委托、快讯）读到就由 Runtime 自动成卡，同一轮同类合并成一张；有卡时正文只写一两句结论，不复述卡里的数字。分析、对比、“咋样 / 怎么看 / 波动大不大 / 哪个强”这类问题本轮会带 `answer_card`：读完数据后作为最后一步单独调一次出回答卡（调完本轮即结束，卡的结论就是正文；标题、一句结论、≤3 条理由、一行风险；单品种填 ≤6 个数字格，对比两个品种只填左右两栏、不填数字格）。卡上的读数一律写字段键 `{[合约.]字段[@周期][=取值]}`（如 `day`、`volumeState@1d=down`、`j2701.day`，字段与取值见 answer_card 的说明）：数字格只写键；结论、对比栏、理由、风险是嵌键的模板（如 `铁矿{i2701.day}，焦炭{j2701.day}且{j2701.volumeState=down}，铁矿更抗跌`；卡上有两个合约时句子里的键都写合约，完整示例见 answer_card 的说明），值、单位和方向由 Runtime 按本轮读数填。本轮这个合约读过两种周期时要写 `@5m` / `@1d`；对比栏每栏写 `symbol`；键之外不写数字，结论、对比栏和理由里也不写放量、下跌这类读数词（写成带取值的定性键；风险行可以写），标题不写数字（写了由 Runtime 改用品种名）；`day` 与 `dayMove` 是同一个当日涨跌，只写一个。键查不到、周期有歧义或取值与读数不符的格、行和理由会被去掉并告诉你原因，结论这样就退回重写。理由只写原因，格子和对比栏已显示的键在理由里不再显示。本轮已读到的行情、走势不要重读（同样参数再读只会得到“本轮已读过”）；用户点名的品种系统已附上合约时直接用，不用 search_instruments。
7. 结果里 `dataStatus` 为 `backup`（`source` 为“新浪（备用）”、带 warning）时，快期行情暂不可用，数字来自新浪备用行情：回答里先说明“这是备用行情，可能延迟几秒”，只用来看盘和分析，不据此出下单卡、止损或提醒。

## 安全边界

- 只读：本技能任何工具都不下单、不改状态。
- 报价有时效：过期行情要说明数据时间，不把旧价当现价；总表超过上限截断时说明只列了前一部分。
- 不预测保证收益；走势描述区分事实与判断。

## 典型说法

- 螺纹钢现在什么价
- 帮我看看沪金今天涨了还是跌了
- 今天哪些品种涨得最多
- 持仓量最大的几个合约是哪些
- 铁矿今天成交量怎么样
- 一手螺纹多少钱，跳一下是多少
- 这个合约什么时候要平仓

## 反例（不该触发）

- 打开图表，切到 15 分钟线（界面命令，走 ui-control）
- 为什么今天原油大跌（消息面归因，走 web-research）
- 帮我开多一手螺纹（下单，走 order-proposal）
