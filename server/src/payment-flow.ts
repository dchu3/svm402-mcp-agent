/**
 * x402 payment flow: verify → settle.
 *
 * Extracted from index.ts for testability. These functions handle the
 * two-phase payment pattern where payment is verified before work begins
 * and only settled after the service delivers a successful result.
 */

import { getCdpAuthHeaders } from "./cdp-auth.js";
import { FACILITATOR_URL, type PaymentRequirements } from "./payments.js";
import { logAuditEvent, extractPayerAddress } from "./audit-logger.js";

/** Pending payment context passed to the MCP tool handler for deferred settlement. */
export interface PendingPayment {
  header: string;
  requirements: PaymentRequirements;
}

/** Decode a base64-encoded x402 payment header into a JSON object. */
export function decodePaymentHeader(
  paymentHeader: string
): Record<string, unknown> | null {
  try {
    const decoded = Buffer.from(paymentHeader, "base64").toString("utf-8");
    const parsed: unknown = JSON.parse(decoded);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return null;
    }
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

/**
 * Validate x402Version from decoded payload.
 * Returns the numeric version (1 or 2), or null if invalid/unsupported.
 * Missing version defaults to 2 (current protocol).
 */
function validateX402Version(payload: Record<string, unknown>): number | null {
  const raw = payload.x402Version;
  if (raw === undefined || raw === null) return 2;
  if (raw === 1 || raw === 2) return raw;
  if (typeof raw === "string") {
    const n = Number(raw);
    if (n === 1 || n === 2) return n;
  }
  return null;
}

export interface VerifyResult {
  valid: boolean;
  /** Set when /verify returned 404 and we fell back to settle-first. */
  fallback?: boolean;
  /** Payer wallet address extracted from payment payload. */
  payer?: string;
  /** Reason for failure — lets callers distinguish user errors from infra errors. */
  reason?: "invalid_header" | "invalid_payment" | "facilitator_error" | "facilitator_timeout" | "facilitator_unreachable";
}

/**
 * Call the x402 facilitator to verify a payment is valid WITHOUT settling it.
 * Returns { valid: true } if the payment can be settled later.
 * When the facilitator doesn't support /verify (404), returns { valid: true, fallback: true }
 * so the caller can fall back to settle-first before running the service.
 */
export async function verifyPayment(
  paymentHeader: string,
  requirements: PaymentRequirements,
  timeoutMs: number,
): Promise<VerifyResult> {
  const paymentPayload = decodePaymentHeader(paymentHeader);
  if (!paymentPayload) {
    logAuditEvent({
      event: "x402_decode_error",
      phase: "verify",
      detail: "Failed to decode payment header as base64 JSON",
    });
    return { valid: false, reason: "invalid_header" };
  }

  const payer = extractPayerAddress(paymentPayload);

  const x402Version = validateX402Version(paymentPayload);
  if (x402Version === null) {
    logAuditEvent({
      event: "x402_decode_error",
      phase: "verify",
      detail: `Unsupported x402Version: ${String(paymentPayload.x402Version)}`,
      payer,
    });
    return { valid: false, reason: "invalid_header" };
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const verifyUrl = `${FACILITATOR_URL}/verify`;
    const authHeaders = await getCdpAuthHeaders("POST", verifyUrl);
    const res = await fetch(verifyUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({
        x402Version,
        paymentPayload,
        paymentRequirements: requirements,
      }),
      signal: controller.signal,
    });

    // Facilitator doesn't support /verify — settle first to validate on-chain
    // before running analysis (original pre-verify behavior).
    if (res.status === 404) {
      logAuditEvent({
        event: "x402_verify_fallback",
        detail: "Facilitator does not support /verify — using settle-first fallback",
        payer,
      });
      return { valid: true, fallback: true, ...(payer !== undefined ? { payer } : {}) };
    }

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      logAuditEvent({
        event: "x402_verify_error",
        detail: `Facilitator /verify returned ${res.status}`,
        facilitator_status: res.status,
        facilitator_body: text.slice(0, 500),
        payer,
      });
      return { valid: false, reason: "facilitator_error" };
    }

    let body: Record<string, unknown>;
    try {
      body = (await res.json()) as Record<string, unknown>;
    } catch {
      logAuditEvent({
        event: "x402_verify_error",
        detail: "Facilitator /verify returned non-JSON response body",
        payer,
      });
      return { valid: false, reason: "facilitator_error" };
    }
    if (body["isValid"] !== true) {
      logAuditEvent({
        event: "x402_verify_error",
        detail: `Payment verification failed: ${String(body["invalidReason"] ?? "unknown")}`,
        payer,
      });
    }
    return {
      valid: body["isValid"] === true,
      ...(payer !== undefined ? { payer } : {}),
      ...(body["isValid"] !== true ? { reason: "invalid_payment" as const } : {}),
    };
  } catch (err) {
    const isTimeout = err instanceof Error && err.name === "AbortError";
    logAuditEvent({
      event: "x402_verify_error",
      detail: err instanceof Error ? err.message : String(err),
      error_type: isTimeout ? "timeout" : "network",
      payer,
    });
    return {
      valid: false,
      ...(payer !== undefined ? { payer } : {}),
      reason: isTimeout ? "facilitator_timeout" : "facilitator_unreachable",
    } as const;
  } finally {
    clearTimeout(timeoutId);
  }
}

