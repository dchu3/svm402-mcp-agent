"""Portfolio database persistence with SQLite."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite


def _normalize_symbol(symbol: str) -> str:
    """Strip emoji/special character prefixes from symbols."""
    return re.sub(r'^[^\w]+', '', symbol).upper()


DEFAULT_DB_PATH = Path.home() / ".dex-bot" / "portfolio.db"

SCHEMA = """
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

CREATE INDEX IF NOT EXISTS idx_portfolio_positions_status
ON portfolio_positions(status, chain);

CREATE INDEX IF NOT EXISTS idx_portfolio_executions_position
ON portfolio_executions(position_id);

CREATE TABLE IF NOT EXISTS token_skip_phases (
    token_address TEXT NOT NULL,
    chain TEXT NOT NULL,
    skip_phases INTEGER NOT NULL DEFAULT 0,
    negative_sl_count INTEGER NOT NULL DEFAULT 0,
    last_negative_sl_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (token_address, chain)
);

CREATE TABLE IF NOT EXISTS discovery_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    symbol TEXT NOT NULL,
    chain TEXT NOT NULL,
    decision_label TEXT NOT NULL,
    price_usd REAL,
    volume_24h REAL,
    liquidity_usd REAL,
    market_cap_usd REAL,
    momentum_score REAL,
    reasoning TEXT,
    metadata_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_discovery_decisions_cycle
ON discovery_decisions(cycle_id);

CREATE INDEX IF NOT EXISTS idx_discovery_decisions_token
ON discovery_decisions(token_address, chain);

CREATE TABLE IF NOT EXISTS shadow_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    symbol TEXT NOT NULL,
    chain TEXT NOT NULL,
    entry_price REAL NOT NULL,
    notional_usd REAL NOT NULL,
    momentum_score REAL,
    reasoning TEXT,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    check_after_minutes INTEGER NOT NULL DEFAULT 30,
    checked_at TIMESTAMP,
    price_at_check REAL,
    pnl_pct REAL,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_shadow_positions_status
