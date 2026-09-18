"""Persona selection and prompt construction helpers for AI chat."""

from nonebot.adapters.onebot.v11 import MessageEvent

from app.personality.manager import PersonaContext
from app.promptlib import load_prompt

CHAT_SYSTEM_PROMPT = load_prompt("chat.txt")

_TECHNICAL_HINTS = (
    "代码", "报错", "bug", "debug", "编译", "运行", "算法", "复杂度", "排序", "函数", "类", "接口",
    "python", "java", "javascript", "typescript", "c++", "rust", "go语言", "linux", "git", "github",
    "sql", "数据库", "网络", "协议", "并发", "线程", "进程", "性能", "优化", "部署", "命令行",
    "traceback", "exception", "error", "benchmark", "cuda", "mpi", "openmp", "simd",
)
_INTIMATE_HINTS = (
    "喜欢我", "喜欢你", "爱我", "爱你", "想我", "想你", "交往", "在一起", "约会", "表白", "告白",
    "男朋友", "女朋友", "对象", "抱抱", "亲亲", "亲一下", "陪我睡", "想和你",
)
_PLAYFUL_HINTS = ("杂鱼", "笨蛋", "笨猫", "小笨蛋", "菜狗", "菜鸡")
_OWNER_IDENTITY_QUESTIONS = (
    "你认识我吗",
    "你还认识我吗",
    "你记得我吗",
    "你还记得我吗",
    "你知道我是谁吗",
    "我是谁",
    "我是你的主人吗",
    "你的主人是谁",
    "谁是你的主人",
)
_OWNER_MANAGER_HINTS = (
    "谁管理你",
    "谁在管理你",
    "谁管理着你",
    "谁管你",
    "谁在管你",
    "由谁管理",
    "你由谁管理",
    "你是由谁管理的",
    "你是谁管理的",
    "管理员是谁",
    "管理者是谁",
    "谁是管理员",
    "管理你的人是谁",
    "你的管理员",
    "谁管理这个机器人",
    "谁管理这个bot",
    "谁管理rumi",
    "谁在负责管理你",
)


def is_owner(event: MessageEvent, runtime) -> bool:
    configured_owner = getattr(runtime.settings, "owner_id", "")
    if not configured_owner:
        configured_owner = getattr(runtime.settings, "owner_qq_id", "")
    return (
        bool(str(configured_owner).strip())
        and str(event.user_id) == str(configured_owner).strip()
    )


def conversation_mode(text: str) -> str:
    lowered = text.lower()
    if any(hint in lowered for hint in _INTIMATE_HINTS):
        return "intimate"
    if any(hint in lowered for hint in _PLAYFUL_HINTS):
        return "playful"
    return (
        "technical"
        if any(hint in lowered for hint in _TECHNICAL_HINTS)
        else "casual"
    )


def persona_context(
    event: MessageEvent,
    text: str,
    runtime,
) -> PersonaContext:
    sender = getattr(event, "sender", None)
    sender_name = (
        getattr(sender, "card", None)
        or getattr(sender, "nickname", "")
        or ""
    )
    settings = runtime.settings
    return PersonaContext(
        relationship="owner" if is_owner(event, runtime) else "normal",
        mode=conversation_mode(text),
        conversation_mood="normal",
        sender_name=str(sender_name),
        owner_name=str(
            getattr(settings, "owner_name", "Parrotools")
            or "Parrotools"
        ),
    )


def owner_identity_reply(
    event: MessageEvent,
    text: str,
    runtime,
) -> str | None:
    if not is_owner(event, runtime):
        return None
    normalized = "".join(text.lower().split()).rstrip("?？！!。~～")
    is_identity_question = normalized in _OWNER_IDENTITY_QUESTIONS
    is_manager_question = any(
        hint in normalized for hint in _OWNER_MANAGER_HINTS
    )
    if not is_identity_question and not is_manager_question:
        return None

    owner_name = " ".join(
        str(
            getattr(runtime.settings, "owner_name", "")
            or "Parrotools"
        ).split()
    )
    owner_name = owner_name[:64] or "Parrotools"
    if is_manager_question:
        return (
            f"当然知道呀，是 {owner_name} 在管理 Rumi。"
            "你就是我的主人，这件事我记得很清楚。"
        )
    return (
        f"当然认识呀，你是 {owner_name}，我的主人。"
        "刚才我把“不知道私人细节”和“不认识你”混在一起了，是我说错啦。"
    )


def temperature(runtime, mode: str) -> float:
    base = float(getattr(runtime.settings, "llm_temperature", 0.9))
    base = max(0.0, min(2.0, base))
    delta = 0.1 if mode in {"casual", "intimate", "playful"} else -0.1
    return max(0.0, min(2.0, base + delta))


def build_system_prompt(
    runtime,
    context: PersonaContext,
    memory_context: str,
) -> str:
    builder = runtime.personality.build_system_prompt
    try:
        return builder(
            CHAT_SYSTEM_PROMPT,
            context=context,
            memory_context=memory_context,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        prompt = builder(CHAT_SYSTEM_PROMPT)
        return (
            f"{prompt}\\n\\n{memory_context.strip()}"
            if memory_context.strip()
            else prompt
        )


def build_chat_messages(
    runtime,
    event: MessageEvent,
    text: str,
    history: list[dict],
    memory_context: str,
    *,
    context_text: str | None = None,
) -> tuple[list[dict], PersonaContext]:
    context = persona_context(
        event,
        context_text if context_text is not None else text,
        runtime,
    )
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                runtime,
                context,
                memory_context,
            ),
        }
    ]
    few_shot_builder = getattr(
        runtime.personality,
        "build_few_shot_messages",
        None,
    )
    if few_shot_builder is not None:
        messages.extend(few_shot_builder(context))
    messages.extend(history)
    messages.append({"role": "user", "content": text})
    return messages, context
