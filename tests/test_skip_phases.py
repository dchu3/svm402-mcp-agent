"""Tests for skip phases feature: tokens with 2 negative stop losses skip one discovery cycle."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

import app.execution as execution_module
from app.portfolio_strategy import (
    PortfolioStrategyConfig,
    PortfolioStrategyEngine,
)
from app.portfolio_discovery import DiscoveryCandidate
from app.database import Database


# ---------------------------------------------------------------------------
# Mock clients
# ---------------------------------------------------------------------------

SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_1 = "TokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN_2 = "TokenBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


class MockDexScreenerClient:
    def __init__(self, price_usd: float = 0.01, native_price_usd: float = 180.0):
        self.price_usd = price_usd
        self.native_price_usd = native_price_usd
        self.prices: Dict[str, float] = {}

    async def call_tool(self, method: str, arguments: Dict[str, Any]) -> Any:
        token = arguments.get("tokenAddress", "")
        if token == SOL_MINT:
            price = self.native_price_usd
        else:
            price = self.prices.get(token.lower(), self.price_usd)
        return {
            "pairs": [
                {
                    "priceUsd": str(price),
                    "liquidity": {"usd": 50000.0},
                }
            ]
        }


class MockTraderClient:
    def __init__(self, price: float = 0.01, success: bool = True):
        self.price = price
        self.success = success
        self.tools: List[Dict[str, Any]] = [
            {
                "name": "getQuote",
                "inputSchema": {
                    "type": "object",
                    "required": ["chain", "inputMint", "outputMint", "amountUsd", "slippageBps", "side"],
                    "properties": {},
                },
            },
            {
                "name": "swap",
                "inputSchema": {
                    "type": "object",
                    "required": ["chain", "inputMint", "outputMint", "amountUsd", "slippageBps", "side"],
                    "properties": {},
                },
            },
        ]

    async def call_tool(self, method: str, arguments: Dict[str, Any]) -> Any:
        if method == "getQuote":
            return {"priceUsd": str(self.price), "liquidityUsd": 100000}
        if method == "swap":
            if self.success:
                return {"success": True, "executedPrice": str(self.price), "txHash": "mocktx"}
            return {"success": False, "error": "execution failed"}
        raise ValueError(f"Unexpected method: {method}")


class MockMCPManager:
    def __init__(self, dexscreener: MockDexScreenerClient, trader: MockTraderClient):
        self._dexscreener = dexscreener
        self._trader = trader

    def get_client(self, name: str) -> Any:
        if name == "dexscreener":
            return self._dexscreener
        if name == "trader":
            return self._trader
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test_skip_phases.db"


@pytest_asyncio.fixture
async def db(temp_db_path):
    database = Database(db_path=temp_db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture(autouse=True)
def seed_decimals_cache():
    """Pre-seed decimals cache for fake mints so tests don't hit Solana RPC."""
    execution_module._decimals_cache[TOKEN_1.lower()] = 6
    execution_module._decimals_cache[TOKEN_2.lower()] = 6
    yield
    execution_module._decimals_cache.pop(TOKEN_1.lower(), None)
    execution_module._decimals_cache.pop(TOKEN_2.lower(), None)


def _make_config(**overrides) -> PortfolioStrategyConfig:
    """Create a test config with defaults."""
    defaults = {
        "enabled": True,
        "dry_run": True,
        "chain": "solana",
        "max_positions": 5,
        "position_size_usd": 10.0,
        "take_profit_pct": 15.0,
        "stop_loss_pct": 8.0,
        "trailing_stop_pct": 5.0,
        "max_hold_hours": 24,
        "discovery_interval_mins": 30,
        "price_check_seconds": 60,
        "daily_loss_limit_usd": 100.0,
        "min_volume_usd": 10000.0,
        "min_liquidity_usd": 5000.0,
        "min_market_cap_usd": 250000.0,
        "cooldown_seconds": 300,
        "min_momentum_score": 50.0,
        "max_slippage_bps": 500,
        "rpc_url": "https://test-rpc",
    }
    defaults.update(overrides)
    return PortfolioStrategyConfig(**defaults)


