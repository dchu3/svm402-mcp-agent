"""Tests for Telegram notifier."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from app.formatting import format_price, format_large_number
from app.telegram_notifier import TelegramNotifier


@pytest.fixture
def temp_db_path():
    """Create a temporary database path for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test_subscribers.db"


@pytest.fixture
def notifier(temp_db_path):
    """Create a test notifier with mock credentials."""
    return TelegramNotifier(
        bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        chat_id="987654321",
        subscribers_db_path=temp_db_path,
    )


@pytest.fixture
def unconfigured_notifier(temp_db_path):
    """Create a notifier without credentials."""
    return TelegramNotifier(
        bot_token="",
        chat_id="",
        subscribers_db_path=temp_db_path,
    )


def test_is_configured_true(notifier):
    """Test is_configured returns True when bot token is set."""
    assert notifier.is_configured is True


def test_is_configured_false(unconfigured_notifier):
    """Test is_configured returns False when bot token is empty."""
    assert unconfigured_notifier.is_configured is False


def test_is_configured_token_only(temp_db_path):
    """Test is_configured returns True when only bot token is set (no chat_id needed)."""
    notifier = TelegramNotifier(
        bot_token="token",
        chat_id="",
        subscribers_db_path=temp_db_path,
    )
    assert notifier.is_configured is True


@pytest.mark.asyncio
async def test_send_message_unconfigured(unconfigured_notifier):
    """Test send_message returns False when not configured."""
    result = await unconfigured_notifier.send_message("test")
    assert result is False


@pytest.mark.asyncio
async def test_send_message_success(notifier):
    """Test send_message returns True on successful API call."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = await notifier.send_message("Hello, World!")

        assert result is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "sendMessage" in call_args[0][0]
        assert call_args[1]["json"]["text"] == "Hello, World!"
        assert call_args[1]["json"]["chat_id"] == "987654321"


@pytest.mark.asyncio
async def test_send_message_api_failure(notifier):
    """Test send_message returns False when API returns error."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": False, "error": "Bad Request"}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = await notifier.send_message("Hello, World!")
        assert result is False


@pytest.mark.asyncio
async def test_send_message_network_error(notifier):
    """Test send_message returns False on network error."""
    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Network error")
        mock_get_client.return_value = mock_client

        result = await notifier.send_message("Hello, World!")
        assert result is False


@pytest.mark.asyncio
async def test_test_connection_success(notifier):
    """Test test_connection returns True when bot token is valid."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True, "result": {"username": "test_bot"}}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = await notifier.test_connection()

        assert result is True
        mock_client.get.assert_called_once()
        assert "getMe" in mock_client.get.call_args[0][0]


@pytest.mark.asyncio
async def test_test_connection_invalid_token(notifier):
    """Test test_connection returns False when bot token is invalid."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": False, "error": "Unauthorized"}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = await notifier.test_connection()
        assert result is False


@pytest.mark.asyncio
async def test_test_connection_unconfigured(unconfigured_notifier):
    """Test test_connection returns False when not configured."""
    result = await unconfigured_notifier.test_connection()
    assert result is False


def test_format_price():
    """Test price formatting with different magnitudes."""
    assert format_price(1234.5678) == "$1,234.5678"
    assert format_price(1.5) == "$1.5000"
    assert format_price(0.001234) == "$0.001234"
    assert format_price(0.00001234) == "$0.0000123400"


def test_format_liquidity():
    """Test liquidity formatting with different magnitudes."""
    assert format_large_number(1500000000) == "$1.50B"
    assert format_large_number(2500000) == "$2.50M"
    assert format_large_number(500000) == "$500.00K"
    assert format_large_number(750) == "$750"


@pytest.mark.asyncio
async def test_close(notifier):
    """Test close method closes the client and stops polling."""
    # Create a mock client
    mock_client = AsyncMock()
    mock_client.is_closed = False
    notifier._client = mock_client

    await notifier.close()

    mock_client.aclose.assert_called_once()
    assert notifier._client is None


