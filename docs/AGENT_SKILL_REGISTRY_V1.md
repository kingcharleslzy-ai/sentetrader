# 内置 AI 技能注册表 V1

状态：已实施。技能资源与 `skill_registry.py` 是当前登记与判定入口，本文描述其职责和边界。

## 目标

内置 AI 的每一项能力都是一个**技能**：一处登记，驱动 AI 可用工具、语音/打字路由、界面按钮、档位开放、全局启停、帮助与外部 AI 技能文档。今后停止某个技能、给它配实体按钮、按档位开放，都只改注册表或在后台勾选。

## 遵循的开放标准（用户 2026-09-23 要求：换底层模型也要能精准发现和使用）

- **技能格式 = Agent Skills 开放规范**（https://agentskills.io/specification ，2026-09-23 核对）：每个技能一个目录，内含 `SKILL.md`（YAML frontmatter + Markdown 正文），可选 `references/`、`scripts/`、`assets/`。
  - `name`：1–64 字符，仅小写字母/数字/连字符，不以连字符开头结尾、不含 `--`，**必须与目录名一致**。
  - `description`：1–1024 字符，写清"做什么 + 什么时候用"，含用户会说的关键词（中文说法也写进去），这是模型发现技能的唯一依据。
  - `compatibility`（≤500 字符，可选）、`license`（可选）、`metadata`（字符串→字符串映射，放本产品扩展字段）、`allowed-tools`（实验性，不依赖）。
  - 正文 < 500 行 / 约 5000 token，细节放 `references/`（一级深度相对路径）。
- **渐进披露**（规范的三层）：① 目录层——会话开始只给模型所有**可用**技能的 `name + description`；② 指令层——模型判断相关时调用 `activate_skill`（参数 `name` 用可用技能名的 enum 约束，防幻觉）拿到正文，包在 `<skill_content name="…">…<skill_resources>…</skill_resources></skill_content>` 里；③ 资源层——正文引用的 `references/*` 按需读取（`read_skill_resource` 工具，只允许该技能目录内的相对路径）。
- **过滤而非拦截**：停用或未开放的技能**不出现在目录里**、`activate_skill` 的 enum 也不含它（规范建议），同时服务端仍做 `require_skill` 兜底拦截。
- **用户显式激活**：快捷技能栏按钮、语音/打字确定性说法命中时，由系统直接注入该技能正文（规范的 user-explicit activation），不依赖模型自己去激活——换弱模型也能稳定触发。
- **上下文管理**：已激活的技能正文在会话压缩时保留（按 `<skill_content>` 标签识别），同一会话重复激活去重。
- **工具 = 标准 JSON Schema 函数工具**（与 MCP tool 定义同构：`name`/`description`/`inputSchema`），不使用任何模型厂商专有字段；技能只"引用"工具名，工具定义仍由 `trader_tools` 提供一份。
- **对外一致**：MCP 服务与打包 CLI 暴露同一批技能（`list_skills` / `activate_skill` / `read_skill_resource`，CLI `skills list|show`），外部 Claude/Codex 等按同一规范使用；仓库 `skills/ai-trader/` 外部指南改为引用这批技能而不重复内容。
- **校验**：按规范规则写单元测试（name 字符集/长度/与目录一致、description 非空 ≤1024、compatibility ≤500、metadata 值为字符串、正文行数上限、引用的 `references/` 文件存在），不引入新依赖。
- **换模型验收**：按受影响路由复用既有指令评测，核对技能、工具选择和真实故障反例；不因文档变化重跑模型评测。

## 数据模型

内置技能以**静态资源**随 Runtime 打包（规范对云端/沙箱 agent 的推荐做法）：`services/trader-runtime/src/trader_runtime/agent_skills/<name>/SKILL.md`。本产品的扩展信息全部放在 frontmatter `metadata` 里（值都是字符串，列表用空格分隔）：

