# DEX Analysis MCP Server

A paid MCP server that exposes AI-powered token analysis behind a USDC paywall, plus a free wallet balance check. Each paid call requires a USDC payment on Solana via the [x402 protocol](https://x402.org).

## Tools

| Tool | Description | Price |
|------|-------------|-------|
| `analyze_token(address, chain?)` | Full AI token safety & market analysis | `$SERVER_PRICE_ANALYZE` USDC |
| `get_wallet_balance(address)` | Check SOL & USDC balance of a Solana wallet | **Free** |

### `get_wallet_balance`

A free tool for clients to check their wallet's SOL and USDC balances before paying for analysis. Returns:

```json
{
  "address": "YourWalletAddress",
  "sol_balance": 1.5,
  "usdc_balance": 10.25,
  "analysis_price_usdc": 0.10,
  "can_afford_analysis": true
}
```

## Payment flow

1. Client calls `analyze_token` → server returns **HTTP 402** with payment requirements
2. Client creates a signed USDC `TransferChecked` transaction and retries with `PAYMENT-SIGNATURE` header
3. Server **verifies** the payment with the x402 facilitator (no funds moved yet)
4. Server runs the AI analysis via the Python backend
5. **Only if analysis succeeds**: server **settles** the payment → USDC lands in `SERVER_WALLET_ADDRESS`
6. Server returns the full Gemini report

If the analysis fails (timeout, service error, etc.), the payment is **never settled** and the client is not charged — provided the facilitator supports the `/verify` endpoint. If `/verify` returns 404 the server falls back to **settle-first** mode (settles before running analysis), in which the client may be charged even on failure. The default PayAI facilitator supports `/verify`. No SOL is needed by the calling agent — the facilitator pays gas.

## Setup

### Environment variables (add to root `.env`)

```env
SERVER_WALLET_ADDRESS=YourSolanaWalletAddress   # receives USDC
SERVER_PORT=4022
SERVER_SOLANA_NETWORK=solana                    # or solana-devnet for testing
SERVER_PRICE_ANALYZE=0.10                       # USD amount charged per call
SERVER_ANALYZE_TIMEOUT_MS=30000                 # Python API timeout
SERVER_SETTLE_TIMEOUT_MS=10000                  # x402 facilitator settle timeout
SERVER_VERIFY_TIMEOUT_MS=10000                  # x402 facilitator verify timeout
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com  # for get_wallet_balance
```

### Choosing a facilitator

The server works with any x402-compatible facilitator. Set `X402_FACILITATOR_URL` in `.env`:

| Facilitator | URL | Auth |
|-------------|-----|------|
| **PayAI** (default) | `https://facilitator.payai.network` | None |
| **Coinbase CDP** | `https://api.cdp.coinbase.com/platform/v2/x402` | JWT (CDP API key) |
| **x402.org** (devnet only) | `https://x402.org/facilitator` | None |

#### Coinbase CDP setup

1. Create an API key at [portal.cdp.coinbase.com](https://portal.cdp.coinbase.com/)
2. Add to `.env`:
   ```env
   X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
   CDP_API_KEY_ID=your-key-id
   CDP_API_KEY_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nMC4CAQ...\n-----END PRIVATE KEY-----"
   ```
3. The server auto-detects the key type (Ed25519 or EC P-256) and generates short-lived JWTs for each facilitator request.

### Run with Docker Compose

```bash
docker compose up api-service analysis-server
```

The TypeScript MCP server listens on port **4022** (public). The Python analysis API runs on port **8080** (internal only).

### Run locally for development

```bash
cd server
npm install
npm run dev          # uses tsx for hot-reload
```

Requires the Python API to be running:

```bash
# From project root
source .venv/bin/activate
python -m app --http-api   # starts FastAPI on port 8080
```

## Connecting an AI agent

### TypeScript (using x402-fetch)

```typescript
import { wrapFetchWithPayment } from "x402-fetch";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const paidFetch = wrapFetchWithPayment(fetch, wallet);

const transport = new StreamableHTTPClientTransport(
  new URL("http://your-host:4022/mcp"),
  { fetch: paidFetch }
);

const client = new Client({ name: "my-agent", version: "1.0.0" });
await client.connect(transport);

const result = await client.callTool("analyze_token", {
  address: "So11111111111111111111111111111111111111112",
  chain: "solana",
});
console.log(result.content[0].text);
```

### Python (using x402-python)

```python
from x402.client import X402Client

client = X402Client(wallet_private_key="your-base58-key")
result = client.post(
    "http://your-host:4022/mcp",
    json={"method": "tools/call", "params": {"name": "analyze_token", "arguments": {"address": "..."}}}
)
print(result.json())
```

### Manual HTTP (testing)

```bash
# Free: check wallet balance before paying
curl -X POST http://localhost:4022/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_wallet_balance","arguments":{"address":"YourWalletAddress"}},"id":1}'

# Step 1: trigger 402 to see payment requirements
curl -X POST http://localhost:4022/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"analyze_token","arguments":{"address":"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}},"id":1}'

# Step 2: add PAYMENT-SIGNATURE header with your payment proof and retry
```

## Architecture

```
[AI Agent / Developer]
    │  HTTP POST /mcp  (port 4022)
    ▼
[server/src/index.ts]      ← TypeScript MCP server (payment gateway)
    │  x402 USDC payment enforced here
    │  POST http://api-service:8080/analyze
    ▼
[app/api_server.py]        ← Python FastAPI (inside api-service container)
    │
    ├── DexScreener MCP    ← price data
    ├── Rugcheck MCP       ← Solana safety
    └── Gemini API         ← AI synthesis
    ▼
Full report returned to paying client
```
