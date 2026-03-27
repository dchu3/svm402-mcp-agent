"""Tests for /portfolio reset command and delete_closed_portfolio_data()."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from app.database import Database


@pytest.fixture
def temp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test_reset.db"


@pytest_asyncio.fixture
async def db(temp_db_path):
    database = Database(db_path=temp_db_path)
    await database.connect()
    yield database
    await database.close()


async def _add_position(db, symbol="TEST", status="open", chain="solana"):
    """Helper to add a position and optionally close it."""
    pos = await db.add_portfolio_position(
        token_address=f"0x{symbol.lower()}",
        symbol=symbol,
        chain=chain,
        entry_price=1.0,
        quantity_token=100.0,
        notional_usd=100.0,
        stop_price=0.9,
        take_price=1.15,
        dry_run=True,
    )
    if status == "closed":
        await db.close_portfolio_position(
            position_id=pos.id,
            exit_price=1.1,
            close_reason="test",
            realized_pnl_usd=10.0,
        )
        await db.record_portfolio_execution(
            position_id=pos.id,
            token_address=pos.token_address,
            symbol=symbol,
            chain=chain,
            action="sell",
            requested_notional_usd=100.0,
            executed_price=1.1,
            quantity_token=100.0,
            tx_hash="0xabc",
            success=True,
        )
    return pos


class TestDeleteClosedPortfolioData:
    """Tests for Database.delete_closed_portfolio_data()."""

    @pytest.mark.asyncio
    async def test_deletes_closed_positions(self, db):
        await _add_position(db, symbol="AAA", status="closed")
        await _add_position(db, symbol="BBB", status="closed")

        deleted = await db.delete_closed_portfolio_data()

        assert deleted == 2
        closed = await db.list_closed_portfolio_positions(limit=100)
        assert len(closed) == 0

    @pytest.mark.asyncio
    async def test_preserves_open_positions(self, db):
        await _add_position(db, symbol="OPEN", status="open")
        await _add_position(db, symbol="CLOSED", status="closed")

        deleted = await db.delete_closed_portfolio_data()

        assert deleted == 1
        open_positions = await db.list_open_portfolio_positions()
        assert len(open_positions) == 1
        assert open_positions[0].symbol == "OPEN"

    @pytest.mark.asyncio
    async def test_deletes_associated_executions(self, db):
        pos = await _add_position(db, symbol="EXE", status="closed")

        await db.delete_closed_portfolio_data()

        conn = await db._ensure_connected()
        cursor = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM portfolio_executions WHERE position_id = ?",
            (pos.id,),
        )
        row = await cursor.fetchone()
        assert row["cnt"] == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_none_closed(self, db):
        await _add_position(db, symbol="STILL_OPEN", status="open")

        deleted = await db.delete_closed_portfolio_data()

        assert deleted == 0

    @pytest.mark.asyncio
    async def test_resets_daily_pnl(self, db):
        await _add_position(db, symbol="PNL", status="closed")
        pnl_before = await db.get_daily_portfolio_pnl()
        assert pnl_before != 0.0

        await db.delete_closed_portfolio_data()

        pnl_after = await db.get_daily_portfolio_pnl()
        assert pnl_after == 0.0


class TestPortfolioResetCommand:
    """Tests for the /portfolio reset CLI command routing."""

    @pytest.mark.asyncio
    async def test_reset_confirmed(self, db):
        from app.cli import _cmd_portfolio

        await _add_position(db, symbol="DEL", status="closed")

        output = AsyncMock()
        output.info = lambda msg: None
        output.warning = lambda msg: None

        with patch("app.cli.input", return_value="yes"):
            await _cmd_portfolio(["reset"], output, db, None)

        closed = await db.list_closed_portfolio_positions(limit=100)
        assert len(closed) == 0

    @pytest.mark.asyncio
    async def test_reset_cancelled(self, db):
        from app.cli import _cmd_portfolio

        await _add_position(db, symbol="KEEP", status="closed")

        output = AsyncMock()
        output.info = lambda msg: None
        output.warning = lambda msg: None

        with patch("app.cli.input", return_value="no"):
            await _cmd_portfolio(["reset"], output, db, None)

        closed = await db.list_closed_portfolio_positions(limit=100)
        assert len(closed) == 1

    @pytest.mark.asyncio
    async def test_reset_no_closed_positions(self, db):
        from app.cli import _cmd_portfolio

        messages = []
        output = AsyncMock()
        output.info = lambda msg: messages.append(msg)
        output.warning = lambda msg: messages.append(msg)

        await _cmd_portfolio(["reset"], output, db, None)

        assert any("No closed" in m for m in messages)


class TestDuplicateOpenPositionMigration:
    """Tests for the dedup migration that runs during Database.connect()."""

    @pytest.mark.asyncio
    async def test_connect_deduplicates_open_positions(self, temp_db_path):
        """Duplicate open positions are closed on connect(), keeping the oldest."""
        import aiosqlite

        # Create schema WITHOUT the unique index (simulates old database).
        conn = await aiosqlite.connect(temp_db_path)
        # Use the base schema but skip the unique index (it's applied in connect()).
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS portfolio_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                chain TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity_token REAL NOT NULL,
                notional_usd REAL NOT NULL,
                stop_price REAL NOT NULL,
                take_price REAL NOT NULL,
                highest_price REAL NOT NULL,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                exit_price REAL,
                realized_pnl_usd REAL,
                status TEXT NOT NULL DEFAULT 'open',
                close_reason TEXT,
                dry_run INTEGER DEFAULT 1,
                momentum_score REAL,
                discovery_reasoning TEXT
            );
            CREATE TABLE IF NOT EXISTS portfolio_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                token_address TEXT NOT NULL,
                symbol TEXT NOT NULL,
                chain TEXT NOT NULL,
                action TEXT NOT NULL,
                requested_notional_usd REAL,
                executed_price REAL,
                quantity_token REAL,
                tx_hash TEXT,
                success INTEGER DEFAULT 0,
                error TEXT,
                metadata_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (position_id) REFERENCES portfolio_positions(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS token_skip_phases (
                token_address TEXT NOT NULL,
                chain TEXT NOT NULL,
                skip_phases INTEGER NOT NULL DEFAULT 0,
                negative_sl_count INTEGER NOT NULL DEFAULT 0,
                last_negative_sl_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (token_address, chain)
            );
            """
        )

        # Insert 3 open positions for same token/chain (duplicates).
        for i in range(3):
            await conn.execute(
                """
                INSERT INTO portfolio_positions
                    (token_address, symbol, chain, entry_price, quantity_token,
                     notional_usd, stop_price, take_price, highest_price, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                ("0xabc", "DUP", "solana", 1.0 + i * 0.1, 100.0, 100.0, 0.9, 1.15, 1.0),
            )
        # Insert a non-duplicate open position on a different chain.
        await conn.execute(
            """
            INSERT INTO portfolio_positions
                (token_address, symbol, chain, entry_price, quantity_token,
                 notional_usd, stop_price, take_price, highest_price, status)
            VALUES ('0xabc', 'DUP', 'ethereum', 2.0, 50.0, 50.0, 1.8, 2.3, 2.0, 'open')
            """,
        )
        await conn.commit()
        await conn.close()

        # Now open via Database.connect() which runs the migration.
        db = Database(db_path=temp_db_path)
        await db.connect()

        try:
            open_positions = await db.list_open_portfolio_positions()
            # Expect 1 open for solana + 1 open for ethereum = 2
            assert len(open_positions) == 2

            solana_open = [p for p in open_positions if p.chain == "solana"]
            assert len(solana_open) == 1
            # The oldest (MIN id) is kept — it has entry_price=1.0
            assert solana_open[0].entry_price == 1.0

            # Check that the duplicates were closed with the right reason.
            conn = await db._ensure_connected()
            cursor = await conn.execute(
                "SELECT * FROM portfolio_positions WHERE status = 'closed' AND close_reason = 'duplicate_cleanup'"
            )
            closed = await cursor.fetchall()
            assert len(closed) == 2

            # Verify the unique index prevents future duplicates.
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    """
                    INSERT INTO portfolio_positions
                        (token_address, symbol, chain, entry_price, quantity_token,
                         notional_usd, stop_price, take_price, highest_price, status)
                    VALUES ('0xabc', 'DUP', 'solana', 3.0, 10.0, 10.0, 2.7, 3.5, 3.0, 'open')
                    """,
                )

            # Verify case-insensitive uniqueness for token address.
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(
                    """
                    INSERT INTO portfolio_positions
                        (token_address, symbol, chain, entry_price, quantity_token,
                         notional_usd, stop_price, take_price, highest_price, status)
                    VALUES ('0xABC', 'DUP', 'solana', 3.1, 10.0, 10.0, 2.8, 3.6, 3.1, 'open')
                    """,
                )
        finally:
            await db.close()
