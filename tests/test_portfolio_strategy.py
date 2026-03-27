"""Tests for portfolio strategy engine: discovery cycle, exit checks, risk guards."""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

from app.portfolio_strategy import (
    PortfolioDiscoveryCycleResult,
    PortfolioExitCycleResult,
    PortfolioStrategyConfig,
    PortfolioStrategyEngine,
)
from app.portfolio_discovery import DiscoveryCandidate
from app.database import Database, PortfolioPosition


# ---------------------------------------------------------------------------
# Mock clients
# ---------------------------------------------------------------------------

SOL_MINT = "So11111111111111111111111111111111111111112"


class MockDexScreenerClient:
    def __init__(
        self,
        price_usd: float = 0.01,
        liquidity_usd: float = 50000.0,
        native_price_usd: float = 180.0,
    ) -> None:
        self.price_usd = price_usd
        self.liquidity_usd = liquidity_usd
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
                    "liquidity": {"usd": self.liquidity_usd},
                }
            ]
        }


class MockTraderClient:
    def __init__(self, price: float = 0.01, success: bool = True) -> None:
        self.price = price
        self.success = success
        self.tools: List[Dict[str, Any]] = [
            {
                "name": "getQuote",
                "inputSchema": {
                    "type": "object",
                    "required": ["chain", "inputMint", "outputMint", "amountUsd", "slippageBps", "side"],
                    "properties": {
                        "chain": {"type": "string"},
                        "inputMint": {"type": "string"},
                        "outputMint": {"type": "string"},
                        "amountUsd": {"type": "number"},
                        "slippageBps": {"type": "integer"},
                        "side": {"type": "string"},
                    },
                },
            },
            {
                "name": "swap",
                "inputSchema": {
                    "type": "object",
                    "required": ["chain", "inputMint", "outputMint", "amountUsd", "slippageBps", "side"],
                    "properties": {
                        "chain": {"type": "string"},
                        "inputMint": {"type": "string"},
                        "outputMint": {"type": "string"},
                        "amountUsd": {"type": "number"},
                        "slippageBps": {"type": "integer"},
                        "side": {"type": "string"},
                    },
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


class MockRugcheckClient:
    def __init__(self, score: float = 100.0) -> None:
        self.score = score

    async def call_tool(self, method: str, arguments: Dict[str, Any]) -> Any:
        return {"score_normalised": self.score, "risks": []}


class MockMCPManager:
    def __init__(
        self,
        dexscreener: MockDexScreenerClient,
        trader: MockTraderClient,
        rugcheck: Optional[MockRugcheckClient] = None,
    ) -> None:
        self._dexscreener = dexscreener
        self._trader = trader
        self._rugcheck = rugcheck or MockRugcheckClient()

    def get_client(self, name: str) -> Any:
        if name == "dexscreener":
            return self._dexscreener
        if name == "trader":
            return self._trader
        if name == "rugcheck":
            return self._rugcheck
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test_portfolio.db"


@pytest_asyncio.fixture
async def db(temp_db_path):
    database = Database(db_path=temp_db_path)
    await database.connect()
    yield database
    await database.close()


def _config(**overrides: Any) -> PortfolioStrategyConfig:
    defaults = {
        "enabled": True,
        "dry_run": True,
        "chain": "solana",
        "max_positions": 5,
        "position_size_usd": 5.0,
        "take_profit_pct": 15.0,
        "stop_loss_pct": 8.0,
        "trailing_stop_pct": 5.0,
        "max_hold_hours": 24,
        "discovery_interval_mins": 30,
        "price_check_seconds": 60,
        "daily_loss_limit_usd": 50.0,
        "min_volume_usd": 10000.0,
        "min_liquidity_usd": 5000.0,
        "min_market_cap_usd": 250000.0,
        "cooldown_seconds": 300,
        "min_momentum_score": 50.0,
        "max_slippage_bps": 300,
        "rpc_url": "https://test-rpc",
    }
    defaults.update(overrides)
    return PortfolioStrategyConfig(**defaults)


def test_config_requires_rpc_url_for_solana():
    with pytest.raises(ValueError, match="rpc_url is required"):
        _config(rpc_url="   ")


def test_config_normalizes_chain_whitespace():
    cfg = _config(chain=" Solana ")
    assert cfg.chain == "solana"


def _make_engine(
    db: Database,
    dex_price: float = 0.01,
    trader_price: float = 0.01,
    trader_success: bool = True,
    native_price: float = 180.0,
    **config_overrides: Any,
) -> PortfolioStrategyEngine:
    dex = MockDexScreenerClient(price_usd=dex_price, native_price_usd=native_price)
    trader = MockTraderClient(price=trader_price, success=trader_success)
    manager = MockMCPManager(dexscreener=dex, trader=trader)
    return PortfolioStrategyEngine(
        db=db,
        mcp_manager=manager,
        config=_config(**config_overrides),
        api_key="fake-key",
        model_name="test-model",
    )


async def _insert_position(
    db: Database,
    token_address: str = "TestToken111111111111111111111111111111111",
    symbol: str = "TEST",
    chain: str = "solana",
    entry_price: float = 0.01,
    quantity_token: float = 500.0,
    notional_usd: float = 5.0,
    stop_pct: float = 8.0,
    take_pct: float = 15.0,
    opened_at_offset_hours: float = 0.0,
) -> PortfolioPosition:
    """Insert a position into the DB and return it."""
    stop_price = entry_price * (1 - stop_pct / 100)
    take_price = float("inf") if take_pct == 0 else entry_price * (1 + take_pct / 100)
    pos = await db.add_portfolio_position(
        token_address=token_address,
        symbol=symbol,
        chain=chain,
        entry_price=entry_price,
        quantity_token=quantity_token,
        notional_usd=notional_usd,
        stop_price=stop_price,
        take_price=take_price,
        dry_run=True,
    )
    # If we need to backdate the position, update it directly
    if opened_at_offset_hours:
        async with db._lock:
            opened_at = datetime.now(timezone.utc) - timedelta(hours=opened_at_offset_hours)
            await db._connection.execute(
                "UPDATE portfolio_positions SET opened_at = ? WHERE id = ?",
                (opened_at.isoformat(), pos.id),
            )
            await db._connection.commit()
        pos.opened_at = opened_at
    return pos


# ---------------------------------------------------------------------------
# Exit checks
# ---------------------------------------------------------------------------


class TestExitChecks:
    """Tests for PortfolioStrategyEngine.run_exit_checks()."""

    @pytest.mark.asyncio
    async def test_stop_loss_triggers_close(self, db):
        """When price drops below stop_price, position closes with stop_loss reason."""
        pos = await _insert_position(db, entry_price=1.00)

        engine = _make_engine(db, dex_price=0.90)  # Below stop (1.00 * 0.92 = 0.92)

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        assert result.positions_closed[0].close_reason == "stop_loss"
        assert len(await db.list_open_portfolio_positions(chain="solana")) == 0

    @pytest.mark.asyncio
    async def test_take_profit_triggers_close(self, db):
        """When price rises above take_price, position closes with take_profit reason."""
        pos = await _insert_position(db, entry_price=1.00)

        engine = _make_engine(db, dex_price=1.20)  # Above take (1.00 * 1.15 = 1.15)

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        assert result.positions_closed[0].close_reason == "take_profit"

    @pytest.mark.asyncio
    async def test_zero_take_profit_never_triggers_take_profit_close(self, db):
        """When take_profit_pct=0, price far above entry never closes with take_profit."""
        pos = await _insert_position(db, entry_price=1.00, take_pct=0)

        engine = _make_engine(db, dex_price=10.00, take_profit_pct=0)  # 10x — no TP ceiling

        result = await engine.run_exit_checks()

        assert all(p.close_reason != "take_profit" for p in result.positions_closed)
        assert len(await db.list_open_portfolio_positions(chain="solana")) == 1

    @pytest.mark.asyncio
    async def test_max_hold_triggers_close(self, db):
        """Position closes after max hold time."""
        pos = await _insert_position(db, entry_price=1.00, opened_at_offset_hours=25.0)

        engine = _make_engine(db, dex_price=1.05)  # Price between SL and TP

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        assert result.positions_closed[0].close_reason == "max_hold_time"

    @pytest.mark.asyncio
    async def test_no_close_when_in_range(self, db):
        """Position stays open when price is between SL and TP."""
        pos = await _insert_position(db, entry_price=1.00)

        engine = _make_engine(db, dex_price=1.05)  # Between SL and TP

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 0
        assert result.positions_checked == 1
        assert len(await db.list_open_portfolio_positions(chain="solana")) == 1

    @pytest.mark.asyncio
    async def test_trailing_stop_ratchets_upward(self, db):
        """Trailing stop updates when price makes new high."""
        pos = await _insert_position(db, entry_price=1.00)
        original_stop = pos.stop_price

        # Price rises — should update trailing stop
        engine = _make_engine(db, dex_price=1.10)

        result = await engine.run_exit_checks()

        assert result.trailing_stops_updated == 1
        assert len(result.positions_closed) == 0

        # Verify stop was ratcheted up
        updated = await db.get_open_portfolio_position(pos.token_address, "solana")
        assert updated is not None
        assert updated.stop_price > original_stop
        assert updated.highest_price == 1.10

    @pytest.mark.asyncio
    async def test_trailing_stop_never_lowers(self, db):
        """Stop price never decreases even when price drops."""
        pos = await _insert_position(db, entry_price=1.00)

        engine = _make_engine(db, dex_price=1.10)
        # First check: raise trailing stop
        await engine.run_exit_checks()
        updated = await db.get_open_portfolio_position(pos.token_address, "solana")
        high_stop = updated.stop_price

        # Second check: price drops but still above stop
        engine2 = _make_engine(db, dex_price=1.06)
        result = await engine2.run_exit_checks()

        updated2 = await db.get_open_portfolio_position(pos.token_address, "solana")
        assert updated2.stop_price >= high_stop  # Never lowered

    @pytest.mark.asyncio
    async def test_pnl_calculation_on_close(self, db):
        """Realized PnL is calculated correctly on exit."""
        pos = await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
        )

        engine = _make_engine(db, dex_price=1.20, trader_price=1.20)

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        closed = result.positions_closed[0]
        expected_pnl = (1.20 - 1.00) * 100.0  # $20.00
        assert closed.realized_pnl_usd == pytest.approx(expected_pnl, rel=0.01)

    @pytest.mark.asyncio
    async def test_partial_sell_uses_sell_pct(self, db):
        """When sell_pct < 100 and position is profitable, position stays open with reduced qty."""
        await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
        )

        engine = _make_engine(db, dex_price=1.20, trader_price=1.20, sell_pct=90.0)

        result = await engine.run_exit_checks()

        # Position is NOT fully closed — it was partially sold
        assert len(result.positions_closed) == 0
        assert result.positions_partially_sold == 1

        # Remaining position is still open with reduced quantity
        open_positions = await db.list_open_portfolio_positions(chain="solana")
        assert len(open_positions) == 1
        pos = open_positions[0]
        assert pos.quantity_token == pytest.approx(10.0, rel=0.01)  # 100 - 90
        assert pos.notional_usd == pytest.approx(10.0, rel=0.01)  # 10 * 1.00 entry

    @pytest.mark.asyncio
    async def test_sell_pct_ignored_on_loss(self, db):
        """When position is at a loss, sell_pct is ignored and 100% is sold."""
        await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
        )

        # Price dropped below stop-loss → loss → should sell all 100 tokens
        engine = _make_engine(db, dex_price=0.90, trader_price=0.90, sell_pct=90.0)

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        closed = result.positions_closed[0]
        # Full 100 tokens sold despite sell_pct=90
        expected_pnl = (0.90 - 1.00) * 100.0  # -$10.00
        assert closed.realized_pnl_usd == pytest.approx(expected_pnl, rel=0.01)

    @pytest.mark.asyncio
    async def test_sell_pct_ignored_at_breakeven(self, db):
        """At breakeven (current_price == entry_price), sell_pct is ignored and 100% is sold.

        PnL must be strictly positive for a partial sell to apply.
        """
        await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
            opened_at_offset_hours=25.0,  # trigger max_hold_time exit
        )

        # Exactly at entry price — not profitable, so expect full sell
        engine = _make_engine(db, dex_price=1.00, trader_price=1.00, sell_pct=90.0)

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        closed = result.positions_closed[0]
        # Full 100 tokens sold — breakeven is not profitable
        assert closed.realized_pnl_usd == pytest.approx(0.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_sell_pct_ignored_on_max_hold_loss(self, db):
        """max_hold_time exit at a loss ignores sell_pct and sells 100%."""
        await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
            opened_at_offset_hours=25.0,  # trigger max_hold_time exit
        )

        # Price between SL and TP but below entry — loss on timeout
        engine = _make_engine(db, dex_price=0.95, trader_price=0.95, sell_pct=90.0)

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        closed = result.positions_closed[0]
        assert closed.close_reason == "max_hold_time"
        # Full 100 tokens sold despite sell_pct=90
        expected_pnl = (0.95 - 1.00) * 100.0  # -$5.00
        assert closed.realized_pnl_usd == pytest.approx(expected_pnl, rel=0.01)

    @pytest.mark.asyncio
    async def test_default_sell_pct_is_100(self, db):
        """Default sell_pct of 100 sells the full position quantity."""
        await _insert_position(
            db, entry_price=1.00, quantity_token=50.0, notional_usd=50.0,
        )

        engine = _make_engine(db, dex_price=1.20, trader_price=1.20)

        result = await engine.run_exit_checks()

        assert len(result.positions_closed) == 1
        closed = result.positions_closed[0]
        # Full 50 tokens sold
        expected_pnl = (1.20 - 1.00) * 50.0  # $10.00
        assert closed.realized_pnl_usd == pytest.approx(expected_pnl, rel=0.01)

    @pytest.mark.asyncio
    async def test_partial_sell_continues_trailing_stop(self, db):
        """After partial sell, remaining position keeps trailing stop and can exit again."""
        await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
        )

        # First exit: price at 1.20 triggers take_profit, partial sell 50%
        engine = _make_engine(db, dex_price=1.20, trader_price=1.20, sell_pct=50.0)
        result = await engine.run_exit_checks()

        assert result.positions_partially_sold == 1
        assert len(result.positions_closed) == 0

        open_positions = await db.list_open_portfolio_positions(chain="solana")
        assert len(open_positions) == 1
        remaining = open_positions[0]
        assert remaining.quantity_token == pytest.approx(50.0, rel=0.01)
        # Trailing stop should be reset relative to exit price
        expected_stop = 1.20 * (1 - 5.0 / 100)  # 1.14
        assert remaining.stop_price == pytest.approx(expected_stop, rel=0.01)
        # Take-profit should be bumped to avoid immediate re-trigger
        expected_take = max(1.00 * (1 + 15.0 / 100), 1.20 * (1 + 15.0 / 100))  # 1.38
        assert remaining.take_price == pytest.approx(expected_take, rel=0.01)

    @pytest.mark.asyncio
    async def test_partial_sell_then_loss_sells_remaining(self, db):
        """After partial sell, if price drops below entry, next cycle sells 100% of remaining."""
        await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
        )

        # First: profitable partial sell at 1.20 with 50%
        engine = _make_engine(db, dex_price=1.20, trader_price=1.20, sell_pct=50.0)
        await engine.run_exit_checks()

        open_positions = await db.list_open_portfolio_positions(chain="solana")
        assert len(open_positions) == 1
        assert open_positions[0].quantity_token == pytest.approx(50.0, rel=0.01)

        # Second: price drops below stop → loss exit sells 100% of remaining
        engine2 = _make_engine(db, dex_price=0.50, trader_price=0.50, sell_pct=50.0)
        result2 = await engine2.run_exit_checks()

        assert len(result2.positions_closed) == 1
        assert result2.positions_partially_sold == 0
        assert len(await db.list_open_portfolio_positions(chain="solana")) == 0

    @pytest.mark.asyncio
    async def test_partial_sell_cumulative_pnl(self, db):
        """realized_pnl_usd on final close includes PnL from all prior partial sells."""
        await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
        )

        # First: profitable partial sell at 1.20, sell 50%
        # partial PnL = (1.20 - 1.00) * 50 = $10.00
        engine = _make_engine(db, dex_price=1.20, trader_price=1.20, sell_pct=50.0)
        result1 = await engine.run_exit_checks()
        assert result1.positions_partially_sold == 1

        # Second: remaining 50 tokens, price drops below stop → loss exit
        # final PnL = (0.50 - 1.00) * 50 = -$25.00
        engine2 = _make_engine(db, dex_price=0.50, trader_price=0.50, sell_pct=50.0)
        result2 = await engine2.run_exit_checks()
        assert len(result2.positions_closed) == 1

        closed = result2.positions_closed[0]
        # Total cumulative PnL = $10.00 + (-$25.00) = -$15.00
        expected_total_pnl = (1.20 - 1.00) * 50 + (0.50 - 1.00) * 50
        assert closed.realized_pnl_usd == pytest.approx(expected_total_pnl, abs=0.01)

        # Verify the DB row also reflects the cumulative total
        closed_positions = await db.list_closed_portfolio_positions()
        assert closed_positions[0].realized_pnl_usd == pytest.approx(expected_total_pnl, abs=0.01)

    @pytest.mark.asyncio
    async def test_stop_loss_with_positive_cumulative_pnl_does_not_increment_negative_sl_count(self, db):
        """Negative SL counter should use cumulative PnL after partial-sell sequences."""
        await _insert_position(
            db, entry_price=1.00, quantity_token=100.0, notional_usd=100.0,
        )

        # First: profitable partial sell, realize +$30.00 on 50 tokens.
        engine = _make_engine(db, dex_price=1.60, trader_price=1.60, sell_pct=50.0)
        first = await engine.run_exit_checks()
        assert first.positions_partially_sold == 1

        # Second: stop-loss close on remaining 50 at $0.60 realizes -$20.00.
        # Cumulative PnL remains positive (+$10.00), so negative SL count should not increment.
        engine2 = _make_engine(db, dex_price=0.60, trader_price=0.60, sell_pct=50.0)
        second = await engine2.run_exit_checks()
        assert len(second.positions_closed) == 1
        assert second.positions_closed[0].close_reason == "stop_loss"
        assert second.positions_closed[0].realized_pnl_usd == pytest.approx(10.0, abs=0.01)

        skip = await db.get_skip_phases("TestToken111111111111111111111111111111111", "solana")
        assert skip == 0

    @pytest.mark.asyncio
    async def test_partial_sell_dust_forces_full_close(self, db):
        """When remaining value after partial sell is dust (<$0.01), force full close."""
        # Tiny position: 10 tokens at $0.001 = $0.01 notional
        await _insert_position(
            db, entry_price=0.001, quantity_token=10.0, notional_usd=0.01,
        )

        # Price up to 0.0012, sell 90% → remaining 1 token × $0.0012 = $0.0012 < $0.01
        engine = _make_engine(db, dex_price=0.0012, trader_price=0.0012, sell_pct=90.0)
        result = await engine.run_exit_checks()

        # Should fully close (dust threshold), not partial
        assert len(result.positions_closed) == 1
        assert result.positions_partially_sold == 0
        assert len(await db.list_open_portfolio_positions(chain="solana")) == 0

    @pytest.mark.asyncio
    async def test_no_positions_exits_early(self, db):
        """Exit check returns quickly when no open positions."""
        engine = _make_engine(db)

        result = await engine.run_exit_checks()

        assert result.positions_checked == 0
        assert result.summary == "No open positions"

    @pytest.mark.asyncio
    async def test_disabled_returns_early(self, db):
        """Engine does nothing when disabled."""
        engine = _make_engine(db, enabled=False)

        result = await engine.run_exit_checks()

        assert result.summary == "Portfolio strategy disabled"


