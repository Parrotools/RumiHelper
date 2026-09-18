"""插件触发规则集成测试：用真实 OneBot v11 事件模型验证触发/去重逻辑。

覆盖验收场景 2/3/5/7 的"触发面"部分（权限与发送逻辑在服务层测试中覆盖）。
"""

import asyncio
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from rumi_helper.plugins import admin as admin_plugin
from rumi_helper.plugins import broadcast as broadcast_plugin
from rumi_helper.plugins import github as github_plugin
from rumi_helper.plugins import memory as memory_plugin
from rumi_helper.plugins import report as report_plugin
from rumi_helper.plugins import scheduler as scheduler_plugin
from rumi_helper.plugins import web_summary
from rumi_helper.plugins.ai_chat import HELP_TEXT
from rumi_helper.plugins.ai_chat import _meta_trigger as chat_meta_trigger
from rumi_helper.plugins.ai_chat import _trigger as chat_trigger

SELF_ID = "10000"


def _group_event(text: str, *, at: bool = False, to_me: bool | None = None, user: int = 20000,
                 mid: int = 1) -> GroupMessageEvent:
    msg = Message()
    if at:
        msg.append(MessageSegment.at(SELF_ID))
    msg.append(text)
    return GroupMessageEvent(
        time=0, self_id=SELF_ID, post_type="message", sub_type="normal",
        user_id=user, message_type="group", message_id=mid, group_id=30000,
        message=msg, raw_message=str(msg), font=0,
        sender=Sender(user_id=user, nickname="u"),
        to_me=at if to_me is None else to_me,
    )


def _private_event(text: str, *, user: int = 20000, mid: int = 1) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0, self_id=SELF_ID, post_type="message", sub_type="friend",
        user_id=user, message_type="private", message_id=mid,
        message=Message(text), raw_message=text, font=0,
        sender=Sender(user_id=user, nickname="u"),
    )


# ---------- ai_chat 触发规则 ----------

async def test_chat_private_always_triggers():
    assert await chat_trigger(_private_event("你好，你是谁", mid=101)) is True


async def test_chat_group_at_triggers():
    ev = _group_event(" 解释一下 Dijkstra", at=True, mid=102)
    assert await chat_trigger(ev) is True


async def test_chat_group_at_triggers_without_to_me_flag():
    ev = _group_event(" 解释一下 Dijkstra", at=True, to_me=False, mid=110)
    assert await chat_trigger(ev) is True


async def test_chat_group_textual_bot_nickname_triggers():
    ev = _group_event("@Rumi hello", at=False, to_me=False, mid=111)
    assert await chat_trigger(ev) is True
    assert await chat_trigger(_group_event("@Rume hello", at=False, to_me=False, mid=112)) is False


async def test_chat_group_reply_triggers():
    ev = _group_event("那时间复杂度呢", to_me=True, mid=103)
    assert await chat_trigger(ev) is True


async def test_chat_group_plain_message_ignored():
    """场景 3：群普通聊天不响应。"""
    assert await chat_trigger(_group_event("今天晚上吃什么", mid=104)) is False


async def test_chat_group_ai_command_requires_at():
    assert await chat_trigger(_group_event("/ai 写个俳句", mid=105)) is False
    assert await chat_trigger(_group_event("写个俳句", at=True, mid=106)) is True


async def test_chat_self_message_ignored():
    ev = _private_event("自言自语", user=int(SELF_ID), mid=106)
    assert await chat_trigger(ev) is False


async def test_chat_message_id_dedup():
    ev = _private_event("重复投递的消息", mid=107)
    assert await chat_trigger(ev) is True
    assert await chat_trigger(ev) is False  # 同一会话同一 message_id 第二次不处理


async def test_chat_dedup_scopes_same_message_id_by_conversation():
    first_private = _private_event("用户 A", user=20001, mid=5000)
    second_private = _private_event("用户 B", user=20002, mid=5000)
    group = _group_event("群消息", at=True, user=20001, mid=5000)

    assert await chat_trigger(first_private) is True
    assert await chat_trigger(second_private) is True
    assert await chat_trigger(group) is True

    assert await chat_trigger(first_private) is False
    assert await chat_trigger(second_private) is False
    assert await chat_trigger(group) is False


