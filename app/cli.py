"""CLI interface for DEX Agentic Bot."""

from __future__ import annotations

import argparse
import asyncio
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import load_settings
from app.database import Database
from app.mcp_client import MCPManager
from app.output import CLIOutput, OutputFormat
from app.telegram_notifier import TelegramNotifier
from app.token_analyzer import TokenAnalyzer
from app.types import PlannerResult
from app.portfolio_scheduler import PortfolioScheduler
from app.portfolio_strategy import PortfolioStrategyConfig, PortfolioStrategyEngine

# Known blockchain chain identifiers for parsing multi-word token names
KNOWN_CHAINS = frozenset({
    "solana", "sol",
})


def _parse_token_and_chain(args: List[str]) -> Tuple[str, Optional[str]]:
    """Parse args into token name and optional chain.
    
    If the last arg is a known chain, treat it as the chain and join
    the rest as the token name. Otherwise, join all args as the token.
    
    Examples:
        ["OLIVE", "OIL"] -> ("OLIVE OIL", None)
        ["OLIVE", "OIL", "solana"] -> ("OLIVE OIL", "solana")
        ["PEPE", "solana"] -> ("PEPE", "solana")
        ["PEPE"] -> ("PEPE", None)
    """
    if not args:
        return ("", None)
    
    if len(args) == 1:
        return (args[0], None)
    
    last_arg = args[-1].lower()
    if last_arg in KNOWN_CHAINS:
        token = " ".join(args[:-1])
        return (token, last_arg)
    else:
        token = " ".join(args)
        return (token, None)


async def run_telegram_only(
    telegram: TelegramNotifier,
    output: CLIOutput,
) -> None:
    """Run only the Telegram bot without CLI interaction."""
    output.info("🤖 Starting Telegram bot in standalone mode...")
    output.info("Send a token address to the bot to get an analysis report.")
    output.info("Press Ctrl+C to stop.\n")
    
    # Start Telegram polling
    await telegram.start_polling()
    
    try:
        # Keep running until interrupted
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await telegram.stop_polling()
        await telegram.close()


async def run_single_query(
    planner: Any,
    query: str,
    output: CLIOutput,
    context: Dict[str, Any],
) -> None:
    """Execute a single query and display the result."""
    try:
        with output.processing("Processing query..."):
            result = await planner.run(query, context)
        output.result(result)
    except Exception as exc:
        output.error(f"Query failed: {exc}")
        raise


async def run_interactive(
    planner: Any,
    output: CLIOutput,
    db: Database,
    mcp_manager: MCPManager,
    telegram: Optional[TelegramNotifier] = None,
    portfolio_scheduler: Optional[PortfolioScheduler] = None,
) -> None:
    """Run interactive REPL session."""
    output.info("DEX Agentic Bot - Interactive Mode")
    output.info("Type your queries, or use /help for commands")
    output.info("-" * 50)

    context: Dict[str, Any] = {}
    conversation_history: List[Dict[str, str]] = []
    recent_tokens: List[Dict[str, str]] = []

    # Start Telegram polling if enabled
    if telegram and telegram.is_configured:
        await telegram.start_polling()
        output.info("📱 Telegram notifications enabled (send /help to bot)")

    # Start portfolio strategy scheduler if enabled
    if portfolio_scheduler:
        await portfolio_scheduler.start()
        output.info(
            f"📈 Portfolio scheduler started "
            f"(discovery={portfolio_scheduler.discovery_interval}s, "
            f"exit_check={portfolio_scheduler.exit_check_interval}s)"
        )

    try:
        while True:
            try:
                loop = asyncio.get_running_loop()
                query = (await loop.run_in_executor(None, input, "\n> ")).strip()
            except (EOFError, KeyboardInterrupt):
                output.info("\nGoodbye!")
                break

            if not query:
                continue

            # Handle commands
            if query.startswith("/"):
                handled = await _handle_command(
                    query, output, db, mcp_manager,
                    conversation_history, recent_tokens,
                    portfolio_scheduler
                )
                if handled == "quit":
                    break
                if handled:
                    continue

            # Build context
            context = {
                "conversation_history": conversation_history,
                "recent_tokens": recent_tokens,
            }

            try:
                with output.processing("Thinking..."):
                    result = await planner.run(query, context)
                output.result(result)

                # Update conversation history
                conversation_history.append({"role": "user", "content": query})
                conversation_history.append({"role": "assistant", "content": result.message})

                # Keep history bounded
                if len(conversation_history) > 20:
                    conversation_history = conversation_history[-20:]

                # Update token context
                if result.tokens:
                    recent_tokens = result.tokens[:10]

            except Exception as exc:
                output.error(f"Error: {exc}")

    finally:
        if portfolio_scheduler:
            await portfolio_scheduler.stop()
        if telegram:
            await telegram.close()


