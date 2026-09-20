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
