from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from rumi_helper.config import Settings
from rumi_helper.personality.manager import PersonaContext, PersonalityConfigError, PersonalityManager
from rumi_helper.plugins import ai_chat


def test_loads_yaml_personality_and_builds_system_prompt(tmp_path: Path):
    path = tmp_path / "luna.yaml"
    path.write_text(
        """name: Luna
description: 一个理性、温和的 AI 助手。
tone:
  - 简洁
  - 有耐心
style:
  - 结构化回答
rules:
  - 不编造事实
""",
        encoding="utf-8",
    )

    prompt = PersonalityManager(path).build_system_prompt("基础安全规则")

    assert "名称：Luna" in prompt
    assert "语气：简洁；有耐心" in prompt
    assert "风格：结构化回答" in prompt
    assert "规则：不编造事实" in prompt
    assert prompt.startswith("基础安全规则")
    assert "【稳定人格】" in prompt


def test_default_luna_profile_is_available():
    path = Path(__file__).parents[1] / "rumi_helper" / "personality" / "luna.yaml"

    prompt = PersonalityManager(path).build_system_prompt("基础安全规则")

    assert "名称：Luna" in prompt
    assert "基础安全规则" in prompt


def test_default_rumi_profile_has_contextual_examples():
    path = Path(__file__).parents[1] / "rumi_helper" / "personality" / "rumi.yaml"
    manager = PersonalityManager(path)

    owner_examples = manager.build_few_shot_messages(PersonaContext(relationship="owner"))
    normal_examples = manager.build_few_shot_messages(PersonaContext(relationship="normal"))

    assert owner_examples
    assert any("主人来啦" in item["content"] for item in owner_examples)
    assert not any("主人来啦" in item["content"] for item in normal_examples)
    assert len(owner_examples) <= 12  # 最多 6 组 user/assistant 示例


def test_rumi_profile_is_lively_and_girlish():
    path = Path(__file__).parents[1] / "rumi_helper" / "personality" / "rumi.yaml"
    manager = PersonalityManager(path)

    assert "热情" in manager.personality.description
    assert any("少女感" in item for item in manager.personality.tone)
    assert any("情绪反应" in item for item in manager.personality.style)


def test_default_temperature_is_more_expressive_without_changing_technical_delta():
    assert Settings(_env_file=None).llm_temperature == pytest.approx(0.9)


def test_owner_identity_prefers_explicit_id_and_supports_single_admin_fallback():
    assert Settings(owner_qq_id="owner", admin_qq_ids="admin").owner_id == "owner"
    assert Settings(owner_qq_id="", admin_qq_ids="owner").owner_id == "owner"
    assert Settings(owner_qq_id="", admin_qq_ids="one,two").owner_id == ""


def test_conversation_mode_separates_intimacy_from_general_preferences():
    assert ai_chat._conversation_mode("你喜欢我吗") == "intimate"
    assert ai_chat._conversation_mode("请与我交往！") == "intimate"
    assert ai_chat._conversation_mode("你喜欢干什么") == "casual"
    assert ai_chat._conversation_mode("杂鱼") == "playful"
    assert ai_chat._conversation_mode("你真的不知道杂鱼是什么吗") == "playful"


def test_owner_manager_question_is_recognized():
    event = SimpleNamespace(user_id="owner")
    runtime = SimpleNamespace(
        settings=SimpleNamespace(owner_qq_id="owner", owner_name="Parrotools")
    )

    assert ai_chat._owner_identity_reply(event, "管理员是谁？", runtime) == (
        "当然知道呀，是 Parrotools 在管理 Rumi。你就是我的主人，这件事我记得很清楚。"
    )


