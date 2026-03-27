# Token Safety & Analysis Bot

> [!CAUTION]
> **⚠️ HIGH RISK – USE AT YOUR OWN RISK**
>
> This is experimental code. Trading bots can lose ALL your money very quickly.
> The author provides NO WARRANTY and is NOT LIABLE for ANY financial losses, bugs, or bad decisions.
> Do NOT use real money without extensive backtesting and forward testing on paper/demo accounts.
> Past performance (if shown) does NOT indicate future results.

An AI-powered CLI and Telegram bot for token safety checks, market analysis, and autonomous portfolio management. Uses Gemini AI with MCP servers for multi-chain data retrieval.

> [!WARNING]
> **API Cost & Security Notice**
> This bot uses the Gemini API which **incurs usage costs** with every request. Set spending limits in your API provider's dashboard to avoid unexpected charges.
>
> **Never commit API keys to source control.** The Telegram bot defaults to **private mode** — configure your `TELEGRAM_CHAT_ID` before use.

## Features

- 🔍 **AI Token Analysis** — Send a token address (CLI or Telegram), get a detailed safety & market report
- 🛡️ **Safety Checks** — Rugcheck token safety analysis (Solana)
- 📊 **Market Data** — Price, volume, liquidity, market cap via DexScreener & DexPaprika
- 📈 **Portfolio Strategy** — Autonomous token discovery → buy → hold → exit at TP/SL with trailing stops (Solana)
- 🤖 **Gemini AI** — Natural language queries, intelligent tool selection, risk assessment
- ⚡ **Solana-Focused** — Full end-to-end support: analysis, safety, trading, and portfolio management

## Quick Start

### Option A: Docker (recommended)

The easiest way to get started. All MCP servers are pre-built and bundled in the image — no need to install Node.js or clone separate repos.

```bash
git clone https://github.com/dchu3/dex-agentic-bot && cd dex-agentic-bot
cp .env.example .env
# Edit .env — set GEMINI_API_KEY (required). Docker injects MCP commands automatically.
docker compose run --rm bot "search for BONK on solana"
```

**Run modes:**

```bash
# Interactive CLI
docker compose up

# Single query
docker compose run --rm bot "search for BONK on solana"

# Telegram bot only
docker compose run --rm bot --telegram-only

# Portfolio strategy (dry-run)
docker compose run --rm bot --interactive --portfolio

# Rebuild to get latest MCP server updates
docker compose build --no-cache
```

Data (SQLite databases) is persisted in a Docker volume (`dex-bot-data`) across container restarts.

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
MCP_TRADER_CMD=node /path/to/dex-trader-mcp/dist/index.js

# Timeout (seconds) for MCP tool calls (default: 90)
MCP_CALL_TIMEOUT=90

# Solana RPC (for token decimal lookups and tx verification)
# The public endpoint is heavily rate-limited and blocks cloud IPs.
# Use a private provider such as Helius (https://helius.dev).
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=your-key

# Trader MCP (optional — needed for live trading on Solana)
SOLANA_PRIVATE_KEY=your-base58-private-key
JUPITER_API_BASE=https://api.jup.ag/swap/v1
JUPITER_API_KEY=your-jupiter-api-key

# Telegram (optional)
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TELEGRAM_CHAT_ID=your-chat-id
TELEGRAM_PRIVATE_MODE=true

