"""Tests for portfolio discovery engine: filters, AI scoring, safety checks."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import pytest

from app.portfolio_discovery import DiscoveryCandidate, PortfolioDiscovery
from app.types import MAX_TOOL_RESULT_CHARS


# ---------------------------------------------------------------------------
# Mock clients
# ---------------------------------------------------------------------------

class MockDexScreenerClient:
    """Returns canned data for DexScreener MCP endpoints."""

    def __init__(
        self,
        pairs: Optional[List[Dict[str, Any]]] = None,
        boosted_tokens: Optional[List[Dict[str, Any]]] = None,
        pool_pairs: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        self.pairs = pairs or []
        self.boosted_tokens = boosted_tokens or []
        # pool_pairs: token_address (lower) → list of pair dicts
        self.pool_pairs = pool_pairs or {}

    async def call_tool(self, method: str, arguments: Dict[str, Any]) -> Any:
        if method == "search_pairs":
            return {"pairs": self.pairs}
        if method in ("get_top_boosted_tokens", "get_latest_boosted_tokens"):
            return self.boosted_tokens
        if method == "get_token_pools":
            addr = arguments.get("tokenAddress", "").lower()
            return self.pool_pairs.get(addr, [])
        return {}


class MockRugcheckClient:
    def __init__(self, score: float = 100.0, risks: Optional[list] = None) -> None:
        self.score = score
        self.risks = risks or []

    async def call_tool(self, method: str, arguments: Dict[str, Any]) -> Any:
        return {"score_normalised": self.score, "risks": self.risks}


class MockMCPManager:
    def __init__(
        self,
        dexscreener: Optional[MockDexScreenerClient] = None,
        rugcheck: Optional[MockRugcheckClient] = None,
    ) -> None:
        self._dexscreener = dexscreener
        self._rugcheck = rugcheck

    def get_client(self, name: str) -> Any:
        if name == "dexscreener":
            return self._dexscreener
        if name == "rugcheck":
            return self._rugcheck
        return None

    def get_gemini_functions(self) -> list:
        return []

    def get_gemini_functions_for(self, client_names: list) -> list:
        return []


class MockDatabase:
    """Mock DB that reports no open positions by default."""

    def __init__(self, held_addresses: Optional[set] = None) -> None:
        self._held = held_addresses or set()

    async def get_open_portfolio_position(self, token_address: str, chain: str) -> Any:
        if token_address.lower() in {a.lower() for a in self._held}:
            return object()  # truthy value = position exists
        return None


def _make_pair(
    address: str = "TestAddr111111111111111111111111111111111",
    symbol: str = "TEST",
    chain: str = "solana",
    price: float = 0.01,
    volume_24h: float = 50000.0,
    liquidity_usd: float = 30000.0,
    market_cap: float = 500000.0,
    price_change: float = 5.0,
    pair_created_at: Optional[int] = None,
) -> Dict[str, Any]:
    pair: Dict[str, Any] = {
        "chainId": chain,
        "baseToken": {"address": address, "symbol": symbol},
        "priceUsd": str(price),
        "volume": {"h24": volume_24h},
        "liquidity": {"usd": liquidity_usd},
        "marketCap": market_cap,
        "priceChange": {"h24": price_change},
    }
    if pair_created_at is not None:
        pair["pairCreatedAt"] = pair_created_at
    return pair


# ---------------------------------------------------------------------------
# Deterministic filter tests
# ---------------------------------------------------------------------------


class TestApplyFilters:
    """Test the deterministic pre-filter step."""

    def test_filters_by_chain(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", chain="solana",
        )
        pairs = [
            _make_pair(chain="solana", address="A1111111111111111111111111111111111111111"),
            _make_pair(chain="ethereum", address="B2222222222222222222222222222222222222222"),
        ]
        result = discovery._apply_filters(pairs)
        assert len(result) == 1
        assert result[0].chain == "solana"

    def test_filters_by_volume(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_volume_usd=50000.0,
        )
        pairs = [
            _make_pair(volume_24h=60000.0, address="A1111111111111111111111111111111111111111"),
            _make_pair(volume_24h=30000.0, address="B2222222222222222222222222222222222222222"),
        ]
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_filters_by_liquidity(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_liquidity_usd=25000.0,
        )
        pairs = [
            _make_pair(liquidity_usd=30000.0, address="A1111111111111111111111111111111111111111"),
            _make_pair(liquidity_usd=15000.0, address="B2222222222222222222222222222222222222222"),
        ]
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_filters_by_market_cap(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_market_cap_usd=250000.0,
        )
        pairs = [
            _make_pair(market_cap=300000.0, address="A1111111111111111111111111111111111111111"),
            _make_pair(market_cap=100000.0, address="B2222222222222222222222222222222222222222"),
        ]
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_filters_by_market_cap_fdv_fallback(self):
        """Should use fdv when marketCap is missing."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_market_cap_usd=250000.0,
        )
        pair = _make_pair(address="A1111111111111111111111111111111111111111")
        pair.pop("marketCap", None)
        pair["fdv"] = 300000.0
        result = discovery._apply_filters([pair])
        assert len(result) == 1

    def test_filters_zero_price(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        pairs = [
            _make_pair(price=0.0),
        ]
        result = discovery._apply_filters(pairs)
        assert len(result) == 0

    def test_deduplicates_addresses(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        addr = "DupAddr1111111111111111111111111111111111"
        pairs = [
            _make_pair(address=addr, symbol="DUP1"),
            _make_pair(address=addr, symbol="DUP2"),
        ]
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_skips_missing_address(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        pairs = [{"chainId": "solana", "baseToken": {"address": "", "symbol": "X"}}]
        result = discovery._apply_filters(pairs)
        assert len(result) == 0

    def test_filters_young_token(self):
        """Rejects a pair created 1 hour ago when min age is 4 hours."""
        import time
        now_ms = int(time.time() * 1000)
        one_hour_ago_ms = now_ms - int(1 * 3_600 * 1_000)
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_token_age_hours=4.0,
        )
        pairs = [_make_pair(pair_created_at=one_hour_ago_ms)]
        result = discovery._apply_filters(pairs)
        assert len(result) == 0

    def test_passes_old_token(self):
        """Allows a pair created 8 hours ago when min age is 4 hours."""
        import time
        now_ms = int(time.time() * 1000)
        eight_hours_ago_ms = now_ms - int(8 * 3_600 * 1_000)
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_token_age_hours=4.0,
        )
        pairs = [_make_pair(pair_created_at=eight_hours_ago_ms)]
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_passes_missing_pair_created_at(self):
        """Passes a pair with no pairCreatedAt field (permissive fallback)."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_token_age_hours=4.0,
        )
        pairs = [_make_pair()]  # no pair_created_at kwarg → field absent
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_parses_price_change_windows(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        pair = _make_pair(address="PriceWin1111111111111111111111111111111111")
        pair["priceChange"] = {"m5": 0.5, "h1": 1.5, "h6": 3.5, "h24": 7.5}

        result = discovery._apply_filters([pair])

        assert len(result) == 1
        candidate = result[0]
        assert candidate.price_change_5m == 0.5
        assert candidate.price_change_1h == 1.5
        assert candidate.price_change_6h == 3.5
        assert candidate.price_change_24h == 7.5

    def test_defaults_invalid_price_change_windows_in_dict(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        pair = _make_pair(address="PriceBad1111111111111111111111111111111111")
        pair["priceChange"] = {"m5": "bad", "h1": None, "h6": "3.5", "h24": "invalid"}

        result = discovery._apply_filters([pair])

        assert len(result) == 1
        candidate = result[0]
        assert candidate.price_change_5m == 0.0
        assert candidate.price_change_1h == 0.0
        assert candidate.price_change_6h == 3.5
        assert candidate.price_change_24h == 0.0

    @pytest.mark.parametrize("price_change_value", [None, "invalid"])
    def test_handles_null_or_non_dict_price_change(self, price_change_value):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        pair = _make_pair(address="PriceNull111111111111111111111111111111111")
        pair["priceChange"] = price_change_value

        result = discovery._apply_filters([pair])

        assert len(result) == 1
        candidate = result[0]
        assert candidate.price_change_5m == 0.0
        assert candidate.price_change_1h == 0.0
        assert candidate.price_change_6h == 0.0
        assert candidate.price_change_24h == 0.0


# ---------------------------------------------------------------------------
# Max token age filter
# ---------------------------------------------------------------------------


class TestMaxTokenAgeFilter:
    def test_rejects_old_token(self):
        """Rejects a pair created 50 hours ago when max age is 24 hours."""
        import time
        now_ms = int(time.time() * 1000)
        fifty_hours_ago_ms = now_ms - int(50 * 3_600 * 1_000)
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", max_token_age_hours=24.0,
        )
        pairs = [_make_pair(pair_created_at=fifty_hours_ago_ms)]
        result = discovery._apply_filters(pairs)
        assert len(result) == 0

    def test_passes_young_enough_token(self):
        """Allows a pair created 5 hours ago when max age is 24 hours."""
        import time
        now_ms = int(time.time() * 1000)
        five_hours_ago_ms = now_ms - int(5 * 3_600 * 1_000)
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", max_token_age_hours=24.0,
        )
        pairs = [_make_pair(pair_created_at=five_hours_ago_ms)]
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_passes_when_disabled(self):
        """When max_token_age_hours=0, no upper bound is applied."""
        import time
        now_ms = int(time.time() * 1000)
        very_old_ms = now_ms - int(10_000 * 3_600 * 1_000)
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", max_token_age_hours=0.0,
        )
        pairs = [_make_pair(pair_created_at=very_old_ms)]
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_passes_missing_pair_created_at(self):
        """Passes a pair with no pairCreatedAt field (permissive fallback)."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", max_token_age_hours=24.0,
        )
        pairs = [_make_pair()]  # no pair_created_at kwarg → field absent
        result = discovery._apply_filters(pairs)
        assert len(result) == 1

    def test_raises_when_min_exceeds_max(self):
        """Raises ValueError if min_token_age_hours > max_token_age_hours (both set)."""
        import pytest
        with pytest.raises(ValueError, match="min_token_age_hours"):
            PortfolioDiscovery(
                mcp_manager=MockMCPManager(), api_key="x",
                min_token_age_hours=48.0,
                max_token_age_hours=24.0,
            )

    def test_rejects_future_timestamp(self):
        """Rejects a pair whose pairCreatedAt is in the future (negative age)."""
        import time
        now_ms = int(time.time() * 1000)
        future_ms = now_ms + int(10 * 3_600 * 1_000)
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", max_token_age_hours=24.0,
        )
        pairs = [_make_pair(pair_created_at=future_ms)]
        result = discovery._apply_filters(pairs)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Held token exclusion
