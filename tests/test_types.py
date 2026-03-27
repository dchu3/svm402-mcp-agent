"""Tests for DEX Agentic Bot."""

import pytest
from app.types import PlannerResult


def test_planner_result():
    """Test PlannerResult dataclass."""
    result = PlannerResult(message="Test message")
    assert result.message == "Test message"
    assert result.tokens == []
    assert result.raw_data == {}


def test_planner_result_with_tokens():
    """Test PlannerResult with token data."""
    tokens = [{"address": "0x123", "symbol": "TEST", "chainId": "base"}]
    result = PlannerResult(message="Found tokens", tokens=tokens)
    assert len(result.tokens) == 1
    assert result.tokens[0]["symbol"] == "TEST"
