# Sente（原 AI Trader）当前架构

对外品牌 2026-10-04 起为 Sente（中文名弈先，AI 私人交易助理）；仓库、包名、bundle identifier、接口路径、界面命令 id、技能 id、更新源与安装包文件名仍用 ai-trader / AI Trader，不随品牌改名。

## 1. 结论

React 单页客户端与 FastAPI Trader Runtime 共用云端业务入口；Tauri 只承担桌面窗口、更新及打包 CLI / MCP，不持有 TqKq 交易进程。TqKq sidecar、公共行情和本站模拟引擎在云端运行。Runtime 用自己的模型循环（`native_agent.py`）调用模型，Fake Adapter 仅用于 CI、离线演示或显式测试。模型负责理解与工具编排，授权、数据校验、审计和恢复由普通程序负责；手工与 AI 模拟操作最终进入同一确定性交易链。

风险预算由 Runtime 将当前账户盈亏与已确认的版本化风险规则确定性合成；账户事实不可用时不把零值当作未亏损。到价提醒（2026-10-02 起服务端实现，详见 `docs/RISK_ALERTS_V1.md`）由 Runtime 的 `price_alerts` 后台监控在每秒轮询里对新鲜行情判定穿越并只触发一次；提醒按用户保存在 `price_alerts` 表，不进入 Sidecar、订单服务或策略运行时，不新增交易状态或任何自动执行能力。

```text
React / PWA / Tauri 单页客户端
        │ IPC bootstrap + HTTPS / SSE
        ▼
FastAPI Trader Runtime
  ├── FakeAgentAdapter / NativeAgentAdapter（openai SDK Responses 流式循环）
  ├── TraderReadOnlyTools（进程内函数工具）+ WorkspacePolicy
  ├── Serialized RunManager
  ├── AccountRegistry / AccountPoolManager / PaperAccountManager
  ├── FakeReadOnlyDataProvider / SidecarReadOnlyDataProvider / PaperProvider
  ├── Trusted RiskPolicy + InstrumentSpec
  ├── SimulationOrderService + AutonomousSimulationService
  ├── tqkq_sim_order_links + ai_execution_authorizations + ai_stop_orders
  ├── StrategyReleaseManager + independent runtime state root
  ├── Deterministic Backtest Worker
  └── SQLite Session / Event / Run / Artifact / Proposal / Release Store
```

交付层不复制业务 UI：同一 React 构建进入 macOS/Windows Tauri，或以 `/trader/` base path 进入网页静态站点（下载页 `/trader/download` 属官网应用）。网页与桌面端都连接云端 Runtime 的独立 HTTPS 子域（2026-09-16 起在腾讯云上海主机），桌面端安装后首屏即登录、不再有本机运行模式。生产 Runtime 容器只监听回环地址，Nginx 负责 TLS、连接限制和长连接转发，网页登录由 Runtime 认证后签发 Secure HttpOnly Cookie；桌面与 MCP 复用签名会话。外部 OIDC 是另一个可配置部署模式。

模型供应商和 TqKq 数据都先接入 Runtime 边界。Voice Bridge 只负责语音转文字和最终回复播报，不拆多 Agent 或增加第二套交易逻辑。Runtime 始终注册只读数据、候选、策略和风险工具；启用模拟网关时再注册单次下单、查询、撤单、平仓、止损及策略监控工具。AI 模拟操作不请求逐单或限时授权，但每次调用仍重新验证内部租约、行情、账户、持仓、活动委托和风险规则。

模型密钥在云端账号模式下全部由服务端托管；桌面端不再保存或解密任何模型密钥（2026-09-16 起云端单模式，本机 DeepSeek Key 入口已移除）。浏览器开发的本地 Runtime 仍从环境变量（支持 `_FILE` 变体）读取密钥，密钥只进入本机模型循环的端点表，不落盘、不进子进程。外部信息检索由 Runtime 注册的 `web_search` 函数工具完成：Runtime 服务端调用 DeepSeek Search / Fetch API 并把标题、链接、摘要和发布时间回传给模型（实测 DeepSeek Responses API 与智谱 chat 接口都会静默忽略第三方调用方的内置 `web_search`），搜索密钥只在服务端 secret，回放内禁用，未配置时失败关闭。模型密钥只存在于 `native_agent.build_model_endpoints` 生成的端点表里，模型池每个供应商的 `api_key_env` 由 `secret_value` 读取（支持 `_FILE` 变体）。

云端 `owner-password` 模式支持所有者与邀请码注册的成员，数据按用户和交易账户隔离。模型池及可用档位由 `model_provider`、`plans` 与技能注册表决定，客户端从 `GET /api/models` 发现可选模型；用量按用户计入档位额度。模拟执行在云端由各账户的业务服务与对应网关完成；`cloud-jwt` 的独立部署限制不能用于描述本站账号模式。

工作区向模型传递单独校验的有界期货上下文，包含当前页面/辅助模块、实际合约、周期、图表选价、订单草稿、选中持仓、行情时间/年龄、今昨仓冻结量、活动委托、最近成交和当前软件会话最近 12 个决策相关操作。Runtime 在页面历史中只保存用户原问题；模型输入要求按完整语义解析自然指代，并明确页面操作只是注意力线索，不是授权、成交或正式事实。任何回答或交易候选仍必须通过只读工具重新读取 Runtime、行情、账户、持仓与活动委托，默认推理强度为 `low`。

2026-09-07 的接口精简沿用现有业务服务：Sidecar 预取、模拟委托和回放命令复用 `SidecarHttp` 的异步 HTTPX 连接池；同步工具和后台状态线程复用独立同步连接池。连接池随 provider 生命周期关闭，保留请求时限、回放串行、发送前检查和原订单 UNKNOWN 回读，不配置自动重试或代理。2026-10-12 容量测试后，`SidecarHttp` 改用 httpx2（httpcore 1.0.9 在建连阶段被取消的请求会在池里留下永远 CONNECTING 的连接，几轮过载占满连接位后只能重启 Runtime；httpx2 已修，见 pydantic/httpx2#983）：每个 sidecar（含号池每个子进程）一份短读取池（48）和一份 K 线长轮询池（`waitMs=`，64），长轮询再多也不挤占报价与总表；请求只用 httpx 自身分项超时（建连 ≤2 秒），被 anyio 转成超时的取消照旧按取消抛出；一个池连续 30 秒只有池等待超时才整池重建（防御）。共享行情的上游取数按种类（目录、总表、日历、报价、号池端口）各自最多 32 个在途，报价按合约缓存一秒、同一合约在读就并入那次读取。

工作区配置、操作记录、简讯与策略/手续费参数由 FastAPI/Pydantic 定义。`npm run generate:workbench-api` 从实际注册函数离线生成 `workbench.generated.ts`，不构造主 Runtime、不连接账户或模型；`AI_TRADER_SCHEMA_PYTHON` 可指定与 `uv.lock` 匹配且不绑定其他工作树源码的共享 Python。前端使用 `openapi-fetch` 和同一认证请求头实现；32 个原生工具从各自真实校验模型生成 schema，不再单独维护属性表。配置、活动、简讯 GET 使用 TanStack Query，查询 key 包含登录代次、Runtime/回放地址及相关合约；身份变化清缓存，不保留前一合约的数据占位。配置 PUT 保留 revision 检查，交易写操作不进入查询缓存、乐观成交或离线补发。

## 2. 统一事件

共享 TypeScript 契约位于 `packages/ui-events/src/index.ts`。所有事件具有：

```ts
interface UiEventEnvelope<Name, Payload> {
  schemaVersion: 1;
  id: string;
  sessionId: string;
  kind: UiEventKind;
  name: Name;
  timestamp: string;
  payload: Payload;
}
```

Python 发布前和 TypeScript 消费前都会深度校验 v1 契约；未知或无效事件在前端降级为警告，不会使页面崩溃。`trade.approval.required` 与 `risk_rule.approval.required` 是确定性业务确认；模型没有工程操作，也不会中途向用户提问。`feed.*`（account/market/activity/execution/ui/status）是 Runtime 主动推送的瞬态快照事件：不落库、不进对话，客户端 `runtimeFeed.ts` 接收后替代轮询，某个通道超过 10 秒没有推送或被标记不可用时，只有该通道恢复自己的轮询间隔。

### Runtime feed

`runtime_feed.py` 是 Runtime 内的一条循环（0.5 秒一拍，Sidecar 快照到达或界面命令入队时立即再拍），只对已连接 SSE 的会话推送瞬态事件：

| 事件 | 内容 | 节流 |
| --- | --- | --- |
| `feed.account` | `/api/account/summary` + `/api/positions` + `/api/account/activity` + `/api/risk/summary` 的合并结果 | ≥1 秒 |
| `feed.execution` | `/api/ai-execution` | ≥1 秒 |
| `feed.market` | 会话关注合约/周期的公共 `market/workbench`（带共享源版本 `revision`） | 配置共享源后行情一变即独立推送，慢端只保留最新完整状态 |
| `feed.market-patch` | 相对客户端已持有的 `base` 版本只推变化：变化的字段（报价变了带整份报价）与 `barsPatch {bars, trimBefore}` | 仅发给在 `PUT /api/sessions/{id}/feed` 里声明 `marketPatches: true` 的会话（旧桌面客户端不声明，仍收整份）；约 1 KB 替代每次约 41 KB 的整份图表；与整份行情共用一个“最新行情”槽，客户端发现 `base` 对不上就重发关注拿一份整份，重连也先发整份 |
| `feed.activity` | 关注合约的 `/api/app/activity` | ≥1 秒 |
| `feed.touch` | `{name, stamps: [[path, value], ...]}`：`feed.account` / `feed.activity` / `feed.execution` 业务内容没变、只是到了 5 秒刷新点时，只带那份帧里的 `capturedAt` / `ageSeconds`（客户端写回上一份整帧并记为新鲜） | 仅发给在 feed focus 里声明 `feedTouch: true` 的会话（2026-09-28；实测委托活动 13 KB、执行状态 5.5 KB 每 5 秒的整帧变为约 100 字节）；内容一变、读者重连都照发整帧；旧客户端不声明仍收整帧 |
| `feed.ui` | `{reason: command|capture}`：agent 排队了界面命令或截图 | 即时 |
| `feed.board` | 报价总表（2026-10-12）：该窗口要的行（`GET /api/market/board` 同一行结构），带总表版本 `feedRevision` 与客户端给的筛选名 `key` | 仅发给在 `PUT /api/sessions/{id}/feed/view` 里声明了 `board` 且页面可见的会话；首次、换筛选、重连、对不上 `base` 重发视图、恢复可见时各一份整份 |
| `feed.board-patch` | `{feedRevision, base, key, meta, rows: {symbol: 变化字段}}`：相对 `base` 只带变化的行与字段（只含该窗口筛选内的行）和变化的元数据，抓取时间每次都带；`base == feedRevision` 是 5 秒无变化的刷新 | 一个循环每秒读一次共享总表（与 HTTP 总表共用 1 秒缓存），整张表的差异只算一次，每种筛选的帧只构建、编码一次后扇出；行增删或换序（换月）时给所有人发整份 |
| `feed.status` | `{channel, status: unavailable, reason}`：某个通道这次读不出来（行情/账户/总表读取失败），带 provider 自己的原因码 | 与该通道同一发送位 |

一拍内同一份数据只读一次（账户活动在账户与操作记录之间共享，同一合约行情在多个会话之间共享）；账户、行情、操作记录在 Sidecar 有新快照或模拟委托链路报告状态变化（`SimulationOrderService.on_change`，变更提交后才通知）后重读，但快照门最多压住 5 秒——provider 停更本身就是要告诉客户端的陈旧，不能被门永久挡住；摘要（忽略 `ageSeconds`/`capturedAt`）没变就不发，但每 5 秒重发一次保持客户端时效显示；读取失败不再沉默跳过，而是在该通道发 `feed.status`（同一发送位，可用与不可用之间的切换一定会发出去）；SSE 读者重新连接后全部快照重发一遍。会话通过 `PUT /api/sessions/{id}/feed` 告诉 Runtime 自己的合约与周期；回放子应用不起 feed。事件不落库、不进对话。多个会话收到同一份内容时只构建一次（2026-10-13 扩容①）：同一行情版本的 `feed.market-patch` 只拼、只校验、只编码一次；一拍内同一读取键（账户、用户、合约）的 `feed.account` / `feed.activity` / `feed.execution` 摘要、校验与编码各一次，`feed.touch` 的时间戳也共用；游客 SSE 的整份/增量文本只在有游客读这一版时才编码。关注时可带 `since`（本机最后一根已收盘 K 线的 `datetime`，与 `GET /api/markets/{symbol}/workbench?since=` 同一规则）：这次关注的第一帧 `feed.market` 只带之后的 K 线及 `barsAfter` / `barsWindow`，客户端用本机 K 线核对后拼成整窗，核对不上就不带 since 重发关注拿整窗；since 不在窗口内时首帧就是整窗，之后的整份帧（重连、对不上 `base`）都是整窗。热路径 JSON（feed 帧、SSE 信封、Sidecar/号池响应）走 orjson（`fast_json.py`），输出与原先标准库一致（浮点同值、中文原样、键序不变；orjson 不接受的非字符串键、超 64 位整数退回标准库原样输出）。