# ---------------------------------------------------------------------------


class TestExcludeHeldTokens:
    @pytest.mark.asyncio
    async def test_excludes_held(self):
        held_addr = "HeldToken111111111111111111111111111111111"
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        candidates = [
            DiscoveryCandidate(
                token_address=held_addr, symbol="HELD", chain="solana",
                price_usd=1.0, volume_24h=50000, liquidity_usd=20000,
            ),
            DiscoveryCandidate(
                token_address="FreeToken111111111111111111111111111111111",
                symbol="FREE", chain="solana",
                price_usd=1.0, volume_24h=50000, liquidity_usd=20000,
            ),
        ]
        db = MockDatabase(held_addresses={held_addr})

        result = await discovery._exclude_held_tokens(candidates, db)

        assert len(result) == 1
        assert result[0].symbol == "FREE"


# ---------------------------------------------------------------------------
# Discovery pipeline behavior
# ---------------------------------------------------------------------------


class TestDiscoverPipeline:
    @pytest.mark.asyncio
    async def test_prefilter_skips_low_scores_and_dedupes_before_ai(self, monkeypatch):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=50.0,
        )
        strong_primary = DiscoveryCandidate(
            token_address="DupAddr111111111111111111111111111111111111",
            symbol="GOOD1",
            chain="solana",
            price_usd=0.01,
            volume_24h=100000.0,
            liquidity_usd=50000.0,
            price_change_24h=10.0,
            safety_status="Safe",
        )
        strong_duplicate = DiscoveryCandidate(
            token_address="DupAddr111111111111111111111111111111111111",
            symbol="GOOD2",
            chain="solana",
            price_usd=0.02,
            volume_24h=120000.0,
            liquidity_usd=60000.0,
            price_change_24h=12.0,
            safety_status="Safe",
        )
        weak = DiscoveryCandidate(
            token_address="WeakAddr11111111111111111111111111111111111",
            symbol="WEAK",
            chain="solana",
            price_usd=0.001,
            volume_24h=1000.0,
            liquidity_usd=20000.0,
            price_change_24h=-5.0,
            safety_status="Dangerous",
        )
        ai_called_symbols: List[str] = []

        async def _scan_trending() -> List[Dict[str, Any]]:
            return [{"pair": "placeholder"}]

        def _apply_filters_with_labels(_pairs):
            return [strong_primary, strong_duplicate, weak], []

        async def _exclude_held_tokens(
            candidates: List[DiscoveryCandidate], _db: MockDatabase
        ) -> List[DiscoveryCandidate]:
            return candidates

        async def _safety_check(
            candidates: List[DiscoveryCandidate],
        ) -> List[DiscoveryCandidate]:
            return candidates

        async def _insider_check(
            candidates: List[DiscoveryCandidate],
        ) -> List[DiscoveryCandidate]:
            return candidates

        async def _ai_decide(candidate: DiscoveryCandidate) -> tuple[bool, str]:
            ai_called_symbols.append(candidate.symbol)
            return True, "buy"

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)
        monkeypatch.setattr(discovery, "_exclude_held_tokens", _exclude_held_tokens)
        monkeypatch.setattr(discovery, "_safety_check", _safety_check)
        monkeypatch.setattr(discovery, "_insider_check", _insider_check)
        monkeypatch.setattr(discovery, "_ai_decide", _ai_decide)

        result = await discovery.discover(MockDatabase(), max_candidates=5)

        assert ai_called_symbols == ["GOOD1"]
        assert len(result) == 1
        assert result[0].symbol == "GOOD1"

    @pytest.mark.asyncio
    async def test_parallel_decisions_cap_ai_launches_relative_to_max_candidates(self, monkeypatch):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=40.0,
        )
        symbols = list("ABCDEFGH")
        candidates = [
            DiscoveryCandidate(
                token_address=f"Addr{symbol}{idx:02d}111111111111111111111111111111111111",
                symbol=symbol,
                chain="solana",
                price_usd=0.01,
                volume_24h=110000.0 - (idx * 2000.0),
                liquidity_usd=50000.0,
                price_change_24h=16.0 - idx,
                safety_status="Safe",
            )
            for idx, symbol in enumerate(symbols)
        ]
        decisions = {
            symbol: (symbol in {"A", "C", "H"}, f"decision {symbol}")
            for symbol in symbols
        }
        called_symbols: List[str] = []

        async def _scan_trending() -> List[Dict[str, Any]]:
            return [{"pair": "placeholder"}]

        def _apply_filters_with_labels(_pairs):
            return candidates, []

        async def _exclude_held_tokens(
            current: List[DiscoveryCandidate], _db: MockDatabase
        ) -> List[DiscoveryCandidate]:
            return current

        async def _safety_check(
            current: List[DiscoveryCandidate],
        ) -> List[DiscoveryCandidate]:
            return current

        async def _insider_check(
            current: List[DiscoveryCandidate],
        ) -> List[DiscoveryCandidate]:
            return current

        async def _ai_decide(candidate: DiscoveryCandidate) -> tuple[bool, str]:
            called_symbols.append(candidate.symbol)
            return decisions[candidate.symbol]

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)
        monkeypatch.setattr(discovery, "_exclude_held_tokens", _exclude_held_tokens)
        monkeypatch.setattr(discovery, "_safety_check", _safety_check)
        monkeypatch.setattr(discovery, "_insider_check", _insider_check)
        monkeypatch.setattr(discovery, "_ai_decide", _ai_decide)

        result = await discovery.discover(MockDatabase(), max_candidates=2)

        assert sorted(called_symbols) == ["A", "B", "C", "D", "E", "F"]
        assert len(called_symbols) == 6
        assert [c.symbol for c in result] == ["A", "C"]

    @pytest.mark.asyncio
    async def test_negative_max_candidates_short_circuits_before_scan(self, monkeypatch):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )

        async def _scan_trending() -> List[Dict[str, Any]]:
            raise AssertionError("scan should not run when max_candidates <= 0")

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)

        result = await discovery.discover(MockDatabase(), max_candidates=-1)

        assert result == []


