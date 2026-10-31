# TqKq 单笔模拟委托器 v1

> 状态：本文是 PR #16 落地时的 v1 范围记录，“一次提交协议”“重启与未知结果”两节至今是权威执行语义。范围一节中的“不包含平仓 / 止损 / 策略自动执行、Agent 没有执行工具”描述已被后续版本扩展：手工平仓见 [MANUAL_CLOSE_V1](MANUAL_CLOSE_V1.md)，AI 在执行租约内的受控开仓、撤单、平仓与确定性止损见 [AI_AUTONOMY_AND_VOICE_V1](AI_AUTONOMY_AND_VOICE_V1.md)。所有扩展都复用本文的单表状态机与 UNKNOWN 不重发规则。快期模拟停用交易（所有者 2026-10-30，#1205）：配置了本站模拟盘的部署里快期账户不再有下单网关，委托与撤单回 `simulation_gateway_disabled`，下单都走本站模拟引擎；本文的提交协议仍是本站模拟引擎（同一张 `tqkq_sim_order_links`）与没有本站模拟盘部署的执行语义。

## 范围

本实现只允许本地桌面在用户明确确认后发送一张快期模拟 LIMIT 开仓委托：`buy_open` 或 `sell_open`。TqKq 是订单、成交和持仓的唯一外部事实源；SQLite 只保存 Proposal 与远端订单的关联和最近一次查询报告。

明确不包含平仓、平今/平昨、多腿、多 Attempt、自动重试、自动改价、自动止损、策略自动执行、云端执行、`TargetPosTask`、`TqAccount`、`TqCtp` 或实盘账户。`liveTrading=disabled` 始终成立。

## 复用与新增

- 复用 Proposal 审批、RiskPolicy、`idempotency_keys`、`proposal_audit`、EventBroker/outbox 和现有单 TqApi sidecar 线程。
- TqSdk 固定为 3.10.2，sidecar 只创建 `TqApi(TqKq())`。
- 只新增 `tqkq_sim_order_links` 一张表；不创建 Execution Journal、OrderPlan、OrderLeg、OrderAttempt、Shadow 或 Owner Epoch。
- Agent 动态工具（2026-10-16 起为 `propose_order`，收下单意图，见 `docs/ORDER_INTENT_V2.md`）不接收委托价以外的执行参数。Runtime 读取当前报价、tick、涨跌停、合约乘数和风险规则后生成候选；Agent 没有 submit、query、cancel、close 或 stop 工具。

## 一次提交协议

```text
用户确认 Proposal
  -> 同一 SQLite 事务写入 tqkq_sim_order_links(PREPARED)
  -> 事务提交
  -> 条件更新 PREPARED -> CALL_STARTED
  -> 事务外调用一次 sidecar submit_order_once
  -> sidecar 以 clientOrderId 作为 TqSdk order_id
  -> wait_update 等远端证据
  -> Runtime 保存最近报告
```

Runtime 只有在条件更新 `PREPARED -> CALL_STARTED` 成功时才会调用 sidecar。Sidecar 在调用 TqSdk 前先记录进程内 `commandId` 和 `clientOrderId`，同一进程中的重复命令只查询原订单。HTTP 客户端没有自动重试。

本地 `insert_order` 返回对象中的预填充字段不能作为远端确认。只有 `wait_update` 后出现在线、终结、拒绝、交易所委托号或成交证据时才返回订单；证据不足返回 `UNKNOWN`。

## 重启与未知结果

| 本地阶段 | 重启行为 | 允许操作 |
|---|---|---|
| `PREPARED` | 保持待发送，不自动提交 | 用户明确继续或放弃 |
| `CALL_STARTED` | 启动时转 `UNKNOWN` | 只查询原 `clientOrderId` |
| `UNKNOWN` | 等 `ordersInitialized=true` 后查询 | 只重新查询 |
| `CONFIRMED` | 以 TqKq 查询报告刷新 | ALIVE 可查询或撤单 |

`ordersInitialized=false` 时的 not-found 不构成订单不存在。即使初始化完成后仍 not-found，也只记录结果，不自动重派。用户若要重新交易，必须创建新候选、新审批和新 `clientOrderId`。

撤单同样只调用一次。撤单响应丢失后 `cancel_phase=UNKNOWN`，页面只提供重新查询；若撤单和成交竞态中 TqKq 返回全部成交，最终显示已成交，不伪装为撤销成功。

Runtime 对查询结果校验 `clientOrderId / symbol / direction / offset / priceType / volume`。任何不一致记录为 `IDENTITY_MISMATCH`，停止自动推进。

## 验证矩阵

自动化黑盒覆盖：未确认不调用、重复确认单 clientOrderId、PREPARED 崩溃、CALL_STARTED 崩溃、提交响应丢失、UNKNOWN 禁重派、重启查询恢复、订单历史未初始化、撤单响应丢失、撤单与成交竞态、远端身份不一致、陈旧行情和非 tick 价格。

真实烟测不是 CI 的一部分，也不会从仓库读取凭据。仅在交易时段、用户明确授权且能选择合法但尽量不会成交的价格时执行：1 手确认 -> 查询 ALIVE -> 撤单 -> 查询 FINISHED/撤销 -> 重启后再查询。若无法安全保证“不故意制造模拟持仓”，则不执行真实烟测。

## 官方依据

- [TqSdk 3.10.2](https://pypi.org/project/tqsdk/3.10.2/)
- [TqSdk 官方交易文档](https://doc.shinnytech.com/tqsdk/latest/usage/trade.html)
- [TqApi.insert_order / get_order / cancel_order / wait_update](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.api.html)
- [TqKq 快期模拟账户](https://doc.shinnytech.com/tqsdk/latest/reference/tqsdk.tqkq.html)
