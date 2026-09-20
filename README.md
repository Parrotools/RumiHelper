# qq-llm-bot

用真实 QQ 账号运行的 LLM 聊天机器人：NapCatQQ + OneBot 11 + NoneBot2 + OpenAI Compatible API。

功能一览：

- 私聊 / 群聊 @ / 回复机器人续聊，群内普通消息不响应，未知命令给帮助而不是丢给 LLM
- 按群聊持久化控制反馈：关闭后普通成员的聊天和命令静默，Bot 管理员仍可使用
- 多轮上下文会话（SQLite 持久化，群内成员默认独立上下文），`/clear` 清空；共享群上下文仅 Bot 管理员可清空
- YAML 可配置人格（默认 Rumi）
- 用户显式维护的长期记忆，并在聊天时按重要度加载
- 一次性与 cron 定时提醒（通知默认关闭）以及管理员定时群发固定消息
- 管理员定时按主题生成不同段子并发送到群
- GitHub 仓库监控：手动检查、commit/Star/Fork/Issue/Release 变化通知
- 自然语言工具操作：可直接描述 GitHub、记忆、提醒、通知、定时任务、日报、状态和管理员群发等操作；白名单工具执行，修改/发送操作二次确认
- 每日日报：汇总 GitHub 变化、任务/提醒和重要记忆，可手动查看
- 网页总结：识别消息中的 URL，抓取正文，超长网页 Map-Reduce 分块总结；URL 结果进程内缓存（TTL 可配）；可选 Playwright 渲染兜底，对浏览器每个 HTTP(S) 请求执行 SSRF 校验，同时禁用 Service Worker 并阻断 WebSocket
- 主动发送 / 多目标群发（仅管理员，带预览确认、TTL、限速、逐目标结果记录、旧待确认任务自动作废）
- 安全：SSRF 逐跳校验、Prompt Injection 定界防御、核心聊天模型无发送能力、API Key 不入日志

> 详细命令用法和操作流程见 [USAGE.md](USAGE.md)。
> 长期运行、升级、备份和故障排查见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

## 架构

```
真实 QQ 账号
   │
   ▼
NapCatQQ                    ← 只负责 QQ 协议 ↔ OneBot 11 转换
   │  OneBot 11 反向 WebSocket（NapCat 主动连入 Bot）
   ▼
NoneBot2 (FastAPI 驱动, ws://127.0.0.1:8080/onebot/v11/ws)
   │
   ├─ rumi_helper/personality/    YAML 人格配置与加载
   ├─ rumi_helper/plugins/        事件路由：ai_chat / agent / web_summary / memory / scheduler / github / report / broadcast / admin / group_feedback
   ├─ rumi_helper/services/       业务层：LLM Gateway、Agent Harness、网页管道、会话、记忆、Scheduler、GitHub、Report、MessageDispatcher、群聊反馈设置
   ├─ rumi_helper/security/       权限、SSRF、Prompt Injection 防御
   └─ rumi_helper/database/       SQLite (aiosqlite)：db.py 管连接/migration，repositories/ 按领域拆分 sessions / messages / memories / scheduled_tasks / 群聊反馈 / GitHub / outbound SQL
```

## 环境要求

- Python 3.11+
- Windows 10/11 或 Linux（x86_64）
- 一个真实 QQ 号（建议用小号，挂协议有风控风险）
- 任一 OpenAI Compatible API 的 Key（GLM / DeepSeek / OpenAI / Qwen / vLLM 均可）

## 快速开始（5 步）

```bash
# 1. 建虚拟环境并装依赖
python -m venv .venv
.venv/Scripts/pip install -e .        # Windows
# .venv/bin/pip install -e .          # Linux

# 2. 配置
copy .env.example .env                # Windows（Linux: cp）
#   编辑 .env：填 LLM_API_KEY、自定 ONEBOT_V11_ACCESS_TOKEN、ADMIN_QQ_IDS

# 3. 安装并登录 NapCatQQ（见下文第 5-7 节）

# 4. 启动
.venv/Scripts/rumi-helper.exe

# 5. 用另一个 QQ 号私聊机器人发"你好"验证链路
```

## 1. Python 环境安装