客户端 `runtimeFeed.ts` 按会话、**按通道**保存 `{receivedAt, sourceAt, status, error}`；账户总览、AI 执行、行情、操作记录各看自己的通道：该通道最近 10 秒内收到过可用的推送才跳过轮询，超时或收到 `feed.status` 不可用就只恢复这一个轮询（行情停更时执行控制的推送不再替行情作证，界面按既有 `dataStatus` / 行情陈旧提示显示）。例外是行情通道：共享行情只在变化时推送，HTTP 读取拿到的也是同一份共享数据，所以只要该会话的事件流还活着（空闲时每秒一次心跳，5 秒内收到过任何帧），可用的行情通道就算在线、不轮询；休市时不再每 2.5 秒整份重拉（2026-09-27 周日实测：3 个窗口一小时 4,682 次）。其余读取在 feed 存活时只做兜底：操作记录 60 秒、成交复盘 60 秒（新成交改变查询键时立即读一次）、简讯 60 秒、账号偏好 60 秒、工作台设置 120 秒、模型账号 120 秒；feed 断开时恢复原来的短间隔。界面心跳 `POST /api/ui/pulse` 在 feed 存活时从 0.5 秒放宽到 30 秒（2026-09-27 前为 10 秒），收到 `feed.ui` 或本地截图完成时立即心跳，关闭页面时立即报 closing；服务端窗口 75 秒无心跳视为离线（此前 20 秒）、450 秒后清除。

**只推看得见的（2026-10-12）。** 客户端 `feedView.ts` 用会话本身作身份（不另造窗口身份），通过 `PUT /api/sessions/{id}/feed/view {visible, board}` 告诉 Runtime：页面是否可见（Page Visibility API，切换时立即发）、当前挂着的总表读者要哪些行（各读者需求的并集：全部，或若干交易所 / 品种 `au`、`SHFE.au` / 合约；报价视图只开一个交易所或“自选”时只要这些行，合约抽屉与手机看盘要全部，快讯只要事件标签的品种）。每次事件流（重新）打开、总表补丁对不上 `base`、需求或可见性变化时都重发一次，每次重发服务端都从一份整份总表重新开始。页面隐藏时 Runtime 不推图表行情（释放该会话的共享行情通道）与总表；`feed.account` / `feed.activity` / `feed.execution` 以及委托回报、到价/收盘提醒、策略信号、AI 结果等事件照常推送，不因隐藏而丢；恢复可见时先补一份整份图表与整份总表再继续增量。旧客户端不发视图：等同可见、无总表推送，照旧轮询 `GET /api/market/board`。总表读者在推送覆盖时不轮询；事件流断开、总表通道 `feed.status` 不可用、或 10 秒没收到总表帧（服务端至少每 5 秒刷新一次）就回到原来的轮询（报价视图 3 秒、快讯标签 15 秒），失败时保留上一屏并在顶部写原因。手机多图的单元格不走推送，仍只读屏幕上的格子（每 5 秒、页面隐藏时不读）。

### 实例租约

同一个 SQLite 状态库同时只允许一个 Runtime 实例跑会下单或写账的后台循环——止损监控（`autonomy`）、策略监控（`strategy_monitor`）和后台记账（`account_ledger`）。租约是状态库里的单行 `runtime_lease` 表（`instance_lease.py`，store 的 `instances` mixin）：启动时在一个 `BEGIN IMMEDIATE` 事务里抢占，抢到的实例成为 leader 并启动这三个循环（记账先 `run_once` 补一次账再起循环）；第二个实例成为 standby——照常提供全部 HTTP 接口、照常跑只读的推送 feed，但不起这三个循环。leader 每 10 秒心跳，30 秒无心跳的租约可被其他实例接管；心跳发现租约已被别人持有时先停会下单的循环、再停记账循环，之后按 standby 节奏重新争取。桌面本机 Runtime 重复拉起、测试版与正式版指向同一状态目录、部署换镜像时新旧容器短暂并存，都由该租约保证只有一个实例在执行；心跳异常不会让实例失去租约，只有心跳被拒（别的实例已持有）才降为 standby；旧实例正常关闭时先停循环再释放租约（部分启动先完整清理；停止失败不释放租约，失租后仍停不下的进程退出，防止两个实例同时执行），新实例下一次重试即接管，被硬杀则等 30 秒过期接管。`/healthz` 保持 `status: "ok"`，新增 `instance` 字段（`instanceId` / `role: leader|standby` / `leaderId` / `leaderSince`；回放子应用不建租约，报 `role: "replay"`），部署脚本按版本与提交断言的 healthz 语义不变。

后台生产者同样只在 leader 上跑（2026-10-12，`wiring/producers.py`）：快讯轮询及其新闻筛选、今日新闻要点、标题翻译，研究台 08:45 预生成，多 AI 研讨对账，到价提醒，收盘前/风险度/保本/交割提醒，盘中异动，提醒记录 30 天清理，06:00 私人记忆整理，15:15 今日复盘（外盘美元子账户美东 17:15），交易所标准手续费每日抓取——凡是读外部源、调模型或写共享状态的后台循环都在拿到租约时随下单循环之后启动、失租或关闭时先于下单循环停下（释放租约前），停止是屏障：研究台预生成已发出的模型调用随之取消，快讯/日历/LPR 轮次与手续费抓取已在工作线程里跑的等它结束（最多 3 秒，超时记日志照常移交）；它们各自的数据库条件更新与推送认领保留为移交瞬间的第二道保护；翻译服务原来自建的 `news_translate_lease` 与 `news_translate_day` 不再使用，表保留（蓝绿切换时旧版本仍在同库上用它们），以后再退役。standby 不跑这些循环，读侧全部回查共享库：研究台判断每次读 `research_desk_judgments`（不再只读启动时加载的内存）、新闻要点读 `news_briefs`、快讯/经济日历/最近一轮轮询状态读 `news_flash` 与 `research_live_cache`，读者看过的品种写 `news_screen_active` 供 leader 的轮询续筛；平台模型调用日预算（新闻筛选、标题翻译、研究台）计在共享表 `model_call_days`，重启和换槽不重拿额度。快讯合并在一个写事务里读-合并-写，移交瞬间两个进程同时合并也不丢来源、不出重复行。新 leader 启动或接手时按“是否已到期、当天是否已完成”补跑：08:45 之后、15:00 之前接手立即补当天预生成（已有当天判断的品种不调模型，开盘前推送每人每天只认领一次）；06:00 整理与 15:15 复盘照原有认领在一分钟内补上。今日复盘生成前先认领（推送认领表一行，10 分钟未完成可被接管），只有认领者调模型并保存，已保存未送达的复盘原样送达、不重新生成，存档与推送是同一份文字；当日“无成交”只在账户活动新鲜（行情正常、委托已初始化）时判定且不作当天定论，活动读取瞬时为空不会让当天复盘静默缺失。不属于生产者、每个进程都跑的：推送 feed、本进程研讨的心跳、未成交委托看护（2026-10-15 #932 起被看护的委托落表 `unfilled_watch_entries`，重启或换槽后接着看，提醒按认领只发一次；卡片跟踪仍在本进程内存里）。

**统一提醒（2026-10-15，#932，`reminders.py` / `reminder_kinds.py` / `store/reminders.py`）。** 各类提醒的判定留在各自模块；投递只有一个入口 `reminders.deliver_reminder`：①这类提醒的开关（`reminder_kinds.KINDS`，每类一个 `notify.*`，同时管对话、桌面通知、语音、锁屏推送；急停无开关）；②认领——`reminders` 表一行（用户×类别×去重键唯一），与对话消息、提醒卡（outbox 暂存）以及调用方自己的业务认领（到价转已触发、保本问题行、复盘 `pushed_at`、给上一版槽位读的旧去重账本行）同一事务提交；③落点——交易与账户类按调用方给的会话或 `reminder_target_session`，信息类（开盘前判断、研讨完成、AI 简讯）不写对话；④提交后 flush、`feed.reminder`（`id`/`type: new|update`/`unread`）、口播（节流按 kind+对象）、`PushHub.notify`。策略确认卡、执行回报卡、目标持仓下一步确认卡标 essential：开关关着也照常进对话。只持有 store 的账户循环（自主执行、策略监控、账户台账）经 `store.reminder_sink` 拿到所属 app 的入口。成交/撤单只由账户台账一处判定（成交按成交 id 在 `reminder_fill_claims` 与提醒同一事务认领，重启或换实例重新分组也只提醒没提醒过的那几笔，#932 P2；认领返回本事务实际新认领的成交 id，提醒记录、feed、口播与推送都只按这批成交生成——两个实例事务前读到同一批成交也不会重算，`ReminderSpec.claim`，#932 R1）。投递结果 `Delivery.outcome` 区分 delivered / off（开关关）/ claimed（已提醒过）/ failed（事务回滚）；台账只把 failed 的成交与撤单留作待处理、下一轮同一台账重读（待处理的成交不受原 600 秒成交新鲜度过滤），自首次失败起最多成交 600 秒、撤单 1800 秒，渠道在提交后失败不算失败、不重新认领（#932 R2）。交割由 30 秒账户循环判定。首次启用时把近 30 天旧去重账本迁成已读记录（`reminder_meta` 记启用时刻，台账重启后据此补报其后的成交）。提醒中心读 `GET /api/reminders`、标已读 `POST /api/reminders/read`（客户端 `useReminders`：当天一次最多读 10 页，还有就显示“继续加载当天提醒”（界面命令 `reminderCenter: {more: true}`，快照 `moreToday`；刷新失败时已读出的行、错误与该按钮都保留）；换交易日时“查看更早”从头读；标已读依次发出、读取等已发出的标已读，未读数只认最新一次请求的回复，#932 R5/R6）；点账户类提醒经 `useAccountNavigation` 总是先发切换（写入按会话依次发出、不截断不重发；断线后另起），只有显示的账户是它、且显示的 runtime.status 不早于这次切换回复的 `statusAt` 时才打开目标——早先切换迟到的状态或之前显示的账户都不算；导航最多等 15 秒、到时提示不打开；账户选择器与 `tradingAccount` 命令的切换也走这同一条写入队列——先放弃等待中的提醒导航并提示，前一笔切换有结果后才发，用户自己的选择是最后一笔（#932 R3）；历史卡结局由 `useProposalSettlement` 只读回读（失败有界退避重读，断线时中止在途读取、重连立即重读，换会话或重连后迟到回复丢弃，#932 R4）；`ReminderRetention`（租约生产者）按 `AI_TRADER_REMINDER_RETAIN_DAYS`（默认 30）清理。

## 3. 会话、事件和运行

1. 客户端用本地令牌换取 HttpOnly Cookie。
2. 客户端恢复已保存的会话；会话失效时才创建新会话。连接过程中断网、5xx 或读取（GET/HEAD）超时算暂时不可用，按 1 s 起、最长 15 s 的退避自动重试，尝试不重叠；401 进入登录，其他 4xx 与写入超时不重试（`requestTimeout.ts` 的 `isTransientFailure`）。
3. `GET /api/events?sessionId=...` 回放 SQLite 事件后持续推送，SSE `id` 支持断点续传。空闲时约每秒一次心跳；客户端 20 秒内连心跳都没收到就放弃这条连接，带 `Last-Event-ID` 重连（`sse.ts` 空闲看门狗）。推送循环每秒复核一次调用者身份与连接（所有者 2026-10-13 批准，原为每条事件一次）：登出、令牌撤销、用户停用或会话过期最迟约 1 秒断流，这 1 秒内仍可能送达事件；只有本进程投递了持久化事件才读事件表，否则每秒读一次兜底（同库的另一进程投递的事件最迟约 1 秒送达）。
4. 每个会话只有一个 FIFO Agent worker，消息不会并发修改同一工作区。
5. 运行状态写入 SQLite，可查询和取消；进程重启把未完成运行标为失败并追加恢复错误事件。
6. 会话设置 TTL，每个会话只保留最近 500 条长期事件；文字、终端和 Diff 增量每 150ms 合并到实时 SSE，最长约 5 秒保存一次快照，完成时保存最终事件或 artifact，避免长任务挤掉历史。
7. 候选业务状态和对应 UI 事件在同一 SQLite 事务写入本地 outbox，重启后可补发；候选、幂等记录和审批审计不跟随会话 TTL 删除。
8. 会话关闭、过期或删除会统一取消运行、释放 FIFO、丢弃内存历史、清理 SSE 等待器和未确认部署目录；候选及审计继续保留。
9. 改动留痕（所有者 2026-10-30）：会被原地改写的交易对象——保护单的止损止盈价、状态与手数（`ai_stop_orders`）、目标持仓计划、到价提醒、执行限额与停止状态（`ai_execution_controls`）、策略的原地改写——每次新建、改动、删除都由数据库触发器在共用的 `state_changes` 表追加一行（对象、改前改后、时间；改价位的代码在 `changed_by` 写明 user / trailing / strategy，状态流转记为系统）。表与触发器在 `store/state_changes.py`，每次启动在全部迁移之后重建（重建表的迁移会丢触发器）。“当时是多少”一律从这里读，不读当前行：交易验尸按平仓那一刻的止损止盈价判断。只记上线之后的改动；没有改动记录的旧保护单仍按当前价位。

