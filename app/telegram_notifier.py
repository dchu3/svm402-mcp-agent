"""Telegram bot integration for token analysis and portfolio notifications."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from app.formatting import format_price, format_large_number
from app.telegram_subscribers import SubscriberDB
from app.token_analyzer import TokenAnalyzer, is_valid_token_address, detect_chain

TELEGRAM_API_BASE = "https://api.telegram.org/bot"

HELP_MESSAGE = """🔍 <b>Token Safety &amp; Analysis Bot</b>

Send me any token address and I'll analyze it for you!

<b>Supported Formats:</b>
• Solana: Base58 address (e.g., <code>So11111111111111111111111111111111111111112</code>)

<b>What You Get:</b>
📊 Price &amp; market data
💧 Liquidity info
🛡️ Safety check (rugcheck)
🤖 AI-powered analysis

<b>Commands:</b>
• /analyze &lt;address&gt; - Quick tweet-style summary
• /full &lt;address&gt; - Detailed analysis report
• /help - Show this message
• /status - Check bot status

Just paste a token address to get started!
"""


class TelegramNotifier:
    """Async Telegram bot client for token analysis and alert notifications."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str = "",
        timeout: float = 10.0,
        poll_interval: float = 2.0,
        subscribers_db_path: Optional[Path] = None,
        token_analyzer: Optional["TokenAnalyzer"] = None,
        private_mode: bool = False,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id  # Allowed chat in private mode
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._client: Optional[httpx.AsyncClient] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._last_update_id: int = 0
        self._running: bool = False
        self._subscribers_db = SubscriberDB(subscribers_db_path)
        self._token_analyzer = token_analyzer
        self._analyzing: set[str] = set()  # Track chats currently being analyzed
        self._private_mode = private_mode

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        """Close the HTTP client and stop polling."""
        await self.stop_polling()
        await self._subscribers_db.close()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def is_configured(self) -> bool:
        """Check if Telegram credentials are configured."""
        return bool(self.bot_token)

    @property
    def is_polling(self) -> bool:
        """Check if the bot is currently polling for messages."""
        return self._running and self._polling_task is not None

    def set_token_analyzer(self, analyzer: "TokenAnalyzer") -> None:
        """Set the token analyzer instance."""
        self._token_analyzer = analyzer

    async def set_commands(self) -> bool:
        """Register bot commands with Telegram for the command menu.
        
        Returns:
            True if commands were registered successfully
        """
        if not self.is_configured:
            return False

        commands = [
            {"command": "start", "description": "Show welcome message"},
            {"command": "help", "description": "Show available commands"},
            {"command": "analyze", "description": "Quick tweet-style token summary"},
            {"command": "full", "description": "Detailed token analysis report"},
            {"command": "status", "description": "Check bot status"},
        ]

        try:
            client = await self._get_client()
            url = f"{TELEGRAM_API_BASE}{self.bot_token}/setMyCommands"
            response = await client.post(url, json={"commands": commands})
            data = response.json()
            return data.get("ok", False)
        except Exception:
            return False

    async def start_polling(self) -> None:
        """Start polling for incoming messages."""
        if self._running or not self.is_configured:
            return
        
        # Register bot commands with Telegram
        await self.set_commands()
        
        # Auto-subscribe legacy chat_id if configured (backwards compatibility)
        if self.chat_id:
            await self._subscribers_db.add_subscriber(self.chat_id)
        
        self._running = True
        self._polling_task = asyncio.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        """Stop polling for incoming messages."""
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None

    async def _poll_loop(self) -> None:
        """Main polling loop for incoming messages."""
        while self._running:
            try:
                await self._poll_updates()
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _poll_updates(self) -> None:
        """Fetch and process new messages from Telegram."""
        client = await self._get_client()
        url = f"{TELEGRAM_API_BASE}{self.bot_token}/getUpdates"
        params = {
            "offset": self._last_update_id + 1,
            "timeout": 1,
            "allowed_updates": ["message"],
        }
        
        try:
            response = await client.get(url, params=params)
            data = response.json()
            
            if not data.get("ok"):
                return
            
            for update in data.get("result", []):
                self._last_update_id = update.get("update_id", self._last_update_id)
                await self._handle_update(update)
        except Exception:
            pass

    async def _handle_update(self, update: dict) -> None:
        """Handle an incoming update from Telegram."""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()
        username = message.get("from", {}).get("username")
        
        if not chat_id or not text:
            return
        
        # Check access in private mode
        if not self._is_allowed(chat_id):
            # Only respond once per chat to avoid spam
            if text.startswith("/") or is_valid_token_address(text):
                await self.send_message_to(
                    chat_id,
                    "🔒 This bot is running in private mode and is not available for public use."
                )
            return
        
        # Handle commands
        if text.startswith("/"):
            await self._handle_command(text, chat_id, username)
            return
        
        # Check if text is a token address
        if is_valid_token_address(text):
            await self._handle_token_address(text, chat_id)
            return

    def _is_allowed(self, chat_id: str) -> bool:
        """Check if a chat_id is allowed to use the bot.
        
        In private mode, only the configured chat_id is allowed.
        In public mode, everyone is allowed.
        """
        if not self._private_mode:
            return True
        return chat_id == self.chat_id

    async def _handle_command(
        self, command: str, chat_id: str, username: Optional[str] = None
    ) -> None:
        """Handle a bot command."""
        parts = command.split()
        cmd = parts[0].lower().split("@")[0]
        
        if cmd in ("/help", "/start"):
            await self.send_message_to(chat_id, HELP_MESSAGE)
        elif cmd == "/status":
            await self._send_status(chat_id)
        elif cmd == "/analyze":
            if len(parts) > 1:
                address = parts[1].strip()
                await self._handle_token_address(address, chat_id)
            else:
                await self.send_message_to(
                    chat_id, 
                    "❌ Please provide a token address.\n\nUsage: /analyze &lt;address&gt;"
                )
        elif cmd == "/full":
            if len(parts) > 1:
                address = parts[1].strip()
                await self._handle_token_address(address, chat_id, full=True)
            else:
                await self.send_message_to(
                    chat_id,
                    "❌ Please provide a token address.\n\nUsage: /full &lt;address&gt;"
                )
        # Legacy commands - keep for backwards compatibility but don't advertise
        elif cmd == "/subscribe":
            await self._handle_subscribe(chat_id, username)
        elif cmd == "/unsubscribe":
            await self._handle_unsubscribe(chat_id)

    async def _handle_token_address(
        self, address: str, chat_id: str, full: bool = False
    ) -> None:
        """Handle a token address - analyze and return report.
        
        Args:
            address: Token contract address
            chat_id: Telegram chat ID
            full: If True, send detailed report; otherwise send tweet summary
        """
        if not self._token_analyzer:
            await self.send_message_to(
                chat_id,
                "❌ Token analyzer not available. Please try again later."
            )
            return
        
        # Prevent duplicate analysis for same chat
        if chat_id in self._analyzing:
            await self.send_message_to(
                chat_id,
                "⏳ Already analyzing a token for you. Please wait..."
            )
            return
        
        self._analyzing.add(chat_id)
        
        try:
            # Send "analyzing" status
            chain = detect_chain(address)
            chain_name = chain.capitalize() if chain else "Unknown"
            await self.send_message_to(
                chat_id,
                f"🔍 Analyzing token on {chain_name}...\n\n<code>{address}</code>\n\nThis may take a few seconds."
            )
            
            # Run analysis
            report = await self._token_analyzer.analyze(
                address,
                chain,
                structured=False,
                legacy_output=True,
            )
            
            # Send tweet summary or full report
            message = report.telegram_message if full else report.tweet_message
            await self._send_long_message(
                chat_id, message, disable_web_page_preview=True
            )
            
        except Exception as e:
            await self.send_message_to(
                chat_id,
                f"❌ Analysis failed: {str(e)}\n\nPlease check the address and try again."
            )
        finally:
            self._analyzing.discard(chat_id)

    async def _send_long_message(
        self,
        chat_id: str,
        text: str,
        max_length: int = 4000,
        disable_web_page_preview: bool = False,
    ) -> None:
        """Send a long message, splitting if necessary."""
        if len(text) <= max_length:
            await self.send_message_to(
                chat_id, text, disable_web_page_preview=disable_web_page_preview
            )
            return
        
        # Split at paragraph breaks or newlines
        parts = []
        current = ""
        
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > max_length:
                if current:
                    parts.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        
        if current:
            parts.append(current)
        
        for i, part in enumerate(parts):
            if len(parts) > 1:
                part = f"{part}\n\n<i>({i+1}/{len(parts)})</i>"
            await self.send_message_to(
                chat_id, part, disable_web_page_preview=disable_web_page_preview
            )
            if i < len(parts) - 1:
                await asyncio.sleep(0.5)  # Brief delay between messages

    async def _handle_subscribe(
        self, chat_id: str, username: Optional[str] = None
    ) -> None:
        """Handle /subscribe command."""
        was_subscribed = await self._subscribers_db.is_subscribed(chat_id)
        await self._subscribers_db.add_subscriber(chat_id, username)
        
        if was_subscribed:
            message = "✅ You're already subscribed to price alerts."
        else:
            message = (
                "✅ <b>Subscribed!</b>\n\n"
                "You will now receive price alerts when watched tokens cross thresholds.\n\n"
                "Use /unsubscribe to stop receiving alerts."
            )
        await self.send_message_to(chat_id, message)

    async def _handle_unsubscribe(self, chat_id: str) -> None:
        """Handle /unsubscribe command."""
        removed = await self._subscribers_db.remove_subscriber(chat_id)
        
        if removed:
            message = (
                "🔕 <b>Unsubscribed</b>\n\n"
                "You will no longer receive price alerts.\n\n"
                "Use /subscribe to re-enable alerts."
            )
        else:
            message = "ℹ️ You weren't subscribed to alerts."
        await self.send_message_to(chat_id, message)

    async def _send_status(self, chat_id: str) -> None:
        """Send bot status message."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        analyzer_status = "✅ Ready" if self._token_analyzer else "❌ Not configured"
        message = (
            "✅ <b>Bot Status</b>\n\n"
            f"<b>Status:</b> Online\n"
            f"<b>Analyzer:</b> {analyzer_status}\n"
            f"<b>Time:</b> {timestamp}\n\n"
            "Send a token address to analyze it!"
        )
        await self.send_message_to(chat_id, message)

    async def test_connection(self) -> bool:
        """Test if the bot token is valid by calling getMe."""
        if not self.is_configured:
            return False

        try:
            client = await self._get_client()
            url = f"{TELEGRAM_API_BASE}{self.bot_token}/getMe"
            response = await client.get(url)
            data = response.json()
            return data.get("ok", False)
        except Exception:
            return False

    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> bool:
        """Send a message to the legacy configured chat (backwards compatibility).
        
        Args:
            text: Message text (supports HTML formatting)
            parse_mode: Message parse mode (HTML or Markdown)
            disable_notification: Send silently
            
        Returns:
            True if message was sent successfully
        """
        if not self.is_configured or not self.chat_id:
            return False
        return await self.send_message_to(
            self.chat_id, text, parse_mode, disable_notification
        )

    async def send_message_to(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
        disable_web_page_preview: bool = False,
    ) -> bool:
        """Send a message to a specific chat.
        
        Args:
            chat_id: Target chat ID
            text: Message text (supports HTML formatting)
            parse_mode: Message parse mode (HTML or Markdown)
            disable_notification: Send silently
            disable_web_page_preview: Disable link previews
            
        Returns:
            True if message was sent successfully
        """
        if not self.is_configured:
            return False

        try:
            client = await self._get_client()
            url = f"{TELEGRAM_API_BASE}{self.bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_notification": disable_notification,
                "disable_web_page_preview": disable_web_page_preview,
            }
            response = await client.post(url, json=payload)
            data = response.json()
            return data.get("ok", False)
        except Exception:
            return False

    async def broadcast_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> int:
        """Send a message to all subscribers.
        
        Args:
            text: Message text (supports HTML formatting)
            parse_mode: Message parse mode (HTML or Markdown)
            disable_notification: Send silently
            
        Returns:
            Number of successfully sent messages
        """
        subscribers = await self._subscribers_db.get_all_subscribers()
        success_count = 0
        for sub in subscribers:
            if await self.send_message_to(
                sub.chat_id, text, parse_mode, disable_notification
            ):
                success_count += 1
        return success_count
