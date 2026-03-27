"""Shared types and constants for the application."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

# Maximum characters for tool result payloads sent to the LLM context.
# Used by both AgenticPlanner and PortfolioDiscovery decision loops.
MAX_TOOL_RESULT_CHARS = 8000


class DecisionLabel(str, Enum):
    """Reason codes for discovery pipeline decisions.

    Each candidate is tagged with the label of the gate where it was
    either approved or rejected.
    """

    # Deterministic filter gates
    FILTER_CHAIN = "filter_chain"
    FILTER_VOLUME = "filter_volume"
    FILTER_LIQUIDITY = "filter_liquidity"
    FILTER_MCAP = "filter_mcap"
    FILTER_AGE = "filter_age"
    FILTER_PRICE = "filter_price"
    FILTER_PARSE = "filter_parse"

    # Pipeline gates
    HELD_TOKEN = "held_token"
    SAFETY_REJECTED = "safety_rejected"
    INSIDER_REJECTED = "insider_rejected"
    DEDUP = "dedup"
    HEURISTIC_SKIP = "heuristic_skip"
    AI_POOL_CAP = "ai_pool_cap"

    # AI decision outcomes
    AI_REJECT = "ai_reject"
    AI_APPROVE = "ai_approve"
    AI_APPROVE_CAPPED = "ai_approve_capped"


@dataclass
class PlannerResult:
    """Result from the agentic planner."""

    message: str
    tokens: List[Dict[str, str]] = field(default_factory=list)
    raw_data: Dict[str, Any] = field(default_factory=dict)
