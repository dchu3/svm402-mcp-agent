import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { makeMcpServer, clearFreeToolCache } from "../src/index.js";

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

let fetchMock: any;
const originalFetch = globalThis.fetch;

beforeEach(() => {
  clearFreeToolCache();
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

const PRICE_DESC = "$0.10 USDC";
const USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const PRICE_MICROUNITS = "100000";

describe("get_wallet_balance security features", () => {
  it("enforces per-IP rate limiting (manual check)", async () => {
    // Stricter check: call 11 times from the same IP, 11th should fail.
    const server = makeMcpServer(PRICE_DESC, USDC_MINT, PRICE_MICROUNITS, undefined, undefined, "127.0.0.1");
    const client = await connectClient(server);

    // Mock successful balance responses (two calls per tool call: getBalance and getTokenAccountsByOwner)
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        jsonrpc: "2.0",
        id: 1,
        result: { value: 1000000000, tokenAmount: { uiAmount: 10 } },
      }),
    });

    // 1st to 10th calls should succeed
    for (let i = 0; i < 10; i++) {
      const result = await client.callTool({
        name: "get_wallet_balance",
        arguments: { address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
      });
      expect(result.isError).toBeFalsy();
    }

    // 11th call should fail
    const result11 = await client.callTool({
      name: "get_wallet_balance",
      arguments: { address: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
    });
    expect(result11.isError).toBeTruthy();
    expect((result11.content[0] as any).text).toContain("Too many balance checks");

    await client.close();
  });

  it("returns cached responses for the same address", async () => {
    const server = makeMcpServer(PRICE_DESC, USDC_MINT, PRICE_MICROUNITS, undefined, undefined, "127.0.0.2");
    const client = await connectClient(server);

    // Mock two successful balance responses
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        jsonrpc: "2.0",
        id: 1,
        result: { value: 2000000000 }, 
      }),
    });

    const address = "So11111111111111111111111111111111111111112";
    
    // First call: should trigger 2 fetches
    const result1 = await client.callTool({
      name: "get_wallet_balance",
      arguments: { address },
    });
    expect(result1.isError).toBeFalsy();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    
    // Second call: should be cached (0 extra fetches)
    const result2 = await client.callTool({
      name: "get_wallet_balance",
      arguments: { address },
    });
    expect(result2.isError).toBeFalsy();
    expect(fetchMock).toHaveBeenCalledTimes(2); // No new fetch
    
    const textBlock = (result2.content as Array<{ type: string; text: string }>)[0];
    const parsed = JSON.parse(textBlock.text);
    expect(parsed._cached).toBe(true);

    await client.close();
  });

  it("coalesces concurrent requests for the same address", async () => {
    const server = makeMcpServer(PRICE_DESC, USDC_MINT, PRICE_MICROUNITS, undefined, undefined, "127.0.0.3");
    const client = await connectClient(server);

    // Controlled mock that takes some time
    fetchMock.mockImplementation(() => new Promise((resolve) => {
      setTimeout(() => {
        resolve({
          ok: true,
          json: async () => ({
            jsonrpc: "2.0",
            id: 1,
            result: { value: 3000000000 }, 
          }),
        });
      }, 50);
    }));

    const address = "So22222222222222222222222222222222222222222";
    
    // Fire off two concurrent requests
    const [result1, result2] = await Promise.all([
      client.callTool({
        name: "get_wallet_balance",
        arguments: { address },
      }),
      client.callTool({
        name: "get_wallet_balance",
        arguments: { address },
      }),
    ]);

    expect(result1.isError).toBeFalsy();
    expect(result2.isError).toBeFalsy();
    expect(fetchMock).toHaveBeenCalledTimes(2); // Only one set of fetches for both

    await client.close();
  });
});
