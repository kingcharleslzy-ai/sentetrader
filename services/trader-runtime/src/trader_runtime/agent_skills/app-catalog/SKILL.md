---
name: app-catalog
description: 产品能力目录：加载全部可用操作手册（app_capabilities）并通过统一调用入口（app_call）执行界面命令与产品接口，是外部能力发现与工程协作的基础。当用户要做界面/配置/工程类操作或问"软件能做什么"时使用。
metadata:
  ai-trader-label: 产品能力目录
  ai-trader-parent: ""
  ai-trader-summary: "外部 AI（MCP / 命令行）调用软件功能的统一入口"
  ai-trader-category: 系统
  ai-trader-tools: app_capabilities app_call
  ai-trader-routes: POST /api/agent/call
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: ""
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
---

# 产品能力目录（app-catalog）

能力发现与统一执行：`app_capabilities` 列出当前用户可用的全部操作（工具+接口+界面命令），`app_call` 按目录执行。UI、内置 AI 与外部 AI（MCP/CLI）走同一目录。

## 何时用

- 软件问题、诊断、功能变更：先 `app_capabilities` 读手册再 `app_call`。
- 问"这个软件能做什么""有哪些命令"。
- 需要按正式接口完成目录里的任何操作。

## 何时不用

- 行情/持仓等有专用工具的问题 → 专用技能的工具更直接。
- 纯聊天/咨询 → 不需要目录。

## 步骤

1. `app_capabilities` 加载目录（按当前用户过滤：停用的技能不在内）。
2. 找到目标操作的 schema，`app_call` 执行并检查结果。
3. 界面命令等 applied 回执；接口按返回码处理。

## 安全边界

- 目录即边界：目录外的接口不存在也不该猜。
- 不经此目录 patch 认证、执行、风控与更新机制。
- 扩展不得绕过产品认证；能力 schema 变化随版本更新。

## 典型说法

- 这个软件都能做什么
- 帮我看看有哪些界面命令
- 用正式接口把配置改一下

## 反例（不该触发）

- 螺纹现在多少钱（有专用行情工具，走 market-lookup）
- 买多一手（有专用下单工具，走 order-proposal）
- 今天成交怎么样（走 review-summary）
