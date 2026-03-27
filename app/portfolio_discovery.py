"""Portfolio token discovery engine with deterministic pre-filter and AI scoring."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from google import genai
from google.genai import types

from app.insider_detection import InsiderAnalysis, InsiderRisk, analyse_insiders
from app.types import DecisionLabel, MAX_TOOL_RESULT_CHARS

if TYPE_CHECKING:
    from app.mcp_client import MCPManager
    from app.database import Database

logger = logging.getLogger(__name__)

LogCallback = Callable[[str, str, Optional[Dict[str, Any]]], None]

_AI_DECISION_POOL_MULTIPLIER = 3
_AI_DECISION_CONCURRENCY = 3


@dataclass
class DiscoveryCandidate:
    """A token candidate that passed deterministic filters."""

    token_address: str
    symbol: str
    chain: str
    price_usd: float
    volume_24h: float
    liquidity_usd: float
    market_cap_usd: float = 0.0
    price_change_24h: float = 0.0
    price_change_1h: float = 0.0
    price_change_6h: float = 0.0
    price_change_5m: float = 0.0
    safety_status: str = "unknown"
    safety_score: Optional[float] = None
    momentum_score: float = 0.0
    reasoning: str = ""
    buy_decision: Optional[bool] = None
    decision_label: Optional[DecisionLabel] = None
    insider_analysis: Optional[InsiderAnalysis] = None


DECISION_SYSTEM_PROMPT = """You are an autonomous crypto investment analyst deciding whether to buy a Solana token for a live trading portfolio.

## Your Job
1. Review the candidate data provided.
2. Use the available tools to fetch any additional information you need (deeper pool data, safety re-check, volume trends).
3. Make a definitive buy or no-buy decision.

## Available Tools
- **dexscreener** — search pairs, get token pools, trending data (uses camelCase params: `tokenAddress`, `chainId`)
- **rugcheck** — Solana token safety summary (uses snake_case params: `token_address`)

## Decision Criteria
- **Buy** if: strong volume surge (volume/liquidity ratio > 1.5), positive price momentum, adequate liquidity (>$25k), safe or only mildly risky rugcheck status, and no severe insider red flags.
- **No-buy** if: negative price momentum, low volume relative to liquidity, dangerous rugcheck risks, insider_analysis risk marked as REJECT or WARN (high top-holder concentration or creator still holding a large share and actively trading), or insufficient data to confirm safety.

## CRITICAL: Final Response Format
When you have finished investigating, you MUST end your response with ONLY this JSON block and nothing else after it:
```json
{
  "buy": true,
  "reasoning": "One sentence explaining the decision"
}
```

