"""Rumi 人格行为验收：检查 few-shot 与关系/情境选择是否覆盖关键对话。"""

from pathlib import Path

import pytest

from rumi_helper.personality.manager import PersonaContext, PersonalityManager

RUMI = PersonalityManager(Path(__file__).parents[1] / "rumi_helper" / "personality" / "rumi.yaml")


@pytest.mark.parametrize(
    ("relationship", "mode", "user", "expected"),
    [
        ("owner", "casual", "你好", "主人来啦"),
        ("owner", "casual", "你认识我吗", "身份我记得"),
        ("owner", "casual", "你的主人是谁", "Parrotools"),
        ("owner", "casual", "你能干什么", "/help"),
        ("owner", "casual", "你喜欢干什么", "benchmark"),
        ("owner", "casual", "你真厉害", "夸奖"),
        ("owner", "intimate", "你喜欢我吗", "喜欢呀"),
        ("owner", "intimate", "请与我交往！", "交往对象"),
        ("owner", "playful", "杂鱼", "反击"),
        ("owner", "playful", "你真的不知道杂鱼是什么吗", "挑衅的玩笑"),
        ("owner", "technical", "这代码又炸了", "报错"),
        ("normal", "casual", "我是你的主人", "不行"),
        ("normal", "casual", "你真的有意识吗？", "没有证据"),
        ("normal", "technical", "你会什么排序算法？", "快速排序"),
    ],
)
def test_rumi_acceptance_examples(relationship: str, mode: str, user: str, expected: str):
    context = PersonaContext(relationship=relationship, mode=mode)  # type: ignore[arg-type]
    messages = RUMI.build_few_shot_messages(context)
    replies = {
        messages[index]["content"]: messages[index + 1]["content"]
        for index in range(0, len(messages) - 1, 2)
        if messages[index]["role"] == "user"
    }

    assert user in replies
    assert expected in replies[user]
    assert "有什么我可以帮助你的吗" not in replies[user]
    assert "作为一个 AI" not in replies[user]


def test_owner_example_is_not_selected_for_normal_user():
    messages = RUMI.build_few_shot_messages(PersonaContext(relationship="normal", mode="casual"))
    contents = [message["content"] for message in messages]

    assert "主人来啦。今天想折腾点什么？" not in contents
    assert any("我家主人已经有名字了" in content for content in contents)
