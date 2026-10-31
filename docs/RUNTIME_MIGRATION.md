# 云端 Runtime 迁移运行手册

把 `ai-runtime.qilinshuzhi.com:8443` 背后的整套服务（蓝绿两槽 Runtime、tqkq-sidecar 及已启用的 overlay、nginx、密钥与配置文件、数据卷）从当前主机搬到一台新主机。域名不变，桌面端、网页端、CLI 和更新清单都不需要改；切换只发生在 DNS 一条 A 记录上，旧机原样保留 7 天作为回滚。

一键脚本：`deploy/vps/migrate-to-host.sh <新机 ssh 别名> --from <旧机 ssh 别名> [--sha <完整发布 SHA>] [--phase …] [--new-ip <公网 IP>] [--force-data] [--apply]`。新机 ssh 用户不必是 root，脚本对 `/opt/ai-trader`、docker、数据卷的每条命令都走 `sudo -n`（要求免密）。**默认 dry-run**：只打印每一步在哪台机执行什么，只读检查会真的读；加 `--apply` 才执行。

### 安全门（#769）

- **布局前置**：每个阶段开头先只读核对旧机正在运行的发布——必须是蓝绿布局（`trader-runtime-blue/green` + `tqkq-sidecar`，nginx upstream include 指明当前槽），`.env.production` 启用的 overlay（accounts / pool / paper）连同基础 compose 和 `images.yml` 能解析出对应服务，env 里每个 `*_FILE` / `*_FILE_HOST` 文件都存在且在 `/opt/ai-trader` 下，两台机 `machine-id` 不同，两台机都没有 `/opt/ai-trader/.deploying`（发布或维护正在进行、或中断后未核实）。`--sha` 必须等于旧机 `current`（不给就取它），迁移不夹带新代码。任一不满足或读不到（ssh/sudo 失败按"未知"处理，不当作"不存在"），在任何停写和写入之前拒绝退出。
- **维护定时不回退**：使用最新 main 的迁移入口；每个阶段在任何远端写入或停服前，先联网 fetch 刷新 `origin/main`，核源机当前 SHA 的 timer 与刚刷新主线内容一致。联网或读取失败、缺失、差异均拒绝，预演同样执行且不支持离线放行。源机 timer 过旧时先正式更新源机，再迁移；不通过修改 `--sha` 夹带新代码，不运行历史旧入口绕过检查。
- **跨机拷贝**：脚本是 bash + `set -o pipefail`；每次拷贝先落到新机暂存目录，两端逐文件比 sha256（密钥只比哈希，只打印文件名），一致才改名到正式位置。源端读失败、管道中断或内容不一致都非零退出，新机原有文件不变。
- **data 不覆盖新数据**：停旧机之前先查新机——有 `ai-trader-production` 容器在跑就拒绝；数据卷非空且与上次迁移装入的清单（`backups/migrate-installed/<卷>.sha256`）不完全一致（例如新机上线后写过库、多了非空 WAL 或新 JSON）也拒绝，确需用旧机快照覆盖时显式加 `--force-data`，被替换的内容保留在 `<卷目录>/_data.before-<run>`。装入前再查一次。只读打开库留下的 `-shm` 与空 `-wal` 不算变化。
- **up 防双登录**：旧机仍有任何 ai-trader 容器在跑、或读不到旧机容器状态，就失败退出；新机 `.env.production` 及其引用的每个密钥/配置文件必须与旧机逐字节一致（起服务前当场比哈希）；每个有状态的卷（含启用 paper 时的账本卷）当前内容必须就是 data 装入的（新机此前已由本次迁移的 up 起过容器、且上述 `.up-started` 记录与装入记录一致、现存容器都是 up 会创建的那几个时，才允许已有写入；不再以“存在任意项目容器”放行），卷缺失或为空就拒绝，否则 compose 会建出空卷。
- **两端发布锁**：`--apply` 一开始就在新机取 `deploy-release.sh` 与维护任务共用的 `flock` 和 `.deploying`，data 停写前在旧机同样取；期间两台机都不会有发布或清理插进来。与 `deploy-release.sh` 同口径：远端结果确定的结束（含普通失败）释放标记；到某台机的 ssh 断线（255）或本地运行被中断（信号），该机远端命令可能仍在跑，标记保留并打印 `SOURCE_GUARD_RETAINED` / `TARGET_GUARD_RETAINED`，人工确认本次迁移的 tar / sqlite3 / docker 命令都已结束后再删 `.deploying`。
- **verify 是门**：经 TLS（正常校验证书）读新机 healthz，HTTP 失败或 `status/commit/liveTrading/authMode` 任一不符即失败。登录态、持仓、语音仍是人工检查项，脚本明确打印"未自动验证"的部分。

