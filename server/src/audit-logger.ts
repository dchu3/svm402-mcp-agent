/**
 * Structured audit logger for x402 payment events.
 *
 * Writes structured JSON events to both stdout (console.log) and an
 * append-only JSONL file for durable audit trail. Each event includes
 * a timestamp, event name, and contextual fields.
 *
 * The write stream is lazily created on the first audit write to avoid
 * side effects at import time (important for tests importing sha256/extractPayerAddress).
 *
 * Environment variables:
 *   X402_AUDIT_LOG_PATH — Path to the audit log file.
 *     Default: "./x402-audit.log"
 *     Set to "none" to disable file logging.
 */

import { createHash } from "node:crypto";
import { createWriteStream, mkdirSync, type WriteStream } from "node:fs";
import { dirname } from "node:path";

let auditStream: WriteStream | null = null;
let streamFailed = false;
let streamPaused = false;

/**
 * Lazily open the audit log write stream on first use.
 * Reads X402_AUDIT_LOG_PATH at call time (not module load) so tests and
 * late configuration can disable file logging without dynamic imports.
 */
function getAuditStream(): WriteStream | null {
  if (streamFailed) return null;
  if (auditStream) return auditStream;

  const auditLogPath = process.env.X402_AUDIT_LOG_PATH ?? "./x402-audit.log";
  if (auditLogPath.toLowerCase() === "none") return null;

  try {
    mkdirSync(dirname(auditLogPath), { recursive: true });
  } catch {
    // Directory likely already exists or path is relative to cwd.
  }
  auditStream = createWriteStream(auditLogPath, { flags: "a", encoding: "utf-8" });
  auditStream.on("error", () => {
    console.error(`[audit-logger] Write stream error on ${auditLogPath} — disabling file logging`);
    streamFailed = true;
    auditStream = null;
  });
  auditStream.on("drain", () => {
    streamPaused = false;
  });
  return auditStream;
}

/** Close the audit log stream. Call on server shutdown or in test teardown. */
export function closeAuditLog(): Promise<void> {
  return new Promise((resolve) => {
    if (auditStream) {
      auditStream.end(resolve);
      auditStream = null;
    } else {
      resolve();
    }
  });
}

export interface AuditEvent {
  event: string;
  request_id?: string;
  [key: string]: unknown;
}

/**
 * Log a structured audit event to stdout and optionally to the audit log file.
 * Automatically injects an ISO-8601 timestamp.
 */
export function logAuditEvent(event: AuditEvent): void {
  const entry = {
    timestamp: new Date().toISOString(),
    ...event,
  };
  const line = JSON.stringify(entry);
  console.log(line);

  const stream = getAuditStream();
  if (stream) {
    if (streamPaused) {
      // Buffer is full — drop file write. Stdout still has the event.
      return;
    }
    try {
      const ok = stream.write(line + "\n");
      if (!ok) {
        // Internal buffer is full — pause file writes until drain.
        streamPaused = true;
      }
    } catch {
      // Stream may have been destroyed; disable to avoid repeated failures.
      streamFailed = true;
      auditStream = null;
    }
  }
}

/**
 * Compute a SHA-256 hex digest of the given string.
 * Used to fingerprint payment headers and response bodies without leaking raw data.
 */
export function sha256(input: string): string {
  return createHash("sha256").update(input, "utf-8").digest("hex");
}

/**
 * Extract the payer wallet address from a decoded x402 payment payload.
 * The field varies by protocol version; tries common locations.
 */
export function extractPayerAddress(
  payload: Record<string, unknown>
): string | undefined {
  // v2: payload.payload.authorization.from or payload.from
  const inner = payload.payload as Record<string, unknown> | undefined;
  if (inner) {
    const auth = inner.authorization as Record<string, unknown> | undefined;
    if (auth && typeof auth.from === "string") return auth.from;
    if (typeof inner.from === "string") return inner.from;
  }
  // v1 / flat: payload.from or payload.sender
  if (typeof payload.from === "string") return payload.from;
  if (typeof payload.sender === "string") return payload.sender;
  // Solana-specific: payload.signature might be set alongside from
  return undefined;
}
