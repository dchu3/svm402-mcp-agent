"""Portfolio strategy engine: discover → buy → hold → exit at TP/SL."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, TYPE_CHECKING

from app.execution import TraderExecutionService
from app.portfolio_discovery import DiscoveryCandidate, PortfolioDiscovery
from app.price_cache import PriceCache
from app.database import PortfolioPosition

if TYPE_CHECKING:
    from app.mcp_client import MCPManager
    from app.database import Database

logger = logging.getLogger(__name__)

LogCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]

_ERROR_SKIP_SECONDS = 300
_NATIVE_PRICE_STALE_SECONDS = 120
_DUST_NOTIONAL_USD = 0.01
_PRICE_DEVIATION_WARN_PCT = 5.0


@dataclass
class PortfolioStrategyConfig:
    """Runtime config for portfolio strategy."""

    enabled: bool
    dry_run: bool
    chain: str
    max_positions: int
    position_size_usd: float
    take_profit_pct: float
    stop_loss_pct: float
    trailing_stop_pct: float
    max_hold_hours: int
    discovery_interval_mins: int
    price_check_seconds: int
    daily_loss_limit_usd: float
    min_volume_usd: float
    min_liquidity_usd: float
    min_market_cap_usd: float
    cooldown_seconds: int
    min_momentum_score: float
    max_slippage_bps: int
    sell_pct: float = 100.0
    quote_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    rpc_url: str = ""
    quote_method: str = ""
    execute_method: str = ""
    min_token_age_hours: float = 4.0
    max_token_age_hours: float = 0.0
    slippage_probe_enabled: bool = False
    slippage_probe_usd: float = 0.50
    slippage_probe_max_slippage_pct: float = 5.0
    sol_dump_threshold_pct: float = -5.0
    sol_trend_lookback_mins: int = 60
    insider_check_enabled: bool = True
    insider_max_concentration_pct: float = 50.0
    insider_max_creator_pct: float = 30.0
    insider_warn_concentration_pct: float = 30.0
    insider_warn_creator_pct: float = 10.0
    shadow_audit_enabled: bool = False
    shadow_check_minutes: int = 30
    decision_log_enabled: bool = False

    def __post_init__(self) -> None:
        """Validate configuration consistency."""
        self.chain = self.chain.strip().lower()
        self.rpc_url = self.rpc_url.strip()
        if self.chain == "solana" and not self.rpc_url:
            raise ValueError("rpc_url is required when chain is solana")


@dataclass
class PortfolioDiscoveryCycleResult:
    """Result of one discovery cycle."""

    timestamp: datetime
    candidates_found: int = 0
    positions_opened: List[PortfolioPosition] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class PortfolioExitCycleResult:
    """Result of one exit check cycle."""

    timestamp: datetime
    positions_checked: int = 0
    trailing_stops_updated: int = 0
    positions_closed: List[PortfolioPosition] = field(default_factory=list)
    positions_partially_sold: int = 0
    errors: List[str] = field(default_factory=list)
    summary: str = ""


class PortfolioStrategyEngine:
    """Orchestrates discovery, entry, position monitoring, and exits."""

    def __init__(
        self,
        db: "Database",
        mcp_manager: "MCPManager",
        config: PortfolioStrategyConfig,
        api_key: str,
        model_name: str = "gemini-2.5-flash",
        verbose: bool = False,
        log_callback: Optional[LogCallback] = None,
    ) -> None:
        self.db = db
        self.mcp_manager = mcp_manager
        self.config = config
        self.api_key = api_key
        self.model_name = model_name
        self.verbose = verbose
        self.log_callback = log_callback

        self.execution = TraderExecutionService(
            mcp_manager=mcp_manager,
            chain=config.chain,
            max_slippage_bps=config.max_slippage_bps,
            quote_method_override=config.quote_method,
            execute_method_override=config.execute_method,
            quote_mint=config.quote_mint,
            rpc_url=config.rpc_url,
        )

        self.discovery = PortfolioDiscovery(
            mcp_manager=mcp_manager,
            api_key=api_key,
            model_name=model_name,
            min_volume_usd=config.min_volume_usd,
            min_liquidity_usd=config.min_liquidity_usd,
            min_market_cap_usd=config.min_market_cap_usd,
            min_token_age_hours=config.min_token_age_hours,
            max_token_age_hours=config.max_token_age_hours,
            min_momentum_score=config.min_momentum_score,
            chain=config.chain,
            verbose=verbose,
            log_callback=log_callback,
            rpc_url=config.rpc_url,
            insider_check_enabled=config.insider_check_enabled,
            insider_max_concentration_pct=config.insider_max_concentration_pct,
            insider_max_creator_pct=config.insider_max_creator_pct,
            insider_warn_concentration_pct=config.insider_warn_concentration_pct,
            insider_warn_creator_pct=config.insider_warn_creator_pct,
        )

        self._native_price_usd: Optional[float] = None
        self._native_price_updated_at: Optional[datetime] = None
        self._native_price_history: Deque[Tuple[datetime, float]] = deque()
        self._ref_price_cache: Optional[PriceCache] = None
        self._skip_until: Dict[str, datetime] = {}

    def _log(self, level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        if self.verbose and self.log_callback:
            self.log_callback(level, message, data)

    def _check_sol_trend(self) -> Optional[float]:
        """Return SOL % change over the lookback window, or *None* if insufficient data."""
        if len(self._native_price_history) < 2:
            return None
        oldest_price = self._native_price_history[0][1]
        newest_price = self._native_price_history[-1][1]
        if oldest_price <= 0:
            return None
        return ((newest_price - oldest_price) / oldest_price) * 100.0

    # ------------------------------------------------------------------
    # Discovery cycle
    # ------------------------------------------------------------------

    async def run_discovery_cycle(self) -> PortfolioDiscoveryCycleResult:
        """Discover new tokens, buy, and create positions."""
        now = datetime.now(timezone.utc)
        result = PortfolioDiscoveryCycleResult(timestamp=now)

        if not self.config.enabled:
            result.summary = "Portfolio strategy disabled"
            return result

        try:
            await self._refresh_native_price()
            if self._native_price_usd is None:
                result.summary = "Skipped: native token price unavailable"
                result.errors.append("Native token price is None")
                return result

            # Check SOL trend — skip discovery if SOL is dumping
            sol_change = self._check_sol_trend()
            if sol_change is not None and sol_change < self.config.sol_dump_threshold_pct:
                result.summary = (
                    f"Skipped: SOL trend {sol_change:+.1f}% "
                    f"(threshold {self.config.sol_dump_threshold_pct}%)"
                )
                self._log("info", result.summary)
                return result

            # Check available slots
            open_count = await self.db.count_open_portfolio_positions(self.config.chain)
            available_slots = self.config.max_positions - open_count
            if available_slots <= 0:
                result.summary = f"Portfolio full ({open_count}/{self.config.max_positions})"
                return result

            # Check daily loss limit
            daily_pnl = await self.db.get_daily_portfolio_pnl(now)
            if daily_pnl <= -abs(self.config.daily_loss_limit_usd):
                result.summary = "Skipped: daily loss limit reached"
                result.errors.append(f"Daily PnL ${daily_pnl:.2f} exceeds limit")
                return result

            # Sync tunable config to discovery engine
            self.discovery.min_momentum_score = self.config.min_momentum_score
            self.discovery.min_volume_usd = self.config.min_volume_usd
            self.discovery.min_liquidity_usd = self.config.min_liquidity_usd
            self.discovery.min_token_age_hours = self.config.min_token_age_hours
            self.discovery.max_token_age_hours = self.config.max_token_age_hours

            # Discover candidates
            candidates = await self.discovery.discover(
                db=self.db,
                max_candidates=available_slots,
                decision_log_enabled=self.config.decision_log_enabled,
                shadow_audit_enabled=self.config.shadow_audit_enabled,
                shadow_check_minutes=self.config.shadow_check_minutes,
                position_size_usd=self.config.position_size_usd,
            )
            result.candidates_found = len(candidates)

            if not candidates:
                result.summary = "No suitable candidates found"
                return result

            # Execute buys for each candidate
            for candidate in candidates:
                key = candidate.token_address.lower()
                skip_expires = self._skip_until.get(key)
                if skip_expires and now < skip_expires:
                    continue
                self._skip_until.pop(key, None)

                # Skip phases check: skip token if it has skip_phases > 0
                skip_phases = await self.db.get_skip_phases(candidate.token_address, candidate.chain)
                if skip_phases > 0:
                    self._log(
                        "info",
                        f"Skipping {candidate.symbol} (skip_phases={skip_phases})",
                    )
                    continue

                # Cooldown check
                last_entry = await self.db.get_last_portfolio_entry_time(
                    candidate.token_address, candidate.chain
                )
                if last_entry and (now - last_entry).total_seconds() < self.config.cooldown_seconds:
                    continue

                try:
                    position = await self._open_position(candidate)
                    if position:
                        result.positions_opened.append(position)
                except Exception as exc:
                    err = f"{candidate.symbol}: {exc}"
                    result.errors.append(err)
                    self._skip_until[key] = now + timedelta(seconds=_ERROR_SKIP_SECONDS)
                    logger.warning("Skipping %s after error: %s", candidate.symbol, exc)

            parts = [f"found={result.candidates_found}"]
            if result.positions_opened:
                parts.append(f"opened={len(result.positions_opened)}")
            if result.errors:
                parts.append(f"errors={len(result.errors)}")
            result.summary = " | ".join(parts)
            return result

        finally:
            # Always advance skip phases once per scheduled cycle (except when disabled).
            await self.db.decrement_all_skip_phases(self.config.chain)

    async def _open_position(
        self, candidate: DiscoveryCandidate
    ) -> Optional[PortfolioPosition]:
        """Execute buy and create portfolio position."""
        notional = self.config.position_size_usd

        # Re-fetch native price if it may have gone stale during discovery
        if self._native_price_updated_at:
            age_seconds = (datetime.now(timezone.utc) - self._native_price_updated_at).total_seconds()
            if age_seconds > _NATIVE_PRICE_STALE_SECONDS:
                self._log("info", f"Native price stale ({age_seconds:.0f}s old), refreshing before buy")
                await self._refresh_native_price()

        if self.config.slippage_probe_enabled and not self.config.dry_run:
            should_abort, slippage_pct, reason = await self.execution.probe_slippage(
                token_address=candidate.token_address,
                probe_usd=self.config.slippage_probe_usd,
                input_price_usd=self._native_price_usd or 1.0,
                max_slippage_pct=self.config.slippage_probe_max_slippage_pct,
            )
            if should_abort:
                self._log(
                    "warning",
                    f"Skipping {candidate.symbol}: {reason}",
                )
                return None

        quote = await self.execution.get_quote(
            token_address=candidate.token_address,
            notional_usd=notional,
            side="buy",
            input_price_usd=self._native_price_usd,
        )

        execution = await self.execution.execute_trade(
            token_address=candidate.token_address,
            notional_usd=notional,
            side="buy",
            quantity_token=None,
            dry_run=self.config.dry_run,
            quote=quote,
            input_price_usd=self._native_price_usd,
        )

        quantity = execution.quantity_token
        if quantity is None:
            if quote.price <= 0:
                raise RuntimeError("Cannot derive quantity from quote price")
            quantity = notional / quote.price

        executed_price = execution.executed_price or quote.price

        # Log warning if execution price deviates significantly from quote
        if execution.success and quote and quote.price > 0 and executed_price > 0:
            deviation_pct = abs(executed_price - quote.price) / quote.price * 100
            if deviation_pct > _PRICE_DEVIATION_WARN_PCT:
                logger.warning(
                    "Price deviation %.1f%% on %s buy: quoted=$%.10f executed=$%.10f",
                    deviation_pct, candidate.symbol, quote.price, executed_price,
                )
                self._log(
                    "warning",
                    f"Price deviation {deviation_pct:.1f}% on {candidate.symbol} buy",
                    {
                        "deviation_pct": deviation_pct,
                        "symbol": candidate.symbol,
                        "quote_price": quote.price,
                        "executed_price": executed_price,
                        "token_address": candidate.token_address,
                    },
                )

        if not execution.success:
            await self.db.record_portfolio_execution(
                position_id=None,
                token_address=candidate.token_address,
                symbol=candidate.symbol,
                chain=candidate.chain,
                action="buy",
                requested_notional_usd=notional,
                executed_price=executed_price,
                quantity_token=quantity,
                tx_hash=execution.tx_hash,
                success=False,
                error=execution.error,
            )
            self._log("error", f"Buy failed for {candidate.symbol}: {execution.error}")
            return None

        stop_price = executed_price * (1 - self.config.stop_loss_pct / 100)
        take_price = (
            float("inf")
            if self.config.take_profit_pct == 0
            else executed_price * (1 + self.config.take_profit_pct / 100)
        )

        position = await self.db.add_portfolio_position(
            token_address=candidate.token_address,
            symbol=candidate.symbol,
            chain=candidate.chain,
            entry_price=executed_price,
            quantity_token=quantity,
            notional_usd=notional,
            stop_price=stop_price,
            take_price=take_price,
            dry_run=self.config.dry_run,
            momentum_score=candidate.momentum_score,
            discovery_reasoning=candidate.reasoning,
        )

        await self.db.record_portfolio_execution(
            position_id=position.id,
            token_address=candidate.token_address,
            symbol=candidate.symbol,
            chain=candidate.chain,
            action="buy",
            requested_notional_usd=notional,
            executed_price=executed_price,
            quantity_token=quantity,
            tx_hash=execution.tx_hash,
            success=True,
        )

        self._log(
            "info",
            f"Opened {candidate.symbol} at ${executed_price:.10f} "
            f"(TP={'disabled' if take_price == float('inf') else f'${take_price:.10f}'} SL=${stop_price:.10f})",
        )
        return position

    # ------------------------------------------------------------------
    # Exit check cycle
    # ------------------------------------------------------------------

    async def run_exit_checks(self) -> PortfolioExitCycleResult:
        """Check all open positions for TP/SL/trailing stop/timeout exits."""
        now = datetime.now(timezone.utc)
        result = PortfolioExitCycleResult(timestamp=now)

        if not self.config.enabled:
            result.summary = "Portfolio strategy disabled"
            return result

        positions = await self.db.list_open_portfolio_positions(chain=self.config.chain)
        result.positions_checked = len(positions)

        if not positions:
            result.summary = "No open positions"
            return result

        await self._refresh_native_price()

        for position in positions:
            try:
                _, trailing_updated = await self._evaluate_position(position, result, now)
                if trailing_updated:
                    result.trailing_stops_updated += 1
            except (OSError, IOError):
                raise
            except Exception as exc:
                err = f"Exit check failed for {position.symbol}: {exc}"
                result.errors.append(err)
                logger.warning(err)

        parts = [f"checked={result.positions_checked}"]
        if result.trailing_stops_updated:
            parts.append(f"trailing_updated={result.trailing_stops_updated}")
        if result.positions_closed:
            parts.append(f"closed={len(result.positions_closed)}")
        if result.positions_partially_sold:
            parts.append(f"partial_sold={result.positions_partially_sold}")
        if result.errors:
            parts.append(f"errors={len(result.errors)}")
        result.summary = " | ".join(parts)
        return result

    async def _evaluate_position(
        self,
        position: PortfolioPosition,
        cycle_result: PortfolioExitCycleResult,
        now: datetime,
    ) -> tuple[str, bool]:
        """Evaluate a position for trailing stop update or exit.

        Returns (exit_action, trailing_updated) where exit_action is one of
        "closed", "reduced", "failed", or "none".
        """
        current_price = await self._fetch_current_price(
            position.token_address, position.chain
        )

        # Update trailing stop
        trailing_updated = False
        if current_price > position.highest_price:
            new_highest = current_price
            new_trail_stop = new_highest * (1 - self.config.trailing_stop_pct / 100)
            new_stop = max(position.stop_price, new_trail_stop)

            if new_stop > position.stop_price or new_highest > position.highest_price:
                updated = await self.db.update_portfolio_trailing_stop(
                    position_id=position.id,
                    new_stop_price=new_stop,
                    new_highest_price=new_highest,
                )
                if updated:
                    position.stop_price = new_stop
                    position.highest_price = new_highest
                    trailing_updated = True
                    logger.debug(
                        "Trailing stop updated %s: stop=$%.10f highest=$%.10f",
                        position.symbol, new_stop, new_highest,
                    )

        # Check exit conditions
        close_reason = self._exit_reason(position, current_price, now)
        if close_reason is None:
            return "none", trailing_updated

        # Execute sell (may fully close or partially reduce)
        action = await self._close_position(position, current_price, close_reason, cycle_result)
        return action, trailing_updated

    def _exit_reason(
        self,
        position: PortfolioPosition,
        current_price: float,
        now: datetime,
    ) -> Optional[str]:
        """Determine if position should be closed."""
        if current_price <= position.stop_price:
            return "stop_loss"
        if current_price >= position.take_price:
            return "take_profit"
        age_hours = (now - position.opened_at).total_seconds() / 3600
        if age_hours >= self.config.max_hold_hours:
            return "max_hold_time"
        return None

    async def _close_position(
        self,
        position: PortfolioPosition,
        current_price: float,
        close_reason: str,
        cycle_result: PortfolioExitCycleResult,
    ) -> str:
        """Execute sell and close (or reduce) position.

        Returns "closed", "reduced", or "failed".
        """
        # In live mode, use wallet balance as the authoritative token count
        # so that partial-sell math (remaining qty) isn't based on a stale DB value.
        # Only hold back a percentage when the position is profitable.
        # If at a loss, always sell the full quantity.
        is_profitable = current_price > position.entry_price
        is_partial = is_profitable and self.config.sell_pct < 100.0
        effective_total = position.quantity_token
        actual_balance: Optional[float] = None
        if not self.config.dry_run:
            actual_balance = await self.execution.get_wallet_token_balance(
                position.token_address,
            )
            if actual_balance is not None and actual_balance > 0:
                # Wallet balance is the source of truth in live mode.
                effective_total = actual_balance
        sell_qty = (
            effective_total * (self.config.sell_pct / 100.0)
            if is_partial
            else effective_total
        )

        # Force full close when remaining value would be dust
        remaining_qty = effective_total - sell_qty
        if is_partial and (remaining_qty * current_price) < _DUST_NOTIONAL_USD:
            sell_qty = effective_total
            is_partial = False

        requested_notional = current_price * sell_qty

        execution = await self.execution.execute_trade(
            token_address=position.token_address,
            notional_usd=requested_notional,
            side="sell",
            quantity_token=sell_qty,
            dry_run=self.config.dry_run,
            quote=None,
            input_price_usd=self._native_price_usd,
        )

        exit_price = execution.executed_price or current_price

        if execution.success:
            realized_pnl = (exit_price - position.entry_price) * sell_qty

            if is_partial:
                ok = await self._reduce_position_after_partial_sell(
                    position, sell_qty, exit_price, realized_pnl,
                    close_reason, cycle_result,
                    requested_notional=requested_notional,
                    tx_hash=execution.tx_hash,
                    effective_total=effective_total,
                )
                return "reduced" if ok else "failed"
            else:
                ok = await self._fully_close_position(
                    position, sell_qty, exit_price, realized_pnl,
                    close_reason, cycle_result,
                    requested_notional=requested_notional,
                    tx_hash=execution.tx_hash,
                )
                return "closed" if ok else "failed"
        else:
            err = f"Sell failed for {position.symbol}: {execution.error}"
            cycle_result.errors.append(err)
            await self.db.record_portfolio_execution(
                position_id=position.id,
                token_address=position.token_address,
                symbol=position.symbol,
                chain=position.chain,
                action="sell",
                requested_notional_usd=requested_notional,
                executed_price=exit_price,
                quantity_token=sell_qty,
                tx_hash=execution.tx_hash,
                success=False,
                error=execution.error,
            )
            return "failed"

    async def _reduce_position_after_partial_sell(
        self,
        position: PortfolioPosition,
        sell_qty: float,
        exit_price: float,
        realized_pnl: float,
        close_reason: str,
        cycle_result: PortfolioExitCycleResult,
        *,
        requested_notional: float,
        tx_hash: Optional[str] = None,
        effective_total: Optional[float] = None,
    ) -> bool:
        """Reduce position after profitable partial sell, keeping it open.

        Returns True if the DB update succeeded.
        """
        base_qty = effective_total if effective_total is not None else position.quantity_token
        new_qty = base_qty - sell_qty
        new_notional = new_qty * position.entry_price
        # Fresh trailing stop from exit price — don't keep old stop which may
        # be above current market and trigger an immediate re-exit.
        new_stop = exit_price * (1 - self.config.trailing_stop_pct / 100)

        # Bump take_price so we don't immediately re-trigger TP next cycle
        if self.config.take_profit_pct == 0:
            new_take_price = float("inf")
        else:
            new_take_price = exit_price * (1 + self.config.take_profit_pct / 100)
            new_take_price = max(position.take_price, new_take_price)

        reduced = await self.db.reduce_portfolio_position(
            position_id=position.id,
            new_quantity=new_qty,
            new_notional=new_notional,
            new_stop_price=new_stop,
            new_highest_price=exit_price,
            new_take_price=new_take_price,
            partial_pnl_usd=realized_pnl,
        )
        await self.db.record_portfolio_execution(
            position_id=position.id,
            token_address=position.token_address,
            symbol=position.symbol,
            chain=position.chain,
            action="sell",
            requested_notional_usd=requested_notional,
            executed_price=exit_price,
            quantity_token=sell_qty,
            tx_hash=tx_hash,
            success=reduced,
            error=None if reduced else "Position reduce update failed",
        )
        if reduced:
            # Accumulate realized PnL in-memory so the final close records the total.
            position.realized_pnl_usd = (position.realized_pnl_usd or 0.0) + realized_pnl
            cycle_result.positions_partially_sold += 1
            self._log(
                "info",
                f"Partial sell {position.symbol} ({close_reason}) "
                f"sold={sell_qty:.4f} remaining={new_qty:.4f} "
                f"PnL=${realized_pnl:.4f}",
            )
        else:
            cycle_result.errors.append(
                f"Failed to reduce position {position.id} "
                f"for {position.symbol}: DB update returned reduced=False."
            )
        return reduced

    async def _fully_close_position(
        self,
        position: PortfolioPosition,
        sell_qty: float,
        exit_price: float,
        realized_pnl: float,
        close_reason: str,
        cycle_result: PortfolioExitCycleResult,
        *,
        requested_notional: float,
        tx_hash: Optional[str] = None,
    ) -> bool:
        """Fully close a position.

        Returns True if the DB update succeeded.
        """
        closed = await self.db.close_portfolio_position(
            position_id=position.id,
            exit_price=exit_price,
            close_reason=close_reason,
            realized_pnl_usd=realized_pnl,
        )
        await self.db.record_portfolio_execution(
            position_id=position.id,
            token_address=position.token_address,
            symbol=position.symbol,
            chain=position.chain,
            action="sell",
            requested_notional_usd=requested_notional,
            executed_price=exit_price,
            quantity_token=sell_qty,
            tx_hash=tx_hash,
            success=closed,
            error=None if closed else "Position close update failed",
        )
        if closed:
            position.exit_price = exit_price
            # Accumulate: add this sell's PnL to any already-realized from partial sells.
            position.realized_pnl_usd = (position.realized_pnl_usd or 0.0) + realized_pnl
            position.close_reason = close_reason
            cycle_result.positions_closed.append(position)
            self._log(
                "info",
                f"Closed {position.symbol} ({close_reason}) "
                f"PnL=${position.realized_pnl_usd:.4f}",
            )

            cumulative_pnl = position.realized_pnl_usd or 0.0

            # Warn when take_profit fires but cumulative PnL is negative.
            if close_reason == "take_profit" and cumulative_pnl < 0:
                logger.warning(
                    "take_profit triggered for %s but cumulative realized PnL is negative "
                    "(entry=$%.10f exit=$%.10f cumulative_pnl=$%.4f) — likely sell-side slippage",
                    position.symbol, position.entry_price, exit_price, cumulative_pnl,
                )

            # Track negative stop losses for skip phases based on cumulative PnL.
            if close_reason == "stop_loss" and cumulative_pnl < 0:
                count = await self.db.increment_negative_sl_count(
                    position.token_address, position.chain
                )
                if count >= 2:
                    self._log(
                        "info",
                        f"{position.symbol} hit 2 negative stop losses - skipping next discovery cycle",
                    )
        else:
            cycle_result.errors.append(
                f"Failed to close position {position.id} ({position.symbol}) "
                "in database after trade execution."
            )
        return closed

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    async def _fetch_current_price(self, token_address: str, chain: str) -> float:
        """Fetch current reference price from DexScreener (cached)."""
        if self._ref_price_cache is None:
            self._ref_price_cache = PriceCache(ttl_seconds=15)

        cached = await self._ref_price_cache.get(chain, token_address)
        if cached is not None:
            price, _ = self._parse_reference_result(cached)
            return price

        dexscreener = self.mcp_manager.get_client("dexscreener")
        if dexscreener is None:
            raise RuntimeError("DexScreener MCP client is not configured")

        result = await dexscreener.call_tool(
            "get_token_pools",
            {"chainId": chain, "tokenAddress": token_address},
        )
        await self._ref_price_cache.set(chain, token_address, result)
        price, _ = self._parse_reference_result(result)
        return price

    async def _refresh_native_price(self) -> None:
        """Fetch current native token (SOL) price in USD."""
        if self._native_price_updated_at:
            age = (datetime.now(timezone.utc) - self._native_price_updated_at).total_seconds()
            if age < _NATIVE_PRICE_STALE_SECONDS:
                return

        from app.execution import SOL_NATIVE_MINT

        dexscreener = self.mcp_manager.get_client("dexscreener")
        if dexscreener is None:
            return

        try:
            result = await dexscreener.call_tool(
                "get_token_pools",
                {"chainId": self.config.chain, "tokenAddress": SOL_NATIVE_MINT},
            )
            price, _ = self._parse_reference_result(result)
            if price and price > 0:
                self._native_price_usd = price
                self._native_price_updated_at = datetime.now(timezone.utc)
                self._native_price_history.append(
                    (self._native_price_updated_at, price)
                )
                # Prune entries older than the lookback window
                cutoff = self._native_price_updated_at - timedelta(
                    minutes=self.config.sol_trend_lookback_mins
                )
                while (
                    self._native_price_history
                    and self._native_price_history[0][0] < cutoff
                ):
                    self._native_price_history.popleft()
        except Exception as exc:
            logger.warning("Failed to fetch native token price: %s", exc)

    @staticmethod
    def _parse_reference_result(result: Any) -> tuple[float, Optional[float]]:
        """Parse DexScreener response for price and liquidity."""
        pairs: List[Dict[str, Any]] = []
        if isinstance(result, list):
            pairs = [p for p in result if isinstance(p, dict)]
        elif isinstance(result, dict):
            raw = result.get("pairs", [])
            if isinstance(raw, list):
                pairs = [p for p in raw if isinstance(p, dict)]

        if not pairs:
            raise RuntimeError("DexScreener returned no pairs")

        def _safe_liquidity_usd(pair: Dict[str, Any]) -> float:
            liquidity = pair.get("liquidity")
            if not isinstance(liquidity, dict):
                return 0.0
            liq_val = liquidity.get("usd")
            if liq_val is None:
                return 0.0
            try:
                return float(liq_val)
            except (TypeError, ValueError):
                return 0.0

        most_liquid_pair = max(pairs, key=_safe_liquidity_usd)
        price_value = most_liquid_pair.get("priceUsd")
        if price_value is None:
            raise RuntimeError("DexScreener pair missing priceUsd")

        price = float(price_value)

        liquidity_usd: Optional[float] = None
        liquidity = most_liquid_pair.get("liquidity")
        if isinstance(liquidity, dict):
            liq_val = liquidity.get("usd")
            if liq_val is not None:
                try:
                    liquidity_usd = float(liq_val)
                except (TypeError, ValueError):
                    pass

        return price, liquidity_usd

    # ------------------------------------------------------------------
    # Shadow audit checks
    # ------------------------------------------------------------------

    async def check_shadow_positions(self) -> int:
        """Resolve pending shadow positions by fetching current prices.

        Returns the number of shadow positions resolved.
        """
        pending = await self.db.list_pending_shadow_positions()
        if not pending:
            return 0

        resolved = 0
        for shadow in pending:
            token_address = shadow["token_address"]
            entry_price = shadow["entry_price"]
            try:
                current_price = await self._fetch_current_price(
                    token_address=token_address,
                    chain=shadow["chain"],
                )
                pnl_pct = ((current_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
                updated = await self.db.resolve_shadow_position(
                    shadow_id=shadow["id"],
                    price_at_check=current_price,
                    pnl_pct=pnl_pct,
                )
                if updated:
                    self._log(
                        "info",
                        f"Shadow {shadow['symbol']}: entry=${entry_price:.10f} "
                        f"now=${current_price:.10f} pnl={pnl_pct:+.1f}%",
                    )
                    resolved += 1
            except Exception as exc:
                self._log("warning", f"Shadow check failed for {shadow.get('symbol', '?')}: {exc}")

        return resolved
