# Runtime REST 契约基线（旧客户端兼容验收）

`openapi-baseline.json` 是"最新桌面 Release 发布时的 Runtime"全量 REST 契约快照，
用来证明云端 Runtime 的持续热修不会破坏已安装客户端正在使用的接口。客户端类型
生成仍走 `scripts/export_workbench_openapi.py`（只装配 workbench 两组路由），与本
文件无关。

## 生成与更新

- 生成脚本：`scripts/export_runtime_openapi.py`——以 fake agent/data 模式构造真实
  app（不开账户、不联网、不进 lifespan），`app.openapi()` 即全量契约；
  `info.x-ai-trader` 记录导出时的 Runtime 版本与源码 commit。
- 基线在每次版本准备时重新生成：`node scripts/prepare_release_version.mjs
  <version>` 会在 15 个版本文件改写完成后、CHANGELOG 之前用当前源码重写本文件
  （此时 `__version__` 已是新版本号，基线即"这个桌面 Release 对外发布的契约"）。
- 手工重生成：`uv run --project services/trader-runtime python
  scripts/export_runtime_openapi.py --out
  services/trader-runtime/contracts/openapi-baseline.json --commit <SHA>`。

## 兼容规则（tests/test_client_compat.py，随 pytest 每次 CI 都跑）

- R1 路径+方法：基线里每个 `paths[p][m]` 当前必须仍存在。
- R2 请求体：不新增必填字段（当前 required ⊆ 基线 required）；基线里存在的属性
  不能被 `additionalProperties: false` 拒之门外（旧客户端还会发它）。
- R3 响应：基线属性当前必须仍存在；类型不得改变（`type` 相同，或当前是包含基线
  类型的 `anyOf`）；数组 `items` 递归同规则。只判"字段被删或类型改变"：当前对象若
  变成自由对象（`properties` 为空/缺失且 `additionalProperties` 不为 false，或整个
  schema 为 `{}`），无法证明删了字段，记 `R3-loosened` 盲区打印不判红；请求侧同理
  ——自由对象接受一切，旧客户端不会被拒。理由：这类放宽只减少 schema 承诺，不改变
  运行时载荷，不是旧客户端兼容破坏。
- R4 枚举：请求与响应 schema 的枚举都不得删值（旧客户端还会发旧值、还要读旧值）。

已知盲区：`anyOf/oneOf` 只在一侧出现时只比较能对上的对象分支，其余记"盲区"打印
但不判失败；请求字段类型变化不在检查范围；递归自引用 schema 每对对象只比较一次；
`R3-loosened`（类型化对象变自由对象）同样只打印不判失败——首例：`05d996a`
（2026-09-18 生产事故修复）把 `UiState.highlights/chartAnnotations` 从
`HighlightState[]`/`ChartAnnotationItem[]` 放宽为 `list[dict]`，这两个字段由客户端
在 pulse 上报、Runtime 原样存回并回传，运行时载荷未变，旧客户端读到的字段一个
不少，属 schema 承诺变松而非客户端破坏（2026-09-20 Fable 审查会话裁定）。

## 测试红了怎么办

先判断是否真破坏：真删了客户端在用的路径/字段/枚举值，要么恢复兼容，要么在版本
准备 PR 里随版本号一起重生成基线，并在 CHANGELOG 写明"旧客户端需升级到 x.y.z"。
不许为了变绿放宽规则或手改基线 JSON。
