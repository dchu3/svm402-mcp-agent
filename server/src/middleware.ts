import rateLimit from "express-rate-limit";
import cors from "cors";
import helmet from "helmet";
import crypto from "node:crypto";
import type { Request, Response, NextFunction } from "express";

// --- Rate Limiting ---
// Global rate limiter
export const globalRateLimiter = rateLimit({
  windowMs: parseInt(process.env.RATE_LIMIT_WINDOW_MS ?? "900000", 10), // 15 min
  max: parseInt(process.env.RATE_LIMIT_MAX ?? "100", 10),
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: "Too many requests, please try again later" },
});

// Stricter rate limiter for the paid /mcp endpoint
export const mcpRateLimiter = rateLimit({
  windowMs: 60_000, // 1 minute
  max: parseInt(process.env.RATE_LIMIT_MCP_MAX ?? "20", 10),
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: "Too many requests to analysis endpoint, please try again later" },
});

// --- CORS ---
export function buildCorsMiddleware() {
  const originsEnv = process.env.CORS_ALLOWED_ORIGINS;
  const origin = originsEnv
    ? originsEnv.split(",").map((o) => o.trim())
    : "*";
  return cors({
    origin,
    methods: ["POST", "OPTIONS"],
    allowedHeaders: ["Content-Type", "PAYMENT-SIGNATURE", "X-PAYMENT"],
    exposedHeaders: ["PAYMENT-REQUIRED"],
  });
}

// --- Security Headers (helmet) ---
export const securityHeaders = helmet({
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'none'"],
      frameAncestors: ["'none'"],
    },
  },
  hsts: { maxAge: 31536000, includeSubDomains: true },
  frameguard: { action: "deny" },
});

// --- Request ID ---
export function requestId(req: Request, _res: Response, next: NextFunction): void {
  const id = req.headers["x-request-id"] as string | undefined
    ?? crypto.randomUUID();
  req.headers["x-request-id"] = id;
  next();
}

// --- Request Logging ---
export function requestLogger(req: Request, res: Response, next: NextFunction): void {
  const start = Date.now();
  const reqId = req.headers["x-request-id"] as string;

  res.on("finish", () => {
    const log: Record<string, unknown> = {
      timestamp: new Date().toISOString(),
      method: req.method,
      path: req.path,
      status: res.statusCode,
      duration_ms: Date.now() - start,
      ip: req.ip,
      request_id: reqId,
    };
    // Use console.log for JSON structured logging (stdout)
    console.log(JSON.stringify(log));
  });

  next();
}

// --- Input Validation ---
export const SOLANA_ADDRESS_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const ALLOWED_CHAINS = new Set(["solana"]);

export interface ValidationError {
  status: number;
  error: string;
}

/**
 * Validate analyze_token arguments extracted from the MCP request body.
 * Returns null if valid, or a ValidationError object if invalid.
 */
export function validateAnalyzeArgs(
  args: Record<string, unknown> | undefined
): ValidationError | null {
  if (!args || typeof args !== "object") {
    return { status: 400, error: "Missing tool arguments" };
  }

  const address = args["address"];
  if (typeof address !== "string" || address.trim() === "") {
    return { status: 400, error: "Invalid or missing address parameter" };
  }

  const trimmed = address.trim();
  if (!SOLANA_ADDRESS_RE.test(trimmed)) {
    return { status: 400, error: "Invalid address format" };
  }

  const chain = args["chain"];
  if (chain !== undefined && chain !== null) {
    if (typeof chain !== "string" || !ALLOWED_CHAINS.has(chain.toLowerCase())) {
      return { status: 400, error: "Invalid chain parameter" };
    }
  }

  return null;
}

// --- Global Error Handler ---
export function globalErrorHandler(
  err: Error,
  _req: Request,
  res: Response,
  _next: NextFunction
): void {
  console.error("Unhandled error:", err.message);
  if (!res.headersSent) {
    res.status(500).json({ error: "Internal server error" });
  }
}
