"""Durable deduplication for inbound OneBot message events."""

_MESSAGE_EVENT_HISTORY_LIMIT = 100_000
_MESSAGE_EVENT_PRUNE_INTERVAL = 256


class MessageEventRepository:
    async def claim_message_event(self, dedup_key: str) -> bool:
        key = str(dedup_key).strip()
        if not key:
            return False

        async with self.operation():
            try:
                async with self.conn.execute(
                    "INSERT OR IGNORE INTO processed_message_events (dedup_key) VALUES (?)",
                    (key,),
                ) as cursor:
                    claimed = cursor.rowcount == 1
                    inserted_id = int(cursor.lastrowid or 0)

                prune_interval = max(1, int(_MESSAGE_EVENT_PRUNE_INTERVAL))
                if claimed and inserted_id % prune_interval == 0:
                    limit = max(1, int(_MESSAGE_EVENT_HISTORY_LIMIT))
                    await self.conn.execute(
                        "DELETE FROM processed_message_events "
                        "WHERE id <= COALESCE(("
                        "  SELECT id FROM processed_message_events "
                        "  ORDER BY id DESC LIMIT 1 OFFSET ?"
                        "), -1)",
                        (limit,),
                    )
                await self.conn.commit()
                return claimed
            except Exception:
                await self.conn.rollback()
                raise
