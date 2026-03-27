"""Background scheduler for portfolio strategy (discovery + exit checks)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from app.formatting import format_price

if TYPE_CHECKING:
    from app.portfolio_strategy import (
        PortfolioDiscoveryCycleResult,
        PortfolioExitCycleResult,
        PortfolioStrategyEngine,
    )
    from app.telegram_notifier import TelegramNotifier

LogCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]


class PortfolioScheduler:
    """Runs portfolio discovery and exit check loops on separate intervals."""

    def __init__(
        self,
        engine: "PortfolioStrategyEngine",
        discovery_interval_seconds: int,
        exit_check_interval_seconds: int,
        telegram: Optional["TelegramNotifier"] = None,
        verbose: bool = False,
        log_callback: Optional[LogCallback] = None,
    ) -> None:
        self.engine = engine
        self.discovery_interval = discovery_interval_seconds
        self._exit_check_interval_fallback = exit_check_interval_seconds
        self.telegram = telegram
        self.verbose = verbose
        self.log_callback = log_callback

        self._discovery_task: Optional[asyncio.Task[None]] = None
        self._exit_task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._discovery_cycle_count = 0
        self._exit_cycle_count = 0
        self._last_discovery: Optional[datetime] = None
        self._last_exit_check: Optional[datetime] = None

    def _log(self, level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        if self.verbose and self.log_callback:
            self.log_callback(level, message, data)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def exit_check_interval(self) -> int:
        """Live exit-check interval in seconds.

        Always reads from ``engine.config.price_check_seconds`` so that
        runtime updates via ``/portfolio set`` are immediately visible in
        logs, status reports, and the sleep between exit checks.
        Falls back to the constructor argument when the engine config does
        not expose that attribute (e.g. in tests with minimal mocks).
        """
        try:
            return self.engine.config.price_check_seconds
        except AttributeError:
            return self._exit_check_interval_fallback

    async def start(self) -> None:
        """Start both discovery and exit check loops."""
        if self._running:
            return
        self._running = True
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        self._exit_task = asyncio.create_task(self._exit_loop())
        self._log(
            "info",
            f"Portfolio scheduler started "
            f"(discovery={self.discovery_interval}s, exit_check={self.exit_check_interval}s)",
        )

    async def stop(self) -> None:
        """Stop both loops."""
        self._running = False
        tasks = []
        if self._discovery_task:
            self._discovery_task.cancel()
            tasks.append(self._discovery_task)
        if self._exit_task:
            self._exit_task.cancel()
            tasks.append(self._exit_task)

        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._discovery_task = None
        self._exit_task = None
        self._log("info", "Portfolio scheduler stopped")

    async def run_discovery_now(self) -> "PortfolioDiscoveryCycleResult":
        """Trigger one discovery cycle immediately."""
        return await self._run_discovery()

    async def run_exit_check_now(self) -> "PortfolioExitCycleResult":
        """Trigger one exit check cycle immediately."""
        return await self._run_exit_check()

    # ------------------------------------------------------------------
    # Internal loops
    # ------------------------------------------------------------------

    async def _discovery_loop(self) -> None:
        while self._running:
            try:
                await self._run_discovery()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log("error", f"Portfolio discovery cycle failed: {exc}")

            try:
                await asyncio.sleep(self.discovery_interval)
            except asyncio.CancelledError:
                break

    async def _exit_loop(self) -> None:
        while self._running:
            try:
                await self._run_exit_check()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log("error", f"Portfolio exit check failed: {exc}")

            try:
                await asyncio.sleep(self.exit_check_interval)
            except asyncio.CancelledError:
                break

    async def _run_discovery(self) -> "PortfolioDiscoveryCycleResult":
        self._discovery_cycle_count += 1
        self._last_discovery = datetime.now(timezone.utc)
        self._log("info", f"Portfolio discovery #{self._discovery_cycle_count}")

        result = await self.engine.run_discovery_cycle()

        if self.telegram and self.telegram.is_configured:
            if result.positions_opened or result.errors:
                await self._send_discovery_notification(result)

        self._log("info", f"Portfolio discovery #{self._discovery_cycle_count}: {result.summary}")
        return result

    async def _run_exit_check(self) -> "PortfolioExitCycleResult":
        self._exit_cycle_count += 1
        self._last_exit_check = datetime.now(timezone.utc)

        result = await self.engine.run_exit_checks()

        # Resolve pending shadow positions alongside exit checks
        try:
            shadow_resolved = await self.engine.check_shadow_positions()
            if shadow_resolved:
                self._log("info", f"Resolved {shadow_resolved} shadow position(s)")
        except Exception as exc:
            self._log("warning", f"Shadow position check failed: {exc}")

        if self.telegram and self.telegram.is_configured:
            if result.positions_closed or result.errors:
                await self._send_exit_notification(result)

        if result.positions_closed or result.errors:
            self._log("info", f"Portfolio exit check: {result.summary}")
        return result

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def _send_discovery_notification(self, result: "PortfolioDiscoveryCycleResult") -> None:
        if not self.telegram:
            return
        lines = [
            "📈 <b>Portfolio Discovery</b>",
            f"⏰ {result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}",
            f"🔍 Candidates found: {result.candidates_found}",
            "",
        ]

        if result.positions_opened:
            lines.append("🟢 <b>New Positions</b>")
            for pos in result.positions_opened:
                reasoning = pos.discovery_reasoning or ""
                lines.append(f"• {pos.symbol}: entry {format_price(pos.entry_price)}")
                lines.append(f"  📋 <code>{pos.token_address}</code>")
                if reasoning:
                    lines.append(f"  💬 {reasoning}")
            lines.append("")

        if result.errors:
            lines.append(f"⚠️ {len(result.errors)} error(s)")

        try:
            await self.telegram.send_message("\n".join(lines))
        except Exception as exc:
            self._log("error", f"Failed to send discovery notification: {exc}")

    async def _send_exit_notification(self, result: "PortfolioExitCycleResult") -> None:
        if not self.telegram:
            return
        lines = [
            "📉 <b>Portfolio Exit Check</b>",
            f"⏰ {result.timestamp.strftime('%Y-%m-%d %H:%M UTC')}",
            "",
        ]

        if result.positions_closed:
            lines.append("🔴 <b>Closed Positions</b>")
            for pos in result.positions_closed:
                pnl = pos.realized_pnl_usd if pos.realized_pnl_usd is not None else 0.0
                pct = (pnl / pos.notional_usd * 100) if pos.notional_usd else 0.0
                reason = pos.close_reason or "unknown"
                entry_p = format_price(pos.entry_price)
                exit_p = format_price(pos.exit_price)
                if pnl != 0 and abs(pnl) < 0.005:
                    pnl_str = f"${pnl:.3f}"
                else:
                    pnl_str = f"${pnl:,.2f}"
                lines.append(
                    f"• {pos.symbol}: {entry_p} → {exit_p} "
                    f"PnL {pnl_str} ({pct:+.1f}%) [{reason}]"
                )
            lines.append("")

        if result.errors:
            lines.append(f"⚠️ {len(result.errors)} error(s)")

        try:
            await self.telegram.send_message("\n".join(lines))
        except Exception as exc:
            self._log("error", f"Failed to send exit notification: {exc}")

    def get_status(self) -> Dict[str, Any]:
        """Return current scheduler status."""
        return {
            "running": self.is_running,
            "discovery_interval_seconds": self.discovery_interval,
            "exit_check_interval_seconds": self.exit_check_interval,
            "discovery_cycles": self._discovery_cycle_count,
            "exit_check_cycles": self._exit_cycle_count,
            "last_discovery": self._last_discovery.isoformat() if self._last_discovery else None,
            "last_exit_check": self._last_exit_check.isoformat() if self._last_exit_check else None,
        }