async def _handle_command(
    query: str,
    output: CLIOutput,
    db: Database,
    mcp_manager: MCPManager,
    conversation_history: List[Dict[str, str]],
    recent_tokens: List[Dict[str, str]],
    portfolio_scheduler: Optional[PortfolioScheduler] = None,
) -> Optional[str]:
    """Handle slash commands. Returns 'quit' to exit, True if handled, None otherwise."""
    try:
        parts = shlex.split(query)
    except ValueError as e:
        output.error(f"Invalid command syntax (check quotes): {e}")
        return True
    cmd = parts[0].lower()

    # Exit commands
    if cmd in ("/quit", "/exit", "/q"):
        output.info("Goodbye!")
        return "quit"

    # Clear context
    if cmd in ("/clear", "/reset"):
        conversation_history.clear()
        recent_tokens.clear()
        output.info("Context cleared.")
        return True

    # Show context
    if cmd in ("/context", "/ctx"):
        if recent_tokens:
            output.info("Recent tokens in context:")
            for t in recent_tokens:
                symbol = t.get("symbol", "?")
                addr = t.get("address", "?")
                addr_short = addr[:10] + "..." if len(addr) > 10 else addr
                chain = t.get("chainId", "unknown")
                output.info(f"  • {symbol} ({addr_short}) on {chain}")
        else:
            output.info("No tokens in context")
        output.info(f"\nConversation history: {len(conversation_history)} messages")
        return True

    # Help
    if cmd in ("/help", "/h"):
        output.help_panel()
        return True

    # Portfolio strategy commands
    if cmd == "/portfolio":
        await _cmd_portfolio(parts[1:], output, db, portfolio_scheduler)
        return True

    output.warning(f"Unknown command: {query}. Use /help for available commands.")
    return True


async def _search_token(
    symbol: str,
    chain: Optional[str],
    mcp_manager: MCPManager,
) -> Optional[Dict[str, str]]:
    """Search for a token using MCP tools and return its info."""
    # Try DexScreener first
    dexscreener = mcp_manager.get_client("dexscreener")
    if dexscreener:
        try:
            # Search for the token
            query = f"{symbol} {chain}" if chain else symbol
            result = await dexscreener.call_tool("search_pairs", {"query": query})
            
            if isinstance(result, dict) and result.get("pairs"):
                pairs = result["pairs"]
                
                # Filter by chain if specified
                for pair in pairs:
                    base_token = pair.get("baseToken", {})
                    pair_chain = pair.get("chainId", "").lower()
                    
                    if base_token.get("symbol", "").upper() == symbol.upper():
                        if chain is None or pair_chain == chain.lower():
                            return {
                                "address": base_token.get("address", ""),
                                "symbol": base_token.get("symbol", symbol),
                                "chain": pair_chain,
                            }
                
                # If exact match not found, return first result
                if pairs:
                    first_pair = pairs[0]
                    base_token = first_pair.get("baseToken", {})
                    return {
                        "address": base_token.get("address", ""),
                        "symbol": base_token.get("symbol", symbol),
                        "chain": first_pair.get("chainId", chain or "unknown"),
                    }
        except Exception:
            pass

    # Fallback to DexPaprika search
    dexpaprika = mcp_manager.get_client("dexpaprika")
    if dexpaprika:
        try:
            result = await dexpaprika.call_tool("search", {"query": symbol})
            
            if isinstance(result, dict):
                tokens = result.get("tokens", [])
                for token in tokens:
                    token_chain = token.get("network", "").lower()
                    if chain is None or token_chain == chain.lower():
                        return {
                            "address": token.get("address", ""),
                            "symbol": token.get("symbol", symbol),
                            "chain": token_chain,
                        }
        except Exception:
            pass

    return None


