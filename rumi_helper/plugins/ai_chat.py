"""AI 聊天插件：私聊直接进入 LLM；群聊仅 @机器人触发；命令需带 / 前缀。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from nonebot import get_bot, logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from rumi_helper.plugins import chat_message_utils as _message_utils
from rumi_helper.plugins import chat_persona as _persona
from rumi_helper.security.permissions import shared_session_clear_requires_admin
from rumi_helper.services.llm.base import LLMError
from rumi_helper.services.runtime import get_runtime
from rumi_helper.services.session.manager import SessionManager
from rumi_helper.utils import send_local_reply, send_reply, truncate_for_qq

# Compatibility re-exports: other plugins and tests historically import these
# helpers from rumi_helper.plugins.ai_chat.
_claim_event = _message_utils.claim_event
command_is_addressed = _message_utils.command_is_addressed
_has_bot_mention = _message_utils.has_bot_mention
_is_addressed_to_bot = _message_utils.is_addressed_to_bot
_is_self = _message_utils.is_self
_render_message_for_prompt = _message_utils.render_message_for_prompt
strip_bot_mention = _message_utils.strip_bot_mention
_message_with_reply_context = _message_utils.with_reply_context

CHAT_SYSTEM_PROMPT = _persona.CHAT_SYSTEM_PROMPT
_build_chat_messages = _persona.build_chat_messages
_build_system_prompt = _persona.build_system_prompt
_conversation_mode = _persona.conversation_mode
_is_owner = _persona.is_owner
_owner_identity_reply = _persona.owner_identity_reply
_persona_context = _persona.persona_context
_temperature = _persona.temperature

# 已知命令白名单：私聊中其他 "/xxx" 一律视为未知命令，而不是丢给 LLM
KNOWN_COMMANDS = {
    "/ai", "/clear", "/总结", "/summary", "/broadcast",
    "/confirm", "/cancel", "/status", "/状态", "/help", "/帮助", "/remember", "/memories", "/memory",
    "/remind", "/notify", "/schedule", "/github", "/report", "/日报", "/agent",
    "/group-feedback", "/群聊反馈",
}
_DELEGATED_COMMANDS = {
    "/总结", "/summary", "/broadcast", "/confirm", "/cancel", "/status", "/状态",
    "/remember", "/memories", "/memory", "/remind", "/notify", "/schedule", "/github", "/report", "/日报", "/agent",
    "/group-feedback", "/群聊反馈",
}

HELP_TEXT = (
    "可用命令：\n"
    "/ai <问题> —— 进入 AI 对话（私聊或群聊）\n"
    "/clear —— 清空当前会话上下文（共享群上下文仅 Bot 管理员可清空）\n"
    "/总结 <URL> 或 /summary <URL> —— 总结网页（一次最多 3 个 URL）\n"
    "/help 或 /帮助 —— 显示本帮助\n"
    "/remember [类型] -- 内容 —— 保存长期记忆\n"
    "/memory 或 /memories —— 查看自己的长期记忆\n"
    "/remind YYYY-MM-DD HH:MM -- 内容 —— 创建一次性提醒自己\n"
    "/remind cron:0 6,18 * * * -- 内容 —— 创建每天重复提醒自己\n"
    "/schedule group:群号 YYYY-MM-DD HH:MM -- 内容 —— 管理员一次性定时发到群\n"
    "/schedule group:群号 cron:0 8 * * * -- 内容 —— 管理员周期性发到群\n"
    "/schedule joke group:群号 cron:0 12 * * * -- 主题 —— 管理员定时生成主题段子\n"
    "/notify reminder|github|report on|off —— 开关提醒、GitHub 或日报通知\n"
    "/github add|remove|list|check|info —— GitHub 仓库监控\n"
    "也可以直接描述 GitHub、记忆、提醒、通知、定时任务、日报、状态等操作；修改或发送操作会先请求确认\n"
    "自然语言示例：记住我喜欢 HPC；每天 12 点在群里讲 mobile 段子；查看我的日报\n"
    "/agent confirm|cancel —— 确认或取消自然语言操作\n"
    "/github watch <URL> user:QQ号|group:群号 —— 仓库变化通知目标\n"
    "/github digest set user:QQ号1,user:QQ号2,group:群号 —— 设置定时汇总目标\n"
    "/github digest list|clear —— 查看或清空定时汇总目标\n"
    "/group-feedback on|off|status —— 管理员开关当前群普通成员的反馈\n"
    "/群聊反馈 开|关|状态 —— 上述命令的中文写法\n"
    "/report 或 /日报 —— 查看今日汇总\n"
    "/status 或 /状态 —— 查看运行状态\n"
    "/broadcast user:QQ号,group:群号 -- 消息 —— 管理员群发（先预览）\n"
    "/confirm —— 管理员确认执行群发\n"
    "/cancel —— 管理员取消待确认的群发\n"
    "群聊命令需先 @Rumi，例如：@Rumi /help；私聊无需 @。\n"
    "私聊直接发消息即可对话；群里 @我 或回复我也可以。"
)

async def _with_reply_context(event: MessageEvent, text: str) -> str:
    return await _message_with_reply_context(
        event,
        text,
        bot_getter=get_bot,
    )


def _session_key(event: MessageEvent) -> str:
    runtime = get_runtime()
    if isinstance(event, GroupMessageEvent):
        return SessionManager.group_key(
            str(event.group_id), str(event.user_id), runtime.settings.group_shared_context
        )
    return SessionManager.private_key(str(event.user_id))


@asynccontextmanager
async def _session_turn(sessions, session_key: str) -> AsyncIterator[None]:
    """Use SessionManager turn locking while keeping lightweight test doubles compatible."""
    turn = getattr(sessions, "turn", None)
    if turn is None:
        yield
        return
    async with turn(session_key):
        yield


def _can_clear_session(runtime, user_id: str, session_key: str) -> bool:
    if not shared_session_clear_requires_admin(session_key):
        return True
    return runtime.permission.is_admin(str(user_id))


async def _append_turn(sessions, session_key: str, user_text: str, assistant_text: str) -> None:
    """Persist a full turn atomically when supported by the concrete SessionManager."""
    append_turn = getattr(sessions, "append_turn", None)
    if append_turn is not None:
        await append_turn(session_key, user_text, assistant_text)
        return
    await sessions.append(session_key, "user", user_text)
    await sessions.append(session_key, "assistant", assistant_text)


async def _trigger(event: MessageEvent) -> bool:
    if _is_self(event):
        return False  # 过滤自己的消息，防循环
    text = strip_bot_mention(event.message.extract_plain_text())
    first_token = text.split(maxsplit=1)[0].lower() if text else ""
    if first_token in _DELEGATED_COMMANDS:
        return False
    if isinstance(event, GroupMessageEvent):
        # 群聊：仅 @机器人 / 回复机器人（to_me）或 "/ai ..." 触发
        matched = _is_addressed_to_bot(event)
    else:
        matched = True  # 私聊默认直接进入 LLM
    if matched:
        return _claim_event(event)
    return False


matcher = on_message(rule=Rule(_trigger), priority=10, block=True)


@matcher.handle()
async def _handle(event: MessageEvent):
    runtime = get_runtime()
    session_key = _session_key(event)
    text = strip_bot_mention(event.message.extract_plain_text())

    # 私聊与群聊统一支持 /ai 前缀
    if text == "/ai" or text.startswith("/ai "):
        text = text[len("/ai"):].strip()

    first_token = text.split(maxsplit=1)[0].lower() if text else ""
    if first_token in ("/help", "/帮助"):
        await send_local_reply(matcher, runtime, HELP_TEXT)
        return
    if text.startswith("/") and first_token not in KNOWN_COMMANDS:
        await send_local_reply(matcher, runtime, f"未知命令 {first_token}。\n\n{HELP_TEXT}")
        return
    if text == "/clear":
        if not _can_clear_session(runtime, str(event.user_id), session_key):
            await send_local_reply(matcher, runtime, "共享群会话仅 Bot 管理员可清空。")
            return
        async with _session_turn(runtime.sessions, session_key):
            await runtime.sessions.clear(session_key)
            await send_local_reply(matcher, runtime, "当前会话已清空。")
        return
    if not text:
        await send_local_reply(matcher, runtime, "有什么想问的？直接发消息，或用 /ai <问题>。")
        return

    async with _session_turn(runtime.sessions, session_key):
        identity_reply = _owner_identity_reply(event, text, runtime)
        if identity_reply is not None:
            await _append_turn(runtime.sessions, session_key, text, identity_reply)
            await send_local_reply(matcher, runtime, identity_reply)
            return

        history = await runtime.sessions.get_context(session_key)
        agent = getattr(runtime, "agent", None)
        if getattr(runtime.settings, "agent_tool_calling_enabled", True) and agent is not None:
            agent_result = await agent.try_handle(
                runtime=runtime,
                event=event,
                text=text,
                history=history,
            )
            if agent_result is not None and agent_result.handled:
                await _append_turn(
                    runtime.sessions,
                    session_key,
                    text,
                    agent_result.message,
                )
                await send_local_reply(matcher, runtime, agent_result.message)
                return

        memory_context = await runtime.memory.context_prompt(str(event.user_id))
        prompt_text = await _with_reply_context(event, text)
        messages, context = _build_chat_messages(
            runtime,
            event,
            prompt_text,
            history,
            memory_context,
            context_text=text,
        )

        logger.info(
            "LLM chat session=%s user=%s group=%s msg=%s",
            session_key,
            event.user_id,
            getattr(event, "group_id", "-"),
            event.message_id,
        )
        try:
            reply = await runtime.llm.chat(
                messages,
                temperature=_temperature(runtime, context.mode),
            )
        except LLMError:
            logger.exception("LLM 调用失败 session=%s", session_key)
            await send_reply(matcher, runtime, "AI 服务暂时不可用，请稍后再试。")
            return

        # 成功后才落库，失败轮次不污染上下文
        await _append_turn(runtime.sessions, session_key, prompt_text, reply)
        await send_reply(matcher, runtime, truncate_for_qq(reply))


# 群聊轻量命令：/clear、/help 也必须 @ 机器人
async def _meta_trigger(event: GroupMessageEvent) -> bool:
    if _is_self(event):
        return False
    if not _is_addressed_to_bot(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text())
    if text in ("/clear", "/help", "/帮助"):
        return _claim_event(event)
    return False


meta_matcher = on_message(rule=Rule(_meta_trigger), priority=9, block=True)


@meta_matcher.handle()
async def _handle_meta(event: GroupMessageEvent):
    runtime = get_runtime()
    text = strip_bot_mention(event.message.extract_plain_text())
    if text == "/clear":
        session_key = _session_key(event)
        if not _can_clear_session(runtime, str(event.user_id), session_key):
            await send_local_reply(meta_matcher, runtime, "共享群会话仅 Bot 管理员可清空。")
            return
        async with _session_turn(runtime.sessions, session_key):
            await runtime.sessions.clear(session_key)
            await send_local_reply(meta_matcher, runtime, "当前会话已清空。")
    else:
        await send_local_reply(meta_matcher, runtime, HELP_TEXT)


# 供其他插件复用的去重声明
claim_message_id = _message_utils.claim_message_id
claim_event = _claim_event