# Portfolio strategy (optional)
PORTFOLIO_ENABLED=false
PORTFOLIO_DRY_RUN=true
PORTFOLIO_POSITION_SIZE_USD=5.0
PORTFOLIO_MAX_POSITIONS=5
PORTFOLIO_TAKE_PROFIT_PCT=0.0
PORTFOLIO_STOP_LOSS_PCT=17.0
PORTFOLIO_TRAILING_STOP_PCT=11.0
PORTFOLIO_SELL_PCT=45.0
```

> **Trader MCP** — for live Solana trading, set `SOLANA_PRIVATE_KEY`, `SOLANA_RPC_URL` (to a reliable/private Solana RPC), `JUPITER_API_BASE`, and `JUPITER_API_KEY` in your `.env`. These variables are forwarded to the `dex-trader-mcp` subprocess automatically. A free Jupiter API key is available at [dev.jup.ag](https://dev.jup.ag/). See the [dex-trader-mcp README](https://github.com/dchu3/dex-trader-mcp) for details.

See `.env.example` for the full list of settings.

#### Telegram Private Mode

By default, only messages from your configured `TELEGRAM_CHAT_ID` are processed. Set `TELEGRAM_PRIVATE_MODE=false` to allow public access (use with caution — anyone can trigger API calls at your expense).

To find your chat ID, send a message to your bot and check the logs, or use [@userinfobot](https://t.me/userinfobot).

### 3. Run

```bash
# Telegram bot only (recommended for production)
./scripts/start.sh --telegram-only

# Interactive CLI
./scripts/start.sh --interactive

# Single query
./scripts/start.sh "search for BONK on solana"

# Portfolio strategy (dry-run)
./scripts/start.sh --interactive --portfolio

# Portfolio strategy (live)
./scripts/start.sh --interactive --portfolio --portfolio-live
```

## Usage

### Telegram Bot

Send any Solana token address to your bot:
- Solana: `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`

| Command | Description |
|---------|-------------|
| `/analyze <address>` | Analyze a token (quick summary) |
| `/full <address>` | Detailed analysis report |
| `/help` | Show help message |
| `/status` | Check bot status |
| `/subscribe` | Subscribe to price alerts (legacy, not shown in bot help) |
| `/unsubscribe` | Unsubscribe from price alerts (legacy, not shown in bot help) |

### Interactive CLI Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/clear` | Clear conversation history |
| `/context` | Show current conversation context |
| `/quit` | Exit the CLI |
| `/portfolio <subcommand>` | Portfolio strategy management |

#### Portfolio Subcommands

| Subcommand | Description |
|------------|-------------|
| `/portfolio status` | Scheduler/risk status and config summary |
| `/portfolio run` | Run one discovery cycle immediately |
| `/portfolio check` | Run one exit check cycle immediately |
| `/portfolio start` / `stop` | Start or stop the scheduler |
| `/portfolio positions` | Show open positions with unrealized PnL |
| `/portfolio close <id\|all>` | Manually close position(s) |
| `/portfolio set [param] [value]` | Show or change tunable runtime parameters |
| `/portfolio history` | Show recent closed positions with PnL |
| `/portfolio reset` | Delete closed positions and reset daily PnL |

### Portfolio Strategy

The portfolio strategy autonomously discovers promising Solana tokens, buys small positions, and exits when take-profit, stop-loss, or trailing stop conditions are met.

**How it works:**
1. **SOL trend gate**: Skip discovery if SOL has dropped faster than the configured threshold in the lookback window
2. **Discovery** (every 20 min): DexScreener trending → volume/liquidity/market cap filter → rugcheck safety → insider/sniper detection → heuristic momentum scoring → Gemini AI per-candidate buy/skip decision → buy approved candidates
3. **Exit monitoring** (every 40s): Check TP/SL thresholds, update trailing stops, close expired positions
4. **Risk guards**: Max positions cap, daily loss limit, cooldown after failures, duplicate prevention

**Discovery filters (configurable via `.env`):**
- `PORTFOLIO_MIN_VOLUME_USD` — Minimum 24h trading volume (default: 380k)
- `PORTFOLIO_MIN_LIQUIDITY_USD` — Minimum liquidity depth (default: 245k)
- `PORTFOLIO_MIN_MARKET_CAP_USD` — Minimum market cap or FDV (default: 1.65M)
- `PORTFOLIO_MIN_TOKEN_AGE_HOURS` — Reject tokens younger than this many hours (default: 11; 0 = disabled)
- `PORTFOLIO_MAX_TOKEN_AGE_HOURS` — Reject tokens older than this many hours (default: 0 = disabled)

**Pre-trade slippage probe (opt-in, live mode only):**

