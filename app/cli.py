"""CLI interface for x402 MCP Token Analysis."""

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
from app.mcp_client import MCPManager
from app.output import CLIOutput, OutputFormat
from app.types import PlannerResult

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
    mcp_manager: MCPManager,
) -> None:
    """Run interactive REPL session."""
    output.info("x402 MCP Token Analysis - Interactive Mode")
    output.info("Type your queries, or use /help for commands")
    output.info("-" * 50)

    context: Dict[str, Any] = {}
    conversation_history: List[Dict[str, str]] = []
    recent_tokens: List[Dict[str, str]] = []

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
                    query, output, mcp_manager,
                    conversation_history, recent_tokens,
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
        pass


async def _handle_command(
    query: str,
    output: CLIOutput,
    mcp_manager: MCPManager,
    conversation_history: List[Dict[str, str]],
    recent_tokens: List[Dict[str, str]],
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
        description="x402 MCP Agent - Query token info across blockchains",
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
    if not args.interactive and not args.query and not args.stdin:
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
    except Exception as exc:
        output.error(str(exc))
        sys.exit(1)

    # Initialize MCP manager
    output.status("Starting MCP servers...")
    mcp_manager = MCPManager(
        dexscreener_cmd=settings.mcp_dexscreener_cmd,
        dexpaprika_cmd=settings.mcp_dexpaprika_cmd,
        rugcheck_cmd="" if args.no_rugcheck else settings.mcp_rugcheck_cmd,
        solana_rpc_cmd=settings.mcp_solana_rpc_cmd,
        call_timeout=float(settings.mcp_call_timeout),
        solana_rpc_url=settings.solana_rpc_url,
    )

    try:
        await mcp_manager.start()
        output.status("MCP servers ready")
    except Exception as exc:
        output.error(f"Failed to start MCP servers: {exc}")
        sys.exit(1)

    # Create log callback for verbose mode
    def log_callback(level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        if level == "error":
            output.error(message)
        elif level == "tool":
            output.debug(message, data)
        else:
            output.debug(message, data)

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
        if args.interactive:
            await run_interactive(
                planner, output, mcp_manager
            )
        elif query:
            await run_single_query(planner, query, output, context={})
    except KeyboardInterrupt:
        output.info("\nInterrupted")
    finally:
        output.status("Shutting down...")
        await mcp_manager.shutdown()


def main() -> None:
    """Synchronous wrapper for CLI entry."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
