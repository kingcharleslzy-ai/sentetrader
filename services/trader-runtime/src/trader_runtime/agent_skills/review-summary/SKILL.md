---
name: review-summary
description: 复盘与日结：读取持仓明细与浮动盈亏、账户资金权益、当日成交流水与决策记录，读交易日志（每笔的来源、理由、用户补充、开仓时的调研判断）并把用户说的想法记成补充，做总结。当用户说"我手里有什么持仓""今天赚了还是亏了""今天成交了几笔""帮我复盘一下今天""我这手做得合理吗""最近亏在哪"时使用。
metadata:
  ai-trader-label: 复盘与日结
  ai-trader-parent: ""
  ai-trader-summary: "复盘成交、日结和引导复盘"
  ai-trader-category: 复盘
  ai-trader-tools: get_positions get_account_summary get_account_activity get_trade_journal get_review_picks set_trade_note get_daily_review regenerate_daily_review answer_review_observation
  ai-trader-routes: GET /api/account/journal GET /api/account/habits PUT /api/account/trade-notes/{round_id} DELETE /api/account/trade-notes/{round_id} PUT /api/account/trade-playbooks/{round_id} DELETE /api/account/trade-playbooks/{round_id} GET /api/account/daily-review POST /api/account/daily-review/regenerate POST /api/account/daily-review/observation
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: positions
  ai-trader-quick-action: prompt:帮我复盘一下今天
  ai-trader-default-tiers: ""
  ai-trader-mount-words: 复盘 交易日志 日志 笔记 备注 做得怎么样
---

# 复盘与日结（review-summary）

账户与持仓的只读复盘：持仓与浮盈、权益与保证金、当日成交与决策流水，以及"今天怎么样"的日结总结。

## 何时用

- "这笔算开盘突破""给这几笔补标交易笔记""我哪个时段最亏""周五是不是做得最差"（交易笔记，2026-10-25 叫打法本：补标走 `PUT /api/account/trade-playbooks/{roundId}`，笔记 id 从 `GET /api/playbooks` 读；分组读 `GET /api/account/pnl` 的 `breakdown`，数字照原样说，不自己另算）。
- "我手里有什么""持仓亏了多少"。
- "账户还有多少钱""保证金占了多少"。
- "今天成交了几笔""帮我复盘一下今天"。
- "我这手做得合理吗""最近亏在哪""我是不是老追涨"（读交易日志）。

## 何时不用

- 查某张挂单的状态 → 撤单/追单/改价（order-ops）。
- 问行情价格 → 行情与合约查询（market-lookup）。
- 要平仓/调仓 → 交易技能出卡。
- 历史某天的重演 → 行情回放（market-replay）。

## 步骤

1. 持仓问题用 `get_positions`（权益/保证金相关再补 `get_account_summary`）。“今天平仓盈亏 / 手续费 / 今天赚了多少 / 浮动多少”直接报 `get_account_summary` 返回的 `pnlFields`（closeProfit 平仓盈亏、positionProfit 持仓盈亏、floatProfit 浮动、commission 手续费、todayNetPnl 今日净盈亏）并说口径：平仓盈亏不含手续费，要扣费就给“平仓盈亏 − 手续费”两个数；不要把成交或交易日志逐笔相加，也不要用“今日 − 浮动”推算。
2. 当日流水与决策用 `get_account_activity`；它的 `protections` 是保护单当前状态，用户提到某个止损/保护价先在这里核对，`effective` 为 false 的直接说已经不在了（附 statusLabel），不要当有效止损讨论或建议改价。
3. 已平仓的交易用 `get_trade_journal`：每笔有来源（手动 / AI 建议 / AI 自主 / 策略 / 止损）、理由、用户补充 `note`（`noteScopeChanged` 为 true 时补充写在更早的手数上，转述要说明）、开仓那天调研员的判断 `deskView`；响应里的 `unlinkedNotes` 是找不到对应交易的补充。
4. 复盘总结区分：事实（成交/盈亏/当时的判断）、原因（理由与补充）、你的评价（明确说是你的判断）、待办（挂单/未完成卡）。
   说盈亏用复盘页同一套叫法：“总盈亏”是账户权益 − 本金（含持仓浮动与手续费），“今日”是账户当日净盈亏，“本月平仓”与按月、按日只算已平仓的交易；两者一正一负时先说总盈亏再解释差在持仓浮动。夜盘成交记在下一个交易日。
5. 用户说出某笔当时的想法或教训时，用 `set_trade_note` 照原意记一句；不替他编理由，不把你的评价写进补充。一次聊一笔，别连珠炮追问。
6. 用户说"复盘一下 / 复盘这周"要你带着聊时，用 `get_review_picks`（day / week）：按它挑的顺序一次问一笔，用它给的 question；回答后记下再问下一笔，说"跳过"就下一笔，说"不聊了"就停；没有 picks 就说没有需要聊的。每笔最多追问一句。
7. 今日复盘（每个交易日 15:15 有成交才生成；外盘美元子账户在 CME 收盘后美东 17:15 生成，金额是美元，别说成元）：用户说“今天复盘 / 今天怎么样 / 复盘摘要”时先 `get_daily_review`，照它的总结、每笔标签（计划内 / 提醒后 / 临时，按成交来源和提醒时间判定，不是你判断的）和平仓盈亏讲，每笔的 checks（提前离场、连亏后再开、没标打法、亏过止损、没设止损、不合入场规则，程序查的）照原话说；带 question 的笔一次问他一笔，他答了用 `set_trade_note` 记到这笔的 roundId；收盘后又有成交或用户说“重新复盘今天”用 `regenerate_daily_review`。一周最后一个交易日的“本周最该改的一件事”（observation）只有用户明确说“记住这条”才用 `answer_review_observation`（verdict remember，quote 原样填他这句话；observationKey、revision 照 `get_daily_review` 返回的那条填，返回 observation_stale 就重读再问他），说“不对”用 dismiss；不要主动替他记。
8. 用户说之前写的补充找不到了：补录的成交被真实成交替换、又证明不了是同一笔时，补充不会跟过去，复盘页末尾“未关联的补充（N）”里能看到原文并删除；不要猜着把它重写到某一笔上，让用户自己在对应那笔重新写。

## 安全边界

- 不下单、不改交易状态；只写用户的补充、重新生成复盘，以及用户明确要求时记下复盘观察。
- 亏损复盘不以"软件有 bug"为默认解释；先看监控状态、挂单与实际决策。
- 数字必须来自工具返回，不凭记忆报数。

## 典型说法

- 我手里有什么持仓
- 今天赚了还是亏了
- 帮我复盘一下今天

## 反例（不该触发）

- 全平（交易动作，走 batch-close）
- 那张挂单成交了吗（查单，走 order-ops）
- 回放一下昨天的行情（历史重演，走 market-replay）
