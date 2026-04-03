# x402 MCP Token Analysis Agent

> [!CAUTION]
> **⚠️ HIGH RISK – USE AT YOUR OWN RISK**
>
> This is experimental code. The author provides NO WARRANTY and is NOT LIABLE for ANY financial losses, bugs, or bad decisions.
> Do NOT use real money without extensive backtesting and forward testing on paper/demo accounts.
> Past performance (if shown) does NOT indicate future results.

An AI-powered CLI and API server for token safety checks and market analysis. Uses Gemini AI with MCP servers for multi-chain data retrieval. It features an x402 paid analysis API endpoint.

> [!WARNING]
> **API Cost & Security Notice**
> This bot uses the Gemini API which **incurs usage costs** with every request. Set spending limits in your API provider's dashboard to avoid unexpected charges.

## Features

- 🔍 **AI Token Analysis** — Send a token address, get a detailed safety & market report.
- 🛡️ **Safety Checks** — Rugcheck token safety analysis (Solana).
- 📊 **Market Data** — Price, volume, liquidity, market cap via DexScreener & DexPaprika.
- 🚨 **Wash Trading Detection** — Scans recent pool transactions to detect repeat-buyer wallets, coordinated manipulation, and potential rug pull accumulation patterns. Generates a 0–10 manipulation score.
- 🤖 **Gemini AI** — Natural language queries, intelligent tool selection, risk assessment.
- 💳 **x402 Paid API** — Built-in Express.js API server for paid analysis via the x402 v2 protocol.

## Quick Start

### Option A: Docker (recommended)

The easiest way to get started. All MCP servers are pre-built and bundled in the image.

```bash
git clone https://github.com/dchu3/x402-mcp-agent && cd x402-mcp-agent
cp .env.example .env
# Edit .env — set GEMINI_API_KEY (required).
docker compose run --rm bot "search for BONK on solana"
```

**Run modes:**

```bash
# Interactive CLI
docker compose up

# Single query
docker compose run --rm bot "search for BONK on solana"

# Rebuild to get latest MCP server updates
docker compose build --no-cache
```

### Option B: Manual Setup

### 1. Setup

```bash
./scripts/install.sh
cp .env.example .env
# Edit .env with your API keys
```

### 2. Configuration

Key settings in `.env`:

```env
# Required
GEMINI_API_KEY=your-gemini-api-key

# Optional (defaults shown)
GEMINI_MODEL=gemini-3-flash-preview

# MCP Servers (token data sources)
MCP_DEXSCREENER_CMD=node /path/to/dex-screener-mcp/dist/index.js
MCP_DEXPAPRIKA_CMD=dexpaprika-mcp
MCP_RUGCHECK_CMD=node /path/to/dex-rugcheck-mcp/dist/index.js
MCP_SOLANA_RPC_CMD=node /path/to/solana-rpc-mcp/dist/index.js

# Timeout (seconds) for MCP tool calls (default: 90)
MCP_CALL_TIMEOUT=90

# Solana RPC (for token decimal lookups and tx verification)
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
```

### 3. Run

```bash
# Interactive CLI
./scripts/start.sh --interactive

# Single query
./scripts/start.sh "search for BONK on solana"

# Run HTTP API Server
./scripts/start.sh --http-api
```

## Interactive CLI Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/clear` | Clear conversation history |
| `/context` | Show current conversation context |
| `/quit` | Exit the CLI |

### x402 Paid Analysis API