内存只保存 SSE 等待条件、队列和当前协程，不作为业务状态真相。

## 4. 模型循环与工作区

```text
RunManager 取出会话 FIFO 中的一轮
  → NativeAgentAdapter.handle_message
  → 历史（Runtime 维护）+ 本轮 workspaceContext → client.responses.create(stream=True)
  → output_text.delta / reasoning_summary.delta / function_call → message / task UiEvent
  → 工具在进程内执行（TraderReadOnlyTools.call_async），function_call_output 追加后再发下一轮
  → response.completed → task.completed / agent.result / message.completed
```

`NativeAgentAdapter` 负责会话、历史、请求与事件；`TraderReadOnlyTools` 负责每个工具的参数校验与业务调用；`WorkspacePolicy` 只提供开发者指令与工程根目录展示路径。每个模型池条目一个 `AsyncOpenAI(base_url, api_key)` 客户端；供应商差异只在一张小表里（是否发 `reasoning`、是否要摘要），DeepSeek 思考模式返回的 `reasoning` 输出项会原样随工具输出传回。策略解释、行情简讯和策略设计通过同一适配器的 `complete_structured`（`text.format=json_schema` + 指令内重述 schema）完成，没有对话历史、只允许 `web_search` 一个工具。Fake Adapter 保留给离线演示和 CI。

历史只保存用户问题与助手回答；本轮的工作区快照与路由指令只随本轮请求发送，早轮的工具往返在轮次结束后折叠，超过预算时本地截断并由同模型生成一段摘要。被取消或失败的轮次从历史回滚；重启后从 `events` 表重建，未得到回答的问题不会进入历史。

Runtime 进程内不再有子进程：模型密钥只存在端点表里（`repr` 不含密钥），不进入事件与日志。本地正式模式默认选择整个 AI Trader 仓库作为工程根目录，也可通过 `AI_TRADER_ENGINEERING_ROOT` 指定；工程根目录只承载回测制品与策略工作区，模型没有文件、shell 或网络工具。

工具注册规则不变：Runtime 始终注册只读数据、候选、策略和历史回测工具（提议风险规则的工具 2026-10-13 按所有者决定删除：模拟账户没有风险规则，生产 `active_risk_policies` 为 0 行）；连接对应账户的模拟网关时再注册受控执行及策略监控工具。工具对象只持有 `AutonomousSimulationService`，不持有凭据或 `TqApi`。服务端对每次动作重新读取内部执行租约、行情、账户、持仓、冻结量、活动委托和风险规则；平仓由 Runtime 决定方向及今昨仓。入参和返回值都经过 Pydantic 校验，陈旧或不可用事实失败关闭；未知工具名与参数错误作为工具结果回给模型，工具异常发一张警告卡后本轮继续。

## 5. 只读数据与独立状态

### 账户与身份

`trading_accounts` 登记账户，`account_grants` 管理 owner/operator/viewer，`user_default_accounts` 保存用户默认账户。会话、提案、策略、成交、日账及执行状态绑定各自交易账户；没有授权或账户未装配时拒绝请求，不能回落另一用户的默认账户。

启用 `AI_TRADER_PAPER_ENGINE_URL` 的部署里，本站注册或快期登录的用户只开本站模拟，初始资金 100 万。快期模拟停用交易（所有者 2026-10-30，#1205）：快期账号只做身份与行情；`tqkq_retirement` 在每次启动时幂等收尾成员的快期账户（仍是行情源的只删交易授权、保留账户行与托管口令，其余整套解绑并停用），默认账户与会话挪到本站模拟；所有者的快期主账号保留行情与历史但没有下单网关，也不列入账户目录（会话仍停在它上面时照列）；所有者和本站账号用密码登录时同样每次落在自己的本站模拟上并设为默认（所有者 2026-10-31：快期只管行情）。“设置 → 账户”只提供切换，开通本站模拟只在开户失败后补开时用到。不启用引擎的部署保持其原开户路径。

本站模拟持仓只读响应（`GET /api/positions`，同样适用于美元子账户）在行情中断时仍返回 HTTP 200 和撮合账本仓位，附 `dataStatus`、`isCached`、`ageSeconds`、`snapshotAvailable`；价格与盈亏保留最近账本读数，降级状态必须一起展示，不代表当前估值。首次装配最多等 2.5 秒，超时 `items: []`、`dataStatus: unavailable`、`snapshotAvailable: false`，不代表确认空仓。引擎不可达或账户快照报错时只回最近成功快照；账户 SSE 的 `positionsStatus` 携带同一状态。此投影仅供展示，执行与恢复仍通过原 `positions()` 严格拒绝未知持仓，快期读取语义不变。

### 服务装配与请求归属

`wiring/accounts.py` 的 `AccountRegistry` 管理每账户的 provider、订单服务、自主执行、策略监控及账本；`account_pool.py` 与 `paper_accounts.py` 分别管理动态快期与本站模拟账户的装配、恢复和回收。静态账户从 `AI_TRADER_ACCOUNTS_FILE` 读取；配置删除的账户保留审计并标记停用，错误配置启动失败。

HTTP、AI、MCP/CLI、SSE 与语音共用账户解析。`resolve_account` 按显式账户、会话与用户授权选择落点；确认读取提案行账户，策略操作读取策略行账户。切换账户更新会话与 feed，后续回合按新账户取工具和上下文；在途事实仍归原账户。

### 角色与正式接口

AI 自主执行只允许 `store.is_execution_owner` 判定的执行负责人；operator 可确认自己的卡及操作被授权账户，viewer 只读。路由与服务复用相同判定，不能仅在界面隐藏按钮。停止、只停新开仓与急停均限制在当前交易账户，结果未知只查询原单。

- `GET /api/accounts`：当前用户被授权的账户与当前落点。
- `PUT /api/sessions/{session_id}/account`：切换到有授权且已装配的账户。每次（含重复同一账户）都发布该会话的 runtime.status，回复 `{sessionId, accountId, statusAt, accountGeneration}`，`statusAt` 是那条状态的时间戳；窗口等到不早于它的状态才认这次切换（#932 R3）。runtime.status 也带会话的 `accountGeneration`＝会话行的 `sessions.account_revision`（由 `EventBroker` 给每条 runtime.status 统一补上，含 agent 的首条状态；补时从同一行读出账户与 revision，状态里的账户已不是会话当前账户就判为过期、不发布；会话第一条状态到达前客户端的账户切换器置灰“账户状态加载中”、`tradingAccount` 命令 rejected，此后每次切换都带 `expectedGeneration`，只有从不报该字段的旧 Runtime 走无条件路径）（迁移加列，默认 0；蓝绿两个槽位共用一份）；请求带 `expectedGeneration` 时先只读比对（不一致直接 409，不解析也不装配目标账户），再用一条条件更新 `UPDATE … SET trading_account_id, account_revision = account_revision + 1 WHERE id AND user_id AND account_revision = expected` 落定，没更新到就 409 `account_switch_superseded` 不切换；带该字段的每次成功切换（含重复同一账户）都 +1——断线或蓝绿切换时留在路上的旧切换晚到也不会把会话写回旧账户。不带该字段的旧客户端照旧，真正换了账户仍 +1（解绑改绑也 +1）；语音回合的归属校验（#523 R1c）只比会话绑定的账户是否还是开始时那个，不读 revision，重复切到同一账户不会让进行中的回答作废（#932 后续，#1000/#1003 审查）。客户端按会话记见过的最大值（`accountRevision.ts` 是唯一判定处：revision 低于已知值的晚到 runtime.status 整条丢弃，不换账户、不更新头部；不带该字段的旧 Runtime 状态照收），收到 409 不自动重试，按 409 带回的会话当前账户与 revision（与条件更新同一次读出；经 `accountRevision.ts` 判定不旧于已采信的）立刻把当前账户与请求头切到服务端的账户，直到更新的 runtime.status 到来，并提示用户；“账户有没有变”取自切换事务里被替换的值，不用事先读的值。
- `POST /api/accounts/bind-tqkq`、`POST /api/accounts/unbind-tqkq`：已下线（#1205），只为旧桌面客户端回 410。
- `/api/runtime/status` 与 SSE `runtime.status`：独立报告账户、行情、网关、模型及风险状态。

账户改名和授权对账由 `wiring/accounts.py` 的现有事务实现；不迁移交易事实或推测持仓。详细操作见内置帮助 `accounts` / `paper-account` 与 [外部指南](../skills/ai-trader/SKILL.md)。

### 同一工作区的账户分级（2026-09-27）

游客是一种免手工登录的短期身份；前端统一使用 `FuturesDecisionWorkspace`，删除独立游客页和样式。`GuestEntry` 在装配私人服务前解析身份，`useGuestAccount` 只提供公共行情/新闻和窄语音，普通账户仍使用原认证与私人业务服务。登录成功更换身份适配层，取消保持浏览状态，退出登录回到游客；共享行情源不会成为访问者的交易身份。

权限按“身份边界 → 全局技能开关 → 档位技能 → 资源额度 → 当前业务安全状态”执行。`plans.py` 和现有 `skill_registry` 是服务端事实源，测试档与后续会员仍是配置行，不再为每个等级建立页面或散落等级判断。工作区 `useAccountAccess` 统一入口反馈：普通功能入口始终可见，游客受限时提示登录，已登录用户未开通时说明档位限制；全局停用说明停用。忙碌、缺少连接、行情过旧与订单结果不确定等业务安全条件继续按原逻辑阻止执行。UI只是反馈和避免无谓装配，直接API、CLI、AI调用仍受服务端约束。

| 能力/资源 | 游客 | 已登录测试/会员 |
| --- | --- | --- |
| 实时报价与图表 | 共用行情源，最近3个交易日内且最多源已有500根 | 共用同一行情源，保留现有范围 |
| 历史/回放/回测 | 不补下载，不创建回放或回测；点击提示登录 | 原技能档位与历史读取链路 |
| 新闻、研究与本机图表/外观 | 公共缓存与本机展示设置 | 同一组件及正式业务入口 |
| AI、私人数据、模拟下单/平仓 | 不装配私人服务、不可调用 | 档位/技能及本人身份，原幂等/新鲜度/价格/持仓检查 |
| 语音 | 窄界面意图，单次15秒、档位/IP额度及全局voice-call开关 | 原模型与语音额度及技能 |

游客档 `marketHistoryTradingDays` 默认为3，可在现有后台档位表选1/3/5；旧配置缺失或非法值回退3，null不能变无限。按K线正式交易日（含夜盘/日线）裁剪，与最新实际bar对齐，休市不按墙上时钟清空。原始共享包保留，源版本＋限额只生成一份裁剪/编码，慢读者与限额变化得到受限完整快照；游客行情限额全局缓存1秒，避免每读者每tick读库。活跃SSE最长约16秒看到档位调整。没有扩展上游下载窗口，也不承诺小周期完整三天都有数据。用户主要收益是杜绝游客启动深历史、AI和私人服务，不能据此承诺实时SDK负载按人数等比例下降。

普通档位本轮只接受 `marketHistoryTradingDays=null`，既有basic/pro/test配置不改。要开放普通会员历史天数，必须在共享feed、HTTP、AI工具、规则回测和回放创建/恢复/读取这些正式入口同时执行，不能只给前端或部分接口加限制。后续模型、技能、token/语音额度沿现有档位配置；付费价格、商业会员权益和到期策略尚未指定，本轮不自造。增加资源字段时仍落在同一Plan，先完整执行再在后台开放。

### 公共行情：体验优先与按规模扩展（2026-09-26）

本节保存本轮研究与设计取舍。产品当前以模拟盘为主，第一优先级是行情及时呈现、切换品种和周期流畅；下单、撤单和平仓的可靠性是必须保留的底线。不能以“交易线程没有受影响”代替行情体验验收，也不能用缓存旧行情掩盖延迟或断线。