Before opening a position, optionally executes a tiny `buy_and_sell` round-trip to validate that real on-chain slippage matches the quoted slippage. If the deviation exceeds the threshold, the trade is aborted.

- `PORTFOLIO_SLIPPAGE_PROBE_ENABLED` — Enable the probe (default: `false`)
- `PORTFOLIO_SLIPPAGE_PROBE_USD` — Size of the test trade in USD (default: `0.50`)
- `PORTFOLIO_SLIPPAGE_PROBE_MAX_SLIPPAGE_PCT` — Abort if real slippage deviates more than this % from quoted price (default: `5.0`)

By default this runs in **dry-run mode** (`PORTFOLIO_DRY_RUN=true`).

**Partial sell (configurable via `.env`):**

Sell a percentage of the position on any **profitable** exit trigger (e.g. take-profit, trailing stop in profit), while keeping the remainder open with a continued trailing stop:
- `PORTFOLIO_SELL_PCT` — Percentage of the position to sell on profitable exits (default: 45; 100 = full exit)

When set below 100 and the trade is in profit, the bot partially closes the position; the remaining size stays open and the trailing stop continues tracking the remaining balance. If an exit is not profitable (at or below entry), the bot closes 100% of the position regardless of this setting.

**SOL trend gate (configurable via `.env`):**

Pauses discovery when the SOL price is dropping to avoid buying into a market-wide dump:
- `PORTFOLIO_SOL_DUMP_THRESHOLD_PCT` — Skip discovery if SOL dropped more than this % (default: -5.0)
- `PORTFOLIO_SOL_TREND_LOOKBACK_MINS` — Lookback window for the trend check (default: 60)

**Insider / sniper detection (configurable via `.env`):**

Analyzes top token holders via Solana RPC before buying. Tokens with suspicious concentration are rejected or flagged for AI review:
- `PORTFOLIO_INSIDER_CHECK_ENABLED` — Enable the check (default: `true`)
- `PORTFOLIO_INSIDER_MAX_CONCENTRATION_PCT` — Hard-reject if top-holder concentration exceeds this % (default: 50)
- `PORTFOLIO_INSIDER_MAX_CREATOR_PCT` — Hard-reject if creator holds more than this % (default: 30)
- `PORTFOLIO_INSIDER_WARN_CONCENTRATION_PCT` — Soft-flag for AI review above this % (default: 30)
- `PORTFOLIO_INSIDER_WARN_CREATOR_PCT` — Soft-flag for AI review above this % (default: 10)

**Shadow audit & decision logging (configurable via `.env`):**

Observability features for evaluating the discovery pipeline alongside normal trading behavior:
- `PORTFOLIO_SHADOW_AUDIT_ENABLED` — Record approved candidates as `shadow_positions` for an additional audit log, in parallel with normal portfolio execution (to avoid real trades, keep `PORTFOLIO_DRY_RUN=true` and do not pass `--portfolio-live`; default: `false`)
- `PORTFOLIO_SHADOW_CHECK_MINUTES` — Delay (in minutes) after a shadow position is created before it becomes eligible for a one-time price check (default: 30)
- `PORTFOLIO_DECISION_LOG_ENABLED` — Persist per-candidate reason codes for pipeline analysis (default: `false`)

### Example Report

The Telegram/CLI bot shows a human-readable report:

```
🔍 Token Analysis Report
Token: BONK | Chain: Solana
Address: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263

💰 Price: $0.00001234 (🟢 +5.20%)
📊 MCap: $5.20B | Vol 24h: $234M | Liq: $45.2M
💧 Top Pool: raydium ($23.0M)
🛡️ Safety: ✅ Safe
   Risk Score: 0.0/10 (low)

✅ Strengths: deep liquidity, clean contract, no tax mechanisms
⚠️ Risks: meme volatility, retail-driven momentum

🎯 Verdict: BUY (medium confidence)
   Solid blue-chip meme with deep liquidity and no red flags.

⏰ 2026-02-03 16:30 UTC
```

### x402 Paid Analysis API