async def test_chat_ai_prefix_variants():
    assert await chat_trigger(_group_event("/airplane 模型", mid=108)) is False  # 不误匹配
    assert await chat_trigger(_group_event("/ai", mid=109)) is False
    assert await chat_trigger(_group_event("/ai", at=True, mid=113)) is True


async def test_chat_group_meta_commands_require_bot_mention():
    assert await chat_meta_trigger(_group_event("/help", mid=111)) is False
    assert await chat_meta_trigger(_group_event("@Rumi /help", to_me=False, mid=112)) is True


def test_shared_group_clear_requires_bot_admin():
    from rumi_helper.plugins.ai_chat import _can_clear_session

    runtime = SimpleNamespace(
        permission=SimpleNamespace(is_admin=lambda user_id: user_id == "admin")
    )

    assert _can_clear_session(runtime, "user-1", "group:30000") is False
    assert _can_clear_session(runtime, "admin", "group:30000") is True
    assert _can_clear_session(runtime, "user-1", "group:30000:user-1") is True
    assert _can_clear_session(runtime, "user-1", "private:user-1") is True


def test_help_text_describes_reminders_and_broadcast_confirmation():
    assert "/remind YYYY-MM-DD HH:MM -- 内容" in HELP_TEXT
    assert "/remind cron:0 6,18 * * * -- 内容" in HELP_TEXT
    assert "/schedule group:群号 YYYY-MM-DD HH:MM -- 内容" in HELP_TEXT
    assert "/schedule group:群号 cron:0 8 * * * -- 内容" in HELP_TEXT
    assert "/schedule joke group:群号 cron:0 12 * * * -- 主题" in HELP_TEXT
    assert "/notify reminder|github|report on|off" in HELP_TEXT
    assert "/github watch <URL> user:QQ号|group:群号" in HELP_TEXT
    assert "/report 或 /日报" in HELP_TEXT
    assert "/status 或 /状态" in HELP_TEXT
    assert "/broadcast user:QQ号,group:群号 -- 消息" in HELP_TEXT
    assert "/confirm —— 管理员确认执行群发" in HELP_TEXT
    assert "/cancel —— 管理员取消待确认的群发" in HELP_TEXT
    assert "/group-feedback on|off|status" in HELP_TEXT
    assert "/群聊反馈 开|关|状态" in HELP_TEXT


# ---------- web_summary 触发规则 ----------

def _patch_mode(monkeypatch, mode: str) -> None:
    stub = SimpleNamespace(settings=SimpleNamespace(url_auto_summary_mode=mode))
    monkeypatch.setattr(web_summary, "get_runtime", lambda: stub)


async def test_summary_command_always_triggers(monkeypatch):
    _patch_mode(monkeypatch, "mentioned")
    assert await web_summary._trigger(_private_event("/总结", mid=201)) is True  # 无 URL 也接住
    assert await web_summary._trigger(_group_event("/总结 https://example.com/a", mid=202)) is False
    assert await web_summary._trigger(_group_event("/总结 https://example.com/a", at=True, mid=203)) is True


async def test_summary_private_url_auto(monkeypatch):
    _patch_mode(monkeypatch, "mentioned")
    assert await web_summary._trigger(_private_event("看看这个 https://example.com/a", mid=214)) is True


async def test_summary_does_not_steal_other_slash_commands_with_urls(monkeypatch):
    _patch_mode(monkeypatch, "mentioned")
    assert await web_summary._trigger(
        _private_event("/github add https://github.com/OpenAI/openai-python", mid=204)
    ) is False


async def test_summary_does_not_steal_github_agent_operation_with_url(monkeypatch):
    _patch_mode(monkeypatch, "mentioned")
    assert await web_summary._trigger(
        _private_event("把 https://github.com/Parrotools/RumiHelper 加入我的仓库列表", mid=210)
    ) is False


