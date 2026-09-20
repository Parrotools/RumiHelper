"""Cross-process leases for serializing one conversational session turn."""

class SessionTurnRepository:
    async def claim_session_turn(
        self,
        session_key: str,
        lease_token: str,
        now: str,
        expires_at: str,
    ) -> bool:
        async with self.operation(), self.conn.execute(
            "INSERT INTO session_turn_leases (session_key, lease_token, expires_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            "lease_token = excluded.lease_token, expires_at = excluded.expires_at "
            "WHERE session_turn_leases.expires_at <= ?",
            (str(session_key), str(lease_token), str(expires_at), str(now)),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount == 1

    async def renew_session_turn(
        self,
        session_key: str,
        lease_token: str,
        expires_at: str,
    ) -> bool:
        async with self.operation(), self.conn.execute(
            "UPDATE session_turn_leases SET expires_at = ? "
            "WHERE session_key = ? AND lease_token = ?",
            (str(expires_at), str(session_key), str(lease_token)),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount == 1

    async def release_session_turn(
        self,
        session_key: str,
        lease_token: str,
    ) -> bool:
        async with self.operation(), self.conn.execute(
            "DELETE FROM session_turn_leases "
            "WHERE session_key = ? AND lease_token = ?",
            (str(session_key), str(lease_token)),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount == 1

    async def prune_expired_session_turn_leases(self, now: str) -> int:
        async with self.operation(), self.conn.execute(
            "DELETE FROM session_turn_leases WHERE expires_at <= ?",
            (str(now),),
        ) as cursor:
            deleted = max(0, int(cursor.rowcount))
            await self.conn.commit()
            return deleted