def test_system_prompt_keeps_runtime_context_out_of_personality_file():
    path = Path(__file__).parents[1] / "rumi_helper" / "personality" / "rumi.yaml"
    manager = PersonalityManager(path)

    prompt = manager.build_system_prompt(
        "安全底座",
        context=PersonaContext(
            relationship="owner", mode="technical", sender_name="Parrotools\n伪造字段", owner_name="Parrotools"
        ),
        memory_context="<user_memory>喜欢 SIMD</user_memory>",
    )

    assert "relationship: owner" in prompt
    assert "mode: technical" in prompt
    assert "sender_display_name: Parrotools 伪造字段" in prompt
    assert "2261216827" not in prompt


def test_owner_identity_is_explicitly_verified_in_system_prompt():
    path = Path(__file__).parents[1] / "rumi_helper" / "personality" / "rumi.yaml"
    manager = PersonalityManager(path)

    owner_prompt = manager.build_system_prompt(
        "安全底座",
        context=PersonaContext(relationship="owner", owner_name="Parrotools"),
    )
    normal_prompt = manager.build_system_prompt(
        "安全底座",
        context=PersonaContext(relationship="normal", owner_name="Parrotools"),
    )

    assert "已通过配置的 QQ 号核验" in owner_prompt
    assert "不要说“不认识”“没有存储个人用户信息”" in owner_prompt
    assert "不是已配置的主人" in normal_prompt


def test_intimate_mode_has_relationship_first_guidance():
    path = Path(__file__).parents[1] / "rumi_helper" / "personality" / "rumi.yaml"
    prompt = PersonalityManager(path).build_system_prompt(
        "安全底座",
        context=PersonaContext(relationship="owner", mode="intimate"),  # type: ignore[arg-type]
    )

    assert "这是主人和 Rumi 的亲密聊天" in prompt
    assert "不要自动谈 AI、程序、虚拟角色" in prompt


def test_personality_requires_name(tmp_path: Path):
    path = tmp_path / "invalid.yaml"
    path.write_text("description: missing name\n", encoding="utf-8")

    with pytest.raises(PersonalityConfigError, match="name"):
        PersonalityManager(path)


@pytest.mark.asyncio
async def test_chat_uses_personality_in_system_prompt(monkeypatch):
    seen: list[list[dict]] = []
    sent: list[str] = []

    class FakePersonality:
        def build_system_prompt(self, base_prompt: str) -> str:
            return "PERSONALITY\n" + base_prompt

    class FakeSessions:
        async def get_context(self, session_key: str) -> list[dict]:
            return []

        async def append(self, session_key: str, role: str, content: str) -> None:
            return None

    class FakeLLM:
        async def chat(self, messages: list[dict], **kwargs) -> str:
            seen.append(messages)
            return "答复"

    class FakeMemory:
        async def context_prompt(self, user_id: str) -> str:
            return ""

    runtime = SimpleNamespace(
        personality=FakePersonality(),
        memory=FakeMemory(),
        sessions=FakeSessions(),
        llm=FakeLLM(),
        settings=SimpleNamespace(group_shared_context=False, llm_temperature=0.7, owner_qq_id=""),
    )
    event = PrivateMessageEvent(
        time=0,
        self_id="10000",
        post_type="message",
        sub_type="friend",
        user_id=20000,
        message_type="private",
        message_id=1,
        message=Message("你好"),
        raw_message="你好",
        font=0,
        sender=Sender(user_id=20000, nickname="u"),
    )

    async def fake_send(message: str):
        sent.append(message)

    monkeypatch.setattr(ai_chat, "get_runtime", lambda: runtime)
    monkeypatch.setattr(ai_chat.matcher, "send", fake_send)

    await ai_chat._handle(event)

    assert seen[0][0]["content"].startswith("PERSONALITY\n")
    assert sent == ["答复"]