```yaml
---
name: strategy-signals
description: 解读策略当前信号：按用户策略规则讲"现在该怎么做"，并把进出场位置以编号标注画到图上。当用户说"按我的策略现在该怎么做""现在能开多吗""我的策略出信号了吗"时使用。
metadata:
  ai-trader-label: 策略信号解读
  ai-trader-parent: "strategy-management"
  ai-trader-summary: "解读运行中策略发出的开平仓信号"
  ai-trader-category: 策略
  ai-trader-tools: explain_strategy_signal
  ai-trader-routes: GET /api/strategies/signal
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: strategy:signal
  ai-trader-quick-action: prompt:按我的策略现在该怎么做
  ai-trader-default-tiers: basic pro test
---
（正文：何时用、步骤、边界与安全约束、示例说法与反例）
```

Runtime 启动时扫描该目录解析为注册表（**SKILL.md 是唯一事实源**，Python 不再另写一份清单）。覆盖当前全部能力（至少）：行情与合约查询、图表与指标、下单出卡、目标持仓、批量平仓、撤单/追单/改价、AI 自主执行、联网研究、行情简讯、策略管理与监控、策略验证与回测、策略信号解读、行情回放、回放控制、语音通话、图上标注、界面控制与读屏、复盘与日结、帮助、后台管理（owner-only）。**每个内置 AI 工具必须恰好属于一个技能**、每个写接口必须属于一个技能（测试保证）。

## 启停与开放

- **全局启停**：新表 `skill_settings(skill_id PRIMARY KEY, enabled INTEGER, updated_at)`，缺行=启用。主账号在后台停用后对**所有人**生效（包括主账号本人），用于"停止某个技能"。
- **按档位开放**：`plan_tiers.config` 的 `features` 迁为 `skills`（技能 id 列表）。一次性迁移：旧 7 个功能键映射到对应技能组（`ai_execution`→AI 自主执行；`web_research`→联网研究；`market_briefs`→行情简讯；`strategies`→策略管理与监控+策略信号解读+语音引导设计策略；`backtests`→策略验证与回测；`replays`→行情回放+回放控制；`voice`→语音通话），`tier_gated=False` 的技能不进列表、始终可用。迁移幂等、保留主账号已做的开关结果。
- **生效判定**：`skill_allowed(store, user_id, skill_id) = 全局启用 ∧ (owner 或 not tier_gated 或 档位包含)`（AI 视角）；用户自己的请求（`human_request`：不是经 `/api/agent/call` 进来的）只有 `AI_FEATURE_SKILLS` 受全局停用约束，停用只管 AI（2026-09-27 主账号决定）：本身就是 AI 功能的技能（AI 自主执行、行情简讯、语音通话、联网研究、AI 引导设计、图表分析、外部 AI 入口 app-catalog，即 `AI_FEATURE_SKILLS`，`GET /api/agent/skills` 标 `aiFeature: true`）停用后对所有人（含主账号）停；其他技能停用只从 AI 那里拿走（内置工具隐藏、能力目录不列、`/api/agent/call` 拒绝、语音/打字路由拒绝），用户自己界面上的按钮和它们直接调用的接口照常可用；`owner_only` 技能仅主账号。`require_skill(...)` 取代 `require_feature(...)`（403 `skill_disabled` / `skill_not_in_plan` 两个可区分的业务码；为兼容旧客户端，`feature_not_in_plan` 继续作为档位拒绝码的别名保留一版）。
- **兼容**：状态载荷继续输出 `features`（由技能推导）与 `strategyTools`，1.6.x 客户端不受影响；新增 `skills`（该用户可用技能 id 列表）。

## 执行点（全部从注册表派生，删除各处硬编码集合）