async def _cmd_portfolio(
    args: List[str],
    output: CLIOutput,
    db: Database,
    scheduler: Optional[PortfolioScheduler],
) -> None:
    """Handle /portfolio command for portfolio strategy operations."""
    if not args:
        output.info("Portfolio Strategy Commands:")
        output.info("  /portfolio status     - Show portfolio strategy status")
        output.info("  /portfolio run        - Run one discovery cycle now")
        output.info("  /portfolio check      - Run one exit check cycle now")
        output.info("  /portfolio start      - Start portfolio scheduler")
        output.info("  /portfolio stop       - Stop portfolio scheduler")
        output.info("  /portfolio positions  - List open portfolio positions")
        output.info("  /portfolio close <id|all> - Manually close position(s)")
        output.info("  /portfolio history    - Show recently closed positions")
        output.info("  /portfolio reset      - Delete closed positions & reset PnL")
        output.info("  /portfolio set [param] [value] - View/change runtime params")
        return

    subcmd = args[0].lower()

    if subcmd == "status":
        if not scheduler:
            output.warning("Portfolio strategy scheduler is not enabled.")
            return
        status = scheduler.get_status()
        config = scheduler.engine.config
        open_count = await db.count_open_portfolio_positions(chain=config.chain)
        daily_pnl = await db.get_daily_portfolio_pnl()

        output.info("📈 Portfolio Strategy Status:")
        output.info(f"  Running: {'✅ Yes' if status['running'] else '❌ No'}")
        output.info(f"  Chain: {config.chain}")
        output.info(f"  Dry run: {'✅ Yes' if config.dry_run else '❌ No (LIVE)'}")
        output.info(f"  Position size: ${config.position_size_usd:,.2f}")
        tp_display = "disabled" if config.take_profit_pct == 0 else f"{config.take_profit_pct:.1f}%"
        output.info(f"  TP: {tp_display} | SL: {config.stop_loss_pct:.1f}% | Trail: {config.trailing_stop_pct:.1f}% | Sell: {config.sell_pct:.1f}%")
        output.info(f"  Open positions: {open_count}/{config.max_positions}")
        output.info(f"  Daily realized PnL: ${daily_pnl:,.2f}")
        output.info(f"  Discovery interval: {status['discovery_interval_seconds']}s")
        output.info(f"  Exit check interval: {status['exit_check_interval_seconds']}s")
        output.info(f"  Discovery cycles: {status['discovery_cycles']}")
        output.info(f"  Exit check cycles: {status['exit_check_cycles']}")
        if status["last_discovery"]:
            output.info(f"  Last discovery: {status['last_discovery']}")
        if status["last_exit_check"]:
            output.info(f"  Last exit check: {status['last_exit_check']}")
        return

    if subcmd == "run":
        if not scheduler:
            output.warning("Portfolio strategy scheduler is not enabled.")
            return
        output.status("Running portfolio discovery cycle...")
        result = await scheduler.run_discovery_now()
        output.info(f"✅ Discovery complete: {result.summary}")
        for pos in result.positions_opened:
            output.info(
                f"  Opened: {pos.symbol} @ ${pos.entry_price:.10f} "
                f"(score: {pos.momentum_score or 0:.0f})"
            )
        for err in result.errors:
            output.warning(f"  Error: {err}")
        return

    if subcmd == "check":
        if not scheduler:
            output.warning("Portfolio strategy scheduler is not enabled.")
            return
        output.status("Running portfolio exit checks...")
        result = await scheduler.run_exit_check_now()
        output.info(f"✅ Exit check complete: {result.summary}")
        for pos in result.positions_closed:
            pnl = pos.realized_pnl_usd if pos.realized_pnl_usd is not None else 0.0
            output.info(f"  Closed: {pos.symbol} ({pos.close_reason}) PnL: ${pnl:,.4f}")
        for err in result.errors:
            output.warning(f"  Error: {err}")
        return

    if subcmd == "start":
        if not scheduler:
            output.warning("Portfolio strategy scheduler is not enabled.")
            return
        if scheduler.is_running:
            output.info("Portfolio scheduler is already running.")
            return
        await scheduler.start()
        output.info("✅ Portfolio scheduler started")
        return

    if subcmd == "stop":
        if not scheduler:
            output.warning("Portfolio strategy scheduler is not enabled.")
            return
        if not scheduler.is_running:
            output.info("Portfolio scheduler is not running.")
            return
        await scheduler.stop()
        output.info("✅ Portfolio scheduler stopped")
        return

    if subcmd == "positions":
        chain_filter = scheduler.engine.config.chain if scheduler else "solana"
        positions = await db.list_open_portfolio_positions(chain=chain_filter)
        if not positions:
            output.info("No open portfolio positions.")
            return
        output.info(f"📈 Open Portfolio Positions ({len(positions)}):")
        for pos in positions:
            age_h = 0.0
            if pos.opened_at:
                from datetime import datetime, timezone
                age_h = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 3600
            output.info(
                f"  • #{pos.id} {pos.symbol} entry ${pos.entry_price:.10f}, "
                f"stop ${pos.stop_price:.10f}, take {'disabled' if pos.take_price == float('inf') else f'${pos.take_price:.10f}'}, "
                f"high ${pos.highest_price:.10f}, qty {pos.quantity_token:.4f}, "
                f"age {age_h:.1f}h"
            )
        return

    if subcmd == "close":
        if len(args) < 2:
            output.warning("Usage: /portfolio close <position_id|all>")
            return

        chain_filter = scheduler.engine.config.chain if scheduler else "solana"
        target = args[1].lower()

        if target == "all":
            positions = await db.list_open_portfolio_positions(chain=chain_filter)
            if not positions:
                output.info("No open portfolio positions to close.")
                return
            closed = 0
            for pos in positions:
                ok = await db.close_portfolio_position(
                    position_id=pos.id,
                    exit_price=pos.entry_price,
                    close_reason="manual",
                    realized_pnl_usd=0.0,
                )
                if ok:
                    closed += 1
            output.info(f"✅ Closed {closed}/{len(positions)} open portfolio positions.")
            return

        try:
            position_id = int(target)
        except ValueError:
            output.warning(f"Invalid position ID: {target}. Use a number or 'all'.")
            return

        positions = await db.list_open_portfolio_positions(chain=chain_filter)
        position = next((p for p in positions if p.id == position_id), None)
        if position is None:
            output.warning(f"No open portfolio position found with ID {position_id}.")
            return

        ok = await db.close_portfolio_position(
            position_id=position.id,
            exit_price=position.entry_price,
            close_reason="manual",
            realized_pnl_usd=0.0,
        )
        if ok:
            output.info(
                f"✅ Closed position #{position.id} ({position.symbol}) "
                f"entry ${position.entry_price:.10f}, qty {position.quantity_token:.4f}"
            )
        else:
            output.warning(f"Failed to close position #{position_id}.")
        return

    if subcmd == "history":
        chain_filter = scheduler.engine.config.chain if scheduler else "solana"
        positions = await db.list_closed_portfolio_positions(limit=20, chain=chain_filter)
        if not positions:
            output.info("No closed portfolio positions yet.")
            return
        output.info(f"📈 Recent Closed Portfolio Positions ({len(positions)}):")
        for pos in positions:
            pnl = pos.realized_pnl_usd if pos.realized_pnl_usd is not None else 0.0
            pct = (pnl / pos.notional_usd * 100) if pos.notional_usd else 0.0
            output.info(
                f"  • #{pos.id} {pos.symbol} ${pos.entry_price:.10f} → ${pos.exit_price or 0:.10f} "
                f"PnL ${pnl:,.4f} ({pct:+.1f}%) [{pos.close_reason or '?'}]"
            )
        return

    if subcmd == "reset":
        chain_filter = scheduler.engine.config.chain if scheduler else "solana"
        closed = await db.list_closed_portfolio_positions(limit=10000, chain=chain_filter)
        if not closed:
            output.info("No closed portfolio positions to delete.")
            return
        output.warning(
            f"⚠️  This will permanently delete {len(closed)} closed position(s) "
            "and their execution records."
        )
        try:
            answer = input("Type 'yes' to confirm: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "yes":
            output.info("Reset cancelled.")
            return
        deleted = await db.delete_closed_portfolio_data()
        output.info(f"✅ Deleted {deleted} closed position(s) and associated executions. Daily PnL reset.")
        return

    if subcmd == "set":
        _TUNABLE_PARAMS: Dict[str, tuple] = {
            "position_size_usd": (float, 0.01, None),
            "max_positions": (int, 1, 50),
            "take_profit_pct": (float, 0.0, 500.0),
            "stop_loss_pct": (float, 0.1, 100.0),
            "trailing_stop_pct": (float, 0.1, 100.0),
            "sell_pct": (float, 0.1, 100.0),
            "max_hold_hours": (int, 1, 720),
            "daily_loss_limit_usd": (float, 0.0, None),
            "cooldown_seconds": (int, 0, None),
            "min_momentum_score": (float, 0.0, 100.0),
            "price_check_seconds": (int, 10, 3600),
            "min_token_age_hours": (float, 0.0, None),
            "max_token_age_hours": (float, 0.0, None),
        }

        if not scheduler:
            output.warning("Portfolio strategy scheduler is not enabled.")
            return

        if len(args) < 2:
            config = scheduler.engine.config
            output.info("📈 Tunable Portfolio Parameters (use /portfolio set <param> <value>):")
            for param, (typ, lo, hi) in _TUNABLE_PARAMS.items():
                val = getattr(config, param)
                constraint = f"≥{lo}"
                if hi is not None:
                    constraint += f", ≤{hi}"
                if typ is float:
                    output.info(f"  {param} = {val:,.2f}  ({constraint})")
                else:
                    output.info(f"  {param} = {val}  ({constraint})")
            return

        if len(args) < 3:
            output.warning("Usage: /portfolio set <param> <value>")
            return

        param_name = args[1].lower()
        raw_value = args[2]

        if param_name not in _TUNABLE_PARAMS:
            output.warning(
                f"Unknown parameter: {param_name}. "
                f"Tunable: {', '.join(_TUNABLE_PARAMS)}"
            )
            return

        typ, lo, hi = _TUNABLE_PARAMS[param_name]
        try:
            parsed = typ(raw_value)
        except (ValueError, TypeError):
            output.warning(f"Invalid value '{raw_value}' for {param_name} (expected {typ.__name__}).")
            return

        if parsed < lo:
            output.warning(f"Value {parsed} is below minimum {lo} for {param_name}.")
            return
        if hi is not None and parsed > hi:
            output.warning(f"Value {parsed} is above maximum {hi} for {param_name}.")
            return

        config = scheduler.engine.config
        old_value = getattr(config, param_name)
        setattr(config, param_name, parsed)

        # Validate token age bounds after update
        if param_name in ("min_token_age_hours", "max_token_age_hours"):
            mn = config.min_token_age_hours
            mx = config.max_token_age_hours
            if mn > 0 and mx > 0 and mn > mx:
                setattr(config, param_name, old_value)
                output.warning(
                    f"Rejected: min_token_age_hours ({mn}) cannot exceed "
                    f"max_token_age_hours ({mx}). Value unchanged."
                )
                return

        if typ is float:
            output.info(f"✅ {param_name}: {old_value:,.2f} → {parsed:,.2f}")
        else:
            output.info(f"✅ {param_name}: {old_value} → {parsed}")
        return

    output.warning(f"Unknown subcommand: {subcmd}. Use /portfolio for help.")


def _validate_command_exists(command: str, label: str, optional: bool = False) -> None:
    """Ensure the first token of a command is available on PATH or as a file."""
    if not command:
        if optional:
            return
        raise ValueError(f"{label} command is empty.")
    parts = shlex.split(command)
    if not parts:
        if optional:
            return
        raise ValueError(f"{label} command is invalid.")
    binary = parts[0]
    if shutil.which(binary) or Path(binary).exists():
        return
    raise FileNotFoundError(
        f"{label} command not found: '{binary}'. Install it or update MCP settings."
    )


async def async_main() -> None:
    """Async CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="DEX Agentic Bot - Query token info across blockchains",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m app "search for BONK on solana"
  python -m app --interactive
  python -m app --output json "top pools on solana"
  python -m app "trending tokens"
        """,
    )

    parser.add_argument(
        "query",
        nargs="?",
        help="Natural language query (e.g., 'search for PEPE')",
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Start interactive REPL mode",
    )
    parser.add_argument(
        "-o", "--output",
        choices=["text", "json", "table"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show debug information",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read query from stdin",
    )
    parser.add_argument(
        "--no-rugcheck",
        action="store_true",
        help="Disable rugcheck MCP server (faster startup)",
    )
    parser.add_argument(
        "--no-trader",
        action="store_true",
        help="Disable trader MCP server (faster startup)",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Disable Telegram notifications",
    )
    parser.add_argument(
        "--telegram-only",
        action="store_true",
        help="Run only the Telegram bot (no CLI interaction)",
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="Enable portfolio strategy scheduler",
    )
    parser.add_argument(
        "--portfolio-live",
        action="store_true",
        help="Run portfolio strategy in live mode (overrides dry-run setting)",
    )
    parser.add_argument(
        "--http-api",
        action="store_true",
        help="Run the HTTP analysis API server (port 8080) instead of interactive bot",
    )

    args = parser.parse_args()

    # Determine output format
    try:
        output_format = OutputFormat(args.output)
    except ValueError:
        output_format = OutputFormat.TABLE

    output = CLIOutput(format=output_format, verbose=args.verbose)

    # Run HTTP API server and return early
    if args.http_api:
        import uvicorn
        config = uvicorn.Config("app.api_server:app", host="0.0.0.0", port=8080, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
        return

    # Validate arguments
    if not args.interactive and not args.query and not args.stdin and not args.telegram_only:
        parser.print_help()
        sys.exit(1)

    # Get query from stdin if requested
    query: Optional[str] = args.query
    if args.stdin:
        query = sys.stdin.read().strip()
        if not query:
            output.error("No query provided via stdin")
            sys.exit(1)

    # Load settings
    try:
        settings = load_settings()
    except Exception as exc:
        output.error(f"Failed to load settings: {exc}")
        output.info("Ensure .env file exists with GEMINI_API_KEY set")
        sys.exit(1)

    # Validate external MCP commands early
    try:
        _validate_command_exists(settings.mcp_dexscreener_cmd, "DexScreener")
        _validate_command_exists(settings.mcp_dexpaprika_cmd, "DexPaprika")
        _validate_command_exists(
            settings.mcp_rugcheck_cmd,
            "Rugcheck",
            optional=args.no_rugcheck or not settings.mcp_rugcheck_cmd,
        )
        _validate_command_exists(
            settings.mcp_trader_cmd,
            "Trader",
            optional=args.no_trader or not settings.mcp_trader_cmd,
        )
    except Exception as exc:
        output.error(str(exc))
        sys.exit(1)

    # Initialize MCP manager
    output.status("Starting MCP servers...")
    trader_env_values = {
        "SOLANA_PRIVATE_KEY": settings.solana_private_key,
        "SOLANA_RPC_URL": settings.solana_rpc_url,
        "JUPITER_API_BASE": settings.jupiter_api_base,
        "JUPITER_API_KEY": settings.jupiter_api_key,
    }
    # Only forward RPC URL when explicitly configured here to avoid overriding
    # a private RPC set in dex-trader-mcp's own runtime environment.
    if "solana_rpc_url" not in settings.model_fields_set:
        trader_env_values.pop("SOLANA_RPC_URL", None)
    trader_env = {k: v for k, v in trader_env_values.items() if v} or None
    mcp_manager = MCPManager(
        dexscreener_cmd=settings.mcp_dexscreener_cmd,
        dexpaprika_cmd=settings.mcp_dexpaprika_cmd,
        rugcheck_cmd="" if args.no_rugcheck else settings.mcp_rugcheck_cmd,
        solana_rpc_cmd=settings.mcp_solana_rpc_cmd,
        trader_cmd="" if args.no_trader else settings.mcp_trader_cmd,
        call_timeout=float(settings.mcp_call_timeout),
        solana_rpc_url=settings.solana_rpc_url,
        trader_env=trader_env,
    )

    try:
        await mcp_manager.start()
        output.status("MCP servers ready")
    except Exception as exc:
        output.error(f"Failed to start MCP servers: {exc}")
        sys.exit(1)

    # Initialize database
    db = Database()
    try:
        await db.connect()
    except Exception as exc:
        output.error(f"Failed to initialize database: {exc}")
        await mcp_manager.shutdown()
        sys.exit(1)

    # Initialize Telegram notifier if enabled
    telegram: Optional[TelegramNotifier] = None
    telegram_only_mode = args.telegram_only
    
    if (
        (args.interactive or telegram_only_mode)
        and settings.telegram_alerts_enabled
        and not args.no_telegram
        and settings.telegram_bot_token
    ):
        telegram = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            subscribers_db_path=settings.telegram_subscribers_db_path,
            private_mode=settings.telegram_private_mode,
        )
        
        # Create token analyzer for Telegram bot
        token_analyzer = TokenAnalyzer(
            api_key=settings.gemini_api_key,
            mcp_manager=mcp_manager,
            model_name=settings.gemini_model,
            verbose=args.verbose,
        )
        telegram.set_token_analyzer(token_analyzer)
    
    # Validate telegram-only mode has telegram configured
    if telegram_only_mode and not telegram:
        output.error("Telegram bot not configured. Set TELEGRAM_BOT_TOKEN in .env")
        sys.exit(1)

    # Create log callback for verbose mode
    def log_callback(level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        if level == "error":
            output.error(message)
        elif level == "tool":
            output.debug(message, data)
        else:
            output.debug(message, data)

    # Initialize portfolio strategy scheduler if enabled
    portfolio_scheduler: Optional[PortfolioScheduler] = None
    portfolio_enabled = args.portfolio or settings.portfolio_enabled
    if args.interactive and portfolio_enabled:
        portfolio_config = PortfolioStrategyConfig(
            enabled=True,
            dry_run=False if args.portfolio_live else settings.portfolio_dry_run,
            chain=settings.portfolio_chain.lower(),
            max_positions=settings.portfolio_max_positions,
            position_size_usd=settings.portfolio_position_size_usd,
            take_profit_pct=settings.portfolio_take_profit_pct,
            stop_loss_pct=settings.portfolio_stop_loss_pct,
            trailing_stop_pct=settings.portfolio_trailing_stop_pct,
            sell_pct=settings.portfolio_sell_pct,
            max_hold_hours=settings.portfolio_max_hold_hours,
            discovery_interval_mins=settings.portfolio_discovery_interval_mins,
            price_check_seconds=settings.portfolio_price_check_seconds,
            daily_loss_limit_usd=settings.portfolio_daily_loss_limit_usd,
            min_volume_usd=settings.portfolio_min_volume_usd,
            min_liquidity_usd=settings.portfolio_min_liquidity_usd,
            min_market_cap_usd=settings.portfolio_min_market_cap_usd,
            min_token_age_hours=settings.portfolio_min_token_age_hours,
            max_token_age_hours=settings.portfolio_max_token_age_hours,
            cooldown_seconds=settings.portfolio_cooldown_seconds,
            min_momentum_score=settings.portfolio_min_momentum_score,
            max_slippage_bps=settings.portfolio_max_slippage_bps,
            quote_mint=settings.portfolio_quote_mint,
            rpc_url=settings.solana_rpc_url,
            quote_method=settings.portfolio_quote_method,
            execute_method=settings.portfolio_execute_method,
            slippage_probe_enabled=settings.portfolio_slippage_probe_enabled,
            slippage_probe_usd=settings.portfolio_slippage_probe_usd,
            slippage_probe_max_slippage_pct=settings.portfolio_slippage_probe_max_slippage_pct,
            sol_dump_threshold_pct=settings.portfolio_sol_dump_threshold_pct,
            sol_trend_lookback_mins=settings.portfolio_sol_trend_lookback_mins,
            insider_check_enabled=settings.portfolio_insider_check_enabled,
            insider_max_concentration_pct=settings.portfolio_insider_max_concentration_pct,
            insider_max_creator_pct=settings.portfolio_insider_max_creator_pct,
            insider_warn_concentration_pct=settings.portfolio_insider_warn_concentration_pct,
            insider_warn_creator_pct=settings.portfolio_insider_warn_creator_pct,
            shadow_audit_enabled=settings.portfolio_shadow_audit_enabled,
            shadow_check_minutes=settings.portfolio_shadow_check_minutes,
            decision_log_enabled=settings.portfolio_decision_log_enabled,
        )
        portfolio_engine = PortfolioStrategyEngine(
            db=db,
            mcp_manager=mcp_manager,
            config=portfolio_config,
            api_key=settings.gemini_api_key,
            model_name=settings.gemini_model,
            verbose=args.verbose,
            log_callback=log_callback if args.verbose else None,
        )
        portfolio_scheduler = PortfolioScheduler(
            engine=portfolio_engine,
            discovery_interval_seconds=settings.portfolio_discovery_interval_mins * 60,
            exit_check_interval_seconds=settings.portfolio_price_check_seconds,
            telegram=telegram,
            verbose=args.verbose,
            log_callback=log_callback if args.verbose else None,
        )

    # Initialize planner
    from app.agent import AgenticPlanner

    planner = AgenticPlanner(
        api_key=settings.gemini_api_key,
        mcp_manager=mcp_manager,
        model_name=settings.gemini_model,
        max_iterations=settings.agentic_max_iterations,
        max_tool_calls=settings.agentic_max_tool_calls,
        timeout_seconds=settings.agentic_timeout_seconds,
        verbose=args.verbose,
        log_callback=log_callback if args.verbose else None,
    )

    try:
        if telegram_only_mode:
            await run_telegram_only(telegram, output)
        elif args.interactive:
            await run_interactive(
                planner, output, db, mcp_manager,
                telegram, portfolio_scheduler
            )
        elif query:
            await run_single_query(planner, query, output, context={})
    except KeyboardInterrupt:
        output.info("\nInterrupted")
    finally:
        output.status("Shutting down...")
        await db.close()
        await mcp_manager.shutdown()


def main() -> None:
    """Synchronous wrapper for CLI entry."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
