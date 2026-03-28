/**
 * x402 payment requirements for the DEX analysis tool.
 *
 * The x402 protocol (https://x402.org) enables machine-to-machine HTTP payments.
 * Clients must include a signed USDC TransferChecked transaction in the
 * PAYMENT-SIGNATURE header (v2) or X-PAYMENT header (v1); the facilitator
 * verifies it up front and settles on-chain only after a successful response.
 *
 * Default facilitator: PayAI (mainnet, no auth required).
 * Coinbase CDP alternative: https://api.cdp.coinbase.com/platform/v2/x402 (requires API keys)
 * For testing on devnet, set X402_FACILITATOR_URL=https://x402.org/facilitator
 *
 * Payment flow:
 *   1. Client calls tool → server returns 402 + payment requirements
 *   2. Client builds partial USDC tx → retries with PAYMENT-SIGNATURE header
 *   3. Server calls facilitator /verify → confirms payment is valid (no funds moved)
 *   4. Server runs analysis via Python backend
 *   5. On success: server calls facilitator /settle → USDC lands in SERVER_WALLET_ADDRESS
 *   6. On failure: settlement is skipped → client is not charged
 *
 * NOTE: If the facilitator does not support /verify (returns 404), the server
 * falls back to settle-first behavior (settles before running analysis). In
 * that mode the client may be charged even if analysis subsequently fails.
 * Use a facilitator that supports /verify (e.g., PayAI) for the full guarantee.
 */

import { getCdpAuthHeaders } from "./cdp-auth.js";

export const FACILITATOR_URL =
  process.env.X402_FACILITATOR_URL ?? "https://facilitator.payai.network";

/** USDC mint on Solana mainnet (6 decimals). */
export const USDC_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
/** USDC mint on Solana devnet. */
export const USDC_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";

/** Solana CAIP-2 chain identifiers. */
const SOLANA_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
const SOLANA_DEVNET_CAIP2 = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1";

/**
 * Accepted SERVER_SOLANA_NETWORK values and their canonical identifiers.
 *
 * Accepts both legacy v1 names ("solana", "solana-devnet") and CAIP-2 IDs.
 * The `caip2` identifier is used in payment requirements (x402 v2 protocol)
 * and for facilitator API calls.
 */
const NETWORK_MAP: Record<
  string,
  { caip2: string; v1Name: string; devnet: boolean }
> = {
  solana: { caip2: SOLANA_MAINNET_CAIP2, v1Name: "solana", devnet: false },
  "solana-devnet": {
    caip2: SOLANA_DEVNET_CAIP2,
    v1Name: "solana-devnet",
    devnet: true,
  },
  [SOLANA_MAINNET_CAIP2]: {
    caip2: SOLANA_MAINNET_CAIP2,
    v1Name: "solana",
    devnet: false,
  },
  [SOLANA_DEVNET_CAIP2]: {
    caip2: SOLANA_DEVNET_CAIP2,
    v1Name: "solana-devnet",
    devnet: true,
  },
};

/** Wire-format payment requirements object (x402 v2 spec §5.1.2). */
export interface PaymentRequirements {
  scheme: string;
  network: string;
  amount: string;
  asset: string;
  payTo: string;
  maxTimeoutSeconds: number;
  extra: { feePayer: string } | null;
}

/** Resource metadata included in the top-level 402 response (x402 v2 spec §5.1.2). */
export interface ResourceInfo {
  url: string;
  description: string;
  mimeType: string;
}

/** Full 402 response body (x402 v2 spec §5.1.1). */
export interface PaymentRequiredBody {
  x402Version: 2;
  error: string;
  resource: ResourceInfo;
  accepts: PaymentRequirements[];
}

/** Result of buildPaymentConfig — everything needed to construct 402 responses. */
export interface PaymentConfig {
  resource: ResourceInfo;
  accepts: PaymentRequirements[];
  priceDescription: string;
}

export function formatUsdFromMicrounits(amountMicrounits: bigint): string {
  const dollars = amountMicrounits / 1_000_000n;
  const micros = (amountMicrounits % 1_000_000n).toString().padStart(6, "0");
  const microsTrimmed = micros.replace(/0+$/, "");
  // Always show at least 2 decimal places for currency display
  const decimals = microsTrimmed.padEnd(2, "0");
  return `${dollars.toString()}.${decimals}`;
}

export function toUsdcMicrounits(priceStr: string): bigint {
  const normalized = priceStr.trim();
  if (!/^\d+(\.\d{1,6})?$/.test(normalized)) {
    throw new Error("SERVER_PRICE_ANALYZE must be a positive number");
  }
  const [whole, fractional = ""] = normalized.split(".");
  const fractionalPadded = (fractional + "000000").slice(0, 6);
  const amount = BigInt(whole) * 1_000_000n + BigInt(fractionalPadded);
  if (amount <= 0n) {
    throw new Error("SERVER_PRICE_ANALYZE must be a positive number");
  }
  return amount;
}

/**
 * Fetch the facilitator's fee-payer address for the given network.
 *
 * The x402 SVM SDK requires `extra.feePayer` in payment requirements — this is
 * the Solana address that pays transaction fees when the facilitator settles.
 *
 * Retries up to {@link MAX_RETRIES} times with exponential back-off so
 * transient facilitator outages don't crash the server on startup.
 */
