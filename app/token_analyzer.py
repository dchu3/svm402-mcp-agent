"""Token safety and market analysis module."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from google import genai
from google.genai import types

from app.formatting import format_price, format_large_number

if TYPE_CHECKING:
    from app.mcp_client import MCPManager

# Type alias for log callback
LogCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]

# Regex patterns for address detection
SOLANA_ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


@dataclass
class TokenData:
    """Raw data collected about a token."""

    address: str
    chain: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    price_usd: Optional[float] = None
    price_change_24h: Optional[float] = None
    volume_24h: Optional[float] = None
    liquidity_usd: Optional[float] = None
    market_cap: Optional[float] = None
    fdv: Optional[float] = None
    pools: List[Dict[str, Any]] = field(default_factory=list)
    safety_data: Optional[Dict[str, Any]] = None
    safety_status: str = "Unverified"  # Safe, Risky, Honeypot, Dangerous, Unverified
    raw_dexscreener: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)

    # Enriched fields
    top_pool_name: Optional[str] = None
    top_pool_liquidity: Optional[float] = None
    pair_created_at: Optional[str] = None
    lp_locked_pct: Optional[float] = None
    contract_open_source: Optional[bool] = None

    # Holder data
    top_10_holders_pct: Optional[float] = None
    holder_concentration_risk: Optional[str] = None  # low, medium, high

    # Safety score (normalized 0-10)
    risk_score: Optional[float] = None
    risk_level: Optional[str] = None  # low, medium, high
    safety_flags: List[str] = field(default_factory=list)


@dataclass
class StructuredAIAnalysis:
    """Structured output from Gemini AI analysis."""

    key_strengths: List[str] = field(default_factory=list)
    key_risks: List[str] = field(default_factory=list)
    whale_signal: str = "unknown"
    narrative_momentum: str = "neutral"


@dataclass
class Verdict:
    """AI-generated verdict for the token."""

    action: str = "hold"
    confidence: str = "low"
    one_sentence: str = "Insufficient data for analysis."


@dataclass
class StructuredAnalysisReport:
    """Full structured analysis report returned by the x402 endpoint."""

    token: str
    chain: str
    address: str
    timestamp: str
    price_data: Dict[str, Any]
    liquidity: Dict[str, Any]
    safety: Dict[str, Any]
    holder_snapshot: Optional[Dict[str, Any]]
    ai_analysis: Dict[str, Any]
    verdict: Dict[str, Any]
    human_readable: str


@dataclass
class AnalysisReport:
    """Complete analysis report for a token."""

    token_data: TokenData
    ai_analysis: str
    generated_at: datetime
    structured: Optional[StructuredAnalysisReport] = None


def detect_chain(address: str) -> Optional[str]:
    """Detect blockchain from address format.
    
    Args:
        address: Token address to analyze
        
    Returns:
        'solana' if the address matches Solana format, None otherwise
    """
    address = address.strip()
    
    if SOLANA_ADDRESS_PATTERN.match(address):
        if not address.startswith("0x"):
            return "solana"
    
    return None


def normalize_chain_identifier(chain: Optional[str]) -> Optional[str]:
    """Normalize chain aliases into canonical internal identifiers."""
    if chain is None:
        return None

    normalized = chain.strip().lower()
    if not normalized:
        return None

    aliases = {
        "sol": "solana",
        "solana": "solana",
    }
    return aliases.get(normalized, normalized)


def is_valid_token_address(text: str) -> bool:
    """Check if text looks like a valid token address.
    
    Args:
        text: Text to check
        
    Returns:
        True if text appears to be a token address
    """
    text = text.strip()
    return bool(SOLANA_ADDRESS_PATTERN.match(text))



# System prompt for structured JSON analysis (x402 endpoint)
STRUCTURED_ANALYSIS_SYSTEM_PROMPT = """You are a crypto token analyst. Analyze the provided token data and return a JSON object with your assessment.

You MUST return valid JSON matching this exact schema:
{
  "key_strengths": ["string", ...],
  "key_risks": ["string", ...],
  "whale_signal": "none detected | accumulation detected | distribution detected | unknown",
  "narrative_momentum": "positive | neutral | negative | unknown",
  "action": "strong_buy | buy | buy_on_dip | hold | reduce | sell | avoid",
  "confidence": "high | medium | low",
  "one_sentence": "A single sentence verdict under 120 characters."
}

