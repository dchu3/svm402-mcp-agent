/**
 * Tests for MCP tool output format.
 *
 * Verifies that analyze_token and get_wallet_balance return proper
 * JSON via structuredContent, and that outputSchema is declared for
 * agent discoverability.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { makeMcpServer } from "../src/index.js";

// --- Helpers ---

/** A realistic AnalyzeResponse JSON matching the Python API schema. */
const MOCK_ANALYSIS_RESPONSE = {
  token: "BONK",
  chain: "solana",
  address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
  timestamp: "2026-03-21T19:00:00Z",
  price_data: {
    price_usd: 0.00002345,
    change_24h_percent: 12.5,
    market_cap_usd: 1500000000,
    volume_24h_usd: 45000000,
    fdv_usd: 2000000000,
  },
  liquidity: {
    total_usd: 8000000,
    top_pool: "BONK/SOL",
    top_pool_liquidity_usd: 5000000,
    lp_locked_pct: null,
  },
  safety: {
    status: "good",
    risk_score: 15,
    risk_level: "low",
    flags: ["verified_creator"],
  },
  holder_snapshot: {
    top_10_holders_percent: 22.5,
    concentration_risk: "moderate",
  },
  wash_trading: {
    manipulation_score: 1,
    manipulation_level: "clean",
    unique_wallets: 42,
    total_transactions_sampled: 100,
    repeat_buyers: [],
    flags: [],
  },
  ai_analysis: {
    key_strengths: ["Strong community", "High liquidity"],
    key_risks: ["Meme token volatility"],
    whale_signal: "neutral",
    narrative_momentum: "positive",
  },
  verdict: {
    action: "hold",
    confidence: "medium",
    one_sentence: "BONK is a well-established meme token with decent fundamentals.",
  },
  human_readable: "# BONK Analysis\n\nLooks good.",
};

/** Connect an MCP client to a server via in-memory transport. */
async function connectClient(
  server: ReturnType<typeof makeMcpServer>,
) {
  const [clientTransport, serverTransport] =
    InMemoryTransport.createLinkedPair();

  await server.connect(serverTransport);

  const client = new Client({ name: "test-client", version: "1.0.0" });
  await client.connect(clientTransport);

  return client;
}

// --- Mock global fetch ---

let fetchMock: ReturnType<typeof vi.fn>;
const originalFetch = globalThis.fetch;