# ---------------------------------------------------------------------------
# Safety check parsing
# ---------------------------------------------------------------------------


class TestParseSafety:
    def test_safe_token(self):
        status, score = PortfolioDiscovery._parse_safety(
            {"score_normalised": 200, "risks": []}
        )
        assert status == "Safe"
        assert score == 200.0

    def test_risky_token(self):
        status, score = PortfolioDiscovery._parse_safety(
            {"score_normalised": 1500, "risks": ["one", "two"]}
        )
        assert status == "Risky"
        assert score == 1500.0

    def test_dangerous_token(self):
        status, score = PortfolioDiscovery._parse_safety(
            {"score_normalised": 5000, "risks": ["a", "b", "c"]}
        )
        assert status == "Dangerous"
        assert score == 5000.0

    def test_string_json_input(self):
        status, score = PortfolioDiscovery._parse_safety(
            json.dumps({"score_normalised": 100, "risks": []})
        )
        assert status == "Safe"

    def test_list_input(self):
        status, score = PortfolioDiscovery._parse_safety(
            [{"score_normalised": 300, "risks": []}]
        )
        assert status == "Safe"

    def test_invalid_string(self):
        status, score = PortfolioDiscovery._parse_safety("not json")
        assert status == "unverified"
        assert score is None


# ---------------------------------------------------------------------------
# AI score parsing
# ---------------------------------------------------------------------------


