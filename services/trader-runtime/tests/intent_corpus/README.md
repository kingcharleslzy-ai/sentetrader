# 下单意图真实语句测试集

依据 `docs/ORDER_INTENT_V2.md` 第 3 节（设计稿在分支 `claude/order-intent-design-20261016`）。一句话一行，期望只写**结构**不写措辞；
账户状态用 `contexts.json` 的命名快照引用。这里只有测试数据，不含产品代码。

| 文件 | 内容 |
|---|---|
| `cases.jsonl` | 一句一行 |
| `contexts.json` | 命名账户上下文 + 品种目录 `_catalog` + 固定盘口 `_quotes` |
| `corpus.py` | 加载、结构校验、比较函数（`tests/test_intent_corpus.py` 与 `eval/intent/run_model_eval.py` 共用） |

## 一行的字段

`id`（`c001` 递增，不复用，文件按 id 顺序追加）· `say`（引擎实际收到的字面串，含 ASR 标点和误听）· `origin`（`typed` / `voice`）·
`ctx`（`contexts.json` 里的名字）· `src`（来源，见下）· `expect`（见下）· 可选 `notes`（拿不准或要解释的写这里）· 可选 `xfail`（main 现状已知不对，修好即删）·
可选 `model`（交易 AI 对这句话实际给过的读法，见下）。

`model`：一组 `{"intent": …, "plan": …}`。`intent` 用 `expect` 的键写模型给的意图（`x_symbol` 模型写的合约代码、`x_symbols` 全平时模型列的几个范围、`x_remaining` 平仓后剩余仓位的止损止盈、
`x_default_volume` 账号默认手数），`plan` 是 `propose_order` 的出处校验（`order_planner.ground`）加规划器对它应有的结果：出卡，或追问（原话没说的不出卡）。
有待确认卡的上下文里、`expect.act` 为 `amend` 的行把那张卡交给出处校验（明确改单）。`tests/test_intent_corpus.py` 逐条核对；模型的新说法问题（离线评测、#932）加在这里。

`src` 前缀：`issue#N` 出错原句 · `prod-test` 生产库测试账号 1/2 的用户原话（只读导出，见下）· `voice-sim` 语音模拟脚本 ·
`repo:<文件>` 仓库测试里的句子 · `eval/fuzzy` 模糊指令评测集 · `artifact:<目录>` 验收证据里的用户句 · `gap:<类>` 为补薄弱类新写的句子（不是真人原话，统计时单列）。

## `expect`（只比对写出的键；缺省=不检查；显式 `null`=必须为空）

键名沿用设计稿 2.1：`act` `stance` `product` `symbol` `dir` `vol` `target` `frac` `price` `bucket` `cond` `legs`，另有语料自己的 `ask` `scope` `fixed` `topic` `plan` `uncertain`。

- `act`：`open` `close` `close_all` `target` `reverse` `cancel_order` `retract` `amend` `confirm` `none`。（Runtime 里撤单叫 `cancel`——发布预检禁止 `cancel_order` 字样——测试比较时换回 `cancel_order`。）
- `stance`：`order`（可操作的指令，任何 act）· `question`（问事实：行情、保证金、怎么做）· `advice`（问该不该做）· `conditional` · `reported`（已发生的陈述）· `retracted`（整句自己收回）· `negated`（“不要/别…”）。
  **`negated` 是语料对设计稿六个语气的补充**：“不要全部平仓”“别撤单”既不是问句也不是转述，设计稿没有对应值；步骤 2 解析器若并入别的值，改这里一个词即可。