### 重跑语义

各阶段可以单独 `--phase` 重跑，但不是无条件幂等：init / secrets / env / images / nginx 以旧机为准覆盖新机对应文件（同样先比哈希）；data 只在新机无容器运行、且卷为空或仍与上次装入的完全一致时重跑（重新装入新快照，属主与权限随快照恢复；读不到新机容器或卷状态按未知拒绝），新机一旦启动过（库已变化）就拒绝，除非 `--force-data`；up 在上述前提下重复 `compose up` 无副作用：起容器之前先在新机记下 `backups/migrate-installed/<卷>.up-started`（装入记录的副本），之后某卷因本次迁移自己的 up 已被 Runtime 写过，只有“记录仍与装入记录一致”且新机上的项目容器全是 up 会创建的那几个时才放行，data 重跑会重写装入记录从而让旧记录失效。每次运行用新的 run id（UTC 时间加进程号），旧机快照目录 `backups/migrate-<short>-<run>` 只新建、不覆盖。

## 0. 新机规格与前提

- 4C8G 起，Ubuntu 24.04，公网 IPv4，能访问 Docker Hub / PyPI / GitHub（国内机准备好镜像源）。当前 zgo-la 上 `/opt/ai-trader` 1.3G、更新源 `/var/www/ai-trader-updates` 842M、数据卷 314M。
- 本机 `~/.ssh/config` 里给新机一个别名（`HostName` 写公网 IP，verify 阶段用它拼 `--resolve`；api.ipify.org 国内机不可达），`ssh <alias> sudo -n true` 无密码。
- 云厂商安全组要放行 TCP 22/80/8443——机内 ufw 放行了、`tcpdump` 看不到 SYN 就是安全组没开（09-16 卡了 40 分钟）。
- 镜像不在新机构建：images 阶段直接把旧机正在运行的 `ai-trader-runtime:<short>` / `ai-trader-sidecar:<short>` 经 `docker save | docker load` 搬过去：旧机标签的 image id 必须就是旧机容器（当前槽、sidecar 与启用的 overlay 成员）创建时用的镜像（标签事后被改指会拒绝），两端 image id 一致才算数（新机已有同名短标签但 id 不同会被替换），所以迁移不依赖新机访问 PyPI/Docker Hub。2026-09-20 起 `Dockerfile.cloud` 接受 `--build-arg PIP_INDEX_URL` / `--build-arg UV_DEFAULT_INDEX`，新机配好镜像源（Docker registry mirror + 腾讯云 PyPI 镜像）后也可直接本机构建；日常发布 `deploy/vps/deploy-release.sh` 已默认在生产机本机构建，不再依赖旧机构建。2026-10-09 起日常发布默认改为家里 NAS 构建并经生产机回环 registry 分发（`--build-on-host` 显式回退到本机构建，详见 `THREE_PLATFORM_AND_WEB_DEPLOYMENT.md` 的 NAS 减负一节）。
- 发布 SHA = 当前 `main` 上正在生产运行的提交（`healthz.commit`），不要在迁移里夹带新代码。
- 一起搬的外围组件（脚本不做）：
  - **桌面更新源镜像**：`scripts/publish_desktop_update.mjs` 的 `updateTargets` 含 zgo-la（nip.io 域名按旧机 IP 拼出）。主源是 aliyun + OSS，迁移后要么删掉该镜像目标，要么改成新机（改代码，单独 PR）。
  - 不搬：sing-box、bizhub-account-directory 站点（与 AI Trader 无关）。

## 1. 阶段与检查点

