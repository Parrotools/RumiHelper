"""Database methods for MemoryRepository."""

class MemoryRepository:
    async def insert_memory(self, user_id: str, memory_type: str, content: str, importance: float) -> int:
        async with self.operation():
            cursor = await self.conn.execute(
                "INSERT INTO memories (user_id, type, content, importance) VALUES (?, ?, ?, ?)",
                (user_id, memory_type, content, importance),
            )
            await self.conn.commit()
            return int(cursor.lastrowid)

    async def fetch_memories(self, user_id: str, limit: int = 5) -> list[dict]:
        return await self.fetchall(
            "SELECT id, user_id, type, content, importance, created_at, updated_at "
            "FROM memories WHERE user_id = ? ORDER BY importance DESC, updated_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        )