async def test_summary_does_not_steal_other_agent_operations_with_urls(monkeypatch):
    _patch_mode(monkeypatch, "mentioned")
    cases = [
        "记住 https://example.com 是我的博客",
        "明天提醒我看 https://example.com",
        "把 https://example.com 发送给 user:123456",
        "把 https://example.com 群发给 group:30000",
    ]
    for offset, text in enumerate(cases, start=220):
        event = _private_event(text, mid=offset)
        assert await web_summary._trigger(event) is False
        assert await chat_trigger(event) is True


async def test_summary_still_handles_normal_url_summary_intent(monkeypatch):
    _patch_mode(monkeypatch, "mentioned")
    event = _private_event("帮我看看这篇文章 https://example.com/article", mid=224)
    assert await web_summary._trigger(event) is True


async def test_summary_group_url_requires_at(monkeypatch):
    """场景 5：@机器人 + URL 自动总结；未 @ 不自动。"""
    _patch_mode(monkeypatch, "mentioned")
    assert await web_summary._trigger(_group_event(" 这篇文章主要说什么 https://example.com/a", at=True, mid=204)) is True
    assert await web_summary._trigger(_group_event("看看 https://example.com/a", mid=205)) is False


async def test_summary_mode_all(monkeypatch):
    _patch_mode(monkeypatch, "all")
    assert await web_summary._trigger(_group_event("看看 https://example.com/a", mid=206)) is True


async def test_summary_mode_off(monkeypatch):
    _patch_mode(monkeypatch, "off")
    assert await web_summary._trigger(_private_event("看看这个 https://example.com/a", mid=207)) is False


async def test_summary_no_url_private(monkeypatch):
    _patch_mode(monkeypatch, "mentioned")
    assert await web_summary._trigger(_private_event("你好", mid=208)) is False


async def test_summary_self_ignored(monkeypatch):
    _patch_mode(monkeypatch, "all")
    assert await web_summary._trigger(_private_event("https://example.com", user=int(SELF_ID), mid=209)) is False


# ---------- broadcast 触发规则 ----------

async def test_broadcast_rule_matches_command():
    ev = _private_event("/broadcast user:123 -- hello", mid=301)
    assert await broadcast_plugin._broadcast_rule(ev) is True


async def test_broadcast_rule_rejects_other_text():
    assert await broadcast_plugin._broadcast_rule(_private_event("broadcast user:123", mid=302)) is False
    assert await broadcast_plugin._broadcast_rule(_private_event("/confirm", mid=303)) is False
    assert await broadcast_plugin._broadcast_rule(
        _private_event("/broadcasting user:123 -- hello", mid=307)
    ) is False


async def test_confirm_and_cancel_rules():
    assert await broadcast_plugin._confirm_rule(_private_event("/confirm", mid=304)) is True
    assert await broadcast_plugin._confirm_rule(_private_event("/confirm now", mid=305)) is False
    assert await broadcast_plugin._cancel_rule(_private_event("/cancel", mid=306)) is True


# ---------- GitHub / Report 触发规则 ----------

async def test_deterministic_private_commands_are_delegated_from_chat():
    assert await chat_trigger(_private_event("/github list", mid=350)) is False
    assert await chat_trigger(_private_event("/report", mid=351)) is False
    assert await github_plugin._github_rule(_private_event("/github list", mid=352)) is True
    assert await report_plugin._report_rule(_private_event("/report", mid=353)) is True


async def test_schedule_rule_returns_false_for_non_matching_private_message():
    assert await scheduler_plugin._schedule_rule(_private_event("你好", mid=354)) is False


async def test_group_commands_require_explicit_bot_mention():
    assert await admin_plugin._status_trigger(_group_event("/status", mid=360)) is False
    assert await broadcast_plugin._broadcast_rule(_group_event("/broadcast user:123 -- hi", mid=361)) is False
    assert await github_plugin._github_rule(_group_event("/github list", mid=362)) is False
    assert await memory_plugin._remember_rule(_group_event("/remember 这是事实", mid=363)) is False
    assert await memory_plugin._list_rule(_group_event("/memory", mid=364)) is False
    assert await scheduler_plugin._remind_rule(_group_event("/remind tomorrow -- hi", mid=365)) is False
    assert await scheduler_plugin._notify_rule(_group_event("/notify reminder on", mid=366)) is False
    assert await scheduler_plugin._schedule_rule(_group_event("/schedule group:30000 cron:0 8 * * * -- hi", mid=369)) is False

    assert await admin_plugin._status_trigger(_group_event("/status", at=True, mid=367)) is True
    assert await github_plugin._github_rule(_group_event("/github list", at=True, mid=368)) is True
    assert await scheduler_plugin._schedule_rule(
        _group_event("/schedule group:30000 cron:0 8 * * * -- hi", at=True, mid=370)
    ) is True


