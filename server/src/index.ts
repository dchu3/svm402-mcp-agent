/**
 * DEX Analysis MCP Server — payment gateway for the Gemini token analysis service.
 *
 * Exposes two MCP tools:
 *   - `analyze_token` — paid (x402 USDC paywall), full AI-powered token analysis
 *   - `get_wallet_balance` — free, check SOL/USDC balance before paying
 *
 * The analysis itself is delegated to the Python FastAPI service (app/api_server.py).
 *
 * Usage (as MCP client, e.g. Claude Desktop or AI agent):
 *   transport: StreamableHTTP
 *   url: http://your-host:4022/mcp
 *
 * The server operates in stateless mode: each HTTP request is a complete
 * MCP exchange, which allows x402 payment verification per call.
 */

import "dotenv/config";
import express from "express";
import type { Request, Response } from "express";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import {
  buildPaymentConfig,
  buildPaymentRequiredResponse,
  FACILITATOR_URL,
} from "./payments.js";
import {
  verifyPayment,
  settlePayment,
  type PendingPayment,
} from "./payment-flow.js";
import {
  globalRateLimiter,
  mcpRateLimiter,
  buildCorsMiddleware,
  securityHeaders,
  requestId,
  requestLogger,
  validateAnalyzeArgs,
  globalErrorHandler,
  SOLANA_ADDRESS_RE,
} from "./middleware.js";
import { logAuditEvent, sha256 } from "./audit-logger.js";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8080";
const INTERNAL_API_SECRET = process.env.INTERNAL_API_SECRET ?? "";
const SERVER_PORT = parseInt(process.env.SERVER_PORT ?? "4022", 10);
const ANALYZE_TIMEOUT_MS = parseTimeoutMs(
  process.env.SERVER_ANALYZE_TIMEOUT_MS,
  30000,
  "SERVER_ANALYZE_TIMEOUT_MS"
);
const SETTLE_TIMEOUT_MS = parseTimeoutMs(
  process.env.SERVER_SETTLE_TIMEOUT_MS,
  10000,
  "SERVER_SETTLE_TIMEOUT_MS"
);
const VERIFY_TIMEOUT_MS = parseTimeoutMs(
  process.env.SERVER_VERIFY_TIMEOUT_MS,
  10000,
  "SERVER_VERIFY_TIMEOUT_MS"
);
const SOLANA_RPC_URL =
  process.env.SOLANA_RPC_URL ?? "https://api.mainnet-beta.solana.com";
const BALANCE_TIMEOUT_MS = parseTimeoutMs(
  process.env.SERVER_BALANCE_TIMEOUT_MS,
  10000,
  "SERVER_BALANCE_TIMEOUT_MS"
);

// --- In-memory cache for free tools ---
interface CachedBalance {
  sol_balance: number;
  usdc_balance: number;
  expiresAt: number;
}
const balanceCache = new Map<string, CachedBalance>();
const BALANCE_CACHE_TTL_MS = 60_000; // 60 seconds

// --- Per-IP rate limiting for free tools (manual) ---
interface IpUsage {
  count: number;
  resetAt: number;
}
const freeToolIpLimits = new Map<string, IpUsage>();
const FREE_TOOL_MAX_PER_WINDOW = 10;
const FREE_TOOL_WINDOW_MS = 60_000; // 1 minute