# ---------------------------------------------------------------------------
# Discovery cycle
# ---------------------------------------------------------------------------


class TestDiscoveryCycle:
    """Tests for PortfolioStrategyEngine.run_discovery_cycle()."""

    @pytest.mark.asyncio
    async def test_disabled_returns_early(self, db):
        engine = _make_engine(db, enabled=False)

        result = await engine.run_discovery_cycle()

        assert result.summary == "Portfolio strategy disabled"

    @pytest.mark.asyncio
    async def test_full_portfolio_skips(self, db):
        """When max positions reached, discovery skips."""
        for i in range(5):
            await _insert_position(
                db, token_address=f"Token{i}{'1' * 38}", symbol=f"T{i}",
            )

        engine = _make_engine(db, max_positions=5)

        result = await engine.run_discovery_cycle()

        assert "full" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_daily_loss_limit_skips(self, db):
        """Discovery skips when daily loss limit is reached."""
        # Create and close a losing position to accumulate loss
        pos = await _insert_position(
            db, entry_price=10.0, quantity_token=10.0, notional_usd=100.0,
        )
        await db.close_portfolio_position(
            position_id=pos.id,
            exit_price=4.0,
            close_reason="stop_loss",
            realized_pnl_usd=-60.0,
        )

        engine = _make_engine(db, daily_loss_limit_usd=50.0)

        result = await engine.run_discovery_cycle()

        assert "daily loss limit" in result.summary.lower()