| 阶段 | 做什么 | 通过标准 |
|---|---|---|
| `init` | apt 装 nginx / certbot / sqlite3 / ufw / fail2ban，docker / compose v2 只在缺失时装（云镜像常预装 docker-ce，`docker.io` 与它冲突）；建 `/opt/ai-trader/{secrets,releases,backups,maintenance}`、`/var/www/letsencrypt`；UFW 开 22/80/8443，并放行容器网段 172.16.0.0/12 到宿主桥地址 172.17.0.1 的 11200（NAS RSSHub 中继）与 1080（CFTC 出口）两个端口 | `docker compose version`、`nginx -v` 有输出，`ufw status` 三条规则 |
| `secrets` | 旧机 `.env.production` 引用的每个 `*_FILE` / `*_FILE_HOST` 文件（含 pool 的 `credential_key`、accounts overlay 的配置与第二账户密钥）加 `secrets/` 下全部文件，`tar` 经 ssh 管道到新机暂存目录，保留属主与权限（容器 uid 999，见 ai-trader.md 踩坑⑩），两端 sha256 一致后改名到位；值不落本机、不打印、不进仓库 | 脚本打印 "N file(s) identical on both hosts (sha256)" |
| `env` | 同样方式逐字节复制 `.env.production`（`*_FILE` 路径与新机目录一致；`AI_TRADER_BUILD_SHA` 由 `images.yml` 按服务注入，不改 env） | 同上，哈希一致 |
| `images` | 发布 SHA 的源码归档先传成文件、两端比 sha256 再解到新机 `releases/<sha>`，把旧机正在运行的 `ai-trader-runtime:<short>` 与 `ai-trader-sidecar:<short>` 搬到新机（标签 id 须等于旧机容器的 `.Image`；image id 两端一致才沿用，否则 `docker save | docker load` 后再核 id）；按 `deploy-release.sh` 同样格式写 `images.yml`（蓝绿两槽、sidecar、已启用 overlay 的 `tqkq-sidecar-2` / `tqkq-pool` / `paper-engine`）；从发布树装 `maintenance/maintain_update_host.py` 与 timer 单元；从发布树装主机侧 unit `ai-trader-egress.service`（172.17.0.1:1080 CFTC SOCKS），并建对应系统用户和 `.ssh` 目录——密钥与已核对的主机指纹不进仓库，由人在新机生成并把公钥加到 zgo-la，unit 在密钥文件出现前保持休眠 | 两个镜像 "same image id on both hosts"；`images.yml` 覆盖全部启用服务 |
| `data` | 先查新机（见上"安全门"）→ **停写、停登录**：旧机停掉 `ai-trader-production` 项目的全部容器（槽位与 overlay 成员在前，持有快期登录的 sidecar 最后；tqsdk 同账号多端会互踢或状态错乱），确认一个都不剩；回滚名单 `backups/migrate-stopped-containers` = 旧机完整服务集合（有效 compose 服务去掉空闲槽）加当时在跑的容器，旧机开跑前已部分停掉也不缺项。停写到脚本结束期间在旧机持有 `deploy-release.sh` 与维护任务共用的锁和 `.deploying` 标记，期间不会有发布或清理改动旧机 → 在新建的 `backups/migrate-<short>-<run>/` 里为每个有状态的卷做完整快照：**整卷**复制，只留下 `codex-*`（已删除 Codex 适配器的残留，约 290M）与可重建缓存 `exchange-fees.json`、`market-backup-catalog.json`；SQLite 主库走 `sqlite3 .backup`（把 WAL 合进去），`integrity_check` 必须为 `ok`。有状态的卷 = Runtime 数据卷（库、`trading-costs/`、`workbench-extensions/`、`research_flash_aliases.json`、回放、策略运行等全部用户状态），启用 paper overlay 时再加 `ai-trader-paper-data`（`paper.sqlite3` 是模拟撮合账本）；`ai-trader-tqkq*-data` 是各 sidecar 的 TqSdk 缓存 HOME，启动时重建，不搬 → 快照经管道到新机 `<卷目录>/_data.incoming-<run>`，两端逐文件 sha256 一致、新机再 `integrity_check == ok` → 再查一次新机后用改名替换 `_data`（属主与权限随快照保留，卷根目录须是容器用户 999，否则 Runtime 建不了 WAL），记录装入清单，并在新机 `backups/migrate-<short>-<run>/` 留一份库 | 每卷 "files identical on both hosts (sha256)"、integrity ok、"installed"；行数从新机备份副本打印备查 |
| `nginx` | 用发布版本里的 `deploy/vps/nginx-ai-trader-runtime.conf` 渲染（`--domain` 不同时替换 server_name / 证书路径），upstream include `/etc/nginx/ai-trader-runtime-upstream.conf` 写为蓝槽 18000；两个文件都暂存比哈希后改名；证书见下 | `nginx -t` 通过 |
| `up` | 前提不满足即失败（见上"安全门"）；`compose up -d --wait` 只起 `tqkq-sidecar`、启用的 overlay 成员、`trader-runtime-blue`（绿槽留给下次发布）；`healthz` 断言 `commit==发布 SHA`、`liveTrading==disabled`、`authMode==owner-password`；随后每个已起的 sidecar / 第二账户 sidecar / pool / paper 成员按 `deploy-release.sh` 的 `member_gate` 同一口径在容器内带令牌读自己的 `/healthz`（pool `status ok` 且 `pool true`；sidecar / paper 报自己的 service、`liveTrading disabled`、`dataStatus` 为 normal 或 stale，每个最多等 60 秒；它们没有 compose healthcheck，`--wait` 只证明在运行）。任一不过则失败且不切 `current`（新机容器仍在运行，回滚旧机前先停掉它们）；之后起 `rsshub-tunnel`（失败只告警，同 `deploy-release.sh`）、切 `current` 软链、启用 maintenance timer | healthz 输出打印在终端；每个成员 `healthy` |
| `verify` | 再确认旧机无 ai-trader 容器运行；DNS 未切前用 `curl -fsS --noproxy '*' --resolve <域名>:8443:<新机 IP>` 走 TLS 读 healthz 并断言（本机有系统代理时 `--resolve` 会被代理忽略，必须 `--noproxy`）；人工：用桌面端连接文件里的会话（`Authorization: Bearer`）读 `/api/runtime/status`、`/api/positions`、`/api/voice/realtime/status` 并记录结果 | 自动部分输出 `VERIFIED (automated)`，并回读两个主机侧 unit（安装文件与发布树一致才算过；未启用/未监听只告警，提示按 unit 头注释补密钥）；人工三项 `trading·ready`、持仓与旧机一致、`keyConfigured=true` |
| `dns` | 手动改 A 记录（前一天先把 TTL 降到套餐最低；阿里云免费版最低 600 s，09-16 实测 6 分钟各解析器生效）；脚本只打印。`ai-runtime.qilinshuzhi.com` 在阿里云 alidns，Keychain `ai-trader-aliyun-ak` 经 `ssh aliyun-qilinshuzhi aliyun alidns UpdateDomainRecord --RecordId … --RR ai-runtime --Type A --Value <新 IP> --TTL 600` 可写（AK 只经 ssh 环境变量传，不落盘）；本机 `dig` 在代理下返回 198.18.x fake-IP，要在服务器上 `dig @dns31.hichina.com` | 域名 healthz 的 `commit` = 发布 SHA |

