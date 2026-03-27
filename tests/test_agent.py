"""Tests for the AgenticPlanner class, especially malformed function call handling."""

import json
from unittest.mock import MagicMock, AsyncMock
import pytest

from app.agent import AgenticPlanner, AgenticContext
from app.types import MAX_TOOL_RESULT_CHARS as _MAX_TOOL_RESULT_CHARS


class MockCandidate:
    """Mock Gemini response candidate."""
    
    def __init__(self, finish_reason=None, content=None):
        self.finish_reason = finish_reason
        self.content = content


class MockResponse:
    """Mock Gemini response."""
    
    def __init__(self, candidates=None):
        self.candidates = candidates or []


class TestIsMalformedResponse:
    """Tests for _is_malformed_response detection method."""
    
    def setup_method(self):
        """Create a minimal planner for testing helper methods."""
        # Create planner with mocked dependencies
        self.planner = object.__new__(AgenticPlanner)
        self.planner.verbose = False
        self.planner.log_callback = None
    
    def test_detects_malformed_function_call_finish_reason(self):
        """Should detect MALFORMED_FUNCTION_CALL in finish_reason."""
        response = MockResponse([
            MockCandidate(finish_reason="FinishReason.MALFORMED_FUNCTION_CALL")
        ])
        assert self.planner._is_malformed_response(response) is True
    
    def test_does_not_flag_normal_stop_reason(self):
        """Should not flag normal STOP finish reason."""
        response = MockResponse([
            MockCandidate(finish_reason="FinishReason.STOP")
        ])
        assert self.planner._is_malformed_response(response) is False
    
    def test_does_not_flag_safety_reason(self):
        """Should not flag SAFETY finish reason (handled elsewhere)."""
        response = MockResponse([
            MockCandidate(finish_reason="FinishReason.SAFETY")
        ])
        assert self.planner._is_malformed_response(response) is False
    
    def test_handles_empty_candidates(self):
        """Should return False for response with no candidates."""
        response = MockResponse([])
        assert self.planner._is_malformed_response(response) is False
    
    def test_handles_no_finish_reason(self):
        """Should return False when candidate has no finish_reason attribute."""
        candidate = MagicMock(spec=[])  # No attributes
        response = MockResponse([candidate])
        assert self.planner._is_malformed_response(response) is False


class TestBuildRecoveryMessage:
    """Tests for _build_recovery_message method."""
    
    def setup_method(self):
        """Create a minimal planner for testing helper methods."""
        self.planner = object.__new__(AgenticPlanner)
        self.planner.verbose = False
        self.planner.log_callback = None
    
    def test_first_attempt_provides_step_guidance(self):
        """First recovery attempt should provide step-by-step guidance."""
        query = "find trending solana tokens"
        msg = self.planner._build_recovery_message(query, attempt=1)
        
        assert "ONE tool call at a time" in msg
        assert query in msg
        assert "required parameters" in msg
    
    def test_second_attempt_asks_for_text_explanation(self):
        """Second recovery attempt should ask for text-only explanation."""
        query = "complex query here"
        msg = self.planner._build_recovery_message(query, attempt=2)
        
        assert "TEXT only" in msg
        assert "What data you need" in msg


class TestAgenticContext:
    """Tests for AgenticContext dataclass."""
    
    def test_default_values(self):
        """Should have correct default values."""
        ctx = AgenticContext()
        assert ctx.iteration == 0
        assert ctx.total_tool_calls == 0
        assert ctx.malformed_retries == 0
        assert ctx.original_query == ""
        assert ctx.tool_calls == []
        assert ctx.tokens_found == []
    
    def test_stores_original_query(self):
        """Should store original query when provided."""
        query = "test query"
        ctx = AgenticContext(original_query=query)
        assert ctx.original_query == query
    
    def test_tracks_malformed_retries(self):
        """Should track malformed retry count."""
        ctx = AgenticContext()
        ctx.malformed_retries += 1
        assert ctx.malformed_retries == 1
        ctx.malformed_retries += 1
        assert ctx.malformed_retries == 2


class TestTruncateResult:
    """Tests for _truncate_result helper."""

    def setup_method(self):
        """Create a minimal planner for testing helper methods."""
        self.planner = object.__new__(AgenticPlanner)
        self.planner.verbose = False
        self.planner.log_callback = None

    def test_truncates_long_string_by_char_count(self):
        """Long strings should still be truncated by character count."""
        long_string = "x" * (_MAX_TOOL_RESULT_CHARS + 25)
        truncated = self.planner._truncate_result(long_string)

        assert len(truncated) <= _MAX_TOOL_RESULT_CHARS
        assert truncated.endswith(" chars]")
        assert "\n... [truncated " in truncated

    @pytest.mark.parametrize(
        ("payload", "payload_type"),
        [
            ({f"key{i}": "x" * 120 for i in range(300)}, "dict"),
            (["x" * 120 for _ in range(300)], "list"),
        ],
    )
    def test_large_structures_return_json_serializable_preview(self, payload, payload_type):
        """Large dict/list payloads should return truncated JSON-safe preview objects."""
        truncated = self.planner._truncate_result(payload)

        assert truncated["_truncated"] is True
        assert truncated["_type"] == payload_type
        assert truncated["_total_items"] == len(payload)
        assert truncated["_preview_items"] < truncated["_total_items"]
        assert truncated["_omitted_items"] > 0
        assert "_preview" in truncated

        json.dumps(truncated)

    def test_problematic_structured_payloads_return_preview_without_raising(self):
        """Circular and non-serializable payloads should not break truncation."""
        circular_payload = {}
        circular_payload["self"] = circular_payload
        non_serializable_payload = {f"key{i}": object() for i in range(400)}

        for payload in (circular_payload, non_serializable_payload):
            truncated = self.planner._truncate_result(payload)

            assert truncated["_truncated"] is True
            assert truncated["_type"] == "dict"
            assert "_preview" in truncated
            json.dumps(truncated)