1. AI 工具：`hidden_tool_names` 改为 `注册表中不被允许技能的 tools 并集`；`FEATURE_TOOL_NAMES` 删除。
2. HTTP：一个 app 级 FastAPI dependency（`write_skill_gate`，随 app 构造烘焙进所有路由）按请求匹配到的路由查 `METHOD path → skill` 归属表，有归属且是写方法（POST/PUT/PATCH/DELETE）即 `require_skill`——注册表路由表本身就是门，不再逐路由写调用点。写门刻意放过的有归属写路由共三条（`HANDLER_GATED_WRITE_ROUTES`，各带书面理由）：回放 control（close 是与 ai-execution/stop 同类的安全退出动作，停用技能也要放行；其余动作由 handler 自己把关）、邀请码生成（它的门是认证层的 owner 会话判定——遗留库里与主账号同名的 `cloud_users` 行会让 owner_only 误判真主账号）、行情简讯刷新（1.6.8 客户端契约：档位未开回温和 `status: "disabled"`，仅全局停用才是 403，由 handler 精确区分）。留在 handler 里的显式 `require_skill` 另有两类语义不同的：GET 接口（写门只管写方法）与引导设计会话入口。技能管理接口本身（`GET/PUT /api/admin/skills`、`PUT /api/admin/skills/{skill_id}`）刻意不归属任何技能：它们是恢复入口，停用 admin-console 不得让主账号无法再启用技能；能力目录条目带 `skill` 字段，并按生效判定过滤。
3. 语音与打字路由：`voice_intents.match_intent` 与 `turn_router.classify_turn` 命中后先查技能是否可用，不可用返回统一口播/回复："「<技能名>」已停用"或"当前档位未开通「<技能名>」"。
4. 界面：`GET /api/agent/skills`（当前用户：每个技能的 id/名称/分类/说明/可用状态/不可用原因/典型说法/界面入口/快捷动作）。客户端用一个 `useSkills()` 统一决定按钮显示/置灰与提示（替换各处 `features.xxx` 读法）。
5. 帮助与文档：帮助目录（`help/catalog.json`）的「技能」章节由注册表生成（数据里是占位 `{"generated": "skills"}`，`app_help.py` 加载时展开）（名称+说明+典型说法）；`skills/ai-trader/SKILL.md` 与 `docs/FUTURES_AGENT_CAPABILITY_V1.md` 各加一张技能表，并有单元测试核对"注册表中的每个技能都出现在这两份文档里"（防漂移，只核对技能 id 存在，不做文本快照）。

## 后台与实体按钮

- 后台管理的「技能」分区：矩阵——行=技能（按分类分组，显示说明），列=「全局」开关 + 各档位勾选；owner_only 与不受档位控制的技能在档位列显示"—"。改动即时保存。原「档位」矩阵里的 7 个功能开关行改为指向技能矩阵（删除重复）。
- **快捷技能栏**：有 `quick_action` 的技能可在「设置 → 界面」里勾选钉到工作台的快捷技能栏（账号偏好 `ui.quickSkills`，默认不钉选；旧默认项由启动迁移一次性清空，后续用户自己钉选的保留）；点击即执行该动作（能力调用或向 AI 发送预置指令），不可用时按钮置灰并提示原因。语音说"把策略信号放到快捷栏"也可操作（UI 命令）。

## 管理接口

`GET /api/admin/skills`（注册表 + 全局启停 + 各档位勾选矩阵）、`PUT /api/admin/skills/{id}`（`enabled`）、档位的技能勾选走现有 `PUT /api/admin/plans/{name}`（`skills` 部分映射，合并语义同 features）。全部仅主账号、进能力目录。

## 验收

- 每个工具/写接口归属唯一技能；停用任一技能后：AI 看不到其工具、能力目录不列、Agent 调用拒绝、语音/打字给出统一提示；用户直接操作是否受限按上文 AI 功能判定，不把所有手动功能一并关掉。
- 档位勾选与全局启停的组合判定正确；owner 受全局停用约束、不受档位约束。
- 旧 plan 配置迁移结果与迁移前各档位可用能力完全一致。
- 1.6.x 客户端（只认 `features`）行为不变。