Windows（PowerShell）：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
```

Linux：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

推荐使用仓库锁文件做可重复安装：`uv sync --frozen --extra dev`。只安装运行依赖可用 `uv sync --frozen --no-dev`。如果不用 uv，仍可使用普通 venv + pip。

## 2. 安装依赖

推荐按仓库中的 `uv.lock` 固定完整依赖图：

```bash
uv sync --frozen --no-dev      # 运行依赖
uv sync --frozen --extra dev   # 开发/测试依赖
```

兼容的 pip 安装方式仍然可用，但会按当时可解析到的依赖版本安装，重建结果不如 lockfile 可重复：

```bash
pip install -e .
pip install -e ".[dev]"
```

## 3. 配置 .env

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

然后编辑 `.env`，至少修改三处：

| 变量 | 说明 |
|---|---|
| `ONEBOT_V11_ACCESS_TOKEN` | 自定一个随机字符串，**必须与 NapCat 侧填的完全一致** |

> 兼容旧配置名 `ONEBOT_ACCESS_TOKEN`；新部署应使用 OneBot v11 adapter 的 canonical 名称 `ONEBOT_V11_ACCESS_TOKEN`。
| `LLM_API_KEY` | 你的 LLM API Key |
| `LLM_BASE_URL` / `LLM_MODEL` | 按厂商填，如 GLM：`https://open.bigmodel.cn/api/paas/v4` + `glm-4-flash` |
| `PERSONALITY_FILE` | 人格 YAML 文件路径，默认 `rumi_helper/personality/rumi.yaml` |
| `OWNER_QQ_ID` | 主人的真实 QQ 号；只按此 ID 识别主人，不按昵称猜测（留空且只有一个管理员时回退到该 ID） |
| `OWNER_NAME` | 主人称呼，默认 `Parrotools` |
| `AGENT_TOOL_CALLING_ENABLED` | 是否启用自然语言工具操作，默认 `true` |
| `AGENT_CONFIRM_TTL_SECONDS` | 自然语言修改操作的确认有效期，默认 300 秒 |
| `NON_LLM_REPLY_DELAY_MIN_SECONDS` / `_MAX_SECONDS` | 不调用 LLM 回复的动态延迟范围，默认 2～6 秒；最大值设为 0 可关闭 |
| `NON_LLM_REPLY_DELAY_CHARS_PER_SECOND` | 动态延迟的文本处理速度，默认每秒 35 个字符 |

其余变量（限速、并发、网页大小上限、URL 缓存等）默认值即可运行，详见 `.env.example` 注释。

## 4. NapCatQQ 安装

