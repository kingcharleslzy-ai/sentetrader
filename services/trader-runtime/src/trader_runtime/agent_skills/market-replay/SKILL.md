---
name: market-replay
description: 行情回放：选定交易日与合约重建历史行情环境，在隔离的回放账户里练习交易（数据从该时刻起逐步推进，不引入未来信息）。当用户说"回放9月18号的行情""开个回放练练""重播那天螺纹的走势"时使用。
metadata:
  ai-trader-label: 行情回放
  ai-trader-parent: ""
  ai-trader-summary: "回到过去某一天逐根重放行情"
  ai-trader-category: 复盘
  ai-trader-tools: ""
  ai-trader-routes: POST /api/replays
  ai-trader-tier-gated: "true"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: replay
  ai-trader-quick-action: ""
  ai-trader-default-tiers: basic pro test
---

# 行情回放（market-replay）

历史交易日的重演：隔离的回放子环境（自己的行情、账户、卡片），从当日第一根 1 分钟线开盘（有夜盘即夜盘开盘）起一步一根 1 分钟线推进，用于练习与复盘。

## 何时用

- "回放 9 月 18 号的行情""开个螺纹的回放"。
- "我想重练一下那天突破的处置"。

## 何时不用

- 播放中要暂停/加速/单步 → 回放控制（replay-control）。
- 复盘今天的实盘/模拟成交 → 复盘与日结（review-summary）。
- 回测策略（自动重放历史）→ 策略验证与回测（strategy-backtests）。

## 步骤

1. 确认合约与交易日（默认最近一个完整交易日时需说明）；不确定哪天休市时先读 `GET /api/market/calendar`（当前交易日、最近已结束交易日与最近十个已结束交易日）。
2. `POST /api/replays` 创建；系统给出隔离回放环境。选到休市日或还没走完的交易日会被 422 拒绝，中文原因直接点名最近的交易日，原话转告、换个交易日重试。
3. 进入回放后用回放控制推进；回放里的下单在回放账户成交，与真实模拟账户完全隔离。

## 安全边界

- 回放环境隔离：任何委托、持仓、账户都只作用于回放，退出即弃。
- 回放中不连接通用 AI（避免未来信息）：行情、持仓、账户与回放控制可以直接问。
- 未开通时创建接口 403。

引擎边界（播放/暂停/单步/四档步速、不能按时间跳）见 [references/engine.md](references/engine.md)。

## 典型说法

- 回放9月18号的螺纹行情
- 开个回放练练
- 重播那天铁矿的走势

## 反例（不该触发）

- 暂停回放 / 快进（播放控制，走 replay-control）
- 回测一下这个策略（自动历史重放，走 strategy-backtests）
- 今天成交怎么样（当日复盘，走 review-summary）
