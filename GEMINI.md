# Project Overview: DEX Agentic Bot

The DEX Agentic Bot is a CLI and Telegram bot for token safety analysis and autonomous portfolio management across multiple blockchains. It uses Gemini AI to orchestrate calls to MCP (Model Context Protocol) servers for data retrieval and analysis.

## Key Features:
- **Agentic AI:** Gemini AI for natural language understanding and intelligent tool selection.
- **Solana-Focused:** Full end-to-end support for Solana tokens.
- **Safety Checks:** Rugcheck token safety analysis (Solana).
- **Interactive CLI:** REPL mode with conversation memory and context management.
- **Telegram Bot:** Send a token address, get an AI-powered analysis report.
- **Portfolio Strategy:** Autonomous token discovery, position management, and exit execution with trailing stops (Solana).
- **Flexible Output:** Formatted tables, raw text, or JSON.

## Architecture:

The core is a Python "Agentic Planner" that interacts with the Gemini API. It interprets user queries and orchestrates calls to MCP servers via native function calling. The `TokenAnalyzer` handles Telegram token reports as a parallel entry point.

### Core Flow

```
User Query → AgenticPlanner → Gemini AI ─┬→ MCP Clients → External APIs
                  ↑                       │
                  └─── Tool Results ──────┘
```

```
┌──────────────────────────────────────────────────┐
│           User Query (CLI / Telegram)            │
└──────────────────────┬───────────────────────────┘
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
┌──────────────────┐     ┌──────────────────┐
│  AgenticPlanner  │     │  TokenAnalyzer   │
│  (interactive    │     │  (Telegram bot   │
│   CLI queries)   │     │   token reports) │
└────────┬─────────┘     └────────┬─────────┘
         │                        │
         └───────────┬────────────┘
                     ▼
         ┌───────────────────────┐
         │    MCP Clients        │
         │  (JSON-RPC / stdio)   │
         └───────────┬───────────┘
                     │
    ┌────────┬───────┼───────┬────────┬────────┐
    ▼        ▼       ▼       ▼        ▼        ▼
DexScreener DexPap Rugcheck Solana  Trader
  (price)  (pools) (safety)  (RPC) (trading)
```

The **Portfolio Strategy** runs as a separate subsystem:

```
PortfolioScheduler (discovery every 30min + exit checks every 60s)
    │
    ├── PortfolioDiscovery → DexScreener + Rugcheck + Gemini AI scoring
    │
    ├── PortfolioStrategy  → trailing stop updates, TP/SL/timeout checks
    │
    └── TraderExecution    → buy/sell via trader MCP
    │
    └── Database (SQLite)  → ~/.dex-bot/portfolio.db
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `cli.py` | Entry point, interactive REPL mode and single queries |
| `agent.py` | Multi-turn reasoning loop with Gemini; tool selection, execution, malformed call recovery |
| `mcp_client.py` | Spawns and manages MCP server subprocesses via JSON-RPC over stdio |
| `tool_converter.py` | Converts MCP tool schemas to Gemini `FunctionDeclaration` format |
| `token_analyzer.py` | Parallel MCP calls for token analysis, chain detection, report synthesis |
| `database.py` | SQLite persistence for portfolio positions (`~/.dex-bot/portfolio.db`) |
| `portfolio_strategy.py` | Exit monitoring engine: TP/SL checks, trailing stop updates |
| `portfolio_discovery.py` | Hybrid discovery pipeline: DexScreener → filters → rugcheck → Gemini scoring |
| `portfolio_scheduler.py` | Async scheduler for discovery and exit check loops |
| `execution.py` | Trade execution service via trader MCP (quote → execute) |
| `price_cache.py` | Cached price lookups via DexScreener |
| `telegram_notifier.py` | Telegram bot: address detection, message routing, alert delivery |
| `telegram_subscribers.py` | Telegram subscriber management |
| `config.py` | Pydantic settings loaded from `.env` |
| `output.py` | Rich terminal formatting (tables, colors) |
| `formatting.py` | Shared formatting utilities |
| `types.py` | Shared type definitions (`PlannerResult`, `LogCallback`, etc.) |

## Technologies Used:

- **Python 3.10+:** Main application logic.
- **Google Generative AI SDK:** For interacting with the Gemini API.
- **Pydantic & Pydantic Settings:** For configuration management and data validation.
- **Rich:** For rich terminal output.
- **Node.js 18+ & npm:** Used by MCP servers.

## Building and Running:

### Prerequisites

- Python 3.10+
- Node.js 18+ (for MCP servers)
- npm (comes with Node.js)

### Installation

```bash
./scripts/install.sh
```

Creates a Python virtual environment and installs dependencies from `requirements.txt`.

### Configuration

Copy `.env.example` to `.env` and fill in your `GEMINI_API_KEY` and MCP server paths. See `.env.example` for the full list of settings.

### Usage

```bash
# Single query
./scripts/start.sh "search for BONK on solana"

# Interactive mode
./scripts/start.sh --interactive

# Telegram bot only
./scripts/start.sh --telegram-only

# Portfolio strategy
./scripts/start.sh --interactive --portfolio
```

### Development

```bash
source .venv/bin/activate
pytest
python -m app "your query"
```

## Development Conventions:

### Tool Naming

MCP tools are namespaced as `{client}_{method}` (e.g., `dexscreener_search_pairs`, `rugcheck_check_token`). The `parse_function_call_name()` function in `tool_converter.py` splits these back into client and method.

### Async Patterns

- All MCP calls are async; use `asyncio.gather()` for parallel tool execution.
- `MCPClient` uses locks (`_init_lock`, `_lock`) for thread-safe process communication.
- Background tasks (polling, Telegram) use `asyncio.create_task()`.

### Configuration

Settings are loaded via `pydantic-settings` from `.env`:
```python
from app.config import load_settings
settings = load_settings()  # Cached singleton
```

### Type Hints

Use type hints throughout. Key types:
- `PlannerResult` — Return type from `AgenticPlanner.run()`
- `LogCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]` — For verbose logging

### Error Handling in Agent

The `AgenticPlanner` has built-in recovery for malformed Gemini function calls:
- Detects `MALFORMED_FUNCTION_CALL` in finish_reason
- Retries up to 2 times with progressively simpler prompts
- Falls back to text-only response if recovery fails

### Modular Design

The application is structured into clear modules: CLI, agent logic, MCP management, portfolio strategy, output handling, and type definitions. Python typing and structured logging are used throughout.