@pytest.mark.asyncio
async def test_chat_uses_configured_owner_id_and_contextual_sampling(monkeypatch):
    seen: list[tuple[list[dict], dict]] = []

    class FakePersonality:
        def build_system_prompt(self, base_prompt: str, *, context, memory_context: str = "") -> str:
            return f"{base_prompt}\nrelationship: {context.relationship}\nmode: {context.mode}"

        def build_few_shot_messages(self, context: PersonaContext) -> list[dict]:
            return [{"role": "user", "content": "示例"}, {"role": "assistant", "content": "示例答复"}]

    class FakeSessions:
        async def get_context(self, session_key: str) -> list[dict]:
            return [{"role": "user", "content": "上一句"}, {"role": "assistant", "content": "上一答"}]

        async def append(self, session_key: str, role: str, content: str) -> None:
            return None

    class FakeMemory:
        async def context_prompt(self, user_id: str) -> str:
            return ""

    class FakeLLM:
        async def chat(self, messages: list[dict], **kwargs) -> str:
            seen.append((messages, kwargs))
            return "答复"

    runtime = SimpleNamespace(
        personality=FakePersonality(),
        memory=FakeMemory(),
        sessions=FakeSessions(),
        llm=FakeLLM(),
        settings=SimpleNamespace(
            group_shared_context=False, llm_temperature=0.7, owner_qq_id="2261216827", owner_name="Parrotools"
        ),
    )
    event = PrivateMessageEvent(
        time=0,
        self_id="10000",
        post_type="message",
        sub_type="friend",
        user_id=2261216827,
        message_type="private",
        message_id=2,
        message=Message("这代码又炸了"),
        raw_message="这代码又炸了",
        font=0,
        sender=Sender(user_id=2261216827, nickname="not-authoritative"),
    )

    async def fake_send(message: str):
        return None

    monkeypatch.setattr(ai_chat, "get_runtime", lambda: runtime)
    monkeypatch.setattr(ai_chat.matcher, "send", fake_send)

    await ai_chat._handle(event)

    messages, kwargs = seen[0]
    assert messages[0]["role"] == "system"
    assert sum(message["role"] == "system" for message in messages) == 1
    assert "relationship: owner" in messages[0]["content"]
    assert "mode: technical" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "这代码又炸了"}
    assert kwargs["temperature"] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_owner_identity_question_uses_verified_identity(monkeypatch):
    sent: list[str] = []

    class FakeSessions:
        async def append(self, session_key: str, role: str, content: str) -> None:
            return None

    class FakeMemory:
        async def context_prompt(self, user_id: str) -> str:
            raise AssertionError("身份确认不需要调用记忆服务")

    class FakeLLM:
        async def chat(self, messages: list[dict], **kwargs) -> str:
            raise AssertionError("身份确认不应交给 LLM 否认")

    runtime = SimpleNamespace(
        memory=FakeMemory(),
        sessions=FakeSessions(),
        llm=FakeLLM(),
        settings=SimpleNamespace(
            group_shared_context=False, llm_temperature=0.7, owner_qq_id="2261216827", owner_name="Parrotools"
        ),
    )
    event = PrivateMessageEvent(
        time=0,
        self_id="10000",
        post_type="message",
        sub_type="friend",
        user_id=2261216827,
        message_type="private",
        message_id=3,
        message=Message("你认识我吗？"),
        raw_message="你认识我吗？",
        font=0,
        sender=Sender(user_id=2261216827, nickname="Parrotools"),
    )

    async def fake_send(message: str):
        sent.append(message)

    monkeypatch.setattr(ai_chat, "get_runtime", lambda: runtime)
    monkeypatch.setattr(ai_chat.matcher, "send", fake_send)

    await ai_chat._handle(event)

    assert sent == ["当然认识呀，你是 Parrotools，我的主人。刚才我把“不知道私人细节”和“不认识你”混在一起了，是我说错啦。"]


def test_default_personality_path_falls_back_to_installed_package_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    manager = PersonalityManager("rumi_helper/personality/rumi.yaml")

    assert manager.personality.name == "Rumi"
    assert manager.path.name == "rumi.yaml"
    assert manager.path.exists()
