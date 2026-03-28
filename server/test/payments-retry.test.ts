import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fetchFacilitatorFeePayer, MAX_RETRIES } from "../src/payments.js";

describe("fetchFacilitatorFeePayer retry and timeout", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    vi.useFakeTimers();
    // Silence console.log during tests
    vi.spyOn(console, "log").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("succeeds on first attempt", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          kinds: [{ network: "solana:mainnet", extra: { feePayer: "payer123" } }],
        }),
        { status: 200 }
      )
    );

    const result = await fetchFacilitatorFeePayer("solana:mainnet");
    expect(result).toBe("payer123");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("retries on transient failure and eventually succeeds", async () => {
    // 1st attempt: 500 Error
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Internal Server Error", { status: 500 })
    );
    // 2nd attempt: Success
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          kinds: [{ network: "solana:mainnet", extra: { feePayer: "payer123" } }],
        }),
        { status: 200 }
      )
    );

    const promise = fetchFacilitatorFeePayer("solana:mainnet");
    
    // Fast-forward through first back-off
    await vi.runAllTimersAsync();
    
    const result = await promise;
    expect(result).toBe("payer123");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("fails after maximum retries", async () => {
    // Mock fetch to always return 500
    vi.mocked(fetch).mockResolvedValue(
      new Response("Internal Server Error", { status: 500 })
    );

    // Attach catch immediately to prevent unhandled rejection warning
    const promise = fetchFacilitatorFeePayer("solana:mainnet").catch((err) => err);
    
    // Fast-forward through all retries
    await vi.runAllTimersAsync();
    
    const error = await promise;
    expect(error.message).toMatch(/Failed to reach facilitator/);
    expect(fetch).toHaveBeenCalledTimes(MAX_RETRIES + 1);
  });

  it("does not retry on permanent config error (missing network)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          kinds: [{ network: "other:network", extra: { feePayer: "payer123" } }],
        }),
        { status: 200 }
      )
    );

    const promise = fetchFacilitatorFeePayer("solana:mainnet").catch((err) => err);
    const error = await promise;
    expect(error.name).toBe("PermanentConfigError");
    expect(error.message).toMatch(/Facilitator does not list a feePayer/);
    
    // Should NOT retry because it's a permanent config error
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("does not retry on 401 Unauthorized (permanent auth error)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Unauthorized", { status: 401 })
    );

    const promise = fetchFacilitatorFeePayer("solana:mainnet").catch((err) => err);
    const error = await promise;
    expect(error.name).toBe("PermanentConfigError");
    expect(error.message).toMatch(/401/);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("retries on 429 Too Many Requests (transient rate limit)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Too Many Requests", { status: 429 })
    );
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          kinds: [{ network: "solana:mainnet", extra: { feePayer: "payer123" } }],
        }),
        { status: 200 }
      )
    );

    const promise = fetchFacilitatorFeePayer("solana:mainnet");
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result).toBe("payer123");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("retries on actual fetch timeout", async () => {
    // 1st attempt: Timeout
    // We mock fetch to never resolve, then advance timers by 10s
    vi.mocked(fetch).mockImplementationOnce(() => {
        return new Promise((resolve, reject) => {
            const timeout = setTimeout(() => {
                const err = new Error("The operation was aborted");
                err.name = "AbortError";
                reject(err);
            }, 10000);
            // Clean up if actually called (not strictly necessary for mock but good practice)
            return () => clearTimeout(timeout);
        });
    });
    
    // 2nd attempt: Success
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          kinds: [{ network: "solana:mainnet", extra: { feePayer: "payer123" } }],
        }),
        { status: 200 }
      )
    );

    const promise = fetchFacilitatorFeePayer("solana:mainnet");
    
    // Advance to trigger first attempt timeout
    await vi.advanceTimersByTimeAsync(10000);
    // Advance to trigger retry backoff
    await vi.runAllTimersAsync();
    
    const result = await promise;
    expect(result).toBe("payer123");
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