**当前边界。** 游客与已登录成员的页面合约目录、报价、图表，以及纯行情 AI/CLI 能力，共用公共 data-only 会话（2026-10-04 起为多账号接力：有序源清单存 `market_sources` 表，首启用 `AI_TRADER_SHARED_MARKET_ACCOUNT_IDS` 播种——优先于旧单账号变量 `AI_TRADER_SHARED_MARKET_ACCOUNT_ID` / `AI_TRADER_GUEST_MARKET_ACCOUNT_ID`——之后以库为准，管理员 `PUT /api/admin/market-sources` 在线维护；每源 48 组 K 线上限、通道创建时固定到源，满/故障时向下一健康源接力；2026-10-13 起新通道分散：按清单顺序每源先放到 16 组（`AI_TRADER_MARKET_SOURCE_SPREAD`，设 48 即回到“先填满第一个源”），之后放到负载最低的健康源，让图表分到多个号池子进程、用上多个核；某源接近 16 组时先预热清单里下一个空闲源（号池子进程登录、目录），行情专用账号排在最后，仍是前面的源快放满时才启动；2026-10-14 起源还在载入的冷序列或刚换源的通道，读者共享同一图表的一次载入、最多等 20 秒首帧，已在服务的图表从换源那一刻起继续给上一份并标 `stale`，从换源起算超时才按失败处理）。用户已选择 `lzy393705181` 并确认可对外展示。共享行情独立于游客开关，读取它不会把访问者变成源账户的用户，也不要求先连接访问者本人的快期账户。资金、持仓、订单、策略执行与下单前报价核验仍使用各自 `TqApi(TqKq(), auth=本人TqAuth)`；规则回测的深历史仍走原有独立 HistoryActor，本轮不改写交易内核。公共 worker 暂借该成员池进程，另有 TqSim 数据会话，原 TqKq 账户连接仍保留，因此并未消除每个交易账户的固定进程成本。代码、部署与开市时延验收分别记录，不因候选测试通过即声称线上已启用。

```text
快期行情 → 公共 data-only SDK 会话 → Runtime 同合约/周期的一份最新状态 → 游客 SSE / 普通用户 feed.market
              ↑ 异步加载与更新通知       ↑ 共用编码、慢连接直接取最新值

个人 TqKq 会话 ↔ 本人账户/委托/成交       深历史 → 独立 HistoryActor 与结果缓存
```

**新浪备用行情（2026-10-07）。** 接力链路的最后一环：只有清单里每个快期源都不可用——处在连接退避中，或最近一次读取（30 秒内）报告自己与快期断开（`public_market_disconnected` / `public_market_unavailable`，或总表/报价/目录为 `stale`）——时，`SharedMarketService` 才把报价、报价总表与图表 K 线改由新浪期货（`sina_market.py`：`hq.sinajs.cn` 批量报价、`InnerFuturesNewService` 分钟线/日线，3 分钟与周线由 1 分钟线/日线合成；走现有温和限速 `GENTLE`，报价 2 秒、K 线 10 秒至数分钟缓存）提供。没有近期观测的源先用目录读取问一次。休市时快期连着但没有新数据不算不可用。合约名、最小变动价位与交易时段沿用最近一次成功的快期目录（Runtime 启动后从未读到时退回部署默认 provider 的目录）。备用包统一标 `dataStatus: "backup"`、`source: "新浪（备用）"`，`runtime.status`/`GET /api/runtime/status`/healthz 在本来非正常时显示 `marketDataStatus: "backup"`（`marketDataSource` 写来源；经 `EventBroker.present_runtime_status` 统一套用，切换时重发）。报告断开的源在 30 秒观测内与连接退避的源同样被跳过：非绑定读取（总表/报价/目录）交给后面真正健康的源，绑定在断开源上的图表先释放原名额、取得健康源的空余名额再读（无名额就留在原源，不无槽读取，满额也不算全断），之后原源恢复也不抢回；退出备用后状态按实际服务的数据给：健康快期读取接管才回到 provider 自己的状态（`normal`）；有连通源但满额/冷启动、实际服务的是旧快期包时为 `stale`（`SharedMarketService.served_status`，直到健康读取），不报 `backup` 也不报 `normal`。新浪也失败时维持原来的不可用表现；48 组上限不变。安全不变量：备用数据只供看盘、图表与 AI 调研；下单与闪电下单、交易方案与决策上下文、直接止损与自主执行、保本、到价与异动提醒、策略监控、成交核验都读本人账户 provider 或只认 `normal`（夹具 `simulated`），`backup` 一律按不可用处理。部署默认开启（`AI_TRADER_DATA_MODE` 非 fake 时），`AI_TRADER_MARKET_BACKUP=off|sina` 覆盖；fake 与回放子 Runtime 默认关闭。

**为什么不能仅把所有请求接到一个原有账户上。** 锁定的 SDK 3.10.2 同步 `get_kline_serial()` 会在返回前等待初始化，单合约内部超时可达 85 秒；之后再给 `wait_update()` 设 0.5 秒截止时间，不能约束前面的等待。现有个人 sidecar 行情命令与交易命令共用线程，因此增加陌生合约会影响该线程。游客旧路径还有一秒缓存和每轮完成后再等一秒，两段等待可能叠加。共享是否更快取决于这些实现，不能简单归因于“多转发一层”。原客户端同样经过 Runtime；新的扇出成本需要测量，但共享可减少重复下载、序列处理和编码。

**本轮公共链路。** 实时会话用 `TqSim()` 只取行情，不登录个人 TqKq 交易账户。SDK 所属线程用 `create_task` 协程等待新 K 线；每次 `wait_update` 返回后按 SDK 的 `is_changing` 逐个检查报价、K 线与交易状态，变了才重新生成（2026-09-28 起；此前用 `register_update_notify` 通道，生产首个交易日发现它不触发，各序列在首次发布后冻结）；一个陌生序列未就绪，其他已就绪序列仍可更新。实时数据固定最新 256 根，支持六种周期。K 线增量渲染（2026-10-13）：worker 保留每个序列各列的副本，每跳用向量比较找出真正变了的行（通常只有形成中的最后一根，新 K 线时窗口上移一行），只重算这些行（同一套取 K 线与交易日规则，结果与整份渲染逐字段一致），每根 K 线记下最后变化的版本；每个视图只编码一次，按读者版本的增量另缓存，不再每个长轮询深拷贝再编码。同一合约/周期由 Runtime 维护一个取数任务，沿内部鉴权 HTTP 的版本长轮询获得更新（带上次的 `barsVersion` 时 sidecar 只回报价与变化的 K 线 `barsDelta`：变化的 K 线、窗口首根时间、根数与摘要，Runtime 按时间删旧、覆盖/追加后核对根数与摘要；版本对不上、换源、换代或核对失败都回到整份；旧 sidecar 不认这个参数、照旧回整份），再唤醒 SSE 读者；Runtime 退役通道断开长轮询后，sidecar 不再等满 `waitMs` 把结果写进已关的连接；没有行情变化时的心跳不作为新行情。等待与唤醒（2026-10-14）：sidecar 一把服务锁上按等待主题（每个合约/周期、报价总表、目录、日历）各挂一个条件变量（同 `queue.Queue` 一锁多条件），数据线程每个合并批次带上变了的主题只唤醒对应读者，健康、换代、关闭才唤醒全部；监督线程只被新兴趣唤醒；停着的长轮询最多每 1 秒醒一次查读者是否断开。行情 sidecar（含号池子进程）走 HTTP/1.1 keep-alive，Runtime 的连接池复用连接，空闲连接由客户端先关，写请求答完即关；本站模拟（paper）不变。只保存最新快照和必要差量，不为每个浏览者存一串历史 tick；慢读者跳过中间状态后取得完整快照。

**回收必须释放真实订阅。** SDK 没有公开的逐项实时退订 API，删除应用字典、取消监听协程都不等于网络退订。本轮用 30 秒兴趣租约控制占用：全部闲置就关闭 data-only 会话；部分闲置时预热仅含活跃组合的新会话，成功切换后关闭旧会话，失败保留旧源。普通活跃会话最多 48 组，冷加载同时最多 8 组，替换候选最多一个（仅占至多2个共享冷槽，替换期间总序列最多96组）；新请求超过预算明确等待/拒绝，不能无限排队。上述上限是资源保护值，**不是已证明的用户容量**。同进程不同线程只能隔离 SDK 会话和命令等待，不能隔离 Python GIL、进程崩溃或整机资源耗尽。

用户数决定连接与出网成本，**不同的合约/周期数**决定上游订阅和计算成本。例如 200 人同看一个组合可共用一份上游数据；20 人各看三个不同组合可能需要 60 组，超出当前单会话预算。新闻、深历史和指标计算也必须单独核算，不可把历史下载排在实时更新之前。游客目前默认最多最近3个交易日内且最多已有500根，尚无任意起止日期的公共历史接口。

| 规模特征（不是人数承诺） | 采用的结构 | 扩展触发证据 |
| --- | --- | --- |
| 起步期，活跃组合较少 | 本轮一个公共实时 worker，Runtime 共享最新状态，保留个人交易连接 | 正式入口压力测试与开市观察确定可承受范围 |
| 人数增加，仍集中看少数组合 | 先检查 SSE 扇出、出网带宽、编码与浏览器渲染；保持上游去重 | 热行情 p95/p99 延迟变差、事件循环积压或出网接近预算 |
| 不同组合增加或频繁切换 | 按合约稳定分配到少量有界行情进程；保留异步预热和回收 | 活跃组合逼近预算、冷加载排队或某进程计算时间持续升高 |
| 单机已无法满足延迟或可用性 | 独立行情服务与多个分发节点；同合约由一个采集者负责，分发仍只保留最新状态 | 持续测得单机 CPU/内存/网络或故障恢复不达标，再引入跨机协调 |

后面三行是扩展路线，尚未实现。当前不预先引入消息中间件或拆一套微服务。扩展保持公开字段、合约/周期键、版本与最新快照语义不变；游客与个人账户的权限界限不随部署规模改变。已登录成员图表复用相同只读入口，独立通知任务不等待私人账户轮询；每个会话的行情只保留最新状态，订单与对话可靠事件仍沿原链路。本人交易连接继续核验下单所需的新鲜报价、跳价、涨跌停及持仓。

**验收口径。** 分开记录上游行情时间、SDK 收到时间、Runtime 收到时间、SSE 收到时间与浏览器实际呈现时间；跨机器要核对时钟，同机处理耗时用单调时钟。看分位数和最差停顿，不能只看平均值。至少比较同组合多人、多组合多人、切换冷品种、慢客户端、来源中断恢复；同时核查模拟下单/平仓没有被公共浏览阻塞、超时结果没有被盲目重下。记录首帧/热更新 p50/p95/p99、冷加载耗时、活跃/实际保留订阅、CPU/RSS、出网与错误率。可先以同机 SDK→SSE 热更新 p95 ≤100ms、p99 ≤250ms 作为待验证目标，**不是目前测得的线上保证**；交易所→用户屏幕的总延迟还包含上游、网络和渲染。

离线受控更新只能证明本机链路开销、隔离与资源边界；休市行情不能证明开市行情时延。源码/接口验收结果与真实云端开市测量分别记录，不能用 fake 的低延迟声称真实行情已达到目标。

**本轮本机验收（2026-09-26）。** Mac Mini M4 / 16GB，真实 TCP 的 Sidecar HTTP → 正式 `PoolClient` → Runtime → SSE；源是可控公开行情 fixture，256 根 K 线、每次同时改变报价与末根 K 线。每档 15 秒，绝对节拍核对发布数；延迟从受控源发布到 SSE 接收，用同机单调时钟，受控 worker 未执行真实 SDK 下载与数据组装，因此数字不包含这些耗时、浏览器绘制或公网。101 个游客身份通过正式 bootstrap 创建，模拟 5 个可信代理来源 IP（每 IP ≤25 次创建）、每身份 ≤2 条流，未修改产品限额。

| 测试负载 | 实际发布频率 | 接收样本 | p95 / p99 |
| --- | --- | --- | --- |
| 50 条 SSE，共享一组 | 20 次/秒 | 15,000 | 12.8 / 13.7 ms |
| 200 条 SSE，共享一组 | 20 次/秒 | 60,000 | 23.1 / 69.5 ms |
| 200 条 SSE，分散 12 组 | 每组 5 次/秒 | 15,000 | 89.8 / 117.5 ms |

三档 HTTP/SSE 错误均为 0，接收数等于读者数乘发布数，分别只有 1 / 1 / 12 个共享取数任务和一个源 worker。冷组合延迟就绪、冷组合未就绪返回503、慢读者、无行情更新时的界面命令唤醒、全部断开后的任务与默认30秒源租约回收另有受影响验收；这些证明隔离和回收，不是全天候并发容量承诺。新增18项侧车、14项 Runtime 行为验证覆盖真实 renderer、SDK协程/日历预热、首帧取消/超时与身份区别、断线状态、受限恢复、公开权限、同组合共享和慢读者；既有66项侧车/池及40项游客/账户池结果复用。浏览器在本机新 Runtime 上验证首帧、切周期与品种，未扩大到未改的布局矩阵。

证据保存在工作站 `~/ai-trader-release-artifacts/news-guest-local/public-market-benchmark*` 与 `public-market-ui.txt`；临时发生器与测试进程验收后清理。上述压测只覆盖当时的游客链路；普通成员共享接入另做受影响行为与正式入口验收，不能直接把游客压测数值作为普通用户实测。真实云端开市延迟仍待开市测量。

