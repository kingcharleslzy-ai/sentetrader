---
name: routines
description: 我的流程：用户把一串固定做法存成一个有名字的流程（如「老规矩」），以后说名字就按顺序做完；要下单的步骤只出卡等用户确认。当用户说"以后我说老规矩就帮我……""把这几步存成流程""老规矩去掉回测那步""我有哪些流程""删掉收盘检查"时使用。
metadata:
  ai-trader-label: 我的流程
  ai-trader-parent: ""
  ai-trader-summary: "一句话做完一串事，下单仍要你确认"
  ai-trader-category: 系统
  ai-trader-tools: propose_routine list_routines run_routine
  ai-trader-routes: POST /api/routines/drafts POST /api/routines PATCH /api/routines/{routine_id} DELETE /api/routines/{routine_id} POST /api/routines/{routine_id}/run
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: settings:memory
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
  ai-trader-mount-words: 流程
---

# 我的流程（routines）

用户说一遍步骤并起个名字，存成"我的流程"（设置 → AI 与记忆 → 我的流程；手机 我的 → AI 与记忆）。以后用户整句只说流程名（或"跑一下老规矩""运行老规矩"），或在设置里点"运行"，Runtime 就按步骤顺序做完：每一步只给这一步类型的工具，进度卡逐条打勾；要下单的步骤只出卡，用户点确认才下单——自主执行开着也一样。每人最多 5 个流程，每个最多 10 步，名称 1–12 字。

## 何时用

- "以后我说「老规矩」，就帮我：看自选的今日判断……设到价提醒；开始盯异动" → `propose_routine` 把话拆成步骤出一张流程卡。
- "老规矩去掉回测那步""收盘检查加一步设夜盘提醒" → 先 `list_routines` 拿到原流程，再 `propose_routine`（带 routineId，写改完后的完整步骤）出新的流程卡。
- "我有哪些流程""老规矩是哪几步" → `list_routines`。
- "把老规矩跑一遍"这类没被 Runtime 直接接住的说法 → `run_routine`，quote 写用户这句原话（必须含流程名）。它按流程 id 运行（设置里点"运行"也是），不会跑成名字相近的另一个流程；排队时流程被删，回答"这个流程已经不在了"，不会改跑别的。用户整句就是流程名或"跑一下 X / 运行 X"时 Runtime 已经直接运行，不会到你这里。
- "删掉收盘检查" → `list_routines` 找到 id，再 `app_call` `DELETE /api/routines/{routine_id}`。

## 何时不用

- 只是问"老规矩是什么意思"→ 先 `list_routines` 照实说步骤，不要运行。
- 一次性的要求（"现在帮我看一下螺纹再出张卡"）→ 直接做，不要存成流程。
- 用户要 AI 以后注意的习惯（"给建议先说止损位"）→ 记忆约定（preferences 的 remember），不是流程。

## 步骤类型（kind）

read 读（今日判断、调研、简报、持仓账户）、draw 画线、backtest 回测、margin 算保证金与可开手数、card 出卡（开仓 / 平仓卡，只出卡）、alert 到价提醒、watch 盯异动（加进 AI 简报订阅）、review 复盘。每步 text 一句话写做什么（≤60 字），按用户说的顺序排，尽量保留用户原话。

提醒、画线、盯异动步骤按原话分两种：只说“看看要不要 / 有没有必要”的只给建议（卡上标“只给建议”，运行时没有写入工具）；原话还带“需要就设 / 有必要就设 / 该设就设 / 合适就设”的是需要时执行（标“需要时执行”，判断后可以设）。不要自己给步骤追加“需要就设”这类承诺，回复里也按卡上的标注说。

## 安全边界

- 你只能出流程卡；保存、改名、改步骤只有用户在界面上点（`POST /api/routines`、`PATCH` 对 AI 一律 403）。不要说"已经存好了"，要说"点保存就存"。
- 流程里不能放免确认下单、自动下单、绕过确认、改风险设置、改自主执行开关。用户这么要求时照实把话放进步骤，流程卡会被拒（没有保存按钮），向用户转述拒绝原因，不要替用户改写成能保存的样子。
- 流程运行期间没有任何直接下单、撤单、止损、自主执行或改设置的工具；出卡步骤的卡要用户确认。
- 不要自己启动流程：只在用户明确要求时 `run_routine`，quote 必须是用户的原话。
- 某一步失败，Runtime 停在那一步，后面不做；已出的卡照常可确认。提醒、画线、盯异动、出卡这几步看它要做的那次写入：先读成功、写入却没成功（例如订阅超出档位 422），这一步就算失败；改参数后重试成功仍算完成。

## 典型说法

- 以后我说「老规矩」，就帮我看自选的今日判断、画支撑位、核保证金、出开仓卡带止损止盈、设到价提醒
- 把这几步存成「收盘检查」：看持仓、问要不要过夜、设夜盘提醒
- 老规矩去掉回测那步
- 我有哪些流程

## 反例（不该触发）

- 老规矩是什么意思（问含义：读出步骤，不运行）
- 老规矩里直接下单不用问我（会被拒绝并说明原因）