@pytest.mark.asyncio
async def test_set_commands(notifier):
    """Test set_commands registers commands with Telegram."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = await notifier.set_commands()

        assert result is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "setMyCommands" in call_args[0][0]
        commands = call_args[1]["json"]["commands"]
        command_names = [c["command"] for c in commands]
        assert "start" in command_names
        assert "help" in command_names
        assert "analyze" in command_names
        assert "full" in command_names
        assert "status" in command_names


@pytest.mark.asyncio
async def test_handle_full_command(notifier):
    """Test /full command routes to _handle_token_address with full=True."""
    with patch.object(
        notifier, "_handle_token_address", new_callable=AsyncMock
    ) as mock_handle:
        await notifier._handle_command(
            "/full DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "123456"
        )
        mock_handle.assert_called_once_with(
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "123456", full=True
        )


@pytest.mark.asyncio
async def test_handle_full_command_no_address(notifier):
    """Test /full without address sends usage message."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        await notifier._handle_command("/full", "123456")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        message_text = call_args[1]["json"]["text"]
        assert "/full" in message_text
        assert "address" in message_text.lower()


@pytest.mark.asyncio
async def test_handle_analyze_routes_without_full(notifier):
    """Test /analyze routes to _handle_token_address without full flag."""
    with patch.object(
        notifier, "_handle_token_address", new_callable=AsyncMock
    ) as mock_handle:
        await notifier._handle_command(
            "/analyze DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "123456"
        )
        mock_handle.assert_called_once_with(
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "123456"
        )


@pytest.mark.asyncio
async def test_raw_address_routes_without_full(notifier):
    """Test raw address sends tweet summary (default, not full)."""
    mock_analyzer = AsyncMock()
    mock_report = MagicMock()
    mock_report.tweet_message = "tweet summary"
    mock_report.telegram_message = "full report"
    mock_analyzer.analyze.return_value = mock_report
    notifier._token_analyzer = mock_analyzer

    with patch.object(notifier, "send_message_to", new_callable=AsyncMock):
        with patch.object(notifier, "_send_long_message", new_callable=AsyncMock) as mock_send:
            await notifier._handle_token_address(
                "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "123456"
            )
            mock_send.assert_called_once()
            sent_text = mock_send.call_args[0][1]
            assert sent_text == "tweet summary"


@pytest.mark.asyncio
async def test_start_stop_polling(notifier):
    """Test starting and stopping polling."""
    assert notifier.is_polling is False

    # Mock set_commands to avoid actual API call
    with patch.object(notifier, "set_commands", new_callable=AsyncMock):
        await notifier.start_polling()
        assert notifier.is_polling is True

    await notifier.stop_polling()
    assert notifier.is_polling is False


@pytest.mark.asyncio
async def test_start_polling_unconfigured(unconfigured_notifier):
    """Test that polling doesn't start when not configured."""
    await unconfigured_notifier.start_polling()
    assert unconfigured_notifier.is_polling is False


@pytest.mark.asyncio
async def test_handle_help_command(notifier):
    """Test handling /help command sends help message."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        await notifier._handle_command("/help", "123456")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        message_text = call_args[1]["json"]["text"]

        assert "Token Safety" in message_text
        assert "/analyze" in message_text
        assert "/help" in message_text


@pytest.mark.asyncio
async def test_handle_start_command(notifier):
    """Test handling /start command sends help message."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        await notifier._handle_command("/start", "123456")

        mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_handle_status_command(notifier):
    """Test handling /status command sends status message."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        await notifier._handle_command("/status", "123456")

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        message_text = call_args[1]["json"]["text"]

        assert "Bot Status" in message_text
        assert "Online" in message_text


@pytest.mark.asyncio
async def test_handle_update_from_any_chat(notifier):
    """Test that updates from any chat are processed (no restriction)."""
    update = {
        "update_id": 12345,
        "message": {
            "chat": {"id": 111111111},  # Any chat ID
            "from": {"username": "testuser"},
            "text": "/help",
        }
    }

    with patch.object(notifier, "_handle_command", new_callable=AsyncMock) as mock_handle:
        await notifier._handle_update(update)
        mock_handle.assert_called_once_with("/help", "111111111", "testuser")


@pytest.mark.asyncio
async def test_subscribe_command(notifier):
    """Test /subscribe command adds user to subscribers."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        await notifier._handle_command("/subscribe", "123456", "testuser")

        # Verify subscription was added
        is_subscribed = await notifier._subscribers_db.is_subscribed("123456")
        assert is_subscribed is True

        # Verify confirmation message was sent
        call_args = mock_client.post.call_args
        message_text = call_args[1]["json"]["text"]
        assert "Subscribed" in message_text


