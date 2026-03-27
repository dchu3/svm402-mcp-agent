"""Tests for shadow audit, decision labels, and discovery decision logging."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

from app.database import Database
from app.portfolio_discovery import DiscoveryCandidate, PortfolioDiscovery
from app.types import DecisionLabel


# ---------------------------------------------------------------------------
# Mock clients
# ---------------------------------------------------------------------------


class MockMCPClient:
    async def call_tool(self, method: str, args: dict) -> Any:
        return []


class MockMCPManager:
    def __init__(self, clients: Optional[Dict[str, Any]] = None) -> None:
        self._clients = clients or {}

    def get_client(self, name: str) -> Any:
        return self._clients.get(name)

    def get_gemini_functions_for(self, client_names: list) -> list:
        return []


class MockDatabase:
    """Mock DB that tracks decision and shadow recording calls."""

    def __init__(self, held_addresses: Optional[set] = None) -> None:
        self._held = held_addresses or set()
        self.recorded_decisions: List[tuple] = []
        self.shadow_positions: List[Dict[str, Any]] = []

    async def get_open_portfolio_position(self, token_address: str, chain: str) -> Any:
        if token_address.lower() in {a.lower() for a in self._held}:
            return object()
        return None

    async def record_discovery_decisions_batch(self, decisions: List[tuple]) -> None:
        self.recorded_decisions.extend(decisions)

    async def add_shadow_position(self, **kwargs) -> int:
        self.shadow_positions.append(kwargs)
        return len(self.shadow_positions)


def _make_pair(
    address: str = "TestAddr111111111111111111111111111111111",
    symbol: str = "TEST",
    chain: str = "solana",
    price: float = 0.01,
    volume_24h: float = 50000.0,
    liquidity_usd: float = 30000.0,
    market_cap: float = 500000.0,
    price_change: float = 5.0,
) -> Dict[str, Any]:
    return {
        "chainId": chain,
        "baseToken": {"address": address, "symbol": symbol},
        "priceUsd": str(price),
        "volume": {"h24": volume_24h},
        "liquidity": {"usd": liquidity_usd},
        "marketCap": market_cap,
        "priceChange": {"h24": price_change},
    }


# ---------------------------------------------------------------------------
# Decision label tests
# ---------------------------------------------------------------------------


class TestDecisionLabels:
    """Verify that candidates are tagged with correct DecisionLabels."""

    def test_decision_label_values(self):
        """DecisionLabel enum has expected values."""
        assert DecisionLabel.FILTER_VOLUME.value == "filter_volume"
        assert DecisionLabel.AI_APPROVE.value == "ai_approve"
        assert DecisionLabel.HEURISTIC_SKIP.value == "heuristic_skip"
        assert DecisionLabel.SAFETY_REJECTED.value == "safety_rejected"

    def test_candidate_default_label_is_none(self):
        c = DiscoveryCandidate(
            token_address="abc", symbol="X", chain="solana",
            price_usd=1, volume_24h=1000, liquidity_usd=1000,
        )
        assert c.decision_label is None

    def test_candidate_label_assignable(self):
        c = DiscoveryCandidate(
            token_address="abc", symbol="X", chain="solana",
            price_usd=1, volume_24h=1000, liquidity_usd=1000,
        )
        c.decision_label = DecisionLabel.AI_APPROVE
        assert c.decision_label == DecisionLabel.AI_APPROVE

    @pytest.mark.asyncio
    async def test_discover_labels_approved_candidates(self, monkeypatch):
        """Approved candidates get AI_APPROVE label."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=10.0,
        )
        candidate = DiscoveryCandidate(
            token_address="Addr111111111111111111111111111111111111111",
            symbol="GOOD", chain="solana", price_usd=0.01,
            volume_24h=100000, liquidity_usd=50000, safety_status="Safe",
            price_change_24h=10.0,
        )

        async def _scan_trending():
            return [{"pair": "x"}]

        def _apply_filters_with_labels(_pairs):
            return [candidate], []

        async def _exclude_held_tokens(candidates, _db):
            return candidates

        async def _safety_check(candidates):
            return candidates

        async def _insider_check(candidates):
            return candidates

        async def _ai_decide(c):
            return True, "looks good"

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)
        monkeypatch.setattr(discovery, "_exclude_held_tokens", _exclude_held_tokens)
        monkeypatch.setattr(discovery, "_safety_check", _safety_check)
        monkeypatch.setattr(discovery, "_insider_check", _insider_check)
        monkeypatch.setattr(discovery, "_ai_decide", _ai_decide)

        db = MockDatabase()
        result = await discovery.discover(db, max_candidates=5, decision_log_enabled=True)

        assert len(result) == 1
        assert result[0].decision_label == DecisionLabel.AI_APPROVE

        # Verify decision was logged
        assert len(db.recorded_decisions) == 1
        assert db.recorded_decisions[0][4] == DecisionLabel.AI_APPROVE.value

    @pytest.mark.asyncio
    async def test_discover_labels_rejected_candidates(self, monkeypatch):
        """AI-rejected candidates get AI_REJECT label."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=10.0,
        )
        candidate = DiscoveryCandidate(
            token_address="Addr222222222222222222222222222222222222222",
            symbol="BAD", chain="solana", price_usd=0.01,
            volume_24h=100000, liquidity_usd=50000, safety_status="Safe",
            price_change_24h=10.0,
        )

        async def _scan_trending():
            return [{"pair": "x"}]

        def _apply_filters_with_labels(_pairs):
            return [candidate], []

        async def _exclude_held_tokens(candidates, _db):
            return candidates

        async def _safety_check(candidates):
            return candidates

        async def _insider_check(candidates):
            return candidates

        async def _ai_decide(c):
            return False, "looks bad"

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)
        monkeypatch.setattr(discovery, "_exclude_held_tokens", _exclude_held_tokens)
        monkeypatch.setattr(discovery, "_safety_check", _safety_check)
        monkeypatch.setattr(discovery, "_insider_check", _insider_check)
        monkeypatch.setattr(discovery, "_ai_decide", _ai_decide)

        db = MockDatabase()
        result = await discovery.discover(db, max_candidates=5, decision_log_enabled=True)

        assert len(result) == 0
        # Should have a single AI_REJECT decision
        reject_decisions = [d for d in db.recorded_decisions if d[4] == DecisionLabel.AI_REJECT.value]
        assert len(reject_decisions) == 1

    @pytest.mark.asyncio
    async def test_discover_labels_heuristic_skip(self, monkeypatch):
        """Low-scoring candidates get HEURISTIC_SKIP label."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=80.0,
        )
        # Candidate with terrible stats → low heuristic score
        weak = DiscoveryCandidate(
            token_address="WeakAddr1111111111111111111111111111111111",
            symbol="WEAK", chain="solana", price_usd=0.001,
            volume_24h=1000, liquidity_usd=20000,
            price_change_24h=-10.0, safety_status="Dangerous",
        )

        async def _scan_trending():
            return [{"pair": "x"}]

        def _apply_filters_with_labels(_pairs):
            return [weak], []

        async def _exclude_held_tokens(candidates, _db):
            return candidates

        async def _safety_check(candidates):
            return candidates

        async def _insider_check(candidates):
            return candidates

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)
        monkeypatch.setattr(discovery, "_exclude_held_tokens", _exclude_held_tokens)
        monkeypatch.setattr(discovery, "_safety_check", _safety_check)
        monkeypatch.setattr(discovery, "_insider_check", _insider_check)

        db = MockDatabase()
        result = await discovery.discover(db, max_candidates=5, decision_log_enabled=True)

        assert len(result) == 0
        skip_decisions = [d for d in db.recorded_decisions if d[4] == DecisionLabel.HEURISTIC_SKIP.value]
        assert len(skip_decisions) == 1

    @pytest.mark.asyncio
    async def test_held_token_logs_original_candidate_context(self, monkeypatch):
        """Held-token decisions should keep original candidate symbol/metrics."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=10.0,
        )
        candidate = DiscoveryCandidate(
            token_address="Held111111111111111111111111111111111111111",
            symbol="HELD",
            chain="solana",
            price_usd=0.123,
            volume_24h=98765,
            liquidity_usd=54321,
        )

        async def _scan_trending():
            return [{"pair": "x"}]

        def _apply_filters_with_labels(_pairs):
            return [candidate], []

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)

        db = MockDatabase(held_addresses={candidate.token_address})
        result = await discovery.discover(db, max_candidates=5, decision_log_enabled=True)

        assert result == []
        assert len(db.recorded_decisions) == 1
        row = db.recorded_decisions[0]
        assert row[4] == DecisionLabel.HELD_TOKEN.value
        assert row[2] == "HELD"
        assert row[5] == candidate.price_usd


class TestApplyFiltersWithLabels:
    """Test that _apply_filters_with_labels returns labeled rejects."""

    def test_chain_reject_labeled(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        pair = _make_pair(chain="ethereum")
        passed, rejects = discovery._apply_filters_with_labels([pair])
        assert len(passed) == 0
        assert len(rejects) == 1
        assert rejects[0][1] == DecisionLabel.FILTER_CHAIN

    def test_missing_chain_reject_labeled_as_parse(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        pair = _make_pair()
        pair.pop("chainId", None)
        passed, rejects = discovery._apply_filters_with_labels([pair])
        assert len(passed) == 0
        assert len(rejects) == 1
        assert rejects[0][1] == DecisionLabel.FILTER_PARSE

    def test_missing_identity_reject_labeled(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
        )
        pair = _make_pair()
        pair["baseToken"] = {"address": "", "symbol": ""}
        passed, rejects = discovery._apply_filters_with_labels([pair])
        assert len(passed) == 0
        assert len(rejects) == 1
        assert rejects[0][1] == DecisionLabel.FILTER_PARSE

    def test_duplicate_address_reject_labeled(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
            min_volume_usd=0, min_liquidity_usd=0, min_market_cap_usd=0,
        )
        pair = _make_pair(address="Dup111111111111111111111111111111111111")
        pair_dup = _make_pair(address="Dup111111111111111111111111111111111111")
        passed, rejects = discovery._apply_filters_with_labels([pair, pair_dup])
        assert len(passed) == 1
        assert len(rejects) == 1
        assert rejects[0][1] == DecisionLabel.FILTER_PARSE

    def test_volume_reject_labeled(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_volume_usd=50000,
        )
        pair = _make_pair(volume_24h=100)  # too low
        passed, rejects = discovery._apply_filters_with_labels([pair])
        assert len(passed) == 0
        assert len(rejects) == 1
        assert rejects[0][1] == DecisionLabel.FILTER_VOLUME

    def test_liquidity_reject_labeled(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_liquidity_usd=25000,
        )
        pair = _make_pair(liquidity_usd=100)  # too low
        passed, rejects = discovery._apply_filters_with_labels([pair])
        assert len(passed) == 0
        assert len(rejects) == 1
        assert rejects[0][1] == DecisionLabel.FILTER_LIQUIDITY

    def test_mcap_reject_labeled(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_market_cap_usd=250000,
        )
        pair = _make_pair(market_cap=100)  # too low
        passed, rejects = discovery._apply_filters_with_labels([pair])
        assert len(passed) == 0
        assert len(rejects) == 1
        assert rejects[0][1] == DecisionLabel.FILTER_MCAP

    def test_price_zero_reject_labeled(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
            min_volume_usd=0, min_liquidity_usd=0, min_market_cap_usd=0,
        )
        pair = _make_pair(price=0)
        passed, rejects = discovery._apply_filters_with_labels([pair])
        assert len(passed) == 0
        assert len(rejects) == 1
        assert rejects[0][1] == DecisionLabel.FILTER_PRICE

    def test_passing_candidate_no_reject(self):
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x",
            min_volume_usd=10000, min_liquidity_usd=10000, min_market_cap_usd=100000,
        )
        pair = _make_pair()
        passed, rejects = discovery._apply_filters_with_labels([pair])
        assert len(passed) == 1
        assert len(rejects) == 0


# ---------------------------------------------------------------------------
# Shadow audit tests
# ---------------------------------------------------------------------------


class TestShadowAudit:
    """Verify shadow position recording during discovery."""

    @pytest.mark.asyncio
    async def test_shadow_positions_recorded_when_enabled(self, monkeypatch):
        """When shadow_audit_enabled=True, approved candidates create shadow positions."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=10.0,
        )
        candidate = DiscoveryCandidate(
            token_address="Addr333333333333333333333333333333333333333",
            symbol="SHADOW", chain="solana", price_usd=0.05,
            volume_24h=100000, liquidity_usd=50000, safety_status="Safe",
            price_change_24h=15.0,
        )

        async def _scan_trending():
            return [{"pair": "x"}]

        def _apply_filters_with_labels(_pairs):
            return [candidate], []

        async def _exclude_held_tokens(candidates, _db):
            return candidates

        async def _safety_check(candidates):
            return candidates

        async def _insider_check(candidates):
            return candidates

        async def _ai_decide(c):
            return True, "buy it"

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)
        monkeypatch.setattr(discovery, "_exclude_held_tokens", _exclude_held_tokens)
        monkeypatch.setattr(discovery, "_safety_check", _safety_check)
        monkeypatch.setattr(discovery, "_insider_check", _insider_check)
        monkeypatch.setattr(discovery, "_ai_decide", _ai_decide)

        db = MockDatabase()
        result = await discovery.discover(
            db, max_candidates=5,
            shadow_audit_enabled=True,
            shadow_check_minutes=60,
            position_size_usd=10.0,
        )

        assert len(result) == 1
        assert len(db.shadow_positions) == 1
        shadow = db.shadow_positions[0]
        assert shadow["token_address"] == "Addr333333333333333333333333333333333333333"
        assert shadow["entry_price"] == 0.05
        assert shadow["notional_usd"] == 10.0
        assert shadow["check_after_minutes"] == 60

    @pytest.mark.asyncio
    async def test_shadow_positions_not_recorded_when_disabled(self, monkeypatch):
        """When shadow_audit_enabled=False, no shadow positions recorded."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=10.0,
        )
        candidate = DiscoveryCandidate(
            token_address="Addr444444444444444444444444444444444444444",
            symbol="NOSHADOW", chain="solana", price_usd=0.05,
            volume_24h=100000, liquidity_usd=50000, safety_status="Safe",
            price_change_24h=15.0,
        )

        async def _scan_trending():
            return [{"pair": "x"}]

        def _apply_filters_with_labels(_pairs):
            return [candidate], []

        async def _exclude_held_tokens(candidates, _db):
            return candidates

        async def _safety_check(candidates):
            return candidates

        async def _insider_check(candidates):
            return candidates

        async def _ai_decide(c):
            return True, "buy"

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)
        monkeypatch.setattr(discovery, "_exclude_held_tokens", _exclude_held_tokens)
        monkeypatch.setattr(discovery, "_safety_check", _safety_check)
        monkeypatch.setattr(discovery, "_insider_check", _insider_check)
        monkeypatch.setattr(discovery, "_ai_decide", _ai_decide)

        db = MockDatabase()
        result = await discovery.discover(db, max_candidates=5, shadow_audit_enabled=False)

        assert len(result) == 1
        assert len(db.shadow_positions) == 0

    @pytest.mark.asyncio
    async def test_decision_log_disabled_skips_recording(self, monkeypatch):
        """When decision_log_enabled=False, no decisions are recorded."""
        discovery = PortfolioDiscovery(
            mcp_manager=MockMCPManager(), api_key="x", min_momentum_score=10.0,
        )
        candidate = DiscoveryCandidate(
            token_address="Addr555555555555555555555555555555555555555",
            symbol="NOLOG", chain="solana", price_usd=0.05,
            volume_24h=100000, liquidity_usd=50000, safety_status="Safe",
            price_change_24h=15.0,
        )

        async def _scan_trending():
            return [{"pair": "x"}]

        def _apply_filters_with_labels(_pairs):
            return [candidate], []

        async def _exclude_held_tokens(candidates, _db):
            return candidates

        async def _safety_check(candidates):
            return candidates

        async def _insider_check(candidates):
            return candidates

        async def _ai_decide(c):
            return True, "buy"

        monkeypatch.setattr(discovery, "_scan_trending", _scan_trending)
        monkeypatch.setattr(discovery, "_apply_filters_with_labels", _apply_filters_with_labels)
        monkeypatch.setattr(discovery, "_exclude_held_tokens", _exclude_held_tokens)
        monkeypatch.setattr(discovery, "_safety_check", _safety_check)
        monkeypatch.setattr(discovery, "_insider_check", _insider_check)
        monkeypatch.setattr(discovery, "_ai_decide", _ai_decide)

        db = MockDatabase()
        result = await discovery.discover(db, max_candidates=5, decision_log_enabled=False)

        assert len(result) == 1
        assert len(db.recorded_decisions) == 0


# ---------------------------------------------------------------------------
# Database integration tests for new tables
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path):
    """Provide a fresh Database connected to a temp file."""
    db = Database(db_path=tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


class TestDatabaseDecisionLog:
    """Test discovery_decisions table operations."""

    @pytest.mark.asyncio
    async def test_record_and_query_decision(self, db):
        await db.record_discovery_decision(
            cycle_id="cycle001",
            token_address="0xABC",
            symbol="TEST",
            chain="solana",
            decision_label="AI_APPROVE",
            price_usd=0.05,
            volume_24h=100000,
            liquidity_usd=50000,
            reasoning="strong momentum",
        )
        rows = await db.get_discovery_decisions(cycle_id="cycle001")
        assert len(rows) == 1
        assert rows[0]["decision_label"] == "ai_approve"
        assert rows[0]["symbol"] == "TEST"

    @pytest.mark.asyncio
    async def test_batch_insert_decisions(self, db):
        batch = [
            ("cycle002", "0xaa", "A", "solana", "filter_volume", None, 1000, None, None, None, "low vol", "{}"),
            ("cycle002", "0xbb", "B", "solana", "ai_approve", 0.01, 100000, 50000, 500000, 75.0, "good", "{}"),
        ]
        await db.record_discovery_decisions_batch(batch)
        rows = await db.get_discovery_decisions(cycle_id="cycle002")
        assert len(rows) == 2
        labels = {r["decision_label"] for r in rows}
        assert labels == {"filter_volume", "ai_approve"}

    @pytest.mark.asyncio
    async def test_batch_insert_normalizes_fields(self, db):
        batch = [
            ("cycle002b", "0xABCDEF", "test", "SoLaNa", "AI_APPROVE", 0.01, 10, 10, 10, 1.0, "ok", {"k": "v"}),
        ]
        await db.record_discovery_decisions_batch(batch)
        rows = await db.get_discovery_decisions(cycle_id="cycle002b")
        assert len(rows) == 1
        assert rows[0]["token_address"] == "0xabcdef"
        assert rows[0]["chain"] == "solana"
        assert rows[0]["symbol"] == "TEST"
        assert rows[0]["decision_label"] == "ai_approve"

    @pytest.mark.asyncio
    async def test_query_by_token_address(self, db):
        await db.record_discovery_decision(
            cycle_id="cycle003",
            token_address="0xTOKEN1",
            symbol="T1",
            chain="solana",
            decision_label="ai_reject",
        )
        await db.record_discovery_decision(
            cycle_id="cycle003",
            token_address="0xTOKEN2",
            symbol="T2",
            chain="solana",
            decision_label="ai_approve",
        )
        rows = await db.get_discovery_decisions(token_address="0xTOKEN1")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "T1"

    @pytest.mark.asyncio
    async def test_query_by_token_address_and_chain(self, db):
        await db.record_discovery_decision(
            cycle_id="cycle004",
            token_address="0xSAME",
            symbol="SOL",
            chain="solana",
            decision_label="ai_approve",
        )
        await db.record_discovery_decision(
            cycle_id="cycle004",
            token_address="0xSAME",
            symbol="ETH",
            chain="ethereum",
            decision_label="ai_reject",
        )
        rows = await db.get_discovery_decisions(token_address="0xSAME", chain="solana")
        assert len(rows) == 1
        assert rows[0]["chain"] == "solana"


class TestDatabaseShadowPositions:
    """Test shadow_positions table operations."""

    @pytest.mark.asyncio
    async def test_add_and_list_shadow_position(self, db):
        shadow_id = await db.add_shadow_position(
            token_address="0xSHADOW1",
            symbol="SHD",
            chain="solana",
            entry_price=0.01,
            notional_usd=10.0,
            momentum_score=75.0,
            reasoning="test buy",
            check_after_minutes=0,  # immediately due
        )
        assert shadow_id > 0

        pending = await db.list_pending_shadow_positions()
        assert len(pending) == 1
        assert pending[0]["token_address"] == "0xshadow1"

    @pytest.mark.asyncio
    async def test_resolve_shadow_position(self, db):
        shadow_id = await db.add_shadow_position(
            token_address="0xSHADOW2",
            symbol="SHD2",
            chain="solana",
            entry_price=0.01,
            notional_usd=10.0,
            check_after_minutes=0,
        )

        resolved = await db.resolve_shadow_position(
            shadow_id=shadow_id,
            price_at_check=0.015,
            pnl_pct=50.0,
        )
        assert resolved is True

        # Should no longer be pending
        pending = await db.list_pending_shadow_positions()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_shadow_summary_no_data(self, db):
        summary = await db.get_shadow_summary()
        assert summary["total"] == 0
        assert summary["min_pnl_pct"] == 0.0
        assert summary["max_pnl_pct"] == 0.0

    @pytest.mark.asyncio
    async def test_shadow_summary_with_data(self, db):
        # Create and resolve two shadow positions
        s1 = await db.add_shadow_position(
            token_address="0xA", symbol="A", chain="solana",
            entry_price=1.0, notional_usd=10.0, check_after_minutes=0,
        )
        s2 = await db.add_shadow_position(
            token_address="0xB", symbol="B", chain="solana",
            entry_price=1.0, notional_usd=10.0, check_after_minutes=0,
        )
        await db.resolve_shadow_position(s1, 1.1, 10.0)  # winner
        await db.resolve_shadow_position(s2, 0.8, -20.0)  # loser

        summary = await db.get_shadow_summary()
        assert summary["total"] == 2
        assert summary["winners"] == 1
        assert summary["losers"] == 1
        assert summary["avg_pnl_pct"] == -5.0  # (10 + -20) / 2

    @pytest.mark.asyncio
    async def test_shadow_summary_respects_limit(self, db):
        s1 = await db.add_shadow_position(
            token_address="0xL1", symbol="L1", chain="solana",
            entry_price=1.0, notional_usd=10.0, check_after_minutes=0,
        )
        s2 = await db.add_shadow_position(
            token_address="0xL2", symbol="L2", chain="solana",
            entry_price=1.0, notional_usd=10.0, check_after_minutes=0,
        )
        s3 = await db.add_shadow_position(
            token_address="0xL3", symbol="L3", chain="solana",
            entry_price=1.0, notional_usd=10.0, check_after_minutes=0,
        )
        await db.resolve_shadow_position(s1, 1.1, 10.0)
        await db.resolve_shadow_position(s2, 1.2, 20.0)
        await db.resolve_shadow_position(s3, 1.3, 30.0)

        summary = await db.get_shadow_summary(limit=2)
        assert summary["total"] == 2

    @pytest.mark.asyncio
    async def test_resolve_idempotent(self, db):
        """Resolving an already-resolved shadow position returns False."""
        shadow_id = await db.add_shadow_position(
            token_address="0xC", symbol="C", chain="solana",
            entry_price=1.0, notional_usd=10.0, check_after_minutes=0,
        )
        assert await db.resolve_shadow_position(shadow_id, 1.5, 50.0) is True
        assert await db.resolve_shadow_position(shadow_id, 2.0, 100.0) is False