# ---------------------------------------------------------------------------
# Risk guards
# ---------------------------------------------------------------------------


class TestRiskGuards:
    """Test risk guards in the strategy engine."""

    @pytest.mark.asyncio
    async def test_exit_reason_stop_loss(self, db):
        engine = _make_engine(db)
        now = datetime.now(timezone.utc)
        pos = PortfolioPosition(
            id=1, token_address="t", symbol="T", chain="solana",
            entry_price=1.0, quantity_token=10, notional_usd=10,
            stop_price=0.92, take_price=1.15, highest_price=1.0,
            opened_at=now,
        )

        assert engine._exit_reason(pos, 0.91, now) == "stop_loss"
        assert engine._exit_reason(pos, 0.92, now) == "stop_loss"

    @pytest.mark.asyncio
    async def test_exit_reason_take_profit(self, db):
        engine = _make_engine(db)
        now = datetime.now(timezone.utc)
        pos = PortfolioPosition(
            id=1, token_address="t", symbol="T", chain="solana",
            entry_price=1.0, quantity_token=10, notional_usd=10,
            stop_price=0.92, take_price=1.15, highest_price=1.0,
            opened_at=now,
        )

        assert engine._exit_reason(pos, 1.15, now) == "take_profit"
        assert engine._exit_reason(pos, 1.50, now) == "take_profit"

    @pytest.mark.asyncio
    async def test_exit_reason_no_take_profit_when_disabled(self, db):
        """When take_price is inf (TP disabled), price far above entry returns None."""
        engine = _make_engine(db, take_profit_pct=0)
        now = datetime.now(timezone.utc)
        pos = PortfolioPosition(
            id=1, token_address="t", symbol="T", chain="solana",
            entry_price=1.0, quantity_token=10, notional_usd=10,
            stop_price=0.92, take_price=float("inf"), highest_price=1.0,
            opened_at=now,
        )

        assert engine._exit_reason(pos, 100.0, now) is None

    @pytest.mark.asyncio
    async def test_exit_reason_max_hold(self, db):
        engine = _make_engine(db, max_hold_hours=24)
        now = datetime.now(timezone.utc)
        pos = PortfolioPosition(
            id=1, token_address="t", symbol="T", chain="solana",
            entry_price=1.0, quantity_token=10, notional_usd=10,
            stop_price=0.92, take_price=1.15, highest_price=1.0,
            opened_at=now - timedelta(hours=25),
        )

        assert engine._exit_reason(pos, 1.05, now) == "max_hold_time"

    @pytest.mark.asyncio
    async def test_exit_reason_none_in_range(self, db):
        engine = _make_engine(db)
        now = datetime.now(timezone.utc)
        pos = PortfolioPosition(
            id=1, token_address="t", symbol="T", chain="solana",
            entry_price=1.0, quantity_token=10, notional_usd=10,
            stop_price=0.92, take_price=1.15, highest_price=1.0,
            opened_at=now,
        )

        assert engine._exit_reason(pos, 1.05, now) is None


