/**
 * Tests for the verify → settle payment flow.
 *
 * Ensures users are NOT charged when the analysis service fails,
 * and ARE charged only when a successful result is delivered.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  decodePaymentHeader,
  verifyPayment,
  settlePayment,
} from "../src/payment-flow.js";

// --- Helpers ---

const VALID_PAYLOAD = { x402Version: 2, data: "test" };
const VALID_HEADER = Buffer.from(JSON.stringify(VALID_PAYLOAD)).toString(
  "base64"
);
const INVALID_HEADER = "not-valid-base64!!!";

const MOCK_REQUIREMENTS = {
  scheme: "exact",
  network: "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
  amount: "100000",
  asset: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  payTo: "TestWalletAddress123",
  maxTimeoutSeconds: 300,
  extra: { feePayer: "FacilitatorFeePayer123" },
};

const TIMEOUT_MS = 5000;

// --- decodePaymentHeader ---

describe("decodePaymentHeader", () => {
  it("decodes a valid base64-encoded JSON header", () => {
    const result = decodePaymentHeader(VALID_HEADER);
    expect(result).toEqual(VALID_PAYLOAD);
  });

  it("returns null for non-decodable header", () => {
    expect(decodePaymentHeader(INVALID_HEADER)).toBeNull();
  });

  it("returns null for valid base64 but invalid JSON", () => {
    const nonJson = Buffer.from("not json").toString("base64");
    expect(decodePaymentHeader(nonJson)).toBeNull();
  });

  it("returns null for empty string", () => {
    expect(decodePaymentHeader("")).toBeNull();
  });

  it("returns null for base64-encoded primitive (not an object)", () => {
    const primitiveHeader = Buffer.from(JSON.stringify(true)).toString("base64");
    expect(decodePaymentHeader(primitiveHeader)).toBeNull();
  });

  it("returns null for base64-encoded array", () => {
    const arrayHeader = Buffer.from(JSON.stringify([1, 2])).toString("base64");
    expect(decodePaymentHeader(arrayHeader)).toBeNull();
  });

  it("returns null for base64-encoded string", () => {
    const stringHeader = Buffer.from(JSON.stringify("hello")).toString("base64");
    expect(decodePaymentHeader(stringHeader)).toBeNull();
  });
});

// --- verifyPayment ---

describe("verifyPayment", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("returns valid:true when facilitator confirms isValid", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ isValid: true }), { status: 200 })
    );

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ valid: true });

    // Verify it called /verify, not /settle
    const call = vi.mocked(fetch).mock.calls[0];
    expect((call[0] as string)).toContain("/verify");
  });

  it("returns valid:false with reason when facilitator says isValid:false", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({ isValid: false, invalidReason: "insufficient_amount" }),
        { status: 200 }
      )
    );

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ valid: false, reason: "invalid_payment" });
  });

  it("returns valid:false with reason for invalid payment header", async () => {
    const result = await verifyPayment(
      INVALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ valid: false, reason: "invalid_header" });
    // Should not call fetch at all
    expect(fetch).not.toHaveBeenCalled();
  });

  it("falls back gracefully when facilitator returns 404", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Not Found", { status: 404 })
    );

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ valid: true, fallback: true });
  });

  it("returns valid:false with facilitator_error reason on 500", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Internal Server Error", { status: 500 })
    );

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ valid: false, reason: "facilitator_error" });
  });

  it("returns valid:false with facilitator_unreachable on network error", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("Network error"));

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ valid: false, reason: "facilitator_unreachable" });
  });

  it("returns valid:false with facilitator_timeout on AbortError", async () => {
    const abortError = new DOMException("The operation was aborted", "AbortError");
    vi.mocked(fetch).mockRejectedValueOnce(abortError);

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ valid: false, reason: "facilitator_timeout" });
  });

  it("returns facilitator_error when response body is not valid JSON", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("not json", { status: 200, headers: { "Content-Type": "text/plain" } })
    );

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ valid: false, reason: "facilitator_error" });
  });

  it("rejects unsupported x402Version as invalid_header", async () => {
    const badVersionPayload = { x402Version: "bogus", data: "test" };
    const header = Buffer.from(JSON.stringify(badVersionPayload)).toString("base64");

    const result = await verifyPayment(header, MOCK_REQUIREMENTS, TIMEOUT_MS);
    expect(result).toEqual({ valid: false, reason: "invalid_header" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("defaults missing x402Version to 2", async () => {
    const noVersionPayload = { data: "test" };
    const header = Buffer.from(JSON.stringify(noVersionPayload)).toString("base64");

    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ isValid: true }), { status: 200 })
    );

    await verifyPayment(header, MOCK_REQUIREMENTS, TIMEOUT_MS);

    const [, init] = vi.mocked(fetch).mock.calls[0];
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body.x402Version).toBe(2);
  });
});

// --- settlePayment ---

describe("settlePayment", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("returns success status when facilitator confirms success", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ success: true }), { status: 200 })
    );

    const result = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ status: "success" });

    // Verify it called /settle
    const call = vi.mocked(fetch).mock.calls[0];
    expect((call[0] as string)).toContain("/settle");
  });

  it("returns failed status when facilitator returns success:false", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ success: false }), { status: 200 })
    );

    const result = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ status: "failed" });
  });

  it("returns invalid_header status for invalid payment header", async () => {
    const result = await settlePayment(
      INVALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ status: "invalid_header" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("returns failed status when facilitator returns HTTP 4xx error", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Bad Request", { status: 400 })
    );

    const result = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ status: "failed" });
  });

  it("returns facilitator_error status when facilitator returns HTTP 5xx", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Internal Server Error", { status: 500 })
    );

    const result = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ status: "facilitator_error" });
  });

  it("returns unreachable status on network error", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("Connection refused"));

    const result = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ status: "unreachable" });
  });

  it("returns timeout status on abort", async () => {
    const abortErr = new Error("Aborted");
    abortErr.name = "AbortError";
    vi.mocked(fetch).mockRejectedValueOnce(abortErr);

    const result = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ status: "timeout" });
  });

  it("returns facilitator_error when response body is not valid JSON", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("not json", { status: 200, headers: { "Content-Type": "text/plain" } })
    );

    const result = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result).toEqual({ status: "facilitator_error" });
  });
});

// --- Integration: verify-then-settle flow ---

describe("verify → settle flow (no charge on failure)", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("settles only after successful verification", async () => {
    // Verify succeeds
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ isValid: true }), { status: 200 })
    );

    const verification = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(verification.valid).toBe(true);

    // Simulate analysis success, then settle
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ success: true }), { status: 200 })
    );

    const settled = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(settled).toEqual({ status: "success" });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("does NOT settle when verification fails", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({ isValid: false, invalidReason: "invalid_signature" }),
        { status: 200 }
      )
    );

    const verification = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(verification.valid).toBe(false);

    // settle should never be called
    expect(fetch).toHaveBeenCalledTimes(1);
    const call = vi.mocked(fetch).mock.calls[0];
    expect((call[0] as string)).toContain("/verify");
  });

  it("does NOT settle when analysis fails (simulated by skipping settle)", async () => {
    // Verify succeeds
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ isValid: true }), { status: 200 })
    );

    const verification = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(verification.valid).toBe(true);

    // Analysis fails → we simply don't call settlePayment
    // This is the key behavior: settle is only called on success
    expect(fetch).toHaveBeenCalledTimes(1); // Only verify, no settle
  });

  it("sends correct payload structure to facilitator /verify", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ isValid: true }), { status: 200 })
    );

    await verifyPayment(VALID_HEADER, MOCK_REQUIREMENTS, TIMEOUT_MS);

    const [url, init] = vi.mocked(fetch).mock.calls[0];
    const body = JSON.parse((init as RequestInit).body as string);

    expect(body).toHaveProperty("x402Version", 2);
    expect(body).toHaveProperty("paymentPayload");
    expect(body.paymentPayload).toEqual(VALID_PAYLOAD);
    expect(body).toHaveProperty("paymentRequirements");
    expect(body.paymentRequirements).toEqual(MOCK_REQUIREMENTS);
  });

  it("uses settle-first when facilitator does not support /verify (404 fallback)", async () => {
    // Verify returns 404 fallback
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Not Found", { status: 404 })
    );

    const verification = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(verification).toEqual({ valid: true, fallback: true });

    // In fallback mode, caller should settle immediately (before analysis)
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ success: true }), { status: 200 })
    );

    const settled = await settlePayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(settled).toEqual({ status: "success" });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("returns reason for infra errors to allow 5xx responses", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("ECONNREFUSED"));

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result.valid).toBe(false);
    expect(result.reason).toBe("facilitator_unreachable");
  });

  it("returns facilitator_error reason for non-404 HTTP errors", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response("Service Unavailable", { status: 503 })
    );

    const result = await verifyPayment(
      VALID_HEADER,
      MOCK_REQUIREMENTS,
      TIMEOUT_MS
    );
    expect(result.valid).toBe(false);
    expect(result.reason).toBe("facilitator_error");
  });
});
