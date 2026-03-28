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

    const promise = fetchFacilitatorFeePayer("solana:mainnet");
    await expect(promise).rejects.toThrow(/Facilitator does not list a feePayer/);
    
    // Should NOT retry because it's a permanent config error
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("respects timeout and retries on AbortError", async () => {
    // 1st attempt: Timeout (AbortError)
    const timeoutError = new Error("The operation was aborted");
    timeoutError.name = "AbortError";
    vi.mocked(fetch).mockRejectedValueOnce(timeoutError);
    
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
    await vi.runAllTimersAsync();
    
    const result = await promise;
    expect(result).toBe("payer123");
    expect(fetch).toHaveBeenCalledTimes(2);
    
    // Verify timeout signal was passed
    const [, init] = vi.mocked(fetch).mock.calls[0];
    expect((init as RequestInit).signal).toBeDefined();
  });
});