beforeEach(() => {
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

// --- Tests ---

describe("tool output schemas", () => {
  it("lists analyze_token with an outputSchema", async () => {
    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const { tools } = await client.listTools();
    const analyzeTool = tools.find((t) => t.name === "analyze_token");

    expect(analyzeTool).toBeDefined();
    expect(analyzeTool!.outputSchema).toBeDefined();
    expect(analyzeTool!.outputSchema!.type).toBe("object");
    expect(analyzeTool!.outputSchema!.properties).toHaveProperty("token");
    expect(analyzeTool!.outputSchema!.properties).toHaveProperty("price_data");
    expect(analyzeTool!.outputSchema!.properties).toHaveProperty("verdict");
    expect(analyzeTool!.outputSchema!.properties).toHaveProperty("human_readable");

    await client.close();
  });

  it("lists get_wallet_balance with an outputSchema", async () => {
    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const { tools } = await client.listTools();
    const balanceTool = tools.find((t) => t.name === "get_wallet_balance");

    expect(balanceTool).toBeDefined();
    expect(balanceTool!.outputSchema).toBeDefined();
    expect(balanceTool!.outputSchema!.type).toBe("object");
    expect(balanceTool!.outputSchema!.properties).toHaveProperty("sol_balance");
    expect(balanceTool!.outputSchema!.properties).toHaveProperty("usdc_balance");
    expect(balanceTool!.outputSchema!.properties).toHaveProperty("can_afford_analysis");

    await client.close();
  });
});

describe("analyze_token output format", () => {
  it("returns structuredContent with valid JSON on success", async () => {
    const rawJson = JSON.stringify(MOCK_ANALYSIS_RESPONSE);

    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: async () => rawJson,
    });

    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const result = await client.callTool({
      name: "analyze_token",
      arguments: { address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
    });

    // content[0].text should contain the raw JSON string
    expect(result.content).toBeInstanceOf(Array);
    const textBlock = (result.content as Array<{ type: string; text: string }>)[0];
    expect(textBlock.type).toBe("text");
    const parsed = JSON.parse(textBlock.text);
    expect(parsed.token).toBe("BONK");
    expect(parsed.verdict.action).toBe("hold");

    // structuredContent should be the parsed object
    expect(result.structuredContent).toBeDefined();
    expect(result.structuredContent).toMatchObject({
      token: "BONK",
      chain: "solana",
      verdict: { action: "hold", confidence: "medium" },
    });

    await client.close();
  });

  it("returns isError without structuredContent on API failure", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      text: async () => '{"detail":"Internal error"}',
    });

    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const result = await client.callTool({
      name: "analyze_token",
      arguments: { address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
    });

    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();
    const textBlock = (result.content as Array<{ type: string; text: string }>)[0];
    expect(textBlock.text).toContain("error");

    await client.close();
  });

  it("returns isError when Python API returns non-JSON", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: async () => "<html>Bad Gateway</html>",
    });

    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const result = await client.callTool({
      name: "analyze_token",
      arguments: { address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
    });

    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();
    const textBlock = (result.content as Array<{ type: string; text: string }>)[0];
    expect(textBlock.text).toContain("invalid response");

    await client.close();
  });

  it("returns isError when Python API returns JSON null", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: async () => "null",
    });

    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const result = await client.callTool({
      name: "analyze_token",
      arguments: { address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
    });

    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();

    await client.close();
  });

  it("returns isError when Python API returns valid JSON missing required fields", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ detail: "Something went wrong" }),
    });

    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const result = await client.callTool({
      name: "analyze_token",
      arguments: { address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
    });

    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();
    const textBlock = (result.content as Array<{ type: string; text: string }>)[0];
    expect(textBlock.text).toContain("invalid response");

    await client.close();
  });

  it("returns isError when Python API returns a JSON string", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: async () => '"just a string"',
    });

    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const result = await client.callTool({
      name: "analyze_token",
      arguments: { address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
    });

    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();

    await client.close();
  });
});

describe("get_wallet_balance output format", () => {
  it("returns structuredContent with balance data", async () => {
    // Mock getBalance RPC
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ jsonrpc: "2.0", id: 1, result: { value: 5_000_000_000 } }),
    });
    // Mock getTokenAccountsByOwner RPC
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jsonrpc: "2.0",
        id: 1,
        result: {
          value: [
            {
              account: {
                data: {
                  parsed: {
                    info: { tokenAmount: { uiAmount: 25.5 } },
                  },
                },
              },
            },
          ],
        },
      }),
    });

    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const result = await client.callTool({
      name: "get_wallet_balance",
      arguments: { address: "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM" },
    });

    // content[0].text should be valid JSON
    const textBlock = (result.content as Array<{ type: string; text: string }>)[0];
    expect(textBlock.type).toBe("text");
    const parsed = JSON.parse(textBlock.text);
    expect(parsed.sol_balance).toBe(5);
    expect(parsed.usdc_balance).toBe(25.5);

    // structuredContent should match
    expect(result.structuredContent).toBeDefined();
    expect(result.structuredContent).toMatchObject({
      sol_balance: 5,
      usdc_balance: 25.5,
      can_afford_analysis: true,
    });

    await client.close();
  });

  it("returns isError for invalid address without structuredContent", async () => {
    const server = makeMcpServer("$0.10 USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "100000");
    const client = await connectClient(server);

    const result = await client.callTool({
      name: "get_wallet_balance",
      arguments: { address: "not-a-valid-address!!!" },
    });

    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();

    await client.close();
  });
});