class TestParseDecision:
    def test_parses_buy_true(self):
        text = '{"buy": true, "reasoning": "Strong volume surge, safe token"}'
        buy, reasoning = PortfolioDiscovery._parse_decision(text)
        assert buy is True
        assert "Strong volume" in reasoning

    def test_parses_buy_false(self):
        text = '{"buy": false, "reasoning": "Negative momentum, low volume"}'
        buy, reasoning = PortfolioDiscovery._parse_decision(text)
        assert buy is False
        assert "Negative" in reasoning

    def test_parses_from_code_block_with_surrounding_text(self):
        text = (
            "I have analysed the token.\n"
            "```json\n"
            '{"buy": true, "reasoning": "Good metrics"}\n'
            "```"
        )
        buy, reasoning = PortfolioDiscovery._parse_decision(text)
        assert buy is True
        assert reasoning == "Good metrics"

    def test_uses_last_json_block(self):
        """When the model emits multiple JSON blocks, use the last one."""
        text = (
            '{"buy": false, "reasoning": "initial thought"}\n'
            "After further investigation:\n"
            '{"buy": true, "reasoning": "final decision"}'
        )
        buy, reasoning = PortfolioDiscovery._parse_decision(text)
        assert buy is True
        assert reasoning == "final decision"

    def test_fallback_bare_buy_true(self):
        text = 'The answer is "buy": true for this token.'
        buy, _ = PortfolioDiscovery._parse_decision(text)
        assert buy is True

    def test_fallback_bare_buy_false(self):
        text = 'Decision: "buy": false — skip this token.'
        buy, _ = PortfolioDiscovery._parse_decision(text)
        assert buy is False

    def test_conservative_skip_on_unparseable(self):
        buy, reasoning = PortfolioDiscovery._parse_decision("I cannot decide.")
        assert buy is False
        assert "unparseable" in reasoning.lower() or "conservative" in reasoning.lower()

    def test_empty_string_returns_skip(self):
        buy, _ = PortfolioDiscovery._parse_decision("")
        assert buy is False

    def test_parses_nested_json_in_code_fence(self):
        """Fence regex captures full content including nested objects."""
        text = (
            "Here is my analysis:\n"
            "```json\n"
            '{"buy": true, "reasoning": "Strong metrics", "metadata": {"score": 85, "confidence": "high"}}\n'
            "```"
        )
        buy, reasoning = PortfolioDiscovery._parse_decision(text)
        assert buy is True
        assert reasoning == "Strong metrics"

    def test_parses_deeply_nested_json(self):
        """JSONDecoder handles multiple nesting levels correctly."""
        text = (
            'After analysis: '
            '{"buy": false, "reasoning": "Weak", "details": {"risks": [{"type": "rug", "level": 3}], "scores": {"safety": 20}}}'
        )
        buy, reasoning = PortfolioDiscovery._parse_decision(text)
        assert buy is False
        assert reasoning == "Weak"

    def test_parses_json_with_array_values(self):
        """JSONDecoder handles JSON containing arrays with objects."""
        text = '{"buy": true, "reasoning": "Good", "tokens": [{"symbol": "SOL"}, {"symbol": "USDC"}]}'
        buy, reasoning = PortfolioDiscovery._parse_decision(text)
        assert buy is True
        assert reasoning == "Good"

    def test_fence_with_nested_braces_not_truncated(self):
        """Regression: old regex with .*? stopped at first }, truncating nested JSON."""
        text = (
            "```json\n"
            '{"buy": true, "reasoning": "Volume looks great", "extra": {"nested": {"deep": true}}}\n'
            "```\n"
            "That's my final answer."
        )
        buy, reasoning = PortfolioDiscovery._parse_decision(text)
        assert buy is True
        assert reasoning == "Volume looks great"