def _make_engine(
    db: Database,
    dex_price: float = 0.01,
    trader_success: bool = True,
) -> PortfolioStrategyEngine:
    """Create a test engine with mock clients."""
    dex = MockDexScreenerClient(price_usd=dex_price)
    trader = MockTraderClient(price=dex_price, success=trader_success)
    mcp = MockMCPManager(dexscreener=dex, trader=trader)

    config = _make_config()
    return PortfolioStrategyEngine(
        db=db,
        mcp_manager=mcp,
        config=config,
        api_key="test",
        model_name="gemini-2.5-flash",
    )


async def _insert_position(
    db: Database,
    token_address: str = TOKEN_1,
    symbol: str = "TEST",
    entry_price: float = 1.00,
    quantity_token: float = 100.0,
) -> Any:
    """Insert a test position."""
    return await db.add_portfolio_position(
        token_address=token_address,
        symbol=symbol,
        chain="solana",
        entry_price=entry_price,
        quantity_token=quantity_token,
        notional_usd=10.0,
        stop_price=entry_price * 0.92,  # 8% SL
        take_price=entry_price * 1.15,  # 15% TP
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSkipPhasesTracking:
    """Tests for skip phases tracking in database."""

    @pytest.mark.asyncio
    async def test_increment_negative_sl_count_first_time(self, db):
        """First negative stop loss increments count to 1."""
        count = await db.increment_negative_sl_count(TOKEN_1, "solana")
        assert count == 1

        skip_phases = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip_phases == 0  # Not skipped yet

    @pytest.mark.asyncio
    async def test_increment_negative_sl_count_second_time(self, db):
        """Second negative stop loss sets skip_phases to 1."""
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        count = await db.increment_negative_sl_count(TOKEN_1, "solana")
        assert count == 2

        skip_phases = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip_phases == 1  # Now skipped

    @pytest.mark.asyncio
    async def test_different_tokens_tracked_separately(self, db):
        """Different tokens have separate skip phase counters."""
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        await db.increment_negative_sl_count(TOKEN_1, "solana")

        count2 = await db.increment_negative_sl_count(TOKEN_2, "solana")
        assert count2 == 1

        skip1 = await db.get_skip_phases(TOKEN_1, "solana")
        skip2 = await db.get_skip_phases(TOKEN_2, "solana")
        assert skip1 == 1
        assert skip2 == 0

    @pytest.mark.asyncio
    async def test_decrement_all_skip_phases(self, db):
        """Decrement skip_phases for all tokens in chain."""
        # Set up two tokens with skip_phases
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        await db.increment_negative_sl_count(TOKEN_2, "solana")
        await db.increment_negative_sl_count(TOKEN_2, "solana")

        assert await db.get_skip_phases(TOKEN_1, "solana") == 1
        assert await db.get_skip_phases(TOKEN_2, "solana") == 1

        # Decrement all
        updated = await db.decrement_all_skip_phases("solana")
        assert updated == 2

        # Both should now be 0
        assert await db.get_skip_phases(TOKEN_1, "solana") == 0
        assert await db.get_skip_phases(TOKEN_2, "solana") == 0

    @pytest.mark.asyncio
    async def test_decrement_resets_negative_sl_count(self, db):
        """When skip_phases reaches 0, negative_sl_count is reset."""
        # Hit 2 negative stop losses
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        await db.increment_negative_sl_count(TOKEN_1, "solana")

        # Decrement skip_phases to 0
        await db.decrement_all_skip_phases("solana")

        # Now a negative stop loss should start fresh at count=1
        count = await db.increment_negative_sl_count(TOKEN_1, "solana")
        assert count == 1
        assert await db.get_skip_phases(TOKEN_1, "solana") == 0

    @pytest.mark.asyncio
    async def test_reset_token_skip_phases(self, db):
        """Reset skip_phases and counter for a specific token."""
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        await db.increment_negative_sl_count(TOKEN_1, "solana")

        reset = await db.reset_token_skip_phases(TOKEN_1, "solana")
        assert reset is True

        skip_phases = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip_phases == 0

    @pytest.mark.asyncio
    async def test_decrement_preserves_count_for_mid_accumulation_token(self, db):
        """decrement_all_skip_phases must NOT reset negative_sl_count for a token
        with count=1 and skip_phases=0 (has not yet triggered a skip phase)."""
        # Token has one negative SL — count=1, skip_phases=0 (not yet skipping)
        count = await db.increment_negative_sl_count(TOKEN_1, "solana")
        assert count == 1
        assert await db.get_skip_phases(TOKEN_1, "solana") == 0

        # A discovery cycle runs; TOKEN_2 has skip_phases=1 so decrement fires
        await db.increment_negative_sl_count(TOKEN_2, "solana")
        await db.increment_negative_sl_count(TOKEN_2, "solana")
        assert await db.get_skip_phases(TOKEN_2, "solana") == 1

        await db.decrement_all_skip_phases("solana")

        # TOKEN_1's counter should still be 1 — not wiped
        count_after = await db.increment_negative_sl_count(TOKEN_1, "solana")
        assert count_after == 2  # Was 1, now 2 → triggers skip
        assert await db.get_skip_phases(TOKEN_1, "solana") == 1


class TestSkipPhasesIntegration:
    """Integration tests for skip phases in portfolio strategy."""

    @pytest.mark.asyncio
    async def test_negative_stop_loss_increments_count(self, db):
        """Closing position with negative stop loss increments counter."""
        engine = _make_engine(db, dex_price=0.90)  # Price below stop
        pos = await _insert_position(db, entry_price=1.00)

        # Run exit check to trigger stop loss
        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        assert result.positions_closed[0].close_reason == "stop_loss"
        assert result.positions_closed[0].realized_pnl_usd < 0

        # Should still increment for any negative PnL stop loss
        skip_phases = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip_phases == 0  # First one doesn't trigger skip yet

    @pytest.mark.asyncio
    async def test_two_negative_stop_losses_set_skip_phases(self, db):
        """Two negative stop losses set skip_phases to 1."""
        engine = _make_engine(db, dex_price=0.90)

        # First negative stop loss
        pos1 = await _insert_position(db, entry_price=1.00)
        result1 = await engine.run_exit_checks()
        assert len(result1.positions_closed) == 1

        # Second negative stop loss
        pos2 = await _insert_position(db, entry_price=1.00)
        result2 = await engine.run_exit_checks()
        assert len(result2.positions_closed) == 1

        # Now skip_phases should be 1
        skip_phases = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip_phases == 1

    @pytest.mark.asyncio
    async def test_first_negative_stop_loss_does_not_trigger_skip(self, db):
        """First negative stop loss increments count to 1 but does not yet trigger skip_phases."""
        # Position at entry 0.85, stop at 0.782 (0.85 * 0.92)
        # Current price 0.78 is below stop, triggering stop loss with negative PnL
        pos = await _insert_position(db, entry_price=0.85, quantity_token=100.0)

        engine = _make_engine(db, dex_price=0.78)  # Below stop (0.85 * 0.92 = 0.782)
        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        assert result.positions_closed[0].close_reason == "stop_loss"
        assert result.positions_closed[0].realized_pnl_usd < 0

        # First negative SL increments count to 1 but skip_phases stays 0
        skip_phases = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip_phases == 0  # First one doesn't trigger skip yet

    @pytest.mark.asyncio
    async def test_skip_phases_filters_during_discovery(self, db):
        """Token with skip_phases > 0 is skipped during discovery."""
        # Manually set skip_phases for TOKEN_1
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        await db.increment_negative_sl_count(TOKEN_1, "solana")

        skip_phases = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip_phases == 1

        # Create a candidate for TOKEN_1
        candidate = DiscoveryCandidate(
            token_address=TOKEN_1,
            symbol="TEST1",
            chain="solana",
            price_usd=0.01,
            volume_24h=50000.0,
            liquidity_usd=25000.0,
            momentum_score=75.0,
        )

        engine = _make_engine(db)

        # Mock the discovery to return our candidate
        async def mock_discover(db, max_candidates, **kwargs):
            return [candidate]
        
        engine.discovery.discover = mock_discover

        # Run discovery cycle
        result = await engine.run_discovery_cycle()

        # Position should NOT be opened because token is skipped
        assert len(result.positions_opened) == 0

    @pytest.mark.asyncio
    async def test_decrement_happens_after_discovery(self, db):
        """Skip_phases is decremented after each discovery cycle."""
        # Set skip_phases to 1
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        await db.increment_negative_sl_count(TOKEN_1, "solana")

        assert await db.get_skip_phases(TOKEN_1, "solana") == 1

        engine = _make_engine(db)
        
        async def mock_discover(db, max_candidates, **kwargs):
            return []
        
        engine.discovery.discover = mock_discover

        # Run discovery cycle
        await engine.run_discovery_cycle()

        # Skip_phases should now be 0
        skip_phases = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip_phases == 0

    @pytest.mark.asyncio
    async def test_token_discoverable_after_skip_phase_expires(self, db):
        """After skip_phases decrements to 0, token can be discovered again."""
        # Set skip_phases to 1
        await db.increment_negative_sl_count(TOKEN_1, "solana")
        await db.increment_negative_sl_count(TOKEN_1, "solana")

        candidate = DiscoveryCandidate(
            token_address=TOKEN_1,
            symbol="TEST1",
            chain="solana",
            price_usd=0.01,
            volume_24h=50000.0,
            liquidity_usd=25000.0,
            momentum_score=75.0,
        )

        engine = _make_engine(db)
        
        async def mock_discover(db, max_candidates, **kwargs):
            return [candidate]
        
        engine.discovery.discover = mock_discover

        # First cycle: token is skipped, skip_phases decremented
        result1 = await engine.run_discovery_cycle()
        assert len(result1.positions_opened) == 0
        assert await db.get_skip_phases(TOKEN_1, "solana") == 0

        # Second cycle: token is discoverable again
        result2 = await engine.run_discovery_cycle()
        assert len(result2.positions_opened) == 1
        assert result2.positions_opened[0].token_address == TOKEN_1

    @pytest.mark.asyncio
    async def test_stop_loss_with_positive_pnl_does_not_increment(self, db):
        """Stop loss with positive PnL (e.g. trailing stop above entry) does NOT increment counter."""
        # stop_price set above entry to simulate a trailing stop that locked in profit
        # entry=1.00, stop=1.10 (trailing stop above entry), current price=1.05 (below stop)
        # PnL = (1.05 - 1.00) * 100 = +5.0  → positive
        pos = await db.add_portfolio_position(
            token_address=TOKEN_1,
            symbol="TEST",
            chain="solana",
            entry_price=1.00,
            quantity_token=100.0,
            notional_usd=10.0,
            stop_price=1.10,   # trailing stop above entry
            take_price=1.20,
        )

        engine = _make_engine(db, dex_price=1.05)  # below stop, but above entry
        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        assert result.positions_closed[0].close_reason == "stop_loss"
        assert result.positions_closed[0].realized_pnl_usd > 0  # positive PnL

        # Positive-PnL stop loss must NOT increment the skip counter
        skip = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip == 0
        # Verify the row wasn't even created (count stays 0)
        count_after = await db.increment_negative_sl_count(TOKEN_1, "solana")
        assert count_after == 1  # fresh start, not 2

    @pytest.mark.asyncio
    async def test_take_profit_close_does_not_increment(self, db):
        """Take profit close does NOT increment negative_sl_count regardless of PnL."""
        # Price above take_price triggers take_profit close
        pos = await _insert_position(db, entry_price=1.00)  # take_price = 1.15

        engine = _make_engine(db, dex_price=1.20)  # above take_price
        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        assert result.positions_closed[0].close_reason == "take_profit"

        skip = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip == 0
        # Counter should not exist or be 0
        count_after = await db.increment_negative_sl_count(TOKEN_1, "solana")
        assert count_after == 1  # fresh, not incremented by the close

    @pytest.mark.asyncio
    async def test_max_hold_time_close_does_not_increment(self, db):
        """max_hold_time close does NOT increment negative_sl_count."""
        # Use max_hold_hours=0 so any position is immediately expired
        pos = await _insert_position(db, entry_price=1.00)

        # Engine with max_hold_hours=0: position is closed by timeout even if price is neutral
        engine = _make_engine(db, dex_price=1.00)
        engine.config = _make_config(max_hold_hours=0)

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        assert result.positions_closed[0].close_reason == "max_hold_time"

        skip = await db.get_skip_phases(TOKEN_1, "solana")
        assert skip == 0
        count_after = await db.increment_negative_sl_count(TOKEN_1, "solana")
        assert count_after == 1  # fresh, not incremented
