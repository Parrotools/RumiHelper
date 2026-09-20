from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.exception import IgnoredException

from rumi_helper.database.db import Database
from rumi_helper.plugins import event_dedup


def _private_event(message_id: int = 1) -> PrivateMessageEvent:
    text = "dedup"
    return PrivateMessageEvent(
        time=0,
        self_id="10000",
        post_type="message",
        sub_type="friend",
        user_id=20000,
        message_type="private",
        message_id=message_id,
        message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender(user_id=20000, nickname="u"),
    )


async def test_global_preprocessor_persists_message_event_claim(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "global-event-dedup.db"))
    await db.connect()
    try:
        monkeypatch.setattr(
            event_dedup,
            "get_runtime",
            lambda: SimpleNamespace(db=db),
        )
        event = _private_event(700001)

        await event_dedup.deduplicate_message_event(event)

        with pytest.raises(IgnoredException):
            await event_dedup.deduplicate_message_event(event)
    finally:
        await db.close()


async def test_global_preprocessor_fails_closed_if_dedup_database_fails(monkeypatch):
    class BrokenDB:
        async def claim_message_event(self, _key: str) -> bool:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        event_dedup,
        "get_runtime",
        lambda: SimpleNamespace(db=BrokenDB()),
    )

    with pytest.raises(IgnoredException):
        await event_dedup.deduplicate_message_event(_private_event(700002))


async def test_global_preprocessor_fails_closed_before_runtime_is_ready(monkeypatch):
    def no_runtime():
        raise RuntimeError("not ready")

    monkeypatch.setattr(event_dedup, "get_runtime", no_runtime)

    with pytest.raises(IgnoredException):
        await event_dedup.deduplicate_message_event(_private_event(700003))


async def test_global_preprocessor_ignores_non_message_event(monkeypatch):
    def should_not_be_called():
        raise AssertionError("non-message events must not touch Runtime")

    monkeypatch.setattr(event_dedup, "get_runtime", should_not_be_called)

    await event_dedup.deduplicate_message_event(object())


def _group_event(
    text: str,
    *,
    message_id: int,
    to_me: bool = False,
    user_id: int = 20000,
):
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    return GroupMessageEvent(
        time=0,
        self_id="10000",
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=message_id,
        group_id=30000,
        message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender(user_id=user_id, nickname="u"),
        to_me=to_me,
    )


class RecordingDedupDB:
    def __init__(self):
        self.keys: list[str] = []

    async def claim_message_event(self, key: str) -> bool:
        self.keys.append(key)
        return True


async def test_global_preprocessor_skips_unaddressed_plain_group_chat(monkeypatch):
    db = RecordingDedupDB()
    runtime = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(url_auto_summary_mode="mentioned"),
    )
    monkeypatch.setattr(event_dedup, "get_runtime", lambda: runtime)

    await event_dedup.deduplicate_message_event(
        _group_event("今天晚上吃什么", message_id=700010)
    )

    assert db.keys == []


async def test_global_preprocessor_claims_addressed_group_chat(monkeypatch):
    db = RecordingDedupDB()
    runtime = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(url_auto_summary_mode="mentioned"),
    )
    monkeypatch.setattr(event_dedup, "get_runtime", lambda: runtime)

    await event_dedup.deduplicate_message_event(
        _group_event("解释一下", message_id=700011, to_me=True)
    )

    assert len(db.keys) == 1


async def test_global_preprocessor_claims_unaddressed_group_url_in_all_mode(monkeypatch):
    db = RecordingDedupDB()
    runtime = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(url_auto_summary_mode="all"),
    )
    monkeypatch.setattr(event_dedup, "get_runtime", lambda: runtime)

    await event_dedup.deduplicate_message_event(
        _group_event("看看 https://example.com/page", message_id=700012)
    )

    assert len(db.keys) == 1


async def test_global_preprocessor_skips_own_group_message(monkeypatch):
    db = RecordingDedupDB()
    runtime = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(url_auto_summary_mode="all"),
    )
    monkeypatch.setattr(event_dedup, "get_runtime", lambda: runtime)

    await event_dedup.deduplicate_message_event(
        _group_event(
            "https://example.com/page",
            message_id=700013,
            to_me=True,
            user_id=10000,
        )
    )

    assert db.keys == []