- `product`：**目录规范名**（`_catalog` 的键：螺纹/沪金/沪银/铁矿/热卷/甲醇…；白银→沪银、黄金/金子→沪金、螺纹钢→螺纹）；`@chart` = 当前图表合约（“这个合约”）；`@all` = 全部持仓。`symbol` 是句子里明说的合约（`螺纹2701`）。
- `dir`：`long` / `short`（多/空，不是买卖）；平仓行里 `dir` 是**被平持仓**的方向；没说就不写或写 `null`。
- `vol` 手数 / `target` 目标净持仓（多正空负，清零=0）/ `frac` 比例（平一半=0.5），三选一。
- `price`：`counter`（对价、对手价、**市价**）· `queue`（挂买一/卖一、排队价、挂单价/挂价）· `last`（现价）· `limit:数值`。设计稿还列了 `passive`，但今天代码里只有一种被动价（挂买一/卖一），语料全写 `queue`，比较时两者视为同一个；要分开由所有者另定。`last`（现价）沿用现有 `priceIntent=last`，设计稿 2.1 未列，算待定扩展。
- `bucket`：`today` / `yesterday` / `auto`（不写=auto）。
- `cond`：`{"type":"price","op":"<=|>=|<|>|reach","value":3000}`（跌到/涨到/跌破/突破；“做到 3100”这种方向不明的写 `reach`）或 `{"type":"time","text":"收盘前"}`。有条件时 `stance=conditional`。
- `legs`：一句话多个意图（按说出顺序），行里只写 `legs`（可加 `plan`）。解析器按分句逐句读（2026-10-17）：每单说全了合约、方向、手数才给 `legs`，拆不开或有一单说不清的行写 `uncertain`（`tests/test_intent_corpus.py` 的 `LEGS_READ` / `LEGS_UNSURE` 逐行钉住）。
- `ask`：缺哪些槽位要追问（`["dir"]`），表示不出卡，出追问卡。
- `scope`：撤单/撤回/改单对象：`pending_card` · `last_order` · `working_order`（可带 `product`）· `all_orders`。
- `fixed: true`：句中有改口（“开三手，不对，两手…”），期望只写**改口后的最终结构**。
- `topic`：非订单对照句的类别（`market` `account` `ui` `chat` `pref` `routine` `protection` `reminder` `risk_rule` `control`），仅供统计和读者看；`act` 一律是 `none`。解析器不判类别，测试比较时不比 `topic`。
- `uncertain: true`：快速规则必须回“不确定”（交模型/追问）。凡听不清、半截、会丢信息、并排两种读法的句子都用它，**必须在 `notes` 写明原因**。
- `x_…`：设计稿未定的扩展字段（`x_protect` 随单止损止盈、`x_tail` 句尾的界面话、`x_noconfirm` 句内“不用确认”、`x_live` 实盘字样、`x_order_price` 被撤委托的价格）。解析器没有这个字段就跳过，有就必须一致。
- `plan`：**规划器**在这个上下文下的期望：`{"outcome":"cards","cards":[{"side","vol","price"?,"bucket"?,"product"?}],"stepwise"?}` /
  `{"outcome":"reject","reason":"…","suggest"?}` / `{"outcome":"ask","slots":[…]}` / `{"outcome":"none"}`。`side` 是 `buy_open` `sell_open` `buy_close` `sell_close`。
  `stepwise: true` = 先出第一张，这一段成交后才出下一张（反手、反向目标、同一合约先平再动的一句两单，走目标持仓接力）；互不影响的一句两单（不同品种，或同一合约都是开仓）一次按顺序出全部卡、各自确认，`stepwise` 为 false（#950 审查）。只在上下文决定结果的行里写；没写 `plan` 时，`act: none` 或非 `order` 语气隐含“不出卡”，有 `ask` 隐含“追问”。
  今昨 `auto` 的拆分由 `close_allocation` 决定，行里没写 `bucket` 就不锁拆分。

`xfail` 是**信息**，不是自动判据：2026-10-16 用 main 的打字路由 `classify_turn` 与语音前台 `classify_action` 跑过全部行，凡期望“不出卡”而 main 判成出卡的写上现状。第 2 步（解析器 `order_intent_parse.parse`）起语音前台改用解析器、第 3 步打字路由也改用解析器后，这些说明全部删除（打字那半 39 行，逐条由 `tests/test_intent_corpus.py` 的 `TYPED_ROUTE_FIXED` 钉住）。现在每一行都要求：解析器有把握的下单句由规划器直接出卡，不是下单的语气不给出卡工具，拿不准的走交易 AI 的 `propose_order`。新发现的“main 现状不对”仍可临时写 `xfail`，修好即删。

## 所有者已定口径（2026-10-16，设计稿第 5 节）

八百二=820 限价（合约价位量级内取整，卡上显示实际价格）· 市价=对价（`counter`，卡上写“市价（按对价）”）· 一句两单=`legs`、按顺序出两张卡各自确认 ·
平一半向下取整、至少 1 手、卡上写明实际手数 · 不带手数的反手=按原持仓手数、两步出卡（先平后开）每步确认 ·
条件单 `stance=conditional`、不出即时卡、提议建价格提醒（`plan.reject` + `suggest: price_alert`）· 问句后跟“算了”=`question`、照常回答、不出卡 ·
整句自己收回=`act none` + `stance retracted`；**有待确认卡时**说“算了”=`act retract`（`ctx` 里有 `pending_card`）· 改单/确认放同一集合，用“有待确认卡”上下文区分 ·
测试集来源只用测试账号对话；所有者本人账号的对话没有导出，也不入库（要入库须所有者同意、脱敏并过目）。

## 怎么加一行

1. 先看 `cases.jsonl` 里有没有同类句；没有再加。每个新的“说法”问题只加一行，追加在文件末尾，`id` 取下一个数。
2. `ctx` 选现成的；确实需要新的持仓/挂单/待确认卡组合，在 `contexts.json` 加一个新名字（命名：`long2_rb` = 螺纹多 2；`order_rb_open` = 有螺纹买开挂单；`card_buyopen_rb1` = 有螺纹买开待确认卡），并保证至少一行在用。品种只用 `_catalog` 里的规范名。
3. `expect` 只写这句话**应该**得到的结构；拿不准就写 `uncertain: true` 并在 `notes` 说明，不要硬写。句子来自生产测试账号的，只放 `say`/`origin` 和结构，**不放** accountId、用户名、sessionId、成交价。
4. 跑 `uv run pytest tests/test_intent_corpus.py`（在 `services/trader-runtime`），校验会指出字段/上下文/品种名的错误，并要求解析器对这一行“给对或不确定”（给错即失败）。

## 语料来源（第一版）

`prod-test` 来自腾讯 Runtime SQLite 的备份副本 `/opt/ai-trader/backups/1.7.2/before-7207e01.sqlite3`（只读，`sqlite3 -readonly`；按测试账号 1、2 的 `user_id` 过滤
`message.completed` 且 `role=user`；没有复制库、没有导出所有者账号的任何对话；语音句靠 `origin=voice` 标记）。去重后再挑下单相关句与少量对照句（行情问句、界面话、转述、问句）。
