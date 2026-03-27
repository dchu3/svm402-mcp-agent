/**
 * Tests for the structured audit logger.
 *
 * Sets X402_AUDIT_LOG_PATH=none and uses dynamic import to ensure
 * no WriteStream is opened during testing.
 */

import { describe, it, expect, vi, beforeEach, afterEach, beforeAll, afterAll } from "vitest";

// Dynamic import — env var must be set before the module loads.
let sha256: typeof import("../src/audit-logger.js").sha256;
let extractPayerAddress: typeof import("../src/audit-logger.js").extractPayerAddress;
let logAuditEvent: typeof import("../src/audit-logger.js").logAuditEvent;

let prevAuditLogPath: string | undefined;

beforeAll(async () => {
  prevAuditLogPath = process.env.X402_AUDIT_LOG_PATH;
  process.env.X402_AUDIT_LOG_PATH = "none";
  const mod = await import("../src/audit-logger.js");
  sha256 = mod.sha256;
  extractPayerAddress = mod.extractPayerAddress;
  logAuditEvent = mod.logAuditEvent;
});

afterAll(() => {
  if (prevAuditLogPath === undefined) {
    delete process.env.X402_AUDIT_LOG_PATH;
  } else {
    process.env.X402_AUDIT_LOG_PATH = prevAuditLogPath;
  }
});

// --- sha256 ---

describe("sha256", () => {
  it("returns a 64-char hex string", () => {
    const hash = sha256("hello world");
    expect(hash).toHaveLength(64);
    expect(hash).toMatch(/^[0-9a-f]{64}$/);
  });

  it("produces consistent output for the same input", () => {
    expect(sha256("test")).toBe(sha256("test"));
  });

  it("produces different output for different inputs", () => {
    expect(sha256("a")).not.toBe(sha256("b"));
  });

  it("matches known SHA-256 value", () => {
    // echo -n "hello" | sha256sum
    expect(sha256("hello")).toBe(
      "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    );
  });
});

// --- extractPayerAddress ---

describe("extractPayerAddress", () => {
  it("extracts from v2 payload.authorization.from", () => {
    const payload = {
      payload: {
        authorization: { from: "WalletABC123" },
      },
    };
    expect(extractPayerAddress(payload)).toBe("WalletABC123");
  });

  it("extracts from payload.from", () => {
    const payload = {
      payload: {
        from: "WalletDEF456",
      },
    };
    expect(extractPayerAddress(payload)).toBe("WalletDEF456");
  });

  it("extracts from top-level from field", () => {
    const payload = {
      from: "WalletGHI789",
    };
    expect(extractPayerAddress(payload)).toBe("WalletGHI789");
  });

  it("extracts from top-level sender field", () => {
    const payload = {
      sender: "WalletJKL012",
    };
    expect(extractPayerAddress(payload)).toBe("WalletJKL012");
  });

  it("returns undefined when no payer field found", () => {
    expect(extractPayerAddress({})).toBeUndefined();
    expect(extractPayerAddress({ amount: "100" })).toBeUndefined();
  });

  it("prefers authorization.from over payload.from", () => {
    const payload = {
      payload: {
        authorization: { from: "AuthFrom" },
        from: "PayloadFrom",
      },
    };
    expect(extractPayerAddress(payload)).toBe("AuthFrom");
  });
});

// --- logAuditEvent ---

describe("logAuditEvent", () => {
  let consoleSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleSpy = vi.spyOn(console, "log").mockImplementation(() => {});
  });

  afterEach(() => {
    consoleSpy.mockRestore();
  });

  it("writes a valid JSON string to stdout", () => {
    logAuditEvent({ event: "test_event" });
    expect(consoleSpy).toHaveBeenCalledOnce();
    const output = consoleSpy.mock.calls[0][0] as string;
    const parsed = JSON.parse(output);
    expect(parsed.event).toBe("test_event");
    expect(parsed.timestamp).toBeDefined();
  });

  it("includes all provided fields in the output", () => {
    logAuditEvent({
      event: "x402_verify",
      request_id: "abc-123",
      payment_hash: "deadbeef",
      payer: "Wallet123",
      custom_field: 42,
    });
    const parsed = JSON.parse(consoleSpy.mock.calls[0][0] as string);
    expect(parsed.event).toBe("x402_verify");
    expect(parsed.request_id).toBe("abc-123");
    expect(parsed.payment_hash).toBe("deadbeef");
    expect(parsed.payer).toBe("Wallet123");
    expect(parsed.custom_field).toBe(42);
  });

  it("injects an ISO-8601 timestamp", () => {
    logAuditEvent({ event: "test" });
    const parsed = JSON.parse(consoleSpy.mock.calls[0][0] as string);
    expect(() => new Date(parsed.timestamp)).not.toThrow();
    expect(parsed.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });
});