function parseTimeoutMs(
  rawValue: string | undefined,
  defaultValue: number,
  envName: string
): number {
  if (rawValue === undefined || rawValue.trim() === "") {
    return defaultValue;
  }
  const parsed = Number.parseInt(rawValue, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${envName} must be a positive integer`);
  }
  return parsed;
}

/** Make a JSON-RPC call to the Solana cluster. */
async function solanaRpc<T>(
  method: string,
  params: unknown[],
  timeoutMs: number = BALANCE_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(SOLANA_RPC_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
      signal: controller.signal,
    });
    const body = (await res.json()) as { result?: T; error?: { message: string } };
    if (body.error) {
      throw new Error(`Solana RPC error: ${body.error.message}`);
    }
    return body.result as T;
  } finally {
    clearTimeout(timeoutId);
  }
}

/** Payment context for audit logging within MCP tool handlers. */
export interface PaymentAuditContext {
  requestId?: string;
  ip?: string;
  paymentHash?: string;
  payer?: string;
  payee?: string;
  amountMicrounits?: string;
  /** SHA-256 of the raw analysis response from the Python API. Set only on successful analysis. */
  analysisHash?: string;
}

/** Create a fresh McpServer for each stateless request. */
export function makeMcpServer(
  priceDescription: string,
  usdcMint: string,
  analysisPriceMicrounits: string,
  pendingPayment?: PendingPayment,
  auditCtx?: PaymentAuditContext,
  clientIp?: string,
): McpServer {
  const server = new McpServer({ name: "dex-analysis", version: "1.0.0" });

  server.registerTool(
    "analyze_token",
    {
      description:
        "Full AI-powered token safety and market analysis using Gemini. " +
        "Returns a structured JSON report with price_data, liquidity, safety, " +
        "holder_snapshot, ai_analysis (key_strengths, key_risks, whale_signal, " +
        "narrative_momentum), verdict (action, confidence, one_sentence), and " +
        `human_readable summary. ${priceDescription} (x402 protocol).`,
      inputSchema: {
        address: z.string().describe("Token contract address"),
        chain: z
          .string()
          .optional()
          .describe(
            "Blockchain network (currently only 'solana' is supported, auto-detected if omitted)"
          ),
      },
      outputSchema: {
        token: z.string(),
        chain: z.string(),
        address: z.string(),
        timestamp: z.string(),
        price_data: z.object({
          price_usd: z.number().nullable(),
          change_24h_percent: z.number().nullable(),
          market_cap_usd: z.number().nullable(),
          volume_24h_usd: z.number().nullable(),
          fdv_usd: z.number().nullable(),
        }),
        liquidity: z.object({
          total_usd: z.number().nullable(),
          top_pool: z.string().nullable(),
          top_pool_liquidity_usd: z.number().nullable(),
          lp_locked_pct: z.number().nullable(),
        }),
        safety: z.object({
          status: z.string(),
          risk_score: z.number().nullable(),
          risk_level: z.string(),
          flags: z.array(z.string()),
        }),
        holder_snapshot: z
          .object({
            top_10_holders_percent: z.number().nullable(),
            concentration_risk: z.string(),
          })
          .nullable(),
        ai_analysis: z.object({
          key_strengths: z.array(z.string()),
          key_risks: z.array(z.string()),
          whale_signal: z.string(),
          narrative_momentum: z.string(),
        }),
        verdict: z.object({
          action: z.string(),
          confidence: z.string(),
          one_sentence: z.string(),
        }),
        human_readable: z.string(),
      },
    },
    async ({ address, chain }) => {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), ANALYZE_TIMEOUT_MS);
      try {
        const internalHeaders: Record<string, string> = {
          "Content-Type": "application/json",
        };
        if (INTERNAL_API_SECRET) {
          internalHeaders["X-Internal-API-Key"] = INTERNAL_API_SECRET;
        }
        if (auditCtx?.requestId) {
          internalHeaders["X-Request-Id"] = auditCtx.requestId;
        }
        const res = await fetch(`${PYTHON_API_URL}/analyze`, {
          method: "POST",
          headers: internalHeaders,
          body: JSON.stringify({ address, chain: chain ?? null }),
          signal: controller.signal,
        });

        let rawBody = "";
        try {
          rawBody = await res.text();
        } catch {
          rawBody = "";
        }
        let data: Record<string, unknown> = {};
        let jsonValid = false;
        try {
          const parsed = JSON.parse(rawBody);
          if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
            data = parsed as Record<string, unknown>;
            jsonValid = true;
          }
        } catch {
          // jsonValid stays false
        }

        if (!res.ok) {
          // Analysis failed — do NOT settle payment
          if (pendingPayment) {
            logAuditEvent({
              event: "x402_settle_skipped",
              request_id: auditCtx?.requestId,
              tool: "analyze_token",
              reason: "analysis_error",
              analysis_status: res.status,
              detail: String(data["detail"] ?? "unknown"),
              payment_hash: auditCtx?.paymentHash,
              payer: auditCtx?.payer,
              amount_microunits: auditCtx?.amountMicrounits,
            });
          }
          return {
            content: [
              {
                type: "text",
                text: "The analysis service encountered an error. Please try again later.",
              },
            ],
            isError: true,
          };
        }

        // Validate that the Python API returned a valid JSON object with
        // required top-level fields. This prevents settling payment for
        // responses that would later fail outputSchema validation.
        const hasRequiredFields = jsonValid
          && typeof data["token"] === "string"
          && typeof data["chain"] === "string"
          && typeof data["address"] === "string";
        if (!hasRequiredFields) {
          if (pendingPayment) {
            logAuditEvent({
              event: "x402_settle_skipped",
              request_id: auditCtx?.requestId,
              tool: "analyze_token",
              reason: "invalid_json_response",
              payment_hash: auditCtx?.paymentHash,
              payer: auditCtx?.payer,
              amount_microunits: auditCtx?.amountMicrounits,
            });
          }
          return {
            content: [
              {
                type: "text",
                text: "The analysis service returned an invalid response. Please try again later.",
              },
            ],
            isError: true,
          };
        }

        // Analysis succeeded — hash the raw response for audit trail.
        // This is the hash of the Python API response body, which is returned
        // verbatim to the client on the success path.
        const analysisHash = rawBody ? sha256(rawBody) : undefined;
        if (auditCtx && analysisHash) {
          auditCtx.analysisHash = analysisHash;
        }

        // Now settle payment before returning data
        if (pendingPayment) {
          const settleResult = await settlePayment(
            pendingPayment.header,
            pendingPayment.requirements,
            SETTLE_TIMEOUT_MS,
          );
          logAuditEvent({
            event: "x402_payment",
            request_id: auditCtx?.requestId,
            tool: "analyze_token",
            payment_hash: auditCtx?.paymentHash,
            payer: auditCtx?.payer,
            payee: auditCtx?.payee,
            amount_microunits: auditCtx?.amountMicrounits,
            status: settleResult.status,
            tx_hash: settleResult.txHash,
            analysis_hash: analysisHash,
            phase: "post_analysis",
          });
          if (settleResult.status !== "success") {
            // Client will receive an error message, not the analysis data.
            // Clear analysisHash so x402_response_sent doesn't claim analysis was delivered.
            if (auditCtx) {
              auditCtx.analysisHash = undefined;
            }
            const isInfra = settleResult.status === "timeout"
              || settleResult.status === "unreachable"
              || settleResult.status === "facilitator_error";
            const message = isInfra
              ? "Payment settlement status is unknown due to a temporary infrastructure issue. " +
                "Your payment may or may not have been processed. Please check your wallet balance before retrying."
              : "Payment settlement failed after analysis completed. " +
                "You have not been charged. Please retry with a new payment.";
            return {
              content: [{ type: "text", text: message }],
              isError: true,
            };
          }
        }

        return {
          content: [{ type: "text", text: rawBody }],
          structuredContent: data,
        };
      } catch (error: unknown) {
        // Analysis threw — do NOT settle payment
        const reason = error instanceof Error && error.name === "AbortError"
          ? "analysis_timeout"
          : "analysis_exception";
        if (pendingPayment) {
          logAuditEvent({
            event: "x402_settle_skipped",
            request_id: auditCtx?.requestId,
            tool: "analyze_token",
            reason,
            detail: error instanceof Error ? error.message : String(error),
            payment_hash: auditCtx?.paymentHash,
            payer: auditCtx?.payer,
            amount_microunits: auditCtx?.amountMicrounits,
          });
        }
        const message = reason === "analysis_timeout"
          ? "Analysis request timed out. Please try again later."
          : "Analysis service request failed. Please try again later.";
        return {
          content: [
            {
              type: "text",
              text: message,
            },
          ],
          isError: true,
        };
      } finally {
        clearTimeout(timeoutId);
      }
    }
  );

  // --- Free tool: get_wallet_balance ---
  const analysisPriceUsdc =
    Number(analysisPriceMicrounits) / 1_000_000;

  server.registerTool(
    "get_wallet_balance",
    {
      description:
        "Check a Solana wallet's SOL and USDC balances. " +
        "Free — no payment required. Useful for verifying funds before " +
        "calling the paid analyze_token tool.",
      inputSchema: {
        address: z
          .string()
          .describe("Solana wallet address to check"),
      },
      outputSchema: {
        address: z.string(),
        sol_balance: z.number(),
        usdc_balance: z.number(),
        analysis_price_usdc: z.number(),
        can_afford_analysis: z.boolean(),
      },
    },
    async ({ address }) => {
      const trimmedAddress = address.trim();
      if (!SOLANA_ADDRESS_RE.test(trimmedAddress)) {
        return {
          content: [
            { type: "text", text: "Invalid Solana wallet address format" },
          ],
          isError: true,
        };
      }

      // --- Manual per-IP rate limiting for free tool ---
      if (clientIp) {
        const now = Date.now();
        const usage = freeToolIpLimits.get(clientIp);
        if (usage && now < usage.resetAt) {
          if (usage.count >= FREE_TOOL_MAX_PER_WINDOW) {
            return {
              content: [
                {
                  type: "text",
                  text: "Too many balance checks. Please try again in a minute.",
                },
              ],
              isError: true,
            };
          }
          usage.count++;
        } else {
          freeToolIpLimits.set(clientIp, {
            count: 1,
            resetAt: now + FREE_TOOL_WINDOW_MS,
          });
        }
      }

      // --- Cache check ---
      const cached = balanceCache.get(trimmedAddress);
      if (cached && Date.now() < cached.expiresAt) {
        const result = {
          address: trimmedAddress,
          sol_balance: cached.sol_balance,
          usdc_balance: cached.usdc_balance,
          analysis_price_usdc: analysisPriceUsdc,
          can_afford_analysis: cached.usdc_balance >= analysisPriceUsdc,
          _cached: true,
        };
        return {
          content: [{ type: "text", text: JSON.stringify(result) }],
          structuredContent: result,
        };
      }

      try {
        // Fetch SOL balance (returns { value: lamports })
        const solResponse = await solanaRpc<{ value: number }>("getBalance", [
          trimmedAddress,
          { commitment: "confirmed" },
        ]);
        const solBalance = solResponse.value / 1e9;

        // Fetch USDC token accounts for this wallet
        let usdcBalance = 0;
        try {
          const tokenAccounts = await solanaRpc<{
            value: Array<{
              account: {
                data: { parsed: { info: { tokenAmount: { uiAmount: number } } } };
              };
            }>;
          }>("getTokenAccountsByOwner", [
            trimmedAddress,
            { mint: usdcMint },
            { encoding: "jsonParsed", commitment: "confirmed" },
          ]);
          for (const acc of tokenAccounts.value) {
            usdcBalance +=
              acc.account.data.parsed.info.tokenAmount.uiAmount ?? 0;
          }
        } catch {
          // No USDC accounts — balance stays 0
        }

        const result = {
          address: trimmedAddress,
          sol_balance: solBalance,
          usdc_balance: usdcBalance,
          analysis_price_usdc: analysisPriceUsdc,
          can_afford_analysis: usdcBalance >= analysisPriceUsdc,
        };

        // --- Update cache ---
        balanceCache.set(trimmedAddress, {
          sol_balance: solBalance,
          usdc_balance: usdcBalance,
          expiresAt: Date.now() + BALANCE_CACHE_TTL_MS,
        });

        return {
          content: [{ type: "text", text: JSON.stringify(result) }],
          structuredContent: result,
        };
      } catch (error: unknown) {
        const message =
          error instanceof Error && error.name === "AbortError"
            ? "Balance check timed out. Please try again later."
            : error instanceof Error
              ? error.message
              : "Failed to fetch wallet balance.";
        return {
          content: [{ type: "text", text: message }],
          isError: true,
        };
      }
    }
  );

  return server;
}

async function main(): Promise<void> {
  const paymentConfig = await buildPaymentConfig();
  const analyzePrice = paymentConfig.priceDescription;
  const usdcMint = paymentConfig.accepts[0].asset;
  const analysisPriceMicrounits = paymentConfig.accepts[0].amount;
  const app = express();

  // Trust proxy (for correct client IP behind Caddy/nginx)
  app.set("trust proxy", 1);

  // Security middleware
  app.use(requestId);
  app.use(requestLogger);
  app.use(securityHeaders);
  app.use(buildCorsMiddleware());
  app.use(globalRateLimiter);

  app.post(
    "/mcp",
    mcpRateLimiter,
    express.json({ limit: "1mb" }),
    async (req: Request, res: Response): Promise<void> => {
      const body = req.body as Record<string, unknown>;

      // Reject JSON-RPC batch requests — the MCP SDK processes arrays natively,
      // which would bypass per-method payment enforcement below.
      if (Array.isArray(body)) {
        res.status(400).json({ error: "Batch requests are not supported" });
        return;
      }

      const method = body?.["method"] as string | undefined;

      // Only enforce x402 payment for the paid analyze_token tool.
      // Payment flow: verify → analyze → settle (user is only charged on success).
      let pendingPayment: PendingPayment | undefined;
      let auditCtx: PaymentAuditContext | undefined;

      if (method === "tools/call") {
        const params = body?.["params"] as Record<string, unknown> | undefined;
        const toolName = params?.["name"];

        if (toolName === "analyze_token") {
          // Validate tool arguments before processing payment
          const toolArgs = params?.["arguments"] as Record<string, unknown> | undefined;
          const validationError = validateAnalyzeArgs(toolArgs);
          if (validationError) {
            res.status(validationError.status).json({ error: validationError.error });
            return;
          }

          // x402 v2 uses PAYMENT-SIGNATURE; v1 used X-PAYMENT.
          // Accept both for backwards compatibility.
          const rawPaymentHeader =
            req.headers["payment-signature"] ?? req.headers["x-payment"];
          let paymentHeader: string | undefined;
          if (Array.isArray(rawPaymentHeader)) {
            if (rawPaymentHeader.length !== 1) {
              res.status(400).json({
                error: "Invalid x-payment header — multiple values are not allowed",
              });
              return;
            }
            paymentHeader = rawPaymentHeader[0];
          } else {
            paymentHeader = rawPaymentHeader;
          }

          if (!paymentHeader) {
            const { body: respBody, headerValue } = buildPaymentRequiredResponse(
              paymentConfig,
              "PAYMENT-SIGNATURE header is required",
            );
            res
              .status(402)
              .set("PAYMENT-REQUIRED", headerValue)
              .json(respBody);
            return;
          }

          // Verify payment is valid without settling — no funds are moved yet.
          const paymentHash = sha256(paymentHeader);
          const rawReqId = req.headers["x-request-id"];
          const reqId = Array.isArray(rawReqId) ? rawReqId[0] : rawReqId;
          const paymentAmount = paymentConfig.accepts[0].amount;
          const payeeAddress = paymentConfig.accepts[0].payTo;

          let verification;
          try {
            verification = await verifyPayment(
              paymentHeader,
              paymentConfig.accepts[0],
              VERIFY_TIMEOUT_MS,
            );
          } catch (err) {
            logAuditEvent({
              event: "x402_verify_error",
              request_id: reqId,
              tool: "analyze_token",
              ip: req.ip,
              payment_hash: paymentHash,
              detail: err instanceof Error ? err.message : String(err),
            });
            res.status(503).json({
              error: "Payment verification service temporarily unavailable. Please try again later.",
            });
            return;
          }

          logAuditEvent({
            event: "x402_verify",
            tool: "analyze_token",
            ip: req.ip,
            request_id: reqId,
            payment_hash: paymentHash,
            payer: verification.payer,
            payee: payeeAddress,
            amount_microunits: paymentAmount,
            valid: verification.valid,
            fallback: verification.fallback ?? false,
            reason: verification.reason,
          });

          if (!verification.valid) {
            // Infrastructure errors (facilitator down/timeout) → 503
            // Invalid payment (bad signature, insufficient amount) → 402
            const isInfraError =
              verification.reason === "facilitator_error" ||
              verification.reason === "facilitator_timeout" ||
              verification.reason === "facilitator_unreachable";

            if (isInfraError) {
              res.status(503).json({
                error:
                  "Payment verification service temporarily unavailable. " +
                  "Please try again later.",
              });
              return;
            }

            const { body: respBody, headerValue } = buildPaymentRequiredResponse(
              paymentConfig,
              "Payment verification failed — invalid or insufficient payment",
            );
            res
              .status(402)
              .set("PAYMENT-REQUIRED", headerValue)
              .json(respBody);
            return;
          }

          // When /verify is not supported (fallback), settle first to validate
          // payment on-chain before running analysis.
          // WARNING: In this mode the client may be charged even if analysis
          // subsequently fails. Use a facilitator that supports /verify to
          // guarantee the "only charge on success" behavior.
          if (verification.fallback) {
            logAuditEvent({
              event: "x402_settle_first_warning",
              request_id: reqId,
              tool: "analyze_token",
              detail: "Settle-first fallback active — client may be charged even if analysis fails",
              payment_hash: paymentHash,
              payer: verification.payer,
            });
            let settleResult;
            try {
              settleResult = await settlePayment(
                paymentHeader,
                paymentConfig.accepts[0],
                SETTLE_TIMEOUT_MS,
              );
            } catch (err) {
              logAuditEvent({
                event: "x402_settle_error",
                request_id: reqId,
                tool: "analyze_token",
                payment_hash: paymentHash,
                payer: verification.payer,
                phase: "settle_first_fallback",
                detail: err instanceof Error ? err.message : String(err),
              });
              res.status(503).json({
                error: "Payment gateway temporarily unavailable",
                retryable: true,
              });
              return;
            }

            logAuditEvent({
              event: "x402_payment",
              tool: "analyze_token",
              ip: req.ip,
              request_id: reqId,
              payment_hash: paymentHash,
              payer: verification.payer,
              payee: payeeAddress,
              amount_microunits: paymentAmount,
              status: settleResult.status,
              tx_hash: settleResult.txHash,
              phase: "settle_first_fallback",
            });

            if (settleResult.status !== "success") {
              const isInfra = settleResult.status === "timeout"
                || settleResult.status === "unreachable"
                || settleResult.status === "facilitator_error";
              if (isInfra) {
                res.status(503).json({
                  error: "Payment gateway temporarily unavailable",
                  retryable: true,
                });
              } else {
                const { body: respBody, headerValue } = buildPaymentRequiredResponse(
                  paymentConfig,
                  "Payment settlement failed — invalid or already-used payment",
                );
                res
                  .status(402)
                  .set("PAYMENT-REQUIRED", headerValue)
                  .json(respBody);
              }
              return;
            }

            // Already settled — don't pass pendingPayment (avoid double-settle)
            pendingPayment = undefined;
            // Still populate auditCtx so delivery confirmation and X-Request-Id forwarding work.
            auditCtx = {
              requestId: reqId,
              ip: req.ip,
              paymentHash,
              payer: verification.payer,
              payee: payeeAddress,
              amountMicrounits: paymentAmount,
            };
          } else {
            // Payment verified — pass it to the tool handler for deferred settlement.
            pendingPayment = {
              header: paymentHeader,
              requirements: paymentConfig.accepts[0],
            };
            auditCtx = {
              requestId: reqId,
              ip: req.ip,
              paymentHash,
              payer: verification.payer,
              payee: payeeAddress,
              amountMicrounits: paymentAmount,
            };
          }
        }
      }

      // Log delivery confirmation for paid requests.
      // 'finish' = server finished writing (best-effort proof of delivery).
      // 'close' without 'finish' = connection dropped before response completed.
      if (auditCtx) {
        let finished = false;
        res.on("finish", () => {
          finished = true;
          logAuditEvent({
            event: "x402_response_sent",
            request_id: auditCtx!.requestId,
            payment_hash: auditCtx!.paymentHash,
            payer: auditCtx!.payer,
            analysis_hash: auditCtx!.analysisHash,
            http_status: res.statusCode,
            content_length: res.getHeader("content-length"),
          });
        });
        res.on("close", () => {
          if (!finished) {
            logAuditEvent({
              event: "x402_response_aborted",
              request_id: auditCtx!.requestId,
              payment_hash: auditCtx!.paymentHash,
              payer: auditCtx!.payer,
              http_status: res.statusCode,
            });
          }
        });
      }

      // Dispatch through a fresh stateless MCP server instance
      const server = makeMcpServer(
        analyzePrice,
        usdcMint,
        analysisPriceMicrounits,
        pendingPayment,
        auditCtx,
        req.ip,
      );
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined, // Stateless: no session state
      });

      try {
        await server.connect(transport);
        await transport.handleRequest(req, res, body);
      } finally {
        // Best-effort cleanup; ignore close errors
        await server.close().catch(() => undefined);
      }
    }
  );

  app.get("/health", (_req: Request, res: Response) => {
    res.json({ status: "ok" });
  });

  // Global error handler (must be registered last)
  app.use(globalErrorHandler);

  app.listen(SERVER_PORT, "0.0.0.0", () => {
    console.log(`DEX Analysis MCP server listening on port ${SERVER_PORT}`);
    console.log(
      `Tool calls require USDC payment via x402 (facilitator: ${FACILITATOR_URL})`
    );
    console.log(`Python API: ${PYTHON_API_URL}`);
  });
}

// Only auto-start when running as the main entry point (not when imported by tests).
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const isMainModule =
  typeof process.argv[1] === "string" &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isMainModule) {
  main().catch((err) => {
    console.error("Fatal:", err instanceof Error ? err.message : String(err));
    process.exit(1);
  });
}
