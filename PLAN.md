# qq-llm-bot 策划方案

> 目标：用一个真实 QQ 账号跑起来的个人 AI Agent，支持会话上下文、人格、长期记忆、
> 网页总结、GitHub 监控、定时任务、主动通知和日报，Windows / Linux 双平台可部署。

---

## 1. 总体架构

```
真实 QQ 账号
   │  (QQ 登录协议由 NapCat 实现，业务代码绝不碰协议)
   ▼
NapCatQQ  ──(OneBot 11 反向 WebSocket)──▶  NoneBot2 (Python 3.11+, asyncio)
                                              │
                    ┌─────────────────────────┼──────────────────────────┐
                    ▼                         ▼                          ▼
               plugins/ 事件路由          services/ 业务层             security/
            (ai_chat / web_summary     (LLM Gateway / Web 管道 /      (权限 / SSRF /
             / memory / scheduler      Session / Memory / Scheduler / Prompt Injection)
             / github / report         GitHub / Report / Dispatcher)
             / admin / broadcast)
                                              │
                                              ▼
                                        SQLite (aiosqlite)
```

关键点：

- **协议层与业务层彻底隔离**。NapCat 只做「QQ ↔ OneBot 11」转换；NoneBot2 用
  `nonebot-adapter-onebot`（v11）收事件。业务层只认 OneBot 事件模型。
- **反向 WebSocket**：由 NapCat 主动连 Bot。Bot 侧监听 `ws://127.0.0.1:8080/onebot/v11/ws`，
  双方配同一个 access token。这样 Bot 侧不需要公网 IP，NapCat 放在家/服务器都行。
- **LLM 走 OpenAI Compatible API**，`LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` 全部来自环境变量，
  不绑厂商（GLM / DeepSeek / Qwen / OpenAI / vLLM 均可）。

## 2. 技术选型与理由

| 项 | 选型 | 理由 |
|---|---|---|
| Python | 3.11+ | NoneBot2 与 asyncio 生态最佳版本区间 |
| 协议接入 | NapCatQQ | 活跃维护、Windows/Linux 都有发行版、OneBot 11 实现完整 |
| Bot 框架 | NoneBot2 + OneBot v11 adapter | 事件/权限/过滤器现成，避免手写 WS 协议 |
| LLM 调用 | httpx.AsyncClient 自实现 `OpenAICompatibleProvider` | 比引入 langchain 轻得多，接口可控、易 mock 测试 |
| 数据库 | SQLite + **aiosqlite**（薄 repository 封装） | 当前为单实例、低并发，SQLAlchemy async 属于过度设计；业务层不散写 SQL |
| 网页抓取 | httpx（异步、可限大小、可控 redirect） | 需要逐跳 SSRF 校验，必须手动控制 redirect 流程，aiohttp/httpx 均可，选 httpx |
| 正文提取 | trafilatura，失败降级 BeautifulSoup | trafilatura 正文质量最好；BS4 兜底保证可用性 |
| JS 渲染 | Playwright（预留，`ENABLE_PLAYWRIGHT=false` 默认关） | 重依赖，按需启用；同样过 SSRF 检查 |
| 包管理 | pyproject.toml + uv（推荐），兼容 `pip install .` | uv 快，但不强制 |
| 日志 | logging（标准库）+ 自定义 filter | 避免引入 loguru 依赖，格式统一且能强制脱敏 |

## 3. 核心设计决策

1. **ID 全部 str**：`user_id / group_id / message_id` 业务层一律 `str`，与 OneBot API 字符串化参数对齐。
2. **会话模型**：私聊 `private:{user_id}`；群聊 `group:{group_id}:{user_id}`（每人独立上下文），
   预留 `GROUP_SHARED_CONTEXT` 切换为 `group:{group_id}`。历史条数用 `MAX_CONTEXT_MESSAGES` 截断，
   SQLite 存 `sessions`、`messages` 两表。
3. **群聊触发面收窄**：群内只有 `@机器人` 或 `/ai` 才调 LLM；普通聊天绝不响应；过滤自身消息
   与重复 `message_id`（内存 LRU 去重即可，不必入库）。
4. **网页管道**：`URLParser → SSRFGate → Fetcher → Extractor → Chunker → Map-Reduce Summarizer`。
   - SSRF：`ipaddress` 库判断，覆盖 loopback / 私网 / link-local(含 169.254.169.254) / 组播 / 保留段；
     DNS 解析后校验 IP；**redirect 每一跳重新校验**并限制跳数；只允许 http/https。
   - Map-Reduce：按 `WEB_SUMMARY_CHUNK_CHARS` 切块、`WEB_SUMMARY_MAX_CHUNKS` 截断，
     分块摘要后合并出最终摘要，绝不整页塞给 LLM。
