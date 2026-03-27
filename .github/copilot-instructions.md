# Copilot Instructions for DEX Agentic Bot

## Git Workflow

**Never push directly to `main` in any repo** (`dex-agentic-bot`, `dex-trader-mcp`, or any other repo in this project).

- Always create a branch before making changes: `feature/<short-description>` or `fix/<short-description>`
- Commit changes to the branch and push it
- Open a pull request for review — do not merge without user approval

```bash
# Start every change like this
git checkout -b fix/my-fix        # or feature/my-feature
# ... make changes ...
git add <files>
git commit -m "..."
git push -u origin fix/my-fix     # opens PR, never pushes to main
```

## Build, Test, and Lint

```bash
# Install dependencies (creates .venv and installs packages)
./scripts/install.sh

# Activate virtual environment
source .venv/bin/activate

# Run all tests
pytest

# Run a single test file
pytest tests/test_agent.py

# Run a specific test
pytest tests/test_agent.py::TestIsMalformedResponse::test_detects_malformed_function_call_finish_reason

# Run CLI directly
python -m app "your query"
```

## Architecture

This is an agentic CLI bot that uses Gemini AI to orchestrate calls to MCP (Model Context Protocol) servers for blockchain/DEX data.

### Core Flow

```
User Query → AgenticPlanner → Gemini AI ─┬→ MCP Clients → External APIs
                  ↑                       │
                  └─── Tool Results ──────┘
```

1. **CLI (`cli.py`)** - Entry point, handles interactive REPL mode and single queries
2. **AgenticPlanner (`agent.py`)** - Multi-turn reasoning loop with Gemini; handles tool selection, execution, and malformed call recovery
3. **MCPManager/MCPClient (`mcp_client.py`)** - Spawns and manages MCP server subprocesses via JSON-RPC over stdio
4. **Tool Converter (`tool_converter.py`)** - Converts MCP tool schemas to Gemini `FunctionDeclaration` format

### MCP Servers

External servers that expose tools to the AI agent:
- **dexscreener** - Token search, trending, pair data
- **dexpaprika** - Pool details, OHLCV, network info
- **rugcheck** - Solana token safety analysis
- **solana** - Direct Solana RPC queries

### Supporting Modules

- **watchlist.py / watchlist_poller.py** - Persistent token tracking with SQLite, background price monitoring
- **autonomous_agent.py / autonomous_scheduler.py** - Automated token discovery and position management
- **telegram_notifier.py** - Alert notifications via Telegram bot
- **config.py** - Pydantic settings from `.env` file
- **output.py** - Rich terminal formatting (tables, colors)

## Key Conventions

### Tool Naming

MCP tools are namespaced as `{client}_{method}` (e.g., `dexscreener_search_pairs`, `rugcheck_check_token`). The `parse_function_call_name()` function in `tool_converter.py` splits these back into client and method.

### Async Patterns

- All MCP calls are async; use `asyncio.gather()` for parallel tool execution
- `MCPClient` uses locks (`_init_lock`, `_lock`) for thread-safe process communication
- Background tasks (polling, Telegram) use `asyncio.create_task()`

### Configuration

Settings are loaded via `pydantic-settings` from `.env`:
```python
from app.config import load_settings
settings = load_settings()  # Cached singleton
```

### Type Hints

Use type hints throughout. Key types:
- `PlannerResult` - Return type from `AgenticPlanner.run()`
- `LogCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]` - For verbose logging

### Error Handling in Agent

The `AgenticPlanner` has built-in recovery for malformed Gemini function calls:
- Detects `MALFORMED_FUNCTION_CALL` in finish_reason
- Retries up to 2 times with progressively simpler prompts
- Falls back to text-only response if recovery fails
