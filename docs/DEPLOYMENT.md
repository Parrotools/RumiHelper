# 生产部署与运行手册

## 运行前检查

- 使用 Python 3.11+，在项目根目录复制 `.env.example` 为 `.env`。
- `.env` 只保存在部署机器，不能提交到 Git；至少填写 `ONEBOT_ACCESS_TOKEN`、`LLM_API_KEY`、`ADMIN_QQ_IDS`。
- 默认监听 `127.0.0.1:8080`。NapCat 和 Bot 在不同机器时，设置 `HOST=0.0.0.0` 并用防火墙只放行 NapCat 所在主机。

## Windows

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e "."
copy .env.example .env
.venv\Scripts\rumi-helper.exe
```

建议用任务计划程序或 NSSM 以工作目录设为项目根目录运行上述命令。停止前发送正常终止信号，避免强制杀进程。

## Linux

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e "."
cp .env.example .env
.venv/bin/rumi-helper
```

仓库提供 `ops/qqbot.service` 作为生产 systemd 模板。模板默认项目位于 `/opt/RumiHelper`，以专用 `qqbot` 用户运行，并只允许写入 `/opt/RumiHelper/data`。安装示例：

```bash
sudo useradd --system --home /opt/RumiHelper --shell /usr/sbin/nologin qqbot 2>/dev/null || true
sudo mkdir -p /opt/RumiHelper/data
sudo chown -R root:root /opt/RumiHelper
sudo chown -R qqbot:qqbot /opt/RumiHelper/data
sudo chown root:qqbot /opt/RumiHelper/.env
sudo chmod 640 /opt/RumiHelper/.env
sudo install -m 0644 ops/qqbot.service /etc/systemd/system/qqbot.service
sudo -u qqbot /opt/RumiHelper/.venv/bin/rumi-doctor
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot.service
sudo systemctl status qqbot.service
```

如果部署目录不是 `/opt/RumiHelper`，先修改 unit 中的 `WorkingDirectory`、`ExecStartPre`、`ExecStart` 和 `ReadWritePaths`。如果把 `DATABASE_PATH` 改到 `/opt/RumiHelper/data` 之外，也必须把实际数据库目录加入 `ReadWritePaths`，否则 `ProtectSystem=strict` 会让 systemd 下的 Bot 无法创建或更新 SQLite/WAL 文件。`rumi-doctor` 只做离线检查，不连接 QQ/LLM，也不会输出 token/API Key；它会校验监听端口是否可绑定、本地资源、cron、时区、文件权限以及关键传输安全配置：端口已被占用、非 loopback 监听但未配置 `ONEBOT_ACCESS_TOKEN`、或携带 API Key 的远程 LLM 使用明文 HTTP，都会判为 `FAIL`。`WARN` 不阻止启动，`FAIL` 返回非零并让 systemd 拒绝启动。日志由 journald 收集，可用 `journalctl -u qqbot.service -f` 查看。

## NapCat OneBot 11

在 NapCat WebUI 创建 WebSocket 客户端：

| 字段 | 值 |
|---|---|
| `enable` | `true` |
| `url` | `ws://<bot-host>:<PORT>/onebot/v11/ws` |
| `token` | 与 `ONEBOT_ACCESS_TOKEN` 完全一致 |
| `reconnectInterval` | `5000` 或 NapCat 默认值 |

NapCat 是客户端，Bot 是服务端。两者同机时使用 `ws://127.0.0.1:8080/onebot/v11/ws`。Bot 启动后在 NapCat 日志确认 WebSocket connected，再用另一个 QQ 私聊发送“你好”。

## 环境变量

除 `.env.example` 的网页、并发、Scheduler 配置外，长期运行重点关注：

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `DATABASE_PATH` | `data/bot.db` | SQLite 数据库路径 |
| `GITHUB_CHECK_CRON` | `0 * * * *` | GitHub 检查频率；留空关闭 |
| `DAILY_REPORT_CRON` | `0 23 * * *` | 日报时间；留空关闭 |
| `SEND_RATE_LIMIT_PER_SECOND` | `1` | direct/broadcast/持久队列在共享同一 SQLite 数据库的进程间统一限速 |
| `INTERACTIVE_SEND_RATE_LIMIT_PER_SECOND` | `0.5` | 所有交互回复的统一发送速率 |
| `OUTBOUND_MAX_ATTEMPTS` | `3` | 主动通知最大投递次数 |
| `OUTBOUND_RETRY_DELAY_SECONDS` | `30` | 投递失败后的固定等待秒数 |
| `OUTBOUND_ACCOUNT_PAUSE_SECONDS` | `1800` | 检测到账号掉线/登录态失效后暂停主动投递的秒数 |
| `OUTBOUND_RATE_LIMIT_PAUSE_SECONDS` | `60` | 检测到接口限流后暂停主动投递的秒数 |
| `OUTBOUND_QUEUE_POLL_SECONDS` | `1` | 队列空闲轮询秒数 |
| `OUTBOUND_LEASE_SECONDS` | `60` | 进程异常后 `sending` 任务可被重新领取的秒数 |

提醒、GitHub 变化和日报先写入 SQLite 队列，再由 Dispatcher 投递。进程重启不会丢失未完成投递；超过最大次数的记录会进入 `failed` 状态。账号掉线或接口限流触发的全局投递熔断截止时间也保存在 SQLite 中，因此 Bot 重启不会绕过尚未结束的 cooldown。