# ---------------------------------------------------------------------------
# Reference price parsing
# ---------------------------------------------------------------------------


class TestParseReferenceResult:
    """Test the DexScreener response parser."""

    def test_parses_pairs_list(self):
        result = {
            "pairs": [{"priceUsd": "1.50", "liquidity": {"usd": 25000}}]
        }
        price, liq = PortfolioStrategyEngine._parse_reference_result(result)
        assert price == 1.50
        assert liq == 25000.0

    def test_parses_raw_list(self):
        result = [{"priceUsd": "2.00", "liquidity": {"usd": 10000}}]
        price, liq = PortfolioStrategyEngine._parse_reference_result(result)
        assert price == 2.00
        assert liq == 10000.0

    def test_raises_on_empty(self):
        with pytest.raises(RuntimeError, match="no pairs"):
            PortfolioStrategyEngine._parse_reference_result({"pairs": []})

    def test_raises_on_missing_price(self):
        with pytest.raises(RuntimeError, match="missing priceUsd"):
            PortfolioStrategyEngine._parse_reference_result(
                {"pairs": [{"liquidity": {"usd": 100}}]}
            )

    def test_handles_missing_liquidity(self):
        result = {"pairs": [{"priceUsd": "1.0"}]}
        price, liq = PortfolioStrategyEngine._parse_reference_result(result)
        assert price == 1.0
        assert liq is None

    def test_selects_highest_liquidity_pair(self):
        """When multiple pairs are present, the one with the highest liquidity is used."""
        result = {
            "pairs": [
                {"priceUsd": "0.50", "liquidity": {"usd": 5_000}},
                {"priceUsd": "1.50", "liquidity": {"usd": 500_000}},
                {"priceUsd": "0.75", "liquidity": {"usd": 50_000}},
            ]
        }
        price, liq = PortfolioStrategyEngine._parse_reference_result(result)
        assert price == pytest.approx(1.50)
        assert liq == pytest.approx(500_000.0)

    def test_handles_none_liquidity_usd(self):
        """Pair with liquidity.usd=None should not crash — treated as 0."""
        result = {
            "pairs": [
                {"priceUsd": "1.00", "liquidity": {"usd": None}},
                {"priceUsd": "2.00", "liquidity": {"usd": 10_000}},
            ]
        }
        price, liq = PortfolioStrategyEngine._parse_reference_result(result)
        assert price == pytest.approx(2.00)
        assert liq == pytest.approx(10_000.0)

    def test_handles_non_numeric_liquidity_usd(self):
        """Pair with non-numeric liquidity.usd (e.g. 'N/A') should not crash."""
        result = {
            "pairs": [
                {"priceUsd": "3.00", "liquidity": {"usd": "N/A"}},
                {"priceUsd": "4.00", "liquidity": {"usd": 20_000}},
            ]
        }
        price, liq = PortfolioStrategyEngine._parse_reference_result(result)
        assert price == pytest.approx(4.00)
        assert liq == pytest.approx(20_000.0)