### 证书

nginx 的 8443 server 块引用 `/etc/letsencrypt/live/<域名>/`。DNS 还指旧机时新机无法用 webroot 验证，两种办法：

1. 从旧机整体拷 `/etc/letsencrypt/{accounts,live,archive,renewal}`（`sudo tar` 经 ssh 管道；少了 `accounts` 则 `renew` 没有 ACME 账号），DNS 切过来后新机 `sudo certbot renew --dry-run` 确认续期链路即可；
2. 或 DNS 切换后立刻在新机 `sudo certbot certonly --webroot -w /var/www/letsencrypt -d <域名>`，中间几分钟 8443 用旧证书文件缺失会起不来——所以推荐 1。

## 2. 切换顺序（正式执行日）

1. 前一天：DNS TTL 降到套餐最低；确认安全组放行 80/8443；`--phase init/secrets/env/images/nginx` 全部 `--apply` 做完（不影响旧机）。
2. 告知用户"几分钟内云端不可用"（模拟盘不必等收盘，用户 09-15 决定）。
3. `--phase data --apply`：从旧机停掉全部 ai-trader 容器到新机 `up` 完成，目标 5 分钟内；此窗口内旧机没有写入者、没有快期登录，不会双写、不会双登录。
4. `--phase up --apply`、`--phase verify`。
5. 改 DNS；本机 `dig +short <域名>`、`curl https://<域名>:8443/healthz` 看到新 commit；桌面端不用重登（同域名、同 `owner-session-secret`）。
6. 多表面回读：healthz（commit / liveTrading / marketDataStatus normal）、`/api/runtime/status`、桌面测试版云端模式登录状态与持仓、`/api/voice/realtime/status`、更新清单三处 `latest.json` 未受影响、官网产品页版本未受影响。
7. 旧机：ai-trader 容器保持停止，其余不动，7 天后再 `docker compose down` 与清理；旧机 `backups/migrate-<short>-<run>/`（整卷快照）与新机同名目录（库副本）各留一份。

