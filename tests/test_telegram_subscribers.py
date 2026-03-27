"""Tests for Telegram subscriber database."""

import pytest
import tempfile
from pathlib import Path

from app.telegram_subscribers import SubscriberDB


@pytest.fixture
def temp_db_path():
    """Create a temporary database path for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test_subscribers.db"


@pytest.mark.asyncio
async def test_add_subscriber(temp_db_path):
    """Test adding a subscriber."""
    db = SubscriberDB(temp_db_path)
    await db.connect()
    try:
        sub = await db.add_subscriber("123456", "testuser")
        
        assert sub.chat_id == "123456"
        assert sub.username == "testuser"
        assert sub.subscribed_at is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_add_subscriber_duplicate(temp_db_path):
    """Test adding a duplicate subscriber updates username."""
    db = SubscriberDB(temp_db_path)
    await db.connect()
    try:
        await db.add_subscriber("123456", "olduser")
        sub = await db.add_subscriber("123456", "newuser")
        
        assert sub.chat_id == "123456"
        assert sub.username == "newuser"
        
        # Should still only have one subscriber
        count = await db.get_subscriber_count()
        assert count == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_remove_subscriber(temp_db_path):
    """Test removing a subscriber."""
    db = SubscriberDB(temp_db_path)
    await db.connect()
    try:
        await db.add_subscriber("123456", "testuser")
        
        removed = await db.remove_subscriber("123456")
        assert removed is True
        
        is_subscribed = await db.is_subscribed("123456")
        assert is_subscribed is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_remove_nonexistent_subscriber(temp_db_path):
    """Test removing a subscriber that doesn't exist."""
    db = SubscriberDB(temp_db_path)
    await db.connect()
    try:
        removed = await db.remove_subscriber("999999")
        assert removed is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_is_subscribed(temp_db_path):
    """Test checking subscription status."""
    db = SubscriberDB(temp_db_path)
    await db.connect()
    try:
        assert await db.is_subscribed("123456") is False
        
        await db.add_subscriber("123456", "testuser")
        
        assert await db.is_subscribed("123456") is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_all_subscribers(temp_db_path):
    """Test getting all subscribers."""
    db = SubscriberDB(temp_db_path)
    await db.connect()
    try:
        await db.add_subscriber("111", "user1")
        await db.add_subscriber("222", "user2")
        await db.add_subscriber("333", "user3")
        
        subscribers = await db.get_all_subscribers()
        
        assert len(subscribers) == 3
        chat_ids = [s.chat_id for s in subscribers]
        assert "111" in chat_ids
        assert "222" in chat_ids
        assert "333" in chat_ids
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_subscriber_count(temp_db_path):
    """Test getting subscriber count."""
    db = SubscriberDB(temp_db_path)
    await db.connect()
    try:
        assert await db.get_subscriber_count() == 0
        
        await db.add_subscriber("111", "user1")
        assert await db.get_subscriber_count() == 1
        
        await db.add_subscriber("222", "user2")
        assert await db.get_subscriber_count() == 2
        
        await db.remove_subscriber("111")
        assert await db.get_subscriber_count() == 1
    finally:
        await db.close()