研究依据：[SDK 3.10.2 源码](https://github.com/shinnytech/tqsdk-python/blob/3.10.2/tqsdk/api.py)、[官方协程多实例示例](https://doc.shinnytech.com/tqsdk/latest/advanced/multi_strategy.html)。独立模拟撮合的取舍见下一节；本轮不替换 TqKq 模拟交易后端。

### 本站模拟引擎

本站模拟已实现：`paper-engine` 是云端独立进程，`services/tqkq-sidecar/src/tqkq_readonly_sidecar/paper.py` 的单线程 actor 服务多个隔离账户，`paper_book.py` 复用官方 TqSdk `SimTrade` 的纯计算记账（理想模式下也用它撮合），不为每个用户连接一个快期账号。共享行情提供报价及合约元数据，账户资金、持仓、委托和成交来自自己的模拟账本。撮合行情的快期连接按账号接力（2026-10-12，`paper_sources.py`）：顺序就是公共行情源清单 `market_sources`，Runtime 租约持有者经引擎 Bearer 鉴权回环接口 `POST /v1/market-sources` 下发（账户池账号带 Runtime 账户库里加密保存的快期登录，主 sidecar 账号标 `local` 用引擎挂载的 secrets；口令只在引擎内存）；引擎同一时刻只连一个账号，认证失败立即换，连接失败/断线连续 3 次才换，不抢回；未下发前只用挂载账号。换号后的首包仍过防倒退水位；日历、结算价查询与合约资料刷新随当前连接。引擎 `/healthz` 的 `marketSource` 与 `GET /api/admin/market-sources` 的 `paperEngine` 显示当前账号、切换原因与各账号状态。合约名（2026-10-13 事故后）：读报价与下单送来的合约名在引擎一个入口（`_symbol_spelling`）规范成交易所写法——资料表与最近一次整表刷新列出的合约名（含资料取不全、没进表的）、已收到行情或账本持有的合约大小写不敏感匹配；只有本进程收到过、且不超过 25 小时的合约服务整表刷新才视为全部未到期期货（#842：重启后库里的旧表、刷新连续失败的旧表都不算），否则按交易所规则（上期/大商/能源/广期品种小写，郑商/中金大写）写好交给行情源订阅判断；完整目录下不存在的合约报价读取答 404 `symbol_not_found`（与只读 sidecar 同码），不进租约、不订阅；形状对但目录里没有的名字（可能是刷新后才挂牌的新合约）会让行情源提前刷新资料表，至多每小时一次。订阅时合约服务仍答“合约不存在”的，只把该合约剔出订阅集 30 分钟（`/healthz` 的 `rejectedSymbols`），同一账号重连一次，不记失败、不切账号（只认这条连接正在订阅的合约，报错点名别的名字仍按连接故障）；断线、认证等真实连接故障仍按上面的接力规则。主 sidecar 与号池公共行情订阅前已按合约目录解析写法、单合约失败按合约隔离，不受同类问题影响。

引擎以 SQLite 持久化账户、命令和成交，Runtime 的 `PaperAccountManager` 先认领唯一账户 id，再幂等开户、登记授权和默认账户；重复请求或中断恢复沿用同一 id。模拟网关复用 `SimulationOrderService`，保持权限、发送前检查、幂等及 `UNKNOWN` 原单回读。引擎的 `paper_commands` 按 `commandId` 幂等并记命令类型（submit/cancel）：同 id 重放只回读原单，换另一类命令重放返回 409 `command_kind_mismatch`。kind 列加入前的旧行在引擎启动迁移时按已存原始请求一次性回填类型（#527 D4，不改写 request_json），判不出的标 `legacy`：重放只按原单只读返回当前状态，原单不存在或撤单遇活单同样 `command_kind_mismatch`，不新下单、不撤单。

引擎落盘是增量的（2026-10-12，压测发现每条命令重写整本快照、单户当日成交越多全站越慢）：每个账户事务只写当前状态行 `paper_account_heads`（资金、持仓、活单、行情，覆盖一行）、本次新增的成交行 `paper_trades`（账户内序号追加）、终态委托行 `paper_orders`（带账本日 `book_day`）与命令行，写入量与当日已有成交笔数无关；命令结果与状态仍同一事务、提交后才回复。整本快照 `paper_accounts.state_json` 降为检查点，格式不变，只在开户、日结（同一事务，此刻当日成交最少）、正常停机和空闲时定期（有新提交满 10 分钟，每 30 秒最多合并一户）写。恢复 = 当前状态行 + 序号起点之后的成交行，之后照旧跑账本自检。`activity` 在撮合锁内只取活单、账本日和成交序号起点并定下 WAL 读快照，当日委托与成交在锁外按索引读，读期间撮合照常提交。旧库首次启动把整本快照转成当前状态行 + 成交行（全部账户一个事务，失败则引擎不启动、快照列不动，`paper_migration_failed` 写明账户与原因）；`checkpoint_of` 记检查点对应的版本，回滚后旧版本写过的账户（它不认识这一列）再升级时以它的快照为准。停机（#527/#510）：一开始就不再接纳、也不再执行命令，正在执行的照常答完，队列里没执行的与之后到的都当场答 `paper_engine_stopping`（没执行，不等 4 秒超时）；随后合并检查点并逐户核对 revision = 当前状态行版本 = `checkpoint_of`，没追平（actor 未在时限内退出、合并失败或核对不过）时进程以 4 退出、日志写明。回滚：检查点追平后旧版本直接读快照列即最新状态；`deploy-release.sh` 回滚在起旧版本引擎之前，用本次版本的镜像对停下的库跑 `python -m tqkq_readonly_sidecar.paper --checkpoint-db <库>`（合并并逐户核对，不过关非 0），不过关就报 `ROLLBACK_PAPER_CHECKPOINT_FAILED`、整个回滚判失败，不在落后快照上起旧引擎；手工回滚同样先跑它。部署前可在生产库副本上跑 `--check-db <副本>` 预演迁移、恢复、自检与逐户比对，正常停机后检查点仍落后判不一致。

`paper_book.py` 在撮合前校验交易时间、tick、涨跌停、到期及持仓等边界；交割月禁开按品种分（2026-10-09 起）：股指 IF/IH/IC/IM 现金交割可开当月合约，商品期货与中金所国债期货（自然人无国债托管账户申报）交割月禁开、平仓不受限；缺交割年月时同样拒开仓（#527 B2c）。合约资料（#527 B2）由引擎 SQLite 的 `paper_contracts` 表承担，取值只在引擎行情入口一处（`resolve_contract_metadata`）：实时行情的有效值优先并回写表（值不变时每合约每天只确认一次）→ 表里的值 → 都没有才拒单并说明原因；字段为合约类型、品种、乘数、最小变动价位、到期时间、交割年月、每手保证金/手续费，静态与费用各记来源（quote / contract_service / snapshot）和更新时间。表在行情源启动时刷新一次、之后每天 20:00（结算后、夜盘前）一次：`query_quotes(ins_class="FUTURE", expired=False)` + 一次批量 `query_symbol_info`；TqSdk 3.10.2 的 `query_symbol_info` 表格不带 margin/commission，但同一次合约服务应答（future 片段）带，按 SDK 自己的 `_symbols_to_quotes` 从缓存应答里取，取不到时靠实时行情回写补齐。刷新失败（取数或写表）保留旧表、健康 `contracts.refreshError` 说明、一小时后重试，不影响交易；到期合约不进表（已在表里的到期满 30 天才清理）。每手保证金/手续费超过 3 个交易日未被确认（两项同时到才算确认）时照用，但下单结果 `contractSource.feeStale` 与健康 `contracts.staleFeeRows` 标注；`contractSource` 随委托存进账本，查询与幂等重放重建的回执同样带（#777）。旧库首次开库时用各账本快照里的行情补表（来源 snapshot）。仍缺保证金/手续费/乘数的合约只更新价格、不交 SimTrade（`apply_quote` 守卫）。重启后的防倒退水位取各账本快照里的最新行情时间，比它旧的迟到行情整笔丢弃；水位不进引擎行情缓存，历史快照不冒充实时行情。恢复自检覆盖账户资金字段与持仓保证金/盈亏/成本、手数与冻结（#527 F3）以及今/昨分桶（每桶手数与冻结为有限非负数、冻结不超本桶、两桶之和等于合计，#777），不过关的账本拒绝上线。理想模式下，下单瞬间即可成交的限价单按对手方最优价成交（买单按卖一、卖单按买一，不劣于自身限价，委托记录保留用户限价；挂单被动成交仍按挂单价——2026-10-08 起，对齐交易所连续竞价口径，TqSdk SimTrade 原生一律按自身限价）。开仓成本按昨/今两桶记账（2026-10-09 起），桶内按持仓明细先开先平（2026-10-17 起，#932，与期货公司 CTP 逐笔持仓明细同口径）：每笔开仓成交记一条明细（`open_lots_{side}_today/_his` = [开仓价, 手数]），平仓从被平的那桶扣最早的明细，开仓成本、开仓均价（展示、浮盈、保本“移到成本”与合并卡成本都读它）都从明细推导；开仓均价＝剩余明细开仓价的加权均价，结算时今仓明细按原顺序接到昨仓之后，只把持仓价标到结算价、不改开仓均价——SimTrade 原生平仓一律按全持仓混合均价扣减 open_cost，昨仓成本会污染今仓开仓均价。明细的写者证明（2026-10-19 #932）：快照顶层 `open_lots_trade_seq` 记写明细时账户的持久成交序号 `{from: trades_from_seq, seq: from + 当日成交笔数}`（跨日单调、只随成交增长），恢复时 seq 与 from + 快照当日成交数不等、from 与库里已提交的起点不等或缺键就丢弃全部明细，下一笔成交或结算翻桶按桶成本重建一条；所有旧版本的 `export_state` 都按固定键重建顶层、不保留它，所以回滚到明细之前的版本又升级回来时，只要旧版本写过这个账户（哪怕平了又开让手数与桶成本恰好复原），明细都不会被沿用。本版本自己重启（含跨结算）证明成立，先开先平次序不丢；丢弃明细不改成交、开仓均价、桶成本与资金。桶成本键 `open_cost_{side}_today/_his` 是明细之和的镜像，回滚到明细之前的版本时旧代码仍按它读出桶成本；没有明细或明细与桶对不上的快照（2026-10-17 前的快照、回滚后又升级）按桶重建成一条明细（价＝桶均价），开仓均价不变、不回改历史成交。快期模拟账户由快期服务端记账，不在此列：它的开仓均价按全部持仓混合扣减，日内开平今仓会改变剩余昨仓的开仓均价，对账时不比两边的开仓均价。持仓成本同处按两桶记（2026-10-12 起）：平仓盈亏按交易所逐日盯市口径——平今对被平那几笔明细的开仓价、平昨对昨结算价，上期所/能源中心按 CLOSE/CLOSETODAY 指令扣对应桶，其他交易所与 SimTrade 实际扣减一致先昨后今；持仓盈亏＝各桶按当前价相对各自成本。SimTrade 原生按混合持仓价算的平仓盈亏与持仓盈亏只差同一个数，成交钩子把它从持仓盈亏挪回平仓盈亏，权益与可用资金不动；结算后两桶随 SimTrade 重置到结算价（分桶键清掉，下一笔成交按今 0、昨＝全部惰性重建），上线前被混合扣减带偏的昨仓成本到下一次结算自然收敛，历史日记录不回改。每日结算按交易所正式结算价（2026-10-08 起，与真实期货账户同口径；目标日与来源约束 2026-10-11 收敛，#527）：结算价约 19:00 公布，引擎在 18:00 换日后推迟结算、行情源每 5 分钟主动 `query_symbol_settlement` 补取（查询结果带所属交易日，优先级高于新交易时段行情携带的 settlement/pre_settlement；同日低优先级来源不得覆盖查询结果）。结算只认目标日（新账本日按日历对应的上一个有效交易日）的正式结算价，更早日期的价格不提前推进结算；收盘价兜底是 PR647 已发布设计，所有者 2026-10-11 亲自选择改为只等交易所正式结算价、未取得前暂停新单（settlementSources 只记 "settlement"）——目标价未到就继续 pending。到期合约的最终结算日 = 最后交易日（合约资料到期时间 `expire_datetime` 所在日，所有者 2026-10-15 决定，#527）：普通目标日恰是最后交易日时与日常结算同一口径（等该日正式价、整户推迟），按原到期平仓语义强平。跨停机越过到期日（普通目标日已晚于最后交易日）时只挂起这一个合约：该次结算把它标记到持仓均价（不确认盈亏、不强平），账户其余合约照常结算、照常下单（已到期合约自身的委托本来就被拒）；行情源按日期补查 `query_symbol_settlement(合约, days=1, start_dt=最后交易日)`，查到就按原到期语义强平；挂起满一个交易日、当天补查（查询失败也算）仍不到，按最后可得价格暂估强平——最后交易日及以前最近的正式结算价与挂起记录里的最新真实价（挂起时取引擎行情缓存与账本行情中较新的一笔，挂起中每收到更新的真实行情就单调更新并随账本同一事务落盘，记价格、交易日与行情时间）二者中较新的：先比日期、同日结算价优先、真实行情之间按行情时间，价格高低不参与比较（2026-10-17，#928 复核：同日后到的低价不再被先记的高价压过，暂估前重启不丢挂起中收到的新价；之前写下的记录只有日期，同日排在带时间的依据之前）。日结日志 `expirySettlements`、成交 `expirySettlement`（Runtime 活动原样透传）与引擎账户读接口 `expirySettlements`（Runtime 账户摘要 `/api/account/summary` 原样透传，持仓面板与 AI 读得到）标“暂估，待核”；之后查到正式价按每条腿（正式价 − 暂估价）× 手数 × 乘数补一笔进当日平仓盈亏，带开仓费率的腿按平昨一栏补手续费差，标“已核”。挂起记录在引擎 SQLite 的 `paper_expiry_settlements`（每户每合约一行，状态 pending → settled / estimated → adjusted），与账本同一事务推进，重复恢复不会重复强平或补差；旧版本不认识这张表、照常开库（pending 的账户回到旧的整户等待，补差等回到新版本再做），`--check-db` 解析这些记录（坏记录开库即失败）并核对 pending 的合约仍有持仓。结算完成前的下单被拒（`paper_settlement_pending`），保证委托晚于结算入账、不被结算的 GFD 撤单波及。主动查询返回的 symbol 是交易所内合约代码（如 `rb2701`），引擎按请求的完整代码映射回去再取价，已带交易所前缀的原样认，映射不唯一或不在请求里的行丢弃并记日志（2026-10-12）。到期强平单带上开仓成交时记在持仓该方向上的 feeRate（最近一笔开仓覆盖，整方向一个口径，不分批次），按平昨一栏收平仓费；没有记录的旧快照沿用行情每手手续费，日结 `expiryFeeSources` 标明口径（2026-10-12，#643）。行情源的交易日历建连时初取一次，前沿不足 7 天且从未尝试续期时立即发起；每次发起（成功或失败）后按一小时节流，失败保留旧日历。无持仓的账户没有可等的结算价，换日后直接日结（2026-10-09 起）：清当日盈亏/费用、结转静态权益、写上一交易日日报、失效当日剩余 GFD 挂单。策略预算由用户定义，不新增平台默认风险限额。日结、费用和恢复以引擎及其账本为准；一档快照只能估算排队，不承诺模拟成交与实盘一致。TqSdk 调用与许可核对沿用 Sidecar 的官方技能，见 [AGENTS.md](../AGENTS.md)。

成交模型（2026-10-13，所有者批准，`paper_fill.py`）：引擎默认“真实模式”，`AI_TRADER_PAPER_FILL_MODE=ideal` 退回“理想模式”（SimTrade 原行为：价格满足即按下单时刻的对手价整笔成交，供对比）。真实模式关掉 SimTrade 自己的撮合（`_match_order` 门控），由账本外层撮合决定每一笔成交，再把它作为子单（同委托号、手数＝本笔、按比例分到冻结保证金；平仓单沿用母单下单时占好的持仓冻结）交给 SimTrade 的整笔成交记账——持仓、保证金、手续费（feeRate 与昨/今分段）、平仓盈亏与昨/今成本分桶都走原路径；母单保留原始手数，剩余与冻结同步扣减，之后的整笔成交/撤单/日终撤销按“手数＝剩余”的影子单记账。成交编号沿用“委托号|成交前剩余手数”。快期免费行情只有一档盘口与累计成交量，模型三部分：① 时间差：委托在下单后 `latency_ms`（默认 300）到达，按到达后的下一笔行情判断；该合约行情静止超过 `arrival_wait_ms`（默认 500）按缓存盘口到达（缓存盘口旧于引擎行情过期阈值 15 秒时不用，等下一笔新行情——重启或断流时不按几分钟前的盘口成交；每笔行情之后与真实时钟空闲时兜底，虚拟时钟只随行情推进，回放确定）——借鉴 vn.py `vnpy_paperaccount`（MIT）关闭 instant_trade 时“等下一笔 tick 撮合”的行为。② 吃单：对手价在限价以内的部分先吃对手一档量按对手价成交，超出部分每多吃一档量多滑 `impact_ticks`（默认 1）跳，直到限价；限价以外的剩余挂在限价上排队（交易所限价单“能成交部分成交、其余挂单”的语义）；同账户同一笔行情内多张单共享这笔行情的盘口量。合成档位与盘口消耗借鉴 Nautilus Trader 的 TwoTier/ThreeTierFillModel 与 `liquidity_consumption`（LGPL-3.0，只借思路不含代码）。③ 排队：委托价是同方向一档时排在一档量之后，价差里为 0，更深的档看不到则等它成为一档再按当时一档量排队尾；之后最新价恰为委托价时，累计成交量增量 × `queue_share`（默认 0.5，保守系数）先消耗队前量，超出部分成交（同账户同价位多张挂单在同一笔行情里共享这份量；不足一手的余量记为负的队前量带到下一笔，不丢）；该档量变小时队前量封顶到当前一档量（hftbacktest `RiskAdverseQueueModel`，MIT；Nautilus `queue_position` 同一规则：成交推进、盘口更新只封顶不后移）；对手价恰等于委托价按对手一档量成交，最新价或对手价越过委托价视为本价位被吃穿，剩余全部按委托价成交。排队的累计成交量基准不跨重启（下一笔行情重新定基准），未落盘的队列推进在重启后丢失（偏保守）；同一笔行情的已耗盘口量（合约 + 行情时间 → 各价位本账户已成交手数，快照键 `fill_consumed`）随当前状态行与成交行在同一事务落盘、重启恢复（#834）：行情源重连推来与停机前完全相同的首包时不再供应已吃掉的量；同一行情时间、盘口量变化的行情与这笔共用预算，只补增加的量；新行情时间重新供应；只留行情时间仍是账本最新行情的合约。#834 之前写的快照没有这一项，按恢复的那笔行情盘口全部用尽处理，新行情时间到来前这笔行情整笔不撮合（不吃单、不按对价/队列成交，已到达挂单也不走穿价分支），还没到达的委托也不按这笔行情到达（到达了只会被当成挂单），等新行情按吃单到达（偏保守）。该键不改快照版本号，82a441df 及更早版本读到会忽略（回滚后回到“重启丢预算”的旧行为，账目不受影响）；进程被杀时未提交的成交与已耗量一起丢失，重放同一笔行情按第一次的结果成交一次。落盘只在到达、排上队与成交时写，不逐笔行情写。多个账户各自是独立的模拟世界，互不消耗对方看到的盘口量。到期强平临时打开 SimTrade 撮合，按结算价当场整笔成交。账本快照有部分成交的活单时写版本 2（其余仍写 1）：不认识部分成交的旧版本拒绝恢复这类账户（引擎停机报错），不会按原始手数撤单记账；回滚到 #833 之前的版本前先撤掉部分成交的挂单。每笔成交行带 `fill` 对账字段（随 `paper_trades`、日结 `paper_trade_log` 与 activity 输出）：`model`、`liquidity`（taker/maker）、`decisionAt`/`decisionPrice`（下单时对手一档价，无对手盘用最新价）、`arrivalAt`/`arrivalPrice`、`fillPrice`、`slippageTicks`（相对决策价的不利跳数，负为改善）、`impactTicks`、`queueAheadAtJoin`、`queueWaitMs`（到达→成交）、`partial`；`/healthz` 的 `fillModel` 给出模式与参数，供每日与快期模拟逐笔对比调参。规则回测（`strategy_backtest.py`）的 `slippageTicks` 是“每次成交按 K 线价固定滑几跳”（规则回测默认 0、策略实验室默认 1），其滑点来自 K 线价、本模型的滑点来自一档价差、盘口量与排队，两者口径不同、不共用参数；对比时把回测的 1 跳近似看成“对价吃单 + 一跳冲击”的上限。

强平（2026-10-30，所有者定，#1204）：引擎像期货公司一样在交易时段内盯每个账户的风险度（保证金 ÷ 权益，SimTrade 口径），到 100% 时先撤该账户全部活单，再按市价从保证金占用最大的持仓平起，只平到风险度低于 100% 所需的手数（权益不为正时全平）。强平单 `clientOrderId` 以 `paper-force-` 开头，成交行带 `forcedLiquidation: true`，照常进 activity；Runtime 的来源入口 `strategy_workbench.order_sources` 把这个前缀读成 `liquidation`（成交提醒、复盘、记忆提炼与成交页回合来源同一处，回合来源以强平平仓为准，与止损同理）。这是模拟真实账户的结算行为，不是平台风控规则。重置命令 `POST /accounts/{id}/reset`（body `confirmAccountId` 须与路径一致）撤活单、清持仓、权益回到初始资金，保留成交历史，重置记录与账本同一事务；Runtime 只经管理员路由 `POST /api/admin/paper-accounts/{id}/reset` 调用。

**外盘本站模拟（外盘二期，2026-10-19）。** 同一引擎进程里起第二个实例撮合外盘（`paper_foreign.py`；`AI_TRADER_PAPER_FOREIGN_PORT` 19012、库 `/data/paper-foreign.sqlite3`），引擎与账本代码不分叉，只换配置：引擎时区美东（`PaperEngine(timezone=…)`，交易日 18:00 换日即 CME Globex 交易日，国内实例默认北京时间不变），CME 时段表的夜盘段锚定交易日前一自然日（`nightAnchor: eve`），日历是工作日去掉全天休市日；行情、合约资料来自 MT5 只读行情桥（`Mt5PaperSource` 每秒轮询订阅集，合约名 `CME.<MT5 月份>`，乘数 tickValue/tickSize、保证金按交易所隔夜标准——`ExchangeMargins` 每 12 小时读 AMP 公开保证金表的交易所维持保证金（“CQG Symbol”列即 MT5 根代码），存 `foreign-margins.json`、读失败沿用上一份，表里没有才退回 MT5 的 marginInitial（AMP 日内保证金）、手续费每手每边 2.5 美元），理想成交（MT5 只有一档盘口）；结算价优先用交易所正式结算价：`Mt5PaperSource` 盘中记 MT5 `session_price_settlement`，本交易日内从开盘时的值变成的新值即当日正式结算价（10-20 实测 CME 公布后一小时内更新）；没看到变化（未更新、引擎中途重启）退回结算日美东 17:00 前最后一根 1 分钟线收盘价近似（`settlement_target_day` 给出目标日，经 `feed_settlements` 进引擎，到期合约同法）。外盘实例起不来或故障只它自己不可用，不触发整进程重启。Runtime 侧美元子账户是同一 `paper` 类账户（`paperusd:` 编号前缀、`AI_TRADER_PAPER_FOREIGN_ENGINE_URL`），`PaperAccountManager` 随人民币本站模拟账户开户与补开；其行情面 `ForeignPaperMarket` 的合约目录是 `foreign_market.trade_catalog()`（主力月份与外盘报价行同一选法），图表读同品种外盘图表。外盘单的唯一入口在出卡工具与规划器（`TraderReadOnlyTools.call_order_card` / `plan_order`）：说的是外盘品种或窗口开着外盘图表时整单交给美元子账户的工具规划，经 `AccountRegistry.foreign_account` 按需装配；下单前估算路由同样转过去。国内费率表对外盘合约返回空（`effective_fee_schedule`），由引擎按行情里的每手美元手续费扣。持仓读数的合并（2026-10-21）：`merge_paper_account_status` 给人民币本站模拟账户的状态账户块写 `foreignAccountId`（`AccountRegistry.foreign_account_of`，只读编号、不装配）；`feed.account.foreign` 推送美元子账户的摘要、持仓状态与委托（按同一个父子关系与授权读取，按账户与用户共享原有 5/15 秒读取预算，帧复用每轮缓存及 `feed.touch`）；客户端 `useForeignHalf` 仅在推送不可用或子账户编号不匹配时带美元账户头按原 5/15 秒读取，撤单后仍立即重读，并进同一持仓面板（每行带 `accountId`/`currency`，汇总只算人民币，撤单发到行自己的账户）；内置 AI 的 `get_positions` / `get_account_activity` 在人民币账户上附 `foreignAccount`。两个账户仍各自记账，不合并资金。

**外盘策略与回测（外盘三期，2026-10-20，所有者定方案 A）。** 合约格式统一在 `instruments.py`（`CONTRACT_MONTH_PATTERN` 同时认国内 `2701` 与 MT5 `Z26` 月份，`product_scope` / `strategy_scope_symbol` 把 `CME.GCEZ26`、`KQD.m@COMEX.GC` 归到品种 `CME.GCE`），策略范围、规则与模板的校验都引用它。账户归属只有一个入口 `wiring/context.py` 的 `account_for_symbol`：外盘可交易合约在非美元账户上经 `services.accounts.foreign_account` 换成美元子账户上下文，策略校验、图表、回测、试参数与模板实例化都经它；AI 启动策略时按策略绑定的 `tradingAccountId` 取美元子账户的执行器。图表符号经 `strategy_workbench.concrete_symbol` 换成主力月份。分钟到小时回测读 `ForeignMarketService.history`、实时判定读 `ForeignMarketService.workbench`（都是这个月份合约自己的 MT5 报价与 K 线，经 `ForeignPaperMarket` 的 `market_history` / `market_workbench`；历史上限 `MT5_MAX_BARS` 1 万根），每根带 `tradingDay`（`trading_calendar.cme_trading_day`，美东 18:00 换日，休市日表 `CME_CLOSED_DAYS` 写到 2027 年底）；日线仍走快期外盘主连的 K 线，但报价与新鲜度取这个月份的 MT5 实时报价（快期外盘报价几分钟才更新一次，过不了策略 15 秒的新鲜度核对）。时段表 `cme_schedule(at, hours)` 带 `dayRule: "cme"` 与 `cmeHours`（品种目录 `ForeignProduct.hours`：globex 23 小时、grains CBOT 谷物两段、livestock 瘦肉猪白天一段，表在 `trading_calendar.CME_HOURS`，来源 CME Group 合约细则 2026-10-21），`sessions_of_day` 按每个交易日自己的美东偏移重算（跨夏令时切换的历史也对），`session_position` 据此按 CME 交易日判断收盘前平仓与是否在时段内；只有能在美元子账户交易的外盘主连在合约目录里换成 CME 时段（伦铜、美棉等照旧）。外盘回测费率由 `trading_costs.backtest_fee_schedule` 取行情里的 `commissionPerLot`（取不到时零费率并在结果卡写明未扣，不套用户的国内费率）；试参数比较建档时就把图表主连换成月份合约。

**外盘图表快照的 CME 口径（外盘盘点 D，2026-10-21）。** 可交易外盘主连（`shared_market.cme_listed`）的每个图表周期都经共享行情生产者 `produce_market` 里的一处 `foreign_market.cme_chart`：`quote.tradingSchedule` 是这个品种的 `cme_schedule`（HH:MM，带 `dayRule: "cme"` 与 `cmeHours`，`trading_calendar.cme_rule` 是规整时保留它们的唯一一处），`marketSessionStatus` 按 `trading_calendar.cme_session(t, hours)`（盘中休息 intraday、每日停盘 break、周末、CME 节假日 holiday——`CME_CLOSED_DAYS` 没有交易日的日子与 `CME_HOLIDAY_HOURS` 节日前后交易日的特别时段，按 CME Group 节假日时段表逐品种组核对，2026-10-21 尾项；休市带 `sessionClosedFor` / `sessionReopensAt`；时段内 MT5 报价超过 30 分钟也算休市）；MT5 不通、分钟到小时线退回快期外盘主连时，`cme_chart` 把 K 线的 `tradingDay` 改成 CME 交易日（这类通道的推送补丁按整窗比较），日线（2026-10-21 尾项起）也由 MT5 主力月份画：`Mt5ForeignSource` 把小时线（整段半小时读一次、其间只读最后 48 根）按 `cme_trading_day` 合成，快期外盘主连的日线按国内日历切、MT5 的 D1 按 UTC 日切都不能用；MT5 首读一个久未读的月份会先给终端本地的旧历史（末根停在几小时前），`_syncing` 认出它时这一轮退回快期、下一轮再读 MT5。周线（快期外盘主连 K 线）的报价与新鲜度换成 `ForeignMarketService.chart_quote`——外盘报价行此刻的 MT5 主力月份（与分钟图、外盘撮合同一份）。这类通道的长轮询缩到 `foreign_wait_ms`（2 秒），快期 revision 不变的轮次也按上一份视图重算一次，CME 状态与 MT5 报价照样推到工作台、推送与 `GET /api/market/{symbol}`。客户端不再有自己的 CME 时钟，只读快照。操作记录 `agent_activity.with_foreign_half`（HTTP 与推送共用）在人民币本站模拟账户的外盘图表上并入美元子账户同一品种的委托、成交与保护单；操作记录卡给成交配盈亏读的 `/api/trade-review?symbol=<外盘合约>` 经 `strategy_workbench.with_foreign_review` 并入子账户同一品种的回合，子账户解析两处共用 `agent_activity.foreign_half`。

### Runtime 模块布局（1.5.0）

```text
services/trader-runtime/src/trader_runtime/
  main.py            create_app / create_replay_app 入口（约 200 行）
  wiring/            config（参数与 fail-closed 校验）、services（存储/broker/行情/委托/候选/自治/监控/研究数据）、
                     agent（模型循环适配器）、auth、feed（推送读取）、lifespan（启停顺序）、context（RuntimeContext）
  routes/            system / sessions / account / execution / strategies / replays / research，按原注册顺序 register(app, ctx)
  store/             base（连接、锁、迁移）+ sessions / strategies / proposals / accounts / trades / instances 领域 mixin，RuntimeStore 门面
  runtime_feed.py    推送循环：feed.account / market / activity / execution / ui，只在变化时发送
  instance_lease.py  实例租约：同一状态库只有一个实例跑止损/策略/记账循环与后台生产者（wiring/producers.py），standby 可接管
  native_agent.py    原生模型循环（OpenAI Responses）；trader_tools.py 进程内只读工具
  order_boundaries.py 委托前置检查（报价新鲜、tick、有限正数）供自治服务与委托服务共用
  research_data.py   AKShare 研究数据（下节）
```

### 统一数据采集（2026-10-18 起分步迁移，#976）

所有者要求行情与新闻采集走同一套接口与模块。职责分三层：数据源函数（各 `research_*` / `sina_*` 模块里的取数与解析）只取回并解析；`collection.Collector` 是唯一的“读一次、大家共用”实现——同键单飞、调用方有界等待（等不到时取数继续跑、本键退避，后来者加入它而不并起第二个）、成功按 TTL / 失败或空结果按重试间隔、好值经 `LastGood` 存 `research_live_cache`（重启与另一槽位读回，失败永不覆盖好值、只标 stale；带 `partial` 的好值重启后也按重试间隔算到期）；按值判新鲜度的读者可给 `epoch`（研究数据给交易日），失败退避只在发起那次取数的 epoch 内有效、交易日换代立即可取；`close()` 后不再起新取数，仍在跑的取数结束时也不写库（`AsyncCollector` 同样关闭即停，研究简报的新闻搜索随 lifespan 关闭）；使用方只读数据集。一个 `Collector` 是一条独立车道（自己的线程池），慢网站只拖住自己那条。后台轮询的启停仍归租约持有者，快讯入库归 `research_live`，快期实时订阅、号池接力与本站模拟撮合不纳入，只统一输出契约；采集结果只是证据，交易入口仍按既有规则核对合约、时效与字段。已迁移：利率、生猪现货价（AKShare 源调用各在一次性线程上、按来源单飞：超时后仍在跑的那次由下一次读取接着等，不另起线程）、外盘行情、研究数据（`research_data.py` 六项），以及异步来源——调研台的慢数据源（统计局、CFTC、FRED、EIA、上期所周报、宏观、机构观点，`research_desk._slow_source`）与研究简报的新闻搜索（`research_brief`）改走 `collection.AsyncCollector`（同一套规则的事件循环版：同键一次取数、读者被取消不影响取数、取数到时限即取消、失败可带各自重试间隔）。交易日历只留一个来源：研究数据与调研台也读快期日历（第 6 步，删除 AKShare 交易日历读取）。手续费表（openctp，后台每日一抓、读路径从不联网、两槽经 state_root 文件共享）与快期日历本身（交易判断热路径，按行情连接分别缓存、事件循环上从不阻塞）已符合统一规则，不迁。快期全挂时的新浪国内备用（`sina_market.SinaBackup`，第 7 步）也走 `AsyncCollector`、只存内存：同时到期的合约合成一次报价请求（`AsyncCollector.get_many`：还新鲜的直接给、别人正在取的加入那次请求、其余到期的合成一次），K 线按合约与周期各一键；新浪失败时照旧答不可用、不拿旧价当新价，重试间隔内（报价 2 秒、K 线 5 秒）不再请求，10 分钟没人读的合约从存储里清掉。刻意不迁：快讯轮询与财经日历（租约持有者的后台抓取 + 合并入库，不是“读一个值”）、联网搜索与简报原文核验（每次问题不同，必须取新结果）。外盘的唯一入口是 `foreign_market.py`（品种与各源代码在 `instruments.py`；几秒级报价的上次好值只存本进程内存，不写 `research_live_cache`，重启或换槽后从下一次读取起算）：报价总表“外盘”分组（`GET /api/market/foreign`）、AI 的 `get_foreign_markets` 与调研“外盘联动”读同一份；数据源按 `AI_TRADER_FOREIGN_SOURCES`（默认 `sina,kqd`；生产 2026-10-19 起 `mt5,sina,kqd`）排序，现有新浪外盘、MT5 与快期外盘主连三个源，每个品种整行取自最前一个有价的源、不跨源拼字段，快期 `KQD.m@…` 主连经共享行情补充报价通道读（没有昨结算，涨跌为空）；MT5 源（`Mt5ForeignSource`）经 4090 上的只读行情桥 `services/mt5-bridge`（反向隧道到生产机回环 11210，容器经 compose 的 `mt5-tunnel` 中继读 `AI_TRADER_MT5_BRIDGE_URL`，Bearer 令牌走 Docker secret）读取，每个品种用主力月份合约（未到期且离到期超过 3 天的最近 3 个月份里持仓量、其次成交量最大者，周末全为 0 时沿用上次的、没有则取离到期超过 30 天的最近月份），整行（价、买卖、成交量、持仓量、结算价作昨结算）都来自该合约，行上 `contract` 写合约名；合约表半小时一读（返回里没有任何目录月份的空表不算读到、不替换已有的表，1 分钟后再读；冷启动就空时该源报 `mt5_contracts_empty`、换下一个源）、报价一次批量请求、4 秒超时，桥不可用即换下一个源；桥对单个合约选不中或读不到报价时行内 `error` 区分 `terminal_read_failed`（MT5 IPC 失败）与 `unknown_symbol` / `no_quote`，`detail` 带 MT5 的 `last_error`；昨结算为负数时 `changePercent` 为空、`change` 照算；某品种有源覆盖却从没取到（也没有上次的行）记 `partial.missing`，missing 只算已配置数据源能提供的品种；按品种读（调研、AI）时关联外盘一行都没有为 unavailable、部分没有列 `missing`，整表读取（含全部来源都没给到的冷启动）也列 `missing`；研究数据的失败退避按交易日（epoch）作废时，读者加入的若是换代前的取数，它结束后同一次等待内再起新交易日的取数；只供看盘与调研，不进下单、止损、提醒与成交核验。外盘图表（所有者 2026-10-19 方案 A）：图表合约是快期外盘主连代码，目录里快期的海外主连只保留 `instruments.py` 有的品种（`shared_market.foreign_listed`）；共享行情读这类合约 1 分钟到 1 小时的图表包时先问 `ForeignMarketService.chart`——MT5 排在快期之前时由 MT5 主力月份合约（与外盘行同一个月份）出报价与 K 线（3 分由 1 分、30 分由 15 分合成），包形同 sidecar 的工作台包、`source` 写“MT5 · 合约”，每 2 秒一读；日线周线、MT5 排在后或出错时整张图走快期主连，出错后该合约 60 秒内不再试 MT5，避免一张图在两个来源间来回切。迁移顺序与待迁项见 #976。

### 研究数据（AKShare）

`research_data.py` 把六类天勤免费行情没有的研究数据按品种聚合：交易所会员持仓排名、仓单日报、现货价与基差、库存、期限结构、保证金 / 手续费参考。`GET /api/research/summary?symbol=` 一次返回六项，`GET /api/research/{dataset}?symbol=` 取单项，路由只读并进入能力目录。每项独立：`status ok|stale|unavailable`、`asOf`、`source`、`error`；读取经统一采集协调器（`collection.Collector`，车道 `research`，2026-10-18 #976 第 4 步）：键为数据集 + scope（持仓排名与保证金/手续费按合约，其余按品种），同键单飞、一次汇总的六项同时开抓、共用一个等待上限；上次好值存 `research_live_cache`（前缀 `research:`，重启与另一槽位读回），失败保留好值标 stale、10 分钟内不再抓（交易日换代时立即重取，不等退避）；新鲜度由值本身判定（跨交易日即过期，17:00 后当日取到的值管到当天，其余 60 分钟）；整表来源按交易所/品种/日期共享 10 分钟，多个月份只抓一次。旧表 `research_cache` 启动时把好值导入（新表没有或比它旧的才导入：回滚后旧版继续写旧表，再升级时取它较新的值）、新版不再写（2026-10-03 前按品种存的两类旧行升级时仍先删除），留着供回滚到旧版本读取。抓取在一次性线程上执行、单项 30 秒超时（九期网手续费表常要 7–25 秒）、超时后放弃不等待（AKShare 多数请求无超时）。“最近交易日”按 Runtime 唯一的交易日历取（快期日历，与下单、过期、交割提醒同一来源 `exchange_calendar.calendar_trading_days`，2026-10-18 #976 第 6 步起不再另读 AKShare 新浪日历；快期日历覆盖前 60 天到后 45 天，范围外与读不到日历时只跳周末），所以中秋、国庆等工作日休市不会被当成交易日。上期所 / 上期能源仓单不再走 AKShare（它读的 JSON 文件 2026-09 起 404），直接解析交易所官网日报页 `stockdata/dailystock_<日期>/ZH/all.html`，同品种仓库与厂库两表合计。大商所 2026 年给数据接口加了反爬虫校验（HTTP 412 + JavaScript 质询），不绕过：大商所期限结构改用新浪各月合约价（`futures_zh_realtime`），仓单用东方财富注册仓单总量（`futures_inventory_em`，无分仓库明细），会员持仓排名无免费替代来源，直接报 `research_source_blocked`、不再请求大商所。广期所期限结构照常经 `get_roll_yield_bar` 读交易所日行情（工业硅、碳酸锂、多晶硅）；AKShare 品种表里没有的铂、钯报 `research_product_not_in_source`。akshare 只在 `research` extra 并延迟导入，云端镜像安装、桌面打包的本机 Runtime 不含（报 `research_dependency_missing`）。研究数据不进入行情、委托、风控与手续费设置的任何路径；保证金 / 手续费只是参考值。

## 6. 可复现回测与策略快照

`BacktestService` 固化策略快照（release）和合成数据集，使用隔离子进程运行确定性引擎。manifest 保存引擎版本、策略/数据/参数/结果摘要、来源与时间；相同输入复用相同内容寻址制品，但每次运行仍记录独立的用户/会话归属。worker 无任何账户、TqSdk 或订单能力。

桌面官方历史回测复用同一个 `BacktestService`、`BacktestCoordinator`、`backtest_runs` 和内容寻址目录，不增加第二套回测数据库。实际历史撮合在 TqKq Sidecar 的独立线程内创建第二个 `TqApi(TqSim, TqBacktest)`；它只用账号做历史数据鉴权，不使用远端交易账户。官方结果与完整 K 线副本回到 Runtime 后再次校验摘要，再保存为研究制品。

REST、Fake Agent 与模型工具都把请求/run ID 绑定到 `BacktestCoordinator` lease。工具调用在独立异步任务中等待 worker；取消时先停止对应用户的 worker lease、释放全局/用户配额，再排空工具任务并关闭模型流 并等待工具线程退出。任务只有在结果持久化和卡片发布后才进入 completed。

```text
回测请求
  → StrategyReleaseManager 计算策略源文件 SHA-256
  → 工程区外 strategy-runtime/releases/sha256-* 只读快照（strategy_releases 表）
  → 回测记录（backtest_runs）以 strategyReleaseId 引用该快照
```

内容相同会复用同一个快照；源文件后续变化会生成新摘要，旧快照从不覆盖。快照只供回测复现，不会被部署或执行；回测结果仅作研究依据。

Release manifest 记录首次写入的 `artifactName`，因此相同字节来自不同策略文件名时仍复用同一 digest 目录。

2026-09-25 起旧的“部署候选 / 模拟部署”链路已删除：不再有 `proposal.deployment` 事件、部署候选卡和 `strategy_deployments` 表。已有数据库里该表为空时启动即删除，非空则保留并记日志；`strategy-runtime/deployments` 目录同理（空则删）。`POST /api/backtests/{id}/deployment-proposal` 的 410 空壳已于 2026-09-30 删除（客户端早已不调用）。

## 7. 确定性安全边界

```text
Agent 创建候选
  → Runtime 从可信 RiskPolicy / InstrumentSpec 读取限制、合约乘数和最小价位
  → Decimal 计算预计亏损并按 tick 对齐
  → 超限：前端禁用 + 服务端 409
  → 用户要求按上限调整，Runtime 递增版本并签发短期 approvalId
  → 客户端提交 approvalId + proposalVersion + idempotencyKey
  → Runtime 在同一事务内验证归属、状态、版本、有效期、一次性凭证和幂等性
  → Runtime 重新读取报价、tick、涨跌停和可信风险数据
  → 写入唯一 tqkq_sim_order_links(PREPARED)
  → 条件更新 CALL_STARTED 后事务外调用一次 sidecar
```

手工下单与方案卡按当前确认设置执行；用户明确要求 AI 执行模拟操作时，Runtime 自动建立内部租约，每次调用仍独立校验授权和账户归属。两条路径最终进入同一确定性检查与单次网关调用链。模拟账户没有平台默认风险限额，Agent 不代客创建风险规则候选；用户策略预算仍然生效。

图表点价只把按合约 Tick 对齐的价格写入候选输入，不触发交易；Runtime 必须再次校验价格为有限正数、落在 Tick 上且未越过涨跌停。行情、账户或持仓状态不是当前可用状态时，客户端按钮禁用，服务端仍独立失败关闭。盘口深度按数据源实际字段展示，缺失不能补成虚构买卖档；来源与能力声明仍由数据适配器负责。

交易确认复用 Proposal、幂等键、审计和 outbox。`PREPARED` 重启后不自动提交；`CALL_STARTED` 重启后转 `UNKNOWN`；`UNKNOWN` 只查询原 `clientOrderId`，not-found 也不重派。止损用 `ARMED -> TRIGGERING` 条件更新占有一次触发权，并在调用网关前持久化平仓 `clientOrderId`，重启后只能回读原单。急停原子撤销授权、取消未触发止损并逐张撤销 AI 自己的活动委托，不自动平仓。TqKq 是订单、成交和持仓事实源；没有真实账户、强平、追价撤单重报或关闭风控接口，云端执行始终关闭。详见 [AI 自主模拟交易与语音会话 v1](AI_AUTONOMY_AND_VOICE_V1.md)。

下单耗时打点（2026-09-27）：每次提交在 Runtime 日志写一行 `order_timing client_order_id=… checks_ms=…（报价/价格/风控检查与 CALL_STARTED 写入）gateway_ms=…（到本账户 Sidecar 的往返）sidecar_queued_ms=…（在 TqSdk actor 前排队）sidecar_exchange_ms=…（insert 到交易所回报）status=…`，确认接口另写 `order_confirm_timing proposal=… server_ms=…`；成交时刻看 `account_trades.trade_date_time`。读取：`sudo docker logs ai-trader-production-trader-runtime-<slot>-1 2>&1 | grep order_timing`。Sidecar actor 每轮读行情后的 0.1 秒空等改为等命令队列，委托入队即处理；读行情的最长 0.25 秒等待也切成 0.05 秒一段，每段后看命令队列，有委托或读报价就提前结束（生产休市实测：经 actor 的报价读取 p50 143 ms、最长 255 ms，而 Runtime→Sidecar 的 HTTP 往返仅 1.4 ms）。无命令时仍等满 0.25 秒，快照构建频率不变。

## 8. 认证与凭据

- `npm run dev` 随机生成开发令牌；正式客户端连接云端，模型、快期和语音密钥只在服务端。
- 网页身份使用 Secure HttpOnly Cookie；桌面签名会话及 MCP 连接信息保存在当前用户私有目录，令牌不进入仓库、URL 或日志。
- API 与长连接复用认证、用户/账户授权和来源检查。客户端数据缓存不能代替服务端授权；云端 CORS 不默认信任 localhost。
- Runtime 与 Sidecar 的内部端口和令牌按部署文档隔离；数据库、备份、密钥及正式 updater 私钥不进入工程根目录。
- `cloud-jwt` 支持 OIDC Discovery/JWKS 的 RS256/ES256 资源令牌验证；`development-hs256` 仅用于 localhost Compose，不是正式 OIDC。 格式无效的 OIDC 令牌（包括深层嵌套 header）按认证失败受控拒绝为 401，调用方需重新认证，不暴露内部 500。cloud-jwt 用户（`cloud-<sha>`）没有成员行，档位按默认档，不落到所有者的无限档。

## 9. 外部部署适配点

- Runtime 的 Python `RunnableAdapter` 协议由 Fake 与原生模型适配器实现；客户端通过 Runtime 接口使用模型。
- Runtime data providers：隔离 sidecar 已完成；真实 TqKq 模拟委托烟测需要用户明确授权，并只允许 1 手低成交概率限价后撤单。
- Backtest runner：确定性合成引擎已完成；官方 TqSdk 历史回测与规则回测并存，数据来源、时间范围、费用和精度随制品记录。
- Persistence：本地/容器 SQLite 已按用户约束查询；生产多副本部署需托管数据库、对象存储、备份和迁移流程。
- Tauri shell：窗口生命周期、签名会话、macOS 签名自动更新与 Developer ID 签名/公证已完成；Windows 商业代码签名和平台沙箱复核仍待完成。
- Voice Bridge：服务端实时语音与按住说话复用同一会话、业务工具和确认边界；回复可流式播报，语音层不另建交易逻辑。
- Cloud identity：Runtime 支持 OIDC Discovery/JWKS 的 RS256/ES256 资源令牌验证，并继续把短期令牌交换为 HttpOnly Cookie；登录页、授权码流程、令牌签发和租户管理由外部身份提供方负责。`development-hs256` 只用于 localhost Compose 验证，不能作为正式 OIDC 的替代。
- Cloud delivery：Runtime 容器使用非 root 用户、只读根文件系统、capability drop 与健康检查；生产前端暴露 `version.json` 供构建提交回读。CI 会实际启动 Compose 并跑 API 隔离和 7 视口页面验证。
- Three-platform delivery：macOS、Windows 与网页端共用 React/事件契约；网页候选固定构建为 `/trader/` PWA scope，前端站点与云端 Runtime（腾讯云上海主机）分离。CI 单独回读网页资源基路径、PWA scope、提交版本和 `liveTrading=disabled`，但不会自动部署。
- Desktop delivery：Tauri 目标包含 `.app`、`.dmg` 与 Windows NSIS `.exe` 安装/更新载荷。Windows Runner 只生成由一次性密钥签名的候选载荷，发布者在 Mac 上用固定 updater 私钥重新签名；国内主源与阿里云 OSS 镜像的同版静态 HTTPS 清单必须同时包含 `darwin-aarch64` 和 `windows-x86_64`。应用使用内置公钥验签并管理自己的更新重启；私钥仅存在于发布者本机非仓库目录。面向普通用户的 macOS 发行已在 main 的 desktop 作业内完成 Developer ID 签名与公证；Windows 商业代码签名仍待完成。
- CI execution：私有仓库的 通用检查使用 GitHub 标准 `ubuntu-24.04`，Windows 桌面候选在常开的 4090D 自建 Runner（`windows-ci`）上构建，安装包留在该机由发布脚本经 SSH 取回；Mac/Linux/Windows 都用仓库级自托管 Runner。Mac Runner 匹配 `e2e` 与 `desktop` 标签，Linux Runner 只匹配 `linux-ci` 标签且单任务串行。所有持久 Runner Job 拒绝 Fork 和非 Owner PR；Runner 不持有 TqKq、账户或交易凭据，也不构成 CloudAgentRunner/TenantExecutionBoundary。

# CI runner placement

- `verify-mac` (Runtime, sidecar and workspace Python tests) runs on the background `mac-mini-cc` host (label `e2e`). Browser e2e is run locally when the affected change requires it.
- `cloud-compose` (docker compose stack smoke, main only) moves the sole `linux-ci` label from `ai-trader-linux-nas` to the background Mac Colima Linux runner after this PR's main acceptance. Until then NAS remains assigned; actual state is recorded in shared `servers.md`. Both remain separate from the Tencent production Runtime. Rollback removes the label from Mac and restores it on this repository's NAS runner, keeping exactly one online match; it requires Docker and targets `linux/amd64`.
- `desktop` builds on the background `mac-mini-cc`; `verify-linux` uses standard GitHub-hosted `ubuntu-24.04`, and `windows-desktop` runs on the always-on self-hosted 4090D (`windows-ci`, since 2026-10-15) and keeps its installer set on that host for `release:all` to fetch over SSH. Interactive development and final updater signing remain on the old workstation.

普通用户补充验收（2026-09-26）：200 个受控 SSE 读者、同组合 256 根、20Hz、12 秒；其中一个读者每 300ms 消费一次。修正每会话重复校验/编码后，同一公共版本只验证并编码一次，199 个快读者收到 47,760/47,760 条，p95/p99 为 43.0/46.4ms，源节拍偏差 p99 为 1.98ms；慢读者取得最新完整状态。此项直接消费 broker 的真实 JSON SSE，包含同进程消费开销，不含 TCP、真实 SDK 或浏览器，不能代替开市端到端时延。正式云服务器隔离只读进程用指定账号已读到 936 个合约、螺纹 5 分钟与沪金 15 分钟各 256 根真实 K 线；休市，不以此宣称开市性能。云端冷目录首次实际 5.7 秒，超过 5 秒有界等待时改回已有 catalogStatus=loading，页面沿原流程自动重试；未就绪 K 线返回503而非未知合约404。