The x402 MCP endpoint (`POST /mcp`) returns a structured JSON report designed for agent consumption. Each call requires a USDC payment via the [x402 v2 protocol](https://x402.org) — the default price is **$0.10 USDC** (configurable via `SERVER_PRICE_ANALYZE`).

Payment uses a **verify → analyze → settle** flow: the server first verifies the client's signed payment is valid (no funds moved), runs the AI analysis, and only settles the payment on-chain after a successful result. If the analysis fails, the client is **not charged**. If the facilitator does not support `/verify` (returns 404), the server falls back to settle-first mode where the client may be charged even if analysis fails — the default [PayAI](https://facilitator.payai.network) facilitator supports `/verify`, so this only applies to custom facilitator URLs. For devnet testing, set `X402_FACILITATOR_URL=https://x402.org/facilitator` and `SERVER_SOLANA_NETWORK=solana-devnet`.

**Protocol details:**
- **Network identifiers**: CAIP-2 format (e.g., `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` for mainnet)
- **Payment header**: Clients send `PAYMENT-SIGNATURE` (v2) or `X-PAYMENT` (v1) — both are accepted
- **402 response**: Includes `PAYMENT-REQUIRED` header (base64-encoded JSON) and matching JSON body

**Tools:**

| Tool | Description | Price |
|------|-------------|-------|
| `analyze_token(address, chain?)` | Full AI token safety & market analysis | `$SERVER_PRICE_ANALYZE` USDC |
| `get_wallet_balance(address)` | Check SOL & USDC balance of a wallet | **Free** |

The `get_wallet_balance` tool lets clients verify they have sufficient USDC before paying for analysis. It returns SOL balance, USDC balance, the current analysis price, and a `can_afford_analysis` boolean.

**Example response:**

```json
{
  "token": "BONK",
  "chain": "solana",
  "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
  "timestamp": "2026-03-09T08:07:00Z",
  "price_data": {
    "price_usd": 0.00001234,
    "change_24h_percent": 5.2,
    "market_cap_usd": 5200000000,
    "volume_24h_usd": 234000000,
    "fdv_usd": 5200000000
  },
  "liquidity": {
    "total_usd": 45200000,
    "top_pool": "raydium",
    "top_pool_liquidity_usd": 23000000
  },
  "safety": {
    "status": "safe",
    "risk_score": 0.0,
    "risk_level": "low",
    "flags": []
  },
  "holder_snapshot": {
    "top_10_holders_percent": 12.3,
    "concentration_risk": "low"
  },
  "ai_analysis": {
    "key_strengths": ["deep liquidity", "clean contract", "no tax mechanisms"],
    "key_risks": ["meme volatility", "retail-driven momentum"],
    "whale_signal": "none detected",
    "narrative_momentum": "positive"
  },
  "verdict": {
    "action": "buy",
    "confidence": "medium",
    "one_sentence": "Solid blue-chip meme with deep liquidity and no red flags."
  },
  "human_readable": "🔍 Token Analysis Report\n..."
}
```

**Environment variables for the x402 server:**

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SERVER_WALLET_ADDRESS` | ✅ | — | Solana wallet to receive USDC payments |
| `SERVER_PRICE_ANALYZE` | ❌ | `0.10` | Price per analysis in USDC |
| `SERVER_SOLANA_NETWORK` | ❌ | `solana` | `solana` (mainnet) or `solana-devnet` |
| `X402_FACILITATOR_URL` | ❌ | `https://facilitator.payai.network` | x402 payment facilitator (use `https://x402.org/facilitator` for devnet testing) |
| `PYTHON_API_URL` | ❌ | `http://localhost:8080` | Internal analysis service URL |
| `SERVER_PORT` | ❌ | `4022` | MCP server listen port |
| `SERVER_VERIFY_TIMEOUT_MS` | ❌ | `10000` | x402 facilitator verify timeout (ms) |
| `SERVER_SETTLE_TIMEOUT_MS` | ❌ | `10000` | x402 facilitator settle timeout (ms) |
| `SERVER_ANALYZE_TIMEOUT_MS` | ❌ | `30000` | Python analysis service timeout (ms) |
| `SOLANA_RPC_URL` | ❌ | `https://api.mainnet-beta.solana.com` | Solana RPC endpoint for `get_wallet_balance` |
| `RATE_LIMIT_WINDOW_MS` | ❌ | `900000` | Global rate limit window (ms) |
| `RATE_LIMIT_MAX` | ❌ | `100` | Max requests per window |
| `RATE_LIMIT_MCP_MAX` | ❌ | `20` | Max `/mcp` requests per minute |
| `CORS_ALLOWED_ORIGINS` | ❌ | `*` | Comma-separated allowed origins |
| `X402_AUDIT_LOG_PATH` | ❌ | `./x402-audit.log` | Path to append-only JSONL audit file (`none` to disable) |

#### Connecting from an MCP Client

The analysis server uses **StreamableHTTP** transport at `/mcp`. Any MCP-compatible client can connect to it by pointing at the server URL.

**Gemini CLI** — add to `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "dex-analysis": {
      "url": "https://your-domain.com/mcp"
    }
  }
}
```

**Claude Desktop / Cursor / other clients** that don't yet support remote HTTP servers natively — use the `mcp-remote` bridge:

```json
{
  "mcpServers": {
    "dex-analysis": {
      "command": "npx",
      "args": ["mcp-remote", "https://your-domain.com/mcp"]
    }
  }
}
```

For local development, replace the URL with `http://localhost:4022/mcp`.

> **Caveats:**
> - `analyze_token` is gated by [x402](https://x402.org) payment. The client must support the x402 payment flow (402 → pay USDC → retry with `PAYMENT-SIGNATURE` header). Most MCP clients **do not** handle this natively yet, so `analyze_token` calls will return a 402 error unless your client has x402 support.
> - `get_wallet_balance` is free and works with any MCP client immediately.
> - The server is stateless — no session initialization or persistent connection is required.

### Production Deployment

For exposing the paid analysis server to external clients with TLS, rate limiting, and container hardening.

**Prerequisites:** A server with Docker, a public domain name, and a funded Solana wallet.

**1. Configure environment:**

```bash
cp .env.production.example .env
# Edit .env — set DOMAIN, GEMINI_API_KEY, SERVER_WALLET_ADDRESS (required)
# Optionally adjust SERVER_PRICE_ANALYZE, rate limits, CORS origins
```

**2. Deploy with Caddy reverse proxy (automatic HTTPS):**

```bash
docker compose -f docker-compose.prod.yml up -d
```

This starts three services:
- **Caddy** — reverse proxy on ports 80/443, auto-provisions Let's Encrypt TLS certificates
- **analysis-server** — Express.js MCP server (internal only, not directly exposed)
- **api-service** — FastAPI Python backend (internal only)

**3. Verify:**

```bash
curl https://your-domain.com/health
# → {"status":"ok"}
```

**Security features (production compose):**

| Layer | Feature |
|-------|---------|
| **TLS** | Automatic HTTPS via Caddy + Let's Encrypt |
| **Rate limiting** | Global (100/15min) + endpoint-specific (20/min) |
| **CORS** | Configurable allowed origins |
| **Security headers** | HSTS, CSP, X-Frame-Options, X-Content-Type-Options via Helmet + Caddy |
| **Input validation** | Address format (Solana base58) + chain enum |
| **Error sanitization** | No internal details leaked to clients |
| **Container hardening** | Non-root user, read-only filesystem, no-new-privileges, resource limits |
| **Health checks** | All services monitored with restart on failure |
| **Audit logging** | Structured JSON logs for all requests + x402 payment events |

#### Payment Audit Trail

Every paid `analyze_token` request produces a chain of structured JSON audit events (to stdout and an append-only JSONL file at `X402_AUDIT_LOG_PATH`). These events enable dispute resolution by proving exactly what happened:

| Event | When | Key Fields |
|-------|------|------------|
| `x402_verify` | Payment verified with facilitator | `payment_hash`, `payer`, `payee`, `amount_microunits`, `valid`, `fallback` |
| `x402_payment` | Settlement attempted after analysis (deferred settle) | `payment_hash`, `payer`, `payee`, `amount_microunits`, `status`, `tx_hash`, `analysis_hash`, `phase` |
| `x402_settle_skipped` | Analysis failed, no charge (deferred settle only) | `payment_hash`, `payer`, `reason`, `detail`, `analysis_status` (when available) |
| `x402_response_sent` | HTTP response fully written (best-effort delivery proof) | `payment_hash`, `payer`, `analysis_hash` (when analysis succeeded), `http_status`, `content_length` |
| `x402_response_aborted` | Connection dropped before response completed | `payment_hash`, `payer`, `http_status` |
| `x402_verify_error` | Verification failed | `payment_hash`, `detail`, `facilitator_status` |
| `x402_settle_error` | Settlement failed | `payment_hash`, `detail`, `facilitator_status` |

> **Settle-first fallback**: When the facilitator does not support `/verify` (404), payment is settled _before_ analysis runs. In this case `x402_verify` will have `fallback=true`, settlement happens immediately (no `x402_payment` event with `analysis_hash`), and the `analysis_hash` appears in `x402_response_sent` after analysis completes.

**Dispute resolution**: To verify a user's claim, search the audit log by `payment_hash` or `payer` address. A complete **deferred-settle** flow shows: `x402_verify` (valid=true) → `x402_payment` (status=success, with `tx_hash` and `analysis_hash`) → `x402_response_sent` (http_status=200). A **settle-first fallback** flow shows: `x402_verify` (fallback=true) → `x402_response_sent` (http_status=200, with `analysis_hash`). If `x402_response_aborted` appears instead, the connection was dropped before the client received the data. The `analysis_hash` is a SHA-256 fingerprint of the raw analysis response from the Python API — it is only present when analysis succeeded, and matches the data returned verbatim to the client. The `payment_hash` is a SHA-256 of the raw payment header, linking all events to the same payment without storing the signature.

## CLI Options

| Option | Description |
|--------|-------------|
| `-i, --interactive` | Start interactive CLI mode |
| `-v, --verbose` | Show debug information |
| `-o, --output` | Output format (`text`, `json`, `table`; default: `table`) |
| `--stdin` | Read query from stdin |
| `--telegram-only` | Run only the Telegram bot (no CLI) |
| `--no-telegram` | Disable Telegram in interactive mode |
| `--no-rugcheck` | Disable rugcheck MCP server |
| `--no-trader` | Disable trader MCP server |
| `--portfolio` | Enable portfolio strategy scheduler |
| `--portfolio-live` | Run portfolio strategy with live execution |

## Architecture

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
    ┌────────┬───────┼───────┬────────┐
    ▼        ▼       ▼       ▼        ▼
DexScreener DexPap Rugcheck Solana  Trader
  (price)  (pools) (safety)  (RPC) (trading)
```

**Portfolio Strategy** runs as a separate subsystem:

```
PortfolioScheduler (discovery every 20min + exit checks every 40s)
    │
    ├── PortfolioDiscovery → DexScreener + Rugcheck + Gemini AI scoring
    │
    ├── PortfolioStrategy  → trailing stop updates, TP/SL/timeout checks
    │
    └── TraderExecution    → buy/sell via trader MCP
    │
    └── Database (SQLite)  → ~/.dex-bot/portfolio.db
```

## Prerequisites

- **Python 3.10+**
- **Node.js 18+** (for MCP servers)
- **Gemini API Key** (from [Google AI Studio](https://makersuite.google.com/app/apikey))
- **Telegram Bot Token** (optional, from [@BotFather](https://t.me/BotFather))

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
| [dex-trader-mcp](https://github.com/dchu3/dex-trader-mcp) | Token trading via Jupiter | Solana |

Each MCP server runs with its project root as the working directory and loads its own `.env` independently.

## License

MIT