export type SettleStatus = "success" | "failed" | "facilitator_error" | "invalid_header" | "timeout" | "unreachable";

export interface SettleResult {
  status: SettleStatus;
  /** Transaction hash from facilitator settlement response, if available. */
  txHash?: string;
}

/**
 * Call the x402 facilitator to settle a payment and confirm it succeeded.
 * Should only be called AFTER the service has delivered a successful result.
 *
 * Returns a structured result so callers can distinguish payment rejection
 * from infrastructure failures (timeout, network errors) where settlement
 * status is unknown.
 */
export async function settlePayment(
  paymentHeader: string,
  requirements: PaymentRequirements,
  timeoutMs: number,
): Promise<SettleResult> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const paymentPayload = decodePaymentHeader(paymentHeader);
    if (!paymentPayload) {
      logAuditEvent({
        event: "x402_decode_error",
        phase: "settle",
        detail: "Failed to decode payment header as base64 JSON",
      });
      return { status: "invalid_header" };
    }

    const x402Version = validateX402Version(paymentPayload);
    if (x402Version === null) {
      logAuditEvent({
        event: "x402_decode_error",
        phase: "settle",
        detail: `Unsupported x402Version: ${String(paymentPayload.x402Version)}`,
      });
      return { status: "invalid_header" };
    }

    const settleUrl = `${FACILITATOR_URL}/settle`;
    const authHeaders = await getCdpAuthHeaders("POST", settleUrl);
    const res = await fetch(settleUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({
        x402Version,
        paymentPayload,
        paymentRequirements: requirements,
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      logAuditEvent({
        event: "x402_settle_error",
        detail: `Facilitator /settle returned ${res.status}`,
        facilitator_status: res.status,
        facilitator_body: text.slice(0, 500),
      });
      return { status: res.status >= 500 ? "facilitator_error" : "failed" };
    }

    let body: Record<string, unknown>;
    try {
      body = (await res.json()) as Record<string, unknown>;
    } catch {
      logAuditEvent({
        event: "x402_settle_error",
        detail: "Facilitator /settle returned non-JSON response body",
      });
      return { status: "facilitator_error" };
    }
    if (body["success"] !== true) {
      logAuditEvent({
        event: "x402_settle_error",
        detail: `Settlement rejected by facilitator`,
        facilitator_response: JSON.stringify(body).slice(0, 500),
      });
    }

    const txHash = typeof body["txHash"] === "string"
      ? body["txHash"]
      : typeof body["transaction"] === "string"
        ? body["transaction"]
        : undefined;

    const result: SettleResult = {
      status: body["success"] === true ? "success" : "failed",
    };
    if (txHash !== undefined) {
      result.txHash = txHash;
    }
    return result;
  } catch (err) {
    const isTimeout = err instanceof Error && err.name === "AbortError";
    logAuditEvent({
      event: "x402_settle_error",
      detail: err instanceof Error ? err.message : String(err),
      error_type: isTimeout ? "timeout" : "network",
    });
    return { status: isTimeout ? "timeout" : "unreachable" };
  } finally {
    clearTimeout(timeoutId);
  }
}
