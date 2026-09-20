import asyncio

import rumi_helper.database.repositories.message_events as message_events_repository
from rumi_helper.database.db import Database


async def test_message_event_claim_across_connections_has_single_winner(tmp_path):
    path = str(tmp_path / "event-dedup.db")
    first = Database(path)
    second = Database(path)
    await first.connect()
    await second.connect()
    try:
        results = await asyncio.gather(
            first.claim_message_event("10000:private:20000:123"),
            second.claim_message_event("10000:private:20000:123"),
        )

        assert sorted(results) == [False, True]
        assert await first.fetchone(
            "SELECT COUNT(*) AS count FROM processed_message_events",
            (),
        ) == {"count": 1}
    finally:
        await first.close()
        await second.close()


async def test_message_event_claim_survives_process_restart(tmp_path):
    path = str(tmp_path / "event-restart.db")
    first = Database(path)
    await first.connect()
    try:
        assert await first.claim_message_event("10000:private:20000:456") is True
    finally:
        await first.close()

    restarted = Database(path)
    await restarted.connect()
    try:
        assert await restarted.claim_message_event("10000:private:20000:456") is False
    finally:
        await restarted.close()


async def test_message_event_history_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        message_events_repository,
        "_MESSAGE_EVENT_HISTORY_LIMIT",
        2,
    )
    monkeypatch.setattr(
        message_events_repository,
        "_MESSAGE_EVENT_PRUNE_INTERVAL",
        1,
    )
    db = Database(str(tmp_path / "event-history.db"))
    await db.connect()
    try:
        for index in range(4):
            assert await db.claim_message_event(f"event-{index}") is True

        rows = await db.fetchall(
            "SELECT dedup_key FROM processed_message_events ORDER BY id",
            (),
        )
        assert rows == [
            {"dedup_key": "event-2"},
            {"dedup_key": "event-3"},
        ]
        assert await db.claim_message_event("event-2") is False
    finally:
        await db.close()


async def test_empty_message_event_key_is_rejected(tmp_path):
    db = Database(str(tmp_path / "event-empty.db"))
    await db.connect()
    try:
        assert await db.claim_message_event("") is False
        assert await db.fetchone(
            "SELECT COUNT(*) AS count FROM processed_message_events",
            (),
        ) == {"count": 0}
    finally:
        await db.close()


async def test_message_event_history_prunes_in_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(
        message_events_repository,
        "_MESSAGE_EVENT_HISTORY_LIMIT",
        2,
    )
    monkeypatch.setattr(
        message_events_repository,
        "_MESSAGE_EVENT_PRUNE_INTERVAL",
        4,
    )
    db = Database(str(tmp_path / "event-batch-prune.db"))
    await db.connect()
    try:
        for index in range(3):
            assert await db.claim_message_event(f"batched-{index}") is True

        assert await db.fetchone(
            "SELECT COUNT(*) AS count FROM processed_message_events",
            (),
        ) == {"count": 3}

        assert await db.claim_message_event("batched-3") is True
        assert await db.fetchall(
            "SELECT dedup_key FROM processed_message_events ORDER BY id",
            (),
        ) == [
            {"dedup_key": "batched-2"},
            {"dedup_key": "batched-3"},
        ]
    finally:
        await db.close()


def test_event_dedup_key_scopes_message_id_by_bot_and_conversation():
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
    from nonebot.adapters.onebot.v11.event import Sender

    from rumi_helper.plugins.chat_message_utils import event_dedup_key

    def private(self_id: int, user_id: int):
        return PrivateMessageEvent(
            time=0,
            self_id=self_id,
            post_type="message",
            sub_type="friend",
            user_id=user_id,
            message_type="private",
            message_id=77,
            message=Message("x"),
            raw_message="x",
            font=0,
            sender=Sender(user_id=user_id, nickname="u"),
        )

    def group(self_id: int, group_id: int, user_id: int):
        return GroupMessageEvent(
            time=0,
            self_id=self_id,
            post_type="message",
            sub_type="normal",
            user_id=user_id,
            message_type="group",
            message_id=77,
            group_id=group_id,
            message=Message("x"),
            raw_message="x",
            font=0,
            sender=Sender(user_id=user_id, nickname="u"),
        )

    keys = {
        event_dedup_key(private(10000, 20001)),
        event_dedup_key(private(10000, 20002)),
        event_dedup_key(group(10000, 30001, 20001)),
        event_dedup_key(group(10000, 30001, 20002)),
        event_dedup_key(group(10000, 30002, 20001)),
        event_dedup_key(private(10001, 20001)),
    }

    assert len(keys) == 6
