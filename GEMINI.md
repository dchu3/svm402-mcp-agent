# Project Overview: x402 MCP Agent

The x402 MCP Agent is a CLI and API server for token safety analysis across multiple blockchains. It uses Gemini AI to orchestrate calls to MCP (Model Context Protocol) servers for data retrieval and analysis, providing a unified `analyze_token` tool for other AI agents to consume.

## Key Features:
- **Agentic AI:** Gemini AI for natural language understanding and intelligent tool selection.
- **Solana-Focused:** Full end-to-end support for Solana tokens.
- **Safety Checks:** Rugcheck token safety analysis (Solana).
- **Interactive CLI:** REPL mode with conversation memory and context management.
- **x402 Paid API:** Exposes the analysis pipeline as a structured JSON endpoint, requiring a USDC payment via the x402 protocol before returning results.

## Architecture:

The core is a Python "Agentic Planner" that interacts with the Gemini API. It interprets user queries and orchestrates calls to MCP servers via native function calling. The `TokenAnalyzer` handles structured token reports for the x402 API endpoint.

### Core Flow

```
User Query → AgenticPlanner → Gemini AI ─┬→ MCP Clients → External APIs
                  ↑                       │
                  └─── Tool Results ──────┘
```

```
┌──────────────────────────────────────────────────┐
│                User Query (CLI)                  │
└──────────────────────┬───────────────────────────┘
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
┌──────────────────┐     ┌──────────────────┐
│  AgenticPlanner  │     │  TokenAnalyzer   │
│  (interactive    │     │   (x402 API)     │
│   CLI queries)   │     │                  │
└────────┬─────────┘     └────────┬─────────┘
         │                        │
         └───────────┬────────────┘
                     ▼
         ┌───────────────────────┐
         │    MCP Clients        │
         │  (JSON-RPC / stdio)   │
         └───────────┬───────────┘
                     │
    ┌────────┬───────┼───────┐
    ▼        ▼       ▼       ▼
DexScreener DexPap Rugcheck Solana
  (price)  (pools) (safety)  (RPC)
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `cli.py` | Entry point, interactive REPL mode and single queries |
| `api_server.py` | FastAPI server exposing the x402 analysis endpoint |
| `agent.py` | Multi-turn reasoning loop with Gemini; tool selection, execution, malformed call recovery |
| `mcp_client.py` | Spawns and manages MCP server subprocesses via JSON-RPC over stdio |
| `tool_converter.py` | Converts MCP tool schemas to Gemini `FunctionDeclaration` format |
| `token_analyzer.py` | Parallel MCP calls for token analysis, chain detection, report synthesis |
| `config.py` | Pydantic settings loaded from `.env` |
| `output.py` | Rich terminal formatting (tables, colors) |
| `formatting.py` | Shared formatting utilities |
| `types.py` | Shared type definitions (`PlannerResult`, `LogCallback`, etc.) |

## Technologies Used:

- **Python 3.10+:** Main application logic.
- **Google Generative AI SDK:** For interacting with the Gemini API.
- **Pydantic & Pydantic Settings:** For configuration management and data validation.
- **FastAPI:** Internal HTTP API.
- **Rich:** For rich terminal output.
- **Node.js 18+ & npm:** Used by MCP servers and the external x402 payment gateway.

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

# HTTP API server
./scripts/start.sh --http-api
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
