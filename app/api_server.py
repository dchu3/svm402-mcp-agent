"""FastAPI HTTP server wrapping TokenAnalyzer for the paid analysis service."""

from __future__ import annotations

import hmac
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response as StarletteResponse

from app.config import load_settings
from app.mcp_client import MCPManager
from app.token_analyzer import AnalysisReport, TokenAnalyzer, normalize_chain_identifier

logger = logging.getLogger(__name__)

_mcp_manager: Optional[MCPManager] = None
_token_analyzer: Optional[TokenAnalyzer] = None
_internal_api_secret: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    global _mcp_manager, _token_analyzer, _internal_api_secret
    settings = load_settings()

    # Configure Python logging from LOG_LEVEL env var.
    # Only set up handlers if none exist to avoid clobbering Uvicorn's logging config.
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        root_logger.setLevel(log_level)

    _internal_api_secret = settings.internal_api_secret

    _mcp_manager = MCPManager(
        dexscreener_cmd=settings.mcp_dexscreener_cmd,
        dexpaprika_cmd=settings.mcp_dexpaprika_cmd,
        rugcheck_cmd=settings.mcp_rugcheck_cmd,
        solana_rpc_cmd=settings.mcp_solana_rpc_cmd,
        call_timeout=float(settings.mcp_call_timeout),
        solana_rpc_url=settings.solana_rpc_url,
    )

    await _mcp_manager.start()

    _token_analyzer = TokenAnalyzer(
        api_key=settings.gemini_api_key,
        mcp_manager=_mcp_manager,
        model_name=settings.gemini_model,
    )

    logger.info("Analysis server ready")
    try:
        yield
    finally:
        mcp_manager = _mcp_manager
        _token_analyzer = None
        _mcp_manager = None
        if mcp_manager:
            await mcp_manager.shutdown()


app = FastAPI(title="DEX Analysis API", lifespan=lifespan)


# -- Security middleware (applied in reverse registration order) --


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response: StarletteResponse = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response


class InternalAPIKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests to /analyze that do not carry a valid X-Internal-API-Key.

    When INTERNAL_API_SECRET is configured, only requests bearing the matching
    header are forwarded to the route handler.  This prevents direct bypass of
    the x402 payment enforcement layer in the TypeScript MCP gateway.

    When INTERNAL_API_SECRET is not set the middleware is a no-op so that local
    development (no shared secret configured) continues to work.
    """

    async def dispatch(self, request: StarletteRequest, call_next):
        if request.url.path == "/analyze" and _internal_api_secret:
            provided = request.headers.get("X-Internal-API-Key", "")
            if not hmac.compare_digest(provided, _internal_api_secret):
                return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        return await call_next(request)


app.add_middleware(InternalAPIKeyMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

trusted_hosts = os.environ.get(
    "TRUSTED_HOSTS", "localhost,api-service,127.0.0.1,test,testserver"
).split(",")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in trusted_hosts])

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://analysis-server:4022"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

# -- Address validation patterns --

SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
ALLOWED_CHAINS = {"solana"}


class AnalyzeRequest(BaseModel):
    address: str
    chain: Optional[str] = None


class PriceDataResponse(BaseModel):
    price_usd: Optional[float] = None
    change_24h_percent: Optional[float] = None
    market_cap_usd: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    fdv_usd: Optional[float] = None


class LiquidityResponse(BaseModel):
    total_usd: Optional[float] = None
    top_pool: Optional[str] = None
    top_pool_liquidity_usd: Optional[float] = None
    lp_locked_pct: Optional[float] = None


class SafetyResponse(BaseModel):
    status: str
    risk_score: Optional[float] = None
    risk_level: str = "unknown"
    flags: List[str] = Field(default_factory=list)


class HolderSnapshotResponse(BaseModel):
    top_10_holders_percent: Optional[float] = None
    concentration_risk: str = "unknown"


class WashTradingResponse(BaseModel):
    manipulation_score: Optional[float] = None
    manipulation_level: str = "unknown"
    unique_wallets: Optional[int] = None
    total_transactions_sampled: Optional[int] = None
    repeat_buyers: List[Dict[str, Any]] = Field(default_factory=list)
    flags: List[str] = Field(default_factory=list)


class AIAnalysisResponse(BaseModel):
    key_strengths: List[str] = Field(default_factory=list)
    key_risks: List[str] = Field(default_factory=list)
    whale_signal: str = "unknown"
    narrative_momentum: str = "neutral"


class VerdictResponse(BaseModel):
    action: str = "hold"
    confidence: str = "low"
    one_sentence: str = "Insufficient data for analysis."


class AnalyzeResponse(BaseModel):
    token: str
    chain: str
    address: str
    timestamp: str
    price_data: PriceDataResponse
    liquidity: LiquidityResponse
    safety: SafetyResponse
    holder_snapshot: Optional[HolderSnapshotResponse] = None
    wash_trading: Optional[WashTradingResponse] = None
    ai_analysis: AIAnalysisResponse
    verdict: VerdictResponse
    human_readable: str


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_token(request: AnalyzeRequest, http_request: Request) -> AnalyzeResponse:
    if not _token_analyzer:
        raise HTTPException(status_code=503, detail="Analysis service not ready")

    request_id = http_request.headers.get("X-Request-Id", "")
    address = request.address.strip()
    if not address:
        raise HTTPException(status_code=400, detail="address is required")

    if not SOLANA_ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid address format")

    normalized_chain = normalize_chain_identifier(request.chain)

    if normalized_chain is not None and normalized_chain not in ALLOWED_CHAINS:
        raise HTTPException(status_code=400, detail="Invalid chain parameter")

    logger.info("Analysis started address=%s chain=%s request_id=%s", address, normalized_chain, request_id)
    start_time = time.monotonic()
    try:
        report: AnalysisReport = await _token_analyzer.analyze(
            address,
            normalized_chain,
            structured=True,
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        logger.exception("Analysis failed address=%s request_id=%s duration_ms=%d", address, request_id, duration_ms)
        raise HTTPException(status_code=500, detail="Analysis failed due to an internal error") from exc

    duration_ms = int((time.monotonic() - start_time) * 1000)

    structured = report.structured
    if not structured:
        logger.error("Structured report generation failed address=%s request_id=%s duration_ms=%d", address, request_id, duration_ms)
        raise HTTPException(status_code=500, detail="Structured report generation failed")

    logger.info("Analysis complete address=%s request_id=%s duration_ms=%d", address, request_id, duration_ms)

    holder_snapshot = None
    if structured.holder_snapshot:
        holder_snapshot = HolderSnapshotResponse(**structured.holder_snapshot)

    wash_trading = None
    if structured.wash_trading:
        wash_trading = WashTradingResponse(**structured.wash_trading)

    return AnalyzeResponse(
        token=structured.token,
        chain=structured.chain,
        address=structured.address,
        timestamp=structured.timestamp,
        price_data=PriceDataResponse(**structured.price_data),
        liquidity=LiquidityResponse(**structured.liquidity),
        safety=SafetyResponse(**structured.safety),
        holder_snapshot=holder_snapshot,
        wash_trading=wash_trading,
        ai_analysis=AIAnalysisResponse(**structured.ai_analysis),
        verdict=VerdictResponse(**structured.verdict),
        human_readable=structured.human_readable,
    )


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "ready": _token_analyzer is not None}
