"""Database methods for MemoryRepository."""

class MemoryRepository:
    async def insert_memory(
        self,
        user_id: str,
        memory_type: str,
        content: str,
        importance: float,
        max_per_user: int = 200,
    ) -> int:
        async with self.operation():
            try:
                await self.conn.execute(
                    "INSERT INTO memories (user_id, type, content, importance) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, type, content) DO UPDATE SET "
                    "importance = MAX(memories.importance, excluded.importance), "
                    "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')",
                    (user_id, memory_type, content, importance),
                )
                row = await self.fetchone(
                    "SELECT id FROM memories "
                    "WHERE user_id = ? AND type = ? AND content = ?",
                    (user_id, memory_type, content),
                )
                memory_id = int(row["id"])
                await self.conn.execute(
                    "DELETE FROM memories WHERE user_id = ? AND id NOT IN ("
                    "  SELECT id FROM memories WHERE user_id = ? "
                    "  ORDER BY datetime(updated_at) DESC, updated_at DESC, id DESC LIMIT ?"
                    ")",
                    (user_id, user_id, max(1, int(max_per_user))),
                )
                await self.conn.commit()
                return memory_id
            except Exception:
                await self.conn.rollback()
                raise

    async def fetch_memories(self, user_id: str, limit: int = 5) -> list[dict]:
        return await self.fetchall(
            "SELECT id, user_id, type, content, importance, created_at, updated_at "
            "FROM memories WHERE user_id = ? ORDER BY importance DESC, updated_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        )