Use `"buy": false` to reject. Keep reasoning to one sentence.
"""


class PortfolioDiscovery:
    """Hybrid discovery: deterministic pre-filter → AI scoring."""

    def __init__(
        self,
        mcp_manager: "MCPManager",
        api_key: str,
        model_name: str = "gemini-2.5-flash",
        min_volume_usd: float = 50000.0,
        min_liquidity_usd: float = 25000.0,
        min_market_cap_usd: float = 250000.0,
        min_token_age_hours: float = 4.0,
        max_token_age_hours: float = 0.0,
        min_momentum_score: float = 50.0,
        chain: str = "solana",
        verbose: bool = False,
        log_callback: Optional[LogCallback] = None,
        rpc_url: str = "",
        insider_check_enabled: bool = True,
        insider_max_concentration_pct: float = 50.0,
        insider_max_creator_pct: float = 30.0,
        insider_warn_concentration_pct: float = 30.0,
        insider_warn_creator_pct: float = 10.0,
    ) -> None:
        self.mcp_manager = mcp_manager
        self.api_key = api_key
        self.model_name = model_name
        self.min_volume_usd = min_volume_usd
        self.min_liquidity_usd = min_liquidity_usd
        self.min_market_cap_usd = min_market_cap_usd
        self.min_token_age_hours = min_token_age_hours
        self.max_token_age_hours = max_token_age_hours
        if (
            self.min_token_age_hours > 0
            and self.max_token_age_hours > 0
            and self.min_token_age_hours > self.max_token_age_hours
        ):
            raise ValueError(
                "Invalid token age configuration: "
                f"min_token_age_hours ({self.min_token_age_hours}) "
                f"cannot be greater than max_token_age_hours ({self.max_token_age_hours})."
            )
        self.min_momentum_score = min_momentum_score
        self.chain = chain
        self.verbose = verbose
        self.log_callback = log_callback
        self.rpc_url = rpc_url.strip()
        self.insider_check_enabled = insider_check_enabled
        if (
            self.chain.lower() == "solana"
            and self.insider_check_enabled
            and not self.rpc_url
        ):
            logger.warning(
                "Insider check disabled: rpc_url is not configured for Solana discovery."
            )
            self.insider_check_enabled = False
        self.insider_max_concentration_pct = insider_max_concentration_pct
        self.insider_max_creator_pct = insider_max_creator_pct
        self.insider_warn_concentration_pct = insider_warn_concentration_pct
        self.insider_warn_creator_pct = insider_warn_creator_pct

    def _log(self, level: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        if self.verbose and self.log_callback:
            self.log_callback(level, message, data)

    async def discover(
        self,
        db: "Database",
        max_candidates: int = 5,
        decision_log_enabled: bool = False,
        shadow_audit_enabled: bool = False,
        shadow_check_minutes: int = 30,
        position_size_usd: float = 0.0,
    ) -> List[DiscoveryCandidate]:
        """Run full discovery pipeline: scan → filter → safety → AI decision."""
        max_candidates = max(0, max_candidates)
        if max_candidates == 0:
            self._log("info", "max_candidates is 0; skipping discovery")
            return []

        cycle_id = uuid.uuid4().hex[:12]
        decisions: List[tuple] = []

        def _record(c: DiscoveryCandidate, label: DecisionLabel, reason: str = "") -> None:
            """Buffer a decision row for batch insert."""
            c.decision_label = label
            if decision_log_enabled:
                decisions.append((
                    cycle_id,
                    c.token_address.lower(),
                    c.symbol,
                    c.chain.lower(),
                    label.value,
                    c.price_usd,
                    c.volume_24h,
                    c.liquidity_usd,
                    c.market_cap_usd,
                    c.momentum_score,
                    reason or c.reasoning or None,
                    "{}",
                ))

        # Step 1: Scan trending tokens via DexScreener
        raw_pairs = await self._scan_trending()
        if not raw_pairs:
            self._log("info", "No trending pairs found")
            return []
        self._log("info", f"Scanned {len(raw_pairs)} trending pairs")

        # Step 2: Apply deterministic filters (labels assigned inside)
        filtered, filter_rejects = self._apply_filters_with_labels(raw_pairs)
        for c, label, reason in filter_rejects:
            _record(c, label, reason)
        if not filtered:
            self._log("info", "No candidates passed filters")
            await self._flush_decisions(db, decisions)
            return []
        self._log("info", f"{len(filtered)} candidates passed filters")

        # Step 3: Exclude already-held tokens
        before_held = {c.token_address.lower(): c for c in filtered}
        filtered = await self._exclude_held_tokens(filtered, db)
        remaining_held = {c.token_address.lower() for c in filtered}
        for addr, original_c in before_held.items():
            if addr not in remaining_held:
                _record(original_c, DecisionLabel.HELD_TOKEN, "already held")
        if not filtered:
            self._log("info", "All candidates already held")
            await self._flush_decisions(db, decisions)
            return []

        # Step 4: Safety check via rugcheck
        before_safety = {c.token_address.lower(): c for c in filtered}
        safe_candidates = await self._safety_check(filtered)
        safe_set = {sc.token_address.lower() for sc in safe_candidates}
        for addr, c in before_safety.items():
            if addr not in safe_set:
                _record(c, DecisionLabel.SAFETY_REJECTED, f"safety={c.safety_status}")
        if not safe_candidates:
            self._log("info", "No candidates passed safety checks")
            await self._flush_decisions(db, decisions)
            return []
        self._log("info", f"{len(safe_candidates)} candidates passed safety")

        # Step 5: Insider / sniper detection
        before_insider = {c.token_address.lower(): c for c in safe_candidates}
        insider_checked = await self._insider_check(safe_candidates)
        insider_set = {ic.token_address.lower() for ic in insider_checked}
        for addr, c in before_insider.items():
            if addr not in insider_set:
                _record(c, DecisionLabel.INSIDER_REJECTED, "insider risk")
        if not insider_checked:
            self._log("info", "No candidates passed insider checks")
            await self._flush_decisions(db, decisions)
            return []
        self._log("info", f"{len(insider_checked)} candidates passed insider checks")

        # Step 5b: Defensive dedup by token_address before AI step
        seen_addrs: set[str] = set()
        deduped: List[DiscoveryCandidate] = []
        for c in insider_checked:
            addr_lower = c.token_address.lower()
            if addr_lower not in seen_addrs:
                seen_addrs.add(addr_lower)
                deduped.append(c)
            else:
                _record(c, DecisionLabel.DEDUP, "duplicate")
        if len(deduped) < len(insider_checked):
            self._log("info", f"Deduped {len(insider_checked) - len(deduped)} duplicate candidates")
        insider_checked = deduped

        # Step 5c: Heuristic pre-filter to avoid expensive AI calls on weak candidates
        for c in insider_checked:
            c.momentum_score = self._heuristic_score(c)
        threshold = self.min_momentum_score * 0.5
        pre_filtered: List[DiscoveryCandidate] = []
        for c in insider_checked:
            if c.momentum_score >= threshold:
                pre_filtered.append(c)
            else:
                _record(c, DecisionLabel.HEURISTIC_SKIP,
                        f"score={c.momentum_score:.0f} < {threshold:.0f}")
        skipped = len(insider_checked) - len(pre_filtered)
        if skipped:
            self._log(
                "info",
                f"Heuristic pre-filter skipped {skipped} low-scoring candidates "
                f"(threshold={threshold:.0f})",
            )
        if not pre_filtered:
            self._log("info", "No candidates passed heuristic pre-filter")
            await self._flush_decisions(db, decisions)
            return []

        decision_pool_size = max_candidates * _AI_DECISION_POOL_MULTIPLIER
        sorted_pre_filtered = sorted(
            pre_filtered,
            key=lambda c: c.momentum_score,
            reverse=True,
        )
        decision_pool = sorted_pre_filtered[:decision_pool_size]
        for c in sorted_pre_filtered[decision_pool_size:]:
            _record(c, DecisionLabel.AI_POOL_CAP, "outside decision pool")
        if len(decision_pool) < len(pre_filtered):
            self._log(
                "info",
                f"Capped AI decision pool to {len(decision_pool)} candidates "
                f"(from {len(pre_filtered)})",
            )

        # Step 6: Per-candidate agentic buy decision (parallel with bounded concurrency)
        sem = asyncio.Semaphore(_AI_DECISION_CONCURRENCY)

        async def _decide(candidate: DiscoveryCandidate) -> DiscoveryCandidate:
            async with sem:
                buy, reasoning = await self._ai_decide(candidate)
                candidate.buy_decision = buy
                candidate.reasoning = reasoning
                self._log(
                    "info",
                    f"Decision: {candidate.symbol} → {'BUY' if buy else 'SKIP'} "
                    f"(heuristic={candidate.momentum_score:.0f}, "
                    f"vol=${candidate.volume_24h:,.0f} liq=${candidate.liquidity_usd:,.0f} "
                    f"chg={candidate.price_change_24h:+.1f}%) — {reasoning}",
                )
                return candidate

        decided = await asyncio.gather(*[_decide(c) for c in decision_pool])

        # Label each decided candidate
        all_approved: List[DiscoveryCandidate] = []
        for c in decided:
            if c.buy_decision:
                all_approved.append(c)
            else:
                _record(c, DecisionLabel.AI_REJECT, c.reasoning)

        approved = all_approved[:max_candidates]
        for c in all_approved[max_candidates:]:
            _record(c, DecisionLabel.AI_APPROVE_CAPPED, c.reasoning)
        for c in approved:
            _record(c, DecisionLabel.AI_APPROVE, c.reasoning)

        if not approved:
            self._log("info", "No candidates approved by AI decision step")

        # Shadow audit: record approved candidates as paper positions
        if shadow_audit_enabled and approved:
            for c in approved:
                try:
                    await db.add_shadow_position(
                        token_address=c.token_address,
                        symbol=c.symbol,
                        chain=c.chain,
                        entry_price=c.price_usd,
                        notional_usd=position_size_usd,
                        momentum_score=c.momentum_score,
                        reasoning=c.reasoning,
                        check_after_minutes=shadow_check_minutes,
                    )
                except Exception as exc:
                    self._log("warning", f"Shadow position failed for {c.symbol}: {exc}")

        # Flush decision log
        await self._flush_decisions(db, decisions)

        return approved

    async def _flush_decisions(
        self, db: "Database", decisions: List[tuple]
    ) -> None:
        """Batch-write buffered decisions to the database."""
        if not decisions:
            return
        try:
            await db.record_discovery_decisions_batch(decisions)
        except Exception as exc:
            self._log("warning", f"Failed to flush {len(decisions)} decisions: {exc}")

    async def _scan_trending(self) -> List[Dict[str, Any]]:
        """Fetch trending tokens from DexScreener using boosted + search endpoints."""
        client = self.mcp_manager.get_client("dexscreener")
        if not client:
            self._log("error", "DexScreener MCP client not available")
            return []

        all_pairs: List[Dict[str, Any]] = []
        seen_addresses: set[str] = set()

        def _add_pairs(pairs: List[Dict[str, Any]]) -> int:
            added = 0
            for pair in pairs:
                addr = (pair.get("baseToken") or {}).get("address", "").lower()
                if not addr or addr in seen_addresses:
                    continue
                seen_addresses.add(addr)
                all_pairs.append(pair)
                added += 1
            return added

        # Primary: boosted/trending token endpoints → fetch pair data per token
        boosted_tokens = await self._fetch_boosted_tokens(client)
        if boosted_tokens:
            boosted_pairs = await self._fetch_pairs_for_tokens(client, boosted_tokens)
            count = _add_pairs(boosted_pairs)
            self._log("info", f"Boosted tokens: {len(boosted_tokens)} found, {count} pairs added")

        # Secondary: text search for additional breadth
        queries = ["trending solana", "solana"]
        for query in queries:
            try:
                result = await client.call_tool("search_pairs", {"query": query})
                pairs = self._extract_pairs(result)
                count = _add_pairs(pairs)
                self._log("info", f"Search '{query}': {len(pairs)} results, {count} new pairs added")
            except Exception as exc:
                self._log("warning", f"DexScreener query '{query}' failed: {exc}")

        return all_pairs

    async def _fetch_boosted_tokens(self, client: Any) -> List[Dict[str, Any]]:
        """Fetch boosted token addresses from DexScreener trending endpoints."""
        endpoints = ["get_top_boosted_tokens", "get_latest_boosted_tokens"]

        async def _call(endpoint: str) -> List[Dict[str, Any]]:
            try:
                result = await client.call_tool(endpoint, {})
                return self._extract_boosted_tokens(result)
            except Exception as exc:
                self._log("warning", f"{endpoint} failed: {exc}")
                return []

        results = await asyncio.gather(*[_call(ep) for ep in endpoints])

        tokens: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for items in results:
            for item in items:
                chain = (item.get("chainId") or "").lower()
                addr = (item.get("tokenAddress") or "").lower()
                if chain != self.chain or not addr or addr in seen:
                    continue
                seen.add(addr)
                tokens.append(item)

        return tokens

    @staticmethod
    def _extract_boosted_tokens(result: Any) -> List[Dict[str, Any]]:
        """Extract token entries from boosted token response."""
        if isinstance(result, list):
            return [t for t in result if isinstance(t, dict) and t.get("tokenAddress")]
        if isinstance(result, dict):
            # Some responses wrap in a key
            for key in ("tokens", "data", "results"):
                items = result.get(key)
                if isinstance(items, list):
                    return [t for t in items if isinstance(t, dict) and t.get("tokenAddress")]
        return []

    async def _fetch_pairs_for_tokens(
        self, client: Any, tokens: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Fetch pair data for a list of boosted tokens via get_token_pools."""

        async def _fetch_one(token: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            chain = token.get("chainId", self.chain)
            addr = token.get("tokenAddress", "")
            if not addr:
                return None
            try:
                result = await client.call_tool(
                    "get_token_pools",
                    {"chainId": chain, "tokenAddress": addr},
                )
                pool_pairs = self._extract_pairs(result)
                if pool_pairs:
                    return max(
                        pool_pairs,
                        key=lambda p: float(
                            (p.get("liquidity") or {}).get("usd", 0)
                            if isinstance(p.get("liquidity"), dict) else 0
                        ),
                    )
            except Exception as exc:
                self._log("warning", f"get_token_pools failed for {addr[:12]}…: {exc}")
            return None

        results = await asyncio.gather(*[_fetch_one(t) for t in tokens])
        return [p for p in results if p is not None]

    @staticmethod
    def _extract_pairs(result: Any) -> List[Dict[str, Any]]:
        """Extract pair dicts from DexScreener response."""
        if isinstance(result, list):
            return [p for p in result if isinstance(p, dict)]
        if isinstance(result, dict):
            pairs = result.get("pairs", result.get("results", []))
            if isinstance(pairs, list):
                return [p for p in pairs if isinstance(p, dict)]
        return []

    def _apply_filters(self, pairs: List[Dict[str, Any]]) -> List[DiscoveryCandidate]:
        """Apply deterministic volume/liquidity/chain/age filters."""
        candidates: List[DiscoveryCandidate] = []
        seen_addresses: set[str] = set()
        chain_counts: Dict[str, int] = {}
        rejected_volume = 0
        rejected_liquidity = 0
        rejected_market_cap = 0
        rejected_age = 0
        now_ms = time.time() * 1000

        for pair in pairs:
            chain_id = (pair.get("chainId") or "").lower()
            chain_counts[chain_id] = chain_counts.get(chain_id, 0) + 1
            if chain_id != self.chain:
                continue

            base_token = pair.get("baseToken", {})
            address = base_token.get("address", "")
            symbol = base_token.get("symbol", "")
            if not address or not symbol:
                continue

            addr_lower = address.lower()
            if addr_lower in seen_addresses:
                continue
            seen_addresses.add(addr_lower)

            try:
                price = float(pair.get("priceUsd", 0))
                volume_24h = float(pair.get("volume", {}).get("h24", 0))
                liquidity_data = pair.get("liquidity", {})
                liquidity = float(liquidity_data.get("usd", 0)) if isinstance(liquidity_data, dict) else 0.0
                price_change_data = pair.get("priceChange", {})
                if not isinstance(price_change_data, dict):
                    price_change_data = {}
                price_change = self._parse_optional_float(price_change_data.get("h24", 0))
                price_change_1h = self._parse_optional_float(price_change_data.get("h1", 0))
                price_change_6h = self._parse_optional_float(price_change_data.get("h6", 0))
                price_change_5m = self._parse_optional_float(price_change_data.get("m5", 0))
                market_cap_usd = float(pair.get("marketCap", pair.get("fdv", 0)))
                pair_created_at_ms = float(pair.get("pairCreatedAt") or 0)
            except (TypeError, ValueError):
                continue

            if volume_24h < self.min_volume_usd:
                rejected_volume += 1
                continue
            if liquidity < self.min_liquidity_usd:
                rejected_liquidity += 1
                continue
            if market_cap_usd < self.min_market_cap_usd:
                rejected_market_cap += 1
                continue
            if price <= 0:
                continue

            if (self.min_token_age_hours > 0 or self.max_token_age_hours > 0) and pair_created_at_ms > 0:
                age_hours = (now_ms - pair_created_at_ms) / 1_000 / 3_600
                if age_hours < 0:
                    rejected_age += 1
                    continue
                if self.min_token_age_hours > 0 and age_hours < self.min_token_age_hours:
                    rejected_age += 1
                    continue
                if self.max_token_age_hours > 0 and age_hours > self.max_token_age_hours:
                    rejected_age += 1
                    continue

            candidates.append(DiscoveryCandidate(
                token_address=address,
                symbol=symbol,
                chain=self.chain,
                price_usd=price,
                volume_24h=volume_24h,
                liquidity_usd=liquidity,
                market_cap_usd=market_cap_usd,
                price_change_24h=price_change,
                price_change_1h=price_change_1h,
                price_change_6h=price_change_6h,
                price_change_5m=price_change_5m,
            ))

        self._log(
            "info",
            f"Filter breakdown: chains={chain_counts}, "
            f"rejected_volume={rejected_volume}, rejected_liquidity={rejected_liquidity}, "
            f"rejected_market_cap={rejected_market_cap}, rejected_age={rejected_age}, "
            f"passed={len(candidates)}",
        )

        return candidates

    def _apply_filters_with_labels(
        self, pairs: List[Dict[str, Any]]
    ) -> tuple[List[DiscoveryCandidate], List[tuple["DiscoveryCandidate", "DecisionLabel", str]]]:
        """Apply deterministic filters, returning both passed candidates and labeled rejects."""
        candidates: List[DiscoveryCandidate] = []
        rejects: List[tuple[DiscoveryCandidate, DecisionLabel, str]] = []
        seen_addresses: set[str] = set()
        chain_counts: Dict[str, int] = {}
        now_ms = time.time() * 1000

        for pair in pairs:
            chain_raw = pair.get("chainId")
            chain_id = str(chain_raw).lower() if chain_raw is not None else ""
            chain_counts[chain_id] = chain_counts.get(chain_id, 0) + 1
            base_token = pair.get("baseToken", {})
            if not isinstance(base_token, dict):
                base_token = {}
            address = str(base_token.get("address", "") or "")
            symbol = str(base_token.get("symbol", "") or "")
            if not chain_id:
                c = DiscoveryCandidate(
                    token_address=address,
                    symbol=symbol,
                    chain="unknown",
                    price_usd=0.0,
                    volume_24h=0.0,
                    liquidity_usd=0.0,
                )
                rejects.append((c, DecisionLabel.FILTER_PARSE, "missing chainId"))
                continue
            if chain_id != self.chain:
                c = DiscoveryCandidate(
                    token_address=address,
                    symbol=symbol,
                    chain=chain_id,
                    price_usd=0.0,
                    volume_24h=0.0,
                    liquidity_usd=0.0,
                )
                rejects.append(
                    (
                        c,
                        DecisionLabel.FILTER_CHAIN,
                        f"chain mismatch: '{chain_id}' != '{self.chain}'",
                    )
                )
                continue

            if not address or not symbol:
                c = DiscoveryCandidate(
                    token_address=address,
                    symbol=symbol,
                    chain=self.chain,
                    price_usd=0.0,
                    volume_24h=0.0,
                    liquidity_usd=0.0,
                )
                rejects.append((c, DecisionLabel.FILTER_PARSE, "missing address or symbol"))
                continue

            addr_lower = address.lower()
            if addr_lower in seen_addresses:
                c = DiscoveryCandidate(
                    token_address=address,
                    symbol=symbol,
                    chain=self.chain,
                    price_usd=0.0,
                    volume_24h=0.0,
                    liquidity_usd=0.0,
                )
                rejects.append((c, DecisionLabel.FILTER_PARSE, "duplicate token address"))
                continue
            seen_addresses.add(addr_lower)

            try:
                price = float(pair.get("priceUsd", 0))
                volume_24h = float(pair.get("volume", {}).get("h24", 0))
                liquidity_data = pair.get("liquidity", {})
                liquidity = float(liquidity_data.get("usd", 0)) if isinstance(liquidity_data, dict) else 0.0
                price_change_data = pair.get("priceChange", {})
                if not isinstance(price_change_data, dict):
                    price_change_data = {}
                price_change = self._parse_optional_float(price_change_data.get("h24", 0))
                price_change_1h = self._parse_optional_float(price_change_data.get("h1", 0))
                price_change_6h = self._parse_optional_float(price_change_data.get("h6", 0))
                price_change_5m = self._parse_optional_float(price_change_data.get("m5", 0))
                market_cap_usd = float(pair.get("marketCap", pair.get("fdv", 0)))
                pair_created_at_ms = float(pair.get("pairCreatedAt") or 0)
            except (TypeError, ValueError):
                c = DiscoveryCandidate(token_address=address, symbol=symbol, chain=self.chain,
                                       price_usd=0, volume_24h=0, liquidity_usd=0)
                rejects.append((c, DecisionLabel.FILTER_PARSE, "unparseable pair data"))
                continue

            c = DiscoveryCandidate(
                token_address=address, symbol=symbol, chain=self.chain,
                price_usd=price, volume_24h=volume_24h, liquidity_usd=liquidity,
                market_cap_usd=market_cap_usd, price_change_24h=price_change,
                price_change_1h=price_change_1h, price_change_6h=price_change_6h,
                price_change_5m=price_change_5m,
            )

            if volume_24h < self.min_volume_usd:
                rejects.append((c, DecisionLabel.FILTER_VOLUME, f"vol=${volume_24h:,.0f}"))
                continue
            if liquidity < self.min_liquidity_usd:
                rejects.append((c, DecisionLabel.FILTER_LIQUIDITY, f"liq=${liquidity:,.0f}"))
                continue
            if market_cap_usd < self.min_market_cap_usd:
                rejects.append((c, DecisionLabel.FILTER_MCAP, f"mcap=${market_cap_usd:,.0f}"))
                continue
            if price <= 0:
                rejects.append((c, DecisionLabel.FILTER_PRICE, "price<=0"))
                continue

            if (self.min_token_age_hours > 0 or self.max_token_age_hours > 0) and pair_created_at_ms > 0:
                age_hours = (now_ms - pair_created_at_ms) / 1_000 / 3_600
                if age_hours < 0:
                    rejects.append((c, DecisionLabel.FILTER_AGE, f"age={age_hours:.1f}h"))
                    continue
                if self.min_token_age_hours > 0 and age_hours < self.min_token_age_hours:
                    rejects.append((c, DecisionLabel.FILTER_AGE, f"age={age_hours:.1f}h < min"))
                    continue
                if self.max_token_age_hours > 0 and age_hours > self.max_token_age_hours:
                    rejects.append((c, DecisionLabel.FILTER_AGE, f"age={age_hours:.1f}h > max"))
                    continue

            candidates.append(c)

        self._log(
            "info",
            f"Filter breakdown: chains={chain_counts}, "
            f"rejected={len(rejects)}, passed={len(candidates)}",
        )

        return candidates, rejects

    async def _exclude_held_tokens(
        self,
        candidates: List[DiscoveryCandidate],
        db: "Database",
    ) -> List[DiscoveryCandidate]:
        """Remove candidates that already have open portfolio positions."""
        sem = asyncio.Semaphore(10)

        async def _check_one(candidate: DiscoveryCandidate) -> Any:
            async with sem:
                return await db.get_open_portfolio_position(
                    candidate.token_address, candidate.chain
                )

        checks = await asyncio.gather(*[_check_one(c) for c in candidates])
        return [c for c, existing in zip(candidates, checks) if existing is None]

    async def _safety_check(
        self, candidates: List[DiscoveryCandidate]
    ) -> List[DiscoveryCandidate]:
        """Run rugcheck safety analysis on each candidate (parallel)."""
        client = self.mcp_manager.get_client("rugcheck")
        if not client:
            self._log("warning", "Rugcheck not available — skipping safety checks")
            for c in candidates:
                c.safety_status = "unverified"
            return candidates

        sem = asyncio.Semaphore(5)

        async def _check_one(candidate: DiscoveryCandidate) -> Optional[DiscoveryCandidate]:
            async with sem:
                try:
                    result = await client.call_tool(
                        "get_token_summary",
                        {"token_address": candidate.token_address},
                    )
                    status, score = self._parse_safety(result)
                    candidate.safety_status = status
                    candidate.safety_score = score

                    if status in ("Safe", "Risky", "unverified"):
                        return candidate
                    else:
                        self._log(
                            "info",
                            f"Rejected {candidate.symbol}: safety={status}",
                        )
                        return None
                except Exception as exc:
                    self._log("warning", f"Safety check failed for {candidate.symbol}: {exc}")
                    candidate.safety_status = "unverified"
                    return candidate

        results = await asyncio.gather(*[_check_one(c) for c in candidates])
        return [c for c in results if c is not None]

    @staticmethod
    def _parse_safety(result: Any) -> tuple[str, Optional[float]]:
        """Parse rugcheck response into (status, score)."""
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, ValueError):
                return "unverified", None

        if isinstance(result, list) and result and isinstance(result[0], dict):
            result = result[0]

        if not isinstance(result, dict):
            return "unverified", None

        score = result.get("score_normalised", result.get("score", 0))
        risks = result.get("risks", [])

        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0

        if score <= 500 and not risks:
            return "Safe", score
        elif score <= 2000 or len(risks) <= 2:
            return "Risky", score
        else:
            return "Dangerous", score

    async def _insider_check(
        self, candidates: List[DiscoveryCandidate]
    ) -> List[DiscoveryCandidate]:
        """Run insider/sniper detection on candidates (Solana only).

        REJECT-level candidates are filtered out. Others proceed with
        insider_analysis attached for AI enrichment.
        """
        if not self.insider_check_enabled:
            self._log("info", "Insider check disabled — skipping")
            return candidates

        if self.chain != "solana":
            self._log("info", f"Insider check not supported for chain '{self.chain}' — skipping")
            return candidates

        passed: List[DiscoveryCandidate] = []
        for candidate in candidates:
            try:
                analysis = await analyse_insiders(
                    mint=candidate.token_address,
                    rpc_url=self.rpc_url,
                    max_concentration_pct=self.insider_max_concentration_pct,
                    max_creator_pct=self.insider_max_creator_pct,
                    warn_concentration_pct=self.insider_warn_concentration_pct,
                    warn_creator_pct=self.insider_warn_creator_pct,
                )
                candidate.insider_analysis = analysis

                if analysis.risk == InsiderRisk.REJECT:
                    self._log(
                        "info",
                        f"Rejected {candidate.symbol}: insider={analysis.summary}",
                    )
                    continue

                if analysis.risk == InsiderRisk.WARN:
                    self._log(
                        "info",
                        f"Warning {candidate.symbol}: insider={analysis.summary}",
                    )

                passed.append(candidate)
            except Exception as exc:
                self._log("warning", f"Insider check failed for {candidate.symbol}: {exc}")
                # Fail-open: proceed without insider data
                passed.append(candidate)

        return passed

    async def _ai_decide(self, candidate: DiscoveryCandidate) -> tuple[bool, str]:
        """Run a per-candidate agentic loop to make a binary buy/no-buy decision.

        The model may call MCP tools to gather additional data before deciding.
        Returns (buy: bool, reasoning: str).
        Falls back to heuristic scoring if the agentic call fails or times out.
        """
        _MAX_ITERATIONS = 5
        _TIMEOUT_SECONDS = 45

        initial_message = (
            f"Should I buy {candidate.symbol} ({candidate.token_address}) on Solana?\n\n"
            f"Current data:\n"
            f"- Price: ${candidate.price_usd}\n"
            f"- 24h Volume: ${candidate.volume_24h:,.0f}\n"
            f"- Liquidity: ${candidate.liquidity_usd:,.0f}\n"
            f"- Market Cap: ${candidate.market_cap_usd:,.0f}\n"
            f"- 5m Price Change: {candidate.price_change_5m:+.2f}%\n"
            f"- 1h Price Change: {candidate.price_change_1h:+.2f}%\n"
            f"- 6h Price Change: {candidate.price_change_6h:+.2f}%\n"
            f"- 24h Price Change: {candidate.price_change_24h:+.2f}%\n"
            f"- Heuristic Score: {candidate.momentum_score:.0f}/100\n"
            f"- Safety: {candidate.safety_status}"
            + (f" (score {candidate.safety_score:.0f})" if candidate.safety_score is not None else "")
        )

        # Enrich with insider analysis if available
        if candidate.insider_analysis:
            ia = candidate.insider_analysis
            initial_message += (
                f"\n\nInsider Analysis ({ia.risk.value}):\n"
                f"- Top-10 holder concentration: {ia.top_holder_concentration_pct:.1f}%\n"
                f"- Creator holding: {ia.creator_holding_pct:.1f}%"
                + (f" ({ia.creator_address[:8]}…)" if ia.creator_address else "")
                + f"\n- Active trading holders: {ia.dumping_holders}/{ia.total_top_holders_checked}\n"
                f"- Assessment: {ia.summary}"
            )

        try:
            return await asyncio.wait_for(
                self._run_decision_loop(candidate, initial_message, _MAX_ITERATIONS),
                timeout=_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._log("warning", f"AI decision timed out for {candidate.symbol} — using heuristic fallback")
        except Exception as exc:
            self._log("error", f"AI decision failed for {candidate.symbol}: {exc} — using heuristic fallback")

        # Heuristic fallback
        score = self._heuristic_score(candidate)
        buy = score >= self.min_momentum_score
        return buy, f"Heuristic fallback (score={score:.0f}): {'buy' if buy else 'skip'}"

    async def _run_decision_loop(
        self,
        candidate: DiscoveryCandidate,
        initial_message: str,
        max_iterations: int,
    ) -> tuple[bool, str]:
        """Inner agentic loop for _ai_decide."""
        from app.tool_converter import parse_function_call_name

        gemini_client = genai.Client(api_key=self.api_key)
        tools = self.mcp_manager.get_gemini_functions_for(["dexscreener", "rugcheck"])
        tool_config = [types.Tool(functionDeclarations=tools)] if tools else None

        chat = gemini_client.chats.create(
            model=self.model_name,
            config=types.GenerateContentConfig(
                system_instruction=DECISION_SYSTEM_PROMPT,
                tools=tool_config,
            ),
        )

        response = await asyncio.to_thread(chat.send_message, initial_message)

        for _ in range(max_iterations):
            # Collect any function calls in this response
            function_calls = []
            if response.candidates:
                resp_candidate = response.candidates[0]
                if not resp_candidate.content or not resp_candidate.content.parts:
                    break
                for part in resp_candidate.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        function_calls.append(part.function_call)

            if not function_calls:
                # No more tool calls — extract the final decision
                text = ""
                if response.candidates:
                    for part in response.candidates[0].content.parts:
                        if hasattr(part, "text") and part.text:
                            text += part.text
                return self._parse_decision(text)

            # Execute tool calls and feed results back
            tool_results = []
            for fc in function_calls:
                client_name, method = parse_function_call_name(fc.name)
                args = dict(fc.args) if fc.args else {}
                if client_name == "dexscreener":
                    args = self._normalize_dexscreener_args(args)
                try:
                    mcp_client = self.mcp_manager.get_client(client_name)
                    if mcp_client:
                        result = await mcp_client.call_tool(method, args)
                        result_str = self._serialize_tool_result_for_response(result)
                    else:
                        result_str = f"Client '{client_name}' not available"
                except Exception as exc:
                    result_str = f"Tool error: {exc}"

                tool_results.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result_str},
                    )
                )
                self._log("debug", f"Tool call: {fc.name}({args}) → {result_str[:120]}…")

            response = await asyncio.to_thread(chat.send_message, tool_results)

        # Exhausted iterations — parse whatever we have
        text = ""
        if response.candidates:
            resp_candidate = response.candidates[0]
            if resp_candidate.content and resp_candidate.content.parts:
                for part in resp_candidate.content.parts:
                    if hasattr(part, "text") and part.text:
                        text += part.text
        return self._parse_decision(text)

    _MAX_TOOL_RESULT_PAYLOAD_CHARS = MAX_TOOL_RESULT_CHARS

    @staticmethod
    def _parse_optional_float(value: Any, default: float = 0.0) -> float:
        """Parse a value as float, returning default when invalid."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _serialize_tool_result_for_response(cls, result: Any) -> str:
        """Serialize tool results with a bounded payload size.

        Returns the full serialized result when it fits, or a valid JSON
        wrapper with a preview and truncation metadata.
        """
        max_chars = cls._MAX_TOOL_RESULT_PAYLOAD_CHARS

        # Fast path for strings
        if isinstance(result, str):
            if len(result) <= max_chars:
                return result
            return cls._build_truncation_wrapper(result, max_chars)

        # For large containers, estimate size from a sample before deciding
        # whether to serialize fully or just return a preview.
        if isinstance(result, (dict, list)) and len(result) > 50:
            if isinstance(result, dict):
                sample = dict(islice(result.items(), 20))
            else:
                sample = result[:20]
            try:
                sample_str = json.dumps(sample, default=str)
            except (TypeError, ValueError):
                sample_str = str(sample)
            sample_count = min(20, len(result))
            estimated_total = (len(sample_str) / sample_count) * len(result) if sample_count else 0

            if estimated_total > max_chars:
                # Likely too large — return a wrapper with truncation metadata
                wrapper: Dict[str, Any] = {
                    "truncated": True,
                    "total_items": len(result),
                    "preview_items": sample_count,
                    "preview": sample,
                }
                try:
                    wrapper_str = json.dumps(wrapper, default=str)
                except (TypeError, ValueError):
                    wrapper_str = str(wrapper)
                if len(wrapper_str) <= max_chars:
                    return wrapper_str
                return cls._build_truncation_wrapper(wrapper_str, max_chars)
            # Estimated to fit — fall through to full serialization

        # Small containers: serialize fully
        try:
            result_str = json.dumps(result, default=str)
        except (TypeError, ValueError):
            result_str = str(result)

        if len(result_str) <= max_chars:
            return result_str
        return cls._build_truncation_wrapper(result_str, max_chars)

    @classmethod
    def _build_truncation_wrapper(cls, result_str: str, max_chars: int) -> str:
        """Build a valid JSON wrapper for content that exceeds the payload limit."""
        original_length = len(result_str)
        preview_budget = max(0, max_chars // 2)
        preview = result_str[:preview_budget]
        wrapper = json.dumps({
            "truncated": True,
            "original_length": original_length,
            "preview": preview,
        }, default=str)
        while len(wrapper) > max_chars and preview_budget > 0:
            preview_budget //= 2
            wrapper = json.dumps({
                "truncated": True,
                "original_length": original_length,
                "preview": result_str[:preview_budget],
            }, default=str)
        return wrapper

    # DexScreener MCP uses camelCase parameter names. The model sometimes
    # generates snake_case (influenced by rugcheck's schema). Normalize before
    # calling the MCP server so the first attempt always succeeds.
    _DEXSCREENER_PARAM_ALIASES: Dict[str, str] = {
        "token_address": "tokenAddress",
        "chain_id": "chainId",
        "pair_address": "pairAddress",
    }

    @classmethod
    def _normalize_dexscreener_args(cls, args: Dict[str, Any]) -> Dict[str, Any]:
        """Translate any snake_case keys in dexscreener args to their camelCase equivalents."""
        return {cls._DEXSCREENER_PARAM_ALIASES.get(k, k): v for k, v in args.items()}

    @staticmethod
    def _parse_decision(text: str) -> tuple[bool, str]:
        """Parse the model's final JSON decision block.

        Expected format (last JSON block in text):
          { "buy": true/false, "reasoning": "..." }
        """
        # Try markdown code fences first (most reliable)
        fence_matches = list(re.finditer(
            r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL
        ))
        for match in reversed(fence_matches):
            try:
                data = json.loads(match.group(1))
                if "buy" in data:
                    return bool(data["buy"]), str(data.get("reasoning", "")).strip()
            except (json.JSONDecodeError, ValueError):
                continue

        # Use JSONDecoder to handle nested braces correctly
        decoder = json.JSONDecoder()
        last_match = None
        idx = 0
        while idx < len(text):
            idx = text.find('{', idx)
            if idx == -1:
                break
            try:
                data, end_idx = decoder.raw_decode(text, idx)
                if isinstance(data, dict) and "buy" in data:
                    last_match = data
                idx = end_idx
            except (json.JSONDecodeError, ValueError):
                idx += 1

        if last_match is not None:
            return bool(last_match["buy"]), str(last_match.get("reasoning", "")).strip()

        # Fallback: look for bare true/false near "buy" keyword
        lower = text.lower()
        if '"buy": true' in lower or '"buy":true' in lower:
            return True, "Decision: buy (parsed from text)"
        if '"buy": false' in lower or '"buy":false' in lower:
            return False, "Decision: skip (parsed from text)"

        # Cannot parse — conservative default
        return False, "AI response unparseable — conservative skip"

    @staticmethod
    def _heuristic_score(candidate: DiscoveryCandidate) -> float:
        """Simple fallback score when AI is unavailable."""
        score = 0.0
        # Volume/liquidity ratio (0-30)
        if candidate.liquidity_usd > 0:
            vol_ratio = candidate.volume_24h / candidate.liquidity_usd
            score += min(30.0, vol_ratio * 10)
        # Price momentum (0-30)
        if candidate.price_change_24h > 0:
            score += min(30.0, candidate.price_change_24h)
        # Liquidity depth (0-20)
        if candidate.liquidity_usd >= 50000:
            score += 20.0
        elif candidate.liquidity_usd >= 10000:
            score += 10.0
        # Safety (0-20)
        if candidate.safety_status == "Safe":
            score += 20.0
        elif candidate.safety_status in ("Risky", "unverified"):
            score += 10.0
        return min(100.0, score)