ON shadow_positions(status);
"""


@dataclass
class PortfolioPosition:
    """Represents an open/closed portfolio strategy position."""

    id: int
    token_address: str
    symbol: str
    chain: str
    entry_price: float
    quantity_token: float
    notional_usd: float
    stop_price: float
    take_price: float
    highest_price: float
    opened_at: datetime
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    realized_pnl_usd: Optional[float] = None
    status: str = "open"
    close_reason: Optional[str] = None
    dry_run: bool = True
    momentum_score: Optional[float] = None
    discovery_reasoning: Optional[str] = None


class Database:
    """Async SQLite manager for portfolio strategy data."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self._connection: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Initialize database connection and schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row

        await self._connection.execute("PRAGMA foreign_keys = ON")
        await self._connection.executescript(SCHEMA)

        # Deduplicate open positions before unique index is enforced.
        # Keeps the oldest open row per (token_address, chain) and closes
        # any newer duplicates so the UNIQUE partial index can be created
        # safely on databases that pre-date this constraint.
        await self._connection.execute(
            """
            UPDATE portfolio_positions
            SET status = 'closed',
                close_reason = 'duplicate_cleanup',
                closed_at = CURRENT_TIMESTAMP
            WHERE status = 'open'
              AND id NOT IN (
                  SELECT MIN(id)
                  FROM portfolio_positions
                  WHERE status = 'open'
                  GROUP BY LOWER(token_address), chain
              )
            """
        )

        # Create the unique partial index after dedup to avoid failure on
        # databases that already contain duplicate open positions.
        # Use LOWER(token_address) to match case-insensitive dedup/query logic.
        await self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_positions_unique_open
            ON portfolio_positions(LOWER(token_address), chain) WHERE status = 'open'
            """
        )

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

    # --- Portfolio Position Operations ---

    async def add_portfolio_position(
        self,
        token_address: str,
        symbol: str,
        chain: str,
        entry_price: float,
        quantity_token: float,
        notional_usd: float,
        stop_price: float,
        take_price: float,
        dry_run: bool = True,
        momentum_score: Optional[float] = None,
        discovery_reasoning: Optional[str] = None,
    ) -> PortfolioPosition:
        """Create a new portfolio strategy position."""
        conn = await self._ensure_connected()
        async with self._lock:
            cursor = await conn.execute(
                """
                INSERT INTO portfolio_positions (
                    token_address, symbol, chain, entry_price, quantity_token,
                    notional_usd, stop_price, take_price, highest_price,
                    dry_run, momentum_score, discovery_reasoning
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    token_address,
                    _normalize_symbol(symbol),
                    chain.lower(),
                    entry_price,
                    quantity_token,
                    notional_usd,
                    stop_price,
                    take_price,
                    entry_price,  # highest_price starts at entry
                    int(dry_run),
                    momentum_score,
                    discovery_reasoning,
                ),
            )
            row = await cursor.fetchone()
            await conn.commit()
            return self._row_to_portfolio_position(row)

    async def close_portfolio_position(
        self,
        position_id: int,
        exit_price: float,
        close_reason: str,
        realized_pnl_usd: float,
        closed_at: Optional[datetime] = None,
    ) -> bool:
        """Close an open portfolio position."""
        conn = await self._ensure_connected()
        closed_at = closed_at or datetime.now(timezone.utc)
        async with self._lock:
            cursor = await conn.execute(
                """
                UPDATE portfolio_positions
                SET status = 'closed',
                    closed_at = ?,
                    exit_price = ?,
                    realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?,
                    close_reason = ?
                WHERE id = ? AND status = 'open'
                """,
                (closed_at, exit_price, realized_pnl_usd, close_reason, position_id),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def list_open_portfolio_positions(
        self,
        chain: Optional[str] = None,
    ) -> List[PortfolioPosition]:
        """List all open portfolio positions."""
        conn = await self._ensure_connected()
        if chain:
            cursor = await conn.execute(
                """
                SELECT * FROM portfolio_positions
                WHERE status = 'open' AND chain = ?
                ORDER BY opened_at ASC
                """,
                (chain.lower(),),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT * FROM portfolio_positions
                WHERE status = 'open'
                ORDER BY opened_at ASC
                """
            )
        rows = await cursor.fetchall()
        return [self._row_to_portfolio_position(row) for row in rows]

    async def list_closed_portfolio_positions(
        self,
        limit: int = 20,
        chain: Optional[str] = None,
    ) -> List[PortfolioPosition]:
        """List recently closed portfolio positions."""
        conn = await self._ensure_connected()
        if chain:
            cursor = await conn.execute(
                """
                SELECT * FROM portfolio_positions
                WHERE status = 'closed' AND chain = ?
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (chain.lower(), limit),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT * FROM portfolio_positions
                WHERE status = 'closed'
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        rows = await cursor.fetchall()
        return [self._row_to_portfolio_position(row) for row in rows]

    async def get_open_portfolio_position(
        self, token_address: str, chain: str
    ) -> Optional[PortfolioPosition]:
        """Get an open portfolio position for a specific token."""
        conn = await self._ensure_connected()
        cursor = await conn.execute(
            """
            SELECT * FROM portfolio_positions
            WHERE LOWER(token_address) = LOWER(?) AND chain = ? AND status = 'open'
            LIMIT 1
            """,
            (token_address, chain.lower()),
        )
        row = await cursor.fetchone()
        return self._row_to_portfolio_position(row) if row else None

    async def count_open_portfolio_positions(self, chain: str) -> int:
        """Count open portfolio positions for a given chain."""
        conn = await self._ensure_connected()
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM portfolio_positions
            WHERE status = 'open' AND chain = ?
            """,
            (chain.lower(),),
        )
        row = await cursor.fetchone()
        return int(row["cnt"]) if row else 0

    async def update_portfolio_trailing_stop(
        self,
        position_id: int,
        new_stop_price: float,
        new_highest_price: float,
    ) -> bool:
        """Update trailing stop and highest price for a portfolio position."""
        conn = await self._ensure_connected()
        async with self._lock:
            cursor = await conn.execute(
                """
                UPDATE portfolio_positions
                SET stop_price = ?, highest_price = ?
                WHERE id = ? AND status = 'open'
                """,
                (new_stop_price, new_highest_price, position_id),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def reduce_portfolio_position(
        self,
        position_id: int,
        new_quantity: float,
        new_notional: float,
        new_stop_price: float,
        new_highest_price: float,
        new_take_price: Optional[float] = None,
        partial_pnl_usd: float = 0.0,
    ) -> bool:
        """Reduce an open position after a partial sell.

        Updates quantity, notional, resets trailing stop (and optionally
        take_price), and accumulates realized PnL while keeping the position
        open so exit checks continue on the remainder.
        """
        conn = await self._ensure_connected()
        async with self._lock:
            if new_take_price is not None:
                cursor = await conn.execute(
                    """
                    UPDATE portfolio_positions
                    SET quantity_token = ?,
                        notional_usd = ?,
                        stop_price = ?,
                        highest_price = ?,
                        take_price = ?,
                        realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?
                    WHERE id = ? AND status = 'open'
                    """,
                    (
                        new_quantity,
                        new_notional,
                        new_stop_price,
                        new_highest_price,
                        new_take_price,
                        partial_pnl_usd,
                        position_id,
                    ),
                )
            else:
                cursor = await conn.execute(
                    """
                    UPDATE portfolio_positions
                    SET quantity_token = ?,
                        notional_usd = ?,
                        stop_price = ?,
                        highest_price = ?,
                        realized_pnl_usd = COALESCE(realized_pnl_usd, 0) + ?
                    WHERE id = ? AND status = 'open'
                    """,
                    (
                        new_quantity,
                        new_notional,
                        new_stop_price,
                        new_highest_price,
                        partial_pnl_usd,
                        position_id,
                    ),
                )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_daily_portfolio_pnl(self, day: Optional[datetime] = None) -> float:
        """Get total realized PnL for the UTC calendar day."""
        conn = await self._ensure_connected()
        day = day or datetime.now(timezone.utc)
        day_str = day.strftime("%Y-%m-%d")
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(realized_pnl_usd), 0) AS pnl
            FROM portfolio_positions
            WHERE status = 'closed' AND DATE(closed_at) = DATE(?)
            """,
            (day_str,),
        )
        row = await cursor.fetchone()
        return float(row["pnl"]) if row and row["pnl"] is not None else 0.0

    async def delete_closed_portfolio_data(self) -> int:
        """Delete all closed positions and their associated executions.

        Returns the number of closed positions deleted.
        """
        conn = await self._ensure_connected()
        async with self._lock:
            # Delete executions linked to closed positions first
            await conn.execute(
                """
                DELETE FROM portfolio_executions
                WHERE position_id IN (
                    SELECT id FROM portfolio_positions WHERE status = 'closed'
                )
                """
            )
            cursor = await conn.execute(
                "DELETE FROM portfolio_positions WHERE status = 'closed'"
            )
            await conn.commit()
            return cursor.rowcount

    async def record_portfolio_execution(
        self,
        position_id: Optional[int],
        token_address: str,
        symbol: str,
        chain: str,
        action: str,
        requested_notional_usd: Optional[float],
        executed_price: Optional[float],
        quantity_token: Optional[float],
        tx_hash: Optional[str],
        success: bool,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a portfolio execution attempt."""
        conn = await self._ensure_connected()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO portfolio_executions (
                    position_id, token_address, symbol, chain, action,
                    requested_notional_usd, executed_price, quantity_token,
                    tx_hash, success, error, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    token_address,
                    _normalize_symbol(symbol),
                    chain.lower(),
                    action.lower(),
                    requested_notional_usd,
                    executed_price,
                    quantity_token,
                    tx_hash,
                    int(success),
                    error,
                    json.dumps(metadata or {}, default=str),
                ),
            )
            await conn.commit()

    async def get_last_portfolio_entry_time(
        self, token_address: str, chain: str
    ) -> Optional[datetime]:
        """Get the most recent portfolio entry time for a token."""
        conn = await self._ensure_connected()
        cursor = await conn.execute(
            """
            SELECT MAX(opened_at) AS last_opened
            FROM portfolio_positions
            WHERE LOWER(token_address) = LOWER(?) AND chain = ?
            """,
            (token_address, chain.lower()),
        )
        row = await cursor.fetchone()
        if row and row["last_opened"]:
            return self._parse_dt(row["last_opened"])
        return None

    # --- Token Skip Phases Operations ---

    async def increment_negative_sl_count(
        self, token_address: str, chain: str
    ) -> int:
        """Increment negative stop loss count for a token.
        
        Returns the updated negative_sl_count. When count reaches 2, sets skip_phases=1.
        """
        conn = await self._ensure_connected()
        now = datetime.now(timezone.utc)
        async with self._lock:
            # First, insert or get current count
            cursor = await conn.execute(
                """
                INSERT INTO token_skip_phases (token_address, chain, negative_sl_count, last_negative_sl_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(token_address, chain) DO UPDATE SET
                    negative_sl_count = negative_sl_count + 1,
                    last_negative_sl_at = ?,
                    updated_at = ?
                RETURNING negative_sl_count
                """,
                (token_address.lower(), chain.lower(), now, now, now, now),
            )
            row = await cursor.fetchone()
            count = int(row["negative_sl_count"]) if row else 1
            
            # If count reaches 2, set skip_phases = 1 (only if not already skipping)
            if count >= 2:
                await conn.execute(
                    """
                    UPDATE token_skip_phases
                    SET skip_phases = 1, updated_at = ?
                    WHERE token_address = ? AND chain = ? AND skip_phases = 0
                    """,
                    (now, token_address.lower(), chain.lower()),
                )
            
            await conn.commit()
            return count

    async def get_skip_phases(self, token_address: str, chain: str) -> int:
        """Get the current skip_phases value for a token."""
        conn = await self._ensure_connected()
        cursor = await conn.execute(
            """
            SELECT skip_phases FROM token_skip_phases
            WHERE token_address = ? AND chain = ?
            """,
            (token_address.lower(), chain.lower()),
        )
        row = await cursor.fetchone()
        return int(row["skip_phases"]) if row else 0

    async def decrement_all_skip_phases(self, chain: str) -> int:
        """Decrement skip_phases for all tokens in the chain.
        
        When skip_phases reaches 0, reset negative_sl_count to allow fresh attempts.
        Returns the number of tokens updated.
        """
        conn = await self._ensure_connected()
        now = datetime.now(timezone.utc)
        async with self._lock:
            # Decrement skip_phases where > 0, and reset negative_sl_count only
            # for tokens whose skip_phases transitions from 1→0.
            cursor = await conn.execute(
                """
                UPDATE token_skip_phases
                SET skip_phases = skip_phases - 1,
                    negative_sl_count = CASE
                        WHEN skip_phases = 1 THEN 0
                        ELSE negative_sl_count
                    END,
                    last_negative_sl_at = CASE
                        WHEN skip_phases = 1 THEN NULL
                        ELSE last_negative_sl_at
                    END,
                    updated_at = ?
                WHERE chain = ? AND skip_phases > 0
                """,
                (now, chain.lower()),
            )
            updated = cursor.rowcount
            await conn.commit()
            return updated

    async def reset_token_skip_phases(self, token_address: str, chain: str) -> bool:
        """Reset skip_phases and negative_sl_count for a specific token."""
        conn = await self._ensure_connected()
        now = datetime.now(timezone.utc)
        async with self._lock:
            cursor = await conn.execute(
                """
                UPDATE token_skip_phases
                SET skip_phases = 0,
                    negative_sl_count = 0,
                    last_negative_sl_at = NULL,
                    updated_at = ?
                WHERE token_address = ? AND chain = ?
                """,
                (now, token_address.lower(), chain.lower()),
            )
            await conn.commit()
            return cursor.rowcount > 0

    # --- Discovery Decision Log Operations ---

    async def record_discovery_decision(
        self,
        cycle_id: str,
        token_address: str,
        symbol: str,
        chain: str,
        decision_label: str,
        price_usd: Optional[float] = None,
        volume_24h: Optional[float] = None,
        liquidity_usd: Optional[float] = None,
        market_cap_usd: Optional[float] = None,
        momentum_score: Optional[float] = None,
        reasoning: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a discovery pipeline decision for audit."""
        conn = await self._ensure_connected()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO discovery_decisions (
                    cycle_id, token_address, symbol, chain, decision_label,
                    price_usd, volume_24h, liquidity_usd, market_cap_usd,
                    momentum_score, reasoning, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    token_address.lower(),
                    _normalize_symbol(symbol),
                    chain.lower(),
                    decision_label.lower(),
                    price_usd,
                    volume_24h,
                    liquidity_usd,
                    market_cap_usd,
                    momentum_score,
                    reasoning,
                    json.dumps(metadata or {}, default=str),
                ),
            )
            await conn.commit()

    async def record_discovery_decisions_batch(
        self,
        decisions: List[tuple],
    ) -> None:
        """Batch-insert discovery decisions.

        Each tuple: (cycle_id, token_address, symbol, chain, decision_label,
                      price_usd, volume_24h, liquidity_usd, market_cap_usd,
                      momentum_score, reasoning, metadata_json)
        """
        if not decisions:
            return
        normalized: List[tuple] = []
        for decision in decisions:
            if len(decision) != 12:
                raise ValueError("Each discovery decision tuple must contain 12 fields")
            (
                cycle_id,
                token_address,
                symbol,
                chain,
                decision_label,
                price_usd,
                volume_24h,
                liquidity_usd,
                market_cap_usd,
                momentum_score,
                reasoning,
                metadata_json,
            ) = decision
            if isinstance(metadata_json, (dict, list)):
                metadata_json = json.dumps(metadata_json, default=str)
            elif metadata_json is None:
                metadata_json = "{}"
            normalized.append(
                (
                    str(cycle_id),
                    str(token_address).lower(),
                    _normalize_symbol(str(symbol)),
                    str(chain).lower(),
                    str(decision_label).lower(),
                    price_usd,
                    volume_24h,
                    liquidity_usd,
                    market_cap_usd,
                    momentum_score,
                    reasoning,
                    metadata_json,
                )
            )
        conn = await self._ensure_connected()
        async with self._lock:
            await conn.executemany(
                """
                INSERT INTO discovery_decisions (
                    cycle_id, token_address, symbol, chain, decision_label,
                    price_usd, volume_24h, liquidity_usd, market_cap_usd,
                    momentum_score, reasoning, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized,
            )
            await conn.commit()

    async def get_discovery_decisions(
        self,
        cycle_id: Optional[str] = None,
        token_address: Optional[str] = None,
        chain: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query discovery decisions with optional filters."""
        conn = await self._ensure_connected()
        conditions = []
        params: list = []
        if cycle_id:
            conditions.append("cycle_id = ?")
            params.append(cycle_id)
        if token_address:
            conditions.append("token_address = ?")
            params.append(token_address.lower())
        if chain:
            conditions.append("chain = ?")
            params.append(chain.lower())
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cursor = await conn.execute(
            f"""
            SELECT * FROM discovery_decisions
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # --- Shadow Position Operations ---

    async def add_shadow_position(
        self,
        token_address: str,
        symbol: str,
        chain: str,
        entry_price: float,
        notional_usd: float,
        momentum_score: Optional[float] = None,
        reasoning: Optional[str] = None,
        check_after_minutes: int = 30,
    ) -> int:
        """Record a shadow (paper) position for audit comparison."""
        conn = await self._ensure_connected()
        async with self._lock:
            cursor = await conn.execute(
                """
                INSERT INTO shadow_positions (
                    token_address, symbol, chain, entry_price, notional_usd,
                    momentum_score, reasoning, check_after_minutes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    token_address.lower(),
                    _normalize_symbol(symbol),
                    chain.lower(),
                    entry_price,
                    notional_usd,
                    momentum_score,
                    reasoning,
                    check_after_minutes,
                ),
            )
            row = await cursor.fetchone()
            await conn.commit()
            return int(row["id"])

    async def list_pending_shadow_positions(self) -> List[Dict[str, Any]]:
        """Return shadow positions due for price check."""
        conn = await self._ensure_connected()
        cursor = await conn.execute(
            """
            SELECT * FROM shadow_positions
            WHERE status = 'pending'
              AND datetime(opened_at, '+' || check_after_minutes || ' minutes')
                  <= datetime('now')
            ORDER BY opened_at ASC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def resolve_shadow_position(
        self,
        shadow_id: int,
        price_at_check: float,
        pnl_pct: float,
    ) -> bool:
        """Resolve a shadow position with the observed price."""
        conn = await self._ensure_connected()
        now = datetime.now(timezone.utc)
        async with self._lock:
            cursor = await conn.execute(
                """
                UPDATE shadow_positions
                SET status = 'checked',
                    checked_at = ?,
                    price_at_check = ?,
                    pnl_pct = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, price_at_check, pnl_pct, shadow_id),
            )
            await conn.commit()
            return cursor.rowcount > 0

    async def get_shadow_summary(self, limit: int = 50) -> Dict[str, Any]:
        """Return aggregate stats for resolved shadow positions."""
        conn = await self._ensure_connected()
        cursor = await conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS winners,
                SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) AS losers,
                AVG(pnl_pct) AS avg_pnl_pct,
                MIN(pnl_pct) AS min_pnl_pct,
                MAX(pnl_pct) AS max_pnl_pct
            FROM (
                SELECT pnl_pct
                FROM shadow_positions
                WHERE status = 'checked'
                ORDER BY checked_at DESC
                LIMIT ?
            ) AS recent_checked
            """,
            (limit,),
        )
        row = await cursor.fetchone()
        if not row or row["total"] == 0:
            return {
                "total": 0,
                "winners": 0,
                "losers": 0,
                "avg_pnl_pct": 0.0,
                "min_pnl_pct": 0.0,
                "max_pnl_pct": 0.0,
            }
        return {
            "total": row["total"],
            "winners": row["winners"] or 0,
            "losers": row["losers"] or 0,
            "avg_pnl_pct": round(row["avg_pnl_pct"] or 0.0, 2),
            "min_pnl_pct": round(row["min_pnl_pct"] or 0.0, 2),
            "max_pnl_pct": round(row["max_pnl_pct"] or 0.0, 2),
        }

    # --- Helper Methods ---

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        """Parse a datetime string ensuring timezone-awareness (UTC)."""
        if not value:
            return None
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _row_to_portfolio_position(row: aiosqlite.Row) -> PortfolioPosition:
        """Convert a database row to PortfolioPosition."""
        return PortfolioPosition(
            id=row["id"],
            token_address=row["token_address"],
            symbol=row["symbol"],
            chain=row["chain"],
            entry_price=row["entry_price"],
            quantity_token=row["quantity_token"],
            notional_usd=row["notional_usd"],
            stop_price=row["stop_price"],
            take_price=row["take_price"],
            highest_price=row["highest_price"],
            opened_at=Database._parse_dt(row["opened_at"]) or datetime.now(timezone.utc),
            closed_at=Database._parse_dt(row["closed_at"]),
            exit_price=row["exit_price"],
            realized_pnl_usd=row["realized_pnl_usd"],
            status=row["status"],
            close_reason=row["close_reason"],
            dry_run=bool(row["dry_run"]),
            momentum_score=row["momentum_score"],
            discovery_reasoning=row["discovery_reasoning"],
        )