# ---------- admin 触发规则 ----------

async def test_status_rule():
    assert await admin_plugin._status_trigger(_private_event("/status", mid=401)) is True
    assert await admin_plugin._status_trigger(_private_event("/状态", mid=402)) is True
    assert await admin_plugin._status_trigger(_private_event("status", mid=403)) is False


# ---------- 长回复截断 ----------

def test_truncate_for_qq():
    from rumi_helper.utils import truncate_for_qq

    assert truncate_for_qq("short") == "short"
    long = "x" * 5000
    out = truncate_for_qq(long)
    assert len(out) < 5000
    assert "已截断" in out


async def test_summary_success_uses_shared_reply_gate(monkeypatch):
    sent: list[str] = []

    async def fake_fetch_document(runtime, url):
        return object()

    class FakeSummarizer:
        async def summarize(self, doc):
            return "summary"

    async def fake_send_reply(matcher, runtime, message):
        sent.append(message)

    runtime = SimpleNamespace(
        web_semaphore=asyncio.Semaphore(1),
        summarizer=FakeSummarizer(),
    )
    matcher_without_direct_send = SimpleNamespace()

    monkeypatch.setattr(web_summary, "_fetch_document", fake_fetch_document)
    monkeypatch.setattr(web_summary, "send_reply", fake_send_reply)

    await web_summary._summarize_one(
        matcher_without_direct_send,
        runtime,
        "https://example.com/article",
    )

    assert sent == ["summary"]


async def test_public_status_skips_operational_diagnostics(monkeypatch):
    calls: list[bool] = []
    sent: list[str] = []

    class FakeHealth:
        async def check(self, *, include_diagnostics: bool = True):
            calls.append(include_diagnostics)
            return {
                "uptime_seconds": 1,
                "database": {"ok": True},
                "llm": {"ok": True},
                "github": {"ok": True},
                "scheduler": {"running": True, "jobs": 0},
            }

    runtime = SimpleNamespace(
        health=FakeHealth(),
        permission=SimpleNamespace(is_admin=lambda _user_id: False),
    )

    async def fake_send_local_reply(_matcher, _runtime, message):
        sent.append(message)

    monkeypatch.setattr(admin_plugin, "get_runtime", lambda: runtime)
    monkeypatch.setattr(admin_plugin, "send_local_reply", fake_send_local_reply)

    await admin_plugin._handle(_private_event("/status", mid=404), SimpleNamespace())

    assert calls == [False]
    assert sent and "服务：正常" in sent[0]


async def test_admin_status_requests_operational_diagnostics(monkeypatch):
    calls: list[bool] = []

    class FakeHealth:
        async def check(self, *, include_diagnostics: bool = True):
            calls.append(include_diagnostics)
            return {
                "uptime_seconds": 1,
                "database": {"ok": True},
                "llm": {"ok": True},
                "github": {"ok": True},
                "scheduler": {"running": True, "jobs": 0},
                "counts": {},
                "recent_errors": [],
            }

    runtime = SimpleNamespace(
        health=FakeHealth(),
        permission=SimpleNamespace(is_admin=lambda _user_id: True),
    )

    async def fake_send_local_reply(_matcher, _runtime, _message):
        return None

    monkeypatch.setattr(admin_plugin, "get_runtime", lambda: runtime)
    monkeypatch.setattr(admin_plugin, "send_local_reply", fake_send_local_reply)

    await admin_plugin._handle(_private_event("/status", mid=405), SimpleNamespace())

    assert calls == [True]
