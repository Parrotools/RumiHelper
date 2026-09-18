import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Reply, Sender

from rumi_helper.plugins import ai_chat


def _private_event(message: Message, *, reply: Reply | None = None) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0,
        self_id=10000,
        post_type="message",
        sub_type="friend",
        user_id=20000,
        message_type="private",
        message_id=2,
        message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=20000, nickname="提问者"),
        reply=reply,
    )


def test_render_reply_message_keeps_text_and_describes_non_text_segments():
    message = Message(
        [
            MessageSegment.text("原消息"),
            MessageSegment.at(123),
            MessageSegment.image(file="image-id"),
            MessageSegment.face(14),
        ]
    )

    rendered = ai_chat._render_message_for_prompt(message)

    assert rendered == "原消息@123[图片][表情]"


@pytest.mark.asyncio
async def test_reply_context_uses_content_already_present_in_event():
    reply = Reply(
        time=1,
        message_type="private",
        message_id=41,
        real_id=41,
        sender=Sender(user_id=123, nickname="小明"),
        message=Message("被引用的内容"),
    )
    event = _private_event(Message([MessageSegment.reply(41), "这句话是什么意思？"]), reply=reply)

    prompt = await ai_chat._with_reply_context(event, "这句话是什么意思？")

    assert "发送者：小明（QQ：123）" in prompt
    assert "内容：被引用的内容" in prompt
    assert "【当前问题】\n这句话是什么意思？" in prompt


@pytest.mark.asyncio
async def test_reply_context_fetches_missing_content_by_message_id(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class FakeBot:
        async def call_api(self, action: str, **params):
            calls.append((action, params))
            return {
                "message_id": 42,
                "sender": {"user_id": 456, "nickname": "小红"},
                "message": [{"type": "text", "data": {"text": "需要补取的原消息"}}],
            }

    monkeypatch.setattr(ai_chat, "get_bot", lambda: FakeBot())
    event = _private_event(Message([MessageSegment.reply(42), "请解释"]), reply=None)

    prompt = await ai_chat._with_reply_context(event, "请解释")

    assert calls == [("get_msg", {"message_id": 42})]
    assert "小红（QQ：456）" in prompt
    assert "内容：需要补取的原消息" in prompt


@pytest.mark.asyncio
async def test_reply_context_fetch_failure_does_not_block_chat(monkeypatch):
    class FakeBot:
        async def call_api(self, action: str, **params):
            raise RuntimeError("暂时不可用")

    monkeypatch.setattr(ai_chat, "get_bot", lambda: FakeBot())
    event = _private_event(Message([MessageSegment.reply(42), "请解释"]))

    prompt = await ai_chat._with_reply_context(event, "请解释")

    assert prompt == "请解释"