# ---------------------------------------------------------------------------
# Agentic decision (_ai_decide) — heuristic fallback path
# ---------------------------------------------------------------------------


class TestAiDecideHeuristicFallback:
    """Test that _ai_decide falls back to heuristic when the AI call fails."""

    @pytest.mark.asyncio
    async def test_fallback_on_api_error(self, monkeypatch):
        """When genai raises, heuristic fallback is used."""
        import app.portfolio_discovery as pd_module

        def _bad_client(*args, **kwargs):
            raise RuntimeError("API unavailable")

        monkeypatch.setattr(pd_module.genai, "Client", _bad_client)

        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(),
            api_key="x",
            min_momentum_score=50.0,
        )
        candidate = DiscoveryCandidate(
            token_address="Addr1111111111111111111111111111111111111",
            symbol="TKN",
            chain="solana",
            price_usd=0.01,
            volume_24h=100000,
            liquidity_usd=50000,
            price_change_24h=15.0,
            safety_status="Safe",
        )
        buy, reasoning = await discovery._ai_decide(candidate)
        assert isinstance(buy, bool)
        assert "fallback" in reasoning.lower()

    @pytest.mark.asyncio
    async def test_heuristic_rejects_weak_candidate(self, monkeypatch):
        """A weak candidate is rejected by the heuristic fallback."""
        import app.portfolio_discovery as pd_module

        def _bad_client(*args, **kwargs):
            raise RuntimeError("API unavailable")

        monkeypatch.setattr(pd_module.genai, "Client", _bad_client)

        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(),
            api_key="x",
            min_momentum_score=50.0,
        )
        candidate = DiscoveryCandidate(
            token_address="Weak111111111111111111111111111111111111",
            symbol="WEAK",
            chain="solana",
            price_usd=0.001,
            volume_24h=1000,
            liquidity_usd=500,
            price_change_24h=-10.0,
            safety_status="Dangerous",
        )
        buy, _ = await discovery._ai_decide(candidate)
        assert buy is False


# ---------------------------------------------------------------------------
# DexScreener parameter normalization
# ---------------------------------------------------------------------------


class TestNormalizeDexscreenerArgs:
    def test_normalizes_token_address(self):
        args = {"token_address": "abc123", "chainId": "solana"}
        result = PortfolioDiscovery._normalize_dexscreener_args(args)
        assert result == {"tokenAddress": "abc123", "chainId": "solana"}

    def test_normalizes_chain_id(self):
        args = {"tokenAddress": "abc123", "chain_id": "solana"}
        result = PortfolioDiscovery._normalize_dexscreener_args(args)
        assert result == {"tokenAddress": "abc123", "chainId": "solana"}

    def test_normalizes_pair_address(self):
        args = {"pair_address": "pair123"}
        result = PortfolioDiscovery._normalize_dexscreener_args(args)
        assert result == {"pairAddress": "pair123"}

    def test_already_camel_case_unchanged(self):
        args = {"tokenAddress": "abc", "chainId": "solana"}
        result = PortfolioDiscovery._normalize_dexscreener_args(args)
        assert result == {"tokenAddress": "abc", "chainId": "solana"}

    def test_unknown_keys_passed_through(self):
        args = {"query": "trending solana"}
        result = PortfolioDiscovery._normalize_dexscreener_args(args)
        assert result == {"query": "trending solana"}

    def test_empty_args_unchanged(self):
        assert PortfolioDiscovery._normalize_dexscreener_args({}) == {}

    def test_all_aliases_at_once(self):
        args = {"token_address": "t", "chain_id": "solana", "pair_address": "p"}
        result = PortfolioDiscovery._normalize_dexscreener_args(args)
        assert result == {"tokenAddress": "t", "chainId": "solana", "pairAddress": "p"}


