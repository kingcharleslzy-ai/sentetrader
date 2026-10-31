# MT5 只读行情桥（外盘，#976）

跑在 4090 Windows（`gpu-4090d`，用户 1 桌面会话）上，挂到所有者已登录的 AMP Global MT5
期货模拟终端，只读取合约属性、报价、K 线和深度，经反向隧道给腾讯生产机的 Runtime 用。
不交易、不登录：只用 `initialize(path=…)` 挂已运行的终端；终端没开、挂不上或与券商断线时
接口返回 503 `{"status":"unavailable","reason":…}`，桥不会启动、登录或重连终端；终端某次读取失败
（MT5 返回 None）也答 503 `terminal_read_failed`，不当成空数据；批量报价里单个合约选不中时为 `unknown_symbol`（选择本身因 IPC 失败则为 `terminal_read_failed`），读不到时该行 `error` 为 `terminal_read_failed`（MT5 IPC 失败，`last_error` ≤ -10000）或 `no_quote`（其余），`detail` 带 MT5 的错误码与文字，其他合约照常返回。价格字段只把 MT5 的 0 当缺失，负价照实返回。
交易函数由 `ReadOnlyMt5` 白名单拦截，发布检查 `scripts/check_desktop_release.mjs`
（`validateMt5BridgeReadOnly`）也禁止本服务 `src/`、`deploy/` 出现交易与登录调用。

## 接口（只监听 127.0.0.1:18930，每个请求带 `Authorization: Bearer <令牌>`）

| 路径 | 内容 |
| --- | --- |
| `GET /v1/status` | 终端/账户状态（总是 200，断线时 `status=unavailable`） |
| `GET /v1/symbols?path=Exchange-Futures&root=GC[&expired=1]` | 合约列表与属性 |
| `GET /v1/symbols/GCEZ26` | 单个合约：乘数（tick_value/tick_size，美元）、最小变动、保证金、到期、结算价、币种 |
| `GET /v1/quotes?symbols=GCEZ26,CLEZ26` | 最新报价：bid/ask/last、成交量、逐笔时间 `timeMsc`（交易服务器时钟≈UTC），最多 50 个 |
| `GET /v1/bars?symbol=GCEZ26&timeframe=M1&count=500&start=0` | K 线 M1/M5/M15/H1/D1，`start` 为从最新往前的位置，最多 10000 根 |
| `GET /v1/depth?symbol=GCEZ26` | 市场深度（首次请求时订阅，最多 20 个合约） |

## 部署位置（4090）

- `D:\mt5-bridge\releases\<sha>\`：每个版本一份源码和自己的 `.venv`（`uv sync --frozen`）。
- `D:\mt5-bridge\current.txt` / `previous.txt`：当前与上一版 sha；`supervise.ps1` 由 deploy 拷入。
- `D:\mt5-bridge\secrets\`（仅用户 1 可读）：`token`（与腾讯机 `/opt/ai-trader/secrets/mt5-bridge-token` 相同）、
  `tunnel_ed25519`（隧道私钥）、`known_hosts`（钉住腾讯机主机密钥）。不进仓库、不进日志。
- `D:\mt5-bridge\logs\`：`bridge.log`（按天轮转留 14 天，只记失败请求与异常）、`tunnel-<日期>.log`、`supervise-<日期>.log`。
- 计划任务 `AI Trader MT5 Bridge`：用户 1 登录时、交互、普通权限，运行 `supervise.ps1`，
  守着桥和隧道（`ssh -R 127.0.0.1:11210:127.0.0.1:18930 ai-trader-tunnel@123.206.121.38`，Git 自带的 ssh）。

## 发布、启停与回滚

在工作站上（4090 经 `mac-mini-cc` 跳板）：

```bash
sha=$(git rev-parse HEAD)
git archive --format=tar "$sha" services/mt5-bridge | ssh -J mac-mini-cc gpu-4090d "mkdir D:\mt5-bridge\releases\\$sha && tar -x -f - -C D:/mt5-bridge/releases/$sha --strip-components 2"
ssh -J mac-mini-cc gpu-4090d "powershell -NoProfile -ExecutionPolicy Bypass -File D:\mt5-bridge\releases\\$sha\deploy\windows\deploy.ps1 -Sha $sha"
```

- 停：`Stop-ScheduledTask "AI Trader MT5 Bridge"; powershell -File D:\mt5-bridge\supervise.ps1 -Stop`。
- 启：`Start-ScheduledTask "AI Trader MT5 Bridge"`。
- 回滚：用 `previous.txt` 里的 sha 重跑该版本的 `deploy.ps1 -Sha <sha>`。
- 整体撤除：停任务后 `Unregister-ScheduledTask "AI Trader MT5 Bridge"`，删腾讯机
  `/var/lib/ai-trader-tunnel/.ssh/authorized_keys` 里注释为 `mt5-bridge@gpu-4090d` 的一行。

## 测试

`npm run test:mt5-bridge`（标准库 unittest + 假终端，任何系统可跑；也随 `npm run test:release`）。
