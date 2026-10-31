# 只读行情工作台 v1

## 用户可观察行为

1. 从期货合约目录选择一个未到期的实际交割合约；选择本身不发送 AI 消息，也不调用交易接口。
2. 点击“查看当前行情”后，显示最新价、涨跌幅、买卖一档、成交量、持仓量与 K 线。
3. 支持 1 分、3 分、5 分、15 分、1 小时和日线；真实实时序列最多返回最近 256 根。
4. 只刷新当前打开的合约；应用进入后台时暂停请求，回到前台后继续。
5. 交易中行情陈旧或读取失败时保留上次快照并明确标记，不用 Fixture 填补真实 TqKq 数据。
6. 午休或休市时明确显示“休市 · 最新行情”，不会把已连接且未发生业务变化的账户误报为陈旧；候选、价格提醒和模拟委托继续禁用。
7. `liveTrading=disabled`，本功能没有委托、撤单、平仓或自动执行入口。

## 官方接口与线程边界

- TqSdk 固定为 3.10.2。
- 交割合约来自 `query_quotes(ins_class="FUTURE", expired=False)`；请求的合约必须存在于该目录。
- 报价使用 `get_quote(symbol)`，K 线使用 `get_kline_serial(symbol, duration_seconds, data_length=256)`。
- 使用 `is_serial_ready()` 判断 K 线初始化完成，后续更新由既有 `wait_update()` 循环推进。
- `wait_update(False)` 仅表示截止时间前没有业务字段变化；连接健康由 TqSdk 网络通知判断，交易时段优先由公开 `get_trading_status()` 判断，无权限时回退到公开 `Quote.trading_time` 合约交易时段。
- 每个 `Quote` 和 K 线 DataFrame 只保留在所属的 TqApi 线程；HTTP 只返回脱离活动对象的 JSON 快照。
- 个人交易会话仍最多保留 48 个“合约 + 周期”序列、96 个动态报价；策略/执行侧订阅满额且休市空闲时才重建会话回收。历史回测使用独立 HistoryActor，不能计入实时容量。
- 游客与普通用户页面行情共用独立 data-only 会话，在 SDK 协程内等待冷序列、每次 wait_update 后按 is_changing 维护热序列（2026-09-28 起，原更新通知通道在生产上不触发）；同组合共享 Runtime 取数任务和编码，SSE 不再以每游客一秒轮询触发更新。内部 `/v1/public-market/*` 沿用 Sidecar 鉴权，游客仍走 `/api/guest/*`，普通身份保留 `/api/instruments`、`/api/market/{symbol}`、`/api/markets/{symbol}/workbench` 与 `feed.market` 正式契约；纯行情 AI/CLI 使用同一来源。普通行情推送独立于私人账户轮询，慢端直接恢复最新完整图表。声明 `marketPatches` 的客户端在整份 `feed.market`（带 `revision`）之后只收 `feed.market-patch`（相对 `base` 的变化，约 1 KB），对不上就重新关注拿整份（2026-09-27）。浏览器本地保留看过的图表（`marketCache.ts` + `localCache.ts`，2026-10-12 起 IndexedDB，经 `idb` 7.1.1 封装；按 Runtime 地址 + 合约 + 周期分键，只存已收盘的 K 线，形成中的最后一根永不入库；只存 `dataStatus` 为 `normal`/`simulated` 且非缓存的数据；总量约 8 MB，按最近使用淘汰）：打开页面、切合约或切周期时先画上次的图表并标成 `dataStatus: stale` + `isCached: true`（下单台显示“不可下单”，Runtime 仍独立校验每笔委托的报价），实时行情到达即替换；回放不读也不写。增量读取：`GET /api/markets/{symbol}/workbench?duration=&since=<本机最后一根已收盘 K 线的 datetime>` 只回 `since` 之后的 K 线，另带 `barsAfter`（即 since）与 `barsWindow` {first 窗口第一根时间, count 窗口总根数, digest 窗口内截至 since 的 K 线摘要}；摘要是 CRC-32（十六进制）——每根 K 线按“秒级时间, 开高低收/成交量/持仓量 ×10000 后 floor(x+0.5), 交易日”拼成文本，`,` 分字段、`;` 分根，Runtime（`shared_market.bars_digest`）与客户端（`barsDigest`）同算。客户端把本机 K 线截到 `first` 起、核对首根、根数与摘要，全部相符才拼成整窗，任何不符（服务端修正数据、交易日划分变化、换源、服务端历史更长）都去掉 since 整段重取；since 不在服务端窗口内时服务端直接回整窗。拼出的图永远等于服务端窗口，游客限额（`historyTradingDays`）下窗口更短时本机多出的部分被截掉。手机多图与桌面轮询（feed 未覆盖时）都走这条增量读取；`feed.market` 推送的首帧同样增量（2026-10-13）：`PUT /api/sessions/{id}/feed` 带 `since`，这次关注的第一帧只带 since 之后的 K 线与 `barsAfter` / `barsWindow`，同一 packet 与 since 的首帧只编码一次，客户端核对不上就不带 since 重新关注拿整窗；之后仍是 patch。同一 packet 与 since 的回答在 Runtime 缓存 256 份，多个读者共享摘要计算。IndexedDB 不可用（隐私模式、配额不足）时只在本页内存保留，照常读整窗。
- 报价总表与只订报价的 extras：单合约 `get_quote` 失败只让该行显示 `market_quote_unavailable`；仍在租约内时由行情线程按 5、10、20 秒……（最长 5 分钟）重试该合约，不随读者数增加调用、不重订正常合约，租约到期即放弃。总表推送（2026-10-12）：成员页面不再每窗口每 3 秒拉整张总表，Runtime 每秒读一次共享总表（与 HTTP 同一份 1 秒缓存）、整表差异只算一次，按各窗口在 `PUT /api/sessions/{id}/feed/view` 声明的行（全部 / 交易所 / 品种 / 合约）扇出 `feed.board`（整份）与 `feed.board-patch`（只带变化的行和字段），页面隐藏时不推；断流或旧 Runtime 时客户端退回原轮询。
- 公共会话最多 48 组、同时冷加载最多 8 组；请求续 30 秒兴趣租约。SDK 没有公开逐项退订，真正回收依靠闲置关闭或至多一个候选会话的预热替换；预热失败不能中断原有热行情。不能把删除缓存项当作订阅已释放。

本轮优先保证行情呈现及时、切换不卡，同时保留模拟下单和平仓可靠性。扩展路线、延迟口径和研究依据见 [架构文档](ARCHITECTURE.md#公共行情体验优先与按规模扩展2026-09-26)；结构上限不能解释为已验证的并发人数，候选实现也不代表已部署。

官方技能包：`tqsdk-trading-and-data`，下载地址由信易科技 TqSdk 官方仓库提供。

## 开源图表

- TradingView Lightweight Charts 5.2.1
- Apache License 2.0
- 项目：<https://github.com/tradingview/lightweight-charts>
- 图表模块按需加载，并在产品界面保留 TradingView 署名链接。

## 黑盒验收

- 未点击“查看当前行情”时，不请求 K 线。
- 任意目录外合约返回 404；不支持的周期返回 422。
- Sidecar Fixture 返回确定性的普通快照，并明确标记 `dataStatus=simulated`。
- 真实返回包含同一 `symbol`、`durationSeconds`、报价和时间递增的 K 线。
- 切换周期只改变读取周期，不创建消息、候选或委托。
- 1920×1080、1366×768、960×720、768×900、414×896、375×812、320×720 均无页面级横向溢出。
