/**
 * Coinbase CDP facilitator authentication.
 *
 * When CDP_API_KEY_ID and CDP_API_KEY_PRIVATE_KEY are set, generates
 * short-lived JWTs for authenticating with the Coinbase CDP x402
 * facilitator (https://api.cdp.coinbase.com/platform/v2/x402).
 *
 * When these env vars are absent, returns empty headers so the server
 * works unchanged with unauthenticated facilitators like PayAI.
 */

import { importPKCS8, importJWK, SignJWT } from "jose";
import { randomBytes } from "node:crypto";

const CDP_API_KEY_ID = process.env.CDP_API_KEY_ID ?? "";
const CDP_API_KEY_PRIVATE_KEY = (
  process.env.CDP_API_KEY_PRIVATE_KEY ?? ""
).replace(/\\n/g, "\n");

/** Whether CDP auth is configured. */
const enabled = CDP_API_KEY_ID !== "" && CDP_API_KEY_PRIVATE_KEY !== "";

/** Cached imported key — importing is expensive, the key never changes. */
let cachedKey: CryptoKey | null = null;

/** Convert a Buffer to base64url (no padding). */
function toBase64Url(buf: Buffer): string {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Import a raw base64 Ed25519 key (32 or 64 bytes) via JWK. */
async function importRawEd25519(raw: Buffer): Promise<CryptoKey> {
  if (raw.length !== 32 && raw.length !== 64) {
    throw new Error(
      `Raw Ed25519 key must be 32 or 64 bytes, got ${raw.length}`,
    );
  }
  const seed = raw.subarray(0, 32);
  const jwk: Record<string, string> = {
    kty: "OKP",
    crv: "Ed25519",
    d: toBase64Url(seed),
  };
  // If 64 bytes, the second half is the public key.
  if (raw.length === 64) {
    jwk.x = toBase64Url(raw.subarray(32));
  }
  return (await importJWK(jwk, "EdDSA")) as CryptoKey;
}

/** Import the private key, trying PEM (PKCS#8) first, then raw base64. */
async function getSigningKey(): Promise<CryptoKey> {
  if (cachedKey) return cachedKey;

  const trimmed = CDP_API_KEY_PRIVATE_KEY.trim();

  // PEM format — try PKCS#8 import (EdDSA then ES256).
  if (trimmed.startsWith("-----BEGIN")) {
    for (const alg of ["EdDSA", "ES256"]) {
      try {
        cachedKey = (await importPKCS8(trimmed, alg)) as CryptoKey;
        return cachedKey;
      } catch {
        // Key doesn't match this algorithm — try next.
      }
    }
    throw new Error(
      "CDP_API_KEY_PRIVATE_KEY PEM could not be imported as EdDSA or ES256.",
    );
  }

  // Raw base64 — assume Ed25519.
  try {
    const raw = Buffer.from(trimmed, "base64");
    cachedKey = await importRawEd25519(raw);
    return cachedKey;
  } catch (err) {
    throw new Error(
      `CDP_API_KEY_PRIVATE_KEY could not be imported. ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

/** Detect the JWT algorithm name from the imported key. */
function algorithmForKey(key: CryptoKey): string {
  const algo = key.algorithm as { name?: string } | undefined;
  if (algo?.name === "Ed25519" || algo?.name === "EdDSA") return "EdDSA";
  return "ES256";
}

/**
 * Generate a short-lived JWT for CDP facilitator authentication.
 *
 * JWT format follows the Coinbase CDP SDK convention:
 * - Header: { alg, kid: keyId, typ: "JWT", nonce: <random> }
 * - Payload: { sub: keyId, iss: "cdp", uris: ["METHOD host/path"] }
 *
 * @param method  HTTP method (GET, POST, etc.)
 * @param url     Full URL of the facilitator endpoint being called.
 *
 * Returns an `Authorization: Bearer <jwt>` header map when CDP auth is
 * configured, or an empty object when it is not.
 */
export async function getCdpAuthHeaders(
  method: string,
  url: string,
): Promise<Record<string, string>> {
  if (!enabled) return {};

  const key = await getSigningKey();
  const alg = algorithmForKey(key);
  const now = Math.floor(Date.now() / 1000);
  const nonce = randomBytes(16).toString("hex");

  // Build the URI claim in Coinbase CDP format: "METHOD host/path"
  const parsed = new URL(url);
  const uri = `${method.toUpperCase()} ${parsed.host}${parsed.pathname}`;

  const jwt = await new SignJWT({ uris: [uri] })
    .setProtectedHeader({ alg, kid: CDP_API_KEY_ID, typ: "JWT", nonce })
    .setSubject(CDP_API_KEY_ID)
    .setIssuer("cdp")
    .setNotBefore(now)
    .setIssuedAt(now)
    .setExpirationTime(now + 120)
    .sign(key);

  return { Authorization: `Bearer ${jwt}` };
}