class TestSerializeToolResultForResponse:
    def test_truncates_long_result_payload(self):
        result = {"blob": "x" * 9000}
        serialized = PortfolioDiscovery._serialize_tool_result_for_response(result)

        assert len(serialized) <= MAX_TOOL_RESULT_CHARS
        parsed = json.loads(serialized)
        assert parsed["truncated"] is True
        assert "original_length" in parsed
        assert "preview" in parsed

    def test_returns_small_result_unchanged(self):
        result = {"key": "value"}
        serialized = PortfolioDiscovery._serialize_tool_result_for_response(result)
        assert serialized == json.dumps(result, default=str)

    def test_large_container_with_small_items_serializes_fully(self):
        """Large containers estimated to fit should be serialized fully, not previewed."""
        result = {f"k{i}": i for i in range(100)}
        serialized = PortfolioDiscovery._serialize_tool_result_for_response(result)
        parsed = json.loads(serialized)
        assert len(parsed) == 100

    def test_large_container_with_big_items_returns_preview(self):
        """Large containers estimated to exceed limit should return a wrapper with metadata."""
        result = {f"key{i}": "x" * 80 for i in range(200)}
        serialized = PortfolioDiscovery._serialize_tool_result_for_response(result)

        assert len(serialized) <= MAX_TOOL_RESULT_CHARS
        parsed = json.loads(serialized)
        assert parsed["truncated"] is True
        assert parsed["total_items"] == 200
        assert "preview_items" in parsed
        assert "preview" in parsed

    def test_string_truncation_produces_valid_json(self):
        long_string = "x" * 10000
        serialized = PortfolioDiscovery._serialize_tool_result_for_response(long_string)

        assert len(serialized) <= MAX_TOOL_RESULT_CHARS
        parsed = json.loads(serialized)
        assert parsed["truncated"] is True





class TestHeuristicScore:
    def test_strong_candidate(self):
        c = DiscoveryCandidate(
            token_address="x", symbol="X", chain="solana",
            price_usd=1.0, volume_24h=100000, liquidity_usd=50000,
            price_change_24h=20.0, safety_status="Safe",
        )
        score = PortfolioDiscovery._heuristic_score(c)
        assert score >= 50.0  # Should be a decent score

    def test_weak_candidate(self):
        c = DiscoveryCandidate(
            token_address="x", symbol="X", chain="solana",
            price_usd=1.0, volume_24h=5000, liquidity_usd=3000,
            price_change_24h=-5.0, safety_status="Dangerous",
        )
        score = PortfolioDiscovery._heuristic_score(c)
        assert score < 50.0

    def test_capped_at_100(self):
        c = DiscoveryCandidate(
            token_address="x", symbol="X", chain="solana",
            price_usd=1.0, volume_24h=1000000, liquidity_usd=100000,
            price_change_24h=100.0, safety_status="Safe",
        )
        score = PortfolioDiscovery._heuristic_score(c)
        assert score == 100.0


# ---------------------------------------------------------------------------
# Extract pairs
# ---------------------------------------------------------------------------


class TestExtractPairs:
    def test_from_dict_with_pairs(self):
        result = PortfolioDiscovery._extract_pairs(
            {"pairs": [{"a": 1}, {"b": 2}]}
        )
        assert len(result) == 2

    def test_from_dict_with_results(self):
        result = PortfolioDiscovery._extract_pairs(
            {"results": [{"a": 1}]}
        )
        assert len(result) == 1

    def test_from_list(self):
        result = PortfolioDiscovery._extract_pairs([{"a": 1}])
        assert len(result) == 1

    def test_from_string(self):
        result = PortfolioDiscovery._extract_pairs("invalid")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Boosted token extraction
# ---------------------------------------------------------------------------


class TestExtractBoostedTokens:
    def test_extracts_from_list(self):
        data = [
            {"tokenAddress": "addr1", "chainId": "solana"},
            {"tokenAddress": "addr2", "chainId": "ethereum"},
        ]
        result = PortfolioDiscovery._extract_boosted_tokens(data)
        assert len(result) == 2

    def test_extracts_from_wrapped_dict(self):
        data = {"tokens": [{"tokenAddress": "addr1", "chainId": "solana"}]}
        result = PortfolioDiscovery._extract_boosted_tokens(data)
        assert len(result) == 1

    def test_skips_entries_without_address(self):
        data = [{"chainId": "solana"}, {"tokenAddress": "addr1", "chainId": "solana"}]
        result = PortfolioDiscovery._extract_boosted_tokens(data)
        assert len(result) == 1

    def test_returns_empty_for_string(self):
        assert PortfolioDiscovery._extract_boosted_tokens("invalid") == []

    def test_returns_empty_for_empty_list(self):
        assert PortfolioDiscovery._extract_boosted_tokens([]) == []


