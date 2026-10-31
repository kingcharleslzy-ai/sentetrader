# docs 索引

按用途分三类。冲突时以“权威架构与边界”为准，功能契约只约束各自功能，历史记录不指导当前实现。

## 权威架构与边界（持续维护）

| 文档 | 内容 |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Runtime、模型循环与工作区、数据 Provider、模型池与账号、确定性安全边界、云端部署适配点 |
| [ENGINEERING_DISCIPLINE.md](ENGINEERING_DISCIPLINE.md) | 复用边界、PR 流程、测试层级、停止条件、发布失败处理 |
| [FUTURES_AGENT_CAPABILITY_V1.md](FUTURES_AGENT_CAPABILITY_V1.md) | Agent 工具分层、决策链、新鲜度门、`web_search`、MCP/CLI 入口、UI 控制接口 |
| [AI_AUTONOMY_AND_VOICE_V1.md](AI_AUTONOMY_AND_VOICE_V1.md) | 执行租约、AI 受控开仓/撤单/平仓/止损、急停语义、语音边界 |
| [AI_STRATEGY_WORKFLOW_V1.md](AI_STRATEGY_WORKFLOW_V1.md) | 策略草案 → 验证 → 意图 → 监控 → 暂停的生命周期与保护器 |
| [AI_NATIVE_FUTURES_WORKSPACE_V1.md](AI_NATIVE_FUTURES_WORKSPACE_V1.md) | 交易台/复盘/回放工作区、下单区与右键、行情能力声明 |
| [THREE_PLATFORM_AND_WEB_DEPLOYMENT.md](THREE_PLATFORM_AND_WEB_DEPLOYMENT.md) | macOS/Windows/网页拓扑、云端 Runtime、更新源与存储维护、上线门禁 |
| [RUNTIME_MIGRATION.md](RUNTIME_MIGRATION.md) | 云端 Runtime 换主机运行手册：`deploy/vps/migrate-to-host.sh` 各阶段、密钥 / SQLite 搬运、证书、DNS 切换与回滚、多表面回读 |
| [AGENT_SKILL_REGISTRY_V1.md](AGENT_SKILL_REGISTRY_V1.md) | 技能登记、启停、档位开放与接口归属 |

研究数据（AKShare 六类数据集、缓存与失败语义、仅云端模式）见 [ARCHITECTURE.md §5](ARCHITECTURE.md) 与 Runtime 帮助目录 `research` 一节；Runtime 模块拆分（`store/`、`wiring/`、`routes/`）与 feed 推送同在 ARCHITECTURE。

## 功能验收契约（边界有效，界面描述以当前版本为准）

| 文档 | 功能 |
| --- | --- |
| [TQKQ_SIM_THIN_V1.md](TQKQ_SIM_THIN_V1.md) | 单笔模拟委托一次提交协议、`tqkq_sim_order_links` 状态机、重启与 UNKNOWN 规则 |
| [MANUAL_CLOSE_V1.md](MANUAL_CLOSE_V1.md) | 手工平仓，平今/平昨由用户显式选择 |
| [SIMULATION_TICKET_V1.md](SIMULATION_TICKET_V1.md) | 模拟开仓候选与确认链 |
| [MARKET_WORKBENCH_V1.md](MARKET_WORKBENCH_V1.md) | 只读行情与 K 线、Sidecar 线程边界 |
| [ACCOUNT_ACTIVITY_V1.md](ACCOUNT_ACTIVITY_V1.md) | 账户持仓、委托、成交只读工作台 |
| [RISK_ALERTS_V1.md](RISK_ALERTS_V1.md) | 风险预算展示与服务端到价提醒 |
| [TQSDK_HISTORICAL_BACKTEST.md](TQSDK_HISTORICAL_BACKTEST.md) | 官方 TqBacktest + TqSim 回测研究制品 |
| [HISTORICAL_REPLAY_ACCEPTANCE.md](HISTORICAL_REPLAY_ACCEPTANCE.md) | 交互式历史回放的验收证据与未覆盖边界 |

## 历史决策记录（只作溯源）

| 文档 | 内容 |
| --- | --- |
| [HISTORICAL_EXECUTION_PR_INVENTORY.md](HISTORICAL_EXECUTION_PR_INVENTORY.md) | PR #12–#15 的历史处置与可复用故障场景 |
| [AI_TRADING_MATURE_TOOL_REVIEW_2026-09-04.md](AI_TRADING_MATURE_TOOL_REVIEW_2026-09-04.md) | LEAN、Hummingbot、Nautilus、Freqtrade、vn.py、Qlib、TradingAgents 的取舍依据 |
| [layout-prototype-2026-09-04/](layout-prototype-2026-09-04/) | 2026-09-04 认可的交易台布局原型源码与设计 QA，已在 1.2.4–1.2.8 落地；1.5.0 的顶栏合约标签与常驻底栏取代了其中的右侧抽屉方案 |
| [UX_WALKTHROUGH_2026-09-13.md](UX_WALKTHROUGH_2026-09-13.md) | 1.5.0 前的交易台走查：问题清单、改动候选与各轮结果（阶段五第一至八轮） |

已退役的阶段快照和原型来源记录从 Git 历史读取；当前操作说明在内置帮助，外部 AI 操作在 [skills/ai-trader/SKILL.md](../skills/ai-trader/SKILL.md)。
