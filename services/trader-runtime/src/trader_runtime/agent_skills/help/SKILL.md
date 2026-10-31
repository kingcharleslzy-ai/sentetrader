---
name: help
description: 查看产品帮助与使用说明（各功能的说明卡、常见问题、操作指南入口）。当用户说"这个怎么用""帮助""说明在哪""X功能是什么"时使用。
metadata:
  ai-trader-label: 帮助
  ai-trader-parent: ""
  ai-trader-summary: "使用说明和常见问题"
  ai-trader-category: 系统
  ai-trader-tools: ""
  ai-trader-routes: ""
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: help
  ai-trader-quick-action: prompt:帮助
  ai-trader-default-tiers: ""
---

# 帮助（help）

产品帮助体系：帮助目录（app_help 主题）与说明卡。概念解释、使用方法、常见问题都在这里，不散落在工作界面上。

## 何时用

- "这个功能怎么用""帮助""说明"。
- 用户问概念（"回放是什么""策略验证和回测有什么区别"）。
- 出错后用户问"这是什么意思"。

## 何时不用

- 具体业务操作（下单、开回放）→ 对应技能直接做，不必先读帮助。
- 软件缺陷诊断/工程修改 → 能力目录（app-catalog）的工程流程。
- 偏好怎么改 → 直接走偏好设置（preferences）顺手改掉。

## 步骤

1. 按问题定位帮助主题（GET /api/app/help 按分组列出：快速上手、看行情、下单与持仓、策略、AI 与语音、回放与复盘、设置与账号、管理员、开发者参考；带 ?topic= 只读一个主题，旧 id 仍可用）。
2. 先用该主题的人读层回答：summary 一句话、steps 怎么用、faq 常见问题，都是大白话；用户要细节时再引用 detail（完整说明）。
3. 需要时打开对应帮助卡（界面命令 help: {topic}，可带 query 搜索、detail 展开完整说明）。

## 安全边界

- 帮助内容与当前版本行为一致；发现过时描述如实指出。
- 只解释，不代替用户执行操作。

## 典型说法

- 这个怎么用
- 帮助
- 回放功能是什么

## 反例（不该触发）

- 打开回放（直接执行，走 market-replay）
- 默认手数改成两手（直接改，走 preferences）
- 为什么今天大跌（业务问题，走 web-research）