# ---------------------------------------------------------------------------
# Boosted token discovery integration
# ---------------------------------------------------------------------------


class TestFetchBoostedTokens:
    @pytest.mark.asyncio
    async def test_filters_by_chain(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", chain="solana",
        )
        client = MockDexScreenerClient(boosted_tokens=[
            {"tokenAddress": "SolToken111", "chainId": "solana"},
            {"tokenAddress": "EthToken111", "chainId": "ethereum"},
        ])
        tokens = await discovery._fetch_boosted_tokens(client)
        assert len(tokens) == 1
        assert tokens[0]["tokenAddress"] == "SolToken111"

    @pytest.mark.asyncio
    async def test_deduplicates_across_endpoints(self):
        """Same token from both boosted endpoints should appear once."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", chain="solana",
        )
        client = MockDexScreenerClient(boosted_tokens=[
            {"tokenAddress": "DupAddr1111", "chainId": "solana"},
            {"tokenAddress": "DupAddr1111", "chainId": "solana"},
            {"tokenAddress": "Unique11111", "chainId": "solana"},
        ])
        tokens = await discovery._fetch_boosted_tokens(client)
        assert len(tokens) == 2


class TestFetchPairsForTokens:
    @pytest.mark.asyncio
    async def test_selects_highest_liquidity_pair(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", chain="solana",
        )
        pool_pairs = {
            "addr1": [
                _make_pair(address="addr1", liquidity_usd=5000),
                _make_pair(address="addr1", liquidity_usd=50000),
                _make_pair(address="addr1", liquidity_usd=10000),
            ]
        }
        client = MockDexScreenerClient(pool_pairs=pool_pairs)
        tokens = [{"tokenAddress": "addr1", "chainId": "solana"}]
        pairs = await discovery._fetch_pairs_for_tokens(client, tokens)
        assert len(pairs) == 1
        assert float(pairs[0]["liquidity"]["usd"]) == 50000

    @pytest.mark.asyncio
    async def test_handles_empty_pools(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", chain="solana",
        )
        client = MockDexScreenerClient(pool_pairs={})
        tokens = [{"tokenAddress": "nopool", "chainId": "solana"}]
        pairs = await discovery._fetch_pairs_for_tokens(client, tokens)
        assert len(pairs) == 0


class TestScanTrendingIntegration:
    @pytest.mark.asyncio
    async def test_merges_boosted_and_search_results(self):
        """Boosted tokens + search results are merged and deduplicated."""
        boosted_addr = "BoostedToken1111111111111111111111111111111"
        search_addr = "SearchToken11111111111111111111111111111111"

        boosted_pair = _make_pair(address=boosted_addr, symbol="BOOST", volume_24h=100000)
        search_pair = _make_pair(address=search_addr, symbol="SRCH", volume_24h=50000)

        client = MockDexScreenerClient(
            pairs=[search_pair],
            boosted_tokens=[{"tokenAddress": boosted_addr, "chainId": "solana"}],
            pool_pairs={boosted_addr.lower(): [boosted_pair]},
        )
        manager = MockMCPManager(dexscreener=client)
        discovery = PortfolioDiscovery(
            mcp_manager=manager, api_key="x", chain="solana",
        )
        pairs = await discovery._scan_trending()
        addresses = {(p.get("baseToken") or {}).get("address", "") for p in pairs}
        assert boosted_addr in addresses
        assert search_addr in addresses

    @pytest.mark.asyncio
    async def test_deduplicates_across_sources(self):
        """Token appearing in both boosted and search results appears once."""
        addr = "SharedToken11111111111111111111111111111111"
        pair = _make_pair(address=addr, symbol="SHARED")

        client = MockDexScreenerClient(
            pairs=[pair],
            boosted_tokens=[{"tokenAddress": addr, "chainId": "solana"}],
            pool_pairs={addr.lower(): [pair]},
        )
        manager = MockMCPManager(dexscreener=client)
        discovery = PortfolioDiscovery(
            mcp_manager=manager, api_key="x", chain="solana",
        )
        pairs = await discovery._scan_trending()
        assert len(pairs) == 1

    @pytest.mark.asyncio
    async def test_works_without_boosted_tokens(self):
        """Falls back to search_pairs when no boosted tokens exist."""
        addr = "SearchOnly111111111111111111111111111111111"
        pair = _make_pair(address=addr, symbol="ONLY")

        client = MockDexScreenerClient(pairs=[pair], boosted_tokens=[])
        manager = MockMCPManager(dexscreener=client)
        discovery = PortfolioDiscovery(
            mcp_manager=manager, api_key="x", chain="solana",
        )
        pairs = await discovery._scan_trending()
        assert len(pairs) == 1


# ---------------------------------------------------------------------------
# Insider check integration tests
# ---------------------------------------------------------------------------


class TestInsiderCheckIntegration:
    """Tests for the _insider_check step in the discovery pipeline."""

    def test_insider_check_enabled_without_rpc_url_disables_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.portfolio_discovery"):
            discovery = PortfolioDiscovery(
                mcp_manager=MockMCPManager(),
                api_key="x",
                insider_check_enabled=True,
            )
        assert discovery.insider_check_enabled is False
        assert "Insider check disabled" in caplog.text

    @pytest.mark.asyncio
    async def test_insider_check_disabled(self):
        """When disabled, all candidates pass through unchanged."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
            insider_check_enabled=False,
            verbose=True, log_callback=lambda *a: None,
        )
        candidates = [
            DiscoveryCandidate(
                token_address="A1111111111111111111111111111111111111111",
                symbol="A", chain="solana", price_usd=1.0,
                volume_24h=100000.0, liquidity_usd=50000.0,
            ),
        ]
        result = await discovery._insider_check(candidates)
        assert len(result) == 1
        assert result[0].insider_analysis is None

    @pytest.mark.asyncio
    async def test_insider_check_reject_filters_candidate(self):
        """Candidates with REJECT risk should be filtered out."""
        from unittest.mock import patch, AsyncMock
        from app.insider_detection import InsiderAnalysis, InsiderRisk

        reject_analysis = InsiderAnalysis(
            risk=InsiderRisk.REJECT,
            top_holder_concentration_pct=65.0,
            summary="top-10 hold 65.0% (>50%)",
        )

        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
            insider_check_enabled=True,
            rpc_url="https://test-rpc",
            verbose=True, log_callback=lambda *a: None,
        )
        candidates = [
            DiscoveryCandidate(
                token_address="REJECT111111111111111111111111111111111111",
                symbol="BAD", chain="solana", price_usd=1.0,
                volume_24h=100000.0, liquidity_usd=50000.0,
            ),
        ]

        with patch(
            "app.portfolio_discovery.analyse_insiders",
            new_callable=AsyncMock,
            return_value=reject_analysis,
        ):
            result = await discovery._insider_check(candidates)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_insider_check_warn_passes_with_data(self):
        """WARN candidates should pass through with insider data attached."""
        from unittest.mock import patch, AsyncMock
        from app.insider_detection import InsiderAnalysis, InsiderRisk

        warn_analysis = InsiderAnalysis(
            risk=InsiderRisk.WARN,
            top_holder_concentration_pct=35.0,
            summary="top-10 hold 35.0% (>30%)",
        )

        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
            insider_check_enabled=True,
            rpc_url="https://test-rpc",
            verbose=True, log_callback=lambda *a: None,
        )
        candidates = [
            DiscoveryCandidate(
                token_address="WARN1111111111111111111111111111111111111",
                symbol="MEH", chain="solana", price_usd=1.0,
                volume_24h=100000.0, liquidity_usd=50000.0,
            ),
        ]

        with patch(
            "app.portfolio_discovery.analyse_insiders",
            new_callable=AsyncMock,
            return_value=warn_analysis,
        ):
            result = await discovery._insider_check(candidates)

        assert len(result) == 1
        assert result[0].insider_analysis is not None
        assert result[0].insider_analysis.risk == InsiderRisk.WARN

    @pytest.mark.asyncio
    async def test_insider_check_error_fails_open(self):
        """RPC errors should not block candidates (fail-open)."""
        from unittest.mock import patch, AsyncMock

        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
            insider_check_enabled=True,
            rpc_url="https://test-rpc",
            verbose=True, log_callback=lambda *a: None,
        )
        candidates = [
            DiscoveryCandidate(
                token_address="ERR11111111111111111111111111111111111111",
                symbol="ERR", chain="solana", price_usd=1.0,
                volume_24h=100000.0, liquidity_usd=50000.0,
            ),
        ]

        with patch(
            "app.portfolio_discovery.analyse_insiders",
            new_callable=AsyncMock,
            side_effect=Exception("RPC timeout"),
        ):
            result = await discovery._insider_check(candidates)

        assert len(result) == 1
        assert result[0].insider_analysis is None

    @pytest.mark.asyncio
    async def test_insider_check_skips_non_solana_chain(self):
        """Insider check should skip for non-Solana chains even when enabled."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
            chain="ethereum",
            insider_check_enabled=True,
            verbose=True, log_callback=lambda *a: None,
        )
        candidates = [
            DiscoveryCandidate(
                token_address="0xabc123",
                symbol="ETH_TOKEN", chain="ethereum", price_usd=1.0,
                volume_24h=100000.0, liquidity_usd=50000.0,
            ),
        ]
        result = await discovery._insider_check(candidates)
        assert len(result) == 1
        assert result[0].insider_analysis is None
