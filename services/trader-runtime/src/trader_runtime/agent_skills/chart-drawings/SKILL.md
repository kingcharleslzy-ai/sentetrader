---
name: chart-drawings
description: 用户画线：读、画、改、删用户自己存在服务端的图上线条和图形（趋势线、水平线、通道、区间、黄金分割、矩形、文字等 20 种，带颜色/粗细/线型/延长/锁定等样式），跨设备同步。当用户说"我画的线被突破了吗""帮我在3150画一条线""把那条线改成红色虚线""把我标的区间删了"时使用。
metadata:
  ai-trader-label: 用户画线
  ai-trader-parent: "chart-analysis"
  ai-trader-summary: "读、画、改、删用户自己的图上线"
  ai-trader-category: 行情
  ai-trader-tools: list_chart_drawings create_chart_drawing update_chart_drawing delete_chart_drawing clear_chart_drawings
  ai-trader-routes: GET /api/market/drawings POST /api/market/drawings PUT /api/market/drawings/{drawing_id} DELETE /api/market/drawings/{drawing_id} DELETE /api/market/drawings
  ai-trader-tier-gated: "false"
  ai-trader-owner-only: "false"
  ai-trader-ui-entries: chart:drawings
  ai-trader-quick-action: ""
  ai-trader-default-tiers: ""
  ai-trader-mount-words: 画线 画一条 画条 画个 横线 竖线 趋势线 射线 水平线 平行线 斐波那契 黄金分割 画的线 线删
---

# 用户画线（chart-drawings）

用户自己在图上画的线和图形，按用户＋合约存在服务端，刷新不丢、换设备还在，每个合约最多 200 条。AI 能读它们并结合现价讨论，也能在用户明确要求时替他画、改、删。它与 AI 标注（chart-annotations）是两层：互不占用对方的上限，"清空图上标记"也不清用户画的线。

20 种（kind）：线条 trendline 趋势线（线段）、ray 射线、extended 直线（两端延长）、hline 水平线、hray 水平射线、vline 垂直线、channel 平行通道、polyline 折线；价位 zone 价格区间、rect_zone 矩形区间；分割 fib 黄金分割、percent 百分比线、gann 江恩八线；形状 rect 矩形、ellipse 圆或椭圆、triangle 三角形；标注 text 文字、arrow 箭头、price_label 价格标签；测量 measure（界面上的测量默认不保存）。

## 何时用

- 用户问自己的线："我画的线被突破了吗""我标的 3100 还有效吗""我画的黄金分割 61.8 在哪""我在这个合约画过哪些线"。
- 用户明确要 AI 替他画："帮我在 3150 画一条线""把 3100 到 3130 标成区间""从这波低点到高点画个黄金分割""在这画条趋势线"。
- 用户要改自己的线："把那条线改成红色""改成虚线、粗一点""向右延长""锁住它""先藏起来""只在 5 分钟图上显示""把 3100 那条挪到 3105"。
- 用户要删自己的线："把我画的 3100 那条线删了""那条趋势线不要了""我画的线全删了"。

## 何时不用

- 自己讲解行情、临时标支撑阻力给用户看 → 图上标注（chart-annotations）的 annotate_chart：那层是会话性的 AI 说明，不占用用户的服务端线。
- 用户自己动手画/改（点工具栏、拖线、改样式）→ 不用工具；需要时用界面控制打开对应界面：画线工具栏（平时隐藏，选了工具才浮现）disclosure chart-drawing-toolbar，某种工具 chart-drawing-tool {id: 工具名}，某条线的样式条 chart-drawing-style {id: 线 id}，精确设置 chart-drawing-settings {id: 线 id}。
- 到价提醒 → 到价提醒（price-alerts）；用户可以在线的右键菜单或样式条上点“设到价提醒”，那是提醒技能的事。

## 步骤

1. 读：`list_chart_drawings`（可传 symbol 只看一个合约）。每条带 id、kind、points、style 和一句回执：价位类（水平线、水平射线、价格标签、区间、趋势线、射线、直线、矩形区间）带现价相对位置（"现价 3112，在 3100 上方 12"），黄金分割/百分比/江恩带各档价位——直接用它回答。
2. 画：`create_chart_drawing`，只在用户明确说了价位/位置时用。points：hline {price}；trendline {t1,p1,t2,p2}；zone {low,high}（这三种也可写 anchors）；其余写 {anchors: [{t,p},…]}（t 秒级 bar 时间、p 价格；hray/text/price_label/vline 一个点、vline 只要 t，channel/triangle 三个点，polyline 2–20 个，其余两个），fib/percent 可加 levels。时间拿不准就问或先 get_chart_bars。label 可选 ≤12 字（text 的内容写在 label，≤60 字）。style 可选（见下）。
3. 改：`update_chart_drawing {drawingId, style?, points?, label?}`，id 从列表拿；style 只写要改的键：color（user/amber/red/green/blue/ink 或 #rrggbb）、width 1–4、dash solid/dashed/dotted、extendLeft/extendRight、showPrice、locked、hidden、allPeriods（false 时只在 period 那个周期显示，period 是画线时的周期秒数，0 = 分时；合并后的样式 allPeriods=false 必须有有效 period，旧线没有时须同时补写可信的当前图表周期，不猜原周期）。锁定的线要移动先解锁（同一次带 style.locked=false 即可）。
4. 删：`delete_chart_drawing` 删一条（id 从列表拿）；用户明确说"全删了/清空画线"时 `clear_chart_drawings {symbol}` 清空这个合约的全部画线。
5. 被拒绝（超过每合约 200 条上限、坐标不完整、合约不在目录、锁定的线不能移动）时把中文原因原话转告，不要换个参数重试。

## 安全边界

- 只在用户明确要求时才画、改、删；不要替用户主动布置画面、不要把自己的分析画进用户的服务端线层。
- 画线只是标注，不触发任何交易、不创建提醒、不改风控。
- 删除只删用户自己的线（别人的行 404）；清空只清指定合约。

## 典型说法

- 我画的线被突破了吗
- 我在螺纹上标过哪些价位
- 帮我在 3150 画一条线
- 把 3100 到 3130 标成区间
- 把那条趋势线改成红色虚线
- 把我画的 3150 那条线删了

## 反例（不该触发）

- 把支撑位画到图上（AI 自己的讲解标注，走 chart-annotations）
- 螺纹 3150 提醒我（到价提醒，走 price-alerts）
- 打开画线工具（界面命令，走 ui-control 的 disclosure: chart-drawing-toolbar）