@pytest.mark.asyncio
async def test_unsubscribe_command(notifier):
    """Test /unsubscribe command removes user from subscribers."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    # First subscribe
    await notifier._subscribers_db.add_subscriber("123456", "testuser")

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        await notifier._handle_command("/unsubscribe", "123456")

        # Verify subscription was removed
        is_subscribed = await notifier._subscribers_db.is_subscribed("123456")
        assert is_subscribed is False

        # Verify confirmation message was sent
        call_args = mock_client.post.call_args
        message_text = call_args[1]["json"]["text"]
        assert "Unsubscribed" in message_text


@pytest.mark.asyncio
async def test_broadcast_message(notifier):
    """Test broadcast_message sends to all subscribers."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"ok": True}

    # Add multiple subscribers
    await notifier._subscribers_db.add_subscriber("111", "user1")
    await notifier._subscribers_db.add_subscriber("222", "user2")
    await notifier._subscribers_db.add_subscriber("333", "user3")

    with patch.object(notifier, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_get_client.return_value = mock_client

        count = await notifier.broadcast_message("Test message")

        assert count == 3
        assert mock_client.post.call_count == 3


class TestPrivateMode:
    """Tests for private mode access control."""

    @pytest.fixture
    def private_notifier(self, temp_db_path):
        """Create a notifier in private mode."""
        return TelegramNotifier(
            bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
            chat_id="allowed_chat_123",
            subscribers_db_path=temp_db_path,
            private_mode=True,
        )

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path for testing."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "test_subscribers.db"

    def test_is_allowed_public_mode(self, temp_db_path):
        """Test that public mode allows all chats."""
        notifier = TelegramNotifier(
            bot_token="test",
            chat_id="owner_chat",
            subscribers_db_path=temp_db_path,
            private_mode=False,
        )
        assert notifier._is_allowed("any_chat") is True
        assert notifier._is_allowed("another_chat") is True

    def test_is_allowed_private_mode_owner(self, private_notifier):
        """Test that private mode allows the owner chat."""
        assert private_notifier._is_allowed("allowed_chat_123") is True

    def test_is_allowed_private_mode_stranger(self, private_notifier):
        """Test that private mode blocks other chats."""
        assert private_notifier._is_allowed("stranger_chat") is False
        assert private_notifier._is_allowed("another_stranger") is False

    @pytest.mark.asyncio
    async def test_handle_update_private_mode_blocked(self, private_notifier):
        """Test that private mode sends rejection message to blocked users."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}

        update = {
            "message": {
                "chat": {"id": "stranger_chat"},
                "text": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                "from": {"username": "stranger"},
            }
        }

        with patch.object(private_notifier, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            await private_notifier._handle_update(update)

            # Should send "private mode" message
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            message_text = call_args[1]["json"]["text"]
            assert "private mode" in message_text.lower()

    @pytest.mark.asyncio
    async def test_handle_update_private_mode_allowed(self, private_notifier):
        """Test that private mode allows the owner chat to use commands."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}

        update = {
            "message": {
                "chat": {"id": "allowed_chat_123"},
                "text": "/help",
                "from": {"username": "owner"},
            }
        }

        with patch.object(private_notifier, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get_client.return_value = mock_client

            await private_notifier._handle_update(update)

            # Should send help message, not rejection
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            message_text = call_args[1]["json"]["text"]
            assert "Token Safety" in message_text  # Help message content
