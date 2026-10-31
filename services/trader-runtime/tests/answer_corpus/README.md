# 回答卡测试集

依据回答卡结构化设计稿（2026-10-17，所有者已确认）4.2 节，写法沿用 `tests/intent_corpus`。一行一张卡，期望只写结构和
Runtime 渲染出的结果；读数用 `contexts.json` 里命名的读回组合引用。运行器是 `tests/test_answer_corpus.py`。

| 文件 | 内容 |
|---|---|
| `cases.jsonl` | 一张卡一行 |
| `contexts.json` | 命名的读数组合：本轮按顺序的工具读回（`tool` / `args` / `data`，`data` 即工具结果的 `data` 原样） |

## 一行的字段

`id`（`c001` 递增，不复用，按 id 顺序追加）· `ctx` · `src` · `card`（`answer_card` 的参数，新格式；`title` / `conclusion`
不写时运行器补“测试卡 / 看卡片。”）· `expect` · 可选 `notes` · 可选 `pending`（属于后续步骤，先跳过）。

`src` 前缀：`commit:<sha>` 回答卡核对的修复提交（G 层 18 个修复的原句改写成键格式，证明这类错误要么写不出来，要么被退回）·
`repo:<文件>` 仓库测试里的卡 · `prod-test` 测试账号对话 · `gap:<类>` 为补薄弱类新写的行。

## `expect`（只比对写出的键；缺省 = 空）

- `figures`：渲染后的格子 `{label, value, unit?, dir?}`，按顺序。
- `compare`：`{"left": [行…], "right": [行…]}`，或 `null`（不出对比）。
- `dropped`：退回的键或行的代码，按出现顺序。`ref_syntax` 键写坏 · `ref_field` 没有这个字段 · `ref_symbol` 合约本轮没读 ·
  `ref_missing` 这个合约没有这项读数（或没有这个周期）· `ref_ambiguous` 读过多个周期却没写周期 · `ref_preopen` 本交易日还没有成交 ·
  `ref_value` 取值不在枚举里、或句子里的定性键没写取值 · `ref_mismatch` 断言与读数不符 · `ref_column` 栏里写了别的合约 ·
  `text_digit` 键之外有数字（标题同样不许有数字）· `text_label` 键之外有读数标签词（风险行除外）· `text_long` 填上读数后超长。
- `error`：`answer_card_invalid`（整张卡退回）或 `null`。
- `conclusion` / `reasons` / `risk` / `symbol`：卡上渲染后的值；`spoken`：语音念稿（不带括号依据）。写了才比。

## 步骤

第 1、2 步（读数表、字段键、模板结论）的行全部启用；以后的步骤新增的行先标 `pending`，落地后补全期望
（`conclusion`、`spoken`）并启用。接受链与收尾短路（d0f78291、fe50fd25）属于发布时序，不在卡面，仍由
`tests/test_native_agent.py` 覆盖。