Guidelines:
- key_strengths: 1-4 short phrases about what's good (safety, liquidity, momentum, etc.)
- key_risks: 1-4 short phrases about concerns (volatility, concentration, low liquidity, etc.)
- whale_signal: Based on holder concentration and trading patterns in the data
- narrative_momentum: Based on price action, volume trends, and market sentiment
- action: Your recommended action for a trader
- confidence: How confident you are in this recommendation
- one_sentence: A punchy, opinionated summary — be direct

Return ONLY the JSON object, no markdown, no explanation.
"""



class TokenAnalyzer:
    """Analyzes tokens for safety and market data using MCP tools and AI."""

    def __init__(
        self,
        api_key: str,
        mcp_manager: "MCPManager",
        model_name: str = "gemini-2.5-flash",
        verbose: bool = False,
        log_callback: Optional[LogCallback] = None,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.model_name = model_name
        self.verbose = verbose
        self.log_callback = log_callback
        self.client = genai.Client(api_key=api_key)

    def _log(self, level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Log a message if verbose mode is enabled."""
        if self.verbose and self.log_callback:
            self.log_callback(level, message, data)

    async def analyze(
        self,
        address: str,
        chain: Optional[str] = None,
        *,
        structured: bool = True,
    ) -> AnalysisReport:
        """Analyze a token and generate a comprehensive report.
        
        Args:
            address: Token contract address
            chain: Blockchain (auto-detected if not provided)
            structured: If True, generate structured JSON analysis for x402
                consumers.
            
        Returns:
            Complete analysis report with AI insights
        """
        chain = normalize_chain_identifier(chain)

        # Auto-detect chain if not provided
        if not chain:
            chain = detect_chain(address)
            if not chain:
                raise ValueError(
                    f"Unable to detect chain for address '{address}'. "
                    "Only Solana addresses are supported."
                )
        
        self._log("info", f"Analyzing token {address} on {chain}")
        
        # Collect data from MCP tools (includes holder data)
        token_data = await self._collect_token_data(address, chain)
        
        ai_structured = None
        verdict = None
        structured_report = None

        if structured:
            # Generate structured AI analysis (JSON mode) — includes verdict
            ai_structured, verdict = await self._generate_structured_ai_analysis(token_data)
        generated_at = datetime.now(timezone.utc)
        
        # Build the structured report (only when requested)
        if structured and ai_structured and verdict:
            structured_report = self._build_structured_report(
                token_data, ai_structured, verdict, generated_at
            )

        ai_analysis = ""
        
        return AnalysisReport(
            token_data=token_data,
            ai_analysis=ai_analysis,
            generated_at=generated_at,
            structured=structured_report,
        )

    async def _collect_token_data(self, address: str, chain: str) -> TokenData:
        """Collect token data from various MCP sources."""
        token_data = TokenData(address=address, chain=chain)
        
        # DexScreener first — it may resolve/correct the chain for the token
        try:
            await self._fetch_dexscreener_data(address, chain, token_data)
        except Exception as e:
            self._log("error", f"Data collection task 'dexscreener' failed: {e}")
            token_data.errors.append(f"dexscreener error: {e}")
        
        # Use resolved chain for safety + holder fetches
        resolved_chain = token_data.chain

        if resolved_chain == "solana":
            # Solana: run safety first (rugcheck populates safety_data with
            # holder info), then holder fetch reads it — avoids unnecessary
            # Solana RPC fallback calls.
            try:
                await self._fetch_safety_data(address, resolved_chain, token_data)
            except Exception as e:
                self._log("error", f"Data collection task 'safety' failed: {e}")
                token_data.errors.append(f"safety error: {e}")
            try:
                await self._fetch_holder_data(address, resolved_chain, token_data)
            except Exception as e:
                self._log("error", f"Data collection task 'holder' failed: {e}")
                token_data.errors.append(f"holder error: {e}")
        else:
            token_data.safety_status = "Unverified"
            token_data.safety_data = {"note": f"Safety checks not available for {resolved_chain}"}
        
        return token_data

    async def _fetch_dexscreener_data(
        self, address: str, chain: str, token_data: TokenData
    ) -> None:
        """Fetch token data from DexScreener."""
        try:
            client = self.mcp_manager.get_client("dexscreener")
            if not client:
                token_data.errors.append("DexScreener client not available")
                return
            
            # For Solana, use get_token_pools with chain
            self._log("tool", f"→ dexscreener_get_token_pools({chain}, {address})")
            result = await client.call_tool("get_token_pools", {
                "chainId": chain,
                "tokenAddress": address,
            })
            self._log("tool", "✓ dexscreener_get_token_pools")
            
            if not result:
                token_data.errors.append("No data from DexScreener")
                return
            
            # Handle MCP error strings
            if isinstance(result, str):
                if result.startswith("MCP error"):
                    token_data.errors.append(f"DexScreener: {result}")
                    return
                # Try to parse as JSON if it's a string
                try:
                    import json
                    result = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    token_data.errors.append(f"DexScreener: unexpected response format")
                    return
            
            token_data.raw_dexscreener = result
            
            # Parse the response - handle both list and dict formats
            pairs = []
            if isinstance(result, list):
                pairs = result
            elif isinstance(result, dict):
                pairs = result.get("pairs", []) or [result]
            
            if not pairs:
                token_data.errors.append("No pairs found for token")
                return
            
            # Get data from first/best pair
            best_pair = pairs[0]
            if not isinstance(best_pair, dict):
                token_data.errors.append("Invalid pair data format")
                return
            
            # Update chain from actual DexScreener result
            actual_chain = best_pair.get("chainId")
            if actual_chain:
                token_data.chain = actual_chain
            
            # Extract base token info
            base_token = best_pair.get("baseToken", {})
            token_data.symbol = base_token.get("symbol", "Unknown")
            token_data.name = base_token.get("name", "Unknown")
            
            # Price data
            token_data.price_usd = self._safe_float(best_pair.get("priceUsd"))
            price_change = best_pair.get("priceChange", {})
            token_data.price_change_24h = self._safe_float(price_change.get("h24"))
            
            # Volume and liquidity
            volume = best_pair.get("volume", {})
            token_data.volume_24h = self._safe_float(volume.get("h24"))
            token_data.liquidity_usd = self._safe_float(
                best_pair.get("liquidity", {}).get("usd")
            )
            
            # Market cap / FDV
            token_data.market_cap = self._safe_float(best_pair.get("marketCap"))
            token_data.fdv = self._safe_float(best_pair.get("fdv"))
            
            # Collect pool info
            for pair in pairs[:5]:  # Top 5 pools
                if not isinstance(pair, dict):
                    continue
                pool_info = {
                    "dex": pair.get("dexId", "Unknown"),
                    "pair": pair.get("pairAddress", ""),
                    "liquidity": self._safe_float(pair.get("liquidity", {}).get("usd")),
                    "volume_24h": self._safe_float(pair.get("volume", {}).get("h24")),
                }
                token_data.pools.append(pool_info)
            
            # Enriched fields: top pool name and liquidity
            if token_data.pools:
                top = token_data.pools[0]
                token_data.top_pool_name = top.get("dex", "Unknown")
                token_data.top_pool_liquidity = top.get("liquidity")
            
            # Token age from pair creation timestamp
            created_at = best_pair.get("pairCreatedAt")
            if created_at:
                token_data.pair_created_at = str(created_at)
                
        except Exception as e:
            self._log("error", f"DexScreener fetch failed: {str(e)}")
            token_data.errors.append(f"DexScreener error: {str(e)}")

    async def _fetch_safety_data(
        self, address: str, chain: str, token_data: TokenData
    ) -> None:
        """Fetch safety data from rugcheck for Solana tokens."""
        try:
            if chain == "solana":
                await self._fetch_rugcheck_data(address, token_data)
            else:
                token_data.safety_status = "Unverified"
                token_data.safety_data = {"note": f"Safety checks not available for {chain}"}
        except Exception as e:
            self._log("error", f"Safety check failed: {str(e)}")
            token_data.safety_status = "Unverified"
            token_data.errors.append(f"Safety check error: {str(e)}")

    async def _fetch_rugcheck_data(
        self, address: str, token_data: TokenData
    ) -> None:
        """Fetch rugcheck data for Solana tokens."""
        client = self.mcp_manager.get_client("rugcheck")
        if not client:
            token_data.safety_status = "Unverified"
            token_data.errors.append("Rugcheck client not available")
            return
        
        self._log("tool", f"→ rugcheck_get_token_summary({address})")
        result = await client.call_tool("get_token_summary", {"token_address": address})
        self._log("tool", "✓ rugcheck_get_token_summary")
        
        # Handle MCP error strings
        if isinstance(result, str):
            if result.startswith("MCP error"):
                token_data.safety_status = "Unverified"
                token_data.errors.append(f"Rugcheck: {result}")
                return
            # Try parsing non-error strings as JSON
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, ValueError):
                token_data.safety_status = "Unverified"
                self._log("warning", f"Unexpected rugcheck string result: {result[:200]}")
                token_data.errors.append(f"Rugcheck: unexpected response format")
                return
        
        # Unwrap list results (e.g. [{"score": ..., "risks": [...]}])
        if isinstance(result, list):
            if result and isinstance(result[0], dict):
                result = result[0]
            else:
                token_data.safety_status = "Unverified"
                self._log("warning", f"Unexpected rugcheck list result: {str(result)[:200]}")
                token_data.errors.append("Rugcheck: unexpected response format")
                return
        
        token_data.safety_data = result
        
        # Parse safety status from rugcheck response
        # Response format: {"score": 1, "score_normalised": 1, "risks": [], "lpLockedPct": ...}
        if isinstance(result, dict):
            self._parse_rugcheck_score(result, token_data)
        else:
            token_data.safety_status = "Unverified"
            self._log("warning", f"Unexpected rugcheck result type: {type(result).__name__}")
            token_data.errors.append("Rugcheck: unexpected response format")
    
    def _parse_rugcheck_score(self, result: Dict[str, Any], token_data: TokenData) -> None:
        """Parse rugcheck score, set safety status, and extract enriched fields."""
        score = result.get("score_normalised", result.get("score"))
        risks = result.get("risks", [])
        
        # Extract LP locked percentage
        lp_locked = result.get("lpLockedPct")
        if lp_locked is not None and not isinstance(lp_locked, bool):
            val = self._safe_float(lp_locked)
            if val is not None and 0 <= val <= 100:
                token_data.lp_locked_pct = val
        
        # Normalize score to 0-10 scale (rugcheck: 0-10000, lower is better)
        score = self._safe_float(score)
        if score is None:
            # No score in response — cannot assess safety
            token_data.safety_status = "Unverified"
            token_data.risk_level = "medium"
            return
        normalized = min(10.0, score / 1000.0)
        token_data.risk_score = round(normalized, 1)
        
        # Collect safety flags from risks
        for risk in risks[:10]:
            if isinstance(risk, dict):
                risk_name = risk.get("name", str(risk))
            else:
                risk_name = str(risk)
            token_data.safety_flags.append(risk_name)
        
        # Score interpretation: lower is better (fewer risks).
        # Classification is purely score-driven to keep risk_score and
        # risk_level consistent — high score always means high risk regardless
        # of how many named risk flags the API returned.
        if score <= 500 and not risks:
            token_data.safety_status = "Safe"
            token_data.risk_level = "low"
        elif score <= 2000:
            token_data.safety_status = "Risky"
            token_data.risk_level = "medium"
        else:
            token_data.safety_status = "Dangerous"
            token_data.risk_level = "high"

    async def _fetch_holder_data(
        self, address: str, chain: str, token_data: TokenData
    ) -> None:
        """Fetch holder concentration data for the token."""
        if chain == "solana":
            await self._fetch_holder_data_solana(address, token_data)

    async def _fetch_holder_data_solana(
        self, address: str, token_data: TokenData
    ) -> None:
        """Extract holder data from rugcheck response or Solana RPC."""
        # First try: extract from rugcheck safety_data (already fetched)
        if token_data.safety_data and isinstance(token_data.safety_data, dict):
            holders = token_data.safety_data.get("topHolders", [])
            if not holders:
                holders = token_data.safety_data.get("holders", [])
            if holders and isinstance(holders, list):
                self._compute_holder_concentration(holders, token_data)
                return

        # Fallback: call Solana RPC getTokenLargestAccounts
        client = self.mcp_manager.get_client("solana")
        if not client:
            self._log("info", "Solana RPC client not available for holder data")
            return

        try:
            self._log("tool", f"→ solana_getTokenLargestAccounts({address})")
            result = await client.call_tool("getTokenLargestAccounts", {
                "mint_address": address,
            })
            self._log("tool", "✓ solana_getTokenLargestAccounts")

            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (json.JSONDecodeError, ValueError):
                    return

            if isinstance(result, dict):
                accounts = result.get("value", [])
                if accounts and isinstance(accounts, list):
                    # Convert RPC format to percentage-based
                    supply_result = await client.call_tool("getTokenSupply", {
                        "mint_address": address,
                    })
                    total_supply = self._extract_supply(supply_result)
                    if total_supply and total_supply > 0:
                        holder_list = []
                        for acc in accounts[:10]:
                            if isinstance(acc, dict):
                                amount = self._extract_solana_ui_amount(acc)
                                if amount:
                                    pct = (amount / total_supply) * 100
                                    holder_list.append({"pct": pct})
                        if holder_list:
                            self._compute_holder_concentration(holder_list, token_data)
        except Exception as e:
            self._log("error", f"Solana holder data fetch failed: {e}")

    def _extract_solana_ui_amount(self, value: Dict[str, Any]) -> Optional[float]:
        """Extract a Solana token amount in UI units from RPC response fields."""
        # Prefer pre-computed decimal string (avoids float precision issues)
        ui_amount_string = value.get("uiAmountString")
        if ui_amount_string not in (None, ""):
            parsed = self._safe_float(ui_amount_string)
            if parsed is not None:
                return parsed

        ui_amount = self._safe_float(value.get("uiAmount"))
        if ui_amount is not None:
            return ui_amount

        # Fall back to raw amount + decimals using Decimal math to avoid
        # float mantissa precision loss on large Solana balances/supplies.
        raw_str = value.get("amount")
        if raw_str is None:
            return None

        try:
            raw_int = int(raw_str) if isinstance(raw_str, str) else int(raw_str)
        except (ValueError, TypeError):
            return None

        decimals_raw = value.get("decimals")
        decimals: Optional[int] = None
        if isinstance(decimals_raw, int):
            decimals = decimals_raw
        else:
            decimals_float = self._safe_float(decimals_raw)
            if decimals_float is not None and decimals_float.is_integer():
                decimals = int(decimals_float)

        if decimals is not None and decimals >= 0:
            try:
                result = Decimal(raw_int) / Decimal(10 ** decimals)
                return float(result)
            except (InvalidOperation, OverflowError):
                pass

        # Cannot determine UI units without decimals — return None rather than
        # silently returning raw base-unit amount, which would mix units in any
        # subsequent percentage computation (e.g. raw/UI or UI/raw → off by 10^decimals).
        return None

    def _extract_supply(self, supply_result: Any) -> Optional[float]:
        """Extract total supply from getTokenSupply response."""
        if isinstance(supply_result, str):
            try:
                supply_result = json.loads(supply_result)
            except (json.JSONDecodeError, ValueError):
                return None
        if isinstance(supply_result, dict):
            value = supply_result.get("value", {})
            if isinstance(value, dict):
                return self._extract_solana_ui_amount(value)
        return None

    def _compute_holder_concentration(
        self, holders: List[Any], token_data: TokenData
    ) -> None:
        """Compute top-10 holder concentration and risk level from holder list."""
        total_pct = 0.0
        for holder in holders[:10]:
            if isinstance(holder, dict):
                pct = self._safe_float(holder.get("pct", holder.get("percentage", 0)))
                if pct:
                    total_pct += pct

        token_data.top_10_holders_pct = round(total_pct, 1)

        if total_pct >= 60:
            token_data.holder_concentration_risk = "high"
        elif total_pct >= 30:
            token_data.holder_concentration_risk = "medium"
        else:
            token_data.holder_concentration_risk = "low"

    async def _generate_structured_ai_analysis(
        self, token_data: TokenData
    ) -> tuple[StructuredAIAnalysis, Verdict]:
        """Generate structured AI analysis using Gemini JSON mode."""
        context = self._build_analysis_context(token_data)

        default_analysis = StructuredAIAnalysis()
        default_verdict = Verdict()

        try:
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.model_name,
                contents=context,
                config=types.GenerateContentConfig(
                    system_instruction=STRUCTURED_ANALYSIS_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                ),
            )

            raw_text = ""
            if response and response.candidates:
                candidate = response.candidates[0]
                if candidate and candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if hasattr(part, "text") and part.text:
                            raw_text = part.text.strip()
                            break

            if not raw_text:
                return default_analysis, default_verdict

            data = json.loads(raw_text)
            if not isinstance(data, dict):
                return default_analysis, default_verdict

            raw_strengths = data.get("key_strengths", [])
            raw_risks = data.get("key_risks", [])
            if not isinstance(raw_strengths, list):
                raw_strengths = [str(raw_strengths)] if raw_strengths else []
            if not isinstance(raw_risks, list):
                raw_risks = [str(raw_risks)] if raw_risks else []

            analysis = StructuredAIAnalysis(
                key_strengths=[str(s) for s in raw_strengths][:4],
                key_risks=[str(r) for r in raw_risks][:4],
                whale_signal=str(data.get("whale_signal", "unknown")),
                narrative_momentum=str(data.get("narrative_momentum", "neutral")),
            )
            verdict = Verdict(
                action=str(data.get("action", "hold")),
                confidence=str(data.get("confidence", "low")),
                one_sentence=str(data.get("one_sentence", "Insufficient data for analysis.")),
            )
            return analysis, verdict

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self._log("error", f"Structured AI analysis parse failed: {e}")
            return default_analysis, default_verdict
        except Exception as e:
            self._log("error", f"Structured AI analysis failed: {e}")
            return default_analysis, default_verdict

    def _build_analysis_context(self, token_data: TokenData) -> str:
        """Build context string for AI analysis."""
        lines = [
            f"Token: {token_data.symbol or 'Unknown'} ({token_data.name or 'Unknown'})",
            f"Chain: {token_data.chain}",
            f"Address: {token_data.address}",
            "",
            "=== Market Data ===",
            f"Price: ${(token_data.price_usd or 0):.10f}",
            f"24h Change: {(token_data.price_change_24h or 0):.2f}%",
            f"24h Volume: ${(token_data.volume_24h or 0):,.0f}",
            f"Liquidity: ${(token_data.liquidity_usd or 0):,.0f}",
            f"Market Cap: ${(token_data.market_cap or 0):,.0f}",
            f"FDV: ${(token_data.fdv or 0):,.0f}",
            "",
            "=== Safety Data ===",
            f"Status: {token_data.safety_status}",
            f"Risk Score: {token_data.risk_score if token_data.risk_score is not None else 'N/A'}/10",
        ]
        
        # Add safety details
        if token_data.safety_data:
            if isinstance(token_data.safety_data, dict):
                # Simulation data (buy/sell tax)
                sim = token_data.safety_data.get("simulationResult", {})
                if sim:
                    lines.append(f"Buy Tax: {sim.get('buyTax', 'N/A')}%")
                    lines.append(f"Sell Tax: {sim.get('sellTax', 'N/A')}%")
                
                # Rugcheck data
                risks = token_data.safety_data.get("risks", [])
                if risks:
                    lines.append(f"Risks: {', '.join(str(r) for r in risks[:5])}")
        
        if token_data.lp_locked_pct is not None:
            lines.append(f"LP Locked: {token_data.lp_locked_pct:.1f}%")
        
        if token_data.contract_open_source is not None:
            lines.append(f"Contract Open Source: {'Yes' if token_data.contract_open_source else 'No'}")

        if token_data.safety_flags:
            lines.append(f"Safety Flags: {', '.join(token_data.safety_flags[:5])}")
        
        # Token age
        if token_data.pair_created_at:
            lines.append(f"Pair Created: {token_data.pair_created_at}")
        
        # Holder concentration
        if token_data.top_10_holders_pct is not None:
            lines.append("")
            lines.append("=== Holder Data ===")
            lines.append(f"Top 10 Holders: {token_data.top_10_holders_pct:.1f}%")
            lines.append(f"Concentration Risk: {token_data.holder_concentration_risk or 'unknown'}")
        
        # Add pool info
        if token_data.pools:
            lines.append("")
            lines.append("=== Top Pools ===")
            for pool in token_data.pools[:3]:
                lines.append(
                    f"- {pool['dex']}: ${(pool.get('liquidity') or 0):,.0f} liquidity"
                )
        
        # Add errors if any
        if token_data.errors:
            lines.append("")
            lines.append("=== Data Issues ===")
            for err in token_data.errors:
                lines.append(f"- {err}")
        
        return "\n".join(lines)


    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        """Safely convert value to float."""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def _build_structured_report(
        self,
        token_data: TokenData,
        ai_analysis: StructuredAIAnalysis,
        verdict: Verdict,
        generated_at: datetime,
    ) -> StructuredAnalysisReport:
        """Build the full structured analysis report for the x402 endpoint."""
        holder_snapshot = None
        if token_data.top_10_holders_pct is not None:
            holder_snapshot = {
                "top_10_holders_percent": token_data.top_10_holders_pct,
                "concentration_risk": token_data.holder_concentration_risk or "unknown",
            }

        human_readable = self._build_human_readable(
            token_data, ai_analysis, verdict, generated_at
        )

        return StructuredAnalysisReport(
            token=token_data.symbol or "Unknown",
            chain=token_data.chain,
            address=token_data.address,
            timestamp=generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            price_data={
                "price_usd": token_data.price_usd,
                "change_24h_percent": token_data.price_change_24h,
                "market_cap_usd": token_data.market_cap,
                "volume_24h_usd": token_data.volume_24h,
                "fdv_usd": token_data.fdv,
            },
            liquidity={
                "total_usd": token_data.liquidity_usd,
                "top_pool": token_data.top_pool_name,
                "top_pool_liquidity_usd": token_data.top_pool_liquidity,
                "lp_locked_pct": token_data.lp_locked_pct,
            },
            safety={
                "status": token_data.safety_status.lower(),
                "risk_score": token_data.risk_score,
                "risk_level": token_data.risk_level or "unknown",
                "flags": token_data.safety_flags[:10],
            },
            holder_snapshot=holder_snapshot,
            ai_analysis={
                "key_strengths": ai_analysis.key_strengths,
                "key_risks": ai_analysis.key_risks,
                "whale_signal": ai_analysis.whale_signal,
                "narrative_momentum": ai_analysis.narrative_momentum,
            },
            verdict={
                "action": verdict.action,
                "confidence": verdict.confidence,
                "one_sentence": verdict.one_sentence,
            },
            human_readable=human_readable,
        )

    def _build_human_readable(
        self,
        token_data: TokenData,
        ai_analysis: StructuredAIAnalysis,
        verdict: Verdict,
        generated_at: datetime,
    ) -> str:
        """Build a human-readable summary from structured data."""
        safety_emoji = {
            "Safe": "✅", "Risky": "⚠️", "Honeypot": "❌",
            "Dangerous": "❌", "Unverified": "❓",
        }.get(token_data.safety_status, "❓")

        change = token_data.price_change_24h or 0
        change_emoji = "🟢" if change >= 0 else "🔴"

        price_fmt = format_price(token_data.price_usd)
        mcap_fmt = format_large_number(token_data.market_cap)
        vol_fmt = format_large_number(token_data.volume_24h)
        liq_fmt = format_large_number(token_data.liquidity_usd)

        lines = [
            "🔍 Token Analysis Report",
            f"Token: {token_data.symbol or 'Unknown'} | Chain: {token_data.chain.capitalize()}",
            f"Address: {token_data.address}",
            "",
            f"💰 Price: {price_fmt} ({change_emoji} {change:+.2f}%)",
            f"📊 MCap: {mcap_fmt} | Vol 24h: {vol_fmt} | Liq: {liq_fmt}",
        ]

        if token_data.top_pool_name:
            pool_liq_fmt = format_large_number(token_data.top_pool_liquidity)
            lines.append(f"💧 Top Pool: {token_data.top_pool_name} ({pool_liq_fmt})")

        if token_data.lp_locked_pct is not None:
            lines.append(f"🔒 LP Locked: {token_data.lp_locked_pct:.1f}%")

        lines.append(f"🛡️ Safety: {safety_emoji} {token_data.safety_status}")

        if token_data.risk_score is not None:
            lines.append(f"   Risk Score: {token_data.risk_score}/10 ({token_data.risk_level or 'unknown'})")

        if token_data.top_10_holders_pct is not None:
            lines.append(f"👥 Top 10 Holders: {token_data.top_10_holders_pct:.1f}% ({token_data.holder_concentration_risk or 'unknown'})")

        if ai_analysis.key_strengths:
            lines.append(f"\n✅ Strengths: {', '.join(ai_analysis.key_strengths)}")
        if ai_analysis.key_risks:
            lines.append(f"⚠️ Risks: {', '.join(ai_analysis.key_risks)}")

        lines.append(f"\n🎯 Verdict: {verdict.action.upper()} ({verdict.confidence} confidence)")
        lines.append(f"   {verdict.one_sentence}")

        timestamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"\n⏰ {timestamp}")

        return "\n".join(lines)