# ---------------------------------------------------------------------------
# Slippage probe integration tests
# ---------------------------------------------------------------------------


class TestSlippageProbeInOpenPosition:
    """Tests for the slippage probe gate inside _open_position()."""

    def _candidate(self) -> DiscoveryCandidate:
        from app.portfolio_discovery import DiscoveryCandidate
        return DiscoveryCandidate(
            token_address="ProbeToken1111111111111111111111111111111",
            symbol="PROB",
            chain="solana",
            price_usd=0.01,
            volume_24h=100_000.0,
            liquidity_usd=50_000.0,
            market_cap_usd=500_000.0,
            momentum_score=80.0,
            safety_status="Safe",
            reasoning="test candidate",
        )

    @pytest.mark.asyncio
    async def test_probe_disabled_skips_probe(self, db):
        """When slippage_probe_enabled=False, probe_slippage is never called."""
        from unittest.mock import AsyncMock, patch

        engine = _make_engine(db, slippage_probe_enabled=False, dry_run=False)
        engine._native_price_usd = 180.0

        with patch.object(engine.execution, "probe_slippage", new_callable=AsyncMock) as mock_probe, \
             patch.object(engine.execution, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(engine.execution, "execute_trade", new_callable=AsyncMock) as mock_exec:

            from app.execution import TradeQuote, TradeExecution
            mock_quote.return_value = TradeQuote(price=0.01, liquidity_usd=50_000.0, method="mock", raw={})
            mock_exec.return_value = TradeExecution(
                success=True, method="mock", raw={}, executed_price=0.01, quantity_token=500.0, tx_hash="abc"
            )

            await engine._open_position(self._candidate())

        mock_probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_enabled_acceptable_slippage_opens_position(self, db):
        """Probe returns no abort → position is opened normally."""
        from unittest.mock import AsyncMock, patch

        engine = _make_engine(db, slippage_probe_enabled=True, dry_run=False)
        engine._native_price_usd = 180.0

        with patch.object(
            engine.execution, "probe_slippage", new_callable=AsyncMock,
            return_value=(False, 1.5, None),
        ), \
             patch.object(engine.execution, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(engine.execution, "execute_trade", new_callable=AsyncMock) as mock_exec:

            from app.execution import TradeQuote, TradeExecution
            mock_quote.return_value = TradeQuote(price=0.01, liquidity_usd=50_000.0, method="mock", raw={})
            mock_exec.return_value = TradeExecution(
                success=True, method="mock", raw={}, executed_price=0.01, quantity_token=500.0, tx_hash="abc"
            )

            position = await engine._open_position(self._candidate())

        assert position is not None

    @pytest.mark.asyncio
    async def test_stale_native_price_refreshes_before_quote_and_execution(self, db):
        """When native price is stale, _open_position refreshes before quote/execution."""
        from unittest.mock import AsyncMock, patch
        from app.execution import TradeExecution, TradeQuote

        engine = _make_engine(db, slippage_probe_enabled=False, dry_run=False)
        engine._native_price_usd = 180.0
        engine._native_price_updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        call_order: List[str] = []

        async def _refresh() -> None:
            call_order.append("refresh")

        async def _quote(*args, **kwargs):
            call_order.append("quote")
            return TradeQuote(price=0.01, liquidity_usd=50_000.0, method="mock", raw={})

        async def _execute(*args, **kwargs):
            call_order.append("execute")
            return TradeExecution(
                success=True, method="mock", raw={}, executed_price=0.01, quantity_token=500.0, tx_hash="abc"
            )

        with patch.object(engine, "_refresh_native_price", new_callable=AsyncMock) as mock_refresh, \
             patch.object(engine.execution, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(engine.execution, "execute_trade", new_callable=AsyncMock) as mock_exec:
            mock_refresh.side_effect = _refresh
            mock_quote.side_effect = _quote
            mock_exec.side_effect = _execute

            await engine._open_position(self._candidate())

        mock_refresh.assert_awaited_once()
        assert call_order[:3] == ["refresh", "quote", "execute"]

    @pytest.mark.asyncio
    async def test_price_deviation_emits_warning_log_callback(self, db, caplog):
        """Large quote/execution deviation should log both logger warning and callback warning."""
        from unittest.mock import AsyncMock, patch
        from app.execution import TradeExecution, TradeQuote

        engine = _make_engine(db, slippage_probe_enabled=False, dry_run=False)
        callback_logs: List[tuple[str, str, Optional[Dict[str, Any]]]] = []
        engine.verbose = True
        engine.log_callback = lambda level, message, data: callback_logs.append((level, message, data))
        caplog.set_level(logging.WARNING, logger="app.portfolio_strategy")

        with patch.object(engine.execution, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(engine.execution, "execute_trade", new_callable=AsyncMock) as mock_exec:
            mock_quote.return_value = TradeQuote(price=1.0, liquidity_usd=50_000.0, method="mock", raw={})
            mock_exec.return_value = TradeExecution(
                success=True, method="mock", raw={}, executed_price=1.2, quantity_token=5.0, tx_hash="abc"
            )

            position = await engine._open_position(self._candidate())

        assert position is not None
        warning_logs = [entry for entry in callback_logs if entry[0] == "warning"]
        assert len(warning_logs) == 1
        _, message, data = warning_logs[0]
        assert "Price deviation 20.0% on PROB buy" in message
        assert data is not None
        assert data["symbol"] == "PROB"
        assert data["quote_price"] == pytest.approx(1.0)
        assert data["executed_price"] == pytest.approx(1.2)
        assert data["deviation_pct"] == pytest.approx(20.0)
        assert any(
            "Price deviation 20.0% on PROB buy: quoted=$1.0000000000 executed=$1.2000000000"
            in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_probe_enabled_excessive_slippage_aborts(self, db):
        """Probe returns should_abort=True → _open_position returns None, no buy executed."""
        from unittest.mock import AsyncMock, patch

        engine = _make_engine(db, slippage_probe_enabled=True, dry_run=False)
        engine._native_price_usd = 180.0

        with patch.object(
            engine.execution, "probe_slippage", new_callable=AsyncMock,
            return_value=(True, 12.5, "probe slippage 12.5% exceeds threshold 5.0%"),
        ), \
             patch.object(engine.execution, "execute_trade", new_callable=AsyncMock) as mock_exec:

            position = await engine._open_position(self._candidate())

        assert position is None
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_skipped_in_dry_run(self, db):
        """Probe is never called in dry-run mode even if enabled."""
        from unittest.mock import AsyncMock, patch

        engine = _make_engine(db, slippage_probe_enabled=True, dry_run=True)
        engine._native_price_usd = 180.0

        with patch.object(engine.execution, "probe_slippage", new_callable=AsyncMock) as mock_probe, \
             patch.object(engine.execution, "get_quote", new_callable=AsyncMock) as mock_quote, \
             patch.object(engine.execution, "execute_trade", new_callable=AsyncMock) as mock_exec:

            from app.execution import TradeQuote, TradeExecution
            mock_quote.return_value = TradeQuote(price=0.01, liquidity_usd=50_000.0, method="mock", raw={})
            mock_exec.return_value = TradeExecution(
                success=True, method="mock", raw={}, executed_price=0.01, quantity_token=500.0, tx_hash=None
            )

            await engine._open_position(self._candidate())

        mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# SOL trend gate tests
# ---------------------------------------------------------------------------


def _async_return(value):
    """Create an async function that returns a fixed value."""
    async def _inner(*args, **kwargs):
        return value
    return _inner


class TestSolTrendGate:
    """Tests for the SOL market trend gate in discovery cycle."""

    @pytest.mark.asyncio
    async def test_discovery_skipped_when_sol_dumping(self, db):
        """Discovery should be skipped when SOL price drops beyond threshold."""
        engine = _make_engine(db, sol_dump_threshold_pct=-5.0, sol_trend_lookback_mins=60)

        # Simulate price history: SOL dropped from 200 to 185 (-7.5%)
        now = datetime.now(timezone.utc)
        engine._native_price_history.append((now - timedelta(minutes=50), 200.0))
        engine._native_price_history.append((now - timedelta(minutes=30), 195.0))
        engine._native_price_history.append((now - timedelta(minutes=1), 185.0))
        engine._native_price_usd = 185.0
        engine._native_price_updated_at = now

        result = await engine.run_discovery_cycle()
        assert "SOL trend" in result.summary
        assert "-7.5%" in result.summary

    @pytest.mark.asyncio
    async def test_discovery_allowed_when_sol_sideways(self, db):
        """Discovery should proceed when SOL is moving sideways."""
        engine = _make_engine(db, sol_dump_threshold_pct=-5.0, sol_trend_lookback_mins=60)
        engine.discovery.discover = _async_return([])

        now = datetime.now(timezone.utc)
        engine._native_price_history.append((now - timedelta(minutes=50), 200.0))
        engine._native_price_history.append((now - timedelta(minutes=1), 198.0))
        engine._native_price_usd = 198.0
        engine._native_price_updated_at = now

        result = await engine.run_discovery_cycle()
        # Should pass trend gate — summary should NOT mention SOL trend
        assert "SOL trend" not in result.summary

    @pytest.mark.asyncio
    async def test_discovery_allowed_when_sol_rising(self, db):
        """Discovery should proceed when SOL is trending up."""
        engine = _make_engine(db, sol_dump_threshold_pct=-5.0, sol_trend_lookback_mins=60)
        engine.discovery.discover = _async_return([])

        now = datetime.now(timezone.utc)
        engine._native_price_history.append((now - timedelta(minutes=50), 200.0))
        engine._native_price_history.append((now - timedelta(minutes=1), 220.0))
        engine._native_price_usd = 220.0
        engine._native_price_updated_at = now

        result = await engine.run_discovery_cycle()
        assert "SOL trend" not in result.summary

    @pytest.mark.asyncio
    async def test_discovery_allowed_with_insufficient_history(self, db):
        """Discovery should proceed (fail-open) when not enough price data."""
        engine = _make_engine(db, sol_dump_threshold_pct=-5.0, sol_trend_lookback_mins=60)
        engine.discovery.discover = _async_return([])

        # Only one data point — insufficient for trend calculation
        now = datetime.now(timezone.utc)
        engine._native_price_history.append((now - timedelta(minutes=1), 200.0))
        engine._native_price_usd = 200.0
        engine._native_price_updated_at = now

        result = await engine.run_discovery_cycle()
        assert "SOL trend" not in result.summary

    @pytest.mark.asyncio
    async def test_custom_threshold_respected(self, db):
        """A stricter threshold (-3%) should trigger skip on a smaller drop."""
        engine = _make_engine(db, sol_dump_threshold_pct=-3.0, sol_trend_lookback_mins=60)

        now = datetime.now(timezone.utc)
        # SOL dropped from 200 to 192 (-4%)
        engine._native_price_history.append((now - timedelta(minutes=50), 200.0))
        engine._native_price_history.append((now - timedelta(minutes=1), 192.0))
        engine._native_price_usd = 192.0
        engine._native_price_updated_at = now

        result = await engine.run_discovery_cycle()
        assert "SOL trend" in result.summary
        assert "threshold -3.0%" in result.summary

    @pytest.mark.asyncio
    async def test_threshold_boundary_not_triggered(self, db):
        """A drop exactly at the threshold should NOT trigger skip (< not <=)."""
        engine = _make_engine(db, sol_dump_threshold_pct=-5.0, sol_trend_lookback_mins=60)
        engine.discovery.discover = _async_return([])

        now = datetime.now(timezone.utc)
        # SOL dropped from 200 to 190 = exactly -5.0%
        engine._native_price_history.append((now - timedelta(minutes=50), 200.0))
        engine._native_price_history.append((now - timedelta(minutes=1), 190.0))
        engine._native_price_usd = 190.0
        engine._native_price_updated_at = now

        result = await engine.run_discovery_cycle()
        # Exactly at threshold — should NOT skip
        assert "SOL trend" not in result.summary

    @pytest.mark.asyncio
    async def test_exits_still_run_during_sol_dump(self, db):
        """Exit checks should always run regardless of SOL trend."""
        engine = _make_engine(db, sol_dump_threshold_pct=-5.0, sol_trend_lookback_mins=60)

        now = datetime.now(timezone.utc)
        # Simulate a dump
        engine._native_price_history.append((now - timedelta(minutes=50), 200.0))
        engine._native_price_history.append((now - timedelta(minutes=1), 180.0))
        engine._native_price_usd = 180.0
        engine._native_price_updated_at = now

        result = await engine.run_exit_checks()
        # Should complete normally — no "SOL trend" skip
        assert result.summary is None or "SOL trend" not in (result.summary or "")

    @pytest.mark.asyncio
    async def test_price_history_recorded_on_refresh(self, db):
        """_refresh_native_price should append to the price history deque."""
        engine = _make_engine(db, native_price=185.0)

        assert len(engine._native_price_history) == 0
        await engine._refresh_native_price()
        assert len(engine._native_price_history) == 1
        assert engine._native_price_history[0][1] == 185.0