- Windows：下载 [NapCat.Shell](https://github.com/NapNeko/NapCatQQ/releases)（zip 解压即用，无需安装完整 QQ）或使用 NapCat.Installer。
- Linux：推荐 Docker（`mlikiowa/napcat-docker` 镜像）或一键安装脚本（见 NapCatQQ 官方文档 https://napcat.napneko.icu/guide/start-install ）。

## 5. 登录 QQ

启动 NapCat 后控制台会出现二维码，用机器人 QQ 号扫码登录（首次登录后保留会话）。登录成功后进入 NapCat WebUI（默认 `http://127.0.0.1:6099/webui`，token 见启动日志）。

## 6. NapCat OneBot 11 反向 WebSocket 配置

在 NapCat WebUI → 网络配置 → 新建「WebSocket 客户端」（websocketClients）：

| 字段 | 填写值 |
|---|---|
| enable | `true` |
| url | `ws://127.0.0.1:8080/onebot/v11/ws` |
| token | 与 `.env` 中 `ONEBOT_V11_ACCESS_TOKEN` **完全相同** |
| heartInterval | 默认即可 |

对应 JSON（可直接改 NapCat 的 onebot11 配置文件）：

```json
{
  "network": {
    "websocketClients": [
      {
        "enable": true,
        "url": "ws://127.0.0.1:8080/onebot/v11/ws",
        "token": "your-onebot-token",
        "reconnectInterval": 5000
      }
    ]
  }
}
```

> NapCat 是 WebSocket **客户端**，Bot 是**服务端**。Bot 侧监听地址和端口由 `.env` 的 `HOST` / `PORT` 决定（默认 127.0.0.1:8080）。`url` 末尾的 `/onebot/v11/ws` 路径不能省。若 NapCat 与 Bot 不在同一台机器，把 `127.0.0.1` 换成 Bot 机器的 IP，并确认端口互通。

## 7. 启动 Bot

```bash
rumi-helper
```

看到 NapCat 日志出现 `Websocket connected` / OneBot 事件即链路打通。机器人私聊回复 `/help` 可查看全部命令。

## 8. 验收测试清单

| 场景 | 操作 | 预期 |
|---|---|---|
| 私聊 AI | 私聊发送 `你好，你是谁` | LLM 回复 |
| 群 @ AI | 群里发送 `@机器人 解释一下 Dijkstra` | LLM 回复 |
| /ai 命令 | 群里发送 `@机器人 /ai 写个俳句`（私聊无需 @） | LLM 回复 |
| 群普通消息不响应 | 群里发 `今天晚上吃什么` | 无反应 |
| 续聊 | 回复机器人上一条消息继续提问 | 带上下文回复 |
| 帮助 | 私聊发送 `/help`；群里发送 `@机器人 /help` | 列出全部命令 |
| 未知命令 | 私聊发送 `/foobar` | 提示未知命令 + 帮助（不调用 LLM） |
| 清空会话 | 发送 `/clear` | 默认清空自己的会话；共享群上下文仅 Bot 管理员可清空 |
| 网页总结 | `/总结 https://example.com` | 先回"正在读取"，再按固定格式返回摘要 |
| @+URL 自动总结 | `@机器人 这篇文章主要说什么 https://example.com/article` | 自动进入总结 |
| SSRF 拒绝 | `/总结 http://127.0.0.1:8080` | "该地址不被允许访问" |
| 运行状态 | `/status` | 普通用户查看基础可用性；管理员额外查看依赖、任务数、监控数、记忆数和最近错误 |
| 主动发送（管理员私聊机器人） | `/broadcast user:123456 -- 你好` | 预览 → `/confirm` → 发送并汇报统计 |
| 定时群发（管理员） | `/schedule group:456789 cron:0 8 * * * -- 大家早上好` | 持久化任务，每天 08:00 自动发到指定群 |
| 定时主题段子（管理员） | `/schedule joke group:456789 cron:0 12 * * * -- mobile` | 每天 12:00 生成不同的 mobile 主题段子并发送 |
| 群聊反馈开关（管理员） | `@机器人 /group-feedback off` | 关闭当前群普通成员的聊天和命令反馈；管理员仍可用 |
| 查看群聊反馈状态（管理员） | `@机器人 /group-feedback status` | 查看当前群普通成员反馈是否开启 |
| 权限拒绝 | 非管理员发 `/broadcast ...` | "该命令仅管理员可用。" |
| 重复 /broadcast | 管理员连续两次 /broadcast | 只保留最新一条待确认，旧的自动作废 |
| GitHub 监控 | `/github add https://github.com/owner/repo` | 添加仓库后可手动检查或接收变化通知 |
| 自然语言工具操作 | `把 OpenAI/openai-python 加入我的 GitHub 列表`、`每天 12 点在群 456789 讲 mobile 段子` | 自动识别为白名单工具；修改、定时和发送操作需回复“确认执行” |
| 每日日报 | `/report` | 查看当天 GitHub、任务和重要记忆汇总 |

GitHub 定时汇总的时间通过 `.env` 配置，收件人和发送群通过管理员命令设置。下面的配置会在上海时间每天 06:00 和 18:00，查询全部已登记仓库的最新 commit，并将包含 commit 时间、作者和消息的同一份汇总发送给指定目标：

```env
GITHUB_DIGEST_CRON=0 6,18 * * *
```

设置目标（会替换之前的目标）：

```text
/github digest set user:123456789,user:987654321,group:456789
```

查看或清空目标：

```text
/github digest list
/github digest clear
```

只有管理员可以设置目标；清空目标后定时发送会自动关闭。修改 `.env` 中的时间后需要重启 Bot。

主动发送也可以不经聊天窗口，用命令行（需 NapCat 开启 HTTP 服务端，环境变量 `ONEBOT_HTTP_URL` 指向其地址）。脚本允许本机 loopback 使用 HTTP；如果配置了 `ONEBOT_V11_ACCESS_TOKEN` 且目标不是 loopback，则要求使用 HTTPS，避免 token 明文传输：

```bash
python scripts/send_message.py user:123456 "你好"
python scripts/send_message.py group:789012 "通知"
```

## 9. 管理员配置

`.env` 中逗号分隔填写：

```
ADMIN_QQ_IDS=111111,222222
```

修改后重启 Bot。管理员专属能力：`/broadcast`、`/confirm`、`/cancel`、`/group-feedback`。

## 10. 安全设计说明

- **SSRF**：普通 `WebFetcher` 对每一跳先做 scheme + DNS/IP 校验（loopback / RFC1918 / link-local（含 169.254.169.254 云 metadata）/ 组播 / 保留段全部拒绝），随后把 TCP 连接固定到本次已校验的公网 IP，同时保留原域名的 Host/SNI，从而关闭 DNS rebinding 的校验-连接窗口；多个已校验公网 IP 会在网络失败时依次 fallback。网页抓取显式 `trust_env=false`，不会读取 `HTTP_PROXY` / `HTTPS_PROXY`；如需代理必须使用 `WEB_PROXY_URL=socks5://...`，因为普通 HTTP CONNECT 代理无法在 IP pinning 后可靠保留原域名 TLS SNI。可选 Playwright fallback 会在独立 BrowserContext 中禁用 Service Worker、阻断 WebSocket，并对浏览器每个 HTTP(S) 请求重新做 SSRF 校验；但 Chromium 无法复用同一套 IP pinning，因此其 DNS rebinding 防护仍弱于默认 WebFetcher。对高安全部署建议保持 `ENABLE_PLAYWRIGHT=false`。
- **Prompt Injection**：网页内容永远包在 `<untrusted_web_content>` 定界符内送入 LLM，system prompt 明确声明其中任何指令无效；总结链路代码中不存在 `MessageDispatcher` 引用，网页内容在架构上不可能触发 QQ 发送。
- **发送权限**：`MessageDispatcher` 是唯一发送出口；核心聊天模型没有发送能力。自然语言 Agent 只能调用已注册的受限工具，管理员群发和定时群发仍需管理员权限及确认。
- **Agent Harness**：LLM 只能返回白名单中的结构化工具名和参数；参数由业务代码再次校验。只读查询直接执行，记忆/通知/提醒/定时/清空/仓库变更/群发等写操作先写入 SQLite 待确认队列，确认后原子领取，避免重复执行。
- **群发**：管理员专属 + 数量上限（默认 20）+ 主动发送全局限速（direct/broadcast/持久队列在共享同一 SQLite 数据库的进程间统一，默认 1 条/秒）+ 预览确认（TTL 5 分钟、发起人本人可确认、仅可执行一次、重复下发自动作废旧任务）。
- **敏感数据**：`.env` 已在 `.gitignore`；日志不输出 API Key，网页正文与发送内容截断后入库。
- **优雅关闭**：Bot 停机时自动释放 LLM/抓取 HTTP 客户端与数据库连接。
- **GitHub 监控**：使用 `GITHUB_CHECK_CRON` 周期检查；仅用户开启 `/notify github on` 后推送，GitHub Token 不写日志；每用户监控仓库数量有上限，避免周期 API 调用无限增长。
- **GitHub 定时汇总**：使用 `GITHUB_DIGEST_CRON` 周期获取全部已登记仓库的最新 commit，并发送给 `/github digest` 配置的用户或群；汇总包含 commit 时间、作者和消息。
- **日报**：使用 `DAILY_REPORT_CRON` 触发；仅用户开启 `/notify report on` 后发送，内容写入 `reports` 表。

## 11. 常见报错

| 现象 | 原因 / 处理 |
|---|---|
| Bot 启动但 NapCat 一直重连失败 | 检查 `url` 路径是否为 `/onebot/v11/ws`；双方 `token` 是否一致；`HOST/PORT` 是否被占用 |
| 报 `401` / 鉴权失败 | token 不一致 |
| 收不到群消息 / 无法 @ 响应 | NapCat 未上报群消息，检查 NapCat 日志；确认机器人未被禁言 |
| AI 回复"服务暂时不可用" | 看 Bot 控制台 traceback：一般是 `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` 填错或欠费 |
| 网页总结失败 | 目标站反爬（可开 `ENABLE_PLAYWRIGHT=true` 并 `pip install ".[playwright]"` + `playwright install chromium`，Bot 会自动降级用 Playwright 重试；浏览器 fallback 的 SSRF 防护弱于默认 IP-pinned WebFetcher，仅在确有需要时开启） |
| `pip install trafilatura` 失败 | 先升级 pip：`pip install -U pip`，确保有预编译 lxml wheel |

## 12. Windows 部署

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .
copy .env.example .env   # 编辑后
.venv\Scripts\rumi-helper.exe
```

建议用 NSSM 或 任务计划程序 注册为常驻服务。

## 13. Linux 部署

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # 编辑后
.venv/bin/rumi-helper
```

生产环境建议直接使用仓库中的 `ops/qqbot.service` 模板。默认部署路径为 `/opt/RumiHelper`，安装后由 console entry point `.venv/bin/rumi-helper` 启动；启动前可先运行 `.venv/bin/rumi-doctor` 做离线配置/权限预检，systemd 模板也会通过 `ExecStartPre` 自动执行该检查。模板同时限制写权限到 `data/`，并启用基础 systemd sandbox。详细安装步骤见 `docs/DEPLOYMENT.md`。

## 14. 开发

```bash
pip install -e ".[dev]"
pytest        # 单测：URL / SSRF / 权限 / 解析器 / 会话 / 记忆 / Scheduler / GitHub / 分块 / LLM / 缓存 / Dispatcher / 插件触发规则 / 注入防御
ruff check rumi_helper tests
```

## 15. 后续路线

- `GROUP_SHARED_CONTEXT=true`（已支持）群共享上下文
- `ENABLE_PLAYWRIGHT=true`（已支持）JS 页面渲染兜底，抓取失败自动降级
- `WEB_CACHE_TTL_SECONDS`（已支持）URL 抓取缓存
- `WEB_PROXY_URL=socks5://...`（已支持）在保持 SSRF IP pinning + 原域名 TLS SNI 的前提下经 SOCKS5 抓取网页
- 在真实 QQ/NapCat 上验收全部自然语言工具；后续新增业务能力继续复用同一套白名单、权限和确认流程