export const MAX_RETRIES = 3;
export const INITIAL_BACKOFF_MS = 2000;

export class PermanentConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PermanentConfigError";
  }
}

export async function fetchFacilitatorFeePayer(network: string): Promise<string> {
  let lastError: Error | undefined;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      const delayMs = INITIAL_BACKOFF_MS * 2 ** (attempt - 1);
      console.log(
        `Retrying facilitator /supported (attempt ${attempt + 1}/${MAX_RETRIES + 1}) in ${delayMs}ms...`,
      );
      await new Promise((r) => setTimeout(r, delayMs));
    }

    try {
      const supportedUrl = `${FACILITATOR_URL}/supported`;
      let authHeaders: Record<string, string>;
      try {
        authHeaders = await getCdpAuthHeaders("GET", supportedUrl);
      } catch (err) {
        throw new PermanentConfigError(
          `Failed to generate CDP authentication headers: ${err instanceof Error ? err.message : String(err)}`,
        );
      }

      const res = await fetch(supportedUrl, {
        headers: { ...authHeaders },
        signal: AbortSignal.timeout(10000),
      });

      if (!res.ok) {
        const errorMsg = `Failed to fetch facilitator /supported (${res.status}): ${res.statusText}`;
        // Retry on 5xx (server errors) or 429 (rate limit).
        // Treat other 4xx (401, 403, 404) as permanent config/auth errors.
        if (res.status >= 500 || res.status === 429) {
          throw new Error(errorMsg);
        } else {
          throw new PermanentConfigError(errorMsg);
        }
      }

      const data = (await res.json()) as {
        kinds: Array<{
          scheme: string;
          network: string;
          extra?: { feePayer?: string };
        }>;
        signers?: Record<string, string[]>;
      };

      // Try to find an exact match for the configured network in the supported kinds.
      for (const kind of data.kinds) {
        if (kind.network === network && kind.extra?.feePayer) {
          return kind.extra.feePayer;
        }
      }

      // Fall back to the wildcard signer list (e.g., "solana:*").
      if (data.signers) {
        for (const [pattern, addresses] of Object.entries(data.signers)) {
          if (
            pattern.endsWith(":*") &&
            network.startsWith(pattern.slice(0, -2))
          ) {
            if (addresses.length > 0) {
              return addresses[0];
            }
          }
        }
      }

      // No match is a permanent config error — don't retry.
      throw new PermanentConfigError(
        `Facilitator does not list a feePayer for network "${network}". ` +
          `Check SERVER_SOLANA_NETWORK and X402_FACILITATOR_URL.`,
      );
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err));
      if (lastError instanceof PermanentConfigError) {
        throw lastError;
      }
      // Continue loop for other errors (network errors, timeouts, 5xx/429)
    }
  }

  throw new Error(
    `Failed to reach facilitator after ${MAX_RETRIES + 1} attempts: ${lastError?.message}`,
  );
}

/** Build payment config from environment variables. */
export async function buildPaymentConfig(): Promise<PaymentConfig> {
  const walletAddress = process.env.SERVER_WALLET_ADDRESS;
  if (!walletAddress) {
    throw new Error("SERVER_WALLET_ADDRESS must be set");
  }

  const priceInput = process.env.SERVER_PRICE_ANALYZE ?? "0.10";
  const amountMicrounits = toUsdcMicrounits(priceInput);
  const priceDisplay = formatUsdFromMicrounits(amountMicrounits);
  const amountRaw = amountMicrounits.toString();

  const rawNetwork = process.env.SERVER_SOLANA_NETWORK ?? "solana";
  const resolved = NETWORK_MAP[rawNetwork];
  if (!resolved) {
    throw new Error(
      `Unsupported SERVER_SOLANA_NETWORK "${rawNetwork}". ` +
        `Accepted values: ${Object.keys(NETWORK_MAP).join(", ")}`,
    );
  }
  const asset = resolved.devnet ? USDC_DEVNET : USDC_MAINNET;

  const feePayer = await fetchFacilitatorFeePayer(resolved.caip2);
  console.log(`Facilitator feePayer for ${resolved.caip2}: ${feePayer}`);

  return {
    resource: {
      url: "/mcp",
      description: `DEX AI token analysis — $${priceDisplay} USDC`,
      mimeType: "application/json",
    },
    accepts: [
      {
        scheme: "exact",
        network: resolved.caip2,
        amount: amountRaw,
        payTo: walletAddress,
        maxTimeoutSeconds: 300,
        asset,
        extra: { feePayer },
      },
    ],
    priceDescription: `DEX AI token analysis — $${priceDisplay} USDC`,
  };
}

/** Build the full 402 response body and base64-encoded header value. */
export function buildPaymentRequiredResponse(
  config: PaymentConfig,
  error: string,
): { body: PaymentRequiredBody; headerValue: string } {
  const body: PaymentRequiredBody = {
    x402Version: 2,
    error,
    resource: config.resource,
    accepts: config.accepts,
  };
  const headerValue = Buffer.from(JSON.stringify(body)).toString("base64");
  return { body, headerValue };
}