## 3. 回滚

- 切换后 7 天内任何时候：先在新机停掉全部 ai-trader 容器并确认一个不剩（同账号只能一个 sidecar 登录；新机状态未知时不要启动旧机）→ A 记录改回旧机 IP → 旧机按 `backups/migrate-stopped-containers` 名单 `docker start`，sidecar 先起（`--phase dns` 打印完整命令）→ healthz 回到旧 commit。新机在这段时间写入的会话/委托链接/成交回填会丢；是否接受丢失、还是先保全新机数据再回滚，由用户决定。切换后先观察一个交易时段再让用户重度使用。
- 数据损坏：旧机 `backups/migrate-<short>-<run>/<卷>/` 是切换时刻的完整卷快照，新机同名目录有库副本；恢复前先停新机容器，不要把快照装到正在运行的库上。

## 4. 2026-09-16 实际执行记录

zgo-la → tencent-ai-trader（123.206.121.38），发布 SHA `10cb680`。data 阶段 16:06 停旧机两容器，七表行数两边一致（sessions 19 / events 1128 / proposals 26 / tqkq_sim_order_links 23 / account_trades 17 / cloud_users 0 / user_token_usage 4）；up 阶段第一次因卷根目录 root 属主起不来，`chown 999:999` 并补搬五个目录后 16:08 healthz 通过；16:09:37 改 A 记录，16:15 本机与旧机解析都到新 commit，切换窗口约 9 分钟；打包 CLI `cli call GET /api/runtime/status` 回 `accountStatus normal`、持仓与迁前一致（空）、语音 `keyConfigured=true`；`certbot renew --dry-run` 通过。未迁：cli-proxy-api（GPT-6 Astra 暂不可用）、`publish_desktop_update.mjs` 的 zgo 更新源镜像目标。旧机两容器 stop 保留到 09-23。

2026-10-24 模型改为国内供应商直连，Kimi K3.1 Pro 复用服务器 Kimi Code 密钥（`MOONSHOT_API_KEY_FILE`）、端点 `https://api.kimi.com/coding/v1`、模型 `k3.1-pro`。GPT 专用隧道与 nginx `/internal-model/v1` 不再部署；迁移仅保留独立 CFTC 出口单元。退役时备份旧环境、nginx 与隧道启用状态，先发布并回读 Kimi，再停用 AI Trader 专属模型隧道；不删除共享上游服务、凭据或历史恢复材料。

## 5. 与现有部署脚本的关系

日常发布走 `deploy/vps/deploy-release.sh`（源码归档 → 构建镜像 → `images.yml` → 服务计划（`service_plan.py` 判定 sidecar netns 里哪些容器要重建）→ 起空闲槽 → healthz 断言 → 切 upstream include → 停旧槽 → 按计划重建 sidecar netns 中需要换的容器 → 重建 paper 引擎 → `current` 软链；详见 `docs/THREE_PLATFORM_AND_WEB_DEPLOYMENT.md`）。迁移脚本只是把同样的步骤拆成可在两台机之间执行的阶段，加上密钥 / 数据 / nginx 三块；迁移完成后 `current` 已指向迁移的 release、upstream include 指向蓝槽，`deploy/vps/deploy-release.sh` 的蓝绿切换可直接接上；但它默认的 NAS 构建路径要求新机上跑独立 compose 项目 `ai-trader-registry`（`docker-compose.registry.yml`，回环 5000）并把家里 NAS 的推送隧道与受限隧道用户改指新机，迁移脚本不做这些。补齐之前，新机上的发布显式加 `--build-on-host`。
