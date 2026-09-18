# QQ LLM Bot 协作入口

- 定位：NapCatQQ + OneBot 11 + NoneBot2 的个人 QQ AI Bot，提供 LLM 对话、人格、记忆、网页总结、GitHub 监控、提醒和日报。
- 启动：复制 `.env.example` 为 `.env`，填写 OneBot token、LLM API Key；运行 `python bot.py`。
- 验证：运行 `pytest` 和 `ruff check app tests`；真实 QQ/NapCat 链路只能手动验收。
- 技术栈：Python 3.11+、asyncio、NoneBot2、httpx、aiosqlite、SQLite、APScheduler。
- 目录：`app/plugins` 负责事件入口，`app/services` 负责业务；`app/database/db.py` 只管理连接与 migration，领域 SQL 拆在 `app/database/repositories`；`app/security` 负责权限/SSRF/注入防护。
- 约定：业务层 ID 使用 `str`；数据库访问集中在 `Database`；所有主动 QQ 发送经过 `MessageDispatcher`；密钥只来自环境变量，不写日志。
- 当前状态（2026-09-08）：Personality、Memory、Scheduler、GitHub Tracker、日报、Agent Harness 和对应测试已实现；真实 QQ/NapCat 链路已手动验收。
- 下一步：在真实 QQ/NapCat 上验收全部自然语言工具；新增业务能力继续复用白名单、权限与确认模式。