The x402 MCP endpoint (`POST /mcp`) returns a structured JSON report designed for agent consumption. Each call requires a USDC payment via the [x402 v2 protocol](https://x402.org).

**Tools:**

| Tool | Description | Price |
|------|-------------|-------|
| `analyze_token(address, chain?)` | Full AI token safety & market analysis | `$SERVER_PRICE_ANALYZE` USDC |
| `get_wallet_balance(address)` | Check SOL & USDC balance of a wallet | **Free** |

The `get_wallet_balance` tool lets clients verify they have sufficient USDC before paying for analysis.

## Production Deployment

### 1. Security Setup

Generate and set `INTERNAL_API_SECRET` to prevent direct `/analyze` bypass (ensuring all analysis requests are paid via x402):

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# Copy the output into .env as INTERNAL_API_SECRET=<value>
```

### 2. Deploy with Caddy (Automatic HTTPS)

The production configuration uses Caddy to handle SSL/TLS termination and reverse proxying.

```bash
docker compose -f docker-compose.prod.yml up -d
```

This starts three services:
- **Caddy** — Reverse proxy on ports 80/443, auto-provisions Let's Encrypt TLS certificates.
- **analysis-server** — Express.js MCP server (internal only, not directly exposed).
- **api-service** — FastAPI Python backend (internal only).

#### Connecting from an MCP Client

The analysis server uses **StreamableHTTP** transport at `/mcp`. Any MCP-compatible client can connect to it by pointing at the server URL.

> [!NOTE]
> Standard MCP clients (like Gemini CLI or Claude Desktop) do not natively support x402 payments. They can be used to call free tools like `get_wallet_balance`, but will receive a `402 Payment Required` error when calling `analyze_token`. For Gemini CLI users, the **[svm402-gemini-extension](https://github.com/dchu3/svm402-gemini-extension)** automatically handles these payments. See [server/README.md](server/README.md) for other payment-enabled client examples.

**Quick Test (Free Tool)** — verify the connection using `curl`:

```bash
curl -X POST https://svm402.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_wallet_balance","arguments":{"address":"<WALLET_ADDRESS>"}},"id":1}'
```

### Gemini CLI Extension (Recommended)

To use this server directly from your [Gemini CLI](https://github.com/google/gemini-cli) with automatic x402 payment handling:

```bash
gemini extensions install https://github.com/dchu3/svm402-gemini-extension
```


## Architecture

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

## Analysis Output

The `analyze_token` tool returns a structured JSON report with the following sections:

| Section | Description |
|---------|-------------|
| `price_data` | Current price, 24h change, market cap, volume, FDV |
| `liquidity` | Total liquidity, top pool, LP locked percentage |
| `safety` | Rugcheck status, risk score (0–10), risk level, safety flags |
| `holder_snapshot` | Top-10 holder concentration and risk level |
| `wash_trading` | Manipulation detection: score (0–10), repeat buyers, flags |
| `ai_analysis` | AI-generated strengths, risks, whale signal, narrative momentum |
| `verdict` | Recommended action, confidence level, one-sentence summary |
| `human_readable` | Formatted text summary with emojis |

### Wash Trading Detection

The wash trading analyzer scans recent transactions on the token's primary DEX pool to detect manipulation patterns:

- **Repeat buyers** — Wallets making multiple purchases of the same token
- **Low wallet diversity** — Few unique wallets relative to total transactions
- **Buy/sell asymmetry** — Extreme buy pressure suggesting accumulation before dump
- **Rapid trading** — Same wallet making many trades in a short time window

The `manipulation_score` ranges from 0 (clean) to 10 (critical manipulation). Levels:
- `clean` (0–2): Normal trading patterns
- `moderate` (3–5): Some concerning signals
- `suspicious` (6–8): Likely manipulation
- `critical` (9–10): Strong wash trading indicators

## Prerequisites

- **Python 3.10+**
- **Node.js 18+** (for MCP servers)
- **Gemini API Key** (from [Google AI Studio](https://makersuite.google.com/app/apikey))

## Development

```bash
source .venv/bin/activate
pytest
python -m app "your query"
```

## MCP Servers

| Server | Purpose | Chains |
|--------|---------|--------|
| [dex-screener-mcp](https://github.com/dchu3/dex-screener-mcp) | Token prices, pools, volume | All |
| [dexpaprika-mcp](https://github.com/coinpaprika/dexpaprika-mcp) | Pool details, OHLCV data | All |
| [dex-rugcheck-mcp](https://github.com/dchu3/dex-rugcheck-mcp) | Token safety | Solana |
| [solana-rpc-mcp](https://github.com/dchu3/solana-rpc-mcp) | Direct Solana RPC queries | Solana |

Each MCP server runs with its project root as the working directory and loads its own `.env` independently.

## License

MIT