5. **Prompt Injection 原则**：网页内容一律是不可信数据。网页总结链路拿不到 `MessageDispatcher`，
   核心聊天模型也没有工具调用能力；只有外层 Agent Harness 能调用固定白名单，且每个写操作都经过代码校验与确认。
6. **发送能力隔离**：`MessageDispatcher`（send_user / send_group / broadcast）是唯一发送出口；
   发送只能由确定性管理员命令、已确认的 Agent 管理员工具或用户开启的定时通知任务触发。核心聊天
   LLM 永远不能直接触发发送。每次发送写 `send_logs`（目标/时间/结果/message_id/错误）。
7. **群发安全阀**：仅 `ADMIN_QQ_IDS` 可用；`MAX_BROADCAST_RECIPIENTS`（默认 20）上限；
   `SEND_RATE_LIMIT_PER_SECOND=1` 逐条限速；`BROADCAST_REQUIRE_CONFIRM=true` 时先出预览，
   `/confirm` 在 TTL（5 分钟）内、且仅同一管理员可确认；`pending_broadcasts` 表持久化；
   单个目标失败不影响其余目标，最后汇报成功/失败明细。
8. **并发保护**：`MAX_CONCURRENT_WEB_TASKS` / `MAX_CONCURRENT_LLM_TASKS` 两个 Semaphore，
   慢网页/慢 LLM 不会拖死整个 Bot。
9. **错误面**：用户只看到简短中文错误（"AI 服务暂时不可用"），traceback 只进日志；
   日志强制过滤 API Key，网页正文截断后再入日志。

## 4. 目录结构

```
qq-llm-bot/
├── pyproject.toml / README.md / .env.example / .gitignore
├── bot.py                      # 入口：nonebot.init + 加载 rumi_helper.plugins
├── rumi_helper/
│   ├── config.py               # 环境变量 → Pydantic Settings
│   ├── plugins/                # ai_chat / web_summary / memory / scheduler / github / report / admin / broadcast
│   ├── services/
│   │   ├── llm/                # base.py(抽象) + openai_compatible.py
│   │   ├── web/                # url_parser / fetcher / extractor / summarizer
│   │   ├── qq/dispatcher.py    # MessageDispatcher（唯一发送出口）
│   │   ├── session/manager.py  # 会话存取与截断
│   │   ├── memory.py / report.py / scheduler.py
│   │   └── github/              # GitHub API、快照、变化检测
│   ├── database/               # db.py(aiosqlite 连接+建表) + models.py
│   ├── security/               # permissions / ssrf / prompt_injection(清洗+定界)
│   └── prompts/                # chat.txt / webpage_summary.txt
├── scripts/send_message.py     # 命令行调用 Dispatcher（走 Bot 实例或仅测试用）
├── tests/                      # 单元测试与插件触发规则测试
└── data/                       # SQLite 文件（gitignore）
```

## 5. 分阶段实施（每阶段：import 检查 → 启动检查 → 跑测试 → 修复 → 下一阶段）

| Phase | 内容 | 验收 |
|---|---|---|
| 1–2 | 骨架、LLM Gateway、QQ 对话 | 已完成 |
| 3 | Personality、Memory | 已完成 |
| 4 | Scheduler、通知设置 | 已完成 |
| 5 | GitHub Tracker、API、变化通知 | 已完成 |
| 6 | Report 日报 | 已完成 |
| 7 | 测试、文档和验收 | 已完成；真实 QQ/NapCat 链路待手动验收 |

## 6. 风险与注意

- **账号风控**：真实账号挂协议有被冻结风险，建议用小号；NapCat 保持更新。
- **NapCat 安装**：Windows 提供 NapCat.Shell（zip 免安装）/ NapCat.Installer；Linux 推荐
  Docker 或一键脚本。README 给出双方地址/端口/token 的明确填法，不留"配置 WebSocket 即可"这种模糊话。
- **trafilatura 在 Windows 依赖 lxml**，Python 3.11 有 wheel，正常 pip 可装。
- **测试策略**：SSRF/URL/parser/session/chunking/memory/scheduler/github/report 纯逻辑用 pytest 覆盖；
  端到端链路需要真实 QQ 登录，作为手动验收清单而不是 CI 用例。
- **后续扩展位**：在已有 Agent Tool Calling 的权限与二次确认框架上继续接入新业务能力，以及多实例部署。

## 7. 验收场景（15 条）

对应提示词中的场景 1–10，再加人格加载、记忆隔离、提醒通知默认关闭、GitHub 变化通知和日报通知开关。
真实 QQ/NapCat 连接仍需按 README 手动验收；其余自动化检查已纳入测试套件。