## 健康检查、备份与升级

- `GET /livez`：只表示 Bot 进程与 HTTP 事件循环仍存活，不探测任何外部依赖；适合作为 liveness probe。
- `GET /readyz`：检查 SQLite、LLM 与 Scheduler；QQ 或 GitHub 短暂异常不会把实例判为未就绪，适合作为 readiness probe。
- `GET /healthz`：完整业务健康检查，额外包含 GitHub 与 QQ/OneBot 状态，但不返回业务计数或原始错误字符串；用于运维观测，不要把 QQ 短暂掉线直接绑定为进程重启条件。不要将这些端点暴露到公网。
- QQ 内发送 `/status`：普通用户只看到基础可用性和运行时间；Bot 管理员额外看到依赖状态、任务数、监控数、记忆数和最近投递错误。
- 升级前先停止 Bot，并备份整个 `data/` 目录。SQLite 使用 WAL 时应同时备份 `bot.db`、`bot.db-wal`、`bot.db-shm`，或先正常停机后仅备份 `bot.db`。
- 新版本启动时自动执行有序 migration；每个 migration 与对应 `schema_migrations` 版本标记在同一 SQLite 事务内，失败会整体回滚并关闭连接。已有数据不会被删除；若启动失败，保留备份并查看 migration 相关日志，不要手改 `schema_migrations`。

## 故障排查

### NapCat 登录恢复（4.15.0 / 4.18.19）

部署 `ops/napcat/napcat_watchdog.py` 到 `/usr/local/lib/qqbot/napcat_watchdog.py`，
部署同目录的 `.sh` 到 `/usr/local/sbin/napcat-watchdog`，并安装 `.service` / `.timer`。
修改 systemd 单元后执行 `systemctl daemon-reload`。守护服务从
`/etc/napcat/napcat.env` 读取 `NAPCAT_QUICK_ACCOUNT` 和 `NAPCAT_QUICK_PASSWORD_MD5`；
该文件必须由 root 持有、权限 600。NapCat 4.15.0 自身不读取这两个密码回退配置，
由守护服务调用本机 WebUI 的现有密码登录接口。WebUI token 从 NapCat 原有配置读取，
不写入日志、命令行或 Git。升级到 4.18.19 后，将 `ops/napcat/controlled-login.conf`
安装为 `/etc/systemd/system/napcat.service.d/controlled-login.conf`，避免 NapCat 自身和守护程序
各尝试一次密码登录。密码仅由守护程序的 EnvironmentFile 加载；WebUI 的人工密码登录照常可用。
4.15.0 不支持验证码提交；4.18.19 支持验证码/新设备验证流程。版本升级不等于消除平台掉线风险。

- WebUI 同时报告登录成功、QQ 在线才视为登录恢复。`get_status` 只表示 QQ 客户端报告的在线状态，不能保证消息端到端送达。
- 启动后先等待 120 秒让快速登录完成；登录态失效时，每次故障最多调用一次密码登录，且 24 小时最多一次。
- 验证码、账号限制、密码错误或登录请求超时后等待人工处理，保留 WebUI；不会停止 NapCat，也不会无限重试。
- 确认离线持续 5 分钟才允许恢复重启；重启间隔指数增长，24 小时最多 3 次。探测超时不触发重启，已停止的服务不会擅自启动。
- 状态和重试预算持久化在 `/var/lib/napcat-watchdog/state.json`，状态变化记录在 `journalctl -u napcat-watchdog.service`。旧日志和旧的 `manual-auth-required` 标记不会反复覆盖新的在线状态。

需要明确恢复一个已停止的实例时，可运行一次（已有凭据仍由 systemd 加载）：

```bash
systemd-run --collect --property=Type=oneshot \
  --property=EnvironmentFile=/etc/napcat/napcat.env \
  /usr/local/sbin/napcat-watchdog --recover
```

`--recover` 不重置当天重试预算。平台是否再次撤销登录态仍须通过运行记录和真实收发验收，
守护脚本不能保证账号永不被踢下线。

| 现象 | 排查方式 |
|---|---|
| NapCat 持续重连 | 检查 `url` 路径、端口、防火墙和双方 token 是否一致 |
| `/healthz` 的 LLM 为异常 | 检查 `LLM_BASE_URL`、API Key、模型权限和出网；错误详情只看部署日志 |
| GitHub 为异常 | 检查出网、`GITHUB_TOKEN` 与 API 限额；无 token 时限额较低 |
| 主动通知未送达 | 用 `/status` 查看最近错误；检查 `outbound_messages` 中 `status/attempts/last_error`，确认 NapCat 已连接 |
| 定时任务不运行 | 核对时区与 cron，用 `/status` 确认 Scheduler 运行中及任务数；system cron 修改会即时重排 APScheduler job，无需仅为 cron 变更重启 Bot |
| 定时任务历史持续增长 | `SCHEDULED_TASK_HISTORY_LIMIT` 控制已禁用/终态任务保留数量；active 任务永不清理，设为 `0` 表示不保留终态历史。对应 `scheduled_content_history` 会随被清理的任务一起删除。 |
| 数据库锁定 | 确认只运行一个 Bot 进程；正常停止残留进程后恢复，禁止多个实例共用同一 SQLite 文件 |
