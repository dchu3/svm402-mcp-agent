"""Telegram subscriber persistence with SQLite."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import aiosqlite


DEFAULT_DB_PATH = Path.home() / ".dex-bot" / "telegram_subscribers.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id TEXT PRIMARY KEY,
    username TEXT,
    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass
class Subscriber:
    """Represents a Telegram subscriber."""

    chat_id: str
    username: Optional[str]
    subscribed_at: datetime


class SubscriberDB:
    """Async SQLite manager for Telegram subscribers."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self._connection: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Initialize database connection and schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.executescript(SCHEMA)
        await self._connection.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def _ensure_connected(self) -> aiosqlite.Connection:
        """Ensure database is connected."""
        if not self._connection:
            await self.connect()
        return self._connection  # type: ignore

    async def add_subscriber(
        self, chat_id: str, username: Optional[str] = None
    ) -> Subscriber:
        """Add a subscriber. Returns the subscriber (new or existing)."""
        conn = await self._ensure_connected()
        async with self._lock:
            cursor = await conn.execute(
                """
                INSERT INTO subscribers (chat_id, username)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username
                RETURNING *
                """,
                (chat_id, username),
            )
            row = await cursor.fetchone()
            await conn.commit()
            return self._row_to_subscriber(row)

    async def remove_subscriber(self, chat_id: str) -> bool:
        """Remove a subscriber. Returns True if removed, False if not found."""
        conn = await self._ensure_connected()
        async with self._lock:
            cursor = await conn.execute(
                "DELETE FROM subscribers WHERE chat_id = ?",
                (chat_id,),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def is_subscribed(self, chat_id: str) -> bool:
        """Check if a chat_id is subscribed."""
        conn = await self._ensure_connected()
        cursor = await conn.execute(
            "SELECT 1 FROM subscribers WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def get_all_subscribers(self) -> List[Subscriber]:
        """Get all subscribers."""
        conn = await self._ensure_connected()
        cursor = await conn.execute(
            "SELECT * FROM subscribers ORDER BY subscribed_at ASC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_subscriber(row) for row in rows]

    async def get_subscriber_count(self) -> int:
        """Get total number of subscribers."""
        conn = await self._ensure_connected()
        cursor = await conn.execute("SELECT COUNT(*) FROM subscribers")
        row = await cursor.fetchone()
        return row[0] if row else 0

    @staticmethod
    def _row_to_subscriber(row: aiosqlite.Row) -> Subscriber:
        """Convert a database row to Subscriber."""
        return Subscriber(
            chat_id=row["chat_id"],
            username=row["username"],
            subscribed_at=datetime.fromisoformat(row["subscribed_at"])
            if row["subscribed_at"]
            else datetime.now(timezone.utc),
        )
